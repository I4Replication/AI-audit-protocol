#!/usr/bin/env python3
"""Validate canonical invocation telemetry and produce exact token/latency summaries."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import pathlib
import statistics
import sys
import tempfile
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import record_invocation


TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
PHASE_ONE_REPORTS = {
    "auditor_reproduction": "report/01_reproduction.md",
    "auditor_data_integrity": "report/02_data_integrity.md",
    "auditor_text_vs_data": "report/03_text_vs_data.md",
    "auditor_code_methods": "report/04_code_methods.md",
    "auditor_privacy_archive": "report/05_privacy_archive.md",
}


def atomic(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load(root: pathlib.Path) -> list[dict[str, Any]]:
    path = root / "invocations.jsonl"
    if not path.is_file():
        raise ValueError(f"canonical invocation ledger is missing: {path}")
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = record_invocation.validate_json_schema.strict_loads(
                line, source=f"invocations.jsonl line {line_number}"
            )
            record_invocation.validate_record(row, root)
        except ValueError as exc:
            raise ValueError(f"invocations.jsonl line {line_number}: {exc}") from exc
        rows.append(row)
    ids = [row["invocation_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("canonical invocation ledger has duplicate invocation IDs")
    record_invocation.validate_attempt_sequences(rows)
    return rows


def phase_one_evidence(rows: list[dict[str, Any]], required: bool) -> tuple[dict[str, Any], list[str]]:
    """Prove the canonical five-way fan-out from terminal provider-call records."""
    auditors = [row for row in rows if row.get("provider_call") is True and
                row.get("status") == "completed" and row.get("role_class") == "auditor"]
    by_role: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in auditors:
        by_role[str(row.get("role"))].append(row)
    counts = {role: len(by_role.get(role, [])) for role in PHASE_ONE_REPORTS}
    extra_roles = sorted(set(by_role) - set(PHASE_ONE_REPORTS))
    exact_quorum = not extra_roles and all(count == 1 for count in counts.values())
    canonical_outputs = True
    errors: list[str] = []
    if required and not exact_quorum:
        errors.append(
            "Phase-1 completed auditor invocations do not form the exact five-role quorum: "
            f"counts={counts}, extra_roles={extra_roles}"
        )
    for role, expected in PHASE_ONE_REPORTS.items():
        for row in by_role.get(role, []):
            if row.get("output_artifact") != expected:
                canonical_outputs = False
                if required:
                    errors.append(
                        f"Phase-1 auditor {role} has output_artifact={row.get('output_artifact')!r}; "
                        f"expected canonical output_artifact {expected!r}"
                    )

    overlap_start = overlap_end = None
    overlap_seconds = None
    if exact_quorum:
        selected = [by_role[role][0] for role in PHASE_ONE_REPORTS]
        starts = [(record_invocation.parse_time(row["started_at"], "started_at"), row)
                  for row in selected]
        ends = [(record_invocation.parse_time(row["ended_at"], "ended_at"), row)
                for row in selected]
        latest_start, latest_row = max(starts, key=lambda item: item[0])
        earliest_end, earliest_row = min(ends, key=lambda item: item[0])
        overlap_start = latest_row["started_at"]
        overlap_end = earliest_row["ended_at"]
        overlap_seconds = (earliest_end - latest_start).total_seconds()
        if required and overlap_seconds <= 0:
            errors.append(
                "Phase-1 five-way concurrency has no positive common overlap: "
                f"max(started_at)={overlap_start} must be earlier than min(ended_at)={overlap_end}"
            )
    return {
        "required": required,
        "expected_roles": list(PHASE_ONE_REPORTS),
        "completed_role_counts": counts,
        "extra_completed_auditor_roles": extra_roles,
        "exact_quorum": exact_quorum,
        "canonical_outputs": canonical_outputs and exact_quorum,
        "common_overlap": {
            "started_at": overlap_start, "ended_at": overlap_end,
            "seconds": overlap_seconds,
            "positive": overlap_seconds is not None and overlap_seconds > 0,
        },
    }, errors


def aggregate(rows: list[dict[str, Any]], *, require_phase_one: bool = False) -> dict[str, Any]:
    calls = [row for row in rows if row["provider_call"]]
    token_complete = [row for row in calls if row["usage"]["obtainable"] and
                      row["usage"]["source"] != "unavailable" and
                      all(row["usage"].get(field) is not None for field in TOKEN_FIELDS)]
    identity_complete = [row for row in calls if row["requested_model"] and
                         row["observed_model"] and row["model_family"] and
                         row["model_source"] == "observed"]
    effort_complete = [row for row in calls if row["reasoning_effort"] not in
                       {"unknown", "provider_default"}]
    latency_complete = [row for row in calls if row["started_at"] and row["ended_at"] and
                        row["elapsed_seconds"] is not None]
    outcomes_complete = [row for row in calls if row["status"] in
                         {"completed", "failed", "cancelled", "timed_out"}]

    by_model: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    by_role: collections.Counter[str] = collections.Counter()
    by_status: collections.Counter[str] = collections.Counter()
    for row in calls:
        by_model[(row["requested_model"] or "unavailable", row["observed_model"] or "unavailable",
                  row["reasoning_effort"])].append(row)
        by_role[row["role"]] += 1
        by_status[row["status"]] += 1

    model_rows = []
    for (requested, observed, effort), group in sorted(by_model.items()):
        tokens = {field: sum((row["usage"].get(field) or 0) for row in group) for field in TOKEN_FIELDS}
        elapsed = [float(row["elapsed_seconds"]) for row in group if row["elapsed_seconds"] is not None]
        model_rows.append({
            "requested_model": requested, "observed_model": observed, "reasoning_effort": effort,
            "calls": len(group), "tokens": tokens,
            "median_elapsed_seconds": statistics.median(elapsed) if elapsed else None,
        })
    totals = {field: sum((row["usage"].get(field) or 0) for row in calls) for field in TOKEN_FIELDS}
    phase_one, phase_one_errors = phase_one_evidence(rows, require_phase_one)
    errors = list(phase_one_errors)
    if not calls:
        errors.append("no provider calls recorded")
    if len(token_complete) != len(calls):
        errors.append(f"token telemetry covers {len(token_complete)}/{len(calls)} provider calls")
    if len(identity_complete) != len(calls):
        errors.append(f"observed model identity covers {len(identity_complete)}/{len(calls)} provider calls")
    if len(effort_complete) != len(calls):
        errors.append(f"exact reasoning effort covers {len(effort_complete)}/{len(calls)} provider calls")
    if len(latency_complete) != len(calls):
        errors.append(f"latency telemetry covers {len(latency_complete)}/{len(calls)} provider calls")
    if len(outcomes_complete) != len(calls):
        errors.append(f"terminal outcome covers {len(outcomes_complete)}/{len(calls)} provider calls")
    return {
        "schema_version": "2.0.0",
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "records": len(rows), "provider_calls": len(calls),
        "cache_records": sum(not row["provider_call"] for row in rows),
        "coverage": {
            "tokens": len(token_complete) / len(calls) if calls else 0,
            "observed_model": len(identity_complete) / len(calls) if calls else 0,
            "reasoning_effort": len(effort_complete) / len(calls) if calls else 0,
            "latency": len(latency_complete) / len(calls) if calls else 0,
            "outcome": len(outcomes_complete) / len(calls) if calls else 0,
        },
        "tokens": totals, "calls_by_role": dict(sorted(by_role.items())),
        "calls_by_status": dict(sorted(by_status.items())), "models": model_rows,
        "phase_one": phase_one,
        "errors": errors,
    }


def markdown(summary: dict[str, Any]) -> str:
    coverage = summary["coverage"]
    lines = ["# Invocation telemetry", "",
             f"Provider calls: {summary['provider_calls']}; cache-only records: {summary['cache_records']}.", "",
             "| Coverage | Fraction |", "|---|---:|"]
    for key, value in coverage.items():
        lines.append(f"| {key.replace('_', ' ')} | {value:.1%} |")
    lines += ["", "| Requested model | Observed served model | Effort | Calls | Input | Cached | Output | Reasoning | Total | Median latency (s) |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in summary["models"]:
        tok = row["tokens"]
        latency = "—" if row["median_elapsed_seconds"] is None else f"{row['median_elapsed_seconds']:.1f}"
        lines.append("| " + " | ".join([
            row["requested_model"], row["observed_model"], row["reasoning_effort"], str(row["calls"]),
            str(tok["input_tokens"]), str(tok["cached_input_tokens"]), str(tok["output_tokens"]),
            str(tok["reasoning_tokens"]), str(tok["total_tokens"]), latency,
        ]) + " |")
    lines += ["", "## Calls by role", ""]
    lines += [f"- `{role}`: {count}" for role, count in summary["calls_by_role"].items()]
    phase_one = summary["phase_one"]
    if phase_one["required"]:
        overlap = phase_one["common_overlap"]
        lines += ["", "## Phase-1 fan-out evidence", "",
                  f"- Exact five-role quorum: {phase_one['exact_quorum']}",
                  f"- Canonical role-specific outputs: {phase_one['canonical_outputs']}",
                  f"- Common overlap: {overlap['seconds']} seconds "
                  f"({overlap['started_at']} to {overlap['ended_at']})"]
    if summary["errors"]:
        lines += ["", "## Release-blocking telemetry gaps", ""]
        lines += [f"- {error}" for error in summary["errors"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--json", type=pathlib.Path)
    parser.add_argument("--markdown", type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail unless all required telemetry is 100%")
    args = parser.parse_args()
    root = args.audit_dir.resolve()
    try:
        rows = load(root)
        run_path = root / "run.json"
        require_phase_one = False
        if run_path.is_file():
            run = record_invocation.validate_json_schema.strict_loads(
                run_path.read_text(encoding="utf-8"), source=str(run_path)
            )
            require_phase_one = (isinstance(run, dict) and run.get("schema_version") == "2.0.0"
                                 and not run.get("normalization"))
        summary = aggregate(rows, require_phase_one=require_phase_one)
    except (OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    json_path = args.json or root / "run_metrics.json"
    md_path = args.markdown or root / "RUN_METADATA.md"
    atomic(json_path, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    atomic(md_path, markdown(summary))
    print(markdown(summary), end="")
    if args.check and summary["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
