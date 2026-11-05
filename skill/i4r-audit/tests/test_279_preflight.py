"""2.7.9: the Phase-1 preflight of a large deposit, or of an audit on a slow synced folder (C04, C03 hardening).

On a WSL2 host, the tree on /mnt/c under the Windows Dropbox client, the provider-free preflight of
one audit with a large deposit paused just past the fixed 60-second pre-dispatch deadline,
with a bare "Phase-1 pre-dispatch deadline exceeded"; another audit passed just under it. 2.7.8 read every large
file about seven times. The same host paused two audits again and again on renames Windows refused
(EACCES) while the sync client or a scanner held a file open.

* ``ReadsEachFileOnce`` -- a deposit with an 8 MB binary and a 3 MB CSV that is over the (lowered) workbook cap,
  through the real preflight sequence of run_phase1.main (snapshot, role packets, provider-free preflight), with
  every byte read through ``Path.open`` counted: each source file is read twice (the manifest check and the copy)
  and its snapshot copy twice (the two snapshot verifications), plus once as text for the CSV's privacy scan;
  2.7.8 read the binary seven times and the CSV nine. The evidence still records the same digests.
* ``ScaledDeadline`` -- a new audit freezes the rule, its preflight runs under 60 s plus 1 s per 10 MB and per five
  files of its manifest (receipt ``deadline_seconds``), capped at 600 s, in Phase 1 and in the verifier; an audit
  whose snapshot has no rule keeps exactly 60 s and the 2.7.8 runtime contract; a rule no runner admits is refused
  before any call; ``--pre-dispatch-deadline-seconds`` still only tightens. A manifest the rule cannot read (nested
  past the parser, a size of 10**400) never stops the run before the deadline: it pauses inside it, as on 2.7.8,
  in Phase 1, and the verifier still runs.
* ``DepositFileChangedDuringThePreflight`` -- a deposit file rewritten between the manifest check and the copy (a sync
  client replacing it) pauses the preflight by name instead of publishing evidence that disagrees with its copy.
* ``AlarmNamesTheStep`` -- the SIGALRM handler, which fires inside a blocking syscall (here an fsync of the big
  copy), names the step the last cooperative check saw: the pause says "during copying big.sav".
* ``RenameRetry`` -- a rename refused with EACCES or EPERM is retried after 0.25 to 4 s, eight tries at most and never
  past the deadline, in phase1_runtime.atomic_write, both snapshot renames and lifecycle_journal._atomic_write; the
  last refusal on a WSL kernel under /mnt/<drive>/ names the sync client and antivirus.
* ``JournalLockRetryBudget`` -- one journal-lock hold spends at most a twelfth of LOCK_TIMEOUT retrying, across all its
  projection writes, and none after one rename was refused to the end, so a second writer waiting for the lock is not
  timed out by the holder's retries; five refused fan-out writers queued at the default LOCK_TIMEOUT finish within
  half of it, and the writer queued behind them appends, every event journaled as on 2.7.8; a waiter waits on while
  the journal keeps growing and is refused LOCK_TIMEOUT (one 0.5 s sample late at most) after its last growth, an
  append or its creation, and a touched or same-size replaced journal does not extend the wait.
* ``DepositRootSkipsStaging`` -- check_deposit_values.deposit_root never returns a hidden ``.<sha>.<pid>.tmp``
  staging directory a refused rename left beside the snapshots.
* ``DriftPinsTheRule`` and ``InitFreezesTheRule`` -- the policy pin, its planted control and the module join; init
  prints the rule and refuses a leftover snapshot whose rule no runner admits, or that has none (2.7.8's
  ``record_invocation.py --init-policy``, an init that did not finish), and re-run after the rm freezes the rule; an
  audit whose init finished on 2.7.8 is refused as before and keeps 60 s. A new audit's snapshot states the rule's
  600 s as phase_1.provider_free_preflight.maximum_seconds, not the 60 an older audit froze.
"""
from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Callable
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import check_deposit_values  # noqa: E402
import lifecycle_journal  # noqa: E402
import phase1_runtime as runtime  # noqa: E402
import run_phase1  # noqa: E402
import test_264_verifier_runtime as verifier_tests  # noqa: E402  (VerifierFixture)
import test_270_claude_routes as route_tests  # noqa: E402  (freeze)
import test_270_policy_and_init as pinit  # noqa: E402  (make_audit, init, envelope_bytes)
import test_run_phase1  # noqa: E402  (the Phase-1 audit fixture)

LEAF = "pre_dispatch_deadline_scaling"
RULE = {"package_bytes_per_second": 10_000_000, "package_files_per_second": 5, "max_seconds": 600}
BIG = 8 * 1024 * 1024 + 7          # a size nothing else in the fixture has
CSV_ROWS = 250_000                 # about 3.5 MB of ASCII
EIGHT_TRIES = (0.25, 0.5, 1.0, 2.0, 4.0, 4.0, 4.0)
# One journal writer in its own process (JournalLockRetryBudget). A "holder" (or "queued") writer has every projection
# rename refused six times, as the Windows Dropbox client does on the WSL host (11.75 s of retries each); it touches
# the marker at its first refusal, inside the lock. The "holder" and "waiter" append a pause_start and its pause_end,
# the queue test's "queued" and "clean" writers one note each, named by the marker. Prints {"outcome", "seconds",
# "ended_at"} (ended_at on the wall clock, to compare writers).
JOURNAL_WRITER = r"""
import errno, json, os, pathlib, sys, time
sys.path.insert(0, sys.argv[1])
import lifecycle_journal
audit, role, marker = pathlib.Path(sys.argv[2]), sys.argv[3], pathlib.Path(sys.argv[4])
if role in ("holder", "queued"):
    real_replace, seen = os.replace, {}

    def refused(source, target, *args, **kwargs):
        seen[os.fspath(source)] = seen.get(os.fspath(source), 0) + 1
        if seen[os.fspath(source)] <= 6:
            marker.touch()
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), os.fspath(source), None, os.fspath(target))
        return real_replace(source, target, *args, **kwargs)
    os.replace = refused
if role == "holder":
    event, payload = "pause_start", {"reason": "local_preflight_failed", "detail": "x", "phase": "fan_out",
                                     "receipt": None, "receipt_sha256": None}
elif role == "waiter":
    event, payload = "pause_end", {"reason": "local_preflight_failed", "phase": "fan_out"}
else:
    event, payload = "note", {"text": marker.name}
started = time.monotonic()
try:
    lifecycle_journal.append_event(audit, event, payload, source="run_phase1.py")
    outcome = "appended"
except Exception as exc:
    outcome = type(exc).__name__
print(json.dumps({"outcome": outcome, "seconds": round(time.monotonic() - started, 3), "ended_at": time.time()}))
"""


def module(name: str):
    """A 2.7.9 module, imported where it is used: on 2.7.8 only the tests that need it fail."""
    return importlib.import_module(name)


