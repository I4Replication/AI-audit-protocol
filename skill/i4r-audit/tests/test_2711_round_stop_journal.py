"""2.7.11: a critique run that stops without its quorum journals that the round has no driver.

In audit A59, round 1 lost all four critics (every attempt reached the provider ceiling
while the host slept). run_critique.sh wrote ROUND_STATUS.json (outcome failed) and journaled nothing, so run.json
kept the round `running` and the run `running` for hours until an operator logged the failure, and the
deploy check and status.py saw a round in progress. The driver now journals a run-level pause_start
(reason critique_round_failed, bound to the invocation's ROUND_STATUS.json) and the next run of the round journals
the pause_end before it dispatches. The round's state machine is untouched: it stays `running`, and the rerun
reserves, dispatches, reaches quorum and integrates exactly as before.

These tests run the real driver with tests/fake_codex.sh (stats times out on every attempt), then a rerun, on a
metadata-2.1 audit built by the shell suite's own fixture; and a run stopped by TERM while a critic is in flight.
(How status.py shows the pause is the ops change's, tested there.)
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
DRIVER = SCRIPTS / "run_critique.sh"
HELPER = SCRIPTS / "test_run_critique_runtime.py"
FAKE_CODEX = TESTS / "fake_codex.sh"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import lifecycle_journal  # noqa: E402
import test_snapshot_mode_drift as drift  # noqa: E402  (write_critique_audit: the shell suite's metadata-2.1 fixture)

REASON = "critique_round_failed"


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RoundStopJournal(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="i4r-2711-stop-")
        cls.base = pathlib.Path(cls.temporary.name).resolve()
        cls.audit = cls.base / "runs" / "J" / "P" / "audit"
        cls.audit.parent.mkdir(parents=True)
        drift.write_critique_audit(cls.audit)
        cls.failed = cls.drive("stop-1", "timeout")
        cls.after_failure = json.loads((cls.audit / "run.json").read_text())
        cls.events_after_failure = lifecycle_journal.load_events(cls.audit)
        cls.stopped = cls.base / "stopped" / "audit"   # the audit as the failed run left it, for the helper tests
        shutil.copytree(cls.audit, cls.stopped, symlinks=True)
        # a run refused before it dispatches (here: a CRITICS override that round 1 refuses at routing) journals nothing
        cls.journal_before_refusal = (cls.audit / "run.events.jsonl").read_bytes()
        cls.refused = cls.drive("stop-refused", "success", CRITICS="stats data fairness")
        cls.journal_after_refusal = (cls.audit / "run.events.jsonl").read_bytes()
        cls.rerun = cls.drive("stop-2", "success")

    @classmethod
    def tearDownClass(cls) -> None:
        drift.thaw(cls.base)
        cls.temporary.cleanup()

    @classmethod
    def drive(cls, invocation: str, scenario: str, **extra: str) -> subprocess.CompletedProcess[str]:
        env = drift.env_without_refreeze(
            AUDIT_DIR=str(cls.audit), CODEX=str(FAKE_CODEX), FAKE_CODEX_STATE=str(cls.base / "fake_state"),
            FAKE_CODEX_SCENARIO=scenario, I4R_INVOCATION_ID=invocation, RETRY_BACKOFF_SECONDS="0",
            PROBE_LOCK_WAIT_SECONDS="3", **extra)
        return subprocess.run(["bash", str(DRIVER), "1"], capture_output=True, text=True, env=env, timeout=900,
                              check=False)

    def stops(self, events: list[dict]) -> list[dict]:
        return [e for e in events if e["type"] == "pause_start" and e["payload"].get("reason") == REASON]

    def test_a_quorum_failure_journals_a_bound_pause(self) -> None:
        self.assertEqual(self.failed.returncode, 1, self.failed.stderr[-3000:])
        receipt = self.audit / "critique/round_1/invocations/stop-1/ROUND_STATUS.json"
        status = json.loads(receipt.read_text())
        self.assertEqual((status["outcome"], status["quorum_met"]), ("failed", False))
        self.assertTrue(status["detail"].startswith("exact role quorum failed; missing: stats"), status["detail"])
        stops = self.stops(self.events_after_failure)
        self.assertEqual(len(stops), 1, "the failed run journals exactly one pause")
        payload = stops[0]["payload"]
        self.assertEqual((payload["round"], payload["phase"], payload["driver_invocation"], payload["quorum_met"]),
                         (1, "critique", "stop-1", False))
        self.assertEqual(payload["failed_roles"], ["critic_stats"])
        self.assertEqual(payload["receipt"], "critique/round_1/invocations/stop-1/ROUND_STATUS.json")
        self.assertEqual(payload["receipt_sha256"], sha(receipt))
        self.assertEqual(stops[0]["source"], "run_critique.sh")
        self.assertEqual(self.events_after_failure[-1]["type"], "pause_start", "nothing is journaled after it")

    def test_run_json_shows_no_driver_and_the_round_state_machine_is_untouched(self) -> None:
        run = self.after_failure
        self.assertEqual((run["status"], run["pause_reason"]), ("paused", REASON))
        round_one = next(row for row in run["rounds"] if row["round"] == 1)
        self.assertEqual(round_one["state"], "running")
        self.assertEqual(lifecycle_journal._round_state_from_events(self.events_after_failure, 1), "running")
        # the projection is a valid run.json while the round waits for its rerun
        path = self.base / "run_after_failure.json"
        path.write_text(json.dumps(run))
        checked = subprocess.run([sys.executable, str(SCRIPTS / "validate_json_schema.py"), str(path),
                                  str(SKILL / "schemas/run.schema.json")], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_a_run_refused_before_it_dispatches_journals_nothing(self) -> None:
        self.assertNotEqual(self.refused.returncode, 0, self.refused.stderr[-2000:])
        self.assertEqual(self.journal_after_refusal, self.journal_before_refusal)

    def test_the_rerun_ends_the_pause_before_it_dispatches_and_integrates(self) -> None:
        self.assertEqual(self.rerun.returncode, 0, self.rerun.stderr[-3000:])
        events = lifecycle_journal.load_events(self.audit)
        ends = [e for e in events if e["type"] == "pause_end" and e["payload"].get("reason") == REASON]
        self.assertEqual(len(ends), 1)
        self.assertEqual((ends[0]["payload"]["driver_invocation"], ends[0]["payload"]["stopped_by"],
                          ends[0]["payload"]["resume_status"]), ("stop-2", "stop-1", "running"))
        later = events[ends[0]["seq"]:]
        first_request = next(e for e in later if e["type"] == "provider_request")
        self.assertLess(ends[0]["seq"], first_request["seq"])
        self.assertTrue(any(e["type"] == "round_quorum_complete" for e in later))
        self.assertTrue(any(e["type"] == "round_integrated" for e in later))
        self.assertFalse(any(e["type"] == "round_start" for e in later), "the rerun did not reopen the round")
        run = json.loads((self.audit / "run.json").read_text())
        self.assertIsNone(run["pause_reason"])
        self.assertEqual(next(r for r in run["rounds"] if r["round"] == 1)["state"], "integrated")
        # the stretch between the stop and the rerun is booked as paused, not idle (a closed interval)
        stop_at = lifecycle_journal.parse_time(self.stops(events)[0]["at"])
        end_at = lifecycle_journal.parse_time(ends[0]["at"])
        metrics = json.loads((self.audit / "run_metrics.json").read_text())
        self.assertGreaterEqual(metrics["durations"]["paused_seconds"], (end_at - stop_at).total_seconds())

    def test_the_helper_journals_nothing_for_a_finished_round_or_a_second_stop(self) -> None:
        work = self.base / "copy"
        shutil.copytree(self.audit, work / "audit", symlinks=True)
        audit = work / "audit"
        drift.thaw(audit)
        before = (audit / "run.events.jsonl").read_bytes()
        fabricated = audit / "critique/round_1/invocations/late/ROUND_STATUS.json"
        fabricated.parent.mkdir(parents=True)
        fabricated.write_text(json.dumps({"outcome": "failed", "round": 1, "invocation_id": "late",
                                          "quorum_met": False, "completed_roles": [], "failed_roles": ["stats"],
                                          "exit_code": 1, "ended_at": None}))
        result = subprocess.run([sys.executable, str(HELPER), "round-stopped", "--workspace", str(audit),
                                 "--round", "1", "--invocation", "late", "--status", str(fabricated)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["journaled"])
        self.assertEqual((audit / "run.events.jsonl").read_bytes(), before, "a finished round is never paused")

    # review DT-2: the two guards the round-stop helpers promise, each on the audit as the failed run left it

    def stopped_copy(self, name: str) -> pathlib.Path:
        audit = self.base / name / "audit"
        shutil.copytree(self.stopped, audit, symlinks=True)
        drift.thaw(audit)
        return audit

    def helper(self, audit: pathlib.Path, command: str, invocation: str, *extra: str) -> dict:
        result = subprocess.run([sys.executable, str(HELPER), command, "--workspace", str(audit), "--round", "1",
                                 "--invocation", invocation, *extra], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def failed_status(self, audit: pathlib.Path, invocation: str) -> pathlib.Path:
        path = audit / f"critique/round_1/invocations/{invocation}/ROUND_STATUS.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"outcome": "failed", "round": 1, "invocation_id": invocation, "quorum_met": False,
                                    "completed_roles": [], "failed_roles": ["stats"], "exit_code": 1,
                                    "ended_at": None}))
        return path

    def log_event(self, audit: pathlib.Path, *fields: str) -> None:
        result = subprocess.run(["bash", str(SCRIPTS / "log_event.sh"), *fields], capture_output=True, text=True,
                                env={**os.environ, "RUN_JSON": str(audit / "run.json")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_second_stop_while_the_first_is_open_journals_nothing(self) -> None:
        audit = self.stopped_copy("second-stop")
        self.assertEqual(len(self.stops(lifecycle_journal.load_events(audit))), 1)
        before = (audit / "run.events.jsonl").read_bytes()
        answer = self.helper(audit, "round-stopped", "stop-1b", "--status", str(self.failed_status(audit, "stop-1b")))
        self.assertFalse(answer["journaled"], answer)
        self.assertEqual((audit / "run.events.jsonl").read_bytes(), before, "one stop, one pause")
        # the same once an operator's pause came and went: the run reads running again, the stop is still open
        self.log_event(audit, "pause_start", "reason=operator_pause")
        self.log_event(audit, "pause_end", "reason=operator_pause", "resume_status=running")
        self.assertEqual(json.loads((audit / "run.json").read_text())["status"], "running")
        before = (audit / "run.events.jsonl").read_bytes()
        answer = self.helper(audit, "round-stopped", "stop-1c", "--status", str(self.failed_status(audit, "stop-1c")))
        self.assertFalse(answer["journaled"], answer)
        self.assertEqual((audit / "run.events.jsonl").read_bytes(), before)

    def test_a_rerun_leaves_an_operators_later_pause_alone(self) -> None:
        audit = self.stopped_copy("operator-pause")
        self.log_event(audit, "pause_start", "reason=operator_pause")
        before = (audit / "run.events.jsonl").read_bytes()
        answer = self.helper(audit, "round-resumed", "stop-2b")
        self.assertFalse(answer["journaled"], answer)
        self.assertEqual((audit / "run.events.jsonl").read_bytes(), before)
        run = json.loads((audit / "run.json").read_text())
        self.assertEqual((run["status"], run["pause_reason"]), ("paused", "operator_pause"))


class InterruptedRoundJournal(unittest.TestCase):
    """A driver stopped by TERM while a critic call is in flight journals the same bound pause (the A59 case
    was a quorum failure; an operator's Ctrl-C or a killed batch leaves the same `running` round behind)."""

    def test_a_term_during_a_critic_call_journals_the_pause(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-2711-term-") as name:
            base = pathlib.Path(name).resolve()
            audit = base / "audit"
            drift.write_critique_audit(audit)
            state = base / "fake_state"
            env = drift.env_without_refreeze(
                AUDIT_DIR=str(audit), CODEX=str(FAKE_CODEX), FAKE_CODEX_STATE=str(state),
                FAKE_CODEX_SCENARIO="slow", FAKE_SLOW_ROLE="stats", I4R_INVOCATION_ID="term-1",
                RETRY_BACKOFF_SECONDS="0", PROBE_LOCK_WAIT_SECONDS="3")
            try:
                driver = subprocess.Popen(["bash", str(DRIVER), "1"], stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True, env=env)
                for _ in range(1200):
                    if (state / "slow_started").exists() or driver.poll() is not None:
                        break
                    time.sleep(0.1)
                self.assertTrue((state / "slow_started").exists(), "the slow critic never started")
                driver.send_signal(signal.SIGTERM)
                _out, err = driver.communicate(timeout=120)
                self.assertEqual(driver.returncode, 143, err[-3000:])
                status = json.loads((audit / "critique/round_1/invocations/term-1/ROUND_STATUS.json").read_text())
                self.assertEqual((status["outcome"], status["detail"]), ("failed", "interrupted by TERM"))
                events = lifecycle_journal.load_events(audit)
                stops = [e for e in events if e["type"] == "pause_start" and e["payload"].get("reason") == REASON]
                self.assertEqual(len(stops), 1, [e["type"] for e in events][-8:])
                self.assertEqual(stops[0]["payload"]["driver_invocation"], "term-1")
                self.assertNotIn("critic_stats", stops[0]["payload"]["completed_roles"])
                run = json.loads((audit / "run.json").read_text())
                self.assertEqual((run["status"], run["pause_reason"]), ("paused", REASON))
            finally:
                drift.thaw(base)


if __name__ == "__main__":
    unittest.main()
