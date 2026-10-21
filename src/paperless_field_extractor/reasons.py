"""Enumerations shared by the extraction and reconciliation code."""

from __future__ import annotations

from enum import StrEnum


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Corroboration(StrEnum):
    """Independent evidence that an extracted total is right."""

    TOTAL_REPEATED = "total-repeated"
    TAX_INCLUDED = "tax-included"
    SUBTOTAL_PLUS_TAX = "subtotal-plus-tax"


class Reason(StrEnum):
    """Why a field was not accepted, or what a reviewer should look at."""

    NO_AMOUNT = "no-amount"
    AMBIGUOUS_AMOUNT = "ambiguous-amount"
    CONFLICTING_TOTALS = "conflicting-totals"
    NO_TOTAL_LABEL = "no-total-label"
    NO_CORROBORATION = "no-corroboration"
    CREDIT_NOTE = "credit-note"
    NEGATIVE_AMOUNT = "negative-amount"
    LOW_WORD_CONFIDENCE = "low-word-confidence"
    CURRENCY_AMBIGUOUS = "currency-ambiguous"
    CURRENCY_CONFLICT = "currency-conflict"
    CURRENCY_UNKNOWN = "currency-unknown"
    UNSUPPORTED_CURRENCY = "unsupported-currency"
    NO_DATE = "no-date"
    DATE_AMBIGUOUS = "date-ambiguous"
    MULTIPLE_DATES = "multiple-dates"
    SOURCE_MISSING = "source-missing"
    SOURCE_DISAGREEMENT = "source-disagreement"


# Reasons that stop a value being written. NO_CORROBORATION is handled
# separately, because agreement between two sources is itself corroboration.
BLOCKING: frozenset[Reason] = frozenset(
    {
        Reason.NO_AMOUNT,
        Reason.AMBIGUOUS_AMOUNT,
        Reason.CONFLICTING_TOTALS,
        Reason.NO_TOTAL_LABEL,
        Reason.NO_CORROBORATION,
        Reason.CREDIT_NOTE,
        Reason.NEGATIVE_AMOUNT,
        Reason.LOW_WORD_CONFIDENCE,
        Reason.CURRENCY_AMBIGUOUS,
        Reason.CURRENCY_CONFLICT,
        Reason.CURRENCY_UNKNOWN,
        Reason.UNSUPPORTED_CURRENCY,
        Reason.NO_DATE,
        Reason.DATE_AMBIGUOUS,
        Reason.SOURCE_MISSING,
        Reason.SOURCE_DISAGREEMENT,
    }
)
