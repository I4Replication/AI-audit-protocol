#!/usr/bin/env python3
"""Run the five immutable, content-addressed I4R Phase-1 auditors.

The order is intentionally strict:

1. freeze the raw sources and generate deterministic non-row-level evidence;
2. run a provider-free smoke test of the exact sandbox and promotion paths;
3. reuse or run one bounded synthetic capability probe on the audit's frozen Claude route; and only then
4. dispatch the five isolated auditors concurrently.

Canonical reports are promoted only after the process exits cleanly, model and usage telemetry is
complete, and the report/result/evidence contracts all validate against the frozen source hashes.
Failed partial outputs remain in their content-addressed attempt directories for diagnosis.
"""

from __future__ import annotations

import argparse
import contextlib
import concurrent.futures
import datetime as dt
import decimal
import fcntl
import functools
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable


HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
import phase1_runtime as runtime
import record_invocation
import run_coordinator
import lifecycle_journal
import verifier_contract
import claude_routes
import probe_bounds
import phase1_timeouts

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    import lock_wait  # noqa: E402
except ImportError:  # a standalone copy (e.g. a release's scripts/) without its sibling helper
    class lock_wait:  # noqa: N801
        @staticmethod
        def acquire_exclusive(handle, label):
            import fcntl as _fcntl
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)


# 2.7.2: the per-call timeout is a frozen per-audit value (scripts/phase1_timeouts.py): 2,700 s for
# every audit initialised before 2.7.2, 5,400 s for a new one. Each run reads its own audit's value
# (phase1_timeout); this name is kept for callers that still import it and is the legacy value.
DEFAULT_TIMEOUT_SECONDS = phase1_timeouts.LEGACY_PHASE1_TIMEOUT
MAXIMUM_TIMEOUT_SECONDS = max(phase1_timeouts.LIVE_PHASE1_TIMEOUTS)
DEFAULT_PRE_DISPATCH_DEADLINE_SECONDS = 60
DEFAULT_MAX_BUDGET_USD = "15"
# The capability probe's normalized-token bound, for the --safe-mode (non-bare) dispatch above.
# The CLI's own system prompt and tool schemas are paid on every call before the probe does any
# work; --bare used to strip that context, which is what the original 25,000 bound measured.
#
# 2.7.1: the bound is a frozen per-audit value, like the audit's Claude route (scripts/
# probe_bounds.py). It is part of the probe contract, which is hashed into probe_key (the probe
# cache key) and portable_probe_key (every auditor's input identity and completion receipt) and,
# through probe_key, into every verifier input. So an audit keeps the bound its snapshot froze
# (phase_1.runtime.probe_max_normalized_tokens) for its whole life, and the runtime admits exactly
# LIVE_PROBE_BOUNDS. Every audit initialised before 2.7.1 froze LEGACY_PROBE_BOUND and gets the
# 2.7.0 contract, key and packet bytes; a new audit freezes SHIPPED_PROBE_BOUND from
# config/pipeline-policy-v2.json. Nothing here defaults to SHIPPED_PROBE_BOUND: an audit's bound
# comes from its own snapshot (probe_bound), and a missing one is an error, never a re-key.
#
# 150,000 was calibrated in 2.3.x on 81,002 / 95,214 / 98,166-token minimal Sonnet/max probes
# (~1.5x headroom). The invocation ledgers under runs/, as of 2026-09-30T09:02Z, hold 39 probe
# calls in 28 audits (cache replays not counted):
# - Passed: 26, one per audit, 67,830 to 149,259 (median 122,621; 5-8 turns). 25 on Sonnet 5
#   (median 122,675), 1 on Sonnet 5.5 (93,846, an audit started on 2.7.0). The Sonnet 5.5 live
#   canary, run outside runs/, used 124,283.
# - Over the bound: 11, in 8 audits, failed "capability probe exceeded 150000 normalized tokens"
#   at 160,004 to 254,191 (median 178,045; 8-11 turns). 3 of them (166,079, 253,086, 254,191)
#   also failed the probe-contract check. The other 2 failures were "Not logged in", at 0 tokens.
# - Retries: all 8 audits passed on a re-run probe: 6 after one over-bound probe, 1 after two,
#   1 after three, each within 15 minutes of its first; the passing retries used 119,498-132,694.
# The programme lead chose 350,000: about 1.4x the worst probe observed (254,191) and about 2.3x
# the worst passing one (149,259). None of the 11 would have failed on tokens (the 3 contract
# failures still fail), and it still bounds a probe that loops: the largest probes averaged about
# 23,000 tokens a turn, so one that keeps calling tools passes 350,000 at roughly 13 to 15 turns.
# 300,000 and 500,000 were considered and not shipped.
LEGACY_PROBE_BOUND = probe_bounds.LEGACY_PROBE_BOUND
SHIPPED_PROBE_BOUND = probe_bounds.SHIPPED_PROBE_BOUND
LIVE_PROBE_BOUNDS = probe_bounds.LIVE_PROBE_BOUNDS
live_probe_bound = probe_bounds.live_probe_bound
PROBE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "nonce_sha256", "tool_contract_sha256", "output_file"],
    "properties": {
        "status": {"const": "complete"},
        "nonce_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "tool_contract_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "output_file": {"const": "probe.ok"},
    },
}


def probe_contract(bound: int) -> dict[str, Any]:
    """The capability-probe contract of an audit whose snapshot froze ``bound``.

    For LEGACY_PROBE_BOUND this is, key for key and in order, the PROBE_CONTRACT constant of 2.7.0
    and every release back to 2.3.x, so every existing audit's contract hash, probe key, portable
    probe key and PROBE_CONTRACT.json packet are unchanged. There is no default: the bound is the
    audit's own (probe_bound), and anything outside LIVE_PROBE_BOUNDS is refused.
    """
    if isinstance(bound, bool) or not isinstance(bound, int) or bound not in LIVE_PROBE_BOUNDS:
        raise RunnerError(
            f"the capability-probe bound {bound!r} is not a packaged probe bound "
            f"({probe_bounds.describe()} normalized tokens)"
        )
    return {
        "schema_version": "i4r-phase1-capability-probe-1",
        "structured_output_schema": PROBE_SCHEMA,
        "required_output": "probe.ok",
        # The probe used to prove only that the Write tool worked, which is the single filesystem
        # path that still succeeds when the sandbox rules are inert: Write takes a relative path
        # under the process cwd and is not seatbelt-confined. It therefore certified "Bash" and a
        # bash_allowlist it never exercised, and passed cleanly on a run where every sandboxed
        # Bash command was denied. These two outputs are produced by allowlisted Bash only.
        "required_bash_write": "evidence/bash.ok",
        "required_bash_read": "packet_copy.json",
        "tools": list(runtime.NON_BASH_TOOLS) + ["Bash"],
        "bash_allowlist": list(runtime.BASH_ALLOWLIST),
        "served_model_required": True,
        "per_model_usage_required": True,
        "maximum_normalized_tokens": bound,
    }


class RunnerError(runtime.Phase1Error):
    """Expected Phase-1 runner error."""


class ProviderLaunchError(RunnerError):
    """The local CLI process was not created."""


class EnvironmentFaultError(RunnerError):
    """The sandbox denied commands the policy grants, so the fault is local, not the model's."""


@dataclass
class ProviderExecution:
    reservation: dict[str, Any]
    attempt: int | None
    provider_request_status: str
    inference_started: bool
    provider_started: bool
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    started_at: dt.datetime
    ended_at: dt.datetime
    result: dict[str, Any]
    observed_model: str | None
    observed_model_ids: list[str]
    usage: dict[str, Any]
    cache_usage: str
    output: pathlib.Path
    attempt_dir: pathlib.Path
    # The snapshot route this call was dispatched on (an auditor's configured row, or the
    # verifier's route). 2.7.0: the terminal record and the served-model check read it; before,
    # both assumed Sonnet 5.
    route: dict[str, Any]
    requested_session_id: str | None = None


@dataclass(frozen=True)
class RoleCompletion:
    role: str
    status: str
    input_sha256: str
    invocation_id: str
    attempt: int | None
    report: str
    report_sha256: str
    evidence: str
    evidence_manifest_sha256: str
    reused: bool
    source_invocation_id: str | None = None


@dataclass(frozen=True)
class RoleTargets:
    """How one role's validated output is admitted, promoted and receipted.

    run_role and its repair/recovery paths take this as a keyword argument whose default,
    auditor_targets(<the role's route>), is exactly the 2.6.3 behaviour: validate_role_output and
    scientific_projection, promotion of the canonical report to report/ and the evidence to
    <work>/phase1_evidence/<input_hash>/, and the i4r-phase1-completion-1 receipt. The 2.6.4
    post-fan-out verifier (scripts/run_verifier.py) supplies its own admission contract, a
    content-addressed promotion to work/verifier/<role>/<input_hash>/ and its
    i4r-verifier-completion-1 receipt, plus the extra sandbox read denials its blind framing needs.

    promote(root, phase_root, execution, validated, role, input_hash, prompt_hash, attempt_kind,
            source_invocation_id) -> completion
    validate_receipt(root, receipt_path, input_hash) -> completion (records the cache replay)
    rebuild_receipt(root, phase_root, role, input_hash, snapshot_manifest, attempt_dir,
                    status_path, status, terminal_row, validated) -> completion
    """

    output: runtime.OutputContract
    promote: Callable[..., Any]
    validate_receipt: Callable[..., Any]
    rebuild_receipt: Callable[..., Any]
    # A fixed tuple, or a callable of one attempt's read roots (phase1_runtime.
    # resolve_extra_deny_read) when the denials depend on which attempt is running. Auditors keep
    # (), so their sandbox settings and every hash of them are the 2.6.3 values.
    extra_deny_read: runtime.ExtraDenyRead = ()
    # A verifier repair is recorded under the batch input hash that names its receipt (the value
    # verifier_contract.verify_record joins on); an auditor repair keeps its own repair hash.
    repair_records_original_input: bool = False
    # A focused repair recovered after a kill (state response_received, adopted by
    # recover_received_role_attempt) is validated against the byte-frozen projection its source
    # attempt wrote, exactly as try_focused_repair validates a live one. Only the verifier sets
    # it: the auditors keep the 2.6.3 recovery path unchanged in this release.
    freeze_recovered_repairs: bool = False


def auditor_targets(route: dict[str, Any] | None = None) -> RoleTargets:
    """The five auditors' targets, resolved at call time exactly as 2.6.3 resolved them.

    ``route`` is the auditors' snapshot route (load_policy); a completion replayed through these
    targets is recorded under it (2.7.0). run_role always passes it.
    """
    return RoleTargets(
        output=runtime.OutputContract(
            name="auditor", validate=runtime.validate_role_output,
            projection=runtime.scientific_projection,
        ),
        promote=promote_role,
        validate_receipt=functools.partial(validate_completed_receipt, route=route),
        rebuild_receipt=functools.partial(rebuild_auditor_receipt, route=route),
    )


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def elapsed(started: dt.datetime, ended: dt.datetime) -> float:
    value = (ended - started).total_seconds()
    if value < 0:
        raise RunnerError("terminal timestamp precedes start timestamp")
    return round(value, 3)


