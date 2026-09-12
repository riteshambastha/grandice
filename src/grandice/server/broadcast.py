"""Fan-out for loop events: one running task, any number of watching tabs.

A bounded history means a tab that opens (or reloads) mid-run isn't starting
from nothing — it replays what already happened, then joins the live tail.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

HISTORY_LIMIT = 500


class Broadcaster:
    def __init__(self, history_limit: int = HISTORY_LIMIT) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._history: deque[dict[str, Any]] = deque(maxlen=history_limit)

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def publish(self, event: dict[str, Any]) -> None:
        self._history.append(event)
        for queue in self._subscribers:
            queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)
