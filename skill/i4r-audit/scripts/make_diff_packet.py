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
import shlex
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
        raise ValueError("SETTLED.json findings-registry hash does not match the current registry (the "
                         "registry changed since the cache was built)")
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
    live_metadata_2_1 = (
        isinstance(run, dict)
        and run.get("schema_version") == "2.1.0"
        and not run.get("normalization")
    )
    rounds = run.get("rounds") if isinstance(run, dict) else None
    # A settled fact may cite a reviewer call from any INTEGRATED round, matching make_settled.sh.
    # Restricting it to rounds 0-1 forced later rounds' literal confirmations of corrected claims
    # to stay unbound, which is how the first audit promoted two facts on OVERSTATED-derived records.
    integrated_rounds = {0, 1}
    for _row in (rounds or []):
        if isinstance(_row, dict) and isinstance(_row.get("round"), int) and _row.get("state") == "integrated":
            integrated_rounds.add(_row["round"])
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

    # Post-fan-out verifier (2.6.4): admitted once over the whole ledger, and only when it holds a
    # role_class "verifier" row, exactly as make_settled.sh does. A settled fact may cite a
    # verifier call only if the runner's record is admissible and the call CONFIRMED the fact's
    # current claim text; make_settled.sh never promotes anything else, so a problem here fails
    # the cache closed. (No timing bound, matching this check's existing round-membership rule.)
    verifier_state: dict[str, Any] = {}

    def verifier_problem(invocation: dict[str, Any], registry_row: dict[str, Any]) -> str | None:
        if not verifier_state:
            sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
            import verifier_contract
            verdicts, errors = verifier_contract.collect_verdicts(root, list(invocations.values()))
            verifier_state.update(contract=verifier_contract, verdicts=verdicts, errors=errors, roles=None)
        contract = verifier_state["contract"]
        if verifier_state["errors"]:
            return "verifier record is not admissible: " + "; ".join(verifier_state["errors"])
        if verifier_state["roles"] is None:
            verifier_state["roles"] = contract.required_roles(contract.load_snapshot_policy(root))
        return contract.confirmation_problem(registry_row, str(invocation.get("invocation_id")),
                                             verifier_state["roles"], verifier_state["verdicts"])

    def identity_is_admissible(invocation: dict[str, Any]) -> bool:
        """Apply the canonical served-identity rule for the audit metadata generation.

        Live metadata 2.1 records may truthfully bind a reviewer to the requested route when
        the adapter exposes no served deployment ID. Historical and normalized records retain
        the observed-identity requirement under which their settled caches were created.
        """
        served = invocation.get("served_model") or invocation.get("observed_model")
        if served:
            return (
                invocation.get("model_source") == "observed"
                and invocation.get("identity_assurance", "observed") == "observed"
            )
        return (
            live_metadata_2_1
            and invocation.get("identity_assurance") == "requested_only"
            and isinstance(invocation.get("requested_model"), str)
            and bool(invocation["requested_model"].strip())
            and invocation.get("model_source") in {"declared", "unavailable"}
        )

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
            # Follow a cache replay to the provider call whose bytes it re-serves. A replay is
            # bookkeeping: any resume -- a killed Phase 1, a recovered strand, a rerun after a
            # fix -- replays the roles that already completed, so a legitimately cached report
            # carries a `cache-` id here. make_settled.sh resolves the same pointer for the same
            # reason; both checks must agree or a settled cache passes one and fails the other.
            seen_replays = set()
            while isinstance(invocation, dict) and invocation.get("attempt_kind") == "cache_replay":
                source = invocation.get("source_invocation_id")
                if not source or source in seen_replays:
                    raise ValueError(
                        f"{label} cites a cache replay with no resolvable source call "
                        f"{invocation_id}")
                seen_replays.add(source)
                invocation = invocations.get(source)
            if (not isinstance(invocation, dict) or invocation.get("status") != "completed" or
                    invocation.get("provider_call") is not True or
                    invocation.get("role_class") not in {"auditor", "critic", "adjudicator", "verifier"} or
                    not identity_is_admissible(invocation) or
                    invocation.get("round") not in integrated_rounds or invocation.get("role") != reviewer or
                    str(invocation.get("model_family") or "").strip().lower() != family.strip().lower() or
                    invocation.get("output_artifact") != evidence_path or
                    invocation.get("output_sha256") != evidence_hash):
                raise ValueError(f"{label} confirmation does not match canonical invocation {invocation_id}")
            if invocation.get("role_class") == "verifier":
                problem = verifier_problem(invocation, registry_row)
                if problem:
                    raise ValueError(f"{label} verifier confirmation is not admissible: {problem}")
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


