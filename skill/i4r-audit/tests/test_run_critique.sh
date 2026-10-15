#!/bin/bash
# Deterministic end-to-end tests for the immutable critique runtime.
set -uo pipefail

TEST_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
ROOT=$(CDPATH= cd -- "$TEST_DIR/.." && pwd) || exit 2
DRIVER="$ROOT/scripts/run_critique.sh"
WAITER="$ROOT/scripts/wait_for_round.sh"
HELPER="$ROOT/scripts/test_run_critique_runtime.py"
MODEL_VERIFIER="$ROOT/scripts/verify_codex_model.sh"
FAKE="$TEST_DIR/fake_codex.sh"
# The critic route every fixture audit below freezes (make_audit runs record_invocation.py --init-policy, which
# snapshots the shipped policy): gpt-6.1-sol since 2.7.0, gpt-5.6-terra before. Read from the policy, never written
# here, so these assertions hold whichever route ships; tests/test_270_critic_route.py drives an audit frozen on
# the 2.6.10 route. The heredoc checks read the two exported names.
SHIPPED_CRITIC_MODEL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["models"]["critics"]["requested_model"])' \
  "$ROOT/config/pipeline-policy-v2.json") || exit 2
SHIPPED_ADJUDICATOR_MODEL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["models"]["targeted_adjudicator"]["requested_model"])' \
  "$ROOT/config/pipeline-policy-v2.json") || exit 2
case "$SHIPPED_CRITIC_MODEL:$SHIPPED_ADJUDICATOR_MODEL" in
  gpt-?*:gpt-?*) ;;
  *) echo "FAIL: the shipped critic route is not a Codex model: $SHIPPED_CRITIC_MODEL / $SHIPPED_ADJUDICATOR_MODEL" >&2; exit 2 ;;
esac
export SHIPPED_CRITIC_MODEL SHIPPED_ADJUDICATOR_MODEL
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/i4r-runtime-test.XXXXXX") || exit 2
cleanup() {
  if [ "${I4R_KEEP_TEST_TMP:-0}" = 1 ]; then
    echo "preserved test workspace: $TMP_ROOT" >&2
    return
  fi
  chmod -R u+w "$TMP_ROOT" 2>/dev/null || true
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT INT TERM HUP
bad=0

fail() {
  echo "FAIL: $*" >&2
  bad=$((bad + 1))
}

make_audit() {
  audit="$1"
  mkdir -p "$audit/report"
  printf '%s\n' 'Example et al. (2026), deterministic runtime fixture.' > "$audit/PAPER.txt"
  printf '%s\n' 'python3 standard-library-only fixture' > "$audit/ENV.txt"
  printf '%s\n' 'Do not run any long job.' > "$audit/SLOW.txt"
  printf '%s\n' 'Review the current changed claims and their dependencies.' > "$audit/WRAPUP.txt"
  printf '%s\n' '# Fixture report' '' 'The immutable audit draft contains one bounded claim.' \
    > "$audit/report/REPORT_final.md"
  mkdir -p "$audit/work"
  python3 - "$audit" <<'PYFINDING'
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]); report=root/"report/REPORT_final.md"
digest=hashlib.sha256(report.read_bytes()).hexdigest()
row={
 "schema_version":"2.0.0","id":"F1","claim":"The bounded fixture claim is correct.",
 "domain":"statistics","source_location":{"path":"report/REPORT_final.md","locator":"fixture claim"},
 "claim_ref":"fixture claim","status":"draft",
 "ledger_disposition":{"status":"INCLUDED","target":None,"reason":None},
 "coordinator_approved":False,"impact":"value_wrong","impact_rank":2,
 "verification_tier":2,"paper_only":False,"confidence":"LIKELY",
 "evidence":{"source_hashes":[{"path":"report/REPORT_final.md","sha256":digest}],
             "rerun_command":"sha256 report/REPORT_final.md","expected":"bound fixture",
             "observed":"bound fixture","tolerance_basis":None},
 "unverifiable_basis":None,"verifications":[],
 "dependencies":{"environment_fingerprint":None,"finding_ids":[],"artifact_paths":[]},
 "settled":None,"benign_explanations_ruled_out":[],"report_section":None,
 "verification_snippet":None,"disclosure_flag":False,
 "remedy":{"class":"one_number","description":"Retain the bounded value.","verified":None},
 "changes_conclusion":False,"changes_conclusion_which":None,
 "provenance":{"origin_role":"fixture","independently_found_by":["fixture"],"agreed_after_seeing":[],
               "round_introduced":0,"source_records":["report/REPORT_final.md"]}}
(root/"work/findings.jsonl").write_text(json.dumps(row,sort_keys=True)+"\n")
PYFINDING
  python3 "$ROOT/scripts/record_invocation.py" --init-policy "$audit" >/dev/null || {
    fail "could not initialize immutable policy snapshot for $audit"
    return 1
  }
}

count_calls() {
  path="$1"
  if [ -f "$path" ]; then sed -n '1p' "$path" | tr -d '[:space:]'
  else printf '%s\n' 0
  fi
}

run_driver() {
  audit="$1"; round="$2"; invocation="$3"; scenario="$4"; shift 4
  log="$TMP_ROOT/${invocation}.log"
  env AUDIT_DIR="$audit" CODEX="$FAKE" FAKE_CODEX_STATE="$audit/fake_state" \
    FAKE_CODEX_SCENARIO="$scenario" I4R_INVOCATION_ID="$invocation" \
    RETRY_BACKOFF_SECONDS=0 PROBE_LOCK_WAIT_SECONDS=3 "$@" \
    bash "$DRIVER" "$round" > "$log" 2>&1
}

show_log() {
  log="$1"
  [ ! -f "$log" ] || sed -n '1,240p' "$log" >&2
}

chmod +x "$DRIVER" "$WAITER" "$HELPER" "$MODEL_VERIFIER" "$FAKE"
bash -n "$DRIVER" || { fail "run_critique.sh syntax"; exit "$bad"; }
bash -n "$WAITER" || { fail "wait_for_round.sh syntax"; exit "$bad"; }
bash -n "$MODEL_VERIFIER" || { fail "verify_codex_model.sh syntax"; exit "$bad"; }
bash -n "$FAKE" || { fail "fake_codex.sh syntax"; exit "$bad"; }
PYTHONPYCACHEPREFIX="$TMP_ROOT/pycache" python3 -m py_compile "$HELPER" \
  || { fail "runtime helper syntax"; exit "$bad"; }
PYTHONPYCACHEPREFIX="$TMP_ROOT/pycache" python3 - "$HELPER" <<'PY' \
  || { fail "provider-schema linter self-test"; exit "$bad"; }
import copy, importlib.util, pathlib, sys

spec = importlib.util.spec_from_file_location("i4r_runtime", pathlib.Path(sys.argv[1]))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
good = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {
        "items": {"type": "array", "items": {"type": "string", "const": "ok"}},
    },
}
runtime.validate_provider_schema(good)
exact_array = runtime.json_const_schema(
    ["F2", "F3"], {"type": "array", "items": {"type": "string"}},
)
assert "const" not in exact_array
assert exact_array["minItems"] == exact_array["maxItems"] == 2
assert exact_array["items"] == {"type": "string", "enum": ["F2", "F3"]}
empty_array = runtime.json_const_schema(
    [], {"type": "array", "items": {"type": "string"}},
)
assert "const" not in empty_array and "enum" not in empty_array["items"]
assert empty_array["minItems"] == empty_array["maxItems"] == 0
bad_schemas = []
for mutation in (
    {"const": "ok"},
    {"enum": ["ok"]},
    {"type": "string", "minLength": 1},
    {"type": "string", "pattern": "^(?!/)"},
    {"type": "array", "const": ["F1"], "items": {"type": "string"}},
):
    candidate = copy.deepcopy(good)
    candidate["properties"]["items"]["items"] = mutation
    bad_schemas.append(candidate)
missing_required = copy.deepcopy(good)
missing_required["required"] = []
bad_schemas.append(missing_required)
for candidate in bad_schemas:
    try:
        runtime.validate_provider_schema(candidate)
    except ValueError:
        continue
    raise AssertionError(f"provider-schema linter accepted invalid nested schema: {candidate}")
assert runtime.provider_failure_classification("stream disconnected; try again") == (
    "transient", "transport")
assert runtime.provider_failure_classification("connection reset by peer") == (
    "transient", "transport")
assert runtime.provider_failure_classification("request timed out; try again") == (
    "permanent", "timeout")
assert runtime.provider_failure_classification("temporary failure; try again") == (
    "permanent", "permanent")
assert runtime.provider_failure_classification("HTTP 429 rate limit exceeded") == (
    "transient", "throttling")
assert runtime.provider_failure_classification("HTTP 503 service unavailable") == (
    "transient", "provider_5xx")
PY

echo "[metadata 2.1] unattended is the default: the generated plan is journaled and the round continues"
AUDIT_AUTO="$TMP_ROOT/audit_auto_authorize"
make_audit "$AUDIT_AUTO"
if ! bash "$ROOT/scripts/init_run.sh" "$AUDIT_AUTO" --audit-id audit-auto-21 \
    --protocol-version test --gate-set-version test >/dev/null 2>&1; then
  fail "could not initialize auto-authorize fixture"
elif ! run_driver "$AUDIT_AUTO" 1 auto-auth success; then
  fail "auto-authorized critique did not complete in one invocation"; show_log "$TMP_ROOT/auto-auth.log"
else
  grep -q "authorizing the generated round-1 plan unchanged" "$TMP_ROOT/auto-auth.log" \
    || fail "auto-authorize did not announce itself in the driver log"
  python3 - "$AUDIT_AUTO" <<'PY' || fail "auto-authorize lifecycle contract"
import json,pathlib,sys
root=pathlib.Path(sys.argv[1]); run=json.loads((root/"run.json").read_text())
round_one=next(row for row in run["rounds"] if row["round"] == 1)
assert round_one["state"] == "integrated", round_one["state"]
events=[json.loads(l) for l in (root/"run.events.jsonl").read_text().splitlines() if l.strip()]
assert sum(1 for e in events if e["type"] == "round_authorized") == 1, "exactly one journaled grant"
assert (root/"critique/round_1/ROUND_AUTHORIZATION_TEMPLATE.json").is_file(), "template retained"
# The property the attended pause used to protect, and the only one that matters once the pause
# is gone: NO provider is dispatched before a grant is on the journal. Removing a human pause is
# allowed; dispatching against an unauthorized plan is not.
seq_grant=next(i for i, e in enumerate(events) if e["type"] == "round_authorized")
dispatches=[i for i, e in enumerate(events) if e["type"] in ("provider_request", "inference_start")]
assert dispatches, "fixture made no provider call, so the ordering claim is vacuous"
assert seq_grant < min(dispatches), (
    f"a provider was dispatched at event {min(dispatches)} before the grant at {seq_grant}")
rows=[json.loads(line) for line in (root/"invocations.jsonl").read_text().splitlines()]
assert len(rows) == 5 and all(row["provider_call"] for row in rows)
PY
fi
# I4R_AUTO_AUTHORIZE=0 restores the attended pause; the next block proves it on a fresh audit.

echo "[metadata 2.1] I4R_AUTO_AUTHORIZE=0 pauses before dispatch; authorized run journals exact lifecycle"
AUDIT_AUTH="$TMP_ROOT/audit_authorization"
make_audit "$AUDIT_AUTH"
if ! bash "$ROOT/scripts/init_run.sh" "$AUDIT_AUTH" --audit-id audit-auth-21 \
    --protocol-version test --gate-set-version test >/dev/null 2>&1; then
  fail "could not initialize metadata-2.1 critique fixture"
else
  if run_driver "$AUDIT_AUTH" 1 auth-paused success I4R_AUTO_AUTHORIZE=0; then
    fail "metadata-2.1 critique ran without exact round authorization"
  else
    auth_rc=$?
    [ "$auth_rc" -eq 3 ] || fail "missing authorization returned $auth_rc instead of paused exit 3"
  fi
  [ "$(count_calls "$AUDIT_AUTH/fake_state/calls_total")" = 0 ] \
    || fail "missing authorization reached the provider"
  python3 - "$AUDIT_AUTH" <<'PY' || fail "authorization pause projection contract"
import json,pathlib,sys
root=pathlib.Path(sys.argv[1]); run=json.loads((root/"run.json").read_text())
assert run["schema_version"] == "2.1.0"
assert (run["status"],run["pause_reason"]) == ("paused","awaiting_authorization")
round_one=next(row for row in run["rounds"] if row["round"] == 1)
assert round_one["state"] == "awaiting_authorization" and round_one["started_at"] is None
assert not (root/"invocations.jsonl").read_text().strip()
template=root/"critique/round_1/ROUND_AUTHORIZATION_TEMPLATE.json"
assert template.is_file()
PY
  if ! python3 "$ROOT/scripts/lifecycle_journal.py" authorize-round "$AUDIT_AUTH" 1 \
      "$AUDIT_AUTH/critique/round_1/ROUND_AUTHORIZATION_TEMPLATE.json" >/dev/null; then
    fail "exact critique authorization could not be recorded"
  elif ! run_driver "$AUDIT_AUTH" 1 auth-complete success; then
    fail "authorized metadata-2.1 critique did not complete"; show_log "$TMP_ROOT/auth-complete.log"
  else
    python3 - "$AUDIT_AUTH" <<'PY' || fail "metadata-2.1 terminal lifecycle contract"
import json,pathlib,sys
root=pathlib.Path(sys.argv[1]); run=json.loads((root/"run.json").read_text())
round_one=next(row for row in run["rounds"] if row["round"] == 1)
assert round_one["state"] == "integrated"
rows=[json.loads(line) for line in (root/"invocations.jsonl").read_text().splitlines()]
assert len(rows) == 5 and all(row["provider_call"] for row in rows)
assert all(row["provider_request_status"] == "accepted" for row in rows)
assert all(row["inference_start_status"] == "started" for row in rows)
assert all(row["served_model"] is None for row in rows)
assert all(row["identity_assurance"] == "requested_only" for row in rows)
assert all(row["turn_count"] == 1 for row in rows)
assert {row["attempt_kind"] for row in rows} == {"capability_probe","role_inference"}
events=run["events"]
assert any(event["type"] == "round_quorum_complete" for event in events)
assert any(event["type"] == "round_integrated" for event in events)
PY
    if ! run_driver "$AUDIT_AUTH" 1 auth-cache success; then
      fail "metadata-2.1 exact-input cache replay failed"; show_log "$TMP_ROOT/auth-cache.log"
    else
      [ "$(count_calls "$AUDIT_AUTH/fake_state/calls_total")" = 5 ] \
        || fail "metadata-2.1 cache replay made a duplicate provider call"
      python3 - "$AUDIT_AUTH" <<'PY' || fail "metadata-2.1 cache provenance contract"
import json,pathlib,sys
root=pathlib.Path(sys.argv[1])
rows=[json.loads(line) for line in (root/"invocations.jsonl").read_text().splitlines()]
assert len(rows) == 10
sources={row["invocation_id"] for row in rows[:5]}
for row in rows[5:]:
    assert row["attempt_kind"] == "cache_replay"
    assert row["attempt"] is None and not row["provider_call"]
    assert row["source_invocation_id"] in sources
    assert row["provider_request_status"] == "not_requested"
    assert row["inference_start_status"] == "not_started"
PY
    fi
  fi
fi

echo "[metadata 2.1] rejected schema admissions stay attemptless and stop before a third request"
AUDIT_ADMISSION="$TMP_ROOT/audit_schema_admission"
make_audit "$AUDIT_ADMISSION"
if ! bash "$ROOT/scripts/init_run.sh" "$AUDIT_ADMISSION" --audit-id audit-admission-21 \
    --protocol-version test --gate-set-version test >/dev/null 2>&1; then
  fail "could not initialize metadata-2.1 schema-admission fixture"
