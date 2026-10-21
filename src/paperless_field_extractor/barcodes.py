"""Barcode scanning for Stage 0: do receipt barcodes carry an amount, date or supplier?

Retail receipt barcodes usually encode a receipt or store ID, not totals or dates.
That is unverified on these receipts, so the scan measures it. Only *summaries* of
a payload (length, character class, whether it looks like an amount or a date)
leave this module for reports. Raw payloads stay in the git-ignored raw directory.
"""

from __future__ import annotations

import datetime
import io
import re
from dataclasses import dataclass

import zxingcpp
from PIL import Image, UnidentifiedImageError

from paperless_field_extractor.dates import find_dates
from paperless_field_extractor.errors import OcrToolError
from paperless_field_extractor.lines import Line

_AMOUNT_LIKE_RE = re.compile(r"(?<!\d)\d{1,6}\.\d{2}(?!\d)")
# yyyymmdd, optionally followed by hhmm or hhmmss.
_COMPACT_DATE_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?:\d{4}|\d{6})?(?!\d)")
_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class BarcodeHit:
    format: str
    text: str


@dataclass(frozen=True, slots=True)
class PayloadSummary:
    """What a barcode payload looks like, without the payload itself."""

    format: str
    length: int
    charset: str
    is_url: bool
    looks_like_amount: bool
    looks_like_date: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "format": self.format,
            "length": self.length,
            "charset": self.charset,
            "is_url": self.is_url,
            "looks_like_amount": self.looks_like_amount,
            "looks_like_date": self.looks_like_date,
        }


def scan_image(data: bytes) -> tuple[BarcodeHit, ...]:
    """Every barcode zxing-cpp finds in an image."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            results = zxingcpp.read_barcodes(image)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise OcrToolError(f"cannot decode the image for barcode scanning: {exc}") from exc
    return tuple(BarcodeHit(result.format.name, result.text) for result in results)


def summarise(hit: BarcodeHit) -> PayloadSummary:
    """Describe a payload without keeping it."""
    text = hit.text
    if text.isdigit():
        charset = "numeric"
    elif text.isalnum():
        charset = "alphanumeric"
    else:
        charset = "other"
    return PayloadSummary(
        format=hit.format,
        length=len(text),
        charset=charset,
        is_url=_URL_RE.match(text) is not None,
        looks_like_amount=_AMOUNT_LIKE_RE.search(text) is not None,
        looks_like_date=_looks_like_date(text),
    )


def _looks_like_date(text: str) -> bool:
    if any(
        hit.iso is not None or hit.alternatives for hit in find_dates([Line(text)], day_first=None)
    ):
        return True
    for match in _COMPACT_DATE_RE.finditer(text):
        try:
            datetime.date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            continue
        return True
    return False
