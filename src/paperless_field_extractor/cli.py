"""Command-line interface for paperless-field-extractor.

Every command here is read-only toward Paperless. ``main`` maps every
``ExtractorError`` to its exit code so a failure never escapes as a traceback;
argparse usage errors still exit 2 with ``SystemExit``.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from paperless_field_extractor import __version__
from paperless_field_extractor.amounts import canonical_amount
from paperless_field_extractor.client import PaperlessClient
from paperless_field_extractor.errors import (
    EXIT_GUARD,
    EXIT_OK,
    ExtractorError,
    SettingsError,
)
from paperless_field_extractor.extract import (
    Extraction,
    Reconciled,
    extract,
    reconcile_amount,
    reconcile_date,
    to_jsonable,
)
from paperless_field_extractor.golden import Outcome, evaluate, load_golden, write_template
from paperless_field_extractor.lines import lines_from_text
from paperless_field_extractor.ocr.cache import OcrCache
from paperless_field_extractor.ocr.config import OcrConfig
from paperless_field_extractor.ocr.router import OcrRouter
from paperless_field_extractor.ocr.types import InputKind, OcrText
from paperless_field_extractor.probe import dump_json, run_probe, select_fy_documents
from paperless_field_extractor.settings import Settings, read_token

_DEFAULT_CACHE_DIR = ".ocr-cache"


def main(argv: Sequence[str] | None = None) -> int:
    """Parse command-line arguments and run the requested subcommand."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except ExtractorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code


