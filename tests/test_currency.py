"""Currency resolution: AUD, USD and JPY from evidence, never from a labelled total alone."""

from __future__ import annotations

import pytest

from paperless_field_extractor.amounts import Adjacent
from paperless_field_extractor.currency import CurrencyDecision, resolve, scan_currency
from paperless_field_extractor.lines import lines_from_text
from paperless_field_extractor.reasons import Reason


def _decide(text: str, adjacent: Adjacent | None = None) -> CurrencyDecision:
    return resolve(scan_currency(lines_from_text(text)), adjacent or Adjacent(symbol=True))


@pytest.mark.parametrize(
    ("text", "adjacent", "code"),
    [
        ("Total AUD 10.00", Adjacent(), "AUD"),
        ("Total USD 10.00", Adjacent(), "USD"),
        ("Total JPY 1000", Adjacent(), "JPY"),
        ("合計 1,100円", Adjacent(code="JPY", symbol=True), "JPY"),
        ("合計 ¥1,100", Adjacent(code="JPY", symbol=True), "JPY"),
        ("Total US$10.00", Adjacent(code="USD", symbol=True), "USD"),
        ("ABN 12 345 678 901\nTotal $10.00", Adjacent(symbol=True), "AUD"),
        ("GST $1.00\nTotal $11.00", Adjacent(symbol=True), "AUD"),
        ("Sales Tax $0.80\nTotal $10.80", Adjacent(symbol=True), "USD"),
        ("Total $10.80\nUnited States", Adjacent(symbol=True), "USD"),
    ],
)
def test_currency_is_resolved_from_evidence(text: str, adjacent: Adjacent, code: str) -> None:
    decision = _decide(text, adjacent)
    assert decision.code == code
    assert decision.reason is None


def test_a_labelled_bare_dollar_total_is_not_evidence_of_aud() -> None:
    decision = _decide("Corner Cafe\nTotal $10.00")
    assert decision.code is None
    assert decision.reason is Reason.CURRENCY_AMBIGUOUS


def test_conflicting_australian_and_us_context_stays_ambiguous() -> None:
    decision = _decide("ABN 12 345 678 901\nSales Tax $1.00\nTotal $11.00")
    assert decision.reason is Reason.CURRENCY_AMBIGUOUS


def test_two_explicit_currencies_conflict_unless_the_amount_names_its_own() -> None:
    text = "Total USD 10.00 (AUD 15.20)"
    assert _decide(text, Adjacent()).reason is Reason.CURRENCY_CONFLICT
    assert _decide(text, Adjacent(code="AUD", symbol=True)).code == "AUD"


def test_a_document_with_no_currency_marker_at_all_is_unknown() -> None:
    assert _decide("Total 10.00", Adjacent()).reason is Reason.CURRENCY_UNKNOWN


@pytest.mark.parametrize(("text", "code"), [("Total €10.00", "EUR"), ("Total NZ$10.00", "NZD")])
def test_other_currencies_are_detected_and_flagged_unsupported(text: str, code: str) -> None:
    adjacent = Adjacent(code=code, symbol=True)
    decision = _decide(text, adjacent)
    assert (decision.code, decision.reason) == (code, Reason.UNSUPPORTED_CURRENCY)


@pytest.mark.parametrize("text", ["Total 10.00 AUDIT", "cad eur chf", "Caudal USDA"])
def test_currency_codes_inside_words_or_in_lower_case_are_not_markers(text: str) -> None:
    assert scan_currency(lines_from_text(text)).codes == frozenset()
