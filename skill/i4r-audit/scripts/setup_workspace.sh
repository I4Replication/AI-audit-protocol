#!/bin/bash
# setup_workspace.sh <audit-dir> <paper.pdf> <package.zip|dir>
set -euo pipefail
DIR="${1:?audit dir}"; PAPER="${2:?paper pdf}"; PKG="${3:?package zip or dir}"

# ---- automatic reservation (2026-08-24): the audit claims its own paper before touching it ----
# The paper dir is the audit dir's parent; the discipline tree is two levels up. If the audit is
# running OUTSIDE the shared tree (fixtures, tmp dirs, one-off jobs) there is no reservations
# root to write into, so the claim degrades to a warning. Inside the tree, a refusal ABORTS:
# someone else is already on this paper. I4R_SKIP_RESERVE=1 bypasses (say why in run notes).
if [ "${I4R_SKIP_RESERVE:-0}" != "1" ]; then
  # the paper dir may not exist yet on a fresh workspace; a failed cd must not kill the run
  PDIR="$(cd "$(dirname "$DIR")" 2>/dev/null && pwd || true)"
  RROOT="${PDIR:+$(dirname "$(dirname "$PDIR")")}"
  RESV="$(cd "$(dirname "$0")" && pwd)/reserve_paper.sh"
  # the discipline root is recognised by its markers, not by merely having subdirectories —
  # otherwise a tmp-dir fixture two levels deep would scatter reservations into random parents
  if [ -x "$RESV" ] && { [ -d "$RROOT/reservations" ] || [ -d "$RROOT/Completed reports" ]; }; then
    set +e; OUT="$("$RESV" claim "$PDIR" 2>&1)"; RC=$?; set -e
    echo "$OUT" | sed 's/^/  [reserve] /'
    if [ $RC -ne 0 ]; then
      echo "FATAL: could not reserve this paper — someone else holds it or a conflict is unresolved."
      echo "       Pick another paper, or resolve per the message above. (I4R_SKIP_RESERVE=1 overrides.)"
      exit 1
    fi
  else
    echo "  [reserve] outside the shared tree — no reservation written (fixtures/tmp are exempt)"
  fi
fi

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