def relative_to(root: pathlib.Path, path: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise RunnerError(f"path escapes audit root: {path}") from exc


def role_identity(role: str) -> tuple[str, int | None]:
    """The (role_class, round) a Phase-1-runtime call is recorded under.

    The five auditors are ("auditor", 0). A 2.6.4 post-fan-out verifier batch, whose role is
    verifier_<12 hex> (verifier_contract.batch_role), is ("verifier", 0): round 0 keeps it clear
    of the critique-round authorization gate in lifecycle_journal.reserve_invocation. Anything
    else this runtime dispatches (the capability probe) stays ("other", None), as before.
    Downstream gates key on the recorded role_class, never on the name.
    """
    if role in runtime.ROLE_SPECS:
        return "auditor", 0
    if verifier_contract.ROLE_PATTERN.match(role):
        return verifier_contract.ROLE_CLASS, verifier_contract.ROUND
    return "other", None


def packaged_runtime(
    bound: int, timeout: int = phase1_timeouts.LEGACY_PHASE1_TIMEOUT,
) -> dict[str, Any]:
    """The packaged phase_1.runtime contract of an audit whose snapshot froze probe ``bound`` and
    per-call ``timeout``.

    For LEGACY_PROBE_BOUND and LEGACY_PHASE1_TIMEOUT it is the dict 2.7.0 required; for
    SHIPPED_PROBE_BOUND and LEGACY_PHASE1_TIMEOUT the dict 2.7.1 required. Only those two leaves differ.
    """
    return {
        "orchestrator": "scripts/run_phase1.py",
        "timeout_seconds": timeout,
        "pre_dispatch_deadline_seconds": DEFAULT_PRE_DISPATCH_DEADLINE_SECONDS,
        "max_provider_budget_usd": int(DEFAULT_MAX_BUDGET_USD),
        "probe_max_normalized_tokens": bound,
        "maximum_pre_inference_retryable_rejections_per_role": 2,
        "probe_cache_key_fields": [
            "cli_version", "requested_model", "reasoning_effort",
            "sandbox_policy_sha256", "probe_contract_sha256",
        ],
        "safe_mode": True,
        "network": "deny_all",
        "session_persistence": False,
        "parallelism": 5,
    }


def phase1_timeout(policy: dict[str, Any]) -> int:
    """The per-call Phase-1 timeout this audit's snapshot froze, as the packaged LIVE_PHASE1_TIMEOUTS int.

    2.7.2. load_policy calls it, so a snapshot load_policy admitted always has one; run_phase1.main and
    run_verifier read the timeout through it. A snapshot with no timeout gets load_policy's 2.7.0
    refusal; one outside LIVE_PHASE1_TIMEOUTS is named.
    """
    phase_1 = policy.get("phase_1") if isinstance(policy, dict) else None
    section = phase_1.get("runtime") if isinstance(phase_1, dict) else None
    if not isinstance(section, dict) or "timeout_seconds" not in section:
        raise RunnerError(
            "audit-scoped phase_1.runtime differs from the packaged execution contract"
        )
    frozen = section["timeout_seconds"]
    timeout = phase1_timeouts.live_phase1_timeout(frozen)
    if timeout is None:
        raise RunnerError(
            "audit-scoped phase_1.runtime differs from the packaged execution contract: its "
            f"timeout_seconds {frozen!r} is not a packaged Phase-1 timeout ({phase1_timeouts.describe()})"
        )
    return timeout


def probe_bound(policy: dict[str, Any]) -> int:
    """The capability-probe bound this audit's snapshot froze, as the packaged LIVE_PROBE_BOUNDS int.

    load_policy calls it, so a snapshot load_policy admitted always has one; the runners read the
    bound through it (run_phase1.main, run_verifier.plan_batch) and pass it to every probe_key,
    portable_probe_key and run_capability_probe call. A snapshot with no bound gets load_policy's
    2.7.0 refusal; one outside LIVE_PROBE_BOUNDS is named.
    """
    phase_1 = policy.get("phase_1") if isinstance(policy, dict) else None
    section = phase_1.get("runtime") if isinstance(phase_1, dict) else None
    if not isinstance(section, dict) or "probe_max_normalized_tokens" not in section:
        raise RunnerError(
            "audit-scoped phase_1.runtime differs from the packaged execution contract"
        )
    frozen = section["probe_max_normalized_tokens"]
    bound = live_probe_bound(frozen)
    if bound is None:
        raise RunnerError(
            "audit-scoped phase_1.runtime differs from the packaged execution contract: its "
            f"probe_max_normalized_tokens {frozen!r} is not a packaged probe bound "
            f"({probe_bounds.describe()})"
        )
    return bound


def load_policy(root: pathlib.Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The audit's frozen policy and its five auditor rows, after every packaged-contract check.

    2.7.1: the audit's probe bound is ``probe_bound(policy)`` (the return shape is 2.7.0's).
    """
    policy = record_invocation.load_policy_snapshot(root)
    record_invocation.validate_policy_binding(root)
    configured = policy.get("models", {}).get("phase_1_auditors")
    required = policy.get("phase_1", {}).get("required_roles")
    if (
        not isinstance(configured, dict)
        or set(configured) != set(runtime.ROLE_SPECS)
        or required != list(runtime.ROLE_SPECS)
    ):
        raise RunnerError("audit-scoped Phase-1 roles differ from the packaged quorum")
    if policy.get("phase_1", {}).get("parallelism") != 5:
        raise RunnerError("audit-scoped Phase-1 parallelism must be exactly five")
    observed_runtime = policy.get("phase_1", {}).get("runtime")
    # 2.7.1: every key is still exact except the probe bound, which may be any LIVE_PROBE_BOUNDS
    # value: the one this audit froze (probe_bound refuses any other, before any call). A legacy
    # snapshot is therefore judged against exactly the 2.7.0 dict. 2.7.2: likewise the per-call
    # timeout, any LIVE_PHASE1_TIMEOUTS value (phase1_timeout), so a 2.7.1 snapshot gets the 2.7.1 dict.
    if observed_runtime != packaged_runtime(probe_bound(policy), phase1_timeout(policy)):
        raise RunnerError(
            "audit-scoped phase_1.runtime differs from the packaged execution contract"
        )
    # 2.7.0: each auditor is bound to one of the packaged Claude routes (claude_routes.LIVE_ROUTES)
    # as this audit's own snapshot froze it -- Sonnet 5 before 2.7.0, Sonnet 5.5 after -- and never
    # to config/. All five share it: one capability probe and one portable probe key, both on the
    # first auditor's route, stand for every role.
    routes: dict[str, dict[str, str]] = {}
    for role, row in configured.items():
        try:
            routes[role] = claude_routes.require_live_route(row, role)
        except claude_routes.RouteError as exc:
            raise RunnerError(str(exc)) from exc
    if len({tuple(route.items()) for route in routes.values()}) != 1:
        raise RunnerError(
            "the five Phase-1 auditors must share one Claude route: "
            + "; ".join(f"{role}: {claude_routes.describe(route)}" for role, route in routes.items())
        )
    return policy, configured


def load_run(root: pathlib.Path) -> dict[str, Any]:
    run = runtime.strict_json(runtime.regular_file(root / "run.json", "run metadata"))
    if not isinstance(run.get("audit_id"), str) or not run["audit_id"]:
        raise RunnerError("run.json lacks a non-empty audit_id")
    return run


def cli_version(
    claude: str, deadline: runtime.MonotonicDeadline | None = None,
) -> str:
    try:
        result = subprocess.run(
            [claude, "--version"], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=deadline.subprocess_timeout("inspecting Claude CLI version", cap=15)
            if deadline is not None else 15,
            check=False, env=claude_routes.spawn_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f"cannot inspect Claude CLI version: {exc}") from exc
    version = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
    if result.returncode or not version:
        raise RunnerError("Claude CLI did not expose a version")
    return version


@contextlib.contextmanager
def hard_pre_dispatch_deadline(deadline: runtime.MonotonicDeadline):
    """Interrupt a blocked filesystem syscall as well as cooperative preparation scans."""
    if not hasattr(signal, "setitimer"):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(_signum: int, _frame: Any) -> None:
        raise RunnerError("Phase-1 pre-dispatch deadline exceeded")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(
        signal.ITIMER_REAL,
        deadline.remaining("arming the pre-dispatch deadline"),
    )
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


# Assembled rather than written so build_package.py's path scan does not read this source
# file as naming a real account; the model still sees the literal segment in its prompt.
HOME_SEGMENT = "/" + "Users/"

# 2.6.8: deposit personal data is reported, never a reason to stop. Auditors only: the post-fan-out
# verifier builds its own prompt (run_verifier.build_verifier_prompt), which stays byte-identical so
# published verifier records still re-admit. A module constant so the 2.6.3 input pin
# (tests/test_264_verifier_runtime.py) can strip exactly this paragraph.
AUDITOR_PII_REPORTING = """Personal data in the deposit is a finding, never a reason to stop, skip or leave something out.
Report it as a finding with disclosure_critical true that names the kind of identifier, the
manifest-relative file and column (or the directory and a file count when file names are themselves
participant codes), the rows or participants affected, whether anonymisation was claimed, and a
verified remedy.
Write it as a data-stewardship observation: counts and presence tests only, never an identifying
value, never a procedure for identifying a person. Keep concrete study dates and clock times on a
different line from any sentence that mentions participants."""


def build_role_prompt(role: str, audit_id: str, packet: dict[str, Any]) -> str:
    spec = runtime.ROLE_SPECS[role]
    # 2.7.0: the model named is the packet's own route. A Sonnet 5 packet, or one with no model
    # block, renders the 2.6.10 bytes exactly: the prompt hash is in every role input hash.
    # 2.7.3: so is the effort (claude_routes.effort_of): a max packet, or one with no model block,
    # renders the 2.7.2 bytes; a new audit's xhigh packet says "at xhigh effort".
    model = claude_routes.display_name(claude_routes.requested_model_of(packet))
    effort = claude_routes.effort_of(packet)
    return f"""You are an independent I4R Phase-1 auditor. Use {model} at {effort} effort.

Audit identity: {audit_id}
Role: {role} (Area {spec['area']}: {spec['name']})
Immutable source snapshot: {packet['source_snapshot_sha256']}

Read ROLE_PACKET.json in the role packet directory, then inspect only the assigned deterministic
indexes under that role packet's evidence/ directory. Verify every claim-critical point against the
raw, read-only paper/ and pkg/ material exposed directly under the source directory. The indexes
are routing aids, never conclusions. Do not access the live audit, another role or attempt, the
internet, plugins, browsers, MCP servers, authors, or journals. Never infer intent. Never reproduce
a participant identifier beside a name, date, timestamp, or clock time. Copy code that may write
beside its inputs into this attempt output directory before running it, then promote or delete
those working copies before you return -- see the closed output contract below.

Cover the role's assigned checklist surface first, then deepen only the two or three uncertainties
most likely to change the audit. Test plausible benign explanations. Flag conclusion-changing and
disclosure-critical claims explicitly so they retain the two review paths recorded in ROLE_PACKET.
Record successful checks and rejected hypotheses as carefully as defects.

{AUDITOR_PII_REPORTING}

Work plan. Every tool call re-reads your whole context, so cost scales with the number of turns,
not the amount of analysis. Aim to finish in well under 40 turns: read the packet and the raw
sources first; then write ONE analysis script (Python or R) that performs all of your planned
checks and writes its results as JSON files under evidence/; run it once and fix it rather than
issuing one command per check; then write REPORT.md from those results. Do not re-verify a value
you have already computed, and do not compute hashes by hand when a script can emit them.

Write incrementally, but return only after these three outputs are complete:

* REPORT.md with exactly these level-2 sections: Result summary; Brief corrections; Checks performed
  and passed; Findings; Tested hypotheses not confirmed; Open questions; Evidence inventory. Every
  finding states CONFIRMED, LIKELY, or SPECULATIVE and carries four labelled elements, spelled with
  these words: *Evidence*: what was checked and what it showed; *Benign explanations tested*: which
  and what they showed; *Impact*: on published conclusions; *Remedy*: verified. The validator looks
  for the words "evidence", "benign", "impact" and "remedy" inside the Findings section, so a finding
  that describes its evidence without using the word is rejected. Include at least one exact source join copied from
  ROLE_PACKET.source_manifest as <!-- source:path@sha256 --> and finish with exactly
  <!-- i4r-role-complete:{role} -->. Never write an absolute filesystem path anywhere in the
  report OR in result.json: refer to deposited files by their manifest-relative paths such as
  pkg/analysis.R. A single absolute path rejects the whole role, and `{HOME_SEGMENT}`, `/home/`,
  `/private/tmp/` and `/private/var/` all count. This applies to paths you found INSIDE the
  deposit too: when reporting that a deposited script hardcodes an absolute path, mask the
  home-directory prefix as `<windows-user-dir>/rest/of/path` or `<unix-user-dir>/rest/of/path`
  and keep the rest. The finding survives that masking; the authors' account name is not yours
  to republish, and an unmasked path blocks the whole audit downstream. No line may place a participant identifier and a date or clock time
  together. {runtime.provider_safety.RULE_STATEMENT} No line anywhere in the report may consist solely of TODO, TBD, PLACEHOLDER, or a
  bracketed insert marker; finish every section instead of leaving a stub.
* evidence/manifest.json plus at least one evidence file. The manifest has schema_version
  `i4r-phase1-role-result-2`, role, source_snapshot_sha256, non-empty source_bindings copied exactly
  from ROLE_PACKET.source_manifest as objects keyed `path` and `sha256`, and non-empty artifacts
  rows with path, sha256, and bytes. Every source hash you cite anywhere -- report joins,
  source_bindings, and finding evidence -- must come from ROLE_PACKET.source_manifest, which lists
  only the deposited paper/ and pkg/ files. NEVER cite the packet's own ROLE_PACKET.assigned_evidence
  index files (pdf_text.txt, file_inventory.json, workbook_summaries.json, code_entrypoints.json,
  environment_fingerprint.json, privacy_scan.json): they are analysis aids, not deposited sources,
  and listing one as a source binding rejects the whole role. Each artifact `path`
  is relative to the evidence directory itself and must NOT begin with `evidence/` or `./`: a file
  written to evidence/scripts/check.R is the row path `scripts/check.R`, never
  `./scripts/check.R`. It closes over every
  evidence file except itself. Evidence contains rerunnable scripts or aggregate results, never
  participant rows.
* result.json with schema_version `i4r-phase1-role-result-2`, role, status=`complete`,
  completion_marker=true (a JSON boolean), source_snapshot_sha256, report, evidence_manifest, and
  scientific_fields. `report` is exactly {{"path": "REPORT.md", "sha256": <hash>}} and
  `evidence_manifest` is exactly {{"path": "evidence/manifest.json", "sha256": <hash>}}: those two
  path strings are compared literally, so do not write "./REPORT.md" or any other spelling.
  scientific_fields is a compact JSON object with exactly
  these three keys, spelled this way and no others: `passed_check_ids` (array of unique non-empty
  ID strings), `open_question_ids` (array of unique non-empty ID strings), and `findings`. Do not
  use `passed_checks` or `open_questions`; the exact key set is validated and a mismatch rejects
  the whole role. Each finding has exactly id, status,
  claim, evidence, impact, remedy, conclusion_changing, and disclosure_critical. `id` is a unique
  non-empty string; `status` is exactly one of CONFIRMED, LIKELY or SPECULATIVE; `claim`, `impact`
  and `remedy` are non-empty strings; `conclusion_changing` and `disclosure_critical` are JSON
  booleans, never the strings "true"/"false". Evidence is a
  non-empty array of exact {{source_path,source_sha256,result}} records, and every one of them must
  name a DEPOSITED source from ROLE_PACKET.source_manifest with that manifest's exact sha256. Cite the
  deposited file the check ran against, never one of your own evidence scripts or result files, and
  put what you observed in `result`.
  This complete scientific record, the report bytes, and every evidence-artifact byte are
  authoritative during a focused format-only repair and must not change in such a repair.

The output directory is a CLOSED contract. When you return it must contain exactly REPORT.md,
result.json, evidence/manifest.json, and the evidence files listed in that manifest -- nothing else.
Any scratch copy, working script, intermediate log or staging directory you created must either be
promoted into evidence/ and listed in the manifest, or deleted before you finish. Leftover work/,
scratch/ or temp files reject the whole role, so do this cleanup as your final step.

Write result.json last. The report {{path,sha256}} and evidence_manifest {{path,sha256}} entries
must hash the final bytes on disk, so make every edit to REPORT.md and to each evidence file first,
then compute the hashes, then write result.json. If you edit any of those files afterwards you must
recompute the hashes and rewrite result.json. Validate all hashes and paths locally before
returning. Your final response is only a short completion notice; the files are authoritative.
"""


def materialize_role_packets(
    phase_root: pathlib.Path,
    snapshot: pathlib.Path,
    snapshot_manifest: dict[str, Any],
    configured: dict[str, Any],
    audit_id: str,
    deadline: runtime.MonotonicDeadline | None = None,
) -> tuple[dict[str, pathlib.Path], dict[str, dict[str, Any]], dict[str, str]]:
    base = phase_root / "role_packets" / snapshot_manifest["snapshot_sha256"]
    runtime.validate_destination(phase_root, base, "immutable role packet store")
    base.mkdir(parents=True, exist_ok=True)
    paths: dict[str, pathlib.Path] = {}
    packets: dict[str, dict[str, Any]] = {}
    prompts: dict[str, str] = {}
    runtime.check_deadline(deadline, "loading Phase-1 packet templates")
    checklist = runtime.regular_file(SKILL / "reference/checklist.md", "Phase-1 checklist").read_text(encoding="utf-8")
    brief_template = runtime.regular_file(SKILL / "reference/brief_template.md", "Phase-1 brief template").read_text(encoding="utf-8")
    for role in runtime.ROLE_SPECS:
        runtime.check_deadline(deadline, f"materializing {role} packet")
        packet = runtime.make_role_packet(role, snapshot_manifest, configured[role])
        prompt = build_role_prompt(role, audit_id, packet)
        contract = {
            "packet_layout_version": 2,
            "packet": packet,
            "checklist_sha256": runtime.sha256_bytes(checklist.encode("utf-8")),
            "brief_template_sha256": runtime.sha256_bytes(brief_template.encode("utf-8")),
            "prompt_sha256": runtime.sha256_bytes(prompt.encode("utf-8")),
        }
        packet_hash = runtime.sha256_bytes(runtime.canonical_bytes(contract))
        target = base / role / packet_hash
        runtime.validate_destination(phase_root, target, f"{role} immutable packet")
        if not target.exists():
            temporary = pathlib.Path(tempfile.mkdtemp(prefix=f"{role}-", dir=base))
            try:
                runtime.atomic_write(temporary / "ROLE_PACKET.json", runtime.pretty_bytes(packet))
                runtime.atomic_write(temporary / "CHECKLIST.md", checklist.encode("utf-8"))
                runtime.atomic_write(temporary / "BRIEF_TEMPLATE.md", brief_template.encode("utf-8"))
                for row in packet["assigned_evidence"]:
                    runtime.check_deadline(deadline, f"copying {role} evidence indexes")
                    source = runtime.beneath(
                        snapshot / "evidence", snapshot / "evidence" / row["path"],
                        f"{role} assigned evidence",
                    )
                    runtime.regular_file(source, f"{role} assigned evidence")
                    if source.stat().st_size != row["bytes"] or runtime.sha256_file(source) != row["sha256"]:
                        raise RunnerError(f"assigned evidence changed before packet creation: {row['path']}")
                    destination = runtime.beneath(
                        temporary, temporary / "evidence" / row["path"],
                        f"{role} packet evidence",
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                    if runtime.sha256_file(destination) != row["sha256"]:
                        raise RunnerError(f"assigned evidence copy changed bytes: {row['path']}")
                packet_files = []
                for path in temporary.rglob("*"):
                    runtime.check_deadline(deadline, f"traversing {role} packet staging")
                    if path.is_file() and not path.is_symlink():
                        packet_files.append(path)
                packet_files.sort()
                runtime.check_deadline(deadline, f"closing {role} packet manifest")
                runtime.atomic_write(temporary / "packet_manifest.json", runtime.pretty_bytes({
                    "schema_version": "i4r-phase1-role-packet-1",
                    "role": role,
                    "packet_sha256": packet_hash,
                    "files": [
                        {
                            "path": path.relative_to(temporary).as_posix(),
                            "sha256": runtime.sha256_file(path),
                            "bytes": path.stat().st_size,
                        }
                        for path in packet_files
                    ],
                }))
                # An abort between publish and freeze left a role packet with a writable root,
                # which materialize_role_packets then reused via its `if not target.exists()`
                # short-circuit without ever rechecking modes.
                runtime.freeze_and_publish(temporary, target, deadline)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)
        manifest = runtime.strict_json(runtime.regular_file(target / "packet_manifest.json", "role packet manifest"))
        if (
            manifest.get("schema_version") != "i4r-phase1-role-packet-1"
            or manifest.get("packet_sha256") != packet_hash
            or manifest.get("role") != role
            or not isinstance(manifest.get("files"), list)
        ):
            raise RunnerError(f"immutable role packet collision for {role}")
        bound_paths: set[str] = set()
        for row in manifest["files"]:
            runtime.check_deadline(deadline, f"verifying {role} packet")
            if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
                raise RunnerError(f"invalid immutable role packet row for {role}")
            path = runtime.beneath(target, target / row["path"], "role packet file")
            runtime.regular_file(path, "role packet file")
            relative = path.relative_to(target.resolve()).as_posix()
            if relative in bound_paths:
                raise RunnerError(f"duplicate immutable role packet path: {role}/{relative}")
            bound_paths.add(relative)
            if path.stat().st_size != row["bytes"] or runtime.sha256_file(path) != row["sha256"]:
                raise RunnerError(f"immutable role packet changed: {role}/{row['path']}")
        actual_paths: set[str] = set()
        for path in target.rglob("*"):
            runtime.check_deadline(deadline, f"traversing {role} packet")
            if path.is_symlink():
                raise RunnerError(f"immutable role packet contains a symlink: {role}/{path.name}")
            if path.is_file():
                actual_paths.add(path.relative_to(target).as_posix())
        if actual_paths != bound_paths | {"packet_manifest.json"}:
            raise RunnerError(f"immutable role packet manifest does not close over files for {role}")
        paths[role], packets[role], prompts[role] = target, packet, prompt
    return paths, packets, prompts


# Turn count is the dominant Phase-1 cost: every turn re-reads the whole accumulated
# context, so a role that takes 100 turns spends ~17M cached tokens on ~180k of output.
# The steer therefore optimises for few, decisive tool calls. Claude Code checks every
# sub-command of a compound command against the allowlist in phase1_runtime.BASH_ALLOWLIST;
# a denied command is recorded as telemetry rather than failing the role, but it still
# wastes a turn, and redirection is never matched, so scripts must write their own files.
BASH_STEER = (
    "Work in as few tool calls as you can. Bash calls are checked against a fixed "
    "allowlist of common commands (python3, Rscript, mkdir, cp, mv, rm, ls, cat, head, "
    "tail, grep, awk, sed, find, shasum and similar) and every part of a compound command "
    "must be on it, so chaining allowed commands with && or | is fine. Redirection (>, >>, "
    "2>&1) and heredocs are never allowed: have your scripts write their own output files. "
    "A denied command is logged and costs you a turn; it does not fail the role."
)
# The probe is a health check, not an audit: re-deriving the values it was handed is what
# pushed it past its token bound and into denied shell pipelines.
PROBE_STEER = BASH_STEER + (
    " Execute exactly the requested steps with the fewest tool calls. Do not verify or "
    "re-derive any value the instructions already supply."
)


def claude_command(
    claude: str,
    settings: pathlib.Path,
    source: pathlib.Path,
    packet: pathlib.Path,
    session_id: str,
    model_alias: str,
    effort: str,
    max_budget_usd: str,
    *,
    json_schema: dict[str, Any] | None = None,
    system_steer: str = BASH_STEER,
) -> list[str]:
    command = [
        claude,
        "--print",
        "--output-format", "json",
        "--model", model_alias,
        "--effort", effort,
        # --safe-mode disables every customization (CLAUDE.md, skills, plugins, hooks,
        # MCP servers, custom commands and agents) while auth, model selection, tools and
        # permissions keep working. That is the isolation this contract needs.
        # --bare is deliberately NOT passed. It additionally refuses to read OAuth and
        # keychain credentials, so a subscription-authenticated Claude CLI cannot start a
        # single auditor. run_coordinator.py has always dispatched without it.
        "--safe-mode",
        "--append-system-prompt", system_steer,
        "--no-chrome",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--settings", str(settings),
        "--add-dir", str(source), str(packet),
        "--no-session-persistence",
        "--session-id", session_id,
        "--max-budget-usd", max_budget_usd,
        "--permission-mode", "dontAsk",
        "--tools", ",".join([*runtime.NON_BASH_TOOLS, "Bash"]),
        "--allowedTools", *runtime.NON_BASH_TOOLS, *runtime.BASH_ALLOWLIST,
    ]
    if json_schema is not None:
        command.extend(["--json-schema", json.dumps(json_schema, sort_keys=True, separators=(",", ":"))])
    return command


def parse_result(stdout: str) -> dict[str, Any]:
    return run_coordinator.parse_provider_result(stdout)


def response_payload(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("structured_output", result.get("result"))
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RunnerError("provider structured response is not JSON") from exc
        if isinstance(parsed, dict):
            return parsed
    raise RunnerError("provider result lacks one structured JSON response")


def usage_by_model(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    raw = result.get("modelUsage")
    if not isinstance(raw, dict):
        return rows
    mapping = {
        "input_tokens": "inputTokens",
        "output_tokens": "outputTokens",
        "cached_input_tokens": "cacheReadInputTokens",
        "cache_creation_input_tokens": "cacheCreationInputTokens",
        "reasoning_tokens": "reasoningTokens",
    }
    for model, counts in raw.items():
        if not isinstance(model, str) or not isinstance(counts, dict):
            continue
        usage = {name: counts.get(source) if isinstance(counts.get(source), int) else None for name, source in mapping.items()}
        disjoint = [usage[name] for name in (
            "input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens"
        )]
        complete = all(value is not None for value in disjoint)
        usage.update({
            "obtainable": any(value is not None for value in usage.values()),
            "total_tokens": sum(disjoint) if complete else None,
            "source": "cli_log",
            "unavailable_reasons": {
                name: "Claude CLI per-model telemetry omitted this component"
                for name, value in usage.items() if value is None
            },
            "total_tokens_kind": "derived" if complete else "unavailable",
            "total_tokens_components": [
                "input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens"
            ] if complete else [],
            "total_tokens_unavailable_reason": None if complete else
            "Claude CLI per-model telemetry lacks enough disjoint components",
        })
        rows.append({"model": model, "identity_assurance": "observed", "usage": usage})
    return rows


def usage_attribution(
    result: dict[str, Any], aggregate: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Partition usage only where provider telemetry supports an exact arithmetic split.

    The invocation-level aggregate remains authoritative.  With no model partitions it is copied
    to the unattributed bucket.  With incomplete partitions, the bucket contains only exact
    non-negative residuals; components that cannot be subtracted honestly remain explicitly
    unavailable rather than being assigned to the requested model.
    """
    partitions = usage_by_model(result)
    if not partitions:
        return partitions, json.loads(json.dumps(aggregate))
    components = (
        "input_tokens", "output_tokens", "cached_input_tokens",
        "cache_creation_input_tokens", "reasoning_tokens",
    )
    residual: dict[str, int | None] = {}
    reasons: dict[str, str] = {}
    completely_attributed = True
    for name in components:
        aggregate_value = aggregate.get(name)
        values = [row["usage"].get(name) for row in partitions]
        numeric = [
            value for value in values
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        ]
        if not isinstance(aggregate_value, int) or isinstance(aggregate_value, bool):
            residual[name] = None
            reasons[name] = "invocation aggregate omitted this component"
            completely_attributed = False
            continue
        remainder = aggregate_value - sum(numeric)
        if remainder < 0:
            residual[name] = None
            reasons[name] = "model-partition sum exceeds the invocation aggregate"
            completely_attributed = False
            continue
        residual[name] = remainder
        if len(numeric) != len(partitions) or remainder != 0:
            completely_attributed = False
    if completely_attributed:
        return partitions, None
    total_components = [
        "input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens",
    ]
    if all(residual[name] is not None for name in total_components):
        total = sum(int(residual[name]) for name in total_components)
        total_kind = "derived"
        total_reason = None
        used_components = total_components
    else:
        total = None
        total_kind = "unavailable"
        total_reason = "exact unattributed total cannot be derived from the exposed partitions"
        used_components = []
    any_observed = any(value is not None for value in residual.values()) or total is not None
    unattributed = {
        "obtainable": any_observed,
        **residual,
        "total_tokens": total,
        "source": "cli_log" if any_observed else "unavailable",
        "unavailable_reasons": reasons,
        "total_tokens_kind": total_kind,
        "total_tokens_components": used_components,
        "total_tokens_unavailable_reason": total_reason,
    }
    return partitions, unattributed


def metadata_21(root: pathlib.Path) -> bool:
    try:
        return runtime.strict_json(root / "run.json").get("schema_version") == "2.1.0"
    except runtime.Phase1Error:
        return False


def journal_pause(root: pathlib.Path, phase_root: pathlib.Path, reason: str, detail: str) -> None:
    if not metadata_21(root):
        return
    status_path = phase_root / "status.json"
    lifecycle_journal.append_event(root, "pause_start", {
        "reason": reason,
        "detail": detail,
        "phase": "fan_out",
        "receipt": relative_to(root, status_path),
        "receipt_sha256": runtime.sha256_file(status_path) if status_path.is_file() else None,
    }, source="run_phase1.py")


def journal_snapshot_refreeze(
    root: pathlib.Path, snapshot: pathlib.Path, facts: dict[str, Any], context: str,
    *, source: str = "run_phase1.py",
) -> None:
    """One note for a Phase-1 snapshot this process re-froze (mode-only drift, content intact).

    Never call it inside hard_pre_dispatch_deadline -- SIGALRM could interrupt the append -- and
    never under a dry run. The payload is audit-relative and depth-limited: run.events.jsonl is
    projected into run.json, which is coordinator context and a release artefact.
    """
    print(
        f"  [note ] Phase-1 snapshot mode drift re-frozen ({facts['path_count']} path(s) under "
        f"{', '.join(facts['paths'])}; context {context}); content verified unchanged",
        file=sys.stderr,
    )
    if not metadata_21(root):
        return
    try:
        lifecycle_journal.append_event(root, "note", {
            "text": (
                f"Frozen Phase-1 snapshot regained write permission on {facts['path_count']} "
                "path(s) with its content unchanged (a synced folder re-applying a mode is the "
                "usual cause); it was re-frozen in place. No hash or input changed."
            ),
            "kind": "snapshot_mode_refrozen",
            "snapshot": relative_to(root, snapshot),
            "snapshot_kind": "phase1",
            "context": context,
            "path_count": facts["path_count"],
            "paths": list(facts["paths"]),
        }, source=source)
    except Exception as exc:  # the note records a harmless chmod; never fail a role over it
        print(f"  [warn ] could not journal the snapshot re-freeze note: {exc}", file=sys.stderr)


def journal_resume(root: pathlib.Path) -> None:
    if not metadata_21(root):
        return
    run = runtime.strict_json(root / "run.json")
    reason = run.get("pause_reason")
    if reason in {"local_preflight_failed", "capability_probe_failed"}:
        lifecycle_journal.append_event(
            root, "pause_end", {"reason": reason, "phase": "fan_out"},
            source="run_phase1.py",
        )


def journal_phase(root: pathlib.Path, event_type: str, **payload: Any) -> None:
    if metadata_21(root):
        lifecycle_journal.append_event(
            root, event_type, {"phase": "fan_out", **payload}, source="run_phase1.py"
        )


def cache_replay_usage() -> dict[str, Any]:
    reasons = {
        name: "cache replay made no provider request"
        for name in (
            "input_tokens", "output_tokens", "cached_input_tokens",
            "cache_creation_input_tokens", "reasoning_tokens",
        )
    }
    return {
        "obtainable": False,
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "cache_creation_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "source": "unavailable",
        "unavailable_reasons": reasons,
        "total_tokens_kind": "unavailable",
        "total_tokens_components": [],
        "total_tokens_unavailable_reason": "cache replay made no provider request",
    }


def record_cache_replay(
    root: pathlib.Path,
    *,
    role: str,
    role_class: str,
    round_number: int | None,
    source_invocation_id: str,
    input_hash: str,
    output_artifact: str,
    output_sha256: str,
    cache_usage: str,
    route: dict[str, Any],
) -> None:
    """Journal the reuse of a completed call, under the route the role is bound to (2.7.0)."""
    if not metadata_21(root):
        return
    now = utc_now()
    record = {
        "invocation_id": f"cache-{uuid.uuid4()}",
        "role": role,
        "role_class": role_class,
        "round": round_number,
        "attempt": None,
        "status": "completed",
        "provider": "anthropic",
        "provider_call": False,
        "cache_usage": cache_usage,
        "session_id": None,
        "requested_model": route["requested_model"],
        "observed_model": None,
        "model_family": "claude",
        "model_source": "unavailable",
        "reasoning_effort": route["reasoning_effort"],
        "started_at": iso(now),
        "ended_at": iso(now),
        "elapsed_seconds": 0,
        "input_artifact_sha256": input_hash,
        "prompt_sha256": None,
        "output_artifact": output_artifact,
        "output_sha256": output_sha256,
        "usage": cache_replay_usage(),
        "error": None,
        "error_class": None,
        "legacy_source": None,
        "attempt_kind": "cache_replay",
        "provider_request_status": "not_requested",
        "inference_start_status": "not_started",
        "source_invocation_id": source_invocation_id,
        "turn_count": 0,
        "provider_cost": None,
        "served_model": None,
        "identity_assurance": "requested_only",
        "model_identity_evidence": None,
        "usage_by_model": [],
        "unattributed_usage": None,
        "provider_duration_seconds": 0,
    }
    record_invocation.validate_record(record, root)
    record_invocation.record_cache_reuse(root, record)


def lifecycle_reserve(
    root: pathlib.Path, role: str, attempt_kind: str, input_hash: str, prompt_hash: str,
    *, route: dict[str, Any], source_invocation_id: str | None = None,
) -> dict[str, Any]:
    role_class, round_number = role_identity(role)
    # The route the call is dispatched on, from the audit snapshot: --recover-stranded rebuilds a
    # killed attempt's terminal record from this reservation.
    reservation = {
        "role": role,
        "role_class": role_class,
        "round": round_number,
        "attempt_kind": attempt_kind,
        "provider": "anthropic",
        "requested_model": route["requested_model"],
        "reasoning_effort": route["reasoning_effort"],
        "input_artifact_sha256": input_hash,
        "prompt_sha256": prompt_hash,
        "source_invocation_id": source_invocation_id,
    }
    if metadata_21(root) and hasattr(record_invocation, "reserve_invocation"):
        return record_invocation.reserve_invocation(root, reservation)
    # Compatibility for pre-2.1 workspaces while this release is being migrated.  The terminal
    # validator remains authoritative; calls for different roles cannot collide.
    rows = read_ledger(root)
    attempts = [
        row.get("attempt") for row in rows
        if row.get("provider_call") and row.get("role") == role and row.get("round") == reservation["round"]
    ]
    return {
        "invocation_id": f"phase1-{role}-{uuid.uuid4()}",
        "attempt": None,
        "attempt_candidate": len(attempts) + 1,
    }


def lifecycle_requested(root: pathlib.Path, reservation: dict[str, Any]) -> None:
    """Durably record a provider dispatch without pretending inference began."""
    if metadata_21(root) and hasattr(record_invocation, "mark_provider_request"):
        record_invocation.mark_provider_request(
            root, reservation["invocation_id"], status="requested",
        )


def lifecycle_started(root: pathlib.Path, reservation: dict[str, Any]) -> int:
    if metadata_21(root) and hasattr(record_invocation, "mark_inference_start"):
        started = record_invocation.mark_inference_start(root, reservation["invocation_id"])
        attempt = started.get("attempt")
        # The ceiling is the journal's own (3 by default, I4R_MAX_ATTEMPTS up to 20), which
        # mark_inference_start has just enforced. A hard-coded 3 here raised on a legitimately
        # allocated fourth start -- after inference_start was journaled and the response written,
        # but with status.json still at provider_requested -- so the strand guard refused every
        # later run and --recover-stranded could not clear it (it keeps a retained response).
        limit = lifecycle_journal.max_attempts_per_role()
    else:
        attempt = reservation.get("attempt_candidate")
        # A legacy ledger (record_invocation.append) keeps its fixed three-attempt budget.
        limit = 3
    if not isinstance(attempt, int) or attempt < 1 or attempt > limit:
        raise RunnerError(f"lifecycle did not allocate a valid 1..{limit} inference attempt")
    return attempt


def workspace_files(directory: pathlib.Path) -> frozenset[str]:
    """The regular files under an attempt's output workspace, as relative POSIX paths.

    Symlinks are neither followed nor counted: only a file the call itself created counts."""
    if not directory.is_dir():
        return frozenset()
    found = set()
    for base, dirs, files in os.walk(directory, followlinks=False):
        for name in files:
            path = pathlib.Path(base) / name
            if path.is_file() and not path.is_symlink():
                found.add(path.relative_to(directory).as_posix())
    return frozenset(found)


def workspace_writes(directory: pathlib.Path, preexisting: frozenset[str]) -> bool:
    """Whether the call wrote a file into its output workspace (2.7.2).

    The output workspace is written only by the model's own tool calls, so a new file proves the
    provider accepted the request and ran inference. It is the evidence a call leaves when it runs
    past the provider timeout: the killed CLI prints no result envelope, which inference_evidence
    needs. Without it every such call was booked as a request the provider never accepted, and two
    of them refused the role for good (maximum_pre_inference_retryable_rejections_per_role) although
    both had run for the full timeout. A request rejected before inference writes nothing, so it is
    still booked as not started."""
    return bool(workspace_files(directory) - preexisting)


def inference_evidence(result: dict[str, Any]) -> bool:
    """Return true only when the returned envelope proves inference started.

    A rejected authentication/quota/schema/capability request commonly has an error
    envelope but no served model, usage, turns, cost, or provider duration.  Merely
    launching the local CLI is therefore not evidence of an accepted inference.
    """
    if not isinstance(result, dict) or not result:
        return False
    _primary, _ids, model_count = run_coordinator.observed_models(result)
    if model_count:
        return True
    usage, _cache = run_coordinator.extract_usage(result)
    for field in (
        "input_tokens", "output_tokens", "cached_input_tokens",
        "cache_creation_input_tokens", "reasoning_tokens", "total_tokens",
    ):
        value = usage.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
    for field in ("num_turns", "turn_count", "duration_api_ms", "total_cost_usd"):
        value = result.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
    # A successful result envelope itself proves that the request crossed the
    # provider's inference boundary even if an older adapter omitted telemetry.
    return result.get("is_error") is False and result.get("subtype") not in {"error", "failed"}


def rejected_provider_request(result: dict[str, Any], stderr: str) -> bool:
    """Classify explicit pre-inference provider rejection versus unknown transport loss."""
    if not isinstance(result, dict) or not result:
        return False
    if not (result.get("is_error") or result.get("subtype") in {"error", "failed"}):
        return False
    message = " ".join((str(result.get("result") or ""), stderr)).lower()
    return any(token in message for token in (
        "authentication", "authorization", "unauthorized", "forbidden", "quota",
        "billing", "unsupported model", "model not found", "schema", "capability",
        "429", "throttl", "rate limit", "overloaded", "500", "502", "503", "504",
    ))


def unavailable_usage() -> dict[str, Any]:
    return {
        "obtainable": False,
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "cache_creation_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "source": "unavailable",
        "unavailable_reasons": {
            "input_tokens": "provider process did not expose usage",
            "output_tokens": "provider process did not expose usage",
            "cached_input_tokens": "provider process did not expose usage",
            "cache_creation_input_tokens": "provider process did not expose usage",
            "reasoning_tokens": "provider process did not expose usage",
        },
        "total_tokens_kind": "unavailable",
        "total_tokens_components": [],
        "total_tokens_unavailable_reason": "provider process did not expose exact token usage",
    }


def observed_or_requested_session(execution: ProviderExecution) -> str | None:
    """Prefer the session the provider reported; fall back to the one we asked for.

    The observed id is absent in exactly the failure modes where a session identity is most
    useful -- a provider rejection returns no envelope at all -- so the requested id, which
    used to be minted inline and discarded, is the only record that survives.
    """
    observed = execution.result.get("session_id") if isinstance(execution.result, dict) else None
    if isinstance(observed, str) and observed:
        return observed
    if execution.provider_started and execution.requested_session_id:
        return execution.requested_session_id
    return None


def make_terminal_record(
    execution: ProviderExecution,
    role: str,
    attempt_kind: str,
    input_hash: str,
    prompt_hash: str,
    *,
    status: str,
    output_artifact: str | None,
    output_hash: str | None,
    error: str | None,
    error_class: str | None,
    source_invocation_id: str | None = None,
) -> dict[str, Any]:
    provider_request_status = execution.provider_request_status
    inference_start_status = "started" if execution.inference_started else "not_started"
    served = execution.observed_model
    stdout_artifact = execution.attempt_dir / "claude_stdout.json"
    identity_evidence = {
        "source": "cli_log" if served else "unavailable",
        "raw_ids": execution.observed_model_ids,
        "artifact": relative_to(execution.attempt_dir.parents[4], stdout_artifact)
        if served and len(execution.attempt_dir.parents) >= 5 and stdout_artifact.is_file() else None,
        "artifact_sha256": runtime.sha256_file(stdout_artifact)
        if served and stdout_artifact.is_file() else None,
    }
    cost = execution.result.get("total_cost_usd")
    provider_cost = (
        {"amount": float(cost), "currency": "USD", "source": "cli_log", "unavailable_reason": None}
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0 else None
    )
    turns = execution.result.get("num_turns", execution.result.get("turn_count"))
    partitions, unattributed = usage_attribution(execution.result, execution.usage)
    role_class, round_number = role_identity(role)
    record: dict[str, Any] = {
        "invocation_id": execution.reservation["invocation_id"],
        "role": role,
        "role_class": role_class,
        "round": round_number,
        "attempt": execution.attempt,
        "status": status,
        "provider": "anthropic",
        "provider_call": execution.provider_started,
        "cache_usage": execution.cache_usage if execution.provider_started else "not_applicable",
        "session_id": observed_or_requested_session(execution),
        "requested_model": execution.route["requested_model"],
        "observed_model": served,
        "observed_model_ids": execution.observed_model_ids or None,
        "model_family": "claude",
        "model_source": "observed" if served else "declared",
        "reasoning_effort": execution.route["reasoning_effort"],
        "started_at": iso(execution.started_at),
        "ended_at": iso(execution.ended_at),
        "elapsed_seconds": elapsed(execution.started_at, execution.ended_at),
        "input_artifact_sha256": input_hash,
        "prompt_sha256": prompt_hash,
        "output_artifact": output_artifact,
        "output_sha256": output_hash,
        "usage": execution.usage,
        # Journaled and projected into run.json, which is later provider-visible: never let an
        # absolute path through (see run_coordinator.redact_private_paths).
        "error": run_coordinator.redact_private_paths(
            error, *([execution.attempt_dir.parents[4]] if len(execution.attempt_dir.parents) >= 5 else []),
        ) if error else error,
        "error_class": error_class,
        "legacy_source": None,
        # Metadata 2.1 fields.  record_terminal strips/normalizes these only for a legacy audit.
        "attempt_kind": attempt_kind,
        "provider_request_status": provider_request_status,
        "inference_start_status": inference_start_status,
        "source_invocation_id": source_invocation_id,
        "turn_count": turns if isinstance(turns, int) and turns >= 0 else None,
        "provider_cost": provider_cost,
        "served_model": served,
        "identity_assurance": "observed" if served else "requested_only",
        "model_identity_evidence": identity_evidence,
        "usage_by_model": partitions,
        "unattributed_usage": unattributed,
        "provider_duration_seconds": execution.result.get("duration_api_ms", 0) / 1000
        if isinstance(execution.result.get("duration_api_ms"), (int, float)) else None,
    }
    if record["observed_model_ids"] is None:
        record.pop("observed_model_ids")
    return record


def terminal_append(root: pathlib.Path, record: dict[str, Any]) -> None:
    if metadata_21(root) and hasattr(record_invocation, "record_terminal"):
        record_invocation.validate_record(record, root)
        record_invocation.record_terminal(root, record)
    else:
        legacy_fields = {
            key: value for key, value in record.items()
            if key not in {
                "attempt_kind", "provider_request_status", "inference_start_status",
                "source_invocation_id", "turn_count", "provider_cost", "served_model",
                "identity_assurance", "model_identity_evidence", "usage_by_model",
                "unattributed_usage", "provider_duration_seconds",
            }
        }
        if legacy_fields["attempt"] is None:
            legacy_fields["attempt"] = int(record.get("attempt") or 1)
        record_invocation.append(root, legacy_fields)


def read_ledger(root: pathlib.Path) -> list[dict[str, Any]]:
    path = root / "invocations.jsonl"
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RunnerError(f"invalid invocation ledger line {number}: {exc}") from exc
        if not isinstance(value, dict):
            raise RunnerError(f"invocation ledger line {number} is not an object")
        rows.append(value)
    return rows


# 2.7.4: keep every Phase-1 Bash call's working directory at the attempt's output directory. On Claude CLI
# 2.1.206 the macOS sandbox profile travels as ONE argv string on every Bash call, at 0.77 to 0.91 of ARG_MAX
# for the audit roots under runs/ (167 to 303 characters). The CLI rebuilds that profile from its tracked
# working directory whenever its settings change; after a `cd` into raw/pkg it adds that directory's
# .claude/, .mcp.json and bare-repo deny paths for every level up to the --add-dir root (34 deny paths
# become 52), the profile passes ARG_MAX, and every later command fails with E2BIG. Two of the five
# auditors of audit A15 lost their shell that way. With this variable the CLI returns the
# tracked directory to the start directory after every call, so a `cd DIR && cmd` still works within its
# call and a rebuild never sees another directory. The environment enters no key, hash, prompt, settings
# file or receipt, so it applies to every audit; the coordinator has no Bash tool and is not spawned here.
PHASE1_SHELL_ENV = {"CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1"}


def run_process(
    root: pathlib.Path,
    role: str,
    attempt_kind: str,
    command: list[str],
    cwd: pathlib.Path,
    prompt: str,
    input_hash: str,
    prompt_hash: str,
    timeout_seconds: int,
    *,
    session_id: str,
    route: dict[str, Any],
    source_invocation_id: str | None = None,
) -> ProviderExecution:
    reservation = lifecycle_reserve(
        root, role, attempt_kind, input_hash, prompt_hash,
        route=route, source_invocation_id=source_invocation_id,
    )
    started_at = utc_now()
    status_path = cwd.parent / "status.json"
    # 2.7.2: what the output workspace held before the call. A file the model writes there is proof
    # that the provider accepted the request and ran inference, even when the call never returns its
    # result envelope (see workspace_writes).
    preexisting = workspace_files(cwd)
    runtime.atomic_write(status_path, runtime.pretty_bytes({
        "schema_version": "i4r-phase1-attempt-1",
        "state": "reserved",
        "invocation_id": reservation["invocation_id"],
        "attempt_kind": attempt_kind,
        "attempt": None,
        "provider_request_status": "not_requested",
        "inference_start_status": "not_started",
        "requested_session_id": session_id,
        "input_artifact_sha256": input_hash,
        "started_at": iso(started_at),
    }))
    process: subprocess.Popen[str] | None = None
    provider_started = False
    provider_request_status = "not_requested"
    inference_started = False
    attempt: int | None = None
    stdout = ""
    stderr = ""
    timed_out = False
    returncode: int | None = None
    try:
        # 2.7.0: never the variables that remap what the alias `sonnet` serves (REMAPPING_ENV).
        process = subprocess.Popen(
            command,
            cwd=cwd,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={**claude_routes.spawn_env(route=route), **PHASE1_SHELL_ENV},
        )
        provider_started = True
        try:
            lifecycle_requested(root, reservation)
            provider_request_status = "requested"
            # Legacy 2.0 ledgers cannot represent a reservation/request separately.
            # Keep their established accounting while metadata 2.1 waits for proof
            # that the provider accepted the inference.
            if not metadata_21(root):
                attempt = lifecycle_started(root, reservation)
                inference_started = True
                provider_request_status = "accepted"
            runtime.atomic_write(status_path, runtime.pretty_bytes({
                "schema_version": "i4r-phase1-attempt-1",
                "state": "inference_started" if inference_started else "provider_requested",
                "invocation_id": reservation["invocation_id"],
                "attempt_kind": attempt_kind,
                "attempt": attempt,
                "provider_request_status": provider_request_status,
                "inference_start_status": "started" if inference_started else "not_started",
                "provider_pid": process.pid, "requested_session_id": session_id,
                "input_artifact_sha256": input_hash,
                "started_at": iso(started_at),
            }))
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            raise
        try:
            stdout, stderr = process.communicate(prompt, timeout=timeout_seconds)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            returncode = 124
    except OSError as exc:
        ended_at = utc_now()
        execution = ProviderExecution(
            reservation=reservation, attempt=None, provider_request_status="not_requested",
            inference_started=False, provider_started=False, returncode=None,
            stdout="", stderr=str(exc), timed_out=False, started_at=started_at, ended_at=ended_at,
            result={}, observed_model=None, observed_model_ids=[], usage=unavailable_usage(),
            cache_usage="not_applicable", output=cwd, attempt_dir=cwd.parent, route=route,
        requested_session_id=session_id,
        )
        runtime.atomic_write(status_path, runtime.pretty_bytes({
            "schema_version": "i4r-phase1-attempt-1", "state": "failed",
            "invocation_id": reservation["invocation_id"], "attempt_kind": attempt_kind,
            "attempt": None, "provider_request_status": "not_requested",
            "inference_start_status": "not_started", "requested_session_id": session_id,
            "error": str(exc),
            "ended_at": iso(ended_at),
        }))
        terminal_append(root, make_terminal_record(
            execution, role, attempt_kind, input_hash, prompt_hash,
            status="failed", output_artifact=None, output_hash=None,
            error=f"Claude CLI could not be launched: {exc}", error_class="transport",
            source_invocation_id=source_invocation_id,
        ))
        raise ProviderLaunchError(f"Claude CLI could not be launched: {exc}") from exc
    ended_at = utc_now()
    (cwd.parent / "claude_stdout.json").write_text(stdout, encoding="utf-8")
    (cwd.parent / "claude_stderr.txt").write_text(stderr, encoding="utf-8")
    result: dict[str, Any] = {}
    try:
        result = parse_result(stdout)
    except (RunnerError, run_coordinator.CoordinatorError):
        # 2.7.2: parse_result raises run_coordinator.CoordinatorError, not RunnerError, on a missing
        # envelope. Until 2.7.2 it escaped here -- on every timeout, since a killed CLI prints nothing --
        # before any status or terminal row was written, so the attempt stayed provider_requested and
        # only --recover-stranded could close it, as a pre-inference request.
        pass
    observed, observed_ids, _count = run_coordinator.observed_models(result)
    usage, cache = run_coordinator.extract_usage(result) if result else (unavailable_usage(), "none")
    if metadata_21(root):
        if inference_evidence(result) or workspace_writes(cwd, preexisting):
            attempt = lifecycle_started(root, reservation)
            inference_started = True
            provider_request_status = "accepted"
        else:
            provider_request_status = (
                "rejected" if rejected_provider_request(result, stderr) else "unknown"
            )
            record_invocation.mark_provider_request(
                root, reservation["invocation_id"], status=provider_request_status,
            )
    if inference_started and metadata_21(root) and hasattr(record_invocation, "mark_response_received"):
        record_invocation.mark_response_received(
            root,
            reservation["invocation_id"],
            artifact=relative_to(root, cwd.parent / "claude_stdout.json"),
            sha256=runtime.sha256_file(cwd.parent / "claude_stdout.json"),
        )
    runtime.atomic_write(status_path, runtime.pretty_bytes({
        "schema_version": "i4r-phase1-attempt-1",
        "state": "response_received",
        "invocation_id": reservation["invocation_id"],
        "attempt_kind": attempt_kind,
        "attempt": attempt,
        "provider_request_status": provider_request_status,
        "inference_start_status": "started" if inference_started else "not_started",
        "requested_session_id": session_id,
        "returncode": returncode,
        "timed_out": timed_out,
        "started_at": iso(started_at),
        "ended_at": iso(ended_at),
        "stdout_sha256": runtime.sha256_bytes(stdout.encode("utf-8")),
        "stderr_sha256": runtime.sha256_bytes(stderr.encode("utf-8")),
    }))
    execution = ProviderExecution(
        reservation=reservation, attempt=attempt,
        provider_request_status=provider_request_status,
        inference_started=inference_started, provider_started=provider_started,
        returncode=returncode, stdout=stdout, stderr=stderr, timed_out=timed_out,
        started_at=started_at, ended_at=ended_at, result=result,
        observed_model=observed, observed_model_ids=observed_ids, usage=usage,
        cache_usage=cache, output=cwd, attempt_dir=cwd.parent, route=route,
        requested_session_id=session_id,
    )
    # Denials no longer fail the role, so surface them beside the attempt instead. The
    # invocation-record schema is closed, and claude_stdout.json already holds the raw
    # entries; this is the reviewable summary.
    denials = denial_telemetry(execution)
    if denials:
        runtime.atomic_write(cwd.parent / "sandbox_denials.json", runtime.pretty_bytes({
            "schema_version": "i4r-phase1-sandbox-denials-1",
            "invocation_id": reservation["invocation_id"],
            "role": role,
            "attempt_kind": attempt_kind,
            "denial_count": len(denials),
            "denials": denials,
        }))
    return execution


def execution_errors(execution: ProviderExecution, *, require_usage: bool = True) -> list[str]:
    errors = []
    if execution.returncode != 0:
        errors.append(f"Claude CLI exited {execution.returncode}")
    if execution.timed_out:
        errors.append("Claude CLI timed out")
    if not execution.result:
        errors.append("Claude CLI did not emit a JSON result object")
    elif execution.result.get("is_error") or execution.result.get("subtype") in {"error", "failed"}:
        # Carry the envelope's own text into the message. classify_error() reads only this
        # string, so without the detail a transport failure ("API Error: Connection closed
        # mid-response") classified as `permanent` and stopped the role instead of taking
        # the transport retry the policy grants it.
        detail = execution.result.get("result")
        detail = detail.strip() if isinstance(detail, str) else ""
        errors.append(
            f"Claude result envelope reports an error: {detail[:300]}"
            if detail else "Claude result envelope reports an error"
        )
    # Judged on the dispatched route: Sonnet 5 by the unchanged pre-2.7.0 check, Sonnet 5.5
    # exactly (a served Sonnet that is not 5.5 refuses the call). The message keeps
    # "served Sonnet", which classify_error keys on.
    requested = execution.route.get("requested_model")
    primary, ids, count = run_coordinator.observed_models(execution.result, requested)
    if count != 1 or primary is None:
        errors.append(
            f"expected exactly one served {claude_routes.short_name(requested)} primary; observed {ids!r}"
        )
    if require_usage and not run_coordinator.complete_usage(execution.usage):
        errors.append("provider result lacks exact input/output/normalized-total usage")
    # A permission denial is the sandbox working: the command was refused, the model saw
    # the refusal and continued. It is not evidence that the science is unsound, and
    # failing the role discards a complete audit over a blocked shell idiom -- observed
    # here rejecting five reports that carried their completion markers, full evidence
    # manifests and validated result.json. Denials are recorded as telemetry by
    # denial_telemetry() and reviewed with the run, not treated as an authorization stop.
    return errors


def denial_telemetry(execution: ProviderExecution) -> list[dict[str, Any]]:
    """Sandbox refusals observed during an attempt, recorded rather than fatal."""
    denials = execution.result.get("permission_denials") if isinstance(execution.result, dict) else None
    if not isinstance(denials, list):
        return []
    rows = []
    for denial in denials:
        if not isinstance(denial, dict):
            continue
        tool_input = denial.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        rows.append({
            "tool_name": denial.get("tool_name"),
            "command": command if isinstance(command, str) else None,
        })
    return rows


def allowlisted_denials(attempt_dir: pathlib.Path) -> list[str]:
    """Denied Bash commands that the sandbox policy explicitly allows.

    A denial of a command outside the allowlist is the policy working. A denial of one inside
    it means the rules the CLI was handed are not the rules it is enforcing -- an environment
    fault. Re-sampling cannot fix it: on the run that motivated this, two roles each spent
    three full attempts against a sandbox that denied their own mkdir and find.
    """
    record = attempt_dir / "sandbox_denials.json"
    if not record.is_file() or record.is_symlink():
        return []
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    prefixes = []
    for entry in runtime.BASH_ALLOWLIST:
        inner = entry[len("Bash("):-1] if entry.startswith("Bash(") and entry.endswith(")") else entry
        prefixes.append(inner[:-1].strip() if inner.endswith("*") else inner.strip())
    granted = _granted_roots(attempt_dir)
    offenders = []
    for denial in payload.get("denials") or []:
        command = denial.get("command") if isinstance(denial, dict) else None
        if not isinstance(command, str):
            continue
        head = command.strip()
        if not _is_simple_command(head):
            # A compound command is checked sub-command by sub-command, so its denial is almost
            # always some other part of it -- `cd x && export Y=1 && ...` is denied because
            # `export` is not on the allowlist, which is the policy working. Judging it by its
            # first token alone reads a correct denial as a broken sandbox, and on the first run
            # with this guard that killed a role whose real fault was a repairable evidence
            # hash mismatch. Only a single simple command can implicate the sandbox itself.
            continue
        if not any(head.startswith(prefix) for prefix in prefixes if prefix):
            continue
        # The Bash allowlist and the filesystem sandbox are two independent gates. An allowlisted
        # VERB aimed at a path nothing grants -- `mkdir -p "$TMPDIR/x"`, `cat /Users/<author>/.Renviron`,
        # `find / -name '*.sav'` -- is denied correctly, and that denial says nothing about the
        # sandbox. The 2.4.6 fix caught the two-line form of exactly that command by adding the
        # newline to _COMPOUND_MARKERS; the one-line form still paused a whole Phase 1. Only a
        # command whose every path operand lies inside the attempt's own granted roots can
        # implicate the sandbox. A bare command with no path operand acts on the working
        # directory, which is granted, so it still counts.
        if not _operands_inside(head, granted):
            continue
        offenders.append(head)
    return offenders


def _granted_roots(attempt_dir: pathlib.Path) -> list[pathlib.Path] | None:
    """The allowRead/allowWrite roots the attempt was actually run with, or None if unknown."""
    settings = attempt_dir / "claude_settings.json"
    if not settings.is_file() or settings.is_symlink():
        return None
    try:
        filesystem = (json.loads(settings.read_text(encoding="utf-8"))
                      .get("sandbox", {}).get("filesystem", {}))
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    roots = [pathlib.Path(x) for x in [*filesystem.get("allowRead", []), *filesystem.get("allowWrite", [])]
             if isinstance(x, str) and x]
    return roots or None


_PATH_LIKE = re.compile(r"^(?:~|\$|\.{1,2}(?:/|$)|/)|/")


def _operands_inside(command: str, granted: list[pathlib.Path] | None) -> bool:
    """True when every path operand of a simple command resolves inside a granted root.

    An operand carrying an unexpanded `$VAR` or `~` cannot be inside a granted root as written.
    A relative operand is taken against the attempt's working directory, which is the first
    writable root. With no granted roots on record (no settings file), only a command with no
    path operand at all can be attributed to the sandbox.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    operands = [t for t in tokens[1:] if not t.startswith("-") and _PATH_LIKE.search(t)]
    if not operands:
        return True
    if granted is None:
        # No settings file, so the roots are unknown. Every real attempt writes
        # claude_settings.json before dispatch; this is the legacy verb-only rule for anything
        # that does not, kept so an attempt record without settings is judged as it always was.
        return True
    if any("$" in t or t.startswith("~") for t in operands):
        return False
    cwd = granted[-1]
    for operand in operands:
        # Cut a glob at its first metacharacter; the literal prefix is what the sandbox saw.
        literal = re.split(r"[*?\[]", operand, maxsplit=1)[0] or operand
        candidate = pathlib.Path(literal)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        resolved = pathlib.Path(os.path.normpath(str(candidate)))
        if not any(resolved == root or root in resolved.parents for root in granted):
            return False
    return True


# Shell metacharacters that make a command compound, and therefore not attributable to the
# sandbox on the strength of its first token. A NEWLINE is one of them, and leaving it out was
# not academic: a role sent
#     mkdir -p "$TMPDIR/audit_work"
#     Rscript -e '...'
# as one Bash call, the sandbox denied it (correctly -- nothing grants $TMPDIR), and this list
# judged it simple, matched `mkdir` against the allowlist, and reported the sandbox as broken.
# That paused a whole Phase 1 on a false diagnosis and refused to spend the role's remaining
# attempts. A bare `&` backgrounds and is compound for the same reason; `&&` is a superset of it.
_COMPOUND_MARKERS = ("&&", "||", ";", "|", ">", "<", "`", "$(", "\n", "\r", "&")
_ASSIGNMENT_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _is_simple_command(command: str) -> bool:
    """One command, one allowlist rule: no operators and no leading VAR=value assignment."""
    if any(marker in command for marker in _COMPOUND_MARKERS):
        return False
    return not _ASSIGNMENT_PREFIX.match(command)


def classify_failure(message: str, timed_out: bool = False) -> str:
    return run_coordinator.classify_error(message, timed_out=timed_out)


def retryable(error_class: str) -> bool:
    return error_class in {"transient", "transport", "output_limit"}


# 2.7.4: an output-cap overflow is retried once per role (run_coordinator.OUTPUT_LIMIT_RETRIES).
def role_output_limit_count(root: pathlib.Path, role: str) -> int:
    return sum(
        1 for row in read_ledger(root)
        if row.get("role") == role and row.get("provider_call") is True
        and row.get("error_class") == "output_limit"
    )


def portable_probe_key(cli: str, model: dict[str, Any], *, bound: int) -> str:
    """The probe identity without the audit-root path, for keying role completions. The probe cache
    must stay path-bound (sandbox behaviour depends on the root), but a completed role's receipt
    must not: a copied or re-mounted audit (another Mac, another Dropbox user) otherwise re-dispatched
    all five auditors and replaced their canonical reports. ``bound`` is the audit's (probe_bound)."""
    _, value = probe_key(cli, model, pathlib.Path("/"), bound=bound)
    value.pop("audit_root_sha256", None)
    return runtime.sha256_bytes(runtime.canonical_bytes(value))


def probe_key(
    cli: str, model: dict[str, Any], audit_root: pathlib.Path, *, bound: int,
) -> tuple[str, dict[str, Any]]:
    """The path-bound probe cache key. ``bound`` is the audit's frozen probe bound (probe_bound): it
    is part of the contract hash, so it has no default -- a missing bound is an error, not a re-key."""
    contract_hash = runtime.sha256_bytes(runtime.canonical_bytes(probe_contract(bound)))
    value = {
        "cli_version": cli,
        # Sandbox expressibility is a property of the audit root, so a probe that passed under
        # one root must not be replayed under another. Without this a shared --probe-cache-dir
        # replays a green receipt into an audit where nothing works.
        "audit_root_sha256": runtime.sha256_bytes(str(audit_root).encode("utf-8")),
        "model": model["requested_model"],
        "model_alias": model["spawn_alias"],
        "effort": model["reasoning_effort"],
        "sandbox_policy_sha256": runtime.sandbox_policy_hash(),
        "probe_contract_sha256": contract_hash,
    }
    return runtime.sha256_bytes(runtime.canonical_bytes(value)), value


def validate_probe_cache(
    path: pathlib.Path, key: str, components: dict[str, Any], *, bound: int,
) -> dict[str, Any]:
    bound = probe_contract(bound)["maximum_normalized_tokens"]
    value = runtime.strict_json(runtime.regular_file(path, "Phase-1 capability probe cache"))
    if (
        value.get("schema_version") != "i4r-phase1-capability-probe-cache-1"
        or value.get("status") != "passed"
        or value.get("probe_key") != key
        or value.get("key_components") != components
        or value.get("normalized_total_tokens", bound + 1) > bound
    ):
        raise RunnerError("cached Phase-1 capability probe is invalid")
    body = dict(value)
    digest = body.pop("cache_record_sha256", None)
    if digest != runtime.sha256_bytes(runtime.canonical_bytes(body)):
        raise RunnerError("cached Phase-1 capability probe hash is invalid")
    return value


def run_capability_probe(
    root: pathlib.Path,
    phase_root: pathlib.Path,
    cache_dir: pathlib.Path,
    claude: str,
    cli: str,
    model: dict[str, Any],
    timeout_seconds: int,
    *,
    bound: int,
) -> tuple[str, dict[str, Any], bool]:
    """Reuse or run the audit's capability probe under its frozen ``bound`` (probe_bound)."""
    contract = probe_contract(bound)
    key, components = probe_key(cli, model, root, bound=bound)
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / f"{key}.lock"
    cache_path = cache_dir / f"{key}.json"
    with lock_path.open("a+") as lock:
        lock_wait.acquire_exclusive(lock, "a Phase-1 lock")
        if cache_path.exists():
            cached = validate_probe_cache(cache_path, key, components, bound=bound)
            ledger = root / "invocations.jsonl"
            known = set()
            if ledger.is_file():
                for line in ledger.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        row = json.loads(line)
                        if row.get("status") == "completed":
                            known.add(row.get("invocation_id"))
            if cached.get("source_invocation_id") not in known:
                # The cached probe's source call belongs to a journal no longer canonical here (init_run.sh
                # --force archived it, or a shared --probe-cache-dir): it cannot be replayed into this
                # ledger, so set it aside and probe live instead of pausing.
                cache_path.rename(cache_path.with_name(f"{key}.stale.{uuid.uuid4().hex[:8]}.json"))
                cached = None
        if cache_path.exists() and cached is not None:
            local_receipt = phase_root / "probe_cache_replays" / f"{key}.json"
            runtime.atomic_write(local_receipt, runtime.pretty_bytes(cached))
            record_cache_replay(
                root,
                role="phase1_capability_probe",
                role_class="other",
                round_number=None,
                source_invocation_id=cached["source_invocation_id"],
                input_hash=key,
                output_artifact=relative_to(root, local_receipt),
                output_sha256=runtime.sha256_file(local_receipt),
                cache_usage="probe_cache",
                route=model,
            )
            return key, cached, True
        base = phase_root / "capability_probes" / key
        source = base / "source"
        packet = base / "packet"
        attempt_dir = base / "attempts" / str(uuid.uuid4())
        output = attempt_dir / "output.partial"
        runtime.validate_destination(root, output, "capability-probe output")
        for directory in (source, packet, output):
            directory.mkdir(parents=True, exist_ok=True)
        nonce = runtime.sha256_bytes(runtime.canonical_bytes({
            "probe_key": key, "contract": contract,
        }))
        runtime.atomic_write(source / "nonce.txt", (nonce + "\n").encode("utf-8"))
        runtime.atomic_write(packet / "PROBE_CONTRACT.json", runtime.pretty_bytes(contract))
        settings = runtime.sandbox_settings(root, phase_root, source, packet, output)
        runtime.validate_sandbox_contract(settings, source, packet, output, root)
        settings_path = attempt_dir / "claude_settings.json"
        runtime.atomic_write(settings_path, runtime.pretty_bytes(settings))
        tool_hash = runtime.sandbox_policy_hash()
        prompt = f"""This is a bounded I4R capability probe, not an audit. Read nonce.txt from the
synthetic source directory and PROBE_CONTRACT.json from the packet directory. Use the configured
Write tool to create probe.ok in the current output directory containing exactly the nonce followed
by one newline. Then use Bash, and only Bash, for these two steps: run `mkdir -p evidence` and
`cp probe.ok evidence/bash.ok` in the current output directory, and copy the contract out of the
packet with `cp <packet directory>/PROBE_CONTRACT.json packet_copy.json`. Use plain `mkdir` and
`cp` only: no shell redirection, no pipes, and no `find` flags that write (`-fprint`, `-exec`),
none of which a prefix permission rule can allow. Do not fall back to the Write tool for these
steps: they exist to prove that sandboxed Bash can read the role packet and write the output
directory, which the Write tool cannot demonstrate because it is not confined by the sandbox. If a
Bash step is denied, stop and report status=complete anyway so the denial is recorded, not retried.
Finally, return only the structured object required by the supplied JSON schema. Copy these four
values verbatim and compute nothing yourself -- in particular nonce_sha256 is the literal string
below, which is the hash OF the nonce, not the nonce you read from nonce.txt:
  status=complete
  nonce_sha256={runtime.sha256_bytes((nonce + chr(10)).encode('utf-8'))}
  tool_contract_sha256={tool_hash}
  output_file=probe.ok
Do not inspect any audit content.
"""
        prompt_hash = runtime.sha256_bytes(prompt.encode("utf-8"))
        input_hash = runtime.sha256_bytes(runtime.canonical_bytes({
            "key": key, "nonce": nonce, "probe_contract": contract,
        }))
        session_id = str(uuid.uuid4())
        command = claude_command(
            claude, settings_path, source, packet, session_id,
            model["spawn_alias"], model["reasoning_effort"], "1",
            json_schema=PROBE_SCHEMA, system_steer=PROBE_STEER,
        )
        execution = run_process(
            root, "phase1_capability_probe", "capability_probe", command, output,
            prompt, input_hash, prompt_hash, min(timeout_seconds, 300),
            session_id=session_id, route=model,
        )
        errors = execution_errors(execution)
        try:
            payload = response_payload(execution.result)
        except RunnerError as exc:
            errors.append(str(exc))
            payload = {}
        expected_payload = {
            "status": "complete",
            "nonce_sha256": runtime.sha256_bytes((nonce + "\n").encode("utf-8")),
            "tool_contract_sha256": tool_hash,
            "output_file": "probe.ok",
        }
        if payload != expected_payload:
            errors.append("structured probe result does not match the exact probe contract")
        probe_file = output / "probe.ok"
        if not probe_file.is_file() or probe_file.is_symlink() or probe_file.read_text(encoding="utf-8") != nonce + "\n":
            errors.append("capability probe did not exercise the configured Write tool")
        # The Write tool succeeds even when the sandbox rules match nothing, so it cannot stand
        # in for these. A failure here means sandboxed Bash is dead and no auditor can run.
        bash_write = output / "evidence" / "bash.ok"
        if not bash_write.is_file() or bash_write.is_symlink() or bash_write.read_text(encoding="utf-8") != nonce + "\n":
            errors.append(
                "capability probe did not complete a sandboxed Bash write to its own output "
                "directory (mkdir/cp denied): the filesystem sandbox is not granting the rules "
                "it was given, so every auditor would be denied the same way"
            )
        bash_read = output / "packet_copy.json"
        contract_bytes = (packet / "PROBE_CONTRACT.json").read_bytes()
        if not bash_read.is_file() or bash_read.is_symlink() or bash_read.read_bytes() != contract_bytes:
            errors.append(
                "capability probe did not complete a sandboxed Bash read of its role packet "
                "(cp denied, or the copy does not match the packet contract): the filesystem "
                "sandbox is not granting the read rules it was given"
            )
        total = execution.usage.get("total_tokens")
        if not isinstance(total, int) or total > bound:
            errors.append(f"capability probe exceeded {bound} normalized tokens")
        if not execution.result.get("modelUsage"):
            errors.append("capability probe lacks per-model served identity/usage telemetry")
        if errors:
            message = "; ".join(errors)
            error_class = classify_failure(message, execution.timed_out)
            terminal_append(root, make_terminal_record(
                execution, "phase1_capability_probe", "capability_probe", input_hash,
                prompt_hash, status="timed_out" if execution.timed_out else "failed",
                output_artifact=None, output_hash=None, error=message, error_class=error_class,
            ))
            raise RunnerError(f"Phase-1 capability probe failed: {message}")
        receipt_artifact = attempt_dir / "probe_receipt.json"
        receipt = {
            "schema_version": "i4r-phase1-capability-probe-cache-1",
            "status": "passed",
            "probe_key": key,
            "key_components": components,
            "source_invocation_id": execution.reservation["invocation_id"],
            "served_model": execution.observed_model,
            "served_model_ids": execution.observed_model_ids,
            "usage": execution.usage,
            "usage_by_model": usage_by_model(execution.result),
            "normalized_total_tokens": total,
            "turn_count": execution.result.get("num_turns"),
        }
        body = dict(receipt)
        receipt["cache_record_sha256"] = runtime.sha256_bytes(runtime.canonical_bytes(body))
        runtime.atomic_write(receipt_artifact, runtime.pretty_bytes(receipt))
        terminal_append(root, make_terminal_record(
            execution, "phase1_capability_probe", "capability_probe", input_hash,
            prompt_hash, status="completed",
            output_artifact=relative_to(root, receipt_artifact),
            output_hash=runtime.sha256_file(receipt_artifact), error=None, error_class=None,
        ))
        runtime.atomic_write(cache_path, runtime.pretty_bytes(receipt))
        return key, receipt, False


def validate_completed_receipt(
    root: pathlib.Path, path: pathlib.Path, input_hash: str, *, route: dict[str, Any],
) -> RoleCompletion:
    receipt = runtime.strict_json(runtime.regular_file(path, "Phase-1 role completion receipt"))
    if receipt.get("schema_version") != "i4r-phase1-completion-1" or receipt.get("input_sha256") != input_hash:
        raise RunnerError(f"invalid Phase-1 completion receipt: {path}")
    role = receipt.get("role")
    if role not in runtime.ROLE_SPECS:
        raise RunnerError(f"completion receipt has unknown role: {path}")
    report = runtime.regular_file(root / receipt["report"], "cached canonical Phase-1 report")
    evidence = runtime.regular_file(root / receipt["evidence"], "cached role evidence manifest")
    if runtime.sha256_file(report) != receipt["report_sha256"]:
        raise RunnerError(f"cached canonical report changed for {role}")
    if runtime.sha256_file(evidence) != receipt["evidence_manifest_sha256"]:
        raise RunnerError(f"cached role evidence changed for {role}")
    ledger = read_ledger(root)
    matches = [row for row in ledger if row.get("invocation_id") == receipt.get("invocation_id")]
    if len(matches) != 1 or matches[0].get("status") != "completed" or not matches[0].get("provider_call"):
        raise RunnerError(f"cached role completion lacks one canonical inference record: {role}")
    record_cache_replay(
        root,
        role=role,
        role_class="auditor",
        round_number=0,
        source_invocation_id=receipt["invocation_id"],
        input_hash=input_hash,
        output_artifact=receipt["report"],
        output_sha256=receipt["report_sha256"],
        cache_usage="read",
        route=route,
    )
    return RoleCompletion(
        role=role, status="completed", input_sha256=input_hash,
        invocation_id=receipt["invocation_id"], attempt=receipt.get("attempt"),
        report=receipt["report"], report_sha256=receipt["report_sha256"],
        evidence=receipt["evidence"],
        evidence_manifest_sha256=receipt["evidence_manifest_sha256"],
        reused=True, source_invocation_id=receipt["invocation_id"],
    )


def completion_path(phase_root: pathlib.Path, role: str, input_hash: str) -> pathlib.Path:
    return phase_root / "completions" / role / f"{input_hash}.json"


def role_attempt_count(root: pathlib.Path, role: str) -> int:
    return sum(
        1 for row in read_ledger(root)
        if row.get("role") == role and row.get("provider_call") is True
        and row.get("inference_start_status", "started") == "started"
        and row.get("attempt_kind", "role_inference") in {"role_inference", "output_repair"}
    )


def role_preinference_request_count(root: pathlib.Path, role: str) -> int:
    """Bound retryable requests that never reached accepted inference."""
    return sum(
        1 for row in read_ledger(root)
        if row.get("role") == role and row.get("provider_call") is True
        and row.get("inference_start_status") == "not_started"
        and row.get("attempt_kind", "role_inference") in {"role_inference", "output_repair"}
    )


def promote_role(
    root: pathlib.Path,
    phase_root: pathlib.Path,
    execution: ProviderExecution,
    validated: runtime.ValidatedRoleOutput,
    role: str,
    input_hash: str,
    prompt_hash: str,
    attempt_kind: str,
    source_invocation_id: str | None,
) -> RoleCompletion:
    spec = runtime.ROLE_SPECS[role]
    report_target = root / spec["report"]
    evidence_dir = root / spec["work"] / "phase1_evidence" / input_hash
    receipt_path = completion_path(phase_root, role, input_hash)
    lock_path = phase_root / "promotion.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        lock_wait.acquire_exclusive(lock, "a Phase-1 lock")
        if receipt_path.exists():
            return validate_completed_receipt(root, receipt_path, input_hash, route=execution.route)
        transaction = phase_root / "transactions" / execution.reservation["invocation_id"]
        transaction.mkdir(parents=True, exist_ok=False)
        staged_report = transaction / "REPORT.md"
        shutil.copyfile(validated.report, staged_report)
        staged_evidence = transaction / "evidence"
        shutil.copytree(validated.evidence_manifest.parent, staged_evidence, symlinks=False)
        if runtime.sha256_file(staged_report) != validated.report_sha256:
            raise RunnerError(f"validated report changed before promotion: {role}")
        old_report = transaction / "old_REPORT.md"
        report_existed = report_target.is_file() and not report_target.is_symlink()
        if report_target.exists() and not report_existed:
            raise RunnerError(f"unsafe canonical Phase-1 report target: {report_target}")
        if report_existed:
            shutil.copyfile(report_target, old_report)
        report_target.parent.mkdir(parents=True, exist_ok=True)
        evidence_created = False
        terminal_recorded = False
        try:
            if evidence_dir.exists():
                existing_manifest = runtime.regular_file(evidence_dir / "manifest.json", "existing role evidence")
                if runtime.sha256_file(existing_manifest) != validated.evidence_manifest_sha256:
                    raise RunnerError(f"content-addressed role evidence collision: {role}")
            else:
                evidence_dir.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_evidence, evidence_dir)
                evidence_created = True
            os.replace(staged_report, report_target)
            attempt_input = runtime.strict_json(execution.attempt_dir / "input.json")
            record = make_terminal_record(
                execution, role, attempt_kind, attempt_input["input_sha256"], prompt_hash,
                status="completed", output_artifact=spec["report"],
                output_hash=validated.report_sha256, error=None, error_class=None,
                source_invocation_id=source_invocation_id,
            )
            terminal_append(root, record)
            terminal_recorded = True
            receipt = {
                "schema_version": "i4r-phase1-completion-1",
                "status": "completed",
                "role": role,
                "input_sha256": input_hash,
                "source_snapshot_sha256": runtime.strict_json(
                    execution.attempt_dir / "input.json"
                )["source_snapshot_sha256"],
                "invocation_id": execution.reservation["invocation_id"],
                "attempt": execution.attempt,
                "attempt_kind": attempt_kind,
                "source_invocation_id": source_invocation_id,
                "report": spec["report"],
                "report_sha256": validated.report_sha256,
                "evidence": relative_to(root, evidence_dir / "manifest.json"),
                "evidence_manifest_sha256": validated.evidence_manifest_sha256,
                "result_sha256": validated.result_sha256,
                "source_bindings": [
                    {"path": path, "sha256": digest} for path, digest in validated.source_bindings
                ],
            }
            runtime.atomic_write(receipt_path, runtime.pretty_bytes(receipt))
            runtime.atomic_write(execution.attempt_dir / "status.json", runtime.pretty_bytes({
                "schema_version": "i4r-phase1-attempt-1",
                "state": "completed",
                "invocation_id": execution.reservation["invocation_id"],
                "attempt": execution.attempt,
                "attempt_kind": attempt_kind,
                "provider_request_status": "accepted",
                "inference_start_status": "started",
                "input_artifact_sha256": attempt_input["input_sha256"],
                "output_sha256": validated.report_sha256,
                "ended_at": iso(execution.ended_at),
            }))
        except BaseException:
            # Once the terminal journal event exists, the canonical report must remain at its
            # recorded hash.  A later receipt/status crash is recoverable; rolling the report back
            # would instead corrupt the canonical invocation projection.
            if not terminal_recorded:
                if report_existed and old_report.is_file():
                    os.replace(old_report, report_target)
                elif report_target.exists() and runtime.sha256_file(report_target) == validated.report_sha256:
                    report_target.unlink()
                if evidence_created:
                    shutil.rmtree(evidence_dir, ignore_errors=True)
            raise
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        shutil.rmtree(transaction, ignore_errors=True)
    return RoleCompletion(
        role=role, status="completed", input_sha256=input_hash,
        invocation_id=execution.reservation["invocation_id"], attempt=execution.attempt,
        report=spec["report"], report_sha256=validated.report_sha256,
        evidence=relative_to(root, evidence_dir / "manifest.json"),
        evidence_manifest_sha256=validated.evidence_manifest_sha256,
        reused=False, source_invocation_id=source_invocation_id,
    )


