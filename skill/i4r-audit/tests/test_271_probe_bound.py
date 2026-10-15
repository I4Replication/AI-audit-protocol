"""2.7.1: the Phase-1 capability probe's token bound is a frozen per-audit value.

New audits freeze 350,000 normalized tokens; every audit initialised earlier froze 150,000 and keeps it for its
whole life. The bound is part of the probe contract, and the contract is hashed into the probe cache key
(``run_phase1.probe_key``), into the portable key every auditor's input identity and completion receipt is keyed
on (``portable_probe_key``), and through the probe key into every verifier input. So a legacy audit must get the
2.7.0 contract, keys and packet bytes exactly, or its cached probe and paid role completions would be re-keyed.

* ``LegacyContractIsThe270Contract`` -- ``probe_contract(150000)``, its hash, the probe keys, the portable keys
  and the ``PROBE_CONTRACT.json`` bytes are pinned to what 2.7.0 computes, and, against the 2.7.0 tree (imported
  by path in a fresh interpreter), equal to it; the 350,000 contract differs in ``maximum_normalized_tokens``
  alone. No function takes a default bound.
* ``SnapshotBound`` -- ``load_policy`` admits a snapshot whose runtime is the packaged one with either live bound
  and refuses any other bound (or a missing one), the never-shipped 300,000 and 500,000 among them, before any
  provider call, in ``run_phase1.py`` and in ``run_verifier.py``.
* ``ProbeRunsUnderTheAuditsBound`` -- with the fake CLI reporting a 200,000-token probe, a legacy audit pauses at
  the probe with the 2.7.0 message and a new audit passes, with its probe row's input hash and its nonce built on
  its own (350,000) contract, and the new audit's run reuses that cached probe; so do the worst probe the ledgers
  under runs/ recorded (254,191) and a 320,000-token one. A 400,000-token probe pauses a new audit too, with the
  350,000 message.
* ``AcrossVersions`` -- against the 2.7.0 tree: the same legacy audit probed by either tree at the same path gets
  the same probe key, packet bytes, prompt and input hashes, summary and pause message; a legacy audit's probe,
  five auditors and verifier call made by 2.7.0 replay on 2.7.1 with no new call, and 2.7.0 replays what 2.7.1
  recorded. A new audit's snapshot is refused by 2.7.0 before any call (the negative control).
* ``VerifierOfANewAudit`` -- the verifier keys and replays the 350,000 probe, and the auditors' receipts are keyed
  on the 350,000 portable key.
* ``InitPrintsTheFrozenBound`` -- init_run.sh prints the bound the snapshot froze and warns when a reused leftover's
  is not the shipped one; the full --force command it prints (INSTALL.md) keeps the audit id, DOI and title and
  re-freezes the shipped bound.
* ``DriftPinsTheShippedBound`` -- config/check_policy_drift.py pins both policy leaves and joins its literal to
  scripts/probe_bounds.py; its named negative control (the bound reverted to 150000) is rejected by each pin.

The 2.7.0 tree is ``I4R_BASE_270``, or ``../base`` beside this tree as a release scratch copy lays it out; the tests
that need it skip without one (and fail if I4R_BASE_270 names something else). Its modules only ever run in a
subprocess with bytecode writing off, so the tree is never written. The fake CLI never calls a provider.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
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
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import claude_routes  # noqa: E402
import fake_claude_phase1  # noqa: E402
import test_264_verifier_runtime as verifier_tests  # noqa: E402  (its Phase-1 audit fixture)
import test_270_claude_routes as route_tests  # noqa: E402  (minimal_audit)

SONNET_5 = claude_routes.SONNET_5
SONNET_5_5 = claude_routes.SONNET_5_5
LEGACY, SHIPPED = 150000, 350000
# Bounds considered for 2.7.1 and not shipped: no audit froze either, so each is refused like any other value.
NEVER_SHIPPED = (300000, 500000)
CLI = "2.1.206 (Fake Claude Code)"  # what tests/fake_claude_phase1.py prints for --version
PIN_ROOT = pathlib.Path("/i4r/pin/audit")
# What the 2.7.0 tree computes (its PROBE_CONTRACT constant; probe_key and portable_probe_key for CLI, PIN_ROOT).
PINNED_270 = {
    "contract_sha256": "74d36736a504a249cf9b962008a57ec11e44346cd00072b6b6b2cc8d791a6f70",
    "packet_sha256": "b41778b89fda245d9ae980a8542a0fc73f7e73f92ffe002fede29b340229853a",
    "probe_key:sonnet-5": "c476bb26732c6e9a7bf5cdd7aeda08e68bfbb7e228fd2f5f85c0ae9eafc8dcfa",
    "portable:sonnet-5": "00d3fe5aca6a3761e05c00bc8bd553045b4d9843df3565d6da1b31de37fd2aba",
    "probe_key:sonnet-5-5": "d5fc9eaa455e3307c8cf089b036ae8cbf480f99ae55abbcbf63ab17912e74ec8",
    "portable:sonnet-5-5": "a87a758dc519abd8ec0aa63916436300e77122b7813ef023252b6cbb9b4e44e6",
}
# The 2.7.0 pause line, prefix doubled as 2.7.0 prints it (RunnerError text inside the runner's own prefix).
LEGACY_PAUSE = ("PAUSED: Phase-1 capability probe failed: Phase-1 capability probe failed: capability probe exceeded "
                "150000 normalized tokens")
# The same line for a new audit, whose frozen bound is the shipped one.
NEW_PAUSE = ("PAUSED: Phase-1 capability probe failed: Phase-1 capability probe failed: capability probe exceeded "
             "350000 normalized tokens")
PROBE_TOTAL = fake_claude_phase1.PROBE_TOTAL_ENV
IGNORED = shutil.ignore_patterns(
    ".git", ".pytest_cache", "__pycache__", "_history*", "CHEATSHEET.pdf", ".DS_Store", "*.pyc")


def _module(name: str):
    """Resolve a scripts module at use: several suites swap sys.modules entries during discovery."""
    return importlib.import_module(name)


def _base_270() -> tuple[pathlib.Path | None, str | None]:
    named = os.environ.get("I4R_BASE_270")
    tree = pathlib.Path(named) if named else SKILL.parent / "base"
    try:
        version = (tree / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = None
    if version == "2.7.0" and (tree / "scripts/claude_routes.py").exists() \
            and not (tree / "scripts/probe_bounds.py").exists():
        return tree.resolve(), None
    return None, (f"I4R_BASE_270={named} is not a pristine 2.7.0 skill tree (VERSION {version!r})" if named else None)


BASE, BASE_PROBLEM = _base_270()
NO_BASE = "needs a pristine 2.7.0 skill tree: set I4R_BASE_270 (or keep one at ../base)"


def shipped_policy_bytes() -> bytes:
    return (SKILL / "config/pipeline-policy-v2.json").read_bytes()


def legacy_policy_bytes() -> bytes:
    """The policy an audit initialised on 2.7.0 froze: the 2.7.0 file itself when that tree is at hand, else the
    shipped file with both bound leaves at 150000 (test_the_legacy_policy_is_the_270_file proves they agree)."""
    if BASE is not None:
        return (BASE / "config/pipeline-policy-v2.json").read_bytes()
    return reconstructed_legacy_policy_bytes()


def reconstructed_legacy_policy_bytes() -> bytes:
    text = shipped_policy_bytes().decode("utf-8")
    for leaf in ('"probe_max_normalized_tokens": ', '"maximum_normalized_tokens": '):
        assert text.count(f"{leaf}{SHIPPED},") == 1, leaf
        text = text.replace(f"{leaf}{SHIPPED},", f"{leaf}{LEGACY},")
    # 2.7.5 added the critic-route lock; every audit before 2.7.5 froze none
    lock_leaf = '\n    "critic_route_lock": {\n      "model": "gpt-6.1-sol",\n      "since": "2.7.5"\n    },'
    assert text.count(lock_leaf) == 1
    text = text.replace(lock_leaf, "")
    # 2.7.4 added the critique ceiling leaf; every audit before 2.7.4 froze none
    critique_leaf = '\n    "provider_timeout_seconds": 5400,\n    "max_attempts_per_role_operator_ceiling"'
    assert text.count(critique_leaf) == 1
    text = text.replace(critique_leaf, '\n    "max_attempts_per_role_operator_ceiling"')
    rule_leaf = ',\n        "claim_change_rule": "substantive_specialists_1"'
    assert text.count(rule_leaf) == 1
    text = text.replace(rule_leaf, "")
    # 2.7.2 raised new audits' Phase-1 timeout; a 2.7.0 audit froze 2700 s in both leaves
    for leaf in ('"timeout_seconds": ', '"provider_timeout_seconds": '):
        assert text.count(f"{leaf}5400,") == 1, leaf
        text = text.replace(f"{leaf}5400,", f"{leaf}2700,")
    # 2.7.3 moved new audits' eight Claude entries (coordinator, five auditors, verifier, probe) to xhigh; 2.7.0 ran
    # them at max
    assert text.count('"reasoning_effort": "xhigh"') == 8
    text = text.replace('"reasoning_effort": "xhigh"', '"reasoning_effort": "max"')
    return text.encode("utf-8")


def on_shipped_claude_routes(policy: dict) -> dict:
    """``policy`` with its eight Claude entries at the shipped effort (2.7.3). A leftover must be on the shipped
    Claude routes for init to reach its probe-bound check: 2.7.0's own routes (Sonnet 5.5 at max) are an older
    skill's since 2.7.3, and init refuses such a leftover on its route first (tests/test_273_sonnet_effort.py).
    2.7.5: and with the shipped critic-route lock, since init refuses a leftover that predates the lock before its
    probe-bound check (tests/test_275_critic_route_lock.py)."""
    models = policy["models"]
    for entry in (models["coordinator"], models["post_fan_out_verifier"], *models["phase_1_auditors"].values(),
                  policy["phase_1"]["capability_probe"]):
        entry["reasoning_effort"] = claude_routes.SHIPPED["reasoning_effort"]
    policy["critique"]["critic_route_lock"] = json.loads(shipped_policy_bytes())["critique"]["critic_route_lock"]
    return policy


def policy_with_bound(bound, *, leaf=None) -> dict:
    """The shipped policy with phase_1.runtime.probe_max_normalized_tokens = ``bound`` (and the descriptive leaf)."""
    policy = json.loads(shipped_policy_bytes())
    policy["phase_1"]["runtime"]["probe_max_normalized_tokens"] = bound
    policy["phase_1"]["capability_probe"]["maximum_normalized_tokens"] = bound if leaf is None else leaf
    return policy


def policy_bytes(policy: dict) -> bytes:
    return (json.dumps(policy, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


BASE_IDENTITY = r'''
import json, pathlib, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1] + "/scripts")
import run_phase1, phase1_runtime as runtime, claude_routes
contract = run_phase1.PROBE_CONTRACT
out = {"contract": list(contract.items()), "canonical": runtime.canonical_bytes(contract).hex(),
       "pretty": runtime.pretty_bytes(contract).hex(), "keys": {}}
for cli, root in json.load(sys.stdin):
    for route in (claude_routes.SONNET_5, claude_routes.SONNET_5_5):
        key, components = run_phase1.probe_key(cli, route, pathlib.Path(root))
        out["keys"][f"{cli}|{root}|{route['requested_model']}"] = [
            key, components, run_phase1.portable_probe_key(cli, route)]
print(json.dumps(out))
'''
KEY_GRID = [[CLI, str(PIN_ROOT)], ["2.1.283 (Claude Code)", "/srv/i4r shared/runs/a b/audit"], ["1.0", "/"]]


def run_base(code: str, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-B", "-c", code, str(BASE), *args], input=stdin, text=True,
                          capture_output=True, check=False, cwd=str(BASE / "scripts"),
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


class BaseTreeIsWhatItSays(unittest.TestCase):
    def test_a_named_base_tree_is_a_pristine_270(self) -> None:
        if BASE_PROBLEM:
            self.fail(BASE_PROBLEM)
        if BASE is None:
            self.skipTest(NO_BASE)
        self.assertEqual((BASE / "VERSION").read_text().strip(), "2.7.0")


# ------------------------------------------------------------------------------ the contract and its keys
class LegacyContractIsThe270Contract(unittest.TestCase):
    def test_the_bounds_are_the_legacy_and_the_shipped_one(self) -> None:
        run_phase1, bounds = _module("run_phase1"), _module("probe_bounds")
        self.assertEqual(bounds.LIVE_PROBE_BOUNDS, (LEGACY, SHIPPED))
        self.assertEqual((bounds.LEGACY_PROBE_BOUND, bounds.SHIPPED_PROBE_BOUND), (LEGACY, SHIPPED))
        self.assertEqual((run_phase1.LIVE_PROBE_BOUNDS, run_phase1.LEGACY_PROBE_BOUND, run_phase1.SHIPPED_PROBE_BOUND),
                         (bounds.LIVE_PROBE_BOUNDS, LEGACY, SHIPPED))
        # the module constants of 2.7.0 are gone, so nothing can read a bound that is not the audit's
        self.assertFalse(hasattr(run_phase1, "PROBE_CONTRACT"))
        self.assertFalse(hasattr(run_phase1, "PROBE_MAX_TOKENS"))

    def test_the_legacy_contract_keys_and_packet_are_the_pinned_270_bytes(self) -> None:
        run_phase1, runtime = _module("run_phase1"), _module("phase1_runtime")
        contract = run_phase1.probe_contract(LEGACY)
        self.assertEqual(runtime.sha256_bytes(runtime.canonical_bytes(contract)), PINNED_270["contract_sha256"])
        self.assertEqual(runtime.sha256_bytes(runtime.pretty_bytes(contract)), PINNED_270["packet_sha256"])
        for route in (SONNET_5, SONNET_5_5):
            with self.subTest(route=route["requested_model"]):
                key, components = run_phase1.probe_key(CLI, route, PIN_ROOT, bound=LEGACY)
                self.assertEqual(key, PINNED_270[f"probe_key:{route['requested_model']}"])
                self.assertEqual(components["probe_contract_sha256"], PINNED_270["contract_sha256"])
                self.assertEqual(run_phase1.portable_probe_key(CLI, route, bound=LEGACY),
                                 PINNED_270[f"portable:{route['requested_model']}"])
        # and the 2.6.3 pin of the Sonnet 5 key, which every audit under runs/ was keyed with
        self.assertEqual(PINNED_270["probe_key:sonnet-5"], verifier_tests.PINNED_263["probe_key"])

    def test_a_new_contract_differs_in_the_bound_alone_and_keys_afresh(self) -> None:
        run_phase1, runtime = _module("run_phase1"), _module("phase1_runtime")
        legacy, new = run_phase1.probe_contract(LEGACY), run_phase1.probe_contract(SHIPPED)
        self.assertEqual(list(new), list(legacy), "same keys in the same order")
        self.assertEqual({key for key in new if new[key] != legacy[key]}, {"maximum_normalized_tokens"})
        self.assertEqual((legacy["maximum_normalized_tokens"], new["maximum_normalized_tokens"]), (LEGACY, SHIPPED))
        self.assertNotEqual(runtime.canonical_bytes(new), runtime.canonical_bytes(legacy))
        for route in (SONNET_5, SONNET_5_5):
            with self.subTest(route=route["requested_model"]):
                old_key, old_components = run_phase1.probe_key(CLI, route, PIN_ROOT, bound=LEGACY)
                new_key, new_components = run_phase1.probe_key(CLI, route, PIN_ROOT, bound=SHIPPED)
                self.assertNotEqual(new_key, old_key)
                self.assertEqual({key for key in new_components if new_components[key] != old_components[key]},
                                 {"probe_contract_sha256"})
                self.assertNotEqual(run_phase1.portable_probe_key(CLI, route, bound=SHIPPED),
                                    run_phase1.portable_probe_key(CLI, route, bound=LEGACY))

    def test_no_function_takes_a_default_bound(self) -> None:
        run_phase1 = _module("run_phase1")
        for call in (lambda: run_phase1.probe_key(CLI, SONNET_5_5, PIN_ROOT),
                     lambda: run_phase1.portable_probe_key(CLI, SONNET_5_5),
                     lambda: run_phase1.validate_probe_cache(PIN_ROOT, "0" * 64, {}),
                     lambda: run_phase1.run_capability_probe(PIN_ROOT, PIN_ROOT, PIN_ROOT, "claude", CLI,
                                                             SONNET_5_5, 300),
                     lambda: run_phase1.probe_contract()):
            with self.subTest(call=call):
                with self.assertRaises(TypeError):
                    call()
        for bound in (None, 0, 200000, 150001, *NEVER_SHIPPED, 349999, 350001, True, "150000", 150000.0, [150000]):
            with self.subTest(bound=bound):
                with self.assertRaisesRegex(run_phase1.RunnerError, "is not a packaged probe bound"):
                    run_phase1.probe_contract(bound)
                with self.assertRaisesRegex(run_phase1.RunnerError, "is not a packaged probe bound"):
                    run_phase1.probe_key(CLI, SONNET_5_5, PIN_ROOT, bound=bound)
        # negative control: each live bound is accepted
        for bound in (LEGACY, SHIPPED):
            self.assertEqual(run_phase1.probe_contract(bound)["maximum_normalized_tokens"], bound)

    def test_a_cached_probe_is_judged_against_the_audits_bound(self) -> None:
        run_phase1, runtime = _module("run_phase1"), _module("phase1_runtime")
        with tempfile.TemporaryDirectory(prefix="i4r-271-cache-") as directory:
            path = pathlib.Path(directory) / "probe.json"
            for bound, total, admitted in ((LEGACY, LEGACY, True), (LEGACY, 200000, False),
                                           (SHIPPED, 200000, True), (SHIPPED, SHIPPED + 1, False)):
                with self.subTest(bound=bound, total=total):
                    key, components = run_phase1.probe_key(CLI, SONNET_5_5, PIN_ROOT, bound=bound)
                    receipt = {"schema_version": "i4r-phase1-capability-probe-cache-1", "status": "passed",
                               "probe_key": key, "key_components": components, "normalized_total_tokens": total}
                    receipt["cache_record_sha256"] = runtime.sha256_bytes(runtime.canonical_bytes(receipt))
                    path.write_bytes(runtime.pretty_bytes(receipt))
                    if admitted:
                        self.assertEqual(run_phase1.validate_probe_cache(path, key, components, bound=bound), receipt)
                    else:
                        with self.assertRaisesRegex(run_phase1.RunnerError, "cached Phase-1 capability probe is invalid"):
                            run_phase1.validate_probe_cache(path, key, components, bound=bound)

    def test_the_legacy_contract_and_keys_equal_the_270_tree(self) -> None:
        if BASE is None:
            self.skipTest(NO_BASE)
        run_phase1, runtime = _module("run_phase1"), _module("phase1_runtime")
        result = run_base(BASE_IDENTITY, stdin=json.dumps(KEY_GRID))
        self.assertEqual(result.returncode, 0, result.stderr)
        base = json.loads(result.stdout)
        legacy = run_phase1.probe_contract(LEGACY)
        self.assertEqual(json.loads(json.dumps(list(legacy.items()))), base["contract"], "keys, order and values")
        self.assertEqual(runtime.canonical_bytes(legacy).hex(), base["canonical"])
        self.assertEqual(runtime.pretty_bytes(legacy).hex(), base["pretty"])
        self.assertEqual(runtime.sha256_bytes(bytes.fromhex(base["canonical"])), PINNED_270["contract_sha256"])
        self.assertEqual(runtime.sha256_bytes(bytes.fromhex(base["pretty"])), PINNED_270["packet_sha256"])
        for cli, root in KEY_GRID:
            for route in (SONNET_5, SONNET_5_5):
                label = f"{cli}|{root}|{route['requested_model']}"
                with self.subTest(key=label):
                    key, components = run_phase1.probe_key(cli, route, pathlib.Path(root), bound=LEGACY)
                    portable = run_phase1.portable_probe_key(cli, route, bound=LEGACY)
                    self.assertEqual([key, components, portable], base["keys"][label])
                    # negative control: the 2.7.1 bound is not what 2.7.0 keyed
                    self.assertNotEqual(run_phase1.probe_key(cli, route, pathlib.Path(root), bound=SHIPPED)[0],
                                        base["keys"][label][0])


# ------------------------------------------------------------------------------ the snapshot's bound
class SnapshotBound(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-271-bound-")
        self.base = pathlib.Path(self.temporary.name).resolve()
        self.fake = self.base / "fake-claude-phase1"
        shutil.copyfile(TESTS / "fake_claude_phase1.py", self.fake)
        self.fake.chmod(0o755)
        self.state = self.base / "fake-state.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def audit(self, name: str, policy: dict, *, metadata21: bool = False) -> pathlib.Path:
        return route_tests.minimal_audit(self.base, name, policy, metadata21=metadata21)

    def test_the_shipped_policy_freezes_the_shipped_bound(self) -> None:
        policy = json.loads(shipped_policy_bytes())
        self.assertEqual(policy["phase_1"]["runtime"]["probe_max_normalized_tokens"], SHIPPED)
        self.assertEqual(policy["phase_1"]["capability_probe"]["maximum_normalized_tokens"], SHIPPED)
        root = self.base / "init"
        root.mkdir()
        _module("record_invocation").initialize_policy_snapshot(root)
        frozen = _module("record_invocation").load_policy_snapshot(root)
        self.assertEqual(_module("run_phase1").probe_bound(frozen), SHIPPED)

    def test_load_policy_admits_each_live_bound_on_each_route(self) -> None:
        run_phase1 = _module("run_phase1")
        for bound in (LEGACY, SHIPPED):
            for route in (SONNET_5, SONNET_5_5):
                with self.subTest(bound=bound, route=route["requested_model"]):
                    policy = route_tests.route_policy(route)
                    policy["phase_1"]["runtime"]["probe_max_normalized_tokens"] = bound
                    root = self.audit(f"ok-{bound}-{route['requested_model']}", policy)
                    loaded, configured = run_phase1.load_policy(root)
                    self.assertEqual(run_phase1.probe_bound(loaded), bound)
                    self.assertEqual(type(run_phase1.probe_bound(loaded)), int)
                    self.assertEqual(set(configured), set(_module("phase1_runtime").ROLE_SPECS))

    def test_load_policy_refuses_any_other_bound_or_none(self) -> None:
        run_phase1 = _module("run_phase1")
        named = "audit-scoped phase_1.runtime differs from the packaged execution contract: its " \
                "probe_max_normalized_tokens {} is not a packaged probe bound (150000 or 350000)"
        generic = "audit-scoped phase_1.runtime differs from the packaged execution contract"
        missing = policy_with_bound(SHIPPED)
        del missing["phase_1"]["runtime"]["probe_max_normalized_tokens"]
        other_key = policy_with_bound(LEGACY)
        # 2.7.2: timeout_seconds is a frozen per-audit value with its own named refusal (test_272), so the drifted
        # key is another exact one
        other_key["phase_1"]["runtime"]["pre_dispatch_deadline_seconds"] = 59
        cases = [(policy_with_bound(value), named.format(repr(value)))
                 for value in (200000, 150001, *NEVER_SHIPPED, 349999, 350001, 600000, 0, "150000", True, None)]
        cases += [(missing, generic), (other_key, generic)]
        for index, (policy, message) in enumerate(cases):
            with self.subTest(case=message):
                root = self.audit(f"bad-{index}", policy)
                with self.assertRaises(run_phase1.RunnerError) as caught:
                    run_phase1.load_policy(root)
                # a missing bound, or another drifted key, gets the 2.7.0 refusal byte for byte
                self.assertEqual(str(caught.exception), message)

    def test_the_runners_refuse_it_before_any_call(self) -> None:
        env = {**os.environ, "FAKE_PHASE1_STATE": str(self.state), "PYTHONDONTWRITEBYTECODE": "1"}
        message = ("audit-scoped phase_1.runtime differs from the packaged execution contract: its "
                   "probe_max_normalized_tokens 200000 is not a packaged probe bound (150000 or 350000)")
        root = self.audit("phase1", policy_with_bound(200000))
        result = subprocess.run([sys.executable, str(SCRIPTS / "run_phase1.py"), str(root), "--claude-bin",
                                 str(self.fake), "--probe-only"], text=True, capture_output=True, env=env,
                                timeout=120, check=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"ERROR: {message}", result.stderr)
        verifier_root = self.audit("verifier", policy_with_bound(200000), metadata21=True)
        (verifier_root / "phase1").mkdir()
        journal = (verifier_root / "run.events.jsonl").read_bytes()
        result = subprocess.run([sys.executable, str(SCRIPTS / "run_verifier.py"), str(verifier_root), "--findings",
                                 "F13", "--claude-bin", str(self.fake)], text=True, capture_output=True, env=env,
                                timeout=120, check=False)
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.assertFalse(self.state.exists(), "no Claude call was started")
        self.assertEqual((verifier_root / "run.events.jsonl").read_bytes(), journal, "nothing was journaled")
        self.assertEqual((verifier_root / "invocations.jsonl").read_bytes(), b"")
        # Negative control: the same audit on a live bound gets past load_policy, to the local preflight,
        # which pauses it because a minimal audit has no deposit to freeze.
        ok = self.audit("phase1-ok", policy_with_bound(SHIPPED))
        result = subprocess.run([sys.executable, str(SCRIPTS / "run_phase1.py"), str(ok), "--claude-bin",
                                 str(self.fake), "--probe-only"], text=True, capture_output=True, env=env,
                                timeout=120, check=False)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("PAUSED: Phase-1 local preflight failed", result.stderr)
        self.assertNotIn("differs from the packaged execution contract", result.stderr)
        self.assertFalse(self.state.exists(), "still no Claude call")


# ------------------------------------------------------------------------------ the probe, run with the fake
class BoundAudit(verifier_tests.VerifierFixture):
    """test_264_verifier_runtime's Phase-1 audit, frozen from an explicit policy file (default: the shipped one)."""

    def setUp(self) -> None:
        super().setUp()
        self.policy = shipped_policy_bytes()

    def make_audit(self, name: str = "audit", *, metadata21: bool = True,
                   audit_id: str | None = None) -> pathlib.Path:
        path = self.base / f"policy-{name}.json"
        path.write_bytes(self.policy)
        modules = {id(module): module for module in (_module("record_invocation"), verifier_tests.record_invocation)}
        with contextlib.ExitStack() as stack:
            for module in modules.values():
                stack.enter_context(mock.patch.object(module, "POLICY", path))
            return super().make_audit(name, metadata21=metadata21, audit_id=audit_id)

    def audit_with(self, policy: bytes, name: str, *, phase1: bool = False) -> pathlib.Path:
        self.policy = policy
        root = self.audit_after_phase1(name) if phase1 else self.make_audit(name)
        return root

    def bound_of(self, root: pathlib.Path) -> int:
        return _module("run_phase1").probe_bound(_module("record_invocation").load_policy_snapshot(root))

    def run_tree(self, tree: pathlib.Path, script: str, root: pathlib.Path, *args: str, scenario: str = "success",
                 target: str = "auditor_reproduction", extra_env: dict[str, str] | None = None,
                 ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(tree / "scripts" / script), str(root), *args, "--claude-bin", str(self.fake)],
            text=True, capture_output=True, env=self.env(scenario, target, {"PYTHONDONTWRITEBYTECODE": "1",
                                                                            **(extra_env or {})}),
            timeout=180, check=False,
        )

    def starts_in(self, root: pathlib.Path) -> list[dict]:
        return [row for row in self.starts() if row["cwd"].startswith(str(root) + os.sep)]

    def probe_rows(self, root: pathlib.Path) -> list[dict]:
        return [row for row in self.ledger(root) if row.get("role") == "phase1_capability_probe"]

    def only(self, paths) -> pathlib.Path:
        paths = sorted(paths)
        self.assertEqual(len(paths), 1, paths)
        return paths[0]

    def packet_bytes(self, root: pathlib.Path) -> bytes:
        return self.only((root / "phase1/capability_probes").glob("*/packet/PROBE_CONTRACT.json")).read_bytes()

    def remove(self, root: pathlib.Path) -> None:
        for path in [root, *root.rglob("*")]:
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)
        shutil.rmtree(root)