else
  run_driver "$AUDIT_ADMISSION" 1 admission-paused success I4R_AUTO_AUTHORIZE=0
  admission_pause_rc=$?
  [ "$admission_pause_rc" -eq 3 ] || fail "schema-admission fixture did not pause for authorization"
  if ! python3 "$ROOT/scripts/lifecycle_journal.py" authorize-round "$AUDIT_ADMISSION" 1 \
      "$AUDIT_ADMISSION/critique/round_1/ROUND_AUTHORIZATION_TEMPLATE.json" >/dev/null; then
    fail "schema-admission fixture authorization failed"
  else
    for admission_invocation in admission-one admission-two; do
      if run_driver "$AUDIT_ADMISSION" 1 "$admission_invocation" probe_preinference_reject; then
        fail "$admission_invocation unexpectedly passed a rejected provider schema"
      fi
    done
    admission_calls=$(count_calls "$AUDIT_ADMISSION/fake_state/calls_model_probe")
    [ "$admission_calls" = 2 ] || fail "two rejected admissions made $admission_calls provider requests"
    if run_driver "$AUDIT_ADMISSION" 1 admission-three probe_preinference_reject; then
      fail "third rejected schema admission unexpectedly reached completion"
    fi
    [ "$(count_calls "$AUDIT_ADMISSION/fake_state/calls_model_probe")" = "$admission_calls" ] \
      || fail "third schema admission reached the provider"
    python3 - "$AUDIT_ADMISSION" <<'PY' || fail "metadata-2.1 schema-admission circuit contract"
import json,pathlib,sys
root=pathlib.Path(sys.argv[1])
rows=[json.loads(line) for line in (root/"invocations.jsonl").read_text().splitlines()]
probes=[row for row in rows if row["role"] == "model_probe" and row["provider_call"]]
assert len(probes) == 2
assert all(row["attempt_kind"] == "schema_admission" for row in probes)
assert all(row["attempt"] is None for row in probes)
assert all(row["provider_request_status"] == "rejected" for row in probes)
assert all(row["inference_start_status"] == "not_started" for row in probes)
assert len({row.get("schema_digest") for row in probes}) == 1
assert all(isinstance(row.get("schema_digest"),str) and len(row["schema_digest"]) == 64
           for row in probes)
events=[json.loads(line) for line in (root/"run.events.jsonl").read_text().splitlines()]
reserved=[event["payload"] for event in events if event["type"] == "invocation_reserved"
          and event["payload"].get("role") == "model_probe"]
assert len(reserved) == 3
assert len({row.get("schema_digest") for row in reserved}) == 1
assert all(isinstance(row.get("schema_digest"),str) and len(row["schema_digest"]) == 64
           for row in reserved)
request_dirs=[]
for invocation in ("admission-one", "admission-two"):
    requests=root/f"critique/round_1/invocations/{invocation}/probe/requests"
    found=sorted(path for path in requests.iterdir() if path.is_dir())
    assert len(found) == 1
    request=found[0]
    manifest=json.loads((request/"REQUEST_MANIFEST.json").read_text())
    assert manifest["manifest_version"] == "i4r-provider-request-1"
    assert manifest["attempt_kind"] == "schema_admission"
    assert manifest["request_id"] == request.name
    assert manifest["invocation_id"].startswith(f"{invocation}:model_probe:")
    actual={path.relative_to(request).as_posix():path for path in request.rglob("*")
            if path.is_file() and path.name != "REQUEST_MANIFEST.json"}
    assert set(actual) == {row["path"] for row in manifest["artifacts"]}
    for artifact in manifest["artifacts"]:
        path=actual[artifact["path"]]
        assert path.stat().st_size == artifact["bytes"]
        assert __import__("hashlib").sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    for path in (request,*request.rglob("*")):
        assert not (path.stat().st_mode & 0o222)
    request_dirs.append(request)
assert request_dirs[0] != request_dirs[1]
assert request_dirs[0].name != request_dirs[1].name or request_dirs[0].parent != request_dirs[1].parent
assert not (root/"critique/round_1/invocations/admission-three/probe/requests").exists()
PY
  fi
fi

echo "[1/10] success, exact quorum, immutable snapshot, and default zero adjudicator calls"
AUDIT_SUCCESS="$TMP_ROOT/audit_success"
make_audit "$AUDIT_SUCCESS"
printf '%s\n' 'ROUND_ONE_MUST_NOT_INJECT_THIS_SETTLED_FACT' > "$AUDIT_SUCCESS/SETTLED.md"
if ! run_driver "$AUDIT_SUCCESS" 1 success-one success; then
  fail "successful round 1 returned nonzero"; show_log "$TMP_ROOT/success-one.log"
else
  python3 - "$AUDIT_SUCCESS/critique/round_1/invocations/success-one/ROUND_STATUS.json" \
    "$AUDIT_SUCCESS/critique/round_1" "$ROOT" <<'PY' \
    || fail "round 1 status/snapshot contract"
import hashlib, json, os, pathlib, stat, sys
critic_model = os.environ["SHIPPED_CRITIC_MODEL"]
status = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert status["outcome"] == "complete" and status["quorum_met"] is True
assert status["expected_roles"] == ["stats", "data", "fairness", "completeness"]
assert status["completed_roles"] == status["expected_roles"]
assert status["provider_calls"] == 5
root = pathlib.Path(sys.argv[2])
audit = pathlib.Path(sys.argv[1]).parents[4]
ledger = {row["invocation_id"]: row for row in
          map(json.loads, (audit / "invocations.jsonl").read_text().splitlines())}
snapshots = [p for p in (root / "snapshots").iterdir() if p.is_dir() and not p.name.startswith(".")]
assert len(snapshots) == 1
manifest = json.loads((snapshots[0] / "MANIFEST.json").read_text())
assert manifest["input_sha256"] == snapshots[0].name
assert manifest["model"] == critic_model and manifest["effort"] == "high"
frozen = json.loads((audit / "pipeline-policy.snapshot.json").read_text())["policy"]
assert frozen["models"]["critics"]["requested_model"] == critic_model, "the fixture audit froze the shipped route"
assert manifest["provider_schema_generation_version"] == "i4r-openai-structured-output-4"
assert manifest["runtime_helper_sha256"] == hashlib.sha256(
    (pathlib.Path(sys.argv[3]) / "scripts/test_run_critique_runtime.py").read_bytes()).hexdigest()
assert "ROUND_ONE_MUST_NOT_INJECT_THIS_SETTLED_FACT" not in (
    snapshots[0] / "source/context/SETTLED.md").read_text()
assert (snapshots[0] / "source/context/PIPELINE_POLICY.json").read_bytes() == (
    pathlib.Path(sys.argv[1]).parents[4] / "pipeline-policy.snapshot.json").read_bytes()
evidence_inventory = json.loads(
    (snapshots[0] / "source/context/EVIDENCE_HASHES.json").read_text())
