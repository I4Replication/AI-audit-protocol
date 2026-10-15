"""Intake and provenance defects, each driven through the shipped shell scripts.

init_run.sh: both provenance stamps fed their file list through an unquoted $(ls ...), which
word-splits on spaces, so on this machine -- where every path has spaces -- every audit recorded
gates-e3b0c442, the SHA-256 of nothing.

setup_workspace.sh: the glob-metacharacter guard tested the audit dir AS TYPED, so a relative
dir under a `?` parent passed; and the paper dir was resolved only inside the reservation block,
so I4R_SKIP_RESERVE=1 silently discarded I4R_PAPER_DIR and never wrote paper_source.json.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
import claude_routes  # noqa: E402  (the model the shipped policy freezes for a new audit)

EMPTY_SHA_PREFIX = "e3b0c442"


def _skill_copy(dest: pathlib.Path) -> pathlib.Path:
    dest.mkdir(parents=True)
    for sub in ("scripts", "schemas", "config", "prompts"):
        shutil.copytree(SKILL / sub, dest / sub,
                        ignore=shutil.ignore_patterns("__pycache__", "tests", "fixtures"))
    (dest / "VERSION").write_text((SKILL / "VERSION").read_text())
    return dest


def _init(audit: pathlib.Path, skill_dir: pathlib.Path) -> subprocess.CompletedProcess:
    (audit / "pkg").mkdir(parents=True)
    (audit / "pkg" / "f.csv").write_text("x\n")
    return subprocess.run(
        ["/bin/bash", str(SCRIPTS / "init_run.sh"), str(audit), "--audit-id", "t", "--doi", "10.1234/x",
         "--auditor-model", claude_routes.SHIPPED["requested_model"], "--auditor-family", "claude",
         "--auditor-provider", claude_routes.SHIPPED["provider"], "--protocol-version", "p1",
         "--skill-dir", str(skill_dir)],
        text=True, capture_output=True, timeout=120, env=dict(os.environ, LC_ALL="C"))


class ProvenanceStampsSurviveSpaces(unittest.TestCase):
    def test_gate_set_and_toolkit_hashes_are_real_and_path_independent(self):
        with tempfile.TemporaryDirectory(prefix="i4r-prov-") as tmp:
            base = pathlib.Path(tmp)
            plain = _skill_copy(base / "skill")
            spaced = _skill_copy(base / "skill with spaces" / "i4r audit")
            results = {}
            for name, skill in (("plain", plain), ("spaced", spaced)):
                r = _init(base / name / "audit", skill)
                self.assertEqual(0, r.returncode, r.stdout + r.stderr)
                run = json.loads((base / name / "audit" / "run.json").read_text())
                results[name] = (run["gate_set_version"], run["toolkit"]["scripts_sha256"])
            for name, (gates, toolkit) in results.items():
                self.assertFalse(gates.endswith(EMPTY_SHA_PREFIX), f"{name}: gate_set_version is sha256(\"\")")
                self.assertFalse(toolkit.startswith(EMPTY_SHA_PREFIX), f"{name}: scripts_sha256 is sha256(\"\")")
            self.assertEqual(results["plain"], results["spaced"], "same files, different stamps")

    def test_a_skill_dir_with_no_gate_files_is_refused(self):
        """Negative control: a digest of nothing must never be stamped."""
        with tempfile.TemporaryDirectory(prefix="i4r-prov-empty-") as tmp:
            empty = pathlib.Path(tmp) / "skill"
            empty.mkdir()
            r = _init(pathlib.Path(tmp) / "audit", empty)
            self.assertNotEqual(0, r.returncode)
            self.assertIn("refusing to stamp a digest of nothing", r.stderr)


class IntakeGuardsSeeTheRealPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-intake-fix-")
        self.base = pathlib.Path(self.tmp.name)
        self.paper = self.base / "paper.pdf"
        self.paper.write_bytes(b"%PDF-1.4\n%%EOF\n")
        self.deposit = self.base / "deposit"
        self.deposit.mkdir()
        (self.deposit / "a.csv").write_text("1,2\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_setup(self, cwd, *args, **env):
        return subprocess.run(
            ["/bin/bash", str(SCRIPTS / "setup_workspace.sh"), *args],
            cwd=str(cwd), text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=120,
            env=dict(os.environ, **env))

    def test_a_relative_audit_dir_under_a_hostile_parent_is_refused(self):
        hostile = self.base / "Journal" / "Does it work?"
        hostile.mkdir(parents=True)
        r = self.run_setup(hostile, "audit", str(self.paper), str(self.deposit), I4R_SKIP_RESERVE="1")
        self.assertEqual(2, r.returncode, r.stdout + r.stderr)
        self.assertIn("disarms the filesystem sandbox", r.stderr)
        self.assertIn(str(hostile), r.stderr, "the refusal must show the absolute path it tested")
        self.assertFalse((hostile / "audit" / "pkg").exists())

    def test_skip_reserve_still_honours_paper_dir_and_writes_paper_source(self):
        true_paper = self.base / "Journal" / "Real Paper"
        true_paper.mkdir(parents=True)
        audit = self.base / "clean" / "audit"
        r = self.run_setup(self.base, str(audit), str(self.paper), str(self.deposit),
                           I4R_SKIP_RESERVE="1", I4R_PAPER_DIR=str(true_paper))
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        source = json.loads((audit / "paper_source.json").read_text())
        self.assertEqual("Real Paper", source.get("paper"))
        self.assertEqual("Journal", source.get("journal"))

    def test_skip_reserve_with_a_missing_paper_dir_is_still_refused(self):
        """Negative control: the I4R_PAPER_DIR existence check is no longer skipped either."""
        r = self.run_setup(self.base, str(self.base / "audit"), str(self.paper), str(self.deposit),
                           I4R_SKIP_RESERVE="1", I4R_PAPER_DIR=str(self.base / "nope"))
        self.assertEqual(2, r.returncode)
        self.assertIn("I4R_PAPER_DIR does not exist", r.stderr)


if __name__ == "__main__":
    unittest.main()
