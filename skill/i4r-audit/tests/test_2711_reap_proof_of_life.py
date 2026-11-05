#!/usr/bin/env python3
"""2.7.11: `reserve_paper.sh reap` must not judge a live audit by an archived copy of the same paper.

When the basename globs find no journal (a title whose ?*[]{}\\ characters new_audit.sh drops from the run folder, or
a workspace relocated with I4R_PAPER_DIR), reap searched the tree for paper_source.json and stopped at the FIRST file
naming the paper. After the archive-and-re-run operations this tree goes through, that file can be an archived audit's
in old_runs/: reap then read the archived journal (idle for weeks), printed WOULD REAP, and with --force deleted the
claim of an audit whose journal was written two hours earlier. Each case below builds the tree on disk and runs the
real script as a dry run. (Which copy rglob reaches first is the file system's order; on APFS the archived copy came
first in every layout tried. The 2.7.11 assertions hold in any order.) `list` also ended with a traceback at a claim
whose claimed_at had another format, listing nothing after it.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unicodedata
import unittest


SKILL = Path(__file__).resolve().parent.parent
RESERVE = SKILL / "scripts" / "reserve_paper.sh"


def claim_file(root: Path, journal: str, paper: str, owner: str, days: float) -> Path:
    """A claim as reserve_paper.sh claim writes it: the hashed name for a title with characters it sanitises."""
    raw = unicodedata.normalize("NFC", f"{journal}__{paper}")
    safe = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9._-]+", "_", raw))[:180]
    if not re.fullmatch(r"[A-Za-z0-9._-]*", raw) or len(raw.encode("utf-8")) > 180:
        safe = safe[:170] + "-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    (root / "reservations").mkdir(exist_ok=True)
    path = root / "reservations" / f"{safe}.json"
    path.write_text(json.dumps({"owner": owner, "paper": str(root / "data" / journal / paper),
                                "claimed_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "active"}), encoding="utf-8")
    return path


def audit_dir(path: Path, paper: str, *, journal_hours: float | None) -> Path:
    path.mkdir(parents=True)
    (path / "paper_source.json").write_text(json.dumps({"journal": "J", "paper": paper}), encoding="utf-8")
    if journal_hours is not None:
        journal = path / "run.events.jsonl"
        journal.write_text("", encoding="utf-8")
        stamp = time.time() - journal_hours * 3600
        os.utime(journal, (stamp, stamp))
    else:
        (path / "pkg").mkdir()
        (path / "package_manifest.json").write_text("{}", encoding="utf-8")
    return path


class ReapProofOfLife2711Test(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="i4r-reap-2711-")
        self.root = Path(self._tmp.name).resolve()
        (self.root / "data" / "J").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def reap(self) -> str:
        result = subprocess.run(["bash", str(RESERVE), "reap", str(self.root)], text=True, capture_output=True,
                                env={**os.environ, "I4R_OWNER": "me@here"}, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result.stdout

    def test_a_live_audit_in_a_sanitised_run_folder_is_not_judged_by_its_archived_copy(self) -> None:
        paper = "Paper h?"   # new_audit.sh runs it in runs/J/Paper h/
        (self.root / "data" / "J" / paper).mkdir()
        claim = claim_file(self.root, "J", paper, "me@here", days=8)
        audit_dir(self.root / "runs" / "J" / "Paper h" / "audit", paper, journal_hours=2)
        audit_dir(self.root / "old_runs" / "archived_2026-10-19_x" / "runs" / "J" / "Paper h" / "audit", paper,
                  journal_hours=480)
        out = self.reap()
        self.assertIn("ACTIVE", out)
        self.assertIn("0 reapable, 1 held", out)
        self.assertTrue(claim.is_file())

    def test_a_relocated_live_audit_is_not_judged_by_its_archived_copy(self) -> None:
        paper = "Paper f?"
        (self.root / "data" / "J" / paper).mkdir()
        claim_file(self.root, "J", paper, "me@here", days=8)
        audit_dir(self.root / ".work" / "audits" / "J_Paper_f", paper, journal_hours=2)
        audit_dir(self.root / "old_runs" / "archived" / "J" / "Paper f" / "audit", paper, journal_hours=480)
        out = self.reap()
        self.assertIn("ACTIVE", out)
        self.assertIn("0 reapable, 1 held", out)

    def test_an_intake_with_no_journal_is_held_whatever_an_archived_copy_says(self) -> None:
        """The version-pin refusal leaves a claimed intake with no journal; an archived run of the paper proves nothing."""
        paper = "Paper e?"
        (self.root / "data" / "J" / paper).mkdir()
        claim_file(self.root, "J", paper, "me@here", days=8)
        audit_dir(self.root / "runs" / "J" / "Paper e" / "audit", paper, journal_hours=None)
        audit_dir(self.root / "old_runs" / "archived" / "runs" / "J" / "Paper e" / "audit", paper, journal_hours=480)
        out = self.reap()
        self.assertIn("UNKNOWN", out)
        self.assertIn("no journal yet", out)
        self.assertIn("0 reapable, 1 held", out)

    def test_an_abandoned_claim_is_still_reapable(self) -> None:
        """Positive control: the fix must not hold everything."""
        paper = "Paper c?"
        (self.root / "data" / "J" / paper).mkdir()
        claim_file(self.root, "J", paper, "me@here", days=8)
        audit_dir(self.root / "runs" / "J" / "Paper c" / "audit", paper, journal_hours=216)
        audit_dir(self.root / "old_runs" / "archived" / "runs" / "J" / "Paper c" / "audit", paper, journal_hours=480)
        out = self.reap()
        self.assertIn("WOULD REAP", out)
        self.assertIn("journal idle 9d", out)

    def test_an_archived_sibling_found_by_the_globs_does_not_decide_alone(self) -> None:
        """A plain title: the basename globs reach runs/J/<paper>/audit.failed-*, an archived sibling. When that is all
        they find, the live audits that name the paper are looked up too (a relocated one with a fresh journal, an
        intake with none), instead of reaping by the archived copy's old journal."""
        (self.root / "data" / "J" / "Paper r").mkdir()
        claim_file(self.root, "J", "Paper r", "me@here", days=8)
        audit_dir(self.root / "runs" / "J" / "Paper r" / "audit.failed-20261010", "Paper r", journal_hours=480)
        audit_dir(self.root / ".work" / "audits" / "J_Paper_r", "Paper r", journal_hours=2)
        (self.root / "data" / "J" / "Paper i").mkdir()
        claim_file(self.root, "J", "Paper i", "me@here", days=8)
        audit_dir(self.root / "runs" / "J" / "Paper i" / "audit.failed-20261010", "Paper i", journal_hours=480)
        audit_dir(self.root / "runs" / "J" / "Paper i" / "audit", "Paper i", journal_hours=None)
        out = self.reap()
        self.assertRegex(out, r"ACTIVE .*Paper r")
        self.assertIn("no journal yet: Paper i", out)
        self.assertIn("0 reapable, 2 held", out)

    def test_a_stale_live_journal_beside_an_archived_sibling_is_still_reapable(self) -> None:
        """Positive control for the globs: a live journal idle past the window decides, archived sibling or not."""
        (self.root / "data" / "J" / "Paper s").mkdir()
        claim_file(self.root, "J", "Paper s", "me@here", days=8)
        audit_dir(self.root / "runs" / "J" / "Paper s" / "audit.failed-20261010", "Paper s", journal_hours=480)
        audit_dir(self.root / "runs" / "J" / "Paper s" / "audit", "Paper s", journal_hours=216)
        out = self.reap()
        self.assertIn("WOULD REAP", out)
        self.assertIn("journal idle 9d", out)

    def test_list_survives_a_claim_it_cannot_parse(self) -> None:
        for name, at in (("J__P1", "2026-10-30T10:00:00Z"), ("J__P2", "2026-10-30T10:00:00.123Z"),
                         ("J__P3", "2026-10-30T10:00:00Z")):
            (self.root / "reservations").mkdir(exist_ok=True)
            (self.root / "reservations" / f"{name}.json").write_text(json.dumps(
                {"owner": f"{name}@h", "paper": f"/x/data/J/{name}", "claimed_at": at, "status": "active"}))
        result = subprocess.run(["bash", str(RESERVE), "list", str(self.root)], text=True, capture_output=True,
                                check=False)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("UNREADABLE  J__P2.json", result.stdout)
        self.assertIn("J__P3@h", result.stdout)


if __name__ == "__main__":
    unittest.main()
