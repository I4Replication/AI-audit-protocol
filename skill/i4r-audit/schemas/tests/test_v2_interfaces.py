from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


SCHEMAS = Path(__file__).resolve().parents[1]
TOOLS = SCHEMAS / "tools"
sys.path.insert(0, str(TOOLS))

from normalize_v1_to_v2 import (  # noqa: E402
    NormalizationError,
    normalize_comment_v1,
    normalize_run_v1,
)
from validate_v2 import (  # noqa: E402
    ValidationError,
    validate_comment_finding,
    validate_critic_result,
    validate_finding_row,
    validate_findings_registry,
    validate_invocation,
    validate_run,
)


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


class SchemaSyntaxTests(unittest.TestCase):
    def test_every_schema_and_fixture_is_valid_json(self) -> None:
        files = sorted(SCHEMAS.glob("*.json")) + sorted((SCHEMAS / "v2").glob("*.json")) + sorted((SCHEMAS / "fixtures").glob("*.json"))
        self.assertGreaterEqual(len(files), 10)
        for path in files:
            with self.subTest(path=path.name):
                load(path)

    def test_v2_schema_ids_are_unique(self) -> None:
        ids = [load(path)["$id"] for path in sorted((SCHEMAS / "v2").glob("*.schema.json"))]
        self.assertEqual(len(ids), len(set(ids)))

    def test_embedded_and_standalone_invocation_contracts_do_not_drift(self) -> None:
        embedded = load(SCHEMAS / "run.schema.json")["$defs"]["invocation_record"]
        standalone = load(SCHEMAS / "v2" / "invocation-record.schema.json")
        self.assertEqual(standalone["$defs"]["invocation_usage"], load(SCHEMAS / "run.schema.json")["$defs"]["invocation_usage"])
        for metadata in ("$schema", "$id", "$defs", "title", "description"):
            standalone.pop(metadata, None)
        self.assertEqual(standalone, embedded)

    def test_run_v2_stopping_enum_is_the_policy_set(self) -> None:
        schema = load(SCHEMAS / "run.schema.json")
        v2_enum = schema["allOf"][0]["then"]["properties"]["stopping_reason"]["enum"]
        self.assertEqual(set(v2_enum) - {None}, {"target_complete", "converged", "cap_with_unresolved", "aborted"})


class CommentFindingSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.example = load(SCHEMAS / "EXAMPLE_comment.json")

    def test_full_example_findings_satisfy_strict_semantics(self) -> None:
        for finding in self.example["findings"]:
            validate_comment_finding(finding, finding["id"])

    def test_null_impact_tier_evidence_or_confidence_is_rejected(self) -> None:
        base = self.example["findings"][0]
        for field in ("impact", "verification_tier", "evidence", "confidence"):
            mutated = copy.deepcopy(base)
            mutated[field] = None
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_comment_finding(mutated)

    def test_unverifiable_requires_explicit_basis_reason_and_blocked_tier(self) -> None:
        base = next(row for row in self.example["findings"] if row["impact"] == "unverifiable")
        for field in ("unverifiable_basis", "unverifiable_reason", "blocked_tier"):
            mutated = copy.deepcopy(base)
            mutated[field] = None
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_comment_finding(mutated)


class FindingRowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.row = load(SCHEMAS / "fixtures" / "v2_finding_row.json")

    def test_valid_promotable_row(self) -> None:
        validate_finding_row(self.row)
        null_settled = copy.deepcopy(self.row)
        null_settled["settled"] = None
        validate_finding_row(null_settled)

    def test_duplicate_reviewer_or_independent_role_is_rejected(self) -> None:
        duplicate_reviewer = copy.deepcopy(self.row)
        duplicate_reviewer["verifications"].append(copy.deepcopy(duplicate_reviewer["verifications"][0]))
        duplicate_origin = copy.deepcopy(self.row)
        duplicate_origin["provenance"]["independently_found_by"].append(
            duplicate_origin["provenance"]["independently_found_by"][0])
        for mutated in (duplicate_reviewer, duplicate_origin):
            with self.assertRaises(ValidationError):
                validate_finding_row(mutated)

    def test_promotable_row_requires_hashed_evidence_environment_and_two_reviewers(self) -> None:
        mutations = []
        no_hashes = copy.deepcopy(self.row)
        no_hashes["evidence"]["source_hashes"] = []
        mutations.append(no_hashes)
        no_environment = copy.deepcopy(self.row)
        no_environment["dependencies"]["environment_fingerprint"] = None
        mutations.append(no_environment)
        one_reviewer = copy.deepcopy(self.row)
        one_reviewer["verifications"] = one_reviewer["verifications"][:1]
        mutations.append(one_reviewer)
        same_reviewer = copy.deepcopy(self.row)
        same_reviewer["verifications"][1]["reviewer_id"] = same_reviewer["verifications"][0]["reviewer_id"]
        mutations.append(same_reviewer)
        same_invocation = copy.deepcopy(self.row)
        same_invocation["verifications"][1]["invocation_id"] = same_invocation["verifications"][0]["invocation_id"]
        mutations.append(same_invocation)
        same_evidence_record = copy.deepcopy(self.row)
        same_evidence_record["verifications"][1]["evidence_record"] = copy.deepcopy(
            same_evidence_record["verifications"][0]["evidence_record"])
        mutations.append(same_evidence_record)
        same_family = copy.deepcopy(self.row)
        same_family["verifications"][1]["model_family"] = same_family["verifications"][0]["model_family"]
        mutations.append(same_family)
        for index, mutated in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValidationError):
                validate_finding_row(mutated)

    def test_every_verification_has_an_invocation_and_hashed_evidence_record(self) -> None:
        missing_invocation = copy.deepcopy(self.row)
        del missing_invocation["verifications"][0]["invocation_id"]
        unsafe_record = copy.deepcopy(self.row)
        unsafe_record["verifications"][0]["evidence_record"]["path"] = "../outside.json"
        malformed_hash = copy.deepcopy(self.row)
        malformed_hash["verifications"][0]["evidence_record"]["sha256"] = "not-a-hash"
        for mutated in (missing_invocation, unsafe_record, malformed_hash):
            with self.assertRaises(ValidationError):
                validate_finding_row(mutated)

    def test_conclusion_change_requires_independence_even_before_promotion(self) -> None:
        mutated = copy.deepcopy(self.row)
        mutated["status"] = "draft"
        mutated["coordinator_approved"] = False
        mutated["changes_conclusion"] = True
        mutated["changes_conclusion_which"] = "Primary inference"
        mutated["verifications"] = mutated["verifications"][:1]
        with self.assertRaises(ValidationError):
            validate_finding_row(mutated)

    def test_policy_and_provenance_mutations_are_rejected(self) -> None:
        wrong_rank = copy.deepcopy(self.row)
        wrong_rank["impact_rank"] = 5
        overlap = copy.deepcopy(self.row)
        overlap["provenance"]["agreed_after_seeing"] = ["critic_stats"]
        for mutated in (wrong_rank, overlap):
            with self.assertRaises(ValidationError):
                validate_finding_row(mutated)

    def test_generation_fields_are_semantic(self) -> None:
        invalid_section = copy.deepcopy(self.row)
        invalid_section["report_section"] = "Results"
        missing_conclusion = copy.deepcopy(self.row)
        missing_conclusion["changes_conclusion"] = True
        missing_conclusion["changes_conclusion_which"] = None
        missing_snippet = copy.deepcopy(self.row)
        missing_snippet["verification_snippet"] = None
        for mutated in (invalid_section, missing_conclusion, missing_snippet):
            with self.assertRaises(ValidationError):
                validate_finding_row(mutated)

    def test_ledger_disposition_requires_merge_target_or_exclusion_reason(self) -> None:
        merged = copy.deepcopy(self.row)
        merged["ledger_disposition"] = {"status": "MERGED", "target": None, "reason": None}
        excluded = copy.deepcopy(self.row)
        excluded["ledger_disposition"] = {"status": "EXCLUDED", "target": None, "reason": None}
        for mutated in (merged, excluded):
            with self.assertRaises(ValidationError):
                validate_finding_row(mutated)

    def test_domain_source_locus_and_dependency_paths_are_semantic(self) -> None:
        invalid_domain = copy.deepcopy(self.row)
        invalid_domain["domain"] = "quantitative"
        absolute_source = copy.deepcopy(self.row)
        absolute_source["source_location"]["path"] = "/private/paper.txt"
        traversal = copy.deepcopy(self.row)
        traversal["dependencies"]["artifact_paths"] = ["../outside.txt"]
        unc_path = copy.deepcopy(self.row)
        unc_path["dependencies"]["artifact_paths"] = [r"\\server\share\outside.txt"]
        duplicates = copy.deepcopy(self.row)
        duplicates["dependencies"]["finding_ids"] = ["F0", "F0"]
        self_dependency = copy.deepcopy(self.row)
        self_dependency["dependencies"]["finding_ids"] = [self.row["id"]]
        for mutated in (invalid_domain, absolute_source, traversal, unc_path, duplicates, self_dependency):
            with self.assertRaises(ValidationError):
                validate_finding_row(mutated)

    def test_unverifiable_row_requires_basis(self) -> None:
        mutated = copy.deepcopy(self.row)
        mutated["impact"] = "unverifiable"
        mutated["impact_rank"] = 1
        mutated["unverifiable_basis"] = None
        with self.assertRaises(ValidationError):
            validate_finding_row(mutated)


class CriticResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = load(SCHEMAS / "fixtures" / "v2_critic_result.json")

    def test_valid_complete_result(self) -> None:
        validate_critic_result(self.result)

    def test_completion_marker_and_coverage_are_atomic(self) -> None:
        no_marker = copy.deepcopy(self.result)
        no_marker["completion_marker"] = False
        uncovered = copy.deepcopy(self.result)
        uncovered["coverage"]["completed_checks"] = []
        for mutated in (no_marker, uncovered):
            with self.assertRaises(ValidationError):
                validate_critic_result(mutated)

    def test_changed_verdict_requires_change_and_unverifiable_basis(self) -> None:
        no_change = copy.deepcopy(self.result)
        no_change["items"][0]["verdict"] = "REFUTED"
        no_change["items"][0]["requested_correction"] = None
        no_basis = copy.deepcopy(self.result)
        no_basis["items"][0]["verdict"] = "UNVERIFIABLE"
        no_basis["items"][0]["unverifiable_basis"] = None
        for mutated in (no_change, no_basis):
            with self.assertRaises(ValidationError):
                validate_critic_result(mutated)

    def test_blocking_rollup_must_match_items(self) -> None:
        mutated = copy.deepcopy(self.result)
        mutated["items"][0]["priority"] = "BLOCKING"
        with self.assertRaises(ValidationError):
            validate_critic_result(mutated)

    def test_coverage_and_artifact_references_are_bounded(self) -> None:
        duplicate_check = copy.deepcopy(self.result)
        duplicate_check["coverage"]["assigned_checks"] *= 2
        unassigned_completion = copy.deepcopy(self.result)
        unassigned_completion["coverage"]["completed_checks"].append("not-assigned")
        absolute_artifact = copy.deepcopy(self.result)
        absolute_artifact["items"][0]["evidence"]["artifact_refs"] = [{
            "path": "/private/source.txt", "sha256": "0" * 64,
        }]
        for mutated in (duplicate_check, unassigned_completion, absolute_artifact):
            with self.assertRaises(ValidationError):
                validate_critic_result(mutated)

    def test_every_covered_claim_has_exactly_one_assessment_and_skips_fail(self) -> None:
        missing = copy.deepcopy(self.result)
        missing["items"] = []
        extra_coverage = copy.deepcopy(self.result)
        extra_coverage["covered_claim_ids"].append("F2")
        duplicate = copy.deepcopy(self.result)
        second = copy.deepcopy(duplicate["items"][0])
        second["item_id"] = "stats-F1-duplicate"
        duplicate["items"].append(second)
        skipped = copy.deepcopy(self.result)
        skipped["coverage"]["skipped_checks"] = [{
            "check": skipped["coverage"]["assigned_checks"][0],
            "reason": "not attempted",
        }]
        skipped["coverage"]["completed_checks"] = []
        for mutated in (missing, extra_coverage, duplicate, skipped):
            with self.assertRaises(ValidationError):
                validate_critic_result(mutated)

    def test_artifact_evidence_requires_exact_immutable_bindings(self) -> None:
        plain_path = copy.deepcopy(self.result)
        plain_path["items"][0]["evidence"]["artifact_refs"] = ["work/check.txt"]
        missing_hash = copy.deepcopy(self.result)
        del missing_hash["items"][0]["evidence"]["artifact_refs"][0]["sha256"]
        malformed_hash = copy.deepcopy(self.result)
        malformed_hash["items"][0]["evidence"]["artifact_refs"][0]["sha256"] = "ABC"
        extra_key = copy.deepcopy(self.result)
        extra_key["items"][0]["evidence"]["artifact_refs"][0]["live"] = True
        traversal = copy.deepcopy(self.result)
        traversal["items"][0]["evidence"]["reproducible_script"]["path"] = "../live.py"
        duplicate_path = copy.deepcopy(self.result)
        duplicate_path["items"][0]["evidence"]["artifact_refs"] *= 2
        for mutated in (plain_path, missing_hash, malformed_hash, extra_key, traversal,
                        duplicate_path):
            with self.assertRaises(ValidationError):
                validate_critic_result(mutated)

    def test_identity_and_all_five_input_hashes_are_required_and_bound(self) -> None:
        cases = []
        for field in ("invocation_id", "input_hashes", "covered_claim_ids", "conflicts",
                      "completeness_declaration"):
            mutated = copy.deepcopy(self.result)
            mutated.pop(field)
            cases.append(mutated)
        for field in ("snapshot_sha256", "report_sha256", "prompt_sha256", "policy_sha256",
                      "evidence_manifest_sha256"):
            mutated = copy.deepcopy(self.result)
            mutated["input_hashes"].pop(field)
            cases.append(mutated)
        mismatched_report = copy.deepcopy(self.result)
        mismatched_report["input_hashes"]["report_sha256"] = "0" * 64
        cases.append(mismatched_report)
        malformed_hash = copy.deepcopy(self.result)
        malformed_hash["input_hashes"]["prompt_sha256"] = "ABC"
        cases.append(malformed_hash)
        for mutated in cases:
            with self.assertRaises(ValidationError):
                validate_critic_result(mutated)

    def test_claim_coverage_conflicts_and_completeness_fail_closed(self) -> None:
        uncovered_assessment = copy.deepcopy(self.result)
        uncovered_assessment["covered_claim_ids"] = []
        foreign_conflict = copy.deepcopy(self.result)
        foreign_conflict["conflicts"] = [{
            "conflict_id": "C1", "claim_ids": ["F9"], "description": "conflict fixture",
        }]
        false_declaration = copy.deepcopy(self.result)
        false_declaration["completeness_declaration"]["all_assessments_evidenced"] = False
        omitted = copy.deepcopy(self.result)
        omitted["completeness_declaration"]["unresolved_omissions"] = ["one missing check"]
        for mutated in (uncovered_assessment, foreign_conflict, false_declaration, omitted):
            with self.assertRaises(ValidationError):
                validate_critic_result(mutated)


class InvocationAndRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.invocation = load(SCHEMAS / "fixtures" / "v2_invocation_record.json")

    def test_valid_completed_invocation(self) -> None:
        validate_invocation(self.invocation)

    def test_completed_invocation_rejects_unknown_effort_usage_contradiction_and_missing_output(self) -> None:
        unknown_effort = copy.deepcopy(self.invocation)
        unknown_effort["reasoning_effort"] = "unknown"
        usage_contradiction = copy.deepcopy(self.invocation)
        usage_contradiction["usage"]["obtainable"] = False
        partial_usage = copy.deepcopy(self.invocation)
        partial_usage["usage"]["reasoning_tokens"] = None
        missing_output = copy.deepcopy(self.invocation)
        missing_output["output_sha256"] = None
        invalid_cache = copy.deepcopy(self.invocation)
        invalid_cache["cache_usage"] = "hit"
        invalid_error_class = copy.deepcopy(self.invocation)
        invalid_error_class["error_class"] = "retryable"
        absolute_output = copy.deepcopy(self.invocation)
        absolute_output["output_artifact"] = "/tmp/result.json"
        for mutated in (unknown_effort, usage_contradiction, partial_usage, missing_output,
                        invalid_cache, invalid_error_class, absolute_output):
            with self.assertRaises(ValidationError):
                validate_invocation(mutated)

    def test_partial_claude_usage_requires_honest_reasons_and_a_verified_derived_total(self) -> None:
        partial = copy.deepcopy(self.invocation)
        partial["usage"] = {
            "obtainable": True,
            "input_tokens": 2,
            "output_tokens": 163,
            "cached_input_tokens": 30597,
            "cache_creation_input_tokens": 10876,
            "reasoning_tokens": None,
            "total_tokens": 41638,
            "source": "cli_log",
            "unavailable_reasons": {
                "reasoning_tokens": "provider CLI does not expose separate reasoning tokens",
            },
            "total_tokens_kind": "derived",
            "total_tokens_components": [
                "input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens",
            ],
            "total_tokens_unavailable_reason": None,
        }
        validate_invocation(partial)

        missing_reason = copy.deepcopy(partial)
        missing_reason["usage"]["unavailable_reasons"] = {}
        reason_for_observed = copy.deepcopy(partial)
        reason_for_observed["usage"]["unavailable_reasons"]["input_tokens"] = "not available"
        wrong_total = copy.deepcopy(partial)
        wrong_total["usage"]["total_tokens"] += 1
        unavailable_component = copy.deepcopy(partial)
        unavailable_component["usage"]["cache_creation_input_tokens"] = None
        unavailable_component["usage"]["unavailable_reasons"]["cache_creation_input_tokens"] = "not exposed"
        provider_total_with_components = copy.deepcopy(partial)
        provider_total_with_components["usage"]["total_tokens_kind"] = "provider_reported"
        completed_without_total = copy.deepcopy(partial)
        completed_without_total["usage"].update({
            "total_tokens": None,
            "total_tokens_kind": "unavailable",
            "total_tokens_components": [],
            "total_tokens_unavailable_reason": "provider did not report and components were incomplete",
        })
        for mutated in (
            missing_reason, reason_for_observed, wrong_total, unavailable_component,
            provider_total_with_components, completed_without_total,
        ):
            with self.subTest(mutated=mutated["usage"]), self.assertRaises(ValidationError):
                validate_invocation(mutated)

    def test_served_model_ids_and_non_provider_cache_disposition_fail_closed(self) -> None:
        validate_invocation(self.invocation)
        missing_primary = copy.deepcopy(self.invocation)
        missing_primary["observed_model_ids"] = ["auxiliary-model"]
        duplicate = copy.deepcopy(self.invocation)
        duplicate["observed_model_ids"] = [
            self.invocation["observed_model"], self.invocation["observed_model"],
        ]
        empty = copy.deepcopy(self.invocation)
        empty["observed_model_ids"] = []
        for mutated in (missing_primary, duplicate, empty):
            with self.assertRaises(ValidationError):
                validate_invocation(mutated)

        no_provider = copy.deepcopy(self.invocation)
        no_provider.update({"provider_call": False, "cache_usage": "not_applicable"})
        validate_invocation(no_provider)
        no_provider_write = copy.deepcopy(no_provider)
        no_provider_write["cache_usage"] = "write"
        provider_not_applicable = copy.deepcopy(self.invocation)
        provider_not_applicable["cache_usage"] = "not_applicable"
        for mutated in (no_provider_write, provider_not_applicable):
            with self.assertRaises(ValidationError):
                validate_invocation(mutated)

    def test_round_target_cap_and_stopping_reason_are_consistent(self) -> None:
        run = normalize_run_v1(load(SCHEMAS / "fixtures" / "v1_minimal_run.json"))
        validate_run(run)
        target_complete = copy.deepcopy(run)
        target_complete.update({
            "round_target": 2,
            "stopping_reason": "target_complete",
            "changed_claim_ids": ["C1"],
            "rereviewed_claim_ids": ["C1"],
            "unresolved_blocking_item_ids": [],
            "unresolved_conclusion_critical_item_ids": [],
            "unreviewed_sections": [],
        })
        validate_run(target_complete)
        target_above_cap = copy.deepcopy(run)
        target_above_cap["round_target"] = 3
        target_mismatch = copy.deepcopy(run)
        target_mismatch["round_target"] = 1
        target_mismatch["stopping_reason"] = "target_complete"
        target_mismatch["changed_claim_ids"] = ["C1"]
        target_mismatch["rereviewed_claim_ids"] = []
        target_mismatch["unresolved_blocking_item_ids"] = []
        target_mismatch["unresolved_conclusion_critical_item_ids"] = []
        target_mismatch["unreviewed_sections"] = []
        for mutated in (target_above_cap, target_mismatch):
            with self.assertRaises(ValidationError):
                validate_run(mutated)

    def test_live_v2_accepts_the_2_6_9_cap_and_its_amendment_chain(self) -> None:
        # 2.6.9: new audits freeze a cap of 7; an audit initialised earlier keeps 5 until a journaled
        # amendment raises it (run.policy_amendments, chained from 5 to the run's cap).
        base = normalize_run_v1(load(SCHEMAS / "fixtures" / "v1_minimal_run.json"))
        base.pop("normalization")
        base.update({"round_target": 2, "round_cap": 7, "rounds_run": 2, "stopping_reason": None})
        validate_run(base)
        for cap in (6, 8):
            mutated = copy.deepcopy(base)
            mutated["round_cap"] = cap
            with self.subTest(cap=cap), self.assertRaises(ValidationError):
                validate_run(mutated)
        at_cap = copy.deepcopy(base)
        at_cap.update({"rounds_run": 7, "stopping_reason": "cap_with_unresolved",
                       "stopping_reason_detail": "One section changed after its last review.",
                       "unreviewed_sections": ["Section 2"],
                       "policy_amendments": [{"seq": 300, "at": "2026-09-28T12:00:00Z",
                                              "leaf": "critique.round_cap", "from": 5, "to": 7,
                                              "reason": "continue past the old cap",
                                              "snapshot_sha256": "a" * 64}]})
        validate_run(at_cap)
        for label, change in (("below the cap", {"rounds_run": 6}),
                              ("chain ends below the cap", {"round_cap": 5, "rounds_run": 5})):
            mutated = copy.deepcopy(at_cap)
            mutated.update(change)
            with self.subTest(label), self.assertRaises(ValidationError):
                validate_run(mutated)

    def test_live_v2_target_cap_and_stop_evidence_fail_closed(self) -> None:
        base = normalize_run_v1(load(SCHEMAS / "fixtures" / "v1_minimal_run.json"))
        base.pop("normalization")
        base.update({
            "round_target": 2,
            "round_cap": 5,
            "rounds_run": 2,
            "rounds": [
                {"round": 1, "factual_corrections": 1},
                {"round": 2, "factual_corrections": 0},
            ],
            "stopping_reason": "target_complete",
            "changed_claim_ids": [],
            "rereviewed_claim_ids": [],
            "unresolved_blocking_item_ids": [],
            "unresolved_conclusion_critical_item_ids": [],
            "unreviewed_sections": [],
        })
        validate_run(base)

        for field, value in (("round_target", 1), ("round_cap", 4)):
            mutated = copy.deepcopy(base)
            mutated[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_run(mutated)

        at_cap = copy.deepcopy(base)
        at_cap.update({
            "rounds_run": 5,
            "stopping_reason": "cap_with_unresolved",
            "stopping_reason_detail": "One blocking claim remains unresolved.",
            "unresolved_blocking_item_ids": ["F9"],
        })
        validate_run(at_cap)
        at_cap["unresolved_blocking_item_ids"] = []
        with self.assertRaises(ValidationError):
            validate_run(at_cap)

        converged = copy.deepcopy(base)
        converged.update({
            "rounds_run": 3,
            "rounds": [
                {"round": 1, "factual_corrections": 1},
                {"round": 2, "factual_corrections": 1},
                {"round": 3, "factual_corrections": 0},
            ],
            "stopping_reason": "converged",
        })
        validate_run(converged)
        converged["rounds"][-1]["factual_corrections"] = None
        with self.assertRaises(ValidationError):
            validate_run(converged)


class NormalizationTests(unittest.TestCase):
    def test_comment_normalization_is_deterministic_and_non_promotable_without_v1_hashes(self) -> None:
        source = load(SCHEMAS / "fixtures" / "v1_minimal_comment.json")
        first = normalize_comment_v1(source)
        second = normalize_comment_v1(source)
        self.assertEqual(first, second)
        validate_findings_registry(first)
        row = first["rows"][0]
        self.assertEqual(row["status"], "draft")
        self.assertFalse(row["coordinator_approved"])
        self.assertEqual(row["evidence"]["source_hashes"], [])
        self.assertIsNone(row["dependencies"]["environment_fingerprint"])
        self.assertEqual(row["domain"], "other")
        self.assertEqual(row["source_location"]["path"], "work/check_primary.txt")
        self.assertEqual(row["dependencies"]["finding_ids"], [])
        self.assertEqual(row["dependencies"]["artifact_paths"], ["report/01_reproduction.md"])
        self.assertEqual(row["verifications"], [])
        self.assertIn("rows[*].verifications[*].invocation_id", first["normalization"]["fields_unavailable"])
        self.assertIn("rows[*].verifications[*].evidence_record", first["normalization"]["fields_unavailable"])

    def test_run_normalization_is_deterministic_and_does_not_invent_v1_telemetry(self) -> None:
        source = load(SCHEMAS / "fixtures" / "v1_minimal_run.json")
        first = normalize_run_v1(source)
        second = normalize_run_v1(source)
        self.assertEqual(first, second)
        validate_run(first)
        self.assertEqual(first["stopping_reason"], "cap_with_unresolved")
        self.assertIsNone(first["round_target"])
        invocation = first["invocations"][0]
        self.assertEqual(invocation["reasoning_effort"], "unknown")
        self.assertTrue(invocation["provider_call"])
        self.assertEqual(invocation["cache_usage"], "none")
        self.assertIsNone(invocation["session_id"])
        self.assertIsNone(invocation["error_class"])
        self.assertIsNone(invocation["prompt_sha256"])
        self.assertEqual(invocation["usage"]["total_tokens"], 120)

    def test_missing_scientific_field_fails_instead_of_being_invented(self) -> None:
        source = load(SCHEMAS / "fixtures" / "v1_minimal_comment.json")
        source["findings"][0]["confidence"] = None
        with self.assertRaises(NormalizationError):
            normalize_comment_v1(source)

    def test_comment_normalization_redacts_unsafe_host_paths(self) -> None:
        source = load(SCHEMAS / "fixtures" / "v1_minimal_comment.json")
        posix_host_path = "/" + "Users" + "/private-name/source.csv"
        windows_host_path = "C:" + "\\" + "Users" + "\\private-name\\audit.md"
        source["findings"][0]["evidence"]["artifact"] = posix_host_path
        source["findings"][0]["provenance"]["auditor_report"] = windows_host_path
        normalized = normalize_comment_v1(source)
        serialized = json.dumps(normalized)
        self.assertNotIn(posix_host_path, serialized)
        self.assertNotIn(windows_host_path, serialized)
        self.assertEqual(normalized["rows"][0]["source_location"]["path"], "legacy/unlocated-source")


if __name__ == "__main__":
    unittest.main()
