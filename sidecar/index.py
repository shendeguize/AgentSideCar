"""Incremental session metadata and source-signature index."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, Mapping, Optional, Set, Tuple

from sidecar.model import Session

SessionKey = Tuple[str, str]

_SOURCE_KEYS = (
    "state",
    "store",
    "workspace",
)


@dataclass(frozen=True)
class FileSignature:
    """The inexpensive identity used to invalidate one source file."""

    path: str
    exists: bool
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class IndexEntry:
    """Cached metadata and signatures for a discovered session."""

    session: Session
    sources: Tuple[FileSignature, ...]
    metadata: str


@dataclass(frozen=True)
class IndexDelta:
    """Keys affected by one index update."""

    changed: FrozenSet[SessionKey]
    removed: FrozenSet[SessionKey]
    unchanged: FrozenSet[SessionKey]


def session_key(session: Session) -> SessionKey:
    return (session.agent, session.session_id)


def _path_signature(path: Path) -> FileSignature:
    rendered = str(path.expanduser())
    try:
        stat_result = path.expanduser().stat()
    except OSError:
        return FileSignature(rendered, False, 0, 0)
    return FileSignature(
        rendered,
        True,
        int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1e9))),
        int(stat_result.st_size),
    )


def _source_paths(session: Session) -> Tuple[Path, ...]:
    paths = []
    if session.transcript:
        paths.append(Path(session.transcript))
    for key in _SOURCE_KEYS:
        value = session.extra.get(key)
        if isinstance(value, (str, os.PathLike)) and str(value):
            paths.append(Path(value))

    unique = {}
    for path in paths:
        expanded = path.expanduser()
        unique[str(expanded)] = expanded
    return tuple(unique[name] for name in sorted(unique))


def source_signatures(session: Session) -> Tuple[FileSignature, ...]:
    """Return transcript/state/store signatures available for ``session``."""

    return tuple(_path_signature(path) for path in _source_paths(session))


def _metadata_signature(session: Session) -> str:
    return json.dumps(
        session.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


class IncrementalIndex:
    """Cache sessions and expose precise changed/removed key sets."""

    def __init__(self) -> None:
        self._entries: Dict[SessionKey, IndexEntry] = {}

    @property
    def entries(self) -> Mapping[SessionKey, IndexEntry]:
        return dict(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def get(self, key: SessionKey) -> Optional[Session]:
        entry = self._entries.get(key)
        return None if entry is None else entry.session

    def keys(self) -> FrozenSet[SessionKey]:
        return frozenset(self._entries)

    def sessions(self) -> Tuple[Session, ...]:
        return tuple(
            entry.session
            for entry in sorted(
                self._entries.values(),
                key=lambda item: (
                    -item.session.updated_at,
                    item.session.agent,
                    item.session.session_id,
                ),
            )
        )

    def update(self, sessions: Iterable[Session]) -> IndexDelta:
        """Replace the current snapshot and report invalidated keys."""

        latest: Dict[SessionKey, Session] = {}
        for session in sessions:
            if not isinstance(session, Session):
                raise TypeError("incremental index accepts Session values")
            key = session_key(session)
            previous = latest.get(key)
            if previous is None or session.updated_at > previous.updated_at:
                latest[key] = session

        next_entries: Dict[SessionKey, IndexEntry] = {}
        changed: Set[SessionKey] = set()
        unchanged: Set[SessionKey] = set()
        for key, session in latest.items():
            entry = IndexEntry(
                session=session,
                sources=source_signatures(session),
                metadata=_metadata_signature(session),
            )
            previous = self._entries.get(key)
            if (
                previous is None
                or previous.sources != entry.sources
                or previous.metadata != entry.metadata
            ):
                changed.add(key)
            else:
                unchanged.add(key)
            next_entries[key] = entry

        removed = set(self._entries).difference(next_entries)
        self._entries = next_entries
        return IndexDelta(
            changed=frozenset(changed),
            removed=frozenset(removed),
            unchanged=frozenset(unchanged),
        )

    def clear(self) -> FrozenSet[SessionKey]:
        removed = frozenset(self._entries)
        self._entries.clear()
        return removed


__all__ = [
    "FileSignature",
    "IncrementalIndex",
    "IndexDelta",
    "IndexEntry",
    "SessionKey",
    "session_key",
    "source_signatures",
]
