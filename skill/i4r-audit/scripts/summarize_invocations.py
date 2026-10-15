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
import lifecycle_journal


TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_creation_input_tokens",
    "output_tokens", "reasoning_tokens", "total_tokens",
)
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
    """Prove initial provider fan-out separately from the final completed-role quorum."""
    # Rows corrected to legacy_record belong to a superseded run (init_run.sh --force); they are
    # history, not the canonical fan-out, so they neither form the quorum nor the concurrency window.
    phase_one_calls = [
        row for row in rows
        if row.get("provider_call") is True
        and row.get("role_class") == "auditor"
        and row.get("round") == 0
        and row.get("status") != "legacy_record"
    ]
    completed = [row for row in phase_one_calls if row.get("status") == "completed"]
    by_role: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in completed:
        by_role[str(row.get("role"))].append(row)
    counts = {role: len(by_role.get(role, [])) for role in PHASE_ONE_REPORTS}
    extra_roles = sorted(set(by_role) - set(PHASE_ONE_REPORTS))
    exact_quorum = not extra_roles and all(count == 1 for count in counts.values())

    # The initial fan-out is each role's earliest canonical provider call. Attempt numbers are
    # round-wide and continue across a reset, so the canonical run may start at attempt 2 or 3.
    earliest_by_role: dict[str, dict[str, Any]] = {}
    for row in phase_one_calls:
        role = str(row.get("role"))
        current = earliest_by_role.get(role)
        if current is None or str(row.get("started_at") or "") < str(current.get("started_at") or ""):
            earliest_by_role[role] = row
    initial_calls = list(earliest_by_role.values())
    initial_by_role: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in initial_calls:
        initial_by_role[str(row.get("role"))].append(row)
    initial_counts = {role: len(initial_by_role.get(role, [])) for role in PHASE_ONE_REPORTS}
    initial_extra_roles = sorted(set(initial_by_role) - set(PHASE_ONE_REPORTS))
    initial_fanout_exact = (
        not initial_extra_roles and all(count == 1 for count in initial_counts.values())
    )

    canonical_outputs = True
    errors: list[str] = []
    if required and not exact_quorum:
        errors.append(
            "Phase-1 completed auditor invocations do not form the exact five-role quorum: "
            f"counts={counts}, extra_roles={extra_roles}"
        )
    if required and not initial_fanout_exact:
        errors.append(
            "Phase-1 initial provider calls do not form the exact five-role fan-out: "
            f"counts={initial_counts}, extra_roles={initial_extra_roles}"
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
    if initial_fanout_exact:
        selected = [initial_by_role[role][0] for role in PHASE_ONE_REPORTS]
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
        "initial_provider_call_counts": initial_counts,
        "extra_initial_auditor_roles": initial_extra_roles,
        "initial_fanout_exact": initial_fanout_exact,
        "common_overlap": {
            "started_at": overlap_start, "ended_at": overlap_end,
            "seconds": overlap_seconds,
            "positive": overlap_seconds is not None and overlap_seconds > 0,
            "source": "attempt_1_provider_calls",
        },
    }, errors


def usage_disposition_complete(row: dict[str, Any]) -> bool:
    """True when every observed or unavailable token field is honestly classified."""
    try:
        record_invocation.validate_v2.validate_invocation(row)
    except record_invocation.validate_v2.ValidationError:
        return False
    return True


def model_identity_disposition_complete(row: dict[str, Any]) -> bool:
    """True when served identity is observed, or honestly unavailable before completion."""
    served = row.get("served_model", row.get("observed_model"))
    assurance = row.get("identity_assurance")
    if served and (assurance == "observed" or row.get("model_source") == "observed"):
        return True
    if assurance == "requested_only" and row.get("requested_model") and served is None:
        return True
    return (
        row.get("status") in {"failed", "cancelled", "timed_out"}
        and row.get("observed_model") is None
        and row.get("model_source") == "unavailable"
    )


