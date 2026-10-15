"""2.7.0: new audits' critics, critique model probe and targeted adjudicator move to GPT-6.1 Sol.

The move is policy-only, exactly as 2.6.0 (to gpt-6-sol) and 2.6.1 (back to gpt-5.6-terra) were:
scripts/run_critique.sh reads the critic and adjudicator route from the audit's own frozen
pipeline-policy.snapshot.json, so an audit initialised on 2.6.1-2.6.10 keeps gpt-5.6-terra for its whole
life while a new one freezes gpt-6.1-sol. Every test drives the real driver with tests/fake_codex.sh, which
records the ``-m`` and effort of every call it receives; no provider is called.

* ``ShippedCriticRoute`` -- the policy ships gpt-6.1-sol at high (critics, probe) and max (adjudicator),
  the drift check pins that literal, and the 2.6.10 policy the legacy audit below is frozen from ships Terra.
* ``CriticRouteDispatch`` -- one round-1 critique with a triggered adjudication on an audit frozen from the
  shipped policy and one frozen from a copy of the 2.6.10 policy. Each audit's calls, ledger rows, round
  plan, snapshots and validation name its own route; the other route is refused by the ledger checks and,
  as a knob, by the driver before any call (the negative controls).
* ``AccountWithoutSol`` -- the refusal seen on 2026-09-29 (CLI 0.158.0-alpha.2.1): the critique model probe
  stops the round before any critic call, and the documented re-pin to gpt-5.6-terra finishes it on Terra.
  An account without Terra does not stop a Sol audit (negative control).
* ``FinalizeOnTheFrozenRoute`` -- finalize_run.sh --self-test builds its critic rows from the audit's snapshot:
  it finalizes a Terra audit in a copy of this tree whose policy ships Terra, a Sol audit in an unchanged
  copy, and in each refuses the same rows on the other model.

The 2.6.10 tree is ``I4R_BASE_2610``, or ``../base`` beside this tree as a release scratch copy lays it out.
Without it the legacy policy is the shipped one with every Claude role on Sonnet 5 and the critic route on
gpt-5.6-terra, which ``test_the_2610_policy_differs_from_the_shipped_one_only_in_the_routes`` proves is the
2.6.10 policy whenever the tree is at hand.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
DRIVER = SCRIPTS / "run_critique.sh"
FAKE_CODEX = TESTS / "fake_codex.sh"
MODEL_VERIFIER = SCRIPTS / "verify_codex_model.sh"
POLICY_PATH = SKILL / "config" / "pipeline-policy-v2.json"
sys.path.insert(0, str(SCRIPTS))
import claude_routes  # noqa: E402

SOL = "gpt-6.1-sol"
TERRA = "gpt-5.6-terra"
ORDINARY_ROLES = ("model_probe", "stats", "data", "fairness", "completeness")
# Knobs a developer's shell could carry into the driver; the route must come from the snapshot alone.
DRIVER_KNOBS = ("CRITIC_MODEL", "CRITIC_EFFORT", "ADJUDICATOR_MODEL", "ADJUDICATOR_EFFORT", "MODEL", "EFFORT",
                "MAX_TRIES", "I4R_MAX_ATTEMPTS", "DIFF_MANIFEST", "ADJUDICATE", "CRITICS", "I4R_FORCE_RERUN",
                "I4R_AUTO_AUTHORIZE", "I4R_CODEX_BIN", "CODEX", "FAKE_UNSUPPORTED_MODEL", "I4R_SNAPSHOT_REFREEZE",
                "I4R_PROVIDER_TIMEOUT_SECONDS")
IGNORED = shutil.ignore_patterns(
    ".git", ".pytest_cache", "__pycache__", "_history*", "CHEATSHEET.pdf", ".DS_Store", "*.pyc")


def _module(name: str):
    """Resolve a scripts module at use: several suites swap sys.modules entries during discovery."""
    return importlib.import_module(name)


def _base_2610() -> pathlib.Path | None:
    named = os.environ.get("I4R_BASE_2610")
    tree = pathlib.Path(named) if named else SKILL.parent / "base"
    try:
        version = (tree / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return tree.resolve() if version == "2.6.10" and not (tree / "scripts/claude_routes.py").exists() else None


BASE = _base_2610()


def shipped_policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def on_critic_route(policy: dict, model: str) -> dict:
    """``policy`` with the critics, the critique model probe and the adjudicator on ``model`` (efforts kept),
    which is also what scripts/repin_critic_route.py writes into one audit's snapshot."""
    policy["models"]["critics"]["requested_model"] = model
    policy["models"]["targeted_adjudicator"]["requested_model"] = model
    policy["critique"]["model_probe"]["requested_model"] = model
    return policy


