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
#         finalization_receipt.json  immutable finalization/registry bindings
#         release-manifest.json  hashes binding the five primary artifacts
#
# Journal and paper names are derived from the audit's own path, which is
# <discipline-root>/<Journal>/<Paper>/audit. The destination holds exactly six files;
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
A="${1:?audit dir}"; A="$(cd "$A" && pwd -P)"
PAPER_DIR="$(dirname "$A")"; JOURNAL_DIR="$(dirname "$PAPER_DIR")"; ROOT="$(dirname "$JOURNAL_DIR")"
DEST_ROOT="${2:-$ROOT/Completed reports}"
PAPER="$(basename "$PAPER_DIR")"; JOURNAL="$(basename "$JOURNAL_DIR")"

PDFS=(); for f in "$A"/report/*.pdf; do
  [ -f "$f" ] || continue
  case "$(basename "$f")" in *_proxy*|*preview*) continue ;; esac
  PDFS+=("$f")
done
[ "${#PDFS[@]}" -eq 1 ] || { echo "FATAL: expected exactly one final PDF, found ${#PDFS[@]}"; exit 1; }
PDF="${PDFS[0]}"
CLAIM_FILES=(); for f in "$A"/claims_script.*; do [ -f "$f" ] && CLAIM_FILES+=("$f"); done
[ "${#CLAIM_FILES[@]}" -eq 1 ] || { echo "FATAL: expected exactly one claims_script.*, found ${#CLAIM_FILES[@]}"; exit 1; }
CLAIMS="${CLAIM_FILES[0]}"
RUN="$A/run.json"; COMMENT="$A/comment.json"
[ -f "$RUN" ]     || { echo "FATAL: no run.json in $A — run init_run.sh and finalize_run.sh first"; exit 1; }
[ -f "$COMMENT" ] || { echo "FATAL: no comment.json in $A — write it, then finalize_run.sh"; exit 1; }
for input in "$PDF" "$CLAIMS" "$RUN" "$COMMENT" "$A/finalization_receipt.json"; do
  [ -f "$input" ] && [ ! -L "$input" ] || { echo "FATAL: publication input is missing or symlinked: $input"; exit 1; }
done

python3 "$(cd "$(dirname "$0")" && pwd)/generate_finding_artifacts.py" "$A" --check || {
  echo "FATAL: canonical finding projections are missing or divergent" >&2; exit 1
}
python3 "$(cd "$(dirname "$0")" && pwd)/validate_round_projections.py" "$A" --require-post-delta-gates || {
  echo "FATAL: immutable critique-round projection binding is stale" >&2; exit 1
}

# Validate the append-only interface itself, not only finalize's aggregate fraction. This covers
# observed served identity, requested effort, elapsed time, outcome, cache disposition and every
# token category for every provider call.
python3 "$(cd "$(dirname "$0")" && pwd)/summarize_invocations.py" "$A" --check >/dev/null || {
  echo "FATAL: invocation telemetry is incomplete or violates the configured model policy" >&2
  exit 1
}

python3 "$(cd "$(dirname "$0")" && pwd)/finalization_receipt.py" check "$A" \
  --require-registry --require-publication || {
  echo "FATAL: audit lacks a current, registry-filed publication receipt" >&2
  exit 1
}

python3 "$(cd "$(dirname "$0")" && pwd)/validate_json_schema.py" \
  "$RUN" "$(cd "$(dirname "$0")/../schemas" && pwd)/run.schema.json" >/dev/null || exit 1
python3 "$(cd "$(dirname "$0")" && pwd)/validate_json_schema.py" \
  "$COMMENT" "$(cd "$(dirname "$0")/../schemas" && pwd)/comment.schema.json" >/dev/null || exit 1
if python3 - "$RUN" <<'PYV2'
import json,sys
raise SystemExit(0 if json.load(open(sys.argv[1])).get("schema_version") == "2.0.0" else 1)
PYV2
then
  python3 "$(cd "$(dirname "$0")/../schemas/tools" && pwd)/validate_v2.py" run "$RUN" || exit 1
fi

# Refuse post-finalize edits that would leave the published comment outside the verification
# binding. finalize_run.sh rebinds deterministic derived metadata and rechecks it atomically.
python3 - "$A" <<'PYBIND' || exit 1
import hashlib, json, pathlib, sys
root=pathlib.Path(sys.argv[1]); manifest=root/"verification_manifest.json"
if not manifest.is_file(): raise SystemExit("FATAL: verification_manifest.json is missing")
m=json.loads(manifest.read_text())
def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda:handle.read(1024*1024),b""): h.update(block)
    return h.hexdigest()
for label in ("report","comment","summary","notebook"):
    item=m.get(label) or {}; path=(root/item.get("path","")).resolve()
    try: path.relative_to(root.resolve())
    except ValueError: raise SystemExit(f"FATAL: verification manifest {label} path escapes audit root")
    if not path.is_file() or item.get("sha256") != digest(path):
        raise SystemExit(f"FATAL: current {label} does not match verification_manifest.json")
if m.get("status") != 0: raise SystemExit("FATAL: verification manifest status is not zero")
status=m.get("status_artifact") or {}; status_path=(root/status.get("path","")).resolve()
try: status_path.relative_to(root.resolve())
except ValueError: raise SystemExit("FATAL: verification status-artifact path escapes audit root")
if not status_path.is_file() or status.get("sha256") != digest(status_path):
    raise SystemExit("FATAL: current status artifact does not match verification_manifest.json")
render=m.get("render") or {}
if render.get("mode") != "executed" or render.get("exit_status") != 0:
    raise SystemExit("FATAL: verification manifest does not prove a successful notebook execution")
PYBIND

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
if r.get("status") != "complete":
    print("FATAL: run status is not complete"); sys.exit(1)
if not (r.get("compliance") or {}).get("compliance_pass"):
    print("FATAL: run compliance gate did not pass"); sys.exit(1)
if (r.get("token_accounting") or {}).get("coverage_fraction") != 1:
    print("FATAL: model/token telemetry coverage is not 100%"); sys.exit(1)
print("  metadata pair: %s, %d findings, finalized %s"
      % (r["audit_id"], len(c["findings"]), r["ended_at"]))
PYCHK

DEST="$DEST_ROOT/$JOURNAL/$PAPER"
mkdir -p "$(dirname "$DEST")"
LOCK="${DEST}.publish.lock"
mkdir "$LOCK" 2>/dev/null || { echo "FATAL: destination is published, locked, or a stale lock exists: $DEST"; exit 1; }
[ ! -e "$DEST" ] || { rmdir "$LOCK"; echo "FATAL: destination already exists; publication never overwrites: $DEST"; exit 1; }
PREP=$(mktemp -d "$(dirname "$DEST")/.${PAPER}.publish.XXXXXX") || { rmdir "$LOCK"; exit 1; }
cleanup_publish () {
  if [ -n "${PREP:-}" ] && [ -d "$PREP" ]; then rm -rf "$PREP"; fi
  if [ -n "${LOCK:-}" ] && [ -d "$LOCK" ]; then rmdir "$LOCK" 2>/dev/null || true; fi
}
trap cleanup_publish EXIT
trap 'cleanup_publish; exit 129' HUP
trap 'cleanup_publish; exit 130' INT
trap 'cleanup_publish; exit 143' TERM
RECEIPT="$A/finalization_receipt.json"
cp "$PDF" "$PREP/" && cp "$CLAIMS" "$PREP/" && cp "$RUN" "$PREP/" && \
  cp "$COMMENT" "$PREP/" && cp "$RECEIPT" "$PREP/" || exit 1
python3 - "$PREP" "$PDF" "$CLAIMS" "$RUN" "$COMMENT" "$RECEIPT" <<'PYMAN' || exit 1
import datetime, hashlib, json, os, pathlib, sys, tempfile
dest=pathlib.Path(sys.argv[1]); sources=list(map(pathlib.Path,sys.argv[2:]))
def h(p):
    x=hashlib.sha256()
    with p.open("rb") as fh:
        for b in iter(lambda:fh.read(1024*1024),b""):x.update(b)
    return x.hexdigest()
rows=[]
for src in sources:
    copied=dest/src.name
    if h(src)!=h(copied): raise SystemExit(f"hash mismatch after staging {src.name}")
    rows.append({"file":src.name,"sha256":h(src),"bytes":src.stat().st_size})
run=json.load(open(sys.argv[4]))
manifest={"schema_version":"2.0.0","audit_id":run["audit_id"],
          "created_at":datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "files":rows}
(dest/"release-manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
count=sum(1 for path in dest.iterdir() if path.is_file())
if count != 6: raise SystemExit(f"staged publication has {count} files, expected six")
PYMAN
# Inspect the exact immutable candidate tree, including PDF text/metadata and
# the portable receipt.  This runs after all six files exist and before the
# atomic rename makes any of them visible at the completed-report path.
python3 "$(cd "$(dirname "$0")" && pwd)/check_release_privacy.py" "$PREP" --identifiers || {
  echo "FATAL: completed-report transaction failed privacy inspection" >&2
  exit 1
}
python3 - "$PREP" "$DEST" <<'PYRENAME' || exit 1
import os,pathlib,sys
source,destination=map(pathlib.Path,sys.argv[1:])
if destination.exists(): raise SystemExit(f"destination appeared while publication lock was held: {destination}")
os.rename(source,destination)
PYRENAME
PREP=""
n=$(find "$DEST" -type f ! -name .DS_Store | wc -l | tr -d ' ')
[ "$n" -eq 6 ] || { echo "FATAL: destination holds $n files, must be five artefacts plus manifest"; exit 1; }
# a filed audit releases its reservation so the pool stays accurate (2026-08-24)
SKILLDIR="$(cd "$(dirname "$0")" && pwd)"
[ -x "$SKILLDIR/reserve_paper.sh" ] && "$SKILLDIR/reserve_paper.sh" release "$PAPER_DIR" || true

echo "published: $DEST"
echo "  $(basename "$PDF")  (hash-verified)"
echo "  $(basename "$CLAIMS")  (hash-verified)"
echo "  run.json  (hash-verified, internal — safe_to_publish=false)"
echo "  comment.json  (hash-verified, internal — safe_to_publish=false)"
echo "  finalization_receipt.json  (hash-verified registry and build bindings)"
echo "  release-manifest.json  (exact file list and hashes)"
