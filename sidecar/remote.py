"""Public remote facade and deterministic fleet aggregation."""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from sidecar.process_runner import (
    bounded_execution_signal_guard as _bounded_execution_signal_guard,
)
from sidecar.remote_inventory import load_remote_hosts
from sidecar.remote_transport import (
    REMOTE_BOOTSTRAP,
    REMOTE_PYTHON_CANDIDATES,
    _validated_probe_candidates,
    build_zipapp_bytes,
    execute_remote_host,
    probe_remote_python,
    remote_shell_command,
    ssh_argv,
)
from sidecar.remote_types import (
    DEFAULT_MAX_WORKERS,
    ELIGIBLE_PHASES,
    EXIT_INVALID_INVENTORY,
    EXIT_NO_SUCCESS,
    EXIT_OK,
    FAILURE_CODES,
    FLEET_TIMEOUT_SECONDS,
    HOST_TIMEOUT_SECONDS,
    MAX_AGGREGATE_BYTES,
    MAX_ARTIFACT_BYTES,
    MAX_HOSTS,
    MAX_INVENTORY_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_ITEMS,
    MAX_JSON_STRING_BYTES,
    MAX_PROTOCOL_BYTES,
    MAX_RECENT_SECONDS,
    MAX_ROWS,
    MAX_SESSION_TIMESTAMP,
    MAX_STDERR_BYTES,
    MAX_WORKERS,
    MIN_SESSION_TIMESTAMP,
    PROBE_TIMEOUT_SECONDS,
    ProtocolResourceLimitError,
    RemoteAggregate,
    RemoteFailure,
    RemoteHost,
    RemoteInventoryError,
    _command_arguments,
    _encoded_row,
    _parse_bounded_json_with_limits,
    _validate_alias,
    validate_remote_python_executable,
)
from sidecar.remote_watch import RemoteWatchSession
from sidecar.remote_watch_transport import (
    REMOTE_WATCH_BOOTSTRAP,
    open_remote_watch_host,
    remote_watch_shell_command,
    remote_watch_ssh_argv,
)
from sidecar.remote_watch_types import (
    DEFAULT_WATCH_QUEUE_ITEMS,
    RemoteWatchEvent,
    RemoteWatchFailure,
    RemoteWatchItem,
    RemoteWatchReady,
)


def parse_bounded_json(payload: object, *, max_bytes: int) -> Any:
    """Parse bounded JSON using the facade's injectable protocol limits."""

    return _parse_bounded_json_with_limits(
        payload,
        max_bytes=max_bytes,
        max_depth=MAX_JSON_DEPTH,
        max_nodes=MAX_JSON_ITEMS,
        max_string_bytes=MAX_JSON_STRING_BYTES,
    )


def _selected_hosts(
    hosts: Sequence[RemoteHost],
    selected: Optional[Iterable[str]],
) -> Tuple[RemoteHost, ...]:
    if len(hosts) > MAX_HOSTS:
        raise ValueError("remote fleet exceeds host limit")
    aliases: Dict[str, RemoteHost] = {}
    for host in hosts:
        if not isinstance(host, RemoteHost):
            raise TypeError("hosts must contain RemoteHost values")
        folded = host.alias.casefold()
        if folded in aliases:
            raise ValueError("duplicate remote host alias")
        aliases[folded] = host
    if selected is None:
        values = list(aliases.values())
    else:
        requested: Dict[str, str] = {}
        for alias in selected:
            valid = _validate_alias(alias)
            folded = valid.casefold()
            if folded in requested:
                raise ValueError("duplicate selected host alias")
            requested[folded] = valid
        missing = sorted(
            (requested[folded] for folded in requested if folded not in aliases),
            key=lambda alias: (alias.casefold(), alias),
        )
        if missing:
            raise ValueError(
                "selected remote host is not eligible: {}".format(missing[0])
            )
        values = [host for folded, host in aliases.items() if folded in requested]
    return tuple(sorted(values, key=lambda host: (host.alias.casefold(), host.alias)))


