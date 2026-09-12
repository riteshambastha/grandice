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


# --- OpenAICompatBackend: billing errors vs genuine rate limits ------------
#
# Found live against Z.ai: a 429 there means "insufficient balance or no
# resource package", not "too many requests" — retrying can't ever fix that,
# so it must fail fast with the provider's own message rather than burn three
# backoff attempts and then blame a shared rate limit that was never the
# actual cause.

def _rate_limit_error(message: str, code: str = "429"):
    import httpx
    from openai import RateLimitError

    request = httpx.Request("POST", "https://example.invalid/chat/completions")
    response = httpx.Response(429, request=request, json={"error": {"code": code, "message": message}})
    return RateLimitError(f"Error code: 429 - {message}", response=response, body={"code": code, "message": message})


async def test_balance_error_fails_fast_with_the_providers_own_message(monkeypatch):
    from grandice.router import OpenAICompatBackend

    backend = OpenAICompatBackend.__new__(OpenAICompatBackend)  # skip __init__, no real client needed
    backend.name = "test"

    error = _rate_limit_error("Insufficient balance or no resource package. Please recharge.", code="1113")

    async def fake_stream_once(self, *a, **kw):
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(OpenAICompatBackend, "_stream_once", fake_stream_once)

    slept = []
    monkeypatch.setattr("grandice.router.asyncio.sleep", lambda s: slept.append(s))

    with pytest.raises(RuntimeError, match="billing problem"):
        async for _ in backend.complete("glm-4.6", [], [], 0.2):
            pass

    assert slept == []  # no backoff attempted — retrying a billing error can't help


async def test_a_genuine_rate_limit_still_retries_with_backoff(monkeypatch):
    from grandice.router import OpenAICompatBackend

    backend = OpenAICompatBackend.__new__(OpenAICompatBackend)
    backend.name = "test"
    calls = []

    async def fake_stream_once(self, *a, **kw):
        calls.append(1)
        raise _rate_limit_error("Rate limit exceeded, please try again later.")
        yield  # pragma: no cover

    monkeypatch.setattr(OpenAICompatBackend, "_stream_once", fake_stream_once)

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("grandice.router.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="Rate-limited"):
        async for _ in backend.complete("glm-4.6", [], [], 0.2):
            pass

    assert len(calls) == OpenAICompatBackend.MAX_RATE_LIMIT_RETRIES + 1
    assert len(slept) == OpenAICompatBackend.MAX_RATE_LIMIT_RETRIES  # it did back off, unlike the billing case
