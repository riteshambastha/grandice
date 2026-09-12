"""Client-side pacing for OpenRouter's free tier.

Free models are rate-limited, not credit-limited: 20 requests/minute always,
and a daily cap of 50 (or 1,000 after a one-time $10 credit purchase — the
purchase does not spend down, it only raises the daily ceiling). One agentic
task can spend 30-120 model calls, so at 50/day the harness would exhaust its
whole budget on a single real task. This module makes that failure explicit
and early rather than as a wall of 429s three tasks in.

The per-minute cap is enforced proactively (sleep before the call). The daily
cap is tracked and raised as a clear error, because silently blocking for
however many hours are left in the day is worse than stopping the session.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


class DailyCapReached(RuntimeError):
    """The free tier's daily request budget is spent."""


@dataclass
class RateLimiter:
    per_minute: int = 18  # OpenRouter's free cap is 20; leave headroom for jitter
    per_day: int = 50     # 1000 after the one-time $10 OpenRouter credit purchase
    state_path: Path | None = None

    _minute_window: deque[float] = field(default_factory=deque, init=False)
    _day: str = field(default="", init=False)
    _day_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._load_day()

    def _today(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _load_day(self) -> None:
        today = self._today()
        if self.state_path and self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text())
                if saved.get("day") == today:
                    self._day, self._day_count = today, int(saved.get("count", 0))
                    return
            except (json.JSONDecodeError, OSError, ValueError):
                pass  # a corrupt state file should not block startup
        self._day, self._day_count = today, 0

    def _save_day(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"day": self._day, "count": self._day_count}))

    async def acquire(self) -> None:
        """Block until a call is allowed under the per-minute window, or raise
        if today's budget is already spent."""
        import asyncio

        today = self._today()
        if today != self._day:
            self._day, self._day_count = today, 0

        if self._day_count >= self.per_day:
            raise DailyCapReached(
                f"{self._day_count}/{self.per_day} free-tier requests used today (UTC). "
                f"Resets at midnight UTC. Raise GRANDICE_DAILY_REQUEST_CAP if you have "
                f"OpenRouter credit, or wait for the reset."
            )

        now = time.monotonic()
        window = self._minute_window
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            wait = 60 - (now - window[0]) + 0.05
            await asyncio.sleep(max(wait, 0))
            now = time.monotonic()
            while window and now - window[0] > 60:
                window.popleft()

        window.append(time.monotonic())
        self._day_count += 1
        self._save_day()

    @property
    def used_today(self) -> int:
        return self._day_count
