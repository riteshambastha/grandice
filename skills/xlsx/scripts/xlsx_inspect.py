#!/usr/bin/env python3
"""Tier-3 helper for the xlsx skill: summarise a workbook without loading it
whole into the agent's context (§05.4 — truncate on the way in, not just out).

Usage:
    python3 xlsx_inspect.py path/to/file.xlsx [--rows N]
"""

from __future__ import annotations

import argparse
import sys

from openpyxl import load_workbook


def inspect(path: str, preview_rows: int = 3) -> str:
    wb = load_workbook(path, read_only=True, data_only=False)
    lines = [f"{path}", f"{len(wb.sheetnames)} sheet(s): {', '.join(wb.sheetnames)}", ""]

    for name in wb.sheetnames:
        ws = wb[name]
        # .dimensions isn't available on a read_only worksheet; max_row/
        # max_column are, and work the same in both modes.
        lines.append(f"[{name}] {ws.max_row} rows x {ws.max_column} cols")

        for i, row in enumerate(ws.iter_rows(max_row=preview_rows, values_only=True)):
            tag = "header" if i == 0 else f"row {i + 1}"
            preview = ", ".join(repr(v) for v in row[:10])
            more = " ..." if len(row) > 10 else ""
            lines.append(f"  {tag}: {preview}{more}")
        lines.append("")

    wb.close()
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--rows", type=int, default=3, help="Preview rows per sheet (default 3).")
    args = parser.parse_args()

    try:
        print(inspect(args.path, preview_rows=args.rows))
    except FileNotFoundError:
        print(f"ERROR: {args.path} does not exist.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — a readable error beats a traceback here
        print(f"ERROR: could not read {args.path}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
