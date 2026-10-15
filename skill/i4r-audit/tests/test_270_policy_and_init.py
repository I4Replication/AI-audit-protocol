#!/usr/bin/env python3
"""2.7.0: the shipped Claude route, the drift check that pins it, and init_run.sh's snapshot hygiene.

2.7.0 moves NEW audits to Claude Sonnet 5.5 while every existing audit keeps the Sonnet 5 route frozen in
its own pipeline-policy.snapshot.json. That is safe only if two things hold:

* ``DriftCheckPinsTheShippedRoute`` -- the runners now accept both routes (scripts/claude_routes.py
  LIVE_ROUTES), so config/check_policy_drift.py is the one place a revert of the shipped route to
  Sonnet 5 is caught. Each drift is planted ON DISK in a copy of this tree and the real checker must
  refuse it by name; the unmodified copy passing is the negative control. A loosened (allow-set) pin
  is simulated in memory to prove the new negative controls see it.
* ``InitLeavesNoSnapshot`` / ``InitRefusesALeftoverSnapshot`` -- until 2.7.0 init froze the snapshot
  before its model, provider, family and skill-version checks, and the next init in that directory
  reused whatever a refused (possibly older) skill had frozen. These run the real init_run.sh.
* ``InitRefusesAnInitialisedAudit`` / ``InitRemovesOnlyASnapshotItCreated`` /
  ``FailedForcedReinitRestoresTheSnapshot`` -- the three init defects of the 2.7.0 review (init-1, init-2,
  init-3): an initialised Sonnet 5 audit whose run.json (a projection) was gone was told to rm its only
  snapshot; the EXIT trap removed a snapshot a concurrent init had created and bound; a --force that failed
  after archiving the snapshot left none. Each reproduces the defect against the real init_run.sh on disk
  (they fail on the pre-fix script) beside negative controls that must keep passing.
* The second review: ``InitRefusesAnInitialisedAudit`` also covers init-r2-1 (legacy-1) -- a workspace where the
  real run_phase1.py was tried before init, leaving phase1/{run.lock,status.json}, was refused as an initialised
  audit -- and ``ConcurrentInitsAreSerialised`` init-r2-2: an init that had loaded, and not yet bound, a snapshot
  another init created then bound it after the other's cleanup removed it. Init now holds <audit>/.init.lock.
  ``DriftCheckPinsTheShippedRoute`` isolates each of the two pins joining claude_routes.SHIPPED to the checker
  (TD-1), and ``FailedForcedReinitRestoresTheSnapshot`` the same-second archive guard of --force (TD-4).

``GeneratedSkillBlock`` pins the generator fix: the SKILL.md block's Phase-1 line was a literal and
kept only areas spawned as ``sonnet``.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
INIT = SCRIPTS / "init_run.sh"
CHECKER = SKILL / "config" / "check_policy_drift.py"
POLICY_PATH = SKILL / "config" / "pipeline-policy-v2.json"
SNAPSHOT = "pipeline-policy.snapshot.json"
sys.path.insert(0, str(SCRIPTS))
import claude_routes  # noqa: E402
import record_invocation  # noqa: E402

IGNORED = shutil.ignore_patterns(
    ".git", ".pytest_cache", "__pycache__", "_history*", "CHEATSHEET.pdf", ".DS_Store", "*.pyc")
OK_LINE = "POLICY DRIFT CHECK OK: v2 policy/docs agree and twenty planted drifts were rejected"
PHASE1_LINE_2610 = "- Phase 1: 5 auditors, 5-up; all areas 1,2,3,4,5 use `sonnet-5` (`sonnet`), effort `max`."
CLAUDE_PINS = ("coordinator policy is not", "one or more phase-1 auditors is not assigned to",
               "post-fan-out verifier policy is not", "auditor routing is",
               "the Phase-1 capability probe is not")


def load_checker(path: pathlib.Path = CHECKER):
    spec = importlib.util.spec_from_file_location(f"i4r_270_drift_{abs(hash(str(path)))}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shipped_policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def claude_entries(policy: dict) -> list[dict]:
    models = policy["models"]
    return [models["coordinator"], models["post_fan_out_verifier"], *models["phase_1_auditors"].values()]


def sonnet_5_policy() -> dict:
    """The shipped policy with the Claude routes every audit initialised on 2.6.10 or earlier froze, and the
    capability-probe bound every audit initialised before 2.7.1 froze (150,000 normalized tokens)."""
    policy = shipped_policy()
    for entry in claude_entries(policy):
        entry.update(claude_routes.SONNET_5)
    policy["phase_1"]["capability_probe"]["requested_model"] = claude_routes.SONNET_5["requested_model"]
    policy["phase_1"]["runtime"]["probe_max_normalized_tokens"] = 150000
    policy["phase_1"]["capability_probe"]["maximum_normalized_tokens"] = 150000
    return policy


def envelope_bytes(policy: dict) -> bytes:
    """A snapshot exactly as record_invocation.initialize_policy_snapshot writes one."""
    envelope = {"snapshot_version": "1", "policy_sha256": record_invocation.policy_hash(policy), "policy": policy}
    return (json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean_env(**extra: str) -> dict[str, str]:
    drop = {"I4R_ALLOW_SKILL_VERSION_MISMATCH", "I4R_CANONICAL_SKILL_DIR", "AUDITOR_MODEL", "AUDITOR_FAMILY",
            "AUDITOR_PROVIDER", "SKILL_DIR", "PROTOCOL_VERSION", "GATE_SET_VERSION"}
    env = {key: value for key, value in os.environ.items() if key not in drop}
    # capture_environment.sh (run after a successful init) would otherwise run the real Codex CLI's --version
    env.update(LC_ALL="C", PYTHONDONTWRITEBYTECODE="1", CODEX="/nonexistent/codex", **extra)
    return env


def init(audit: pathlib.Path, *args: str, script: pathlib.Path = INIT, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(script), str(audit), "--audit-id", "t-270", *args],
                          text=True, capture_output=True, timeout=300, env=clean_env(**env))


def make_audit(root: pathlib.Path) -> pathlib.Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "f.csv").write_text("x\n", encoding="utf-8")
    return root


# ------------------------------------------------------------------------------ shipped policy
class ShippedPolicy(unittest.TestCase):
    def test_every_claude_role_of_the_shipped_policy_is_claude_routes_shipped(self) -> None:
        policy = shipped_policy()
        self.assertEqual(claude_routes.SHIPPED, claude_routes.SONNET_5_5_XHIGH)
        roles = {"coordinator": policy["models"]["coordinator"],
                 "post_fan_out_verifier": policy["models"]["post_fan_out_verifier"],
                 **policy["models"]["phase_1_auditors"]}
        self.assertEqual(len(roles), 7)
        for role, entry in roles.items():
            with self.subTest(role=role):
                self.assertEqual(claude_routes.project(entry), claude_routes.SHIPPED)
        self.assertEqual(policy["phase_1"]["capability_probe"]["requested_model"], "sonnet-5-5")
        self.assertEqual(policy["phase_1"]["capability_probe"]["reasoning_effort"],
                         claude_routes.SHIPPED["reasoning_effort"])
        # the checker's literal pin is the module's SHIPPED, and the legacy route stays dispatchable
        checker = load_checker()
        self.assertEqual(checker.SHIPPED_CLAUDE_ROUTE, claude_routes.SHIPPED)
        self.assertEqual(checker.LEGACY_CLAUDE_ROUTE, claude_routes.SONNET_5)
        self.assertEqual(claude_routes.LIVE_ROUTES[-1], claude_routes.SHIPPED)
        self.assertIn(claude_routes.SONNET_5, claude_routes.LIVE_ROUTES)


# ------------------------------------------------------------------------------ drift check
class DriftCheckPinsTheShippedRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="i4r-270-drift-")
        cls.tree = pathlib.Path(cls.temporary.name) / "i4r-audit"
        shutil.copytree(SKILL, cls.tree, ignore=IGNORED, symlinks=True)
        cls.checker = cls.tree / "config" / "check_policy_drift.py"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def drift_check(self) -> tuple[int, str]:
        result = subprocess.run([sys.executable, str(self.checker)], cwd=self.tree, text=True,
                                capture_output=True, timeout=120, env=clean_env())
        return result.returncode, result.stdout + result.stderr

    def planted(self, changes: dict[str, callable]) -> tuple[int, str]:
        """Rewrite files of the copy, run the checker, and put the original bytes back."""
        originals = {relative: (self.tree / relative).read_bytes() for relative in changes}
        try:
            for relative, transform in changes.items():
                transform(self.tree / relative)
            return self.drift_check()
        finally:
            for relative, data in originals.items():
                (self.tree / relative).write_bytes(data)

    @staticmethod
    def write_policy(mutate):
        def transform(path: pathlib.Path) -> None:
            policy = json.loads(path.read_text(encoding="utf-8"))
            mutate(policy)
            path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        return transform

    @staticmethod
    def replace_line(pattern: str, replacement: str):
        def transform(path: pathlib.Path) -> None:
            text = path.read_text(encoding="utf-8")
            changed, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
            if count != 1:
                raise AssertionError(f"{path.name}: no line matches {pattern!r}")
            path.write_text(changed, encoding="utf-8")
        return transform

    def assert_baseline(self) -> None:
        code, output = self.drift_check()
        self.assertEqual(code, 0, output)
        self.assertIn(OK_LINE, output)

    def test_unmodified_copy_passes(self) -> None:
        # Negative control for every planted drift below.
        self.assert_baseline()

    def test_reverting_the_shipped_route_to_sonnet_5_is_rejected(self) -> None:
        def everything(policy: dict) -> None:
            policy["models"] = sonnet_5_policy()["models"]
            policy["phase_1"]["capability_probe"]["requested_model"] = "sonnet-5"

        def coordinator_only(policy: dict) -> None:
            policy["models"]["coordinator"].update(claude_routes.SONNET_5)

        def phase_1_only(policy: dict) -> None:
            for entry in [policy["models"]["post_fan_out_verifier"], *policy["models"]["phase_1_auditors"].values()]:
                entry.update(claude_routes.SONNET_5)

        def alias_only(policy: dict) -> None:
            # the full id matters: on CLI 2.1.206 the alias `sonnet` serves claude-sonnet-5
            for entry in claude_entries(policy):
                entry["spawn_alias"] = "sonnet"

        def probe_leaf(policy: dict) -> None:
            policy["phase_1"]["capability_probe"]["requested_model"] = "sonnet-5"

        cases = [
            ("the 2.6.10 Claude routes", everything,
             ["coordinator policy is not Sonnet 5.5 / xhigh",
              "one or more phase-1 auditors is not assigned to Sonnet 5.5 / xhigh",
              "post-fan-out verifier policy is not Sonnet 5.5 / xhigh on the Phase-1 auditor family",
              "the Phase-1 capability probe is not Sonnet 5.5 / xhigh"]),
            ("coordinator only", coordinator_only, ["coordinator policy is not Sonnet 5.5 / xhigh"]),
            ("auditors and verifier together", phase_1_only,
             ["one or more phase-1 auditors is not assigned to Sonnet 5.5 / xhigh",
              "post-fan-out verifier policy is not Sonnet 5.5 / xhigh"]),
            ("spawned by the alias", alias_only, ["auditor routing is", "coordinator policy is not Sonnet 5.5"]),
            ("the probe's descriptive leaf", probe_leaf, ["the Phase-1 capability probe is not Sonnet 5.5 / xhigh"]),
        ]
        for label, mutate, expected in cases:
            with self.subTest(drift=label):
                code, output = self.planted({"config/pipeline-policy-v2.json": self.write_policy(mutate)})
                self.assertEqual(code, 1, output)
                self.assertIn("POLICY DRIFT CHECK FAILED", output)
                for message in expected:
                    self.assertIn(message, output)
        self.assert_baseline()

    def test_the_route_module_is_joined_to_the_pin(self) -> None:
        # check_claude_routes has two pins on the module: SHIPPED equals the checker's literal (pin 1), and SHIPPED
        # is the newest LIVE_ROUTES entry (pin 2). Their messages share the prefix "scripts/claude_routes.py SHIPPED
        # is", so each case names the full text of the pin it expects and asserts the other's absence where only one
        # fires; a prefix let either pin stand in for the other (2.7.0 review, TD-1).
        pin_1 = ("scripts/claude_routes.py SHIPPED is {'requested_model': 'sonnet-5', 'spawn_alias': 'sonnet', "
                 "'provider': 'anthropic', 'reasoning_effort': 'max'}, expected {'requested_model': 'sonnet-5-5', "
                 "'spawn_alias': 'claude-sonnet-5-5', 'provider': 'anthropic', 'reasoning_effort': 'xhigh'}")
        pin_2 = "scripts/claude_routes.py SHIPPED is not the newest LIVE_ROUTES entry"
        policy_pin = "coordinator policy is not Sonnet 5.5 / xhigh"
        shipped_line = r"^SHIPPED(?:: [^=\n]+)? = SONNET_5_5_XHIGH$"
        live_line = r"^LIVE_ROUTES(?:: [^=\n]+)? = \(SONNET_5, SONNET_5_5, SONNET_5_5_XHIGH\)$"
        shipped_5 = self.replace_line(shipped_line, "SHIPPED: dict[str, str] = SONNET_5")
        newest_first = self.replace_line(live_line, "LIVE_ROUTES: tuple[dict[str, str], ...] = (SONNET_5_5_XHIGH, SONNET_5_5, SONNET_5)")

        def both(path: pathlib.Path) -> None:
            shipped_5(path)
            newest_first(path)

        cases = [
            # (label, planted files, messages that must be reported, messages that must not)
            # The policy and the module reverted together: pin 1, pin 2 (Sonnet 5 is not the newest entry) and the
            # policy pins all fire; the literal pin 1 is the one that does not depend on the policy or the order.
            ("module and policy reverted together",
             {"scripts/claude_routes.py": shipped_5,
              "config/pipeline-policy-v2.json": self.write_policy(
                  lambda p: p.update(models=sonnet_5_policy()["models"]))},
             [pin_1, pin_2, policy_pin], []),
            # Pin 2 alone: the order says Sonnet 5 is newer than the SHIPPED route, which still equals the literal.
            ("LIVE_ROUTES reordered newest first", {"scripts/claude_routes.py": newest_first}, [pin_2], [pin_1]),
            # Pin 1 alone: SHIPPED reverted to Sonnet 5 and made the newest entry, the policy untouched -- so neither
            # pin 2 nor any policy pin sees it.
            ("SHIPPED reverted and made newest", {"scripts/claude_routes.py": both}, [pin_1], [pin_2, policy_pin]),
            ("the legacy route dropped",
             {"scripts/claude_routes.py": self.replace_line(
                 live_line, "LIVE_ROUTES: tuple[dict[str, str], ...] = (SONNET_5_5, SONNET_5_5_XHIGH)")},
             ["scripts/claude_routes.py LIVE_ROUTES no longer holds the Sonnet 5 route that every audit initialised "
              "before 2.7.0 froze"], [pin_1, pin_2]),
            # 2.7.3: every audit initialised on 2.7.0 to 2.7.2 froze Sonnet 5.5 / max and must keep dispatching.
            ("the Sonnet 5.5 / max route dropped",
             {"scripts/claude_routes.py": self.replace_line(
                 live_line, "LIVE_ROUTES: tuple[dict[str, str], ...] = (SONNET_5, SONNET_5_5_XHIGH)")},
             ["scripts/claude_routes.py LIVE_ROUTES no longer holds the Sonnet 5.5 / max route that every audit "
              "initialised on 2.7.0 to 2.7.2 froze"], [pin_1, pin_2]),
        ]
        for label, changes, expected, absent in cases:
            with self.subTest(drift=label):
                code, output = self.planted(changes)
                self.assertEqual(code, 1, output)
                self.assertIn("POLICY DRIFT CHECK FAILED", output)
                for message in expected:
                    self.assertIn(f"- {message}\n", output + "\n")
                for message in absent:
                    self.assertNotIn(message, output)
        self.assert_baseline()

    def test_a_printed_init_command_naming_sonnet_5_is_rejected(self) -> None:
        old = self.replace_line(r"--auditor-model sonnet-5-5 ", "--auditor-model sonnet-5 ")
        code, output = self.planted({"SKILL.md": old})
        self.assertEqual(code, 1, output)
        self.assertIn("SKILL.md prints init_run.sh --auditor-model sonnet-5, but the shipped policy freezes "
                      "sonnet-5-5", output)

        def append(path: pathlib.Path) -> None:
            path.write_text(path.read_text(encoding="utf-8") + "\n<p>init_run.sh --auditor-model=sonnet-5</p>\n",
                            encoding="utf-8")
        code, output = self.planted({"CHEATSHEET.html": append})
        self.assertEqual(code, 1, output)
        self.assertIn("CHEATSHEET.html prints init_run.sh --auditor-model sonnet-5", output)
        self.assert_baseline()

    def test_the_sonnet_5_negative_controls_see_a_loosened_pin(self) -> None:
        checker = load_checker(self.checker)
        policy = checker.load_policy()
        found: list[str] = []
        checker.check_negative_controls(policy, found)
        self.assertEqual(found, [], "the unmodified checker rejects every planted drift")

        # A pin loosened to "any live route", the tempting mistake once the runners accept both: its errors
        # are dropped whenever every Claude role names a LIVE_ROUTES entry.
        strict = checker.check_policy_shape

        def loosened(candidate: dict, errors: list[str]) -> None:
            raised: list[str] = []
            strict(candidate, raised)
            live = all(claude_routes.project(entry) in claude_routes.LIVE_ROUTES
                       for entry in claude_entries(candidate))
            errors.extend(error for error in raised if not (live and error.startswith(CLAUDE_PINS)))

        checker.check_policy_shape = loosened
        try:
            found = []
            checker.check_negative_controls(policy, found)
        finally:
            checker.check_policy_shape = strict
        self.assertIn("negative control was not rejected: coordinator restored to Sonnet 5", found)
        self.assertIn("negative control was not rejected: Phase-1 route restored to Sonnet 5", found)
        self.assertNotIn("negative control was not rejected: auditor routing", found,
                         "a route outside LIVE_ROUTES (opus) is still refused by the loosened pin")


# ------------------------------------------------------------------------------ generated block
class GeneratedSkillBlock(unittest.TestCase):
    checker = load_checker()

    def phase1_line(self, policy: dict) -> str:
        return next(line for line in self.checker.policy_summary(policy).splitlines()
                    if line.startswith("- Phase 1:"))

    def test_skill_md_block_regenerates_byte_exact(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        block = re.search(r"<!-- PIPELINE-POLICY-V2:START -->.*?<!-- PIPELINE-POLICY-V2:END -->", skill, re.DOTALL)
        self.assertIsNotNone(block)
        self.assertEqual(block.group(0), self.checker.policy_summary(shipped_policy()))
        self.assertIn("- Coordinator: `sonnet-5-5` (`claude-sonnet-5-5`), effort `xhigh`.", block.group(0))
        self.assertIn("all areas 1,2,3,4,5 use `sonnet-5-5` (`claude-sonnet-5-5`), effort `xhigh`.", block.group(0))

    def test_a_sonnet_5_policy_still_renders_the_2_6_10_line(self) -> None:
        self.assertEqual(self.phase1_line(sonnet_5_policy()), PHASE1_LINE_2610)

    def test_a_split_policy_renders_both_routes(self) -> None:
        # Negative control for the old filter: it listed only areas spawned as `sonnet` under a literal
        # `sonnet-5`, so this policy printed "all areas 1,2 use `sonnet-5` (`sonnet`)".
        policy = shipped_policy()
        for role in ("auditor_reproduction", "auditor_data_integrity"):
            policy["models"]["phase_1_auditors"][role].update(claude_routes.SONNET_5)
        self.assertEqual(self.phase1_line(policy),
                         "- Phase 1: 5 auditors, 5-up; areas 1,2 use `sonnet-5` (`sonnet`), effort `max`; "
                         "areas 3,4,5 use `sonnet-5-5` (`claude-sonnet-5-5`), effort `xhigh`.")


# ------------------------------------------------------------------------------ init_run.sh
class InitLeavesNoSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-270-init-")
        self.root = pathlib.Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_refused_init_leaves_no_snapshot(self) -> None:
        cases = [
            ("the pre-2.7.0 printed model", ["--auditor-model", "sonnet-5"],
             "requires coordinator and Phase-1 model sonnet-5-5 (got sonnet-5)"),
            ("the pre-2.6.9 cap", ["--round-cap", "5"], "requires round target 2 and hard cap 7"),
            ("another provider", ["--auditor-provider", "openai"], "requires provider anthropic (got openai)"),
            ("another family", ["--auditor-family", "gpt"], "requires model family claude (got gpt)"),
        ]
        for label, args, message in cases:
            with self.subTest(refusal=label):
                audit = make_audit(self.root / label.replace(" ", "_") / "audit")
                result = init(audit, *args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)
                self.assertFalse((audit / "run.json").exists())
                self.assertFalse((audit / SNAPSHOT).exists(), "a refused init froze a snapshot")
                self.assertEqual(sorted(p.name for p in audit.iterdir()), ["pkg"])

        # Negative control: a snapshot that was there before the refusal is kept, byte for byte (init removes
        # only a snapshot it froze), so the assertion above can see a snapshot when one is there.
        audit = make_audit(self.root / "kept" / "audit")
        subprocess.run([sys.executable, str(SCRIPTS / "record_invocation.py"), "--init-policy", str(audit)],
                       check=True, capture_output=True, env=clean_env())
        before = sha(audit / SNAPSHOT)
        result = init(audit, "--auditor-model", "sonnet-5")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(sha(audit / SNAPSHOT), before)

    def test_the_skill_version_pin_refuses_before_anything_is_written(self) -> None:
        tree = self.root / "tree"
        canonical = tree / "skill" / "i4r-audit-unified" / "i4r-audit"
        canonical.mkdir(parents=True)
        (canonical / "VERSION").write_text("9.9.9\n", encoding="utf-8")
        audit = make_audit(tree / "Journal" / "Paper" / "audit")
        result = init(audit)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("canonical skill is 9.9.9", result.stderr)
        self.assertFalse((audit / SNAPSHOT).exists(), "the version pin refused after freezing a snapshot")
        # Negative control: the same init allowed through the pin freezes one.
        result = init(audit, I4R_ALLOW_SKILL_VERSION_MISMATCH="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((audit / SNAPSHOT).is_file())
        self.assertEqual(record_invocation.load_policy_snapshot(audit)["models"]["coordinator"],
                         claude_routes.SHIPPED)

    def test_a_failure_after_the_freeze_removes_the_snapshot_it_froze(self) -> None:
        # An empty --skill-dir passes every pre-freeze check and fails at the gate digest, after the freeze.
        empty = self.root / "empty-skill"
        empty.mkdir()
        audit = make_audit(self.root / "late" / "audit")
        result = init(audit, "--skill-dir", str(empty))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no gate files found", result.stderr)
        self.assertFalse((audit / "run.json").exists())
        self.assertFalse((audit / SNAPSHOT).exists(), "a failed init left the snapshot it froze")
        # Negative control: a snapshot that predates the failing init is not this init's to remove.
        subprocess.run([sys.executable, str(SCRIPTS / "record_invocation.py"), "--init-policy", str(audit)],
                       check=True, capture_output=True, env=clean_env())
        before = sha(audit / SNAPSHOT)
        result = init(audit, "--skill-dir", str(empty))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(sha(audit / SNAPSHOT), before)


class InitRefusesALeftoverSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-270-leftover-")
        self.root = pathlib.Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def plant(self, name: str, data: bytes) -> pathlib.Path:
        audit = make_audit(self.root / name / "audit")
        (audit / SNAPSHOT).write_bytes(data)
        return audit

    def test_a_leftover_on_another_claude_route_is_refused_untouched(self) -> None:
        pre_264 = sonnet_5_policy()
        del pre_264["models"]["post_fan_out_verifier"]
        verifier_only = shipped_policy()
        verifier_only["models"]["post_fan_out_verifier"].update(claude_routes.SONNET_5)
        cases = [
            ("a 2.6.10 snapshot", envelope_bytes(sonnet_5_policy()),
             "coordinator is Sonnet 5 (sonnet-5, spawned as --model sonnet) / max there, Sonnet 5.5 "
             "(sonnet-5-5, spawned as --model claude-sonnet-5-5) / xhigh in"),
            ("a snapshot older than the verifier", envelope_bytes(pre_264), "coordinator is Sonnet 5"),
            ("only the verifier differs", envelope_bytes(verifier_only),
             "post_fan_out_verifier is Sonnet 5 (sonnet-5, spawned as --model sonnet) / max there"),
            ("a snapshot whose hash does not verify",
             envelope_bytes(shipped_policy()).replace(b'"round_cap": 7', b'"round_cap": 6'),
             "policy snapshot hash is invalid"),
        ]
        for label, data, message in cases:
            with self.subTest(leftover=label):
                audit = self.plant(label.replace(" ", "_"), data)
                result = init(audit)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)
                self.assertIn(f"remove it (rm {audit / SNAPSHOT})", result.stderr)
                self.assertFalse((audit / "run.json").exists())
                self.assertEqual((audit / SNAPSHOT).read_bytes(), data, "the leftover was rewritten")
                # the remedy works: with the file removed, init freezes the shipped route
                (audit / SNAPSHOT).unlink()
                result = init(audit)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(claude_routes.project(
                    record_invocation.load_policy_snapshot(audit)["models"]["coordinator"]), claude_routes.SHIPPED)

    def test_a_leftover_matching_the_shipped_routes_is_reused(self) -> None:
        # Negative controls. The test flow record_invocation.py --init-policy, then init_run.sh, reuses its
        # snapshot as before; so does one that differs only outside the Claude routes (the cap-5 snapshot
        # tests/test_269_round_cap_amendment.py plants), because only the Claude routes are compared.
        cap_five = shipped_policy()
        cap_five["critique"]["round_cap"] = 5
        cap_five["critique"].pop("round_cap_amendment", None)
        audit = make_audit(self.root / "init-policy" / "audit")
        subprocess.run([sys.executable, str(SCRIPTS / "record_invocation.py"), "--init-policy", str(audit)],
                       check=True, capture_output=True, env=clean_env())
        for label, audit, cap in [("record_invocation --init-policy", audit, 7),
                                  ("the same routes, cap 5", self.plant("cap5", envelope_bytes(cap_five)), 5)]:
            with self.subTest(leftover=label):
                before = sha(audit / SNAPSHOT)
                result = init(audit)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                run = json.loads((audit / "run.json").read_text(encoding="utf-8"))
                self.assertEqual(run["toolkit"]["pipeline_policy_snapshot_sha256"], before)
                self.assertEqual(sha(audit / SNAPSHOT), before)
                self.assertEqual(run["round_cap"], cap)
                self.assertEqual(run["auditor_model"]["model_id"], "sonnet-5-5")

    def test_force_refreezes_from_the_shipped_policy_and_a_refused_force_archives_nothing(self) -> None:
        # --force is the documented recovery for exhausted Phase-1 roles; since 2.7.0 it re-pins an older
        # audit to Sonnet 5.5 (INSTALL.md). A refused --force must not archive the snapshot first.
        audit = self.plant("forced", envelope_bytes(sonnet_5_policy()))
        (audit / "run.json").write_text("{}\n", encoding="utf-8")
        legacy = (audit / SNAPSHOT).read_bytes()
        result = init(audit, "--force", "--auditor-model", "sonnet-5")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual((audit / SNAPSHOT).read_bytes(), legacy)
        self.assertEqual(list(audit.glob("pipeline-policy.snapshot.previous.*.json")), [])

        result = init(audit, "--force")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        archived = list(audit.glob("pipeline-policy.snapshot.previous.*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), legacy)
        self.assertEqual(claude_routes.project(
            record_invocation.load_policy_snapshot(audit)["models"]["coordinator"]), claude_routes.SHIPPED)


# ------------------------------------------------------------------------------ the 2.7.0 review: init-1, -2, -3
JOURNAL = "run.events.jsonl"
ARCHIVES = "pipeline-policy.snapshot.previous.*.json"
# Every audit sits under a journal folder; this one's name has the spaces and '&' init must quote.
JOURNAL_DIR = "Scandinavian Journal of Medicine & Science in Sports"
# python3 for one init under test. Around that init's single `record_invocation.py --init-policy` call it runs
# I4R_TEST_RACE_HOOK -- a concurrent init's step -- before the real call, or after it (I4R_TEST_RACE_WHEN=after).
# The hook runs without the variable, so the inits it starts get the real python3. With I4R_TEST_SCAN_GATE=<path>
# (OTHER_INIT sets it for the other init alone), the package scan, `python3 - <audit>/pkg`, first creates
# <path>.scanning and then waits for <path>.go: that init has loaded and hashed its snapshot, and has not yet
# written run.json -- the window of the 2.7.0 review's init-r2-2, which on a large deposit lasts minutes.
RACE_SHIM = r'''#!/bin/bash
real=@REAL@
if [ -n "${I4R_TEST_SCAN_GATE:-}" ] && [ "$#" -eq 2 ] && [ "$1" = - ] && [ "${2##*/}" = pkg ]; then
  gate="$I4R_TEST_SCAN_GATE"; unset I4R_TEST_SCAN_GATE
  : > "$gate.scanning"
  i=0
  while [ ! -e "$gate.go" ] && [ "$i" -lt 1800 ]; do sleep 0.1; i=$((i + 1)); done
fi
if [ -n "${I4R_TEST_RACE_HOOK:-}" ] && [ "${2:-}" = "--init-policy" ]; then
  hook="$I4R_TEST_RACE_HOOK"; unset I4R_TEST_RACE_HOOK
  if [ "${I4R_TEST_RACE_WHEN:-before}" = after ]; then
    "$real" "$@"; rc=$?
    /bin/bash -c "$hook" >>"$I4R_TEST_RACE_LOG" 2>&1; echo "hook rc=$?" >>"$I4R_TEST_RACE_LOG"
    exit $rc
  fi
  /bin/bash -c "$hook" >>"$I4R_TEST_RACE_LOG" 2>&1; echo "hook rc=$?" >>"$I4R_TEST_RACE_LOG"
fi
exec "$real" "$@"
'''
# `python3 OTHER_INIT <init_run.sh> <audit> <log> <gate or "">`: start another init of the audit as a second
# operator would -- its own session, stdio only, none of the descriptors of the init that runs this (whose init
# lock is one) -- and return once that init waits for the init lock, reaches its gated package scan, or ends. The
# init's output goes to <log> and its exit status to <log>.rc. Run as a hook, it is a concurrent init that does not
# block the init under test, so a lock that serialises the two cannot deadlock the test.
OTHER_INIT = r'''
import os, pathlib, subprocess, sys, time
init, audit, log, gate = sys.argv[1:5]
env = {key: value for key, value in os.environ.items() if not key.startswith("I4R_TEST_")}
if gate:
    env["I4R_TEST_SCAN_GATE"] = gate
done = pathlib.Path(log + ".rc")
with open(log, "ab") as out:
    subprocess.Popen(["/bin/bash", "-c", '/bin/bash "$0" "$1" --audit-id t-270-other; echo $? > "$2.tmp"; mv "$2.tmp" "$2"',
                      init, audit, str(done)],
                     stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                     close_fds=True, start_new_session=True)
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    text = pathlib.Path(log).read_text(encoding="utf-8", errors="replace")
    if done.exists() or "waiting: the init lock" in text or (gate and os.path.exists(gate + ".scanning")):
        break
    time.sleep(0.05)
'''
LOCK_FILE = ".init.lock"
WAITED = "waiting: the init lock of this audit"


def listing(audit: pathlib.Path) -> dict[str, str]:
    """Every file of the audit (lock files aside) and its sha256: what a refused or failed init may not change."""
    return {path.relative_to(audit).as_posix(): sha(path) for path in sorted(audit.rglob("*"))
            if path.is_file() and not path.name.startswith(".")}


def binds(audit: pathlib.Path) -> bool:
    """run.json and the snapshot are there, and run.json binds the snapshot's exact bytes."""
    if not (audit / "run.json").is_file() or not (audit / SNAPSHOT).is_file():
        return False
    try:
        record_invocation.validate_policy_binding(audit)
    except (OSError, ValueError):
        return False
    return True


