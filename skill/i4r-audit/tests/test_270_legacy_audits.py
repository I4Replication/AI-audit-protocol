"""2.7.0: an audit initialised on 2.6.10 keeps Sonnet 5 for its whole life, byte for byte.

2.7.0 ships Sonnet 5.5, so every fixture that freezes the shipped policy is now a Sonnet 5.5 audit, and
the rest of the suite no longer exercises the route every audit under runs/ froze. Here an audit is
frozen the way 2.6.10 froze it -- record_invocation.initialize_policy_snapshot, unchanged, reading a
copy of the 2.6.10 policy -- and the real 2.7.0 runners are driven on it with the fake CLIs, never a
provider. Every assertion has a negative control, usually the same audit on a Sonnet 5.5 snapshot.

* ``LegacyAuditOn270`` -- the capability probe, the five Phase-1 auditors and the post-fan-out
  verifier: every spawn is ``--model sonnet``, every record and reservation says ``sonnet-5``, the
  loose pre-2.7.0 check admits ``claude-sonnet-5-20260801``, and the verifier's record re-admits
  through ``run_verifier.readmit_record``.
* ``LegacyCoordinatorOn270`` -- the coordinator on the same terms.
* ``LegacyAuditAgainst2610`` -- the snapshot is the one 2.6.10 froze; the rendered auditor, verifier
  and coordinator prompts are the bytes a pristine 2.6.10 tree renders for the same frozen packet;
  and the audit crosses versions both ways (Phase 1 and a verifier call made by 2.6.10 replay on
  2.7.0 with no provider call and re-admit, and 2.6.10 replays what 2.7.0 recorded).
* ``MixedRoutes`` -- a Sonnet 5 audit served claude-sonnet-5-5 is accepted exactly as 2.6.10 accepted
  it (the legacy looseness is intentional); a Sonnet 5.5 audit served claude-sonnet-5 is refused
  before anything is promoted.

The 2.6.10 tree is ``I4R_BASE_2610``, or ``../base`` beside this tree as a release scratch copy lays
it out; the tests that need it skip without one (and fail if I4R_BASE_2610 names something else).
Without it a Sonnet 5 snapshot is frozen from the shipped policy with every Claude role on Sonnet 5,
which ``test_the_legacy_snapshot_is_the_one_2610_froze`` proves is the 2.6.10 policy. The 2.6.10
modules only ever run in a subprocess with bytecode writing off, so the tree is never written.
"""
from __future__ import annotations

import contextlib
import importlib
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
import fake_claude_coordinator  # noqa: E402
import fake_claude_phase1  # noqa: E402
import test_264_verifier_runtime as verifier_tests  # noqa: E402  (its Phase-1 audit fixture)
import test_270_claude_routes as route_tests  # noqa: E402  (its logging, id-renaming CLI wrapper)

SONNET_5 = claude_routes.SONNET_5
SONNET_5_5 = claude_routes.SONNET_5_5
LEGACY_SENTENCE = "Use Claude Sonnet 5 at max effort."
NEW_SENTENCE = "Use Claude Sonnet 5.5 at max effort."
LEGACY_CRITIC_MODEL = "gpt-5.6-terra"  # the critic route every audit initialised on 2.6.1-2.6.10 froze
LEGACY_PROBE_BOUND = 150000  # the probe bound every audit initialised before 2.7.1 froze
# What the fakes serve each route (tests/fake_claude_phase1.py, tests/fake_claude_coordinator.py).
PHASE1_SERVED = fake_claude_phase1.SERVED_BY_MODEL
COORDINATOR_SERVED = {alias: ids[0] for alias, ids in fake_claude_coordinator.SERVED_BY_MODEL.items()}
PHASE1_LEGACY = fake_claude_phase1.LEGACY_SERVED           # claude-sonnet-5-20260801
COORDINATOR_LEGACY = COORDINATOR_SERVED["sonnet"]           # claude-sonnet-5
AUDITORS = tuple(importlib.import_module("phase1_runtime").ROLE_SPECS)
PHASE1_ROLES = ("phase1_capability_probe", *AUDITORS)
COORDINATOR_TEST = "test_success_is_content_bound_promotes_and_records_all_models_and_usage"


def _module(name: str):
    """Resolve a scripts module at use: several suites swap sys.modules entries during discovery."""
    return importlib.import_module(name)


