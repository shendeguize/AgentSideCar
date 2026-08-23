"""Dependency-neutral text normalization and Cursor title helpers."""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator, List, Optional, Tuple

CURSOR_TITLE_LIMIT = 160
CURSOR_TITLE_MAX_INPUT_CHARS = 64 * 1024
CURSOR_TITLE_MAX_TEXTS = 256
_CURSOR_METADATA_TAGS = (
    "user_info",
    "open_and_recently_viewed_files",
    "attached_files",
    "agent_transcripts",
    "git_status",
    "rules",
    "system_reminder",
    "timestamp",
)


def normalize_scalar_text(text: str, errors: str = "strict") -> str:
    """Return text containing only valid Unicode scalar values.

    Valid text is returned unchanged. ``strict`` raises for the first surrogate
    code point, while ``replace`` substitutes each surrogate with U+FFFD.
    """

    if errors not in ("strict", "replace"):
        raise ValueError("errors must be 'strict' or 'replace'")

    parts: Optional[List[str]] = None
    segment_start = 0
    for index, character in enumerate(text):
        if not 0xD800 <= ord(character) <= 0xDFFF:
            continue
        if errors == "strict":
            raise UnicodeEncodeError(
                "utf-8",
                text,
                index,
                index + 1,
                "surrogates not allowed",
            )
        if parts is None:
            parts = []
        parts.append(text[segment_start:index])
        parts.append("\ufffd")
        segment_start = index + 1

    if parts is None:
        return text
    parts.append(text[segment_start:])
    return "".join(parts)


def redact_message(
    text: str,
    message: str,
    replacement: str = "[message redacted]",
) -> str:
    """Redact raw and JSON-escaped forms of a message without sanitizing text."""

    if not text or not message:
        return text

    normalized = normalize_scalar_text(message, errors="replace")
    message_forms = (message,) if normalized == message else (message, normalized)
    candidates = set()
    for message_form in message_forms:
        # JSON escaping never shortens a string. Avoid expanding a message that
        # cannot occur in the bounded text being inspected.
        if not message_form or len(message_form) > len(text):
            continue
        candidates.add(message_form)
        for ensure_ascii in (False, True):
            encoded = json.dumps(message_form, ensure_ascii=ensure_ascii)[1:-1]
            if len(encoded) <= len(text):
                candidates.add(encoded)

    for candidate in sorted(candidates, key=len, reverse=True):
        text = text.replace(candidate, replacement)
    return text


def snip(value: Any, limit: int = 120) -> str:
    """Collapse whitespace and truncate text to a display-safe length."""

    text = " ".join(str(value or "").split())
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _wrapper_sections(
    text: str,
    tag: str,
) -> Iterator[Tuple[int, int, int, int]]:
    """Yield bounded outer wrapper and content spans, including nested tags."""

    lowered = text.lower()
    opening_marker = "<{}>".format(tag)
    closing_marker = "</{}>".format(tag)
    cursor = 0
    while cursor < len(text):
        opening = lowered.find(opening_marker, cursor)
        if opening < 0:
            return
        content_start = opening + len(opening_marker)
        scan = content_start
        depth = 1
        while depth:
            nested = lowered.find(opening_marker, scan)
            closing = lowered.find(closing_marker, scan)
            if closing < 0:
                yield opening, len(text), content_start, len(text)
                return
            if 0 <= nested < closing:
                depth += 1
                scan = nested + len(opening_marker)
                continue
            depth -= 1
            if depth:
                scan = closing + len(closing_marker)
                continue
            wrapper_end = closing + len(closing_marker)
            yield opening, wrapper_end, content_start, closing
            cursor = wrapper_end


def _remove_tag_tokens(text: str, tag: str) -> str:
    markers = ("<{}>".format(tag), "</{}>".format(tag))
    lowered = text.lower()
    rendered: List[str] = []
    cursor = 0
    while cursor < len(text):
        positions = [
            (position, marker)
            for marker in markers
            for position in (lowered.find(marker, cursor),)
            if position >= 0
        ]
        if not positions:
            rendered.append(text[cursor:])
            break
        position, marker = min(positions, key=lambda item: item[0])
        rendered.append(text[cursor:position])
        cursor = position + len(marker)
    return "".join(rendered)


def _remove_wrapped_sections(text: str, tag: str) -> str:
    closing_marker = "</{}>".format(tag)
    rendered: List[str] = []
    cursor = 0
    for wrapper_start, wrapper_end, _content_start, _content_end in (
        _wrapper_sections(text, tag)
    ):
        rendered.append(text[cursor:wrapper_start])
        cursor = wrapper_end
    rendered.append(text[cursor:])
    residual = "".join(rendered)
    # A close marker left outside a matched section is malformed metadata.
    # Discard the candidate rather than presenting wrapper contents as a title.
    if closing_marker in residual.lower():
        return ""
    return _remove_tag_tokens(residual, tag)


def extract_cursor_title(
    texts: Iterable[Any],
    fallback: str = "",
) -> str:
    """Extract a bounded title from Cursor user payload text.

    Precedence is the first non-empty ``user_query`` wrapper, then the first
    non-metadata user text, then ``fallback``. At most
    ``CURSOR_TITLE_MAX_TEXTS`` values and ``CURSOR_TITLE_MAX_INPUT_CHARS``
    aggregate characters are inspected. Output preserves the existing
    whitespace and truncation policy of title consumers.
    """

    remaining = CURSOR_TITLE_MAX_INPUT_CHARS
    fallback_user = ""
    for _index, value in zip(range(CURSOR_TITLE_MAX_TEXTS), texts):
        if remaining <= 0:
            break
        if not isinstance(value, str):
            continue
        bounded = value[:remaining]
        remaining -= len(bounded)
        if not bounded:
            continue

        for _start, _end, content_start, content_end in _wrapper_sections(
            bounded,
            "user_query",
        ):
            candidate = _remove_tag_tokens(
                bounded[content_start:content_end],
                "user_query",
            )
            title = snip(candidate, CURSOR_TITLE_LIMIT)
            if title:
                return title

        residual = bounded
        for tag in _CURSOR_METADATA_TAGS:
            residual = _remove_wrapped_sections(residual, tag)
            if not residual:
                break
        if residual:
            residual = _remove_wrapped_sections(residual, "user_query")
        candidate = snip(residual, CURSOR_TITLE_LIMIT)
        if candidate and not fallback_user:
            fallback_user = candidate

    return fallback_user or snip(
        fallback if isinstance(fallback, str) else "",
        CURSOR_TITLE_LIMIT,
    )


__all__ = [
    "CURSOR_TITLE_LIMIT",
    "CURSOR_TITLE_MAX_INPUT_CHARS",
    "CURSOR_TITLE_MAX_TEXTS",
    "extract_cursor_title",
    "normalize_scalar_text",
    "redact_message",
    "snip",
]
