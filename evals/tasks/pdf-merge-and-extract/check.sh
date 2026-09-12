#!/bin/bash
# Mechanical check via pypdf: combined.pdf has both source pages in the
# right order, and first_page.txt matches the real first page's text.
set -u
python3 - <<'PY'
import sys
from pathlib import Path
from pypdf import PdfReader

try:
    combined = PdfReader("combined.pdf")
except Exception as e:
    print(f"combined.pdf could not be opened: {e}"); sys.exit(1)

if len(combined.pages) != 2:
    print(f"expected 2 pages, got {len(combined.pages)}"); sys.exit(1)

page0_text = combined.pages[0].extract_text()
page1_text = combined.pages[1].extract_text()
if "cover page" not in page0_text.lower():
    print(f"page 0 is not the cover page: {page0_text!r}"); sys.exit(1)
if "body content" not in page1_text.lower():
    print(f"page 1 is not the body page: {page1_text!r}"); sys.exit(1)

first_page_file = Path("first_page.txt")
if not first_page_file.exists():
    print("first_page.txt was not created"); sys.exit(1)
if "cover page" not in first_page_file.read_text().lower():
    print("first_page.txt does not contain the actual first page's text"); sys.exit(1)

print("ok")
PY
