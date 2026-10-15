"""2.7.3: new audits run every Claude role at effort xhigh; every existing audit keeps max, byte for byte.

2.7.3 lowers a NEW audit's Claude effort one degree, from max to xhigh, for the coordinator, the five Phase-1
auditors, their capability probe and the post-fan-out verifier. The effort is part of the Claude route an audit's
pipeline-policy.snapshot.json froze, so an audit initialised on 2.7.0 to 2.7.2 keeps Sonnet 5.5 at max and one
initialised earlier keeps Sonnet 5 at max. Every class below builds its condition on disk and runs the real code
(the runners as subprocesses, with the fake CLIs, never a provider):

* ``ShippedRoute`` / ``NewAuditInit`` -- the route module and the packaged policy ship xhigh, and a new audit
  initialised by the real init_run.sh freezes it in all eight Claude entries and prints it. A leftover snapshot on
  Sonnet 5.5 at max (an older skill's) or on a route no runner dispatches is refused, untouched.
* ``Phase1AtXhigh`` / ``CoordinatorAtXhigh`` -- the probe, the five auditors, the verifier and the coordinator are
  dispatched with ``--effort xhigh``, each prompt says "at xhigh effort", and the records say xhigh; a verifier
  record and a quarantined coordinator attempt are re-admitted against their own (xhigh) prompts.
* ``LegacyRoutesAreThe272Bytes`` -- for a Sonnet 5.5/max and a Sonnet 5/max snapshot the prompts, role packets,
  role input hashes, probe keys and verifier packet hashes are the bytes the 2.7.2 tree renders; Phase 1, a verifier
  call and a coordinator call made by 2.7.2 on a 2.7.2 audit replay on this tree with no provider call, and 2.7.2
  refuses a new (xhigh) audit before any call. The 2.7.2 tree is ``I4R_BASE_272``, or ``../base`` beside this tree
  as a release scratch copy lays it out; these tests skip without one (and fail if I4R_BASE_272 names another tree).
  Its modules only ever run in a subprocess with bytecode writing off.
* ``RoutesNoRunnerDispatches`` -- Sonnet 5.5 at high and Sonnet 5 at xhigh are refused before any provider call by
  run_phase1.py, run_coordinator.py and run_verifier.py, each naming the live routes; mixed routes stay refused.
* ``StrandRecoveryEffort`` -- --recover-stranded's fallback prefers the snapshot route's effort.
* ``DriftPinsXhigh`` -- config/check_policy_drift.py pins xhigh, joins it to claude_routes, and rejects each of the
  three new planted controls by the pins it names, and reverts on disk.

The routes are literals here, not claude_routes names, so the module loads on a tree without the new route and
each test fails there on its own.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shlex
import shutil
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
import test_264_verifier_runtime as verifier_tests  # noqa: E402  (the Phase-1 audit fixture)
import test_270_claude_routes as route_tests  # noqa: E402  (route_policy, minimal_audit, RouteFixture, wrapper)

SONNET_5 = {"requested_model": "sonnet-5", "spawn_alias": "sonnet", "provider": "anthropic", "reasoning_effort": "max"}
SONNET_5_5_MAX = {"requested_model": "sonnet-5-5", "spawn_alias": "claude-sonnet-5-5", "provider": "anthropic",
                  "reasoning_effort": "max"}
XHIGH = {**SONNET_5_5_MAX, "reasoning_effort": "xhigh"}
HIGH_5_5 = {**SONNET_5_5_MAX, "reasoning_effort": "high"}
XHIGH_5 = {**SONNET_5, "reasoning_effort": "xhigh"}
XHIGH_SENTENCE = "Use Claude Sonnet 5.5 at xhigh effort."
MAX_SENTENCE = "Use Claude Sonnet 5.5 at max effort."
AUDITORS = tuple(importlib.import_module("phase1_runtime").ROLE_SPECS)
PHASE1_ROLES = ("phase1_capability_probe", *AUDITORS)
INIT = SCRIPTS / "init_run.sh"
SNAPSHOT = "pipeline-policy.snapshot.json"
LIVE_DESCRIPTIONS = (
    "Sonnet 5 (sonnet-5, spawned as --model sonnet) / max",
    "Sonnet 5.5 (sonnet-5-5, spawned as --model claude-sonnet-5-5) / max",
    "Sonnet 5.5 (sonnet-5-5, spawned as --model claude-sonnet-5-5) / xhigh",
)


def _module(name: str):
    """Resolve a scripts module at use: several suites swap sys.modules entries during discovery."""
    return importlib.import_module(name)


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def effort_of(argv: list[str]) -> str:
    return argv[argv.index("--effort") + 1]


def model_of(argv: list[str]) -> str:
    return argv[argv.index("--model") + 1]


def shipped_policy() -> dict:
    return json.loads((SKILL / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))


def claude_entries(policy: dict) -> dict[str, dict]:
    """The eight Claude entries of a policy: the coordinator, five auditors, the verifier and the capability probe."""
    models = policy["models"]
    entries = {"coordinator": models["coordinator"], "post_fan_out_verifier": models["post_fan_out_verifier"],
               "capability_probe": policy["phase_1"]["capability_probe"]}
    entries.update(models["phase_1_auditors"])
    return entries


def policy_at(route: dict[str, str], *, verifier: dict[str, str] | None = None) -> dict:
    """The shipped policy with every Claude entry, the capability probe's leaves included, on ``route``."""
    policy = route_tests.route_policy(route, verifier=verifier)
    policy["phase_1"]["capability_probe"].update(requested_model=route["requested_model"],
                                                 reasoning_effort=route["reasoning_effort"])
    return policy


def envelope_bytes(policy: dict) -> bytes:
    """A snapshot exactly as record_invocation.initialize_policy_snapshot writes one."""
    envelope = {"snapshot_version": "1", "policy_sha256": _module("record_invocation").policy_hash(policy),
                "policy": policy}
    return (json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def clean_env(**extra: str) -> dict[str, str]:
    drop = {"I4R_ALLOW_SKILL_VERSION_MISMATCH", "I4R_CANONICAL_SKILL_DIR", "AUDITOR_MODEL", "AUDITOR_FAMILY",
            "AUDITOR_PROVIDER", "SKILL_DIR", "PROTOCOL_VERSION", "GATE_SET_VERSION", "I4R_MAX_ATTEMPTS"}
    env = {key: value for key, value in os.environ.items() if key not in drop}
    # capture_environment.sh (run after a successful init) would otherwise look for a real Codex CLI
    env.update(LC_ALL="C", PYTHONDONTWRITEBYTECODE="1", CODEX="/nonexistent/codex", **extra)
    return env


def init(audit: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(INIT), str(audit), "--audit-id", "t-273", *args],
                          text=True, capture_output=True, timeout=300, env=clean_env())


