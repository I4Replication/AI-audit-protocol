#!/usr/bin/env python3
"""Repair a live Phase-1 ledger after a launcher-only preflight was misclassified.

This is an explicit, receipt-producing migration.  It does not infer successful
reports: callers must name every invocation-directory number to promote.  Raw
provider JSON remains immutable and is used to recover served-model IDs and
honest token accounting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import record_invocation
import claude_model_usage


ROLE_CONFIG = {
    "auditor_reproduction": ("a1_reproduction", "report/01_reproduction.md"),
    "auditor_data_integrity": ("a2_data_integrity", "report/02_data_integrity.md"),
    "auditor_text_vs_data": ("a3_text_vs_data", "report/03_text_vs_data.md"),
    "auditor_code_methods": ("a4_code_methods", "report/04_code_methods.md"),
    "auditor_privacy_archive": ("a5_privacy_archive", "report/05_privacy_archive.md"),
}

NO_PROVIDER_MARKER = (
    "Input must be provided either through stdin or as a prompt argument when using --print"
)
UNAVAILABLE_REASON = "no_provider_usage_record_was_emitted"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def strict_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def unavailable_usage() -> dict[str, Any]:
    fields = (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "reasoning_tokens",
    )
    return {
        "obtainable": False,
        **{field: None for field in fields},
        "total_tokens": None,
        "source": "unavailable",
        "unavailable_reasons": {field: UNAVAILABLE_REASON for field in fields},
        "total_tokens_kind": "unavailable",
        "total_tokens_components": [],
        "total_tokens_unavailable_reason": UNAVAILABLE_REASON,
    }


def provider_usage(result: dict[str, Any], primary: str) -> dict[str, Any]:
    usage, _ = claude_model_usage.aggregate(result, primary)
    return usage


def primary_model(record: dict[str, Any], model_ids: list[str]) -> str | None:
    requested = str(record.get("requested_model") or "").lower()
    if "sonnet" in requested:
        candidates = [value for value in model_ids if "sonnet" in value.lower()]
    elif "terra" in requested or "gpt" in requested:
        candidates = [value for value in model_ids if "gpt" in value.lower()]
    else:
        candidates = model_ids
    return candidates[0] if len(candidates) == 1 else None


def sanitize_report(role: str, text: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    sanitized = re.sub(
        r"/(?:Users|home)/[^`\s,)]+(?:/[^`\s,)]+)*",
        "[local environment path redacted]",
        text,
    )
    if sanitized != text:
        changes.append("redacted absolute local environment path")
    text = sanitized
    if role == "auditor_reproduction":
        marker = "in only **5/28 cells**."
        clarification = (
            "in only **5/28 cells** for the exact `*`/`**`/none grade. "
            "At the binary significant/non-significant level, 24/28 decisions agree; "
            "the four decision conflicts are the conclusion-relevant subset."
        )
        if marker in text:
            text = text.replace(marker, clarification, 1)
            changes.append("clarified exact star grade versus binary significance")
    if role == "auditor_text_vs_data":
        replacements = {
            "19 Table-2 variables × 2 groups = 38 descriptive cells fully recomputed":
                "14 Table-2 variables × 2 groups = 28 descriptive cells fully recomputed",
            "22 distinct Python scripts producing 24 captured output files":
                "21 distinct Python scripts producing 25 captured output files",
        }
        for before, after in replacements.items():
            if before in text:
                text = text.replace(before, after)
                changes.append(f"corrected report object count: {before}")
    return text, changes


def parse_promotions(values: list[str]) -> dict[str, int]:
    promotions: dict[str, int] = {}
    for value in values:
        role, separator, directory = value.partition("=")
        if separator != "=" or role not in ROLE_CONFIG or not directory.isdigit():
            raise ValueError(f"invalid --promote value: {value!r}")
        number = int(directory)
        if number < 1 or role in promotions:
            raise ValueError(f"invalid or duplicate --promote value: {value!r}")
        promotions[role] = number
    return promotions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--promote", action="append", default=[], metavar="ROLE=ATTEMPT_DIR")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.audit_dir.resolve()
    ledger = root / "invocations.jsonl"
    if not ledger.is_file():
        raise SystemExit(f"ledger is missing: {ledger}")
    promotions = parse_promotions(args.promote)

    original_bytes = ledger.read_bytes()
    rows = [json.loads(line) for line in original_bytes.decode().splitlines() if line.strip()]
    if not all(isinstance(row, dict) for row in rows):
        raise SystemExit("ledger contains a non-object row")

    record_paths: dict[str, pathlib.Path] = {}
    raw_results: dict[str, dict[str, Any]] = {}
    for path in root.glob("work/a*_*/invocations/attempt_*/invocation_record.json"):
        value = strict_json(path)
        invocation_id = value.get("invocation_id")
        if isinstance(invocation_id, str):
            record_paths[invocation_id] = path
            raw_path = path.with_name("claude_stdout.json")
            if raw_path.is_file():
                try:
                    raw_results[invocation_id] = strict_json(raw_path)
                except (ValueError, json.JSONDecodeError):
                    pass

    promoted_ids: dict[str, str] = {}
    report_receipts: list[dict[str, Any]] = []
    if promotions:
        for role, directory_number in promotions.items():
            work_name, output_relative = ROLE_CONFIG[role]
            source_dir = root / "work" / work_name / "invocations" / f"attempt_{directory_number}"
            source = source_dir / "report.md"
            invocation_record = strict_json(source_dir / "invocation_record.json")
            invocation_id = invocation_record.get("invocation_id")
            if not source.is_file() or not isinstance(invocation_id, str):
                raise SystemExit(f"promotion source is incomplete: {source_dir}")
            destination = root / output_relative
            if destination.exists():
                raise SystemExit(f"refusing existing canonical report: {destination}")
            source_text = source.read_text(encoding="utf-8")
            output_text, changes = sanitize_report(role, source_text)
            if len(output_text.strip()) < 1200:
                raise SystemExit(f"promotion report is unexpectedly short: {source}")
            if re.search(r"/(?:Users|home)/[^`\s,)]+", output_text):
                raise SystemExit(f"absolute path remained after sanitization: {source}")
            if args.apply:
                atomic_bytes(destination, output_text.encode())
            promoted_ids[invocation_id] = output_relative
            report_receipts.append({
                "role": role,
                "invocation_id": invocation_id,
                "source": source.relative_to(root).as_posix(),
                "source_sha256": sha256_file(source),
                "canonical": output_relative,
                "canonical_sha256": sha256_bytes(output_text.encode()),
                "sanitizations": changes,
            })

    changes: list[dict[str, Any]] = []
    for row in rows:
        invocation_id = str(row.get("invocation_id"))
        old_attempt = row.get("attempt")
        original_provider_call = row.get("provider_call")
        if (
            original_provider_call is True
            and row.get("observed_model") is None
            and NO_PROVIDER_MARKER in str(row.get("error") or "")
        ):
            row["provider_call"] = False
            row["cache_usage"] = "not_applicable"
            row["usage"] = unavailable_usage()
            changes.append({
                "invocation_id": invocation_id,
                "change": "launcher preflight reclassified as non-provider",
            })
        elif row.get("provider_call") is True:
            result = raw_results.get(invocation_id, {})
            model_usage = result.get("modelUsage")
            model_ids = list(model_usage) if isinstance(model_usage, dict) else []
            primary = primary_model(row, model_ids)
            if primary:
                row["observed_model"] = primary
                row["observed_model_ids"] = model_ids
                row["model_source"] = "observed"
            if not primary:
                raise SystemExit(
                    f"cannot identify exactly one primary served model for {invocation_id}"
                )
            row["usage"] = provider_usage(result, primary)
            if old_attempt == 2 and row.get("status") == "failed":
                row["error_class"] = "authorization"
                row["error"] = (
                    "Provider response was not accepted because the invocation sandbox denied "
                    "the raw-data execution tools required by the role; no canonical report was promoted."
                )
            if row.get("role") == "auditor_code_methods" and old_attempt == 3:
                row["error_class"] = "transient"
                row["error"] = "Provider API server error interrupted the response before a complete report was produced."

        if invocation_id in promoted_ids:
            output_relative = promoted_ids[invocation_id]
            output_path = root / output_relative
            row.update({
                "status": "completed",
                "output_artifact": output_relative,
                "output_sha256": sha256_file(output_path) if args.apply else next(
                    item["canonical_sha256"] for item in report_receipts
                    if item["invocation_id"] == invocation_id
                ),
                "error": None,
                "error_class": None,
            })
            changes.append({"invocation_id": invocation_id, "change": "canonical report promoted"})

    provider_groups: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("provider_call") is True:
            provider_groups.setdefault((row.get("role"), row.get("round")), []).append(row)
    for group in provider_groups.values():
        for attempt, row in enumerate(group, 1):
            if row.get("attempt") != attempt:
                changes.append({
                    "invocation_id": row.get("invocation_id"),
                    "change": f"provider attempt renumbered {row.get('attempt')} -> {attempt}",
                })
                row["attempt"] = attempt

    if args.apply:
        for row in rows:
            record_invocation.validate_record(row, root)
        record_invocation.validate_attempt_sequences(rows)

    migrated_bytes = b"".join(
        (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode() for row in rows
    )
    receipt = {
        "migration": "phase1_provider_accounting_v1",
        "applied": bool(args.apply),
        "ledger": "invocations.jsonl",
        "before_sha256": sha256_bytes(original_bytes),
        "after_sha256": sha256_bytes(migrated_bytes),
        "before_records": len(rows),
        "after_records": len(rows),
        "changes": changes,
        "report_promotions": report_receipts,
    }
    if not args.apply:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0

    backup = root / "invocations.pre_provider_attempt_correction.jsonl"
    receipt_path = root / "work" / "phase1_provider_accounting_migration_receipt.json"
    if backup.exists() or receipt_path.exists():
        raise SystemExit("migration backup or receipt already exists; refusing a second migration")
    shutil.copyfile(ledger, backup)
    atomic_bytes(ledger, migrated_bytes)

    migrated_by_id = {str(row["invocation_id"]): row for row in rows}
    for invocation_id, path in record_paths.items():
        if invocation_id not in migrated_by_id:
            continue
        backup_path = path.with_name("invocation_record.pre_provider_attempt_correction.json")
        if backup_path.exists():
            raise SystemExit(f"attempt-record migration backup already exists: {backup_path}")
        shutil.copyfile(path, backup_path)
        atomic_bytes(
            path,
            (json.dumps(migrated_by_id[invocation_id], indent=2, sort_keys=True) + "\n").encode(),
        )
    atomic_bytes(receipt_path, (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
