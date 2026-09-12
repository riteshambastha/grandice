#!/usr/bin/env python3
"""Tier-3 helper for the docx skill: summarise a Word document without
loading it whole into the agent's context (§05.4).

Usage:
    python3 docx_inspect.py path/to/file.docx [--preview-items N]
"""

from __future__ import annotations

import argparse
import sys

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph


def inspect(path: str, preview_items: int = 8) -> str:
    doc = Document(path)
    lines = [f"{path}"]

    section = doc.sections[0]
    header = section.header.paragraphs[0].text.strip()
    footer = section.footer.paragraphs[0].text.strip()
    lines.append(
        f"{len(doc.paragraphs)} paragraph(s), {len(doc.tables)} table(s), "
        f"{len(doc.sections)} section(s)"
    )
    if header:
        lines.append(f"header: {header!r}")
    if footer:
        lines.append(f"footer: {footer!r}")
    lines.append("")

    # True reading order — doc.paragraphs and doc.tables are separate lists
    # and lose it (see SKILL.md). Walking the body directly is the fix.
    lines.append(f"Preview, in actual document order (first {preview_items} blocks):")
    shown = 0
    for child in doc.element.body:
        if shown >= preview_items:
            lines.append(f"  ... [{sum(1 for _ in doc.element.body) - shown} more blocks not shown]")
            break
        tag = child.tag.split("}")[-1]
        if tag == "p":
            text = Paragraph(child, doc).text.strip()
            if text:
                lines.append(f"  ¶ {text[:100]}")
                shown += 1
        elif tag == "tbl":
            table = Table(child, doc)
            first_row = [c.text.strip() for c in table.rows[0].cells] if table.rows else []
            lines.append(f"  table ({len(table.rows)}x{len(table.columns)}): {first_row}")
            shown += 1

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--preview-items", type=int, default=8, help="Blocks to preview (default 8).")
    args = parser.parse_args()

    try:
        print(inspect(args.path, preview_items=args.preview_items))
    except FileNotFoundError:
        print(f"ERROR: {args.path} does not exist.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — a readable error beats a traceback here
        print(f"ERROR: could not read {args.path}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