def _base_278() -> tuple[pathlib.Path | None, str | None]:
    """A pristine 2.7.8 skill tree (I4R_BASE_278, or ../base when that is one), whose own scripts freeze what a 2.7.8
    init leaves; (None, why) for a named tree that is not one."""
    named = os.environ.get("I4R_BASE_278")
    tree = pathlib.Path(named) if named else SKILL.parent / "base"
    try:
        version = (tree / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = None
    if version == "2.7.8" and (tree / "scripts/record_invocation.py").is_file() \
            and not (tree / "scripts/pre_dispatch_deadlines.py").exists():
        return tree.resolve(), None
    return None, (f"I4R_BASE_278={named} is not a pristine 2.7.8 skill tree (VERSION {version!r})" if named else None)


BASE_278, BASE_278_PROBLEM = _base_278()
NO_BASE_278 = "needs a pristine 2.7.8 skill tree: set I4R_BASE_278 (or keep one at ../base)"


def policy_278() -> dict:
    """The policy 2.7.8 ships, and so freezes: this one without the rule, its preflight maximum at 60, and without
    the three leaves 2.7.11 added (critic snapshot evidence, the reasoning-token parse, the claims-script harness)."""
    policy = shipped_policy()
    del policy["phase_1"]["runtime"][LEAF]
    policy["phase_1"]["provider_free_preflight"]["maximum_seconds"] = 60
    del policy["critique"]["snapshot_evidence"]
    del policy["metadata"]["claude_reasoning_tokens"]
    del policy["publication"]["claims_script_harness"]
    return policy


def shipped_policy() -> dict:
    return json.loads((SKILL / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))


def expected_deadline(root: pathlib.Path) -> float:
    rows = json.loads((root / "package_manifest.json").read_text(encoding="utf-8"))["files"]
    return round(min(600.0, 60 + sum(row["size"] for row in rows) / 10_000_000 + len(rows) / 5), 3)


def big_csv() -> bytes:
    return ("id,score,group\n" + "".join(f"{index},{index % 97}.5,g{index % 7}\n"
                                          for index in range(CSV_ROWS))).encode("ascii")


def corrupt_manifest(root: pathlib.Path, how: str) -> str:
    """A package_manifest.json the rule cannot use: "overflow" gives its first row a size of 10**400, which still
    parses (returns that row's pkg/ path); "nested" is 100,000 nested arrays, past the JSON parser's recursion."""
    path = root / "package_manifest.json"
    if how == "nested":
        path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
        return ""
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][0]["size"] = 10 ** 400
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return f"pkg/{manifest['files'][0]['path']}"