assert evidence_inventory["source/REPORT_TO_REVIEW.md"]["sha256"] == manifest["report_sha256"]
for rel, expected in manifest["files_sha256"].items():
    path = snapshots[0] / rel
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    assert not (path.stat().st_mode & stat.S_IWUSR)
def content_hash(tree, identity):
    digest = hashlib.sha256()
    for path in sorted(path for path in tree.rglob("*") if path.is_file()):
        digest.update(path.relative_to(tree).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    for value in identity:
        digest.update(value.encode() + b"\0")
    return digest.hexdigest()
identity = [
    "runtime-contract-6", manifest["provider_schema_generation_version"],
    str(manifest["round"]), manifest["model"], manifest["effort"],
    " ".join(manifest["roles"]), manifest["audit_id"],
    manifest["canonical_critic_schema_sha256"],
]
assert content_hash(snapshots[0] / "source", identity) == manifest["input_sha256"]
# The runtime, helper and validator hashes are telemetry in the manifest but no longer part of
# the input identity: a bug fix to the driver must not orphan an authorized round or its caches.
for telemetry_key in ("runtime_script_sha256", "runtime_helper_sha256",
                      "schema_validator_sha256", "semantic_validator_sha256"):
    assert manifest[telemetry_key]
legacy_identity = ["runtime-contract-5"] + identity[1:7] + [
    manifest["runtime_script_sha256"], manifest["runtime_helper_sha256"],
    manifest["canonical_critic_schema_sha256"], manifest["schema_validator_sha256"],
    manifest["semantic_validator_sha256"],
]
assert content_hash(snapshots[0] / "source", legacy_identity) != manifest["input_sha256"]
for schema_path in (snapshots[0] / "schemas").glob("*.schema.json"):
    schema = json.loads(schema_path.read_text())
    def json_type(value):
        if value is None: return "null"
        if isinstance(value, bool): return "boolean"
        if isinstance(value, str): return "string"
        if isinstance(value, int): return "integer"
        if isinstance(value, float): return "number"
        if isinstance(value, list): return "array"
        if isinstance(value, dict): return "object"
        raise AssertionError(f"unsupported const value type: {type(value).__name__}")
    def assert_typed_consts(value, path="#"):
        if isinstance(value, dict):
            if "const" in value:
                expected = json_type(value["const"])
                declared = value.get("type")
                choices = declared if isinstance(declared, list) else [declared]
                assert expected in choices, (
                    f"{schema_path}: {path} const has type {declared!r}, expected {expected!r}")
            for key, child in value.items():
                assert_typed_consts(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                assert_typed_consts(child, f"{path}/{index}")
    assert_typed_consts(schema)
    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)
    assert not ({"uniqueItems", "minLength", "maxLength"} & set(keys(schema)))
    def patterns(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "pattern": yield child
                yield from patterns(child)
        elif isinstance(value, list):
            for child in value:
                yield from patterns(child)
    assert not any(any(marker in pattern for marker in ("(?=", "(?!", "(?<=", "(?<!"))
                   for pattern in patterns(schema))
    def assert_typed_enums(value, path="#"):
        if isinstance(value, dict):
            if "enum" in value:
                assert "type" in value, f"{schema_path}: {path} enum lacks explicit type"
            for key, child in value.items():
                assert_typed_enums(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                assert_typed_enums(child, f"{path}/{index}")
    assert_typed_enums(schema)
    if schema_path.name != "probe.schema.json":
        assert not ({"allOf", "oneOf", "if", "then", "else"} & set(keys(schema)))
        completed = schema["properties"]["coverage"]["properties"]["completed_checks"]
        if "maxItems" in completed:
            assert completed["minItems"] == completed["maxItems"] == len(completed["items"]["enum"])
        assert completed["minItems"] >= 1
    def assert_no_composite_consts(value, path="#"):
        if isinstance(value, dict):
            if "const" in value:
                assert not isinstance(value["const"], (list, dict)), (
                    f"{schema_path}: {path} uses a provider-incompatible composite const")
            for key, child in value.items():
                assert_no_composite_consts(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                assert_no_composite_consts(child, f"{path}/{index}")
    assert_no_composite_consts(schema)
assert (root / "adjudication_packet.json").is_file()
assert (root / "CURRENT_PROJECTION").read_text().strip() == "success-one"
projection = root / "projections/success-one"
assert projection.is_dir() and (projection / "MANIFEST.json").is_file()
assert not (projection.stat().st_mode & stat.S_IWUSR)
assert not (root / "CRITIQUE.md").exists()
for role in status["expected_roles"]:
    assert (root / f"critic_{role}.md").read_text().rstrip().endswith("<!-- I4R_CRITIC_COMPLETE -->")
    obj = json.loads((root / f"critic_{role}.json").read_text())
    invocation = ledger[obj["invocation_id"]]
    assert invocation["provider_call"] is True and invocation["status"] == "completed"
    assert invocation["role"] == f"critic_{role}"
    assert invocation["requested_model"] == critic_model
    assert invocation["reasoning_effort"] == "high"
    assert invocation["output_sha256"] == hashlib.sha256(
        (root / f"critic_{role}.json").read_bytes()).hexdigest()
    contract = obj["input_hashes"]
    assert contract["snapshot_sha256"] == manifest["input_sha256"]
    assert contract["report_sha256"] == manifest["report_sha256"]
    assert contract["prompt_sha256"] == hashlib.sha256(
        (snapshots[0] / f"rendered/prompt_{role}.txt").read_bytes()).hexdigest()
    assert contract["policy_sha256"] == hashlib.sha256(
        (snapshots[0] / "source/context/PIPELINE_POLICY.json").read_bytes()).hexdigest()
    assert contract["evidence_manifest_sha256"] == hashlib.sha256(
        (snapshots[0] / "source/context/EVIDENCE_HASHES.json").read_bytes()).hexdigest()
    for item in obj["items"] + obj["new_issues"]:
        for binding in item["evidence"]["artifact_refs"]:
            assert set(binding) == {"path", "sha256"}
            artifact = snapshots[0] / binding["path"]
            assert binding["path"] in evidence_inventory
            assert hashlib.sha256(artifact.read_bytes()).hexdigest() == binding["sha256"]
assert (projection / "CLAIM_MERGE.json").is_file()
provider_rows = [row for row in ledger.values() if row["provider_call"]]
assert len(provider_rows) == 5
assert all(row["requested_model"] == critic_model for row in provider_rows)
assert all(row["reasoning_effort"] == "high" for row in provider_rows)
PY
fi
[ "$(count_calls "$AUDIT_SUCCESS/fake_state/calls_total")" = 5 ] \
  || fail "round 1 should make one probe plus four critic calls"
[ ! -f "$AUDIT_SUCCESS/fake_state/calls_targeted_adjudicator" ] \
  || fail "default mode unexpectedly called a model adjudicator"
for ordinary_role in model_probe stats data fairness completeness; do
  [ "$(sort -u "$AUDIT_SUCCESS/fake_state/efforts_$ordinary_role" 2>/dev/null)" = high ] \
    || fail "$ordinary_role was not actually invoked with effort=high"
done
"$MODEL_VERIFIER" "$AUDIT_SUCCESS" critic_stats "$SHIPPED_CRITIC_MODEL" high >/dev/null \
  || fail "exact observed ordinary model/effort verification failed"
for ordinary_role in model_probe stats data fairness completeness; do
  [ "$(sort -u "$AUDIT_SUCCESS/fake_state/models_$ordinary_role" 2>/dev/null)" = "$SHIPPED_CRITIC_MODEL" ] \
    || fail "$ordinary_role was not actually invoked with -m $SHIPPED_CRITIC_MODEL"
done
if run_driver "$AUDIT_SUCCESS" 1 reject-critic-max success CRITIC_EFFORT=max; then
  fail "ordinary critic runtime accepted non-policy effort=max"
fi
if run_driver "$AUDIT_SUCCESS" 1 reject-adjudicator-high success ADJUDICATOR_EFFORT=high; then
  fail "targeted-adjudicator runtime accepted non-policy effort=high"
fi
[ "$(count_calls "$AUDIT_SUCCESS/fake_state/calls_total")" = 5 ] \
  || fail "rejected effort overrides reached the provider"

echo "[probe retry] a pre-inference HTTP 400 resumes at attempt 2 and the probe budget cannot reset"
AUDIT_PROBE_RETRY="$TMP_ROOT/audit_probe_retry"
make_audit "$AUDIT_PROBE_RETRY"
if run_driver "$AUDIT_PROBE_RETRY" 1 probe-retry-one probe_preinference_reject; then
  fail "pre-inference probe rejection unexpectedly completed"
fi
[ "$(count_calls "$AUDIT_PROBE_RETRY/fake_state/calls_model_probe")" = 1 ] \
  || fail "pre-inference rejection did not make exactly one truthful provider request"
if ! run_driver "$AUDIT_PROBE_RETRY" 1 probe-retry-two success; then
  fail "corrected model probe did not resume successfully"; show_log "$TMP_ROOT/probe-retry-two.log"
else
  python3 - "$AUDIT_PROBE_RETRY/invocations.jsonl" \
    "$AUDIT_PROBE_RETRY/critique/round_1/invocations/probe-retry-two/ROUND_STATUS.json" <<'PY' \
    || fail "model-probe resume telemetry contract"
import hashlib, json, pathlib, sys
rows = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
probes = [row for row in rows if row["role"] == "model_probe" and row["provider_call"]]
assert [(row["attempt"], row["status"]) for row in probes] == [
    (1, "failed"), (2, "completed")]
assert probes[0]["provider_call"] is True
assert probes[0]["observed_model"] is None and probes[0]["model_source"] == "unavailable"
assert probes[0]["error_class"] == "deterministic_schema"
assert probes[1]["cache_usage"] == "probe_cache"
status = json.loads(pathlib.Path(sys.argv[2]).read_text())
assert status["outcome"] == "complete" and status["provider_calls"] == 5
PY
fi
[ "$(count_calls "$AUDIT_PROBE_RETRY/fake_state/calls_model_probe")" = 2 ] \
  || fail "corrected probe did not use exactly the contiguous second provider attempt"

AUDIT_PROBE_EXHAUSTED="$TMP_ROOT/audit_probe_exhausted"
make_audit "$AUDIT_PROBE_EXHAUSTED"
for probe_invocation in probe-budget-one probe-budget-two probe-budget-three; do
  if run_driver "$AUDIT_PROBE_EXHAUSTED" 1 "$probe_invocation" probe_preinference_reject; then
    fail "$probe_invocation unexpectedly completed"
  fi
done
probe_calls_before_exhausted_resume=$(count_calls "$AUDIT_PROBE_EXHAUSTED/fake_state/calls_total")
if run_driver "$AUDIT_PROBE_EXHAUSTED" 1 probe-budget-four probe_preinference_reject; then
  fail "a fourth model-probe provider attempt escaped the three-attempt cap"
fi
[ "$(count_calls "$AUDIT_PROBE_EXHAUSTED/fake_state/calls_total")" = "$probe_calls_before_exhausted_resume" ] \
  || fail "resuming the model probe reset its three-attempt budget"
python3 - "$AUDIT_PROBE_EXHAUSTED/invocations.jsonl" \
  "$AUDIT_PROBE_EXHAUSTED/critique/round_1/invocations/probe-budget-four/ROUND_STATUS.json" <<'PY' \
  || fail "model-probe exhaustion telemetry contract"
import json, pathlib, sys
rows = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
probes = [row for row in rows if row["role"] == "model_probe" and row["provider_call"]]
assert [row["attempt"] for row in probes] == [1, 2, 3]
assert all(row["status"] == "failed" for row in probes)
status = json.loads(pathlib.Path(sys.argv[2]).read_text())
assert status["outcome"] == "failed" and status["provider_calls"] == 0
assert "exhausted" in status["detail"]
PY

echo "[2/10] exact-input rerun uses the same snapshot, cached probe, and cached critic outputs"
if ! run_driver "$AUDIT_SUCCESS" 1 success-two success; then
  fail "cached round 1 rerun returned nonzero"; show_log "$TMP_ROOT/success-two.log"
else
  python3 - "$AUDIT_SUCCESS/critique/round_1/invocations/success-two/ROUND_STATUS.json" \
    "$AUDIT_SUCCESS/critique/round_1" <<'PY' || fail "cache reuse status contract"
import json, pathlib, sys
status = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert status["outcome"] == "complete" and status["provider_calls"] == 0
assert status["cache_hits"] == 5
snapshots = [p for p in (pathlib.Path(sys.argv[2]) / "snapshots").iterdir()
             if p.is_dir() and not p.name.startswith(".")]
assert len(snapshots) == 1
PY
fi
[ "$(count_calls "$AUDIT_SUCCESS/fake_state/calls_total")" = 5 ] \
  || fail "exact-input rerun made redundant provider calls"
[ "$(find "$AUDIT_SUCCESS/critique/round_1/invocations" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" = 2 ] \
  || fail "reruns did not retain unique invocation directories"
SNAPSHOT_DIR=$(find "$AUDIT_SUCCESS/critique/round_1/snapshots" -mindepth 1 -maxdepth 1 -type d | sed -n '1p')
chmod u+w "$SNAPSHOT_DIR/source/REPORT_TO_REVIEW.md"
printf '%s\n' 'tamper' >> "$SNAPSHOT_DIR/source/REPORT_TO_REVIEW.md"
if run_driver "$AUDIT_SUCCESS" 1 tamper-three success; then
  fail "tampered immutable snapshot was silently reused"
fi
[ "$(count_calls "$AUDIT_SUCCESS/fake_state/calls_total")" = 5 ] \
  || fail "snapshot-integrity failure reached the provider"
# Undo the tamper: later sections compare their before-report with what round 1 reviewed.
python3 - "$SNAPSHOT_DIR/source/REPORT_TO_REVIEW.md" <<'PY' || fail "could not undo the snapshot tamper"
import pathlib, sys
path = pathlib.Path(sys.argv[1])
data = path.read_bytes()
assert data.endswith(b"tamper\n"), "tamper line not found"
with open(path, "r+b") as handle:   # in place: the snapshot directory itself is read-only
    handle.truncate(len(data) - len(b"tamper\n"))
PY
chmod a-w "$SNAPSHOT_DIR/source/REPORT_TO_REVIEW.md"
cmp -s "$SNAPSHOT_DIR/source/REPORT_TO_REVIEW.md" "$AUDIT_SUCCESS/report/REPORT_final.md" \
  || fail "round 1's snapshot does not match report/REPORT_final.md after the tamper was undone"

echo "[3/10] round-2 diff routing runs one best specialist plus completeness"
MANIFEST="$AUDIT_SUCCESS/critique/DIFF_MANIFEST_round_2.json"
mkdir -p "$AUDIT_SUCCESS/work"
python3 - "$ROOT/schemas/fixtures/v2_finding_row.json" "$AUDIT_SUCCESS" <<'PY' \
  || fail "could not prepare deterministic round-2 diff inputs"
import copy, hashlib, json, pathlib, sys
fixture = json.loads(pathlib.Path(sys.argv[1]).read_text())
root = pathlib.Path(sys.argv[2])
evidence = root / "work/check_primary.txt"
evidence.write_text("bounded evidence fixture\n")
before = copy.deepcopy(fixture); after = copy.deepcopy(fixture)
for row in (before, after):
    row["id"] = "F2"; row["domain"] = "data"
    row["evidence"]["source_hashes"][0]["sha256"] = hashlib.sha256(evidence.read_bytes()).hexdigest()
before["claim"] = "The data manifest has an older bounded description."
after["claim"] = "The data manifest has a corrected bounded description."
(root / "work/findings_before_round_2.jsonl").write_text(json.dumps(before, sort_keys=True) + "\n")
# round 1 counts as integrated: integrate_round's backup is the registry round 1 reviewed (make_diff_packet checks
# the before-findings against it; with neither a backup nor an integration it checks round 1's snapshot instead)
(root / "work/findings.before_round1_integration.jsonl").write_text(json.dumps(before, sort_keys=True) + "\n")
(root / "work/findings.jsonl").write_text(json.dumps(after, sort_keys=True) + "\n")
# SKILL step 1: the before-report is a copy of REPORT_final.md taken before round 1's integration
(root / "work/report_before_round_2.md").write_bytes((root / "report/REPORT_final.md").read_bytes())
PY
python3 "$ROOT/scripts/make_diff_packet.py" "$AUDIT_SUCCESS" --round 2 \
  --before-report "$AUDIT_SUCCESS/work/report_before_round_2.md" \
  --after-report "$AUDIT_SUCCESS/report/REPORT_final.md" \
  --before-findings "$AUDIT_SUCCESS/work/findings_before_round_2.jsonl" \
  --after-findings "$AUDIT_SUCCESS/work/findings.jsonl" --output "$MANIFEST" >/dev/null \
  || fail "could not generate deterministic round-2 diff packet"
if ! run_driver "$AUDIT_SUCCESS" 2 route-two success "DIFF_MANIFEST=$MANIFEST"; then
  fail "routed round 2 returned nonzero"; show_log "$TMP_ROOT/route-two.log"
else
  python3 - "$AUDIT_SUCCESS/critique/round_2/invocations/route-two/ROUND_STATUS.json" \
    "$AUDIT_SUCCESS/critique/round_2/invocations/route-two/ROUTING.json" <<'PY' \
    || fail "round-2 role routing contract"
import json, os, pathlib, sys
status = json.loads(pathlib.Path(sys.argv[1]).read_text())
routing = json.loads(pathlib.Path(sys.argv[2]).read_text())
assert routing["roles"] == ["data", "completeness"]
assert routing["claim_assignment_mode"] == "domain_routed_delta"
assert routing["claim_assignments"] == {"data": ["F2"], "completeness": ["F2"]}
assert status["expected_roles"] == routing["roles"] and status["quorum_met"] is True
assert status["provider_calls"] == 2 and status["cache_hits"] == 1
snapshot = pathlib.Path(sys.argv[1]).parents[4] / status["snapshot"]
assert (snapshot / "source/context/DIFF_MANIFEST.json").is_file()
snapshot_manifest = json.loads((snapshot / "MANIFEST.json").read_text())
assert snapshot_manifest["model"] == os.environ["SHIPPED_CRITIC_MODEL"]
assert snapshot_manifest["effort"] == "high"
covered = json.loads((snapshot / "schemas/critic_data.schema.json").read_text())["properties"][
    "covered_claim_ids"]
assert "const" not in covered
assert covered["minItems"] == covered["maxItems"] == 1
assert covered["items"]["enum"] == ["F2"]
prompt = (snapshot / "rendered/prompt_data.txt").read_text()
assert "runtime_diff_manifest=source/context/DIFF_MANIFEST.json" in prompt
assert "Later-round delta scope (supersedes full-document review instructions above)" in prompt
settled = (snapshot / "source/context/SETTLED.md").read_text()
assert "ROUND_ONE_MUST_NOT_INJECT_THIS_SETTLED_FACT" not in settled
assert settled.rstrip().endswith("No settled facts are referenced by this delta review.")
round_root = pathlib.Path(sys.argv[1]).parents[2]
projection = round_root / "projections/route-two"
merge = json.loads((projection / "CLAIM_MERGE.json").read_text())
assert merge["merge_version"] == "i4r-claim-merge-1"
assert merge["roles"] == ["critic_data", "critic_completeness"]
assert len(merge["claims"]) == 1 and merge["claims"][0]["claim_id"] == "F2"
row = merge["claims"][0]
assert [result["role"] for result in row["role_results"]] == [
    "critic_data", "critic_completeness"]
assert row["verdicts"] == ["CONFIRMED"] and row["agreement"] is True
assert row["conflict"] is False and row["requested_corrections"] == []
for role in routing["roles"]:
    critic = json.loads((projection / f"critic_{role}.json").read_text())
    assert critic["covered_claim_ids"] == ["F2"]
    assert [item["finding_id"] for item in critic["items"]] == ["F2"]
manifest = json.loads((projection / "MANIFEST.json").read_text())
assert manifest["claim_assessments"] == {"data": ["F2"], "completeness": ["F2"]}
assert manifest["rereviewed_claim_ids"] == ["F2"]
PY
fi
[ "$(sort -u "$AUDIT_SUCCESS/fake_state/efforts_data")" = high ] \
  || fail "round-2 data critic was not actually invoked with effort=high"
[ "$(sort -u "$AUDIT_SUCCESS/fake_state/efforts_completeness")" = high ] \
  || fail "round-2 completeness critic was not actually invoked with effort=high"
[ "$(count_calls "$AUDIT_SUCCESS/fake_state/calls_total")" = 7 ] \
  || fail "routed round should reuse the probe and call exactly two critics"

NEGATIVE_RESULTS="$TMP_ROOT/negative_empty_results"
cp -R "$AUDIT_SUCCESS/critique/round_2/invocations/route-two/results" "$NEGATIVE_RESULTS"
python3 - "$NEGATIVE_RESULTS/critic_data.json" "$NEGATIVE_RESULTS/data.complete.json" <<'PY' \
  || fail "could not construct empty-assessment projection negative"
import hashlib, json, pathlib, sys
result_path, sidecar_path = map(pathlib.Path, sys.argv[1:])
result = json.loads(result_path.read_text())
result["items"] = []
result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
sidecar = json.loads(sidecar_path.read_text())
sidecar["output_sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n")
PY
route_status="$AUDIT_SUCCESS/critique/round_2/invocations/route-two/ROUND_STATUS.json"
route_input=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["input_sha256"])' "$route_status")
route_report=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["report_sha256"])' "$route_status")
route_audit=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["audit_id"])' "$route_status")
if python3 "$HELPER" projection --out "$AUDIT_SUCCESS/critique/round_2" \
    --results "$NEGATIVE_RESULTS" --invocation negative-empty --input-sha "$route_input" \
    --report-sha "$route_report" --audit-id "$route_audit" --round 2 \
    --roles 'data completeness' --adjudication skipped \
    --routing "$AUDIT_SUCCESS/critique/round_2/invocations/route-two/ROUTING.json" \
    --packet "$AUDIT_SUCCESS/critique/round_2/invocations/route-two/adjudication_packet.json" \
    >/dev/null 2>&1; then
  fail "projection accepted routed coverage with no claim-level assessment"
fi
rm -rf "$NEGATIVE_RESULTS"

MULTI_MANIFEST="$AUDIT_SUCCESS/critique/DIFF_MANIFEST_round_2_multi_critical.json"
MULTI_ROUTING="$AUDIT_SUCCESS/critique/ROUTING_round_2_multi_critical.json"
printf '%s\n' '{"round":2,"changed_claim_ids":["F2"],"changed_sections":["Section 4","Appendix A"],"routing_mode":"multi_domain_critical","critical_domains":["statistics","data"],"recommended_roles":["stats","data"]}' \
  > "$MULTI_MANIFEST"
if ! python3 "$HELPER" route --workspace "$AUDIT_SUCCESS" --round 2 --manifest "$MULTI_MANIFEST" --output "$MULTI_ROUTING"; then
  fail "multi-domain critical routing helper returned nonzero"
else
  python3 - "$MULTI_ROUTING" <<'PY' || fail "multi-domain critical specialist expansion contract"
import json, pathlib, sys
routing = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert routing["roles"] == ["stats", "data", "completeness"]
assert routing["reason"] == "multi-domain critical specialists recommended by diff manifest"
PY
fi

echo "[4/10] transient retry succeeds once and emits unique structured call telemetry"
AUDIT_TRANSIENT="$TMP_ROOT/audit_transient"
make_audit "$AUDIT_TRANSIENT"
if ! run_driver "$AUDIT_TRANSIENT" 1 transient-one transient_once; then
  fail "transient retry round returned nonzero"; show_log "$TMP_ROOT/transient-one.log"
else
  python3 - "$AUDIT_TRANSIENT/critique/round_1/invocations/transient-one/invocations.jsonl" \
    "$AUDIT_TRANSIENT/critique/round_1/invocations/transient-one/ROUND_STATUS.json" <<'PY' \
    || fail "transient retry/telemetry contract"
import json, os, pathlib, sys
rows = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
status = json.loads(pathlib.Path(sys.argv[2]).read_text())
stats = [row for row in rows if row["role"] == "stats"]
assert [(row["attempt"], row["failure_class"], row["outcome"]) for row in stats] == [
    (1, "transient", "failed"), (2, None, "complete")]
assert len({row["call_id"] for row in rows}) == len(rows)
assert all(row["requested_model"] == os.environ["SHIPPED_CRITIC_MODEL"] for row in rows)
assert all(row["requested_effort"] == "high" for row in rows)
completed = [row for row in rows if row["outcome"] == "complete"]
assert completed and all(row["usage"]["reasoning_tokens"] == 10 for row in completed)
assert all(row["usage"]["cache_write_input_tokens"] == 7 for row in completed)
assert status["provider_calls"] == 6 and status["outcome"] == "complete"
PY
fi
[ "$(count_calls "$AUDIT_TRANSIENT/fake_state/calls_stats")" = 2 ] \
  || fail "transient role did not use exactly two attempts"

echo "[typed retry] timeout and vague retry wording stop; 429 and provider 5xx retry"
AUDIT_TIMEOUT="$TMP_ROOT/audit_timeout"
make_audit "$AUDIT_TIMEOUT"
if run_driver "$AUDIT_TIMEOUT" 1 timeout-one timeout; then
  fail "bare timeout wording unexpectedly completed"
fi
[ "$(count_calls "$AUDIT_TIMEOUT/fake_state/calls_stats")" = 1 ] \
  || fail "bare timeout wording was retried"
python3 - "$AUDIT_TIMEOUT/critique/round_1/invocations/timeout-one/invocations.jsonl" \
    "$AUDIT_TIMEOUT/invocations.jsonl" <<'PY' \
  || fail "timeout failure classification contract"
import json,pathlib,sys
local=[json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
global_rows=[json.loads(line) for line in pathlib.Path(sys.argv[2]).read_text().splitlines()]
assert next(row for row in local if row["role"] == "stats")["failure_class"] == "permanent"
assert next(row for row in global_rows if row["role"] == "critic_stats")["error_class"] == "timeout"
PY

AUDIT_VAGUE="$TMP_ROOT/audit_vague_permanent"
make_audit "$AUDIT_VAGUE"
if run_driver "$AUDIT_VAGUE" 1 vague-one permanent_wording; then
  fail "vague retry wording unexpectedly completed"
fi
[ "$(count_calls "$AUDIT_VAGUE/fake_state/calls_stats")" = 1 ] \
  || fail "vague retry wording was retried"
python3 - "$AUDIT_VAGUE/critique/round_1/invocations/vague-one/invocations.jsonl" \
    "$AUDIT_VAGUE/invocations.jsonl" <<'PY' \
  || fail "vague permanent failure classification contract"
import json,pathlib,sys
local=[json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
global_rows=[json.loads(line) for line in pathlib.Path(sys.argv[2]).read_text().splitlines()]
assert next(row for row in local if row["role"] == "stats")["failure_class"] == "permanent"
assert next(row for row in global_rows if row["role"] == "critic_stats")["error_class"] == "permanent"
PY

AUDIT_5XX="$TMP_ROOT/audit_provider_5xx"
make_audit "$AUDIT_5XX"
if ! run_driver "$AUDIT_5XX" 1 provider-5xx-one provider_5xx_once; then
  fail "explicit provider 5xx did not retry to completion"; show_log "$TMP_ROOT/provider-5xx-one.log"
else
  [ "$(count_calls "$AUDIT_5XX/fake_state/calls_stats")" = 2 ] \
    || fail "explicit provider 5xx did not make exactly two stats calls"
  python3 - "$AUDIT_5XX/critique/round_1/invocations/provider-5xx-one/invocations.jsonl" \
      "$AUDIT_5XX/invocations.jsonl" <<'PY' \
    || fail "provider 5xx retry classification contract"
import json,pathlib,sys
local=[json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
global_rows=[json.loads(line) for line in pathlib.Path(sys.argv[2]).read_text().splitlines()]
stats=[row for row in local if row["role"] == "stats"]
assert [(row["attempt"],row["failure_class"],row["outcome"]) for row in stats] == [
    (1,"transient","failed"),(2,None,"complete")]
canonical=[row for row in global_rows if row["role"] == "critic_stats"]
assert [row["error_class"] for row in canonical] == ["provider_5xx",None]
PY
fi

echo "[metadata 2.1] a hung provider call is stopped by the wall-clock timeout, not left holding the round"
AUDIT_HANG="$TMP_ROOT/audit_provider_timeout"
make_audit "$AUDIT_HANG"
if ! bash "$ROOT/scripts/init_run.sh" "$AUDIT_HANG" --audit-id audit-timeout-21 \
    --protocol-version test --gate-set-version test >/dev/null 2>&1; then
  fail "could not initialize provider-timeout fixture"
else
  hang_started=$(date +%s)
  # The fake sleeps 30s for the stats role; the driver must give up at 6s and stop the child.
  run_driver "$AUDIT_HANG" 1 hang-one slow \
    I4R_PROVIDER_TIMEOUT_SECONDS=6 FAKE_SLOW_ROLE=stats RETRY_BACKOFF_SECONDS=0
  hang_elapsed=$(( $(date +%s) - hang_started ))
  grep -q "provider call exceeded 6s" "$TMP_ROOT/hang-one.log" \
    || { fail "provider timeout did not fire"; show_log "$TMP_ROOT/hang-one.log"; }
  # The 30s sleeper must have been killed, not merely abandoned: if the group were left running
  # it would finish and write slow_finished.
  [ ! -f "$AUDIT_HANG/fake_state/slow_finished" ] \
    || fail "timed-out provider was abandoned rather than stopped"
  # And the driver must not have waited out the full 30s sleep.
  [ "$hang_elapsed" -lt 28 ] \
    || fail "driver waited ${hang_elapsed}s: the timeout did not shorten the call"
  # A wall-clock kill leaves no classifiable text in the log, so before the timeout branch
  # existed the generic text classifier called it permanent: the round died on attempt 1 with
  # the whole retry budget unspent, which is worse than the unbounded wait the ceiling replaced.
  grep -q "critic_stats attempt 2" "$TMP_ROOT/hang-one.log" \
    || { fail "a timed-out attempt was not retried"; show_log "$TMP_ROOT/hang-one.log"; }
  [ ! -e "$AUDIT_HANG/critique/round_1/attempt_budget/stats.permanent" ] \
    || fail "a timed-out attempt was marked permanent"
  # ...and the retry is BOUNDED. A killed call never reaches inference_start, so the journal
  # counts it as nothing; when the budget read only the journal, this role re-dispatched attempt
  # 1 forever and the round never returned at all.
  grep -q "exhausted transient attempts" "$TMP_ROOT/hang-one.log" \
    || { fail "timed-out retries were not bounded by the attempt budget";
         show_log "$TMP_ROOT/hang-one.log"; }
  [ ! -d "$AUDIT_HANG/critique/round_1/invocations/hang-one/attempts/stats/4" ] \
    || fail "the attempt budget was exceeded"
  # Local telemetry carries the driver's own classification; the canonical ledger carries the
  # typed error class. The bare-"timeout"-wording test above reads the same two files the same
  # way -- provider-reported timeout wording stays permanent, our wall-clock kill is transient.
  python3 - "$AUDIT_HANG/critique/round_1/invocations/hang-one/invocations.jsonl" \
      "$AUDIT_HANG/invocations.jsonl" <<'PY' \
    || fail "timed-out attempt was not recorded as a transient timeout"
import json, pathlib, sys
local = [json.loads(l) for l in pathlib.Path(sys.argv[1]).read_text().splitlines() if l.strip()]
ledger = [json.loads(l) for l in pathlib.Path(sys.argv[2]).read_text().splitlines() if l.strip()]
stats = [r for r in local if r.get("role") == "stats"]
assert stats, "no local telemetry row for stats"
assert all(r["failure_class"] == "transient" for r in stats), [r["failure_class"] for r in stats]
rows = [r for r in ledger if r.get("role") == "critic_stats" and r.get("status") != "started"]
assert rows, "no terminal ledger row for critic_stats"
assert all(r["error_class"] == "timeout" for r in rows), [r["error_class"] for r in rows]
assert all(r["status"] == "failed" for r in rows), [r["status"] for r in rows]
PY
fi

AUDIT_429="$TMP_ROOT/audit_throttling_lifecycle"
make_audit "$AUDIT_429"
if ! bash "$ROOT/scripts/init_run.sh" "$AUDIT_429" --audit-id audit-throttling-21 \
    --protocol-version test --gate-set-version test >/dev/null 2>&1; then
  fail "could not initialize metadata-2.1 throttling fixture"
else
  run_driver "$AUDIT_429" 1 throttling-paused success I4R_AUTO_AUTHORIZE=0
  throttling_pause_rc=$?
  [ "$throttling_pause_rc" -eq 3 ] || fail "throttling fixture did not pause for authorization"
  if ! python3 "$ROOT/scripts/lifecycle_journal.py" authorize-round "$AUDIT_429" 1 \
      "$AUDIT_429/critique/round_1/ROUND_AUTHORIZATION_TEMPLATE.json" >/dev/null; then
    fail "throttling fixture authorization failed"
  elif ! run_driver "$AUDIT_429" 1 throttling-one throttled_once RETRY_BACKOFF_SECONDS=1; then
    fail "explicit HTTP 429 did not retry to completion"; show_log "$TMP_ROOT/throttling-one.log"
  else
    [ "$(count_calls "$AUDIT_429/fake_state/calls_stats")" = 2 ] \
      || fail "explicit HTTP 429 did not make exactly two stats calls"
    python3 - "$AUDIT_429" \
        "$AUDIT_429/critique/round_1/invocations/throttling-one/invocations.jsonl" <<'PY' \
      || fail "retry backoff lifecycle contract"
import json,pathlib,sys
root=pathlib.Path(sys.argv[1])
rows=[json.loads(line) for line in (root/"invocations.jsonl").read_text().splitlines()]
stats=[row for row in rows if row["role"] == "critic_stats"]
assert [(row["attempt"],row["error_class"],row["status"]) for row in stats] == [
    (1,"throttling","failed"),(2,None,"completed")]
local=[json.loads(line) for line in pathlib.Path(sys.argv[2]).read_text().splitlines()]
assert [(row["attempt"],row["failure_class"],row["outcome"])
        for row in local if row["role"] == "stats"] == [
    (1,"transient","failed"),(2,None,"complete")]
events=[json.loads(line) for line in (root/"run.events.jsonl").read_text().splitlines()]
backoff=[event for event in events if event["type"] in
         {"retry_backoff_start","retry_backoff_end"}]
assert [event["type"] for event in backoff] == ["retry_backoff_start","retry_backoff_end"]
start,end=backoff
assert start["payload"] == end["payload"]
payload=start["payload"]
assert payload["role"] == "critic_stats" and payload["attempt"] == 1
assert payload["error_class"] == "throttling" and payload["delay_seconds"] == 1
assert start["seq"] < end["seq"] and start["at"] <= end["at"]
run=json.loads((root/"run.json").read_text())
assert run["durations"]["retry_backoff_seconds"] >= 1
PY
  fi
fi

echo "[5/10] the three-attempt transient budget cannot reset across resume or changed input"
AUDIT_EXHAUSTED="$TMP_ROOT/audit_exhausted"
make_audit "$AUDIT_EXHAUSTED"
if run_driver "$AUDIT_EXHAUSTED" 1 exhausted-one transient_always; then
  fail "always-transient role unexpectedly completed"
fi
[ "$(count_calls "$AUDIT_EXHAUSTED/fake_state/calls_stats")" = 3 ] \
  || fail "always-transient role did not stop at exactly three total attempts"
exhausted_total=$(count_calls "$AUDIT_EXHAUSTED/fake_state/calls_total")
if run_driver "$AUDIT_EXHAUSTED" 1 exhausted-two transient_always; then
  fail "resumed exhausted round unexpectedly completed"
fi
[ "$(count_calls "$AUDIT_EXHAUSTED/fake_state/calls_total")" = "$exhausted_total" ] \
  || fail "resume reset the three-attempt transient budget"
printf '%s\n' 'A changed same-round report must not reset a role budget.' \
  >> "$AUDIT_EXHAUSTED/report/REPORT_final.md"
if run_driver "$AUDIT_EXHAUSTED" 1 exhausted-three transient_always; then
  fail "changed same-round input reset the exhausted role budget"
fi
[ "$(count_calls "$AUDIT_EXHAUSTED/fake_state/calls_stats")" = 3 ] \
  || fail "changed input allowed more than three round-wide stats attempts"

echo "[knob] MAX_TRIES above the policy default governs the helper's inference-start ceiling too"
# The first production audit lost two complete fourth-attempt reviews: the driver allowed attempt 4
# under MAX_TRIES=5 while the runtime helper still refused to record a fourth accepted inference
# start under its own default of 3. One knob must govern both.
AUDIT_KNOB="$TMP_ROOT/audit_knob"
make_audit "$AUDIT_KNOB"
if run_driver "$AUDIT_KNOB" 1 knob-one transient_always MAX_TRIES=4; then
  fail "always-transient role unexpectedly completed under MAX_TRIES=4"
fi
[ "$(count_calls "$AUDIT_KNOB/fake_state/calls_stats")" = 4 ] \
  || fail "MAX_TRIES=4 did not govern the attempt ceiling end to end (got $(count_calls "$AUDIT_KNOB/fake_state/calls_stats"))"
if grep -q "accepted inference starts exhausted" "$TMP_ROOT/knob-one.log"; then
  fail "the runtime helper's inference-start ceiling contradicted MAX_TRIES"
fi

echo "[6/10] permanent failures stop immediately; a later invocation retries once within budget"
AUDIT_PERMANENT="$TMP_ROOT/audit_permanent"
make_audit "$AUDIT_PERMANENT"
if run_driver "$AUDIT_PERMANENT" 1 permanent-one permanent; then
  fail "permanent provider failure was treated as success"
fi
[ "$(count_calls "$AUDIT_PERMANENT/fake_state/calls_data")" = 1 ] \
  || fail "permanent provider failure was retried"
# 2.5.1: the provider's own message reaches the terminal, not only the classification.
grep -q "permanent failure on attempt 1 (.*): invalid_request_error: model is not supported; see " "$TMP_ROOT/permanent-one.log" \
  || { fail "the provider's error message was not surfaced on the permanent-failure line"; show_log "$TMP_ROOT/permanent-one.log"; }
permanent_total=$(count_calls "$AUDIT_PERMANENT/fake_state/calls_total")
if run_driver "$AUDIT_PERMANENT" 1 permanent-two permanent; then
  fail "resumed permanently failed round was treated as success"
fi
# A permanent classification describes one provider response, not the role: a later invocation
# retries the role within the shared attempt budget (attempt 2 of 3 here) and the persistent
# ledger still counts every call. The fake fails permanently again, so the round still fails.
[ "$(count_calls "$AUDIT_PERMANENT/fake_state/calls_total")" = "$((permanent_total + 1))" ] \
  || fail "a later invocation did not retry the permanently failed role exactly once within budget (got $(count_calls "$AUDIT_PERMANENT/fake_state/calls_total"), expected $((permanent_total + 1)))"
[ "$(count_calls "$AUDIT_PERMANENT/fake_state/calls_data")" = 2 ] \
  || fail "the retried role did not spend exactly one more attempt"
grep -q "permanent-failure marker from an earlier invocation" "$TMP_ROOT/permanent-two.log" \
  || fail "the earlier permanent marker was not announced as a note"
python3 - "$AUDIT_PERMANENT/critique/round_1/invocations/permanent-two/ROUND_STATUS.json" <<'PY' \
  || fail "permanent failure status contract"
import json, pathlib, sys
status = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert status["outcome"] == "failed" and status["quorum_met"] is False
assert "data" in status["failed_roles"] and status["provider_calls"] == 1, status["provider_calls"]
PY

echo "[7/10] focused output repair is bounded; scientific/schema-invalid outputs fail closed"
AUDIT_REPAIRABLE="$TMP_ROOT/audit_repairable"
make_audit "$AUDIT_REPAIRABLE"
if ! run_driver "$AUDIT_REPAIRABLE" 1 repairable-one repairable; then
  fail "administrative-only output repair did not complete"; show_log "$TMP_ROOT/repairable-one.log"
else
  python3 - "$AUDIT_REPAIRABLE" <<'PY' || fail "focused repair preservation/budget contract"
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
receipt = next(root.glob("critique/round_1/invocations/repairable-one/attempts/data/*/repair_prepare.json"))
validation = next(root.glob("critique/round_1/invocations/repairable-one/attempts/data/*/repair_validation.json"))
prepared, checked = json.loads(receipt.read_text()), json.loads(validation.read_text())
assert prepared["repair_prompt_bytes"] <= prepared["maximum_repair_bytes"]
assert prepared["ratio"] <= 0.2
assert checked["scientific_fields_byte_equivalent"] is True
assert checked["scientific_field_sha256"] == prepared["scientific_field_sha256"]
rows = [json.loads(line) for line in (
    root / "critique/round_1/invocations/repairable-one/invocations.jsonl").read_text().splitlines()]
data = [row for row in rows if row["role"] == "data" and row["provider_call"]]
assert [(row["attempt"], row["outcome"]) for row in data] == [
    (1, "schema_invalid"), (2, "complete")]
PY
fi
[ "$(count_calls "$AUDIT_REPAIRABLE/fake_state/calls_data")" = 2 ] \
  || fail "focused repair did not use exactly one additional inference"

AUDIT_INCOMPLETE="$TMP_ROOT/audit_incomplete"
make_audit "$AUDIT_INCOMPLETE"
if run_driver "$AUDIT_INCOMPLETE" 1 incomplete-one incomplete; then
  fail "schema-incomplete response was treated as complete"
fi
# A schema-invalid review is re-sampled while budget remains: the initial attempt, one focused
# repair, then a fresh attempt, three inferences at MAX_TRIES=3. A deterministically incomplete
# fake therefore exhausts the budget instead of stopping after the repair, and no invalid output
# is ever promoted along the way.
[ "$(count_calls "$AUDIT_INCOMPLETE/fake_state/calls_data")" = 3 ] \
  || fail "schema-incomplete response did not spend its full retry budget (got $(count_calls "$AUDIT_INCOMPLETE/fake_state/calls_data"))"
python3 - "$AUDIT_INCOMPLETE/critique/round_1/invocations/incomplete-one/invocations.jsonl" <<'PY' \
  || fail "schema rejection telemetry contract"
import json, pathlib, sys
rows = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
data = [row for row in rows if row["role"] == "data"]
assert len(data) == 3, len(data)
assert all(row["outcome"] == "schema_invalid" for row in data), [row["outcome"] for row in data]
assert data[0]["failure_class"] == "repairable", data[0]["failure_class"]
assert not any(row["outcome"] == "complete" for row in data)
PY
[ ! -f "$AUDIT_INCOMPLETE/critique/round_1/critic_data.json" ] \
  || fail "an invalid review was published"

for edge_case in all_skipped bad_path empty_assessments live_only wrong_evidence_hash \
  missing_invocation wrong_input_hash missing_declaration; do
  edge_audit="$TMP_ROOT/audit_${edge_case}"
  make_audit "$edge_audit"
  if run_driver "$edge_audit" 1 "${edge_case}-one" "$edge_case"; then
    fail "$edge_case canonical-invalid response was treated as complete"
  fi
  # A canonical-invalid review is re-sampled within MAX_TRIES (three inferences here) and is
  # never published. The focused repair stays zero-or-one per role however many samples run:
  # administrative defects earn exactly one repair, scientific-surface defects earn none.
  case "$edge_case" in
    all_skipped|missing_invocation|wrong_input_hash|missing_declaration) expected_edge_repair=1 ;;
    *) expected_edge_repair=0 ;;
  esac
  [ "$(count_calls "$edge_audit/fake_state/calls_data")" = 3 ] \
    || fail "$edge_case did not spend its full re-sample budget (got $(count_calls "$edge_audit/fake_state/calls_data"))"
  # The marker is scoped by the round's input hash, so a genuinely new snapshot starts with a
  # fresh repair budget while the one-per-role rule holds within a snapshot. Glob for it.
  if compgen -G "$edge_audit/critique/round_1/attempt_budget/data.*.repair.used" >/dev/null 2>&1
  then edge_repair=1; else edge_repair=0; fi
  [ "$edge_repair" = "$expected_edge_repair" ] \
    || fail "$edge_case violated the zero-or-one focused-repair budget (repair used: $edge_repair)"
  [ ! -f "$edge_audit/critique/round_1/critic_data.json" ] \
    || fail "$edge_case invalid review was published"
done

# tamper_evidence is not a bad sample: the data critic's frozen REPORT_TO_REVIEW.md gains a line
# DURING its call, so the snapshot, not the response, is what changed. 2.6.7 re-sampled it as
# schema-invalid and spent all three attempts against the same broken snapshot (on the ninth
# production audit a snapshot fault cost twelve paid calls that way). Now the post-call validator
# exits 5: one call, recorded as error_class environment, no focused repair, no retry, no permanent
# marker against the role, nothing published. Parallel critics that validate after the tamper stop
# the same way; none is ever re-dispatched.
AUDIT_TAMPERED="$TMP_ROOT/audit_tamper_evidence"
make_audit "$AUDIT_TAMPERED"
if run_driver "$AUDIT_TAMPERED" 1 tamper_evidence-one tamper_evidence; then
  fail "a snapshot tampered during a call was treated as complete"
fi
[ "$(count_calls "$AUDIT_TAMPERED/fake_state/calls_data")" = 1 ] \
  || fail "a snapshot changed under the data call was re-dispatched (got $(count_calls "$AUDIT_TAMPERED/fake_state/calls_data") calls)"
[ "$(count_calls "$AUDIT_TAMPERED/fake_state/calls_total")" -le 5 ] \
  || fail "a snapshot tamper cost extra calls (got $(count_calls "$AUDIT_TAMPERED/fake_state/calls_total"))"
python3 - "$AUDIT_TAMPERED" <<'PY' || { fail "post-call snapshot tamper contract"; show_log "$TMP_ROOT/tamper_evidence-one.log"; }
import json, pathlib, sys
root = pathlib.Path(sys.argv[1]); out = root / "critique/round_1"
inv = out / "invocations/tamper_evidence-one"
ledger = [json.loads(l) for l in (root / "invocations.jsonl").read_text().splitlines() if l.strip()]
data = [r for r in ledger if r["role"] == "critic_data" and r["provider_call"]]
assert [(r["attempt"], r["status"], r["error_class"]) for r in data] == [(1, "failed", "environment")], data
local = [json.loads(l) for l in (inv / "invocations.jsonl").read_text().splitlines() if l.strip()]
row = next(r for r in local if r["role"] == "data" and r["provider_call"])
assert (row["outcome"], row["failure_class"]) == ("failed", "permanent"), row
assert not [r for r in ledger if r["error_class"] == "schema"], "a snapshot fault was recorded as schema"
for role in ("stats", "data", "fairness", "completeness"):
    # A parallel critic either finished before the tamper, stopped on it after its call, or (if
    # its pre-dispatch check came later) was refused with no call at all. Never twice.
    calls = [r for r in ledger if r["role"] == f"critic_{role}" and r["provider_call"]]
    assert len(calls) <= 1, (role, len(calls))
    failure_path = inv / f"results/{role}.failure.json"
    if failure_path.exists():
        failure = json.loads(failure_path.read_text())
        assert failure["failure_class"] == "snapshot_integrity", (role, failure)
    else:
        assert calls and calls[0]["status"] == "completed", (role, calls)
assert json.loads((inv / "results/data.failure.json").read_text())["failure_class"] == "snapshot_integrity"
budget = out / "attempt_budget"
assert not list(budget.glob("data.*.repair.used")), "a focused repair was spent on a snapshot fault"
assert not (budget / "data.permanent").exists(), "the data critic was marked permanent for a host fault"
assert not (out / "critic_data.json").exists(), "a review against a changed snapshot was published"
status = json.loads((out / "ROUND_STATUS.json").read_text())
assert status["outcome"] == "failed" and "round snapshot integrity check failed" in status["detail"], status
assert "changed during a provider call, not retried, for" in status["detail"], status["detail"]
PY
grep -q "\[fail \] critic_data: snapshot changed after the provider call (environment, not the critic); not retried" \
  "$TMP_ROOT/tamper_evidence-one.log" || fail "the driver log does not name the snapshot fault"
! grep -q "\[wait \]\|\[fix  \]" "$TMP_ROOT/tamper_evidence-one.log" \
  || fail "a snapshot fault was retried or repaired"

AUDIT_NO_WRAPUP="$TMP_ROOT/audit_no_wrapup"
make_audit "$AUDIT_NO_WRAPUP"
rm "$AUDIT_NO_WRAPUP/WRAPUP.txt"
if run_driver "$AUDIT_NO_WRAPUP" 1 no-wrapup-one success; then
  fail "missing required WRAPUP.txt was silently replaced"
fi
[ ! -f "$AUDIT_NO_WRAPUP/fake_state/calls_total" ] \
  || fail "missing WRAPUP.txt reached the provider"

AUDIT_MISSING_TOTAL="$TMP_ROOT/audit_missing_total_usage"
make_audit "$AUDIT_MISSING_TOTAL"
if ! run_driver "$AUDIT_MISSING_TOTAL" 1 missing-total-one missing_total_usage; then
  fail "complete Codex usage without a provider total was not derived from input plus output"
  show_log "$TMP_ROOT/missing-total-one.log"
else
  python3 - "$AUDIT_MISSING_TOTAL/invocations.jsonl" \
    "$AUDIT_MISSING_TOTAL/critique/round_1/invocations/missing-total-one/ROUND_STATUS.json" <<'PY' \
    || fail "derived total-token telemetry contract"
import json, pathlib, sys
rows = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
provider_rows = [row for row in rows if row["provider_call"]]
assert len(provider_rows) == 5
assert all(row["status"] == "completed" for row in provider_rows)
for row in provider_rows:
    usage = row["usage"]
    assert usage["total_tokens_kind"] == "derived"
    assert usage["total_tokens_components"] == ["input_tokens", "output_tokens"]
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    assert usage["total_tokens_unavailable_reason"] is None
status = json.loads(pathlib.Path(sys.argv[2]).read_text())
assert status["outcome"] == "complete" and status["provider_calls"] == 5
PY
fi
[ "$(count_calls "$AUDIT_MISSING_TOTAL/fake_state/calls_total")" = 5 ] \
  || fail "derived-total round should make one probe plus four critic calls"

echo "[8/10] policy-triggered adjudication is conditional, immutable, and waiter binds exact status"
AUDIT_ADJ="$TMP_ROOT/audit_adjudication"
make_audit "$AUDIT_ADJ"
if ! run_driver "$AUDIT_ADJ" 1 adjudicate-one adjudicate; then
  fail "default policy-triggered adjudication returned nonzero"; show_log "$TMP_ROOT/adjudicate-one.log"
else
python3 - "$AUDIT_ADJ/critique/round_1/invocations/adjudicate-one/ROUND_STATUS.json" \
    "$AUDIT_ADJ/critique/round_1/invocations/adjudicate-one/adjudication_packet.json" \
    "$ROOT" <<'PY' \
    || fail "targeted adjudication status/packet contract"
import hashlib, json, os, pathlib, sys
status = json.loads(pathlib.Path(sys.argv[1]).read_text())
packet = json.loads(pathlib.Path(sys.argv[2]).read_text())
assert status["outcome"] == "complete" and status["quorum_met"] is True
assert status["adjudication"] == "complete" and status["adjudication_trigger_count"] == 1
assert len(packet["triggers"]) == 1 and packet["triggers"][0]["kind"] == "verdict_conflict"
assert packet["packet_version"] == "i4r-targeted-adjudication-packet-3"
assert len(packet["unresolved_rows"]) == 1
assert packet["deferred_rows"] == []
assert packet["unresolved_rows"][0]["row_id"] == "F1"
assert packet["unresolved_rows"][0]["adjudication_ready"] is True
assert packet["unresolved_rows"][0]["defer_reasons"] == []
assert all(set(entry) == {"role", "json_path", "json_sha256", "markdown_path",
                          "markdown_sha256", "completion_path", "completion_sha256"}
           for entry in packet["critic_outputs"])
invocation = pathlib.Path(sys.argv[1]).parent
target_meta = json.loads((invocation / "target_snapshot.json").read_text())
target = pathlib.Path(target_meta["snapshot"])
assert target.name == target_meta["target_input_sha256"]
assert (target / "MANIFEST.json").is_file() and not (target.stat().st_mode & 0o222)
target_manifest = json.loads((target / "MANIFEST.json").read_text())
assert target_manifest["snapshot_version"] == "i4r-target-snapshot-4"
assert target_manifest["provider_schema_generation_version"] == \
    "i4r-openai-structured-output-4"
assert target_manifest["runtime_helper_sha256"] == hashlib.sha256(
    (pathlib.Path(sys.argv[3]) / "scripts/test_run_critique_runtime.py").read_bytes()
).hexdigest()
assert "packet_sha256" not in target_manifest
assert target_manifest["adjudication_input_sha256"] == hashlib.sha256(
    (target / "source/adjudication_packet.json").read_bytes()
).hexdigest()
assert target_manifest["model"] == os.environ["SHIPPED_ADJUDICATOR_MODEL"] and target_manifest["effort"] == "max"
ordinary_manifest = json.loads((pathlib.Path(target_meta["snapshot"]).parents[1] /
    "snapshots" / packet["input_sha256"] / "MANIFEST.json").read_text())
assert ordinary_manifest["model"] == os.environ["SHIPPED_CRITIC_MODEL"] and ordinary_manifest["effort"] == "high"
assert not (target / "source/critics").exists()
provider_packet = json.loads((target / "source/adjudication_packet.json").read_text())
assert set(provider_packet) == {"packet_version", "audit_id", "round", "input_sha256",
                                "report_sha256", "policy", "claim_merge_sha256",
                                "unresolved_rows"}
assert provider_packet["unresolved_rows"] == packet["unresolved_rows"]
assert (target / "source/evidence/report/REPORT_final.md").is_file()
covered = json.loads((target / "output.schema.json").read_text())["properties"][
    "covered_claim_ids"]
assert "const" not in covered
assert covered["minItems"] == covered["maxItems"] == 1
assert covered["items"]["enum"] == ["F1"]
projection = invocation.parents[1] / "projections/adjudicate-one"
assert projection.is_dir() and (projection / "CRITIQUE.json").is_file()
merge = json.loads((projection / "CLAIM_MERGE.json").read_text())
assert merge["claims"][0]["conflict"] is True
assert merge["claims"][0]["unresolved"] is True
assert {row["verdict"] for row in merge["claims"][0]["role_results"]} == {
    "CONFIRMED", "OVERSTATED"}
target_result = json.loads((projection / "CRITIQUE.json").read_text())
audit = invocation.parents[3]
ledger = {row["invocation_id"]: row for row in
          map(json.loads, (audit / "invocations.jsonl").read_text().splitlines())}
joined = ledger[target_result["invocation_id"]]
assert joined["role"] == "critic_synth" and joined["provider_call"] is True
assert joined["role_class"] == "adjudicator"
assert joined["requested_model"] == os.environ["SHIPPED_ADJUDICATOR_MODEL"]
assert joined["reasoning_effort"] == "max"
assert joined["output_sha256"] == __import__("hashlib").sha256(
    (projection / "CRITIQUE.json").read_bytes()).hexdigest()
PY
  [ -s "$AUDIT_ADJ/critique/round_1/CRITIQUE.md" ] \
    || fail "targeted adjudication projection is missing"
  [ -s "$AUDIT_ADJ/critique/round_1/CRITIQUE.json" ] \
    || fail "targeted adjudication JSON projection is missing"
  [ "$(count_calls "$AUDIT_ADJ/fake_state/calls_targeted_adjudicator")" = 1 ] \
    || fail "auto mode did not make exactly one targeted call"
  [ "$(sort -u "$AUDIT_ADJ/fake_state/efforts_targeted_adjudicator")" = max ] \
    || fail "targeted adjudicator was not actually invoked with effort=max"
  "$MODEL_VERIFIER" "$AUDIT_ADJ" critic_synth "$SHIPPED_ADJUDICATOR_MODEL" max >/dev/null \
    || fail "exact observed targeted-adjudicator model/effort verification failed"
  [ "$(sort -u "$AUDIT_ADJ/fake_state/models_targeted_adjudicator")" = "$SHIPPED_ADJUDICATOR_MODEL" ] \
    || fail "targeted adjudicator was not actually invoked with -m $SHIPPED_ADJUDICATOR_MODEL"
  for ordinary_role in model_probe stats data fairness completeness; do
    [ "$(sort -u "$AUDIT_ADJ/fake_state/efforts_$ordinary_role" 2>/dev/null)" = high ] \
      || fail "$ordinary_role did not remain at effort=high in an adjudicated round"
  done
fi

if ! run_driver "$AUDIT_ADJ" 1 adjudicate-replay adjudicate; then
  fail "same-content targeted snapshot replay returned nonzero"
  show_log "$TMP_ROOT/adjudicate-replay.log"
else
  python3 - "$AUDIT_ADJ/critique/round_1/invocations/adjudicate-one/target_snapshot.json" \
    "$AUDIT_ADJ/critique/round_1/invocations/adjudicate-replay/target_snapshot.json" \
    "$AUDIT_ADJ/critique/round_1/invocations/adjudicate-replay/ROUND_STATUS.json" <<'PY' \
    || fail "target snapshot replay/content-address contract"
import hashlib, json, pathlib, sys
first_meta = json.loads(pathlib.Path(sys.argv[1]).read_text())
replay_meta = json.loads(pathlib.Path(sys.argv[2]).read_text())
status = json.loads(pathlib.Path(sys.argv[3]).read_text())
assert first_meta["target_input_sha256"] == replay_meta["target_input_sha256"]
assert first_meta["snapshot"] == replay_meta["snapshot"]
assert first_meta["packet_sha256"] != replay_meta["packet_sha256"]
target = pathlib.Path(replay_meta["snapshot"])
manifest = json.loads((target / "MANIFEST.json").read_text())
assert "packet_sha256" not in manifest
assert manifest["adjudication_input_sha256"] == hashlib.sha256(
    (target / "source/adjudication_packet.json").read_bytes()
).hexdigest()
assert status["outcome"] == "complete" and status["adjudication"] == "complete"
assert status["provider_calls"] == 0
PY
fi
[ "$(count_calls "$AUDIT_ADJ/fake_state/calls_targeted_adjudicator")" = 1 ] \
  || fail "same-content target replay did not reuse the validated adjudication cache"

if ! run_driver "$AUDIT_ADJ" 1 adjudicate-two success ADJUDICATE=0; then
  fail "default-mode rerun after targeted adjudication returned nonzero"
  show_log "$TMP_ROOT/adjudicate-two.log"
fi
[ ! -e "$AUDIT_ADJ/critique/round_1/CRITIQUE.md" ] \
  && [ ! -e "$AUDIT_ADJ/critique/round_1/CRITIQUE.json" ] \
  || fail "stale targeted projections survived a no-adjudicator invocation"

echo "[forced adjudication] evidence-gap-only packet defers without provider dispatch"
AUDIT_FORCED_DEFER="$TMP_ROOT/audit_forced_defer"
make_audit "$AUDIT_FORCED_DEFER"
if ! run_driver "$AUDIT_FORCED_DEFER" 1 forced-defer-one success ADJUDICATE=1; then
  fail "forced evidence-gap-only adjudication did not defer cleanly"
  show_log "$TMP_ROOT/forced-defer-one.log"
else
  [ "$(count_calls "$AUDIT_FORCED_DEFER/fake_state/calls_total")" = 5 ] \
    || fail "forced deferred-only run made an unexpected provider call"
  [ "$(count_calls "$AUDIT_FORCED_DEFER/fake_state/calls_targeted_adjudicator")" = 0 ] \
    || fail "forced deferred-only run dispatched the targeted adjudicator"
  python3 - "$AUDIT_FORCED_DEFER/critique/round_1/invocations/forced-defer-one" <<'PY' \
    || fail "forced evidence-gap deferral contract"
import json,pathlib,sys
invocation=pathlib.Path(sys.argv[1])
status=json.loads((invocation/"ROUND_STATUS.json").read_text())
packet=json.loads((invocation/"adjudication_packet.json").read_text())
assert status["outcome"] == "complete" and status["quorum_met"] is True
assert status["adjudication"] == "deferred_evidence_gaps"
assert status["provider_calls"] == 5
assert packet["policy"] == "1" and len(packet["triggers"]) == 1
assert packet["triggers"][0]["kind"] == "forced"
assert packet["unresolved_rows"] == [] and len(packet["deferred_rows"]) == 1
deferred=packet["deferred_rows"][0]
assert deferred["adjudication_ready"] is False and deferred["defer_reasons"]
assert "canonical finding or merged critic record is absent" in deferred["defer_reasons"]
assert not (invocation/"target_snapshot.json").exists()
assert not (invocation/"results/TARGETED_ADJUDICATION.json").exists()
assert not (invocation.parents[1]/"CRITIQUE.json").exists()
PY
fi

echo "[8b/10] a failed adjudication still publishes the critic quorum it already earned"
AUDIT_QAF="$TMP_ROOT/audit_quorum_adj_failed"
make_audit "$AUDIT_QAF"
if run_driver "$AUDIT_QAF" 1 quorum-adj-failed adjudicator_invalid; then
  fail "adjudication failure must still exit non-zero"
else
python3 - "$AUDIT_QAF" "$AUDIT_QAF/critique/round_1/invocations/quorum-adj-failed/ROUND_STATUS.json" <<'QAF' \
    || { fail "quorum work was not published before the adjudication failure"; show_log "$TMP_ROOT/quorum-adj-failed.log"; }
import json, pathlib, sys
audit = pathlib.Path(sys.argv[1])
status = json.loads(pathlib.Path(sys.argv[2]).read_text())
# Every critic succeeded; only the adjudicator failed. Those reviews are expensive and valid, so
# they must be journaled and projected before the round reports the adjudication failure.
assert status["quorum_met"] is True, status
assert status["adjudication"] == "failed", status
assert status["outcome"] == "quorum_complete_adjudication_failed", status
out = audit / "critique/round_1"
for role in ("stats", "data", "fairness", "completeness"):
    assert (out / f"critic_{role}.json").is_file(), f"critic_{role}.json was discarded"
    assert (out / f"critic_{role}.md").is_file(), f"critic_{role}.md was discarded"
# ...but the round is NOT integrated: adjudication is part of integration, so a rerun replays
# the cached critics and retries only the adjudicator.
journal = audit / "run.events.jsonl"
if journal.is_file():
    kinds = [json.loads(line)["type"] for line
             in journal.read_text().splitlines() if line.strip()]
    assert "round_quorum_complete" in kinds, kinds
    assert "round_integrated" not in kinds, kinds
QAF
fi
echo "[metadata 2.1] the driver's own remedies work on a finished round: adjudicator retry, then forced rerun"
# Both remedies used to dispatch nothing on their first application: reserve_invocation refused any
# dispatch into a round past `running`, appended round_authorization_required (regressing the round
# to awaiting_authorization and the run to paused), and the attempt debited before the refusal
# stayed spent. The legacy fixtures above never saw it because only a metadata-2.1 audit has the
# journal gate. This block drives the exact production sequence.
AUDIT_RETRY="$TMP_ROOT/audit_adj_retry_21"
make_audit "$AUDIT_RETRY"
if ! bash "$ROOT/scripts/init_run.sh" "$AUDIT_RETRY" --audit-id audit-adj-retry-21 \
    --protocol-version test --gate-set-version test >/dev/null 2>&1; then
  fail "could not initialize adjudication-retry fixture"
elif run_driver "$AUDIT_RETRY" 1 adj-retry-one adjudicator_invalid; then
  fail "adjudicator_invalid must leave the round at quorum_complete_adjudication_failed"
else
  adj_before=$(count_calls "$AUDIT_RETRY/fake_state/calls_targeted_adjudicator")
  # The remedy the driver printed, with the ceiling its [stop] message names.
  if ! run_driver "$AUDIT_RETRY" 1 adj-retry-two adjudicate MAX_TRIES=4; then
    fail "the printed remedy (rerun with MAX_TRIES raised) did not complete the round"
    show_log "$TMP_ROOT/adj-retry-two.log"
  else
    adj_after=$(count_calls "$AUDIT_RETRY/fake_state/calls_targeted_adjudicator")
    [ "$adj_after" -gt "$adj_before" ] || fail "the rerun dispatched no adjudicator call ($adj_before -> $adj_after)"
    python3 - "$AUDIT_RETRY" <<'PY' || fail "adjudication-retry lifecycle contract"
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
events = [json.loads(l) for l in (root/"run.events.jsonl").read_text().splitlines() if l.strip()]
kinds = [e["type"] for e in events]
assert "round_integrated" in kinds, kinds
regressions = [e for e in events if e["type"] == "round_authorization_required"
               and (e.get("payload") or {}).get("reason") == "missing_authorization"]
assert not regressions, "the retry regressed the round to awaiting_authorization"
reopened = [e for e in events if e["type"] == "round_start" and (e.get("payload") or {}).get("reopened_from")]
assert reopened and reopened[-1]["payload"]["reopened_from"] == "quorum_complete", reopened
run = json.loads((root/"run.json").read_text())
row = next(r for r in run["rounds"] if r["round"] == 1)
assert row["state"] == "integrated" and run["status"] != "paused", (row["state"], run["status"])
PY
  fi
  # And the other documented remedy, on the now-integrated 2.1 round.
  if ! run_driver "$AUDIT_RETRY" 1 adj-retry-three adjudicate I4R_FORCE_RERUN=1 MAX_TRIES=6; then
    fail "I4R_FORCE_RERUN=1 on an integrated metadata-2.1 round did not complete"
    show_log "$TMP_ROOT/adj-retry-three.log"
  else
    python3 - "$AUDIT_RETRY" <<'PY' || fail "forced-rerun lifecycle contract"
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
run = json.loads((root/"run.json").read_text())
assert run["status"] != "paused", run["status"]
events = [json.loads(l) for l in (root/"run.events.jsonl").read_text().splitlines() if l.strip()]
assert not [e for e in events if e["type"] == "round_authorization_required"
            and (e.get("payload") or {}).get("reason") == "missing_authorization"]
PY
  fi
fi
echo "[9/10] a same-input forced rerun cannot overwrite the first valid cache object, and still completes"
AUDIT_CONFLICT="$TMP_ROOT/audit_conflict"
make_audit "$AUDIT_CONFLICT"
if ! run_driver "$AUDIT_CONFLICT" 1 conflict-one success; then
  fail "cache-conflict fixture baseline failed"; show_log "$TMP_ROOT/conflict-one.log"
else
  first_cache_sha=$(shasum -a 256 "$AUDIT_CONFLICT/critique/round_1/result_cache"/*/stats/output.json | awk '{print $1}')
  # Two valid reviews of identical input that differ is ordinary sampling variance. The first
  # cached object stays canonical and the alternative is preserved; the round must still finish.
  # The first production audit lost a fully valid four-critic round to this being fatal.
  if ! run_driver "$AUDIT_CONFLICT" 1 conflict-two nondeterministic I4R_FORCE_RERUN=1; then
    fail "nondeterministic forced rerun must complete using the first cached object"
    show_log "$TMP_ROOT/conflict-two.log"
    find "$AUDIT_CONFLICT/critique/round_1/result_cache" -maxdepth 4 -print >&2
  fi
  grep -q "second valid output" "$TMP_ROOT/conflict-two.log" \
    || fail "conflict resolution was not announced in the driver log"
  second_cache_sha=$(shasum -a 256 "$AUDIT_CONFLICT/critique/round_1/result_cache"/*/stats/output.json | awk '{print $1}')
  [ "$first_cache_sha" = "$second_cache_sha" ] || fail "first valid same-input cache was overwritten"
  find "$AUDIT_CONFLICT/critique/round_1/result_cache" -name '*.conflict' -type f | grep -q . \
    || fail "same-input cache conflict was not surfaced persistently"
  find "$AUDIT_CONFLICT/critique/round_1/result_cache" -path '*/conflicts/*/conflict-two' -type d | grep -q . \
    || fail "conflicting valid output was not retained separately"
fi

echo "[10/10] terminating the driver reaps active providers before releasing the round lock"
AUDIT_SIGNAL="$TMP_ROOT/audit_signal"
make_audit "$AUDIT_SIGNAL"
signal_log="$TMP_ROOT/signal-one.log"
env AUDIT_DIR="$AUDIT_SIGNAL" CODEX="$FAKE" FAKE_CODEX_STATE="$AUDIT_SIGNAL/fake_state" \
  FAKE_CODEX_SCENARIO=slow I4R_INVOCATION_ID=signal-one RETRY_BACKOFF_SECONDS=0 \
  PROBE_LOCK_WAIT_SECONDS=3 bash "$DRIVER" 1 > "$signal_log" 2>&1 &
signal_driver_pid=$!
signal_started=0
# In the `slow` scenario ONLY the stats role sleeps; the other three run normally. Signalling as
# soon as stats reports started therefore caught the other three at an arbitrary point -- sometimes
# cached, sometimes mid-flight -- and the resume below then saw a different world each run. That is
# a genuine flake: this test failed once in three runs with all four roles restarting at attempt 2.
# Wait until the three fast roles have banked their results, so the reap has exactly one live
# provider to kill and the resume state is the same every time.
signal_results="$AUDIT_SIGNAL/critique/round_1/invocations/signal-one/results"
for signal_tick in $(seq 1 400); do
  signal_ready=0
  for signal_role in data fairness completeness; do
    [ -f "$signal_results/${signal_role}.complete.json" ] && signal_ready=$((signal_ready + 1))
  done
  if [ -s "$AUDIT_SIGNAL/fake_state/slow_started" ] && [ "$signal_ready" -eq 3 ]; then
    signal_started=1; break
  fi
  sleep 0.05
done
if [ "$signal_started" -ne 1 ]; then
  fail "slow provider did not start for the signal test"
  kill -TERM "$signal_driver_pid" 2>/dev/null || true
fi
kill -TERM "$signal_driver_pid" 2>/dev/null || true
signal_rc=0
wait "$signal_driver_pid" || signal_rc=$?
[ "$signal_rc" -ne 0 ] || fail "terminated critique driver returned success"
slow_provider_pid=$(sed -n '1p' "$AUDIT_SIGNAL/fake_state/slow_started" 2>/dev/null || true)
sleep 0.2
case "$slow_provider_pid" in
  ''|*[!0-9]*) fail "signal fixture did not record a provider pid" ;;
  *) kill -0 "$slow_provider_pid" 2>/dev/null && fail "provider survived driver termination" ;;
