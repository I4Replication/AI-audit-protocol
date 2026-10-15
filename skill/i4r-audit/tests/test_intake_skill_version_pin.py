"""init_run.sh refuses a skill copy whose VERSION differs from the tree's canonical one (2.4.11).

Two of the three audits started after 2.4.10 shipped ran from other machines: one from a 2.2.0
copy, one from a copy with no VERSION file. Both stamped themselves faithfully and then met
defects fixed releases earlier. The discipline tree syncs the canonical skill to every machine,
so intake can compare.
"""
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


def skill_copy(dest: pathlib.Path, version: str | None) -> pathlib.Path:
    dest.mkdir(parents=True)
    for sub in ("scripts", "schemas", "config", "prompts"):
        shutil.copytree(SKILL / sub, dest / sub, ignore=shutil.ignore_patterns("__pycache__", "tests", "fixtures"))
    (dest / "SKILL.md").write_text("# fixture\n")
    if version is not None:
        (dest / "VERSION").write_text(version + "\n")
    return dest


def init(audit: pathlib.Path, skill_dir: pathlib.Path, **env: str) -> subprocess.CompletedProcess:
    (audit / "pkg").mkdir(parents=True)
    (audit / "pkg" / "f.csv").write_text("x\n")
    environment = {k: v for k, v in os.environ.items()
                   if k not in ("I4R_ALLOW_SKILL_VERSION_MISMATCH", "I4R_CANONICAL_SKILL_DIR")}
    environment.update(LC_ALL="C", **env)
    return subprocess.run(
        ["/bin/bash", str(skill_dir / "scripts" / "init_run.sh"), str(audit), "--audit-id", "t", "--doi", "10.1234/x",
         "--auditor-model", claude_routes.SHIPPED["requested_model"], "--auditor-family", "claude",
         "--auditor-provider", claude_routes.SHIPPED["provider"], "--skill-dir", str(skill_dir)],
        text=True, capture_output=True, timeout=120, env=environment)


class SkillVersionPin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-pin-")
        self.tree = pathlib.Path(self.tmp.name) / "tree"
        self.canonical = skill_copy(self.tree / "skill" / "i4r-audit-unified" / "i4r-audit", "9.9.9")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_stale_copy_is_refused_naming_both_versions(self) -> None:
        stale = skill_copy(pathlib.Path(self.tmp.name) / "stale", "2.2.0")
        result = init(self.tree / "Journal" / "Paper" / "audit", stale)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("version '2.2.0'", result.stderr)
        self.assertIn("canonical skill is 9.9.9", result.stderr)
        self.assertFalse((self.tree / "Journal" / "Paper" / "audit" / "run.json").exists())

    def test_an_unversioned_copy_is_refused(self) -> None:
        unversioned = skill_copy(pathlib.Path(self.tmp.name) / "unversioned", None)
        result = init(self.tree / "Journal" / "Paper2" / "audit", unversioned)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("version 'unversioned'", result.stderr)

    def test_the_canonical_copy_itself_passes(self) -> None:
        result = init(self.tree / "Journal" / "Paper3" / "audit", self.canonical)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("WARNING: running skill version", result.stderr)

    def test_the_override_downgrades_to_a_warning_and_the_stale_stamp_is_recorded(self) -> None:
        stale = skill_copy(pathlib.Path(self.tmp.name) / "stale2", "2.2.0")
        result = init(self.tree / "Journal" / "Paper4" / "audit", stale, I4R_ALLOW_SKILL_VERSION_MISMATCH="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARNING: running skill version '2.2.0' differs", result.stderr)
        run = (self.tree / "Journal" / "Paper4" / "audit" / "run.json").read_text()
        self.assertIn('"protocol_version": "2.2.0"', run)

    def test_outside_a_tree_with_a_canonical_copy_nothing_is_checked(self) -> None:
        stale = skill_copy(pathlib.Path(self.tmp.name) / "stale3", "2.2.0")
        result = init(pathlib.Path(self.tmp.name) / "elsewhere" / "audit", stale)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_canonical_location_can_be_pointed_elsewhere(self) -> None:
        other = skill_copy(pathlib.Path(self.tmp.name) / "other-canonical", "2.2.0")
        stale = skill_copy(pathlib.Path(self.tmp.name) / "stale4", "2.2.0")
        result = init(self.tree / "Journal" / "Paper5" / "audit", stale, I4R_CANONICAL_SKILL_DIR=str(other))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