def frozen_route(audit: pathlib.Path) -> dict:
    return claude_routes.project(record_invocation.load_policy_snapshot(audit)["models"]["coordinator"])


class ReviewedInitFixture(unittest.TestCase):
    """Audits initialised as every audit before 2.7.0 was -- by init_run.sh, on a policy whose Claude routes are
    Sonnet 5 -- or on the shipped routes. The Sonnet 5 ones come from a copy of this tree's scripts whose shipped
    policy has the 2.6.10 Claude routes, so they are real init output, journal included."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tree_temporary = tempfile.TemporaryDirectory(prefix="i4r-270-sonnet5-tree-")
        tree = pathlib.Path(cls.tree_temporary.name) / "i4r-audit"
        for name in ("scripts", "schemas", "config", "prompts"):
            shutil.copytree(SKILL / name, tree / name, ignore=IGNORED, symlinks=True)
        shutil.copy2(SKILL / "VERSION", tree / "VERSION")
        (tree / "config" / "pipeline-policy-v2.json").write_text(
            json.dumps(sonnet_5_policy(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        cls.sonnet_5_init = tree / "scripts" / "init_run.sh"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tree_temporary.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-270-review-")
        self.base = pathlib.Path(self.temporary.name)
        self.root = self.base / JOURNAL_DIR
        self.empty_skill = self.base / "empty-skill"   # passes every pre-freeze check, fails at the gate digest
        self.empty_skill.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def initialised(self, name: str, *, sonnet_5: bool) -> pathlib.Path:
        audit = make_audit(self.root / name / "audit")
        result = init(audit, script=self.sonnet_5_init if sonnet_5 else INIT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(frozen_route(audit), claude_routes.SONNET_5 if sonnet_5 else claude_routes.SHIPPED)
        self.assertTrue(binds(audit))
        self.assertGreater((audit / JOURNAL).stat().st_size, 0)
        return audit


class InitRefusesAnInitialisedAudit(ReviewedInitFixture):
    """init-1: run.json is a projection of run.events.jsonl, and log_event.sh sends an operator whose run.json is
    gone to init_run.sh. Init took the missing run.json to mean an unfinished init and told the operator to rm the
    audit's frozen Sonnet 5 snapshot -- its only copy."""

    def test_a_sonnet_5_audit_whose_run_json_is_gone_is_refused_and_keeps_its_snapshot(self) -> None:
        audit = self.initialised("sonnet-5", sonnet_5=True)
        (audit / "run.json").unlink()
        before = listing(audit)
        result = init(audit)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotRegex(result.stderr, r"\brm ", "init told the operator to remove the audit's snapshot")
        self.assertNotIn("left over", result.stderr)
        self.assertEqual(listing(audit), before, "the refusal changed the audit")
        self.assertIn("is an initialised audit (it has the journal run.events.jsonl) whose run.json is missing",
                      result.stderr)
        self.assertIn("pipeline-policy.snapshot.json is the policy the audit froze -- keep that file as it is",
                      result.stderr)
        self.assertIn("This audit froze sonnet-5, so --force would move it to sonnet-5-5", result.stderr)
        # The remedy it prints works as printed, quoting and all: run.json is rebuilt from the journal and binds the
        # untouched Sonnet 5 snapshot.
        command = re.search(r"^ +(python3 \S.* --recover \S.*)$", result.stderr, flags=re.MULTILINE)
        self.assertIsNotNone(command, result.stderr)
        recovered = subprocess.run(["/bin/bash", "-c", command.group(1)], text=True, capture_output=True,
                                   timeout=120, env=clean_env())
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertTrue(binds(audit))
        self.assertEqual(frozen_route(audit), claude_routes.SONNET_5)
        run = json.loads((audit / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["auditor_model"]["model_id"], "sonnet-5")

    def test_an_audit_on_the_shipped_route_whose_run_json_is_gone_is_refused_before_anything_is_written(self) -> None:
        # Its snapshot matches the shipped routes, so init used to reuse it, write a fresh run.json over the audit,
        # and only then refuse because the journal exists.
        audit = self.initialised("sonnet-5-5", sonnet_5=False)
        (audit / "run.json").unlink()
        before = listing(audit)
        result = init(audit)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse((audit / "run.json").exists(), "init wrote a run.json over an initialised audit")
        self.assertEqual(listing(audit), before)
        self.assertIn("is an initialised audit (it has the journal run.events.jsonl)", result.stderr)
        self.assertIn("--recover", result.stderr)
        self.assertNotIn("This audit froze", result.stderr, "no route change to announce")

    def test_phase1_work_or_an_invocation_ledger_without_a_journal_is_refused_without_a_rebuild_command(self) -> None:
        # phase1/ counts when it holds Phase-1 work: anything besides the run.lock and status.json that run_phase1.py
        # leaves when it refuses before init (test_a_workspace_where_run_phase1_was_tried_first_initialises). An
        # empty phase1/ used to be planted here; that is run_phase1.py's leftover when no Claude CLI is found.
        def phase1_with(*entries: str):
            def plant(audit: pathlib.Path) -> None:
                (audit / "phase1").mkdir()
                for name in ("run.lock", "status.json"):
                    (audit / "phase1" / name).write_text("", encoding="utf-8")
                for entry in entries:
                    if entry.endswith("/"):
                        (audit / "phase1" / entry).mkdir()
                    else:
                        (audit / "phase1" / entry).write_text("{}\n", encoding="utf-8")
            return plant

        def phase1_symlink(audit: pathlib.Path) -> None:
            (audit.parent / "elsewhere").mkdir()
            (audit / "phase1").symlink_to(audit.parent / "elsewhere")

        for label, sign, plant in (
                ("phase1-roles", "phase1/", phase1_with("roles/")),
                ("phase1-snapshots-and-quorum", "phase1/", phase1_with("snapshots/", "quorum.json")),
                ("phase1-status-is-a-directory", "phase1/", lambda audit: (audit / "phase1" / "status.json").mkdir(
                    parents=True)),
                ("phase1-symlink", "phase1/", phase1_symlink),
                ("ledger", "the invocation ledger invocations.jsonl",
                 lambda audit: (audit / "invocations.jsonl").write_text("{}\n", encoding="utf-8"))):
            with self.subTest(sign=label):
                audit = make_audit(self.root / label / "audit")
                plant(audit)
                before = listing(audit)
                result = init(audit)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(f"is an initialised audit (it has {sign}) whose run.json is missing", result.stderr)
                self.assertIn("restore run.json from a backup", result.stderr)
                self.assertNotIn("--recover", result.stderr, "there is no journal to recover from")
                # there is no snapshot, so none is called the policy the audit froze
                self.assertNotIn("keep that file as it is", result.stderr)
                self.assertIn("Its pipeline-policy.snapshot.json, the policy the audit froze, is missing too",
                              result.stderr)
                self.assertFalse((audit / SNAPSHOT).exists())
                self.assertFalse((audit / ".init.lock").exists(), "a refused init left its lock file")
                self.assertEqual(listing(audit), before)

    def test_a_workspace_where_run_phase1_was_tried_first_initialises(self) -> None:
        # (2.7.0 review, legacy-1 = init-r2-1) run_phase1.py makes phase1/ -- its run.lock and, as it refuses, a
        # status.json -- before it looks for the snapshot. Run before init (the --probe-only canary, say), it refuses
        # with "run scripts/init_run.sh on the audit first"; init then refused the workspace as an initialised audit
        # and pointed to a snapshot and a backup that do not exist. Each leftover is made by the real run_phase1.py.
        no_cli = self.base / "no-claude-on-path"
        no_cli.mkdir()
        cases = [
            ("probe-canary", ["--probe-only", "--claude-bin", "/usr/bin/false"], {},
             "policy snapshot is missing", ["run.lock", "status.json"]),
            ("phase-1", ["--claude-bin", "/usr/bin/false"], {},
             "policy snapshot is missing", ["run.lock", "status.json"]),
            ("no-claude-cli", ["--probe-only"], {"PATH": str(no_cli)}, "Claude CLI not found", []),
        ]
        for label, args, env, message, left in cases:
            with self.subTest(run_phase1=label):
                audit = make_audit(self.root / label / "audit")
                for name in ("paper", "work", "report", "critique"):   # setup_workspace.sh's directories
                    (audit / name).mkdir()
                tried = subprocess.run([sys.executable, str(SCRIPTS / "run_phase1.py"), str(audit), *args],
                                       text=True, capture_output=True, timeout=120, env=clean_env(**env))
                self.assertEqual(tried.returncode, 1, tried.stdout + tried.stderr)
                self.assertIn(message, tried.stderr)
                if message == "policy snapshot is missing":
                    self.assertIn("run scripts/init_run.sh on the audit first", tried.stderr)
                self.assertEqual(sorted(path.name for path in (audit / "phase1").iterdir()), left)
                result = init(audit)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("is an initialised audit", result.stderr)
                self.assertTrue(binds(audit))
                self.assertEqual(frozen_route(audit), claude_routes.SHIPPED)
                self.assertFalse((audit / ".init.lock").exists(), "init left its lock file")

    def test_negative_controls_a_new_workspace_initialises_and_force_starts_an_initialised_audit_over(self) -> None:
        # setup_workspace.sh's directories are no sign of an initialised audit.
        audit = make_audit(self.root / "workspace" / "audit")
        for name in ("paper", "work", "report", "critique"):
            (audit / name).mkdir()
        result = init(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(binds(audit))
        # --force still starts an initialised audit whose run.json is gone over, and re-pins it (INSTALL.md).
        audit = self.initialised("forced", sonnet_5=True)
        legacy, journal = (audit / SNAPSHOT).read_bytes(), (audit / JOURNAL).read_bytes()
        (audit / "run.json").unlink()
        result = init(audit, "--force")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([path.read_bytes() for path in audit.glob(ARCHIVES)], [legacy])
        self.assertEqual([path.read_bytes() for path in audit.glob("run.events.previous.*.jsonl")], [journal])
        self.assertEqual(frozen_route(audit), claude_routes.SHIPPED)
        self.assertTrue(binds(audit))


class RaceFixture(ReviewedInitFixture):
    """The python3 shim (RACE_SHIM) that runs a concurrent step inside the freeze of the init under test, and the
    launcher (OTHER_INIT) that starts a concurrent init without blocking it."""

    def setUp(self) -> None:
        super().setUp()
        self.others: list[tuple[pathlib.Path, pathlib.Path | None]] = []

    def tearDown(self) -> None:
        # Never leave another init running into a removed directory: open its gate and give it time to end.
        for log, gate in self.others:
            if gate is not None:
                pathlib.Path(f"{gate}.go").touch()
            done, deadline = pathlib.Path(f"{log}.rc"), time.monotonic() + 60
            while not done.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
        super().tearDown()

    def shim_path(self) -> str:
        shim = self.base / "shim" / "python3"
        if not shim.exists():
            shim.parent.mkdir(exist_ok=True)
            shim.write_text(RACE_SHIM.replace("@REAL@", shlex.quote(sys.executable)), encoding="utf-8")
            shim.chmod(0o755)
        return f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}"

    def race(self, audit: pathlib.Path, hook: str, *args: str, when: str = "before") -> tuple:
        log = self.base / f"race-{audit.parent.name}.log"
        result = init(audit, *args, PATH=self.shim_path(),
                      I4R_TEST_RACE_HOOK=hook, I4R_TEST_RACE_WHEN=when, I4R_TEST_RACE_LOG=str(log))
        return result, log.read_text(encoding="utf-8") if log.exists() else ""

    def other_init(self, audit: pathlib.Path, tag: str, *, gate: bool = False) -> tuple[list[str], pathlib.Path,
                                                                                        pathlib.Path | None]:
        """The command that starts another init of ``audit`` (OTHER_INIT), its log, and its scan gate."""
        launcher = self.base / "other_init.py"
        launcher.write_text(OTHER_INIT, encoding="utf-8")
        log = self.base / f"other-{tag}.log"
        log.write_text("", encoding="utf-8")
        gate_path = self.base / f"gate-{tag}" if gate else None
        self.others.append((log, gate_path))
        return ([sys.executable, str(launcher), str(INIT), str(audit), str(log), str(gate_path or "")],
                log, gate_path)

    @staticmethod
    def as_hook(command: list[str]) -> str:
        return " ".join(shlex.quote(part) for part in command)

    def finish(self, log: pathlib.Path, gate: pathlib.Path | None = None) -> tuple[int, str]:
        """Open the other init's gate, wait for it to end, and return its exit status and output."""
        if gate is not None:
            pathlib.Path(f"{gate}.go").touch()
        done, deadline = pathlib.Path(f"{log}.rc"), time.monotonic() + 180
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        output = log.read_text(encoding="utf-8")
        self.assertTrue(done.exists(), f"the other init did not end:\n{output}")
        return int(done.read_text(encoding="utf-8").strip()), output

    def wait_for(self, path: pathlib.Path, what: str) -> None:
        deadline = time.monotonic() + 120
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(path.exists(), what)


class InitRemovesOnlyASnapshotItCreated(RaceFixture):
    """init-2: the EXIT trap was armed whenever the snapshot was absent before the freeze, and record_invocation.py
    --init-policy quietly loads one that exists. An init that loaded a concurrent init's snapshot therefore removed
    it on the way out. A python3 shim runs the concurrent step inside the freeze of the init under test. Since
    init-r2-2 a concurrent init_run.sh waits for the init lock instead (ConcurrentInitsAreSerialised), so the steps
    here that bind without it are what a process holding no init lock does: record_invocation.py's own freeze, an
    older skill's snapshot, a run.json written beside it."""

    def test_init_policy_says_whether_this_call_created_the_snapshot(self) -> None:
        # the contract init_run.sh arms its cleanup on
        audit = make_audit(self.root / "cli" / "audit")
        command = [sys.executable, str(SCRIPTS / "record_invocation.py"), "--init-policy", str(audit)]
        first = subprocess.run(command, text=True, capture_output=True, timeout=60, env=clean_env())
        second = subprocess.run(command, text=True, capture_output=True, timeout=60, env=clean_env())
        self.assertEqual((first.returncode, second.returncode), (0, 0), first.stderr + second.stderr)
        digest = record_invocation.policy_hash(record_invocation.load_policy_snapshot(audit))
        self.assertEqual(first.stdout, f"policy snapshot created: {audit.resolve() / SNAPSHOT} ({digest})\n")
        self.assertEqual(second.stdout, f"policy snapshot loaded: {audit.resolve() / SNAPSHOT} ({digest})\n")

    def test_a_snapshot_another_init_created_and_bound_is_kept(self) -> None:
        # The review's r2: init A is started inside B's freeze, and B then fails after the freeze. Before init-r2-2 A
        # ran to the end there, creating and binding the snapshot B then loaded; now A waits for B's lock, B removes
        # the snapshot B created, and A then creates and binds its own. Either way the snapshot A binds is kept.
        audit = make_audit(self.root / "bound-by-another" / "audit")
        command, other_log, _ = self.other_init(audit, "bound")
        result, log = self.race(audit, self.as_hook(command), "--skill-dir", str(self.empty_skill))
        self.assertIn("hook rc=0", log, log)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no gate files found", result.stderr)
        code, other = self.finish(other_log)
        self.assertEqual(code, 0, other)
        self.assertTrue((audit / SNAPSHOT).is_file(), "B removed the snapshot A created and bound")
        self.assertTrue(binds(audit))

    def test_a_snapshot_another_process_created_is_never_this_inits_to_remove(self) -> None:
        # The O_EXCL report alone: no run.json binds these snapshots.
        older = self.base / "older-skill.snapshot.json"
        older.write_bytes(envelope_bytes(sonnet_5_policy()))
        freeze = f"{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPTS / 'record_invocation.py'))} --init-policy"
        cases = [
            # B fails after the freeze
            ("init-policy", lambda audit: f"{freeze} {shlex.quote(str(audit))}",
             ["--skill-dir", str(self.empty_skill)], "no gate files found"),
            # an older skill froze Sonnet 5 meanwhile; B's own check refuses it (the review's r3)
            ("older-skill", lambda audit: f"cp {shlex.quote(str(older))} {shlex.quote(str(audit / SNAPSHOT))}",
             [], "the frozen policy snapshot is not the policy init checked"),
        ]
        for label, hook, args, message in cases:
            with self.subTest(race=label):
                audit = make_audit(self.root / label / "audit")
                result, log = self.race(audit, hook(audit), *args)
                self.assertIn("hook rc=0", log, log)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)
                self.assertTrue((audit / SNAPSHOT).is_file(), "the init removed a snapshot it did not create")
                self.assertFalse((audit / "run.json").exists())
                self.assertFalse((audit / LOCK_FILE).exists(), "the failed init left its lock file")
                if label == "older-skill":
                    self.assertEqual((audit / SNAPSHOT).read_bytes(), older.read_bytes())

    def test_a_snapshot_this_init_created_that_another_init_bound_is_kept(self) -> None:
        # The run.json guard: B creates the snapshot, something binds it, and B then fails after the freeze.
        bind = (f"{shlex.quote(sys.executable)} -c "
                + shlex.quote("import hashlib, json, pathlib, sys; audit = pathlib.Path(sys.argv[1]); "
                              "digest = hashlib.sha256((audit / 'pipeline-policy.snapshot.json').read_bytes()).hexdigest(); "
                              "(audit / 'run.json').write_text(json.dumps({'toolkit': "
                              "{'pipeline_policy_snapshot_sha256': digest}}) + '\\n')"))
        # A process that holds no init lock (an older skill's init_run.sh, say) writes a run.json binding it.
        audit = make_audit(self.root / "created-then-bound-without-the-lock" / "audit")
        result, log = self.race(audit, f"{bind} {shlex.quote(str(audit))}", "--skill-dir", str(self.empty_skill),
                                when="after")
        self.assertIn("hook rc=0", log, log)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"NOTE: kept {audit / SNAPSHOT}, which this init froze: {audit / 'run.json'} binds its bytes",
                      result.stderr)
        self.assertTrue((audit / SNAPSHOT).is_file(), "B removed a snapshot a run.json binds")
        self.assertEqual(json.loads((audit / "run.json").read_text(encoding="utf-8"))["toolkit"][
            "pipeline_policy_snapshot_sha256"], sha(audit / SNAPSHOT))
        # Another init_run.sh, started after B's freeze: before init-r2-2 it reused B's snapshot and bound it inside
        # B's freeze, and the guard kept it; now it waits for B's lock and binds a snapshot of its own.
        audit = make_audit(self.root / "created-then-bound" / "audit")
        command, other_log, _ = self.other_init(audit, "created-then-bound")
        result, log = self.race(audit, self.as_hook(command), "--skill-dir", str(self.empty_skill), when="after")
        self.assertIn("hook rc=0", log, log)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no gate files found", result.stderr)
        code, other = self.finish(other_log)
        self.assertEqual(code, 0, other)
        self.assertTrue((audit / SNAPSHOT).is_file(), "B removed a snapshot A bound")
        self.assertTrue(binds(audit))
        # Negative control: InitLeavesNoSnapshot.test_a_failure_after_the_freeze_removes_the_snapshot_it_froze --
        # with no other process, the snapshot B created is removed.


