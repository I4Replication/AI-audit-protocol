#!/bin/bash
# run_meta.sh — per-audit run metadata: wall-clock per phase, and token usage.
#
#   run_meta.sh start  <audit-slug> <phase>            # stamp a phase start
#   run_meta.sh end    <audit-slug> <phase>            # stamp a phase end, compute elapsed
#   run_meta.sh note   <audit-slug> <key> <value>      # record an arbitrary metric
#   run_meta.sh tokens <audit-slug> <audit-dir>        # scrape Codex logs for token usage
#   run_meta.sh report                                 # render RUNS.md from the event log
#
# Events append to $ROOT/audits/runs.jsonl. That file is the source of truth; RUNS.md is derived
# and regenerated, never hand-edited. Wall-clock is measured, not estimated — a phase with a start
# and no end renders as RUNNING so an abandoned phase cannot masquerade as a fast one.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOG="$ROOT/audits/runs.jsonl"
mkdir -p "$ROOT/audits"
CMD="${1:?usage: run_meta.sh start|end|note|tokens|report ...}"

emit () {  # emit <json-object-body>
  printf '{"ts":"%s","epoch":%s,%s}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(date +%s)" "$1" >> "$LOG"
}

case "$CMD" in
  start)
    A="${2:?audit slug}"; P="${3:?phase}"
    emit "\"event\":\"start\",\"audit\":\"$A\",\"phase\":\"$P\""
    echo "[meta] $A / $P started $(date +%H:%M:%S)"
    ;;
  end)
    A="${2:?audit slug}"; P="${3:?phase}"
    emit "\"event\":\"end\",\"audit\":\"$A\",\"phase\":\"$P\""
    echo "[meta] $A / $P ended $(date +%H:%M:%S)"
    ;;
  note)
    A="${2:?audit slug}"; K="${3:?key}"; V="${4:?value}"
    emit "\"event\":\"note\",\"audit\":\"$A\",\"key\":\"$K\",\"value\":\"$V\""
    echo "[meta] $A / $K = $V"
    ;;
  tokens)
    A="${2:?audit slug}"; D="${3:?audit dir}"
    python3 - "$A" "$D" "$LOG" <<'PY'
import sys, re, json, pathlib, time, datetime
audit, d, log = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
# Codex prints a token total per session; it has appeared both inline ("tokens used: 3,882")
# and split across two lines ("tokens used" / "3,882"). Accept both, and require the number to
# follow the marker so an unrelated digit elsewhere in the log cannot be mistaken for a total.
pat = re.compile(r"tokens?\s+used[:\s]*([\d,]+)", re.I)
rows, total = [], 0
for f in sorted(d.glob("critique/round_*/log_*.txt")):
    t = f.read_text(errors="replace")
    t = re.sub(r"tokens?\s+used\s*\n\s*", "tokens used: ", t, flags=re.I)
    n = [int(m.group(1).replace(",", "")) for m in pat.finditer(t)]
    if n:
        v = max(n)                       # the final cumulative figure for that session
        rows.append((str(f.relative_to(d)), v)); total += v
out = {"ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
       "epoch": int(time.time()), "event": "note", "audit": audit,
       "key": "codex_tokens", "value": str(total), "detail": rows}
with log.open("a") as fh: fh.write(json.dumps(out) + "\n")
print(f"[meta] {audit}: codex tokens = {total:,} across {len(rows)} sessions")
if not rows: print("[meta]   (no Codex logs found yet — critique rounds have not run)")
PY
    ;;
  report)
    python3 - "$LOG" "$ROOT/audits/RUNS.md" <<'PY'
import sys, json, pathlib, collections, datetime, time
log, dest = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
ev = [json.loads(l) for l in log.read_text().splitlines() if l.strip()] if log.exists() else []
starts, spans, notes = {}, collections.defaultdict(list), collections.defaultdict(dict)
order = []
for e in ev:
    a = e.get("audit", "?")
    if a not in order: order.append(a)
    if e["event"] == "start": starts[(a, e["phase"])] = e["epoch"]
    elif e["event"] == "end":
        s = starts.pop((a, e["phase"]), None)
        if s is not None: spans[a].append((e["phase"], e["epoch"] - s))
    elif e["event"] == "note": notes[a][e["key"]] = e["value"]

def hms(sec):
    h, m = divmod(int(sec) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {int(sec) % 60:02d}s"

L = ["# Run metadata", "",
     "Derived from `runs.jsonl` by `scripts/run_meta.sh report`. Do not hand-edit.", "",
     f"Regenerated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}.", ""]
if not order:
    L += ["No runs recorded yet."]
else:
    L += ["## Wall-clock by phase", "", "| Audit | Phase | Wall-clock |", "|---|---|---|"]
    for a in order:
        for ph, sec in spans[a]: L.append(f"| {a} | {ph} | {hms(sec)} |")
        for (aa, ph), s in sorted(starts.items()):
            if aa == a: L.append(f"| {a} | {ph} | RUNNING ({hms(time.time()-s)} so far) |")

    def num(v):
        try: return f"{int(v):,}"
        except (TypeError, ValueError): return "—"

    L += ["", "## Totals per audit", "",
          "| Audit | Total wall-clock | Codex tokens | Claude agent output tokens | Agents |",
          "|---|---|---|---|---|"]
    for a in order:
        tot = sum(s for _, s in spans[a]); n = notes[a]
        L.append(f"| {a} | {hms(tot) if tot else '—'} | {num(n.get('codex_tokens'))} | "
                 f"{num(n.get('claude_output_tokens'))} | {n.get('agents', '—')} |")
    reserved = ("codex_tokens", "claude_output_tokens", "agents")
    extra = {k for a in order for k in notes[a] if k not in reserved}
    if extra:
        L += ["", "| Audit | Metric | Value |", "|---|---|---|"]
        for a in order:
            for k in sorted(notes[a]):
                if k in extra: L.append(f"| {a} | {k} | {notes[a][k]} |")
dest.write_text("\n".join(L) + "\n")
print("\n".join(L))
PY
    ;;
  *) echo "unknown subcommand: $CMD"; exit 1 ;;
esac
