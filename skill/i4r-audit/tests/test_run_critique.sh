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
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/i4r-runtime-test.XXXXXX") || exit 2
cleanup() {
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

echo "[1/10] success, exact quorum, immutable snapshot, and default zero adjudicator calls"
AUDIT_SUCCESS="$TMP_ROOT/audit_success"
make_audit "$AUDIT_SUCCESS"
printf '%s\n' 'ROUND_ONE_MUST_NOT_INJECT_THIS_SETTLED_FACT' > "$AUDIT_SUCCESS/SETTLED.md"
if ! run_driver "$AUDIT_SUCCESS" 1 success-one success; then
  fail "successful round 1 returned nonzero"; show_log "$TMP_ROOT/success-one.log"
else
  python3 - "$AUDIT_SUCCESS/critique/round_1/invocations/success-one/ROUND_STATUS.json" \
    "$AUDIT_SUCCESS/critique/round_1" <<'PY' || fail "round 1 status/snapshot contract"
import hashlib, json, pathlib, stat, sys
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
assert manifest["model"] == "gpt-5.6-terra" and manifest["effort"] == "high"
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
for schema_path in (snapshots[0] / "schemas").glob("*.schema.json"):
    schema = json.loads(schema_path.read_text())
    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)
    if schema_path.name != "probe.schema.json":
        assert not ({"allOf", "oneOf", "if", "then", "else"} & set(keys(schema)))
        assert schema["properties"]["coverage"]["properties"]["completed_checks"]["minItems"] == 1
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
    assert invocation["requested_model"] == "gpt-5.6-terra"
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
assert all(row["requested_model"] == "gpt-5.6-terra" for row in provider_rows)
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
"$MODEL_VERIFIER" "$AUDIT_SUCCESS" critic_stats gpt-5.6-terra high >/dev/null \
  || fail "exact observed ordinary model/effort verification failed"
if run_driver "$AUDIT_SUCCESS" 1 reject-critic-max success CRITIC_EFFORT=max; then
  fail "ordinary critic runtime accepted non-policy effort=max"
fi
if run_driver "$AUDIT_SUCCESS" 1 reject-adjudicator-high success ADJUDICATOR_EFFORT=high; then
  fail "targeted-adjudicator runtime accepted non-policy effort=high"
fi
[ "$(count_calls "$AUDIT_SUCCESS/fake_state/calls_total")" = 5 ] \
  || fail "rejected effort overrides reached the provider"

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
(root / "work/findings.jsonl").write_text(json.dumps(after, sort_keys=True) + "\n")
(root / "work/report_before_round_2.md").write_text("# Fixture report\n\nOlder bounded data wording.\n")
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
import json, pathlib, sys
status = json.loads(pathlib.Path(sys.argv[1]).read_text())
routing = json.loads(pathlib.Path(sys.argv[2]).read_text())
assert routing["roles"] == ["data", "completeness"]
assert routing["claim_assignment_mode"] == "exact_changed_claims"
assert routing["claim_assignments"] == {"data": ["F2"], "completeness": ["F2"]}
assert status["expected_roles"] == routing["roles"] and status["quorum_met"] is True
assert status["provider_calls"] == 2 and status["cache_hits"] == 1
snapshot = pathlib.Path(sys.argv[1]).parents[4] / status["snapshot"]
assert (snapshot / "source/context/DIFF_MANIFEST.json").is_file()
snapshot_manifest = json.loads((snapshot / "MANIFEST.json").read_text())
assert snapshot_manifest["model"] == "gpt-5.6-terra"
assert snapshot_manifest["effort"] == "high"
prompt = (snapshot / "rendered/prompt_data.txt").read_text()
assert "runtime_diff_manifest=" + status["snapshot"] in prompt
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
printf '%s\n' '{"round":2,"changed_claim_ids":["F2","F3"],"changed_sections":["Section 4","Appendix A"],"routing_mode":"multi_domain_critical","critical_domains":["statistics","data"],"recommended_roles":["stats","data"]}' \
  > "$MULTI_MANIFEST"
if ! python3 "$HELPER" route --round 2 --manifest "$MULTI_MANIFEST" --output "$MULTI_ROUTING"; then
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
import json, pathlib, sys
rows = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
status = json.loads(pathlib.Path(sys.argv[2]).read_text())
stats = [row for row in rows if row["role"] == "stats"]
assert [(row["attempt"], row["failure_class"], row["outcome"]) for row in stats] == [
    (1, "transient", "failed"), (2, None, "complete")]
assert len({row["call_id"] for row in rows}) == len(rows)
assert all(row["requested_model"] == "gpt-5.6-terra" for row in rows)
assert all(row["requested_effort"] == "high" for row in rows)
completed = [row for row in rows if row["outcome"] == "complete"]
assert completed and all(row["usage"]["reasoning_tokens"] == 10 for row in completed)
assert all(row["usage"]["cache_write_input_tokens"] == 7 for row in completed)
assert status["provider_calls"] == 6 and status["outcome"] == "complete"
PY
fi
[ "$(count_calls "$AUDIT_TRANSIENT/fake_state/calls_stats")" = 2 ] \
  || fail "transient role did not use exactly two attempts"

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

