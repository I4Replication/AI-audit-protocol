"""2.7.11: the critique per-call ceiling counts the time the host is awake, not the time it slept.

run_critique.sh stopped a provider call once `date +%s` passed start + ceiling. The wall clock runs on while
the host sleeps, so a batch audit whose Mac hibernated minutes into a targeted-adjudicator
call had that call stopped one second after the wake, hours later, journaled as a timeout of many hours, and
one of its three attempts spent. These tests drive the real driver with the fake Codex. A `date` on PATH reads
FAKE_SLEPT_SECONDS ahead while the slow call runs (from the fake Codex's slow_started marker to its
slow_finished marker), which is what a sleep looks like to the wall clock; the call itself takes a few seconds.
The negative control keeps a genuinely hung call stopped at the ceiling.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
FAKE = SKILL / "tests" / "fake_codex.sh"

FAKE_DATE = textwrap.dedent("""\
    #!/bin/bash
    # the wall clock of a host that slept FAKE_SLEPT_SECONDS while the fake Codex's slow call was running
    off=0
    if [ -f "$FAKE_CODEX_STATE/slow_started" ] && [ ! -f "$FAKE_CODEX_STATE/slow_finished" ]; then
      off=${FAKE_SLEPT_SECONDS:-0}
    fi
    [ "$off" -ne 0 ] || exec /bin/date "$@"
    now=$(( $(/bin/date +%s) + off ))
    if /bin/date -r 0 +%s >/dev/null 2>&1; then exec /bin/date -r "$now" "$@"; else exec /bin/date -d "@$now" "$@"; fi
""")

# review NC-2: a `date` that fails ONCE while the slow call runs, as a command substitution does when fork gives
# EAGAIN. Its empty output must not be kept as the last poll: read as 0, the next gap was the whole epoch, the
# deadline moved out by decades, and a hung call ran to its end.
FAILING_DATE = textwrap.dedent("""\
    #!/bin/bash
    if [ "$1" = "+%s" ] && [ -f "$FAKE_CODEX_STATE/slow_started" ] && [ ! -f "$FAKE_CODEX_STATE/slow_finished" ] \\
       && [ ! -f "$FAKE_CODEX_STATE/date_failed" ]; then
      : > "$FAKE_CODEX_STATE/date_failed"; echo "fork: Resource temporarily unavailable" >&2; exit 1
    fi
    exec /bin/date "$@"
