"""2.7.11: an audit initialised on 2.7.11 records the Claude CLI's thinking count as reasoning_tokens; an older audit's
parse stays 2.7.10's byte for byte.

The CLI writes thinking as modelUsage.<id>.thinkingTokens (and usage.output_tokens_details.thinking_tokens); every
Claude usage parser looked for reasoningTokens, which the CLI never writes, so 960 of 1,048 real envelopes had their
thinking dropped. The choice is frozen per audit (metadata.claude_reasoning_tokens, scripts/claude_thinking_tokens.py).
Each test runs the shipped runner against a fake CLI that writes the real fields.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SKILL / "tests"))
sys.path.insert(0, str(SKILL / "schemas" / "tools"))
import claude_thinking_tokens  # noqa: E402
import record_invocation  # noqa: E402
import run_coordinator  # noqa: E402
import run_phase1  # noqa: E402
import validate_v2  # noqa: E402
import test_run_coordinator as coordinator_tests  # noqa: E402
import test_run_phase1 as phase1_tests  # noqa: E402

LEGACY_REASON = "Claude CLI response did not expose this token component"


def drop_leaf(root: pathlib.Path, value: str | None = None) -> None:
    """Make the audit's snapshot one frozen before 2.7.11 (no leaf), or one freezing ``value``, re-bound as init
    binds it."""
    path = root / "pipeline-policy.snapshot.json"
    envelope = json.loads(path.read_text())
    envelope["policy"]["metadata"].pop("claude_reasoning_tokens")
    if value is not None:
        envelope["policy"]["metadata"]["claude_reasoning_tokens"] = value
    envelope["policy_sha256"] = record_invocation.policy_hash(envelope["policy"])
    path.chmod(0o644)
    path.write_text(json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    run = json.loads((root / "run.json").read_text())
    run["toolkit"]["pipeline_policy_snapshot_sha256"] = coordinator_tests.digest(path)
    (root / "run.json").write_text(json.dumps(run) + "\n")


class CoordinatorThinking(unittest.TestCase):
    setUp = coordinator_tests.CoordinatorRunnerTests.setUp
    tearDown = coordinator_tests.CoordinatorRunnerTests.tearDown
    run_runner = coordinator_tests.CoordinatorRunnerTests.run_runner
    ledger = coordinator_tests.CoordinatorRunnerTests.ledger
    upgrade_to_metadata_21 = coordinator_tests.CoordinatorRunnerTests.upgrade_to_metadata_21

    def coordinator_row(self) -> dict:
        rows = [row for row in self.ledger() if row["role"] == "coordinator"]
        self.assertEqual(1, len(rows))
        return rows[0]

    def test_a_new_audit_records_thinking_as_reasoning_tokens(self):
        self.assertTrue(claude_thinking_tokens.frozen(self.root), "the shipped policy must freeze the leaf")
        self.upgrade_to_metadata_21()
        result = self.run_runner("thinking")
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        row = self.coordinator_row()
        self.assertEqual(31, row["usage"]["reasoning_tokens"])          # 30 primary + 1 helper
        self.assertNotIn("reasoning_tokens", row["usage"]["unavailable_reasons"])
        envelope = json.loads(next((self.root / "coordinator/invocations").glob("*/*/claude_stdout.json"))
                              .read_text())
        legacy, _cache = run_coordinator.extract_usage(envelope)
        self.assertEqual(legacy["total_tokens"], row["usage"]["total_tokens"])   # inside output: never added
        self.assertEqual(legacy["total_tokens_components"], row["usage"]["total_tokens_components"])
        self.assertEqual({part["model"]: part["usage"]["reasoning_tokens"] for part in row["usage_by_model"]},
                         {self.served: 30, coordinator_tests.fake_claude_coordinator.HELPER: 1})
        validate_v2.validate_invocation(row)

    def test_an_audit_frozen_before_2711_keeps_the_2710_parse(self):
        drop_leaf(self.root)
        self.assertFalse(claude_thinking_tokens.frozen(self.root))
        self.upgrade_to_metadata_21()
        result = self.run_runner("thinking")
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        row = self.coordinator_row()
        self.assertIsNone(row["usage"]["reasoning_tokens"])
        self.assertEqual(LEGACY_REASON, row["usage"]["unavailable_reasons"]["reasoning_tokens"])
        envelope = json.loads(next((self.root / "coordinator/invocations").glob("*/*/claude_stdout.json"))
                              .read_text())
        self.assertEqual(row["usage"], run_coordinator.extract_usage(envelope)[0])   # the 2.7.10 call, unchanged

    def test_a_value_the_parse_cannot_honour_is_refused_before_the_call(self):
        drop_leaf(self.root, "reasoningTokens")
        result = self.run_runner("thinking")
        self.assertEqual(1, result.returncode, result.stderr + result.stdout)
        self.assertIn("metadata.claude_reasoning_tokens is 'reasoningTokens'", result.stderr)
        self.assertEqual([], [row for row in self.ledger() if row["role"] == "coordinator"])
        self.assertFalse((self.root / "coordinator/invocations").exists() and
                         any((self.root / "coordinator/invocations").rglob("claude_stdout.json")))

    def test_the_top_level_view_is_read_when_no_model_partition_exists(self):
        usage, _cache = run_coordinator.extract_usage(
            {"usage": {"input_tokens": 1, "output_tokens": 9, "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 0, "output_tokens_details": {"thinking_tokens": 4}}},
            thinking=True)
        self.assertEqual(4, usage["reasoning_tokens"])


class Phase1Thinking(unittest.TestCase):
    setUp = phase1_tests.Phase1RunnerTests.setUp
    tearDown = phase1_tests.Phase1RunnerTests.tearDown
    make_audit = phase1_tests.Phase1RunnerTests.make_audit
    write_package_manifest = phase1_tests.Phase1RunnerTests.write_package_manifest
    run_runner = phase1_tests.Phase1RunnerTests.run_runner
    ledger = phase1_tests.Phase1RunnerTests.ledger
    events = phase1_tests.Phase1RunnerTests.events
    starts = phase1_tests.Phase1RunnerTests.starts

    def run_with_thinking(self, root: pathlib.Path) -> list[dict]:
        result = self.run_runner(root=root, extra_env={"FAKE_PHASE1_THINKING": "7"})
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        return [row for row in self.ledger(root) if row.get("provider") == "anthropic" and row.get("provider_call")]

    def test_a_new_audit_records_thinking_for_the_probe_and_every_auditor(self):
        root = self.make_audit("thinking-new", metadata21=True)
        rows = self.run_with_thinking(root)
        self.assertEqual(6, len(rows))                                  # one probe, five auditors
        for row in rows:
            self.assertEqual(7, row["usage"]["reasoning_tokens"], row["role"])
            self.assertEqual([7], [part["usage"]["reasoning_tokens"] for part in row["usage_by_model"]])
            self.assertIsNone(row["unattributed_usage"], row["role"])   # fully attributed, no zero bucket
            validate_v2.validate_invocation(row)

    def test_a_received_attempt_recovered_after_a_crash_records_thinking(self):
        # recover_received_role_attempt (shared with run_verifier) re-parses a received attempt that has no ledger
        # row: a runner that died after the response landed and before the terminal record
        root = self.make_audit("thinking-recover")
        thinking = {"FAKE_PHASE1_THINKING": "7"}
        state = self.base / "thinking-recover-state.jsonl"
        first = self.run_runner(root=root, state=state, extra_env=thinking)
        self.assertEqual(0, first.returncode, first.stderr + first.stdout)
        role = "auditor_reproduction"
        completion = next(row for row in json.loads(first.stdout)["roles"] if row["role"] == role)
        record = next(row for row in self.ledger(root) if row["invocation_id"] == completion["invocation_id"])
        self.assertEqual(7, record["usage"]["reasoning_tokens"])
        attempt = next((root / "phase1/roles" / role / completion["input_sha256"]).glob("attempt_*"))
        (attempt / "status.json").write_text(json.dumps({
            "schema_version": "i4r-phase1-attempt-1", "state": "response_received",
            "invocation_id": completion["invocation_id"], "attempt_kind": "role_inference", "attempt": 1,
            "provider_request_status": "accepted", "inference_start_status": "started", "returncode": 0,
            "timed_out": False, "started_at": record["started_at"], "ended_at": record["ended_at"],
            "stdout_sha256": phase1_tests.digest(attempt / "claude_stdout.json"),
            "stderr_sha256": phase1_tests.digest(attempt / "claude_stderr.txt"),
        }, indent=2) + "\n", encoding="utf-8")
        (root / "invocations.jsonl").write_text("".join(
            json.dumps(row, sort_keys=True) + "\n" for row in self.ledger(root)
            if row["invocation_id"] != completion["invocation_id"]), encoding="utf-8")
        (root / "phase1/completions" / role / f"{completion['input_sha256']}.json").unlink()
        (root / completion["report"]).unlink()
        shutil.rmtree((root / completion["evidence"]).parent)
        calls = len(self.starts(state))
        second = self.run_runner(root=root, state=state, extra_env=thinking)
        self.assertEqual(0, second.returncode, second.stderr + second.stdout)
        self.assertEqual(calls, len(self.starts(state)), "no new provider call")
        recovered = [row for row in self.ledger(root) if row["role"] == role]
        self.assertEqual([completion["invocation_id"]], [row["invocation_id"] for row in recovered])
        self.assertEqual(7, recovered[0]["usage"]["reasoning_tokens"])
        self.assertNotIn("reasoning_tokens", recovered[0]["usage"]["unavailable_reasons"])

    def test_a_value_the_parse_cannot_honour_is_refused_before_any_call(self):
        root = self.make_audit("thinking-unknown")
        drop_leaf(root, "reasoningTokens")
        state = self.base / "thinking-unknown-state.jsonl"
        result = self.run_runner(root=root, state=state, extra_env={"FAKE_PHASE1_THINKING": "7"})
        self.assertNotEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("metadata.claude_reasoning_tokens is 'reasoningTokens'", result.stderr + result.stdout)
        self.assertFalse(state.exists() and state.read_text().strip(), "no provider call was made")
        self.assertEqual([], self.ledger(root))

    def test_an_audit_frozen_before_2711_keeps_the_2710_rows(self):
        root = self.make_audit("thinking-legacy")
        drop_leaf(root)                                   # before the journal binds the snapshot, as at init
        phase1_tests.lifecycle_journal.initialize(root)
        rows = self.run_with_thinking(root)
        self.assertEqual(6, len(rows))
        for row in rows:
            self.assertIsNone(row["usage"]["reasoning_tokens"], row["role"])
            # 2.7.10's shape, zero bucket included: reasoning unattributed because the aggregate omitted it
            self.assertEqual({"reasoning_tokens": "invocation aggregate omitted this component"},
                             row["unattributed_usage"]["unavailable_reasons"], row["role"])


class FrozenRule(unittest.TestCase):
    def test_only_the_packaged_value_or_no_leaf_is_admitted(self):
        self.assertFalse(claude_thinking_tokens.rule_of({"metadata": {}}))
        self.assertTrue(claude_thinking_tokens.rule_of({"metadata": {"claude_reasoning_tokens": "thinking_tokens"}}))
        with self.assertRaises(claude_thinking_tokens.RuleError):
            claude_thinking_tokens.rule_of({"metadata": {"claude_reasoning_tokens": "reasoningTokens"}})
        shipped = json.loads((SKILL / "config/pipeline-policy-v2.json").read_text())
        self.assertTrue(claude_thinking_tokens.rule_of(shipped))

    def test_init_refuses_a_leftover_frozen_before_2711_and_freezes_the_leaf_after_its_rm(self):
        import tempfile
        import test_270_policy_and_init as pinit
        with tempfile.TemporaryDirectory(prefix="i4r-2711-leftover-") as name:
            audit = pinit.make_audit(pathlib.Path(name) / "audit")
            policy = pinit.shipped_policy()
            policy["metadata"].pop("claude_reasoning_tokens")          # what 2.7.10 froze
            data = pinit.envelope_bytes(policy)
            (audit / pinit.SNAPSHOT).write_bytes(data)
            result = pinit.init(audit)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("predates the Claude reasoning-token parse", result.stderr)
            self.assertEqual(data, (audit / pinit.SNAPSHOT).read_bytes())
            self.assertFalse((audit / "run.json").exists())
            (audit / pinit.SNAPSHOT).unlink()                          # the remedy the refusal names
            again = pinit.init(audit)
            self.assertEqual(0, again.returncode, again.stdout + again.stderr)
            self.assertTrue(claude_thinking_tokens.frozen(audit))


if __name__ == "__main__":
    unittest.main()
