"""Deterministic extraction of a receipt's total and date from text lines.

The rules follow ``parse.js`` in actual-flows (label preference, currency from
evidence, AU date formats) with these deliberate changes:

- Amounts are ``Decimal`` from a strict per-currency grammar. Ambiguous formats
  are reported, not guessed (see ``amounts``).
- AUD, USD and JPY are supported. Other currencies are detected and withheld.
- A labelled total is read from the label's own line, or from a value-only line
  directly below it, instead of an 80 character window.
- A bare ``$`` is AUD or USD only on evidence (see ``currency``).
- The document's ``created`` date is never used as the receipt date.
- Confidence ``high`` needs a label and an independent corroboration of the total.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from paperless_field_extractor.amounts import (
    EXPONENTS,
    Token,
    TokenKind,
    canonical_amount,
    classify,
    iter_tokens,
)
from paperless_field_extractor.currency import (
    CurrencyEvidence,
    resolve,
    scan_currency,
)
from paperless_field_extractor.dates import DateHit, find_dates
from paperless_field_extractor.lines import Line
from paperless_field_extractor.reasons import BLOCKING, Confidence, Corroboration, Reason

DEFAULT_MIN_WORD_CONFIDENCE = 60.0

# Label priority, most specific first. Latin labels use word boundaries; CJK
# labels cannot, because there are no spaces between words.
_TOTAL_LABELS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\btotal\b",
        r"総合計|合計",
        r"\bamount\s+due\b",
        r"ご?請求金額",
        r"\bgst\s+incl\b",
        r"\bbalance\b",
        r"\bto\s+pay\b",
        r"\bpay\s+this\s+amount\b",
        r"お会計",
        r"お支払金額",
    )
)
# A "total" that is not the receipt total: "Total GST", "Total items", "GST total".
_EXCLUDED_AFTER = re.compile(
    r"^\s*[:\-]?\s*(?:gst|vat|tax|discount|savings?|items?|qty|quantity|points?|change"
    r"|cash|tendered|rounding)\b|^\s*(?:点数|個数|点|個)",
    re.IGNORECASE,
)
_EXCLUDED_BEFORE = re.compile(
    r"(?:gst|vat|tax|discount|savings?|items?|points?|sub|net)\s*[:\-]?\s*$", re.IGNORECASE
)
_SUBTOTAL_LABEL = re.compile(r"\bsub\s*-?\s*total\b|小計", re.IGNORECASE)
_TAX_LABEL = re.compile(r"\b(?:gst|vat|tax)\b|消費税|税", re.IGNORECASE)
_TAX_EXCLUDE = re.compile(
    r"tax\s+invoice|gst\s+free|\babn\b|\btax\s+id\b|exempt|非課税|免税", re.IGNORECASE
)
_CREDIT_NOTE = re.compile(
    r"\b(?:refund|credit\s*-?\s*note|cashback)\b|返金|払戻|払い戻し", re.IGNORECASE
)
_VALUE_ONLY_LINE = re.compile(r"^\s*[^\d\s]{0,4}\s*[\d.,]+\s*[^\d\s]{0,4}\s*$")

_TOLERANCE: dict[int, Decimal] = {2: Decimal("0.01"), 0: Decimal(1)}
_EVIDENCE_LIMIT = 120


@dataclass(frozen=True, slots=True)
class AmountResult:
    value: Decimal
    currency: str | None
    confidence: Confidence
    corroboration: tuple[Corroboration, ...]
    evidence: str


@dataclass(frozen=True, slots=True)
class DateResult:
    value: str
    confidence: Confidence
    from_header: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class Extraction:
    amount: AmountResult | None
    amount_reasons: tuple[Reason, ...]
    date: DateResult | None
    date_reasons: tuple[Reason, ...]

    @property
    def amount_ready(self) -> bool:
        """True when the amount is high confidence with no blocking reason."""
        return (
            self.amount is not None
            and self.amount.confidence is Confidence.HIGH
            and not (set(self.amount_reasons) & BLOCKING)
        )

    @property
    def date_ready(self) -> bool:
        return (
            self.date is not None
            and self.date.confidence is Confidence.HIGH
            and not (set(self.date_reasons) & BLOCKING)
        )


@dataclass(slots=True)
class _Hit:
    value: Decimal
    line_index: int
    token: Token
    labelled: bool
    negative: bool


@dataclass(slots=True)
class _Search:
    hits: list[_Hit] = field(default_factory=list)
    ambiguous: bool = False


def extract(
    lines: Sequence[Line],
    *,
    header_date: str | None = None,
    min_word_confidence: float = DEFAULT_MIN_WORD_CONFIDENCE,
) -> Extraction:
    """Extract the receipt total and date from *lines*."""
    evidence = scan_currency(lines)
    amount, amount_reasons = _extract_amount(lines, evidence, min_word_confidence)
    currency = amount.currency if amount is not None else evidence.single_code
    date, date_reasons = _extract_date(lines, currency, header_date)
    return Extraction(amount, tuple(amount_reasons), date, tuple(date_reasons))


def _extract_amount(
    lines: Sequence[Line], evidence: CurrencyEvidence, min_word_confidence: float
) -> tuple[AmountResult | None, list[Reason]]:
    search, labelled = _find_total(lines, evidence.single_code)
    if not search.hits:
        return None, [Reason.AMBIGUOUS_AMOUNT if search.ambiguous else Reason.NO_AMOUNT]

    first = search.hits[0]
    reasons: list[Reason] = []
    if len({hit.value for hit in search.hits}) > 1:
        reasons.append(Reason.CONFLICTING_TOTALS)
    if first.negative:
        reasons.append(Reason.NEGATIVE_AMOUNT)
    if any(_CREDIT_NOTE.search(line.text) for line in lines):
        reasons.append(Reason.CREDIT_NOTE)

    decision = resolve(evidence, first.token.adjacent)
    if decision.reason is not None:
        reasons.append(decision.reason)

    line = lines[first.line_index]
    if line.min_conf is not None and line.min_conf < min_word_confidence:
        reasons.append(Reason.LOW_WORD_CONFIDENCE)

    corroboration = _corroborate(lines, first.value, decision.code) if decision.code else ()
    if not labelled:
        reasons.append(Reason.NO_TOTAL_LABEL)
        confidence = Confidence.LOW
    elif corroboration:
        confidence = Confidence.HIGH
    else:
        reasons.append(Reason.NO_CORROBORATION)
        confidence = Confidence.MEDIUM

    result = AmountResult(
        value=first.value,
        currency=decision.code,
        confidence=confidence,
        corroboration=corroboration,
        evidence=_snippet(line.text),
    )
    return result, reasons


def _find_total(lines: Sequence[Line], doc_code: str | None) -> tuple[_Search, bool]:
    """Search by label priority; fall back to a marker-attached number."""
    for label in _TOTAL_LABELS:
        search = _Search()
        for index, line in enumerate(lines):
            for match in label.finditer(line.text):
                if _excluded(line.text, match):
                    continue
                _collect_after(lines, index, match.end(), doc_code, search)
        if search.hits or search.ambiguous:
            return search, True
    return _find_unlabelled(lines, doc_code), False


def _excluded(text: str, match: re.Match[str]) -> bool:
    return (
        _EXCLUDED_AFTER.match(text[match.end() :]) is not None
        or _EXCLUDED_BEFORE.search(text[: match.start()]) is not None
    )


def _collect_after(
    lines: Sequence[Line], index: int, position: int, doc_code: str | None, search: _Search
) -> None:
    """The first meaningful token after a label, on its line or the value-only line below."""
    if _collect_from(lines[index].text, index, position, doc_code, search):
        return
    for below in range(index + 1, min(index + 3, len(lines))):
        text = lines[below].text
        if not text.strip():
            continue
        if _VALUE_ONLY_LINE.match(text):
            _collect_from(text, below, 0, doc_code, search)
        return


def _collect_from(
    text: str, index: int, position: int, doc_code: str | None, search: _Search
) -> bool:
    for token in iter_tokens(text, offset=position):
        value = classify(token, token.adjacent.code or doc_code)
        if value.kind is TokenKind.SKIP:
            continue
        if value.kind is TokenKind.AMBIGUOUS or value.value is None:
            search.ambiguous = True
            return True
        search.hits.append(_Hit(value.value, index, token, True, token.negative))
        return True
    return False


def _find_unlabelled(lines: Sequence[Line], doc_code: str | None) -> _Search:
    search = _Search()
    for index, line in enumerate(lines):
        if _SUBTOTAL_LABEL.search(line.text) or _TAX_LABEL.search(line.text):
            continue
        for token in iter_tokens(line.text):
            if not token.adjacent.symbol:
                continue
            value = classify(token, token.adjacent.code or doc_code)
            if value.kind is TokenKind.VALID and value.value is not None:
                search.hits.append(_Hit(value.value, index, token, False, token.negative))
                return search
    return search


def _corroborate(lines: Sequence[Line], total: Decimal, currency: str) -> tuple[Corroboration, ...]:
    exponent = EXPONENTS.get(currency, 2)
    tolerance = _TOLERANCE[exponent]
    found: list[Corroboration] = []
    if _count_equal(lines, total, currency) >= 2:
        found.append(Corroboration.TOTAL_REPEATED)
    taxes = _labelled_values(lines, _TAX_LABEL, currency, skip=_TAX_EXCLUDE)
    if _tax_is_included(total, taxes, currency, tolerance):
        found.append(Corroboration.TAX_INCLUDED)
    subtotals = _labelled_values(lines, _SUBTOTAL_LABEL, currency)
    if any(
        abs(subtotal + tax - total) <= tolerance
        for subtotal in subtotals
        for tax in taxes
        if subtotal < total
    ):
        found.append(Corroboration.SUBTOTAL_PLUS_TAX)
    return tuple(found)


def _count_equal(lines: Sequence[Line], total: Decimal, currency: str) -> int:
    count = 0
    for line in lines:
        for token in iter_tokens(line.text):
            value = classify(token, token.adjacent.code or currency)
            if value.kind is TokenKind.VALID and value.value == total:
                count += 1
    return count


def _labelled_values(
    lines: Sequence[Line],
    label: re.Pattern[str],
    currency: str,
    *,
    skip: re.Pattern[str] | None = None,
) -> list[Decimal]:
    """The first valid amount after *label* on every line that carries it."""
    values: list[Decimal] = []
    for line in lines:
        if skip is not None and skip.search(line.text):
            continue
        match = label.search(line.text)
        if match is None:
            continue
        for token in iter_tokens(line.text, offset=match.end()):
            value = classify(token, token.adjacent.code or currency)
            if value.kind is TokenKind.SKIP:
                continue
            if value.kind is TokenKind.VALID and value.value is not None:
                values.append(value.value)
            break
    return values


def _tax_is_included(
    total: Decimal, taxes: Sequence[Decimal], currency: str, tolerance: Decimal
) -> bool:
    """Whether a tax line matches the tax contained in a tax-inclusive total.

    AUD: GST is one eleventh of the total. JPY: consumption tax is 10/110 of the
    total, or 8/108 on the reduced rate. USD sales tax is added on top, so it has
    no inclusive check; the subtotal-plus-tax check covers it.
    """
    if currency == "AUD":
        fractions: tuple[Decimal, ...] = (Decimal(1) / 11,)
    elif currency == "JPY":
        fractions = (Decimal(10) / 110, Decimal(8) / 108)
    else:
        return False
    exponent = Decimal(1).scaleb(-EXPONENTS[currency])
    expected = [
        (total * fraction).quantize(exponent, rounding=ROUND_HALF_UP) for fraction in fractions
    ]
    return any(abs(tax - target) <= tolerance for tax in taxes for target in expected)


def _extract_date(
    lines: Sequence[Line], currency: str | None, header_date: str | None
) -> tuple[DateResult | None, list[Reason]]:
    day_first = True if currency in ("AUD", "JPY") else None
    hits = find_dates(lines, day_first=day_first)
    resolved = [hit for hit in hits if hit.iso is not None]
    ambiguous = [hit for hit in hits if hit.iso is None]

    if not resolved:
        if header_date is not None:
            return DateResult(header_date, Confidence.MEDIUM, True, "email Date header"), []
        return None, [Reason.DATE_AMBIGUOUS if ambiguous else Reason.NO_DATE]

    first = resolved[0]
    assert first.iso is not None
    reasons: list[Reason] = []
    distinct = {hit.iso for hit in resolved}
    if len(distinct) > 1:
        reasons.append(Reason.MULTIPLE_DATES)
    if ambiguous:
        reasons.append(Reason.DATE_AMBIGUOUS)
    confidence = Confidence.MEDIUM if reasons else Confidence.HIGH
    return DateResult(first.iso, confidence, False, _date_snippet(lines, first)), reasons


def _date_snippet(lines: Sequence[Line], hit: DateHit) -> str:
    return _snippet(lines[hit.line_index].text)


def _snippet(text: str) -> str:
    stripped = " ".join(text.split())
    return stripped if len(stripped) <= _EVIDENCE_LIMIT else stripped[: _EVIDENCE_LIMIT - 1] + "…"


@dataclass(frozen=True, slots=True)
class Reconciled:
    """The outcome for one field after comparing two text sources."""

    write: bool
    value: str | None
    currency: str | None
    reasons: tuple[Reason, ...]


def reconcile_amount(own: Extraction, content: Extraction) -> Reconciled:
    """Write an amount only when both text sources give the same value and currency."""
    a, b = own.amount, content.amount
    if a is None or b is None:
        present = a or b
        return Reconciled(
            False,
            canonical_amount(present.value, present.currency) if present else None,
            present.currency if present else None,
            (Reason.SOURCE_MISSING, *own.amount_reasons, *content.amount_reasons),
        )
    if a.value != b.value or a.currency != b.currency:
        return Reconciled(False, None, None, (Reason.SOURCE_DISAGREEMENT, *own.amount_reasons))
    hard = {
        reason
        for reason in (*own.amount_reasons, *content.amount_reasons)
        if reason in BLOCKING and reason is not Reason.NO_CORROBORATION
    }
    corroborated = a.confidence is Confidence.HIGH or b.confidence is Confidence.HIGH
    reasons: tuple[Reason, ...] = tuple(sorted(hard, key=str))
    if not corroborated and not hard:
        reasons = (Reason.NO_CORROBORATION,)
    return Reconciled(not reasons, canonical_amount(a.value, a.currency), a.currency, reasons)


def reconcile_date(own: Extraction, content: Extraction) -> Reconciled:
    """Write a date only when both text sources give the same date."""
    a, b = own.date, content.date
    if a is None or b is None:
        present = a or b
        return Reconciled(
            False,
            present.value if present else None,
            None,
            (Reason.SOURCE_MISSING, *own.date_reasons, *content.date_reasons),
        )
    if a.value != b.value:
        return Reconciled(False, None, None, (Reason.SOURCE_DISAGREEMENT,))
    hard = {reason for reason in (*own.date_reasons, *content.date_reasons) if reason in BLOCKING}
    corroborated = a.confidence is Confidence.HIGH or b.confidence is Confidence.HIGH
    reasons: tuple[Reason, ...] = tuple(sorted(hard, key=str))
    if not corroborated and not hard:
        reasons = (Reason.NO_CORROBORATION,)
    return Reconciled(not reasons, a.value, None, reasons)


def to_jsonable(extraction: Extraction) -> dict[str, Any]:
    """A JSON-safe view of *extraction*. Amounts are strings, never floats."""
    amount = extraction.amount
    date = extraction.date
    return {
        "amount": None
        if amount is None
        else {
            "value": canonical_amount(amount.value, amount.currency),
            "currency": amount.currency,
            "confidence": amount.confidence.value,
            "corroboration": [item.value for item in amount.corroboration],
            "evidence": amount.evidence,
        },
        "amount_reasons": [reason.value for reason in extraction.amount_reasons],
        "date": None
        if date is None
        else {
            "value": date.value,
            "confidence": date.confidence.value,
            "from_header": date.from_header,
            "evidence": date.evidence,
        },
        "date_reasons": [reason.value for reason in extraction.date_reasons],
    }


__all__ = [
    "AmountResult",
    "DateResult",
    "Extraction",
    "Reconciled",
    "extract",
    "reconcile_amount",
    "reconcile_date",
    "to_jsonable",
]
