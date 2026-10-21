"""Parallel processing: bounded, ordered, and never able to change the output."""

from __future__ import annotations

import io
import os
import re
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from PIL import Image

from paperless_field_extractor.errors import OcrToolError, SettingsError
from paperless_field_extractor.ocr.cache import OcrCache
from paperless_field_extractor.ocr.config import MAX_AUTO_WORKERS, OcrConfig, default_workers
from paperless_field_extractor.ocr.fingerprint import collect_fingerprint
from paperless_field_extractor.ocr.parallel import parallel_map
from paperless_field_extractor.ocr.router import OcrRouter
from paperless_field_extractor.ocr.runner import CommandResult
from paperless_field_extractor.ocr.types import InputKind, OcrInput, OcrText, TextSource

from .fakes import FakeRunner, make_tessdata, ok, png_bytes, tesseract_handler, tsv


class TestParallelMap:
    def test_results_are_in_input_order_whatever_order_tasks_finish(self) -> None:
        def slow_first(item: int) -> int:
            time.sleep(0.05 if item == 0 else 0)
            return item * 10

        assert parallel_map(slow_first, list(range(8)), workers=4) == [i * 10 for i in range(8)]

    def test_work_really_runs_on_several_threads(self) -> None:
        barrier = threading.Barrier(3, timeout=5)

        def meet(item: int) -> int:
            barrier.wait()  # only returns if three tasks are running at the same time
            return item

        assert parallel_map(meet, [1, 2, 3], workers=3) == [1, 2, 3]

    @pytest.mark.parametrize("items", [[], [1]])
    def test_nothing_to_parallelise_runs_inline(self, items: list[int]) -> None:
        main = threading.get_ident()

        assert parallel_map(lambda _: threading.get_ident(), items, workers=8) == [main] * len(
            items
        )

    def test_one_worker_runs_inline(self) -> None:
        main = threading.get_ident()

        assert set(parallel_map(lambda _: threading.get_ident(), [1, 2, 3], workers=1)) == {main}

    def test_an_error_is_raised_and_unstarted_tasks_are_cancelled(self) -> None:
        started: list[int] = []

        def work(item: int) -> int:
            started.append(item)
            if item == 0:
                raise ValueError("boom")
            time.sleep(0.05)
            return item

        with pytest.raises(ValueError, match="boom"):
            parallel_map(work, list(range(50)), workers=2)

        assert len(started) < 50


class TestWorkerCount:
    @pytest.mark.parametrize(
        ("cpus", "expected"),
        [(8, 8), (1, 1), (None, 1), (0, 1), (500, MAX_AUTO_WORKERS)],
    )
    def test_the_default_is_one_worker_per_usable_core(
        self, monkeypatch: pytest.MonkeyPatch, cpus: int | None, expected: int
    ) -> None:
        monkeypatch.setattr(os, "process_cpu_count", lambda: cpus)

        assert default_workers() == expected
        assert OcrConfig().effective_workers() == expected

    def test_an_explicit_count_wins_even_above_the_automatic_cap(self) -> None:
        assert OcrConfig(workers=3).effective_workers() == 3
        assert OcrConfig(workers=64).effective_workers() == 64

    def test_a_negative_count_is_rejected(self) -> None:
        with pytest.raises(SettingsError, match="workers"):
            OcrConfig(workers=-1)

    def test_the_default_uses_more_than_one_worker_on_a_multi_core_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(os, "process_cpu_count", lambda: 16)

        assert OcrRouter(runner=FakeRunner({})).workers == 16

    def test_the_worker_count_is_not_part_of_the_fingerprint(self, tmp_path: Path) -> None:
        runner = FakeRunner(
            {
                "tesseract": tesseract_handler(make_tessdata(tmp_path / "t"), lambda a: ok()),
                "pdftotext": lambda args: ok(stderr="pdftotext version 24.08.0\n"),
            }
        )

        one = collect_fingerprint(runner, OcrConfig(workers=1)).digest()
        many = collect_fingerprint(runner, OcrConfig(workers=32)).digest()

        assert one == many


class _Rig:
    """Fake tools for PDFs that say how many pages and which id they have, and measure overlap."""

    def __init__(self, *, delay: float = 0.03) -> None:
        self._delay = delay
        self._lock = threading.Lock()
        self._running = 0
        self.peak = 0
        self.runner = FakeRunner(
            {
                "tesseract": tesseract_handler(Path("."), self._tesseract),
                "pdftotext": lambda args: ok("  \n"),
                "pdftoppm": self._pdftoppm,
            }
        )

    @staticmethod
    def pdf(identity: int, pages: int) -> bytes:
        return f"%PDF-1.7 id={identity} pages={pages}".encode()

    def _pdftoppm(self, args: Sequence[str]) -> CommandResult:
        header = Path(args[-2]).read_bytes().decode()
        identity = int(re.search(r"id=(\d+)", header).group(1))  # type: ignore[union-attr]
        pages = int(re.search(r"pages=(\d+)", header).group(1))  # type: ignore[union-attr]
        digits = len(str(pages))
        for number in range(1, pages + 1):
            width = identity * 100 + number
            Path(f"{args[-1]}-{number:0{digits}d}.png").write_bytes(png_bytes(width, 10))
        return ok()

    def _tesseract(self, args: Sequence[str]) -> CommandResult:
        with self._lock:
            self._running += 1
            self.peak = max(self.peak, self._running)
        try:
            time.sleep(self._delay)
            width = Image.open(io.BytesIO(Path(args[1]).read_bytes())).size[0]
            return ok(tsv((1, 1, 1, 1, 1, 90, f"w{width}")))
        finally:
            with self._lock:
                self._running -= 1

    def router(self, workers: int) -> OcrRouter:
        return OcrRouter(runner=self.runner, config=OcrConfig(workers=workers))