esac
[ ! -e "$AUDIT_SIGNAL/fake_state/slow_finished" ] || fail "orphan provider completed after termination"
[ ! -e "$AUDIT_SIGNAL/critique/round_1/.runtime_lock" ] || fail "round lock survived reaping"
python3 - "$AUDIT_SIGNAL/critique/round_1/invocations/signal-one/ROUND_STATUS.json" \
  "$AUDIT_SIGNAL/invocations.jsonl" <<'PY' || fail "signal terminal status/telemetry contract"
import json, pathlib, sys
status = json.loads(pathlib.Path(sys.argv[1]).read_text())
rows = [json.loads(line) for line in pathlib.Path(sys.argv[2]).read_text().splitlines()]
assert status["outcome"] == "failed" and status["quorum_met"] is False
assert any(row["role"] == "critic_stats" and row["status"] == "cancelled"
           and row["error_class"] == "transient" for row in rows)
PY
if ! run_driver "$AUDIT_SIGNAL" 1 signal-two success; then
  fail "resume after reaped signal cancellation failed"; show_log "$TMP_ROOT/signal-two.log"
fi

if ! bash "$WAITER" "$AUDIT_SUCCESS" 1 --invocation success-two --timeout-min 0 >/dev/null 2>&1; then
  fail "waiter rejected a completed exact invocation"
