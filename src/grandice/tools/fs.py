"""Filesystem tools: read, write, edit, glob.

The edit design is the load-bearing one. Exact-string-replace is right for a
frontier model and a trap for everything else — weaker models reproduce
whitespace inexactly and then thrash on the same failure. So edits address a
line range and carry a hash of what the model believes is there (§05.7).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..sandbox import Sandbox
from .base import Risk, ToolSpec

MAX_WRITE_BYTES = 2_000_000


def _range_hash(lines: list[str], start: int, end: int) -> str:
    """Short hash of a 1-indexed inclusive line range."""
    return hashlib.sha256("".join(lines[start - 1 : end]).encode()).hexdigest()[:8]


def _numbered(lines: list[str], start: int = 1) -> str:
    width = len(str(start + len(lines) - 1))
    return "".join(f"{start + i:>{width}}\t{line}" for i, line in enumerate(lines))


def build(sandbox: Sandbox) -> list[ToolSpec]:
    async def read(path: str, offset: int = 1, limit: int = 400) -> str:
        target = sandbox.resolve(path)
        if not target.exists():
            siblings = sorted(p.name for p in target.parent.glob("*"))[:20] if target.parent.exists() else []
            hint = f" Nearby: {', '.join(siblings)}" if siblings else ""
            raise FileNotFoundError(f"{path} does not exist.{hint}")
        if target.is_dir():
            raise IsADirectoryError(f"{path} is a directory. Use glob to list it.")

        lines = target.read_text(errors="replace").splitlines(keepends=True)
        offset = max(1, offset)
        window = lines[offset - 1 : offset - 1 + limit]
        if not window:
            return f"{path} has {len(lines)} lines; offset {offset} is past the end."

        body = _numbered(window, offset)
        shown_to = offset + len(window) - 1
        footer = ""
        if shown_to < len(lines):
            footer = f"\n[showing {offset}-{shown_to} of {len(lines)} lines; read again with offset={shown_to + 1}]"
        return body + footer

    async def write(path: str, content: str) -> str:
        if len(content.encode()) > MAX_WRITE_BYTES:
            raise ValueError(f"Content exceeds {MAX_WRITE_BYTES:,} bytes. Write it in pieces.")
        target = sandbox.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content)
        n = len(content.splitlines())
        return f"{'Overwrote' if existed else 'Wrote'} {path} ({n} lines, {len(content):,} chars)."

    async def edit(
        path: str,
        start_line: int,
        end_line: int,
        replacement: str,
        expected_hash: str = "",
    ) -> str:
        target = sandbox.resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"{path} does not exist. Use write to create it.")

        lines = target.read_text(errors="replace").splitlines(keepends=True)
        total = len(lines)
        if not 1 <= start_line <= total:
            raise ValueError(f"start_line {start_line} out of range; {path} has {total} lines.")
        if not start_line <= end_line <= total:
            raise ValueError(f"end_line {end_line} out of range; {path} has {total} lines.")

        actual = _range_hash(lines, start_line, end_line)
        if expected_hash and expected_hash != actual:
            # Don't just say no — show what is actually there, with context, so
            # the retry is informed rather than another guess.
            lo, hi = max(1, start_line - 5), min(total, end_line + 5)
            return (
                f"STALE: lines {start_line}-{end_line} of {path} hash {actual}, "
                f"you expected {expected_hash}. The file changed under you. "
                f"Here is what is actually there:\n\n{_numbered(lines[lo - 1 : hi], lo)}\n"
                f"Re-issue the edit against these line numbers."
            )

        body = replacement if replacement.endswith("\n") or not replacement else replacement + "\n"
        lines[start_line - 1 : end_line] = body.splitlines(keepends=True) if replacement else []
        target.write_text("".join(lines))

        new_total = len(lines)
        lo, hi = max(1, start_line - 3), min(new_total, start_line + len(body.splitlines()) + 2)
        return (
            f"Edited {path}: replaced lines {start_line}-{end_line} "
            f"({total} → {new_total} lines). Now reads:\n\n{_numbered(lines[lo - 1 : hi], lo)}"
        )

    async def glob(pattern: str = "**/*", limit: int = 200) -> str:
        root = sandbox.workspace
        hits = []
        for p in sorted(root.glob(pattern)):
            try:
                sandbox.resolve(str(p))
            except PermissionError:
                continue  # a symlink pointing out of the workspace
            rel = p.relative_to(root)
            hits.append(f"{rel}/" if p.is_dir() else f"{rel}\t{p.stat().st_size:,}b")

        if not hits:
            return f"No matches for {pattern!r} under the workspace."
        shown = hits[:limit]
        more = f"\n[{len(hits) - limit} more matches; narrow the pattern]" if len(hits) > limit else ""
        return "\n".join(shown) + more

    return [
        ToolSpec(
            name="read",
            description=(
                "Read a file from the workspace with line numbers. Returns a window of "
                "lines; use offset to page through a long file rather than reading it whole."
            ),
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace."},
                    "offset": {"type": "integer", "minimum": 1, "description": "First line to show (1-indexed)."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "How many lines."},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            run=read,
            risk=Risk.READ,
        ),
        ToolSpec(
            name="write",
            description="Create a file, or replace one entirely. To change part of an existing file use edit instead.",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace."},
                    "content": {"type": "string", "description": "Full contents of the file."},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            run=write,
            risk=Risk.WRITE,
        ),
        ToolSpec(
            name="edit",
            description=(
                "Replace an inclusive range of lines in a file. Read the file first to get "
                "line numbers. Pass expected_hash from a previous edit's output when you have "
                "it, so a stale edit is rejected with the current contents instead of "
                "corrupting the file. An empty replacement deletes the range."
            ),
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1, "description": "First line to replace (1-indexed, inclusive)."},
                    "end_line": {"type": "integer", "minimum": 1, "description": "Last line to replace (inclusive)."},
                    "replacement": {"type": "string", "description": "New text for that range. Empty string deletes it."},
                    "expected_hash": {"type": "string", "description": "Optional 8-char hash of the range you expect to be replacing."},
                },
                "required": ["path", "start_line", "end_line", "replacement"],
                "additionalProperties": False,
            },
            run=edit,
            risk=Risk.WRITE,
        ),
        ToolSpec(
            name="glob",
            description="List workspace files matching a glob pattern, e.g. '**/*.csv' or 'reports/*'.",
            schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern relative to the workspace."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            run=glob,
            risk=Risk.READ,
        ),
    ]
