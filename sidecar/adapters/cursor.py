"""Cursor IDE JSONL and Cursor CLI SQLite adapter."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Hashable, Iterable, List, Mapping, Optional, Sequence, Tuple, Union, cast

from sidecar.adapters.base import (
    Adapter,
    DEFAULT_METADATA_CACHE_SIZE,
    MetadataCache,
    compact_json,
    content_items,
    file_signature,
    local_timestamp,
    read_jsonl_prefix,
    snip,
    text_content,
)
from sidecar.model import Event, Session, Status

_USER_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL | re.IGNORECASE)
_META_LIMIT = 256 * 1024
# SQLite mtimes are activity snapshots, not durable leases.  These windows
# mirror the generic state engine's fresh and idle defaults.
_CLI_WORKING_SECONDS = 90.0
_CLI_IDLE_SECONDS = 15.0 * 60.0
_CursorMetadata = Union[str, Tuple[str, str, Dict[str, Any]]]


@lru_cache(maxsize=256)
def decode_cursor_project_slug(slug: str) -> str:
    """Resolve Cursor's ambiguous slash-to-dash project encoding when possible."""

    parts = slug.split("-")

    def resolve(base: Path, index: int) -> Optional[Path]:
        if index == len(parts):
            return base
        for end in range(len(parts), index, -1):
            for separator in ("-", "_"):
                candidate = base / separator.join(parts[index:end])
                if candidate.is_dir():
                    resolved = resolve(candidate, end)
                    if resolved is not None:
                        return resolved
        return None

    resolved = resolve(Path("/"), 0)
    if resolved is not None:
        return str(resolved)
    if slug.startswith("Users-"):
        return "/" + slug.replace("-", "/")
    return slug

def _first_cursor_user_text(path: Path) -> str:
    for record in read_jsonl_prefix(path):
        message = record.get("message")
        if not isinstance(message, Mapping):
            message = {}
        role = record.get("role") or message.get("role")
        if role != "user":
            continue
        content = message.get("content", record.get("content"))
        for item in content_items(content):
            if item.get("type") != "text":
                continue
            text = str(item.get("text") or "")
            if not text:
                continue
            query = _USER_QUERY.search(text)
            return snip(query.group(1) if query else text, 160)
    return ""


def _decode_meta_value(value: Any) -> Any:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return value
    text = value.strip()
    candidates = [text]
    if len(text) % 2 == 0 and text and all(char in "0123456789abcdefABCDEF" for char in text):
        try:
            candidates.append(bytes.fromhex(text).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            pass
    for candidate in candidates:
        decoded: Any = candidate
        for _ in range(2):
            if not isinstance(decoded, str):
                break
            try:
                decoded = json.loads(decoded)
            except json.JSONDecodeError:
                break
        if decoded != candidate:
            return decoded
    return text


def _direct_string(value: Any, names: Sequence[str]) -> str:
    if not isinstance(value, Mapping):
        return ""
    lowered = {str(key).lower(): item for key, item in value.items()}
    for name in names:
        item = lowered.get(name.lower())
        if isinstance(item, str) and item.strip():
            return item.strip()
    return ""


def _read_cli_meta(path: Path) -> Tuple[str, str, Dict[str, Any]]:
    """Read Cursor CLI metadata through a read-only SQLite URI."""

    rows: List[Tuple[Any, Any]] = []
    connection: Optional[sqlite3.Connection] = None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only = ON")
        try:
            rows = list(
                connection.execute(
                    "SELECT key, substr(value, 1, ?) FROM meta LIMIT 64",
                    (_META_LIMIT,),
                )
            )
        except sqlite3.DatabaseError:
            rows = [
                ("", row[0])
                for row in connection.execute(
                    "SELECT substr(value, 1, ?) FROM meta LIMIT 64",
                    (_META_LIMIT,),
                )
            ]
    except (OSError, sqlite3.DatabaseError):
        rows = []
    finally:
        if connection is not None:
            connection.close()

    title = ""
    project = ""
    decoded_rows: Dict[str, Any] = {}
    for key, raw in rows:
        key_text = str(key or "")
        value = _decode_meta_value(raw)
        decoded_rows[key_text] = value
        if not title and key_text.lower() == "name" and isinstance(value, str):
            title = value
        if not project and key_text.lower() in ("cwd", "project", "workspace"):
            if isinstance(value, str):
                project = value
        title = title or _direct_string(value, ("name", "title"))
        project = project or _direct_string(
            value, ("cwd", "projectPath", "workspacePath", "rootPath")
        )
    return snip(title, 160), project, {"meta_keys": sorted(filter(None, decoded_rows))}


def _sqlite_store_signature(path: Path) -> Tuple[float, Dict[str, Dict[str, Any]]]:
    """Capture activity evidence for a SQLite database and its live WAL files."""

    updated_at = 0.0
    signature: Dict[str, Dict[str, Any]] = {}
    for name, candidate in (
        ("db", path),
        ("wal", Path(str(path) + "-wal")),
        ("shm", Path(str(path) + "-shm")),
    ):
        try:
            stat_result = candidate.stat()
        except OSError:
            signature[name] = {"exists": False, "mtime_ns": 0, "size": 0}
            continue
        mtime_ns = int(
            getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1e9))
        )
        signature[name] = {
            "exists": True,
            "mtime_ns": mtime_ns,
            "size": int(stat_result.st_size),
        }
        updated_at = max(updated_at, mtime_ns / 1e9)
    return updated_at, signature


