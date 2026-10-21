"""The Stage 0 probe against a fake Paperless server."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
import pytest

from paperless_field_extractor.errors import StartupError
from paperless_field_extractor.ocr.config import OcrConfig
from paperless_field_extractor.ocr.router import OcrRouter
from paperless_field_extractor.ocr.runner import CommandResult
from paperless_field_extractor.probe import dump_json, run_probe, select_fy_documents
from paperless_field_extractor.settings import Settings

from .conftest import (
    document_payload,
    field_payload,
    json_response,
    make_client,
    page_payload,
    tag_payload,
)
from .fakes import FakeRunner, barcode_png, ok, png_bytes

TAG = tag_payload(1, "tax deductible", 47)
FIELDS = [
    field_payload(1, "Transaction Amount", "monetary"),
    field_payload(4, "Transaction Date", "date"),
    field_payload(3, "Reference"),
]
SECRET_TITLE = "SECRET-SUPPLIER-TITLE"
SECRET_NAME = "secret-file-name"
SECRET_CONTENT = "SECRET-OCR-CONTENT total 99.99"
SECRET_VALUE = "AUD99.99"

DOCUMENTS = [
    document_payload(
        10,
        title=SECRET_TITLE,
        content=SECRET_CONTENT,
        mime_type="application/pdf",
        original_file_name=f"{SECRET_NAME}.pdf",
        custom_fields=[{"field": 1, "value": SECRET_VALUE}, {"field": 4, "value": None}],
    ),
    document_payload(
        11,
        title=SECRET_TITLE,
        content="",
        mime_type="image/jpeg",
        original_file_name=f"{SECRET_NAME}.jpg",
    ),
    document_payload(
        12,
        title=SECRET_TITLE,
        content="Total $5.00",
        mime_type="message/rfc822",
        original_file_name=f"{SECRET_NAME}.eml",
        custom_fields=[{"field": 1, "value": "AUD5.00"}, {"field": 4, "value": "2026-10-01"}],
    ),
]
ORIGINALS = {10: b"%PDF-1.7 text pdf", 11: barcode_png("RCPT123456"), 12: b"Subject: x\n\nbody"}


def _server(
    tags: list[dict[str, object]] | None = None,
    documents: list[dict[str, object]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags/":
            return json_response(page_payload([TAG] if tags is None else tags))
        if path == "/api/custom_fields/":
            return json_response(page_payload(FIELDS))
        if path == "/api/documents/":
            return json_response(page_payload(DOCUMENTS if documents is None else documents))
        if path.endswith("/download/"):
            return httpx.Response(200, content=ORIGINALS[int(path.split("/")[3])])
        return httpx.Response(404)

    return handler


def _router(workers: int = 0) -> OcrRouter:
    def pdftotext(args: Sequence[str]) -> CommandResult:
        return ok("ABN 12 345 678 901\nTotal $50.00 incl GST\nThank you for your custom\n")

    def pdftoppm(args: Sequence[str]) -> CommandResult:
        Path(f"{args[-1]}-1.png").write_bytes(png_bytes())
        return ok()

    return OcrRouter(
        runner=FakeRunner({"pdftotext": pdftotext, "pdftoppm": pdftoppm}),
        config=OcrConfig(workers=workers),
    )


def _probe(**options: object) -> dict[str, object]:
    with make_client(_server()) as client:
        return run_probe(
            client,
            Settings(),
            router=options.pop("router", None),  # type: ignore[arg-type]
            save_raw=options.pop("save_raw", None),  # type: ignore[arg-type]
            scan_barcodes=bool(options.pop("scan_barcodes", False)),
            checked_at="2026-10-22T00:00:00+00:00",
        )


def test_the_report_describes_the_instance_fields_and_coverage() -> None:
    report = _probe()

    assert report["paperless"] == {
        "api_version": "10",
        "server_version": "3.3.2",
        "origin_rewrites": [],
    }
    assert report["tag"] == {
        "name": "tax deductible",
        "matches": [{"id": 1, "document_count": 47}],
        "near_misses": [],
    }
    assert report["expected_fields"] == {
        "amount": {"name": "Transaction Amount", "exists": True, "id": 1, "data_type": "monetary"},
        "date": {"name": "Transaction Date", "exists": True, "id": 4, "data_type": "date"},
    }
    assert report["fy"] == {"start": "2025-07-01", "end": "2026-06-30", "document_count": 3}
    assert report["coverage"] == {
        "amount": {"filled": 2, "empty": 0, "absent": 1},
        "date": {"filled": 1, "empty": 1, "absent": 1},
    }
    assert report["mix"] == {"eml": 1, "image": 1, "pdf": 1}


def test_the_report_never_contains_titles_file_names_content_or_field_values() -> None:
    text = dump_json(_probe())

    for secret in (SECRET_TITLE, SECRET_NAME, SECRET_CONTENT, SECRET_VALUE, "99.99"):
        assert secret not in text


def test_the_report_is_canonical_json() -> None:
    text = dump_json(_probe())

    assert text.endswith("}\n")
    assert json.loads(text) == _probe()
    assert text == dump_json(_probe())


def test_a_near_miss_tag_name_is_reported_so_a_wrong_name_is_visible() -> None:
    with make_client(_server(tags=[tag_payload(9, "tax-deductible")])) as client:
        report = run_probe(
            client, Settings(), router=None, save_raw=None, scan_barcodes=False, checked_at="x"
        )

    assert report["tag"]["near_misses"] == ["tax-deductible"]
    assert report["fy"]["document_count"] == 0
    assert any("no tag named" in note for note in report["notes"])


def test_a_token_that_sees_no_tags_gets_the_permissions_hint() -> None:
    with make_client(_server(tags=[])) as client:
        report = run_probe(
            client, Settings(), router=None, save_raw=None, scan_barcodes=False, checked_at="x"
        )

    assert any("per object type" in note for note in report["notes"])


def test_the_unverified_items_are_listed() -> None:
    notes = _probe()["notes"]

    assert any("PATCH" in note and "Tika" in note for note in notes)  # type: ignore[attr-defined]


def test_downloading_splits_pdfs_by_text_layer_and_saves_raw_data(tmp_path: Path) -> None:
    report = _probe(router=_router(), save_raw=tmp_path / "raw")

    assert report["mix"] == {"eml": 1, "image": 1, "pdf-text-layer": 1}
    folder = tmp_path / "raw" / "10"
    assert (folder / "content.txt").read_text(encoding="utf-8") == SECRET_CONTENT
    assert (folder / "original.pdf").read_bytes() == ORIGINALS[10]
    assert (tmp_path / "raw" / "11" / "original.jpg").read_bytes() == ORIGINALS[11]
    assert (tmp_path / "raw" / "12" / "original.eml").exists()


def test_a_scanned_pdf_is_distinguished_from_a_text_pdf(tmp_path: Path) -> None:
    blank = OcrRouter(runner=FakeRunner({"pdftotext": lambda args: ok("  \n")}))

    report = _probe(router=blank, save_raw=tmp_path)

    assert report["mix"] == {"eml": 1, "image": 1, "pdf-scanned": 1}


def test_barcodes_are_summarised_and_the_raw_payload_stays_in_the_raw_directory(
    tmp_path: Path,
) -> None:
    report = _probe(router=_router(), save_raw=tmp_path, scan_barcodes=True)

    image = next(doc for doc in report["documents"] if doc["id"] == 11)  # type: ignore[attr-defined]
    assert image["barcodes"] == [
        {
            "format": "Code128",
            "length": 10,
            "charset": "alphanumeric",
            "is_url": False,
            "looks_like_amount": False,
            "looks_like_date": False,
        }
    ]
    assert report["barcodes"] == {
        "documents_scanned": 3,
        "documents_with_barcodes": 1,
        "barcodes_found": 1,
        "any_amount_like": False,
        "any_date_like": False,
        "any_url": False,
    }
    assert "RCPT123456" not in dump_json(report)
    raw = json.loads((tmp_path / "11" / "barcodes.json").read_text(encoding="utf-8"))
    assert raw == [{"format": "Code128", "text": "RCPT123456"}]


def test_a_parallel_probe_gives_the_same_report_as_a_sequential_one(tmp_path: Path) -> None:
    sequential = _probe(router=_router(1), save_raw=tmp_path / "a", scan_barcodes=True)
    parallel = _probe(router=_router(8), save_raw=tmp_path / "b", scan_barcodes=True)

    assert dump_json(parallel) == dump_json(sequential)
    assert [p.name for p in sorted((tmp_path / "b").iterdir())] == ["10", "11", "12"]


def test_downloading_without_a_router_is_a_programming_error(tmp_path: Path) -> None:
    with make_client(_server()) as client, pytest.raises(ValueError, match="router"):
        run_probe(
            client,
            Settings(),
            router=None,
            save_raw=tmp_path,
            scan_barcodes=False,
            checked_at="x",
        )


def test_select_fy_documents_needs_exactly_one_matching_tag() -> None:
    with make_client(_server(tags=[])) as client, pytest.raises(StartupError, match="exactly one"):
        select_fy_documents(client, Settings())

    with make_client(_server(tags=[TAG, tag_payload(2, "tax deductible")])) as client:
        with pytest.raises(StartupError):
            select_fy_documents(client, Settings())


def test_select_fy_documents_returns_the_documents() -> None:
    with make_client(_server()) as client:
        assert [d.id for d in select_fy_documents(client, Settings())] == [10, 11, 12]
