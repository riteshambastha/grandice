#!/usr/bin/env python3
"""Tier-3 helper for the pdf skill: summarise a PDF without dumping every
page's full text into the agent's context (§05.4).

Usage:
    python3 pdf_inspect.py path/to/file.pdf
"""

from __future__ import annotations

import argparse
import sys

from pypdf import PdfReader

# Below this many characters of extracted text, a page is more likely
# scanned/image-based than genuinely short — flagged, not treated as an error,
# since pypdf raises nothing in either case (§ SKILL.md's "no OCR" note).
SHORT_EXTRACTION_CHARS = 20


def inspect(path: str, preview_chars: int = 200) -> str:
    reader = PdfReader(path)
    lines = [f"{path}"]

    # is_encrypted must be checked before .pages is touched at all — accessing
    # .pages on an undecrypted reader raises, it doesn't just return 0 pages.
    if reader.is_encrypted:
        lines.append("ENCRYPTED — call reader.decrypt(password) before reading pages.")
        return "\n".join(lines)

    lines.append(f"{len(reader.pages)} page(s)")
    meta = reader.metadata
    if meta:
        title = meta.get("/Title") or "(untitled)"
        author = meta.get("/Author") or "(unknown)"
        lines.append(f"title: {title!r}  author: {author!r}")
    lines.append("")

    short_pages = []
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        preview = text[:preview_chars].replace("\n", " ")
        suffix = "…" if len(text) > preview_chars else ""
        lines.append(f"page {i}: {len(text)} chars — {preview!r}{suffix}")
        if len(text) < SHORT_EXTRACTION_CHARS:
            short_pages.append(i)

    if short_pages:
        lines.append(
            f"\nWARNING: page(s) {short_pages} extracted under {SHORT_EXTRACTION_CHARS} chars. "
            f"This usually means a scanned/image-based page, not an empty one — pypdf does no "
            f"OCR and will not error, it just returns little or no text."
        )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--preview-chars", type=int, default=200, help="Preview length per page (default 200).")
    args = parser.parse_args()

    try:
        print(inspect(args.path, preview_chars=args.preview_chars))
    except FileNotFoundError:
        print(f"ERROR: {args.path} does not exist.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — a readable error beats a traceback here
        print(f"ERROR: could not read {args.path}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
