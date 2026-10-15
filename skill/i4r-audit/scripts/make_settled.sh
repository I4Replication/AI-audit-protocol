#!/bin/bash
# make_settled.sh <audit-dir> [findings.jsonl]
# Promote only independently verified findings to the round-2 cache. A hash proves identity,
# not correctness, so no artefact is admitted merely because it exists in work/.
set -euo pipefail

if [ "${1:-}" = "--self-test" ]; then
  T="$(mktemp -d "${TMPDIR:-/tmp}/i4r-settled.XXXXXX")"
  trap 'rm -rf "$T"' EXIT
  mkdir -p "$T/work" "$T/report" "$T/critique/round_1"
  printf 'source\n' > "$T/work/source.csv"
  printf '# Current report\n' > "$T/report/REPORT_final.md"
  printf '{"finding_id":"F-ok","verdict":"confirmed"}\n' > "$T/work/reviewer-a.json"
  printf '{"finding_id":"F-ok","verdict":"confirmed independently"}\n' > "$T/work/reviewer-b.json"
  printf '{"finding_id":"F-same-family","verdict":"confirmed"}\n' > "$T/work/reviewer-c.json"
  printf '{"finding_id":"F-same-family","verdict":"confirmed independently"}\n' > "$T/work/reviewer-d.json"
  H="$(shasum -a 256 "$T/work/source.csv" | awk '{print $1}')"
  RH="$(shasum -a 256 "$T/report/REPORT_final.md" | awk '{print $1}')"
  HA="$(shasum -a 256 "$T/work/reviewer-a.json" | awk '{print $1}')"
  HB="$(shasum -a 256 "$T/work/reviewer-b.json" | awk '{print $1}')"
  HC="$(shasum -a 256 "$T/work/reviewer-c.json" | awk '{print $1}')"
  HD="$(shasum -a 256 "$T/work/reviewer-d.json" | awk '{print $1}')"
  cat > "$T/run.json" <<'EOF'
{"schema_version":"2.1.0","normalization":null,"audit_id":"settled-self-test","rounds":[{"round":1,"started_at":"2026-08-27T10:00:00Z","ended_at":"2026-08-27T11:00:00Z","integrated_at":"2026-08-27T12:00:00Z","consistency_check_run":true,"consistency_check_exit":0}]}
EOF
  cat > "$T/critique/round_1/ROUND_STATUS.json" <<'EOF'
{"audit_id":"settled-self-test","round":1,"outcome":"complete","exit_code":0,"quorum_met":true,"expected_roles":["stats","data","fairness","completeness"],"completed_roles":["stats","data","fairness","completeness"],"failed_roles":[]}
EOF
  python3 - "$T" <<'PYINV'
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1])
rows=[]
specs=[
 ("inv-a","auditor_reproduction","auditor",0,"anthropic","claude","work/reviewer-a.json"),
 ("inv-b","critic_stats","critic",1,"openai","gpt","work/reviewer-b.json"),
 ("inv-c","auditor_data_integrity","auditor",0,"anthropic","claude","work/reviewer-c.json"),
 ("inv-d","auditor_code_methods","auditor",0,"anthropic","claude","work/reviewer-d.json"),
]
for index,(inv,role,role_class,round_no,provider,family,relative) in enumerate(specs,1):
    output=root/relative
    rows.append({"invocation_id":inv,"role":role,"role_class":role_class,"round":round_no,
      "attempt":1,"status":"completed","provider":provider,"provider_call":True,
      "cache_usage":"none","session_id":f"session-{index}","requested_model":"model-alias",
      "observed_model":f"served-{family}-{index}","model_family":family,"model_source":"observed",
      "reasoning_effort":"high" if role_class=="critic" else "max",
      "started_at":"2026-08-27T10:00:00Z",
      "ended_at":"2026-08-27T10:01:00Z","elapsed_seconds":60,
      "input_artifact_sha256":"a"*64,"prompt_sha256":"b"*64,
      "output_artifact":relative,"output_sha256":hashlib.sha256(output.read_bytes()).hexdigest(),
      "usage":{"obtainable":True,"input_tokens":10,"output_tokens":5,"cached_input_tokens":0,
               "reasoning_tokens":1,"total_tokens":16,"source":"api_response"},
      "error":None,"error_class":None,"legacy_source":None})
    if inv == "inv-b":
      rows[-1].update({"observed_model":None,"served_model":None,
        "model_source":"declared","identity_assurance":"requested_only",
        "attempt_kind":"role_inference","provider_request_status":"accepted",
        "inference_start_status":"started","source_invocation_id":None,
        "turn_count":1,"provider_cost":None,"model_identity_evidence":None,
        "usage_by_model":[],"unattributed_usage":rows[-1]["usage"],
        "provider_duration_seconds":60})
