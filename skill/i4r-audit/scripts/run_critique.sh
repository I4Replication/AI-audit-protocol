#!/bin/bash
# Fan out independent ChatGPT/Codex critics against a report version, then have Codex
# synthesize their findings into one consolidated critique.
#
# Usage: ./run_codex_critique.sh <round_number>
#
# Design notes learned the hard way:
#  * Prompt text lives in prompts/*.txt, never inline. Inline heredocs/quoted strings containing
#    apostrophes and quotes broke the parser once already.
#  * Critics run SEQUENTIALLY. Four concurrent Codex sessions on one account get throttled
#    (websocket "Connection refused" then "stream disconnected"), which killed all four.
#  * Each critic gets up to 3 attempts; an existing non-empty output file counts as done, so
#    the script is resumable.

set -uo pipefail

# NEVER edit this script while a round is running. Bash reads a script incrementally, so changing
# it mid-run shifts byte offsets under the live process and produces a bogus syntax error at a line
# that is perfectly valid — in calibration this killed a synthesis step after all four critics had
# already succeeded. The run is resumable (a non-empty critic output counts as done), so the recovery
# is to re-invoke with the same round number; but the fix is to wait for the round to finish.

WS="${AUDIT_DIR:?set AUDIT_DIR to the audit workspace}"
SKILL="$(cd "$(dirname "$0")/.." && pwd)"
CODEX=/Applications/ChatGPT.app/Contents/Resources/codex
# PIN THE CRITIC MODEL AND EFFORT. Do not inherit ~/.codex/config.toml.
# On 2026-08-17 the CLI updated to 0.148.0-alpha.9 and its configured default became
# gpt-5.3-codex-spark, which a ChatGPT-account login cannot use: every critic in the round died with
# HTTP 400 "model is not supported", three attempts each, and the round produced nothing. The audit
# before this one recommended pinning and it was not done; this is that fix.
# A silently changed default is also the exact hazard model_assignments exists to record - an audit
# whose critics quietly swap model is not the audit its metadata claims.
# Unified defaults (team decision 2026-08-21): critics on gpt-5.6-terra at MAX effort. Accepts
# Bruno's MODEL/EFFORT names too. The CLI records whatever effort string you pass, valid or not, so
# the rollout alone proves nothing; an invalid value is rejected by the API with
# invalid_request_error, so output at all means the level was honoured (tested: max and xhigh
# accepted, "banana" rejected). Confirm the served model per session with verify_codex_model.sh.
CRITIC_MODEL="${CRITIC_MODEL:-${MODEL:-gpt-5.6-terra}}"
CRITIC_EFFORT="${CRITIC_EFFORT:-${EFFORT:-max}}"

# Fail fast if the pinned model is rejected. Without this the round burns MAX_TRIES per critic on an
# error that is identical every time, and reports "no output file" rather than the real cause.
printf 'reply with the single word OK\n' | "$CODEX" exec --skip-git-repo-check -m "$CRITIC_MODEL" \
  -c sandbox_mode=read-only - > /tmp/i4r_model_probe.$$ 2>&1
if grep -qiE 'out of credits' /tmp/i4r_model_probe.$$; then
  echo "FATAL: the Codex workspace is OUT OF CREDITS — every critic would die with 'no output" >&2
  echo "       file' after three useless retries (this ate a full round once). Refill, re-run." >&2
  grep -oiE 'out of credits[^"]*' /tmp/i4r_model_probe.$$ | head -1 >&2
  rm -f /tmp/i4r_model_probe.$$; exit 2
fi
if grep -qiE '"?(is not supported|invalid_request_error|unauthorized)' /tmp/i4r_model_probe.$$; then
  echo "FATAL: the critic model '$CRITIC_MODEL' is rejected by this Codex account:" >&2
  grep -oE '"message":"[^"]*"' /tmp/i4r_model_probe.$$ | head -1 >&2
  echo "       set CRITIC_MODEL to a supported model and re-run. No round was started." >&2
  rm -f /tmp/i4r_model_probe.$$; exit 2
fi
rm -f /tmp/i4r_model_probe.$$
ROUND="${1:?usage: run_codex_critique.sh <round_number>}"
# Keying the review file to the round number silently serves STALE text whenever the draft was
# revised out of band (e.g. a round run by a different critic pool, or a round re-run after a fix).
# In calibration this made an entire external round review a superseded revision: a third of its
# blocking items were already fixed. Always review the CURRENT draft; fall back to the round file.
if [ -f "$WS/report/REPORT_final.md" ]; then
  REPORT="$WS/report/REPORT_final.md"
else
  REPORT="$WS/report/REPORT_v${ROUND}.md"
fi
OUT="$WS/critique/round_${ROUND}"
P="$SKILL/prompts"
MAX_TRIES=3

[ -f "$REPORT" ] || { echo "FATAL: $REPORT not found"; exit 1; }
for f in common.txt synth.txt role_stats.txt role_data.txt role_fairness.txt role_completeness.txt; do
  [ -s "$P/$f" ] || { echo "FATAL: missing prompt file $P/$f"; exit 1; }