fi
if bash "$WAITER" "$AUDIT_PERMANENT" 1 --invocation permanent-two --timeout-min 0 >/dev/null 2>&1; then
  fail "waiter accepted a failed invocation"
fi
if bash "$WAITER" "$AUDIT_SUCCESS" 1 --invocation does-not-exist --timeout-min 0 >/dev/null 2>&1; then
  fail "waiter accepted a missing invocation"
fi

for telemetry_audit in "$AUDIT_SUCCESS" "$AUDIT_TRANSIENT" "$AUDIT_TIMEOUT" "$AUDIT_VAGUE" \
  "$AUDIT_5XX" "$AUDIT_429" "$AUDIT_EXHAUSTED" "$AUDIT_PERMANENT" "$AUDIT_INCOMPLETE" \
  "$TMP_ROOT/audit_all_skipped" "$TMP_ROOT/audit_bad_path" "$AUDIT_ADJ" \
  "$AUDIT_FORCED_DEFER" "$AUDIT_CONFLICT" "$AUDIT_SIGNAL"; do
  python3 - "$telemetry_audit/invocations.jsonl" "$ROOT" <<'PY' \
    || fail "canonical invocation telemetry failed v2 validation for $telemetry_audit"
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[2]) / "schemas/tools"))
sys.path.insert(0, str(pathlib.Path(sys.argv[2]) / "scripts"))
import validate_v2
import record_invocation
schema = json.loads((pathlib.Path(sys.argv[2]) / "schemas/v2/invocation-record.schema.json").read_text())
expected = set(schema["required"])
allowed = set(schema["properties"])
rows = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
assert rows and len({row["invocation_id"] for row in rows}) == len(rows)
for row in rows:
    assert expected <= set(row) <= allowed
    validate_v2.validate_invocation(row)
    record_invocation.validate_record(row, pathlib.Path(sys.argv[1]).resolve().parent)
    assert row["model_family"] == "gpt"
