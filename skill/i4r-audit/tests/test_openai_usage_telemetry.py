#!/usr/bin/env python3
"""Focused regression tests for OpenAI Codex CLI token accounting."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest


MODULE = Path(__file__).parents[1] / "scripts" / "test_run_critique_runtime.py"
SPEC = importlib.util.spec_from_file_location("i4r_critique_runtime", MODULE)
runtime = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runtime)


def parse_usage(event: dict) -> dict:
    usage: dict[str, int] = {}
    extras: dict[str, int] = {}
    runtime.walk_telemetry(event["usage"], usage, extras, [], [])
    return runtime.canonicalize_codex_usage(usage, extras)


class OpenAIUsageTelemetryTests(unittest.TestCase):
    def test_exact_live_turn_completed_payload_derives_non_overlapping_total(self) -> None:
        # Exact event emitted by the live Terra model-capability probe on 2026-08-28.
        event = json.loads(
            '{"type":"turn.completed","usage":{"input_tokens":13196,'
            '"cached_input_tokens":0,"cache_write_input_tokens":0,'
            '"output_tokens":15,"reasoning_output_tokens":0}}'
        )

        usage = parse_usage(event)

        self.assertEqual(usage["input_tokens"], 13_196)
        self.assertEqual(usage["output_tokens"], 15)
        self.assertEqual(usage["cached_input_tokens"], 0)
        self.assertEqual(usage["cache_creation_input_tokens"], 0)
        self.assertEqual(usage["reasoning_tokens"], 0)
        self.assertEqual(usage["total_tokens"], 13_211)
        self.assertEqual(usage["total_tokens_kind"], "derived")
        self.assertEqual(
            usage["total_tokens_components"], ["input_tokens", "output_tokens"]
        )
        self.assertIsNone(usage["total_tokens_unavailable_reason"])
        self.assertEqual(usage["unavailable_reasons"], {})

    def test_cache_and_reasoning_counts_are_subsets_not_addends(self) -> None:
        event = {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "cache_write_input_tokens": 7,
                "output_tokens": 30,
                "reasoning_output_tokens": 20,
            },
        }

        usage = parse_usage(event)

        self.assertEqual(usage["total_tokens"], 130)
        self.assertEqual(
            usage["total_tokens_components"], ["input_tokens", "output_tokens"]
        )

    def test_provider_total_remains_provider_reported(self) -> None:
        event = {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 131},
        }

        usage = parse_usage(event)

        self.assertEqual(usage["total_tokens"], 131)
        self.assertEqual(usage["total_tokens_kind"], "provider_reported")
        self.assertEqual(usage["total_tokens_components"], [])

    def test_total_stays_unavailable_without_both_non_overlapping_components(self) -> None:
        event = {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "cached_input_tokens": 80},
        }

        usage = parse_usage(event)

        self.assertIsNone(usage["total_tokens"])
        self.assertEqual(usage["total_tokens_kind"], "unavailable")
        self.assertEqual(usage["total_tokens_components"], [])
        self.assertIn("output_tokens", usage["total_tokens_unavailable_reason"])


if __name__ == "__main__":
    unittest.main()