def reconstructed_2610_policy() -> dict:
    """The shipped policy with the routes 2.6.10 shipped: Sonnet 5 for every Claude role, Terra for the critics,
    and 2.6.10's capability-probe bound of 150,000 normalized tokens (2.7.1 raised new audits' to 350,000)."""
    policy = on_critic_route(shipped_policy(), TERRA)
    models = policy["models"]
    for row in (models["coordinator"], *models["phase_1_auditors"].values(), models["post_fan_out_verifier"]):
        row.update(claude_routes.SONNET_5)
    policy["phase_1"]["capability_probe"]["requested_model"] = claude_routes.SONNET_5["requested_model"]
    # 2.7.3 moved new audits' Claude roles, the probe included, to xhigh; 2.6.10 ran them all at max.
    policy["phase_1"]["capability_probe"]["reasoning_effort"] = claude_routes.SONNET_5["reasoning_effort"]
    policy["phase_1"]["runtime"]["probe_max_normalized_tokens"] = 150000
    policy["phase_1"]["capability_probe"]["maximum_normalized_tokens"] = 150000
    # 2.7.2 raised new audits' Phase-1 timeout to 5,400 s; every audit initialised before froze 2,700 s.
    policy["phase_1"]["runtime"]["timeout_seconds"] = 2700
    policy["phase_1"]["provider_timeout_seconds"] = 2700
    # 2.7.4 added the critique ceiling leaf; 2.6.10 froze none
    policy["critique"].pop("provider_timeout_seconds", None)
    policy["critique"]["later_rounds"]["routing_manifest"].pop("claim_change_rule", None)
    policy["critique"].pop("critic_route_lock", None)   # 2.7.5 added the critic-route lock; 2.6.10 froze none
    return policy


