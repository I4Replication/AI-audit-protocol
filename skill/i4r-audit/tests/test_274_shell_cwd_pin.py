"""2.7.4: every Phase-1 Claude spawn (the capability probe, the five auditors, a focused repair and the post-fan-out
verifier) runs with CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1, and the coordinator (no Bash tool) does not.

On Claude CLI 2.1.206 the macOS sandbox profile is passed as one argv string on every Bash call, at 0.77 to 0.91
of ARG_MAX for the audit roots under runs/. After a `cd` into the deposit the CLI rebuilt the profile from the
new directory when its settings changed, the profile passed ARG_MAX, and every later command failed with E2BIG:
two of the five auditors of audit A15 lost their shell. With the variable the CLI resets its
tracked directory after every call. These tests run the real runners with the fake CLIs, which record the
variable each spawn received; they fail on 2.7.3, which never set it.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "tests"))
import run_phase1  # noqa: E402
import test_264_verifier_runtime as verifier_tests  # noqa: E402  (the Phase-1 + verifier fixture)
import test_274_output_cap as cap_tests  # noqa: E402  (the coordinator fixture with its env log)
import test_run_phase1 as phase1_tests  # noqa: E402  (the Phase-1 runner fixture)

VAR = "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR"


def operator_env(value: str | None) -> dict[str, str]:
    """The operator's environment with the variable unset, or set to ``value``."""
    env = {key: item for key, item in os.environ.items() if key != VAR}
    if value is not None:
        env[VAR] = value
    return env


class Phase1ShellPin(phase1_tests.Phase1RunnerTests):
    def test_the_probe_and_every_auditor_pin_the_shell_whatever_the_operator_exported(self) -> None:
        self.assertEqual(run_phase1.PHASE1_SHELL_ENV, {VAR: "1"})
        for exported in (None, "0", "1"):
            with self.subTest(exported=exported):
                self.tearDown(); self.setUp()
                with mock.patch.dict(os.environ, operator_env(exported), clear=True):
                    result = self.run_runner("success")
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                starts = self.starts()
                self.assertEqual(len(starts), 6, "the probe and the five auditors")
                self.assertEqual({row["role"] for row in starts} - {"phase1_capability_probe"},
                                 set(run_phase1.runtime.ROLE_SPECS))
                self.assertEqual({row["shell_cwd_pin"] for row in starts}, {"1"})


class VerifierShellPin(verifier_tests.VerifierFixture):
    def test_the_verifier_spawn_pins_the_shell(self) -> None:
        with mock.patch.dict(os.environ, operator_env(None), clear=True):
            root = self.audit_after_phase1()
            result = self.run_verifier(root, ["F13"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual([row["shell_cwd_pin"] for row in self.verifier_starts()], ["1"])
        self.assertEqual({row["shell_cwd_pin"] for row in self.starts()}, {"1"})


class CoordinatorIsNotPinned(cap_tests.CoordinatorOutputCap):
    """Negative control: the coordinator has no Bash tool, is spawned by run_coordinator.run_provider, and keeps the
    2.7.3 environment apart from the output cap."""

    def test_the_coordinator_spawn_does_not_get_the_variable(self) -> None:
        self.upgrade_to_metadata_21()
        with mock.patch.dict(os.environ, operator_env(None), clear=True):
            result = self.run_cap("output_cap_then_success")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        seen = [json.loads(line) for line in self.env_log.read_text().splitlines() if line]
        self.assertEqual(len(seen), 2)
        self.assertEqual({row[VAR] for row in seen}, {None})


def load_tests(loader, tests, pattern):
    """Only the tests defined here; the borrowed fixtures' own tests run in their own files."""
    suite = unittest.TestSuite()
    for cls in (Phase1ShellPin, VerifierShellPin, CoordinatorIsNotPinned):
        for name in sorted(vars(cls)):
            if name.startswith("test"):
                suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
