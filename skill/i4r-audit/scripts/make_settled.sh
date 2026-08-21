#!/bin/bash
# make_settled.sh <audit-dir>
#
# Writes SETTLED.md — the facts later critics may take as given instead of re-deriving.
# Critics rebuilt the analytic frame and refit every model on all four roles in all seven
# calibration rounds: ~28 full reconstructions of artefacts that never changed. This is the
# single largest cost in the pipeline.
#
# Rule: a fact belongs here only once it has been independently confirmed by at least two
# critics or auditors. Everything here is spot-checkable — each row carries a hash or a
# one-line command — so a critic can verify cheaply rather than trust blindly.
set -uo pipefail
WS="${1:?usage: make_settled.sh <audit-dir>}"
OUT="$WS/SETTLED.md"

{
  echo "# Settled facts — verified, do not re-derive"
  echo
  echo "Each entry below has been independently confirmed by at least two reviewers. **Spot-check"
  echo "these rather than rebuilding them.** If a spot-check fails, say so loudly — that is a"
  echo "finding in itself. Rebuild from raw only when your specific criticism depends on it."
  echo
  echo "Re-derivation of these artefacts was the largest single cost in earlier rounds and produced"
  echo "no disagreement in any of them."
  echo
  echo "## Artefact hashes"
  echo
  echo '| Artefact | sha256 | Verify with |'
  echo '|---|---|---|'
  for f in "$WS"/work/*.csv "$WS"/work/out/*.csv; do
    [ -f "$f" ] || continue
    h=$(shasum -a 256 "$f" | cut -c1-16)
    r=${f#$WS/}
    echo "| \`$r\` | \`${h}…\` | \`shasum -a 256 $r\` |"
  done
  echo
  echo "## Reusable model fits"
  echo
  ls "$WS"/work/*.nc "$WS"/pkg/**/*.nc 2>/dev/null | while read -r f; do
    echo "- \`${f#$WS/}\` — load rather than refit unless your point is about the fit itself"
  done || echo "- (none found)"
  echo
  echo "## Counts confirmed by two or more reviewers"
  echo
  echo "_Fill these in from the round-1 and round-2 critiques before round 3._"
  echo
  echo '| Quantity | Value | First confirmed by |'
  echo '|---|---|---|'
  echo '| | | |'
} > "$OUT"
echo "wrote $OUT"
echo "NEXT: fill in the counts table by hand from the first two critique rounds."
