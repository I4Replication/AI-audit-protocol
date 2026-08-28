#!/bin/bash
# init_run.sh <audit-dir> [options]        (or: init_run.sh --self-test)
#
# Called ONCE, at the very start of an audit, immediately after setup_workspace.sh.
# Writes <audit-dir>/run.json holding everything that is knowable at that moment:
# identifiers, the start timestamp, the protocol and gate-set versions in force, and the
# characteristics of the replication package AS RECEIVED.
#
# Why at the start and not at the end: none of this survives to be collected later. The package
# gets written into, the skill gets edited, the clock is gone. The first cohort of audits lost
# their auditor-phase cost data, five sixths of their token logs and six tenths of their working
# records precisely because collection was left until someone wanted the numbers.
#
# Options:
#   --audit-id ID           default: <yyyymmdd>-<basename of audit dir>-<8 hex of the dir path>
#   --doi DOI               bare 10.xxxx/yyyy form; omit only if the paper truly has none
#   --paper-title TITLE
#   --round-target N        required production rounds; default 2
#   --round-cap N           hard safety cap; default 5
#   --auditor-model ID      coordinator/Phase-1 model; must match policy (env AUDITOR_MODEL)
#   --auditor-family F      e.g. claude                          (env AUDITOR_FAMILY)
#   --auditor-provider P    e.g. anthropic                       (env AUDITOR_PROVIDER)
#   --protocol-version V    (env PROTOCOL_VERSION)   default: derived from the skill directory
#   --gate-set-version V    (env GATE_SET_VERSION)   default: derived from scripts/*.sh
#   --skill-dir DIR         (env SKILL_DIR)          default: the i4r-audit skill
#   --pkg-dir DIR           default: <audit-dir>/pkg
#   --force                 overwrite an existing run.json (refuses by default)
#   --self-test             run the built-in tests in a scratch directory and exit
#
# Exit status: 0 on success, non-zero on any failure. Every step's status is checked
# explicitly -- `&& echo OK` has swallowed failures in this toolkit before.
set -uo pipefail

SCHEMA_VERSION="2.0.0"
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
DEFAULT_SKILL_DIR="${SKILL_DIR:-$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd)}"

# Absolute path to this script. The self-test re-invokes it, and "$0" alone is whatever the
# caller typed -- a bare name resolves to nothing and every re-invocation exits 127, which a
# nonzero-means-refused assertion happily reads as a pass.
SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"

# Exit 2 means "this script refused", as distinct from 127 (not found), 126 (not executable) or
# 1 (a test failed). The negative controls assert 2 exactly, so a broken invocation cannot
# masquerade as a successful refusal.
die () { echo "FATAL: $*" >&2; exit 2; }
now_utc () { date -u +%Y-%m-%dT%H:%M:%SZ; }

need_jq () {
  command -v jq >/dev/null
  if [ $? -ne 0 ]; then die "jq is required but not on PATH"; fi
  command -v python3 >/dev/null
  if [ $? -ne 0 ]; then die "python3 is required but not on PATH"; fi
}

