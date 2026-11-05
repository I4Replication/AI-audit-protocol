"""2.7.11: a skill copy older (or newer) than the tree's canonical one is refused before the paper is reserved.

init_run.sh refuses a copy whose VERSION is not the canonical one (2.4.11), but new_audit.sh and setup_workspace.sh
run first: they reserved the paper and copied the deposit, init then refused, and the reservation stayed, so after a
release a teammate still on the previous one left the paper looking taken (and a rerun of new_audit.sh refused: an
audit of this paper already exists). Intake now makes the same comparison before anything is reserved, copied or paid
for. These tests run the real new_audit.sh on a scratch tree whose canonical copy is another version; on 2.7.10
new_audit.sh reserves the paper and copies the deposit, so each refusal test fails there.
"""
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from tests.test_tree_layout import SCRIPTS, make_paper, sh

SKILL = SCRIPTS.parent


class IntakeVersionPin(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="i4r-2711-pin-")
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name).resolve() / "tree"
        (self.root / "reservations").mkdir(parents=True)
        self.canonical = self.root / "skill" / "i4r-audit-unified" / "i4r-audit"
        self.canonical.mkdir(parents=True)
        self.paper = make_paper(self.root, "Journal A", "Paper One")
        self.own = (SKILL / "VERSION").read_text().strip()

    def nothing_reserved_or_copied(self) -> None:
        self.assertEqual(list((self.root / "reservations").iterdir()), [])
        self.assertFalse((self.root / "runs").exists())

    def test_a_copy_other_than_the_canonical_one_reserves_nothing(self) -> None:
        (self.canonical / "VERSION").write_text("9.9.9\n")
        # Review DT-5: new_audit.sh refuses before its critic-access call (a Codex probe), with its own message.
        # A Codex double that leaves a marker, and an empty state folder (no cached pass), show the call never ran.
        probe = self.root.parent / "probe"
        probe.mkdir()
        codex = probe / "codex"
        codex.write_text(f'#!/bin/bash\n: > "{probe}/called"\nexec "{SKILL}/tests/fake_codex.sh" "$@"\n')
        codex.chmod(0o755)
        result = sh("/bin/bash", SCRIPTS / "new_audit.sh", self.paper,
                    env={"CODEX": str(codex), "I4R_STATE_DIR": str(probe / "state")})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(result.stderr.startswith("FATAL: new_audit.sh: no audit started"), result.stderr)
        self.assertFalse((probe / "called").exists(), "the critic-access check ran before the refusal")
        self.assertIn(f"nothing reserved or copied: this skill copy is version '{self.own}'", result.stderr)
        self.assertIn("canonical skill is 9.9.9", result.stderr)
        self.nothing_reserved_or_copied()
        # the same from setup_workspace.sh called on its own (the legacy entry point)
        audit = self.root / "runs" / "Journal A" / "Paper One" / "audit"
        direct = sh("/bin/bash", SCRIPTS / "setup_workspace.sh", audit, self.paper / "article.pdf",
                    self.paper / "pkg.zip", env={"I4R_PAPER_DIR": str(self.paper)})
        self.assertEqual(direct.returncode, 2, direct.stdout + direct.stderr)
        self.nothing_reserved_or_copied()

    def test_the_override_warns_and_starts_the_audit(self) -> None:
        (self.canonical / "VERSION").write_text("9.9.9\n")
        result = sh("/bin/bash", SCRIPTS / "new_audit.sh", self.paper, env={"I4R_ALLOW_SKILL_VERSION_MISMATCH": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"WARNING: running skill version '{self.own}' differs", result.stderr)
        self.assertEqual(len(list((self.root / "reservations").glob("*.json"))), 1)

    def test_the_canonical_version_starts_the_audit(self) -> None:
        (self.canonical / "VERSION").write_text(self.own + "\n")
        result = sh("/bin/bash", SCRIPTS / "new_audit.sh", self.paper)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("WARNING: running skill version", result.stderr)
        claims = list((self.root / "reservations").glob("*.json"))
        self.assertEqual(len(claims), 1, claims)
        self.assertEqual(pathlib.Path(json.loads(claims[0].read_text())["paper"]).name, "Paper One")

    def test_an_intake_left_by_a_refused_init_says_how_to_go_on(self) -> None:
        # what a 2.7.10 install leaves after a release: the intake made, init_run.sh refused, no run.json
        (self.canonical / "VERSION").write_text(self.own + "\n")
        first = sh("/bin/bash", SCRIPTS / "new_audit.sh", self.paper)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        again = sh("/bin/bash", SCRIPTS / "new_audit.sh", self.paper)
        self.assertEqual(again.returncode, 2, again.stdout + again.stderr)
        self.assertIn("an audit of this paper already exists", again.stderr)
        self.assertIn("It holds an intake only (no run.json", again.stderr)
        self.assertIn("resume", again.stderr)

    def test_init_refused_after_intake_says_the_intake_stays_valid(self) -> None:
        # the canonical copy moves on between intake and init (or intake ran from an older install)
        (self.canonical / "VERSION").write_text(self.own + "\n")
        intake = sh("/bin/bash", SCRIPTS / "new_audit.sh", self.paper)
        self.assertEqual(intake.returncode, 0, intake.stdout + intake.stderr)
        (self.canonical / "VERSION").write_text("9.9.9\n")
        audit = self.root / "runs" / "Journal A" / "Paper One" / "audit"
        init = sh("bash", SCRIPTS / "init_run.sh", audit, "--audit-id", "t-2711-pin",
                  "--protocol-version", "test", "--gate-set-version", "test", env={"PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(init.returncode, 2, init.stdout + init.stderr)
        self.assertIn("canonical skill is 9.9.9", init.stderr)
        self.assertIn("run init_run.sh again on this AUDIT_DIR", init.stderr)
        self.assertIn("reserve_paper.sh release", init.stderr)
        self.assertFalse((audit / "run.json").exists())
        self.assertEqual(len(list((self.root / "reservations").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
