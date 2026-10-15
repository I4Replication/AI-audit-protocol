"""2.7.0: new audits run on Claude Sonnet 5.5; every audit that exists stays on Sonnet 5, byte for byte.

Every Claude role (the coordinator, the five Phase-1 auditors, their capability probe and the
post-fan-out verifier) takes its route from the audit's own frozen pipeline-policy.snapshot.json;
scripts/claude_routes.py holds the one allow-set both generations need. Each class below proves one
part of that through real code on disk -- the runners as subprocesses with the fake CLIs, never a
provider -- and every guard has a negative control:

* ``ClaudeRoutesModule`` -- the allow-set, the per-route served-model predicates, the prompt names
  and the environment scrub.
* ``SnapshotRoutePins`` -- run_phase1.load_policy, the coordinator pin and the verifier pin accept
  exactly the packaged routes (one shared by all five auditors, the verifier on the auditors' own)
  and read them from the snapshot, never from config/.
* ``PromptsStay2610`` -- the auditor, coordinator and verifier prompts for a Sonnet 5 packet and for a
  packet with no model block are the 2.6.10 bytes (pinned, and re-derived from a pristine 2.6.10
  tree when I4R_BASE_2610 names one); a Sonnet 5.5 packet differs by the model sentence alone.
* ``ServedModelChecks`` -- Sonnet 5 keeps the loose pre-2.7.0 check and its messages; Sonnet 5.5 is
  exact: a mixed Sonnet 5 + 5.5 call is refused, a Haiku helper is not; the records carry the route.
* ``RecoveryToolsReadTheSnapshotRoute`` -- both coordinator recovery tools judge a record against the
  snapshot's coordinator route. ``MisplacedOutputRecoveryReadsTheSnapshotRoute`` runs the misplaced-output tool's
  main on real defective attempts of each route, and ``QuarantinedCoordinatorServedRecheck`` serves a quarantined
  attempt other ids: the recovery recheck is exact on Sonnet 5.5 and 2.6.10's on Sonnet 5 (2.7.0 review, TD-2/3).
* ``Sonnet55Audit`` / ``Sonnet55Coordinator`` -- a Sonnet 5.5 snapshot end to end: probe, five
  auditors, replays, verifier and coordinator spawn --model claude-sonnet-5-5 and record sonnet-5-5.
* ``SpawnEnvironment`` -- no Claude CLI subprocess sees the variables that remap the alias `sonnet`.

The snapshots are built in memory from config/pipeline-policy-v2.json with the route substituted,
so these tests hold whichever route the shipped policy names.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import importlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import claude_routes  # noqa: E402
import test_264_verifier_runtime as verifier_tests  # noqa: E402  (its Phase-1 fixture)


def _module(name: str):
    """Resolve a scripts module at use: several suites swap sys.modules entries during discovery."""
    return importlib.import_module(name)


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SONNET_5 = claude_routes.SONNET_5
SONNET_5_5 = claude_routes.SONNET_5_5
# 2.7.3: what a new audit freezes; SONNET_5_5 (max) is what an audit initialised on 2.7.0 to 2.7.2 keeps.
SONNET_5_5_XHIGH = claude_routes.SONNET_5_5_XHIGH


def slug(route: dict[str, str]) -> str:
    """A name unique among the live routes (2.7.3: two of them are Sonnet 5.5, at max and at xhigh)."""
    return f"{route['requested_model']}-{route['reasoning_effort']}"
LEGACY_SENTENCE = "Use Claude Sonnet 5 at max effort."
NEW_SENTENCE = "Use Claude Sonnet 5.5 at max effort."
PHASE1_SERVED = "claude-sonnet-5-20260801"  # what tests/fake_claude_phase1.py serves
COORDINATOR_SERVED = "claude-sonnet-5"      # what tests/fake_claude_coordinator.py serves
REMAPPING = {"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5-5", "ANTHROPIC_MODEL": "claude-opus-5"}
KEPT = {"I4R_TEST_ENV_KEPT": "kept"}


def route_policy(route: dict[str, str], *, verifier: dict[str, str] | None = None,
                 coordinator: dict[str, str] | None = None) -> dict:
    """The shipped policy with every Claude role on ``route``, built in memory; config/ is untouched."""
    policy = json.loads((SKILL / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))
    models = policy["models"]
    for row in models["phase_1_auditors"].values():
        row.update(route)
    models["coordinator"].update(coordinator or route)
    models["post_fan_out_verifier"].update(verifier or route)
    policy["phase_1"]["capability_probe"]["requested_model"] = route["requested_model"]
    # the probe runs at the auditors' effort, as every snapshot a skill froze has it (2.7.3)
    policy["phase_1"]["capability_probe"]["reasoning_effort"] = route["reasoning_effort"]
    return policy


def freeze(root: pathlib.Path, policy: dict) -> str:
    """Write the snapshot record_invocation.initialize_policy_snapshot writes, for ``policy``."""
    record_invocation = _module("record_invocation")
    envelope = {"snapshot_version": "1", "policy_sha256": record_invocation.policy_hash(policy),
                "policy": policy}
    path = root / record_invocation.POLICY_SNAPSHOT
    path.write_bytes((json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode())
    return digest(path)


def minimal_audit(base: pathlib.Path, name: str, policy: dict, *, metadata21: bool = False) -> pathlib.Path:
    """A run.json bound to a frozen snapshot, and nothing else."""
    root = base / name
    root.mkdir(parents=True)
    (root / "run.json").write_text(json.dumps({
        "schema_version": "2.0.0", "audit_id": f"audit-{name}", "normalization": None,
        "toolkit": {"pipeline_policy_snapshot_sha256": freeze(root, policy)},
    }) + "\n", encoding="utf-8")
    (root / "invocations.jsonl").write_text("", encoding="utf-8")
    if metadata21:
        _module("lifecycle_journal").initialize(root)
    return root


# A fake Claude CLI around another fake: it logs each spawn's argv and the watched environment
# variables to I4R_TEST_SPAWN_LOG, runs the inner fake unchanged, and renames the served model ids
# in the result envelope per I4R_TEST_SERVED ({old id: [new ids]}, each new id keeping the usage of
# the id it replaces).
WRAPPER = r'''#!@PYTHON@
import json, os, subprocess, sys
argv = sys.argv[1:]
log = os.environ.get("I4R_TEST_SPAWN_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd(),
                                 "env": {name: os.environ.get(name) for name in @WATCHED@}},
                                sort_keys=True) + "\n")
stdin = "" if "--version" in argv else sys.stdin.read()
done = subprocess.run([sys.executable, @INNER@, *argv], input=stdin, text=True, capture_output=True)
out = done.stdout
served = json.loads(os.environ.get("I4R_TEST_SERVED") or "{}")
try:
    envelope = json.loads(out) if served else None
except ValueError:  # --version prints plain text
    envelope = None
if isinstance(envelope, dict):
    usage = envelope.get("modelUsage")
    if isinstance(usage, dict):
        renamed = {}
        for key, counts in usage.items():
            for new in served.get(key, [key]):
                renamed[new] = dict(counts)
        envelope["modelUsage"] = renamed
    out = json.dumps(envelope, separators=(",", ":")) + "\n"
sys.stdout.write(out)
sys.stderr.write(done.stderr)
raise SystemExit(done.returncode)
'''


def write_wrapper(path: pathlib.Path, inner: pathlib.Path) -> pathlib.Path:
    watched = sorted([*claude_routes.REMAPPING_ENV, *KEPT])
    path.write_text(WRAPPER.replace("@PYTHON@", sys.executable).replace("@INNER@", repr(str(inner)))
                    .replace("@WATCHED@", repr(watched)), encoding="utf-8")
    path.chmod(0o755)
    return path


def spawn_log(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def model_of(argv: list[str]) -> str:
    return argv[argv.index("--model") + 1]


# ------------------------------------------------------------------------------ the route module
class ClaudeRoutesModule(unittest.TestCase):
    # run_coordinator.sonnet5 in 2.6.10, measured on the pristine tree (see PromptsStay2610's
    # base test, which re-derives it): the loose check every existing audit was judged by.
    LEGACY_2610 = {
        "claude-sonnet-5": True, "claude-sonnet-5-20260801": True, "CLAUDE-SONNET-5": True,
        "claude-sonnet-5-5": True, "claude-sonnet-5-5-20261001": True, "claude-sonnet-5-5[1m]": True,
        "claude-sonnet-5[1m]": False, "claude-sonnet-55": False, "claude-sonnet-6": False,
        "claude-haiku-4-5": False, "claude-haiku-4-5-20251001": False, "claude-opus-5": False,
        "sonnet-5": True, "us.anthropic.claude-sonnet-5-v1:0": True, "": False,
    }
    STRICT_5_5 = {
        "claude-sonnet-5-5": True, "claude-sonnet-5-5-20261001": True, "claude-sonnet-5-5[1m]": True,
        "CLAUDE-SONNET-5-5": True,
        "claude-sonnet-5": False, "claude-sonnet-5-20260801": False, "claude-sonnet-5[1m]": False,
        "claude-sonnet-5.5": False, "claude-sonnet-5-5-5": False, "claude-sonnet-5-5-x": False,
        "sonnet-5-5": False, "us.anthropic.claude-sonnet-5-5-v1:0": False, "claude-haiku-4-5": False,
        "": False,
    }

    def test_the_allow_set_is_the_legacy_route_and_the_full_id_5_5_route(self) -> None:
        self.assertEqual(claude_routes.LIVE_ROUTES, (SONNET_5, SONNET_5_5, SONNET_5_5_XHIGH))
        self.assertIs(claude_routes.SHIPPED, SONNET_5_5_XHIGH)
        self.assertEqual(SONNET_5_5_XHIGH, {**SONNET_5_5, "reasoning_effort": "xhigh"})
        self.assertEqual(SONNET_5, {"requested_model": "sonnet-5", "spawn_alias": "sonnet",
                                    "provider": "anthropic", "reasoning_effort": "max"})
        # A full id, never an alias: on CLI 2.1.206 the alias `sonnet` still serves claude-sonnet-5.
        self.assertEqual(SONNET_5_5, {"requested_model": "sonnet-5-5", "spawn_alias": "claude-sonnet-5-5",
                                      "provider": "anthropic", "reasoning_effort": "max"})
        for route in claude_routes.LIVE_ROUTES:
            with self.subTest(route=slug(route)):
                # Extra policy keys (area, must_equal_auditor_family) are not part of a route.
                row = {**route, "area": 1, "must_equal_auditor_family": True}
                self.assertEqual(claude_routes.live_route(row), route)
                self.assertEqual(claude_routes.require_live_route(row, "x"), route)
                self.assertIsNot(claude_routes.live_route(row), route, "a copy, never the constant")
        # Negative controls: every other route, and anything that is not a mapping.
        for label, row in (
            ("5.5 spawned by the alias", {**SONNET_5_5, "spawn_alias": "sonnet"}),
            ("5 spawned by the 5.5 id", {**SONNET_5, "spawn_alias": "claude-sonnet-5-5"}),
            ("high effort", {**SONNET_5_5, "reasoning_effort": "high"}),
            ("openai", {**SONNET_5, "provider": "openai"}),
            ("opus", {"requested_model": "opus-5", "spawn_alias": "opus", "provider": "anthropic",
                      "reasoning_effort": "max"}),
            ("a missing key", {key: value for key, value in SONNET_5.items() if key != "provider"}),
            ("None", None), ("a string", "sonnet-5"),
        ):
            with self.subTest(refused=label):
                self.assertIsNone(claude_routes.live_route(row))
                with self.assertRaises(claude_routes.RouteError) as caught:
                    claude_routes.require_live_route(row, "auditor_reproduction")
                message = str(caught.exception)
                self.assertIn("auditor_reproduction is not a packaged Claude route", message)
                self.assertIn("Sonnet 5 (sonnet-5, spawned as --model sonnet) / max", message)
                self.assertIn("Sonnet 5.5 (sonnet-5-5, spawned as --model claude-sonnet-5-5) / max", message)

    def test_the_legacy_check_is_the_2610_one_and_the_5_5_check_is_exact(self) -> None:
        coordinator = _module("run_coordinator")
        for model, expected in self.LEGACY_2610.items():
            with self.subTest(legacy=model):
                self.assertIs(claude_routes.is_sonnet_5_legacy(model), expected)
                self.assertIs(coordinator.sonnet5(model), expected, "the importers' alias")
                self.assertIs(claude_routes.serves("sonnet-5", model), expected)
                self.assertIs(claude_routes.serves(None, model), expected)
        for model, expected in self.STRICT_5_5.items():
            with self.subTest(strict=model):
                self.assertIs(claude_routes.serves("sonnet-5-5", model), expected)
        # The looseness is kept for the legacy route only (negative control on the same ids).
        self.assertTrue(claude_routes.serves("sonnet-5", "claude-sonnet-5-5"))
        self.assertFalse(claude_routes.serves("sonnet-5-5", "claude-sonnet-5"))

    def test_primaries_separate_a_helper_from_a_conflicting_sonnet(self) -> None:
        cases = {
            ("sonnet-5-5", ("claude-sonnet-5-5", "claude-haiku-4-5")): (["claude-sonnet-5-5"], []),
            ("sonnet-5-5", ("claude-sonnet-5-5", "claude-sonnet-5")): (["claude-sonnet-5-5"], ["claude-sonnet-5"]),
            ("sonnet-5-5", ("claude-sonnet-5",)): ([], ["claude-sonnet-5"]),
            ("sonnet-5-5", ("claude-haiku-4-5",)): ([], []),
            ("sonnet-5-5", ("claude-sonnet-6", "claude-sonnet-5-5")): (["claude-sonnet-5-5"], ["claude-sonnet-6"]),
            # The legacy route never reports a conflict: both loose matches are primaries, and two
            # primaries refuse the call exactly as in 2.6.10.
            ("sonnet-5", ("claude-sonnet-5", "claude-sonnet-5-5")): (["claude-sonnet-5", "claude-sonnet-5-5"], []),
            ("sonnet-5", ("claude-sonnet-5-20260801", "claude-haiku-4-5")): (["claude-sonnet-5-20260801"], []),
            (None, ("claude-sonnet-6",)): ([], []),
        }
        for (requested, ids), expected in cases.items():
            with self.subTest(requested=requested, ids=ids):
                self.assertEqual(claude_routes.primaries(requested, ids), expected)

    def test_names_default_to_the_legacy_route(self) -> None:
        for requested in (None, "sonnet-5", "", "opus-5"):
            with self.subTest(requested=requested):
                self.assertEqual(claude_routes.display_name(requested), "Claude Sonnet 5")
                self.assertEqual(claude_routes.short_name(requested), "Sonnet 5")
                self.assertEqual(claude_routes.model_id(requested), "claude-sonnet-5")
        self.assertEqual(claude_routes.display_name("sonnet-5-5"), "Claude Sonnet 5.5")
        self.assertEqual(claude_routes.short_name("sonnet-5-5"), "Sonnet 5.5")
        self.assertEqual(claude_routes.model_id("sonnet-5-5"), "claude-sonnet-5-5")
        self.assertEqual(claude_routes.requested_model_of({"model": {"requested_model": "sonnet-5-5"}}),
                         "sonnet-5-5")
        for packet in ({}, {"model": None}, {"model": "sonnet-5-5"}, {"model": {"requested_model": 5}},
                       None, ["model"]):
            with self.subTest(packet=packet):
                self.assertIsNone(claude_routes.requested_model_of(packet))

    def test_the_spawn_environment_drops_only_the_remapping_variables(self) -> None:
        self.assertEqual(set(claude_routes.REMAPPING_ENV), set(REMAPPING))
        source = {**REMAPPING, **KEPT, "PATH": "/usr/bin", "ANTHROPIC_API_KEY": "placeholder"}
        scrubbed = claude_routes.spawn_env(source)
        self.assertEqual(scrubbed, {**KEPT, "PATH": "/usr/bin", "ANTHROPIC_API_KEY": "placeholder"})
        self.assertEqual(source["ANTHROPIC_MODEL"], "claude-opus-5", "the operator environment is not mutated")
        with mock.patch.dict(os.environ, {**REMAPPING, **KEPT}):
            default = claude_routes.spawn_env()
            self.assertIn("ANTHROPIC_MODEL", os.environ)
        self.assertEqual(default["I4R_TEST_ENV_KEPT"], "kept")
        self.assertFalse(set(REMAPPING) & set(default))


# ----------------------------------------------------------------------------- the runtime pins
class SnapshotRoutePins(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-270-pins-")
        self.base = pathlib.Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_load_policy_accepts_each_packaged_route_from_the_snapshot_not_config(self) -> None:
        run_phase1 = _module("run_phase1")
        record_invocation = _module("record_invocation")
        for route in claude_routes.LIVE_ROUTES:
            with self.subTest(route=slug(route)):
                policy = route_policy(route)
                root = minimal_audit(self.base, slug(route), policy)
                # config/ is never read for an existing audit's route: take it away entirely.
                with mock.patch.object(record_invocation, "POLICY", self.base / "no-such-policy.json"):
                    loaded, configured = run_phase1.load_policy(root)
                self.assertEqual(loaded, policy)
                # The rows come back unchanged (area kept): they feed the packets and probe keys.
                self.assertEqual(configured, policy["models"]["phase_1_auditors"])
                self.assertTrue(all(row["area"] for row in configured.values()))

    def test_load_policy_refuses_any_other_route_and_a_mixed_quorum(self) -> None:
        run_phase1 = _module("run_phase1")
        cases = {
            "alias-5-5": ({**SONNET_5_5, "spawn_alias": "sonnet"}, "is not a packaged Claude route"),
            "high": ({**SONNET_5, "reasoning_effort": "high"}, "is not a packaged Claude route"),
            "opus": ({**SONNET_5, "requested_model": "opus-5", "spawn_alias": "opus"},
                     "is not a packaged Claude route"),
        }
        for name, (route, fragment) in cases.items():
            with self.subTest(refused=name):
                root = minimal_audit(self.base, name, route_policy(route))
                with self.assertRaisesRegex(run_phase1.RunnerError, fragment):
                    run_phase1.load_policy(root)
        for first, rest in ((SONNET_5_5, SONNET_5), (SONNET_5, SONNET_5_5)):
            with self.subTest(mixed=first["requested_model"]):
                policy = route_policy(rest)
                policy["models"]["phase_1_auditors"]["auditor_reproduction"].update(first)
                root = minimal_audit(self.base, f"mixed-{first['requested_model']}", policy)
                with self.assertRaisesRegex(run_phase1.RunnerError, "must share one Claude route") as caught:
                    run_phase1.load_policy(root)
                self.assertIn("auditor_reproduction: " + claude_routes.describe(first), str(caught.exception))

    def run_coordinator_once(self, root: pathlib.Path) -> str:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = _module("run_coordinator").run_once(
                [str(root), "--safe-evidence-manifest", str(root / "missing.json")])
        self.assertNotEqual(code, 0)
        self.assertFalse((root / "coordinator").exists(), "nothing is dispatched or reserved")
        return stderr.getvalue()

    def test_the_coordinator_pin_accepts_each_packaged_route_and_refuses_any_other(self) -> None:
        record_invocation = _module("record_invocation")
        for route in claude_routes.LIVE_ROUTES:
            with self.subTest(route=slug(route)):
                root = minimal_audit(self.base, f"coordinator-{slug(route)}", route_policy(route))
                with mock.patch.object(record_invocation, "POLICY", self.base / "no-such-policy.json"):
                    message = self.run_coordinator_once(root)
                # It gets past the pin and stops at the missing Phase-1 reports.
                self.assertNotIn("packaged Claude route", message)
                self.assertIn("canonical Phase-1 report", message)
        for name, coordinator in (
            ("alias-5-5", {**SONNET_5_5, "spawn_alias": "sonnet"}),
            ("extra-key", {**SONNET_5, "must_equal_auditor_family": True}),
            ("opus", {**SONNET_5, "requested_model": "opus-5", "spawn_alias": "opus"}),
        ):
            with self.subTest(refused=name):
                policy = route_policy(SONNET_5)
                policy["models"]["coordinator"] = coordinator
                root = minimal_audit(self.base, f"coordinator-{name}", policy)
                message = self.run_coordinator_once(root)
                self.assertIn("audit-scoped coordinator policy is not a packaged Claude route", message)

    def plan(self, root: pathlib.Path) -> str:
        run_verifier = _module("run_verifier")
        args = argparse.Namespace(timeout_seconds=None, max_budget_usd="15")  # 2.7.2: None = the audit's own
        with self.assertRaises(run_verifier.VerifierRefusal) as caught:
            run_verifier.plan_batch(root, root / "phase1", ["F13"], args)
        self.assertTrue(caught.exception.remedy.strip())
        return caught.exception.reason

    def test_the_verifier_pin_accepts_the_auditors_packaged_route_only(self) -> None:
        record_invocation = _module("record_invocation")
        for route in claude_routes.LIVE_ROUTES:
            for declared in (True, False):
                with self.subTest(route=slug(route), declared=declared):
                    policy = route_policy(route)
                    if not declared:  # a pre-2.6.4 snapshot: the verifier takes the auditors' route
                        policy["models"].pop("post_fan_out_verifier")
                    root = minimal_audit(self.base, f"verifier-{slug(route)}-{declared}",
                                         policy, metadata21=True)
                    with mock.patch.object(record_invocation, "POLICY", self.base / "no-such-policy.json"):
                        reason = self.plan(root)
                    # Past both pins, stopped at the missing quorum.
                    self.assertIn("quorum.json is missing", reason)
        for name, auditors, verifier, fragment in (
            ("5-5-on-5", SONNET_5, SONNET_5_5, "is not the Phase-1 auditors' route"),
            ("5-on-5-5", SONNET_5_5, SONNET_5, "is not the Phase-1 auditors' route"),
            ("high", SONNET_5_5, {**SONNET_5_5, "reasoning_effort": "high"}, "is not a packaged Claude route"),
            ("alias", SONNET_5_5, {**SONNET_5_5, "spawn_alias": "sonnet"}, "is not a packaged Claude route"),
        ):
            with self.subTest(refused=name):
                root = minimal_audit(self.base, f"verifier-{name}", route_policy(auditors, verifier=verifier),
                                     metadata21=True)
                self.assertIn(fragment, self.plan(root))


# ------------------------------------------------------------------------------------- prompts
# The renderer the pins below were measured with, run against any skill tree in a fresh process.
RENDERER = r'''
import hashlib, json, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1] + "/scripts")
import run_coordinator, run_phase1, run_verifier
spec = json.load(sys.stdin)
out = {}
for key, (kind, args) in spec["prompts"].items():
    build = {"role": run_phase1.build_role_prompt, "coordinator": run_coordinator.build_prompt,
             "verifier": run_verifier.build_verifier_prompt}[kind]
    out[key] = hashlib.sha256(build(*args).encode("utf-8")).hexdigest()
legacy = {model: run_coordinator.sonnet5(model) for model in spec["models"]}
observed = [list(run_coordinator.observed_models(result)) for result in spec["results"]]
print(json.dumps({"prompts": out, "legacy": legacy, "observed": observed}, sort_keys=True))
'''


class PromptsStay2610(unittest.TestCase):
    AUDIT_ID = "20260101-audit-00000000"
    VERIFIER_ROLE = "verifier_0123456789ab"
    # sha256 of each prompt as the pristine 2.6.10 tree renders it for the inputs in spec() below.
    PINNED_2610 = {
        "coordinator": "5166dcff95803ce7e6460ded9779d019dcb29b115cf38d3fcc56227968c4664f",
        "role:auditor_code_methods": "2c67cfa91612841e8ad1ac6918770c8e5571c95ffd3cb311bfd8d8b8086d342a",
        "role:auditor_data_integrity": "f41839880b857260b5fd30877a57a1429c92814e5f78e3311b721f742aeb28c7",
        "role:auditor_privacy_archive": "721ff8d30cbdadfcd3b314c80ef4b4bf08edcfdc86108242f2c0fd64dd9a1aa8",
        "role:auditor_reproduction": "6f74c61bca8656f451c0d1e40c720a1b403aee51f2dce18ca9bceaa48c4fa15b",
        "role:auditor_text_vs_data": "02548a59e0bf99f1bfe6b80ff96845606acf638d75aba3eea7fa47b1f9d03732",
        "role-nomodel:auditor_code_methods": "12b34a5aa83b69bcc9ee9eb70df7a01db01cd688588d757a8a0e48a7756c01c6",
        "role-nomodel:auditor_data_integrity": "5530d70a379d238099ef515f147c20bca68101852eba2e0b37d7b206b2107f6b",
        "role-nomodel:auditor_privacy_archive": "8cbbe4cebef60df9cb275d90fd118d436dc19cfb020c8ce9ab0d323f94dc9c5d",
        "role-nomodel:auditor_reproduction": "ec3942f901dce3624f45dc8bbb186a41a52cdd6cb62a8a4ecd8facec153809b2",
        "role-nomodel:auditor_text_vs_data": "8b77271743f0434a28f3d4623f279a32a5c2c54605fa548ba9541d724a0671e2",
        # Both equal tests/test_prompt_contract.py's VERIFIER_PROMPT_267: the verifier prompt has
        # not moved since 2.6.7, so every published verifier record still re-admits.
        "verifier": "75e937e9460529a752217c7121f5c1257173aeb84d33141da66881953d2e610a",
        "verifier-nomodel": "75e937e9460529a752217c7121f5c1257173aeb84d33141da66881953d2e610a",
    }

    def manifest(self) -> dict:
        names = ["archive_licence_metadata.json", "code_entrypoints.json", "environment_fingerprint.json",
                 "file_inventory.json", "pdf_text.txt", "privacy_scan.json", "workbook_summaries.json"]
        return {
            "snapshot_sha256": "1" * 64,
            "source_files": [{"path": "paper/paper.txt", "sha256": "2" * 64, "bytes": 10}],
            "common_evidence": {"artifacts": [
                {"path": name, "sha256": f"{index + 4:x}" * 64, "bytes": 100 + index}
                for index, name in enumerate(names)
            ]},
        }

    def role_packet(self, role: str, route: dict[str, str]) -> dict:
        return _module("phase1_runtime").make_role_packet(role, self.manifest(), {**route, "area": 1})

    def verifier_packet(self, route: dict[str, str] | None) -> dict:
        packet = {"source_snapshot_sha256": "a" * 64, "hypotheses_sha256": "b" * 64,
                  "hypotheses": [{"hypothesis_id": "H1", "claim_sha256": "c" * 64},
                                 {"hypothesis_id": "H2", "claim_sha256": "d" * 64}]}
        if route is not None:
            packet["model"] = {key: route[key] for key in ("requested_model", "spawn_alias", "reasoning_effort")}
        return packet

    def spec(self) -> dict:
        prompts: dict[str, tuple[str, list]] = {}
        for role in _module("phase1_runtime").ROLE_SPECS:
            prompts[f"role:{role}"] = ("role", [role, self.AUDIT_ID, self.role_packet(role, SONNET_5)])
            prompts[f"role-nomodel:{role}"] = ("role", [role, "audit-test", {"source_snapshot_sha256": "0" * 64}])
        prompts["coordinator"] = ("coordinator", ["audit-test", "inv-test", "0" * 64])
        prompts["verifier"] = ("verifier", [self.VERIFIER_ROLE, self.AUDIT_ID, self.verifier_packet(SONNET_5)])
        prompts["verifier-nomodel"] = ("verifier", [self.VERIFIER_ROLE, self.AUDIT_ID, self.verifier_packet(None)])
        results = [{"modelUsage": {model: {} for model in ids}} for ids in ServedModelChecks.OBSERVED_2610]
        results.append({"model": "claude-sonnet-5"})
        return {"prompts": prompts, "models": sorted(ClaudeRoutesModule.LEGACY_2610), "results": results}

    def build(self, kind: str, args: list, requested_model: str | None = "absent") -> str:
        if kind == "role":
            return _module("run_phase1").build_role_prompt(*args)
        if kind == "verifier":
            return _module("run_verifier").build_verifier_prompt(*args)
        coordinator = _module("run_coordinator")
        return (coordinator.build_prompt(*args) if requested_model == "absent"
                else coordinator.build_prompt(*args, requested_model))

    def test_legacy_and_modelless_packets_render_the_2610_bytes(self) -> None:
        for key, (kind, args) in self.spec()["prompts"].items():
            with self.subTest(prompt=key):
                self.assertEqual(digest_text(self.build(kind, args)), self.PINNED_2610[key])
        # The coordinator's route argument: none, or the legacy route, is the 2.6.10 prompt that
        # recover_coordinator.py compares a quarantined attempt's prompt.txt against.
        for requested in (None, "sonnet-5"):
            with self.subTest(coordinator=requested):
                prompt = self.build("coordinator", ["audit-test", "inv-test", "0" * 64], requested)
                self.assertEqual(digest_text(prompt), self.PINNED_2610["coordinator"])
        # The pre-2.6.5 rebuild still gives the 2.6.4 prompt for a packet that carries a model block.
        run_verifier = _module("run_verifier")
        legacy = run_verifier.pre_265_prompt(self.build("verifier", self.spec()["prompts"]["verifier"][1]))
        self.assertEqual(digest_text(legacy), "f6591dfa53f0f370e0bdfcb59a1bbed1f14f117397db59f5788457f819318479")

    def test_the_2610_role_inputs_are_unchanged(self) -> None:
        # The whole auditor identity, not just the prompt: packet, prompt and probe key hash to the
        # values 2.6.10 computes (tests/test_264_verifier_runtime.PINNED_263 pins them from 2.6.3).
        run_phase1, runtime = _module("run_phase1"), _module("phase1_runtime")
        # Every audit initialised before 2.7.1 froze the legacy probe bound (tests/test_271_probe_bound.py).
        legacy = run_phase1.LEGACY_PROBE_BOUND
        key, _ = run_phase1.probe_key("2.1.206 (Fake Claude Code)", SONNET_5, pathlib.Path("/i4r/pin/audit"),
                                      bound=legacy)
        self.assertEqual(key, verifier_tests.PINNED_263["probe_key"])
        key_5_5, components = run_phase1.probe_key("2.1.206 (Fake Claude Code)", SONNET_5_5,
                                                   pathlib.Path("/i4r/pin/audit"), bound=legacy)
        self.assertNotEqual(key_5_5, key, "a Sonnet 5.5 audit probes and keys its auditors afresh")
        self.assertEqual((components["model"], components["model_alias"]), ("sonnet-5-5", "claude-sonnet-5-5"))
        for role in runtime.ROLE_SPECS:
            with self.subTest(role=role):
                old = self.role_packet(role, SONNET_5)
                new = self.role_packet(role, SONNET_5_5)
                self.assertNotEqual(runtime.role_input_hash(new, run_phase1.build_role_prompt(role, "a", new), key_5_5),
                                    runtime.role_input_hash(old, run_phase1.build_role_prompt(role, "a", old), key))

    def test_a_sonnet_5_5_packet_differs_by_the_model_sentence_alone(self) -> None:
        for role in _module("phase1_runtime").ROLE_SPECS:
            with self.subTest(role=role):
                legacy = self.build("role", [role, self.AUDIT_ID, self.role_packet(role, SONNET_5)])
                new = self.build("role", [role, self.AUDIT_ID, self.role_packet(role, SONNET_5_5)])
                self.assertEqual(legacy.count(LEGACY_SENTENCE), 1)
                self.assertEqual(new, legacy.replace(LEGACY_SENTENCE, NEW_SENTENCE, 1))
                self.assertNotIn(LEGACY_SENTENCE, new)
        legacy = self.build("verifier", [self.VERIFIER_ROLE, self.AUDIT_ID, self.verifier_packet(SONNET_5)])
        new = self.build("verifier", [self.VERIFIER_ROLE, self.AUDIT_ID, self.verifier_packet(SONNET_5_5)])
        self.assertEqual(new, legacy.replace(LEGACY_SENTENCE, NEW_SENTENCE, 1))
        self.assertNotIn(LEGACY_SENTENCE, new)
        # The rule statement still occurs once, so a 5.5 record's pre-2.6.5 rebuild is well defined.
        self.assertIsNotNone(_module("run_verifier").pre_265_prompt(new))
        args = ["audit-test", "inv-test", "0" * 64]
        legacy = self.build("coordinator", args, None)
        new = self.build("coordinator", args, "sonnet-5-5")
        self.assertEqual(legacy.count("`claude-sonnet-5`"), 1)
        self.assertEqual(new, legacy.replace(LEGACY_SENTENCE, NEW_SENTENCE, 1)
                         .replace("`claude-sonnet-5`", "`claude-sonnet-5-5`", 1))
        self.assertNotIn(LEGACY_SENTENCE, new)

    @unittest.skipUnless(os.environ.get("I4R_BASE_2610"), "set I4R_BASE_2610 to a pristine 2.6.10 skill tree")
    def test_the_pins_are_what_a_2610_tree_renders(self) -> None:
        base = pathlib.Path(os.environ["I4R_BASE_2610"]).resolve()
        self.assertEqual((base / "VERSION").read_text().strip(), "2.6.10")
        spec = self.spec()
        rendered = {}
        for label, tree in (("2.6.10", base), ("this tree", SKILL)):
            result = subprocess.run(
                [sys.executable, "-B", "-c", RENDERER, str(tree)], input=json.dumps(spec), text=True,
                capture_output=True, check=False, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered[label] = json.loads(result.stdout)
        self.assertEqual(rendered["2.6.10"]["prompts"], self.PINNED_2610)
        self.assertEqual(rendered["this tree"], rendered["2.6.10"])
        self.assertEqual(rendered["2.6.10"]["legacy"], ClaudeRoutesModule.LEGACY_2610)
        self.assertEqual([tuple(row) for row in rendered["2.6.10"]["observed"]],
                         [*ServedModelChecks.OBSERVED_2610.values(), ("claude-sonnet-5", ["claude-sonnet-5"], 1)])


# ---------------------------------------------------------------------------- served models
class ServedModelChecks(unittest.TestCase):
    # observed_models in 2.6.10 on these served ids: (primary, ids, count). The legacy route (and a
    # caller that names none) must return exactly this.
    OBSERVED_2610 = {
        ("claude-sonnet-5-20260801", "claude-haiku-4-5"): ("claude-sonnet-5-20260801", ["claude-sonnet-5-20260801", "claude-haiku-4-5"], 1),
        ("claude-sonnet-5", "claude-sonnet-5-5"): ("claude-sonnet-5", ["claude-sonnet-5", "claude-sonnet-5-5"], 2),
        ("claude-haiku-4-5", "claude-sonnet-5-5"): ("claude-sonnet-5-5", ["claude-haiku-4-5", "claude-sonnet-5-5"], 1),
        ("claude-haiku-4-5",): ("claude-haiku-4-5", ["claude-haiku-4-5"], 0),
        (): (None, [], 0),
    }

    def result(self, ids) -> dict:
        counts = {"inputTokens": 10, "outputTokens": 5, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}
        return {"type": "result", "subtype": "success", "is_error": False,
                "modelUsage": {model: dict(counts) for model in ids},
                "num_turns": 1, "duration_api_ms": 100, "total_cost_usd": 0.01}

    def test_observed_models_is_2610_on_the_legacy_route_and_exact_on_5_5(self) -> None:
        coordinator = _module("run_coordinator")
        for ids, expected in self.OBSERVED_2610.items():
            for requested in ("default", None, "sonnet-5"):
                with self.subTest(ids=ids, requested=requested):
                    result = self.result(ids)
                    observed = (coordinator.observed_models(result) if requested == "default"
                                else coordinator.observed_models(result, requested))
                    self.assertEqual(observed, expected)
        self.assertEqual(coordinator.observed_models({"model": "claude-sonnet-5"}, "sonnet-5"),
                         ("claude-sonnet-5", ["claude-sonnet-5"], 1))
        strict = {
            ("claude-sonnet-5-5",): ("claude-sonnet-5-5", 1),
            ("claude-sonnet-5-5", "claude-haiku-4-5"): ("claude-sonnet-5-5", 1),  # a helper is fine
            ("claude-haiku-4-5", "claude-sonnet-5-5-20261001"): ("claude-sonnet-5-5-20261001", 1),
            ("claude-sonnet-5-5", "claude-sonnet-5"): ("claude-sonnet-5-5", 2),  # mixed: refused
            ("claude-sonnet-5", "claude-sonnet-5-5"): ("claude-sonnet-5", 2),
            ("claude-sonnet-5",): ("claude-sonnet-5", 0),                        # misrouted: refused
            ("claude-haiku-4-5", "claude-sonnet-5"): ("claude-haiku-4-5", 0),
            (): (None, 0),
        }
        for ids, (primary, count) in strict.items():
            with self.subTest(strict=ids):
                self.assertEqual(coordinator.observed_models(self.result(ids), "sonnet-5-5"),
                                 (primary, list(ids), count))

    def execution(self, route: dict[str, str], ids, directory: pathlib.Path):
        run_phase1, coordinator = _module("run_phase1"), _module("run_coordinator")
        result = self.result(ids)
        usage, cache = coordinator.extract_usage(result)
        observed, observed_ids, _count = coordinator.observed_models(result)
        now = dt.datetime.now(dt.timezone.utc)
        return run_phase1.ProviderExecution(
            reservation={"invocation_id": "phase1-test"}, attempt=1, provider_request_status="accepted",
            inference_started=True, provider_started=True, returncode=0, stdout="", stderr="",
            timed_out=False, started_at=now, ended_at=now, result=result, observed_model=observed,
            observed_model_ids=observed_ids, usage=usage, cache_usage=cache, output=directory,
            attempt_dir=directory, route=dict(route), requested_session_id="session-test",
        )

    def test_execution_errors_judge_the_dispatched_route_with_the_2610_messages(self) -> None:
        run_phase1 = _module("run_phase1")
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            for route, ids, expected in (
                (SONNET_5, ("claude-sonnet-5-20260801", "claude-haiku-4-5"), None),
                (SONNET_5, ("claude-sonnet-5-5",), None),  # the loose legacy check, unchanged
                (SONNET_5, ("claude-sonnet-5", "claude-sonnet-5-5"),
                 "expected exactly one served Sonnet 5 primary; observed ['claude-sonnet-5', 'claude-sonnet-5-5']"),
                (SONNET_5, ("claude-haiku-4-5",),
                 "expected exactly one served Sonnet 5 primary; observed ['claude-haiku-4-5']"),
                (SONNET_5_5, ("claude-sonnet-5-5", "claude-haiku-4-5"), None),
                (SONNET_5_5, ("claude-sonnet-5-5-20261001",), None),
                (SONNET_5_5, ("claude-sonnet-5-5", "claude-sonnet-5"),
                 "expected exactly one served Sonnet 5.5 primary; observed ['claude-sonnet-5-5', 'claude-sonnet-5']"),
                (SONNET_5_5, ("claude-sonnet-5",),
                 "expected exactly one served Sonnet 5.5 primary; observed ['claude-sonnet-5']"),
            ):
                with self.subTest(route=route["requested_model"], ids=ids):
                    errors = run_phase1.execution_errors(self.execution(route, ids, path))
                    served = [error for error in errors if "served" in error]
                    self.assertEqual(served, [expected] if expected else [], errors)
                    self.assertEqual(errors, served, "nothing else is wrong with the fixture")
                    if expected:
                        self.assertEqual(run_phase1.classify_failure(expected), "model")

    def test_the_terminal_record_carries_the_dispatched_route(self) -> None:
        run_phase1 = _module("run_phase1")
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            for route, ids in ((SONNET_5, (PHASE1_SERVED,)), (SONNET_5_5, ("claude-sonnet-5-5",))):
                with self.subTest(route=slug(route)):
                    record = run_phase1.make_terminal_record(
                        self.execution(route, ids, path), "auditor_reproduction", "role_inference",
                        "a" * 64, "b" * 64, status="completed", output_artifact="report/01_reproduction.md",
                        output_hash="c" * 64, error=None, error_class=None,
                    )
                    self.assertEqual((record["requested_model"], record["reasoning_effort"], record["provider"]),
                                     (route["requested_model"], "max", "anthropic"))
                    self.assertEqual(record["observed_model"], ids[0])

    def test_a_record_validates_only_against_its_own_snapshot_route(self) -> None:
        record_invocation = _module("record_invocation")
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory).resolve()
            roots = {slug(route): minimal_audit(base, slug(route), route_policy(route))
                     for route in claude_routes.LIVE_ROUTES}
            for audit, root in roots.items():
                (root / "work").mkdir()
                (root / "work/auditor.md").write_text("complete\n")
                for route in claude_routes.LIVE_ROUTES:
                    record = {
                        "invocation_id": f"test-{slug(route)}", "role": "auditor_reproduction",
                        "role_class": "auditor", "round": 0, "attempt": 1, "status": "completed",
                        "provider": "anthropic", "provider_call": True, "cache_usage": "none",
                        "session_id": "session-test", "requested_model": route["requested_model"],
                        "observed_model": claude_routes.model_id(route["requested_model"]),
                        "observed_model_ids": [claude_routes.model_id(route["requested_model"])],
                        "model_family": "claude", "model_source": "observed", "reasoning_effort": route["reasoning_effort"],
                        "started_at": "2026-09-29T10:00:00Z", "ended_at": "2026-09-29T10:01:00Z",
                        "elapsed_seconds": 60, "input_artifact_sha256": "a" * 64, "prompt_sha256": "b" * 64,
                        "output_artifact": "work/auditor.md", "output_sha256": digest(root / "work/auditor.md"),
                        "usage": {"obtainable": True, "input_tokens": 10, "output_tokens": 5,
                                  "cached_input_tokens": 0, "reasoning_tokens": 2, "total_tokens": 17,
                                  "source": "api_response"},
                        "error": None, "error_class": None, "legacy_source": None,
                    }
                    with self.subTest(audit=audit, record=slug(route)):
                        if slug(route) == audit:
                            record_invocation.validate_record(record, root)
                        else:
                            with self.assertRaisesRegex(ValueError, "recorded request violates pipeline policy"):
                                record_invocation.validate_record(record, root)


# ----------------------------------------------------------------------------- recovery tools
class RecoveryToolsReadTheSnapshotRoute(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-270-recovery-")
        self.base = pathlib.Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def coordinator_row(self, route: dict[str, str], **changes) -> dict:
        row = {
            "invocation_id": "coordinator-x", "role": "coordinator", "role_class": "coordinator",
            "round": None, "attempt": 3, "status": "failed", "provider": "anthropic",
            "provider_call": True, "requested_model": route["requested_model"], "model_family": "claude",
            "reasoning_effort": route["reasoning_effort"], "input_artifact_sha256": "a" * 64,
            "error_class": "schema", "output_artifact": None, "output_sha256": None,
            "provider_request_status": "accepted", "inference_start_status": "started",
            "error": _module("recover_misplaced_coordinator_outputs").DEFECT_PREFIX + ": REPORT_v1.md",
        }
        row.update(changes)
        return row

    def test_the_misplaced_output_profile_is_the_snapshot_route(self) -> None:
        recovery = _module("recover_misplaced_coordinator_outputs")
        for audit in claude_routes.LIVE_ROUTES:
            for recorded in claude_routes.LIVE_ROUTES:
                with self.subTest(audit=slug(audit), recorded=slug(recorded)):
                    rows = [self.coordinator_row(recorded)]
                    if audit == recorded:
                        self.assertIs(recovery.failed_record(rows, "coordinator-x", 3, "a" * 64, audit), rows[0])
                    else:
                        # 2.7.3: two live routes share Sonnet 5.5 and differ only in the effort
                        with self.assertRaisesRegex(recovery.RecoveryError,
                                                    "profile: (requested_model|reasoning_effort)"):
                            recovery.failed_record(rows, "coordinator-x", 3, "a" * 64, audit)
        # No route is the one every earlier audit froze (the pre-2.7.0 callers).
        self.assertTrue(recovery.failed_record([self.coordinator_row(SONNET_5)], "coordinator-x", 3, "a" * 64))
        with self.assertRaises(recovery.RecoveryError):
            recovery.failed_record([self.coordinator_row(SONNET_5_5)], "coordinator-x", 3, "a" * 64)

    def envelope(self, attempt: pathlib.Path, ids) -> dict:
        coordinator = _module("run_coordinator")
        attempt.mkdir(parents=True)
        result = {"type": "result", "subtype": "success", "is_error": False, "terminal_reason": "completed",
                  "stop_reason": "end_turn", "session_id": "session-x", "permission_denials": [],
                  "modelUsage": {model: {"inputTokens": 10, "outputTokens": 5, "cacheReadInputTokens": 0,
                                         "cacheCreationInputTokens": 0} for model in ids},
                  "num_turns": 2, "duration_api_ms": 1000}
        (attempt / "claude_stdout.json").write_text(json.dumps(result), encoding="utf-8")
        (attempt / "claude_stderr.txt").write_text("", encoding="utf-8")
        (attempt / "permission_denials.json").write_text(json.dumps({
            "count": 0, "tool_names": [], "raw_stdout_sha256": digest(attempt / "claude_stdout.json"),
        }), encoding="utf-8")
        usage, cache = coordinator.extract_usage(result)
        primary, all_models, _count = coordinator.observed_models(result)
        return {"observed_model": primary, "observed_model_ids": all_models, "usage": usage,
                "cache_usage": cache, "session_id": "session-x"}

    def test_the_misplaced_output_envelope_is_judged_on_the_recorded_route(self) -> None:
        recovery = _module("recover_misplaced_coordinator_outputs")
        for number, (route, ids, fragment) in enumerate((
            (SONNET_5, ("claude-sonnet-5", "claude-haiku-4-5"), None),
            (SONNET_5_5, ("claude-sonnet-5-5", "claude-haiku-4-5"), None),
            (SONNET_5_5, ("claude-sonnet-5-5", "claude-sonnet-5"), "served Sonnet 5.5 primary"),
            (SONNET_5_5, ("claude-sonnet-5",), "served Sonnet 5.5 primary"),
            (SONNET_5, ("claude-sonnet-5", "claude-sonnet-5-5"), "served Sonnet 5 primary"),
        )):
            with self.subTest(route=route["requested_model"], ids=ids):
                attempt = self.base / f"attempt-{number}"
                record = {**self.coordinator_row(route), **self.envelope(attempt, ids)}
                if fragment is None:
                    self.assertEqual(recovery.validate_envelope(attempt, record)["primary"], ids[0])
                else:
                    with self.assertRaisesRegex(recovery.RecoveryError, fragment):
                        recovery.validate_envelope(attempt, record)

    def test_the_quarantined_record_identity_is_the_snapshot_route(self) -> None:
        recover_coordinator = _module("recover_coordinator")
        coordinator = _module("run_coordinator")
        for audit in claude_routes.LIVE_ROUTES:
            root = minimal_audit(self.base, f"quarantine-{slug(audit)}", route_policy(audit))
            for recorded in claude_routes.LIVE_ROUTES:
                with self.subTest(audit=slug(audit), recorded=slug(recorded)):
                    attempt = root / f"attempt-{slug(recorded)}"
                    attempt.mkdir()
                    (attempt / "invocation_record.json").write_text(json.dumps({
                        "invocation_id": "coordinator-x", "role": "coordinator", "role_class": "coordinator",
                        "round": None, "attempt": 1, "status": "completed", "provider": "anthropic",
                        "provider_call": True, "requested_model": recorded["requested_model"],
                        "model_family": "claude", "model_source": "observed", "reasoning_effort": recorded["reasoning_effort"],
                        "input_artifact_sha256": "a" * 64, "error": None, "error_class": None,
                        "legacy_source": None, "started_at": "2026-09-29T10:00:00Z",
                        "ended_at": "2026-09-29T10:01:00Z", "elapsed_seconds": 60,
                    }), encoding="utf-8")
                    with self.assertRaises(coordinator.CoordinatorError) as caught:
                        recover_coordinator.corrected_record_only(root, attempt, "coordinator-x", 1, "a" * 64)
                    if audit == recorded:
                        # Past the identity check: its elapsed time is consistent, so nothing to recover.
                        self.assertIn("recoverable elapsed-time inconsistency", str(caught.exception))
                    else:
                        # 2.7.3: two live routes share Sonnet 5.5 and differ only in the effort
                        self.assertRegex(str(caught.exception),
                                         "non-timing identity defects: (requested_model|reasoning_effort)")


def coordinator_on_route(case: unittest.TestCase, route: dict[str, str], *, metadata21: bool = False):
    """test_run_coordinator's fixture (borrowed, as Sonnet55Coordinator does), frozen on ``route`` before any
    coordinator call: the snapshot, run.json's binding and the Phase-1 rows follow the route."""
    fixture = coordinator_fixture()(
        methodName="test_success_is_content_bound_promotes_and_records_all_models_and_usage")
    fixture.setUp()
    case.addCleanup(fixture.tearDown)
    run = json.loads((fixture.root / "run.json").read_text())
    run["toolkit"]["pipeline_policy_snapshot_sha256"] = freeze(fixture.root, route_policy(route))
    (fixture.root / "run.json").write_text(json.dumps(run) + "\n")
    served = claude_routes.model_id(route["requested_model"])
    rows = fixture.ledger()
    for row in rows:
        row.update(requested_model=route["requested_model"], reasoning_effort=route["reasoning_effort"],
                   observed_model=served,
                   observed_model_ids=[served, "claude-haiku-4-5"])
    (fixture.root / "invocations.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    if metadata21:
        fixture.upgrade_to_metadata_21()
    return fixture


@contextlib.contextmanager
def writable(*paths: pathlib.Path):
    """Lift the read-only modes the runner froze an attempt with, and put them back."""
    modes = {path: path.stat().st_mode & 0o7777 for path in paths}
    try:
        for path in paths:
            path.chmod(0o755 if path.is_dir() else 0o644)
        yield
    finally:
        for path, mode in modes.items():
            if path.exists():
                path.chmod(mode)


# The fake Claude CLI the misplaced-output fixture spawns: it lifts the read-only attempt root the runner sets for the
# call -- the files then land where the production attempt's did, beside output.partial -- and runs the fake.
LIFTING_FAKE = r'''#!@PYTHON@
import os, pathlib, sys
if "--version" not in sys.argv:
    os.chmod(pathlib.Path.cwd().parent, 0o755)
os.execv(@PYTHON_REPR@, [@PYTHON_REPR@, @FAKE@, *sys.argv[1:]])
'''


class MisplacedOutputRecoveryReadsTheSnapshotRoute(unittest.TestCase):
    """(2.7.0 review, TD-2) recover_misplaced_coordinator_outputs.main reads the coordinator route from the audit's
    snapshot and hands it to failed_record; given no route, failed_record falls back to Sonnet 5, so a main that
    dropped it would refuse every Sonnet 5.5 audit, and no test ran main. Each case is a real attempt with the defect:
    run_coordinator.py on a metadata-2.1 audit, the fake CLI in its misplaced_outputs scenario with the read-only
    attempt root lifted, and then the tool's main, --dry-run, as a subprocess."""

    def misplaced_attempt(self, route: dict[str, str]):
        fixture = coordinator_on_route(self, route, metadata21=True)
        fake = pathlib.Path(fixture.temporary.name) / "claude-lifting-the-attempt-root"
        fake.write_text(LIFTING_FAKE.replace("@PYTHON_REPR@", repr(sys.executable)).replace("@PYTHON@", sys.executable)
                        .replace("@FAKE@", repr(str(fixture.fake))), encoding="utf-8")
        fake.chmod(0o755)
        fixture.fake = fake
        result = fixture.run_runner("misplaced_outputs")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        row = fixture.ledger()[-1]
        recovery = _module("recover_misplaced_coordinator_outputs")
        self.assertEqual((row["role"], row["status"], row["error_class"], row["requested_model"]),
                         ("coordinator", "failed", "schema", route["requested_model"]))
        self.assertTrue(row["error"].startswith(recovery.DEFECT_PREFIX), row["error"])
        attempt = fixture.attempt()
        # What the fake does not do as the CLI does (test_recover_coordinator's quarantine does the same): it leaves its
        # own fake_args.json in output.partial, and its envelope lacks terminal_reason and stop_reason.
        stdout, denials = attempt / "claude_stdout.json", attempt / "permission_denials.json"
        with writable(attempt, attempt / "output.partial", stdout, denials):
            (attempt / "output.partial" / "fake_args.json").unlink()
            envelope = json.loads(stdout.read_text())
            envelope.update(terminal_reason="completed", stop_reason="end_turn")
            stdout.write_text(json.dumps(envelope, separators=(",", ":")) + "\n")
            denials.write_text(json.dumps({"count": 0, "tool_names": [], "raw_stdout_sha256": digest(stdout)}))
        return fixture, attempt

    def dry_run(self, fixture, attempt: pathlib.Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "recover_misplaced_coordinator_outputs.py"), str(fixture.root),
             "--attempt", attempt.relative_to(fixture.root).as_posix(), "--dry-run"],
            text=True, capture_output=True, timeout=120, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    @staticmethod
    def state(fixture, attempt: pathlib.Path) -> dict[str, str]:
        """Every file of the attempt and the audit's canonical outputs and ledger: what a dry run may not change."""
        paths = [*sorted(attempt.rglob("*")), fixture.root / "invocations.jsonl", fixture.root / "run.events.jsonl",
                 *(fixture.root / target for target in _module("run_coordinator").OUTPUT_TARGETS.values())]
        return {str(path): digest(path) if path.is_file() else ("dir" if path.is_dir() else "absent")
                for path in paths}

    def test_a_sonnet_5_5_attempt_passes_every_precondition(self) -> None:
        fixture, attempt = self.misplaced_attempt(SONNET_5_5)
        before = self.state(fixture, attempt)
        result = self.dry_run(fixture, attempt)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual((report["status"], report["dry_run"], report["attempt"]), ("preconditions_ok", True, 1))
        self.assertEqual(sorted(report["misplaced_outputs"]), sorted(_module("run_coordinator").OUTPUT_TARGETS))
        self.assertEqual(self.state(fixture, attempt), before, "the dry run moved or wrote something")

    def test_a_sonnet_5_attempt_passes_as_in_2610(self) -> None:
        # Negative control: the route every earlier audit froze, read from its snapshot (config/ ships Sonnet 5.5).
        fixture, attempt = self.misplaced_attempt(SONNET_5)
        before = self.state(fixture, attempt)
        result = self.dry_run(fixture, attempt)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["status"], "preconditions_ok")
        self.assertEqual(self.state(fixture, attempt), before)

    def test_a_row_on_another_route_than_the_snapshot_is_refused(self) -> None:
        # A sonnet-5-5 row on a Sonnet 5 snapshot. The ledger's own validation refuses it before failed_record's
        # profile check ("misplaced-output profile: requested_model"), which RecoveryToolsReadTheSnapshotRoute
        # exercises directly; either way nothing is moved.
        fixture, attempt = self.misplaced_attempt(SONNET_5)
        ledger = fixture.root / "invocations.jsonl"
        rows = fixture.ledger()
        rows[-1]["requested_model"] = SONNET_5_5["requested_model"]
        ledger.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
        before = self.state(fixture, attempt)
        result = self.dry_run(fixture, attempt)
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertIn("requested_model='sonnet-5-5', expected 'sonnet-5'", result.stderr)
        self.assertNotIn("preconditions_ok", result.stdout)
        self.assertEqual(self.state(fixture, attempt), before)


