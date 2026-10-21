"""Pydantic models for the Paperless-ngx API responses this tool consumes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# The union of value shapes Paperless custom fields can carry. The declared
# union intentionally mirrors the JSON types so a value is never silently
# coerced by pydantic before we inspect it.
CustomFieldValueType = str | int | float | dict[str, Any] | None


class CustomFieldValue(BaseModel):
    """A single filled-in value of one custom field on one document."""

    model_config = ConfigDict(extra="ignore")

    field: int
    value: CustomFieldValueType


class CustomFieldDef(BaseModel):
    """The definition of a custom field."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    data_type: str
    extra_data: dict[str, Any] = Field(default_factory=dict)
    document_count: int = 0

    def default_currency(self) -> str | None:
        """The currency Paperless suggests for monetary fields, if any."""
        value: Any = self.extra_data.get("default_currency")
        return value if isinstance(value, str) else None


class Document(BaseModel):
    """A Paperless document with only the fields this tool needs."""

    model_config = ConfigDict(extra="ignore")

    id: int
    title: str = ""
    created: str = ""
    tags: list[int] = Field(default_factory=list)
    custom_fields: list[CustomFieldValue] = Field(default_factory=list)
    content: str | None = None
    mime_type: str | None = None
    original_file_name: str | None = None

    def has_field(self, field_id: int) -> bool:
        """True when an entry for *field_id* exists, even if its value is null."""
        return any(custom_field.field == field_id for custom_field in self.custom_fields)

    def field_value(self, field_id: int) -> CustomFieldValueType:
        """The stored value for *field_id*, or ``None`` when no entry exists."""
        for custom_field in self.custom_fields:
            if custom_field.field == field_id:
                return custom_field.value
        return None

    def field_is_empty(self, field_id: int) -> bool:
        """True when *field_id* has no entry, a null value or a blank string."""
        value = self.field_value(field_id)
        return value is None or (isinstance(value, str) and value.strip() == "")


class Tag(BaseModel):
    """A Paperless tag."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    document_count: int = 0


class Page[T](BaseModel):
    """One page of a paginated Paperless list endpoint."""

    model_config = ConfigDict(extra="ignore")

    count: int
    next: str | None
    previous: str | None
    results: list[T]
