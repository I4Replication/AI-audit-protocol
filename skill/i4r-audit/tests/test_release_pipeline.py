#!/usr/bin/env python3
"""Bounded integration tests for release staging and atomic publication.

The fixtures contain no source data, participant identifiers, private paths, or
network dependencies.  Run directly with Python or through unittest discovery.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1]
STAGE = SKILL / "scripts" / "stage_release.sh"
PUBLISH = SKILL / "scripts" / "publish_completed.sh"
GENERATOR = SKILL / "scripts" / "generate_finding_artifacts.py"
INIT = SKILL / "scripts" / "init_run.sh"
LOG_EVENT = SKILL / "scripts" / "log_event.sh"
FINALIZE = SKILL / "scripts" / "finalize_run.sh"
MAKE_SETTLED = SKILL / "scripts" / "make_settled.sh"
MAKE_DIFF = SKILL / "scripts" / "make_diff_packet.py"
SUMMARIZE = SKILL / "scripts" / "summarize_invocations.py"
POST_DELTA = SKILL / "scripts" / "run_post_delta_gates.py"
VALIDATE_ROUNDS = SKILL / "scripts" / "validate_round_projections.py"
sys.path.insert(0, str(SKILL / "scripts"))
import test_run_critique_runtime as critique_runtime


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def tiny_pdf(text: str = "Sanitized independent audit comment") -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects)+1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def checked(command: list[str], *, environment: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, text=True, capture_output=True, env=environment)
    if result.returncode:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{result.stdout}\n{result.stderr}"
        )


def verification_manifest(audit: Path) -> dict[str, object]:
    rows: dict[str, dict[str, object]] = {}
    for label, relative in {
        "report": "report/REPORT_final.md",
        "comment": "comment.json",
        "summary": "verification_summary.csv",
        "notebook": "verification.Rmd",
    }.items():
        path = audit / relative
        rows[label] = {
            "path": relative,
            "sha256": digest(path),
            "bytes": path.stat().st_size,
        }
    status = audit / "verification_status.txt"
    return {
        "schema_version": "2.0.0",
        "status": 0,
        "claim_ids": ["C-1"],
        "package_manifest_sha256": digest(audit / "package_manifest.json"),
        "status_artifact": {"path": "verification_status.txt", "sha256": digest(status)},
        "render": {
            "transaction_id": "integration-render-1",
            "started_at": "2026-08-27T12:00:00Z",
            "ended_at": "2026-08-27T12:00:01Z",
            "exit_status": 0,
            "mode": "executed",
        },
        **rows,
    }


def round_input_hash(round_number: int) -> str:
    return hashlib.sha256(f"integration-input-{round_number}".encode()).hexdigest()


def critic_invocation_id(audit_id: str, round_number: int, role: str) -> str:
    return f"{audit_id}:r{round_number}:critic_{role}:{round_input_hash(round_number)[:16]}"


def seed_round_projection(
    audit: Path,
    audit_id: str,
    round_number: int,
    roles: list[str],
    report_source: Path,
    diff_manifest: Path | None = None,
) -> None:
    def json_file(path: Path, value: object) -> None:
        write(path, json.dumps(value, sort_keys=True) + "\n")

    round_dir = audit / f"critique/round_{round_number}"
    invocation = f"integration-round-{round_number}"
    input_hash = round_input_hash(round_number)
    report_hash = digest(report_source)
    snapshot = round_dir / "snapshots" / input_hash
    source = snapshot / "source"
    write(source / "REPORT_TO_REVIEW.md", report_source.read_bytes())
    write(source / "context/PIPELINE_POLICY.json", (audit / "pipeline-policy.snapshot.json").read_bytes())
    write(source / "context/EVIDENCE_HASHES.json", (audit / "EVIDENCE_HASHES.json").read_bytes())
    for relative in ("work/check_primary.txt", "work/check_primary.R", "work/draft_review.txt"):
        write(snapshot / relative, (audit / relative).read_bytes())
    findings_source = (audit / "work/findings_round_1.jsonl" if round_number == 1
                       else audit / "work/findings.jsonl")
    write(source / "context/work/findings.jsonl", findings_source.read_bytes())
    if round_number > 1:
        write(source / "context/SETTLED.json", (audit / "SETTLED.json").read_bytes())
        write(source / "context/SETTLED.md", (audit / "SETTLED.md").read_bytes())
    for role in roles:
        write(snapshot / f"rendered/prompt_{role}.txt",
              (SKILL / f"prompts/role_{role}.txt").read_bytes())
    diff_path = None
    if diff_manifest is not None:
        diff_path = source / "context/DIFF_MANIFEST.json"
        write(diff_path, diff_manifest.read_bytes())
    assignments = {role: ([] if round_number == 1 else ["F2"]) for role in roles}
    routing = {
        "routing_version": "i4r-round-routing-1", "round": round_number, "roles": roles,
        "reason": "integration fixture route", "diff_manifest_present": diff_path is not None,
        "diff_manifest_sha256": digest(diff_path) if diff_path else None,
        "claim_assignment_mode": "unscoped_round_1" if round_number == 1 else "exact_changed_claims",
        "claim_assignments": assignments,
    }
    json_file(source / "ROUTING.json", routing)
    snapshot_files = {
        path.relative_to(snapshot).as_posix(): digest(path)
        for path in sorted(snapshot.rglob("*")) if path.is_file()
    }
    json_file(snapshot / "MANIFEST.json", {
        "snapshot_version": "i4r-runtime-snapshot-3", "input_sha256": input_hash,
        "report_sha256": report_hash, "round": round_number, "model": "gpt-5.6-terra",
        "effort": "high", "roles": roles, "audit_id": audit_id,
        "files_sha256": snapshot_files,
    })
    projection = round_dir / "projections" / invocation
    json_file(projection / "ROUTING.json", routing)
    result_rows: list[tuple[str, dict[str, object]]] = []
    claim_assessments: dict[str, list[str]] = {}
    critic_outputs: list[dict[str, object]] = []
    for role in roles:
        original = round_dir / f"critic_{role}.json"
        copied = projection / f"critic_{role}.json"
        write(copied, original.read_bytes())
        result = json.loads(copied.read_text())
        result_rows.append((role, result))
        claim_assessments[role] = [item["finding_id"] for item in result["items"]]
        markdown = projection / f"critic_{role}.md"
        write(markdown, f"# critic_{role}\n\nSynthetic complete result.\n\n<!-- I4R_CRITIC_COMPLETE -->\n")
        sidecar = projection / f"{role}.complete.json"
        json_file(sidecar, {
            "completion_version": "i4r-runtime-completion-2", "audit_id": audit_id,
            "input_sha256": input_hash, "report_sha256": report_hash,
            "round": round_number, "role": role, "attempts": 1,
            "completion_marker": True, "output_sha256": digest(copied),
            "markdown_sha256": digest(markdown),
        })
        critic_outputs.append({
            "role": f"critic_{role}",
            "json_path": copied.relative_to(audit).as_posix(), "json_sha256": digest(copied),
            "markdown_path": markdown.relative_to(audit).as_posix(),
            "markdown_sha256": digest(markdown),
            "completion_path": sidecar.relative_to(audit).as_posix(),
            "completion_sha256": digest(sidecar),
        })
    merge = critique_runtime.build_claim_merge(
        result_rows,
        critique_runtime.load_registry_file(source / "context/work/findings.jsonl"),
        [], audit_id=audit_id, round_number=round_number,
        input_sha=input_hash, report_sha=report_hash,
    )
    merge_path = projection / "CLAIM_MERGE.json"
    json_file(merge_path, merge)
    json_file(projection / "adjudication_packet.json", {
        "packet_version": "i4r-targeted-adjudication-packet-2", "audit_id": audit_id,
        "round": round_number, "input_sha256": input_hash, "report_sha256": report_hash,
        "policy": "auto", "triggers": [], "claim_merge_sha256": digest(merge_path),
        "unresolved_rows": [], "critic_outputs": critic_outputs,
    })
    projection_files = {
        path.relative_to(projection).as_posix(): digest(path)
        for path in sorted(projection.rglob("*")) if path.is_file()
    }
    json_file(projection / "MANIFEST.json", {
        "projection_version": "i4r-round-projection-1", "invocation_id": invocation,
        "audit_id": audit_id, "round": round_number, "input_sha256": input_hash,
        "report_sha256": report_hash, "roles": roles, "adjudication": "no_triggers",
        "target_input_sha256": None, "claim_assessments": claim_assessments,
        "rereviewed_claim_ids": list(dict.fromkeys(
            claim_id for role in roles for claim_id in claim_assessments[role])),
        "claim_merge_sha256": digest(merge_path), "files_sha256": projection_files,
    })
    write(round_dir / "CURRENT_PROJECTION", invocation + "\n")
    json_file(round_dir / "ROUND_STATUS.json", {
        "status_version": "i4r-round-status-2", "audit_id": audit_id,
        "invocation_id": invocation, "input_sha256": input_hash,
        "report_sha256": report_hash, "round": round_number, "outcome": "complete",
        "exit_code": 0, "expected_roles": roles, "completed_roles": roles,
        "failed_roles": [], "quorum_met": True,
    })


def make_audit(base: Path, paper: str = "Paper-Sanitized") -> Path:
    """Create the smallest release-capable audit tree accepted by both scripts."""
    audit = base / "Discipline" / "Journal-Sanitized" / paper / "audit"
    (audit / "pkg").mkdir(parents=True, exist_ok=True)
    write(audit / "package_manifest.json", json.dumps({
        "schema_version": "2.0.0", "algorithm": "sha256",
        "package_sha256": hashlib.sha256(b"").hexdigest(),
        "file_count": 0, "file_types": {}, "files": [],
    }, indent=2) + "\n")
    os.chmod(audit / "pkg", 0o555)
    checked([
        "/bin/bash", str(INIT), str(audit), "--audit-id", "audit-integration-001",
        "--doi", "10.5555/integration", "--auditor-model", "sonnet-5",
        "--auditor-family", "claude", "--auditor-provider", "anthropic",
        "--protocol-version", "2.0.0", "--gate-set-version", "integration",
    ])
    before_report = audit / "report/REPORT_v1.md"
    final_report = audit / "report/REPORT_final.md"
    write(before_report, "# Independent audit\n\n## 1. Finding\n\nDraft wording before adversarial review [claim 1].\n")
    write(final_report, """# Independent audit

