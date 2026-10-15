"""An audit root containing a glob metacharacter silently disarms the filesystem sandbox.

The Claude CLI compiles ``sandbox.filesystem`` entries as glob-ish patterns, so a rule whose
literal path contains one of ``?*[]{}\\`` matches nothing.  Every allowRead/allowWrite grant is
then inert and seatbelt denies every Bash read and write under the audit root, while the
denyRead/denyWrite protections for ``paper/``, ``pkg/`` and ``run.json`` go inert at the same
moment.  The Write tool separately refuses such a path outright.

A paper folder named "Can automatic reactions mirror exercise dependence?" spent 5,443,051
tokens and $6.43 across five roles and produced no report.  Nothing caught it: the preflight
receipt writes through Python rather than through the sandbox, and the capability probe proved
only that the *Write* tool worked -- the one filesystem path that still succeeds when the rules
are inert, because it takes a relative path under the process cwd and is not seatbelt-confined.

The end-to-end suite cannot catch this class at all: ``tests/fake_claude_phase1.py`` never
emulates seatbelt, so the full five-role fan-out runs green on the exact folder name that
failed in production.  These tests therefore pin the settings emitter and its validator
directly, in the register of ``test_prompt_contract.py`` and ``test_error_classification.py``.

Character set measured against the real CLI: ``?``, ``*``, ``[``, ``]``, ``{``, ``}`` and
``\\`` break; space, ``:``, ``~``, ``(``, ``)`` and ``!`` are safe.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import phase1_runtime  # noqa: E402
import run_coordinator  # noqa: E402


class SandboxRuleExpressibility(unittest.TestCase):
    """The guard itself, including its negative controls."""

    def test_safe_punctuation_is_not_treated_as_a_metacharacter(self) -> None:
        # Paper 1's audit root contained a colon and spaces and worked. If this test ever
        # starts failing, the guard has become a blanket ban on punctuation and will reject
        # ordinary journal-article directory names.
        for name in ("Paper: A Study (2026)", "Smith & Jones ~ final!", "a b c"):
            with self.subTest(name=name):
                self.assertEqual(phase1_runtime.sandbox_rule_metacharacters(f"/audits/{name}"), "")

    def test_every_breaking_character_is_detected(self) -> None:
        for character in "?*[]{}\\":
            with self.subTest(character=character):
                found = phase1_runtime.sandbox_rule_metacharacters(f"/audits/name{character}x")
                self.assertEqual(found, character)

    def test_the_remedy_names_the_character_and_the_path(self) -> None:
        with self.assertRaises(phase1_runtime.Phase1Error) as caught:
            phase1_runtime.assert_sandbox_expressible(
                ["/audits/dependence?/phase1"], pathlib.Path("/audits/dependence?")
            )
        message = str(caught.exception)
        self.assertIn("dependence?", message)
        self.assertIn("I4R_PAPER_DIR", message)
        self.assertIn("Forbidden:", message)


class Phase1SandboxSettingsRejectGlobRoots(unittest.TestCase):
    def paths(self, name: str):
        root = pathlib.Path("/audits") / name
        return (
            root,
            root / "phase1",
            root / "phase1/snapshots/aa/raw",
            root / "phase1/role_packets/aa/auditor_reproduction/bb",
            root / "phase1/roles/auditor_reproduction/bb/attempt_01_cc/output.partial",
        )

    def test_clean_root_still_builds_a_valid_contract(self) -> None:
        root, phase_root, snapshot, packet, output = self.paths("Paper: A Study (2026)")
        settings = phase1_runtime.sandbox_settings(root, phase_root, snapshot, packet, output)
        phase1_runtime.validate_sandbox_contract(settings, snapshot, packet, output, root)
        entries = (
            settings["sandbox"]["filesystem"]["allowRead"]
            + settings["sandbox"]["filesystem"]["allowWrite"]
        )
        self.assertEqual([e for e in entries if set("?*[]{}\\") & set(e)], [])

    def test_each_glob_metacharacter_in_the_audit_root_is_refused_at_emission(self) -> None:
        for name in ("dependence?", "star*name", "bracket[1]", "brace{x}"):
            with self.subTest(name=name):
                root, phase_root, snapshot, packet, output = self.paths(name)
                with self.assertRaises(phase1_runtime.Phase1Error):
                    phase1_runtime.sandbox_settings(root, phase_root, snapshot, packet, output)

    def test_validator_refuses_a_hand_built_settings_dict_with_a_glob_rule(self) -> None:
        # The validator used to compare the emitted rules against themselves, which cannot see
        # a rule that is well-formed JSON but matches nothing.
        root, phase_root, snapshot, packet, output = self.paths("clean")
        settings = phase1_runtime.sandbox_settings(root, phase_root, snapshot, packet, output)
        settings["sandbox"]["filesystem"]["allowWrite"] = ["/audits/dependence?/out"]
        with self.assertRaises(phase1_runtime.Phase1Error):
            phase1_runtime.validate_sandbox_contract(settings, snapshot, packet, output, root)


class CoordinatorSandboxSettingsRejectGlobRoots(unittest.TestCase):
    """The coordinator emitted the same raw-path rules and had no validator at all."""

    def test_clean_root_is_accepted(self) -> None:
        root = pathlib.Path("/audits/Paper: A Study (2026)")
        attempt = root / "coordinator/invocations/aa/attempt_01_bb"
        settings = run_coordinator.sandbox_settings(
            root, attempt, attempt / "source", attempt / "output.partial"
        )
        self.assertEqual(settings["sandbox"]["network"]["deniedDomains"], ["*"])

    def test_glob_root_is_refused(self) -> None:
        root = pathlib.Path("/audits/Can automatic reactions mirror exercise dependence?")
        attempt = root / "coordinator/invocations/aa/attempt_01_bb"
        # The class run_coordinator raises, resolved through it: several suites swap sys.modules entries during
        # discovery, so the module-level phase1_runtime can be another copy than the one run_coordinator imported.
        with self.assertRaises(run_coordinator.phase1_runtime.Phase1Error):
            run_coordinator.sandbox_settings(
                root, attempt, attempt / "source", attempt / "output.partial"
            )


class CapabilityProbeExercisesSandboxedBash(unittest.TestCase):
    """The probe must prove Bash works, not just the Write tool."""

    def setUp(self) -> None:
        import run_phase1

        self.run_phase1 = run_phase1

    def test_probe_contract_requires_bash_produced_evidence(self) -> None:
        for bound in self.run_phase1.LIVE_PROBE_BOUNDS:
            with self.subTest(bound=bound):
                contract = self.run_phase1.probe_contract(bound)
                self.assertEqual(contract["required_bash_write"], "evidence/bash.ok")
                self.assertEqual(contract["required_bash_read"], "packet_copy.json")

    def test_probe_cache_key_is_bound_to_the_audit_root(self) -> None:
        # Sandbox expressibility is a property of the root, so a receipt earned under one root
        # must not be replayed under another via a shared --probe-cache-dir.
        model = {"requested_model": "m", "spawn_alias": "a", "reasoning_effort": "max"}
        bound = self.run_phase1.SHIPPED_PROBE_BOUND
        first, _ = self.run_phase1.probe_key("1.0", model, pathlib.Path("/audits/one"), bound=bound)
        second, _ = self.run_phase1.probe_key("1.0", model, pathlib.Path("/audits/two"), bound=bound)
        self.assertNotEqual(first, second)


class AllowlistedDenialsAreEnvironmentFaults(unittest.TestCase):
    def setUp(self) -> None:
        import json
        import tempfile

        import run_phase1

        self.run_phase1 = run_phase1
        self.json = json
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-denial-test-")
        self.addCleanup(self.temporary.cleanup)
        self.attempt = pathlib.Path(self.temporary.name)

    def write(self, *commands: str) -> None:
        (self.attempt / "sandbox_denials.json").write_text(
            self.json.dumps({
                "schema_version": "i4r-phase1-sandbox-denials-1",
                "denials": [{"tool_name": "Bash", "command": c} for c in commands],
            }),
            encoding="utf-8",
        )

    def test_no_record_is_not_a_fault(self) -> None:
        self.assertEqual(self.run_phase1.allowlisted_denials(self.attempt), [])

    def test_denial_of_an_allowlisted_command_is_reported(self) -> None:
        # Both of these are verbatim from the failed production run.
        self.write('find "/x/audit/phase1/role_packets/aa/auditor_text_vs_data" -maxdepth 3')
        self.assertEqual(len(self.run_phase1.allowlisted_denials(self.attempt)), 1)
        self.write("mkdir -p /x/audit/phase1/roles/r/attempt_01/output.partial/evidence/scripts")
        self.assertEqual(len(self.run_phase1.allowlisted_denials(self.attempt)), 1)

    def test_denial_of_a_command_outside_the_allowlist_is_the_policy_working(self) -> None:
        self.write("curl https://example.invalid", "nc -l 1234")
        self.assertEqual(self.run_phase1.allowlisted_denials(self.attempt), [])

    def test_a_compound_command_is_not_blamed_on_the_sandbox(self) -> None:
        # Verbatim shapes from the first run under this guard. Each starts with an allowlisted
        # token but is denied because of a later sub-command (`export` is not on the allowlist)
        # or a leading variable assignment. Judging them by the first token read a correct
        # policy denial as a broken sandbox and killed a role whose real fault was a repairable
        # evidence hash mismatch.
        for command in (
            'cd "/x/output.partial" && export RAW_PKG_DIR="/y" && Rscript a.R',
            'cd "/x" && mkdir -p evidence',
            'RAW_PKG_DIR="/y" OUT_EVIDENCE_DIR="/z" Rscript a.R',
            "find /x -maxdepth 2 | head -5",
            "cat a.txt > b.txt",
        ):
            with self.subTest(command=command):
                self.write(command)
                self.assertEqual(self.run_phase1.allowlisted_denials(self.attempt), [])

    def test_a_simple_allowlisted_command_still_implicates_the_sandbox(self) -> None:
        # The run-2 shapes, which really were the sandbox: single commands, no operators.
        for command in (
            'find "/x/phase1/role_packets/aa/auditor_text_vs_data/bb" -maxdepth 3 -type f',
            'mkdir -p "/x/attempt_01/output.partial/evidence/scripts"',
        ):
            with self.subTest(command=command):
                self.write(command)
                self.assertEqual(len(self.run_phase1.allowlisted_denials(self.attempt)), 1)


if __name__ == "__main__":
    unittest.main()
