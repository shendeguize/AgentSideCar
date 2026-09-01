"""Bounded, opt-in semantic enrichment for cluster results.

The deterministic cluster result remains the source of truth. This module only
adds a best-effort local DSH headless summary and never sends raw transcripts,
paths, credentials, or unbounded text to a model.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Iterable, Mapping, Optional, Sequence

SEMANTIC_INPUT_LIMIT = 8_000
SEMANTIC_OUTPUT_LIMIT = 4_000
SEMANTIC_TIMEOUT_SECONDS = 60
SEMANTIC_MAX_GROUPS = 100
SEMANTIC_RULES = frozenset(
    {"largest", "recent", "agent", "model", "workspace", "max-groups"}
)
DEFAULT_SEMANTIC_RULES = ("largest", "recent")
_SECRET_RE = re.compile(r"(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{8,}")
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_PATH_RE = re.compile(r"(?:/Users/|/home/|/tmp/|[A-Za-z]:\\)[^\s,)]+")


def redact_semantic_text(value: object, limit: int = SEMANTIC_INPUT_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    text = _SECRET_RE.sub("[secret]", text)
    text = _EMAIL_RE.sub("[email]", text)
    text = _PATH_RE.sub("[path]", text)
    return text[:limit]


def _group_number(group: Mapping[str, Any], key: str) -> float:
    value = group.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _group_text(value: object, limit: int = 256) -> str:
    return str(value).strip()[:limit] if isinstance(value, str) else ""


def select_semantic_groups(
    groups: Iterable[Mapping[str, Any]],
    *,
    rules: Sequence[str] = DEFAULT_SEMANTIC_RULES,
    max_groups: int = SEMANTIC_MAX_GROUPS,
) -> list[Mapping[str, Any]]:
    """Select a deterministic, bounded subset for semantic analysis.

    Rules are ordered priorities. ``largest`` and ``recent`` rank clusters;
    ``agent``, ``model``, and ``workspace`` preserve deterministic diversity
    ordering. ``max-groups`` is accepted as a declarative alias for the cap.
    """

    try:
        cap = int(max_groups)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("semantic max-groups must be a positive integer") from error
    if isinstance(max_groups, bool) or cap <= 0 or cap > 10_000:
        raise ValueError("semantic max-groups is out of bounds")
    normalized = tuple(str(rule).strip().lower() for rule in rules if str(rule).strip())
    unknown = sorted(set(normalized) - SEMANTIC_RULES)
    if unknown:
        raise ValueError("unknown semantic rule: {}".format(",".join(unknown)))
    if len(set(normalized)) != len(normalized):
        raise ValueError("semantic rules must not repeat")
    if "max-groups" in normalized:
        cap = min(cap, SEMANTIC_MAX_GROUPS)
    values = [group for group in groups if isinstance(group, Mapping)]
    priorities = normalized or DEFAULT_SEMANTIC_RULES
    keys = []
    for rule in priorities:
        if rule == "largest":
            keys.append(lambda item: -_group_number(item, "count"))
        elif rule == "recent":
            keys.append(lambda item: -_group_number(item, "time_bucket"))
        elif rule == "agent":
            keys.append(lambda item: _group_text(item.get("agent")).casefold())
        elif rule == "model":
            keys.append(lambda item: _group_text(item.get("model")).casefold())
        elif rule == "workspace":
            keys.append(lambda item: _group_text(item.get("project")).casefold())
    keys.append(lambda item: _group_text(item.get("cluster_id"), limit=64))
    values.sort(key=lambda item: tuple(key(item) for key in keys))
    return values[:cap]


def build_semantic_payload(
    groups: Iterable[Mapping[str, Any]],
    *,
    snippets: Optional[Iterable[object]] = None,
    limit: int = SEMANTIC_INPUT_LIMIT,
    rules: Sequence[str] = DEFAULT_SEMANTIC_RULES,
    max_groups: int = SEMANTIC_MAX_GROUPS,
) -> str:
    rows = []
    for group in select_semantic_groups(groups, rules=rules, max_groups=max_groups):
        rows.append(
            {
                "project": redact_semantic_text(group.get("project"), 160),
                "agent": redact_semantic_text(group.get("agent"), 80),
                "model": redact_semantic_text(group.get("model"), 120),
                "model_provider": redact_semantic_text(
                    group.get("model_provider"), 120
                ),
                "count": group.get("count", 0),
                "hosts": [
                    redact_semantic_text(host, 80)
                    for host in group.get("hosts", ())
                    if isinstance(host, str)
                ],
            }
        )
    payload = {"clusters": rows}
    if snippets:
        payload["snippets"] = [
            redact_semantic_text(snippet, 300)
            for snippet in snippets
            if redact_semantic_text(snippet, 300)
        ][:16]
    return redact_semantic_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        limit,
    )


def run_headless_report(
    payload: str,
    *,
    executable: Optional[str] = None,
    timeout: float = SEMANTIC_TIMEOUT_SECONDS,
) -> dict:
    command = executable or os.environ.get("DSH_BIN") or shutil.which("dsh")
    if not command:
        return {"ok": False, "error": "dsh headless unavailable", "report": None}
    prompt = (
        "根据以下脱敏的会话聚类元数据，给出不超过三条的摘要。"
        "不要猜测缺失字段，不要输出路径、凭据或个人信息："
        + payload
    )
    try:
        result = subprocess.run(
            [command, "--profile", "headless", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "error": "dsh headless failed or timed out", "report": None}
    if result.returncode != 0:
        return {"ok": False, "error": "dsh headless returned a non-zero status", "report": None}
    report = redact_semantic_text(result.stdout, SEMANTIC_OUTPUT_LIMIT)
    return {"ok": bool(report), "error": None if report else "empty headless report", "report": report or None}


__all__ = [
    "DEFAULT_SEMANTIC_RULES",
    "SEMANTIC_INPUT_LIMIT",
    "SEMANTIC_MAX_GROUPS",
    "SEMANTIC_OUTPUT_LIMIT",
    "SEMANTIC_RULES",
    "build_semantic_payload",
    "redact_semantic_text",
    "run_headless_report",
    "select_semantic_groups",
]

