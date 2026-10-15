#!/usr/bin/env python3
"""Compare existing baseline/optimized audit measurements without making provider calls.

Input records supply stable finding IDs, measured tokens/wall time, and telemetry coverage. Up to
eight real records are retained in input order; seeded cases may be appended from a separate local
fixture file. Missing measurements remain explicit and make release-gate status indeterminate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import statistics
import sys
import tempfile
from typing import Any


DOMAINS = {"statistics", "data", "privacy", "archive", "report_synchronization", "methods", "other"}
COVERAGE_KEYS = ("model", "reasoning_effort", "latency", "outcome", "tokens")
OPERATIONAL_KEYS = (
    "phase1_turn_counts",
    "claude_cache_read_tokens",
    "accounted_tokens",
    "failed_billable_tokens",
    "local_preflight_billable_tokens",
    "schema_admission_billable_tokens",
)
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MEASUREMENT_MANIFEST_SCHEMA = "i4r-benchmark-measurement-manifest-1"


class InputError(ValueError):
    pass


def strict_load(path: pathlib.Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in values:
            if key in out:
                raise InputError(f"{path}: duplicate JSON key {key!r}")
            out[key] = value
        return out

    def constant(value: str) -> None:
        raise InputError(f"{path}: non-finite JSON number {value}")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"{path}: top-level value must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def confined_file(root: pathlib.Path, relative: Any, label: str) -> pathlib.Path:
    if not isinstance(relative, str) or not relative:
        raise InputError(f"{label}.path must be a nonempty relative path")
    candidate_value = pathlib.PurePosixPath(relative)
    if candidate_value.is_absolute() or ".." in candidate_value.parts:
        raise InputError(f"{label}.path must remain inside its provenance root")
    root = root.resolve(strict=True)
    lexical = root / pathlib.Path(*candidate_value.parts)
    cursor = root
    for part in candidate_value.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise InputError(f"{label}.path may not traverse a symlink")
    candidate = lexical.resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise InputError(f"{label}.path escapes its provenance root") from exc
    if not candidate.is_file():
        raise InputError(f"{label}.path must be a regular, non-symlink file")
    return candidate


def require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise InputError(f"{label} must be a lowercase SHA-256 digest")
    return value


def verify_bound_file(root: pathlib.Path, record: Any, label: str) -> pathlib.Path:
    if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
        raise InputError(f"{label} must contain exactly path and sha256")
    path = confined_file(root, record["path"], label)
    expected = require_hash(record["sha256"], f"{label}.sha256")
    observed = file_sha256(path)
    if observed != expected:
        raise InputError(f"{label} SHA-256 is stale or forged")
    return path


def parse_journal(path: pathlib.Path, label: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    previous: str | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeError as exc:
        raise InputError(f"{label} is not UTF-8") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
            event_value: dict[str, Any] = {}
            for key, value in values:
                if key in event_value:
                    raise InputError(f"{label} line {number} has duplicate key {key!r}")
                event_value[key] = value
            return event_value

        def constant(value: str) -> None:
            raise InputError(f"{label} line {number} has non-finite number {value}")

        try:
            event = json.loads(line, object_pairs_hook=pairs, parse_constant=constant)
        except json.JSONDecodeError as exc:
            raise InputError(f"{label} line {number} is invalid JSON") from exc
        if not isinstance(event, dict):
            raise InputError(f"{label} line {number} is not an event object")
        if event.get("schema_version") != "2.1.0" or event.get("seq") != len(events) + 1:
            raise InputError(f"{label} has a noncanonical schema version or sequence")
        if event.get("previous_hash") != previous:
            raise InputError(f"{label} has a broken previous-hash chain")
        observed = require_hash(event.get("event_hash"), f"{label} event_hash")
        expected = canonical_sha256({key: value for key, value in event.items()
                                     if key != "event_hash"})
        if observed != expected:
            raise InputError(f"{label} has a forged event hash")
        events.append(event)
        previous = observed
    if not events:
        raise InputError(f"{label} is empty")
    return {"journal_seq": events[-1]["seq"], "journal_hash": previous}


def measurement_sha256(run: dict[str, Any]) -> str:
    value = {key: value for key, value in run.items() if key != "measurement_manifest"}
    return canonical_sha256(value)


def verify_measurement_manifest(
    run: dict[str, Any], reference: Any, *, input_root: pathlib.Path,
    case_id: str, variant: str, label: str,
) -> dict[str, Any]:
    manifest_path = verify_bound_file(input_root, reference, f"{label}.measurement_manifest")
    manifest, _ = strict_load(manifest_path)
    expected_keys = {
        "schema_version", "case_id", "variant", "measurement_sha256",
        "journal", "run_metrics", "artifacts",
    }
    if set(manifest) != expected_keys or manifest.get("schema_version") != MEASUREMENT_MANIFEST_SCHEMA:
        raise InputError(f"{label} measurement manifest has an unsupported contract")
    if manifest.get("case_id") != case_id or manifest.get("variant") != variant:
        raise InputError(f"{label} measurement manifest is bound to another case or variant")
    if require_hash(manifest.get("measurement_sha256"),
                    f"{label}.measurement_sha256") != measurement_sha256(run):
        raise InputError(f"{label} numeric measurement is stale or forged")

    manifest_root = manifest_path.parent
    journal_record = manifest.get("journal")
    if not isinstance(journal_record, dict) or set(journal_record) != {
        "path", "sha256", "journal_seq", "journal_hash",
    }:
        raise InputError(f"{label}.journal binding is malformed")
    journal_path = verify_bound_file(
        manifest_root, {key: journal_record[key] for key in ("path", "sha256")},
        f"{label}.journal",
    )
    if journal_path.name != "run.events.jsonl" or journal_path.parent != manifest_root:
        raise InputError(f"{label}.journal must point to canonical run.events.jsonl")
    actual_watermark = parse_journal(journal_path, f"{label}.journal")
    declared_watermark = {
        "journal_seq": journal_record.get("journal_seq"),
        "journal_hash": journal_record.get("journal_hash"),
    }
    if declared_watermark != actual_watermark:
        raise InputError(f"{label}.journal watermark is stale or forged")

    metrics_path = verify_bound_file(manifest_root, manifest.get("run_metrics"),
                                     f"{label}.run_metrics")
    if metrics_path.name != "run_metrics.json" or metrics_path.parent != manifest_root:
        raise InputError(f"{label}.run_metrics must point to canonical run_metrics.json")
    metrics, _ = strict_load(metrics_path)
    if metrics.get("schema_version") != "2.1.0":
        raise InputError(f"{label}.run_metrics is not a metadata-2.1 projection")
    if metrics.get("journal_watermark") != actual_watermark:
        raise InputError(f"{label}.run_metrics has a stale journal watermark")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise InputError(f"{label}.artifacts must bind at least one measured artifact")
    seen: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict) or set(artifact) != {"kind", "path", "sha256"}:
            raise InputError(f"{label}.artifacts[{index}] is malformed")
        if not isinstance(artifact["kind"], str) or not artifact["kind"]:
            raise InputError(f"{label}.artifacts[{index}].kind must be nonempty")
        artifact_path = verify_bound_file(
            manifest_root, {key: artifact[key] for key in ("path", "sha256")},
            f"{label}.artifacts[{index}]",
        )
        if artifact_path in {manifest_path, journal_path, metrics_path}:
            raise InputError(
                f"{label}.artifacts[{index}] must bind an output, not provenance metadata"
            )
        relative = artifact_path.relative_to(manifest_root.resolve()).as_posix()
        if relative in seen:
            raise InputError(f"{label}.artifacts contains duplicate paths")
        seen.add(relative)
    return {
        "manifest_sha256": file_sha256(manifest_path),
        "journal_watermark": actual_watermark,
    }


def unique_strings(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise InputError(f"{label} must be an array of nonempty strings")
    if len(value) != len(set(value)):
        raise InputError(f"{label} contains duplicates")
    if not allow_empty and not value:
        raise InputError(f"{label} must not be empty")
    return value


def nullable_nonnegative_integer(value: Any, label: str, *, positive: bool = False) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise InputError(f"{label} must be a {qualifier} integer or null")
    return value


def validate_operational_metrics(value: Any, label: str) -> dict[str, Any] | None:
    """Validate optional v1-compatible measurements used by the tightened release gates."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(OPERATIONAL_KEYS):
        raise InputError(
            f"{label} must contain exactly {', '.join(OPERATIONAL_KEYS)}"
        )
    turns = value["phase1_turn_counts"]
    if turns is not None:
        if (
            not isinstance(turns, list)
            or len(turns) != 5
            or any(isinstance(turn, bool) or not isinstance(turn, int) or turn <= 0 for turn in turns)
        ):
            raise InputError(f"{label}.phase1_turn_counts must be five positive integers or null")
    cached = nullable_nonnegative_integer(
        value["claude_cache_read_tokens"], f"{label}.claude_cache_read_tokens"
    )
    accounted = nullable_nonnegative_integer(
        value["accounted_tokens"], f"{label}.accounted_tokens", positive=True
    )
    failed = nullable_nonnegative_integer(
        value["failed_billable_tokens"], f"{label}.failed_billable_tokens"
    )
    preflight = nullable_nonnegative_integer(
        value["local_preflight_billable_tokens"],
        f"{label}.local_preflight_billable_tokens",
    )
    schema = nullable_nonnegative_integer(
        value["schema_admission_billable_tokens"],
        f"{label}.schema_admission_billable_tokens",
    )
    if accounted is not None:
        for field, observed in (
            ("claude_cache_read_tokens", cached),
            ("failed_billable_tokens", failed),
            ("local_preflight_billable_tokens", preflight),
            ("schema_admission_billable_tokens", schema),
        ):
            if observed is not None and observed > accounted:
                raise InputError(f"{label}.{field} cannot exceed accounted_tokens")
    return value


