"""Tool registry assembly."""

from __future__ import annotations

from ..sandbox import Sandbox
from . import fs, shell, todo
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


def build_registry(sandbox: Sandbox, todos: TodoList) -> Registry:
    """P0's five tools plus the plan tracker. Six of a budget of fifteen."""
    registry = Registry()
    for spec in [*fs.build(sandbox), *shell.build(sandbox), *todo.build(todos)]:
        registry.add(spec)
    return registry
