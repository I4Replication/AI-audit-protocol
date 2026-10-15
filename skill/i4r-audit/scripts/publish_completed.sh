#!/bin/bash
# publish_completed.sh <audit-dir> [tree-root]
#
# Files the finished audit into the tree's two publication folders (layout 2.5.0; see
# scripts/tree_layout.sh):
#
#     <root>/comments/<Journal>/<Paper>/       what a reader wants
#         <comment>.pdf          the final built PDF, hash-verified against report/
#         <comment>.docx         the editable artifact, when the document manifest binds one
#     <root>/metadata/<Journal>/<Paper>/       how the comment was produced
#         claims_script.<ext>    the numbered-claims code file
#         run.json               how the audit ran
#         comment.json           what it found, coded against SCHEME.md
#         finalization_receipt.json  immutable finalization/registry bindings
#         document_build_manifest.json  exact document/page/code binding (schema 3)
#         release-manifest.json  hashes binding the complete publication, both folders
#
# Journal and paper names come from paper_source.json (written at intake), else from the audit's
# path: <root>/runs/<Journal>/<Paper>/audit, or the legacy <root>/<Journal>/<Paper>/audit. Both
# destinations hold only the exact bound set; anything already there blocks the publish rather
# than being silently buried. The optional second argument must be the audit's own tree root (only test
# fixtures publish elsewhere, with I4R_PUBLISH_ANY_ROOT=1); options such as --registry are refused.
#
# Why the metadata pair is filed here as well as in the registry: the registry is keyed by audit id
# and is not browsable by paper. The registry copy remains the canonical one; these are copies, and
# finalize_run.sh must have filed the pair before this runs.
#
# run.json AND comment.json ARE INTERNAL. Both carry safe_to_publish=false and a publication_redaction
# block naming what must be removed before any extract is released. metadata/ is an internal index,
# so copying them there does not release them -- but do not forward that folder outward without
# reading that block first.
set -uo pipefail
case "${1:-}" in
  -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  -*) echo "FATAL: publish_completed.sh <audit-dir> [tree-root]: unknown option ${1}" >&2; exit 2 ;;
esac
case "${2:-}" in
  -*) echo "FATAL: publish_completed.sh takes no ${2} option; it reads the registry from finalization_receipt.json (only finalize_run.sh takes --registry). Run: publish_completed.sh <audit-dir>" >&2; exit 2 ;;
esac
A="${1:?audit dir}"; A_LOGICAL="$(cd "$A" && pwd)"; A="$(cd "$A" && pwd -P)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
DOCUMENT_RELEASE="$SCRIPT_DIR/document_release.py"
. "$SCRIPT_DIR/tree_layout.sh"
# the tree comes from the audit's path as given (finalize derives its registry the same way): through a
# symlinked runs/<journal> the physical path names another folder, and publish filed there instead
# but a symlink to the audit (or paper) folder itself does not sit in a tree: then the real path decides, and
# a path that is in no tree at all is refused rather than filed beside the symlink
# a tree is what finalize files into: its markers, or a registry/ folder (finalize's own test)
publish_tree_root () { i4r_is_tree_root "$1" || [ -d "$1/registry" ]; }
A_TREE="$A_LOGICAL"
if ! publish_tree_root "$(i4r_root_of_audit "$A_LOGICAL")"; then A_TREE="$A"; fi
ROOT="$(i4r_root_of_audit "$A_TREE")"
if ! publish_tree_root "$ROOT" && [ "${I4R_PUBLISH_ANY_ROOT:-0}" != "1" ]; then
  echo "FATAL: $A is not inside a discipline tree (<tree>/runs/<journal>/<paper>/audit, or the legacy layout);" >&2
  echo "       no comments/ and metadata/ folders to publish into. Run publish on the audit inside its tree." >&2
  exit 2