def append_failure(
    root: pathlib.Path,
    execution: ProviderExecution,
    role: str,
    attempt_kind: str,
    input_hash: str,
    prompt_hash: str,
    message: str,
    error_class: str,
    *,
    source_invocation_id: str | None = None,
) -> None:
    terminal_append(root, make_terminal_record(
        execution, role, attempt_kind, input_hash, prompt_hash,
        status="timed_out" if execution.timed_out else "failed",
        output_artifact=None, output_hash=None, error=message, error_class=error_class,
        source_invocation_id=source_invocation_id,
    ))
    status_path = execution.attempt_dir / "status.json"
    runtime.atomic_write(status_path, runtime.pretty_bytes({
        "schema_version": "i4r-phase1-attempt-1",
        "state": "failed",
        "invocation_id": execution.reservation["invocation_id"],
        "attempt": execution.attempt,
        "attempt_kind": attempt_kind,
        "provider_request_status": execution.provider_request_status,
        "inference_start_status": "started" if execution.inference_started else "not_started",
        "requested_session_id": execution.requested_session_id,
        "error": message,
        "error_class": error_class,
        "ended_at": iso(execution.ended_at),
    }))


def ledger_has_terminal(root: pathlib.Path, invocation_id: str | None) -> bool:
    """Whether this invocation already has its one terminal row.

    invocations.jsonl holds only terminal rows (lifecycle_journal.project_invocations appends
    on invocation_terminal/cache_reuse alone), so matching any row is equivalent to asking
    whether the call has been accounted for. The ledger, not a local flag, is the arbiter.
    """
    if not invocation_id:
        return False
    try:
        return any(row.get("invocation_id") == invocation_id for row in read_ledger(root))
    except RunnerError:
        return False


