#!/bin/bash
# Wait for one exact run_critique invocation and verify its machine-readable terminal state.
# Never searches the process table by command text and never infers success from prose logs.
set -uo pipefail

usage() {
  echo "usage: wait_for_round.sh <audit-dir> <round> [--pid N] [--invocation ID] [--timeout-min M] [--poll-seconds S]" >&2
  exit 2
}

DIR="${1:-}"
ROUND="${2:-}"
[ -n "$DIR" ] && [ -n "$ROUND" ] || usage
shift 2

PID=""
INVOCATION=""
FROM_POINTER=0
TIMEOUT_MIN=""
POLL_SECONDS="${I4R_WAIT_POLL_SECONDS:-2}"
while [ $# -gt 0 ]; do
  case "$1" in
    --pid) [ $# -ge 2 ] || usage; PID="$2"; shift 2 ;;
    --invocation) [ $# -ge 2 ] || usage; INVOCATION="$2"; shift 2 ;;
    --timeout-min) [ $# -ge 2 ] || usage; TIMEOUT_MIN="$2"; shift 2 ;;
    --poll-seconds) [ $# -ge 2 ] || usage; POLL_SECONDS="$2"; shift 2 ;;
    *) usage ;;
  esac
done

case "$ROUND" in ''|*[!0-9]*) usage ;; esac
[ "$ROUND" -ge 1 ] || usage
# 2.7.4: by default, twice this audit's per-call critique ceiling (scripts/critique_timeouts.py): 90 minutes for an
# audit initialised before 2.7.4 (2,700 s, the old fixed default), 180 for a new one (5,400 s).
if [ -z "$TIMEOUT_MIN" ]; then
  # the effective ceiling, as run_critique.sh resolves it (an older audit's I4R_PROVIDER_TIMEOUT_SECONDS included)
  ceiling=$(python3 "$(dirname "$0")/critique_timeouts.py" "$DIR/pipeline-policy.snapshot.json" "${I4R_PROVIDER_TIMEOUT_SECONDS:-}" 2>/dev/null) || ceiling=2700
  case "$ceiling" in ''|*[!0-9]*) ceiling=2700 ;; esac
  TIMEOUT_MIN=$(( 2 * ceiling / 60 ))
fi
case "$TIMEOUT_MIN" in ''|*[!0-9]*) usage ;; esac
case "$POLL_SECONDS" in ''|*[!0-9]*) usage ;; esac
[ "$POLL_SECONDS" -ge 1 ] || usage
if [ -n "$PID" ]; then
  case "$PID" in ''|*[!0-9]*) usage ;; esac
  [ "$PID" -ge 1 ] || usage
fi
if [ -n "$INVOCATION" ]; then
  case "$INVOCATION" in *[!A-Za-z0-9._-]*) usage ;; esac
fi

