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
        max_tokens: int | None = None,
    ) -> AsyncIterator[str | Reply]:
        raise NotImplementedError


class OpenAICompatBackend(Backend):
    """Every model in §03 speaks OpenAI-compatible tool calling. Point base_url
    at OpenRouter, a first-party vendor, or your own vLLM box — same code."""

    MAX_RATE_LIMIT_RETRIES = 3

    def __init__(
        self, base_url: str, api_key: str, name: str = "openai-compat", timeout: float = 60.0
    ) -> None:
        from openai import AsyncOpenAI

        self.name = name
        self._timeout = timeout
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str | Reply]:
        import httpx
        from openai import (
            APIConnectionError,
            APITimeoutError,
            AuthenticationError,
            PermissionDeniedError,
            RateLimitError,
        )

        attempt = 0
        while True:
            try:
                async for item in self._stream_once(model, messages, tools, temperature, max_tokens):
                    yield item
                return
            except httpx.RemoteProtocolError as exc:
                # Real bug, found live summarizing an uploaded CSV: the
                # model was streaming a long response and the connection
                # was cut mid-body ("peer closed connection without sending
                # complete message body") — not any openai exception type,
                # so it fell through to a generic, unhelpful "All backends
                # failed" before this. This is the gateway (or the network
                # path to it — a Tailscale tunnel included) dropping a
                # long-running connection partway through, not a token or
                # context limit on grandice's own side. No retry: the
                # partial text already streamed to the client can't be
                # cleanly un-shown, so retrying here would just append a
                # second, overlapping attempt on top of it — surfacing this
                # clearly and letting the user re-send is the honest option.
                raise RuntimeError(
                    f"{self.name} closed the connection before finishing this response "
                    f"({_error_message(exc)}). This usually means the gateway (or the "
                    f"network path to it) can't sustain a very long streaming response — try "
                    f"asking for something more targeted (e.g. a summary instead of a full "
                    f"row-by-row dump), or check the gateway's own request/timeout settings if "
                    f"this keeps happening on long generations."
                ) from exc
            except AuthenticationError as exc:
                # HTTP 401 — the key itself, not the request, is the problem.
                raise RuntimeError(
                    f"{self.name} rejected the API key as invalid or revoked (401). "
                    f"Check GRANDICE_LLM_API_KEY / GRANDICE_API_KEY. Provider said: "
                    f"{_error_message(exc)}"
                ) from exc
            except PermissionDeniedError as exc:
                # HTTP 403 — a valid key, but not for this model.
                raise RuntimeError(
                    f"{self.name} refused to run {model!r} with this key (403 — model not "
                    f"permitted). Provider said: {_error_message(exc)}"
                ) from exc
            except APITimeoutError as exc:
                # Must be caught before APIConnectionError: it's a subclass.
                raise RuntimeError(
                    f"{self.name} timed out waiting for {model!r} after {self._timeout}s. "
                    f"A self-hosted model can be slow on first load (cold start) or under load — "
                    f"raise GRANDICE_LLM_TIMEOUT_SECONDS if this happens consistently, otherwise "
                    f"retry."
                ) from exc
            except APIConnectionError as exc:
                # No response at all — the gateway's box, its Tailscale
                # tunnel, or the network between here and it is down, not
                # something the model/request caused.
                raise RuntimeError(
                    f"Could not reach {self.name} at all ({exc}). If this is a private "
                    f"gateway, check that the host machine is on and Tailscale is connected "
                    f"on both ends (`tailscale status`), and that the health endpoint "
                    f"responds."
                ) from exc
            except RateLimitError as exc:
                # A 429 is not always an actual rate limit — some providers
                # (confirmed on Z.ai) reuse the status code for "no balance
                # or active plan", where retrying can only ever fail the same
                # way. Fail fast and clearly for that case instead of
                # burning three backoff attempts on something a retry can't
                # fix.
                if _is_balance_error(exc):
                    raise RuntimeError(
                        f"{model!r} rejected the request as a billing problem, not a rate "
                        f"limit — retrying will not help. Provider said: {_error_message(exc)}"
                    ) from exc

                # The proactive limiter (§ratelimit) should make a genuine rate
                # limit rare here — this catches another process sharing the
                # same key, or the provider tightening the window without notice.
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
        max_tokens: int | None,
    ) -> AsyncIterator[str | Reply]:
        stream = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools or None,
            temperature=temperature,
            max_tokens=max_tokens,
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


