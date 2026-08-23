"""Failure-isolated discovery and state application."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Tuple, Union

from sidecar.adapters.base import Adapter
from sidecar.model import Session, Status
from sidecar.state import StateEngine

AdapterSource = Union[Iterable[Adapter], Mapping[str, Adapter]]


@dataclass(frozen=True)
class ScanError:
    """A recoverable adapter or state error observed during one scan."""

    adapter: str
    stage: str
    message: str
    exception_type: str
    session_id: Optional[str] = None


def _adapter_name(adapter: Adapter) -> str:
    name = getattr(adapter, "name", "")
    return str(name or adapter.__class__.__name__)


def _adapter_agent_names(adapter: Adapter) -> Tuple[str, ...]:
    names = (_adapter_name(adapter),) + tuple(getattr(adapter, "agent_names", ()) or ())
    return tuple(dict.fromkeys(str(name) for name in names if str(name)))


def _unique_adapters(adapters: AdapterSource) -> Tuple[Adapter, ...]:
    values: Iterable[Adapter]
    if isinstance(adapters, Mapping):
        values = adapters.values()
    else:
        values = adapters
    unique: List[Adapter] = []
    seen = set()
    for adapter in values:
        identity = id(adapter)
        if identity not in seen:
            seen.add(identity)
            unique.append(adapter)
    return tuple(unique)


class Scanner:
    """Discover sessions independently, then classify and order them."""

    def __init__(
        self,
        adapters: Optional[AdapterSource] = None,
        state_engine: Optional[StateEngine] = None,
        home: Optional[Path] = None,
    ) -> None:
        if adapters is None:
            from sidecar.adapters import iter_adapters

            self.adapters = iter_adapters()
        else:
            self.adapters = _unique_adapters(adapters)
        self.home = Path.home() if home is None else Path(home).expanduser()
        self.state_engine = state_engine or StateEngine(home=self.home)
        self.errors: List[ScanError] = []
        self.failed_agent_names: Tuple[str, ...] = ()

    def _record_error(
        self,
        errors: List[ScanError],
        adapter: Adapter,
        stage: str,
        error: Exception,
        session_id: Optional[str] = None,
    ) -> None:
        errors.append(
            ScanError(
                adapter=_adapter_name(adapter),
                stage=stage,
                message=str(error) or error.__class__.__name__,
                exception_type=error.__class__.__name__,
                session_id=session_id,
            )
        )

    def scan(
        self,
        recent_seconds: Optional[float] = None,
        now: Optional[float] = None,
        recent: Optional[float] = None,
    ) -> List[Session]:
        """Return newest-first sessions; each call obtains its own current time."""

        current = time.time() if now is None else float(now)
        if recent is not None:
            if recent_seconds is not None:
                raise ValueError("provide only one of recent or recent_seconds")
            recent_seconds = recent
        if recent_seconds is not None and recent_seconds < 0:
            raise ValueError("recent_seconds must be non-negative")

        errors: List[ScanError] = []
        failed_agent_names = set()
        deduplicated = {}

        for adapter in self.adapters:
            try:
                discovered = adapter.discover(self.home)
                iterator = iter(discovered)
            except Exception as error:
                self._record_error(errors, adapter, "discover", error)
                failed_agent_names.update(_adapter_agent_names(adapter))
                continue

            while True:
                try:
                    session = next(iterator)
                except StopIteration:
                    break
                except Exception as error:
                    self._record_error(errors, adapter, "discover", error)
                    failed_agent_names.update(_adapter_agent_names(adapter))
                    break

                if not isinstance(session, Session):
                    self._record_error(
                        errors,
                        adapter,
                        "discover",
                        TypeError("adapter discover() yielded a non-Session value"),
                    )
                    failed_agent_names.update(_adapter_agent_names(adapter))
                    continue
                if (
                    recent_seconds is not None
                    and session.age_seconds(current) > recent_seconds
                ):
                    continue

                def state_error(stage: str, error: Exception) -> None:
                    self._record_error(
                        errors,
                        adapter,
                        stage,
                        error,
                        session_id=session.session_id,
                    )

                try:
                    self.state_engine.apply(
                        session,
                        adapter=adapter,
                        now=current,
                        on_error=state_error,
                    )
                except Exception as error:
                    self._record_error(
                        errors,
                        adapter,
                        "state",
                        error,
                        session_id=session.session_id,
                    )
                    session.status = Status.DEAD

                key = (session.agent, session.session_id)
                previous = deduplicated.get(key)
                if previous is None or session.updated_at > previous.updated_at:
                    deduplicated[key] = session

        self.errors = errors
        self.failed_agent_names = tuple(sorted(failed_agent_names))
        return sorted(
            deduplicated.values(),
            key=lambda session: (-session.updated_at, session.agent, session.session_id),
        )

__all__ = ["ScanError", "Scanner"]
