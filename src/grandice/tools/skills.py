"""The load_skill tool: Tier 2 of progressive disclosure (§07)."""

from __future__ import annotations

from ..skills import SkillMeta
from .base import Risk, ToolSpec


def build(skills: list[SkillMeta]) -> list[ToolSpec]:
    by_name = {s.name: s for s in skills}

    async def load_skill(name: str) -> str:
        skill = by_name.get(name)
        if skill is None:
            available = ", ".join(sorted(by_name)) or "none installed"
            raise ValueError(f"No skill named {name!r}. Available: {available}.")
        return (
            f"{skill.body}\n\n"
            f"---\n"
            f"Any scripts or reference files for this skill live in "
            f".skills/{skill.name}/ — read or run them with your usual tools; "
            f"they were not preloaded into context."
        )

    return [
        ToolSpec(
            name="load_skill",
            description=(
                "Load full instructions for one skill by name. Call this before "
                "attempting a task a skill covers — see SKILLS AVAILABLE above "
                "for the current list and what triggers each one."
            ),
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact skill name from the catalog."}
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            run=load_skill,
            risk=Risk.READ,
        )
    ]
