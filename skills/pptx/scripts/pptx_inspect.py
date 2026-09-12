#!/usr/bin/env python3
"""Tier-3 helper for the pptx skill: summarise a deck without dumping the
whole thing into the agent's context (§05.4).

Usage:
    python3 inspect.py path/to/file.pptx
"""

from __future__ import annotations

import argparse
import sys

from pptx import Presentation


def _text_preview(slide, limit: int = 200) -> str:
    chunks = []
    for shape in slide.shapes:
        if shape.has_text_frame and shape.text_frame.text.strip():
            chunks.append(shape.text_frame.text.strip())
    joined = " / ".join(chunks)
    return joined[:limit] + ("…" if len(joined) > limit else "")


def inspect(path: str) -> str:
    prs = Presentation(path)
    lines = [f"{path}", f"{len(prs.slides)} slide(s)", ""]
    lines.append("Layouts in this file's master:")
    for i, layout in enumerate(prs.slide_layouts):
        lines.append(f"  {i}: {layout.name}")
    lines.append("")

    for i, slide in enumerate(prs.slides, 1):
        layout_name = slide.slide_layout.name
        placeholders = [
            f"{ph.placeholder_format.idx}:{ph.placeholder_format.type}"
            for ph in slide.placeholders
        ]
        lines.append(f"Slide {i} — layout {layout_name!r}, placeholders: {placeholders or 'none'}")
        preview = _text_preview(slide)
        if preview:
            lines.append(f"  text: {preview}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    args = parser.parse_args()

    try:
        print(inspect(args.path))
    except FileNotFoundError:
        print(f"ERROR: {args.path} does not exist.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — a readable error beats a traceback here
        print(f"ERROR: could not read {args.path}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