## Abstract

This sanitized fixture verifies that the audit's generated evidence and structured findings agree.

## 1. Introduction

The bounded arithmetic example is a release-pipeline fixture.

## 2. Finding

The sanitized evidence reproduces exactly.

## 3. Conclusion

The fixture supports the narrow implementation claim tested here.

## References

AI-Games protocol. AI-vertical audit guidance.
""")
    write(audit / "report/ADJUDICATION.md", "# Adjudication\n\nF1 accepted.\n")
    for name in (
        "01_reproduction.md",
        "02_data_integrity.md",
        "03_text_vs_data.md",
        "04_code_methods.md",
        "05_privacy_archive.md",
    ):
        write(audit / f"report/{name}", f"# {name.removesuffix('.md')}\n\nComplete.\n")

    comment = json.loads((SKILL / "schemas/EXAMPLE_comment.json").read_text())
    comment["audit_id"] = "audit-integration-001"
    comment["bibliographic"].update({
        "doi": "10.5555/integration", "title": "Sanitized integration fixture",
        "journal": "Journal-Sanitized", "field": "test fixture",
    })
    comment["findings"] = [{"id": "F1", "claim": "Sanitized fixture finding"}]
    write(audit / "comment.json", json.dumps(comment, indent=2) + "\n")
    write(audit / "work/analysis.py", "print('sanitized fixture')\n")
    evidence = audit / "work/check_primary.txt"
    snippet = audit / "work/check_primary.R"
    draft_evidence = audit / "work/draft_review.txt"
    write(evidence, "expected=2 observed=2\n")
    write(snippet, "stopifnot(1 + 1 == 2)\n")
    write(draft_evidence, "The pre-integration wording was reviewed and superseded.\n")
    evidence_inventory = {
        path.relative_to(audit).as_posix(): {"sha256": digest(path), "size": path.stat().st_size}
        for path in (evidence, snippet, draft_evidence)
    }
    write(audit / "EVIDENCE_HASHES.json", json.dumps(evidence_inventory, sort_keys=True) + "\n")
    finding = json.loads((SKILL / "schemas/fixtures/v2_finding_row.json").read_text())
    finding.update({
        "claim": "The sanitized arithmetic fixture reproduces exactly.",
        "source_location": {"path": "report/REPORT_final.md", "locator": "Section 1"},
        "claim_ref": "Section 1",
        "report_section": "Section 1",
        "settled": {"approved": True, "valid_for_report_hash": digest(final_report)},
    })
    finding["evidence"].update({
        "source_hashes": [{"path": "work/check_primary.txt", "sha256": digest(evidence)}],
        "rerun_command": "Rscript work/check_primary.R",
        "expected": 2,
        "observed": 2,
        "tolerance_basis": "Exact integer comparison.",
    })
    finding["dependencies"]["artifact_paths"] = ["work/check_primary.txt", "work/check_primary.R"]
    finding["verification_snippet"] = {
        "kind": "code",
        "path": "work/check_primary.R",
        "basis": "The bounded arithmetic check reproduces exactly.",
    }
    excluded = json.loads(json.dumps(finding))
    excluded.update({
        "id": "F2",
        "claim": "The superseded draft wording is excluded after round-one integration.",
        "domain": "framing",
        "status": "rejected",
        "coordinator_approved": False,
        "ledger_disposition": {
            "status": "EXCLUDED", "target": None,
            "reason": "Superseded wording retained as a tested-negative ledger row.",
        },
        "verifications": [],
        "settled": None,
    })
    excluded["dependencies"]["finding_ids"] = ["F1"]
    before_excluded = json.loads(json.dumps(excluded))
    before_excluded["claim"] = "Draft wording remained under review before round-one integration."

    event_environment = os.environ.copy()
    event_environment["RUN_JSON"] = str(audit / "run.json")
    round_one_events = [
        ["phase_end", "phase=intake"],
        ["phase_start", "phase=fan_out"], ["phase_end", "phase=fan_out"],
        ["phase_start", "phase=adjudication"], ["phase_end", "phase=adjudication"],
        ["phase_start", "phase=critique"],
        ["round_start", "round=1", "at=2026-08-27T10:00:00Z",
         "critics_run=critic_stats,critic_data,critic_fairness,critic_completeness"],
        ["round_end", "round=1", "at=2026-08-27T10:20:00Z"],
        ["round_result", "round=1", "blocking_items=1", "comment_defects=1", "paper_defects=0",
         "integrated_at=2026-08-27T10:30:00Z", "consistency_check_run=true",
         "consistency_check_exit=0", "character=wording_only", "factual_corrections=1",
         "report_version_reviewed=report/REPORT_v1.md"],
    ]
    for event in round_one_events:
        checked(["/bin/bash", str(LOG_EVENT), *event], environment=event_environment)

    critic_fixture = json.loads((SKILL / "schemas/fixtures/v2_critic_result.json").read_text())
    report_hashes = {1: digest(before_report), 2: digest(final_report)}
    policy_hash = digest(audit / "pipeline-policy.snapshot.json")
    evidence_manifest_hash = digest(audit / "EVIDENCE_HASHES.json")

    def write_critic(round_number: int, short_role: str) -> tuple[Path, dict[str, object]]:
        role = f"critic_{short_role}"
        result = json.loads(json.dumps(critic_fixture))
        result.update({
            "audit_id": "audit-integration-001",
            "invocation_id": critic_invocation_id("audit-integration-001", round_number, short_role),
            "round": round_number,
            "role": role,
            "report_sha256": report_hashes[round_number],
            "items": [],
            "new_issues": [],
            "covered_claim_ids": ["F1", "F2"] if round_number == 1 else ["F2"],
            "conflicts": [],
            "settled_spot_checks": [] if round_number == 1 else [{
                "fact_id": "F1", "status": "passed",
                "evidence": "Rechecked the bound source hash in work/check_primary.txt.",
            }],
        })
        result["input_hashes"] = {
            "snapshot_sha256": round_input_hash(round_number),
            "report_sha256": report_hashes[round_number],
            "prompt_sha256": digest(SKILL / f"prompts/role_{short_role}.txt"),
            "policy_sha256": policy_hash,
            "evidence_manifest_sha256": evidence_manifest_hash,
        }
        result["coverage"] = {
            "assigned_checks": [role], "completed_checks": [role], "skipped_checks": [],
        }
        item = json.loads(json.dumps(critic_fixture["items"][0]))
        item["item_id"] = f"{short_role}-F{1 if round_number == 1 else 2}"
        item["finding_id"] = "F1" if round_number == 1 else "F2"
        item["claim"] = finding["claim"] if round_number == 1 else excluded["claim"]
        item["evidence"] = {
            "artifact_refs": [{"path": "work/check_primary.txt", "sha256": digest(evidence)}],
            "operation": "compare the bounded rerun with the printed value",
            "result": "The exact comparison reproduces the discrepancy.",
            "reproducible_script": {"path": "work/check_primary.R", "sha256": digest(snippet)},
        }
        if round_number == 2:
            item.update({
                "claim": excluded["claim"],
                "evidence": {
                    "artifact_refs": [{"path": "work/draft_review.txt",
                                       "sha256": digest(draft_evidence)}],
                    "operation": "check the final ledger disposition against the integrated report",
                    "result": "The superseded wording remains excluded and no factual correction is needed.",
                    "reproducible_script": None,
                },
            })
        result["items"].append(item)
        if round_number == 1:
            draft_item = json.loads(json.dumps(item))
            draft_item.update({
                "item_id": f"{short_role}-F2", "finding_id": "F2",
                "claim": before_excluded["claim"],
                "evidence": {
                    "artifact_refs": [{"path": "work/draft_review.txt",
                                       "sha256": digest(draft_evidence)}],
                    "operation": "review the draft wording before integration",
                    "result": "The draft wording is explicitly accounted for as superseded.",
                    "reproducible_script": None,
                },
            })
            result["items"].append(draft_item)
        result["overall"] = {
            "publishable": True, "blocking_count": 0,
            "summary": "Synthetic integration critic completed every assigned check.",
        }
        output = audit / f"critique/round_{round_number}/{role}.json"
        write(output, json.dumps(result, sort_keys=True) + "\n")
        checked(["python3", str(SKILL / "scripts/validate_json_schema.py"), str(output),
                 str(SKILL / "schemas/v2/critic-result.schema.json")])
        checked(["python3", str(SKILL / "schemas/tools/validate_v2.py"), "critic", str(output)])
        return output, result

    output_by_auditor = {
        "auditor_reproduction": "report/01_reproduction.md",
        "auditor_data_integrity": "report/02_data_integrity.md",
        "auditor_text_vs_data": "report/03_text_vs_data.md",
        "auditor_code_methods": "report/04_code_methods.md",
        "auditor_privacy_archive": "report/05_privacy_archive.md",
    }
    initial_roles = [
        ("coordinator", "coordinator", 0, "sonnet-5", "anthropic", "claude",
         "report/REPORT_final.md"),
        *[(role, "auditor", 0, "sonnet-5",
           "anthropic", "claude", output) for role, output in output_by_auditor.items()],
        ("model_probe", "other", 0, "gpt-5.6-terra", "openai", "gpt", "critique/model_probe.json"),
    ]
    ledger: list[dict[str, object]] = []

    def append_invocation(
        invocation_id: str, role: str, role_class: str, round_number: int,
        requested: str, provider: str, family: str, relative: str,
        *, input_hash: str = "a" * 64, prompt_hash: str = "b" * 64,
        cache_usage: str = "write",
        started_at: str = "2026-08-27T09:00:00Z",
        ended_at: str = "2026-08-27T09:01:00Z",
    ) -> None:
        output = audit / relative
        if role == "model_probe":
            write(output, json.dumps({"status": "ok"}) + "\n")
        observed = ("claude-sonnet-5-20260801" if requested == "sonnet-5" else
                    "claude-opus-5-20260801" if provider == "anthropic" else
                    "gpt-5.6-terra-2026-08-01")
        ledger.append({
            "invocation_id": invocation_id, "role": role, "role_class": role_class,
            "round": round_number, "attempt": 1, "status": "completed", "provider": provider,
            "provider_call": True, "cache_usage": cache_usage,
            "session_id": f"session-{len(ledger) + 1}",
            "requested_model": requested, "observed_model": observed, "model_family": family,
            "model_source": "observed",
            "reasoning_effort": ("max" if role_class in {"coordinator", "auditor", "adjudicator"}
                                  or role == "critic_synth" else "high"),
            "started_at": started_at, "ended_at": ended_at,
            "elapsed_seconds": 60, "input_artifact_sha256": input_hash,
            "prompt_sha256": prompt_hash, "output_artifact": relative,
            "output_sha256": digest(output),
            "usage": {"obtainable": True, "input_tokens": 100, "output_tokens": 20,
                      "cached_input_tokens": 10, "reasoning_tokens": 5, "total_tokens": 125,
                      "source": "api_response"},
            "error": None, "error_class": None, "legacy_source": None,
        })

    auditor_times = {
        role: (f"2026-08-27T09:00:0{index}Z", f"2026-08-27T09:01:0{index}Z")
        for index, role in enumerate(output_by_auditor)
    }
    for index, role_spec in enumerate(initial_roles, 1):
        role, role_class, round_number, requested, provider, family, relative = role_spec
        started_at, ended_at = auditor_times.get(
            role, ("2026-08-27T09:00:00Z", "2026-08-27T09:01:00Z"))
        append_invocation(
            f"integration-{role}-{index}", role, role_class, round_number,
            requested, provider, family, relative,
            cache_usage="probe_cache" if role == "model_probe" else "write",
            started_at=started_at, ended_at=ended_at,
        )
    for short_role in ("stats", "data", "fairness", "completeness"):
        output, result = write_critic(1, short_role)
        append_invocation(
            result["invocation_id"], f"critic_{short_role}", "critic", 1,
            "gpt-5.6-terra", "openai", "gpt", output.relative_to(audit).as_posix(),
            input_hash=result["input_hashes"]["snapshot_sha256"],
            prompt_hash=result["input_hashes"]["prompt_sha256"],
        )
    write(audit / "invocations.jsonl", "".join(json.dumps(row, sort_keys=True) + "\n" for row in ledger))

    finding["verifications"] = [
        {
            "reviewer_id": "auditor_reproduction",
            "invocation_id": next(row["invocation_id"] for row in ledger
                                  if row["role"] == "auditor_reproduction"),
            "evidence_record": {
                "path": "report/01_reproduction.md",
                "sha256": digest(audit / "report/01_reproduction.md"),
            },
            "model_family": "claude", "outcome": "confirmed",
        },
        {
            "reviewer_id": "critic_stats",
            "invocation_id": critic_invocation_id("audit-integration-001", 1, "stats"),
            "evidence_record": {
                "path": "critique/round_1/critic_stats.json",
                "sha256": digest(audit / "critique/round_1/critic_stats.json"),
            },
            "model_family": "gpt", "outcome": "confirmed",
        },
    ]
    write(audit / "work/findings_round_1.jsonl",
          "".join(json.dumps(row, sort_keys=True) + "\n" for row in (finding, before_excluded)))
    write(audit / "work/findings.jsonl",
          "".join(json.dumps(row, sort_keys=True) + "\n" for row in (finding, excluded)))
    checked(["python3", str(GENERATOR), str(audit), "--apply"])
    # The generator adds the deterministic claim map/Appendix to REPORT_final.md. Bind settlement
    # only after that canonical projection exists, then prove the second generation is idempotent.
    finding["settled"]["valid_for_report_hash"] = digest(final_report)
    write(audit / "work/findings_round_1.jsonl",
          "".join(json.dumps(row, sort_keys=True) + "\n" for row in (finding, before_excluded)))
    write(audit / "work/findings.jsonl",
          "".join(json.dumps(row, sort_keys=True) + "\n" for row in (finding, excluded)))
    checked(["python3", str(GENERATOR), str(audit), "--apply"])
    if finding["settled"]["valid_for_report_hash"] != digest(final_report):
        raise AssertionError("finding settlement hash changed after deterministic projection")
    report_hashes[2] = digest(final_report)
    # Preserve REPORT_v1 as the real pre-integration diff base and cut the normal
    # post-integration snapshot used by release staging's currentness gate.
    write(audit / "report/REPORT_v2.md", final_report.read_bytes())

    write(audit / "verification_status.txt", "0\n")
    write(audit / "verification_summary.csv",
          "claim_id,key,verdict,detail\nC-1,fixture,PASS,sanitized fixture\n")
    write(audit / "verification_manifest.json",
          json.dumps(verification_manifest(audit), indent=2) + "\n")
    pdf = audit / "report/comment.pdf"
    write(pdf, tiny_pdf())
    document_manifest = {
        "schema_version": "2.0.0", "created_at": "2026-08-27T12:00:00Z",
        "report": {"path": "report/REPORT_final.md", "sha256": digest(final_report),
                   "bytes": final_report.stat().st_size},
        "docx": None,
        "pdf": {"path": "report/comment.pdf", "sha256": digest(pdf), "bytes": pdf.stat().st_size},
    }
    write(audit / "document_build_manifest.json", json.dumps(document_manifest, indent=2) + "\n")

    seed_round_projection(
        audit, "audit-integration-001", 1,
        ["stats", "data", "fairness", "completeness"], before_report,
    )
    checked(["/bin/bash", str(LOG_EVENT), "settled", "used=true", "generated_after_round=1",
             "path=SETTLED.md", "entries=1", "spot_check_failures=0"],
            environment=event_environment)
    checked(["/bin/bash", str(MAKE_SETTLED), str(audit)])

    diff_manifest = audit / "critique/DIFF_MANIFEST_round_2.json"
    diff_command = [
        "python3", str(MAKE_DIFF), str(audit), "--round", "2",
        "--before-report", str(before_report), "--after-report", str(final_report),
        "--before-findings", str(audit / "work/findings_round_1.jsonl"),
        "--after-findings", str(audit / "work/findings.jsonl"),
        "--output", str(diff_manifest),
    ]
    checked(diff_command)
    diff = json.loads(diff_manifest.read_text())
    if diff["changed_claim_ids"] != ["F2"] or diff["recommended_roles"] != ["fairness"]:
        raise AssertionError(f"unexpected generated round-2 route: {diff}")
    if [item["fact_id"] for item in diff["settled_fact_references"]] != ["F1"]:
        raise AssertionError("round-2 diff did not carry the validated F1 settled dependency")

    round_two_events = [
        ["round_start", "round=2", "at=2026-08-27T10:35:00Z",
         "critics_run=critic_fairness,critic_completeness"],
        ["round_end", "round=2", "at=2026-08-27T10:45:00Z"],
        ["round_result", "round=2", "blocking_items=0", "comment_defects=0", "paper_defects=0",
         "integrated_at=2026-08-27T10:50:00Z", "consistency_check_run=true",
         "consistency_check_exit=0", "character=wording_only", "factual_corrections=0",
         "report_version_reviewed=report/REPORT_final.md"],
        ["review_scope", 'changed_claim_ids:=["F2"]', 'rereviewed_claim_ids:=["F2"]',
         "unresolved_blocking_item_ids:=[]", "unresolved_conclusion_critical_item_ids:=[]",
         "unreviewed_sections:=[]"],
        ["phase_end", "phase=critique"],
        ["phase_start", "phase=release"], ["phase_end", "phase=release"],
        ["delivery", "artifact_path=report/comment.pdf", "delivered_at=2026-08-27T11:00:00Z"],
    ]
    for event in round_two_events:
        checked(["/bin/bash", str(LOG_EVENT), *event], environment=event_environment)
    for short_role in ("fairness", "completeness"):
        output, result = write_critic(2, short_role)
        append_invocation(
            result["invocation_id"], f"critic_{short_role}", "critic", 2,
            "gpt-5.6-terra", "openai", "gpt", output.relative_to(audit).as_posix(),
            input_hash=result["input_hashes"]["snapshot_sha256"],
            prompt_hash=result["input_hashes"]["prompt_sha256"],
        )
    # Canonical projections can update a previously registered output (notably the
    # coordinator's REPORT_final.md).  Bind the final ledger to the bytes that will
    # actually be released, while retaining the original invocation identities.
    for row in ledger:
        row["output_sha256"] = digest(audit / str(row["output_artifact"]))
    write(audit / "invocations.jsonl", "".join(json.dumps(row, sort_keys=True) + "\n" for row in ledger))

    # Refresh the content bindings after the append-only round-two events/invocations, then regenerate
    # the deterministic packet so its settled-cache hash remains exact at finalization.
    checked(["/bin/bash", str(MAKE_SETTLED), str(audit)])
    checked(diff_command)
    seed_round_projection(
        audit, "audit-integration-001", 2, ["fairness", "completeness"], final_report,
        diff_manifest,
    )
    checked([sys.executable, str(POST_DELTA), str(audit), "--round", "2",
             "--report", str(final_report)])
    checked(["/bin/bash", str(FINALIZE), str(audit), "--stopping-reason", "target_complete",
             "--registry", str(base / "_registry")])
    return audit


class ReleasePipelineIntegrationTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-release-integration-")
        self.root = Path(self.temporary.name)
        self.environment = os.environ.copy()
        self.environment.update({"LC_ALL": "C", "I4R_RESERVE_GRACE": "0"})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_script(
        self,
        script: Path,
        *arguments: Path | str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["/bin/bash", str(script), *(str(value) for value in arguments)],
            text=True,
            capture_output=True,
            timeout=30,
            env=environment or self.environment,
        )
        return result

    def summarize_check(self, audit: Path, label: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(SUMMARIZE), str(audit), "--check",
             "--json", str(audit / f"work/{label}.json"),
             "--markdown", str(audit / f"work/{label}.md")],
            text=True, capture_output=True, timeout=30, env=self.environment,
        )

    def test_stage_success_is_complete_and_unmarked(self) -> None:
        audit = make_audit(self.root)
        phase_one = json.loads((audit / "run_metrics.json").read_text())["phase_one"]
        self.assertTrue(phase_one["exact_quorum"])
        self.assertTrue(phase_one["canonical_outputs"])
        self.assertEqual(phase_one["common_overlap"]["seconds"], 56)
        release = self.root / "stage-success"
        result = self.run_script(STAGE, audit, release)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((release / "report/REPORT_final.md").is_file())
        self.assertTrue((release / "work/analysis.py").is_file())
        self.assertTrue((release / "verification_summary.csv").is_file())
        self.assertTrue((release / "verification_manifest.json").is_file())
        self.assertFalse((release / "RELEASE_REJECTED").exists())

    def test_phase_one_sequential_auditors_are_rejected(self) -> None:
        audit = make_audit(self.root)
        ledger_path = audit / "invocations.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
        auditors = sorted((row for row in rows if row["role_class"] == "auditor"),
                          key=lambda row: row["role"])
        self.assertEqual(len(auditors), 5)
        for index, row in enumerate(auditors):
            row["started_at"] = f"2026-08-27T09:{index * 2:02d}:00Z"
            row["ended_at"] = f"2026-08-27T09:{index * 2 + 1:02d}:00Z"
            row["elapsed_seconds"] = 60
        write(ledger_path, "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
        result = self.summarize_check(audit, "sequential-phase-one")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Phase-1 five-way concurrency has no positive common overlap",
                      result.stdout + result.stderr)

    def test_phase_one_wrong_or_shared_output_path_is_rejected(self) -> None:
        audit = make_audit(self.root)
        ledger_path = audit / "invocations.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
        row = next(item for item in rows if item["role"] == "auditor_data_integrity")
        row["output_artifact"] = "report/01_reproduction.md"
        row["output_sha256"] = digest(audit / "report/01_reproduction.md")
        write(ledger_path, "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows))
        result = self.summarize_check(audit, "misrouted-phase-one")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "auditor_data_integrity has output_artifact='report/01_reproduction.md'; "
            "expected canonical output_artifact 'report/02_data_integrity.md'",
            result.stdout + result.stderr,
        )

    def test_opus_is_rejected_for_coordinator_and_every_phase_one_role(self) -> None:
        audit = make_audit(self.root)
        ledger_path = audit / "invocations.jsonl"
        original = ledger_path.read_text()
        roles = ["coordinator", "auditor_reproduction", "auditor_data_integrity",
                 "auditor_text_vs_data", "auditor_code_methods", "auditor_privacy_archive"]
        for index, role in enumerate(roles):
            with self.subTest(role=role):
                rows = [json.loads(line) for line in original.splitlines() if line.strip()]
                row = next(item for item in rows if item["role"] == role)
                row["requested_model"] = "opus-5"
                row["observed_model"] = "claude-opus-5-20260801"
                write(ledger_path, "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows))
                result = self.summarize_check(audit, f"opus-baseline-{index}")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("recorded request violates pipeline policy",
                              result.stdout + result.stderr)
        write(ledger_path, original)

    def test_ordinary_terra_calls_reject_max_effort(self) -> None:
        audit = make_audit(self.root)
        ledger_path = audit / "invocations.jsonl"
        original = ledger_path.read_text()
        for index, role in enumerate(("model_probe", "critic_stats")):
            with self.subTest(role=role):
                rows = [json.loads(line) for line in original.splitlines() if line.strip()]
                row = next(item for item in rows if item["role"] == role)
                self.assertEqual(row["reasoning_effort"], "high")
                row["reasoning_effort"] = "max"
                write(ledger_path, "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows))
                result = self.summarize_check(audit, f"max-ordinary-{index}")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("reasoning_effort='max', expected 'high'",
                              result.stdout + result.stderr)
        write(ledger_path, original)

    def test_targeted_adjudicator_requires_max_effort(self) -> None:
        audit = make_audit(self.root)
        ledger_path = audit / "invocations.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
        source = next(item for item in rows if item["role"] == "critic_stats")
        output = audit / "work/targeted-adjudicator.json"
        write(output, json.dumps({"status": "complete"}) + "\n")
        targeted = dict(source)
        targeted.update({
            "invocation_id": "integration-targeted-adjudicator",
            "role": "critic_synth", "role_class": "adjudicator", "reasoning_effort": "max",
            "output_artifact": output.relative_to(audit).as_posix(),
            "output_sha256": digest(output),
        })
        rows.append(targeted)
        write(ledger_path, "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows))
        accepted = self.summarize_check(audit, "targeted-max")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)

        targeted["reasoning_effort"] = "high"
        rows[-1] = targeted
        write(ledger_path, "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows))
        rejected = self.summarize_check(audit, "targeted-high")
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        self.assertIn("reasoning_effort='high', expected 'max'",
                      rejected.stdout + rejected.stderr)

    def test_round_snapshot_rejects_wrong_ordinary_effort(self) -> None:
        audit = make_audit(self.root)
        pointer = (audit / "critique/round_1/CURRENT_PROJECTION").read_text().strip()
        projection = json.loads(
            (audit / f"critique/round_1/projections/{pointer}/MANIFEST.json").read_text()
        )
        manifest_path = audit / f"critique/round_1/snapshots/{projection['input_sha256']}/MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["effort"], "high")
        manifest["effort"] = "max"
        write(manifest_path, json.dumps(manifest, indent=2) + "\n")
        result = subprocess.run(
            [sys.executable, str(VALIDATE_ROUNDS), str(audit), "--require-post-delta-gates"],
            text=True, capture_output=True, timeout=30, env=self.environment,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ordinary critic snapshot model/effort differs from policy",
                      result.stdout + result.stderr)

    def test_stage_missing_input_removes_empty_transaction(self) -> None:
        audit = make_audit(self.root)
        (audit / "report/ADJUDICATION.md").unlink()
        release = self.root / "stage-missing"
        result = self.run_script(STAGE, audit, release)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("missing report/ADJUDICATION.md", result.stdout)
        self.assertFalse(release.exists(), "preflight failure left a release directory")

    def test_stage_refuses_to_overwrite_existing_target(self) -> None:
        audit = make_audit(self.root)
        release = self.root / "stage-existing"
        write(release / "sentinel.txt", "preserve exactly\n")
        before = digest(release / "sentinel.txt")
        result = self.run_script(STAGE, audit, release)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(digest(release / "sentinel.txt"), before)
        self.assertEqual(sorted(path.name for path in release.iterdir()), ["sentinel.txt"])

    def test_stage_marks_privacy_rejection_and_never_reports_success(self) -> None:
        audit = make_audit(self.root)
        # Assemble the positive control so this test source itself contains no private path.
        private_path = "/Us" + "ers/realperson/private/input.csv"
        write(audit / "work/leak.R", f"source({private_path!r})\n")
        release = self.root / "stage-rejected"
        result = self.run_script(STAGE, audit, release)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("absolute path(s) naming a real account", result.stdout)
        self.assertNotIn("release staged clean", result.stdout)
        self.assertEqual((release / "RELEASE_REJECTED").read_text().strip(), "REJECTED_OR_INCOMPLETE")

    def test_stage_verification_csv_exception_does_not_admit_other_csv(self) -> None:
        audit = make_audit(self.root)
        (audit / "claims_script.R").unlink()
        write(audit / "claims_script.csv", "claim_id,value\nC-1,2\n")
        release = self.root / "stage-other-csv"
        result = self.run_script(STAGE, audit, release)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("canonical finding projections are missing or divergent",
                      result.stdout + result.stderr)

    def test_publish_success_is_exact_and_second_attempt_preserves_it(self) -> None:
        audit = make_audit(self.root)
        completed_root = self.root / "completed"
        destination = completed_root / "Journal-Sanitized/Paper-Sanitized"
        result = self.run_script(PUBLISH, audit, completed_root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        expected = {
            "comment.pdf",
            "claims_script.R",
            "run.json",
            "comment.json",
            "finalization_receipt.json",
            "release-manifest.json",
        }
        self.assertEqual({path.name for path in destination.iterdir()}, expected)
        manifest = json.loads((destination / "release-manifest.json").read_text())
        self.assertEqual(manifest["audit_id"], "audit-integration-001")
        self.assertEqual({row["file"] for row in manifest["files"]}, expected - {"release-manifest.json"})
        for row in manifest["files"]:
            artifact = destination / row["file"]
            self.assertEqual(row["sha256"], digest(artifact))
            self.assertEqual(row["bytes"], artifact.stat().st_size)
        receipt = json.loads((destination / "finalization_receipt.json").read_text())
        self.assertFalse(Path(receipt["registry"]["root"]).is_absolute())

        before = {path.name: digest(path) for path in destination.iterdir()}
        again = self.run_script(PUBLISH, audit, completed_root)
        self.assertNotEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual({path.name: digest(path) for path in destination.iterdir()}, before)

    def test_publish_rejects_stale_verification_before_creating_destination(self) -> None:
        audit = make_audit(self.root, paper="Paper-Stale")
        write(audit / "verification_summary.csv",
              "claim_id,key,verdict,detail\nC-1,fixture,PASS,post-verification edit\n")
        completed_root = self.root / "completed"
        destination = completed_root / "Journal-Sanitized/Paper-Stale"
        result = self.run_script(PUBLISH, audit, completed_root)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("does not match verification_manifest.json", result.stdout + result.stderr)
        self.assertFalse(destination.exists())
        parent = destination.parent
        self.assertFalse(parent.exists() and list(parent.glob(".Paper-Stale.publish.*")))

    def test_publish_requires_current_finalization_receipt(self) -> None:
        audit = make_audit(self.root, paper="Paper-No-Receipt")
        (audit / "finalization_receipt.json").unlink()
        completed_root = self.root / "completed"
        result = self.run_script(PUBLISH, audit, completed_root)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("finalization_receipt.json", result.stdout + result.stderr)
        self.assertFalse((completed_root / "Journal-Sanitized/Paper-No-Receipt").exists())

    def test_publish_rejects_tampered_immutable_round_projection(self) -> None:
        audit = make_audit(self.root, paper="Paper-Round-Tamper")
        projection_id = (audit / "critique/round_2/CURRENT_PROJECTION").read_text().strip()
        routing = audit / f"critique/round_2/projections/{projection_id}/ROUTING.json"
        write(routing, routing.read_text() + "\n")
        completed_root = self.root / "completed"
        result = self.run_script(PUBLISH, audit, completed_root)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("immutable", (result.stdout + result.stderr).lower())
        self.assertFalse((completed_root / "Journal-Sanitized/Paper-Round-Tamper").exists())

    def test_concurrent_publication_exposes_one_exact_destination(self) -> None:
        audit = make_audit(self.root, paper="Paper-Concurrent")
        completed_root = self.root / "completed"
        commands = [["/bin/bash", str(PUBLISH), str(audit), str(completed_root)] for _ in range(2)]
        processes = [subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=self.environment) for command in commands]
        outcomes = [process.communicate(timeout=30) + (process.returncode,) for process in processes]
        self.assertEqual(sorted(item[2] for item in outcomes).count(0), 1, outcomes)
        destination = completed_root / "Journal-Sanitized/Paper-Concurrent"
        self.assertEqual(len([path for path in destination.iterdir() if path.is_file()]), 6)
        self.assertFalse((destination.parent / "Paper-Concurrent.publish.lock").exists())
        self.assertFalse(list(destination.parent.glob(".Paper-Concurrent.publish.*")))

    def test_publish_mid_copy_failure_removes_transaction_and_destination(self) -> None:
        audit = make_audit(self.root, paper="Paper-Copy-Failure")
        completed_root = self.root / "completed"
        destination = completed_root / "Journal-Sanitized/Paper-Copy-Failure"

        fake_bin = self.root / "fake-bin"
        state = self.root / "copy-state"
        real_cp = shutil.which("cp")
        self.assertIsNotNone(real_cp)
        wrapper = fake_bin / "cp"
        write(
            wrapper,
            "#!/bin/sh\n"
            "if [ -e \"$I4R_TEST_CP_STATE\" ]; then exit 91; fi\n"
            ": > \"$I4R_TEST_CP_STATE\"\n"
            "exec \"$I4R_TEST_REAL_CP\" \"$@\"\n",
        )
        wrapper.chmod(0o755)
        environment = dict(self.environment)
        environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
        environment["I4R_TEST_CP_STATE"] = str(state)
        environment["I4R_TEST_REAL_CP"] = str(real_cp)

        result = self.run_script(PUBLISH, audit, completed_root, environment=environment)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(state.exists(), "fault injector did not reach the publication copy transaction")
        self.assertFalse(destination.exists(), "failed publication exposed a destination tree")
        parent = destination.parent
        self.assertFalse(parent.exists() and list(parent.glob(".Paper-Copy-Failure.publish.*")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
