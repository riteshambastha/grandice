"""Turn loop.py's Event dataclasses into the JSON the dashboard consumes."""

from __future__ import annotations

from typing import Any

from .. import loop as agent_loop


def event_to_dict(event: agent_loop.Event) -> dict[str, Any]:
    match event:
        case agent_loop.TextDelta(text):
            return {"type": "text_delta", "text": text}
        case agent_loop.ToolStarted(name, arguments):
            return {"type": "tool_started", "name": name, "arguments": arguments}
        case agent_loop.ToolFinished(name, ok, preview):
            return {"type": "tool_finished", "name": name, "ok": ok, "preview": preview}
        case agent_loop.Finished(reason, steps, cost_usd):
            return {"type": "finished", "reason": reason, "steps": steps, "cost_usd": cost_usd}
    raise TypeError(f"Unhandled event type: {type(event)}")  # pragma: no cover