(root/"invocations.jsonl").write_text("".join(json.dumps(row,sort_keys=True)+"\n" for row in rows))
PYINV
  cat > "$T/work/findings.jsonl" <<EOF
{"id":"F-ok","claim":"N is 10","status":"confirmed","coordinator_approved":true,"ledger_disposition":{"status":"INCLUDED","target":null,"reason":null},"evidence":{"source_hashes":[{"path":"work/source.csv","sha256":"$H"}],"rerun_command":"wc -l work/source.csv","expected":"1 line","observed":"1 line"},"verifications":[{"reviewer_id":"auditor_reproduction","invocation_id":"inv-a","evidence_record":{"path":"work/reviewer-a.json","sha256":"$HA"},"model_family":"claude","outcome":"confirmed"},{"reviewer_id":"critic_stats","invocation_id":"inv-b","evidence_record":{"path":"work/reviewer-b.json","sha256":"$HB"},"model_family":"gpt","outcome":"confirmed"}],"dependencies":{"environment_fingerprint":"env-1"},"settled":{"approved":true,"valid_for_report_hash":"$RH"}}
{"id":"F-one","claim":"not independently confirmed","status":"confirmed","coordinator_approved":true,"ledger_disposition":{"status":"INCLUDED","target":null,"reason":null},"evidence":{"source_hashes":[{"path":"work/source.csv","sha256":"$H"}],"rerun_command":"wc -l work/source.csv"},"verifications":[{"reviewer_id":"auditor_reproduction","invocation_id":"inv-a","evidence_record":{"path":"work/reviewer-a.json","sha256":"$HA"},"model_family":"claude","outcome":"confirmed"}]}
{"id":"F-noapproval","claim":"not approved","status":"confirmed","coordinator_approved":false,"ledger_disposition":{"status":"EXCLUDED","target":null,"reason":"coordinator rejected"},"evidence":{"source_hashes":[{"path":"work/source.csv","sha256":"$H"}],"rerun_command":"wc -l work/source.csv"},"verifications":[{"reviewer_id":"auditor_reproduction","invocation_id":"inv-a","evidence_record":{"path":"work/reviewer-a.json","sha256":"$HA"},"model_family":"claude","outcome":"confirmed"},{"reviewer_id":"critic_stats","invocation_id":"inv-b","evidence_record":{"path":"work/reviewer-b.json","sha256":"$HB"},"model_family":"gpt","outcome":"confirmed"}]}
{"id":"F-same-family","claim":"not independently model-confirmed","status":"confirmed","coordinator_approved":true,"ledger_disposition":{"status":"INCLUDED","target":null,"reason":null},"evidence":{"source_hashes":[{"path":"work/source.csv","sha256":"$H"}],"rerun_command":"wc -l work/source.csv"},"verifications":[{"reviewer_id":"auditor_data_integrity","invocation_id":"inv-c","evidence_record":{"path":"work/reviewer-c.json","sha256":"$HC"},"model_family":"claude","outcome":"confirmed"},{"reviewer_id":"auditor_code_methods","invocation_id":"inv-d","evidence_record":{"path":"work/reviewer-d.json","sha256":"$HD"},"model_family":"claude","outcome":"confirmed"}],"dependencies":{"environment_fingerprint":"env-1"},"settled":{"approved":true,"valid_for_report_hash":"$RH"}}
EOF
  I4R_SKIP_FINDING_SCHEMA=1 "$0" "$T" >/dev/null
  python3 - "$T/SETTLED.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert [x["id"] for x in d["facts"]] == ["F-ok"], d
assert d["invalidated"] is False
assert len(d["source_registry_sha256"]) == 64
assert len(d["report_sha256"]) == 64
assert len(d["invocation_ledger"]["bound_prefix_sha256"]) == 64
assert d["invocation_ledger"]["records_at_generation"] == 4
assert d["round_1_gate"]["consistency_check_exit"] == 0
assert d["facts"][0]["coordinator_approved"] is True
assert d["facts"][0]["valid_for_report_hash"] == d["report_sha256"]
assert [x["invocation_id"] for x in d["facts"][0]["confirmations"]] == ["inv-a", "inv-b"]
rejected = {x["id"]: x["reasons"] for x in d["rejected"]}
assert "confirmations do not contain two distinct model families" in rejected["F-same-family"]
PY
  cp "$T/work/findings.jsonl" "$T/work/findings.good.jsonl"
  python3 - "$T/work/findings.jsonl" <<'PY'
import json,sys
p=sys.argv[1]; rows=[json.loads(line) for line in open(p) if line.strip()]
rows[0]["verifications"][0]["reviewer_id"]="invented-reviewer-label"
open(p,"w").write("".join(json.dumps(row)+"\n" for row in rows))
PY
  if I4R_SKIP_FINDING_SCHEMA=1 "$0" "$T" >/dev/null 2>&1; then
    echo "FAIL: reviewer label not bound to the invocation role was accepted" >&2
    exit 1
  fi
  mv "$T/work/findings.good.jsonl" "$T/work/findings.jsonl"
  cp "$T/run.json" "$T/run.good.json"
  python3 - "$T/run.json" <<'PY'
import json,sys
p=sys.argv[1]; value=json.load(open(p)); value["rounds"][0]["consistency_check_exit"]=1
open(p,"w").write(json.dumps(value)+"\n")
PY
  if I4R_SKIP_FINDING_SCHEMA=1 "$0" "$T" >/dev/null 2>&1; then
    echo "FAIL: failed round-1 consistency gate was accepted" >&2
    exit 1
  fi
  mv "$T/run.good.json" "$T/run.json"
  # A claim reworded at the latest integration is not settled on confirmations of the old wording.
  python3 - "$T/work/findings.jsonl" "$T/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl" <<'PY'
import json,sys
rows=[json.loads(line) for line in open(sys.argv[1]) if line.strip()]
rows[0]["claim"]="N was 12 before the correction"
open(sys.argv[2],"w").write("".join(json.dumps(row)+"\n" for row in rows))
PY
  I4R_SKIP_FINDING_SCHEMA=1 "$0" "$T" >/dev/null
  python3 - "$T/SETTLED.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["facts"] == [], d["facts"]
assert any("revised at round 1 integration" in r for r in {x["id"]: x["reasons"] for x in d["rejected"]}["F-ok"])
PY
  # ...and a later integration that confirms nothing new does not lift the hold.
  cp "$T/run.json" "$T/run.good.json"
  mkdir -p "$T/critique/round_2"
  cp "$T/work/findings.jsonl" "$T/critique/round_2/FINDINGS_BEFORE_INTEGRATION.jsonl"
  python3 - "$T/run.json" <<'PY'
import json,sys
p=sys.argv[1]; value=json.load(open(p))
value["rounds"].append({"round":2,"state":"integrated","started_at":"2026-08-27T13:00:00Z",
                        "ended_at":"2026-08-27T14:00:00Z","integrated_at":"2026-08-27T15:00:00Z"})
open(p,"w").write(json.dumps(value)+"\n")
PY
  I4R_SKIP_FINDING_SCHEMA=1 "$0" "$T" >/dev/null 2>&1
  python3 - "$T/SETTLED.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["facts"] == [], d["facts"]
PY
  mv "$T/run.good.json" "$T/run.json"
  rm -r "$T/critique/round_2"
  rm "$T/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl"
  # A merge that adopts another finding's wording is a rewording: the snapshot's "N is 10" belonged to
  # F-one, which still holds it, so F-ok's critics never reviewed that text for F-ok.
  cp "$T/work/findings.jsonl" "$T/work/findings.good.jsonl"
  python3 - "$T/work/findings.jsonl" "$T/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl" <<'PY'
