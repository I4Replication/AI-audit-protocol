"""Two blockers found by driving the shipped scripts on awkward but legitimate inputs.

setup_workspace.sh: `find | head` under `set -euo pipefail` takes SIGPIPE once find has written
more than a pipe buffer of names -- a deposit with a few hundred top-level directories -- and
intake dies with exit 141, no message, after the paper was reserved and before pkg/ was hashed.

wait_for_round.sh: the waiter knew three of the driver's five terminal outcomes and reported the
other two -- `paused`, and `quorum_complete_adjudication_failed`, the one state the driver made
distinct on purpose -- as a malformed or foreign status file (exit 4, ABNORMAL).
"""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SETUP = SKILL / "scripts" / "setup_workspace.sh"
WAIT = SKILL / "scripts" / "wait_for_round.sh"


class IntakeSurvivesALargeDeposit(unittest.TestCase):
    def test_seven_hundred_top_level_directories(self):
        with tempfile.TemporaryDirectory(prefix="i4r-intake-large-") as tmp:
            base = pathlib.Path(tmp)
            paper = base / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\n%%EOF\n")
            deposit = base / "deposit"
            for i in range(700):
                d = deposit / f"subdirectory_with_a_realistically_long_name_number_{i}"
                d.mkdir(parents=True)
                (d / f"analysis_script_{i}.R").write_text("1 + 1\n")
            audit = base / "paperdir" / "audit"
            result = subprocess.run(
                ["/bin/bash", str(SETUP), str(audit), str(paper), str(deposit)],
                text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=600,
                env=dict(os.environ, I4R_SKIP_RESERVE="1"))
            self.assertEqual(0, result.returncode,
                             f"rc={result.returncode}\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}")
            self.assertTrue((audit / "package_manifest.json").is_file(), "intake never hashed the package")
            self.assertIn("and 680 more", result.stdout)


class WaiterKnowsEveryDriverOutcome(unittest.TestCase):
    def status(self, outcome, *, exit_code, quorum, detail=""):
        return {
            "status_version": "i4r-round-status-2", "invocation_id": "inv-1", "round": 2,
            "pid": 999999, "outcome": outcome, "exit_code": exit_code,
            "expected_roles": ["stats", "completeness"],
            "completed_roles": ["stats", "completeness"] if quorum else ["stats"],
            "quorum_met": quorum, "adjudication": "failed" if "adjudication" in outcome else "not_reached",
            "detail": detail,
        }

    def wait(self, status):
        with tempfile.TemporaryDirectory(prefix="i4r-waiter-") as tmp:
            audit = pathlib.Path(tmp) / "audit"
            inv = audit / "critique" / "round_2" / "invocations" / "inv-1"
            inv.mkdir(parents=True)
            (audit / "critique" / "round_2" / "CURRENT_INVOCATION").write_text("inv-1\n")
            (inv / "ROUND_STATUS.json").write_text(json.dumps(status))
            return subprocess.run(
                ["/bin/bash", str(WAIT), str(audit), "2", "--invocation", "inv-1", "--timeout-min", "1"],
                text=True, capture_output=True, timeout=60)

    def test_adjudication_failed_is_reported_as_what_it_is(self):
        r = self.wait(self.status("quorum_complete_adjudication_failed", exit_code=1, quorum=True,
                                  detail="adjudicator schema-invalid three times"))
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertNotIn("ABNORMAL", r.stderr)
        self.assertIn("only the targeted adjudicator failed", r.stderr)
        self.assertIn("critic quorum is complete", r.stderr)

    def test_paused_is_reported_as_paused(self):
        r = self.wait(self.status("paused", exit_code=3, quorum=False, detail="awaiting authorization"))
        self.assertEqual(3, r.returncode, r.stdout + r.stderr)
        self.assertIn("PAUSED", r.stderr)
        self.assertNotIn("ABNORMAL", r.stderr)

    def test_the_three_original_outcomes_still_behave(self):
        self.assertEqual(0, self.wait(self.status("complete", exit_code=0, quorum=True)).returncode)
        self.assertEqual(1, self.wait(self.status("failed", exit_code=1, quorum=False)).returncode)
        # running with a dead pid and no terminal status is genuinely abnormal
        r = self.wait(self.status("running", exit_code=None, quorum=False))
        self.assertEqual(4, r.returncode)

    def test_an_unknown_outcome_is_still_rejected(self):
        """Negative control: widening the enum must not admit garbage."""
        r = self.wait(self.status("finished", exit_code=0, quorum=True))
        self.assertEqual(4, r.returncode)
        self.assertIn("malformed", r.stderr)


if __name__ == "__main__":
    unittest.main()
