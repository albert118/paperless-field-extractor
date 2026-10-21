"""Test doubles and builders for the OCR tool chain. No real binary is ever run here."""

from __future__ import annotations

import io
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import zxingcpp
from PIL import Image

from paperless_field_extractor.errors import OcrToolError
from paperless_field_extractor.ocr.runner import CommandResult

Handler = Callable[[Sequence[str]], CommandResult]

TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)


def ok(stdout: str | bytes = b"", stderr: str | bytes = b"") -> CommandResult:
    return CommandResult(
        0,
        stdout.encode() if isinstance(stdout, str) else stdout,
        stderr.encode() if isinstance(stderr, str) else stderr,
    )


def tsv(*rows: tuple[int, int, int, int, int, float, str]) -> str:
    """A Tesseract TSV with word rows ``(page, block, par, line, word, conf, text)``."""
    body = [
        f"5\t{page}\t{block}\t{par}\t{line}\t{word}\t0\t0\t10\t10\t{conf}\t{text}"
        for page, block, par, line, word, conf, text in rows
    ]
    return "\n".join([TSV_HEADER, *body]) + "\n"


class FakeRunner:
    """Dispatches commands by executable name and records every call."""

    def __init__(self, handlers: Mapping[str, Handler]) -> None:
        self._handlers = dict(handlers)
        self.calls: list[list[str]] = []
        self.envs: list[Mapping[str, str] | None] = []

    def __call__(
        self, args: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None
    ) -> CommandResult:
        self.calls.append(list(args))
        self.envs.append(env)
        handler = self._handlers.get(args[0])
        if handler is None:
            raise OcrToolError(f"{args[0]} is not installed or not on PATH")
        return handler(args)

    def count(self, executable: str, *, containing: str | None = None) -> int:
        return sum(
            1
            for call in self.calls
            if call[0] == executable and (containing is None or containing in call)
        )


def tesseract_handler(tessdata: Path, on_image: Handler) -> Handler:
    """Answers ``--version`` and ``--list-langs`` and delegates OCR calls to *on_image*."""

    def handle(args: Sequence[str]) -> CommandResult:
        if args[1] == "--version":
            return ok("tesseract 5.3.4\n leptonica-1.82.0\n")
        if args[1] == "--list-langs":
            return ok(f'List of available languages in "{tessdata}" (2):\neng\njpn\n')
        return on_image(args)

    return handle


def make_tessdata(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "eng.traineddata").write_bytes(b"eng-model")
    (directory / "jpn.traineddata").write_bytes(b"jpn-model")
    return directory


def png_bytes(width: int = 40, height: int = 20, colour: int = 255) -> bytes:
    buffer = io.BytesIO()
    Image.new("L", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def barcode_png(text: str, *, scale: int = 4) -> bytes:
    """A real Code 128 barcode as PNG bytes."""
    code = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.Code128)
    image = zxingcpp.write_barcode_to_image(code, scale=scale)
    height, width = image.shape[0], image.shape[1]
    pil = Image.frombuffer("L", (width, height), memoryview(image).tobytes(), "raw", "L", 0, 1)
    buffer = io.BytesIO()
    pil.save(buffer, format="PNG")
    return buffer.getvalue()
