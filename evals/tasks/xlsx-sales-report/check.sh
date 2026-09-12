#!/bin/bash
# Mechanical check via openpyxl itself, not "does this look right":
# report.xlsx exists, has a Summary sheet, a bold header, and rows sorted by
# revenue descending with the right values.
set -u
python3 - <<'PY'
import sys
from openpyxl import load_workbook

try:
    wb = load_workbook("report.xlsx", data_only=True)
except FileNotFoundError:
    print("report.xlsx was not created"); sys.exit(1)

if "Summary" not in wb.sheetnames:
    print(f"no Summary sheet; found {wb.sheetnames}"); sys.exit(1)
ws = wb["Summary"]

header = [c.value for c in ws[1]]
if not any(c.font and c.font.bold for c in ws[1]):
    print("header row is not bold"); sys.exit(1)

rows = list(ws.iter_rows(min_row=2, values_only=True))
rows = [r for r in rows if r and r[0]]
# widget 540 | cog 855 | sprocket 375 | flange 1056 -> expect flange first, sprocket last
expected_order = ["flange", "cog", "widget", "sprocket"]
found_order = [str(r[0]).lower() for r in rows]
if found_order != expected_order:
    print(f"expected revenue-descending order {expected_order}, got {found_order}")
    sys.exit(1)

print("ok")
PY
