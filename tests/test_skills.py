"""Tests for the skills loader (§07): frontmatter parsing, workspace sync,
the catalog (Tier 1), and the load_skill tool (Tier 2)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from grandice import loop as agent_loop
from grandice import skills as skills_mod
from grandice.config import Config
from grandice.permissions import Gate, always_deny
from grandice.router import ToolCall
from grandice.session import build as build_session


def _write_skill(root: Path, name: str, description: str, body: str = "Do the thing.") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
    )
    return skill_dir


# --- frontmatter + discovery -------------------------------------------

def test_discover_parses_name_and_description(tmp_path: Path):
    _write_skill(tmp_path, "widgets", "Trigger on .widget files.")
    found = skills_mod.discover(tmp_path)
    assert len(found) == 1
    assert found[0].name == "widgets"
    assert found[0].description == "Trigger on .widget files."


def test_discover_reads_body_lazily_and_strips_frontmatter(tmp_path: Path):
    _write_skill(tmp_path, "widgets", "desc", body="# Heading\n\nReal instructions here.")
    found = skills_mod.discover(tmp_path)
    assert "Real instructions here." in found[0].body
    assert "description:" not in found[0].body  # frontmatter stripped


def test_discover_skips_non_skill_directories(tmp_path: Path):
    (tmp_path / "not_a_skill").mkdir()
    (tmp_path / "not_a_skill" / "readme.txt").write_text("hi")
    assert skills_mod.discover(tmp_path) == []


def test_discover_requires_a_description(tmp_path: Path):
    skill_dir = tmp_path / "broken"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: broken\n---\nbody\n")
    with pytest.raises(ValueError, match="description"):
        skills_mod.discover(tmp_path)


def test_discover_on_a_missing_directory_returns_empty(tmp_path: Path):
    assert skills_mod.discover(tmp_path / "does-not-exist") == []


# --- Tier 1: the catalog -------------------------------------------------

def test_catalog_is_empty_string_with_no_skills():
    assert skills_mod.catalog([]) == ""


def test_catalog_lists_name_and_description_only(tmp_path: Path):
    _write_skill(tmp_path, "xlsx", "Trigger on .xlsx files.", body="A" * 5000)
    found = skills_mod.discover(tmp_path)
    text = skills_mod.catalog(found)
    assert "xlsx" in text and "Trigger on .xlsx files." in text
    assert "A" * 100 not in text  # the 5000-char body must NOT leak into Tier 1


# --- sync_into_workspace --------------------------------------------------

def test_sync_mirrors_the_source_tree(tmp_path: Path):
    source = tmp_path / "source-skills"
    _write_skill(source, "xlsx", "desc")
    (source / "xlsx" / "scripts").mkdir()
    (source / "xlsx" / "scripts" / "helper.py").write_text("print('hi')")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    dest = skills_mod.sync_into_workspace(workspace, source=source)

    assert dest == workspace / ".skills"
    assert (dest / "xlsx" / "SKILL.md").exists()
    assert (dest / "xlsx" / "scripts" / "helper.py").read_text() == "print('hi')"


def test_sync_replaces_a_stale_copy_rather_than_merging(tmp_path: Path):
    source = tmp_path / "source-skills"
    _write_skill(source, "xlsx", "desc")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    stale = workspace / ".skills" / "deleted-skill"
    stale.mkdir(parents=True)

    skills_mod.sync_into_workspace(workspace, source=source)
    assert not stale.exists()  # last session's skill is gone, not left behind
    assert (workspace / ".skills" / "xlsx").exists()


def test_sync_with_no_source_leaves_an_empty_skills_dir(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dest = skills_mod.sync_into_workspace(workspace, source=tmp_path / "nonexistent")
    assert dest.exists() and skills_mod.discover(dest) == []


# --- the load_skill tool, through a real session -------------------------

@pytest.fixture
def session(tmp_path: Path, monkeypatch):
    source = tmp_path / "canonical-skills"
    _write_skill(source, "xlsx", "Trigger on .xlsx files.", body="Use openpyxl.")
    monkeypatch.setattr(skills_mod, "REPO_SKILLS_DIR", source)

    config = replace(
        Config.from_env(),
        workspace=tmp_path / "ws",
        sandbox="sandbox-exec",
        api_key=None,
        base_url=None,
    )
    return build_session(config, Gate(always_deny))


async def test_session_discovers_and_exposes_skills(session):
    assert [s.name for s in session.skills] == ["xlsx"]
    assert "load_skill" in session.registry.names()


async def test_load_skill_returns_the_body(session):
    result = await agent_loop.execute(session, ToolCall("1", "load_skill", {"name": "xlsx"}))
    assert "Use openpyxl." in result["content"]
    assert ".skills/xlsx/" in result["content"]  # tells the model where Tier 3 lives


async def test_load_skill_on_an_unknown_name_lists_the_real_ones(session):
    result = await agent_loop.execute(session, ToolCall("1", "load_skill", {"name": "docx"}))
    assert result["content"].startswith("ERROR:")
    assert "xlsx" in result["content"]


def test_tool_count_stays_well_under_the_active_cap(session):
    assert len(session.registry.active()) < session.registry.MAX_ACTIVE


# --- the real skills shipped in the repo ---------------------------------

@pytest.mark.parametrize("name", ["xlsx", "pptx", "docx", "pdf"])
def test_shipped_skill_frontmatter_is_well_formed(name):
    found = [s for s in skills_mod.discover(skills_mod.REPO_SKILLS_DIR) if s.name == name]
    assert len(found) == 1, f"{name} skill not discovered from {skills_mod.REPO_SKILLS_DIR}"
    skill = found[0]
    assert len(skill.description) > 20
    assert len(skill.body) > 200


def test_shipped_skills_catalog_names_the_real_trigger_conditions():
    found = skills_mod.discover(skills_mod.REPO_SKILLS_DIR)
    text = skills_mod.catalog(found)
    assert ".xlsx" in text
    assert ".pptx" in text
    assert ".docx" in text
    assert ".pdf" in text
