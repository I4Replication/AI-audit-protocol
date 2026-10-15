from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import sys
import unittest


SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "summarize_invocations", SCRIPTS / "summarize_invocations.py"
)
assert spec and spec.loader
summarize_invocations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summarize_invocations)


class HonestInvocationTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider_total = json.loads(
            (SKILL / "schemas/fixtures/v2_invocation_record.json").read_text()
        )
        self.derived = copy.deepcopy(self.provider_total)
        self.derived.update({
            "invocation_id": "inv-claude-derived",
            "requested_model": "sonnet-5",
            "observed_model": "claude-sonnet-5",
            "observed_model_ids": ["claude-sonnet-5", "claude-haiku-4-5"],
            "model_family": "claude",
            "reasoning_effort": "max",
        })
        self.derived["usage"] = {
            "obtainable": True,
            "input_tokens": 2,
            "output_tokens": 163,
            "cached_input_tokens": 30597,
            "cache_creation_input_tokens": 10876,
            "reasoning_tokens": None,
            "total_tokens": 41638,
            "source": "cli_log",
            "unavailable_reasons": {
                "reasoning_tokens": "provider CLI does not expose separate reasoning tokens",
            },
            "total_tokens_kind": "derived",
            "total_tokens_components": [
                "input_tokens", "cached_input_tokens",
                "cache_creation_input_tokens", "output_tokens",
            ],
            "total_tokens_unavailable_reason": None,
        }
        self.failed = copy.deepcopy(self.provider_total)
        self.failed.update({
            "invocation_id": "inv-failed-before-usage",
            "status": "failed",
            "error": "transport failed before usage was returned",
            "error_class": "transport",
        })
        self.failed["usage"] = {
            "obtainable": False,
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "source": "unavailable",
        }

    def test_aggregate_separates_provider_derived_and_unavailable_totals(self) -> None:
        summary = summarize_invocations.aggregate(
            [self.provider_total, self.derived, self.failed]
        )
        self.assertEqual(summary["coverage"]["tokens"], 1)
        self.assertEqual(summary["numeric_token_coverage"]["reasoning_tokens"], 1 / 3)
        self.assertEqual(summary["numeric_token_coverage"]["total_tokens"], 2 / 3)
        self.assertEqual(summary["tokens"]["reasoning_tokens"], 10)
        self.assertEqual(summary["tokens"]["cache_creation_input_tokens"], 10876)
        basis = summary["total_token_basis"]
        self.assertEqual(basis["provider_reported"], {"calls": 1, "sum": 120})
        self.assertEqual(basis["derived"], {"calls": 1, "sum": 41638})
        self.assertEqual(basis["unavailable"], {"calls": 1, "sum": None})
        self.assertEqual(basis["accounted"], {"calls": 2, "sum": 41758})
        rendered = summarize_invocations.markdown(summary)
        self.assertIn("10 (1/2)", rendered)
        self.assertIn("— (0/1)", rendered)

    def test_unlabelled_partial_usage_remains_release_blocking(self) -> None:
        partial = copy.deepcopy(self.provider_total)
        partial["usage"]["reasoning_tokens"] = None
        summary = summarize_invocations.aggregate([partial])
        self.assertEqual(summary["coverage"]["tokens"], 0)
        self.assertIn("token disposition telemetry covers 0/1 provider calls", summary["errors"])

    def test_preinference_failure_truthfully_accounts_for_unavailable_served_model(self) -> None:
        rejected = copy.deepcopy(self.failed)
        rejected.update({
            "invocation_id": "inv-schema-rejected-before-inference",
            "observed_model": None,
            "model_source": "unavailable",
            "session_id": None,
            "error": "provider rejected invalid_json_schema before inference",
            "error_class": "schema",
        })
        rejected.pop("observed_model_ids", None)
        summary = summarize_invocations.aggregate([rejected])
        self.assertEqual(summary["coverage"]["model_identity_disposition"], 1)
        self.assertEqual(summary["coverage"]["observed_model"], 0)
        self.assertNotIn(
            "model identity disposition covers 0/1 provider calls", summary["errors"]
        )


if __name__ == "__main__":
    unittest.main()