class AuditFixture(unittest.TestCase):
    """test_run_phase1's Phase-1 audit (shipped policy frozen at init), with optional extra deposit files."""

    def setUp(self) -> None:
        self.fixture = test_run_phase1.Phase1RunnerTests(
            "test_normal_path_is_one_probe_plus_five_parallel_first_try_auditors")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def audit(self, name: str, *, extra: dict[str, bytes] | None = None, legacy: bool = False) -> pathlib.Path:
        root = self.fixture.make_audit(name)
        for relative, data in (extra or {}).items():
            (root / relative).write_bytes(data)
        self.fixture.write_package_manifest(root)
        if legacy:
            # the snapshot an audit initialised before 2.7.9 froze: no pre-dispatch rule
            policy = record_policy(root)
            policy["phase_1"]["runtime"].pop(LEAF, None)
            policy["phase_1"]["provider_free_preflight"]["maximum_seconds"] = 60
            run = json.loads((root / "run.json").read_text(encoding="utf-8"))
            run["toolkit"]["pipeline_policy_snapshot_sha256"] = route_tests.freeze(root, policy)
            (root / "run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        return root

    def probe_only(self, root: pathlib.Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.fixture.run_runner(root=root, extra_args=["--probe-only", *extra])

    def receipt(self, root: pathlib.Path) -> dict:
        status = json.loads((root / "phase1/status.json").read_text(encoding="utf-8"))
        return json.loads((root / "phase1/preflight" / status["source_snapshot_sha256"] / "receipt.json")
                          .read_text(encoding="utf-8"))


def record_policy(root: pathlib.Path) -> dict:
    return json.loads((root / "pipeline-policy.snapshot.json").read_text(encoding="utf-8"))["policy"]


# ------------------------------------------------------------------------------------------------ read counts
class _CountingReader:
    """A file object that adds every byte (or character) read to ``counts[key]``."""

    def __init__(self, handle, counts: dict[str, int], key: str) -> None:
        self._handle, self._counts, self._key = handle, counts, key

    def read(self, *args):
        data = self._handle.read(*args)
        self._counts[self._key] += len(data)
        return data

    def __iter__(self):
        for line in self._handle:
            self._counts[self._key] += len(line)
            yield line

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self._handle.close()

    def __getattr__(self, name: str):
        return getattr(self._handle, name)


class ReadsEachFileOnce(AuditFixture):
    def preflight_counting_reads(self, root: pathlib.Path, names: tuple[str, ...]) -> tuple[dict, dict]:
        """run_phase1.main's pre-dispatch sequence, in process, counting what is read of each named file."""
        counts = {f"{where}:{name}": 0 for name in names for where in ("source", "copy")}
        real_open = pathlib.Path.open
        deposit = (root / "pkg").resolve()

        def counting_open(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if "r" in mode and path.name in names:
                where = "source" if path.resolve().parent == deposit else "copy"
                return _CountingReader(handle, counts, f"{where}:{path.name}")
            return handle

        policy, configured = run_phase1.load_policy(root)
        phase_root = root / "phase1"
        phase_root.mkdir(exist_ok=True)
        deadline = runtime.MonotonicDeadline.after(120)
        with mock.patch.object(pathlib.Path, "open", counting_open), \
                mock.patch.object(runtime, "WORKBOOK_SUMMARY_MAX_BYTES", 1024 * 1024):
            snapshot, manifest = runtime.materialize_snapshot(root, phase_root, deadline, lenient_modes=True)
            packet_dirs, _packets, _prompts = run_phase1.materialize_role_packets(
                phase_root, snapshot, manifest, configured, "audit-reads", deadline)
            runtime.provider_free_preflight(root, phase_root, snapshot, manifest, packet_dirs, deadline,
                                            lenient_modes=True)
        return counts, manifest

    def test_a_large_file_is_read_twice_and_its_copy_twice(self) -> None:
        sav, csv = os.urandom(BIG), big_csv()
        root = self.audit("reads", extra={"pkg/big.sav": sav, "pkg/big.csv": csv})
        counts, manifest = self.preflight_counting_reads(root, ("big.sav", "big.csv"))
        passes = {key: round(value / (len(sav) if key.endswith(".sav") else len(csv)), 2)
                  for key, value in counts.items()}
        # the binary: the manifest check and the copy (plus the one-byte readability probe); 2.7.8 also read it for
        # the inventory, the archive rows and a hash of the copy -- seven passes in all
        self.assertEqual(passes["source:big.sav"], 2.0, passes)
        self.assertEqual(passes["copy:big.sav"], 2.0, passes)
        # the oversize CSV: the same, plus the privacy scan's text read; 2.7.8 also re-hashed it for the workbook
        # row -- nine passes in all
        self.assertEqual(passes["source:big.csv"], 3.0, passes)
        self.assertEqual(passes["copy:big.csv"], 2.0, passes)
        # every row still records the file's own digest
        rows = {row["path"]: row["sha256"] for row in manifest["source_files"]}
        evidence = pathlib.Path(root / "phase1/snapshots" / manifest["snapshot_sha256"] / "evidence")
        inventory = {row["path"]: row["sha256"] for row in
                     json.loads((evidence / "file_inventory.json").read_text(encoding="utf-8"))["files"]}
        [workbook] = [row for row in json.loads((evidence / "workbook_summaries.json").read_text(
            encoding="utf-8"))["workbooks"] if row["path"] == "pkg/big.csv"]
        self.assertEqual(workbook["status"], "unavailable")
        for name, data in (("big.sav", sav), ("big.csv", csv)):
            digest = hashlib.sha256(data).hexdigest()
            self.assertEqual((rows[f"pkg/{name}"], inventory[f"pkg/{name}"]), (digest, digest))
        self.assertEqual(workbook["sha256"], hashlib.sha256(csv).hexdigest())


# ------------------------------------------------------------------------------------------------ the deadline
class ScaledDeadline(AuditFixture):
    def test_the_rule_and_its_arithmetic(self) -> None:
        deadlines = module("pre_dispatch_deadlines")
        self.assertEqual(deadlines.SHIPPED_PRE_DISPATCH_SCALING, RULE)
        self.assertEqual(deadlines.LIVE_PRE_DISPATCH_SCALINGS, (RULE,))
        # a large deposit: 700,000,000 bytes in 25 files
        self.assertEqual(deadlines.deadline_seconds(RULE, 60, 700_000_000, 25), 135.0)
        self.assertEqual(deadlines.deadline_seconds(RULE, 60, 50_000_000_000, 10), 600.0)
        legacy = deadlines.deadline_seconds(None, 60, 700_000_000, 25)
        self.assertEqual((legacy, type(legacy)), (60, int))
        for value in ({**RULE, "max_seconds": 900}, {**RULE, "extra": 1}, {**RULE, "package_files_per_second": True},
                      60, None, [RULE]):
            with self.subTest(value=value):
                self.assertIsNone(deadlines.live_scaling(value))

    def test_a_new_audit_freezes_the_rule_and_runs_under_it(self) -> None:
        root = self.audit("scaled", extra={"pkg/big.sav": os.urandom(BIG)})
        self.assertEqual(record_policy(root)["phase_1"]["runtime"][LEAF], RULE)
        policy, _configured = run_phase1.load_policy(root)
        expected = expected_deadline(root)
        self.assertGreater(expected, 61.6)
        self.assertEqual(run_phase1.pre_dispatch_deadline(policy, root), expected)
        result = self.probe_only(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.receipt(root)["deadline_seconds"], expected)
        # an explicit value still only tightens: 60 s or less is taken as given, anything more refused
        tightened = self.probe_only(root, "--pre-dispatch-deadline-seconds", "45")
        self.assertEqual(tightened.returncode, 0, tightened.stdout + tightened.stderr)
        self.assertEqual(self.receipt(root)["deadline_seconds"], 45.0)
        raised = self.probe_only(root, "--pre-dispatch-deadline-seconds", "61")
        self.assertEqual(raised.returncode, 2, raised.stdout + raised.stderr)

    def test_an_audit_without_the_rule_keeps_exactly_60_seconds_and_the_278_contract(self) -> None:
        root = self.audit("legacy", extra={"pkg/big.sav": os.urandom(BIG)}, legacy=True)
        policy, _configured = run_phase1.load_policy(root)
        self.assertNotIn(LEAF, policy["phase_1"]["runtime"])
        self.assertEqual(policy["phase_1"]["runtime"], run_phase1.packaged_runtime(350000, 5400))
        result = self.probe_only(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deadline = self.receipt(root)["deadline_seconds"]
        self.assertEqual((deadline, type(deadline)), (60, int))
        raised = self.probe_only(root, "--pre-dispatch-deadline-seconds", "61")
        self.assertEqual(raised.returncode, 2, raised.stdout + raised.stderr)
        self.assertIn("no greater than 60 seconds", raised.stderr)

    def test_a_rule_no_runner_admits_is_refused_before_any_call(self) -> None:
        root = self.audit("odd")
        policy = record_policy(root)
        policy["phase_1"]["runtime"][LEAF] = {**RULE, "max_seconds": 900}
        run = json.loads((root / "run.json").read_text(encoding="utf-8"))
        run["toolkit"]["pipeline_policy_snapshot_sha256"] = route_tests.freeze(root, policy)
        (root / "run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(run_phase1.RunnerError) as caught:
            run_phase1.load_policy(root)
        self.assertIn("audit-scoped phase_1.runtime differs from the packaged execution contract: its "
                      "pre_dispatch_deadline_scaling", str(caught.exception))
        result = self.probe_only(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.fixture.state.exists(), "no Claude call was started")


    def test_an_absurd_extent_gets_the_ceiling_and_never_raises(self) -> None:
        deadlines = module("pre_dispatch_deadlines")
        for package_bytes, package_files in ((10 ** 400, 3), (5, 10 ** 400), (10 ** 400, 10 ** 400)):
            with self.subTest(digits=(len(str(package_bytes)), len(str(package_files)))):
                self.assertEqual(deadlines.deadline_seconds(RULE, 60, package_bytes, package_files), 600.0)
        # below the ceiling, and at it, exactly the formula
        for package_bytes in (0, 1, 700_000_000, 5_399_999_999, 5_400_000_000, 5_999_999_999, 6_000_000_000):
            for package_files in (0, 1, 25, 2_699, 2_700, 2_999, 3_000, 3_001):
                formula = round(min(600.0, 60 + package_bytes / 10_000_000 + package_files / 5), 3)
                self.assertEqual(deadlines.deadline_seconds(RULE, 60, package_bytes, package_files), formula,
                                 (package_bytes, package_files))

    def test_a_manifest_the_parser_cannot_read_counts_as_empty(self) -> None:
        root = self.audit("nested-extent")
        corrupt_manifest(root, "nested")
        self.assertEqual(runtime.package_manifest_extent(root), (0, 0))

    def test_an_absurd_manifest_pauses_inside_the_deadline_as_on_278(self) -> None:
        # 2.7.8 read the manifest only inside the deadline and paused on it; the rule reads it before, and must not
        # turn the same manifest into a failed run with an unrelated error
        for how in ("overflow", "nested"):
            with self.subTest(how=how):
                root = self.audit(f"absurd-{how}")
                named = corrupt_manifest(root, how)
                result = self.probe_only(root)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                status = json.loads((root / "phase1/status.json").read_text(encoding="utf-8"))
                self.assertEqual((status["status"], status["pause_reason"]), ("paused", "local_preflight_failed"))
                if how == "overflow":
                    self.assertEqual(status["error"], f"package manifest hash/size mismatch: {named}")
                # the nested manifest's error is the JSON parser's own words, which differ by Python version
                # ("maximum recursion depth exceeded ..." on 3.12, "Stack overflow ..." on 3.14): the pause, its
                # reason and that no call started are what 2.7.8 did
                self.assertFalse(self.fixture.state.exists(), "no Claude call was started")


class VerifierUsesTheAuditsDeadline(verifier_tests.VerifierFixture):
    def test_the_verifier_preflight_runs_under_the_audits_own_deadline(self) -> None:
        root = self.audit_after_phase1()
        result = self.run_verifier(root, ["F13"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        [receipt] = (root / "phase1/verifier").glob("verifier_*/preflight/*/receipt.json")
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["deadline_seconds"],
                         expected_deadline(root))

    def test_an_absurd_manifest_does_not_stop_the_verifier(self) -> None:
        # plan_batch reads the extent too; the verifier checks the frozen snapshot, never this file, so 2.7.8 ran it
        root = self.audit_after_phase1()
        corrupt_manifest(root, "overflow")
        result = self.run_verifier(root, ["F13"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        [receipt] = (root / "phase1/verifier").glob("verifier_*/preflight/*/receipt.json")
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["deadline_seconds"], 600.0)


# ------------------------------------------------------------------------------------------------ a changing deposit
class DepositFileChangedDuringThePreflight(AuditFixture):
    @staticmethod
    def rewritten_after_the_manifest_check(phase1_runtime, root: pathlib.Path):
        """The paper is extracted after the manifest check and before the copy: a sync client replaces
        pkg/data.csv right then."""
        real = phase1_runtime.paper_text

        def paper_text(*args, **kwargs):
            extracted = real(*args, **kwargs)
            (root / "pkg/data.csv").write_text("participant,value,group\nchanged,9,Z\n", encoding="utf-8")
            return extracted
        return mock.patch.object(phase1_runtime, "paper_text", paper_text)

    def test_the_snapshot_is_refused_by_name_and_nothing_is_published(self) -> None:
        root = self.audit("changed")
        with self.rewritten_after_the_manifest_check(runtime, root), \
                self.assertRaises(runtime.Phase1Error) as caught:
            runtime.materialize_snapshot(root, root / "phase1", runtime.MonotonicDeadline.after(120),
                                         lenient_modes=True)
        self.assertEqual(str(caught.exception), "deposit file changed during the preflight: pkg/data.csv")
        snapshots = root / "phase1/snapshots"
        self.assertEqual(sorted(os.listdir(snapshots)) if snapshots.exists() else [], [])
        self.assertEqual(os.listdir(root / "phase1/snapshot_staging"), [])

    def test_phase_1_pauses_before_any_call(self) -> None:
        root = self.audit("changed-run")
        environment = {"FAKE_PHASE1_SCENARIO": "success", "FAKE_PHASE1_STATE": str(self.fixture.state)}
        with mock.patch.dict(os.environ, environment), \
                self.rewritten_after_the_manifest_check(run_phase1.runtime, root):
            code = run_phase1.main([str(root), "--claude-bin", str(self.fixture.fake), "--probe-only"])
        self.assertEqual(code, 3)
        status = json.loads((root / "phase1/status.json").read_text(encoding="utf-8"))
        self.assertEqual((status["status"], status["pause_reason"]), ("paused", "local_preflight_failed"))
        self.assertEqual(status["error"], "deposit file changed during the preflight: pkg/data.csv")
        self.assertFalse(self.fixture.state.exists(), "no Claude call was started")


# ------------------------------------------------------------------------------------------------ the alarm
class AlarmNamesTheStep(AuditFixture):
    def test_the_pause_names_the_step_the_alarm_interrupted(self) -> None:
        root = self.audit("alarm", extra={"pkg/big.sav": os.urandom(BIG)})
        real_fsync = os.fsync

        def stalled_fsync(fd: int) -> None:
            # the copy of the big file stalls in its fsync, as a synced folder's can, past the whole deadline
            if os.fstat(fd).st_size == BIG:
                time.sleep(60)
            real_fsync(fd)

        environment = {"FAKE_PHASE1_SCENARIO": "success", "FAKE_PHASE1_STATE": str(self.fixture.state)}
        with mock.patch.dict(os.environ, environment), mock.patch("os.fsync", stalled_fsync):
            started = time.monotonic()
            code = run_phase1.main([str(root), "--claude-bin", str(self.fixture.fake), "--probe-only",
                                    "--pre-dispatch-deadline-seconds", "6"])
        self.assertLess(time.monotonic() - started, 30, "the alarm interrupted the stalled fsync")
        self.assertEqual(code, 3)
        status = json.loads((root / "phase1/status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["pause_reason"], "local_preflight_failed")
        self.assertEqual(status["error"], "Phase-1 pre-dispatch deadline exceeded during copying big.sav")
        self.assertFalse(self.fixture.state.exists(), "no Claude call was started")


# ------------------------------------------------------------------------------------------------ renames
class RenameRetry(AuditFixture):
    def flaky(self, failures: int, code: int = errno.EACCES, only=lambda source, target: True):
        """os.replace that Windows refuses ``failures`` times (the matching renames only), then lets through."""
        real_replace = os.replace
        self.renames: list[tuple[str, str]] = []
        remaining = [failures]

        def replace(source, target, *args, **kwargs):
            self.renames.append((os.fspath(source), os.fspath(target)))
            if remaining[0] > 0 and only(os.fspath(source), os.fspath(target)):
                remaining[0] -= 1
                raise PermissionError(code, os.strerror(code), os.fspath(source), None, os.fspath(target))
            return real_replace(source, target, *args, **kwargs)
        return mock.patch("os.replace", replace)

    def sleeps(self):
        """The retry's backoff, recorded instead of slept. Any other sleep is real: subprocess.run polls with
        time.sleep while a tool-version probe of the evidence step exits."""
        self.slept: list[float] = []
        real_sleep = time.sleep

        def sleep(seconds: float) -> None:
            if sys._getframe(1).f_globals.get("__name__") == "fs_retry":
                self.slept.append(seconds)
            else:
                real_sleep(seconds)
        return mock.patch("time.sleep", sleep)

    def test_atomic_write_retries_a_refused_rename(self) -> None:
        target = self.fixture.base / "receipt.json"
        with self.flaky(3), self.sleeps():
            runtime.atomic_write(target, b"{}\n")
        self.assertEqual(target.read_bytes(), b"{}\n")
        self.assertEqual(self.slept, [0.25, 0.5, 1.0])
        self.assertEqual(len(self.renames), 4)

    def test_the_journal_projection_write_retries_a_refused_rename(self) -> None:
        target = self.fixture.base / "RUN_METADATA.md"
        with self.flaky(2, errno.EPERM), self.sleeps():
            lifecycle_journal._atomic_write(target, b"# metadata\n")
        self.assertEqual(target.read_bytes(), b"# metadata\n")
        self.assertEqual(self.slept, [0.25, 0.5])
        self.assertEqual(list(target.parent.glob(".RUN_METADATA.md.*")), [], "no temporary left behind")

    def test_both_snapshot_renames_retry(self) -> None:
        root = self.audit("renames")
        # snapshot_staging/snapshot-* -> snapshots/.<sha>.<pid>.tmp is refused twice, then .<sha>.<pid>.tmp -> <sha>
        # once: each rename must retry on its own
        refusals = {"stage": 2, "publish": 1}

        def which(source: str, target: str) -> bool:
            kind = "stage" if pathlib.Path(target).name.endswith(".tmp") and "snapshot-" in source else \
                "publish" if pathlib.Path(source).name.endswith(".tmp") and "/snapshots/" in target else None
            if kind is None or not refusals[kind]:
                return False
            refusals[kind] -= 1
            return True

        with self.flaky(99, only=which), self.sleeps():
            snapshot, manifest = runtime.materialize_snapshot(
                root, root / "phase1", runtime.MonotonicDeadline.after(120), lenient_modes=True)
        self.assertEqual(refusals, {"stage": 0, "publish": 0})
        self.assertEqual(snapshot.name, manifest["snapshot_sha256"])
        runtime.verify_snapshot(snapshot, manifest)
        self.assertEqual(self.slept, [0.25, 0.5, 0.25])
        self.assertEqual([name for name in os.listdir(root / "phase1/snapshots") if name.startswith(".")], [])

    def test_a_permanent_refusal_is_raised_after_eight_tries_and_named_on_wsl(self) -> None:
        fs_retry = module("fs_retry")
        source, target = "/mnt/c/Users/example/I4R/run.json.tmp", "/mnt/c/Users/example/I4R/run.json"
        for wsl, paths, named in ((True, (source, target), True), (False, (source, target), False),
                                  (True, ("/home/example/a.tmp", "/home/example/a"), False)):
            with self.subTest(wsl=wsl, paths=paths), self.flaky(99), self.sleeps(), \
                    mock.patch.object(fs_retry, "wsl_kernel", return_value=wsl):
                with self.assertRaises(PermissionError) as caught:
                    fs_retry.replace_with_retry(*paths)
                self.assertEqual(len(self.renames), 8)
                self.assertEqual(tuple(self.slept), EIGHT_TRIES)
                self.assertEqual(caught.exception.errno, errno.EACCES)
                message = str(caught.exception)
                self.assertEqual("Dropbox or OneDrive client, or an antivirus scan" in message, named, message)
                self.assertEqual("/mnt/c" in message.split(":")[0], named, message)
                self.assertTrue(message.startswith("[Errno 13] Permission denied"), message)
                self.assertNotIn("\n", message)

    def test_wsl_is_recognised_by_its_kernel(self) -> None:
        fs_retry = module("fs_retry")
        with mock.patch("platform.release", return_value="5.15.167.4-microsoft-standard-WSL2"):
            self.assertTrue(fs_retry.wsl_kernel())
        with mock.patch("platform.release", return_value="24.0.0"), \
                mock.patch.object(pathlib.Path, "read_text", side_effect=OSError("no /proc")):
            self.assertFalse(fs_retry.wsl_kernel())

    def test_the_retry_never_sleeps_past_the_deadline(self) -> None:
        fs_retry = module("fs_retry")
        target = self.fixture.base / "late.json"
        source = self.fixture.base / "late.json.tmp"
        source.write_bytes(b"x")
        started = time.monotonic()
        with self.flaky(99), self.assertRaises(PermissionError):
            fs_retry.replace_with_retry(source, target, runtime.MonotonicDeadline.after(1.0))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(self.renames), 3, "0.25 s and 0.5 s fit in the second left, 1 s does not")

    def test_other_errors_are_not_retried(self) -> None:
        missing = self.fixture.base / "absent.tmp"
        with self.sleeps(), self.assertRaises(FileNotFoundError):
            module("fs_retry").replace_with_retry(missing, self.fixture.base / "absent")
        self.assertEqual(self.slept, [])


# ------------------------------------------------------------------------------------------------ journal lock
class JournalLockRetryBudget(AuditFixture):
    LOCK_TIMEOUT = 24
    BUDGET = 2.0              # a twelfth of LOCK_TIMEOUT is what one hold may spend retrying
    SAMPLE = 0.5              # how often a waiter reads the journal's size (LOCK_PROGRESS_SAMPLE_SECONDS)
    SLACK = 0.4               # the waiter's 0.05 s polling, and a loaded machine

    def writer(self, audit: pathlib.Path, role: str, marker: pathlib.Path,
               lock_timeout: float | None) -> subprocess.Popen[str]:
        """A JOURNAL_WRITER process; ``lock_timeout`` None leaves LOCK_TIMEOUT unset (its default, 60 s)."""
        script = self.fixture.base / "journal_writer.py"
        if not script.exists():
            script.write_text(JOURNAL_WRITER, encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if key != "LOCK_TIMEOUT"}
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if lock_timeout is not None:
            environment["LOCK_TIMEOUT"] = str(lock_timeout)
        child = subprocess.Popen([sys.executable, "-B", str(script), str(SCRIPTS), str(audit), role, str(marker)],
                                 env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (child.kill(), child.communicate()))
        return child

    def await_marker(self, child: subprocess.Popen[str], marker: pathlib.Path) -> None:
        give_up = time.monotonic() + 60
        while not marker.exists():
            self.assertIsNone(child.poll(), "the holder ended before its first refused rename")
            self.assertLess(time.monotonic(), give_up)
            time.sleep(0.02)

    def test_a_writer_waiting_for_the_lock_is_not_timed_out_by_the_holders_retries(self) -> None:
        audit = self.fixture.make_audit("lock", metadata21=True)
        marker = self.fixture.base / "holder-is-retrying"
        holder = self.writer(audit, "holder", marker, self.LOCK_TIMEOUT)
        self.await_marker(holder, marker)
        waiter = self.writer(audit, "waiter", marker, self.LOCK_TIMEOUT)
        waited = json.loads(waiter.communicate(timeout=120)[0])
        # 2.7.9 before this fix: the holder kept the lock 7 x 11.75 s and the waiter gave up after LOCK_TIMEOUT
        self.assertEqual(waited["outcome"], "appended", waited)
        self.assertLess(waited["seconds"], self.BUDGET + 2.0, waited)
        held = json.loads(holder.communicate(timeout=120)[0])
        # its renames would need 11.75 s each; the hold's 2 s ends the holder's append as 2.7.8 ended it at once
        self.assertEqual(held["outcome"], "PermissionError", held)
        self.assertLess(held["seconds"], self.BUDGET + 2.0, held)
        # both events are journaled, and the waiter's projection covers them
        events = lifecycle_journal.load_events(audit)
        self.assertEqual([event["type"] for event in events[-2:]], ["pause_start", "pause_end"])
        self.assertEqual(json.loads((audit / "invocations.watermark.json").read_text(encoding="utf-8")),
                         lifecycle_journal.watermark(events))

    def test_one_hold_has_one_budget_for_all_its_writes(self) -> None:
        directory = self.fixture.base / "hold"
        directory.mkdir()
        real_replace, seen = os.replace, {}

        def refused_twice(source, target, *args, **kwargs):
            seen[os.fspath(source)] = seen.get(os.fspath(source), 0) + 1
            if seen[os.fspath(source)] <= 2:
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), os.fspath(source), None,
                                      os.fspath(target))
            return real_replace(source, target, *args, **kwargs)

        written: list[str] = []
        # LOCK_TIMEOUT 15: the hold may retry for 1.25 s. Alone each write passes after 0.25 + 0.5 s; in one hold the
        # second gets only what the first left, and its refusal is raised
        with mock.patch.dict(os.environ, {"LOCK_TIMEOUT": "15"}), mock.patch("os.replace", refused_twice):
            with lifecycle_journal.journal_lock(directory), self.assertRaises(PermissionError):
                for name in ("run.json", "invocations.jsonl", "run_metrics.json"):
                    lifecycle_journal._atomic_write(directory / name, b"{}\n")
                    written.append(name)
            self.assertEqual(written, ["run.json"])
            # after the hold, a write is back on fs_retry's own tries
            lifecycle_journal._atomic_write(directory / "RUN_METADATA.md", b"# metadata\n")
        self.assertEqual((directory / "RUN_METADATA.md").read_bytes(), b"# metadata\n")
        self.assertEqual(sorted(path.name for path in directory.iterdir() if path.name.startswith(".")
                                and path.name != lifecycle_journal.LOCK_NAME), [], "no temporary left behind")

    def test_a_rename_refused_to_the_end_spends_the_holds_budget(self) -> None:
        # one write whose retries ran out leaves the rest of its hold none: a later write in it fails at once, as
        # every write did on 2.7.8, instead of retrying on files the sync client still holds
        directory = self.fixture.base / "spent"
        directory.mkdir()
        real_replace, real_sleep = os.replace, time.sleep
        refusals = {"run.json": 99, "invocations.jsonl": 1}
        slept: list[float] = []

        def replace(source, target, *args, **kwargs):
            name = pathlib.Path(os.fspath(target)).name
            if refusals.get(name, 0) > 0:
                refusals[name] -= 1
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), os.fspath(source), None,
                                      os.fspath(target))
            return real_replace(source, target, *args, **kwargs)

        def sleep(seconds: float) -> None:   # fs_retry's backoff recorded, not slept: the budget never runs down
            if sys._getframe(1).f_globals.get("__name__") == "fs_retry":
                slept.append(seconds)
            else:
                real_sleep(seconds)

        # LOCK_TIMEOUT 36: a 3 s budget, which run.json's refusals leave whole (no time passes), until its 4 s step
        with mock.patch.dict(os.environ, {"LOCK_TIMEOUT": "36"}), mock.patch("os.replace", replace), \
                mock.patch("time.sleep", sleep), lifecycle_journal.journal_lock(directory):
            with self.assertRaises(PermissionError):
                lifecycle_journal._atomic_write(directory / "run.json", b"{}\n")
            self.assertEqual(slept, [0.25, 0.5, 1.0, 2.0])
            # refused once only, which 0.25 s of the budget left would clear; the budget is spent, so it is raised
            with self.assertRaises(PermissionError):
                lifecycle_journal._atomic_write(directory / "invocations.jsonl", b"\n")
            self.assertEqual(slept, [0.25, 0.5, 1.0, 2.0], "no retry after the hold's budget was spent")
        self.assertEqual(sorted(path.name for path in directory.iterdir()), [lifecycle_journal.LOCK_NAME])

    def test_five_refused_fan_out_writers_never_time_out_a_writer_queued_behind_them(self) -> None:
        # the re-review's queue: Phase 1's five fan-out workers append at once with every projection rename refused
        # six times, and one more writer, whose renames go through, queues behind them, all at the default
        # LOCK_TIMEOUT (60 s). Before this fix each refused hold took 13.6 s, so the queue ran 68 s and a writer
        # served last gave up after 60 s without its event; 2.7.8 journaled all six at once.
        audit = self.fixture.make_audit("queue", metadata21=True)
        before = len(lifecycle_journal.load_events(audit))
        markers = {name: self.fixture.base / name
                   for name in ("queued-1", "queued-2", "queued-3", "queued-4", "queued-5", "clean")}
        started = time.time()
        children = {"queued-1": self.writer(audit, "queued", markers["queued-1"], None)}
        self.await_marker(children["queued-1"], markers["queued-1"])   # the first holds the lock
        for name in ("queued-2", "queued-3", "queued-4", "queued-5"):
            children[name] = self.writer(audit, "queued", markers[name], None)
        time.sleep(0.5)   # the four others poll the lock
        children["clean"] = self.writer(audit, "clean", markers["clean"], None)
        results = {name: json.loads(child.communicate(timeout=300)[0]) for name, child in children.items()}
        # no JournalError: the writer queued behind them appended, and each refused writer ended on its refused
        # rename, as on 2.7.8
        self.assertEqual(results.pop("clean")["outcome"], "appended")
        self.assertEqual({name: result["outcome"] for name, result in results.items()},
                         {name: "PermissionError" for name in results})
        # every event is journaled, as on 2.7.8 (each writer appends before it projects)
        notes = [event["payload"]["text"] for event in lifecycle_journal.load_events(audit)[before:]
                 if event["type"] == "note"]
        self.assertEqual(sorted(notes), sorted(markers))
        # and the five refused holds' retries took less than half of the default LOCK_TIMEOUT
        self.assertLess(max(result["ended_at"] for result in results.values()) - started, 30.0, results)

    def hold_against_a_waiter(self, audit: pathlib.Path, lock_timeout: float, hold: float,
                              act: Callable[[float], None]) -> dict:
        """Hold the real journal lock while a waiter thread appends a note with LOCK_TIMEOUT=lock_timeout, calling
        act(seconds since the waiter began) every 0.05 s; let go ``hold`` seconds after it began, or once it gave up.
        Returns the waiter's outcome, and when it began and ended (time.monotonic())."""
        waited: dict = {}
        started = threading.Event()

        def waiter() -> None:
            waited["began"] = time.monotonic()
            started.set()
            try:
                lifecycle_journal.append_event(audit, "note", {"text": "waiter"}, source="test")
                waited["outcome"] = "appended"
            except lifecycle_journal.JournalError:
                waited["outcome"] = "JournalError"
            waited["ended"] = time.monotonic()

        thread = threading.Thread(target=waiter)
        with mock.patch.dict(os.environ, {"LOCK_TIMEOUT": f"{lock_timeout:g}"}):
            with lifecycle_journal.journal_lock(audit):
                thread.start()
                started.wait(timeout=60)
                while thread.is_alive() and time.monotonic() - waited["began"] < hold:
                    act(time.monotonic() - waited["began"])
                    time.sleep(0.05)
            thread.join(timeout=60)
        return waited

    @staticmethod
    def appends_at(audit: pathlib.Path, *seconds: float) -> tuple[Callable[[float], None], list[float]]:
        """A holder's act that appends one note as each of ``seconds`` passes, and the times it did (monotonic)."""
        grew: list[float] = []

        def act(held: float) -> None:
            if len(grew) < len(seconds) and held >= seconds[len(grew)]:
                lifecycle_journal._append_locked(audit, [{
                    "type": "note", "payload": {"text": f"holder {len(grew)}"}, "source": "test"}])
                grew.append(time.monotonic())
        return act, grew

    def test_a_waiter_waits_on_while_the_journal_grows_and_not_for_a_holder_that_journals_nothing(self) -> None:
        # LOCK_TIMEOUT counts from the journal's last growth. This test holds the lock for 4 s against a waiter with a
        # 1.5 s LOCK_TIMEOUT, appending a note each second (a queue that moves) or only once, at 0.25 s (a holder that
        # then stops making progress)
        audit = self.fixture.make_audit("moving", metadata21=True)
        for every_second in (True, False):
            with self.subTest(every_second=every_second):
                act, grew = self.appends_at(audit, *((1.0, 2.0, 3.0) if every_second else (0.25,)))
                waited = self.hold_against_a_waiter(audit, 1.5, 4.0, act)
                if every_second:
                    # the journal grew before each 1.5 s ran out: it waited for the lock
                    self.assertEqual(waited["outcome"], "appended", waited)
                    self.assertGreater(waited["ended"] - waited["began"], 3.5)
                else:
                    # refused 1.5 s after the append, one sample (0.5 s) late at most, before the holder let go.
                    # 2.7.9 before this fix looked only when 1.5 s ran out, and then waited 1.5 s more: 2.75 s
                    self.assertEqual(waited["outcome"], "JournalError", waited)
                    self.assertGreaterEqual(waited["ended"] - grew[0], 1.5 - 0.1, waited)
                    self.assertLess(waited["ended"] - grew[0], 1.5 + self.SAMPLE + self.SLACK, waited)

    def test_the_wait_counts_from_the_last_growth_and_from_the_journals_creation(self) -> None:
        # a holder that appends three notes in its first 1.1 s and then stalls, or that puts the journal back at 0.5 s
        # where there was none: the waiter (LOCK_TIMEOUT 2.5 s) is refused 2.5 s after that last growth, not after
        # the first and not 2.5 s after it began. 2.7.9 before this fix refused it at 5 s
        audit = self.fixture.make_audit("stalled", metadata21=True)
        journal, aside = audit / lifecycle_journal.JOURNAL_NAME, audit / "journal-aside"
        for how in ("appends", "creates"):
            with self.subTest(how=how):
                if how == "appends":
                    act, grew = self.appends_at(audit, 0.1, 0.6, 1.1)
                else:
                    journal.rename(aside)
                    grew = []

                    def act(held: float) -> None:
                        if not grew and held >= 0.5:
                            aside.rename(journal)
                            grew.append(time.monotonic())
                waited = self.hold_against_a_waiter(audit, 2.5, 6.0, act)
                self.assertEqual(waited["outcome"], "JournalError", waited)
                self.assertGreaterEqual(waited["ended"] - grew[-1], 2.5 - 0.1, waited)
                self.assertLess(waited["ended"] - grew[-1], 2.5 + self.SAMPLE + self.SLACK, waited)

    def test_a_growth_no_sample_saw_is_seen_when_lock_timeout_runs_out(self) -> None:
        # samples too sparse to see an append at 1 s (one a minute): the look the waiter takes when its 1.5 s run out
        # still sees it, so it is never refused sooner than LOCK_TIMEOUT after the journal grew
        audit = self.fixture.make_audit("unsampled", metadata21=True)
        act, grew = self.appends_at(audit, 1.0)
        with mock.patch.object(lifecycle_journal, "LOCK_PROGRESS_SAMPLE_SECONDS", 60):
            waited = self.hold_against_a_waiter(audit, 1.5, 4.0, act)
        self.assertEqual(waited["outcome"], "JournalError", waited)
        self.assertGreaterEqual(waited["ended"] - grew[0], 1.5 - 0.1, waited)

    def test_a_touched_or_same_size_replaced_journal_does_not_extend_the_wait(self) -> None:
        # the holder journals nothing while the journal's mtime changes every 0.05 s (a touch loop), or while the
        # journal is replaced by a copy of itself (a sync client): the waiter is refused after its 1.5 s LOCK_TIMEOUT,
        # as on 2.7.8. 2.7.9 before this fix counted either as progress and waited until the holder let go
        audit = self.fixture.make_audit("touched", metadata21=True)
        journal = audit / lifecycle_journal.JOURNAL_NAME
        size = journal.stat().st_size
        for how in ("touch", "replace"):
            with self.subTest(how=how):
                def act(held: float) -> None:
                    if how == "touch":
                        os.utime(journal)
                    else:
                        copy = audit / ".journal-copy"
                        copy.write_bytes(journal.read_bytes())
                        os.replace(copy, journal)
                waited = self.hold_against_a_waiter(audit, 1.5, 4.5, act)
                self.assertEqual(waited["outcome"], "JournalError", waited)
                self.assertGreaterEqual(waited["ended"] - waited["began"], 1.5 - 0.05, waited)
                self.assertLess(waited["ended"] - waited["began"], 1.5 + self.SLACK, waited)
                self.assertEqual(journal.stat().st_size, size)

    def test_a_journal_that_flaps_or_goes_and_comes_back_extends_the_wait_once_at_most(self) -> None:
        # nothing is journaled while something outside the lock swaps the journal between two sizes every 0.3 s, or
        # unlinks it and writes the same bytes back: only a size above the largest seen counts, so the waiter is
        # refused 1.5 s after the first larger size at the latest, not when the holder lets go 4.5 s after it began
        audit = self.fixture.make_audit("flapping", metadata21=True)
        journal = audit / lifecycle_journal.JOURNAL_NAME
        original = journal.read_bytes()
        for how in ("two sizes", "gone and back"):
            with self.subTest(how=how):
                def act(held: float) -> None:
                    phase = int(held / 0.3) % 2
                    if how == "two sizes":
                        copy = audit / ".journal-copy"
                        copy.write_bytes(original + b"\n" * phase)
                        os.replace(copy, journal)
                    elif phase:
                        journal.unlink(missing_ok=True)
                    elif not journal.exists():
                        journal.write_bytes(original)
                try:
                    waited = self.hold_against_a_waiter(audit, 1.5, 4.5, act)
                finally:
                    journal.write_bytes(original)
                self.assertEqual(waited["outcome"], "JournalError", waited)
                self.assertGreaterEqual(waited["ended"] - waited["began"], 1.5 - 0.05, waited)
                self.assertLess(waited["ended"] - waited["began"],
                                1.5 + (0.3 + self.SAMPLE if how == "two sizes" else 0) + self.SLACK, waited)


# ------------------------------------------------------------------------------------------------ deposit_root
class DepositRootSkipsStaging(unittest.TestCase):
    def test_a_hidden_staging_directory_is_never_the_deposit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-279-deposit-") as name:
            audit = pathlib.Path(name)
            published = audit / "phase1/snapshots" / ("a" * 64) / "raw/pkg"
            leftover = audit / "phase1/snapshots" / f".{'b' * 64}.4242.tmp" / "raw/pkg"
            published.mkdir(parents=True)
            self.assertEqual(check_deposit_values.deposit_root(audit), (published, "phase1 snapshot"))
            leftover.mkdir(parents=True)
            stamp = time.time() + 60
            os.utime(leftover, (stamp, stamp))   # the newest, which 2.7.8 picked
            self.assertEqual(check_deposit_values.deposit_root(audit), (published, "phase1 snapshot"))
            shutil.rmtree(published.parent.parent)
            self.assertEqual(check_deposit_values.deposit_root(audit), (None, "none"))


# ------------------------------------------------------------------------------------------------ policy and init
class DriftPinsTheRule(unittest.TestCase):
    def checker(self, tree: pathlib.Path = SKILL):
        spec = importlib.util.spec_from_file_location("i4r_279_drift", tree / "config/check_policy_drift.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        return loaded

    def test_the_named_control_is_rejected_by_its_pin(self) -> None:
        checker = self.checker()
        self.assertEqual(checker.SHIPPED_PRE_DISPATCH_SCALING, RULE)
        for change in ("drop", "legacy-cap", "bool"):
            with self.subTest(change=change):
                candidate = shipped_policy()
                runtime_section = candidate["phase_1"]["runtime"]
                if change == "drop":
                    runtime_section.pop(LEAF)
                elif change == "legacy-cap":
                    runtime_section[LEAF]["max_seconds"] = 60
                else:
                    runtime_section[LEAF]["package_files_per_second"] = True
                errors: list[str] = []
                checker.check_policy_shape(candidate, errors)
                self.assertTrue(any(error.startswith(
                    "the Phase-1 pre-dispatch rule phase_1.runtime.pre_dispatch_deadline_scaling is")
                    for error in errors), errors)

    def test_a_new_audits_preflight_ceiling_is_the_rules(self) -> None:
        # the descriptive leaf every new audit freezes beside the rule: the 600 s the rule allows, not 2.7.8's 60
        self.assertEqual(shipped_policy()["phase_1"]["provider_free_preflight"]["maximum_seconds"], RULE["max_seconds"])
        candidate = shipped_policy()
        candidate["phase_1"]["provider_free_preflight"]["maximum_seconds"] = 60
        errors: list[str] = []
        self.checker().check_policy_shape(candidate, errors)
        self.assertIn("Phase-1 provider-free preflight/deadline contract drifted", errors)

    def test_a_revert_of_the_module_on_disk_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-279-drift-") as name:
            tree = pathlib.Path(name) / "tree"
            for part in ("config", "scripts", "schemas", "reference", "prompts"):
                shutil.copytree(SKILL / part, tree / part, ignore=shutil.ignore_patterns("__pycache__"))
            for part in ("SKILL.md", "INSTALL.md", "CHEATSHEET.html", "VERSION", "CHANGELOG.md"):
                shutil.copyfile(SKILL / part, tree / part)

            def check() -> subprocess.CompletedProcess[str]:
                return subprocess.run([sys.executable, "-B", str(tree / "config/check_policy_drift.py")], text=True,
                                      capture_output=True, timeout=180, check=False)

            passed = check()
            self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
            self.assertIn("twenty-two planted drifts were rejected", passed.stdout)
            module_path = tree / "scripts/pre_dispatch_deadlines.py"
            text = module_path.read_text(encoding="utf-8")
            module_path.write_text(text.replace('"max_seconds": 600,', '"max_seconds": 900,'), encoding="utf-8")
            reverted = check()
            self.assertEqual(reverted.returncode, 1, reverted.stdout)
            self.assertIn("scripts/pre_dispatch_deadlines.py SHIPPED_PRE_DISPATCH_SCALING is", reverted.stdout)


class InitFreezesTheRule(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-279-init-")
        self.root = pathlib.Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def test_a_new_audit_freezes_and_prints_the_rule(self) -> None:
        audit = pinit.make_audit(self.root / "new" / "audit")
        result = pinit.init(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(record_policy(audit)["phase_1"]["runtime"][LEAF], RULE)
        self.assertEqual(record_policy(audit)["phase_1"]["provider_free_preflight"]["maximum_seconds"], 600)
        self.assertIn("\n  pre-dispatch:      60 s + 1 s per 10000000 package bytes and per 5 files, at most 600 s\n",
                      result.stdout)

    def test_a_leftover_with_a_rule_no_runner_admits_is_refused_untouched(self) -> None:
        policy = pinit.shipped_policy()
        policy["phase_1"]["runtime"][LEAF] = {**RULE, "package_bytes_per_second": 1_000_000}
        data = pinit.envelope_bytes(policy)
        audit = pinit.make_audit(self.root / "odd" / "audit")
        (audit / pinit.SNAPSHOT).write_bytes(data)
        result = pinit.init(audit)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("freezes a pre-dispatch rule this skill does not run", result.stderr)
        self.assertEqual((audit / pinit.SNAPSHOT).read_bytes(), data)
        self.assertFalse((audit / "run.json").exists())

    def test_a_leftover_278_froze_is_refused_untouched_and_init_after_its_rm_freezes_the_rule(self) -> None:
        # the re-review's RC-2: a 2.7.8 init killed after its record_invocation.py --init-policy (or that command run
        # by hand on 2.7.8) leaves a snapshot without the rule; reused, the new audit kept 60 s for life, unwarned
        self.assertIsNone(BASE_278_PROBLEM, BASE_278_PROBLEM)
        audit = pinit.make_audit(self.root / "p278" / "audit")
        data = pinit.envelope_bytes(policy_278())
        if BASE_278 is not None:
            frozen = subprocess.run([sys.executable, "-B", str(BASE_278 / "scripts/record_invocation.py"),
                                     "--init-policy", str(audit)], text=True, capture_output=True, timeout=120,
                                    env=pinit.clean_env())
            self.assertEqual(frozen.returncode, 0, frozen.stdout + frozen.stderr)
            self.assertEqual((audit / pinit.SNAPSHOT).read_bytes(), data, "what 2.7.8 itself freezes")
        else:
            (audit / pinit.SNAPSHOT).write_bytes(data)
        result = pinit.init(audit)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("predates the pre-dispatch rule (phase_1.runtime.pre_dispatch_deadline_scaling, 2.7.9), so the "
                      "audit would keep the fixed 60 s pre-dispatch deadline", result.stderr)
        self.assertIn("remove it (rm ", result.stderr)
        self.assertEqual((audit / pinit.SNAPSHOT).read_bytes(), data)
        self.assertFalse((audit / "run.json").exists())
        (audit / pinit.SNAPSHOT).unlink()   # the remedy
        again = pinit.init(audit)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(record_policy(audit)["phase_1"]["runtime"][LEAF], RULE)

    @unittest.skipUnless(BASE_278, NO_BASE_278)
    def test_an_audit_whose_init_finished_on_278_is_refused_as_before_and_keeps_60_s(self) -> None:
        # the rule reaches only a snapshot with no run.json and no journal beside it: an audit 2.7.8 initialised is
        # refused by init as every initialised audit is, changes nothing, and Phase 1 still admits it at 60 s
        audit = pinit.make_audit(self.root / "done278" / "audit")
        done = pinit.init(audit, "--doi", "10.1234/abc.278", script=BASE_278 / "scripts/init_run.sh")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        before = {name: (audit / name).read_bytes() for name in (pinit.SNAPSHOT, "run.json", "run.events.jsonl")}
        self.assertNotIn(LEAF, record_policy(audit)["phase_1"]["runtime"])
        result = pinit.init(audit, "--doi", "10.1234/abc.278")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("already exists. init_run.sh runs ONCE per audit", result.stderr)
        self.assertNotIn("pre-dispatch rule", result.stderr)
        (audit / "run.json").unlink()   # run.json lost: the journal still makes it an initialised audit
        result = pinit.init(audit, "--doi", "10.1234/abc.278")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("is an initialised audit", result.stderr)
        self.assertNotIn("pre-dispatch rule", result.stderr)
        (audit / "run.json").write_bytes(before["run.json"])
        self.assertEqual({name: (audit / name).read_bytes() for name in before}, before)
        self.assertEqual(run_phase1.pre_dispatch_deadline(run_phase1.load_policy(audit), audit), 60)


if __name__ == "__main__":
    unittest.main()
