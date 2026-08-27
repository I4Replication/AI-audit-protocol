#!/bin/bash
# wait_for_round.sh <audit-dir> <round> [--pid N] [--timeout-min M] [--selftest]
#
# Blocks until a critique round is genuinely finished, then reports what happened and exits
# non-zero if the round failed. Use this instead of hand-rolling a wait.
#
# WHY THIS EXISTS — the failure it replaces, which cost 22 hours.
#
# run_critique.sh announces its outcome in prose ("=== Round 2: synthesis SKIPPED ==="). In one
# audit the coordinator backgrounded a waiter that polled the driver log for the
# string "synthesis skipped". The log says "SKIPPED". The grep never matched. Round 2 had finished
# in 17 minutes; the waiter kept polling a job that no longer existed for a further 22 hours, which
# was roughly 90% of that audit's total elapsed time. Nothing crashed, nothing was lost, and every
# artefact was correct and sitting on disk — the loop simply had no way to learn that.
#
# Three rules follow, and this script exists to make all three automatic:
#
#   1. WAIT ON THE PROCESS, NOT ON ITS OUTPUT. A process either exists or it does not. Log text is
#      a human artefact that gets reworded, recased, buffered, or never printed at all on the path
#      that actually happened.
#   2. NEVER POLL WITHOUT A DEADLINE. An unbounded wait converts "I misread the signal" into "the
#      session is gone". The timeout is what turned a 22-hour stall into a 40-minute one.
#   3. VERIFY THE OUTCOME, DO NOT INFER IT. Process exit means the round STOPPED, not that it
#      SUCCEEDED. run_critique.sh writes critique/round_N/ROUND_STATUS on every exit path; this
#      script reads it. Its absence after the process is gone is itself a finding — the driver died
#      before it could name its own output directory.
#
# The general form of the bug is worth stating, because it recurs in every audit that watches a long
# job: a detector whose silence is indistinguishable from "not yet". Before waiting on anything, ask
# what this waiter prints if the job crashed one second from now. If the answer is "nothing", the
# waiter is wrong however careful its pattern is.
set -uo pipefail

if [ "${1:-}" = "--selftest" ]; then
  T=$(mktemp -d); bad=0
  mkdir -p "$T/critique/round_7"
  # (a) a completed, successful round is reported and accepted
  printf 'round=7\nexit=0\noutcome=complete\ncritic_reports=2\nsynthesis=skipped\n' \
    > "$T/critique/round_7/ROUND_STATUS"
  out=$("$0" "$T" 7 --timeout-min 1 2>&1); rc=$?
  [ $rc -eq 0 ] || { echo "FAIL: completed round reported rc=$rc"; bad=1; }
  echo "$out" | grep -q "synthesis=skipped" || { echo "FAIL: status not echoed"; bad=1; }
  # (b) THE ORIGINAL BUG: lowercase/uppercase must not matter, because nothing greps prose
  printf 'round=7\nexit=0\noutcome=complete\ncritic_reports=2\nsynthesis=SKIPPED\n' \
    > "$T/critique/round_7/ROUND_STATUS"
  "$0" "$T" 7 --timeout-min 1 >/dev/null 2>&1 \
    || { echo "FAIL: case of a status VALUE changed the verdict"; bad=1; }
  # (c) a failed round must exit non-zero, not be mistaken for success
  printf 'round=7\nexit=1\noutcome=failed\ncritic_reports=0\nsynthesis=not_reached\n' \
    > "$T/critique/round_7/ROUND_STATUS"
  "$0" "$T" 7 --timeout-min 1 >/dev/null 2>&1
  [ $? -ne 0 ] || { echo "FAIL: a failed round was reported as success"; bad=1; }
  # (d) a vanished driver that wrote no status must be reported, never waited on forever
  rm -f "$T/critique/round_7/ROUND_STATUS"
  out=$("$0" "$T" 7 --timeout-min 1 2>&1); rc=$?
  [ $rc -ne 0 ] || { echo "FAIL: missing ROUND_STATUS treated as success"; bad=1; }
  echo "$out" | grep -q "no ROUND_STATUS" || { echo "FAIL: missing status not explained"; bad=1; }
  # (d2) a pre-trap round (reports present, no status) is NOT reported as a crash
  echo x > "$T/critique/round_7/critic_stats.md"
  out=$("$0" "$T" 7 --timeout-min 1 2>&1); rc=$?
  [ $rc -eq 5 ] || { echo "FAIL: legacy round not distinguished (rc=$rc)"; bad=1; }
  echo "$out" | grep -q "predates" || { echo "FAIL: legacy round not explained"; bad=1; }
  rm -f "$T/critique/round_7/critic_stats.md"

  # (e) the deadline must fire rather than poll forever
  sleep 120 & sp=$!; disown 2>/dev/null || true
  start=$(date +%s); "$0" "$T" 7 --pid $sp --timeout-min 0 >/dev/null 2>&1; rc=$?
  el=$(( $(date +%s) - start )); kill $sp 2>/dev/null
  [ $rc -eq 3 ] || { echo "FAIL: timeout did not return rc=3 (got $rc)"; bad=1; }
  [ $el -lt 30 ] || { echo "FAIL: timeout took ${el}s to fire"; bad=1; }
  rm -rf "$T"
  [ $bad -eq 0 ] && echo "  ok: wait_for_round self-test (success, case-independence, failure, missing status, deadline)"
  exit $bad
