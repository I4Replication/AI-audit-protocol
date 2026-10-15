#!/bin/bash
# Run an immutable, schema-gated critique round.
#
# Usage:
#   AUDIT_DIR=/path/to/audit bash scripts/run_critique.sh ROUND
#
# Optional controls:
#   MAX_TRIES=N           attempts per role per round (default 3, max 20). THIS is the knob for a
#                         critique round -- I4R_MAX_ATTEMPTS is exported from it below, so setting
#                         that variable from outside has no effect here.
#   CRITICS="stats data fairness completeness"   round 2+ only; round 1 must be the exact four
#   PARALLEL=N            concurrent roles
#   DIFF_MANIFEST=/path/to/manifest.json         required for a later-round delta review
#   ADJUDICATE=0|auto|1   (default auto: call the adjudicator model only for policy triggers)
#   I4R_AUTO_AUTHORIZE    1 (default) journals a grant for the driver's own unmodified plan so a
#                         run completes every round without a human pause. Set 0 to restore the
#                         attended pause, which exits 3 and waits for lifecycle_journal.py
#                         authorize-round. The grant is journaled and bound to the exact generated
#                         plan either way; only the pause differs.
#   I4R_FORCE_RERUN=1     ignore cached role results for this round (a finished metadata-2.1
#                         round is reopened under its live grant so the roles can re-dispatch)
#   I4R_PROVIDER_TIMEOUT_SECONDS
#                         wall-clock ceiling per provider call; a call that exceeds it is stopped and
#                         classified transient. An audit initialised on 2.7.4 or later froze its own
#                         ceiling (5400) and this may only lower it; an older audit runs at 2700 and
#                         this may raise or lower it (scripts/critique_timeouts.py)
#
# NOT controls, despite being read: CRITIC_MODEL, CRITIC_EFFORT, ADJUDICATOR_MODEL,
# ADJUDICATOR_EFFORT, MODEL, EFFORT. All are pinned by the audit-scoped policy snapshot -- any
# value other than the built-in default is a fail_early, so they can only restate the default.
# They are read so that a mismatch is caught loudly rather than silently honoured.
#
# Every provider call is made from an immutable content-addressed snapshot and must return a
# schema-valid JSON final response. Attempt counts persist by round and role, so restarting the
# driver or changing a same-round input snapshot cannot reset the three-attempt ceiling.
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

[ -n "${AUDIT_DIR:-}" ] || fail_early "set AUDIT_DIR to the audit workspace"
[ -d "$AUDIT_DIR" ] || fail_early "AUDIT_DIR is not a directory: $AUDIT_DIR"
WS=$(CDPATH= cd -- "$AUDIT_DIR" 2>/dev/null && pwd) || fail_early "cannot resolve AUDIT_DIR"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) \
  || fail_early "cannot resolve script directory"
SKILL=$(CDPATH= cd -- "$SCRIPT_DIR/.." 2>/dev/null && pwd) \
  || fail_early "cannot resolve skill directory"
HELPER="$SCRIPT_DIR/test_run_critique_runtime.py"
PROMPTS="$SKILL/prompts"
# The Codex CLI: CODEX, then I4R_CODEX_BIN (capture_environment.sh reads the same two, in the same
# order), else the newest working CLI that scripts/codex_cli.py finds among the ChatGPT app's bundled
# copies and every codex on PATH. A literal default used to name the pre-2026-09-28 bundle path,
# which the ChatGPT app update moved, so every round on an updated Mac stopped here.
CODEX_SEARCH_NOTE=""
if [ -z "${CODEX:-}" ] && [ -z "${I4R_CODEX_BIN:-}" ]; then
  CODEX=$(python3 "$SCRIPT_DIR/codex_cli.py" 2>/dev/null) || CODEX=""
  [ -n "$CODEX" ] || CODEX_SEARCH_NOTE=$(python3 "$SCRIPT_DIR/codex_cli.py" 2>&1 >/dev/null)
fi
CODEX="${CODEX:-${I4R_CODEX_BIN:-}}"
# The critic and adjudicator route is pinned by THIS AUDIT's frozen policy snapshot, written at
# init_run.sh, not by a literal here, so an audit keeps the route it was initialised with even when
# the programme's policy changes (2.6.0 moved new audits to GPT-6 Sol; 2.6.1 moved them back to
# Terra because Sol is not available on every ChatGPT account). Since 2.7.5 an account that cannot use the
# pinned model is changed, not the audit's route: scripts/repin_critic_route.py moves an audit only to the
# shipped route, and an audit initialised on 2.7.5 or later carries critique.critic_route_lock (checked below).
# The skill's shipped policy is only the fallback for a workspace with no snapshot (fixtures).
POLICY_SNAPSHOT_FILE="$WS/pipeline-policy.snapshot.json"
policy_route () {  # <jq path> -> the value from the audit snapshot, else from the shipped policy
  local v=""
  if [ -f "$POLICY_SNAPSHOT_FILE" ]; then v=$(jq -r ".policy$1 // empty" "$POLICY_SNAPSHOT_FILE" 2>/dev/null); fi
  [ -n "$v" ] || v=$(jq -r "$1 // empty" "$SKILL/config/pipeline-policy-v2.json" 2>/dev/null)
  printf '%s' "$v"
}
# The round cap is this audit's own, frozen in its snapshot at init like the route (2.6.9 ships 7; an audit
# initialised earlier keeps 5 until scripts/amend_round_cap.py raises it with a journaled reason). A literal here
# used to ignore both. A finalized audit runs no further round until a journaled reopen. Both are refused before
# the round folder is created and before anything is built, journaled or paid for.
ROUND_CAP=$(policy_route .critique.round_cap)
case "$ROUND_CAP" in
  ''|*[!0-9]*) fail_early "could not read critique.round_cap from $POLICY_SNAPSHOT_FILE or the shipped policy" ;;
esac
round_refusal=$(python3 "$SCRIPT_DIR/round_cap_amendment.py" check-round "$WS" "$ROUND" 2>&1); round_refusal_rc=$?
if [ "$round_refusal_rc" -ne 0 ]; then
  [ -n "$round_refusal" ] || round_refusal="could not check round $ROUND against the round cap and journal of this audit (exit $round_refusal_rc)"
  fail_early "$round_refusal"
fi
# the same rule without a snapshot (a runtime fixture reads the shipped cap)
[ "$ROUND" -le "$ROUND_CAP" ] || fail_early "round $ROUND exceeds the round cap of $ROUND_CAP; stop with cap_with_unresolved"
POLICY_CRITIC_MODEL=$(policy_route .models.critics.requested_model)
POLICY_CRITIC_EFFORT=$(policy_route .models.critics.reasoning_effort)
POLICY_ADJUDICATOR_MODEL=$(policy_route .models.targeted_adjudicator.requested_model)
POLICY_ADJUDICATOR_EFFORT=$(policy_route .models.targeted_adjudicator.reasoning_effort)
[ -n "$POLICY_CRITIC_MODEL" ] && [ -n "$POLICY_ADJUDICATOR_MODEL" ] \
  || fail_early "could not read the critic route from $POLICY_SNAPSHOT_FILE or the shipped policy"
# 2.7.5: an audit initialised on 2.7.5 or later froze critique.critic_route_lock, the shipped critic model. Its
# critics, adjudicator and critique probe stay on it: a re-pin by an older copy of repin_critic_route.py (2.7.4 and
# earlier accepted any --critic-model) is refused here, before anything is built, journaled or paid for. An audit
# initialised earlier froze no lock and keeps the route it has.
CRITIC_ROUTE_LOCK=""
if [ -f "$POLICY_SNAPSHOT_FILE" ]; then
  CRITIC_ROUTE_LOCK=$(jq -r '.policy.critique.critic_route_lock.model // empty' "$POLICY_SNAPSHOT_FILE" 2>/dev/null)
fi
if [ -n "$CRITIC_ROUTE_LOCK" ]; then
  POLICY_PROBE_MODEL=$(jq -r '.policy.critique.model_probe.requested_model // empty' "$POLICY_SNAPSHOT_FILE" 2>/dev/null)
  if [ "$POLICY_CRITIC_MODEL" != "$CRITIC_ROUTE_LOCK" ] || [ "$POLICY_ADJUDICATOR_MODEL" != "$CRITIC_ROUTE_LOCK" ] \
     || [ "$POLICY_PROBE_MODEL" != "$CRITIC_ROUTE_LOCK" ]; then
    fail_early "this audit froze critique.critic_route_lock=$CRITIC_ROUTE_LOCK at init (2.7.5), but its snapshot now routes critics=$POLICY_CRITIC_MODEL, adjudicator=$POLICY_ADJUDICATOR_MODEL, probe=${POLICY_PROBE_MODEL:-<none>} (a re-pin by an older copy of repin_critic_route.py; policy.critic_route_history has it). Nothing was started. Restore the locked route with the canonical skill's python3 scripts/repin_critic_route.py \"\$AUDIT_DIR\" --reason \"restore the locked critic route\", and run the round from an account that can use $CRITIC_ROUTE_LOCK (python3 scripts/check_critic_access.py --force checks it)."
  fi
fi
CRITIC_MODEL="${CRITIC_MODEL:-${MODEL:-$POLICY_CRITIC_MODEL}}"
CRITIC_EFFORT="${CRITIC_EFFORT:-${EFFORT:-$POLICY_CRITIC_EFFORT}}"
ADJUDICATOR_MODEL="${ADJUDICATOR_MODEL:-$POLICY_ADJUDICATOR_MODEL}"
ADJUDICATOR_EFFORT="${ADJUDICATOR_EFFORT:-$POLICY_ADJUDICATOR_EFFORT}"
MAX_TRIES="${MAX_TRIES:-3}"
# Wall-clock ceiling for a single provider call, mirroring Phase 1's 45-minute per-attempt
# timeout. The critique path had none: a hung call stalled the round forever while holding
# the round lock, with nothing to break it but a human noticing.
# 2.7.4: the ceiling is this audit's own (scripts/critique_timeouts.py). A snapshot without
# critique.provider_timeout_seconds (every audit initialised before 2.7.4) keeps 2700 and the
# I4R_PROVIDER_TIMEOUT_SECONDS knob exactly as before; a frozen one (5400) may only be lowered by it.
if [ -f "$POLICY_SNAPSHOT_FILE" ]; then TIMEOUT_POLICY_FILE="$POLICY_SNAPSHOT_FILE"; else TIMEOUT_POLICY_FILE="$SKILL/config/pipeline-policy-v2.json"; fi
PROVIDER_TIMEOUT_SECONDS=$(python3 "$SCRIPT_DIR/critique_timeouts.py" "$TIMEOUT_POLICY_FILE" "${I4R_PROVIDER_TIMEOUT_SECONDS:-}" 2>&1) \
  || fail_early "$PROVIDER_TIMEOUT_SECONDS"
if jq -e '(.policy // .).critique | has("provider_timeout_seconds")' "$TIMEOUT_POLICY_FILE" > /dev/null 2>&1; then
  echo "  [limit] provider call ceiling ${PROVIDER_TIMEOUT_SECONDS} s (this audit's critique.provider_timeout_seconds)" >&2
fi
# Set when targeted adjudication fails after the critic quorum was already met. The round
# still exits non-zero, but only after the completed critic reviews have been journaled and
# projected: four valid, paid-for reviews must not be discarded because a later stage failed.
ADJUDICATION_FAILED=0
RETRY_BACKOFF_SECONDS="${RETRY_BACKOFF_SECONDS:-15}"
PROBE_LOCK_WAIT_SECONDS="${PROBE_LOCK_WAIT_SECONDS:-60}"
FORCE_RERUN="${I4R_FORCE_RERUN:-0}"

# The policy default is three total attempts per role. The operator may raise the ceiling (to 20,
# the same range as I4R_MAX_ATTEMPTS for Phase 1 and the coordinator): on audit
# A34 every exhausted budget was spent on harness defects, not on a critic failing at its task.
case "$MAX_TRIES" in ''|*[!0-9]*) fail_early "MAX_TRIES must be an integer from 1 to 20 (policy default 3)" ;; esac
[ "$MAX_TRIES" -ge 1 ] && [ "$MAX_TRIES" -le 20 ] \
  || fail_early "MAX_TRIES must be from 1 to 20 (policy default 3)"
# One knob governs the whole critique path. The runtime helper counts accepted inference starts
# per role under I4R_MAX_ATTEMPTS (default 3); audit A34 lost two complete
# fourth-attempt reviews because the driver allowed attempt 4 while the helper still refused to
# record it. The driver's ceiling is authoritative for its own helper calls.
export I4R_MAX_ATTEMPTS="$MAX_TRIES"
# A synced folder can give a frozen snapshot directory its write bit back seconds after the freeze
# (the Dropbox File Provider did, 2026-09-24 and 2026-09-27) while every byte still matches. With
# this set, the helper's reuse, post-call and cache checks re-freeze such mode-only drift in place
# and journal one note, instead of failing a paid response as schema-invalid. Content, inventory
# and manifest faults stay hard failures (exit SNAPSHOT_INTEGRITY_RC). The helper's library
# default, used by recovery scripts and compatibility sweeps, is strict and read-only.
export I4R_SNAPSHOT_REFREEZE=1
SNAPSHOT_INTEGRITY_RC=5
# The one command that removes a frozen snapshot: its 0555 directories and 0444 files make a plain
# `rm -rf` fail on every entry. $1 is the audit-relative snapshot directory.
snapshot_remedy() {
  printf 'in the audit directory run: chmod -R u+w %s && rm -rf %s (it is content-addressed and is rebuilt identically), then rerun' "$1" "$1"
}
# A helper exits SNAPSHOT_INTEGRITY_RC for a content change and also for a mode that would not stay
# frozen after bounded re-freezes (content verified). The second is not the snapshot's fault and
# deleting it cannot help, so it is worded apart: its message starts with snapshot_mode_unstable.
# Only helper-written logs are passed here, never provider output.
snapshot_mode_unstable_in() {
  for unstable_log in "$@"; do
    [ -n "$unstable_log" ] && [ -f "$unstable_log" ] || continue
    grep -q '^runtime environment error: snapshot_mode_unstable:' "$unstable_log" && return 0
  done
  return 1
}
SNAPSHOT_MODE_UNSTABLE_REMEDY="its content verified against its manifest, but its modes keep changing after repeated re-freezes; stop the sync client, or move the audit off the synced folder, then rerun (deleting the snapshot does not help)"
# The audit-relative snapshot directory a helper's integrity message names (round or target), or
# nothing. Every SnapshotIntegrityError the helper raises names the one it is about.
snapshot_named_in() {
  grep -o 'critique/round_[0-9][0-9]*/[a-z_]*snapshots/[0-9a-f]\{64\}' "$1" 2>/dev/null | head -1
}
case "$RETRY_BACKOFF_SECONDS" in ''|*[!0-9]*) fail_early "RETRY_BACKOFF_SECONDS must be an integer" ;; esac
case "$PROBE_LOCK_WAIT_SECONDS" in ''|*[!0-9]*) fail_early "PROBE_LOCK_WAIT_SECONDS must be an integer" ;; esac
case "$FORCE_RERUN" in 0|1) ;; *) fail_early "I4R_FORCE_RERUN must be 0 or 1" ;; esac
[ "$CRITIC_MODEL" = "$POLICY_CRITIC_MODEL" ] \
  || fail_early "critic model is pinned to $POLICY_CRITIC_MODEL by this audit's pipeline-policy.snapshot.json (got $CRITIC_MODEL)"
[ "$CRITIC_EFFORT" = "$POLICY_CRITIC_EFFORT" ] \
  || fail_early "ordinary critic and probe effort is pinned to $POLICY_CRITIC_EFFORT by this audit's policy snapshot (got $CRITIC_EFFORT)"
[ "$ADJUDICATOR_MODEL" = "$POLICY_ADJUDICATOR_MODEL" ] \
  || fail_early "targeted-adjudicator model is pinned to $POLICY_ADJUDICATOR_MODEL by this audit's pipeline-policy.snapshot.json (got $ADJUDICATOR_MODEL)"
