"""Dependency-light helpers shared by presentation adapters."""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional


def row_value(row: object, key: str, default: Any = "") -> Any:
    """Read a field from either a mapping row or an object."""

    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def format_age_seconds(seconds: float) -> str:
    """Format a non-negative, whole-second age using the largest unit."""

    whole_seconds = int(max(0.0, seconds))
    if whole_seconds < 60:
        return "{}s".format(whole_seconds)
    if whole_seconds < 3600:
        return "{}m".format(whole_seconds // 60)
    if whole_seconds < 86400:
        return "{}h".format(whole_seconds // 3600)
    return "{}d".format(whole_seconds // 86400)


def row_age(
    row: object,
    now: Optional[float] = None,
    *,
    default: str = "",
) -> str:
    """Format a mapping/object row age, returning ``default`` if malformed."""

    age_method = getattr(row, "age_str", None)
    if callable(age_method):
        return str(age_method(now))
    try:
        updated_at = float(row_value(row, "updated_at", 0.0))
    except (TypeError, ValueError):
        return default
    current = time.time() if now is None else now
    return format_age_seconds(current - updated_at)


__all__ = ["format_age_seconds", "row_age", "row_value"]