class QuarantinedCoordinatorServedRecheck(unittest.TestCase):
    """(2.7.0 review, TD-3) recover_coordinator.validate_provider_and_bundle rechecks the served model on the route the
    snapshot names -- exactly on Sonnet 5.5, with 2.6.10's loose check on Sonnet 5 -- and no test served it anything
    but the fake's own ids. Each case is test_recover_coordinator's quarantined attempt (a real run_coordinator.py
    call on the route, recoverable but for its elapsed time) re-served: the envelope, the record and the bundle all
    name the served ids, so the recheck alone decides. A refusal runs recover_coordinator.py and promotes nothing."""

    def quarantined(self, route: dict[str, str]):
        fixture = coordinator_on_route(self, route)
        result = fixture.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        recovery = _module("test_recover_coordinator").CoordinatorRecoveryTests(
            methodName="test_refuses_changed_provider_model")
        recovery.fixture, recovery.root = fixture, fixture.root
        recovery.attempt = fixture.attempt()
        recovery.invocation_id = recovery.attempt.name.split("_", 3)[-1]
        recovery.attempt_relative = recovery.attempt.relative_to(fixture.root).as_posix()
        recovery._quarantine_success()
        return recovery

    def serve(self, recovery, ids: tuple[str, ...]) -> None:
        coordinator = _module("run_coordinator")
        stdout = recovery.attempt / "claude_stdout.json"
        envelope = json.loads(stdout.read_text())
        counts = list(envelope["modelUsage"].values())
        envelope["modelUsage"] = {model: dict(counts[min(index, len(counts) - 1)]) for index, model in enumerate(ids)}
        stdout.write_text(json.dumps(envelope, separators=(",", ":")) + "\n")
        (recovery.attempt / "permission_denials.json").write_text(json.dumps(
            {"count": 0, "tool_names": [], "raw_stdout_sha256": digest(stdout)}, indent=2, sort_keys=True) + "\n")
        usage, cache_usage = coordinator.extract_usage(envelope)
        record = json.loads((recovery.attempt / "invocation_record.json").read_text())
        record.update(observed_model=ids[0], observed_model_ids=list(ids), usage=usage, cache_usage=cache_usage)
        bundle = json.loads((recovery.attempt / "coordinator_bundle.json").read_text())
        bundle.update(observed_model=ids[0], observed_model_ids=list(ids), provider_stdout_sha256=digest(stdout))
        recovery._write_bundle_and_rebind_record(bundle, record)

    def refused(self, recovery, ids: tuple[str, ...], message: str) -> None:
        self.serve(recovery, ids)
        result = recovery.run_recovery()
        self.assertIn(message, result.stderr)
        recovery.assert_refused_without_promotion(result)

    def accepted(self, recovery, ids: tuple[str, ...]) -> None:
        """Past the recheck and every other precondition, without committing (the fixture is reused)."""
        self.serve(recovery, ids)
        try:
            state = _module("recover_coordinator").validate_recovery(recovery.root.resolve(), recovery.attempt.resolve())
        except _module("run_coordinator").CoordinatorError as exc:
            self.fail(f"the recheck refused {list(ids)}: {exc}")
        self.assertEqual(state.corrected_record["observed_model_ids"], list(ids))

    def recovered(self, recovery, ids: tuple[str, ...]) -> None:
        self.serve(recovery, ids)
        result = recovery.run_recovery()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = recovery.coordinator_rows()
        self.assertEqual([(row["status"], row["observed_model_ids"]) for row in rows], [("completed", list(ids))])

    def test_the_sonnet_5_5_recheck_is_exact(self) -> None:
        recovery = self.quarantined(SONNET_5_5)
        for ids, count in ((("claude-sonnet-5-5", "claude-sonnet-5"), 2),        # mixed: the loose check counts 2 too
                           (("claude-sonnet-5",), 0),                             # the loose check accepts it
                           (("claude-sonnet-5-5", "claude-sonnet-4-5"), 2)):      # the loose check counts 1
            with self.subTest(served=ids):
                self.refused(recovery, ids,
                             f"expected exactly one served Sonnet 5.5 primary; observed {count}: {list(ids)!r}")
        # Negative controls: a dated id beside the Haiku helper passes the recheck, and the fake's own ids recover.
        with self.subTest(served="dated"):
            self.accepted(recovery, ("claude-sonnet-5-5-20260801", "claude-haiku-4-5"))
        with self.subTest(served="the fake's own"):
            self.recovered(recovery, ("claude-sonnet-5-5", "claude-haiku-4-5"))

    def test_the_sonnet_5_recheck_is_the_2610_one(self) -> None:
        # 2.6.10's check (run_coordinator.sonnet5) also matches claude-sonnet-5-5: a Sonnet 5 audit served the pair is
        # refused as two primaries, with 2.6.10's message, and one served claude-sonnet-5-5 alone is accepted.
        recovery = self.quarantined(SONNET_5)
        with self.subTest(served="mixed"):
            self.refused(recovery, ("claude-sonnet-5", "claude-sonnet-5-5"),
                         "expected exactly one served Sonnet 5 primary; observed 2: "
                         "['claude-sonnet-5', 'claude-sonnet-5-5']")
        with self.subTest(served="the fake's own"):
            self.accepted(recovery, ("claude-sonnet-5", "claude-haiku-4-5"))
        with self.subTest(served="claude-sonnet-5-5 alone"):
            self.recovered(recovery, ("claude-sonnet-5-5",))


