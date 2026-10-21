"""The per-currency money grammar: strict, never guessing."""

from __future__ import annotations

from decimal import Decimal

import pytest

from paperless_field_extractor.amounts import (
    Adjacent,
    Token,
    TokenKind,
    adjacent_marker,
    canonical_amount,
    classify,
    iter_tokens,
)


def _token(text: str, *, symbol: bool = False) -> Token:
    return Token(text, 0, len(text), Adjacent(symbol=symbol), negative=False)


@pytest.mark.parametrize(
    ("text", "symbol", "currency", "expected"),
    [
        ("12.34", False, "AUD", Decimal("12.34")),
        ("1,234.56", False, "USD", Decimal("1234.56")),
        ("1234.56", False, "AUD", Decimal("1234.56")),
        ("50", True, "AUD", Decimal("50")),
        ("1,234", False, "AUD", Decimal("1234")),
        ("1,234", False, "JPY", Decimal("1234")),
        ("1234", True, "JPY", Decimal("1234")),
        ("12,345,678", False, "JPY", Decimal("12345678")),
    ],
)
def test_valid_tokens_read_as_exact_decimals(
    text: str, symbol: bool, currency: str, expected: Decimal
) -> None:
    result = classify(_token(text, symbol=symbol), currency)
    assert result.kind is TokenKind.VALID
    assert result.value == expected


@pytest.mark.parametrize(
    ("text", "currency"),
    [
        ("1.234", "AUD"),
        ("1.234,56", "AUD"),
        ("1234,56", "USD"),
        ("12.3", "AUD"),
        ("12.345", "USD"),
        ("1,23", "AUD"),
        ("12.50", "JPY"),
        ("1,234.50", "JPY"),
    ],
)
def test_ambiguous_formats_are_reported_never_guessed(text: str, currency: str) -> None:
    result = classify(_token(text, symbol=True), currency)
    assert result.kind is TokenKind.AMBIGUOUS
    assert result.value is None


@pytest.mark.parametrize(
    ("text", "currency"),
    [("3", "AUD"), ("50", "AUD"), ("1234", "JPY"), ("0.00", "AUD"), ("0", "JPY")],
)
def test_quantities_and_zero_are_skipped_not_read_as_money(text: str, currency: str) -> None:
    assert classify(_token(text), currency).kind is TokenKind.SKIP


def test_an_unknown_currency_uses_the_two_decimal_grammar() -> None:
    assert classify(_token("12.34"), None).value == Decimal("12.34")
    assert classify(_token("12"), None).kind is TokenKind.SKIP


@pytest.mark.parametrize(
    ("line", "code", "symbol"),
    [
        ("Total $12.34", None, True),
        ("Total US$12.34", "USD", True),
        ("Total AU$12.34", "AUD", True),
        ("Total USD 12.34", "USD", True),
        ("Total 12.34 AUD", "AUD", True),
        ("合計 ¥1,100", "JPY", True),
        ("合計 1,100円", "JPY", True),
        ("Total €12.34", "EUR", True),
        ("Total 12.34", None, False),
    ],
)
def test_the_adjacent_currency_marker_is_found(line: str, code: str | None, symbol: bool) -> None:
    token = next(iter_tokens(line))
    assert token.adjacent == Adjacent(code=code, symbol=symbol)


def test_percentages_are_not_tokens() -> None:
    assert [token.text for token in iter_tokens("GST 10% $4.55")] == ["4.55"]


@pytest.mark.parametrize(
    ("line", "negative"),
    [
        ("Total -$30.00", True),
        ("Total $-30.00", True),
        ("Total ($30.00)", True),
        ("Total $30.00", False),
        ("Ref A-30.00", False),
    ],
)
def test_negative_amounts_are_detected(line: str, negative: bool) -> None:
    token = next(iter_tokens(line))
    assert token.negative is negative


def test_tokens_inside_longer_words_are_ignored() -> None:
    assert [token.text for token in iter_tokens("ABN12345.67 ref#A99.00 $5.00")] == ["5.00"]


def test_cjk_next_to_a_number_does_not_hide_it() -> None:
    assert [token.text for token in iter_tokens("合計1,100円")] == ["1,100"]


def test_adjacent_marker_ignores_markers_separated_by_text() -> None:
    assert adjacent_marker("$ total 5.00", 8, 12) == Adjacent()


@pytest.mark.parametrize(
    ("value", "currency", "expected"),
    [
        (Decimal("50"), "AUD", "50.00"),
        (Decimal("50.5"), "USD", "50.50"),
        (Decimal("1100"), "JPY", "1100"),
        (Decimal("7"), None, "7"),
    ],
)
def test_canonical_amount_uses_the_currency_decimal_places(
    value: Decimal, currency: str | None, expected: str
) -> None:
    assert canonical_amount(value, currency) == expected
