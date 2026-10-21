"""Deciding which currency a receipt is in.

AUD, USD and JPY are the supported currencies. A bare ``$`` is the hard case: AUD
and USD receipts both print it. It resolves only on evidence (an explicit code, or
Australian or US context in the text). A labelled total is not evidence, because
most US receipts label their total too. Without evidence the currency is
ambiguous and the document goes to review.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from paperless_field_extractor.amounts import EXPONENTS, MARKER_CODES, Adjacent
from paperless_field_extractor.lines import Line
from paperless_field_extractor.reasons import Reason

_MARKER_RE = re.compile(
    r"AU\$|US\$|NZ\$|CA\$|\$AU|[€£¥円]"
    r"|(?<![A-Za-z])(?:AUD|USD|JPY|NZD|CAD|EUR|GBP|CHF|CNY)(?![A-Za-z])"
)
_AU_CONTEXT_RE = re.compile(r"\b(?:gst|abn|australia|australian|tax invoice)\b", re.IGNORECASE)
_US_CONTEXT_RE = re.compile(r"\b(?:sales tax|united states|u\.?s\.?a\.?)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CurrencyEvidence:
    """What the text of a document says about its currency."""

    codes: frozenset[str]
    bare_dollar: bool
    au_context: bool
    us_context: bool

    @property
    def single_code(self) -> str | None:
        """The one explicit currency in the text, or ``None`` if zero or several."""
        return next(iter(self.codes)) if len(self.codes) == 1 else None


@dataclass(frozen=True, slots=True)
class CurrencyDecision:
    code: str | None
    reason: Reason | None = None


def scan_currency(lines: Sequence[Line]) -> CurrencyEvidence:
    """Collect every explicit currency marker and the Australian or US context."""
    codes: set[str] = set()
    bare_dollar = False
    for line in lines:
        for match in _MARKER_RE.finditer(line.text):
            codes.add(MARKER_CODES[match.group(0)])
        if "$" in _MARKER_RE.sub(" ", line.text):
            bare_dollar = True
    text = "\n".join(line.text for line in lines)
    return CurrencyEvidence(
        codes=frozenset(codes),
        bare_dollar=bare_dollar,
        au_context=_AU_CONTEXT_RE.search(text) is not None,
        us_context=_US_CONTEXT_RE.search(text) is not None,
    )


def resolve(evidence: CurrencyEvidence, adjacent: Adjacent) -> CurrencyDecision:
    """The currency of the amount whose neighbouring marker is *adjacent*."""
    if adjacent.code is not None:
        return _supported(adjacent.code)
    if len(evidence.codes) > 1:
        return CurrencyDecision(None, Reason.CURRENCY_CONFLICT)
    if evidence.single_code is not None:
        return _supported(evidence.single_code)
    if evidence.bare_dollar or adjacent.symbol:
        if evidence.au_context and not evidence.us_context:
            return CurrencyDecision("AUD")
        if evidence.us_context and not evidence.au_context:
            return CurrencyDecision("USD")
        return CurrencyDecision(None, Reason.CURRENCY_AMBIGUOUS)
    return CurrencyDecision(None, Reason.CURRENCY_UNKNOWN)


def _supported(code: str) -> CurrencyDecision:
    if code in EXPONENTS:
        return CurrencyDecision(code)
    return CurrencyDecision(code, Reason.UNSUPPORTED_CURRENCY)
