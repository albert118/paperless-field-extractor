"""Runtime configuration for a paperless-field-extractor run.

The settings live in a frozen, slotted dataclass and are validated eagerly in
``__post_init__`` so a bad configuration fails before any network traffic.
Nothing is read from the environment. The token is read from a file and never
becomes part of the settings object, so it cannot leak through a repr.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

from paperless_field_extractor.errors import SettingsError

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated settings with sensible local-development defaults."""

    base_url: str = "http://localhost:8000"
    token_file: Path = Path("secrets/paperless-token.txt")
    deduction_tag: str = "tax deductible"
    field_amount: str = "Transaction Amount"
    field_date: str = "Transaction Date"
    fy_start: str = "2025-07-01"
    fy_end: str = "2026-06-30"
    page_size: int = 100
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        _validate_iso_date(self.fy_start, "fy_start")
        _validate_iso_date(self.fy_end, "fy_end")
        if self.fy_start > self.fy_end:
            raise SettingsError(
                f"fy_start must be on or before fy_end ({self.fy_start!r} > {self.fy_end!r})"
            )
        if not 1 <= self.page_size <= 100_000:
            raise SettingsError(f"page_size must be between 1 and 100000, got {self.page_size}")
        if not self.base_url.startswith(("http://", "https://")):
            raise SettingsError(
                f"base_url must start with http:// or https://, got {self.base_url!r}"
            )
        if self.timeout_seconds <= 0:
            raise SettingsError(f"timeout_seconds must be positive, got {self.timeout_seconds}")


def read_token(path: Path) -> str:
    """Read the API token from *path*; the token never appears in an error."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsError(f"cannot read the token file {path}: {exc.strerror}") from exc
    token = raw.strip()
    if not token:
        raise SettingsError(f"the token file {path} is empty")
    return token


def _validate_iso_date(value: str, name: str) -> None:
    """Raise ``SettingsError`` unless *value* is a strict ISO calendar date."""
    if _ISO_DATE_RE.match(value) is None:
        raise SettingsError(f"{name} must be an ISO date (YYYY-MM-DD), got {value!r}")
    try:
        datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise SettingsError(f"{name} must be an ISO date (YYYY-MM-DD), got {value!r}") from exc