PY
done

python3 - "$TMP_ROOT" "$ROOT" <<'PY' || fail "published critic JSON failed the shared v2 contract"
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[2]) / "schemas/tools"))
import validate_v2
schema = json.loads((pathlib.Path(sys.argv[2]) / "schemas/v2/critic-result.schema.json").read_text())
expected = set(schema["required"])
names = {f"critic_{role}.json" for role in ("stats", "data", "fairness", "completeness")}
paths = [path for path in pathlib.Path(sys.argv[1]).rglob("*.json") if path.name in names]
paths += list(pathlib.Path(sys.argv[1]).rglob("TARGETED_ADJUDICATION.json"))
assert paths
for path in paths:
    obj = json.loads(path.read_text())
    assert set(obj) == expected
    validate_v2.validate_critic_result(obj)
PY

python3 - "$TMP_ROOT" <<'PY' || fail "generated provider schema contains an untyped const"
import json, pathlib, sys

def json_type(value):
    if value is None: return "null"
    if isinstance(value, bool): return "boolean"
    if isinstance(value, str): return "string"
    if isinstance(value, int): return "integer"
    if isinstance(value, float): return "number"
    if isinstance(value, list): return "array"
    if isinstance(value, dict): return "object"
    raise AssertionError(f"unsupported const value type: {type(value).__name__}")

