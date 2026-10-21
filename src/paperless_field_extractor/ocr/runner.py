"""Running external OCR tools, with failures that cannot be mistaken for empty text."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from paperless_field_extractor.errors import OcrToolError


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    """Runs a command to completion. Tests inject a fake; production uses ``run_command``."""

    def __call__(
        self, args: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None
    ) -> CommandResult: ...


def run_command(
    args: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None
) -> CommandResult:
    """Run *args* and return its output. A missing tool or a timeout raises ``OcrToolError``."""
    merged = {**os.environ, **(env or {})}
    try:
        completed = subprocess.run(
            list(args), capture_output=True, timeout=timeout, env=merged, check=False
        )
    except FileNotFoundError as exc:
        raise OcrToolError(f"{args[0]} is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise OcrToolError(f"{args[0]} timed out after {timeout:.0f}s") from exc
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def require_success(result: CommandResult, tool: str) -> bytes:
    """Return stdout, or raise ``OcrToolError`` carrying the tool's stderr."""
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise OcrToolError(f"{tool} failed with exit code {result.returncode}: {detail}")
    return result.stdout
