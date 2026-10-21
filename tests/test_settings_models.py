"""Settings validation, token reading and the document model."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperless_field_extractor.errors import SettingsError
from paperless_field_extractor.models import CustomFieldDef, Document
from paperless_field_extractor.settings import Settings, read_token


def test_default_settings_are_valid() -> None:
    settings = Settings()
    assert settings.deduction_tag == "tax deductible"
    assert (settings.field_amount, settings.field_date) == (
        "Transaction Amount",
        "Transaction Date",
    )


@pytest.mark.parametrize(
    "options",
    [
        {"fy_start": "2025-7-1"},
        {"fy_end": "2026-02-30"},
        {"fy_start": "2026-07-01", "fy_end": "2025-07-01"},
        {"page_size": 0},
        {"page_size": 100_001},
        {"base_url": "paperless.local"},
        {"timeout_seconds": 0},
    ],
)
def test_invalid_settings_are_rejected_before_any_network_traffic(
    options: dict[str, object],
) -> None:
    with pytest.raises(SettingsError):
        Settings(**options)  # type: ignore[arg-type]


def test_read_token_strips_whitespace(tmp_path: Path) -> None:
    path = tmp_path / "token.txt"
    path.write_text("  abc123 \n", encoding="utf-8")
    assert read_token(path) == "abc123"


def test_read_token_rejects_an_empty_file_without_echoing_it(tmp_path: Path) -> None:
    path = tmp_path / "token.txt"
    path.write_text("  \n", encoding="utf-8")
    with pytest.raises(SettingsError, match="empty"):
        read_token(path)


def test_read_token_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="cannot read"):
        read_token(tmp_path / "missing.txt")


def test_settings_hold_the_token_file_path_never_the_token() -> None:
    assert not hasattr(Settings(), "token")


def _document(custom_fields: list[dict[str, object]]) -> Document:
    return Document.model_validate({"id": 1, "custom_fields": custom_fields})


def test_field_is_empty_for_absent_null_and_blank_values() -> None:
    document = _document(
        [
            {"field": 1, "value": None},
            {"field": 2, "value": "  "},
            {"field": 3, "value": "AUD12.34"},
        ]
    )
    assert document.field_is_empty(1)
    assert document.field_is_empty(2)
    assert document.field_is_empty(99)
    assert not document.field_is_empty(3)


def test_has_field_is_true_even_when_the_value_is_null() -> None:
    document = _document([{"field": 1, "value": None}])
    assert document.has_field(1)
    assert not document.has_field(2)


def test_unknown_keys_in_the_api_response_are_ignored() -> None:
    document = Document.model_validate({"id": 5, "surprise": {"nested": True}})
    assert document.id == 5


def test_default_currency_is_read_from_extra_data() -> None:
    definition = CustomFieldDef.model_validate(
        {
            "id": 1,
            "name": "Amount",
            "data_type": "monetary",
            "extra_data": {"default_currency": "AUD"},
        }
    )
    assert definition.default_currency() == "AUD"
