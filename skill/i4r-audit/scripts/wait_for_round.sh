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
TIMEOUT_MIN=90
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
    if obj["outcome"] not in ("running", "complete", "failed"):
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
      *) INVOCATION="$candidate" ;;
    esac
  fi

  if [ -n "$INVOCATION" ]; then
    STATUS="$OUT/invocations/$INVOCATION/ROUND_STATUS.json"
    if [ -s "$STATUS" ]; then
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
        exit 1
      fi
      watch_pid="$status_pid"
    else
      watch_pid="$PID"
    fi
  else
    watch_pid="$PID"
  fi

  if [ -n "$watch_pid" ] && is_alive "$watch_pid"; then
    dead_observations=0
  elif [ -n "$watch_pid" ]; then
    dead_observations=$((dead_observations + 1))
    if [ "$dead_observations" -ge 2 ]; then
      echo "ABNORMAL: invocation ${INVOCATION:-unknown} is no longer running and has no terminal status." >&2
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