def _base_2610() -> tuple[pathlib.Path | None, str | None]:
    named = os.environ.get("I4R_BASE_2610")
    tree = pathlib.Path(named) if named else SKILL.parent / "base"
    try:
        version = (tree / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = None
    if version == "2.6.10" and not (tree / "scripts/claude_routes.py").exists():
        return tree.resolve(), None
    problem = f"I4R_BASE_2610={named} is not a pristine 2.6.10 skill tree (VERSION {version!r})" if named else None
    return None, problem


BASE, BASE_PROBLEM = _base_2610()
NO_BASE = "needs a pristine 2.6.10 skill tree: set I4R_BASE_2610 (or keep one at ../base)"


def policy_on(route: dict[str, str], *, probe_bound: int | None = None) -> dict:
    """The shipped policy with every Claude role (and the capability probe) on ``route``. On the Sonnet 5
    route the critics, their probe and the adjudicator are on the Terra route that 2.6.10 shipped with it,
    and the probe bound is 2.6.10's 150,000, so the result is the 2.6.10 policy (2.7.0 also moved new
    audits' critic route, to GPT-6.1 Sol, and 2.7.1 their probe bound, to 350,000). ``probe_bound`` sets
    the bound on any route (150,000 on Sonnet 5.5 is an audit as 2.7.0 froze it)."""
    policy = json.loads((SKILL / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))
    models = policy["models"]
    for row in (models["coordinator"], *models["phase_1_auditors"].values(), models["post_fan_out_verifier"]):
        row.update(route)
    policy["phase_1"]["capability_probe"]["requested_model"] = route["requested_model"]
    # the probe runs at the auditors' effort (2.7.3 moved new audits to xhigh; every route before ran at max)
    policy["phase_1"]["capability_probe"]["reasoning_effort"] = route["reasoning_effort"]
    if route == SONNET_5:
        for row in (models["critics"], models["targeted_adjudicator"], policy["critique"]["model_probe"]):
            row["requested_model"] = LEGACY_CRITIC_MODEL
        # 2.7.1 raised new audits' probe bound; every audit initialised before froze 150,000.
        policy["phase_1"]["runtime"]["probe_max_normalized_tokens"] = LEGACY_PROBE_BOUND
        policy["phase_1"]["capability_probe"]["maximum_normalized_tokens"] = LEGACY_PROBE_BOUND
        # 2.7.2 raised new audits' Phase-1 timeout; every audit initialised before froze 2,700 s.
        policy["phase_1"]["runtime"]["timeout_seconds"] = 2700
        policy["phase_1"]["provider_timeout_seconds"] = 2700
        # 2.7.4 added the critique ceiling leaf; 2.6.10 froze none
        policy["critique"].pop("provider_timeout_seconds", None); policy["critique"]["later_rounds"]["routing_manifest"].pop("claim_change_rule", None); policy["critique"].pop("critic_route_lock", None)
    if probe_bound is not None:
        policy["phase_1"]["runtime"]["probe_max_normalized_tokens"] = probe_bound
        policy["phase_1"]["capability_probe"]["maximum_normalized_tokens"] = probe_bound
    if probe_bound == LEGACY_PROBE_BOUND:
        # an audit as 2.7.0 froze it: 2.7.2 raised new audits' Phase-1 timeout, and 2.7.0 froze 2,700 s
        policy["phase_1"]["runtime"]["timeout_seconds"] = 2700
        policy["phase_1"]["provider_timeout_seconds"] = 2700
        policy["critique"].pop("provider_timeout_seconds", None); policy["critique"]["later_rounds"]["routing_manifest"].pop("claim_change_rule", None); policy["critique"].pop("critic_route_lock", None)  # 2.7.0 froze no critique ceiling
    return policy


def policy_bytes(route: dict[str, str], *, probe_bound: int | None = None) -> bytes:
    """The policy file init reads for an audit on ``route``: for Sonnet 5 the 2.6.10 file itself when
    the 2.6.10 tree is at hand, else the shipped policy on the route."""
    if route == SONNET_5 and BASE is not None and probe_bound in (None, LEGACY_PROBE_BOUND):
        return (BASE / "config/pipeline-policy-v2.json").read_bytes()
    return (json.dumps(policy_on(route, probe_bound=probe_bound), indent=2, ensure_ascii=False) + "\n").encode("utf-8")


@contextlib.contextmanager
def freezing(route: dict[str, str], directory: pathlib.Path, *modules, probe_bound: int | None = None):
    """initialize_policy_snapshot, unchanged, reading a policy file on ``route`` (patched on every
    record_invocation module object the callers hold)."""
    path = directory / f"policy-{route['requested_model']}-{probe_bound}.json"
    path.write_bytes(policy_bytes(route, probe_bound=probe_bound))
    targets = {id(module): module for module in (_module("record_invocation"), *modules)}
    with contextlib.ExitStack() as stack:
        for module in targets.values():
            stack.enter_context(mock.patch.object(module, "POLICY", path))
        yield


def frozen_routes(root: pathlib.Path) -> set[tuple]:
    """Every Claude route the audit's snapshot froze (coordinator, auditors, verifier)."""
    models = _module("record_invocation").load_policy_snapshot(root)["models"]
    rows = [models["coordinator"], *models["phase_1_auditors"].values(), models["post_fan_out_verifier"]]
    return {tuple(claude_routes.project(row).items()) for row in rows}


def model_of(argv: list[str]) -> str:
    return argv[argv.index("--model") + 1]


def tree_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(extra or {})}


RENDER_2610 = r'''
import json, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1] + "/scripts")
import run_coordinator, run_phase1, run_verifier
builders = {"role": run_phase1.build_role_prompt, "verifier": run_verifier.build_verifier_prompt,
            "coordinator": run_coordinator.build_prompt}
print(json.dumps([builders[kind](*args) for kind, args in json.load(sys.stdin)]))
'''

FREEZE_2610 = r'''
import pathlib, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1] + "/scripts")
import record_invocation
record_invocation.initialize_policy_snapshot(pathlib.Path(sys.argv[2]))
'''


def run_2610(code: str, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-B", "-c", code, str(BASE), *args], input=stdin, text=True,
                          capture_output=True, check=False, cwd=str(BASE / "scripts"), env=tree_env())