def total_kind(row: dict[str, Any]) -> str:
    usage = row["usage"]
    explicit = usage.get("total_tokens_kind")
    if explicit in {"provider_reported", "derived", "unavailable"}:
        return explicit
    # The pre-extension contract admitted only a complete provider total or a fully
    # unavailable usage record. Preserve those immutable rows without relabeling a
    # derived value as provider-observed.
    if usage.get("obtainable") is True and isinstance(usage.get("total_tokens"), int):
        return "provider_reported"
    return "unavailable"


def token_summary(group: list[dict[str, Any]]) -> tuple[dict[str, int | None], dict[str, int]]:
    sums: dict[str, int | None] = {}
    counts: dict[str, int] = {}
    for field in TOKEN_FIELDS:
        values = [row["usage"].get(field) for row in group]
        observed = [value for value in values if isinstance(value, int) and not isinstance(value, bool)]
        sums[field] = sum(observed) if observed else None
        counts[field] = len(observed)
    return sums, counts


def total_basis_summary(group: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for kind in ("provider_reported", "derived", "unavailable"):
        selected = [row for row in group if total_kind(row) == kind]
        values = [row["usage"].get("total_tokens") for row in selected]
        observed = [value for value in values if isinstance(value, int) and not isinstance(value, bool)]
        result[kind] = {
            "calls": len(selected),
            "sum": sum(observed) if observed else None,
        }
    result["accounted"] = {
        "calls": result["provider_reported"]["calls"] + result["derived"]["calls"],
        "sum": sum(
            value for value in (
                result["provider_reported"]["sum"], result["derived"]["sum"]
            ) if value is not None
        ) if result["provider_reported"]["calls"] + result["derived"]["calls"] else None,
    }
    return result


def interval_union_seconds(intervals: list[tuple[dt.datetime, dt.datetime]]) -> float:
    valid = sorted((start, end) for start, end in intervals if end >= start)
    if not valid:
        return 0.0
    total = 0.0
    current_start, current_end = valid[0]
    for start, end in valid[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
    return total + (current_end - current_start).total_seconds()


def event_intervals(
    events: list[dict[str, Any]], start_type: str, end_type: str,
    *, close_at: dt.datetime | None = None,
) -> list[tuple[dt.datetime, dt.datetime]]:
    starts: list[dt.datetime] = []
    intervals: list[tuple[dt.datetime, dt.datetime]] = []
    for event in events:
        if event.get("type") == start_type:
            starts.append(lifecycle_journal.parse_time(event.get("at")))
        elif event.get("type") == end_type and starts:
            intervals.append((starts.pop(), lifecycle_journal.parse_time(event.get("at"))))
    if close_at is not None:
        intervals.extend((start, close_at) for start in starts)
    return intervals


def duration_summary(
    rows: list[dict[str, Any]], events: list[dict[str, Any]], run: dict[str, Any] | None,
) -> dict[str, Any]:
    provider_intervals: list[tuple[dt.datetime, dt.datetime]] = []
    for row in rows:
        if row.get("inference_start_status", "started") != "started":
            continue
        if row.get("started_at") and row.get("ended_at"):
            provider_intervals.append((
                lifecycle_journal.parse_time(row["started_at"]),
                lifecycle_journal.parse_time(row["ended_at"]),
            ))
    provider_sum_dispositions = [
        row.get("provider_duration_seconds", row.get("elapsed_seconds"))
        for row in rows if row.get("inference_start_status", "started") == "started"
    ]
    provider_sum_values = [
        float(value) for value in provider_sum_dispositions
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    provider_sum = sum(provider_sum_values)
    boundary = None
    if run and run.get("ended_at"):
        boundary = lifecycle_journal.parse_time(run["ended_at"])
    elif events:
        boundary = lifecycle_journal.parse_time(events[-1]["at"])
    authorization = event_intervals(
        events, "authorization_wait_start", "authorization_wait_end", close_at=boundary,
    )
    paused = event_intervals(events, "pause_start", "pause_end", close_at=boundary)
    # 2.6.9: a finalized audit that was reopened (reopen_audit.py, amend_round_cap.py --reopen) lay closed from its
    # previous finalize until the reopen. That dormant stretch is a pause (finalized_then_reopened), not idle time.
    for event in events:
        if event.get("type") == "audit_reopened":
            closed = (event.get("payload") or {}).get("previous_finalized_at")
            try:
                start = lifecycle_journal.parse_time(closed)
            except (lifecycle_journal.JournalError, ValueError, TypeError):
                continue
            end = lifecycle_journal.parse_time(event.get("at"))
            if end >= start:
                paused.append((start, end))
    retry = event_intervals(events, "retry_backoff_start", "retry_backoff_end", close_at=boundary)
    # Round authorization-required/authorized events are also exact wait boundaries.
    waiting_by_round: dict[Any, dt.datetime] = {}
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") == "round_authorization_required":
            waiting_by_round[payload.get("round")] = lifecycle_journal.parse_time(event["at"])
        elif event.get("type") == "round_authorized" and payload.get("round") in waiting_by_round:
            authorization.append((
                waiting_by_round.pop(payload.get("round")),
                lifecycle_journal.parse_time(event["at"]),
            ))
    if boundary is not None:
        authorization.extend((start, boundary) for start in waiting_by_round.values())
    elapsed = None
    overall: list[tuple[dt.datetime, dt.datetime]] = []
    if run and run.get("started_at"):
        start = lifecycle_journal.parse_time(run["started_at"])
        end = boundary or start
        elapsed = max(0.0, (end - start).total_seconds())
        overall = provider_intervals + authorization + paused + retry
    human = None
    if run and isinstance(run.get("human_effort"), dict):
        values = (run["human_effort"].get("person_minutes_by_phase") or {}).values()
        observed = [value for value in values if isinstance(value, (int, float))]
        human = sum(observed) if observed else None
    occupied = interval_union_seconds(overall)
    return {
        "audit_elapsed_seconds": elapsed,
        "provider_duration_sum_seconds": provider_sum if provider_sum_values else None,
        "provider_active_interval_union_seconds": interval_union_seconds(provider_intervals),
        "authorization_wait_seconds": interval_union_seconds(authorization),
        "paused_seconds": interval_union_seconds(paused),
        "retry_backoff_seconds": interval_union_seconds(retry),
        "idle_seconds": max(0.0, elapsed - occupied) if elapsed is not None else None,
        "human_effort_person_minutes": human,
    }


def normalized_usage_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    fresh: list[int] = []
    for row in calls:
        usage = row["usage"]
        semantics = usage.get("semantics") or {}
        input_count = usage.get("input_tokens")
        cached = usage.get("cached_input_tokens")
        if not isinstance(input_count, int) or isinstance(input_count, bool):
            continue
        if semantics.get("input_tokens") == "fresh_only":
            fresh.append(input_count)
        elif semantics.get("input_tokens") == "includes_cache_read" and isinstance(cached, int):
            fresh.append(max(0, input_count - cached))
    totals = total_basis_summary(calls)
    return {
        "fresh_input_tokens": sum(fresh) if fresh else None,
        "fresh_input_observation_count": len(fresh),
        "cache_read_tokens": sum(
            row["usage"]["cached_input_tokens"] for row in calls
            if isinstance(row["usage"].get("cached_input_tokens"), int)
        ) if any(isinstance(row["usage"].get("cached_input_tokens"), int) for row in calls) else None,
        "cache_write_tokens": sum(
            row["usage"]["cache_creation_input_tokens"] for row in calls
            if isinstance(row["usage"].get("cache_creation_input_tokens"), int)
        ) if any(isinstance(row["usage"].get("cache_creation_input_tokens"), int) for row in calls) else None,
        "output_tokens": sum(
            row["usage"]["output_tokens"] for row in calls
            if isinstance(row["usage"].get("output_tokens"), int)
        ) if any(isinstance(row["usage"].get("output_tokens"), int) for row in calls) else None,
        "reasoning_tokens": sum(
            row["usage"]["reasoning_tokens"] for row in calls
            if isinstance(row["usage"].get("reasoning_tokens"), int)
        ) if any(isinstance(row["usage"].get("reasoning_tokens"), int) for row in calls) else None,
        "normalized_non_overlapping_total": totals["accounted"]["sum"],
        "normalized_total_is_lower_bound": totals["unavailable"]["calls"] > 0,
    }


def aggregate(
    rows: list[dict[str, Any]], *, require_phase_one: bool = False,
    events: list[dict[str, Any]] | None = None, run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    calls = [row for row in rows if row["provider_call"]]
    token_complete = [row for row in calls if usage_disposition_complete(row)]
    observed_identity = [row for row in calls if row["requested_model"] and
                         row.get("served_model", row.get("observed_model")) and
                         row["model_family"] and
                         row.get("identity_assurance", "observed") == "observed"]
    identity_disposition_complete = [row for row in calls
                                     if row["requested_model"] and row["model_family"] and
                                     model_identity_disposition_complete(row)]
    effort_complete = [row for row in calls if row["reasoning_effort"] not in
                       {"unknown", "provider_default"}]
    latency_complete = [row for row in calls if row["started_at"] and row["ended_at"] and
                        row["elapsed_seconds"] is not None]
    outcomes_complete = [row for row in calls if row["status"] in
                         {"completed", "failed", "cancelled", "timed_out", "legacy_record"}]

    by_model: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    by_role: collections.Counter[str] = collections.Counter()
    by_status: collections.Counter[str] = collections.Counter()
    by_superseded_status: collections.Counter[str] = collections.Counter()
    for row in calls:
        served = row.get("served_model", row.get("observed_model"))
        by_model[(row["requested_model"] or "unavailable", served or "unavailable",
                  row["reasoning_effort"])].append(row)
        by_role[row["role"]] += 1
        by_status[row["status"]] += 1
        if row.get("superseded_status"):
            by_superseded_status[row["superseded_status"]] += 1

    model_rows = []
    for (requested, observed, effort), group in sorted(by_model.items()):
        tokens, observation_counts = token_summary(group)
        elapsed = [float(row["elapsed_seconds"]) for row in group if row["elapsed_seconds"] is not None]
        model_rows.append({
            "requested_model": requested, "observed_model": observed, "reasoning_effort": effort,
            "calls": len(group), "tokens": tokens,
            "token_observation_counts": observation_counts,
            "total_token_basis": total_basis_summary(group),
            "median_elapsed_seconds": statistics.median(elapsed) if elapsed else None,
        })
    totals, total_observation_counts = token_summary(calls)
    total_bases = total_basis_summary(calls)
    phase_one, phase_one_errors = phase_one_evidence(rows, require_phase_one)
    errors = list(phase_one_errors)
    if not calls:
        errors.append("no provider calls recorded")
    if len(token_complete) != len(calls):
        errors.append(f"token disposition telemetry covers {len(token_complete)}/{len(calls)} provider calls")
    if len(identity_disposition_complete) != len(calls):
        errors.append(
            "model identity disposition covers "
            f"{len(identity_disposition_complete)}/{len(calls)} provider calls"
        )
    if len(effort_complete) != len(calls):
        errors.append(f"exact reasoning effort covers {len(effort_complete)}/{len(calls)} provider calls")
    if len(latency_complete) != len(calls):
        errors.append(f"latency telemetry covers {len(latency_complete)}/{len(calls)} provider calls")
    if len(outcomes_complete) != len(calls):
        errors.append(f"terminal outcome covers {len(outcomes_complete)}/{len(calls)} provider calls")
    per_model_usage: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    unattributed_rows: list[dict[str, Any]] = []
    for row in calls:
        for partition in row.get("usage_by_model") or []:
            per_model_usage[partition["model"]].append({"usage": partition["usage"]})
        if isinstance(row.get("unattributed_usage"), dict):
            unattributed_rows.append({"usage": row["unattributed_usage"]})
    usage_by_served_model = []
    for model, partitions in sorted(per_model_usage.items()):
        token_values, observation_counts = token_summary(partitions)
        usage_by_served_model.append({
            "model": model, "invocation_partitions": len(partitions),
            "tokens": token_values, "token_observation_counts": observation_counts,
        })
    unattributed_tokens, unattributed_counts = token_summary(unattributed_rows)
    failed_accounted = sum(
        row["usage"].get("total_tokens") or 0
        for row in calls if row.get("status") != "completed"
        and isinstance(row["usage"].get("total_tokens"), int)
    )
    accounted_total = total_bases["accounted"]["sum"]
    cost_records = [row["provider_cost"] for row in calls if isinstance(row.get("provider_cost"), dict)]
    cost_values = [
        record.get("amount") for record in cost_records
        if isinstance(record.get("amount"), (int, float)) and not isinstance(record.get("amount"), bool)
    ]
    turn_values = [row.get("turn_count") for row in calls if isinstance(row.get("turn_count"), int)]
    events = events or []
    current_watermark = lifecycle_journal.watermark(events) if events else None
    lower_bound = total_bases["unavailable"]["calls"] > 0
    return {
        "schema_version": "2.1.0" if events or any("attempt_kind" in row for row in rows) else "2.0.0",
        "generated_at": (
            events[-1]["at"] if events
            else dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        "journal_watermark": current_watermark,
        "records": len(rows), "provider_calls": len(calls),
        "accepted_inference_starts": sum(
            row.get("inference_start_status", "started") == "started" for row in calls
        ),
        "cache_records": sum(not row["provider_call"] for row in rows),
        "coverage": {
            "tokens": len(token_complete) / len(calls) if calls else 0,
            "model_identity_disposition": (
                len(identity_disposition_complete) / len(calls) if calls else 0
            ),
            "observed_model": len(observed_identity) / len(calls) if calls else 0,
            "reasoning_effort": len(effort_complete) / len(calls) if calls else 0,
            "latency": len(latency_complete) / len(calls) if calls else 0,
            "outcome": len(outcomes_complete) / len(calls) if calls else 0,
        },
        "numeric_token_coverage": {
            field: total_observation_counts[field] / len(calls) if calls else 0
            for field in TOKEN_FIELDS
        },
        "tokens": totals,
        "normalized_usage": normalized_usage_summary(calls),
        "totals_are_lower_bounds": lower_bound,
        "unaccounted_provider_calls": total_bases["unavailable"]["calls"],
        "failed_accounted_tokens": failed_accounted,
        "failed_accounted_token_fraction": (
            failed_accounted / accounted_total if accounted_total else None
        ),
        "token_observation_counts": total_observation_counts,
        "total_token_basis": total_bases,
        "usage_by_served_model": usage_by_served_model,
        "unattributed_within_invocation": {
            "partitions": len(unattributed_rows), "tokens": unattributed_tokens,
            "token_observation_counts": unattributed_counts,
        },
        "provider_cost": {
            "disposition_calls": len(cost_records), "observed_calls": len(cost_values),
            "sum": sum(cost_values) if cost_values else None,
        },
        "turn_count": {
            "observed_calls": len(turn_values),
            "sum": sum(turn_values) if turn_values else None,
            "median": statistics.median(turn_values) if turn_values else None,
        },
        "durations": duration_summary(calls, events, run),
        "calls_by_role": dict(sorted(by_role.items())),
        "calls_by_status": dict(sorted(by_status.items())),
        "calls_by_corrected_from_status": dict(sorted(by_superseded_status.items())),
        "models": model_rows,
        "phase_one": phase_one,
        "errors": errors,
    }


def markdown(summary: dict[str, Any]) -> str:
    coverage = summary["coverage"]
    bound = " (lower bound)" if summary.get("totals_are_lower_bounds") else ""
    lines = ["# Invocation telemetry", "",
             f"Provider calls: {summary['provider_calls']}; non-provider records: {summary['cache_records']}.", "",
             f"Accounted tokens are reported{bound}; unknown failed-call usage remains unknown.", "",
             "| Coverage | Fraction |", "|---|---:|"]
    for key, value in coverage.items():
        lines.append(f"| {key.replace('_', ' ')} | {value:.1%} |")
    # "tokens | 100.0%" means every call's usage disposition is KNOWN -- observed or honestly
    # unavailable -- not that every call's numbers are known. Reported alone it reads as full
    # accounting, so the two figures that qualify it are printed immediately beneath.
    unaccounted = summary.get("unaccounted_provider_calls")
    if unaccounted:
        lines += [
            "",
            f"**{unaccounted} provider call(s) report usage as unavailable.** Token coverage "
            "counts them as covered because their disposition is known; the totals below are "
            "lower bounds, not complete counts.",
        ]
    lines += ["", "| Requested model | Primary observed model | Effort | Calls | Input | Cache read | Cache create | Output | Reasoning | Accounted total | Total basis | Median latency (s) |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|"]
    for row in summary["models"]:
        tok = row["tokens"]
        observed = row["token_observation_counts"]
        calls = row["calls"]
        def token_cell(field: str) -> str:
            value = tok[field]
            rendered = "—" if value is None else str(value)
            return f"{rendered} ({observed[field]}/{calls})"
        basis = row["total_token_basis"]
        basis_text = ", ".join(
            f"{kind}={basis[kind]['calls']}"
            for kind in ("provider_reported", "derived", "unavailable")
            if basis[kind]["calls"]
        ) or "—"
        latency = "—" if row["median_elapsed_seconds"] is None else f"{row['median_elapsed_seconds']:.1f}"
        lines.append("| " + " | ".join([
            row["requested_model"], row["observed_model"], row["reasoning_effort"], str(row["calls"]),
            token_cell("input_tokens"), token_cell("cached_input_tokens"),
            token_cell("cache_creation_input_tokens"), token_cell("output_tokens"),
            token_cell("reasoning_tokens"), token_cell("total_tokens"), basis_text, latency,
        ]) + " |")
    lines += ["", "## Calls by role", ""]
    lines += [f"- `{role}`: {count}" for role, count in summary["calls_by_role"].items()]
    phase_one = summary["phase_one"]
    if phase_one["required"]:
        overlap = phase_one["common_overlap"]
        lines += ["", "## Phase-1 fan-out evidence", "",
                  f"- Exact five-role completed quorum: {phase_one['exact_quorum']}",
                  f"- Exact initial five-role provider fan-out: {phase_one['initial_fanout_exact']}",
                  f"- Canonical role-specific outputs: {phase_one['canonical_outputs']}",
                  f"- Initial provider-call common overlap: {overlap['seconds']} seconds "
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
    parser.add_argument("--check", action="store_true", help="fail unless all required telemetry is 100%%")
    args = parser.parse_args()
    root = args.audit_dir.resolve()
    try:
        rows = load(root)
        run_path = root / "run.json"
        run = None
        require_phase_one = False
        if run_path.is_file():
            run = record_invocation.validate_json_schema.strict_loads(
                run_path.read_text(encoding="utf-8"), source=str(run_path)
            )
            require_phase_one = (isinstance(run, dict) and run.get("schema_version") in {"2.0.0", "2.1.0"}
                                 and not run.get("normalization"))
        events = []
        if run_path.is_file() and isinstance(run, dict) and run.get("schema_version") == "2.1.0":
            events = lifecycle_journal.load_events(root)
        summary = aggregate(
            rows, require_phase_one=require_phase_one, events=events,
            run=run if run_path.is_file() else None,
        )
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
