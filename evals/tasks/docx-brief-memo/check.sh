#!/bin/bash
# Mechanical check via python-docx: memo.docx exists, has the heading, and
# a bullet-styled paragraph for each of the three notes.txt items.
set -u
python3 - <<'PY'
import sys
from docx import Document

try:
    doc = Document("memo.docx")
except Exception as e:
    print(f"memo.docx could not be opened: {e}"); sys.exit(1)

texts = [p.text.lower() for p in doc.paragraphs]
bullets = [p for p in doc.paragraphs if p.style.name.lower().startswith("list")]

if not any("q3 planning memo" in t for t in texts):
    print("heading text not found"); sys.exit(1)
if len(bullets) < 3:
    print(f"expected at least 3 bullet-styled paragraphs, found {len(bullets)}"); sys.exit(1)
for keyword in ["headcount", "timeline", "budget"]:
    if not any(keyword in t for t in texts):
        print(f"missing expected point: {keyword}"); sys.exit(1)

print("ok")
PY