fi
PAPER_DIR="$(i4r_paper_dir_of_audit "$A_TREE")"
PAPER="$(i4r_paper_name_of_audit "$A_TREE")"; JOURNAL="$(i4r_journal_of_audit "$A_TREE")"
TREE_ROOT="${2:-$ROOT}"
# the optional second argument is a tree root (fixtures); the registry folder is not one -- publishing there
# filed comments/ and metadata/ inside registry/ and reported success
if [ $# -gt 2 ]; then
  echo "FATAL: publish_completed.sh <audit-dir> [tree-root]: unexpected argument ${3}" >&2; exit 2
fi
if [ -n "${2:-}" ] && { [ "$(basename "$TREE_ROOT")" = "registry" ] || [ -f "$TREE_ROOT/index.jsonl" ]; }; then
  echo "FATAL: the second argument $TREE_ROOT is a registry folder, not a tree root. Run publish_completed.sh <audit-dir>" >&2
  echo "       (the registry comes from finalization_receipt.json; only finalize_run.sh takes --registry)." >&2
  exit 2
fi
# Any other folder filed comments/ and metadata/ inside it (e.g. <tree>/comments/comments/...) and released the
# reservation. The second argument must be the audit's own tree root; test fixtures that publish into a scratch
# root opt in with I4R_PUBLISH_ANY_ROOT=1.
if [ -n "${2:-}" ] && [ "$(cd "$TREE_ROOT" 2>/dev/null && pwd -P)" != "$(cd "$ROOT" 2>/dev/null && pwd -P)" ] \
    && [ "${I4R_PUBLISH_ANY_ROOT:-0}" != "1" ]; then
  echo "FATAL: the second argument $TREE_ROOT is not this audit's tree root ($ROOT). Run publish_completed.sh <audit-dir>;" >&2
  echo "       the tree root is derived from the audit's location." >&2
  exit 2
fi
# paper_source.json is written at intake and is authoritative: the path derivation above is only
# correct when the run folder carries the paper's own name, which it cannot when that name holds a
# character that disarms the sandbox (the run folder then drops it).
if [ -f "$A/paper_source.json" ]; then
  PS_J="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("journal") or "")' "$A/paper_source.json" 2>/dev/null || true)"
  PS_P="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("paper") or "")' "$A/paper_source.json" 2>/dev/null || true)"
  # Only the names are overridden. ROOT stays as derived above: the audit dir is still required
  # to sit at <root>/runs/<journal>/<name>/audit (or the legacy <root>/<journal>/<name>/audit).
  if [ -n "$PS_J" ] && [ -n "$PS_P" ]; then
    JOURNAL="$PS_J"; PAPER="$PS_P"
    # The reservation was claimed under the TRUE paper folder (I4R_PAPER_DIR at intake), so the
    # release at the end must name that folder too, or a relocated audit's claim stays on the
    # board forever and status.py keeps flagging a finished paper.
    if [ -d "$ROOT/data/$PS_J/$PS_P" ]; then PAPER_DIR="$ROOT/data/$PS_J/$PS_P"
    elif [ -d "$ROOT/$PS_J/$PS_P" ]; then PAPER_DIR="$ROOT/$PS_J/$PS_P"; fi
  fi
fi
CDEST="$(i4r_published_comment_dir "$TREE_ROOT" "$JOURNAL" "$PAPER")"
DEST="$(i4r_published_metadata_dir "$TREE_ROOT" "$JOURNAL" "$PAPER")"

if [ ! -f "$A/finalization_receipt.json" ]; then
  echo "FATAL: $A has no finalization_receipt.json: publish runs last. Build the document (build_docx.sh), stage," >&2
  echo "  log delivery and run finalize_run.sh (SKILL.md section 5) first." >&2
  exit 1
fi
python3 "$DOCUMENT_RELEASE" verify "$A" --builder "$SCRIPT_DIR/build_docx.sh" \
  --boundary publish-source || exit 1
BOUND_PATHS=$(python3 "$DOCUMENT_RELEASE" paths "$A/document_build_manifest.json") || exit 1
BOUND_PDF=$(printf '%s\n' "$BOUND_PATHS" | sed -n '1p')
BOUND_DOCX=$(printf '%s\n' "$BOUND_PATHS" | sed -n '2p')
[ -n "$BOUND_PDF" ] || { echo "FATAL: document manifest has no PDF" >&2; exit 1; }
PDF="$A/$BOUND_PDF"
DOCX=""
[ -z "$BOUND_DOCX" ] || DOCX="$A/$BOUND_DOCX"
DOCUMENT_MANIFEST="$A/document_build_manifest.json"
CLAIM_FILES=(); for f in "$A"/claims_script.*; do [ -f "$f" ] && CLAIM_FILES+=("$f"); done
[ "${#CLAIM_FILES[@]}" -eq 1 ] || { echo "FATAL: expected exactly one claims_script.*, found ${#CLAIM_FILES[@]}"; exit 1; }
CLAIMS="${CLAIM_FILES[0]}"
RUN="$A/run.json"; COMMENT="$A/comment.json"
[ -f "$RUN" ]     || { echo "FATAL: no run.json in $A — run init_run.sh and finalize_run.sh first"; exit 1; }
[ -f "$COMMENT" ] || { echo "FATAL: no comment.json in $A — write it, then finalize_run.sh"; exit 1; }
for input in "$PDF" "$CLAIMS" "$RUN" "$COMMENT" "$A/finalization_receipt.json" "$DOCUMENT_MANIFEST"; do
  [ -f "$input" ] && [ ! -L "$input" ] || { echo "FATAL: publication input is missing or symlinked: $input"; exit 1; }
done
if [ -n "$DOCX" ]; then
  [ -f "$DOCX" ] && [ ! -L "$DOCX" ] || { echo "FATAL: bound DOCX is missing or symlinked: $DOCX"; exit 1; }
fi

python3 "$(cd "$(dirname "$0")" && pwd)/generate_finding_artifacts.py" "$A" --check || {
  echo "FATAL: canonical finding projections are missing or divergent" >&2; exit 1
}
python3 "$(cd "$(dirname "$0")" && pwd)/validate_round_projections.py" "$A" --require-post-delta-gates || {
  echo "FATAL: the critique-round check above failed; fix the cause named in its FATAL line, re-run finalize_run.sh, then publish again" >&2; exit 1
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
  echo "  Follow the fix named above. If it names a journal event (re-log it) or a stale finalize, re-running" >&2
  echo "  finalize_run.sh is enough. If a released file changed after finalize, rebuild what depends on it," >&2
  echo "  snapshot REPORT_v<N+1>.md, stage into a fresh directory, re-log delivery (bash scripts/log_event.sh delivery" >&2
  echo "  artifact_path=report/<output-name>.pdf recipient=...) and then phase_end phase=release, and re-run" >&2
  echo "  finalize_run.sh; then publish again." >&2
  exit 1
}

python3 "$(cd "$(dirname "$0")" && pwd)/validate_json_schema.py" \
  "$RUN" "$(cd "$(dirname "$0")/../schemas" && pwd)/run.schema.json" >/dev/null || exit 1
python3 "$(cd "$(dirname "$0")" && pwd)/validate_json_schema.py" \
  "$COMMENT" "$(cd "$(dirname "$0")/../schemas" && pwd)/comment.schema.json" >/dev/null || exit 1
if ! python3 - "$RUN" <<'PY21'
import json,sys
raise SystemExit(0 if json.load(open(sys.argv[1])).get("schema_version") == "2.1.0" else 1)
PY21
then
  echo "FATAL: every new publication or republication requires metadata schema 2.1.0" >&2
  exit 1
fi
python3 "$(cd "$(dirname "$0")/../schemas/tools" && pwd)/validate_v2.py" run "$RUN" || exit 1

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
if r.get("schema_version") != "2.1.0":
    print("FATAL: publication requires metadata schema 2.1.0"); sys.exit(1)
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

mkdir -p "$(dirname "$DEST")" "$(dirname "$CDEST")"
LOCK="${DEST}.publish.lock"
# The lock is a FILE created O_EXCL and holding the owner's pid -- not a directory. The
# directory form was removed by the EXIT trap and then resurrected, empty, by the synced folder
# this tree lives in, so every published paper left a `<name>.publish.lock/` beside its report.
# A live owner blocks; one whose owner is gone, or which carries no pid at all and is older
# than a minute, is broken with a warning rather than wedging publication until someone
# deletes it. A legacy directory lock is judged by the same rules.
lock_owner_pid () {
  # Total by construction: a lock with no pid file (the resurrected legacy directory is exactly
  # that) yields an empty owner and a zero status, whatever shell options are in force.
  local pid_source="$LOCK"
  [ -d "$LOCK" ] && pid_source="$LOCK/pid"
  { cat "$pid_source" 2>/dev/null || true; } | head -1 | tr -d '[:space:]' || true
}
try_publish_lock () {
  python3 - "$LOCK" "$$" <<'PYLOCK'
import os, sys
try:
    fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
except FileExistsError:
    raise SystemExit(1)
with os.fdopen(fd, "w") as handle:
    handle.write(sys.argv[2] + "\n")
PYLOCK
}
if ! try_publish_lock 2>/dev/null; then
  lock_owner=$(lock_owner_pid)
  lock_age=$(( $(date +%s) - $( (stat -f %m "$LOCK" 2>/dev/null || stat -c %Y "$LOCK" 2>/dev/null) || echo 0) ))
  if [ -n "$lock_owner" ] && kill -0 "$lock_owner" 2>/dev/null; then
    echo "FATAL: another publication is running (pid $lock_owner) for: $DEST" >&2
    exit 1
  fi
  if [ -n "$lock_owner" ]; then
    echo "WARNING: breaking a publish lock held by dead pid $lock_owner" >&2
  elif [ "$lock_age" -ge 60 ]; then
    echo "WARNING: breaking an orphaned publish lock with no owner (${lock_age}s old)" >&2
  else
    echo "FATAL: a publish lock was just created by another process for: $DEST" >&2
    echo "       If nothing else is publishing, re-run in a minute or remove: $LOCK" >&2
    exit 1
  fi
  rm -rf "$LOCK"
  try_publish_lock 2>/dev/null || {
    echo "FATAL: could not acquire the publish lock: $LOCK" >&2
    echo "       Remove it by hand if no other publication is running." >&2
    exit 1; }
fi
for existing in "$DEST" "$CDEST"; do
  [ ! -e "$existing" ] || { rm -rf "$LOCK"; echo "FATAL: destination already exists; publication never overwrites: $existing"
    echo "       To correct a published comment, see INSTALL.md 'Correcting a published comment'." >&2; exit 1; }
done
PREP=$(mktemp -d "$(dirname "$DEST")/.${PAPER}.publish.XXXXXX") || { rm -rf "$LOCK"; exit 1; }
PREP_C=""
PUBLISHED_DEST=""
PUBLISHED_CDEST=""
PUBLISH_ACCEPTED=0
quarantine () {  # <visible dir> -> moves it back out of the namespace; echoes the quarantine path
  local visible="$1" rollback
  rollback=$(mktemp -d "$(dirname "$visible")/.${PAPER}.rollback.XXXXXX" 2>/dev/null || true)
  [ -n "$rollback" ] || { echo "FATAL: could not allocate publication rollback quarantine" >&2; return 1; }
  rmdir "$rollback" 2>/dev/null || true
  mv "$visible" "$rollback" 2>/dev/null || { echo "FATAL: could not quarantine failed publication destination: $visible" >&2; return 1; }
  printf '%s' "$rollback"
}
cleanup_publish () {
  # Both destinations are new and held under our publish lock.  If any exact-count, privacy, or
  # hash check fails after the atomic renames, move them back out of the visible namespace before
  # deleting the private transactions.  A failed command must never leave an apparently completed
  # report behind -- in either folder.
  local q
  if [ "$PUBLISH_ACCEPTED" -ne 1 ]; then
    if [ -n "${PUBLISHED_DEST:-}" ] && [ -d "$PUBLISHED_DEST" ]; then
      q=$(quarantine "$PUBLISHED_DEST") && { PREP="$q"; PUBLISHED_DEST=""; }
    fi
    if [ -n "${PUBLISHED_CDEST:-}" ] && [ -d "$PUBLISHED_CDEST" ]; then
      q=$(quarantine "$PUBLISHED_CDEST") && { PREP_C="$q"; PUBLISHED_CDEST=""; }
    fi
  fi
  if [ -n "${PREP:-}" ] && [ -d "$PREP" ]; then rm -rf "$PREP"; fi
  if [ -n "${PREP_C:-}" ] && [ -d "$PREP_C" ]; then rm -rf "$PREP_C"; fi
  if [ -n "${LOCK:-}" ] && [ -e "$LOCK" ]; then rm -rf "$LOCK" 2>/dev/null || true; fi
}
trap cleanup_publish EXIT
trap 'cleanup_publish; exit 129' HUP
trap 'cleanup_publish; exit 130' INT
trap 'cleanup_publish; exit 143' TERM
RECEIPT="$A/finalization_receipt.json"
PUBLISH_SOURCES=("$PDF" "$CLAIMS" "$RUN" "$COMMENT" "$RECEIPT" "$DOCUMENT_MANIFEST")
[ -z "$DOCX" ] || PUBLISH_SOURCES+=("$DOCX")
for source in "${PUBLISH_SOURCES[@]}"; do cp "$source" "$PREP/" || exit 1; done
python3 - "$PREP" "${PUBLISH_SOURCES[@]}" <<'PYMAN' || exit 1
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
run=json.loads((dest/"run.json").read_text())
document=json.loads((dest/"document_build_manifest.json").read_text())
if document.get("schema_version") != "3.0.0" or document.get("release_eligible") is not True:
    raise SystemExit("publication requires a release-eligible document manifest v3")
pdf_record=(document.get("artifacts") or {}).get("pdf") or {}
published_pdf=dest/pathlib.PurePosixPath(pdf_record.get("path","")).name
if not published_pdf.is_file() or pdf_record.get("sha256") != h(published_pdf):
    raise SystemExit("published PDF is not the document-manifest-bound PDF")
manifest={"schema_version":"3.0.0","audit_id":run["audit_id"],
          "created_at":datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "document_manifest_sha256":h(dest/"document_build_manifest.json"),
          "document_pdf_sha256":pdf_record["sha256"],
          "document_pages":document.get("pages"),"files":rows}
(dest/"release-manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
count=sum(1 for path in dest.iterdir() if path.is_file())
expected=len(sources)+1
if count != expected: raise SystemExit(f"staged publication has {count} files, expected {expected}")
PYMAN
# Recount the flattened publication candidate independently of the audit-tree verification above.
python3 "$DOCUMENT_RELEASE" recount "$PREP/document_build_manifest.json" \
  "$PREP/$(basename "$PDF")" >/dev/null || exit 1
# Inspect the exact immutable candidate tree, including PDF text/metadata and
# the portable receipt.  This runs after all six files exist and before the
# atomic rename makes any of them visible at the completed-report path.
# The flattened candidate has no report/front_matter.md, so name the audit's own byline and article
# text as the public email sources (the byline's correspondence address is mandatory content).
python3 "$(cd "$(dirname "$0")" && pwd)/check_release_privacy.py" "$PREP" --identifiers \
    --allow-emails-from "$A/report/front_matter.md" --allow-emails-from "$A/paper/paper.txt" || {
  echo "FATAL: completed-report transaction failed privacy inspection" >&2
  exit 1
}
# 2.6.8: staging check 3c again, on the exact candidate set. run.json is never staged, so this is
# the first time it meets the deposit's values, and comment.json may have been re-finalized since
# staging. Locations and value kinds only; a deposit file that cannot be read never blocks
# publication. Exit 1: a value was found (reword and rebuild). Exit 2: the set could not be
# inspected (a document that cannot be read, a missing extractor, an internal error): fix that and
# re-run; the text needs no rewording on this account.
python3 "$SCRIPT_DIR/check_deposit_values.py" --self-test >/dev/null || {
  echo "FATAL: the deposit-value detector failed its self-test; run it with --self-test" >&2
  exit 1
}
DEPOSIT_VALUES_STATUS=0
python3 "$SCRIPT_DIR/check_deposit_values.py" "$A" "$PREP" || DEPOSIT_VALUES_STATUS=$?
case "$DEPOSIT_VALUES_STATUS" in
  0) ;;
  1) echo "FATAL: the publication set reproduces an identifying value from the deposit, or an e-mail or IP address: reword the finding (see the locations above), rebuild and restage" >&2
     exit 1 ;;
  2) echo "FATAL: check 3c could not inspect the publication set (see above): fix the document or the environment and re-run; the text needs no rewording on this account" >&2
     exit 1 ;;
  *) echo "FATAL: check 3c stopped unexpectedly (exit $DEPOSIT_VALUES_STATUS), so nothing was published" >&2
     exit 1 ;;
