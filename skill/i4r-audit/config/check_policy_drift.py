#!/usr/bin/env python3
"""Fail when maintained workflow docs drift from pipeline-policy-v2.json."""

from __future__ import annotations

import json
import re
import sys
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "pipeline-policy-v2.json"


def load_policy() -> dict:
    with POLICY_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def policy_summary(policy: dict) -> str:
    coordinator = policy["models"]["coordinator"]
    auditors = policy["models"]["phase_1_auditors"]
    critics = policy["models"]["critics"]
    adjudicator = policy["models"]["targeted_adjudicator"]
    critique = policy["critique"]
    r1 = critique["round_1"]
    later = critique["later_rounds"]
    settled = critique["settled_facts"]

    sonnet_areas = sorted(v["area"] for v in auditors.values() if v["spawn_alias"] == "sonnet")
    role_labels = [name.removeprefix("critic_") for name in r1["roles"]]

    return "\n".join(
        [
            "<!-- PIPELINE-POLICY-V2:START -->",
            f"- Coordinator: `{coordinator['requested_model']}` (`{coordinator['spawn_alias']}`), effort `{coordinator['reasoning_effort']}`.",
            f"- Phase 1: {len(auditors)} auditors, {policy['phase_1']['parallelism']}-up; all areas {','.join(map(str, sonnet_areas))} use `sonnet-5` (`sonnet`), effort `max`.",
            f"- Critics: `{critics['requested_model']}`, effort `{critics['reasoning_effort']}`.",
            f"- Critique: target {critique['round_target']} rounds, hard cap {critique['round_cap']}; round 1 = {', '.join(role_labels)}, {r1['parallelism']}-up.",
            f"- Later rounds: completeness plus one best specialist by default ({later['default_total_critics']} critics); add specialists only for conclusion-changing or disclosure-critical changes spanning multiple domains.",
            f"- Synthesis: deterministic merge; `{adjudicator['requested_model']}`, effort `{adjudicator['reasoning_effort']}`, adjudicates only policy-triggered conflicts or high-risk evidence gaps.",
            f"- Retry budget: at most {critique['max_attempts_per_role_total']} total attempts per role across parallel, serial, and resumed execution.",
            f"- Settled facts: generate after round {settled['generate_after_round']} after {settled['minimum_independent_confirmations']} independent confirmations spanning {settled['minimum_distinct_model_families']} model families; inject from round {settled['inject_from_round']}.",
            "- Stop values: `target_complete` after the required reviewed rounds; `converged` only after continuing; `cap_with_unresolved` at round 5; `aborted` only for a genuine blocker or failed run.",
            "<!-- PIPELINE-POLICY-V2:END -->",
        ]
    )


