#!/bin/bash
# run_verification.sh <audit-dir>
# Render the verification notebook and bind its result to the exact report, comment and package.
set -euo pipefail

if [ "${1:-}" = "--self-test" ]; then
  T="$(mktemp -d "${TMPDIR:-/tmp}/i4r-verification.XXXXXX")"
  trap 'rm -rf "$T"' EXIT
  mkdir -p "$T/report"
  printf '## 1. Finding [claim 1]\n' > "$T/report/REPORT_final.md"
  printf '{"findings":[{"id":"F1"}]}\n' > "$T/comment.json"
  printf '%s\n' '---' 'title: test' '---' > "$T/verification.Rmd"
  printf '{"package_sha256":"abc"}\n' > "$T/package_manifest.json"
  mkdir -p "$T/bin"
  printf '%s\n' '#!/bin/sh' \
    'printf '\''claim,key,verdict\nC-1,x,PASS\n'\'' > verification_summary.csv' \
    'printf '\''0\n'\'' > verification_status.txt' 'exit 0' > "$T/bin/Rscript"
  chmod +x "$T/bin/Rscript"
  PATH="$T/bin:$PATH" "$0" "$T" >/dev/null
  python3 - "$T/verification_manifest.json" <<'PY'
import json, sys
d=json.load(open(sys.argv[1])); assert d["claim_ids"] == ["C-1"]; assert d["status"] == 0
assert d["status_artifact"]["path"] == "verification_status.txt"
PY
  printf '#!/bin/sh\nexit 7\n' > "$T/bin/Rscript"; chmod +x "$T/bin/Rscript"
  printf '0\n' > "$T/verification_status.txt"
  printf 'claim,key,verdict\nC-1,stale,PASS\n' > "$T/verification_summary.csv"
  set +e
  PATH="$T/bin:$PATH" "$0" "$T" >/dev/null 2>&1
  rc=$?
  set -e
  [ "$rc" -eq 7 ] || { echo "stale-output negative control returned $rc, expected 7" >&2; exit 1; }
  [ ! -e "$T/verification_manifest.json" ] || { echo "failed render left a verification manifest" >&2; exit 1; }
  [ "$(tr -d '[:space:]' < "$T/verification_status.txt")" = "7" ] || {
    echo "failed render did not replace the stale passing status" >&2; exit 1; }
  [ ! -e "$T/verification_summary.csv" ] || { echo "failed render retained a stale summary" >&2; exit 1; }
  echo "run_verification self-test: ok"
  exit 0
fi

DIR="${1:?usage: run_verification.sh <audit-dir>}"
[ $# -eq 1 ] || { echo "usage: run_verification.sh <audit-dir>" >&2; exit 2; }
[ -d "$DIR" ] || { echo "audit directory not found: $DIR" >&2; exit 2; }

find_one () {
  for value in "$@"; do [ -f "$value" ] && { printf '%s\n' "$value"; return 0; }; done
  return 1
}

NB=$(find_one "$DIR/verification.Rmd" "$DIR/report/verification.Rmd" "$DIR/verification/verification.Rmd") || {
  echo "verification.Rmd not found" >&2; exit 2; }
REPORT=$(find_one "$DIR/report/REPORT_final.md" "$DIR/REPORT_final.md") || {
  echo "REPORT_final.md not found" >&2; exit 2; }
COMMENT="$DIR/comment.json"; [ -f "$COMMENT" ] || { echo "comment.json not found" >&2; exit 2; }
VDIR=$(cd "$(dirname "$NB")" && pwd)
STATUS="$VDIR/verification_status.txt"; SUMMARY="$VDIR/verification_summary.csv"
MANIFEST="$DIR/verification_manifest.json"
RENDER_TX="verification-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
RENDER_STARTED=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
RENDER_RC=0

command -v Rscript >/dev/null || { echo "Rscript is required" >&2; exit 2; }
# A render is a fresh transaction. A failed process must not reuse an older PASS
# status, summary, or manifest from this directory.
rm -f "$STATUS" "$SUMMARY" "$MANIFEST"
set +e
(cd "$VDIR" && Rscript --no-init-file -e 'rmarkdown::render(basename(commandArgs(TRUE)[1]), quiet=TRUE)' "$NB")
RENDER_RC=$?
set -e
if [ "$RENDER_RC" -ne 0 ]; then
  printf '%s\n' "$RENDER_RC" > "$STATUS"
  rm -f "$SUMMARY" "$MANIFEST"
  echo "verification notebook render failed with exit $RENDER_RC" >&2
  exit "$RENDER_RC"
fi
RENDER_ENDED=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

[ -s "$STATUS" ] || { echo "verification_status.txt missing or empty" >&2; exit 1; }
[ -s "$SUMMARY" ] || { echo "verification_summary.csv missing or empty" >&2; exit 1; }
CODE=$(tr -d '[:space:]' < "$STATUS")
case "$CODE" in ''|*[!0-9]*) echo "verification status is not an integer: $CODE" >&2; exit 1 ;; esac
if [ "$CODE" != "0" ]; then
  rm -f "$MANIFEST"
  echo "verification failed with status $CODE; failing rows of $SUMMARY:" >&2
  python3 - "$SUMMARY" <<'PYFAIL' >&2 || true
