"""Shared fixtures across the whole test suite.

Autouse, not opt-in: this repo's own .env/.env.local carry real values —
.env has OpenRouter-tuned defaults from the P1 setup, and .env.local can
carry a real private-gateway key once a user has actually configured one
(see config.py's Config.from_env()). Without this, any test that builds a
Config without manually clearing every one of these vars could silently
pick up real credentials and make a live network call during `pytest`.

Real bug this guards against: after filling in .env.local with a real
gateway key, three existing tests started making live network calls to that
gateway mid-test-run, because they only cleared the legacy GRANDICE_API_KEY/
GRANDICE_BASE_URL, not the newer GRANDICE_LLM_API_KEY/BASE_URL pair that
now takes priority over them.
"""

from __future__ import annotations

import pytest

_LIVE_MODE_VARS = [
    "GRANDICE_API_KEY",
    "GRANDICE_BASE_URL",
    "GRANDICE_LLM_API_KEY",
    "GRANDICE_LLM_BASE_URL",
]


@pytest.fixture(autouse=True)
def _no_accidental_live_calls(monkeypatch):
    """Every test starts with live-mode config vars cleared. A test that
    genuinely needs a live or partially-live Config sets exactly what it
    needs itself, afterward, in its own body — that still works normally,
    since this only runs before the test, not after."""
    for var in _LIVE_MODE_VARS:
        monkeypatch.delenv(var, raising=False)
