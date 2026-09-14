"""Configuration. Everything the router and sandbox need, read once from env."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

load_dotenv()
# A second, more-specific env file for a private/self-hosted model gateway
# (e.g. a Tailscale-tunneled Ollama box) — kept separate from `.env` so a
# real gateway key never has to sit in the same file as everything else,
# and separately gitignored for the same reason. Loaded with override=True:
# when both files set the same var, the private-gateway file wins, since a
# deliberately-created `.env.local` signals a specific intent to point at
# that gateway. Both files start out gitignored; see `.gitignore`.
_env_local = find_dotenv(".env.local", usecwd=True)
if _env_local:
    load_dotenv(_env_local, override=True)

# The exact placeholder .env.local ships with for GRANDICE_LLM_API_KEY —
# recognized here (not just by scripts/smoke_test_llm.py) so an unedited
# .env.local never accidentally looks "live". Keep this in sync with the
# literal value in .env.local's own template.
PLACEHOLDER_API_KEY = "REPLACE_WITH_YOUR_GLL_KEY"


class ConfigError(RuntimeError):
    """Raised when environment configuration is present but contradictory —
    e.g. a base URL with no key, or vice versa. Deliberately louder than
    silently falling back to the stub router: a half-set private-gateway
    config almost always means the person configuring it made a mistake,
    not that they meant to run without a real model."""

# Free-tier OpenRouter picks, chosen from a live catalog fetch (2026-09-12) by
# provider-stated fit, not from training-data memory — this roster "shifts
# constantly" per OpenRouter's own docs. Re-check openrouter.ai/models before
# trusting these past a few weeks; a model that is free today may be repriced
# or pulled tomorrow, and the router will simply error when that happens.
_DEFAULT_ORCHESTRATOR = "nvidia/nemotron-3-ultra-550b-a55b:free"  # billed as "orchestration"
_DEFAULT_WORKER = "nvidia/nemotron-3-super-120b-a12b:free"        # billed as "multi-agent"
_DEFAULT_BULK = "nvidia/nemotron-3.5-lightning:free"              # billed as "high-throughput"


@dataclass(frozen=True)
class Tiers:
    """Per-step model choice (§03). Ids are provider strings, nothing more."""

    orchestrator: str = _DEFAULT_ORCHESTRATOR
    worker: str = _DEFAULT_WORKER
    bulk: str = _DEFAULT_BULK


@dataclass(frozen=True)
class Config:
    base_url: str | None = None
    api_key: str | None = None
    tiers: Tiers = field(default_factory=Tiers)

    workspace: Path = Path("workspace")
    sandbox: str = "sandbox-exec"
    sandbox_image: str = "grandice-sandbox:py3.12"  # only used by the docker backend; build via sandbox/Dockerfile

    # Loop limits (§04).
    max_steps: int = 120
    max_tool_retries: int = 3
    temperature: float = 0.2

    # Context limits (§05.3, §05.4).
    context_window: int = 128_000
    compact_at: float = 0.70
    # Real bug, found live: a tool result (e.g. `read` on an uploaded CSV)
    # over this was silently cut to ~8,000 characters — fine as a safety
    # net against one huge result blowing the whole context window (the
    # rest still lands in an overflow file the model is told the path to,
    # so nothing is actually lost), but 2,000 tokens is small enough to
    # bite on perfectly ordinary files. Raised, and now configurable.
    tool_result_budget: int = 16_000
    reinject_every: int = 6

    # Real bug, found live: no max_tokens was ever sent on a completion
    # request at all, leaving each provider's own default output-length
    # cap in charge — confirmed as the cause of a summary that started
    # fine and then stopped mid-sentence. Generous by default; raise
    # further if a specific model's own context size allows more.
    max_output_tokens: int = 16_000

    # Economics (§11). On the free tier this ledger reads ~$0 — the real
    # ceiling is the rate limit below, not the dollar cap.
    cost_cap_usd: float = 2.00

    # OpenRouter free tier: 20 req/min always; 50/day free, 1,000/day after a
    # one-time (non-recurring) $10 credit purchase. Only applied when live.
    requests_per_minute: int = 18
    daily_request_cap: int = 50

    # MCP connectors (§P3). Empty by default — `mcp` is only imported at all
    # if this is non-empty, so a build with no connectors configured never
    # needs the optional `mcp` extras installed.
    mcp_connectors: tuple[str, ...] = ()
    mcp_sqlite_path: str = "workspace.db"  # relative to the workspace

    # Subagents (§P4). A smaller cap than max_steps — bounded, mechanical
    # work is the point; a subagent that needs 120 steps should probably be
    # the orchestrator's own job instead.
    subagent_max_steps: int = 40

    # A private/self-hosted gateway's embedding model, if it has one — no
    # feature in grandice consumes this yet (there's no RAG/retrieval tool),
    # but it's real, callable config: see `router.embed()`.
    embedding_model: str | None = None

    # Reasonable default for a self-hosted model that may run on CPU and be
    # much slower than a hosted API — the OpenAI SDK's own default (10
    # minutes) is too patient to double as "the gateway is unreachable."
    llm_timeout_seconds: float = 60.0

    @property
    def live(self) -> bool:
        """False means the stub router — the loop still runs, nothing is billed."""
        return bool(self.api_key and self.base_url)

    @classmethod
    def from_env(cls) -> Config:
        default_sandbox = "sandbox-exec" if sys.platform == "darwin" else "docker"

        # GRANDICE_LLM_BASE_URL/API_KEY (a private/self-hosted gateway, e.g. a
        # Tailscale-tunneled Ollama box) take priority over the older
        # GRANDICE_BASE_URL/API_KEY (OpenRouter or a first-party vendor) when
        # both are set — either scheme points at the same Config fields,
        # since the router only ever needs one base_url/api_key pair.
        llm_base_url = os.getenv("GRANDICE_LLM_BASE_URL")
        llm_api_key = os.getenv("GRANDICE_LLM_API_KEY")
        if llm_api_key == PLACEHOLDER_API_KEY:
            # .env.local ships with this placeholder so the file (and a
            # working, error-free stub-mode app) exists before anyone edits
            # it — treat an unedited placeholder as the whole pair being
            # unset, not as "half configured." Real bug this guards against:
            # without it, merely creating .env.local from the template
            # flipped every grandice run (including the test suite) into
            # "live" mode against an unreachable host, since a non-empty
            # placeholder string is still truthy.
            llm_base_url = None
            llm_api_key = None
        if bool(llm_base_url) != bool(llm_api_key):
            raise ConfigError(
                "GRANDICE_LLM_BASE_URL and GRANDICE_LLM_API_KEY must both be set together "
                f"to use a private model gateway — only {'GRANDICE_LLM_BASE_URL' if llm_base_url else 'GRANDICE_LLM_API_KEY'} "
                "is set. Set both in .env.local, or unset both to fall back to "
                "GRANDICE_BASE_URL/GRANDICE_API_KEY (or the stub router if those are unset too)."
            )
        base_url = llm_base_url or os.getenv("GRANDICE_BASE_URL") or None
        api_key = llm_api_key or os.getenv("GRANDICE_API_KEY") or None

        # A gateway that only exposes one chat model (the common case for a
        # self-hosted box) sets all three tiers to it. Deliberately NOT
        # falling through to GRANDICE_ORCHESTRATOR/WORKER/BULK here: those
        # vars belong to the legacy GRANDICE_BASE_URL/API_KEY path, and this
        # repo's own .env already ships them pre-filled with OpenRouter model
        # ids from an earlier setup — letting those leak through would send
        # a real OpenRouter model name to a gateway that has never heard of
        # it (caught by actually running this against the real gateway: the
        # tier silently stayed "nvidia/nemotron-3-..." instead of "chat").
        # Per-tier control on your own gateway is still possible — just don't
        # set GRANDICE_LLM_CHAT_MODEL and set GRANDICE_ORCHESTRATOR/WORKER/
        # BULK directly to your own model ids instead.
        using_private_gateway = bool(llm_base_url and llm_api_key)
        chat_model = os.getenv("GRANDICE_LLM_CHAT_MODEL")

        if using_private_gateway and chat_model:
            tiers = Tiers(orchestrator=chat_model, worker=chat_model, bulk=chat_model)
        else:
            tiers = Tiers(
                orchestrator=os.getenv("GRANDICE_ORCHESTRATOR", Tiers.orchestrator),
                worker=os.getenv("GRANDICE_WORKER", Tiers.worker),
                bulk=os.getenv("GRANDICE_BULK", Tiers.bulk),
            )

        # The 18/min-50/day defaults are OpenRouter-free-tier-specific (see
        # .env.example) — a private, self-hosted gateway has no such limit.
        # Same reasoning as the tier vars above, and the same real bug this
        # guards against: this repo's own .env has GRANDICE_REQUESTS_PER_
        # MINUTE=18/GRANDICE_DAILY_REQUEST_CAP=50 set explicitly from the
        # OpenRouter setup, which would otherwise silently throttle a local
        # box that has no such limit at all — caught by actually watching
        # the dashboard report "1 / 50 requests" against the live private
        # gateway. So, like the tier vars, these are ignored on the private-
        # gateway path rather than falling through to whatever the legacy
        # path happened to have configured.
        if using_private_gateway:
            requests_per_minute = 100_000
            daily_request_cap = 100_000
        else:
            requests_per_minute = int(os.getenv("GRANDICE_REQUESTS_PER_MINUTE", "18"))
            daily_request_cap = int(os.getenv("GRANDICE_DAILY_REQUEST_CAP", "50"))

        return cls(
            base_url=base_url,
            api_key=api_key,
            tiers=tiers,
            workspace=Path(os.getenv("GRANDICE_WORKSPACE", "workspace")).resolve(),
            sandbox=os.getenv("GRANDICE_SANDBOX", default_sandbox),
            sandbox_image=os.getenv("GRANDICE_SANDBOX_IMAGE", "grandice-sandbox:py3.12"),
            cost_cap_usd=float(os.getenv("GRANDICE_COST_CAP_USD", "2.00")),
            requests_per_minute=requests_per_minute,
            daily_request_cap=daily_request_cap,
            mcp_connectors=tuple(
                c.strip() for c in os.getenv("GRANDICE_MCP_CONNECTORS", "").split(",") if c.strip()
            ),
            mcp_sqlite_path=os.getenv("GRANDICE_MCP_SQLITE_PATH", "workspace.db"),
            subagent_max_steps=int(os.getenv("GRANDICE_SUBAGENT_MAX_STEPS", "40")),
            embedding_model=os.getenv("GRANDICE_LLM_EMBEDDING_MODEL") or None,
            llm_timeout_seconds=float(os.getenv("GRANDICE_LLM_TIMEOUT_SECONDS", "60")),
            tool_result_budget=int(os.getenv("GRANDICE_TOOL_RESULT_BUDGET", "16000")),
            max_output_tokens=int(os.getenv("GRANDICE_MAX_OUTPUT_TOKENS", "16000")),
        )
