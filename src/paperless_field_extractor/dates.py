"""Finding calendar dates in receipt text.

Day and month order is the trap. Australian and Japanese receipts read day-first
(or year-first); US receipts read month-first. When both readings of a numeric date
are valid and differ, and the document gives no way to choose, the date is
*ambiguous* and is not guessed. Years outside 2000 to 2099 are rejected, which
filters most OCR junk.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Sequence
from dataclasses import dataclass

from paperless_field_extractor.lines import Line

_MIN_YEAR = 2000
_MAX_YEAR = 2099
_REIWA_FIRST_YEAR = 2018  # Reiwa 1 is 2019, so Reiwa n is 2018 + n.

_MONTHS: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_YEAR_FIRST_RE = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_NUMERIC_RE = re.compile(r"(?<![\d/.-])(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})(?![\d/])")
_SPACED_RE = re.compile(r"(?<!\d)(\d{1,2})\s+(\d{1,2})\s+(\d{4})(?!\d)")
_DAY_MONTH_NAME_RE = re.compile(
    r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s\-]+([A-Za-z]{3,9})\.?,?[\s\-]+(\d{4}|\d{2})(?!\d)"
)
_MONTH_NAME_DAY_RE = re.compile(
    r"(?<![A-Za-z])([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)"
)
_JAPANESE_RE = re.compile(r"(?<!\d)(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")
_REIWA_RE = re.compile(r"令和\s*(\d{1,2}|元)年\s*(\d{1,2})月\s*(\d{1,2})日")


@dataclass(frozen=True, slots=True)
class DateHit:
    """A date found in the text. ``iso`` is ``None`` when the order is ambiguous."""

    iso: str | None
    line_index: int
    column: int
    alternatives: tuple[str, ...] = ()


def find_dates(lines: Sequence[Line], *, day_first: bool | None) -> list[DateHit]:
    """Every date in *lines*, in reading order.

    *day_first* is ``True`` for day-first documents, and ``None`` when the order
    is unknown, so a numeric date with two valid readings is reported ambiguous.
    """
    hits: dict[tuple[int, int], DateHit] = {}
    for index, line in enumerate(lines):
        for column, hit in _scan_line(line.text, day_first):
            hits.setdefault((index, column), DateHit(hit.iso, index, column, hit.alternatives))
    return [hits[key] for key in sorted(hits)]


def _scan_line(text: str, day_first: bool | None) -> list[tuple[int, DateHit]]:
    found: list[tuple[int, DateHit]] = []

    def add(column: int, iso: str | None, alternatives: tuple[str, ...] = ()) -> None:
        if iso is not None or alternatives:
            found.append((column, DateHit(iso, 0, column, alternatives)))

    for m in _YEAR_FIRST_RE.finditer(text):
        add(m.start(), _iso(int(m[1]), int(m[2]), int(m[3])))
    for m in _JAPANESE_RE.finditer(text):
        add(m.start(), _iso(int(m[1]), int(m[2]), int(m[3])))
    for m in _REIWA_RE.finditer(text):
        era_year = 1 if m[1] == "元" else int(m[1])
        add(m.start(), _iso(_REIWA_FIRST_YEAR + era_year, int(m[2]), int(m[3])))
    for m in _NUMERIC_RE.finditer(text):
        year = _year(m[3])
        iso, alternatives = _resolve_order(int(m[1]), int(m[2]), year, day_first)
        add(m.start(), iso, alternatives)
    for m in _SPACED_RE.finditer(text):
        iso, alternatives = _resolve_order(int(m[1]), int(m[2]), int(m[3]), day_first)
        add(m.start(), iso, alternatives)
    for m in _DAY_MONTH_NAME_RE.finditer(text):
        month = _MONTHS.get(m[2].lower())
        if month is not None:
            add(m.start(), _iso(_year(m[3]), month, int(m[1])))
    for m in _MONTH_NAME_DAY_RE.finditer(text):
        month = _MONTHS.get(m[1].lower())
        if month is not None:
            add(m.start(), _iso(int(m[3]), month, int(m[2])))
    return found


def _resolve_order(
    first: int, second: int, year: int | None, day_first: bool | None
) -> tuple[str | None, tuple[str, ...]]:
    """Read ``first/second/year`` as day-first or month-first, never guessing."""
    day_month = _iso(year, second, first)
    month_day = _iso(year, first, second)
    if day_month is None and month_day is None:
        return None, ()
    if day_month is None or month_day is None or day_month == month_day:
        return day_month or month_day, ()
    if day_first is True:
        return day_month, ()
    return None, tuple(sorted((day_month, month_day)))


def _year(text: str) -> int | None:
    value = int(text)
    if len(text) == 2:
        return 2000 + value
    return value


def _iso(year: int | None, month: int, day: int) -> str | None:
    if year is None or not _MIN_YEAR <= year <= _MAX_YEAR:
        return None
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None
