"""2.7.4: the Claude CLI's per-response output cap (prototype regression test).

The first live Sonnet 5.5/max coordinator call (20260930-audit-0ccfcb4e, attempt 1) failed after 33
minutes with "API Error: Claude's response exceeded the 64000 output token maximum ...": Claude CLI
2.1.206 sends max_tokens = its catalog default (64,000) unless CLAUDE_CODE_MAX_OUTPUT_TOKENS says
otherwise, and the runner classed the failure `permanent`, so nothing retried it. These tests build that
call with the fake CLI (the attempt-1 envelope's shape) and run the real runners.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "tests"))
sys.path.insert(0, str(SKILL / "schemas" / "tools"))
import claude_routes  # noqa: E402
import record_invocation  # noqa: E402
import validate_json_schema  # noqa: E402
import test_run_coordinator as coordinator_tests  # noqa: E402
import test_run_phase1 as phase1_tests  # noqa: E402

CAP = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"


def without_cap(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != CAP}
    env.update(extra)
    return env


class CoordinatorOutputCap(coordinator_tests.CoordinatorRunnerTests):
    """The coordinator fixture, re-used; only the tests below run (the parent's are masked)."""

    def run_cap(self, scenario: str, operator_cap: str | None = None):
        state = self.root.parent / "fake-state.txt"
        self.env_log = self.root.parent / "fake-env.jsonl"
        env = without_cap(FAKE_COORDINATOR_STATE=str(state), FAKE_COORDINATOR_ENV_LOG=str(self.env_log),
                          RETRY_BACKOFF_SECONDS="0")
        if operator_cap is not None:
            env[CAP] = operator_cap
        with mock.patch.dict(os.environ, env, clear=True):
            return self.run_runner(scenario)

    def caps_seen(self) -> list[str | None]:
        return [json.loads(line)[CAP] for line in self.env_log.read_text().splitlines() if line]

    def coordinator_rows(self) -> list[dict]:
        return [row for row in self.ledger() if row["role"] == "coordinator"]

    def test_overflow_is_output_limit_and_retried_once_in_the_same_run(self) -> None:
        self.assertEqual(self.route["requested_model"], "sonnet-5-5")
        self.upgrade_to_metadata_21()
        result = self.run_cap("output_cap_then_success")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = self.coordinator_rows()
        self.assertEqual([row["status"] for row in rows], ["failed", "completed"])
        self.assertEqual(rows[0]["error_class"], "output_limit")
        self.assertEqual(rows[0]["provider_request_status"], "accepted")
        self.assertEqual([row["attempt"] for row in rows], [1, 2])
        self.assertIn("failed with a output_limit error", result.stderr)
        # The failed row is a valid record under the invocation-record contract.
        errors = validate_json_schema.errors_for_value(
            rows[0], SKILL / "schemas/v2/invocation-record.schema.json")
        self.assertEqual(errors, [])

    def test_a_second_overflow_stops_after_one_retry(self) -> None:
        self.upgrade_to_metadata_21()
        result = self.run_cap("output_cap_always")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        rows = self.coordinator_rows()
        self.assertEqual([row["error_class"] for row in rows], ["output_limit", "output_limit"])
        self.assertEqual(len(list((self.root / "coordinator/invocations").glob("*/*"))), 2)

    def test_sonnet_5_5_spawns_with_the_packaged_cap_whatever_the_operator_exported(self) -> None:
        for operator_cap in (None, "32000", "128000"):
            with self.subTest(operator_cap=operator_cap):
                self.tearDown(); self.setUp()
                result = self.run_cap("output_cap_then_success", operator_cap)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertEqual(self.caps_seen(), ["128000", "128000"])


class LegacyRouteCoordinator(CoordinatorOutputCap):
    """A Sonnet 5 audit keeps the 2.7.3 spawn environment: the operator's, cap included or not."""

    def setUp(self) -> None:
        policy = json.loads((SKILL / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))
        for row in (policy["models"]["coordinator"], *policy["models"]["phase_1_auditors"].values(),
                    policy["models"]["post_fan_out_verifier"]):
            row.update(claude_routes.SONNET_5)
        import tempfile
        self._policy_dir = tempfile.TemporaryDirectory()
        path = pathlib.Path(self._policy_dir.name) / "policy.json"
        path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        with mock.patch.object(record_invocation, "POLICY", path):
            super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        self._policy_dir.cleanup()

    def test_sonnet_5_spawn_env_is_the_operators(self) -> None:
        self.assertEqual(self.route["requested_model"], "sonnet-5")
        for operator_cap, expected in ((None, None), ("100000", "100000")):
            with self.subTest(operator_cap=operator_cap):
                self.tearDown(); self.setUp()
                result = self.run_cap("output_cap_then_success", operator_cap)
                self.assertEqual(self.caps_seen()[0], expected)


class Phase1OutputCap(phase1_tests.Phase1RunnerTests):
    def test_role_overflow_is_output_limit_retried_once_with_the_cap(self) -> None:
        with mock.patch.dict(os.environ, without_cap(), clear=True):
            result = self.run_runner("output_cap_once")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = [row for row in self.ledger() if row["role"] == "auditor_reproduction"]
        self.assertEqual([row["status"] for row in rows], ["failed", "completed"])
        self.assertEqual(rows[0]["error_class"], "output_limit")
        caps = {row["output_cap"] for row in self.starts()}
        self.assertEqual(caps, {"128000"})  # every Sonnet 5.5 spawn, the probe included

    def test_role_second_overflow_stops_the_role(self) -> None:
        with mock.patch.dict(os.environ, without_cap(), clear=True):
            result = self.run_runner("output_cap_always")
        self.assertNotEqual(result.returncode, 0)
        rows = [row for row in self.ledger() if row["role"] == "auditor_reproduction"]
        self.assertEqual([row["error_class"] for row in rows], ["output_limit", "output_limit"])


def load_tests(loader, tests, pattern):
    """Only the tests defined here; the inherited fixtures' own tests run in their own files."""
    suite = unittest.TestSuite()
    for cls in (CoordinatorOutputCap, LegacyRouteCoordinator, Phase1OutputCap):
        for name in cls.__dict__:
            if name.startswith("test_"):
                suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
