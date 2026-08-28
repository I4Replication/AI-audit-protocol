#!/bin/bash
# Deterministic Codex CLI double for test_run_critique.sh.
set -uo pipefail

if [ "${1:-}" = "--version" ] || [ "${1:-}" = "-V" ]; then
  echo "codex-cli fake-1.0"
  exit 0
fi
[ "${1:-}" = exec ] || { echo "fake_codex: expected exec" >&2; exit 2; }
shift

output=""
schema=""
workspace=""
model="unknown"
effort="unknown"
while [ $# -gt 0 ]; do
  case "$1" in
    -o|--output-last-message) output="$2"; shift 2 ;;
    --output-schema) schema="$2"; shift 2 ;;
    -C|--cd) workspace="$2"; shift 2 ;;
    -m|--model) model="$2"; shift 2 ;;
    -c|--config)
      case "$2" in model_reasoning_effort=*) effort="${2#model_reasoning_effort=}" ;; esac
      shift 2
      ;;
    -s|--sandbox|--color|--thread-source) shift 2 ;;
    --skip-git-repo-check|--ignore-user-config|--ephemeral|--json|--ignore-rules) shift ;;
    -) shift; break ;;
    *) shift ;;
  esac
done

[ -n "$output" ] || { echo "fake_codex: missing -o" >&2; exit 2; }
[ -n "$schema" ] || { echo "fake_codex: missing --output-schema" >&2; exit 2; }
[ -n "$workspace" ] || { echo "fake_codex: missing -C" >&2; exit 2; }
[ -n "${FAKE_CODEX_STATE:-}" ] || { echo "fake_codex: set FAKE_CODEX_STATE" >&2; exit 2; }

prompt_file=$(mktemp "${TMPDIR:-/tmp}/fake_codex_prompt.XXXXXX") || exit 2
trap 'rm -f "$prompt_file"' EXIT INT TERM HUP
sed -n '1,$p' > "$prompt_file"

exec python3 - "$prompt_file" "$output" "$schema" "$workspace" "$model" "$effort" "$FAKE_CODEX_STATE" <<'PY'
import json
import hashlib
import os
import pathlib
import re
import sys
import time

prompt_path, output_path, schema_path, workspace_path, model_path, effort_path, state_path = map(
    pathlib.Path, sys.argv[1:8]
)
model = str(model_path)
effort = str(effort_path)
prompt = prompt_path.read_text()
try:
    prompt_path.unlink()
except OSError:
    pass
workspace = pathlib.Path(workspace_path)
output_schema = json.loads(pathlib.Path(schema_path).read_text())
state = pathlib.Path(state_path)
state.mkdir(parents=True, exist_ok=True)

if "I4R_RUNTIME_PROBE" in prompt:
    role = "model_probe"
elif "I4R_TARGETED_ADJUDICATION" in prompt:
    role = "targeted_adjudicator"
else:
    match = re.search(r"^runtime_role=([a-z_]+)$", prompt, re.M)
    if not match:
        print("fake_codex: runtime role missing", file=sys.stderr)
        raise SystemExit(2)
    role = match.group(1)

lock = state / ".lock"
for _ in range(1000):
    try:
        lock.mkdir()
        break
    except FileExistsError:
        time.sleep(0.005)
else:
    print("fake_codex: state lock timeout", file=sys.stderr)
    raise SystemExit(2)
try:
    count_path = state / f"calls_{role}"
    try:
        count = int(count_path.read_text()) + 1
    except Exception:
        count = 1
    tmp = state / f".{count_path.name}.{os.getpid()}.tmp"
    tmp.write_text(str(count) + "\n")
    os.replace(tmp, count_path)
    global_path = state / "calls_total"
    try:
        global_count = int(global_path.read_text()) + 1
    except Exception:
        global_count = 1
    tmp = state / f".{global_path.name}.{os.getpid()}.tmp"
    tmp.write_text(str(global_count) + "\n")
    os.replace(tmp, global_path)
    with (state / f"efforts_{role}").open("a", encoding="utf-8") as handle:
        handle.write(effort + "\n")
    with (state / f"models_{role}").open("a", encoding="utf-8") as handle:
        handle.write(model + "\n")
finally:
    lock.rmdir()

scenario = os.environ.get("FAKE_CODEX_SCENARIO", "success")
transient_role = os.environ.get("FAKE_TRANSIENT_ROLE", "stats")
permanent_role = os.environ.get("FAKE_PERMANENT_ROLE", "data")
incomplete_role = os.environ.get("FAKE_INCOMPLETE_ROLE", "data")

