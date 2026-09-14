"""Fan-out for loop events: one running task, any number of watching tabs.

A bounded history means a tab that opens (or reloads) mid-run isn't starting
from nothing — it replays what already happened, then joins the live tail.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

# Real bug, found live: a single verbose turn (a long streamed reply, plus
# several tool calls) can easily emit more than a few hundred events — a
# provider that streams token-by-token turns even a moderate response into
# hundreds of tiny text_delta events on its own. At the old 500-event cap,
# reopening a chat (or an SSE reconnect after a network blip) could replay
# a history missing the *start* of that turn, since publish() evicts the
# oldest event once the deque is full — a coherent-looking response that
# actually began somewhere in the middle. Each event is small (a short
# JSON dict), so even a generous cap here costs only a few MB per chat.
HISTORY_LIMIT = 20_000


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
