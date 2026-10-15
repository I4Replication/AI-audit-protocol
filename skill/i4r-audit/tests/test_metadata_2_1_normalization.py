from __future__ import annotations

import copy
import json
import pathlib
import sys
import unittest


SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "schemas/tools"))

from normalize_to_v2_1 import normalize_run, normalize_run_v2_to_v2_1
from validate_v2 import validate_run


class Metadata21NormalizationTests(unittest.TestCase):
    def test_v1_normalization_is_deterministic_and_hash_chained(self) -> None:
        source = json.loads((SKILL / "schemas/fixtures/v1_minimal_run.json").read_text())
        first = normalize_run(source)
        second = normalize_run(copy.deepcopy(source))
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], "2.1.0")
        self.assertEqual(len(first["invocations"]), 1)
        self.assertEqual(first["invocations"][0]["identity_assurance"], "observed")
        self.assertEqual(
            [event["type"] for event in first["events"]],
            ["init", "invocation_reserved", "provider_request", "invocation_terminal"],
        )
        # The v1 adapter cannot prove that the historical request reached an
        # accepted inference start, so it must not manufacture that event.
        self.assertEqual(first["journal_watermark"]["journal_seq"], 4)
        self.assertEqual(
            first["invocations"][0]["journal_event"]["hash"],
            first["events"][-1]["event_hash"],
        )
        validate_run(first)

    def test_v2_declared_terra_route_is_not_relabelled_as_served(self) -> None:
        source = json.loads((SKILL / "schemas/fixtures/v1_minimal_run.json").read_text())
        source["schema_version"] = "2.0.0"
        source.update({
            "round_target": None, "invocations": [{
                "invocation_id": "inv-terra", "role": "critic_stats", "role_class": "critic",
                "round": 1, "attempt": 1, "status": "legacy_record", "provider": "openai",
                "provider_call": True, "cache_usage": "none", "session_id": None,
                "requested_model": "gpt-5.6-terra", "observed_model": "gpt-5.6-terra",
                "model_family": "gpt", "model_source": "declared", "reasoning_effort": "unknown",
                "started_at": None, "ended_at": None, "elapsed_seconds": None,
                "input_artifact_sha256": None, "prompt_sha256": None,
                "output_artifact": None, "output_sha256": None,
                "usage": {"obtainable": False, "input_tokens": None, "output_tokens": None,
                          "cached_input_tokens": None, "reasoning_tokens": None,
                          "total_tokens": None, "source": "unavailable"},
                "error": None, "error_class": None, "legacy_source": None,
            }],
        })
        result = normalize_run_v2_to_v2_1(source)
        invocation = result["invocations"][0]
        self.assertIsNone(invocation["served_model"])
        self.assertIsNone(invocation["observed_model"])
        self.assertEqual(invocation["identity_assurance"], "requested_only")
        self.assertEqual(invocation["requested_model"], "gpt-5.6-terra")


if __name__ == "__main__":
    unittest.main()