echo "[6/10] permanent failures stop immediately and remain stopped across resume"
AUDIT_PERMANENT="$TMP_ROOT/audit_permanent"
make_audit "$AUDIT_PERMANENT"
if run_driver "$AUDIT_PERMANENT" 1 permanent-one permanent; then
  fail "permanent provider failure was treated as success"
fi
[ "$(count_calls "$AUDIT_PERMANENT/fake_state/calls_data")" = 1 ] \
  || fail "permanent provider failure was retried"
permanent_total=$(count_calls "$AUDIT_PERMANENT/fake_state/calls_total")
if run_driver "$AUDIT_PERMANENT" 1 permanent-two permanent; then
  fail "resumed permanently failed round was treated as success"
fi
[ "$(count_calls "$AUDIT_PERMANENT/fake_state/calls_total")" = "$permanent_total" ] \
  || fail "resume reset a persistent attempt/permanent-failure ledger"
python3 - "$AUDIT_PERMANENT/critique/round_1/invocations/permanent-two/ROUND_STATUS.json" <<'PY' \
  || fail "permanent failure status contract"
import json, pathlib, sys
status = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert status["outcome"] == "failed" and status["quorum_met"] is False
assert "data" in status["failed_roles"] and status["provider_calls"] == 0
PY

echo "[7/10] canonical schema and semantic-invalid clean exits are rejected without retries"
AUDIT_INCOMPLETE="$TMP_ROOT/audit_incomplete"
make_audit "$AUDIT_INCOMPLETE"
if run_driver "$AUDIT_INCOMPLETE" 1 incomplete-one incomplete; then
  fail "schema-incomplete response was treated as complete"
fi
[ "$(count_calls "$AUDIT_INCOMPLETE/fake_state/calls_data")" = 1 ] \
  || fail "schema-incomplete response consumed redundant retries"
python3 - "$AUDIT_INCOMPLETE/critique/round_1/invocations/incomplete-one/invocations.jsonl" <<'PY' \
  || fail "schema rejection telemetry contract"
import json, pathlib, sys
rows = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
row = next(row for row in rows if row["role"] == "data")
assert row["outcome"] == "schema_invalid" and row["failure_class"] == "permanent"
PY

for edge_case in all_skipped bad_path empty_assessments live_only wrong_evidence_hash \
  tamper_evidence missing_invocation wrong_input_hash missing_declaration; do
  edge_audit="$TMP_ROOT/audit_${edge_case}"
  make_audit "$edge_audit"
  if run_driver "$edge_audit" 1 "${edge_case}-one" "$edge_case"; then
    fail "$edge_case canonical-invalid response was treated as complete"
  fi
  [ "$(count_calls "$edge_audit/fake_state/calls_data")" = 1 ] \
    || fail "$edge_case canonical-invalid response consumed redundant retries"
done

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
if run_driver "$AUDIT_MISSING_TOTAL" 1 missing-total-one missing_total_usage; then
  fail "provider usage without observed total_tokens passed telemetry coverage"
fi
[ "$(count_calls "$AUDIT_MISSING_TOTAL/fake_state/calls_total")" = 1 ] \
  || fail "missing total-token telemetry was not rejected at the model probe"
[ ! -s "$AUDIT_MISSING_TOTAL/invocations.jsonl" ] \
  || fail "missing total-token telemetry was synthesized into the canonical ledger"

echo "[8/10] policy-triggered adjudication is conditional, immutable, and waiter binds exact status"
AUDIT_ADJ="$TMP_ROOT/audit_adjudication"
make_audit "$AUDIT_ADJ"
if ! run_driver "$AUDIT_ADJ" 1 adjudicate-one adjudicate; then
  fail "default policy-triggered adjudication returned nonzero"; show_log "$TMP_ROOT/adjudicate-one.log"
else
  python3 - "$AUDIT_ADJ/critique/round_1/invocations/adjudicate-one/ROUND_STATUS.json" \
    "$AUDIT_ADJ/critique/round_1/invocations/adjudicate-one/adjudication_packet.json" <<'PY' \
    || fail "targeted adjudication status/packet contract"
import json, pathlib, sys
status = json.loads(pathlib.Path(sys.argv[1]).read_text())
packet = json.loads(pathlib.Path(sys.argv[2]).read_text())
assert status["outcome"] == "complete" and status["quorum_met"] is True
assert status["adjudication"] == "complete" and status["adjudication_trigger_count"] == 1
assert len(packet["triggers"]) == 1 and packet["triggers"][0]["kind"] == "verdict_conflict"
assert packet["packet_version"] == "i4r-targeted-adjudication-packet-2"
assert len(packet["unresolved_rows"]) == 1
assert packet["unresolved_rows"][0]["row_id"] == "F1"
assert all(set(entry) == {"role", "json_path", "json_sha256", "markdown_path",
                          "markdown_sha256", "completion_path", "completion_sha256"}
           for entry in packet["critic_outputs"])
