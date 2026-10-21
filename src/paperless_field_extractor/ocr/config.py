"""The OCR settings that, with the tool versions, make a run reproducible."""

from __future__ import annotations

import os
from dataclasses import dataclass

from paperless_field_extractor.errors import SettingsError

# Bump when preprocessing changes in a way that can alter the image.
PREPROCESS_VERSION = "1"

# Each Tesseract process stays single-threaded, because OpenMP threading can vary
# its output. Cores are used across processes instead (documents, pages and
# attachments run in parallel), which scales better and keeps output identical at
# any worker count.
OMP_THREAD_LIMIT = "1"

# Upper bound for the automatic worker count, so a very large host does not open
# dozens of simultaneous downloads against Paperless or load dozens of models.
MAX_AUTO_WORKERS = 32


def default_workers() -> int:
    """One worker per CPU this process may use, at least 1 and at most ``MAX_AUTO_WORKERS``.

    ``os.process_cpu_count`` honours CPU affinity. It does not see a container CPU
    quota (``docker run --cpus``), so set ``--workers`` explicitly there.
    """
    return max(1, min(os.process_cpu_count() or 1, MAX_AUTO_WORKERS))


@dataclass(frozen=True, slots=True)
class OcrConfig:
    """Everything that decides OCR output besides the tool versions themselves.

    ``workers`` changes how fast a run goes, never what it produces, so it is not
    part of the OCR fingerprint.
    """

    # English and Japanese, because AUD, USD and JPY receipts are all in scope.
    # Whether adding jpn costs accuracy on English receipts is measured on the
    # golden set, not assumed.
    lang: str = "eng+jpn"
    psm: int = 4
    dpi: int = 300
    long_side: int = 3000
    timeout_seconds: float = 60.0
    max_pdf_pages: int = 10
    min_text_layer_chars: int = 50
    min_attachment_bytes: int = 2048
    max_attachments: int = 5
    # 0 means automatic: one worker per usable CPU core.
    workers: int = 0

    def __post_init__(self) -> None:
        if self.workers < 0:
            raise SettingsError(f"workers must be 0 (automatic) or more, got {self.workers}")

    def effective_workers(self) -> int:
        """The number of tool processes that may run at once."""
        return self.workers if self.workers > 0 else default_workers()
