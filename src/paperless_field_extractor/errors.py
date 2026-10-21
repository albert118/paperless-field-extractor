"""Exit codes and typed exceptions for paperless-field-extractor.

Every failure that can reach the CLI maps to a distinct exit code so callers can
tell usage errors (2), network or server problems (3), internal guard failures
(4) and missing or failing OCR tooling (5) apart.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NETWORK = 3
EXIT_GUARD = 4
EXIT_TOOL = 5


class ExtractorError(Exception):
    """Base class for all tool errors; carries the process exit code."""

    exit_code: int


class SettingsError(ExtractorError):
    """Invalid or contradictory settings."""

    exit_code = EXIT_USAGE


class PaperlessAuthError(ExtractorError):
    """Paperless rejected the API token (HTTP 401/403)."""

    exit_code = EXIT_NETWORK


class HttpStatusError(ExtractorError):
    """Paperless returned an unexpected non-2xx status."""

    exit_code = EXIT_NETWORK


class TransportError(ExtractorError):
    """The Paperless server could not be reached (network or timeout)."""

    exit_code = EXIT_NETWORK


class ApiVersionError(ExtractorError):
    """Paperless does not serve the API version this client speaks (406)."""

    exit_code = EXIT_NETWORK


class StartupError(ExtractorError):
    """The tag or custom-field configuration cannot be resolved against the instance."""

    exit_code = EXIT_GUARD


class PaginationIntegrityError(ExtractorError):
    """Paperless paging violated an integrity invariant (count, repeated next)."""

    exit_code = EXIT_GUARD


class GoldenSetError(ExtractorError):
    """The golden set file is malformed."""

    exit_code = EXIT_USAGE


class OcrToolError(ExtractorError):
    """An external OCR tool is missing, timed out or failed.

    Never swallowed into a fallback: a missing Tesseract must be loud, otherwise
    a dead OCR path looks like a document with no text.
    """

    exit_code = EXIT_TOOL
