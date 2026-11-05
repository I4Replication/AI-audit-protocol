#!/usr/bin/env python3
"""2.7.11: status.py's board must show the audits that need someone, say why, and show nothing else.

Each case builds the condition on disk and runs the real status.py. On the live tree (2026-11-01) 2.7.10 listed
201 "audits" with 91 needing attention, of which three were real: 54 rows were the separate baseline study's
run records (baseline_comments/ and two archives of it) reported as "no run.json", and 35 were finished audits
whose only "conflict" was a Dropbox copy of a round lock's pid file. An audit whose Phase 1 had stopped (paused at
the preflight, or roles failed) stayed off the board until its journal had been idle for a day, and an audit
parked before Phase 1 a month earlier was listed with no reason and no remedy. A critique round that stopped without
its quorum (seven rounds on the tree since 2026-10-01) also waited a day, and gave no rerun command when it showed.
"""
from __future__ import annotations

import fcntl
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


SKILL = Path(__file__).resolve().parent.parent
STATUS = SKILL / "scripts" / "status.py"
_IDS = itertools.count(1)


def run(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-B", str(STATUS), *[str(a) for a in arguments]],
                          text=True, capture_output=True, check=False)


class StatusBoard2711Test(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="i4r-status-2711-")
        self.tree = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def audit(self, paper: str = "A paper", *, status: str | None = "running", idle_days: float = 0.1,
              phase1: dict | None = None, phase1_dir: bool = True, events: list[dict] | None = None,
              extra: dict | None = None) -> Path:
        path = self.tree / "runs" / "Journal A" / paper / "audit"
        path.mkdir(parents=True)
        if phase1_dir or phase1 is not None:
            (path / "phase1").mkdir()
        if phase1 is not None:
            (path / "phase1" / "status.json").write_text(json.dumps(phase1), encoding="utf-8")
        events = events or [{"seq": 1, "type": "init", "payload": {}}]
        if status is not None:
            body = {"schema_version": "2.1.0", "audit_id": f"20261101-audit-{next(_IDS):08x}", "status": status,
                    "rounds_run": 0, "events": events}
            body.update(extra or {})
            (path / "run.json").write_text(json.dumps(body), encoding="utf-8")
        journal = path / "run.events.jsonl"
        journal.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        stamp = time.time() - idle_days * 86400
        os.utime(journal, (stamp, stamp))
        return path

    def board(self, *flags: str) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
        result = run(self.tree, "--json", *flags)
        self.assertNotIn("Traceback", result.stderr)
        return result, json.loads(result.stdout)

    # ---- C55: the baseline study's run records are not audits ---------------------------------------------------
    def test_baseline_run_records_are_not_audits_wherever_they_sit(self) -> None:
        record = {"schema_version": "i4r-baseline-run-1", "baseline_version": "1.2.0", "run_id": "r1",
                  "outcome": "completed", "paper": {"journal": "Journal A", "paper": "A paper"}}
        for folder in ("baseline_comments/Journal A/A paper",                        # the baseline's own output
                       "baseline_archive/v1.1.1/baseline_comments/Journal A/A paper",  # archives of it
                       "old_baseline/archived_2026-10-29/x/Journal A/B paper"):
            (self.tree / folder).mkdir(parents=True)
            (self.tree / folder / "run.json").write_text(json.dumps(record), encoding="utf-8")
            (self.tree / folder / "records").mkdir()
        self.audit(status="complete", idle_days=3.0)
        result, rows = self.board()
        self.assertEqual([row["audit_dir"].split("/runs/")[-1] for row in rows], ["Journal A/A paper/audit"], rows)
        self.assertEqual(result.returncode, 0, rows)

    def test_an_unreadable_run_json_is_not_reported_missing(self) -> None:
        audit = self.audit()
        (audit / "run.json").write_text('{"schema_version": "2.1.0", "status": "runn', encoding="utf-8")
        _, rows = self.board()
        attention = " | ".join(rows[0]["attention"])
        self.assertIn("run.json cannot be read", attention)
        self.assertNotIn("no run.json", attention)

    # ---- C26: a stopped Phase 1 is on the board at once ---------------------------------------------------------
    def test_a_preflight_pause_is_flagged_at_once(self) -> None:
        self.audit(status="paused", idle_days=0.05, extra={"pause_reason": "local_preflight_failed"},
                   phase1={"status": "paused", "pause_reason": "local_preflight_failed",
                           "auditor_inferences_started": 0,
                           "error": "Phase-1 pre-dispatch deadline exceeded during snapshot hashing"})
        result, rows = self.board("--stuck")
        self.assertEqual(len(rows), 1, rows)
        attention = " | ".join(rows[0]["attention"])
        self.assertIn("local_preflight_failed", attention)
        self.assertIn("run_phase1.py", attention)
        self.assertEqual(result.returncode, 1)

    def test_a_status_only_pause_and_a_failed_fan_out_are_flagged_at_once(self) -> None:
        self.audit("Paused paper", idle_days=0.2,
                   phase1={"status": "paused", "pause_reason": "sandbox_environment_fault",
                           "completed_roles": [], "environment_faults": {"auditor_reproduction": "sandbox denied"}})
        self.audit("Failed paper", idle_days=0.7,
                   phase1={"status": "failed", "pause_reason": None,
                           "completed_roles": ["auditor_code_methods"],
                           "failed_roles": {"auditor_reproduction": "auditor_reproduction inference failed "
                                                                    "(permanent): usage limit reached"}})
        result, rows = self.board("--stuck")
        by_paper = {row["paper"]: " | ".join(row["attention"]) for row in rows}
        self.assertEqual(sorted(by_paper), ["Failed paper", "Paused paper"], rows)
        self.assertIn("sandbox_environment_fault", by_paper["Paused paper"])
        self.assertIn("auditor_reproduction", by_paper["Failed paper"])
        self.assertIn("usage limit reached", by_paper["Failed paper"])
        self.assertEqual(result.returncode, 1)

    def test_a_phase1_past_quorum_or_running_on_this_host_is_not_flagged(self) -> None:
        """Negative controls: a later failed run beside a quorum, and a pause this host is re-running now."""
        past = self.audit("Past quorum", phase1={"status": "failed", "error": "stray re-run refused"})
        (past / "phase1" / "quorum.json").write_text("{}", encoding="utf-8")
        live = self.audit("Re-running", phase1={"status": "paused", "pause_reason": "local_preflight_failed"})
        lock = live / "phase1" / "run.lock"
        lock.touch()
        holder = subprocess.Popen([sys.executable, "-c", (
            "import fcntl, sys, time\n"
            "h = open(sys.argv[1], 'a+'); fcntl.flock(h.fileno(), fcntl.LOCK_EX)\n"
            "print('held', flush=True); time.sleep(60)\n"), str(lock)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            result, rows = self.board("--stuck")
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        self.assertEqual(rows, [])
        self.assertEqual(result.returncode, 0)

    # ---- D7: an audit parked before Phase 1 says why and what to do ----------------------------------------------
    def test_an_audit_parked_before_phase1_shows_its_note_and_a_remedy(self) -> None:
        events = [{"seq": 1, "type": "init", "payload": {}},
                  {"seq": 2, "type": "phase_start", "payload": {"phase": "intake"}},
                  {"seq": 3, "type": "note", "payload": {
                      "text": "Parked at intake before Phase 1: the deposited package does not match this paper."}}]
        self.audit(idle_days=31.0, phase1_dir=False, events=events)
        result, rows = self.board("--stuck")
        self.assertEqual(len(rows), 1, rows)
        attention = " | ".join(rows[0]["attention"])
        self.assertIn("Phase 1 never started", attention)
        self.assertIn("old_runs/", attention)
        self.assertTrue(str(rows[0].get("last_note") or "").startswith("Parked at intake"), rows[0])
        text = run(self.tree, "--stuck").stdout
        self.assertIn("note: Parked at intake before Phase 1", text)

    # ---- the lock directory's own pid file is not a conflict in the audit -----------------------------------------
    def test_a_round_locks_conflicted_pid_is_not_flagged(self) -> None:
        clean = self.audit("Clean paper", status="complete", idle_days=5.0)
        lock = clean / "critique" / "round_2" / ".runtime_lock"
        lock.mkdir(parents=True)
        (lock / "pid (Someone's conflicted copy 2026-10-07)").write_text("4242\n", encoding="utf-8")
        stale = clean / "critique" / "round_1" / ".runtime_lock.stale.20261007T000000Z"   # a lock moved aside (O2)
        stale.mkdir(parents=True)
        (stale / "pid (Someone's conflicted copy 2026-10-07)").write_text("4242\n", encoding="utf-8")
        forked = self.audit("Forked paper", status="complete", idle_days=5.0)   # positive control
        (forked / "run.events (Someone's conflicted copy 2026-10-07).jsonl").write_text("", encoding="utf-8")
        result, rows = self.board()
        by_paper = {row["paper"]: row["attention"] for row in rows}
        self.assertEqual(by_paper["Clean paper"], [])
        self.assertTrue(any("conflicted" in why for why in by_paper["Forked paper"]), by_paper)

    # ---- the version-pin refusal: intake done, init refused, the paper still claimed --------------------------------
    def test_an_intake_that_never_initialised_names_its_way_forward(self) -> None:
        audit = self.audit(status=None, phase1_dir=False)
        (audit / "run.events.jsonl").unlink()
        (audit / "pkg").mkdir()
        (audit / "package_manifest.json").write_text("{}", encoding="utf-8")
        (audit / "paper_source.json").write_text(json.dumps({"journal": "Journal A", "paper": "A paper"}),
                                                 encoding="utf-8")
        (self.tree / "reservations").mkdir()
        (self.tree / "reservations" / "Journal_A__A_paper.json").write_text(json.dumps({
            "owner": "someone@elsewhere", "paper": "/Users/someone/tree/data/Journal A/A paper",  # placeholder
            "claimed_at": "2026-11-01T00:00:00Z", "status": "active"}), encoding="utf-8")
        result, rows = self.board("--stuck")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["reservation_file"], "Journal_A__A_paper.json")
        attention = " | ".join(rows[0]["attention"])
        self.assertIn("never initialised", attention)
        self.assertIn("init_run.sh", attention)
        self.assertIn("reserve_paper.sh release", attention)

    # ---- C25: a critique round that stopped is on the board at once, with the command that resumes it --------------
    def critique_audit(self, paper: str, *, round_number: int = 1, state: str = "running", events: list[dict],
                       status: str = "running", pause_reason: str | None = None, round_status: dict | None = None,
                       idle_days: float = 0.05) -> Path:
        audit = self.audit(paper, status=status, idle_days=idle_days, events=events, extra={
            "pause_reason": pause_reason, "rounds": [
                {"round": n, "state": "integrated" if n < round_number else state, "started_at": "2026-11-01T00:00:00Z"}
                for n in range(1, round_number + 1)]})
        (audit / "phase1").joinpath("status.json").write_text(json.dumps({"status": "quorum_complete"}),
                                                              encoding="utf-8")
        (audit / "phase1" / "quorum.json").write_text("{}", encoding="utf-8")
        out = audit / "critique" / f"round_{round_number}"
        (out / "invocations").mkdir(parents=True)
        if round_status is not None:
            body = {"status_version": "i4r-round-status-2", "round": round_number, "pid": 1, **round_status}
            (out / "ROUND_STATUS.json").write_text(json.dumps(body), encoding="utf-8")
            invocation = out / "invocations" / str(body.get("invocation_id") or "inv")
            (invocation / "attempts").mkdir(parents=True)
            (invocation / "attempts" / "a1.json").write_text("{}", encoding="utf-8")
            (invocation / "ROUND_STATUS.json").write_text(json.dumps(body), encoding="utf-8")
        return audit

    @staticmethod
    def stop_events(round_number: int, invocation: str, *, ended: bool = False) -> list[dict]:
        receipt = f"critique/round_{round_number}/invocations/{invocation}/ROUND_STATUS.json"
        events = [{"seq": 1, "type": "init", "payload": {}},
                  {"seq": 2, "type": "round_start", "payload": {"round": round_number}},
                  {"seq": 3, "type": "pause_start", "source": "run_critique.sh", "payload": {
                      "reason": "critique_round_failed", "phase": "critique", "round": round_number,
                      "driver_invocation": invocation, "quorum_met": False,
                      "completed_roles": ["critic_data", "critic_fairness", "critic_completeness"],
                      "failed_roles": ["critic_stats"], "exit_code": 1, "receipt": receipt,
                      "receipt_sha256": "0" * 64}}]
        if ended:
            events.append({"seq": 4, "type": "pause_end", "source": "run_critique.sh", "payload": {
                "reason": "critique_round_failed", "phase": "critique", "round": round_number,
                "driver_invocation": "rerun", "resume_status": "running", "stopped_by": invocation}})
        return events

    def test_a_journaled_critique_stop_is_flagged_at_once_with_its_rerun(self) -> None:
        failed = {"invocation_id": "stop-1", "outcome": "failed", "exit_code": 1, "quorum_met": False,
                  "completed_roles": ["data", "fairness", "completeness"], "failed_roles": ["stats"],
                  "detail": "exact role quorum failed; missing: stats"}
        audit = self.critique_audit("Stopped round", round_number=2, events=self.stop_events(2, "stop-1"),
                                    status="paused", pause_reason="critique_round_failed", round_status=failed)
        # round 2 was started with a CRITICS override and a diff manifest: the rerun must restate both
        (audit / "critique" / "round_2" / "invocations" / "stop-1" / "ROUTING.json").write_text(json.dumps(
            {"reason": "CRITICS override", "roles": ["stats", "data"]}), encoding="utf-8")
        (audit / "critique" / "DIFF_MANIFEST_round_2.json").write_text("{}", encoding="utf-8")
        result, rows = self.board("--stuck")
        self.assertEqual(len(rows), 1, rows)
        attention = " | ".join(rows[0]["attention"])
        self.assertIn("critique round 2 stopped without its critic quorum", attention)
        self.assertIn("critic_stats", attention)
        self.assertIn("CRITICS='stats data' DIFF_MANIFEST=<audit dir>/critique/DIFF_MANIFEST_round_2.json "
                      "AUDIT_DIR=<audit dir> bash scripts/run_critique.sh 2", attention)
        self.assertIn("critique/round_2/invocations/stop-1/ROUND_STATUS.json", attention)
        self.assertNotIn("unfinished and idle", attention)
        self.assertEqual(rows[0]["critique_stop"]["round"], 2)
        self.assertEqual(result.returncode, 1)

    def test_an_unjournaled_failed_round_is_flagged_at_once(self) -> None:
        """An older driver, or a run refused before dispatch, journals no pause: ROUND_STATUS.json says it."""
        self.critique_audit("Old driver", events=[{"seq": 1, "type": "init", "payload": {}}], round_status={
            "invocation_id": "old-1", "outcome": "failed", "failed_roles": ["stats", "data"],
            "completed_roles": [], "detail": "exact role quorum failed; missing: stats data (/Users/someone/x)"})
        self.critique_audit("Adjudicator", events=[{"seq": 1, "type": "init", "payload": {}}], round_status={
            "invocation_id": "adj-1", "outcome": "quorum_complete_adjudication_failed", "failed_roles": [],
            "completed_roles": ["stats", "data", "fairness", "completeness"], "detail": "adjudicator timed out"})
        _, rows = self.board("--stuck")
        by_paper = {row["paper"]: " | ".join(row["attention"]) for row in rows}
        self.assertEqual(sorted(by_paper), ["Adjudicator", "Old driver"], rows)
        self.assertIn("critique round 1's last run failed", by_paper["Old driver"])
        self.assertIn("2 critic(s) failed (stats, data)", by_paper["Old driver"])
        self.assertIn("AUDIT_DIR=<audit dir> bash scripts/run_critique.sh 1", by_paper["Old driver"])
        self.assertIn("/Users/<user>/x", by_paper["Old driver"])
        self.assertNotIn("someone", by_paper["Old driver"])
        self.assertIn("adjudicator failed", by_paper["Adjudicator"])

    def test_a_resumed_integrated_or_rerunning_round_is_not_flagged(self) -> None:
        """Negative controls: the pause a rerun ended, a finished round whose refused rerun failed, a stopped round
        whose rerun is under way on this host, and a stale Phase-1 failure once critique has started."""
        running = {"invocation_id": "rerun", "outcome": "running", "failed_roles": [], "completed_roles": []}
        self.critique_audit("Resumed", events=self.stop_events(1, "stop-1", ended=True), round_status=running)
        self.critique_audit("Integrated", state="integrated", events=[{"seq": 1, "type": "init", "payload": {}}],
                            round_status={"invocation_id": "refused", "outcome": "failed", "failed_roles": []})
        live = self.critique_audit("Rerunning", events=self.stop_events(1, "stop-1"), status="paused",
                                   pause_reason="critique_round_failed",
                                   round_status={"invocation_id": "stop-1", "outcome": "failed",
                                                 "failed_roles": ["stats"]})
        stale = self.critique_audit("Stale phase1", events=[{"seq": 1, "type": "init", "payload": {}}],
                                    round_status=running)
        (stale / "phase1" / "status.json").write_text(json.dumps({"status": "failed", "error": "old"}),
                                                      encoding="utf-8")
        (stale / "phase1" / "quorum.json").unlink()
        driver = self.tree / "bin" / "run_critique.sh"
        driver.parent.mkdir()
        driver.write_text("echo started; sleep 60\n", encoding="utf-8")
        holder = subprocess.Popen(["bash", str(driver)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "started")
            lock = live / "critique" / "round_1" / ".runtime_lock"
            lock.mkdir()
            (lock / "pid").write_text(f"{holder.pid}\n", encoding="utf-8")
            result, rows = self.board("--stuck")
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        self.assertEqual(rows, [])
        self.assertEqual(result.returncode, 0)
        # the same stopped round with its driver gone is flagged (the control above held only while it ran)
        _, rows = self.board("--stuck")
        self.assertEqual([row["paper"] for row in rows], ["Rerunning"], rows)

    def test_any_pause_end_closes_a_journaled_critique_stop(self) -> None:
        # Review EDGE-4: the run projection closes a pause on any pause_end, whatever its reason; the board kept the
        # stop open until a pause_end with reason critique_round_failed and told the operator to re-run round 1,
        # which was already integrated.
        events = self.stop_events(1, "stop-1") + [
            {"seq": 4, "type": "pause_end", "source": "operator", "payload": {"phase": "critique"}},
            {"seq": 5, "type": "round_integrated", "payload": {"round": 1}},
            {"seq": 6, "type": "round_start", "payload": {"round": 2}}]
        self.critique_audit("Closed by hand", round_number=2, events=events, round_status={
            "invocation_id": "r2", "outcome": "running", "failed_roles": [], "completed_roles": []})
        result, rows = self.board("--stuck")
        self.assertEqual(rows, [])
        self.assertEqual(result.returncode, 0)

    def test_an_attended_round_waiting_for_its_grant_names_authorize_round(self) -> None:
        self.critique_audit("Attended", state="awaiting_authorization", status="paused",
                            pause_reason="awaiting_authorization",
                            events=[{"seq": 1, "type": "init", "payload": {}}])
        _, rows = self.board("--stuck")
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("authorize-round", " | ".join(rows[0]["attention"]))

    def test_provider_text_on_the_board_masks_an_e_mail_address(self) -> None:
        self.audit(phase1={"status": "failed", "failed_roles": {
            "auditor_reproduction": "usage limit reached for operator@example.org; try again later"}})
        _, rows = self.board("--stuck")
        attention = " | ".join(rows[0]["attention"])
        self.assertIn("<e-mail>", attention)
        self.assertNotIn("operator@example.org", attention)


if __name__ == "__main__":
    unittest.main()
