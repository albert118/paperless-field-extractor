"""The command line, end to end against a fake Paperless and fake OCR tools."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from paperless_field_extractor import __version__, cli
from paperless_field_extractor.client import PaperlessClient
from paperless_field_extractor.ocr.router import OcrRouter
from paperless_field_extractor.ocr.runner import CommandResult
from paperless_field_extractor.settings import Settings, read_token

from .conftest import (
    document_payload,
    field_payload,
    json_response,
    make_client,
    page_payload,
    tag_payload,
)
from .fakes import FakeRunner, make_tessdata, ok, png_bytes, tesseract_handler, tsv

CONTENT = "ABN 12 345 678 901\n25/10/2026\nGST $4.55\nTotal $50.00\n"
ORIGINAL_TSV = tsv(
    (1, 1, 1, 1, 1, 90, "ABN"),
    (1, 1, 1, 1, 2, 90, "12"),
    (1, 1, 1, 2, 1, 90, "25/10/2026"),
    (1, 1, 1, 3, 1, 90, "GST"),
    (1, 1, 1, 3, 2, 90, "$4.55"),
    (1, 1, 1, 4, 1, 90, "Total"),
    (1, 1, 1, 4, 2, 90, "$50.00"),
)


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/tags/":
        return json_response(page_payload([tag_payload(1, "tax deductible", 2)]))
    if path == "/api/custom_fields/":
        return json_response(page_payload([field_payload(1, "Transaction Amount", "monetary")]))
    if path == "/api/documents/":
        return json_response(
            page_payload(
                [
                    document_payload(
                        7, content=CONTENT, mime_type="image/png", original_file_name="r.png"
                    )
                ]
            )
        )
    if path == "/api/documents/7/":
        return json_response(
            document_payload(7, content=CONTENT, mime_type="image/png", original_file_name="r.png")
        )
    if path == "/api/documents/7/download/":
        return httpx.Response(200, content=png_bytes())
    return httpx.Response(404)


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token.txt"
    path.write_text("secret-token\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_client(settings: Settings) -> PaperlessClient:
        read_token(settings.token_file)  # the real client reads it first, so a bad path fails here
        return make_client(_handler)

    def fake_router(args: object, *, cache: bool = True) -> OcrRouter:
        runner = FakeRunner(
            {
                "tesseract": tesseract_handler(
                    make_tessdata(tmp_path / "tessdata"), lambda a: ok(ORIGINAL_TSV)
                )
            }
        )
        return OcrRouter(runner=runner)

    monkeypatch.setattr(cli, "_client", fake_client)
    monkeypatch.setattr(cli, "_router", fake_router)


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_help_names_every_command_and_the_currencies(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])

    out = capsys.readouterr().out
    for word in ("probe", "ocr", "extract", "golden-template", "evaluate", "AUD, USD, JPY"):
        assert word in out


def test_extract_from_a_text_file_prints_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = tmp_path / "receipt.txt"
    text.write_text(CONTENT, encoding="utf-8")

    assert cli.main(["extract", "--text", str(text)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["amount"]["value"] == "50.00"
    assert payload["amount"]["currency"] == "AUD"
    assert payload["date"]["value"] == "2026-10-25"


def test_extract_for_a_document_compares_own_ocr_with_paperless_content(
    token_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["extract", "--doc-id", "7", "--token-file", str(token_file)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["own_ocr"]["amount"]["value"] == "50.00"
    assert payload["paperless_content"]["amount"]["value"] == "50.00"
    assert payload["reconciled"]["amount"] == {
        "write": True,
        "value": "50.00",
        "currency": "AUD",
        "reasons": [],
    }
    assert payload["reconciled"]["date"]["write"] is True


def test_ocr_prints_the_text_of_a_local_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = tmp_path / "receipt.png"
    image.write_bytes(png_bytes())

    assert cli.main(["ocr", "--file", str(image)]) == 0

    assert "Total $50.00" in capsys.readouterr().out


def test_ocr_rejects_an_unsupported_original(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = tmp_path / "receipts.zip"
    archive.write_bytes(b"PK\x03\x04")

    assert cli.main(["ocr", "--file", str(archive)]) == 2
    assert "not an email, PDF or image" in capsys.readouterr().err


def test_probe_writes_the_report_and_prints_notes(
    tmp_path: Path, token_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "out" / "probe.json"

    code = cli.main(["probe", "--token-file", str(token_file), "--output", str(output)])

    assert code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["tag"]["matches"] == [{"id": 1, "document_count": 2}]
    assert "PATCH" in capsys.readouterr().err


def test_errors_become_exit_codes_and_never_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["probe", "--token-file", str(tmp_path / "missing.txt")])

    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("error: ")
    assert "Traceback" not in err


def _git(directory: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=directory, check=True, capture_output=True)


def test_probe_refuses_to_save_receipts_to_a_path_git_would_commit(
    tmp_path: Path,
    token_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text("raw/\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    refused = cli.main(
        ["probe", "--token-file", str(token_file), "--output", "p.json", "--save-raw", "tracked"]
    )
    err = capsys.readouterr().err
    assert refused == 2
    assert "not ignored by git" in err
    assert not (repo / "tracked").exists()

    allowed = cli.main(
        ["probe", "--token-file", str(token_file), "--output", "p.json", "--save-raw", "raw"]
    )
    assert allowed == 0
    assert (repo / "raw" / "7" / "content.txt").exists()


def test_golden_template_then_evaluate_end_to_end(
    tmp_path: Path,
    token_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    golden = tmp_path / "golden.csv"

    assert cli.main(["golden-template", "--token-file", str(token_file), "--out", str(golden)]) == 0
    assert golden.read_text(encoding="utf-8").splitlines()[1] == "7,,,,"

    raw = tmp_path / "raw" / "7"
    raw.mkdir(parents=True)
    (raw / "content.txt").write_text(CONTENT, encoding="utf-8")
    (raw / "original.png").write_bytes(png_bytes())
    golden.write_text(
        "doc_id,currency,amount,date,notes\n7,AUD,50.00,2026-10-25,\n", encoding="utf-8"
    )
    capsys.readouterr()

    passed = cli.main(["evaluate", "--golden", str(golden), "--raw-dir", str(tmp_path / "raw")])
    assert passed == 0
    assert "PASS" in capsys.readouterr().out

    golden.write_text(
        "doc_id,currency,amount,date,notes\n7,AUD,51.00,2026-10-25,\n", encoding="utf-8"
    )
    failed = cli.main(
        [
            "evaluate",
            "--golden",
            str(golden),
            "--raw-dir",
            str(tmp_path / "raw"),
            "--source",
            "content",
            "--json",
            str(tmp_path / "report.json"),
        ]
    )
    assert failed == 4
    assert "WRONG ready 1" in capsys.readouterr().out
    assert (
        json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))["amount"]["wrong_ready"]
        == 1
    )


def test_a_missing_ocr_tool_exits_5_with_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "_router", lambda args, *, cache=True: OcrRouter(runner=FakeRunner({}))
    )
    image = tmp_path / "r.png"
    image.write_bytes(png_bytes())

    code = cli.main(["ocr", "--file", str(image)])

    assert code == 5
    assert "tesseract is not installed" in capsys.readouterr().err


def test_an_unpinned_fingerprint_warns_that_results_are_not_reproducible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unpinned_tesseract(args: Sequence[str]) -> CommandResult:
        if args[1] == "--version":
            return ok("tesseract 5.3.4\n")
        if args[1] == "--list-langs":
            return ok("eng\n")  # no tessdata directory to hash
        return ok(ORIGINAL_TSV)

    monkeypatch.setattr(
        cli,
        "_router",
        lambda args, *, cache=True: OcrRouter(runner=FakeRunner({"tesseract": unpinned_tesseract})),
    )
    image = tmp_path / "r.png"
    image.write_bytes(png_bytes())

    assert cli.main(["ocr", "--file", str(image)]) == 0

    assert "not fully pinned" in capsys.readouterr().err