fi

DIR="${1:?usage: wait_for_round.sh <audit-dir> <round> [--pid N] [--timeout-min M]}"
ROUND="${2:?round number}"
shift 2
PID=""; TIMEOUT_MIN=90
while [ $# -gt 0 ]; do
  case "$1" in
    --pid) PID="$2"; shift 2 ;;
    --timeout-min) TIMEOUT_MIN="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

STATUS="$DIR/critique/round_${ROUND}/ROUND_STATUS"
deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 ))

alive() {
  if [ -n "$PID" ]; then kill -0 "$PID" 2>/dev/null
  else pgrep -f "run_critique.sh $ROUND" >/dev/null 2>&1
  fi
}

while alive; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "TIMEOUT: round $ROUND still running after ${TIMEOUT_MIN} min." >&2
    echo "  The driver is alive; this waiter is not going to keep polling silently." >&2
    echo "  Check: tail critique/round_${ROUND}/log_*try*.txt — a round is normally 15-75 min." >&2
    exit 3
  fi
  sleep 15
done

if [ ! -f "$STATUS" ]; then
  # Discriminate two very different causes of a missing status file. A round that produced critic
  # reports but no ROUND_STATUS almost certainly predates the EXIT trap (added 2026-08-27) rather
  # than having died: several audits were already in flight in the shared tree when it landed, and
  # reporting those as crashes would be a false alarm of exactly the kind this script exists to
  # prevent.
  _n=$(ls "$DIR/critique/round_${ROUND}"/critic_*.md 2>/dev/null | wc -l | tr -d " ")
  if [ "$_n" -gt 0 ]; then
    echo "round $ROUND is not running and wrote no ROUND_STATUS, but $_n critic report(s) exist." >&2
    echo "  This round most likely predates the ROUND_STATUS contract (added 2026-08-27) rather" >&2
    echo "  than having failed. Read the reports and judge them directly; rounds started after" >&2
    echo "  that date always write the file." >&2
    exit 5
  fi
  echo "ABNORMAL: round $ROUND is no longer running and wrote no ROUND_STATUS." >&2
  echo "  run_critique.sh writes it from an EXIT trap, so its absence means the driver died before" >&2
  echo "  it resolved its output directory — a bad AUDIT_DIR, a rejected critic model, or an" >&2
  echo "  out-of-credits workspace (all of which exit before that point). Read its stdout." >&2
  exit 4
fi

echo "round $ROUND finished:"
sed 's/^/  /' "$STATUS"
outcome=$(sed -n 's/^outcome=//p' "$STATUS")
[ "$outcome" = "complete" ] || { echo "round $ROUND did NOT complete cleanly." >&2; exit 1; }
