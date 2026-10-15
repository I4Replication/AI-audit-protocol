"""2.6.1: the fixes surfaced by the first audits on 2.6.0 and on a stale 2.2.0 copy.

Every test builds the condition and runs the real code: the journaled critic re-pin and the route
history both policy checks honour, the approval-field refusal in integrate_round, the status
board's reservation match for a sanitised run folder, and the stale-transaction sweeps. (The
`tester` placeholder is in test_release_privacy_scope; the provider-message and end-to-end re-pin
paths are scenario 11 of tests/test_run_critique.sh.)
"""
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import time
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
import integrate_round  # noqa: E402
import record_invocation  # noqa: E402
import repin_critic_route  # noqa: E402


def init_policy(audit: pathlib.Path) -> None:
    audit.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(SCRIPTS / "record_invocation.py"), "--init-policy", str(audit)],
                   check=True, capture_output=True)


class RouteHistory(unittest.TestCase):
    POLICY = {"critic_route_history": [{
        "superseded_at": "2026-09-23T06:00:00Z", "reason": "x",
        "critics": {"requested_model": "gpt-6-sol", "provider": "openai", "reasoning_effort": "high"},
        "targeted_adjudicator": {"requested_model": "gpt-6-sol", "provider": "openai", "reasoning_effort": "max"}}]}

    def routes(self, **record):
        return record_invocation.historical_route_for(record, self.POLICY)

    def test_a_probe_or_failed_attempt_made_before_the_repin_keeps_its_route(self):
        self.assertEqual(self.routes(role="model_probe", role_class="other", status="failed",
                                     started_at="2026-09-23T05:42:18Z")[0]["requested_model"], "gpt-6-sol")
        self.assertEqual(self.routes(role="model_probe", role_class="other", status="completed",
                                     started_at="2026-09-23T05:42:18Z")[0]["requested_model"], "gpt-6-sol")
        self.assertEqual(self.routes(role="critic_stats", role_class="critic", status="failed",
                                     started_at="2026-09-23T05:50:00Z")[0]["reasoning_effort"], "high")
        self.assertEqual(self.routes(role="targeted_adjudicator", role_class="adjudicator", status="failed",
                                     started_at="2026-09-23T05:50:00Z")[0]["reasoning_effort"], "max")

    def test_a_completed_review_or_a_later_call_never_uses_history(self):
        """Negative controls: history cannot launder a completed review or a post-re-pin call."""
        self.assertEqual(self.routes(role="critic_stats", role_class="critic", status="completed",
                                     started_at="2026-09-23T05:50:00Z"), [])
        self.assertEqual(self.routes(role="model_probe", role_class="other", status="failed",
                                     started_at="2026-09-23T06:30:00Z"), [])
        self.assertEqual(self.routes(role="auditor_reproduction", role_class="auditor", status="failed",
                                     started_at="2026-09-23T05:00:00Z"), [])


