#!/usr/bin/env python3
"""Build a content-bound, deterministic delta packet for routed critique rounds."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
from typing import Any


DOMAIN_PRIMARY_ROLE = {
    "statistics": "stats",
    "data": "data",
    "text": "fairness",
    "code_methods": "stats",
    "privacy_archive": "fairness",
    "framing": "fairness",
    "other": "fairness",
}
ROLE_ORDER = ("stats", "data", "fairness")
ROLE_PATTERNS = {
    "stats": re.compile(r"stat|model|infer|estimate|effect|p.?value|regress|analysis|power|interval", re.I),
    "data": re.compile(r"data|duplicate|missing|archive|file|variable|record|integrity|sample|participant|exclusion|outcome|manifest|repository", re.I),
    "fairness": re.compile(r"fair|framing|wording|impact|privacy|disclos|author|conclusion|proportion|licen|remed", re.I),
}


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_rows(path: pathlib.Path, raw: bytes | None = None) -> list[dict[str, Any]]:
    rows = []
    text = raw.decode("utf-8") if raw is not None else path.read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        value = strict_json(line.encode("utf-8"), f"{path}:{line_number}")
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: row is not an object")
        rows.append(value)
    ids = [str(row.get("id")) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: duplicate finding IDs")
    return rows


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[str] = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else key
            out.extend(changed_paths(before.get(key), after.get(key), path))
        return out
    if canonical(before) != canonical(after):
        return [prefix or "<root>"]
    return []


def routing_text(row: dict[str, Any]) -> str:
    evidence = row.get("evidence") or {}; remedy = row.get("remedy") or {}
    source = row.get("source_location") or {}; dependencies = row.get("dependencies") or {}
    values: list[Any] = [row.get("claim"), source.get("locator"), row.get("claim_ref"),
                         row.get("impact"), row.get("changes_conclusion_which"),
                         evidence.get("rerun_command"), evidence.get("tolerance_basis"),
                         remedy.get("class"), remedy.get("description"),
                         *(row.get("benign_explanations_ruled_out") or []),
                         *(dependencies.get("artifact_paths") or [])]
    return " ".join(str(value) for value in values if value is not None)


def specialist_scores(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Score a change set without turning every weak lexical match into another critic."""
    scores = {role: 0 for role in ROLE_ORDER}
    for row in rows:
        primary = DOMAIN_PRIMARY_ROLE.get(str(row.get("domain") or "other"), "fairness")
        scores[primary] += 8
        text = routing_text(row)
        for role, pattern in ROLE_PATTERNS.items():
            if pattern.search(text):
                scores[role] += 1
        if row.get("disclosure_flag") is True:
            scores["fairness"] += 8
        if row.get("changes_conclusion") is True:
            scores[primary] += 3
    return scores


def best_specialist(rows: list[dict[str, Any]]) -> str:
    scores = specialist_scores(rows)
    if not any(scores.values()):
        return "fairness"
    # ROLE_ORDER is the stable tie-breaker; domain weights normally make the choice unique.
    return max(ROLE_ORDER, key=lambda role: (scores[role], -ROLE_ORDER.index(role)))


