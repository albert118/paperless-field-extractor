"""The golden set: hand-confirmed true values used to measure extraction accuracy.

``golden.csv`` has one row per receipt: ``doc_id,currency,amount,date,notes``. The
owner fills ``currency``, ``amount`` (a plain decimal, no symbol or thousands
separator) and ``date`` (ISO) from the physical receipt. A row with no amount and
no date is pending and is skipped. The file holds real receipt values, so it lives
in a git-ignored path.

The acceptance rule from the design is *zero wrong values at high confidence*:
``wrong_ready`` in the report must be 0 before ``apply`` is allowed to write.
"""

from __future__ import annotations

import csv
import datetime
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from paperless_field_extractor.amounts import EXPONENTS, canonical_amount
from paperless_field_extractor.errors import GoldenSetError

GOLDEN_COLUMNS = ("doc_id", "currency", "amount", "date", "notes")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLAIN_AMOUNT_RE = re.compile(r"^\d+(?:\.\d+)?$")


@dataclass(frozen=True, slots=True)
class GoldenRow:
    doc_id: int
    currency: str | None
    amount: Decimal | None
    date: str | None


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one pipeline decided for one field of one document."""

    value: str | None
    currency: str | None
    ready: bool


@dataclass(frozen=True, slots=True)
class FieldReport:
    labelled: int
    correct_ready: int
    correct_not_ready: int
    wrong_ready: int
    wrong_not_ready: int
    missing: int

    def to_dict(self) -> dict[str, int]:
        return {
            "labelled": self.labelled,
            "correct_ready": self.correct_ready,
            "correct_not_ready": self.correct_not_ready,
            "wrong_ready": self.wrong_ready,
            "wrong_not_ready": self.wrong_not_ready,
            "missing": self.missing,
        }


@dataclass(frozen=True, slots=True)
class EvalReport:
    pending: int
    amount: FieldReport
    date: FieldReport

    @property
    def passes_acceptance(self) -> bool:
        """True when no field has a wrong value marked ready to write."""
        return self.amount.wrong_ready == 0 and self.date.wrong_ready == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending": self.pending,
            "amount": self.amount.to_dict(),
            "date": self.date.to_dict(),
            "passes_acceptance": self.passes_acceptance,
        }

    def render(self) -> str:
        lines = [f"pending rows skipped: {self.pending}"]
        for name, report in (("amount", self.amount), ("date", self.date)):
            lines.append(
                f"{name}: {report.labelled} labelled | ready+correct {report.correct_ready}"
                f" | review+correct {report.correct_not_ready}"
                f" | WRONG ready {report.wrong_ready}"
                f" | wrong review {report.wrong_not_ready} | missing {report.missing}"
            )
        verdict = "PASS" if self.passes_acceptance else "FAIL: a wrong value is marked ready"
        lines.append(f"acceptance (zero wrong at ready): {verdict}")
        return "\n".join(lines)


def write_template(path: Path, doc_ids: Iterable[int], *, force: bool = False) -> None:
    """Write a golden CSV with blank values. An existing file is never overwritten silently."""
    if path.exists() and not force:
        raise GoldenSetError(f"{path} already exists; pass --force to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(GOLDEN_COLUMNS)
        for doc_id in sorted(doc_ids):
            writer.writerow([doc_id, "", "", "", ""])


def load_golden(path: Path) -> tuple[list[GoldenRow], int]:
    """The labelled rows, and the count of pending (fully blank) rows."""
    try:
        handle = path.open(encoding="utf-8", newline="")
    except OSError as exc:
        raise GoldenSetError(f"cannot read the golden set {path}: {exc.strerror}") from exc
    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or any(c not in reader.fieldnames for c in GOLDEN_COLUMNS):
            raise GoldenSetError(f"{path} must have the columns {', '.join(GOLDEN_COLUMNS)}")
        rows: list[GoldenRow] = []
        pending = 0
        seen: set[int] = set()
        for number, raw in enumerate(reader, start=2):
            row = _parse_row(raw, number)
            if row.doc_id in seen:
                raise GoldenSetError(f"line {number}: duplicate doc_id {row.doc_id}")
            seen.add(row.doc_id)
            if row.amount is None and row.date is None:
                pending += 1
            else:
                rows.append(row)
    return rows, pending


def evaluate(
    rows: Iterable[GoldenRow],
    amounts: Mapping[int, Outcome],
    dates: Mapping[int, Outcome],
    *,
    pending: int = 0,
) -> EvalReport:
    """Score per-field outcomes against the golden rows."""
    amount_rows = []
    date_rows = []
    for row in rows:
        if row.amount is not None:
            expected = canonical_amount(row.amount, row.currency)
            amount_rows.append((amounts.get(row.doc_id), (expected, row.currency)))
        if row.date is not None:
            date_rows.append((dates.get(row.doc_id), (row.date, None)))
    return EvalReport(
        pending=pending,
        amount=_score(amount_rows, check_currency=True),
        date=_score(date_rows, check_currency=False),
    )


def _score(
    cases: Iterable[tuple[Outcome | None, tuple[str, str | None]]], *, check_currency: bool
) -> FieldReport:
    counts = dict.fromkeys(
        (
            "labelled",
            "correct_ready",
            "correct_not_ready",
            "wrong_ready",
            "wrong_not_ready",
            "missing",
        ),
        0,
    )
    for outcome, (expected_value, expected_currency) in cases:
        counts["labelled"] += 1
        if outcome is None or outcome.value is None:
            counts["missing"] += 1
            continue
        correct = outcome.value == expected_value and (
            not check_currency or outcome.currency == expected_currency
        )
        suffix = "ready" if outcome.ready else "not_ready"
        counts[f"{'correct' if correct else 'wrong'}_{suffix}"] += 1
    return FieldReport(**counts)


def _parse_row(raw: Mapping[str, str | None], number: int) -> GoldenRow:
    doc_id_text = (raw["doc_id"] or "").strip()
    if not doc_id_text.isdigit():
        raise GoldenSetError(f"line {number}: doc_id must be a whole number, got {doc_id_text!r}")
    currency = (raw["currency"] or "").strip().upper() or None
    amount_text = (raw["amount"] or "").strip()
    date = (raw["date"] or "").strip() or None

    amount: Decimal | None = None
    if amount_text:
        if _PLAIN_AMOUNT_RE.match(amount_text) is None:
            raise GoldenSetError(
                f"line {number}: amount must be a plain decimal like 12.34, got {amount_text!r}"
            )
        try:
            amount = Decimal(amount_text)
        except InvalidOperation as exc:
            raise GoldenSetError(f"line {number}: unreadable amount {amount_text!r}") from exc
        if currency not in EXPONENTS:
            raise GoldenSetError(
                f"line {number}: currency must be one of {', '.join(EXPONENTS)}, got {currency!r}"
            )
        exponent = EXPONENTS[currency]
        decimals = len(amount_text.partition(".")[2])
        if decimals > exponent:
            raise GoldenSetError(
                f"line {number}: {currency} has {exponent} decimal places, got {amount_text!r}"
            )
    if date is not None and not _is_calendar_date(date):
        raise GoldenSetError(f"line {number}: date must be YYYY-MM-DD, got {date!r}")
    return GoldenRow(int(doc_id_text), currency, amount, date)


def _is_calendar_date(text: str) -> bool:
    if _ISO_DATE_RE.match(text) is None:
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True