class ConcurrentInitsAreSerialised(RaceFixture):
    """init-r2-2: the init-2 cleanup kept a snapshot another init had bound, but not one another init had loaded, as a
    matching leftover, and not yet bound. B created the snapshot; A loaded and hashed it and spent its package scan
    (minutes on a large deposit) before writing run.json; B failed and removed the snapshot; A then bound a file that
    no longer existed, rc 0 -- an audit whose runners refuse "snapshot is missing" and whose init refuses "run.json
    already exists". Two inits could also both pass the run.json check and write run.json over each other. init_run.sh
    now holds <audit>/.init.lock from its run.json check through the journal, so the other init waits and then sees
    what the first left. The scan gate pauses A exactly where the review's A was when B failed."""

    def test_an_init_that_loaded_this_inits_snapshot_binds_one_that_exists(self) -> None:
        audit = make_audit(self.root / "loaded-not-bound" / "audit")
        command, other_log, gate = self.other_init(audit, "loaded", gate=True)
        result, log = self.race(audit, self.as_hook(command), "--skill-dir", str(self.empty_skill), when="after")
        self.assertIn("hook rc=0", log, log)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no gate files found", result.stderr)
        code, other = self.finish(other_log, gate)
        self.assertEqual(code, 0, other)
        self.assertTrue((audit / SNAPSHOT).is_file(),
                        f"the other init bound a snapshot this init had removed:\n{other}")
        self.assertTrue(binds(audit))
        self.assertIn(WAITED, other, "the other init did not wait for this one")
        self.assertEqual(json.loads((audit / "run.json").read_text(encoding="utf-8"))["audit_id"], "t-270-other")
        self.assertFalse((audit / LOCK_FILE).exists(), "an init left its lock file")

    def test_a_second_init_waits_and_then_refuses_the_run_json_the_first_wrote(self) -> None:
        # The older race the lock closes too: both inits passed the run.json check; the second's run.json was then
        # overwritten by the first, which died on the second's journal ("a canonical journal already exists").
        audit = make_audit(self.root / "second-init" / "audit")
        command, other_log, _ = self.other_init(audit, "second")
        result, log = self.race(audit, self.as_hook(command), when="after")
        self.assertIn("hook rc=0", log, log)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        code, other = self.finish(other_log)
        self.assertEqual(code, 2, other)
        self.assertIn(WAITED, other)
        self.assertIn(f"FATAL: {audit / 'run.json'} already exists. init_run.sh runs ONCE per audit", other)
        self.assertTrue(binds(audit))
        self.assertEqual(json.loads((audit / "run.json").read_text(encoding="utf-8"))["audit_id"], "t-270")
        self.assertNotIn("t-270-other", (audit / JOURNAL).read_text(encoding="utf-8"))
        self.assertFalse((audit / LOCK_FILE).exists(), "an init left its lock file")

    def test_a_third_init_waits_for_the_init_that_took_the_lock_over(self) -> None:
        # The lock file is removed on release, so an init that waited on it holds the lock of a removed file once the
        # holder ends. It must lock the file at the path instead (lock anew), or a third init that creates that file
        # runs beside it. B holds the lock, A waits; B fails and releases; A takes over and pauses in its scan; C starts.
        audit = make_audit(self.root / "third-init" / "audit")
        command, log_a, gate = self.other_init(audit, "a", gate=True)
        result, log = self.race(audit, self.as_hook(command), "--skill-dir", str(self.empty_skill), when="after")
        self.assertIn("hook rc=0", log, log)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.wait_for(pathlib.Path(f"{gate}.scanning"), "the second init never reached its package scan")
        self.assertTrue((audit / LOCK_FILE).is_file(), "the second init holds no lock file at the path")
        command_c, log_c, _ = self.other_init(audit, "c")
        subprocess.run(command_c, check=True, timeout=180, env=clean_env(PATH=self.shim_path()))
        started_c = log_c.read_text(encoding="utf-8")
        self.assertIn(WAITED, started_c, f"a third init ran beside the init holding the lock:\n{started_c}")
        self.assertFalse(pathlib.Path(f"{log_c}.rc").exists(), started_c)
        code_a, output_a = self.finish(log_a, gate)
        code_c, output_c = self.finish(log_c)
        self.assertEqual(code_a, 0, output_a)
        self.assertIn(WAITED, output_a)
        self.assertEqual(code_c, 2, output_c)
        self.assertIn("already exists. init_run.sh runs ONCE per audit", output_c)
        self.assertTrue(binds(audit))
        self.assertFalse((audit / LOCK_FILE).exists(), "an init left its lock file")