def _read(workers: int, pdfs: list[bytes]) -> tuple[list[OcrText], int]:
    rig = _Rig()
    inputs = [OcrInput(pdf, "application/pdf", None) for pdf in pdfs]
    return rig.router(workers).read_many(inputs), rig.peak


class TestRouterParallelism:
    def test_pages_of_one_pdf_run_in_parallel_but_never_beyond_the_worker_count(self) -> None:
        rig = _Rig()

        text = rig.router(4).read(_Rig.pdf(1, 12), mime_type="application/pdf", file_name=None)

        assert [line.text for line in text.lines] == [f"w{100 + n}" for n in range(1, 13)]
        assert 1 < rig.peak <= 4

    def test_documents_run_in_parallel_with_results_in_input_order(self) -> None:
        pdfs = [_Rig.pdf(identity, 1) for identity in range(1, 9)]

        texts, peak = _read(4, pdfs)

        assert [text.lines[0].text for text in texts] == [f"w{i * 100 + 1}" for i in range(1, 9)]
        assert 1 < peak <= 4

    def test_documents_and_pages_together_never_exceed_the_worker_count(self) -> None:
        pdfs = [_Rig.pdf(identity, 3) for identity in range(1, 7)]

        texts, peak = _read(3, pdfs)

        assert len(texts) == 6
        assert 1 < peak <= 3

    @pytest.mark.parametrize("workers", [2, 8, 32])
    def test_output_is_identical_at_any_worker_count(self, workers: int) -> None:
        pdfs = [_Rig.pdf(identity, 4) for identity in range(1, 6)]

        sequential, _ = _read(1, pdfs)
        parallel, _ = _read(workers, pdfs)

        assert parallel == sequential

    def test_one_worker_never_overlaps(self) -> None:
        _, peak = _read(1, [_Rig.pdf(1, 5), _Rig.pdf(2, 5)])

        assert peak == 1

    def test_every_tool_still_runs_with_a_single_openmp_thread(self) -> None:
        rig = _Rig()
        rig.router(8).read_many(
            [OcrInput(_Rig.pdf(i, 2), "application/pdf", None) for i in range(1, 5)]
        )

        tesseract_envs = [
            env
            for call, env in zip(rig.runner.calls, rig.runner.envs, strict=True)
            if call[0] == "tesseract"
        ]
        assert tesseract_envs
        assert all(env == {"OMP_THREAD_LIMIT": "1"} for env in tesseract_envs)

    def test_an_unsupported_input_in_a_batch_is_returned_in_place(self) -> None:
        rig = _Rig()

        texts = rig.router(4).read_many(
            [
                OcrInput(_Rig.pdf(1, 1), "application/pdf", None),
                OcrInput(b"PK", "application/zip", None),
                OcrInput(_Rig.pdf(2, 1), "application/pdf", None),
            ]
        )

        assert [text.kind for text in texts] == [
            InputKind.PDF,
            InputKind.UNSUPPORTED,
            InputKind.PDF,
        ]

    def test_a_failing_tool_in_a_batch_raises_instead_of_returning_partial_results(self) -> None:
        runner = FakeRunner(
            {
                "pdftotext": lambda args: ok("  \n"),
                "pdftoppm": lambda args: CommandResult(1, b"", b"Syntax Error: broken pdf"),
            }
        )
        router = OcrRouter(runner=runner, config=OcrConfig(workers=4))

        with pytest.raises(OcrToolError, match="broken pdf"):
            router.read_many([OcrInput(b"%PDF-1.7", "application/pdf", None)] * 4)

    def test_the_cache_is_safe_when_threads_read_the_same_original_at_once(
        self, tmp_path: Path
    ) -> None:
        rig = _Rig()
        make_tessdata(tmp_path / "t")
        cache = OcrCache(tmp_path / "cache")
        router = OcrRouter(
            runner=FakeRunner(
                {
                    "tesseract": tesseract_handler(tmp_path / "t", rig._tesseract),  # noqa: SLF001
                    "pdftotext": lambda args: (
                        ok(stderr="pdftotext version 24.08.0\n") if args[1] == "-v" else ok("  \n")
                    ),
                    "pdftoppm": rig._pdftoppm,  # noqa: SLF001
                }
            ),
            config=OcrConfig(workers=8),
            cache=cache,
        )
        same = OcrInput(_Rig.pdf(1, 2), "application/pdf", None)

        texts = router.read_many([same] * 16)

        assert all(text == texts[0] for text in texts)
        assert [p.suffix for p in (tmp_path / "cache").iterdir()] == [".json"]


def test_concurrent_cache_writes_of_one_key_never_collide(tmp_path: Path) -> None:
    cache = OcrCache(tmp_path)
    key = cache.key(b"original", "f" * 64)
    text = OcrText(InputKind.IMAGE, (TextSource.OCR,), (), None)
    errors: list[BaseException] = []

    def write() -> None:
        try:
            for _ in range(50):
                cache.put(key, text)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert cache.get(key) == text
    assert [p.suffix for p in tmp_path.iterdir()] == [".json"]