esac
# Split the verified set: the comment (PDF, DOCX) goes to comments/, the record to metadata/.
# Both moves are renames within one volume, done under the lock; a failure after either one is
# rolled back by cleanup_publish for both.
PREP_C=$(mktemp -d "$(dirname "$CDEST")/.${PAPER}.publish.XXXXXX") || exit 1
mv "$PREP/$(basename "$PDF")" "$PREP_C/" || exit 1
[ -z "$DOCX" ] || mv "$PREP/$(basename "$DOCX")" "$PREP_C/" || exit 1
python3 - "$PREP" "$DEST" "$PREP_C" "$CDEST" <<'PYRENAME' || exit 1
import os,pathlib,sys
pairs=list(zip(map(pathlib.Path,sys.argv[1::2]),map(pathlib.Path,sys.argv[2::2])))
for _,destination in pairs:
    if destination.exists(): raise SystemExit(f"destination appeared while publication lock was held: {destination}")
for source,destination in pairs:
    os.rename(source,destination)
PYRENAME
PUBLISHED_DEST="$DEST"; PUBLISHED_CDEST="$CDEST"
PREP=""; PREP_C=""
EXPECTED_COMMENT=1; [ -z "$DOCX" ] || EXPECTED_COMMENT=2
EXPECTED_META=$(( ${#PUBLISH_SOURCES[@]} + 1 - EXPECTED_COMMENT ))
n=$(find "$DEST" -type f ! -name .DS_Store | wc -l | tr -d ' ')
[ "$n" -eq "$EXPECTED_META" ] || {
  echo "FATAL: metadata destination holds $n files, expected $EXPECTED_META manifest-bound files"; exit 1; }
n=$(find "$CDEST" -type f ! -name .DS_Store | wc -l | tr -d ' ')
[ "$n" -eq "$EXPECTED_COMMENT" ] || {
  echo "FATAL: comment destination holds $n files, expected $EXPECTED_COMMENT manifest-bound files"; exit 1; }
python3 "$DOCUMENT_RELEASE" recount "$DEST/document_build_manifest.json" \
  "$CDEST/$(basename "$PDF")" >/dev/null || {
  echo "FATAL: published PDF failed the post-rename exact recount" >&2; exit 1; }
PUBLISH_ACCEPTED=1
# The filing itself is journaled: a coordinator-logged delivery can only describe a staging
# directory, which is mutable and predates the gates. This event names the immutable published set
# by its release-manifest hash, with a discipline-root-relative destination (never a local path).
# It is appended after the rename, so the run.json copy filed a moment earlier does not contain it;
# the canonical journal in the audit directory does.
RELEASE_MANIFEST_SHA=$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$DEST/release-manifest.json" 2>/dev/null)
if [ -n "$RELEASE_MANIFEST_SHA" ] && [ -f "$A/run.json" ]; then
  bash "$(cd "$(dirname "$0")" && pwd)/log_event.sh" --run "$A/run.json" delivery \
    artifact_path="$(basename "$PDF")" artifact_sha256="$RELEASE_MANIFEST_SHA" \
    release_dir="${CDEST#"$TREE_ROOT"/}" recipient="comments/ and metadata/ (filed publication set)" >/dev/null 2>&1 \
    || echo "warning: the filing was not journaled (run.json is unwritable or the event was rejected)" >&2
fi
# a filed audit releases its reservation so the pool stays accurate (2026-08-24)
SKILLDIR="$(cd "$(dirname "$0")" && pwd)"
if [ -x "$SKILLDIR/reserve_paper.sh" ]; then
  # --force: the paper is now filed, so its reservation is moot whoever holds it. The claimant is
  # often the same person on another machine (the owner is user@host), which a plain release refuses.
  "$SKILLDIR/reserve_paper.sh" release "$PAPER_DIR" --force \
    || echo "warning: the reservation for $(basename "$PAPER_DIR") was not released; release it with reserve_paper.sh release <paper-dir>" >&2
fi

echo "published: $CDEST"
echo "  $(basename "$PDF")  (hash-verified)"
[ -z "$DOCX" ] || echo "  $(basename "$DOCX")  (optional editable artifact, manifest-bound)"
echo "published: $DEST"
echo "  $(basename "$CLAIMS")  (hash-verified)"
echo "  run.json  (hash-verified, internal — safe_to_publish=false)"
echo "  comment.json  (hash-verified, internal — safe_to_publish=false)"
echo "  finalization_receipt.json  (hash-verified registry and build bindings)"
echo "  document_build_manifest.json  (v3 exact page/document binding)"
echo "  release-manifest.json  (exact file list, hashes, and document binding across both folders)"
