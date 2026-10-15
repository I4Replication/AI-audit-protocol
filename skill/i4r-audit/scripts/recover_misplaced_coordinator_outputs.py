#!/usr/bin/env python3
"""Recover a coordinator attempt whose four outputs were written to the attempt directory.

    python3 scripts/recover_misplaced_coordinator_outputs.py "$AUDIT_DIR" \\
        --attempt coordinator/invocations/<input-sha256>/attempt_NN_<invocation-id>

This utility is deliberately narrow. It exists for exactly one failure: the provider call
succeeded, the model wrote REPORT_v1.md, ADJUDICATION.md, findings.jsonl and
retired_claims.txt, but into the attempt directory rather than its ``output.partial``
child, so ``run_coordinator.py`` found nothing to validate and recorded the attempt as
``failed`` with ``error_class=schema``. The Claude CLI's built-in Write tool is not confined
by the sandbox filesystem rules that govern shell commands, so nothing but the prompt's
"current directory" steered it. The runner now makes the attempt root read-only during the
call; this tool repairs the one attempt that predates that fix, or any future recurrence.

What it does, all fail-closed and under the coordinator promotion lock:

1. Verifies the attempt is bound to its input hash, its ledger row has exactly this defect,
   no coordinator call has completed, no bundle or promotion transaction exists, the
   provider envelope is an exact success with zero denials and one served primary of the
   audit's frozen Claude route matching the record, the prompt and sandbox settings are
   canonical, and every live input still matches the immutable snapshot.
2. Moves the four files, byte for byte, into ``output.partial`` and re-hashes them.
3. Runs the standard ``validate_outputs`` -- the same checks a first-time promotion gets.
4. Builds the canonical bundle and a corrected record, journals an ``invocation_correction``
   (status, output binding, error cleared; identity and attempt accounting untouched), and
   promotes the files through the shared transaction path.
5. Writes ``coordinator/recoveries/<invocation>/receipt.json`` recording the relocation.

No provider is called, no attempt budget is consumed, and the original failed terminal
event stays in the hash chain. If any check fails after the move, the files are moved back.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import pathlib
import re
import sys
from typing import Any

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

import claude_routes  # noqa: E402
import record_invocation  # noqa: E402
import recover_coordinator as base  # noqa: E402
import run_coordinator as coordinator  # noqa: E402

RECEIPT_SCHEMA = "i4r-coordinator-misplaced-output-recovery-1"
ATTEMPT_NAME = re.compile(r"^attempt_(\d{2,})_(coordinator-[0-9a-f-]{36})$")
DEFECT_PREFIX = "required coordinator output REPORT_v1.md must be a regular non-symlink file"
CORRECTED_FIELDS = ("status", "output_artifact", "output_sha256", "error", "error_class")


class RecoveryError(coordinator.CoordinatorError):
    """A precondition for this narrow recovery was not met."""


def find_misplaced_outputs(attempt: pathlib.Path) -> dict[str, str]:
    """Return {name: sha256} for the four outputs sitting in the attempt root.

    Fails unless all four are present there as regular non-symlink files and
    ``output.partial`` holds no regular file at all.
    """
    output = attempt / "output.partial"
    if output.is_symlink() or not output.is_dir():
        raise RecoveryError("attempt has no output.partial directory")
    stray = [p for p in output.rglob("*") if p.is_file() or p.is_symlink()]
    if stray:
        raise RecoveryError("output.partial is not empty; this is not the misplaced-output defect")
    hashes: dict[str, str] = {}
    for name in coordinator.OUTPUT_TARGETS:
        path = attempt / name
        if path.is_symlink() or not path.is_file():
            raise RecoveryError(f"expected misplaced output is missing from the attempt root: {name}")
        hashes[name] = coordinator.sha256_file(path)
    return hashes


def relocate(attempt: pathlib.Path, hashes: dict[str, str]) -> None:
    output = attempt / "output.partial"
    moved: list[str] = []
    try:
        for name, digest in hashes.items():
            os.replace(attempt / name, output / name)
            moved.append(name)
            if coordinator.sha256_file(output / name) != digest:
                raise RecoveryError(f"output changed while being relocated: {name}")
    except Exception:
        for name in reversed(moved):
            if (output / name).is_file():
                os.replace(output / name, attempt / name)
        raise


def unrelocate(attempt: pathlib.Path, names: list[str]) -> None:
    output = attempt / "output.partial"
    for name in names:
        if (output / name).is_file():
            os.replace(output / name, attempt / name)


def validate_live_inputs(
    root: pathlib.Path, attempt: pathlib.Path, manifest: dict[str, Any], *, metadata_21: bool,
) -> tuple[list[pathlib.Path], list[pathlib.Path], list[dict[str, Any]], dict[str, str]]:
    """recover_coordinator.validate_live_inputs, with the runner's run.json exemption.

    Under metadata 2.1 ``run.json`` is a journal projection: the runner snapshots its exact
    pre-reservation bytes as packet content precisely because lifecycle events (including
    this attempt's own failure) rewrite the live file afterwards. Comparing the live file
    would call every recoverable attempt stale. Everything else is checked as before.
    """
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for entry in manifest["files"]:
        by_kind.setdefault(entry["kind"], []).append(entry)
        path = base.live_path(root, entry)
        if path is None:
            continue
        if metadata_21 and entry["kind"] == "audit_context" and entry["origin"] == "run.json":
            # The model saw the snapshot's frozen bytes, never the live projection, so neither
            # its hash nor its text is a precondition here; the snapshot tree check covers it.
            continue
        if entry["kind"] == "audit_context" and entry["origin"] == coordinator.PACKAGE_MANIFEST:
            # 2.6.8: compared as the runner projects it (masked code-shaped file names).
            try:
                base.check_live_package_manifest(root, entry, path)
            except coordinator.CoordinatorError as exc:
                raise RecoveryError(str(exc)) from exc
            continue
        if coordinator.sha256_file(path) != entry["sha256"] or path.stat().st_size != entry["bytes"]:
            raise RecoveryError(f"live coordinator input became stale: {entry['origin']}")
        coordinator.validate_provider_visible_text(
            path, f"live coordinator input {entry['origin']}", coordinator.deposit_exemptions(root)
        )
    if {e["origin"] for e in by_kind.get("phase1_report", [])} != set(coordinator.PHASE_ONE_REPORTS.values()):
        raise RecoveryError("snapshot does not contain the exact Phase-1 report quorum")
    if {e["origin"] for e in by_kind.get("audit_context", [])} != set(coordinator.REQUIRED_AUDIT_CONTEXT):
        raise RecoveryError("snapshot does not contain the exact audit context")
    if not set(coordinator.REQUIRED_REFERENCES).issubset({e["origin"] for e in by_kind.get("reference", [])}):
        raise RecoveryError("snapshot lacks a required coordinator reference")
    if len(by_kind.get("phase1_bindings", [])) != 1 or len(by_kind.get("safe_evidence_manifest", [])) != 1:
        raise RecoveryError("snapshot requires exactly one Phase-1 binding record and one safe-evidence manifest")
    if not by_kind.get("draft_registry"):
        raise RecoveryError("snapshot has no draft registry")
    rows = coordinator.read_ledger(root)
    policy = record_invocation.load_policy_snapshot(root)
    record_invocation.validate_policy_binding(root)
    bindings, report_hashes = coordinator.phase_one_bindings(root, rows, policy)
    expected_binding = coordinator.pretty_bytes({"schema_version": "1.0.0", "bindings": bindings})
    binding_snapshot = attempt / "source" / by_kind["phase1_bindings"][0]["snapshot"]
    if binding_snapshot.read_bytes() != expected_binding:
        raise RecoveryError("Phase-1 invocation bindings no longer match the ledger")
    draft_paths = [root / e["origin"] for e in by_kind["draft_registry"]]
    safe_paths = coordinator.load_safe_evidence(root, root / by_kind["safe_evidence_manifest"][0]["origin"])
    observed_safe = {coordinator.audit_relative(root, p) for p in safe_paths[1:]}
    if observed_safe != {e["origin"] for e in by_kind.get("safe_evidence", [])}:
        raise RecoveryError("safe-evidence snapshot disagrees with its live manifest")
    return draft_paths, safe_paths, rows, report_hashes


def failed_record(rows: list[dict[str, Any]], invocation_id: str, attempt_number: int, input_hash: str,
                  route: dict[str, Any] | None = None) -> dict[str, Any]:
    """The one failed row with the misplaced-output profile. ``route`` is the audit snapshot's
    coordinator route (2.7.0); None is the one route every earlier audit froze, Sonnet 5."""
    route = route or claude_routes.SONNET_5
    matching = [row for row in rows if row.get("invocation_id") == invocation_id]
    if len(matching) != 1:
        raise RecoveryError(f"ledger must hold exactly one row for {invocation_id}; found {len(matching)}")
    row = matching[0]
    expected = {
        "role": "coordinator", "role_class": "coordinator", "round": None,
        "attempt": attempt_number, "status": "failed", "provider": "anthropic",
        "provider_call": True, "requested_model": route["requested_model"], "model_family": "claude",
        "reasoning_effort": route["reasoning_effort"], "input_artifact_sha256": input_hash,
        "error_class": "schema", "output_artifact": None, "output_sha256": None,
        "provider_request_status": "accepted", "inference_start_status": "started",
    }
    wrong = [key for key, value in expected.items() if row.get(key) != value]
    if wrong:
        raise RecoveryError("failed coordinator row does not have the misplaced-output profile: " + ", ".join(wrong))
    error = row.get("error")
    if not isinstance(error, str) or not error.startswith(DEFECT_PREFIX):
        raise RecoveryError("failed coordinator row records a different error than the missing REPORT_v1.md")
    if coordinator.completed_for_input(rows, input_hash) is not None:
        raise RecoveryError("a coordinator call has already completed; recovery is unnecessary")
    return row


def validate_envelope(attempt: pathlib.Path, record: dict[str, Any]) -> dict[str, Any]:
    stdout_path = base.ensure_regular(attempt / "claude_stdout.json", "coordinator provider envelope")
    stderr_path = base.ensure_regular(attempt / "claude_stderr.txt", "coordinator provider stderr")
    if stderr_path.read_text(encoding="utf-8").strip():
        raise RecoveryError("coordinator provider stderr is non-empty")
    result = coordinator.parse_provider_result(stdout_path.read_text(encoding="utf-8"))
    if (
        result.get("type") != "result" or result.get("subtype") != "success"
        or result.get("is_error") is not False or result.get("terminal_reason") != "completed"
        or result.get("stop_reason") != "end_turn"
    ):
        raise RecoveryError("coordinator provider envelope is not an exact success")
    # failed_record has bound requested_model to the snapshot's coordinator route.
    requested_model = record.get("requested_model")
    primary, all_models, primary_count = coordinator.observed_models(result, requested_model)
    if (
        primary_count != 1 or primary is None
        or not claude_routes.serves(requested_model, primary)
    ):
        raise RecoveryError(
            f"expected exactly one served {claude_routes.short_name(requested_model)} primary; "
            f"observed {all_models!r}"
        )
    if primary != record.get("observed_model") or all_models != record.get("observed_model_ids"):
        raise RecoveryError("served model IDs disagree with the failed record")
    usage, cache_usage = coordinator.extract_usage(result)
    if not coordinator.complete_usage(usage) or usage != record.get("usage"):
        raise RecoveryError("provider usage is incomplete or disagrees with the failed record")
    if cache_usage != record.get("cache_usage") or result.get("session_id") != record.get("session_id"):
        raise RecoveryError("provider cache usage or session disagrees with the failed record")
    denials = result.get("permission_denials")
    if not isinstance(denials, list) or denials:
        raise RecoveryError("strict recovery requires zero provider permission denials")
    denial_receipt = coordinator.strict_object(
        base.ensure_regular(attempt / "permission_denials.json", "permission-denial receipt"))
    if denial_receipt != {"count": 0, "tool_names": [], "raw_stdout_sha256": coordinator.sha256_file(stdout_path)}:
        raise RecoveryError("permission-denial receipt disagrees with provider output")
    return {"primary": primary, "all_models": all_models, "stdout_sha256": coordinator.sha256_file(stdout_path)}


def validate_canonical_inputs(root: pathlib.Path, attempt: pathlib.Path, audit_id: str,
                              invocation_id: str, input_hash: str, record: dict[str, Any]) -> None:
    prompt_path = base.ensure_regular(attempt / "prompt.txt", "coordinator prompt")
    if coordinator.sha256_file(prompt_path) != record["prompt_sha256"]:
        raise RecoveryError("coordinator prompt hash disagrees with the failed record")
    settings = coordinator.strict_object(base.ensure_regular(attempt / "claude_settings.json", "sandbox settings"))
    if settings != coordinator.sandbox_settings(root, attempt, attempt / "source", attempt / "output.partial"):
        raise RecoveryError("coordinator sandbox settings are not canonical")
    if (attempt / "coordinator_bundle.json").exists():
        raise RecoveryError("a coordinator bundle already exists; this attempt was validated before")
    transaction = root / "coordinator/transactions" / invocation_id
    if transaction.exists() or transaction.is_symlink():
        raise RecoveryError("a promotion transaction already exists for this invocation")
    for artifact_target in coordinator.OUTPUT_TARGETS.values():
        if (root / artifact_target).exists():
            raise RecoveryError(f"canonical coordinator target already exists: {artifact_target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--attempt", required=True, type=pathlib.Path,
                        help="audit-relative or absolute quarantined coordinator attempt directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="check every precondition and report; move and promote nothing")
    args = parser.parse_args(argv)
    root = args.audit_dir.resolve()
    relocated: list[str] = []
    try:
        if not root.is_dir():
            raise RecoveryError(f"audit directory does not exist: {root}")
        attempt = base.strict_path(root, args.attempt)
        match = ATTEMPT_NAME.fullmatch(attempt.name)
        if match is None:
            raise RecoveryError(f"not a coordinator attempt directory: {attempt.name}")
        attempt_number, invocation_id = int(match.group(1)), match.group(2)
        manifest, input_hash = base.load_input_manifest(attempt)
        if attempt.parent.name != input_hash:
            raise RecoveryError("attempt directory is not bound to its input hash")
        run = coordinator.strict_object(base.ensure_regular(root / "run.json", "run metadata"))
        audit_id = run.get("audit_id")
        if not isinstance(audit_id, str) or not audit_id:
            raise RecoveryError("run.json lacks an audit_id")
        metadata_21 = run.get("schema_version") == "2.1.0"

        lock_path = root / "coordinator/promotion.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            draft_paths, safe_paths, rows, report_hashes = validate_live_inputs(
                root, attempt, manifest, metadata_21=metadata_21)
            route = (record_invocation.load_policy_snapshot(root).get("models") or {}).get("coordinator")
            if not isinstance(route, dict):
                raise RecoveryError("the audit policy snapshot names no coordinator route")
            record = failed_record(rows, invocation_id, attempt_number, input_hash, route)
            envelope = validate_envelope(attempt, record)
            validate_canonical_inputs(root, attempt, audit_id, invocation_id, input_hash, record)
            hashes = find_misplaced_outputs(attempt)
            if args.dry_run:
                print(json.dumps({"status": "preconditions_ok", "dry_run": True,
                                  "invocation_id": invocation_id, "attempt": attempt_number,
                                  "misplaced_outputs": hashes}, sort_keys=True, indent=2))
                return 0

            output = attempt / "output.partial"
            # The runner froze the failed attempt (dirs 0o555, files 0o444). Renaming into or out
            # of a directory and writing the bundle both need the directory writable; the tree is
            # frozen again by freeze_attempt() once the recovery has committed.
            os.chmod(attempt, 0o755)
            os.chmod(output, 0o755)
            relocate(attempt, hashes)
            relocated = list(hashes)
            draft_rows, draft_ids = coordinator.load_draft_rows(draft_paths)
            known_pairs, known_paths, _canonical = coordinator.known_evidence(
                draft_rows, report_hashes, root, safe_paths)
            artifacts, findings = coordinator.validate_outputs(output, draft_ids, known_pairs, known_paths, rows)

            bundle = {
                "schema_version": "1.0.0", "audit_id": audit_id, "invocation_id": invocation_id,
                "input_artifact_sha256": input_hash, "prompt_sha256": record["prompt_sha256"],
                "observed_model": envelope["primary"], "observed_model_ids": envelope["all_models"],
                "provider_stdout_sha256": envelope["stdout_sha256"], "outputs": artifacts,
                "finding_ids": [row["id"] for row in findings],
            }
            bundle_path = attempt / "coordinator_bundle.json"
            bundle_path.write_bytes(coordinator.pretty_bytes(bundle))
            bundle_relative = coordinator.audit_relative(root, bundle_path)
            changes = {
                "status": "completed", "output_artifact": bundle_relative,
                "output_sha256": coordinator.sha256_file(bundle_path), "error": None, "error_class": None,
            }
            corrected = copy.deepcopy(record)
            corrected.update(changes)
            record_invocation.validate_record(corrected, root)
            reason = (
                "coordinator outputs were written to the attempt directory instead of output.partial; "
                "relocated byte-for-byte, revalidated with the standard output contract, and bound. "
                "No provider call was made."
            )

            def journal_correction(record_root: pathlib.Path, value: dict[str, Any]) -> None:
                if metadata_21:
                    record_invocation.correct_invocation(record_root, invocation_id, changes, reason=reason)
                else:  # legacy direct ledger: rewrite is not allowed; append the corrected record
                    record_invocation.append(record_root, value)

            # promote_and_append re-verifies live inputs from InputSpec objects; every live input
            # was already verified against the manifest by base.validate_live_inputs above, under
            # this same lock, so no specs are passed here and that loop is vacuous by design.
            specs: list[coordinator.InputSpec] = []
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            coordinator.promote_and_append(
                root, output, artifacts, corrected, specs, manifest,
                transaction_name=f"{invocation_id}-recovery", metadata_21=metadata_21,
                append_record=journal_correction,
            )
            relocated = []  # promoted; never move back now
            # promote_and_append treats an already-present invocation as "ledger step done" when
            # deciding whether to roll back, and this invocation was present (failed) before we
            # started. Prove the correction actually landed rather than assuming it.
            projected = [row for row in coordinator.read_ledger(root) if row.get("invocation_id") == invocation_id]
            if len(projected) != 1 or projected[0].get("status") != "completed" \
                    or projected[0].get("output_sha256") != changes["output_sha256"]:
                raise RecoveryError(
                    "files were promoted but the invocation correction did not project as completed; "
                    "inspect run.events.jsonl before retrying"
                )

            receipt_dir = root / "coordinator/recoveries" / invocation_id
            receipt_dir.mkdir(parents=True, exist_ok=False)
            receipt = {
                "schema_version": RECEIPT_SCHEMA, "state": "completed", "mode": "no_provider",
                "provider_called_during_recovery": False,
                "recovered_at": coordinator.iso(coordinator.utc_now()),
                "audit_id": audit_id, "invocation_id": invocation_id, "attempt": attempt_number,
                "attempt_directory": coordinator.audit_relative(root, attempt),
                "input_artifact_sha256": input_hash,
                "defect": "outputs written to the attempt directory instead of output.partial",
                "relocated_outputs": [
                    {"name": name, "from": coordinator.audit_relative(root, attempt / name),
                     "to": coordinator.audit_relative(root, output / name), "sha256": digest}
                    for name, digest in hashes.items()
                ],
                "corrected_fields": list(CORRECTED_FIELDS), "corrections": changes, "reason": reason,
                "provider_envelope_sha256": envelope["stdout_sha256"],
                "coordinator_bundle_sha256": changes["output_sha256"],
                "finding_ids": bundle["finding_ids"], "outputs": artifacts,
                "checks": {"live_inputs": True, "failed_row_profile": True, "provider_success": True,
                           "served_model": True, "canonical_prompt_and_settings": True,
                           "relocation_byte_identical": True, "standard_output_validation": True},
            }
            coordinator.atomic_write(receipt_dir / "receipt.json", coordinator.pretty_bytes(receipt))
            os.chmod(receipt_dir / "receipt.json", 0o444)
            status = coordinator.strict_object(attempt / "status.json")
            status.update({"state": "completed", "recovery": {
                "mode": "no_provider", "receipt": coordinator.audit_relative(root, receipt_dir / "receipt.json"),
                "corrected_fields": list(CORRECTED_FIELDS)}})
            coordinator.atomic_write(attempt / "status.json", coordinator.pretty_bytes(status))
            coordinator.freeze_attempt(attempt)
        print(json.dumps({"status": "completed", "mode": "no_provider", "provider_called_during_recovery": False,
                          "invocation_id": invocation_id, "attempt": attempt_number,
                          "corrected_fields": list(CORRECTED_FIELDS),
                          "receipt": coordinator.audit_relative(root, receipt_dir / "receipt.json"),
                          "finding_ids": bundle["finding_ids"]}, sort_keys=True))
        return 0
    except (coordinator.CoordinatorError, OSError, KeyError, TypeError, ValueError) as exc:
        if relocated:
            try:
                unrelocate(attempt, relocated)  # noqa: F821 - attempt is bound before relocation
            except Exception as undo:  # pragma: no cover
                print(f"WARNING: could not move outputs back: {undo}", file=sys.stderr)
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
