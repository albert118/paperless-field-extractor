"""Date finding: day-first for AU and JP, never guessing between day and month."""

from __future__ import annotations

import pytest

from paperless_field_extractor.dates import find_dates
from paperless_field_extractor.lines import Line, lines_from_text


def _isos(text: str, *, day_first: bool | None = True) -> list[str | None]:
    return [hit.iso for hit in find_dates(lines_from_text(text), day_first=day_first)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Date: 25/10/2026", "2026-10-25"),
        ("25-10-2026", "2026-10-25"),
        ("25.10.2026", "2026-10-25"),
        ("25/10/26", "2026-10-25"),
        ("2026-10-25", "2026-10-25"),
        ("2026/10/25", "2026-10-25"),
        ("25 10 2026", "2026-10-25"),
        ("25 Oct 2026", "2026-10-25"),
        ("25th October 2026", "2026-10-25"),
        ("25 Sept 2026", "2026-09-25"),
        ("Oct 25, 2026", "2026-10-25"),
        ("October 25 2026", "2026-10-25"),
        ("2026年10月25日", "2026-10-25"),
        ("2026年 1月 5日", "2026-01-05"),
        ("令和8年10月25日", "2026-10-25"),
        ("令和元年5月1日", "2019-05-01"),
    ],
)
def test_supported_formats(text: str, expected: str) -> None:
    assert _isos(text) == [expected]


@pytest.mark.parametrize(
    "text",
    ["31/02/2026", "32/13/2026", "25/10/1999", "25/10/2100", "12:30:45", "1 2 99", "Total 12/2026"],
)
def test_invalid_or_out_of_range_dates_are_rejected(text: str) -> None:
    assert _isos(text) == []


def test_day_first_documents_read_ambiguous_numeric_dates_day_first() -> None:
    assert _isos("03/04/2026", day_first=True) == ["2026-04-03"]


def test_an_unknown_order_reports_both_readings_instead_of_guessing() -> None:
    (hit,) = find_dates(lines_from_text("03/04/2026"), day_first=None)
    assert hit.iso is None
    assert hit.alternatives == ("2026-03-04", "2026-04-03")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("10/25/2026", "2026-10-25"), ("25/10/2026", "2026-10-25"), ("05/05/2026", "2026-05-05")],
)
def test_an_unknown_order_still_resolves_when_only_one_reading_is_valid(
    text: str, expected: str
) -> None:
    assert _isos(text, day_first=None) == [expected]


def test_dates_come_back_in_reading_order_across_lines() -> None:
    lines = [Line("Printed 30/10/2026"), Line("Sale 25/10/2026")]
    hits = find_dates(lines, day_first=True)
    assert [hit.iso for hit in hits] == ["2026-10-30", "2026-10-25"]
    assert [hit.line_index for hit in hits] == [0, 1]


def test_an_iso_date_is_not_also_read_as_a_day_first_date() -> None:
    assert _isos("2026-10-25") == ["2026-10-25"]