def render_2610(calls: list[tuple[str, list]]) -> list[str]:
    """What the 2.6.10 prompt builders render for these (kind, args) calls, in a fresh process."""
    result = run_2610(RENDER_2610, stdin=json.dumps(calls))
    if result.returncode != 0:
        raise AssertionError(f"the 2.6.10 renderer failed: {result.stderr}")
    return json.loads(result.stdout)


def served_env(served: dict[str, list[str]]) -> dict[str, str]:
    """test_270_claude_routes' wrapper: rename each served id (keyed on the legacy ids)."""
    return {"I4R_TEST_SERVED": json.dumps(served)}


# ------------------------------------------------------------------------------ the Phase-1 fixture
class RouteAudit(verifier_tests.VerifierFixture):
    """test_264_verifier_runtime's Phase-1 audit, frozen on an explicit Claude route (default Sonnet 5)."""

    def setUp(self) -> None:
        super().setUp()
        self.route = SONNET_5
        self.probe_bound = None  # the policy's own bound (2.7.1: 150,000 on Sonnet 5, as every such audit froze)

    def make_audit(self, name: str = "audit", *, metadata21: bool = True,
                   audit_id: str | None = None) -> pathlib.Path:
        with freezing(self.route, self.base, probe_bound=self.probe_bound):
            root = super().make_audit(name, metadata21=metadata21, audit_id=audit_id)
        self.assertEqual(frozen_routes(root), {tuple(self.route.items())}, "the snapshot is on one route")
        return root

    def audit_on(self, route: dict[str, str], name: str, *, phase1: bool = True) -> pathlib.Path:
        """An audit frozen on ``route``; with ``phase1``, after the real runner reached quorum."""
        self.route = route
        try:
            return self.audit_after_phase1(name) if phase1 else self.make_audit(name)
        finally:
            self.route = SONNET_5

    def use_wrapper(self) -> None:
        """Spawn the fake through test_270_claude_routes' wrapper, which renames served ids."""
        self.fake = route_tests.write_wrapper(self.base / "claude-wrapper", TESTS / "fake_claude_phase1.py")

    # ------------------------------------------------------------------------------ reading
    def starts_in(self, root: pathlib.Path) -> list[dict]:
        return [row for row in self.starts() if row["cwd"].startswith(str(root) + os.sep)]

    def provider_rows(self, root: pathlib.Path) -> list[dict]:
        return [row for row in self.ledger(root) if row.get("provider_call")]

    def reservations(self, root: pathlib.Path) -> list[dict]:
        return [event["payload"] for event in verifier_tests.lifecycle_journal.load_events(root)
                if event["type"] == "invocation_reserved"]

    def only(self, paths) -> pathlib.Path:
        paths = sorted(paths)
        self.assertEqual(len(paths), 1, paths)
        return paths[0]

    def packet_of(self, root: pathlib.Path, role: str) -> dict:
        return json.loads(self.only((root / "phase1/role_packets").glob(f"*/{role}/*/ROLE_PACKET.json")).read_text())

    def prompt_of(self, root: pathlib.Path, role: str) -> bytes:
        return self.only((root / "phase1/roles" / role).glob("*/attempt_*/prompt.txt")).read_bytes()

    def audit_id(self, root: pathlib.Path) -> str:
        return json.loads((root / "run.json").read_text())["audit_id"]

    def verifier_row(self, root: pathlib.Path) -> dict:
        rows = [row for row in self.verifier_rows(root) if row.get("provider_call")]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def readmit(self, root: pathlib.Path, row: dict) -> None:
        _path, receipt = self.receipt(root, row["role"])
        _module("run_verifier").readmit_record(root, row, receipt)

    def run_tree(self, tree: pathlib.Path, script: str, root: pathlib.Path, *args: str,
                 scenario: str = "success", target: str = "auditor_reproduction",
                 extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """One of ``tree``'s runners (this one or 2.6.10's), exactly as the fixture runs its own."""
        return subprocess.run(
            [sys.executable, "-B", str(tree / "scripts" / script), str(root), *args, "--claude-bin", str(self.fake)],
            text=True, capture_output=True, env=self.env(scenario, target, {"PYTHONDONTWRITEBYTECODE": "1",
                                                                            **(extra_env or {})}),
            timeout=180, check=False,
        )

    def assert_phase1_on(self, root: pathlib.Path, route: dict[str, str], served: str) -> None:
        starts = self.starts_in(root)
        self.assertEqual(sorted(row["role"] for row in starts), sorted(PHASE1_ROLES), "one probe and five auditors")
        self.assertEqual({model_of(row["argv"]) for row in starts}, {route["spawn_alias"]})
        rows = self.provider_rows(root)
        self.assertEqual(sorted(row["role"] for row in rows), sorted(PHASE1_ROLES))
        for row in rows:
            self.assertEqual((row["status"], row["provider"], row["requested_model"], row["reasoning_effort"],
                              row["model_family"], row["observed_model"], row["served_model"]),
                             ("completed", "anthropic", route["requested_model"], "max", "claude", served, served),
                             row["role"])
        self.assertEqual({(row["requested_model"], row["reasoning_effort"]) for row in self.reservations(root)},
                         {(route["requested_model"], "max")})
        cache = json.loads(self.only((root / "phase1/probe_cache").glob("*.json")).read_text())
        self.assertEqual((cache["key_components"]["model"], cache["key_components"]["model_alias"],
                          cache["served_model"]), (route["requested_model"], route["spawn_alias"], served))
        sentence = NEW_SENTENCE if route == SONNET_5_5 else LEGACY_SENTENCE
        for role in AUDITORS:
            self.assertEqual(self.packet_of(root, role)["model"],
                             {key: route[key] for key in ("requested_model", "spawn_alias", "reasoning_effort")})
            prompt = self.prompt_of(root, role).decode("utf-8")
            self.assertTrue(prompt.startswith("You are an independent I4R Phase-1 auditor. " + sentence), role)


# --------------------------------------------------------------------------- the coordinator fixture
class CoordinatorAudits:
    """test_run_coordinator's own fixture, frozen on an explicit route, for any TestCase."""

    def coordinator_audit(self, route: dict[str, str]):
        module = _module("test_run_coordinator")
        fixture = module.CoordinatorRunnerTests(methodName=COORDINATOR_TEST)
        policies = tempfile.TemporaryDirectory(prefix="i4r-270-policy-")
        self.addCleanup(policies.cleanup)
        with freezing(route, pathlib.Path(policies.name), module.record_invocation):
            fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.assertEqual(frozen_routes(fixture.root), {tuple(route.items())})
        return fixture

    @staticmethod
    def run_coordinator(fixture, served: dict[str, list[str]] | None = None, *,
                        tree: pathlib.Path = SKILL) -> subprocess.CompletedProcess[str]:
        env = tree_env({"FAKE_COORDINATOR_SCENARIO": "success"})
        env.pop("I4R_TEST_SERVED", None)
        env.update(served_env(served) if served else {})
        return subprocess.run([
            sys.executable, "-B", str(tree / "scripts/run_coordinator.py"), str(fixture.root),
            "--safe-evidence-manifest", str(fixture.safe_manifest), "--claude-bin", str(fixture.fake),
            "--timeout-min", "1",
        ], text=True, capture_output=True, env=env, check=False)

    @staticmethod
    def wrap(fixture) -> None:
        fixture.fake = route_tests.write_wrapper(pathlib.Path(fixture.temporary.name) / "claude-wrapper",
                                                 TESTS / "fake_claude_coordinator.py")

    @staticmethod
    def coordinator_row(fixture) -> dict:
        rows = [row for row in fixture.ledger() if row["role"] == "coordinator"]
        return rows[-1]

    @staticmethod
    def coordinator_attempt(fixture) -> tuple[dict, pathlib.Path]:
        """The last coordinator row and its attempt directory (attempt_<nn>_<invocation id>)."""
        row = CoordinatorAudits.coordinator_row(fixture)
        [attempt] = (fixture.root / "coordinator/invocations" / row["input_artifact_sha256"]).glob(
            f"attempt_*_{row['invocation_id']}")
        return row, attempt

    @staticmethod
    def coordinator_prompt(fixture) -> tuple[bytes, list]:
        """The attempt's prompt.txt and the build_prompt arguments that must reproduce it."""
        row, attempt = CoordinatorAudits.coordinator_attempt(fixture)
        audit_id = json.loads((fixture.root / "run.json").read_text())["audit_id"]
        return (attempt / "prompt.txt").read_bytes(), [audit_id, row["invocation_id"], row["input_artifact_sha256"]]

    @staticmethod
    def spawned_model(fixture) -> str:
        _row, attempt = CoordinatorAudits.coordinator_attempt(fixture)
        return model_of(json.loads((attempt / "output.partial/fake_args.json").read_text())["argv"])


# ------------------------------------------------------------------ a legacy audit on this release
class LegacyAuditOn270(RouteAudit):
    def test_the_probe_and_the_five_auditors_spawn_sonnet_and_record_sonnet_5(self) -> None:
        legacy = self.audit_on(SONNET_5, "legacy")
        self.assert_phase1_on(legacy, SONNET_5, PHASE1_LEGACY)
        # Spelled out, not only derived: the 2.6.10 alias, request and served id.
        self.assertEqual({model_of(row["argv"]) for row in self.starts_in(legacy)}, {"sonnet"})
        self.assertEqual({row["requested_model"] for row in self.ledger(legacy)}, {"sonnet-5"})
        self.assertEqual({row["observed_model"] for row in self.provider_rows(legacy)}, {"claude-sonnet-5-20260801"})
        # A re-run replays the probe and every auditor from their receipts, on the same route.
        again = self.run_phase1(legacy)
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertEqual(len(self.starts_in(legacy)), 6, "a replay makes no provider call")
        replays = [row for row in self.ledger(legacy) if row.get("attempt_kind") == "cache_replay"]
        self.assertEqual(sorted(row["role"] for row in replays), sorted(PHASE1_ROLES))
        self.assertEqual({(row["requested_model"], row["reasoning_effort"]) for row in replays}, {("sonnet-5", "max")})

        # Negative control: the same audit on a Sonnet 5.5 snapshot spawns the full id and records 5.5.
        new = self.audit_on(SONNET_5_5, "new")
        self.assert_phase1_on(new, SONNET_5_5, "claude-sonnet-5-5")
        self.assertEqual({model_of(row["argv"]) for row in self.starts_in(new)}, {"claude-sonnet-5-5"})
        self.assertNotIn("sonnet-5", {row["requested_model"] for row in self.ledger(new)})

    def test_the_loose_legacy_check_admits_the_legacy_served_id(self) -> None:
        coordinator = _module("run_coordinator")
        result = {"modelUsage": {PHASE1_LEGACY: {}}}
        self.assertTrue(claude_routes.serves("sonnet-5", PHASE1_LEGACY))
        self.assertEqual(coordinator.observed_models(result, "sonnet-5"), (PHASE1_LEGACY, [PHASE1_LEGACY], 1))
        self.assertEqual(coordinator.observed_models(result), (PHASE1_LEGACY, [PHASE1_LEGACY], 1))
        # Negative control: the exact Sonnet 5.5 check does not.
        self.assertFalse(claude_routes.serves("sonnet-5-5", PHASE1_LEGACY))
        self.assertEqual(coordinator.observed_models(result, "sonnet-5-5")[2], 0)

        # On disk: a legacy audit served the legacy id completes (every row observed it)...
        legacy = self.audit_on(SONNET_5, "legacy", phase1=False)
        done = self.run_phase1(legacy)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertEqual({(row["status"], row["observed_model"]) for row in self.provider_rows(legacy)},
                         {("completed", PHASE1_LEGACY)})
        # ...and a Sonnet 5.5 audit served the same id stops at the probe, before any auditor.
        self.use_wrapper()
        new = self.audit_on(SONNET_5_5, "new", phase1=False)
        refused = self.run_phase1(new, extra_env=served_env({PHASE1_LEGACY: [PHASE1_LEGACY]}))
        self.assertEqual(refused.returncode, 4, refused.stderr + refused.stdout)
        self.assertIn(f"expected exactly one served Sonnet 5.5 primary; observed {[PHASE1_LEGACY]!r}", refused.stderr)
        self.assertEqual([row["role"] for row in self.starts_in(new)], ["phase1_capability_probe"])
        probe = self.provider_rows(new)
        self.assertEqual([(row["status"], row["error_class"], row["requested_model"]) for row in probe],
                         [("failed", "model", "sonnet-5-5")])

    def test_the_verifier_spawns_sonnet_records_sonnet_5_and_its_record_readmits(self) -> None:
        contract = _module("verifier_contract")
        legacy = self.audit_on(SONNET_5, "legacy")
        result = self.run_verifier(legacy, ["F13"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        starts = [row for row in self.verifier_starts() if row["cwd"].startswith(str(legacy) + os.sep)]
        self.assertEqual([model_of(row["argv"]) for row in starts], ["sonnet"])
        row = self.verifier_row(legacy)
        self.assertEqual((row["status"], row["requested_model"], row["reasoning_effort"], row["observed_model"]),
                         ("completed", "sonnet-5", "max", PHASE1_LEGACY))
        self.assertEqual([(event["role"], event["requested_model"]) for event in self.reservations(legacy)
                          if event["role_class"] == "verifier"], [(row["role"], "sonnet-5")])
        self.assertEqual(self.packet_of(legacy, row["role"])["model"],
                         {"requested_model": "sonnet-5", "spawn_alias": "sonnet", "reasoning_effort": "max"})
        self.assertTrue(self.prompt_of(legacy, row["role"]).decode().startswith(
            "You are the independent I4R post-fan-out verifier. " + LEGACY_SENTENCE))
        # Re-admission rebuilds the prompt from the frozen packet and re-hashes it.
        self.readmit(legacy, row)
        ledger = self.ledger(legacy)
        admitted = contract.verify_record(legacy, row, {r["invocation_id"]: r for r in ledger},
                                          contract.auditor_families(ledger))
        self.assertEqual(admitted, {self.claim_hash("F13"): "CONFIRMED"})
        verdicts, errors = self.verdicts(legacy)
        self.assertEqual(errors, [])
        # Negative control: had 2.7.0 renamed the model in a legacy packet's prompt, the record
        # published on 2.6.10 would no longer re-admit.
        rv = _module("run_verifier")
        with mock.patch.object(rv.claude_routes, "display_name", lambda _requested: "Claude Sonnet 5.5"):
            with self.assertRaisesRegex(contract.VerifierContractError,
                                        "do not hash to the receipt's packet_sha256"):
                self.readmit(legacy, row)
        self.readmit(legacy, row)
        # Its replay is recorded on the legacy route too.
        replay = self.run_verifier(legacy, ["F13"])
        self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)
        self.assertEqual(len([r for r in self.verifier_starts() if r["cwd"].startswith(str(legacy) + os.sep)]), 1)
        self.assertEqual([(r["requested_model"], r["reasoning_effort"]) for r in self.verifier_rows(legacy)
                          if r.get("attempt_kind") == "cache_replay"], [("sonnet-5", "max")])

        # Negative control: the same verifier call on a Sonnet 5.5 audit.
        new = self.audit_on(SONNET_5_5, "new")
        result = self.run_verifier(new, ["F13"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        starts = [r for r in self.verifier_starts() if r["cwd"].startswith(str(new) + os.sep)]
        self.assertEqual([model_of(r["argv"]) for r in starts], ["claude-sonnet-5-5"])
        row = self.verifier_row(new)
        self.assertEqual((row["requested_model"], row["observed_model"]), ("sonnet-5-5", "claude-sonnet-5-5"))
        self.assertTrue(self.prompt_of(new, row["role"]).decode().startswith(
            "You are the independent I4R post-fan-out verifier. " + NEW_SENTENCE))
        self.readmit(new, row)


class LegacyCoordinatorOn270(CoordinatorAudits, unittest.TestCase):
    def test_the_coordinator_spawns_sonnet_records_sonnet_5_and_admits_claude_sonnet_5(self) -> None:
        coordinator = _module("run_coordinator")
        legacy = self.coordinator_audit(SONNET_5)
        # The fixture's Phase-1 rows are what a 2.6.10 audit holds.
        self.assertEqual({(row["requested_model"], row["observed_model"]) for row in legacy.ledger()},
                         {("sonnet-5", COORDINATOR_LEGACY)})
        result = self.run_coordinator(legacy)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        row = self.coordinator_row(legacy)
        self.assertEqual((row["status"], row["requested_model"], row["reasoning_effort"], row["observed_model"]),
                         ("completed", "sonnet-5", "max", "claude-sonnet-5"))
        self.assertEqual(row["observed_model_ids"], ["claude-sonnet-5", fake_claude_coordinator.HELPER])
        self.assertEqual(self.spawned_model(legacy), "sonnet")
        prompt, args = self.coordinator_prompt(legacy)
        self.assertEqual(prompt, coordinator.build_prompt(*args, "sonnet-5").encode("utf-8"))
        self.assertEqual(prompt, coordinator.build_prompt(*args).encode("utf-8"), "the 2.6.10 call shape")
        self.assertIn(LEGACY_SENTENCE, prompt.decode())
        self.assertIn("`claude-sonnet-5`", prompt.decode())
        self.assertTrue((legacy.root / "report/REPORT_v1.md").is_file())

        # Negative control: the same fixture on a Sonnet 5.5 snapshot.
        new = self.coordinator_audit(SONNET_5_5)
        result = self.run_coordinator(new)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        row = self.coordinator_row(new)
        self.assertEqual((row["requested_model"], row["observed_model"]), ("sonnet-5-5", "claude-sonnet-5-5"))
        self.assertEqual(self.spawned_model(new), "claude-sonnet-5-5")
        prompt, args = self.coordinator_prompt(new)
        self.assertEqual(prompt, coordinator.build_prompt(*args, "sonnet-5-5").encode("utf-8"))
        self.assertNotEqual(prompt, coordinator.build_prompt(*args).encode("utf-8"))
        self.assertIn(NEW_SENTENCE, prompt.decode())
        self.assertNotIn(LEGACY_SENTENCE, prompt.decode())


# ----------------------------------------------------------------------- against the 2.6.10 tree
class LegacyAuditAgainst2610(CoordinatorAudits, RouteAudit):
    def setUp(self) -> None:
        if BASE_PROBLEM:
            self.fail(BASE_PROBLEM)
        if BASE is None:
            self.skipTest(NO_BASE)
        super().setUp()

    def role_calls(self, root: pathlib.Path, roles) -> tuple[list[tuple[str, list]], list[bytes]]:
        calls, prompts = [], []
        for role in roles:
            kind = "verifier" if role.startswith("verifier_") else "role"
            calls.append((kind, [role, self.audit_id(root), self.packet_of(root, role)]))
            prompts.append(self.prompt_of(root, role))
        return calls, prompts

    def test_the_legacy_snapshot_is_the_one_2610_froze(self) -> None:
        # The derivation used when no 2.6.10 tree is at hand is the 2.6.10 policy.
        self.assertEqual(policy_on(SONNET_5), json.loads((BASE / "config/pipeline-policy-v2.json").read_text()))
        legacy = self.audit_on(SONNET_5, "legacy", phase1=False)
        fresh = self.base / "frozen-by-2610"
        fresh.mkdir()
        result = run_2610(FREEZE_2610, str(fresh))
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = "pipeline-policy.snapshot.json"
        self.assertEqual((legacy / snapshot).read_bytes(), (fresh / snapshot).read_bytes())
        # Negative control: an audit frozen from the shipped policy is not a 2.6.10 audit.
        new = self.audit_on(SONNET_5_5, "new", phase1=False)
        self.assertNotEqual((new / snapshot).read_bytes(), (fresh / snapshot).read_bytes())

    def test_the_auditor_and_verifier_prompts_are_the_2610_bytes(self) -> None:
        for route, name in ((SONNET_5, "legacy"), (SONNET_5_5, "new")):
            root = self.audit_on(route, name)
            result = self.run_verifier(root, ["F13"])
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            calls, prompts = self.role_calls(root, [*AUDITORS, self.verifier_row(root)["role"]])
            rendered = [text.encode("utf-8") for text in render_2610(calls)]
            for (kind, args), prompt, old in zip(calls, prompts, rendered):
                with self.subTest(route=name, role=args[0]):
                    if route == SONNET_5:
                        self.assertEqual(prompt, old)
                    else:
                        # Negative control: 2.6.10 renders a Sonnet 5.5 packet as Sonnet 5, so the
                        # comparison sees the one sentence 2.7.0 changes, and nothing else.
                        self.assertNotEqual(prompt, old)
                        self.assertEqual(prompt, old.replace(LEGACY_SENTENCE.encode(), NEW_SENTENCE.encode(), 1))

    def test_the_coordinator_prompt_is_the_2610_bytes(self) -> None:
        legacy = self.coordinator_audit(SONNET_5)
        result = self.run_coordinator(legacy)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        prompt, args = self.coordinator_prompt(legacy)
        [old] = render_2610([("coordinator", args)])
        self.assertEqual(prompt, old.encode("utf-8"))
        # Negative control: on Sonnet 5.5 the model sentence and the model-id example differ.
        new = self.coordinator_audit(SONNET_5_5)
        result = self.run_coordinator(new)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        prompt, args = self.coordinator_prompt(new)
        [old] = render_2610([("coordinator", args)])
        self.assertNotEqual(prompt, old.encode("utf-8"))
        self.assertEqual(prompt.decode(), old.replace(LEGACY_SENTENCE, NEW_SENTENCE, 1)
                         .replace("`claude-sonnet-5`", "`claude-sonnet-5-5`", 1))

    def test_phase1_and_a_verifier_call_made_by_2610_continue_on_270(self) -> None:
        contract = _module("verifier_contract")
        root = self.audit_on(SONNET_5, "legacy", phase1=False)
        done = self.run_tree(BASE, "run_phase1.py", root, scenario="slow_parallel")
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.write_registry(root)
        verified = self.run_tree(BASE, "run_verifier.py", root, "--findings", "F13", target="verifier")
        self.assertEqual(verified.returncode, 0, verified.stderr + verified.stdout)
        starts = len(self.starts_in(root))
        self.assertEqual(starts, 7, "2.6.10 made the probe, five auditor and one verifier calls")
        self.assertEqual({model_of(row["argv"]) for row in self.starts_in(root)}, {"sonnet"})
        # 2.7.0 replays every one of them: the probe key and each role input hash are unchanged.
        recorded = len(self.ledger(root))
        again = self.run_phase1(root)
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertEqual(len(self.starts_in(root)), starts, "no Phase-1 call is re-dispatched")
        replays = self.ledger(root)[recorded:]
        self.assertTrue(all(row.get("attempt_kind") == "cache_replay" for row in replays), replays)
        self.assertEqual(sorted(row["role"] for row in replays), sorted(PHASE1_ROLES))
        self.assertEqual({row["requested_model"] for row in replays}, {"sonnet-5"})
        # The 2.6.10 verifier record re-admits on 2.7.0, and replays from its receipt.
        row = self.verifier_row(root)
        self.readmit(root, row)
        verdicts, errors = self.verdicts(root)
        self.assertEqual(errors, [])
        self.assertEqual([verdict for _id, verdict in verdicts[self.claim_hash("F13")]], ["CONFIRMED"])
        replay = self.run_verifier(root, ["F13"])
        self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)
        self.assertEqual(len(self.starts_in(root)), starts, "the verifier call is not re-dispatched")
        self.assertEqual([r["requested_model"] for r in self.verifier_rows(root)
                          if r.get("attempt_kind") == "cache_replay"], ["sonnet-5"])
        self.assertIsNone(contract.confirmation_problem(
            {r["id"]: r for r in map(json.loads, (root / "work/findings.jsonl").read_text().splitlines())}["F13"],
            row["invocation_id"], contract.required_roles(contract.load_snapshot_policy(root)), verdicts))

        # Negative control: 2.6.10 refuses a Sonnet 5.5 audit before any provider call, so the
        # replay above is a property of the legacy route. The audit is frozen as 2.7.0 froze one, with
        # the 150,000 probe bound, so the route is its only difference (2.6.10 refuses a 2.7.1 audit's
        # 350,000 bound first, at its runtime check).
        self.probe_bound = LEGACY_PROBE_BOUND
        try:
            new = self.audit_on(SONNET_5_5, "new", phase1=False)
        finally:
            self.probe_bound = None
        refused = self.run_tree(BASE, "run_phase1.py", new)
        self.assertNotEqual(refused.returncode, 0, refused.stderr + refused.stdout)
        self.assertIn("is not bound to Sonnet 5 / max", refused.stderr + refused.stdout)
        self.assertEqual(self.starts_in(new), [])

    def test_2610_replays_what_270_recorded_on_a_legacy_audit(self) -> None:
        root = self.audit_on(SONNET_5, "legacy")
        result = self.run_verifier(root, ["F13"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        starts = len(self.starts_in(root))
        again = self.run_tree(BASE, "run_phase1.py", root)
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        replay = self.run_tree(BASE, "run_verifier.py", root, "--findings", "F13", target="verifier")
        self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)
        self.assertEqual(len(self.starts_in(root)), starts, "2.6.10 re-dispatches nothing 2.7.0 recorded")
        self.assertEqual(json.loads(replay.stdout)["status"], "replayed")


# --------------------------------------------------------------------------------- mixed routes
class MixedRoutes(CoordinatorAudits, RouteAudit):
    STABLE = ("role", "status", "provider", "requested_model", "reasoning_effort", "model_family",
              "observed_model", "observed_model_ids", "served_model", "error_class", "attempt_kind")

    def projection(self, rows: list[dict]) -> list[tuple]:
        return sorted(tuple(json.dumps(row.get(key), sort_keys=True) for key in self.STABLE) for row in rows)

    def test_a_sonnet_5_audit_served_sonnet_5_5_is_accepted_as_2610_accepted_it(self) -> None:
        self.use_wrapper()
        served = served_env({PHASE1_LEGACY: ["claude-sonnet-5-5"]})
        trees = [("2.7.0", SKILL)] + ([("2.6.10", BASE)] if BASE is not None else [])
        outcomes = {}
        for label, tree in trees:
            with self.subTest(tree=label):
                root = self.audit_on(SONNET_5, f"legacy-{label}", phase1=False)
                phase1 = self.run_tree(tree, "run_phase1.py", root, scenario="slow_parallel", extra_env=served)
                self.assertEqual(phase1.returncode, 0, phase1.stderr + phase1.stdout)
                self.write_registry(root)
                verifier = self.run_tree(tree, "run_verifier.py", root, "--findings", "F13", target="verifier",
                                         extra_env=served)
                self.assertEqual(verifier.returncode, 0, verifier.stderr + verifier.stdout)
                self.assertEqual({model_of(row["argv"]) for row in self.starts_in(root)}, {"sonnet"})
                rows = self.provider_rows(root)
                self.assertEqual(len(rows), 7)
                self.assertEqual({(row["status"], row["requested_model"], row["observed_model"]) for row in rows},
                                 {("completed", "sonnet-5", "claude-sonnet-5-5")})
                self.readmit(root, self.verifier_row(root))
                outcomes[label] = (phase1.returncode, verifier.returncode, self.projection(rows))
        if BASE is not None:
            self.assertEqual(outcomes["2.7.0"], outcomes["2.6.10"])
        # The check that admits it is the 2.6.10 one (pinned against 2.6.10 in test_270_claude_routes).
        self.assertTrue(claude_routes.is_sonnet_5_legacy("claude-sonnet-5-5"))
        self.assertTrue(route_tests.ClaudeRoutesModule.LEGACY_2610["claude-sonnet-5-5"])

        coordinator_outcomes = {}
        for label, tree in trees:
            with self.subTest(coordinator=label):
                fixture = self.coordinator_audit(SONNET_5)
                self.wrap(fixture)
                result = self.run_coordinator(fixture, {COORDINATOR_LEGACY: ["claude-sonnet-5-5"]}, tree=tree)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                row = self.coordinator_row(fixture)
                self.assertEqual((row["status"], row["requested_model"], row["observed_model"]),
                                 ("completed", "sonnet-5", "claude-sonnet-5-5"))
                self.assertEqual(self.spawned_model(fixture), "sonnet")
                self.assertTrue((fixture.root / "report/REPORT_v1.md").is_file())
                coordinator_outcomes[label] = (result.returncode, self.projection([row]))
        if BASE is not None:
            self.assertEqual(coordinator_outcomes["2.7.0"], coordinator_outcomes["2.6.10"])

    def test_a_sonnet_5_5_audit_served_sonnet_5_is_refused_before_promotion(self) -> None:
        self.use_wrapper()
        misrouted = served_env({PHASE1_LEGACY: ["claude-sonnet-5"]})
        # Phase 1: the probe is refused, and no auditor is dispatched or promoted.
        root = self.audit_on(SONNET_5_5, "phase1", phase1=False)
        result = self.run_phase1(root, extra_env=misrouted)
        self.assertEqual(result.returncode, 4, result.stderr + result.stdout)
        self.assertIn("expected exactly one served Sonnet 5.5 primary; observed ['claude-sonnet-5']", result.stderr)
        self.assertEqual([row["role"] for row in self.starts_in(root)], ["phase1_capability_probe"])
        self.assertEqual([(row["status"], row["error_class"]) for row in self.provider_rows(root)],
                         [("failed", "model")])
        self.assertEqual(sorted((root / "report").glob("0*.md")), [])
        self.assertFalse((root / "phase1/quorum.json").exists())
        self.assertFalse((root / "phase1/completions").exists())

        # The verifier on an honest Sonnet 5.5 Phase 1: every sample is refused, nothing promoted.
        root = self.audit_on(SONNET_5_5, "verifier")
        role = self.role_for("F13")
        result = self.run_verifier(root, ["F13"], extra_env=misrouted)
        self.assertNotEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = [row for row in self.verifier_rows(root) if row.get("provider_call")]
        self.assertTrue(rows)
        self.assertEqual({(row["status"], row["error_class"], row["requested_model"], row["observed_model"])
                          for row in rows}, {("failed", "model", "sonnet-5-5", "claude-sonnet-5")})
        self.assertFalse((root / "work/verifier" / role).exists())
        self.assertFalse((root / "phase1/completions" / role).exists())
        self.assertEqual(self.verdicts(root)[0], {})
        # Negative control: the same call served Sonnet 5.5 is admitted.
        admitted = self.run_verifier(root, ["F13"])
        self.assertEqual(admitted.returncode, 0, admitted.stderr + admitted.stdout)
        self.assertTrue((root / "work/verifier" / role).is_dir())

        # The coordinator.
        fixture = self.coordinator_audit(SONNET_5_5)
        self.wrap(fixture)
        result = self.run_coordinator(fixture, {COORDINATOR_LEGACY: ["claude-sonnet-5"]})
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        row = self.coordinator_row(fixture)
        self.assertEqual((row["status"], row["error_class"], row["requested_model"]), ("failed", "model", "sonnet-5-5"))
        self.assertIn("expected exactly one served Sonnet 5.5 primary; observed 0", row["error"])
        for promoted in ("report/REPORT_v1.md", "report/ADJUDICATION.md", "work/findings.jsonl"):
            self.assertFalse((fixture.root / promoted).exists(), promoted)
        # Negative control: served Sonnet 5.5, the same fixture promotes.
        result = self.run_coordinator(fixture, {COORDINATOR_LEGACY: ["claude-sonnet-5-5"]})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue((fixture.root / "report/REPORT_v1.md").is_file())


if __name__ == "__main__":
    unittest.main()