""")


def make_audit(audit: pathlib.Path, audit_id: str) -> None:
    """The fixture tests/test_run_critique.sh builds (make_audit), then init_run.sh."""
    (audit / "report").mkdir(parents=True)
    (audit / "work").mkdir()
    (audit / "PAPER.txt").write_text("Example et al. (2026), deterministic runtime fixture.\n")
    (audit / "ENV.txt").write_text("python3 standard-library-only fixture\n")
    (audit / "SLOW.txt").write_text("Do not run any long job.\n")
    (audit / "WRAPUP.txt").write_text("Review the current changed claims and their dependencies.\n")
    report = audit / "report" / "REPORT_final.md"
    report.write_text("# Fixture report\n\nThe immutable audit draft contains one bounded claim.\n")
    import hashlib
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    row = {
        "schema_version": "2.0.0", "id": "F1", "claim": "The bounded fixture claim is correct.",
        "domain": "statistics", "source_location": {"path": "report/REPORT_final.md", "locator": "fixture claim"},
        "claim_ref": "fixture claim", "status": "draft",
        "ledger_disposition": {"status": "INCLUDED", "target": None, "reason": None},
        "coordinator_approved": False, "impact": "value_wrong", "impact_rank": 2,
        "verification_tier": 2, "paper_only": False, "confidence": "LIKELY",
        "evidence": {"source_hashes": [{"path": "report/REPORT_final.md", "sha256": digest}],
                     "rerun_command": "sha256 report/REPORT_final.md", "expected": "bound fixture",
                     "observed": "bound fixture", "tolerance_basis": None},
        "unverifiable_basis": None, "verifications": [],
        "dependencies": {"environment_fingerprint": None, "finding_ids": [], "artifact_paths": []},
        "settled": None, "benign_explanations_ruled_out": [], "report_section": None,
        "verification_snippet": None, "disclosure_flag": False,
        "remedy": {"class": "one_number", "description": "Retain the bounded value.", "verified": None},
        "changes_conclusion": False, "changes_conclusion_which": None,
        "provenance": {"origin_role": "fixture", "independently_found_by": ["fixture"], "agreed_after_seeing": [],
                       "round_introduced": 0, "source_records": ["report/REPORT_final.md"]},
    }
    (audit / "work" / "findings.jsonl").write_text(json.dumps(row, sort_keys=True) + "\n")
    for command in (["python3", str(SCRIPTS / "record_invocation.py"), "--init-policy", str(audit)],
                    ["bash", str(SCRIPTS / "init_run.sh"), str(audit), "--audit-id", audit_id,
                     "--protocol-version", "test", "--gate-set-version", "test"]):
        done = subprocess.run(command, text=True, capture_output=True, timeout=300)
        if done.returncode:
            raise AssertionError(f"fixture step failed: {command[1]}\n{done.stdout}\n{done.stderr}")


class SleepIsNotProviderTime(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-2711-sleep-")
        self.addCleanup(directory.cleanup)
        self.addCleanup(lambda: subprocess.run(["chmod", "-R", "u+w", directory.name], capture_output=True))
        self.base = pathlib.Path(directory.name).resolve()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        (self.bin / "date").write_text(FAKE_DATE)
        (self.bin / "date").chmod(0o755)

    def drive(self, name: str, **env: str) -> tuple[pathlib.Path, subprocess.CompletedProcess[str]]:
        audit = self.base / name / "audit"
        make_audit(audit, f"audit-2711-{name}")
        environment = {k: v for k, v in os.environ.items() if not k.startswith(("I4R_", "FAKE_"))}
        environment.update(
            AUDIT_DIR=str(audit), CODEX=str(FAKE), FAKE_CODEX_STATE=str(audit / "fake_state"),
            FAKE_CODEX_SCENARIO="slow", FAKE_SLOW_ROLE="stats", I4R_INVOCATION_ID=f"{name}-one",
            RETRY_BACKOFF_SECONDS="0", PROBE_LOCK_WAIT_SECONDS="3", PARALLEL="1",
            PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", PYTHONDONTWRITEBYTECODE="1", **env)
        result = subprocess.run(["bash", str(SCRIPTS / "run_critique.sh"), "1"], text=True, capture_output=True,
                                env=environment, timeout=600)
        return audit, result

    @staticmethod
    def stats_rows(audit: pathlib.Path) -> list[dict]:
        rows = [json.loads(line) for line in (audit / "invocations.jsonl").read_text().splitlines() if line.strip()]
        return [row for row in rows if row.get("role") == "critic_stats" and row.get("status") != "started"]

    def test_a_call_the_host_slept_through_is_not_stopped_and_costs_no_attempt(self) -> None:
        # 100,000 s asleep (the observed hibernation was several hours) during a 3 s call under a 30 s ceiling
        audit, result = self.drive("slept", FAKE_SLOW_SECONDS="3", FAKE_SLEPT_SECONDS="100000",
                                   I4R_PROVIDER_TIMEOUT_SECONDS="30")
        log = result.stdout + result.stderr
        self.assertNotIn("[tmout]", log, log[-4000:])
        self.assertIn("[sleep] no poll for", log)
        self.assertTrue((audit / "fake_state" / "slow_finished").exists(), "the slow call ran to its end")
        rows = self.stats_rows(audit)
        self.assertEqual([(row.get("attempt"), row.get("status"), row.get("error_class")) for row in rows],
                         [(1, "completed", None)], rows)
        self.assertEqual(result.returncode, 0, log[-4000:])

    def test_negative_control_a_hung_call_is_still_stopped_at_the_ceiling(self) -> None:
        audit, result = self.drive("hung", FAKE_SLOW_SECONDS="30", I4R_PROVIDER_TIMEOUT_SECONDS="6", MAX_TRIES="1")
        log = result.stdout + result.stderr
        self.assertIn("[tmout] provider call exceeded 6s", log, log[-4000:])
        self.assertNotIn("[sleep]", log)
        self.assertFalse((audit / "fake_state" / "slow_finished").exists(), "the hung call was stopped, not waited out")
        self.assertEqual([row.get("error_class") for row in self.stats_rows(audit)], ["timeout"])

    def test_negative_control_a_failed_clock_read_does_not_lift_the_ceiling(self) -> None:
        (self.bin / "date").write_text(FAILING_DATE)
        audit, result = self.drive("clockfail", FAKE_SLOW_SECONDS="20", I4R_PROVIDER_TIMEOUT_SECONDS="6",
                                   MAX_TRIES="1")
        log = result.stdout + result.stderr
        self.assertTrue((audit / "fake_state" / "date_failed").exists(), "the clock read failed once during the call")
        self.assertIn("[tmout] provider call exceeded 6s", log, log[-4000:])
        self.assertNotIn("[sleep]", log)
        self.assertNotIn("integer expression expected", log, "the empty read is never compared")
        self.assertFalse((audit / "fake_state" / "slow_finished").exists(), "the hung call was stopped, not waited out")
        self.assertEqual([row.get("error_class") for row in self.stats_rows(audit)], ["timeout"])

    def test_a_gap_counts_as_sleep_only_past_60_seconds(self) -> None:
        # review DT-7 (K4): the threshold itself. A 120 s gap is sleep; a 30 s one is time the call ran (the 300 s
        # ceiling is far off either way, so neither call is stopped)
        audit, result = self.drive("gap120", FAKE_SLOW_SECONDS="3", FAKE_SLEPT_SECONDS="120",
                                   I4R_PROVIDER_TIMEOUT_SECONDS="300")
        log = result.stdout + result.stderr
        self.assertRegex(log, r"\[sleep\] no poll for 12[01]s", log[-4000:])
        self.assertEqual(result.returncode, 0, log[-4000:])
        audit, result = self.drive("gap30", FAKE_SLOW_SECONDS="3", FAKE_SLEPT_SECONDS="30",
                                   I4R_PROVIDER_TIMEOUT_SECONDS="300")
        log = result.stdout + result.stderr
        self.assertTrue((audit / "fake_state" / "slow_finished").exists())
        self.assertNotIn("[sleep]", log)
        self.assertNotIn("[tmout]", log)
        self.assertEqual(result.returncode, 0, log[-4000:])


# wait_for_round.sh's clock: FAKE_CLOCK_OFFSET_FILE holds seconds to add (a host that slept), and with
# FAKE_CLOCK_STEP set every read adds that many more seconds than the last (a waiter that is awake and slow); with
# FAKE_CLOCK_FAIL_READ=n the n-th read fails with no output, once (a fork that gave EAGAIN, review NC-2)
WAITER_DATE = textwrap.dedent("""\
    #!/bin/bash
    [ "$1" = "+%s" ] || exec /bin/date "$@"
    if [ -n "${FAKE_CLOCK_FAIL_READ:-}" ]; then
      k=$(( $(cat "$FAKE_CLOCK_OFFSET_FILE.all" 2>/dev/null || echo 0) + 1 )); echo "$k" > "$FAKE_CLOCK_OFFSET_FILE.all"
      if [ "$k" -eq "$FAKE_CLOCK_FAIL_READ" ]; then echo "fork: Resource temporarily unavailable" >&2; exit 1; fi
    fi
    off=$(cat "$FAKE_CLOCK_OFFSET_FILE" 2>/dev/null || echo 0)
    if [ -n "${FAKE_CLOCK_STEP:-}" ]; then
      n=$(( $(cat "$FAKE_CLOCK_OFFSET_FILE.reads" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_CLOCK_OFFSET_FILE.reads"
      off=$(( off + n * FAKE_CLOCK_STEP ))
    fi
    echo $(( $(/bin/date +%s) + off ))
""")


class WaiterCountsAwakeTime(unittest.TestCase):
    """wait_for_round.sh's own deadline follows the same rule: a host that slept through a round no longer wakes the
    observer to TIMEOUT (exit 3) while the driver it watches carries on."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-2711-waiter-")
        self.addCleanup(directory.cleanup)
        self.base = pathlib.Path(directory.name).resolve()
        (self.base / "bin").mkdir()
        (self.base / "bin" / "date").write_text(WAITER_DATE)
        (self.base / "bin" / "date").chmod(0o755)
        self.offset = self.base / "clock_offset"
        self.offset.write_text("0\n")
        self.round = self.base / "audit" / "critique" / "round_1"
        (self.round / "invocations" / "w1").mkdir(parents=True)
        (self.round / "CURRENT_INVOCATION").write_text("w1\n")
        self.status("running")

    def status(self, outcome: str) -> None:
        done = outcome == "complete"
        roles = ["stats", "data", "fairness", "completeness"]
        body = {"status_version": "i4r-round-status-2", "invocation_id": "w1", "round": 1, "pid": os.getpid(),
                "outcome": outcome, "exit_code": 0 if done else None, "expected_roles": roles,
                "completed_roles": roles if done else [], "quorum_met": done, "adjudication": "not_needed"}
        target = self.round / "invocations" / "w1" / "ROUND_STATUS.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(body))
        temporary.replace(target)

    def waiter(self, **env: str) -> subprocess.Popen:
        environment = {k: v for k, v in os.environ.items() if not k.startswith(("I4R_", "FAKE_"))}
        environment.update(PATH=f"{self.base / 'bin'}{os.pathsep}{os.environ['PATH']}",
                           FAKE_CLOCK_OFFSET_FILE=str(self.offset), **env)
        return subprocess.Popen(["bash", str(SCRIPTS / "wait_for_round.sh"), str(self.base / "audit"), "1",
                                 "--timeout-min", "1", "--poll-seconds", "1"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)

    def test_a_wait_the_host_slept_through_does_not_time_out(self) -> None:
        waiter = self.waiter()
        time.sleep(2.5)
        self.offset.write_text("100000\n")   # the host slept 100,000 s between two polls
        time.sleep(3)
        self.status("complete")
        out, err = waiter.communicate(timeout=60)
        self.assertNotIn("TIMEOUT", err, err[-3000:])
        self.assertIn("[sleep] no poll for", err)
        self.assertEqual(waiter.returncode, 0, out + err)

    def test_negative_control_an_awake_wait_still_times_out(self) -> None:
        # each read is 25 s later than the last: awake time, no gap, and the minute is up after a few polls
        waiter = self.waiter(FAKE_CLOCK_STEP="25")
        out, err = waiter.communicate(timeout=60)
        self.assertEqual(waiter.returncode, 3, out + err)
        self.assertIn("TIMEOUT: round 1 invocation w1 did not finish within 1 min", err)
        self.assertNotIn("[sleep]", err)

    def test_negative_control_a_failed_clock_read_does_not_lift_the_wait(self) -> None:
        # the first read (which sets the deadline) and a later one; either, kept, read as 0 and moved the deadline
        # out by the whole epoch, so the observer never timed out
        for read in ("1", "2"):
            with self.subTest(failed_read=read):
                for leftover in (self.offset.with_name(self.offset.name + ".reads"),
                                 self.offset.with_name(self.offset.name + ".all")):
                    leftover.unlink(missing_ok=True)
                waiter = self.waiter(FAKE_CLOCK_STEP="25", FAKE_CLOCK_FAIL_READ=read)
                try:
                    out, err = waiter.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    waiter.kill()
                    out, err = waiter.communicate()
                    self.fail("the waiter never timed out after one failed clock read:\n" + err[-3000:])
                self.assertEqual(waiter.returncode, 3, out + err)
                self.assertIn("TIMEOUT: round 1 invocation w1 did not finish within 1 min", err)
                self.assertNotIn("[sleep]", err)
                self.assertNotIn("integer expression expected", err, "the empty read is never compared")


if __name__ == "__main__":
    unittest.main()
