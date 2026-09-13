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


# --- OpenAICompatBackend: 401/403/timeout/connection-failure, each a clear,
# distinct error rather than one generic "backend failed" message -----------
#
# Written for the private-gateway integration (a Tailscale-tunneled
# self-hosted box): a revoked key, an unpermitted model, a slow cold-started
# local model, and the desktop/tunnel being offline are all real, distinct
# failure modes a user needs to tell apart at a glance.

def _make_backend(name: str = "test", timeout: float = 60.0):
    from grandice.router import OpenAICompatBackend

    backend = OpenAICompatBackend.__new__(OpenAICompatBackend)  # skip __init__, no real client needed
    backend.name = name
    backend._timeout = timeout
    return backend


def _http_error(cls, status: int, message: str):
    import httpx

    request = httpx.Request("POST", "https://example.invalid/chat/completions")
    response = httpx.Response(status, request=request, json={"error": {"message": message}})
    return cls(message, response=response, body={"message": message})


async def test_401_reports_an_invalid_or_revoked_key(monkeypatch):
    from openai import AuthenticationError

    backend = _make_backend()
    error = _http_error(AuthenticationError, 401, "Invalid API key")

    async def fake_stream_once(self, *a, **kw):
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(type(backend), "_stream_once", fake_stream_once)

    with pytest.raises(RuntimeError, match="invalid or revoked"):
        async for _ in backend.complete("chat", [], [], 0.2):
            pass


async def test_403_reports_model_not_permitted(monkeypatch):
    from openai import PermissionDeniedError

    backend = _make_backend()
    error = _http_error(PermissionDeniedError, 403, "You do not have access to this model")

    async def fake_stream_once(self, *a, **kw):
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(type(backend), "_stream_once", fake_stream_once)

    with pytest.raises(RuntimeError, match="not permitted"):
        async for _ in backend.complete("chat", [], [], 0.2):
            pass


async def test_timeout_reports_a_clear_timeout_message_with_the_configured_seconds(monkeypatch):
    from openai import APITimeoutError

    backend = _make_backend(timeout=42.0)

    async def fake_stream_once(self, *a, **kw):
        raise APITimeoutError(request=None)
        yield  # pragma: no cover

    monkeypatch.setattr(type(backend), "_stream_once", fake_stream_once)

    with pytest.raises(RuntimeError, match="timed out.*42"):
        async for _ in backend.complete("chat", [], [], 0.2):
            pass


async def test_connection_failure_mentions_tailscale(monkeypatch):
    from openai import APIConnectionError

    backend = _make_backend()

    async def fake_stream_once(self, *a, **kw):
        raise APIConnectionError(message="Connection refused", request=None)
        yield  # pragma: no cover

    monkeypatch.setattr(type(backend), "_stream_once", fake_stream_once)

    with pytest.raises(RuntimeError, match="Tailscale"):
        async for _ in backend.complete("chat", [], [], 0.2):
            pass


# --- Router.embed() ---------------------------------------------------------

async def test_embed_requires_live_config():
    config = replace(Config.from_env(), api_key=None, base_url=None)
    router = Router(config)
    with pytest.raises(RuntimeError, match="live model config"):
        await router.embed(["hello"])


async def test_embed_requires_an_embedding_model_configured():
    config = replace(
        Config.from_env(), api_key="gll-x", base_url="https://example.invalid/v1", embedding_model=None
    )
    router = Router(config, backends=[StubBackend()], rate_limiter=None)
    with pytest.raises(RuntimeError, match="GRANDICE_LLM_EMBEDDING_MODEL"):
        await router.embed(["hello"])


async def test_embed_calls_the_configured_base_url_and_model(monkeypatch):
    config = replace(
        Config.from_env(),
        api_key="gll-x",
        base_url="https://example.invalid/v1",
        embedding_model="embed",
    )
    router = Router(config, backends=[StubBackend()], rate_limiter=None)

    captured = {}

    class FakeEmbeddings:
        async def create(self, model, input):
            captured["model"] = model
            captured["input"] = input

            class Item:
                embedding = [0.1, 0.2]

            class Result:
                data = [Item(), Item()]

            return Result()

    class FakeClient:
        def __init__(self, base_url, api_key, timeout):
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            self.embeddings = FakeEmbeddings()

    monkeypatch.setattr("openai.AsyncOpenAI", FakeClient)

    vectors = await router.embed(["a", "b"])

    assert captured["base_url"] == "https://example.invalid/v1"
    assert captured["model"] == "embed"
    assert captured["input"] == ["a", "b"]
    assert vectors == [[0.1, 0.2], [0.1, 0.2]]


# --- Router.complete(model=...) — a per-call override of the tier's model --

async def test_complete_uses_the_tiers_model_by_default(monkeypatch):
    config = replace(Config.from_env(), api_key="k", base_url="https://example.invalid")
    captured = {}

    class _Capture(Backend):
        name = "capture"

        async def complete(self, model, messages, tools, temperature):
            captured["model"] = model
            yield Reply(text="ok")

    router = Router(config, backends=[_Capture()], rate_limiter=None)
    async for _ in router.complete(tier="orchestrator", messages=[]):
        pass
    assert captured["model"] == config.tiers.orchestrator


async def test_complete_model_override_wins_over_the_tier(monkeypatch):
    config = replace(Config.from_env(), api_key="k", base_url="https://example.invalid")
    captured = {}

    class _Capture(Backend):
        name = "capture"

        async def complete(self, model, messages, tools, temperature):
            captured["model"] = model
            yield Reply(text="ok")

    router = Router(config, backends=[_Capture()], rate_limiter=None)
    async for _ in router.complete(tier="orchestrator", messages=[], model="a-custom-alias"):
        pass
    assert captured["model"] == "a-custom-alias"


# --- Router.list_models() ---------------------------------------------

async def test_list_models_returns_tier_ids_when_not_live():
    config = replace(Config.from_env(), api_key=None, base_url=None)
    router = Router(config)
    models = await router.list_models()
    assert set(models) == {config.tiers.orchestrator, config.tiers.worker, config.tiers.bulk}


async def test_list_models_queries_the_provider_when_live(monkeypatch):
    config = replace(Config.from_env(), api_key="k", base_url="https://example.invalid")
    router = Router(config, backends=[StubBackend()], rate_limiter=None)

    class FakeModel:
        def __init__(self, id):
            self.id = id

    class FakeModels:
        async def list(self):
            class Result:
                data = [FakeModel("chat"), FakeModel("code"), FakeModel("vision")]

            return Result()

    class FakeClient:
        def __init__(self, base_url, api_key, timeout):
            self.models = FakeModels()

    monkeypatch.setattr("openai.AsyncOpenAI", FakeClient)

    models = await router.list_models()
    assert models == ["chat", "code", "vision"]


async def test_list_models_falls_back_if_the_provider_endpoint_errors(monkeypatch):
    config = replace(Config.from_env(), api_key="k", base_url="https://example.invalid")
    router = Router(config, backends=[StubBackend()], rate_limiter=None)

    class FakeModels:
        async def list(self):
            raise RuntimeError("the gateway doesn't support /v1/models")

    class FakeClient:
        def __init__(self, base_url, api_key, timeout):
            self.models = FakeModels()

    monkeypatch.setattr("openai.AsyncOpenAI", FakeClient)

    models = await router.list_models()
    assert set(models) == {config.tiers.orchestrator, config.tiers.worker, config.tiers.bulk}
