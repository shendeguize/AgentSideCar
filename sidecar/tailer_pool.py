"""Tailer lifecycle, checkpoint retention, and bounded event polling."""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    Mapping,
    Optional,
    Set,
    Tuple,
)

from sidecar.index import SessionKey, session_key
from sidecar.model import Event, Session, Status
from sidecar.tail import SessionTailer

DEFAULT_TAIL_RECENT_SECONDS = 48.0 * 60.0 * 60.0
DEFAULT_EVENT_POLLS = 32

TailerFactory = Callable[..., Any]
EventPublisher = Callable[[Any], None]
Checkpoint = Tuple[str, Mapping[str, object]]


@dataclass(frozen=True)
class TailerPoolState:
    """Read-only lifecycle state exposed for diagnostics and focused tests."""

    known: FrozenSet[SessionKey]
    active: FrozenSet[SessionKey]
    checkpoints: FrozenSet[SessionKey]
    pending: FrozenSet[SessionKey]


class TailerPool:
    """Own session tailers and preserve cursors across temporary expiry.

    ``refresh`` is intentionally serialized by its caller. A pool has no
    internal locking because the daemon gives scanning a single owner.
    """

    def __init__(
        self,
        publisher: Any,
        *,
        tail_recent_seconds: float = DEFAULT_TAIL_RECENT_SECONDS,
        max_event_polls: int = DEFAULT_EVENT_POLLS,
        tailer_factory: Optional[TailerFactory] = None,
    ) -> None:
        if tail_recent_seconds < 0 or max_event_polls <= 0:
            raise ValueError("tail bounds are invalid")
        self._publish = self._publisher_callable(publisher)
        self.tail_recent_seconds = float(tail_recent_seconds)
        self.max_event_polls = int(max_event_polls)
        self.tailer_factory = tailer_factory
        self._known_keys: Set[SessionKey] = set()
        self._tailers: Dict[SessionKey, Any] = {}
        self._paths: Dict[SessionKey, str] = {}
        self._checkpoints: Dict[SessionKey, Checkpoint] = {}
        self._pending: Set[SessionKey] = set()

    @staticmethod
    def _publisher_callable(publisher: Any) -> EventPublisher:
        publish = getattr(publisher, "publish", None)
        if callable(publish):
            return publish
        if callable(publisher):
            return publisher
        raise TypeError("publisher must be callable or expose publish()")

    @property
    def state(self) -> TailerPoolState:
        """Return an immutable view without exposing tailer instances."""

        return TailerPoolState(
            known=frozenset(self._known_keys),
            active=frozenset(self._tailers),
            checkpoints=frozenset(self._checkpoints),
            pending=frozenset(self._pending),
        )

    def reset(self) -> None:
        """Discard all lifecycle state before a fresh daemon run."""

        self._known_keys.clear()
        self._tailers.clear()
        self._paths.clear()
        self._checkpoints.clear()
        self._pending.clear()

    @staticmethod
    def supports_tailing(session: Session) -> bool:
        """Return whether the session has a supported transcript source."""

        transcript = session.transcript
        if not transcript:
            return False
        suffix = Path(transcript).suffix.lower()
        if session.extra.get("transcript_kind") == "cursor-chat-sqlite":
            return suffix in (".db", ".sqlite", ".sqlite3")
        if session.agent == "dsh":
            return True
        return suffix == ".jsonl"

    def should_tail(self, session: Session, now: Optional[float] = None) -> bool:
        """Apply active-status and recency policy to a supported session."""

        if not self.supports_tailing(session):
            return False
        if session.status in (Status.WORKING, Status.WAITING):
            return True
        return session.age_seconds(now) <= self.tail_recent_seconds

    def refresh(
        self,
        sessions: Iterable[Session],
        *,
        changed_keys: Iterable[SessionKey],
        initial: bool = False,
        now: Optional[float] = None,
    ) -> bool:
        """Reconcile one complete session snapshot and poll bounded work.

        Missing keys are real removals and lose checkpoints. Sessions that
        merely age outside the recency window retain a checkpoint for resume.
        Snapshot/replay tailers are polled once per refresh; JSONL tailers may
        drain up to ``max_event_polls`` bounded batches.
        """

        current = {session_key(session): session for session in sessions}
        current_keys = set(current)
        changed = set(changed_keys).intersection(current_keys)
        added = current_keys.difference(self._known_keys)
        removed = self._known_keys.difference(current_keys)

        for key in removed:
            self._drop(key)

        current_time = time.time() if now is None else now
        expired = False
        for key in tuple(self._tailers):
            session = current.get(key)
            if session is None:
                expired = self._drop(key) or expired
            elif not self.should_tail(session, current_time):
                expired = self._drop(key, retain_checkpoint=True) or expired

        for key in sorted(changed):
            self._update(
                key,
                current[key],
                initial=initial,
                new_session=key in added,
                now=current_time,
            )

        pending_before = set(self._pending)
        for key in sorted(pending_before.difference(changed)):
            tailer = self._tailers.get(key)
            if tailer is None or not self._poll(tailer):
                self._pending.discard(key)

        self._known_keys = current_keys
        return bool(changed or removed or pending_before or expired)

    def _drop(
        self,
        key: SessionKey,
        *,
        retain_checkpoint: bool = False,
    ) -> bool:
        existed = (
            key in self._tailers
            or key in self._paths
            or key in self._checkpoints
            or key in self._pending
        )
        tailer = self._tailers.get(key)
        if retain_checkpoint and tailer is not None:
            self._checkpoints.pop(key, None)
            exporter = getattr(tailer, "export_checkpoint", None)
            if callable(exporter):
                try:
                    checkpoint = exporter()
                except Exception:
                    checkpoint = None
                path = self._paths.get(key)
                if path is not None and isinstance(checkpoint, Mapping):
                    self._checkpoints[key] = (path, checkpoint)
        elif not retain_checkpoint:
            self._checkpoints.pop(key, None)
        self._tailers.pop(key, None)
        self._paths.pop(key, None)
        self._pending.discard(key)
        return existed

    def _capture_initial_checkpoint(
        self,
        key: SessionKey,
        session: Session,
    ) -> None:
        """Save an EOF/sequence cursor for a startup-expired session."""

        self._checkpoints.pop(key, None)
        if not self.supports_tailing(session):
            return
        if session.agent == "dsh":
            seq = session.extra.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool):
                return
        else:
            try:
                if not Path(session.transcript).is_file():
                    return
            except OSError:
                return

        tailer = None
        checkpoint = None
        try:
            tailer = self._make_tailer(session, from_start=False)
            exporter = getattr(tailer, "export_checkpoint", None)
            if callable(exporter):
                checkpoint = exporter()
        except Exception:
            return
        finally:
            if tailer is not None:
                closer = getattr(tailer, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:
                        pass

        if isinstance(checkpoint, Mapping):
            self._checkpoints[key] = (session.transcript, checkpoint)

    def _make_tailer(self, session: Session, from_start: bool) -> Any:
        if self.tailer_factory is None:
            return SessionTailer(session, from_start=from_start)

        try:
            parameters = inspect.signature(self.tailer_factory).parameters.values()
        except (TypeError, ValueError):
            return self.tailer_factory(session, from_start=from_start)
        accepts_keyword = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            or (
                parameter.name == "from_start"
                and parameter.kind
                in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                )
            )
            for parameter in parameters
        )
        if accepts_keyword:
            return self.tailer_factory(session, from_start=from_start)
        return self.tailer_factory(session)

    def _make_resumable_tailer(
        self,
        session: Session,
        from_start: bool,
        checkpoint: Optional[Mapping[str, object]],
    ) -> Tuple[Any, bool]:
        """Build a tailer and restore without a duplicate Cursor snapshot."""

        if (
            self.tailer_factory is None
            and checkpoint is not None
            and session.extra.get("transcript_kind") == "cursor-chat-sqlite"
        ):
            return (
                SessionTailer(
                    session,
                    from_start=from_start,
                    _initial_checkpoint=checkpoint,
                ),
                True,
            )
        return self._make_tailer(session, from_start), False

    @staticmethod
    def _has_unread_data(tailer: Any) -> bool:
        pending = getattr(tailer, "has_pending_records", False)
        try:
            if bool(pending() if callable(pending) else pending):
                return True
        except Exception:
            pass

        follower = getattr(tailer, "follower", None)
        path = getattr(follower, "path", None)
        offset = getattr(follower, "offset", None)
        if path is None or not isinstance(offset, int):
            return False
        try:
            return Path(path).stat().st_size > offset
        except OSError:
            return False

    def _poll(self, tailer: Any) -> bool:
        try:
            single_poll = bool(
                getattr(tailer, "single_poll_per_refresh", False)
            )
        except Exception:
            single_poll = False
        poll_limit = 1 if single_poll else self.max_event_polls
        for _ in range(poll_limit):
            try:
                events = tailer.poll()
            except Exception:
                return False
            emitted = False
            for event in events or ():
                if isinstance(event, (Event, Mapping)):
                    self._publish(event)
                    emitted = True
            if single_poll:
                return self._has_unread_data(tailer)
            if not emitted and not self._has_unread_data(tailer):
                return False
        return True

    def _update(
        self,
        key: SessionKey,
        session: Session,
        *,
        initial: bool,
        new_session: bool,
        now: Optional[float],
    ) -> None:
        if not self.should_tail(session, now):
            if initial:
                self._capture_initial_checkpoint(key, session)
            else:
                self._drop(key, retain_checkpoint=True)
            return

        tailer = self._tailers.get(key)
        if tailer is not None and self._paths.get(key) != session.transcript:
            tailer = None
            self._drop(key)
        if tailer is None:
            saved_checkpoint = self._checkpoints.get(key)
            checkpoint = None
            if saved_checkpoint is not None:
                checkpoint_path, checkpoint_value = saved_checkpoint
                if checkpoint_path == session.transcript:
                    checkpoint = checkpoint_value
                else:
                    self._checkpoints.pop(key, None)
            try:
                tailer, checkpoint_consumed = self._make_resumable_tailer(
                    session,
                    from_start=bool(new_session and not initial),
                    checkpoint=checkpoint,
                )
            except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                return
            if checkpoint is not None and not checkpoint_consumed:
                restore = getattr(tailer, "restore_checkpoint", None)
                if callable(restore):
                    try:
                        restore(checkpoint)
                    except Exception:
                        pass
            self._checkpoints.pop(key, None)
            self._tailers[key] = tailer
            self._paths[key] = session.transcript
        elif hasattr(tailer, "session"):
            tailer.session = session

        if not initial:
            if self._poll(tailer):
                self._pending.add(key)
            else:
                self._pending.discard(key)


__all__ = [
    "DEFAULT_EVENT_POLLS",
    "DEFAULT_TAIL_RECENT_SECONDS",
    "EventPublisher",
    "TailerFactory",
    "TailerPool",
    "TailerPoolState",
]
