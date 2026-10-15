#!/bin/bash
# Deterministic Codex CLI double for test_run_critique.sh.
set -uo pipefail

if [ "${1:-}" = "--version" ] || [ "${1:-}" = "-V" ]; then
  echo "codex-cli ${FAKE_CODEX_VERSION:-fake-1.0}"   # 2.7.5: FAKE_CODEX_VERSION (check_critic_access.py's CLI floor)
  exit 0
fi
# 2.7.5 (scripts/check_critic_access.py reads the login line): FAKE_CODEX_LOGIN overrides it
if [ "${1:-}" = login ] && [ "${2:-}" = status ]; then
  echo "${FAKE_CODEX_LOGIN:-Logged in using an API key - sk-fake****1234}"
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

# 2.7.5: scripts/check_critic_access.py's one minimal call before an audit starts. Counted in access_calls;
# FAKE_UNSUPPORTED_MODEL and FAKE_ACCESS_SCENARIO=transport make it fail the way a real account or network does.
if "I4R_CRITIC_ACCESS_CHECK" in prompt:
    calls_file = state / "access_calls"
    calls_file.write_text(str(int(calls_file.read_text() or 0) + 1 if calls_file.exists() else 1))
    (state / "access_requests.jsonl").open("a").write(json.dumps({"model": model, "effort": effort}) + "\n")
    if os.environ.get("FAKE_UNSUPPORTED_MODEL") == model:
        reason = f"The '{model}' model is not supported when using Codex with a ChatGPT account."
        print(json.dumps({"type": "thread.started", "thread_id": "fake-access"}))
        print(json.dumps({"type": "error", "message": json.dumps(
            {"type": "error", "status": 400, "error": {"type": "invalid_request_error", "message": reason}})}))
        print(json.dumps({"type": "turn.failed", "error": {"message": reason}}))
        sys.exit(1)
    if os.environ.get("FAKE_ACCESS_SCENARIO") == "transport":
        print("stream disconnected before completion: error sending request", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("FAKE_ACCESS_SCENARIO") in {"key_without_model", "provider_other"}:
        reason = (f"The model `{model}` does not exist or you do not have access to it."
                  if os.environ["FAKE_ACCESS_SCENARIO"] == "key_without_model" else "The request was rejected.")
        print(json.dumps({"type": "thread.started", "thread_id": "fake-access"}))
        print(json.dumps({"type": "error", "message": reason}))
        print(json.dumps({"type": "turn.failed", "error": {"message": reason}}))
        sys.exit(1)
    if os.environ.get("FAKE_ACCESS_SCENARIO") == "cli_rejects":
        print("error: unexpected argument '--output-schema' found", file=sys.stderr)
        print("Usage: codex exec [OPTIONS] [PROMPT]", file=sys.stderr)
        sys.exit(2)
    pathlib.Path(output_path).write_text(json.dumps({"status": "I4R_CRITIC_ACCESS_OK"}))
    sys.exit(0)

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

# Simulate a provider rejecting the output schema before a served model identity or usage exists.
# The CLI process still made a provider request, so the runtime must retain provider_call=true.
# An account that cannot use one model, as a teammate's ChatGPT account could not use gpt-6-sol:
# codex-cli prints an unrelated models-cache warning to stderr first, and the real 400 arrives only
# as a JSON error event. Any other model behaves as the requested scenario.
unsupported_model = os.environ.get("FAKE_UNSUPPORTED_MODEL")
if unsupported_model and model == unsupported_model:
    print("2026-09-23T05:42:18.112028Z ERROR codex_models_manager::manager: failed to load models "
          "cache: missing field `supports_parallel_tool_calls` at line 138 column 5", file=sys.stderr)
    reason = f"The '{model}' model is not supported when using Codex with a ChatGPT account."
    print(json.dumps({"type": "thread.started", "thread_id": f"fake-{role}-{count}"}))
    print(json.dumps({"type": "error", "message": json.dumps(
        {"type": "error", "status": 400, "error": {"type": "invalid_request_error", "message": reason}})}))
    print(json.dumps({"type": "turn.failed", "error": {"message": reason}}))
    raise SystemExit(1)
if scenario == "probe_preinference_reject" and role == "model_probe":
    message = (
        "invalid_request_error: invalid_json_schema: output schema invalid for response_format; "
        "schema must have a type key (HTTP 400)"
    )
    print(json.dumps({"type": "error", "message": message}))
    print(message, file=sys.stderr)
    raise SystemExit(2)

print(json.dumps({
    "type": "thread.started", "thread_id": f"fake-{role}-{count}",
    "model": model, "reasoning_effort": effort,
}))
if scenario in ("transient_once", "transient_always") and role == transient_role \
        and (scenario == "transient_always" or count == 1):
    print(json.dumps({"type": "error", "message": "stream disconnected; try again"}))
    print("stream disconnected; try again", file=sys.stderr)
    raise SystemExit(75)
if scenario == "timeout" and role == transient_role:
    print(json.dumps({"type": "error", "message": "request timed out; try again"}))
    print("request timed out; try again", file=sys.stderr)
    raise SystemExit(75)
if scenario == "permanent_wording" and role == transient_role:
    print(json.dumps({"type": "error", "message": "temporary failure; try again"}))
    print("temporary failure; try again", file=sys.stderr)
    raise SystemExit(75)
if scenario == "throttled_once" and role == transient_role and count == 1:
    print(json.dumps({"type": "error", "message": "HTTP 429 rate limit exceeded"}))
    print("HTTP 429 rate limit exceeded", file=sys.stderr)
    raise SystemExit(75)
if scenario == "provider_5xx_once" and role == transient_role and count == 1:
    print(json.dumps({"type": "error", "message": "HTTP 503 service unavailable"}))
    print("HTTP 503 service unavailable", file=sys.stderr)
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
    spec = output_schema["properties"]["covered_claim_ids"]
    if "const" in spec:
        return spec["const"]
    members = spec.get("items", {}).get("enum")
    if isinstance(members, list) and spec.get("minItems") == len(members) \
            and spec.get("maxItems") == len(members):
        return members
    return fallback

def exact_string_array(spec, fallback):
    if "const" in spec:
        return spec["const"]
    members = spec.get("items", {}).get("enum")
    if isinstance(members, list) and spec.get("minItems") == len(members) \
            and spec.get("maxItems") == len(members):
        return members
    return fallback

def frozen_binding(relative: str):
    snapshot = workspace / runtime_value("runtime_snapshot")
    path = snapshot / relative
    return {
        "path": relative,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }

if role == "model_probe":
    response = {
        "status": "ok",
        "critic_contract": {
            "role": "critic_stats", "covered_claim_ids": ["probe-claim"],
            "evidence": {
                "artifact_refs": [{"path": "source/probe.txt", "sha256": "0" * 64}],
                "operation": "probe operation", "result": "probe result",
            },
        },
        "adjudicator_contract": {
            "role": "critic_synth", "covered_claim_ids": ["probe-claim"],
            "decision_question": "Is the evidence complete?",
        },
    }
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
                    # An uninventoried path: real in the snapshot, absent from the frozen
                    # inventory. This is exactly what killed round 2 of the second production
                    # audit, and it is how this scenario fails ONLY the adjudicator.
                    "artifact_refs": [
                        {"path": "source/EVIDENCE_HASHES.json", "sha256": "0" * 64}
                    ] if scenario == "adjudicator_invalid"
                    else [frozen_binding("source/adjudication_packet.json")],
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
    adjudication_required = (
        (scenario in ("adjudicate", "adjudicator_invalid")
         or (scenario == "tamper_evidence" and incomplete_role == "targeted_adjudicator"))
        and role in ("stats", "completeness")
    )
    items = []
    if adjudication_required:
        items.append({
            "item_id": f"{role}-F1",
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
    check_spec = output_schema["properties"]["coverage"]["properties"]["assigned_checks"]
    assigned_checks = exact_string_array(check_spec, [f"full {role} scope"])
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
            "assigned_checks": assigned_checks,
            "completed_checks": list(assigned_checks),
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
    if (scenario == "repairable" and role == incomplete_role
            and "I4R_OUTPUT_REPAIR" not in prompt):
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
        live = workspace / "source/REPORT_TO_REVIEW.md"
        response["items"][0]["evidence"]["artifact_refs"] = [{
            "path": "report/REPORT_final.md",
            "sha256": hashlib.sha256(live.read_bytes()).hexdigest(),
        }]
    if scenario == "wrong_evidence_hash" and role == incomplete_role and response["items"]:
        response["items"][0]["evidence"]["artifact_refs"][0]["sha256"] = "0" * 64
    if scenario == "nondeterministic":
        response["overall"]["summary"] += f" Provider call ordinal {count}."

output_path = pathlib.Path(output_path)
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text(json.dumps(response, indent=2, sort_keys=True) + "\n")

# The frozen snapshot changing DURING a call; both act on the -C workspace (the round or target
# snapshot) after the response is written.
#   tamper_evidence       a genuine content change: the snapshot's REPORT_TO_REVIEW.md is made
#                         writable and gains a line. It hits FAKE_INCOMPLETE_ROLE (default data);
#                         model_probe and targeted_adjudicator can be named too, and for the
#                         adjudicator the critics plant the same trigger as "adjudicate".
#   FAKE_SNAPSHOT_DRIFT   =<role>: the Dropbox File Provider shape seen on 2026-09-24 and
#                         2026-09-27, a mode-only drift: the snapshot's `source` directory is reset
#                         to 0700 seconds after the freeze while every byte stays unchanged.
if scenario == "tamper_evidence" and role == incomplete_role:
    frozen = workspace / "source/REPORT_TO_REVIEW.md"
    frozen.chmod(0o644)
    with frozen.open("a") as handle:
        handle.write("provider-side tamper\n")
if os.environ.get("FAKE_SNAPSHOT_DRIFT") == role:
    (workspace / "source").chmod(0o700)
#   FAKE_TAMPER_ROUND_FROM=<role>: from that role's call (the targeted adjudicator, whose workspace
#                         is the target snapshot) change one byte of the ROUND snapshot instead,
#                         which only the round projection re-checks: two snapshots are in play.
#   FAKE_SNAPSHOT_MODE_STORM=<role>: from this role's call on, the workspace's `source` directory
#                         keeps regaining a write bit (0700, content untouched), so a re-freeze
#                         never sticks. The call opens `source` once and names its workspace in
#                         $FAKE_CODEX_STATE/mode_storm.active; the test's sitecustomize hook
#                         (tests/test_268_snapshot_fix_a.py, MODE_STORM_SITECUSTOMIZE) re-opens it
#                         after every freeze of that snapshot. Deterministic: the busy-loop process
#                         this replaced (2.6.9) could lose the race to a re-freeze under load.
#   FAKE_CORRUPT_MANIFEST=<role>: damage a field of the workspace snapshot's MANIFEST.json that no
#                         launch can change (its content address), keeping every mode frozen.
if os.environ.get("FAKE_CORRUPT_MANIFEST") == role:
    manifest_path = workspace / "MANIFEST.json"
    manifest_obj = json.loads(manifest_path.read_text())
    address_key = "input_sha256" if "input_sha256" in manifest_obj else "target_input_sha256"
    manifest_obj[address_key] = "f" * 64
    manifest_path.chmod(0o644)
    manifest_path.write_text(json.dumps(manifest_obj, indent=2, sort_keys=True) + "\n")
    manifest_path.chmod(0o444)
if os.environ.get("FAKE_TAMPER_ROUND_FROM") == role:
    for snap in sorted((workspace.parent.parent / "snapshots").iterdir()):
        if snap.is_dir() and not snap.name.startswith("."):
            frozen = snap / "source/REPORT_TO_REVIEW.md"
            frozen.chmod(0o644)
            with frozen.open("a") as handle:
                handle.write("changed from another call\n")
            frozen.chmod(0o444)
if os.environ.get("FAKE_SNAPSHOT_MODE_STORM") == role:
    (state / "mode_storm.active").write_text(os.path.realpath(workspace) + "\n")
    os.chmod(workspace / "source", 0o700)
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