def legacy_policy_bytes() -> bytes:
    """The policy an audit initialised on 2.6.10 froze: the 2.6.10 file itself when the tree is at hand."""
    if BASE is not None:
        return (BASE / "config/pipeline-policy-v2.json").read_bytes()
    return (json.dumps(reconstructed_2610_policy(), indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def clean_env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in DRIVER_KNOBS}
    env.update(PYTHONDONTWRITEBYTECODE="1", LC_ALL="C", **extra)
    return env


def write_audit(audit: pathlib.Path, policy_file: pathlib.Path, audit_id: str) -> pathlib.Path:
    """tests/test_run_critique.sh's fixture audit as metadata 2.1, its snapshot frozen by
    record_invocation.initialize_policy_snapshot (unchanged) from ``policy_file``."""
    record_invocation, journal = _module("record_invocation"), _module("lifecycle_journal")
    (audit / "report").mkdir(parents=True)
    (audit / "work").mkdir()
    for name, text in (("PAPER.txt", "Example et al. (2026), deterministic runtime fixture."),
                       ("ENV.txt", "python3 standard-library-only fixture"),
                       ("SLOW.txt", "Do not run any long job."),
                       ("WRAPUP.txt", "Review the current changed claims and their dependencies.")):
        (audit / name).write_text(text + "\n", encoding="utf-8")
    report = audit / "report/REPORT_final.md"
    report.write_text("# Fixture report\n\nThe immutable audit draft contains one bounded claim.\n", encoding="utf-8")
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    row = {
        "schema_version": "2.0.0", "id": "F1", "claim": "The bounded fixture claim is correct.",
        "domain": "statistics", "source_location": {"path": "report/REPORT_final.md", "locator": "fixture claim"},
        "claim_ref": "fixture claim", "status": "draft",
        "ledger_disposition": {"status": "INCLUDED", "target": None, "reason": None},
        "coordinator_approved": False, "impact": "value_wrong", "impact_rank": 2,
        "verification_tier": 2, "paper_only": False, "confidence": "LIKELY",
        "evidence": {"source_hashes": [{"path": "report/REPORT_final.md", "sha256": digest}],
                     "rerun_command": "sha256 report/REPORT_final.md", "expected": "bound fixture",
                     "observed": "bound fixture", "tolerance_basis": None},
        "unverifiable_basis": None, "verifications": [],
        "dependencies": {"environment_fingerprint": None, "finding_ids": [], "artifact_paths": []},
        "settled": None, "benign_explanations_ruled_out": [], "report_section": None,
        "verification_snippet": None, "disclosure_flag": False,
        "remedy": {"class": "one_number", "description": "Retain the bounded value.", "verified": None},
        "changes_conclusion": False, "changes_conclusion_which": None,
        "provenance": {"origin_role": "fixture", "independently_found_by": ["fixture"], "agreed_after_seeing": [],
                       "round_introduced": 0, "source_records": ["report/REPORT_final.md"]},
    }
    (audit / "work/findings.jsonl").write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    with mock.patch.object(record_invocation, "POLICY", policy_file):
        record_invocation.initialize_policy_snapshot(audit)
    snapshot = hashlib.sha256((audit / "pipeline-policy.snapshot.json").read_bytes()).hexdigest()
    (audit / "run.json").write_text(json.dumps({
        "schema_version": "2.0.0", "audit_id": audit_id, "normalization": None,
        "toolkit": {"pipeline_policy_snapshot_sha256": snapshot},
    }, indent=2) + "\n", encoding="utf-8")
    (audit / "invocations.jsonl").write_text("", encoding="utf-8")
    journal.initialize(audit)
    return audit


def run_driver(audit: pathlib.Path, state: pathlib.Path, invocation: str, scenario: str = "adjudicate",
               **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DRIVER), "1"], text=True, capture_output=True, timeout=600, check=False,
        env=clean_env(AUDIT_DIR=str(audit), CODEX=str(FAKE_CODEX), FAKE_CODEX_STATE=str(state),
                      FAKE_CODEX_SCENARIO=scenario, I4R_INVOCATION_ID=invocation, RETRY_BACKOFF_SECONDS="0",
                      PROBE_LOCK_WAIT_SECONDS="3", **extra))


def calls(state: pathlib.Path, role: str = "total") -> int:
    path = state / f"calls_{role}"
    return int(path.read_text().split()[0]) if path.is_file() else 0


def received(state: pathlib.Path, kind: str, role: str) -> list[str]:
    """Every ``-m`` (kind "models") or effort (kind "efforts") the fake Codex CLI received for ``role``."""
    path = state / f"{kind}_{role}"
    return path.read_text().split() if path.is_file() else []


def ledger(audit: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in (audit / "invocations.jsonl").read_text().splitlines() if line.strip()]


def frozen_critic_route(audit: pathlib.Path) -> tuple[str, str, str]:
    policy = _module("record_invocation").load_policy_snapshot(audit)
    return (policy["models"]["critics"]["requested_model"], policy["models"]["targeted_adjudicator"]["requested_model"],
            policy["critique"]["model_probe"]["requested_model"])


def thaw(root: pathlib.Path) -> None:
    """Give back the write bits frozen snapshots and read-only packages took, so the directory can be removed."""
    for path in [root, *root.rglob("*")]:
        try:
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)
        except OSError:
            pass


def validate_ledger(test: unittest.TestCase, audit: pathlib.Path) -> None:
    """Every ledger row passes record_invocation.validate_record against the audit's own snapshot."""
    rows = ledger(audit)
    test.assertTrue(rows)
    for row in rows:
        _module("record_invocation").validate_record(row, audit)