def check_policy_shape(policy: dict, errors: list[str]) -> None:
    auditors = policy["models"]["phase_1_auditors"]
    aliases = {entry["area"]: entry["spawn_alias"] for entry in auditors.values()}
    expected_aliases = {1: "sonnet", 2: "sonnet", 3: "sonnet", 4: "sonnet", 5: "sonnet"}
    if aliases != expected_aliases:
        errors.append(f"auditor routing is {aliases}, expected {expected_aliases}")
    if any(entry.get("provider") != "anthropic" for entry in auditors.values()):
        errors.append("one or more phase-1 auditors is not assigned to Anthropic")
    if any(
        (entry.get("requested_model"), entry.get("reasoning_effort")) != ("sonnet-5", "max")
        for entry in auditors.values()
    ):
        errors.append("one or more phase-1 auditors is not assigned to Sonnet 5 / max")
    if policy["models"]["coordinator"] != {
        "requested_model": "sonnet-5",
        "spawn_alias": "sonnet",
        "provider": "anthropic",
        "reasoning_effort": "max",
    }:
        errors.append("coordinator policy is not Sonnet 5 / max")
    critics = policy["models"]["critics"]
    if critics != {
        "requested_model": "gpt-5.6-terra",
        "provider": "openai",
        "reasoning_effort": "high",
        "must_differ_from_coordinator_family": True,
    }:
        errors.append("critic policy is not gpt-5.6-terra / high")
    targeted_adjudicator = policy["models"].get("targeted_adjudicator")
    if targeted_adjudicator != {
        "requested_model": "gpt-5.6-terra",
        "provider": "openai",
        "reasoning_effort": "max",
        "must_differ_from_coordinator_family": True,
    }:
        errors.append("targeted-adjudicator policy is not gpt-5.6-terra / max")
    critique = policy["critique"]
    if (critique["round_target"], critique["round_cap"]) != (2, 5):
        errors.append("round target/cap is not 2/5")
    expected_r1 = ["critic_stats", "critic_data", "critic_fairness", "critic_completeness"]
    if critique["round_1"]["roles"] != expected_r1 or critique["round_1"]["parallelism"] != 4:
        errors.append("round-1 critic policy drifted")
    if critique["max_attempts_per_role_total"] != 3:
        errors.append("retry budget is not three total attempts")
    expected_probe = {
        "requested_model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "cache_scope": "one_success_per_audit_model_and_effort",
        "maximum_successful_provider_calls": 1,
        "reuse_across_rounds": True,
    }
    if critique.get("model_probe") != expected_probe:
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
        errors.append("targeted adjudication is not assigned to Terra")
    if adjudication.get("model_policy_ref") != "models.targeted_adjudicator":
        errors.append("targeted adjudication does not reference its max-effort model policy")
    if adjudication.get("separate_model_default") != "triggered_only":
        errors.append("targeted adjudication is not conditional on policy triggers")
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
        "cold_start_provider_invocations_including_probe": 13,
        "cold_start_critique_provider_invocations": 7,
        "warm_probe_critique_provider_invocations": 6,
        "normal_worst_case_critique_provider_invocations": 19,
        "one_triggered_adjudicator_round_worst_case_provider_invocations": 22,
    }
    if policy.get("default_role_counts") != expected_counts:
        errors.append("default actor/provider invocation counts drifted")


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
        "primary_artifact_count": 5,
        "primary_artifacts": [
            "report/*.pdf", "claims_script.*", "run.json", "comment.json",
            "finalization_receipt.json",
        ],
        "manifest": "release-manifest.json",
        "total_file_count": 6,
    }
    if publication != expected_publication:
        errors.append("publication policy is not five bound artefacts plus one release manifest")


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
        "metadata/scripts/": "obsolete metadata/scripts path",
        "eleven-item": "checklist count is twelve",
        "Critics run **sequentially**": "obsolete sequential-critic instruction",
        "executive summary and the two summary tables": "obsolete page-1 format",
        "generated_after_round=2": "obsolete settled-facts timing",
        "--stopping-reason cap": "obsolete stopping enum",
        "SYNTH=": "legacy synthesis control",
    }
    for path in maintained:
        if not path.is_file():
            errors.append(f"missing maintained document: {path.relative_to(ROOT)}")
            continue
        text = path.read_text(encoding="utf-8")
        if "config/pipeline-policy-v2.json" not in text:
            errors.append(f"{path.relative_to(ROOT)} does not identify the canonical policy")
        for needle, reason in banned.items():
            if needle in text:
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
    }
    for path in [ROOT / "SKILL.md", ROOT / "INSTALL.md"]:
        text = path.read_text(encoding="utf-8")
        for marker, label in workflow_markers.items():
            if marker not in text:
                errors.append(f"{path.relative_to(ROOT)} is missing {label}: {marker!r}")

    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    words = len(re.findall(r"\b[\w'-]+\b", skill))
    if words > 4000:
        errors.append(f"SKILL.md has {words} words; maximum is 4000")
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


def check_negative_controls(policy: dict, errors: list[str]) -> None:
    controls = []

    def change_round(candidate: dict) -> None:
        candidate["critique"]["round_target"] = 1

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

    controls.extend(
        [
            ("round target", change_round, check_policy_shape),
            ("auditor routing", change_auditor, check_policy_shape),
            ("verification waiver", enable_waiver, check_verification_and_release),
            ("finding registry", change_registry, check_finding_projection),
            ("later-round routing", broaden_later_round, check_policy_shape),
            ("document cache", disable_document_cache, check_local_execution),
        ]
    )
    for label, mutate, detector in controls:
        candidate = deepcopy(policy)
        mutate(candidate)
        detected: list[str] = []
        detector(candidate, detected)
        if not detected:
            errors.append(f"negative control was not rejected: {label}")


def main() -> int:
    errors: list[str] = []
    try:
        policy = load_policy()
        check_policy_shape(policy, errors)
        check_finding_row_schema(policy, errors)
        check_finding_projection(policy, errors)
        check_integration_helpers(errors)
        check_local_execution(policy, errors)
        check_verification_and_release(policy, errors)
        check_docs(policy, errors)
        check_negative_controls(policy, errors)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        errors.append(f"invalid policy structure: {exc}")

    if errors:
        print("POLICY DRIFT CHECK FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print("POLICY DRIFT CHECK OK: v2 policy/docs agree and six planted drifts were rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
