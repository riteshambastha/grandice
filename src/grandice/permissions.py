"""Permission gate (§08).

Per-action, not per-session, and it shows the actual payload. "The agent wants to
send an email" is not a decision a person can make; the recipient and the body are.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .tools.base import Risk, ToolSpec

Asker = Callable[[str], Awaitable[bool]]


class Gate:
    def __init__(self, ask: Asker, auto_approve: bool = False) -> None:
        self._ask = ask
        self.auto_approve = auto_approve
        self.log: list[tuple[str, bool]] = []

    async def allows(self, spec: ToolSpec, arguments: dict) -> bool:
        if spec.risk is not Risk.OUTWARD:
            return True
        if self.auto_approve:
            self.log.append((spec.name, True))
            return True
        approved = await self._ask(spec.describe(arguments))
        self.log.append((spec.name, approved))
        return approved


async def always_allow(_: str) -> bool:
    return True


async def always_deny(_: str) -> bool:
    return False
