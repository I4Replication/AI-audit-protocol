"""reserve_paper.sh reap must not die on a claim whose audit exists but has no journal yet.

paper_source.json named the paper, the audit had no run.events.jsonl (intake done, init_run.sh
never run), and the sweep fell through to an f-string with idle=None: a TypeError outside the
per-claim try/except that killed the whole sweep and released nothing.
"""
import datetime as dt
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
RESERVE = SKILL / "scripts" / "reserve_paper.sh"


class ReapSurvivesIntakeOnlyAudit(unittest.TestCase):
    def test_a_journal_less_audit_is_held_not_a_traceback(self):
        with tempfile.TemporaryDirectory(prefix="i4r-reap-") as tmp:
            root = pathlib.Path(tmp)
            (root / "reservations").mkdir()
            (root / "Completed reports").mkdir()
            old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
            # a stale claim for a paper folder that no longer exists at its literal path
            (root / "reservations" / "Journal__Paper.json").write_text(json.dumps({
                "owner": "tester@host", "paper": str(root / "Journal" / "Paper"),
                "claimed_at": old, "status": "active"}, indent=2))
            # ... and a relocated, intake-only audit that names it
            audit = root / ".work" / "audits" / "clean" / "audit"
            audit.mkdir(parents=True)
            (audit / "paper_source.json").write_text(json.dumps({
                "schema_version": "i4r-paper-source-1", "journal": "Journal", "paper": "Paper"}))
            # a second, genuinely reapable claim AFTER it in sort order proves the sweep continued
            (root / "reservations" / "Zjournal__Zpaper.json").write_text(json.dumps({
                "owner": "tester@host", "paper": str(root / "Zjournal" / "Zpaper"),
                "claimed_at": old, "status": "active"}, indent=2))
            r = subprocess.run(
                ["/bin/bash", str(RESERVE), "reap", str(root)],
                text=True, capture_output=True, timeout=120,
                env=dict(os.environ, I4R_OWNER="tester@host", I4R_RESERVE_GRACE="0"))
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("UNKNOWN", r.stdout)
            self.assertIn("Paper", r.stdout)
            self.assertIn("Zpaper", r.stdout, "the sweep did not reach the claim after the bad one")


if __name__ == "__main__":
    unittest.main()
