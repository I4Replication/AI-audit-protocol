#!/usr/bin/env python3
"""2.6.4: the post-fan-out verifier's policy, its drift check, and the package freshness check.

The Neudorfer audit (20260923-audit-3309cfcf) excluded three true article-text findings, F13-F15,
because no Claude-family confirmation was admissible for a finding first raised after Phase 1.
Hand-made verification records were refused by make_settled.sh (role_class `other`; a call that
ended after round-1 integration), and the one relabelled `role_class auditor` was counted by
finalize as a sixth auditor. 2.6.4 adds a packaged verifier role whose route and contract are
`models.post_fan_out_verifier` and `post_fan_out_verification` in config/pipeline-policy-v2.json.

A verifier routed to the critics' family would let one family confirm a row twice, which is the
failure the two-family rule exists to prevent. So every new policy leaf must be pinned. These tests
plant each drift ON DISK in a copy of this skill tree and run the real config/check_policy_drift.py
against it. The unmodified copy passing is the negative control.

The same release found `build_package.py --check` failing on an unchanged package. An empty
`.Rhistory`, written by R into the skill root after the 2.6.3 ZIP was built, made the check fail.
The package tests run the real builder on a mini skill that holds those stray R files.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
BUILDER = SKILL / "scripts" / "build_package.py"
PRIVACY = SKILL / "scripts" / "check_release_privacy.py"
POLICY_RELATIVE = Path("config") / "pipeline-policy-v2.json"
IGNORED = shutil.ignore_patterns(
    ".git", ".pytest_cache", "__pycache__", "_history*", "CHEATSHEET.pdf", ".DS_Store", "*.pyc")

# The negative-control count in the OK line is the only way to see, from outside, that the checker's own
# planted-drift self-test grew: seven with the verifier (2.6.4), nine with the round-cap controls (2.6.9),
# thirteen with the reverts to Sonnet 5 of the coordinator, the Phase-1 route and its probe leaf, and of the
# critic route to gpt-5.6-terra (2.7.0), fourteen with the revert of the probe bound to 150000 (2.7.1), fifteen
# with the revert of the Phase-1 timeout to 2700 (2.7.2), eighteen with the Claude effort restored to max and the
# verifier or the probe alone left at max (2.7.3), nineteen with the critique ceiling
# restored to 2700 and twenty with the claim-change rule removed (2.7.4).
OK_LINE = "POLICY DRIFT CHECK OK: v2 policy/docs agree and twenty planted drifts were rejected"
VERIFIER_POLICY = "post-fan-out verifier policy is not Sonnet 5.5 / xhigh on the Phase-1 auditor family"
VERIFIER_ROUTE = "post-fan-out verifier route differs from the Phase-1 auditors' route"
CONTRACT = "post-fan-out verification contract drifted"


def run(*args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(arg) for arg in args], cwd=cwd, text=True, capture_output=True,
                          timeout=120, env={**os.environ, "LC_ALL": "C"})


def write(path: Path, content: str | bytes = "fixture\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


# ------------------------------------------------------------------------------ planted drifts
def verifier_on_critic_family(policy: dict) -> None:
    policy["models"]["post_fan_out_verifier"].update(
        requested_model=policy["models"]["critics"]["requested_model"], provider="openai")


def verifier_provider_only(policy: dict) -> None:
    policy["models"]["post_fan_out_verifier"]["provider"] = "openai"


def verifier_on_opus(policy: dict) -> None:
    policy["models"]["post_fan_out_verifier"].update(requested_model="opus-5", spawn_alias="opus")


def verifier_effort_high(policy: dict) -> None:
    policy["models"]["post_fan_out_verifier"]["reasoning_effort"] = "high"


def family_rule_dropped(policy: dict) -> None:
    policy["models"]["post_fan_out_verifier"]["must_equal_auditor_family"] = False


def verifier_model_deleted(policy: dict) -> None:
    del policy["models"]["post_fan_out_verifier"]


def auditors_moved_verifier_left(policy: dict) -> None:
    for entry in policy["models"]["phase_1_auditors"].values():
        entry["requested_model"] = "sonnet-6"


def verifier_made_a_sixth_auditor(policy: dict) -> None:
    # The Neudorfer relabelling, in policy form. Phase 1 refuses a sixth audit-scoped role.
    policy["models"]["phase_1_auditors"]["verifier"] = dict(policy["models"]["post_fan_out_verifier"])


def section(policy: dict) -> dict:
    return policy["post_fan_out_verification"]


PLANTED_POLICY_DRIFTS = [
    ("verifier on the critics' family", verifier_on_critic_family, [VERIFIER_POLICY, VERIFIER_ROUTE]),
    ("verifier provider openai", verifier_provider_only, [VERIFIER_POLICY, VERIFIER_ROUTE]),
    ("verifier on opus", verifier_on_opus, [VERIFIER_POLICY, VERIFIER_ROUTE]),
    ("verifier effort high", verifier_effort_high, [VERIFIER_POLICY, VERIFIER_ROUTE]),
    ("auditor-family rule dropped", family_rule_dropped, [VERIFIER_POLICY]),
    ("verifier model entry deleted", verifier_model_deleted, [VERIFIER_POLICY]),
    ("auditors moved, verifier left behind", auditors_moved_verifier_left, [VERIFIER_ROUTE]),
    ("verifier listed as a sixth auditor", verifier_made_a_sixth_auditor, ["invalid policy structure"]),
    ("contract section deleted", lambda p: p.pop("post_fan_out_verification"),
     [CONTRACT, "policy has no post_fan_out_verification section"]),
    ("role_class auditor", lambda p: section(p).update(role_class="auditor"),
     [CONTRACT, "role_class is 'auditor' in the policy but 'verifier' in scripts/verifier_contract.py"]),
    ("round 1", lambda p: section(p).update(round=1),
     [CONTRACT, "round is 1 in the policy but 0 in scripts/verifier_contract.py"]),
    ("role pattern loosened", lambda p: section(p).update(role_pattern="^verifier_.*$"), [CONTRACT]),
    ("critic verdicts no longer withheld",
     lambda p: section(p)["withheld_inputs"].remove("critic_verdicts"), [CONTRACT]),
    ("packet carries the finding status", lambda p: section(p)["packet_fields"].append("status"), [CONTRACT]),
    ("one verdict per claim dropped", lambda p: section(p).update(one_verdict_per_claim=False), [CONTRACT]),
    ("bound by finding id", lambda p: section(p).update(binding="finding_id"), [CONTRACT]),
    ("OVERSTATED added", lambda p: section(p)["verdicts"].append("OVERSTATED"),
     [CONTRACT, "verdicts is ['CONFIRMED', 'REFUTED', 'UNVERIFIABLE', 'OVERSTATED'] in the policy"]),
    ("timing rule loosened", lambda p: section(p).update(timing="any_time"), [CONTRACT]),
    ("verifier on by default", lambda p: section(p).update(default_sessions=1), [CONTRACT]),
    ("runner renamed", lambda p: section(p).update(runner="scripts/verify.py"),
     [CONTRACT, "post-fan-out verification runner is missing: scripts/verify.py"]),
    ("runtime contract copied by value",
     lambda p: section(p).update(runtime_contract_ref=copy.deepcopy(p["phase_1"]["runtime"])), [CONTRACT]),
    ("repair ref names nothing", lambda p: section(p).update(repair_contract_ref="phase_1.repairs"),
     [CONTRACT, "repair_contract_ref names no policy entry: 'phase_1.repairs'"]),
    ("an unpinned extra key", lambda p: section(p).update(unverifiable_counts_as_confirmation=True),
     [CONTRACT]),
]


class DriftCheckRejectsVerifierDrift(unittest.TestCase):
    """Each drift is planted in a copy of this tree and the real checker must refuse it by name."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="i4r-264-drift-")
        cls.tree = Path(cls.temporary.name) / "i4r-audit"
        shutil.copytree(SKILL, cls.tree, ignore=IGNORED, symlinks=True)
        runner = cls.tree / "scripts" / "run_verifier.py"
        if not runner.is_file():
            # The checker asks only that the runner the policy names ships (is_file). The runner
            # lands in its own change; stand in for it so these tests exercise the policy pins.
            # test_runner_and_contract_module_must_ship removes it to prove the guard.
            write(runner, "#!/usr/bin/env python3\n")
        cls.checker = cls.tree / "config" / "check_policy_drift.py"
        cls.policy_path = cls.tree / POLICY_RELATIVE
        cls.policy_bytes = cls.policy_path.read_bytes()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def drift_check(self) -> tuple[int, str]:
        result = run(sys.executable, self.checker, cwd=self.tree)
        return result.returncode, result.stdout + result.stderr

    def assert_baseline(self) -> None:
        code, output = self.drift_check()
        self.assertEqual(code, 0, output)
        self.assertIn(OK_LINE, output)

    def planted(self, relative: str, transform) -> tuple[int, str]:
        """Rewrite one file of the copy, run the checker, and put the original bytes back."""
        path = self.tree / relative
        original = path.read_bytes()
        try:
            transform(path)
            return self.drift_check()
        finally:
            path.write_bytes(original)

    def test_unmodified_copy_passes_and_counts_fifteen_negative_controls(self) -> None:
        # Negative control for every planted drift below: the same tree, unmodified, is clean.
        self.assert_baseline()
        policy = json.loads(self.policy_bytes)
        self.assertEqual(policy["models"]["post_fan_out_verifier"]["requested_model"], "sonnet-5-5")
        self.assertEqual(policy["models"]["post_fan_out_verifier"]["spawn_alias"], "claude-sonnet-5-5")
        self.assertEqual(policy["post_fan_out_verification"]["default_sessions"], 0)

    def test_each_planted_policy_drift_is_rejected(self) -> None:
        for label, mutate, expected in PLANTED_POLICY_DRIFTS:
            with self.subTest(drift=label):
                def transform(path: Path, mutate=mutate) -> None:
                    policy = json.loads(self.policy_bytes)
                    mutate(policy)
                    path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
                code, output = self.planted(str(POLICY_RELATIVE), transform)
                self.assertEqual(code, 1, output)
                self.assertIn("POLICY DRIFT CHECK FAILED", output)
                for message in expected:
                    self.assertIn(message, output)
        self.assertEqual(self.policy_path.read_bytes(), self.policy_bytes)
        self.assert_baseline()

    def test_documentation_drift_is_rejected(self) -> None:
        verifier_line = re.compile(r"^- Post-fan-out verifier: .*\n", re.MULTILINE)

        def drop_summary_line(path: Path) -> None:
            text = path.read_text(encoding="utf-8")
            self.assertRegex(text, verifier_line)
            path.write_text(verifier_line.sub("", text, count=1), encoding="utf-8")

        def unverifiable_as_confirmation(path: Path) -> None:
            text = path.read_text(encoding="utf-8")
            row = "| UNVERIFIABLE | `inconclusive` |"
            self.assertEqual(text.count(row), 1)
            path.write_text(text.replace(row, "| UNVERIFIABLE | `confirmed` |"), encoding="utf-8")

        def overstated_row_added(path: Path) -> None:
            text = path.read_text(encoding="utf-8")
            end = "<!-- VERIFIER-VERDICT-OUTCOME:END -->"
            path.write_text(text.replace(end, "| OVERSTATED | `confirmed` | reworded |\n" + end),
                            encoding="utf-8")

        def table_markers_removed(path: Path) -> None:
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("<!-- VERIFIER-VERDICT-OUTCOME:START -->", ""), encoding="utf-8")

        def runner_unmentioned(path: Path) -> None:
            text = path.read_text(encoding="utf-8")
            self.assertIn("run_verifier.py", text)
            path.write_text(text.replace("run_verifier.py", "the verifier"), encoding="utf-8")

        cases = [
            ("SKILL.md", drop_summary_line, "SKILL.md policy summary does not match pipeline-policy-v2.json"),
            ("reference/adjudication_guide.md", unverifiable_as_confirmation,
             "verifier verdict table {'CONFIRMED': 'confirmed', 'REFUTED': 'refuted', "
             "'UNVERIFIABLE': 'confirmed'} differs from verifier_contract.VERDICT_OUTCOME"),
            ("reference/adjudication_guide.md", overstated_row_added,
             "differs from verifier_contract.VERDICT_OUTCOME"),
            ("reference/adjudication_guide.md", table_markers_removed,
             "adjudication_guide.md has no marked verifier verdict table"),
            ("SKILL.md", runner_unmentioned, "SKILL.md is missing post-fan-out verifier runner: 'run_verifier.py'"),
            ("INSTALL.md", runner_unmentioned,
             "INSTALL.md is missing post-fan-out verifier runner: 'run_verifier.py'"),
        ]
        for relative, transform, message in cases:
            with self.subTest(file=relative, drift=transform.__name__):
                code, output = self.planted(relative, transform)
                self.assertEqual(code, 1, output)
                self.assertIn(message, output)
        self.assert_baseline()

    def test_contract_module_change_without_the_policy_is_rejected(self) -> None:
        # The policy's literals are pinned in the checker; this proves they are ALSO joined to the
        # code every verifier consumer calls, so code and policy cannot drift apart silently.
        def round_one(path: Path) -> None:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("\nROUND = 0\n"), 1)
            path.write_text(text.replace("\nROUND = 0\n", "\nROUND = 1\n"), encoding="utf-8")

        def outcome_changed(path: Path) -> None:
            text = path.read_text(encoding="utf-8")
            old = '"UNVERIFIABLE": "inconclusive"}'
            self.assertEqual(text.count(old), 1)
            path.write_text(text.replace(old, '"UNVERIFIABLE": "confirmed"}'), encoding="utf-8")

        def pattern_removed(path: Path) -> None:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("\nROLE_PATTERN = "), 1)
            path.write_text(text.replace("\nROLE_PATTERN = ", "\nOLD_ROLE_PATTERN = "), encoding="utf-8")

        for transform, message in [
            (round_one, "round is 0 in the policy but 1 in scripts/verifier_contract.py"),
            (outcome_changed, "differs from verifier_contract.VERDICT_OUTCOME"),
            # An incomplete module is reported by name, not a traceback out of the checker.
            (pattern_removed, "shared verifier contract cannot be loaded: AttributeError"),
        ]:
            with self.subTest(drift=transform.__name__):
                code, output = self.planted("scripts/verifier_contract.py", transform)
                self.assertEqual(code, 1, output)
                self.assertIn(message, output)
        self.assert_baseline()

    def test_runner_and_contract_module_must_ship(self) -> None:
        for relative, message in [
            ("scripts/run_verifier.py", "post-fan-out verification runner is missing: scripts/run_verifier.py"),
            ("scripts/verifier_contract.py",
             "post-fan-out verification contract_module is missing: scripts/verifier_contract.py"),
        ]:
            with self.subTest(missing=relative):
                path = self.tree / relative
                original = path.read_bytes()
                path.unlink()
                try:
                    code, output = self.drift_check()
                finally:
                    path.write_bytes(original)
                self.assertEqual(code, 1, output)
                self.assertIn(message, output)
        self.assert_baseline()


