"""Types shared by the OCR router, cache and probe."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from paperless_field_extractor.lines import Line


class InputKind(StrEnum):
    """What a receipt original is, decided from its type and never from its content quality."""

    EML = "eml"
    PDF = "pdf"
    IMAGE = "image"
    UNSUPPORTED = "unsupported"


class TextSource(StrEnum):
    """How a piece of text was obtained."""

    EMAIL = "email"
    TEXT_LAYER = "pdf-text-layer"
    OCR = "ocr"


@dataclass(frozen=True, slots=True)
class OcrText:
    """Text read from one original, with how it was read."""

    kind: InputKind
    sources: tuple[TextSource, ...]
    lines: tuple[Line, ...]
    header_date: str | None = None

    def plain(self) -> str:
        return "\n".join(line.text for line in self.lines)