# --------------------------------------------------------------------- a Sonnet 5.5 audit on disk
class RouteFixture(verifier_tests.VerifierFixture):
    """test_264_verifier_runtime's Phase-1 fixture on an explicit route, through the logging wrapper."""

    ROUTE = SONNET_5_5
    SERVED: dict[str, list[str]] = {PHASE1_SERVED: ["claude-sonnet-5-5"]}
    EXTRA_ENV: dict[str, str] = {}

    def setUp(self) -> None:
        super().setUp()
        self.spawns = self.base / "spawns.jsonl"
        self.fake = write_wrapper(self.base / "claude-wrapper", TESTS / "fake_claude_phase1.py")

    def env(self, scenario: str, target: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = super().env(scenario, target, {"I4R_TEST_SERVED": json.dumps(self.SERVED),
                                             "I4R_TEST_SPAWN_LOG": str(self.spawns),
                                             **self.EXTRA_ENV, **(extra or {})})
        return env

    def make_audit(self, name: str = "audit", *, metadata21: bool = True,
                   audit_id: str | None = None, policy: dict | None = None) -> pathlib.Path:
        policy = policy or route_policy(self.ROUTE)

        def initialize(root: pathlib.Path) -> dict:
            freeze(root, policy)
            return policy

        with mock.patch.object(_module("record_invocation"), "initialize_policy_snapshot", initialize):
            return super().make_audit(name, metadata21=metadata21, audit_id=audit_id)

    def starts_in(self, root: pathlib.Path) -> list[dict]:
        return [row for row in self.starts() if row["cwd"].startswith(str(root))]

    def provider_rows(self, root: pathlib.Path) -> list[dict]:
        return [row for row in self.ledger(root) if row.get("provider_call")]


class Sonnet55Audit(RouteFixture):
    def assert_on_5_5(self, row: dict) -> None:
        self.assertEqual((row["requested_model"], row["reasoning_effort"], row["provider"],
                          row["model_family"]), ("sonnet-5-5", "max", "anthropic", "claude"), row["role"])

    def test_phase1_replay_and_verifier_spawn_and_record_the_5_5_route(self) -> None:
        root = self.audit_after_phase1()
        starts = self.starts_in(root)
        self.assertEqual(len(starts), 6, "one probe and five auditors")
        self.assertEqual({model_of(row["argv"]) for row in starts}, {"claude-sonnet-5-5"})
        rows = self.provider_rows(root)
        self.assertEqual({row["role"] for row in rows}, {"phase1_capability_probe", *verifier_tests.phase1_runtime.ROLE_SPECS})
        for row in rows:
            self.assert_on_5_5(row)
            self.assertEqual((row["observed_model"], row["served_model"]), ("claude-sonnet-5-5", "claude-sonnet-5-5"))
        reserved = [event["payload"] for event in verifier_tests.lifecycle_journal.load_events(root)
                    if event["type"] == "invocation_reserved"]
        self.assertEqual(len(reserved), 6)
        self.assertEqual({(row["requested_model"], row["reasoning_effort"]) for row in reserved}, {("sonnet-5-5", "max")})
        prompt = next((root / "phase1/roles/auditor_reproduction").glob("*/attempt_*/prompt.txt")).read_text()
        self.assertTrue(prompt.startswith("You are an independent I4R Phase-1 auditor. " + NEW_SENTENCE), prompt[:120])
        self.assertNotIn(LEGACY_SENTENCE, prompt)
        packet = json.loads(next((root / "phase1/role_packets").glob("*/auditor_reproduction/*/ROLE_PACKET.json")).read_text())
        self.assertEqual(packet["model"], {"requested_model": "sonnet-5-5", "spawn_alias": "claude-sonnet-5-5",
                                           "reasoning_effort": "max"})
        cache = json.loads(next((root / "phase1/probe_cache").glob("*.json")).read_text())
        self.assertEqual((cache["key_components"]["model"], cache["key_components"]["model_alias"]),
                         ("sonnet-5-5", "claude-sonnet-5-5"))
        self.assertEqual(cache["served_model"], "claude-sonnet-5-5")

        # A re-run replays the probe and every auditor from their receipts, on the same route.
        again = self.run_phase1(root)
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertEqual(len(self.starts_in(root)), 6, "a replay makes no provider call")
        replays = [row for row in self.ledger(root) if row.get("attempt_kind") == "cache_replay"]
        self.assertEqual(len(replays), 6)
        for row in replays:
            self.assert_on_5_5(row)

        # The verifier runs on the auditors' route, and its record re-admits.
        result = self.run_verifier(root, ["F13"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        verifier_starts = self.verifier_starts()
        self.assertEqual(len(verifier_starts), 1)
        self.assertEqual(model_of(verifier_starts[0]["argv"]), "claude-sonnet-5-5")
        verifier_rows = [row for row in self.verifier_rows(root) if row.get("provider_call")]
        self.assertEqual(len(verifier_rows), 1)
        self.assert_on_5_5(verifier_rows[0])
        self.assertEqual(verifier_rows[0]["observed_model"], "claude-sonnet-5-5")
        verdicts, errors = self.verdicts(root)
        self.assertEqual(errors, [])
        self.assertEqual([verdict for _id, verdict in verdicts[self.claim_hash("F13")]], ["CONFIRMED"])
        role = self.role_for("F13")
        verifier_prompt = next((root / "phase1/roles" / role).glob("*/attempt_*/prompt.txt")).read_text()
        self.assertTrue(verifier_prompt.startswith(
            "You are the independent I4R post-fan-out verifier. " + NEW_SENTENCE))
        # Its replay is recorded on the route as well.
        replay = self.run_verifier(root, ["F13"])
        self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)
        self.assertEqual(len(self.verifier_starts()), 1)
        verifier_replays = [row for row in self.verifier_rows(root) if row.get("attempt_kind") == "cache_replay"]
        self.assertEqual(len(verifier_replays), 1)
        self.assert_on_5_5(verifier_replays[0])

    def run_served(self, name: str, served: list[str]) -> tuple[pathlib.Path, subprocess.CompletedProcess[str]]:
        root = self.make_audit(name)
        result = self.run_phase1(root, extra_env={"I4R_TEST_SERVED": json.dumps({PHASE1_SERVED: served})})
        return root, result

    def test_a_misrouted_or_mixed_call_stops_at_the_probe_and_a_haiku_helper_does_not(self) -> None:
        for name, served in (("misrouted", ["claude-sonnet-5"]),
                             ("mixed", ["claude-sonnet-5-5", "claude-sonnet-5"])):
            with self.subTest(served=served):
                root, result = self.run_served(name, served)
                self.assertEqual(result.returncode, 4, result.stderr + result.stdout)
                self.assertIn(f"expected exactly one served Sonnet 5.5 primary; observed {served!r}", result.stderr)
                self.assertEqual([row["role"] for row in self.starts_in(root)], ["phase1_capability_probe"],
                                 "no auditor is dispatched after a misrouted probe")
                probe = self.provider_rows(root)
                self.assertEqual(len(probe), 1)
                self.assert_on_5_5(probe[0])
                self.assertEqual((probe[0]["status"], probe[0]["error_class"]), ("failed", "model"))
                status = json.loads((root / "phase1/status.json").read_text())
                self.assertEqual(status["pause_reason"], "capability_probe_failed")
        root, result = self.run_served("helper", ["claude-sonnet-5-5", "claude-haiku-4-5"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = self.provider_rows(root)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row["observed_model"] == "claude-sonnet-5-5"
                            and row["observed_model_ids"] == ["claude-sonnet-5-5", "claude-haiku-4-5"]
                            for row in rows))


class Sonnet5AuditIsUnchanged(RouteFixture):
    """Negative controls on the legacy route: the same served ids are judged by the 2.6.10 check."""

    ROUTE = SONNET_5
    SERVED: dict[str, list[str]] = {}

    def test_a_legacy_audit_spawns_the_alias_and_keeps_the_loose_check(self) -> None:
        root = self.make_audit("legacy")
        result = self.run_phase1(root)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual({model_of(row["argv"]) for row in self.starts_in(root)}, {"sonnet"})
        self.assertEqual({(row["requested_model"], row["reasoning_effort"]) for row in self.provider_rows(root)},
                         {("sonnet-5", "max")})
        prompt = next((root / "phase1/roles/auditor_reproduction").glob("*/attempt_*/prompt.txt")).read_text()
        self.assertTrue(prompt.startswith("You are an independent I4R Phase-1 auditor. " + LEGACY_SENTENCE))
        # A Sonnet 5.5 id is still a Sonnet 5 primary on the legacy route, exactly as in 2.6.10...
        loose = self.make_audit("loose")
        result = self.run_phase1(loose, extra_env={"I4R_TEST_SERVED": json.dumps({PHASE1_SERVED: ["claude-sonnet-5-5"]})})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        # ...and two Sonnet ids are two primaries, refused with the 2.6.10 message.
        mixed = self.make_audit("mixed")
        served = [PHASE1_SERVED, "claude-sonnet-5-5"]
        result = self.run_phase1(mixed, extra_env={"I4R_TEST_SERVED": json.dumps({PHASE1_SERVED: served})})
        self.assertEqual(result.returncode, 4, result.stderr + result.stdout)
        self.assertIn(f"expected exactly one served Sonnet 5 primary; observed {served!r}", result.stderr)


# ------------------------------------------------------------------ the coordinator on Sonnet 5.5
def coordinator_fixture() -> type:
    """test_run_coordinator's fixture, resolved at use: importing that suite here would import
    run_coordinator ahead of its place in discovery, binding it to another phase1_runtime."""
    return _module("test_run_coordinator").CoordinatorRunnerTests


class Sonnet55Coordinator(unittest.TestCase):
    """test_run_coordinator's own fixture, re-frozen on a route and served through the wrapper.

    Borrowed, not inherited: inheriting would re-run every coordinator test under this class.
    """

    def setUp(self) -> None:
        coordinator_fixture().setUp(self)
        self.spawns = pathlib.Path(self.temporary.name) / "spawns.jsonl"
        self.fake = write_wrapper(pathlib.Path(self.temporary.name) / "claude-wrapper",
                                  TESTS / "fake_claude_coordinator.py")

    def tearDown(self) -> None:
        coordinator_fixture().tearDown(self)

    def run_runner(self, *args, **kwargs) -> subprocess.CompletedProcess:
        return coordinator_fixture().run_runner(self, *args, **kwargs)

    def ledger(self) -> list[dict]:
        return coordinator_fixture().ledger(self)

    def attempt(self) -> pathlib.Path:
        return coordinator_fixture().attempt(self)

    def reroute(self, route: dict[str, str], *, coordinator: dict[str, str] | None = None,
                observed: dict[str, str] | None = None) -> None:
        run = json.loads((self.root / "run.json").read_text())
        run["toolkit"]["pipeline_policy_snapshot_sha256"] = freeze(
            self.root, route_policy(route, coordinator=coordinator))
        (self.root / "run.json").write_text(json.dumps(run) + "\n")
        served = claude_routes.model_id(route["requested_model"])
        rows = self.ledger()
        for row in rows:
            model = (observed or {}).get(row["role"], served)
            row.update(requested_model=route["requested_model"], reasoning_effort=route["reasoning_effort"],
                       observed_model=model,
                       observed_model_ids=[model, "claude-haiku-4-5"])
        (self.root / "invocations.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))

    def run_served(self, served: list[str] | None, extra: dict[str, str] | None = None):
        env = {"I4R_TEST_SERVED": json.dumps({COORDINATOR_SERVED: served} if served else {}),
               "I4R_TEST_SPAWN_LOG": str(self.spawns), **(extra or {})}
        with mock.patch.dict(os.environ, env):
            return self.run_runner()

    def test_the_coordinator_runs_and_records_on_its_5_5_route(self) -> None:
        self.reroute(SONNET_5_5)
        result = self.run_served(["claude-sonnet-5-5"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        row = self.ledger()[-1]
        self.assertEqual((row["role"], row["requested_model"], row["reasoning_effort"], row["observed_model"]),
                         ("coordinator", "sonnet-5-5", "max", "claude-sonnet-5-5"))
        self.assertEqual(row["observed_model_ids"], ["claude-sonnet-5-5", "claude-haiku-4-5"])
        attempt = self.attempt()
        args = json.loads((attempt / "output.partial/fake_args.json").read_text())["argv"]
        self.assertEqual(model_of(args), "claude-sonnet-5-5")
        # 2.7.3: an audit frozen on 2.7.0 to 2.7.2 (Sonnet 5.5 at max) is still DISPATCHED at max, not at the shipped
        # xhigh; its ledger row above says max, and the argv must agree (2.7.3 review, mutant M31)
        self.assertEqual(args[args.index("--effort") + 1], "max")
        prompt = (attempt / "prompt.txt").read_text()
        invocation_id = attempt.name.split("_", 2)[2]
        self.assertEqual(prompt, _module("run_coordinator").build_prompt(
            "audit-test-coordinator", invocation_id, attempt.parent.name, "sonnet-5-5"))
        self.assertIn(NEW_SENTENCE, prompt)
        self.assertIn("`claude-sonnet-5-5`", prompt)
        self.assertNotIn(LEGACY_SENTENCE, prompt)

    def test_a_mixed_or_misrouted_coordinator_call_is_a_model_failure_on_5_5(self) -> None:
        self.reroute(SONNET_5_5)
        for served, count in ((["claude-sonnet-5-5", "claude-sonnet-5"], 2), (["claude-sonnet-5"], 0)):
            with self.subTest(served=served):
                result = self.run_served(served)
                self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
                row = self.ledger()[-1]
                self.assertEqual((row["status"], row["error_class"], row["requested_model"]),
                                 ("failed", "model", "sonnet-5-5"))
                self.assertIn(f"expected exactly one served Sonnet 5.5 primary; observed {count}", row["error"])
        self.assertFalse((self.root / "report/REPORT_v1.md").exists())

    def test_the_legacy_coordinator_keeps_the_loose_check_and_its_message(self) -> None:
        self.reroute(SONNET_5)
        result = self.run_served(["claude-sonnet-5", "claude-sonnet-5-5"])
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        row = self.ledger()[-1]
        self.assertEqual((row["requested_model"], row["error_class"]), ("sonnet-5", "model"))
        self.assertIn("expected exactly one served Sonnet 5 primary; observed 2: "
                      "['claude-sonnet-5', 'claude-sonnet-5-5', 'claude-haiku-4-5']", row["error"])
        result = self.run_served(["claude-sonnet-5-5"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        attempt = sorted((self.root / "coordinator/invocations").glob("*/*"))[-1]
        self.assertEqual(model_of(json.loads((attempt / "output.partial/fake_args.json").read_text())["argv"]),
                         "sonnet")
        self.assertIn(LEGACY_SENTENCE, (attempt / "prompt.txt").read_text())

    def test_the_phase1_bindings_judge_each_auditor_on_its_route(self) -> None:
        # Strict on 5.5: a completed auditor that was served Sonnet 5 does not bind.
        self.reroute(SONNET_5_5, observed={"auditor_data_integrity": "claude-sonnet-5"})
        result = self.run_served(["claude-sonnet-5-5"])
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertIn("auditor_data_integrity completed under the wrong model policy", result.stderr)
        self.assertFalse((self.root / "coordinator").exists())

    def test_the_legacy_bindings_stay_loose(self) -> None:
        # Negative control: the same served-id mismatch on the legacy route is what 2.6.10 accepted.
        self.reroute(SONNET_5, observed={"auditor_data_integrity": "claude-sonnet-5-5"})
        result = self.run_served(None)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_a_coordinator_route_that_is_not_packaged_is_refused_before_any_call(self) -> None:
        self.reroute(SONNET_5_5, coordinator={**SONNET_5_5, "spawn_alias": "sonnet"})
        result = self.run_served(["claude-sonnet-5-5"])
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertIn("audit-scoped coordinator policy is not a packaged Claude route", result.stderr)
        self.assertEqual(spawn_log(self.spawns), [])


# ------------------------------------------------------------------------ the spawn environment
class SpawnEnvironment(RouteFixture):
    """Every Claude CLI subprocess runs without the variables that remap the alias `sonnet`."""

    ROUTE = SONNET_5
    SERVED: dict[str, list[str]] = {}
    EXTRA_ENV = {**REMAPPING, **KEPT}

    def assert_scrubbed(self, entries: list[dict], minimum: int) -> None:
        self.assertGreaterEqual(len(entries), minimum)
        for entry in entries:
            with self.subTest(argv=entry["argv"][:3]):
                self.assertEqual(entry["env"], {"ANTHROPIC_DEFAULT_SONNET_MODEL": None, "ANTHROPIC_MODEL": None,
                                                "I4R_TEST_ENV_KEPT": "kept"})

    def test_the_wrapper_sees_the_variables_when_nothing_scrubs_them(self) -> None:
        # Negative control: the logging wrapper does record them when it is spawned directly.
        result = subprocess.run([str(self.fake), "--version"], text=True, capture_output=True, check=False,
                                env=self.env("success", "auditor_reproduction"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(spawn_log(self.spawns)[-1]["env"], {**REMAPPING, **KEPT})

    def test_phase1_and_the_verifier_spawn_without_them(self) -> None:
        root = self.audit_after_phase1()
        phase1 = spawn_log(self.spawns)
        # The version check, the probe and the five auditors, all on the legacy alias.
        self.assertTrue(any(entry["argv"] == ["--version"] for entry in phase1))
        self.assertEqual({model_of(entry["argv"]) for entry in phase1 if "--model" in entry["argv"]}, {"sonnet"})
        self.assert_scrubbed(phase1, 7)
        result = self.run_verifier(root, ["F13"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        verifier = spawn_log(self.spawns)[len(phase1):]
        self.assertTrue(any("--model" in entry["argv"] for entry in verifier))
        self.assert_scrubbed(verifier, 2)

    def test_the_coordinator_spawns_without_them(self) -> None:
        fixture = Sonnet55Coordinator("test_the_coordinator_runs_and_records_on_its_5_5_route")
        fixture.setUp()
        try:
            fixture.reroute(SONNET_5)
            result = fixture.run_served(None, {**REMAPPING, **KEPT})
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            entries = spawn_log(fixture.spawns)
            self.assertEqual([model_of(entry["argv"]) for entry in entries], ["sonnet"])
            self.assert_scrubbed(entries, 1)
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
