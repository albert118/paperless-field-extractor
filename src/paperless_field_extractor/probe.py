"""Stage 0: read-only facts about the live Paperless instance and the receipt originals.

The probe answers: which custom fields exist and what types they have, how many
FY receipts are emails, text PDFs, scanned PDFs or photos, how much of each field
is already filled, and (optionally) whether receipt barcodes carry an amount, a
date or a supplier.

``probe.json`` carries counts, ids and field metadata only: never titles, file
names, field values, OCR text or barcode payloads. It is safe to share. Anything
that is receipt data goes to the optional raw directory, which must be ignored by
git.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from paperless_field_extractor import __version__
from paperless_field_extractor.barcodes import BarcodeHit, PayloadSummary, scan_image, summarise
from paperless_field_extractor.client import PaperlessClient
from paperless_field_extractor.errors import StartupError
from paperless_field_extractor.models import CustomFieldDef, Document
from paperless_field_extractor.ocr.router import OcrRouter, classify_input
from paperless_field_extractor.ocr.types import InputKind
from paperless_field_extractor.settings import Settings

_NAME_NOISE_RE = re.compile(r"[\s_\-]+")
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".heic"})
_UNVERIFIED = (
    "The custom_fields PATCH semantics, the monetary write format and the Tika image tag "
    "are not probed: the probe is read-only and Paperless does not report the Tika tag."
)


def run_probe(
    client: PaperlessClient,
    settings: Settings,
    *,
    router: OcrRouter | None,
    save_raw: Path | None,
    scan_barcodes: bool,
    checked_at: str,
) -> dict[str, Any]:
    """Probe the instance. *router* is required when downloading originals."""
    download = save_raw is not None or scan_barcodes
    if download and router is None:
        raise ValueError("an OCR router is required to download and classify originals")

    notes: list[str] = [_UNVERIFIED]
    tags = client.list_tags()
    matches = [tag for tag in tags if tag.name == settings.deduction_tag]
    near_misses = sorted(
        tag.name
        for tag in tags
        if tag.name != settings.deduction_tag
        and _normal(tag.name) == _normal(settings.deduction_tag)
    )
    if not tags:
        notes.append(
            "the token sees no tags at all: Paperless permissions are per object type, and "
            "a missing permission returns an empty list, not an error"
        )
    elif not matches:
        notes.append(f"no tag named {settings.deduction_tag!r}: the FY document set is empty")
    elif len(matches) > 1:
        notes.append(f"{len(matches)} tags named {settings.deduction_tag!r}: selection skipped")

    fields = client.list_custom_fields()
    amount_field = _find_field(fields, settings.field_amount)
    date_field = _find_field(fields, settings.field_date)
    for name, found in ((settings.field_amount, amount_field), (settings.field_date, date_field)):
        if found is None:
            notes.append(f"custom field {name!r} does not exist")

    documents: list[Document] = []
    if len(matches) == 1:
        documents = client.list_documents(
            tag_id=matches[0].id,
            created_from=settings.fy_start,
            created_to=settings.fy_end,
            include_content=True,
        )

    per_document: list[dict[str, Any]] = []
    barcode_summaries: list[list[PayloadSummary]] = []
    for document in documents:
        entry, summaries = _probe_document(
            client,
            document,
            amount_field,
            date_field,
            router=router if download else None,
            save_raw=save_raw,
            scan_barcodes=scan_barcodes,
        )
        per_document.append(entry)
        if summaries is not None:
            barcode_summaries.append(summaries)

    return {
        "checked_at": checked_at,
        "tool_version": __version__,
        "paperless": {
            "api_version": client.api_version,
            "server_version": client.server_version,
            "origin_rewrites": list(client.origin_rewrites),
        },
        "tag": {
            "name": settings.deduction_tag,
            "matches": [{"id": tag.id, "document_count": tag.document_count} for tag in matches],
            "near_misses": near_misses,
        },
        "custom_fields": [
            {
                "id": field.id,
                "name": field.name,
                "data_type": field.data_type,
                "default_currency": field.default_currency(),
            }
            for field in sorted(fields, key=lambda item: item.id)
        ],
        "expected_fields": {
            "amount": _expected(settings.field_amount, amount_field),
            "date": _expected(settings.field_date, date_field),
        },
        "fy": {
            "start": settings.fy_start,
            "end": settings.fy_end,
            "document_count": len(documents),
        },
        "coverage": {
            "amount": _coverage(documents, amount_field),
            "date": _coverage(documents, date_field),
        },
        "mix": dict(sorted(Counter(entry["input"] for entry in per_document).items())),
        "documents": per_document,
        "barcodes": _barcode_report(barcode_summaries) if scan_barcodes else None,
        "notes": notes,
    }


def select_fy_documents(
    client: PaperlessClient, settings: Settings, *, include_content: bool = False
) -> list[Document]:
    """The FY documents carrying exactly one tag named ``settings.deduction_tag``."""
    matches = [tag for tag in client.list_tags() if tag.name == settings.deduction_tag]
    if len(matches) != 1:
        raise StartupError(
            f"expected exactly one tag named {settings.deduction_tag!r}, found {len(matches)}"
            " (a token without permission on tags sees none)"
        )
    return client.list_documents(
        tag_id=matches[0].id,
        created_from=settings.fy_start,
        created_to=settings.fy_end,
        include_content=include_content,
    )


def dump_json(report: dict[str, Any]) -> str:
    """Canonical JSON: sorted keys, two-space indent, trailing newline."""
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _probe_document(
    client: PaperlessClient,
    document: Document,
    amount_field: CustomFieldDef | None,
    date_field: CustomFieldDef | None,
    *,
    router: OcrRouter | None,
    save_raw: Path | None,
    scan_barcodes: bool,
) -> tuple[dict[str, Any], list[PayloadSummary] | None]:
    kind = classify_input(document.mime_type, document.original_file_name, b"")
    entry: dict[str, Any] = {
        "id": document.id,
        "input": kind.value,
        "content_chars": len(document.content or ""),
        "amount_field": _state(document, amount_field),
        "date_field": _state(document, date_field),
    }
    if router is None:
        return entry, None

    original = client.download_original(document.id)
    kind = classify_input(document.mime_type, document.original_file_name, original)
    entry["input"] = kind.value
    if kind is InputKind.PDF:
        entry["input"] = "pdf-text-layer" if router.pdf_has_text_layer(original) else "pdf-scanned"

    hits: list[BarcodeHit] = []
    summaries: list[PayloadSummary] | None = None
    if scan_barcodes:
        hits = _scan(router, kind, original)
        summaries = [summarise(hit) for hit in hits]
        entry["barcodes"] = [summary.to_dict() for summary in summaries]
    if save_raw is not None:
        _save_raw(save_raw, document, original, kind, hits)
    return entry, summaries


def _scan(router: OcrRouter, kind: InputKind, original: bytes) -> list[BarcodeHit]:
    if kind is InputKind.IMAGE:
        return list(scan_image(original))
    if kind is InputKind.PDF:
        hits: list[BarcodeHit] = []
        for page in router.render_pdf_pages(original):
            hits.extend(scan_image(page))
        return hits
    return []


def _save_raw(
    root: Path, document: Document, original: bytes, kind: InputKind, hits: Sequence[BarcodeHit]
) -> None:
    folder = root / str(document.id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "content.txt").write_text(document.content or "", encoding="utf-8")
    (folder / f"original{_suffix(document, kind)}").write_bytes(original)
    if hits:
        payload = [{"format": hit.format, "text": hit.text} for hit in hits]
        (folder / "barcodes.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )


def _suffix(document: Document, kind: InputKind) -> str:
    if kind is InputKind.EML:
        return ".eml"
    if kind is InputKind.PDF:
        return ".pdf"
    suffix = Path(document.original_file_name or "").suffix.lower()
    return suffix if suffix in _IMAGE_SUFFIXES else ".img"


def _barcode_report(per_document: Sequence[Sequence[PayloadSummary]]) -> dict[str, Any]:
    flat = [summary for summaries in per_document for summary in summaries]
    return {
        "documents_scanned": len(per_document),
        "documents_with_barcodes": sum(1 for summaries in per_document if summaries),
        "barcodes_found": len(flat),
        "any_amount_like": any(summary.looks_like_amount for summary in flat),
        "any_date_like": any(summary.looks_like_date for summary in flat),
        "any_url": any(summary.is_url for summary in flat),
    }


def _find_field(fields: Sequence[CustomFieldDef], name: str) -> CustomFieldDef | None:
    return next((field for field in fields if field.name == name), None)


def _expected(name: str, field: CustomFieldDef | None) -> dict[str, Any]:
    if field is None:
        return {"name": name, "exists": False}
    return {"name": name, "exists": True, "id": field.id, "data_type": field.data_type}


def _state(document: Document, field: CustomFieldDef | None) -> str:
    if field is None or not document.has_field(field.id):
        return "absent"
    return "empty" if document.field_is_empty(field.id) else "filled"


def _coverage(documents: Sequence[Document], field: CustomFieldDef | None) -> dict[str, int]:
    counts = Counter(_state(document, field) for document in documents)
    return {state: counts.get(state, 0) for state in ("filled", "empty", "absent")}


def _normal(name: str) -> str:
    return _NAME_NOISE_RE.sub("", name.lower())
