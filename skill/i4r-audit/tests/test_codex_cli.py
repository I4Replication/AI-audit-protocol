"""The skill finds the Codex CLI instead of naming one path (2.6.10).

The ChatGPT app update of 2026-09-28 moved its bundled CLI from ``Contents/Resources/codex`` to
``Contents/Resources/codex-cli/bin/codex``. run_critique.sh, capture_environment.sh and the recovery
scripts all defaulted to the old path, so every critique round on an updated Mac stopped with "Codex
executable is not executable" until CODEX was set by hand. ``scripts/codex_cli.py`` now picks the
newest working CLI among the known install locations and every ``codex`` on PATH; an explicit CODEX
or I4R_CODEX_BIN still wins. No test here reaches a provider: the fake CLIs only answer --version,
and the driver case runs tests/fake_codex.sh.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import codex_cli  # noqa: E402
import test_snapshot_mode_drift as drift  # noqa: E402  (the real-round critique fixture)

DRIVER = SCRIPTS / "run_critique.sh"
FAKE_CODEX = TESTS / "fake_codex.sh"


def fake_cli(path: pathlib.Path, version_line: str, *, executable: bool = True, rc: int = 0) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n[ \"$1\" = --version ] && {{ echo '{version_line}'; exit {rc}; }}\nexit 9\n")
    path.chmod(0o755 if executable else 0o644)
    return path


class VersionOrder(unittest.TestCase):
    def test_release_beats_its_prereleases_and_numbers_compare_numerically(self) -> None:
        ordered = ["garbage", "0.9.9", "0.150.0", "0.158.0-alpha.2", "0.158.0-alpha.2.1", "0.158.0-alpha.10",
                   "0.158.0-beta", "0.158.0", "1.0.0"]
        self.assertEqual(sorted(ordered, key=codex_cli.version_key), ordered)


class Resolution(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="i4r-codex-cli-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.root)], check=False))
        self.bundle_new = self.root / "ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
        self.bundle_old = self.root / "ChatGPT.app/Contents/Resources/codex"
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def env(self, **extra: str) -> dict[str, str]:
        base = {"PATH": str(self.bin), "I4R_CODEX_KNOWN_PATHS": f"{self.bundle_new}:{self.bundle_old}"}
        base.update(extra)
        return base

    def test_the_moved_bundle_is_found_when_the_old_path_is_gone(self) -> None:
        # The 2026-09-28 layout: only codex-cli/bin/codex exists.
        fake_cli(self.bundle_new, "codex-cli 0.158.0-alpha.2.1")
        path, how = codex_cli.resolve(self.env())
        self.assertEqual(path, str(self.bundle_new))
        self.assertIn("0.158.0-alpha.2.1", how)

    def test_the_newest_working_cli_wins_wherever_it_is(self) -> None:
        fake_cli(self.bundle_new, "codex-cli 0.158.0-alpha.2.1")
        fake_cli(self.bundle_old, "codex-cli 0.150.0")
        fake_cli(self.bin / "codex", "codex-cli 0.158.0")          # a newer standalone release on PATH
        self.assertEqual(codex_cli.resolve(self.env())[0], str(self.bin / "codex"))

    def test_a_tie_goes_to_the_bundled_cli(self) -> None:
        fake_cli(self.bundle_new, "codex-cli 0.158.0")
        fake_cli(self.bin / "codex", "codex-cli 0.158.0")
        self.assertEqual(codex_cli.resolve(self.env())[0], str(self.bundle_new))

    def test_broken_candidates_are_skipped(self) -> None:
        fake_cli(self.bundle_new, "codex-cli 9.9.9", executable=False)   # not executable
        fake_cli(self.bundle_old, "codex-cli 9.9.8", rc=1)               # --version fails
        fake_cli(self.bin / "codex", "something else 9.9.7")            # not a Codex CLI
        other = self.root / "bin2"
        fake_cli(other / "codex", "codex-cli 0.100.0")
        path, _ = codex_cli.resolve(self.env(PATH=f"{self.bin}:{other}"))
        self.assertEqual(path, str(other / "codex"))

    def test_a_symlinked_copy_is_one_candidate(self) -> None:
        fake_cli(self.bundle_new, "codex-cli 0.158.0")
        (self.bin / "codex").symlink_to(self.bundle_new)
        self.assertEqual([path for path, _ in codex_cli.survey(self.env())], [str(self.bundle_new)])

    def test_an_explicit_choice_always_wins_even_when_it_is_older_or_missing(self) -> None:
        fake_cli(self.bundle_new, "codex-cli 0.158.0")
        older = fake_cli(self.root / "old/codex", "codex-cli 0.1.0")
        self.assertEqual(codex_cli.resolve(self.env(CODEX=str(older)))[0], str(older))
        self.assertEqual(codex_cli.resolve(self.env(I4R_CODEX_BIN=str(older)))[0], str(older))
        self.assertEqual(codex_cli.resolve(self.env(CODEX="/nowhere/codex", I4R_CODEX_BIN=str(older)))[0],
                         "/nowhere/codex", "CODEX outranks I4R_CODEX_BIN, and a typo is not replaced")

    def test_nothing_working_is_reported_by_the_command_line(self) -> None:
        result = subprocess.run([sys.executable, str(SCRIPTS / "codex_cli.py")], capture_output=True, text=True,
                                env={**self.env(), "PYTHONDONTWRITEBYTECODE": "1"}, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no working Codex CLI found; searched:", result.stderr)
        self.assertIn("set CODEX=/path/to/codex", result.stderr)


class DriverAndEnvironment(unittest.TestCase):
    """The real driver and capture_environment.sh with CODEX and I4R_CODEX_BIN unset."""

    def setUp(self) -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="i4r-codex-driver-")).resolve()
        self.addCleanup(lambda: (drift.thaw(self.work), subprocess.run(["rm", "-rf", str(self.work)], check=False)))
        # A bundle-shaped wrapper around the fake CLI, at the moved location only.
        self.bundle = self.work / "ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
        self.bundle.parent.mkdir(parents=True)
        self.bundle.write_text(f"#!/bin/sh\nexec {FAKE_CODEX} \"$@\"\n")
        self.bundle.chmod(0o755)

    def env(self, known: str, **extra: str) -> dict[str, str]:
        env = drift.env_without_refreeze(I4R_CODEX_KNOWN_PATHS=known, **extra)
        env.pop("CODEX", None)
        env.pop("I4R_CODEX_BIN", None)
        env["PATH"] = os.pathsep.join(   # the operator's PATH, minus any directory holding a codex
            entry for entry in env.get("PATH", "").split(os.pathsep)
            if entry and not os.path.exists(os.path.join(entry, "codex")))
        return env

    def run_round(self, known: str) -> tuple[int, str, pathlib.Path]:
        audit = self.work / "audit"
        drift.write_critique_audit(audit)
        state = self.work / "fake_state"
        log = self.work / "driver.log"
        with log.open("w") as handle:
            result = subprocess.run(["bash", str(DRIVER), "1"], stdout=handle, stderr=subprocess.STDOUT,
                                    env=self.env(known, AUDIT_DIR=str(audit), FAKE_CODEX_STATE=str(state),
                                                 FAKE_CODEX_SCENARIO="success", I4R_INVOCATION_ID="codex-find",
                                                 RETRY_BACKOFF_SECONDS="0", PROBE_LOCK_WAIT_SECONDS="3"),
                                    timeout=600, check=False)
        return result.returncode, log.read_text(), state

    def test_the_driver_runs_a_round_with_the_cli_it_finds(self) -> None:
        rc, log, state = self.run_round(str(self.bundle))
        self.assertEqual(rc, 0, log)
        self.assertGreater(int((state / "calls_total").read_text().split()[0]), 0, "the found CLI was run")
        self.assertNotIn("not executable", log)

    def test_the_driver_refuses_clearly_when_no_cli_works(self) -> None:
        rc, log, state = self.run_round("")
        self.assertNotEqual(rc, 0, log)
        self.assertIn("no working Codex CLI found; searched:", log)
        self.assertFalse((state / "calls_total").exists(), "no provider call without a CLI")

    def test_capture_environment_records_the_found_cli(self) -> None:
        audit = self.work / "env-audit"
        audit.mkdir()
        (audit / "run.json").write_text("{}\n")
        subprocess.run(["bash", str(SCRIPTS / "capture_environment.sh"), str(audit)], env=self.env(str(self.bundle)),
                       capture_output=True, text=True, timeout=300, check=False)
        environment = json.loads((audit / "run.json").read_text()).get("environment") or {}
        self.assertEqual(environment.get("codex_cli"), "codex-cli fake-1.0")


if __name__ == "__main__":
    unittest.main()