def validate_run(value: Any, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InputError(f"{label} must be an object or null")
    required = {
        "total_tokens", "wall_clock_seconds", "reported_finding_ids", "telemetry_coverage",
        "accepted_partial_or_stale_outputs", "settled_spot_check_failures",
    }
    missing = required - set(value)
    if missing:
        raise InputError(f"{label} is missing {', '.join(sorted(missing))}")
    tokens, wall = value["total_tokens"], value["wall_clock_seconds"]
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        raise InputError(f"{label}.total_tokens must be a positive integer")
    if isinstance(wall, bool) or not isinstance(wall, (int, float)) or wall <= 0:
        raise InputError(f"{label}.wall_clock_seconds must be positive")
    unique_strings(value["reported_finding_ids"], f"{label}.reported_finding_ids")
    coverage = value["telemetry_coverage"]
    if not isinstance(coverage, dict) or set(coverage) != set(COVERAGE_KEYS):
        raise InputError(f"{label}.telemetry_coverage must contain exactly {', '.join(COVERAGE_KEYS)}")
    for key in COVERAGE_KEYS:
        observed = coverage[key]
        if (isinstance(observed, bool) or not isinstance(observed, (int, float)) or
                observed < 0 or observed > 1):
            raise InputError(f"{label}.telemetry_coverage.{key} must be between 0 and 1")
    for key in ("accepted_partial_or_stale_outputs", "settled_spot_check_failures"):
        observed = value[key]
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
            raise InputError(f"{label}.{key} must be a nonnegative integer")
    validate_operational_metrics(value.get("operational_metrics"), f"{label}.operational_metrics")
    return value


def validate_record(value: Any, label: str, *, input_root: pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError(f"{label} must be an object")
    case_id = value.get("case_id")
    source = value.get("source")
    if not isinstance(case_id, str) or not case_id:
        raise InputError(f"{label}.case_id must be a nonempty string")
    if source not in {"real", "seeded"}:
        raise InputError(f"{label}.source must be real or seeded")
    status = value.get("measurement_status")
    if status is None:
        status = (
            "measured" if source == "real" and value.get("baseline") is not None
            and value.get("optimized") is not None else "unmeasured"
        )
    if status not in {"measured", "projected", "unmeasured"}:
        raise InputError(f"{label}.measurement_status must be measured, projected, or unmeasured")
    if status == "projected" and source != "seeded":
        raise InputError(f"{label}: projected records must use source=seeded")
    failure_domains = unique_strings(value.get("failure_domains"), f"{label}.failure_domains", allow_empty=False)
    unknown = set(failure_domains) - DOMAINS
    if unknown:
        raise InputError(f"{label}.failure_domains contains unsupported values: {sorted(unknown)}")
    truth = value.get("ground_truth")
    if not isinstance(truth, dict):
        raise InputError(f"{label}.ground_truth must be an object")
    expected_truth = {"substantive_finding_ids", "conclusion_changing_ids", "disclosure_critical_ids"}
    if set(truth) != expected_truth:
        raise InputError(f"{label}.ground_truth must contain exactly {', '.join(sorted(expected_truth))}")
    substantive = set(unique_strings(
        truth["substantive_finding_ids"], f"{label}.ground_truth.substantive_finding_ids",
        allow_empty=False,
    ))
    conclusion = set(unique_strings(
        truth["conclusion_changing_ids"], f"{label}.ground_truth.conclusion_changing_ids"
    ))
    disclosure = set(unique_strings(
        truth["disclosure_critical_ids"], f"{label}.ground_truth.disclosure_critical_ids"
    ))
    if not conclusion <= substantive or not disclosure <= substantive:
        raise InputError(f"{label}: critical finding IDs must be a subset of substantive finding IDs")
    baseline = validate_run(value.get("baseline"), f"{label}.baseline")
    optimized = validate_run(value.get("optimized"), f"{label}.optimized")
    provenance: dict[str, Any] = {}
    if status == "measured":
        if baseline is None or optimized is None:
            raise InputError(f"{label}: measured records require baseline and optimized runs")
        for variant, run in (("baseline", baseline), ("optimized", optimized)):
            reference = run.get("measurement_manifest")
            if reference is None:
                raise InputError(
                    f"{label}.{variant}: measured run lacks a hash-bound measurement_manifest"
                )
            provenance[variant] = verify_measurement_manifest(
                run, reference, input_root=input_root, case_id=case_id,
                variant=variant, label=f"{label}.{variant}",
            )
    if status == "projected" and optimized is not None:
        raise InputError(f"{label}: projected records must keep optimized=null")
    value["_measurement_status"] = status
    value["_measurement_provenance"] = provenance
    return value


def load_records(path: pathlib.Path) -> tuple[list[dict[str, Any]], str]:
    value, digest = strict_load(path)
    if value.get("schema_version") != "i4r-benchmark-input-1":
        raise InputError(f"{path}: unsupported schema_version")
    records = value.get("records")
    if not isinstance(records, list):
        raise InputError(f"{path}: records must be an array")
    checked = [validate_record(record, f"{path}:records[{index}]", input_root=path.parent)
               for index, record in enumerate(records)]
    ids = [record["case_id"] for record in checked]
    if len(ids) != len(set(ids)):
        raise InputError(f"{path}: duplicate case_id")
    return checked, digest


def ratio(found: int, denominator: int) -> float:
    return found / denominator if denominator else 1.0


def complete_operational_pair(record: dict[str, Any]) -> bool:
    """Return whether one measured pair can support every added operational gate."""
    baseline = record["baseline"].get("operational_metrics")
    optimized = record["optimized"].get("operational_metrics")
    if not isinstance(baseline, dict) or not isinstance(optimized, dict):
        return False
    if not isinstance(baseline.get("claude_cache_read_tokens"), int):
        return False
    if baseline["claude_cache_read_tokens"] <= 0:
        return False
    required_optimized = (
        "claude_cache_read_tokens", "accounted_tokens", "failed_billable_tokens",
        "local_preflight_billable_tokens", "schema_admission_billable_tokens",
    )
    if any(not isinstance(optimized.get(key), int) for key in required_optimized):
        return False
    turns = optimized.get("phase1_turn_counts")
    return isinstance(turns, list) and len(turns) == 5


def evaluate(records: list[dict[str, Any]], source_hashes: list[dict[str, str]], max_real: int) -> dict[str, Any]:
    retained: list[dict[str, Any]] = []
    real_seen = 0
    omitted_real: list[str] = []
    for record in records:
        if record["source"] == "real":
            real_seen += 1
            if real_seen > max_real:
                omitted_real.append(record["case_id"])
                continue
        retained.append(record)

    measured = [record for record in retained
                if record.get("_measurement_status") == "measured"]
    incomplete = [record["case_id"] for record in retained if record not in measured]
    projected = [record["case_id"] for record in retained
                 if record.get("_measurement_status") == "projected"]
    operational_measured = [record for record in measured if complete_operational_pair(record)]
    operational_incomplete = [
        record["case_id"] for record in measured if record not in operational_measured
    ]
    per_case: list[dict[str, Any]] = []
    token_reductions: list[float] = []
    wall_reductions: list[float] = []
    phase1_turn_counts: list[int] = []
    baseline_cache_read_tokens = 0
    optimized_cache_read_tokens = 0
    optimized_accounted_tokens = 0
    optimized_failed_billable_tokens = 0
    optimized_local_preflight_billable_tokens = 0
    optimized_schema_admission_billable_tokens = 0
    aggregate = {
        "truth": 0,
        "critical": 0,
        "baseline_true_positive": 0,
        "baseline_reported": 0,
        "optimized_true_positive": 0,
        "optimized_reported": 0,
    }
    optimized_critical_misses: list[str] = []
    coverage_min = {key: 1.0 for key in COVERAGE_KEYS}
    partial_or_stale = 0
    settled_failures = 0

    for record in measured:
        truth = set(record["ground_truth"]["substantive_finding_ids"])
        critical = set(record["ground_truth"]["conclusion_changing_ids"]) | set(
            record["ground_truth"]["disclosure_critical_ids"]
        )
        baseline, optimized = record["baseline"], record["optimized"]
        baseline_reported = set(baseline["reported_finding_ids"])
        optimized_reported = set(optimized["reported_finding_ids"])
        baseline_tp, optimized_tp = truth & baseline_reported, truth & optimized_reported
        token_reduction = (baseline["total_tokens"] - optimized["total_tokens"]) / baseline["total_tokens"]
        wall_reduction = (
            baseline["wall_clock_seconds"] - optimized["wall_clock_seconds"]
        ) / baseline["wall_clock_seconds"]
        operational: dict[str, Any] | None = None
        if complete_operational_pair(record):
            baseline_ops = baseline["operational_metrics"]
            optimized_ops = optimized["operational_metrics"]
            baseline_cached = baseline_ops["claude_cache_read_tokens"]
            optimized_cached = optimized_ops["claude_cache_read_tokens"]
            accounted = optimized_ops["accounted_tokens"]
            failed = optimized_ops["failed_billable_tokens"]
            phase1_turn_counts.extend(optimized_ops["phase1_turn_counts"])
            baseline_cache_read_tokens += baseline_cached
            optimized_cache_read_tokens += optimized_cached
            optimized_accounted_tokens += accounted
            optimized_failed_billable_tokens += failed
            optimized_local_preflight_billable_tokens += optimized_ops[
                "local_preflight_billable_tokens"
            ]
            optimized_schema_admission_billable_tokens += optimized_ops[
                "schema_admission_billable_tokens"
            ]
            operational = {
                "median_phase1_turn_count": statistics.median(optimized_ops["phase1_turn_counts"]),
                "claude_cache_read_reduction_fraction": (
                    baseline_cached - optimized_cached
                ) / baseline_cached,
                "failed_billable_token_fraction": failed / accounted,
                "local_preflight_billable_tokens": optimized_ops[
                    "local_preflight_billable_tokens"
                ],
                "schema_admission_billable_tokens": optimized_ops[
                    "schema_admission_billable_tokens"
                ],
            }
        token_reductions.append(token_reduction)
        wall_reductions.append(wall_reduction)
        misses = sorted(critical - optimized_reported)
        optimized_critical_misses.extend(f"{record['case_id']}:{finding_id}" for finding_id in misses)
        for key in COVERAGE_KEYS:
            coverage_min[key] = min(coverage_min[key], float(optimized["telemetry_coverage"][key]))
        partial_or_stale += optimized["accepted_partial_or_stale_outputs"]
        settled_failures += optimized["settled_spot_check_failures"]
        aggregate["truth"] += len(truth)
        aggregate["critical"] += len(critical)
        aggregate["baseline_true_positive"] += len(baseline_tp)
        aggregate["baseline_reported"] += len(baseline_reported)
        aggregate["optimized_true_positive"] += len(optimized_tp)
        aggregate["optimized_reported"] += len(optimized_reported)
        per_case.append({
            "case_id": record["case_id"],
            "source": record["source"],
            "measurement_status": record["_measurement_status"],
            "measurement_provenance": record["_measurement_provenance"],
            "failure_domains": record["failure_domains"],
            "token_reduction_fraction": token_reduction,
            "wall_clock_reduction_fraction": wall_reduction,
            "baseline_recall": ratio(len(baseline_tp), len(truth)),
            "optimized_recall": ratio(len(optimized_tp), len(truth)),
            "baseline_precision": ratio(len(baseline_tp), len(baseline_reported)),
            "optimized_precision": ratio(len(optimized_tp), len(optimized_reported)),
            "optimized_critical_misses": misses,
            "operational_metrics": operational,
        })

    baseline_recall = (
        ratio(aggregate["baseline_true_positive"], aggregate["truth"]) if measured else None
    )
    optimized_recall = (
        ratio(aggregate["optimized_true_positive"], aggregate["truth"]) if measured else None
    )
    baseline_precision = (
        ratio(aggregate["baseline_true_positive"], aggregate["baseline_reported"])
        if measured else None
    )
    optimized_precision = (
        ratio(aggregate["optimized_true_positive"], aggregate["optimized_reported"])
        if measured else None
    )
    operational_ready = bool(measured) and not operational_incomplete
    ready = bool(measured) and not incomplete and operational_ready
    median_token = statistics.median(token_reductions) if token_reductions else None
    median_wall = statistics.median(wall_reductions) if wall_reductions else None
    median_phase1_turns = statistics.median(phase1_turn_counts) if operational_ready else None
    cache_read_reduction = (
        (baseline_cache_read_tokens - optimized_cache_read_tokens) / baseline_cache_read_tokens
        if operational_ready else None
    )
    failed_billable_fraction = (
        optimized_failed_billable_tokens / optimized_accounted_tokens
        if operational_ready else None
    )
    checks = {
        "zero_missed_conclusion_or_disclosure_critical": not optimized_critical_misses,
        "substantive_recall_within_two_percentage_points": (
            optimized_recall is not None and baseline_recall is not None
            and optimized_recall >= baseline_recall - 0.02
        ),
        "precision_no_worse": (
            optimized_precision is not None and baseline_precision is not None
            and optimized_precision >= baseline_precision
        ),
        "zero_accepted_partial_or_stale_outputs": partial_or_stale == 0,
        "zero_failed_settled_spot_checks": settled_failures == 0,
        "telemetry_coverage_100_percent": all(value == 1.0 for value in coverage_min.values()),
        "median_token_reduction_at_least_30_percent": median_token is not None and median_token >= 0.30,
        "median_wall_clock_reduction_at_least_35_percent": median_wall is not None and median_wall >= 0.35,
        "median_phase1_turn_count_at_most_45": (
            median_phase1_turns is not None and median_phase1_turns <= 45
        ),
        "claude_cache_read_token_reduction_at_least_50_percent": (
            operational_ready
            and optimized_cache_read_tokens * 2 <= baseline_cache_read_tokens
        ),
        "failed_billable_token_fraction_at_most_10_percent": (
            operational_ready
            and optimized_failed_billable_tokens * 10 <= optimized_accounted_tokens
        ),
        "zero_preflight_and_schema_admission_billable_tokens": (
            operational_ready
            and optimized_local_preflight_billable_tokens == 0
            and optimized_schema_admission_billable_tokens == 0
        ),
    }
    gate_status = "pass" if ready and all(checks.values()) else ("fail" if ready else "indeterminate")
    return {
        "schema_version": "i4r-benchmark-result-1",
        "source_inputs": source_hashes,
        "selection": {
            "max_real_records": max_real,
            "retained_real_records": sum(r["source"] == "real" for r in retained),
            "retained_seeded_records": sum(r["source"] == "seeded" for r in retained),
            "omitted_real_case_ids": omitted_real,
            "measured_case_ids": [record["case_id"] for record in measured],
            "incomplete_case_ids": incomplete,
            "projected_case_ids": projected,
            "operationally_measured_case_ids": [
                record["case_id"] for record in operational_measured
            ],
            "operationally_incomplete_case_ids": operational_incomplete,
        },
        "performance": {
            "median_paired_token_reduction_fraction": median_token,
            "median_paired_wall_clock_reduction_fraction": median_wall,
            "median_phase1_turn_count": median_phase1_turns,
            "baseline_claude_cache_read_tokens": (
                baseline_cache_read_tokens if operational_ready else None
            ),
            "optimized_claude_cache_read_tokens": (
                optimized_cache_read_tokens if operational_ready else None
            ),
            "claude_cache_read_token_reduction_fraction": cache_read_reduction,
        },
        "noninferiority": {
            "substantive_ground_truth_count": aggregate["truth"],
            "critical_ground_truth_count": aggregate["critical"],
            "baseline_true_positive_count": aggregate["baseline_true_positive"],
            "baseline_reported_count": aggregate["baseline_reported"],
            "optimized_true_positive_count": aggregate["optimized_true_positive"],
            "optimized_reported_count": aggregate["optimized_reported"],
            "baseline_recall": baseline_recall,
            "optimized_recall": optimized_recall,
            "recall_difference_fraction": (
                optimized_recall - baseline_recall
                if optimized_recall is not None and baseline_recall is not None else None
            ),
            "baseline_precision": baseline_precision,
            "optimized_precision": optimized_precision,
            "precision_difference_fraction": (
                optimized_precision - baseline_precision
                if optimized_precision is not None and baseline_precision is not None else None
            ),
            "optimized_critical_misses": optimized_critical_misses,
        },
        "optimized_safety": {
            "minimum_telemetry_coverage": coverage_min if measured else None,
            "accepted_partial_or_stale_outputs": partial_or_stale if measured else None,
            "failed_settled_spot_checks": settled_failures if measured else None,
            "accounted_tokens": optimized_accounted_tokens if operational_ready else None,
            "failed_billable_tokens": (
                optimized_failed_billable_tokens if operational_ready else None
            ),
            "failed_billable_token_fraction": failed_billable_fraction,
            "local_preflight_billable_tokens": (
                optimized_local_preflight_billable_tokens if operational_ready else None
            ),
            "schema_admission_billable_tokens": (
                optimized_schema_admission_billable_tokens if operational_ready else None
            ),
        },
        "release_gates": {"status": gate_status, "ready": ready, "checks": checks},
        "per_case": per_case,
    }


def markdown(result: dict[str, Any]) -> str:
    selection = result["selection"]
    performance = result["performance"]
    noninferiority = result["noninferiority"]
    safety = result["optimized_safety"]
    gates = result["release_gates"]

    def percent(value: float | None) -> str:
        return "unmeasured" if value is None else f"{value:.1%}"

    lines = [
        "# Accuracy-safe optimization benchmark", "",
        f"Release-gate status: **{gates['status'].upper()}**.", "",
        (f"Measured {len(selection['measured_case_ids'])} case(s): "
         f"{selection['retained_real_records']} real and {selection['retained_seeded_records']} seeded retained."),
    ]
    if selection["incomplete_case_ids"]:
        lines += ["", "Unmeasured retained cases: " + ", ".join(
            f"`{case_id}`" for case_id in selection["incomplete_case_ids"]
        ) + "."]
    if selection["projected_case_ids"]:
        lines += ["", "Projected cases excluded from every measured statistic: " + ", ".join(
            f"`{case_id}`" for case_id in selection["projected_case_ids"]
        ) + "."]
    if selection["operationally_incomplete_case_ids"]:
        lines += ["", "Cases missing required operational gate measurements: " + ", ".join(
            f"`{case_id}`" for case_id in selection["operationally_incomplete_case_ids"]
        ) + "."]
    if selection["omitted_real_case_ids"]:
        lines += ["", "Real cases beyond the configured cap were omitted: " + ", ".join(
            f"`{case_id}`" for case_id in selection["omitted_real_case_ids"]
        ) + "."]
    lines += [
        "", "| Outcome | Result |", "|---|---:|",
        f"| Median paired token reduction | {percent(performance['median_paired_token_reduction_fraction'])} |",
        f"| Median paired wall-clock reduction | {percent(performance['median_paired_wall_clock_reduction_fraction'])} |",
        ("| Median Phase-1 turn count | "
         f"{performance['median_phase1_turn_count'] if performance['median_phase1_turn_count'] is not None else 'unmeasured'} |"),
        f"| Claude cache-read token reduction | {percent(performance['claude_cache_read_token_reduction_fraction'])} |",
        f"| Failed billable-token fraction | {percent(safety['failed_billable_token_fraction'])} |",
        ("| Local-preflight billable tokens | "
         f"{safety['local_preflight_billable_tokens'] if safety['local_preflight_billable_tokens'] is not None else 'unmeasured'} |"),
        ("| Schema-admission billable tokens | "
         f"{safety['schema_admission_billable_tokens'] if safety['schema_admission_billable_tokens'] is not None else 'unmeasured'} |"),
        f"| Baseline substantive recall | {percent(noninferiority['baseline_recall'])} |",
        f"| Optimized substantive recall | {percent(noninferiority['optimized_recall'])} |",
        f"| Baseline precision | {percent(noninferiority['baseline_precision'])} |",
        f"| Optimized precision | {percent(noninferiority['optimized_precision'])} |",
        "", "## Release gates", "", "| Gate | Pass |", "|---|---:|",
    ]
    for name, passed in gates["checks"].items():
        lines.append(f"| {name.replace('_', ' ')} | {'yes' if passed else 'no'} |")
    return "\n".join(lines) + "\n"


def atomic_write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=pathlib.Path, help="measured benchmark-input JSON")
    parser.add_argument("--seeded-fixtures", type=pathlib.Path,
                        help="append local seeded cases (null run measurements remain incomplete)")
    parser.add_argument("--max-real", type=int, default=8)
    parser.add_argument("--output-json", type=pathlib.Path)
    parser.add_argument("--output-markdown", type=pathlib.Path)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--require-gates", action="store_true")
    args = parser.parse_args()
    if args.max_real < 1 or args.max_real > 8:
        parser.error("--max-real must be between 1 and 8")
    try:
        records, input_hash = load_records(args.input.resolve())
        # Results may be shared; bind content without leaking the operator's absolute filesystem path.
        hashes = [{"label": "primary", "sha256": input_hash}]
        if args.seeded_fixtures:
            seeds, seed_hash = load_records(args.seeded_fixtures.resolve())
            if any(record["source"] != "seeded" for record in seeds):
                raise InputError("--seeded-fixtures may contain only source=seeded records")
            records += seeds
            hashes.append({"label": "seeded_fixtures", "sha256": seed_hash})
        ids = [record["case_id"] for record in records]
        if len(ids) != len(set(ids)):
            raise InputError("case_id values must remain unique after merging seeded fixtures")
        result = evaluate(records, hashes, args.max_real)
    except (OSError, InputError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    rendered = markdown(result)
    if args.output_json:
        atomic_write(args.output_json.resolve(), json.dumps(result, indent=2, sort_keys=True) + "\n")
    if args.output_markdown:
        atomic_write(args.output_markdown.resolve(), rendered)
    print(rendered, end="")
    if args.require_complete and (
        result["selection"]["incomplete_case_ids"]
        or result["selection"]["operationally_incomplete_case_ids"]
    ):
        return 1
    if args.require_gates and result["release_gates"]["status"] != "pass":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