def _sqlite_write_activity_at(signature: Mapping[str, Any]) -> float:
    """Return the newest DB/WAL mtime, excluding read-touched SHM state."""

    activity_at = 0.0
    for name in ("db", "wal"):
        details = signature.get(name)
        if not isinstance(details, Mapping) or not details.get("exists"):
            continue
        mtime_ns = details.get("mtime_ns")
        if isinstance(mtime_ns, (int, float)) and not isinstance(mtime_ns, bool):
            activity_at = max(activity_at, float(mtime_ns) / 1e9)
    return activity_at


class CursorAdapter(Adapter):
    name = "cursor"
    agent_names = ("cursor", "cursor-ide", "cursor-cli")

    def __init__(
        self,
        metadata_cache_size: int = DEFAULT_METADATA_CACHE_SIZE,
    ) -> None:
        self._metadata_cache: MetadataCache[_CursorMetadata] = MetadataCache(
            metadata_cache_size
        )

    def discover(self, home: Path) -> Iterable[Session]:
        sessions: List[Session] = []
        active_signatures: List[Hashable] = []
        projects_root = home / ".cursor" / "projects"
        patterns = (
            "*/agent-transcripts/*.jsonl",
            "*/agent-transcripts/*/*.jsonl",
            "*/agent-transcripts/*/subagents/*.jsonl",
        )
        seen = set()
        for pattern in patterns:
            for transcript in projects_root.glob(pattern):
                if transcript in seen:
                    continue
                seen.add(transcript)
                signature = file_signature(transcript)
                if signature is None:
                    continue
                active_signatures.append(signature)
                updated_at = signature[1] / 1e9
                transcript_root = next(
                    (parent for parent in transcript.parents if parent.name == "agent-transcripts"),
                    None,
                )
                if transcript_root is None:
                    continue
                project = decode_cursor_project_slug(transcript_root.parent.name)
                parent_id = (
                    transcript.parent.parent.name if transcript.parent.name == "subagents" else None
                )
                sessions.append(
                    Session(
                        agent="cursor-ide",
                        session_id=transcript.stem,
                        project=project,
                        transcript=str(transcript),
                        updated_at=updated_at,
                        title=cast(
                            str,
                            self._metadata_cache.get_or_load(
                                signature,
                                lambda: _first_cursor_user_text(transcript),
                            ),
                        ),
                        parent_id=parent_id,
                        extra={"source": "ide"},
                    )
                )

        for store in (home / ".cursor" / "chats").glob("*/*/store.db"):
            db_signature = file_signature(store)
            if db_signature is None:
                continue
            wal_signature = file_signature(Path(str(store) + "-wal"))
            metadata_signature = (db_signature, wal_signature)
            active_signatures.append(metadata_signature)
            title, project, meta = cast(
                Tuple[str, str, Dict[str, Any]],
                self._metadata_cache.get_or_load(
                    metadata_signature,
                    lambda: _read_cli_meta(store),
                ),
            )
            updated_at, store_signature = _sqlite_store_signature(store)
            if not store_signature["db"]["exists"]:
                continue
            cwd_hash = store.parents[1].name
            extra = {
                "source": "cli",
                "store": str(store),
                "store_signature": store_signature,
                "cwd_hash": cwd_hash,
            }
            extra.update(meta)
            sessions.append(
                Session(
                    agent="cursor-cli",
                    session_id=store.parent.name,
                    project=project or "cwd-hash:{}".format(cwd_hash[:8]),
                    transcript="",
                    updated_at=updated_at,
                    title=title,
                    extra=extra,
                )
            )
        self._metadata_cache.prune(active_signatures)
        return sessions

    def normalize(self, record: Mapping[str, Any], session: Session) -> Iterable[Event]:
        message = record.get("message")
        if not isinstance(message, Mapping):
            message = {}
        role = str(record.get("role") or message.get("role") or "text")
        content = message.get("content", record.get("content"))
        timestamp = local_timestamp(record.get("timestamp") or message.get("timestamp"))
        events: List[Event] = []
        for item in content_items(content):
            item_type = item.get("type")
            if item_type == "text":
                text = snip(item.get("text"))
                if text:
                    events.append(Event(timestamp, session.agent, session.session_id, role, text))
            elif item_type == "tool_use":
                name = str(item.get("name") or "tool")
                details = snip(compact_json(item.get("input") or {}), 80)
                events.append(
                    Event(
                        timestamp,
                        session.agent,
                        session.session_id,
                        "tool_call",
                        snip("{} {}".format(name, details), 120),
                    )
                )
            elif item_type == "tool_result":
                events.append(
                    Event(
                        timestamp,
                        session.agent,
                        session.session_id,
                        "tool_result",
                        snip(text_content(item.get("content")), 80),
                    )
                )
        return events

    def infer_status(self, session: Session, now: Optional[float] = None) -> Optional[Status]:
        source = str(session.extra.get("source") or "").lower()
        if source != "cli" and session.agent.lower() != "cursor-cli":
            return None

        store_value = session.extra.get("store")
        if not isinstance(store_value, (str, Path)) or not str(store_value):
            return Status.DEAD
        store = Path(store_value).expanduser()
        if not store.is_file():
            return Status.DEAD

        signature = session.extra.get("store_signature")
        if not isinstance(signature, Mapping):
            _updated_at, signature = _sqlite_store_signature(store)
        activity_at = _sqlite_write_activity_at(signature)
        if activity_at <= 0:
            _updated_at, signature = _sqlite_store_signature(store)
            activity_at = _sqlite_write_activity_at(signature)
        if activity_at <= 0:
            return Status.DEAD

        current = time.time() if now is None else float(now)
        age = max(0.0, current - activity_at)
        if age > _CLI_IDLE_SECONDS:
            return Status.IDLE
        if age <= _CLI_WORKING_SECONDS:
            return Status.WORKING
        return Status.WAITING
