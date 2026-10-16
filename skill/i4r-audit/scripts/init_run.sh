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
#   --round-cap N           hard safety cap; default: the policy snapshot's critique.round_cap (7 from
#                           2.6.9). Any other value is refused. An existing audit keeps the cap frozen in its
#                           snapshot; scripts/amend_round_cap.py raises it with a journaled reason.
#   --auditor-model ID      coordinator/Phase-1 model; must equal the policy's coordinator requested_model
#                           (sonnet-5-5 since 2.7.0), which is also the default (env AUDITOR_MODEL)
#   --auditor-family F      e.g. claude                          (env AUDITOR_FAMILY)
#   --auditor-provider P    e.g. anthropic                       (env AUDITOR_PROVIDER)
#   --protocol-version V    (env PROTOCOL_VERSION)   default: derived from the skill directory
#   --gate-set-version V    (env GATE_SET_VERSION)   default: digest of every gate file (scripts/*.sh,
#                           scripts/*.py, schemas/**, config/, prompts/) -- see gate_files_digest
#   --skill-dir DIR         (env SKILL_DIR)          default: the i4r-audit skill. Its VERSION must
#                           match <tree>/skill/i4r-audit-unified/i4r-audit/VERSION when that file
#                           exists (env I4R_CANONICAL_SKILL_DIR overrides the location;
#                           I4R_ALLOW_SKILL_VERSION_MISMATCH=1 downgrades the refusal to a warning)
#   --pkg-dir DIR           default: <audit-dir>/pkg
#   --force                 overwrite an existing run.json (refuses by default). The policy snapshot is
#                           archived and frozen again from the shipped policy, so a forced re-init moves an
#                           audit initialised earlier to Sonnet 5.5 (2.7.0), the 350,000 probe bound (2.7.1),
#                           the 5,400 s Phase-1 timeout (2.7.2) and Claude effort xhigh (2.7.3).
#                           Pass its --audit-id, --doi and --paper-title again: run.json is rebuilt from them
#   --self-test             run the built-in tests in a scratch directory and exit
#
# Every refusal is decided before pipeline-policy.snapshot.json is frozen, and a snapshot this run created is
# removed again if it stops before run.json binds it (a forced re-init puts the archived one back). An audit
# with a journal, Phase-1 work under phase1/ or an invocation ledger is initialised even when run.json, a
# projection, is gone: it is refused with the rebuild command. A snapshot with none of these beside it is left
# over from an init that did not finish, perhaps by an older skill: it is reused only when its Claude routes are
# the shipped policy's, and otherwise refused with the file to remove -- never rewritten. Init prints the Claude
# effort and the probe bound the snapshot froze, and warns when a reused bound is not the shipped one. One init at a time
# runs per audit directory: from its run.json check to its journal it holds <audit-dir>/.init.lock (an fcntl
# lock, removed on release), and a second init waits for it, saying so.
#
# Exit status: 0 on success, non-zero on any failure. Every step's status is checked
# explicitly -- `&& echo OK` has swallowed failures in this toolkit before.
set -uo pipefail

