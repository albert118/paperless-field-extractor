"""Text lines: the unit every text source is reduced to before extraction."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Line:
    """One line of text and, for OCR, the lowest word confidence on it (0 to 100)."""

    text: str
    min_conf: float | None = None


def normalise(text: str) -> str:
    """NFKC-normalise *text* so full-width digits and yen signs match ASCII rules.

    Japanese receipts commonly print ``１，２３４円`` or ``￥1,234``; NFKC maps
    those to ``1,234`` and ``¥``. Non-breaking spaces become plain spaces.
    """
    return unicodedata.normalize("NFKC", text).replace(" ", " ").rstrip()


def lines_from_text(text: str) -> tuple[Line, ...]:
    """Split plain text into normalised lines with no confidence information."""
    return tuple(Line(normalise(raw)) for raw in text.splitlines())
