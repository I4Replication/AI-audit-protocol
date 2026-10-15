#!/usr/bin/env python3
"""Fail when maintained workflow docs drift from pipeline-policy-v2.json."""

from __future__ import annotations

import html
import importlib.util
import json
import re
import sys
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "pipeline-policy-v2.json"
VERIFIER_CONTRACT_PATH = ROOT / "scripts" / "verifier_contract.py"
CLAUDE_ROUTES_PATH = ROOT / "scripts" / "claude_routes.py"
PROBE_BOUNDS_PATH = ROOT / "scripts" / "probe_bounds.py"
PHASE1_TIMEOUTS_PATH = ROOT / "scripts" / "phase1_timeouts.py"
CRITIQUE_TIMEOUTS_PATH = ROOT / "scripts" / "critique_timeouts.py"
CLAIM_CHANGE_RULES_PATH = ROOT / "scripts" / "claim_change_rules.py"
VERIFIER_TABLE = re.compile(
    r"<!-- VERIFIER-VERDICT-OUTCOME:START -->(.*?)<!-- VERIFIER-VERDICT-OUTCOME:END -->", re.DOTALL)
ROUTE_KEYS = ("requested_model", "spawn_alias", "provider", "reasoning_effort")
# 2.7.0: the Claude route a NEW audit freezes for the coordinator, the five Phase-1 auditors, their probe and
# the post-fan-out verifier. A literal rather than an import of scripts/claude_routes.py, so reverting the
# module's SHIPPED together with the policy still fails: check_claude_routes joins the two. Every audit
# initialised on 2.6.10 or earlier froze LEGACY_CLAUDE_ROUTE and keeps it for its whole life.
# 2.7.3: the shipped effort is xhigh, one degree below max. Every audit initialised on 2.7.0 to 2.7.2 froze
# SONNET_5_5_MAX_ROUTE and keeps it for its whole life, so an allow-set of live routes would accept a revert to
# max: every Claude pin compares with this literal.
SHIPPED_CLAUDE_ROUTE = {
    "requested_model": "sonnet-5-5", "spawn_alias": "claude-sonnet-5-5",
    "provider": "anthropic", "reasoning_effort": "xhigh",
}
SHIPPED_CLAUDE_LABEL = "Sonnet 5.5 / xhigh"
LEGACY_CLAUDE_ROUTE = {
    "requested_model": "sonnet-5", "spawn_alias": "sonnet",
    "provider": "anthropic", "reasoning_effort": "max",
}
SONNET_5_5_MAX_ROUTE = {
    "requested_model": "sonnet-5-5", "spawn_alias": "claude-sonnet-5-5",
    "provider": "anthropic", "reasoning_effort": "max",
}
# 2.7.0: the Codex model a NEW audit freezes for the four critics and the critique model probe (effort high) and
# the targeted adjudicator (effort max). run_critique.sh reads the route from the audit's own snapshot, so every
# audit initialised on 2.6.10 or earlier keeps LEGACY_CRITIC_MODEL, which the driver still dispatches (2.6.0 moved
# new audits to gpt-6-sol and 2.6.1 back to Terra the same way). A literal, like SHIPPED_CLAUDE_ROUTE.
SHIPPED_CRITIC_MODEL = "gpt-6.1-sol"
LEGACY_CRITIC_MODEL = "gpt-5.6-terra"
# 2.7.1: the Phase-1 capability-probe bound (normalized tokens) a NEW audit freezes, in phase_1.runtime and in the
# descriptive capability_probe leaf. The bound is part of the probe contract, so every audit keeps the one it froze:
# every audit initialised before 2.7.1 froze LEGACY_PROBE_BOUND, which scripts/probe_bounds.py still admits. A
# literal, like SHIPPED_CLAUDE_ROUTE: check_probe_bounds joins it to the module, so a revert of both still fails.
SHIPPED_PROBE_BOUND = 350000
LEGACY_PROBE_BOUND = 150000
# 2.7.2: the per-call Phase-1 provider timeout (seconds) a NEW audit freezes, in phase_1.runtime and in the
# descriptive phase_1.provider_timeout_seconds leaf. Every audit keeps the one it froze: every audit initialised
# before 2.7.2 froze LEGACY_PHASE1_TIMEOUT, which scripts/phase1_timeouts.py still admits. A literal, like
# SHIPPED_PROBE_BOUND: check_phase1_timeouts joins it to the module, so a revert of both still fails.
SHIPPED_PHASE1_TIMEOUT = 5400
LEGACY_PHASE1_TIMEOUT = 2700
# 2.7.4: the per-call critique ceiling (seconds) a NEW audit freezes in critique.provider_timeout_seconds. Every
# audit initialised before 2.7.4 froze no such leaf and keeps LEGACY_CRITIQUE_TIMEOUT and the operator's
# I4R_PROVIDER_TIMEOUT_SECONDS, as before. A literal: check_critique_timeouts joins it to the module.
SHIPPED_CRITIQUE_TIMEOUT = 5400
LEGACY_CRITIQUE_TIMEOUT = 2700
# 2.7.4: the later-round claim-change rule a NEW audit freezes (specialists re-review only substantive changes;
# completeness reviews every changed finding). An audit initialised earlier froze no leaf and keeps the legacy
# rule. A literal: check_claim_change_rules joins it to scripts/claim_change_rules.py.
SHIPPED_CLAIM_CHANGE_RULE = "substantive_specialists_1"
# A value printed for init_run.sh --auditor-model: separated by whitespace or "=", optionally opened by a quote.
AUDITOR_MODEL_FLAG = re.compile(r"--auditor-model(?:\s+|=)[\"']?([A-Za-z0-9][A-Za-z0-9._-]*)")
# The HTML tags the maintained documents use, by name: a Markdown placeholder such as <audit-dir> is not a tag, and
# stripping it would join the flag to the next word.
HTML_TAG = re.compile(r"</?(?:a|b|br|code|div|em|h[1-6]|i|kbd|li|ol|p|pre|span|strong|sub|sup|table|tbody|td|"
                      r"th|thead|tr|tt|u|ul)(?:\s[^<>]*)?/?>", re.IGNORECASE)
_VERIFIER_CONTRACT = None
_CLAUDE_ROUTES = None
_PROBE_BOUNDS = None
_PHASE1_TIMEOUTS = None
_CRITIQUE_TIMEOUTS = None
_CLAIM_CHANGE_RULES = None