done
mkdir -p "$OUT"
cp "$REPORT" "$WS/report/REPORT_TO_REVIEW.md"

NAMES="stats data fairness completeness"

# Round-dependent critic set. Round 1 needs all four: the failure mode then is a MISSED finding and
# redundancy is what catches it. FROM ROUND 2 the failure mode has already flipped to a WRONG
# correction in our own new text, which two critics catch as reliably as four. Measured over the
# Philyaw and Refalo audits (2026-08-21/22): round 2 produced zero new findings about either paper
# and only corrections to the audit's own record, and in earlier calibration rounds three of four
# critics converged on identical items. The threshold moved from 5 to 2 on 2026-08-22; it saves
# roughly 40 minutes a round. Override with CRITICS=.
if [ -z "${CRITICS:-}" ] && [ "$ROUND" -ge 2 ]; then
  NAMES="stats completeness"
  echo "  round $ROUND >= 2: running 2 critics (set CRITICS to override)"
elif [ -n "${CRITICS:-}" ]; then
  NAMES="$CRITICS"
fi

# Concurrency. Four concurrent sessions were throttled to death once, on an older CLI; the fix at
# the time was to drop to one, then to two. The pre-flight probe now catches a dead account or a
# rejected model before any critic starts, and every critic is individually resumable (a non-empty
# output counts as done), so a throttled critic costs a retry rather than the round. Round 1 runs
# its four critics 4-up; later rounds have only two critics anyway. Set PARALLEL=2 or 1 if
# throttling reappears — the serial retry pass below already recovers anything that comes back empty.
if [ -z "${PARALLEL:-}" ]; then
  if [ "$ROUND" -eq 1 ]; then PARALLEL=4; else PARALLEL=2; fi
fi

# Round 1 lost critic_completeness and three synthesis attempts to a DNS outage
# ("failed to lookup address information"). Wait for connectivity to come back rather than
# burning retries into a dead network. Up to 30 minutes, checking every 30 s.
wait_for_net () {
  local i=0
  while [ "$i" -lt 60 ]; do
    if curl -s -o /dev/null --max-time 10 https://chatgpt.com/ 2>/dev/null; then
      [ "$i" -gt 0 ] && echo "         network back after $((i*30))s"
      sleep 5; return 0
    fi
    [ "$i" -eq 0 ] && echo "         waiting for network..."
    i=$((i+1)); sleep 30
  done
  echo "         network still down after 30 min; continuing anyway"; return 1
}

# Build a critic prompt on stdout: common preamble + role, with ${ROUND} substituted.
# Per-audit context lives in $AUDIT_DIR/PAPER.txt, ENV.txt, SLOW.txt and (optionally) WRAPUP.txt.
# WRAPUP.txt tells critics how many rounds remain, so a late round closes threads instead of
# opening new ones. The prompt files in the
# skill are paper-agnostic; a missing context file is a hard error, because a critic primed with
# the wrong paper will confidently review a document that does not exist.
subst () {
  python3 - "$1" "$WS" "$ROUND" <<'PYEOF'
import sys, pathlib
tpl, ws, rnd = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3]
t = pathlib.Path(tpl).read_text()
for key, fname, default in (("PAPER","PAPER.txt",None),
                            ("ENV","ENV.txt","python3 with the scientific stack"),
                            ("SLOW","SLOW.txt","any job over ~10 minutes"),
                            ("WRAPUP","WRAPUP.txt","This round is one of an open-ended series; no wrap-up constraint applies yet."),
                            ("SETTLED","SETTLED.md","No settled-facts file exists yet; derive what you need.")):
    f = ws/fname
    if f.exists(): val = f.read_text().strip()
    elif default is not None: val = default
    else:
        sys.stderr.write(f"FATAL: {ws/fname} is required (the {{{{{key}}}}} placeholder)\n"); sys.exit(1)
    t = t.replace("{{"+key+"}}", val)
t = t.replace("${ROUND}", rnd)
sys.stdout.write(t)
PYEOF
}

build_prompt () {
  { subst "$P/common.txt" || exit 1; printf '\n\n'; subst "$P/role_$1.txt" || exit 1; }
}

run_one () {
  local n="$1" f="$OUT/critic_$1.md" try=1 rc
  if [ -s "$f" ]; then echo "  [skip] critic_${n} (already has $(wc -l < "$f") lines)"; return 0; fi
  while [ "$try" -le "$MAX_TRIES" ]; do
    echo "  [run ] critic_${n} attempt ${try}/${MAX_TRIES} $(date +%H:%M:%S)"
    build_prompt "$n" > "$OUT/prompt_${n}.txt"
    "$CODEX" exec --skip-git-repo-check \
      -m "$CRITIC_MODEL" \
      -c model_reasoning_effort="$CRITIC_EFFORT" \
      -c sandbox_mode=workspace-write \
      -C "$WS" \
      -o "$OUT/last_${n}.txt" \
      - < "$OUT/prompt_${n}.txt" > "$OUT/log_${n}_try${try}.txt" 2>&1
    rc=$?
    if [ -s "$f" ]; then
      echo "  [ok  ] critic_${n}: $(wc -l < "$f") lines (exit $rc)"; return 0
    fi
    echo "  [fail] critic_${n} attempt ${try}: exit $rc, no output file"
    try=$((try+1)); wait_for_net
  done
  echo "  [GIVE UP] critic_${n} produced nothing after ${MAX_TRIES} attempts"; return 1
}