def attempt_binding(
    execution: ProviderExecution, input_hash: str, prompt_hash: str,
) -> tuple[str, str, str, str | None]:
    """Attempt kind and hashes for one attempt, tolerant of an unreadable input.json.

    Reading that file is itself one of the steps that can fail after the call is paid for,
    so the recorder must not depend on it succeeding.
    """
    try:
        value = runtime.strict_json(execution.attempt_dir / "input.json")
    except runtime.Phase1Error:
        return "role_inference", input_hash, prompt_hash, None
    return (
        value.get("attempt_kind") or "role_inference",
        value.get("input_sha256") or input_hash,
        value.get("prompt_sha256") or prompt_hash,
        value.get("source_invocation_id"),
    )


@contextlib.contextmanager
def terminal_on_escape(
    root: pathlib.Path,
    execution: ProviderExecution,
    role: str,
    input_hash: str,
    prompt_hash: str,
):
    """Once the provider call is paid for, every exit leaves one terminal row and status.

    The typed handlers already record their own outcome; this covers the paths that do not --
    a post-call snapshot check, an output fault that is not an OutputValidationError, a
    promotion that dies before its terminal journal event. Without it the attempt keeps state
    "response_received" forever, the run under-reports its spend, and
    recover_received_role_attempt re-adopts the same dead attempt on every later run.

    If a row already exists the guard does nothing at all -- in particular it must not rewrite
    status.json, because a completed row beside state "response_received" is exactly what the
    terminal-journal recovery keys on.
    """
    try:
        yield
    except BaseException as exc:
        invocation_id = execution.reservation.get("invocation_id")
        if not ledger_has_terminal(root, invocation_id):
            kind, attempt_input_hash, attempt_prompt_hash, source = attempt_binding(
                execution, input_hash, prompt_hash,
            )
            message = f"{type(exc).__name__}: {exc}"
            # A snapshot that changed under the call is the host's fault, whatever keyword the
            # message happens to contain; the text classifier would call it permanent or worse.
            error_class = (
                "environment" if isinstance(exc, runtime.SnapshotIntegrityError)
                else classify_failure(message, execution.timed_out)
            )
            try:
                append_failure(
                    root, execution, role, kind, attempt_input_hash, attempt_prompt_hash,
                    message, error_class,
                    source_invocation_id=source,
                )
            except Exception as record_exc:  # never let the recorder mask the real failure
                print(
                    f"  [error] {role}: could not record terminal invocation "
                    f"{invocation_id}: {record_exc}",
                    file=sys.stderr,
                )
        raise


