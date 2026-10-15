"""2.7.2: a Phase-1 call stopped by its timeout is a charged, terminalized attempt, and the timeout is per audit.

Until 2.7.2 every Phase-1 call the provider timeout stopped was left stranded at ``provider_requested``: the killed
CLI prints no result envelope, ``parse_result`` then raised ``run_coordinator.CoordinatorError``, which
``run_process`` did not catch, so no status or terminal row was written. ``--recover-stranded`` then closed it as a
request the provider never accepted, and two such closes refused the role for good
(``maximum_pre_inference_retryable_rejections_per_role``), although both calls had run for the full timeout. On
the Goepp et al. audit (20260930-audit-2b59fd0d) that stopped four of five Sonnet 5.5 auditors after two dispatches.

* ``TimeoutAccounting`` -- with a fake CLI that hangs past a two-second timeout, ``run_process`` returns, and the
  runner's own failure path writes the terminal row: a call that wrote into its output workspace is a started,
  charged attempt (``timed_out``, ``error_class`` timeout, one attempt, zero pre-inference requests); one that wrote
  nothing is still booked as not started, but is terminalized rather than stranded.
* ``RecoverStranded`` -- a 2.1 strand whose workspace holds files the model wrote is charged one attempt and
  recorded as started; one with an empty workspace is recorded uncharged, as before.
* ``FrozenTimeout`` -- a new audit freezes 5,400 s; ``load_policy`` admits a snapshot with either live timeout
  (2,700 or 5,400) on either live probe bound, and refuses any other (or a missing one) before any call; each
  runner uses its audit's own timeout and refuses an override that differs from it.
* ``DriftPinsTheShippedTimeout`` -- config/check_policy_drift.py pins both policy leaves and joins its literal to
  scripts/phase1_timeouts.py; its named negative control is rejected by each pin, and reverts on disk fail.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import claude_routes  # noqa: E402
import lifecycle_journal  # noqa: E402
import phase1_timeouts  # noqa: E402
import record_invocation  # noqa: E402
import run_phase1  # noqa: E402
import test_270_claude_routes as route_tests  # noqa: E402  (minimal_audit, route_policy)
import test_run_phase1  # noqa: E402  (the Phase-1 audit fixture)

LEGACY, SHIPPED = 2700, 5400
PROBE_BOUNDS = (150000, 350000)
ROLE = "auditor_reproduction"
# The shipped auditor route, as the shipped policy these fixtures freeze names it (2.7.3: Sonnet 5.5 at xhigh).
ROUTE = {key: json.loads((SKILL / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))
         ["models"]["phase_1_auditors"][ROLE][key] for key in ("requested_model", "spawn_alias", "reasoning_effort")}
HANG_WRITES = "#!/bin/sh\ncat >/dev/null\nmkdir -p evidence\necho partial > evidence/partial.json\nexec sleep 30\n"
HANG_SILENT = "#!/bin/sh\ncat >/dev/null\nexec sleep 30\n"


def shipped_policy() -> dict:
    return json.loads((SKILL / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))


def policy_with(timeout, bound: int = 350000) -> dict:
    policy = shipped_policy()
    policy["phase_1"]["runtime"]["timeout_seconds"] = timeout
    policy["phase_1"]["provider_timeout_seconds"] = timeout
    policy["phase_1"]["runtime"]["probe_max_normalized_tokens"] = bound
    policy["phase_1"]["capability_probe"]["maximum_normalized_tokens"] = bound
    return policy


class AuditFixture(unittest.TestCase):
    """A metadata-2.1 Phase-1 audit (test_run_phase1's fixture) and a hanging fake CLI."""

    def setUp(self) -> None:
        self.fixture = test_run_phase1.Phase1RunnerTests("test_normal_path_is_one_probe_plus_five_parallel_first_try_auditors")
        self.fixture.setUp()
        self.root = self.fixture.make_audit("timeout21", metadata21=True)

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def fake(self, body: str) -> pathlib.Path:
        path = self.fixture.base / f"hang-{abs(hash(body))}.sh"
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_and_fail(self, body: str, name: str):
        """One call through run_process under a two-second timeout, then the runner's own failure path."""
        attempt_dir = self.root / "phase1/roles" / ROLE / ("a" * 64) / f"attempt_01_{name}"
        output = attempt_dir / "output.partial"
        output.mkdir(parents=True)
        execution = run_phase1.run_process(
            self.root, ROLE, "role_inference", [str(self.fake(body))], output, "prompt", "a" * 64, "b" * 64, 2,
            session_id="00000000-0000-4000-8000-000000000001", route=ROUTE,
        )
        errors = run_phase1.execution_errors(execution)
        error_class = run_phase1.classify_failure("; ".join(errors) + " " + execution.stderr, execution.timed_out)
        run_phase1.append_failure(self.root, execution, ROLE, "role_inference", "a" * 64, "b" * 64,
                                  "; ".join(errors), error_class)
        [row] = [row for row in run_phase1.read_ledger(self.root) if row.get("role") == ROLE]
        status = json.loads((attempt_dir / "status.json").read_text(encoding="utf-8"))
        return execution, error_class, row, status


class TimeoutAccounting(AuditFixture):
    def test_a_timed_out_call_that_wrote_to_its_workspace_is_a_charged_attempt(self) -> None:
        execution, error_class, row, status = self.run_and_fail(HANG_WRITES, "writes")
        self.assertTrue(execution.timed_out)
        self.assertTrue(execution.inference_started)
        self.assertEqual(execution.provider_request_status, "accepted")
        self.assertEqual(error_class, "timeout")
        self.assertFalse(run_phase1.retryable(error_class), "a timeout is not retried inside one run")
        self.assertEqual((row["status"], row["attempt"], row["inference_start_status"]), ("timed_out", 1, "started"))
        self.assertEqual((status["state"], status["attempt"]), ("failed", 1))
        self.assertEqual(run_phase1.role_attempt_count(self.root, ROLE), 1)
        self.assertEqual(run_phase1.role_preinference_request_count(self.root, ROLE), 0)
        # the next run is not refused by the strand guard
        run_phase1.guard_stranded_attempts(self.root, self.root / "phase1")

    def test_a_timed_out_call_that_wrote_nothing_is_terminalized_and_uncharged(self) -> None:
        execution, error_class, row, status = self.run_and_fail(HANG_SILENT, "silent")
        self.assertTrue(execution.timed_out)
        self.assertFalse(execution.inference_started)
        self.assertEqual((row["status"], row["attempt"], row["inference_start_status"]),
                         ("timed_out", None, "not_started"))
        self.assertEqual(status["state"], "failed")
        self.assertEqual(run_phase1.role_attempt_count(self.root, ROLE), 0)
        self.assertEqual(run_phase1.role_preinference_request_count(self.root, ROLE), 1)
        run_phase1.guard_stranded_attempts(self.root, self.root / "phase1")

    def test_an_empty_envelope_no_longer_escapes_run_process(self) -> None:
        # the 2.7.1 defect: parse_result's CoordinatorError was not caught
        with self.assertRaises(run_phase1.run_coordinator.CoordinatorError):
            run_phase1.parse_result("")
        self.assertFalse(issubclass(run_phase1.run_coordinator.CoordinatorError, run_phase1.RunnerError))
        self.run_and_fail(HANG_SILENT, "envelope")  # returns instead of raising


class RecoverStranded(AuditFixture):
    def test_a_reservation_without_an_effort_is_recorded_at_the_snapshot_routes_effort(self) -> None:
        # 2.7.3 (review, mutant M20): --recover-stranded's terminal row takes the snapshot route's effort when the
        # reservation names none (the journal requires the key, not a value) -- through recover_stranded_attempts
        # itself, not only snapshot_route_effort as a unit. The fixture freezes the shipped policy: xhigh.
        self.assertEqual(ROUTE["reasoning_effort"], "xhigh")
        reservation = run_phase1.lifecycle_reserve(self.root, ROLE, "role_inference", "c" * 64, "d" * 64,
                                                   route={**ROUTE, "reasoning_effort": None})
        run_phase1.lifecycle_requested(self.root, reservation)
        attempt_dir = self.root / "phase1/roles" / ROLE / ("c" * 64) / "attempt_01_killed"
        (attempt_dir / "output.partial").mkdir(parents=True)
        (attempt_dir / "output.partial/REPORT.md").write_text("partial\n", encoding="utf-8")
        (attempt_dir / "status.json").write_text(json.dumps({
            "schema_version": "i4r-phase1-attempt-1", "state": "provider_requested",
            "invocation_id": reservation["invocation_id"], "attempt_kind": "role_inference", "attempt": None,
            "provider_request_status": "requested", "inference_start_status": "not_started",
            "requested_session_id": "00000000-0000-4000-8000-000000000002",
            "input_artifact_sha256": "c" * 64, "started_at": "2026-09-30T15:08:33.000Z",
        }), encoding="utf-8")
        self.assertEqual(run_phase1.recover_stranded_attempts(self.root, self.root / "phase1"), 1)
        [row] = [row for row in run_phase1.read_ledger(self.root) if row.get("role") == ROLE]
        self.assertEqual(row["reasoning_effort"], "xhigh")

    def strand(self, name: str, *, writes: bool) -> pathlib.Path:
        """What a runner killed mid-call leaves: a 2.1 reservation, a requested dispatch, status provider_requested."""
        reservation = run_phase1.lifecycle_reserve(self.root, ROLE, "role_inference", "c" * 64, "d" * 64, route=ROUTE)
        run_phase1.lifecycle_requested(self.root, reservation)
        attempt_dir = self.root / "phase1/roles" / ROLE / ("c" * 64) / f"attempt_01_{name}"
        (attempt_dir / "output.partial").mkdir(parents=True)
        if writes:
            (attempt_dir / "output.partial/REPORT.md").write_text("partial\n", encoding="utf-8")
        (attempt_dir / "status.json").write_text(json.dumps({
            "schema_version": "i4r-phase1-attempt-1", "state": "provider_requested",
            "invocation_id": reservation["invocation_id"], "attempt_kind": "role_inference", "attempt": None,
            "provider_request_status": "requested", "inference_start_status": "not_started",
            "requested_session_id": "00000000-0000-4000-8000-000000000002",
            "input_artifact_sha256": "c" * 64, "started_at": "2026-09-30T15:08:33.000Z",
        }), encoding="utf-8")
        return attempt_dir

    def test_a_strand_whose_workspace_holds_model_writes_is_charged_one_attempt(self) -> None:
        self.strand("killed", writes=True)
        self.assertEqual(run_phase1.recover_stranded_attempts(self.root, self.root / "phase1"), 1)
        [row] = [row for row in run_phase1.read_ledger(self.root) if row.get("role") == ROLE]
        self.assertEqual((row["attempt"], row["inference_start_status"], row["provider_request_status"]),
                         (1, "started", "accepted"))
        self.assertIn("inference had started", row["error"])
        self.assertEqual(run_phase1.role_preinference_request_count(self.root, ROLE), 0)

    def test_a_strand_with_an_empty_workspace_is_still_uncharged(self) -> None:
        self.strand("rejected", writes=False)
        self.assertEqual(run_phase1.recover_stranded_attempts(self.root, self.root / "phase1"), 1)
        [row] = [row for row in run_phase1.read_ledger(self.root) if row.get("role") == ROLE]
        self.assertEqual((row["attempt"], row["inference_start_status"]), (None, "not_started"))
        self.assertEqual(run_phase1.role_preinference_request_count(self.root, ROLE), 1)


class SnapshotFixture(unittest.TestCase):
    """Minimal audits frozen from an explicit policy, and the Phase-1 fake CLI (which records any start)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-272-")
        self.base = pathlib.Path(self.temporary.name)
        self.fake = self.base / "fake-claude-phase1"
        shutil.copyfile(TESTS / "fake_claude_phase1.py", self.fake)
        self.fake.chmod(0o755)
        self.state = self.base / "fake-state.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def audit(self, name: str, policy: dict, *, metadata21: bool = False) -> pathlib.Path:
        return route_tests.minimal_audit(self.base, name, policy, metadata21=metadata21)


class FrozenTimeout(SnapshotFixture):
    def test_the_module_holds_the_legacy_and_the_shipped_timeout(self) -> None:
        self.assertEqual(phase1_timeouts.LIVE_PHASE1_TIMEOUTS, (LEGACY, SHIPPED))
        self.assertEqual(run_phase1.MAXIMUM_TIMEOUT_SECONDS, SHIPPED)
        self.assertEqual(run_phase1.DEFAULT_TIMEOUT_SECONDS, LEGACY)
        for value in (True, "5400", None, 5400.5):
            self.assertIsNone(phase1_timeouts.live_phase1_timeout(value))
        self.assertEqual(phase1_timeouts.live_phase1_timeout(5400.0), SHIPPED)

    def test_the_shipped_policy_freezes_the_shipped_timeout(self) -> None:
        policy = shipped_policy()
        self.assertEqual(policy["phase_1"]["runtime"]["timeout_seconds"], SHIPPED)
        self.assertEqual(policy["phase_1"]["provider_timeout_seconds"], SHIPPED)
        root = self.base / "init"
        root.mkdir()
        record_invocation.initialize_policy_snapshot(root)
        self.assertEqual(run_phase1.phase1_timeout(record_invocation.load_policy_snapshot(root)), SHIPPED)

    def test_load_policy_admits_each_live_timeout_on_each_bound_and_route(self) -> None:
        for timeout in (LEGACY, SHIPPED):
            for bound in PROBE_BOUNDS:
                for route in (claude_routes.SONNET_5, claude_routes.SONNET_5_5):
                    with self.subTest(timeout=timeout, bound=bound, route=route["requested_model"]):
                        policy = route_tests.route_policy(route)
                        policy["phase_1"]["runtime"].update(timeout_seconds=timeout, probe_max_normalized_tokens=bound)
                        root = self.audit(f"ok-{timeout}-{bound}-{route['requested_model']}", policy)
                        loaded, _configured = run_phase1.load_policy(root)
                        self.assertEqual(run_phase1.phase1_timeout(loaded), timeout)
                        self.assertIs(type(run_phase1.phase1_timeout(loaded)), int)

    def test_a_271_snapshot_is_judged_against_exactly_the_271_runtime(self) -> None:
        runtime = run_phase1.packaged_runtime(350000)
        self.assertEqual(runtime["timeout_seconds"], LEGACY, "the default is the legacy (2.7.1) timeout")
        self.assertEqual(run_phase1.packaged_runtime(350000, SHIPPED),
                         {**runtime, "timeout_seconds": SHIPPED}, "only the timeout leaf differs")

    def test_load_policy_refuses_any_other_timeout_or_none(self) -> None:
        named = ("audit-scoped phase_1.runtime differs from the packaged execution contract: its timeout_seconds {} "
                 "is not a packaged Phase-1 timeout (2700 or 5400)")
        generic = "audit-scoped phase_1.runtime differs from the packaged execution contract"
        missing = policy_with(SHIPPED)
        del missing["phase_1"]["runtime"]["timeout_seconds"]
        cases = [(policy_with(value), named.format(repr(value)))
                 for value in (2699, 2701, 3600, 5399, 5401, 10800, 0, "5400", True, None)]
        cases.append((missing, generic))
        for index, (policy, message) in enumerate(cases):
            with self.subTest(case=message):
                root = self.audit(f"bad-{index}", policy)
                with self.assertRaises(run_phase1.RunnerError) as caught:
                    run_phase1.load_policy(root)
                self.assertEqual(str(caught.exception), message)

    def test_bounded_timeout_admits_up_to_the_largest_live_timeout(self) -> None:
        self.assertEqual(run_phase1.bounded_timeout("5400"), SHIPPED)
        with self.assertRaises(Exception):
            run_phase1.bounded_timeout("5401")

    def run_probe_only(self, root: pathlib.Path, *extra: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "FAKE_PHASE1_STATE": str(self.state), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(SCRIPTS / "run_phase1.py"), str(root), "--claude-bin",
                               str(self.fake), "--probe-only", *extra], text=True, capture_output=True, env=env,
                              timeout=120, check=False)

    def test_each_audit_runs_under_its_own_timeout_and_refuses_another(self) -> None:
        for timeout, other in ((LEGACY, SHIPPED), (SHIPPED, LEGACY)):
            with self.subTest(timeout=timeout):
                root = self.audit(f"runner-{timeout}", policy_with(timeout))
                refused = self.run_probe_only(root, "--timeout-seconds", str(other))
                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertIn(f"canonical Phase-1 runs require the policy-bound {timeout}-second/$15 limits",
                              refused.stderr)
                # the default and an explicit equal value both reach the local preflight, which pauses a minimal
                # audit because it has no deposit to freeze
                for extra in ((), ("--timeout-seconds", str(timeout))):
                    result = self.run_probe_only(root, *extra)
                    self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                    self.assertIn("PAUSED: Phase-1 local preflight failed", result.stderr)
        self.assertFalse(self.state.exists(), "no Claude call was started")

    def test_the_verifier_refuses_a_timeout_other_than_its_audits(self) -> None:
        root = self.audit("verifier", policy_with(SHIPPED), metadata21=True)
        (root / "phase1").mkdir()
        env = {**os.environ, "FAKE_PHASE1_STATE": str(self.state), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, str(SCRIPTS / "run_verifier.py"), str(root), "--findings", "F13",
                                 "--claude-bin", str(self.fake), "--timeout-seconds", str(LEGACY)],
                                text=True, capture_output=True, env=env, timeout=120, check=False)
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertIn("a verifier call runs under the policy-bound 5400-second/$15 Phase-1 limits", result.stderr)
        self.assertFalse(self.state.exists(), "no Claude call was started")


def base_271() -> pathlib.Path | None:
    """The 2.7.1 tree: I4R_BASE_271, or ../base271 beside this tree as a release scratch copy lays it out."""
    named = os.environ.get("I4R_BASE_271")
    candidate = pathlib.Path(named) if named else SKILL.parent / "base271"
    if (candidate / "VERSION").is_file() and (candidate / "VERSION").read_text().strip() == "2.7.1":
        return candidate
    if named:
        raise AssertionError(f"I4R_BASE_271={named} is not a 2.7.1 tree")
    return None


class AcrossVersions(SnapshotFixture):
    """A 2.7.1 audit runs on 2.7.2 (above); 2.7.1 refuses a 2.7.2 audit's snapshot before any call."""

    def test_271_refuses_a_new_audit_and_admits_its_own(self) -> None:
        base = base_271()
        if base is None:
            self.skipTest("no 2.7.1 tree (set I4R_BASE_271)")
        env = {**os.environ, "FAKE_PHASE1_STATE": str(self.state), "PYTHONDONTWRITEBYTECODE": "1"}

        def probe_only(root: pathlib.Path) -> subprocess.CompletedProcess[str]:
            return subprocess.run([sys.executable, "-B", str(base / "scripts/run_phase1.py"), str(root),
                                   "--claude-bin", str(self.fake), "--probe-only"], text=True, capture_output=True,
                                  env=env, timeout=120, check=False)

        refused = probe_only(self.audit("new-on-271", policy_with(SHIPPED)))
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("ERROR: audit-scoped phase_1.runtime differs from the packaged execution contract",
                      refused.stderr)
        # an audit as 2.7.1 froze it: the legacy timeout, on Sonnet 5.5 at max (2.7.3 moved new audits to xhigh,
        # a route 2.7.1 does not dispatch)
        as_271 = policy_with(LEGACY)
        models = as_271["models"]
        for entry in (models["coordinator"], models["post_fan_out_verifier"], *models["phase_1_auditors"].values(),
                      as_271["phase_1"]["capability_probe"]):
            entry["reasoning_effort"] = "max"
        as_271["critique"].pop("provider_timeout_seconds", None)  # 2.7.4's leaves, which 2.7.1 never froze
        as_271["critique"]["later_rounds"]["routing_manifest"].pop("claim_change_rule", None)
        as_271["critique"].pop("critic_route_lock", None)   # 2.7.5's lock
        admitted = probe_only(self.audit("old-on-271", as_271))
        self.assertEqual(admitted.returncode, 3, admitted.stdout + admitted.stderr)
        self.assertIn("PAUSED: Phase-1 local preflight failed", admitted.stderr)
        self.assertFalse(self.state.exists(), "no Claude call was started")


class DriftPinsTheShippedTimeout(unittest.TestCase):
    def checker(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("i4r_272_drift", SKILL / "config/check_policy_drift.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_checker_literal_is_the_shipped_timeout(self) -> None:
        module = self.checker()
        self.assertEqual((module.SHIPPED_PHASE1_TIMEOUT, module.LEGACY_PHASE1_TIMEOUT), (SHIPPED, LEGACY))
        self.assertEqual(phase1_timeouts.SHIPPED_PHASE1_TIMEOUT, SHIPPED)

    def test_the_named_control_is_rejected_by_each_pin(self) -> None:
        module = self.checker()
        policy = shipped_policy()
        for leaves in (("runtime",), ("leaf",), ("runtime", "leaf")):
            with self.subTest(leaves=leaves):
                candidate = json.loads(json.dumps(policy))
                if "runtime" in leaves:
                    candidate["phase_1"]["runtime"]["timeout_seconds"] = LEGACY
                if "leaf" in leaves:
                    candidate["phase_1"]["provider_timeout_seconds"] = LEGACY
                errors: list[str] = []
                module.check_policy_shape(candidate, errors)
                self.assertEqual(
                    any(e.startswith("the Phase-1 timeout phase_1.runtime.timeout_seconds is") for e in errors),
                    "runtime" in leaves, errors)
                self.assertEqual(
                    any(e.startswith("the Phase-1 leaf provider_timeout_seconds is") for e in errors),
                    "leaf" in leaves, errors)

    def copied_tree(self, base: pathlib.Path) -> pathlib.Path:
        tree = base / "tree"
        for part in ("config", "scripts", "schemas", "reference", "prompts"):
            if (SKILL / part).exists():
                shutil.copytree(SKILL / part, tree / part, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("SKILL.md", "INSTALL.md", "CHEATSHEET.html", "VERSION", "CHANGELOG.md"):
            if (SKILL / name).exists():
                shutil.copyfile(SKILL / name, tree / name)
        return tree

    def run_checker(self, tree: pathlib.Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-B", str(tree / "config/check_policy_drift.py")], text=True,
                              capture_output=True, timeout=180, check=False)

    def test_the_unmodified_tree_passes_with_fifteen_controls(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            result = self.run_checker(self.copied_tree(pathlib.Path(base)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("twenty planted drifts were rejected", result.stdout)

    def test_each_revert_on_disk_is_rejected_by_its_own_pin(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            tree = self.copied_tree(pathlib.Path(base))
            module = tree / "scripts/phase1_timeouts.py"
            text = module.read_text(encoding="utf-8")
            module.write_text(text.replace("SHIPPED_PHASE1_TIMEOUT = 5_400", "SHIPPED_PHASE1_TIMEOUT = 2_700"),
                              encoding="utf-8")
            result = self.run_checker(tree)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("scripts/phase1_timeouts.py SHIPPED_PHASE1_TIMEOUT is 2700, expected 5400", result.stdout)
            module.write_text(text.replace("LIVE_PHASE1_TIMEOUTS: tuple[int, ...] = (LEGACY_PHASE1_TIMEOUT, "
                                           "SHIPPED_PHASE1_TIMEOUT)",
                                           "LIVE_PHASE1_TIMEOUTS: tuple[int, ...] = (SHIPPED_PHASE1_TIMEOUT,)"),
                              encoding="utf-8")
            result = self.run_checker(tree)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no longer holds the 2700 s timeout", result.stdout)
            module.write_text(text, encoding="utf-8")
            policy_path = tree / "config/pipeline-policy-v2.json"
            policy_path.write_text(policy_path.read_text(encoding="utf-8").replace(
                '"timeout_seconds": 5400,', '"timeout_seconds": 2700,'), encoding="utf-8")
            result = self.run_checker(tree)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("the Phase-1 timeout phase_1.runtime.timeout_seconds is 2700, not 5400", result.stdout)


if __name__ == "__main__":
    unittest.main()