class FailedForcedReinitRestoresTheSnapshot(ReviewedInitFixture):
    """init-3: a --force that failed after archiving the snapshot left the audit with none: the old run.json and
    journal bound the archived file, the runners refused for want of a snapshot, and nothing said where it went."""

    def test_a_failed_force_puts_the_archived_sonnet_5_snapshot_back(self) -> None:
        def empty_skill(audit: pathlib.Path) -> list[str]:
            return ["--skill-dir", str(self.empty_skill)]

        def work_is_a_file(audit: pathlib.Path) -> list[str]:   # the review's scenario
            shutil.rmtree(audit / "work")
            (audit / "work").write_text("not a directory\n", encoding="utf-8")
            return []

        for label, prepare, message in (("gate-digest", empty_skill, "no gate files found"),
                                        ("work-is-a-file", work_is_a_file, "could not create")):
            with self.subTest(failure=label):
                audit = self.initialised(label, sonnet_5=True)
                args = prepare(audit)
                before = listing(audit)
                result = init(audit, "--force", *args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)
                self.assertTrue((audit / SNAPSHOT).is_file(), "the failed --force left no live snapshot")
                self.assertEqual(list(audit.glob(ARCHIVES)), [])
                self.assertEqual(listing(audit), before, "the audit is not as the failed --force found it")
                self.assertTrue(binds(audit))
                self.assertEqual(frozen_route(audit), claude_routes.SONNET_5)
                self.assertIn(f"restored the archived one to {audit / SNAPSHOT}", result.stderr)

    def test_a_failed_force_on_the_shipped_route_keeps_the_bound_snapshot(self) -> None:
        # The forced freeze wrote the bytes the old run.json binds, so the run.json guard keeps that file (and the
        # archive, a copy, stays where the message says); before the review it was removed, leaving none.
        audit = self.initialised("sonnet-5-5", sonnet_5=False)
        snapshot, run = (audit / SNAPSHOT).read_bytes(), (audit / "run.json").read_bytes()
        result = init(audit, "--force", "--skill-dir", str(self.empty_skill))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no gate files found", result.stderr)
        self.assertTrue((audit / SNAPSHOT).is_file(), "the failed --force left no live snapshot")
        self.assertEqual((audit / SNAPSHOT).read_bytes(), snapshot)
        self.assertEqual((audit / "run.json").read_bytes(), run)
        self.assertTrue(binds(audit))
        archived = list(audit.glob(ARCHIVES))
        self.assertEqual([path.read_bytes() for path in archived], [snapshot])
        self.assertIn(f"stays archived at {archived[0]}", result.stderr)
        # Negative controls: InitRefusesALeftoverSnapshot's --force test -- a successful --force archives the old
        # snapshot and re-pins, and a refused one archives nothing.

    def test_a_force_never_overwrites_an_archive_made_the_same_second(self) -> None:
        # (2.7.0 review, TD-4) The archive is named by the second, pipeline-policy.snapshot.previous.<UTC
        # %Y%m%dT%H%M%SZ>.json; mv would overwrite one made in the same second, and the trap would then put back the
        # wrong file. Decoys for every second the init can run in stand for that archive: whenever init takes its
        # timestamp, the name is taken, so it must refuse and change nothing.
        audit = self.initialised("same-second", sonnet_5=True)
        span = 60
        start = int(time.time())
        for offset in range(span + 1):
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(start + offset))
            (audit / f"pipeline-policy.snapshot.previous.{stamp}.json").write_text(
                f"decoy archive of {stamp}\n", encoding="utf-8")
        before = listing(audit)   # the live snapshot, run.json, the journal and every decoy
        self.assertEqual(len(list(audit.glob(ARCHIVES))), span + 1)
        result = init(audit, "--force")
        self.assertLess(time.time(), start + span, "init outran the decoys; the test proves nothing")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        refused = re.search(r"^FATAL: (.+/pipeline-policy\.snapshot\.previous\.(\d{8}T\d{6}Z)\.json) already exists "
                            r"\(a snapshot was archived this second\); re-run init_run\.sh$", result.stderr,
                            flags=re.MULTILINE)
        self.assertIsNotNone(refused, result.stderr)
        self.assertEqual(refused.group(1), str(audit / f"pipeline-policy.snapshot.previous.{refused.group(2)}.json"))
        self.assertEqual(listing(audit), before, "the refused --force changed the audit or wrote a new archive")
        self.assertTrue(binds(audit))
        self.assertEqual(frozen_route(audit), claude_routes.SONNET_5)
        self.assertNotIn("restored the archived one", result.stderr)
        self.assertFalse((audit / LOCK_FILE).exists())
        # Negative control: InitRefusesALeftoverSnapshot's --force test -- with no archive of that second, --force
        # archives the snapshot and re-pins.