def role_command_and_attempt(
    root: pathlib.Path,
    phase_root: pathlib.Path,
    snapshot: pathlib.Path,
    packet_dir: pathlib.Path,
    role: str,
    input_hash: str,
    prompt: str,
    prompt_hash: str,
    claude: str,
    model: dict[str, Any],
    timeout_seconds: int,
    max_budget_usd: str,
    attempt_kind: str,
    *,
    source_invocation_id: str | None = None,
    extra_deny_read: runtime.ExtraDenyRead = (),
    snapshot_manifest: dict[str, Any] | None = None,
) -> ProviderExecution:
    # Pre-dispatch, before the attempt directory or any reservation exists: a snapshot that no
    # longer verifies raises SnapshotIntegrityError here and costs nothing; mode-only drift is
    # re-frozen and journaled once. Before 2.6.8 the snapshot was checked only after the call.
    if snapshot_manifest is None:
        snapshot_manifest = runtime.strict_json(snapshot / "snapshot_manifest.json")
        if snapshot_manifest.get("snapshot_sha256") != snapshot.name:
            raise runtime.SnapshotIntegrityError("snapshot manifest does not name its directory")
    runtime.verify_snapshot(
        snapshot, snapshot_manifest, refreeze=True,
        on_refreeze=lambda facts: journal_snapshot_refreeze(
            root, snapshot, facts, f"pre_dispatch:{role}"),
    )
    index = role_attempt_count(root, role) + 1
    attempt_dir = phase_root / "roles" / role / input_hash / f"attempt_{index:02d}_{uuid.uuid4()}"
    output = attempt_dir / "output.partial"
    runtime.validate_destination(root, output, f"{role} attempt output")
    output.mkdir(parents=True, exist_ok=False)
    runtime.atomic_write(attempt_dir / "input.json", runtime.pretty_bytes({
        "schema_version": "i4r-phase1-attempt-input-1",
        "role": role,
        "input_sha256": input_hash,
        "source_snapshot_sha256": runtime.strict_json(snapshot / "snapshot_manifest.json")["snapshot_sha256"],
        "role_packet_sha256": runtime.sha256_file(packet_dir / "packet_manifest.json"),
        "prompt_sha256": prompt_hash,
        "attempt_kind": attempt_kind,
        "source_invocation_id": source_invocation_id,
    }))
    provider_source = snapshot / "raw"
    settings = runtime.sandbox_settings(
        root, phase_root, provider_source, packet_dir, output, extra_deny_read=extra_deny_read,
    )
    runtime.validate_sandbox_contract(settings, provider_source, packet_dir, output, root)
    settings_path = attempt_dir / "claude_settings.json"
    runtime.atomic_write(settings_path, runtime.pretty_bytes(settings))
    runtime.atomic_write(attempt_dir / "prompt.txt", prompt.encode("utf-8"))
    session_id = str(uuid.uuid4())
    command = claude_command(
        claude, settings_path, provider_source, packet_dir, session_id,
        model["spawn_alias"], model["reasoning_effort"], max_budget_usd,
    )
    return run_process(
        root, role, attempt_kind, command, output, prompt, input_hash, prompt_hash,
        timeout_seconds, session_id=session_id, route=model,
        source_invocation_id=source_invocation_id,
    )


