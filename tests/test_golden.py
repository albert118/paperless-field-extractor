"""The golden set: template, loading, validation and scoring."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from paperless_field_extractor.errors import GoldenSetError
from paperless_field_extractor.golden import (
    GoldenRow,
    Outcome,
    evaluate,
    load_golden,
    write_template,
)

HEADER = "doc_id,currency,amount,date,notes\n"


def _golden(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "golden.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    return path


def test_the_template_has_one_blank_row_per_document_in_id_order(tmp_path: Path) -> None:
    path = tmp_path / "scratch" / "golden.csv"

    write_template(path, [30, 10, 20])

    assert path.read_text(encoding="utf-8") == HEADER + "10,,,,\n20,,,,\n30,,,,\n"


def test_the_template_never_overwrites_existing_work(tmp_path: Path) -> None:
    path = _golden(tmp_path, "10,AUD,12.34,2026-10-25,\n")

    with pytest.raises(GoldenSetError, match="already exists"):
        write_template(path, [10])
    assert "12.34" in path.read_text(encoding="utf-8")

    write_template(path, [10], force=True)
    assert "12.34" not in path.read_text(encoding="utf-8")


def test_labelled_rows_load_and_blank_rows_are_counted_as_pending(tmp_path: Path) -> None:
    rows, pending = load_golden(
        _golden(tmp_path, "10,AUD,12.34,2026-10-25,x\n11,,,,\n12,JPY,1100,,\n13,,,2026-10-01,\n")
    )

    assert rows == [
        GoldenRow(10, "AUD", Decimal("12.34"), "2026-10-25"),
        GoldenRow(12, "JPY", Decimal("1100"), None),
        GoldenRow(13, None, None, "2026-10-01"),
    ]
    assert pending == 1


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("abc,AUD,1.00,,", "doc_id"),
        ('10,AUD,"12,34",,', "plain decimal"),
        ("10,AUD,$12.34,,", "plain decimal"),
        ('10,AUD,"1,234.00",,', "plain decimal"),
        ("10,,12.34,,", "currency"),
        ("10,EUR,12.34,,", "currency"),
        ("10,JPY,1100.50,,", "decimal places"),
        ("10,AUD,12.345,,", "decimal places"),
        ("10,AUD,12.34,25/10/2026,", "YYYY-MM-DD"),
        ("10,AUD,12.34,2026-13-01,", "YYYY-MM-DD"),
    ],
)
def test_malformed_rows_are_rejected_with_the_line_number(
    tmp_path: Path, row: str, message: str
) -> None:
    with pytest.raises(GoldenSetError, match=message):
        load_golden(_golden(tmp_path, row + "\n"))


def test_duplicate_ids_and_missing_columns_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(GoldenSetError, match="duplicate"):
        load_golden(_golden(tmp_path, "10,AUD,1.00,,\n10,AUD,2.00,,\n"))

    path = tmp_path / "bad.csv"
    path.write_text("doc_id,amount\n1,2\n", encoding="utf-8")
    with pytest.raises(GoldenSetError, match="columns"):
        load_golden(path)


def test_a_missing_file_is_a_golden_set_error(tmp_path: Path) -> None:
    with pytest.raises(GoldenSetError, match="cannot read"):
        load_golden(tmp_path / "nope.csv")


def _rows() -> list[GoldenRow]:
    return [
        GoldenRow(1, "AUD", Decimal("50.00"), "2026-10-25"),
        GoldenRow(2, "AUD", Decimal("10"), "2026-10-26"),
        GoldenRow(3, "JPY", Decimal("1100"), None),
        GoldenRow(4, "AUD", Decimal("5.00"), "2026-10-27"),
        GoldenRow(5, "USD", Decimal("9.99"), None),
    ]


def test_scoring_separates_correct_wrong_ready_and_missing() -> None:
    amounts = {
        1: Outcome("50.00", "AUD", True),  # correct and ready
        2: Outcome("10.00", "AUD", False),  # correct (50 == 50.00) but sent to review
        3: Outcome("1100", "JPY", True),
        4: Outcome("6.00", "AUD", False),  # wrong, but caught by review
        # doc 5 missing
    }
    dates = {1: Outcome("2026-10-25", None, True), 2: Outcome("2026-10-01", None, True)}

    report = evaluate(_rows(), amounts, dates)

    assert report.amount.to_dict() == {
        "labelled": 5,
        "correct_ready": 2,
        "correct_not_ready": 1,
        "wrong_ready": 0,
        "wrong_not_ready": 1,
        "missing": 1,
    }
    assert report.date.wrong_ready == 1
    assert report.date.missing == 1
    assert not report.passes_acceptance


def test_acceptance_passes_only_with_zero_wrong_values_marked_ready() -> None:
    amounts = {1: Outcome("50.00", "AUD", True), 4: Outcome("6.00", "AUD", False)}
    dates = {1: Outcome("2026-10-25", None, True)}

    report = evaluate(_rows()[:1] + _rows()[3:4], amounts, dates)

    assert report.passes_acceptance
    assert "PASS" in report.render()


def test_a_wrong_currency_is_a_wrong_amount() -> None:
    report = evaluate(
        [GoldenRow(1, "USD", Decimal("9.99"), None)],
        {1: Outcome("9.99", "AUD", True)},
        {},
    )

    assert report.amount.wrong_ready == 1
    assert "WRONG ready 1" in report.render()


def test_pending_rows_are_reported() -> None:
    assert evaluate([], {}, {}, pending=4).to_dict()["pending"] == 4
