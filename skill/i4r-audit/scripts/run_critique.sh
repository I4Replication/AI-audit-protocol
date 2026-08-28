#!/bin/bash
# Run an immutable, schema-gated critique round.
#
# Usage:
#   AUDIT_DIR=/path/to/audit bash scripts/run_critique.sh ROUND
#
# Optional controls:
#   CRITIC_MODEL=gpt-5.6-terra  CRITIC_EFFORT=high  (ordinary critics and probe)
#   ADJUDICATOR_MODEL=gpt-5.6-terra  ADJUDICATOR_EFFORT=max  (targeted only)
#   CRITICS="stats data fairness completeness"  PARALLEL=N
#   DIFF_MANIFEST=/path/to/manifest.json
#   ADJUDICATE=0|auto|1   (default auto: call Terra only for policy triggers)
#
# Every provider call is made from an immutable content-addressed snapshot and must return a
# schema-valid JSON final response. Attempt counts persist by snapshot and role, so restarting the
# driver cannot reset the three-attempt ceiling.
set -uo pipefail

fail_early() {
  echo "FATAL: $*" >&2
  exit 1
}

case "${1:-}" in
  ''|*[!0-9]*) fail_early "usage: run_critique.sh <positive-round-number>" ;;
esac
ROUND="$1"
[ "$ROUND" -ge 1 ] || fail_early "round must be at least 1"
[ "$ROUND" -le 5 ] || fail_early "round exceeds the policy cap of 5"

[ -n "${AUDIT_DIR:-}" ] || fail_early "set AUDIT_DIR to the audit workspace"
[ -d "$AUDIT_DIR" ] || fail_early "AUDIT_DIR is not a directory: $AUDIT_DIR"
WS=$(CDPATH= cd -- "$AUDIT_DIR" 2>/dev/null && pwd) || fail_early "cannot resolve AUDIT_DIR"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) \
  || fail_early "cannot resolve script directory"
SKILL=$(CDPATH= cd -- "$SCRIPT_DIR/.." 2>/dev/null && pwd) \
  || fail_early "cannot resolve skill directory"
HELPER="$SCRIPT_DIR/test_run_critique_runtime.py"
PROMPTS="$SKILL/prompts"
CODEX="${CODEX:-/Applications/ChatGPT.app/Contents/Resources/codex}"
CRITIC_MODEL="${CRITIC_MODEL:-${MODEL:-gpt-5.6-terra}}"
CRITIC_EFFORT="${CRITIC_EFFORT:-${EFFORT:-high}}"
ADJUDICATOR_MODEL="${ADJUDICATOR_MODEL:-gpt-5.6-terra}"
ADJUDICATOR_EFFORT="${ADJUDICATOR_EFFORT:-max}"
MAX_TRIES="${MAX_TRIES:-3}"
RETRY_BACKOFF_SECONDS="${RETRY_BACKOFF_SECONDS:-15}"
PROBE_LOCK_WAIT_SECONDS="${PROBE_LOCK_WAIT_SECONDS:-60}"
FORCE_RERUN="${I4R_FORCE_RERUN:-0}"

case "$MAX_TRIES" in ''|*[!0-9]*) fail_early "MAX_TRIES must be an integer from 1 to 3" ;; esac
[ "$MAX_TRIES" -ge 1 ] && [ "$MAX_TRIES" -le 3 ] \
  || fail_early "MAX_TRIES must be from 1 to 3; the global cap cannot be raised"
case "$RETRY_BACKOFF_SECONDS" in ''|*[!0-9]*) fail_early "RETRY_BACKOFF_SECONDS must be an integer" ;; esac
case "$PROBE_LOCK_WAIT_SECONDS" in ''|*[!0-9]*) fail_early "PROBE_LOCK_WAIT_SECONDS must be an integer" ;; esac
case "$FORCE_RERUN" in 0|1) ;; *) fail_early "I4R_FORCE_RERUN must be 0 or 1" ;; esac
[ "$CRITIC_MODEL" = "gpt-5.6-terra" ] \
  || fail_early "critic model is policy-pinned to gpt-5.6-terra (got $CRITIC_MODEL)"
[ "$CRITIC_EFFORT" = "high" ] \
  || fail_early "ordinary critic and probe effort is policy-pinned to high (got $CRITIC_EFFORT)"
[ "$ADJUDICATOR_MODEL" = "gpt-5.6-terra" ] \
  || fail_early "targeted-adjudicator model is policy-pinned to gpt-5.6-terra (got $ADJUDICATOR_MODEL)"
[ "$ADJUDICATOR_EFFORT" = "max" ] \
  || fail_early "targeted-adjudicator effort is policy-pinned to max (got $ADJUDICATOR_EFFORT)"
[ -x "$CODEX" ] || fail_early "Codex executable is not executable: $CODEX"
[ -x "$HELPER" ] || fail_early "runtime helper is not executable: $HELPER"

