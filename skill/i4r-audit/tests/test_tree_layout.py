"""The 2.5.0 tree layout: data/ (inputs), runs/ (audits), comments/ + metadata/ (publications).

scripts/tree_layout.sh is the one place that knows the layout. These tests drive the real scripts
on a scratch tree in BOTH layouts; nothing here greps source.
"""
import atexit
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import zipfile

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"


def layout(fn: str, *paths: str) -> str:
    out = subprocess.run(["/bin/bash", str(SCRIPTS / "tree_layout.sh"), fn, *paths],
                         text=True, capture_output=True, timeout=30)
    return out.stdout.strip()


# 2.7.5: new_audit.sh checks the critic route with one Codex call before it reserves a paper; tests answer it with
# the fake CLI and keep its cache out of the tester's home
_ACCESS_STATE = tempfile.mkdtemp(prefix="i4r-tree-layout-access-")
atexit.register(shutil.rmtree, _ACCESS_STATE, True)


def sh(*command: str, env: dict | None = None) -> subprocess.CompletedProcess:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("I4R_")}
    environment.update(I4R_RESERVE_GRACE="0", I4R_OWNER="tester@fixture",
                       CODEX=str(SCRIPTS.parent / "tests" / "fake_codex.sh"),
                       FAKE_CODEX_STATE=str(pathlib.Path(_ACCESS_STATE) / "fake"),
                       I4R_STATE_DIR=str(pathlib.Path(_ACCESS_STATE) / "home"),
                       CODEX_HOME=str(pathlib.Path(_ACCESS_STATE) / "codex_home"))
    environment.update(env or {})
    return subprocess.run([str(c) for c in command], text=True, capture_output=True, timeout=180, env=environment)


def make_paper(root: pathlib.Path, journal: str, paper: str, *, pdfs=("article.pdf",), zips=("pkg.zip",)) -> pathlib.Path:
    folder = root / "data" / journal / paper
    folder.mkdir(parents=True, exist_ok=True)
    for name in pdfs:
        (folder / name).write_bytes(b"%PDF-1.4\n% fixture\n")
    for name in zips:
        with zipfile.ZipFile(folder / name, "w") as zf:
            zf.writestr("analysis.R", "x <- read.csv('data.csv')\n")
            zf.writestr("data.csv", "a,b\n1,2\n")
    return folder


