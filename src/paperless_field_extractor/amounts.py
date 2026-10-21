"""Money tokens: finding them in a line and reading them as ``Decimal``.

The grammar is strict and per currency. AUD and USD amounts have two decimal
places; JPY has none. A token that does not fit the grammar is *ambiguous*, never
guessed: ``1.234`` could be a thousands group or a decimal, ``1234,56`` a decimal
comma, and a wrong guess becomes a silent wrong value in a tax field.

No ``float`` appears anywhere in an amount path.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

# Decimal places per supported currency. Others are detected but not read.
EXPONENTS: dict[str, int] = {"AUD": 2, "USD": 2, "JPY": 0}
DEFAULT_EXPONENT = 2

MARKER_CODES: dict[str, str] = {
    "AU$": "AUD",
    "$AU": "AUD",
    "AUD": "AUD",
    "US$": "USD",
    "USD": "USD",
    "¥": "JPY",
    "円": "JPY",
    "JPY": "JPY",
    "NZ$": "NZD",
    "NZD": "NZD",
    "CA$": "CAD",
    "CAD": "CAD",
    "€": "EUR",
    "EUR": "EUR",
    "£": "GBP",
    "GBP": "GBP",
    "CHF": "CHF",
    "CNY": "CNY",
}

_CODES = "AUD|USD|JPY|NZD|CAD|EUR|GBP|CHF|CNY"
_PREFIX_RE = re.compile(rf"(?:AU\$|US\$|NZ\$|CA\$|\$AU|[€£¥$]|(?<![A-Za-z])(?:{_CODES}))\s*$")
_SUFFIX_RE = re.compile(rf"^\s*(?:円|(?:{_CODES})(?![A-Za-z])|[€£¥$])")

# A loose money-shaped token: it starts and ends with a digit and may contain
# separators. ASCII lookarounds, because CJK characters count as word characters.
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.,])\d(?:[\d.,]*\d)?(?![A-Za-z0-9_])")
_STRICT_RE = re.compile(r"^(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d{2}))?$")
_PERCENT_RE = re.compile(r"^\s*%")


@dataclass(frozen=True, slots=True)
class Adjacent:
    """The currency marker touching a token, if any."""

    code: str | None = None
    symbol: bool = False


@dataclass(frozen=True, slots=True)
class Token:
    text: str
    start: int
    end: int
    adjacent: Adjacent
    negative: bool


class TokenKind(StrEnum):
    VALID = "valid"
    AMBIGUOUS = "ambiguous"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class TokenValue:
    kind: TokenKind
    value: Decimal | None = None


def canonical_amount(value: Decimal, currency: str | None) -> str:
    """*value* as a string with the currency's decimal places: ``50`` becomes ``50.00``.

    One format is used for reports, reconciliation and golden-set scoring, so two
    equal amounts always compare equal as strings.
    """
    if currency in EXPONENTS:
        return str(value.quantize(Decimal(1).scaleb(-EXPONENTS[currency])))
    return str(value)


def adjacent_marker(line: str, start: int, end: int) -> Adjacent:
    """The currency marker immediately before or after ``line[start:end]``."""
    before = _PREFIX_RE.search(line[:start])
    if before is not None:
        marker = before.group(0).strip()
        return Adjacent(code=MARKER_CODES.get(marker), symbol=True)
    after = _SUFFIX_RE.match(line[end:])
    if after is not None:
        marker = after.group(0).strip()
        return Adjacent(code=MARKER_CODES.get(marker), symbol=True)
    return Adjacent()


def iter_tokens(line: str, *, offset: int = 0) -> Iterator[Token]:
    """Every money-shaped token in *line* from *offset*, skipping percentages."""
    for match in _TOKEN_RE.finditer(line, offset):
        start, end = match.span()
        if _PERCENT_RE.match(line[end:]):
            continue
        yield Token(
            text=match.group(0),
            start=start,
            end=end,
            adjacent=adjacent_marker(line, start, end),
            negative=_is_negative(line, start, end),
        )


def classify(token: Token, currency: str | None) -> TokenValue:
    """Read *token* under *currency*'s grammar.

    A bare integer with no currency marker and no thousands separator is skipped
    (a quantity, a percentage, a day of the month), not treated as money.
    """
    exponent = EXPONENTS.get(currency, DEFAULT_EXPONENT) if currency else DEFAULT_EXPONENT
    match = _STRICT_RE.match(token.text)
    if match is None:
        return TokenValue(TokenKind.AMBIGUOUS)
    decimals = match.group("dec")
    if exponent == 0 and decimals is not None:
        return TokenValue(TokenKind.AMBIGUOUS)
    has_separator = "," in token.text
    if decimals is None and not (token.adjacent.symbol or has_separator):
        return TokenValue(TokenKind.SKIP)
    text = match.group("int").replace(",", "")
    if decimals is not None:
        text = f"{text}.{decimals}"
    value = Decimal(text)
    if value == 0:
        return TokenValue(TokenKind.SKIP)
    return TokenValue(TokenKind.VALID, value)


def _is_negative(line: str, start: int, end: int) -> bool:
    before = line[:start]
    marker = _PREFIX_RE.search(before)
    if marker is not None:
        before = before[: marker.start()]  # the sign may sit before the currency symbol
    before = before.rstrip()
    if before.endswith("-") and (len(before) == 1 or not before[-2].isalnum()):
        return True
    return before.endswith("(") and line[end:].lstrip().startswith(")")
