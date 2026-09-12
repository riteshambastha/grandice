#!/bin/bash
# Checkable output: the file exists, names the right product, and has the right total.
# widget 540.00 | cog 855.00 | sprocket 375.00 | flange 1056.00 => total 2826.00
set -u
[ -f summary.md ] || { echo "summary.md was not created"; exit 1; }
grep -qi 'flange' summary.md || { echo "did not identify flange as best seller"; exit 1; }
grep -qE '2,?826(\.00)?' summary.md || { echo "total revenue 2826.00 not found"; exit 1; }
echo "ok"