def load_policy() -> dict:
    with POLICY_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_verifier_contract():
    """scripts/verifier_contract.py from THIS tree (never another copy on sys.path), loaded once."""
    global _VERIFIER_CONTRACT
    if _VERIFIER_CONTRACT is None:
        spec = importlib.util.spec_from_file_location("i4r_drift_verifier_contract", VERIFIER_CONTRACT_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {VERIFIER_CONTRACT_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _VERIFIER_CONTRACT = module
    return _VERIFIER_CONTRACT


def load_claude_routes():
    """scripts/claude_routes.py from THIS tree (never another copy on sys.path), loaded once."""
    global _CLAUDE_ROUTES
    if _CLAUDE_ROUTES is None:
        spec = importlib.util.spec_from_file_location("i4r_drift_claude_routes", CLAUDE_ROUTES_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {CLAUDE_ROUTES_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CLAUDE_ROUTES = module
    return _CLAUDE_ROUTES


def load_probe_bounds():
    """scripts/probe_bounds.py from THIS tree (never another copy on sys.path), loaded once."""
    global _PROBE_BOUNDS
    if _PROBE_BOUNDS is None:
        spec = importlib.util.spec_from_file_location("i4r_drift_probe_bounds", PROBE_BOUNDS_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {PROBE_BOUNDS_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PROBE_BOUNDS = module
    return _PROBE_BOUNDS


def load_phase1_timeouts():
    """scripts/phase1_timeouts.py from THIS tree (never another copy on sys.path), loaded once."""
    global _PHASE1_TIMEOUTS
    if _PHASE1_TIMEOUTS is None:
        spec = importlib.util.spec_from_file_location("i4r_drift_phase1_timeouts", PHASE1_TIMEOUTS_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {PHASE1_TIMEOUTS_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PHASE1_TIMEOUTS = module
    return _PHASE1_TIMEOUTS


def printed_auditor_models(text: str) -> list[str]:
    """Every model a document prints for init_run.sh --auditor-model, in order, each once.

    The document is read as a reader copies it: HTML tags stripped (`--auditor-model <b>sonnet-5</b>`),
    entities decoded (CHEATSHEET.html writes `--auditor-model&nbsp;sonnet-5`), and a backslash-newline
    continuation joined (`--auditor-model \\` on one line, the model on the next). The text is read with its
    tags as well, so stripping can only add a flag. Until 2.7.0 only the raw text was read, and each of those
    forms passed, as did a quoted value (`--auditor-model "sonnet-5"`).
    """
    found: list[str] = []
    for variant in (text, HTML_TAG.sub("", text)):
        plain = re.sub(r"\\[ \t]*\r?\n", " ", html.unescape(variant).replace("\u00a0", " "))
        for named in AUDITOR_MODEL_FLAG.findall(plain):
            if named not in found:
                found.append(named)
    return found


def policy_summary(policy: dict) -> str:
    coordinator = policy["models"]["coordinator"]
    auditors = policy["models"]["phase_1_auditors"]
    verifier = policy["models"]["post_fan_out_verifier"]
    verification = policy["post_fan_out_verification"]
    critics = policy["models"]["critics"]
    adjudicator = policy["models"]["targeted_adjudicator"]
    critique = policy["critique"]
    r1 = critique["round_1"]
    later = critique["later_rounds"]
    settled = critique["settled_facts"]

    # The areas grouped by route, all read from the policy. Until 2.7.0 this kept only the areas spawned as
    # `sonnet` and printed a literal `sonnet-5` (`sonnet`), so a Sonnet 5.5 policy generated "all areas  use
    # `sonnet-5`" and the byte-exact comparison below could pass while SKILL.md was wrong.
    phase1_routes: dict[tuple[str, str, str], list[int]] = {}
    for entry in auditors.values():
        route = (entry["requested_model"], entry["spawn_alias"], entry["reasoning_effort"])
        phase1_routes.setdefault(route, []).append(entry["area"])
    phase1_text = "; ".join(
        f"{'all areas' if len(phase1_routes) == 1 else 'areas'} {','.join(map(str, sorted(areas)))} "
        f"use `{model}` (`{alias}`), effort `{effort}`"
        for (model, alias, effort), areas in sorted(phase1_routes.items(), key=lambda item: min(item[1])))
    role_labels = [name.removeprefix("critic_") for name in r1["roles"]]

    return "\n".join(
        [
            "<!-- PIPELINE-POLICY-V2:START -->",
            f"- Coordinator: `{coordinator['requested_model']}` (`{coordinator['spawn_alias']}`), effort `{coordinator['reasoning_effort']}`.",
            f"- Phase 1: {len(auditors)} auditors, {policy['phase_1']['parallelism']}-up; {phase1_text}.",
            f"- Post-fan-out verifier: `{verifier['requested_model']}` (`{verifier['spawn_alias']}`), effort `{verifier['reasoning_effort']}`, auditor family; one blind call per batch of INCLUDED rows raised after fan-out, under the Phase-1 runtime contract; {verification['default_sessions']} calls by default.",
            f"- Critics: `{critics['requested_model']}`, effort `{critics['reasoning_effort']}`.",
            f"- Critique: target {critique['round_target']} rounds, hard cap {critique['round_cap']}; round 1 = {', '.join(role_labels)}, {r1['parallelism']}-up.",
            f"- Later rounds: completeness plus one best specialist by default ({later['default_total_critics']} critics); add specialists only for conclusion-changing or disclosure-critical changes spanning multiple domains.",
            f"- Synthesis: deterministic merge; `{adjudicator['requested_model']}`, effort `{adjudicator['reasoning_effort']}`, adjudicates only policy-triggered conflicts or high-risk evidence gaps.",
            f"- Retry budget: {critique['max_attempts_per_role_total']} total attempts per role by default across parallel, serial, and resumed execution; the operator may raise the ceiling to {critique['max_attempts_per_role_operator_ceiling']} (`MAX_TRIES` for critics, `I4R_MAX_ATTEMPTS` for Phase 1 and the coordinator).",
            f"- Settled facts: generate after round {settled['generate_after_round']} after {settled['minimum_independent_confirmations']} independent confirmations spanning {settled['minimum_distinct_model_families']} model families; inject from round {settled['inject_from_round']}.",
            "- Stop values: `target_complete` after the required reviewed rounds; `converged` only after continuing; `cap_with_unresolved` at the audit's round cap; `aborted` only for a genuine blocker or failed run.",
            "<!-- PIPELINE-POLICY-V2:END -->",
        ]
    )


def check_policy_shape(policy: dict, errors: list[str]) -> None:
    auditors = policy["models"]["phase_1_auditors"]
    aliases = {entry["area"]: entry["spawn_alias"] for entry in auditors.values()}
    expected_aliases = {area: SHIPPED_CLAUDE_ROUTE["spawn_alias"] for area in range(1, 6)}
    if aliases != expected_aliases:
        errors.append(f"auditor routing is {aliases}, expected {expected_aliases}")
    if any(entry.get("provider") != SHIPPED_CLAUDE_ROUTE["provider"] for entry in auditors.values()):
        errors.append("one or more phase-1 auditors is not assigned to Anthropic")
    shipped_model = (SHIPPED_CLAUDE_ROUTE["requested_model"], SHIPPED_CLAUDE_ROUTE["reasoning_effort"])
    if any(
        (entry.get("requested_model"), entry.get("reasoning_effort")) != shipped_model
        for entry in auditors.values()
    ):
        errors.append(f"one or more phase-1 auditors is not assigned to {SHIPPED_CLAUDE_LABEL}")
    # The probe runs on the auditors' route (its cache key takes the first auditor's); this descriptive leaf
    # was free until 2.7.0, so it could keep naming Sonnet 5 beside five Sonnet 5.5 auditors.
    probe = policy["phase_1"].get("capability_probe") or {}
    if (probe.get("requested_model"), probe.get("reasoning_effort")) != shipped_model:
        errors.append(f"the Phase-1 capability probe is not {SHIPPED_CLAUDE_LABEL}, the auditors' route")
    # 2.7.1: the probe bound a new audit freezes. The runtime leaf is also inside the exact runtime pin below; it
    # has its own pin so that a revert is reported as one (and the named control below requires this pin). The
    # capability_probe leaf restates it and was free until 2.7.1, so the two could disagree and pass.
    runtime_bound = (policy["phase_1"].get("runtime") or {}).get("probe_max_normalized_tokens")
    if type(runtime_bound) is not int or runtime_bound != SHIPPED_PROBE_BOUND:
        errors.append(f"the Phase-1 probe bound phase_1.runtime.probe_max_normalized_tokens is {runtime_bound!r}, "
                      f"not {SHIPPED_PROBE_BOUND}")
    leaf_bound = probe.get("maximum_normalized_tokens")
    if type(leaf_bound) is not int or leaf_bound != SHIPPED_PROBE_BOUND:
        errors.append(f"the capability-probe leaf maximum_normalized_tokens is {leaf_bound!r}, not "
                      f"{SHIPPED_PROBE_BOUND}, the Phase-1 probe bound")
    # 2.7.2: the per-call timeout a new audit freezes, pinned in both leaves like the probe bound above.
    runtime_timeout = (policy["phase_1"].get("runtime") or {}).get("timeout_seconds")
    if type(runtime_timeout) is not int or runtime_timeout != SHIPPED_PHASE1_TIMEOUT:
        errors.append(f"the Phase-1 timeout phase_1.runtime.timeout_seconds is {runtime_timeout!r}, "
                      f"not {SHIPPED_PHASE1_TIMEOUT}")
    leaf_timeout = policy["phase_1"].get("provider_timeout_seconds")
    if type(leaf_timeout) is not int or leaf_timeout != SHIPPED_PHASE1_TIMEOUT:
        errors.append(f"the Phase-1 leaf provider_timeout_seconds is {leaf_timeout!r}, not "
                      f"{SHIPPED_PHASE1_TIMEOUT}, the Phase-1 timeout")
    expected_phase1_runtime = {
        "orchestrator": "scripts/run_phase1.py",
        "timeout_seconds": SHIPPED_PHASE1_TIMEOUT,
        "pre_dispatch_deadline_seconds": 60,
        "max_provider_budget_usd": 15,
        "probe_max_normalized_tokens": SHIPPED_PROBE_BOUND,
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
    if policy["phase_1"].get("runtime") != expected_phase1_runtime:
        errors.append("packaged Phase-1 runtime limits and probe contract drifted")
    expected_preflight = {
        "required_before_inference": True,
        "maximum_seconds": 60,
        "checks": [
            "exact_sandbox_and_tool_policy", "source_and_evidence_readability",
            "attempt_output_writability", "canonical_output_promotion",
        ],
        "failure_action": "pause_before_any_auditor_inference",
    }
    if policy["phase_1"].get("provider_free_preflight") != expected_preflight:
        errors.append("Phase-1 provider-free preflight/deadline contract drifted")
    expected_repair = {
        "maximum_calls_per_role": 1,
        "maximum_input_fraction": 0.2,
        "scientific_fields_must_be_byte_equivalent": True,
        "byte_frozen_surfaces": [
            "finding_claim", "finding_evidence_and_results", "finding_impact",
            "finding_remedy", "report_prose", "every_evidence_artifact",
        ],
        "permitted_changes": "structural_json_bindings_only",
    }
    if policy["phase_1"].get("repair") != expected_repair:
        errors.append("Phase-1 focused-repair byte-freeze contract drifted")
    # 2.6.4, the post-fan-out verifier (Neudorfer F13-F15). It is the family-A confirmation for rows
    # no Phase-1 auditor raised, so it must stay on the auditors' route and family: a verifier on
    # the critics' family would let one family confirm a row twice.
    verifier = policy["models"].get("post_fan_out_verifier")
    if verifier != {**SHIPPED_CLAUDE_ROUTE, "must_equal_auditor_family": True}:
        errors.append(f"post-fan-out verifier policy is not {SHIPPED_CLAUDE_LABEL} on the Phase-1 auditor family")
    verifier_route = {key: (verifier if isinstance(verifier, dict) else {}).get(key) for key in ROUTE_KEYS}
    if any({key: entry.get(key) for key in ROUTE_KEYS} != verifier_route for entry in auditors.values()):
        errors.append("post-fan-out verifier route differs from the Phase-1 auditors' route")
    # Refer to the Phase-1 contracts by name, never by copied value: a copy could drift from its
    # source while both still passed (see the FREE_LEAVES note below).
    expected_verification = {
        "runner": "scripts/run_verifier.py",
        "contract_module": "scripts/verifier_contract.py",
        "role_class": "verifier",
        "role_pattern": "^verifier_[0-9a-f]{12}$",
        "round": 0,
        "model_policy_ref": "models.post_fan_out_verifier",
        "applies_to": "included_rows_with_no_phase_1_role_in_origin_role_or_independently_found_by",
        "runtime_contract_ref": "phase_1.runtime",
        "admission_contract_ref": "phase_1.admission",
        "repair_contract_ref": "phase_1.repair",
        "framing": "blind_hypothesis_plus_source_locations",
        "packet_fields": ["hypothesis_id", "claim", "claim_sha256", "source_location", "source_hashes"],
        "withheld_inputs": [
            "critic_verdicts", "coordinator_reasoning", "adjudication_records", "settled_facts",
            "finding_ids", "registry_fields_other_than_claim_and_source_location",
            "comment_and_claim_projections", "auditor_checklist_and_brief",
        ],
        "verdicts": ["CONFIRMED", "REFUTED", "UNVERIFIABLE"],
        "binding": "claim_sha256",
        "one_verdict_per_claim": True,
        "timing": "call_ends_no_later_than_the_integration_that_binds_it",
        "run_when": (
            "after_a_round_reaches_quorum_and_before_it_is_integrated_"
            "or_before_round_1_for_rows_written_at_draft_time"
        ),
        "default_sessions": 0,
    }
    if policy.get("post_fan_out_verification") != expected_verification:
        errors.append("post-fan-out verification contract drifted")
    if policy["models"]["coordinator"] != SHIPPED_CLAUDE_ROUTE:
        errors.append(f"coordinator policy is not {SHIPPED_CLAUDE_LABEL}")
    critics = policy["models"]["critics"]
    if critics != {
        "requested_model": SHIPPED_CRITIC_MODEL,
        "provider": "openai",
        "reasoning_effort": "high",
        "must_differ_from_coordinator_family": True,
    }:
        errors.append(f"critic policy is not {SHIPPED_CRITIC_MODEL} / high")
    targeted_adjudicator = policy["models"].get("targeted_adjudicator")
    if targeted_adjudicator != {
        "requested_model": SHIPPED_CRITIC_MODEL,
        "provider": "openai",
        "reasoning_effort": "max",
        "must_differ_from_coordinator_family": True,
    }:
        errors.append(f"targeted-adjudicator policy is not {SHIPPED_CRITIC_MODEL} / max")
    critique = policy["critique"]
    if (critique["round_target"], critique["round_cap"]) != (2, 7):
        errors.append("round target/cap is not 2/7")
    # 2.6.9: an existing audit keeps the cap frozen in its snapshot until a journaled amendment raises it.
    # The amendment contract, the validators' accepted caps and the tools must say one thing.
    amendment = critique.get("round_cap_amendment")
    expected_amendment = {
        "tool": "scripts/amend_round_cap.py",
        "reopen_tool": "scripts/reopen_audit.py",
        "verifier": "scripts/round_cap_amendment.py",
        "event": "policy_amendment",
        "reopen_event": "audit_reopened",
        "may_only_raise": True,
        "maximum": critique["round_cap"],
        "earlier_caps": [5],
    }
    if amendment != expected_amendment:
        errors.append("round-cap amendment contract drifted (tool, events, raise-only, maximum = round_cap, "
                      "earlier caps [5])")
    else:
        for key in ("tool", "reopen_tool", "verifier"):
            if not (ROOT / amendment[key]).is_file():
                errors.append(f"round-cap amendment {key} is missing: {amendment[key]}")
        live_caps = sorted(set(amendment["earlier_caps"]) | {critique["round_cap"]})
        check_round_cap_consumers(live_caps, errors)
    expected_r1 = ["critic_stats", "critic_data", "critic_fairness", "critic_completeness"]
    if critique["round_1"]["roles"] != expected_r1 or critique["round_1"]["parallelism"] != 4:
        errors.append("round-1 critic policy drifted")
    if critique["max_attempts_per_role_total"] != 3:
        errors.append("default retry budget is not three total attempts")
    if critique.get("max_attempts_per_role_operator_ceiling") != 20:
        errors.append("operator attempt ceiling is not 20")
    # 2.7.4: the per-call ceiling a new audit freezes (an older audit has no leaf and keeps 2,700 s)
    critique_timeout = critique.get("provider_timeout_seconds")
    if type(critique_timeout) is not int or critique_timeout != SHIPPED_CRITIQUE_TIMEOUT:
        errors.append(f"the critique ceiling critique.provider_timeout_seconds is {critique_timeout!r}, "
                      f"not {SHIPPED_CRITIQUE_TIMEOUT}")
    # The driver runs the probe on the critics' model and effort; this leaf names that route, pinned apart from the
    # cache contract so a route revert is reported as one.
    critique_probe = critique.get("model_probe")
    critique_probe = critique_probe if isinstance(critique_probe, dict) else {}
    route_leaves = ("requested_model", "reasoning_effort")
    if tuple(critique_probe.get(key) for key in route_leaves) != (SHIPPED_CRITIC_MODEL, "high"):
        errors.append(f"critique model probe is not {SHIPPED_CRITIC_MODEL} / high, the critics' route")
    # 2.7.5: the critic-route lock every new audit freezes; run_critique.sh refuses any other critic, adjudicator or
    # probe model on such an audit, and repin_critic_route.py moves nothing off it.
    route_lock = critique.get("critic_route_lock")
    route_lock = route_lock if isinstance(route_lock, dict) else {}
    if route_lock.get("model") != SHIPPED_CRITIC_MODEL or route_lock.get("since") != "2.7.5":
        errors.append(f"critique.critic_route_lock is not {SHIPPED_CRITIC_MODEL} (since 2.7.5)")
    expected_probe = {
        "cache_scope": "one_success_per_audit_model_and_effort",
        "maximum_successful_provider_calls": 1,
        "reuse_across_rounds": True,
        "provider_schema_compiler_version_bound": True,
        "schema_set_hash_bound": True,
        "cache_key_fields": [
            "cli_version", "requested_model", "reasoning_effort",
            "compiler_sha256", "compiler_version", "schema_set_sha256",
            "probe_contract_sha256",
        ],
        "contracts_exercised": ["critic", "targeted_adjudicator"],
    }
    if {key: value for key, value in critique_probe.items() if key not in route_leaves} != expected_probe:
        errors.append("model probe is not cached once per audit/model/effort")
    settled = critique["settled_facts"]
    if settled["generate_after_round"] != 1:
        errors.append("settled facts are not generated after round 1")
    if settled["generation_timing"] != "immediately_after_round_1_integration":
        errors.append("settled facts are not generated immediately after round-1 integration")
    expected_settled_contract = {
        "runtime_registry_path": "work/findings.jsonl",
        "runtime_row_schema": "schemas/v2/finding-row.schema.json",
        "optional_export_envelope_schema": "schemas/v2/findings-registry.schema.json",
        "generated_outputs": ["SETTLED.json", "SETTLED.md"],
        "minimum_distinct_model_families": 2,
        "require_coordinator_approval": True,
        "require_dependency_fingerprint": True,
        "require_rerunnable_source_hashes": True,
        "require_canonical_registry_hash_binding": True,
        "require_current_report_hash_binding": True,
        "require_valid_for_report_hash": True,
    }
    for key, expected in expected_settled_contract.items():
        if settled.get(key) != expected:
            errors.append(f"settled-facts {key} is {settled.get(key)!r}, expected {expected!r}")
    if critique["round_1"]["synthesis"] != "deterministic_merge_then_targeted_adjudication":
        errors.append("round-1 synthesis is not deterministic merge plus targeted adjudication")
    if critique["round_1"].get("runtime_adjudicate_mode") != "auto":
        errors.append("round-1 runtime does not use conditional targeted adjudication")
    later = critique["later_rounds"]
    if later.get("specialist_selection") != "always_choose_exactly_one_best_matching_specialist":
        errors.append("later-round routing does not select exactly one best specialist by default")
    if later.get("ordinary_cross_domain_rule") != "one_best_matching_specialist_only":
        errors.append("ordinary cross-domain changes can expand the specialist quorum")
    if later.get("cross_domain_rule") != (
        "add_specialists_only_when_multiple_domains_have_conclusion_changing_or_disclosure_critical_changes"
    ):
        errors.append("later-round extra specialists are not restricted to multi-domain critical changes")
    routing = later.get("routing_manifest", {})
    if routing.get("explicit_environment_variable") != "DIFF_MANIFEST":
        errors.append("later-round policy does not use the runtime DIFF_MANIFEST interface")
    if routing.get("always_retain_completeness") is not True:
        errors.append("later-round routing can drop the completeness critic")
    if routing.get("required_scope_metadata") != ["changed_claim_ids", "changed_sections"]:
        errors.append("later-round diff manifest does not retain changed claims and sections")
    if routing.get("generated_packet_verifier") != "scripts/verify_diff_packet.py":
        errors.append("later-round diff manifests are not recomputed before use")
    if routing.get("claim_change_rule") != SHIPPED_CLAIM_CHANGE_RULE:
        errors.append("the later-round claim-change rule critique.later_rounds.routing_manifest.claim_change_rule is "
                      f"{routing.get('claim_change_rule')!r}, not {SHIPPED_CLAIM_CHANGE_RULE!r}")
    if later.get("parallelism_default") != "actual_routed_role_count":
        errors.append("later-round parallelism is not derived from the routed role count")
    if later.get("runtime_adjudicate_mode") != "auto":
        errors.append("later-round runtime does not use conditional targeted adjudication")
    expected_post_delta = {
        "required": ["consistency", "privacy", "checklist", "archive"],
        "runner": "scripts/run_post_delta_gates.py",
        "receipt": "critique/round_<N>/POST_DELTA_GATES.json",
        "completion_contract": "all_four_pass_on_one_content_bound_snapshot",
        "finalization_replays_latest_receipt": True,
    }
    if later.get("post_delta_gates") != expected_post_delta:
        errors.append("later-round post-delta gate receipt contract drifted")
    adjudication = critique.get("adjudication", {})
    if adjudication.get("deterministic_trigger_packet") != "adjudication_packet.json":
        errors.append("targeted adjudication packet is not canonical")
    if adjudication.get("mode") != "terra_targeted_on_policy_triggers":
        errors.append("targeted adjudication is not assigned to the adjudicator model")
    if adjudication.get("model_policy_ref") != "models.targeted_adjudicator":
        errors.append("targeted adjudication does not reference its max-effort model policy")
    if adjudication.get("separate_model_default") != "triggered_only":
        errors.append("targeted adjudication is not conditional on policy triggers")
    if adjudication.get("evidence_complete_input_required") is not True:
        errors.append("targeted adjudication can receive evidence-incomplete triggers")
    if adjudication.get("incomplete_trigger_action") != "defer_evidence_gap_to_next_critique_round":
        errors.append("evidence gaps are not deferred to a critique round")
    if adjudication.get("required_input_bindings") != [
            "canonical_finding", "critic_records", "source_evidence",
            "decision_complete_question"]:
        errors.append("targeted adjudication input bindings drifted")
    expected_triggers = [
        "conflicting_verdicts", "disputed_conclusion_changing_claim",
        "high_impact_finding_lacking_two_independent_evidence_records",
        "failed_settled_fact_spot_check",
    ]
    if adjudication.get("automatic_triggers") != expected_triggers:
        errors.append("automatic targeted-adjudication triggers drifted")
    expected_stop_keys = {
        "review_scope_event",
        "required_run_state_fields",
        "target_complete",
        "continue_after_target",
        "converged",
        "cap_with_unresolved",
        "aborted",
    }
    if set(critique["stopping"]) != expected_stop_keys:
        errors.append("stopping policy does not use the v2 target/convergence/cap enum")
    expected_scope = [
        "changed_claim_ids",
        "rereviewed_claim_ids",
        "unresolved_blocking_item_ids",
        "unresolved_conclusion_critical_item_ids",
        "unreviewed_sections",
    ]
    if critique["stopping"].get("review_scope_event") != "review_scope":
        errors.append("stopping evidence is not recorded through the review_scope event")
    if critique["stopping"].get("required_run_state_fields") != expected_scope:
        errors.append("stopping evidence fields drifted")
    expected_counts = {
        "coordinator": 1,
        "phase_1_subagents": 5,
        "round_1_critics": 4,
        "round_1_model_synthesizers": 0,
        "round_2_critics": 2,
        "default_external_codex_substantive_sessions": 6,
        "default_targeted_adjudicator_sessions": 0,
        "default_substantive_non_coordinator_invocations": 11,
        "default_substantive_sessions_including_coordinator": 12,
        "phase_1_capability_probe_sessions": 1,
        "critique_capability_probe_sessions": 1,
        "cold_start_provider_invocations_including_probes": 14,
        "cold_start_critique_provider_invocations": 7,
        "warm_probe_critique_provider_invocations": 6,
        "normal_worst_case_critique_provider_invocations": 19,
        "one_triggered_adjudicator_round_worst_case_provider_invocations": 22,
    }
    if policy.get("default_role_counts") != expected_counts:
        errors.append("default actor/provider invocation counts drifted")
    metadata = policy.get("metadata", {})
    if (metadata.get("schema_version") != "2.1" or
            metadata.get("canonical_journal") != "run.events.jsonl" or
            metadata.get("journal_contract") != "append_only_hash_chained_and_locked" or
            metadata.get("projections") != [
                "run.json", "invocations.jsonl", "run_metrics.json", "RUN_METADATA.md"] or
            metadata.get("projection_watermark_fields") != ["journal_seq", "journal_hash"] or
            metadata.get("invocation_lifecycle_events") != [
                "invocation_reserved", "provider_request", "inference_start",
                "provider_response", "invocation_terminal", "cache_reuse"] or
            metadata.get("authorization_binding_fields") != [
                "purpose", "round_plan_sha256", "provider", "model_route", "data_classes",
                "policy_sha256", "expires_at"] or
            metadata.get("round_states") != [
                "prepared", "awaiting_authorization", "ready", "running",
                "quorum_complete", "integrated"]):
        errors.append("metadata 2.1 journal or lifecycle contract drifted")
    document = policy.get("document", {})
    if (document.get("body_max_rendered_pages") != 10 or
            document.get("draft_body_page_target") != 9 or
            document.get("manifest_version") != 3 or
            document.get("canonical_environment_override") != "rejected"):
        errors.append("canonical ten-page document transaction policy drifted")


def check_round_cap_consumers(live_caps: list[int], errors: list[str]) -> None:
    """validate_v2, run.schema.json and the amendment verifier accept exactly the live caps."""
    try:
        text = (ROOT / "schemas" / "tools" / "validate_v2.py").read_text(encoding="utf-8")
        match = re.search(r"^LIVE_ROUND_CAPS = \(([0-9, ]+)\)", text, flags=re.MULTILINE)
        coded = sorted(int(part) for part in match.group(1).split(",") if part.strip()) if match else None
        if coded != live_caps:
            errors.append(f"schemas/tools/validate_v2.py LIVE_ROUND_CAPS is {coded}, expected {live_caps}")
        schema = json.loads((ROOT / "schemas" / "run.schema.json").read_text(encoding="utf-8"))
        enums = [branch["then"]["properties"]["round_cap"].get("enum") for branch in schema.get("allOf", [])
                 if isinstance(branch, dict) and "round_cap" in (branch.get("then") or {}).get("properties", {})]
        if not enums or any(sorted(values or []) != live_caps for values in enums):
            errors.append(f"schemas/run.schema.json live round_cap enums are {enums}, expected {live_caps}")
        verifier = (ROOT / "scripts" / "round_cap_amendment.py").read_text(encoding="utf-8")
        match = re.search(r"^LIVE_ROUND_CAPS = \(([0-9, ]+)\)", verifier, flags=re.MULTILINE)
        coded = sorted(int(part) for part in match.group(1).split(",") if part.strip()) if match else None
        if coded != live_caps:
            errors.append(f"scripts/round_cap_amendment.py LIVE_ROUND_CAPS is {coded}, expected {live_caps}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(f"round-cap consumers could not be checked: {exc}")


def check_finding_row_schema(policy: dict, errors: list[str]) -> None:
    relative = policy["critique"]["settled_facts"]["runtime_row_schema"]
    path = ROOT / relative
    if not path.is_file():
        errors.append(f"canonical runtime row schema is missing: {relative}")
        return
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"canonical runtime row schema is invalid JSON: {exc}")
        return

    expected_required = {
        "id",
        "claim",
        "domain",
        "source_location",
        "status",
        "ledger_disposition",
        "coordinator_approved",
        "impact",
        "impact_rank",
        "verification_tier",
        "paper_only",
        "confidence",
        "evidence",
        "unverifiable_basis",
        "verifications",
        "dependencies",
        "settled",
        "disclosure_flag",
        "remedy",
        "changes_conclusion",
        "changes_conclusion_which",
        "provenance",
    }
    if set(schema.get("required", [])) != expected_required:
        errors.append("finding-row.schema.json required fields drifted from the runtime contract")

    properties = schema.get("properties", {})
    nested_required = {
        "ledger_disposition": {"status", "target", "reason"},
        "source_location": {"path", "locator"},
        "evidence": {"source_hashes", "rerun_command", "expected", "observed", "tolerance_basis"},
        "dependencies": {"environment_fingerprint", "finding_ids", "artifact_paths"},
        "settled": {"approved", "valid_for_report_hash"},
        "remedy": {"class", "description", "verified"},
        "verification_snippet": {"kind", "path", "basis"},
        "provenance": {
            "origin_role",
            "independently_found_by",
            "agreed_after_seeing",
            "round_introduced",
            "source_records",
        },
    }
    for name, expected in nested_required.items():
        if set(properties.get(name, {}).get("required", [])) != expected:
            errors.append(f"finding-row.schema.json {name} fields drifted")
    verification_required = (
        properties.get("verifications", {}).get("items", {}).get("required", [])
    )
    if set(verification_required) != {
        "reviewer_id", "invocation_id", "evidence_record", "model_family", "outcome"
    }:
        errors.append("finding-row.schema.json verification identity fields drifted")
    evidence_record_required = set(
        properties.get("verifications", {}).get("items", {}).get("properties", {})
        .get("evidence_record", {}).get("required", [])
    )
    if evidence_record_required != {"path", "sha256"}:
        errors.append("finding-row.schema.json verification evidence binding fields drifted")
    try:
        minimum = schema["allOf"][1]["then"]["properties"]["verifications"]["minItems"]
    except (KeyError, IndexError, TypeError):
        minimum = None
    if minimum != policy["critique"]["settled_facts"]["minimum_independent_confirmations"]:
        errors.append("finding-row.schema.json confirmation minimum does not match policy")
    try:
        promotable_required = set(schema["allOf"][1]["then"]["required"])
    except (KeyError, IndexError, TypeError):
        promotable_required = set()
    if promotable_required != {
        "benign_explanations_ruled_out",
        "report_section",
        "verification_snippet",
    }:
        errors.append("finding-row.schema.json promotable evidence fields drifted")


def check_finding_projection(policy: dict, errors: list[str]) -> None:
    expected = {
        "runtime_registry": "work/findings.jsonl",
        "row_schema": "schemas/v2/finding-row.schema.json",
        "projection_command": "python3 scripts/generate_finding_artifacts.py <audit-dir> --apply",
        "ledger_scope": "all_registry_rows",
        "final_projection_scope": "final_status_coordinator_approved_and_included_rows",
        "generated_outputs": [
            "report/LEDGER.md",
            "report/APPENDIX_A.md",
            "claims_map.tsv",
            "work/verification_plan.json",
            "work/comment_findings.generated.json",
            "verification_snippets/",
        ],
    }
    if policy.get("finding_source_of_truth") != expected:
        errors.append("finding source-of-truth projection policy drifted")
    if not (ROOT / "scripts" / "generate_finding_artifacts.py").is_file():
        errors.append("canonical finding projection helper is missing")


def check_integration_helpers(errors: list[str]) -> None:
    expected = {
        "scripts/record_invocation.py": "canonical invocation recorder",
        "scripts/make_diff_packet.py": "content-bound delta builder",
        "scripts/verify_diff_packet.py": "deterministic delta verifier",
        "scripts/make_settled.sh": "content-bound settled-cache builder",
        "scripts/benchmark_optimization.py": "offline optimization benchmark",
        "scripts/run_post_delta_gates.py": "content-bound post-delta gate runner",
        "schemas/v2/post-delta-gates.schema.json": "post-delta receipt schema",
        "scripts/document_build_cache.py": "content-addressed document cache",
        "scripts/test_document_build_cache.py": "document cache fail-closed tests",
        "config/benchmark-seeded-fixtures.json": "seeded benchmark cases",
    }
    for relative, label in expected.items():
        if not (ROOT / relative).is_file():
            errors.append(f"{label} is missing: {relative}")


def _policy_node(policy: dict, dotted: str):
    node = policy
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def check_verifier_contract(policy: dict, errors: list[str]) -> None:
    """The verifier policy, the shared contract module and the adjudication guide say one thing.

    check_policy_shape pins the policy's literal values. This joins them to the code every
    verifier consumer calls (scripts/verifier_contract.py) and to the guide's verdict table, so a
    change to any one of the three without the others fails here. It is not part of the leaf sweep:
    a missing module would otherwise make every mutated leaf look pinned.
    """
    section = policy.get("post_fan_out_verification")
    if not isinstance(section, dict):
        errors.append("policy has no post_fan_out_verification section")
        return
    for key in ("runner", "contract_module"):
        relative = section.get(key)
        if not isinstance(relative, str) or not (ROOT / relative).is_file():
            errors.append(f"post-fan-out verification {key} is missing: {relative}")
    for key in ("model_policy_ref", "runtime_contract_ref", "admission_contract_ref", "repair_contract_ref"):
        if _policy_node(policy, str(section.get(key))) is None:
            errors.append(f"post-fan-out verification {key} names no policy entry: {section.get(key)!r}")
    try:
        contract = load_verifier_contract()
        joined = {
            "role_class": (section.get("role_class"), contract.ROLE_CLASS),
            "role_pattern": (section.get("role_pattern"), contract.ROLE_PATTERN.pattern),
            "round": (section.get("round"), contract.ROUND),
            "verdicts": (section.get("verdicts"), list(contract.VERDICTS)),
            "route keys": (list(ROUTE_KEYS), list(contract.ROUTE_KEYS)),
        }
        outcomes, verifier_route = contract.VERDICT_OUTCOME, contract.verifier_route
    except Exception as exc:  # a broken or incomplete module is a finding, not a checker crash
        errors.append(f"shared verifier contract cannot be loaded: {type(exc).__name__}: {exc}")
        return
    for label, (declared, coded) in joined.items():
        if declared != coded:
            errors.append(f"post-fan-out verifier {label} is {declared!r} in the policy but "
                          f"{coded!r} in scripts/verifier_contract.py")
    auditors = [entry for entry in policy["models"]["phase_1_auditors"].values() if isinstance(entry, dict)]
    if not auditors:
        errors.append("policy has no Phase-1 auditor route for the verifier to share")
        return
    auditor_route = {key: auditors[0].get(key) for key in ROUTE_KEYS}
    if verifier_route(policy) != auditor_route:
        errors.append("verifier_contract.verifier_route does not resolve to the Phase-1 auditor route")
    # An audit initialised before 2.6.4 has no models.post_fan_out_verifier in its frozen snapshot;
    # the contract then falls back to the auditors' shared route, which must be the same route.
    legacy = deepcopy(policy)
    legacy["models"].pop("post_fan_out_verifier", None)
    legacy.pop("post_fan_out_verification", None)
    if verifier_route(legacy) != auditor_route:
        errors.append("a pre-2.6.4 policy snapshot would not fall back to the Phase-1 auditor route")

    guide = ROOT / "reference" / "adjudication_guide.md"
    match = VERIFIER_TABLE.search(guide.read_text(encoding="utf-8")) if guide.is_file() else None
    if not match:
        errors.append("adjudication_guide.md has no marked verifier verdict table (VERIFIER-VERDICT-OUTCOME)")
        return
    rows = re.findall(r"^\|\s*([A-Z]+)\s*\|\s*`([a-z]+)`\s*\|", match.group(1), flags=re.MULTILINE)
    documented = dict(rows)
    if len(rows) != len(documented) or documented != outcomes:
        errors.append(f"adjudication_guide.md verifier verdict table {documented} differs from "
                      f"verifier_contract.VERDICT_OUTCOME {outcomes}")


def check_claude_routes(errors: list[str]) -> None:
    """The shipped Claude route, its pin above and scripts/claude_routes.py say one thing (2.7.0).

    check_policy_shape pins every Claude role of the policy to SHIPPED_CLAUDE_ROUTE. This joins that
    literal to the module every Claude runner takes its allowed routes from, so a revert of the module's
    SHIPPED fails even when the policy is reverted with it, and so does dropping the Sonnet 5 route that
    every audit initialised before 2.7.0 froze, or (2.7.3) the Sonnet 5.5 / max route every audit initialised
    on 2.7.0 to 2.7.2 froze (those audits would stop dispatching). Like
    check_verifier_contract, it is not part of the leaf sweep.
    """
    try:
        routes = load_claude_routes()
        shipped = dict(routes.SHIPPED)
        live = [dict(route) for route in routes.LIVE_ROUTES]
        keys = tuple(routes.ROUTE_KEYS)
    except Exception as exc:  # a broken or incomplete module is a finding, not a checker crash
        errors.append(f"shared Claude route module cannot be loaded: {type(exc).__name__}: {exc}")
        return
    if keys != ROUTE_KEYS:
        errors.append(f"scripts/claude_routes.py ROUTE_KEYS is {keys!r}, expected {ROUTE_KEYS!r}")
    if shipped != SHIPPED_CLAUDE_ROUTE:
        errors.append(f"scripts/claude_routes.py SHIPPED is {shipped}, expected {SHIPPED_CLAUDE_ROUTE}")
    if not live or live[-1] != shipped:
        errors.append("scripts/claude_routes.py SHIPPED is not the newest LIVE_ROUTES entry")
    if LEGACY_CLAUDE_ROUTE not in live:
        errors.append("scripts/claude_routes.py LIVE_ROUTES no longer holds the Sonnet 5 route that every "
                      "audit initialised before 2.7.0 froze")
    # 2.7.3: nor the Sonnet 5.5 / max route every audit initialised on 2.7.0 to 2.7.2 froze
    if SONNET_5_5_MAX_ROUTE not in live:
        errors.append("scripts/claude_routes.py LIVE_ROUTES no longer holds the Sonnet 5.5 / max route that every "
                      "audit initialised on 2.7.0 to 2.7.2 froze")


def check_probe_bounds(errors: list[str]) -> None:
    """The shipped probe bound, its pins above and scripts/probe_bounds.py say one thing (2.7.1).

    check_policy_shape pins both policy leaves to SHIPPED_PROBE_BOUND. This joins that literal to the module
    run_phase1.py takes its allowed bounds from, so a new audit's frozen bound is one the runtime admits (else its
    Phase 1 is refused before any call), a revert of the module's SHIPPED_PROBE_BOUND fails even when the policy is
    reverted with it, and so does dropping the 150,000 bound every audit initialised before 2.7.1 froze (those
    audits would stop at their next Phase-1 or verifier call). Like check_claude_routes, not part of the leaf sweep.
    """
    try:
        bounds = load_probe_bounds()
        shipped = bounds.SHIPPED_PROBE_BOUND
        live = list(bounds.LIVE_PROBE_BOUNDS)
        legacy = bounds.LEGACY_PROBE_BOUND
    except Exception as exc:  # a broken or incomplete module is a finding, not a checker crash
        errors.append(f"shared probe-bound module cannot be loaded: {type(exc).__name__}: {exc}")
        return
    if shipped != SHIPPED_PROBE_BOUND:
        errors.append(f"scripts/probe_bounds.py SHIPPED_PROBE_BOUND is {shipped!r}, expected {SHIPPED_PROBE_BOUND}")
    if not live or live[-1] != shipped:
        errors.append("scripts/probe_bounds.py SHIPPED_PROBE_BOUND is not the newest LIVE_PROBE_BOUNDS entry")
    if legacy != LEGACY_PROBE_BOUND or LEGACY_PROBE_BOUND not in live:
        errors.append("scripts/probe_bounds.py LIVE_PROBE_BOUNDS no longer holds the 150000 bound that every audit "
                      "initialised before 2.7.1 froze")


def check_phase1_timeouts(errors: list[str]) -> None:
    """The shipped Phase-1 timeout, its pins above and scripts/phase1_timeouts.py say one thing (2.7.2).

    check_policy_shape pins both policy leaves to SHIPPED_PHASE1_TIMEOUT. This joins that literal to the module
    run_phase1.py and run_verifier.py take their allowed timeouts from, so a new audit's frozen timeout is one the
    runtime admits (else its Phase 1 is refused before any call), a revert of the module's SHIPPED_PHASE1_TIMEOUT
    fails even when the policy is reverted with it, and so does dropping the 2,700 s timeout every audit
    initialised before 2.7.2 froze. Like check_probe_bounds, not part of the leaf sweep.
    """
    try:
        timeouts = load_phase1_timeouts()
        shipped = timeouts.SHIPPED_PHASE1_TIMEOUT
        live = list(timeouts.LIVE_PHASE1_TIMEOUTS)
        legacy = timeouts.LEGACY_PHASE1_TIMEOUT
    except Exception as exc:  # a broken or incomplete module is a finding, not a checker crash
        errors.append(f"shared Phase-1 timeout module cannot be loaded: {type(exc).__name__}: {exc}")
        return
    if shipped != SHIPPED_PHASE1_TIMEOUT:
        errors.append(f"scripts/phase1_timeouts.py SHIPPED_PHASE1_TIMEOUT is {shipped!r}, expected "
                      f"{SHIPPED_PHASE1_TIMEOUT}")
    if not live or live[-1] != shipped:
        errors.append("scripts/phase1_timeouts.py SHIPPED_PHASE1_TIMEOUT is not the newest LIVE_PHASE1_TIMEOUTS entry")
    if legacy != LEGACY_PHASE1_TIMEOUT or LEGACY_PHASE1_TIMEOUT not in live:
        errors.append("scripts/phase1_timeouts.py LIVE_PHASE1_TIMEOUTS no longer holds the 2700 s timeout that every "
                      "audit initialised before 2.7.2 froze")


def load_critique_timeouts():
    """scripts/critique_timeouts.py from THIS tree (never another copy on sys.path), loaded once."""
    global _CRITIQUE_TIMEOUTS
    if _CRITIQUE_TIMEOUTS is None:
        spec = importlib.util.spec_from_file_location("i4r_drift_critique_timeouts", CRITIQUE_TIMEOUTS_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {CRITIQUE_TIMEOUTS_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CRITIQUE_TIMEOUTS = module
    return _CRITIQUE_TIMEOUTS


def check_critique_timeouts(errors: list[str]) -> None:
    """The shipped critique ceiling, its pin and scripts/critique_timeouts.py say one thing (2.7.4).

    check_policy_shape pins critique.provider_timeout_seconds to SHIPPED_CRITIQUE_TIMEOUT. This joins that literal
    to the module run_critique.sh resolves the ceiling with, so a new audit's frozen ceiling is one the driver
    admits, a revert of the module fails even when the policy is reverted with it, and so does dropping the
    2,700 s legacy ceiling every audit initialised before 2.7.4 runs at. Not part of the leaf sweep.
    """
    try:
        timeouts = load_critique_timeouts()
        shipped = timeouts.SHIPPED_CRITIQUE_TIMEOUT
        live = list(timeouts.LIVE_CRITIQUE_TIMEOUTS)
        legacy = timeouts.LEGACY_CRITIQUE_TIMEOUT
    except Exception as exc:  # a broken or incomplete module is a finding, not a checker crash
        errors.append(f"shared critique timeout module cannot be loaded: {type(exc).__name__}: {exc}")
        return
    if shipped != SHIPPED_CRITIQUE_TIMEOUT:
        errors.append(f"scripts/critique_timeouts.py SHIPPED_CRITIQUE_TIMEOUT is {shipped!r}, expected "
                      f"{SHIPPED_CRITIQUE_TIMEOUT}")
    if not live or live[-1] != shipped:
        errors.append("scripts/critique_timeouts.py SHIPPED_CRITIQUE_TIMEOUT is not the newest "
                      "LIVE_CRITIQUE_TIMEOUTS entry")
    if legacy != LEGACY_CRITIQUE_TIMEOUT or LEGACY_CRITIQUE_TIMEOUT not in live:
        errors.append("scripts/critique_timeouts.py LIVE_CRITIQUE_TIMEOUTS no longer holds the 2700 s ceiling that "
                      "every audit initialised before 2.7.4 runs at")


def load_claim_change_rules():
    """scripts/claim_change_rules.py from THIS tree (never another copy on sys.path), loaded once."""
    global _CLAIM_CHANGE_RULES
    if _CLAIM_CHANGE_RULES is None:
        spec = importlib.util.spec_from_file_location("i4r_drift_claim_change_rules", CLAIM_CHANGE_RULES_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {CLAIM_CHANGE_RULES_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CLAIM_CHANGE_RULES = module
    return _CLAIM_CHANGE_RULES


def check_claim_change_rules(errors: list[str]) -> None:
    """The shipped claim-change rule, its pin and scripts/claim_change_rules.py say one thing (2.7.4): the module's
    SHIPPED_RULE is the literal, and its LIVE_RULES still hold the legacy rule every older audit keeps."""
    try:
        rules = load_claim_change_rules()
        shipped, live, legacy = rules.SHIPPED_RULE, list(rules.LIVE_RULES), rules.LEGACY_RULE
    except Exception as exc:  # a broken or incomplete module is a finding, not a checker crash
        errors.append(f"shared claim-change rule module cannot be loaded: {type(exc).__name__}: {exc}")
        return
    if shipped != SHIPPED_CLAIM_CHANGE_RULE or shipped not in live:
        errors.append(f"scripts/claim_change_rules.py SHIPPED_RULE is {shipped!r}, expected {SHIPPED_CLAIM_CHANGE_RULE!r}")
    if legacy != "all_fields" or legacy not in live:
        errors.append("scripts/claim_change_rules.py LIVE_RULES no longer holds the legacy rule every audit initialised "
                      "before 2.7.4 keeps")


def check_local_execution(policy: dict, errors: list[str]) -> None:
    expected = {
        "intake_manifest": "package_manifest.json",
        "intake_manifest_reused_by": [
            "critique", "verification", "post_delta_archive_gate", "staging", "finalization",
        ],
        "event_projection": {
            "runner": "scripts/log_event.sh --batch <events.jsonl>",
            "transaction": "one_lock_one_projection_one_run_json_rename",
            "single_event_compatible": True,
        },
        "environment_probes": {
            "runner": "scripts/capture_environment.sh", "parallel": True,
        },
        "document_build_cache": {
            "runner": "scripts/build_docx.sh",
            "helper": "scripts/document_build_cache.py",
            "default": "validated_content_addressed_cache",
            "bypass_flag": "--no-cache", "refresh_flag": "--refresh-cache",
        },
        "publication": "atomic_manifest_last",
    }
    if policy.get("local_execution") != expected:
        errors.append("local batch/cache/manifest/atomic execution policy drifted")


def check_verification_and_release(policy: dict, errors: list[str]) -> None:
    verification = policy.get("verification", {})
    expected_verification = {
        "required_runner": "scripts/run_verification.sh",
        "content_bound_manifest": "verification_manifest.json",
        "allow_no_verification": False,
        "release_stage_required_artifacts": [
            "verification.Rmd",
            "verification_status.txt",
            "verification_summary.csv",
            "verification_manifest.json",
        ],
    }
    if verification != expected_verification:
        errors.append("verification policy drifted from the accuracy-safe content-bound contract")
    publication = policy.get("publication", {})
    expected_publication = {
        "primary_artifact_count": 6,
        "primary_artifacts": [
            "report/*.pdf", "claims_script.*", "run.json", "comment.json",
            "finalization_receipt.json", "document_build_manifest.json",
        ],
        "manifest": "release-manifest.json",
        "total_file_count_without_docx": 7,
        "total_file_count_with_docx": 8,
    }
    if publication != expected_publication:
        errors.append("publication policy is not six bound artefacts plus release manifest and optional DOCX")


def check_docs(policy: dict, errors: list[str]) -> None:
    maintained = [
        ROOT / "SKILL.md",
        ROOT / "INSTALL.md",
        ROOT / "reference" / "adjudication_guide.md",
        ROOT / "reference" / "checklist.md",
        ROOT / "reference" / "report_structure.md",
        ROOT / "reference" / "brief_template.md",
    ]
    banned = {
        # 2.6.9: the cap is the audit's own (7 from 2.6.9; 5 for an older audit until amended), never a literal 5
        "hard cap 5": "a round cap of 5 stated as the rule (the audit's cap is critique.round_cap in its snapshot)",
        "cap 5": "a round cap of 5 stated as the rule (the audit's cap is critique.round_cap in its snapshot)",
        "at round 5": "a round cap of 5 stated as the rule (say: at the audit's round cap)",
        "round&nbsp;6": "the driver refusing round 6 stated as the rule (it refuses a round above the audit's cap)",
        "five is the hard cap": "a round cap of 5 stated as the rule",
        "metadata/scripts/": "obsolete metadata/scripts path",
        "eleven-item": "checklist count is twelve",
        "Critics run **sequentially**": "obsolete sequential-critic instruction",
        "executive summary and the two summary tables": "obsolete page-1 format",
        "generated_after_round=2": "obsolete settled-facts timing",
        "--stopping-reason cap": "obsolete stopping enum",
        "SYNTH=": "legacy synthesis control",
    }
    for path in maintained + [ROOT / "CHEATSHEET.html"]:
        if not path.is_file():
            errors.append(f"missing maintained document: {path.relative_to(ROOT)}")
            continue
        text = path.read_text(encoding="utf-8")
        if path in maintained and "config/pipeline-policy-v2.json" not in text:
            errors.append(f"{path.relative_to(ROOT)} does not identify the canonical policy")
        for needle, reason in banned.items():
            # Bounded, not a bare substring: "--stopping-reason cap" used to match the CORRECT
            # current value "--stopping-reason cap_with_unresolved", so documenting the cap path
            # failed the mandatory pre-audit check and grep found only the right line.
            # whitespace-collapsed as well: "at\nround 5" across a wrapped line is the same sentence
            if re.search(re.escape(needle) + r"(?![_A-Za-z0-9])", text) or \
                    re.search(re.escape(needle) + r"(?![_A-Za-z0-9])", " ".join(text.split())):
                errors.append(f"{path.relative_to(ROOT)} contains {reason}: {needle!r}")

    contract_docs = [
        ROOT / "SKILL.md",
        ROOT / "INSTALL.md",
        ROOT / "reference" / "adjudication_guide.md",
    ]
    contract_markers = [
        "work/findings.jsonl",
        "schemas/v2/finding-row.schema.json",
        "SETTLED.json",
        "SETTLED.md",
    ]
    for path in contract_docs:
        text = path.read_text(encoding="utf-8")
        for marker in contract_markers:
            if marker not in text:
                errors.append(f"{path.relative_to(ROOT)} is missing settled contract marker {marker!r}")

    workflow_markers = {
        "bash scripts/run_verification.sh": "content-bound verification runner",
        "verification_manifest.json": "verification manifest",
        "target_complete": "target stopping value",
        "cap_with_unresolved": "cap stopping value",
        "release-manifest.json": "published release manifest",
        "ADJUDICATE=auto": "conditional targeted-adjudication mode",
        "DIFF_MANIFEST": "later-round routing manifest",
        "scripts/generate_finding_artifacts.py": "finding projection helper",
        "scripts/record_invocation.py": "canonical invocation recorder",
        "scripts/make_diff_packet.py": "content-bound delta builder",
        "review_scope": "atomic stopping-evidence event",
        "run_post_delta_gates.py": "post-delta whole-document gate runner",
        "POST_DELTA_GATES.json": "post-delta gate receipt",
        "run_verifier.py": "post-fan-out verifier runner",
    }
    for path in [ROOT / "SKILL.md", ROOT / "INSTALL.md"]:
        text = path.read_text(encoding="utf-8")
        for marker, label in workflow_markers.items():
            if marker not in text:
                errors.append(f"{path.relative_to(ROOT)} is missing {label}: {marker!r}")

    # 2.7.0: SKILL.md printed `--auditor-model sonnet-5`, which init_run.sh refuses once the shipped policy
    # freezes another model. Only the operator documents are checked: the reference files are hashed into
    # audit identities, name no model, and must not be edited for a model change.
    shipped_model = policy["models"]["coordinator"].get("requested_model")
    for path in [ROOT / "SKILL.md", ROOT / "INSTALL.md", ROOT / "CHEATSHEET.html"]:
        if not path.is_file():
            continue
        for named in printed_auditor_models(path.read_text(encoding="utf-8")):
            if named != shipped_model:
                errors.append(f"{path.relative_to(ROOT)} prints init_run.sh --auditor-model {named}, but the "
                              f"shipped policy freezes {shipped_model}")

    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    words = len(re.findall(r"\b[\w'-]+\b", skill))
    # Raised from 4000 to 4500 on 2026-09-09. The cap is there to keep SKILL.md a procedure
    # rather than a manual, and it works -- but at 4000 the file sat six words under, and adding
    # the one line that stops `approve_settled.py` being skipped meant deleting a safety rule to
    # pay for it. Detail still belongs in INSTALL.md, which has no cap. Raised again to 4750 on 2026-10-07
    # (2.7.5, at the operators' request): the file sat at exactly 4500, and the critic-access step had no room.
    SKILL_WORD_CAP = 4750
    if words > SKILL_WORD_CAP:
        errors.append(f"SKILL.md has {words} words; maximum is {SKILL_WORD_CAP}")
    match = re.search(
        r"<!-- PIPELINE-POLICY-V2:START -->.*?<!-- PIPELINE-POLICY-V2:END -->",
        skill,
        flags=re.DOTALL,
    )
    if not match:
        errors.append("SKILL.md has no generated v2 policy summary block")
    elif match.group(0) != policy_summary(policy):
        errors.append("SKILL.md policy summary does not match pipeline-policy-v2.json")

    checklist = (ROOT / "reference" / "checklist.md").read_text(encoding="utf-8")
    numbers = [int(n) for n in re.findall(r"^## (\d+)\.", checklist, flags=re.MULTILINE)]
    if numbers != list(range(1, 13)):
        errors.append(f"checklist numbered sections are {numbers}, expected 1..12")

    report = (ROOT / "reference" / "report_structure.md").read_text(encoding="utf-8")
    required_report_phrases = [
        "There is no executive summary",
        "≤250 words",
        "Appendix A",
        "10 rendered pages",
    ]
    for phrase in required_report_phrases:
        if phrase not in report:
            errors.append(f"report_structure.md is missing format invariant {phrase!r}")



# Leaves the value checks above deliberately do NOT pin. Every one of them is either descriptive
# prose the runtime never reads, or a knob whose consumer reads it live from the snapshot rather
# than comparing it against an expected constant. The list exists so an omission is EXPLICIT: a
# mutation sweep below changes every leaf of the policy in turn and requires the value checks to
# object unless the leaf is named here. 72 of 240 leaves were silently free before this sweep,
# including siblings that restate a pinned rule (phase_1.provider_timeout_seconds beside
# phase_1.runtime.timeout_seconds, pinned itself since 2.7.2), so the policy could contradict itself and pass.
FREE_LEAVES = frozenset([
    "policy_id",
    "effective_date",
    "policy_source",
    "phase_1.runner",
    "phase_1.required_roles",
    "phase_1.parallelism",
    "phase_1.isolated_work_directories",
    "phase_1.package_is_read_only",
    "phase_1.provider_budget_usd_per_role",
    "phase_1.network_access",
    "phase_1.plugins_enabled",
    "phase_1.cross_attempt_state",
    "phase_1.capability_probe.maximum_successful_provider_calls",
    "phase_1.capability_probe.required_contracts",
    "phase_1.capability_probe.cache_key_fields",
    "phase_1.deterministic_evidence.generated_once",
    "phase_1.deterministic_evidence.contains_participant_rows",
    "phase_1.deterministic_evidence.indexes",
    "phase_1.deterministic_evidence.raw_read_only_access_retained",
    "phase_1.deterministic_evidence.dual_review_for",
    "phase_1.admission",
    "critique.attempt_budget_scope",
    "critique.completion_contract",
    "critique.attempt_kinds",
    "critique.schema_admission_maximum_per_role_and_digest",
    "critique.retryable_error_classes",
    "critique.non_retryable_error_classes",
    "critique.focused_output_repair.maximum_calls_per_role",
    "critique.focused_output_repair.maximum_input_fraction",
    "critique.focused_output_repair.scientific_fields_must_be_byte_equivalent",
    "critique.round_1.separate_adjudicator_model_invocations_default",
    "critique.later_rounds.always_run",
    "critique.later_rounds.default_total_critics",
    "critique.later_rounds.specialist_router.critic_fairness",
    "critique.later_rounds.specialist_router.critic_stats",
    "critique.later_rounds.specialist_router.critic_data",
    "critique.later_rounds.fallback_specialist",
    "critique.later_rounds.scope",
    "critique.later_rounds.synthesis",
    "critique.later_rounds.routing_manifest.signals_in_precedence_order",
    "critique.later_rounds.routing_manifest.explicit_missing_path_is_fatal",
    "critique.later_rounds.separate_adjudicator_model_invocations_default",
    "critique.settled_facts.require_spot_check_or_hash",
    "critique.settled_facts.inject_from_round",
    "critique.adjudication.items_requiring_recomputation",
    "critique.adjudication.second_structurally_different_route_required_for",
    "critique.adjudication.confirmed_settled_items",
    "critique.stopping.target_complete",
    "critique.stopping.continue_after_target",
    "critique.stopping.converged",
    "critique.stopping.cap_with_unresolved",
    "critique.stopping.aborted",
    "metadata.honest_unknown_usage",
    "metadata.requested_and_served_model_separate",
    "metadata.elapsed_components",
    "metadata.normalizers",
    "document.abstract_max_words",
    "document.executive_summary",
    "document.findings_table_location",
    "document.recommendations_section",
    "document.canonical_cap_source",
    "document.preview_flag",
    "document.preview_directory",
    "document.preview_is_releasable",
    "document.build_transaction",
    "document.manifest_schema",
    "document.release_recounts_exact_pdf_at_each_boundary",
    "document.failed_build_preserves_prior_passing_pair"
])


# 2.6.8: deposit personal data is reported in the comment, never a reason to pause or withhold.
# These phrases carried the old pause and withholding instructions; none may come back into the
# operator, auditor, coordinator or critic text. Matched case-insensitively.
DISCLOSURE_PAUSE_PHRASES = {
    "responsible-disclosure decision": "a disclosure pause (deposit PII never pauses an audit)",
    "route an immediate disclosure": "an out-of-band disclosure step (the filed comment is the route)",
    "route an immediate-disclosure": "an out-of-band disclosure step (the filed comment is the route)",
    "before the comment is drafted": "a disclosure pause before drafting",
    "escalated to the coordinator before drafting": "a disclosure pause before drafting",
    "obliges responsible disclosure before": "a disclosure pause before circulation",
    "disclosure email": "withholding the file and column from the record",
    "disclosure e-mail": "withholding the file and column from the record",
    "which is a map to it": "withholding a value-free location as if it were a leak",
}


def disclosure_language_files(root: Path = ROOT) -> list[Path]:
    files = [root / "SKILL.md", root / "INSTALL.md", root / "CHEATSHEET.html",
             root / "schemas" / "comment.schema.json", root / "schemas" / "EXAMPLE_comment.json"]
    files += sorted((root / "reference").glob("*.md")) + sorted((root / "prompts").glob("*.txt"))
    return files


def check_disclosure_language(errors: list[str], root: Path = ROOT) -> None:
    # Negative control first: a detector that cannot see the old SKILL.md sentence proves nothing.
    planted = "Pause only for a missing input, a responsible-disclosure decision, or a gate"
    if not any(needle in planted.lower() for needle in DISCLOSURE_PAUSE_PHRASES):
        errors.append("the disclosure-pause phrase check does not fire on the 2.6.7 SKILL.md sentence")
        return
    for path in disclosure_language_files(root):
        if not path.is_file():
            continue
        text = " ".join(path.read_text(encoding="utf-8").lower().split())
        for needle, reason in DISCLOSURE_PAUSE_PHRASES.items():
            if needle in text:
                errors.append(f"{path.relative_to(root)} reintroduces {reason}: {needle!r}")


def _policy_leaves(node, path=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _policy_leaves(value, path + (str(key),))
    else:
        yield path, node


def _mutated(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1 if value != 1 else 7
    if isinstance(value, str):
        return value + "_drift"
    if isinstance(value, list):
        return value + ["drift"]
    return "drift"


def check_policy_leaf_coverage(policy: dict, errors: list[str]) -> None:
    """Every leaf is either pinned by a value check or named in FREE_LEAVES."""
    import copy
    for path, value in _policy_leaves(policy):
        dotted = ".".join(path)
        trial = copy.deepcopy(policy)
        node = trial
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = _mutated(value)
        found: list[str] = []
        try:
            check_policy_shape(trial, found)
            check_local_execution(trial, found)
            check_verification_and_release(trial, found)
            check_finding_row_schema(trial, found)
            check_finding_projection(trial, found)
        except Exception as exc:  # a mutation that breaks a check's own assumptions counts as caught
            found.append(str(exc))
        pinned = bool(found)
        if pinned and dotted in FREE_LEAVES:
            errors.append(f"policy leaf {dotted} is now pinned; remove it from FREE_LEAVES")
        elif not pinned and dotted not in FREE_LEAVES:
            errors.append(
                f"policy leaf {dotted} can change without any check objecting; pin it or add it "
                "to FREE_LEAVES with a reason")


COUNT_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
               "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty")


def check_negative_controls(policy: dict, errors: list[str]) -> int:
    """Plant each drift in memory; the pin(s) each names must reject it. Returns the number of controls.

    Each control names the message(s) of the pin(s) that must reject it, not merely "some error". A control
    used to pass on any error: the Sonnet 5 Phase-1 control, which also reverted the capability-probe leaf, was
    satisfied by the probe pin alone, so a loosened auditor or verifier pin went unnoticed. A message is matched
    by its start (the auditor-routing message goes on to print the routes).
    """
    controls = []

    def change_round(candidate: dict) -> None:
        candidate["critique"]["round_target"] = 1

    def restore_cap_five(candidate: dict) -> None:
        # 2.6.9 raised the cap to 7; a revert to 5 without the amendment contract must not pass
        candidate["critique"]["round_cap"] = 5

    def unbounded_amendment(candidate: dict) -> None:
        candidate["critique"]["round_cap_amendment"]["maximum"] = 9

    def restore_sonnet_5_coordinator(candidate: dict) -> None:
        # 2.7.0 moved new audits to Sonnet 5.5; a revert must not pass however the pin is written (an
        # allow-set of LIVE_ROUTES would accept it: existing audits legitimately carry Sonnet 5)
        candidate["models"]["coordinator"].update(LEGACY_CLAUDE_ROUTE)

    def restore_sonnet_5_phase_1(candidate: dict) -> None:
        # the auditors and the verifier together, so the verifier-equals-auditors join cannot be what catches it;
        # the auditor model, auditor alias and verifier pins must each reject it
        for entry in candidate["models"]["phase_1_auditors"].values():
            entry.update(LEGACY_CLAUDE_ROUTE)
        candidate["models"]["post_fan_out_verifier"].update(LEGACY_CLAUDE_ROUTE)

    def restore_sonnet_5_probe(candidate: dict) -> None:
        # the descriptive probe leaf alone, which no other pin reads
        candidate["phase_1"]["capability_probe"]["requested_model"] = LEGACY_CLAUDE_ROUTE["requested_model"]

    def restore_max_effort(candidate: dict) -> None:
        # 2.7.3 lowered new audits' Claude effort from max to xhigh; every audit initialised on 2.7.0 to 2.7.2 keeps
        # Sonnet 5.5 / max, a live route, so an allow-set pin would accept this revert. All eight Claude entries,
        # as reverting the release would: the coordinator, auditor, verifier and probe pins must each reject it.
        models = candidate["models"]
        for entry in [models["coordinator"], models["post_fan_out_verifier"], *models["phase_1_auditors"].values()]:
            entry["reasoning_effort"] = SONNET_5_5_MAX_ROUTE["reasoning_effort"]
        candidate["phase_1"]["capability_probe"]["reasoning_effort"] = SONNET_5_5_MAX_ROUTE["reasoning_effort"]

    def leave_verifier_at_max(candidate: dict) -> None:
        # a partial release: the verifier alone still at max beside xhigh auditors (a mixed route the verifier
        # runner refuses, since it must run on the auditors' own route and reuse their probe)
        candidate["models"]["post_fan_out_verifier"]["reasoning_effort"] = SONNET_5_5_MAX_ROUTE["reasoning_effort"]

    def leave_probe_at_max(candidate: dict) -> None:
        # the descriptive probe leaf alone still at max, which no other pin reads
        candidate["phase_1"]["capability_probe"]["reasoning_effort"] = SONNET_5_5_MAX_ROUTE["reasoning_effort"]

    def restore_terra_critic_route(candidate: dict) -> None:
        # 2.7.0 moved new audits' critics, critique probe and adjudicator to GPT-6.1 Sol; every older audit keeps
        # Terra, so an allow-set of dispatchable models would accept this revert. What repin_critic_route.py
        # writes into one audit's snapshot, applied to the shipped policy.
        candidate["models"]["critics"]["requested_model"] = LEGACY_CRITIC_MODEL
        candidate["models"]["targeted_adjudicator"]["requested_model"] = LEGACY_CRITIC_MODEL
        candidate["critique"]["model_probe"]["requested_model"] = LEGACY_CRITIC_MODEL

    def restore_legacy_probe_bound(candidate: dict) -> None:
        # 2.7.1 raised new audits' probe bound to 350000; every older audit keeps 150000, so an allow-set of live
        # bounds would accept this revert. Both leaves, as reverting the release would; each has its own pin.
        candidate["phase_1"]["runtime"]["probe_max_normalized_tokens"] = LEGACY_PROBE_BOUND
        candidate["phase_1"]["capability_probe"]["maximum_normalized_tokens"] = LEGACY_PROBE_BOUND

    def restore_legacy_phase1_timeout(candidate: dict) -> None:
        # 2.7.2 raised new audits' per-call Phase-1 timeout to 5400 s; every older audit keeps 2700, so an allow-set
        # of live timeouts would accept this revert. Both leaves, as reverting the release would; each has its pin.
        candidate["phase_1"]["runtime"]["timeout_seconds"] = LEGACY_PHASE1_TIMEOUT
        candidate["phase_1"]["provider_timeout_seconds"] = LEGACY_PHASE1_TIMEOUT

    def restore_legacy_critique_timeout(candidate: dict) -> None:
        # 2.7.4 froze new audits' critique ceiling at 5400 s; 2700 is still live (older audits run at it, unfrozen),
        # so an allow-set would accept this revert.
        candidate["critique"]["provider_timeout_seconds"] = LEGACY_CRITIQUE_TIMEOUT

    def drop_claim_change_rule(candidate: dict) -> None:
        # 2.7.4 froze new audits' claim-change rule; without the leaf a new audit would be legacy (every specialist
        # re-reviews every bookkeeping change), which the runtime accepts, so only the pin can see it.
        candidate["critique"]["later_rounds"]["routing_manifest"].pop("claim_change_rule")

    def change_auditor(candidate: dict) -> None:
        candidate["models"]["phase_1_auditors"]["auditor_text_vs_data"]["spawn_alias"] = "opus"

    def enable_waiver(candidate: dict) -> None:
        candidate["verification"]["allow_no_verification"] = True

    def change_registry(candidate: dict) -> None:
        candidate["finding_source_of_truth"]["runtime_registry"] = "other.jsonl"

    def broaden_later_round(candidate: dict) -> None:
        candidate["critique"]["later_rounds"]["cross_domain_rule"] = "run_every_implicated_specialist"

    def disable_document_cache(candidate: dict) -> None:
        candidate["local_execution"]["document_build_cache"]["default"] = "disabled"

    def move_verifier_to_critic_family(candidate: dict) -> None:
        # The tempting mistake: "the verifier is an independent check, so put it on the other
        # family". A family-B verifier plus a family-B critic is one family confirming twice.
        candidate["models"]["post_fan_out_verifier"].update(
            requested_model=SHIPPED_CRITIC_MODEL, provider="openai")

    claude = SHIPPED_CLAUDE_LABEL
    auditor_model_pin = f"one or more phase-1 auditors is not assigned to {claude}"
    verifier_pin = f"post-fan-out verifier policy is not {claude} on the Phase-1 auditor family"
    controls.extend(
        [
            ("round target", change_round, check_policy_shape, ["round target/cap is not 2/7"]),
            ("round cap", restore_cap_five, check_policy_shape, ["round target/cap is not 2/7"]),
            ("round-cap amendment maximum", unbounded_amendment, check_policy_shape,
             ["round-cap amendment contract drifted"]),
            ("coordinator restored to Sonnet 5", restore_sonnet_5_coordinator, check_policy_shape,
             [f"coordinator policy is not {claude}"]),
            ("Phase-1 route restored to Sonnet 5", restore_sonnet_5_phase_1, check_policy_shape,
             [auditor_model_pin, "auditor routing is", verifier_pin]),
            ("Phase-1 capability probe restored to Sonnet 5", restore_sonnet_5_probe, check_policy_shape,
             [f"the Phase-1 capability probe is not {claude}"]),
            ("Claude effort restored to max", restore_max_effort, check_policy_shape,
             [f"coordinator policy is not {claude}", auditor_model_pin, verifier_pin,
              f"the Phase-1 capability probe is not {claude}"]),
            ("post-fan-out verifier left at max", leave_verifier_at_max, check_policy_shape,
             [verifier_pin, "post-fan-out verifier route differs from the Phase-1 auditors' route"]),
            ("Phase-1 capability probe left at max", leave_probe_at_max, check_policy_shape,
             [f"the Phase-1 capability probe is not {claude}"]),
            (f"critic route restored to {LEGACY_CRITIC_MODEL}", restore_terra_critic_route, check_policy_shape,
             [f"critic policy is not {SHIPPED_CRITIC_MODEL} / high",
              f"targeted-adjudicator policy is not {SHIPPED_CRITIC_MODEL} / max",
              f"critique model probe is not {SHIPPED_CRITIC_MODEL} / high"]),
            (f"Phase-1 probe bound restored to {LEGACY_PROBE_BOUND}", restore_legacy_probe_bound, check_policy_shape,
             ["the Phase-1 probe bound phase_1.runtime.probe_max_normalized_tokens is",
              "the capability-probe leaf maximum_normalized_tokens is"]),
            (f"Phase-1 timeout restored to {LEGACY_PHASE1_TIMEOUT}", restore_legacy_phase1_timeout,
             check_policy_shape,
             ["the Phase-1 timeout phase_1.runtime.timeout_seconds is",
              "the Phase-1 leaf provider_timeout_seconds is"]),
            (f"critique ceiling restored to {LEGACY_CRITIQUE_TIMEOUT}", restore_legacy_critique_timeout,
             check_policy_shape, ["the critique ceiling critique.provider_timeout_seconds is"]),
            ("claim-change rule removed", drop_claim_change_rule, check_policy_shape,
             ["the later-round claim-change rule critique.later_rounds.routing_manifest.claim_change_rule is"]),
            ("auditor routing", change_auditor, check_policy_shape, ["auditor routing is"]),
            ("verification waiver", enable_waiver, check_verification_and_release,
             ["verification policy drifted from the accuracy-safe content-bound contract"]),
            ("finding registry", change_registry, check_finding_projection,
             ["finding source-of-truth projection policy drifted"]),
            ("later-round routing", broaden_later_round, check_policy_shape,
             ["later-round extra specialists are not restricted to multi-domain critical changes"]),
            ("document cache", disable_document_cache, check_local_execution,
             ["local batch/cache/manifest/atomic execution policy drifted"]),
            ("post-fan-out verifier family", move_verifier_to_critic_family, check_policy_shape,
             [verifier_pin, "post-fan-out verifier route differs from the Phase-1 auditors' route"]),
        ]
    )
    for label, mutate, detector, required in controls:
        candidate = deepcopy(policy)
        mutate(candidate)
        detected: list[str] = []
        detector(candidate, detected)
        if not detected:
            errors.append(f"negative control was not rejected: {label}")
            continue
        for message in required:
            if not any(error.startswith(message) for error in detected):
                errors.append(f"negative control was not rejected by its pin: {label}: no error starts "
                              f"{message!r} (got {detected})")
    return len(controls)


def main() -> int:
    errors: list[str] = []
    try:
        policy = load_policy()
        check_policy_shape(policy, errors)
        check_finding_row_schema(policy, errors)
        check_finding_projection(policy, errors)
        check_integration_helpers(errors)
        check_verifier_contract(policy, errors)
        check_claude_routes(errors)
        check_probe_bounds(errors)
        check_phase1_timeouts(errors)
        check_critique_timeouts(errors)
        check_claim_change_rules(errors)
        check_local_execution(policy, errors)
        check_verification_and_release(policy, errors)
        check_docs(policy, errors)
        check_disclosure_language(errors)
        check_policy_leaf_coverage(policy, errors)
        controls = check_negative_controls(policy, errors)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        errors.append(f"invalid policy structure: {exc}")

    if errors:
        print("POLICY DRIFT CHECK FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"POLICY DRIFT CHECK OK: v2 policy/docs agree and {COUNT_WORDS[controls]} planted drifts were rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