print(json.dumps({
    "type": "thread.started", "thread_id": f"fake-{role}-{count}",
    "model": model, "reasoning_effort": effort,
}))
if scenario in ("transient_once", "transient_always") and role == transient_role \
        and (scenario == "transient_always" or count == 1):
    print(json.dumps({"type": "error", "message": "stream disconnected; try again"}))
    print("stream disconnected; try again", file=sys.stderr)
    raise SystemExit(75)
if scenario == "permanent" and role == permanent_role:
    print(json.dumps({"type": "error", "message": "invalid_request_error: model is not supported"}))
    print("invalid_request_error: model is not supported", file=sys.stderr)
    raise SystemExit(2)
if scenario == "slow" and role == os.environ.get("FAKE_SLOW_ROLE", "stats"):
    (state / "slow_started").write_text(f"{os.getpid()}\n")
    time.sleep(30)
    (state / "slow_finished").write_text("unexpected\n")

def runtime_value(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}=(.+)$", prompt, re.M)
    if not match:
        raise ValueError(f"missing {name}")
    return match.group(1).strip()

def schema_const(name: str):
    return output_schema["properties"][name]["const"]

def input_hash_consts():
    properties = output_schema["properties"]["input_hashes"]["properties"]
    return {name: spec["const"] for name, spec in properties.items()}

def covered_claims_or(fallback):
    return output_schema["properties"]["covered_claim_ids"].get("const", fallback)

def frozen_binding(relative: str):
    snapshot = workspace / runtime_value("runtime_snapshot")
    path = snapshot / relative
    return {
        "path": relative,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }

if role == "model_probe":
    response = {"status": "ok"}
elif role == "targeted_adjudicator":
    packet_path = workspace / runtime_value("runtime_packet")
    packet = json.loads(packet_path.read_text())
    response = {
        "schema_version": "2.0.0",
        "audit_id": packet["audit_id"],
        "invocation_id": schema_const("invocation_id"),
        "round": packet["round"],
        "role": "critic_synth",
        "report_sha256": packet["report_sha256"],
        "input_hashes": input_hash_consts(),
        "status": "complete",
        "completion_marker": True,
        "completeness_declaration": {
            "all_assigned_checks_accounted_for": True,
            "all_assessments_evidenced": True,
            "unresolved_omissions": [],
        },
        "coverage": {
            "assigned_checks": [row["row_id"] for row in packet["unresolved_rows"]],
            "completed_checks": [row["row_id"] for row in packet["unresolved_rows"]],
            "skipped_checks": [],
        },
        "covered_claim_ids": covered_claims_or(
            [row["row_id"] for row in packet["unresolved_rows"]]),
        "conflicts": [],
        "settled_spot_checks": [],
        "items": [
            {
                "item_id": row["row_id"],
                "finding_id": row["row_id"],
                "claim": "The targeted evidence supports a bounded coordinator decision.",
                "verdict": "CONFIRMED",
                "priority": "NON_BLOCKING",
                "confidence": "CONFIRMED",
                "evidence": {
                    "artifact_refs": [frozen_binding("source/adjudication_packet.json")],
                    "operation": "Compare the bounded unresolved row with its merged role evidence.",
                    "result": "The immutable unresolved-row packet identifies the exact source and scope.",
                    "reproducible_script": None,
                },
                "unverifiable_basis": None,
                "requested_correction": None,
            }
            for row in packet["unresolved_rows"]
        ],
        "new_issues": [],
        "overall": {
            "publishable": True,
            "blocking_count": 0,
            "summary": "Apply only the trigger-specific decisions recorded in this targeted review.",
        },
    }
