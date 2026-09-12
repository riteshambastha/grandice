"""Shell access, through the sandbox and never around it."""

from __future__ import annotations

from ..sandbox import Sandbox
from .base import Risk, ToolSpec

MAX_TIMEOUT = 300.0


def build(sandbox: Sandbox) -> list[ToolSpec]:
    async def bash(command: str, timeout: float = 60.0) -> str:
        result = await sandbox.run(command, timeout=min(timeout, MAX_TIMEOUT))
        return result.render()

    return [
        ToolSpec(
            name="bash",
            description=(
                "Run a bash command inside the sandbox. The workspace is the only writable "
                "path and there is no network access, so package installs and downloads will "
                "fail — say so rather than retrying them. Prefer grep, sed, awk and python3 "
                "over reading large files into context."
            ),
            schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to run."},
                    "timeout": {"type": "number", "minimum": 1, "maximum": MAX_TIMEOUT, "description": "Wall-clock cap in seconds."},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            run=bash,
            risk=Risk.WRITE,
        )
    ]
