#!/usr/bin/env python3
"""The strand guard and --recover-stranded must see the state real strands are actually in.

Under metadata 2.1 the runner holds status.json at `provider_requested` for the whole provider
call and journals `inference_start` only once the stream proves acceptance, so a killed 2.1 run
leaves `provider_requested`. Both the guard and the recovery originally matched only
`inference_started` -- a state 2.1 never writes -- so on every one of the seven real strands in
this tree the guard silently allowed a duplicate paid call and the recovery cleared nothing.

These tests build strands on disk and call the two functions, rather than reading the source.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
RUN_PHASE1 = SKILL / "scripts" / "run_phase1.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_phase1", RUN_PHASE1)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_phase1"] = module
    spec.loader.exec_module(module)
    return module


phase1 = _load()
import lifecycle_journal  # noqa: E402
import record_invocation  # noqa: E402  (resolved through the scripts path inserted above)


def iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class StrandFixture:
    """A minimal audit root with one non-terminal Phase-1 attempt."""

    def __init__(self, base: Path, state: str, *, journal: bool = True,
                 provider_pid: int | None = None, stdout: bytes | None = None) -> None:
        self.root = base / "audit"
        self.phase_root = self.root / "phase1"
        self.invocation_id = "inv-" + hashlib.sha256(state.encode()).hexdigest()[:32]
        started = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)
        self.attempt_dir = (
            self.phase_root / "roles" / "auditor_code_methods" / ("a" * 64) / "attempt_01_x"
        )
        self.attempt_dir.mkdir(parents=True)
        status = {
            "schema_version": "i4r-phase1-attempt-1",
            "state": state,
            "invocation_id": self.invocation_id,
            "attempt_kind": "role_inference",
            "attempt": None if state == "provider_requested" else 1,
            "provider_request_status": "requested",
            "inference_start_status": (
                "not_started" if state == "provider_requested" else "started"),
            "requested_session_id": "sess-1",
            "input_artifact_sha256": "b" * 64,
            "started_at": iso(started),
        }
        if provider_pid is not None:
            status["provider_pid"] = provider_pid
        (self.attempt_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
        if stdout is not None:
            (self.attempt_dir / "claude_stdout.json").write_bytes(stdout)
        (self.root / "invocations.jsonl").write_text("", encoding="utf-8")
        # validate_record reads the audit-scoped policy snapshot, so a fixture needs a real one.
        record_invocation.initialize_policy_snapshot(self.root)
        snapshot = self.root / "pipeline-policy.snapshot.json"
        (self.root / "run.json").write_text(json.dumps({
            "schema_version": "2.0.0",
            "audit_id": "audit-strand-fixture",
            "normalization": None,
            "toolkit": {"pipeline_policy_snapshot_sha256":
                        hashlib.sha256(snapshot.read_bytes()).hexdigest()},
        }, indent=2) + "\n", encoding="utf-8")
        # The route the runner reserves on is the auditor's, as this audit's snapshot froze it.
        self.route = record_invocation.load_policy_snapshot(self.root)["models"]["phase_1_auditors"][
            "auditor_code_methods"]
        if journal:
            # Built through the journal API, not hand-written: run.events.jsonl is an
            # append-only hash chain and a hand-forged row is rejected before recovery is
            # reached, which would make these tests pass for the wrong reason.
            lifecycle_journal.initialize(self.root)
            record_invocation.reserve_invocation(self.root, {
                "invocation_id": self.invocation_id,
                "role": "auditor_code_methods",
                "role_class": "auditor",
                "round": 0,
                "attempt_kind": "role_inference",
                "provider": self.route["provider"],
                "requested_model": self.route["requested_model"],
                "reasoning_effort": self.route["reasoning_effort"],
                "input_artifact_sha256": "b" * 64,
                "prompt_sha256": "c" * 64,
                "source_invocation_id": None,
            })
            if state == "inference_started":
                record_invocation.mark_provider_request(
                    self.root, self.invocation_id, status="requested")
                record_invocation.mark_inference_start(self.root, self.invocation_id)


class StrandedAttemptTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="i4r-strand-")
        self.base = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_guard_refuses_a_provider_requested_strand(self) -> None:
        fixture = StrandFixture(self.base, "provider_requested")
        with self.assertRaises(phase1.RunnerError) as caught:
            phase1.guard_stranded_attempts(fixture.root, fixture.phase_root)
        message = str(caught.exception)
        self.assertIn("provider_requested", message)
        self.assertIn("--recover-stranded", message)

    def test_guard_refuses_a_legacy_inference_started_strand(self) -> None:
        fixture = StrandFixture(self.base, "inference_started")
        with self.assertRaises(phase1.RunnerError):
            phase1.guard_stranded_attempts(fixture.root, fixture.phase_root)

    def test_guard_names_a_live_call_rather_than_calling_it_a_strand(self) -> None:
        fixture = StrandFixture(self.base, "provider_requested", provider_pid=os.getpid())
        with self.assertRaises(phase1.RunnerError) as caught:
            phase1.guard_stranded_attempts(fixture.root, fixture.phase_root)
        self.assertIn("still in flight", str(caught.exception))
        self.assertNotIn("--recover-stranded", str(caught.exception))

    def test_recovery_terminalizes_a_provider_requested_strand(self) -> None:
        fixture = StrandFixture(self.base, "provider_requested")
        self.assertEqual(
            phase1.recover_stranded_attempts(fixture.root, fixture.phase_root), 1)
        rows = [json.loads(line) for line
                in (fixture.root / "invocations.jsonl").read_text().splitlines() if line.strip()]
        row = next(r for r in rows if r["invocation_id"] == fixture.invocation_id)
        self.assertEqual(row["status"], "failed")
        # Nothing was inferred, so no attempt was consumed and no observation is claimed.
        self.assertIsNone(row["attempt"])
        self.assertEqual(row["inference_start_status"], "not_started")
        self.assertIsNone(row["observed_model"])
        self.assertIsNone(row["provider_cost"])
        self.assertFalse(row["usage"]["obtainable"])
        # started_at is the real dispatch time, not the moment of recovery.
        self.assertGreater(row["elapsed_seconds"], 6 * 3600 - 120)
        # And the guard is satisfied afterwards.
        phase1.guard_stranded_attempts(fixture.root, fixture.phase_root)

    def test_recovery_is_idempotent(self) -> None:
        fixture = StrandFixture(self.base, "provider_requested")
        self.assertEqual(
            phase1.recover_stranded_attempts(fixture.root, fixture.phase_root), 1)
        self.assertEqual(
            phase1.recover_stranded_attempts(fixture.root, fixture.phase_root), 0)

    def test_recovery_refuses_to_discard_a_retained_response(self) -> None:
        """Negative control: a strand holding real output belongs to the other recovery path."""
        fixture = StrandFixture(self.base, "provider_requested", stdout=b'{"result": "x"}')
        with self.assertRaises(phase1.RunnerError) as caught:
            phase1.recover_stranded_attempts(fixture.root, fixture.phase_root)
        self.assertIn("retained a provider response", str(caught.exception))

    def test_legacy_workspace_is_refused_with_an_accurate_reason(self) -> None:
        fixture = StrandFixture(self.base, "inference_started", journal=False)
        with self.assertRaises(phase1.RunnerError) as caught:
            phase1.recover_stranded_attempts(fixture.root, fixture.phase_root)
        message = str(caught.exception)
        self.assertIn("legacy", message)
        self.assertIn("run.events.jsonl", message)
        self.assertNotIn("killed-process strand", message)

    def test_terminalized_attempt_is_not_a_strand(self) -> None:
        """Negative control: a state with a terminal ledger row must not be re-recovered."""
        fixture = StrandFixture(self.base, "provider_requested")
        phase1.recover_stranded_attempts(fixture.root, fixture.phase_root)
        status = json.loads((fixture.attempt_dir / "status.json").read_text())
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["inference_start_status"], "not_started")


@unittest.skipUnless(os.environ.get("I4R_STRAND_CORPUS"),
                     "set I4R_STRAND_CORPUS=<discipline-root> to sweep real strands on copies")
class RealStrandRecoveryTest(unittest.TestCase):
    """End-to-end over the strands in a corpus the operator explicitly names, on copies.

    This used to glob three directories above the skill root unasked. The suite's verdict then
    depended on data that is not part of the artifact: with nothing there it skipped, and with an
    archived audit whose strand legitimately retained a provider response it went red for a
    reason no operator could act on. The synthetic StrandFixture cases above cover every state
    in STRANDED_STATES with a negative control; this sweep is opt-in extra coverage.
    """

    def test_every_real_strand_recovers(self) -> None:
        tree = Path(os.environ["I4R_STRAND_CORPUS"])
        audits = sorted({
            path.parents[5] for path in
            tree.glob("*/*/audit/phase1/roles/*/*/*/status.json")
            if json.loads(path.read_text()).get("state") in phase1.STRANDED_STATES
        })
        if not audits:
            self.skipTest("no stranded attempts in the named corpus")
        for audit in audits:
            with self.subTest(audit=audit.parent.name):
                with tempfile.TemporaryDirectory(prefix="i4r-real-strand-") as tmp:
                    copy = Path(tmp) / "audit"
                    subprocess.run(["cp", "-R", str(audit), str(copy)], check=True)
                    subprocess.run(["chmod", "-R", "u+w", str(copy)], check=False)
                    result = subprocess.run(
                        [sys.executable, str(RUN_PHASE1), str(copy), "--recover-stranded"],
                        capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                    self.assertGreater(
                        json.loads(result.stdout)["recovered_stranded_attempts"], 0)
                    again = subprocess.run(
                        [sys.executable, str(RUN_PHASE1), str(copy), "--recover-stranded"],
                        capture_output=True, text=True, check=False)
                    self.assertEqual(
                        json.loads(again.stdout)["recovered_stranded_attempts"], 0)
                    subprocess.run(["chmod", "-R", "u+w", str(copy)], check=False)


if __name__ == "__main__":
    unittest.main()
