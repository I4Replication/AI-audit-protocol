#!/usr/bin/env python3
"""Focused tests for complete multi-model Claude CLI token accounting."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE = Path(__file__).parents[1] / "scripts" / "claude_model_usage.py"
SPEC = importlib.util.spec_from_file_location("claude_model_usage", MODULE)
assert SPEC is not None and SPEC.loader is not None
usage_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usage_module)


def result(*, partial_top: bool = False) -> dict:
    sonnet = {
        "inputTokens": 2,
        "outputTokens": 163,
        "cacheReadInputTokens": 30597,
        "cacheCreationInputTokens": 10876,
        "costUSD": 1.25,
    }
    top = {
        "input_tokens": 1 if partial_top else 2,
        "output_tokens": 160 if partial_top else 163,
        "cache_read_input_tokens": 30000 if partial_top else 30597,
        "cache_creation_input_tokens": 10000 if partial_top else 10876,
    }
    return {
        "usage": top,
        "modelUsage": {
            "claude-haiku-4-5": {
                "inputTokens": 1500,
                "outputTokens": 20,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "costUSD": 0.002,
            },
            "claude-sonnet-5": sonnet,
        },
        "total_cost_usd": 1.252,
    }


class ClaudeModelUsageTest(unittest.TestCase):
    def test_sums_every_served_model_without_adding_top_level_again(self) -> None:
        usage, evidence = usage_module.aggregate(result(), "claude-sonnet-5")
        self.assertEqual(usage["input_tokens"], 1502)
        self.assertEqual(usage["output_tokens"], 183)
        self.assertEqual(usage["cached_input_tokens"], 30597)
        self.assertEqual(usage["cache_creation_input_tokens"], 10876)
        self.assertEqual(usage["total_tokens"], 43158)
        self.assertEqual(usage["total_tokens_components"], [
            "input_tokens", "cached_input_tokens",
            "cache_creation_input_tokens", "output_tokens",
        ])
        self.assertEqual(evidence["top_level_relation"], "matches_primary")
        self.assertTrue(evidence["cost_sum_matches"])

    def test_accepts_error_result_whose_top_level_is_a_primary_subset(self) -> None:
        usage, evidence = usage_module.aggregate(result(partial_top=True), "claude-sonnet-5")
        self.assertEqual(usage["total_tokens"], 43158)
        self.assertEqual(evidence["top_level_relation"], "primary_superset_of_top_level")
        self.assertEqual(evidence["primary_minus_top_level"]["cached_input_tokens"], 597)

    def test_rejects_cost_mismatch(self) -> None:
        value = result()
        value["total_cost_usd"] = 2.0
        with self.assertRaisesRegex(ValueError, "costUSD sum"):
            usage_module.aggregate(value, "claude-sonnet-5")

    def test_rejects_top_level_greater_than_primary(self) -> None:
        value = result()
        value["usage"]["output_tokens"] = 164
        with self.assertRaisesRegex(ValueError, "exceeds primary"):
            usage_module.aggregate(value, "claude-sonnet-5")

    def test_rejects_missing_per_model_count(self) -> None:
        value = result()
        del value["modelUsage"]["claude-haiku-4-5"]["inputTokens"]
        with self.assertRaisesRegex(ValueError, "inputTokens"):
            usage_module.aggregate(value, "claude-sonnet-5")


if __name__ == "__main__":
    unittest.main()