def repair_packet(
    execution: ProviderExecution,
    diagnostics: str,
    original_packet_bytes: int,
    projection: dict[str, Any],
) -> tuple[pathlib.Path, pathlib.Path, int]:
    source = execution.attempt_dir / "repair_source"
    packet = execution.attempt_dir / "repair_packet"
    source.mkdir(); packet.mkdir()
    prior = source / "prior_output"
    shutil.copytree(execution.output, prior, symlinks=False)
    runtime.atomic_write(packet / "validator_diagnostics.txt", (diagnostics + "\n").encode("utf-8"))
    runtime.atomic_write(packet / "SCIENTIFIC_PROJECTION.json", runtime.pretty_bytes({
        "projection": projection,
        "projection_sha256": runtime.sha256_bytes(runtime.canonical_bytes(projection)),
    }))
    total = sum(path.stat().st_size for path in [*source.rglob("*"), *packet.rglob("*")] if path.is_file())
    if total * 5 > original_packet_bytes:
        raise RunnerError(
            f"focused repair packet is {total} bytes, exceeding 20% of the {original_packet_bytes}-byte original packet"
        )
    return source, packet, total


def try_focused_repair(
    root: pathlib.Path,
    phase_root: pathlib.Path,
    role: str,
    original: ProviderExecution,
    original_error: runtime.OutputValidationError,
    snapshot_manifest: dict[str, Any],
    input_hash: str,
    claude: str,
    model: dict[str, Any],
    timeout_seconds: int,
    max_budget_usd: str,
    original_packet_bytes: int,
    *,
    targets: RoleTargets | None = None,
    deposit_exemptions: frozenset[str] = frozenset(),
) -> RoleCompletion:
    targets = targets or auditor_targets(model)
    # The repair is a paid dispatch too: check the snapshot before its packet, attempt directory
    # or reservation exists.
    snapshot = phase_root / "snapshots" / snapshot_manifest["snapshot_sha256"]
    runtime.verify_snapshot(
        snapshot, snapshot_manifest, refreeze=True,
        on_refreeze=lambda facts: journal_snapshot_refreeze(
            root, snapshot, facts, f"pre_dispatch:{role}:repair"),
    )
    projection = targets.output.projection(original.output, role, snapshot_manifest)
    source, packet, packet_bytes = repair_packet(
        original, str(original_error), original_packet_bytes, projection,
    )
    prompt = f"""Perform one format-only repair of the prior Phase-1 output. The prior files are in
the read-only prior_output/ directory. Validator diagnostics are in validator_diagnostics.txt.
The authoritative byte-freeze is SCIENTIFIC_PROJECTION.json.
Create a complete corrected REPORT.md, evidence/manifest.json and result.json in the current output
directory. Do not add, remove, reinterpret, or recompute scientific content. Copy REPORT.md and
every evidence artifact byte-for-byte; copy scientific_fields byte-equivalent as canonical JSON.
Only structural JSON bindings may change. Recompute and compare the full projection before return.
This repair packet is {packet_bytes} bytes and is bounded to at most 20% of the original packet.
Return only a short completion notice.
"""
    prompt_hash = runtime.sha256_bytes(prompt.encode("utf-8"))
    repair_hash = runtime.sha256_bytes(runtime.canonical_bytes({
        "source_invocation_id": original.reservation["invocation_id"],
        "original_input_sha256": input_hash,
        "diagnostics_sha256": runtime.sha256_bytes(str(original_error).encode("utf-8")),
        "repair_packet_bytes": packet_bytes,
    }))
    attempt_dir = phase_root / "roles" / role / input_hash / f"repair_{uuid.uuid4()}"
    output = attempt_dir / "output.partial"
    runtime.validate_destination(root, output, f"{role} repair output")
    output.mkdir(parents=True)
    settings = runtime.sandbox_settings(
        root, phase_root, source, packet, output, extra_deny_read=targets.extra_deny_read,
    )
    runtime.validate_sandbox_contract(settings, source, packet, output, root)
    settings_path = attempt_dir / "claude_settings.json"
    runtime.atomic_write(settings_path, runtime.pretty_bytes(settings))
    runtime.atomic_write(attempt_dir / "prompt.txt", prompt.encode("utf-8"))
    attempt_input = {
        "schema_version": "i4r-phase1-attempt-input-1", "role": role,
        "input_sha256": repair_hash,
        "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
        "role_packet_sha256": runtime.sha256_file(original.attempt_dir / "input.json"),
        "prompt_sha256": prompt_hash,
        "attempt_kind": "output_repair",
        "source_invocation_id": original.reservation["invocation_id"],
    }
    if targets.repair_records_original_input:
        # Record the repair under the batch input hash that names the completion receipt, and
        # keep the repair's own content digest beside it for diagnosis.
        attempt_input["repair_input_sha256"] = repair_hash
        attempt_input["input_sha256"] = input_hash
        repair_hash = input_hash
    runtime.atomic_write(attempt_dir / "input.json", runtime.pretty_bytes(attempt_input))
    repair_session_id = str(uuid.uuid4())
    command = claude_command(
        claude, settings_path, source, packet, repair_session_id,
        model["spawn_alias"], model["reasoning_effort"], max_budget_usd,
    )
    repair = run_process(
        root, role, "output_repair", command, output, prompt, repair_hash, prompt_hash,
        timeout_seconds, session_id=repair_session_id, route=model,
        source_invocation_id=original.reservation["invocation_id"],
    )
    with terminal_on_escape(root, repair, role, repair_hash, prompt_hash):
        runtime.verify_snapshot(
            snapshot, snapshot_manifest, refreeze=True,
            on_refreeze=lambda facts: journal_snapshot_refreeze(
                root, snapshot, facts, f"post_call:{role}:repair"),
        )
        errors = execution_errors(repair)
        if errors:
            message = "; ".join(errors)
            append_failure(
                root, repair, role, "output_repair", repair_hash, prompt_hash,
                message, classify_failure(message, repair.timed_out),
                source_invocation_id=original.reservation["invocation_id"],
            )
            raise RunnerError(f"focused output repair failed: {message}")
        try:
            validated = targets.output.validate(
                repair.output, role, snapshot_manifest,
                expected_scientific_projection=projection,
                deposit_exemptions=deposit_exemptions,
            )
        except runtime.OutputValidationError as exc:
            append_failure(
                root, repair, role, "output_repair", repair_hash, prompt_hash,
                str(exc), "schema", source_invocation_id=original.reservation["invocation_id"],
            )
            raise RunnerError(f"focused output repair failed validation: {exc}") from exc
        return targets.promote(
            root, phase_root, repair, validated, role, input_hash, prompt_hash,
            "output_repair", original.reservation["invocation_id"],
        )


def run_role(
    root: pathlib.Path,
    phase_root: pathlib.Path,
    snapshot: pathlib.Path,
    snapshot_manifest: dict[str, Any],
    role: str,
    packet_dir: pathlib.Path,
    packet: dict[str, Any],
    prompt: str,
    probe_key_value: str,
    claude: str,
    model: dict[str, Any],
    timeout_seconds: int,
    max_budget_usd: str,
    portable_key: str | None = None,
    *,
    targets: RoleTargets | None = None,
) -> RoleCompletion:
    targets = targets or auditor_targets(model)
    legacy_hash = runtime.role_input_hash(packet, prompt, probe_key_value)
    input_hash = runtime.role_input_hash(packet, prompt, portable_key) if portable_key else legacy_hash
    completed = completion_path(phase_root, role, input_hash)
    if completed.exists():
        return targets.validate_receipt(root, completed, input_hash)
    alias = completed.with_suffix(".alias")
    if alias.is_file():
        # a legacy receipt found at an earlier path, recorded under this path-independent key
        aliased = alias.read_text(encoding="utf-8").strip()
        target = completion_path(phase_root, role, aliased) if re.fullmatch(r"[0-9a-f]{64}", aliased) else None
        if target is None or not target.is_file():
            # never silently re-dispatch (and pay for) a role whose completion is recorded
            raise RunnerError(
                f"corrupt completion alias {alias}: "
                + ("it does not hold a receipt hash" if target is None else f"its receipt {target} is missing")
                + "; restore the receipt, or remove the alias to run the role again")
        return targets.validate_receipt(root, target, aliased)
    legacy = completion_path(phase_root, role, legacy_hash)
    if legacy_hash != input_hash and legacy.exists():
        # a receipt written before completions were keyed path-independently, at this same path; alias
        # it so the audit still reuses it after a later move
        reused = targets.validate_receipt(root, legacy, legacy_hash)
        runtime.atomic_write(alias, (legacy_hash + "\n").encode("utf-8"))
        return reused
    prompt_hash = runtime.sha256_bytes(prompt.encode("utf-8"))
    original_packet_bytes = (
        sum(row["bytes"] for row in snapshot_manifest["source_files"])
        + sum(path.stat().st_size for path in packet_dir.rglob("*") if path.is_file())
        + len(prompt.encode("utf-8"))
    )
    # The deposit's own code-shaped column names, admitted by Phase 1 and the coordinator alike.
    deposit_exemptions = runtime.snapshot_deposit_exemptions(snapshot)
    recovered_completion, recovered_execution = recover_received_role_attempt(
        root, phase_root, role, input_hash, snapshot_manifest, route=model, targets=targets,
        deposit_exemptions=deposit_exemptions,
    )
    if recovered_completion is not None:
        return recovered_completion
    limit = lifecycle_journal.max_attempts_per_role()
    while role_attempt_count(root, role) < limit and role_preinference_request_count(root, role) < 2:
        execution = recovered_execution
        recovered_execution = None
        if execution is None:
            execution = role_command_and_attempt(
                root, phase_root, snapshot, packet_dir, role, input_hash, prompt, prompt_hash,
                claude, model, timeout_seconds, max_budget_usd, "role_inference",
                extra_deny_read=targets.extra_deny_read, snapshot_manifest=snapshot_manifest,
            )
        with terminal_on_escape(root, execution, role, input_hash, prompt_hash):
            attempt_input = runtime.strict_json(execution.attempt_dir / "input.json")
            attempt_kind = attempt_input.get("attempt_kind", "role_inference")
            attempt_prompt_hash = attempt_input.get("prompt_sha256", prompt_hash)
            source_invocation_id = attempt_input.get("source_invocation_id")
            # Mode-only drift during the call is re-frozen and the paid output kept; a content
            # change raises SnapshotIntegrityError, recorded as one environment failure.
            runtime.verify_snapshot(
                snapshot, snapshot_manifest, refreeze=True,
                on_refreeze=lambda facts: journal_snapshot_refreeze(
                    root, snapshot, facts, f"post_call:{role}"),
            )
            errors = execution_errors(execution)
            if errors:
                message = "; ".join(errors)
                error_class = classify_failure(message + " " + execution.stderr, execution.timed_out)
                append_failure(
                    root, execution, role, attempt_kind, attempt_input["input_sha256"],
                    attempt_prompt_hash, message, error_class,
                    source_invocation_id=source_invocation_id,
                )
                if (
                    retryable(error_class)
                    and role_attempt_count(root, role) < limit
                    and role_preinference_request_count(root, role) < 2
                    and (error_class != "output_limit"
                         or role_output_limit_count(root, role) <= run_coordinator.OUTPUT_LIMIT_RETRIES)
                ):
                    continue
                raise RunnerError(f"{role} inference failed ({error_class}): {message}")
            try:
                frozen: dict[str, Any] = {}
                if attempt_kind == "output_repair" and targets.freeze_recovered_repairs:
                    # Only a recovered execution can be a repair here. Re-prove its freeze: a
                    # repair adopted after a kill must not change what a live repair may not.
                    frozen["expected_scientific_projection"] = recovered_repair_projection(
                        phase_root, role, input_hash, source_invocation_id,
                    )
                validated = targets.output.validate(
                    execution.output, role, snapshot_manifest, **frozen,
                    deposit_exemptions=deposit_exemptions,
                )
            except runtime.OutputValidationError as exc:
                append_failure(
                    root, execution, role, attempt_kind, attempt_input["input_sha256"],
                    attempt_prompt_hash, str(exc), "schema",
                    source_invocation_id=source_invocation_id,
                )
                if attempt_kind == "role_inference" and exc.repairable and role_attempt_count(root, role) < limit:
                    try:
                        return try_focused_repair(
                            root, phase_root, role, execution, exc, snapshot_manifest, input_hash,
                            claude, model, timeout_seconds, max_budget_usd, original_packet_bytes,
                            targets=targets, deposit_exemptions=deposit_exemptions,
                        )
                    except RunnerError as repair_exc:
                        if role_attempt_count(root, role) < limit:
                            print(
                                f"  [note ] {role}: focused repair rejected ({repair_exc}); "
                                "spending a fresh attempt",
                                file=sys.stderr,
                            )
                            continue
                        raise RunnerError(
                            f"{role} output failed validation and repair: {repair_exc}"
                        ) from repair_exc
                # A schema-invalid output is a property of one sample, not of the role. On audit
                # A34 every such failure passed on a fresh attempt, while the old
                # immediate stop left budget unused and forced a full journal reset. Spend the
                # remaining budget on a fresh sample before giving up.
                denied = allowlisted_denials(execution.attempt_dir)
                if denied:
                    raise EnvironmentFaultError(
                        f"{role} was denied {len(denied)} command(s) its own sandbox allowlist "
                        f"grants (first: {denied[0].split()[0]}); the filesystem sandbox is not "
                        "enforcing the rules it was given, so a fresh sample would be denied the "
                        "same way. Stopping instead of spending the remaining attempts."
                    ) from exc
                if role_attempt_count(root, role) < limit and role_preinference_request_count(root, role) < 2:
                    continue
                raise RunnerError(f"{role} output failed validation: {exc}") from exc
            return targets.promote(
                root, phase_root, execution, validated, role, input_hash, attempt_prompt_hash,
                attempt_kind, source_invocation_id,
            )
    if role_preinference_request_count(root, role) >= 2:
        raise RunnerError(
            f"{role} exhausted two pre-inference provider-request retries (a request the "
            "provider never accepted twice in a row; check credentials/quota, then re-run)")
    raise RunnerError(
        f"{role} exhausted its {limit} accepted inference starts (raise the ceiling with "
        "I4R_MAX_ATTEMPTS=<1..20>, or recover a stranded attempt with --recover-stranded, "
        "then re-run)")


