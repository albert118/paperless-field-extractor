"""Reading Tesseract TSV output into lines with per-line word confidence."""

from __future__ import annotations

import csv
import io
import unicodedata

from paperless_field_extractor.lines import Line, normalise

_WORD_LEVEL = 5


def parse_tsv(tsv: str) -> tuple[Line, ...]:
    """Group Tesseract word rows into lines.

    Each line carries the lowest word confidence on it, so a misread digit on the
    total's line is visible to the extractor.
    """
    reader = csv.reader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    header = next(reader, None)
    if header is None:
        return ()
    column = {name: index for index, name in enumerate(header)}
    required = ("level", "page_num", "block_num", "par_num", "line_num", "word_num", "conf", "text")
    if any(name not in column for name in required):
        raise ValueError("unexpected Tesseract TSV header")

    grouped: dict[tuple[int, int, int, int], list[tuple[int, str, float]]] = {}
    for row in reader:
        if len(row) <= column["text"] or int(row[column["level"]]) != _WORD_LEVEL:
            continue
        text = row[column["text"]].strip()
        confidence = float(row[column["conf"]])
        if not text or confidence < 0:
            continue
        key = (
            int(row[column["page_num"]]),
            int(row[column["block_num"]]),
            int(row[column["par_num"]]),
            int(row[column["line_num"]]),
        )
        grouped.setdefault(key, []).append((int(row[column["word_num"]]), text, confidence))

    lines: list[Line] = []
    for words in grouped.values():
        words.sort(key=lambda word: word[0])
        joined = normalise(_join_words([word[1] for word in words]))
        lines.append(Line(joined, min(word[2] for word in words)))
    return tuple(lines)


def _join_words(words: list[str]) -> str:
    """Join with spaces, except next to CJK characters, which are not space separated."""
    out = ""
    for word in words:
        if out and not (_is_cjk(out[-1]) or _is_cjk(word[0])):
            out += " "
        out += word
    return out


def _is_cjk(char: str) -> bool:
    return unicodedata.east_asian_width(char) in ("W", "F")
