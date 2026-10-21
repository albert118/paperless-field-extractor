"""Routing by input type, with the external tools replaced by a recording fake."""

from __future__ import annotations

import io
from collections.abc import Sequence
from email.message import EmailMessage
from pathlib import Path

import pytest
from PIL import Image

from paperless_field_extractor.errors import OcrToolError
from paperless_field_extractor.ocr.cache import OcrCache
from paperless_field_extractor.ocr.config import OcrConfig
from paperless_field_extractor.ocr.router import OcrRouter, classify_input
from paperless_field_extractor.ocr.runner import CommandResult
from paperless_field_extractor.ocr.types import InputKind, TextSource

from .fakes import FakeRunner, make_tessdata, ok, png_bytes, tesseract_handler, tsv

RECEIPT_TSV = tsv(
    (1, 1, 1, 1, 1, 95, "Total"),
    (1, 1, 1, 1, 2, 88, "$50.00"),
)
TEXT_LAYER = (
    "ACME PLUMBING PTY LTD\nABN 12 345 678 901\nTAX INVOICE 25/10/2026\n"
    "GST   $4.55\nTotal   $50.00\n"
)


@pytest.mark.parametrize(
    ("mime", "name", "data", "kind"),
    [
        ("message/rfc822", None, b"", InputKind.EML),
        (None, "Receipt.EML", b"", InputKind.EML),
        ("application/pdf", None, b"", InputKind.PDF),
        (None, "scan.pdf", b"", InputKind.PDF),
        (None, None, b"%PDF-1.7", InputKind.PDF),
        ("image/jpeg", None, b"", InputKind.IMAGE),
        (None, "photo.HEIC", b"", InputKind.IMAGE),
        ("application/zip", "a.zip", b"PK", InputKind.UNSUPPORTED),
        (None, None, b"", InputKind.UNSUPPORTED),
    ],
)
def test_input_is_classified_by_type_never_by_content_quality(
    mime: str | None, name: str | None, data: bytes, kind: InputKind
) -> None:
    assert classify_input(mime, name, data) is kind


def _page_name(args: Sequence[str]) -> str:
    return Path(args[1]).name


class _Rig:
    """A fake tool chain: scripted pdftotext and pdftoppm output, recorded OCR input."""

    def __init__(self, tmp_path: Path, *, layer: str = TEXT_LAYER, pages: int = 1) -> None:
        self.layer = layer
        self.pages = pages
        self.ocr_inputs: list[bytes] = []
        self.runner = FakeRunner(
            {
                "tesseract": tesseract_handler(make_tessdata(tmp_path / "tessdata"), self._ocr),
                "pdftotext": self._pdftotext,
                "pdftoppm": self._pdftoppm,
            }
        )

    def _ocr(self, args: Sequence[str]) -> CommandResult:
        data = Path(args[1]).read_bytes()
        self.ocr_inputs.append(data)
        return ok(RECEIPT_TSV)

    def _pdftotext(self, args: Sequence[str]) -> CommandResult:
        if args[1] == "-v":
            return ok(stderr="pdftotext version 24.08.0\n")
        return ok(self.layer)

    def _pdftoppm(self, args: Sequence[str]) -> CommandResult:
        prefix = args[-1]
        digits = len(str(self.pages))
        for number in range(1, self.pages + 1):
            Path(f"{prefix}-{number:0{digits}d}.png").write_bytes(png_bytes(10 + number, 10))
        return ok()

    def router(self, tmp_path: Path, **options: object) -> OcrRouter:
        return OcrRouter(runner=self.runner, **options)  # type: ignore[arg-type]