# Confirmed on Z.ai: HTTP 429 body {"code": "1113", "message": "Insufficient
# balance or no resource package. Please recharge."} — a billing state, not a
# request-rate one. Phrased generically because providers word this
# differently; matched on the message text since a numeric code (like "1113")
# is provider-specific and won't generalise.
_BALANCE_ERROR_SIGNALS = (
    "insufficient balance",
    "insufficient quota",
    "no resource package",
    "recharge",
    "exceeded your current quota",
    "add a payment method",
    "billing",
)


def _error_message(exc: Exception) -> str:
    """The provider's own explanation, not the SDK's generic wrapper text."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return str(exc)


def _is_balance_error(exc: Exception) -> bool:
    return any(signal in _error_message(exc).lower() for signal in _BALANCE_ERROR_SIGNALS)


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
        max_tokens: int | None = None,
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
            self.backends = [
                OpenAICompatBackend(
                    config.base_url, config.api_key, name="primary", timeout=config.llm_timeout_seconds
                )
            ]
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
        model: str | None = None,
    ) -> AsyncIterator[str | Reply]:
        """Yields text deltas, then exactly one Reply as the final item.

        `model` overrides the tier's configured model id for just this call
        — the dashboard's model selector (§ chat.model in projects.py) uses
        this to let a chat pick any model/alias the provider actually
        exposes, rather than being locked to whatever GRANDICE_ORCHESTRATOR
        happens to be. `tier` still governs cost-ledger pricing either way.
        """
        self.ledger.check()
        if self.rate_limiter is not None:
            await self.rate_limiter.acquire()  # may raise DailyCapReached

        resolved_model = model or self.model_for(tier)
        temp = self.config.temperature if temperature is None else temperature

        last_error: Exception | None = None
        for backend in self.backends:
            try:
                async for item in backend.complete(
                    resolved_model, messages, tools or [], temp, self.config.max_output_tokens
                ):
                    if isinstance(item, Reply):
                        self.ledger.record(tier, item.prompt_tokens, item.completion_tokens)
                    yield item
                return
            except Exception as exc:  # noqa: BLE001 — failover is the point
                last_error = exc
                continue
        raise RuntimeError(f"All backends failed. Last error: {last_error}")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embeddings via the same OpenAI-compatible base_url/api_key as chat
        completions, against `config.embedding_model` — no tool or feature in
        grandice consumes this yet (there's no RAG/retrieval tool), but it's
        real, callable config for a gateway that offers one, not a dead
        setting. Not routed through `self.backends`/the stub: embeddings
        don't make sense to script, so this requires live config and an
        embedding_model, rather than silently no-op'ing."""
        if not self.config.live:
            raise RuntimeError(
                "embed() needs a live model config (GRANDICE_LLM_BASE_URL/API_KEY or "
                "GRANDICE_BASE_URL/API_KEY) — none is set."
            )
        if not self.config.embedding_model:
            raise RuntimeError(
                "embed() needs GRANDICE_LLM_EMBEDDING_MODEL set to the gateway's embedding "
                "model id."
            )

        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            timeout=self.config.llm_timeout_seconds,
        )
        response = await client.embeddings.create(model=self.config.embedding_model, input=texts)
        return [item.embedding for item in response.data]

    async def list_models(self) -> list[str]:
        """The provider's own model/alias list (GET /v1/models) — lets the
        dashboard's model selector show whatever a given gateway actually
        exposes (e.g. a self-hosted box's "chat"/"code"/"vision" aliases)
        rather than a hardcoded guess. Falls back to just the configured
        tiers if the provider doesn't support the endpoint or isn't live —
        the selector should never come up empty."""
        fallback = sorted({self.config.tiers.orchestrator, self.config.tiers.worker, self.config.tiers.bulk})
        if not self.config.live:
            return fallback

        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            timeout=self.config.llm_timeout_seconds,
        )
        try:
            response = await client.models.list()
            ids = sorted({m.id for m in response.data})
            return ids or fallback
        except Exception:  # noqa: BLE001 — a broken /models endpoint shouldn't break the selector
            return fallback