def run_python(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, *args], text=True, capture_output=True, timeout=300, check=False,
                          env=clean_env())


# ------------------------------------------------------------------------------ the shipped route
class ShippedCriticRoute(unittest.TestCase):
    def test_new_audits_freeze_gpt_6_1_sol_with_the_efforts_unchanged(self) -> None:
        policy = shipped_policy()
        self.assertEqual(policy["models"]["critics"], {
            "requested_model": SOL, "provider": "openai", "reasoning_effort": "high",
            "must_differ_from_coordinator_family": True})
        self.assertEqual(policy["models"]["targeted_adjudicator"], {
            "requested_model": SOL, "provider": "openai", "reasoning_effort": "max",
            "must_differ_from_coordinator_family": True})
        probe = policy["critique"]["model_probe"]
        self.assertEqual((probe["requested_model"], probe["reasoning_effort"]), (SOL, "high"))
        # an identifier, kept under Sol in 2.6.0 as well; the checker pins it
        self.assertEqual(policy["critique"]["adjudication"]["mode"], "terra_targeted_on_policy_triggers")
        spec = importlib.util.spec_from_file_location("i4r_270_critic_drift", SKILL / "config/check_policy_drift.py")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        self.assertEqual((checker.SHIPPED_CRITIC_MODEL, checker.LEGACY_CRITIC_MODEL), (SOL, TERRA))

    def test_the_2610_policy_differs_from_the_shipped_one_only_in_the_routes(self) -> None:
        legacy = json.loads(legacy_policy_bytes())
        self.assertEqual(legacy["models"]["critics"]["requested_model"], TERRA)
        self.assertEqual(legacy["models"]["targeted_adjudicator"]["requested_model"], TERRA)
        self.assertEqual(legacy["critique"]["model_probe"]["requested_model"], TERRA)
        self.assertEqual(legacy, reconstructed_2610_policy())
        self.assertNotEqual(on_critic_route(shipped_policy(), TERRA), shipped_policy(), "negative control")
        if BASE is None:
            self.skipTest("the byte-level check needs a pristine 2.6.10 tree (I4R_BASE_2610 or ../base)")
        self.assertEqual(legacy_policy_bytes(), (BASE / "config/pipeline-policy-v2.json").read_bytes())


