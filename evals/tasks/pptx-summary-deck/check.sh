#!/bin/bash
# Mechanical check via python-pptx: brief.pptx exists, has a title slide plus
# one slide per numbered point (4 total), and each point's keyword appears.
set -u
python3 - <<'PY'
import sys
from pptx import Presentation

try:
    prs = Presentation("brief.pptx")
except Exception as e:
    print(f"brief.pptx could not be opened: {e}"); sys.exit(1)

if len(prs.slides) < 4:
    print(f"expected a title slide + 3 content slides (4+), got {len(prs.slides)}")
    sys.exit(1)

all_text = []
for slide in prs.slides:
    for shape in slide.shapes:
        if shape.has_text_frame:
            all_text.append(shape.text_frame.text)
joined = " ".join(all_text).lower()

if "q3 planning brief" not in joined:
    print("title slide text not found"); sys.exit(1)
for keyword in ["headcount", "timeline", "budget"]:
    if keyword not in joined:
        print(f"missing expected point: {keyword}"); sys.exit(1)

print("ok")
PY
