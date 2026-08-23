"""Bounded fleet coordination for remote watch streams."""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Callable, Dict, Iterator, Optional, Sequence, Tuple

from sidecar.remote_types import (
    MAX_ARTIFACT_BYTES,
    MAX_HOSTS,
    RemoteFailure,
    RemoteHost,
)
from sidecar.remote_watch_transport import (
    RemoteWatchHostStream,
    RemoteWatchTransportError,
    open_remote_watch_host,
)
from sidecar.remote_watch_types import (
    DEFAULT_WATCH_QUEUE_ITEMS,
    RemoteWatchEvent,
    RemoteWatchFailure,
    RemoteWatchItem,
    RemoteWatchReady,
    WATCH_JOIN_TIMEOUT_SECONDS,
    WATCH_QUEUE_POLL_SECONDS,
    validate_watch_queue_items,
)


class _CombinedCancel:
    def __init__(
        self,
        internal: threading.Event,
        external: Optional[threading.Event],
    ) -> None:
        self._internal = internal
        self._external = external

    def is_set(self) -> bool:
        return self._internal.is_set() or bool(
            self._external is not None and self._external.is_set()
        )


class _FleetBuffer:
    """Per-host bounded queues with fleet-global admission and fair draining."""

    def __init__(
        self,
        hosts: Sequence[str],
        capacity: int,
        cancel_event: _CombinedCancel,
    ) -> None:
        self._order = tuple(hosts)
        self._queues = {host: deque() for host in self._order}
        self._capacity = capacity
        self._cancel_event = cancel_event
        self._condition = threading.Condition()
        self._size = 0
        self._cursor = 0
        self._admit_cursor = 0
        self._waiters = {host: 0 for host in self._order}
        self._priority_waiters = {host: 0 for host in self._order}

    def _next_waiting_host(self, *, priority: bool) -> Optional[str]:
        waiters = self._priority_waiters if priority else self._waiters
        for offset in range(len(self._order)):
            index = (self._admit_cursor + offset) % len(self._order)
            host = self._order[index]
            if waiters[host] > 0:
                return host
        return None

    def put(self, host: str, item: object, *, priority: bool) -> bool:
        with self._condition:
            self._waiters[host] += 1
            if priority:
                self._priority_waiters[host] += 1
            try:
                while not self._cancel_event.is_set():
                    host_queue = self._queues[host]
                    priority_host = self._next_waiting_host(priority=True)
                    eligible = (
                        host == priority_host
                        if priority_host is not None
                        else host == self._next_waiting_host(priority=False)
                    )
                    if (
                        self._size < self._capacity
                        and len(host_queue) < self._capacity
                        and eligible
                    ):
                        host_queue.append(item)
                        self._size += 1
                        self._admit_cursor = (self._order.index(host) + 1) % len(
                            self._order
                        )
                        self._condition.notify_all()
                        return True
                    self._condition.wait(WATCH_QUEUE_POLL_SECONDS)
                return False
            finally:
                self._waiters[host] -= 1
                if priority:
                    self._priority_waiters[host] -= 1
                self._condition.notify_all()

    def get(self, timeout: float) -> object:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._size == 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            for offset in range(len(self._order)):
                index = (self._cursor + offset) % len(self._order)
                host = self._order[index]
                host_queue = self._queues[host]
                if not host_queue:
                    continue
                item = host_queue.popleft()
                self._size -= 1
                self._cursor = (index + 1) % len(self._order)
                self._condition.notify_all()
                return item
        raise queue.Empty

    def empty(self) -> bool:
        with self._condition:
            return self._size == 0

    def wake_all(self) -> None:
        with self._condition:
            self._condition.notify_all()


