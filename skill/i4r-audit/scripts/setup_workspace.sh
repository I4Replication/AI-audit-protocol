#!/bin/bash
# setup_workspace.sh <audit-dir> <paper.pdf> <package.zip|dir>
set -euo pipefail
DIR="${1:?audit dir}"; PAPER="${2:?paper pdf}"; PKG="${3:?package zip or dir}"
mkdir -p "$DIR"/{pkg,paper,work,report,critique}
cp "$PAPER" "$DIR/paper/paper.pdf"
if [ -d "$PKG" ]; then cp -R "$PKG"/. "$DIR/pkg/"; else unzip -q -o "$PKG" -d "$DIR/pkg"; fi
# flatten a single wrapper directory
inner=$(find "$DIR/pkg" -mindepth 1 -maxdepth 1 -type d | head -1)
if [ "$(find "$DIR/pkg" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" = "1" ] && [ -n "$inner" ]; then
  mv "$inner"/* "$DIR/pkg/" 2>/dev/null || true; rmdir "$inner" 2>/dev/null || true
fi
command -v pdftotext >/dev/null && pdftotext -layout "$DIR/paper/paper.pdf" "$DIR/paper/paper.txt" 2>/dev/null || true
echo "=== workspace: $DIR ==="
echo "package entries: $(find "$DIR/pkg" -type f | wc -l | tr -d ' ')"
echo "paper text:      $( [ -f "$DIR/paper/paper.txt" ] && wc -l < "$DIR/paper/paper.txt" || echo 'EXTRACTION FAILED — read the PDF directly')"
echo ""
echo "--- file types ---"
find "$DIR/pkg" -type f | sed 's/.*\.//' | tr 'A-Z' 'a-z' | sort | uniq -c | sort -rn | head -12
echo ""
echo "--- candidate analysis code ---"
find "$DIR/pkg" -type f \( -name '*.R' -o -name '*.r' -o -name '*.qmd' -o -name '*.Rmd' -o -name '*.py' -o -name '*.do' -o -name '*.sps' -o -name '*.ipynb' \) | head -20
echo ""
echo "NOTE: verify these counts yourself before briefing auditors. A wrong brief propagates."