import json,sys
rows=[json.loads(line) for line in open(sys.argv[1]) if line.strip()]
by={r["id"]:r for r in rows}
by["F-one"]["claim"]=by["F-ok"]["claim"]
open(sys.argv[1],"w").write("".join(json.dumps(r)+"\n" for r in rows))
snap=[dict(r) for r in rows]
for r in snap:
    if r["id"]=="F-ok": r["claim"]="N was 9"
open(sys.argv[2],"w").write("".join(json.dumps(r)+"\n" for r in snap))
PY
  I4R_SKIP_FINDING_SCHEMA=1 "$0" "$T" >/dev/null 2>&1
  python3 - "$T/SETTLED.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["facts"] == [], d["facts"]
PY
  mv "$T/work/findings.good.jsonl" "$T/work/findings.jsonl"
  rm "$T/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl"
  # An integrated latest round whose snapshot was never copied: a change since round 1's snapshot is
  # attributed to round 2 (it has an INTEGRATION_SUMMARY), so no round-2 record can settle it.
  cp "$T/run.json" "$T/run.good.json"
  mkdir -p "$T/critique/round_2"
  echo '{"round": 2}' > "$T/critique/round_2/INTEGRATION_SUMMARY.json"
  python3 - "$T/work/findings.jsonl" "$T/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl" "$T/run.json" <<'PY'
import json,sys
rows=[json.loads(line) for line in open(sys.argv[1]) if line.strip()]
snap=[dict(r) for r in rows]
snap[0]["claim"]="N was 9"
open(sys.argv[2],"w").write("".join(json.dumps(r)+"\n" for r in snap))
value=json.load(open(sys.argv[3]))
value["rounds"].append({"round":2,"state":"integrated","started_at":"2026-08-27T13:00:00Z",
                        "ended_at":"2026-08-27T14:00:00Z","integrated_at":"2026-08-27T15:00:00Z"})
open(sys.argv[3],"w").write(json.dumps(value)+"\n")
PY
  I4R_SKIP_FINDING_SCHEMA=1 "$0" "$T" 2>"$T/warn.txt" >/dev/null
  grep -q "round 2 was integrated but has no FINDINGS_BEFORE_INTEGRATION" "$T/warn.txt"
  python3 - "$T/SETTLED.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["facts"] == [], d["facts"]
assert any("revised at round 2 integration" in r for r in {x["id"]: x["reasons"] for x in d["rejected"]}["F-ok"])
PY
  mv "$T/run.good.json" "$T/run.json"
  rm -r "$T/critique/round_2"
  rm "$T/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl"
  # Exact mode (integrate_round's backup, no legacy renumber): a wording swapped between two findings
  # is a rewording, while a renumber recorded in the history is not.
  cp "$T/work/findings.jsonl" "$T/work/findings.good.jsonl"
  python3 - "$T/work/findings.jsonl" "$T/work/findings.before_round1_integration.jsonl" <<'PY'
import json,sys
rows=[json.loads(line) for line in open(sys.argv[1]) if line.strip()]
by={r["id"]:r for r in rows}
snap=[dict(r) for r in rows]
sb={r["id"]:r for r in snap}
sb["F-ok"]["claim"], sb["F-one"]["claim"] = by["F-one"]["claim"], by["F-ok"]["claim"]
open(sys.argv[2],"w").write("".join(json.dumps(r)+"\n" for r in snap))
PY
  I4R_SKIP_FINDING_SCHEMA=1 "$0" "$T" >/dev/null 2>&1
  python3 - "$T/SETTLED.json" <<'PY'
import json,sys
assert json.load(open(sys.argv[1]))["facts"] == []
PY
  python3 - "$T/work/findings.jsonl" "$T/work/findings.before_round1_integration.jsonl" "$T/work/id_map_history.jsonl" <<'PY'
import json,sys
rows=[json.loads(line) for line in open(sys.argv[1]) if line.strip()]
snap=[dict(r) for r in rows]
snap[0]["id"]="F-moved"
open(sys.argv[2],"w").write("".join(json.dumps(r)+"\n" for r in snap))
open(sys.argv[3],"w").write(json.dumps({"map":{"F-moved":rows[0]["id"]},"rounds_run_before_renumber":["round_1"]})+"\n")
PY
  I4R_SKIP_FINDING_SCHEMA=1 "$0" "$T" >/dev/null
  python3 - "$T/SETTLED.json" <<'PY'
import json,sys
assert [x["id"] for x in json.load(open(sys.argv[1]))["facts"]] == ["F-ok"]
PY
  rm "$T/work/findings.before_round1_integration.jsonl" "$T/work/id_map_history.jsonl"
  mv "$T/work/findings.good.jsonl" "$T/work/findings.jsonl"
  # A renumber moves ids without rewording anything: a snapshot naming F-ok by another id but with
  # the same claim text must not hold it.
  python3 - "$T/work/findings.jsonl" "$T/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl" <<'PY'
import json,sys
rows=[json.loads(line) for line in open(sys.argv[1]) if line.strip()]
rows[0]["id"]="F-moved"
open(sys.argv[2],"w").write("".join(json.dumps(row)+"\n" for row in rows))
PY
  I4R_SKIP_FINDING_SCHEMA=1 "$0" "$T" >/dev/null
  python3 - "$T/SETTLED.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert [x["id"] for x in d["facts"]] == ["F-ok"], d["facts"]
PY
  rm "$T/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl"
  printf 'tampered reviewer output\n' > "$T/work/reviewer-a.json"
  if I4R_SKIP_FINDING_SCHEMA=1 "$0" "$T" >/dev/null 2>&1; then
    echo "FAIL: changed reviewer evidence record was accepted" >&2
    exit 1
  fi
  printf '{"finding_id":"F-ok","verdict":"confirmed"}\n' > "$T/work/reviewer-a.json"
  printf 'changed\n' > "$T/work/source.csv"
  if I4R_SKIP_FINDING_SCHEMA=1 "$0" "$T" >/dev/null 2>&1; then
    echo "FAIL: changed evidence hash was accepted" >&2
    exit 1
  fi
  echo "make_settled self-test: ok"
  exit 0
fi

