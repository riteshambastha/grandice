"""Tool registry assembly."""

from __future__ import annotations

from ..sandbox import Sandbox
from ..skills import SkillMeta
from . import fs, search, shell, skills as skills_tool, todo
from .base import Registry, Risk, ToolBudgetExceeded, ToolSpec, tool_error, tool_result, truncate, validate
from .todo import TodoList

__all__ = [
    "Registry",
    "Risk",
    "ToolBudgetExceeded",
    "ToolSpec",
    "TodoList",
    "build_registry",
    "tool_error",
    "tool_result",
    "truncate",
    "validate",
]


def build_registry(
    sandbox: Sandbox,
    todos: TodoList,
    skills: list[SkillMeta] | None = None,
) -> Registry:
    """P0's five tools, the plan tracker, load_skill (§07), and search_tools
    (§05.2 / §P3) — eight of a budget of fifteen, all active by default.
    MCP connectors add their tools later, latent, via session.start_connectors;
    search_tools is what surfaces them."""
    registry = Registry()
    for spec in [
        *fs.build(sandbox),
        *shell.build(sandbox),
        *todo.build(todos),
        *skills_tool.build(skills or []),
    ]:
        registry.add(spec)

    # search_tools closes over the registry itself, so it's built last.
    for spec in search.build(registry):
        registry.add(spec)

    return registry
