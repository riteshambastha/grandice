"""Configuration. Everything the router and sandbox need, read once from env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Tiers:
    """Per-step model choice (§03). Ids are provider strings, nothing more."""

    orchestrator: str = "z-ai/glm-4.6"
    worker: str = "minimax/minimax-m2"
    bulk: str = "openai/gpt-oss-120b"


@dataclass(frozen=True)
class Config:
    base_url: str | None = None
    api_key: str | None = None
    tiers: Tiers = field(default_factory=Tiers)

    workspace: Path = Path("workspace")
    sandbox: str = "sandbox-exec"

    # Loop limits (§04).
    max_steps: int = 120
    max_tool_retries: int = 3
    temperature: float = 0.2

    # Context limits (§05.3, §05.4).
    context_window: int = 128_000
    compact_at: float = 0.70
    tool_result_budget: int = 2_000
    reinject_every: int = 6

    # Economics (§11).
    cost_cap_usd: float = 2.00

    @property
    def live(self) -> bool:
        """False means the stub router — the loop still runs, nothing is billed."""
        return bool(self.api_key and self.base_url)

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            base_url=os.getenv("GRANDICE_BASE_URL") or None,
            api_key=os.getenv("GRANDICE_API_KEY") or None,
            tiers=Tiers(
                orchestrator=os.getenv("GRANDICE_ORCHESTRATOR", Tiers.orchestrator),
                worker=os.getenv("GRANDICE_WORKER", Tiers.worker),
                bulk=os.getenv("GRANDICE_BULK", Tiers.bulk),
            ),
            workspace=Path(os.getenv("GRANDICE_WORKSPACE", "workspace")).resolve(),
            sandbox=os.getenv("GRANDICE_SANDBOX", "sandbox-exec"),
            cost_cap_usd=float(os.getenv("GRANDICE_COST_CAP_USD", "2.00")),
        )