[ "$ADJUDICATOR_EFFORT" = "$POLICY_ADJUDICATOR_EFFORT" ] \
  || fail_early "targeted-adjudicator effort is pinned to $POLICY_ADJUDICATOR_EFFORT by this audit's policy snapshot (got $ADJUDICATOR_EFFORT)"
[ -n "$CODEX" ] || fail_early "${CODEX_SEARCH_NOTE:-no working Codex CLI found}"
[ -x "$CODEX" ] || fail_early "Codex executable is not executable: $CODEX (unset CODEX and I4R_CODEX_BIN to let scripts/codex_cli.py find the newest working CLI; python3 scripts/codex_cli.py --explain lists the candidates)"
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

# Critic verdicts join the registry by id, and a renumbering after round 1 orphans them for good.
# Refuse round 1 while any included finding still carries a pre-renumbering id.
# a finished round 1 (integrated) is refused as finished further down; its withdrawal gap is not the cause
if [ "$ROUND" -eq 1 ] && [ -f "$WS/work/findings.jsonl" ] && [ ! -f "$WS/critique/round_1/INTEGRATION_SUMMARY.json" ]; then
  unrenumbered=$(python3 - "$WS/work/findings.jsonl" <<'PYIDS'
import json, re, sys
bad, included = [], []
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    row = json.loads(line)  # a malformed row fails the check rather than hiding an id
    if (row.get("ledger_disposition") or {}).get("status") != "INCLUDED":
        continue
    if re.fullmatch(r"F[1-9][0-9]*", str(row.get("id") or "")):
        included.append(int(row["id"][1:]))
    else:
        bad.append(str(row.get("id")))
if not bad and sorted(included) != list(range(1, len(included) + 1)):
    # F-shaped is not final: F1,F2,F7, a duplicate, or F1,F3 after F2 was merged at adjudication.
    bad = ["F%d" % n for n in sorted(included)]
print(" ".join(bad[:5]))
PYIDS
); ids_rc=$?
  [ "$ids_rc" -eq 0 ] || fail_early "could not check that finding ids are renumbered before round 1 (the check exited $ids_rc; see the error above)"
  [ -z "$unrenumbered" ] || fail_early "round 1 needs final, contiguous F<n> finding ids first (INCLUDED ids found: $unrenumbered). Run: python3 scripts/renumber_findings.py \"$AUDIT_DIR\" --apply --also report/ADJUDICATION.md"
fi

# checked again after dispatch, but a typo must fail before any critic is paid for
if [ -n "${ADJUDICATE+x}" ]; then   # set, even to the empty string (ADJUDICATE=$MODE with MODE unset)
  case "$ADJUDICATE" in 0|1|auto) ;; *) fail_early "ADJUDICATE must be 0, auto, or 1 (got '$ADJUDICATE')" ;; esac
fi
OUT="$WS/critique/round_${ROUND}"
mkdir -p "$OUT" || fail_early "cannot create output directory: $OUT"

# Only one driver may project CURRENT_* state for a round at a time. Preserve a stale lock for
# diagnosis instead of recursively deleting an unknown directory.
RUN_LOCK="$OUT/.runtime_lock"
# Every interrupted run moves its lock aside as .runtime_lock.stale.<ts>.<pid> and nothing
# ever removed them, so they accumulated for the life of the audit. Reap ones older than a
# day: anything younger might still belong to a run being diagnosed right now.
find "$OUT" -maxdepth 1 -type d -name ".runtime_lock.stale.*" -mtime +1 \
  -exec rm -rf {} + 2>/dev/null || true
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
# Remember the round's current pointer and status: a rerun that the journal refuses because the
# round is already finished must leave the finished round's record exactly as it was.
PRIOR_CURRENT_INVOCATION=""
[ -f "$OUT/CURRENT_INVOCATION" ] && PRIOR_CURRENT_INVOCATION=$(sed -n '1p' "$OUT/CURRENT_INVOCATION" 2>/dev/null || true)
PRIOR_STATUS_COPY=""
if [ -f "$OUT/ROUND_STATUS.json" ] && cp -p "$OUT/ROUND_STATUS.json" "$INV/prior_ROUND_STATUS.json" 2>/dev/null; then
  PRIOR_STATUS_COPY="$INV/prior_ROUND_STATUS.json"
fi
RESTORE_PRIOR_POINTER=0
RESTORE_NOTE=""
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
CODEX_TIMED_OUT=0
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

round_is_finished() {
  python3 - "$WS" "$ROUND" <<'PYFIN' 2>/dev/null
import json, pathlib, sys
path = pathlib.Path(sys.argv[1]) / "run.events.jsonl"
rnd = int(sys.argv[2])
finished = False
if path.is_file():
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (event.get("payload") or {}).get("round") != rnd:
            continue
        if event.get("type") in {"round_integrated", "round_end"}:
            finished = True
        elif event.get("type") == "round_start":
            # A forced rerun or adjudicator retry reopens the round (round_start reopened_from):
            # from then on its old "complete" status is not the truth, so it must not be restored.
            finished = False
sys.exit(0 if finished else 1)
PYFIN
}

# the round's critics already reached quorum since its last round_start (only an adjudicator retry reruns it)
round_has_quorum() {
  python3 - "$WS" "$ROUND" <<'PYQ' 2>/dev/null
import json, pathlib, sys
path = pathlib.Path(sys.argv[1]) / "run.events.jsonl"
rnd = int(sys.argv[2])
quorum = False
if path.is_file():
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or (event.get("payload") or {}).get("round") != rnd:
            continue
        if event.get("type") in {"round_quorum_complete", "round_integrated", "round_end"}:
            quorum = True
        elif event.get("type") == "round_start":
            quorum = False
sys.exit(0 if quorum else 1)
PYQ
}

finish_run() {
  finish_rc=$?
  trap - EXIT INT TERM HUP
  terminate_active_workers
  release_probe_lock
  if [ "$FINAL_OUTCOME" = "paused" ]; then
    [ "$finish_rc" -ne 0 ] || finish_rc=3
  elif [ "$FINAL_OUTCOME" = "quorum_complete_adjudication_failed" ]; then
    # A distinct terminal state, not a plain failure: the critic quorum was met, journaled and
    # projected, and only the adjudicator failed. Collapsing it to "failed" would hide that the
    # round's expensive reviews are on disk and that a rerun need only retry the adjudicator.
    [ "$finish_rc" -ne 0 ] || finish_rc=1
  elif [ "$FINAL_OUTCOME" != "complete" ]; then
    FINAL_OUTCOME="failed"
    [ "$finish_rc" -ne 0 ] || finish_rc=1
  elif [ "$finish_rc" -ne 0 ]; then
    FINAL_OUTCOME="failed"
  fi
  ended_at=$(utc_now)
  # Any unsuccessful rerun of a round the journal already records as finished (an authorization
  # refusal, a changed-evidence refusal, a crash) leaves that round's pointer and status as they were.
  if { [ "$FINAL_OUTCOME" = "failed" ] || [ "$FINAL_OUTCOME" = "quorum_complete_adjudication_failed" ]; } \
      && { [ -n "$PRIOR_CURRENT_INVOCATION" ] || [ -n "$PRIOR_STATUS_COPY" ]; } && round_is_finished; then
    # older rounds carry a status but no CURRENT_INVOCATION pointer: restore those too
    RESTORE_PRIOR_POINTER=1
  fi
  if [ "$STATUS_ACTIVE" -eq 1 ]; then
    if ! write_status "$FINAL_OUTCOME" "$finish_rc" "$ended_at"; then
      echo "FATAL: could not write atomic round status" >&2
      finish_rc=1
    fi
  fi
  if [ "$RESTORE_PRIOR_POINTER" -eq 1 ]; then
    # The refused attempt keeps its own record under invocations/; the round's current pointer and
    # status go back to the finished run they described (no pointer before: none after).
    if [ -n "$PRIOR_CURRENT_INVOCATION" ]; then
      printf '%s\n' "$PRIOR_CURRENT_INVOCATION" > "$OUT/.CURRENT_INVOCATION.restore.$$" \
        && mv -f "$OUT/.CURRENT_INVOCATION.restore.$$" "$OUT/CURRENT_INVOCATION"
    else
      rm -f "$OUT/CURRENT_INVOCATION"
    fi
    [ -n "$PRIOR_STATUS_COPY" ] && cp -p "$PRIOR_STATUS_COPY" "$OUT/.ROUND_STATUS.restore.$$" \
      && mv -f "$OUT/.ROUND_STATUS.restore.$$" "$CURRENT_STATUS"
    echo "  ${RESTORE_NOTE:-round $ROUND is finished; its CURRENT_INVOCATION and ROUND_STATUS.json were left unchanged}" >&2
  fi
  rm -f "$RUN_LOCK/pid" 2>/dev/null || true
  rmdir "$RUN_LOCK" 2>/dev/null || true
  exit "$finish_rc"
}
trap finish_run EXIT
trap 'FINAL_DETAIL="interrupted by INT"; exit 130' INT
trap 'FINAL_DETAIL="interrupted by TERM"; exit 143' TERM
trap 'FINAL_DETAIL="interrupted by HUP"; exit 129' HUP

