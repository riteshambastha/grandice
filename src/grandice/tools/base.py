"""Tool layer: schema-validated in, truncated out.

Two rules from §04 are enforced here rather than in each tool. Arguments are
validated before execution, because open models hallucinate parameter names and
a malformed call must never crash the turn. Results are truncated on the way in,
because one grep over a large folder otherwise eats a third of the context.
"""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

# Tokens are roughly four characters of English. Good enough for a budget.
CHARS_PER_TOKEN = 4


class Risk(enum.Enum):
    READ = "read"        # no side effects
    WRITE = "write"      # changes the workspace, reversible
    OUTWARD = "outward"  # leaves the machine, or destroys something. Gate it (§08).


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[..., Awaitable[str]]
    risk: Risk = Risk.READ

    def as_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }

    def describe(self, arguments: dict[str, Any]) -> str:
        """What the human sees at the permission gate. Show the actual payload,
        not 'the agent wants to do something' (§08)."""
        rendered = json.dumps(arguments, indent=2, default=str)
        if len(rendered) > 1200:
            rendered = rendered[:1200] + "\n  … (truncated)"
        return f"{self.name}\n{rendered}"


class ToolBudgetExceeded(ValueError):
    """Activating a tool would push the active set past Registry.MAX_ACTIVE."""


class Registry:
    """Tool count is a quality lever. Past ~15 active tools accuracy degrades
    sharply (§05.2), so this caps rather than trusting us to be disciplined.

    Built-in and skill tools are added active by default. A connector (§P3)
    adds many tools at once — those go in latent, invisible to the model
    until `search_tools` (tools/search.py) activates the ones it needs. This
    is the same three-tier shape as skills.py: known-to-exist, then loaded
    on demand, and never all in context at once.
    """

    MAX_ACTIVE = 15

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._active: set[str] = set()

    def add(self, spec: ToolSpec, active: bool = True) -> None:
        self._tools[spec.name] = spec
        if active:
            self._active.add(spec.name)

    def get(self, name: str) -> ToolSpec | None:
        """Looks up any known tool, active or latent. A model can only ever
        emit a call for a name it was shown in `tools=` (i.e. an active one),
        so this stays permissive rather than adding an access-control layer
        the API itself already provides."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """Active tool names — what's actually offered to the model right
        now. See `latent()` for what search_tools can still surface."""
        return sorted(self._active)

    def latent(self) -> list[ToolSpec]:
        return [self._tools[n] for n in sorted(self._tools) if n not in self._active]

    def search(self, query: str) -> list[ToolSpec]:
        """Case-insensitive substring match over latent tools' name and
        description. Deliberately simple — a connector adds a handful of
        tools, not hundreds; this isn't a ranking problem yet."""
        q = query.lower().strip()
        if not q:
            return []
        return [s for s in self.latent() if q in s.name.lower() or q in s.description.lower()]

    def activate(self, name: str) -> None:
        """Move a latent tool into the active set the model is shown. Raises
        ToolBudgetExceeded rather than letting `active()`'s hard cap explode
        mid-turn — search_tools (the only caller) turns that into a normal
        tool-result message instead of a crashed loop."""
        if name not in self._tools:
            raise KeyError(f"No tool named {name!r}.")
        if name in self._active:
            return
        if len(self._active) + 1 > self.MAX_ACTIVE:
            raise ToolBudgetExceeded(
                f"Activating {name!r} would exceed the {self.MAX_ACTIVE}-tool budget."
            )
        self._active.add(name)

    def active(self) -> list[ToolSpec]:
        tools = [self._tools[n] for n in sorted(self._active)]
        if len(tools) > self.MAX_ACTIVE:
            # A safety net, not the primary guard — activate() should always
            # catch this first. Reachable only if something bypassed it.
            raise RuntimeError(
                f"{len(tools)} active tools, cap is {self.MAX_ACTIVE}. Put the rest "
                f"behind a search_tools(query) indirection (§05.2)."
            )
        return tools

    def as_openai(self) -> list[dict[str, Any]]:
        return [t.as_openai() for t in self.active()]


def validate(arguments: dict[str, Any], schema: dict[str, Any]) -> tuple[bool, str]:
    """Return (ok, message). The message goes back to the model verbatim, so it
    names what was wrong and what was expected."""
    if "__malformed__" in arguments:
        return False, (
            "arguments were not valid JSON. Emit a single JSON object matching "
            f"this schema: {json.dumps(schema)}"
        )
    errors = sorted(Draft202012Validator(schema).iter_errors(arguments), key=lambda e: e.path)
    if not errors:
        return True, ""
    parts = []
    for err in errors[:4]:
        where = ".".join(str(p) for p in err.path) or "(root)"
        parts.append(f"{where}: {err.message}")
    allowed = ", ".join(schema.get("properties", {})) or "none"
    return False, "; ".join(parts) + f". Valid parameters: {allowed}"


def truncate(text: str, budget_tokens: int, overflow_dir: Path, label: str) -> str:
    """Cap the result, spill the rest to disk, and hand the model the path.

    The pointer matters as much as the cap — a truncated result with nowhere to
    look next just makes the agent guess.
    """
    limit = budget_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text

    overflow_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(text.encode()).hexdigest()[:10]
    path = overflow_dir / f"{label}-{digest}.txt"
    path.write_text(text)

    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.2):]
    dropped = len(text) - len(head) - len(tail)
    return (
        f"{head}\n\n"
        f"… [{dropped:,} characters omitted. Full output: {path}. "
        f"Use read(path=...) with a line range, or grep it via bash, "
        f"rather than reading the whole file.] …\n\n"
        f"{tail}"
    )


def tool_result(call_id: str, name: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


def tool_error(call_id: str, name: str, message: str) -> dict[str, Any]:
    """Every failure returns to the model as a tool result rather than raising.
    The model can only recover from what it can see."""
    return tool_result(call_id, name, f"ERROR: {message}")
