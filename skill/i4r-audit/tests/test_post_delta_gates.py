#!/usr/bin/env python3
"""Fail-closed tests for the release-bound post-delta gate receipt."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


TESTS = Path(__file__).resolve().parent
SKILL = TESTS.parent
sys.path.insert(0, str(TESTS))
from test_release_pipeline import make_audit


POST_DELTA = SKILL / "scripts" / "run_post_delta_gates.py"
VALIDATE_ROUNDS = SKILL / "scripts" / "validate_round_projections.py"


def run(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(argument) for argument in arguments],
        text=True,
        capture_output=True,
        check=False,
    )


def make_writable(root: Path) -> None:
    if not root.exists():
        return
    for path in [root, *root.rglob("*")]:
        try:
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
        except OSError:
            pass


class PostDeltaGateReceiptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._baseline_tmp = tempfile.TemporaryDirectory(prefix="i4r-post-delta-baseline-")
        cls.baseline = make_audit(Path(cls._baseline_tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        make_writable(Path(cls._baseline_tmp.name))
        cls._baseline_tmp.cleanup()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="i4r-post-delta-test-")
        destination = Path(self._tmp.name) / "audit"
        self.audit = Path(shutil.copytree(self.baseline, destination))

    def tearDown(self) -> None:
        make_writable(Path(self._tmp.name))
        self._tmp.cleanup()

    def receipt(self) -> Path:
        return self.audit / "critique/round_2/POST_DELTA_GATES.json"

    def check(self, *, current: bool = False) -> subprocess.CompletedProcess[str]:
        arguments: list[str | Path] = [
            sys.executable, POST_DELTA, self.audit, "--round", "2", "--check",
        ]
        if current:
            arguments.append("--require-current")
        return run(*arguments)

    def test_valid_receipt_and_release_validator_pass(self) -> None:
        self.assertEqual(self.check(current=True).returncode, 0)
        result = run(
            sys.executable, VALIDATE_ROUNDS, self.audit,
            "--require-post-delta-gates",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_receipt_only_blocks_release_boundary(self) -> None:
        self.receipt().unlink()
        runtime = run(sys.executable, VALIDATE_ROUNDS, self.audit)
        release = run(
            sys.executable, VALIDATE_ROUNDS, self.audit,
            "--require-post-delta-gates",
        )
        self.assertEqual(runtime.returncode, 0, runtime.stderr)
        self.assertNotEqual(release.returncode, 0)
        self.assertIn("post-delta", release.stderr.lower())

    def test_failed_or_incomplete_gate_quorum_is_rejected(self) -> None:
        original = json.loads(self.receipt().read_text(encoding="utf-8"))
        for mutation in (
            lambda value: value["gates"][1].update(status="failed", exit_status=1),
            lambda value: value["gates"].pop(),
        ):
            value = json.loads(json.dumps(original))
            mutation(value)
            self.receipt().write_text(json.dumps(value) + "\n", encoding="utf-8")
            self.assertNotEqual(self.check().returncode, 0)

    def test_tampered_snapshot_log_is_rejected(self) -> None:
        receipt = json.loads(self.receipt().read_text(encoding="utf-8"))
        log = self.audit / receipt["snapshot"]["path"] / "logs/privacy.log"
        os.chmod(log, 0o644)
        log.write_text(log.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("snapshot inventory", result.stderr.lower())

    def test_live_report_and_archive_drift_are_rejected(self) -> None:
        report = self.audit / "report/REPORT_final.md"
        report.write_text(report.read_text(encoding="utf-8") + "\nStale change.\n",
                          encoding="utf-8")
        self.assertEqual(self.check().returncode, 0)
        current = self.check(current=True)
        self.assertNotEqual(current.returncode, 0)
        self.assertIn("binding", current.stderr.lower())

        # Restore the report so the independent archive condition is isolated.
        shutil.copy2(self.baseline / "report/REPORT_final.md", report)
        os.chmod(self.audit / "pkg", 0o755)
        archive = self.check()
        self.assertNotEqual(archive.returncode, 0)
        self.assertIn("read-only", archive.stderr.lower())


if __name__ == "__main__":
    unittest.main()
