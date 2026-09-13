"""Context management (§05.3).

Compaction happens at 70% of the window and produces structure, not prose.
Freeform summaries lose exactly the thing the model needed, so the summary is
forced into fixed fields and the last few turns are kept verbatim.
"""

from __future__ import annotations

import json
from typing import Any

from . import prompts
from . import skills as skills_mod
from .session import Session
from .tools.base import CHARS_PER_TOKEN

KEEP_VERBATIM = 6


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(len(json.dumps(m, default=str)) for m in messages) // CHARS_PER_TOKEN


def assemble(session: Session) -> list[dict[str, Any]]:
    """System prompt, transcript, and a constraint reminder near the end."""
    connector_names = [c.spec.name for c in session.connectors]
    system = prompts.system_prompt(connector_names)
    catalog = skills_mod.catalog(session.skills)  # Tier 1 (§07) — name + description only
    if catalog:
        system = f"{system}\n\n{catalog}"

    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    out.extend(session.messages)

    if session.turns and session.turns % session.config.reinject_every == 0:
        out.append(
            {
                "role": "user",
                "content": prompts.reminder(session.todos.render(), connector_names),
            }
        )
    return out


def needs_compaction(session: Session) -> bool:
    budget = session.config.context_window * session.config.compact_at
    return estimate_tokens(session.messages) > budget


async def compact(session: Session) -> None:
    """Fold everything but the last few turns into one structured summary.

    Uses the bulk tier — this is extraction, not reasoning, and it happens often
    enough that the rate matters.
    """
    if len(session.messages) <= KEEP_VERBATIM:
        return

    head, tail = session.messages[:-KEEP_VERBATIM], session.messages[-KEEP_VERBATIM:]
    transcript = "\n".join(
        f"{m.get('role')}: {json.dumps(m.get('content') or m.get('tool_calls'), default=str)[:1500]}"
        for m in head
    )

    summary = ""
    async for item in session.router.complete(
        tier="bulk",
        messages=[
            {"role": "system", "content": prompts.COMPACT},
            {"role": "user", "content": transcript},
        ],
        temperature=0.0,
    ):
        if not isinstance(item, str):
            summary = item.text

    if not summary.strip():
        # Never let a failed summary drop the transcript on the floor.
        summary = f"(compaction produced nothing; {len(head)} earlier messages dropped)"

    session.compactions += 1
    session.messages = [
        {"role": "user", "content": f"<summary_of_earlier_work>\n{summary}\n</summary_of_earlier_work>"},
        *tail,
    ]
    session.log("compaction", kept=len(tail), summarised=len(head))


# Tools stay fixed in P0. The search_tools indirection lands with MCP in P3,
# when connector tool counts make it necessary (§05.2).
def active_tools(session: Session) -> list[dict[str, Any]]:
    return session.registry.as_openai()