class ProbeRunsUnderTheAuditsBound(BoundAudit):
    def probe_only(self, root: pathlib.Path, total: int | None) -> subprocess.CompletedProcess[str]:
        return self.run_tree(SKILL, "run_phase1.py", root, "--probe-only",
                             extra_env={PROBE_TOTAL: str(total)} if total is not None else None)

    def assert_probe_identity_on_the_audits_contract(self, root: pathlib.Path, key: str, bound: int) -> None:
        """The probe's nonce and its ledger row's input hash are built on the audit's own contract (2.7.1 review,
        mutants M4b and M5: either built on another bound's contract passed every suite, since the probe key
        already hashes the contract). Recomputed here from probe_contract(bound), as run_capability_probe does."""
        run_phase1, runtime = _module("run_phase1"), _module("phase1_runtime")
        first = next(iter(runtime.ROLE_SPECS))
        route = claude_routes.project(_module("record_invocation").load_policy_snapshot(root)["models"]
                                      ["phase_1_auditors"][first])
        self.assertEqual(key, run_phase1.probe_key(CLI, route, root, bound=bound)[0])

        def identity(contract_bound: int) -> tuple[str, str]:
            contract = run_phase1.probe_contract(contract_bound)
            nonce = runtime.sha256_bytes(runtime.canonical_bytes({"probe_key": key, "contract": contract}))
            return nonce, runtime.sha256_bytes(runtime.canonical_bytes(
                {"key": key, "nonce": nonce, "probe_contract": contract}))

        nonce, input_hash = identity(bound)
        self.assertEqual((root / "phase1/capability_probes" / key / "source/nonce.txt").read_text(encoding="utf-8"),
                         nonce + "\n")
        [row] = [row for row in self.probe_rows(root) if row.get("provider_call")]
        self.assertEqual(row["input_artifact_sha256"], input_hash)
        # negative control: the other live bound's contract gives another nonce and input hash
        other = next(value for value in (LEGACY, SHIPPED) if value != bound)
        self.assertNotEqual(identity(other)[0], nonce)
        self.assertNotEqual(identity(other)[1], input_hash)

    def test_the_worst_observed_and_a_320k_probe_pass_a_new_audit_and_pause_a_legacy_one(self) -> None:
        # 254,191: the largest probe the ledgers under runs/ recorded (it paused a 150,000 audit on 2026-09-25).
        # 320,000: well above it and still inside the 350,000 bound.
        for total in (254191, 320000):
            with self.subTest(total=total):
                legacy = self.audit_with(legacy_policy_bytes(), f"legacy-{total}")
                result = self.probe_only(legacy, total)
                self.assertEqual(result.returncode, 4, result.stderr + result.stdout)
                self.assertEqual(result.stderr.strip().splitlines()[-1], LEGACY_PAUSE)
                self.assertEqual(json.loads((legacy / "phase1/status.json").read_text())["pause_reason"],
                                 "capability_probe_failed")
                new = self.audit_with(shipped_policy_bytes(), f"new-{total}")
                result = self.probe_only(new, total)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                summary = json.loads(result.stdout)
                self.assertEqual((summary["status"], summary["normalized_total_tokens"],
                                  summary["probe_max_normalized_tokens"]), ("probe_only_complete", total, SHIPPED))
                self.assert_probe_identity_on_the_audits_contract(new, summary["probe_key"], SHIPPED)

    def test_a_400k_probe_pauses_a_new_audit_too_with_its_own_bound(self) -> None:
        # Above the 350,000 bound: a new audit pauses as a legacy one does, and the message names its own bound.
        legacy = self.audit_with(legacy_policy_bytes(), "legacy-400k")
        result = self.probe_only(legacy, 400000)
        self.assertEqual(result.returncode, 4, result.stderr + result.stdout)
        self.assertEqual(result.stderr.strip().splitlines()[-1], LEGACY_PAUSE)
        new = self.audit_with(shipped_policy_bytes(), "new-400k")
        self.assertEqual(self.bound_of(new), SHIPPED)
        result = self.probe_only(new, 400000)
        self.assertEqual(result.returncode, 4, result.stderr + result.stdout)
        self.assertEqual(result.stderr.strip().splitlines()[-1], NEW_PAUSE)
        self.assertNotIn("150000", result.stderr)
        status = json.loads((new / "phase1/status.json").read_text())
        self.assertEqual((status["status"], status["pause_reason"], status["auditor_inferences_started"]),
                         ("paused", "capability_probe_failed", 0))
        [row] = self.probe_rows(new)
        self.assertEqual((row["status"], row["error"], row["usage"]["total_tokens"]),
                         ("failed", "capability probe exceeded 350000 normalized tokens", 400000))
        self.assertEqual(list((new / "phase1/probe_cache").glob("*.json")), [], "a failed probe is never cached")
        self.assertEqual([row["role"] for row in self.starts_in(new)], ["phase1_capability_probe"])

    def test_the_fake_reports_the_total_it_is_given(self) -> None:
        # the knob the tests below rely on: the runner's own normalization sees exactly the requested total
        root = self.audit_with(shipped_policy_bytes(), "knob")
        result = self.probe_only(root, 123456)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["normalized_total_tokens"], 123456)
        self.assertEqual(self.probe_rows(root)[-1]["usage"]["total_tokens"], 123456)
        other = self.audit_with(shipped_policy_bytes(), "unset")
        result = self.probe_only(other, None)
        self.assertEqual(json.loads(result.stdout)["normalized_total_tokens"], 170, "unset: what it always reported")

    def test_a_200k_probe_pauses_a_legacy_audit_and_passes_a_new_one(self) -> None:
        legacy = self.audit_with(legacy_policy_bytes(), "legacy")
        self.assertEqual(self.bound_of(legacy), LEGACY)
        result = self.probe_only(legacy, 200000)
        self.assertEqual(result.returncode, 4, result.stderr + result.stdout)
        self.assertEqual(result.stderr.strip().splitlines()[-1], LEGACY_PAUSE)
        status = json.loads((legacy / "phase1/status.json").read_text())
        self.assertEqual((status["status"], status["pause_reason"], status["auditor_inferences_started"]),
                         ("paused", "capability_probe_failed", 0))
        [row] = self.probe_rows(legacy)
        self.assertEqual((row["status"], row["error"]), ("failed", "capability probe exceeded 150000 normalized tokens"))
        self.assertEqual(list((legacy / "phase1/probe_cache").glob("*.json")), [], "a failed probe is never cached")
        self.assertEqual([row["role"] for row in self.starts_in(legacy)], ["phase1_capability_probe"])

        new = self.audit_with(shipped_policy_bytes(), "new")
        self.assertEqual(self.bound_of(new), SHIPPED)
        result = self.probe_only(new, 200000)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        summary = json.loads(result.stdout)
        self.assertEqual((summary["status"], summary["normalized_total_tokens"], summary["probe_max_normalized_tokens"]),
                         ("probe_only_complete", 200000, SHIPPED))
        self.assertEqual(json.loads(self.packet_bytes(new))["maximum_normalized_tokens"], SHIPPED)
        self.assertEqual(self.packet_bytes(new), _module("phase1_runtime").pretty_bytes(
            _module("run_phase1").probe_contract(SHIPPED)))
        self.assert_probe_identity_on_the_audits_contract(new, summary["probe_key"], SHIPPED)
        # The full run reuses that cached 200,000-token probe: the cache is validated against the audit's bound.
        probes = len([row for row in self.starts_in(new) if row["role"] == "phase1_capability_probe"])
        full = self.run_tree(SKILL, "run_phase1.py", new, scenario="slow_parallel")
        self.assertEqual(full.returncode, 0, full.stderr + full.stdout)
        self.assertEqual(len([row for row in self.starts_in(new) if row["role"] == "phase1_capability_probe"]), probes)
        self.assertTrue(json.loads(full.stdout)["probe_reused"])

    def test_the_bound_is_inclusive_on_either_audit(self) -> None:
        # total > bound fails, total == bound passes, exactly as 2.7.0 judged 150000
        for policy, bound in ((legacy_policy_bytes(), LEGACY), (shipped_policy_bytes(), SHIPPED)):
            for total, code in ((bound, 0), (bound + 1, 4)):
                with self.subTest(bound=bound, total=total):
                    root = self.audit_with(policy, f"edge-{bound}-{total}")
                    result = self.probe_only(root, total)
                    self.assertEqual(result.returncode, code, result.stderr + result.stdout)
                    if code:
                        self.assertIn(f"capability probe exceeded {bound} normalized tokens", result.stderr)
                    else:
                        self.assertEqual(json.loads(result.stdout)["probe_max_normalized_tokens"], bound)

    def test_a_legacy_audit_prints_and_keys_the_legacy_bound(self) -> None:
        legacy = self.audit_with(legacy_policy_bytes(), "legacy-ok")
        result = self.probe_only(legacy, None)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["probe_max_normalized_tokens"], LEGACY)
        route = claude_routes.project(_module("record_invocation").load_policy_snapshot(legacy)["models"]
                                      ["phase_1_auditors"]["auditor_reproduction"])
        self.assertEqual(summary["probe_key"], _module("run_phase1").probe_key(CLI, route, legacy, bound=LEGACY)[0])
        self.assertNotEqual(summary["probe_key"],
                            _module("run_phase1").probe_key(CLI, route, legacy, bound=SHIPPED)[0])
        self.assertEqual(_module("phase1_runtime").sha256_bytes(self.packet_bytes(legacy)), PINNED_270["packet_sha256"])