# --------------------------------------------------------------------------------- packaging
class PackageIgnoresStrayRFiles(unittest.TestCase):
    """The real builder on a mini skill, as in test_stage_package_hardening."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-264-package-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "mini-skill"
        scripts = self.source / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(BUILDER, scripts / BUILDER.name)
        shutil.copy2(PRIVACY, scripts / PRIVACY.name)
        write(self.source / "VERSION", "2.6.4\n")
        write(self.source / "config/pipeline-policy-v2.json", "{}\n")
        write(self.source / "SKILL.md", "# Clean fixture\n")
        self.builder = scripts / BUILDER.name
        self.output = self.root / "mini-skill.zip"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self) -> subprocess.CompletedProcess:
        return run(sys.executable, self.builder, "--output", self.output)

    def check(self) -> subprocess.CompletedProcess:
        return run(sys.executable, self.builder, "--check", "--output", self.output)

    def names(self) -> list[str]:
        with zipfile.ZipFile(self.output) as archive:
            return archive.namelist()

    def test_rhistory_written_after_the_build_keeps_the_check_passing(self) -> None:
        # The incident: the ZIP was built, then R wrote an empty .Rhistory into the skill root.
        built = self.build()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        write(self.source / ".Rhistory", "")
        write(self.source / "reference" / ".RData", b"RDX3\n")
        checked = self.check()
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("package freshness check: ok", checked.stdout)

        # Negative control: a real new file is still a stale package, and the report names it,
        # not the R files beside it.
        write(self.source / "reference" / "notes.md", "# a real change\n")
        stale = self.check()
        output = stale.stdout + stale.stderr
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("package inventory is stale", output)
        self.assertIn("mini-skill/reference/notes.md", output)
        self.assertNotIn(".Rhistory", output)
        self.assertNotIn(".RData", output)

    def test_stray_r_files_present_at_build_time_are_not_packaged(self) -> None:
        write(self.source / ".Rhistory", "summary(fit)\n")
        write(self.source / ".RData", b"RDX3\n")
        write(self.source / "tests" / ".Rhistory", "q()\n")
        write(self.source / ".Rprofile", "options(digits = 7)\n")
        built = self.build()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        names = self.names()
        self.assertFalse([name for name in names if name.endswith((".Rhistory", ".RData"))], names)
        # Negative control: exclusion is by exact name, not an `.R*` pattern, so any other file,
        # dotfile or not, is still packaged.
        self.assertIn("mini-skill/.Rprofile", names)
        self.assertIn("mini-skill/SKILL.md", names)
        with zipfile.ZipFile(self.output) as archive:
            manifest = json.loads(archive.read("mini-skill/PACKAGE_MANIFEST.json"))
        self.assertFalse([row for row in manifest["entries"]
                          if row["path"].endswith((".Rhistory", ".RData"))])
        checked = self.check()
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_symlinked_r_files_are_skipped_in_both_name_sets(self) -> None:
        # build_package.py tests names twice: in included() and in the symlink refusal. A symlinked
        # .Rhistory (R started from a linked home) must be skipped, not refused as package input.
        target = self.root / "outside-history"
        write(target, "q()\n")
        os.symlink(target, self.source / ".Rhistory")
        os.symlink(target, self.source / ".RData")
        built = self.build()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertFalse([name for name in self.names() if name.endswith((".Rhistory", ".RData"))])

        # Negative control: any other symlinked input is still refused.
        os.symlink(target, self.source / "linked.md")
        refused = self.build()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("symlinked package input is not allowed: linked.md", refused.stdout + refused.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