def workspace(root: pathlib.Path) -> pathlib.Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "f.csv").write_text("x\n", encoding="utf-8")
    return root


def base_272() -> pathlib.Path | None:
    """The 2.7.2 tree: I4R_BASE_272, or ../base beside this tree as a release scratch copy lays it out."""
    named = os.environ.get("I4R_BASE_272")
    candidate = pathlib.Path(named) if named else SKILL.parent / "base"
    try:
        version = (candidate / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = None
    if version == "2.7.2":
        return candidate.resolve()
    if named:
        raise AssertionError(f"I4R_BASE_272={named} is not a 2.7.2 tree (VERSION {version!r})")
    return None


NO_BASE = "needs the 2.7.2 tree: set I4R_BASE_272 (or keep one at ../base)"


# --------------------------------------------------------------------------------- what ships
class ShippedRoute(unittest.TestCase):
    def test_the_route_module_ships_xhigh_and_keeps_both_max_routes_live(self) -> None:
        self.assertEqual(getattr(claude_routes, "SONNET_5_5_XHIGH", None), XHIGH)
        self.assertEqual(claude_routes.LIVE_ROUTES, (SONNET_5, SONNET_5_5_MAX, XHIGH), "oldest first")
        self.assertEqual(claude_routes.SHIPPED, XHIGH)
        self.assertEqual(claude_routes.SONNET_5_5, SONNET_5_5_MAX, "the 2.7.0-2.7.2 route stays live")
        self.assertEqual(tuple(claude_routes.describe(route) for route in claude_routes.LIVE_ROUTES),
                         LIVE_DESCRIPTIONS)
        # The prompt effort: a packet's own, and max for a pre-2.7.0 packet or one naming none.
        self.assertEqual(claude_routes.effort_of({"model": dict(XHIGH)}), "xhigh")
        self.assertEqual(claude_routes.effort_of({"model": dict(SONNET_5_5_MAX)}), "max")
        for packet in ({}, {"model": {}}, {"model": None}, None, {"model": {"reasoning_effort": ""}}):
            with self.subTest(packet=packet):
                self.assertEqual(claude_routes.effort_of(packet), "max")
        # The served-model checks key on the model alone: an xhigh route is judged as Sonnet 5.5.
        self.assertTrue(claude_routes.serves(XHIGH["requested_model"], "claude-sonnet-5-5"))
        self.assertFalse(claude_routes.serves(XHIGH["requested_model"], "claude-sonnet-5"))

    def test_the_packaged_policy_freezes_xhigh_for_every_claude_entry_and_nothing_else(self) -> None:
        policy = shipped_policy()
        entries = claude_entries(policy)
        self.assertEqual(len(entries), 8)
        for name, entry in entries.items():
            with self.subTest(entry=name):
                self.assertEqual((entry["requested_model"], entry["reasoning_effort"]), ("sonnet-5-5", "xhigh"))
                if name != "capability_probe":
                    self.assertEqual(claude_routes.project(entry), XHIGH)
        # The GPT side is unchanged: critics and the critique model probe at high, the adjudicator at max.
        self.assertEqual(policy["models"]["critics"]["reasoning_effort"], "high")
        self.assertEqual(policy["critique"]["model_probe"]["reasoning_effort"], "high")
        self.assertEqual(policy["models"]["targeted_adjudicator"]["reasoning_effort"], "max")
        base = base_272()
        if base is not None:
            # Against the 2.7.2 policy: exactly the eight effort leaves moved, from max to xhigh.
            old = json.loads((base / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))
            for entry in claude_entries(old).values():
                self.assertEqual(entry["reasoning_effort"], "max")
                entry["reasoning_effort"] = "xhigh"
            # later releases' own leaves (2.7.4: the critique ceiling and the claim-change rule; 2.7.5: the
            # critic-route lock) are not 2.7.3's
            policy["critique"].pop("provider_timeout_seconds", None)
            policy["critique"]["later_rounds"]["routing_manifest"].pop("claim_change_rule", None)
            policy["critique"].pop("critic_route_lock", None)
            self.assertEqual(old, policy)


class NewAuditInit(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-273-init-")
        self.root = pathlib.Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_new_audit_freezes_xhigh_in_all_eight_claude_entries_and_prints_it(self) -> None:
        audit = workspace(self.root / "Journal" / "Paper" / "audit")
        result = init(audit, "--doi", "10.1234/abc")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        policy = _module("record_invocation").load_policy_snapshot(audit)
        for name, entry in claude_entries(policy).items():
            with self.subTest(entry=name):
                self.assertEqual(entry["reasoning_effort"], "xhigh")
        self.assertIn("\n  auditor model:     sonnet-5-5 (claude / anthropic), effort xhigh\n", result.stdout)
        run = json.loads((audit / "run.json").read_text())
        self.assertEqual(run["coordinator_model"]["effort"], "xhigh")
        self.assertEqual(run["toolkit"]["pipeline_policy_snapshot_sha256"], sha((audit / SNAPSHOT).read_bytes()))

    def plant(self, name: str, policy: dict) -> tuple[pathlib.Path, bytes]:
        audit = workspace(self.root / name / "audit")
        data = envelope_bytes(policy)
        (audit / SNAPSHOT).write_bytes(data)
        return audit, data

    def assert_refused_untouched(self, audit: pathlib.Path, data: bytes, result) -> None:
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"remove it (rm {audit / SNAPSHOT})", result.stderr)
        self.assertFalse((audit / "run.json").exists())
        self.assertEqual((audit / SNAPSHOT).read_bytes(), data, "the leftover was rewritten")

    def test_a_leftover_on_a_route_no_runner_dispatches_is_refused_and_says_so(self) -> None:
        for route in (HIGH_5_5, XHIGH_5):
            with self.subTest(route=route):
                audit, data = self.plant(f"{route['requested_model']}-{route['reasoning_effort']}", policy_at(route))
                result = init(audit)
                self.assert_refused_untouched(audit, data, result)
                self.assertIn(f"coordinator is {claude_routes.describe(route)} there, {LIVE_DESCRIPTIONS[2]} in",
                              result.stderr)
                self.assertIn("No runner dispatches that route (the live routes: " + "; ".join(LIVE_DESCRIPTIONS) + ")",
                              result.stderr)
                self.assertNotIn("older skill", result.stderr)

    def test_a_272_leftover_at_max_is_refused_as_an_older_skills(self) -> None:
        # What 2.7.2's record_invocation.py --init-policy left behind: the 2.7.2 policy itself when the tree is at
        # hand, else the shipped policy at max. It was the shipped route there; here it is a live route that is not.
        base = base_272()
        policy = (json.loads((base / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))
                  if base is not None else policy_at(SONNET_5_5_MAX))
        audit, data = self.plant("left-by-272", policy)
        result = init(audit)
        self.assert_refused_untouched(audit, data, result)
        self.assertIn(f"coordinator is {LIVE_DESCRIPTIONS[1]} there, {LIVE_DESCRIPTIONS[2]} in", result.stderr)
        self.assertIn("It may have been frozen by an older skill.", result.stderr)
        # Negative control: a leftover of the shipped policy itself is reused.
        audit, data = self.plant("left-by-273", shipped_policy())
        result = init(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((audit / SNAPSHOT).read_bytes(), data)


# ------------------------------------------------------------------------------ a new audit's calls
class Phase1AtXhigh(route_tests.RouteFixture):
    """The Phase-1 fixture frozen on the shipped route (the probe leaves at xhigh too), through the logging wrapper."""

    ROUTE = XHIGH

    def make_audit(self, name: str = "audit", *, metadata21: bool = True,
                   audit_id: str | None = None, policy: dict | None = None) -> pathlib.Path:
        return super().make_audit(name, metadata21=metadata21, audit_id=audit_id, policy=policy or policy_at(XHIGH))

    def starts_in(self, root: pathlib.Path) -> list[dict]:
        return [row for row in self.starts() if row["cwd"].startswith(str(root) + os.sep)]

    def reservations(self, root: pathlib.Path) -> list[dict]:
        return [event["payload"] for event in verifier_tests.lifecycle_journal.load_events(root)
                if event["type"] == "invocation_reserved"]

    def only(self, paths) -> pathlib.Path:
        paths = sorted(paths)
        self.assertEqual(len(paths), 1, paths)
        return paths[0]

    def test_the_probe_and_the_five_auditors_dispatch_xhigh_and_say_so(self) -> None:
        root = self.audit_after_phase1()
        starts = self.starts_in(root)
        self.assertEqual(sorted(row["role"] for row in starts), sorted(PHASE1_ROLES))
        for start in starts:
            with self.subTest(spawn=start["role"]):
                self.assertEqual((model_of(start["argv"]), effort_of(start["argv"])), ("claude-sonnet-5-5", "xhigh"))
        rows = self.provider_rows(root)
        self.assertEqual(sorted(row["role"] for row in rows), sorted(PHASE1_ROLES))
        for row in rows:
            self.assertEqual((row["status"], row["requested_model"], row["reasoning_effort"], row["observed_model"]),
                             ("completed", "sonnet-5-5", "xhigh", "claude-sonnet-5-5"), row["role"])
        self.assertEqual({(row["requested_model"], row["reasoning_effort"]) for row in self.reservations(root)},
                         {("sonnet-5-5", "xhigh")})
        for role in AUDITORS:
            with self.subTest(role=role):
                packet = json.loads(self.only((root / "phase1/role_packets").glob(f"*/{role}/*/ROLE_PACKET.json"))
                                    .read_text())
                self.assertEqual(packet["model"], {"requested_model": "sonnet-5-5", "spawn_alias": "claude-sonnet-5-5",
                                                   "reasoning_effort": "xhigh"})
                prompt = self.only((root / "phase1/roles" / role).glob("*/attempt_*/prompt.txt")).read_text()
                self.assertTrue(prompt.startswith("You are an independent I4R Phase-1 auditor. " + XHIGH_SENTENCE),
                                prompt[:120])
                self.assertNotIn("at max effort", prompt)
                # The prompt differs from a max packet's by the effort word alone.
                self.assertEqual(prompt, _module("run_phase1").build_role_prompt(
                    role, json.loads((root / "run.json").read_text())["audit_id"],
                    {**packet, "model": {**packet["model"], "reasoning_effort": "max"}}
                ).replace(MAX_SENTENCE, XHIGH_SENTENCE, 1))
        cache = json.loads(self.only((root / "phase1/probe_cache").glob("*.json")).read_text())
        self.assertEqual((cache["key_components"]["model"], cache["key_components"]["effort"]), ("sonnet-5-5", "xhigh"))
        # Negative control: the same audit keyed on the max route is another probe, so nothing replays across efforts.
        run_phase1 = _module("run_phase1")
        bound = run_phase1.probe_bound(_module("record_invocation").load_policy_snapshot(root))
        cli = cache["key_components"]["cli_version"]
        self.assertEqual(run_phase1.probe_key(cli, XHIGH, root, bound=bound)[0], cache["probe_key"])
        self.assertNotEqual(run_phase1.probe_key(cli, SONNET_5_5_MAX, root, bound=bound)[0], cache["probe_key"])
        # A re-run replays the probe and every auditor, on the same route.
        again = self.run_phase1(root)
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertEqual(len(self.starts_in(root)), 6, "a replay makes no provider call")
        replays = [row for row in self.ledger(root) if row.get("attempt_kind") == "cache_replay"]
        self.assertEqual(sorted(row["role"] for row in replays), sorted(PHASE1_ROLES))
        self.assertEqual({(row["requested_model"], row["reasoning_effort"]) for row in replays}, {("sonnet-5-5", "xhigh")})

    def test_the_verifier_dispatches_xhigh_says_so_and_its_record_readmits(self) -> None:
        root = self.audit_after_phase1()
        result = self.run_verifier(root, ["F13"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        [start] = self.verifier_starts()
        self.assertEqual((model_of(start["argv"]), effort_of(start["argv"])), ("claude-sonnet-5-5", "xhigh"))
        [row] = [row for row in self.verifier_rows(root) if row.get("provider_call")]
        self.assertEqual((row["status"], row["requested_model"], row["reasoning_effort"]),
                         ("completed", "sonnet-5-5", "xhigh"))
        prompt = self.only((root / "phase1/roles" / row["role"]).glob("*/attempt_*/prompt.txt")).read_text()
        self.assertTrue(prompt.startswith("You are the independent I4R post-fan-out verifier. " + XHIGH_SENTENCE))
        self.assertNotIn("at max effort", prompt)
        # Re-admission rebuilds the prompt from the frozen packet, effort included, and re-hashes it.
        run_verifier = _module("run_verifier")
        _path, receipt = self.receipt(root, row["role"])
        run_verifier.readmit_record(root, row, receipt)
        verdicts, errors = self.verdicts(root)
        self.assertEqual(errors, [])
        self.assertEqual([verdict for _id, verdict in verdicts[self.claim_hash("F13")]], ["CONFIRMED"])
        # Negative control: a rebuild that named max instead would not hash to the recorded packet.
        with mock.patch.object(run_verifier.claude_routes, "effort_of", lambda _packet: "max"):
            with self.assertRaisesRegex(_module("verifier_contract").VerifierContractError,
                                        "do not hash to the receipt's packet_sha256"):
                run_verifier.readmit_record(root, row, receipt)
        # Its replay is recorded at xhigh too.
        replay = self.run_verifier(root, ["F13"])
        self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)
        self.assertEqual(len(self.verifier_starts()), 1)
        self.assertEqual([(r["requested_model"], r["reasoning_effort"]) for r in self.verifier_rows(root)
                          if r.get("attempt_kind") == "cache_replay"], [("sonnet-5-5", "xhigh")])


class CoordinatorAtXhigh(unittest.TestCase):
    """test_run_coordinator's fixture follows the frozen snapshot, the shipped policy's by default: a new audit."""

    def fixture(self):
        fixture = _module("test_run_coordinator").CoordinatorRunnerTests(
            methodName="test_success_is_content_bound_promotes_and_records_all_models_and_usage")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def test_the_coordinator_dispatches_xhigh_and_its_prompt_names_it(self) -> None:
        fixture = self.fixture()
        self.assertEqual(fixture.route, XHIGH, "a new audit's coordinator route")
        self.assertEqual({(row["requested_model"], row["reasoning_effort"]) for row in fixture.ledger()},
                         {("sonnet-5-5", "xhigh")}, "the fixture's Phase-1 rows follow the snapshot")
        result = fixture.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        row = fixture.ledger()[-1]
        self.assertEqual((row["role"], row["status"], row["requested_model"], row["reasoning_effort"]),
                         ("coordinator", "completed", "sonnet-5-5", "xhigh"))
        attempt = fixture.attempt()
        argv = json.loads((attempt / "output.partial/fake_args.json").read_text())["argv"]
        self.assertEqual((model_of(argv), effort_of(argv)), ("claude-sonnet-5-5", "xhigh"))
        prompt = (attempt / "prompt.txt").read_text()
        self.assertTrue(prompt.startswith("You are the I4R coordinator. " + XHIGH_SENTENCE), prompt[:80])
        run_coordinator = _module("run_coordinator")
        args = ["audit-test-coordinator", attempt.name.split("_", 2)[2], attempt.parent.name]
        self.assertEqual(prompt, run_coordinator.build_prompt(*args, "sonnet-5-5", "xhigh"))
        # Negative control: the default effort is max, the 2.7.2 prompt, which differs by the effort word alone.
        self.assertEqual(run_coordinator.build_prompt(*args, "sonnet-5-5"),
                         prompt.replace(XHIGH_SENTENCE, MAX_SENTENCE, 1))
        self.assertEqual(run_coordinator.build_prompt(*args, "sonnet-5-5", "max"),
                         run_coordinator.build_prompt(*args, "sonnet-5-5"))

    def test_a_quarantined_xhigh_attempt_recovers_against_its_own_prompt(self) -> None:
        # test_recover_coordinator's quarantined attempt (a real run_coordinator.py call, recoverable but for its
        # elapsed time) on the shipped route: recover_coordinator.py rebuilds the prompt with the record's effort.
        recovery = _module("test_recover_coordinator").CoordinatorRecoveryTests(
            methodName="test_elapsed_only_recovery_promotes_records_status_receipt_and_freezes")
        recovery.setUp()
        self.addCleanup(recovery.doCleanups)
        record = json.loads((recovery.attempt / "invocation_record.json").read_text())
        self.assertEqual((record["requested_model"], record["reasoning_effort"]), ("sonnet-5-5", "xhigh"))
        self.assertIn(XHIGH_SENTENCE, (recovery.attempt / "prompt.txt").read_text())
        result = recovery.run_recovery()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual([(row["status"], row["reasoning_effort"]) for row in recovery.coordinator_rows()],
                         [("completed", "xhigh")])


# ------------------------------------------------------------------------ existing audits vs 2.7.2
# The renderer, run against any skill tree in a fresh process: prompts, packets, role input hashes and probe keys.
RENDERER = r'''
import hashlib, json, pathlib, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1] + "/scripts")
import phase1_runtime as runtime, run_coordinator, run_phase1, run_verifier
spec = json.load(sys.stdin)
h = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()
out = {}
for label, route in spec["routes"].items():
    for role in runtime.ROLE_SPECS:
        packet = runtime.make_role_packet(role, spec["manifest"], {**route, "area": runtime.ROLE_SPECS[role]["area"]})
        prompt = run_phase1.build_role_prompt(role, spec["audit_id"], packet)
        out[f"{label}:packet:{role}"] = runtime.sha256_bytes(runtime.canonical_bytes(packet))
        out[f"{label}:prompt:{role}"] = h(prompt)
        for cli in spec["clis"]:
            for bound in spec["bounds"]:
                key, _ = run_phase1.probe_key(cli, route, pathlib.Path(spec["root"]), bound=bound)
                portable = run_phase1.portable_probe_key(cli, route, bound=bound)
                out[f"{label}:probe_key:{cli}:{bound}"] = key
                out[f"{label}:portable_key:{cli}:{bound}"] = portable
                out[f"{label}:input:{role}:{cli}:{bound}"] = runtime.role_input_hash(packet, prompt, portable)
                out[f"{label}:input-path-bound:{role}:{cli}:{bound}"] = runtime.role_input_hash(packet, prompt, key)
    verifier = dict(spec["verifier_packet"], model={k: route[k] for k in ("requested_model", "spawn_alias", "reasoning_effort")})
    prompt = run_verifier.build_verifier_prompt(spec["verifier_role"], spec["audit_id"], verifier)
    out[f"{label}:verifier"] = h(prompt)
    out[f"{label}:verifier-pre265"] = h(run_verifier.pre_265_prompt(prompt))
    out[f"{label}:verifier-packet"] = run_verifier.packet_contract_hash(verifier, prompt)
for role in runtime.ROLE_SPECS:
    out[f"nomodel:prompt:{role}"] = h(run_phase1.build_role_prompt(role, spec["audit_id"], {"source_snapshot_sha256": "0" * 64}))
nomodel = run_verifier.build_verifier_prompt(spec["verifier_role"], spec["audit_id"], spec["verifier_packet"])
out["nomodel:verifier"] = h(nomodel)
out["nomodel:verifier-packet"] = run_verifier.packet_contract_hash(spec["verifier_packet"], nomodel)
for requested in (None, "sonnet-5", "sonnet-5-5"):
    out[f"coordinator:{requested}"] = h(run_coordinator.build_prompt("audit-test", "coordinator-0", "0" * 64, requested))
out["coordinator:3-args"] = h(run_coordinator.build_prompt("audit-test", "coordinator-0", "0" * 64))
print(json.dumps(out, sort_keys=True))
'''


def tree_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(extra or {})}


def render(tree: pathlib.Path, spec: dict) -> dict[str, str]:
    result = subprocess.run([sys.executable, "-B", "-c", RENDERER, str(tree)], input=json.dumps(spec), text=True,
                            capture_output=True, check=False, cwd=str(tree / "scripts"), env=tree_env())
    if result.returncode != 0:
        raise AssertionError(f"the renderer failed on {tree}: {result.stderr}")
    return json.loads(result.stdout)


def render_spec(routes: dict[str, dict[str, str]]) -> dict:
    names = ["archive_licence_metadata.json", "code_entrypoints.json", "environment_fingerprint.json",
             "file_inventory.json", "pdf_text.txt", "privacy_scan.json", "workbook_summaries.json"]
    return {
        "routes": routes, "audit_id": "20260930-audit-00000000", "root": "/i4r/pin/audit",
        "clis": ["2.1.206 (Claude Code)", "2.1.285 (Claude Code)"], "bounds": [150000, 350000],
        "verifier_role": "verifier_0123456789ab",
        "verifier_packet": {"source_snapshot_sha256": "a" * 64, "hypotheses_sha256": "b" * 64,
                            "hypotheses": [{"hypothesis_id": "H1", "claim_sha256": "c" * 64},
                                           {"hypothesis_id": "H2", "claim_sha256": "d" * 64}]},
        "manifest": {
            "snapshot_sha256": "1" * 64,
            "source_files": [{"path": "paper/paper.txt", "sha256": "2" * 64, "bytes": 10}],
            "common_evidence": {"artifacts": [{"path": name, "sha256": f"{index + 4:x}" * 64, "bytes": 100 + index}
                                              for index, name in enumerate(names)]},
        },
    }


def base_policy_at(base: pathlib.Path, route: dict[str, str]) -> dict:
    """The 2.7.2 policy (Sonnet 5.5 at max) itself, or with every Claude entry on the legacy Sonnet 5 route and the
    probe bound, timeout and critic route every audit initialised before 2.7.0 froze."""
    policy = json.loads((base / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))
    if route == SONNET_5:
        for entry in claude_entries(policy).values():
            entry.update({key: route[key] for key in entry if key in route})
        runtime = policy["phase_1"]["runtime"]
        runtime.update(probe_max_normalized_tokens=150000, timeout_seconds=2700)
        policy["phase_1"]["capability_probe"]["maximum_normalized_tokens"] = 150000
        policy["phase_1"]["provider_timeout_seconds"] = 2700
        for entry in (policy["models"]["critics"], policy["models"]["targeted_adjudicator"],
                      policy["critique"]["model_probe"]):
            entry["requested_model"] = "gpt-5.6-terra"
    return policy


class LegacyRoutesAreThe272Bytes(route_tests.RouteFixture):
    """Audits frozen as 2.7.2 froze them (Sonnet 5.5 at max) and as every earlier audit did (Sonnet 5 at max)."""

    def setUp(self) -> None:
        self.base_tree = base_272()
        if self.base_tree is None:
            self.skipTest(NO_BASE)
        super().setUp()

    def starts_in(self, root: pathlib.Path) -> list[dict]:
        # by directory: "made-by-272-sonnet-5" is a prefix of "made-by-272-sonnet-5-5"
        return [row for row in self.starts() if row["cwd"].startswith(str(root) + os.sep)]

    def run_tree(self, tree: pathlib.Path, script: str, root: pathlib.Path, *args: str,
                 scenario: str = "success", target: str = "auditor_reproduction") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(tree / "scripts" / script), str(root), *args, "--claude-bin", str(self.fake)],
            text=True, capture_output=True, env=self.env(scenario, target, {"PYTHONDONTWRITEBYTECODE": "1"}),
            timeout=180, check=False)

    def test_prompts_packets_input_hashes_and_probe_keys_are_the_272_bytes(self) -> None:
        spec = render_spec({"sonnet-5": SONNET_5, "sonnet-5-5-max": SONNET_5_5_MAX})
        old, new = render(self.base_tree, spec), render(SKILL, spec)
        # per route: 5 roles x (packet, prompt, 2 CLIs x 2 bounds x 2 input hashes) + 2 x 2 x 2 probe keys + 3 verifier
        # values; then 5 model-less auditor prompts, 2 model-less verifier values and 4 coordinator prompts
        self.assertEqual(len(old), 2 * (5 * (2 + 8) + 8 + 3) + 5 + 2 + 4, "every rendered value")
        self.assertEqual(new, old)
        # Negative control: this tree renders an xhigh route with the effort word changed and its identities re-keyed.
        spec = render_spec({"xhigh": XHIGH, "sonnet-5-5-max": SONNET_5_5_MAX})
        rendered = render(SKILL, spec)
        run_phase1, runtime = _module("run_phase1"), _module("phase1_runtime")
        for role in AUDITORS:
            with self.subTest(role=role):
                packet = runtime.make_role_packet(role, spec["manifest"], {**XHIGH, "area": 1})
                prompt = run_phase1.build_role_prompt(role, spec["audit_id"], packet)
                self.assertIn(XHIGH_SENTENCE, prompt)
                self.assertEqual(rendered[f"xhigh:prompt:{role}"], sha(prompt))
                self.assertNotEqual(rendered[f"xhigh:prompt:{role}"], rendered[f"sonnet-5-5-max:prompt:{role}"])
                self.assertNotEqual(rendered[f"xhigh:input:{role}:2.1.206 (Claude Code):350000"],
                                    rendered[f"sonnet-5-5-max:input:{role}:2.1.206 (Claude Code):350000"])
        self.assertNotEqual(rendered["xhigh:probe_key:2.1.206 (Claude Code):350000"],
                            rendered["sonnet-5-5-max:probe_key:2.1.206 (Claude Code):350000"])
        self.assertNotEqual(rendered["xhigh:verifier"], rendered["sonnet-5-5-max:verifier"])

    def test_a_272_phase1_and_verifier_call_replay_here_and_272_refuses_a_new_audit(self) -> None:
        for route in (SONNET_5_5_MAX, SONNET_5):
            with self.subTest(route=route["requested_model"]):
                root = self.make_audit(f"made-by-272-{route['requested_model']}",
                                       policy=base_policy_at(self.base_tree, route))
                done = self.run_tree(self.base_tree, "run_phase1.py", root, scenario="slow_parallel")
                self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
                self.write_registry(root)
                verified = self.run_tree(self.base_tree, "run_verifier.py", root, "--findings", "F13", target="verifier")
                self.assertEqual(verified.returncode, 0, verified.stderr + verified.stdout)
                starts = self.starts_in(root)
                self.assertEqual(len(starts), 7, "2.7.2 made the probe, five auditor and one verifier calls")
                self.assertEqual({effort_of(row["argv"]) for row in starts}, {"max"})
                # This tree replays every one of them: the probe key and each role input hash are unchanged.
                recorded = len(self.ledger(root))
                again = self.run_phase1(root)
                self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
                self.assertEqual(len(self.starts_in(root)), 7, "no Phase-1 call is re-dispatched")
                replays = self.ledger(root)[recorded:]
                self.assertEqual(sorted(row["role"] for row in replays), sorted(PHASE1_ROLES))
                self.assertTrue(all(row.get("attempt_kind") == "cache_replay" for row in replays), replays)
                self.assertEqual({(row["requested_model"], row["reasoning_effort"]) for row in replays},
                                 {(route["requested_model"], "max")})
                # The 2.7.2 verifier record re-admits here, and replays from its receipt.
                [row] = [r for r in self.verifier_rows(root) if r.get("provider_call")]
                _path, receipt = self.receipt(root, row["role"])
                _module("run_verifier").readmit_record(root, row, receipt)
                verdicts, errors = self.verdicts(root)
                self.assertEqual(errors, [])
                replay = self.run_verifier(root, ["F13"])
                self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)
                self.assertEqual(json.loads(replay.stdout)["status"], "replayed")
                self.assertEqual(len(self.starts_in(root)), 7, "the verifier call is not re-dispatched")
        # And back: 2.7.2 replays what this tree recorded on a 2.7.2 audit.
        root = self.make_audit("made-here", policy=base_policy_at(self.base_tree, SONNET_5_5_MAX))
        done = self.run_phase1(root, "slow_parallel")
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertEqual({effort_of(row["argv"]) for row in self.starts_in(root)}, {"max"})
        again = self.run_tree(self.base_tree, "run_phase1.py", root)
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertEqual(len(self.starts_in(root)), 6, "2.7.2 re-dispatches nothing this tree recorded")
        # Negative control: 2.7.2 refuses a new (xhigh) audit before any provider call; this tree admits it.
        for tree, code in ((self.base_tree, 1), (SKILL, 3)):
            with self.subTest(tree=str(tree)):
                new = route_tests.minimal_audit(self.base, f"new-{code}", policy_at(XHIGH))
                probe = self.run_tree(tree, "run_phase1.py", new, "--probe-only")
                self.assertEqual(probe.returncode, code, probe.stderr + probe.stdout)
                if code == 1:
                    self.assertIn("is not a packaged Claude route", probe.stderr)
                else:
                    self.assertIn("PAUSED: Phase-1 local preflight failed", probe.stderr)
                self.assertEqual(self.starts_in(new), [])

    def test_a_272_coordinator_call_is_reused_here_with_the_same_input_and_prompt(self) -> None:
        policy_file = self.base / "policy-272.json"
        policy_file.write_bytes((self.base_tree / "config/pipeline-policy-v2.json").read_bytes())
        coordinator_tests = _module("test_run_coordinator")
        fixture = coordinator_tests.CoordinatorRunnerTests(
            methodName="test_success_is_content_bound_promotes_and_records_all_models_and_usage")
        with mock.patch.object(coordinator_tests.record_invocation, "POLICY", policy_file):
            fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.assertEqual(fixture.route, SONNET_5_5_MAX, "a 2.7.2 audit")

        def run(tree: pathlib.Path) -> subprocess.CompletedProcess[str]:
            env = tree_env({"FAKE_COORDINATOR_SCENARIO": "success"})
            return subprocess.run([
                sys.executable, "-B", str(tree / "scripts/run_coordinator.py"), str(fixture.root),
                "--safe-evidence-manifest", str(fixture.safe_manifest), "--claude-bin", str(fixture.fake),
                "--timeout-min", "1"], text=True, capture_output=True, env=env, check=False)

        made = run(self.base_tree)
        self.assertEqual(made.returncode, 0, made.stderr + made.stdout)
        attempt = fixture.attempt()
        argv = json.loads((attempt / "output.partial/fake_args.json").read_text())["argv"]
        self.assertEqual((model_of(argv), effort_of(argv)), ("claude-sonnet-5-5", "max"))
        rows = len(fixture.ledger())
        reused = run(SKILL)
        self.assertEqual(reused.returncode, 0, reused.stderr + reused.stdout)
        summary = json.loads(reused.stdout)
        # The same input hash: the 2.7.2 completion binds it, so this tree calls nothing.
        self.assertEqual((summary["status"], summary["reused"], summary["invocation_id"]),
                         ("completed", True, attempt.name.split("_", 2)[2]))
        self.assertEqual(len(fixture.ledger()), rows)
        # And the prompt 2.7.2 wrote is the one this tree builds for the route (recover_coordinator compares it).
        args = ["audit-test-coordinator", attempt.name.split("_", 2)[2], attempt.parent.name]
        coordinator = _module("run_coordinator")
        prompt = (attempt / "prompt.txt").read_text()
        self.assertEqual(prompt, coordinator.build_prompt(*args, "sonnet-5-5", "max"))
        self.assertEqual(prompt, coordinator.build_prompt(*args, "sonnet-5-5"))
        self.assertIn(MAX_SENTENCE, prompt)


