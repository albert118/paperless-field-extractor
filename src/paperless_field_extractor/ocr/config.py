"""The OCR settings that, with the tool versions, make a run reproducible."""

from __future__ import annotations

from dataclasses import dataclass

# Bump when preprocessing changes in a way that can alter the image.
PREPROCESS_VERSION = "1"

# Env var that stops Tesseract's OpenMP threading from varying output.
OMP_THREAD_LIMIT = "1"


@dataclass(frozen=True, slots=True)
class OcrConfig:
    """Everything that decides OCR output besides the tool versions themselves."""

    # English and Japanese, because AUD, USD and JPY receipts are all in scope.
    # Whether adding jpn costs accuracy on English receipts is measured on the
    # golden set, not assumed.
    lang: str = "eng+jpn"
    psm: int = 4
    dpi: int = 300
    long_side: int = 3000
    timeout_seconds: float = 120.0
    max_pdf_pages: int = 10
    min_text_layer_chars: int = 50
    min_attachment_bytes: int = 2048
    max_attachments: int = 5