def recovered_repair_projection(
    phase_root: pathlib.Path, role: str, input_hash: str, source_invocation_id: Any,
) -> dict[str, Any]:
    """The byte-frozen projection a recovered focused repair must reproduce.

    try_focused_repair writes it to <source attempt>/repair_packet/SCIENTIFIC_PROJECTION.json
    before the repair call, beside the attempt whose output it freezes. A recovered repair whose
    source attempt or frozen projection is missing or altered cannot prove its freeze, so it is
    rejected as an invalid repair sample (never promoted, never a runner crash).
    """
    def refuse(reason: str) -> runtime.OutputValidationError:
        return runtime.OutputValidationError(
            f"recovered focused repair cannot prove its byte-freeze: {reason}", rule="frozen_projection",
        )

    if not isinstance(source_invocation_id, str) or not source_invocation_id:
        raise refuse("it names no source invocation")
    matches = []
    for status_path in sorted((phase_root / "roles" / role / input_hash).glob("*/status.json")):
        try:
            status = runtime.strict_json(status_path)
        except runtime.Phase1Error:
            continue
        if status.get("invocation_id") == source_invocation_id:
            matches.append(status_path.parent)
    if len(matches) != 1:
        raise refuse(f"{len(matches)} source attempts carry invocation {source_invocation_id}")
    frozen_path = matches[0] / "repair_packet" / "SCIENTIFIC_PROJECTION.json"
    try:
        frozen = runtime.strict_json(runtime.regular_file(frozen_path, "frozen repair projection"))
    except runtime.Phase1Error as exc:
        raise refuse(str(exc)) from exc
    projection = frozen.get("projection")
    if (
        set(frozen) != {"projection", "projection_sha256"} or not isinstance(projection, dict)
        or frozen.get("projection_sha256") != runtime.sha256_bytes(runtime.canonical_bytes(projection))
    ):
        raise refuse("SCIENTIFIC_PROJECTION.json does not hash to its own projection_sha256")
    return projection