class RepinRefusalsAndEffect(unittest.TestCase):
    def setUp(self):
        # 2.7.5: these tests exercise the re-pin mechanics with arbitrary targets; the shipped-route-only
        # rule itself is tests/test_275_critic_route_lock.py's
        repin_critic_route.SHIPPED_ROUTE_ONLY = False
        self.addCleanup(setattr, repin_critic_route, "SHIPPED_ROUTE_ONLY", True)
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-repin-")
        self.audit = pathlib.Path(self.tmp.name) / "audit"
        init_policy(self.audit)

    def tearDown(self):
        self.tmp.cleanup()

    def test_repin_rewrites_the_route_keeps_history_and_archives_the_old_snapshot(self):
        before = record_invocation.load_policy_snapshot(self.audit)
        result = repin_critic_route.repin(self.audit, critic_model="gpt-5.6-luna", adjudicator_model=None,
                                          reason="account cannot use the pinned model")
        self.assertTrue(result["changed"])
        after = record_invocation.load_policy_snapshot(self.audit)          # hash still verifies
        self.assertEqual(after["models"]["critics"]["requested_model"], "gpt-5.6-luna")
        self.assertEqual(after["models"]["targeted_adjudicator"]["requested_model"], "gpt-5.6-luna")
        self.assertEqual(after["models"]["critics"]["reasoning_effort"], before["models"]["critics"]["reasoning_effort"])
        self.assertEqual(after["critic_route_history"][-1]["critics"]["requested_model"],
                         before["models"]["critics"]["requested_model"])
        archive = self.audit / result["archived_snapshot"]
        self.assertTrue(archive.is_file())
        self.assertFalse(archive.stat().st_mode & stat.S_IWUSR)
        again = repin_critic_route.repin(self.audit, critic_model="gpt-5.6-luna", adjudicator_model=None, reason="again")
        self.assertFalse(again["changed"])

    def test_dry_run_checks_everything_and_writes_nothing(self):
        before = (self.audit / "pipeline-policy.snapshot.json").read_bytes()
        result = repin_critic_route.repin(self.audit, critic_model="gpt-5.6-luna", adjudicator_model=None,
                                          reason="", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual((self.audit / "pipeline-policy.snapshot.json").read_bytes(), before)
        self.assertEqual(list(self.audit.glob("pipeline-policy.snapshot.superseded-*")), [])

    def test_repin_refuses_after_a_completed_review(self):
        (self.audit / "invocations.jsonl").write_text(json.dumps({
            "invocation_id": "inv-1", "role": "critic_stats", "role_class": "critic", "status": "completed"}) + "\n")
        with self.assertRaisesRegex(repin_critic_route.RepinError, "already completed"):
            repin_critic_route.repin(self.audit, critic_model="gpt-5.6-luna", adjudicator_model=None, reason="x")

    def test_repin_refuses_while_a_driver_holds_a_round_lock(self):
        lock = self.audit / "critique/round_1/.runtime_lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{os.getpid()}\n")
        with self.assertRaisesRegex(repin_critic_route.RepinError, "driver is running"):
            repin_critic_route.repin(self.audit, critic_model="gpt-5.6-luna", adjudicator_model=None, reason="x")

    def test_repin_requires_a_reason(self):
        with self.assertRaisesRegex(repin_critic_route.RepinError, "reason"):
            repin_critic_route.repin(self.audit, critic_model="gpt-5.6-luna", adjudicator_model=None, reason="  ")


class IntegrateNamesTheApprovalFields(unittest.TestCase):
    def test_an_approved_row_without_section_or_snippet_is_named(self):
        row = {"status": "confirmed", "coordinator_approved": True, "report_section": None, "verification_snippet": None}
        self.assertEqual(integrate_round.approval_fields_missing(row), ["report_section", "verification_snippet"])
        row.update(report_section="Section 3",
                   verification_snippet={"kind": "code", "path": "work/coordinator/verification_R/f1_x.R", "basis": "b"})
        self.assertEqual(integrate_round.approval_fields_missing(row), [])

    def test_a_conclusion_changing_row_must_say_which_conclusion(self):
        """2.6.2: apply never copied changes_conclusion_which from the decision, so a row could not be
        set conclusion-changing through the documented path; the seventh audit hit it in round 1."""
        row = {"status": "confirmed", "coordinator_approved": True, "report_section": "Section 3",
               "verification_snippet": {"kind": "code", "path": "p", "basis": "b"}, "changes_conclusion": True}
        self.assertEqual(integrate_round.approval_fields_missing(row), ["changes_conclusion_which"])
        row["changes_conclusion_which"] = "the per-category Neutral comparisons"
        self.assertEqual(integrate_round.approval_fields_missing(row), [])
        self.assertIn('"changes_conclusion_which", "report_section",',
                      (SCRIPTS / "integrate_round.py").read_text())   # and apply copies it

    def test_an_unapproved_row_needs_neither(self):
        self.assertEqual(integrate_round.approval_fields_missing(
            {"status": "draft", "coordinator_approved": False}), [])


