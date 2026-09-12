"""Tests for the free-tier survival pieces added once the harness targets
OpenRouter's actual free-tier limits: the client-side pacer, the daily cap,
and the router's live/stub split."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest

from grandice.config import Config
from grandice.ratelimit import DailyCapReached, RateLimiter
from grandice.router import Backend, Reply, Router, StubBackend


# --- RateLimiter ---------------------------------------------------------

async def test_per_minute_window_paces_calls_without_a_hard_stop():
    limiter = RateLimiter(per_minute=2, per_day=100)
    start = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    # third call inside the same minute should have to wait for the window,
    # but the test caps the wait so this proves pacing without a 60s test.
    limiter._minute_window[0] = time.monotonic() - 59.98  # about to fall out of window
    await asyncio.wait_for(limiter.acquire(), timeout=2)
    assert time.monotonic() - start < 2  # paced, not stuck


async def test_daily_cap_raises_a_clear_error_not_a_silent_block():
    limiter = RateLimiter(per_minute=1000, per_day=2)
    await limiter.acquire()
    await limiter.acquire()
    with pytest.raises(DailyCapReached, match="2/2"):
        await limiter.acquire()


def test_daily_count_survives_a_restart_via_state_file(tmp_path: Path):
    state = tmp_path / "rate_limit.json"
    first = RateLimiter(per_minute=1000, per_day=50, state_path=state)
    asyncio.run(first.acquire())
    asyncio.run(first.acquire())

    second = RateLimiter(per_minute=1000, per_day=50, state_path=state)
    assert second.used_today == 2  # picked up from disk, not reset to 0


def test_daily_count_resets_on_a_new_day(tmp_path: Path):
    state = tmp_path / "rate_limit.json"
    state.write_text('{"day": "2000-01-01", "count": 49}')
    limiter = RateLimiter(per_minute=1000, per_day=50, state_path=state)
    assert limiter.used_today == 0  # yesterday's count does not carry over


# --- Router: stub is not a live-mode failover ----------------------------

class _AlwaysFails(Backend):
    name = "flaky"

    async def complete(self, model, messages, tools, temperature):
        raise ConnectionError("provider unreachable")
        yield  # pragma: no cover - unreachable, satisfies the async generator shape


async def test_live_router_does_not_fall_back_to_the_stub():
    config = replace(Config.from_env(), api_key="k", base_url="https://example.invalid")
    assert config.live
    router = Router(config, backends=[_AlwaysFails()], rate_limiter=None)

    with pytest.raises(RuntimeError, match="All backends failed"):
        async for _ in router.complete(tier="orchestrator", messages=[{"role": "user", "content": "hi"}]):
            pass  # a silent stub reply here would be the bug


async def test_stub_router_is_used_only_when_not_live():
    config = replace(Config.from_env(), api_key=None, base_url=None)
    assert not config.live
    router = Router(config)
    assert isinstance(router.backends[0], StubBackend)
    assert router.rate_limiter is None  # no point pacing calls that never leave the box
