"""Barcode scanning and payload summaries (real zxing-cpp, generated barcodes)."""

from __future__ import annotations

import pytest

from paperless_field_extractor.barcodes import BarcodeHit, scan_image, summarise
from paperless_field_extractor.errors import OcrToolError

from .fakes import barcode_png, png_bytes


def test_a_generated_barcode_is_read_back() -> None:
    hits = scan_image(barcode_png("RCPT123456"))

    assert [(hit.format, hit.text) for hit in hits] == [("Code128", "RCPT123456")]


def test_an_image_with_no_barcode_has_no_hits() -> None:
    assert scan_image(png_bytes(200, 100)) == ()


def test_undecodable_bytes_are_a_loud_error() -> None:
    with pytest.raises(OcrToolError, match="cannot decode"):
        scan_image(b"not an image")


@pytest.mark.parametrize(
    ("text", "charset", "url", "amount", "date"),
    [
        ("1234567890123", "numeric", False, False, False),
        ("RCPT123456", "alphanumeric", False, False, False),
        ("https://shop.example/r/9", "other", True, False, False),
        ("R-1234|TOTAL=50.00", "other", False, True, False),
        ("20261025123456", "numeric", False, False, True),
        ("REC 25/10/2026", "other", False, False, True),
    ],
)
def test_a_payload_is_summarised_by_what_it_looks_like(
    text: str, charset: str, url: bool, amount: bool, date: bool
) -> None:
    summary = summarise(BarcodeHit("Code128", text))

    assert (
        summary.charset,
        summary.is_url,
        summary.looks_like_amount,
        summary.looks_like_date,
    ) == (
        charset,
        url,
        amount,
        date,
    )
    assert summary.length == len(text)


def test_a_summary_never_contains_the_payload() -> None:
    summary = summarise(BarcodeHit("Code128", "SECRET-PAYLOAD-123"))

    assert "SECRET" not in str(summary.to_dict())