# ------------------------------------------------------------------------------ what an operator can do
class Remedies(BoundAudit):
    """INSTALL.md, *Upgrading in-flight audits to 2.7.1*: an existing audit whose probe exceeds 150,000 still
    pauses; re-running the probe is free of any attempt budget, and init_run.sh --force re-freezes 350,000."""

    def test_a_paused_legacy_audit_continues_when_a_retried_probe_fits(self) -> None:
        legacy = self.audit_with(legacy_policy_bytes(), "retry")
        for _ in range(3):  # a probe that keeps running over is paused every time, and costs no auditor attempt
            paused = self.run_tree(SKILL, "run_phase1.py", legacy, extra_env={PROBE_TOTAL: "150001"})
            self.assertEqual(paused.returncode, 4, paused.stderr + paused.stdout)
        done = self.run_tree(SKILL, "run_phase1.py", legacy, scenario="slow_parallel", extra_env={PROBE_TOTAL: "149259"})
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertEqual(json.loads(done.stdout)["status"], "quorum_complete")
        self.assertEqual([(row["status"], row["usage"]["total_tokens"]) for row in self.probe_rows(legacy)
                          if row.get("provider_call")], [("failed", 150001)] * 3 + [("completed", 149259)])
        auditors = [row for row in self.ledger(legacy) if row.get("role_class") == "auditor" and row.get("provider_call")]
        self.assertEqual([(row["status"], row["attempt"]) for row in auditors], [("completed", 1)] * 5)
        events = [event["type"] for event in _module("lifecycle_journal").load_events(legacy)
                  if event["type"] in ("pause_start", "pause_end")]
        self.assertEqual(events, ["pause_start", "pause_end"] * 3)
        self.assertEqual(self.bound_of(legacy), LEGACY, "the audit keeps its frozen bound")

    def test_force_refreezes_the_shipped_bound(self) -> None:
        init_tests = _module("test_270_policy_and_init")
        legacy_policy = json.loads(legacy_policy_bytes())
        for forced, code in ((False, 2), (True, 0)):
            with self.subTest(force=forced):
                audit = init_tests.make_audit(self.base / f"init-{forced}" / "audit")
                data = init_tests.envelope_bytes(legacy_policy)
                (audit / init_tests.SNAPSHOT).write_bytes(data)
                (audit / "run.json").write_text("{}\n", encoding="utf-8")
                result = init_tests.init(audit, *(["--force"] if forced else []))
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                if not forced:  # negative control: without --force the audit keeps its snapshot, and its bound
                    self.assertEqual((audit / init_tests.SNAPSHOT).read_bytes(), data)
                    continue
                self.assertEqual(self.bound_of(audit), SHIPPED)
                [archived] = audit.glob("pipeline-policy.snapshot.previous.*.json")
                self.assertEqual(archived.read_bytes(), data)


