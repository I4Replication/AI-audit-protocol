#!/usr/bin/env python3
"""status.py must find every live audit, survive a damaged one, and report through its exit code.

Three defects, each of which made the sweep quietly under-report at exactly the scale it exists
for. It walked a fixed <journal>/<paper>/audit path, so the workspaces relocated with
I4R_PAPER_DIR -- the escape hatch for papers whose titles contain glob metacharacters -- were
invisible; `--json` always exited 0, so a batch driver branching on it saw a clean tree; and one
unreadable audit ended the whole sweep.
"""
from __future__ import annotations

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


def run(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(STATUS), *[str(a) for a in arguments]],
                          text=True, capture_output=True, check=False)


class StatusSweepTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="i4r-status-")
        self.tree = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_audit(self, path: Path, *, status: str = "running", idle_days: float = 9.0,
                   run_json: bool = True) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / "phase1").mkdir(exist_ok=True)
        if run_json:
            (path / "run.json").write_text(json.dumps({
                "schema_version": "2.0.0", "audit_id": f"audit-{path.name}", "status": status,
            }), encoding="utf-8")
        journal = path / "run.events.jsonl"
        journal.write_text("", encoding="utf-8")
        old = time.time() - idle_days * 86400
        os.utime(journal, (old, old))
        return path

    def test_conventional_audit_is_found(self) -> None:
        self.make_audit(self.tree / "Journal A" / "A paper" / "audit")
        result = run(self.tree, "--json")
        rows = json.loads(result.stdout)
        self.assertEqual([r["paper"] for r in rows], ["A paper"])

    def test_relocated_workspace_is_found_and_named_for_its_paper(self) -> None:
        """.work/audits/<name> is where a paper with '?' in its title has to run."""
        self.make_audit(self.tree / ".work" / "audits" / "IJSEP_gymnastics_autonomy_support")
        result = run(self.tree, "--json")
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 1, result.stdout)
        self.assertEqual(rows[0]["paper"], "IJSEP_gymnastics_autonomy_support")
        self.assertTrue(rows[0]["relocated"])

    def test_json_exits_nonzero_when_something_needs_attention(self) -> None:
        self.make_audit(self.tree / "Journal A" / "Stalled paper" / "audit", idle_days=9.0)
        needy = run(self.tree, "--json")
        self.assertEqual(needy.returncode, 1)
        self.assertEqual(run(self.tree).returncode, 1)

    def test_json_exits_zero_on_a_clean_tree(self) -> None:
        self.make_audit(self.tree / "Journal A" / "Done paper" / "audit", status="complete")
        clean = run(self.tree, "--json")
        self.assertEqual(clean.returncode, 0, clean.stdout)
        self.assertEqual(run(self.tree).returncode, 0)

    def test_one_damaged_audit_does_not_end_the_sweep(self) -> None:
        self.make_audit(self.tree / "Journal A" / "Good paper" / "audit", idle_days=9.0)
        broken = self.make_audit(self.tree / "Journal A" / "Broken paper" / "audit")
        (broken / "run.json").write_text("{not json", encoding="utf-8")
        result = run(self.tree, "--json")
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 2, result.stdout)
        self.assertIn("Good paper", [r["paper"] for r in rows])

    def test_archived_siblings_are_listed_but_not_flagged(self) -> None:
        paper = self.tree / "Journal A" / "A paper"
        self.make_audit(paper / "audit", status="complete")
        self.make_audit(paper / "audit.failed-20260903-sandbox-path", idle_days=30.0)
        rows = json.loads(run(self.tree, "--json").stdout)
        archived = next(r for r in rows if r["archived"])
        self.assertEqual(archived["attention"], [])
        self.assertEqual(run(self.tree).returncode, 0)

    def test_published_and_skipped_trees_are_not_walked(self) -> None:
        self.make_audit(self.tree / "Completed reports" / "A paper" / "audit", idle_days=99.0)
        self.make_audit(self.tree / "skill" / "fixtures" / "audit", idle_days=99.0)
        rows = json.loads(run(self.tree, "--json").stdout)
        self.assertEqual(rows, [])

    def reserve(self, paper: Path, *, owner: str = "someone@elsewhere",
                age_days: float = 3.0, name: str | None = None) -> Path:
        """Write a reservation the way reserve_paper.sh does: an ABSOLUTE claiming-machine path."""
        (self.tree / "reservations").mkdir(exist_ok=True)
        path = self.tree / "reservations" / (name or f"{paper.name}.json")
        path.write_text(json.dumps({
            "paper": f"/Users/someone/tree/{paper.parent.name}/{paper.name}",  # allowlisted placeholder
            "owner": owner, "claimed_at": "2026-09-01T00:00:00Z",
        }), encoding="utf-8")
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_a_reservation_is_matched_to_its_audit(self) -> None:
        """The claim path is absolute on the CLAIMING machine, so it can never match ours.

        Keying the index by that path meant every row reported null while reservations sat on
        the board, and the "finished but never released" check could not fire at all.
        """
        paper = self.tree / "Journal A" / "A paper"
        self.make_audit(paper / "audit")
        self.reserve(paper)
        row = json.loads(run(self.tree, "--json").stdout)[0]
        self.assertIsNotNone(row["reservation_age_days"])
        self.assertEqual(row["reservation_file"], "A paper.json")

    def test_a_finished_audit_with_a_live_reservation_is_flagged(self) -> None:
        paper = self.tree / "Journal A" / "A paper"
        self.make_audit(paper / "audit", status="complete")
        self.reserve(paper)
        result = run(self.tree, "--json")
        row = json.loads(result.stdout)[0]
        self.assertTrue(any("never released" in a for a in row["attention"]), row["attention"])
        self.assertEqual(result.returncode, 1)

    def test_an_unrelated_reservation_is_not_attached(self) -> None:
        """Negative control: matching by basename must not attach any claim to any audit."""
        paper = self.tree / "Journal A" / "A paper"
        self.make_audit(paper / "audit", status="complete")
        self.reserve(self.tree / "Journal A" / "A different paper")
        row = json.loads(run(self.tree, "--json").stdout)[0]
        self.assertIsNone(row["reservation_age_days"])
        self.assertEqual(row["attention"], [])

    def test_a_relocated_workspace_claims_nothing_without_paper_source(self) -> None:
        """Its parent is a holding directory; guessing from that name would attach the wrong claim."""
        audit = self.make_audit(self.tree / ".work" / "audits" / "IJSEP_gymnastics")
        self.reserve(self.tree / "x", name="audits.json")
        row = json.loads(run(self.tree, "--json").stdout)[0]
        self.assertTrue(row["relocated"])
        self.assertIsNone(row["reservation_age_days"])
        # ...but it does resolve once intake has recorded the true paper name.
        (audit / "paper_source.json").write_text(json.dumps(
            {"journal": "IJSEP", "paper": "Gymnastics autonomy support"}), encoding="utf-8")
        self.reserve(self.tree / "IJSEP" / "Gymnastics autonomy support")  # status matches journal AND paper
        row = json.loads(run(self.tree, "--json").stdout)[0]
        self.assertIsNotNone(row["reservation_age_days"])

    def test_a_closed_pipe_does_not_produce_a_traceback(self) -> None:
        """`status.py <tree> | head` is the obvious usage and it used to die with a traceback."""
        self.make_audit(self.tree / "Journal A" / "A paper" / "audit", idle_days=9.0)
        piped = subprocess.run(
            f"{sys.executable} {STATUS} {self.tree!s} --json | head -2",
            shell=True, capture_output=True, text=True)
        self.assertNotIn("BrokenPipeError", piped.stderr)
        self.assertNotIn("Traceback", piped.stderr)

    def test_an_audits_own_subtree_is_not_rescanned(self) -> None:
        """A nested phase1/ or pkg/ must not be reported as another audit."""
        audit = self.make_audit(self.tree / "Journal A" / "A paper" / "audit")
        (audit / "pkg" / "phase1").mkdir(parents=True)
        (audit / "work" / "nested").mkdir(parents=True)
        (audit / "work" / "nested" / "run.json").write_text("{}", encoding="utf-8")
        rows = json.loads(run(self.tree, "--json").stdout)
        self.assertEqual(len(rows), 1, rows)


if __name__ == "__main__":
    unittest.main()
