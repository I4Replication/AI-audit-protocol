#!/bin/bash
# check_claims_script.sh [--release] <audit-dir> [claims-script]
#
# Runs the audit's claims script the way a replicator does: Rscript from an empty folder, with I4R_PKG_DIR set to
# <audit-dir>/pkg and I4R_AUDIT_DIR set to a scratch folder holding a copy of the article (paper/) and nothing else,
# since a replicator has the deposit and the article but none of the audit's working files. R runs in a UTF-8
# locale and reads the user's .Renviron, as the verification notebook's run does (run_verification.sh). The notebook
# runs every check inside knitr, from the audit folder, with its own setup; this proves the published file also runs on
# its own (2.7.10: about half of the claims scripts published before then stopped with an error when run this way).
# Passes when the script exits 0, prints its summary line with 0 FAIL, and reports the deposit unmodified, or when it
# has no code-backed claim at all. A claims file in another language (.do, .sps) is reported and passes: only an R
# claims file can be run here. I4R_CLAIMS_TIMEOUT sets the limit in seconds (default 1800). On a failure the run's
# output is kept in <audit-dir>/work/claims_check.log, or in I4R_CLAIMS_LOG_DIR (none: not kept; a tool that checks a
# published audit without changing it sets this).
# --release (stage_release.sh and publish_completed.sh, 2.7.11): a claims script this audit already published, built
# before 2.7.10, is re-published as it was sent. It is recognised when the audit was initialised before 2.7.10
# (run.json protocol_version), its journal records publish's filing, the script has no 2.7.10 harness (no 'deposit
# unmodified' line) and the finalization receipt binds these bytes. It passes on exit 0 with 0 FAIL, as before
# 2.7.10; otherwise it is named in a WARNING and the release goes on. Any other script is checked as above.
#
# Exit: 0 pass, 1 the script failed a check or stopped, 2 usage or setup (no R, no pkg/, no claims script).
set -uo pipefail
RELEASE=0
case "${1:-}" in
  -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  --release) RELEASE=1; shift ;;
esac
case "${1:-}" in
  "") echo "usage: check_claims_script.sh [--release] <audit-dir> [claims-script]" >&2; exit 2 ;;
esac
A="$(cd "$1" 2>/dev/null && pwd)" || { echo "check_claims_script: no such audit directory: $1" >&2; exit 2; }
if [ -n "${2:-}" ]; then CS="$2"; else
  set -- "$A"/claims_script.*
  [ "$#" -eq 1 ] && [ -f "$1" ] || { echo "check_claims_script: expected exactly one claims_script.* in $A" >&2; exit 2; }
  CS="$1"
fi
[ -f "$CS" ] || { echo "check_claims_script: no claims script: $CS" >&2; exit 2; }
case "$CS" in
  *.R) ;;
  *) echo "claims script $(basename "$CS") is not R; it cannot be run here (checked by its own tool)"; exit 0 ;;
esac
CS="$(cd "$(dirname "$CS")" && pwd)/$(basename "$CS")"
# 2.7.11: is this the pre-2.7.10 script the audit already published? (published claims scripts stay as they were sent)
legacy_published () {   # prints the protocol version of an audit whose claims script is its published pre-2.7.10 one
  python3 - "$A" "$CS" <<'PYLEGACY'
import hashlib, json, pathlib, re, sys
audit, script = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
try:
    relative = script.relative_to(audit).as_posix()
    run = json.loads((audit / "run.json").read_text(encoding="utf-8"))
    receipt = json.loads((audit / "finalization_receipt.json").read_text(encoding="utf-8"))
    journal = (audit / "run.events.jsonl").read_text(encoding="utf-8").splitlines()
    text = script.read_bytes()
except (OSError, ValueError):
    sys.exit(0)
version = str(run.get("protocol_version") or "")
number = re.match(r"(\d+)\.(\d+)\.(\d+)", version)      # 2.7.10, 2.7.10-dev; unversioned copies are older
if number and tuple(map(int, number.groups())) >= (2, 7, 10):
    sys.exit(0)                                   # initialised on 2.7.10 or later: always checked in full
filed = False                                     # publish's own filing, as finalize_run.sh reads it
for line in journal:
    try:
        event = json.loads(line)
    except ValueError:
        continue
    payload = event.get("payload") if isinstance(event, dict) else None
    if (isinstance(payload, dict) and event.get("type") == "delivery" and payload.get("release_dir")
            and payload.get("recipient") == "comments/ and metadata/ (filed publication set)"):
        filed = True
if not filed or b"deposit unmodified" in text:
    sys.exit(0)                                   # never published, or rebuilt with the 2.7.10 harness
rows = [row for row in receipt.get("artifacts") or [] if isinstance(row, dict) and row.get("path") == relative]
if len(rows) != 1 or rows[0].get("sha256") != hashlib.sha256(text).hexdigest():
    sys.exit(0)                                   # not the bytes the audit finalized
print(version or "an unversioned skill", end="")
PYLEGACY
}
LEGACY=""
[ "$RELEASE" -eq 1 ] && LEGACY="$(legacy_published 2>/dev/null)"
legacy_warning () {   # <why> : a published pre-2.7.10 script never stops its audit's re-publication
  echo "WARNING: $(basename "$CS") was built before 2.7.10 (the audit was initialised on $LEGACY) and is already published;"
  echo "  it is re-published as it was sent, although $1."
  echo "  Published claims scripts stay as they were sent; amend_claims_script.py replaces one only where that is decided."
}
if [ ! -d "$A/pkg" ]; then
  [ -n "$LEGACY" ] && { legacy_warning "it could not be run here (the audit has no pkg/)"; exit 0; }
  echo "check_claims_script: $A has no pkg/ to run the claims script against" >&2; exit 2
