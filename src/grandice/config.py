"""Configuration. Everything the router and sandbox need, read once from env."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

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
    tool_result_budget: int = 2_000
    reinject_every: int = 6

    # Economics (§11). On the free tier this ledger reads ~$0 — the real
    # ceiling is the rate limit below, not the dollar cap.
    cost_cap_usd: float = 2.00

    # OpenRouter free tier: 20 req/min always; 50/day free, 1,000/day after a
    # one-time (non-recurring) $10 credit purchase. Only applied when live.
    requests_per_minute: int = 18
    daily_request_cap: int = 50

    @property
    def live(self) -> bool:
        """False means the stub router — the loop still runs, nothing is billed."""
        return bool(self.api_key and self.base_url)

    @classmethod
    def from_env(cls) -> Config:
        default_sandbox = "sandbox-exec" if sys.platform == "darwin" else "docker"
        return cls(
            base_url=os.getenv("GRANDICE_BASE_URL") or None,
            api_key=os.getenv("GRANDICE_API_KEY") or None,
            tiers=Tiers(
                orchestrator=os.getenv("GRANDICE_ORCHESTRATOR", Tiers.orchestrator),
                worker=os.getenv("GRANDICE_WORKER", Tiers.worker),
                bulk=os.getenv("GRANDICE_BULK", Tiers.bulk),
            ),
            workspace=Path(os.getenv("GRANDICE_WORKSPACE", "workspace")).resolve(),
            sandbox=os.getenv("GRANDICE_SANDBOX", default_sandbox),
            sandbox_image=os.getenv("GRANDICE_SANDBOX_IMAGE", "grandice-sandbox:py3.12"),
            cost_cap_usd=float(os.getenv("GRANDICE_COST_CAP_USD", "2.00")),
            requests_per_minute=int(os.getenv("GRANDICE_REQUESTS_PER_MINUTE", "18")),
            daily_request_cap=int(os.getenv("GRANDICE_DAILY_REQUEST_CAP", "50")),
        )
