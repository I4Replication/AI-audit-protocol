"""2.6.8 fix A: the adversarial review of the snapshot-immutability change, pinned behaviourally.

Each class reproduces one reviewed defect through the real runners (the fake Codex and fake Claude
CLIs; no provider is ever called) and fails on the release candidate that had it:

* ``Phase1Inventory`` -- a file that lands in a drifted Phase-1 snapshot directory (a sync
  "conflicted copy") was sealed by the re-freeze and then read by every auditor and the verifier.
  The inventory is now checked before any chmod, by the library, the runner and the verifier.
* ``PrintedRemedies`` -- the only printed remedy was "delete the directory", which a plain
  ``rm -rf`` cannot do to 0555 directories. Every remedy is now the full command, and the tests
  run exactly what was printed.
* ``ProjectionNamesTheSnapshot`` -- with a round and a target snapshot both in play, the round
  projection's failure named neither.
* ``ReuseCorruption`` -- a damaged content address in MANIFEST.json was "a different launch" at
  reuse (exit 1) but an integrity fault before dispatch (exit 5).
* ``ModesThatKeepChanging`` -- a mode that would not stay re-frozen was reported as a content change
  ("changed during a provider call", delete the snapshot) although the content verified.
* ``VerifierDryRun`` -- ``--dry-run`` refused a mode-only drift that the real run repairs.
* ``NoRefreezeUnderTheDeadline`` -- the re-freeze walk ran under the SIGALRM pre-dispatch deadline,
  which could cut it short.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import phase1_runtime  # noqa: E402
import test_run_critique_runtime as critique  # noqa: E402  (the critique runtime helper)
import test_264_verifier_runtime as verifier_tests  # noqa: E402  (its Phase-1 fixture)
import test_snapshot_mode_drift as drift  # noqa: E402  (the real-round critique fixture)

DRIVER = SCRIPTS / "run_critique.sh"
FAKE_CODEX = TESTS / "fake_codex.sh"
REMEDY = re.compile(r"chmod -R u\+w (\S+) && rm -rf (\S+?)[`'\";,)]*(?:\s|$)")


def modes(root: pathlib.Path) -> dict[str, int]:
    return {path.relative_to(root).as_posix() or ".": stat.S_IMODE(path.lstat().st_mode)
            for path in [root, *root.rglob("*")]}


def run_printed_remedy(test: unittest.TestCase, text: str, cwd: pathlib.Path,
                       expected: str) -> None:
    """Run exactly the command a message printed, where it says to run it (the audit)."""
    match = REMEDY.search(text)
    test.assertIsNotNone(match, f"no full remedy command in: {text}")
    first, second = match.group(1), match.group(2).rstrip("`'\";,)")
    test.assertEqual((first, second), (expected, expected), text)
    target = cwd / expected
    test.assertTrue(target.is_dir(), target)
    # The command a frozen tree needs, not just rm: a plain rm -rf leaves it in place.
    plain = subprocess.run(["rm", "-rf", expected], cwd=cwd, capture_output=True, text=True)
    test.assertNotEqual(plain.returncode, 0, "a frozen snapshot resists a plain rm -rf")
    test.assertTrue(target.is_dir())
    result = subprocess.run(["bash", "-c", f"chmod -R u+w {first} && rm -rf {second}"], cwd=cwd,
                            capture_output=True, text=True)
    test.assertEqual(result.returncode, 0, result.stderr)
    test.assertFalse(target.exists(), "the printed remedy removed the snapshot")


# ------------------------------------------------------------------------------------ Phase 1
class Phase1Fixture(verifier_tests.VerifierFixture):
    def completed(self, name: str = "audit") -> tuple[pathlib.Path, pathlib.Path, dict]:
        root = self.audit_after_phase1(name)
        quorum = json.loads((root / "phase1/quorum.json").read_text())
        snapshot = root / "phase1/snapshots" / quorum["source_snapshot_sha256"]
        manifest = json.loads((snapshot / "snapshot_manifest.json").read_text())
        return root, snapshot, manifest

    def plant(self, directory: pathlib.Path, name: str = "data (conflicted copy).csv") -> pathlib.Path:
        directory.chmod(0o755)  # the File Provider drift that lets a new file land
        planted = directory / name
        planted.write_text("column\nplanted\n")
        return planted


class Phase1Inventory(Phase1Fixture):
    def test_a_file_planted_in_a_drifted_directory_is_refused_and_no_mode_is_touched(self) -> None:
        root, snapshot, manifest = self.completed()
        planted = self.plant(snapshot / "raw/paper")
        before = modes(snapshot)
        seen: list[dict] = []
        for label, kwargs in (("strict", {}), ("lenient", {"lenient_modes": True}),
                              ("refreeze", {"refreeze": True, "on_refreeze": seen.append})):
            with self.subTest(mode=label):
                with self.assertRaises(phase1_runtime.SnapshotIntegrityError) as caught:
                    phase1_runtime.verify_snapshot(snapshot, manifest, **kwargs)
                message = str(caught.exception)
                self.assertIn("immutable snapshot inventory differs: raw/ holds 1 file(s) the "
                              "manifest does not list", message)
                self.assertNotIn("conflicted", message, "counts only, never a stray name")
                self.assertNotIn("paper", message)
        self.assertEqual(modes(snapshot), before, "every mode left exactly as found")
        self.assertEqual(stat.S_IMODE(planted.stat().st_mode), 0o644, "the stray is not sealed")
        self.assertEqual(seen, [], "no re-freeze was reported")
        self.assertFalse((root / "phase1/.snapshot_refreeze.lock").exists(), "no lock was taken")

        # The verifier, dry run and real run alike, refuses it before any chmod or journal event.
        journal = self.journal_bytes(root)
        for extra in (["--dry-run"], []):
            with self.subTest(verifier=extra):
                refused = self.run_verifier(root, ["F13"], extra_args=extra)
                payload = self.assert_refused(refused, "verified snapshot mismatch")
                self.assertIn("inventory differs", payload["reason"])
                self.assertNotIn("conflicted", payload["reason"])
        self.assertEqual(modes(snapshot), before)
        self.assertEqual(self.journal_bytes(root), journal)

    def test_every_area_and_the_root_are_inventoried(self) -> None:
        root, snapshot, manifest = self.completed()
        cases = {
            "evidence": lambda: self.plant(snapshot / "evidence", "extra.json"),
            "context": lambda: self.plant(snapshot / "context", "extra.json"),
            "root": lambda: self.plant(snapshot, "notes.txt"),
            "stray directory": lambda: (
                (snapshot / "raw").chmod(0o755), (snapshot / "raw/empty").mkdir()),
        }
        expected = {
            "evidence": "evidence/ holds 1 file(s) the manifest does not list",
            "context": "context/ holds 1 file(s) the manifest does not list",
            "root": "the snapshot root holds 1 entry(ies) besides snapshot_manifest.json",
            "stray directory": "raw/ holds 1 directory(ies) that hold no listed file",
        }
        pristine = self.base / "pristine-snapshot"
        shutil.copytree(snapshot, pristine)
        for label, fault in cases.items():
            with self.subTest(area=label):
                drift.thaw(snapshot)
                shutil.rmtree(snapshot)
                shutil.copytree(pristine, snapshot)
                phase1_runtime.freeze_tree(snapshot)
                self.assertEqual(phase1_runtime.verify_snapshot(snapshot, manifest), ())
                fault()
                before = modes(snapshot)
                with self.assertRaises(phase1_runtime.SnapshotIntegrityError) as caught:
                    phase1_runtime.verify_snapshot(snapshot, manifest, refreeze=True)
                self.assertIn(expected[label], str(caught.exception))
                self.assertEqual(modes(snapshot), before)

    def test_the_runner_pauses_on_a_planted_file_instead_of_sealing_it(self) -> None:
        # During an auditor's call a raw/ directory drifts and a file lands in it. 2.6.8 before
        # fix A re-froze the directory, sealed the stray at 0444 and completed Phase 1 on it.
        root = self.make_audit("planted")
        result = self.run_phase1(root, "snapshot_planted_file")
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        status = json.loads((root / "phase1/status.json").read_text())
        self.assertEqual((status["status"], status["pause_reason"]),
                         ("paused", "snapshot_integrity_fault"))
        self.assertIn("inventory differs", json.dumps(status["environment_faults"]))
        self.assertNotIn("conflicted", json.dumps(status))
        snapshot = root / "phase1/snapshots" / status["source_snapshot_sha256"]
        planted = next(snapshot.rglob("data (conflicted copy).csv"))
        self.assertEqual(stat.S_IMODE(planted.stat().st_mode), 0o644, "never sealed")
        self.assertEqual(stat.S_IMODE(planted.parent.stat().st_mode), 0o755, "never re-frozen")
        rows = [row for row in self.ledger(root) if row["role"] == "auditor_reproduction"]
        self.assertEqual([(row["status"], row["error_class"]) for row in rows],
                         [("failed", "environment")])
        # The printed remedy, run as printed, clears it; the rerun rebuilds and completes.
        run_printed_remedy(self, status["remedy"], root,
                           f"phase1/snapshots/{status['source_snapshot_sha256']}")
        rerun = self.run_phase1(root)
        self.assertEqual(rerun.returncode, 0, rerun.stderr + rerun.stdout)
        self.assertTrue(snapshot.is_dir())
        self.assertFalse(any(snapshot.rglob("data (conflicted copy).csv")))

    def test_a_relaunch_on_a_planted_snapshot_pauses_preflight_with_the_full_remedy(self) -> None:
        root, snapshot, _manifest = self.completed("relaunch")
        self.plant(snapshot / "raw/pkg")
        before = modes(snapshot)
        result = self.run_phase1(root)
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        status = json.loads((root / "phase1/status.json").read_text())
        self.assertEqual(status["pause_reason"], "local_preflight_failed")
        self.assertIn("inventory differs", status["error"])
        self.assertEqual(modes(snapshot), before)
        run_printed_remedy(self, status["error"], root, f"phase1/snapshots/{snapshot.name}")


class Phase1ModesThatKeepChanging(Phase1Fixture):
    def test_bounded_refreezes_then_its_own_error_and_pause_reason(self) -> None:
        _root, snapshot, manifest = self.completed()
        pkg = snapshot / "raw/pkg"
        original = phase1_runtime.freeze_tree
        calls: list[int] = []

        def freeze_then_reset(path: pathlib.Path, deadline=None, *, times: int) -> None:
            original(path, deadline)
            calls.append(1)
            if len(calls) <= times:
                os.chmod(pkg, 0o755)  # the host puts the write bit straight back

        with mock.patch.object(phase1_runtime, "SNAPSHOT_REFREEZE_RETRY_SECONDS", 0):
            # One reset is repaired quietly by the next pass.
            pkg.chmod(0o755)
            seen: list[dict] = []
            with mock.patch.object(phase1_runtime, "freeze_tree",
                                   lambda p, d=None: freeze_then_reset(p, d, times=1)):
                self.assertEqual(phase1_runtime.verify_snapshot(
                    snapshot, manifest, refreeze=True, on_refreeze=seen.append), ("raw/pkg",))
            self.assertEqual(len(seen), 1)
            # A mode that keeps changing is not a content change.
            calls.clear()
            pkg.chmod(0o755)
            with mock.patch.object(phase1_runtime, "freeze_tree",
                                   lambda p, d=None: freeze_then_reset(p, d, times=99)):
                with self.assertRaises(phase1_runtime.SnapshotModeUnstableError) as caught:
                    phase1_runtime.verify_snapshot(snapshot, manifest, refreeze=True,
                                                   on_refreeze=seen.append)
        self.assertEqual(len(calls), phase1_runtime.SNAPSHOT_REFREEZE_ATTEMPTS)
        self.assertIsInstance(caught.exception, phase1_runtime.SnapshotIntegrityError)
        message = str(caught.exception)
        self.assertTrue(message.startswith("snapshot_mode_unstable:"), message)
        self.assertIn("content verifies", message)
        self.assertIn("stop the sync client", message.lower())
        self.assertNotIn("pkg", message)
        self.assertEqual(len(seen), 1, "no note for a re-freeze that did not hold")
        phase1_runtime.freeze_tree(snapshot)
        self.assertEqual(phase1_runtime.verify_snapshot(snapshot, manifest), ())


# ------------------------------------------------------------------------------------ verifier
class VerifierDryRun(Phase1Fixture):
    def test_the_dry_run_predicts_the_real_run_and_changes_nothing(self) -> None:
        root, snapshot, manifest = self.completed()
        (snapshot / "raw/pkg").chmod(0o755)
        (snapshot / "evidence").chmod(0o755)
        before, journal = modes(snapshot), self.journal_bytes(root)
        dry = self.run_verifier(root, ["F13"], extra_args=["--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr + dry.stdout)
        plan = json.loads(dry.stdout)
        self.assertEqual((plan["status"], plan["would_refreeze"]), ("dry_run", 2))
        self.assertEqual(modes(snapshot), before, "a dry run never chmods")
        self.assertEqual(self.journal_bytes(root), journal, "nor journals")
        self.assertFalse((root / "phase1/.snapshot_refreeze.lock").exists())
        done = self.run_verifier(root, ["F13"])
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertEqual(phase1_runtime.verify_snapshot(snapshot, manifest, lenient_modes=True), ())
        clean = self.run_verifier(root, ["F13"], extra_args=["--dry-run"])
        self.assertEqual(json.loads(clean.stdout)["would_refreeze"], 0)


# --------------------------------------------------------------------- the SIGALRM deadline
SITECUSTOMIZE = textwrap.dedent('''\
    """Test-only: record whether ITIMER_REAL is armed whenever a published snapshot is frozen."""
    import json, os, pathlib, signal, sys
    sys.path.insert(0, os.environ["I4R_TEST_SCRIPTS"])
    import phase1_runtime
    _original = phase1_runtime.freeze_tree

    def _recording(path, deadline=None):
        path = pathlib.Path(path)
        if path.parent.name == "snapshots" and not path.name.startswith("."):
            with open(os.environ["I4R_TEST_FREEZE_LOG"], "a") as handle:
                handle.write(json.dumps({
                    "timer_armed": signal.getitimer(signal.ITIMER_REAL)[0] > 0,
                    "argv0": pathlib.Path(sys.argv[0]).name,
                }) + "\\n")
        return _original(path, deadline)

    phase1_runtime.freeze_tree = _recording
    ''')


class NoRefreezeUnderTheDeadline(Phase1Fixture):
    def test_the_refreeze_walk_never_runs_while_the_sigalrm_deadline_is_armed(self) -> None:
        site = self.base / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
        log = self.base / "freeze.jsonl"
        extra = {"PYTHONPATH": str(site), "I4R_TEST_SCRIPTS": str(SCRIPTS),
                 "I4R_TEST_FREEZE_LOG": str(log)}
        root, snapshot, _manifest = self.completed()
        for runner in ("run_phase1.py", "run_verifier.py"):
            with self.subTest(runner=runner):
                log.unlink(missing_ok=True)
                (snapshot / "raw/paper").chmod(0o755)
                if runner == "run_phase1.py":
                    result = self.run_phase1(root, extra_env=extra)
                else:
                    result = self.run_verifier(root, ["F13"], extra_env=extra)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertTrue(calls, "the drift was re-frozen")
                self.assertEqual([call["timer_armed"] for call in calls], [False] * len(calls),
                                 "a SIGALRM could have interrupted the re-freeze walk")
                self.assertEqual(drift.writable(snapshot), [])
        notes = [event["payload"]["context"] for event in
                 verifier_tests.lifecycle_journal.load_events(root)
                 if event["type"] == "note"
                 and event["payload"].get("kind") == "snapshot_mode_refrozen"]
        self.assertEqual(notes, ["preflight", "verifier_bind_inputs"], "one note per re-freeze")


# ------------------------------------------------------------------------------ critique
class CritiqueDriverCase(unittest.TestCase):
    """A fresh critique audit per test, run through the real driver and the fake Codex CLI."""

    def setUp(self) -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="i4r-fix-a-")).resolve()
        self.addCleanup(self.cleanup)
        self.audit = self.work / "audit"
        drift.write_critique_audit(self.audit)
        self.state = self.work / "fake_state"
        self.round = self.audit / "critique/round_1"

    def cleanup(self) -> None:
        drift.thaw(self.work)
        shutil.rmtree(self.work, ignore_errors=True)

    def driver(self, invocation: str, scenario: str = "success", **env: str) -> tuple[int, str]:
        log = self.work / f"{invocation}.log"
        with log.open("w") as handle:
            result = subprocess.run(
                ["bash", str(DRIVER), "1"], stdout=handle, stderr=subprocess.STDOUT, check=False,
                env=drift.env_without_refreeze(
                    AUDIT_DIR=str(self.audit), CODEX=str(FAKE_CODEX),
                    FAKE_CODEX_STATE=str(self.state), FAKE_CODEX_SCENARIO=scenario,
                    I4R_INVOCATION_ID=invocation, RETRY_BACKOFF_SECONDS="0",
                    PROBE_LOCK_WAIT_SECONDS="3", **env),
                timeout=600,
            )
        return result.returncode, log.read_text()

    def status(self) -> dict:
        return json.loads((self.round / "ROUND_STATUS.json").read_text())

    def failures(self, invocation: str) -> dict[str, dict]:
        results = self.round / "invocations" / invocation / "results"
        return {path.name.split(".")[0]: json.loads(path.read_text())
                for path in sorted(results.glob("*.failure.json"))}

    def calls(self) -> int:
        path = self.state / "calls_total"
        return int(path.read_text().split()[0]) if path.is_file() else 0


class ReuseCorruption(CritiqueDriverCase):
    def test_a_damaged_content_address_is_corruption_at_reuse_too(self) -> None:
        # The probe's call damages the round snapshot's MANIFEST.json input_sha256: every critic
        # is refused before dispatch (an integrity fault) ...
        rc, log = self.driver("corrupt-one", FAKE_CORRUPT_MANIFEST="model_probe")
        self.assertNotEqual(rc, 0, log)
        snapshot_rel = self.status()["snapshot"]
        self.assertIn("[stop ] critic_stats: snapshot integrity check failed before dispatch", log)
        self.assertIn(f"remedy: in the audit directory run: chmod -R u+w {snapshot_rel} && "
                      f"rm -rf {snapshot_rel}", log)
        # ... and the relaunch, which reuses the directory, classifies it the same way: exit 5
        # from the helper and an integrity FINAL_DETAIL, not "a different launch".
        rc, log = self.driver("corrupt-two")
        self.assertNotEqual(rc, 0, log)
        self.assertIn("runtime environment error: corrupt snapshot manifest: round snapshot "
                      f"{snapshot_rel}", log)
        self.assertNotIn("different launch", log)
        detail = self.status()["detail"]
        self.assertIn(f"round snapshot {snapshot_rel} failed its integrity check", detail)
        self.assertNotIn("creation or verification failed", detail)
        # The printed remedy, exactly as printed, then a relaunch that completes on the cache.
        run_printed_remedy(self, detail, self.audit, snapshot_rel)
        rc, log = self.driver("corrupt-three")
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.status()["outcome"], "complete")
        self.assertEqual(self.calls(), 5, "one probe and four critics, the probe cached")

    def test_the_helper_splits_corruption_from_a_different_launch(self) -> None:
        rc, log = self.driver("clean")
        self.assertEqual(rc, 0, log)
        status = self.status()
        snapshot = self.audit / status["snapshot"]
        pristine = json.loads((snapshot / "MANIFEST.json").read_text())
        routing = self.round / "invocations/clean/ROUTING.json"

        def reuse_with(field: str, value) -> subprocess.CompletedProcess[str]:
            manifest = snapshot / "MANIFEST.json"
            manifest.chmod(0o644)
            manifest.write_text(json.dumps({**pristine, field: value}, indent=2, sort_keys=True))
            manifest.chmod(0o444)
            return subprocess.run(
                [sys.executable, str(SCRIPTS / "test_run_critique_runtime.py"), "snapshot",
                 "--workspace", str(self.audit), "--report", str(self.audit / "report/REPORT_final.md"),
                 "--prompts", str(SKILL / "prompts"), "--out", str(self.round),
                 "--model", pristine["model"], "--effort", pristine["effort"],
                 "--roles", " ".join(pristine["roles"]), "--routing", str(routing),
                 "--runtime-script", str(DRIVER), "--round", "1"],
                capture_output=True, text=True, env=drift.env_without_refreeze(), timeout=120)

        for field in ("input_sha256", "snapshot_version", "provider_schema_generation_version"):
            with self.subTest(corrupted=field):
                result = reuse_with(field, "f" * 64)
                self.assertEqual(result.returncode, 5, result.stderr)
                self.assertIn("runtime environment error: corrupt snapshot manifest: round "
                              f"snapshot {status['snapshot']}", result.stderr)
                self.assertIn(field, result.stderr)
        for field, value in (("model", "some-other-model"), ("effort", "low")):
            with self.subTest(launch=field):
                result = reuse_with(field, value)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("snapshot was built for a different launch", result.stderr)
                self.assertIn(f"chmod -R u+w {status['snapshot']} && rm -rf {status['snapshot']}",
                              result.stderr)
        self.assertEqual(reuse_with("model", pristine["model"]).returncode, 0)


class ProjectionNamesTheSnapshot(CritiqueDriverCase):
    def test_a_round_snapshot_changed_during_the_adjudicator_call_is_named(self) -> None:
        rc, log = self.driver("round-from-target", "adjudicate",
                              FAKE_TAMPER_ROUND_FROM="targeted_adjudicator")
        self.assertNotEqual(rc, 0, log)
        status = self.status()
        snapshot_rel = status["snapshot"]
        self.assertTrue(snapshot_rel.startswith("critique/round_1/snapshots/"), snapshot_rel)
        self.assertIn("runtime environment error: immutable file hash differs: "
                      f"source/REPORT_TO_REVIEW.md in round snapshot {snapshot_rel}", log)
        detail = status["detail"]
        self.assertIn(f"immutable round projection failed: round snapshot {snapshot_rel} failed "
                      "its integrity check", detail)
        self.assertNotIn("named in the driver log", detail)
        self.assertNotIn("target_snapshots", detail, "the target snapshot is intact")
        run_printed_remedy(self, detail, self.audit, snapshot_rel)
        rc, log = self.driver("round-from-target-two", "adjudicate")
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.status()["outcome"], "complete")


class PrintedRemedies(drift.CritiqueRoundFixture):
    def test_every_helper_remedy_is_the_full_command_and_it_works(self) -> None:
        for kind, snapshot in (("round", self.snapshot), ("target", self.target)):
            with self.subTest(kind=kind):
                relative = snapshot.relative_to(self.audit).as_posix()
                self.tamper(snapshot / "source/REPORT_TO_REVIEW.md")
                refused = self.verify_snapshot(kind)
                self.assertEqual(refused.returncode, 5, refused.stderr)
                self.assertIn(f"{kind} snapshot {relative}", refused.stderr)
                self.assertIn(f"chmod -R u+w {relative} && rm -rf {relative}", refused.stderr)
                run_printed_remedy(self, refused.stderr, self.audit, relative)
        self.assertEqual(critique.snapshot_delete_remedy(self.snapshot),
                         critique.SNAPSHOT_DELETE_REMEDY.format(
                             dir=self.snapshot.relative_to(self.audit).as_posix()))

    def test_integrity_messages_name_the_audit_relative_snapshot(self) -> None:
        manifest = self.manifest(self.snapshot)
        self.tamper(self.snapshot / "source/REPORT_TO_REVIEW.md")
        with self.assertRaises(critique.SnapshotIntegrityError) as caught:
            critique.verify_snapshot_integrity(self.snapshot, manifest)
        self.assertEqual(str(caught.exception),
                         "immutable file hash differs: source/REPORT_TO_REVIEW.md in round "
                         f"snapshot critique/round_1/snapshots/{self.input_sha}")
        extra = self.target / "source/extra.md"
        self.target.joinpath("source").chmod(0o755)
        extra.write_text("x\n")
        with self.assertRaises(critique.SnapshotIntegrityError) as caught:
            critique.verify_snapshot_integrity(self.target, self.manifest(self.target))
        self.assertEqual(str(caught.exception), "immutable file inventory differs: target "
                         f"snapshot critique/round_1/target_snapshots/{self.target_sha}")
        self.assertNotIn(str(self.audit), str(caught.exception))


class ModesThatKeepChanging(drift.CritiqueRoundFixture):
    def resetting(self, times: int):
        original = critique.make_read_only
        calls: list[int] = []

        def freeze_then_reset(root: pathlib.Path) -> None:
            original(root)
            calls.append(1)
            if len(calls) <= times:
                (root / "source").chmod(0o700)
        return calls, freeze_then_reset

    def test_bounded_refreezes_then_its_own_error_class(self) -> None:
        manifest = self.manifest(self.snapshot)
        with mock.patch.object(critique, "SNAPSHOT_REFREEZE_RETRY_SECONDS", 0):
            (self.snapshot / "source").chmod(0o700)
            calls, once = self.resetting(1)
            with mock.patch.object(critique, "make_read_only", once):
                self.assertEqual(critique.ensure_frozen(self.snapshot, manifest,
                                                        audit_root=self.audit, context="t",
                                                        refreeze=True), ["source"])
            self.assertEqual(len(calls), 2, "one reset is repaired by the next pass")
            self.assertEqual(len(self.notes("snapshot_mode_refrozen")), 1)
            (self.snapshot / "source").chmod(0o700)
            calls, always = self.resetting(99)
            with mock.patch.object(critique, "make_read_only", always):
                with self.assertRaises(critique.SnapshotModeUnstableError) as caught:
                    critique.ensure_frozen(self.snapshot, manifest, audit_root=self.audit,
                                           context="t", refreeze=True)
        self.assertEqual(len(calls), critique.SNAPSHOT_REFREEZE_ATTEMPTS)
        self.assertIsInstance(caught.exception, critique.SnapshotIntegrityError)
        message = str(caught.exception)
        self.assertTrue(message.startswith("snapshot_mode_unstable: round snapshot "
                                           f"critique/round_1/snapshots/{self.input_sha}"), message)
        self.assertIn("content verified", message)
        self.assertIn("stop the sync client", message)
        self.assertNotIn("rm -rf", message, "deleting cannot help while modes keep changing")
        self.assertEqual(len(self.notes("snapshot_mode_refrozen")), 1, "no note for a failed hold")

    def test_the_post_call_validator_exits_5_with_the_mode_unstable_token(self) -> None:
        attempt = self.invocation / "attempts/stats/1"
        (self.snapshot / "source").chmod(0o700)
        code = textwrap.dedent(f"""\
            import pathlib, sys
            sys.path.insert(0, {str(SCRIPTS)!r})
            import test_run_critique_runtime as runtime
            original = runtime.make_read_only
            def keeps_changing(root):
                original(root)
                (pathlib.Path(root) / "source").chmod(0o700)
            runtime.make_read_only = keeps_changing
            runtime.SNAPSHOT_REFREEZE_RETRY_SECONDS = 0
            sys.argv = ["helper"] + sys.argv[1:]
            raise SystemExit(runtime.main())
            """)
        result = subprocess.run(
            [sys.executable, "-c", code, "validate-critic", "--input", str(attempt / "response.json"),
             "--role", "stats", "--round", "1", "--input-sha", self.input_sha,
             "--report-sha", self.report_sha, "--audit-id", self.audit_id,
             "--schema", str(attempt / "output.schema.json"),
             "--markdown", str(self.work / "stats.md")],
            capture_output=True, text=True, timeout=120,
            env=drift.env_without_refreeze(I4R_SNAPSHOT_REFREEZE="1"))
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertRegex(result.stderr, r"(?m)^runtime environment error: snapshot_mode_unstable: ")


MODE_STORM_SITECUSTOMIZE = textwrap.dedent('''\
    """Test-only host that keeps re-opening a snapshot's `source` directory (content untouched).

    Once the fake Codex names its workspace in $I4R_TEST_MODE_STORM (from the storming role's call
    on), every freeze of that snapshot -- make_read_only ends with the root's chmod 0555 -- is followed
    at once by a chmod 0700 of its `source`, so no re-freeze ever sticks. Deterministic, unlike the
    busy-loop process it replaced, which could lose the race to a re-freeze on a loaded host."""
    import os, pathlib
    _marker = pathlib.Path(os.environ["I4R_TEST_MODE_STORM"])
    _chmod = pathlib.Path.chmod

    def _storming_chmod(self, mode, *args, **kwargs):
        _chmod(self, mode, *args, **kwargs)
        if mode != 0o555:
            return
        try:
            target = _marker.read_text().strip()
        except OSError:
            return
        if target and os.path.realpath(self) == target and os.path.isdir(os.path.join(target, "source")):
            os.chmod(os.path.join(target, "source"), 0o700)

    pathlib.Path.chmod = _storming_chmod
    ''')


class ModeStormThroughTheDriver(CritiqueDriverCase):
    """`source` keeps regaining a write bit from one role's call on (content untouched): the
    fake Codex starts it and a sitecustomize hook in every Python process of the run re-opens it
    after each freeze (MODE_STORM_SITECUSTOMIZE)."""

    def storm(self, invocation: str, role: str, **env: str) -> tuple[int, str]:
        site = self.work / "storm_site"
        site.mkdir(exist_ok=True)
        (site / "sitecustomize.py").write_text(MODE_STORM_SITECUSTOMIZE)
        rc, log = self.driver(invocation, FAKE_SNAPSHOT_MODE_STORM=role, PYTHONPATH=str(site),
                              I4R_TEST_MODE_STORM=str(self.state / "mode_storm.active"), **env)
        self.assertTrue((self.state / "mode_storm.active").is_file(), f"the storm never started\n{log}")
        return rc, log

    def assert_worded_as_modes(self, invocation: str, detail: str) -> dict[str, dict]:
        failures = self.failures(invocation)
        snapshot_classes = {name: failure["failure_class"] for name, failure in failures.items()
                            if failure["failure_class"].startswith("snapshot")}
        self.assertTrue(snapshot_classes, failures)
        self.assertEqual(set(snapshot_classes.values()), {"snapshot_mode_unstable"}, failures)
        self.assertIn("modes kept changing", detail)
        self.assertIn("stop the sync client", detail)
        self.assertNotIn("changed during a provider call", detail)
        self.assertNotIn("rm -rf", detail, "deleting the snapshot cannot help")
        return failures

    def test_modes_changing_from_the_probe_on_stop_every_critic_as_mode_unstable(self) -> None:
        rc, log = self.storm("storm-probe", "model_probe")
        self.assertNotEqual(rc, 0, log)
        status = self.status()
        self.assertEqual(status["outcome"], "failed", status)
        self.assert_worded_as_modes("storm-probe", status["detail"])
        self.assertIn("snapshot modes keep changing (content verified)", log)
        ledger = [json.loads(line) for line in (self.audit / "invocations.jsonl").read_text().splitlines()
                  if line.strip()]
        for row in ledger:
            if row["role"] != "model_probe":
                self.assertEqual((row["status"], row.get("error_class")), ("failed", "environment"),
                                 row)

    def test_a_paid_response_is_not_reported_as_changed_when_only_modes_moved(self) -> None:
        rc, log = self.storm("storm-stats", "stats", PARALLEL="1")
        self.assertNotEqual(rc, 0, log)
        status = self.status()
        failures = self.assert_worded_as_modes("storm-stats", status["detail"])
        self.assertEqual(failures["stats"]["failure_class"], "snapshot_mode_unstable")
        self.assertIn("[fail ] critic_stats: snapshot modes kept changing after the provider call "
                      "(content verified; environment, not the critic); response kept", log)
        self.assertTrue((self.round / "invocations/storm-stats/attempts/stats/1/response.json")
                        .is_file(), "the paid response is kept on disk")
        ledger = [json.loads(line) for line in (self.audit / "invocations.jsonl").read_text().splitlines()
                  if line.strip()]
        stats = [row for row in ledger if row["role"] == "critic_stats"]
        self.assertEqual([(row["status"], row.get("error_class")) for row in stats],
                         [("failed", "environment")], "one call, never re-sampled")


if __name__ == "__main__":
    unittest.main()
