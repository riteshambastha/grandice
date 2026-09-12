"""search_tools: the same on-demand-loading trick as skills, applied to
tools (§05.2). A connector can add many tools at once; this is what keeps
them out of the active set — and thus out of context — until something
actually needs one."""

from __future__ import annotations

from .base import Registry, Risk, ToolBudgetExceeded, ToolSpec

MAX_ACTIVATIONS_PER_CALL = 5


def build(registry: Registry) -> list[ToolSpec]:
    async def search_tools(query: str) -> str:
        matches = registry.search(query)
        if not matches:
            latent = [s.name for s in registry.latent()]
            return (
                f"No latent tool matches {query!r}."
                + (f" Latent tools: {', '.join(latent)}." if latent else " No latent tools installed.")
            )

        activated: list[ToolSpec] = []
        skipped: list[str] = []
        for spec in matches[:MAX_ACTIVATIONS_PER_CALL]:
            try:
                registry.activate(spec.name)
                activated.append(spec)
            except ToolBudgetExceeded:
                skipped.append(spec.name)
                break  # further activations would only fail the same way

        lines = []
        if activated:
            lines.append("Activated — callable from your next turn:")
            lines.extend(f"- {s.name}: {s.description}" for s in activated)
        else:
            lines.append("Matched, but none could be activated.")
        if skipped:
            lines.append(
                f"Not activated (would exceed the {registry.MAX_ACTIVE}-tool budget): "
                f"{', '.join(skipped)}. Deactivating isn't supported yet — narrow your "
                f"query or finish with what's already active."
            )
        return "\n".join(lines)

    return [
        ToolSpec(
            name="search_tools",
            description=(
                "Search for tools that aren't active yet — typically ones a connector "
                "provides — and activate matches so you can call them starting next turn. "
                "Use this when a task needs a capability you don't see in your current "
                "tool list, e.g. 'sqlite' or 'fetch a url'."
            ),
            schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords describing the capability you need."}
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            run=search_tools,
            risk=Risk.READ,
        )
    ]
