"""The OCR fingerprint: every input that decides OCR output, hashed.

Determinism here comes from controls, not from an engine guarantee: pinned tool
versions, the ``traineddata`` hash, a fixed ``--psm``, a single OpenMP thread and
fixed preprocessing. The fingerprint records all of them so a result can be tied
to the exact configuration that produced it, and so the OCR cache never returns
text from a different configuration.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import PIL

from paperless_field_extractor.errors import OcrToolError
from paperless_field_extractor.ocr.config import OMP_THREAD_LIMIT, PREPROCESS_VERSION, OcrConfig
from paperless_field_extractor.ocr.runner import CommandRunner

NOT_INSTALLED = "not-installed"
UNAVAILABLE = "unavailable"

_TESSDATA_DIR_RE = re.compile(r'in "(?P<dir>[^"]+)"')


@dataclass(frozen=True, slots=True)
class OcrFingerprint:
    tesseract_version: str
    traineddata: tuple[tuple[str, str], ...]
    poppler_version: str
    pillow_version: str
    lang: str
    psm: int
    dpi: int
    long_side: int
    max_pdf_pages: int
    omp_thread_limit: str
    preprocess_version: str

    def digest(self) -> str:
        """A stable SHA-256 over every field."""
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def pinned(self) -> bool:
        """False when a tool version or a ``traineddata`` hash could not be read."""
        return self.tesseract_version != UNAVAILABLE and all(
            sha != UNAVAILABLE for _, sha in self.traineddata
        )


def collect_fingerprint(runner: CommandRunner, config: OcrConfig) -> OcrFingerprint:
    """Read tool versions and ``traineddata`` hashes. A missing Tesseract raises."""
    timeout = config.timeout_seconds
    version_result = runner(["tesseract", "--version"], timeout=timeout)
    tesseract_version = _first_line(version_result.stdout + version_result.stderr)

    languages = runner(["tesseract", "--list-langs"], timeout=timeout)
    tessdata_dir = _tessdata_dir(languages.stdout + languages.stderr)
    traineddata = tuple(
        (lang, _hash_traineddata(tessdata_dir, lang)) for lang in config.lang.split("+")
    )

    return OcrFingerprint(
        tesseract_version=tesseract_version or UNAVAILABLE,
        traineddata=traineddata,
        poppler_version=_poppler_version(runner, timeout),
        pillow_version=PIL.__version__,
        lang=config.lang,
        psm=config.psm,
        dpi=config.dpi,
        long_side=config.long_side,
        max_pdf_pages=config.max_pdf_pages,
        omp_thread_limit=OMP_THREAD_LIMIT,
        preprocess_version=PREPROCESS_VERSION,
    )


def _poppler_version(runner: CommandRunner, timeout: float) -> str:
    try:
        result = runner(["pdftotext", "-v"], timeout=timeout)
    except OcrToolError:
        return NOT_INSTALLED
    return _first_line(result.stdout + result.stderr) or UNAVAILABLE


def _first_line(raw: bytes) -> str:
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _tessdata_dir(raw: bytes) -> Path | None:
    match = _TESSDATA_DIR_RE.search(raw.decode("utf-8", errors="replace"))
    return Path(match.group("dir")) if match else None


def _hash_traineddata(directory: Path | None, lang: str) -> str:
    if directory is None:
        return UNAVAILABLE
    path = directory / f"{lang}.traineddata"
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return UNAVAILABLE
    return digest.hexdigest()