def check(value, source, path="#"):
    if isinstance(value, dict):
        forbidden = {"uniqueItems", "minLength", "maxLength"} & set(value)
        assert not forbidden, f"{source}: {path} has unsupported keywords {sorted(forbidden)}"
        if "pattern" in value:
            assert not any(marker in value["pattern"] for marker in ("(?=", "(?!", "(?<=", "(?<!")), (
                f"{source}: {path} has an unsupported lookaround pattern")
        if "const" in value:
            assert not isinstance(value["const"], (list, dict)), (
                f"{source}: {path} has a provider-incompatible composite const")
            expected = json_type(value["const"])
            declared = value.get("type")
            choices = declared if isinstance(declared, list) else [declared]
            assert expected in choices, (
                f"{source}: {path} const has type {declared!r}, expected {expected!r}")
        if "enum" in value:
            assert "type" in value, f"{source}: {path} enum lacks explicit type"
        for key, child in value.items():
            check(child, source, f"{path}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            check(child, source, f"{path}/{index}")

paths = list(pathlib.Path(sys.argv[1]).rglob("*.schema.json"))
assert paths, "test suite did not generate provider schemas"
for schema_path in paths:
    check(json.loads(schema_path.read_text()), schema_path)
PY

echo "[11/11] an account that cannot use the pinned model: the provider's words; 2.7.5 locks the route"
# The shipped critic model is the one refused (on 2026-09-29 a ChatGPT account on CLI 0.158.0-alpha.2.1 got
# "The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account"). Until 2.7.5 the documented
# remedy re-pinned the audit to Terra; 2.7.5 refuses that, and an audit initialised on 2.7.5 froze
# critique.critic_route_lock, which this driver enforces even after a re-pin by an older copy of the tool.
STALE_MODEL=gpt-5.6-terra
[ "$SHIPPED_CRITIC_MODEL" != "$STALE_MODEL" ] || STALE_MODEL=gpt-5.6-luna
export STALE_MODEL
snapshot_sha() { shasum -a 256 "$1/pipeline-policy.snapshot.json" | cut -d' ' -f1; }
AUDIT_REPIN="$TMP_ROOT/audit_repin"
make_audit "$AUDIT_REPIN"
if run_driver "$AUDIT_REPIN" 1 repin-one success FAKE_UNSUPPORTED_MODEL="$SHIPPED_CRITIC_MODEL"; then
  fail "a model the account cannot use was treated as success"
fi
grep -q "not supported when using Codex with a ChatGPT account" "$TMP_ROOT/repin-one.log" \
  || { fail "the probe failure line does not carry the provider's message"; show_log "$TMP_ROOT/repin-one.log"; }
# the probe stops the round before any critic is paid for
[ "$(count_calls "$AUDIT_REPIN/fake_state/calls_total")" = "$(count_calls "$AUDIT_REPIN/fake_state/calls_model_probe")" ] \
  && [ "$(count_calls "$AUDIT_REPIN/fake_state/calls_model_probe")" -ge 1 ] \
  || fail "a critic was dispatched after the model probe was refused"
python3 - "$AUDIT_REPIN/invocations.jsonl" <<'PY' || fail "the ledger records a CLI warning instead of the provider's reason"
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
probe = [row for row in rows if row["role"] == "model_probe"][-1]
assert probe["status"] != "completed", probe["status"]
assert "not supported when using Codex" in probe["error"] and "models cache" not in probe["error"], probe["error"]
PY
# the 2.7.0 remedy is refused and writes nothing
before=$(snapshot_sha "$AUDIT_REPIN")
if python3 "$ROOT/scripts/repin_critic_route.py" "$AUDIT_REPIN" --critic-model "$STALE_MODEL" \
    --reason "fixture: this account cannot use $SHIPPED_CRITIC_MODEL" > "$TMP_ROOT/repin.log" 2>&1; then
  fail "2.7.5 re-pinned an audit off the shipped critic route"
fi
grep -q "since 2.7.5 a re-pin can only move an audit to the shipped critic route" "$TMP_ROOT/repin.log" \
  || { fail "the re-pin refusal does not say why"; show_log "$TMP_ROOT/repin.log"; }
[ "$(snapshot_sha "$AUDIT_REPIN")" = "$before" ] || fail "a refused re-pin changed the snapshot"
# an older copy of the tool (no route rule) still moves it: the driver refuses the round before any call
python3 - "$AUDIT_REPIN" "$ROOT" <<'PY' > "$TMP_ROOT/repin-stale.log" 2>&1 || fail "could not simulate an older copy's re-pin"
import os, pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[2]) / "scripts"))
import repin_critic_route
repin_critic_route.SHIPPED_ROUTE_ONLY = False   # what repin_critic_route.py 2.7.4 and earlier did
repin_critic_route.repin(pathlib.Path(sys.argv[1]), critic_model=os.environ["STALE_MODEL"], adjudicator_model=None,
                         reason="fixture: an older copy of the tool")
PY
calls_before=$(count_calls "$AUDIT_REPIN/fake_state/calls_total")
if run_driver "$AUDIT_REPIN" 1 repin-locked success; then
  fail "the driver ran a round off the frozen critic_route_lock"
fi
grep -q "critic_route_lock=$SHIPPED_CRITIC_MODEL" "$TMP_ROOT/repin-locked.log" \
  || { fail "the lock refusal does not name the lock"; show_log "$TMP_ROOT/repin-locked.log"; }
[ "$(count_calls "$AUDIT_REPIN/fake_state/calls_total")" = "$calls_before" ] || fail "a provider was called off the locked route"
# the remedy the refusal names restores the route, and an account with the model finishes the round on it
python3 "$ROOT/scripts/repin_critic_route.py" "$AUDIT_REPIN" --reason "fixture: restore the locked critic route" \
  > "$TMP_ROOT/repin-restore.log" 2>&1 || { fail "the locked route could not be restored"; show_log "$TMP_ROOT/repin-restore.log"; }
if ! run_driver "$AUDIT_REPIN" 1 repin-two success; then
  fail "the round on the locked route did not complete"; show_log "$TMP_ROOT/repin-two.log"
else
  [ "$(sort -u "$AUDIT_REPIN/fake_state/models_stats")" = "$SHIPPED_CRITIC_MODEL" ] \
    || fail "the critics were not invoked with -m $SHIPPED_CRITIC_MODEL"
  python3 "$ROOT/scripts/summarize_invocations.py" "$AUDIT_REPIN" --check >/dev/null \
    || fail "the ledger of the restored audit does not validate"
fi

# The one re-pin 2.7.5 still makes: an audit frozen on an older route (no lock) moves up to the shipped one, and the
# probe refused before the re-pin is still judged against the route in force when it ran.
AUDIT_UP="$TMP_ROOT/audit_repin_up"
make_audit "$AUDIT_UP"
python3 - "$AUDIT_UP" "$ROOT" <<'PY' || fail "could not freeze the older-route fixture"
import json, os, pathlib, sys, tempfile
from unittest import mock
audit, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
sys.path.insert(0, str(root / "scripts"))
import record_invocation
policy = json.loads((root / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))
for entry in (policy["models"]["critics"], policy["models"]["targeted_adjudicator"], policy["critique"]["model_probe"]):
    entry["requested_model"] = os.environ["STALE_MODEL"]
policy["critique"].pop("critic_route_lock", None)   # frozen by 2.7.4 or earlier
snapshot = audit / "pipeline-policy.snapshot.json"
snapshot.chmod(0o600)
snapshot.unlink()
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
    json.dump(policy, handle, indent=2)
with mock.patch.object(record_invocation, "POLICY", pathlib.Path(handle.name)):
    record_invocation.initialize_policy_snapshot(audit)
os.unlink(handle.name)
PY
if run_driver "$AUDIT_UP" 1 up-one success FAKE_UNSUPPORTED_MODEL="$STALE_MODEL"; then
  fail "a model the account cannot use was treated as success (older route)"
fi
python3 "$ROOT/scripts/repin_critic_route.py" "$AUDIT_UP" --reason "fixture: this account can use the shipped route" \
  > "$TMP_ROOT/repin-up.log" 2>&1 || { fail "an older-route audit could not be moved up to the shipped route"; show_log "$TMP_ROOT/repin-up.log"; }
if ! run_driver "$AUDIT_UP" 1 up-two success FAKE_UNSUPPORTED_MODEL="$STALE_MODEL"; then
  fail "the round moved up to the shipped route did not complete"; show_log "$TMP_ROOT/up-two.log"
else
  python3 - "$AUDIT_UP" <<'PY' || fail "moved-up route contract"
import json, os, pathlib, sys
shipped, older = os.environ["SHIPPED_CRITIC_MODEL"], os.environ["STALE_MODEL"]
root = pathlib.Path(sys.argv[1])
status = json.loads((root / "critique/round_1/invocations/up-two/ROUND_STATUS.json").read_text())
assert status["outcome"] == "complete" and status["quorum_met"] is True, status
rows = [json.loads(line) for line in (root / "invocations.jsonl").read_text().splitlines() if line.strip()]
critics = [row for row in rows if row["role"].startswith("critic_") and row["status"] == "completed"]
assert critics and all(row["requested_model"] == shipped for row in critics), [r["requested_model"] for r in critics]
assert any(row["role"] == "model_probe" and row["requested_model"] == older and row["status"] != "completed" for row in rows)
history = json.loads((root / "pipeline-policy.snapshot.json").read_text())["policy"]["critic_route_history"]
assert history[-1]["critics"]["requested_model"] == older and history[-1]["reason"]
PY
  [ "$(sort -u "$AUDIT_UP/fake_state/models_stats")" = "$SHIPPED_CRITIC_MODEL" ] \
    || fail "the moved-up round did not invoke the critics with -m $SHIPPED_CRITIC_MODEL"
  python3 "$ROOT/scripts/summarize_invocations.py" "$AUDIT_UP" --check >/dev/null \
    || fail "the probe made before the re-pin was judged against the new route"
fi
# after reviews completed, another model is refused and the shipped one is already pinned
if python3 "$ROOT/scripts/repin_critic_route.py" "$AUDIT_UP" --critic-model "$STALE_MODEL" \
    --reason "negative control" > "$TMP_ROOT/repin-refused.log" 2>&1; then
  fail "a re-pin back to the older route was allowed"
fi
grep -q "since 2.7.5" "$TMP_ROOT/repin-refused.log" || fail "the re-pin refusal does not say why"
# and an older-route audit whose reviews completed on that route is not moved even to the shipped one: one audit's
# reviews stay on one route
AUDIT_DONE="$TMP_ROOT/audit_repin_done"
make_audit "$AUDIT_DONE"
python3 - "$AUDIT_DONE" "$ROOT" <<'PY' || fail "could not freeze the finished older-route fixture"
import json, os, pathlib, sys, tempfile
from unittest import mock
audit, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
sys.path.insert(0, str(root / "scripts"))
import record_invocation
policy = json.loads((root / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))
for entry in (policy["models"]["critics"], policy["models"]["targeted_adjudicator"], policy["critique"]["model_probe"]):
    entry["requested_model"] = os.environ["STALE_MODEL"]
policy["critique"].pop("critic_route_lock", None)
snapshot = audit / "pipeline-policy.snapshot.json"
snapshot.chmod(0o600)
snapshot.unlink()
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
    json.dump(policy, handle, indent=2)
with mock.patch.object(record_invocation, "POLICY", pathlib.Path(handle.name)):
    record_invocation.initialize_policy_snapshot(audit)
os.unlink(handle.name)
PY
run_driver "$AUDIT_DONE" 1 done-one success || { fail "the older-route round did not complete"; show_log "$TMP_ROOT/done-one.log"; }
before=$(snapshot_sha "$AUDIT_DONE")
if python3 "$ROOT/scripts/repin_critic_route.py" "$AUDIT_DONE" --reason "negative control" \
    > "$TMP_ROOT/repin-done.log" 2>&1; then
  fail "a re-pin was allowed after critic reviews had completed"
fi
grep -q "already completed" "$TMP_ROOT/repin-done.log" || { fail "the completed-review refusal does not say why"; show_log "$TMP_ROOT/repin-done.log"; }
[ "$(snapshot_sha "$AUDIT_DONE")" = "$before" ] || fail "a refused re-pin changed the snapshot"

echo "[12/12] a frozen snapshot that drifts is re-frozen before any paid call; one that changes stops"
# On the ninth production audit the Dropbox File Provider reset a freshly frozen snapshot's source
# directory to 0700 during the model probe. Nothing checked before dispatch, every critic's
# post-call check then failed as "schema", and the retry loop spent twelve paid calls; the
# adjudicator's target snapshot hit the same race. FAKE_SNAPSHOT_DRIFT=<role> is that mode-only
# drift; tamper_evidence with FAKE_INCOMPLETE_ROLE=<role> is a genuine content change.
make_audit_21() {
  make_audit "$1" && bash "$ROOT/scripts/init_run.sh" "$1" --audit-id "$2" \
    --protocol-version test --gate-set-version test >/dev/null 2>&1
}
# Shared checks over one metadata-2.1 audit: <audit> <tmp-root> <expected refrozen-note count>
# [<snapshot kind of those notes>]. Every note is audit-relative and depth-limited, no snapshot
# fault is ever retried, and a re-frozen snapshot is left with no write bit anywhere.
check_drift_notes() {
  python3 - "$@" <<'PY'
import json, pathlib, sys
root, tmp, expected = pathlib.Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
kind = sys.argv[4] if len(sys.argv) > 4 else None
events = [json.loads(l) for l in (root / "run.events.jsonl").read_text().splitlines() if l.strip()]
notes = [e["payload"] for e in events if e["type"] == "note"
         and e["payload"].get("kind") in ("snapshot_mode_refrozen", "snapshot_integrity_refused")]
refrozen = [n for n in notes if n["kind"] == "snapshot_mode_refrozen"]
assert len(refrozen) == expected, refrozen
assert kind is None or all(n["snapshot_kind"] == kind for n in refrozen), refrozen
def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
for note in notes:
    for text in strings(note):
        assert tmp not in text and not text.startswith("/"), ("absolute path in a note", note)
    assert all(len(path.split("/")) <= 2 for path in note.get("paths", [])), note
    assert note["snapshot"].startswith("critique/round_1/"), note
assert not [e for e in events if e["type"].startswith("retry_backoff")], "a snapshot fault was retried"
# A re-frozen snapshot is left fully frozen; a content fault is deliberately left as found.
if expected:
    for sub in ("snapshots", "target_snapshots"):
        for snapshot in (root / "critique/round_1" / sub).glob("[0-9a-f]*"):
            loose = [p for p in [snapshot, *snapshot.rglob("*")] if p.stat().st_mode & 0o222]
            assert not loose, ("a snapshot was left writable", loose[:3])
PY
}

# (a) The ninth audit's shape: the drift lands during the (uncached) model probe. Every critic
# now succeeds at attempt 1; the first pre-dispatch check re-freezes and journals one note.
AUDIT_DRIFT_PROBE="$TMP_ROOT/audit_drift_probe"
if ! make_audit_21 "$AUDIT_DRIFT_PROBE" audit-drift-probe-21; then
  fail "could not initialize the probe-drift fixture"
elif ! run_driver "$AUDIT_DRIFT_PROBE" 1 drift-probe success FAKE_SNAPSHOT_DRIFT=model_probe; then
  fail "a mode-only drift during the probe failed the round"; show_log "$TMP_ROOT/drift-probe.log"
else
  [ "$(count_calls "$AUDIT_DRIFT_PROBE/fake_state/calls_total")" = 5 ] \
    || fail "probe-time drift cost extra calls (got $(count_calls "$AUDIT_DRIFT_PROBE/fake_state/calls_total"))"
  check_drift_notes "$AUDIT_DRIFT_PROBE" "$TMP_ROOT" 1 round || fail "probe-time drift note contract"
  python3 - "$AUDIT_DRIFT_PROBE" <<'PY' || { fail "probe-time drift contract"; show_log "$TMP_ROOT/drift-probe.log"; }
import json, pathlib, sys
root = pathlib.Path(sys.argv[1]); out = root / "critique/round_1"
ledger = [json.loads(l) for l in (root / "invocations.jsonl").read_text().splitlines() if l.strip()]
provider = [r for r in ledger if r["provider_call"]]
assert [(r["attempt"], r["status"]) for r in provider] == [(1, "completed")] * 5, provider
assert sorted(r["role"] for r in provider) == [
    "critic_completeness", "critic_data", "critic_fairness", "critic_stats", "model_probe"]
