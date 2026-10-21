"""Reading a ``.eml`` original with the standard library only."""

from __future__ import annotations

import email.utils
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser

from paperless_field_extractor.lines import Line, lines_from_text

_BLOCK_TAGS = frozenset(
    {"p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "hr"}
)
_CELL_TAGS = frozenset({"td", "th"})
_SKIPPED_TAGS = frozenset({"script", "style", "head", "title"})


@dataclass(frozen=True, slots=True)
class Attachment:
    file_name: str | None
    mime_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class EmlContent:
    header_date: str | None
    lines: tuple[Line, ...]
    attachments: tuple[Attachment, ...]


def parse_eml(data: bytes) -> EmlContent:
    """The Date header (as the sender wrote it), body text lines and attachments."""
    message = BytesParser(policy=policy.default).parsebytes(data)
    assert isinstance(message, EmailMessage)
    return EmlContent(
        header_date=_header_date(message),
        lines=lines_from_text(_body_text(message)),
        attachments=tuple(_attachments(message)),
    )


def _header_date(message: EmailMessage) -> str | None:
    raw = message["Date"]
    if raw is None:
        return None
    try:
        return email.utils.parsedate_to_datetime(str(raw)).date().isoformat()
    except TypeError, ValueError:
        return None


def _body_text(message: EmailMessage) -> str:
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    content = body.get_content()
    if not isinstance(content, str):
        return ""
    if body.get_content_type() == "text/html":
        return _html_to_text(content)
    return content


def _attachments(message: EmailMessage) -> list[Attachment]:
    found: list[Attachment] = []
    for part in message.iter_attachments():
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes) and payload:
            found.append(Attachment(part.get_filename(), part.get_content_type(), payload))
    return found


def _html_to_text(html: str) -> str:
    extractor = _HtmlText()
    extractor.feed(html)
    extractor.close()
    return "".join(extractor.parts)


class _HtmlText(HTMLParser):
    """Flatten HTML to text, keeping table rows on one line."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in _CELL_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)
