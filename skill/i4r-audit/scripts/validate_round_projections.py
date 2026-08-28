#!/usr/bin/env python3
"""Verify every critique round against its immutable runtime projection."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SKILL / "schemas/tools"))
import validate_json_schema
import validate_v2
import verify_diff_packet
import test_run_critique_runtime as critique_runtime
import run_post_delta_gates


SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def digest(path: pathlib.Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def load(path: pathlib.Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"required immutable round file is missing or symlinked: {path}")
    value = validate_json_schema.strict_loads(path.read_text(encoding="utf-8"), source=str(path))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def verify_inventory(root: pathlib.Path, expected: Any, manifest_name: str = "MANIFEST.json") -> None:
    if not isinstance(expected, dict):
        raise ValueError(f"{root}: files_sha256 must be an object")
    actual: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink inside immutable round tree: {path}")
        if path.is_file() and path.relative_to(root).as_posix() != manifest_name:
            actual[path.relative_to(root).as_posix()] = digest(path)
    if actual != expected:
        raise ValueError(f"immutable file inventory differs under {root}")


def verify_result_evidence(result: dict[str, Any], snapshot: pathlib.Path) -> None:
    inventory_paths = (
        snapshot / "source/context/EVIDENCE_HASHES.json",
        snapshot / "source/EVIDENCE_HASHES.json",
    )
    found = [path for path in inventory_paths if path.is_file() and not path.is_symlink()]
    if len(found) != 1:
        raise ValueError(f"{snapshot}: expected exactly one frozen evidence inventory")
    inventory_path = found[0]
    if digest(inventory_path) != result["input_hashes"]["evidence_manifest_sha256"]:
        raise ValueError("critic evidence inventory hash is not bound to its result")
    inventory = load(inventory_path)
    snapshot_manifest = load(snapshot / "MANIFEST.json")
    manifest_hashes = snapshot_manifest.get("files_sha256")
    for collection in ("items", "new_issues"):
        for index, assessment in enumerate(result[collection]):
            evidence = assessment["evidence"]
            bindings = list(evidence["artifact_refs"])
            if evidence["reproducible_script"] is not None:
                bindings.append(evidence["reproducible_script"])
            for binding_index, binding in enumerate(bindings):
                if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
                    raise ValueError(
                        f"{collection}[{index}] evidence binding {binding_index} is malformed"
                    )
                rel = binding["path"]
                if (not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) or
                        re.match(r"^[A-Za-z]:[\\/]", rel) or
                        ".." in pathlib.PurePosixPath(rel.replace("\\", "/")).parts):
                    raise ValueError(f"unsafe critic evidence path: {rel!r}")
                row = inventory.get(rel)
                if not isinstance(row, dict) or set(row) != {"sha256", "size"}:
                    raise ValueError(f"critic evidence is not in frozen inventory: {rel}")
                artifact = snapshot / rel
                cursor = snapshot
                for part in pathlib.PurePosixPath(rel.replace("\\", "/")).parts:
                    cursor = cursor / part
                    if cursor.is_symlink():
                        raise ValueError(f"symlinked critic evidence path: {rel}")
                if not artifact.is_file():
                    raise ValueError(f"missing critic evidence artifact: {rel}")
                actual = digest(artifact)
                manifest_rel = artifact.relative_to(snapshot).as_posix()
                if (binding["sha256"] != actual or row.get("sha256") != actual or
                        row.get("size") != artifact.stat().st_size or
                        not isinstance(manifest_hashes, dict) or
                        manifest_hashes.get(manifest_rel) != actual):
                    raise ValueError(f"critic evidence hash binding differs: {rel}")


def verify(audit: pathlib.Path, *, require_post_delta_gates: bool = False) -> int:
    audit = audit.resolve()
    run = load(audit / "run.json")
    if run.get("schema_version") != "2.0.0":
        print("legacy run: immutable v2 round projection gate not applicable")
        return 0
    audit_id = run.get("audit_id")
    rounds = run.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        raise ValueError("v2 finalization requires recorded critique rounds")
    policy = load(audit / "pipeline-policy.snapshot.json").get("policy")
    if not isinstance(policy, dict):
        raise ValueError("policy snapshot lacks policy")
    try:
        critic_model = policy["models"]["critics"]
        targeted_model = policy["models"]["targeted_adjudicator"]
    except (KeyError, TypeError) as exc:
        raise ValueError("policy snapshot lacks split critic/adjudicator model routing") from exc
    ledger_rows = []
    ledger_path = audit / "invocations.jsonl"
    for number, line in enumerate(ledger_path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            row = validate_json_schema.strict_loads(line, source=f"{ledger_path}:{number}")
            if not isinstance(row, dict):
                raise ValueError(f"invocation ledger line {number} is not an object")
            validate_v2.validate_invocation(row)
            ledger_rows.append(row)

    expected_numbers = list(range(1, len(rounds) + 1))
    if [row.get("round") for row in rounds] != expected_numbers:
        raise ValueError("run critique rounds are not unique and contiguous")
    rereviewed_change_ids: list[str] = []
    for row in rounds:
        number = row["round"]
        round_dir = audit / f"critique/round_{number}"
        pointer_path = round_dir / "CURRENT_PROJECTION"
        if pointer_path.is_symlink() or not pointer_path.is_file():
            raise ValueError(f"round {number}: immutable current-projection pointer is missing")
        invocation_id = pointer_path.read_text(encoding="utf-8").strip()
        if not SAFE_ID.fullmatch(invocation_id):
            raise ValueError(f"round {number}: unsafe current projection ID")
        projection = round_dir / "projections" / invocation_id
        try:
            projection.resolve().relative_to(round_dir.resolve())
        except ValueError as exc:
            raise ValueError(f"round {number}: projection escapes round directory") from exc
        manifest = load(projection / "MANIFEST.json")
        if (manifest.get("projection_version") != "i4r-round-projection-1" or
                manifest.get("invocation_id") != invocation_id or
                manifest.get("audit_id") != audit_id or manifest.get("round") != number):
            raise ValueError(f"round {number}: projection identity mismatch")
        roles = manifest.get("roles")
        if not isinstance(roles, list) or not roles or len(roles) != len(set(roles)):
            raise ValueError(f"round {number}: projection role quorum is malformed")
        verify_inventory(projection, manifest.get("files_sha256"))
        routing = load(projection / "ROUTING.json")
        if (routing.get("routing_version") != "i4r-round-routing-1" or
                routing.get("round") != number or routing.get("roles") != roles):
            raise ValueError(f"round {number}: routing does not match immutable projection")
        assignment_mode = routing.get("claim_assignment_mode")
        assignments = routing.get("claim_assignments")
        if not isinstance(assignments, dict) or set(assignments) != set(roles):
            raise ValueError(f"round {number}: claim assignments do not match routed roles")
        if number == 1:
            if assignment_mode != "unscoped_round_1" or any(assignments[role] for role in roles):
                raise ValueError("round 1 must use explicit empty/unscoped claim assignments")
        elif assignment_mode != "exact_changed_claims":
            raise ValueError(f"round {number}: later-round claim assignment is not exact")
        declared = row.get("critics_run")
        projected = [f"critic_{role}" for role in roles]
        if declared != projected:
            raise ValueError(
                f"round {number}: run critics_run {declared!r} differs from immutable route {projected!r}"
            )
        if number == 1 and projected != policy["critique"]["round_1"]["roles"]:
            raise ValueError("round 1 immutable route differs from canonical four-role policy")
        if number > 1:
            if routing.get("diff_manifest_present") is not True:
                raise ValueError(f"round {number}: later round lacks its generated diff manifest")
            diff_path = round_dir / "snapshots" / manifest["input_sha256"] / "source/context/DIFF_MANIFEST.json"
            if digest(diff_path) != routing.get("diff_manifest_sha256"):
                raise ValueError(f"round {number}: routed diff-manifest hash is stale")
            verify_diff_packet.verify(audit, diff_path, number)
            diff = load(diff_path)
            expected_claims = diff.get("changed_claim_ids")
            if (not isinstance(expected_claims, list) or
                    any(assignments[role] != expected_claims for role in roles)):
                raise ValueError(f"round {number}: routed claim assignments differ from diff packet")

        snapshot = round_dir / "snapshots" / manifest["input_sha256"]
        snapshot_manifest = load(snapshot / "MANIFEST.json")
        if (snapshot_manifest.get("snapshot_version") != "i4r-runtime-snapshot-3" or
                snapshot_manifest.get("input_sha256") != manifest.get("input_sha256") or
                snapshot_manifest.get("report_sha256") != manifest.get("report_sha256") or
                snapshot_manifest.get("round") != number or snapshot_manifest.get("roles") != roles or
                snapshot_manifest.get("audit_id") != audit_id):
            raise ValueError(f"round {number}: content-addressed input snapshot identity mismatch")
        if (snapshot_manifest.get("model") != critic_model.get("requested_model") or
                snapshot_manifest.get("effort") != critic_model.get("reasoning_effort")):
            raise ValueError(
                f"round {number}: ordinary critic snapshot model/effort differs from policy"
            )
        verify_inventory(snapshot, snapshot_manifest.get("files_sha256"))
        if digest(snapshot / "source/REPORT_TO_REVIEW.md") != manifest.get("report_sha256"):
            raise ValueError(f"round {number}: report snapshot hash is stale")

        def exact_invocation(result: dict[str, Any], result_path: pathlib.Path,
                             expected_role: str, expected_input_sha: str,
                             configured_model: dict[str, Any]) -> None:
            matches = [item for item in ledger_rows
                       if item.get("invocation_id") == result.get("invocation_id")]
            if len(matches) != 1:
                raise ValueError(
                    f"round {number} {expected_role}: result does not join one exact invocation ID"
                )
            telemetry = matches[0]
            expected = {
                "provider_call": True, "status": "completed", "round": number,
                "role": expected_role, "input_artifact_sha256": expected_input_sha,
                "prompt_sha256": result["input_hashes"]["prompt_sha256"],
                "output_sha256": digest(result_path),
                "requested_model": configured_model["requested_model"],
                "provider": configured_model["provider"],
                "reasoning_effort": configured_model["reasoning_effort"],
            }
            if any(telemetry.get(key) != value for key, value in expected.items()):
                raise ValueError(
                    f"round {number} {expected_role}: telemetry/output join is not exact"
                )

        actual_assessments: dict[str, list[str]] = {}
        result_rows: list[tuple[str, dict[str, Any]]] = []
        for role in roles:
            result_path = projection / f"critic_{role}.json"
            markdown_path = projection / f"critic_{role}.md"
            sidecar = load(projection / f"{role}.complete.json")
            result = load(result_path)
            schema_errors = validate_json_schema.errors_for_value(
                result, SKILL / "schemas/v2/critic-result.schema.json",
            )
            if schema_errors:
                raise ValueError(
                    f"round {number} critic_{role}: critic-result schema errors: "
                    + "; ".join(schema_errors[:20])
                )
            validate_v2.validate_critic_result(result)
            verify_result_evidence(result, snapshot)
            expected_sidecar = {
                "completion_version": "i4r-runtime-completion-2", "audit_id": audit_id,
                "input_sha256": manifest["input_sha256"], "report_sha256": manifest["report_sha256"],
                "round": number, "role": role, "completion_marker": True,
                "output_sha256": digest(result_path), "markdown_sha256": digest(markdown_path),
            }
            if any(sidecar.get(key) != value for key, value in expected_sidecar.items()):
                raise ValueError(f"round {number} critic_{role}: completion sidecar mismatch")
            if not isinstance(sidecar.get("attempts"), int) or not 1 <= sidecar["attempts"] <= 3:
                raise ValueError(f"round {number} critic_{role}: invalid round-wide attempt count")
            if (result.get("audit_id") != audit_id or result.get("round") != number or
                    result.get("role") != f"critic_{role}" or
                    result.get("report_sha256") != manifest["report_sha256"] or
                    result.get("status") != "complete" or result.get("completion_marker") is not True):
                raise ValueError(f"round {number} critic_{role}: result identity/completion mismatch")
            hashes = result["input_hashes"]
            expected_hashes = {
                "snapshot_sha256": manifest["input_sha256"],
                "report_sha256": manifest["report_sha256"],
                "prompt_sha256": digest(snapshot / f"rendered/prompt_{role}.txt"),
                "policy_sha256": digest(snapshot / "source/context/PIPELINE_POLICY.json"),
                "evidence_manifest_sha256": digest(
                    snapshot / "source/context/EVIDENCE_HASHES.json"),
            }
            if hashes != expected_hashes:
                raise ValueError(f"round {number} critic_{role}: input-hash binding mismatch")
            assessed_claims = [item["finding_id"] for item in result["items"]]
            actual_assessments[role] = assessed_claims
            result_rows.append((role, result))
            if number > 1 and (result["covered_claim_ids"] != assignments[role] or
                               assessed_claims != assignments[role]):
                raise ValueError(
                    f"round {number} critic_{role}: actual claim assessments are not assigned"
                )
            exact_invocation(
                result, result_path, f"critic_{role}", manifest["input_sha256"], critic_model,
            )

        actual_rereviewed = list(dict.fromkeys(
            claim_id for role in roles for claim_id in actual_assessments[role]
        ))
        if manifest.get("claim_assessments") != actual_assessments:
            raise ValueError(f"round {number}: projection claim assessments are not actual results")
        if manifest.get("rereviewed_claim_ids") != actual_rereviewed:
            raise ValueError(f"round {number}: rereview projection is not derived from assessments")
        if number > 1:
            if actual_rereviewed != expected_claims:
                raise ValueError(f"round {number}: changed claims lack exact completed reassessment")
            for claim_id in actual_rereviewed:
                if claim_id not in rereviewed_change_ids:
                    rereviewed_change_ids.append(claim_id)

        merge_path = projection / "CLAIM_MERGE.json"
        merge = load(merge_path)
        packet = load(projection / "adjudication_packet.json")
        if (manifest.get("claim_merge_sha256") != digest(merge_path) or
                packet.get("claim_merge_sha256") != digest(merge_path)):
            raise ValueError(f"round {number}: deterministic claim merge hash is unbound")
        frozen_registry = snapshot / "source/context/work/findings.jsonl"
        expected_merge = critique_runtime.build_claim_merge(
            result_rows, critique_runtime.load_registry_file(frozen_registry),
            packet.get("triggers", []), audit_id=audit_id, round_number=number,
            input_sha=manifest["input_sha256"], report_sha=manifest["report_sha256"],
        )
        if merge != expected_merge:
            raise ValueError(f"round {number}: claim merge differs from exact role assessments")
        if packet.get("unresolved_rows") != critique_runtime.unresolved_rows_for_packet(
                packet.get("triggers", []), merge):
            raise ValueError(f"round {number}: targeted packet includes non-unresolved merge rows")

        adjudication = manifest.get("adjudication")
        triggers = packet.get("triggers")
        if not isinstance(triggers, list):
            raise ValueError(f"round {number}: adjudication packet lacks trigger array")
        target_names = ("CRITIQUE.json", "CRITIQUE.md", "targeted_adjudicator.complete.json")
        if adjudication == "complete":
            if not triggers:
                raise ValueError(f"round {number}: completed targeted adjudication has no trigger")
            target_sha = manifest.get("target_input_sha256")
            if not isinstance(target_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", target_sha):
                raise ValueError(f"round {number}: completed adjudication lacks target input hash")
            target = round_dir / "target_snapshots" / target_sha
            target_manifest = load(target / "MANIFEST.json")
            if (target_manifest.get("snapshot_version") != "i4r-target-snapshot-2" or
                    target_manifest.get("target_input_sha256") != target_sha or
                    target_manifest.get("round_input_sha256") != manifest["input_sha256"] or
                    target_manifest.get("report_sha256") != manifest["report_sha256"] or
                    target_manifest.get("round") != number or
                    target_manifest.get("audit_id") != audit_id):
                raise ValueError(f"round {number}: targeted snapshot identity mismatch")
            if (target_manifest.get("model") != targeted_model.get("requested_model") or
                    target_manifest.get("effort") != targeted_model.get("reasoning_effort")):
                raise ValueError(
                    f"round {number}: targeted adjudicator snapshot model/effort differs from policy"
                )
            verify_inventory(target, target_manifest.get("files_sha256"))
            target_result_path = projection / "CRITIQUE.json"
            target_markdown = projection / "CRITIQUE.md"
            target_result = load(target_result_path)
            target_sidecar = load(projection / "targeted_adjudicator.complete.json")
            schema_errors = validate_json_schema.errors_for_value(
                target_result, SKILL / "schemas/v2/critic-result.schema.json",
            )
            if schema_errors:
                raise ValueError(
                    f"round {number} targeted adjudicator schema errors: "
                    + "; ".join(schema_errors[:20])
                )
            validate_v2.validate_critic_result(target_result)
            verify_result_evidence(target_result, target)
            target_expected_sidecar = {
                "completion_version": "i4r-runtime-completion-2", "audit_id": audit_id,
                "input_sha256": target_sha, "report_sha256": manifest["report_sha256"],
                "round": number, "role": "targeted_adjudicator", "completion_marker": True,
                "output_sha256": digest(target_result_path),
                "markdown_sha256": digest(target_markdown),
            }
            if any(target_sidecar.get(key) != value
                   for key, value in target_expected_sidecar.items()):
                raise ValueError(f"round {number}: targeted adjudication sidecar mismatch")
            if (target_result.get("audit_id") != audit_id or target_result.get("round") != number or
                    target_result.get("role") != "critic_synth" or
                    target_result.get("report_sha256") != manifest["report_sha256"] or
                    target_result.get("status") != "complete" or
                    target_result.get("completion_marker") is not True or
                    target_result.get("input_hashes") != target_manifest.get("input_hashes")):
                raise ValueError(f"round {number}: targeted result identity/input binding mismatch")
            unresolved_ids = [item.get("row_id") for item in packet.get("unresolved_rows", [])]
            covered_items = [item.get("finding_id") for item in target_result["items"]]
            if (target_result["coverage"]["assigned_checks"] != unresolved_ids or
                    target_result["covered_claim_ids"] != unresolved_ids or
                    covered_items != unresolved_ids):
                raise ValueError(f"round {number}: targeted result does not cover unresolved rows")
            provider_packet = load(target / "source/adjudication_packet.json")
            if set(provider_packet) != {
                    "packet_version", "audit_id", "round", "input_sha256", "report_sha256",
                    "policy", "claim_merge_sha256", "unresolved_rows"}:
                raise ValueError(f"round {number}: target snapshot exposes non-delta packet fields")
            if (provider_packet.get("packet_version") != "i4r-targeted-adjudication-input-1" or
                    provider_packet.get("unresolved_rows") != packet.get("unresolved_rows")):
                raise ValueError(f"round {number}: target snapshot unresolved rows differ")
            if (target / "source/critics").exists() or (target / "source/critics").is_symlink():
                raise ValueError(f"round {number}: target snapshot exposes full critic outputs")
            exact_invocation(
                target_result, target_result_path, "critic_synth", target_sha, targeted_model,
            )
        else:
            if manifest.get("target_input_sha256") is not None:
                raise ValueError(f"round {number}: no-target projection declares target input")
            if any((projection / name).exists() or (projection / name).is_symlink()
                   for name in target_names):
                raise ValueError(f"round {number}: no-target projection contains targeted output")

        status = load(round_dir / "ROUND_STATUS.json")
        if (status.get("status_version") != "i4r-round-status-2" or
                status.get("invocation_id") != invocation_id or status.get("audit_id") != audit_id or
                status.get("round") != number or status.get("input_sha256") != manifest["input_sha256"] or
                status.get("report_sha256") != manifest["report_sha256"] or
                status.get("expected_roles") != roles or status.get("completed_roles") != roles or
                status.get("failed_roles") != [] or status.get("quorum_met") is not True or
                status.get("outcome") != "complete" or status.get("exit_code") != 0):
            raise ValueError(f"round {number}: terminal status does not prove exact successful quorum")
        # Runtime critique validation happens before the coordinator integrates the
        # delta and runs the whole-document gates. Finalization/publication opt in
        # to this receipt requirement, preserving a strict release boundary.
        if number > 1 and require_post_delta_gates:
            receipt = round_dir / "POST_DELTA_GATES.json"
            try:
                run_post_delta_gates.verify_receipt(
                    audit, receipt, require_current=(number == len(rounds))
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"round {number}: post-delta whole-document gates are not current: {exc}"
                ) from exc
    if run.get("stopping_reason") in {"target_complete", "converged"}:
        if run.get("changed_claim_ids") != rereviewed_change_ids:
            raise ValueError(
                "run changed_claim_ids is not the exact ordered union of generated later-round diffs"
            )
        if run.get("rereviewed_claim_ids") != rereviewed_change_ids:
            raise ValueError(
                "run rereviewed_claim_ids is not exactly joined to assigned completed claim coverage"
            )
    print(f"immutable critique projections: {len(rounds)} rounds exactly joined")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument(
        "--require-post-delta-gates",
        action="store_true",
        help="require current passing whole-document gate receipts for rounds 2+",
    )
    args = parser.parse_args()
    try:
        return verify(
            args.audit_dir,
            require_post_delta_gates=args.require_post_delta_gates,
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
