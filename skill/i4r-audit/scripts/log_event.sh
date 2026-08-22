#!/bin/bash
# log_event.sh [--run <run.json>] <type> [key=value ...]     (or: log_event.sh --self-test)
#
# Appends ONE structured event to run.json's journal and projects it into the typed fields.
# Call it the moment something happens. Everything this records is unreconstructable later.
#
#   export RUN_JSON=<audit-dir>/run.json     # then --run can be omitted
#
#   log_event.sh phase_start phase=fan_out
#   log_event.sh phase_end   phase=fan_out
#   log_event.sh round_start round=3 parallelism=2 report_version_reviewed=REPORT_v3.md
#   log_event.sh model role=critic_stats role_class=critic round=3 \
#                      model_id=gpt-5.4-2026-01-20 model_family=gpt provider=openai source=observed
#   log_event.sh tokens role=critic_stats round=3 input_tokens=98211 output_tokens=8140 \
#                      total_tokens=106351 source=cli_log
#   log_event.sh tokens role=auditor_reproduction obtainable=false      # null, NOT an estimate
#   log_event.sh cost   role=critic_stats round=3 amount=2.41 currency=USD billing_model=metered_api
#   log_event.sh failure kind=quota_exhausted role=critic_fairness round=3 retries=3 \
#                      detail='429 insufficient_quota on all attempts' \
#                      substitution='re-ran on the auditor family; round 3 is not independent'
#   log_event.sh brief_correction raised_by=auditor_privacy_archive field='file count' \
#                      description='brief said 198, pkg/ holds 213'
#   log_event.sh round_result round=3 blocking_items=3 comment_defects=3 paper_defects=0 \
#                      integrated_at=2026-07-15T18:01:30Z character=framing
#   log_event.sh gate gate=check_consistency round=3 exit_status=$?   # capture $? IMMEDIATELY
#   log_event.sh delivery artifact_path=report/comment.docx recipient='I4R editorial'
#   log_event.sh human_effort phase=critique person_minutes=95   # self-reported; accumulates
#   log_event.sh settled used=true generated_after_round=2 path=SETTLED.md entries=14
#
# Values are typed automatically: bare integers and decimals become numbers, true/false become
# booleans, `null` and the empty string become null, everything else is a string. Use `key:=<json>`
# to pass raw JSON (arrays, objects).
#
# SAFE TO CALL REPEATEDLY AND CONCURRENTLY. Every write takes an exclusive lock, rewrites the file
# through a temporary in the same directory, and renames it into place, so a reader never sees a
# half-written file and two writers never lose each other's event. A stale lock whose owner
# process is dead is broken automatically after 5 seconds.
#
# Exit status: 0 written, 2 refused (bad arguments, missing file, lock timeout). Never silent.
set -uo pipefail

SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"
LOCK_TIMEOUT="${LOCK_TIMEOUT:-60}"     # seconds to wait for the write lock

TYPES="init phase_start phase_end round_start round_end round_result model tokens cost failure brief_correction gate delivery package records human_effort settled note finalize"
PHASES="intake fan_out adjudication critique release"

