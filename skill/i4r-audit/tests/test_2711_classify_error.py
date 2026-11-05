"""2.7.11: an HTTP status in a provider error is a token of its own, never three digits inside another token.

run_coordinator.classify_error tested "429"/"502"/"503"/"504" as bare substrings, before its served-model test. A
served-model mismatch names the ids the call was served, and a dated id (claude-sonnet-5-20260503) carries "503", so
a deterministic wrong-model failure was classified transient and the coordinator paid for two more full calls. The
same held for a sha256 in a message. Genuine 5xx and rate-limit failures stay transient.

run_phase1.rejected_provider_request had the same substring test, with "500" too: a pre-inference error envelope whose
text carried a request id such as req_...5004... was journaled as provider_request_status "rejected" instead of
"unknown" (triage N4). The Phase-1 cases run the real run_phase1.py against tests/fake_claude_phase1.py's
opaque_error_once scenario (one error envelope with FAKE_PHASE1_ERROR_MESSAGE and nothing else, then success).
"""
import os
import pathlib
import sys
import unittest
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "tests"))
import run_coordinator  # noqa: E402
import run_phase1  # noqa: E402
import test_run_coordinator as coordinator_tests  # noqa: E402
import test_run_phase1 as phase1_tests  # noqa: E402

DATED_WRONG_MODEL = "claude-sonnet-5-20260503"


class WrongModelIsNotRetried(unittest.TestCase):
    setUp = coordinator_tests.CoordinatorRunnerTests.setUp
    tearDown = coordinator_tests.CoordinatorRunnerTests.tearDown
    run_main = coordinator_tests.CoordinatorRunnerTests.run_main
    ledger = coordinator_tests.CoordinatorRunnerTests.ledger
    upgrade_to_metadata_21 = coordinator_tests.CoordinatorRunnerTests.upgrade_to_metadata_21

    def test_a_dated_wrong_model_is_one_model_failure_not_three_transient_ones(self):
        self.upgrade_to_metadata_21()
        with mock.patch.dict(os.environ, {"FAKE_WRONG_MODEL_ID": DATED_WRONG_MODEL, "RETRY_BACKOFF_SECONDS": "0"}):
            code = self.run_main("wrong_model")
        self.assertEqual(2, code)
        rows = [row for row in self.ledger() if row["role"] == "coordinator"]
        self.assertEqual(["model"], [row["error_class"] for row in rows])
        self.assertIn(DATED_WRONG_MODEL, rows[0]["error"])


class StatusTokens(unittest.TestCase):
    def test_genuine_transient_failures_stay_retryable(self):
        for message in ('API Error: 503 {"type":"error","error":{"type":"overloaded_error"}}', "HTTP 502 Bad Gateway",
                        "upstream status=504", "error code: 429", "provider_5xx", "rate limit exceeded",
                        # an error envelope with no served model: the 503 wins over the served-model line
                        "Claude result envelope reports an error | API Error: 503 | expected exactly one served "
                        "Sonnet 5.5 primary; observed 0: []"):
            with self.subTest(message=message):
                self.assertEqual("transient", run_coordinator.classify_error(message))
                self.assertTrue(run_phase1.retryable(run_phase1.classify_failure(message)))

    def test_digits_inside_an_id_or_a_hash_are_not_a_status(self):
        served = f"expected exactly one served Sonnet 5.5 primary; observed ['{DATED_WRONG_MODEL}']"
        self.assertEqual("model", run_phase1.classify_failure(served))
        self.assertFalse(run_phase1.retryable(run_phase1.classify_failure(served)))
        self.assertEqual("permanent", run_coordinator.classify_error(
            "output hash 9f2c4a503e17 does not match the frozen artifact"))
        # a separator before the digits and a letter after them: the look-ahead half of the boundary
        for message in ("request id 5004ab17", "sha256 503e17aa does not match the frozen artifact",
                        "error id 429c then failed"):
            with self.subTest(message=message):
                self.assertEqual("permanent", run_coordinator.classify_error(message))
                self.assertFalse(run_phase1.retryable(run_phase1.classify_failure(message)))


class Phase1StatusTokens(unittest.TestCase):
    setUp = phase1_tests.Phase1RunnerTests.setUp
    tearDown = phase1_tests.Phase1RunnerTests.tearDown
    make_audit = phase1_tests.Phase1RunnerTests.make_audit
    write_package_manifest = phase1_tests.Phase1RunnerTests.write_package_manifest
    run_runner = phase1_tests.Phase1RunnerTests.run_runner
    ledger = phase1_tests.Phase1RunnerTests.ledger

    def run_opaque(self, name: str, message: str):
        root = self.make_audit(name, metadata21=True)
        result = self.run_runner("opaque_error_once", root=root, state=self.base / f"{name}-state.jsonl",
                                 extra_env={"FAKE_PHASE1_ERROR_MESSAGE": message})
        rows = [row for row in self.ledger(root) if row["role"] == "auditor_reproduction"]
        return result, rows

    def test_a_request_id_holding_500_is_not_a_rejection(self):
        result, rows = self.run_opaque("opaque500", "request failed before inference (request id req_011cv5004ab)")
        self.assertEqual(2, result.returncode, result.stderr + result.stdout)   # deterministic: not retried
        self.assertEqual([("failed", "unknown", "not_started")],
                         [(row["status"], row["provider_request_status"], row["inference_start_status"])
                          for row in rows])
        self.assertFalse(run_phase1.retryable(rows[0]["error_class"]), rows[0]["error_class"])

    def test_a_hash_holding_503_is_not_a_transient_failure(self):
        result, rows = self.run_opaque("opaque503", "request failed before inference (digest 9f2c4a503e17aa)")
        self.assertEqual(2, result.returncode, result.stderr + result.stdout)   # one call, not three
        self.assertEqual([("failed", "unknown")], [(row["status"], row["provider_request_status"]) for row in rows])
        self.assertNotEqual("transient", rows[0]["error_class"])
        self.assertFalse(run_phase1.retryable(rows[0]["error_class"]), rows[0]["error_class"])

    def test_a_genuine_status_is_still_a_retryable_rejection(self):
        # Negative control: the same envelope with a real status token is rejected, transient, retried, completed.
        result, rows = self.run_opaque("opaque502", "HTTP 502 Bad Gateway (request id req_011cv5004ab)")
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertEqual([("failed", "rejected", "transient"), ("completed", "accepted", None)],
                         [(row["status"], row["provider_request_status"], row["error_class"]) for row in rows])

    def test_the_rejection_matcher_reads_status_tokens_only(self):
        def rejected(text: str) -> bool:
            return run_phase1.rejected_provider_request({"is_error": True, "subtype": "error", "result": text}, "")
        for text in ("API Error: 500 Internal Server Error", "status=503", "error code: 429", "HTTP_504",
                     "overloaded_error", "rate limit reached", "authentication failed"):
            with self.subTest(text=text):
                self.assertTrue(rejected(text))
        for text in ("request id req_011cv5004ab", "sha256 9f2c4a5003e1", "served claude-sonnet-5-20260503",
                     "took 1500ms", "request failed",
                     # a separator before the digits and a letter after them (the look-ahead half)
                     "request id 5004ab17", "sha256 503e17aa", "error id 429c"):
            with self.subTest(text=text):
                self.assertFalse(rejected(text))


if __name__ == "__main__":
    unittest.main()