import csv, sys
for row in csv.DictReader(open(sys.argv[1], encoding="utf-8")):
    if row.get("verdict") == "FAIL":
        print(f"  {row.get('claim')} {row.get('key')}: {row.get('detail')}")
PYFAIL
  exit "$CODE"
fi

RENDER_TX="$RENDER_TX" RENDER_STARTED="$RENDER_STARTED" RENDER_ENDED="$RENDER_ENDED" \
python3 - "$DIR" "$NB" "$REPORT" "$COMMENT" "$STATUS" "$SUMMARY" <<'PY'
import csv, datetime as dt, hashlib, json, os, pathlib, re, sys, tempfile
root, nb, report, comment, status, summary = map(pathlib.Path, sys.argv[1:])
def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024*1024), b""): h.update(block)
    return h.hexdigest()
with summary.open(newline="", errors="replace") as fh: rows=list(csv.DictReader(fh))
claim_key = "claim" if rows and "claim" in rows[0] else "claim_id" if rows and "claim_id" in rows[0] else None
if not claim_key: raise SystemExit("verification summary has no claim column")
claims=[]
seen=set()
pairs=set()
allowed={"PASS", "FAIL", "JUDGEMENT", "UNVERIFIABLE"}
for index, row in enumerate(rows, 2):
    claim=str(row.get(claim_key) or "").strip().upper()
    if not re.fullmatch(r"C-[1-9][0-9]*", claim):
        raise SystemExit(f"verification summary row {index} has an invalid claim ID: {claim!r}")
    verdict=str(row.get("verdict") or "").strip().upper()
    if verdict not in allowed:
        raise SystemExit(f"verification summary row {index} has an invalid verdict: {verdict!r}")
    key=str(row.get("key") or "").strip()
    if not key:
        raise SystemExit(f"verification summary row {index} has an empty check key")
    pair=(claim,key)
    if pair in pairs:
        raise SystemExit(f"verification summary repeats claim/key pair {claim}/{key}")
    pairs.add(pair)
    if claim not in seen:
        claims.append(claim); seen.add(claim)
if not claims: raise SystemExit("verification summary contains no claims")
numbers=[int(claim[2:]) for claim in claims]
# unique and increasing: a finding whose approval is deferred leaves a hole (finalize refuses the stop meanwhile)
if numbers != sorted(set(numbers)):
    raise SystemExit(f"verification summary claim groups must be unique C-<n> in increasing order; observed {claims}")
report_claims=[f"C-{int(n)}" for n in re.findall(r"\[claim\s+([1-9][0-9]*)\]", report.read_text(errors="replace"), re.I)]
comment_obj=json.loads(comment.read_text())
comment_claims=[]
for index,finding in enumerate(comment_obj.get("findings") or [],1):
    match=re.fullmatch(r"F([1-9][0-9]*)",str(finding.get("id") or ""),re.I)
    if not match: raise SystemExit(f"comment finding {index} has an invalid stable ID")
    comment_claims.append(f"C-{int(match.group(1))}")
if report_claims != comment_claims or claims != comment_claims:
    raise SystemExit("report, comment, and verification-summary claim groups do not join in exact canonical order")
pkg=root/"package_manifest.json"
payload={"schema_version":"2.0.0",
         "recorded_at":dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "status":int(status.read_text().strip()), "claim_ids":claims,
         "status_artifact":{"path":os.path.relpath(status,root),"sha256":digest(status)},
         "render":{"transaction_id":os.environ["RENDER_TX"],
                   "started_at":os.environ["RENDER_STARTED"],
                   "ended_at":os.environ["RENDER_ENDED"], "exit_status":0, "mode":"executed"},
         "notebook":{"path":os.path.relpath(nb,root),"sha256":digest(nb)},
         "report":{"path":os.path.relpath(report,root),"sha256":digest(report)},
         "comment":{"path":os.path.relpath(comment,root),"sha256":digest(comment)},
         "summary":{"path":os.path.relpath(summary,root),"sha256":digest(summary)},
         "package_manifest_sha256":digest(pkg) if pkg.is_file() else None}
out=root/"verification_manifest.json"
fd,tmp=tempfile.mkstemp(prefix=out.name+".",dir=out.parent)
with os.fdopen(fd,"w",encoding="utf-8") as fh:
    json.dump(payload,fh,indent=2); fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
os.replace(tmp,out)
print(f"verification manifest: {out} ({len(claims)} claims, status {payload['status']})")
PY

echo "verification passed and is content-bound"
