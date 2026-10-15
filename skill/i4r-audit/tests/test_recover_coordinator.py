from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock


SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import claude_routes
import record_invocation
import recover_coordinator
import run_coordinator
import test_run_coordinator


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(path: pathlib.Path) -> str:
    rows = []
    for item in sorted([path, *path.rglob("*")], key=lambda value: value.as_posix()):
        relative = "." if item == path else item.relative_to(path).as_posix()
        info = item.lstat()
        if stat.S_ISDIR(info.st_mode):
            kind, file_hash, size = "directory", None, None
        elif stat.S_ISREG(info.st_mode):
            kind, file_hash, size = "file", digest(item), info.st_size
        else:
            kind, file_hash, size = "other", None, info.st_size
        rows.append({
            "path": relative, "kind": kind, "mode": stat.S_IMODE(info.st_mode),
            "bytes": size, "sha256": file_hash,
        })
    return hashlib.sha256(json.dumps(
        rows, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


class CoordinatorRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = test_run_coordinator.CoordinatorRunnerTests(
            methodName="test_success_is_content_bound_promotes_and_records_all_models_and_usage"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        result = self.fixture.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.attempt = self.fixture.attempt()
        self.invocation_id = self.attempt.name.split("_", 3)[-1]
        self.attempt_relative = self.attempt.relative_to(self.root).as_posix()
        self._quarantine_success()

    def _thaw_attempt(self) -> None:
        paths = [self.attempt, *self.attempt.rglob("*")]
        for path in sorted(
            (item for item in paths if item.is_dir()), key=lambda value: len(value.parts)
        ):
            os.chmod(path, 0o755)
        for path in (item for item in paths if item.is_file()):
            os.chmod(path, 0o644)

    def _write_bundle_and_rebind_record(self, bundle: dict, record: dict) -> None:
        bundle_path = self.attempt / "coordinator_bundle.json"
        bundle_path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
        record["output_sha256"] = digest(bundle_path)
        (self.attempt / "invocation_record.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n"
        )

    def _quarantine_success(self) -> None:
        self._thaw_attempt()
        (self.attempt / "output.partial/fake_args.json").unlink()
        ledger = self.fixture.ledger()
        coordinator_row = ledger.pop()
        self.assertEqual(coordinator_row["role"], "coordinator")
        (self.root / "invocations.jsonl").write_text("".join(
            json.dumps(row, sort_keys=True) + "\n" for row in ledger
        ))
        for relative in (
            "report/REPORT_v1.md", "report/ADJUDICATION.md", "work/findings.jsonl",
            "report/retired_claims.txt", "report/REPORT_final.md",
        ):
            (self.root / relative).unlink()

        stdout_path = self.attempt / "claude_stdout.json"
        provider = json.loads(stdout_path.read_text())
        provider.update({
            "terminal_reason": "completed",
            "stop_reason": "end_turn",
        })
        stdout_path.write_text(json.dumps(provider, separators=(",", ":")) + "\n")
        denial = {
            "count": 0,
            "tool_names": [],
            "raw_stdout_sha256": digest(stdout_path),
        }
        (self.attempt / "permission_denials.json").write_text(
            json.dumps(denial, indent=2, sort_keys=True) + "\n"
        )

        record = json.loads((self.attempt / "invocation_record.json").read_text())
        started = dt.datetime.fromisoformat(record["started_at"].replace("Z", "+00:00"))
        ended = dt.datetime.fromisoformat(record["ended_at"].replace("Z", "+00:00"))
        wall = round((ended - started).total_seconds(), 3)
        record["elapsed_seconds"] = wall + 30

        bundle = json.loads((self.attempt / "coordinator_bundle.json").read_text())
        bundle["provider_stdout_sha256"] = digest(stdout_path)
        self._write_bundle_and_rebind_record(bundle, record)
        with self.assertRaisesRegex(ValueError, "timestamps and elapsed_seconds are inconsistent"):
            record_invocation.validate_record(record, self.root)

        status = {
            "schema_version": "1.0.0",
            "state": "running",
            "audit_id": "audit-test-coordinator",
            "invocation_id": self.invocation_id,
            "attempt": 1,
            "input_artifact_sha256": record["input_artifact_sha256"],
            "provider_call_started": False,
        }
        (self.attempt / "status.json").write_text(
            json.dumps(status, indent=2, sort_keys=True) + "\n"
        )
        transaction = self.root / "coordinator/transactions" / self.invocation_id
        (transaction / "staging").mkdir(parents=True)
        (transaction / "backups").mkdir()
        artifacts = bundle["outputs"]
        journal = {
            "schema_version": "1.0.0",
            "state": "backups_ready",
            "invocation_id": self.invocation_id,
            "entries": [{
                "name": name,
                "target": artifact["canonical_path"],
                "sha256": artifact["sha256"],
                "existed": False,
                "backup": None,
            } for name, artifact in artifacts.items()],
        }
        (transaction / "journal.json").write_text(
            json.dumps(journal, indent=2, sort_keys=True) + "\n"
        )
        self.transaction = transaction
        source = self.attempt / "source"
        for path in (item for item in source.rglob("*") if item.is_file()):
            os.chmod(path, 0o444)
        for path in sorted(
            (item for item in [source, *source.rglob("*")] if item.is_dir()),
            key=lambda value: len(value.parts), reverse=True,
        ):
            os.chmod(path, 0o555)

    def run_recovery(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([
            sys.executable,
            str(SCRIPTS / "recover_coordinator.py"),
            str(self.root),
            "--attempt", self.attempt_relative,
        ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def coordinator_rows(self) -> list[dict]:
        return [row for row in self.fixture.ledger() if row.get("role") == "coordinator"]

    def assert_refused_without_promotion(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertEqual(self.coordinator_rows(), [])
        for relative in (
            "report/REPORT_v1.md", "report/ADJUDICATION.md", "work/findings.jsonl",
            "report/retired_claims.txt", "report/REPORT_final.md",
        ):
            self.assertFalse((self.root / relative).exists(), relative)
        self.assertFalse((
            self.root / f"coordinator/recoveries/{self.invocation_id}/receipt.json"
        ).exists())

    def test_elapsed_only_recovery_promotes_records_status_receipt_and_freezes(self) -> None:
        original_record_hash = digest(self.attempt / "invocation_record.json")
        original_transaction_hash = tree_digest(self.transaction)
        provider_hash = digest(self.attempt / "claude_stdout.json")
        result = self.run_recovery()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["mode"], "no_provider")
        self.assertFalse(summary["provider_called_during_recovery"])
        self.assertEqual(summary["corrected_fields"], ["elapsed_seconds"])
        rows = self.coordinator_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        started = dt.datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
        ended = dt.datetime.fromisoformat(row["ended_at"].replace("Z", "+00:00"))
        self.assertEqual(row["elapsed_seconds"], round((ended - started).total_seconds(), 3))
        record_invocation.validate_record(row, self.root.resolve())
        for relative in (
            "report/REPORT_v1.md", "report/ADJUDICATION.md", "work/findings.jsonl",
            "report/retired_claims.txt", "report/REPORT_final.md",
        ):
            self.assertTrue((self.root / relative).is_file(), relative)
        receipt_path = self.root / summary["receipt"]
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["corrected_fields"], ["elapsed_seconds"])
        self.assertFalse(receipt["provider_called_during_recovery"])
        self.assertTrue(all(receipt["checks"].values()))
        status = json.loads((self.attempt / "status.json").read_text())
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["recovery"]["mode"], "no_provider")
        self.assertEqual(digest(self.attempt / "invocation_record.json"), original_record_hash)
        self.assertEqual(tree_digest(self.transaction), original_transaction_hash)
        self.assertEqual(digest(self.attempt / "claude_stdout.json"), provider_hash)
        for path in [self.attempt, *self.attempt.rglob("*")]:
            self.assertFalse(path.stat().st_mode & 0o222, path)
        self.assertFalse(receipt_path.stat().st_mode & 0o222)

    def test_refuses_stale_live_input(self) -> None:
        grouping = self.root / "work/registry_draft/GROUPING.md"
        grouping.write_text(grouping.read_text() + "\nChanged after provider completion.\n")
        result = self.run_recovery()
        self.assertIn("became stale", result.stderr)
        self.assert_refused_without_promotion(result)

    def test_refuses_changed_provider_model(self) -> None:
        provider_path = self.attempt / "claude_stdout.json"
        provider = json.loads(provider_path.read_text())
        # The primary the fake served on this audit's snapshot route, swapped for another model.
        provider["modelUsage"] = {
            "claude-opus-5": provider["modelUsage"].pop(self.fixture.served)
        }
        provider_path.write_text(json.dumps(provider) + "\n")
        result = self.run_recovery()
        self.assertIn(f"served {claude_routes.short_name(self.fixture.route['requested_model'])} primary",
                      result.stderr)
        self.assert_refused_without_promotion(result)

    def test_refuses_tampered_output_even_when_nonempty(self) -> None:
        report = self.attempt / "output.partial/REPORT_v1.md"
        report.write_text(report.read_text() + "\nNonempty stale addition.\n")
        result = self.run_recovery()
        self.assertIn("bundle disagrees", result.stderr)
        self.assert_refused_without_promotion(result)

    def test_refuses_non_elapsed_record_defect(self) -> None:
        path = self.attempt / "invocation_record.json"
        record = json.loads(path.read_text())
        record["requested_model"] = "opus-5"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        result = self.run_recovery()
        self.assertIn("non-timing identity defects", result.stderr)
        self.assert_refused_without_promotion(result)

    def test_refuses_missing_failed_transaction(self) -> None:
        self.transaction.rename(self.transaction.with_name(self.transaction.name + ".missing"))
        result = self.run_recovery()
        self.assertIn("failed promotion transaction is missing", result.stderr)
        self.assert_refused_without_promotion(result)

    def test_refuses_when_elapsed_record_is_already_valid(self) -> None:
        path = self.attempt / "invocation_record.json"
        record = json.loads(path.read_text())
        started = dt.datetime.fromisoformat(record["started_at"].replace("Z", "+00:00"))
        ended = dt.datetime.fromisoformat(record["ended_at"].replace("Z", "+00:00"))
        record["elapsed_seconds"] = round((ended - started).total_seconds(), 3)
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        result = self.run_recovery()
        self.assertIn("does not have the recoverable", result.stderr)
        self.assert_refused_without_promotion(result)

    def test_second_validation_refuses_change_staged_during_recovery(self) -> None:
        real_validate = recover_coordinator.validate_recovery
        calls = 0

        def mutate_before_second_validation(root: pathlib.Path, attempt: pathlib.Path):
            nonlocal calls
            calls += 1
            if calls == 2:
                report = attempt / "output.partial/REPORT_v1.md"
                report.write_text(report.read_text() + "\nChanged between validation and commit.\n")
            return real_validate(root, attempt)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            recover_coordinator, "validate_recovery", side_effect=mutate_before_second_validation
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            returncode = recover_coordinator.main([
                str(self.root), "--attempt", self.attempt_relative,
            ])
        result = subprocess.CompletedProcess([], returncode, stdout.getvalue(), stderr.getvalue())
        self.assertEqual(calls, 2)
        self.assertIn("bundle disagrees", result.stderr)
        self.assert_refused_without_promotion(result)


if __name__ == "__main__":
    unittest.main()
