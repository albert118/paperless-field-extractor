"""An on-disk OCR cache keyed by ``sha256(original) + fingerprint``.

A rerun on the same original with the same configuration returns identical text,
without running OCR again. The cache holds text read from real receipts, so its
directory is git-ignored.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from paperless_field_extractor.lines import Line
from paperless_field_extractor.ocr.types import InputKind, OcrText, TextSource


class OcrCache:
    """Stores one JSON file per ``(original, fingerprint)`` pair."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def key(self, original: bytes, fingerprint_digest: str) -> str:
        return f"{hashlib.sha256(original).hexdigest()}-{fingerprint_digest[:16]}"

    def get(self, key: str) -> OcrText | None:
        """The cached text, or ``None`` on a miss or an unreadable entry."""
        path = self._path(key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return OcrText(
                kind=InputKind(raw["kind"]),
                sources=tuple(TextSource(source) for source in raw["sources"]),
                lines=tuple(Line(text, conf) for text, conf in raw["lines"]),
                header_date=raw["header_date"],
            )
        except OSError, ValueError, KeyError, TypeError:
            return None

    def put(self, key: str, text: OcrText) -> None:
        """Write *text* atomically, so a crash never leaves a half-written entry."""
        self._directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": text.kind.value,
            "sources": [source.value for source in text.sources],
            "lines": [[line.text, line.min_conf] for line in text.lines],
            "header_date": text.header_date,
        }
        path = self._path(key)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def _path(self, key: str) -> Path:
        return self._directory / f"{key}.json"
