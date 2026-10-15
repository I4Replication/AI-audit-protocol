"""2.6.8: a frozen snapshot is proven intact before every paid dispatch, and mode drift is repaired.

On 2026-09-27 the Dropbox File Provider gave a freshly frozen critique snapshot's ``source``
directory mode 0700 seconds after the freeze. Every file stayed 0444 and every hash matched. The
only immutability check ran after each paid call, reported the drift as a schema failure, and the
retry loop re-dispatched twelve calls against the same snapshot; the adjudicator's target snapshot
hit the same race.

These tests pin the repair on real artefacts: one critique round is run through the real driver
with the fake Codex CLI (a round snapshot, a target snapshot and valid critic and adjudicator
responses), and every test works on its own copy of that audit.

* ``ensure_frozen`` tells mode drift from content drift. Opted in, it re-freezes the first under a
  lock and journals exactly one note from the process that ran chmod; the second raises
  ``SnapshotIntegrityError`` and leaves every mode as it found it, with no note.
* Opted out (the library default, and what recovery scripts and compatibility sweeps rely on) it
  is today's strict check, message and all, and changes nothing on disk.
* The driver's pre-dispatch command ``verify-snapshot`` exits 0 on a clean or re-frozen snapshot
  and 5 on an integrity fault, for round and target snapshots alike; the post-call validators exit
  5, not 1, when the snapshot's content changed, so the driver can tell a host fault from a
  schema-invalid response.
* Coordinator recovery and the post-fan-out verifier follow the same rule.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
HELPER = SCRIPTS / "test_run_critique_runtime.py"
DRIVER = SCRIPTS / "run_critique.sh"
FAKE_CODEX = TESTS / "fake_codex.sh"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import test_run_critique_runtime as runtime  # the critique runtime helper, after the path insert
import test_264_verifier_runtime as verifier_tests  # its fixture; it imports lazily

REFREEZE_ENV = "I4R_SNAPSHOT_REFREEZE"
INVOCATION = "drift-base"


def module(name: str):
    """Resolve a scripts module at use: later suites swap some sys.modules entries."""
    return importlib.import_module(name)


def mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def writable(root: pathlib.Path) -> list[str]:
    return [path.relative_to(root).as_posix() or "." for path in [root, *root.rglob("*")]
            if path.lstat().st_mode & 0o222]


def thaw(root: pathlib.Path) -> None:
    for path in [root, *root.rglob("*")]:
        try:
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)
        except OSError:
            pass


def env_without_refreeze(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != REFREEZE_ENV}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def write_critique_audit(audit: pathlib.Path) -> None:
    """The fixture audit of tests/test_run_critique.sh (make_audit), as metadata 2.1."""
    (audit / "report").mkdir(parents=True)
    (audit / "work").mkdir()
    (audit / "PAPER.txt").write_text("Example et al. (2026), deterministic runtime fixture.\n")
    (audit / "ENV.txt").write_text("python3 standard-library-only fixture\n")
    (audit / "SLOW.txt").write_text("Do not run any long job.\n")
    (audit / "WRAPUP.txt").write_text("Review the current changed claims and their dependencies.\n")
    report = audit / "report/REPORT_final.md"
    report.write_text("# Fixture report\n\nThe immutable audit draft contains one bounded claim.\n")
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    finding = "F" + "1"  # the shell fixture's single claim id
    row = {
        "schema_version": "2.0.0", "id": finding, "claim": "The bounded fixture claim is correct.",
        "domain": "statistics",
        "source_location": {"path": "report/REPORT_final.md", "locator": "fixture claim"},
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
        "remedy": {"class": "one_number", "description": "Retain the bounded value.",
                   "verified": None},
        "changes_conclusion": False, "changes_conclusion_which": None,
        "provenance": {"origin_role": "fixture", "independently_found_by": ["fixture"],
                       "agreed_after_seeing": [], "round_introduced": 0,
                       "source_records": ["report/REPORT_final.md"]},
    }
    (audit / "work/findings.jsonl").write_text(json.dumps(row, sort_keys=True) + "\n")
    for command in (
        [sys.executable, str(SCRIPTS / "record_invocation.py"), "--init-policy", str(audit)],
        ["bash", str(SCRIPTS / "init_run.sh"), str(audit), "--audit-id", "audit-drift-21",
         "--protocol-version", "test", "--gate-set-version", "test"],
    ):
        result = subprocess.run(command, capture_output=True, text=True, env=env_without_refreeze(),
                                check=False)
        if result.returncode != 0:
            raise AssertionError(f"fixture command failed: {command[1]}\n{result.stderr}")


class CritiqueRoundFixture(unittest.TestCase):
    """One real critique round (fake Codex), copied afresh for every test."""

    base: pathlib.Path
    pristine: pathlib.Path
    temporary: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="i4r-snapshot-drift-")
        cls.base = pathlib.Path(cls.temporary.name).resolve()
        cls.pristine = cls.base / "pristine"
        write_critique_audit(cls.pristine)
        log = cls.base / "driver.log"
        with log.open("w") as handle:
            result = subprocess.run(
                ["bash", str(DRIVER), "1"], stdout=handle, stderr=subprocess.STDOUT, check=False,
                env=env_without_refreeze(
                    AUDIT_DIR=str(cls.pristine), CODEX=str(FAKE_CODEX),
                    FAKE_CODEX_STATE=str(cls.base / "fake_state"), FAKE_CODEX_SCENARIO="adjudicate",
                    I4R_INVOCATION_ID=INVOCATION, RETRY_BACKOFF_SECONDS="0",
                    PROBE_LOCK_WAIT_SECONDS="3",
                ),
                timeout=600,
            )
        if result.returncode != 0:
            cls.tearDownClass()
            raise AssertionError(f"fixture round failed:\n{log.read_text()[-4000:]}")

    @classmethod
    def tearDownClass(cls) -> None:
        thaw(cls.base)
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="case-", dir=self.base))
        self.addCleanup(lambda: (thaw(self.work), shutil.rmtree(self.work, ignore_errors=True)))
        self.audit = self.work / "audit"
        shutil.copytree(self.pristine, self.audit, symlinks=True)
        self.round = self.audit / "critique/round_1"
        status = json.loads((self.round / "ROUND_STATUS.json").read_text())
        self.assertEqual((status["outcome"], status["adjudication"]), ("complete", "complete"))
        self.input_sha = status["input_sha256"]
        self.report_sha = status["report_sha256"]
        self.audit_id = status["audit_id"]
        self.snapshot = self.round / "snapshots" / self.input_sha
        self.invocation = self.round / "invocations" / INVOCATION
        meta = json.loads((self.invocation / "target_snapshot.json").read_text())
        self.target_sha = meta["target_input_sha256"]
        self.target = self.round / "target_snapshots" / self.target_sha
        for tree in (self.snapshot, self.target):
            self.assertEqual(writable(tree), [], "the fixture's snapshots start frozen")

    # ------------------------------------------------------------------------------ helpers
    def manifest(self, snapshot: pathlib.Path) -> dict:
        return json.loads((snapshot / "MANIFEST.json").read_text())

    def journal(self) -> bytes:
        return (self.audit / "run.events.jsonl").read_bytes()

    def notes(self, kind: str) -> list[dict]:
        return [event["payload"] for event in module("lifecycle_journal").load_events(self.audit)
                if event["type"] == "note" and event["payload"].get("kind") == kind]

    def tamper(self, path: pathlib.Path) -> None:
        """A genuine content change that restores the file's own frozen mode afterwards."""
        path.chmod(0o644)
        with path.open("a") as handle:
            handle.write("tampered\n")
        path.chmod(0o444)

    def helper(self, *args: str, refreeze: bool) -> subprocess.CompletedProcess[str]:
        extra = {REFREEZE_ENV: "1"} if refreeze else {}
        return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True,
                              env=env_without_refreeze(**extra), check=False, timeout=120)

    def verify_snapshot(self, kind: str, *, snapshot: pathlib.Path | None = None,
                        expected: str | None = None) -> subprocess.CompletedProcess[str]:
        snapshot = snapshot or (self.snapshot if kind == "round" else self.target)
        expected = expected or (self.input_sha if kind == "round" else self.target_sha)
        return self.helper(
            "verify-snapshot", "--workspace", str(self.audit), "--snapshot", str(snapshot),
            "--kind", kind, "--expected-input-sha", expected, "--round", "1",
            "--context", "critic_stats" if kind == "round" else "critic_synth",
            "--invocation", "drift-check", "--runtime-script", str(DRIVER), refreeze=False,
        )

    def validate_critic(self, *, refreeze: bool) -> subprocess.CompletedProcess[str]:
        attempt = self.invocation / "attempts/stats/1"
        return self.helper(
            "validate-critic", "--input", str(attempt / "response.json"), "--role", "stats",
            "--round", "1", "--input-sha", self.input_sha, "--report-sha", self.report_sha,
            "--audit-id", self.audit_id, "--schema", str(attempt / "output.schema.json"),
            "--markdown", str(self.work / "stats.md"), refreeze=refreeze,
        )

    def validate_adjudication(self, *, refreeze: bool) -> subprocess.CompletedProcess[str]:
        attempt = self.invocation / "attempts/targeted_adjudicator/1"
        return self.helper(
            "validate-adjudication", "--input", str(attempt / "response.json"),
            "--packet", str(self.invocation / "adjudication_packet.json"),
            "--input-sha", self.target_sha, "--schema", str(attempt / "output.schema.json"),
            "--markdown", str(self.work / "adjudication.md"), refreeze=refreeze,
        )

    def assert_note_is_private(self, note: dict) -> None:
        encoded = json.dumps(note)
        self.assertNotIn(str(self.audit), encoded, "no absolute path in a journaled note")
        self.assertNotIn(str(self.base), encoded)
        self.assertFalse(any(part.startswith("/") for part in note.get("paths", [])))
        # Depth-limited: at most two levels, and never a file of the snapshot.
        for path in note.get("paths", []):
            self.assertLessEqual(len(path.split("/")), 2, path)
            self.assertNotIn(".", path.split("/")[-1].strip("."), path)


