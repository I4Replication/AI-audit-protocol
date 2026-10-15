#!/usr/bin/env python3
"""Recover a successful critique model probe that failed during telemetry publication.

This is deliberately narrower than a general retry mechanism.  It accepts only the known
post-provider failure shape produced by ``run_critique.sh``: an immutable snapshot, an exact
``{"status":"ok"}`` response, one completed Codex turn, a failed round status saying telemetry
could not be recorded, and a round-wide probe budget already at attempt 2.  No provider is called.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from types import SimpleNamespace
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
import test_run_critique_runtime as runtime  # type: ignore


EXPECTED_PROMPT = (
    "I4R_RUNTIME_PROBE\n"
    "Return exactly the JSON object required by the supplied schema. Do not use tools.\n"
)


def load_object(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed


def regular_inside(root: pathlib.Path, path: pathlib.Path, label: str) -> pathlib.Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is not a regular non-symlink file: {path}")
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the audit directory: {path}") from exc
    return resolved


def atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def canonical_probe_schema() -> dict[str, Any]:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["status"],
        "properties": {"status": {"type": "string", "const": "ok"}},
    }
    runtime.validate_provider_schema(schema)
    return schema


def canonical_schema_bytes() -> bytes:
    return (
        json.dumps(canonical_probe_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()


def read_events(path: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed probe event line {line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"probe event line {line_number} is not an object")
        rows.append(row)
    expected_types = ["thread.started", "turn.started", "item.completed", "turn.completed"]
    if [row.get("type") for row in rows] != expected_types:
        raise ValueError("probe event sequence is not the exact completed no-tool canary sequence")
    thread_id = rows[0].get("thread_id")
    if not isinstance(thread_id, str) or not re.fullmatch(r"[0-9a-f-]{20,80}", thread_id):
        raise ValueError("probe thread ID is missing or malformed")
    item = rows[2].get("item")
    if not isinstance(item, dict) or item.get("type") != "agent_message":
        raise ValueError("probe completion lacks the expected agent message")
    if json.loads(item.get("text", "null")) != {"status": "ok"}:
        raise ValueError("probe agent message does not match the accepted response")
    if not isinstance(rows[3].get("usage"), dict):
        raise ValueError("completed probe event lacks usage")
    return rows


def read_ledger(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed invocation ledger line {line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"invocation ledger line {line_number} is not an object")
        rows.append(row)
    return rows


def cache_key(parts: list[str]) -> str:
    return hashlib.sha256(("i4r-model-probe-2\0" + "\0".join(parts)).encode()).hexdigest()


def recover(args: argparse.Namespace) -> dict[str, Any]:
    audit = pathlib.Path(args.audit_dir).resolve()
    if audit.is_symlink() or not audit.is_dir():
        raise ValueError("audit directory is missing or symlinked")
    invocation_id = args.invocation
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,239}", invocation_id):
        raise ValueError("malformed critique invocation ID")
    invocation = audit / "critique" / f"round_{args.round}" / "invocations" / invocation_id
    if invocation.is_symlink() or not invocation.is_dir():
        raise ValueError("critique invocation directory is missing or symlinked")

    status_path = regular_inside(audit, invocation / "ROUND_STATUS.json", "round status")
    status = load_object(status_path)
    required_status = {
        "invocation_id": invocation_id,
        "round": args.round,
        "outcome": "failed",
        "detail": "could not record model probe telemetry",
        "exit_code": 1,
        "provider_calls": 0,
    }
    for key, expected in required_status.items():
        if status.get(key) != expected:
            raise ValueError(f"round status {key}={status.get(key)!r}; expected {expected!r}")

    budget_path = regular_inside(
        audit,
        audit / "critique" / f"round_{args.round}" / "attempt_budget" / "model_probe.count",
        "probe attempt counter",
    )
    if budget_path.read_text(encoding="utf-8").strip() != "2":
        raise ValueError("recovery is restricted to the stranded successful probe attempt 2")

    snapshot_relative = status.get("snapshot")
    if not isinstance(snapshot_relative, str):
        raise ValueError("round status lacks its immutable snapshot")
    snapshot = (audit / snapshot_relative).resolve()
    try:
        snapshot.relative_to(audit)
    except ValueError as exc:
        raise ValueError("snapshot escapes the audit directory") from exc
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError("snapshot is missing or symlinked")
    manifest_path = regular_inside(audit, snapshot / "MANIFEST.json", "snapshot manifest")
    manifest = load_object(manifest_path)
    expected_manifest = {
        "input_sha256": status.get("input_sha256"),
        "report_sha256": status.get("report_sha256"),
        "round": args.round,
        "audit_id": status.get("audit_id"),
        "model": "gpt-5.6-terra",
        "effort": "high",
        "provider_schema_generation_version": runtime.PROVIDER_SCHEMA_GENERATION_VERSION,
    }
    for key, expected in expected_manifest.items():
        if manifest.get(key) != expected:
            raise ValueError(f"snapshot manifest {key} does not match failed invocation status")
    runtime.verify_read_only_inventory(snapshot, manifest)

    schema_path = regular_inside(audit, snapshot / "schemas" / "probe.schema.json", "probe schema")
    schema_bytes = schema_path.read_bytes()
    current_schema_bytes = canonical_schema_bytes()
    if schema_bytes != current_schema_bytes:
        raise ValueError("current probe schema is not byte-identical to the provider-accepted schema")
    schema = load_object(schema_path)

    prompt_path = regular_inside(audit, invocation / "probe" / "prompt.txt", "probe prompt")
    events_path = regular_inside(audit, invocation / "probe" / "events.jsonl", "probe events")
    response_path = regular_inside(audit, invocation / "probe" / "response.json", "probe response")
    stderr_path = regular_inside(audit, invocation / "probe" / "stderr.txt", "probe stderr")
    if prompt_path.read_text(encoding="utf-8") != EXPECTED_PROMPT:
        raise ValueError("probe prompt differs from the no-tool runtime canary")
    response = load_object(response_path)
    if response != {"status": "ok"}:
        raise ValueError("probe response is not the exact accepted canary response")
    errors = runtime.validate_json_schema.errors_for_value(response, schema_path)
    if errors:
        raise ValueError("probe response no longer validates: " + "; ".join(errors[:5]))
    event_rows = read_events(events_path)
    parsed_usage: dict[str, int] = {}
    usage_extras: dict[str, int] = {}
    runtime.walk_telemetry(event_rows[-1]["usage"], parsed_usage, usage_extras, [], [])
    usage = runtime.canonicalize_codex_usage(parsed_usage, usage_extras)
    if usage.get("total_tokens_kind") != "derived" or usage.get("total_tokens") != 13211:
        raise ValueError("probe usage does not reproduce the exact 13,211-token derived total")

    started = status.get("started_at")
    ended = status.get("ended_at")
    if not isinstance(started, str) or not isinstance(ended, str):
        raise ValueError("failed invocation lacks bounded driver timestamps")
    elapsed = int((parse_time(ended) - parse_time(started)).total_seconds())
    if elapsed < 0 or elapsed > 60:
        raise ValueError("failed invocation timestamps do not bound a short model probe")

    ledger_path = audit / "invocations.jsonl"
    local_log = invocation / "invocations.jsonl"
    call_id = f"{invocation_id}:model_probe:2:recovered"
    rows_before = read_ledger(ledger_path)
    existing = [row for row in rows_before if row.get("invocation_id") == call_id]
    provider_attempts = [
        row.get("attempt") for row in rows_before
        if row.get("round") == args.round and row.get("role") == "model_probe"
        and row.get("provider_call") is True
    ]
    if existing:
        if len(existing) != 1 or existing[0].get("status") != "completed" \
                or existing[0].get("attempt") != 2 \
                or existing[0].get("output_sha256") != sha256(response_path):
            raise ValueError("existing recovery invocation conflicts with the immutable probe")
        already_recorded = True
    else:
        if provider_attempts != [1]:
            raise ValueError(
                f"probe provider attempts must be exactly [1] before recovery; got {provider_attempts}"
            )
        already_recorded = False

    codex = pathlib.Path(args.codex).resolve()
    if codex.is_symlink() or not codex.is_file() or not os.access(codex, os.X_OK):
        raise ValueError("Codex executable is missing, symlinked, or not executable")
    version_process = subprocess.run(
        [str(codex), "--version"], check=True, capture_output=True, text=True, timeout=15,
    )
    codex_version = version_process.stdout.splitlines()[0].strip()
    if not re.fullmatch(r"codex-cli [A-Za-z0-9.+_-]+", codex_version):
        raise ValueError("Codex version output is missing or malformed")
    schema_sha = sha256(schema_path)
    original_helper_sha = manifest.get("runtime_helper_sha256")
    current_helper_sha = sha256(pathlib.Path(runtime.__file__).resolve())
    if not isinstance(original_helper_sha, str) or not re.fullmatch(
            r"[0-9a-f]{64}", original_helper_sha):
        raise ValueError("snapshot lacks its original helper hash")
    identity_parts = [
        "gpt-5.6-terra", "high", codex_version, schema_sha, current_helper_sha,
        runtime.PROVIDER_SCHEMA_GENERATION_VERSION,
    ]
    key = cache_key(identity_parts)
    cache_path = audit / "critique" / ".runtime" / "probes" / f"{key}.json"
    cache = {
        "cache_version": "i4r-model-probe-2",
        "status": "ok",
        "model": "gpt-5.6-terra",
        "effort": "high",
        "codex_version": codex_version,
        "probe_schema_sha256": schema_sha,
        "runtime_helper_sha256": current_helper_sha,
        "provider_schema_generation_version": runtime.PROVIDER_SCHEMA_GENERATION_VERSION,
        "checked_at": ended,
    }

    receipt = {
        "recovery_version": "i4r-model-probe-recovery-1",
        "audit_id": status.get("audit_id"),
        "round": args.round,
        "invocation_id": invocation_id,
        "provider_call_id": call_id,
        "attempt": 2,
        "provider_called_during_recovery": False,
        "already_recorded": already_recorded,
        "source_hashes": {
            "round_status_sha256": sha256(status_path),
            "snapshot_manifest_sha256": sha256(manifest_path),
            "prompt_sha256": sha256(prompt_path),
            "events_sha256": sha256(events_path),
            "response_sha256": sha256(response_path),
            "stderr_sha256": sha256(stderr_path),
            "probe_schema_sha256": schema_sha,
        },
        "schema_identity": {
            "original_runtime_helper_sha256": original_helper_sha,
            "current_runtime_helper_sha256": current_helper_sha,
            "provider_schema_generation_version": runtime.PROVIDER_SCHEMA_GENERATION_VERSION,
            "probe_schema_byte_identical": True,
        },
        "usage": usage,
        "cache": {
            "key": key,
            "path": cache_path.relative_to(audit).as_posix(),
            "codex_version": codex_version,
        },
    }
    if args.dry_run:
        receipt["dry_run"] = True
        return receipt

    if not already_recorded:
        runtime.command_telemetry(SimpleNamespace(
            local_log=str(local_log), global_log=str(ledger_path), events=str(events_path),
            response=str(response_path), stderr=str(stderr_path), prompt=str(prompt_path),
            invocation=invocation_id, call_id=call_id,
            input_sha=status["input_sha256"], round=args.round, role="model_probe", attempt=2,
            provider_call=1, outcome="complete", failure_class="", error_class="",
            cache_usage="probe_cache", exit_code=0, model="gpt-5.6-terra", effort="high",
            started=started, ended=ended, elapsed=elapsed, workspace=str(audit),
        ))
    rows_after = read_ledger(ledger_path)
    recovered = [row for row in rows_after if row.get("invocation_id") == call_id]
    if len(recovered) != 1 or recovered[0].get("usage", {}).get("total_tokens") != 13211:
        raise ValueError("canonical probe record was not recovered exactly once")

    if cache_path.exists() or cache_path.is_symlink():
        if cache_path.is_symlink() or load_object(cache_path) != cache:
            raise ValueError("probe cache target already exists with conflicting content")
    else:
        atomic_json(cache_path, cache)
    receipt["cache"]["sha256"] = sha256(cache_path)
    receipt["canonical_invocation_sha256"] = hashlib.sha256(
        (json.dumps(recovered[0], sort_keys=True, ensure_ascii=False) + "\n").encode()
    ).hexdigest()
    receipt["recovered_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    receipt_path = invocation / "probe" / "recovery.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        existing_receipt = load_object(receipt_path)
        stable_keys = ("provider_call_id", "source_hashes", "schema_identity", "usage", "cache")
        if any(existing_receipt.get(key_name) != receipt.get(key_name) for key_name in stable_keys):
            raise ValueError("existing probe recovery receipt conflicts with this recovery")
    else:
        atomic_json(receipt_path, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_dir")
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--invocation", required=True)
    parser.add_argument("--codex", default=None,
                        help="Codex CLI (default: CODEX, I4R_CODEX_BIN, else the newest working CLI; see codex_cli.py)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.codex is None:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import codex_cli
        args.codex, _how = codex_cli.resolve()
        if args.codex is None:
            parser.error("no working Codex CLI found; set CODEX or pass --codex (python3 scripts/codex_cli.py --explain)")
    try:
        result = recover(args)
    except Exception as exc:
        print(f"recovery refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
