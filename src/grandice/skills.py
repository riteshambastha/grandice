"""Skills: three-tier progressive disclosure (§07).

Tier 1 — always resident. Only `name` and `description` from a skill's
frontmatter, maybe 30 tokens apiece — enough for the model to know a
capability exists without paying for its body every turn (see `catalog`).

Tier 2 — loaded on demand. The full SKILL.md body enters context only when
the model calls `load_skill(name)` for that skill.

Tier 3 — never loaded. Scripts and reference files next to SKILL.md are
executed or read selectively with the agent's ordinary tools; nothing there
is ever pulled into the prompt.

Canonical skills live in the repo's top-level skills/ directory, version
controlled like the eval tasks. Each session mirrors them into
workspace/.skills/ so every sandbox backend sees the identical,
workspace-relative path — the Docker backend only bind-mounts the workspace,
so a skill living anywhere else would be invisible inside the container.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


def _default_skills_dir() -> Path:
    """Where canonical skills live, for however this process was started.

    In a normal source checkout or editable install, that's the repo's
    top-level skills/ directory — two levels up from this file. Under
    PyInstaller, `__file__`-based climbing breaks: files are extracted to a
    temp directory (`sys._MEIPASS`) with a different layout, and there is no
    "repo root" at all. The desktop build's spec bundles skills/ at the
    bundle root to match, so frozen apps look there instead.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "skills"
    return Path(__file__).resolve().parents[2] / "skills"


REPO_SKILLS_DIR = _default_skills_dir()
WORKSPACE_SUBDIR = ".skills"


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    dir: Path  # the workspace-mirrored directory, e.g. workspace/.skills/xlsx

    @property
    def body(self) -> str:
        _, body = _parse_frontmatter((self.dir / "SKILL.md").read_text())
        return body


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """A minimal parser for the flat `key: value` frontmatter skills use.
    No nesting, no lists — not enough here to justify a YAML dependency."""
    if not text.startswith("---"):
        raise ValueError("SKILL.md must open with a --- frontmatter block.")
    _, _, rest = text.partition("---")
    fm_block, sep, body = rest.partition("---")
    if not sep:
        raise ValueError("SKILL.md frontmatter block is not closed with a second ---.")

    meta: dict[str, str] = {}
    for line in fm_block.strip().splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, body.strip()


def sync_into_workspace(workspace: Path, source: Path | None = None) -> Path:
    """Mirror the canonical skills directory into the workspace.

    `source` defaults to REPO_SKILLS_DIR, resolved at call time rather than
    bound into the signature — a module-level default captured at def time
    would silently ignore a test (or caller) that reassigns
    `skills.REPO_SKILLS_DIR` afterward, since the function would keep
    whatever value existed when it was first defined.

    Re-synced on every session build — cheap at this size, and it means an
    edit to a skill's markdown is picked up on the next run rather than
    silently served from a stale copy. The mirror is plain files, not
    read-only: the agent's write/edit tools could in principle touch it, but
    the next session build discards and re-copies, so any such edit is only
    ever session-scoped.
    """
    source = REPO_SKILLS_DIR if source is None else source
    dest = workspace / WORKSPACE_SUBDIR
    if dest.exists():
        shutil.rmtree(dest)
    if source.exists():
        shutil.copytree(source, dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)
    return dest


def discover(skills_dir: Path) -> list[SkillMeta]:
    """Parse every skills_dir/<name>/SKILL.md into its Tier-1 metadata."""
    found: list[SkillMeta] = []
    if not skills_dir.exists():
        return found

    for entry in sorted(skills_dir.iterdir()):
        skill_file = entry / "SKILL.md"
        if not entry.is_dir() or not skill_file.exists():
            continue
        meta, _ = _parse_frontmatter(skill_file.read_text())
        name = meta.get("name") or entry.name
        description = meta.get("description", "")
        if not description:
            raise ValueError(f"{skill_file} has no `description` in its frontmatter.")
        found.append(SkillMeta(name=name, description=description, dir=entry))
    return found


def catalog(skills: list[SkillMeta]) -> str:
    """Tier 1, rendered for the system prompt. Name + description only — the
    whole point is that this stays cheap regardless of how many skills exist."""
    if not skills:
        return ""
    lines = "\n".join(f"- {s.name}: {s.description}" for s in skills)
    return (
        "SKILLS AVAILABLE — call load_skill(name) for full instructions "
        f"before attempting a task one of these covers:\n{lines}"
    )
