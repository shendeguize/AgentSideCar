"""Bounded in-process fanout for normalized sidecar events."""

from __future__ import annotations

import queue
import threading
from typing import Any, Mapping, Optional, Set

from sidecar.model import Event

DEFAULT_SUBSCRIBER_QUEUE = 256

_CLOSED = object()


class BusError(RuntimeError):
    """Base error for event bus failures."""


class SubscriptionClosed(BusError):
    """Raised after the event bus closes a subscription."""


class Subscription:
    """One bounded event queue owned by :class:`EventBus`."""

    def __init__(self, bus: "EventBus", maxsize: int) -> None:
        self._bus = bus
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

    def subscribe(self) -> Subscription:
        subscription = Subscription(self, self.queue_size)
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