# ----------------------------------------------------------------------------- refused routes
class RoutesNoRunnerDispatches(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-273-refused-")
        self.base = pathlib.Path(self.temporary.name).resolve()
        self.fake = self.base / "fake-claude-phase1"
        shutil.copyfile(TESTS / "fake_claude_phase1.py", self.fake)
        self.fake.chmod(0o755)
        self.state = self.base / "fake-state.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_script(self, script: str, root: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "FAKE_PHASE1_STATE": str(self.state), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(SCRIPTS / script), str(root), *args, "--claude-bin", str(self.fake)],
                              text=True, capture_output=True, env=env, timeout=120, check=False)

    def assert_lists_the_live_routes(self, text: str) -> None:
        self.assertIn("; ".join(LIVE_DESCRIPTIONS), text)

    def test_phase1_and_the_coordinator_refuse_before_any_call(self) -> None:
        for route in (HIGH_5_5, XHIGH_5):
            with self.subTest(route=route):
                label = f"{route['requested_model']}-{route['reasoning_effort']}"
                root = route_tests.minimal_audit(self.base, f"phase1-{label}", policy_at(route))
                result = self.run_script("run_phase1.py", root, "--probe-only")
                self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
                self.assertRegex(result.stderr, r"auditor_[a-z_]+ is not a packaged Claude route: " + re.escape(str(route)))
                self.assert_lists_the_live_routes(result.stderr)
                coordinator = self.run_script("run_coordinator.py", root, "--safe-evidence-manifest",
                                              str(root / "missing.json"))
                self.assertEqual(coordinator.returncode, 1, coordinator.stderr + coordinator.stdout)
                self.assertIn("audit-scoped coordinator policy is not a packaged Claude route", coordinator.stderr)
                self.assert_lists_the_live_routes(coordinator.stderr)
                self.assertFalse((root / "coordinator").exists(), "nothing is dispatched or reserved")
        # Mixed: four auditors at xhigh and one at max share no route.
        policy = policy_at(XHIGH)
        policy["models"]["phase_1_auditors"]["auditor_code_methods"].update(SONNET_5_5_MAX)
        root = route_tests.minimal_audit(self.base, "phase1-mixed", policy)
        result = self.run_script("run_phase1.py", root, "--probe-only")
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertIn("the five Phase-1 auditors must share one Claude route", result.stderr)
        self.assertIn("auditor_code_methods: " + LIVE_DESCRIPTIONS[1], result.stderr)
        self.assertFalse(self.state.exists(), "no Claude call was started")

    def plan(self, root: pathlib.Path) -> str:
        run_verifier = _module("run_verifier")
        args = argparse.Namespace(timeout_seconds=None, max_budget_usd="15")
        with self.assertRaises(run_verifier.VerifierRefusal) as caught:
            run_verifier.plan_batch(root, root / "phase1", ["F13"], args)
        self.assertTrue(caught.exception.remedy.strip())
        return caught.exception.reason + " " + caught.exception.remedy

    def test_the_verifier_refuses_before_any_call(self) -> None:
        for name, verifier, fragment in (
            ("high", HIGH_5_5, "is not a packaged Claude route"),
            ("sonnet-5-xhigh", XHIGH_5, "is not a packaged Claude route"),
            # the verifier runs on the auditors' own route and reuses their probe: xhigh auditors, max verifier
            ("max-beside-xhigh", SONNET_5_5_MAX, "is not the Phase-1 auditors' route"),
        ):
            with self.subTest(verifier=name):
                root = route_tests.minimal_audit(self.base, f"verifier-{name}", policy_at(XHIGH, verifier=verifier),
                                                 metadata21=True)
                (root / "phase1").mkdir()
                reason = self.plan(root)
                self.assertIn(fragment, reason)
                if fragment == "is not a packaged Claude route":
                    self.assert_lists_the_live_routes(reason)
                result = self.run_script("run_verifier.py", root, "--findings", "F13")
                self.assertEqual(result.returncode, 5, result.stderr + result.stdout)
                self.assertIn(fragment, json.loads(result.stdout)["reason"])
        # Negative control: the shipped route passes both pins and stops at the missing quorum.
        root = route_tests.minimal_audit(self.base, "verifier-xhigh", policy_at(XHIGH), metadata21=True)
        (root / "phase1").mkdir()
        self.assertIn("quorum.json is missing", self.plan(root))
        self.assertFalse(self.state.exists(), "no Claude call was started")


