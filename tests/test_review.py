"""Planning outcomes and the review queue."""

from __future__ import annotations

import csv
import threading
from decimal import Decimal
from pathlib import Path

from paperless_field_extractor.extract import AmountResult, DateResult, Extraction
from paperless_field_extractor.models import Document
from paperless_field_extractor.ocr.router import OcrRouter
from paperless_field_extractor.reasons import Confidence, Corroboration, Reason
from paperless_field_extractor.review import (
    REVIEW_COLUMNS,
    DocumentPlan,
    Outcome,
    plan_document,
    plan_documents,
    plan_rows,
    review_rows,
    skipped_fields,
    summarise,
    write_csv,
)

from .conftest import document_payload
from .fakes import FakeRunner

AMOUNT_FIELD = 1
DATE_FIELD = 4
GOOD = "ABN 12 345 678 901\n25/10/2026\nGST $4.55\nTotal $50.00\n"


def _document(doc_id: int = 1, fields: list[dict[str, object]] | None = None) -> Document:
    return Document.model_validate(document_payload(doc_id, custom_fields=fields))


def _extraction(
    amount: str | None = "50.00",
    date: str | None = "2026-10-25",
    *,
    confidence: Confidence = Confidence.HIGH,
    amount_reasons: tuple[Reason, ...] = (),
    date_reasons: tuple[Reason, ...] = (),
) -> Extraction:
    return Extraction(
        None
        if amount is None
        else AmountResult(
            Decimal(amount), "AUD", confidence, (Corroboration.TOTAL_REPEATED,), "Total $50.00"
        ),
        amount_reasons,
        None if date is None else DateResult(date, confidence, False, "25/10/2026"),
        date_reasons,
    )


def _plan(
    own: Extraction | None,
    content: Extraction | None,
    *,
    doc_id: int = 1,
    amount_filled: bool = False,
    date_filled: bool = False,
) -> DocumentPlan:
    return plan_document(
        _document(doc_id),
        amount_filled=amount_filled,
        date_filled=date_filled,
        input_kind="image",
        own=own,
        content=content,
    )


def test_two_agreeing_sources_make_both_fields_ready() -> None:
    plan = _plan(_extraction(), _extraction())

    assert plan.amount.outcome is Outcome.READY
    assert (plan.amount.currency, plan.amount.value) == ("AUD", "50.00")
    assert plan.date.outcome is Outcome.READY
    assert plan.date.value == "2026-10-25"
    assert not plan.needs_review


def test_a_filled_field_is_skipped_and_never_planned_for_writing() -> None:
    plan = _plan(_extraction(), _extraction(), amount_filled=True)

    assert plan.amount == plan.amount.__class__(Outcome.SKIPPED)
    assert plan.date.outcome is Outcome.READY
    assert plan.needs_text


def test_a_document_with_both_fields_filled_needs_no_text() -> None:
    plan = _plan(None, None, amount_filled=True, date_filled=True)

    assert not plan.needs_text
    assert not plan.needs_review


def test_disagreeing_sources_go_to_review_with_both_candidates() -> None:
    plan = _plan(_extraction("50.00"), _extraction("56.00"))

    assert plan.amount.outcome is Outcome.REVIEW
    assert Reason.SOURCE_DISAGREEMENT in plan.amount.reasons
    assert plan.amount.own == "AUD 50.00"
    assert plan.amount.content == "AUD 56.00"
    assert plan.amount.value is None


def test_a_missing_source_goes_to_review() -> None:
    plan = _plan(_extraction(), None)

    assert plan.amount.outcome is Outcome.REVIEW
    assert Reason.SOURCE_MISSING in plan.amount.reasons
    assert plan.amount.own == "AUD 50.00"
    assert plan.amount.content == ""


