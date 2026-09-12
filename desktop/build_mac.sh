#!/bin/bash
# Build the macOS desktop app. Verified — this is the exact sequence used
# to build and smoke-test grandice.app during development (see
# DESKTOP_APP.md for what "verified" checked).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "No .venv found — run this from a checkout with the venv already set up:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -e '.[dev,web,desktop]'"
  exit 1
fi

.venv/bin/pip install -q pyinstaller
.venv/bin/pyinstaller desktop/grandice.spec --noconfirm --clean

echo
echo "Built: dist/grandice.app"
echo "Run it:   open dist/grandice.app"
echo "Or directly, to see any startup errors: ./dist/grandice.app/Contents/MacOS/grandice"