class StrandRecoveryEffort(unittest.TestCase):
    """--recover-stranded records a reservation without an effort at the snapshot route's, else max (2.7.3)."""

    def test_the_fallback_is_the_snapshot_routes_live_effort(self) -> None:
        run_phase1 = _module("run_phase1")
        with tempfile.TemporaryDirectory(prefix="i4r-273-strand-") as directory:
            base = pathlib.Path(directory)
            roles = (("auditor_reproduction", "auditor"), ("phase1_capability_probe", "other"),
                     ("verifier_0123456789ab", "verifier"), ("coordinator", "coordinator"))
            for route in (XHIGH, SONNET_5_5_MAX, SONNET_5):
                label = f"{route['requested_model']}-{route['reasoning_effort']}"
                root = route_tests.minimal_audit(base, label, policy_at(route))
                for role, role_class in roles:
                    with self.subTest(route=label, role=role):
                        self.assertEqual(run_phase1.snapshot_route_effort(root, role, role_class),
                                         route["reasoning_effort"])
            # A route no runner dispatches, an unknown role or an unreadable snapshot says nothing (the caller keeps max).
            root = route_tests.minimal_audit(base, "high", policy_at(HIGH_5_5))
            self.assertIsNone(run_phase1.snapshot_route_effort(root, "auditor_reproduction", "auditor"))
            root = route_tests.minimal_audit(base, "xhigh", policy_at(XHIGH))
            self.assertIsNone(run_phase1.snapshot_route_effort(root, "critic_stats", "critic"))
            (root / SNAPSHOT).unlink()
            self.assertIsNone(run_phase1.snapshot_route_effort(root, "auditor_reproduction", "auditor"))


