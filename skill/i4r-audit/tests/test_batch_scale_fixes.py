"""Fixes for the shapes a 146-paper batch contains that the first three audits never met.

Each of these stranded or bricked a real audit directory in the tree, or would have on a paper
already sitting in it. They are grouped by the failure they prevent, not by the file they touch.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import phase1_runtime  # noqa: E402

SETUP = (SKILL / "scripts/setup_workspace.sh").read_text()
RESERVE = (SKILL / "scripts/reserve_paper.sh").read_text()
CRITIQUE = (SKILL / "scripts/run_critique.sh").read_text()
PHASE1 = (SKILL / "scripts/run_phase1.py").read_text()


class SymlinksAreJudgedByWhereTheyPoint(unittest.TestCase):
    """Any symlink was fatal, so one deposit shipping conf.yaml -> src/conf.yaml could not be
    audited at all. Only an ESCAPING link is a sandbox problem."""

    def _tree(self, tmp: str) -> pathlib.Path:
        root = pathlib.Path(tmp)
        (root / "paper").mkdir()
        (root / "pkg/src").mkdir(parents=True)
        (root / "paper/paper.txt").write_text("x")
        (root / "pkg/src/conf.yaml").write_text("real")
        return root

    def test_an_internal_symlink_is_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / "pkg/conf.yaml").symlink_to("src/conf.yaml")
            names = {r for r, _ in phase1_runtime.source_files(root)}
            self.assertIn("pkg/conf.yaml", names)

    def test_a_dangling_symlink_is_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / "pkg/gone.yaml").symlink_to("src/missing.yaml")
            names = {r for r, _ in phase1_runtime.source_files(root)}
            self.assertNotIn("pkg/gone.yaml", names)
            self.assertIn("paper/paper.txt", names)

    def test_an_escaping_symlink_is_still_fatal(self) -> None:
        # Negative control. This is the case the original rule existed for.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / "pkg/escape.txt").symlink_to("/etc/hosts")
            with self.assertRaises(phase1_runtime.Phase1Error) as caught:
                phase1_runtime.source_files(root)
            self.assertIn("escaping", str(caught.exception))


class ReapingIsOwnerScopedAndEvidenceBased(unittest.TestCase):
    """reap() must free abandoned claims without ever freeing a live one.

    These build reservations and audit trees on disk and run the real subcommand. The previous
    version asserted that particular substrings appeared in reserve_paper.sh, which passes just
    as well on a version whose logic has been inverted around them.
    """

    RESERVE_SH = SKILL / "scripts" / "reserve_paper.sh"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="i4r-reap-")
        self.tree = pathlib.Path(self._tmp.name).resolve()
        (self.tree / "reservations").mkdir()
        (self.tree / "Completed reports").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def whoami(self) -> str:
        return subprocess.run(["bash", "-c", 'echo "${USER:-$(id -un)}@$(hostname -s)"'],
                              capture_output=True, text=True).stdout.strip()

    def claim(self, slug: str, *, age_days: int, owner: str | None = None,
              paper_dir: pathlib.Path | None = None, journal_age_days: float | None = None,
              name: str | None = None) -> pathlib.Path:
        when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days)
        if paper_dir is None:
            paper_dir = self.tree / "Journal A" / slug
            paper_dir.mkdir(parents=True, exist_ok=True)
        if journal_age_days is not None:
            audit = paper_dir / "audit"
            audit.mkdir(parents=True, exist_ok=True)
            (audit / "paper_source.json").write_text("{}", encoding="utf-8")
            journal = audit / "run.events.jsonl"
            journal.write_text("", encoding="utf-8")
            stamp = time.time() - journal_age_days * 86400
            os.utime(journal, (stamp, stamp))
        path = self.tree / "reservations" / (name or f"{slug}.json")
        path.write_text(json.dumps({
            "paper": str(paper_dir),
            "owner": owner if owner is not None else self.whoami(),
            "claimed_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }), encoding="utf-8")
        return path

    def reap(self, *extra: str, **env: str) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, **env}
        return subprocess.run(
            ["bash", str(self.RESERVE_SH), "reap", str(self.tree), *extra],
            capture_output=True, text=True, env=environment)

    def test_an_abandoned_claim_of_mine_is_reaped(self) -> None:
        claim = self.claim("abandoned", age_days=60, journal_age_days=60)
        result = self.reap("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(claim.exists(), result.stdout)

    def test_a_live_audit_keeps_its_claim(self) -> None:
        """The attack the reviewer found: an old claim whose audit is being worked on today."""
        claim = self.claim("live", age_days=60, journal_age_days=0.01)
        result = self.reap("--force")
        self.assertTrue(claim.exists(), result.stdout)
        self.assertIn("HELD", result.stdout.upper())

    def test_a_recent_claim_is_never_reaped(self) -> None:
        claim = self.claim("fresh", age_days=0, journal_age_days=60)
        self.reap("--force")
        self.assertTrue(claim.exists())

    def test_another_owners_claim_is_held_by_default(self) -> None:
        claim = self.claim("theirs", age_days=60, journal_age_days=60,
                           owner="someone@elsewhere")
        result = self.reap("--force")
        self.assertTrue(claim.exists(), result.stdout)

    def test_another_owners_claim_can_be_reaped_only_deliberately(self) -> None:
        claim = self.claim("theirs", age_days=60, journal_age_days=60,
                           owner="someone@elsewhere")
        result = self.reap("--force", I4R_REAP_ANY_OWNER="1")
        self.assertFalse(claim.exists(), result.stdout)

    def test_a_claim_with_no_visible_audit_is_held_not_reaped(self) -> None:
        """"No audit directory" can just mean the claim was made on another machine."""
        claim = self.claim("elsewhere", age_days=60)
        result = self.reap("--force")
        self.assertTrue(claim.exists(), result.stdout)

    def test_a_conflicted_copy_is_held_not_deleted(self) -> None:
        """It is the evidence claim() uses to arbitrate a race; deleting one destroys that."""
        claim = self.claim("raced", age_days=60, journal_age_days=60,
                           name="raced (alice's conflicted copy 2026-08-02).json")
        result = self.reap("--force")
        self.assertTrue(claim.exists(), result.stdout)
        self.assertIn("CONFLICT COPY", result.stdout)

    def test_a_malformed_reservation_is_held_and_does_not_end_the_sweep(self) -> None:
        broken = self.tree / "reservations" / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        good = self.claim("abandoned", age_days=60, journal_age_days=60)
        result = self.reap("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(broken.exists())
        self.assertFalse(good.exists(), result.stdout)

    def test_without_force_nothing_is_deleted(self) -> None:
        claim = self.claim("abandoned", age_days=60, journal_age_days=60)
        result = self.reap()
        self.assertTrue(claim.exists(), result.stdout)

    def test_release_requires_force_in_the_documented_interface(self) -> None:
        self.assertIn("re-run with --force to release", RESERVE)


class RenumberingIsAToolNotHandwork(unittest.TestCase):
    def test_the_tool_exists(self) -> None:
        self.assertTrue((SKILL / "scripts/renumber_findings.py").is_file())

    def test_it_is_idempotent_on_an_already_numbered_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "report").mkdir(); (root / "work").mkdir()
            (root / "report/REPORT_final.md").write_text("## 1. X\n<!-- finding:F1 -->\n")
            (root / "work/findings.jsonl").write_text(json.dumps({
                "id": "F1", "ledger_disposition": {"status": "INCLUDED", "target": None}}) + "\n")
            out = subprocess.run(
                [sys.executable, str(SKILL / "scripts/renumber_findings.py"), str(root), "--apply"],
                capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("already renumbered", out.stdout)

    def test_it_refuses_when_markers_and_rows_disagree(self) -> None:
        # Negative control: it must not guess an order.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "report").mkdir(); (root / "work").mkdir()
            (root / "report/REPORT_final.md").write_text("## 1. X\n<!-- finding:AAA -->\n")
            (root / "work/findings.jsonl").write_text(json.dumps({
                "id": "BBB", "ledger_disposition": {"status": "INCLUDED", "target": None}}) + "\n")
            out = subprocess.run(
                [sys.executable, str(SKILL / "scripts/renumber_findings.py"), str(root), "--apply"],
                capture_output=True, text=True)
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("do not correspond", out.stdout + out.stderr)

    def test_merged_rows_are_pushed_past_the_included_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "report").mkdir(); (root / "work").mkdir()
            (root / "report/REPORT_final.md").write_text(
                "## 1. X\n<!-- finding:ALPHA -->\n<!-- merged:ZETA->ALPHA -->\n")
            (root / "work/findings.jsonl").write_text("\n".join([
                json.dumps({"id": "ALPHA", "ledger_disposition": {"status": "INCLUDED", "target": None}}),
                json.dumps({"id": "ZETA", "ledger_disposition": {"status": "MERGED", "target": "ALPHA"}}),
            ]) + "\n")
            out = subprocess.run(
                [sys.executable, str(SKILL / "scripts/renumber_findings.py"), str(root), "--apply"],
                capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
            rows = [json.loads(l) for l in (root / "work/findings.jsonl").read_text().splitlines() if l.strip()]
            by = {r["id"]: r for r in rows}
            self.assertEqual(sorted(by), ["F1", "F2"])
            self.assertEqual(by["F2"]["ledger_disposition"]["target"], "F1")
            self.assertIn("<!-- merged:F2->F1 -->", (root / "report/REPORT_final.md").read_text())

    def test_tested_negative_markers_are_renumbered_with_everything_else(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "report").mkdir(); (root / "work").mkdir()
            (root / "report/REPORT_final.md").write_text(
                "## 1. X\n<!-- finding:ALPHA -->\n"
                "## Appendix B\n<!-- tested-negative:OMEGA -->\n")
            (root / "work/findings.jsonl").write_text("\n".join([
                json.dumps({"id": "ALPHA",
                            "ledger_disposition": {"status": "INCLUDED", "target": None}}),
                json.dumps({"id": "OMEGA",
                            "ledger_disposition": {"status": "EXCLUDED", "target": None}}),
            ]) + "\n")
            out = subprocess.run(
                [sys.executable, str(SKILL / "scripts/renumber_findings.py"), str(root), "--apply"],
                capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
            report = (root / "report/REPORT_final.md").read_text()
            self.assertIn("<!-- tested-negative:F2 -->", report)
            self.assertNotIn("OMEGA", report)

    def test_disposition_reasons_stop_naming_ids_that_no_longer_exist(self) -> None:
        """A MERGED row's reason is prose that names its target; it was left pre-renumber."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "report").mkdir(); (root / "work").mkdir()
            (root / "report/REPORT_final.md").write_text(
                "## 1. X\n<!-- finding:ALPHA -->\n## 2. Y\n<!-- finding:BETA -->\n")
            (root / "work/findings.jsonl").write_text("\n".join([
                json.dumps({"id": "ALPHA",
                            "ledger_disposition": {"status": "INCLUDED", "target": None}}),
                json.dumps({"id": "BETA",
                            "ledger_disposition": {"status": "INCLUDED", "target": None}}),
                json.dumps({"id": "ZETA", "ledger_disposition": {
                    "status": "MERGED", "target": "ALPHA",
                    "reason": "duplicate of ALPHA; see also BETA"}}),
            ]) + "\n")
            out = subprocess.run(
                [sys.executable, str(SKILL / "scripts/renumber_findings.py"), str(root), "--apply"],
                capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
            rows = {r["id"]: r for r in (json.loads(l) for l
                    in (root / "work/findings.jsonl").read_text().splitlines() if l.strip())}
            self.assertEqual(rows["F3"]["ledger_disposition"]["reason"],
                             "duplicate of F1; see also F2")

    def test_a_reason_naming_an_id_that_is_also_a_target_is_not_double_rewritten(self) -> None:
        """Negative control: F1->F2 and F2->F3 pending at once must not cascade."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "report").mkdir(); (root / "work").mkdir()
            # ALPHA takes F1 while the row already called F1 becomes F2, so the mapping is
            # {ALPHA: F1, F1: F2} -- a NEW id colliding with an OLD one, which is the shape a
            # key-by-key substitution gets wrong however it orders the keys.
            (root / "report/REPORT_final.md").write_text(
                "## 1. X\n<!-- finding:ALPHA -->\n## 2. Y\n<!-- finding:F1 -->\n")
            (root / "work/findings.jsonl").write_text("\n".join([
                json.dumps({"id": "ALPHA",
                            "ledger_disposition": {"status": "INCLUDED", "target": None}}),
                json.dumps({"id": "F1", "ledger_disposition": {
                    "status": "INCLUDED", "target": None,
                    "reason": "follows from ALPHA, not F1"}}),
            ]) + "\n")
            out = subprocess.run(
                [sys.executable, str(SKILL / "scripts/renumber_findings.py"), str(root), "--apply"],
                capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
            rows = {r["id"]: r for r in (json.loads(l) for l
                    in (root / "work/findings.jsonl").read_text().splitlines() if l.strip())}
            self.assertEqual(rows["F2"]["ledger_disposition"]["reason"],
                             "follows from F1, not F2")
            self.assertNotIn("ALPHA", (root / "work/findings.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