def strict_json(raw: bytes, label: str) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in values:
            if key in out:
                raise ValueError(f"{label}: duplicate JSON key {key!r}")
            out[key] = value
        return out

    def constant(value: str) -> None:
        raise ValueError(f"{label}: non-finite JSON number {value}")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def validated_settled_facts(
    root: pathlib.Path,
    settled_path: pathlib.Path,
    *,
    registry_rows: list[dict[str, Any]],
    registry_sha256: str,
    report_sha256: str,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Validate the complete cache before allowing even one dependency reference through."""
    raw = settled_path.read_bytes()
    settled = strict_json(raw, str(settled_path))
    if not isinstance(settled, dict):
        raise ValueError("SETTLED.json must be an object")
    if settled.get("schema_version") != "2.0.0" or settled.get("invalidated") is not False:
        raise ValueError("SETTLED.json is invalidated or has an unsupported schema version")
    if settled.get("source_registry") != "work/findings.jsonl":
        raise ValueError("SETTLED.json is not bound to the canonical findings registry")
    if settled.get("source_registry_sha256") != registry_sha256:
        raise ValueError("SETTLED.json findings-registry hash does not match the current registry")
    if settled.get("report_path") != "report/REPORT_final.md":
        raise ValueError("SETTLED.json is not bound to the canonical report")
    if settled.get("report_sha256") != report_sha256:
        raise ValueError("SETTLED.json report hash does not match the current report")
    def bound_file(binding: Any, expected_path: str, label: str) -> pathlib.Path:
        if not isinstance(binding, dict) or binding.get("path") != expected_path:
            raise ValueError(f"SETTLED.json lacks its canonical {label} binding")
        path = root / expected_path
        if path.is_symlink() or not path.is_file() or binding.get("sha256") != sha256(path):
            raise ValueError(f"SETTLED.json {label} binding is stale")
        return path

    gate = settled.get("round_1_gate")
    if not isinstance(gate, dict):
        raise ValueError("SETTLED.json lacks a Round-1 completion gate binding")
    if gate.get("run_path") != "run.json":
        raise ValueError("SETTLED.json is not bound to canonical run.json")
    run_path = root / "run.json"
    if run_path.is_symlink() or not run_path.is_file():
        raise ValueError("SETTLED.json canonical run.json is missing or a symlink")
    status_path = bound_file(
        {"path": gate.get("round_status_path"), "sha256": gate.get("round_status_sha256")},
        "critique/round_1/ROUND_STATUS.json", "Round-1 runtime status")
    run = strict_json(run_path.read_bytes(), str(run_path))
    status = strict_json(status_path.read_bytes(), str(status_path))
    rounds = run.get("rounds") if isinstance(run, dict) else None
    round_rows = ([row for row in rounds if isinstance(row, dict) and row.get("round") == 1]
                  if isinstance(rounds, list) else [])
    if len(round_rows) != 1:
        raise ValueError("SETTLED.json Round-1 run binding has no unique Round 1")
    round_one = round_rows[0]
    run_projection = {
        "audit_id": run.get("audit_id"), "round": 1,
        "started_at": round_one.get("started_at"), "ended_at": round_one.get("ended_at"),
        "integrated_at": round_one.get("integrated_at"),
        "consistency_check_run": round_one.get("consistency_check_run"),
        "consistency_check_exit": round_one.get("consistency_check_exit"),
    }
    run_projection_hash = hashlib.sha256(json.dumps(
        run_projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    if (gate.get("round_projection_sha256") != run_projection_hash or
            round_one.get("started_at") != gate.get("started_at") or
            round_one.get("ended_at") != gate.get("ended_at") or
            round_one.get("integrated_at") != gate.get("integrated_at") or
            round_one.get("consistency_check_run") is not True or
            round_one.get("consistency_check_exit") != 0 or
            gate.get("consistency_check_exit") != 0):
        raise ValueError("SETTLED.json Round-1 completion/integration/consistency gate is invalid")
    expected_roles = status.get("expected_roles") if isinstance(status, dict) else None
    if (not isinstance(status, dict) or status.get("round") != 1 or
            status.get("audit_id") != run.get("audit_id") or status.get("outcome") != "complete" or
            status.get("exit_code") != 0 or status.get("quorum_met") is not True or
            expected_roles != ["stats", "data", "fairness", "completeness"] or
            status.get("completed_roles") != expected_roles or
            status.get("failed_roles") != []):
        raise ValueError("SETTLED.json Round-1 runtime quorum is no longer complete")

    invocation_binding = settled.get("invocation_ledger")
    if not isinstance(invocation_binding, dict) or invocation_binding.get("path") != "invocations.jsonl":
        raise ValueError("SETTLED.json lacks its canonical invocation-ledger binding")
    invocation_path = root / "invocations.jsonl"
    if invocation_path.is_symlink() or not invocation_path.is_file():
        raise ValueError("SETTLED.json canonical invocation ledger is missing or a symlink")
    invocation_bytes = invocation_path.read_bytes()
    prefix_bytes = invocation_binding.get("bound_prefix_bytes")
    prefix_sha256 = invocation_binding.get("bound_prefix_sha256")
    records_at_generation = invocation_binding.get("records_at_generation")
    if (not isinstance(prefix_bytes, int) or isinstance(prefix_bytes, bool) or prefix_bytes <= 0 or
            prefix_bytes > len(invocation_bytes) or
            not isinstance(prefix_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", prefix_sha256) or
            hashlib.sha256(invocation_bytes[:prefix_bytes]).hexdigest() != prefix_sha256 or
            not isinstance(records_at_generation, int) or records_at_generation <= 0):
        raise ValueError("SETTLED.json invocation ledger bound prefix is stale")
    bound_lines = [line for line in invocation_bytes[:prefix_bytes].splitlines() if line.strip()]
    if len(bound_lines) != records_at_generation:
        raise ValueError("SETTLED.json invocation ledger bound-prefix record count is invalid")
    invocations: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(invocation_bytes.splitlines(), 1):
        if not line.strip():
            continue
        invocation = strict_json(line, f"{invocation_path}:{line_number}")
        invocation_id = invocation.get("invocation_id") if isinstance(invocation, dict) else None
        if not isinstance(invocation_id, str) or not invocation_id or invocation_id in invocations:
            raise ValueError("SETTLED.json invocation ledger has missing or duplicate invocation IDs")
        invocations[invocation_id] = invocation
    facts = settled.get("facts")
    if not isinstance(facts, list):
        raise ValueError("SETTLED.json facts must be an array")

    registry_by_id = {row["id"]: row for row in registry_rows}
    by_id: dict[str, dict[str, Any]] = {}
    for index, fact in enumerate(facts):
        label = f"SETTLED.json facts[{index}]"
        if not isinstance(fact, dict):
            raise ValueError(f"{label} must be an object")
        fact_id = fact.get("id")
        if not isinstance(fact_id, str) or not fact_id.strip() or fact_id in by_id:
            raise ValueError(f"{label} has a missing or duplicate fact id")
        registry_row = registry_by_id.get(fact_id)
        if registry_row is None:
            raise ValueError(f"{label} does not join to the canonical findings registry")
        if fact.get("coordinator_approved") is not True:
            raise ValueError(f"{label} lacks coordinator approval")
        row_settled = registry_row.get("settled")
        if (registry_row.get("coordinator_approved") is not True or
                (registry_row.get("ledger_disposition") or {}).get("status") != "INCLUDED" or
                str(registry_row.get("status") or "").lower() not in
                {"confirmed", "verified", "accepted"} or
                not isinstance(row_settled, dict) or row_settled.get("approved") is not True):
            raise ValueError(f"{label} is not coordinator-approved and included in the registry")
        if fact.get("valid_for_report_hash") != report_sha256:
            raise ValueError(f"{label} is not valid for the current report hash")
        if row_settled.get("valid_for_report_hash") != report_sha256:
            raise ValueError(f"{label} registry row is not valid for the current report hash")
        if fact.get("claim") != registry_row.get("claim"):
            raise ValueError(f"{label} claim does not match the canonical registry")
        fingerprint = fact.get("environment_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError(f"{label} lacks a dependency/environment fingerprint")
        if fingerprint != (registry_row.get("dependencies") or {}).get("environment_fingerprint"):
            raise ValueError(f"{label} dependency fingerprint does not match the canonical registry")

        confirmations = fact.get("confirmations")
        if not isinstance(confirmations, list) or len(confirmations) < 2:
            raise ValueError(f"{label} lacks two independent confirmations")
        reviewers: set[str] = set()
        invocation_ids: set[str] = set()
        evidence_records: set[tuple[str, str]] = set()
        families: set[str] = set()
        for confirmation in confirmations:
            if not isinstance(confirmation, dict):
                raise ValueError(f"{label} has a malformed confirmation")
            reviewer = confirmation.get("reviewer_id")
            invocation_id = confirmation.get("invocation_id")
            evidence_record = confirmation.get("evidence_record")
            family = confirmation.get("model_family")
            outcome = str(confirmation.get("outcome") or "").lower()
            if (not isinstance(reviewer, str) or not reviewer.strip() or reviewer in reviewers or
                    not isinstance(invocation_id, str) or not invocation_id.strip() or
                    not isinstance(evidence_record, dict) or
                    not isinstance(family, str) or not family.strip() or
                    family.strip().lower() in {"unknown", "unavailable"} or
                    outcome not in {"confirmed", "pass", "verified"}):
                raise ValueError(f"{label} has non-independent or incomplete confirmation evidence")
            evidence_path = evidence_record.get("path")
            evidence_hash = evidence_record.get("sha256")
            if (not isinstance(evidence_path, str) or not evidence_path or
                    not isinstance(evidence_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", evidence_hash)):
                raise ValueError(f"{label} has a malformed reviewer evidence record")
            invocation = invocations.get(invocation_id)
            if (not isinstance(invocation, dict) or invocation.get("status") != "completed" or
                    invocation.get("provider_call") is not True or
                    invocation.get("role_class") not in {"auditor", "critic", "adjudicator"} or
                    invocation.get("model_source") != "observed" or
                    invocation.get("round") not in {0, 1} or invocation.get("role") != reviewer or
                    str(invocation.get("model_family") or "").strip().lower() != family.strip().lower() or
                    invocation.get("output_artifact") != evidence_path or
                    invocation.get("output_sha256") != evidence_hash):
                raise ValueError(f"{label} confirmation does not match canonical invocation {invocation_id}")
            reviewer_lexical = root / evidence_path
            reviewer_output = reviewer_lexical.resolve()
            try:
                reviewer_output.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"{label} reviewer evidence escapes the audit root") from exc
            if (reviewer_lexical.is_symlink() or not reviewer_output.is_file() or
                    sha256(reviewer_output) != evidence_hash):
                raise ValueError(f"{label} reviewer evidence record is stale: {evidence_path}")
            reviewers.add(reviewer)
            invocation_ids.add(invocation_id)
            evidence_records.add((evidence_path, evidence_hash))
            families.add(family.strip().lower())
        if (len(reviewers) < 2 or len(invocation_ids) < 2 or len(evidence_records) < 2 or
                len(families) < 2):
            raise ValueError(
                f"{label} confirmations do not span two reviewers, invocations, evidence records, and model families")
        row_confirmations = [{
            "reviewer_id": item.get("reviewer_id"),
            "invocation_id": item.get("invocation_id"),
            "evidence_record": item.get("evidence_record"),
            "model_family": item.get("model_family"),
            "outcome": str(item.get("outcome") or "").lower(),
        } for item in (registry_row.get("verifications") or [])
            if isinstance(item, dict) and str(item.get("outcome") or "").lower()
            in {"confirmed", "pass", "verified"}]
        fact_confirmations = [{
            "reviewer_id": item.get("reviewer_id"),
            "invocation_id": item.get("invocation_id"),
            "evidence_record": item.get("evidence_record"),
            "model_family": item.get("model_family"),
            "outcome": str(item.get("outcome") or "").lower(),
        } for item in confirmations]
        if fact_confirmations != row_confirmations:
            raise ValueError(f"{label} confirmations do not match the canonical registry")

        evidence = fact.get("evidence")
        hashes = evidence.get("source_hashes") if isinstance(evidence, dict) else None
        command = evidence.get("rerun_command") if isinstance(evidence, dict) else None
        if not isinstance(command, str) or not command.strip() or not isinstance(hashes, list) or not hashes:
            raise ValueError(f"{label} lacks rerunnable, source-hashed evidence")
        seen_paths: set[str] = set()
        checked: list[dict[str, str]] = []
        for item in hashes:
            if not isinstance(item, dict):
                raise ValueError(f"{label} has malformed source-hash evidence")
            relative, expected = item.get("path"), item.get("sha256")
            if (not isinstance(relative, str) or not relative or relative in seen_paths or
                    not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
                raise ValueError(f"{label} has duplicate or malformed source-hash evidence")
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"{label} evidence path escapes the audit root: {relative}") from exc
            if not candidate.is_file() or sha256(candidate) != expected:
                raise ValueError(f"{label} evidence hash is stale or invalid: {relative}")
            seen_paths.add(relative)
            checked.append({"path": relative, "sha256": expected})
        row_evidence = registry_row.get("evidence") or {}
        if (checked != row_evidence.get("source_hashes") or
                command.strip() != str(row_evidence.get("rerun_command") or "").strip()):
            raise ValueError(f"{label} evidence does not match the canonical registry")
        copy = dict(fact)
        copy["evidence"] = dict(evidence, source_hashes=checked)
        by_id[fact_id] = copy
    return by_id, hashlib.sha256(raw).hexdigest()


def section(text: str, name: str) -> str:
    lines = text.splitlines()
    start = None
    level = None
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match and re.search(name, match.group(2), re.I):
            start, level = index + 1, len(match.group(1))
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        match = re.match(r"^(#{1,6})\s+", lines[index])
        if match and len(match.group(1)) <= int(level):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def sentences(text: str) -> list[str]:
    plain = re.sub(r"\s+", " ", re.sub(r"[`*_]", "", text)).strip()
    return [piece.strip() for piece in re.split(r"(?<=[.!?])\s+", plain) if piece.strip()]


def sequence_changes(before: list[str], after: list[str]) -> list[dict[str, Any]]:
    changes = []
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for operation, i1, i2, j1, j2 in matcher.get_opcodes():
        if operation != "equal":
            changes.append({"operation": operation, "before": before[i1:i2], "after": after[j1:j2]})
    return changes


def appendix_rows(text: str) -> dict[str, str]:
    body = section(text, r"appendix\s+a")
    rows: dict[str, str] = {}
    for line in body.splitlines():
        if not line.lstrip().startswith("|") or re.match(r"^\s*\|?\s*:?-+", line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells:
            continue
        match = re.fullmatch(r"(?:F)?([1-9][0-9]*)", cells[0], re.I)
        if match:
            rows[f"F{int(match.group(1))}"] = re.sub(r"\s+", " ", line.strip())
    return rows


def section_changes(before: str, after: str) -> list[str]:
    left, right = report_blocks(before), report_blocks(after)
    return sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))


def report_blocks(text: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    current = "Preamble"
    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            title = match.group(2)
            number = re.search(r"(?:Section\s+)?(\d+(?:\.\d+)*)", title, re.I)
            current = f"Section {number.group(1)}" if number else title
            out.setdefault(current, []).append(line)
        else:
            out.setdefault(current, []).append(line)
    return {key: "\n".join(value).strip() for key, value in out.items()}


def atomic_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--before-report", type=pathlib.Path, required=True)
    parser.add_argument("--after-report", type=pathlib.Path, required=True)
    parser.add_argument("--before-findings", type=pathlib.Path, required=True)
    parser.add_argument("--after-findings", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    if args.round < 2:
        raise SystemExit("diff packets are for round 2 or later")
    root = args.audit_dir.resolve()
    before_report = args.before_report.resolve()
    after_report = args.after_report.resolve()
    before_findings = args.before_findings.resolve()
    after_findings = (args.after_findings or root / "work/findings.jsonl").resolve()
    if after_report != root / "report/REPORT_final.md":
        raise SystemExit("--after-report must be the canonical report/REPORT_final.md")
    if after_findings != root / "work/findings.jsonl":
        raise SystemExit("--after-findings must be the canonical work/findings.jsonl")
    for path in (before_report, after_report, before_findings, after_findings):
        if not path.is_file():
            raise SystemExit(f"required delta input is missing: {path}")
        if path.is_symlink():
            raise SystemExit(f"delta input must be a regular audit-local file, not a symlink: {path}")
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise SystemExit(f"delta input escapes the audit directory: {path}") from exc
    # Read every content-addressed input exactly once. A file changing between a hash read and
    # a later parse must never produce a packet whose declared digest describes different bytes.
    raw = {path: path.read_bytes() for path in (before_report, after_report, before_findings, after_findings)}
    digest = {path: hashlib.sha256(raw[path]).hexdigest() for path in raw}
    no_changes = (digest[before_report] == digest[after_report] and
                  digest[before_findings] == digest[after_findings])

    here = pathlib.Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    sys.path.insert(0, str(here.parent / "schemas/tools"))
    import validate_json_schema
    import validate_v2
    finding_schema = here.parent / "schemas/v2/finding-row.schema.json"
    before_rows, after_rows = load_rows(before_findings, raw[before_findings]), load_rows(after_findings, raw[after_findings])
    schema_errors = []
    for label, rows in (("before", before_rows), ("after", after_rows)):
        for index, row in enumerate(rows, 1):
            schema_errors.extend(
                f"{label} line {index}{error}"
                for error in validate_json_schema.errors_for_value(row, finding_schema)
            )
            try:
                validate_v2.validate_finding_row(row, f"{label} line {index}")
            except validate_v2.ValidationError as exc:
                schema_errors.append(str(exc))
    if schema_errors:
        raise SystemExit("delta findings failed schema validation:\n  " + "\n  ".join(schema_errors[:40]))

    before_by_id = {row["id"]: row for row in before_rows}
    after_by_id = {row["id"]: row for row in after_rows}
    changed = sorted(
        finding_id for finding_id in set(before_by_id) | set(after_by_id)
        if canonical(before_by_id.get(finding_id)) != canonical(after_by_id.get(finding_id))
    )
    changed_claims = [{
        "finding_id": finding_id,
        "change": ("added" if finding_id not in before_by_id else
                   "removed" if finding_id not in after_by_id else "modified"),
        "before": before_by_id.get(finding_id),
        "after": after_by_id.get(finding_id),
        "changed_fields": changed_paths(before_by_id.get(finding_id), after_by_id.get(finding_id)),
    } for finding_id in changed]
    # Removed findings retain their before-body for routing and review; otherwise deletion
    # silently erases the very domain/risk metadata needed to choose the specialist.
    changed_rows = [after_by_id.get(finding_id) or before_by_id[finding_id] for finding_id in changed]
    domains = sorted({row.get("domain", "other") for row in changed_rows})
    types_set: set[str] = set()
    for row in changed_rows:
        types_set.add(str(row.get("impact") or "other"))
        if row.get("changes_conclusion"): types_set.add("conclusion_changing")
        if row.get("disclosure_flag"): types_set.add("disclosure_critical")
    types = sorted(types_set)
    risk_tags = sorted(set(domains + types))

    before_text = raw[before_report].decode("utf-8")
    after_text = raw[after_report].decode("utf-8")
    changed_section_names = section_changes(before_text, after_text)
    before_blocks, after_blocks = report_blocks(before_text), report_blocks(after_text)
    changed_section_bodies = [{"section": name, "before": before_blocks.get(name),
                               "after": after_blocks.get(name)} for name in changed_section_names]
    # Later rounds always receive completeness in the runtime. Ordinarily this packet chooses
    # exactly one best specialist. Extra specialists are accuracy-safe only for critical changes
    # spanning multiple domains; ordinary cross-domain edits do not silently restore a full round.
    role_scores = specialist_scores(changed_rows)
    selected = {best_specialist(changed_rows)}
    critical_rows = [row for row in changed_rows
                     if row.get("changes_conclusion") is True or row.get("disclosure_flag") is True]
    critical_domains = sorted({str(row.get("domain") or "other") for row in critical_rows})
    if len(critical_domains) > 1:
        for domain in critical_domains:
            selected.add(best_specialist([row for row in critical_rows
                                          if str(row.get("domain") or "other") == domain]))
    recommended = [role for role in ROLE_ORDER if role in selected]
    routing_mode = ("multi_domain_critical" if len(recommended) > 1
                    else "single_best_specialist")
    dependencies = [{
        "finding_id": row["id"],
        "finding_ids": (row.get("dependencies") or {}).get("finding_ids", []),
        "artifact_paths": (row.get("dependencies") or {}).get("artifact_paths", []),
        "environment_fingerprint": (row.get("dependencies") or {}).get("environment_fingerprint"),
    } for row in changed_rows]

    before_appendix, after_appendix = appendix_rows(before_text), appendix_rows(after_text)
    appendix_changes = [{"finding_id": finding_id, "before": before_appendix.get(finding_id),
                         "after": after_appendix.get(finding_id)}
                        for finding_id in sorted(set(before_appendix) | set(after_appendix))
                        if before_appendix.get(finding_id) != after_appendix.get(finding_id)]

    settled_refs = []
    settled_json_sha256 = None
    settled_path = root / "SETTLED.json"
    if settled_path.is_file():
        if settled_path.is_symlink():
            raise SystemExit("SETTLED.json must be a regular audit-local file, not a symlink")
        try:
            settled_by_id, settled_json_sha256 = validated_settled_facts(
                root, settled_path,
                registry_rows=after_rows,
                registry_sha256=digest[after_findings],
                report_sha256=digest[after_report],
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise SystemExit(f"SETTLED.json failed closed validation: {exc}") from exc
        referenced_ids = sorted({finding_id for item in dependencies
                                 for finding_id in item["finding_ids"]})
        for fact_id in referenced_ids:
            fact = settled_by_id.get(fact_id)
            if fact is not None:
                settled_refs.append({
                    "fact_id": fact_id,
                    "source_hashes": fact["evidence"]["source_hashes"],
                    "rerun_command": fact["evidence"]["rerun_command"],
                    "confirmations": fact["confirmations"],
                    "coordinator_approved": True,
                    "environment_fingerprint": fact["environment_fingerprint"],
                    "valid_for_report_hash": fact["valid_for_report_hash"],
                })

    run_path = root / "run.json"
    audit_id = root.name
    if run_path.is_file():
        run_value = strict_json(run_path.read_bytes(), str(run_path))
        if not isinstance(run_value, dict):
            raise SystemExit("run.json must contain an object")
        audit_id = run_value.get("audit_id") or audit_id
    output = args.output or root / f"critique/DIFF_MANIFEST_round_{args.round}.json"
    packet = {
        "packet_version": "i4r-diff-packet-2",
        "audit_id": audit_id,
        "round": args.round,
        "change_state": "no_changes" if no_changes else "changed",
        "input_paths": {
            "before_report": before_report.relative_to(root).as_posix(),
            "after_report": after_report.relative_to(root).as_posix(),
            "before_findings": before_findings.relative_to(root).as_posix(),
            "after_findings": after_findings.relative_to(root).as_posix(),
        },
        "inputs": {
            "before_report_sha256": digest[before_report],
            "after_report_sha256": digest[after_report],
            "before_findings_sha256": digest[before_findings],
            "after_findings_sha256": digest[after_findings],
        },
        "changed_claim_ids": changed,
        "changed_claims": changed_claims,
        "removed_claim_ids": sorted(set(before_by_id) - set(after_by_id)),
        "changed_sections": changed_section_names,
        "changed_section_bodies": changed_section_bodies,
        "abstract_sentence_changes": sequence_changes(
            sentences(section(before_text, r"abstract")), sentences(section(after_text, r"abstract"))),
        "appendix_row_changes": appendix_changes,
        "dependencies": dependencies,
        "settled_fact_references": settled_refs,
        "settled_cache_sha256": settled_json_sha256,
        "domains": domains,
        "changed_claim_types": types,
        "risk_tags": risk_tags,
        "critical_domains": critical_domains,
        "routing_mode": routing_mode,
        "recommended_roles": recommended,
        "role_scores": role_scores,
        "required_global_gates": ["consistency", "privacy", "checklist", "archive"],
    }
    atomic_json(output.resolve(), packet)
    print(f"wrote {output.resolve()} ({len(changed)} changed claim IDs; "
          f"state={packet['change_state']}; roles={','.join(recommended)},completeness)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