else:
    round_number = int(runtime_value("runtime_round"))
    input_sha = runtime_value("runtime_input_sha256")
    report_sha = runtime_value("runtime_report_sha256")
    report_binding = frozen_binding("source/REPORT_TO_REVIEW.md")
    adjudication_required = scenario == "adjudicate" and role in ("stats", "data")
    items = []
    if adjudication_required:
        items.append({
            "item_id": "stats-F1",
            "finding_id": "F1",
            "claim": "The deliberately planted quantitative claim is too broad.",
            "verdict": "OVERSTATED" if role == "stats" else "CONFIRMED",
            "priority": "NON_BLOCKING",
            "confidence": "CONFIRMED",
            "evidence": {
                "artifact_refs": [report_binding],
                "operation": "Compare the bounded statement with the immutable report.",
                "result": "The statement requires a narrower qualification.",
                "reproducible_script": None,
            },
            "unverifiable_basis": None,
            "requested_correction": (
                "Narrow the statement to the evidence actually reviewed."
                if role == "stats" else None
            ),
        })
    if scenario == "bad_path" and role == incomplete_role:
        items.append({
            "item_id": "bad-path-F1",
            "finding_id": "F1",
            "claim": "This fixture deliberately uses an invalid absolute evidence path.",
            "verdict": "CONFIRMED",
            "priority": "NON_BLOCKING",
            "confidence": "CONFIRMED",
            "evidence": {
                "artifact_refs": [{"path": "/tmp/escaped-evidence.txt", "sha256": "0" * 64}],
                "operation": "Read an invalid absolute artifact path.",
                "result": "The runtime must reject this otherwise complete object.",
                "reproducible_script": None,
            },
            "unverifiable_basis": None,
            "requested_correction": None,
        })
    fallback_claims = list(dict.fromkeys(
        item["finding_id"] for item in items if item["finding_id"] is not None
    ))
    if scenario in ("empty_assessments", "live_only", "wrong_evidence_hash",
                    "tamper_evidence") and role == incomplete_role:
        fallback_claims = ["F1"]
    assigned_claims = covered_claims_or(fallback_claims)
    existing = {item["finding_id"] for item in items}
    for claim_id in assigned_claims:
        if claim_id in existing:
            continue
        items.append({
            "item_id": f"{role}-{claim_id}",
            "finding_id": claim_id,
            "claim": f"The {role} review completed its bounded assessment of {claim_id}.",
            "verdict": "CONFIRMED",
            "priority": "NON_BLOCKING",
            "confidence": "CONFIRMED",
            "evidence": {
                "artifact_refs": [report_binding],
                "operation": "Review the assigned changed claim against the immutable report.",
                "result": "The bounded changed claim has one explicit role-level outcome.",
                "reproducible_script": None,
            },
            "unverifiable_basis": None,
            "requested_correction": None,
        })
    response = {
        "schema_version": "2.0.0",
        "audit_id": runtime_value("runtime_audit_id"),
        "invocation_id": schema_const("invocation_id"),
        "round": round_number,
        "role": f"critic_{role}",
        "report_sha256": report_sha,
        "input_hashes": input_hash_consts(),
        "status": "complete",
        "completion_marker": True,
        "completeness_declaration": {
            "all_assigned_checks_accounted_for": True,
            "all_assessments_evidenced": True,
            "unresolved_omissions": [],
        },
        "coverage": {
            "assigned_checks": [f"full {role} scope"],
            "completed_checks": [f"full {role} scope"],
            "skipped_checks": [],
        },
        "covered_claim_ids": assigned_claims,
        "conflicts": [],
        "settled_spot_checks": [],
        "items": items,
        "new_issues": [],
        "overall": {
            "publishable": True,
            "blocking_count": 0,
            "summary": f"The {role} review completed its assigned scope without a verified blocking defect.",
        },
    }
    if scenario == "incomplete" and role == incomplete_role:
        response["coverage"].pop("completed_checks")
    if scenario == "missing_invocation" and role == incomplete_role:
        response.pop("invocation_id")
    if scenario == "wrong_input_hash" and role == incomplete_role:
        response["input_hashes"]["snapshot_sha256"] = "0" * 64
    if scenario == "missing_declaration" and role == incomplete_role:
        response.pop("completeness_declaration")
    if scenario == "all_skipped" and role == incomplete_role:
        check = response["coverage"]["assigned_checks"][0]
        response["coverage"]["completed_checks"] = []
        response["coverage"]["skipped_checks"] = [{"check": check, "reason": "fixture"}]
    if scenario == "empty_assessments" and role == incomplete_role:
        response["items"] = []
        response["covered_claim_ids"] = ["F1"]
    if scenario == "live_only" and role == incomplete_role and response["items"]:
        live = workspace / "report/REPORT_final.md"
        response["items"][0]["evidence"]["artifact_refs"] = [{
            "path": "report/REPORT_final.md",
            "sha256": hashlib.sha256(live.read_bytes()).hexdigest(),
        }]
    if scenario == "wrong_evidence_hash" and role == incomplete_role and response["items"]:
        response["items"][0]["evidence"]["artifact_refs"][0]["sha256"] = "0" * 64
    if scenario == "tamper_evidence" and role == incomplete_role:
        frozen = workspace / runtime_value("runtime_snapshot") / "source/REPORT_TO_REVIEW.md"
        frozen.chmod(0o644)
        with frozen.open("a") as handle:
            handle.write("provider-side tamper\n")
    if scenario == "nondeterministic":
        response["overall"]["summary"] += f" Provider call ordinal {count}."

output_path = pathlib.Path(output_path)
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text(json.dumps(response, indent=2, sort_keys=True) + "\n")
usage = {
    "input_tokens": 100 + count,
    "cache_read_input_tokens": 25,
    "cache_write_input_tokens": 7,
    "output_tokens": 30,
    "reasoning_output_tokens": 10,
}
if scenario != "missing_total_usage":
    usage["total_tokens"] = 140 + count
print(json.dumps({
    "type": "turn.completed",
    "model": model,
    "usage": usage,
}))
PY
