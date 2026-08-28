#!/usr/bin/env python3
"""Validate and append one canonical model-attempt record to an audit ledger."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import sys
import tempfile
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
SCHEMA = SKILL / "schemas/v2/invocation-record.schema.json"
POLICY = SKILL / "config/pipeline-policy-v2.json"
POLICY_SNAPSHOT = "pipeline-policy.snapshot.json"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SKILL / "schemas/tools"))
import validate_json_schema
import validate_v2


def file_hash(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def policy_hash(policy: dict[str, Any]) -> str:
    data = json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def load_policy_snapshot(root: pathlib.Path) -> dict[str, Any]:
    path = root / POLICY_SNAPSHOT
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"immutable audit-scoped policy snapshot is missing: {path}")
    try:
        envelope = validate_json_schema.strict_loads(path.read_text(encoding="utf-8"), source=str(path))
        policy = envelope["policy"]
        if envelope.get("snapshot_version") != "1" or envelope.get("policy_sha256") != policy_hash(policy):
            raise ValueError("policy snapshot hash is invalid")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid audit-scoped policy snapshot: {path}: {exc}") from exc
    return policy


def initialize_policy_snapshot(root: pathlib.Path) -> dict[str, Any]:
    """Create the init-time snapshot once; ordinary validation is load-only."""
    path = root / POLICY_SNAPSHOT
    if path.exists() or path.is_symlink():
        return load_policy_snapshot(root)
    policy = validate_json_schema.strict_loads(POLICY.read_text(encoding="utf-8"), source=str(POLICY))
    envelope = {"snapshot_version": "1", "policy_sha256": policy_hash(policy), "policy": policy}
    data = (json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_policy_snapshot(root)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    return load_policy_snapshot(root)


def parse_time(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be an observed timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not an ISO date-time") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def configured_for(record: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any] | None:
    role = record["role"]
    if role == "coordinator":
        return policy["models"]["coordinator"]
    phase_one = policy["models"]["phase_1_auditors"]
    if role in phase_one:
        return phase_one[role]
    # Targeted adjudication deliberately keeps max effort while the routine Terra path uses
    # high.  The runtime emits targeted_adjudicator telemetry and a critic_synth result role;
    # accept either identity here so an adapter cannot accidentally route adjudication through
    # the cheaper ordinary-critic policy.
    if record["role_class"] == "adjudicator" or role in {"targeted_adjudicator", "critic_synth"}:
        return policy["models"]["targeted_adjudicator"]
    if record["role_class"] in {"critic", "synthesizer"} or role == "model_probe":
        return policy["models"]["critics"]
    return None


def validate_policy_binding(root: pathlib.Path) -> None:
    """Require live v2 audits to bind the exact snapshot bytes captured at init."""
    run_path = root / "run.json"
    if not run_path.is_file():
        return
    run = validate_json_schema.strict_loads(run_path.read_text(encoding="utf-8"), source=str(run_path))
    if not isinstance(run, dict) or run.get("schema_version") != "2.0.0" or run.get("normalization"):
        return
    toolkit = run.get("toolkit") or {}
    expected = toolkit.get("pipeline_policy_snapshot_sha256")
    actual = file_hash(root / POLICY_SNAPSHOT)
    if expected != actual:
        raise ValueError("run.json does not bind the exact init-time pipeline policy snapshot")


def derived_model_family(record: dict[str, Any]) -> str | None:
    provider = str(record.get("provider") or "").lower()
    observed = str(record.get("observed_model") or "").lower()
    if provider == "anthropic" or "claude" in observed:
        return "claude"
    if provider == "openai" or any(token in observed for token in ("gpt", "codex", "o1", "o3", "o4")):
        return "gpt"
    return None


def validate_attempt_sequences(rows: list[dict[str, Any]]) -> None:
    groups: dict[tuple[Any, Any], list[int]] = {}
    for row in rows:
        if row.get("provider_call"):
            groups.setdefault((row.get("role"), row.get("round")), []).append(row.get("attempt"))
    for (role, round_number), attempts in groups.items():
        if len(attempts) > 3 or attempts != list(range(1, len(attempts) + 1)):
            raise ValueError(
                f"provider attempts for {role} round {round_number} must be ordered, contiguous 1..N, N<=3; "
                f"observed {attempts}"
            )


def validate_record(record: dict[str, Any], root: pathlib.Path) -> None:
    errors = validate_json_schema.errors_for_value(record, SCHEMA)
    if errors:
        raise ValueError("JSON Schema errors:\n  " + "\n  ".join(errors[:30]))
    try:
        validate_v2.validate_invocation(record)
    except validate_v2.ValidationError as exc:
        raise ValueError(str(exc)) from exc
    policy = load_policy_snapshot(root)
    validate_policy_binding(root)
    configured = configured_for(record, policy) if record.get("status") != "legacy_record" else None
    if configured:
        expected = {
            "requested_model": configured["requested_model"],
            "provider": configured["provider"],
            "reasoning_effort": configured["reasoning_effort"],
        }
        wrong = [f"{key}={record.get(key)!r}, expected {value!r}"
                 for key, value in expected.items() if record.get(key) != value]
        if wrong:
            raise ValueError("recorded request violates pipeline policy: " + "; ".join(wrong))
    if record.get("status") == "started":
        raise ValueError("append-only invocation rows must be terminal; 'started' cannot be updated in place")
    if record.get("provider_call"):
        for field in ("input_artifact_sha256", "prompt_sha256"):
            if not isinstance(record.get(field), str):
                raise ValueError(f"provider call requires {field}")
        family = derived_model_family(record)
        if family is not None and record.get("model_family") != family:
            raise ValueError(
                f"model_family={record.get('model_family')!r} disagrees with family {family!r} "
                "derived from observed model/provider"
            )
    if record.get("status") != "legacy_record":
        started = parse_time(record.get("started_at"), "started_at")
        ended = parse_time(record.get("ended_at"), "ended_at")
        observed_elapsed = (ended - started).total_seconds()
        elapsed = record.get("elapsed_seconds")
        if observed_elapsed < 0 or not isinstance(elapsed, (int, float)) or abs(observed_elapsed-float(elapsed)) > 2:
            raise ValueError("timestamps and elapsed_seconds are inconsistent")
    if record.get("status") == "completed":
        if record.get("error") is not None or record.get("error_class") is not None:
            raise ValueError("completed invocation cannot carry an error")
    elif record.get("status") in {"failed", "cancelled", "timed_out"}:
        if not isinstance(record.get("error"), str) or not record["error"].strip() or record.get("error_class") is None:
            raise ValueError("failed/cancelled/timed-out invocation requires error and error_class")
    if record.get("provider_call") is False and record.get("cache_usage") == "none":
        raise ValueError("non-provider record must declare cache usage")
    artifact = record.get("output_artifact")
    expected_hash = record.get("output_sha256")
    if record.get("status") == "completed":
        output = (root / artifact).resolve()
        try:
            output.relative_to(root)
        except ValueError as exc:
            raise ValueError("output_artifact escapes the audit directory") from exc
        if not output.is_file():
            raise ValueError(f"completed invocation output is missing: {artifact}")
        actual = file_hash(output)
        if actual != expected_hash:
            raise ValueError(f"completed invocation output hash mismatch: {actual} != {expected_hash}")


def append(root: pathlib.Path, record: dict[str, Any]) -> None:
    ledger = root / "invocations.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        existing = []
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = validate_json_schema.strict_loads(
                    line, source=f"invocations.jsonl line {line_number}"
                )
                validate_record(value, root)
            except ValueError as exc:
                raise ValueError(f"existing invocation ledger line {line_number} is invalid: {exc}") from exc
            existing.append(value)
        validate_attempt_sequences(existing)
        if record["invocation_id"] in {row["invocation_id"] for row in existing}:
            raise ValueError(f"duplicate invocation_id: {record['invocation_id']}")
        if record.get("provider_call"):
            same = [row for row in existing if row.get("provider_call") and
                    (row.get("role"), row.get("round")) == (record.get("role"), record.get("round"))]
            if len(same) >= 3:
                raise ValueError(f"three-attempt budget exhausted for {record['role']} round {record['round']}")
            attempts = [row.get("attempt") for row in same]
            if record.get("attempt") in attempts:
                raise ValueError(f"duplicate attempt {record['attempt']} for {record['role']} round {record['round']}")
            expected_attempt = len(same) + 1
            if record.get("attempt") != expected_attempt:
                raise ValueError(f"attempt must be contiguous across resumes: expected {expected_attempt}")
        validate_attempt_sequences(existing + [record])
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="i4r-invocation-") as directory:
        root = pathlib.Path(directory).resolve()
        (root / "work").mkdir()
        output = root / "work/auditor.md"
        output.write_text("complete\n")
        record = {
            "invocation_id": "test-auditor-1", "role": "auditor_reproduction",
            "role_class": "auditor", "round": 0, "attempt": 1, "status": "completed",
            "provider": "anthropic", "provider_call": True, "cache_usage": "none",
            "session_id": "session-test", "requested_model": "sonnet-5",
            "observed_model": "claude-sonnet-5-20260801", "model_family": "claude",
            "model_source": "observed", "reasoning_effort": "max",
            "started_at": "2026-08-27T10:00:00Z", "ended_at": "2026-08-27T10:01:00Z",
            "elapsed_seconds": 60, "input_artifact_sha256": "a" * 64,
            "prompt_sha256": "b" * 64, "output_artifact": "work/auditor.md",
            "output_sha256": file_hash(output),
            "usage": {"obtainable": True, "input_tokens": 10, "output_tokens": 5,
                      "cached_input_tokens": 0, "reasoning_tokens": 2, "total_tokens": 17,
                      "source": "api_response"},
            "error": None, "error_class": None, "legacy_source": None,
        }
        try:
            validate_record(record, root)
        except ValueError as exc:
            if "snapshot is missing" not in str(exc):
                raise
        else:
            raise AssertionError("ordinary validation silently backfilled a missing policy snapshot")
        initialize_policy_snapshot(root)
        validate_record(record, root); append(root, record)
        try:
            append(root, record)
        except ValueError:
            pass
        else:
            raise AssertionError("duplicate invocation ID was accepted")
        bad = dict(record, invocation_id="bad", requested_model="opus-5",
                   observed_model="claude-opus-5-20260801")
        try:
            validate_record(bad, root)
        except ValueError:
            pass
        else:
            raise AssertionError("Opus was accepted for a Sonnet-only baseline role")
        target_output = root / "work/targeted.json"
        target_output.write_text("{}\n")
        target = dict(
            record,
            invocation_id="test-targeted-1",
            role="targeted_adjudicator",
            role_class="adjudicator",
            round=1,
            provider="openai",
            requested_model="gpt-5.6-terra",
            observed_model="gpt-5.6-terra-2026-08-01",
            model_family="gpt",
            reasoning_effort="max",
            output_artifact="work/targeted.json",
            output_sha256=file_hash(target_output),
        )
        validate_record(target, root)
        wrong_target_effort = dict(target, invocation_id="bad-target-effort",
                                   reasoning_effort="high")
        try:
            validate_record(wrong_target_effort, root)
        except ValueError:
            pass
        else:
            raise AssertionError("targeted adjudication accepted ordinary critic effort")
        critic = dict(
            target,
            invocation_id="test-critic-1",
            role="critic_stats",
            role_class="critic",
            reasoning_effort="high",
        )
        validate_record(critic, root)
        wrong_critic_effort = dict(critic, invocation_id="bad-critic-effort",
                                   reasoning_effort="max")
        try:
            validate_record(wrong_critic_effort, root)
        except ValueError:
            pass
        else:
            raise AssertionError("ordinary critic accepted targeted-adjudicator effort")
        bad_family = dict(record, invocation_id="bad-family", model_family="gpt")
        try:
            validate_record(bad_family, root)
        except ValueError:
            pass
        else:
            raise AssertionError("typed model family was trusted over provider/served identity")
        try:
            validate_attempt_sequences([dict(record, attempt=2)])
        except ValueError:
            pass
        else:
            raise AssertionError("non-contiguous existing attempt ledger was accepted")
        snapshot = root / POLICY_SNAPSHOT
        run = {"schema_version": "2.0.0", "normalization": None,
               "toolkit": {"pipeline_policy_snapshot_sha256": file_hash(snapshot)}}
        (root / "run.json").write_text(json.dumps(run) + "\n", encoding="utf-8")
        validate_record(record, root)
        envelope = validate_json_schema.strict_loads(snapshot.read_text(), source=str(snapshot))
        snapshot.write_text(json.dumps(envelope, separators=(",", ":")) + "\n", encoding="utf-8")
        try:
            validate_record(record, root)
        except ValueError:
            pass
        else:
            raise AssertionError("self-consistent policy snapshot rewrite escaped the init-time run binding")
    print("record_invocation self-test: ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_dir", nargs="?", type=pathlib.Path)
    parser.add_argument("record", nargs="?", type=pathlib.Path,
                        help="JSON object file; use '-' to read stdin")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--init-policy", type=pathlib.Path,
                        help="create/validate the immutable audit-scoped policy snapshot and exit")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.init_policy is not None:
        root = args.init_policy.resolve(); root.mkdir(parents=True, exist_ok=True)
        try: policy = initialize_policy_snapshot(root)
        except (OSError, ValueError) as exc:
            print(f"FATAL: {exc}", file=sys.stderr); return 1
        print(f"policy snapshot: {root / POLICY_SNAPSHOT} ({policy_hash(policy)})")
        return 0
    if args.audit_dir is None or args.record is None:
        parser.error("audit_dir and record are required")
    root = args.audit_dir.resolve()
    if not root.is_dir():
        parser.error(f"audit directory does not exist: {root}")
    try:
        raw = sys.stdin.read() if str(args.record) == "-" else args.record.read_text(encoding="utf-8")
        record = validate_json_schema.strict_loads(raw, source=str(args.record))
        if not isinstance(record, dict):
            raise ValueError("record must be a JSON object")
        validate_record(record, root)
        append(root, record)
    except (OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    print(f"recorded invocation {record['invocation_id']} in {root / 'invocations.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