# ------------------------------------------------------------------------------ dispatch by frozen route
class CriticRouteDispatch(unittest.TestCase):
    """One round 1 with a triggered adjudication on each audit, run once for the class."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="i4r-270-critic-route-")
        base = pathlib.Path(cls.temporary.name).resolve()
        legacy_file = base / "policy-2610.json"
        legacy_file.write_bytes(legacy_policy_bytes())
        cls.cases = {}
        for label, policy_file, route in (("new", POLICY_PATH, SOL), ("legacy", legacy_file, TERRA)):
            audit = write_audit(base / label / "audit", policy_file, f"audit-270-{label}")
            snapshot = (audit / "pipeline-policy.snapshot.json").read_bytes()
            state = base / label / "fake_state"
            result = run_driver(audit, state, f"route-{label}")
            if result.returncode != 0:
                thaw(base)
                cls.temporary.cleanup()
                raise AssertionError(f"the {label} round failed:\n{(result.stdout + result.stderr)[-4000:]}")
            cls.cases[label] = {"audit": audit, "state": state, "route": route, "snapshot": snapshot}

    @classmethod
    def tearDownClass(cls) -> None:
        thaw(pathlib.Path(cls.temporary.name))
        cls.temporary.cleanup()

    def audits(self) -> list[tuple[str, pathlib.Path, pathlib.Path, str, str]]:
        """(label, audit, fake state, its route, the other route) for each audit."""
        return [(label, case["audit"], case["state"], case["route"], TERRA if case["route"] == SOL else SOL)
                for label, case in self.cases.items()]

    def test_the_audits_froze_different_routes(self) -> None:
        self.assertEqual(frozen_critic_route(self.cases["new"]["audit"]), (SOL, SOL, SOL))
        self.assertEqual(frozen_critic_route(self.cases["legacy"]["audit"]), (TERRA, TERRA, TERRA))
        # the legacy audit is a 2.6.10 audit throughout: Sonnet 5 for its Claude roles too
        legacy = _module("record_invocation").load_policy_snapshot(self.cases["legacy"]["audit"])
        self.assertEqual(claude_routes.project(legacy["models"]["coordinator"]), claude_routes.SONNET_5)

    def test_every_call_is_made_on_the_audits_own_route(self) -> None:
        for label, audit, state, route, other in self.audits():
            with self.subTest(audit=label, route=route):
                status = json.loads((audit / "critique/round_1/ROUND_STATUS.json").read_text())
                self.assertEqual((status["outcome"], status["adjudication"]), ("complete", "complete"))
                for role in ORDINARY_ROLES:
                    self.assertEqual(received(state, "models", role), [route] * calls(state, role), role)
                    self.assertEqual(set(received(state, "efforts", role)), {"high"}, role)
                self.assertEqual(received(state, "models", "targeted_adjudicator"), [route])
                self.assertEqual(received(state, "efforts", "targeted_adjudicator"), ["max"])
                self.assertEqual(calls(state), 6, "one probe, four critics, one adjudicator")
                everything = [model for role in (*ORDINARY_ROLES, "targeted_adjudicator")
                              for model in received(state, "models", role)]
                self.assertNotIn(other, everything)
                # the round changed nothing in the frozen route
                self.assertEqual((audit / "pipeline-policy.snapshot.json").read_bytes(),
                                 self.cases["new" if route == SOL else "legacy"]["snapshot"])

    def test_the_ledger_plan_and_snapshots_name_the_audits_route(self) -> None:
        for label, audit, state, route, other in self.audits():
            with self.subTest(audit=label, route=route):
                rows = [row for row in ledger(audit) if row.get("provider") == "openai"]
                self.assertEqual({row["role"] for row in rows},
                                 {"model_probe", "critic_stats", "critic_data", "critic_fairness",
                                  "critic_completeness", "critic_synth"})
                self.assertEqual({row["requested_model"] for row in rows}, {route})
                self.assertEqual({row["reasoning_effort"] for row in rows if row["role"] != "critic_synth"}, {"high"})
                self.assertEqual({row["reasoning_effort"] for row in rows if row["role"] == "critic_synth"}, {"max"})
                round_dir = audit / "critique/round_1"
                status = json.loads((round_dir / "ROUND_STATUS.json").read_text())
                manifest = json.loads((round_dir / "snapshots" / status["input_sha256"] / "MANIFEST.json").read_text())
                self.assertEqual((manifest["model"], manifest["effort"]), (route, "high"))
                target = json.loads((round_dir / "invocations" / status["invocation_id"] / "target_snapshot.json").read_text())
                target_manifest = json.loads((pathlib.Path(target["snapshot"]) / "MANIFEST.json").read_text())
                self.assertEqual((target_manifest["model"], target_manifest["effort"]), (route, "max"))
                events = [json.loads(line) for line in (audit / "run.events.jsonl").read_text().splitlines() if line]
                grants = [event["payload"]["authorization"]["model_route"] for event in events
                          if event["type"] == "round_authorized"]
                self.assertEqual(len(grants), 1)
                self.assertIn(f"critics={route}/high", grants[0])
                self.assertIn(f"targeted_adjudicator={route}/max", grants[0])
                self.assertNotIn(other, grants[0])

    def test_the_ledger_validates_on_the_audits_route_and_not_on_the_other(self) -> None:
        for label, audit, state, route, other in self.audits():
            with self.subTest(audit=label, route=route):
                everything = subprocess.run(["bash", str(MODEL_VERIFIER), str(audit), "all"], text=True,
                                            capture_output=True, timeout=120, env=clean_env())
                self.assertEqual(everything.returncode, 0, everything.stdout + everything.stderr)
                for role, effort in (("critic_stats", "high"), ("critic_synth", "max"), ("model_probe", "high")):
                    exact = subprocess.run(["bash", str(MODEL_VERIFIER), str(audit), role, route, effort],
                                           text=True, capture_output=True, timeout=120, env=clean_env())
                    self.assertEqual(exact.returncode, 0, exact.stdout + exact.stderr)
                    wrong = subprocess.run(["bash", str(MODEL_VERIFIER), str(audit), role, other, effort],
                                           text=True, capture_output=True, timeout=120, env=clean_env())
                    self.assertNotEqual(wrong.returncode, 0, "negative control: the other route is not this audit's")
                    self.assertIn(f"expected exact route {other!r}", wrong.stderr)
                # the shared validator the ledger check of finalize runs (summarize_invocations --check, which
                # also wants a Phase 1 these audits do not have) judges every row by the snapshot, and the same
                # rows on the other route fail
                validate_ledger(self, audit)
                record_invocation = _module("record_invocation")
                for row in ledger(audit):
                    with self.assertRaisesRegex(ValueError, f"violates pipeline policy: requested_model='{other}', "
                                                            f"expected '{route}'"):
                        record_invocation.validate_record(dict(row, requested_model=other), audit)

    def test_a_knob_naming_the_other_route_is_refused_before_any_call(self) -> None:
        for label, audit, state, route, other in self.audits():
            with self.subTest(audit=label, route=route):
                before = calls(state)
                for knob, message in (
                        ("CRITIC_MODEL", f"critic model is pinned to {route} by this audit's "
                                         f"pipeline-policy.snapshot.json (got {other})"),
                        ("ADJUDICATOR_MODEL", f"targeted-adjudicator model is pinned to {route} by this audit's "
                                              f"pipeline-policy.snapshot.json (got {other})")):
                    result = run_driver(audit, state, f"knob-{knob.lower()}", **{knob: other})
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(message, result.stdout + result.stderr)
                self.assertEqual(calls(state), before, "a refused knob makes no provider call")


# ------------------------------------------------------------------------------ an account without Sol
class AccountWithoutSol(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="i4r-270-no-sol-")
        self.base = pathlib.Path(temporary.name).resolve()
        self.addCleanup(lambda: (thaw(self.base), temporary.cleanup()))
        self.audit = write_audit(self.base / "audit", POLICY_PATH, "audit-270-no-sol")
        self.state = self.base / "fake_state"

    def test_the_probe_stops_before_any_critic_a_terra_repin_is_refused_and_sol_finishes(self) -> None:
        refused = run_driver(self.audit, self.state, "no-sol-1", "success", FAKE_UNSUPPORTED_MODEL=SOL)
        output = refused.stdout + refused.stderr
        self.assertNotEqual(refused.returncode, 0, output)
        self.assertIn(f"The '{SOL}' model is not supported when using Codex with a ChatGPT account", output)
        self.assertGreaterEqual(calls(self.state, "model_probe"), 1)
        self.assertEqual(calls(self.state), calls(self.state, "model_probe"), "no critic was dispatched")
        for role in ("stats", "data", "fairness", "completeness", "targeted_adjudicator"):
            self.assertEqual(calls(self.state, role), 0, role)
        rows = ledger(self.audit)
        self.assertTrue(rows)
        self.assertEqual({row["role"] for row in rows}, {"model_probe"})
        self.assertTrue(all(row["status"] != "completed" and row["requested_model"] == SOL for row in rows))
        self.assertIn("not supported when using Codex", rows[-1]["error"])

        # 2.7.0 documented a re-pin to Terra here. 2.7.5 refuses it: every critique round runs on the shipped
        # route, and the host's account is changed instead (scripts/check_critic_access.py).
        repin = run_python(str(SCRIPTS / "repin_critic_route.py"), str(self.audit), "--critic-model", TERRA,
                           "--reason", f"this account cannot use {SOL}")
        self.assertEqual(repin.returncode, 2, repin.stdout + repin.stderr)
        self.assertIn("since 2.7.5 a re-pin can only move an audit to the shipped critic route", repin.stderr)
        self.assertEqual(frozen_critic_route(self.audit), (SOL, SOL, SOL), "a refused re-pin writes nothing")

        # the same round, from an account that can use Sol, finishes on Sol
        finished = run_driver(self.audit, self.state, "no-sol-2", "success")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        for role in ("stats", "data", "fairness", "completeness"):
            self.assertEqual(received(self.state, "models", role), [SOL], role)
        critics = [row for row in ledger(self.audit) if row["role"].startswith("critic_")]
        self.assertTrue(critics and all(row["requested_model"] == SOL and row["status"] == "completed"
                                        for row in critics))
        validate_ledger(self, self.audit)

    def test_negative_control_an_account_without_terra_does_not_stop_a_sol_audit(self) -> None:
        result = run_driver(self.audit, self.state, "no-terra", "success", FAKE_UNSUPPORTED_MODEL=TERRA)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for role in ORDINARY_ROLES:
            self.assertEqual(set(received(self.state, "models", role)), {SOL}, role)

    def test_a_repin_without_a_model_keeps_the_shipped_route(self) -> None:
        # INSTALL.md: without --critic-model the tool re-pins to the shipped route, which is Sol
        result = run_python(str(SCRIPTS / "repin_critic_route.py"), str(self.audit), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"already pinned to critics={SOL} adjudicator={SOL}; nothing to do", result.stdout)
        # 2.7.5: another model is refused, dry run or not
        dry = run_python(str(SCRIPTS / "repin_critic_route.py"), str(self.audit), "--dry-run", "--critic-model", TERRA)
        self.assertEqual(dry.returncode, 2, dry.stdout + dry.stderr)
        self.assertIn("since 2.7.5 a re-pin can only move an audit to the shipped critic route", dry.stderr)
        self.assertEqual(frozen_critic_route(self.audit), (SOL, SOL, SOL), "a refused dry run writes nothing")


# ------------------------------------------------------------------------------ finalize
class FinalizeOnTheFrozenRoute(unittest.TestCase):
    def test_the_finalize_self_test_finalizes_whichever_critic_route_the_audit_froze(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-270-finalize-") as directory:
            tree = pathlib.Path(directory) / "i4r-audit"
            shutil.copytree(SKILL, tree, ignore=IGNORED, symlinks=True)
            policy_file = tree / "config/pipeline-policy-v2.json"
            shipped_bytes = policy_file.read_bytes()
            # an audit frozen on Terra was initialised before 2.7.5, so it froze no critique.critic_route_lock (with the
            # Sol lock its Terra reviews would never validate: tests/test_275_critic_route_lock.py)
            terra = on_critic_route(json.loads(shipped_bytes), TERRA)
            terra["critique"].pop("critic_route_lock", None)
            terra_bytes = (json.dumps(terra, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            for route, other, policy_bytes in ((SOL, TERRA, shipped_bytes), (TERRA, SOL, terra_bytes)):
                with self.subTest(route=route):
                    policy_file.write_bytes(policy_bytes)
                    scratch = pathlib.Path(directory) / f"tmp-{route}"
                    scratch.mkdir()
                    result = subprocess.run(["bash", str(tree / "scripts/finalize_run.sh"), "--self-test"],
                                            text=True, capture_output=True, timeout=600, check=False,
                                            env=clean_env(CODEX=str(FAKE_CODEX), TMPDIR=str(scratch)))
                    output = result.stdout + result.stderr
                    self.assertEqual(result.returncode, 0, output[-6000:])
                    self.assertIn("SELF-TEST PASSED", output)
                    self.assertIn("ok: end-to-end init -> log_event -> finalize validates clean", output)
                    self.assertIn(f"ok: the critic rows follow the audit's frozen critic route ({route}); "
                                  f"rows on {other} are refused", output)
            thaw(pathlib.Path(directory))


if __name__ == "__main__":
    unittest.main()