# Gate: the rendered prompt must actually name this paper, and must carry no placeholders.
# A critic primed with a stale citation fails silently, producing confident review of a document
# that does not exist.
build_prompt stats > "$OUT/_gate.txt" 2>/dev/null || { echo "FATAL: prompt render failed"; exit 1; }
KEY=$(head -3 "$WS/PAPER.txt" | grep -oE '[A-ZÄÖÜ][a-zäöüß]{4,}' | head -1)
if [ -n "$KEY" ] && ! grep -qi "$KEY" "$OUT/_gate.txt"; then
  echo "FATAL: rendered prompt never mentions '$KEY' from PAPER.txt — critics would review the wrong paper"; exit 1
fi
if grep -q '{{' "$OUT/_gate.txt"; then
  echo "FATAL: unsubstituted placeholder in rendered prompt:"; grep -o '{{[A-Z]*}}' "$OUT/_gate.txt" | sort -u; exit 1
fi
rm -f "$OUT/_gate.txt"
echo "prompt gate passed (paper keyword: ${KEY:-none})"

echo "=== Round ${ROUND}: Codex critics — $(echo $NAMES | wc -w | tr -d ' ') roles, ${PARALLEL}-up ==="
# Run up to $PARALLEL critics at a time. run_one is already resumable (a non-empty output file
# counts as done), so a throttled critic is retried by re-running the script rather than lost.
running=0
for n in $NAMES; do
  if [ "$PARALLEL" -le 1 ]; then
    run_one "$n"
  else
    run_one "$n" &
    running=$((running + 1))
    if [ "$running" -ge "$PARALLEL" ]; then wait -n 2>/dev/null || wait; running=$((running - 1)); fi
  fi
done
wait

# Any critic that produced nothing was probably throttled; retry those serially before synthesis.
for n in $NAMES; do
  if [ ! -s "$OUT/critic_$n.md" ]; then
    echo "  [retry] critic_$n produced no output — retrying serially"
    run_one "$n"
  fi
done

echo "=== critic inventory ==="
ls -la "$OUT"/critic_*.md 2>/dev/null || echo "  (none)"
present=$(ls "$OUT"/critic_*.md 2>/dev/null | wc -l | tr -d ' ')
[ "$present" -gt 0 ] || { echo "FATAL: no critic output; aborting synthesis"; exit 1; }

# Synthesis is a serial pass that re-reads work already done, and it cost 22-25 minutes per round
# in the 2026-08-22 audits. With four critic reports it earns that by deduplicating and adjudicating
# between them. With two it does not: the coordinator can read both directly, and reading them
# directly is what actually happened in those audits while synthesis was still running. Skipped at
# <=2 critics unless SYNTH=1 forces it; SYNTH=0 skips it always.
if [ "${SYNTH:-auto}" = "0" ] || { [ "${SYNTH:-auto}" = "auto" ] && [ "$present" -le 2 ]; }; then
  echo "=== Round ${ROUND}: synthesis SKIPPED (${present} critic report(s) — read them directly) ==="
  echo "    critic reports: $(ls "$OUT"/critic_*.md | tr '\n' ' ')"
  echo "    set SYNTH=1 to force a consolidated CRITIQUE.md."
  exit 0
fi
echo "=== Round ${ROUND}: synthesis over ${present} critiques ==="
subst "$P/synth.txt" | sed "s/\${present}/${present}/g" > "$OUT/prompt_synth.txt"

try=1
while [ "$try" -le "$MAX_TRIES" ]; do
  echo "  [run ] synthesis attempt ${try}/${MAX_TRIES} $(date +%H:%M:%S)"
  "$CODEX" exec --skip-git-repo-check \
    -m "$CRITIC_MODEL" \
    -c model_reasoning_effort="$CRITIC_EFFORT" \
    -c sandbox_mode=workspace-write \
    -C "$WS" \
    -o "$OUT/last_synth.txt" \
    - < "$OUT/prompt_synth.txt" > "$OUT/log_synth_try${try}.txt" 2>&1
  rc=$?
  if [ -s "$OUT/CRITIQUE.md" ]; then
    echo "=== CRITIQUE.md written: $(wc -l < "$OUT/CRITIQUE.md") lines (exit $rc) ==="; exit 0
  fi
  echo "  [fail] synthesis attempt ${try}: exit $rc"; try=$((try+1)); sleep 30
done
echo "FATAL: synthesis produced no CRITIQUE.md after ${MAX_TRIES} attempts"; exit 1