# ------------------------------------------------------------------------------- the drift check
class DriftPinsXhigh(unittest.TestCase):
    LABEL = "Sonnet 5.5 / xhigh"

    def checker(self, path: pathlib.Path = SKILL / "config/check_policy_drift.py"):
        spec = importlib.util.spec_from_file_location(f"i4r_273_drift_{abs(hash(str(path)))}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_checker_pins_xhigh_and_joins_both_max_routes(self) -> None:
        module = self.checker()
        self.assertEqual(module.SHIPPED_CLAUDE_ROUTE, XHIGH)
        self.assertEqual(module.SHIPPED_CLAUDE_LABEL, self.LABEL)
        self.assertEqual(getattr(module, "SONNET_5_5_MAX_ROUTE", None), SONNET_5_5_MAX)
        self.assertEqual(module.LEGACY_CLAUDE_ROUTE, SONNET_5)
        errors: list[str] = []
        module.check_claude_routes(errors)
        self.assertEqual(errors, [])
        errors = []
        self.assertEqual(module.check_negative_controls(shipped_policy(), errors), 20)
        self.assertEqual(errors, [])

    def test_each_new_control_is_rejected_by_the_pins_it_names_and_only_those(self) -> None:
        module = self.checker()
        coordinator = f"coordinator policy is not {self.LABEL}"
        auditors = f"one or more phase-1 auditors is not assigned to {self.LABEL}"
        verifier = f"post-fan-out verifier policy is not {self.LABEL} on the Phase-1 auditor family"
        join = "post-fan-out verifier route differs from the Phase-1 auditors' route"
        probe = f"the Phase-1 capability probe is not {self.LABEL}"

        def everything(policy: dict) -> None:
            for entry in claude_entries(policy).values():
                entry["reasoning_effort"] = "max"

        def verifier_only(policy: dict) -> None:
            policy["models"]["post_fan_out_verifier"]["reasoning_effort"] = "max"

        def probe_only(policy: dict) -> None:
            policy["phase_1"]["capability_probe"]["reasoning_effort"] = "max"

        pins = (coordinator, auditors, verifier, join, probe)
        for label, mutate, expected in (("all eight at max", everything, {coordinator, auditors, verifier, probe}),
                                        ("the verifier at max", verifier_only, {verifier, join}),
                                        ("the probe at max", probe_only, {probe})):
            with self.subTest(control=label):
                policy = shipped_policy()
                mutate(policy)
                errors: list[str] = []
                module.check_policy_shape(policy, errors)
                fired = {pin for pin in pins if any(error.startswith(pin) for error in errors)}
                self.assertEqual(fired, expected, errors)
        errors = []
        module.check_policy_shape(shipped_policy(), errors)
        self.assertEqual(errors, [], "the shipped policy passes every pin")

    def copied_tree(self, base: pathlib.Path) -> pathlib.Path:
        tree = base / "tree"
        for part in ("config", "scripts", "schemas", "reference", "prompts"):
            shutil.copytree(SKILL / part, tree / part, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("SKILL.md", "INSTALL.md", "CHEATSHEET.html", "VERSION", "CHANGELOG.md"):
            if (SKILL / name).exists():
                shutil.copyfile(SKILL / name, tree / name)
        return tree

    def run_checker(self, tree: pathlib.Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-B", str(tree / "config/check_policy_drift.py")], text=True,
                              capture_output=True, timeout=180, check=False)

    def test_reverts_on_disk_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            tree = self.copied_tree(pathlib.Path(base))
            result = self.run_checker(tree)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("twenty planted drifts were rejected", result.stdout)
            policy_path = tree / "config/pipeline-policy-v2.json"
            original = policy_path.read_text(encoding="utf-8")
            policy_path.write_text(original.replace('"reasoning_effort": "xhigh"', '"reasoning_effort": "max"'),
                                   encoding="utf-8")
            result = self.run_checker(tree)
            self.assertNotEqual(result.returncode, 0)
            for pin in (f"coordinator policy is not {self.LABEL}", f"the Phase-1 capability probe is not {self.LABEL}",
                        f"one or more phase-1 auditors is not assigned to {self.LABEL}"):
                self.assertIn(pin, result.stdout)
            policy_path.write_text(original, encoding="utf-8")
            module = tree / "scripts/claude_routes.py"
            text = module.read_text(encoding="utf-8")
            module.write_text(text.replace("SHIPPED: dict[str, str] = SONNET_5_5_XHIGH", "SHIPPED: dict[str, str] = SONNET_5_5"),
                              encoding="utf-8")
            result = self.run_checker(tree)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("scripts/claude_routes.py SHIPPED is", result.stdout)
            self.assertIn("scripts/claude_routes.py SHIPPED is not the newest LIVE_ROUTES entry", result.stdout)
            module.write_text(text.replace("LIVE_ROUTES: tuple[dict[str, str], ...] = (SONNET_5, SONNET_5_5, SONNET_5_5_XHIGH)",
                                           "LIVE_ROUTES: tuple[dict[str, str], ...] = (SONNET_5, SONNET_5_5_XHIGH)"),
                              encoding="utf-8")
            result = self.run_checker(tree)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no longer holds the Sonnet 5.5 / max route that every audit initialised on 2.7.0 to 2.7.2 froze",
                          result.stdout)



# ------------------------------------------------------------------------- review gaps (2.7.3 mutation review)
def _effort_of_argv(argv: list[str]) -> str:
    return argv[argv.index("--effort") + 1]


class MaxRoutePhase1Dispatch(route_tests.RouteFixture):
    """An audit frozen on Sonnet 5.5 at max (2.7.0 to 2.7.2) is DISPATCHED at --effort max by this tree: the probe,
    the five auditors and the verifier. Needs no 2.7.2 tree, so it never skips (review, mutant M30)."""

    ROUTE = claude_routes.SONNET_5_5

    def test_probe_auditors_and_verifier_of_a_max_audit_are_spawned_at_max(self) -> None:
        root = self.audit_after_phase1()
        starts = self.starts_in(root)
        self.assertEqual(len(starts), 6)
        self.assertEqual({_effort_of_argv(row["argv"]) for row in starts}, {"max"})
        result = self.run_verifier(root, ["F13"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual([_effort_of_argv(row["argv"]) for row in self.verifier_starts()], ["max"])


def _policy_272() -> dict:
    """What 2.7.2's record_invocation.py --init-policy froze: the shipped policy with every Claude entry at max."""
    pinit = route_tests._module("test_270_policy_and_init")
    policy = pinit.shipped_policy()
    models = policy["models"]
    for entry in [models["coordinator"], models["post_fan_out_verifier"], *models["phase_1_auditors"].values()]:
        entry.update(claude_routes.SONNET_5_5)
    policy["phase_1"]["capability_probe"]["reasoning_effort"] = claude_routes.SONNET_5_5["reasoning_effort"]
    return policy


_PINIT = importlib.import_module("test_270_policy_and_init")


class PostFreezeEffortCheck(_PINIT.RaceFixture):
    """A 2.7.2 snapshot frozen by another process inside this init's freeze has the shipped requested_model, round
    target and cap: only init's post-freeze effort line can refuse it (review, mutant M16)."""

    def test_a_272_snapshot_frozen_inside_the_freeze_is_refused_by_the_effort_check(self) -> None:
        older = self.base / "older-272.snapshot.json"
        older.write_bytes(_PINIT.envelope_bytes(_policy_272()))
        audit = _PINIT.make_audit(self.root / "older-272" / "audit")
        hook = f"cp {shlex.quote(str(older))} {shlex.quote(str(audit / _PINIT.SNAPSHOT))}"
        result, log = self.race(audit, hook)
        self.assertIn("hook rc=0", log, log)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("the frozen policy snapshot is not the policy init checked", result.stderr)
        self.assertFalse((audit / "run.json").exists(), "an xhigh init bound a max snapshot")
        self.assertEqual((audit / _PINIT.SNAPSHOT).read_bytes(), older.read_bytes())


class ForceHintNamesTheEffort(unittest.TestCase):
    """An initialised 2.7.2 audit whose run.json is gone is told that --force would also move its effort (review,
    mutant M23)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-273-force-hint-")
        self.root = pathlib.Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_an_initialised_272_audit_whose_run_json_is_gone_is_told_force_moves_its_effort(self) -> None:
        audit = _PINIT.make_audit(self.root / "J" / "P" / "audit")
        (audit / _PINIT.SNAPSHOT).write_bytes(_PINIT.envelope_bytes(_policy_272()))
        (audit / "run.events.jsonl").write_text(json.dumps({"type": "audit_initialized"}) + "\n", encoding="utf-8")
        result = _PINIT.init(audit)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("This audit froze Claude effort max, so --force would move it to xhigh", result.stderr)


if __name__ == "__main__":
    unittest.main()