if [ -n "${REPORT_PATH:-}" ]; then
  case "$REPORT_PATH" in
    /*) REPORT="$REPORT_PATH" ;;
    *) REPORT="$WS/$REPORT_PATH" ;;
  esac
elif [ -f "$WS/report/REPORT_final.md" ]; then
  REPORT="$WS/report/REPORT_final.md"
else
  REPORT="$WS/report/REPORT_v${ROUND}.md"
fi
[ -s "$REPORT" ] || fail_early "report not found or empty: $REPORT"
[ -s "$WS/PAPER.txt" ] || fail_early "required audit context is missing: $WS/PAPER.txt"
for prompt_name in common.txt role_stats.txt role_data.txt role_fairness.txt role_completeness.txt; do
  [ -s "$PROMPTS/$prompt_name" ] || fail_early "missing prompt: $PROMPTS/$prompt_name"
done

OUT="$WS/critique/round_${ROUND}"
mkdir -p "$OUT" || fail_early "cannot create output directory: $OUT"

# Only one driver may project CURRENT_* state for a round at a time. Preserve a stale lock for
# diagnosis instead of recursively deleting an unknown directory.
RUN_LOCK="$OUT/.runtime_lock"
if ! mkdir "$RUN_LOCK" 2>/dev/null; then
  lock_pid=""
  [ -f "$RUN_LOCK/pid" ] && lock_pid=$(sed -n '1p' "$RUN_LOCK/pid" 2>/dev/null || true)
  case "$lock_pid" in
    ''|*[!0-9]*) lock_alive=0 ;;
    *) if kill -0 "$lock_pid" 2>/dev/null; then lock_alive=1; else lock_alive=0; fi ;;
  esac
  if [ "$lock_alive" -eq 1 ]; then
    fail_early "round $ROUND already has a live driver (pid $lock_pid)"
  fi
  stale_lock="$OUT/.runtime_lock.stale.$(date -u +%Y%m%dT%H%M%SZ).$$"
  mv "$RUN_LOCK" "$stale_lock" 2>/dev/null \
    || fail_early "round lock exists and could not be inspected safely: $RUN_LOCK"
  mkdir "$RUN_LOCK" 2>/dev/null || fail_early "another driver acquired the round lock"
fi
printf '%s\n' "$$" > "$RUN_LOCK/pid"

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
epoch_now() { date +%s; }

if [ -n "${I4R_INVOCATION_ID:-}" ]; then
  INVOCATION_ID="$I4R_INVOCATION_ID"
else
  INVOCATION_ID="$(date -u +%Y%m%dT%H%M%SZ).r${ROUND}.$$.$RANDOM"
fi
case "$INVOCATION_ID" in
  ''|*[!A-Za-z0-9._-]*)
    rm -f "$RUN_LOCK/pid" 2>/dev/null || true
    rmdir "$RUN_LOCK" 2>/dev/null || true
    fail_early "invocation id may contain only letters, digits, dot, underscore, and hyphen"
    ;;
esac
INV_ROOT="$OUT/invocations"
INV="$INV_ROOT/$INVOCATION_ID"
if [ -e "$INV" ]; then
  rm -f "$RUN_LOCK/pid" 2>/dev/null || true
  rmdir "$RUN_LOCK" 2>/dev/null || true
  fail_early "invocation id already exists: $INVOCATION_ID"
fi
if ! mkdir -p "$INV/results" "$INV/attempts" "$INV/probe" "$INV/active_providers"; then
  rm -f "$RUN_LOCK/pid" 2>/dev/null || true
  rmdir "$RUN_LOCK" 2>/dev/null || true
  fail_early "cannot create invocation directory"
fi
CURRENT_TMP="$OUT/.CURRENT_INVOCATION.$$.$RANDOM.tmp"
if ! printf '%s\n' "$INVOCATION_ID" > "$CURRENT_TMP" \
  || ! mv -f "$CURRENT_TMP" "$OUT/CURRENT_INVOCATION"; then
  rm -f "$CURRENT_TMP" "$RUN_LOCK/pid" 2>/dev/null || true
  rmdir "$RUN_LOCK" 2>/dev/null || true
  fail_early "cannot publish CURRENT_INVOCATION"
fi

LOCAL_TELEMETRY="$INV/invocations.jsonl"
GLOBAL_TELEMETRY="$WS/invocations.jsonl"
LOCAL_STATUS="$INV/ROUND_STATUS.json"
CURRENT_STATUS="$OUT/ROUND_STATUS.json"
STARTED_AT=$(utc_now)
STARTED_EPOCH=$(epoch_now)
NAMES=""
INPUT_SHA=""
REPORT_SHA=""
AUDIT_ID=""
SNAPSHOT=""
SNAPSHOT_REL=""
FINAL_OUTCOME="running"
FINAL_ADJUDICATION="not_evaluated"
FINAL_DETAIL="initializing"
PROBE_LOCK=""
PROBE_LOCK_HELD=0
STATUS_ACTIVE=1
ACTIVE_WORKER_PIDS=""
CODEX_CHILD_PID=""
CODEX_CANCELLED=0
IN_PROVIDER_WORKER=0
PROVIDER_SLOT="model_probe"
PROVIDER_PID_FILE=""

write_status() {
  status_outcome="$1"
  status_exit="$2"
  status_ended="$3"
  "$HELPER" status \
    --local-status "$LOCAL_STATUS" \
    --current-status "$CURRENT_STATUS" \
    --results "$INV/results" \
    --telemetry "$LOCAL_TELEMETRY" \
    --invocation "$INVOCATION_ID" \
    --input-sha "$INPUT_SHA" \
    --report-sha "$REPORT_SHA" \
    --audit-id "$AUDIT_ID" \
    --round "$ROUND" \
    --pid "$$" \
    --outcome "$status_outcome" \
    --exit-code "$status_exit" \
    --roles "$NAMES" \
    --adjudication "$FINAL_ADJUDICATION" \
    --snapshot "$SNAPSHOT_REL" \
    --started "$STARTED_AT" \
    --ended "$status_ended" \
    --detail "$FINAL_DETAIL"
}

release_probe_lock() {
  if [ "$PROBE_LOCK_HELD" -eq 1 ] && [ -n "$PROBE_LOCK" ]; then
    rm -f "$PROBE_LOCK/pid" 2>/dev/null || true
    rmdir "$PROBE_LOCK" 2>/dev/null || true
    PROBE_LOCK_HELD=0
  fi
}

stop_pid_bounded() {
  stop_pid="$1"
  stop_already_sent="${2:-0}"
  case "$stop_pid" in ''|*[!0-9]*) return 0 ;; esac
  if kill -0 "$stop_pid" 2>/dev/null; then
    [ "$stop_already_sent" -eq 1 ] || kill -TERM "$stop_pid" 2>/dev/null || true
    stop_ticks=0
    while kill -0 "$stop_pid" 2>/dev/null && [ "$stop_ticks" -lt 50 ]; do
      sleep 0.1
      stop_ticks=$((stop_ticks + 1))
    done
    if kill -0 "$stop_pid" 2>/dev/null; then
      kill -KILL "$stop_pid" 2>/dev/null || true
    fi
  fi
  wait "$stop_pid" 2>/dev/null || true
}

provider_group_alive() {
  provider_group_pid="$1"
  kill -0 -- "-$provider_group_pid" 2>/dev/null
}

stop_provider_group() {
  provider_group_pid="$1"
  case "$provider_group_pid" in ''|*[!0-9]*) return 0 ;; esac
  if provider_group_alive "$provider_group_pid"; then
    kill -TERM -- "-$provider_group_pid" 2>/dev/null || true
    provider_ticks=0
    while provider_group_alive "$provider_group_pid" && [ "$provider_ticks" -lt 50 ]; do
      sleep 0.1
      provider_ticks=$((provider_ticks + 1))
    done
    if provider_group_alive "$provider_group_pid"; then
      kill -KILL -- "-$provider_group_pid" 2>/dev/null || true
    fi
  fi
}

stop_registered_providers() {
  for provider_file in "$INV/active_providers"/*.pid; do
    [ -f "$provider_file" ] || continue
    provider_pid=$(sed -n '1p' "$provider_file" 2>/dev/null || true)
    case "$provider_pid" in
      ''|*[!0-9]*) ;;
      *)
        atomic_text 143 "${provider_file}.cancel" 2>/dev/null || true
        stop_provider_group "$provider_pid"
        ;;
    esac
  done
}

terminate_active_workers() {
  worker_pids="$ACTIVE_WORKER_PIDS"
  ACTIVE_WORKER_PIDS=""
  stop_registered_providers
  worker_ticks=0
  while [ "$worker_ticks" -lt 50 ]; do
    workers_alive=0
    for worker_pid in $worker_pids; do
      if kill -0 "$worker_pid" 2>/dev/null; then workers_alive=1; fi
    done
    [ "$workers_alive" -eq 1 ] || break
    stop_registered_providers
    sleep 0.1
    worker_ticks=$((worker_ticks + 1))
  done
  for worker_pid in $worker_pids; do stop_pid_bounded "$worker_pid"; done
  stop_registered_providers
  if [ -n "$CODEX_CHILD_PID" ]; then
    stop_provider_group "$CODEX_CHILD_PID"
    CODEX_CHILD_PID=""
  fi
}

finish_run() {
  finish_rc=$?
  trap - EXIT INT TERM HUP
  terminate_active_workers
  release_probe_lock
  if [ "$FINAL_OUTCOME" != "complete" ]; then
    FINAL_OUTCOME="failed"
    [ "$finish_rc" -ne 0 ] || finish_rc=1
  elif [ "$finish_rc" -ne 0 ]; then
    FINAL_OUTCOME="failed"
  fi
  ended_at=$(utc_now)
  if [ "$STATUS_ACTIVE" -eq 1 ]; then
    if ! write_status "$FINAL_OUTCOME" "$finish_rc" "$ended_at"; then
      echo "FATAL: could not write atomic round status" >&2
      finish_rc=1
    fi
  fi
  rm -f "$RUN_LOCK/pid" 2>/dev/null || true
  rmdir "$RUN_LOCK" 2>/dev/null || true
  exit "$finish_rc"
}
trap finish_run EXIT
trap 'FINAL_DETAIL="interrupted by INT"; exit 130' INT
trap 'FINAL_DETAIL="interrupted by TERM"; exit 143' TERM
trap 'FINAL_DETAIL="interrupted by HUP"; exit 129' HUP

atomic_copy() {
  copy_source="$1"
  copy_target="$2"
  copy_tmp="${copy_target}.tmp.$$.$RANDOM"
  cp "$copy_source" "$copy_tmp" || return 1
  mv -f "$copy_tmp" "$copy_target"
}

atomic_text() {
  text_value="$1"
  text_target="$2"
  text_tmp="${text_target}.tmp.$$.$RANDOM"
  printf '%s\n' "$text_value" > "$text_tmp" || return 1
  mv -f "$text_tmp" "$text_target"
}

# Later rounds consume a machine-readable diff manifest. An explicit path must exist; otherwise
# discovery is deterministic and conservative.
DIFF_MANIFEST_PATH=""
if [ -n "${DIFF_MANIFEST:-}" ]; then
  case "$DIFF_MANIFEST" in
    /*) DIFF_MANIFEST_PATH="$DIFF_MANIFEST" ;;
    *) DIFF_MANIFEST_PATH="$WS/$DIFF_MANIFEST" ;;
  esac
  if [ ! -s "$DIFF_MANIFEST_PATH" ]; then
    FINAL_DETAIL="explicit diff manifest is missing: $DIFF_MANIFEST_PATH"
    echo "FATAL: $FINAL_DETAIL" >&2
    exit 1
  fi
elif [ "$ROUND" -ge 2 ]; then
  for manifest_candidate in \
    "$OUT/DIFF_MANIFEST.json" \
    "$WS/critique/DIFF_MANIFEST_round_${ROUND}.json" \
    "$WS/DIFF_MANIFEST.json"
  do
    if [ -s "$manifest_candidate" ]; then
      DIFF_MANIFEST_PATH="$manifest_candidate"
      break
    fi
  done
fi
if [ "$ROUND" -ge 2 ] && [ -z "$DIFF_MANIFEST_PATH" ]; then
  FINAL_DETAIL="round $ROUND requires a generated, content-bound diff manifest"
  echo "FATAL: $FINAL_DETAIL" >&2
  exit 1
fi
if [ -n "$DIFF_MANIFEST_PATH" ]; then
  python3 "$SCRIPT_DIR/verify_diff_packet.py" "$WS" "$DIFF_MANIFEST_PATH" --round "$ROUND" \
    >/dev/null || {
      FINAL_DETAIL="diff manifest is not the deterministic projection of its bound inputs"
      echo "FATAL: $FINAL_DETAIL" >&2
      exit 1
    }
fi

ROUTING="$INV/ROUTING.json"
if [ -n "$DIFF_MANIFEST_PATH" ]; then
  "$HELPER" route --round "$ROUND" --override "${CRITICS:-}" \
    --manifest "$DIFF_MANIFEST_PATH" --output "$ROUTING"
else
  "$HELPER" route --round "$ROUND" --override "${CRITICS:-}" --output "$ROUTING"
fi
if [ $? -ne 0 ]; then
  FINAL_DETAIL="role routing failed"
  exit 1
fi
NAMES=$(python3 - "$ROUTING" <<'PY'
import json, pathlib, sys
obj = json.loads(pathlib.Path(sys.argv[1]).read_text())
print(" ".join(obj["roles"]))
PY
)
if [ $? -ne 0 ] || [ -z "$NAMES" ]; then
  FINAL_DETAIL="routing produced no roles"
  exit 1
fi
set -- $NAMES
ROLE_COUNT=$#

if [ -z "${PARALLEL:-}" ]; then
  PARALLEL="$ROLE_COUNT"
fi
case "$PARALLEL" in ''|*[!0-9]*) FINAL_DETAIL="PARALLEL must be a positive integer"; exit 1 ;; esac
[ "$PARALLEL" -ge 1 ] || { FINAL_DETAIL="PARALLEL must be at least 1"; exit 1; }
if [ "$PARALLEL" -gt "$ROLE_COUNT" ]; then PARALLEL="$ROLE_COUNT"; fi

SNAPSHOT_META="$INV/snapshot.json"
if [ -n "$DIFF_MANIFEST_PATH" ]; then
  "$HELPER" snapshot --workspace "$WS" --report "$REPORT" --prompts "$PROMPTS" \
    --out "$OUT" --model "$CRITIC_MODEL" --effort "$CRITIC_EFFORT" \
    --roles "$NAMES" --routing "$ROUTING" --runtime-script "$SCRIPT_DIR/run_critique.sh" --round "$ROUND" \
    --diff-manifest "$DIFF_MANIFEST_PATH" > "$SNAPSHOT_META"
else
  "$HELPER" snapshot --workspace "$WS" --report "$REPORT" --prompts "$PROMPTS" \
    --out "$OUT" --model "$CRITIC_MODEL" --effort "$CRITIC_EFFORT" \
    --roles "$NAMES" --routing "$ROUTING" --runtime-script "$SCRIPT_DIR/run_critique.sh" --round "$ROUND" \
    > "$SNAPSHOT_META"
fi
if [ $? -ne 0 ]; then
  FINAL_DETAIL="immutable snapshot creation or verification failed"
  exit 1
fi
INPUT_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["input_sha256"])' "$SNAPSHOT_META")
REPORT_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["report_sha256"])' "$SNAPSHOT_META")
AUDIT_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["audit_id"])' "$SNAPSHOT_META")
SNAPSHOT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["snapshot"])' "$SNAPSHOT_META")
SNAPSHOT_REL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["snapshot_rel"])' "$SNAPSHOT_META")
if [ -z "$INPUT_SHA" ] || [ -z "$REPORT_SHA" ] || [ ! -d "$SNAPSHOT" ]; then
  FINAL_DETAIL="snapshot metadata is incomplete"
  exit 1
fi

FINAL_DETAIL="snapshot ready; checking model probe"
write_status running 0 "" || { FINAL_DETAIL="initial status write failed"; exit 1; }

record_telemetry() {
  telemetry_role="$1"
  telemetry_attempt="$2"
  telemetry_provider_call="$3"
  telemetry_outcome="$4"
  telemetry_failure="$5"
  telemetry_exit="$6"
  telemetry_started="$7"
  telemetry_started_epoch="$8"
  telemetry_ended="$9"
  shift 9
  telemetry_ended_epoch="$1"
  telemetry_events="$2"
  telemetry_response="$3"
  telemetry_stderr="$4"
  telemetry_prompt="$5"
  telemetry_cache_usage="$6"
  telemetry_error_class="$7"
  if [ "$telemetry_role" = targeted_adjudicator ]; then
    telemetry_model="$ADJUDICATOR_MODEL"
    telemetry_effort="$ADJUDICATOR_EFFORT"
  else
    telemetry_model="$CRITIC_MODEL"
    telemetry_effort="$CRITIC_EFFORT"
  fi
  telemetry_elapsed=$((telemetry_ended_epoch - telemetry_started_epoch))
  [ "$telemetry_elapsed" -ge 0 ] || telemetry_elapsed=0
  telemetry_call_id="${ACTIVE_CALL_ID:-${INVOCATION_ID}:${telemetry_role}:${telemetry_attempt}:${telemetry_started_epoch}:$RANDOM}"
  "$HELPER" telemetry \
    --local-log "$LOCAL_TELEMETRY" --global-log "$GLOBAL_TELEMETRY" \
    --events "$telemetry_events" --response "$telemetry_response" --stderr "$telemetry_stderr" \
    --prompt "$telemetry_prompt" --cache-usage "$telemetry_cache_usage" \
    --invocation "$INVOCATION_ID" --call-id "$telemetry_call_id" \
    --input-sha "${TELEMETRY_INPUT_SHA:-$INPUT_SHA}" --round "$ROUND" --role "$telemetry_role" \
    --attempt "$telemetry_attempt" --provider-call "$telemetry_provider_call" \
    --outcome "$telemetry_outcome" --failure-class "$telemetry_failure" \
    --error-class "$telemetry_error_class" \
    --exit-code "$telemetry_exit" --model "$telemetry_model" --effort "$telemetry_effort" \
    --started "$telemetry_started" --ended "$telemetry_ended" --elapsed "$telemetry_elapsed" \
    --workspace "$WS"
}

classify_failure() {
  failure_log="$1"
  if grep -qiE 'out of credits|insufficient[_ ]quota|quota exceeded|model[^[:cntrl:]]*(not supported|unsupported)|invalid_request_error|unauthorized|authentication|forbidden|invalid[^[:cntrl:]]*effort|context[^[:cntrl:]]*(too long|length)|unknown option|output schema[^[:cntrl:]]*invalid' "$failure_log" 2>/dev/null; then
    printf '%s\n' permanent
  elif grep -qiE '(^|[^0-9])429([^0-9]|$)|rate.?limit|throttl|stream disconnected|connection (refused|reset|closed)|failed to lookup|name or service not known|temporary failure|timed? ?out|timeout|(^|[^0-9])5[0-9][0-9]([^0-9]|$)|server (error|unavailable)|service unavailable|try again' "$failure_log" 2>/dev/null; then
    printf '%s\n' transient
  else
    printf '%s\n' permanent
  fi
}

classify_error() {
  error_log="$1"
  if grep -qiE 'output schema|schema[^[:cntrl:]]*(invalid|mismatch)' "$error_log" 2>/dev/null; then echo schema
  elif grep -qiE 'out of credits|insufficient[_ ]quota|quota exceeded' "$error_log" 2>/dev/null; then echo quota
  elif grep -qiE 'unauthorized|authentication|invalid api key' "$error_log" 2>/dev/null; then echo authentication
  elif grep -qiE 'forbidden|authorization|permission denied' "$error_log" 2>/dev/null; then echo authorization
  elif grep -qiE 'model[^[:cntrl:]]*(not supported|unsupported)|invalid[^[:cntrl:]]*effort' "$error_log" 2>/dev/null; then echo model
  elif grep -qiE 'timed? ?out|timeout' "$error_log" 2>/dev/null; then echo timeout
  elif grep -qiE 'stream disconnected|connection (refused|reset|closed)|failed to lookup|name or service not known|temporary failure|server unavailable|service unavailable' "$error_log" 2>/dev/null; then echo transport
  elif [ "$(classify_failure "$error_log")" = transient ]; then echo transient
  elif [ -s "$error_log" ]; then echo permanent
  else echo unknown
  fi
}

codex_exec_cancel() {
  CODEX_CANCELLED="$1"
  if [ -n "$PROVIDER_PID_FILE" ]; then atomic_text "$1" "${PROVIDER_PID_FILE}.cancel" || true; fi
  if [ -n "$CODEX_CHILD_PID" ]; then stop_provider_group "$CODEX_CHILD_PID"; fi
}

restore_provider_traps() {
  if [ "$IN_PROVIDER_WORKER" -eq 1 ]; then
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP
  else
    trap 'FINAL_DETAIL="interrupted by INT"; exit 130' INT
    trap 'FINAL_DETAIL="interrupted by TERM"; exit 143' TERM
    trap 'FINAL_DETAIL="interrupted by HUP"; exit 129' HUP
  fi
}

codex_exec() {
  exec_prompt="$1"
  exec_schema="$2"
  exec_response="$3"
  exec_events="$4"
  exec_stderr="$5"
  exec_model="${6:-$CRITIC_MODEL}"
  exec_effort="${7:-$CRITIC_EFFORT}"
  CODEX_CANCELLED=0
  PROVIDER_PID_FILE="$INV/active_providers/${PROVIDER_SLOT}.pid"
  rm -f "$PROVIDER_PID_FILE" "${PROVIDER_PID_FILE}.cancel" 2>/dev/null || true
  "$HELPER" provider-exec --codex "$CODEX" --model "$exec_model" --effort "$exec_effort" \
    --workspace "$WS" --schema "$exec_schema" --response "$exec_response" \
    --pid-file "$PROVIDER_PID_FILE" \
    < "$exec_prompt" > "$exec_events" 2> "$exec_stderr" &
  CODEX_CHILD_PID=$!
  trap 'codex_exec_cancel 130' INT
  trap 'codex_exec_cancel 143' TERM
  trap 'codex_exec_cancel 129' HUP
  wait "$CODEX_CHILD_PID"
  exec_rc=$?
  if [ -s "${PROVIDER_PID_FILE}.cancel" ]; then
    CODEX_CANCELLED=$(sed -n '1p' "${PROVIDER_PID_FILE}.cancel" 2>/dev/null || true)
    case "$CODEX_CANCELLED" in ''|*[!0-9]*) CODEX_CANCELLED=143 ;; esac
  fi
  if provider_group_alive "$CODEX_CHILD_PID"; then stop_provider_group "$CODEX_CHILD_PID"; fi
  CODEX_CHILD_PID=""
  rm -f "$PROVIDER_PID_FILE" "${PROVIDER_PID_FILE}.cancel" 2>/dev/null || true
  PROVIDER_PID_FILE=""
  restore_provider_traps
  if [ "$CODEX_CANCELLED" -ne 0 ]; then return "$CODEX_CANCELLED"; fi
  return "$exec_rc"
}

probe_cache_valid() {
  python3 - "$PROBE_CACHE" "$CRITIC_MODEL" "$CRITIC_EFFORT" <<'PY'
import json, pathlib, sys
try:
    obj = json.loads(pathlib.Path(sys.argv[1]).read_text())
    good = (
        obj.get("cache_version") == "i4r-model-probe-1"
        and obj.get("status") == "ok"
        and obj.get("model") == sys.argv[2]
        and obj.get("effort") == sys.argv[3]
    )
except Exception:
    good = False
raise SystemExit(0 if good else 1)
PY
}

CODEX_VERSION=$("$CODEX" --version 2>/dev/null | sed -n '1p')
[ -n "$CODEX_VERSION" ] || CODEX_VERSION="unknown"
PROBE_KEY=$(python3 - "$CRITIC_MODEL" "$CRITIC_EFFORT" <<'PY'
import hashlib, sys
print(hashlib.sha256("\0".join(sys.argv[1:]).encode()).hexdigest())
PY
)
PROBE_DIR="$WS/critique/.runtime/probes"
PROBE_CACHE="$PROBE_DIR/$PROBE_KEY.json"
PROBE_LOCK="$PROBE_DIR/$PROBE_KEY.lock"
PROBE_CACHE_RECORD="$INV/probe/cache_record.json"
mkdir -p "$PROBE_DIR"
PROBE_PROMPT="$INV/probe/prompt.txt"
printf '%s\n' \
  'I4R_RUNTIME_PROBE' \
  'Return exactly the JSON object required by the supplied schema. Do not use tools.' \
  > "$PROBE_PROMPT"

if probe_cache_valid; then
  atomic_copy "$PROBE_CACHE" "$PROBE_CACHE_RECORD" \
    || { FINAL_DETAIL="could not bind cached probe metadata to this invocation"; exit 1; }
  probe_time=$(utc_now); probe_epoch=$(epoch_now)
  record_telemetry model_probe 0 0 cache_hit "" 0 \
    "$probe_time" "$probe_epoch" "$probe_time" "$probe_epoch" "" "$PROBE_CACHE_RECORD" "" \
    "$PROBE_PROMPT" probe_cache "" \
    || { FINAL_DETAIL="could not record cached probe telemetry"; exit 1; }
else
  probe_waited=0
  while ! mkdir "$PROBE_LOCK" 2>/dev/null; do
    probe_lock_pid=""
    [ -f "$PROBE_LOCK/pid" ] && probe_lock_pid=$(sed -n '1p' "$PROBE_LOCK/pid" 2>/dev/null || true)
    probe_stale=0
    case "$probe_lock_pid" in
      ''|*[!0-9]*) [ "$probe_waited" -lt 2 ] || probe_stale=1 ;;
      *) kill -0 "$probe_lock_pid" 2>/dev/null || probe_stale=1 ;;
    esac
    if [ "$probe_stale" -eq 1 ]; then
      mv "$PROBE_LOCK" "$PROBE_LOCK.stale.$(date -u +%Y%m%dT%H%M%SZ).$$" 2>/dev/null || true
      continue
    fi
    if [ "$probe_waited" -ge "$PROBE_LOCK_WAIT_SECONDS" ]; then
      FINAL_DETAIL="timed out waiting for the audit/model probe lock"
      exit 1
    fi
    sleep 1
    probe_waited=$((probe_waited + 1))
  done
  printf '%s\n' "$$" > "$PROBE_LOCK/pid"
  PROBE_LOCK_HELD=1

  # A concurrent round may have populated the cache while this driver waited.
  if probe_cache_valid; then
    atomic_copy "$PROBE_CACHE" "$PROBE_CACHE_RECORD" \
      || { FINAL_DETAIL="could not bind cached probe metadata to this invocation"; exit 1; }
    probe_time=$(utc_now); probe_epoch=$(epoch_now)
    record_telemetry model_probe 0 0 cache_hit "" 0 \
      "$probe_time" "$probe_epoch" "$probe_time" "$probe_epoch" "" "$PROBE_CACHE_RECORD" "" \
      "$PROBE_PROMPT" probe_cache "" \
      || { FINAL_DETAIL="could not record cached probe telemetry"; exit 1; }
  else
    PROBE_RESPONSE="$INV/probe/response.json"
    PROBE_EVENTS="$INV/probe/events.jsonl"
    PROBE_STDERR="$INV/probe/stderr.txt"
    probe_started=$(utc_now); probe_started_epoch=$(epoch_now)
    codex_exec "$PROBE_PROMPT" "$SNAPSHOT/schemas/probe.schema.json" \
      "$PROBE_RESPONSE" "$PROBE_EVENTS" "$PROBE_STDERR"
    probe_rc=$?
    probe_cancelled=$CODEX_CANCELLED
    probe_ended=$(utc_now); probe_ended_epoch=$(epoch_now)
    probe_valid=0
    if [ "$probe_rc" -eq 0 ] && python3 - "$PROBE_RESPONSE" <<'PY'
import json, pathlib, sys
try: obj = json.loads(pathlib.Path(sys.argv[1]).read_text())
except Exception: raise SystemExit(1)
raise SystemExit(0 if obj == {"status": "ok"} else 1)
PY
    then
      probe_valid=1
    fi
    if [ "$probe_cancelled" -ne 0 ]; then
      probe_class="transient"
      probe_error_class="transient"
      probe_cache_usage=none
      probe_outcome="cancelled"
      printf 'provider cancelled by signal (exit %s)\n' "$probe_cancelled" >> "$PROBE_STDERR"
    elif [ "$probe_valid" -eq 1 ]; then
      probe_class=""
      probe_error_class=""
      probe_cache_usage=probe_cache
      probe_outcome="complete"
    else
      probe_combined="$INV/probe/failure.log"
      { [ -f "$PROBE_STDERR" ] && sed -n '1,200p' "$PROBE_STDERR"; \
        [ -f "$PROBE_EVENTS" ] && sed -n '1,200p' "$PROBE_EVENTS"; } > "$probe_combined"
      if [ "$probe_rc" -eq 0 ]; then
        probe_class="permanent"; probe_error_class=schema
      else
        probe_class=$(classify_failure "$probe_combined"); probe_error_class=$(classify_error "$probe_combined")
      fi
      probe_cache_usage=none
      probe_outcome="failed"
    fi
    record_telemetry model_probe 1 1 "$probe_outcome" "$probe_class" "$probe_rc" \
      "$probe_started" "$probe_started_epoch" "$probe_ended" "$probe_ended_epoch" \
      "$PROBE_EVENTS" "$PROBE_RESPONSE" "$PROBE_STDERR" \
      "$PROBE_PROMPT" "$probe_cache_usage" "$probe_error_class" \
      || { FINAL_DETAIL="could not record model probe telemetry"; exit 1; }
    if [ "$probe_cancelled" -ne 0 ]; then
      FINAL_DETAIL="model probe interrupted by signal"
      exit "$probe_cancelled"
    fi
    if [ "$probe_valid" -ne 1 ]; then
      FINAL_DETAIL="model probe failed ($probe_class); no critic calls were started"
      echo "FATAL: $FINAL_DETAIL" >&2
      exit 2
    fi
    python3 - "$PROBE_CACHE" "$CRITIC_MODEL" "$CRITIC_EFFORT" "$CODEX_VERSION" <<'PY'
import datetime, json, os, pathlib, sys
path = pathlib.Path(sys.argv[1]); tmp = pathlib.Path(str(path) + ".tmp." + str(os.getpid()))
obj = {"cache_version":"i4r-model-probe-1", "status":"ok", "model":sys.argv[2],
       "effort":sys.argv[3], "codex_version":sys.argv[4],
       "checked_at":datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n"); os.replace(tmp, path)
PY
    if [ $? -ne 0 ]; then FINAL_DETAIL="could not publish model probe cache"; exit 1; fi
  fi
  release_probe_lock
fi

FINAL_DETAIL="model probe accepted; critics running"
write_status running 0 "" || { FINAL_DETAIL="status update after probe failed"; exit 1; }

BUDGET_ROOT="$OUT/attempt_budget"
CACHE_ROOT="$OUT/result_cache/$INPUT_SHA"
mkdir -p "$BUDGET_ROOT" "$CACHE_ROOT"

budget_used() {
  budget_name="$1"
  budget_file="$BUDGET_ROOT/$budget_name.count"
  budget_value=$("$HELPER" budget-used --ledger "$GLOBAL_TELEMETRY" --counter "$budget_file" \
    --legacy-root "$BUDGET_ROOT" --budget-role "$budget_name" --round "$ROUND") || return 1
  case "$budget_value" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s\n' "$budget_value"
}

claim_attempt() {
  budget_name="$1"
  used=$(budget_used "$budget_name")
  [ "$used" -lt "$MAX_TRIES" ] || return 1
  next=$((used + 1))
  atomic_text "$next" "$BUDGET_ROOT/$budget_name.count" || return 1
  printf '%s\n' "$next"
}

mark_permanent() {
  marker_name="$1"
  marker_detail="$2"
  atomic_text "$marker_detail" "$BUDGET_ROOT/$marker_name.permanent"
}

publish_role_cache() {
  publish_role="$1"
  publish_dir="$CACHE_ROOT/$publish_role"
  publish_conflict_marker="$CACHE_ROOT/${publish_role}.conflict"
  if [ -d "$publish_dir" ]; then
    if "$HELPER" verify-complete --sidecar "$publish_dir/complete.json" \
      --response "$publish_dir/output.json" --markdown "$publish_dir/output.md" \
      --role "$publish_role" --round "$ROUND" --input-sha "$INPUT_SHA" \
      --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" >/dev/null 2>&1; then
      if "$HELPER" critic-equivalent --left "$INV/results/critic_${publish_role}.json" \
        --right "$publish_dir/output.json" >/dev/null 2>&1 \
        && cmp -s "$INV/results/critic_${publish_role}.md" "$publish_dir/output.md"; then
        return 0
      fi
      conflict_root="$CACHE_ROOT/conflicts/$publish_role"
      conflict_stage="$conflict_root/.${INVOCATION_ID}.$RANDOM.tmp"
      conflict_dir="$conflict_root/$INVOCATION_ID"
      mkdir -p "$conflict_root" || return 1
      mkdir "$conflict_stage" || return 1
      cp "$INV/results/critic_${publish_role}.json" "$conflict_stage/output.json" || return 1
      cp "$INV/results/critic_${publish_role}.md" "$conflict_stage/output.md" || return 1
      cp "$INV/results/${publish_role}.complete.json" "$conflict_stage/complete.json" || return 1
      chmod 444 "$conflict_stage"/* 2>/dev/null || true
      chmod 555 "$conflict_stage" 2>/dev/null || true
      mv "$conflict_stage" "$conflict_dir" || return 1
      atomic_text "valid same-input outputs differed; first cache retained; conflict=$conflict_dir" \
        "$publish_conflict_marker" || return 1
      return 2
    fi
    mv "$publish_dir" "$publish_dir.invalid.$INVOCATION_ID.$RANDOM" || return 1
  elif [ -e "$publish_dir" ]; then
    mv "$publish_dir" "$publish_dir.invalid.$INVOCATION_ID.$RANDOM" || return 1
  fi
  publish_stage="$CACHE_ROOT/.${publish_role}.${INVOCATION_ID}.$RANDOM.tmp"
  mkdir "$publish_stage" || return 1
  cp "$INV/results/critic_${publish_role}.json" "$publish_stage/output.json" || return 1
  cp "$INV/results/critic_${publish_role}.md" "$publish_stage/output.md" || return 1
  cp "$INV/results/${publish_role}.complete.json" "$publish_stage/complete.json" || return 1
  chmod 444 "$publish_stage"/* 2>/dev/null || true
  mv "$publish_stage" "$publish_dir" || return 1
  chmod 555 "$publish_dir" 2>/dev/null || true
}

load_role_cache() {
  cached_role="$1"
  [ "$FORCE_RERUN" -eq 0 ] || return 1
  cached_dir="$CACHE_ROOT/$cached_role"
  [ -s "$cached_dir/output.json" ] && [ -s "$cached_dir/complete.json" ] || return 1
  "$HELPER" verify-complete --sidecar "$cached_dir/complete.json" \
    --response "$cached_dir/output.json" --markdown "$cached_dir/output.md" \
    --role "$cached_role" --round "$ROUND" --input-sha "$INPUT_SHA" \
    --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" >/dev/null 2>&1 || return 1
  atomic_copy "$cached_dir/output.json" "$INV/results/critic_${cached_role}.json" || return 1
  atomic_copy "$cached_dir/output.md" "$INV/results/critic_${cached_role}.md" || return 1
  cached_attempts=$(python3 - "$cached_dir/complete.json" <<'PY'
import json, pathlib, sys
value=json.loads(pathlib.Path(sys.argv[1]).read_text()).get("attempts")
if not isinstance(value,int) or not 1 <= value <= 3: raise SystemExit(1)
print(value)
PY
  ) || return 1
  "$HELPER" complete --output "$INV/results/${cached_role}.complete.json" \
    --input-sha "$INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" --round "$ROUND" \
    --role "$cached_role" --attempts "$cached_attempts" \
    --response "$INV/results/critic_${cached_role}.json" \
    --markdown "$INV/results/critic_${cached_role}.md" || return 1
  cache_time=$(utc_now); cache_epoch=$(epoch_now)
  record_telemetry "$cached_role" 0 0 cache_hit "" 0 \
    "$cache_time" "$cache_epoch" "$cache_time" "$cache_epoch" "" \
    "$INV/results/critic_${cached_role}.json" "" \
    "$SNAPSHOT/rendered/prompt_${cached_role}.txt" read ""
}

run_one() (
  trap - EXIT INT TERM HUP
  IN_PROVIDER_WORKER=1
  role="$1"
  PROVIDER_SLOT="$role"
  if [ -f "$CACHE_ROOT/${role}.conflict" ]; then
    echo "  [stop ] critic_$role has unresolved same-input cache conflicts" >&2
    exit 1
  fi
  if load_role_cache "$role"; then
    echo "  [cache] critic_$role"
    exit 0
  fi
  if [ "$FORCE_RERUN" -eq 0 ] && [ -f "$BUDGET_ROOT/$role.permanent" ]; then
    used=$(budget_used "$role")
    "$HELPER" failure --output "$INV/results/${role}.failure.json" --role "$role" \
      --attempts "$used" --failure-class permanent --log "$BUDGET_ROOT/$role.permanent" >/dev/null 2>&1 || true
    echo "  [stop ] critic_$role has a persistent permanent failure" >&2
    exit 1
  fi

  while :; do
    attempt=$(claim_attempt "$role") || {
      used=$(budget_used "$role")
      "$HELPER" failure --output "$INV/results/${role}.failure.json" --role "$role" \
        --attempts "$used" --failure-class budget_exhausted \
        --log "$BUDGET_ROOT/$role.count" >/dev/null 2>&1 || true
      echo "  [stop ] critic_$role exhausted its global $MAX_TRIES-attempt budget" >&2
      exit 1
    }
    attempt_dir="$INV/attempts/$role/$attempt"
    mkdir -p "$attempt_dir"
    prompt="$SNAPSHOT/rendered/prompt_${role}.txt"
    base_schema="$SNAPSHOT/schemas/critic_${role}.schema.json"
    schema="$attempt_dir/output.schema.json"
    response="$attempt_dir/response.json"
    events="$attempt_dir/events.jsonl"
    stderr_file="$attempt_dir/stderr.txt"
    markdown="$attempt_dir/response.md"
    validation="$attempt_dir/validation.txt"
    echo "  [run  ] critic_$role attempt $attempt/$MAX_TRIES"
    call_started=$(utc_now); call_started_epoch=$(epoch_now)
    ACTIVE_CALL_ID="${INVOCATION_ID}:${role}:${attempt}:${call_started_epoch}:$RANDOM"
    "$HELPER" bind-contract --base "$base_schema" --output "$schema" \
      --invocation-id "$ACTIVE_CALL_ID" > "$validation" 2>&1 || exit 1
    codex_exec "$prompt" "$schema" "$response" "$events" "$stderr_file"
    call_rc=$?
    call_cancelled=$CODEX_CANCELLED
    valid=0
    if [ "$call_rc" -eq 0 ] && [ -s "$response" ]; then
      "$HELPER" validate-critic --input "$response" --role "$role" --round "$ROUND" \
        --input-sha "$INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" \
        --schema "$schema" --markdown "$markdown" \
        > "$validation" 2>&1 && valid=1
    else
      printf 'provider exit=%s; response missing or empty\n' "$call_rc" > "$validation"
    fi
    call_ended=$(utc_now); call_ended_epoch=$(epoch_now)
    if [ "$valid" -eq 1 ]; then
      record_telemetry "$role" "$attempt" 1 complete "" "$call_rc" \
        "$call_started" "$call_started_epoch" "$call_ended" "$call_ended_epoch" \
        "$events" "$response" "$stderr_file" "$prompt" write "" || {
          mark_permanent "$role" "telemetry write failed after a valid provider response"
          exit 1
        }
      atomic_copy "$response" "$INV/results/critic_${role}.json" || exit 1
      atomic_copy "$markdown" "$INV/results/critic_${role}.md" || exit 1
      "$HELPER" complete --output "$INV/results/${role}.complete.json" \
        --input-sha "$INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" --round "$ROUND" \
        --role "$role" --attempts "$attempt" \
        --response "$INV/results/critic_${role}.json" \
        --markdown "$INV/results/critic_${role}.md" || exit 1
      publish_role_cache "$role"
      publish_rc=$?
      if [ "$publish_rc" -eq 2 ]; then
        echo "  [fail ] critic_$role produced a conflicting valid output for the same input" >&2
        exit 1
      fi
      [ "$publish_rc" -eq 0 ] || exit 1
      echo "  [ok   ] critic_$role"
      exit 0
    fi

    combined="$attempt_dir/failure.log"
    { sed -n '1,200p' "$validation" 2>/dev/null; \
      sed -n '1,200p' "$stderr_file" 2>/dev/null; \
      sed -n '1,200p' "$events" 2>/dev/null; } > "$combined"
    if [ "$call_cancelled" -ne 0 ]; then
      printf 'provider cancelled by signal (exit %s)\n' "$call_cancelled" >> "$combined"
      failure_class=transient
      failure_outcome=cancelled
      error_class=transient
    elif [ "$call_rc" -eq 0 ]; then
      failure_class=permanent
      failure_outcome=schema_invalid
      error_class=schema
    else
      failure_class=$(classify_failure "$combined")
      failure_outcome=failed
      error_class=$(classify_error "$combined")
    fi
    record_telemetry "$role" "$attempt" 1 "$failure_outcome" "$failure_class" "$call_rc" \
      "$call_started" "$call_started_epoch" "$call_ended" "$call_ended_epoch" \
      "$events" "$response" "$stderr_file" "$prompt" none "$error_class" || {
        mark_permanent "$role" "telemetry write failed after provider failure"
        exit 1
      }
    if [ "$call_cancelled" -ne 0 ]; then
      "$HELPER" failure --output "$INV/results/${role}.failure.json" --role "$role" \
        --attempts "$attempt" --failure-class cancelled --log "$combined" >/dev/null 2>&1 || true
      exit "$call_cancelled"
    fi
    if [ "$failure_class" = permanent ]; then
      mark_permanent "$role" "$(sed -n '1,40p' "$combined")"
      "$HELPER" failure --output "$INV/results/${role}.failure.json" --role "$role" \
        --attempts "$attempt" --failure-class permanent --log "$combined" >/dev/null 2>&1 || true
      echo "  [fail ] critic_$role permanent failure on attempt $attempt" >&2
      exit 1
    fi
    if [ "$attempt" -ge "$MAX_TRIES" ]; then
      "$HELPER" failure --output "$INV/results/${role}.failure.json" --role "$role" \
        --attempts "$attempt" --failure-class transient_exhausted --log "$combined" >/dev/null 2>&1 || true
      echo "  [fail ] critic_$role exhausted transient attempts" >&2
      exit 1
    fi
    retry_delay=$((RETRY_BACKOFF_SECONDS * attempt))
    if [ "$retry_delay" -gt 0 ]; then
      echo "  [wait ] critic_$role transient failure; retrying in ${retry_delay}s"
      sleep "$retry_delay"
    fi
  done
)

echo "=== Round $ROUND: $ROLE_COUNT exact roles, ${PARALLEL}-up; input ${INPUT_SHA%${INPUT_SHA#????????????}} ==="
batch_pids=""
batch_roles=""
batch_count=0
batch_failed=0
all_failed=0
for role in $NAMES; do
  run_one "$role" &
  role_pid=$!
  batch_pids="$batch_pids $role_pid"
  ACTIVE_WORKER_PIDS="$batch_pids"
  batch_roles="$batch_roles $role"
  batch_count=$((batch_count + 1))
  if [ "$batch_count" -ge "$PARALLEL" ]; then
    for wait_pid in $batch_pids; do
      wait "$wait_pid" || batch_failed=1
    done
    ACTIVE_WORKER_PIDS=""
    if [ "$batch_failed" -ne 0 ]; then
      all_failed=1
      break
    fi
    batch_pids=""; batch_roles=""; batch_count=0; batch_failed=0
  fi
done
if [ "$all_failed" -eq 0 ] && [ "$batch_count" -gt 0 ]; then
  for wait_pid in $batch_pids; do
    wait "$wait_pid" || batch_failed=1
  done
  ACTIVE_WORKER_PIDS=""
  [ "$batch_failed" -eq 0 ] || all_failed=1
fi

missing_roles=""
for role in $NAMES; do
  if ! "$HELPER" verify-complete --sidecar "$INV/results/${role}.complete.json" \
    --response "$INV/results/critic_${role}.json" --markdown "$INV/results/critic_${role}.md" \
    --role "$role" --round "$ROUND" --input-sha "$INPUT_SHA" \
    --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" >/dev/null 2>&1; then
    missing_roles="$missing_roles $role"
  fi
done
if [ "$all_failed" -ne 0 ] || [ -n "$missing_roles" ]; then
  FINAL_DETAIL="exact role quorum failed; missing:${missing_roles:- unknown}"
  FINAL_ADJUDICATION="not_reached"
  echo "FATAL: $FINAL_DETAIL" >&2
  exit 1
fi

# Routine model synthesis is intentionally absent. The deterministic packet invokes a separate
# Terra adjudicator only when the machine policy identifies a targeted trigger.
if [ -n "${ADJUDICATE+x}" ]; then
  ADJUDICATION_MODE="$ADJUDICATE"
elif [ -n "${SYNTH+x}" ]; then
  echo "  [note ] SYNTH is deprecated; use ADJUDICATE" >&2
  if [ "$SYNTH" = "1" ]; then ADJUDICATION_MODE=1; else ADJUDICATION_MODE=0; fi
else
  ADJUDICATION_MODE=auto
fi
case "$ADJUDICATION_MODE" in 0|1|auto) ;; *) FINAL_DETAIL="ADJUDICATE must be 0, auto, or 1"; exit 1 ;; esac

PACKET="$INV/adjudication_packet.json"
TRIGGER_COUNT=$("$HELPER" packet --results "$INV/results" --output "$PACKET" \
  --round "$ROUND" --input-sha "$INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" \
  --workspace "$WS" --mode "$ADJUDICATION_MODE")
if [ $? -ne 0 ]; then FINAL_DETAIL="deterministic adjudication packet failed"; exit 1; fi

TARGET_INPUT_SHA=""
TARGET_SNAPSHOT=""
TARGET_SNAPSHOT_REL=""
TARGET_PROMPT=""
TARGET_SCHEMA=""
if [ "$ADJUDICATION_MODE" = 1 ] \
  || { [ "$ADJUDICATION_MODE" = auto ] && [ "$TRIGGER_COUNT" -gt 0 ]; }; then
  TARGET_META="$INV/target_snapshot.json"
  "$HELPER" target-snapshot --workspace "$WS" --out "$OUT" --round-snapshot "$SNAPSHOT" \
    --packet "$PACKET" --schema "$SNAPSHOT/schemas/targeted_adjudication.schema.json" \
    --model "$ADJUDICATOR_MODEL" --effort "$ADJUDICATOR_EFFORT" \
    --runtime-script "$SCRIPT_DIR/run_critique.sh" > "$TARGET_META"
  if [ $? -ne 0 ]; then FINAL_DETAIL="immutable targeted-adjudication snapshot failed"; exit 1; fi
  TARGET_INPUT_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["target_input_sha256"])' "$TARGET_META")
  TARGET_SNAPSHOT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["snapshot"])' "$TARGET_META")
  TARGET_SNAPSHOT_REL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["snapshot_rel"])' "$TARGET_META")
  TARGET_PROMPT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["prompt"])' "$TARGET_META")
  TARGET_SCHEMA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["schema"])' "$TARGET_META")
  [ -n "$TARGET_INPUT_SHA" ] && [ -s "$TARGET_PROMPT" ] && [ -s "$TARGET_SCHEMA" ] \
    || { FINAL_DETAIL="target snapshot metadata is incomplete"; exit 1; }
fi

run_targeted_adjudication() (
  trap - EXIT INT TERM HUP
  IN_PROVIDER_WORKER=1
  PROVIDER_SLOT="targeted_adjudicator"
  TELEMETRY_INPUT_SHA="$TARGET_INPUT_SHA"
  target_budget="targeted_adjudicator"
  target_cache_root="$OUT/adjudication_cache"
  target_cache="$target_cache_root/$TARGET_INPUT_SHA"
  mkdir -p "$target_cache_root"
  target_json="$INV/results/TARGETED_ADJUDICATION.json"
  target_md="$INV/results/TARGETED_ADJUDICATION.md"
  target_prompt="$TARGET_PROMPT"

  if [ -f "$target_cache.conflict" ]; then
    echo "  [stop ] targeted adjudication has unresolved same-input cache conflicts" >&2
    exit 1
  fi

  if [ "$FORCE_RERUN" -eq 0 ] && [ -s "$target_cache/output.json" ]; then
    cached_md="$INV/results/.targeted.cache.$$.md"
    if "$HELPER" validate-adjudication --input "$target_cache/output.json" \
      --packet "$PACKET" --input-sha "$TARGET_INPUT_SHA" \
      --markdown "$cached_md" >/dev/null 2>&1; then
      atomic_copy "$target_cache/output.json" "$target_json" || exit 1
      mv -f "$cached_md" "$target_md" || exit 1
      target_cached_attempts=$(python3 - "$target_cache/complete.json" <<'PY'
import json, pathlib, sys
value=json.loads(pathlib.Path(sys.argv[1]).read_text()).get("attempts")
if not isinstance(value,int) or not 1 <= value <= 3: raise SystemExit(1)
print(value)
PY
      ) || exit 1
      "$HELPER" complete --output "$INV/results/targeted_adjudicator.complete.json" \
        --input-sha "$TARGET_INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" --round "$ROUND" \
        --role targeted_adjudicator --attempts "$target_cached_attempts" \
        --response "$target_json" --markdown "$target_md" || exit 1
      cache_time=$(utc_now); cache_epoch=$(epoch_now)
      record_telemetry targeted_adjudicator 0 0 cache_hit "" 0 \
        "$cache_time" "$cache_epoch" "$cache_time" "$cache_epoch" "" "$target_json" "" \
        "$target_prompt" read "" || exit 1
      exit 0
    fi
  fi
  if [ "$FORCE_RERUN" -eq 0 ] && [ -f "$BUDGET_ROOT/$target_budget.permanent" ]; then exit 1; fi

  while :; do
    attempt=$(claim_attempt "$target_budget") || exit 1
    attempt_dir="$INV/attempts/targeted_adjudicator/$attempt"
    mkdir -p "$attempt_dir"
    response="$attempt_dir/response.json"
    events="$attempt_dir/events.jsonl"
    stderr_file="$attempt_dir/stderr.txt"
    markdown="$attempt_dir/response.md"
    validation="$attempt_dir/validation.txt"
    call_started=$(utc_now); call_started_epoch=$(epoch_now)
    ACTIVE_CALL_ID="${INVOCATION_ID}:targeted_adjudicator:${attempt}:${call_started_epoch}:$RANDOM"
    attempt_schema="$attempt_dir/output.schema.json"
    "$HELPER" bind-contract --base "$TARGET_SCHEMA" --output "$attempt_schema" \
      --invocation-id "$ACTIVE_CALL_ID" > "$validation" 2>&1 || exit 1
    codex_exec "$target_prompt" "$attempt_schema" \
      "$response" "$events" "$stderr_file" "$ADJUDICATOR_MODEL" "$ADJUDICATOR_EFFORT"
    call_rc=$?
    call_cancelled=$CODEX_CANCELLED
    valid=0
    if [ "$call_rc" -eq 0 ] && [ -s "$response" ]; then
      "$HELPER" validate-adjudication --input "$response" --packet "$PACKET" \
        --input-sha "$TARGET_INPUT_SHA" --schema "$attempt_schema" \
        --markdown "$markdown" > "$validation" 2>&1 && valid=1
    else
      printf 'provider exit=%s; response missing or empty\n' "$call_rc" > "$validation"
    fi
    call_ended=$(utc_now); call_ended_epoch=$(epoch_now)
    if [ "$valid" -eq 1 ]; then
      record_telemetry targeted_adjudicator "$attempt" 1 complete "" "$call_rc" \
        "$call_started" "$call_started_epoch" "$call_ended" "$call_ended_epoch" \
        "$events" "$response" "$stderr_file" "$target_prompt" write "" || exit 1
      atomic_copy "$response" "$target_json" || exit 1
      atomic_copy "$markdown" "$target_md" || exit 1
      "$HELPER" complete --output "$INV/results/targeted_adjudicator.complete.json" \
        --input-sha "$TARGET_INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" --round "$ROUND" \
        --role targeted_adjudicator --attempts "$attempt" \
        --response "$target_json" --markdown "$target_md" || exit 1
      if [ -d "$target_cache" ]; then
        existing_md="$INV/results/.targeted.existing.$$.md"
        if "$HELPER" validate-adjudication --input "$target_cache/output.json" \
          --packet "$PACKET" --input-sha "$TARGET_INPUT_SHA" \
          --markdown "$existing_md" >/dev/null 2>&1; then
          if "$HELPER" critic-equivalent --left "$target_json" \
            --right "$target_cache/output.json" >/dev/null 2>&1 \
            && cmp -s "$target_md" "$existing_md"; then
            rm -f "$existing_md" 2>/dev/null || true
            exit 0
          fi
          conflict_root="$target_cache_root/conflicts/$TARGET_INPUT_SHA"
          conflict_stage="$conflict_root/.${INVOCATION_ID}.$RANDOM.tmp"
          conflict_dir="$conflict_root/$INVOCATION_ID"
          mkdir -p "$conflict_root" || exit 1
          mkdir "$conflict_stage" || exit 1
          cp "$target_json" "$conflict_stage/output.json" || exit 1
          cp "$target_md" "$conflict_stage/output.md" || exit 1
          cp "$INV/results/targeted_adjudicator.complete.json" "$conflict_stage/complete.json" || exit 1
          chmod 444 "$conflict_stage"/* 2>/dev/null || true
          chmod 555 "$conflict_stage" 2>/dev/null || true
          mv "$conflict_stage" "$conflict_dir" || exit 1
          atomic_text "valid same-input targeted outputs differed; first cache retained; conflict=$conflict_dir" \
            "$target_cache.conflict" || exit 1
          echo "  [fail ] targeted adjudication conflicted with its immutable cache" >&2
          exit 1
        fi
        mv "$target_cache" "$target_cache.invalid.$INVOCATION_ID.$RANDOM" || exit 1
      elif [ -e "$target_cache" ]; then
        mv "$target_cache" "$target_cache.invalid.$INVOCATION_ID.$RANDOM" || exit 1
      fi
      target_stage="$target_cache_root/.${TARGET_INPUT_SHA}.${INVOCATION_ID}.$RANDOM.tmp"
      mkdir "$target_stage" || exit 1
      cp "$target_json" "$target_stage/output.json" || exit 1
      cp "$target_md" "$target_stage/output.md" || exit 1
      cp "$INV/results/targeted_adjudicator.complete.json" "$target_stage/complete.json" || exit 1
      chmod 444 "$target_stage"/* 2>/dev/null || true
      chmod 555 "$target_stage" 2>/dev/null || true
      mv "$target_stage" "$target_cache" || exit 1
      exit 0
    fi
    combined="$attempt_dir/failure.log"
    { sed -n '1,200p' "$validation" 2>/dev/null; sed -n '1,200p' "$stderr_file" 2>/dev/null; \
      sed -n '1,200p' "$events" 2>/dev/null; } > "$combined"
    if [ "$call_cancelled" -ne 0 ]; then
      printf 'provider cancelled by signal (exit %s)\n' "$call_cancelled" >> "$combined"
      failure_class=transient; failure_outcome=cancelled; error_class=transient
    elif [ "$call_rc" -eq 0 ]; then
      failure_class=permanent; failure_outcome=schema_invalid; error_class=schema
    else
      failure_class=$(classify_failure "$combined"); failure_outcome=failed; error_class=$(classify_error "$combined")
    fi
    record_telemetry targeted_adjudicator "$attempt" 1 "$failure_outcome" "$failure_class" "$call_rc" \
      "$call_started" "$call_started_epoch" "$call_ended" "$call_ended_epoch" \
      "$events" "$response" "$stderr_file" "$target_prompt" none "$error_class" || exit 1
    [ "$call_cancelled" -eq 0 ] || exit "$call_cancelled"
    if [ "$failure_class" = permanent ]; then
      mark_permanent "$target_budget" "$(sed -n '1,40p' "$combined")"
      exit 1
    fi
    [ "$attempt" -lt "$MAX_TRIES" ] || exit 1
    retry_delay=$((RETRY_BACKOFF_SECONDS * attempt))
    [ "$retry_delay" -le 0 ] || sleep "$retry_delay"
  done
)

execute_targeted_adjudication() {
  run_targeted_adjudication &
  targeted_pid=$!
  ACTIVE_WORKER_PIDS="$targeted_pid"
  wait "$targeted_pid"
  targeted_rc=$?
  ACTIVE_WORKER_PIDS=""
  return "$targeted_rc"
}

case "$ADJUDICATION_MODE" in
  0)
    FINAL_ADJUDICATION="coordinator_packet"
    ;;
  auto)
    if [ "$TRIGGER_COUNT" -gt 0 ]; then
      FINAL_ADJUDICATION="running"
      FINAL_DETAIL="critic quorum met; targeted adjudication running"
      write_status running 0 "" || exit 1
      execute_targeted_adjudication || {
        FINAL_ADJUDICATION="failed"
        FINAL_DETAIL="targeted adjudication failed"
        exit 1
      }
      FINAL_ADJUDICATION="complete"
    else
      FINAL_ADJUDICATION="no_triggers"
    fi
    ;;
  1)
    FINAL_ADJUDICATION="running"
    FINAL_DETAIL="critic quorum met; forced targeted adjudication running"
    write_status running 0 "" || exit 1
    execute_targeted_adjudication || {
      FINAL_ADJUDICATION="failed"
      FINAL_DETAIL="forced targeted adjudication failed"
      exit 1
    }
    FINAL_ADJUDICATION="complete"
    ;;
esac

PROJECTION_META="$INV/projection.json"
if [ "$FINAL_ADJUDICATION" = complete ]; then
  "$HELPER" projection --out "$OUT" --results "$INV/results" --invocation "$INVOCATION_ID" \
    --input-sha "$INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" --round "$ROUND" \
    --roles "$NAMES" --adjudication "$FINAL_ADJUDICATION" --routing "$ROUTING" --packet "$PACKET" \
    --target-input-sha "$TARGET_INPUT_SHA" > "$PROJECTION_META"
else
  "$HELPER" projection --out "$OUT" --results "$INV/results" --invocation "$INVOCATION_ID" \
    --input-sha "$INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" --round "$ROUND" \
    --roles "$NAMES" --adjudication "$FINAL_ADJUDICATION" --routing "$ROUTING" --packet "$PACKET" \
    > "$PROJECTION_META"
fi
if [ $? -ne 0 ]; then FINAL_DETAIL="immutable round projection failed"; exit 1; fi
PROJECTION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["projection"])' "$PROJECTION_META")
[ -d "$PROJECTION" ] || { FINAL_DETAIL="round projection metadata is incomplete"; exit 1; }

# Root-level files are compatibility projections for existing release tooling. Retire every prior
# file first, then populate only this invocation's set from the verified immutable projection.
STALE_PROJECTIONS="$INV/stale_projections"
mkdir -p "$STALE_PROJECTIONS" || { FINAL_DETAIL="failed to create projection archive"; exit 1; }
for projection_role in stats data fairness completeness; do
  for projection_suffix in json md; do
    projection_name="critic_${projection_role}.${projection_suffix}"
    if [ -e "$OUT/$projection_name" ]; then
      mv "$OUT/$projection_name" "$STALE_PROJECTIONS/$projection_name" || {
        FINAL_DETAIL="failed to archive stale $projection_name"; exit 1;
      }
    fi
  done
done
for critique_projection in CRITIQUE.json CRITIQUE.md; do
  if [ -e "$OUT/$critique_projection" ]; then
    mv "$OUT/$critique_projection" "$STALE_PROJECTIONS/$critique_projection" || {
      FINAL_DETAIL="failed to archive stale $critique_projection"; exit 1;
    }
  fi
done
for role in $NAMES; do
  atomic_copy "$PROJECTION/critic_${role}.json" "$OUT/critic_${role}.json" || {
    FINAL_DETAIL="failed to project critic_$role JSON"; exit 1;
  }
  atomic_copy "$PROJECTION/critic_${role}.md" "$OUT/critic_${role}.md" || {
    FINAL_DETAIL="failed to project critic_$role Markdown"; exit 1;
  }
done
if [ "$FINAL_ADJUDICATION" = complete ]; then
  atomic_copy "$PROJECTION/CRITIQUE.json" "$OUT/CRITIQUE.json" \
    || { FINAL_DETAIL="failed to project targeted adjudication JSON"; exit 1; }
  atomic_copy "$PROJECTION/CRITIQUE.md" "$OUT/CRITIQUE.md" \
    || { FINAL_DETAIL="failed to project targeted adjudication Markdown"; exit 1; }
fi
atomic_copy "$PROJECTION/ROUTING.json" "$OUT/CURRENT_ROUTING.json" \
  || { FINAL_DETAIL="failed to project routing"; exit 1; }
atomic_copy "$PROJECTION/adjudication_packet.json" "$OUT/CURRENT_ADJUDICATION_PACKET.json" \
  || { FINAL_DETAIL="failed to project adjudication packet"; exit 1; }
atomic_copy "$PROJECTION/adjudication_packet.json" "$OUT/adjudication_packet.json" \
  || { FINAL_DETAIL="failed to project adjudication packet"; exit 1; }
atomic_text "$INVOCATION_ID" "$OUT/CURRENT_PROJECTION" \
  || { FINAL_DETAIL="failed to publish current projection pointer"; exit 1; }

FINAL_OUTCOME="complete"
FINAL_DETAIL="exact role quorum met; routine synthesis omitted; adjudication=$FINAL_ADJUDICATION"
echo "=== Round $ROUND complete: roles=$NAMES; adjudication=$FINAL_ADJUDICATION ==="
exit 0
