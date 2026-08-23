"""Strict, bounded JSON parsing without caller-specific error policy."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


class JSONSyntaxError(ValueError):
    """JSON is malformed or contains a value outside the JSON data model."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class JSONLimitError(ValueError):
    """JSON is validly framed but exceeds a configured resource bound."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class JSONLimits:
    """Immutable resource limits for :func:`parse_json` and :func:`validate_json`.

    Depth starts at zero for the root value. ``max_items`` bounds the total
    number of array elements and object members, while ``max_nodes`` also
    counts the root and every object key.
    """

    max_bytes: Optional[int] = None
    max_depth: Optional[int] = None
    max_items: Optional[int] = None
    max_nodes: Optional[int] = None
    max_string_bytes: Optional[int] = None
    max_integer_bits: Optional[int] = None

    def __post_init__(self) -> None:
        for name in (
            "max_bytes",
            "max_depth",
            "max_items",
            "max_nodes",
            "max_string_bytes",
            "max_integer_bits",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError("{} must be a non-negative integer or None".format(name))


def _object_without_duplicates(
    pairs: Iterable[Tuple[str, Any]],
) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JSONSyntaxError("duplicate_key", "duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    del value
    raise JSONSyntaxError("nonfinite", "non-finite JSON number")


def _raw_payload(payload: object) -> bytes:
    if isinstance(payload, str):
        try:
            return payload.encode("utf-8", "strict")
        except UnicodeEncodeError as error:
            raise JSONSyntaxError(
                "unicode",
                "JSON payload contains invalid Unicode",
            ) from error
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    raise JSONSyntaxError("type", "JSON payload must be bytes or text")


def _parse_integer(value: str, limits: JSONLimits) -> int:
    # A decimal integer needs at least one bit per roughly 0.301 digits.  This
    # cheap rejection also avoids Python 3.11's process-global digit ceiling.
    digits = len(value) - int(value.startswith("-"))
    if (
        limits.max_integer_bits is not None
        and digits > limits.max_integer_bits
    ):
        raise JSONLimitError("integer", "JSON integer exceeds limits")
    try:
        parsed = int(value)
    except ValueError as error:
        raise JSONLimitError("integer", "JSON integer exceeds limits") from error
    if (
        limits.max_integer_bits is not None
        and parsed.bit_length() > limits.max_integer_bits
    ):
        raise JSONLimitError("integer", "JSON integer exceeds limits")
    return parsed


def parse_json(payload: object, limits: JSONLimits) -> Any:
    """Decode one strict UTF-8 JSON value and enforce ``limits``."""

    if not isinstance(limits, JSONLimits):
        raise TypeError("limits must be JSONLimits")
    raw = _raw_payload(payload)
    if not raw:
        raise JSONSyntaxError("empty", "JSON payload is empty")
    if limits.max_bytes is not None and len(raw) > limits.max_bytes:
        raise JSONLimitError("bytes", "JSON payload exceeds byte limit")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise JSONSyntaxError("utf8", "JSON payload is not UTF-8") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
            parse_int=lambda item: _parse_integer(item, limits),
        )
    except (JSONSyntaxError, JSONLimitError):
        raise
    except RecursionError as error:
        raise JSONLimitError("depth", "JSON structure exceeds depth limit") from error
    except json.JSONDecodeError as error:
        raise JSONSyntaxError("malformed", "malformed JSON payload") from error
    except ValueError as error:
        raise JSONSyntaxError("malformed", "malformed JSON payload") from error
    validate_json(value, limits)
    return value


def validate_json(value: Any, limits: JSONLimits) -> None:
    """Validate an existing JSON-like value with an iterative traversal."""

    if not isinstance(limits, JSONLimits):
        raise TypeError("limits must be JSONLimits")
    nodes = 1
    items = 0
    if limits.max_nodes is not None and nodes > limits.max_nodes:
        raise JSONLimitError("nodes", "JSON structure exceeds node limit")
    active_containers = set()
    stack: List[Tuple[Any, int, bool]] = [(value, 0, False)]
    while stack:
        current, depth, leaving = stack.pop()
        if leaving:
            active_containers.remove(id(current))
            continue
        if limits.max_depth is not None and depth > limits.max_depth:
            raise JSONLimitError("depth", "JSON structure exceeds depth limit")
        if current is None or isinstance(current, bool):
            continue
        if isinstance(current, int):
            if (
                limits.max_integer_bits is not None
                and current.bit_length() > limits.max_integer_bits
            ):
                raise JSONLimitError("integer", "JSON integer exceeds limits")
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise JSONSyntaxError("nonfinite", "non-finite JSON number")
            continue
        if isinstance(current, str):
            try:
                encoded = current.encode("utf-8", "strict")
            except UnicodeEncodeError as error:
                raise JSONSyntaxError(
                    "unicode",
                    "JSON string contains invalid Unicode",
                ) from error
            if (
                limits.max_string_bytes is not None
                and len(encoded) > limits.max_string_bytes
            ):
                raise JSONLimitError("string", "JSON string exceeds byte limit")
            continue
        if isinstance(current, list):
            items += len(current)
            nodes += len(current)
            _check_structure_counts(items, nodes, limits)
            _enter_container(current, active_containers)
            stack.append((current, depth, True))
            stack.extend((item, depth + 1, False) for item in reversed(current))
            continue
        if isinstance(current, Mapping):
            pairs = list(current.items())
            items += len(pairs)
            nodes += len(pairs) * 2
            _check_structure_counts(items, nodes, limits)
            _enter_container(current, active_containers)
            stack.append((current, depth, True))
            for key, item in reversed(pairs):
                if not isinstance(key, str):
                    raise JSONSyntaxError("key", "JSON object key is not text")
                stack.append((item, depth + 1, False))
                stack.append((key, depth + 1, False))
            continue
        raise JSONSyntaxError("type", "unsupported JSON value")


def _enter_container(value: Any, active_containers: set) -> None:
    identity = id(value)
    if identity in active_containers:
        raise JSONSyntaxError("cycle", "cyclic JSON value")
    active_containers.add(identity)


def _check_structure_counts(
    items: int,
    nodes: int,
    limits: JSONLimits,
) -> None:
    if limits.max_items is not None and items > limits.max_items:
        raise JSONLimitError("items", "JSON structure exceeds item limit")
    if limits.max_nodes is not None and nodes > limits.max_nodes:
        raise JSONLimitError("nodes", "JSON structure exceeds node limit")


__all__ = [
    "JSONLimitError",
    "JSONLimits",
    "JSONSyntaxError",
    "parse_json",
    "validate_json",
]