WS="${1:?usage: make_settled.sh <audit-dir> [findings.jsonl]}"
REG="${2:-$WS/work/findings.jsonl}"
REPORT="$WS/report/REPORT_final.md"
OUT_JSON="$WS/SETTLED.json"
OUT_MD="$WS/SETTLED.md"
HERE="$(cd "$(dirname "$0")" && pwd)"
SCHEMA="${I4R_FINDING_SCHEMA:-$HERE/../schemas/v2/finding-row.schema.json}"
INVOCATION_SCHEMA="${I4R_INVOCATION_SCHEMA:-$HERE/../schemas/v2/invocation-record.schema.json}"
[ -d "$WS" ] || { echo "FATAL: audit directory not found: $WS" >&2; exit 2; }
[ -f "$REG" ] || { echo "FATAL: findings registry not found: $REG" >&2; exit 2; }
[ -f "$REPORT" ] || { echo "FATAL: canonical report not found: $REPORT" >&2; exit 2; }
[ "${I4R_SKIP_FINDING_SCHEMA:-0}" = "1" ] || [ -f "$SCHEMA" ] || {
  echo "FATAL: finding row schema not found: $SCHEMA" >&2; exit 2; }
[ -f "$INVOCATION_SCHEMA" ] || {
  echo "FATAL: invocation record schema not found: $INVOCATION_SCHEMA" >&2; exit 2; }

python3 - "$WS" "$REG" "$REPORT" "$OUT_JSON" "$OUT_MD" "$SCHEMA" "${I4R_SKIP_FINDING_SCHEMA:-0}" "$HERE" "$INVOCATION_SCHEMA" <<'PY'
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import tempfile

ws, registry, report, out_json, out_md = map(pathlib.Path, sys.argv[1:6])
schema_path, skip_schema = pathlib.Path(sys.argv[6]), sys.argv[7] == "1"
validator_dir = pathlib.Path(sys.argv[8])
invocation_schema = pathlib.Path(sys.argv[9])
ws = ws.resolve()
registry = registry.resolve()
report = report.resolve()
canonical_registry = ws / "work/findings.jsonl"
canonical_report = ws / "report/REPORT_final.md"
if registry != canonical_registry:
    raise SystemExit(f"findings registry must be canonical {canonical_registry}, observed {registry}")
if report != canonical_report:
    raise SystemExit(f"report must be canonical {canonical_report}, observed {report}")

def strict_loads(text, label):
    def pairs(values):
        out={}
        for key,value in values:
            if key in out: raise ValueError(f"{label}: duplicate JSON key {key!r}")
            out[key]=value
        return out
    def constant(value): raise ValueError(f"{label}: non-finite JSON number {value!r}")
    return json.loads(text,object_pairs_hook=pairs,parse_constant=constant)

