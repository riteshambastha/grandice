"""Shared fixtures across the whole test suite.

Autouse, not opt-in: this repo's own .env/.env.local carry real, evolving
values from actual local use — a private gateway key, a production base
URL, raised rate limits, enabled MCP connectors, and whatever else gets
added next. Without this, any test that builds a Config without manually
clearing every one of these vars could silently pick up real settings —
live credentials, a real connector, a skipped rate limit — that the test
never intended to exercise.

This has concretely happened three separate times as .env/.env.local
picked up more real settings over time, each only caught by actually
running the affected test and seeing it behave unexpectedly:
1. GRANDICE_LLM_API_KEY/BASE_URL (once real) made tests make live network
   calls, because they only cleared the legacy GRANDICE_API_KEY/BASE_URL.
2. GRANDICE_ORCHESTRATOR/WORKER/BULK and GRANDICE_REQUESTS_PER_MINUTE/
   DAILY_REQUEST_CAP (OpenRouter-tuned defaults from the original .env)
   leaked into private-gateway-path tests that assumed a clean slate.
3. GRANDICE_MCP_CONNECTORS=fetch (enabled for real local testing) made a
   "no connectors configured" test fail, since it wasn't in any
   hand-maintained list of vars to clear.

Rather than keep extending a hardcoded list every time this happens again,
this clears every GRANDICE_*-prefixed env var before each test — a test
that genuinely needs a specific value sets it explicitly in its own body,
which still works normally since this only runs before the test, not after.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_grandice_env(monkeypatch):
    """Every test starts with a completely clean GRANDICE_* environment,
    regardless of what this machine's own .env/.env.local happen to set."""
    import os

    for var in list(os.environ):
        if var.startswith("GRANDICE_"):
            monkeypatch.delenv(var, raising=False)
