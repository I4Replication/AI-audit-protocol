#!/usr/bin/env python3
"""Semantic validation for I4R v2 interfaces, using only the Python standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


IMPACT_RANK = {
    "none": 0,
    "unverifiable": 1,
    "value_wrong": 2,
    "conclusion_narrowed": 3,
    "conclusion_unsupported": 4,
    "conclusion_contradicted": 5,
    "artefact_of_construction": 6,
}
CONFIDENCE = {"CONFIRMED", "LIKELY", "SPECULATIVE"}
DOMAINS = {"statistics", "data", "text", "code_methods", "privacy_archive", "framing", "other"}
CACHE_USAGE = {"none", "read", "write", "read_write", "probe_cache", "not_applicable"}
ERROR_CLASSES = {
    "transient", "permanent", "schema", "authentication", "authorization",
    "quota", "model", "unsupported_model", "capability", "deterministic_schema",
    "throttling", "provider_5xx", "server_5xx", "timeout", "transport",
    # A provider-side content-safety classifier refused the request. Not a skill gate,
    # never retried with identical content; the mitigation is stewardship framing.
    "provider_safety_refusal",
    # 2.7.4: one response hit the Claude CLI's per-response output cap; retried once.
    "output_limit",
    # The frozen input snapshot changed around a provider call (content, inventory or manifest):
    # a fault of the host, not of the provider or the response. Never retried or repaired.
    "environment",
    "unknown",
}
ATTEMPT_KINDS = {
    "local_preflight", "capability_probe", "schema_admission", "role_inference",
    "output_repair", "cache_replay",
}
STOPPING_REASONS = {
    "target_complete",
    "converged",
    "cap_with_unresolved",
    "aborted",
}


class ValidationError(ValueError):
    pass


def _fail(path: str, message: str) -> None:
    raise ValidationError(f"{path}: {message}")


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "expected object")
    return value


def _required(obj: dict[str, Any], names: tuple[str, ...], path: str) -> None:
    missing = [name for name in names if name not in obj]
    if missing:
        _fail(path, "missing required fields: " + ", ".join(missing))


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "expected non-empty string")
    return value


def _sha256(value: Any, path: str) -> str:
    text = _text(value, path)
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        _fail(path, "expected lowercase SHA-256 hex")
    return text


def _integer(value: Any, path: str, low: int | None = None, high: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(path, "expected integer")
    if low is not None and value < low:
        _fail(path, f"must be >= {low}")
    if high is not None and value > high:
        _fail(path, f"must be <= {high}")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(path, "expected array")
    return value


def _relative_path(value: Any, path: str) -> str:
    text = _text(value, path)
    normalized = text.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized) or ".." in Path(normalized).parts:
        _fail(path, "must be workspace-relative and may not traverse upward")
    return text


def _unique_texts(value: Any, path: str, *, relative_paths: bool = False) -> list[str]:
    rows = _list(value, path)
    seen: set[str] = set()
    for index, item in enumerate(rows):
        item_path = f"{path}[{index}]"
        text = _relative_path(item, item_path) if relative_paths else _text(item, item_path)
        if text in seen:
            _fail(item_path, "duplicate value")
        seen.add(text)
    return rows


def validate_comment_finding(finding: dict[str, Any], path: str = "finding") -> None:
    """Enforce the strict finding semantics layered onto legacy comment.schema.json."""
    _required(finding, ("id", "claim", "impact", "verification_tier", "evidence", "confidence"), path)
    _text(finding["id"], f"{path}.id")
    _text(finding["claim"], f"{path}.claim")
    impact = finding["impact"]
    if impact not in IMPACT_RANK:
        _fail(f"{path}.impact", "unsupported or null impact")
    _integer(finding["verification_tier"], f"{path}.verification_tier", 1, 5)
    if finding["confidence"] not in CONFIDENCE:
        _fail(f"{path}.confidence", "unsupported or null confidence")
    evidence = _object(finding["evidence"], f"{path}.evidence")
    _required(evidence, ("artifact", "operation"), f"{path}.evidence")
    _text(evidence["artifact"], f"{path}.evidence.artifact")
    _text(evidence["operation"], f"{path}.evidence.operation")
    if impact == "unverifiable":
        _text(finding.get("unverifiable_reason"), f"{path}.unverifiable_reason")
        _integer(finding.get("blocked_tier"), f"{path}.blocked_tier", 1, 5)
        _text(finding.get("unverifiable_basis"), f"{path}.unverifiable_basis")


def _validate_registry_evidence(evidence: Any, path: str) -> list[dict[str, Any]]:
    obj = _object(evidence, path)
    _required(obj, ("source_hashes", "rerun_command", "expected", "observed", "tolerance_basis"), path)
    hashes = _list(obj["source_hashes"], f"{path}.source_hashes")
    for index, source in enumerate(hashes):
        row = _object(source, f"{path}.source_hashes[{index}]")
        _required(row, ("path", "sha256"), f"{path}.source_hashes[{index}]")
        _relative_path(row["path"], f"{path}.source_hashes[{index}].path")
        digest = _text(row["sha256"], f"{path}.source_hashes[{index}].sha256")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            _fail(f"{path}.source_hashes[{index}].sha256", "expected lowercase SHA-256 hex")
    _text(obj["rerun_command"], f"{path}.rerun_command")
    return hashes


# Maximum of the operator attempt knob (MAX_TRIES / I4R_MAX_ATTEMPTS); the policy default is 3.
ATTEMPT_CEILING_MAX = 20
# The round caps a live v2 run may carry (config/check_policy_drift.py pins this against the policy):
# 2.6.8 and earlier froze 5 into every audit's snapshot, 2.6.9 ships 7, and an older audit reaches 7
# only through a journaled amendment (scripts/amend_round_cap.py), recorded in run.policy_amendments.
LIVE_ROUND_TARGET = 2
LIVE_ROUND_CAPS = (5, 7)

def validate_finding_row(finding: dict[str, Any], path: str = "finding_row") -> None:
    finding = _object(finding, path)
    _required(finding, ("id", "claim", "domain", "source_location", "status", "ledger_disposition", "coordinator_approved", "impact", "impact_rank", "verification_tier", "paper_only", "confidence", "evidence", "unverifiable_basis", "verifications", "dependencies", "settled", "disclosure_flag", "remedy", "changes_conclusion", "changes_conclusion_which", "provenance"), path)
    finding_id = _text(finding["id"], f"{path}.id")
    _text(finding["claim"], f"{path}.claim")
    if finding["domain"] not in DOMAINS:
        _fail(f"{path}.domain", "unsupported domain")
    source_location = _object(finding["source_location"], f"{path}.source_location")
    _required(source_location, ("path", "locator"), f"{path}.source_location")
    _relative_path(source_location["path"], f"{path}.source_location.path")
    _text(source_location["locator"], f"{path}.source_location.locator")
    status = finding["status"]
    if status not in {"draft", "confirmed", "verified", "accepted", "rejected", "superseded"}:
        _fail(f"{path}.status", "unsupported status")
    disposition = _object(finding["ledger_disposition"], f"{path}.ledger_disposition")
    _required(disposition, ("status", "target", "reason"), f"{path}.ledger_disposition")
    if disposition["status"] not in {"INCLUDED", "MERGED", "EXCLUDED"}:
        _fail(f"{path}.ledger_disposition.status", "unsupported disposition")
    if disposition["status"] == "MERGED":
        _text(disposition["target"], f"{path}.ledger_disposition.target")
    if disposition["status"] == "EXCLUDED":
        _text(disposition["reason"], f"{path}.ledger_disposition.reason")
    if not isinstance(finding["coordinator_approved"], bool):
        _fail(f"{path}.coordinator_approved", "expected boolean")
    impact = finding["impact"]
    if impact not in IMPACT_RANK:
        _fail(f"{path}.impact", "unsupported or null impact")
    rank = _integer(finding["impact_rank"], f"{path}.impact_rank", 0, 6)
    if rank != IMPACT_RANK[impact]:
        _fail(f"{path}.impact_rank", f"expected {IMPACT_RANK[impact]} for impact {impact}")
    _integer(finding["verification_tier"], f"{path}.verification_tier", 1, 5)
    if finding["confidence"] not in CONFIDENCE:
        _fail(f"{path}.confidence", "unsupported or null confidence")
    source_hashes = _validate_registry_evidence(finding["evidence"], f"{path}.evidence")
    basis = finding["unverifiable_basis"]
    if impact == "unverifiable":
        basis_obj = _object(basis, f"{path}.unverifiable_basis")
        _required(basis_obj, ("reason", "blocked_tier", "attempted_check", "basis"), f"{path}.unverifiable_basis")
        _text(basis_obj["reason"], f"{path}.unverifiable_basis.reason")
        _integer(basis_obj["blocked_tier"], f"{path}.unverifiable_basis.blocked_tier", 1, 5)
        _text(basis_obj["attempted_check"], f"{path}.unverifiable_basis.attempted_check")
        _text(basis_obj["basis"], f"{path}.unverifiable_basis.basis")
    elif basis is not None:
        _fail(f"{path}.unverifiable_basis", "must be null unless impact is unverifiable")
    verifications = _list(finding["verifications"], f"{path}.verifications")
    confirmed_reviewers: set[str] = set()
    confirmed_invocations: set[str] = set()
    confirmed_evidence_records: set[tuple[str, str]] = set()
    confirmed_families: set[str] = set()
    all_reviewers: set[str] = set()
    for index, verification in enumerate(verifications):
        row = _object(verification, f"{path}.verifications[{index}]")
        _required(row, ("reviewer_id", "invocation_id", "evidence_record", "model_family", "outcome"),
                  f"{path}.verifications[{index}]")
        reviewer = _text(row["reviewer_id"], f"{path}.verifications[{index}].reviewer_id")
        if reviewer in all_reviewers:
            _fail(f"{path}.verifications[{index}].reviewer_id", "duplicate reviewer identity")
        all_reviewers.add(reviewer)
        invocation_id = _text(row["invocation_id"], f"{path}.verifications[{index}].invocation_id")
        evidence_record = _object(row["evidence_record"],
                                  f"{path}.verifications[{index}].evidence_record")
        _required(evidence_record, ("path", "sha256"),
                  f"{path}.verifications[{index}].evidence_record")
        evidence_path = _relative_path(evidence_record["path"],
                                       f"{path}.verifications[{index}].evidence_record.path")
        evidence_hash = _text(evidence_record["sha256"],
                              f"{path}.verifications[{index}].evidence_record.sha256")
        if len(evidence_hash) != 64 or any(char not in "0123456789abcdef" for char in evidence_hash):
            _fail(f"{path}.verifications[{index}].evidence_record.sha256",
                  "expected lowercase SHA-256 hex")
        family = _text(row["model_family"], f"{path}.verifications[{index}].model_family")
        if row["outcome"] not in {"confirmed", "pass", "verified", "refuted", "inconclusive"}:
            _fail(f"{path}.verifications[{index}].outcome", "unsupported outcome")
        if row["outcome"] in {"confirmed", "pass", "verified"}:
            confirmed_reviewers.add(reviewer)
            confirmed_invocations.add(invocation_id)
            confirmed_evidence_records.add((evidence_path, evidence_hash))
            confirmed_families.add(family.strip().lower())
    dependencies = _object(finding["dependencies"], f"{path}.dependencies")
    _required(dependencies, ("environment_fingerprint", "finding_ids", "artifact_paths"), f"{path}.dependencies")
    dependency_ids = _unique_texts(dependencies["finding_ids"], f"{path}.dependencies.finding_ids")
    if finding_id in dependency_ids:
        _fail(f"{path}.dependencies.finding_ids", "finding may not depend on itself")
    _unique_texts(dependencies["artifact_paths"], f"{path}.dependencies.artifact_paths", relative_paths=True)
    promotable = status in {"confirmed", "verified", "accepted"} and finding["coordinator_approved"]
    requires_independent_confirmation = promotable or finding["changes_conclusion"] is True
    if requires_independent_confirmation:
        origin = str(((finding.get("provenance") or {}).get("origin_role")) or "")
        found_by = (finding.get("provenance") or {}).get("independently_found_by") or []
        coordinator_hint = ("" if origin.startswith("auditor") or any(str(r).startswith("auditor") for r in found_by)
                            else " (no Phase-1 auditor raised this row: a finding raised after fan-out gets its"
                                 " Sonnet-family confirmation only from python3 scripts/run_verifier.py <audit-dir>"
                                 " --findings <id>, run before this integration; see INSTALL.md, 'Confirming findings"
                                 " raised after fan-out', and reference/adjudication_guide.md,"
                                 " 'Findings the coordinator raises')")
        if len(confirmed_reviewers) < 2:
            _fail(f"{path}.verifications", "promotable or conclusion-changing row requires two confirmed reviewers" + coordinator_hint)
        if len(confirmed_invocations) < 2:
            _fail(f"{path}.verifications", "confirmations must bind two distinct invocation IDs")
        if len(confirmed_evidence_records) < 2:
            _fail(f"{path}.verifications", "confirmations must bind two distinct evidence records")
        if len(confirmed_families) < 2 or confirmed_families & {"unknown", "unavailable"}:
            _fail(f"{path}.verifications", "confirmations must span two known model families" + coordinator_hint)
    if promotable:
        if not source_hashes:
            _fail(f"{path}.evidence.source_hashes", "approved confirmed row requires source hashes")
        _text(dependencies["environment_fingerprint"], f"{path}.dependencies.environment_fingerprint")
        _required(finding, ("benign_explanations_ruled_out", "report_section", "verification_snippet"), path)
        for index, explanation in enumerate(_list(finding["benign_explanations_ruled_out"], f"{path}.benign_explanations_ruled_out")):
            _text(explanation, f"{path}.benign_explanations_ruled_out[{index}]")
        section = _text(finding["report_section"], f"{path}.report_section")
        if not re.fullmatch(r"Section [0-9]+(?:\.[0-9]+)?", section):
            _fail(f"{path}.report_section", "expected 'Section N' or 'Section N.N'")
        snippet = _object(finding["verification_snippet"], f"{path}.verification_snippet")
        _required(snippet, ("kind", "path", "basis"), f"{path}.verification_snippet")
        if snippet["kind"] not in {"code", "judgement", "unverifiable"}:
            _fail(f"{path}.verification_snippet.kind", "unsupported snippet kind")
        _relative_path(snippet["path"], f"{path}.verification_snippet.path")
        _text(snippet["basis"], f"{path}.verification_snippet.basis")
    if finding["changes_conclusion"]:
        _text(finding["changes_conclusion_which"], f"{path}.changes_conclusion_which")
    provenance = _object(finding["provenance"], f"{path}.provenance")
    _required(provenance, ("origin_role", "independently_found_by", "agreed_after_seeing"), f"{path}.provenance")
    origin = _text(provenance["origin_role"], f"{path}.provenance.origin_role")
    independent = set(_unique_texts(provenance["independently_found_by"],
                                    f"{path}.provenance.independently_found_by"))
    exposed = set(_unique_texts(provenance["agreed_after_seeing"],
                                f"{path}.provenance.agreed_after_seeing"))
    if origin not in independent:
        _fail(f"{path}.provenance.independently_found_by", "must include origin_role")
    if independent & exposed:
        _fail(f"{path}.provenance", "independent and post-exposure roles must be disjoint")


def validate_findings_registry(value: dict[str, Any]) -> None:
    obj = _object(value, "registry")
    _required(obj, ("schema_version", "audit_id", "policy_version", "source_comment_sha256", "report_sha256", "generated_at", "rows", "normalization"), "registry")
    if obj["schema_version"] != "2.0.0":
        _fail("registry.schema_version", "must equal 2.0.0")
    _text(obj["audit_id"], "registry.audit_id")
    _text(obj["policy_version"], "registry.policy_version")
    if not isinstance(obj["source_comment_sha256"], str) or len(obj["source_comment_sha256"]) != 64:
        _fail("registry.source_comment_sha256", "expected SHA-256 hex digest")
    findings = _list(obj["rows"], "registry.rows")
    finding_ids: set[str] = set()
    for index, finding_value in enumerate(findings):
        path = f"registry.rows[{index}]"
        finding = _object(finding_value, path)
        validate_finding_row(finding, path)
        finding_id = finding["id"]
        if finding_id in finding_ids:
            _fail(f"{path}.id", "duplicate finding ID")
        finding_ids.add(finding_id)


def _validate_artifact_binding(value: Any, path: str) -> tuple[str, str]:
    binding = _object(value, path)
    _required(binding, ("path", "sha256"), path)
    if set(binding) != {"path", "sha256"}:
        _fail(path, "artifact binding permits only path and sha256")
    artifact_path = _relative_path(binding["path"], f"{path}.path")
    artifact_sha = _sha256(binding["sha256"], f"{path}.sha256")
    return artifact_path, artifact_sha


def _validate_critic_evidence(evidence: Any, path: str) -> None:
    obj = _object(evidence, path)
    _required(obj, ("artifact_refs", "operation", "result", "reproducible_script"), path)
    refs = _list(obj["artifact_refs"], f"{path}.artifact_refs")
    if not refs:
        _fail(f"{path}.artifact_refs", "must not be empty")
    seen_refs: set[str] = set()
    for index, ref in enumerate(refs):
        ref_path, _ = _validate_artifact_binding(ref, f"{path}.artifact_refs[{index}]")
        normalized = ref_path.replace("\\", "/").casefold()
        if normalized in seen_refs:
            _fail(f"{path}.artifact_refs[{index}]", "duplicate artifact reference")
        seen_refs.add(normalized)
    _text(obj["operation"], f"{path}.operation")
    _text(obj["result"], f"{path}.result")
    if obj["reproducible_script"] is not None:
        _validate_artifact_binding(obj["reproducible_script"],
                                   f"{path}.reproducible_script")


def _validate_assessment(value: Any, path: str, new_issue: bool) -> str:
    item = _object(value, path)
    _required(item, ("item_id", "finding_id", "claim", "verdict", "priority", "confidence", "evidence", "unverifiable_basis", "requested_correction"), path)
    item_id = _text(item["item_id"], f"{path}.item_id")
    if new_issue and item["finding_id"] is not None:
        _fail(f"{path}.finding_id", "new issues must use null finding_id")
    if not new_issue:
        _text(item["finding_id"], f"{path}.finding_id")
    _text(item["claim"], f"{path}.claim")
    verdict = item["verdict"]
    if verdict not in {"CONFIRMED", "REFUTED", "OVERSTATED", "UNVERIFIABLE"}:
        _fail(f"{path}.verdict", "unsupported verdict")
    if item["priority"] not in {"BLOCKING", "NON_BLOCKING"}:
        _fail(f"{path}.priority", "unsupported priority")
    if item["confidence"] not in CONFIDENCE:
        _fail(f"{path}.confidence", "unsupported confidence")
    _validate_critic_evidence(item["evidence"], f"{path}.evidence")
    if verdict == "UNVERIFIABLE":
        basis = _object(item["unverifiable_basis"], f"{path}.unverifiable_basis")
        _required(basis, ("missing_or_inaccessible", "attempted", "effect_on_verdict"), f"{path}.unverifiable_basis")
        for key in ("missing_or_inaccessible", "attempted", "effect_on_verdict"):
            _text(basis[key], f"{path}.unverifiable_basis.{key}")
    elif item["unverifiable_basis"] is not None:
        _fail(f"{path}.unverifiable_basis", "must be null unless verdict is UNVERIFIABLE")
    if verdict in {"REFUTED", "OVERSTATED"}:
        _text(item["requested_correction"], f"{path}.requested_correction")
    elif item["requested_correction"] is not None:
        _text(item["requested_correction"], f"{path}.requested_correction")
    return item_id


def validate_critic_result(value: dict[str, Any]) -> None:
    obj = _object(value, "critic_result")
    _required(obj, ("schema_version", "audit_id", "invocation_id", "round", "role", "report_sha256", "input_hashes", "status", "completion_marker", "completeness_declaration", "coverage", "covered_claim_ids", "conflicts", "settled_spot_checks", "items", "new_issues", "overall"), "critic_result")
    if obj["schema_version"] != "2.0.0":
        _fail("critic_result.schema_version", "must equal 2.0.0")
    _text(obj["audit_id"], "critic_result.audit_id")
    invocation_id = _text(obj["invocation_id"], "critic_result.invocation_id")
    if len(invocation_id) < 8 or len(invocation_id) > 240 or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]*", invocation_id):
        _fail("critic_result.invocation_id", "must be a bounded machine-safe identifier")
    _integer(obj["round"], "critic_result.round", 1)
    if obj["role"] not in {
            "critic_stats", "critic_data", "critic_fairness", "critic_completeness",
            "critic_synth"}:
        _fail("critic_result.role", "unsupported critic role")
    report_sha = _sha256(obj["report_sha256"], "critic_result.report_sha256")
    input_hashes = _object(obj["input_hashes"], "critic_result.input_hashes")
    hash_names = ("snapshot_sha256", "report_sha256", "prompt_sha256", "policy_sha256",
                  "evidence_manifest_sha256")
    _required(input_hashes, hash_names, "critic_result.input_hashes")
    for name in hash_names:
        _sha256(input_hashes[name], f"critic_result.input_hashes.{name}")
    if input_hashes["report_sha256"] != report_sha:
        _fail("critic_result.input_hashes.report_sha256", "must equal report_sha256")
    status = obj["status"]
    if status not in {"partial", "complete", "failed"}:
        _fail("critic_result.status", "unsupported status")
    if obj["completion_marker"] is not (status == "complete"):
        _fail("critic_result.completion_marker", "must be true exactly when status is complete")
    declaration = _object(obj["completeness_declaration"],
                          "critic_result.completeness_declaration")
    declaration_fields = ("all_assigned_checks_accounted_for", "all_assessments_evidenced",
                          "unresolved_omissions")
    _required(declaration, declaration_fields, "critic_result.completeness_declaration")
    for name in declaration_fields[:2]:
        if not isinstance(declaration[name], bool):
            _fail(f"critic_result.completeness_declaration.{name}", "expected boolean")
    omissions = _unique_texts(declaration["unresolved_omissions"],
                              "critic_result.completeness_declaration.unresolved_omissions")
    declaration_is_complete = (declaration["all_assigned_checks_accounted_for"] and
                               declaration["all_assessments_evidenced"] and not omissions)
    if declaration_is_complete is not (status == "complete"):
        _fail("critic_result.completeness_declaration",
              "must declare complete exactly when status is complete")
    coverage = _object(obj["coverage"], "critic_result.coverage")
    _required(coverage, ("assigned_checks", "completed_checks", "skipped_checks"), "critic_result.coverage")
    assigned = _unique_texts(coverage["assigned_checks"], "critic_result.coverage.assigned_checks")
    if not assigned:
        _fail("critic_result.coverage.assigned_checks", "must not be empty")
    completed = _unique_texts(coverage["completed_checks"], "critic_result.coverage.completed_checks")
    skipped = _list(coverage["skipped_checks"], "critic_result.coverage.skipped_checks")
    if skipped:
        _fail("critic_result.coverage.skipped_checks",
              "skipped checks are unsupported and cannot count as completed review")
    skipped_names = []
    for index, entry in enumerate(skipped):
        row = _object(entry, f"critic_result.coverage.skipped_checks[{index}]")
        _required(row, ("check", "reason"), f"critic_result.coverage.skipped_checks[{index}]")
        skipped_names.append(_text(row["check"], f"critic_result.coverage.skipped_checks[{index}].check"))
        _text(row["reason"], f"critic_result.coverage.skipped_checks[{index}].reason")
    if len(skipped_names) != len(set(skipped_names)):
        _fail("critic_result.coverage.skipped_checks", "duplicate skipped check")
    assigned_set, completed_set, skipped_set = set(assigned), set(completed), set(skipped_names)
    if not completed_set <= assigned_set or not skipped_set <= assigned_set:
        _fail("critic_result.coverage", "completed and skipped checks must be assigned")
    if completed_set & skipped_set:
        _fail("critic_result.coverage", "a check may not be both completed and skipped")
    if status == "complete" and not completed:
        _fail("critic_result.coverage.completed_checks", "complete output requires at least one completed check")
    if status == "complete" and assigned_set != completed_set | skipped_set:
        _fail("critic_result.coverage", "complete output must account for every assigned check")
    covered_claims = _unique_texts(obj["covered_claim_ids"],
                                  "critic_result.covered_claim_ids")
    covered_set = set(covered_claims)
    conflict_ids: set[str] = set()
    for index, conflict in enumerate(_list(obj["conflicts"], "critic_result.conflicts")):
        path = f"critic_result.conflicts[{index}]"
        row = _object(conflict, path)
        _required(row, ("conflict_id", "claim_ids", "description"), path)
        conflict_id = _text(row["conflict_id"], f"{path}.conflict_id")
        if conflict_id.casefold() in conflict_ids:
            _fail(f"{path}.conflict_id", "duplicate conflict ID")
        conflict_ids.add(conflict_id.casefold())
        claim_ids = set(_unique_texts(row["claim_ids"], f"{path}.claim_ids"))
        if not claim_ids:
            _fail(f"{path}.claim_ids", "must not be empty")
        if not claim_ids <= covered_set:
            _fail(f"{path}.claim_ids", "conflicts may reference only covered claim IDs")
        _text(row["description"], f"{path}.description")
    spot_ids: set[str] = set()
    for index, spot_check in enumerate(_list(obj["settled_spot_checks"], "critic_result.settled_spot_checks")):
        row = _object(spot_check, f"critic_result.settled_spot_checks[{index}]")
        _required(row, ("fact_id", "status", "evidence"), f"critic_result.settled_spot_checks[{index}]")
        fact_id = _text(row["fact_id"], f"critic_result.settled_spot_checks[{index}].fact_id")
        if fact_id in spot_ids:
            _fail(f"critic_result.settled_spot_checks[{index}].fact_id", "duplicate settled fact spot-check")
        spot_ids.add(fact_id)
        if row["status"] not in {"passed", "failed"}:
            _fail(f"critic_result.settled_spot_checks[{index}].status", "unsupported status")
        _text(row["evidence"], f"critic_result.settled_spot_checks[{index}].evidence")
    item_ids: set[str] = set()
    assessed_claim_ids: list[str] = []
    blocking = 0
    for collection, is_new in (("items", False), ("new_issues", True)):
        for index, item in enumerate(_list(obj[collection], f"critic_result.{collection}")):
            item_id = _validate_assessment(item, f"critic_result.{collection}[{index}]", is_new)
            if item_id in item_ids:
                _fail(f"critic_result.{collection}[{index}].item_id", "duplicate item ID")
            item_ids.add(item_id)
            if not is_new and item["finding_id"] not in covered_set:
                _fail(f"critic_result.{collection}[{index}].finding_id",
                      "assessment finding_id must be declared in covered_claim_ids")
            if not is_new:
                assessed_claim_ids.append(item["finding_id"])
            if item["priority"] == "BLOCKING":
                blocking += 1
    duplicate_assessments = sorted({claim_id for claim_id in assessed_claim_ids
                                    if assessed_claim_ids.count(claim_id) > 1})
    if duplicate_assessments:
        _fail("critic_result.items",
              "each covered claim requires exactly one assessment; duplicate finding_id(s): "
              + ", ".join(duplicate_assessments))
    assessed_set = set(assessed_claim_ids)
    if assessed_set != covered_set:
        missing = sorted(covered_set - assessed_set)
        extra = sorted(assessed_set - covered_set)
        details = []
        if missing:
            details.append("missing assessments for " + ", ".join(missing))
        if extra:
            details.append("uncovered assessments for " + ", ".join(extra))
        _fail("critic_result.items",
              "covered_claim_ids must have exactly one claim-level assessment"
              + (": " + "; ".join(details) if details else ""))
    overall = _object(obj["overall"], "critic_result.overall")
    _required(overall, ("publishable", "blocking_count", "summary"), "critic_result.overall")
    if _integer(overall["blocking_count"], "critic_result.overall.blocking_count", 0) != blocking:
        _fail("critic_result.overall.blocking_count", f"expected {blocking}")
    if bool(overall["publishable"]) != (blocking == 0):
        _fail("critic_result.overall.publishable", "must be false exactly when blocking items exist")
    _text(overall["summary"], "critic_result.overall.summary")


def validate_invocation(value: dict[str, Any], path: str = "invocation") -> None:
    obj = _object(value, path)
    required = ("invocation_id", "role", "role_class", "round", "attempt", "status", "provider", "provider_call", "cache_usage", "session_id", "requested_model", "observed_model", "model_family", "model_source", "reasoning_effort", "started_at", "ended_at", "elapsed_seconds", "input_artifact_sha256", "prompt_sha256", "output_artifact", "output_sha256", "usage", "error", "error_class", "legacy_source")
    _required(obj, required, path)
    _text(obj["invocation_id"], f"{path}.invocation_id")
    _text(obj["role"], f"{path}.role")
    if obj["attempt"] is not None:
        _integer(obj["attempt"], f"{path}.attempt", 1)
    status = obj["status"]
    if status not in {"started", "completed", "failed", "cancelled", "timed_out", "legacy_record"}:
        _fail(f"{path}.status", "unsupported status")
    if not isinstance(obj["provider_call"], bool):
        _fail(f"{path}.provider_call", "expected boolean")
    attempt_kind = obj.get("attempt_kind")
    if attempt_kind is not None and attempt_kind not in ATTEMPT_KINDS:
        _fail(f"{path}.attempt_kind", "unsupported attempt kind")
    provider_request_status = obj.get("provider_request_status")
    if provider_request_status is not None and provider_request_status not in {
        "not_requested", "requested", "accepted", "rejected", "unknown",
    }:
        _fail(f"{path}.provider_request_status", "unsupported provider-request status")
    inference_start_status = obj.get("inference_start_status")
    if inference_start_status is not None and inference_start_status not in {
        "not_started", "started", "unknown",
    }:
        _fail(f"{path}.inference_start_status", "unsupported inference-start status")
    if inference_start_status == "started" and obj["attempt"] is None:
        _fail(f"{path}.attempt", "accepted inference start requires an allocated attempt")
    if inference_start_status == "not_started" and obj["attempt"] is not None:
        _fail(f"{path}.attempt", "non-started inference cannot consume an attempt")
    if attempt_kind == "cache_replay":
        if obj["provider_call"] or not obj.get("source_invocation_id"):
            _fail(path, "cache replay requires provider_call=false and source_invocation_id")
    if obj["cache_usage"] not in CACHE_USAGE:
        _fail(f"{path}.cache_usage", "unsupported cache disposition")
    if obj["provider_call"]:
        if obj["cache_usage"] == "not_applicable":
            _fail(f"{path}.cache_usage", "provider calls may not use not_applicable")
    elif obj["cache_usage"] not in {"read", "probe_cache", "not_applicable"}:
        _fail(
            f"{path}.cache_usage",
            "non-provider records require read, probe_cache, or not_applicable",
        )
    if obj["session_id"] is not None:
        _text(obj["session_id"], f"{path}.session_id")
    if "observed_model_ids" in obj:
        model_ids = _list(obj["observed_model_ids"], f"{path}.observed_model_ids")
        if not model_ids:
            _fail(f"{path}.observed_model_ids", "must contain at least one served model ID")
        seen_model_ids: set[str] = set()
        for index, model_id in enumerate(model_ids):
            served = _text(model_id, f"{path}.observed_model_ids[{index}]")
            if served in seen_model_ids:
                _fail(f"{path}.observed_model_ids[{index}]", "duplicate served model ID")
            seen_model_ids.add(served)
        primary_model = obj.get("observed_model")
        if not isinstance(primary_model, str) or primary_model not in seen_model_ids:
            _fail(
                f"{path}.observed_model_ids",
                "must include observed_model as the primary served model",
            )
    served_model = obj.get("served_model", obj.get("observed_model"))
    assurance = obj.get("identity_assurance")
    if assurance is not None and assurance not in {"observed", "requested_only", "unavailable"}:
        _fail(f"{path}.identity_assurance", "unsupported identity assurance")
    if assurance == "observed" and not isinstance(served_model, str):
        _fail(f"{path}.served_model", "observed assurance requires served identity")
    if assurance == "requested_only" and served_model is not None:
        _fail(f"{path}.served_model", "requested-only assurance requires null served identity")
    if obj.get("served_model") is not None and obj.get("observed_model") not in {
        None, obj.get("served_model"),
    }:
        _fail(f"{path}.observed_model", "legacy alias disagrees with served_model")
    if obj["error_class"] is not None and obj["error_class"] not in ERROR_CLASSES:
        _fail(f"{path}.error_class", "unsupported error class")
    effort = obj["reasoning_effort"]
    if effort not in {"low", "medium", "high", "xhigh", "max", "ultra", "provider_default", "unknown"}:
        _fail(f"{path}.reasoning_effort", "unsupported effort")
    usage = _object(obj["usage"], f"{path}.usage")
    legacy_token_fields = (
        "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens",
    )
    component_fields = (
        "input_tokens", "output_tokens", "cached_input_tokens",
        "cache_creation_input_tokens", "reasoning_tokens",
    )
    extension_fields = (
        "cache_creation_input_tokens", "unavailable_reasons", "total_tokens_kind",
        "total_tokens_components", "total_tokens_unavailable_reason",
    )
    _required(usage, ("obtainable", *legacy_token_fields, "source"), f"{path}.usage")
    if not isinstance(usage["obtainable"], bool):
        _fail(f"{path}.usage.obtainable", "expected boolean")

    extension_present = any(name in usage for name in extension_fields)
    if extension_present:
        _required(usage, extension_fields, f"{path}.usage")
        reasons = _object(usage["unavailable_reasons"], f"{path}.usage.unavailable_reasons")
        extra_reasons = sorted(set(reasons) - set(component_fields))
        if extra_reasons:
            _fail(
                f"{path}.usage.unavailable_reasons",
                "unsupported component reason(s): " + ", ".join(extra_reasons),
            )
        observed_components: set[str] = set()
        for name in component_fields:
            count = usage[name]
            reason = reasons.get(name)
            if count is None:
                _text(reason, f"{path}.usage.unavailable_reasons.{name}")
            else:
                _integer(count, f"{path}.usage.{name}", 0)
                observed_components.add(name)
                if reason is not None:
                    _fail(
                        f"{path}.usage.unavailable_reasons.{name}",
                        "must be absent when the component is observed",
                    )

        total = usage["total_tokens"]
        if total is not None:
            _integer(total, f"{path}.usage.total_tokens", 0)
        kind = usage["total_tokens_kind"]
        if kind not in {"provider_reported", "derived", "unavailable"}:
            _fail(f"{path}.usage.total_tokens_kind", "unsupported total-token provenance")
        components = _list(
            usage["total_tokens_components"], f"{path}.usage.total_tokens_components"
        )
        named_components: list[str] = []
        for index, component in enumerate(components):
            name = _text(component, f"{path}.usage.total_tokens_components[{index}]")
            if name not in component_fields:
                _fail(
                    f"{path}.usage.total_tokens_components[{index}]",
                    "unsupported token component",
                )
            named_components.append(name)
        if len(named_components) != len(set(named_components)):
            _fail(f"{path}.usage.total_tokens_components", "components must be unique")
        unavailable_reason = usage["total_tokens_unavailable_reason"]
        if kind == "provider_reported":
            if total is None or named_components or unavailable_reason is not None:
                _fail(
                    f"{path}.usage",
                    "provider-reported total requires a numeric total, no components, and no unavailable reason",
                )
        elif kind == "derived":
            if total is None or not named_components or unavailable_reason is not None:
                _fail(
                    f"{path}.usage",
                    "derived total requires a numeric total, named components, and no unavailable reason",
                )
            missing = [name for name in named_components if name not in observed_components]
            if missing:
                _fail(
                    f"{path}.usage.total_tokens_components",
                    "derived total names unavailable component(s): " + ", ".join(missing),
                )
            expected_total = sum(usage[name] for name in named_components)
            if total != expected_total:
                _fail(
                    f"{path}.usage.total_tokens",
                    f"derived total {total} does not equal named-component sum {expected_total}",
                )
        else:
            if total is not None or named_components:
                _fail(
                    f"{path}.usage",
                    "unavailable total requires total_tokens=null and no components",
                )
            _text(unavailable_reason, f"{path}.usage.total_tokens_unavailable_reason")

        any_observed = bool(observed_components) or total is not None
        if usage["obtainable"] != any_observed:
            _fail(
                f"{path}.usage.obtainable",
                "must be true exactly when at least one token count is observed",
            )
        if any_observed and usage["source"] == "unavailable":
            _fail(f"{path}.usage.source", "observed usage may not use source=unavailable")
        if not any_observed and usage["source"] != "unavailable":
            _fail(f"{path}.usage.source", "fully unavailable usage requires source=unavailable")
        if status == "completed" and obj["provider_call"]:
            if usage["input_tokens"] is None or usage["output_tokens"] is None:
                _fail(f"{path}.usage", "completed invocation requires observed input and output tokens")
            if kind == "unavailable":
                _fail(f"{path}.usage", "completed invocation requires a provider-reported or derived total")
    else:
        # Backward-compatible all-or-nothing v2 shape. New partial records must use the
        # provenance extension above; old immutable ledgers remain valid without rewriting.
        counts = [usage[name] for name in legacy_token_fields]
        for name, count in zip(legacy_token_fields, counts):
            if count is not None:
                _integer(count, f"{path}.usage.{name}", 0)
        if usage["obtainable"]:
            if status != "legacy_record" and any(count is None for count in counts):
                _fail(
                    f"{path}.usage",
                    "partial usage requires explicit unavailable reasons and total-token provenance",
                )
            if usage["source"] == "unavailable":
                _fail(f"{path}.usage.source", "cannot be unavailable when usage is obtainable")
        elif any(count is not None for count in counts) or usage["source"] != "unavailable":
            _fail(f"{path}.usage", "unobtainable usage requires null counts and source=unavailable")
    usage_by_model = obj.get("usage_by_model")
    if usage_by_model is not None:
        partitions = _list(usage_by_model, f"{path}.usage_by_model")
        seen_models: set[str] = set()
        for index, partition in enumerate(partitions):
            part = _object(partition, f"{path}.usage_by_model[{index}]")
            _required(part, ("model", "identity_assurance", "usage"), f"{path}.usage_by_model[{index}]")
            model = _text(part["model"], f"{path}.usage_by_model[{index}].model")
            if model in seen_models:
                _fail(f"{path}.usage_by_model[{index}].model", "duplicate model partition")
            seen_models.add(model)
            if part["identity_assurance"] not in {"observed", "requested_only"}:
                _fail(f"{path}.usage_by_model[{index}].identity_assurance", "unsupported assurance")
            _object(part["usage"], f"{path}.usage_by_model[{index}].usage")
    if obj.get("turn_count") is not None:
        _integer(obj["turn_count"], f"{path}.turn_count", 0)
    if obj.get("provider_duration_seconds") is not None:
        duration = obj["provider_duration_seconds"]
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
            _fail(f"{path}.provider_duration_seconds", "expected non-negative number")
    provider_cost = obj.get("provider_cost")
    if provider_cost is not None:
        cost = _object(provider_cost, f"{path}.provider_cost")
        _required(cost, ("amount", "currency", "source", "unavailable_reason"), f"{path}.provider_cost")
        if cost["amount"] is None:
            _text(cost["unavailable_reason"], f"{path}.provider_cost.unavailable_reason")
        elif isinstance(cost["amount"], bool) or not isinstance(cost["amount"], (int, float)) or cost["amount"] < 0:
            _fail(f"{path}.provider_cost.amount", "expected non-negative number or null")
    if status == "completed":
        if assurance == "requested_only":
            _text(obj["requested_model"], f"{path}.requested_model")
        else:
            _text(served_model, f"{path}.served_model")
            if obj["model_source"] == "unavailable":
                _fail(f"{path}.model_source", "completed invocation requires an identity disposition")
        if effort == "unknown":
            _fail(f"{path}.reasoning_effort", "live completed invocation may not use unknown")
        _text(obj["started_at"], f"{path}.started_at")
        _text(obj["ended_at"], f"{path}.ended_at")
        if not isinstance(obj["elapsed_seconds"], (int, float)) or obj["elapsed_seconds"] < 0:
            _fail(f"{path}.elapsed_seconds", "completed invocation requires non-negative elapsed time")
        _relative_path(obj["output_artifact"], f"{path}.output_artifact")
        _text(obj["output_sha256"], f"{path}.output_sha256")


def validate_run(value: dict[str, Any]) -> None:
    obj = _object(value, "run")
    _required(obj, ("schema_version", "audit_id", "round_target", "round_cap", "rounds_run", "stopping_reason", "invocations"), "run")
    if obj["schema_version"] not in {"2.0.0", "2.1.0"}:
        _fail("run.schema_version", "must equal 2.0.0 or 2.1.0")
    metadata_2_1 = obj["schema_version"] == "2.1.0"
    target = obj["round_target"]
    cap = obj["round_cap"]
    if target is not None:
        target = _integer(target, "run.round_target", 1)
    if cap is not None:
        cap = _integer(cap, "run.round_cap", 1)
    if target is not None and cap is not None and target > cap:
        _fail("run.round_target", "must not exceed round_cap")
    normalized_v1 = isinstance(obj.get("normalization"), dict)
    if not normalized_v1 and (target != LIVE_ROUND_TARGET or cap not in LIVE_ROUND_CAPS):
        _fail("run.round_target", f"live v2 policy requires round_target={LIVE_ROUND_TARGET} and round_cap in "
                                  f"{list(LIVE_ROUND_CAPS)} (a raised cap needs a journaled amendment)")
    amendments = obj.get("policy_amendments")
    if amendments is not None:
        # The chain of raises must start at a cap a live audit was initialised with and end at this run's cap;
        # finalize binds each step to the snapshot history and the archived snapshots (round_cap_amendment.py).
        previous = None
        for index, entry_value in enumerate(_list(amendments, "run.policy_amendments")):
            entry = _object(entry_value, f"run.policy_amendments[{index}]")
            _required(entry, ("seq", "at", "leaf", "from", "to", "reason", "snapshot_sha256"),
                      f"run.policy_amendments[{index}]")
            if entry["leaf"] != "critique.round_cap":
                _fail(f"run.policy_amendments[{index}].leaf", "only critique.round_cap may be amended")
            low = _integer(entry["from"], f"run.policy_amendments[{index}].from", 1)
            high = _integer(entry["to"], f"run.policy_amendments[{index}].to", 1)
            _text(entry["reason"], f"run.policy_amendments[{index}].reason")
            if high <= low:
                _fail(f"run.policy_amendments[{index}]", "a round-cap amendment may only raise the cap")
            if not normalized_v1 and (low not in LIVE_ROUND_CAPS or high not in LIVE_ROUND_CAPS):
                _fail(f"run.policy_amendments[{index}]", f"amended caps must be in {list(LIVE_ROUND_CAPS)}")
            if previous is not None and low != previous:
                _fail(f"run.policy_amendments[{index}].from", "amendments must chain: from == the previous to")
            previous = high
        if previous is not None and previous != cap:
            _fail("run.round_cap", "must equal the last round-cap amendment's 'to'")
    rounds_run = obj["rounds_run"]
    if rounds_run is not None:
        rounds_run = _integer(rounds_run, "run.rounds_run", 0)
        if cap is not None and rounds_run > cap:
            _fail("run.rounds_run", "must not exceed round_cap")
    reason = obj["stopping_reason"]
    if reason is not None and reason not in STOPPING_REASONS:
        _fail("run.stopping_reason", "unsupported v2 stopping reason")
    if reason == "cap_with_unresolved":
        if rounds_run != cap:
            _fail("run.stopping_reason", "cap_with_unresolved requires rounds_run == round_cap")
        _text(obj.get("stopping_reason_detail"), "run.stopping_reason_detail")
        unresolved_scope = []
        for field in ("unreviewed_sections", "unresolved_blocking_item_ids",
                      "unresolved_conclusion_critical_item_ids"):
            value = obj.get(field)
            unresolved_scope.extend(_list(value if value is not None else [], f"run.{field}"))
        if not unresolved_scope:
            _fail("run.stopping_reason", "cap_with_unresolved requires identified unresolved scope")
    if reason == "target_complete":
        if target is None or rounds_run is None or rounds_run < target:
            _fail("run.stopping_reason", "target_complete requires rounds_run >= round_target")
        _required(obj, ("changed_claim_ids", "rereviewed_claim_ids", "unresolved_blocking_item_ids", "unresolved_conclusion_critical_item_ids"), "run")
        changed = set(_list(obj["changed_claim_ids"], "run.changed_claim_ids"))
        rereviewed = set(_list(obj["rereviewed_claim_ids"], "run.rereviewed_claim_ids"))
        if not changed <= rereviewed:
            _fail("run.rereviewed_claim_ids", "must include every changed claim ID")
        if _list(obj["unresolved_blocking_item_ids"], "run.unresolved_blocking_item_ids"):
            _fail("run.unresolved_blocking_item_ids", "target_complete requires no unresolved blocking item")
        if _list(obj["unresolved_conclusion_critical_item_ids"], "run.unresolved_conclusion_critical_item_ids"):
            _fail("run.unresolved_conclusion_critical_item_ids", "target_complete requires no unresolved conclusion-critical item")
        if _list(obj.get("unreviewed_sections", []), "run.unreviewed_sections"):
            _fail("run.unreviewed_sections", "target_complete requires no unreviewed section")
    if reason == "converged":
        if target is not None and rounds_run is not None and rounds_run <= target:
            _fail("run.stopping_reason", "converged is reserved for continuation beyond round_target")
        changed = set(_list(obj.get("changed_claim_ids", []), "run.changed_claim_ids"))
        rereviewed = set(_list(obj.get("rereviewed_claim_ids", []), "run.rereviewed_claim_ids"))
        if not changed <= rereviewed:
            _fail("run.rereviewed_claim_ids", "converged requires every changed claim to be re-reviewed")
        for field in ("unresolved_blocking_item_ids", "unresolved_conclusion_critical_item_ids"):
            if _list(obj.get(field, []), f"run.{field}"):
                _fail(f"run.{field}", "converged requires no unresolved critical item")
        if _list(obj.get("unreviewed_sections", []), "run.unreviewed_sections"):
            _fail("run.unreviewed_sections", "converged requires no unreviewed section")
        rounds = _list(obj.get("rounds", []), "run.rounds")
        # a trailing round prepared or authorized but never started is not part of the review (finalize's rule)
        while rounds and isinstance(rounds[-1], dict) and not rounds[-1].get("started_at") \
                and rounds[-1].get("state") in ("prepared", "awaiting_authorization", "ready"):
            rounds = rounds[:-1]
        if not rounds or not isinstance(rounds[-1], dict) or rounds[-1].get("factual_corrections") != 0:
            _fail("run.rounds", "converged requires a final beyond-target round with factual_corrections=0")
    if reason == "aborted":
        _text(obj.get("stopping_reason_detail"), "run.stopping_reason_detail")
    invocation_ids: set[str] = set()
    attempts: dict[tuple[Any, Any], list[int]] = {}
    for index, invocation in enumerate(_list(obj["invocations"], "run.invocations")):
        validate_invocation(invocation, f"run.invocations[{index}]")
        invocation_id = invocation["invocation_id"]
        if invocation_id in invocation_ids:
            _fail(f"run.invocations[{index}].invocation_id", "duplicate invocation ID")
        invocation_ids.add(invocation_id)
        if invocation.get("inference_start_status") == "started":
            # Role attempts are numbered round-wide by the persistent budget; a capability probe is
            # numbered within the driver invocation that ran it, so probe sequences group per invocation.
            key = (invocation.get("role"), invocation.get("round"))
            if invocation.get("attempt_kind") == "capability_probe":
                key = key + (str(invocation.get("invocation_id") or "").split(":")[0],)
            attempts.setdefault(key, []).append(invocation.get("attempt"))
    for key, allocated in attempts.items():
        if allocated != list(range(1, len(allocated) + 1)) or len(allocated) > ATTEMPT_CEILING_MAX:
            _fail("run.invocations", f"accepted inference attempts for {key[:2]} must be contiguous 1..N, N<={ATTEMPT_CEILING_MAX}")

    if metadata_2_1:
        _required(
            obj,
            ("journal_watermark", "pause_reason", "post_delta_gates_passed", "events"),
            "run",
        )
        events = _list(obj["events"], "run.events")
        previous_hash = None
        saw_live_chain = False
        # text_redaction (lifecycle_journal 2.7.6) masks a free-text field of an earlier event in this projection; the
        # masked copy cannot re-hash to the journal's event_hash, so such an event is checked by chain continuity only.
        redacted_seqs = set()
        for _event in events:
            if isinstance(_event, dict) and _event.get("type") == "text_redaction":
                try:
                    redacted_seqs.add(int((_event.get("payload") or {}).get("seq")))
                except (TypeError, ValueError):
                    pass
        quorum_events: set[int] = set()
        integrated_events: set[int] = set()
        for index, event_value in enumerate(events):
            event = _object(event_value, f"run.events[{index}]")
            _required(
                event,
                ("schema_version", "seq", "at", "type", "payload", "source", "pid",
                 "previous_hash", "event_hash"),
                f"run.events[{index}]",
            )
            event_schema = event["schema_version"]
            if event_schema == "2.1.0":
                saw_live_chain = True
            elif event_schema == "legacy" and not saw_live_chain:
                pass
            else:
                _fail(f"run.events[{index}].schema_version", "legacy events are allowed only as an immutable prefix")
            if event["seq"] != index + 1:
                _fail(f"run.events[{index}]", "metadata 2.1 events require contiguous 1-based sequence")
            if event["previous_hash"] != previous_hash:
                _fail(f"run.events[{index}].previous_hash", "hash chain is broken")
            material = {key: val for key, val in event.items() if key != "event_hash"}
            digest = hashlib.sha256(json.dumps(
                material, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            if event["seq"] in redacted_seqs and any(
                    (event.get("payload") or {}).get(field) == "[redacted]"
                    for field in ("text", "notes", "note", "detail", "description", "recipient")):
                digest = event["event_hash"]
            if event["event_hash"] != digest:
                _fail(f"run.events[{index}].event_hash", "does not hash the canonical event")
            previous_hash = digest
            payload = event.get("payload") or {}
            if event.get("type") in {"round_quorum_complete", "round_end"} and isinstance(payload.get("round"), int):
                quorum_events.add(payload["round"])
            if event.get("type") in {"round_integrated", "round_result"} and isinstance(payload.get("round"), int):
                integrated_events.add(payload["round"])
        mark = _object(obj["journal_watermark"], "run.journal_watermark")
        expected_mark = {
            "journal_seq": len(events),
            "journal_hash": previous_hash,
        }
        if mark != expected_mark:
            _fail("run.journal_watermark", f"must equal {expected_mark}")

        round_rows = _list(obj.get("rounds", []), "run.rounds")
        valid_states = {
            "prepared", "awaiting_authorization", "ready", "running",
            "quorum_complete", "integrated",
        }
        integrated = 0
        for index, round_value in enumerate(round_rows):
            row = _object(round_value, f"run.rounds[{index}]")
            state = row.get("state")
            if state not in valid_states:
                _fail(f"run.rounds[{index}].state", "metadata 2.1 round requires a state-machine state")
            integrated += int(state == "integrated")
            if state == "awaiting_authorization" and row.get("started_at") is not None:
                _fail(f"run.rounds[{index}]", "awaiting authorization cannot be projected as started")
            if state in {"running", "quorum_complete", "integrated"} and row.get("started_at") is None:
                _fail(f"run.rounds[{index}].started_at", "running-or-later round needs dispatch start")
        if not normalized_v1 and obj.get("rounds_run") != integrated:
            _fail("run.rounds_run", "must count quorum-complete integrated rounds only")
        if reason == "target_complete":
            if integrated < 2:
                _fail("run.stopping_reason", "target completion requires two integrated rounds")
            completed_rounds = quorum_events & integrated_events
            if len(completed_rounds) < 2:
                _fail(
                    "run.events",
                    "target completion requires two rounds with both quorum-complete and integrated lifecycle events",
                )
            if obj.get("post_delta_gates_passed") is not True:
                _fail("run.post_delta_gates_passed", "target completion requires passing post-delta gates")


VALIDATORS = {
    "row": validate_finding_row,
    "findings": validate_findings_registry,
    "critic": validate_critic_result,
    "invocation": validate_invocation,
    "run": validate_run,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=tuple(VALIDATORS))
    parser.add_argument("input")
    args = parser.parse_args(argv)
    try:
        with Path(args.input).open(encoding="utf-8") as handle:
            value = json.load(handle)
        VALIDATORS[args.kind](value)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