OUT="$DIR/critique/round_${ROUND}"
# a role provider of the watched invocation is still running (it outlives a driver killed with -9)
provider_alive() {
  local provider_file provider_pid
  [ -n "${INVOCATION:-}" ] || return 1
  for provider_file in "$OUT/invocations/$INVOCATION/active_providers"/*.pid; do
    [ -f "$provider_file" ] || continue
    provider_pid=$(sed -n '1p' "$provider_file" 2>/dev/null || true)
    case "$provider_pid" in ''|*[!0-9]*) continue ;; esac
    # a reused pid is not our provider: count it only while it is still a codex process
    kill -0 "$provider_pid" 2>/dev/null && ps -o command= -p "$provider_pid" 2>/dev/null | grep -q codex && return 0
  done
  return 1
}
# the command that resumes this round with the roles (and diff manifest) its invocation was started with
rerun_hint() {
  local roles="" manifest="" line
  # the newest invocation that dispatched (a refused run can leave the pointer on one that never started)
  roles=$(python3 -c 'import json, os, sys
base = os.path.join(sys.argv[1], "invocations")
try:
    names = sorted(os.listdir(base), reverse=True)
except OSError:
    names = []
for name in names:
    attempts = os.path.join(base, name, "attempts")
    if os.path.isdir(attempts) and os.listdir(attempts):
        try:
            routing = json.load(open(os.path.join(base, name, "ROUTING.json")))
            # only a run started WITH a CRITICS override needs it again; adding one to a packet-routed run changes
            # its routing binding (a new plan, and the finished critics re-run)
            if "CRITICS override" in str(routing.get("reason", "")):
                print(" ".join(routing["roles"]))
        except Exception:
            pass
        break' "$OUT" 2>/dev/null || true)
  [ -f "$DIR/critique/DIFF_MANIFEST_round_${ROUND}.json" ] && manifest="$DIR/critique/DIFF_MANIFEST_round_${ROUND}.json"
  line=""
  [ -n "$roles" ] && [ "$ROUND" -ge 2 ] && line="$line$(printf 'CRITICS=%q ' "$roles")"
  [ -n "$manifest" ] && line="$line$(printf 'DIFF_MANIFEST=%q ' "$manifest")"
  line="$line$(printf 'AUDIT_DIR=%q bash %q %s' "$DIR" "$(cd "$(dirname "$0")" && pwd)/run_critique.sh" "$ROUND")"
  echo "  re-run it with the settings it was started with (finished critics come from cache): $line" >&2
  echo "  (plus the same I4R_AUTO_AUTHORIZE / ADJUDICATE values, e.g. I4R_AUTO_AUTHORIZE=0 for an attended run)" >&2
}
POINTER="$OUT/CURRENT_INVOCATION"
deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 ))
dead_observations=0

is_alive() {
  check_pid="$1"
  [ -n "$check_pid" ] && kill -0 "$check_pid" 2>/dev/null
}

read_status() {
  status_file="$1"
  python3 - "$status_file" "$INVOCATION" "$ROUND" <<'PY'
import json, pathlib, sys
path, invocation, round_text = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
try:
    obj = json.loads(path.read_text())
    required = {
        "status_version", "invocation_id", "round", "pid", "outcome", "exit_code",
        "expected_roles", "completed_roles", "quorum_met", "adjudication",
    }
    if not required.issubset(obj):
        raise ValueError("required fields missing")
    if obj["status_version"] != "i4r-round-status-2":
        raise ValueError("unsupported status version")
    if obj["invocation_id"] != invocation or obj["round"] != int(round_text):
        raise ValueError("status identity mismatch")
    # The writer's full enum (test_run_critique_runtime.py `status --outcome` choices). The waiter
    # used to know three of the five and reported the other two -- including the one state the
    # driver went to trouble to make distinct, "all critics succeeded, only the adjudicator
    # failed" -- as a malformed or foreign status file.
    if obj["outcome"] not in ("running", "paused", "complete", "failed",
                              "quorum_complete_adjudication_failed"):
        raise ValueError("invalid outcome")
    if not isinstance(obj["pid"], int) or obj["pid"] < 1:
        raise ValueError("invalid pid")
    if not isinstance(obj["quorum_met"], bool):
        raise ValueError("invalid quorum flag")
    if not isinstance(obj["expected_roles"], list) or not isinstance(obj["completed_roles"], list):
        raise ValueError("invalid role lists")
    allowed = {"stats", "data", "fairness", "completeness"}
    expected = set(obj["expected_roles"])
    completed = set(obj["completed_roles"])
    if not expected or not expected <= allowed or not completed <= allowed:
        raise ValueError("status contains an empty or unknown role set")
    if int(round_text) == 1 and expected != allowed:
        raise ValueError("round 1 does not name the exact four-role quorum")
    if int(round_text) >= 2 and (
        "completeness" not in expected or not expected & {"stats", "data", "fairness"}
    ):
        raise ValueError("later round lacks completeness plus a specialist")
    if len(obj["expected_roles"]) != len(set(obj["expected_roles"])):
        raise ValueError("duplicate expected role")
    if len(obj["completed_roles"]) != len(set(obj["completed_roles"])):
        raise ValueError("duplicate completed role")
    if obj["quorum_met"] != (expected == completed):
        raise ValueError("quorum flag disagrees with exact role sets")
    if obj["outcome"] == "running" and obj["exit_code"] is not None:
        raise ValueError("running status must have null exit_code")
    if obj["outcome"] != "running" and (
        not isinstance(obj["exit_code"], int) or isinstance(obj["exit_code"], bool)
    ):
        raise ValueError("terminal status must have an integer exit_code")
except Exception as exc:
    print(f"invalid ROUND_STATUS.json: {exc}", file=sys.stderr)
    raise SystemExit(1)
print(obj["outcome"])
print(obj["pid"])
print("true" if obj["quorum_met"] else "false")
print("" if obj["exit_code"] is None else obj["exit_code"])
print(obj.get("detail") or "")
PY
}

while :; do
  if [ -z "$INVOCATION" ] && [ -s "$POINTER" ]; then
    candidate=$(sed -n '1p' "$POINTER" 2>/dev/null || true)
    case "$candidate" in
      ''|*[!A-Za-z0-9._-]*)
        echo "ABNORMAL: invalid invocation id in $POINTER" >&2
        exit 4
        ;;
      *) INVOCATION="$candidate"; FROM_POINTER=1 ;;
    esac
  fi

  if [ -n "$INVOCATION" ]; then
    STATUS="$OUT/invocations/$INVOCATION/ROUND_STATUS.json"
    if [ -s "$STATUS" ] && ! status_info=$(read_status "$STATUS"); then
      echo "ABNORMAL: status for invocation $INVOCATION is malformed or belongs to another run." >&2
      exit 4
    fi
    status_pid=$( [ -s "$STATUS" ] && printf '%s\n' "$status_info" | sed -n '2p' )
    if [ -s "$STATUS" ] && [ -n "$PID" ] && [ "$status_pid" != "$PID" ] && [ "$FROM_POINTER" = 1 ] &&
        is_alive "$PID"; then
      # The pointer still names an earlier invocation -- the paused prepare call, or the failed run
      # being retried -- because the run started with --pid has not published its own yet. Re-read.
      INVOCATION=""
      FROM_POINTER=0
      watch_pid="$PID"
    elif [ -s "$STATUS" ]; then
      if ! status_info=$(read_status "$STATUS"); then
        echo "ABNORMAL: status for invocation $INVOCATION is malformed or belongs to another run." >&2
        exit 4
      fi
      outcome=$(printf '%s\n' "$status_info" | sed -n '1p')
      status_pid=$(printf '%s\n' "$status_info" | sed -n '2p')
      quorum=$(printf '%s\n' "$status_info" | sed -n '3p')
      status_exit=$(printf '%s\n' "$status_info" | sed -n '4p')
      detail=$(printf '%s\n' "$status_info" | sed -n '5p')
      if [ -n "$PID" ] && [ "$status_pid" != "$PID" ]; then
        if [ "$FROM_POINTER" = 1 ] && ! is_alive "$PID"; then
          # The run started with --pid has exited, and the pointer names another invocation: a refused
          # rerun of a finished round puts the finished round's pointer back. Report that run's own status.
          own=$(python3 - "$OUT" "$PID" "$STATUS" <<'PY'
import json, pathlib, sys
# Only a status written after the pointer's (restored with cp -p, so it keeps its time): an older
# invocation that happened to have the same, recycled pid is not this run.
floor = pathlib.Path(sys.argv[3]).stat().st_mtime
matches = []
for path in pathlib.Path(sys.argv[1]).glob("invocations/*/ROUND_STATUS.json"):
    if path.stat().st_mtime <= floor:
        continue
    try:
        if str(json.loads(path.read_text(encoding="utf-8")).get("pid")) == sys.argv[2]:
            matches.append((path.stat().st_mtime, path.parent.name))
    except (OSError, ValueError):
        continue
print(max(matches)[1] if matches else "")
PY
)
          if [ -n "$own" ] && [ "$own" != "$INVOCATION" ]; then
            echo "note: pid $PID ran invocation $own; $POINTER still names $INVOCATION (left unchanged)." >&2
            INVOCATION="$own"
            FROM_POINTER=0
            continue
          fi
        fi
        if ! is_alive "$PID"; then
          # the run started with --pid is gone and wrote no status of its own: it refused before publishing one
          echo "ABNORMAL: pid $PID exited without publishing an invocation for round $ROUND (a refusal, not a failed round);" >&2
          echo "  its log's FATAL line says why. $POINTER still names the earlier invocation $INVOCATION." >&2
          exit 4
        fi
        echo "ABNORMAL: requested pid $PID but invocation $INVOCATION belongs to pid $status_pid." >&2
        exit 4
      fi
      if [ "$outcome" = complete ]; then
        echo "round $ROUND invocation $INVOCATION finished:"
        python3 -m json.tool "$STATUS" | sed 's/^/  /'
        if [ "$quorum" = true ] && { [ -z "$status_exit" ] || [ "$status_exit" -eq 0 ]; }; then
          exit 0
        fi
        echo "round $ROUND stopped without the exact role quorum." >&2
        exit 1
      fi
      if [ "$outcome" = failed ]; then
        echo "round $ROUND invocation $INVOCATION failed${detail:+: $detail}" >&2
        python3 -m json.tool "$STATUS" | sed 's/^/  /' >&2
        case "$detail" in *interrupted*) rerun_hint ;; esac
        exit 1
      fi
      if [ "$outcome" = quorum_complete_adjudication_failed ]; then
        case "$detail" in
          *I4R_FORCE_RERUN*)
            echo "round $ROUND invocation $INVOCATION: the targeted adjudication conflicts with a forced rerun's" >&2
            case "$detail" in
              "finished round left as it was"*) echo "  (${detail}); nothing was republished." >&2 ;;
              *) echo "  (${detail}); the critic quorum is projected, but the round has no valid adjudication." >&2 ;;
            esac
            echo "  Retry with I4R_FORCE_RERUN=1 -- a plain rerun cannot reuse the pre-force adjudication." >&2 ;;
          *)
            echo "round $ROUND invocation $INVOCATION: the critic quorum is complete, journaled and" >&2
            echo "  published; only the targeted adjudicator failed${detail:+: $detail}" >&2
            echo "  Retry the adjudicator alone -- the critics replay from cache and cost nothing (once the round's" >&2
            echo "  adjudicator attempts are used up, add MAX_TRIES=<higher>):" >&2
            rerun_hint ;;
        esac
        python3 -m json.tool "$STATUS" | sed 's/^/  /' >&2
        exit 1
      fi
      if [ "$outcome" = paused ]; then
        echo "PAUSED: round $ROUND invocation $INVOCATION is waiting for authorization${detail:+: $detail}" >&2
        echo "  Review the round plan it names, then re-run run_critique.sh $ROUND to continue." >&2
        exit 3
      fi
      watch_pid="$status_pid"
    else
      # No status yet. If the id came from the pointer and --pid is alive, the pointer may still name
      # a run killed before its first status write; re-read it on every poll until a status appears.
      if [ "$FROM_POINTER" = 1 ] && [ -n "$PID" ] && is_alive "$PID"; then
        INVOCATION=""
        FROM_POINTER=0
      fi
      watch_pid="$PID"
    fi
  else
    watch_pid="$PID"
  fi

  if [ -n "$watch_pid" ] && is_alive "$watch_pid"; then
    dead_observations=0
  elif [ -n "$watch_pid" ] && provider_alive; then
    # the driver is gone (killed with -9) but its role provider is still running: the round is not dead yet
    dead_observations=0
  elif [ -n "$watch_pid" ]; then
    dead_observations=$((dead_observations + 1))
    if [ "$dead_observations" -ge 2 ]; then
      echo "ABNORMAL: invocation ${INVOCATION:-unknown} is no longer running and has no terminal status." >&2
      if [ -n "${INVOCATION:-}" ]; then
        echo "  the round started but did not finish;" >&2
        rerun_hint
      else
        echo "  the driver exited before publishing an invocation (a refusal, not a failed round): its log's FATAL line says why" >&2
      fi
      exit 4
    fi
  elif [ -z "$INVOCATION" ]; then
    echo "ABNORMAL: no CURRENT_INVOCATION exists and no --pid was supplied for round $ROUND." >&2
    exit 4
  elif [ -z "$watch_pid" ]; then
    echo "ABNORMAL: invocation $INVOCATION has no status and no --pid was supplied." >&2
    exit 4
  fi

  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "TIMEOUT: round $ROUND invocation ${INVOCATION:-not-yet-published} did not finish within ${TIMEOUT_MIN} min." >&2
    echo "  The driver was not killed; inspect $OUT and the invocation-scoped status." >&2
    exit 3
  fi
  sleep "$POLL_SECONDS"
done