class EnsureFrozen(CritiqueRoundFixture):
    def test_mode_drift_is_refrozen_in_place_and_journaled_once(self) -> None:
        before_hashes = self.manifest(self.snapshot)["files_sha256"]
        (self.snapshot / "source").chmod(0o700)  # the File Provider shape, nothing else changed
        refrozen = runtime.ensure_frozen(self.snapshot, self.manifest(self.snapshot),
                                         audit_root=self.audit, context="post_call:critic_stats",
                                         refreeze=True, invocation="drift-check")
        self.assertEqual(refrozen, ["source"])
        self.assertEqual(writable(self.snapshot), [])
        self.assertEqual(mode(self.snapshot / "source"), 0o555)
        runtime.verify_read_only_inventory(self.snapshot, self.manifest(self.snapshot))
        # A chmod changes no byte: the content address and every file hash are what they were.
        self.assertEqual(self.manifest(self.snapshot)["files_sha256"], before_hashes)
        self.assertEqual(runtime.hash_tree(self.snapshot / "source",
                                           runtime.round_identity_parts(self.manifest(self.snapshot))),
                         self.input_sha)
        notes = self.notes("snapshot_mode_refrozen")
        self.assertEqual(len(notes), 1, notes)
        note = notes[0]
        self.assertEqual(note["snapshot"], f"critique/round_1/snapshots/{self.input_sha}")
        self.assertEqual((note["snapshot_kind"], note["round"], note["context"]),
                         ("round", 1, "post_call:critic_stats"))
        self.assertEqual((note["path_count"], note["paths"], note["invocation"]),
                         (1, ["source"], "drift-check"))
        self.assert_note_is_private(note)
        # A second look finds nothing to do and journals nothing.
        self.assertEqual(runtime.ensure_frozen(self.snapshot, self.manifest(self.snapshot),
                                               audit_root=self.audit, context="again",
                                               refreeze=True), [])
        self.assertEqual(len(self.notes("snapshot_mode_refrozen")), 1)

    def test_concurrent_checks_refreeze_once_and_journal_once(self) -> None:
        # Four critic workers validate against one snapshot at the same moment.
        (self.snapshot / "source").chmod(0o700)
        (self.snapshot / "source/context").chmod(0o755)
        go = self.work / "go"
        child = (
            "import json, pathlib, sys, time\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "import test_run_critique_runtime as runtime\n"
            "snapshot, audit, go = map(pathlib.Path, sys.argv[1:4])\n"
            "manifest = json.loads((snapshot / 'MANIFEST.json').read_text())\n"
            "while not go.exists():\n"
            "    time.sleep(0.001)\n"
            "print(json.dumps(runtime.ensure_frozen(snapshot, manifest, audit_root=audit,\n"
            "                 context='concurrency', refreeze=True)))\n"
        )
        workers = [subprocess.Popen(
            [sys.executable, "-c", child, str(self.snapshot), str(self.audit), str(go)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env_without_refreeze(),
        ) for _ in range(4)]
        time.sleep(0.5)
        go.write_text("go\n")
        results = [worker.communicate(timeout=120) for worker in workers]
        self.assertEqual([worker.returncode for worker in workers], [0] * 4,
                         [stderr for _out, stderr in results])
        refrozen = [json.loads(out) for out, _err in results]
        self.assertEqual(sum(1 for paths in refrozen if paths), 1, refrozen)
        self.assertEqual(sorted(next(paths for paths in refrozen if paths)),
                         ["source", "source/context"])
        self.assertEqual(len(self.notes("snapshot_mode_refrozen")), 1)
        self.assertEqual(writable(self.snapshot), [])
        lock = self.round / ".snapshot_refreeze.lock"
        self.assertTrue(lock.is_file() and not lock.is_symlink(), "a regular lock file, not a dir")

    def test_the_strict_default_is_todays_check_and_changes_nothing(self) -> None:
        (self.snapshot / "source").chmod(0o700)
        before = self.journal()
        for value in (None, "0", ""):
            with self.subTest(env=value), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(REFREEZE_ENV, None)
                if value is not None:
                    os.environ[REFREEZE_ENV] = value
                for check in (
                    lambda: runtime.ensure_frozen(self.snapshot, self.manifest(self.snapshot),
                                                  audit_root=self.audit, context="strict"),
                    lambda: runtime.verify_read_only_inventory(self.snapshot,
                                                               self.manifest(self.snapshot)),
                ):
                    with self.assertRaises(ValueError) as caught:
                        check()
                    self.assertNotIsInstance(caught.exception, runtime.SnapshotIntegrityError)
                    self.assertEqual(str(caught.exception), (
                        f"immutable path regained write permission: {self.snapshot / 'source'}\n"
                        "  A snapshot is frozen before it is published, so this means something "
                        "changed its mode afterwards -- a synced folder re-applying permissions is "
                        "the usual cause.\n"
                        "  Remedy: chmod -R a-w the path above, or delete it (it is "
                        "content-addressed and will be rebuilt identically)."))
        self.assertEqual(mode(self.snapshot / "source"), 0o700, "strict mode never chmods")
        self.assertEqual(self.journal(), before, "strict mode never journals")
        self.assertFalse((self.round / ".snapshot_refreeze.lock").exists())
        # The command-line default is the same: a post-call validator without the opt-in fails
        # the way 2.6.7 did (exit 1, the mode message) and leaves the drift for the operator.
        result = self.validate_critic(refreeze=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("runtime error: immutable path regained write permission", result.stderr)
        self.assertEqual(mode(self.snapshot / "source"), 0o700)
        self.assertEqual(self.journal(), before)

    def test_content_faults_raise_integrity_errors_and_touch_no_mode(self) -> None:
        def symlink(snapshot: pathlib.Path) -> None:
            (snapshot / "source/linked.md").symlink_to("REPORT_TO_REVIEW.md")

        def extra_file(snapshot: pathlib.Path) -> None:
            extra = snapshot / "source/extra.md"
            extra.write_text("not in the manifest\n")
            extra.chmod(0o444)

        def changed_byte(snapshot: pathlib.Path) -> None:
            self.tamper(snapshot / "source/REPORT_TO_REVIEW.md")

        cases = {"symlink inside immutable snapshot": symlink,
                 "immutable file inventory differs": extra_file,
                 "immutable file hash differs": changed_byte}
        for index, (message, fault) in enumerate(cases.items()):
            with self.subTest(fault=fault.__name__):
                if index:
                    self.setUp()
                source = self.snapshot / "source"
                source.chmod(0o700)  # drifted AND faulted: the drift must not be repaired
                fault(self.snapshot)
                before = self.journal()
                with self.assertRaises(runtime.SnapshotIntegrityError) as caught:
                    runtime.ensure_frozen(self.snapshot, self.manifest(self.snapshot),
                                          audit_root=self.audit, context="post_call:critic_stats",
                                          refreeze=True)
                self.assertIsInstance(caught.exception, ValueError)
                self.assertIn(message, str(caught.exception))
                self.assertEqual(mode(source), 0o700, "left exactly as found, for inspection")
                self.assertEqual(self.journal(), before)
                self.assertEqual(self.notes("snapshot_mode_refrozen"), [])
                self.assertFalse((self.round / ".snapshot_refreeze.lock").exists())


class PreDispatchCommand(CritiqueRoundFixture):
    def test_verify_snapshot_exit_codes_for_round_and_target_snapshots(self) -> None:
        for kind, snapshot in (("round", self.snapshot), ("target", self.target)):
            with self.subTest(kind=kind):
                clean = self.verify_snapshot(kind)
                self.assertEqual(clean.returncode, 0, clean.stderr)
                self.assertEqual(json.loads(clean.stdout)["status"], "clean")

                (snapshot / "source").chmod(0o700)
                drift = self.verify_snapshot(kind)
                self.assertEqual(drift.returncode, 0, drift.stderr)
                self.assertEqual(json.loads(drift.stdout),
                                 {"refrozen_paths": 1, "snapshot_kind": kind, "status": "refrozen"})
                self.assertIn("[note ] snapshot mode drift re-frozen", drift.stderr)
                self.assertEqual(writable(snapshot), [])
                notes = [n for n in self.notes("snapshot_mode_refrozen") if n["snapshot_kind"] == kind]
                self.assertEqual(len(notes), 1, notes)
                self.assertEqual(notes[0]["invocation"], "drift-check")
                self.assert_note_is_private(notes[0])

                # Drift plus a content change: exit 5, no chmod, a refusal note, not a re-freeze.
                (snapshot / "source").chmod(0o700)
                self.tamper(snapshot / "source/REPORT_TO_REVIEW.md")
                before_refrozen = len(self.notes("snapshot_mode_refrozen"))
                refused = self.verify_snapshot(kind)
                self.assertEqual(refused.returncode, 5, refused.stderr)
                self.assertIn("runtime environment error:", refused.stderr)
                self.assertIn("rebuilt identically", refused.stderr)
                self.assertEqual(mode(snapshot / "source"), 0o700)
                self.assertEqual(len(self.notes("snapshot_mode_refrozen")), before_refrozen)
                refusals = [n for n in self.notes("snapshot_integrity_refused")
                            if n["snapshot_kind"] == kind]
                self.assertEqual(len(refusals), 1, refusals)
                self.assert_note_is_private(refusals[0])

    def test_verify_snapshot_refuses_a_wrong_address_and_rejects_bad_arguments(self) -> None:
        other = "f" * 64
        wrong = self.verify_snapshot("round", expected=other)
        self.assertEqual(wrong.returncode, 5, wrong.stderr)
        missing = self.verify_snapshot("round", snapshot=self.round / "snapshots" / other,
                                       expected=other)
        self.assertEqual(missing.returncode, 5, missing.stderr)
        malformed = self.verify_snapshot("round", expected="not-a-digest")
        self.assertEqual(malformed.returncode, 1, malformed.stderr)
        self.assertIn("runtime error:", malformed.stderr)
        # A snapshot whose MANIFEST.json is gone is an integrity fault, not a crash.
        manifest = self.snapshot / "MANIFEST.json"
        self.snapshot.chmod(0o755)
        manifest.unlink()
        self.snapshot.chmod(0o555)
        gone = self.verify_snapshot("round")
        self.assertEqual(gone.returncode, 5, gone.stderr)
        self.assertIn("no MANIFEST.json", gone.stderr)


class PostCallValidators(CritiqueRoundFixture):
    def test_validate_critic_keeps_a_drifted_response_and_exits_5_on_changed_content(self) -> None:
        self.assertEqual(self.validate_critic(refreeze=True).returncode, 0)
        (self.snapshot / "source").chmod(0o700)
        drift = self.validate_critic(refreeze=True)
        self.assertEqual(drift.returncode, 0, drift.stderr)
        self.assertEqual(writable(self.snapshot), [])
        notes = self.notes("snapshot_mode_refrozen")
        self.assertEqual([(n["context"], n["invocation"]) for n in notes],
                         [("post_call:critic_stats", INVOCATION)])
        self.tamper(self.snapshot / "source/REPORT_TO_REVIEW.md")
        tampered = self.validate_critic(refreeze=True)
        self.assertEqual(tampered.returncode, 5, tampered.stderr)
        self.assertIn("runtime environment error: immutable file hash differs", tampered.stderr)

    def test_validate_adjudication_exits_5_when_the_target_snapshot_changed(self) -> None:
        self.assertEqual(self.validate_adjudication(refreeze=True).returncode, 0)
        (self.target / "source").chmod(0o700)
        drift = self.validate_adjudication(refreeze=True)
        self.assertEqual(drift.returncode, 0, drift.stderr)
        self.assertEqual(writable(self.target), [])
        self.tamper(self.target / "source/REPORT_TO_REVIEW.md")
        tampered = self.validate_adjudication(refreeze=True)
        self.assertEqual(tampered.returncode, 5, tampered.stderr)
        self.assertIn("runtime environment error:", tampered.stderr)

    def test_snapshot_reuse_refreezes_a_relaunch_and_refuses_changed_content(self) -> None:
        # The relaunch path: `snapshot` on an existing content-addressed snapshot.
        def rebuild() -> subprocess.CompletedProcess[str]:
            routing = self.invocation / "ROUTING.json"
            return self.helper(
                "snapshot", "--workspace", str(self.audit),
                "--report", str(self.audit / "report/REPORT_final.md"),
                "--prompts", str(SKILL / "prompts"), "--out", str(self.round),
                "--model", self.manifest(self.snapshot)["model"],
                "--effort", self.manifest(self.snapshot)["effort"],
                "--roles", " ".join(self.manifest(self.snapshot)["roles"]),
                "--routing", str(routing), "--runtime-script", str(DRIVER), "--round", "1",
                refreeze=True,
            )
        self.assertEqual(rebuild().returncode, 0)
        (self.snapshot / "source").chmod(0o700)
        drift = rebuild()
        self.assertEqual(drift.returncode, 0, drift.stderr)
        self.assertEqual(json.loads(drift.stdout)["input_sha256"], self.input_sha)
        self.assertEqual(writable(self.snapshot), [])
        self.assertEqual([n["context"] for n in self.notes("snapshot_mode_refrozen")],
                         ["snapshot_reuse"])
        self.tamper(self.snapshot / "source/REPORT_TO_REVIEW.md")
        tampered = rebuild()
        self.assertEqual(tampered.returncode, 5, tampered.stderr)
        self.assertIn("no longer matches its content address", tampered.stderr)


class CoordinatorRecoverySnapshot(unittest.TestCase):
    """recover_coordinator.validate_snapshot_tree: integrity first, then modes."""

    def setUp(self) -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="i4r-coordinator-drift-")).resolve()
        self.addCleanup(lambda: (thaw(self.work), shutil.rmtree(self.work, ignore_errors=True)))
        self.source = self.work / "attempt/source"
        (self.source / "report").mkdir(parents=True)
        report = self.source / "report/draft.md"
        report.write_text("# Draft\n\nA bounded coordinator input.\n")
        self.manifest = {"files": [{"snapshot": "report/draft.md", "bytes": report.stat().st_size,
                                    "sha256": hashlib.sha256(report.read_bytes()).hexdigest()}]}
        (self.source / "input_manifest.json").write_text(json.dumps(self.manifest) + "\n")
        for path in sorted([self.source, *self.source.rglob("*")], key=lambda p: -len(p.parts)):
            path.chmod(0o555 if path.is_dir() else 0o444)

    def validate(self, refreeze: str | None) -> None:
        recover = module("recover_coordinator")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(REFREEZE_ENV, None)
            if refreeze is not None:
                os.environ[REFREEZE_ENV] = refreeze
            recover.validate_snapshot_tree(self.source, self.manifest)

    def test_mode_drift_is_strict_by_default_and_refrozen_when_opted_in(self) -> None:
        error = module("run_coordinator").CoordinatorError
        (self.source / "report").chmod(0o755)
        with self.assertRaises(error) as caught:
            self.validate(None)
        self.assertIn("immutable snapshot entry is writable: report", str(caught.exception))
        self.assertIn("I4R_SNAPSHOT_REFREEZE=1", str(caught.exception))
        self.assertEqual(mode(self.source / "report"), 0o755, "strict mode never chmods")
        self.source.chmod(0o755)
        with self.assertRaises(error) as caught:
            self.validate(None)
        self.assertIn("immutable snapshot root is unsafe or writable", str(caught.exception))
        self.validate("1")
        self.assertEqual(writable(self.source), [])
        self.validate(None)  # and the strict check passes on the re-frozen tree

    def test_a_content_change_is_refused_and_no_mode_is_touched(self) -> None:
        error = module("run_coordinator").CoordinatorError
        report = self.source / "report/draft.md"
        (self.source / "report").chmod(0o755)
        report.chmod(0o644)
        report.write_text("# Draft\n\nA different input.\n")
        with self.assertRaises(error) as caught:
            self.validate("1")
        self.assertIn("immutable snapshot changed", str(caught.exception))
        self.assertEqual((mode(self.source / "report"), mode(report)), (0o755, 0o644))


