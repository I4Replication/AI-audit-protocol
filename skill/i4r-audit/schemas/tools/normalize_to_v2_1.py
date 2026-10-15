#!/usr/bin/env python3
"""Deterministically normalize metadata v1 or v2 run records to metadata 2.1.

No requested model is relabelled as provider-observed. Unknown timing, cost,
turns, served identity, and token partitions remain explicit nulls.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import sys
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1] / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

from normalize_v1_to_v2 import (  # noqa: E402
    NormalizationError,
    canonical_sha256,
    normalize_run_v1,
)


SCHEMA_VERSION = "2.1.0"


def _hash_event(event: dict[str, Any]) -> str:
    material = {key: value for key, value in event.items() if key != "event_hash"}
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_invocation(record: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(record)
    requested = result.get("requested_model")
    legacy_observed = result.get("observed_model")
    model_source = result.get("model_source")
    genuinely_observed = model_source == "observed" and isinstance(legacy_observed, str)
    result["served_model"] = legacy_observed if genuinely_observed else None
    if not genuinely_observed:
        # The pre-2.1 field could contain the configured route. Retaining that
        # as observed would manufacture provider identity evidence.
        result["observed_model"] = None
        result.pop("observed_model_ids", None)
    result["identity_assurance"] = (
        "observed" if genuinely_observed else
        "requested_only" if isinstance(requested, str) and requested else "unavailable"
    )
    result["model_identity_evidence"] = None
    result.setdefault("attempt_kind", "role_inference" if result.get("provider_call") else "cache_replay")
    if result.get("status") == "legacy_record":
        result["provider_request_status"] = "unknown"
        result["inference_start_status"] = "unknown"
    else:
        result["provider_request_status"] = (
            "accepted" if result.get("provider_call") else "not_requested"
        )
        result["inference_start_status"] = (
            "started" if result.get("provider_call") else "not_started"
        )
    if result["inference_start_status"] == "not_started":
        result["attempt"] = None
    result.setdefault("source_invocation_id", None)
    result.setdefault("turn_count", None)
    result.setdefault("provider_cost", None)
    result.setdefault("provider_duration_seconds", result.get("elapsed_seconds"))
    result.setdefault("usage_by_model", [])
    result.setdefault("unattributed_usage", copy.deepcopy(result.get("usage")))
    return result


def _round_state(row: dict[str, Any]) -> str:
    if row.get("integrated_at"):
        return "integrated"
    if row.get("ended_at"):
        return "quorum_complete"
    if row.get("started_at"):
        return "running"
    return "prepared"


def normalize_run_v2_to_v2_1(run: dict[str, Any]) -> dict[str, Any]:
    version = str(run.get("schema_version", ""))
    if not version.startswith("2."):
        raise NormalizationError(f"expected v2 input, got schema_version={version!r}")
    if version == SCHEMA_VERSION:
        return copy.deepcopy(run)
    source_sha = canonical_sha256(run)
    result = copy.deepcopy(run)
    result["schema_version"] = SCHEMA_VERSION
    result["invocations"] = [normalize_invocation(row) for row in run.get("invocations") or []]
    for row in result.get("rounds") or []:
        row.setdefault("state", _round_state(row))
        row.setdefault("prepared_at", row.get("started_at"))
        row.setdefault("plan", None)
        row.setdefault("authorization", None)
        row.setdefault("authorization_wait_started_at", None)
        row.setdefault("authorization_wait_ended_at", None)
    if result.get("rounds"):
        result["rounds_run"] = sum(
            row.get("state") == "integrated" for row in result.get("rounds") or []
        )
    result.setdefault("pause_reason", None)
    result.setdefault("post_delta_gates_passed", False)
    result.setdefault("durations", None)
    result.setdefault("diagnostic_artifacts", [])
    unavailable = [
        "invocations[*].turn_count", "invocations[*].provider_cost",
        "invocations[*].model_identity_evidence", "invocations[*].usage_by_model",
        "durations.authorization_wait_seconds", "durations.paused_seconds",
        "durations.retry_backoff_seconds", "durations.idle_seconds",
    ]
    prior_normalization = result.get("normalization")
    result["normalization"] = {
        "source_schema_version": version,
        "source_sha256": source_sha,
        "adapter": "normalize_to_v2_1.py",
        "fields_unavailable": sorted(set(
            unavailable + list((prior_normalization or {}).get("fields_unavailable") or [])
        )),
    }
    normalized_invocations = copy.deepcopy(result["invocations"])
    base = copy.deepcopy(result)
    base["invocations"] = []
    base["events"] = []
    base["journal_watermark"] = {"journal_seq": 0, "journal_hash": None}
    event = {
        "schema_version": SCHEMA_VERSION,
        "seq": 1,
        "at": result.get("started_at") or "1970-01-01T00:00:00Z",
        "type": "init",
        "payload": {"initial_run": base},
        "pid": None,
        "source": "normalize_to_v2_1.py",
        "previous_hash": None,
    }
    event["event_hash"] = _hash_event(event)
    events = [event]
    projected_invocations = []
    previous_hash = event["event_hash"]

    def emit(event_type: str, payload: dict[str, Any], at: str) -> dict[str, Any]:
        nonlocal previous_hash
        emitted = {
            "schema_version": SCHEMA_VERSION,
            "seq": len(events) + 1,
            "at": at,
            "type": event_type,
            "payload": payload,
            "pid": None,
            "source": "normalize_to_v2_1.py",
            "previous_hash": previous_hash,
        }
        emitted["event_hash"] = _hash_event(emitted)
        events.append(emitted)
        previous_hash = emitted["event_hash"]
        return emitted

    for row in normalized_invocations:
        invocation_id = row["invocation_id"]
        started = row.get("started_at") or result.get("started_at") or "1970-01-01T00:00:00Z"
        ended = row.get("ended_at") or started
        reservation = {
            key: copy.deepcopy(row.get(key))
            for key in (
                "invocation_id", "role", "role_class", "round", "attempt_kind",
                "provider", "requested_model", "reasoning_effort",
                "input_artifact_sha256", "prompt_sha256",
            )
        }
        reservation.update({
            "attempt": None,
            "provider_request_status": "not_requested",
            "inference_start_status": "not_started",
        })
        emit("invocation_reserved", reservation, started)
        if row.get("provider_request_status") != "not_requested":
            emit("provider_request", {
                "invocation_id": invocation_id,
                "status": row.get("provider_request_status"),
            }, started)
        if row.get("inference_start_status") == "started":
            emit("inference_start", {
                "invocation_id": invocation_id,
                "attempt": row.get("attempt"),
            }, started)
        terminal = emit(
            "invocation_terminal",
            {"invocation_id": invocation_id, "record": copy.deepcopy(row)},
            ended,
        )
        projected = copy.deepcopy(row)
        projected["journal_event"] = {"seq": terminal["seq"], "hash": terminal["event_hash"]}
        projected_invocations.append(projected)
    result["invocations"] = projected_invocations
    result["events"] = events
    result["journal_watermark"] = {
        "journal_seq": events[-1]["seq"], "journal_hash": events[-1]["event_hash"],
    }
    return result


def normalize_run_v1_to_v2_1(run: dict[str, Any]) -> dict[str, Any]:
    intermediate = normalize_run_v1(run)
    result = normalize_run_v2_to_v2_1(intermediate)
    result["normalization"]["source_schema_version"] = str(run.get("schema_version"))
    result["normalization"]["source_sha256"] = canonical_sha256(run)
    return result


def normalize_run(run: dict[str, Any]) -> dict[str, Any]:
    version = str(run.get("schema_version", ""))
    if version.startswith("1."):
        return normalize_run_v1_to_v2_1(run)
    if version.startswith("2."):
        return normalize_run_v2_to_v2_1(run)
    raise NormalizationError(f"unsupported source schema_version={version!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=pathlib.Path)
    parser.add_argument("-o", "--output", default="-")
    args = parser.parse_args(argv)
    try:
        run = json.loads(args.input.read_text(encoding="utf-8"))
        result = normalize_run(run)
        payload = json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if args.output == "-":
            sys.stdout.write(payload)
        else:
            pathlib.Path(args.output).write_text(payload, encoding="utf-8")
    except (OSError, json.JSONDecodeError, NormalizationError) as exc:
        print(f"normalization failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
