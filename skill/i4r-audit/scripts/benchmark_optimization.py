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
import statistics
import sys
import tempfile
from typing import Any


DOMAINS = {"statistics", "data", "privacy", "archive", "report_synchronization", "methods", "other"}
COVERAGE_KEYS = ("model", "reasoning_effort", "latency", "outcome", "tokens")


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


def unique_strings(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise InputError(f"{label} must be an array of nonempty strings")
    if len(value) != len(set(value)):
        raise InputError(f"{label} contains duplicates")
    if not allow_empty and not value:
        raise InputError(f"{label} must not be empty")
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
    return value


def validate_record(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError(f"{label} must be an object")
    case_id = value.get("case_id")
    source = value.get("source")
    if not isinstance(case_id, str) or not case_id:
        raise InputError(f"{label}.case_id must be a nonempty string")
    if source not in {"real", "seeded"}:
        raise InputError(f"{label}.source must be real or seeded")
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
    validate_run(value.get("baseline"), f"{label}.baseline")
    validate_run(value.get("optimized"), f"{label}.optimized")
    return value


def load_records(path: pathlib.Path) -> tuple[list[dict[str, Any]], str]:
    value, digest = strict_load(path)
    if value.get("schema_version") != "i4r-benchmark-input-1":
        raise InputError(f"{path}: unsupported schema_version")
    records = value.get("records")
    if not isinstance(records, list):
        raise InputError(f"{path}: records must be an array")
    checked = [validate_record(record, f"{path}:records[{index}]")
               for index, record in enumerate(records)]
    ids = [record["case_id"] for record in checked]
    if len(ids) != len(set(ids)):
        raise InputError(f"{path}: duplicate case_id")
    return checked, digest


def ratio(found: int, denominator: int) -> float:
    return found / denominator if denominator else 1.0


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
                if record.get("baseline") is not None and record.get("optimized") is not None]
    incomplete = [record["case_id"] for record in retained if record not in measured]
    per_case: list[dict[str, Any]] = []
    token_reductions: list[float] = []
    wall_reductions: list[float] = []
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
            "failure_domains": record["failure_domains"],
            "token_reduction_fraction": token_reduction,
            "wall_clock_reduction_fraction": wall_reduction,
            "baseline_recall": ratio(len(baseline_tp), len(truth)),
            "optimized_recall": ratio(len(optimized_tp), len(truth)),
            "baseline_precision": ratio(len(baseline_tp), len(baseline_reported)),
            "optimized_precision": ratio(len(optimized_tp), len(optimized_reported)),
            "optimized_critical_misses": misses,
        })

    baseline_recall = ratio(aggregate["baseline_true_positive"], aggregate["truth"])
    optimized_recall = ratio(aggregate["optimized_true_positive"], aggregate["truth"])
    baseline_precision = ratio(aggregate["baseline_true_positive"], aggregate["baseline_reported"])
    optimized_precision = ratio(aggregate["optimized_true_positive"], aggregate["optimized_reported"])
    ready = bool(measured) and not incomplete
    median_token = statistics.median(token_reductions) if token_reductions else None
    median_wall = statistics.median(wall_reductions) if wall_reductions else None
    checks = {
        "zero_missed_conclusion_or_disclosure_critical": not optimized_critical_misses,
        "substantive_recall_within_two_percentage_points": optimized_recall >= baseline_recall - 0.02,
        "precision_no_worse": optimized_precision >= baseline_precision,
        "zero_accepted_partial_or_stale_outputs": partial_or_stale == 0,
        "zero_failed_settled_spot_checks": settled_failures == 0,
        "telemetry_coverage_100_percent": all(value == 1.0 for value in coverage_min.values()),
        "median_token_reduction_at_least_30_percent": median_token is not None and median_token >= 0.30,
        "median_wall_clock_reduction_at_least_35_percent": median_wall is not None and median_wall >= 0.35,
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
        },
        "performance": {
            "median_paired_token_reduction_fraction": median_token,
            "median_paired_wall_clock_reduction_fraction": median_wall,
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
            "recall_difference_fraction": optimized_recall - baseline_recall,
            "baseline_precision": baseline_precision,
            "optimized_precision": optimized_precision,
            "precision_difference_fraction": optimized_precision - baseline_precision,
            "optimized_critical_misses": optimized_critical_misses,
        },
        "optimized_safety": {
            "minimum_telemetry_coverage": coverage_min if measured else None,
            "accepted_partial_or_stale_outputs": partial_or_stale if measured else None,
            "failed_settled_spot_checks": settled_failures if measured else None,
        },
        "release_gates": {"status": gate_status, "ready": ready, "checks": checks},
        "per_case": per_case,
    }


def markdown(result: dict[str, Any]) -> str:
    selection = result["selection"]
    performance = result["performance"]
    noninferiority = result["noninferiority"]
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
    if selection["omitted_real_case_ids"]:
        lines += ["", "Real cases beyond the configured cap were omitted: " + ", ".join(
            f"`{case_id}`" for case_id in selection["omitted_real_case_ids"]
        ) + "."]
    lines += [
        "", "| Outcome | Result |", "|---|---:|",
        f"| Median paired token reduction | {percent(performance['median_paired_token_reduction_fraction'])} |",
        f"| Median paired wall-clock reduction | {percent(performance['median_paired_wall_clock_reduction_fraction'])} |",
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
    if args.require_complete and result["selection"]["incomplete_case_ids"]:
        return 1
    if args.require_gates and result["release_gates"]["status"] != "pass":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