def load_frozen_manifest(root: pathlib.Path, path: pathlib.Path) -> dict[str, Any]:
    """A round snapshot's frozen DIFF_MANIFEST.json, refused by name when it cannot be read as one."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"frozen diff packet {path.relative_to(root)} is unreadable: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("input_paths") or {}, dict) \
            or not isinstance(value.get("inputs") or {}, dict) \
            or not all(isinstance(v, str) for v in (value.get("input_paths") or {}).values()):
        raise SystemExit(f"frozen diff packet {path.relative_to(root)} is not a diff packet object")
    # the bindings it exists for: an empty or partial manifest would switch the frozen-binding guard off silently
    paths, inputs = value.get("input_paths") or {}, value.get("inputs") or {}
    for key in ("before_report", "before_findings"):
        digest_value = inputs.get(f"{key}_sha256")
        if not (isinstance(paths.get(key), str) and paths[key]) or not (
                isinstance(digest_value, str) and re.fullmatch(r"[0-9a-f]{64}", digest_value)):
            raise SystemExit(f"frozen diff packet {path.relative_to(root)} does not bind {key} (input_paths.{key} "
                             f"and a 64-hex inputs.{key}_sha256 are required); restore it from the round's snapshot")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--before-report", type=pathlib.Path, required=True)
    parser.add_argument("--after-report", type=pathlib.Path, required=True)
    parser.add_argument("--before-findings", type=pathlib.Path, required=True)
    parser.add_argument("--after-findings", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--no-remedy-file", action="store_true",
                        help="a check-only caller (verify_diff_packet): refuse without writing the expected file")
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
    if before_report == after_report or (before_report.is_file() and before_report.samefile(after_report)):
        raise SystemExit(f"--before-report must be round {args.round - 1}'s step-1 copy "
                         f"critique/round_{args.round - 1}/REPORT_BEFORE_INTEGRATION.md, not the canonical report")
    if after_findings != root / "work/findings.jsonl":
        raise SystemExit("--after-findings must be the canonical work/findings.jsonl")
    for path in (before_report, after_report, before_findings, after_findings):
        if not path.is_file():
            prior_dir = root / f"critique/round_{args.round - 1}"
            integrated = (prior_dir / "INTEGRATION_SUMMARY.json").is_file()
            if path.parent == prior_dir and path.name in ("REPORT_BEFORE_INTEGRATION.md",
                                                          "FINDINGS_BEFORE_INTEGRATION.jsonl"):
                # the round ended the audit and was never integrated (e.g. correcting a published comment): its
                # step-1 copies are the files that round's critics reviewed, taken from its current snapshot
                snapshot_source = None
                pointer_file = prior_dir / "CURRENT_INVOCATION"
                if pointer_file.is_file():
                    try:
                        rel = json.loads((prior_dir / "invocations" / pointer_file.read_text(encoding="utf-8").strip()
                                          / "snapshot.json").read_text(encoding="utf-8")).get("snapshot_rel")
                        if isinstance(rel, str) and (root / rel / "source/REPORT_TO_REVIEW.md").is_file():
                            snapshot_source = root / rel / "source"
                    except (OSError, ValueError, AttributeError):
                        snapshot_source = None
                if snapshot_source is None:
                    found = sorted(prior_dir.glob("snapshots/*/source/REPORT_TO_REVIEW.md"))
                    snapshot_source = found[-1].parent if len(found) == 1 else None
                if snapshot_source is not None:
                    sources = {"REPORT_BEFORE_INTEGRATION.md": snapshot_source / "REPORT_TO_REVIEW.md",
                               "FINDINGS_BEFORE_INTEGRATION.jsonl": snapshot_source / "context/work/findings.jsonl"}
                    # integrated already: integrate_round's backup of this run is the registry as reviewed (it keeps
                    # what the snapshot does, plus nothing apply changed); the report is never touched by apply
                    backup_copy = root / f"work/findings.before_round{args.round - 1}_integration.jsonl"
                    marker_copy = backup_copy.with_suffix(".invocation")
                    pointer_id = pointer_file.read_text(encoding="utf-8").strip() if pointer_file.is_file() else None
                    if integrated and backup_copy.is_file() and (not marker_copy.is_file() or
                                                                 marker_copy.read_text(encoding="utf-8").strip() == pointer_id):
                        sources["FINDINGS_BEFORE_INTEGRATION.jsonl"] = backup_copy
                    # a step-1 file this round's frozen packet already binds must get exactly those bytes back
                    for manifest_path in sorted(root.glob(f"critique/round_{args.round}/snapshots/*/source/context/DIFF_MANIFEST.json")):
                        try:
                            manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            continue
                        if not isinstance(manifest_doc, dict) or not isinstance(manifest_doc.get("input_paths") or {}, dict) \
                                or not isinstance(manifest_doc.get("inputs") or {}, dict):
                            continue   # malformed: load_frozen_manifest refuses it by name when the packet is built
                        for key, target in (("before_report", "REPORT_BEFORE_INTEGRATION.md"),
                                            ("before_findings", "FINDINGS_BEFORE_INTEGRATION.jsonl")):
                            bound_rel = (manifest_doc.get("input_paths") or {}).get(key)
                            bound_hash = (manifest_doc.get("inputs") or {}).get(f"{key}_sha256")
                            if bound_rel != f"critique/round_{args.round - 1}/{target}" or not bound_hash:
                                continue
                            candidates = [sources[target], backup_copy,
                                          *sorted(prior_dir.glob(f"snapshots/*/source/{'REPORT_TO_REVIEW.md' if key == 'before_report' else 'context/work/findings.jsonl'}")),
                                          *sorted(root.glob(f"critique/round_*/post_delta/*/payload/{bound_rel}"))]
                            match = next((c for c in candidates if c.is_file()
                                          and hashlib.sha256(c.read_bytes()).hexdigest() == bound_hash), None)
                            if match is not None:
                                sources[target] = match
                    raise SystemExit(
                        f"required delta input is missing: {path}. Round {args.round - 1}'s step-1 copies were not made"
                        + (" before it was integrated" if integrated else " (it was never integrated)")
                        + ". Make them from the files that round reviewed, then re-run:\n"
                        + "".join(
                            f"  cp -f {shlex.quote(str(source))} {shlex.quote(str(prior_dir / target))} && "
                            f"chmod u+w {shlex.quote(str(prior_dir / target))}\n"
                            for target, source in sources.items() if not (prior_dir / target).is_file())
                        + "(the sources are read-only; the copies must stay writable for a later renumber)")
                found_snapshots = sorted(prior_dir.glob("snapshots/*/source/REPORT_TO_REVIEW.md"))
                if not found_snapshots:
                    why = "it has no snapshot to copy from (restore critique/round_N/snapshots/ from a backup of the audit)"
                elif pointer_file.is_file():
                    why = (f"its CURRENT_INVOCATION names {pointer_file.read_text(encoding='utf-8').strip()!r}, whose "
                           "invocations/<id>/snapshot.json is missing or unusable (restore it from a backup, or correct the id)")
                else:
                    why = (f"its reviewed snapshot is not pinned (several snapshots, no CURRENT_INVOCATION): write the "
                           f"reviewed invocation id to critique/round_{args.round - 1}/CURRENT_INVOCATION (see its "
                           "invocations/ folder), then re-run for the exact commands")
                raise SystemExit(
                    f"required delta input is missing: {path}. Round {args.round - 1}'s step-1 copies were not made, and "
                    f"{why}; or copy that round's reviewed snapshot files "
                    f"(critique/round_{args.round - 1}/snapshots/<its current invocation>/source/REPORT_TO_REVIEW.md and "
                    "source/context/work/findings.jsonl) to REPORT_BEFORE_INTEGRATION.md and "
                    "FINDINGS_BEFORE_INTEGRATION.jsonl in that round's folder, then re-run")
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
    # The round-N packet describes round N-1's integration, so its "before" registry must be what round
    # N-1's critics reviewed. integrate_round keeps that as a write-once backup; a step-1 copy taken
    # late (after apply) hides the integration's rewordings, withdrawals and impact changes, and routes
    # no critic to them. The backup keeps the ids its round was reviewed under, so it is translated
    # through the renumbers recorded after that invocation before whole rows are compared.
    prior = args.round - 1
    # The round-N packet describes round N-1's integration. Before-files from another round's folder
    # produce a packet for the wrong delta, which every later check then accepts.
    for label, path in (("--before-report", before_report), ("--before-findings", before_findings)):
        rel = tuple(part.lower() for part in path.relative_to(root).parts)   # APFS is case-insensitive
        if len(rel) > 2 and rel[0] == "critique" and rel[1].startswith("round_") and rel[1] != f"round_{prior}":
            raise SystemExit(
                f"{label} {path.relative_to(root)} is from {rel[1]}, but --round {args.round} describes round "
                f"{prior}'s integration: use critique/round_{prior}/REPORT_BEFORE_INTEGRATION.md and "
                f"critique/round_{prior}/FINDINGS_BEFORE_INTEGRATION.jsonl")
    # Marker contents are masked: the documented renumbers (withdrawal, late) rewrite REPORT_BEFORE_INTEGRATION.md
    # with --also, which in a report snapshot changes only the markers (draft ids like CM-F2 included). Prose
    # is compared exactly: "Table F3" -> "Table F4" is a real edit the packet must carry.
    def id_masked(data):
        return hashlib.sha256(re.sub(rb"<!--\s*(finding|tested-negative|merged):[^\s>]+(?:->[^\s>]+)?\s*-->",
                                     rb"<!-- \1:# -->", data)).hexdigest()
    # the snapshot round {prior}'s CURRENT invocation reviewed: a refused rerun of a finished round still writes a
    # snapshot of whatever the report was then, which no critic saw (older audits: every snapshot)
    reviewed_pinned = False   # True when the reviewed snapshot is the one the round's current invocation recorded

    def reviewed_snapshot_reports() -> list:
        nonlocal_pin = False
        pointer = root / f"critique/round_{prior}/CURRENT_INVOCATION"
        if pointer.is_file():
            record = root / f"critique/round_{prior}/invocations/{pointer.read_text(encoding='utf-8').strip()}/snapshot.json"
            try:
                relative = json.loads(record.read_text(encoding="utf-8")).get("snapshot_rel")
            except (OSError, ValueError):
                relative = None
            if isinstance(relative, str) and (root / relative / "source/REPORT_TO_REVIEW.md").is_file():
                return [root / relative / "source/REPORT_TO_REVIEW.md"], True
        return sorted(r for r in root.glob(f"critique/round_{prior}/snapshots/*/source/REPORT_TO_REVIEW.md")
                      if r.is_file()), nonlocal_pin
    reviewed_paths, reviewed_pinned = reviewed_snapshot_reports()
    def frozen_manifests() -> list:
        """The packet round {args.round} was built from: its CURRENT invocation's snapshot (older audits, or no
        such record: every round-{args.round} snapshot). Round N-1's step-1 copies are bound only by round N."""
        round_dir = root / f"critique/round_{args.round}"
        pointer = round_dir / "CURRENT_INVOCATION"
        if pointer.is_file():
            try:
                relative = json.loads((round_dir / "invocations" / pointer.read_text(encoding="utf-8").strip()
                                       / "snapshot.json").read_text(encoding="utf-8")).get("snapshot_rel")
            except (OSError, ValueError, AttributeError):
                relative = None
            manifest = root / relative / "source/context/DIFF_MANIFEST.json" if isinstance(relative, str) else None
            if manifest is not None and manifest.is_file():
                return [manifest]
        return sorted(round_dir.glob("snapshots/*/source/context/DIFF_MANIFEST.json"))
    def _draft_ids(data: bytes) -> list:
        return [m for m in re.findall(r"<!--\s*finding:(\S+?)\s*-->", data.decode("utf-8", "replace"))
                if not re.fullmatch(r"F[1-9][0-9]*", m)]
    draft_markers = _draft_ids(raw[before_report])
    # an older report version (e.g. report/REPORT_v1.md) still carries draft ids; an audit whose round {prior} was
    # itself reviewed under draft ids (before the renumber guard existed) is legitimate and passes
    step1_for_draft = root / f"critique/round_{prior}/REPORT_BEFORE_INTEGRATION.md"
    def _bound_before_report(manifest_path):
        try:
            relative = (load_frozen_manifest(root, manifest_path).get("input_paths") or {}).get("before_report")
        except (OSError, ValueError) as exc:
            raise SystemExit(f"frozen diff packet {manifest_path.relative_to(root)} is unreadable: {exc}") from exc
        return root / relative if isinstance(relative, str) and relative else None
    bound_by_frozen = step1_for_draft.is_file() and before_report.samefile(step1_for_draft) and any(
        (bound := _bound_before_report(m)) is not None and bound.is_file() and bound.samefile(step1_for_draft)
        for m in frozen_manifests())
    # a frozen-bound step-1 copy gets the exact-bytes restore below instead of this message
    if draft_markers and not bound_by_frozen and reviewed_paths and not any(_draft_ids(r.read_bytes()) for r in reviewed_paths):
        raise SystemExit(
            f"--before-report {before_report.relative_to(root)} still carries draft finding ids ({draft_markers[:3]}), "
            f"but round {prior}'s critics reviewed final F-ids: pass round {prior}'s step-1 copy "
            f"critique/round_{prior}/REPORT_BEFORE_INTEGRATION.md"
            + (" -- this is that file, so restore it from the reviewed snapshot "
               f"{reviewed_paths[0].relative_to(root)}" if (root / f"critique/round_{prior}/REPORT_BEFORE_INTEGRATION.md").is_file()
               and before_report.samefile(root / f"critique/round_{prior}/REPORT_BEFORE_INTEGRATION.md") else ""))
    reviewed_reports = {id_masked(r.read_bytes()) for r in reviewed_paths}
    # A step-1 copy that a later round's frozen packet binds must keep those exact bytes: a renumber changes only
    # markers, so the masked comparison below cannot see it (finalize's binding check does).
    step1_report_path = root / f"critique/round_{prior}/REPORT_BEFORE_INTEGRATION.md"
    frozen_report_hash = None
    if step1_report_path.is_file() and before_report.is_file() and before_report.samefile(step1_report_path):
        for manifest in frozen_manifests():
            packet_doc = load_frozen_manifest(root, manifest)
            bound_path = root / ((packet_doc.get("input_paths") or {}).get("before_report") or "")
            if bound_path.is_file() and bound_path.samefile(step1_report_path):
                frozen_report_hash = (packet_doc.get("inputs") or {}).get("before_report_sha256")
    frozen_mismatch = bool(frozen_report_hash) and hashlib.sha256(raw[before_report]).hexdigest() != frozen_report_hash
    if (reviewed_reports and id_masked(raw[before_report]) not in reviewed_reports) or frozen_mismatch:
        snapshots = reviewed_paths
        # restore only round {prior}'s own step-1 copy, never another file (least of all the canonical report)
        step1_report = root / f"critique/round_{prior}/REPORT_BEFORE_INTEGRATION.md"
        own_report_copy = step1_report.is_file() and before_report.samefile(step1_report)
        if not own_report_copy:
            # never a restore target: a snapshot, a round's published files, or anything else
            raise SystemExit(
                f"--before-report {before_report.relative_to(root)} is not round {prior}'s step-1 copy: pass "
                f"critique/round_{prior}/REPORT_BEFORE_INTEGRATION.md (the report as round {prior}'s critics reviewed it)")
        target = step1_report
        others = [s for s in sorted(root.glob(f"critique/round_{prior}/snapshots/*/source/REPORT_TO_REVIEW.md"))
                  if s not in snapshots and id_masked(s.read_bytes()) == id_masked(raw[before_report])]
        why = ("it changed after a later round's packet was built from it (its finding ids differ)"
               if frozen_mismatch and id_masked(raw[before_report]) in reviewed_reports else
               f"it matches {others[0].relative_to(root)}, written by a rerun that never became the round's current "
               "invocation, so no critic reviewed it" if others else
               f"it matches none of {[str(s.relative_to(root)) for s in snapshots]}")
        # A later round's frozen packet may bind this exact file (after the N=1 renumber it holds renumbered bytes):
        # restore those bytes, not the snapshot's pre-renumber ones.
        bound_hash = None
        for manifest in frozen_manifests():
            packet_doc = load_frozen_manifest(root, manifest)
            bound_path = root / ((packet_doc.get("input_paths") or {}).get("before_report") or "")
            if bound_path.is_file() and target.is_file() and bound_path.samefile(target):
                bound_hash = (packet_doc.get("inputs") or {}).get("before_report_sha256")
        remedy = ""
        if bound_hash:
            sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
            import renumber_findings as _renumber
            relative = target.relative_to(root).as_posix()
            candidates = _renumber.bound_report_candidates(root, relative, snapshots)
            match = next((c for kind, c in candidates
                          if kind == "text" and hashlib.sha256(c.encode("utf-8")).hexdigest() == bound_hash), None)
            source = next((c for kind, c in candidates
                           if kind == "file" and hashlib.sha256(c.read_bytes()).hexdigest() == bound_hash), None)
            if match is not None and source is None:
                if args.no_remedy_file:   # a check-only caller writes nothing
                    raise SystemExit(
                        f"--before-report {before_report.relative_to(root)} is not the report round {prior}'s critics "
                        f"reviewed, and a later round's frozen packet binds it; run make_diff_packet.py --round "
                        f"{args.round} directly to write a restore copy of the bound bytes")
                source = target.with_name(target.name + ".restored")
                source.write_text(match, encoding="utf-8")
            remedy = (f"; a later round's frozen packet binds {relative}, so restore the exact bytes it was built "
                      f"from: chmod u+w {shlex.quote(str(target))} 2>/dev/null; cp {shlex.quote(str(source))} {shlex.quote(str(target))}" if source else
                      f"; a later round's frozen packet binds {relative} (sha256 {bound_hash}) and no copy of those "
                      "bytes remains in the audit: restore it from a backup of the audit folder")
        elif len(snapshots) == 1:
            remedy = (f"; restore it: chmod u+w {shlex.quote(str(target))} 2>/dev/null; cp {shlex.quote(str(snapshots[0]))} {shlex.quote(str(target))} "
                      "(its ids are translated when the packet is built)")
        else:
            remedy = f"; the reviewed report is one of {[str(s.relative_to(root)) for s in snapshots]}"
        raise SystemExit(
            f"--before-report {before_report.relative_to(root)} is not the report round {prior}'s critics reviewed "
            f"({why})" + ("" if frozen_mismatch else " -- a step-1 copy taken after the report was edited?") + remedy)
    backup = root / f"work/findings.before_round{prior}_integration.jsonl"
    marker = backup.with_suffix(".invocation")
    pointer = root / f"critique/round_{prior}/CURRENT_INVOCATION"
    invocation = pointer.read_text(encoding="utf-8").strip() if pointer.is_file() else None
    backup_invocation = marker.read_text(encoding="utf-8").strip() if marker.is_file() else None
    if backup.is_file() and before_findings.is_file() and before_findings.samefile(backup):   # APFS is case-insensitive
        raise SystemExit(
            f"--before-findings must be the step-1 copy critique/round_{prior}/FINDINGS_BEFORE_INTEGRATION.jsonl, not "
            f"integrate_round's backup {backup.relative_to(root)}: the backup keeps the pre-renumber ids, and it "
            "must never be overwritten")
    backup_label = f"integrate_round's backup {backup.relative_to(root)}"
    backup_usable = backup.is_file() and not (backup_invocation and invocation and backup_invocation != invocation)
    if not backup_usable and not reviewed_pinned and len(reviewed_paths) > 1:
        # unpinned (no CURRENT_INVOCATION record) with several snapshots: the reference is the snapshots whose report
        # is the verified before-report, and only when they all hold the same registry; otherwise nothing can say
        # which registry the critics reviewed, so the packet is refused rather than built unchecked
        # the reviewed-report rule: marker ids masked (a documented renumber rewrites them in the step-1 copy)
        paired = [r for r in reviewed_paths if id_masked(r.read_bytes()) == id_masked(raw[before_report])
                  and (r.parent / "context/work/findings.jsonl").is_file()]
        # a snapshot whose registry already IS the post-integration registry was written after integration (a refused
        # rerun): no critic reviewed it, so it is no reference -- unless every candidate is (nothing changed)
        if (root / f"critique/round_{prior}/INTEGRATION_SUMMARY.json").is_file():
            unchanged = [r for r in paired if (r.parent / "context/work/findings.jsonl").read_bytes() != raw[after_findings]]
            paired = unchanged or paired
        registries = {(r.parent / "context/work/findings.jsonl").read_bytes() for r in paired}
        if len(registries) == 1:
            reviewed_paths = paired[:1]
        elif not paired:
            raise SystemExit(
                f"--before-findings cannot be checked: round {prior} has no integrate_round backup for its current "
                "run, no CURRENT_INVOCATION record, and no snapshot with both this before-report and a registry copy. "
                f"Write the reviewed invocation id to critique/round_{prior}/CURRENT_INVOCATION (see its invocations/ "
                "folder), then re-run")
        else:
            raise SystemExit(
                f"--before-findings cannot be checked: round {prior} has no integrate_round backup for its current "
                f"run and no CURRENT_INVOCATION record, and its snapshots "
                f"{[str(r.parent.parent.relative_to(root)) for r in reviewed_paths]} do not agree on the registry "
                "reviewed with this before-report. Write the reviewed invocation id to "
                f"critique/round_{prior}/CURRENT_INVOCATION (see its invocations/ folder), then re-run")
    if not backup_usable and (reviewed_pinned or len(reviewed_paths) == 1):
        # no backup of what round {prior}'s current invocation reviewed: never integrated (it ended the audit; now
        # reopened, e.g. to correct a published comment), integrated only for an earlier invocation (a rerun not yet
        # re-integrated), or the backup was removed. The step-1 registry copy is then checked against that
        # invocation's snapshot registry, translated through later renumbers the same way. An unpinned round with
        # several snapshots is not guessed at (one may be a refused rerun's, written after integration).
        reviewed_registry = next((r.parent / "context/work/findings.jsonl" for r in reviewed_paths
                                  if r.read_bytes() == raw[before_report]
                                  and (r.parent / "context/work/findings.jsonl").is_file()), None)
        if reviewed_registry is None and len(reviewed_paths) == 1 \
                and (reviewed_paths[0].parent / "context/work/findings.jsonl").is_file():
            reviewed_registry = reviewed_paths[0].parent / "context/work/findings.jsonl"   # report ids renumbered since
        if reviewed_registry is not None:
            backup, backup_invocation = reviewed_registry, invocation
            backup_label = f"the registry round {prior} reviewed, {reviewed_registry.relative_to(root)}"
    translated_rows = None      # set when the before-files keep the ids round {prior} was reviewed under
    maps_after = []
    report_maps = []            # the renumber maps the before-report still needs
    if backup.is_file() and not (backup_invocation and invocation and backup_invocation != invocation):
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import renumber_findings
        expected = [json.loads(line) for line in backup.read_text(encoding="utf-8").splitlines() if line.strip()]
        untranslated = json.loads(json.dumps(expected))   # deep copy: rewrite_rows mutates nested fields
        prefixes = []
        history_path = root / "work/id_map_history.jsonl"
        def _moved(path: pathlib.Path) -> bool:
            # only a map that moved an F<n> id: the pre-round-1 draft->F renumber needs no translation
            try:
                return any(re.fullmatch(r"F[1-9][0-9]*", str(k)) and k != v
                           for k, v in json.loads(path.read_text(encoding="utf-8")).items())
            except (OSError, ValueError, AttributeError):
                return path.is_file()
        # renumbered by a version that kept no history: 2.6.3 wrote only work/id_map.json, later QA builds
        # also work/id_map_last.json
        import later_disputes
        legacy = later_disputes.legacy_renumbered(root)   # one rule for every tool (F<n> moves only)
        for line in (history_path.read_text(encoding="utf-8").splitlines() if history_path.is_file() else []):
            if not line.strip():
                continue
            entry = json.loads(line)
            ran = entry.get("rounds_run_before_renumber") or {}
            key = f"round_{prior}"
            if key in ran and not (isinstance(ran, dict) and backup_invocation and ran[key] != backup_invocation):
                prefixes.append(json.loads(json.dumps(expected)))   # the backup through the maps before this one
                expected = renumber_findings.rewrite_rows(expected, entry.get("map") or {})
                maps_after.append(entry.get("map") or {})

        def comparable(rows: list) -> list:
            return sorted(json.dumps({k: v for k, v in row.items() if k != "settled"}, sort_keys=True) for row in rows)
        given = [json.loads(line) for line in raw[before_findings].decode("utf-8").splitlines() if line.strip()]
        if legacy:
            # a before-file still in the pre-renumber ids (2.6.3's recipe did not rewrite round N-1's): its ids
            # match the untranslatable backup's, while today's registry holds the same claims under other ids
            live_rows = [json.loads(line) for line in raw[after_findings].decode("utf-8").splitlines() if line.strip()]
            given_ids = {r.get("claim"): r.get("id") for r in given}
            backup_ids = {r.get("claim"): r.get("id") for r in untranslated}   # the ids round {prior} reviewed
            live_ids = {r.get("claim"): r.get("id") for r in live_rows}
            shared = [c for c in given_ids if c in live_ids]
            if given_ids == backup_ids and any(given_ids[c] != live_ids[c] for c in shared):
                # The file may be bound by a later round's frozen snapshot, so it is not rewritten: its ids are
                # translated here by claim text (a renumber with no history cannot be replayed exactly).
                mapping = {given_ids[c]: live_ids[c] for c in shared}
                candidate = renumber_findings.rewrite_rows(json.loads(json.dumps(given)), mapping)
                # only when claim text identifies EVERY finding (a row reworded or merged at that integration has no
                # shared text) and the result has unique ids; otherwise the rows stay as they are, which shows more
                # change than there was (extra review), never less
                if len(given_ids) == len(given) and set(mapping) == {r.get("id") for r in given} \
                        and len({r.get("id") for r in candidate}) == len(candidate):
                    translated_rows = candidate
                    if any(raw[before_report] == r.read_bytes() for r in reviewed_paths):
                        report_maps = [mapping]   # the report still carries the reviewed ids
        if legacy:
            # renumbered by a version that kept no history: the backup's ids cannot be translated reliably,
            # so rows are matched by claim text and compared without their ids (a late copy that withdrew or
            # re-rated a finding still differs)
            def comparable(rows: list) -> list:
                def strip(row: dict) -> dict:
                    row = {k: v for k, v in row.items() if k not in ("id", "settled")}
                    deps = dict(row.get("dependencies") or {})
                    deps.pop("finding_ids", None)
                    row["dependencies"] = deps
                    disposition = dict(row.get("ledger_disposition") or {})
                    disposition.pop("target", None)
                    disposition.pop("reason", None)
                    row["ledger_disposition"] = disposition
                    return row
                return sorted(json.dumps(strip(row), sort_keys=True) for row in rows)
        # Round {prior}'s before-files may stop at any renumber: the N=1 recipe rewrites them once, and later
        # renumbers must leave them alone once a later round's frozen snapshot binds their hashes. Translate the
        # rest of the way here.
        matched = next((k for k, rows in enumerate(prefixes) if comparable(given) == comparable(rows)), None) \
            if not legacy and comparable(given) != comparable(expected) else None
        # the registry round {prior}'s critics reviewed may also be its snapshot's own copy (the documented step-1
        # copy for a round reopened after the audit ended, whose backup was taken later, after a deferred approval
        # was bound; or several snapshots with no CURRENT_INVOCATION): accept a copy equal to any snapshot registry
        # paired with the verified before-report, translated through the same renumber maps
        snapshot_stage = None

        def frozen_binding_broken() -> bool:
            # a later round's frozen packet binds this before-file: it may not be replaced, not even by an otherwise
            # acceptable snapshot copy (the mismatch branch below then refuses with the restore command)
            for manifest in frozen_manifests():
                packet_doc = load_frozen_manifest(root, manifest)
                bound_path = root / ((packet_doc.get("input_paths") or {}).get("before_findings") or "")
                if bound_path.is_file() and bound_path.samefile(before_findings):
                    bound_hash = (packet_doc.get("inputs") or {}).get("before_findings_sha256")
                    if bound_hash and bound_hash != hashlib.sha256(raw[before_findings]).hexdigest():
                        return True
            return False
        # only the snapshot the current invocation recorded: with no such record every snapshot of the round is a
        # candidate, including one a refused rerun wrote after integration, which no critic reviewed
        if frozen_binding_broken():
            # whatever else would accept it (the backup, a snapshot), a before-file a later round's frozen packet binds
            # must keep those bytes
            relative = before_findings.relative_to(root).as_posix()
            bound_source = next((c for c in sorted(root.glob(f"critique/round_{prior}/snapshots/*/source/context/work/findings.jsonl"))
                                 + sorted(root.glob(f"critique/round_*/post_delta/*/payload/{relative}"))
                                 + sorted(root.glob(f"work/findings.before_round{prior}_integration.jsonl"))
                                 if any(hashlib.sha256(c.read_bytes()).hexdigest() == (load_frozen_manifest(root, m).get("inputs") or {}).get("before_findings_sha256")
                                        for m in frozen_manifests())), None)
            raise SystemExit(
                f"--before-findings {relative} differs from the bytes a later round's frozen diff packet binds, so it "
                "must not be replaced. " + (f"Restore them, then rebuild: chmod u+w {shlex.quote(str(before_findings))} && "
                                            f"cp {shlex.quote(str(bound_source))} {shlex.quote(str(before_findings))}"
                                            if bound_source else "Restore it from a backup of the audit folder"))
        # the pinned snapshot, or the round's only snapshot (a refused rerun always adds a second one)
        if matched is None and not legacy and (reviewed_pinned or len(reviewed_paths) == 1) \
                and comparable(given) != comparable(expected):
            paired = [r for r in reviewed_paths if r.read_bytes() == raw[before_report]] or list(reviewed_paths)
            for reviewed_report in paired:
                registry_copy = reviewed_report.parent / "context/work/findings.jsonl"
                if not registry_copy.is_file():
                    continue
                stages = [[json.loads(line) for line in registry_copy.read_text(encoding="utf-8").splitlines()
                           if line.strip()]]
                for stage_map in maps_after:
                    stages.append(renumber_findings.rewrite_rows(json.loads(json.dumps(stages[-1])), stage_map))
                hit = next((k for k, rows in enumerate(stages) if comparable(given) == comparable(rows)), None)
                if hit is not None:
                    snapshot_stage = (hit, stages)
                    break
        def frozen_binds_these_bytes() -> bool:
            # 2.6.9: round {args.round}'s own frozen packet was built from exactly these bytes, and its critics were
            # routed by it. Those bytes are authoritative for rebuilding that packet (e.g. to regenerate the last
            # round's receipt when a published audit is re-filed), even when they no longer compare equal to the
            # backup: a renumber by a version that kept no history (Schütz: F19 back to F4 after round 4, only the
            # draft map in id_map.json) leaves nothing to translate the backup with, and the frozen binding forbids
            # any replacement, so a refusal here had no working remedy (its printed restore was a no-op).
            current = hashlib.sha256(raw[before_findings]).hexdigest()
            for manifest in frozen_manifests():
                packet_doc = load_frozen_manifest(root, manifest)
                bound_path = root / ((packet_doc.get("input_paths") or {}).get("before_findings") or "")
                if bound_path.is_file() and bound_path.samefile(before_findings) \
                        and (packet_doc.get("inputs") or {}).get("before_findings_sha256") == current:
                    return True
            return False

        if matched is not None:
            translated_rows = expected
            report_maps = maps_after[matched:]
        elif snapshot_stage is not None:
            hit, stages = snapshot_stage
            if hit < len(stages) - 1:
                translated_rows = stages[-1]
                report_maps = maps_after[hit:]

        elif comparable(given) != comparable(expected) and frozen_binds_these_bytes():
            print(f"note: --before-findings {before_findings.relative_to(root)} differs from {backup_label}, but it holds "
                  f"exactly the bytes round {args.round}'s frozen diff packet was built from (sha256 "
                  f"{hashlib.sha256(raw[before_findings]).hexdigest()[:12]}), so the packet is rebuilt from them "
                  "as that round's critics were routed", file=sys.stderr)
        elif comparable(given) != comparable(expected):
            if legacy or args.no_remedy_file:
                raise SystemExit(
                    f"--before-findings {before_findings.relative_to(root)} is not the registry round {prior}'s critics "
                    f"reviewed (it differs from {backup_label}); rebuild the "
                    "step-1 copy from what round {prior} reviewed before building this packet".replace("{prior}", str(prior)))
            # the given file IS the backup's content under its old ids: a renumber after round {prior} did not
            # reach round {prior}'s before-files (not a late copy)
            renumbered_after = not legacy and comparable(given) == comparable(untranslated)
            if renumbered_after:
                # most likely cause: the withdrawal renumber ran after round {prior} without its before-files
                raise SystemExit(
                    f"--before-findings {before_findings.relative_to(root)} still carries the ids from before a renumber "
                    f"that ran after round {prior}. Finish that renumber for round {prior}'s before-files:\n"
                    f"  python3 {shlex.quote(str(pathlib.Path(__file__).resolve().parent / 'renumber_findings.py'))} {shlex.quote(str(root))} --apply --late-also "
                    f"--also critique/round_{prior}/REPORT_BEFORE_INTEGRATION.md "
                    f"--also critique/round_{prior}/FINDINGS_BEFORE_INTEGRATION.jsonl")
            step1_findings = root / f"critique/round_{prior}/FINDINGS_BEFORE_INTEGRATION.jsonl"
            own_copy = step1_findings.is_file() and before_findings.samefile(step1_findings)
            frozen_hash = None
            for manifest in frozen_manifests():
                packet_doc = load_frozen_manifest(root, manifest)
                bound_path = root / ((packet_doc.get("input_paths") or {}).get("before_findings") or "")
                if bound_path.is_file() and bound_path.samefile(before_findings):   # APFS is case-insensitive
                    frozen_hash = (packet_doc.get("inputs") or {}).get("before_findings_sha256")
            if frozen_hash:
                relative = before_findings.relative_to(root).as_posix()
                source = next((c for c in sorted(root.glob(f"critique/round_{prior}/snapshots/*/source/context/work/findings.jsonl"))
                               + sorted(root.glob(f"critique/round_*/post_delta/*/payload/{relative}"))
                               + sorted(root.glob(f"work/findings.before_round{prior}_integration.jsonl"))
                               if hashlib.sha256(c.read_bytes()).hexdigest() == frozen_hash), None)
                raise SystemExit(
                    f"--before-findings {relative} is not the registry round {prior}'s critics reviewed, and a later "
                    "round's frozen diff packet binds it, so it must not be replaced. "
                    + (f"Restore the bytes that packet was built from, then rebuild: chmod u+w {shlex.quote(str(before_findings))} && "
                       f"cp {shlex.quote(str(source))} {shlex.quote(str(before_findings))}" if source else
                       f"No copy of the bytes that packet was built from (sha256 {frozen_hash}) remains in the audit; "
                       "restore it from a backup of the audit folder"))
            if not own_copy:
                raise SystemExit(
                    f"--before-findings {before_findings.relative_to(root)} is not the registry round {prior}'s critics "
                    f"reviewed; use round {prior}'s step-1 copy critique/round_{prior}/FINDINGS_BEFORE_INTEGRATION.jsonl")
            before_findings = step1_findings   # a hard-link alias: the remedy always names the step-1 file itself
            translated = before_findings.with_name(before_findings.stem + ".from_backup.jsonl")
            translated.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in expected), encoding="utf-8")
            raise SystemExit(
                f"--before-findings {before_findings.relative_to(root)} is not the registry round {prior}'s critics "
                f"reviewed: it differs from {backup_label} (translated through "
                f"the renumber history) -- was the step-1 copy taken after apply or an edit? The expected file was written to "
                f"{translated.relative_to(root)}; move it into place and rebuild the packet:\n"
                f"  mv {shlex.quote(str(translated))} {shlex.quote(str(before_findings))}")
    if maps_after and not legacy:
        # The report may stop at a different renumber than the findings (only one of them passed to --also, or
        # restored from the snapshot). The reviewed snapshot carries no maps: the stage whose replay reproduces the
        # given before-report exactly is the one it carries, whatever the findings file did.
        given_report = raw[before_report].decode("utf-8")
        for reviewed in reviewed_paths:
            stage_text = reviewed.read_text(encoding="utf-8")
            stages = [stage_text]
            for mapping in maps_after:
                stage_text = renumber_findings.rewrite_text(stage_text, mapping)
                stages.append(stage_text)
            if given_report in stages:
                report_maps = maps_after[stages.index(given_report):]
                break
    digest = {path: hashlib.sha256(raw[path]).hexdigest() for path in raw}
    before_report_text = raw[before_report].decode("utf-8")
    if translated_rows is not None or report_maps:
        for mapping in report_maps:
            before_report_text = renumber_findings.rewrite_text(before_report_text, mapping)
        no_changes = (before_report_text == raw[after_report].decode("utf-8") and
                      [json.dumps(r, sort_keys=True) for r in (translated_rows if translated_rows is not None else
                                                               [json.loads(l) for l in raw[before_findings].decode("utf-8").splitlines() if l.strip()])]
                      == [json.dumps(json.loads(line), sort_keys=True)
                          for line in raw[after_findings].decode("utf-8").splitlines() if line.strip()])
    else:
        no_changes = (digest[before_report] == digest[after_report] and
                      digest[before_findings] == digest[after_findings])

    here = pathlib.Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    sys.path.insert(0, str(here.parent / "schemas/tools"))
    import validate_json_schema
    import validate_v2
    finding_schema = here.parent / "schemas/v2/finding-row.schema.json"
    before_rows, after_rows = load_rows(before_findings, raw[before_findings]), load_rows(after_findings, raw[after_findings])
    if translated_rows is not None:
        before_rows = translated_rows
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

    before_text = before_report_text
    after_text = raw[after_report].decode("utf-8")
    changed_section_names = section_changes(before_text, after_text)
    before_blocks, after_blocks = report_blocks(before_text), report_blocks(after_text)
    changed_section_bodies = [{"section": name, "before": before_blocks.get(name),
                               "after": after_blocks.get(name)} for name in changed_section_names]
    # 2.7.4: the audit's frozen claim-change rule (scripts/claim_change_rules.py). No leaf (every audit initialised
    # earlier) is the legacy rule: nothing below changes and the packet keeps its bytes. Under the specialist rule
    # the packet also lists the substantive changes, which the runtime gives the specialists; completeness keeps
    # every changed finding.
    import claim_change_rules
    try:
        claim_rule = claim_change_rules.frozen_rule(root)
    except claim_change_rules.RuleError as exc:
        raise SystemExit(f"make_diff_packet: {exc}") from exc
    specialist_ids: list[str] = []
    if claim_rule != claim_change_rules.LEGACY_RULE:
        specialist_ids = claim_change_rules.specialist_claim_ids(
            changed, before_by_id, after_by_id, changed_paths, changed_section_names)
    # Later rounds always receive completeness in the runtime. Ordinarily this packet chooses
    # exactly one best specialist. Extra specialists are accuracy-safe only for critical changes
    # spanning multiple domains; ordinary cross-domain edits do not silently restore a full round.
    # 2.7.4: under the specialist rule the specialists review only the substantive changes, so they are also chosen
    # from those rows (all changed rows when there is none, as the runtime then keeps the unnarrowed scope). The
    # legacy rule routes on every changed row, byte for byte as before.
    routing_rows = changed_rows
    if specialist_ids:
        routing_rows = [after_by_id.get(finding_id) or before_by_id[finding_id] for finding_id in specialist_ids]
    role_scores = specialist_scores(routing_rows)
    selected = {best_specialist(routing_rows)}
    critical_rows = [row for row in routing_rows
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
            raise SystemExit(
                f"SETTLED.json failed closed validation: {exc}. After a report or registry edit, rebuild the settled "
                "cache first: approve_settled.py -> generate_finding_artifacts.py --apply -> approve_settled.py -> "
                "generate_finding_artifacts.py --apply -> make_settled.sh -> re-log the settled event, then build "
                "this packet and re-run this round's post-delta gates before running the round: python3 "
                f"{shlex.quote(str(pathlib.Path(__file__).resolve().parent / 'run_post_delta_gates.py'))} "
                f"{shlex.quote(str(root))} --round {args.round} --report "
                f"{shlex.quote(str(root / 'report' / 'REPORT_final.md'))}") from exc
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
                if fact.get("reworded_at_round") is not None:
                    # the critics must see that the published wording rests on fewer families than the list
                    settled_refs[-1]["reworded_at_round"] = fact["reworded_at_round"]
                    settled_refs[-1]["new_wording_confirmed_by_families"] = fact.get(
                        "new_wording_confirmed_by_families") or []

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
    if claim_rule != claim_change_rules.LEGACY_RULE:
        # only a new audit's packet carries these keys, so a legacy packet's bytes are unchanged
        packet["claim_change_rule"] = claim_rule
        packet["specialist_claim_ids"] = specialist_ids
        packet["bookkeeping_only_claim_ids"] = [finding_id for finding_id in changed if finding_id not in specialist_ids]
    atomic_json(output.resolve(), packet)
    print(f"wrote {output.resolve()} ({len(changed)} changed claim IDs; "
          f"state={packet['change_state']}; roles={','.join(recommended)},completeness)"
          + (f"; specialists see {len(specialist_ids)} of them, completeness all"
             if claim_rule != claim_change_rules.LEGACY_RULE else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