def _parse_iso(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise RunnerError(f"{field} is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RunnerError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise RunnerError(f"{field} lacks a timezone")
    return parsed


def rebuild_auditor_receipt(
    root: pathlib.Path,
    phase_root: pathlib.Path,
    role: str,
    input_hash: str,
    snapshot_manifest: dict[str, Any],
    attempt_dir: pathlib.Path,
    status_path: pathlib.Path,
    status: dict[str, Any],
    row: dict[str, Any],
    validated: runtime.ValidatedRoleOutput,
    *,
    route: dict[str, Any],
) -> RoleCompletion:
    """Rebuild an auditor's missing completion receipt from its bound canonical outputs."""
    invocation_id = status.get("invocation_id")
    spec = runtime.ROLE_SPECS[role]
    report = runtime.regular_file(root / spec["report"], "terminal-recovery report")
    evidence = runtime.regular_file(
        root / spec["work"] / "phase1_evidence" / input_hash / "manifest.json",
        "terminal-recovery evidence manifest",
    )
    if runtime.sha256_file(report) != validated.report_sha256:
        raise RunnerError("terminal-recovery canonical report hash disagrees with response")
    if runtime.sha256_file(evidence) != validated.evidence_manifest_sha256:
        raise RunnerError("terminal-recovery evidence hash disagrees with response")
    attempt_input = runtime.strict_json(attempt_dir / "input.json")
    receipt = {
        "schema_version": "i4r-phase1-completion-1",
        "status": "completed",
        "role": role,
        "input_sha256": input_hash,
        "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
        "invocation_id": invocation_id,
        "attempt": row.get("attempt"),
        "attempt_kind": attempt_input.get("attempt_kind", "role_inference"),
        "source_invocation_id": attempt_input.get("source_invocation_id"),
        "report": spec["report"],
        "report_sha256": validated.report_sha256,
        "evidence": relative_to(root, evidence),
        "evidence_manifest_sha256": validated.evidence_manifest_sha256,
        "result_sha256": validated.result_sha256,
        "source_bindings": [
            {"path": path, "sha256": digest} for path, digest in validated.source_bindings
        ],
    }
    runtime.atomic_write(completion_path(phase_root, role, input_hash), runtime.pretty_bytes(receipt))
    runtime.atomic_write(status_path, runtime.pretty_bytes({
        "schema_version": "i4r-phase1-attempt-1", "state": "completed",
        "invocation_id": invocation_id, "attempt": row.get("attempt"),
        "attempt_kind": receipt["attempt_kind"], "recovered_after_terminal": True,
        "ended_at": status.get("ended_at"),
    }))
    return validate_completed_receipt(
        root, completion_path(phase_root, role, input_hash), input_hash, route=route,
    )


def recover_received_role_attempt(
    root: pathlib.Path,
    phase_root: pathlib.Path,
    role: str,
    input_hash: str,
    snapshot_manifest: dict[str, Any],
    *,
    route: dict[str, Any],
    targets: RoleTargets | None = None,
    deposit_exemptions: frozenset[str] = frozenset(),
) -> tuple[RoleCompletion | None, ProviderExecution | None]:
    """Adopt this role's one retained response. ``route`` is the role's snapshot route, which the
    adopted execution is recorded under (its status.json names only the invocation)."""
    targets = targets or auditor_targets(route)
    candidates = []
    base = phase_root / "roles" / role / input_hash
    for status_path in sorted(base.glob("*/status.json")):
        status = runtime.strict_json(status_path)
        if status.get("state") == "response_received":
            candidates.append((status_path, status))
    if not candidates:
        return None, None
    if len(candidates) > 1:
        raise RunnerError(f"multiple unretired received responses exist for {role}/{input_hash}")
    status_path, status = candidates[0]
    attempt_dir = status_path.parent
    invocation_id = status.get("invocation_id")
    rows = [row for row in read_ledger(root) if row.get("invocation_id") == invocation_id]
    if rows:
        if len(rows) != 1 or rows[0].get("status") != "completed":
            raise RunnerError(
                f"received response {invocation_id} already has a non-completed terminal record"
            )
        validated = targets.output.validate(
            attempt_dir / "output.partial", role, snapshot_manifest,
            deposit_exemptions=deposit_exemptions,
        )
        return targets.rebuild_receipt(
            root, phase_root, role, input_hash, snapshot_manifest, attempt_dir, status_path,
            status, rows[0], validated,
        ), None
    stdout_path = runtime.regular_file(attempt_dir / "claude_stdout.json", "received Claude response")
    stderr_path = runtime.regular_file(attempt_dir / "claude_stderr.txt", "received Claude stderr")
    stdout = stdout_path.read_text(encoding="utf-8")
    stderr = stderr_path.read_text(encoding="utf-8")
    if runtime.sha256_bytes(stdout.encode("utf-8")) != status.get("stdout_sha256"):
        raise RunnerError(f"received response bytes changed for {invocation_id}")
    result: dict[str, Any] = {}
    try:
        result = parse_result(stdout)
    except Exception:
        pass
    observed, observed_ids, _ = run_coordinator.observed_models(result)
    usage, cache = run_coordinator.extract_usage(result) if result else (unavailable_usage(), "none")
    execution = ProviderExecution(
        reservation={"invocation_id": invocation_id},
        attempt=status.get("attempt"),
        provider_request_status=status.get("provider_request_status", "accepted"),
        inference_started=status.get("inference_start_status", "started") == "started",
        provider_started=True,
        returncode=status.get("returncode"),
        stdout=stdout,
        stderr=stderr,
        timed_out=status.get("timed_out") is True,
        started_at=_parse_iso(status.get("started_at"), "received response started_at"),
        ended_at=_parse_iso(status.get("ended_at"), "received response ended_at"),
        result=result,
        observed_model=observed,
        observed_model_ids=observed_ids,
        usage=usage,
        cache_usage=cache,
        output=attempt_dir / "output.partial",
        attempt_dir=attempt_dir,
        route=route,
        requested_session_id=(
            status.get("requested_session_id")
            if isinstance(status.get("requested_session_id"), str) else None
        ),
    )
    return None, execution


# Non-terminal attempt states a killed process can leave behind. `response_received` is NOT
# here: it retains real output and has its own recovery path, which must not discard it.
STRANDED_STATES = ("provider_requested", "inference_started")


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def snapshot_route_effort(root: pathlib.Path, role: Any, role_class: Any) -> str | None:
    """The effort of the Claude route the audit's snapshot binds ``role`` to, or None (2.7.3).

    For ``--recover-stranded`` only, when a reservation carries no reasoning_effort (none this runner
    writes does). The auditors, the verifier and the coordinator are routed as the ledger validator
    routes them (record_invocation.configured_for); the capability probe runs on the first auditor's
    route. Only a live Claude route counts: an unreadable snapshot, an unknown role or a route no runner
    dispatches gives None, and the caller keeps the pre-2.7.3 "max".
    """
    try:
        policy = record_invocation.load_policy_snapshot(root)
        if role == "phase1_capability_probe":
            route = next(iter(policy["models"]["phase_1_auditors"].values()))
        else:
            route = record_invocation.configured_for({"role": role, "role_class": role_class}, policy)
    except Exception:  # a snapshot the runners would refuse is not a source of truth here either
        return None
    live = claude_routes.live_route(route) if isinstance(route, dict) else None
    return live["reasoning_effort"] if live else None


def recover_stranded_attempts(root: pathlib.Path, phase_root: pathlib.Path) -> int:
    """Terminalize Phase-1 attempts whose process died mid-call, so the audit can proceed.

    ``guard_stranded_attempts`` refuses to dispatch while any attempt sits in a non-terminal
    state with no terminal ledger row, because automatically re-dispatching could duplicate a
    paid call. Both states count. Under metadata 2.1 the runner holds status.json at
    ``provider_requested`` for the WHOLE call and only journals ``inference_start`` once the
    provider stream proves acceptance, so a 2.1 strand never reaches ``inference_started`` --
    which is the only state the first version of this looked at. Every one of the seven real
    strands in this tree is ``provider_requested``, so both the guard and the recovery were
    inert on all of them: the guard let a duplicate call through and the recovery cleared
    nothing. ``inference_started`` still appears in legacy 2.0 workspaces. That is right, but nothing could ever clear the state: the window is
    the whole 45- or 90-minute call times five concurrent roles, so any laptop sleep, OOM, reboot or
    mistimed Ctrl-C left the audit directory permanently unrunnable. ``response_received`` is
    already auto-recovered; this is the missing sibling.

    An attempt is recoverable only when it retained NO response. If a response is on disk the
    right path is the existing recovery, and we refuse rather than discard real work.

    Budget: a legacy ``inference_started`` strand carries an allocated attempt and is charged one,
    exactly as a normal failure would be. A 2.1 ``provider_requested`` strand never had one
    allocated -- the validator is right that a non-started inference cannot consume an attempt --
    so it is recorded with ``attempt: null`` and charges nothing. Neither case can be used to buy
    extra attempts: no inference ran, so none is refunded.
    """
    terminal_ids = {row.get("invocation_id") for row in read_ledger(root)}
    # invocations.jsonl is a projection of TERMINAL rows only, so a strand has none. The
    # reservation the runner made before dispatch lives in the append-only journal, and it already
    # carries every identity field the terminal record needs.
    reserved: dict[str, dict[str, Any]] = {}
    journal = root / "run.events.jsonl"
    if journal.is_file():
        for line in journal.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") != "invocation_reserved":
                continue
            payload = event.get("payload") or {}
            if payload.get("invocation_id"):
                reserved[payload["invocation_id"]] = payload
    started_by_id: dict[str, Any] = {}
    reserved_at: dict[str, Any] = {}
    if journal.is_file():
        for line in journal.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get("payload") or {}
            if not payload.get("invocation_id"):
                continue
            if event.get("type") == "inference_start":
                started_by_id[payload["invocation_id"]] = event.get("at")
            elif (event.get("type") == "invocation_reserved"
                    and payload["invocation_id"] not in reserved_at):
                reserved_at[payload["invocation_id"]] = event.get("at")
    recovered = 0
    for status_path in sorted((phase_root / "roles").glob("*/*/*/status.json")):
        value = runtime.strict_json(status_path)
        if value.get("state") not in STRANDED_STATES:
            continue
        invocation_id = value.get("invocation_id")
        if invocation_id in terminal_ids:
            continue
        attempt_dir = status_path.parent
        stdout_path = attempt_dir / "claude_stdout.json"
        if stdout_path.is_file() and stdout_path.stat().st_size > 0:
            raise RunnerError(
                f"{attempt_dir} retained a provider response; recover it through the normal "
                "received-response path rather than discarding it with --recover-stranded"
            )
        prior = reserved.get(invocation_id)
        if prior is None:
            # Every real strand has a ledger row: reserve_invocation and mark_inference_start both
            # ran before the process died. No row means something other than a kill happened, and
            # inventing a provider/model to satisfy the schema would put fiction in the ledger.
            # A 2.1 runner writes invocation_reserved before it dispatches, so a real strand
            # always has one. A legacy 2.0 workspace has no journal at all, and its status.json
            # carries neither the model nor the prompt hash a terminal record requires --
            # inventing them to satisfy the schema would put fiction in the ledger, which is the
            # one thing this tool must not do. Say which case it is.
            if not (root / "run.events.jsonl").is_file():
                raise RunnerError(
                    f"{attempt_dir} is a legacy (pre-metadata-2.1) attempt: there is no "
                    "run.events.jsonl, so the reservation fields a terminal record needs "
                    "(requested_model, prompt_sha256, role_class) were never recorded and "
                    "cannot be reconstructed without inventing them. Retire this workspace or "
                    "re-run the audit from intake; --recover-stranded cannot terminalize it."
                )
            raise RunnerError(
                f"{attempt_dir} is at {value.get('state')} but has no reservation in the "
                f"journal for {invocation_id}; this is not a killed-process strand and needs "
                "manual diagnosis, not --recover-stranded"
            )
        now = dt.datetime.now(dt.timezone.utc)
        # Most accurate first. status.json's started_at is stamped at dispatch, so it is the
        # real moment this attempt went out; the reservation payload carries no timestamp of its
        # own, only the journal row's `at`. Falling straight through to now() -- which is what
        # this did -- recorded every days-old strand as having started the instant it was
        # recovered, and an elapsed_seconds of 0.001 for a call that hung overnight.
        started_at = (
            value.get("started_at")
            or prior.get("started_at")
            or started_by_id.get(invocation_id)
            or reserved_at.get(invocation_id)
            or iso(now)
        )
        ended_at = iso(now)
        try:
            started_dt = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            started_dt = now
            started_at = iso(now)
        # Nothing was observed, so every observation field is null and every accounting field
        # says "unavailable" rather than zero. A killed call produced no tokens, no cost and no
        # served model, and recording zeros would put measurements in the ledger that nobody made.
        record: dict[str, Any] = {
            "invocation_id": invocation_id,
            "role": prior.get("role"),
            "role_class": prior.get("role_class") or "auditor",
            "round": prior.get("round", 0),
            # A 2.1 strand at provider_requested never had an attempt allocated -- the runner
            # allocates one only when the provider stream proves acceptance -- and the validator
            # rightly refuses an attempt number on a non-started inference. Preserve the
            # distinction rather than inventing attempt 1: nothing was inferred, so nothing is
            # charged, and the role's budget is untouched. A legacy inference_started strand
            # does carry its allocated attempt and is charged exactly as a normal failure is.
            "attempt": value.get("attempt"),
            "status": "failed",
            "provider": prior.get("provider") or "anthropic",
            "provider_call": True,
            "cache_usage": "none",
            "session_id": value.get("requested_session_id"),
            "requested_model": prior.get("requested_model"),
            "observed_model": None,
            "model_family": "claude",
            "model_source": "declared",
            # A reservation always carries the effort. One that did not was recorded as "max", every
            # route's effort before 2.7.3; the snapshot route's own effort is preferred when it names
            # a live route (2.7.3), so a new audit's strand is not recorded at an effort it never ran.
            "reasoning_effort": (prior.get("reasoning_effort")
                                 or snapshot_route_effort(root, prior.get("role"),
                                                          prior.get("role_class") or "auditor")
                                 or "max"),
            "started_at": started_at,
            "ended_at": ended_at,
            # The interval the attempt stayed outstanding -- dispatch to the moment recovery
            # terminalized it -- NOT the duration of the provider call, which nobody observed.
            # The validator requires this to agree with the two timestamps, and the record's own
            # error text says the response was never retained, so it cannot be read as a runtime.
            "elapsed_seconds": elapsed(started_dt, now),
            "input_artifact_sha256": prior.get("input_artifact_sha256"),
            "prompt_sha256": prior.get("prompt_sha256"),
            "output_artifact": None,
            "output_sha256": None,
            "usage": {"obtainable": False, "input_tokens": None, "output_tokens": None,
                      "cached_input_tokens": None, "reasoning_tokens": None,
                      "total_tokens": None, "source": "unavailable"},
            "error": "process ended before the provider response was retained; terminalized by "
                     "--recover-stranded",
            "error_class": "transient",
            "legacy_source": None,
            "attempt_kind": value.get("attempt_kind") or "role_inference",
            "provider_request_status": value.get("provider_request_status") or "requested",
            "inference_start_status": value.get("inference_start_status") or (
                "started" if value.get("state") == "inference_started" else "not_started"),
            "source_invocation_id": prior.get("source_invocation_id"),
            "turn_count": None,
            "provider_cost": None,
            "served_model": None,
            "identity_assurance": "requested_only",
            "model_identity_evidence": {"source": "unavailable", "raw_ids": [],
                                        "artifact": None, "artifact_sha256": None},
            "usage_by_model": [],
            "unattributed_usage": None,
            "provider_duration_seconds": None,
        }
        if record["inference_start_status"] == "started" and record["attempt"] is None:
            record["attempt"] = 1
        # 2.7.2: a 2.1 strand whose output workspace holds files the model wrote was an accepted
        # call that ran until its process died (a killed runner, or a pre-2.7.2 timeout, which
        # stranded every attempt it stopped). Booking it as a request the provider never accepted
        # made two such strands refuse the role for good (role_preinference_request_count). Charge it
        # one attempt, as a finished failure is charged. If the journal has no attempt left to
        # allocate, keep the uncharged pre-2.7.2 record rather than leave the strand unrecoverable.
        if (
            record["inference_start_status"] == "not_started"
            and record["attempt"] is None
            and metadata_21(root)
            and workspace_writes(attempt_dir / "output.partial", frozenset())
        ):
            try:
                record["attempt"] = lifecycle_started(root, {"invocation_id": invocation_id})
            except (RunnerError, lifecycle_journal.JournalError, ValueError) as exc:
                print(f"  [note ] {invocation_id}: the model wrote to its workspace but no attempt "
                      f"could be allocated ({exc}); recording it uncharged", file=sys.stderr)
            else:
                record["inference_start_status"] = "started"
                record["provider_request_status"] = "accepted"
                record["error"] = (
                    "process ended before the provider response was retained; the output workspace "
                    "holds files the model wrote, so inference had started; terminalized by "
                    "--recover-stranded"
                )
        # Validate BEFORE appending. record_terminal appends the event and then projects, so an
        # incomplete record does not fail cleanly: it lands in the append-only journal and every
        # later projection crashes on it. Every other terminal write here validates first.
        record_invocation.validate_record(record, root)
        record_invocation.record_terminal(root, record)
        runtime.atomic_write(status_path, runtime.pretty_bytes({
            "schema_version": "i4r-phase1-attempt-1",
            "state": "failed",
            "invocation_id": invocation_id,
            "attempt": record["attempt"],
            "attempt_kind": record["attempt_kind"],
            "provider_request_status": value.get("provider_request_status"),
            "inference_start_status": record["inference_start_status"],
            "requested_session_id": value.get("requested_session_id"),
            "error": record["error"],
            "error_class": record["error_class"],
            "ended_at": record["ended_at"],
        }))
        print(f"recovered stranded attempt {invocation_id} at {attempt_dir}", file=sys.stderr)
        recovered += 1
    return recovered


def guard_stranded_attempts(root: pathlib.Path, phase_root: pathlib.Path) -> None:
    terminal_ids = {row.get("invocation_id") for row in read_ledger(root)}
    for status_path in sorted((phase_root / "roles").glob("*/*/*/status.json")):
        value = runtime.strict_json(status_path)
        if value.get("state") == "response_received":
            # The content-addressed role path recovers this response before considering a new call.
            continue
        if value.get("state") in STRANDED_STATES:
            invocation_id = value.get("invocation_id")
            if invocation_id in terminal_ids:
                continue
            # A live sibling is not a strand. The guard runs once, before this run dispatches
            # anything, so a live pid here belongs to another process holding the audit.
            pid = value.get("provider_pid")
            if isinstance(pid, int) and pid > 0 and process_alive(pid):
                raise RunnerError(
                    f"Phase-1 invocation {invocation_id} at {status_path.parent} is still in "
                    f"flight (provider pid {pid} is alive). Another run holds this audit; wait "
                    "for it to finish rather than dispatching a second, duplicate paid call."
                )
            # response_received can be recovered only through its retained output; automatic
            # re-dispatch would risk a duplicate call.  This release leaves explicit evidence for
            # a recovery tool and blocks the duplicate.
            raise RunnerError(
                f"unresolved Phase-1 invocation {invocation_id} at {status_path.parent} "
                f"(state {value.get('state')}); recover its retained response before dispatching "
                "another call. If the process was killed and no response was retained, clear it "
                "with: python3 scripts/run_phase1.py <audit-dir> --recover-stranded"
            )


def write_status(phase_root: pathlib.Path, value: dict[str, Any]) -> None:
    runtime.atomic_write(phase_root / "status.json", runtime.pretty_bytes({
        "schema_version": "i4r-phase1-status-1", **value,
    }))


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def bounded_timeout(value: str) -> int:
    parsed = positive_int(value)
    if parsed > MAXIMUM_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"must not exceed the largest canonical {MAXIMUM_TIMEOUT_SECONDS}-second limit"
        )
    return parsed


def bounded_pre_dispatch_deadline(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number of seconds") from exc
    if not parsed > 0 or parsed > DEFAULT_PRE_DISPATCH_DEADLINE_SECONDS:
        raise argparse.ArgumentTypeError(
            f"must be positive and no greater than {DEFAULT_PRE_DISPATCH_DEADLINE_SECONDS} seconds"
        )
    return parsed


def bounded_budget(value: str) -> str:
    try:
        parsed = decimal.Decimal(value)
        maximum = decimal.Decimal(DEFAULT_MAX_BUDGET_USD)
    except decimal.InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a decimal USD amount") from exc
    if not parsed.is_finite() or parsed <= 0 or parsed > maximum:
        raise argparse.ArgumentTypeError(
            f"must be positive and no greater than ${DEFAULT_MAX_BUDGET_USD}"
        )
    return format(parsed, "f")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--claude-bin", default=None, help="Claude CLI executable (fake runner in tests)")
    parser.add_argument("--probe-cache-dir", type=pathlib.Path, default=None)
    # 2.7.2: default None = this audit's frozen timeout (phase1_timeout); an explicit value must equal it.
    parser.add_argument("--timeout-seconds", type=bounded_timeout, default=None)
    parser.add_argument(
        "--pre-dispatch-deadline-seconds",
        type=bounded_pre_dispatch_deadline,
        default=DEFAULT_PRE_DISPATCH_DEADLINE_SECONDS,
        help="provider-free preparation deadline; may only tighten the canonical 60-second cap",
    )
    parser.add_argument("--max-budget-usd", type=bounded_budget, default=DEFAULT_MAX_BUDGET_USD)
    parser.add_argument(
        "--probe-only", action="store_true",
        help="run preflight and the bounded capability probe, print the result, and exit "
             "without dispatching any auditor; a cheap live canary for credentials, model "
             "routing, sandbox tooling and the probe token bound",
    )
    parser.add_argument(
        "--recover-stranded", action="store_true",
        help="terminalize attempts a killed process left in a non-terminal state with no "
             "retained response, then exit. Under metadata 2.1 that state is provider_requested, "
             "not inference_started, and such an attempt never had an attempt number allocated, "
             "so recovering it charges nothing; a legacy inference_started strand is charged one "
             "attempt, exactly as a normal failure is. The documented escape from an audit that "
             "cannot dispatch because a process died mid-call.",
    )
    args = parser.parse_args(argv)

    root = args.audit_dir.resolve()
    if not root.is_dir():
        print(f"ERROR: audit directory does not exist: {root}", file=sys.stderr)
        return 1
    phase_root = root / "phase1"
    if phase_root.is_symlink() or (phase_root.exists() and not phase_root.is_dir()):
        print(f"ERROR: unsafe Phase-1 runtime path: {phase_root}", file=sys.stderr)
        return 1
    phase_root.mkdir(exist_ok=True)
    cache_dir = (args.probe_cache_dir or (phase_root / "probe_cache")).resolve()
    claude = args.claude_bin or shutil.which("claude")
    if not claude:
        print("ERROR: Claude CLI not found", file=sys.stderr)
        return 1
    run_lock_path = phase_root / "run.lock"
    with run_lock_path.open("a+") as run_lock:
        lock_wait.acquire_exclusive(run_lock, "the Phase-1 run lock")
        if args.recover_stranded:
            try:
                count = recover_stranded_attempts(root, phase_root)
            except RunnerError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            print(json.dumps({"recovered_stranded_attempts": count}))
            return 0
        try:
            policy, configured = load_policy(root)
            bound = probe_bound(policy)
            audit_timeout = phase1_timeout(policy)
            if args.timeout_seconds is None:
                args.timeout_seconds = audit_timeout
            run = load_run(root)
            if (
                args.timeout_seconds != audit_timeout
                or decimal.Decimal(args.max_budget_usd)
                != decimal.Decimal(str(policy["phase_1"]["runtime"]["max_provider_budget_usd"]))
            ):
                raise RunnerError(
                    f"canonical Phase-1 runs require the policy-bound {audit_timeout}-second/$15 limits; "
                    "timeout or budget overrides are non-canonical and rejected"
                )
            guard_stranded_attempts(root, phase_root)
            try:
                pre_dispatch = runtime.MonotonicDeadline.after(
                    args.pre_dispatch_deadline_seconds
                )
                preparation_started = time.monotonic()
                # Inside the SIGALRM deadline the snapshot's content and inventory are verified
                # strictly but its modes are not judged (lenient_modes): SIGALRM can interrupt any
                # syscall, and a re-freeze walk cut short would leave the published snapshot
                # half-frozen with no note. Mode drift is re-frozen just below, once the deadline
                # is disarmed and before any reservation.
                with hard_pre_dispatch_deadline(pre_dispatch):
                    cli = cli_version(claude, pre_dispatch)
                    snapshot, snapshot_manifest = runtime.materialize_snapshot(
                        root, phase_root, pre_dispatch, lenient_modes=True,
                    )
                    packet_dirs, packets, prompts = materialize_role_packets(
                        phase_root, snapshot, snapshot_manifest, configured, run["audit_id"],
                        pre_dispatch,
                    )
                    preflight = runtime.provider_free_preflight(
                        root, phase_root, snapshot, snapshot_manifest, packet_dirs, pre_dispatch,
                        lenient_modes=True,
                    )
                    preflight["deadline_seconds"] = args.pre_dispatch_deadline_seconds
                    preflight["elapsed_seconds"] = round(
                        time.monotonic() - preparation_started, 3,
                    )
                    runtime.atomic_write(
                        phase_root / "preflight" / snapshot_manifest["snapshot_sha256"]
                        / "receipt.json",
                        runtime.pretty_bytes(preflight),
                    )
                # Outside the deadline, not yet reserved: the one re-freeze walk, journaled by
                # the process that ran it. A cheap lstat walk decides whether it is needed.
                runtime.refreeze_published_snapshot(
                    snapshot, snapshot_manifest,
                    on_refreeze=lambda facts: journal_snapshot_refreeze(
                        root, snapshot, facts, "preflight"),
                )
            except Exception as exc:
                write_status(phase_root, {
                    "status": "paused", "pause_reason": "local_preflight_failed",
                    "auditor_inferences_started": 0, "error": str(exc),
                })
                journal_pause(root, phase_root, "local_preflight_failed", str(exc))
                print(f"PAUSED: Phase-1 local preflight failed: {exc}", file=sys.stderr)
                return 3
            journal_resume(root)
            write_status(phase_root, {
                "status": "preflight_complete", "pause_reason": None,
                "auditor_inferences_started": 0,
                "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
                "preflight": preflight,
            })
            first_model = configured[next(iter(runtime.ROLE_SPECS))]
            try:
                probe_key_value, probe, probe_reused = run_capability_probe(
                    root, phase_root, cache_dir, claude, cli, first_model, args.timeout_seconds,
                    bound=bound,
                )
            except Exception as exc:
                write_status(phase_root, {
                    "status": "paused", "pause_reason": "capability_probe_failed",
                    "auditor_inferences_started": 0,
                    "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
                    "error": str(exc),
                })
                journal_pause(root, phase_root, "capability_probe_failed", str(exc))
                print(f"PAUSED: Phase-1 capability probe failed: {exc}", file=sys.stderr)
                return 4
            journal_resume(root)
            if args.probe_only:
                write_status(phase_root, {
                    "status": "probe_only_complete", "pause_reason": None,
                    "auditor_inferences_started": 0,
                    "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
                    "probe_key": probe_key_value, "probe_reused": probe_reused,
                })
                print(json.dumps({
                    "status": "probe_only_complete",
                    "probe_key": probe_key_value,
                    "probe_reused": probe_reused,
                    "normalized_total_tokens": probe.get("normalized_total_tokens"),
                    "probe_max_normalized_tokens": bound,
                    "cli_version": cli,
                    "auditor_inferences_started": 0,
                }, sort_keys=True))
                return 0
            write_status(phase_root, {
                "status": "running", "pause_reason": None,
                "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
                "probe_key": probe_key_value, "probe_reused": probe_reused,
            })
            journal_phase(
                root, "phase_start",
                source_snapshot_sha256=snapshot_manifest["snapshot_sha256"],
                probe_key=probe_key_value,
            )
            completions: dict[str, RoleCompletion] = {}
            failures: dict[str, str] = {}
            environment_faults: dict[str, str] = {}
            snapshot_faults: dict[str, str] = {}
            content_faults = 0  # snapshot faults that are not SnapshotModeUnstableError
            with concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="i4r-phase1") as pool:
                futures = {
                    pool.submit(
                        run_role, root, phase_root, snapshot, snapshot_manifest, role,
                        packet_dirs[role], packets[role], prompts[role], probe_key_value,
                        claude, configured[role], args.timeout_seconds, args.max_budget_usd,
                        portable_probe_key(cli, first_model, bound=bound),
                    ): role
                    for role in runtime.ROLE_SPECS
                }
                for future in concurrent.futures.as_completed(futures):
                    role = futures[future]
                    try:
                        completions[role] = future.result()
                    except Exception as exc:
                        # status.json is provider-visible and a release artefact; this path
                        # bypasses make_terminal_record, which is where redaction used to live.
                        failures[role] = run_coordinator.redact_private_paths(str(exc), root)
                        if isinstance(exc, EnvironmentFaultError):
                            environment_faults[role] = failures[role]
                        elif isinstance(exc, runtime.SnapshotIntegrityError):
                            snapshot_faults[role] = failures[role]
                            if not isinstance(exc, runtime.SnapshotModeUnstableError):
                                content_faults += 1
            if snapshot_faults:
                # The frozen snapshot no longer verifies: every role would fail the same way, so
                # pause, as for a sandbox fault, rather than report the roles as failed. Through
                # status.json only; run.json's pause_reason enum has no such value. A mode that
                # would not stay frozen (content verified) is its own reason: deleting the
                # snapshot cannot cure what keeps changing its modes.
                snapshot_rel = relative_to(root, snapshot)
                if content_faults:
                    pause_reason = "snapshot_integrity_fault"
                    remedy = (
                        "the Phase-1 snapshot's content no longer matches its manifest; in the "
                        f"audit directory run `{runtime.snapshot_delete_remedy(snapshot_rel)}` (it "
                        "is content-addressed and rebuilt identically from the deposit) and re-run"
                    )
                else:
                    pause_reason = runtime.SNAPSHOT_MODE_UNSTABLE
                    remedy = (
                        f"the Phase-1 snapshot {snapshot_rel} verified against its manifest, but "
                        "its modes kept changing after repeated re-freezes; stop the sync client, "
                        "or move the audit off the synced folder, then re-run"
                    )
                write_status(phase_root, {
                    "status": "paused", "pause_reason": pause_reason,
                    "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
                    "probe_key": probe_key_value,
                    "completed_roles": sorted(completions),
                    "failed_roles": failures,
                    "environment_faults": {**environment_faults, **snapshot_faults},
                    "remedy": remedy,
                })
                print(json.dumps({
                    "status": "paused", "pause_reason": pause_reason,
                    "environment_faults": {**environment_faults, **snapshot_faults},
                    "remedy": remedy,
                }, indent=2))
                return 3
            if environment_faults:
                write_status(phase_root, {
                    "status": "paused", "pause_reason": "sandbox_environment_fault",
                    "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
                    "probe_key": probe_key_value,
                    "completed_roles": sorted(completions),
                    "failed_roles": failures,
                    "environment_faults": environment_faults,
                })
                print(json.dumps({
                    "status": "paused", "pause_reason": "sandbox_environment_fault",
                    "environment_faults": environment_faults,
                }, indent=2))
                return 3
            if failures:
                write_status(phase_root, {
                    "status": "failed", "pause_reason": None,
                    "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
                    "probe_key": probe_key_value,
                    "completed_roles": sorted(completions),
                    "failed_roles": failures,
                })
                print(json.dumps({
                    "status": "failed", "completed_roles": sorted(completions),
                    "failed_roles": failures,
                }, sort_keys=True), file=sys.stderr)
                journal_phase(root, "phase_end", status="failed", failed_roles=sorted(failures))
                return 2
            ordered = [completions[role] for role in runtime.ROLE_SPECS]
            quorum = {
                "schema_version": "i4r-phase1-quorum-1",
                "status": "quorum_complete",
                "audit_id": run["audit_id"],
                "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
                "probe_key": probe_key_value,
                "probe_source_invocation_id": probe["source_invocation_id"],
                "roles": [completion.__dict__ for completion in ordered],
            }
            quorum["quorum_sha256"] = runtime.sha256_bytes(runtime.canonical_bytes(quorum))
            runtime.atomic_write(phase_root / "quorum.json", runtime.pretty_bytes(quorum))
            journal_phase(
                root, "phase_end", status="quorum_complete",
                quorum_sha256=quorum["quorum_sha256"],
            )
            write_status(phase_root, {
                "status": "quorum_complete", "pause_reason": None,
                "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
                "probe_key": probe_key_value,
                "completed_roles": list(runtime.ROLE_SPECS),
                "reused_roles": [row.role for row in ordered if row.reused],
            })
            print(json.dumps({
                "status": "quorum_complete",
                "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
                "probe_key": probe_key_value,
                "probe_reused": probe_reused,
                "roles": [completion.__dict__ for completion in ordered],
            }, sort_keys=True))
            return 0
        except Exception as exc:
            write_status(phase_root, {
                "status": "failed", "pause_reason": None, "error": str(exc),
            })
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            fcntl.flock(run_lock.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    raise SystemExit(main())