# A driver killed with -9 leaves its role provider running: a rerun would call the same role twice at once and
# interleave the journal. Refuse while any earlier invocation of this round still has a live provider.
# 2.6.9: checked first, before this run builds, reuses or re-freezes the round snapshot: a snapshot that
# live provider is still reading must not be handed a delete-and-rebuild remedy (it needs only $OUT and
# $INV, and the pointer written above is restored on the way out).
live_provider=""
for provider_file in "$OUT"/invocations/*/active_providers/*.pid; do
  [ -f "$provider_file" ] || continue
  case "$provider_file" in "$INV"/*) continue ;; esac
  provider_pid=$(sed -n '1p' "$provider_file" 2>/dev/null || true)
  case "$provider_pid" in ''|*[!0-9]*) continue ;; esac
  # a pid file outlives a driver killed with -9, and the OS reuses pids: count it only while that pid is still a
  # codex provider process
  if kill -0 "$provider_pid" 2>/dev/null && ps -o command= -p "$provider_pid" 2>/dev/null | grep -q codex; then
    live_provider="$provider_pid"; live_file="$provider_file"; break
  fi
done
if [ -n "$live_provider" ]; then
  FINAL_DETAIL="an earlier run of round $ROUND still has a live provider process (pid $live_provider, $live_file)"
  echo "FATAL: $FINAL_DETAIL; wait for it to finish (or stop it: kill $live_provider), then rerun this same command" >&2
  echo "  (if pid $live_provider is not an I4R provider -- a reused pid -- delete that .pid file instead)" >&2
  # this refusal is not a run of the round: the killed run's pointer and status stay current (the waiter reads them)
  RESTORE_PRIOR_POINTER=1
  RESTORE_NOTE="the earlier run's CURRENT_INVOCATION and ROUND_STATUS.json were left unchanged"
  exit 1
fi

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
  verify_output=$(python3 "$SCRIPT_DIR/verify_diff_packet.py" "$WS" "$DIFF_MANIFEST_PATH" --round "$ROUND" 2>&1 >/dev/null) || {
      FINAL_DETAIL="diff manifest is not the deterministic projection of its bound inputs"
      if round_is_finished; then
        # the packet check's own remedy rebuilds this round's packet; a finished round is never rerun
        next_step=$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import lifecycle_journal as j, pathlib
print(j.next_round_hint(j.load_events(pathlib.Path(sys.argv[2])), int(sys.argv[3])))' "$SCRIPT_DIR" "$WS" "$ROUND" 2>/dev/null \
          || echo "prepare the next round")
        echo "FATAL: round $ROUND is already finished (integrated or ended) and cannot be rerun: its inputs changed since it ran;" >&2
        echo "  do not rebuild its packet. If more review is needed, $next_step." >&2
      else
        printf '%s\n' "$verify_output" >&2
        echo "FATAL: $FINAL_DETAIL" >&2
      fi
      exit 1
    }
fi

ROUTING="$INV/ROUTING.json"
if [ -n "$DIFF_MANIFEST_PATH" ]; then
  "$HELPER" route --workspace "$WS" --round "$ROUND" --override "${CRITICS:-}" \
    --manifest "$DIFF_MANIFEST_PATH" --output "$ROUTING"
else
  "$HELPER" route --workspace "$WS" --round "$ROUND" --override "${CRITICS:-}" --output "$ROUTING"
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
# Resuming a round whose critics already started (a killed or failed run) must review with the SAME roles: re-routing
# without the original CRITICS override would supersede the authorized plan and silently drop a role. Refuse unless the
# operator re-routes on purpose.
# The started invocation is the newest one that actually dispatched (non-empty attempts/), not the pointer: a refused
# or early-failed run moves the pointer to an invocation that never started.
if [ "${I4R_SUPERSEDE_PLAN:-0}" != "1" ] && ! round_has_quorum; then
  PRIOR_NAMES=$(python3 -c 'import json, os, sys
out, current = sys.argv[1], sys.argv[2]
base = os.path.join(out, "invocations")
try:
    names = sorted(os.listdir(base), reverse=True)
except OSError:
    names = []
for name in names:
    inv = os.path.join(base, name)
    if name == current or not os.path.isdir(os.path.join(inv, "attempts")) or not os.listdir(os.path.join(inv, "attempts")):
        continue
    try:
        print(" ".join(json.load(open(os.path.join(inv, "ROUTING.json")))["roles"]))
    except Exception:
        pass
    break' "$OUT" "$(basename "$INV")" 2>/dev/null || true)
  same_roles() { [ "$(printf '%s\n' $1 | sort | tr '\n' ' ')" = "$(printf '%s\n' $2 | sort | tr '\n' ' ')" ]; }
  if [ -n "$PRIOR_NAMES" ] && ! same_roles "$PRIOR_NAMES" "$NAMES"; then
    FINAL_DETAIL="round $ROUND already started with roles '$PRIOR_NAMES'; this run routed '$NAMES'"
    echo "FATAL: $FINAL_DETAIL. To resume it with its roles:" >&2
    printf '  CRITICS=%q AUDIT_DIR=%q bash %q %s\n' "$PRIOR_NAMES" "$WS" "$SCRIPT_DIR/run_critique.sh" "$ROUND" >&2
    echo "  (plus the DIFF_MANIFEST / I4R_AUTO_AUTHORIZE / ADJUDICATE settings it was started with); to re-route it on purpose, set I4R_SUPERSEDE_PLAN=1" >&2
    exit 1
  fi
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
    --diff-manifest "$DIFF_MANIFEST_PATH" > "$SNAPSHOT_META" 2> "$INV/snapshot.stderr"
else
  "$HELPER" snapshot --workspace "$WS" --report "$REPORT" --prompts "$PROMPTS" \
    --out "$OUT" --model "$CRITIC_MODEL" --effort "$CRITIC_EFFORT" \
    --roles "$NAMES" --routing "$ROUTING" --runtime-script "$SCRIPT_DIR/run_critique.sh" --round "$ROUND" \
    > "$SNAPSHOT_META" 2> "$INV/snapshot.stderr"
fi
snapshot_rc=$?
cat "$INV/snapshot.stderr" >&2 2>/dev/null
if [ "$snapshot_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
  faulted_snapshot=$(snapshot_named_in "$INV/snapshot.stderr")
  if snapshot_mode_unstable_in "$INV/snapshot.stderr"; then
    FINAL_DETAIL="round snapshot ${faulted_snapshot:-named above}: $SNAPSHOT_MODE_UNSTABLE_REMEDY; no provider call was made"
  else
    FINAL_DETAIL="round snapshot ${faulted_snapshot:-named above} failed its integrity check (content, inventory or manifest); no provider call was made; remedy: $(snapshot_remedy "${faulted_snapshot:-<the snapshot directory named above>}")"
  fi
  echo "FATAL: $FINAL_DETAIL" >&2
  exit 1
elif [ "$snapshot_rc" -ne 0 ]; then
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

# Metadata 2.1 binds authorization to this exact immutable round plan. Preparing the snapshot is
# provider-free; a missing/expired grant pauses here with no reservation, attempt, or provider call.
AUTHORIZATION_STATE="$INV/round_authorization.json"
AUTHORIZATION_TEMPLATE="$OUT/ROUND_AUTHORIZATION_TEMPLATE.json"
auth_rc=0
# Unattended runs may supersede a prepared plan whose inputs changed (for example after a
# runtime fix) instead of refusing; the superseding plan is journaled and then authorized
# through the same auto-authorize path. Attended runs keep the refusal so the operator sees it.
# An attended operator re-preparing after a legitimate change says so with I4R_SUPERSEDE_PLAN=1 (the new plan still
# needs authorize-round and, from round 2, fresh post-delta gates, so nothing runs unreviewed).
AUTO_SUPERSEDE_FLAG=""
{ [ "${I4R_AUTO_AUTHORIZE:-1}" = "1" ] || [ "${I4R_SUPERSEDE_PLAN:-0}" = "1" ]; } && AUTO_SUPERSEDE_FLAG="--supersede-plan"
round_authorization() {
  # shellcheck disable=SC2086
  "$HELPER" round-authorization --workspace "$WS" --round "$ROUND" \
    --policy "$SNAPSHOT/source/context/PIPELINE_POLICY.json" --routing "$ROUTING" \
    --input-sha "$INPUT_SHA" --report-sha "$REPORT_SHA" --roles "$NAMES" \
    --model "$CRITIC_MODEL" --effort "$CRITIC_EFFORT" \
    --adjudicator-model "$ADJUDICATOR_MODEL" --adjudicator-effort "$ADJUDICATOR_EFFORT" \
    --output "$AUTHORIZATION_STATE" --authorization-template "$AUTHORIZATION_TEMPLATE" \
    $AUTO_SUPERSEDE_FLAG "$@"
}
if [ -n "$DIFF_MANIFEST_PATH" ]; then
  round_authorization --diff-manifest "$DIFF_MANIFEST_PATH" || auth_rc=$?
else
  round_authorization || auth_rc=$?
fi
if [ "$auth_rc" -eq 3 ] && [ "${I4R_AUTO_AUTHORIZE:-1}" = "1" ]; then
  # Unattended is the DEFAULT: a round completes without a human pause. The grant is still
  # journaled and still bound to the exact generated plan, and the template is authorized
  # byte-for-byte as the driver wrote it, so "never authorize a modified template" continues to
  # hold -- only the pause is gone. Set I4R_AUTO_AUTHORIZE=0 for an attended run, which restores
  # the exit-3 pause. What is NOT removed by either setting: the round plan is still prepared,
  # journaled, hash-bound and replayable, and every model/effort/data-class in it is still policy
  # -pinned, so an unattended grant cannot widen the route a human would have seen.
  [ -f "$AUTHORIZATION_TEMPLATE" ] || fail_early "auto-authorize: template was not generated"
  echo "  [auth ] authorizing the generated round-$ROUND plan unchanged (unattended default;" >&2
  echo "          set I4R_AUTO_AUTHORIZE=0 to pause for review instead)" >&2
  python3 "$SCRIPT_DIR/lifecycle_journal.py" authorize-round "$WS" "$ROUND" "$AUTHORIZATION_TEMPLATE" \
    || fail_early "auto-authorize: lifecycle_journal.py rejected the generated template"
  auth_rc=0
  if [ -n "$DIFF_MANIFEST_PATH" ]; then
    round_authorization --diff-manifest "$DIFF_MANIFEST_PATH" || auth_rc=$?
  else
    round_authorization || auth_rc=$?
  fi
  [ "$auth_rc" -eq 0 ] || fail_early "auto-authorize: plan did not validate after authorization (rc=$auth_rc)"
fi
if [ "$auth_rc" -eq 3 ]; then
  FINAL_OUTCOME="paused"
  FINAL_ADJUDICATION="not_reached"
  FINAL_DETAIL="awaiting authorization for the exact round plan; review $AUTHORIZATION_TEMPLATE"
  echo "PAUSED: $FINAL_DETAIL" >&2
  printf '  after reviewing it: python3 %q authorize-round %q %s %q\n  then rerun this same command\n' \
    "$SCRIPT_DIR/lifecycle_journal.py" "$WS" "$ROUND" "$AUTHORIZATION_TEMPLATE" >&2
  exit 3
elif [ "$auth_rc" -ne 0 ]; then
  FINAL_DETAIL="round authorization validation failed"
  exit 1
fi

# SKILL: round N's post-delta gates run after its prepare call and BEFORE it runs. A receipt that exists but no longer
# matches the report, registry or packet (edited after the gates) would let the critics review an ungated state.
# An adjudicator-only retry: an earlier invocation of this round reached critic quorum on THIS exact input. A run on a
# new input is a new critic run, whatever the journal says about an earlier quorum, and must be gated.
quorum_on_this_input() {
  # a forced rerun re-dispatches every critic: it is not an adjudicator-only retry and must be gated
  [ "${FORCE_RERUN:-0}" -eq 1 ] && return 1
  python3 - "$OUT" "$INPUT_SHA" <<'PYIN' 2>/dev/null
import json, os, sys
base = os.path.join(sys.argv[1], "invocations")
try:
    names = os.listdir(base)
except OSError:
    names = []
for name in names:
    try:
        status = json.load(open(os.path.join(base, name, "ROUND_STATUS.json")))
    except Exception:
        continue
    if isinstance(status, dict) and status.get("quorum_met") is True and status.get("input_sha256") == sys.argv[2]:
        sys.exit(0)
sys.exit(1)
PYIN
}
# A round 2+ with NO receipt ran ungated, and finalize can never accept it afterwards: refuse it now. (Only once the
# audit has canonical finding projections -- the gates' own input; a synthetic runtime fixture without them has
# nothing to gate.)
if [ "$ROUND" -ge 2 ] && [ ! -f "$OUT/POST_DELTA_GATES.json" ] && [ ! -f "$OUT/POST_DELTA_GATES.partial.json" ] \
    && [ -f "$WS/work/finding_projections.manifest.json" ] && ! quorum_on_this_input; then
  FINAL_DETAIL="round $ROUND has no post-delta gate receipt; run its gates (after its packet) before the round"
  echo "FATAL: $FINAL_DETAIL:" >&2
  printf '  python3 %q %q --round %s --report %q\n  then rerun this same command\n' "$SCRIPT_DIR/run_post_delta_gates.py" \
    "$WS" "$ROUND" "$WS/report/REPORT_final.md" >&2
  exit 1
fi
# Gates that FAILED leave POST_DELTA_GATES.partial.json (a passing run deletes it): the round must not run on that state.
if [ "$ROUND" -ge 2 ] && [ -f "$OUT/POST_DELTA_GATES.partial.json" ] && ! quorum_on_this_input; then
  FINAL_DETAIL="round $ROUND's last post-delta gate run failed (POST_DELTA_GATES.partial.json); fix what it reported, then re-run the gates"
  echo "FATAL: $FINAL_DETAIL:" >&2
  printf '  python3 %q %q --round %s --report %q\n' "$SCRIPT_DIR/run_post_delta_gates.py" "$WS" "$ROUND" \
    "$WS/report/REPORT_final.md" >&2
  exit 1
fi
# A round whose critics already reached quorum (finished, or awaiting an adjudicator retry) is only rerun by the
# driver's own remedies; its packet check above already refuses changed inputs, and its receipt keeps the gate
# code it ran with.
if [ "$ROUND" -ge 2 ] && [ -f "$OUT/POST_DELTA_GATES.json" ] && ! quorum_on_this_input; then
  if ! gate_detail="$(python3 -c 'import pathlib, sys
sys.path.insert(0, sys.argv[1])
import run_post_delta_gates
root = pathlib.Path(sys.argv[2])
try:
    run_post_delta_gates.verify_receipt(root, root / f"critique/round_{sys.argv[3]}/POST_DELTA_GATES.json", require_current=True)
except Exception as exc:
    print(exc); sys.exit(1)' "$SCRIPT_DIR" "$WS" "$ROUND" 2>&1)"; then
    FINAL_DETAIL="round $ROUND's post-delta gates are not current ($gate_detail)"
    echo "FATAL: $FINAL_DETAIL; re-run them before the round:" >&2
    printf '  python3 %q %q --round %s --report %q\n' "$SCRIPT_DIR/run_post_delta_gates.py" "$WS" "$ROUND" \
      "$WS/report/REPORT_final.md" >&2
    exit 1
  fi
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
  telemetry_attempt_kind="${8:-}"
  telemetry_source_invocation="${9:-}"
  if [ -z "$telemetry_attempt_kind" ]; then
    if [ "$telemetry_provider_call" -eq 0 ]; then
      telemetry_attempt_kind=cache_replay
    elif [ "$telemetry_role" = model_probe ]; then
      telemetry_attempt_kind=capability_probe
    else
      telemetry_attempt_kind=role_inference
    fi
  fi
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
    --attempt-kind "$telemetry_attempt_kind" \
    --source-invocation-id "$telemetry_source_invocation" \
    --outcome "$telemetry_outcome" --failure-class "$telemetry_failure" \
    --error-class "$telemetry_error_class" \
    --exit-code "$telemetry_exit" --model "$telemetry_model" --effort "$telemetry_effort" \
    --started "$telemetry_started" --ended "$telemetry_ended" --elapsed "$telemetry_elapsed" \
    --workspace "$WS"
}

classify_provider_failure() {
  "$HELPER" classify-provider-failure --workspace "$WS" \
    --policy "$SNAPSHOT/source/context/PIPELINE_POLICY.json" --log "$1"
}

# The provider's own last words, from a sealed events/failure log, for the operator's terminal.
# On audit A28 the probe failed "(permanent)" with error_class quota and the
# operator had to re-run codex by hand to learn "Your workspace is out of credits": the message
# had been on disk in failure.log the whole time. One line, no newlines, at most 240 characters.
provider_error_message() {
  python3 - "$1" <<'PYMSG' 2>/dev/null || true
import json, sys
message = ""
try:
    for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") == "error" and isinstance(row.get("message"), str):
            message = row["message"]
        elif row.get("type") == "turn.failed" and isinstance((row.get("error") or {}).get("message"), str):
            message = row["error"]["message"]
except OSError:
    pass
print(" ".join(message.split())[:240])
PYMSG
}

journal_retry_backoff() {
  backoff_role="$1"
  backoff_attempt="$2"
  backoff_error_class="$3"
  backoff_delay="$4"
  backoff_id="${INVOCATION_ID}:${backoff_role}:${backoff_attempt}:$RANDOM:retry"
  backoff_started=$(utc_now)
  "$HELPER" retry-backoff --workspace "$WS" --phase start --retry-id "$backoff_id" \
    --round "$ROUND" --role "$backoff_role" --attempt "$backoff_attempt" \
    --error-class "$backoff_error_class" --delay-seconds "$backoff_delay" \
    --at "$backoff_started" >/dev/null || return 1
  if [ "$backoff_delay" -gt 0 ]; then sleep "$backoff_delay"; fi
  backoff_ended=$(utc_now)
  "$HELPER" retry-backoff --workspace "$WS" --phase end --retry-id "$backoff_id" \
    --round "$ROUND" --role "$backoff_role" --attempt "$backoff_attempt" \
    --error-class "$backoff_error_class" --delay-seconds "$backoff_delay" \
    --at "$backoff_ended" >/dev/null
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
  CODEX_TIMED_OUT=0
  PROVIDER_PID_FILE="$INV/active_providers/${PROVIDER_SLOT}.pid"
  rm -f "$PROVIDER_PID_FILE" "${PROVIDER_PID_FILE}.cancel" 2>/dev/null || true
  "$HELPER" provider-exec --codex "$CODEX" --model "$exec_model" --effort "$exec_effort" \
    --workspace "${PROVIDER_WORKSPACE:-$SNAPSHOT}" --schema "$exec_schema" --response "$exec_response" \
    --pid-file "$PROVIDER_PID_FILE" \
    < "$exec_prompt" > "$exec_events" 2> "$exec_stderr" &
  CODEX_CHILD_PID=$!
  trap 'codex_exec_cancel 130' INT
  trap 'codex_exec_cancel 143' TERM
  trap 'codex_exec_cancel 129' HUP
  # Bounded wait. On expiry the provider group is stopped and the call reports exit 124, which the
  # driver's existing policy treats as transient, so the ordinary retry budget applies.
  exec_deadline=$(( $(date +%s) + PROVIDER_TIMEOUT_SECONDS ))
  exec_rc=""
  while :; do
    # Poll the CHILD pid, not its process group: the helper is launched with `&` from a shell
    # without job control, so it never becomes a group leader and `kill -0 -- -PID` is false from
    # the first tick. provider_group_alive stays the right call for STOPPING the group, which is
    # why it is still used on the expiry path below.
    if ! kill -0 "$CODEX_CHILD_PID" 2>/dev/null; then
      wait "$CODEX_CHILD_PID"; exec_rc=$?; break
    fi
    if [ "$(date +%s)" -ge "$exec_deadline" ]; then
      echo "  [tmout] provider call exceeded ${PROVIDER_TIMEOUT_SECONDS}s; stopping it" >&2
      CODEX_TIMED_OUT=1
      stop_provider_group "$CODEX_CHILD_PID"
      kill -TERM "$CODEX_CHILD_PID" 2>/dev/null || true
      sleep 1
      kill -KILL "$CODEX_CHILD_PID" 2>/dev/null || true
      wait "$CODEX_CHILD_PID" 2>/dev/null || true
      exec_rc=124
      break
    fi
    # Poll finely. A coarse tick adds its full period to the latency of EVERY provider call,
    # including the sub-second probe: a 5s tick made the whole round ~5s slower per call and broke
    # the termination test's 5s start window. 0.25s costs nothing and bounds the overshoot.
    sleep 0.25
  done
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

# Prove the frozen snapshot intact immediately before a paid dispatch. Callers run it BEFORE
# claim_attempt and lifecycle_before_provider, so a snapshot that fails costs no attempt, leaves no
# reservation or telemetry row and makes no call; mode-only drift is re-frozen and journaled once.
# (Not inside codex_exec: by then the attempt is claimed and the reservation made.) Before 2.6.8
# nothing looked until after each call: a probe-time drift on audit A20 failed
# twelve paid critic responses as schema-invalid and re-dispatched every one of them.
# Returns the helper's code: 0 ready, SNAPSHOT_INTEGRITY_RC integrity failure, other nonzero error.
snapshot_ready_for_dispatch() {
  ready_snapshot="$1"
  ready_kind="$2"
  ready_sha="$3"
  ready_context="$4"
  mkdir -p "$INV/pre_dispatch" || return 1
  ready_log="$INV/pre_dispatch/${ready_context}.log"
  "$HELPER" verify-snapshot --workspace "$WS" --snapshot "$ready_snapshot" --kind "$ready_kind" \
    --expected-input-sha "$ready_sha" --round "$ROUND" --context "$ready_context" \
    --invocation "$INVOCATION_ID" --runtime-script "$SCRIPT_DIR/run_critique.sh" \
    > /dev/null 2> "$ready_log"
  ready_rc=$?
  # The helper's own [note ] (a re-freeze) or error lines belong in the driver log too.
  [ ! -s "$ready_log" ] || sed -n '1,20p' "$ready_log" >&2
  return "$ready_rc"
}

# A worker whose pre-dispatch check failed stops here: failure.json, a [stop ] line saying no call
# was made, and the remedy. Nothing was claimed or reserved, so there is nothing to undo.
# Usage: stop_before_dispatch <label> <role> <failure.json> <attempts> <log> <snapshot_rel> <rc>
stop_before_dispatch() {
  stop_label="$1"; stop_role="$2"; stop_output="$3"; stop_attempts="$4"; stop_log="$5"
  stop_snapshot_rel="$6"; stop_rc="$7"
  if [ "$stop_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ] && snapshot_mode_unstable_in "$stop_log"; then
    "$HELPER" failure --output "$stop_output" --role "$stop_role" --attempts "$stop_attempts" \
      --failure-class snapshot_mode_unstable --log "$stop_log" >/dev/null 2>&1 || true
    echo "  [stop ] $stop_label: snapshot modes keep changing (content verified) before dispatch; no provider call was made" >&2
    echo "         $stop_snapshot_rel: $SNAPSHOT_MODE_UNSTABLE_REMEDY" >&2
  elif [ "$stop_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
    "$HELPER" failure --output "$stop_output" --role "$stop_role" --attempts "$stop_attempts" \
      --failure-class snapshot_integrity --log "$stop_log" >/dev/null 2>&1 || true
    echo "  [stop ] $stop_label: snapshot integrity check failed before dispatch; no provider call was made" >&2
    echo "         remedy: $(snapshot_remedy "$stop_snapshot_rel") this round" >&2
  else
    "$HELPER" failure --output "$stop_output" --role "$stop_role" --attempts "$stop_attempts" \
      --failure-class pre_dispatch_check_failed --log "$stop_log" >/dev/null 2>&1 || true
    echo "  [stop ] $stop_label: the pre-dispatch snapshot check could not run (rc=$stop_rc); no provider call was made; see $stop_log" >&2
  fi
}

# Which roles stopped on a snapshot integrity failure, split by whether any provider call was made
# for them in this invocation. Empty when none did. Used only to word FINAL_DETAIL.
snapshot_integrity_summary() {
  python3 - "$INV/results" "$LOCAL_TELEMETRY" <<'PYSI' 2>/dev/null || true
import json, pathlib, sys
results, telemetry = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
called = set()
if telemetry.is_file():
    for line in telemetry.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("provider_call"):
            called.add(row.get("role"))
refused, after, unstable = [], [], []
for path in sorted(results.glob("*.failure.json")):
    try:
        obj = json.loads(path.read_text())
    except (OSError, ValueError):
        continue
    if not isinstance(obj, dict):
        continue
    if obj.get("failure_class") == "snapshot_mode_unstable":
        # Content verified; the modes would not stay frozen. Not a change, whatever was paid.
        unstable.append(str(obj.get("role")))
        continue
    if obj.get("failure_class") != "snapshot_integrity":
        continue
    (after if obj.get("role") in called else refused).append(str(obj.get("role")))
parts = []
if refused:
    parts.append("refused before dispatch, no provider call was made for " + " ".join(refused))
if after:
    parts.append("changed during a provider call, not retried, for " + " ".join(after))
if unstable:
    parts.append("content verified but its modes kept changing after repeated re-freezes, for "
                 + " ".join(unstable))
print("; ".join(parts))
PYSI
}

# The remedy for what snapshot_integrity_summary reported about snapshot $1: the delete command
# when any content fault was recorded, else the sync-client remedy for modes that keep changing.
snapshot_fault_remedy() {
  if grep -l '"failure_class": *"snapshot_integrity"' "$INV"/results/*.failure.json >/dev/null 2>&1; then
    printf 'remedy: %s' "$(snapshot_remedy "$1")"
  elif grep -l '"failure_class": *"snapshot_mode_unstable"' "$INV"/results/*.failure.json >/dev/null 2>&1; then
    printf '%s: %s' "$1" "$SNAPSHOT_MODE_UNSTABLE_REMEDY"
  else
    printf 'remedy: %s' "$(snapshot_remedy "$1")"
  fi
}

lifecycle_before_provider() {
  lifecycle_call_id="$1"
  lifecycle_role="$2"
  lifecycle_role_class="$3"
  lifecycle_attempt_kind="$4"
  lifecycle_model="$5"
  lifecycle_effort="$6"
  lifecycle_prompt="$7"
  lifecycle_at="$8"
  lifecycle_schema_digest="${9:-}"
  if [ -n "$lifecycle_schema_digest" ]; then
    "$HELPER" lifecycle-before-provider --workspace "$WS" \
      --invocation-id "$lifecycle_call_id" --role "$lifecycle_role" \
      --role-class "$lifecycle_role_class" --attempt-kind "$lifecycle_attempt_kind" \
      --round "$ROUND" --model "$lifecycle_model" --effort "$lifecycle_effort" \
      --input-sha "${TELEMETRY_INPUT_SHA:-$INPUT_SHA}" --prompt "$lifecycle_prompt" \
      --at "$lifecycle_at" --schema-digest "$lifecycle_schema_digest" >/dev/null
  else
    "$HELPER" lifecycle-before-provider --workspace "$WS" \
      --invocation-id "$lifecycle_call_id" --role "$lifecycle_role" \
      --role-class "$lifecycle_role_class" --attempt-kind "$lifecycle_attempt_kind" \
      --round "$ROUND" --model "$lifecycle_model" --effort "$lifecycle_effort" \
      --input-sha "${TELEMETRY_INPUT_SHA:-$INPUT_SHA}" --prompt "$lifecycle_prompt" \
      --at "$lifecycle_at" >/dev/null
  fi
}

lifecycle_after_provider() {
  "$HELPER" lifecycle-after-provider --workspace "$WS" --invocation-id "$1" \
    --events "$2" --response "$3" --stderr "$4" --started-at "$5" --ended-at "$6" \
    >/dev/null
}

# Provider-attempt budgets are round-wide and ledger-backed.  Define them before the model probe
# so a failed probe cannot restart at attempt 1 when this driver is resumed with a new invocation
# ID (for example after correcting a locally generated response schema).
BUDGET_ROOT="$OUT/attempt_budget"
mkdir -p "$BUDGET_ROOT" || { FINAL_DETAIL="could not initialize attempt budgets"; exit 1; }

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

# A claim is debited BEFORE the journal is asked to reserve the call, because the attempt number
# is part of the invocation id the reservation needs. When the journal refuses the reservation,
# no provider was contacted and nothing was journaled, so the debit must be returned -- otherwise
# a refused dispatch silently costs one of the round's attempts, and a rerun that is refused
# twice has spent two-thirds of its budget without a single call.
release_attempt() {
  budget_name="$1"
  claimed="$2"
  current=$(cat "$BUDGET_ROOT/$budget_name.count" 2>/dev/null || echo 0)
  [ "$current" = "$claimed" ] || return 0
  atomic_text "$((claimed - 1))" "$BUDGET_ROOT/$budget_name.count" || true
}

probe_cache_valid() {
  python3 - "$PROBE_CACHE" "$CRITIC_MODEL" "$CRITIC_EFFORT" "$CODEX_VERSION" \
    "$PROBE_SCHEMA_SHA" "$PROBE_HELPER_SHA" "$PROBE_SCHEMA_GENERATION_VERSION" \
    "$PROBE_SCHEMA_SET_SHA" <<'PY'
import json, pathlib, sys
try:
    obj = json.loads(pathlib.Path(sys.argv[1]).read_text())
    good = (
        obj.get("cache_version") == "i4r-model-probe-4"
        and obj.get("status") == "ok"
        and obj.get("model") == sys.argv[2]
        and obj.get("effort") == sys.argv[3]
        and obj.get("codex_version") == sys.argv[4]
        and obj.get("probe_schema_sha256") == sys.argv[5]
        and obj.get("runtime_helper_sha256") == sys.argv[6]
        and obj.get("provider_schema_generation_version") == sys.argv[7]
        and obj.get("schema_set_sha256") == sys.argv[8]
        and isinstance(obj.get("source_invocation_id"), str)
        and bool(obj["source_invocation_id"])
    )
except Exception:
    good = False
raise SystemExit(0 if good else 1)
PY
}

CODEX_VERSION=$("$CODEX" --version 2>/dev/null | sed -n '1p')
[ -n "$CODEX_VERSION" ] || CODEX_VERSION="unknown"
PROBE_SCHEMA_META=$(python3 - "$SNAPSHOT/MANIFEST.json" "$SNAPSHOT/schemas/probe.schema.json" \
  "$HELPER" <<'PY'
import hashlib, json, pathlib, sys
manifest_path, schema_path, helper_path = map(pathlib.Path, sys.argv[1:])
manifest = json.loads(manifest_path.read_text())
version = manifest.get("provider_schema_generation_version")
if not isinstance(version, str) or not version:
    raise SystemExit("snapshot lacks provider_schema_generation_version")
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
# Cache the capability of the versioned *contract shapes*, not one round's
# content-bound schema instances. Exact claim IDs, hashes and invocation IDs
# remain enforced by the per-call schema and semantic validators, but must not
# force a second capability probe in Round 2.
shape_keys = (
    "provider_schema_generation_version",
    "canonical_critic_schema_sha256",
    "schema_validator_sha256",
    "semantic_validator_sha256",
)
shape = {key: manifest.get(key) for key in shape_keys}
if any(not isinstance(value, str) or not value for value in shape.values()):
    raise SystemExit("snapshot lacks compiled critic/adjudicator contract-shape identity")
schema_set = hashlib.sha256(json.dumps(
    shape, sort_keys=True, separators=(",", ":"),
).encode()).hexdigest()
print(digest(schema_path), digest(helper_path), version, schema_set, sep="\t")
PY
) || { FINAL_DETAIL="could not bind probe cache to schema-generation identity"; exit 1; }
IFS=$(printf '\t') read -r PROBE_SCHEMA_SHA PROBE_HELPER_SHA PROBE_SCHEMA_GENERATION_VERSION \
  PROBE_SCHEMA_SET_SHA \
  <<EOF
$PROBE_SCHEMA_META
EOF
if [ -z "$PROBE_SCHEMA_SHA" ] || [ -z "$PROBE_HELPER_SHA" ] \
    || [ -z "$PROBE_SCHEMA_GENERATION_VERSION" ] || [ -z "$PROBE_SCHEMA_SET_SHA" ]; then
  FINAL_DETAIL="probe cache schema-generation identity is incomplete"
  exit 1
fi
PROBE_KEY=$(python3 - "$CRITIC_MODEL" "$CRITIC_EFFORT" "$CODEX_VERSION" \
  "$PROBE_SCHEMA_SHA" "$PROBE_HELPER_SHA" "$PROBE_SCHEMA_GENERATION_VERSION" \
  "$PROBE_SCHEMA_SET_SHA" <<'PY'
import hashlib, sys
print(hashlib.sha256(("i4r-model-probe-4\0" + "\0".join(sys.argv[1:])).encode()).hexdigest())
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
  'Return the exact compact JSON object required by the schema. It contains critic_contract and adjudicator_contract examples that jointly exercise the compiled provider schema subset. Do not use tools.' \
  > "$PROBE_PROMPT"

if probe_cache_valid; then
  atomic_copy "$PROBE_CACHE" "$PROBE_CACHE_RECORD" \
    || { FINAL_DETAIL="could not bind cached probe metadata to this invocation"; exit 1; }
  probe_time=$(utc_now); probe_epoch=$(epoch_now)
  probe_source_invocation=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_invocation_id"])' "$PROBE_CACHE_RECORD")
  record_telemetry model_probe 0 0 cache_hit "" 0 \
    "$probe_time" "$probe_epoch" "$probe_time" "$probe_epoch" "" "$PROBE_CACHE_RECORD" "" \
    "$PROBE_PROMPT" probe_cache "" cache_replay "$probe_source_invocation" \
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
    probe_source_invocation=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_invocation_id"])' "$PROBE_CACHE_RECORD")
    record_telemetry model_probe 0 0 cache_hit "" 0 \
      "$probe_time" "$probe_epoch" "$probe_time" "$probe_epoch" "" "$PROBE_CACHE_RECORD" "" \
      "$PROBE_PROMPT" probe_cache "" cache_replay "$probe_source_invocation" \
      || { FINAL_DETAIL="could not record cached probe telemetry"; exit 1; }
  else
    snapshot_ready_for_dispatch "$SNAPSHOT" round "$INPUT_SHA" model_probe
    probe_ready_rc=$?
    if [ "$probe_ready_rc" -ne 0 ]; then
      if [ "$probe_ready_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ] \
          && snapshot_mode_unstable_in "$INV/pre_dispatch/model_probe.log"; then
        FINAL_DETAIL="round snapshot $SNAPSHOT_REL: $SNAPSHOT_MODE_UNSTABLE_REMEDY; no provider call was made"
      elif [ "$probe_ready_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
        FINAL_DETAIL="round snapshot $SNAPSHOT_REL failed its pre-dispatch integrity check; no provider call was made; remedy: $(snapshot_remedy "$SNAPSHOT_REL")"
      else
        FINAL_DETAIL="pre-dispatch snapshot check could not run (rc=$probe_ready_rc); no provider call was made"
      fi
      echo "FATAL: $FINAL_DETAIL" >&2
      exit 1
    fi
    probe_attempt=$(claim_attempt model_probe) || {
      probe_used=$(budget_used model_probe 2>/dev/null || printf '%s' unknown)
      FINAL_DETAIL="model probe exhausted its round-wide $MAX_TRIES-attempt budget (used=$probe_used)"
      echo "FATAL: $FINAL_DETAIL" >&2
      exit 2
    }
    probe_started=$(utc_now); probe_started_epoch=$(epoch_now)
    ACTIVE_CALL_ID="${INVOCATION_ID}:model_probe:${probe_attempt}:${probe_started_epoch}:$RANDOM"
    lifecycle_before_provider "$ACTIVE_CALL_ID" model_probe other capability_probe \
      "$CRITIC_MODEL" "$CRITIC_EFFORT" "$PROBE_PROMPT" "$probe_started" "$PROBE_SCHEMA_SHA" \
      || { FINAL_DETAIL="could not reserve model probe lifecycle"; exit 1; }
    PROBE_REQUEST_ID="request_${probe_attempt}_${probe_started_epoch}_$RANDOM"
    PROBE_REQUEST_DIR="$INV/probe/requests/$PROBE_REQUEST_ID"
    mkdir -p "$PROBE_REQUEST_DIR" \
      || { FINAL_DETAIL="could not create unique model-probe request directory"; exit 1; }
    PROBE_RESPONSE="$PROBE_REQUEST_DIR/response.json"
    PROBE_EVENTS="$PROBE_REQUEST_DIR/events.jsonl"
    PROBE_STDERR="$PROBE_REQUEST_DIR/stderr.txt"
    codex_exec "$PROBE_PROMPT" "$SNAPSHOT/schemas/probe.schema.json" \
      "$PROBE_RESPONSE" "$PROBE_EVENTS" "$PROBE_STDERR"
    probe_rc=$?
    probe_cancelled=$CODEX_CANCELLED
    probe_ended=$(utc_now); probe_ended_epoch=$(epoch_now)
    lifecycle_after_provider "$ACTIVE_CALL_ID" "$PROBE_EVENTS" "$PROBE_RESPONSE" \
      "$PROBE_STDERR" "$probe_started" "$probe_ended" \
      || { FINAL_DETAIL="could not record model probe lifecycle"; exit 1; }
    probe_valid=0
    if [ "$probe_rc" -eq 0 ] && python3 - "$PROBE_RESPONSE" <<'PY'
import json, pathlib, sys
try: obj = json.loads(pathlib.Path(sys.argv[1]).read_text())
except Exception: raise SystemExit(1)
expected={
 "status":"ok",
 "critic_contract":{
  "role":"critic_stats","covered_claim_ids":["probe-claim"],
  "evidence":{"artifact_refs":[{"path":"source/probe.txt","sha256":"0"*64}],
              "operation":"probe operation","result":"probe result"}},
 "adjudicator_contract":{
  "role":"critic_synth","covered_claim_ids":["probe-claim"],
  "decision_question":"Is the evidence complete?"}}
raise SystemExit(0 if obj == expected else 1)
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
      probe_combined="$PROBE_REQUEST_DIR/failure.log"
      { [ -f "$PROBE_STDERR" ] && sed -n '1,200p' "$PROBE_STDERR"; \
        [ -f "$PROBE_EVENTS" ] && sed -n '1,200p' "$PROBE_EVENTS"; } > "$probe_combined"
      if [ "$probe_rc" -eq 0 ]; then
        probe_class="permanent"; probe_error_class=schema
      else
        probe_classification=$(classify_provider_failure "$probe_combined") \
          || { FINAL_DETAIL="provider failure classification failed"; exit 1; }
        probe_class=${probe_classification%% *}
        probe_error_class=${probe_classification#* }
      fi
      probe_cache_usage=none
      probe_outcome="failed"
    fi
    probe_attempt_kind=capability_probe
    if ! grep -qE '"type"[[:space:]]*:[[:space:]]*"thread\.started"' "$PROBE_EVENTS" \
        2>/dev/null && [ "$probe_error_class" = deterministic_schema ]; then
      probe_attempt_kind=schema_admission
    fi
    "$HELPER" seal-request-dir --workspace "$WS" --directory "$PROBE_REQUEST_DIR" \
      --request-id "$PROBE_REQUEST_ID" --invocation-id "$ACTIVE_CALL_ID" \
      --attempt-kind "$probe_attempt_kind" \
      || { FINAL_DETAIL="could not seal immutable model-probe request directory"; exit 1; }
    PROBE_SOURCE_INVOCATION_ID="$ACTIVE_CALL_ID"
    record_telemetry model_probe "$probe_attempt" 1 "$probe_outcome" "$probe_class" "$probe_rc" \
      "$probe_started" "$probe_started_epoch" "$probe_ended" "$probe_ended_epoch" \
      "$PROBE_EVENTS" "$PROBE_RESPONSE" "$PROBE_STDERR" \
      "$PROBE_PROMPT" "$probe_cache_usage" "$probe_error_class" "$probe_attempt_kind" \
      || { FINAL_DETAIL="could not record model probe telemetry"; exit 1; }
    ACTIVE_CALL_ID=""
    if [ "$probe_cancelled" -ne 0 ]; then
      FINAL_DETAIL="model probe interrupted by signal"
      exit "$probe_cancelled"
    fi
    if [ "$probe_valid" -ne 1 ]; then
      probe_message=$(provider_error_message "${probe_combined:-$PROBE_EVENTS}")
      FINAL_DETAIL="model probe failed ($probe_class${probe_error_class:+, $probe_error_class})${probe_message:+: $probe_message}; provider log: ${probe_combined:-$PROBE_EVENTS}; no critic calls were started"
      echo "FATAL: $FINAL_DETAIL" >&2
      exit 2
    fi
    python3 - "$PROBE_CACHE" "$CRITIC_MODEL" "$CRITIC_EFFORT" "$CODEX_VERSION" \
      "$PROBE_SCHEMA_SHA" "$PROBE_HELPER_SHA" "$PROBE_SCHEMA_GENERATION_VERSION" \
      "$PROBE_SCHEMA_SET_SHA" "$PROBE_SOURCE_INVOCATION_ID" <<'PY'
import datetime, json, os, pathlib, sys
path = pathlib.Path(sys.argv[1]); tmp = pathlib.Path(str(path) + ".tmp." + str(os.getpid()))
obj = {"cache_version":"i4r-model-probe-4", "status":"ok", "model":sys.argv[2],
       "effort":sys.argv[3], "codex_version":sys.argv[4],
       "probe_schema_sha256":sys.argv[5], "runtime_helper_sha256":sys.argv[6],
       "provider_schema_generation_version":sys.argv[7],
       "schema_set_sha256":sys.argv[8],
       "source_invocation_id":sys.argv[9],
       "checked_at":datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n"); os.replace(tmp, path)
PY
    if [ $? -ne 0 ]; then FINAL_DETAIL="could not publish model probe cache"; exit 1; fi
  fi
  release_probe_lock
fi

FINAL_DETAIL="model probe accepted; critics running"
write_status running 0 "" || { FINAL_DETAIL="status update after probe failed"; exit 1; }

CACHE_ROOT="$OUT/result_cache/$INPUT_SHA"
mkdir -p "$CACHE_ROOT" || { FINAL_DETAIL="could not initialize result cache"; exit 1; }

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
    "$HELPER" verify-complete --sidecar "$publish_dir/complete.json" \
      --response "$publish_dir/output.json" --markdown "$publish_dir/output.md" \
      --role "$publish_role" --round "$ROUND" --input-sha "$INPUT_SHA" \
      --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" >/dev/null 2>&1
    publish_verify_rc=$?
    if [ "$publish_verify_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
      # The snapshot is at fault, not the cache: never retire a valid cache over it.
      echo "  [fail ] critic_$publish_role: the round snapshot failed its integrity check while comparing the result cache; cache left in place" >&2
      return 1
    fi
    if [ "$publish_verify_rc" -eq 0 ]; then
      if "$HELPER" critic-equivalent --left "$INV/results/critic_${publish_role}.json" \
        --right "$publish_dir/output.json" >/dev/null 2>&1 \
        && cmp -s "$INV/results/critic_${publish_role}.md" "$publish_dir/output.md"; then
        # a forced run that reproduced the first cache supersedes any earlier forced run's differing
        # output: drop that marker, or a later plain rerun would replay the older forced verdicts
        [ "$FORCE_RERUN" -eq 1 ] && rm -f "$publish_conflict_marker"
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
  atomic_text "$ACTIVE_CALL_ID" "$publish_stage/source_invocation_id" || return 1
  chmod 444 "$publish_stage"/* 2>/dev/null || true
  mv "$publish_stage" "$publish_dir" || return 1
  chmod 555 "$publish_dir" 2>/dev/null || true
}

load_role_cache() {
  # Adopt the first valid same-input result for a role. Callers decide whether a forced rerun
  # should bypass this; the loader itself is unconditional so the conflict path can reuse it.
  cached_role="$1"
  cached_dir="${2:-$CACHE_ROOT/$cached_role}"     # a forced rerun's preserved output may be passed instead
  [ -s "$cached_dir/output.json" ] && [ -s "$cached_dir/complete.json" ] || return 1
  # Returns 1 for a plain miss, and SNAPSHOT_INTEGRITY_RC when the check could not trust the round's
  # snapshot (2.6.9): the forced-rerun replay then stops with the snapshot remedy (its log words it).
  mkdir -p "$INV/pre_dispatch" || return 1
  cache_log="$INV/pre_dispatch/cache_critic_${cached_role}.log"
  cache_verify_rc=0
  "$HELPER" verify-complete --sidecar "$cached_dir/complete.json" \
    --response "$cached_dir/output.json" --markdown "$cached_dir/output.md" \
    --role "$cached_role" --round "$ROUND" --input-sha "$INPUT_SHA" \
    --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" >/dev/null 2>"$cache_log" || cache_verify_rc=$?
  if [ "$cache_verify_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then return "$SNAPSHOT_INTEGRITY_RC"; fi
  [ "$cache_verify_rc" -eq 0 ] || return 1
  atomic_copy "$cached_dir/output.json" "$INV/results/critic_${cached_role}.json" || return 1
  atomic_copy "$cached_dir/output.md" "$INV/results/critic_${cached_role}.md" || return 1
  cached_attempts=$(python3 - "$cached_dir/complete.json" <<'PY'
import json, pathlib, sys
value=json.loads(pathlib.Path(sys.argv[1]).read_text()).get("attempts")
if not isinstance(value,int) or not 1 <= value <= 20: raise SystemExit(1)  # operator ceiling maximum (MAX_TRIES)
print(value)
PY
  ) || return 1
  "$HELPER" complete --output "$INV/results/${cached_role}.complete.json" \
    --input-sha "$INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" --round "$ROUND" \
    --role "$cached_role" --attempts "$cached_attempts" \
    --response "$INV/results/critic_${cached_role}.json" \
    --markdown "$INV/results/critic_${cached_role}.md" || return 1
  cache_time=$(utc_now); cache_epoch=$(epoch_now)
  cached_source_invocation="${3:-}"
  [ -z "$cached_source_invocation" ] && [ -f "$cached_dir/source_invocation_id" ] \
    && cached_source_invocation=$(sed -n '1p' "$cached_dir/source_invocation_id" 2>/dev/null || true)
  record_telemetry "$cached_role" 0 0 cache_hit "" 0 \
    "$cache_time" "$cache_epoch" "$cache_time" "$cache_epoch" "" \
    "$INV/results/critic_${cached_role}.json" "" \
    "$SNAPSHOT/rendered/prompt_${cached_role}.txt" read "" cache_replay "$cached_source_invocation"
}

run_one() (
  trap - EXIT INT TERM HUP
  IN_PROVIDER_WORKER=1
  role="$1"
  PROVIDER_SLOT="$role"
  if [ -f "$CACHE_ROOT/${role}.conflict" ]; then
    # Two valid same-input outputs once differed for this role. The first cached object is the
    # canonical one and the alternative is preserved under result_cache/conflicts/ for review;
    # neither fact makes the completed review unusable, so this is a note, not a stop.
    echo "  [note ] critic_$role has a recorded same-input cache conflict (first cache kept; a plain rerun replays the forced run's output)" >&2
    if [ "$FORCE_RERUN" -eq 0 ]; then
      # The differing output came from a forced rerun of this round, and that run's verdicts are the
      # round's. A plain rerun (the adjudication-only retry) replays THEM -- not the first cache, which
      # would silently restore the pre-force verdicts and drop any trigger they raised.
      forced_dir=$(sed -n 's/.*conflict=//p' "$CACHE_ROOT/${role}.conflict" 2>/dev/null | head -1)
      # the marker holds the path at write time; the audit may have moved since (another machine, another
      # Dropbox home), so resolve the preserved output by its folder name under this cache
      [ -n "$forced_dir" ] && forced_dir="$CACHE_ROOT/conflicts/$role/$(basename "$forced_dir")"
      forced_source=""
      if [ -n "$forced_dir" ] && [ -d "$forced_dir" ]; then
        forced_source=$(python3 - "$WS/invocations.jsonl" "critic_$role" "$ROUND" "$(basename "$forced_dir")" <<'PYSRC' 2>/dev/null
import json, sys
path, role, rnd, prefix = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
hits = []
for line in open(path, encoding="utf-8"):
    if not line.strip():
        continue
    row = json.loads(line)
    if (row.get("role") == role and row.get("round") == rnd and row.get("status") == "completed"
            and row.get("provider_call") is True and str(row.get("invocation_id") or "").startswith(prefix)):
        hits.append(row["invocation_id"])
print(hits[-1] if hits else "")
PYSRC
)
      fi
      forced_rc=1
      [ -z "$forced_source" ] || { load_role_cache "$role" "$forced_dir" "$forced_source"; forced_rc=$?; }
      if [ "$forced_rc" -eq 0 ]; then
        rm -f "$BUDGET_ROOT/$role.permanent"
        echo "  [cache] critic_$role (the forced rerun's output)"
        exit 0
      fi
      if [ "$forced_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
        # The preserved output is fine; the round's snapshot is not. I4R_FORCE_RERUN would pay for every
        # critic again, when a plain rerun after the snapshot remedy replays the forced output for free.
        stop_before_dispatch "critic_$role (forced-rerun replay)" "$role" "$INV/results/${role}.failure.json" \
          "$(budget_used "$role" 2>/dev/null || printf '%s' 0)" \
          "$INV/pre_dispatch/cache_critic_${role}.log" "$SNAPSHOT_REL" "$forced_rc"
        echo "         then rerun this same command without I4R_FORCE_RERUN: the forced run's output is replayed" >&2
        exit 1
      fi
      echo "FATAL: critic_$role's first cached output predates the forced rerun of round $ROUND, whose verdicts" >&2
      echo "  differ, and the forced run's preserved output (result_cache/conflicts/$role) cannot be replayed." >&2
      echo "  Retry with the same inputs and I4R_FORCE_RERUN=1 (raise MAX_TRIES if the budget is spent)." >&2
      exit 2
    fi
  fi
  if [ "$FORCE_RERUN" -eq 0 ] && load_role_cache "$role"; then
    rm -f "$BUDGET_ROOT/$role.permanent"
    echo "  [cache] critic_$role"
    exit 0
  fi
  if [ -f "$BUDGET_ROOT/$role.permanent" ]; then
    # An earlier invocation classified a provider failure for this role as permanent. That is a
    # property of one response, not of the role (credentials, quotas and configuration change
    # between invocations), and the attempt budget already bounds spending, so the role is
    # retried while budget remains. The marker stays on disk as evidence until the role completes.
    echo "  [note ] critic_$role carries a permanent-failure marker from an earlier invocation; retrying within the attempt budget" >&2
  fi

  while :; do
    # a worker whose driver was killed (-9 leaves this subshell running through its retry backoff) must not make
    # another provider call: a rerun owns the round now. $$ is the driver's pid inside this subshell.
    # First in the loop (2.6.9): before the pre-dispatch snapshot check, which may re-freeze the snapshot and
    # journal a note, and before claim_attempt, which would spend an attempt of the round-wide budget.
    if ! kill -0 "$$" 2>/dev/null; then
      echo "  [stop ] critic_$role: the driver (pid $$) is gone; this orphaned worker makes no further attempt" >&2
      exit 1
    fi
    if ! snapshot_ready_for_dispatch "$SNAPSHOT" round "$INPUT_SHA" "critic_$role"; then
      # Before the claim: this costs the role no attempt and writes no row.
      stop_before_dispatch "critic_$role" "$role" "$INV/results/${role}.failure.json" \
        "$(budget_used "$role" 2>/dev/null || printf '%s' 0)" \
        "$INV/pre_dispatch/critic_${role}.log" "$SNAPSHOT_REL" "$ready_rc"
      exit 1
    fi
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
    schema_digest=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$base_schema") || exit 1
    lifecycle_before_provider "$ACTIVE_CALL_ID" "critic_$role" critic role_inference \
      "$CRITIC_MODEL" "$CRITIC_EFFORT" "$prompt" "$call_started" "$schema_digest" \
      || { release_attempt "$role" "$attempt"; exit 1; }
    codex_exec "$prompt" "$schema" "$response" "$events" "$stderr_file"
    call_rc=$?
    call_cancelled=$CODEX_CANCELLED
    call_timed_out=$CODEX_TIMED_OUT
    call_ended=$(utc_now); call_ended_epoch=$(epoch_now)
    lifecycle_after_provider "$ACTIVE_CALL_ID" "$events" "$response" "$stderr_file" \
      "$call_started" "$call_ended" || exit 1
    valid=0
    validate_rc=0
    repair_blocked=0
    repair_orphaned=0
    # The helper log that explains a validate_rc of 5 (validate-critic's, or prepare-repair's).
    environment_log="$validation"
    if [ "$call_rc" -eq 0 ] && [ -s "$response" ]; then
      # Keep the exit code: 5 means the snapshot changed under the call (an environment fault),
      # not that the response is schema-invalid, and must never be re-sampled or repaired.
      "$HELPER" validate-critic --input "$response" --role "$role" --round "$ROUND" \
        --input-sha "$INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" \
        --schema "$schema" --markdown "$markdown" \
        > "$validation" 2>&1
      validate_rc=$?
      [ "$validate_rc" -ne 0 ] || valid=1
    else
      printf 'provider exit=%s; response missing or empty\n' "$call_rc" > "$validation"
    fi
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
        # A second valid review of identical input differed from the first (ordinary sampling
        # variance). The first cached object is never overwritten and this output is preserved
        # under result_cache/conflicts/ for review. This invocation keeps the valid output it
        # produced and bound in its own telemetry; a valid review is never discarded over a
        # tie-break between two valid reviews.
        echo "  [note ] critic_$role produced a second valid output for the same input; first cache left untouched, this output kept for this invocation" >&2
        rm -f "$BUDGET_ROOT/$role.permanent"
        echo "  [ok   ] critic_$role (second valid output; cache conflict recorded)"
        exit 0
      fi
      [ "$publish_rc" -eq 0 ] || exit 1
      rm -f "$BUDGET_ROOT/$role.permanent"
      echo "  [ok   ] critic_$role"
      exit 0
    fi

    combined="$attempt_dir/failure.log"
    { sed -n '1,200p' "$validation" 2>/dev/null; \
      sed -n '1,200p' "$stderr_file" 2>/dev/null; \
      sed -n '1,200p' "$events" 2>/dev/null; } > "$combined"

    # A clean provider exit with scientifically usable content may receive one small,
    # administration-only repair. The helper first proves the scientific fields and frozen
    # evidence are already valid; the repaired response must preserve those fields exactly.
    # Never after a snapshot integrity failure (validate_rc 5): the fault is not the response's.
    if [ "$call_cancelled" -eq 0 ] && [ "$call_rc" -eq 0 ] \
        && [ "$validate_rc" -ne "$SNAPSHOT_INTEGRITY_RC" ] \
        && [ -s "$response" ] && [ ! -f "$BUDGET_ROOT/$role.${INPUT_SHA%"${INPUT_SHA#????????????}"}.repair.used" ]; then
      repair_used=$(budget_used "$role" 2>/dev/null || printf '%s' "$MAX_TRIES")
      repair_attempt=$((repair_used + 1))
      if [ "$repair_used" -lt "$MAX_TRIES" ] && ! kill -0 "$$" 2>/dev/null; then
        # An orphaned worker (its driver killed with -9 during this call) makes no repair call either, and is
        # checked before any of the repair exists: its directory, its bound contract, and prepare-repair, which
        # re-validates the response against the snapshot and so may re-freeze it and journal a note. The
        # original attempt is still recorded below as what it was; then the worker stops.
        repair_orphaned=1
      elif [ "$repair_used" -lt "$MAX_TRIES" ]; then
        repair_dir="$INV/attempts/$role/$repair_attempt"
        mkdir -p "$repair_dir"
        repair_schema="$repair_dir/output.schema.json"
        repair_prompt="$repair_dir/repair_prompt.txt"
        repair_prepare_receipt="$repair_dir/repair_prepare.json"
        repair_response="$repair_dir/response.json"
        repair_events="$repair_dir/events.jsonl"
        repair_stderr="$repair_dir/stderr.txt"
        repair_markdown="$repair_dir/response.md"
        repair_validation="$repair_dir/validation.txt"
        repair_validation_receipt="$repair_dir/repair_validation.json"
        repair_started=$(utc_now); repair_started_epoch=$(epoch_now)
        repair_call_id="${INVOCATION_ID}:${role}:${repair_attempt}:${repair_started_epoch}:repair:$RANDOM"
        # Two steps, not `bind && prepare`, so prepare-repair's exit code stays visible: it
        # re-validates the original response against the snapshot, and 5 means the snapshot broke.
        repair_prepare_rc=1
        if "$HELPER" bind-contract --base "$base_schema" --output "$repair_schema" \
            --invocation-id "$repair_call_id" > "$repair_validation" 2>&1; then
          "$HELPER" prepare-repair --input "$response" --diagnostics "$validation" \
            --original-prompt "$prompt" --snapshot "$SNAPSHOT" --schema "$repair_schema" \
            --output "$repair_prompt" --receipt "$repair_prepare_receipt" \
            --role "$role" --round "$ROUND" --input-sha "$INPUT_SHA" \
            --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" \
            >> "$repair_validation" 2>&1
          repair_prepare_rc=$?
        fi
        if [ "$repair_prepare_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
          validate_rc="$SNAPSHOT_INTEGRITY_RC"
          environment_log="$repair_validation"
        elif [ "$repair_prepare_rc" -eq 0 ] && ! kill -0 "$$" 2>/dev/null; then
          # Checked again here: the driver may have been killed while bind-contract or
          # prepare-repair ran, after the check above. An orphan claims no attempt and makes no
          # paid repair call; the original attempt is still recorded below.
          repair_orphaned=1
        elif [ "$repair_prepare_rc" -eq 0 ] \
            && ! snapshot_ready_for_dispatch "$SNAPSHOT" round "$INPUT_SHA" "critic_$role"; then
          # The repair is a paid dispatch too, so it is checked before its claim like every
          # attempt. The original attempt is still recorded below as what it was; then the role
          # stops.
          repair_blocked="$ready_rc"
        elif [ "$repair_prepare_rc" -eq 0 ]; then
          claimed_repair_attempt=$(claim_attempt "$role") || exit 1
          if [ "$claimed_repair_attempt" != "$repair_attempt" ]; then
            # The journal moved between the budget read and the claim, so another process
            # is working this role. Bailing is right; doing it silently was not.
            echo "  [stop ] critic_$role: attempt budget advanced between read and claim" >&2
            echo "         (expected $repair_attempt, claimed $claimed_repair_attempt);" >&2
            echo "         another run is active on this role -- wait for it, then rerun" >&2
            exit 1
          fi
          # Scoped to the round's input hash. The marker had no removal site anywhere, so a
          # focused repair consumed by a HARNESS defect locked the role out of repair for the rest
          # of the round even after the defect was fixed. Keying it by input keeps the
          # one-repair-per-role rule exactly as pinned within a snapshot, while a genuinely new
          # snapshot -- which is what a fixed harness produces -- starts clean.
          atomic_text "$repair_call_id" \
            "$BUDGET_ROOT/$role.${INPUT_SHA%"${INPUT_SHA#????????????}"}.repair.used" || exit 1
          record_telemetry "$role" "$attempt" 1 schema_invalid repairable "$call_rc" \
            "$call_started" "$call_started_epoch" "$call_ended" "$call_ended_epoch" \
            "$events" "$response" "$stderr_file" "$prompt" none schema || exit 1

          echo "  [fix  ] critic_$role focused output repair $repair_attempt/$MAX_TRIES"
          ACTIVE_CALL_ID="$repair_call_id"
          repair_schema_digest=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$base_schema") || exit 1
          lifecycle_before_provider "$ACTIVE_CALL_ID" "critic_$role" critic output_repair \
            "$CRITIC_MODEL" "$CRITIC_EFFORT" "$repair_prompt" "$repair_started" \
            "$repair_schema_digest" || exit 1
          codex_exec "$repair_prompt" "$repair_schema" "$repair_response" \
            "$repair_events" "$repair_stderr"
          repair_rc=$?
          repair_cancelled=$CODEX_CANCELLED
          repair_ended=$(utc_now); repair_ended_epoch=$(epoch_now)
          lifecycle_after_provider "$ACTIVE_CALL_ID" "$repair_events" "$repair_response" \
            "$repair_stderr" "$repair_started" "$repair_ended" || exit 1
          repair_valid=0
          repair_validate_rc=0
          if [ "$repair_rc" -eq 0 ] && [ -s "$repair_response" ]; then
            "$HELPER" validate-repair --before "$response" --input "$repair_response" \
              --role "$role" --round "$ROUND" --input-sha "$INPUT_SHA" \
              --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" --schema "$repair_schema" \
              --markdown "$repair_markdown" --receipt "$repair_validation_receipt" \
              > "$repair_validation" 2>&1
            repair_validate_rc=$?
            [ "$repair_validate_rc" -ne 0 ] || repair_valid=1
          else
            printf 'provider exit=%s; repair response missing or empty\n' "$repair_rc" \
              > "$repair_validation"
          fi
          if [ "$repair_valid" -eq 1 ]; then
            record_telemetry "$role" "$repair_attempt" 1 complete "" "$repair_rc" \
              "$repair_started" "$repair_started_epoch" "$repair_ended" "$repair_ended_epoch" \
              "$repair_events" "$repair_response" "$repair_stderr" "$repair_prompt" write "" \
              output_repair \
              || exit 1
            atomic_copy "$repair_response" "$INV/results/critic_${role}.json" || exit 1
            atomic_copy "$repair_markdown" "$INV/results/critic_${role}.md" || exit 1
            "$HELPER" complete --output "$INV/results/${role}.complete.json" \
              --input-sha "$INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" \
              --round "$ROUND" --role "$role" --attempts "$repair_attempt" \
              --response "$INV/results/critic_${role}.json" \
              --markdown "$INV/results/critic_${role}.md" || exit 1
            publish_role_cache "$role"
            publish_rc=$?
            if [ "$publish_rc" -eq 2 ]; then
              echo "  [note ] critic_$role repair produced a second valid output for the same input; first cache left untouched, this output kept for this invocation" >&2
              rm -f "$BUDGET_ROOT/$role.permanent"
              echo "  [ok   ] critic_$role (second valid output; cache conflict recorded)"
              exit 0
            fi
            [ "$publish_rc" -eq 0 ] || exit 1
            rm -f "$BUDGET_ROOT/$role.permanent"
            echo "  [ok   ] critic_$role repaired without scientific changes"
            exit 0
          fi

          repair_combined="$repair_dir/failure.log"
          { sed -n '1,200p' "$repair_validation" 2>/dev/null; \
            sed -n '1,200p' "$repair_stderr" 2>/dev/null; \
            sed -n '1,200p' "$repair_events" 2>/dev/null; } > "$repair_combined"
          if [ "$repair_cancelled" -ne 0 ]; then
            repair_class=transient; repair_outcome=cancelled; repair_error=transient
          elif [ "$repair_validate_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
            # The snapshot changed during the repair call: an environment fault, never retried.
            repair_class=permanent; repair_outcome=failed; repair_error=environment
          elif [ "$repair_rc" -eq 0 ]; then
            # A failed focused repair does not condemn the role either; fall through to a
            # fresh attempt while budget remains.
            repair_class=transient; repair_outcome=schema_invalid; repair_error=schema
          else
            repair_classification=$(classify_provider_failure "$repair_combined") || exit 1
            repair_class=${repair_classification%% *}
            repair_outcome=failed
            repair_error=${repair_classification#* }
          fi
          record_telemetry "$role" "$repair_attempt" 1 "$repair_outcome" "$repair_class" \
            "$repair_rc" "$repair_started" "$repair_started_epoch" "$repair_ended" \
            "$repair_ended_epoch" "$repair_events" "$repair_response" "$repair_stderr" \
            "$repair_prompt" none "$repair_error" output_repair || exit 1
          ACTIVE_CALL_ID=""
          if [ "$repair_cancelled" -ne 0 ]; then exit "$repair_cancelled"; fi
          if [ "$repair_error" = environment ] && snapshot_mode_unstable_in "$repair_validation"; then
            # Content verified: the repaired response is not blamed for a mode that keeps changing.
            "$HELPER" failure --output "$INV/results/${role}.failure.json" --role "$role" \
              --attempts "$repair_attempt" --failure-class snapshot_mode_unstable \
              --log "$repair_combined" >/dev/null 2>&1 || true
            echo "  [fail ] critic_$role: snapshot modes kept changing after the focused repair call (content verified; environment, not the critic); not retried; see $repair_combined" >&2
            echo "         $SNAPSHOT_REL: $SNAPSHOT_MODE_UNSTABLE_REMEDY" >&2
            exit 1
          fi
          if [ "$repair_error" = environment ]; then
            # Not the role's fault: no permanent marker, no backoff, no further call.
            "$HELPER" failure --output "$INV/results/${role}.failure.json" --role "$role" \
              --attempts "$repair_attempt" --failure-class snapshot_integrity \
              --log "$repair_combined" >/dev/null 2>&1 || true
            echo "  [fail ] critic_$role: snapshot changed during the focused repair call (environment, not the critic); not retried; see $repair_combined" >&2
            echo "         remedy: $(snapshot_remedy "$SNAPSHOT_REL") this round" >&2
            exit 1
          fi
          if [ "$repair_class" = transient ] && [ "$repair_attempt" -lt "$MAX_TRIES" ]; then
            retry_delay=$((RETRY_BACKOFF_SECONDS * repair_attempt))
            journal_retry_backoff "critic_$role" "$repair_attempt" "$repair_error" \
              "$retry_delay" || exit 1
            continue
          fi
          mark_permanent "$role" "$(sed -n '1,40p' "$repair_combined")"
          "$HELPER" failure --output "$INV/results/${role}.failure.json" --role "$role" \
            --attempts "$repair_attempt" --failure-class output_repair_failed \
            --log "$repair_combined" >/dev/null 2>&1 || true
          echo "  [fail ] critic_$role focused repair failed" >&2
          exit 1
        fi
      fi
    fi
    if [ "${call_timed_out:-0}" -ne 0 ]; then
      # A wall-clock timeout is a property of this attempt, not of the role, exactly as a
      # cancellation or a schema-invalid sample is. The text classifier cannot see it -- a killed
      # call writes no distinguishing line into the combined log -- so without this branch it fell
      # through to permanent and ended the round on attempt 1 with the retry budget unspent.
      printf 'provider call exceeded the %ss wall-clock ceiling and was stopped\n' \
        "$PROVIDER_TIMEOUT_SECONDS" >> "$combined"
      failure_class=transient
      failure_outcome=failed
      error_class=timeout
    elif [ "$call_cancelled" -ne 0 ]; then
      printf 'provider cancelled by signal (exit %s)\n' "$call_cancelled" >> "$combined"
      failure_class=transient
      failure_outcome=cancelled
      error_class=transient
    elif [ "$validate_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
      # The frozen snapshot's content changed during the call. Re-sampling cannot help and the
      # critic did nothing wrong: one call, recorded as an environment failure, and a stop. On
      # audit A20 this was labelled schema and re-dispatched twelve times.
      failure_class=permanent
      failure_outcome=failed
      error_class=environment
    elif [ "$call_rc" -eq 0 ]; then
      # A schema-invalid review is a property of one sample, not of the role: on audit
      # A34 all three such critics passed on a fresh attempt. Re-sample within
      # MAX_TRIES instead of marking the role permanent with budget unspent.
      failure_class=transient
      failure_outcome=schema_invalid
      error_class=schema
    else
      failure_classification=$(classify_provider_failure "$combined") || exit 1
      failure_class=${failure_classification%% *}
      failure_outcome=failed
      error_class=${failure_classification#* }
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
    if [ "$error_class" = environment ] && snapshot_mode_unstable_in "$environment_log"; then
      # The content verified after the call; only the modes would not stay frozen. The paid
      # response is not reported as changed: it is kept under $attempt_dir for inspection, and
      # the role stops because nothing can be certified while the modes keep changing.
      "$HELPER" failure --output "$INV/results/${role}.failure.json" --role "$role" \
        --attempts "$attempt" --failure-class snapshot_mode_unstable --log "$combined" >/dev/null 2>&1 || true
      echo "  [fail ] critic_$role: snapshot modes kept changing after the provider call (content verified; environment, not the critic); response kept, not retried; see $combined" >&2
      echo "         $SNAPSHOT_REL: $SNAPSHOT_MODE_UNSTABLE_REMEDY" >&2
      exit 1
    fi
    if [ "$error_class" = environment ]; then
      # Before the permanent branch on purpose: no permanent marker, the fault is not the role's.
      "$HELPER" failure --output "$INV/results/${role}.failure.json" --role "$role" \
        --attempts "$attempt" --failure-class snapshot_integrity --log "$combined" >/dev/null 2>&1 || true
      echo "  [fail ] critic_$role: snapshot changed after the provider call (environment, not the critic); not retried; see $combined" >&2
      echo "         remedy: $(snapshot_remedy "$SNAPSHOT_REL") this round" >&2
      exit 1
    fi
    if [ "$repair_blocked" -ne 0 ]; then
      # The response was recorded above as what it was; the snapshot no longer verifies before
      # the focused repair, so neither the repair nor a fresh attempt is dispatched against it.
      stop_before_dispatch "critic_$role (focused repair)" "$role" \
        "$INV/results/${role}.failure.json" "$attempt" \
        "$INV/pre_dispatch/critic_${role}.log" "$SNAPSHOT_REL" "$repair_blocked"
      exit 1
    fi
    if [ "$repair_orphaned" -eq 1 ]; then
      echo "  [stop ] critic_$role: the driver (pid $$) is gone; this orphaned worker makes no repair call" >&2
      exit 1
    fi
    if [ "$failure_class" = permanent ]; then
      mark_permanent "$role" "$(sed -n '1,40p' "$combined")"
      "$HELPER" failure --output "$INV/results/${role}.failure.json" --role "$role" \
        --attempts "$attempt" --failure-class permanent --log "$combined" >/dev/null 2>&1 || true
      failure_message=$(provider_error_message "$combined")
      echo "  [fail ] critic_$role permanent failure on attempt $attempt ($error_class)${failure_message:+: $failure_message}; see $combined" >&2
      exit 1
    fi
    if [ "$attempt" -ge "$MAX_TRIES" ]; then
      "$HELPER" failure --output "$INV/results/${role}.failure.json" --role "$role" \
        --attempts "$attempt" --failure-class transient_exhausted --log "$combined" >/dev/null 2>&1 || true
      failure_message=$(provider_error_message "$combined")
      echo "  [fail ] critic_$role exhausted transient attempts ($error_class on the last)${failure_message:+: $failure_message}; see $combined" >&2
      exit 1
    fi
    retry_delay=$((RETRY_BACKOFF_SECONDS * attempt))
    echo "  [wait ] critic_$role $error_class failure; retrying in ${retry_delay}s"
    journal_retry_backoff "critic_$role" "$attempt" "$error_class" "$retry_delay" || exit 1
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
quorum_integrity=0
quorum_unstable=0
for role in $NAMES; do
  "$HELPER" verify-complete --sidecar "$INV/results/${role}.complete.json" \
    --response "$INV/results/critic_${role}.json" --markdown "$INV/results/critic_${role}.md" \
    --role "$role" --round "$ROUND" --input-sha "$INPUT_SHA" \
    --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" >/dev/null 2> "$INV/quorum_${role}.log"
  quorum_rc=$?
  [ "$quorum_rc" -ne 0 ] || rm -f "$INV/quorum_${role}.log"
  if [ "$quorum_rc" -ne 0 ]; then
    missing_roles="$missing_roles $role"
    # 5: the result is fine but the snapshot it is bound to no longer verifies (or, content
    # verified, its modes would not stay frozen).
    if [ "$quorum_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
      if snapshot_mode_unstable_in "$INV/quorum_${role}.log"; then
        quorum_unstable=1
      else
        quorum_integrity=1
      fi
    fi
  fi
done
if [ -n "$missing_roles" ]; then
  FINAL_DETAIL="exact role quorum failed; missing:$missing_roles"
  integrity_summary=$(snapshot_integrity_summary)
  if [ -z "$integrity_summary" ] && [ "$quorum_integrity" -eq 1 ]; then
    integrity_summary="changed after the critics ran"
  elif [ -z "$integrity_summary" ] && [ "$quorum_unstable" -eq 1 ]; then
    integrity_summary="content verified but its modes kept changing after the critics ran"
  fi
  if [ -n "$integrity_summary" ]; then
    if [ "$quorum_integrity" -eq 1 ] \
        || grep -l '"failure_class": *"snapshot_integrity"' "$INV"/results/*.failure.json >/dev/null 2>&1; then
      FINAL_DETAIL="$FINAL_DETAIL; round snapshot integrity check failed ($integrity_summary); remedy: $(snapshot_remedy "$SNAPSHOT_REL")"
    else
      FINAL_DETAIL="$FINAL_DETAIL; round snapshot $SNAPSHOT_REL ($integrity_summary): $SNAPSHOT_MODE_UNSTABLE_REMEDY"
    fi
  fi
elif [ "$all_failed" -ne 0 ]; then
  # Every role has a verified complete result, but a worker still exited nonzero. Say so
  # instead of the old "missing: unknown", which named nothing and hid a met quorum.
  FINAL_DETAIL="all roles complete but a critic worker exited nonzero; inspect the driver log"
fi
if [ "$all_failed" -ne 0 ] || [ -n "$missing_roles" ]; then
  FINAL_ADJUDICATION="not_reached"
  echo "FATAL: $FINAL_DETAIL" >&2
  exit 1
fi

# Routine model synthesis is intentionally absent. The deterministic packet invokes a separate
# the adjudicator model only when the machine policy identifies a targeted trigger.
if [ -n "${ADJUDICATE+x}" ]; then
  ADJUDICATION_MODE="$ADJUDICATE"
elif [ -n "${SYNTH+x}" ]; then
  echo "  [note ] SYNTH is deprecated; use ADJUDICATE" >&2
  if [ "$SYNTH" = "1" ]; then ADJUDICATION_MODE=1; else ADJUDICATION_MODE=0; fi
else
  ADJUDICATION_MODE=auto
fi
case "$ADJUDICATION_MODE" in 0|1|auto) ;; *) FINAL_DETAIL="ADJUDICATE must be 0, auto, or 1"; echo "FATAL: $FINAL_DETAIL" >&2; exit 1 ;; esac

PACKET="$INV/adjudication_packet.json"
TRIGGER_COUNT=$("$HELPER" packet --results "$INV/results" --output "$PACKET" \
  --round "$ROUND" --input-sha "$INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" \
  --workspace "$WS" --mode "$ADJUDICATION_MODE" 2> "$INV/packet.stderr")
packet_rc=$?
cat "$INV/packet.stderr" >&2 2>/dev/null
if [ "$packet_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ] && snapshot_mode_unstable_in "$INV/packet.stderr"; then
  FINAL_DETAIL="deterministic adjudication packet failed: round snapshot $SNAPSHOT_REL: $SNAPSHOT_MODE_UNSTABLE_REMEDY"
  exit 1
elif [ "$packet_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
  FINAL_DETAIL="deterministic adjudication packet failed: the round snapshot $SNAPSHOT_REL failed its integrity check; remedy: $(snapshot_remedy "$SNAPSHOT_REL")"
  exit 1
elif [ "$packet_rc" -ne 0 ]; then
  FINAL_DETAIL="deterministic adjudication packet failed"; exit 1
fi
case "$TRIGGER_COUNT" in ''|*[!0-9]*) FINAL_DETAIL="invalid evidence-ready trigger count"; exit 1 ;; esac
DEFERRED_COUNT=$(python3 - "$PACKET" <<'PY'
import json, pathlib, sys
packet = json.loads(pathlib.Path(sys.argv[1]).read_text())
rows = packet.get("deferred_rows")
if not isinstance(rows, list):
    raise SystemExit(1)
print(len(rows))
PY
) || { FINAL_DETAIL="could not count deferred adjudication evidence gaps"; exit 1; }
case "$DEFERRED_COUNT" in ''|*[!0-9]*) FINAL_DETAIL="invalid deferred trigger count"; exit 1 ;; esac

TARGET_INPUT_SHA=""
TARGET_SNAPSHOT=""
TARGET_SNAPSHOT_REL=""
TARGET_PROMPT=""
TARGET_SCHEMA=""
if [ "$TRIGGER_COUNT" -gt 0 ] \
  && { [ "$ADJUDICATION_MODE" = 1 ] || [ "$ADJUDICATION_MODE" = auto ]; }; then
  TARGET_META="$INV/target_snapshot.json"
  "$HELPER" target-snapshot --workspace "$WS" --out "$OUT" --round-snapshot "$SNAPSHOT" \
    --packet "$PACKET" --schema "$SNAPSHOT/schemas/targeted_adjudication.schema.json" \
    --model "$ADJUDICATOR_MODEL" --effort "$ADJUDICATOR_EFFORT" \
    --runtime-script "$SCRIPT_DIR/run_critique.sh" > "$TARGET_META" 2> "$INV/target_snapshot.stderr"
  target_snapshot_rc=$?
  cat "$INV/target_snapshot.stderr" >&2 2>/dev/null
  if [ "$target_snapshot_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
    # Building the target snapshot re-verifies the round snapshot too: name the one that failed.
    faulted_snapshot=$(snapshot_named_in "$INV/target_snapshot.stderr")
    case "$faulted_snapshot" in */snapshots/*) faulted_label="round snapshot" ;; *) faulted_label="target snapshot" ;; esac
    if snapshot_mode_unstable_in "$INV/target_snapshot.stderr"; then
      FINAL_DETAIL="targeted-adjudication snapshot failed: $faulted_label ${faulted_snapshot:-named above}: $SNAPSHOT_MODE_UNSTABLE_REMEDY; no adjudicator call was made"
    else
      FINAL_DETAIL="targeted-adjudication snapshot failed: $faulted_label ${faulted_snapshot:-named above} failed its integrity check; no adjudicator call was made; remedy: $(snapshot_remedy "${faulted_snapshot:-<the snapshot directory named above>}")"
    fi
    echo "FATAL: $FINAL_DETAIL" >&2
    exit 1
  elif [ "$target_snapshot_rc" -ne 0 ]; then
    FINAL_DETAIL="immutable targeted-adjudication snapshot failed"; exit 1
  fi
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
  PROVIDER_WORKSPACE="$TARGET_SNAPSHOT"
  TELEMETRY_INPUT_SHA="$TARGET_INPUT_SHA"
  target_budget="targeted_adjudicator"
  target_cache_root="$OUT/adjudication_cache"
  target_cache="$target_cache_root/$TARGET_INPUT_SHA"
  mkdir -p "$target_cache_root"
  target_json="$INV/results/TARGETED_ADJUDICATION.json"
  target_md="$INV/results/TARGETED_ADJUDICATION.md"
  target_prompt="$TARGET_PROMPT"

  if [ -f "$target_cache.conflict" ]; then
    # Same rule as the critics: two valid same-input outputs differing is ordinary sampling
    # variance. The first cached object is canonical and the alternative is preserved for review;
    # neither fact makes the completed adjudication unusable. This used to exit 1 -- and because
    # the check precedes the FORCE_RERUN branch below, nothing could get past it and no code
    # anywhere removes the marker, so one conflict wedged the round permanently.
    echo "  [note ] targeted adjudication has a recorded same-input cache conflict; first cached output is canonical" >&2
    if [ "$FORCE_RERUN" -eq 0 ]; then
      # The differing adjudication came from a forced rerun, whose run used it; loading the first cache in
      # a plain rerun would silently integrate the pre-force adjudication instead.
      echo "FATAL: the cached targeted adjudication predates the forced rerun of round $ROUND, whose" >&2
      echo "  adjudication differs (adjudication_cache conflict). A plain rerun would restore the old one." >&2
      echo "  Retry with the same inputs and I4R_FORCE_RERUN=1 (raise MAX_TRIES if the budget is spent)." >&2
      exit 2
    fi
  fi

  if [ "$FORCE_RERUN" -eq 0 ] && [ -s "$target_cache/output.json" ]; then
    cached_md="$INV/results/.targeted.cache.$$.md"
    # A cache that fails with 5 is not replayed, but it is not the cache's fault either: the loop
    # below re-checks the target snapshot before any dispatch and stops there without a call.
    if "$HELPER" validate-adjudication --input "$target_cache/output.json" \
      --packet "$PACKET" --input-sha "$TARGET_INPUT_SHA" \
      --markdown "$cached_md" >/dev/null 2>&1; then
      atomic_copy "$target_cache/output.json" "$target_json" || exit 1
      mv -f "$cached_md" "$target_md" || exit 1
      target_cached_attempts=$(python3 - "$target_cache/complete.json" <<'PY'
import json, pathlib, sys
value=json.loads(pathlib.Path(sys.argv[1]).read_text()).get("attempts")
if not isinstance(value,int) or not 1 <= value <= 20: raise SystemExit(1)  # operator ceiling maximum (MAX_TRIES)
print(value)
PY
      ) || exit 1
      "$HELPER" complete --output "$INV/results/targeted_adjudicator.complete.json" \
        --input-sha "$TARGET_INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" --round "$ROUND" \
        --role targeted_adjudicator --attempts "$target_cached_attempts" \
        --response "$target_json" --markdown "$target_md" || exit 1
      cache_time=$(utc_now); cache_epoch=$(epoch_now)
      target_source_invocation=""
      [ -f "$target_cache/source_invocation_id" ] \
        && target_source_invocation=$(sed -n '1p' "$target_cache/source_invocation_id" 2>/dev/null || true)
      record_telemetry targeted_adjudicator 0 0 cache_hit "" 0 \
        "$cache_time" "$cache_epoch" "$cache_time" "$cache_epoch" "" "$target_json" "" \
        "$target_prompt" read "" cache_replay "$target_source_invocation" || exit 1
      exit 0
    fi
  fi
  if [ -f "$BUDGET_ROOT/$target_budget.permanent" ]; then
    # Same rule as the critics: an earlier permanent classification describes one response, not
    # the adjudicator; retry within the attempt budget and keep the marker as evidence.
    echo "  [note ] targeted adjudicator carries a permanent-failure marker from an earlier invocation; retrying within the attempt budget" >&2
  fi

  while :; do
    # The critic workers' guard, first in the loop here too: an adjudicator whose driver was killed with -9 during
    # its retry backoff would otherwise re-check the target snapshot, claim the round's next attempt and make a
    # max-effort call for a dead invocation, alongside the rerun that owns the round now. $$ is the driver's pid.
    if ! kill -0 "$$" 2>/dev/null; then
      echo "  [stop ] targeted_adjudicator: the driver (pid $$) is gone; this orphaned worker makes no further attempt" >&2
      exit 1
    fi
    if ! snapshot_ready_for_dispatch "$TARGET_SNAPSHOT" target "$TARGET_INPUT_SHA" critic_synth; then
      # Before the claim: no attempt, no reservation, no telemetry row, no call. The critic
      # quorum is still journaled and projected through the ADJUDICATION_FAILED path.
      stop_before_dispatch targeted_adjudicator targeted_adjudicator \
        "$INV/results/targeted_adjudicator.failure.json" \
        "$(budget_used "$target_budget" 2>/dev/null || printf '%s' 0)" \
        "$INV/pre_dispatch/critic_synth.log" "$TARGET_SNAPSHOT_REL" "$ready_rc"
      exit 1
    fi
    if ! attempt=$(claim_attempt "$target_budget"); then
      # The critic path prints this; the adjudicator bailed silently, so an operator saw a
      # non-zero exit with no reason recorded anywhere.
      echo "  [stop ] targeted adjudication exhausted its round-wide $MAX_TRIES-attempt budget" >&2
      if [ "$FORCE_RERUN" -eq 1 ] && [ -s "$target_cache/output.json" ] && [ ! -f "$target_cache.conflict" ]; then
        # a validated adjudication of this exact packet is already cached: a plain rerun reuses it free
        echo "         a validated adjudication of this packet is cached: rerun WITHOUT I4R_FORCE_RERUN to" >&2
        echo "         reuse it at no cost, or raise MAX_TRIES=<n> for a fresh call" >&2
      else
        echo "         raise it with MAX_TRIES=<n> (not I4R_MAX_ATTEMPTS, which this driver" >&2
        echo "         overwrites) and rerun this round" >&2
      fi
      exit 1
    fi
    attempt_dir="$INV/attempts/targeted_adjudicator/$attempt"
    mkdir -p "$attempt_dir"
    # The critic path announces every attempt, retry and terminal exit; this one printed nothing
    # between the four critics completing and "targeted adjudication FAILED", so three max-effort
    # calls and three failures left zero lines in the driver log.
    echo "  [run  ] targeted_adjudicator attempt $attempt/$MAX_TRIES ($ADJUDICATOR_MODEL/$ADJUDICATOR_EFFORT)" >&2
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
    target_schema_digest=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$TARGET_SCHEMA") || exit 1
    lifecycle_before_provider "$ACTIVE_CALL_ID" critic_synth adjudicator role_inference \
      "$ADJUDICATOR_MODEL" "$ADJUDICATOR_EFFORT" "$target_prompt" "$call_started" \
      "$target_schema_digest" || { release_attempt "$target_budget" "$attempt"; exit 1; }
    codex_exec "$target_prompt" "$attempt_schema" \
      "$response" "$events" "$stderr_file" "$ADJUDICATOR_MODEL" "$ADJUDICATOR_EFFORT"
    call_rc=$?
    call_cancelled=$CODEX_CANCELLED
    call_timed_out=$CODEX_TIMED_OUT
    call_ended=$(utc_now); call_ended_epoch=$(epoch_now)
    lifecycle_after_provider "$ACTIVE_CALL_ID" "$events" "$response" "$stderr_file" \
      "$call_started" "$call_ended" || exit 1
    valid=0
    validate_rc=0
    if [ "$call_rc" -eq 0 ] && [ -s "$response" ]; then
      "$HELPER" validate-adjudication --input "$response" --packet "$PACKET" \
        --input-sha "$TARGET_INPUT_SHA" --schema "$attempt_schema" \
        --markdown "$markdown" > "$validation" 2>&1
      validate_rc=$?
      [ "$validate_rc" -ne 0 ] || valid=1
    else
      printf 'provider exit=%s; response missing or empty\n' "$call_rc" > "$validation"
    fi
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
        "$HELPER" validate-adjudication --input "$target_cache/output.json" \
          --packet "$PACKET" --input-sha "$TARGET_INPUT_SHA" \
          --markdown "$existing_md" >/dev/null 2>&1
        existing_rc=$?
        if [ "$existing_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
          # The target snapshot is at fault, not the cache: never retire a valid cache over it.
          echo "  [fail ] targeted_adjudicator: target snapshot failed its integrity check while comparing the cache; cache left in place" >&2
          exit 1
        fi
        if [ "$existing_rc" -eq 0 ]; then
          if "$HELPER" critic-equivalent --left "$target_json" \
            --right "$target_cache/output.json" >/dev/null 2>&1 \
            && cmp -s "$target_md" "$existing_md"; then
            rm -f "$existing_md" 2>/dev/null || true
            # the forced adjudication reproduced the cached one: an earlier forced run's differing
            # output no longer describes the round, so its marker must not block later plain reruns
            [ "$FORCE_RERUN" -eq 1 ] && rm -f "$target_cache.conflict"
            echo "  [ok   ] targeted_adjudicator attempt $attempt (same as the cached adjudication)"
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
          # The critic path treats this exact condition as a note and keeps its valid output
          # (see the [note ] branches above): two valid same-input reviews that differ is
          # ordinary sampling variance, the first cached object stays canonical, and the
          # alternative is preserved for review. The adjudicator alone made it a hard stop that
          # nothing on disk could clear -- I4R_FORCE_RERUN does not help, because the cache read
          # precedes it -- so a round that had already produced a valid adjudication was thrown
          # away over a tie-break between two valid adjudications. Same rule for both now.
          #
          # NOT covered by tests/test_run_critique.sh, and the reason is worth recording: reaching
          # this branch needs the SAME adjudication packet to trigger twice, and the trigger count
          # is computed from unresolved critic rows that the first round resolves. A rerun with
          # I4R_FORCE_RERUN re-dispatches the critics but produces TRIGGER_COUNT=0, so the
          # adjudicator is never called again. That is also why the hard stop sat here unnoticed:
          # the path is nearly unreachable, and fatal when reached.
          echo "  [note ] targeted adjudication produced a second valid output for the same input;" >&2
          echo "          first cache left untouched, this output kept for this invocation" >&2
          echo "          conflict recorded at $conflict_dir" >&2
          echo "  [ok   ] targeted_adjudicator (second valid output; cache conflict recorded)"
          rm -f "$BUDGET_ROOT/$target_budget.permanent"
          exit 0
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
      atomic_text "$ACTIVE_CALL_ID" "$target_stage/source_invocation_id" || exit 1
      chmod 444 "$target_stage"/* 2>/dev/null || true
      chmod 555 "$target_stage" 2>/dev/null || true
      mv "$target_stage" "$target_cache" || exit 1
      # The critic paths all clear their marker on success; this one never did, so a single early
      # failure left a stale (and multi-hundred-kilobyte) marker in every later run of the round.
      rm -f "$BUDGET_ROOT/$target_budget.permanent"
      echo "  [ok   ] targeted_adjudicator attempt $attempt/$MAX_TRIES" >&2
      exit 0
    fi
    combined="$attempt_dir/failure.log"
    { sed -n '1,200p' "$validation" 2>/dev/null; sed -n '1,200p' "$stderr_file" 2>/dev/null; \
      sed -n '1,200p' "$events" 2>/dev/null; } > "$combined"
    if [ "${call_timed_out:-0}" -ne 0 ]; then
      # Same reasoning as the critic path: the text classifier cannot see a wall-clock kill, so
      # without this branch a timeout fell through to permanent and ended the round on attempt 1.
      printf 'provider call exceeded the %ss wall-clock ceiling and was stopped\n' \
        "$PROVIDER_TIMEOUT_SECONDS" >> "$combined"
      failure_class=transient; failure_outcome=failed; error_class=timeout
    elif [ "$call_cancelled" -ne 0 ]; then
      printf 'provider cancelled by signal (exit %s)\n' "$call_cancelled" >> "$combined"
      failure_class=transient; failure_outcome=cancelled; error_class=transient
    elif [ "$validate_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
      # The target snapshot changed during the call: an environment fault, never re-sampled.
      failure_class=permanent; failure_outcome=failed; error_class=environment
    elif [ "$call_rc" -eq 0 ]; then
      # Same rule as the critics above: a schema-invalid response is a property of one sample,
      # not of the adjudicator. Round 2 of audit A38 died here at attempt 1 of
      # 3 -- one bad artifact ref out of ten in an otherwise complete, well-reasoned response --
      # because this branch alone said permanent while the critic branch says transient.
      failure_class=transient; failure_outcome=schema_invalid; error_class=schema
    else
      failure_classification=$(classify_provider_failure "$combined") || exit 1
      failure_class=${failure_classification%% *}
      failure_outcome=failed
      error_class=${failure_classification#* }
    fi
    record_telemetry targeted_adjudicator "$attempt" 1 "$failure_outcome" "$failure_class" "$call_rc" \
      "$call_started" "$call_started_epoch" "$call_ended" "$call_ended_epoch" \
      "$events" "$response" "$stderr_file" "$target_prompt" none "$error_class" || exit 1
    [ "$call_cancelled" -eq 0 ] || exit "$call_cancelled"
    if [ "$error_class" = environment ] && snapshot_mode_unstable_in "$validation"; then
      # Content verified; only the modes would not stay frozen (see the critic path).
      "$HELPER" failure --output "$INV/results/targeted_adjudicator.failure.json" \
        --role targeted_adjudicator --attempts "$attempt" --failure-class snapshot_mode_unstable \
        --log "$combined" >/dev/null 2>&1 || true
      unstable_snapshot=$(snapshot_named_in "$validation")
      echo "  [fail ] targeted_adjudicator: snapshot modes kept changing after the provider call (content verified; environment, not the adjudicator); response kept, not retried; see $combined" >&2
      echo "         ${unstable_snapshot:-$TARGET_SNAPSHOT_REL}: $SNAPSHOT_MODE_UNSTABLE_REMEDY" >&2
      exit 1
    fi
    if [ "$error_class" = environment ]; then
      # Ahead of the generic failure record, mark_permanent and journal_retry_backoff below
      # (which would refuse this class): the adjudicator did nothing wrong.
      "$HELPER" failure --output "$INV/results/targeted_adjudicator.failure.json" \
        --role targeted_adjudicator --attempts "$attempt" --failure-class snapshot_integrity \
        --log "$combined" >/dev/null 2>&1 || true
      changed_snapshot=$(snapshot_named_in "$validation")
      [ -n "$changed_snapshot" ] || changed_snapshot="$TARGET_SNAPSHOT_REL"
      case "$changed_snapshot" in */target_snapshots/*) changed_label="target snapshot" ;; *) changed_label="round snapshot" ;; esac
      echo "  [fail ] targeted_adjudicator: $changed_label changed after the provider call (environment, not the adjudicator); not retried; see $combined" >&2
      echo "         remedy: $(snapshot_remedy "$changed_snapshot") this round" >&2
      exit 1
    fi
    "$HELPER" failure --output "$INV/results/targeted_adjudicator.failure.json" \
      --role targeted_adjudicator --attempts "$attempt" --failure-class "$failure_class" \
      --log "$combined" >/dev/null 2>&1 || true
    if [ "$failure_class" = permanent ]; then
      mark_permanent "$target_budget" "$(sed -n '1,40p' "$combined")"
      echo "  [fail ] targeted_adjudicator permanent failure on attempt $attempt ($error_class); see $combined" >&2
      exit 1
    fi
    if [ "$attempt" -ge "$MAX_TRIES" ]; then
      echo "  [fail ] targeted_adjudicator exhausted its $MAX_TRIES-attempt budget ($error_class on the last attempt); see $combined" >&2
      exit 1
    fi
    retry_delay=$((RETRY_BACKOFF_SECONDS * attempt))
    echo "  [wait ] targeted_adjudicator $error_class failure on attempt $attempt; retrying in ${retry_delay}s" >&2
    journal_retry_backoff critic_synth "$attempt" "$error_class" "$retry_delay" || exit 1
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
      if execute_targeted_adjudication; then
        FINAL_ADJUDICATION="complete"
      else
        FINAL_ADJUDICATION="failed"
        FINAL_DETAIL="targeted adjudication failed"
        integrity_summary=$(snapshot_integrity_summary)
        [ -z "$integrity_summary" ] || FINAL_DETAIL="$FINAL_DETAIL; target snapshot integrity check failed ($integrity_summary); $(snapshot_fault_remedy "$TARGET_SNAPSHOT_REL")"
        ADJUDICATION_FAILED=1
      fi
    elif [ "$DEFERRED_COUNT" -gt 0 ]; then
      FINAL_ADJUDICATION="deferred_evidence_gaps"
    else
      FINAL_ADJUDICATION="no_triggers"
    fi
    ;;
  1)
    if [ "$TRIGGER_COUNT" -gt 0 ]; then
      FINAL_ADJUDICATION="running"
      FINAL_DETAIL="critic quorum met; forced targeted adjudication running"
      write_status running 0 "" || exit 1
      if execute_targeted_adjudication; then
        FINAL_ADJUDICATION="complete"
      else
        FINAL_ADJUDICATION="failed"
        FINAL_DETAIL="forced targeted adjudication failed"
        integrity_summary=$(snapshot_integrity_summary)
        [ -z "$integrity_summary" ] || FINAL_DETAIL="$FINAL_DETAIL; target snapshot integrity check failed ($integrity_summary); $(snapshot_fault_remedy "$TARGET_SNAPSHOT_REL")"
        ADJUDICATION_FAILED=1
      fi
    else
      FINAL_ADJUDICATION="deferred_evidence_gaps"
    fi
    ;;
esac

quorum_at=$(utc_now)
"$HELPER" round-transition --workspace "$WS" --round "$ROUND" \
  --transition quorum_complete --roles "$NAMES" --at "$quorum_at" >/dev/null \
  || { FINAL_DETAIL="could not journal round quorum completion"; exit 1; }

PROJECTION_META="$INV/projection.json"
if [ "$FINAL_ADJUDICATION" = complete ]; then
  "$HELPER" projection --out "$OUT" --results "$INV/results" --invocation "$INVOCATION_ID" \
    --input-sha "$INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" --round "$ROUND" \
    --roles "$NAMES" --adjudication "$FINAL_ADJUDICATION" --routing "$ROUTING" --packet "$PACKET" \
    --target-input-sha "$TARGET_INPUT_SHA" > "$PROJECTION_META" 2> "$INV/projection.stderr"
else
  "$HELPER" projection --out "$OUT" --results "$INV/results" --invocation "$INVOCATION_ID" \
    --input-sha "$INPUT_SHA" --report-sha "$REPORT_SHA" --audit-id "$AUDIT_ID" --round "$ROUND" \
    --roles "$NAMES" --adjudication "$FINAL_ADJUDICATION" --routing "$ROUTING" --packet "$PACKET" \
    > "$PROJECTION_META" 2> "$INV/projection.stderr"
fi
projection_rc=$?
cat "$INV/projection.stderr" >&2 2>/dev/null
if [ "$projection_rc" -eq "$SNAPSHOT_INTEGRITY_RC" ]; then
  # A round and a target snapshot are both in play here: name the one the helper refused.
  faulted_snapshot=$(snapshot_named_in "$INV/projection.stderr")
  case "$faulted_snapshot" in
    */target_snapshots/*) faulted_label="target snapshot $faulted_snapshot" ;;
    ?*) faulted_label="round snapshot $faulted_snapshot" ;;
    *) faulted_label="a frozen snapshot (round $SNAPSHOT_REL${TARGET_SNAPSHOT_REL:+ or target $TARGET_SNAPSHOT_REL})" ;;
  esac
  if snapshot_mode_unstable_in "$INV/projection.stderr"; then
    FINAL_DETAIL="immutable round projection failed: $faulted_label: $SNAPSHOT_MODE_UNSTABLE_REMEDY"
  else
    FINAL_DETAIL="immutable round projection failed: $faulted_label failed its integrity check; remedy: $(snapshot_remedy "${faulted_snapshot:-$SNAPSHOT_REL}")"
  fi
  echo "FATAL: $FINAL_DETAIL" >&2
  exit 1
elif [ "$projection_rc" -ne 0 ]; then
  FINAL_DETAIL="immutable round projection failed"; exit 1
fi
PROJECTION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["projection"])' "$PROJECTION_META")
[ -d "$PROJECTION" ] || { FINAL_DETAIL="round projection metadata is incomplete"; exit 1; }

if [ "$ADJUDICATION_FAILED" -eq 1 ] && [ -n "$PRIOR_CURRENT_INVOCATION" ] && round_is_finished; then
  # A finished round keeps the projection it finished with: republishing here would retire its
  # CRITIQUE.* (this run's adjudication failed) and repoint CURRENT_PROJECTION, leaving a round that no
  # longer validates. This run's critic results stay under its own invocation and projection.
  FINAL_OUTCOME="quorum_complete_adjudication_failed"
  echo "=== Round $ROUND: this rerun's targeted adjudication FAILED; the finished round is left as it was ===" >&2
  if [ "$FORCE_RERUN" -eq 0 ] && [ -f "$OUT/adjudication_cache/$TARGET_INPUT_SHA.conflict" ]; then
    FINAL_DETAIL="finished round left as it was; a plain rerun cannot reuse the pre-force adjudication; retry with I4R_FORCE_RERUN=1"
    echo "    retry the adjudication with I4R_FORCE_RERUN=1 (see above)" >&2
  else
    echo "    rerun this round to retry adjudication only (once its $MAX_TRIES adjudicator attempts are used, add MAX_TRIES=<higher>)" >&2
  fi
  exit 1
fi
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

if [ "$ADJUDICATION_FAILED" -eq 1 ]; then
  # The critic reviews are journaled and projected above, so none of that work is lost. The round
  # is deliberately NOT journaled as integrated: adjudication is part of integration, and a rerun
  # replays the cached critics and retries only the adjudicator.
  FINAL_OUTCOME="quorum_complete_adjudication_failed"
  echo "=== Round $ROUND: critic quorum complete and projected; targeted adjudication FAILED ===" >&2
  if [ "$FORCE_RERUN" -eq 0 ] && [ -f "$OUT/adjudication_cache/${TARGET_INPUT_SHA:-none}.conflict" ]; then
    # the refusal above: a plain rerun cannot reuse a pre-force adjudication
    FINAL_DETAIL="a plain rerun cannot reuse the pre-force adjudication; retry with I4R_FORCE_RERUN=1"
    echo "    the critic reviews are published; retry the adjudication with I4R_FORCE_RERUN=1 (see above)" >&2
  else
    echo "    the critic reviews are published; rerun this round to retry adjudication only" >&2
    echo "    (once its $MAX_TRIES adjudicator attempts are used, add MAX_TRIES=<higher> to the same command)" >&2
  fi
  exit 1
fi

integrated_at=$(utc_now)
"$HELPER" round-transition --workspace "$WS" --round "$ROUND" \
  --transition integrated --roles "$NAMES" --at "$integrated_at" --report-sha256 "$REPORT_SHA" >/dev/null \
  || { FINAL_DETAIL="could not journal round integration"; exit 1; }

FINAL_OUTCOME="complete"
FINAL_DETAIL="exact role quorum met; routine synthesis omitted; adjudication=$FINAL_ADJUDICATION"
echo "=== Round $ROUND complete: roles=$NAMES; adjudication=$FINAL_ADJUDICATION ==="
exit 0
