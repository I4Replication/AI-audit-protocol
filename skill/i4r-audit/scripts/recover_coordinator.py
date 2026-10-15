#!/usr/bin/env python3
"""Recover one quarantined coordinator promotion without another provider call.

This utility is intentionally narrow.  It is only for the historical failure mode in which
``run_coordinator.py`` completed and validated the provider response, wrote its bundle and output
files, then rejected its own invocation record because ``elapsed_seconds`` came from a clock that
paused during system sleep.  Recovery is allowed only when the persisted record has exactly that
one defect.  Every content, model, usage, schema, privacy, and live-input binding is revalidated
under the promotion lock before anything is promoted.

The original invalid invocation record and the failed promotion transaction are forensic inputs;
they are never rewritten or removed.  The corrected record is appended to the canonical ledger,
and a separate recovery receipt records the single-field correction.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import json
import os
import pathlib
import re
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Iterable


HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
import claude_routes
import record_invocation
import run_coordinator as coordinator


ATTEMPT_NAME = re.compile(r"^attempt_(\d{2})_(coordinator-[A-Za-z0-9-]+)$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_INPUT_KINDS = {
    "phase1_report",
    "draft_registry",
    "safe_evidence_manifest",
    "safe_evidence",
    "audit_context",
    "reference",
    "phase1_bindings",
}
RECEIPT_SCHEMA = "i4r-coordinator-recovery-1"


@dataclass(frozen=True)
class RecoveryState:
    root: pathlib.Path
    attempt: pathlib.Path
    output: pathlib.Path
    invocation_id: str
    attempt_number: int
    audit_id: str
    input_manifest: dict[str, Any]
    input_hash: str
    artifacts: dict[str, dict[str, Any]]
    finding_ids: list[str]
    corrected_record: dict[str, Any]
    original_record_hash: str
    original_record_validation_error: str
    original_elapsed: float
    corrected_elapsed: float
    prior_status_hash: str
    attempt_tree_hash: str
    failed_transaction: pathlib.Path
    failed_transaction_tree_hash: str
    permission_denials: dict[str, Any]


def strict_path(root: pathlib.Path, supplied: pathlib.Path) -> pathlib.Path:
    path = supplied if supplied.is_absolute() else root / supplied
    if path.is_symlink() or not path.is_dir():
        raise coordinator.CoordinatorError(
            f"coordinator recovery attempt must be a regular directory: {path}"
        )
    resolved = path.resolve()
    attempts_root = (root / "coordinator/invocations").resolve()
    try:
        relative = resolved.relative_to(attempts_root)
    except ValueError as exc:
        raise coordinator.CoordinatorError(
            "coordinator recovery attempt must be under coordinator/invocations"
        ) from exc
    if len(relative.parts) != 2 or not SHA256.fullmatch(relative.parts[0]):
        raise coordinator.CoordinatorError("coordinator recovery attempt has an unsafe layout")
    match = ATTEMPT_NAME.fullmatch(relative.parts[1])
    if not match:
        raise coordinator.CoordinatorError("coordinator recovery attempt name is invalid")
    return resolved


def ensure_regular(path: pathlib.Path, label: str) -> pathlib.Path:
    return coordinator.require_regular(path, label)


def parse_timestamp(value: Any, field: str) -> dt.datetime:
    try:
        return record_invocation.parse_time(value, field)
    except ValueError as exc:
        raise coordinator.CoordinatorError(str(exc)) from exc


def safe_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise coordinator.CoordinatorError(f"{label} must be a non-empty POSIX path")
    pure = pathlib.PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != value:
        raise coordinator.CoordinatorError(f"{label} is unsafe: {value!r}")
    return value


def tree_digest(path: pathlib.Path) -> str:
    """Hash a forensic tree without following links."""
    rows: list[dict[str, Any]] = []
    for item in sorted([path, *path.rglob("*")], key=lambda value: value.as_posix()):
        info = item.lstat()
        relative = "." if item == path else item.relative_to(path).as_posix()
        if stat.S_ISLNK(info.st_mode):
            kind = "symlink"
            digest = coordinator.sha256_bytes(os.readlink(item).encode("utf-8"))
            size = len(os.readlink(item).encode("utf-8"))
        elif stat.S_ISDIR(info.st_mode):
            kind = "directory"
            digest = None
            size = None
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
            digest = coordinator.sha256_file(item)
            size = info.st_size
        else:
            kind = "other"
            digest = None
            size = info.st_size
        rows.append({
            "path": relative,
            "kind": kind,
            "mode": stat.S_IMODE(info.st_mode),
            "bytes": size,
            "sha256": digest,
        })
    return coordinator.sha256_bytes(coordinator.canonical_bytes(rows))


SNAPSHOT_MODE_REMEDY = (
    "the content verified unchanged, so only a mode drifted (a synced folder re-applying a "
    "permission is the usual cause). Remedy: chmod -R a-w the attempt's source/ directory, or "
    "re-run with I4R_SNAPSHOT_REFREEZE=1 to re-freeze it after these same integrity checks"
)


def validate_snapshot_tree(
    source: pathlib.Path, manifest: dict[str, Any], exempt: Iterable[str] = frozenset()
) -> None:
    # Integrity first -- links, hashes, closure, provider-visible text -- and modes last, so a
    # mode-only drift is told apart from a content change and never repaired over one.
    if source.is_symlink() or not source.is_dir():
        raise coordinator.CoordinatorError("immutable snapshot root is unsafe or writable")
    coordinator.verify_snapshot(source, manifest)
    expected = {entry["snapshot"] for entry in manifest["files"]}
    expected.add("input_manifest.json")
    observed: set[str] = set()
    writable: list[str] = []
    for path in source.rglob("*"):
        info = path.lstat()
        relative = path.relative_to(source).as_posix()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise coordinator.CoordinatorError(f"immutable snapshot contains an unsafe entry: {relative}")
        if info.st_mode & 0o222:
            writable.append(relative)
        if stat.S_ISREG(info.st_mode):
            observed.add(relative)
            coordinator.validate_provider_visible_text(path, f"snapshot input {relative}", exempt)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise coordinator.CoordinatorError(
            f"immutable snapshot closure mismatch; missing={missing!r}; extra={extra!r}"
        )
    root_writable = bool(source.stat().st_mode & 0o222)
    if not root_writable and not writable:
        return
    if os.environ.get("I4R_SNAPSHOT_REFREEZE", "") != "1":
        if root_writable:
            raise coordinator.CoordinatorError(
                f"immutable snapshot root is unsafe or writable; {SNAPSHOT_MODE_REMEDY}")
        raise coordinator.CoordinatorError(
            f"immutable snapshot entry is writable: {writable[0]}; {SNAPSHOT_MODE_REMEDY}")
    # Opted in: re-freeze exactly as run_coordinator.materialize_snapshot froze it (files 0444,
    # directories 0555, root last), then insist it stayed frozen.
    for path in sorted(source.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    os.chmod(source, 0o555)
    if source.stat().st_mode & 0o222 or any(
            path.lstat().st_mode & 0o222 for path in source.rglob("*")):
        raise coordinator.CoordinatorError(
            "immutable snapshot could not be re-frozen: write permission came back immediately")
    print(
        f"  [note ] coordinator snapshot mode drift re-frozen ({len(writable) + root_writable} "
        "path(s)); content verified unchanged",
        file=sys.stderr,
    )


def load_input_manifest(
    attempt: pathlib.Path, exempt: Iterable[str] = frozenset()
) -> tuple[dict[str, Any], str]:
    path = ensure_regular(attempt / "source/input_manifest.json", "coordinator input manifest")
    manifest = coordinator.strict_object(path)
    if set(manifest) != {
        "schema_version", "files", "input_artifact_sha256", "total_bytes"
    } or manifest.get("schema_version") != "1.0.0":
        raise coordinator.CoordinatorError("coordinator input manifest has an invalid envelope")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise coordinator.CoordinatorError("coordinator input manifest has no files")
    seen_snapshots: set[str] = set()
    total = 0
    for index, entry in enumerate(files):
        if not isinstance(entry, dict) or set(entry) != {
            "kind", "origin", "snapshot", "sha256", "bytes"
        }:
            raise coordinator.CoordinatorError(f"input manifest files[{index}] has an invalid shape")
        if entry["kind"] not in ALLOWED_INPUT_KINDS:
            raise coordinator.CoordinatorError(f"input manifest files[{index}] has an invalid kind")
        safe_relative(entry["snapshot"], f"input manifest files[{index}].snapshot")
        if entry["snapshot"] in seen_snapshots:
            raise coordinator.CoordinatorError("coordinator input manifest repeats a snapshot path")
        seen_snapshots.add(entry["snapshot"])
        if (
            not isinstance(entry["origin"], str)
            or not SHA256.fullmatch(str(entry["sha256"]))
            or not isinstance(entry["bytes"], int)
            or isinstance(entry["bytes"], bool)
            or entry["bytes"] < 0
        ):
            raise coordinator.CoordinatorError(f"input manifest files[{index}] is malformed")
        total += entry["bytes"]
    descriptor = {"schema_version": "1.0.0", "files": files}
    calculated = coordinator.sha256_bytes(coordinator.canonical_bytes(descriptor))
    if manifest["input_artifact_sha256"] != calculated or manifest["total_bytes"] != total:
        raise coordinator.CoordinatorError("coordinator input manifest content hash or byte total is invalid")
    validate_snapshot_tree(attempt / "source", manifest, exempt)
    return manifest, calculated


def live_path(root: pathlib.Path, entry: dict[str, Any]) -> pathlib.Path | None:
    kind = entry["kind"]
    origin = entry["origin"]
    if kind == "phase1_bindings":
        if origin != "generated:phase1_bindings.json":
            raise coordinator.CoordinatorError("phase1_bindings has an unexpected origin")
        return None
    safe_relative(origin, f"live {kind} origin")
    base = SKILL if kind == "reference" else root
    path = ensure_regular(base / origin, f"live coordinator input {origin}")
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise coordinator.CoordinatorError(f"live coordinator input escapes its root: {origin}") from exc
    return path


def check_live_package_manifest(
    root: pathlib.Path, entry: dict[str, Any], path: pathlib.Path
) -> None:
    """Compare the live package manifest, as the runner projects it, with the snapshot entry."""
    data = coordinator.live_context_bytes(root, entry["origin"], path)
    if coordinator.sha256_bytes(data) != entry["sha256"] or len(data) != entry["bytes"]:
        raise coordinator.CoordinatorError(f"live coordinator input became stale: {entry['origin']}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise coordinator.CoordinatorError(
            f"live coordinator input {entry['origin']} is not UTF-8 text: {path}"
        ) from exc
    coordinator.validate_provider_visible_string(
        text, f"live coordinator input {entry['origin']}", coordinator.deposit_exemptions(root)
    )


def validate_live_inputs(
    root: pathlib.Path, attempt: pathlib.Path, manifest: dict[str, Any]
) -> tuple[list[pathlib.Path], list[pathlib.Path], list[dict[str, Any]], dict[str, str]]:
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for entry in manifest["files"]:
        by_kind.setdefault(entry["kind"], []).append(entry)
        path = live_path(root, entry)
        if path is None:
            continue
        if entry["kind"] == "audit_context" and entry["origin"] == coordinator.PACKAGE_MANIFEST:
            # 2.6.8: the snapshot holds the runner's masked projection of the live manifest, which
            # is the live file itself for every deposit without code-shaped file names.
            check_live_package_manifest(root, entry, path)
            continue
        if coordinator.sha256_file(path) != entry["sha256"] or path.stat().st_size != entry["bytes"]:
            raise coordinator.CoordinatorError(f"live coordinator input became stale: {entry['origin']}")
        coordinator.validate_provider_visible_text(
            path, f"live coordinator input {entry['origin']}", coordinator.deposit_exemptions(root)
        )

    if {entry["origin"] for entry in by_kind.get("phase1_report", [])} != set(
        coordinator.PHASE_ONE_REPORTS.values()
    ):
        raise coordinator.CoordinatorError("snapshot does not contain the exact Phase-1 report quorum")
    if {entry["origin"] for entry in by_kind.get("audit_context", [])} != set(
        coordinator.REQUIRED_AUDIT_CONTEXT
    ):
        raise coordinator.CoordinatorError("snapshot does not contain the exact audit context")
    required_references = set(coordinator.REQUIRED_REFERENCES)
    reference_origins = {entry["origin"] for entry in by_kind.get("reference", [])}
    if not required_references.issubset(reference_origins):
        raise coordinator.CoordinatorError("snapshot lacks a required coordinator reference")
    if len(by_kind.get("phase1_bindings", [])) != 1:
        raise coordinator.CoordinatorError("snapshot requires exactly one Phase-1 binding record")
    if len(by_kind.get("safe_evidence_manifest", [])) != 1:
        raise coordinator.CoordinatorError("snapshot requires exactly one safe-evidence manifest")
    if not by_kind.get("draft_registry"):
        raise coordinator.CoordinatorError("snapshot has no draft registry")

    rows = coordinator.read_ledger(root)
    policy = record_invocation.load_policy_snapshot(root)
    record_invocation.validate_policy_binding(root)
    bindings, report_hashes = coordinator.phase_one_bindings(root, rows, policy)
    expected_binding = coordinator.pretty_bytes({"schema_version": "1.0.0", "bindings": bindings})
    binding_entry = by_kind["phase1_bindings"][0]
    binding_snapshot = attempt / "source" / binding_entry["snapshot"]
    if binding_snapshot.read_bytes() != expected_binding:
        raise coordinator.CoordinatorError("Phase-1 invocation bindings no longer match the ledger")

    draft_paths = [root / entry["origin"] for entry in by_kind["draft_registry"]]
    safe_manifest = root / by_kind["safe_evidence_manifest"][0]["origin"]
    safe_paths = coordinator.load_safe_evidence(root, safe_manifest)
    observed_safe = {coordinator.audit_relative(root, path) for path in safe_paths[1:]}
    snapshot_safe = {entry["origin"] for entry in by_kind.get("safe_evidence", [])}
    if observed_safe != snapshot_safe:
        raise coordinator.CoordinatorError("safe-evidence snapshot disagrees with its live manifest")
    return draft_paths, safe_paths, rows, report_hashes


def corrected_record_only(
    root: pathlib.Path,
    attempt: pathlib.Path,
    invocation_id: str,
    attempt_number: int,
    input_hash: str,
) -> tuple[dict[str, Any], str, str, float, float]:
    path = ensure_regular(attempt / "invocation_record.json", "original coordinator invocation record")
    original_hash = coordinator.sha256_file(path)
    original = coordinator.strict_object(path)
    # The route the attempt was dispatched on is the audit snapshot's (2.7.0), never config/.
    route = (record_invocation.load_policy_snapshot(root).get("models") or {}).get("coordinator")
    if not isinstance(route, dict):
        raise coordinator.CoordinatorError("the audit policy snapshot names no coordinator route")
    expected_identity = {
        "invocation_id": invocation_id,
        "role": "coordinator",
        "role_class": "coordinator",
        "round": None,
        "attempt": attempt_number,
        "status": "completed",
        "provider": "anthropic",
        "provider_call": True,
        "requested_model": route["requested_model"],
        "model_family": "claude",
        "model_source": "observed",
        "reasoning_effort": route["reasoning_effort"],
        "input_artifact_sha256": input_hash,
        "error": None,
        "error_class": None,
        "legacy_source": None,
    }
    wrong = [key for key, value in expected_identity.items() if original.get(key) != value]
    if wrong:
        raise coordinator.CoordinatorError(
            "quarantined coordinator record has non-timing identity defects: " + ", ".join(wrong)
        )
    started = parse_timestamp(original.get("started_at"), "started_at")
    ended = parse_timestamp(original.get("ended_at"), "ended_at")
    corrected_elapsed = coordinator.wall_elapsed_seconds(started, ended)
    original_elapsed = original.get("elapsed_seconds")
    if not isinstance(original_elapsed, (int, float)) or isinstance(original_elapsed, bool):
        raise coordinator.CoordinatorError("quarantined elapsed_seconds is not numeric")
    if abs(float(original_elapsed) - corrected_elapsed) <= 2:
        raise coordinator.CoordinatorError(
            "coordinator record does not have the recoverable elapsed-time inconsistency"
        )
    try:
        record_invocation.validate_record(original, root)
    except ValueError as exc:
        validation_error = str(exc)
    else:
        raise coordinator.CoordinatorError("coordinator record is already valid; recovery is unnecessary")
    if validation_error != "timestamps and elapsed_seconds are inconsistent":
        raise coordinator.CoordinatorError(
            "original coordinator record has a non-recoverable validation error: " + validation_error
        )
    corrected = copy.deepcopy(original)
    corrected["elapsed_seconds"] = corrected_elapsed
    changed = [key for key in set(original) | set(corrected) if original.get(key) != corrected.get(key)]
    if changed != ["elapsed_seconds"]:
        raise coordinator.CoordinatorError("recovery attempted to change more than elapsed_seconds")
    try:
        record_invocation.validate_record(corrected, root)
    except ValueError as exc:
        raise coordinator.CoordinatorError(
            f"elapsed-only corrected coordinator record is still invalid: {exc}"
        ) from exc
    return corrected, original_hash, validation_error, float(original_elapsed), corrected_elapsed


def validate_provider_and_bundle(
    root: pathlib.Path,
    attempt: pathlib.Path,
    audit_id: str,
    input_hash: str,
    corrected: dict[str, Any],
    draft_paths: list[pathlib.Path],
    safe_paths: list[pathlib.Path],
    ledger_rows: list[dict[str, Any]],
    report_hashes: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, Any]]:
    prompt_path = ensure_regular(attempt / "prompt.txt", "coordinator prompt")
    # corrected_record_only has bound requested_model and reasoning_effort to the snapshot's coordinator
    # route; the prompt names both (2.7.3: the effort, so a max audit's prompt is the 2.7.2 bytes).
    requested_model = corrected["requested_model"]
    expected_prompt = coordinator.build_prompt(
        audit_id, corrected["invocation_id"], input_hash, requested_model, corrected["reasoning_effort"],
    )
    if prompt_path.read_text(encoding="utf-8") != expected_prompt:
        raise coordinator.CoordinatorError("coordinator prompt is not the content-bound canonical prompt")
    if coordinator.sha256_file(prompt_path) != corrected["prompt_sha256"]:
        raise coordinator.CoordinatorError("coordinator prompt hash disagrees with the invocation record")

    settings_path = ensure_regular(attempt / "claude_settings.json", "coordinator sandbox settings")
    settings = coordinator.strict_object(settings_path)
    expected_settings = coordinator.sandbox_settings(
        root, attempt, attempt / "source", attempt / "output.partial"
    )
    if settings != expected_settings:
        raise coordinator.CoordinatorError("coordinator sandbox settings are not canonical")

    stdout_path = ensure_regular(attempt / "claude_stdout.json", "coordinator provider envelope")
    stderr_path = ensure_regular(attempt / "claude_stderr.txt", "coordinator provider stderr")
    if stderr_path.read_text(encoding="utf-8").strip():
        raise coordinator.CoordinatorError("coordinator provider stderr is non-empty")
    result = coordinator.parse_provider_result(stdout_path.read_text(encoding="utf-8"))
    if (
        result.get("type") != "result"
        or result.get("subtype") != "success"
        or result.get("is_error") is not False
        or result.get("terminal_reason") != "completed"
        or result.get("stop_reason") != "end_turn"
    ):
        raise coordinator.CoordinatorError("coordinator provider envelope is not an exact success")
    primary, all_models, primary_count = coordinator.observed_models(result, requested_model)
    if (
        primary_count != 1 or primary is None
        or not claude_routes.serves(requested_model, primary)
    ):
        raise coordinator.CoordinatorError(
            f"expected exactly one served {claude_routes.short_name(requested_model)} primary; "
            f"observed {primary_count}: {all_models!r}"
        )
    if primary != corrected.get("observed_model") or all_models != corrected.get("observed_model_ids"):
        raise coordinator.CoordinatorError("served model IDs disagree with the coordinator record")
    usage, cache_usage = coordinator.extract_usage(result)
    if not coordinator.complete_usage(usage) or usage != corrected.get("usage"):
        raise coordinator.CoordinatorError("provider usage is incomplete or disagrees with the record")
    if cache_usage != corrected.get("cache_usage"):
        raise coordinator.CoordinatorError("provider cache usage disagrees with the record")
    if result.get("session_id") != corrected.get("session_id"):
        raise coordinator.CoordinatorError("provider session ID disagrees with the record")

    denials = result.get("permission_denials")
    if not isinstance(denials, list) or denials:
        raise coordinator.CoordinatorError("strict recovery requires zero provider permission denials")
    denial_path = ensure_regular(attempt / "permission_denials.json", "permission-denial receipt")
    denial_receipt = coordinator.strict_object(denial_path)
    expected_denials = {
        "count": 0,
        "tool_names": [],
        "raw_stdout_sha256": coordinator.sha256_file(stdout_path),
    }
    if denial_receipt != expected_denials:
        raise coordinator.CoordinatorError("permission-denial receipt disagrees with provider output")

    draft_rows, draft_ids = coordinator.load_draft_rows(draft_paths)
    known_pairs, known_paths, _canonical = coordinator.known_evidence(
        draft_rows, report_hashes, root, safe_paths
    )
    output = attempt / "output.partial"
    if output.is_symlink() or not output.is_dir():
        raise coordinator.CoordinatorError("coordinator partial output directory is unsafe")
    observed_output_files: set[str] = set()
    for path in output.rglob("*"):
        if path.is_symlink() or not path.is_file():
            raise coordinator.CoordinatorError("coordinator output directory has an unsafe entry")
        observed_output_files.add(path.relative_to(output).as_posix())
    if observed_output_files != set(coordinator.OUTPUT_TARGETS):
        raise coordinator.CoordinatorError(
            "coordinator output closure is incomplete or contains unexpected files"
        )
    artifacts, findings = coordinator.validate_outputs(
        output, draft_ids, known_pairs, known_paths, ledger_rows
    )

    bundle_path = ensure_regular(attempt / "coordinator_bundle.json", "coordinator bundle")
    bundle = coordinator.strict_object(bundle_path)
    if set(bundle) != {
        "schema_version", "audit_id", "invocation_id", "input_artifact_sha256",
        "prompt_sha256", "observed_model", "observed_model_ids",
        "provider_stdout_sha256", "outputs", "finding_ids",
    } or bundle.get("schema_version") != "1.0.0":
        raise coordinator.CoordinatorError("coordinator bundle has an invalid envelope")
    expected_bundle = {
        "schema_version": "1.0.0",
        "audit_id": audit_id,
        "invocation_id": corrected["invocation_id"],
        "input_artifact_sha256": input_hash,
        "prompt_sha256": corrected["prompt_sha256"],
        "observed_model": primary,
        "observed_model_ids": all_models,
        "provider_stdout_sha256": coordinator.sha256_file(stdout_path),
        "outputs": artifacts,
        "finding_ids": [row["id"] for row in findings],
    }
    if bundle != expected_bundle:
        raise coordinator.CoordinatorError("coordinator bundle disagrees with revalidated artifacts")
    if (
        coordinator.audit_relative(root, bundle_path) != corrected["output_artifact"]
        or coordinator.sha256_file(bundle_path) != corrected["output_sha256"]
    ):
        raise coordinator.CoordinatorError("coordinator bundle hash disagrees with the record")
    return artifacts, expected_bundle["finding_ids"], denial_receipt


def validate_failed_transaction(
    root: pathlib.Path, invocation_id: str, artifacts: dict[str, dict[str, Any]]
) -> tuple[pathlib.Path, str]:
    transaction = root / "coordinator/transactions" / invocation_id
    if transaction.is_symlink() or not transaction.is_dir():
        raise coordinator.CoordinatorError("the original failed promotion transaction is missing")
    expected_entries = {
        name: {
            "name": name,
            "target": artifact["canonical_path"],
            "sha256": artifact["sha256"],
            "existed": False,
            "backup": None,
        }
        for name, artifact in artifacts.items()
    }
    journal_path = ensure_regular(transaction / "journal.json", "failed promotion journal")
    journal = coordinator.strict_object(journal_path)
    if set(journal) != {"schema_version", "state", "invocation_id", "entries"}:
        raise coordinator.CoordinatorError("failed promotion journal has an invalid envelope")
    entries = journal.get("entries")
    if (
        journal.get("schema_version") != "1.0.0"
        or journal.get("state") != "backups_ready"
        or journal.get("invocation_id") != invocation_id
        or not isinstance(entries, list)
        or len(entries) != len(expected_entries)
    ):
        raise coordinator.CoordinatorError("failed promotion journal is not recoverable")
    observed_entries = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "name", "target", "sha256", "existed", "backup"
        } or entry.get("name") in observed_entries:
            raise coordinator.CoordinatorError("failed promotion journal entry is malformed")
        observed_entries[entry["name"]] = entry
    if observed_entries != expected_entries:
        raise coordinator.CoordinatorError("failed promotion journal disagrees with validated outputs")
    expected_tree = {"journal.json", "staging", "backups"}
    observed_tree = {path.relative_to(transaction).as_posix() for path in transaction.rglob("*")}
    if observed_tree != expected_tree:
        raise coordinator.CoordinatorError("failed promotion transaction contains unexpected state")
    for directory in (transaction / "staging", transaction / "backups"):
        if directory.is_symlink() or not directory.is_dir() or any(directory.iterdir()):
            raise coordinator.CoordinatorError("failed promotion rollback is incomplete")
    for artifact in artifacts.values():
        target = root / artifact["canonical_path"]
        if target.exists() or target.is_symlink():
            raise coordinator.CoordinatorError(
                f"canonical coordinator target already exists: {artifact['canonical_path']}"
            )
    return transaction, tree_digest(transaction)


def validate_status(
    attempt: pathlib.Path, audit_id: str, invocation_id: str, attempt_number: int, input_hash: str
) -> str:
    path = ensure_regular(attempt / "status.json", "quarantined coordinator status")
    digest = coordinator.sha256_file(path)
    status_value = coordinator.strict_object(path)
    if (
        status_value.get("schema_version") != "1.0.0"
        or status_value.get("state") != "running"
        or status_value.get("audit_id") != audit_id
        or status_value.get("invocation_id") != invocation_id
        or status_value.get("attempt") != attempt_number
        or status_value.get("input_artifact_sha256") != input_hash
        or not isinstance(status_value.get("provider_call_started"), bool)
    ):
        raise coordinator.CoordinatorError("coordinator status is not the expected quarantine state")
    return digest


def validate_recovery(root: pathlib.Path, attempt: pathlib.Path) -> RecoveryState:
    match = ATTEMPT_NAME.fullmatch(attempt.name)
    assert match is not None
    attempt_number = int(match.group(1))
    invocation_id = match.group(2)
    manifest, input_hash = load_input_manifest(attempt, coordinator.deposit_exemptions(root))
    if attempt.parent.name != input_hash:
        raise coordinator.CoordinatorError("attempt directory is not bound to its input hash")
    run = coordinator.strict_object(ensure_regular(root / "run.json", "run metadata"))
    audit_id = run.get("audit_id")
    if run.get("schema_version") != "2.0.0" or not isinstance(audit_id, str) or not audit_id:
        raise coordinator.CoordinatorError("run.json is not a live v2 audit")
    draft_paths, safe_paths, ledger_rows, report_hashes = validate_live_inputs(
        root, attempt, manifest
    )
    if any(row.get("invocation_id") == invocation_id for row in ledger_rows):
        raise coordinator.CoordinatorError("coordinator invocation is already present in the ledger")
    prior_coordinator = [
        row for row in ledger_rows
        if row.get("provider_call") is True
        and row.get("role") == "coordinator"
        and row.get("round") is None
    ]
    if attempt_number != len(prior_coordinator) + 1:
        raise coordinator.CoordinatorError("coordinator attempt is not contiguous with the ledger")
    other_attempts = [
        path for path in (root / "coordinator/invocations").glob("*/*")
        if path.resolve() != attempt.resolve()
    ]
    if other_attempts:
        raise coordinator.CoordinatorError("another coordinator attempt exists; recovery is ambiguous")

    corrected, original_hash, validation_error, old_elapsed, new_elapsed = corrected_record_only(
        root, attempt, invocation_id, attempt_number, input_hash
    )
    artifacts, finding_ids, denial_receipt = validate_provider_and_bundle(
        root, attempt, audit_id, input_hash, corrected,
        draft_paths, safe_paths, ledger_rows, report_hashes,
    )
    failed_transaction, transaction_hash = validate_failed_transaction(
        root, invocation_id, artifacts
    )
    prior_status_hash = validate_status(
        attempt, audit_id, invocation_id, attempt_number, input_hash
    )
    return RecoveryState(
        root=root,
        attempt=attempt,
        output=attempt / "output.partial",
        invocation_id=invocation_id,
        attempt_number=attempt_number,
        audit_id=audit_id,
        input_manifest=manifest,
        input_hash=input_hash,
        artifacts=artifacts,
        finding_ids=finding_ids,
        corrected_record=corrected,
        original_record_hash=original_hash,
        original_record_validation_error=validation_error,
        original_elapsed=old_elapsed,
        corrected_elapsed=new_elapsed,
        prior_status_hash=prior_status_hash,
        attempt_tree_hash=tree_digest(attempt),
        failed_transaction=failed_transaction,
        failed_transaction_tree_hash=transaction_hash,
        permission_denials=denial_receipt,
    )


def mode_snapshot(path: pathlib.Path) -> dict[pathlib.Path, int]:
    result: dict[pathlib.Path, int] = {}
    for item in [path, *path.rglob("*")]:
        if item.is_symlink():
            raise coordinator.CoordinatorError(f"attempt contains a symlink: {item}")
        result[item] = stat.S_IMODE(item.stat().st_mode)
    return result


def restore_modes(modes: dict[pathlib.Path, int]) -> None:
    directories = sorted(
        (path for path in modes if path.is_dir()), key=lambda value: len(value.parts)
    )
    files = [path for path in modes if not path.is_dir()]
    for path in [*directories, *files]:
        if path.exists() and not path.is_symlink():
            os.chmod(path, modes[path])


def ledger_line_bytes(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def build_final_documents(state: RecoveryState) -> tuple[bytes, str, bytes]:
    recovered_at = coordinator.iso(coordinator.utc_now())
    attempt_relative = coordinator.audit_relative(state.root, state.attempt)
    original_record_relative = coordinator.audit_relative(
        state.root, state.attempt / "invocation_record.json"
    )
    failed_transaction_relative = coordinator.audit_relative(
        state.root, state.failed_transaction
    )
    receipt_relative = f"coordinator/recoveries/{state.invocation_id}/receipt.json"
    corrected_hash = coordinator.sha256_bytes(
        coordinator.canonical_bytes(state.corrected_record)
    )
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "state": "completed",
        "mode": "no_provider",
        "provider_called_during_recovery": False,
        "recovered_at": recovered_at,
        "audit_id": state.audit_id,
        "invocation_id": state.invocation_id,
        "attempt": state.attempt_number,
        "attempt_directory": attempt_relative,
        "input_artifact_sha256": state.input_hash,
        "input_manifest_sha256": coordinator.sha256_file(
            state.attempt / "source/input_manifest.json"
        ),
        "original_status_sha256": state.prior_status_hash,
        "quarantined_attempt_tree_sha256": state.attempt_tree_hash,
        "original_invocation_record": original_record_relative,
        "original_invocation_record_sha256": state.original_record_hash,
        "original_validation_error": state.original_record_validation_error,
        "corrected_fields": ["elapsed_seconds"],
        "elapsed_seconds": {
            "original": state.original_elapsed,
            "corrected": state.corrected_elapsed,
            "basis": "ended_at minus started_at wall time",
        },
        "corrected_record_canonical_sha256": corrected_hash,
        "corrected_ledger_line_sha256": coordinator.sha256_bytes(
            ledger_line_bytes(state.corrected_record)
        ),
        "failed_transaction": failed_transaction_relative,
        "failed_transaction_tree_sha256": state.failed_transaction_tree_hash,
        "provider_envelope_sha256": coordinator.sha256_file(
            state.attempt / "claude_stdout.json"
        ),
        "coordinator_bundle_sha256": coordinator.sha256_file(
            state.attempt / "coordinator_bundle.json"
        ),
        "permission_denials": state.permission_denials,
        "finding_ids": state.finding_ids,
        "outputs": state.artifacts,
        "checks": {
            "immutable_snapshot": True,
            "live_inputs": True,
            "provider_success": True,
            "served_model": True,
            "all_model_usage": True,
            "bundle": True,
            "output_hashes": True,
            "schemas_and_joins": True,
            "privacy": True,
            "failed_transaction_preserved": True,
            "elapsed_only_correction": True,
        },
    }
    status_value = {
        "schema_version": "1.0.0",
        "state": "completed",
        "audit_id": state.audit_id,
        "invocation_id": state.invocation_id,
        "attempt": state.attempt_number,
        "input_artifact_sha256": state.input_hash,
        "provider_call_started": True,
        "observed_model": state.corrected_record["observed_model"],
        "observed_model_ids": state.corrected_record["observed_model_ids"],
        "usage": state.corrected_record["usage"],
        "permission_denials": state.permission_denials,
        "outputs": state.artifacts,
        "recovery": {
            "mode": "no_provider",
            "recovered_at": recovered_at,
            "corrected_fields": ["elapsed_seconds"],
            "original_invocation_record_sha256": state.original_record_hash,
            "failed_transaction_tree_sha256": state.failed_transaction_tree_hash,
            "receipt": receipt_relative,
        },
    }
    return coordinator.pretty_bytes(status_value), receipt_relative, coordinator.pretty_bytes(receipt)


def commit_recovery(state: RecoveryState) -> str:
    status_bytes, receipt_relative, receipt_bytes = build_final_documents(state)
    root = state.root
    recovery_tx = root / "coordinator/recovery-transactions" / state.invocation_id
    if recovery_tx.exists() or recovery_tx.is_symlink():
        raise coordinator.CoordinatorError("a coordinator recovery transaction already exists")
    recovery_tx.mkdir(parents=True, exist_ok=False)
    staging = recovery_tx / "staging"
    backups = recovery_tx / "backups"
    staging.mkdir(); backups.mkdir()
    receipt_target = root / receipt_relative
    if receipt_target.exists() or receipt_target.is_symlink():
        raise coordinator.CoordinatorError("a coordinator recovery receipt already exists")
    receipt_target.parent.mkdir(parents=True, exist_ok=False)

    entries: list[dict[str, Any]] = []
    payloads: list[tuple[str, pathlib.Path, bytes]] = []
    for name, artifact in state.artifacts.items():
        source_name = artifact.get("source_output", name)
        source = ensure_regular(state.output / source_name, f"validated coordinator output {name}")
        data = source.read_bytes()
        if coordinator.sha256_bytes(data) != artifact["sha256"] or len(data) != artifact["bytes"]:
            raise coordinator.CoordinatorError(f"validated coordinator output changed: {name}")
        payloads.append((name, root / artifact["canonical_path"], data))
    payloads.extend([
        ("status.json", state.attempt / "status.json", status_bytes),
        ("receipt.json", receipt_target, receipt_bytes),
    ])
    for name, target, data in payloads:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise coordinator.CoordinatorError(f"unsafe recovery target: {target}")
        staged = staging / name
        staged.write_bytes(data)
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        os.chmod(staged, 0o600)
        existed = target.is_file()
        backup = backups / name
        if existed:
            shutil.copyfile(target, backup)
        entries.append({
            "name": name,
            "target": coordinator.audit_relative(root, target),
            "sha256": coordinator.sha256_bytes(data),
            "existed": existed,
            "backup": name if existed else None,
        })
    recovery_journal = {
        "schema_version": RECEIPT_SCHEMA,
        "state": "backups_ready",
        "invocation_id": state.invocation_id,
        "source_failed_transaction": coordinator.audit_relative(root, state.failed_transaction),
        "entries": entries,
    }
    coordinator.atomic_write(recovery_tx / "journal.json", coordinator.pretty_bytes(recovery_journal))

    # Validation and promotion share the coordinator lock, but an unrelated process need not honor
    # that lock.  Repeat the complete validation after staging, then bind the last mutable attempt
    # byte/mode immediately before the first canonical replacement.
    rechecked = validate_recovery(root, state.attempt)
    if rechecked != state:
        raise coordinator.CoordinatorError("coordinator recovery inputs changed before commit")
    if tree_digest(state.attempt) != state.attempt_tree_hash:
        raise coordinator.CoordinatorError("quarantined coordinator attempt changed before commit")
    if tree_digest(state.failed_transaction) != state.failed_transaction_tree_hash:
        raise coordinator.CoordinatorError("failed promotion transaction changed before commit")

    modes = mode_snapshot(state.attempt)
    replaced: list[dict[str, Any]] = []
    committed = False
    try:
        for entry in entries:
            target = root / entry["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / entry["name"], target)
            replaced.append(entry)
        coordinator.freeze_attempt(state.attempt)
        os.chmod(receipt_target, 0o444)
        os.chmod(receipt_target.parent, 0o555)
        record_invocation.validate_record(state.corrected_record, root)
        record_invocation.append(root, state.corrected_record)
        committed = True
    except Exception:
        if coordinator.invocation_present(root, state.invocation_id):
            committed = True
        if not committed:
            os.chmod(receipt_target.parent, 0o755)
            restore_modes(modes)
            for entry in reversed(replaced):
                target = root / entry["target"]
                if entry["existed"]:
                    os.replace(backups / entry["name"], target)
                elif target.exists() and not target.is_symlink():
                    target.unlink()
            if receipt_target.parent.exists() and not any(receipt_target.parent.iterdir()):
                receipt_target.parent.rmdir()
            raise
    finally:
        if committed:
            shutil.rmtree(recovery_tx, ignore_errors=True)
    if not committed:
        raise coordinator.CoordinatorError("coordinator recovery did not commit")
    if coordinator.sha256_file(state.attempt / "invocation_record.json") != state.original_record_hash:
        raise coordinator.CoordinatorError("original invalid invocation record changed during recovery")
    if tree_digest(state.failed_transaction) != state.failed_transaction_tree_hash:
        raise coordinator.CoordinatorError("failed promotion transaction changed during recovery")
    return receipt_relative


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument(
        "--attempt", required=True, type=pathlib.Path,
        help="audit-relative or absolute quarantined coordinator attempt directory",
    )
    args = parser.parse_args(argv)
    root = args.audit_dir.resolve()
    try:
        if not root.is_dir():
            raise coordinator.CoordinatorError(f"audit directory does not exist: {root}")
        attempt = strict_path(root, args.attempt)
        lock_path = root / "coordinator/promotion.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = validate_recovery(root, attempt)
            receipt = commit_recovery(state)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        print(json.dumps({
            "status": "completed",
            "mode": "no_provider",
            "provider_called_during_recovery": False,
            "invocation_id": state.invocation_id,
            "corrected_fields": ["elapsed_seconds"],
            "elapsed_seconds": {
                "original": state.original_elapsed,
                "corrected": state.corrected_elapsed,
            },
            "receipt": receipt,
        }, sort_keys=True))
        return 0
    except (coordinator.CoordinatorError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
