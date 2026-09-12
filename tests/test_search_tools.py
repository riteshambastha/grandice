"""Tests for the active/latent tool split (§05.2) and search_tools — the
same on-demand-loading trick skills.py uses, applied to tools."""

from __future__ import annotations

from dataclasses import replace

import pytest

from grandice import loop as agent_loop
from grandice.config import Config
from grandice.permissions import Gate, always_deny
from grandice.router import ToolCall
from grandice.session import build as build_session
from grandice.tools.base import Registry, Risk, ToolBudgetExceeded, ToolSpec


def _spec(name: str, description: str = "does a thing") -> ToolSpec:
    async def run(**kwargs):
        return "ok"

    return ToolSpec(name=name, description=description, schema={"type": "object"}, run=run)


# --- Registry: active/latent -------------------------------------------

def test_tools_are_active_by_default():
    reg = Registry()
    reg.add(_spec("read"))
    assert reg.names() == ["read"]
    assert reg.latent() == []


def test_latent_tools_are_hidden_from_names_and_active():
    reg = Registry()
    reg.add(_spec("read"))
    reg.add(_spec("fetch.fetch", "fetches a url"), active=False)

    assert reg.names() == ["read"]  # not offered to the model yet
    assert [s.name for s in reg.latent()] == ["fetch.fetch"]
    assert reg.get("fetch.fetch") is not None  # still dispatchable if somehow called


def test_search_matches_name_and_description_case_insensitively():
    reg = Registry()
    reg.add(_spec("sqlite.read_query", "Execute a SELECT query"), active=False)
    reg.add(_spec("fetch.fetch", "Fetches a URL from the internet"), active=False)

    assert [s.name for s in reg.search("SELECT")] == ["sqlite.read_query"]
    assert [s.name for s in reg.search("url")] == ["fetch.fetch"]
    assert reg.search("nonexistent-capability") == []
    assert reg.search("") == []  # an empty query matches nothing, not everything


def test_activate_moves_a_tool_from_latent_to_active():
    reg = Registry()
    reg.add(_spec("fetch.fetch"), active=False)
    reg.activate("fetch.fetch")
    assert "fetch.fetch" in reg.names()
    assert reg.latent() == []


def test_activate_is_idempotent():
    reg = Registry()
    reg.add(_spec("fetch.fetch"), active=False)
    reg.activate("fetch.fetch")
    reg.activate("fetch.fetch")  # must not raise or double-count
    assert reg.names().count("fetch.fetch") == 1


def test_activate_unknown_tool_raises_keyerror():
    reg = Registry()
    with pytest.raises(KeyError):
        reg.activate("nonexistent")


def test_activate_refuses_to_exceed_the_budget():
    reg = Registry()
    for i in range(Registry.MAX_ACTIVE):
        reg.add(_spec(f"core_{i}"))
    reg.add(_spec("one_too_many"), active=False)

    with pytest.raises(ToolBudgetExceeded):
        reg.activate("one_too_many")
    assert "one_too_many" not in reg.names()  # refused cleanly, not left half-activated


# --- the search_tools tool itself, through a real session ----------------

@pytest.fixture
def session(tmp_path):
    config = replace(
        Config.from_env(),
        workspace=tmp_path / "ws",
        sandbox="sandbox-exec",
        api_key=None,
        base_url=None,
    )
    return build_session(config, Gate(always_deny))


async def test_search_tools_is_active_by_default(session):
    assert "search_tools" in session.registry.names()


async def test_search_tools_activates_a_latent_match(session):
    session.registry.add(_spec("fetch.fetch", "Fetches a URL from the internet"), active=False)

    result = await agent_loop.execute(session, ToolCall("1", "search_tools", {"query": "url"}))
    assert "fetch.fetch" in result["content"]
    assert "fetch.fetch" in session.registry.names()  # now active for the next turn


async def test_search_tools_reports_no_match_and_lists_latent(session):
    session.registry.add(_spec("fetch.fetch"), active=False)
    result = await agent_loop.execute(
        session, ToolCall("1", "search_tools", {"query": "something-nobody-has"})
    )
    assert "No latent tool matches" in result["content"]
    assert "fetch.fetch" in result["content"]


async def test_search_tools_reports_budget_exceeded_without_crashing(session):
    for i in range(Registry.MAX_ACTIVE - len(session.registry.names())):
        session.registry.add(_spec(f"filler_{i}"))
    session.registry.add(_spec("overflow_capability"), active=False)

    result = await agent_loop.execute(
        session, ToolCall("1", "search_tools", {"query": "overflow"})
    )
    assert "would exceed" in result["content"]
    assert not result["content"].startswith("ERROR:")  # a clear message, not a crashed turn