# ------------------------------------------------------------------------------ what init prints
class InitPrintsTheFrozenBound(unittest.TestCase):
    """2.7.1 review (newbound-3, newbound-4). init_run.sh reuses a snapshot left without a run.json when its Claude
    routes are the shipped ones, so a leftover of 2.7.0's record_invocation.py --init-policy gives a new audit the
    150,000 bound; init now prints the bound the snapshot froze and warns when it is not the shipped one. The
    --force command it prints (and INSTALL.md gives) passes the audit id, DOI and title again, since a forced
    re-init rebuilds run.json from its arguments, and re-freezes the whole shipped policy."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-271-init-")
        self.base = pathlib.Path(self.temporary.name).resolve()
        self.init_tests = _module("test_270_policy_and_init")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_init(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/bin/bash", str(SCRIPTS / "init_run.sh"), *argv], text=True, capture_output=True,
                              timeout=300, env=self.init_tests.clean_env())

    def bound_of(self, audit: pathlib.Path) -> int:
        return _module("run_phase1").probe_bound(_module("record_invocation").load_policy_snapshot(audit))

    def test_a_new_audit_prints_the_shipped_bound_and_no_warning(self) -> None:
        audit = self.init_tests.make_audit(self.base / "new" / "audit")
        result = self.run_init(str(audit), "--audit-id", "t-271-new")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"\n  probe bound:       {SHIPPED} normalized tokens\n", result.stdout)
        self.assertNotIn("probe bound of", result.stdout + result.stderr)
        self.assertEqual(self.bound_of(audit), SHIPPED)

    def test_a_reused_legacy_leftover_is_printed_and_warned_and_the_printed_force_refreezes(self) -> None:
        audit = self.init_tests.make_audit(self.base / "leftover" / "audit")
        # what 2.7.0's record_invocation.py --init-policy froze: the shipped routes with the 150,000 bound (2.7.3: on
        # the shipped Claude effort, so that the bound is the leftover's only difference that init weighs)
        data = self.init_tests.envelope_bytes(on_shipped_claude_routes(json.loads(legacy_policy_bytes())))
        (audit / self.init_tests.SNAPSHOT).write_bytes(data)
        title = "A title: with 'quotes' and $HOME"
        result = self.run_init(str(audit), "--audit-id", "t-271-leftover", "--doi", "10.1234/abc.5678",
                               "--paper-title", title)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((audit / self.init_tests.SNAPSHOT).read_bytes(), data, "the leftover is reused as it was")
        self.assertEqual(self.bound_of(audit), LEGACY)
        self.assertIn(f"\n  probe bound:       {LEGACY} normalized tokens\n", result.stdout)
        self.assertIn("WARNING: this init reused the policy snapshot left in the audit directory", result.stderr)
        self.assertIn(f"That snapshot froze a Phase-1 probe bound of {LEGACY} normalized\n", result.stderr)
        self.assertIn(f"tokens, not the shipped {SHIPPED}.", result.stderr)
        # The remedy it prints is the full command, with this audit's own values; run it exactly as printed.
        [line] = [line for line in result.stderr.splitlines() if line.lstrip().startswith("bash ")]
        argv = shlex.split(line)
        self.assertEqual(argv[:3], ["bash", str(SCRIPTS / "init_run.sh"), str(audit)])
        self.assertEqual(argv[3:], ["--force", "--audit-id", "t-271-leftover", "--doi", "10.1234/abc.5678",
                                    "--paper-title", title])
        forced = self.run_init(*argv[2:])
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
        self.assertIn(f"\n  probe bound:       {SHIPPED} normalized tokens\n", forced.stdout)
        self.assertNotIn("probe bound of", forced.stdout + forced.stderr)
        run = json.loads((audit / "run.json").read_text(encoding="utf-8"))
        self.assertEqual((run["audit_id"], run["paper_doi"], run["paper_title"]),
                         ("t-271-leftover", "10.1234/abc.5678", title))
        self.assertEqual(self.bound_of(audit), SHIPPED)
        [archived] = audit.glob("pipeline-policy.snapshot.previous.*.json")
        self.assertEqual(archived.read_bytes(), data)
        # negative control: without the three flags a forced re-init rebuilds run.json without the DOI and title
        time.sleep(1.1)  # --force refuses to overwrite an archive made in the same second
        bare = self.run_init(str(audit), "--force")
        self.assertEqual(bare.returncode, 0, bare.stdout + bare.stderr)
        run = json.loads((audit / "run.json").read_text(encoding="utf-8"))
        self.assertEqual((run["paper_doi"], run["paper_title"]), (None, None))
        self.assertNotEqual(run["audit_id"], "t-271-leftover")

    def test_a_leftover_with_a_bound_no_runner_admits_is_refused_not_reused(self) -> None:
        # 2.7.1 re-check F1: such a leftover (a candidate build's 300,000 or 500,000 on the shipped routes) was reused
        # and init said the audit keeps that bound for life, but run_phase1.py and run_verifier.py refuse it.
        for bound in (*NEVER_SHIPPED, 349999):
            with self.subTest(bound=bound):
                audit = self.init_tests.make_audit(self.base / f"unrun-{bound}" / "audit")
                policy = on_shipped_claude_routes(json.loads(legacy_policy_bytes()))
                policy["phase_1"]["runtime"]["probe_max_normalized_tokens"] = bound
                policy["phase_1"]["capability_probe"]["maximum_normalized_tokens"] = bound
                data = self.init_tests.envelope_bytes(policy)
                snapshot = audit / self.init_tests.SNAPSHOT
                snapshot.write_bytes(data)
                result = self.run_init(str(audit), "--audit-id", f"t-271-unrun-{bound}")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(f"probe_max_normalized_tokens {bound} is not a packaged probe bound "
                              f"({LEGACY} or {SHIPPED})", result.stderr)
                self.assertIn("remove it (rm ", result.stderr)
                self.assertEqual(snapshot.read_bytes(), data, "the leftover is left untouched")
                self.assertFalse((audit / "run.json").exists())
                self.assertFalse((audit / "run.events.jsonl").exists())
        # negative control: the same leftover on a live bound (the legacy 150,000) is reused, as above
        audit = self.init_tests.make_audit(self.base / "unrun-live" / "audit")
        (audit / self.init_tests.SNAPSHOT).write_bytes(
            self.init_tests.envelope_bytes(on_shipped_claude_routes(json.loads(legacy_policy_bytes()))))
        result = self.run_init(str(audit), "--audit-id", "t-271-unrun-live")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.bound_of(audit), LEGACY)


# ------------------------------------------------------------------------------ across 2.7.0 and 2.7.1
@unittest.skipIf(BASE is None, NO_BASE)
class AcrossVersions(BoundAudit):
    STABLE = ("role", "status", "attempt_kind", "requested_model", "reasoning_effort", "prompt_sha256",
              "input_artifact_sha256", "error", "error_class", "observed_model", "served_model")

    def test_the_legacy_policy_is_the_270_file(self) -> None:
        self.assertEqual(reconstructed_legacy_policy_bytes(), (BASE / "config/pipeline-policy-v2.json").read_bytes())

    def probe_at_one_path(self, tree: pathlib.Path, total: int | None) -> dict:
        """A fresh legacy audit at the same path, probed by ``tree``: everything the probe's identity touches."""
        root = self.base / "same-path"
        if root.exists():
            self.remove(root)
        self.audit_with(legacy_policy_bytes(), "same-path")
        result = self.run_tree(tree, "run_phase1.py", root, "--probe-only",
                               extra_env={PROBE_TOTAL: str(total)} if total is not None else None)
        [row] = self.probe_rows(root)
        caches = sorted((root / "phase1/probe_cache").glob("*.json"))
        return {
            "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr.strip().splitlines(),
            "row": {key: row.get(key) for key in self.STABLE},
            "packet": self.packet_bytes(root),
            "packet_dir": [path.parent.parent.name for path in (root / "phase1/capability_probes").glob("*/packet")],
            "cache": [(path.name, {key: value for key, value in json.loads(path.read_text()).items()
                                   if key in ("probe_key", "key_components", "normalized_total_tokens", "status")})
                      for path in caches],
            "snapshot": (root / "pipeline-policy.snapshot.json").read_bytes(),
        }

    def test_both_trees_probe_a_legacy_audit_identically(self) -> None:
        for total in (None, 200000):
            with self.subTest(total=total):
                old = self.probe_at_one_path(BASE, total)
                new = self.probe_at_one_path(SKILL, total)
                self.assertEqual(new, old)
                self.assertEqual(_module("phase1_runtime").sha256_bytes(new["packet"]), PINNED_270["packet_sha256"])
                if total is None:
                    self.assertEqual(new["returncode"], 0)
                    self.assertEqual(json.loads(new["stdout"])["probe_max_normalized_tokens"], LEGACY)
                else:
                    self.assertEqual((new["returncode"], new["stderr"][-1]), (4, LEGACY_PAUSE))
                    self.assertEqual(new["cache"], [])

    def test_a_legacy_audit_recorded_by_270_replays_on_271(self) -> None:
        root = self.audit_with(legacy_policy_bytes(), "legacy-270")
        done = self.run_tree(BASE, "run_phase1.py", root, scenario="slow_parallel")
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.write_registry(root)
        verified = self.run_tree(BASE, "run_verifier.py", root, "--findings", "F13", target="verifier")
        self.assertEqual(verified.returncode, 0, verified.stderr + verified.stdout)
        starts = len(self.starts_in(root))
        self.assertEqual(starts, 7, "2.7.0 made the probe, five auditor and one verifier calls")
        recorded = len(self.ledger(root))
        again = self.run_tree(SKILL, "run_phase1.py", root)
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertEqual(len(self.starts_in(root)), starts, "2.7.1 re-dispatches no Phase-1 call")
        replays = self.ledger(root)[recorded:]
        self.assertEqual(sorted(row["role"] for row in replays),
                         sorted(["phase1_capability_probe", *_module("phase1_runtime").ROLE_SPECS]))
        self.assertTrue(all(row.get("attempt_kind") == "cache_replay" for row in replays), replays)
        replay = self.run_tree(SKILL, "run_verifier.py", root, "--findings", "F13", target="verifier")
        self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)
        self.assertEqual(json.loads(replay.stdout)["status"], "replayed")
        self.assertEqual(len(self.starts_in(root)), starts, "nor the verifier call")

    def test_270_replays_what_271_recorded_on_a_legacy_audit(self) -> None:
        root = self.audit_with(legacy_policy_bytes(), "legacy-271", phase1=True)
        verified = self.run_verifier(root, ["F13"])
        self.assertEqual(verified.returncode, 0, verified.stderr + verified.stdout)
        starts = len(self.starts_in(root))
        self.assertEqual(starts, 7)
        again = self.run_tree(BASE, "run_phase1.py", root)
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        replay = self.run_tree(BASE, "run_verifier.py", root, "--findings", "F13", target="verifier")
        self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)
        self.assertEqual(json.loads(replay.stdout)["status"], "replayed")
        self.assertEqual(len(self.starts_in(root)), starts, "2.7.0 re-dispatches nothing 2.7.1 recorded")

    def test_270_refuses_a_new_audit_before_any_call(self) -> None:
        # Negative control for the two replays: a 2.7.1 audit's snapshot (350,000) is not one 2.7.0 admits, so a
        # new audit cannot be continued on 2.7.0 (INSTALL.md, mixed teams).
        new = self.audit_with(shipped_policy_bytes(), "new-on-270")
        for script, args in (("run_phase1.py", ()), ("run_verifier.py", ("--findings", "F13"))):
            with self.subTest(script=script):
                refused = self.run_tree(BASE, script, new, *args)
                self.assertNotEqual(refused.returncode, 0, refused.stderr + refused.stdout)
                self.assertIn("audit-scoped phase_1.runtime differs from the packaged execution contract",
                              refused.stderr + refused.stdout)
        self.assertEqual(self.starts_in(new), [])


