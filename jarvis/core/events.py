"""In-process publish/subscribe used to push real backend state to the UI."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self, queue_size: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        event = {"type": event_type, "ts": time.time(), "data": data or {}}
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled client must not block the core; it will resync
                # from the next full state snapshot.
                log.warning("Dropping event %s for a slow subscriber", event_type)
