"""Stage 3: plan what ``apply`` would write, and queue the rest for review.

Each document gets one outcome per field:

- ``ready``: the field is empty, both text sources agree and no blocking reason remains.
  A dry run only reports it; nothing is written.
- ``review``: the field is empty but a check failed or the sources disagree.
- ``skipped``: the field already holds a value. A human value is never overwritten.

``review.csv`` has one row per document with a field in review, lowest confidence first,
and blank ``currency``, ``amount`` and ``date`` columns for the owner to fill. Both files
hold receipt text and values, so the caller keeps them in a git-ignored directory.
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from paperless_field_extractor.amounts import canonical_amount
from paperless_field_extractor.client import PaperlessClient
from paperless_field_extractor.extract import (
    AmountResult,
    DateResult,
    Extraction,
    Reconciled,
    extract,
    reconcile_amount,
    reconcile_date,
)
from paperless_field_extractor.lines import lines_from_text
from paperless_field_extractor.models import Document
from paperless_field_extractor.ocr.parallel import parallel_map
from paperless_field_extractor.ocr.router import OcrRouter
from paperless_field_extractor.ocr.types import InputKind
from paperless_field_extractor.probe import MAX_DOWNLOAD_WORKERS
from paperless_field_extractor.reasons import BLOCKING, Reason

REVIEW_COLUMNS = (
    "doc_id",
    "link",
    "input",
    "reasons",
    "amount_own",
    "amount_content",
    "date_own",
    "date_content",
    "evidence",
    "currency",
    "amount",
    "date",
)
PLAN_COLUMNS = ("doc_id", "field", "outcome", "currency", "value", "reasons")

# Spreadsheet programs run a cell that starts with one of these as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class Outcome(StrEnum):
    READY = "ready"
    REVIEW = "review"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class FieldPlan:
    outcome: Outcome
    value: str | None = None
    currency: str | None = None
    reasons: tuple[Reason, ...] = ()
    own: str = ""
    content: str = ""
    evidence: str = ""


@dataclass(frozen=True, slots=True)
class DocumentPlan:
    doc_id: int
    input_kind: str
    amount: FieldPlan
    date: FieldPlan

    @property
    def needs_review(self) -> bool:
        return Outcome.REVIEW in (self.amount.outcome, self.date.outcome)

    @property
    def needs_text(self) -> bool:
        """False when both fields are filled, so there is nothing to extract."""
        return (
            self.amount.outcome is not Outcome.SKIPPED or self.date.outcome is not Outcome.SKIPPED
        )


def skipped_fields(
    document: Document, amount_field_id: int, date_field_id: int
) -> tuple[bool, bool]:
    """Whether the amount and the date already hold a value."""
    return (
        not document.field_is_empty(amount_field_id),
        not document.field_is_empty(date_field_id),
    )


def plan_document(
    document: Document,
    *,
    amount_filled: bool,
    date_filled: bool,
    input_kind: str,
    own: Extraction | None,
    content: Extraction | None,
) -> DocumentPlan:
    """Plan one document. A missing text source counts as a source with nothing in it."""
    own = own or _nothing()
    content = content or _nothing()
    amount = (
        FieldPlan(Outcome.SKIPPED)
        if amount_filled
        else _amount_plan(own, content, reconcile_amount(own, content))
    )
    date = (
        FieldPlan(Outcome.SKIPPED)
        if date_filled
        else _date_plan(own, content, reconcile_date(own, content))
    )
    return DocumentPlan(document.id, input_kind, amount, date)


def plan_documents(
    client: PaperlessClient,
    router: OcrRouter,
    documents: Sequence[Document],
    *,
    amount_field_id: int,
    date_field_id: int,
) -> list[DocumentPlan]:
    """Plan every document, in order. Originals are downloaded only where a field is empty."""
    # Downloads are capped separately from the tool workers, so a many-core host
    # does not hammer Paperless.
    pool = min(router.workers, MAX_DOWNLOAD_WORKERS)

    def one(document: Document) -> DocumentPlan:
        amount_filled, date_filled = skipped_fields(document, amount_field_id, date_field_id)
        if amount_filled and date_filled:
            return plan_document(
                document,
                amount_filled=True,
                date_filled=True,
                input_kind=InputKind.UNSUPPORTED.value,
                own=None,
                content=None,
            )
        original = client.download_original(document.id)
        text = router.read(
            original, mime_type=document.mime_type, file_name=document.original_file_name
        )
        return plan_document(
            document,
            amount_filled=amount_filled,
            date_filled=date_filled,
            input_kind=text.kind.value,
            own=extract(text.lines, header_date=text.header_date),
            content=extract(lines_from_text(document.content or "")),
        )

    return parallel_map(one, documents, pool)


def review_rows(plans: Sequence[DocumentPlan], base_url: str) -> list[dict[str, str]]:
    """One row per document with a field in review, lowest confidence first."""
    queue = sorted((plan for plan in plans if plan.needs_review), key=_review_order)
    return [_review_row(plan, base_url) for plan in queue]


def plan_rows(plans: Sequence[DocumentPlan]) -> list[dict[str, str]]:
    """One row per document field, in document order."""
    rows: list[dict[str, str]] = []
    for plan in sorted(plans, key=lambda item: item.doc_id):
        for name, field_plan in (("amount", plan.amount), ("date", plan.date)):
            rows.append(
                {
                    "doc_id": str(plan.doc_id),
                    "field": name,
                    "outcome": field_plan.outcome.value,
                    "currency": field_plan.currency or "",
                    "value": (field_plan.value or "")
                    if field_plan.outcome is Outcome.READY
                    else "",
                    "reasons": _reasons(field_plan.reasons),
                }
            )
    return rows


def summarise(plans: Sequence[DocumentPlan]) -> dict[str, object]:
    """Counts only: outcomes per field and how often each reason holds a field back."""
    reasons: Counter[str] = Counter()
    for plan in plans:
        for field_plan in (plan.amount, plan.date):
            reasons.update(reason.value for reason in field_plan.reasons)
    return {
        "documents": len(plans),
        "amount": _outcome_counts(plan.amount for plan in plans),
        "date": _outcome_counts(plan.date for plan in plans),
        "in_review": sum(1 for plan in plans if plan.needs_review),
        "reasons": dict(sorted(reasons.items())),
    }


def write_csv(path: Path, columns: Sequence[str], rows: Sequence[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _cell(row.get(column, "")) for column in columns})


def _nothing() -> Extraction:
    return Extraction(None, (Reason.NO_AMOUNT,), None, (Reason.NO_DATE,))


def _amount_plan(own: Extraction, content: Extraction, reconciled: Reconciled) -> FieldPlan:
    return FieldPlan(
        outcome=Outcome.READY if reconciled.write else Outcome.REVIEW,
        value=reconciled.value,
        currency=reconciled.currency,
        reasons=reconciled.reasons,
        own=_amount_candidate(own),
        content=_amount_candidate(content),
        evidence=_evidence(own.amount or content.amount),
    )


def _date_plan(own: Extraction, content: Extraction, reconciled: Reconciled) -> FieldPlan:
    return FieldPlan(
        outcome=Outcome.READY if reconciled.write else Outcome.REVIEW,
        value=reconciled.value,
        reasons=reconciled.reasons,
        own=own.date.value if own.date else "",
        content=content.date.value if content.date else "",
        evidence=_evidence(own.date or content.date),
    )


def _evidence(result: AmountResult | DateResult | None) -> str:
    return result.evidence if result is not None else ""


def _amount_candidate(extraction: Extraction) -> str:
    amount = extraction.amount
    if amount is None:
        return ""
    return f"{amount.currency or '?'} {canonical_amount(amount.value, amount.currency)}"


def _review_order(plan: DocumentPlan) -> tuple[int, int, int]:
    """Fewest ready fields first, then most blocking reasons, then document id."""
    fields = (plan.amount, plan.date)
    ready = sum(1 for item in fields if item.outcome is Outcome.READY)
    blocking = sum(1 for item in fields for reason in item.reasons if reason in BLOCKING)
    return ready, -blocking, plan.doc_id


def _review_row(plan: DocumentPlan, base_url: str) -> dict[str, str]:
    reasons = (*plan.amount.reasons, *plan.date.reasons)
    evidence = " | ".join(item for item in (plan.amount.evidence, plan.date.evidence) if item)
    return {
        "doc_id": str(plan.doc_id),
        "link": f"{base_url.rstrip('/')}/documents/{plan.doc_id}/details",
        "input": plan.input_kind,
        "reasons": _reasons(reasons),
        "amount_own": plan.amount.own,
        "amount_content": plan.amount.content,
        "date_own": plan.date.own,
        "date_content": plan.date.content,
        "evidence": evidence,
        "currency": "",
        "amount": "",
        "date": "",
    }


def _reasons(reasons: Sequence[Reason]) -> str:
    return ";".join(dict.fromkeys(reason.value for reason in reasons))


def _outcome_counts(fields: Iterable[FieldPlan]) -> dict[str, int]:
    counts = Counter(item.outcome for item in fields)
    return {outcome.value: counts.get(outcome, 0) for outcome in Outcome}


def _cell(value: str) -> str:
    return f"'{value}" if value.startswith(_FORMULA_PREFIXES) else value


__all__ = [
    "PLAN_COLUMNS",
    "REVIEW_COLUMNS",
    "DocumentPlan",
    "FieldPlan",
    "Outcome",
    "plan_document",
    "plan_rows",
    "review_rows",
    "skipped_fields",
    "summarise",
    "write_csv",
]
