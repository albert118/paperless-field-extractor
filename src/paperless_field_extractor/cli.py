"""Command-line interface for paperless-field-extractor."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from paperless_field_extractor import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Parse command-line arguments and run the requested subcommand."""
    parser = _build_parser()
    parser.parse_args(argv)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paperless-field-extractor",
        description=(
            "Extract receipt amounts and dates into Paperless-ngx custom fields"
            " (deterministic, no LLM)."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser
