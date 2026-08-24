"""Bounded in-process fanout for normalized sidecar events."""

from __future__ import annotations

import queue
import threading
from typing import Any, FrozenSet, Iterable, Mapping, Optional, Set

from sidecar.model import Event

DEFAULT_SUBSCRIBER_QUEUE = 256

_CLOSED = object()


class BusError(RuntimeError):
    """Base error for event bus failures."""


class SubscriptionClosed(BusError):
    """Raised after the event bus closes a subscription."""


def _normalized_agents(
    agents: Optional[Iterable[str]],
) -> Optional[FrozenSet[str]]:
    if agents is None:
        return None
    names = frozenset(agents)
    if not names or any(
        not isinstance(name, str) or not name for name in names
    ):
        raise ValueError(
            "agents filter must be a nonempty collection of nonempty strings"
        )
    return names


class Subscription:
    """One bounded event queue owned by :class:`EventBus`."""

    def __init__(
        self,
        bus: "EventBus",
        maxsize: int,
        agents: Optional[Iterable[str]] = None,
    ) -> None:
        self._bus = bus
        self.agents = _normalized_agents(agents)
        self.queue: "queue.Queue[object]" = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._closed = False
        self.dropped = 0

    @property
    def maxsize(self) -> int:
        return self.queue.maxsize

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _offer(self, event: Mapping[str, Any]) -> None:
        # Filtered-out events never consume a bounded queue slot and never
        # count as dropped, so the 256-slot semantics apply per subscriber
        # to its own selected agents only.
        if self.agents is not None and event.get("agent") not in self.agents:
            return
        with self._lock:
            if self._closed:
                return
            try:
                self.queue.put_nowait(dict(event))
                return
            except queue.Full:
                pass
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.dropped += 1
            try:
                self.queue.put_nowait(dict(event))
            except queue.Full:
                self.dropped += 1

    def _finish(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.queue.put_nowait(_CLOSED)
                return
            except queue.Full:
                pass
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(_CLOSED)
            except queue.Full:
                pass

    def get(self, timeout: Optional[float] = None) -> Mapping[str, Any]:
        value = self.queue.get(timeout=timeout)
        if value is _CLOSED:
            raise SubscriptionClosed("subscription is closed")
        if not isinstance(value, Mapping):
            raise SubscriptionClosed("subscription received an invalid sentinel")
        return value

    def close(self) -> None:
        self._bus.unsubscribe(self)


class EventBus:
    """Fan events out without allowing any client queue to grow unbounded."""

    def __init__(self, queue_size: int = DEFAULT_SUBSCRIBER_QUEUE) -> None:
        if queue_size <= 0:
            raise ValueError("subscriber queue size must be positive")
        self.queue_size = int(queue_size)
        self._lock = threading.Lock()
        self._subscriptions: Set[Subscription] = set()
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    def subscribe(
        self,
        agents: Optional[Iterable[str]] = None,
    ) -> Subscription:
        """Register one subscriber, optionally filtered to ``agents`` names."""

        subscription = Subscription(self, self.queue_size, agents=agents)
        with self._lock:
            if self._closed:
                subscription._finish()
            else:
                self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            self._subscriptions.discard(subscription)
        subscription._finish()

    def publish(self, event: Any) -> None:
        if isinstance(event, Event):
            payload = event.to_dict()
        elif isinstance(event, Mapping):
            payload = dict(event)
        else:
            raise TypeError("event bus accepts Event or mapping values")
        with self._lock:
            subscriptions = tuple(self._subscriptions)
        for subscription in subscriptions:
            subscription._offer(payload)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscriptions = tuple(self._subscriptions)
            self._subscriptions.clear()
        for subscription in subscriptions:
            subscription._finish()


__all__ = [
    "BusError",
    "DEFAULT_SUBSCRIBER_QUEUE",
    "EventBus",
    "Subscription",
    "SubscriptionClosed",
]
