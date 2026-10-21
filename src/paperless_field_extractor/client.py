"""A read-only HTTP client for the Paperless-ngx API.

The client is GET-only by construction: every request goes through ``_send``,
which calls ``httpx.Client.get``, and there are no write methods. The write path
(custom-field PATCH) is not built until its semantics are tested on a throwaway
document. The API token and headers are never logged and never appear in error
messages. ``next`` links that carry their own authority are re-anchored to the
configured base URL before they are followed, so a token-bearing request never
goes to a host the operator did not configure.

Ported from ``paperless-tax-export``, including the origin pinning that project
added after a live proxy served ``http://`` ``next`` links.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, TypeVar

import httpx
from pydantic import TypeAdapter

from paperless_field_extractor import __version__
from paperless_field_extractor.errors import (
    ApiVersionError,
    HttpStatusError,
    PaginationIntegrityError,
    PaperlessAuthError,
    TransportError,
)
from paperless_field_extractor.models import CustomFieldDef, Document, Page, Tag

T = TypeVar("T")

# Beyond this many pages the pagination loop is treated as malformed input.
_MAX_PAGES = 1000

_LIST_FIELDS = "id,title,created,tags,custom_fields,mime_type,original_file_name"
_LIST_FIELDS_WITH_CONTENT = _LIST_FIELDS + ",content"

# The original-file download is binary, so it must not ask for JSON.
_BINARY_HEADERS = {"Accept": "*/*"}


class PaperlessClient:
    """A small, typed, read-only client for the Paperless-ngx REST API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        page_size: int = 100,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {
            "Accept": "application/json; version=10",
            "Authorization": f"Token {token}",
            "User-Agent": f"paperless-field-extractor/{__version__}",
        }
        self._page_size = page_size
        self._api_version: str | None = None
        self._server_version: str | None = None
        self._origin_rewrites: list[str] = []
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    @property
    def api_version(self) -> str | None:
        """The Paperless API version from the first response, if any."""
        return self._api_version

    @property
    def server_version(self) -> str | None:
        """The Paperless release version from the first response, if any."""
        return self._server_version

    @property
    def origin_rewrites(self) -> tuple[str, ...]:
        """Records of ``next`` links re-anchored to the base origin.

        Surfaced as non-fatal notes so a misconfigured server is warned about
        instead of hidden.
        """
        return tuple(self._origin_rewrites)

    def __enter__(self) -> PaperlessClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def list_documents(
        self,
        *,
        tag_id: int,
        created_from: str,
        created_to: str,
        include_content: bool = True,
    ) -> list[Document]:
        """All documents carrying *tag_id* created within the inclusive window."""
        params: dict[str, Any] = {
            "created__gte": created_from,
            "created__lte": created_to,
            "tags__id__all": str(tag_id),
            "fields": _LIST_FIELDS_WITH_CONTENT if include_content else _LIST_FIELDS,
        }
        return self._paginate("/api/documents/", TypeAdapter(Page[Document]), params=params)

    def get_document(self, doc_id: int) -> Document:
        """One document, including its OCR ``content``."""
        response = self._request(f"/api/documents/{doc_id}/")
        return Document.model_validate(response.json())

    def download_original(self, doc_id: int) -> bytes:
        """The original uploaded file of a document, byte for byte."""
        response = self._request(
            f"/api/documents/{doc_id}/download/",
            params={"original": "true"},
            headers=_BINARY_HEADERS,
        )
        return response.content

    def list_tags(self) -> list[Tag]:
        return self._paginate("/api/tags/", TypeAdapter(Page[Tag]))

    def list_custom_fields(self) -> list[CustomFieldDef]:
        return self._paginate("/api/custom_fields/", TypeAdapter(Page[CustomFieldDef]))

    def _paginate(
        self,
        url: str,
        page_adapter: TypeAdapter[Page[T]],
        *,
        params: dict[str, Any] | None = None,
    ) -> list[T]:
        """Fetch every page for *url* and return the accumulated items.

        The first page asks for ``ordering=id`` and the configured page size;
        later requests follow the ``next`` link, re-anchored to the configured
        base URL. The loop guards against a repeated ``next`` URL, runaway
        paging, and a final count that disagrees with the first page's total.
        """
        first_params = {"ordering": "id", "page_size": str(self._page_size)}
        if params is not None:
            first_params.update(params)
        response = self._request(url, params=first_params)
        page = page_adapter.validate_python(response.json())
        expected_count = page.count
        results: list[T] = list(page.results)
        seen_next: set[str] = set()
        next_url = page.next
        pages = 1
        while next_url is not None:
            if next_url in seen_next:
                raise PaginationIntegrityError(f"Paperless repeated the 'next' URL {next_url!r}")
            seen_next.add(next_url)
            pages += 1
            if pages > _MAX_PAGES:
                raise PaginationIntegrityError(f"pagination exceeded {_MAX_PAGES} pages")
            response = self._request(self._resolve_next(next_url))
            page = page_adapter.validate_python(response.json())
            results.extend(page.results)
            next_url = page.next
        if len(results) != expected_count:
            raise PaginationIntegrityError(
                f"Paperless returned {len(results)} items but reported {expected_count} total"
            )
        return results

    def _resolve_next(self, next_url: str) -> str:
        """Return *next_url* for use as a request URL.

        A link that carries its own authority (an absolute URL, a
        protocol-relative URL, or one that differs only by scheme) is
        re-anchored to the configured origin, keeping the path and query, so
        the request and its Authorization header always go to the configured
        host. Links without an authority resolve against the base URL.
        """
        parsed = httpx.URL(next_url)
        base = self._client.base_url
        if parsed.host is None:
            return str(base.join(next_url))
        rewritten = self._reanchor_to_base(parsed, base)
        if str(rewritten) != str(parsed):
            self._origin_rewrites.append(
                f"server advertised {next_url}; re-anchored to {rewritten}"
            )
        return str(rewritten)

    def _reanchor_to_base(self, next_url: httpx.URL, base: httpx.URL) -> httpx.URL:
        """Rebuild *next_url* on *base*'s origin, keeping its path and query.

        The base URL's path prefix is kept unless the advertised path already
        carries it, so a base URL served below a proxy subpath keeps working.
        """
        path = next_url.path or "/"
        base_path = base.path
        if base_path not in ("", "/") and not (
            path == base_path or path.startswith(base_path.rstrip("/") + "/")
        ):
            path = base_path.rstrip("/") + "/" + path.lstrip("/")
        return base.copy_with(
            scheme=base.scheme,
            host=base.host,
            port=base.port,
            path=path,
            query=next_url.query,
        )

    def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        response = self._send(url, params=params, headers=headers)
        target = self._redirect_target(response)
        if target is not None:
            # The resolved target carries its own query; follow at most once.
            response = self._send(target, headers=headers)
        self._capture_versions(response)
        self._raise_for_status(response)
        return response

    def _send(
        self,
        url: str | httpx.URL,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return self._client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise TransportError("the Paperless server timed out") from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise TransportError(f"could not reach the Paperless server: {exc}") from exc

    def _redirect_target(self, response: httpx.Response) -> httpx.URL | None:
        """The one redirect hop this client is willing to follow, if any.

        httpx does not follow redirects. A single ``301``/``308`` hop is followed
        only when it stays on the same origin and the path merely gains or loses
        a trailing slash. Any other redirect is reported by ``_raise_for_status``
        instead, and a token-bearing request is never forwarded to another origin.
        """
        if response.status_code not in (301, 308):
            return None
        location = response.headers.get("Location")
        if not location:
            return None
        target = response.request.url.join(location)
        request_url = response.request.url
        if (target.scheme, target.host, target.port) != (
            request_url.scheme,
            request_url.host,
            request_url.port,
        ):
            return None
        if target.path.rstrip("/") != request_url.path.rstrip("/"):
            return None
        if target.path == request_url.path:
            return None
        return target

    def _capture_versions(self, response: httpx.Response) -> None:
        if self._api_version is None:
            self._api_version = response.headers.get("X-Api-Version")
        if self._server_version is None:
            self._server_version = response.headers.get("X-Version")

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code
        # The request URL identifies the request; the token travels in a header
        # and is never part of it.
        url = str(response.request.url)
        location = response.headers.get("Location")
        redirected = f"; redirected to {location}" if location else ""
        if status in (401, 403):
            raise PaperlessAuthError(
                f"Paperless rejected the API token (HTTP {status} for {url}){redirected}"
            )
        if status == 406:
            raise ApiVersionError(
                f"Paperless does not serve API version 10 (HTTP 406 for {url}){redirected}"
            )
        raise HttpStatusError(f"Paperless returned HTTP {status} for GET {url}{redirected}")