def load_rows(path, raw=None):
    text = raw.decode("utf-8") if raw is not None else path.read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        value = strict_loads(text,str(path))
        if not isinstance(value, list):
            raise ValueError("findings JSON must be a list")
        return value
    rows = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = strict_loads(line,f"{path}:{line_no}")
        except Exception as exc:
            raise ValueError(f"invalid JSON on line {line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"line {line_no} is not an object")
        rows.append(row)
    return rows

def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def canonical_sha256(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def safe_regular_file(relative, label):
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError(f"{label}: missing audit-relative path")
    candidate = (ws / relative).resolve()
    try:
        candidate.relative_to(ws)
    except ValueError as exc:
        raise ValueError(f"{label}: path escapes audit directory: {relative}") from exc
    lexical = ws / relative
    if lexical.is_symlink() or not candidate.is_file():
        raise ValueError(f"{label}: file is missing or is a symlink: {relative}")
    return candidate

def iso_time(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO date-time") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed

def round_one_gate():
    run_path = safe_regular_file("run.json", "round-1 gate")
    status_path = safe_regular_file("critique/round_1/ROUND_STATUS.json", "round-1 gate")
    run = strict_loads(run_path.read_text(encoding="utf-8"), str(run_path))
    status = strict_loads(status_path.read_text(encoding="utf-8"), str(status_path))
    if not isinstance(run, dict) or not isinstance(status, dict):
        raise ValueError("round-1 gate records must be JSON objects")
    rounds = run.get("rounds")
    if not isinstance(rounds, list):
        raise ValueError("run.json has no rounds array")
    matches = [item for item in rounds if isinstance(item, dict) and item.get("round") == 1]
    if len(matches) != 1:
        raise ValueError(f"run.json must contain exactly one Round 1 record, observed {len(matches)}")
    round_one = matches[0]
    started = iso_time(round_one.get("started_at"), "Round 1 started_at")
    ended = iso_time(round_one.get("ended_at"), "Round 1 ended_at")
    integrated = iso_time(round_one.get("integrated_at"), "Round 1 integrated_at")
    if not started <= ended <= integrated:
        raise ValueError("Round 1 timestamps must satisfy started_at <= ended_at <= integrated_at")
    if round_one.get("consistency_check_run") is not True or round_one.get("consistency_check_exit") != 0:
        raise ValueError("Round 1 must have a recorded passing post-integration consistency check")
    expected = status.get("expected_roles")
    completed = status.get("completed_roles")
    failed = status.get("failed_roles")
    canonical_roles = ["stats", "data", "fairness", "completeness"]
    if (status.get("round") != 1 or status.get("outcome") != "complete" or
            status.get("exit_code") != 0 or status.get("quorum_met") is not True or
            expected != canonical_roles or
            completed != expected or failed != []):
        raise ValueError("Round 1 runtime status does not prove the canonical four-role quorum")
    if status.get("audit_id") != run.get("audit_id"):
        raise ValueError("Round 1 runtime status audit_id does not match run.json")
    run_projection = {
        "audit_id": run.get("audit_id"), "round": 1,
        "started_at": round_one["started_at"], "ended_at": round_one["ended_at"],
        "integrated_at": round_one["integrated_at"], "consistency_check_run": True,
        "consistency_check_exit": 0,
    }
    return {
        "run_path": "run.json", "round_projection_sha256": canonical_sha256(run_projection),
        "round_status_path": "critique/round_1/ROUND_STATUS.json",
        "round_status_sha256": sha256(status_path),
        "started_at": round_one["started_at"], "ended_at": round_one["ended_at"],
        "integrated_at": round_one["integrated_at"],
        "consistency_check_exit": 0,
    }, integrated

def load_invocations():
    ledger_path = safe_regular_file("invocations.jsonl", "canonical invocation ledger")
    ledger_bytes = ledger_path.read_bytes()
    rows = []
    for line_number, line in enumerate(ledger_bytes.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = strict_loads(line, f"{ledger_path}:{line_number}")
        if not isinstance(value, dict):
            raise ValueError(f"{ledger_path}:{line_number}: invocation must be an object")
        errors = validate_json_schema.errors_for_value(value, invocation_schema)
        if errors:
            raise ValueError(f"{ledger_path}:{line_number}: " + "; ".join(errors[:10]))
        try:
            validate_v2.validate_invocation(value, f"invocations.jsonl[{line_number}]")
        except validate_v2.ValidationError as exc:
            raise ValueError(str(exc)) from exc
        rows.append(value)
    ids = [row["invocation_id"] for row in rows]
    if not rows or len(ids) != len(set(ids)):
        raise ValueError("canonical invocation ledger is empty or has duplicate invocation IDs")
    return {row["invocation_id"]: row for row in rows}, {
        "path": "invocations.jsonl", "bound_prefix_bytes": len(ledger_bytes),
        "bound_prefix_sha256": hashlib.sha256(ledger_bytes).hexdigest(),
        "records_at_generation": len(rows),
    }

try:
    round_gate, round_one_integrated_at = round_one_gate()
except (ValueError, OSError) as exc:
    # The gate's own checks raise ValueError with the reason; show that reason, not a traceback.
    print(f"FATAL: Round-1 completion/integration/consistency gate: {exc}"
          + ("; re-journal it: bash scripts/log_event.sh round_integrated round=1 integrated_at=<the same time with "
             "Z or an offset>" if "timezone" in str(exc) else ""), file=sys.stderr)
    sys.exit(1)
run_metadata_path = safe_regular_file("run.json", "settled identity policy")
run_metadata = strict_loads(
    run_metadata_path.read_text(encoding="utf-8"), str(run_metadata_path)
)
# Round 0 (Phase 1) is bounded by round 1's integration; every later INTEGRATED critique round is
# bounded by its own integrated_at, so that round's confirmations are admissible evidence.
integrated_rounds = {0: round_one_integrated_at, 1: round_one_integrated_at}
for _row in ((run_metadata.get("rounds") if isinstance(run_metadata, dict) else None) or []):
    if not isinstance(_row, dict):
        continue
    _n, _at = _row.get("round"), _row.get("integrated_at")
    if isinstance(_n, int) and _n > 1 and _row.get("state") == "integrated" and _at:
        try:
            integrated_rounds[_n] = iso_time(_at, f"round {_n} integrated_at")
        except ValueError as _exc:
            print(f"FATAL: {_exc}; re-journal it: bash scripts/log_event.sh round_integrated round={_n} "
                  "integrated_at=<the same time with Z or an offset>", file=sys.stderr)
            sys.exit(1)
# A claim reworded at an integration was confirmed in its earlier wording only: it settles once a
# LATER round confirms the new wording, never on the records that reviewed the old one
# (adjudication_guide: settled facts rest on records confirming the claim text as published).
# Rewording is detected by claim TEXT, not id: a withdrawal renumber moves ids between findings
# without changing any wording, and comparing by id held every moved claim forever. Each integrated
# round's FINDINGS_BEFORE_INTEGRATION.jsonl is the text its critics reviewed; a text absent from one
# snapshot and present in the next was introduced at an integration in between (attributed to the
# latest round it could have been, which is the conservative choice). The final round is not
# integrated and has no snapshot; any other round missing one is treated as integrated unrecorded.
latest_round = max(integrated_rounds)
# A reviewer that called a finding OVERSTATED/REFUTED in a later, never-integrated round (the final clean
# round) no longer confirms it; a fact resting on it is not settled. One rule, shared with finalize.
sys.path.insert(0, str(validator_dir))
import later_disputes
_disputed_later = later_disputes.later_disputes(ws)
_invocation_rounds = later_disputes.invocation_rounds(ws)
_live_claims = {r["id"]: r.get("claim") for r in load_rows(registry)
                if isinstance(r, dict) and isinstance(r.get("id"), str)}
_texts = {}          # round -> claim text -> ids holding it in that round's snapshot
_exact = {}          # round -> ids in _texts are today's ids (translated through the renumber history)
_unrecorded = []
_history_path = ws / "work" / "id_map_history.jsonl"
_history = ([json.loads(line) for line in _history_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if _history_path.is_file() else None)
def _map_moves(path):
    try:
        return any(k != v for k, v in json.loads(path.read_text(encoding="utf-8")).items())
    except (OSError, ValueError, AttributeError):
        return path.is_file()
# no history but a renumber happened (2.6.3 wrote only work/id_map.json): ids cannot be translated exactly
_legacy_renumbered = later_disputes.legacy_renumbered(ws)   # one rule for every tool (F<n> moves only)
for _n in sorted(n for n in integrated_rounds if n >= 1):
    # integrate_round's write-once backup first: SKILL step 1's copy is overwritten when a round is
    # re-integrated from step 1, and the withdrawal renumber rewrites it to new ids.
    _backup = ws / "work" / f"findings.before_round{_n}_integration.jsonl"
    _copy = ws / "critique" / f"round_{_n}" / "FINDINGS_BEFORE_INTEGRATION.jsonl"
    _before = next((c for c in (_backup, _copy) if c.is_file() and not c.is_symlink()), None)
    if _before is None:
        if (ws / "critique" / f"round_{_n}" / "INTEGRATION_SUMMARY.json").is_file():
            _unrecorded.append(_n)      # integrated, but no snapshot survives
        continue                        # else never integrated (the clean final round)
    _rows = [r for r in load_rows(_before) if isinstance(r, dict)]
    _exact[_n] = _before == _backup and not _legacy_renumbered
    if _exact[_n]:
        _marker = _backup.with_suffix(".invocation")
        _backup_invocation = _marker.read_text(encoding="utf-8").strip() if _marker.is_file() else None
        for _entry in (_history or []):
            _ran = _entry.get("rounds_run_before_renumber") or {}
            if f"round_{_n}" not in _ran:
                continue
            if isinstance(_ran, dict) and _backup_invocation is not None and \
                    _ran[f"round_{_n}"] != _backup_invocation:
                continue    # the round ran again after this renumber: its backup already has the new ids
            _map = _entry.get("map") or {}
            for r in _rows:
                r["id"] = _map.get(r.get("id"), r.get("id"))
    _held = {}
    for r in _rows:
        _held.setdefault(r.get("claim"), set()).add(r.get("id"))
    _texts[_n] = _held
for _n in _unrecorded:
    if any(m < _n for m in _texts):
        print(f"warning: round {_n} was integrated but has no FINDINGS_BEFORE_INTEGRATION.jsonl; claims that "
              f"changed after the previous snapshot are held until a round after {_n} confirms them",
              file=sys.stderr)
    else:
        print(f"warning: round {_n} was integrated but no earlier snapshot exists, so claims reworded at it "
              "cannot be detected and are not held", file=sys.stderr)
_available = sorted(_texts)

def _reviewed(claim, fid, n):
    """Round n's critics reviewed `claim` for finding `fid`: some snapshot row held that text and is fid,
    or its id no longer holds that text now (a renumber moved it). A different live finding that still
    holds the same text is not fid's review (a merge that adopted another finding's wording)."""
    holders = _texts[n].get(claim, ())
    if _exact.get(n):
        return fid in holders
    return any(h == fid or _live_claims.get(h) != claim for h in holders)

def reworded_round(claim, fid):
    """The latest integration at which `claim`'s current text can have been introduced, or None."""
    found = None
    for _index, _n in enumerate(_available):
        if _reviewed(claim, fid, _n):
            continue
        nxt = _available[_index + 1] if _index + 1 < len(_available) else None
        if nxt is not None:
            if _reviewed(claim, fid, nxt):
                found = max([nxt - 1] + [m for m in _unrecorded if _n < m < nxt])
        else:
            found = max([_n] + [m for m in _unrecorded if m > _n])
    return found
live_metadata_2_1 = (
    isinstance(run_metadata, dict)
    and run_metadata.get("schema_version") == "2.1.0"
    and not run_metadata.get("normalization")
)

# The schemas are loaded for both ordinary and I4R_SKIP_FINDING_SCHEMA self-test execution:
# skipping the finding-row fixture schema must never skip canonical invocation validation.
sys.path.insert(0, str(validator_dir))
sys.path.insert(0, str(validator_dir.parent / "schemas/tools"))
import validate_json_schema
import validate_v2
invocations, invocation_ledger = load_invocations()

# Post-fan-out verifier (2.6.4, scripts/verifier_contract.py). Admission is computed once, over
# the whole ledger, and only when the ledger holds a role_class "verifier" row; without one this
# block does nothing and every output is byte-identical to 2.6.3.
verifier_contract = None
verifier_verdicts, verifier_errors, verifier_phase_one_roles = {}, [], None
if any(row.get("role_class") == "verifier" for row in invocations.values()):
    import verifier_contract
    verifier_verdicts, verifier_errors = verifier_contract.collect_verdicts(ws, list(invocations.values()))
# A verifier call (round 0) must have finished before the integration that bound it; the latest
# integration of an integrated round is the bound, since it may bind in any round.
verifier_bound_round = max(integrated_rounds, key=lambda number: (integrated_rounds[number], number))

def phase_one_roles():
    global verifier_phase_one_roles
    if verifier_phase_one_roles is None:
        verifier_phase_one_roles = verifier_contract.required_roles(
            verifier_contract.load_snapshot_policy(ws))
    return verifier_phase_one_roles

def confirmations(row, verifier_notes=None):
    good = []
    for index, value in enumerate(row.get("verifications") or []):
        if not isinstance(value, dict):
            raise ValueError(f"verifications[{index}] is not an object")
        outcome = str(value.get("outcome") or "").lower()
        if outcome not in {"confirmed", "pass", "verified"}:
            continue
        reviewer = value.get("reviewer_id")
        invocation_id = value.get("invocation_id")
        family = value.get("model_family")
        record = value.get("evidence_record")
        if not all(isinstance(item, str) and item.strip()
                   for item in (reviewer, invocation_id, family)) or not isinstance(record, dict):
            raise ValueError(f"verifications[{index}] lacks reviewer/invocation/family/evidence binding")
        relative, expected_hash = record.get("path"), record.get("sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError(f"verifications[{index}] has an invalid evidence-record hash")
        invocation = invocations.get(invocation_id)
        if invocation is None:
            raise ValueError(f"verification invocation does not exist in invocations.jsonl: {invocation_id}")
        # A cache replay is bookkeeping, not a second review: it re-serves the bytes an earlier
        # provider call produced, and records that call in source_invocation_id. Any resume --
        # a killed Phase 1, a recovered strand, a rerun after a fix -- replays the roles that
        # already completed, so their reports are legitimately served from cache. Refusing the
        # replay id made every such audit unsettleable, and the operator's only route was to
        # hand-rewrite the registry. Follow the pointer to the call that did the work instead;
        # the bytes are hash-checked below either way.
        seen_replays = set()
        while invocation.get("attempt_kind") == "cache_replay":
            source = invocation.get("source_invocation_id")
            if not source or source in seen_replays:
                raise ValueError(
                    f"verification cites a cache replay with no resolvable source call: {invocation_id}")
            seen_replays.add(source)
            invocation = invocations.get(source)
            if invocation is None:
                raise ValueError(
                    f"verification cites a cache replay whose source call is absent from "
                    f"invocations.jsonl: {source}")
        # "verifier" is admitted only through its own branch below (receipt-bound record, timing
        # bound, and a CONFIRMED verdict for this row's current claim text).
        is_verifier = invocation.get("role_class") == "verifier"
        if (invocation.get("status") != "completed" or invocation.get("provider_call") is not True or
                invocation.get("role_class") not in {"auditor", "critic", "adjudicator", "verifier"}):
            raise ValueError(f"verification invocation is not a completed reviewer provider call: {invocation_id}")
        if is_verifier and verifier_errors:
            # An inadmissible verifier record, even one on another claim, might hold a REFUTED for
            # this row's claim text, so a row that cites a verifier cannot be promoted. Only such
            # rows depend on verifier verdicts: they are rejected with the reason, and every other
            # fact is still settled (aborting here lost the whole cache over one record).
            note = "verifier: verifier record is not admissible: " + "; ".join(verifier_errors)
            if verifier_notes is None:
                raise ValueError(note.removeprefix("verifier: "))
            if note not in verifier_notes:
                verifier_notes.append(note)
            continue
        if invocation.get("role") != reviewer:
            raise ValueError(f"reviewer_id does not match invocation role for {invocation_id}")
        invocation_family = str(invocation.get("model_family") or "").strip().lower()
        if (str(family).strip().lower() != invocation_family or
                invocation_family in {"", "unknown", "unavailable"}):
            raise ValueError(f"model_family does not match canonical invocation {invocation_id}")
        served = invocation.get("served_model") or invocation.get("observed_model")
        if served:
            if (invocation.get("model_source") != "observed"
                    or invocation.get("identity_assurance", "observed") != "observed"):
                raise ValueError(
                    f"verification invocation has unobserved served-model identity: {invocation_id}"
                )
        elif not (
            live_metadata_2_1
            and invocation.get("identity_assurance") == "requested_only"
            and isinstance(invocation.get("requested_model"), str)
            and invocation["requested_model"].strip()
            and invocation.get("model_source") in {"declared", "unavailable"}
        ):
            raise ValueError(
                f"verification invocation lacks an honest model-identity disposition: {invocation_id}"
            )
        # A reviewer call from any INTEGRATED round may back a settled fact, bounded by that
        # round's own integration time. Restricting this to round 1 forced later rounds' literal
        # confirmations of corrected claims to be left unbound (first audit: F8 and F11 were
        # promoted on round-1 records whose verdicts read OVERSTATED).
        invocation_round = invocation.get("round")
        if not isinstance(invocation_round, int) or invocation_round not in integrated_rounds:
            raise ValueError(
                f"verification invocation is not from an integrated round: {invocation_id}"
            )
        bound = integrated_rounds[invocation_round]
        if is_verifier:
            # A verifier call is round 0 but may be bound by any round's integration: it must
            # have ended no later than the latest integrated round's integration.
            if (iso_time(invocation.get("ended_at"), f"{invocation_id}.ended_at") >
                    integrated_rounds[verifier_bound_round]):
                raise ValueError(
                    f"verification invocation ended after round {verifier_bound_round} integration, "
                    f"the latest integration: {invocation_id}"
                )
        elif iso_time(invocation.get("ended_at"), f"{invocation_id}.ended_at") > bound:
            raise ValueError(
                f"verification invocation ended after round {invocation_round} integration: {invocation_id}"
            )
        evidence_path = safe_regular_file(relative, f"verification evidence for {invocation_id}")
        actual_hash = sha256(evidence_path)
        if actual_hash != expected_hash:
            raise ValueError(f"verification evidence hash changed for {relative}")
        if (invocation.get("output_artifact") != relative or
                invocation.get("output_sha256") != expected_hash):
            raise ValueError(f"evidence record is not the canonical output of invocation {invocation_id}")
        if is_verifier:
            problem = verifier_contract.confirmation_problem(
                row, str(invocation.get("invocation_id")), phase_one_roles(), verifier_verdicts)
            if problem:
                # A well-formed record that does not confirm this claim text does not count; the
                # row is rejected with the reason rather than aborting every other fact.
                if verifier_notes is not None:
                    verifier_notes.append(f"verifier: {problem}")
                continue
        good.append({
            "reviewer_id": reviewer, "invocation_id": invocation_id,
            "evidence_record": {"path": relative, "sha256": expected_hash},
            "model_family": family, "outcome": outcome,
        })
    return good

def _confirms_new_wording(item, reworded_at):
    """A record that reviewed the claim's current wording: a critic/adjudicator call of a round after the
    rewording, or an admitted post-fan-out verifier call (round 0, but confirmations() keeps it only when it
    returned CONFIRMED for this exact claim text's hash)."""
    invocation = invocations.get(item["invocation_id"]) or {}
    if invocation.get("role_class") == "verifier":
        return True
    return isinstance(invocation.get("round"), int) and invocation["round"] > reworded_at


def evidence_for(row):
    ev = row.get("evidence")
    if not isinstance(ev, dict):
        return None
    command = ev.get("rerun_command") or ev.get("command")
    hashes = ev.get("source_hashes") or ev.get("hashes")
    if not isinstance(command, str) or not command.strip() or not isinstance(hashes, list) or not hashes:
        return None
    checked = []
    for item in hashes:
        if not isinstance(item, dict):
            return None
        rel, expected = item.get("path"), item.get("sha256")
        if not isinstance(rel, str) or not isinstance(expected, str) or len(expected) != 64:
            return None
        candidate = (ws / rel).resolve()
        try:
            candidate.relative_to(ws.resolve())
        except ValueError:
            raise ValueError(f"evidence path escapes audit directory: {rel}")
        if not candidate.is_file():
            raise ValueError(f"evidence file is missing: {rel}")
        actual = sha256(candidate)
        if actual != expected.lower():
            raise ValueError(f"evidence hash changed for {rel}: expected {expected}, observed {actual}")
        checked.append({"path": rel, "sha256": actual})
    return {"source_hashes": checked, "rerun_command": command.strip(),
            "expected": ev.get("expected"), "observed": ev.get("observed")}

registry_bytes = registry.read_bytes()
report_bytes = report.read_bytes()
registry_sha256 = hashlib.sha256(registry_bytes).hexdigest()
report_sha256 = hashlib.sha256(report_bytes).hexdigest()
rows = load_rows(registry, registry_bytes)
if not skip_schema:
    sys.path.insert(0, str(validator_dir))
    sys.path.insert(0, str(validator_dir.parent / "schemas/tools"))
    import validate_json_schema
    import validate_v2
    errors = []
    for index, row in enumerate(rows, 1):
        for error in validate_json_schema.errors_for_value(row, schema_path):
            errors.append(f"line {index}{error}")
        try:
            validate_v2.validate_finding_row(row, f"line {index}")
        except validate_v2.ValidationError as exc:
            errors.append(str(exc))
    if errors:
        raise SystemExit("findings registry failed schema validation:\n  " + "\n  ".join(errors[:30]))

facts, rejected = [], []
for row in rows:
    fid = row.get("id") or row.get("finding_id")
    why = []
    if not isinstance(fid, str) or not fid:
        why.append("missing stable id")
    if str(row.get("status") or row.get("verdict") or "").lower() not in {"confirmed", "verified", "accepted"}:
        why.append("status is not confirmed")
    settled = row.get("settled") if isinstance(row.get("settled"), dict) else {}
    if row.get("coordinator_approved") is not True:
        why.append("coordinator approval missing")
    if settled.get("approved") is not True:
        why.append("settled promotion approval missing")
    if settled.get("valid_for_report_hash") != report_sha256:
        why.append("valid_for_report_hash does not match the current canonical report")
    if (row.get("ledger_disposition") or {}).get("status") != "INCLUDED":
        why.append("finding is not INCLUDED in the final ledger")
    verifier_notes = []
    reviewers = confirmations(row, verifier_notes)
    verifier_at = len(why)
    _disputing = later_disputes.stale_confirmations(ws, fid, reviewers, _disputed_later, _invocation_rounds)
    if _disputing:
        # Excluded, not filtered: a fact's confirmations must equal the registry row's (make_diff_packet's
        # closed validation), and the registry keeps the record until that round is integrated.
        why.append(f"{', '.join(_disputing)} confirmed it earlier but called it OVERSTATED/REFUTED in a later, "
                   "unintegrated round")
    _reworded = reworded_round(row.get("claim"), fid) if _available else None
    if _reworded is not None and not any(_confirms_new_wording(item, _reworded) for item in reviewers):
        why.append(f"claim text was revised at round {_reworded} integration and no later round "
                   "has confirmed the new wording yet (a later round's literal CONFIRMED verdict is bound "
                   "by that round's decision rebind_literal_confirmations: true)")
    if len(reviewers) < 2:
        why.append("fewer than two independent confirmations")
    if len({item["reviewer_id"] for item in reviewers}) < 2:
        why.append("confirmations do not contain two distinct reviewer identities")
    if len({item["invocation_id"] for item in reviewers}) < 2:
        why.append("confirmations do not contain two distinct canonical invocations")
    if len({(item["evidence_record"]["path"], item["evidence_record"]["sha256"])
            for item in reviewers}) < 2:
        why.append("confirmations do not contain two distinct evidence records")
    if len({item["model_family"].strip().lower() for item in reviewers}) < 2:
        why.append("confirmations do not contain two distinct model families")
    ev = evidence_for(row)
    if ev is None:
        why.append("rerunnable evidence with source hashes missing")
    deps = row.get("dependencies") or {}
    env = deps.get("environment_fingerprint") if isinstance(deps, dict) else None
    if not env:
        why.append("environment fingerprint missing")
    if verifier_notes:
        # A confirmed-outcome verifier record that does not count rejects its row, even when the
        # other records would promote it: a fact must never omit a confirmation its registry row
        # carries (make_diff_packet.py requires the two to be equal and refuses the whole cache).
        why[verifier_at:verifier_at] = verifier_notes
    if why:
        rejected.append({"id": fid or "<missing>", "reasons": why})
        continue
    facts.append({"id": fid, "claim": row.get("claim") or row.get("statement"),
                  "value": row.get("value") or ev.get("observed"), "evidence": ev,
                  "confirmations": reviewers, "environment_fingerprint": env,
                  "coordinator_approved": True,
                  "valid_for_report_hash": report_sha256})
    if _reworded is not None:
        # The family rule still counts records of the earlier wording: only the critics' family reviews
        # after Phase 1, so requiring two families on the new wording would mean a reworded claim can
        # never settle. Say so in the fact rather than imply both families saw the published text.
        facts[-1]["reworded_at_round"] = _reworded
        facts[-1]["new_wording_confirmed_by_families"] = sorted({
            item["model_family"] for item in reviewers if _confirms_new_wording(item, _reworded)})

payload = {"schema_version": "2.0.0",
           "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "source_registry": "work/findings.jsonl",
           "source_registry_sha256": registry_sha256,
           "report_path": "report/REPORT_final.md",
           "report_sha256": report_sha256,
           "round_1_gate": round_gate,
           "invocation_ledger": invocation_ledger,
           "invalidated": False,
           "facts": sorted(facts, key=lambda x: x["id"]), "rejected": rejected}
lines = ["# Settled facts — independently verified after round 1", "",
         "Critics may use these records as cached evidence, but must spot-check every fact on which a",
         "criticism depends. Any failed spot-check invalidates the complete cache and is itself a finding.", ""]
if not facts:
    lines += ["_No findings satisfied the promotion gate._", ""]
for fact in sorted(facts, key=lambda x: x["id"]):
    lines += [f"## {fact['id']}: {fact.get('claim') or '(no claim text)'}", ""]
    if fact.get("value") is not None:
        lines += [f"- Value: `{fact['value']}`"]
    if fact.get("reworded_at_round") is not None:
        lines += [f"- Reworded at round {fact['reworded_at_round']} integration: the published wording is "
                  f"confirmed by {', '.join(fact['new_wording_confirmed_by_families'])} only; earlier records "
                  "reviewed the prior wording."]
    lines += [f"- Environment: `{fact['environment_fingerprint']}`",
              "- Confirmed by: " + ", ".join(
                  f"`{x['reviewer_id']}` ({x['model_family']}; invocation `{x['invocation_id']}`; "
                  f"evidence `{x['evidence_record']['path']}`)" for x in fact["confirmations"]),
              f"- Re-run: `{fact['evidence']['rerun_command']}`"]
    for source in fact["evidence"]["source_hashes"]:
        lines += [f"- Source: `{source['path']}` — sha256 `{source['sha256']}`"]
    lines += [""]

def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data); fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

atomic_write(out_json, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
atomic_write(out_md, "\n".join(lines))
print(f"settled facts: {len(facts)} promoted, {len(rejected)} rejected")
print(f"wrote {out_json} and {out_md}")
PY