# ---------------------------------------------------------------- package scan
# Done in python rather than find/stat/awk: the histogram, the byte total and the manifest hash
# all have to agree with each other, and one pass that emits JSON cannot disagree with itself.
scan_package () {
  python3 - "$1" <<'PY'
import hashlib, json, pathlib, sys
pkg = pathlib.Path(sys.argv[1])
files, hist, total, lines = [], {}, 0, []
cached = pkg.parent / "package_manifest.json"
if cached.is_file():
    try:
        c = json.loads(cached.read_text())
        for item in c.get("files", []):
            rel, size, digest = item["path"], int(item["size"]), item["sha256"]
            ext = pathlib.Path(rel).suffix.lower().lstrip(".")
            files.append(rel); total += size
            hist[ext] = hist.get(ext, {"count": 0, "bytes": 0})
            hist[ext]["count"] += 1; hist[ext]["bytes"] += size
            lines.append(digest + "  " + rel)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        files, hist, total, lines = [], {}, 0, []
if not files and pkg.is_dir():
    for p in sorted(pkg.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(pkg).as_posix()
        try:
            b = p.read_bytes()
        except OSError:
            continue
        total += len(b)
        ext = p.suffix.lower().lstrip(".")
        hist[ext] = hist.get(ext, {"count": 0, "bytes": 0})
        hist[ext]["count"] += 1
        hist[ext]["bytes"] += len(b)
        lines.append(hashlib.sha256(b).hexdigest() + "  " + rel)
        files.append(rel)
CODE = (".r", ".do", ".py", ".qmd", ".rmd", ".sps", ".ipynb", ".m", ".jl", ".sas")
formats = [{"extension": k, "count": v["count"], "bytes": v["bytes"]}
           for k, v in sorted(hist.items(), key=lambda kv: (-kv[1]["count"], kv[0]))]
manifest = "\n".join(lines)
print(json.dumps({
    "file_count": len(files),
    "total_bytes": total,
    "formats": formats,
    "analysis_code_files": sum(1 for f in files if pathlib.Path(f).suffix.lower() in CODE),
    "manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest() if lines else None,
    "_manifest_text": manifest,
}))
PY
}

sha8_of_file () {
  if [ -f "$1" ]; then shasum -a 256 "$1" 2>/dev/null | cut -c1-8; else echo ""; fi
}

derive_protocol_version () {
  local d="$1"
  if [ -n "${PROTOCOL_VERSION:-}" ]; then echo "$PROTOCOL_VERSION"; return 0; fi
  if [ -f "$d/VERSION" ]; then head -1 "$d/VERSION" | tr -d ' \t\r\n'; return 0; fi
  local h; h=$(sha8_of_file "$d/SKILL.md")
  if [ -n "$h" ]; then echo "unversioned-skill-$h"; else echo "unknown"; fi
}

derive_gate_set_version () {
  local d="$1"
  if [ -n "${GATE_SET_VERSION:-}" ]; then echo "$GATE_SET_VERSION"; return 0; fi
  if [ -d "$d/scripts" ]; then
    local h
    h=$(cat $(ls "$d"/scripts/*.sh 2>/dev/null | sort) 2>/dev/null | shasum -a 256 | cut -c1-8)
    if [ -n "$h" ]; then echo "gates-$h"; return 0; fi
  fi
  echo "unknown"
}

# ---------------------------------------------------------------------- main
main () {
  need_jq
  DIR=""; AUDIT_ID=""; DOI=""; TITLE=""; TARGET=2; CAP=5; FORCE=0
  AM="${AUDITOR_MODEL:-}"; AF="${AUDITOR_FAMILY:-}"; AP="${AUDITOR_PROVIDER:-}"
  PV=""; GV=""; SKILLD="$DEFAULT_SKILL_DIR"; PKGD=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --audit-id) AUDIT_ID="${2:-}"; shift 2 ;;
      --doi) DOI="${2:-}"; shift 2 ;;
      --paper-title) TITLE="${2:-}"; shift 2 ;;
      --round-target) TARGET="${2:-}"; shift 2 ;;
      --round-cap) CAP="${2:-}"; shift 2 ;;
      --auditor-model) AM="${2:-}"; shift 2 ;;
      --auditor-family) AF="${2:-}"; shift 2 ;;
      --auditor-provider) AP="${2:-}"; shift 2 ;;
      --protocol-version) PV="${2:-}"; shift 2 ;;
      --gate-set-version) GV="${2:-}"; shift 2 ;;
      --skill-dir) SKILLD="${2:-}"; shift 2 ;;
      --pkg-dir) PKGD="${2:-}"; shift 2 ;;
      --force) FORCE=1; shift ;;
      -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
      -*) die "unknown option $1" ;;
      *) if [ -z "$DIR" ]; then DIR="$1"; else die "unexpected argument $1"; fi; shift ;;
    esac
  done
  [ -n "$DIR" ] || die "usage: init_run.sh <audit-dir> [options]  (--help for the list)"
  [ -d "$DIR" ] || die "$DIR is not a directory -- run setup_workspace.sh first"
  case "$TARGET" in ''|*[!0-9]*) die "round target must be a positive integer" ;; esac
  case "$CAP" in ''|*[!0-9]*) die "round cap must be a positive integer" ;; esac
  [ "$TARGET" -ge 1 ] && [ "$CAP" -ge "$TARGET" ] || die "require 1 <= round-target <= round-cap"

  RUN="$DIR/run.json"
  if [ -f "$RUN" ] && [ "$FORCE" -ne 1 ]; then
    die "$RUN already exists. init_run.sh runs ONCE per audit; re-running would discard the
       journal of everything logged so far. Pass --force only if you mean to start over."
  fi

  if [ "$FORCE" -eq 1 ] && [ -e "$DIR/pipeline-policy.snapshot.json" ]; then
    [ ! -L "$DIR/pipeline-policy.snapshot.json" ] || die "refusing to archive a symlinked policy snapshot"
    mv "$DIR/pipeline-policy.snapshot.json" \
       "$DIR/pipeline-policy.snapshot.previous.$(date -u +%Y%m%dT%H%M%SZ).json" ||
      die "could not archive the prior policy snapshot for forced reinitialization"
  fi

  [ -n "$PKGD" ] || PKGD="$DIR/pkg"
  python3 "$SCRIPT_DIR/record_invocation.py" --init-policy "$DIR" >/dev/null ||
    die "could not create the immutable audit-scoped pipeline policy snapshot"
  POLICY_TARGET=$(jq -r '.policy.critique.round_target // empty' "$DIR/pipeline-policy.snapshot.json")
  POLICY_CAP=$(jq -r '.policy.critique.round_cap // empty' "$DIR/pipeline-policy.snapshot.json")
  [ -n "$POLICY_TARGET" ] && [ -n "$POLICY_CAP" ] ||
    die "audit-scoped policy snapshot lacks critique round target/cap"
  [ "$TARGET" -eq "$POLICY_TARGET" ] && [ "$CAP" -eq "$POLICY_CAP" ] ||
    die "canonical accuracy-safe policy requires round target $POLICY_TARGET and hard cap $POLICY_CAP"
  POLICY_COORD_MODEL=$(jq -r '.policy.models.coordinator.requested_model // empty' "$DIR/pipeline-policy.snapshot.json")
  POLICY_COORD_PROVIDER=$(jq -r '.policy.models.coordinator.provider // empty' "$DIR/pipeline-policy.snapshot.json")
  POLICY_COORD_EFFORT=$(jq -r '.policy.models.coordinator.reasoning_effort // empty' "$DIR/pipeline-policy.snapshot.json")
  [ -n "$POLICY_COORD_MODEL" ] && [ -n "$POLICY_COORD_PROVIDER" ] && [ -n "$POLICY_COORD_EFFORT" ] ||
    die "audit-scoped policy snapshot lacks the coordinator model/provider/effort"
  case "$POLICY_COORD_PROVIDER" in
    anthropic) POLICY_COORD_FAMILY=claude ;;
    openai) POLICY_COORD_FAMILY=gpt ;;
    *) POLICY_COORD_FAMILY="$POLICY_COORD_PROVIDER" ;;
  esac
  [ -n "$AM" ] || AM="$POLICY_COORD_MODEL"
  [ -n "$AP" ] || AP="$POLICY_COORD_PROVIDER"
  [ -n "$AF" ] || AF="$POLICY_COORD_FAMILY"
  [ "$AM" = "$POLICY_COORD_MODEL" ] ||
    die "canonical baseline requires coordinator and Phase-1 model $POLICY_COORD_MODEL (got $AM)"
  [ "$AP" = "$POLICY_COORD_PROVIDER" ] ||
    die "canonical baseline requires provider $POLICY_COORD_PROVIDER (got $AP)"
  [ "$AF" = "$POLICY_COORD_FAMILY" ] ||
    die "canonical baseline requires model family $POLICY_COORD_FAMILY (got $AF)"
  POLICY_SHA=$(shasum -a 256 "$DIR/pipeline-policy.snapshot.json" | cut -d' ' -f1)
  [ ${#POLICY_SHA} -eq 64 ] || die "could not hash the audit-scoped pipeline policy snapshot"
  [ -n "$PV" ] || PV=$(derive_protocol_version "$SKILLD")
  [ -n "$GV" ] || GV=$(derive_gate_set_version "$SKILLD")
  START=$(now_utc)
  if [ -z "$AUDIT_ID" ]; then
    h=$(printf '%s' "$(cd "$DIR" && pwd)" | shasum -a 256 | cut -c1-8)
    AUDIT_ID="$(date -u +%Y%m%d)-$(basename "$DIR")-$h"
  fi

  PKGJSON=$(scan_package "$PKGD")
  if [ $? -ne 0 ] || [ -z "$PKGJSON" ]; then die "package scan of $PKGD failed"; fi
  MANIFEST=$(printf '%s' "$PKGJSON" | jq -r '._manifest_text')
  if [ $? -ne 0 ]; then die "could not read the package manifest out of the scan"; fi
  mkdir -p "$DIR/work"
  if [ $? -ne 0 ]; then die "could not create $DIR/work"; fi
  printf '%s\n' "$MANIFEST" > "$DIR/work/pkg_manifest.txt"
  if [ $? -ne 0 ]; then die "could not write $DIR/work/pkg_manifest.txt"; fi
  PKGJSON=$(printf '%s' "$PKGJSON" | jq 'del(._manifest_text)
              | . + {source_url:null, retrieved_at:null, file_count_at_close:null,
                     manifest_sha256_at_close:null, pkg_unchanged:null,
                     runs_as_shipped:null, runs_as_shipped_notes:null}')
  if [ $? -ne 0 ]; then die "could not assemble the package block"; fi

  DOI_J=null; [ -n "$DOI" ] && DOI_J=$(printf '%s' "$DOI" | jq -R .)
  TITLE_J=null; [ -n "$TITLE" ] && TITLE_J=$(printf '%s' "$TITLE" | jq -R .)

  TMP="$RUN.tmp.$$"
  jq -n \
    --arg sv "$SCHEMA_VERSION" --arg id "$AUDIT_ID" --argjson doi "$DOI_J" \
    --argjson title "$TITLE_J" --arg pv "$PV" --arg gv "$GV" --arg start "$START" \
    --arg am "$AM" --arg af "$AF" --arg ap "$AP" --arg ae "$POLICY_COORD_EFFORT" \
    --argjson target "$TARGET" --argjson cap "$CAP" \
    --arg skill "$(basename "$SKILLD")" --arg policysha "$POLICY_SHA" \
    --arg skillsha "$(cat $(ls "$SKILLD"/scripts/*.sh 2>/dev/null | sort) 2>/dev/null | shasum -a 256 | cut -d' ' -f1)" \
    --arg os "$(uname -sr)" --arg bash "${BASH_VERSION:-unknown}" \
    --argjson pkg "$PKGJSON" '
    {
      schema_version: $sv,
      audit_id: $id,
      paper_doi: $doi,
      paper_title: $title,
      protocol_version: $pv,
      gate_set_version: $gv,
      safe_to_publish: false,
      publication_redaction: {
        fields_requiring_removal: ["/audit_dir","/toolkit/skill_path","/costs",
          "/auditor_model/provider","/model_assignments","/failure_events",
          "/compliance/violations","/delivery/recipient"],
        safe_aggregates: ["/phases","/rounds","/rounds_run","/stopping_reason",
          "/brief_corrections/count","/compliance/compliance_pass",
          "/token_accounting/coverage_fraction","/package"],
        note: "Internal registry record. Before any release: drop local paths, costs and provider names, code model ids down to families, and report compliance violations only as aggregate counts.",
        cleared_by: null
      },
      registry: null,
      toolkit: { skill_path: $skill, skill_commit: null,
                 scripts_sha256: (if $skillsha == "" then null else $skillsha end),
                 pipeline_policy_snapshot_sha256: $policysha,
                 host_os: $os, bash_version: $bash },
      # The run record already lives at the audit root, so an absolute
      # workstation path adds no reproducibility while leaking an account
      # name into internal copies.  Finalization resolves retained artifacts
      # relative to run.json when this field is null.
      audit_dir: null,
      started_at: $start,
      ended_at: null,
      wall_clock_seconds: null,
      active_seconds: null,
      status: "initialized",
      coordinator_model: { configured_model: $am, served_model: null, family: $af,
                           provider: $ap, effort: $ae, observed_from: null, declared_at: $start },
      auditor_model: { model_id: $am, family: $af, provider: $ap, declared_at: $start },
      phases: ( ["intake","fan_out","adjudication","critique","release"]
                | map({key: ., value: {ran:false, started_at:null, ended_at:null,
                                       wall_clock_seconds:null, notes:null}})
                | from_entries ),
      human_effort: {
        self_reported: true, instrumented: false,
        person_minutes_by_phase: { intake: null, fan_out: null, adjudication: null,
                                   critique: null, release: null },
        person_minutes_total: null, phases_recorded: null, people_involved: null,
        notes: "Self-reported, not instrumented. Log with: log_event.sh human_effort phase=<p> person_minutes=<n>"
      },
      settled_facts: { used: null, generated_after_round: null, path: null, entries: null,
                       spot_check_failures: null,
                       notes: "Generate after round 1; promotion requires two independent confirmations." },
      model_assignments: [],
      invocations: [],
      tokens: [],
      costs: [],
      token_accounting: null,
      rounds: [],
      rounds_run: null,
      round_target: $target,
      round_cap: $cap,
      stopping_reason: null,
      stopping_reason_detail: null,
      unreviewed_sections: [],
      changed_claim_ids: [],
      rereviewed_claim_ids: [],
      unresolved_blocking_item_ids: [],
      unresolved_conclusion_critical_item_ids: [],
      compliance: null,
      brief_corrections: { count: 0, items: [] },
      failure_events: [],
      records_retained: [],
      records_retained_checked_at: null,
      package: $pkg,
      delivery: null,
      notes: null,
      events: [ { seq: 1, at: $start, type: "init",
                  payload: { audit_id: $id, protocol_version: $pv, gate_set_version: $gv,
                             pipeline_policy_snapshot_sha256: $policysha,
                             package_file_count: $pkg.file_count },
                  pid: null, source: "init_run.sh" } ]
    }
    | .phases.intake = {ran:false, started_at:$start, ended_at:null,
                        wall_clock_seconds:null, notes:"opened by init_run.sh"}
    ' > "$TMP"
  if [ $? -ne 0 ]; then rm -f "$TMP"; die "jq failed to assemble run.json"; fi
  jq empty "$TMP"
  if [ $? -ne 0 ]; then rm -f "$TMP"; die "assembled run.json is not valid JSON"; fi
  mv -f "$TMP" "$RUN"
  if [ $? -ne 0 ]; then rm -f "$TMP"; die "could not move the new run.json into place"; fi

  # The journal mirrors every event. If run.json is ever lost or corrupted, this file is what the
  # projections are rebuilt from, so it is written separately and only ever appended to.
  if [ "$FORCE" -eq 1 ] && [ -s "$DIR/run.events.jsonl" ]; then
    mv "$DIR/run.events.jsonl" "$DIR/run.events.previous.$(date -u +%Y%m%dT%H%M%SZ).jsonl"
  fi
  jq -c '.events[0]' "$RUN" > "$DIR/run.events.jsonl"
  if [ $? -ne 0 ]; then die "could not append the init event to the journal"; fi
  : > "$DIR/invocations.jsonl"
  if [ $? -ne 0 ]; then die "could not initialize the invocation telemetry ledger"; fi

  echo "=== run.json initialised ==="
  echo "  audit_id:          $AUDIT_ID"
  echo "  paper_doi:         ${DOI:-<none given -- set it before finalize>}"
  echo "  protocol_version:  $PV"
  echo "  gate_set_version:  $GV"
  echo "  auditor model:     $AM ($AF / $AP)"
  echo "  critique rounds:   target $TARGET, hard cap $CAP"
  echo "  package:           $(printf '%s' "$PKGJSON" | jq -r '.file_count') files, $(printf '%s' "$PKGJSON" | jq -r '.total_bytes') bytes"
  echo "  package manifest:  work/pkg_manifest.txt (sha256 $(printf '%s' "$PKGJSON" | jq -r '.manifest_sha256 // "n/a"' | cut -c1-16)...)"
  echo "  written:           $RUN"
  echo ""
  echo "NEXT: export RUN_JSON=$RUN and log every phase boundary, model, failure and round result"
  echo "      with log_event.sh as they happen. Nothing here can be reconstructed afterwards."
  return 0
}

# ------------------------------------------------------------------ self-test
self_test () {
  need_jq
  local T rc fails
  T=$(mktemp -d "${TMPDIR:-/tmp}/init_run_selftest.XXXXXX")
  if [ $? -ne 0 ]; then die "could not make a scratch directory"; fi
  fails=0
  echo "== init_run.sh self-test in $T =="

  mkdir -p "$T/a/pkg/sub"
  printf 'x,y\n1,2\n' > "$T/a/pkg/data.csv"
  printf 'print(1)\n'  > "$T/a/pkg/analysis.R"
  printf 'hello\n'     > "$T/a/pkg/sub/README.md"

  # 1. a normal init succeeds and records the package correctly
  "$SELF" "$T/a" --audit-id t-1 --doi 10.1234/abc --round-cap 5 \
       --auditor-model sonnet-5 --auditor-family claude --auditor-provider anthropic \
       --protocol-version p1 --gate-set-version g1 > "$T/out1.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then echo "  FAIL: init exited $rc"; cat "$T/out1.txt"; fails=1
  else echo "  ok: init exited 0"; fi

  jq empty "$T/a/run.json" 2>/dev/null
  if [ $? -ne 0 ]; then echo "  FAIL: run.json is not valid JSON"; fails=1
  else echo "  ok: run.json parses"; fi

  local n
  n=$(jq -r '.package.file_count' "$T/a/run.json" 2>/dev/null)
  if [ "$n" != "3" ]; then echo "  FAIL: package.file_count is '$n', expected 3"; fails=1
  else echo "  ok: package.file_count 3"; fi

  n=$(jq -r '.package.analysis_code_files' "$T/a/run.json" 2>/dev/null)
  if [ "$n" != "1" ]; then echo "  FAIL: analysis_code_files is '$n', expected 1"; fails=1
  else echo "  ok: analysis_code_files 1"; fi

  n=$(jq -r '[.schema_version,.audit_id,.paper_doi,.protocol_version,.gate_set_version,
              .started_at,.status,.auditor_model.model_id]|map(select(.==null))|length' "$T/a/run.json")
  if [ "$n" != "0" ]; then echo "  FAIL: $n required field(s) came out null"; fails=1
  else echo "  ok: every field knowable at init is populated"; fi

  n=$(jq -r '.safe_to_publish' "$T/a/run.json")
  if [ "$n" != "false" ]; then echo "  FAIL: safe_to_publish defaulted to '$n', must be false"; fails=1
  else echo "  ok: safe_to_publish defaults false"; fi

  n=$(jq -r '.phases|keys|length' "$T/a/run.json")
  if [ "$n" != "5" ]; then echo "  FAIL: expected 5 phase keys, got $n"; fails=1
  else echo "  ok: all five phases present and marked not-yet-run"; fi

  if [ ! -s "$T/a/work/pkg_manifest.txt" ]; then echo "  FAIL: no package manifest written"; fails=1
  else echo "  ok: package manifest written ($(wc -l < "$T/a/work/pkg_manifest.txt" | tr -d ' ') entries)"; fi

  if [ ! -s "$T/a/run.events.jsonl" ]; then echo "  FAIL: journal not started"; fails=1
  else echo "  ok: event journal started"; fi

  # 2. NEGATIVE CONTROL: a second init must refuse rather than discard the journal.
  # Assert exit 2 exactly, not merely nonzero: while this test was being written the re-invocation
  # was exiting 127 (command not found) and "nonzero means it refused" passed it clean.
  "$SELF" "$T/a" --audit-id t-1 > "$T/out2.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ]; then echo "  FAIL: re-init should refuse with exit 2, got $rc"; cat "$T/out2.txt"; fails=1
  else echo "  ok: re-init refused with the refusal code (exit 2)"; fi

  # 3. --force must be able to override
  "$SELF" "$T/a" --audit-id t-1 --force > "$T/out3.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then echo "  FAIL: --force re-init exited $rc"; fails=1
  else echo "  ok: --force re-init allowed"; fi

  # 4. NEGATIVE CONTROL: a missing audit directory must fail
  "$SELF" "$T/does_not_exist" > "$T/out4.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ]; then echo "  FAIL: missing directory should refuse with exit 2, got $rc"; fails=1
  else echo "  ok: missing audit directory rejected with the refusal code (exit 2)"; fi

  # 5. an empty package must still produce a valid file (no package is itself a finding)
  mkdir -p "$T/b/pkg"
  "$SELF" "$T/b" --audit-id t-2 > "$T/out5.txt" 2>&1
  rc=$?
  n=$(jq -r '.package.file_count' "$T/b/run.json" 2>/dev/null)
  if [ "$rc" -ne 0 ] || [ "$n" != "0" ]; then
    echo "  FAIL: empty package: exit $rc, file_count '$n'"; fails=1
  else echo "  ok: empty package handled (file_count 0)"; fi

  # 6. Live v2 runs may not weaken the immutable accuracy-safe target or cap.
  mkdir -p "$T/c/pkg" "$T/d/pkg"
  "$SELF" "$T/c" --audit-id t-target --round-target 1 > "$T/out6.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ] || [ -e "$T/c/run.json" ]; then
    echo "  FAIL: noncanonical round target should refuse with exit 2 before run creation (got $rc)"; fails=1
  else echo "  ok: noncanonical round target is rejected by the policy snapshot"; fi
  "$SELF" "$T/d" --audit-id t-cap --round-cap 4 > "$T/out7.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ] || [ -e "$T/d/run.json" ]; then
    echo "  FAIL: noncanonical round cap should refuse with exit 2 before run creation (got $rc)"; fails=1
  else echo "  ok: noncanonical round cap is rejected by the policy snapshot"; fi

  # 7. Baseline identity is policy, not caller preference: Opus may not be substituted.
  mkdir -p "$T/e/pkg"
  "$SELF" "$T/e" --audit-id t-model --auditor-model opus-5 \
       --auditor-family claude --auditor-provider anthropic > "$T/out8.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ] || [ -e "$T/e/run.json" ]; then
    echo "  FAIL: noncanonical Opus baseline should refuse with exit 2 before run creation (got $rc)"; fails=1
  else echo "  ok: Opus cannot replace the Sonnet coordinator/Phase-1 baseline"; fi

  rm -rf "$T"
  if [ "$fails" -ne 0 ]; then echo "SELF-TEST FAILED"; return 1; fi
  echo "SELF-TEST PASSED"
  return 0
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi
main "$@"
rc=$?
# record the machine state the audit will actually run on (team requirement 2026-08-24)
if [ $rc -eq 0 ] && [ -n "${DIR:-}" ] && [ -f "$DIR/run.json" ]; then
  bash "$(cd "$(dirname "$0")" && pwd)/capture_environment.sh" "$DIR" || echo "WARNING: environment capture failed — record it manually before finalize"
fi
exit $rc
