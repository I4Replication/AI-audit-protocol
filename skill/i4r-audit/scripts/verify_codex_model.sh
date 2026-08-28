#!/bin/bash
# verify_codex_model.sh <audit-dir> [role|all] [exact-observed-model] [exact-effort]
# Verify canonical, invocation-scoped model identity rather than sweeping global session files.
set -uo pipefail
AUDIT="${1:?usage: verify_codex_model.sh <audit-dir> [role|all] [exact-observed-model] [exact-effort]}"
ROLE="${2:-all}"
EXPECTED="${3:-}"
EXPECTED_EFFORT="${4:-}"
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
python3 - "$AUDIT" "$ROLE" "$EXPECTED" "$EXPECTED_EFFORT" "$HERE" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1]).resolve(); role=sys.argv[2]; expected=sys.argv[3]
expected_effort=sys.argv[4]
sys.path.insert(0,sys.argv[5]); import record_invocation
ledger=root/"invocations.jsonl"
if not ledger.is_file(): raise SystemExit(f"FAIL: canonical ledger missing: {ledger}")
rows=[]
for number,line in enumerate(ledger.read_text().splitlines(),1):
    if not line.strip(): continue
    try:
        row=json.loads(line); record_invocation.validate_record(row,root)
    except Exception as exc: raise SystemExit(f"FAIL: invocation line {number}: {exc}")
    if row["provider_call"] and row["status"]=="completed" and (role=="all" or row["role"]==role):
        rows.append(row)
if not rows: raise SystemExit(f"FAIL: no completed provider calls match role={role!r}")
bad=[]
for row in rows:
    problems=[]
    if row["model_source"]!="observed": problems.append("model_source is not observed")
    if not row["observed_model"]: problems.append("observed model ID missing")
    if expected and row["observed_model"]!=expected:
        problems.append(f"observed {row['observed_model']!r}, expected exact {expected!r}")
    if expected_effort and row["reasoning_effort"]!=expected_effort:
        problems.append(
            f"effort {row['reasoning_effort']!r}, expected exact {expected_effort!r}"
        )
    if problems: bad.append((row["invocation_id"],problems))
    print(f"{row['invocation_id']}  role={row['role']}  requested={row['requested_model']}  "
          f"served={row['observed_model']}  effort={row['reasoning_effort']}  session={row['session_id'] or '-'}")
if bad:
    for invocation,problems in bad:
        print(f"FAIL: {invocation}: {'; '.join(problems)}",file=sys.stderr)
    raise SystemExit(1)
print(f"OK: {len(rows)} completed call(s) have exact observed model telemetry")
PY