class RemoteWatchSession(Iterator[RemoteWatchItem]):
    """Thread-closeable iterator that owns only its host streams and workers.

    A main-thread caller that wants SIGTERM/SIGHUP converted to cleanup may
    place the session lifetime inside ``bounded_execution_signal_guard``.
    The session itself never installs, restores, or closes process-global
    signal state.
    """

    def __init__(
        self,
        hosts: Sequence[RemoteHost],
        artifact: bytes,
        *,
        from_start: bool = False,
        runner: Optional[Callable[..., object]] = None,
        stream_factory: Optional[Callable[..., object]] = None,
        host_opener: Optional[Callable[..., object]] = None,
        queue_items: int = DEFAULT_WATCH_QUEUE_ITEMS,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        try:
            targets = tuple(hosts)
        except TypeError as error:
            raise TypeError("hosts must be a sequence of RemoteHost values") from error
        if len(targets) > MAX_HOSTS:
            raise ValueError("remote fleet exceeds host limit")
        aliases = set()
        for host in targets:
            if not isinstance(host, RemoteHost):
                raise TypeError("hosts must contain RemoteHost values")
            folded = host.alias.casefold()
            if folded in aliases:
                raise ValueError("duplicate remote host alias")
            aliases.add(folded)
        if (
            not isinstance(artifact, bytes)
            or (targets and not artifact)
            or len(artifact) > MAX_ARTIFACT_BYTES
        ):
            raise ValueError("invalid zipapp artifact")
        if type(from_start) is not bool:
            raise TypeError("from_start must be bool")
        if cancel_event is not None and not hasattr(cancel_event, "is_set"):
            raise TypeError("cancel_event must provide is_set")

        self.hosts = tuple(host.alias for host in targets)
        self.from_start = from_start
        self._targets = targets
        self._artifact = artifact
        self._runner = runner
        self._stream_factory = stream_factory
        self._host_opener = (
            open_remote_watch_host if host_opener is None else host_opener
        )
        capacity = validate_watch_queue_items(queue_items)
        self._cancel = threading.Event()
        self._combined_cancel = _CombinedCancel(self._cancel, cancel_event)
        self._buffer = _FleetBuffer(
            self.hosts,
            capacity,
            self._combined_cancel,
        )
        self._lock = threading.Lock()
        self._active: Dict[str, RemoteWatchHostStream] = {}
        self._ready_hosts = set()
        self._failures = []
        self._failed_hosts = set()
        self._done_hosts = set()
        self._workers = []
        self._closed = False

        try:
            for index, host in enumerate(self._targets):
                worker = threading.Thread(
                    target=self._run_host,
                    args=(host,),
                    name="sidecar-remote-watch-{:03d}".format(index),
                    daemon=False,
                )
                worker.start()
                self._workers.append(worker)
        except BaseException:
            self.close()
            raise

    @property
    def ready_hosts(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._ready_hosts,
                    key=lambda alias: (alias.casefold(), alias),
                )
            )

    @property
    def failures(self) -> Tuple[RemoteWatchFailure, ...]:
        with self._lock:
            return tuple(self._failures)

    @property
    def empty(self) -> bool:
        return not self.hosts

    @property
    def all_failed(self) -> bool:
        with self._lock:
            return bool(
                self.hosts
                and len(self._done_hosts) == len(self.hosts)
                and len(self._failed_hosts) == len(self.hosts)
            )

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def __enter__(self) -> "RemoteWatchSession":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def __iter__(self) -> "RemoteWatchSession":
        return self

    def _offer(self, host: str, item: object, *, priority: bool = False) -> bool:
        return self._buffer.put(host, item, priority=priority)

    def _record_ready(self, ready: RemoteWatchReady) -> None:
        with self._lock:
            self._ready_hosts.add(ready.host)

    def _record_failure(self, failure: RemoteWatchFailure) -> None:
        with self._lock:
            self._failures.append(failure)
            self._failed_hosts.add(failure.host)

    def _publish_failure(self, failure: RemoteFailure) -> None:
        item = RemoteWatchFailure(failure.host, failure.code)
        self._record_failure(item)
        self._offer(failure.host, item, priority=True)

    def _run_host(self, host: RemoteHost) -> None:
        stream: Optional[RemoteWatchHostStream] = None
        try:
            opened = self._host_opener(
                host,
                self._artifact,
                from_start=self.from_start,
                runner=self._runner,
                stream_factory=self._stream_factory,
                cancel_event=self._combined_cancel,
            )
            if not isinstance(opened, tuple) or len(opened) != 2:
                raise TypeError("invalid remote watch opener result")
            stream, failure = opened
            if failure is not None:
                if not isinstance(failure, RemoteFailure):
                    raise TypeError("invalid remote watch opener failure")
                if not self._combined_cancel.is_set():
                    self._publish_failure(failure)
                return
            if not isinstance(stream, RemoteWatchHostStream):
                raise TypeError("invalid remote watch opener stream")
            with self._lock:
                self._active[host.alias] = stream
            if self._combined_cancel.is_set():
                return
            ready = stream.read_ready()
            self._record_ready(ready)
            if not self._offer(host.alias, ready, priority=True):
                return
            for event in stream:
                if not isinstance(event, RemoteWatchEvent):
                    raise TypeError("invalid remote watch stream item")
                if not self._offer(host.alias, event):
                    return
        except RemoteWatchTransportError as error:
            if not self._combined_cancel.is_set():
                self._publish_failure(RemoteFailure(host.alias, error.code))
        except BaseException:
            if not self._combined_cancel.is_set():
                self._publish_failure(RemoteFailure(host.alias, "remote"))
        finally:
            if stream is not None:
                try:
                    stream.close()
                except BaseException:
                    pass
            with self._lock:
                self._active.pop(host.alias, None)
                self._done_hosts.add(host.alias)
            self._buffer.wake_all()

    def __next__(self) -> RemoteWatchItem:
        if self.closed:
            raise StopIteration
        if not self._workers:
            self.close()
            raise StopIteration
        try:
            while True:
                if self._combined_cancel.is_set():
                    self.close()
                    raise StopIteration
                try:
                    item = self._buffer.get(WATCH_QUEUE_POLL_SECONDS)
                except queue.Empty:
                    with self._lock:
                        done = len(self._done_hosts) == len(self.hosts)
                    if done and self._buffer.empty():
                        self.close()
                        raise StopIteration
                    continue
                if isinstance(
                    item,
                    (RemoteWatchReady, RemoteWatchFailure, RemoteWatchEvent),
                ):
                    return item
                self.close()
                raise RuntimeError("invalid remote watch queue item")
        except KeyboardInterrupt:
            self.close()
            raise

    def cancel(self) -> None:
        """Cancel all hosts and close the iterator."""

        self.close()

    def close(self) -> None:
        """Kill SSH groups, join workers with a bound, and close the session."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._cancel.set()
        self._buffer.wake_all()
        with self._lock:
            active = tuple(self._active.values())
        for stream in active:
            try:
                stream.close()
            except BaseException:
                pass
        deadline = time.monotonic() + WATCH_JOIN_TIMEOUT_SECONDS
        for worker in self._workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


__all__ = ["RemoteWatchSession"]
