"""Cursor IDE JSONL and Cursor CLI SQLite adapter."""

from __future__ import annotations

import json
import sqlite3
import time
from functools import lru_cache
from pathlib import Path
from typing import (
    Any,
    Dict,
    Hashable,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

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
from sidecar.cursor_chat import (
    CursorChatError,
    CursorChatSnapshotBroker,
    default_snapshot_broker,
    snapshot_cursor_chat,
)
from sidecar.model import Event, Session, Status
from sidecar.text_utils import extract_cursor_title

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
    def user_texts() -> Iterable[str]:
        for record in read_jsonl_prefix(path):
            message = record.get("message")
            if not isinstance(message, Mapping):
                message = {}
            role = record.get("role") or message.get("role")
            if role != "user":
                continue
            content = message.get("content", record.get("content"))
            parts = [
                str(item.get("text") or "")
                for item in content_items(content)
                if item.get("type") == "text" and item.get("text")
            ]
            if parts:
                yield "\n".join(parts)

    return extract_cursor_title(user_texts())


def _decode_meta_value(value: Any) -> Any:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    if not isinstance(value, str):
        return value
    text = value.strip()
    candidates = [text]
    if (
        len(text) % 2 == 0
        and text
        and all(char in "0123456789abcdefABCDEF" for char in text)
    ):
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
            except (json.JSONDecodeError, RecursionError, TypeError):
                break
        if decoded != candidate:
            return decoded
    return text


def _utf8_scalar_text(value: Any) -> str:
    """Return strings containing only UTF-8-encodable Unicode scalars."""

    if not isinstance(value, str):
        return ""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    return value


def _meta_key_text(value: Any) -> str:
    """Coerce a SQLite metadata key without retaining invalid Unicode."""

    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    elif not isinstance(value, str):
        try:
            value = str(value or "")
        except (RecursionError, TypeError, ValueError):
            return ""
    return _utf8_scalar_text(value)


def _direct_string(value: Any, names: Sequence[str]) -> str:
    if not isinstance(value, Mapping):
        return ""
    lowered = {}
    for key, item in value.items():
        key_text = _utf8_scalar_text(key)
        if key_text:
            lowered[key_text.lower()] = item
    for name in names:
        item = lowered.get(name.lower())
        item_text = _utf8_scalar_text(item).strip()
        if item_text:
            return item_text
    return ""


def _read_cli_meta(path: Path) -> Tuple[str, str, Dict[str, Any]]:
    """Read checkpointed Cursor CLI metadata without touching SQLite sidecars."""

    rows: List[Tuple[Any, Any]] = []
    connection: Optional[sqlite3.Connection] = None
    try:
        # mode=ro still takes WAL read locks and can update the source -shm.
        # immutable=1 avoids all locking and sidecar access, so it intentionally
        # sees only checkpointed main-DB state.  Stale/empty metadata is safer
        # than mutating a live Cursor store to read WAL-only rows.
        uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
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
    decoded_keys: Dict[str, None] = {}
    for key, raw in rows:
        try:
            key_text = _meta_key_text(key)
            value = _decode_meta_value(raw)
            if key_text:
                decoded_keys[key_text] = None
            value_text = _utf8_scalar_text(value).strip()
            if not title and key_text.lower() == "name":
                title = value_text
            if not project and key_text.lower() in ("cwd", "project", "workspace"):
                project = value_text
            title = title or _direct_string(value, ("name", "title"))
            project = project or _direct_string(
                value, ("cwd", "projectPath", "workspacePath", "rootPath")
            )
        except (RecursionError, TypeError, ValueError, UnicodeError):
            # One malformed row must not hide metadata from valid siblings.
            continue
    title = _utf8_scalar_text(title)
    project = _utf8_scalar_text(project)
    return snip(title, 160), project, {"meta_keys": sorted(decoded_keys)}


def _read_cli_snapshot_meta(
    path: Path,
    snapshot_broker: Optional[CursorChatSnapshotBroker] = None,
) -> Tuple[str, str, Dict[str, Any]]:
    """Prefer a production chat snapshot and safely degrade for legacy stores."""

    try:
        state = snapshot_cursor_chat(path, broker=snapshot_broker)
    except CursorChatError as error:
        title, project, metadata = _read_cli_meta(path)
        fallback = dict(metadata)
        fallback.update(
            {
                "cursor_chat_snapshot": "fallback",
                # The typed class is useful diagnostics without copying an
                # exception message that could contain source-derived text.
                "cursor_chat_error": error.__class__.__name__,
            }
        )
        return title, project, fallback

    return (
        state.title,
        state.project,
        {
            "agentId": state.metadata.agent_id,
            "name": state.metadata.name,
            "mode": state.metadata.mode,
            "createdAt": state.metadata.created_at,
            "latestRoot": state.root_blob_id,
            "cursor_chat_snapshot": "production",
        },
    )


def _sqlite_store_signature(path: Path) -> Tuple[float, Dict[str, Dict[str, Any]]]:
    """Capture DB/WAL activity plus diagnostic SQLite sidecar signatures."""

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
        if name in ("db", "wal"):
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


def _bounded_extra_string(value: Any, limit: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    return snip(value, limit)


def _cursor_chat_extra(
    record: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> Dict[str, Any]:
    """Copy only bounded identifiers and follower state into event metadata."""

    extra: Dict[str, Any] = {
        "synthetic": record.get("synthetic") is True
        or metadata.get("synthetic") is True,
        "provisional": record.get("provisional") is True
        or metadata.get("provisional") is True,
    }
    for source_key, output_key, limit in (
        ("root_blob_id", "root_blob_id", 80),
        ("message_id", "message_id", 80),
        ("timestamp_source", "timestamp_source", 40),
    ):
        value = _bounded_extra_string(metadata.get(source_key), limit)
        if value:
            extra[output_key] = value
    return extra


def _cursor_chat_tool_identity(item: Mapping[str, Any]) -> Tuple[str, str]:
    call_id = ""
    for key in ("toolCallId", "tool_call_id", "callId", "id"):
        call_id = _bounded_extra_string(item.get(key), 160)
        if call_id:
            break
    name = ""
    for key in ("toolName", "tool_name", "name"):
        name = _bounded_extra_string(item.get(key), 80)
        if name:
            break
    return call_id, name


def _cursor_chat_tool_call_text(item: Mapping[str, Any], name: str) -> str:
    arguments: Any = ""
    for key in ("args", "arguments", "input"):
        if key in item:
            arguments = item.get(key)
            break
    if isinstance(arguments, str):
        details = snip(arguments, 80)
    elif arguments in (None, ""):
        details = ""
    else:
        details = snip(compact_json(arguments), 80)
    return snip("{} {}".format(name or "tool", details).strip(), 120)


def _cursor_chat_tool_result_text(
    item: Mapping[str, Any],
    call_id: str,
    name: str,
) -> str:
    for key in ("content", "result", "output"):
        if key in item:
            text = snip(text_content(item.get(key)), 80)
            if text:
                return text
            break
    if call_id or name:
        return "{} completed".format(snip(name, 68) or "tool")
    return ""


def _normalize_cursor_chat(
    record: Mapping[str, Any],
    session: Session,
    metadata: Mapping[str, Any],
) -> List[Event]:
    """Normalize one logical CursorChatFollower record block by block."""

    timestamp = local_timestamp(record.get("timestamp"), fallback=session.updated_at)
    base_extra = _cursor_chat_extra(record, metadata)
    native_type = record.get("type")
    if native_type == "session_reset" or metadata.get("kind") == "session_reset":
        text = snip(record.get("content") or "Cursor chat history reset", 120)
        return [
            Event(
                timestamp,
                session.agent,
                session.session_id,
                "session_reset",
                text,
                base_extra,
            )
        ]

    role_value = record.get("role")
    role = role_value if isinstance(role_value, str) else ""
    if role == "system":
        return []

    content = record.get("content")
    events: List[Event] = []
    for item in content_items(content):
        try:
            item_type_value = item.get("type")
            item_type = item_type_value if isinstance(item_type_value, str) else ""
            event_extra = dict(base_extra)
            call_id, tool_name = _cursor_chat_tool_identity(item)
            if call_id:
                event_extra["tool_call_id"] = call_id
            if tool_name:
                event_extra["tool_name"] = tool_name

            kind = ""
            text = ""
            if item_type in ("", "text"):
                raw_text = item.get("text")
                if role == "user":
                    kind = "user"
                    text = snip(extract_cursor_title((raw_text,)), 120)
                elif role == "assistant":
                    kind = (
                        "assistant_update" if base_extra["provisional"] else "assistant"
                    )
                    text = snip(raw_text, 120)
                elif role == "tool":
                    kind = "tool_result"
                    text = snip(raw_text, 80)
            elif item_type == "reasoning":
                kind = "thinking"
                text = snip(item.get("text") or item.get("reasoning"), 80)
            elif item_type in ("tool-call", "tool_use"):
                kind = "tool_call"
                text = _cursor_chat_tool_call_text(item, tool_name)
            elif item_type in ("tool-result", "tool_result"):
                kind = "tool_result"
                text = _cursor_chat_tool_result_text(item, call_id, tool_name)

            if kind and text:
                events.append(
                    Event(
                        timestamp,
                        session.agent,
                        session.session_id,
                        kind,
                        text,
                        event_extra,
                    )
                )
        except (RecursionError, TypeError, ValueError):
            # A malformed native block must not hide valid siblings.
            continue
    return events


class CursorAdapter(Adapter):
    name = "cursor"
    agent_names = ("cursor", "cursor-ide", "cursor-cli")

    def __init__(
        self,
        metadata_cache_size: int = DEFAULT_METADATA_CACHE_SIZE,
        snapshot_broker: Optional[CursorChatSnapshotBroker] = None,
    ) -> None:
        self._metadata_cache: MetadataCache[_CursorMetadata] = MetadataCache(
            metadata_cache_size
        )
        self._snapshot_broker = (
            snapshot_broker
            if snapshot_broker is not None
            else default_snapshot_broker()
        )

    def discover(self, home: Path) -> Iterable[Session]:
        with self._snapshot_broker.scan_generation():
            return self._discover(home)

    def _discover(self, home: Path) -> Iterable[Session]:
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
                    (
                        parent
                        for parent in transcript.parents
                        if parent.name == "agent-transcripts"
                    ),
                    None,
                )
                if transcript_root is None:
                    continue
                project = decode_cursor_project_slug(transcript_root.parent.name)
                parent_id = (
                    transcript.parent.parent.name
                    if transcript.parent.name == "subagents"
                    else None
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
            wal = Path(str(store) + "-wal")
            wal_signature = file_signature(wal)
            try:
                metadata_signature: Hashable = self._snapshot_broker.cache_key(store)
                self._snapshot_broker.pin(metadata_signature)
            except CursorChatError:
                metadata_signature = (db_signature, wal_signature)
            active_signatures.append(metadata_signature)
            title, project, meta = cast(
                Tuple[str, str, Dict[str, Any]],
                self._metadata_cache.get_or_load(
                    metadata_signature,
                    lambda: _read_cli_snapshot_meta(
                        store,
                        self._snapshot_broker,
                    ),
                ),
            )
            updated_at, store_signature = _sqlite_store_signature(store)
            if not store_signature["db"]["exists"]:
                continue
            cwd_hash = store.parents[1].name
            directory_session_id = store.parent.name
            extra = {
                "source": "cli",
                "store": str(store),
                "wal": str(wal),
                "store_signature": store_signature,
                "db_signature": dict(store_signature["db"]),
                "wal_signature": dict(store_signature["wal"]),
                "cwd_hash": cwd_hash,
                "directory_session_id": directory_session_id,
                "transcript_kind": "cursor-chat-sqlite",
            }
            extra.update(meta)
            agent_id = extra.get("agentId")
            if isinstance(agent_id, str) and agent_id:
                matches_directory = agent_id == directory_session_id
                extra["agent_id_matches_directory"] = matches_directory
                extra["session_id_mismatch"] = not matches_directory
            sessions.append(
                Session(
                    agent="cursor-cli",
                    session_id=directory_session_id,
                    project=project or "cwd-hash:{}".format(cwd_hash[:8]),
                    transcript=str(store),
                    updated_at=updated_at,
                    title=title,
                    extra=extra,
                )
            )
        self._metadata_cache.prune(active_signatures)
        return sessions

    def normalize(self, record: Mapping[str, Any], session: Session) -> Iterable[Event]:
        cursor_chat_value = record.get("_cursor_chat")
        cursor_chat_metadata = (
            cursor_chat_value if isinstance(cursor_chat_value, Mapping) else {}
        )
        if (
            cursor_chat_metadata.get("source") == "cursor-cli"
            or session.extra.get("transcript_kind") == "cursor-chat-sqlite"
        ):
            return _normalize_cursor_chat(record, session, cursor_chat_metadata)

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
                    events.append(
                        Event(timestamp, session.agent, session.session_id, role, text)
                    )
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

    def infer_status(
        self, session: Session, now: Optional[float] = None
    ) -> Optional[Status]:
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
