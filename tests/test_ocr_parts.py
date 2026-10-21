"""TSV parsing, email parsing, preprocessing, the cache and the fingerprint."""

from __future__ import annotations

import io
from email.message import EmailMessage
from pathlib import Path

import pytest
from PIL import Image

from paperless_field_extractor.errors import OcrToolError
from paperless_field_extractor.lines import Line
from paperless_field_extractor.ocr.cache import OcrCache
from paperless_field_extractor.ocr.config import OcrConfig
from paperless_field_extractor.ocr.eml import parse_eml
from paperless_field_extractor.ocr.fingerprint import (
    NOT_INSTALLED,
    UNAVAILABLE,
    collect_fingerprint,
)
from paperless_field_extractor.ocr.preprocess import prepare_image
from paperless_field_extractor.ocr.tsv import parse_tsv
from paperless_field_extractor.ocr.types import InputKind, OcrText, TextSource

from .fakes import FakeRunner, make_tessdata, ok, png_bytes, tesseract_handler, tsv


class TestTsv:
    def test_words_are_joined_into_lines_with_the_lowest_confidence(self) -> None:
        output = tsv(
            (1, 1, 1, 1, 1, 96, "Total"),
            (1, 1, 1, 1, 2, 41, "$50.00"),
            (1, 1, 1, 2, 1, 90, "GST"),
        )

        assert parse_tsv(output) == (Line("Total $50.00", 41.0), Line("GST", 90.0))

    def test_words_are_ordered_by_word_number_not_by_row_order(self) -> None:
        output = tsv((1, 1, 1, 1, 2, 90, "$50.00"), (1, 1, 1, 1, 1, 90, "Total"))

        assert parse_tsv(output)[0].text == "Total $50.00"

    def test_blank_words_and_non_word_rows_are_dropped(self) -> None:
        output = tsv((1, 1, 1, 1, 1, -1, ""), (1, 1, 1, 1, 2, 90, "ok"))
        output += "4\t1\t1\t1\t1\t0\t0\t0\t10\t10\t-1\t\n"

        assert parse_tsv(output) == (Line("ok", 90.0),)

    def test_cjk_words_are_not_separated_by_spaces(self) -> None:
        output = tsv(
            (1, 1, 1, 1, 1, 90, "合"), (1, 1, 1, 1, 2, 90, "計"), (1, 1, 1, 1, 3, 90, "¥1,100")
        )

        assert parse_tsv(output)[0].text == "合計¥1,100"

    def test_full_width_digits_are_normalised(self) -> None:
        assert parse_tsv(tsv((1, 1, 1, 1, 1, 90, "１，１００円")))[0].text == "1,100円"

    def test_empty_output_is_no_lines(self) -> None:
        assert parse_tsv("") == ()

    def test_an_unexpected_header_is_an_error_not_empty_text(self) -> None:
        with pytest.raises(ValueError, match="header"):
            parse_tsv("not\ta\ttsv\nrow\n")


def _eml(
    body: str, *, html: bool = False, date: str | None = "Sat, 25 Oct 2026 10:00:00 +1100"
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "shop@example.test"
    message["To"] = "me@example.test"
    message["Subject"] = "Your receipt"
    if date is not None:
        message["Date"] = date
    message.set_content(body, subtype="html" if html else "plain")
    return message


class TestEml:
    def test_plain_text_body_and_the_senders_local_date(self) -> None:
        content = parse_eml(_eml("Total $50.00\nGST $4.55").as_bytes())

        assert [line.text for line in content.lines] == ["Total $50.00", "GST $4.55"]
        assert content.header_date == "2026-10-25"

    def test_the_date_is_the_date_as_the_sender_wrote_it_not_utc(self) -> None:
        content = parse_eml(_eml("x", date="Sun, 26 Oct 2026 00:30:00 +1100").as_bytes())

        assert content.header_date == "2026-10-26"

    @pytest.mark.parametrize("date", [None, "not a date"])
    def test_a_missing_or_garbled_date_header_is_none(self, date: str | None) -> None:
        message = _eml("x", date=None)
        if date is not None:
            message["Date"] = date

        assert parse_eml(message.as_bytes()).header_date is None

    def test_html_table_rows_stay_on_one_line_and_scripts_are_dropped(self) -> None:
        html = (
            "<html><head><title>x</title><style>td{}</style></head><body>"
            "<script>var total = 999.99;</script>"
            "<table><tr><td>Subtotal</td><td>$45.45</td></tr>"
            "<tr><td>Total</td><td>$50.00</td></tr></table></body></html>"
        )

        content = parse_eml(_eml(html, html=True).as_bytes())
        text = [" ".join(line.text.split()) for line in content.lines if line.text.strip()]

        assert text == ["Subtotal $45.45", "Total $50.00"]

    def test_attachments_are_returned_with_their_type_and_name(self) -> None:
        message = _eml("see attached")
        message.add_attachment(
            b"%PDF-1.7 data", maintype="application", subtype="pdf", filename="receipt.pdf"
        )

        content = parse_eml(message.as_bytes())

        assert [(a.file_name, a.mime_type, a.data) for a in content.attachments] == [
            ("receipt.pdf", "application/pdf", b"%PDF-1.7 data")
        ]


class TestPreprocess:
    @staticmethod
    def _jpeg(width: int, height: int, orientation: int | None = None) -> bytes:
        buffer = io.BytesIO()
        exif = Image.Exif()
        if orientation is not None:
            exif[0x0112] = orientation
        Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format="JPEG", exif=exif)
        return buffer.getvalue()

    def test_output_is_a_grayscale_png_with_the_fixed_long_side(self) -> None:
        with Image.open(io.BytesIO(prepare_image(self._jpeg(400, 200), long_side=1000))) as image:
            assert image.format == "PNG"
            assert image.mode == "L"
            assert image.size == (1000, 500)

    def test_exif_rotation_is_applied_before_scaling(self) -> None:
        rotated = prepare_image(self._jpeg(400, 200, orientation=6), long_side=1000)

        with Image.open(io.BytesIO(rotated)) as image:
            assert image.size == (500, 1000)

    def test_preprocessing_is_byte_for_byte_deterministic(self) -> None:
        data = self._jpeg(300, 150, orientation=3)

        assert prepare_image(data, long_side=900) == prepare_image(data, long_side=900)

    def test_undecodable_bytes_are_a_loud_error(self) -> None:
        with pytest.raises(OcrToolError, match="cannot decode"):
            prepare_image(b"definitely not an image", long_side=1000)


