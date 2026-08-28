#!/usr/bin/env python3
"""Deterministically adapt v1 I4R records to the strict v2 interfaces.

The adapter never estimates unobserved model, effort, time, hash, or token data.
Missing v1 telemetry becomes null or the explicit value ``unknown`` and is listed
under ``normalization.fields_unavailable``. Scientific finding fields required by
v2 cause a hard error when absent; silently inventing them would be unsafe.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections import defaultdict
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


class NormalizationError(ValueError):
    """Raised when a strict v2 scientific field is absent from v1 input."""


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _nonempty(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NormalizationError(f"{path} must be a non-empty string; refusing to invent it")
    return value


def _relative_safe(value: Any) -> bool:
    """Return whether a legacy string can safely enter a workspace-relative path field."""
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value.strip().replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return False
    return ".." not in Path(normalized).parts


def _artifact_path(value: Any) -> str | None:
    """Remove a legacy line/column locator while retaining its relative artifact path."""
    if not isinstance(value, str):
        return None
    candidate = re.split(r"\s+column\s+", value.strip(), maxsplit=1, flags=re.IGNORECASE)[0]
    candidate = re.sub(r":\d+(?:-\d+)?$", "", candidate)
    return candidate if _relative_safe(candidate) else None


def _safe_legacy_label(value: Any) -> str:
    """Retain useful relative labels without leaking a legacy host path into v2 output."""
    text = str(value)
    if _relative_safe(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"redacted-unsafe-location-sha256:{digest}"


def _derive_domain(finding: dict[str, Any], origin: str) -> str:
    if finding.get("disclosure_flag"):
        return "privacy_archive"
    origin_domain = {
        "auditor_data_integrity": "data",
        "auditor_text_vs_data": "text",
        "auditor_code_methods": "code_methods",
        "auditor_privacy_archive": "privacy_archive",
    }
    if origin in origin_domain:
        return origin_domain[origin]
    return {
        "estimation": "statistics",
        "inference": "statistics",
        "data": "data",
        "cleaning": "data",
        "sample": "data",
        "reporting": "text",
        "figure": "text",
        "text": "text",
        "analysis_code": "code_methods",
        "environment": "code_methods",
    }.get(finding.get("locus"), "other")


def _derive_source_location(finding: dict[str, Any], artifact: str) -> dict[str, str]:
    locator = next(
        (
            value
            for value in (finding.get("claim_ref"), finding.get("section"), finding.get("locus"))
            if isinstance(value, str) and value.strip()
        ),
        "legacy locator unavailable",
    )
    if finding.get("paper_only"):
        return {"path": "paper/paper.txt", "locator": locator}
    for legacy_locus in finding.get("package_locus") or []:
        source_path = _artifact_path(legacy_locus)
        if source_path:
            return {"path": source_path, "locator": legacy_locus}
    if _relative_safe(artifact):
        return {"path": artifact, "locator": locator}
    return {"path": "legacy/unlocated-source", "locator": f"{locator}; legacy artifact label: {_safe_legacy_label(artifact)}"}


def _major_v1(record: dict[str, Any]) -> None:
    version = str(record.get("schema_version", ""))
    if not version.startswith("1."):
        raise NormalizationError(f"expected a v1 record, got schema_version={version!r}")


def _claim_id(finding: dict[str, Any]) -> str:
    key = finding.get("claim_ref") or finding.get("claim")
    return "claim-" + hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16]


def normalize_comment_v1(comment: dict[str, Any]) -> dict[str, Any]:
    """Project a v1 comment into the v2 findings-registry interface."""
    _major_v1(comment)
    rows_out: list[dict[str, Any]] = []

    for index, finding in enumerate(comment.get("findings", [])):
        path = f"findings[{index}]"
        finding_id = _nonempty(finding.get("id"), f"{path}.id")
        impact = _nonempty(finding.get("impact"), f"{path}.impact")
        if impact not in IMPACT_RANK:
            raise NormalizationError(f"{path}.impact has unsupported value {impact!r}")
        tier = finding.get("verification_tier")
        if not isinstance(tier, int) or isinstance(tier, bool) or not 1 <= tier <= 5:
            raise NormalizationError(f"{path}.verification_tier must be an integer from 1 to 5")
        confidence = _nonempty(finding.get("confidence"), f"{path}.confidence")
        if confidence not in {"CONFIRMED", "LIKELY", "SPECULATIVE"}:
            raise NormalizationError(f"{path}.confidence has unsupported value {confidence!r}")

        evidence = finding.get("evidence")
        if not isinstance(evidence, dict):
            raise NormalizationError(f"{path}.evidence must be an object")
        artifact = _nonempty(evidence.get("artifact"), f"{path}.evidence.artifact")
        operation = _nonempty(evidence.get("operation"), f"{path}.evidence.operation")

        unverifiable_basis = None
        if impact == "unverifiable":
            reason = _nonempty(finding.get("unverifiable_reason"), f"{path}.unverifiable_reason")
            blocked_tier = finding.get("blocked_tier")
            if not isinstance(blocked_tier, int) or isinstance(blocked_tier, bool) or not 1 <= blocked_tier <= 5:
                raise NormalizationError(f"{path}.blocked_tier must be an integer from 1 to 5")
            explicit_basis = finding.get("unverifiable_basis")
            if not explicit_basis:
                observed = json.dumps(evidence.get("observed"), sort_keys=True, ensure_ascii=False)
                explicit_basis = f"Attempted: {operation}. Observed: {observed}."
            unverifiable_basis = {
                "reason": reason,
                "blocked_tier": blocked_tier,
                "attempted_check": operation,
                "basis": _nonempty(explicit_basis, f"{path}.unverifiable_basis"),
            }

        provenance = finding.get("provenance") or {}
        origin = _nonempty(provenance.get("origin_role"), f"{path}.provenance.origin_role")
        independent = list(provenance.get("independently_found_by") or [origin])
        agreed = list(provenance.get("agreed_after_seeing") or [])
        raw_source_records = [
            value
            for value in (
                provenance.get("auditor_report"),
                provenance.get("adjudication_entry"),
                provenance.get("ledger_row"),
                finding.get("verification_script"),
            )
            if isinstance(value, str) and value
        ]
        source_records = [_safe_legacy_label(value) for value in raw_source_records]
        dependency_paths: list[str] = []
        for candidate in [finding.get("verification_script"), *raw_source_records, *(finding.get("package_locus") or [])]:
            artifact_path = _artifact_path(candidate)
            if artifact_path and artifact_path not in dependency_paths:
                dependency_paths.append(artifact_path)
        # v1 verifier labels do not identify a canonical invocation or an immutable evidence
        # record. Retaining them as v2 verification records would manufacture independence.
        # The source comment remains hash-bound by source_comment_sha256; migration must backfill
        # these records from actual invocations.jsonl rows and reviewer output artifacts.
        verification_rows: list[dict[str, Any]] = []

        rows_out.append(
            {
                "schema_version": "2.0.0",
                "id": finding_id,
                "claim": _nonempty(finding.get("claim"), f"{path}.claim"),
                "domain": _derive_domain(finding, origin),
                "source_location": _derive_source_location(finding, artifact),
                "claim_ref": finding.get("claim_ref"),
                "status": "draft",
                "ledger_disposition": {"status": "INCLUDED", "target": None, "reason": None},
                "coordinator_approved": False,
                "impact": impact,
                "impact_rank": IMPACT_RANK[impact],
                "verification_tier": tier,
                "paper_only": bool(finding.get("paper_only")),
                "confidence": confidence,
                "evidence": {
                    "source_hashes": [],
                    "unhashed_source_paths": [_safe_legacy_label(artifact)],
                    "rerun_command": operation,
                    "expected": evidence.get("expected"),
                    "observed": evidence.get("observed"),
                    "tolerance_basis": evidence.get("tolerance_note"),
                },
                "unverifiable_basis": unverifiable_basis,
                "verifications": verification_rows,
                "dependencies": {
                    "environment_fingerprint": None,
                    "finding_ids": [],
                    "artifact_paths": dependency_paths,
                },
                "settled": None,
                "benign_explanations_ruled_out": list(finding.get("benign_explanations_ruled_out") or []),
                "report_section": finding.get("section"),
                "verification_snippet": None,
                "disclosure_flag": bool(finding.get("disclosure_flag")),
                "remedy": {
                    "class": _nonempty(finding.get("remedy_class"), f"{path}.remedy_class"),
                    "description": finding.get("remedy_description"),
                    "verified": finding.get("remedy_verified"),
                },
                "changes_conclusion": bool(finding.get("changes_conclusion")),
                "changes_conclusion_which": finding.get("changes_conclusion_which"),
                "provenance": {
                    "origin_role": origin,
                    "independently_found_by": independent,
                    "agreed_after_seeing": agreed,
                    "round_introduced": provenance.get("round_introduced"),
                    "source_records": source_records,
                },
            }
        )

    unavailable = [
        "report_sha256",
        "generated_at",
        "rows[*].evidence.source_hashes",
        "rows[*].dependencies.environment_fingerprint",
        "rows[*].verifications[*].invocation_id",
        "rows[*].verifications[*].evidence_record",
        "rows[*].verifications[*].model_family",
    ]
    return {
        "schema_version": "2.0.0",
        "audit_id": _nonempty(comment.get("audit_id"), "audit_id"),
        "policy_version": _nonempty(comment.get("enumeration_scheme"), "enumeration_scheme"),
        "source_comment_sha256": canonical_sha256(comment),
        "report_sha256": None,
        "generated_at": None,
        "rows": rows_out,
        "normalization": {
            "source_schema_version": str(comment.get("schema_version")),
            "adapter": "normalize_v1_to_v2.py",
            "fields_unavailable": unavailable,
        },
    }


def _usage_from_token(token: dict[str, Any] | None) -> dict[str, Any]:
    if token is None:
        return {
            "obtainable": False,
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "source": "unavailable",
        }
    obtainable = bool(token.get("obtainable"))
    return {
        "obtainable": obtainable,
        "input_tokens": token.get("input_tokens") if obtainable else None,
        "output_tokens": token.get("output_tokens") if obtainable else None,
        "cached_input_tokens": token.get("cached_input_tokens") if obtainable else None,
        "reasoning_tokens": token.get("reasoning_tokens") if obtainable else None,
        "total_tokens": token.get("total_tokens") if obtainable else None,
        "source": (token.get("source") or "unavailable") if obtainable else "unavailable",
    }


def normalize_run_v1(run: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a v1 run in place conceptually while preserving its complete legacy record."""
    _major_v1(run)
    result = copy.deepcopy(run)
    source_sha = canonical_sha256(run)

    token_queues: dict[tuple[Any, Any], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, token in enumerate(run.get("tokens") or []):
        token_queues[(token.get("role"), token.get("round"))].append((index, token))

    attempts: dict[tuple[Any, Any], int] = defaultdict(int)
    invocations: list[dict[str, Any]] = []
    for index, assignment in enumerate(run.get("model_assignments") or []):
        key = (assignment.get("role"), assignment.get("round"))
        attempts[key] += 1
        token_index, token = (None, None)
        if token_queues[key]:
            token_index, token = token_queues[key].pop(0)

        source = assignment.get("source") or "unavailable"
        model_id = assignment.get("model_id")
        requested = model_id if source == "declared" else None
        observed = model_id if source in {"observed", "inferred"} else None
        raw_id = f"{run.get('audit_id')}|{assignment.get('role')}|{assignment.get('round')}|{index}"
        invocation_id = "inv-" + hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]
        invocations.append(
            {
                "invocation_id": invocation_id,
                "role": _nonempty(assignment.get("role"), f"model_assignments[{index}].role"),
                "role_class": assignment.get("role_class") or "other",
                "round": assignment.get("round"),
                "attempt": attempts[key],
                "status": "legacy_record",
                "provider": assignment.get("provider"),
                "provider_call": True,
                "cache_usage": "none",
                "session_id": None,
                "requested_model": requested,
                "observed_model": observed,
                "model_family": assignment.get("model_family"),
                "model_source": source if source in {"observed", "declared", "inferred"} else "unavailable",
                "reasoning_effort": "unknown",
                "started_at": None,
                "ended_at": None,
                "elapsed_seconds": None,
                "input_artifact_sha256": None,
                "prompt_sha256": None,
                "output_artifact": None,
                "output_sha256": None,
                "usage": _usage_from_token(token),
                "error": None,
                "error_class": None,
                "legacy_source": {
                    "model_assignment_index": index,
                    "token_record_index": token_index,
                },
            }
        )

    old_reason = run.get("stopping_reason")
    reason_map = {
        "cap": "cap_with_unresolved",
        "cap_reached": "cap_with_unresolved",
        "cap_with_unresolved": "cap_with_unresolved",
        "target_complete": "target_complete",
        "converged": "aborted",
        "target_reached": "aborted",
        "quality_gate_passed": "aborted",
        "budget_exhausted": "aborted",
        "operator_stopped": "aborted",
        "failed": "aborted",
        "aborted": "aborted",
        None: None,
    }
    result["schema_version"] = "2.0.0"
    result["round_target"] = run.get("round_target")
    result["stopping_reason"] = reason_map.get(old_reason, "aborted")
    if result["stopping_reason"] == "cap_with_unresolved":
        result["stopping_reason_detail"] = run.get("stopping_reason_detail") or "Legacy v1 stopped at its cap; v1 did not capture a complete unresolved-item ledger."
        result["unreviewed_sections"] = list(run.get("unreviewed_sections") or ["unknown:v1-unreviewed-sections-not-captured"])
    elif result["stopping_reason"] == "aborted" and old_reason is not None:
        result["stopping_reason_detail"] = run.get("stopping_reason_detail") or f"Legacy v1 reason {old_reason!r} lacks the evidence required to certify a canonical v2 completion state."
    result["invocations"] = invocations
    unavailable = [
        "invocations[*].reasoning_effort",
        "invocations[*].started_at",
        "invocations[*].ended_at",
        "invocations[*].elapsed_seconds",
        "invocations[*].input_artifact_sha256",
        "invocations[*].prompt_sha256",
        "invocations[*].output_artifact",
        "invocations[*].output_sha256",
        "invocations[*].cache_usage",
        "invocations[*].session_id",
        "invocations[*].error_class",
        "changed_claim_ids",
        "rereviewed_claim_ids",
        "unresolved_blocking_item_ids",
        "unresolved_conclusion_critical_item_ids",
    ]
    if result["round_target"] is None:
        unavailable.append("round_target")
    result["normalization"] = {
        "source_schema_version": str(run.get("schema_version")),
        "source_sha256": source_sha,
        "adapter": "normalize_v1_to_v2.py",
        "fields_unavailable": unavailable,
    }
    return result


def _read(path: str) -> dict[str, Any]:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def _write(value: dict[str, Any], path: str) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path == "-":
        sys.stdout.write(payload)
    else:
        Path(path).write_text(payload, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("run", "comment"), help="v1 input record type")
    parser.add_argument("input", help="input JSON path, or - for stdin")
    parser.add_argument("-o", "--output", default="-", help="output JSON path (default: stdout)")
    args = parser.parse_args(argv)
    try:
        value = _read(args.input)
        normalized = normalize_run_v1(value) if args.kind == "run" else normalize_comment_v1(value)
        _write(normalized, args.output)
    except (OSError, json.JSONDecodeError, NormalizationError) as exc:
        print(f"normalization failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