def _row_sort_key(row: Mapping[str, Any]) -> Tuple[str, str, str, str]:
    return (
        str(row.get("host", "")).casefold(),
        str(row.get("agent", "")).casefold(),
        str(row.get("session_id", "")),
        json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ),
    )


def _cancel_pending_futures(
    futures: Iterable[concurrent.futures.Future],
) -> None:
    """Cancel submitted work that has not started.

    This is the Python 3.8-compatible equivalent of the pending-work portion
    of ``Executor.shutdown(cancel_futures=True)``. Running work cannot be
    force-cancelled here, so callers must signal it separately before cleanup.
    """

    for future in futures:
        future.cancel()


def watch_remote(
    *,
    hosts: Optional[Sequence[RemoteHost]] = None,
    selected: Optional[Iterable[str]] = None,
    from_start: bool = False,
    python_candidates: Optional[Sequence[str]] = None,
    runner: Optional[Callable[..., object]] = None,
    stream_factory: Optional[Callable[..., object]] = None,
    host_opener: Optional[Callable[..., object]] = None,
    artifact: Optional[bytes] = None,
    queue_items: int = DEFAULT_WATCH_QUEUE_ITEMS,
    cancel_event: Optional[threading.Event] = None,
    inventory_env: Optional[Mapping[str, str]] = None,
    inventory_home: Optional[Path] = None,
    inventory_file_reader: Optional[Callable[[Path], bytes]] = None,
) -> RemoteWatchSession:
    """Start every selected eligible host as one bounded watch session.

    Session close is thread-safe and owns no process-global signal state.
    A future CLI/main-thread caller may wrap the complete session lifetime in
    ``bounded_execution_signal_guard`` to convert exit signals while retaining
    per-session stream ownership.
    """

    if type(from_start) is not bool:
        raise TypeError("from_start must be bool")
    probe_candidates = (
        REMOTE_PYTHON_CANDIDATES
        if python_candidates is None
        else _validated_probe_candidates(python_candidates)
    )
    available = (
        load_remote_hosts(
            runner=runner,
            env=inventory_env,
            home=inventory_home,
            file_reader=inventory_file_reader,
        )
        if hosts is None
        else tuple(hosts)
    )
    targets = _selected_hosts(available, selected)
    zipapp = (
        build_zipapp_bytes()
        if artifact is None and targets
        else (b"" if not targets else artifact)
    )
    if (
        not isinstance(zipapp, bytes)
        or (targets and not zipapp)
        or len(zipapp) > MAX_ARTIFACT_BYTES
    ):
        raise ValueError("invalid zipapp artifact")
    effective_host_opener = host_opener
    if python_candidates is not None:
        provider = open_remote_watch_host if host_opener is None else host_opener

        def effective_host_opener(host, artifact, **kwargs):
            return provider(
                host,
                artifact,
                python_candidates=probe_candidates,
                **kwargs
            )

    return RemoteWatchSession(
        targets,
        zipapp,
        from_start=from_start,
        runner=runner,
        stream_factory=stream_factory,
        host_opener=effective_host_opener,
        queue_items=queue_items,
        cancel_event=cancel_event,
    )