class StatusMatchesASanitisedRunFolder(unittest.TestCase):
    def test_a_finished_audit_whose_run_folder_dropped_a_question_mark_is_flagged(self):
        with tempfile.TemporaryDirectory(prefix="i4r-status-") as tmp:
            tree = pathlib.Path(tmp)
            audit = tree / "runs" / "Journal A" / "Does it work" / "audit"
            (audit / "phase1").mkdir(parents=True)
            (audit / "run.json").write_text(json.dumps({"schema_version": "2.1.0", "audit_id": "a", "status": "complete"}))
            (audit / "run.events.jsonl").write_text("")
            (audit / "paper_source.json").write_text(json.dumps({"journal": "Journal A", "paper": "Does it work?"}))
            (tree / "reservations").mkdir()
            (tree / "reservations" / "Journal_A_Does_it_work_.json").write_text(json.dumps({
                "paper": "/Users/someone/tree/data/Journal A/Does it work?", "owner": "someone@elsewhere",
                "claimed_at": "2026-09-20T00:00:00Z"}))
            out = subprocess.run([sys.executable, str(SCRIPTS / "status.py"), str(tree), "--json"],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 1, out.stderr)   # non-zero by design: something needs attention
            rows = json.loads(out.stdout)
            self.assertEqual(len(rows), 1)
            self.assertIn("finished but its paper reservation was never released", rows[0]["attention"])


class RoundResultKeepsTheJournaledIntegrationTime(unittest.TestCase):
    """2.6.2: a round_result logged after integration moved integrated_at and staled the settled cache."""

    def setUp(self):
        import lifecycle_journal
        self.lj = lifecycle_journal
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-rr-")
        self.root = pathlib.Path(self.tmp.name)
        from tests.test_metadata_2_1_lifecycle import base_run
        lifecycle_journal.initialize(self.root, base_run())
        lifecycle_journal.append_event(self.root, "round_start", {"round": 1}, at="2026-09-23T18:59:56Z")
        lifecycle_journal.append_event(self.root, "round_integrated", {"round": 1, "integrated_at": "2026-09-23T19:15:18Z"},
                                       at="2026-09-23T19:15:18Z")

    def tearDown(self):
        self.tmp.cleanup()

    def integrated_at(self):
        run = json.loads((self.root / "run.json").read_text())
        return next(r for r in run["rounds"] if r["round"] == 1)["integrated_at"]

    def test_omitted_integrated_at_inherits_the_journaled_time(self):
        self.lj.append_event(self.root, "round_result", {"round": 1, "blocking_items": 2}, at="2026-09-23T19:40:00Z")
        self.assertEqual(self.integrated_at(), "2026-09-23T19:15:18Z")

    def test_a_different_integrated_at_is_refused_with_the_journaled_value(self):
        with self.assertRaisesRegex(self.lj.JournalError, "pass integrated_at=2026-09-23T19:15:18Z"):
            self.lj.append_event(self.root, "round_result", {"round": 1, "integrated_at": "2026-09-23T19:19:07Z"})
        self.assertEqual(self.integrated_at(), "2026-09-23T19:15:18Z")

    def test_a_legacy_round_without_round_integrated_still_takes_the_payload(self):
        self.lj.append_event(self.root, "round_result", {"round": 2, "integrated_at": "2026-09-24T10:00:00Z"})
        run = json.loads((self.root / "run.json").read_text())
        self.assertEqual(next(r for r in run["rounds"] if r["round"] == 2)["integrated_at"], "2026-09-24T10:00:00Z")


class StaleTransactionSweep(unittest.TestCase):
    def test_finalize_sweeps_hour_old_transactions_and_keeps_fresh_ones(self):
        with tempfile.TemporaryDirectory(prefix="i4r-sweep-") as tmp:
            audit = pathlib.Path(tmp) / "audit"
            audit.mkdir()
            (audit / "run.json").write_text(json.dumps({"schema_version": "2.1.0"}) + "\n")
            (audit / "comment.json").write_text("{}\n")
            stale, fresh = audit / ".finalize-tx.OLDone", audit / ".finalize-tx.NEWone"
            for d in (stale, fresh):
                d.mkdir(); (d / "run.json").write_text("{}")
            old = time.time() - 7200
            os.utime(stale, (old, old))
            subprocess.run(["/bin/bash", str(SCRIPTS / "finalize_run.sh"), str(audit), "--stopping-reason",
                            "target_complete", "--no-registry"], capture_output=True, text=True, timeout=120)
            self.assertFalse(stale.exists(), "an hour-old transaction survived")
            self.assertTrue(fresh.exists(), "a recent transaction was swept")


if __name__ == "__main__":
    unittest.main()
