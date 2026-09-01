"""Deterministic session clustering for local and remote snapshots."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Tuple

DEFAULT_CLUSTER_WINDOW_SECONDS = 24.0 * 60.0 * 60.0
MAX_CLUSTER_WINDOW_SECONDS = 365.0 * 24.0 * 60.0 * 60.0
MAX_CLUSTER_SESSIONS = 4096
MAX_CLUSTER_TEXT = 256


def _text(value: object, *, limit: int = MAX_CLUSTER_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _extra_value(row: Mapping[str, Any], key: str) -> str:
    extra = row.get("extra")
    if not isinstance(extra, Mapping):
        return ""
    return _text(extra.get(key))


def _timestamp(row: Mapping[str, Any]) -> float:
    value = row.get("updated_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    numeric = float(value)
    return numeric if math.isfinite(numeric) and numeric >= 0 else 0.0


def _window(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("cluster window must be a finite positive number")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("cluster window must be a finite positive number") from error
    if (
        not math.isfinite(numeric)
        or not 0 < numeric <= MAX_CLUSTER_WINDOW_SECONDS
    ):
        raise ValueError("cluster window is out of bounds")
    return numeric


def _cluster_key(row: Mapping[str, Any], window: float) -> Tuple[str, str, str, str, int]:
    project = _text(row.get("project")) or "unknown"
    agent = _text(row.get("agent")) or "unknown"
    model = _extra_value(row, "model") or "unknown"
    provider = _extra_value(row, "model_provider") or "unknown"
    bucket = int(_timestamp(row) // window)
    return project, agent, model, provider, bucket


def _cluster_id(key: Tuple[str, str, str, str, int]) -> str:
    payload = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def cluster_sessions(
    rows: Iterable[Mapping[str, Any]],
    *,
    window_seconds: float = DEFAULT_CLUSTER_WINDOW_SECONDS,
) -> List[Dict[str, Any]]:
    """Group bounded session rows by workspace, agent, model, and time bucket."""

    window = _window(window_seconds)
    groups: Dict[Tuple[str, str, str, str, int], Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = _cluster_key(row, window)
        group = groups.get(key)
        if group is None:
            project, agent, model, provider, bucket = key
            group = {
                "cluster_id": _cluster_id(key),
                "project": project,
                "agent": agent,
                "model": model,
                "model_provider": provider,
                "time_bucket": bucket * window,
                "count": 0,
                "session_ids": [],
                "hosts": [],
            }
            groups[key] = group
        group["count"] += 1
        session_id = _text(row.get("session_id"), limit=4096)
        if session_id and len(group["session_ids"]) < MAX_CLUSTER_SESSIONS:
            group["session_ids"].append(session_id)
        host = _text(row.get("host"), limit=256)
        if host and host not in group["hosts"]:
            group["hosts"].append(host)

    result = list(groups.values())
    result.sort(
        key=lambda item: (
            str(item["project"]).casefold(),
            str(item["agent"]).casefold(),
            str(item["model"]).casefold(),
            item["time_bucket"],
            item["cluster_id"],
        )
    )
    for item in result:
        item["hosts"].sort(key=lambda value: value.casefold())
        item["session_ids"].sort()
    return result


def merge_cluster_results(
    groups: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge per-host cluster rows into one deterministic fleet view."""

    merged: Dict[str, Dict[str, Any]] = {}
    for source in groups:
        if not isinstance(source, Mapping):
            continue
        cluster_id = _text(source.get("cluster_id"), limit=64)
        if not cluster_id:
            continue
        target = merged.get(cluster_id)
        if target is None:
            target = {
                "cluster_id": cluster_id,
                "project": _text(source.get("project")) or "unknown",
                "agent": _text(source.get("agent")) or "unknown",
                "model": _text(source.get("model")) or "unknown",
                "model_provider": (
                    _text(source.get("model_provider")) or "unknown"
                ),
                "time_bucket": source.get("time_bucket", 0),
                "count": 0,
                "session_ids": [],
                "hosts": [],
            }
            merged[cluster_id] = target
        count = source.get("count")
        if type(count) is int and count >= 0:
            target["count"] += count
        for value in source.get("session_ids", ()):
            if (
                isinstance(value, str)
                and value not in target["session_ids"]
                and len(target["session_ids"]) < MAX_CLUSTER_SESSIONS
            ):
                target["session_ids"].append(value)
        hosts = list(source.get("hosts", ()))
        host = source.get("host")
        if isinstance(host, str):
            hosts.append(host)
        for value in hosts:
            if (
                isinstance(value, str)
                and value not in target["hosts"]
                and len(target["hosts"]) < MAX_CLUSTER_SESSIONS
            ):
                target["hosts"].append(value)
    result = list(merged.values())
    result.sort(
        key=lambda item: (
            str(item["project"]).casefold(),
            str(item["agent"]).casefold(),
            str(item["model"]).casefold(),
            item["time_bucket"],
            item["cluster_id"],
        )
    )
    for item in result:
        item["hosts"].sort(key=lambda value: value.casefold())
        item["session_ids"].sort()
    return result


__all__ = [
    "DEFAULT_CLUSTER_WINDOW_SECONDS",
    "MAX_CLUSTER_WINDOW_SECONDS",
    "cluster_sessions",
    "merge_cluster_results",
]