class TestCache:
    def _text(self) -> OcrText:
        return OcrText(
            InputKind.IMAGE,
            (TextSource.OCR,),
            (Line("Total $50.00", 91.5), Line("GST", None)),
            "2026-10-25",
        )

    def test_a_stored_text_round_trips_exactly(self, tmp_path: Path) -> None:
        cache = OcrCache(tmp_path / "cache")
        key = cache.key(b"original", "f" * 64)

        cache.put(key, self._text())

        assert cache.get(key) == self._text()

    def test_the_key_depends_on_both_the_original_and_the_fingerprint(self, tmp_path: Path) -> None:
        cache = OcrCache(tmp_path)

        assert cache.key(b"a", "1" * 64) != cache.key(b"b", "1" * 64)
        assert cache.key(b"a", "1" * 64) != cache.key(b"a", "2" * 64)
        assert cache.key(b"a", "1" * 64) == cache.key(b"a", "1" * 64)

    def test_a_miss_and_a_corrupt_entry_are_both_none(self, tmp_path: Path) -> None:
        cache = OcrCache(tmp_path)
        key = cache.key(b"a", "1" * 64)
        assert cache.get(key) is None

        (tmp_path / f"{key}.json").write_text("{not json", encoding="utf-8")
        assert cache.get(key) is None

    def test_no_temporary_file_is_left_behind(self, tmp_path: Path) -> None:
        cache = OcrCache(tmp_path)
        cache.put(cache.key(b"a", "1" * 64), self._text())

        assert [path.suffix for path in tmp_path.iterdir()] == [".json"]


class TestFingerprint:
    def _runner(self, tmp_path: Path, *, poppler: bool = True) -> FakeRunner:
        handlers = {
            "tesseract": tesseract_handler(make_tessdata(tmp_path / "tessdata"), lambda a: ok()),
        }
        if poppler:
            handlers["pdftotext"] = lambda args: ok(stderr="pdftotext version 24.08.0\n")
        return FakeRunner(handlers)

    def test_versions_and_model_hashes_are_collected(self, tmp_path: Path) -> None:
        fingerprint = collect_fingerprint(self._runner(tmp_path), OcrConfig())

        assert fingerprint.tesseract_version == "tesseract 5.3.4"
        assert fingerprint.poppler_version == "pdftotext version 24.08.0"
        assert [lang for lang, _ in fingerprint.traineddata] == ["eng", "jpn"]
        assert all(len(sha) == 64 for _, sha in fingerprint.traineddata)
        assert fingerprint.pinned

    def test_the_digest_is_stable_and_changes_with_any_setting(self, tmp_path: Path) -> None:
        runner = self._runner(tmp_path)
        base = collect_fingerprint(runner, OcrConfig())

        assert base.digest() == collect_fingerprint(runner, OcrConfig()).digest()
        assert base.digest() != collect_fingerprint(runner, OcrConfig(psm=6)).digest()
        assert base.digest() != collect_fingerprint(runner, OcrConfig(long_side=2000)).digest()
        assert base.digest() != collect_fingerprint(runner, OcrConfig(lang="eng")).digest()

    def test_changing_a_model_file_changes_the_digest(self, tmp_path: Path) -> None:
        runner = self._runner(tmp_path)
        before = collect_fingerprint(runner, OcrConfig()).digest()

        (tmp_path / "tessdata" / "jpn.traineddata").write_bytes(b"a different model")

        assert collect_fingerprint(runner, OcrConfig()).digest() != before

    def test_a_missing_model_file_is_recorded_and_marks_the_run_unpinned(
        self, tmp_path: Path
    ) -> None:
        runner = self._runner(tmp_path)
        (tmp_path / "tessdata" / "jpn.traineddata").unlink()

        fingerprint = collect_fingerprint(runner, OcrConfig())

        assert dict(fingerprint.traineddata)["jpn"] == UNAVAILABLE
        assert not fingerprint.pinned

    def test_missing_poppler_is_recorded_not_fatal(self, tmp_path: Path) -> None:
        fingerprint = collect_fingerprint(self._runner(tmp_path, poppler=False), OcrConfig())

        assert fingerprint.poppler_version == NOT_INSTALLED

    def test_missing_tesseract_is_a_loud_error(self) -> None:
        with pytest.raises(OcrToolError, match="tesseract"):
            collect_fingerprint(FakeRunner({}), OcrConfig())


def test_a_png_helper_produces_a_real_image() -> None:
    with Image.open(io.BytesIO(png_bytes(8, 4))) as image:
        assert image.size == (8, 4)