def aggregate_remote(
    command: str,
    *,
    recent_seconds: Optional[float] = None,
    hosts: Optional[Sequence[RemoteHost]] = None,
    selected: Optional[Iterable[str]] = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    python_candidates: Optional[Sequence[str]] = None,
    runner: Optional[Callable[..., object]] = None,
    artifact: Optional[bytes] = None,
    inventory_env: Optional[Mapping[str, str]] = None,
    inventory_home: Optional[Path] = None,
    inventory_file_reader: Optional[Callable[[Path], bytes]] = None,
    fleet_timeout: float = FLEET_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> RemoteAggregate:
    """Run an allowlisted read-only command across an eligible remote fleet.

    Aggregate admission is fleet-global: if the combined validated successes
    exceed either aggregate cap, every otherwise-successful host is reported
    as ``resource_limit`` and no rows are returned. This makes admission
    independent of completion order while retaining bounded row storage.
    """

    _command_arguments(command, recent_seconds)
    if type(max_workers) is not int or max_workers <= 0:
        raise ValueError("max_workers must be a positive integer")
    if not 0 < float(fleet_timeout) <= FLEET_TIMEOUT_SECONDS:
        raise ValueError("fleet timeout is out of bounds")
    probe_candidates = (
        REMOTE_PYTHON_CANDIDATES
        if python_candidates is None
        else _validated_probe_candidates(python_candidates)
    )
    workers = min(max_workers, MAX_WORKERS)
    available = (
        load_remote_hosts(
            runner=runner,
            env=inventory_env,
            home=inventory_home,
            file_reader=inventory_file_reader,
        )
        if hosts is None
        else tuple(hosts)
    )
    targets = _selected_hosts(available, selected)
    labels = tuple(host.alias for host in targets)
    if not targets:
        return RemoteAggregate(command=command, hosts=labels)
    zipapp = build_zipapp_bytes() if artifact is None else artifact
    if not isinstance(zipapp, bytes) or not zipapp or len(zipapp) > MAX_ARTIFACT_BYTES:
        raise ValueError("invalid zipapp artifact")

    rows: List[Mapping[str, Any]] = []
    failures: List[RemoteFailure] = []
    succeeded: List[str] = []
    successful_candidates: List[str] = []
    aggregate_rows = 0
    aggregate_bytes = 2
    aggregate_overflow = False
    cancel_event = threading.Event()
    wall_deadline = monotonic() + float(fleet_timeout)
    cleanup_reserve = min(0.25, float(fleet_timeout) / 5.0)
    deadline = wall_deadline - cleanup_reserve
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(workers, len(targets)),
        thread_name_prefix="sidecar-remote",
    )
    futures: Dict[concurrent.futures.Future, RemoteHost] = {}
    next_target = 0
    deadline_expired = False

    def submit_available() -> None:
        nonlocal next_target
        while len(futures) < workers and next_target < len(targets):
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            host = targets[next_target]
            next_target += 1
            future = executor.submit(
                execute_remote_host,
                host,
                command,
                zipapp,
                recent_seconds=recent_seconds,
                python_candidates=probe_candidates,
                runner=runner,
                timeout=min(HOST_TIMEOUT_SECONDS, remaining),
                cancel_event=cancel_event,
            )
            futures[future] = host

    def consume(future: concurrent.futures.Future, host: RemoteHost) -> None:
        nonlocal aggregate_bytes, aggregate_overflow, aggregate_rows
        try:
            host_rows, failure = future.result()
        except Exception:
            host_rows = None
            failure = RemoteFailure(host.alias, "remote")
        if failure is not None:
            failures.append(failure)
            return
        candidate_rows = tuple(host_rows or ())
        try:
            candidate_bytes = sum(len(_encoded_row(row)) + 1 for row in candidate_rows)
        except RecursionError:
            failures.append(RemoteFailure(host.alias, "resource_limit"))
            return
        except (TypeError, UnicodeError, ValueError):
            failures.append(RemoteFailure(host.alias, "protocol"))
            return
        successful_candidates.append(host.alias)
        aggregate_rows += len(candidate_rows)
        aggregate_bytes += candidate_bytes
        if aggregate_rows > MAX_ROWS or aggregate_bytes > MAX_AGGREGATE_BYTES:
            aggregate_overflow = True
            rows.clear()
            succeeded.clear()
            return
        rows.extend(candidate_rows)
        succeeded.append(host.alias)

    with _bounded_execution_signal_guard() as process_registry:
        try:
            submit_available()
            while futures:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    deadline_expired = True
                    break
                done, _pending = concurrent.futures.wait(
                    tuple(futures),
                    timeout=remaining,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not done:
                    deadline_expired = True
                    break
                for future in sorted(
                    done,
                    key=lambda item: (
                        futures[item].alias.casefold(),
                        futures[item].alias,
                    ),
                ):
                    consume(future, futures.pop(future))
                    submit_available()

            if next_target < len(targets):
                deadline_expired = True
            if deadline_expired:
                for future in sorted(
                    tuple(futures),
                    key=lambda item: (
                        futures[item].alias.casefold(),
                        futures[item].alias,
                    ),
                ):
                    if future.done():
                        consume(future, futures.pop(future))
                cancel_event.set()
                for host in futures.values():
                    failures.append(RemoteFailure(host.alias, "timeout"))
                _cancel_pending_futures(tuple(futures))
                for host in targets[next_target:]:
                    failures.append(RemoteFailure(host.alias, "timeout"))
                cleanup_remaining = max(0.0, wall_deadline - monotonic())
                if futures and cleanup_remaining > 0:
                    concurrent.futures.wait(
                        tuple(futures),
                        timeout=cleanup_remaining,
                    )
        except BaseException:
            cancel_event.set()
            _cancel_pending_futures(tuple(futures))
            process_registry.kill_all()
            executor.shutdown(wait=True)
            raise

    # Deadline cancellation happens before the reserved cleanup wait so queued
    # work cannot begin while running workers are observing ``cancel_event``.
    executor.shutdown(
        wait=not deadline_expired or all(future.done() for future in futures)
    )

    if aggregate_overflow:
        failures.extend(
            RemoteFailure(alias, "resource_limit") for alias in successful_candidates
        )

    return RemoteAggregate(
        command=command,
        rows=tuple(sorted(rows, key=_row_sort_key)),
        failures=tuple(
            sorted(failures, key=lambda item: (item.host.casefold(), item.host))
        ),
        hosts=labels,
        succeeded=tuple(sorted(succeeded, key=lambda alias: (alias.casefold(), alias))),
    )


__all__ = [
    "DEFAULT_MAX_WORKERS",
    "DEFAULT_WATCH_QUEUE_ITEMS",
    "ELIGIBLE_PHASES",
    "EXIT_INVALID_INVENTORY",
    "EXIT_NO_SUCCESS",
    "EXIT_OK",
    "FAILURE_CODES",
    "FLEET_TIMEOUT_SECONDS",
    "HOST_TIMEOUT_SECONDS",
    "MAX_AGGREGATE_BYTES",
    "MAX_ARTIFACT_BYTES",
    "MAX_HOSTS",
    "MAX_INVENTORY_BYTES",
    "MAX_PROTOCOL_BYTES",
    "MAX_RECENT_SECONDS",
    "MAX_ROWS",
    "MAX_STDERR_BYTES",
    "MAX_SESSION_TIMESTAMP",
    "MAX_WORKERS",
    "MIN_SESSION_TIMESTAMP",
    "PROBE_TIMEOUT_SECONDS",
    "ProtocolResourceLimitError",
    "REMOTE_BOOTSTRAP",
    "REMOTE_PYTHON_CANDIDATES",
    "REMOTE_WATCH_BOOTSTRAP",
    "RemoteAggregate",
    "RemoteFailure",
    "RemoteHost",
    "RemoteInventoryError",
    "RemoteWatchEvent",
    "RemoteWatchFailure",
    "RemoteWatchItem",
    "RemoteWatchReady",
    "RemoteWatchSession",
    "aggregate_remote",
    "build_zipapp_bytes",
    "execute_remote_host",
    "load_remote_hosts",
    "parse_bounded_json",
    "probe_remote_python",
    "remote_shell_command",
    "remote_watch_shell_command",
    "remote_watch_ssh_argv",
    "ssh_argv",
    "validate_remote_python_executable",
    "watch_remote",
]
