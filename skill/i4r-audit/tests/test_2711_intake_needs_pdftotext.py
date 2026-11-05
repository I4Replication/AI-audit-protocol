"""2.7.11: intake refuses to start when pdftotext (Poppler) is not installed.

setup_workspace.sh extracted the article text with `command -v pdftotext && pdftotext -layout ... || true`: on a host
without Poppler it printed "paper text: EXTRACTION FAILED" and went on, and Phase 1's paper_text() then fell back to
PAPER.txt, the operator's citation and summary, as if it were the article. These tests run the real new_audit.sh and
setup_workspace.sh on a scratch tree with a PATH that has no pdftotext: both refuse before anything is reserved,
copied or paid for. With Poppler present, a PDF it cannot read (here a stub, as a scan would be) is still taken with
today's note.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
import unittest

from tests.test_tree_layout import SCRIPTS, make_paper, sh


def path_without_pdftotext() -> str:
    """The current PATH minus every folder that holds a pdftotext."""
    return os.pathsep.join(folder for folder in os.environ.get("PATH", "").split(os.pathsep)
                           if folder and not os.path.exists(os.path.join(folder, "pdftotext")))


class IntakeNeedsPdftotext(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="i4r-2711-poppler-")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(lambda: sh("chmod", "-R", "u+w", tmp.name))
        self.root = pathlib.Path(tmp.name).resolve() / "tree"
        (self.root / "reservations").mkdir(parents=True)
        self.paper = make_paper(self.root, "J", "P")
        self.audit = self.root / "runs" / "J" / "P" / "audit"

    def nothing_reserved_or_copied(self) -> None:
        self.assertEqual(list((self.root / "reservations").iterdir()), [])
        self.assertFalse((self.root / "runs").exists())

    def test_new_audit_without_pdftotext_refuses_before_the_access_call_and_the_claim(self) -> None:
        bare = path_without_pdftotext()
        self.assertIsNone(shutil.which("pdftotext", path=bare))
        # a Codex that does not exist: the refusal must come before the critic-access call
        result = sh("/bin/bash", SCRIPTS / "new_audit.sh", self.paper,
                    env={"PATH": bare, "CODEX": str(self.root / "no-such-codex")})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("pdftotext (Poppler) is not on PATH", result.stderr)
        self.assertIn("nothing reserved or copied", result.stderr)
        self.nothing_reserved_or_copied()

    def test_setup_workspace_called_directly_refuses_too(self) -> None:
        result = sh("/bin/bash", SCRIPTS / "setup_workspace.sh", self.audit, self.paper / "article.pdf",
                    self.paper / "pkg.zip", env={"PATH": path_without_pdftotext(), "I4R_PAPER_DIR": str(self.paper)})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("setup_workspace.sh: no audit started", result.stderr)
        self.nothing_reserved_or_copied()

    @unittest.skipUnless(shutil.which("pdftotext"), "needs Poppler on PATH")
    def test_control_with_poppler_an_unreadable_pdf_keeps_todays_note(self) -> None:
        result = sh("/bin/bash", SCRIPTS / "new_audit.sh", self.paper)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("EXTRACTION FAILED", result.stdout)
        self.assertFalse((self.audit / "paper" / "paper.txt").exists())
        self.assertTrue((self.audit / "paper" / "paper.pdf").is_file())
        self.assertEqual(len(list((self.root / "reservations").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
