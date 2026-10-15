#!/usr/bin/env python3
"""Fail-closed accounting for Claude CLI results with multiple served models."""

from __future__ import annotations

import math
from typing import Any


FIELDS = (
    ("input_tokens", "inputTokens", "input_tokens"),
    ("cached_input_tokens", "cacheReadInputTokens", "cache_read_input_tokens"),
    (
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
        "cache_creation_input_tokens",
    ),
    ("output_tokens", "outputTokens", "output_tokens"),
)
TOKEN_REASON = "provider_cli_does_not_expose_separate_reasoning_tokens"
ACCOUNTING_BASIS = (
    "sum each token category across every modelUsage entry; modelUsage costUSD values "
    "must sum to total_cost_usd, while top-level usage is checked as a non-additive "
    "primary-model view"
)


def nonnegative_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def nonnegative_number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative number")
    return float(value)


def aggregate(result: dict[str, Any], primary_model: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return schema-v2 usage plus an evidence record for its accounting basis."""
    model_usage = result.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        raise ValueError("Claude result requires a non-empty modelUsage object")
    if not isinstance(primary_model, str) or primary_model not in model_usage:
        raise ValueError("primary observed model is absent from modelUsage")

    totals = {canonical: 0 for canonical, _, _ in FIELDS}
    per_model: dict[str, dict[str, int]] = {}
    costs: dict[str, float] = {}
    for model_id, details in model_usage.items():
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("modelUsage keys must be non-empty served-model IDs")
        if not isinstance(details, dict):
            raise ValueError(f"modelUsage[{model_id!r}] must be an object")
        counts: dict[str, int] = {}
        for canonical, exposed, _ in FIELDS:
            count = nonnegative_integer(
                details.get(exposed), f"modelUsage[{model_id!r}].{exposed}"
            )
            counts[canonical] = count
            totals[canonical] += count
        per_model[model_id] = counts
        costs[model_id] = nonnegative_number(
            details.get("costUSD"), f"modelUsage[{model_id!r}].costUSD"
        )

    reported_cost = nonnegative_number(result.get("total_cost_usd"), "total_cost_usd")
    model_cost = sum(costs.values())
    if not math.isclose(model_cost, reported_cost, rel_tol=1e-12, abs_tol=1e-9):
        raise ValueError(
            f"modelUsage costUSD sum {model_cost} does not equal total_cost_usd {reported_cost}"
        )

    top_raw = result.get("usage")
    if not isinstance(top_raw, dict):
        raise ValueError("Claude result requires a top-level usage object for consistency checking")
    top_level: dict[str, int] = {}
    primary_minus_top: dict[str, int] = {}
    for canonical, _, top_name in FIELDS:
        top_count = nonnegative_integer(top_raw.get(top_name), f"usage.{top_name}")
        top_level[canonical] = top_count
        difference = per_model[primary_model][canonical] - top_count
        if difference < 0:
            raise ValueError(
                f"top-level usage.{top_name} exceeds primary modelUsage for {primary_model}"
            )
        primary_minus_top[canonical] = difference
    relation = (
        "matches_primary" if all(value == 0 for value in primary_minus_top.values())
        else "primary_superset_of_top_level"
    )

    components = [canonical for canonical, _, _ in FIELDS]
    total_tokens = sum(totals.values())
    usage = {
        "obtainable": True,
        **totals,
        "reasoning_tokens": None,
        "total_tokens": total_tokens,
        "source": "cli_log",
        "unavailable_reasons": {"reasoning_tokens": TOKEN_REASON},
        "total_tokens_kind": "derived",
        "total_tokens_components": components,
        "total_tokens_unavailable_reason": None,
    }
    evidence = {
        "accounting_basis": ACCOUNTING_BASIS,
        "served_model_ids": list(model_usage),
        "primary_model": primary_model,
        "per_model_counts": per_model,
        "all_model_counts": totals,
        "all_model_total_tokens": total_tokens,
        "top_level_counts": top_level,
        "top_level_relation": relation,
        "primary_minus_top_level": primary_minus_top,
        "per_model_cost_usd": costs,
        "model_cost_sum_usd": model_cost,
        "total_cost_usd": reported_cost,
        "cost_sum_matches": True,
    }
    return usage, evidence
