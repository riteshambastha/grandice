#!/usr/bin/env python3
"""One real call through grandice's actual Router/Config path, against
whatever model is configured live (normally the private gateway in
.env.local) — proves the wiring end to end without going through the full
agentic loop/sandbox, since a plain reply needs neither.

Never prints the API key, in any code path — only the model's reply text
and grandice's own error messages, which name missing/invalid config by
variable name, not by value. Run it yourself after editing .env.local:

    .venv/bin/python scripts/smoke_test_llm.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from grandice.config import PLACEHOLDER_API_KEY, Config, ConfigError
from grandice.router import Reply, Router

EXPECTED_REPLY = "LAPTOP APPLICATION CONNECTED"
PROMPT = f"Reply exactly: {EXPECTED_REPLY}"


async def main() -> int:
    # Checked against the raw env var, not config.api_key: Config.from_env()
    # itself treats an unedited placeholder as "unset" (see config.py), so by
    # the time it returns, this specific case is indistinguishable from
    # never having set GRANDICE_LLM_API_KEY at all — this check exists to
    # give that specific case a more useful message than "not configured".
    if os.getenv("GRANDICE_LLM_API_KEY") == PLACEHOLDER_API_KEY:
        print(
            "GRANDICE_LLM_API_KEY in .env.local still has the placeholder value — "
            "edit that file and replace it with your real gateway key first.",
            file=sys.stderr,
        )
        return 1

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    if not config.live:
        print(
            "No live model is configured (GRANDICE_LLM_BASE_URL/API_KEY or "
            "GRANDICE_BASE_URL/API_KEY are both unset) — nothing to smoke-test.",
            file=sys.stderr,
        )
        return 1

    print(f"Calling {config.tiers.orchestrator!r} at {config.base_url} ...")
    router = Router(config)

    try:
        reply: Reply | None = None
        async for item in router.complete(
            tier="orchestrator", messages=[{"role": "user", "content": PROMPT}]
        ):
            if isinstance(item, Reply):
                reply = item
    except RuntimeError as exc:
        # grandice's own router.py already turns 401/403/timeout/connection
        # failures into a clear, specific message — surface it as-is.
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    assert reply is not None
    text = reply.text.strip()
    print(f"Response: {text!r}")

    if text == EXPECTED_REPLY:
        print("PASS: exact match.")
        return 0

    print(f"FAIL: expected exactly {EXPECTED_REPLY!r}.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