class PrintedInitCommands(unittest.TestCase):
    """The init commands this release prints are ones its init_run.sh accepts."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-270-printed-")
        self.root = pathlib.Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_printed(self, command: str, audit: pathlib.Path) -> subprocess.CompletedProcess:
        command = command.replace("<doi>", "10.1234/x").replace("<title>", "'A title'")
        return subprocess.run(["/bin/bash", "-c", command], cwd=SKILL, text=True, capture_output=True,
                              timeout=300, env=clean_env(AUDIT_DIR=str(audit)))

    def test_the_skill_md_command_is_accepted(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        match = re.search(r"^bash scripts/init_run\.sh .*?(?<!\\)\n", skill, flags=re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match)
        self.assertIn("--auditor-model sonnet-5-5", match.group(0))
        audit = make_audit(self.root / "skill-md" / "audit")
        result = self.run_printed(match.group(0), audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Negative control: the 2.6.10 command is refused, and leaves nothing behind.
        audit = make_audit(self.root / "skill-md-2610" / "audit")
        result = self.run_printed(match.group(0).replace("sonnet-5-5", "sonnet-5"), audit)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse((audit / SNAPSHOT).exists())

    def test_the_new_audit_sh_command_is_accepted(self) -> None:
        source = (SCRIPTS / "new_audit.sh").read_text(encoding="utf-8")
        snippet = source[source.index("# The target, cap, model and provider printed"):]
        printed = subprocess.run(["/bin/bash", "-c", snippet], text=True, capture_output=True, timeout=60,
                                 env=clean_env(HERE=str(SCRIPTS))).stdout
        self.assertIn("--auditor-model sonnet-5-5 --auditor-family claude --auditor-provider anthropic", printed)
        self.assertIn("--round-target 2 --round-cap 7", printed)
        audit = make_audit(self.root / "new-audit" / "audit")
        result = self.run_printed(printed, audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Negative control: with the policy unreadable nothing is guessed; the command falls back to the
        # flag-free form, whose every value init takes from the snapshot it freezes.
        fallback = subprocess.run(["/bin/bash", "-c", snippet], text=True, capture_output=True, timeout=60,
                                  env=clean_env(HERE=str(self.root / "no-skill" / "scripts"))).stdout
        self.assertNotIn("--auditor-model", fallback)
        self.assertIn("init_run.sh", fallback)


if __name__ == "__main__":
    unittest.main(verbosity=2)
