#!/bin/bash
# run_meta.sh report|check <audit-dir>
# Derive model, latency, cache, outcome, and exact token totals from invocations.jsonl.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
CMD="${1:?usage: run_meta.sh report|check <audit-dir>}"
AUDIT="${2:?usage: run_meta.sh report|check <audit-dir>}"
case "$CMD" in
  report) exec python3 "$HERE/summarize_invocations.py" "$AUDIT" ;;
  check)  exec python3 "$HERE/summarize_invocations.py" "$AUDIT" --check ;;
  *) echo "FATAL: unknown command '$CMD' (use report or check)" >&2; exit 2 ;;
esac
