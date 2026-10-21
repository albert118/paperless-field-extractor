"""The real tool chain: Tesseract and poppler on generated receipts.

These run in CI and in the container image, where the tools are installed, and are
skipped on machines without them. They are what proves the fingerprint, the
router and the extractor work together; the unit tests use a fake tool chain.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from paperless_field_extractor.extract import extract
from paperless_field_extractor.ocr.cache import OcrCache
from paperless_field_extractor.ocr.config import OcrConfig
from paperless_field_extractor.ocr.router import OcrRouter
from paperless_field_extractor.ocr.types import InputKind, TextSource


def _tesseract_languages() -> set[str]:
    if shutil.which("tesseract") is None:
        return set()
    result = subprocess.run(
        ["tesseract", "--list-langs"], capture_output=True, text=True, check=False
    )
    return set((result.stdout + result.stderr).split())


_HAVE_OCR = {"eng", "jpn"} <= _tesseract_languages()
_HAVE_POPPLER = shutil.which("pdftotext") is not None and shutil.which("pdftoppm") is not None


def test_the_ocr_tools_are_installed_where_they_are_required() -> None:
    """CI sets REQUIRE_OCR_TOOLS so a missing tool fails the build instead of skipping tests."""
    if os.environ.get("REQUIRE_OCR_TOOLS") != "1":
        pytest.skip("REQUIRE_OCR_TOOLS is not set")
    assert _HAVE_OCR, "tesseract with eng and jpn is required"
    assert _HAVE_POPPLER, "pdftotext and pdftoppm are required"


needs_ocr = pytest.mark.skipif(not _HAVE_OCR, reason="tesseract with eng and jpn is not installed")
needs_poppler = pytest.mark.skipif(
    not (_HAVE_OCR and _HAVE_POPPLER), reason="tesseract and poppler are not installed"
)

RECEIPT = [
    "ACME PLUMBING PTY LTD",
    "ABN 12 345 678 901",
    "TAX INVOICE",
    "Date 25/10/2026",
    "GST $4.55",
    "Total $50.00",
]


def _receipt_image(lines: list[str]) -> Image.Image:
    font = ImageFont.load_default(size=44)
    image = Image.new("RGB", (1400, 120 + 90 * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    for index, text in enumerate(lines):
        draw.text((60, 60 + 90 * index), text, fill="black", font=font)
    return image


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _text_layer_pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF whose text is a real text layer (Helvetica)."""
    commands = ["BT /F1 18 Tf 50 740 Td 24 TL"]
    commands += [f"({line.replace('(', '[').replace(')', ']')}) Tj T*" for line in lines]
    commands.append("ET")
    stream = "\n".join(commands).encode("latin-1")
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


@needs_ocr
def test_a_photographed_receipt_is_read_and_the_total_extracted() -> None:
    router = OcrRouter()

    text = router.read(_png(_receipt_image(RECEIPT)), mime_type="image/png", file_name="r.png")
    result = extract(text.lines)

    assert text.sources == (TextSource.OCR,)
    assert result.amount is not None
    assert (result.amount.value, result.amount.currency) == (Decimal("50.00"), "AUD")
    assert result.date is not None and result.date.value == "2026-10-25"


@needs_ocr
def test_ocr_is_byte_for_byte_repeatable_with_the_pinned_configuration(tmp_path: Path) -> None:
    data = _png(_receipt_image(RECEIPT))

    first = OcrRouter().read(data, mime_type="image/png", file_name="r.png")
    second = OcrRouter().read(data, mime_type="image/png", file_name="r.png")

    assert first == second
    assert OcrRouter().fingerprint.pinned


@needs_ocr
def test_the_cache_returns_the_same_text_without_running_ocr_again(tmp_path: Path) -> None:
    data = _png(_receipt_image(RECEIPT))
    cache = OcrCache(tmp_path)

    first = OcrRouter(cache=cache).read(data, mime_type="image/png", file_name="r.png")
    second = OcrRouter(cache=cache).read(data, mime_type="image/png", file_name="r.png")

    assert first == second
    assert len(list(tmp_path.glob("*.json"))) == 1


@needs_ocr
def test_a_usd_receipt_with_an_explicit_code_is_read() -> None:
    lines = [
        "Hotel Example",
        "Oct 25, 2026",
        "Subtotal USD 90.00",
        "Tax USD 9.00",
        "Total USD 99.00",
    ]

    text = OcrRouter().read(_png(_receipt_image(lines)), mime_type="image/png", file_name=None)
    result = extract(text.lines)

    assert result.amount is not None
    assert (result.amount.value, result.amount.currency) == (Decimal("99.00"), "USD")
    assert result.date is not None and result.date.value == "2026-10-25"


@needs_ocr
def test_a_yen_amount_with_the_jpy_code_is_read_without_decimals() -> None:
    lines = [
        "Convenience Store Tokyo",
        "2026/10/25",
        "Subtotal JPY 1,000",
        "Tax JPY 100",
        "Total JPY 1,100",
    ]

    text = OcrRouter().read(_png(_receipt_image(lines)), mime_type="image/png", file_name=None)
    result = extract(text.lines)

    assert result.amount is not None
    assert (result.amount.value, result.amount.currency) == (Decimal("1100"), "JPY")


@needs_poppler
def test_a_pdf_with_a_text_layer_is_read_without_ocr() -> None:
    router = OcrRouter()
    pdf = _text_layer_pdf(RECEIPT)

    text = router.read(pdf, mime_type="application/pdf", file_name="r.pdf")
    result = extract(text.lines)

    assert text.kind is InputKind.PDF
    assert text.sources == (TextSource.TEXT_LAYER,)
    assert router.pdf_has_text_layer(pdf)
    assert result.amount is not None and result.amount.value == Decimal("50.00")


@needs_poppler
def test_a_scanned_pdf_is_rasterised_and_ocred() -> None:
    buffer = io.BytesIO()
    _receipt_image(RECEIPT).save(buffer, format="PDF", resolution=150)
    router = OcrRouter(config=OcrConfig())

    text = router.read(buffer.getvalue(), mime_type="application/pdf", file_name="scan.pdf")
    result = extract(text.lines)

    assert not router.pdf_has_text_layer(buffer.getvalue())
    assert text.sources == (TextSource.OCR,)
    assert result.amount is not None and result.amount.value == Decimal("50.00")