fi
if ! command -v Rscript >/dev/null; then
  [ -n "$LEGACY" ] && { legacy_warning "it could not be run here (no Rscript on this host)"; exit 0; }
  echo "check_claims_script: Rscript is required (the claims script is R)" >&2; exit 2
fi
W="$(mktemp -d "${TMPDIR:-/tmp}/i4r-claims-check.XXXXXX")"; trap 'rm -rf "$W"' EXIT
mkdir -p "$W/cwd" "$W/replicator"
[ -d "$A/paper" ] && cp -R "$A/paper" "$W/replicator/paper"
LIMIT="${I4R_CLAIMS_TIMEOUT:-1800}"
# the measurement and a replicator's R run in a UTF-8 locale; a C/POSIX caller must not change the verdict
UTF8_LOCALE="$(locale -a 2>/dev/null | grep -i -m1 -E '^(C|en_US)\.utf-?8$')"
(cd "$W/cwd" && env -u I4R_SNIPS_DIR ${UTF8_LOCALE:+LC_ALL="$UTF8_LOCALE" LANG="$UTF8_LOCALE"} \
   I4R_PKG_DIR="$A/pkg" I4R_AUDIT_DIR="$W/replicator" \
   perl -e 'alarm shift; exec @ARGV' "$LIMIT" Rscript --no-init-file "$CS") > "$W/run.log" 2>&1
rc=$?
summary="$(grep -E '^[0-9]+ checks, [0-9]+ PASS, [0-9]+ FAIL$' "$W/run.log" | tail -1)"
deposit="$(grep -E '^deposit unmodified: ' "$W/run.log" | tail -1)"
fails="$(printf '%s' "$summary" | sed -nE 's/.* ([0-9]+) FAIL$/\1/p')"
if [ "$rc" -eq 0 ] && [ -n "$summary" ] && [ "${fails:-1}" -eq 0 ] && [ "$deposit" = "deposit unmodified: yes" ]; then
  echo "claims script runs on its own: $summary"
  exit 0
fi
if [ "$rc" -eq 0 ] && [ -z "$summary" ] && ! grep -q 'claims_summary(' "$CS"; then
  echo "claims script runs on its own: it has no code-backed claim (every claim is checked by inspection)"
  exit 0
fi
if [ -n "$LEGACY" ] && [ "$rc" -eq 0 ] && [ -n "$summary" ] && [ "${fails:-1}" -eq 0 ]; then
  echo "claims script runs on its own: $summary (built before 2.7.10 and already published: re-published as it"
  echo "  was sent; its harness does not report on the deposit)"
  exit 0
fi
LOG_DIR="${I4R_CLAIMS_LOG_DIR:-$A/work}"
if [ "$LOG_DIR" != "none" ]; then
  mkdir -p "$LOG_DIR" 2>/dev/null && cp "$W/run.log" "$LOG_DIR/claims_check.log" 2>/dev/null && LOGNOTE="$LOG_DIR/claims_check.log"
fi
if [ -n "$LEGACY" ]; then
  legacy_warning "it does not run cleanly on its own here (Rscript exit $rc${summary:+; $summary})"
else
  echo "FAIL: $(basename "$CS") does not run cleanly on its own (Rscript exit $rc${summary:+; $summary}${deposit:+; $deposit})"
fi
[ "$rc" -eq 142 ] && echo "  it ran past ${LIMIT} s (I4R_CLAIMS_TIMEOUT)"
if [ -z "$LEGACY" ] && [ -n "$summary" ] && [ -z "$deposit" ]; then
  echo "  it was built before 2.7.10 (no 'deposit unmodified' line): rebuild it with"
  echo "      python3 scripts/generate_finding_artifacts.py \"\$AUDIT_DIR\" --apply"
fi
grep -A1 -E '^Error' "$W/run.log" | head -6 | sed 's/^/  /'
grep -E '\] (OBSERVED .* FAIL|FAIL  )|^claims that|^the shared helpers|^outcomes recorded|^These R packages|^Some checks read|^Outside program|^outside programs' \
  "$W/run.log" | head -25 | sed 's/^/  /'
echo "  A check may read the deposit and the article (paper/), never the audit's own working files."
[ -n "${LOGNOTE:-}" ] && echo "  The whole run's output: $LOGNOTE"
[ -n "$LEGACY" ] && exit 0   # 2.7.11: a published pre-2.7.10 script is re-published as it was sent (warned above)
echo "  To run it as this check does:"
echo "      R=\$(mktemp -d); cp -R \"$A/paper\" \"\$R/\" 2>/dev/null; cd \"\$(mktemp -d)\" && \\"
echo "      LC_ALL=${UTF8_LOCALE:-en_US.UTF-8} I4R_PKG_DIR=\"$A/pkg\" I4R_AUDIT_DIR=\"\$R\" Rscript --no-init-file \"$CS\""
exit 1