def _build_parser() -> argparse.ArgumentParser:
    defaults = Settings()
    parser = argparse.ArgumentParser(
        prog="paperless-field-extractor",
        description=(
            "Extract receipt amounts and dates into Paperless-ngx custom fields"
            " (deterministic, no LLM). Currencies: AUD, USD, JPY."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    probe = commands.add_parser(
        "probe",
        help="read-only Stage 0 facts: fields, receipt mix, coverage, barcodes",
        description=(
            "Write output/probe.json (counts and metadata only). With --save-raw, also"
            " download originals and Paperless content for the golden set."
        ),
    )
    _add_connection(probe, defaults)
    _add_selection(probe, defaults)
    probe.add_argument("--output", type=Path, default=Path("output/probe.json"))
    probe.add_argument(
        "--save-raw",
        type=Path,
        metavar="DIR",
        help="save originals and content under DIR (must be ignored by git)",
    )
    probe.add_argument("--barcodes", action="store_true", help="scan originals for barcodes")
    probe.set_defaults(handler=_probe)

    ocr = commands.add_parser("ocr", help="read text from one receipt original")
    source = ocr.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="a local receipt original")
    source.add_argument("--doc-id", type=int, help="download this document's original")
    _add_connection(ocr, defaults)
    _add_ocr(ocr)
    ocr.set_defaults(handler=_ocr)

    extract_parser = commands.add_parser(
        "extract", help="extract the total and date from text or from one document"
    )
    source = extract_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", type=Path, help="a plain text file")
    source.add_argument("--doc-id", type=int, help="compare own OCR with Paperless content")
    _add_connection(extract_parser, defaults)
    _add_ocr(extract_parser)
    extract_parser.set_defaults(handler=_extract)

    template = commands.add_parser(
        "golden-template", help="write a blank golden.csv for the FY documents"
    )
    _add_connection(template, defaults)
    _add_selection(template, defaults)
    template.add_argument("--out", type=Path, default=Path("scratch/golden.csv"))
    template.add_argument("--force", action="store_true")
    template.set_defaults(handler=_golden_template)

    score = commands.add_parser("evaluate", help="score extraction against the golden set")
    score.add_argument("--golden", type=Path, default=Path("scratch/golden.csv"))
    score.add_argument("--raw-dir", type=Path, required=True, help="the probe --save-raw directory")
    score.add_argument(
        "--source",
        choices=("content", "ocr", "both"),
        default="both",
        help="text source: Paperless content, own OCR, or both reconciled",
    )
    score.add_argument("--json", type=Path, help="also write the report as JSON")
    _add_ocr(score)
    score.set_defaults(handler=_evaluate)
    return parser


def _add_connection(parser: argparse.ArgumentParser, defaults: Settings) -> None:
    parser.add_argument("--base-url", default=defaults.base_url)
    parser.add_argument("--token-file", type=Path, default=defaults.token_file)


def _add_selection(parser: argparse.ArgumentParser, defaults: Settings) -> None:
    parser.add_argument("--tag", default=defaults.deduction_tag)
    parser.add_argument("--fy-start", default=defaults.fy_start)
    parser.add_argument("--fy-end", default=defaults.fy_end)


def _add_ocr(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ocr-lang", default=OcrConfig().lang)
    parser.add_argument("--cache-dir", type=Path, default=Path(_DEFAULT_CACHE_DIR))


def _settings(args: argparse.Namespace) -> Settings:
    options: dict[str, object] = {"base_url": args.base_url, "token_file": args.token_file}
    if hasattr(args, "tag"):
        options.update(deduction_tag=args.tag, fy_start=args.fy_start, fy_end=args.fy_end)
    return Settings(**options)  # type: ignore[arg-type]


def _client(settings: Settings) -> PaperlessClient:
    return PaperlessClient(
        settings.base_url,
        read_token(settings.token_file),
        page_size=settings.page_size,
        timeout=settings.timeout_seconds,
    )


def _router(args: argparse.Namespace, *, cache: bool = True) -> OcrRouter:
    directory = getattr(args, "cache_dir", None)
    return OcrRouter(
        config=OcrConfig(lang=getattr(args, "ocr_lang", OcrConfig().lang)),
        cache=OcrCache(directory) if cache and directory is not None else None,
    )


def _probe(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if args.save_raw is not None:
        _require_ignored(args.save_raw)
    download = args.save_raw is not None or args.barcodes
    router = _router(args, cache=False) if download else None
    with _client(settings) as client:
        report = run_probe(
            client,
            settings,
            router=router,
            save_raw=args.save_raw,
            scan_barcodes=args.barcodes,
            checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(dump_json(report), encoding="utf-8")
    print(f"wrote {args.output}")
    for note in report["notes"]:
        print(f"note: {note}", file=sys.stderr)
    return EXIT_OK


def _ocr(args: argparse.Namespace) -> int:
    router = _router(args)
    if args.file is not None:
        data = args.file.read_bytes()
        text = router.read(data, mime_type=_guess_mime(args.file.name), file_name=args.file.name)
    else:
        with _client(_settings(args)) as client:
            document = client.get_document(args.doc_id)
            data = client.download_original(args.doc_id)
        text = router.read(
            data, mime_type=document.mime_type, file_name=document.original_file_name
        )
    if text.kind is InputKind.UNSUPPORTED:
        raise SettingsError("the original is not an email, PDF or image")
    _warn_if_unpinned(router)
    print(text.plain())
    return EXIT_OK


def _extract(args: argparse.Namespace) -> int:
    if args.text is not None:
        content = lines_from_text(args.text.read_text(encoding="utf-8"))
        print(json.dumps(to_jsonable(extract(content)), indent=2, ensure_ascii=False))
        return EXIT_OK

    router = _router(args)
    with _client(_settings(args)) as client:
        document = client.get_document(args.doc_id)
        original = client.download_original(args.doc_id)
    own_text = router.read(
        original, mime_type=document.mime_type, file_name=document.original_file_name
    )
    own = extract(own_text.lines, header_date=own_text.header_date)
    paperless = extract(lines_from_text(document.content or ""))
    _warn_if_unpinned(router)
    result = {
        "own_ocr": to_jsonable(own),
        "paperless_content": to_jsonable(paperless),
        "reconciled": {
            "amount": _reconciled_json(reconcile_amount(own, paperless)),
            "date": _reconciled_json(reconcile_date(own, paperless)),
        },
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return EXIT_OK


def _golden_template(args: argparse.Namespace) -> int:
    _require_ignored(args.out.parent)
    with _client(_settings(args)) as client:
        documents = select_fy_documents(client, _settings(args))
    write_template(args.out, (document.id for document in documents), force=args.force)
    print(f"wrote {args.out} with {len(documents)} blank rows")
    return EXIT_OK


def _evaluate(args: argparse.Namespace) -> int:
    rows, pending = load_golden(args.golden)
    router = _router(args)
    amounts: dict[int, Outcome] = {}
    dates: dict[int, Outcome] = {}
    for row in rows:
        folder = args.raw_dir / str(row.doc_id)
        own = _read_own(router, folder) if args.source in ("ocr", "both") else None
        paperless = _read_content(folder) if args.source in ("content", "both") else None
        amounts[row.doc_id], dates[row.doc_id] = _outcomes(own, paperless)
    report = evaluate(rows, amounts, dates, pending=pending)
    print(report.render())
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    _warn_if_unpinned(router, enabled=args.source != "content")
    return EXIT_OK if report.passes_acceptance else EXIT_GUARD


def _read_own(router: OcrRouter, folder: Path) -> Extraction | None:
    originals = sorted(folder.glob("original.*"))
    if not originals:
        return None
    original = originals[0]
    text: OcrText = router.read(
        original.read_bytes(), mime_type=_guess_mime(original.name), file_name=original.name
    )
    return extract(text.lines, header_date=text.header_date)


def _read_content(folder: Path) -> Extraction | None:
    path = folder / "content.txt"
    if not path.exists():
        return None
    return extract(lines_from_text(path.read_text(encoding="utf-8")))


def _outcomes(own: Extraction | None, paperless: Extraction | None) -> tuple[Outcome, Outcome]:
    if own is not None and paperless is not None:
        amount = reconcile_amount(own, paperless)
        date = reconcile_date(own, paperless)
        return (
            Outcome(amount.value, amount.currency, amount.write),
            Outcome(date.value, None, date.write),
        )
    single = own or paperless
    if single is None:
        return Outcome(None, None, False), Outcome(None, None, False)
    amount_value = (
        canonical_amount(single.amount.value, single.amount.currency) if single.amount else None
    )
    return (
        Outcome(
            amount_value, single.amount.currency if single.amount else None, single.amount_ready
        ),
        Outcome(single.date.value if single.date else None, None, single.date_ready),
    )


def _reconciled_json(reconciled: Reconciled) -> dict[str, object]:
    return {
        "write": reconciled.write,
        "value": reconciled.value,
        "currency": reconciled.currency,
        "reasons": [reason.value for reason in reconciled.reasons],
    }


def _warn_if_unpinned(router: OcrRouter, *, enabled: bool = True) -> None:
    if not enabled:
        return
    fingerprint = router.fingerprint
    if not fingerprint.pinned:
        print(
            "warning: the OCR fingerprint is not fully pinned (a tool version or traineddata"
            " hash could not be read); results are not reproducible",
            file=sys.stderr,
        )


def _guess_mime(name: str) -> str | None:
    return mimetypes.guess_type(name)[0]


def _require_ignored(directory: Path) -> None:
    """Refuse to write receipt data to a path git would commit."""
    probe = directory / ".receipt-data"
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(probe)], capture_output=True, check=False
        )
    except FileNotFoundError:
        return
    if result.returncode == 1:
        raise SettingsError(
            f"{directory} is not ignored by git; refusing to write receipt data there"
            " (use tests/fixtures/raw/ or scratch/)"
        )
