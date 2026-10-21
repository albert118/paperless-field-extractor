"""Routing a receipt original to the right text source.

Routing is by input type, never by confidence:

=================  ==============================================================
``.eml``           stdlib ``email``: headers, text or HTML body, attachments recurse
PDF, text layer    ``pdftotext -layout``
PDF, no text       rasterise at a fixed DPI, then Tesseract
photo              EXIF rotate, grayscale, fixed scale, then Tesseract
=================  ==============================================================

A PDF has a text layer when ``pdftotext`` returns enough non-space characters. A
missing tool raises ``OcrToolError`` and is never turned into empty text.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from paperless_field_extractor.lines import Line, lines_from_text
from paperless_field_extractor.ocr.cache import OcrCache
from paperless_field_extractor.ocr.config import OMP_THREAD_LIMIT, OcrConfig
from paperless_field_extractor.ocr.eml import parse_eml
from paperless_field_extractor.ocr.fingerprint import OcrFingerprint, collect_fingerprint
from paperless_field_extractor.ocr.preprocess import prepare_image
from paperless_field_extractor.ocr.runner import (
    CommandResult,
    CommandRunner,
    require_success,
    run_command,
)
from paperless_field_extractor.ocr.tsv import parse_tsv
from paperless_field_extractor.ocr.types import InputKind, OcrText, TextSource

_PAGE_NUMBER_RE = re.compile(r"-(\d+)\.png$")


def classify_input(mime_type: str | None, file_name: str | None, data: bytes) -> InputKind:
    """What kind of original this is, from its MIME type, file name and magic bytes."""
    mime = (mime_type or "").lower()
    name = (file_name or "").lower()
    if mime == "message/rfc822" or name.endswith(".eml"):
        return InputKind.EML
    if mime == "application/pdf" or name.endswith(".pdf") or data[:5] == b"%PDF-":
        return InputKind.PDF
    if mime.startswith("image/") or name.endswith(
        (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".heic")
    ):
        return InputKind.IMAGE
    return InputKind.UNSUPPORTED


class OcrRouter:
    """Reads text from receipt originals, caching by original and configuration."""

    def __init__(
        self,
        *,
        config: OcrConfig | None = None,
        runner: CommandRunner = run_command,
        cache: OcrCache | None = None,
    ) -> None:
        self._config = config or OcrConfig()
        self._runner = runner
        self._cache = cache
        self._fingerprint: OcrFingerprint | None = None

    @property
    def fingerprint(self) -> OcrFingerprint:
        """The fingerprint of this configuration. Raises if Tesseract is missing."""
        if self._fingerprint is None:
            self._fingerprint = collect_fingerprint(self._runner, self._config)
        return self._fingerprint

    def read(self, data: bytes, *, mime_type: str | None, file_name: str | None) -> OcrText:
        """Text from *data*, from the cache when this exact original and config were read."""
        kind = classify_input(mime_type, file_name, data)
        if kind is InputKind.UNSUPPORTED:
            return OcrText(kind, (), ())
        key = None
        if self._cache is not None:
            key = self._cache.key(data, self.fingerprint.digest())
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        text = self._read_uncached(kind, data)
        if self._cache is not None and key is not None:
            self._cache.put(key, text)
        return text

    def _read_uncached(self, kind: InputKind, data: bytes) -> OcrText:
        if kind is InputKind.EML:
            return self._read_eml(data)
        if kind is InputKind.PDF:
            sources, lines = self._read_pdf(data)
            return OcrText(kind, sources, lines)
        return OcrText(kind, (TextSource.OCR,), self._read_image(data))

    def _read_eml(self, data: bytes) -> OcrText:
        content = parse_eml(data)
        lines: list[Line] = list(content.lines)
        sources: list[TextSource] = [TextSource.EMAIL]
        read = 0
        for attachment in content.attachments:
            if read >= self._config.max_attachments:
                break
            if len(attachment.data) < self._config.min_attachment_bytes:
                continue
            kind = classify_input(attachment.mime_type, attachment.file_name, attachment.data)
            if kind is InputKind.PDF:
                attachment_sources, attachment_lines = self._read_pdf(attachment.data)
            elif kind is InputKind.IMAGE:
                attachment_sources = (TextSource.OCR,)
                attachment_lines = self._read_image(attachment.data)
            else:
                continue
            read += 1
            sources.extend(attachment_sources)
            lines.extend(attachment_lines)
        return OcrText(
            InputKind.EML, tuple(dict.fromkeys(sources)), tuple(lines), content.header_date
        )

    def pdf_has_text_layer(self, data: bytes) -> bool:
        """Whether a PDF carries a usable text layer. Needs ``pdftotext``, not Tesseract."""
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "input.pdf"
            pdf.write_bytes(data)
            return self._text_layer(pdf) is not None

    def render_pdf_pages(self, data: bytes) -> tuple[bytes, ...]:
        """Rasterise up to ``max_pdf_pages`` pages to PNG bytes at the configured DPI."""
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "input.pdf"
            pdf.write_bytes(data)
            return tuple(page.read_bytes() for page in self._rasterise(pdf))

    def _read_pdf(self, data: bytes) -> tuple[tuple[TextSource, ...], tuple[Line, ...]]:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "input.pdf"
            pdf.write_bytes(data)
            layer = self._text_layer(pdf)
            if layer is not None:
                return (TextSource.TEXT_LAYER,), lines_from_text(layer)
            lines: list[Line] = []
            for page in self._rasterise(pdf):
                lines.extend(self._tesseract(page))
            return (TextSource.OCR,), tuple(lines)

    def _text_layer(self, pdf: Path) -> str | None:
        """The text layer, or ``None`` when the PDF has too little text to be one."""
        layer = require_success(
            self._run(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), "-"]), "pdftotext"
        ).decode("utf-8", errors="replace")
        characters = sum(1 for char in layer if not char.isspace())
        return layer if characters >= self._config.min_text_layer_chars else None

    def _rasterise(self, pdf: Path) -> list[Path]:
        prefix = pdf.parent / "page"
        require_success(
            self._run(
                [
                    "pdftoppm",
                    "-r",
                    str(self._config.dpi),
                    "-png",
                    "-f",
                    "1",
                    "-l",
                    str(self._config.max_pdf_pages),
                    str(pdf),
                    str(prefix),
                ]
            ),
            "pdftoppm",
        )
        return sorted(pdf.parent.glob("page-*.png"), key=_page_number)

    def _read_image(self, data: bytes) -> tuple[Line, ...]:
        prepared = prepare_image(data, long_side=self._config.long_side)
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "receipt.png"
            image.write_bytes(prepared)
            return self._tesseract(image)

    def _tesseract(self, image: Path) -> tuple[Line, ...]:
        output = require_success(
            self._run(
                [
                    "tesseract",
                    str(image),
                    "stdout",
                    "-l",
                    self._config.lang,
                    "--psm",
                    str(self._config.psm),
                    "--dpi",
                    str(self._config.dpi),
                    "tsv",
                ]
            ),
            "tesseract",
        )
        return parse_tsv(output.decode("utf-8", errors="replace"))

    def _run(self, args: list[str]) -> CommandResult:
        return self._runner(
            args,
            timeout=self._config.timeout_seconds,
            env={"OMP_THREAD_LIMIT": OMP_THREAD_LIMIT},
        )


def _page_number(path: Path) -> int:
    match = _PAGE_NUMBER_RE.search(path.name)
    return int(match.group(1)) if match else 0