for role in ("stats", "data", "fairness", "completeness"):
    assert (out / f"attempt_budget/{role}.count").read_text().strip() == "1", role
events = [json.loads(l) for l in (root / "run.events.jsonl").read_text().splitlines() if l.strip()]
note = next(e["payload"] for e in events if e["type"] == "note"
            and e["payload"].get("kind") == "snapshot_mode_refrozen")
assert note["context"].startswith("pre_dispatch:critic_") and note["paths"] == ["source"], note
assert note["invocation"] == "drift-probe", note
status = json.loads((out / "ROUND_STATUS.json").read_text())
assert status["outcome"] == "complete" and status["quorum_met"] is True, status
PY
  grep -q "\[note \] snapshot mode drift re-frozen" "$TMP_ROOT/drift-probe.log" \
    || fail "the re-freeze was not announced in the driver log"
fi

# (b) A content change during the probe: every critic is refused BEFORE its claim, so no attempt,
# no reservation and no call. Then the printed remedy: delete the content-addressed snapshot and
# rerun; it is rebuilt identically and the round completes on the cached probe.
AUDIT_TAMPER_PROBE="$TMP_ROOT/audit_tamper_probe"
if ! make_audit_21 "$AUDIT_TAMPER_PROBE" audit-tamper-probe-21; then
  fail "could not initialize the probe-tamper fixture"
elif run_driver "$AUDIT_TAMPER_PROBE" 1 tamper-probe tamper_evidence FAKE_INCOMPLETE_ROLE=model_probe; then
  fail "critics were dispatched against a snapshot whose content changed"
else
  [ "$(count_calls "$AUDIT_TAMPER_PROBE/fake_state/calls_total")" = 1 ] \
    || fail "a snapshot that failed its pre-dispatch check reached the provider (calls $(count_calls "$AUDIT_TAMPER_PROBE/fake_state/calls_total"))"
  check_drift_notes "$AUDIT_TAMPER_PROBE" "$TMP_ROOT" 0 || fail "probe-tamper note contract"
  python3 - "$AUDIT_TAMPER_PROBE" <<'PY' || { fail "pre-dispatch refusal contract"; show_log "$TMP_ROOT/tamper-probe.log"; }
import json, pathlib, sys
root = pathlib.Path(sys.argv[1]); out = root / "critique/round_1"
inv = out / "invocations/tamper-probe"
ledger = [json.loads(l) for l in (root / "invocations.jsonl").read_text().splitlines() if l.strip()]
assert [r["role"] for r in ledger] == ["model_probe"], [r["role"] for r in ledger]
events = [json.loads(l) for l in (root / "run.events.jsonl").read_text().splitlines() if l.strip()]
reserved = [e["payload"]["role"] for e in events if e["type"] == "invocation_reserved"]
assert reserved == ["model_probe"], reserved
requested = [e["payload"]["invocation_id"] for e in events if e["type"] == "provider_request"]
assert requested and all(":model_probe:" in value for value in requested), requested
refusals = [e["payload"] for e in events if e["type"] == "note"
            and e["payload"].get("kind") == "snapshot_integrity_refused"]
assert sorted(n["context"] for n in refusals) == [
    "pre_dispatch:critic_completeness", "pre_dispatch:critic_data", "pre_dispatch:critic_fairness",
    "pre_dispatch:critic_stats"], refusals
for role in ("stats", "data", "fairness", "completeness"):
    count = out / f"attempt_budget/{role}.count"
    assert not count.exists() or count.read_text().strip() == "0", (role, "an attempt was spent")
    failure = json.loads((inv / f"results/{role}.failure.json").read_text())
    assert (failure["failure_class"], failure["attempts"]) == ("snapshot_integrity", 0), failure
status = json.loads((out / "ROUND_STATUS.json").read_text())
assert status["outcome"] == "failed" and status["provider_calls"] == 1, status
assert "refused before dispatch, no provider call was made for" in status["detail"], status["detail"]
assert "rebuilt identically" in status["detail"], status["detail"]
report = root / status["snapshot"] / "source/REPORT_TO_REVIEW.md"
assert report.stat().st_mode & 0o200, "a content fault is left exactly as found, not re-frozen"
PY
  grep -q "\[stop \] critic_stats: snapshot integrity check failed before dispatch; no provider call was made" \
    "$TMP_ROOT/tamper-probe.log" || fail "the pre-dispatch refusal does not say that no call was made"
  tampered_rel=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["snapshot"])' \
    "$AUDIT_TAMPER_PROBE/critique/round_1/ROUND_STATUS.json")
  # 2.6.8 fix A: run the remedy exactly as ROUND_STATUS printed it, in the audit directory. A
  # plain `rm -rf` cannot remove a frozen snapshot, so the printed command must carry the chmod.
  printed_remedy=$(python3 - "$AUDIT_TAMPER_PROBE/critique/round_1/ROUND_STATUS.json" <<'PY'
import json, re, sys
detail = json.load(open(sys.argv[1]))["detail"]
match = re.search(r"chmod -R u\+w (critique/round_1/snapshots/[0-9a-f]{64}) && rm -rf "
                  r"(critique/round_1/snapshots/[0-9a-f]{64})", detail)
if not match or match.group(1) != match.group(2):
    raise SystemExit(f"no full remedy command in ROUND_STATUS: {detail}")
print(f"chmod -R u+w {match.group(1)} && rm -rf {match.group(2)}")
PY
  ) || fail "ROUND_STATUS printed no full remedy command"
  case "$tampered_rel" in
    critique/round_1/snapshots/?*)
      if (cd "$AUDIT_TAMPER_PROBE" && rm -rf "$tampered_rel" 2>/dev/null); then
        fail "a plain rm -rf removed a frozen snapshot; the remedy test proves nothing"
      fi
      (cd "$AUDIT_TAMPER_PROBE" && bash -c "$printed_remedy") \
        || fail "the printed remedy failed: $printed_remedy"
      [ ! -e "$AUDIT_TAMPER_PROBE/$tampered_rel" ] || fail "the printed remedy left the snapshot"
      ;;
    *) fail "ROUND_STATUS names no round snapshot: $tampered_rel" ;;
  esac
  if ! run_driver "$AUDIT_TAMPER_PROBE" 1 tamper-probe-two success; then
    fail "the printed remedy (delete the snapshot, rerun) did not complete the round"
    show_log "$TMP_ROOT/tamper-probe-two.log"
  else
    [ -d "$AUDIT_TAMPER_PROBE/$tampered_rel" ] || fail "the snapshot was not rebuilt at the same content address"
    [ "$(count_calls "$AUDIT_TAMPER_PROBE/fake_state/calls_total")" = 5 ] \
      || fail "the remedy rerun did not reuse the cached probe (calls $(count_calls "$AUDIT_TAMPER_PROBE/fake_state/calls_total"))"
  fi
fi

# (c) A mode-only drift during one critic's call: its post-call check re-freezes, the paid review
# is kept, and nothing is re-sampled.
AUDIT_DRIFT_POST="$TMP_ROOT/audit_drift_post"
if ! make_audit_21 "$AUDIT_DRIFT_POST" audit-drift-post-21; then
  fail "could not initialize the post-call drift fixture"
elif ! run_driver "$AUDIT_DRIFT_POST" 1 drift-post success FAKE_SNAPSHOT_DRIFT=stats; then
  fail "a mode-only drift during a critic call failed the round"; show_log "$TMP_ROOT/drift-post.log"
else
  [ "$(count_calls "$AUDIT_DRIFT_POST/fake_state/calls_stats")" = 1 ] || fail "the drifted stats review was re-sampled"
  [ "$(count_calls "$AUDIT_DRIFT_POST/fake_state/calls_total")" = 5 ] || fail "post-call drift cost extra calls"
  check_drift_notes "$AUDIT_DRIFT_POST" "$TMP_ROOT" 1 round || fail "post-call drift note contract"
  python3 - "$AUDIT_DRIFT_POST" <<'PY' || fail "post-call drift ledger contract"
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
ledger = [json.loads(l) for l in (root / "invocations.jsonl").read_text().splitlines() if l.strip()]
assert all(r["status"] == "completed" and r["attempt"] == 1 for r in ledger if r["provider_call"]), ledger
PY
  # (g) A relaunch on a snapshot a previous invocation left drifted needs no manual chmod: the
  # reuse path re-freezes it and journals why.
  drifted_rel=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["snapshot"])' \
    "$AUDIT_DRIFT_POST/critique/round_1/ROUND_STATUS.json")
  chmod 700 "$AUDIT_DRIFT_POST/$drifted_rel/source"
  if ! run_driver "$AUDIT_DRIFT_POST" 1 drift-relaunch success I4R_FORCE_RERUN=1; then
    fail "a relaunch on a drifted snapshot did not complete"; show_log "$TMP_ROOT/drift-relaunch.log"
  else
    check_drift_notes "$AUDIT_DRIFT_POST" "$TMP_ROOT" 2 round || fail "relaunch drift note contract"
    python3 - "$AUDIT_DRIFT_POST" <<'PY' || fail "the relaunch did not re-freeze on reuse"
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
events = [json.loads(l) for l in (root / "run.events.jsonl").read_text().splitlines() if l.strip()]
contexts = [e["payload"]["context"] for e in events if e["type"] == "note"
            and e["payload"].get("kind") == "snapshot_mode_refrozen"]
assert contexts[-1] == "snapshot_reuse", contexts
PY
  fi
fi

# (d) A content change during a critic call, sequentially: one call, error_class environment, a
# terminal row for every reservation, no repair, no retry, no permanent marker, and the later
# roles are never dispatched.
AUDIT_TAMPER_POST="$TMP_ROOT/audit_tamper_post"
if ! make_audit_21 "$AUDIT_TAMPER_POST" audit-tamper-post-21; then
  fail "could not initialize the post-call tamper fixture"
elif run_driver "$AUDIT_TAMPER_POST" 1 tamper-post tamper_evidence PARALLEL=1; then
  fail "a snapshot tampered during the data call was treated as complete"
else
  for tamper_role in stats data; do
    [ "$(count_calls "$AUDIT_TAMPER_POST/fake_state/calls_$tamper_role")" = 1 ] \
      || fail "$tamper_role made $(count_calls "$AUDIT_TAMPER_POST/fake_state/calls_$tamper_role") calls, expected 1"
  done
  for tamper_role in fairness completeness; do
    [ "$(count_calls "$AUDIT_TAMPER_POST/fake_state/calls_$tamper_role")" = 0 ] \
      || fail "$tamper_role was dispatched after the snapshot changed"
  done
  check_drift_notes "$AUDIT_TAMPER_POST" "$TMP_ROOT" 0 || fail "post-call tamper note contract"
  python3 - "$AUDIT_TAMPER_POST" <<'PY' || { fail "post-call tamper contract"; show_log "$TMP_ROOT/tamper-post.log"; }
import json, pathlib, sys
root = pathlib.Path(sys.argv[1]); out = root / "critique/round_1"
inv = out / "invocations/tamper-post"
ledger = [json.loads(l) for l in (root / "invocations.jsonl").read_text().splitlines() if l.strip()]
data = [r for r in ledger if r["role"] == "critic_data"]
assert [(r["attempt"], r["status"], r["error_class"], r["provider_call"]) for r in data] == [
    (1, "failed", "environment", True)], data
events = [json.loads(l) for l in (root / "run.events.jsonl").read_text().splitlines() if l.strip()]
reserved = {e["payload"]["invocation_id"] for e in events if e["type"] == "invocation_reserved"}
terminal = {e["payload"]["invocation_id"] for e in events if e["type"] == "invocation_terminal"}
assert reserved == terminal, ("a reservation was left without a terminal row", reserved - terminal)
assert json.loads((inv / "results/data.failure.json").read_text())["failure_class"] == "snapshot_integrity"
assert not list((out / "attempt_budget").glob("data.*.repair.used")), "a repair was spent"
assert not (out / "attempt_budget/data.permanent").exists(), "the critic was blamed for a host fault"
assert not (out / "critic_data.json").exists(), "a review against a changed snapshot was published"
status = json.loads((out / "ROUND_STATUS.json").read_text())
assert "changed during a provider call, not retried, for data" in status["detail"], status["detail"]
PY
fi

# (e) Mode-only drift of the TARGET snapshot during the adjudicator's call: kept, attempt 1.
AUDIT_DRIFT_TARGET="$TMP_ROOT/audit_drift_target"
if ! make_audit_21 "$AUDIT_DRIFT_TARGET" audit-drift-target-21; then
  fail "could not initialize the target-drift fixture"
elif ! run_driver "$AUDIT_DRIFT_TARGET" 1 drift-target adjudicate FAKE_SNAPSHOT_DRIFT=targeted_adjudicator; then
  fail "a mode-only drift of the target snapshot failed the adjudication"; show_log "$TMP_ROOT/drift-target.log"
else
  [ "$(count_calls "$AUDIT_DRIFT_TARGET/fake_state/calls_targeted_adjudicator")" = 1 ] \
    || fail "the drifted adjudication was re-sampled"
  check_drift_notes "$AUDIT_DRIFT_TARGET" "$TMP_ROOT" 1 target || fail "target drift note contract"
  python3 - "$AUDIT_DRIFT_TARGET" <<'PY' || fail "target drift contract"
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
status = json.loads((root / "critique/round_1/ROUND_STATUS.json").read_text())
assert status["outcome"] == "complete" and status["adjudication"] == "complete", status
events = [json.loads(l) for l in (root / "run.events.jsonl").read_text().splitlines() if l.strip()]
assert "round_integrated" in [e["type"] for e in events]
PY
fi

# (f) A content change of the target snapshot during the adjudicator's call: one call, recorded
# as environment, no permanent marker, no retry, and the critic quorum is still projected.
AUDIT_TAMPER_TARGET="$TMP_ROOT/audit_tamper_target"
if ! make_audit_21 "$AUDIT_TAMPER_TARGET" audit-tamper-target-21; then
  fail "could not initialize the target-tamper fixture"
elif run_driver "$AUDIT_TAMPER_TARGET" 1 tamper-target tamper_evidence FAKE_INCOMPLETE_ROLE=targeted_adjudicator; then
  fail "an adjudication against a changed target snapshot was treated as complete"
else
  [ "$(count_calls "$AUDIT_TAMPER_TARGET/fake_state/calls_targeted_adjudicator")" = 1 ] \
    || fail "a changed target snapshot was re-dispatched (calls $(count_calls "$AUDIT_TAMPER_TARGET/fake_state/calls_targeted_adjudicator"))"
  check_drift_notes "$AUDIT_TAMPER_TARGET" "$TMP_ROOT" 0 || fail "target tamper note contract"
  python3 - "$AUDIT_TAMPER_TARGET" <<'PY' || { fail "target tamper contract"; show_log "$TMP_ROOT/tamper-target.log"; }
import json, pathlib, sys
root = pathlib.Path(sys.argv[1]); out = root / "critique/round_1"
inv = out / "invocations/tamper-target"
ledger = [json.loads(l) for l in (root / "invocations.jsonl").read_text().splitlines() if l.strip()]
synth = [r for r in ledger if r["role"] == "critic_synth"]
assert [(r["status"], r["error_class"]) for r in synth] == [("failed", "environment")], synth
assert not (out / "attempt_budget/targeted_adjudicator.permanent").exists(), \
    "the adjudicator was marked permanent for a host fault"
failure = json.loads((inv / "results/targeted_adjudicator.failure.json").read_text())
assert failure["failure_class"] == "snapshot_integrity", failure
status = json.loads((out / "ROUND_STATUS.json").read_text())
assert status["outcome"] == "quorum_complete_adjudication_failed", status
assert "target snapshot integrity check failed (changed during a provider call" in status["detail"], status
for role in ("stats", "data", "fairness", "completeness"):
    assert (out / f"critic_{role}.json").is_file(), f"critic_{role}.json was discarded"
PY
fi

python3 "$ROOT/scripts/summarize_invocations.py" "$AUDIT_SUCCESS" --check \
  --json "$AUDIT_SUCCESS/runtime_metrics.json" --markdown "$AUDIT_SUCCESS/runtime_metrics.md" \
  >/dev/null || fail "successful provider calls did not achieve complete model/token/latency telemetry"

if [ "$bad" -eq 0 ]; then
  echo "ok: immutable runtime, caches, routing, retries, schema quorum, telemetry, adjudication, waiter"
else
  echo "$bad runtime test(s) failed" >&2
fi
exit "$bad"
