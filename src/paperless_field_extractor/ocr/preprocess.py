"""Fixed, deterministic preprocessing for photographed receipts."""

from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError

from paperless_field_extractor.errors import OcrToolError


def prepare_image(data: bytes, *, long_side: int) -> bytes:
    """EXIF-rotate, convert to grayscale and scale so the long side is *long_side*.

    The output is PNG. The same input and Pillow version always give the same
    bytes, which is what lets the OCR cache key on the original alone.
    """
    try:
        with Image.open(io.BytesIO(data)) as source:
            rotated = ImageOps.exif_transpose(source)
            gray = rotated.convert("L")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise OcrToolError(f"cannot decode the image: {exc}") from exc

    width, height = gray.size
    factor = long_side / max(width, height)
    resized = gray.resize(
        (max(1, round(width * factor)), max(1, round(height * factor))),
        Image.Resampling.LANCZOS,
    )
    buffer = io.BytesIO()
    resized.save(buffer, format="PNG", compress_level=6)
    return buffer.getvalue()
