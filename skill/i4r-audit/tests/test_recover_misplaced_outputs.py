"""Unit tests for the misplaced-output recovery's fail-closed helpers.

The end-to-end path is exercised against the real quarantined attempt it was written for;
these tests pin the precondition logic so a future change cannot widen the recovery into
something that moves files it should not.
"""
from __future__ import annotations

import hashlib
import pathlib
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import recover_misplaced_coordinator_outputs as recovery  # noqa: E402
import run_coordinator  # noqa: E402


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FindMisplacedOutputs(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.attempt = pathlib.Path(self.temporary.name) / "attempt_03_coordinator-00000000-0000-0000-0000-000000000000"
        (self.attempt / "output.partial").mkdir(parents=True)
        for name in run_coordinator.OUTPUT_TARGETS:
            (self.attempt / name).write_bytes(f"content of {name}\n".encode())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_all_four_in_root_and_empty_partial_is_the_defect(self) -> None:
        hashes = recovery.find_misplaced_outputs(self.attempt)
        self.assertEqual(set(hashes), set(run_coordinator.OUTPUT_TARGETS))
        self.assertEqual(hashes["REPORT_v1.md"], sha(b"content of REPORT_v1.md\n"))

    def test_partial_with_any_file_is_refused(self) -> None:
        (self.attempt / "output.partial" / "REPORT_v1.md").write_text("x")
        with self.assertRaises(recovery.RecoveryError):
            recovery.find_misplaced_outputs(self.attempt)

    def test_missing_one_output_is_refused(self) -> None:
        (self.attempt / "retired_claims.txt").unlink()
        with self.assertRaises(recovery.RecoveryError):
            recovery.find_misplaced_outputs(self.attempt)

    def test_symlinked_output_is_refused(self) -> None:
        (self.attempt / "findings.jsonl").unlink()
        (self.attempt / "findings.jsonl").symlink_to(self.attempt / "REPORT_v1.md")
        with self.assertRaises(recovery.RecoveryError):
            recovery.find_misplaced_outputs(self.attempt)

    def test_relocate_is_byte_identical_and_reversible(self) -> None:
        hashes = recovery.find_misplaced_outputs(self.attempt)
        recovery.relocate(self.attempt, hashes)
        for name, digest in hashes.items():
            self.assertFalse((self.attempt / name).exists())
            self.assertEqual(sha((self.attempt / "output.partial" / name).read_bytes()), digest)
        recovery.unrelocate(self.attempt, list(hashes))
        for name in hashes:
            self.assertTrue((self.attempt / name).is_file())
            self.assertFalse((self.attempt / "output.partial" / name).exists())


class FailedRecordProfile(unittest.TestCase):
    def row(self, **overrides):
        base = {
            "invocation_id": "coordinator-x", "role": "coordinator", "role_class": "coordinator",
            "round": None, "attempt": 3, "status": "failed", "provider": "anthropic",
            "provider_call": True, "requested_model": "sonnet-5", "model_family": "claude",
            "reasoning_effort": "max", "input_artifact_sha256": "a" * 64,
            "error_class": "schema", "output_artifact": None, "output_sha256": None,
            "provider_request_status": "accepted", "inference_start_status": "started",
            "error": recovery.DEFECT_PREFIX + ": /somewhere/output.partial/REPORT_v1.md",
        }
        base.update(overrides)
        return base

    def test_exact_profile_is_accepted(self) -> None:
        row = recovery.failed_record([self.row()], "coordinator-x", 3, "a" * 64)
        self.assertEqual(row["status"], "failed")

    def test_other_errors_are_refused(self) -> None:
        with self.assertRaises(recovery.RecoveryError):
            recovery.failed_record([self.row(error="REPORT_v1.md lacks required citation marker AI-Games")],
                                   "coordinator-x", 3, "a" * 64)

    def test_provider_safety_refusal_is_not_recoverable_this_way(self) -> None:
        with self.assertRaises(recovery.RecoveryError):
            recovery.failed_record([self.row(error_class="provider_safety_refusal")], "coordinator-x", 3, "a" * 64)

    def test_completed_row_blocks_recovery(self) -> None:
        completed = self.row(invocation_id="coordinator-y", attempt=2, status="completed",
                             error=None, error_class=None, output_artifact="coordinator/x/bundle.json",
                             output_sha256="b" * 64)
        with self.assertRaises(recovery.RecoveryError):
            recovery.failed_record([completed, self.row()], "coordinator-x", 3, "a" * 64)


if __name__ == "__main__":
    unittest.main()
