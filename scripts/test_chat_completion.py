#!/usr/bin/env python3
"""A small, non-streaming request against whichever OpenAI-compatible
provider Config.from_env() resolves to (GRANDICE_BASE_URL/API_KEY, or
GRANDICE_LLM_BASE_URL/API_KEY if that's what's active — see config.py) —
proof that a plain, non-agentic request round-trips correctly, separate
from the full loop's streaming behavior (which router.py's OpenAICompatBackend
always uses; this script bypasses it deliberately for a simpler check).

Never prints the API key, in any code path. Run it yourself:

    .venv/bin/python scripts/test_chat_completion.py
    .venv/bin/python scripts/test_chat_completion.py "some other prompt"
"""

from __future__ import annotations

import asyncio
import sys

from grandice.config import Config, ConfigError


async def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    if not config.live:
        print(
            "No live model is configured (GRANDICE_LLM_BASE_URL/API_KEY or "
            "GRANDICE_BASE_URL/API_KEY are both unset) — nothing to test.",
            file=sys.stderr,
        )
        return 1

    prompt = sys.argv[1] if len(sys.argv) > 1 else "Reply with a short greeting."
    model = config.tiers.orchestrator
    print(f"Non-streaming request to {model!r} at {config.base_url} ...")

    from openai import (
        APIConnectionError,
        APITimeoutError,
        AsyncOpenAI,
        AuthenticationError,
        PermissionDeniedError,
    )

    client = AsyncOpenAI(
        base_url=config.base_url, api_key=config.api_key, timeout=config.llm_timeout_seconds
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
    except AuthenticationError as exc:
        print(f"FAIL: invalid or revoked API key (401). {exc}", file=sys.stderr)
        return 1
    except PermissionDeniedError as exc:
        print(f"FAIL: {model!r} not permitted with this key (403). {exc}", file=sys.stderr)
        return 1
    except APITimeoutError:
        print(
            f"FAIL: timed out after {config.llm_timeout_seconds}s. A self-hosted model can be "
            f"slow on a cold start — raise GRANDICE_LLM_TIMEOUT_SECONDS if this keeps happening.",
            file=sys.stderr,
        )
        return 1
    except APIConnectionError as exc:
        print(
            f"FAIL: could not reach the gateway at all ({exc}). Check the host is up and "
            f"reachable (Tailscale connected, DNS resolving, etc.).",
            file=sys.stderr,
        )
        return 1

    text = (response.choices[0].message.content or "").strip()
    print(f"Response: {text!r}")
    print("PASS: request completed." if text else "FAIL: empty response.")
    return 0 if text else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