class VerifierOnADriftedPhase1Snapshot(verifier_tests.VerifierFixture):
    """The verifier binds a days-old Phase-1 snapshot, the likeliest one to have drifted."""

    def test_mode_drift_is_refrozen_once_but_never_under_dry_run(self) -> None:
        root = self.audit_after_phase1()
        quorum = json.loads((root / "phase1/quorum.json").read_text())
        snapshot = root / "phase1/snapshots" / quorum["source_snapshot_sha256"]
        paper = snapshot / "raw/paper"
        paper.chmod(0o755)
        (paper / "paper.txt").chmod(0o644)
        before = self.journal_bytes(root)

        # Fix A: the dry run predicts the real run. The content is verified strictly, the drift
        # is counted as would_refreeze, and it exits 0 -- without a chmod or a journal event.
        dry = self.run_verifier(root, ["F13"], extra_args=["--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr + dry.stdout)
        plan = json.loads(dry.stdout)
        self.assertEqual((plan["status"], plan["would_refreeze"]), ("dry_run", 2))
        self.assertEqual((mode(paper), mode(paper / "paper.txt")), (0o755, 0o644),
                         "a dry run neither chmods")
        self.assertEqual(self.journal_bytes(root), before, "nor journals")

        done = self.run_verifier(root, ["F13"])
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertEqual(writable(snapshot), [])
        events = [event for event in module("lifecycle_journal").load_events(root)
                  if event["type"] == "note"
                  and event["payload"].get("kind") == "snapshot_mode_refrozen"]
        self.assertEqual(len(events), 1, events)
        note = events[0]["payload"]
        self.assertEqual((events[0]["source"], note["context"], note["snapshot_kind"]),
                         ("run_verifier.py", "verifier_bind_inputs", "phase1"))
        self.assertEqual((note["path_count"], note["paths"]), (2, ["raw/paper"]))
        self.assertNotIn(str(root), json.dumps(note))
        self.assertNotIn("paper.txt", json.dumps(note), "no deposit file name in the note")

    def test_phase1_directory_modes_count_only_when_refreezing(self) -> None:
        phase1_runtime = module("phase1_runtime")
        root = self.audit_after_phase1()
        quorum = json.loads((root / "phase1/quorum.json").read_text())
        snapshot = root / "phase1/snapshots" / quorum["source_snapshot_sha256"]
        manifest = json.loads((snapshot / "snapshot_manifest.json").read_text())
        pkg = snapshot / "raw/pkg"
        pkg.chmod(0o755)
        # Strict, exactly as before 2.6.8: a directory's mode is not judged, so days-old
        # snapshots stay admissible, and nothing is changed.
        self.assertEqual(phase1_runtime.verify_snapshot(snapshot, manifest), ())
        self.assertEqual(mode(pkg), 0o755)
        seen: list[dict] = []
        self.assertEqual(phase1_runtime.verify_snapshot(snapshot, manifest, refreeze=True,
                                                        on_refreeze=seen.append), ("raw/pkg",))
        self.assertEqual(seen, [{"path_count": 1, "paths": ["raw/pkg"]}])
        self.assertEqual(writable(snapshot), [])
        # A content change is an integrity fault that names a manifest index, never a deposit
        # file, and it is raised before any mode is touched.
        script = pkg / "analysis.py"
        pkg.chmod(0o755)
        script.chmod(0o644)
        script.write_text("print('changed')\n")
        with self.assertRaises(phase1_runtime.SnapshotIntegrityError) as caught:
            phase1_runtime.verify_snapshot(snapshot, manifest, refreeze=True,
                                           on_refreeze=seen.append)
        self.assertIsInstance(caught.exception, phase1_runtime.Phase1Error)
        self.assertRegex(str(caught.exception), r"source_files\[\d+\]")
        self.assertNotIn("analysis", str(caught.exception))
        self.assertEqual((mode(pkg), mode(script)), (0o755, 0o644))
        self.assertEqual(len(seen), 1)

    def test_a_content_change_is_still_a_refusal_with_no_chmod_and_no_event(self) -> None:
        root = self.audit_after_phase1()
        quorum = json.loads((root / "phase1/quorum.json").read_text())
        snapshot = root / "phase1/snapshots" / quorum["source_snapshot_sha256"]
        raw = snapshot / "raw/paper/paper.txt"
        (snapshot / "raw/paper").chmod(0o755)
        raw.chmod(0o644)
        raw.write_bytes(raw.read_bytes() + b"changed\n")
        before, starts = self.journal_bytes(root), len(self.starts())
        refused = self.run_verifier(root, ["F13"])
        self.assert_refused(refused, "verified snapshot mismatch")
        self.assertNotIn("paper.txt", json.loads(refused.stdout)["reason"],
                         "the refusal names a manifest index, not a deposit file")
        self.assertEqual(self.journal_bytes(root), before)
        self.assertEqual(len(self.starts()), starts)
        self.assertEqual((mode(snapshot / "raw/paper"), mode(raw)), (0o755, 0o644))


if __name__ == "__main__":
    unittest.main()