# ------------------------------------------------------------------------------ the verifier of a new audit
class VerifierOfANewAudit(BoundAudit):
    def test_the_verifier_keys_and_replays_the_350k_probe(self) -> None:
        run_phase1, runtime = _module("run_phase1"), _module("phase1_runtime")
        root = self.audit_with(shipped_policy_bytes(), "new", phase1=True)
        route = claude_routes.project(_module("record_invocation").load_policy_snapshot(root)["models"]
                                      ["post_fan_out_verifier"])
        new_key = run_phase1.probe_key(CLI, route, root, bound=SHIPPED)[0]
        old_key = run_phase1.probe_key(CLI, route, root, bound=LEGACY)[0]
        self.assertEqual([path.stem for path in (root / "phase1/probe_cache").glob("*.json")], [new_key])
        self.assertEqual(json.loads(self.packet_bytes(root))["maximum_normalized_tokens"], SHIPPED)
        # The auditors' completions are keyed on the 350,000 portable key (role_input_hash of packet, prompt, key).
        portable = run_phase1.portable_probe_key(CLI, route, bound=SHIPPED)
        legacy_portable = run_phase1.portable_probe_key(CLI, route, bound=LEGACY)
        for role in runtime.ROLE_SPECS:
            with self.subTest(role=role):
                [receipt] = (root / "phase1/completions" / role).glob("*.json")
                packet = json.loads(self.only((root / "phase1/role_packets").glob(f"*/{role}/*/ROLE_PACKET.json"))
                                    .read_text())
                prompt = self.only((root / "phase1/roles" / role).glob("*/attempt_*/prompt.txt")).read_text()
                self.assertEqual(receipt.stem, runtime.role_input_hash(packet, prompt, portable))
                self.assertNotEqual(receipt.stem, runtime.role_input_hash(packet, prompt, legacy_portable))
        # The verifier plans on the audit's own bound ...
        dry = self.run_verifier(root, ["F13"], extra_args=["--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr + dry.stdout)
        self.assertEqual(json.loads(dry.stdout)["probe_key"], new_key)
        self.assertNotEqual(new_key, old_key)
        # ... and reuses the auditors' probe: one new call, the verifier's, and a probe cache replay.
        probes = [row for row in self.starts_in(root) if row["role"] == "phase1_capability_probe"]
        self.assertEqual(len(probes), 1)
        before = len(self.ledger(root))
        result = self.run_verifier(root, ["F13"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual([row["role"] for row in self.starts_in(root)][-1:], [self.role_for("F13")])
        self.assertEqual(len([row for row in self.starts_in(root) if row["role"] == "phase1_capability_probe"]), 1,
                         "the verifier did not re-probe")
        replayed = [row for row in self.ledger(root)[before:] if row.get("role") == "phase1_capability_probe"]
        self.assertEqual([(row["attempt_kind"], row["input_artifact_sha256"]) for row in replayed],
                         [("cache_replay", new_key)])
        # The verifier's input identity is the one it planned on the 350,000 key.
        _path, receipt = self.receipt(root, self.role_for("F13"))
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["input_sha256"], json.loads(dry.stdout)["input_sha256"])

    def test_a_plan_without_the_audits_bound_is_refused_not_rekeyed(self) -> None:
        run_verifier, run_phase1 = _module("run_verifier"), _module("run_phase1")
        root = self.audit_with(shipped_policy_bytes(), "plan", phase1=True)
        self.write_registry(root)
        args = argparse.Namespace(timeout_seconds=None, max_budget_usd="15")  # 2.7.2: None = the audit's own
        plan = run_verifier.plan_batch(root, root / "phase1", ["F13"], args)
        self.assertEqual(plan.probe_bound, SHIPPED)
        required = ("root", "phase_root", "audit_id", "route", "snapshot", "snapshot_manifest", "hypotheses", "role",
                    "packet", "prompt", "packet_hash")
        bare = run_verifier.BatchPlan(**{name: getattr(plan, name) for name in required})
        self.assertIsNone(bare.probe_bound, "a plan not built by plan_batch has no bound, never the shipped one")
        plan.probe_bound = None
        # run_verifier's own run_phase1 (several suites swap sys.modules entries during discovery)
        refusal = run_verifier.run_phase1.RunnerError
        for candidate in (plan, bare):
            deadline = _module("phase1_runtime").MonotonicDeadline.after(60)
            with self.assertRaisesRegex(refusal, "None is not a packaged probe bound"):
                run_verifier.bind_inputs(candidate, str(self.fake), deadline)
            self.assertIsNone(candidate.probe_key)


# ------------------------------------------------------------------------------ the drift check
def load_checker(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(f"i4r_271_drift_{abs(hash(str(path)))}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DriftPinsTheShippedBound(unittest.TestCase):
    RUNTIME_PIN = "the Phase-1 probe bound phase_1.runtime.probe_max_normalized_tokens is"
    LEAF_PIN = "the capability-probe leaf maximum_normalized_tokens is"
    CONTROL = "Phase-1 probe bound restored to 150000"

    def test_the_checker_literal_is_the_shipped_bound(self) -> None:
        checker = load_checker(SKILL / "config/check_policy_drift.py")
        self.assertEqual((checker.SHIPPED_PROBE_BOUND, checker.LEGACY_PROBE_BOUND), (SHIPPED, LEGACY))
        self.assertNotIn("phase_1.capability_probe.maximum_normalized_tokens", checker.FREE_LEAVES)

    def test_the_named_control_is_rejected_by_each_pin(self) -> None:
        checker = load_checker(SKILL / "config/check_policy_drift.py")
        found: list[str] = []
        self.assertEqual(checker.check_negative_controls(checker.load_policy(), found), 20)
        self.assertEqual(found, [], "negative control: the unmodified checker")
        for dropped in (self.RUNTIME_PIN, self.LEAF_PIN):
            with self.subTest(loosened=dropped):
                module = load_checker(SKILL / "config/check_policy_drift.py")
                strict = module.check_policy_shape

                def loosened(candidate: dict, errors: list[str], strict=strict, dropped=dropped) -> None:
                    raised: list[str] = []
                    strict(candidate, raised)
                    errors.extend(error for error in raised if not error.startswith(dropped))
                module.check_policy_shape = loosened
                found = []
                module.check_negative_controls(module.load_policy(), found)
                self.assertEqual(len(found), 1, found)
                self.assertTrue(found[0].startswith(f"negative control was not rejected by its pin: {self.CONTROL}: "
                                                    f"no error starts {dropped!r}"), found[0])

    def drift(self, edits: dict[str, callable]) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="i4r-271-drift-") as directory:
            tree = pathlib.Path(directory) / "i4r-audit"
            shutil.copytree(SKILL, tree, ignore=IGNORED, symlinks=True)
            for relative, edit in edits.items():
                path = tree / relative
                path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")
            return subprocess.run([sys.executable, "-B", str(tree / "config/check_policy_drift.py")], text=True,
                                  capture_output=True, timeout=180, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    @staticmethod
    def leaf(name: str, value: int):
        return lambda text: text.replace(f'"{name}": {SHIPPED},', f'"{name}": {value},', 1)

    @staticmethod
    def line(pattern: str, replacement: str):
        def edit(text: str) -> str:
            new, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
            assert count == 1, pattern
            return new
        return edit

    def test_each_revert_on_disk_is_rejected_by_its_own_pin(self) -> None:
        policy = "config/pipeline-policy-v2.json"
        module = "scripts/probe_bounds.py"
        both = lambda text: self.leaf("maximum_normalized_tokens", LEGACY)(  # noqa: E731
            self.leaf("probe_max_normalized_tokens", LEGACY)(text))
        cases = [
            ("the release reverted (both leaves)", {policy: both}, [self.RUNTIME_PIN, self.LEAF_PIN], []),
            ("the runtime leaf alone", {policy: self.leaf("probe_max_normalized_tokens", LEGACY)},
             [self.RUNTIME_PIN, "packaged Phase-1 runtime limits and probe contract drifted"], [self.LEAF_PIN]),
            ("the descriptive leaf alone (free until 2.7.1)", {policy: self.leaf("maximum_normalized_tokens", LEGACY)},
             [self.LEAF_PIN], [self.RUNTIME_PIN]),
            *((f"both leaves at the never-shipped {value}",
               {policy: lambda text, value=value: self.leaf("maximum_normalized_tokens", value)(
                   self.leaf("probe_max_normalized_tokens", value)(text))},
               [self.RUNTIME_PIN, self.LEAF_PIN, "packaged Phase-1 runtime limits and probe contract drifted"], [])
              for value in NEVER_SHIPPED),
            ("the module's SHIPPED reverted with the policy",
             {policy: both, module: self.line(r"^SHIPPED_PROBE_BOUND = 350_000$", "SHIPPED_PROBE_BOUND = 150_000")},
             ["scripts/probe_bounds.py SHIPPED_PROBE_BOUND is 150000, expected 350000", self.RUNTIME_PIN], []),
            ("LIVE_PROBE_BOUNDS newest first",
             {module: self.line(r"^LIVE_PROBE_BOUNDS: .*$",
                                "LIVE_PROBE_BOUNDS: tuple[int, ...] = (SHIPPED_PROBE_BOUND, LEGACY_PROBE_BOUND)")},
             ["scripts/probe_bounds.py SHIPPED_PROBE_BOUND is not the newest LIVE_PROBE_BOUNDS entry"],
             ["SHIPPED_PROBE_BOUND is 150000", self.RUNTIME_PIN]),
            ("the legacy bound dropped",
             {module: self.line(r"^LIVE_PROBE_BOUNDS: .*$", "LIVE_PROBE_BOUNDS: tuple[int, ...] = (SHIPPED_PROBE_BOUND,)")},
             ["scripts/probe_bounds.py LIVE_PROBE_BOUNDS no longer holds the 150000 bound"], []),
        ]
        for label, edits, expected, absent in cases:
            with self.subTest(drift=label):
                result = self.drift(edits)
                self.assertEqual(result.returncode, 1, result.stdout)
                for message in expected:
                    self.assertIn(message, result.stdout)
                for message in absent:
                    self.assertNotIn(message, result.stdout)
        result = self.drift({})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("twenty planted drifts were rejected", result.stdout)


if __name__ == "__main__":
    unittest.main()
