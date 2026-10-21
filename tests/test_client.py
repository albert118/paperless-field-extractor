"""The read-only Paperless client: paging integrity, origin pinning, error mapping."""

from __future__ import annotations

import httpx
import pytest

from paperless_field_extractor.errors import (
    ApiVersionError,
    HttpStatusError,
    PaginationIntegrityError,
    PaperlessAuthError,
    TransportError,
)

from .conftest import (
    BASE_URL,
    TOKEN,
    document_payload,
    field_payload,
    json_response,
    make_client,
    page_payload,
    tag_payload,
)


def test_every_request_is_a_get_and_carries_the_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/tags/":
            return json_response(page_payload([tag_payload(1, "tax deductible")]))
        if request.url.path == "/api/custom_fields/":
            return json_response(page_payload([field_payload(1, "Transaction Amount")]))
        return json_response(page_payload([document_payload(7)]))

    with make_client(handler) as client:
        client.list_tags()
        client.list_custom_fields()
        client.list_documents(tag_id=1, created_from="2025-07-01", created_to="2026-06-30")

    assert {request.method for request in seen} == {"GET"}
    assert all(request.headers["Authorization"] == f"Token {TOKEN}" for request in seen)
    assert all(
        request.headers["Accept"] == "application/json; version=10"
        and request.headers["User-Agent"].startswith("paperless-field-extractor/")
        for request in seen
    )


def test_list_documents_filters_by_tag_and_created_window_and_asks_for_content() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return json_response(page_payload([document_payload(7, content="Total $5.00")]))

    with make_client(handler) as client:
        documents = client.list_documents(
            tag_id=3, created_from="2025-07-01", created_to="2026-06-30"
        )

    params = captured[0].url.params
    assert params["tags__id__all"] == "3"
    assert params["created__gte"] == "2025-07-01"
    assert params["created__lte"] == "2026-06-30"
    assert "content" in params["fields"].split(",")
    assert documents[0].content == "Total $5.00"


def test_list_documents_can_skip_content() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return json_response(page_payload([]))

    with make_client(handler) as client:
        client.list_documents(
            tag_id=3, created_from="2025-07-01", created_to="2026-06-30", include_content=False
        )

    assert "content" not in captured[0].url.params["fields"].split(",")


def test_get_document_returns_the_document_with_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/documents/42/"
        return json_response(document_payload(42, content="Total $9.99"))

    with make_client(handler) as client:
        document = client.get_document(42)

    assert document.id == 42
    assert document.content == "Total $9.99"


def test_download_original_returns_the_bytes_and_does_not_ask_for_json() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"%PDF-1.7 raw bytes")

    with make_client(handler) as client:
        data = client.download_original(42)

    assert data == b"%PDF-1.7 raw bytes"
    assert captured[0].url.path == "/api/documents/42/download/"
    assert captured[0].url.params["original"] == "true"
    assert captured[0].headers["Accept"] == "*/*"


def test_versions_are_captured_from_the_first_response() -> None:
    with make_client(lambda request: json_response(page_payload([]))) as client:
        before = (client.api_version, client.server_version)
        client.list_tags()
        after = (client.api_version, client.server_version)

    assert before == (None, None)
    assert after == ("10", "3.3.2")


def test_pagination_follows_next_and_accumulates_every_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return json_response(page_payload([tag_payload(2, "b")], count=2))
        return json_response(
            page_payload([tag_payload(1, "a")], count=2, next_url=f"{BASE_URL}/api/tags/?page=2")
        )

    with make_client(handler) as client:
        tags = client.list_tags()

    assert [tag.name for tag in tags] == ["a", "b"]


def test_pagination_rejects_a_count_that_disagrees_with_the_items_returned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(page_payload([tag_payload(1, "a")], count=3))

    with make_client(handler) as client, pytest.raises(PaginationIntegrityError):
        client.list_tags()


def test_pagination_rejects_a_repeated_next_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            page_payload([tag_payload(1, "a")], count=99, next_url=f"{BASE_URL}/api/tags/?page=2")
        )

    with make_client(handler) as client, pytest.raises(PaginationIntegrityError):
        client.list_tags()


def test_a_next_link_on_another_host_is_re_anchored_and_the_token_never_leaves() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.params.get("page") == "2":
            return json_response(page_payload([tag_payload(2, "b")], count=2))
        return json_response(
            page_payload(
                [tag_payload(1, "a")], count=2, next_url="http://evil.example/api/tags/?page=2"
            )
        )

    with make_client(handler) as client:
        client.list_tags()
        notes = client.origin_rewrites

    assert set(hosts) == {"paperless.test"}
    assert len(notes) == 1
    assert "re-anchored" in notes[0]


def test_an_http_next_link_behind_a_tls_proxy_is_re_anchored_to_https() -> None:
    schemes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        schemes.append(request.url.scheme)
        if request.url.params.get("page") == "2":
            return json_response(page_payload([tag_payload(2, "b")], count=2))
        return json_response(
            page_payload(
                [tag_payload(1, "a")], count=2, next_url="http://paperless.test/api/tags/?page=2"
            )
        )

    with make_client(handler) as client:
        client.list_tags()

    assert set(schemes) == {"https"}


def test_a_base_url_path_prefix_is_kept_when_re_anchoring() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.params.get("page") == "2":
            return json_response(page_payload([tag_payload(2, "b")], count=2))
        return json_response(
            page_payload([tag_payload(1, "a")], count=2, next_url="http://x.test/api/tags/?page=2")
        )

    with make_client(handler, base_url="https://paperless.test/pl") as client:
        client.list_tags()

    assert paths == ["/pl/api/tags/", "/pl/api/tags/"]


def test_a_same_origin_trailing_slash_redirect_is_followed_once() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(301, headers={"Location": "/api/tags/"})
        return json_response(page_payload([tag_payload(1, "a")]))

    with make_client(handler) as client:
        client._request("/api/tags", params={"page_size": "1"})  # noqa: SLF001

    assert paths == ["/api/tags", "/api/tags/"]


def test_a_cross_origin_redirect_is_not_followed() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(301, headers={"Location": "https://evil.example/api/tags/"})

    with make_client(handler) as client, pytest.raises(HttpStatusError) as caught:
        client.list_tags()

    assert hosts == ["paperless.test"]
    assert "evil.example" in str(caught.value)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, PaperlessAuthError),
        (403, PaperlessAuthError),
        (406, ApiVersionError),
        (500, HttpStatusError),
        (404, HttpStatusError),
    ],
)
def test_error_statuses_map_to_typed_errors_that_never_contain_the_token(
    status: int, error: type[Exception]
) -> None:
    with make_client(lambda request: httpx.Response(status)) as client:
        with pytest.raises(error) as caught:
            client.list_tags()

    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_failures_become_transport_errors(exc: type[httpx.TransportError]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("boom", request=request)

    with make_client(handler) as client, pytest.raises(TransportError):
        client.list_tags()