SCHEMA_VERSION="2.1.0"
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
DEFAULT_SKILL_DIR="${SKILL_DIR:-$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd)}"
. "$SCRIPT_DIR/tree_layout.sh"

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
# The discipline tree carries the canonical skill at <tree>/skill/i4r-audit-unified/i4r-audit
# and every machine syncs it, yet audit A28 was run from a copy three minor
# versions behind (2.2.0 against 2.4.10) and another from a copy with no VERSION at all. Each
# recorded its own stale stamp faithfully and then hit defects fixed months earlier. When the
# tree the audit sits in holds a canonical VERSION, the running copy must match it.
canonical_skill_version () {
  local audit_dir="$1" resolved tree c
  resolved="$(cd "$audit_dir" 2>/dev/null && pwd)" || return 1
  tree="$(i4r_root_of_audit "$resolved")"
  c="${I4R_CANONICAL_SKILL_DIR:-$tree/skill/i4r-audit-unified/i4r-audit}"
  [ -f "$c/VERSION" ] || return 1
  printf '%s\t%s' "$(head -1 "$c/VERSION" | tr -d ' \t\r\n')" "$c"
}
check_skill_version_pin () {
  local audit_dir="$1" skill_dir="$2" pin canonical where own
  pin=$(canonical_skill_version "$audit_dir") || return 0
  canonical="${pin%%	*}"; where="${pin#*	}"
  [ -n "$canonical" ] || return 0
  own=""; [ -f "$skill_dir/VERSION" ] && own=$(head -1 "$skill_dir/VERSION" | tr -d ' \t\r\n')
  if [ "$own" = "$canonical" ]; then return 0; fi
  if [ "${I4R_ALLOW_SKILL_VERSION_MISMATCH:-0}" = "1" ]; then
    echo "WARNING: running skill version '${own:-unversioned}' differs from the tree's canonical $canonical ($where); allowed by I4R_ALLOW_SKILL_VERSION_MISMATCH=1" >&2
    return 0
  fi
  echo "FATAL: init_run.sh: this skill copy is version '${own:-unversioned}' ($skill_dir)" >&2
  echo "       but the discipline tree's canonical skill is $canonical ($where)." >&2
  echo "       Run the scripts from the canonical copy, or reinstall it (its ZIP sits beside it)." >&2
  echo "       To proceed anyway, set I4R_ALLOW_SKILL_VERSION_MISMATCH=1; the mismatch is then" >&2
  echo "       recorded in protocol_version, not hidden." >&2
  return 1
}
# One digest over every file that decides whether a release passes, used by BOTH provenance
# stamps (gate_set_version and toolkit.scripts_sha256). They used to be two different glob lists
# -- one widened to the Python gates, one still hashing scripts/*.sh only -- and both fed the
# file list through an UNQUOTED $(ls ...), which word-splits on spaces. Every path in this tree
# has spaces, so `cat` got fragments, found nothing, and both stamps came out as sha256("") =
# e3b0c442... on every audit ever initialised here: a plausible-looking constant that fixed
# nothing. The list is read line-safe, and an empty stream is refused rather than hashed.
gate_files_digest() {
  local d="$1" f stream
  stream=$( for f in "$d"/scripts/*.sh "$d"/scripts/*.py "$d"/schemas/*.json \
                     "$d"/schemas/v2/*.json "$d"/schemas/v3/*.json "$d"/schemas/tools/*.py \
                     "$d"/config/check_policy_drift.py "$d"/config/pipeline-policy-v2.json \
                     "$d"/prompts/*.txt; do
              [ -f "$f" ] && printf '%s\n' "$f"
            done | LC_ALL=C sort | while IFS= read -r f; do cat "$f"; done )
  [ -n "$stream" ] || { echo "FATAL: no gate files found under $d; refusing to stamp a digest of nothing" >&2; return 1; }
  printf '%s' "$stream" | shasum -a 256 | cut -d' ' -f1
}


derive_gate_set_version () {
  local d="$1"
  if [ -n "${GATE_SET_VERSION:-}" ]; then echo "$GATE_SET_VERSION"; return 0; fi
  if [ -d "$d/scripts" ]; then
    local h
    # The gates are not only shell. Roughly thirty-five Python gates -- run_post_delta_gates.py,
    # validate_v2.py, check_release_privacy.py and the rest -- decide whether a release passes,
    # and hashing only *.sh left the stamp unchanged when any of them changed. Hash both, and the
    # provider-facing schemas too, since a schema change alters what a gate accepts.
    h=$(gate_files_digest "$d" | cut -c1-8)
    if [ -n "$h" ]; then echo "gates-$h"; return 0; fi
  fi
  echo "unknown"
}

# ------------------------------------------------------------- the policy to freeze
# Until 2.7.0 init froze the snapshot first and refused afterwards (the model, provider and family checks,
# and the skill-version pin), so a refused init left a snapshot that the next init in the directory reused
# without a word: a teammate refused for running an older skill had already frozen that skill's policy --
# its Claude route included -- into what the operator then started as a new audit. This reads, writing
# nothing, the policy this init would freeze: an existing snapshot without --force, else the shipped one.
# It prints round_target, round_cap and the coordinator's model, provider and effort, one per line, or
# refuses (exit 2) a leftover snapshot that is invalid or whose Claude routes are not the shipped policy's.
policy_to_freeze () {
  python3 - "$SCRIPT_DIR" "$1" "$2" <<'PY'
import pathlib, shlex, sys
scripts, audit, force = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3] == "1"
sys.path.insert(0, str(scripts))
import claude_routes
import probe_bounds
import phase1_timeouts
import critique_timeouts
import claim_change_rules
import record_invocation


def refuse(message):
    print(f"FATAL: {message}", file=sys.stderr)
    sys.exit(2)


def section(node, key):
    value = node.get(key) if isinstance(node, dict) else None
    return value if isinstance(value, dict) else {}


def claude_routes_of(policy):
    """Every Claude role's route (claude_routes.ROUTE_KEYS only); a role the policy lacks is None."""
    models = section(policy, "models")
    roles = [("coordinator", models.get("coordinator"))]
    roles += sorted(section(models, "phase_1_auditors").items())
    roles += [("post_fan_out_verifier", models.get("post_fan_out_verifier"))]
    return {role: claude_routes.project(entry) if isinstance(entry, dict) else None for role, entry in roles}


def described(route):
    return claude_routes.describe(route) if route else "no route"


def critic_route_of(policy):
    """2.7.5: the critic route a snapshot freezes: critics, targeted adjudicator and critique probe, model and effort."""
    models, critique = section(policy, "models"), section(policy, "critique")
    return [(entry.get("requested_model"), entry.get("reasoning_effort")) for entry in
            (section(models, "critics"), section(models, "targeted_adjudicator"), section(critique, "model_probe"))]


try:
    shipped = record_invocation.validate_json_schema.strict_loads(
        record_invocation.POLICY.read_text(encoding="utf-8"), source=str(record_invocation.POLICY))
except (OSError, ValueError) as exc:
    refuse(f"cannot read the shipped pipeline policy: {exc}")
snapshot = audit / record_invocation.POLICY_SNAPSHOT
remedy = (f"There is no run.json and no journal, so it is left over from an init that did not finish. init_run.sh "
          f"never reuses or rewrites such a file: remove it (rm {shlex.quote(str(snapshot))}) and re-run "
          f"this command, which freezes the shipped policy.")
policy = shipped
if not force and (snapshot.exists() or snapshot.is_symlink()):
    try:
        policy = record_invocation.load_policy_snapshot(audit)
    except ValueError as exc:
        refuse(f"{exc}. {remedy}")
    theirs, ours = claude_routes_of(policy), claude_routes_of(shipped)
    if theirs != ours:
        role = next(role for role in list(ours) + list(theirs) if theirs.get(role) != ours.get(role))
        # 2.7.3: say which kind of leftover it is. A live route (Sonnet 5, or Sonnet 5.5 at max) is an older
        # skill's; any other (sonnet-5-5 at high, sonnet-5 at xhigh, ...) is one no runner dispatches, so
        # Phase 1, the coordinator and the verifier would refuse the audit before any call.
        if theirs.get(role) is not None and claude_routes.live_route(theirs.get(role)) is None:
            why = ("No runner dispatches that route (the live routes: "
                   + "; ".join(claude_routes.describe(route) for route in claude_routes.LIVE_ROUTES)
                   + "), so Phase 1, the coordinator and the verifier would refuse the audit before any call.")
        else:
            why = "It may have been frozen by an older skill."
        refuse(f"{snapshot} freezes a Claude route this skill does not ship: {role} is "
               f"{described(theirs.get(role))} there, {described(ours.get(role))} in "
               f"{record_invocation.POLICY}. {why} {remedy}")
    # 2.7.1 (re-check F1): a leftover on the shipped routes whose probe bound no runner admits (a candidate build's
    # 300,000 or 500,000, say) was reused, and warn_probe_bound then said the audit keeps it for life -- but
    # run_phase1.py and run_verifier.py refuse such an audit before any call, so it could never run.
    bound = section(section(policy, "phase_1"), "runtime").get("probe_max_normalized_tokens")
    if probe_bounds.live_probe_bound(bound) is None:
        refuse(f"{snapshot} freezes a Phase-1 probe bound this skill does not run: probe_max_normalized_tokens "
               f"{bound} is not a packaged probe bound ({probe_bounds.describe()}), so Phase 1 would refuse the "
               f"audit before any call. {remedy}")
    # 2.7.2: the same for the per-call Phase-1 timeout (scripts/phase1_timeouts.py).
    timeout = section(section(policy, "phase_1"), "runtime").get("timeout_seconds")
    if phase1_timeouts.live_phase1_timeout(timeout) is None:
        refuse(f"{snapshot} freezes a Phase-1 timeout this skill does not run: timeout_seconds {timeout} is not a "
               f"packaged Phase-1 timeout ({phase1_timeouts.describe()}), so Phase 1 would refuse the audit before "
               f"any call. {remedy}")
    # 2.7.4: and for the per-call critique ceiling (scripts/critique_timeouts.py); a leftover without the leaf is an
    # older skill's and its routes decide it above.
    try:
        critique_timeouts.frozen_ceiling(policy)
    except critique_timeouts.CritiqueTimeoutError as exc:
        refuse(f"{snapshot} freezes a critique ceiling this skill does not run: {exc}, so run_critique.sh would refuse "
               f"every round. {remedy}")
    # 2.7.4: and for the later-round claim-change rule (scripts/claim_change_rules.py)
    try:
        claim_change_rules.rule_of(policy)
    except claim_change_rules.RuleError as exc:
        refuse(f"{snapshot} freezes a claim-change rule this skill does not run: {exc}, so make_diff_packet.py would "
               f"refuse every later round. {remedy}")
    # 2.7.5: and for the critic route. A leftover frozen on another critic model (the snapshot of an audit re-pinned
    # to Terra, say) would give a new audit that route for life; every audit initialised now runs its critique on the
    # shipped route, and a leftover that predates critique.critic_route_lock (frozen by 2.7.4 or earlier) is refused
    # too: its audit's route would not be locked, and removing a leftover costs nothing.
    theirs, ours = critic_route_of(policy), critic_route_of(shipped)
    if theirs != ours:
        refuse(f"{snapshot} freezes a critic route this skill does not ship: critics, adjudicator and critique probe "
               f"{theirs} there, {ours} in {record_invocation.POLICY}. Every audit initialised now runs its critique "
               f"on the shipped route (2.7.5). {remedy}")
    lock, shipped_lock = (section(section(p, "critique"), "critic_route_lock") for p in (policy, shipped))
    if lock != shipped_lock:
        refuse(f"{snapshot} " + (f"freezes a critic-route lock this skill does not ship ({lock})" if lock else
               "predates the critic-route lock (critique.critic_route_lock, 2.7.5), so the audit's critic route "
               "would not be locked") + f". {remedy}")
critique, coordinator = section(policy, "critique"), section(section(policy, "models"), "coordinator")
for value in (critique.get("round_target"), critique.get("round_cap"), coordinator.get("requested_model"),
              coordinator.get("provider"), coordinator.get("reasoning_effort")):
    print("" if value is None else value)
PY
}

# ------------------------------------------------- an initialised audit whose run.json is gone
# run.json is only a projection of run.events.jsonl, the canonical record, and log_event.sh sends an operator
# whose run.json is gone to this script. Until the 2.7.0 review (init-1) init took "no run.json" to mean
# "never initialised": policy_to_freeze called the audit's frozen snapshot -- every audit before 2.7.0 froze
# Sonnet 5 -- a leftover of an unfinished init and printed the rm that deletes its only copy; a snapshot
# matching the shipped routes got a fresh run.json written over the audit before the journal refusal. A
# journal, Phase-1 work under phase1/ (run_phase1.py's, never setup_workspace.sh's) or an invocation ledger is
# an initialised audit. Without --force it is refused here, before the version pin and before anything is written.
#
# (2.7.0 review, init-r2-1) run_phase1.py makes phase1/ -- with its run.lock and, as it refuses, a status.json --
# before it looks for the snapshot, so a run_phase1.py tried before init (the --probe-only canary, say) refuses
# with "run scripts/init_run.sh on the audit first" and leaves exactly those two files, or an empty phase1/ when
# no Claude CLI is found. Neither is an initialised audit: init refused such a workspace as one, and pointed to a
# snapshot and a backup that do not exist. Any other entry is Phase-1 work (snapshots/, preflight/, roles/,
# probe_cache/, quorum.json, ...), and so is a phase1 that is not a plain directory or cannot be listed.
phase1_holds_work () {
  local p1="$1" other
  [ -e "$p1" ] || [ -L "$p1" ] || return 1
  { [ -d "$p1" ] && [ ! -L "$p1" ]; } || return 0
  other=$(find "$p1" -mindepth 1 -maxdepth 1 ! \( -type f \( -name run.lock -o -name status.json \) \) -print -quit) ||
    return 0
  [ -n "$other" ]
}
refuse_initialised_audit () {
  local dir="$1" signs="" shipped frozen
  [ -s "$dir/run.events.jsonl" ] && signs="$signs, the journal run.events.jsonl"
  if phase1_holds_work "$dir/phase1"; then signs="$signs, phase1/"; fi
  [ -s "$dir/invocations.jsonl" ] && signs="$signs, the invocation ledger invocations.jsonl"
  [ -n "$signs" ] || return 0
  echo "FATAL: $dir is an initialised audit (it has ${signs#, }) whose run.json is missing." >&2
  echo "       run.json is only a projection of the journal: init_run.sh does not initialise the audit again." >&2
  if [ -e "$dir/pipeline-policy.snapshot.json" ] || [ -L "$dir/pipeline-policy.snapshot.json" ]; then
    echo "       Its pipeline-policy.snapshot.json is the policy the audit froze -- keep that file as it is." >&2
  else
    echo "       Its pipeline-policy.snapshot.json, the policy the audit froze, is missing too: restore it from a backup of the audit." >&2
  fi
  if [ -s "$dir/run.events.jsonl" ]; then
    echo "       Rebuild run.json from the journal (no provider call), then carry on:" >&2
    printf '         python3 %q --recover %q\n' "$SCRIPT_DIR/record_invocation.py" "$dir" >&2
  else
    echo "       It has no journal to rebuild run.json from: restore run.json from a backup of the audit." >&2
  fi
  shipped=$(jq -r '.models.coordinator.requested_model // empty' "$SCRIPT_DIR/../config/pipeline-policy-v2.json" 2>/dev/null)
  frozen=$(jq -r '.policy.models.coordinator.requested_model // empty' "$dir/pipeline-policy.snapshot.json" 2>/dev/null)
  echo "       To start the audit over instead, pass --force: it archives the journal, phase1/ and the snapshot," >&2
  echo "       freezes the shipped policy again (Claude route ${shipped:-unknown}) and Phase 1 re-runs." >&2
  if [ -n "$frozen" ] && [ -n "$shipped" ] && [ "$frozen" != "$shipped" ]; then
    echo "       This audit froze $frozen, so --force would move it to $shipped (INSTALL.md, Upgrading in-flight audits to 2.7.0)." >&2
  fi
  # 2.7.3: the effort is part of the route too (max before 2.7.3, xhigh after)
  shipped=$(jq -r '.models.coordinator.reasoning_effort // empty' "$SCRIPT_DIR/../config/pipeline-policy-v2.json" 2>/dev/null)
  frozen=$(jq -r '.policy.models.coordinator.reasoning_effort // empty' "$dir/pipeline-policy.snapshot.json" 2>/dev/null)
  if [ -n "$frozen" ] && [ -n "$shipped" ] && [ "$frozen" != "$shipped" ]; then
    echo "       This audit froze Claude effort $frozen, so --force would move it to $shipped (INSTALL.md, Upgrading in-flight audits to 2.7.3)." >&2
  fi
  return 1
}

# The EXIT trap's cleanup, acting from the freeze (or a forced re-init's archive of the old snapshot) until run.json
# binds the new one (RUN_BOUND=1), so no refusal or failure in between leaves an unbound snapshot for a later init:
# * it removes the live snapshot only when THIS run created it (record_invocation.py --init-policy printed
#   "created": its O_EXCL create won) and no run.json binds its bytes. Taking "absent before the freeze" to
#   mean "mine" removed a snapshot a concurrent init had created and bound (2.7.0 review, init-2).
# * it then moves the snapshot a --force archived back into place and says so (init-3): the old run.json and
#   journal are untouched until run.json binds the new snapshot, so the audit is as it was before the re-init.
restore_unbound_snapshot () {
  local snap="$DIR/pipeline-policy.snapshot.json" bound
  [ "${RUN_BOUND:-0}" -eq 1 ] && return 0
  if [ "${SNAPSHOT_CREATED:-0}" -eq 1 ] && [ -f "$snap" ] && [ ! -L "$snap" ]; then
    bound=$(jq -r '.toolkit.pipeline_policy_snapshot_sha256 // empty' "$DIR/run.json" 2>/dev/null)
    if [ -n "$bound" ] && [ "$bound" = "$(shasum -a 256 "$snap" 2>/dev/null | cut -d' ' -f1)" ]; then
      echo "NOTE: kept $snap, which this init froze: $DIR/run.json binds its bytes" >&2
    else
      rm -f "$snap"
    fi
  fi
  if [ -n "${ARCHIVED_SNAPSHOT:-}" ] && [ -f "$ARCHIVED_SNAPSHOT" ]; then
    if [ ! -e "$snap" ] && [ ! -L "$snap" ] && mv "$ARCHIVED_SNAPSHOT" "$snap"; then
      echo "NOTE: init stopped before run.json bound a new policy snapshot; restored the archived one to $snap" >&2
    else
      echo "NOTE: the prior policy snapshot stays archived at $ARCHIVED_SNAPSHOT ($snap exists)" >&2
    fi
  fi
  return 0
}

# ------------------------------------------------------------- one init at a time per audit directory
# (2.7.0 review, init-r2-2) The cleanup above keeps a snapshot another init bound, but not one another init had
# loaded, as a matching leftover, and not yet bound: this init removed it, and the other then bound a file that
# no longer existed (an audit whose run.json names a missing snapshot). Two inits could also both pass the
# run.json check and write run.json over each other. So init holds an exclusive fcntl lock on
# <audit-dir>/.init.lock from its run.json check through the journal's initialisation, and on a failure until
# the cleanup has run; a second init waits, says so (lock_wait.py), and then finds a run.json to refuse or
# nothing left over. The lock is bash's fd 9, taken by a python3 child that inherits it and kept when that
# child exits. The file is removed on release, as the holder, so a refused init leaves nothing behind; a waiter
# therefore checks, once it holds the lock, that the path still names the file it locked, else locks anew.
INIT_LOCK_HELD=0
acquire_init_lock () {
  local lock="$DIR/.init.lock" rc
  while :; do
    [ ! -L "$lock" ] || die "$lock is a symlink, not init's lock file; remove it and re-run init_run.sh"
    exec 9>>"$lock" || die "could not open the init lock $lock"
    python3 - "$SCRIPT_DIR" "$lock" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
import lock_wait


class InheritedLock:
    name = sys.argv[2]

    @staticmethod
    def fileno() -> int:
        return 9


lock_wait.acquire_exclusive(InheritedLock(), "the init lock of this audit")
try:
    same = os.path.samestat(os.fstat(9), os.lstat(sys.argv[2]))
except FileNotFoundError:
    same = False
sys.exit(0 if same else 3)
PY
    rc=$?
    if [ "$rc" -eq 0 ]; then INIT_LOCK_HELD=1; return 0; fi
    exec 9>&-
    [ "$rc" -eq 3 ] || die "could not take the init lock $lock"
  done
}
release_init_lock () {
  [ "${INIT_LOCK_HELD:-0}" -eq 1 ] || return 0
  INIT_LOCK_HELD=0
  rm -f "$DIR/.init.lock"
  exec 9>&-
}
# The EXIT trap from the lock on: the snapshot cleanup (a no-op until the freeze, and once RUN_BOUND=1), then the lock.
on_init_exit () {
  restore_unbound_snapshot
  release_init_lock
}
refuse_existing_run () {
  if [ -f "$RUN" ] && [ "$FORCE" -ne 1 ]; then
    die "$RUN already exists. init_run.sh runs ONCE per audit; re-running would discard the
       journal of everything logged so far. Pass --force only if you mean to start over."
  fi
}

# ---------------------------------------------------------------------- main
main () {
  need_jq
  DIR=""; AUDIT_ID=""; DOI=""; TITLE=""; TARGET=2; CAP=""; FORCE=0
  AM="${AUDITOR_MODEL:-}"; AF="${AUDITOR_FAMILY:-}"; AP="${AUDITOR_PROVIDER:-}"
  PV=""; GV=""; SKILLD="$DEFAULT_SKILL_DIR"; PKGD=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --audit-id) AUDIT_ID="${2:-}"; { [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ]; } || die "$1 requires a value (got ${2:-nothing}; an empty variable?)"; shift 2 ;;
      --doi) DOI="${2:-}"; { [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ]; } || die "$1 requires a value (got ${2:-nothing}; an empty variable?)"; shift 2 ;;
      --paper-title) TITLE="${2:-}"; { [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ]; } || die "$1 requires a value (got ${2:-nothing}; an empty variable?)"; shift 2 ;;
      --round-target) TARGET="${2:-}"; { [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ]; } || die "$1 requires a value (got ${2:-nothing}; an empty variable?)"; shift 2 ;;
      --round-cap) CAP="${2:-}"; { [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ]; } || die "$1 requires a value (got ${2:-nothing}; an empty variable?)"; shift 2 ;;
      --auditor-model) AM="${2:-}"; { [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ]; } || die "$1 requires a value (got ${2:-nothing}; an empty variable?)"; shift 2 ;;
      --auditor-family) AF="${2:-}"; { [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ]; } || die "$1 requires a value (got ${2:-nothing}; an empty variable?)"; shift 2 ;;
      --auditor-provider) AP="${2:-}"; { [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ]; } || die "$1 requires a value (got ${2:-nothing}; an empty variable?)"; shift 2 ;;
      --protocol-version) PV="${2:-}"; { [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ]; } || die "$1 requires a value (got ${2:-nothing}; an empty variable?)"; shift 2 ;;
      --gate-set-version) GV="${2:-}"; { [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ]; } || die "$1 requires a value (got ${2:-nothing}; an empty variable?)"; shift 2 ;;
      --skill-dir) SKILLD="${2:-}"; { [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ]; } || die "$1 requires a value (got ${2:-nothing}; an empty variable?)"; shift 2 ;;
      --pkg-dir) PKGD="${2:-}"; { [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ]; } || die "$1 requires a value (got ${2:-nothing}; an empty variable?)"; shift 2 ;;
      --force) FORCE=1; shift ;;
      -h|--help) sed -n '2,52p' "$0"; exit 0 ;;
      -*) die "unknown option $1" ;;
      *) if [ -z "$DIR" ]; then DIR="$1"; else die "unexpected argument $1"; fi; shift ;;
    esac
  done
  [ -n "$DIR" ] || die "usage: init_run.sh <audit-dir> [options]  (--help for the list)"
  [ -d "$DIR" ] || die "$DIR is not a directory -- run setup_workspace.sh first"
  case "$TARGET" in ''|*[!0-9]*) die "round target must be a positive integer" ;; esac
  # an omitted --round-cap takes the snapshot's cap below; a given one must be a positive integer
  if [ -n "$CAP" ]; then
    case "$CAP" in *[!0-9]*) die "round cap must be a positive integer" ;; esac
    [ "$TARGET" -ge 1 ] && [ "$CAP" -ge "$TARGET" ] || die "require 1 <= round-target <= round-cap"
  else
    [ "$TARGET" -ge 1 ] || die "require 1 <= round-target <= round-cap"
  fi
  if [ -n "$DOI" ]; then
    # run.schema.json requires the bare form, and nothing can change paper_doi after intake, so a URL-form
    # DOI surfaced only at init_comment.py, after Phase 1 and the coordinator were paid for.
    DOI=$(printf '%s' "$DOI" | sed -E 's#^[[:space:]]+##; s#[[:space:]]+$##; s#^(https?://(dx\.)?doi\.org/|doi:[[:space:]]*)##I; s#[/.,;]$##')   # trailing punctuation from a citation
    printf '%s' "$DOI" | grep -Eq '^10\.[0-9]{4,9}/[^[:space:]]+$' ||
      die "--doi must be a bare DOI such as 10.1234/abc.5678 (got '$DOI'); drop any https://doi.org/ or doi: prefix"
  fi

  RUN="$DIR/run.json"
  refuse_existing_run   # at once, writing nothing, not even the lock file; repeated under the lock, where it counts
  # From here the EXIT trap releases the init lock and, once the snapshot is frozen, cleans up after this run.
  RUN_BOUND=0; SNAPSHOT_CREATED=0; ARCHIVED_SNAPSHOT=""
  trap on_init_exit EXIT
  acquire_init_lock
  refuse_existing_run
  if [ "$FORCE" -ne 1 ]; then refuse_initialised_audit "$DIR" || exit 2; fi

  # Everything that can refuse is decided before the snapshot is frozen (see policy_to_freeze).
  check_skill_version_pin "$DIR" "$SKILLD" || exit 2
  PLAN=$(policy_to_freeze "$DIR" "$FORCE") || exit 2
  { IFS= read -r POLICY_TARGET; IFS= read -r POLICY_CAP; IFS= read -r POLICY_COORD_MODEL
    IFS= read -r POLICY_COORD_PROVIDER; IFS= read -r POLICY_COORD_EFFORT; } <<EOF
$PLAN
EOF
  [ -n "$POLICY_TARGET" ] && [ -n "$POLICY_CAP" ] ||
    die "audit-scoped policy snapshot lacks critique round target/cap"
  [ -n "$CAP" ] || CAP="$POLICY_CAP"   # the default is the snapshot's own cap, never a literal
  [ "$TARGET" -eq "$POLICY_TARGET" ] && [ "$CAP" -eq "$POLICY_CAP" ] ||
    die "canonical accuracy-safe policy requires round target $POLICY_TARGET and hard cap $POLICY_CAP"
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
    die "canonical baseline requires coordinator and Phase-1 model $POLICY_COORD_MODEL (got $AM); omit --auditor-model to take the policy's"
  [ "$AP" = "$POLICY_COORD_PROVIDER" ] ||
    die "canonical baseline requires provider $POLICY_COORD_PROVIDER (got $AP)"
  [ "$AF" = "$POLICY_COORD_FAMILY" ] ||
    die "canonical baseline requires model family $POLICY_COORD_FAMILY (got $AF)"

  # From here until run.json binds the snapshot, the EXIT trap removes a snapshot this run created and puts
  # back one a --force archived (restore_unbound_snapshot). ARCHIVED_SNAPSHOT is named before the mv: the trap
  # acts on the file only if it exists, so a stop on either side of the mv is covered.
  if [ "$FORCE" -eq 1 ] && [ -e "$DIR/pipeline-policy.snapshot.json" ]; then
    [ ! -L "$DIR/pipeline-policy.snapshot.json" ] || die "refusing to archive a symlinked policy snapshot"
    PRIOR_ARCHIVE="$DIR/pipeline-policy.snapshot.previous.$(date -u +%Y%m%dT%H%M%SZ).json"
    # mv would overwrite an archive made in the same second, and the trap would put back the wrong file
    [ ! -e "$PRIOR_ARCHIVE" ] && [ ! -L "$PRIOR_ARCHIVE" ] ||
      die "$PRIOR_ARCHIVE already exists (a snapshot was archived this second); re-run init_run.sh"
    ARCHIVED_SNAPSHOT="$PRIOR_ARCHIVE"
    mv "$DIR/pipeline-policy.snapshot.json" "$ARCHIVED_SNAPSHOT" ||
      die "could not archive the prior policy snapshot for forced reinitialization"
  fi

  [ -n "$PKGD" ] || PKGD="$DIR/pkg"
  # Only the O_EXCL create in record_invocation.py knows whether this run froze the file (init-2).
  FROZE=$(python3 "$SCRIPT_DIR/record_invocation.py" --init-policy "$DIR") ||
    die "could not create the immutable audit-scoped pipeline policy snapshot"
  case "$FROZE" in "policy snapshot created: "*) SNAPSHOT_CREATED=1 ;; esac
  # the frozen snapshot must be the policy checked above (a concurrent init could have frozen another)
  [ "$(jq -r '.policy.critique.round_target // empty' "$DIR/pipeline-policy.snapshot.json")" = "$POLICY_TARGET" ] &&
    [ "$(jq -r '.policy.critique.round_cap // empty' "$DIR/pipeline-policy.snapshot.json")" = "$POLICY_CAP" ] &&
    [ "$(jq -r '.policy.models.coordinator.requested_model // empty' "$DIR/pipeline-policy.snapshot.json")" = "$POLICY_COORD_MODEL" ] &&
    [ "$(jq -r '.policy.models.coordinator.reasoning_effort // empty' "$DIR/pipeline-policy.snapshot.json")" = "$POLICY_COORD_EFFORT" ] ||
    die "the frozen policy snapshot is not the policy init checked; re-run init_run.sh"
  POLICY_SHA=$(shasum -a 256 "$DIR/pipeline-policy.snapshot.json" | cut -d' ' -f1)
  [ ${#POLICY_SHA} -eq 64 ] || die "could not hash the audit-scoped pipeline policy snapshot"
  # 2.7.1: the Phase-1 capability-probe bound, read from the snapshot as frozen, like the round cap. It is part of
  # the probe contract, so the audit keeps it for its whole life (scripts/probe_bounds.py). A snapshot created
  # here holds the shipped one; a leftover reused above keeps its own (2.7.0's record_invocation.py --init-policy
  # froze 150000), and the summary warns about that.
  PROBE_BOUND=$(jq -r '.policy.phase_1.runtime.probe_max_normalized_tokens // empty' "$DIR/pipeline-policy.snapshot.json")
  SHIPPED_PROBE_BOUND=$(jq -r '.phase_1.runtime.probe_max_normalized_tokens // empty' "$SCRIPT_DIR/../config/pipeline-policy-v2.json")
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
  SKILLSHA="$(gate_files_digest "$SKILLD")" || exit 2
  jq -n \
    --arg sv "$SCHEMA_VERSION" --arg id "$AUDIT_ID" --argjson doi "$DOI_J" \
    --argjson title "$TITLE_J" --arg pv "$PV" --arg gv "$GV" --arg start "$START" \
    --arg am "$AM" --arg af "$AF" --arg ap "$AP" --arg ae "$POLICY_COORD_EFFORT" \
    --argjson target "$TARGET" --argjson cap "$CAP" \
    --arg skill "$(basename "$SKILLD")" --arg policysha "$POLICY_SHA" \
    --arg skillsha "$SKILLSHA" \
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
      pause_reason: null,
      post_delta_gates_passed: false,
      journal_watermark: {journal_seq:0,journal_hash:null},
      durations: null,
      diagnostic_artifacts: [],
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
  RUN_BOUND=1   # run.json now binds the snapshot's hash; the EXIT trap still releases the init lock

  # Metadata 2.1 makes the hash-chained journal canonical.  run.json and
  # invocations.jsonl are regenerated projections, never competing sources of truth.
  if [ "$FORCE" -eq 1 ] && [ -s "$DIR/run.events.jsonl" ]; then
    mv "$DIR/run.events.jsonl" "$DIR/run.events.previous.$(date -u +%Y%m%dT%H%M%SZ).jsonl"
  fi
  if [ "$FORCE" -eq 1 ] && [ -d "$DIR/phase1" ]; then
    # Phase 1's probe cache and role completions point at invocations in the journal just archived; left
    # in place they make every later run_phase1.py pause ("cache replay source ... not a terminal
    # canonical invocation") or refuse ("cached role completion lacks one canonical inference record").
    # The attempt directories are read-only by design, so make them movable first.
    chmod -R u+w "$DIR/phase1" 2>/dev/null || true
    mv "$DIR/phase1" "$DIR/phase1.previous.$(date -u +%Y%m%dT%H%M%SZ)" ||
      die "could not archive $DIR/phase1; move it aside before re-running Phase 1"
  fi
  if [ -s "$DIR/run.events.jsonl" ]; then
    die "a canonical journal already exists; use --force to archive it before reinitializing"
  fi
  python3 "$SCRIPT_DIR/lifecycle_journal.py" initialize "$DIR" >/dev/null ||
    die "could not initialize the canonical metadata-2.1 lifecycle journal"
  release_init_lock; trap - EXIT

  echo "=== run.json initialised ==="
  echo "  audit_id:          $AUDIT_ID"
  echo "  paper_doi:         ${DOI:-<none given -- no event sets it later: re-run init_run.sh with --force --doi <doi> before any other event>}"
  echo "  protocol_version:  $PV"
  echo "  gate_set_version:  $GV"
  # 2.7.3: the effort the snapshot froze for the Claude roles (xhigh for a new audit; an older audit keeps max)
  echo "  auditor model:     $AM ($AF / $AP), effort $(jq -r '.policy.models.coordinator.reasoning_effort // "<none frozen>"' "$DIR/pipeline-policy.snapshot.json")"
  echo "  critique rounds:   target $TARGET, hard cap $CAP"
  echo "  probe bound:       ${PROBE_BOUND:-<none frozen>} normalized tokens"
  echo "  phase-1 timeout:   $(jq -r '.policy.phase_1.runtime.timeout_seconds // "<none frozen>"' "$DIR/pipeline-policy.snapshot.json") seconds per auditor call"
  # 2.7.4: the critique ceiling the snapshot froze (an older audit froze none and runs at 2700)
  echo "  critique timeout:  $(jq -r '.policy.critique.provider_timeout_seconds // "<none frozen: 2700>"' "$DIR/pipeline-policy.snapshot.json") seconds per critic or adjudicator call"
  # 2.7.5: the critic route the snapshot froze, and whether it is locked
  echo "  critic route:      $(jq -r '"critics \(.policy.models.critics.requested_model) at \(.policy.models.critics.reasoning_effort), adjudicator \(.policy.models.targeted_adjudicator.requested_model) at \(.policy.models.targeted_adjudicator.reasoning_effort)" + (if .policy.critique.critic_route_lock.model then "; locked (2.7.5)" else "; not locked" end)' "$DIR/pipeline-policy.snapshot.json")"
  # 2.7.4: which later-round findings the specialists re-review (an older audit froze none: every changed finding)
  echo "  later rounds:      specialists re-review $(jq -r 'if .policy.critique.later_rounds.routing_manifest.claim_change_rule == "substantive_specialists_1" then "substantive changes only; completeness every changed finding" else "every changed finding" end' "$DIR/pipeline-policy.snapshot.json")"
  echo "  package:           $(printf '%s' "$PKGJSON" | jq -r '.file_count') files, $(printf '%s' "$PKGJSON" | jq -r '.total_bytes') bytes"
  echo "  package manifest:  work/pkg_manifest.txt (sha256 $(printf '%s' "$PKGJSON" | jq -r '.manifest_sha256 // "n/a"' | cut -c1-16)...)"
  echo "  written:           $RUN"
  echo ""
  printf 'NEXT: export RUN_JSON=%q and log every phase boundary, model, failure and round result\n' "$RUN"
  echo "      with log_event.sh as they happen. Nothing here can be reconstructed afterwards."
  warn_probe_bound
  warn_critique_leaves
  return 0
}

# 2.7.1: a snapshot init reused (one left in the directory without a run.json, which it reuses when its Claude
# routes are the shipped ones) keeps the probe bound it froze, and so does the audit, for its whole life. Say so
# when that is not the shipped bound, with the re-init that freezes the shipped policy -- the bound, the routes
# and all -- while nothing has run yet. Only a warning: the audit is valid on its frozen bound, because
# policy_to_freeze has already refused a leftover whose bound is not a live one.
warn_probe_bound () {
  [ "$PROBE_BOUND" = "$SHIPPED_PROBE_BOUND" ] && return 0
  {
    echo ""
    if [ "${SNAPSHOT_CREATED:-0}" -eq 1 ]; then
      echo "WARNING: the policy snapshot this init froze has a Phase-1 probe bound of ${PROBE_BOUND:-<none>} normalized"
    else
      echo "WARNING: this init reused the policy snapshot left in the audit directory by an init that did not finish."
      echo "         That snapshot froze a Phase-1 probe bound of ${PROBE_BOUND:-<none>} normalized"
    fi
    echo "         tokens, not the shipped ${SHIPPED_PROBE_BOUND:-<none>}. The bound is part of the probe contract, so this audit"
    echo "         keeps ${PROBE_BOUND:-<none>} for its whole life (INSTALL.md, Upgrading in-flight audits to 2.7.1). To"
    echo "         freeze the shipped policy instead, re-initialise it now, before Phase 1 (this archives the snapshot):"
    printf '           bash %q %q --force --audit-id %q' "$SCRIPT_DIR/init_run.sh" "$DIR" "$AUDIT_ID"
    [ -z "$DOI" ] || printf ' --doi %q' "$DOI"
    [ -z "$TITLE" ] || printf ' --paper-title %q' "$TITLE"
    printf '\n'
  } >&2
  return 0
}

# 2.7.4: a reused leftover frozen by 2.7.3 or earlier has neither critique leaf, so the new audit would keep the
# legacy 2,700 s critique ceiling and every-changed-finding specialist scope for life.
warn_critique_leaves () {
  [ "${SNAPSHOT_CREATED:-0}" -eq 1 ] && return 0
  local snap="$DIR/pipeline-policy.snapshot.json" missing=""
  jq -e '.policy.critique | has("provider_timeout_seconds")' "$snap" > /dev/null 2>&1 || missing="critique.provider_timeout_seconds"
  jq -e '.policy.critique.later_rounds.routing_manifest | has("claim_change_rule")' "$snap" > /dev/null 2>&1 \
    || missing="${missing:+$missing and }critique.later_rounds.routing_manifest.claim_change_rule"
  [ -z "$missing" ] && return 0
  {
    echo ""
    echo "WARNING: this init reused the policy snapshot left in the audit directory by an init that did not finish."
    echo "         That snapshot froze no $missing, so this audit keeps the pre-2.7.4 critique"
    echo "         ceiling (2,700 s) and later-round specialist scope for its whole life (INSTALL.md, Upgrading in-flight"
    if [ "$PROBE_BOUND" != "$SHIPPED_PROBE_BOUND" ]; then
      echo "         audits to 2.7.4). The --force command above freezes the shipped policy, these leaves included."
    else
      echo "         audits to 2.7.4). To freeze the shipped policy instead, re-initialise it now, before Phase 1:"
      printf '           bash %q %q --force --audit-id %q' "$SCRIPT_DIR/init_run.sh" "$DIR" "$AUDIT_ID"
      [ -z "$DOI" ] || printf ' --doi %q' "$DOI"
      [ -z "$TITLE" ] || printf ' --paper-title %q' "$TITLE"
      printf '\n'
    fi
  } >&2
  return 0
}

# ------------------------------------------------------------------ self-test
self_test () {
  need_jq
  local T rc fails shipped_model
  T=$(mktemp -d "${TMPDIR:-/tmp}/init_run_selftest.XXXXXX")
  if [ $? -ne 0 ]; then die "could not make a scratch directory"; fi
  fails=0
  echo "== init_run.sh self-test in $T =="
  # the model the shipped policy freezes (sonnet-5-5 since 2.7.0), never a literal
  shipped_model=$(jq -r '.models.coordinator.requested_model // empty' "$SCRIPT_DIR/../config/pipeline-policy-v2.json")
  [ -n "$shipped_model" ] || { echo "  FAIL: the shipped policy names no coordinator model"; fails=1; }

  mkdir -p "$T/a/pkg/sub"
  printf 'x,y\n1,2\n' > "$T/a/pkg/data.csv"
  printf 'print(1)\n'  > "$T/a/pkg/analysis.R"
  printf 'hello\n'     > "$T/a/pkg/sub/README.md"

  # 1. a normal init succeeds and records the package correctly
  "$SELF" "$T/a" --audit-id t-1 --doi 10.1234/abc --round-cap 7 \
       --auditor-model "$shipped_model" --auditor-family claude --auditor-provider anthropic \
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
  # 2.6.9: a new audit gets the shipped cap; the old cap of 5 is refused for it, and the default is the snapshot's
  mkdir -p "$T/f/pkg" "$T/g/pkg"
  "$SELF" "$T/f" --audit-id t-cap5 --round-cap 5 > "$T/out9.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ] || [ -e "$T/f/run.json" ]; then
    echo "  FAIL: a new audit asking for the pre-2.6.9 cap of 5 should refuse with exit 2 (got $rc)"; fails=1
  else echo "  ok: a new audit cannot be initialised with the pre-2.6.9 cap of 5"; fi
  "$SELF" "$T/g" --audit-id t-capdefault > "$T/out10.txt" 2>&1
  rc=$?
  n=$(jq -r '.round_cap' "$T/g/run.json" 2>/dev/null)
  local want
  want=$(jq -r '.policy.critique.round_cap' "$T/g/pipeline-policy.snapshot.json" 2>/dev/null)
  if [ "$rc" -ne 0 ] || [ "$n" != "$want" ] || [ "$n" != "7" ]; then
    echo "  FAIL: an omitted --round-cap should take the snapshot's cap 7 (exit $rc, run $n, snapshot $want)"; fails=1
  else echo "  ok: an omitted --round-cap takes the snapshot's cap ($n)"; fi

  # 7. Baseline identity is policy, not caller preference: Opus may not be substituted.
  mkdir -p "$T/e/pkg"
  "$SELF" "$T/e" --audit-id t-model --auditor-model opus-5 \
       --auditor-family claude --auditor-provider anthropic > "$T/out8.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ] || [ -e "$T/e/run.json" ]; then
    echo "  FAIL: noncanonical Opus baseline should refuse with exit 2 before run creation (got $rc)"; fails=1
  else echo "  ok: Opus cannot replace the Sonnet coordinator/Phase-1 baseline"; fi

  # 8. 2.7.0: a refused init leaves no policy snapshot behind (it used to freeze one before refusing)
  local d n_left
  n_left=0
  for d in c d f e; do [ -e "$T/$d/pipeline-policy.snapshot.json" ] && n_left=$((n_left + 1)); done
  if [ "$n_left" -ne 0 ]; then echo "  FAIL: $n_left refused init(s) left a policy snapshot behind"; fails=1
  else echo "  ok: the four refused inits above left no policy snapshot"; fi

  # 9. a leftover snapshot whose Claude routes are not the shipped policy's (an older skill's) is refused,
  #    untouched, with the file to remove; a matching one (record_invocation.py --init-policy) is reused
  mkdir -p "$T/h/pkg" "$T/i/pkg"
  python3 - "$SCRIPT_DIR" "$T/h" <<'PY' || { echo "  FAIL: could not plant a Sonnet 5 leftover snapshot"; fails=1; }
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import record_invocation
policy = json.loads(record_invocation.POLICY.read_text(encoding="utf-8"))
legacy = {"requested_model": "sonnet-5", "spawn_alias": "sonnet"}
for entry in [policy["models"]["coordinator"], policy["models"]["post_fan_out_verifier"],
              *policy["models"]["phase_1_auditors"].values()]:
    entry.update(legacy)
envelope = {"snapshot_version": "1", "policy_sha256": record_invocation.policy_hash(policy), "policy": policy}
(pathlib.Path(sys.argv[2]) / record_invocation.POLICY_SNAPSHOT).write_text(
    json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  local before
  before=$(shasum -a 256 "$T/h/pipeline-policy.snapshot.json" 2>/dev/null | cut -d' ' -f1)
  "$SELF" "$T/h" --audit-id t-leftover > "$T/out11.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ] || [ -e "$T/h/run.json" ] || ! grep -q "rm .*pipeline-policy.snapshot.json" "$T/out11.txt" ||
     [ "$(shasum -a 256 "$T/h/pipeline-policy.snapshot.json" 2>/dev/null | cut -d' ' -f1)" != "$before" ]; then
    echo "  FAIL: a Sonnet 5 leftover snapshot should be refused, untouched, naming the file (exit $rc)"
    cat "$T/out11.txt"; fails=1
  else echo "  ok: a leftover snapshot on another Claude route is refused and left as it was"; fi
  python3 "$SCRIPT_DIR/record_invocation.py" --init-policy "$T/i" > /dev/null 2>&1
  before=$(shasum -a 256 "$T/i/pipeline-policy.snapshot.json" 2>/dev/null | cut -d' ' -f1)
  "$SELF" "$T/i" --audit-id t-matching > "$T/out12.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$before" ] ||
     [ "$(jq -r '.toolkit.pipeline_policy_snapshot_sha256' "$T/i/run.json" 2>/dev/null)" != "$before" ]; then
    echo "  FAIL: a leftover snapshot of the shipped policy should be reused (exit $rc)"; cat "$T/out12.txt"; fails=1
  else echo "  ok: a leftover snapshot of the shipped policy is reused unchanged"; fi

  # 10. (2.7.0 review, init-1) an initialised audit whose run.json -- a projection of its journal -- is gone is
  #     refused with the rebuild command, never the rm of its snapshot, and nothing is written; the command works
  before=$(shasum -a 256 "$T/a/pipeline-policy.snapshot.json" 2>/dev/null | cut -d' ' -f1)
  mv "$T/a/run.json" "$T/a-run.json.moved"
  "$SELF" "$T/a" --audit-id t-1 > "$T/out13.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ] || [ -e "$T/a/run.json" ] || ! grep -q -- "--recover" "$T/out13.txt" ||
     grep -Eq '(^|[^[:alnum:]])rm ' "$T/out13.txt" ||
     [ "$(shasum -a 256 "$T/a/pipeline-policy.snapshot.json" 2>/dev/null | cut -d' ' -f1)" != "$before" ]; then
    echo "  FAIL: an initialised audit without run.json should be refused with the rebuild command (exit $rc)"
    cat "$T/out13.txt"; fails=1
  else echo "  ok: an initialised audit without run.json is refused with the rebuild command, its snapshot untouched"; fi
  python3 "$SCRIPT_DIR/record_invocation.py" --recover "$T/a" > "$T/out14.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$before" ] ||
     [ "$(jq -r '.toolkit.pipeline_policy_snapshot_sha256' "$T/a/run.json" 2>/dev/null)" != "$before" ]; then
    echo "  FAIL: record_invocation.py --recover should rebuild a run.json bound to the snapshot (exit $rc)"
    cat "$T/out14.txt"; fails=1
  else echo "  ok: record_invocation.py --recover rebuilds run.json, bound to the snapshot"; fi

  # 11. (init-3) a --force that fails after archiving the snapshot puts it back: the Sonnet 5 snapshot of 9, now
  #     bound by a run.json, survives a forced re-init that stops at the gate digest of an empty --skill-dir
  mkdir -p "$T/empty-skill"
  before=$(shasum -a 256 "$T/h/pipeline-policy.snapshot.json" 2>/dev/null | cut -d' ' -f1)
  jq -n --arg s "$before" '{toolkit: {pipeline_policy_snapshot_sha256: $s}}' > "$T/h/run.json"
  "$SELF" "$T/h" --audit-id t-leftover --force --skill-dir "$T/empty-skill" > "$T/out15.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ] || [ -z "$before" ] || ! grep -q "restored the archived one" "$T/out15.txt" ||
     [ "$(shasum -a 256 "$T/h/pipeline-policy.snapshot.json" 2>/dev/null | cut -d' ' -f1)" != "$before" ] ||
     [ -n "$(find "$T/h" -maxdepth 1 -name 'pipeline-policy.snapshot.previous.*' -print)" ]; then
    echo "  FAIL: a --force that fails after the archive should put the archived snapshot back (exit $rc)"
    cat "$T/out15.txt"; fails=1
  else echo "  ok: a --force that fails after archiving the snapshot puts it back"; fi

  # 12. (2.7.0 review, TD-4) a --force never overwrites an archive of the same second: with one planted for
  #     every second the init can run in, it refuses and changes nothing (no new archive, the snapshot as it was)
  before=$(shasum -a 256 "$T/h/pipeline-policy.snapshot.json" 2>/dev/null | cut -d' ' -f1)
  python3 - "$T/h" <<'PY' || { echo "  FAIL: could not plant the same-second archives"; fails=1; }
import pathlib, sys, time
start = int(time.time())
for offset in range(61):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(start + offset))
    (pathlib.Path(sys.argv[1]) / f"pipeline-policy.snapshot.previous.{stamp}.json").write_text(f"decoy {stamp}\n")
PY
  n=$(find "$T/h" -maxdepth 1 -name 'pipeline-policy.snapshot.previous.*' | wc -l | tr -d ' ')
  "$SELF" "$T/h" --audit-id t-leftover --force > "$T/out16.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ] || ! grep -q "already exists (a snapshot was archived this second)" "$T/out16.txt" ||
     [ "$(shasum -a 256 "$T/h/pipeline-policy.snapshot.json" 2>/dev/null | cut -d' ' -f1)" != "$before" ] ||
     [ "$(find "$T/h" -maxdepth 1 -name 'pipeline-policy.snapshot.previous.*' | wc -l | tr -d ' ')" != "$n" ] ||
     [ "$n" != "61" ]; then
    echo "  FAIL: a --force should refuse to overwrite an archive made the same second (exit $rc)"
    cat "$T/out16.txt"; fails=1
  else echo "  ok: a --force refuses to overwrite an archive made the same second"; fi

  # 13. (init-r2-1) run_phase1.py tried before init leaves phase1/{run.lock,status.json}: no sign of an initialised
  #     audit, so init initialises; Phase-1 work under phase1/ is still one. No init leaves its lock file.
  mkdir -p "$T/p/pkg" "$T/q/pkg" "$T/q/phase1/roles"
  python3 "$SCRIPT_DIR/run_phase1.py" "$T/p" --probe-only --claude-bin /usr/bin/false > "$T/out17.txt" 2>&1
  "$SELF" "$T/p" --audit-id t-probe-first > "$T/out18.txt" 2>&1
  rc=$?
  if ! grep -q "run scripts/init_run.sh on the audit first" "$T/out17.txt" || [ ! -f "$T/p/phase1/run.lock" ] ||
     [ "$rc" -ne 0 ] || [ ! -f "$T/p/run.json" ]; then
    echo "  FAIL: a workspace where run_phase1.py was tried before init should initialise (exit $rc)"
    cat "$T/out17.txt" "$T/out18.txt"; fails=1
  else echo "  ok: a workspace where run_phase1.py was tried before init initialises"; fi
  "$SELF" "$T/q" --audit-id t-phase1-work > "$T/out19.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ] || ! grep -q "is an initialised audit (it has phase1/)" "$T/out19.txt" || [ -e "$T/q/run.json" ] ||
     grep -q "keep that file as it is" "$T/out19.txt"; then
    echo "  FAIL: phase1/ holding Phase-1 work without run.json should be refused (exit $rc)"
    cat "$T/out19.txt"; fails=1
  else echo "  ok: phase1/ holding Phase-1 work is an initialised audit, refused"; fi
  n=$(find "$T" -name .init.lock | wc -l | tr -d ' ')
  if [ "$n" != "0" ]; then echo "  FAIL: $n init(s) left .init.lock behind"; fails=1
  else echo "  ok: no init, refused, failed or done, left its lock file"; fi

  # 14. (2.7.1) init prints the probe bound the snapshot froze, and warns when a reused leftover's is not the
  #     shipped one: 2.7.0's record_invocation.py --init-policy froze 150000 on the shipped routes, and such a
  #     leftover is reused (its routes match), so the audit keeps 150000. The shipped bound is read, never a literal.
  local shipped_bound
  shipped_bound=$(jq -r '.phase_1.runtime.probe_max_normalized_tokens // empty' "$SCRIPT_DIR/../config/pipeline-policy-v2.json")
  if [ -z "$shipped_bound" ] || [ "$shipped_bound" = "150000" ]; then
    echo "  FAIL: the shipped policy's probe bound is '$shipped_bound', expected one above the legacy 150000"; fails=1
  fi
  if ! grep -q "^  probe bound:       $shipped_bound normalized tokens$" "$T/out1.txt" ||
     grep -q "probe bound of" "$T/out1.txt" "$T/out12.txt"; then
    echo "  FAIL: a new init should print the shipped probe bound $shipped_bound and warn about none"
    cat "$T/out1.txt" "$T/out12.txt"; fails=1
  else echo "  ok: a new init, and one reusing a shipped-policy leftover, print the shipped bound without a warning"; fi
  mkdir -p "$T/r/pkg"
  python3 - "$SCRIPT_DIR" "$T/r" <<'PY' || { echo "  FAIL: could not plant a 150000-bound leftover snapshot"; fails=1; }
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import record_invocation
policy = json.loads(record_invocation.POLICY.read_text(encoding="utf-8"))
policy["phase_1"]["runtime"]["probe_max_normalized_tokens"] = 150000
policy["phase_1"]["capability_probe"]["maximum_normalized_tokens"] = 150000
envelope = {"snapshot_version": "1", "policy_sha256": record_invocation.policy_hash(policy), "policy": policy}
(pathlib.Path(sys.argv[2]) / record_invocation.POLICY_SNAPSHOT).write_text(
    json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  before=$(shasum -a 256 "$T/r/pipeline-policy.snapshot.json" 2>/dev/null | cut -d' ' -f1)
  "$SELF" "$T/r" --audit-id t-legacy-bound --doi 10.1234/abc > "$T/out20.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$before" ] ||
     [ "$(jq -r '.toolkit.pipeline_policy_snapshot_sha256' "$T/r/run.json" 2>/dev/null)" != "$before" ] ||
     ! grep -q "^  probe bound:       150000 normalized tokens$" "$T/out20.txt" ||
     ! grep -q "^WARNING: this init reused the policy snapshot left in the audit directory" "$T/out20.txt" ||
     ! grep -q "^         That snapshot froze a Phase-1 probe bound of 150000 normalized$" "$T/out20.txt" ||
     ! grep -q "^         tokens, not the shipped $shipped_bound\. " "$T/out20.txt" ||
     ! grep -q -- "init_run.sh .* --force --audit-id t-legacy-bound --doi 10.1234/abc$" "$T/out20.txt"; then
    echo "  FAIL: a reused 150000 leftover should print its bound and warn, with the full --force command (exit $rc)"
    cat "$T/out20.txt"; fails=1
  else echo "  ok: a reused leftover's 150000 bound is printed, with a warning and the full --force command"; fi

  # 15. (2.7.3) a new init prints the Claude effort it froze, the shipped one (read, never a literal), and a
  #     leftover on a Claude route this skill does not ship is refused untouched: a 2.7.2 --init-policy leftover
  #     (Sonnet 5.5 at max, a live route an older skill froze) and one on a route no runner dispatches (Sonnet 5.5
  #     at high), each naming the file to remove and saying which kind it is.
  shipped_effort=$(jq -r '.models.coordinator.reasoning_effort // empty' "$SCRIPT_DIR/../config/pipeline-policy-v2.json")
  if [ -z "$shipped_effort" ] ||
     ! grep -q "^  auditor model:     $shipped_model (claude / anthropic), effort $shipped_effort$" "$T/out1.txt"; then
    echo "  FAIL: a new init should print the shipped Claude effort '$shipped_effort'"; cat "$T/out1.txt"; fails=1
  else echo "  ok: a new init prints the Claude effort it froze ($shipped_effort)"; fi
  for leftover in max:older high:dispatches; do
    effort="${leftover%%:*}"; expect="${leftover#*:}"
    [ "$effort" != "$shipped_effort" ] || continue
    mkdir -p "$T/e-$effort/pkg"
    python3 - "$SCRIPT_DIR" "$T/e-$effort" "$effort" <<'PY' || { echo "  FAIL: could not plant an effort-$effort leftover"; fails=1; }
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import record_invocation
policy = json.loads(record_invocation.POLICY.read_text(encoding="utf-8"))
models = policy["models"]
for entry in [models["coordinator"], models["post_fan_out_verifier"], *models["phase_1_auditors"].values()]:
    entry["reasoning_effort"] = sys.argv[3]
policy["phase_1"]["capability_probe"]["reasoning_effort"] = sys.argv[3]
envelope = {"snapshot_version": "1", "policy_sha256": record_invocation.policy_hash(policy), "policy": policy}
(pathlib.Path(sys.argv[2]) / record_invocation.POLICY_SNAPSHOT).write_text(
    json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
    before=$(shasum -a 256 "$T/e-$effort/pipeline-policy.snapshot.json" 2>/dev/null | cut -d' ' -f1)
    "$SELF" "$T/e-$effort" --audit-id "t-effort-$effort" > "$T/out-e-$effort.txt" 2>&1
    rc=$?
    if [ "$rc" -ne 2 ] || [ -e "$T/e-$effort/run.json" ] || [ -z "$before" ] ||
       [ "$(shasum -a 256 "$T/e-$effort/pipeline-policy.snapshot.json" 2>/dev/null | cut -d' ' -f1)" != "$before" ] ||
       ! grep -q "freezes a Claude route this skill does not ship: coordinator is .* / $effort there" "$T/out-e-$effort.txt" ||
       ! grep -q "remove it (rm " "$T/out-e-$effort.txt" ||
       { [ "$expect" = older ] && ! grep -q "It may have been frozen by an older skill" "$T/out-e-$effort.txt"; } ||
       { [ "$expect" = dispatches ] && ! grep -q "No runner dispatches that route" "$T/out-e-$effort.txt"; }; then
      echo "  FAIL: a leftover at Claude effort $effort should be refused, untouched, naming the file (exit $rc)"
      cat "$T/out-e-$effort.txt"; fails=1
    else echo "  ok: a leftover at Claude effort $effort is refused and left as it was"; fi
  done

  rm -rf "$T"
  if [ "$fails" -ne 0 ]; then echo "SELF-TEST FAILED"; return 1; fi
  echo "SELF-TEST PASSED"
  return 0
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi
main "$@"
rc=$?
# record the machine state the audit will actually run on (team requirement 2026-08-24); since 2.7.4 the capture is
# journaled (environment_captured), so run.json keeps it through every later append
if [ $rc -eq 0 ] && [ -n "${DIR:-}" ] && [ -f "$DIR/run.json" ]; then
  bash "$(cd "$(dirname "$0")" && pwd)/capture_environment.sh" "$DIR" ||
    echo "WARNING: the environment was not recorded (see above); fix the cause and run: bash scripts/capture_environment.sh \"$DIR\""
fi
exit $rc
