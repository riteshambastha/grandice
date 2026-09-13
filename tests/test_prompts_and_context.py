"""Tests for prompts.py's connector-aware "no network access" rule, and
context.assemble()'s wiring of it.

Real bug this guards against, found by actually running a self-hosted model
against a configured `fetch` connector through the dashboard: HARD RULE 1
said "There is no network access" unconditionally, and the reinjected
REMINDER repeated "no network" the same way — both simply false once an
outward-facing connector is configured, and a weaker local model took the
blanket statement literally, flatly refusing to use `fetch` even when told
its exact name. Sandboxed tools genuinely have no network access; the fix
is naming the approved exception, not removing the rule.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from grandice import prompts
from grandice.config import Config
from grandice.context import assemble
from grandice.mcp_client import Connector, ConnectorSpec
from grandice.permissions import Gate, always_deny
from grandice.session import build as build_session


# --- prompts.py --------------------------------------------------------

def test_system_prompt_says_no_network_access_with_no_connectors():
    system = prompts.system_prompt([])
    assert "There is no network access." in system
    assert "connector" not in system.lower()


def test_system_prompt_names_a_single_connector_as_an_exception():
    system = prompts.system_prompt(["fetch"])
    assert "There is no network access." not in system
    assert "fetch" in system
    assert "search_tools" in system
    assert "real, approved network access" in system


def test_system_prompt_names_multiple_connectors():
    system = prompts.system_prompt(["fetch", "sqlite"])
    assert "fetch, sqlite" in system
    assert "are connector tools" in system


def test_system_prompt_still_states_sandboxed_tools_have_no_network():
    """The fix must not just delete the constraint — sandboxed tools (bash,
    edit, write, ...) genuinely have no network access; only connector
    tools are the approved exception."""
    system = prompts.system_prompt(["fetch"])
    assert "Sandboxed tools have no network access" in system


def test_reminder_mentions_no_network_with_no_connectors():
    text = prompts.reminder(plan="- [ ] step one", connector_names=[])
    assert "no network access" in text
    assert "step one" in text


def test_reminder_mentions_the_connector_exception_when_configured():
    text = prompts.reminder(plan="- [ ] step one", connector_names=["fetch"])
    assert "approved connector tools" in text


# --- context.assemble() -----------------------------------------------

@pytest.fixture
def session(tmp_path: Path):
    # mcp_connectors explicitly cleared: this repo's own .env may have
    # GRANDICE_MCP_CONNECTORS set for real local testing, which must not
    # leak into a test that specifically wants a connector-free baseline.
    config = replace(
        Config.from_env(),
        workspace=tmp_path / "ws",
        sandbox="sandbox-exec",
        api_key=None,
        base_url=None,
        mcp_connectors=(),
    )
    return build_session(config, Gate(always_deny))


def test_assemble_has_no_network_wording_with_no_connectors(session):
    messages = assemble(session)
    assert messages[0]["role"] == "system"
    assert "There is no network access." in messages[0]["content"]


def test_assemble_names_configured_connectors_in_the_system_prompt(session):
    session.connectors.append(Connector(ConnectorSpec(name="fetch", command="fake-fetch-server")))
    messages = assemble(session)
    assert "fetch" in messages[0]["content"]
    assert "There is no network access." not in messages[0]["content"]


def test_assemble_reminder_reflects_connectors_too(session):
    session.connectors.append(Connector(ConnectorSpec(name="fetch", command="fake-fetch-server")))
    session.turns = session.config.reinject_every  # trigger reinjection
    messages = assemble(session)
    reminder_messages = [m for m in messages if m["role"] == "user" and "<constraints>" in m["content"]]
    assert reminder_messages
    assert "approved connector tools" in reminder_messages[0]["content"]
