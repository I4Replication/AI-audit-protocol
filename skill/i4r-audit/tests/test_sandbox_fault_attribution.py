#!/usr/bin/env python3
"""A correct sandbox denial must not be reported as a broken sandbox.

`allowlisted_denials` exists to tell two things apart: a denial of a command the policy allows
(the CLI is not enforcing the rules it was handed -- re-sampling cannot help, so stop) from a
denial of a command the policy forbids (the policy working). It decides by looking at the first
token, which is only sound for a SINGLE simple command -- and the marker list that enforced that
omitted the most common separator of all, the newline.

On a real audit a role sent

    mkdir -p "$TMPDIR/audit_work"
    Rscript -e '...'

as one Bash call. Nothing grants `$TMPDIR`, so the denial was correct. The guard read `mkdir`,
matched it against the allowlist, declared the filesystem sandbox broken, and paused the whole of
Phase 1 rather than spending the role's remaining attempts.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))


def _load():
    spec = importlib.util.spec_from_file_location("run_phase1", SKILL / "scripts" / "run_phase1.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_phase1"] = module
    spec.loader.exec_module(module)
    return module


phase1 = _load()


class OnlyASimpleCommandCanImplicateTheSandbox(unittest.TestCase):
    def test_a_single_allowlisted_command_is_simple(self) -> None:
        for command in ("mkdir -p /tmp/x", "find . -name '*.sav'", "ls -la"):
            self.assertTrue(phase1._is_simple_command(command), command)

    def test_a_newline_separated_script_is_compound(self) -> None:
        """The case that paused a real Phase 1."""
        self.assertFalse(phase1._is_simple_command(
            'mkdir -p "$TMPDIR/audit_work"\nRscript -e \'library(haven)\''))
        self.assertFalse(phase1._is_simple_command("mkdir a\r\nls"))

    def test_the_previously_covered_operators_still_count(self) -> None:
        for command in ("cd x && ls", "ls | wc -l", "a; b", "ls > out", "echo `id`", "echo $(id)"):
            self.assertFalse(phase1._is_simple_command(command), command)

    def test_backgrounding_is_compound(self) -> None:
        self.assertFalse(phase1._is_simple_command("mkdir a & sleep 1"))

    def test_a_leading_assignment_is_not_simple(self) -> None:
        self.assertFalse(phase1._is_simple_command("FOO=1 mkdir x"))


class DenialsAreAttributedToTheRightSide(unittest.TestCase):
    def denials(self, *commands: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = Path(tmp)
            (attempt / "sandbox_denials.json").write_text(json.dumps({
                "attempt_kind": "role_inference",
                "denial_count": len(commands),
                "denials": [{"command": c, "tool_name": "Bash"} for c in commands],
            }), encoding="utf-8")
            return phase1.allowlisted_denials(attempt)

    def test_the_real_denial_is_not_blamed_on_the_sandbox(self) -> None:
        self.assertEqual(self.denials(
            'mkdir -p "$TMPDIR/audit_work"\nRscript -e \'library(haven)\''), [])

    def test_a_genuinely_allowlisted_denial_is_still_reported(self) -> None:
        """Negative control: the guard must keep catching a real environment fault."""
        allowed = next(
            (e[len("Bash("):-1] for e in phase1.runtime.BASH_ALLOWLIST
             if e.startswith("Bash(") and e.endswith(")")), None)
        self.assertIsNotNone(allowed, "no Bash allowlist entry to build the control from")
        command = allowed[:-1].strip() if allowed.endswith("*") else allowed.strip()
        self.assertEqual(self.denials(command), [command])

    def test_a_command_outside_the_allowlist_is_the_policy_working(self) -> None:
        self.assertEqual(self.denials("curl https://example.org"), [])

    def test_a_missing_record_is_not_a_fault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(phase1.allowlisted_denials(Path(tmp)), [])

    # ---- the path side of the same rule (2.4.8) ---------------------------------------------
    # The Bash allowlist and the filesystem sandbox are independent gates. An allowlisted VERB
    # on a path nothing grants is denied correctly. 2.4.6 caught the two-line form of
    # `mkdir -p "$TMPDIR/audit_work"` via the newline marker; the one-line form still paused a
    # whole Phase 1 on a false diagnosis. These build the attempt's real settings file.

    def denials_with_roots(self, *commands: str) -> tuple[list[str], Path]:
        tmp = tempfile.mkdtemp()
        attempt = Path(tmp)
        out, snap, packet = attempt / "output", attempt / "snapshot", attempt / "packet"
        (attempt / "claude_settings.json").write_text(json.dumps({"sandbox": {"filesystem": {
            "allowRead": [str(snap), str(packet), str(out)], "allowWrite": [str(out)]}}}))
        (attempt / "sandbox_denials.json").write_text(json.dumps({
            "denials": [{"command": c, "tool_name": "Bash"} for c in commands]}))
        return phase1.allowlisted_denials(attempt), attempt

    def test_one_line_allowlisted_verb_on_an_ungranted_path_is_the_policy_working(self) -> None:
        flagged, _ = self.denials_with_roots(
            'mkdir -p "$TMPDIR/audit_work"',
            "cat /" + "Users/someone/.Renviron",
            "find / -name '*.sav'",
            "cp /etc/hosts ./evidence/hosts.txt",
            "rm -rf /" + "Users/someone/audit/work",
            "ls /" + "Users/x/*.csv",
        )
        self.assertEqual([], flagged)

    def test_an_allowlisted_verb_on_a_granted_path_is_still_a_fault(self) -> None:
        """Negative control: the sandbox denying what it was told to allow IS broken."""
        _, attempt = self.denials_with_roots("ls")
        out = attempt / "output"
        commands = [f"ls {out}/evidence", f"mkdir -p {out}/evidence/sub",
                    f"cat {attempt / 'snapshot'}/pkg/data.csv", "find . -name '*.csv'",
                    f"ls {out}/*.csv", "ls"]
        (attempt / "sandbox_denials.json").write_text(json.dumps({
            "denials": [{"command": c, "tool_name": "Bash"} for c in commands]}))
        self.assertEqual(commands, phase1.allowlisted_denials(attempt))

    def test_without_a_settings_file_the_verb_rule_still_applies(self) -> None:
        """Every real attempt has claude_settings.json; without it the roots are unknown and the
        pre-2.4.8 verb-only rule is kept, so old attempt records are judged as they were."""
        self.assertEqual(self.denials("cat /" + "Users/someone/.Renviron"),
                         ["cat /" + "Users/someone/.Renviron"])


if __name__ == "__main__":
    unittest.main()
