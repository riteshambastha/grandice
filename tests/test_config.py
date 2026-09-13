"""Tests for the private-gateway env var support in Config.from_env():
GRANDICE_LLM_BASE_URL/API_KEY/CHAT_MODEL/EMBEDDING_MODEL, and the
fail-loud-not-silent-stub behaviour when only one of the paired vars is set.

These monkeypatch os.environ directly and call Config.from_env() (rather
than going through .env/.env.local) so the test doesn't depend on this
machine's actual dotenv files."""

from __future__ import annotations

import pytest

from grandice.config import PLACEHOLDER_API_KEY, Config, ConfigError

_ALL_RELEVANT_VARS = [
    "GRANDICE_LLM_BASE_URL",
    "GRANDICE_LLM_API_KEY",
    "GRANDICE_LLM_CHAT_MODEL",
    "GRANDICE_LLM_EMBEDDING_MODEL",
    "GRANDICE_LLM_TIMEOUT_SECONDS",
    "GRANDICE_BASE_URL",
    "GRANDICE_API_KEY",
    "GRANDICE_ORCHESTRATOR",
    "GRANDICE_WORKER",
    "GRANDICE_BULK",
    "GRANDICE_REQUESTS_PER_MINUTE",
    "GRANDICE_DAILY_REQUEST_CAP",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from a blank slate for these vars, regardless of
    what this machine's own .env happens to set."""
    for var in _ALL_RELEVANT_VARS:
        monkeypatch.delenv(var, raising=False)


def test_neither_pair_set_stays_in_stub_mode():
    config = Config.from_env()
    assert not config.live
    assert config.base_url is None
    assert config.api_key is None


def test_legacy_vars_alone_still_work_unchanged(monkeypatch):
    monkeypatch.setenv("GRANDICE_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("GRANDICE_API_KEY", "sk-legacy")
    config = Config.from_env()
    assert config.live
    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.api_key == "sk-legacy"


def test_legacy_base_url_with_no_key_is_still_valid_stub_mode(monkeypatch):
    """The repo's own .env.example ships GRANDICE_BASE_URL pre-filled with an
    empty GRANDICE_API_KEY, specifically so stub mode works out of the box —
    this must not become a ConfigError."""
    monkeypatch.setenv("GRANDICE_BASE_URL", "https://openrouter.ai/api/v1")
    config = Config.from_env()
    assert not config.live


def test_private_gateway_vars_take_priority_over_legacy(monkeypatch):
    monkeypatch.setenv("GRANDICE_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("GRANDICE_API_KEY", "sk-legacy")
    monkeypatch.setenv("GRANDICE_LLM_BASE_URL", "https://ambeast.tail7156f0.ts.net/v1")
    monkeypatch.setenv("GRANDICE_LLM_API_KEY", "gll-private")
    config = Config.from_env()
    assert config.base_url == "https://ambeast.tail7156f0.ts.net/v1"
    assert config.api_key == "gll-private"


def test_chat_model_sets_all_three_tiers(monkeypatch):
    monkeypatch.setenv("GRANDICE_LLM_BASE_URL", "https://ambeast.tail7156f0.ts.net/v1")
    monkeypatch.setenv("GRANDICE_LLM_API_KEY", "gll-private")
    monkeypatch.setenv("GRANDICE_LLM_CHAT_MODEL", "chat")
    config = Config.from_env()
    assert config.tiers.orchestrator == "chat"
    assert config.tiers.worker == "chat"
    assert config.tiers.bulk == "chat"


def test_chat_model_ignores_legacy_tier_vars_on_the_private_gateway_path(monkeypatch):
    """Real bug caught by actually running this against the private gateway:
    this repo's own .env ships GRANDICE_ORCHESTRATOR pre-filled with an
    OpenRouter model id from an earlier setup. If that leaked through, the
    private gateway would receive a model name it's never heard of instead
    of GRANDICE_LLM_CHAT_MODEL's "chat" — GRANDICE_ORCHESTRATOR/WORKER/BULK
    belong to the legacy GRANDICE_BASE_URL/API_KEY path and must not apply
    once GRANDICE_LLM_CHAT_MODEL is driving the private-gateway path."""
    monkeypatch.setenv("GRANDICE_LLM_BASE_URL", "https://ambeast.tail7156f0.ts.net/v1")
    monkeypatch.setenv("GRANDICE_LLM_API_KEY", "gll-private")
    monkeypatch.setenv("GRANDICE_LLM_CHAT_MODEL", "chat")
    monkeypatch.setenv("GRANDICE_ORCHESTRATOR", "nvidia/nemotron-3-ultra-550b-a55b:free")
    config = Config.from_env()
    assert config.tiers.orchestrator == "chat"
    assert config.tiers.worker == "chat"
    assert config.tiers.bulk == "chat"


def test_legacy_tier_vars_still_apply_without_a_chat_model(monkeypatch):
    """Per-tier control on your own gateway stays possible — just don't set
    GRANDICE_LLM_CHAT_MODEL, and set the tier vars directly instead."""
    monkeypatch.setenv("GRANDICE_LLM_BASE_URL", "https://ambeast.tail7156f0.ts.net/v1")
    monkeypatch.setenv("GRANDICE_LLM_API_KEY", "gll-private")
    monkeypatch.setenv("GRANDICE_ORCHESTRATOR", "big-local-model")
    monkeypatch.setenv("GRANDICE_WORKER", "small-local-model")
    config = Config.from_env()
    assert config.tiers.orchestrator == "big-local-model"
    assert config.tiers.worker == "small-local-model"


def test_embedding_model_is_stored(monkeypatch):
    monkeypatch.setenv("GRANDICE_LLM_BASE_URL", "https://ambeast.tail7156f0.ts.net/v1")
    monkeypatch.setenv("GRANDICE_LLM_API_KEY", "gll-private")
    monkeypatch.setenv("GRANDICE_LLM_EMBEDDING_MODEL", "embed")
    config = Config.from_env()
    assert config.embedding_model == "embed"


def test_embedding_model_defaults_to_none(monkeypatch):
    monkeypatch.setenv("GRANDICE_LLM_BASE_URL", "https://ambeast.tail7156f0.ts.net/v1")
    monkeypatch.setenv("GRANDICE_LLM_API_KEY", "gll-private")
    config = Config.from_env()
    assert config.embedding_model is None


def test_unedited_placeholder_key_is_treated_as_completely_unset(monkeypatch):
    """Real bug caught by actually running this end to end: merely creating
    .env.local from the template (base_url filled in, api_key still the
    placeholder) must not flip the app into "live" mode, and must not raise
    a ConfigError either — both are truthy strings, so naive bool checks
    would get this wrong in either direction."""
    monkeypatch.setenv("GRANDICE_LLM_BASE_URL", "https://ambeast.tail7156f0.ts.net/v1")
    monkeypatch.setenv("GRANDICE_LLM_API_KEY", PLACEHOLDER_API_KEY)
    config = Config.from_env()
    assert not config.live
    assert config.base_url is None
    assert config.api_key is None


def test_only_base_url_set_raises_config_error(monkeypatch):
    monkeypatch.setenv("GRANDICE_LLM_BASE_URL", "https://ambeast.tail7156f0.ts.net/v1")
    with pytest.raises(ConfigError, match="GRANDICE_LLM_API_KEY"):
        Config.from_env()


def test_only_api_key_set_raises_config_error(monkeypatch):
    monkeypatch.setenv("GRANDICE_LLM_API_KEY", "gll-private")
    with pytest.raises(ConfigError, match="GRANDICE_LLM_BASE_URL"):
        Config.from_env()


def test_config_error_never_includes_the_key_value(monkeypatch):
    """The error message names which var is missing, not any value — a
    partial-config error must not leak the key that *was* set either."""
    monkeypatch.setenv("GRANDICE_LLM_API_KEY", "gll-super-secret-value")
    with pytest.raises(ConfigError) as exc_info:
        Config.from_env()
    assert "gll-super-secret-value" not in str(exc_info.value)


def test_private_gateway_defaults_rate_limits_much_higher_than_openrouter(monkeypatch):
    """A self-hosted box has no external rate limit — defaulting to
    OpenRouter's free-tier 18/min-50/day would silently cripple it."""
    monkeypatch.setenv("GRANDICE_LLM_BASE_URL", "https://ambeast.tail7156f0.ts.net/v1")
    monkeypatch.setenv("GRANDICE_LLM_API_KEY", "gll-private")
    config = Config.from_env()
    assert config.requests_per_minute > 1000
    assert config.daily_request_cap > 1000


def test_explicit_rate_limit_vars_still_override_the_private_gateway_default(monkeypatch):
    monkeypatch.setenv("GRANDICE_LLM_BASE_URL", "https://ambeast.tail7156f0.ts.net/v1")
    monkeypatch.setenv("GRANDICE_LLM_API_KEY", "gll-private")
    monkeypatch.setenv("GRANDICE_REQUESTS_PER_MINUTE", "5")
    monkeypatch.setenv("GRANDICE_DAILY_REQUEST_CAP", "10")
    config = Config.from_env()
    assert config.requests_per_minute == 5
    assert config.daily_request_cap == 10


def test_legacy_openrouter_path_keeps_its_original_rate_limit_defaults(monkeypatch):
    monkeypatch.setenv("GRANDICE_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("GRANDICE_API_KEY", "sk-legacy")
    config = Config.from_env()
    assert config.requests_per_minute == 18
    assert config.daily_request_cap == 50


def test_llm_timeout_seconds_has_a_sane_default(monkeypatch):
    config = Config.from_env()
    assert config.llm_timeout_seconds == 60.0


def test_llm_timeout_seconds_is_overridable(monkeypatch):
    monkeypatch.setenv("GRANDICE_LLM_TIMEOUT_SECONDS", "120")
    config = Config.from_env()
    assert config.llm_timeout_seconds == 120.0
