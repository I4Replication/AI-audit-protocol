#!/usr/bin/env python3
"""Repair Phase-1 Claude accounting to include every served model.

Claude CLI's top-level ``usage`` normally repeats the primary Sonnet counts and
does not include auxiliary model calls. ``modelUsage`` partitions usage by served
model, and its per-model costs sum to ``total_cost_usd``. This migration therefore
sums token categories across ``modelUsage`` and treats top-level usage only as a
non-additive consistency check. It is dry-run by default and produces immutable
backups plus a content-hashed receipt when applied.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import claude_model_usage
import record_invocation


PHASE_ONE_ROLES = {
    "auditor_reproduction",
    "auditor_data_integrity",
    "auditor_text_vs_data",
    "auditor_code_methods",
    "auditor_privacy_archive",
}
BACKUP_NAME = "invocations.pre_all_model_usage_repair.jsonl"
RECEIPT_NAME = "phase1_all_model_usage_repair_receipt.json"
RECORD_BACKUP_NAME = "invocation_record.pre_all_model_usage_repair.json"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    return sha256_bytes(path.read_bytes())


def atomic_bytes(path: pathlib.Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def strict_object(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def ledger_rows(value: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(value.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"ledger line {number} is not an object")
        rows.append(row)
    ids = [row.get("invocation_id") for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("ledger invocation IDs are not unique")
    return rows


def canonical_usage_hash(usage: dict[str, Any]) -> str:
    return sha256_bytes(
        json.dumps(usage, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )


def pretty_record_bytes(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def build_plan(root: pathlib.Path, original_bytes: bytes) -> tuple[bytes, dict[str, Any], dict[pathlib.Path, dict[str, Any]]]:
    rows = ledger_rows(original_bytes)
    record_paths: dict[str, pathlib.Path] = {}
    for path in root.glob("work/a*_*/invocations/attempt_*/invocation_record.json"):
        record = strict_object(path)
        invocation_id = record.get("invocation_id")
        if not isinstance(invocation_id, str):
            raise ValueError(f"attempt record lacks invocation_id: {path}")
        if invocation_id in record_paths:
            raise ValueError(f"duplicate attempt-record invocation_id: {invocation_id}")
        record_paths[invocation_id] = path

    updates: dict[pathlib.Path, dict[str, Any]] = {}
    repairs: list[dict[str, Any]] = []
    changed_invocations = 0
    delta_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "total_tokens": 0,
    }
    for row in rows:
        if not (
            row.get("provider_call") is True
            and row.get("provider") == "anthropic"
            and row.get("role") in PHASE_ONE_ROLES
        ):
            continue
        invocation_id = row.get("invocation_id")
        path = record_paths.get(str(invocation_id))
        if path is None:
            raise ValueError(f"no attempt record for provider invocation {invocation_id}")
        if strict_object(path) != row:
            raise ValueError(f"attempt record disagrees with ledger before repair: {path}")
        raw_path = path.with_name("claude_stdout.json")
        if not raw_path.is_file():
            raise ValueError(f"raw Claude result is missing: {raw_path}")
        raw = strict_object(raw_path)
        primary = row.get("observed_model")
        usage, evidence = claude_model_usage.aggregate(raw, primary)
        if row.get("observed_model_ids") != evidence["served_model_ids"]:
            raise ValueError(f"served-model IDs disagree with raw modelUsage: {path}")

        before = row.get("usage")
        if not isinstance(before, dict):
            raise ValueError(f"usage is not an object: {path}")
        delta: dict[str, int] = {}
        for field in delta_totals:
            old = before.get(field)
            new = usage.get(field)
            if not isinstance(old, int) or isinstance(old, bool):
                raise ValueError(f"pre-repair {field} is not numeric: {path}")
            if not isinstance(new, int) or isinstance(new, bool):
                raise ValueError(f"post-repair {field} is not numeric: {path}")
            delta[field] = new - old
            if delta[field] < 0:
                raise ValueError(f"all-model repair would reduce {field}: {path}")
            delta_totals[field] += delta[field]
        changed = before != usage
        if changed:
            row["usage"] = usage
            updates[path] = row
            changed_invocations += 1
        after_record_bytes = pretty_record_bytes(row)
        repairs.append({
            "invocation_id": invocation_id,
            "role": row.get("role"),
            "attempt": row.get("attempt"),
            "status": row.get("status"),
            "attempt_record": path.relative_to(root).as_posix(),
            "attempt_record_backup": (
                path.with_name(RECORD_BACKUP_NAME).relative_to(root).as_posix()
                if changed else None
            ),
            "before_attempt_record_sha256": sha256_file(path),
            "after_attempt_record_sha256": sha256_bytes(after_record_bytes),
            "raw_result": raw_path.relative_to(root).as_posix(),
            "raw_result_sha256": sha256_file(raw_path),
            "output_artifact": row.get("output_artifact"),
            "output_sha256": row.get("output_sha256"),
            "before_usage_sha256": canonical_usage_hash(before),
            "after_usage_sha256": canonical_usage_hash(usage),
            "changed": changed,
            "token_delta": delta,
            "evidence": evidence,
        })

    for row in rows:
        record_invocation.validate_record(row, root)
    record_invocation.validate_attempt_sequences(rows)
    migrated_bytes = b"".join(
        (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode() for row in rows
    )
    incomplete = sorted(
        path.relative_to(root).as_posix()
        for path in root.glob("work/a*_*/invocations/attempt_*")
        if path.is_dir() and (path / "prompt.txt").is_file()
        and not (path / "invocation_record.json").is_file()
    )
    receipt = {
        "repair": "phase1_all_served_model_usage_v1",
        "applied": False,
        "accounting_basis": claude_model_usage.ACCOUNTING_BASIS,
        "ledger": "invocations.jsonl",
        "before_sha256": sha256_bytes(original_bytes),
        "after_sha256": sha256_bytes(migrated_bytes),
        "before_records": len(rows),
        "after_records": len(rows),
        "provider_invocations_checked": len(repairs),
        "provider_invocations_changed": changed_invocations,
        "token_delta": delta_totals,
        "incomplete_invocation_directories": incomplete,
        "repairs": repairs,
    }
    return migrated_bytes, receipt, updates


def first_record_bytes(value: bytes, count: int) -> bytes:
    selected: list[bytes] = []
    for line in value.splitlines(keepends=True):
        if not line.strip():
            continue
        selected.append(line if line.endswith(b"\n") else line + b"\n")
        if len(selected) == count:
            break
    if len(selected) != count:
        raise ValueError(f"ledger has fewer than the receipt's {count} records")
    return b"".join(selected)


def verify_applied(root: pathlib.Path) -> dict[str, Any]:
    receipt_path = root / "work" / RECEIPT_NAME
    receipt = strict_object(receipt_path)
    if receipt.get("repair") != "phase1_all_served_model_usage_v1" or receipt.get("applied") is not True:
        raise ValueError("all-model repair receipt is absent or not applied")
    backup = root / BACKUP_NAME
    if sha256_file(backup) != receipt.get("before_sha256"):
        raise ValueError("ledger backup hash does not match repair receipt")
    current_bytes = (root / "invocations.jsonl").read_bytes()
    repaired_prefix = first_record_bytes(current_bytes, int(receipt["after_records"]))
    if sha256_bytes(repaired_prefix) != receipt.get("after_sha256"):
        raise ValueError("repaired ledger-prefix hash does not match repair receipt")
    rows = ledger_rows(current_bytes)
    by_id = {str(row["invocation_id"]): row for row in rows}
    for row in rows:
        record_invocation.validate_record(row, root)
    record_invocation.validate_attempt_sequences(rows)

    for item in receipt.get("repairs", []):
        if not isinstance(item, dict):
            raise ValueError("repair receipt contains a non-object item")
        invocation_id = str(item.get("invocation_id"))
        path = root / str(item.get("attempt_record"))
        raw_path = root / str(item.get("raw_result"))
        if sha256_file(path) != item.get("after_attempt_record_sha256"):
            raise ValueError(f"post-repair attempt-record hash mismatch: {path}")
        if sha256_file(raw_path) != item.get("raw_result_sha256"):
            raise ValueError(f"raw-result hash mismatch: {raw_path}")
        if item.get("changed"):
            backup_path = root / str(item.get("attempt_record_backup"))
            if sha256_file(backup_path) != item.get("before_attempt_record_sha256"):
                raise ValueError(f"attempt-record backup hash mismatch: {backup_path}")
        record = strict_object(path)
        if record != by_id.get(invocation_id):
            raise ValueError(f"attempt record disagrees with ledger: {path}")
        if canonical_usage_hash(record["usage"]) != item.get("after_usage_sha256"):
            raise ValueError(f"usage hash mismatch: {path}")
        raw = strict_object(raw_path)
        recomputed, evidence = claude_model_usage.aggregate(raw, str(record["observed_model"]))
        if recomputed != record["usage"] or evidence != item.get("evidence"):
            raise ValueError(f"raw all-model accounting no longer reproduces: {path}")
        output = item.get("output_artifact")
        if output is not None and sha256_file(root / str(output)) != item.get("output_sha256"):
            raise ValueError(f"canonical output hash mismatch for {invocation_id}")
    return {
        "verified": True,
        "receipt": receipt_path.relative_to(root).as_posix(),
        "before_sha256": receipt["before_sha256"],
        "after_sha256": receipt["after_sha256"],
        "repaired_records": receipt["provider_invocations_changed"],
        "current_records": len(rows),
        "token_delta": receipt["token_delta"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_dir", type=pathlib.Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = args.audit_dir.resolve()
    ledger = root / "invocations.jsonl"
    if not ledger.is_file():
        raise SystemExit(f"ledger is missing: {ledger}")

    if args.verify:
        try:
            verified = verify_applied(root)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise SystemExit(f"repair verification failed: {exc}") from exc
        print(json.dumps(verified, indent=2, sort_keys=True))
        return 0

    if not args.apply:
        try:
            _, receipt, _ = build_plan(root, ledger.read_bytes())
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"repair preflight failed: {exc}") from exc
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0

    backup = root / BACKUP_NAME
    receipt_path = root / "work" / RECEIPT_NAME
    if backup.exists() or receipt_path.exists():
        raise SystemExit("repair backup or receipt already exists; refusing a second repair")
    try:
        with ledger.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            original_bytes = handle.read().encode("utf-8")
            migrated_bytes, receipt, updates = build_plan(root, original_bytes)
            if receipt["incomplete_invocation_directories"]:
                raise ValueError(
                    "provider invocation is still in progress: "
                    + ", ".join(receipt["incomplete_invocation_directories"])
                )
            if not updates:
                raise ValueError("no all-model usage repairs are required")
            existing_record_backups = [
                path.with_name(RECORD_BACKUP_NAME)
                for path in updates
                if path.with_name(RECORD_BACKUP_NAME).exists()
            ]
            if existing_record_backups:
                raise ValueError(
                    "attempt-record repair backup already exists: "
                    + ", ".join(str(path) for path in existing_record_backups)
                )
            atomic_bytes(backup, original_bytes)
            for path, row in updates.items():
                backup_path = path.with_name(RECORD_BACKUP_NAME)
                shutil.copyfile(path, backup_path)
                atomic_bytes(path, pretty_record_bytes(row))
            handle.seek(0)
            handle.write(migrated_bytes.decode("utf-8"))
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
            receipt["applied"] = True
            atomic_bytes(
                receipt_path,
                (json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(),
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"repair failed: {exc}") from exc
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