invocation = pathlib.Path(sys.argv[1]).parent
target_meta = json.loads((invocation / "target_snapshot.json").read_text())
target = pathlib.Path(target_meta["snapshot"])
assert target.name == target_meta["target_input_sha256"]
assert (target / "MANIFEST.json").is_file() and not (target.stat().st_mode & 0o222)
target_manifest = json.loads((target / "MANIFEST.json").read_text())
assert target_manifest["snapshot_version"] == "i4r-target-snapshot-2"
assert target_manifest["model"] == "gpt-5.6-terra" and target_manifest["effort"] == "max"
ordinary_manifest = json.loads((pathlib.Path(target_meta["snapshot"]).parents[1] /
    "snapshots" / packet["input_sha256"] / "MANIFEST.json").read_text())
assert ordinary_manifest["model"] == "gpt-5.6-terra" and ordinary_manifest["effort"] == "high"
assert not (target / "source/critics").exists()
provider_packet = json.loads((target / "source/adjudication_packet.json").read_text())
assert set(provider_packet) == {"packet_version", "audit_id", "round", "input_sha256",
                                "report_sha256", "policy", "claim_merge_sha256",
                                "unresolved_rows"}
assert provider_packet["unresolved_rows"] == packet["unresolved_rows"]
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
assert joined["requested_model"] == "gpt-5.6-terra"
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
  "$MODEL_VERIFIER" "$AUDIT_ADJ" critic_synth gpt-5.6-terra max >/dev/null \
    || fail "exact observed targeted-adjudicator model/effort verification failed"
  for ordinary_role in model_probe stats data fairness completeness; do
    [ "$(sort -u "$AUDIT_ADJ/fake_state/efforts_$ordinary_role" 2>/dev/null)" = high ] \
      || fail "$ordinary_role did not remain at effort=high in an adjudicated round"
  done
fi

if ! run_driver "$AUDIT_ADJ" 1 adjudicate-two success ADJUDICATE=0; then
  fail "default-mode rerun after targeted adjudication returned nonzero"
  show_log "$TMP_ROOT/adjudicate-two.log"
fi
[ ! -e "$AUDIT_ADJ/critique/round_1/CRITIQUE.md" ] \
  && [ ! -e "$AUDIT_ADJ/critique/round_1/CRITIQUE.json" ] \
  || fail "stale targeted projections survived a no-adjudicator invocation"

echo "[9/10] a same-input forced rerun cannot overwrite the first valid cache object"
AUDIT_CONFLICT="$TMP_ROOT/audit_conflict"
make_audit "$AUDIT_CONFLICT"
if ! run_driver "$AUDIT_CONFLICT" 1 conflict-one success; then
  fail "cache-conflict fixture baseline failed"; show_log "$TMP_ROOT/conflict-one.log"
else
  first_cache_sha=$(shasum -a 256 "$AUDIT_CONFLICT/critique/round_1/result_cache"/*/stats/output.json | awk '{print $1}')
  if run_driver "$AUDIT_CONFLICT" 1 conflict-two nondeterministic I4R_FORCE_RERUN=1; then
    fail "nondeterministic forced rerun silently replaced immutable caches"
    show_log "$TMP_ROOT/conflict-two.log"
    find "$AUDIT_CONFLICT/critique/round_1/result_cache" -maxdepth 4 -print >&2
  fi
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
for signal_tick in $(seq 1 100); do
  if [ -s "$AUDIT_SIGNAL/fake_state/slow_started" ]; then signal_started=1; break; fi
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

for telemetry_audit in "$AUDIT_SUCCESS" "$AUDIT_TRANSIENT" "$AUDIT_EXHAUSTED" "$AUDIT_PERMANENT" \
  "$AUDIT_INCOMPLETE" "$TMP_ROOT/audit_all_skipped" "$TMP_ROOT/audit_bad_path" "$AUDIT_ADJ" \
  "$AUDIT_CONFLICT" "$AUDIT_SIGNAL"; do
  python3 - "$telemetry_audit/invocations.jsonl" "$ROOT" <<'PY' \
    || fail "canonical invocation telemetry failed v2 validation for $telemetry_audit"
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[2]) / "schemas/tools"))
sys.path.insert(0, str(pathlib.Path(sys.argv[2]) / "scripts"))
import validate_v2
import record_invocation
schema = json.loads((pathlib.Path(sys.argv[2]) / "schemas/v2/invocation-record.schema.json").read_text())
expected = set(schema["required"])
rows = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
assert rows and len({row["invocation_id"] for row in rows}) == len(rows)
for row in rows:
    assert set(row) == expected
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

python3 "$ROOT/scripts/summarize_invocations.py" "$AUDIT_SUCCESS" --check \
  --json "$AUDIT_SUCCESS/runtime_metrics.json" --markdown "$AUDIT_SUCCESS/runtime_metrics.md" \
  >/dev/null || fail "successful provider calls did not achieve complete model/token/latency telemetry"

if [ "$bad" -eq 0 ]; then
  echo "ok: immutable runtime, caches, routing, retries, schema quorum, telemetry, adjudication, waiter"
else
  echo "$bad runtime test(s) failed" >&2
fi
exit "$bad"
