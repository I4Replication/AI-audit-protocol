#!/bin/bash
# publish_completed.sh <audit-dir> [completed-root]
#
# Files the finished audit into the team's completed-reports tree (decided 2026-08-21, extended
# 2026-08-22):
#
#     <discipline-root>/Completed reports/<Journal>/<Paper>/
#         <comment>.pdf          the final built PDF, hash-verified against report/
#         claims_script.<ext>    the numbered-claims code file
#         run.json               how the audit ran
#         comment.json           what it found, coded against SCHEME.md
#
# Journal and paper names are derived from the audit's own path, which is
# <discipline-root>/<Journal>/<Paper>/audit. The destination holds exactly these four files;
# anything else already there blocks the publish rather than being silently buried.
#
# Why the metadata pair lives here as well as in the registry: the registry is keyed by audit id
# and is not browsable by paper. Someone looking at a finished comment wants the record of how it
# was produced beside it, not in a directory named after a hash. The registry copy remains the
# canonical one; these are copies, and finalize_run.sh must have filed the pair before this runs.
#
# THESE TWO FILES ARE INTERNAL. Both carry safe_to_publish=false and a publication_redaction block
# naming what must be removed before any extract is released. "Completed reports" is an internal
# index, so copying them here does not release them — but do not forward this folder outward
# without reading that block first.
set -uo pipefail
A="${1:?audit dir}"; A="$(cd "$A" && pwd)"
PAPER_DIR="$(dirname "$A")"; JOURNAL_DIR="$(dirname "$PAPER_DIR")"; ROOT="$(dirname "$JOURNAL_DIR")"
DEST_ROOT="${2:-$ROOT/Completed reports}"
PAPER="$(basename "$PAPER_DIR")"; JOURNAL="$(basename "$JOURNAL_DIR")"

PDF=$(ls "$A/report/"*.pdf 2>/dev/null | grep -viE "_proxy|preview" | head -1)
[ -n "$PDF" ] || { echo "FATAL: no built PDF in $A/report — run build_docx.sh first"; exit 1; }
CLAIMS=$(ls "$A"/claims_script.* 2>/dev/null | head -1)
[ -n "$CLAIMS" ] || { echo "FATAL: no claims_script.* in $A — run make_claims_script.sh first"; exit 1; }
RUN="$A/run.json"; COMMENT="$A/comment.json"
[ -f "$RUN" ]     || { echo "FATAL: no run.json in $A — run init_run.sh and finalize_run.sh first"; exit 1; }
[ -f "$COMMENT" ] || { echo "FATAL: no comment.json in $A — write it, then finalize_run.sh"; exit 1; }

# Refuse to publish a pair finalize_run.sh has not accepted: an unvalidated record filed beside a
# finished comment reads as authoritative and is not.
python3 - "$RUN" "$COMMENT" <<'PYCHK' || exit 1
import json, sys
try:
    r = json.load(open(sys.argv[1])); c = json.load(open(sys.argv[2]))
except Exception as e:
    print("FATAL: metadata is not valid JSON: %s" % e); sys.exit(1)
if not r.get("ended_at"):
    print("FATAL: run.json has no ended_at — finalize_run.sh has not been run"); sys.exit(1)
if r.get("audit_id") != c.get("audit_id"):
    print("FATAL: run.json and comment.json disagree on audit_id"); sys.exit(1)
if not c.get("findings"):
    print("FATAL: comment.json carries no findings"); sys.exit(1)
print("  metadata pair: %s, %d findings, finalized %s"
      % (r["audit_id"], len(c["findings"]), r["ended_at"]))
PYCHK

DEST="$DEST_ROOT/$JOURNAL/$PAPER"
mkdir -p "$DEST"
KEEP=("$(basename "$PDF")" "$(basename "$CLAIMS")" run.json comment.json)
extra=$(find "$DEST" -type f ! -name ".DS_Store" \
        ! -name "${KEEP[0]}" ! -name "${KEEP[1]}" ! -name run.json ! -name comment.json | head -3)
[ -n "$extra" ] && { echo "FATAL: destination already holds other files — the folder carries exactly four:"; echo "$extra"; exit 1; }

cp "$PDF" "$DEST/"; cp "$CLAIMS" "$DEST/"; cp "$RUN" "$DEST/"; cp "$COMMENT" "$DEST/"
for f in "$(basename "$PDF")" "$(basename "$CLAIMS")" run.json comment.json; do
  src="$A/$f"; [ "$f" = "$(basename "$PDF")" ] && src="$PDF"
  [ "$f" = "$(basename "$CLAIMS")" ] && src="$CLAIMS"
  h1=$(shasum -a 256 "$src" | cut -d' ' -f1); h2=$(shasum -a 256 "$DEST/$f" | cut -d' ' -f1)
  [ "$h1" = "$h2" ] || { echo "FATAL: copied $f hash mismatch"; exit 1; }
done
n=$(find "$DEST" -type f ! -name .DS_Store | wc -l | tr -d ' ')
[ "$n" -eq 4 ] || { echo "FATAL: destination holds $n files, must be exactly 4"; exit 1; }
echo "published: $DEST"
echo "  $(basename "$PDF")  (hash-verified)"
echo "  $(basename "$CLAIMS")  (hash-verified)"
echo "  run.json  (hash-verified, internal — safe_to_publish=false)"
echo "  comment.json  (hash-verified, internal — safe_to_publish=false)"