class LayoutFunctions(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-layout-")
        self.root = pathlib.Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_new_layout_paths_round_trip(self) -> None:
        paper = self.root / "tree" / "data" / "J" / "P"
        paper.mkdir(parents=True)
        self.assertEqual(layout("i4r_layout_of_paper", str(paper)), "new")
        self.assertEqual(layout("i4r_root_of_paper", str(paper)), str(self.root / "tree"))
        audit = layout("i4r_audit_dir_for_paper", str(paper))
        self.assertEqual(audit, str(self.root / "tree" / "runs" / "J" / "P" / "audit"))
        self.assertEqual(layout("i4r_layout_of_audit", audit), "new")
        self.assertEqual(layout("i4r_root_of_audit", audit), str(self.root / "tree"))
        self.assertEqual(layout("i4r_paper_dir_of_audit", audit), str(paper))
        self.assertEqual(layout("i4r_journal_of_audit", audit), "J")

    def test_legacy_layout_is_still_recognised(self) -> None:
        paper = self.root / "tree" / "J" / "P"
        (paper / "audit").mkdir(parents=True)
        self.assertEqual(layout("i4r_layout_of_paper", str(paper)), "legacy")
        self.assertEqual(layout("i4r_root_of_paper", str(paper)), str(self.root / "tree"))
        self.assertEqual(layout("i4r_audit_dir_for_paper", str(paper)), str(paper / "audit"))
        self.assertEqual(layout("i4r_layout_of_audit", str(paper / "audit")), "legacy")
        self.assertEqual(layout("i4r_root_of_audit", str(paper / "audit")), str(self.root / "tree"))
        self.assertEqual(layout("i4r_paper_dir_of_audit", str(paper / "audit")), str(paper))

    def test_sandbox_hostile_title_gets_a_clean_run_folder(self) -> None:
        paper = self.root / "tree" / "data" / "J" / "Does it work?"
        paper.mkdir(parents=True)
        audit = layout("i4r_audit_dir_for_paper", str(paper))
        self.assertEqual(audit, str(self.root / "tree" / "runs" / "J" / "Does it work" / "audit"))
        # the run folder no longer names the paper exactly, so the input folder is NOT derived from it
        self.assertEqual(layout("i4r_paper_dir_of_audit", audit), str(self.root / "tree" / "runs" / "J" / "Does it work"))

    def test_an_audit_placed_under_data_is_detected(self) -> None:
        inside = self.root / "tree" / "data" / "J" / "P" / "audit"
        outside = self.root / "tree" / "runs" / "J" / "P" / "audit"
        self.assertEqual(subprocess.run(["/bin/bash", str(SCRIPTS / "tree_layout.sh"), "i4r_audit_is_inside_data", str(inside)]).returncode, 0)
        self.assertNotEqual(subprocess.run(["/bin/bash", str(SCRIPTS / "tree_layout.sh"), "i4r_audit_is_inside_data", str(outside)]).returncode, 0)


class NewAuditIntake(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-intake-")
        self.root = pathlib.Path(self.tmp.name).resolve() / "tree"
        (self.root / "reservations").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_new_audit_derives_the_run_folder_and_reserves_the_paper(self) -> None:
        paper = make_paper(self.root, "Journal A", "Paper One")
        result = sh("/bin/bash", SCRIPTS / "new_audit.sh", paper)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        audit = self.root / "runs" / "Journal A" / "Paper One" / "audit"
        self.assertTrue((audit / "pkg" / "analysis.R").is_file(), result.stdout)
        self.assertTrue((audit / "paper" / "paper.pdf").is_file())
        source = json.loads((audit / "paper_source.json").read_text())
        self.assertEqual(source, {"schema_version": "i4r-paper-source-1", "journal": "Journal A", "paper": "Paper One"})
        claims = list((self.root / "reservations").glob("*.json"))
        self.assertEqual(len(claims), 1, claims)
        self.assertEqual(pathlib.Path(json.loads(claims[0].read_text())["paper"]).name, "Paper One")
        self.assertIn(f'export AUDIT_DIR="{audit}"', result.stdout)
        # nothing was written into data/
        self.assertEqual(sorted(p.name for p in paper.iterdir()), ["article.pdf", "pkg.zip"])

    def test_new_audit_strips_sandbox_hostile_characters_but_records_the_true_name(self) -> None:
        paper = make_paper(self.root, "Journal A", "Really?")
        result = sh("/bin/bash", SCRIPTS / "new_audit.sh", paper)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        audit = self.root / "runs" / "Journal A" / "Really" / "audit"
        self.assertTrue(audit.is_dir())
        self.assertEqual(json.loads((audit / "paper_source.json").read_text())["paper"], "Really?")

    def test_new_audit_prefers_the_article_over_a_comment_and_refuses_ambiguity(self) -> None:
        paper = make_paper(self.root, "Journal A", "Paper Two", pdfs=("article.pdf", "I4R_comment_Someone_2026.pdf"))
        result = sh("/bin/bash", SCRIPTS / "new_audit.sh", paper)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("article.pdf", result.stdout)
        ambiguous = make_paper(self.root, "Journal A", "Paper Three", pdfs=("a.pdf", "b.pdf"))
        result = sh("/bin/bash", SCRIPTS / "new_audit.sh", ambiguous)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("2 candidate article PDFs", result.stderr)
        self.assertIn("a.pdf", result.stderr)
        self.assertFalse((self.root / "runs" / "Journal A" / "Paper Three").exists())

    def test_new_audit_refuses_a_paper_outside_data(self) -> None:
        legacy = self.root / "Journal A" / "Old Paper"
        legacy.mkdir(parents=True)
        result = sh("/bin/bash", SCRIPTS / "new_audit.sh", legacy)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not under <root>/data/", result.stderr)

    def test_setup_workspace_refuses_an_audit_dir_under_data(self) -> None:
        paper = make_paper(self.root, "Journal A", "Paper Four")
        result = sh("/bin/bash", SCRIPTS / "setup_workspace.sh", paper / "audit", paper / "article.pdf", paper / "pkg.zip")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("under data/, which holds inputs only", result.stderr)
        self.assertIn("new_audit.sh", result.stderr)
        self.assertFalse((paper / "audit").exists())

    def test_reservation_root_and_published_guard_follow_the_new_layout(self) -> None:
        paper = make_paper(self.root, "Journal A", "Paper Five")
        result = sh("/bin/bash", SCRIPTS / "reserve_paper.sh", "claim", paper)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(list((self.root / "reservations").glob("Journal_A*Paper_Five*.json")), list((self.root / "reservations").iterdir()))
        self.assertFalse((self.root / "data" / "reservations").exists())
        sh("/bin/bash", SCRIPTS / "reserve_paper.sh", "release", paper)
        (self.root / "comments" / "Journal A" / "Paper Five").mkdir(parents=True)
        result = sh("/bin/bash", SCRIPTS / "reserve_paper.sh", "claim", paper)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already published", result.stdout)


if __name__ == "__main__":
    unittest.main()