def test_review_rows_hold_only_documents_in_review_lowest_confidence_first() -> None:
    both_ready = _plan(_extraction(), _extraction(), doc_id=1)
    date_only = _plan(_extraction(), _extraction(date=None), doc_id=2)
    nothing = _plan(_extraction(None, None), _extraction(None, None), doc_id=3)
    plans = [both_ready, date_only, nothing]

    rows = review_rows(plans, "https://paperless.test/")

    assert [row["doc_id"] for row in rows] == ["3", "2"]
    assert rows[0]["link"] == "https://paperless.test/documents/3/details"
    assert (rows[0]["currency"], rows[0]["amount"], rows[0]["date"]) == ("", "", "")
    assert list(rows[0]) == list(REVIEW_COLUMNS)


def test_equal_confidence_review_rows_order_by_document_id() -> None:
    plans = [_plan(None, None, doc_id=doc_id) for doc_id in (9, 4, 6)]

    assert [row["doc_id"] for row in review_rows(plans, "https://p")] == ["4", "6", "9"]


def test_plan_rows_show_a_value_only_for_ready_fields() -> None:
    ready = _plan(_extraction(), _extraction(), doc_id=1)
    review = _plan(_extraction("50.00"), _extraction("56.00"), doc_id=2)

    rows = plan_rows([review, ready])

    by_key = {(row["doc_id"], row["field"]): row for row in rows}
    assert by_key[("1", "amount")]["value"] == "50.00"
    assert by_key[("2", "amount")]["value"] == ""
    assert "source-disagreement" in by_key[("2", "amount")]["reasons"]
    assert [row["doc_id"] for row in rows] == ["1", "1", "2", "2"]


def test_summary_holds_counts_only() -> None:
    plans = [
        _plan(_extraction(), _extraction(), doc_id=1),
        _plan(_extraction("50.00"), _extraction("56.00"), doc_id=2),
        _plan(None, None, doc_id=3, amount_filled=True, date_filled=True),
    ]

    summary = summarise(plans)

    assert summary["documents"] == 3
    assert summary["amount"] == {"ready": 1, "review": 1, "skipped": 1}
    assert summary["in_review"] == 1
    assert summary["reasons"] == {"source-disagreement": 1}


def test_a_formula_in_ocr_text_is_neutralised_in_the_csv(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"

    write_csv(path, ("evidence", "reasons"), [{"evidence": "=HYPERLINK(1)", "reasons": "-x"}])

    with path.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row == {"evidence": "'=HYPERLINK(1)", "reasons": "'-x"}


def test_a_field_holding_whitespace_counts_as_empty() -> None:
    document = _document(
        1,
        [{"field": AMOUNT_FIELD, "value": "  "}, {"field": DATE_FIELD, "value": "2026-01-02"}],
    )

    assert skipped_fields(document, AMOUNT_FIELD, DATE_FIELD) == (False, True)


class _Counting:
    """A client that records which originals were downloaded, and from which threads."""

    def __init__(self) -> None:
        self.downloaded: list[int] = []
        self.threads: set[int] = set()
        self._lock = threading.Lock()

    def download_original(self, doc_id: int) -> bytes:
        with self._lock:
            self.downloaded.append(doc_id)
            self.threads.add(threading.get_ident())
        return b"not an image"


def test_originals_are_downloaded_only_for_documents_with_an_empty_field() -> None:
    filled = _document(
        1,
        [
            {"field": AMOUNT_FIELD, "value": "AUD1.00"},
            {"field": DATE_FIELD, "value": "2026-01-02"},
        ],
    )
    empty = Document.model_validate(
        document_payload(2, content=GOOD, mime_type="text/plain", original_file_name="r.txt")
    )
    client = _Counting()
    router = OcrRouter(runner=FakeRunner({}))

    plans = plan_documents(
        client,  # type: ignore[arg-type]
        router,
        [filled, empty],
        amount_field_id=AMOUNT_FIELD,
        date_field_id=DATE_FIELD,
    )

    assert client.downloaded == [2]
    assert [plan.doc_id for plan in plans] == [1, 2]
    assert not plans[0].needs_text
