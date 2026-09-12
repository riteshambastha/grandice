"""Model router (§03).

One interface in front of every provider. Per-step model choice, a per-session
cost cap, and automatic failover. Everything left of this file is ours;
everything right of it is a config string.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .ratelimit import RateLimiter

# Rough $/Mtok (in, out) by tier, §11. Prices drift — treat as an estimate for
# the cap, not an invoice.
PRICING: dict[str, tuple[float, float]] = {
    "orchestrator": (0.50, 2.00),
    "worker": (0.30, 1.15),
    "bulk": (0.10, 0.55),
}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""

    @property
    def message(self) -> dict[str, Any]:
        """The assistant turn as it goes back into the transcript."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in self.tool_calls
            ]
        return msg


class CostCapExceeded(RuntimeError):
    """Raised when a session's spend passes the configured ceiling."""


@dataclass
class Ledger:
    """Per-session spend. An agent stuck in a retry loop at 3am is an
    expensive way to learn you needed this (§11)."""

    cap_usd: float
    spent_usd: float = 0.0
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def record(self, tier: str, prompt_tokens: int, completion_tokens: int) -> None:
        rate_in, rate_out = PRICING.get(tier, PRICING["orchestrator"])
        self.spent_usd += (prompt_tokens * rate_in + completion_tokens * rate_out) / 1e6
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens

    def check(self) -> None:
        if self.spent_usd >= self.cap_usd:
            raise CostCapExceeded(
                f"Session spent ~${self.spent_usd:.2f}, cap is ${self.cap_usd:.2f}. "
                f"Raise GRANDICE_COST_CAP_USD to continue."
            )


class Backend:
    """A provider. Implementations yield text deltas and return a Reply."""

    name: str = "backend"

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float,
    ) -> AsyncIterator[str | Reply]:
        raise NotImplementedError


class OpenAICompatBackend(Backend):
    """Every model in §03 speaks OpenAI-compatible tool calling. Point base_url
    at OpenRouter, a first-party vendor, or your own vLLM box — same code."""

    MAX_RATE_LIMIT_RETRIES = 3

    def __init__(self, base_url: str, api_key: str, name: str = "openai-compat") -> None:
        from openai import AsyncOpenAI

        self.name = name
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float,
    ) -> AsyncIterator[str | Reply]:
        from openai import RateLimitError

        attempt = 0
        while True:
            try:
                async for item in self._stream_once(model, messages, tools, temperature):
                    yield item
                return
            except RateLimitError as exc:
                # The proactive limiter (§ratelimit) should make this rare — it
                # catches the case of another process sharing the same key, or
                # the provider tightening the window without notice.
                attempt += 1
                if attempt > self.MAX_RATE_LIMIT_RETRIES:
                    raise RuntimeError(
                        f"Rate-limited {attempt - 1} times in a row by {model!r}. "
                        f"The free tier's 20/min cap is likely shared with another "
                        f"process. Wait a minute, or reduce concurrency."
                    ) from exc
                wait = _retry_after_seconds(exc) or (2**attempt)
                await asyncio.sleep(wait)

    async def _stream_once(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float,
    ) -> AsyncIterator[str | Reply]:
        stream = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools or None,
            temperature=temperature,
            stream=True,
            stream_options={"include_usage": True},
        )

        text_parts: list[str] = []
        # Tool calls arrive fragmented across deltas, indexed by position.
        partial: dict[int, dict[str, str]] = {}
        prompt_tokens = completion_tokens = 0

        async for chunk in stream:
            if chunk.usage:
                prompt_tokens = chunk.usage.prompt_tokens
                completion_tokens = chunk.usage.completion_tokens
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                text_parts.append(delta.content)
                yield delta.content
            for tc in delta.tool_calls or []:
                slot = partial.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["args"] += tc.function.arguments

        yield Reply(
            text="".join(text_parts),
            tool_calls=[_parse_call(s) for s in partial.values()],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
        )


def _retry_after_seconds(exc: Exception) -> float | None:
    """Honour a provider's Retry-After header when it gives one."""
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}).get("retry-after") if response else None
    try:
        return float(header) if header else None
    except ValueError:
        return None


def _parse_call(slot: dict[str, str]) -> ToolCall:
    """Open models emit malformed JSON often enough that this must not raise.
    An unparseable blob becomes an argument error the model can see and fix."""
    try:
        args = json.loads(slot["args"] or "{}")
        if not isinstance(args, dict):
            args = {"__malformed__": slot["args"]}
    except json.JSONDecodeError:
        args = {"__malformed__": slot["args"]}
    return ToolCall(id=slot["id"] or "call_0", name=slot["name"], arguments=args)


class StubBackend(Backend):
    """No key, no network, no bill — but a real trip through the loop.

    Walks a fixed script so you can watch tool dispatch, validation, truncation
    and the permission gate work before you have a provider account.
    """

    name = "stub"

    def __init__(self) -> None:
        self._turn = 0

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float,
    ) -> AsyncIterator[str | Reply]:
        self._turn += 1
        names = {t["function"]["name"] for t in tools}

        if self._turn == 1 and "glob" in names:
            text = "Looking at what's in the workspace first.\n"
            for part in text.split(" "):
                yield part + " "
            yield Reply(
                text=text,
                tool_calls=[ToolCall("stub_1", "glob", {"pattern": "**/*"})],
                prompt_tokens=900,
                completion_tokens=40,
                model="stub",
            )
            return

        text = (
            "[stub router] No GRANDICE_API_KEY set, so this is a scripted reply.\n"
            "The loop, tools, validation and cost ledger are all real — only the "
            "model is fake. Add a key to .env to run this against a real model.\n"
        )
        for line in text.splitlines(keepends=True):
            yield line
        yield Reply(text=text, prompt_tokens=950, completion_tokens=70, model="stub")


class Router:
    """Tries each backend in order. A provider outage should degrade, not stop.

    The stub is deliberately NOT in the live failover chain. Falling back to
    scripted text on a real error would look, from inside the loop, exactly
    like the model responding — the session would silently stop meaning
    anything. Live mode fails loudly instead; only "no key configured" gets
    the stub.
    """

    def __init__(
        self,
        config: Config,
        backends: list[Backend] | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.config = config
        self.ledger = Ledger(cap_usd=config.cost_cap_usd)
        if backends is not None:
            self.backends = backends
        elif config.live:
            self.backends = [OpenAICompatBackend(config.base_url, config.api_key, name="primary")]
        else:
            self.backends = [StubBackend()]

        self.rate_limiter = rate_limiter if rate_limiter is not None else (
            RateLimiter(
                per_minute=config.requests_per_minute,
                per_day=config.daily_request_cap,
                state_path=config.workspace.parent / ".grandice" / "rate_limit.json",
            )
            if config.live
            else None
        )

    def model_for(self, tier: str) -> str:
        return getattr(self.config.tiers, tier)

    async def complete(
        self,
        tier: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str | Reply]:
        """Yields text deltas, then exactly one Reply as the final item."""
        self.ledger.check()
        if self.rate_limiter is not None:
            await self.rate_limiter.acquire()  # may raise DailyCapReached

        model = self.model_for(tier)
        temp = self.config.temperature if temperature is None else temperature

        last_error: Exception | None = None
        for backend in self.backends:
            try:
                async for item in backend.complete(model, messages, tools or [], temp):
                    if isinstance(item, Reply):
                        self.ledger.record(tier, item.prompt_tokens, item.completion_tokens)
                    yield item
                return
            except Exception as exc:  # noqa: BLE001 — failover is the point
                last_error = exc
                continue
        raise RuntimeError(f"All backends failed. Last error: {last_error}")
