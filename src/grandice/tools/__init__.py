"""Tool registry assembly."""

from __future__ import annotations

from ..sandbox import Sandbox
from ..skills import SkillMeta
from . import fs, shell, skills as skills_tool, todo
from .base import Registry, Risk, ToolSpec, tool_error, tool_result, truncate, validate
from .todo import TodoList

__all__ = [
    "Registry",
    "Risk",
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
    """P0's five tools, the plan tracker, and load_skill (§07). Seven of a
    budget of fifteen — see Registry.MAX_ACTIVE."""
    registry = Registry()
    tools = [
        *fs.build(sandbox),
        *shell.build(sandbox),
        *todo.build(todos),
        *skills_tool.build(skills or []),
    ]
    for spec in tools:
        registry.add(spec)
    return registry
