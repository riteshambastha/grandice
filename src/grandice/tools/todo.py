"""The todo tool (§05.5).

An explicit, model-written plan the agent reads back each turn is the single
largest lever on long-horizon coherence for weaker models. It is worth more than
a model upgrade, and it costs about eighty lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import Risk, ToolSpec

STATES = ("pending", "in_progress", "done", "blocked")
MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "blocked": "[!]"}


@dataclass
class TodoList:
    items: list[dict[str, str]] = field(default_factory=list)

    def render(self) -> str:
        if not self.items:
            return "(no plan yet)"
        return "\n".join(
            f"{i}. {MARKS[it['status']]} {it['task']}" for i, it in enumerate(self.items, 1)
        )

    def summary(self) -> str:
        if not self.items:
            return ""
        done = sum(1 for i in self.items if i["status"] == "done")
        return f"{done}/{len(self.items)} done"


def build(todos: TodoList) -> list[ToolSpec]:
    async def todo_write(items: list[dict[str, Any]]) -> str:
        cleaned = []
        for raw in items:
            status = str(raw.get("status", "pending"))
            if status not in STATES:
                return f"ERROR: status {status!r} is not one of {', '.join(STATES)}."
            cleaned.append({"task": str(raw["task"]), "status": status})

        in_progress = [i for i in cleaned if i["status"] == "in_progress"]
        if len(in_progress) > 1:
            return (
                f"ERROR: {len(in_progress)} items marked in_progress. Work one at a "
                f"time — mark the rest pending."
            )

        todos.items = cleaned
        return f"Plan updated ({todos.summary()}):\n{todos.render()}"

    return [
        ToolSpec(
            name="todo",
            description=(
                "Write or rewrite your plan for the current task. Call this once at the "
                "start with the full list of steps, then again after each step to mark it "
                "done and start the next. Exactly one item may be in_progress. The plan is "
                "shown back to you every turn — keep it accurate and you will not lose track."
            ),
            schema={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "task": {"type": "string", "description": "One concrete step."},
                                "status": {"type": "string", "enum": list(STATES)},
                            },
                            "required": ["task", "status"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["items"],
                "additionalProperties": False,
            },
            run=todo_write,
            risk=Risk.READ,
        )
    ]
