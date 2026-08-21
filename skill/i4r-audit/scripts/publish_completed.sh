#!/bin/bash
# publish_completed.sh <audit-dir> [completed-root]
#
# Files the finished audit into the team's completed-reports tree (decided 2026-08-21):
#
#     <discipline-root>/Completed reports/<Journal>/<Paper>/
#         <comment>.pdf          the final built PDF, hash-verified against report/
#         claims_script.<ext>    the numbered-claims code file, and NOTHING ELSE
#
# Journal and paper names are derived from the audit's own path, which is
# <discipline-root>/<Journal>/<Paper>/audit. The destination holds exactly two files; anything
# else already there blocks the publish rather than being silently buried.
set -uo pipefail
A="${1:?audit dir}"; A="$(cd "$A" && pwd)"
PAPER_DIR="$(dirname "$A")"; JOURNAL_DIR="$(dirname "$PAPER_DIR")"; ROOT="$(dirname "$JOURNAL_DIR")"
DEST_ROOT="${2:-$ROOT/Completed reports}"
PAPER="$(basename "$PAPER_DIR")"; JOURNAL="$(basename "$JOURNAL_DIR")"

PDF=$(ls "$A/report/"*.pdf 2>/dev/null | grep -viE "_proxy|preview" | head -1)
[ -n "$PDF" ] || { echo "FATAL: no built PDF in $A/report — run build_docx.sh first"; exit 1; }
CLAIMS=$(ls "$A"/claims_script.* 2>/dev/null | head -1)
[ -n "$CLAIMS" ] || { echo "FATAL: no claims_script.* in $A — run make_claims_script.sh first"; exit 1; }

DEST="$DEST_ROOT/$JOURNAL/$PAPER"
mkdir -p "$DEST"
extra=$(find "$DEST" -type f ! -name "$(basename "$PDF")" ! -name "$(basename "$CLAIMS")" ! -name ".DS_Store" | head -3)
[ -n "$extra" ] && { echo "FATAL: destination already holds other files — the folder carries exactly two:"; echo "$extra"; exit 1; }

cp "$PDF" "$DEST/"; cp "$CLAIMS" "$DEST/"
h1=$(shasum -a 256 "$PDF" | cut -d' ' -f1); h2=$(shasum -a 256 "$DEST/$(basename "$PDF")" | cut -d' ' -f1)
[ "$h1" = "$h2" ] || { echo "FATAL: copied PDF hash mismatch"; exit 1; }
n=$(find "$DEST" -type f ! -name .DS_Store | wc -l | tr -d ' ')
[ "$n" -eq 2 ] || { echo "FATAL: destination holds $n files, must be exactly 2"; exit 1; }
echo "published: $DEST"
echo "  $(basename "$PDF")  (hash-verified)"
echo "  $(basename "$CLAIMS")"