die () { echo "FATAL: log_event.sh: $*" >&2; exit 2; }
now_utc () { date -u +%Y-%m-%dT%H:%M:%SZ; }
in_list () { case " $2 " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

LOCK=""
release_lock () { [ -n "$LOCK" ] && rm -rf "$LOCK"; }

acquire_lock () {
  local waited=0 limit owner age now_s lock_s
  limit=$((LOCK_TIMEOUT * 20))          # polling at 0.05 s
  while true; do
    mkdir "$LOCK" 2>/dev/null
    if [ $? -eq 0 ]; then
      printf '%s\n' "$$" > "$LOCK/pid"
      date +%s > "$LOCK/ts"
      trap release_lock EXIT INT TERM
      return 0
    fi
    # Break a lock whose owner is gone AND which is at least 5 s old. Both conditions: a live
    # writer can hold the lock for a moment, and a pid check alone races against a just-forked
    # writer that has not written its pid file yet.
    owner=$(cat "$LOCK/pid" 2>/dev/null)
    lock_s=$(cat "$LOCK/ts" 2>/dev/null)
    now_s=$(date +%s)
    if [ -n "$owner" ] && [ -n "$lock_s" ]; then
      age=$((now_s - lock_s))
      kill -0 "$owner" 2>/dev/null
      if [ $? -ne 0 ] && [ "$age" -ge 5 ]; then
        echo "WARNING: breaking a stale lock held by dead pid $owner (${age}s old)" >&2
        rm -rf "$LOCK"
        continue
      fi
    else
      # An ORPHANED lock: the directory exists but carries no pid and no ts, so the branch above
      # cannot decide anything and falls straight through. Before this clause such a lock was
      # UNBREAKABLE -- every later writer blocked until LOCK_TIMEOUT, forever, and the only cure
      # was deleting the directory by hand.
      #
      # The window is real and narrow: mkdir succeeds, then the owner dies before `printf > pid`
      # and `date > ts` land. A kill, a full disk, or a synced-folder stall does it. It happened on
      # the first audit run out of a Dropbox directory and cost that run its model_assignments.
      #
      # Fall back on the DIRECTORY's own mtime, which the filesystem records whether or not the
      # owner ever got to write anything into it.
      lock_s=$(stat -f %m "$LOCK" 2>/dev/null || stat -c %Y "$LOCK" 2>/dev/null)
      age=$(( now_s - ${lock_s:-$now_s} ))
      if [ "$age" -ge 5 ]; then
        echo "WARNING: breaking an orphaned lock with no pid/ts (${age}s old); its owner died between mkdir and writing its metadata" >&2
        rm -rf "$LOCK"
        continue
      fi
    fi
    sleep 0.05
    waited=$((waited + 1))
    if [ "$waited" -gt "$limit" ]; then return 1; fi
  done
}

# ------------------------------------------------------------------- the write
write_event () {
  local RUN="$1" TYPE="$2"; shift 2
  local DIRN JOURNAL TMP rc before after
  DIRN=$(dirname "$RUN")
  JOURNAL="$DIRN/run.events.jsonl"
  TMP="$RUN.tmp.$$"

  before=$(jq -r '.events | length' "$RUN" 2>/dev/null)
  if [ -z "$before" ]; then echo "FATAL: $RUN is unreadable or has no events array" >&2; return 1; fi

  # The input file arrives on STDIN, not as an argument. Under --args every remaining
  # non-option argument is a positional string, so a filename there is consumed as data,
  # jq reads an empty stdin, and the output is an empty file -- which `jq empty` then
  # accepts, renaming a zero-byte file over the run record. Both halves of that are fixed
  # here: stdin for the input, and a real content check before the rename.
  jq --arg now "$(now_utc)" --arg type "$TYPE" --arg pid "$$" --arg src "log_event.sh" \
     '
    # ---- typed key=value parsing -------------------------------------------------
    def parse_kv:
      reduce .[] as $kv ({};
        ($kv | index("=")) as $i
        | if $i == null then .
          else
            ($kv[:$i]) as $k0 | ($kv[$i+1:]) as $v
            | if ($k0 | endswith(":"))
                then .[$k0[:-1]] = ($v | fromjson)
              elif $v == "" or $v == "null" then .[$k0] = null
              elif $v == "true"  then .[$k0] = true
              elif $v == "false" then .[$k0] = false
              elif ($v | test("^-?[0-9]+$"))            then .[$k0] = ($v | tonumber)
              elif ($v | test("^-?[0-9]*\\.[0-9]+$"))   then .[$k0] = ($v | tonumber)
              else .[$k0] = $v
              end
          end);
    def pick($keys): with_entries(select(.key as $k | $keys | index($k)));
    def listify: if type == "string" then (split(",") | map(select(length > 0))) else . end;
    def dur($a; $b):
      if $a == null or $b == null then null
      else (($b | fromdateiso8601) - ($a | fromdateiso8601)) | floor
      end;

    ($ARGS.positional | parse_kv) as $p
    | ($p.at // $now) as $t
    # The outer parentheses are load-bearing: without them jq binds `as` tighter than `+`
    # and the entire rest of the program becomes the right operand of the addition.
    | ((((.events | map(.seq) | max) // 0) + 1)) as $seq

    # ---- 1. the journal entry: always, whatever the type ---------------------------
    | .events += [{ seq: $seq, at: $t, type: $type,
                    payload: ($p | del(.at)),
                    pid: ($pid | tonumber), source: $src }]

    # ---- 2. the projection into typed fields ---------------------------------------
    | def ensure_round($r):
        if (.rounds | map(.round) | index($r)) != null then .
        else .rounds += [{ round: $r, started_at: null, ended_at: null, wall_clock_seconds: null,
                           critics_run: [], critics_failed: [], parallelism: null,
                           report_version_reviewed: null, blocking_items: null,
                           non_blocking_items: null, comment_defects: null, paper_defects: null,
                           unclassified_defects: null, items_accepted: null, items_contested: null,
                           integrated_at: null, integrated_before_delivery: null,
                           consistency_check_run: null, consistency_check_exit: null,
                           character: null, notes: null }]
             | .rounds |= sort_by(.round)
        end;
      def merge_round($r; $fields):
        .rounds |= map(if .round == $r then . + $fields else . end);

      if $type == "phase_start" then
        .phases[$p.phase].started_at = $t
        | .phases[$p.phase].notes = ($p.notes // .phases[$p.phase].notes)
        | .status = (if .status == "initialized" then "running" else .status end)

      elif $type == "phase_end" then
        # A phase closed in the same second it opened was stamped retroactively, not measured.
        # Record null rather than 0: a zero that looks measured is worse than an honest gap, and
        # the rest of this record already follows that rule. Added 2026-08-22, after two audits
        # filed 0h00m intake and release phases that nobody had actually timed.
        # NOTE: no apostrophes in this comment. It sits inside a single-quoted shell string.
        .phases[$p.phase].ended_at = $t
        | .phases[$p.phase].ran = (.phases[$p.phase].started_at != null)
        | (dur(.phases[$p.phase].started_at; $t)) as $d
        | .phases[$p.phase].wall_clock_seconds =
            (if $d == null then null elif $d < 2 then null else $d end)
        | .phases[$p.phase].notes =
            (($p.notes // .phases[$p.phase].notes) as $n
             | if ($d != null and $d < 2)
               then (($n // "") + " [duration not recorded: opened and closed in the same second, so it was stamped retroactively rather than measured]" | ltrimstr(" "))
               else $n end)

      elif $type == "round_start" then
        ensure_round($p.round)
        | merge_round($p.round;
            ({started_at: $t} + ($p | del(.round, .at)
              | pick(["parallelism","report_version_reviewed","critics_run","critics_failed","notes"])
              | if has("critics_run") then .critics_run |= listify else . end
              | if has("critics_failed") then .critics_failed |= listify else . end)))
        | .status = (if .status == "initialized" then "running" else .status end)

      elif $type == "round_end" then
        ensure_round($p.round)
        | merge_round($p.round; {ended_at: $t})
        | .rounds |= map(if .round == $p.round
                         then .wall_clock_seconds = dur(.started_at; .ended_at) else . end)
        | merge_round($p.round;
            ($p | del(.round, .at)
             | pick(["critics_run","critics_failed","notes"])
             | if has("critics_run") then .critics_run |= listify else . end
             | if has("critics_failed") then .critics_failed |= listify else . end))

      elif $type == "round_result" then
        ensure_round($p.round)
        | merge_round($p.round;
            ($p | del(.round, .at)
             | pick(["blocking_items","non_blocking_items","comment_defects","paper_defects",
                     "unclassified_defects","items_accepted","items_contested","integrated_at",
                     "consistency_check_run","consistency_check_exit","character","notes",
                     "report_version_reviewed"])))
        # unclassified is derived, so a partial classification is visible rather than implied
        | .rounds |= map(
            if .round == $p.round and .blocking_items != null
               and .comment_defects != null and .paper_defects != null
               and ($p | has("unclassified_defects") | not)
            then .unclassified_defects = (.blocking_items - .comment_defects - .paper_defects)
            else . end)

      elif $type == "model" then
        ( [ .model_assignments[] | select(.role == $p.role) ] | last ) as $prev
        | .model_assignments += [{
            role: $p.role,
            role_class: ($p.role_class // "other"),
            round: ($p.round // null),
            model_id: $p.model_id,
            model_family: ($p.model_family // null),
            provider: $p.provider,
            differs_from_auditor_family:
              (($p.model_family // "unknown") != .auditor_model.family),
            auditor_family_at_time: .auditor_model.family,
            source: ($p.source // "declared"),
            # A model that changes under a role is the silent-swap signature. Fill it in
            # automatically: nobody logs a swap they did not know had happened.
            replaces_model_id:
              ($p.replaces_model_id //
               (if $prev != null and $prev.model_id != $p.model_id then $prev.model_id else null end)),
            intended: (if ($p | has("intended")) then $p.intended else true end),
            recorded_at: $t }]

      elif $type == "tokens" then
        .tokens += [{
          role: $p.role, round: ($p.round // null),
          obtainable: (if ($p | has("obtainable")) then $p.obtainable
                       else (($p.total_tokens // $p.input_tokens // null) != null) end),
          input_tokens: ($p.input_tokens // null),
          output_tokens: ($p.output_tokens // null),
          cached_input_tokens: ($p.cached_input_tokens // null),
          reasoning_tokens: ($p.reasoning_tokens // null),
          total_tokens: ($p.total_tokens // null),
          source: ($p.source // (if ($p.total_tokens // $p.input_tokens // null) == null
                                 then "unavailable" else null end)),
          recorded_at: $t }]

      elif $type == "cost" then
        .costs += [{
          role: $p.role, round: ($p.round // null),
          obtainable: (if ($p | has("obtainable")) then $p.obtainable
                       else (($p.amount // null) != null) end),
          amount: ($p.amount // null),
          currency: ($p.currency // (if ($p.amount // null) == null then null else "USD" end)),
          billing_model: ($p.billing_model // null),
          source: ($p.source // (if ($p.amount // null) == null then "unavailable" else null end)),
          recorded_at: $t }]

      elif $type == "failure" then
        .failure_events += [{
          at: $t, kind: $p.kind, role: ($p.role // null), round: ($p.round // null),
          detail: $p.detail, retries: ($p.retries // null),
          substitution: ($p.substitution // null),
          resolved: (if ($p | has("resolved")) then $p.resolved else null end) }]

      elif $type == "brief_correction" then
        .brief_corrections.items += [{
          id: ($p.id // ("bc" + ((.brief_corrections.items | length) + 1 | tostring))),
          at: $t, raised_by: ($p.raised_by // null), field: ($p.field // null),
          description: $p.description,
          accepted: (if ($p | has("accepted")) then $p.accepted else null end) }]
        | .brief_corrections.count = (.brief_corrections.items | length)

      elif $type == "gate" then
        # compliance is rebuilt at finalize; the gate log accumulates here so a gate result is
        # never held only in a transcript. Seed a schema-valid skeleton on first use.
        (if .compliance == null then
           .compliance = { phases_ran: {intake:false, fan_out:false, adjudication:false,
                                        critique:false, release:false},
                           all_five_auditors_ran: null,
                           critique_integrated_before_delivery: false,
                           rounds_integrated_before_delivery: null,
                           rounds_not_integrated: [],
                           converged: false, critics_independent_all_rounds: null,
                           no_silent_model_swap: null, gates_run: [],
                           consistency_check_after_every_round: null,
                           compliance_pass: false,
                           violations: ["not yet finalized"] }
         else . end)
        | .compliance.gates_run += [{
            gate: $p.gate, at: $t, round: ($p.round // null),
            exit_status: $p.exit_status,
            passed: ($p.exit_status == 0),
            log_path: ($p.log_path // null) }]

      elif $type == "delivery" then
        .delivery = ((.delivery // {}) + {
          delivered_at: ($p.delivered_at // $t),
          artifact_path: ($p.artifact_path // (.delivery.artifact_path? // null)),
          artifact_sha256: ($p.artifact_sha256 // (.delivery.artifact_sha256? // null)),
          release_dir: ($p.release_dir // (.delivery.release_dir? // null)),
          recipient: ($p.recipient // (.delivery.recipient? // null)) })

      elif $type == "package" then
        .package = ((.package // {}) + ($p | del(.at)))

      elif $type == "human_effort" then
        # Person-minutes ACCUMULATE: effort on a phase is reported in whatever chunks the
        # person remembers, and overwriting would silently discard every earlier report.
        .human_effort.person_minutes_by_phase[$p.phase] =
          (((.human_effort.person_minutes_by_phase[$p.phase]) // 0) + $p.person_minutes)
        | .human_effort.people_involved = ($p.people_involved // .human_effort.people_involved)
        | .human_effort.notes = ($p.notes // .human_effort.notes)

      elif $type == "settled" then
        .settled_facts = ((.settled_facts // {}) + ($p | del(.at)))

      elif $type == "records" then
        .records_retained += [{
          artifact: $p.artifact, path: ($p.path // null),
          present: (if ($p | has("present")) then $p.present else false end),
          count: ($p.count // null), bytes: ($p.bytes // null), sha256: ($p.sha256 // null),
          retention: ($p.retention // null), reason: ($p.reason // null) }]
        | .records_retained_checked_at = $t

      else .        # note, init, finalize: journal only
      end
  ' --args "$@" < "$RUN" > "$TMP"
  rc=$?
  if [ "$rc" -ne 0 ]; then rm -f "$TMP"; echo "FATAL: jq projection failed (exit $rc)" >&2; return 1; fi

  # Never rename a file we have not proved is the record plus exactly one event. `jq empty`
  # is NOT sufficient: it exits 0 on an empty file, so it certifies the one output that
  # destroys the run. Check the shape and the event count instead.
  if [ ! -s "$TMP" ]; then
    rm -f "$TMP"; echo "FATAL: projection produced an empty file; original left untouched" >&2; return 1
  fi
  after=$(jq -r 'if type == "object" and (.events | type) == "array" then (.events | length) else "bad" end' "$TMP" 2>/dev/null)
  if [ "$after" != "$((before + 1))" ]; then
    rm -f "$TMP"
    echo "FATAL: projection produced '$after' events, expected $((before + 1)); original left untouched" >&2
    return 1
  fi

  mv -f "$TMP" "$RUN"
  if [ $? -ne 0 ]; then rm -f "$TMP"; echo "FATAL: could not rename the new run.json into place" >&2; return 1; fi

  jq -c '.events[-1]' "$RUN" >> "$JOURNAL"
  if [ $? -ne 0 ]; then echo "WARNING: run.json updated but the journal append failed" >&2; fi
  return 0
}

# --------------------------------------------------------------------- main
main () {
  command -v jq >/dev/null; if [ $? -ne 0 ]; then die "jq is required but not on PATH"; fi

  local RUN="${RUN_JSON:-}"
  while [ $# -gt 0 ]; do
    case "$1" in
      --run) RUN="${2:-}"; shift 2 ;;
      -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
      *) break ;;
    esac
  done
  [ -n "$RUN" ] || die "no run.json given: pass --run <path> or set RUN_JSON"
  [ -f "$RUN" ] || die "$RUN does not exist -- run init_run.sh first"
  [ $# -ge 1 ] || die "no event type given (one of: $TYPES)"

  local TYPE="$1"; shift
  in_list "$TYPE" "$TYPES" || die "unknown event type '$TYPE' (one of: $TYPES)"

  # Per-type required keys. Refusing here rather than writing a half-specified event is
  # deliberate: an event that cannot be projected is an event nobody will notice is missing.
  local need="" k found
  case "$TYPE" in
    phase_start|phase_end) need="phase" ;;
    round_start|round_end|round_result) need="round" ;;
    model) need="role model_id provider" ;;
    tokens|cost) need="role" ;;
    failure) need="kind detail" ;;
    brief_correction) need="description" ;;
    gate) need="gate exit_status" ;;
    records) need="artifact" ;;
    human_effort) need="phase person_minutes" ;;
    settled) need="used" ;;
  esac
  for k in $need; do
    found=0
    for a in "$@"; do case "$a" in "$k="*|"$k:="*) found=1 ;; esac; done
    [ "$found" -eq 1 ] || die "event type '$TYPE' requires $k=<value>"
  done
  if [ "$TYPE" = "phase_start" ] || [ "$TYPE" = "phase_end" ] || [ "$TYPE" = "human_effort" ]; then
    for a in "$@"; do
      case "$a" in
        phase=*) in_list "${a#phase=}" "$PHASES" || die "unknown phase '${a#phase=}' (one of: $PHASES)" ;;
      esac
    done
  fi

  LOCK="$RUN.lock"
  acquire_lock
  if [ $? -ne 0 ]; then die "timed out after ${LOCK_TIMEOUT}s waiting for the write lock on $RUN"; fi
  write_event "$RUN" "$TYPE" "$@"
  local rc=$?
  release_lock; LOCK=""
  if [ "$rc" -ne 0 ]; then exit 1; fi
  return 0
}

# ------------------------------------------------------------------ self-test
self_test () {
  command -v jq >/dev/null; if [ $? -ne 0 ]; then die "jq is required"; fi
  local T rc fails n i INIT
  INIT="$(dirname "$SELF")/init_run.sh"
  [ -f "$INIT" ] || die "init_run.sh must sit beside log_event.sh for the self-test"
  T=$(mktemp -d "${TMPDIR:-/tmp}/log_event_selftest.XXXXXX") || die "no scratch dir"
  fails=0
  echo "== log_event.sh self-test in $T =="

  mkdir -p "$T/a/pkg"; printf 'x\n' > "$T/a/pkg/f.csv"
  bash "$INIT" "$T/a" --audit-id t-1 --doi 10.1234/abc \
       --auditor-model claude-test --auditor-family claude --auditor-provider anthropic \
       --protocol-version p1 --gate-set-version g1 > /dev/null 2>&1
  if [ $? -ne 0 ]; then echo "  FAIL: could not initialise a run to test against"; rm -rf "$T"; return 1; fi
  local RUN="$T/a/run.json"

  # 1. phase timing
  bash "$SELF" --run "$RUN" phase_start phase=fan_out at=2026-07-14T10:00:00Z; rc=$?
  bash "$SELF" --run "$RUN" phase_end   phase=fan_out at=2026-07-14T10:52:00Z; rc=$((rc + $?))
  n=$(jq -r '.phases.fan_out.wall_clock_seconds' "$RUN")
  if [ "$rc" -ne 0 ] || [ "$n" != "3120" ]; then
    echo "  FAIL: phase wall clock is '$n', expected 3120 (rc $rc)"; fails=1
  else echo "  ok: phase_start/phase_end computed 3120 s"; fi
  n=$(jq -r '.phases.fan_out.ran' "$RUN")
  if [ "$n" != "true" ]; then echo "  FAIL: phases.fan_out.ran is '$n'"; fails=1
  else echo "  ok: phase marked ran"; fi

  # 2. model identity and the independence flag
  bash "$SELF" --run "$RUN" model role=critic_stats role_class=critic round=1 \
       model_id=gpt-5.4 model_family=gpt provider=openai source=observed > /dev/null
  n=$(jq -r '.model_assignments[-1].differs_from_auditor_family' "$RUN")
  if [ "$n" != "true" ]; then echo "  FAIL: a gpt critic against a claude auditor gave differs=$n"; fails=1
  else echo "  ok: differs_from_auditor_family true for an external critic"; fi

  bash "$SELF" --run "$RUN" model role=auditor_reproduction role_class=auditor \
       model_id=claude-test model_family=claude provider=anthropic source=observed > /dev/null
  n=$(jq -r '.model_assignments[-1].differs_from_auditor_family' "$RUN")
  if [ "$n" != "false" ]; then echo "  FAIL: an auditor-family role gave differs=$n"; fails=1
  else echo "  ok: differs_from_auditor_family false for the auditor family"; fi

  # 3. the silent swap: a role whose model changes must record what it replaced
  bash "$SELF" --run "$RUN" model role=critic_stats role_class=critic round=2 \
       model_id=gpt-5.4-turbo model_family=gpt provider=openai source=observed intended=false > /dev/null
  n=$(jq -r '.model_assignments[-1].replaces_model_id' "$RUN")
  if [ "$n" != "gpt-5.4" ]; then echo "  FAIL: mid-run model change recorded replaces_model_id='$n'"; fails=1
  else echo "  ok: mid-run model change auto-records the model it replaced"; fi

  # 4. tokens: an unobtainable count stays null and is never invented
  bash "$SELF" --run "$RUN" tokens role=auditor_reproduction obtainable=false > /dev/null
  n=$(jq -r '[.tokens[-1] | .total_tokens, .input_tokens] | map(select(. == null)) | length' "$RUN")
  if [ "$n" != "2" ]; then echo "  FAIL: an unobtainable token record was filled in"; fails=1
  else echo "  ok: unobtainable tokens stay null"; fi
  bash "$SELF" --run "$RUN" tokens role=critic_stats round=1 input_tokens=100 output_tokens=20 total_tokens=120 source=cli_log > /dev/null
  n=$(jq -r '.tokens[-1].obtainable' "$RUN")
  if [ "$n" != "true" ]; then echo "  FAIL: a measured token record came out obtainable=$n"; fails=1
  else echo "  ok: measured tokens marked obtainable"; fi

  # 5. round results, including the derived unclassified count
  bash "$SELF" --run "$RUN" round_start round=1 parallelism=2 report_version_reviewed=REPORT_v1.md \
       critics_run=critic_stats,critic_data at=2026-07-14T14:00:00Z > /dev/null
  bash "$SELF" --run "$RUN" round_end round=1 at=2026-07-14T15:30:00Z > /dev/null
  bash "$SELF" --run "$RUN" round_result round=1 blocking_items=5 non_blocking_items=7 \
       comment_defects=3 paper_defects=1 integrated_at=2026-07-14T16:00:00Z character=substantive > /dev/null
  n=$(jq -r '.rounds[0] | "\(.wall_clock_seconds)/\(.unclassified_defects)/\(.critics_run|length)"' "$RUN")
  if [ "$n" != "5400/1/2" ]; then
    echo "  FAIL: round projection gave '$n', expected '5400/1/2'"; fails=1
  else echo "  ok: round wall clock 5400 s, unclassified derived as 1, critic list split"; fi

  # 6. brief corrections keep their own count
  bash "$SELF" --run "$RUN" brief_correction raised_by=auditor_data_integrity \
       field='data inventory' description='brief missed pkg/data_wave2/' accepted=true > /dev/null
  n=$(jq -r '"\(.brief_corrections.count)/\(.brief_corrections.items|length)"' "$RUN")
  if [ "$n" != "1/1" ]; then echo "  FAIL: brief_corrections count/items = '$n'"; fails=1
  else echo "  ok: brief_corrections count tracks its items"; fi

  # 7. a gate's exit status is stored as a status, not as a word
  bash "$SELF" --run "$RUN" gate gate=check_consistency round=1 exit_status=1 log_path=work/cc.log > /dev/null
  n=$(jq -r '.compliance.gates_run[-1] | "\(.exit_status)/\(.passed)"' "$RUN")
  if [ "$n" != "1/false" ]; then echo "  FAIL: gate record = '$n', expected '1/false'"; fails=1
  else echo "  ok: a failing gate is recorded as failing"; fi

  # 8. an apostrophe in free text must survive (prompt text broke this toolkit before)
  bash "$SELF" --run "$RUN" note text="the authors' own script isn't deterministic" > /dev/null
  rc=$?
  n=$(jq -r '.events[-1].payload.text' "$RUN")
  if [ "$rc" -ne 0 ] || [ "$n" != "the authors' own script isn't deterministic" ]; then
    echo "  FAIL: apostrophes in free text did not round-trip (rc $rc): '$n'"; fails=1
  else echo "  ok: apostrophes in free text round-trip"; fi

  # 9. CONCURRENCY. 40 writers at once, no lost events, no corruption, no duplicate sequence.
  local before after
  before=$(jq -r '.events | length' "$RUN")
  i=0
  while [ "$i" -lt 40 ]; do
    bash "$SELF" --run "$RUN" note text="concurrent $i" > /dev/null 2>&1 &
    i=$((i + 1))
  done
  wait
  jq empty "$RUN" 2>/dev/null
  if [ $? -ne 0 ]; then echo "  FAIL: run.json is CORRUPT after 40 concurrent writes"; fails=1
  else echo "  ok: run.json still parses after 40 concurrent writes"; fi
  after=$(jq -r '.events | length' "$RUN")
  if [ "$after" != "$((before + 40))" ]; then
    echo "  FAIL: expected $((before + 40)) events, found $after -- writes were lost"; fails=1
  else echo "  ok: all 40 concurrent events present (no lost updates)"; fi
  n=$(jq -r '[.events[].seq] | (length) as $l | (unique | length) as $u
             | if $l == $u then "unique" else "dup" end' "$RUN")
  if [ "$n" != "unique" ]; then echo "  FAIL: duplicate sequence numbers after concurrent writes"; fails=1
  else echo "  ok: sequence numbers unique under concurrency"; fi
  n=$(jq -r '[.events[].seq] | . == (sort) and (max == length)' "$RUN")
  if [ "$n" != "true" ]; then echo "  FAIL: sequence numbers are not contiguous and ordered"; fails=1
  else echo "  ok: sequence numbers contiguous and ordered"; fi
  n=$(wc -l < "$T/a/run.events.jsonl" | tr -d ' ')
  if [ "$n" != "$after" ]; then
    echo "  FAIL: journal has $n lines, run.json has $after events"; fails=1
  else echo "  ok: journal and run.json agree ($n entries)"; fi

  # 10. NEGATIVE CONTROLS -- each must refuse with exit 2 specifically
  bash "$SELF" --run "$RUN" not_a_type > /dev/null 2>&1
  if [ $? -ne 2 ]; then echo "  FAIL: an unknown event type was accepted"; fails=1
  else echo "  ok: unknown event type refused (exit 2)"; fi
  bash "$SELF" --run "$RUN" phase_start > /dev/null 2>&1
  if [ $? -ne 2 ]; then echo "  FAIL: phase_start without phase= was accepted"; fails=1
  else echo "  ok: missing required key refused (exit 2)"; fi
  bash "$SELF" --run "$RUN" phase_start phase=not_a_phase > /dev/null 2>&1
  if [ $? -ne 2 ]; then echo "  FAIL: an unknown phase name was accepted"; fails=1
  else echo "  ok: unknown phase name refused (exit 2)"; fi
  bash "$SELF" --run "$T/nope.json" note text=x > /dev/null 2>&1
  if [ $? -ne 2 ]; then echo "  FAIL: writing to a nonexistent run.json was accepted"; fails=1
  else echo "  ok: nonexistent run.json refused (exit 2)"; fi
  bash "$SELF" --run "$RUN" gate gate=stage_release > /dev/null 2>&1
  if [ $? -ne 2 ]; then echo "  FAIL: a gate event without exit_status was accepted"; fails=1
  else echo "  ok: gate without exit_status refused (exit 2)"; fi

  # 11. a stale lock left by a dead process must not wedge the run
  mkdir -p "$RUN.lock"; printf '999999\n' > "$RUN.lock/pid"; printf '%s\n' "$(( $(date +%s) - 60 ))" > "$RUN.lock/ts"
  bash "$SELF" --run "$RUN" note text="after stale lock" > /dev/null 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then echo "  FAIL: a stale lock from a dead pid blocked the write (rc $rc)"; fails=1
  else echo "  ok: stale lock from a dead pid broken automatically"; fi

  rm -rf "$T"
  if [ "$fails" -ne 0 ]; then echo "SELF-TEST FAILED"; return 1; fi
  echo "SELF-TEST PASSED"
  return 0
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi
main "$@"
exit $?
