#!/bin/bash
# verify_codex_model.sh <expected-model> <since-epoch> [audit-slug] [until-epoch] [expected-effort]
#
# Also reports the reasoning effort recorded per session. NOTE the asymmetry: the recorded MODEL is
# what actually served the session, but the recorded EFFORT is merely what was requested — the CLI
# writes the string through regardless of validity. An invalid effort is rejected by the API at
# request time, so a session that produced output at all did not silently downgrade its effort.
#
# Confirms every Codex session in the window [since-epoch, until-epoch] actually ran on
# <expected-model>. Pass until-epoch — the round's end stamp — whenever one is available: an
# open-ended window sweeps in every later session on the machine and reports them as mismatches,
# which is a false alarm rather than a downgrade.
#
# WHY THIS EXISTS. A ChatGPT plan that has hit its quota can silently serve a different, cheaper model
# instead of refusing the request. The Codex CLI's stdout — which is all run_critique.sh captures —
# does NOT record which model served the session, so a downgrade mid-round is invisible in the audit
# record. The consequence is not a slow round; it is a round whose findings get attributed to a model
# that never produced them. The session rollouts under ~/.codex/sessions DO record the model, so this
# script reads those instead of trusting the logs.
#
# Exits non-zero if any session used something other than the expected model, so a caller can treat a
# silent downgrade as a hard failure rather than a footnote.
set -uo pipefail
EXPECTED="${1:?usage: verify_codex_model.sh <expected-model> <since-epoch> [audit-slug]}"
SINCE="${2:?since-epoch (seconds); e.g. the value stamped at round start}"
SLUG="${3:-}"
UNTIL="${4:-0}"
EXP_EFFORT="${5:-}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$EXPECTED" "$SINCE" "$SLUG" "$ROOT" "$UNTIL" "$EXP_EFFORT" <<'PY'
import json, pathlib, sys, datetime, re
expected, since, slug, root = sys.argv[1], int(sys.argv[2]), sys.argv[3], pathlib.Path(sys.argv[4])
until = int(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] not in ("", "0") else None
exp_effort = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] else None
base = pathlib.Path.home() / ".codex" / "sessions"
if not base.exists():
    print(f"WARN: {base} not found — cannot verify the serving model. Treat model attribution as UNVERIFIED.")
    raise SystemExit(0)

KNOWN = ("gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-lune", "luna", "terra", "o3")
rows, bad = [], []
for f in sorted(base.rglob("*.jsonl")):
    mt = f.stat().st_mtime
    if mt < since or (until and mt > until):
        continue
    model, turns, effort = None, 0, None
    for line in f.open(errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if model is None:
            s = json.dumps(d)
            for k in KNOWN:
                if f'"{k}"' in s:
                    model = k
                    break
        if effort is None:
            m = re.search(r'reasoning_effort"\s*:\s*"([a-z]+)"', json.dumps(d), re.I)
            if m:
                effort = m.group(1)
        if d.get("type") in ("response_item", "event_msg", "turn_context"):
            turns += 1
    # A rejected request never gets a model recorded; it also did no work, so it is not a downgrade.
    if model is None and turns <= 8:
        continue
    rows.append((f.name[8:27], model or "UNRECORDED", turns, effort or "-"))
    if model != expected:
        bad.append((f.name, model or "UNRECORDED", turns))

win = f"{datetime.datetime.fromtimestamp(since):%Y-%m-%d %H:%M}" + (f" to {datetime.datetime.fromtimestamp(until):%H:%M}" if until else " onward (UNBOUNDED — pass until-epoch to avoid false alarms)")
print(f"expected model: {expected}   window: {win}")
for started, model, turns, effort in rows:
    flag = "  " if model == expected else "<-- MISMATCH"
    eflag = "" if (not exp_effort or effort == exp_effort) else f"  <-- effort requested {effort!r}, expected {exp_effort!r}"
    print(f"  {started}  {model:16s} {turns:5d} turns  effort={effort:8s}{flag}{eflag}")
total = sum(r[2] for r in rows)
print(f"  {len(rows)} session(s), {total} model turns total")

if slug:
    log = root / "audits" / "runs.jsonl"
    if log.parent.exists():
        import time
        with log.open("a") as fh:
            fh.write(json.dumps({
                "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "epoch": int(time.time()), "event": "note", "audit": slug,
                "key": "codex_model_verified",
                "value": f"{len(rows)} sessions, {total} turns, all {expected}" if not bad
                         else f"MISMATCH: {bad}",
            }) + "\n")

if bad:
    print(f"\nFAIL: {len(bad)} session(s) did not run on {expected}:")
    for name, model, turns in bad:
        print(f"   {name}: {model} ({turns} turns)")
    print("A silent plan downgrade invalidates model attribution for this round. Re-run the affected")
    print("critics once quota is available, or record the actual model in the audit record.")
    raise SystemExit(1)
print(f"\nOK: every session ran on {expected} — no silent downgrade.")
PY