def test_a_photo_is_preprocessed_then_read_by_tesseract_with_the_fixed_settings(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    jpeg = io.BytesIO()
    Image.new("RGB", (600, 300), (240, 240, 240)).save(jpeg, format="JPEG")

    text = rig.router(tmp_path).read(jpeg.getvalue(), mime_type="image/jpeg", file_name="r.jpg")

    assert text.kind is InputKind.IMAGE
    assert text.sources == (TextSource.OCR,)
    assert [(line.text, line.min_conf) for line in text.lines] == [("Total $50.00", 88.0)]
    call = next(c for c in rig.runner.calls if c[0] == "tesseract" and c[-1] == "tsv")
    assert call[call.index("-l") + 1] == "eng+jpn"
    assert call[call.index("--psm") + 1] == "4"
    assert call[call.index("--dpi") + 1] == "300"
    with Image.open(io.BytesIO(rig.ocr_inputs[0])) as sent:
        assert (sent.mode, max(sent.size)) == ("L", 3000)


def test_tesseract_runs_with_a_single_openmp_thread(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    rig.router(tmp_path).read(png_bytes(), mime_type="image/png", file_name="r.png")

    ocr_index = next(i for i, c in enumerate(rig.runner.calls) if c[-1] == "tsv")
    assert rig.runner.envs[ocr_index] == {"OMP_THREAD_LIMIT": "1"}


def test_a_pdf_with_a_text_layer_uses_pdftotext_and_never_runs_ocr(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    text = rig.router(tmp_path).read(b"%PDF-1.7", mime_type="application/pdf", file_name="r.pdf")

    assert text.sources == (TextSource.TEXT_LAYER,)
    assert [line.text for line in text.lines] == [
        "ACME PLUMBING PTY LTD",
        "ABN 12 345 678 901",
        "TAX INVOICE 25/10/2026",
        "GST   $4.55",
        "Total   $50.00",
    ]
    assert rig.runner.count("tesseract", containing="tsv") == 0
    assert rig.runner.count("pdftoppm") == 0
    pdftotext = next(c for c in rig.runner.calls if c[0] == "pdftotext" and "-layout" in c)
    assert "-layout" in pdftotext and "UTF-8" in pdftotext


def test_a_scanned_pdf_is_rasterised_then_ocred_page_by_page_in_numeric_order(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path, layer="  \n \n", pages=12)

    # One worker, so the order tools are called in is the order pages are read.
    text = rig.router(tmp_path, config=OcrConfig(workers=1)).read(
        b"%PDF-1.7", mime_type="application/pdf", file_name="r.pdf"
    )

    assert text.sources == (TextSource.OCR,)
    assert rig.runner.count("pdftoppm") == 1
    widths = [Image.open(io.BytesIO(data)).size[0] for data in rig.ocr_inputs]
    assert widths == [10 + n for n in range(1, 13)]
    pdftoppm = next(c for c in rig.runner.calls if c[0] == "pdftoppm")
    assert pdftoppm[pdftoppm.index("-r") + 1] == "300"
    assert pdftoppm[pdftoppm.index("-l") + 1] == "10"


def test_a_pdf_with_almost_no_text_counts_as_scanned(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, layer="p. 1\n")

    text = rig.router(tmp_path).read(b"%PDF-1.7", mime_type="application/pdf", file_name="r.pdf")

    assert text.sources == (TextSource.OCR,)


def test_pdf_text_layer_detection_does_not_need_tesseract(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    router = OcrRouter(runner=FakeRunner({"pdftotext": rig._pdftotext}))  # noqa: SLF001

    assert router.pdf_has_text_layer(b"%PDF-1.7")


def test_rendered_pdf_pages_are_returned_as_png_bytes(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, pages=3)

    pages = rig.router(tmp_path).render_pdf_pages(b"%PDF-1.7")

    assert [Image.open(io.BytesIO(page)).size[0] for page in pages] == [11, 12, 13]


def test_an_unsupported_original_runs_no_tools(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    text = rig.router(tmp_path).read(b"PK", mime_type="application/zip", file_name="a.zip")

    assert text.kind is InputKind.UNSUPPORTED
    assert text.lines == ()
    assert rig.runner.calls == []


@pytest.mark.parametrize("missing", ["tesseract", "pdftotext"])
def test_a_missing_tool_is_a_loud_error_never_empty_text(tmp_path: Path, missing: str) -> None:
    rig = _Rig(tmp_path)
    router = OcrRouter(
        runner=FakeRunner({k: v for k, v in rig.runner._handlers.items() if k != missing})  # noqa: SLF001
    )
    mime, data = (
        ("image/png", png_bytes()) if missing == "tesseract" else ("application/pdf", b"%PDF-")
    )

    with pytest.raises(OcrToolError, match=missing):
        router.read(data, mime_type=mime, file_name=None)


def test_a_failing_tool_reports_its_stderr(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            "tesseract": tesseract_handler(
                make_tessdata(tmp_path / "t"), lambda a: CommandResult(1, b"", b"Error: bad lang")
            )
        }
    )

    with pytest.raises(OcrToolError, match="bad lang"):
        OcrRouter(runner=runner).read(png_bytes(), mime_type="image/png", file_name=None)


def test_a_second_read_of_the_same_original_is_served_from_the_cache(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    router = OcrRouter(runner=rig.runner, cache=OcrCache(tmp_path / "cache"))
    data = png_bytes()

    first = router.read(data, mime_type="image/png", file_name="r.png")
    second = router.read(data, mime_type="image/png", file_name="r.png")

    assert first == second
    assert rig.runner.count("tesseract", containing="tsv") == 1


def test_a_different_configuration_does_not_reuse_the_cache(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    cache = OcrCache(tmp_path / "cache")
    data = png_bytes()

    OcrRouter(runner=rig.runner, cache=cache).read(data, mime_type="image/png", file_name=None)
    OcrRouter(runner=rig.runner, cache=cache, config=OcrConfig(psm=6)).read(
        data, mime_type="image/png", file_name=None
    )

    assert rig.runner.count("tesseract", containing="tsv") == 2


def _email_with(
    attachments: list[tuple[bytes, str, str, str]], body: str = "Total $50.00"
) -> bytes:
    message = EmailMessage()
    message["Date"] = "Sat, 25 Oct 2026 10:00:00 +1100"
    message["Subject"] = "Receipt"
    message.set_content(body)
    for data, maintype, subtype, name in attachments:
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return message.as_bytes()


def test_an_email_is_read_from_its_body_with_the_header_date_and_no_ocr(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    text = rig.router(tmp_path).read(_email_with([]), mime_type="message/rfc822", file_name=None)

    assert text.kind is InputKind.EML
    assert text.sources == (TextSource.EMAIL,)
    assert text.header_date == "2026-10-25"
    assert [line.text for line in text.lines] == ["Total $50.00"]
    assert rig.runner.calls == []


def test_email_attachments_are_read_and_tiny_ones_such_as_tracking_pixels_are_skipped(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    big_png = png_bytes(300, 300)
    assert len(big_png) < 2048  # a flat image compresses small; pad it past the threshold
    padded = big_png + b"\x00" * 3000
    data = _email_with(
        [(padded, "image", "png", "receipt.png"), (png_bytes(1, 1), "image", "png", "pixel.png")]
    )

    text = rig.router(tmp_path).read(data, mime_type="message/rfc822", file_name=None)

    assert text.sources == (TextSource.EMAIL, TextSource.OCR)
    assert rig.runner.count("tesseract", containing="tsv") == 1
    assert [line.text for line in text.lines] == ["Total $50.00", "Total $50.00"]


def test_an_email_pdf_attachment_with_a_text_layer_uses_pdftotext(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    data = _email_with([(b"%PDF-1.7" + b"\x00" * 3000, "application", "pdf", "r.pdf")], body="Hi")

    text = rig.router(tmp_path).read(data, mime_type="message/rfc822", file_name=None)

    assert text.sources == (TextSource.EMAIL, TextSource.TEXT_LAYER)
    assert "Total   $50.00" in [line.text for line in text.lines]


def test_attachments_beyond_the_limit_are_not_read(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    attachments = [
        (b"%PDF-1.7" + bytes([n]) * 3000, "application", "pdf", f"{n}.pdf") for n in range(4)
    ]
    router = OcrRouter(runner=rig.runner, config=OcrConfig(max_attachments=2))

    router.read(_email_with(attachments), mime_type="message/rfc822", file_name=None)

    assert rig.runner.count("pdftotext", containing="-layout") == 2
