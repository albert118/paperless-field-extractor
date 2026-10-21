"""Shared helpers. Every client test stays offline through ``httpx.MockTransport``."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from paperless_field_extractor.client import PaperlessClient

BASE_URL = "https://paperless.test"
TOKEN = "test-token-1234"


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    page_size: int = 100,
    base_url: str = BASE_URL,
) -> PaperlessClient:
    """A client wired to an offline transport; no socket is ever opened."""
    return PaperlessClient(
        base_url,
        TOKEN,
        page_size=page_size,
        transport=httpx.MockTransport(handler),
    )


def json_response(
    payload: object,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    if headers is None:
        headers = {"X-Api-Version": "10", "X-Version": "3.3.2"}
    return httpx.Response(status_code, json=payload, headers=headers)


def page_payload(
    results: list[dict[str, object]],
    *,
    count: int | None = None,
    next_url: str | None = None,
) -> dict[str, object]:
    return {
        "count": count if count is not None else len(results),
        "next": next_url,
        "previous": None,
        "results": results,
    }


def tag_payload(tag_id: int, name: str, document_count: int = 0) -> dict[str, object]:
    return {"id": tag_id, "name": name, "document_count": document_count}


def field_payload(
    field_id: int, name: str, data_type: str = "string", extra: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "id": field_id,
        "name": name,
        "data_type": data_type,
        "extra_data": extra or {},
        "document_count": 0,
    }


def document_payload(
    doc_id: int,
    *,
    title: str = "Receipt",
    created: str = "2025-10-01T00:00:00Z",
    tags: list[int] | None = None,
    custom_fields: list[dict[str, object]] | None = None,
    content: str | None = None,
    mime_type: str | None = "application/pdf",
    original_file_name: str | None = "receipt.pdf",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": doc_id,
        "title": title,
        "created": created,
        "tags": [1] if tags is None else tags,
        "custom_fields": custom_fields or [],
        "mime_type": mime_type,
        "original_file_name": original_file_name,
    }
    if content is not None:
        payload["content"] = content
    return payload
