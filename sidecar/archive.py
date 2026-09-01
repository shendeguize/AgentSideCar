"""Observation-level session archiving.

Archiving hides an idle or dead session from the board and from ``list``
without touching the vendor transcript, the vendor session directory, or any
running process. The registry is owned entirely by the sidecar, so an archive
decision is always reversible: a session that produces new transcript activity
after it was archived is released automatically on the next scan.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from sidecar.model import Status
from sidecar.presentation import row_value

ARCHIVE_FILE_NAME = "archive.json"
ARCHIVE_SCHEMA_VERSION = 1
MAX_ARCHIVE_ENTRIES = 4096

DEFAULT_IDLE_THRESHOLD_SECONDS = 2.0 * 60.0 * 60.0
DEFAULT_AUTO_THRESHOLD_SECONDS = 24.0 * 60.0 * 60.0
MIN_THRESHOLD_SECONDS = 60.0
MAX_THRESHOLD_SECONDS = 365.0 * 24.0 * 60.0 * 60.0

ARCHIVABLE_STATUSES: Tuple[str, ...] = (Status.IDLE.value, Status.DEAD.value)
ARCHIVE_REASONS = frozenset(("manual", "batch", "auto"))

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_DURATION_UNITS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

Target = Tuple[str, str]


class ArchiveError(RuntimeError):
    """A stable archive-registry failure."""

    _MESSAGES = {
        "archive_corrupt": "archive registry is corrupt",
        "archive_error": "archive registry could not be updated",
        "invalid_target": "archive target must carry an agent and a session ID",
        "invalid_threshold": "archive threshold is invalid",
        "invalid_status": "archive status filter is invalid",
    }

    def __init__(self, code: str) -> None:
        if code not in self._MESSAGES:
            raise ValueError("invalid archive error code")
        self.code = code
        super().__init__(self._MESSAGES[code])


def parse_duration(value: object) -> float:
    """Parse ``90``/``30m``/``2h``/``7d`` into bounded seconds."""

    if isinstance(value, bool):
        raise ArchiveError("invalid_threshold")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        match = _DURATION_RE.match(value)
        if match is None:
            raise ArchiveError("invalid_threshold")
        seconds = float(match.group(1)) * _DURATION_UNITS[match.group(2).lower()]
    else:
        raise ArchiveError("invalid_threshold")
    if not MIN_THRESHOLD_SECONDS <= seconds <= MAX_THRESHOLD_SECONDS:
        raise ArchiveError("invalid_threshold")
    return seconds


def normalize_statuses(values: Optional[Iterable[object]]) -> Tuple[str, ...]:
    """Return a validated archivable-status filter, defaulting to idle+dead."""

    if values is None:
        return ARCHIVABLE_STATUSES
    names: List[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ArchiveError("invalid_status")
        name = value.strip().lower()
        if name not in ARCHIVABLE_STATUSES:
            raise ArchiveError("invalid_status")
        if name not in names:
            names.append(name)
    if not names:
        raise ArchiveError("invalid_status")
    return tuple(names)


def session_target(row: object) -> Target:
    """Return the ``(agent, session_id)`` identity of a session row."""

    agent = row_value(row, "agent", "")
    session_id = row_value(row, "session_id", "")
    if not isinstance(agent, str) or not isinstance(session_id, str):
        raise ArchiveError("invalid_target")
    if not agent or not session_id:
        raise ArchiveError("invalid_target")
    return agent, session_id


def normalize_targets(values: Iterable[object]) -> Tuple[Target, ...]:
    """Return de-duplicated ``(agent, session_id)`` pairs preserving order."""

    targets: List[Target] = []
    seen: Set[Target] = set()
    for value in values:
        if isinstance(value, (tuple, list)) and len(value) == 2:
            agent, session_id = value
            if not isinstance(agent, str) or not isinstance(session_id, str):
                raise ArchiveError("invalid_target")
            if not agent or not session_id:
                raise ArchiveError("invalid_target")
            target = (agent, session_id)
        else:
            target = session_target(value)
        if target not in seen:
            seen.add(target)
            targets.append(target)
    return tuple(targets)


def _row_status(row: object) -> str:
    value = row_value(row, "status", "")
    if isinstance(value, Status):
        return value.value
    return str(value or "")


def _row_updated_at(row: object) -> float:
    try:
        return float(row_value(row, "updated_at", 0.0))
    except (TypeError, ValueError):
        return 0.0


def select_archivable(
    rows: Iterable[object],
    *,
    idle_seconds: float,
    statuses: Optional[Iterable[object]] = None,
    now: Optional[float] = None,
) -> List[object]:
    """Return rows idle for at least ``idle_seconds`` in an eligible status."""

    threshold = parse_duration(idle_seconds)
    allowed = normalize_statuses(statuses)
    current = time.time() if now is None else float(now)
    selected: List[object] = []
    for row in rows:
        if _row_status(row) not in allowed:
            continue
        if current - _row_updated_at(row) < threshold:
            continue
        selected.append(row)
    return selected


@dataclass(frozen=True)
class ArchiveEntry:
    """One archived session identity and the decision that produced it."""

    agent: str
    session_id: str
    archived_at: float
    reason: str

    @property
    def target(self) -> Target:
        return self.agent, self.session_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent": self.agent,
            "session_id": self.session_id,
            "archived_at": self.archived_at,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ArchiveEntry":
        if not isinstance(value, Mapping):
            raise ArchiveError("archive_corrupt")
        agent = value.get("agent")
        session_id = value.get("session_id")
        archived_at = value.get("archived_at")
        reason = value.get("reason")
        if (
            not isinstance(agent, str)
            or not agent
            or not isinstance(session_id, str)
            or not session_id
            or isinstance(archived_at, bool)
            or not isinstance(archived_at, (int, float))
            or archived_at < 0
            or not isinstance(reason, str)
            or reason not in ARCHIVE_REASONS
        ):
            raise ArchiveError("archive_corrupt")
        return cls(
            agent=agent,
            session_id=session_id,
            archived_at=float(archived_at),
            reason=reason,
        )


@dataclass(frozen=True)
class ArchiveView:
    """The result of applying the registry to one freshly scanned snapshot."""

    visible: List[object]
    archived: List[Dict[str, Any]]
    released: Tuple[Target, ...]


def default_archive_dir() -> Path:
    """Resolve the runtime directory without importing the daemon module."""

    configured = os.environ.get("AGENT_SIDECAR_RUNTIME_DIR") or os.environ.get(
        "AGENT_SIDECAR_HOME"
    )
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))
    return Path.home() / ".agent_sidecar"


class ArchiveStore:
    """A small, atomically replaced JSON registry of archived sessions."""

    def __init__(
        self,
        runtime_dir: Optional[os.PathLike] = None,
        *,
        path: Optional[os.PathLike] = None,
    ) -> None:
        if path is not None:
            self.path = Path(path).expanduser()
        else:
            root = (
                default_archive_dir()
                if runtime_dir is None
                else Path(runtime_dir).expanduser()
            )
            self.path = root / ARCHIVE_FILE_NAME

    def entries(self) -> Tuple[ArchiveEntry, ...]:
        """Return the persisted entries, newest decision last."""

        return self._load()

    def is_archived(self, agent: str, session_id: str) -> bool:
        return any(
            entry.target == (agent, session_id) for entry in self._load()
        )

    def archive(
        self,
        targets: Iterable[object],
        *,
        reason: str = "manual",
        now: Optional[float] = None,
    ) -> Tuple[ArchiveEntry, ...]:
        """Archive every target, returning the entries that were newly added."""

        if reason not in ARCHIVE_REASONS:
            raise ArchiveError("archive_error")
        wanted = normalize_targets(targets)
        if not wanted:
            return ()
        current = time.time() if now is None else float(now)
        existing = list(self._load())
        known = {entry.target for entry in existing}
        added = tuple(
            ArchiveEntry(
                agent=agent,
                session_id=session_id,
                archived_at=current,
                reason=reason,
            )
            for agent, session_id in wanted
            if (agent, session_id) not in known
        )
        if not added:
            return ()
        self._store(existing + list(added))
        return added

    def unarchive(self, targets: Iterable[object]) -> Tuple[Target, ...]:
        """Remove every listed target, returning the ones that were present."""

        wanted = set(normalize_targets(targets))
        if not wanted:
            return ()
        existing = list(self._load())
        remaining = [entry for entry in existing if entry.target not in wanted]
        removed = tuple(
            entry.target for entry in existing if entry.target in wanted
        )
        if not removed:
            return ()
        self._store(remaining)
        return removed

    def unarchive_all(self) -> Tuple[Target, ...]:
        existing = self._load()
        if not existing:
            return ()
        self._store([])
        return tuple(entry.target for entry in existing)

    def partition(
        self,
        rows: Iterable[object],
        *,
        now: Optional[float] = None,
    ) -> ArchiveView:
        """Split a snapshot into visible and archived rows.

        An entry whose session shows transcript activity newer than the
        archive decision is released here, so reviving an archived agent
        never requires an explicit unarchive.
        """

        materialized = list(rows)
        entries = self._load()
        if not entries:
            return ArchiveView(visible=materialized, archived=[], released=())

        indexed = {entry.target: entry for entry in entries}
        released: List[Target] = []
        visible: List[object] = []
        archived: List[Dict[str, Any]] = []
        for row in materialized:
            try:
                target = session_target(row)
            except ArchiveError:
                visible.append(row)
                continue
            entry = indexed.get(target)
            if entry is None:
                visible.append(row)
                continue
            if _row_updated_at(row) > entry.archived_at:
                released.append(target)
                visible.append(row)
                continue
            archived.append(self._archived_row(row, entry))

        if released:
            self._store(
                [entry for entry in entries if entry.target not in set(released)]
            )
        return ArchiveView(
            visible=visible,
            archived=archived,
            released=tuple(released),
        )

    @staticmethod
    def _archived_row(row: object, entry: ArchiveEntry) -> Dict[str, Any]:
        converter = getattr(row, "to_dict", None)
        if callable(converter):
            payload = dict(converter())
        elif isinstance(row, Mapping):
            payload = dict(row)
        else:
            payload = {"agent": entry.agent, "session_id": entry.session_id}
        payload["archived_at"] = entry.archived_at
        payload["archive_reason"] = entry.reason
        return payload

    def _load(self) -> Tuple[ArchiveEntry, ...]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise ArchiveError("archive_error") from error
        if not raw.strip():
            return ()
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ArchiveError("archive_corrupt") from error
        if (
            not isinstance(document, Mapping)
            or document.get("schema_version") != ARCHIVE_SCHEMA_VERSION
        ):
            raise ArchiveError("archive_corrupt")
        rows = document.get("entries")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise ArchiveError("archive_corrupt")
        if len(rows) > MAX_ARCHIVE_ENTRIES:
            raise ArchiveError("archive_corrupt")
        entries: List[ArchiveEntry] = []
        seen: Set[Target] = set()
        for row in rows:
            entry = ArchiveEntry.from_mapping(row)
            if entry.target in seen:
                raise ArchiveError("archive_corrupt")
            seen.add(entry.target)
            entries.append(entry)
        return tuple(entries)

    def _store(self, entries: Sequence[ArchiveEntry]) -> None:
        retained = list(entries)
        if len(retained) > MAX_ARCHIVE_ENTRIES:
            retained.sort(key=lambda entry: entry.archived_at)
            retained = retained[-MAX_ARCHIVE_ENTRIES:]
        document = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "entries": [entry.to_dict() for entry in retained],
        }
        payload = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                prefix=".archive-",
                suffix=".tmp",
                dir=str(directory),
            )
            try:
                os.fchmod(handle, stat.S_IRUSR | stat.S_IWUSR)
                with os.fdopen(handle, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError as error:
            raise ArchiveError("archive_error") from error


__all__ = [
    "ARCHIVABLE_STATUSES",
    "ARCHIVE_FILE_NAME",
    "ARCHIVE_REASONS",
    "ARCHIVE_SCHEMA_VERSION",
    "DEFAULT_AUTO_THRESHOLD_SECONDS",
    "DEFAULT_IDLE_THRESHOLD_SECONDS",
    "MAX_ARCHIVE_ENTRIES",
    "MAX_THRESHOLD_SECONDS",
    "MIN_THRESHOLD_SECONDS",
    "ArchiveEntry",
    "ArchiveError",
    "ArchiveStore",
    "ArchiveView",
    "default_archive_dir",
    "normalize_statuses",
    "normalize_targets",
    "parse_duration",
    "select_archivable",
    "session_target",
]
