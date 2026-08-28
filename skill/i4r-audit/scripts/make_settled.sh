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
{"audit_id":"settled-self-test","rounds":[{"round":1,"started_at":"2026-08-27T10:00:00Z","ended_at":"2026-08-27T11:00:00Z","integrated_at":"2026-08-27T12:00:00Z","consistency_check_run":true,"consistency_check_exit":0}]}
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

round_gate, round_one_integrated_at = round_one_gate()

# The schemas are loaded for both ordinary and I4R_SKIP_FINDING_SCHEMA self-test execution:
# skipping the finding-row fixture schema must never skip canonical invocation validation.
sys.path.insert(0, str(validator_dir))
sys.path.insert(0, str(validator_dir.parent / "schemas/tools"))
import validate_json_schema
import validate_v2
invocations, invocation_ledger = load_invocations()

def confirmations(row):
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
        if (invocation.get("status") != "completed" or invocation.get("provider_call") is not True or
                invocation.get("role_class") not in {"auditor", "critic", "adjudicator"}):
            raise ValueError(f"verification invocation is not a completed reviewer provider call: {invocation_id}")
        if invocation.get("role") != reviewer:
            raise ValueError(f"reviewer_id does not match invocation role for {invocation_id}")
        invocation_family = str(invocation.get("model_family") or "").strip().lower()
        if (str(family).strip().lower() != invocation_family or
                invocation_family in {"", "unknown", "unavailable"}):
            raise ValueError(f"model_family does not match canonical invocation {invocation_id}")
        if invocation.get("model_source") != "observed" or not invocation.get("observed_model"):
            raise ValueError(f"verification invocation lacks observed served-model identity: {invocation_id}")
        if not isinstance(invocation.get("round"), int) or invocation["round"] not in {0, 1}:
            raise ValueError(f"verification invocation occurred after Round 1: {invocation_id}")
        if iso_time(invocation.get("ended_at"), f"{invocation_id}.ended_at") > round_one_integrated_at:
            raise ValueError(f"verification invocation ended after Round 1 integration: {invocation_id}")
        evidence_path = safe_regular_file(relative, f"verification evidence for {invocation_id}")
        actual_hash = sha256(evidence_path)
        if actual_hash != expected_hash:
            raise ValueError(f"verification evidence hash changed for {relative}")
        if (invocation.get("output_artifact") != relative or
                invocation.get("output_sha256") != expected_hash):
            raise ValueError(f"evidence record is not the canonical output of invocation {invocation_id}")
        good.append({
            "reviewer_id": reviewer, "invocation_id": invocation_id,
            "evidence_record": {"path": relative, "sha256": expected_hash},
            "model_family": family, "outcome": outcome,
        })
    return good

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
    reviewers = confirmations(row)
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
    if why:
        rejected.append({"id": fid or "<missing>", "reasons": why})
        continue
    facts.append({"id": fid, "claim": row.get("claim") or row.get("statement"),
                  "value": row.get("value") or ev.get("observed"), "evidence": ev,
                  "confirmations": reviewers, "environment_fingerprint": env,
                  "coordinator_approved": True,
                  "valid_for_report_hash": report_sha256})

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
