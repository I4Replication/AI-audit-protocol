"""2.6.8: the coordinator sees a masked projection of package_manifest.json.

A deposit whose FILE NAMES are shaped like participant codes (one file per participant, named
after the code) used to make every coordinator run refuse before any provider call, and
recover_coordinator refused the same bytes: package_manifest.json is provider-visible context and
the participant-code rule admits a code-shaped token only when it is a column name of the
deposit. These tests drive the real runner with the fake provider (tests/fake_claude_coordinator.py)
and the real recovery tools; no model is ever called.

Every code-shaped token is assembled at run time, so this file carries none literally.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import pathlib
import sys
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "tests"))
import provider_safety  # noqa: E402
import recover_misplaced_coordinator_outputs as misplaced  # noqa: E402
import run_coordinator  # noqa: E402
import test_recover_coordinator  # noqa: E402
import test_run_coordinator  # noqa: E402


def code(number: int) -> str:
    """A participant-code-shaped token, built so the source never spells one."""
    return "EMG" + "_P" + str(900 + number)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_deposit(root: pathlib.Path, files: dict[str, str]) -> bytes:
    """Write pkg/ and the canonical intake manifest exactly as setup_workspace.sh does."""
    pkg = root / "pkg"
    for relative, text in files.items():
        path = pkg / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    rows, types = [], collections.Counter()
    for path in sorted(p for p in pkg.rglob("*") if p.is_file()):
        info = path.stat()
        relative = path.relative_to(pkg).as_posix()
        extension = path.suffix.lower().lstrip(".") or "<none>"
        types[extension] += 1
        rows.append({"path": relative, "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                     "sha256": sha(path.read_bytes()), "extension": extension})
    tree = sha("".join(f"{row['path']}\0{row['sha256']}\n" for row in rows).encode())
    payload = {"schema_version": "2.0.0", "algorithm": "sha256", "package_sha256": tree,
               "file_count": len(rows), "file_types": dict(sorted(types.items())), "files": rows}
    data = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    (root / "package_manifest.json").write_bytes(data)
    return data


def code_named_deposit() -> dict[str, str]:
    return {
        f"emg/{code(1)}.csv": "time,value\n0,1.5\n1,1.7\n",
        f"emg/{code(2)}.csv": "time,value\n0,2.5\n1,2.1\n",
        f"{code(3)}/trial.csv": "time,value\n0,0.5\n",
        "readme.txt": "One EMG file per participant.\n",
        "analysis.R": "files <- list.files('emg')\n",
    }


class MaskedManifestRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = test_run_coordinator.CoordinatorRunnerTests(
            methodName="test_success_is_content_bound_promotes_and_records_all_models_and_usage")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root

    def snapshot_manifest(self) -> bytes:
        return (self.fixture.attempt() / "source/context/package_manifest.json").read_bytes()

    def test_code_named_deposit_files_no_longer_refuse_and_are_masked(self) -> None:
        live = write_deposit(self.root, code_named_deposit())
        self.assertIsNotNone(provider_safety.first_participant_code(live.decode()),
                             "the fixture must carry code-shaped file names")
        result = self.fixture.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue((self.root / "report/REPORT_v1.md").is_file(),
                        "promotion needs the post-call live-input check to accept the projection")
        shown = self.snapshot_manifest()
        text = shown.decode("utf-8")
        self.assertIsNone(provider_safety.first_participant_code(text))
        projection = json.loads(text)
        paths = [row["path"] for row in projection["files"]]
        self.assertIn("emg/masked-file-001.csv", paths)
        self.assertIn("emg/masked-file-002.csv", paths)
        self.assertIn("masked-dir-001/trial.csv", paths)
        self.assertIn("readme.txt", paths)
        # Hashes and sizes are kept, so the coordinator still sees every deposited file.
        live_rows = json.loads(live)["files"]
        self.assertEqual(sorted(row["sha256"] for row in projection["files"]),
                         sorted(row["sha256"] for row in live_rows))
        self.assertEqual(sorted(row["size"] for row in projection["files"]),
                         sorted(row["size"] for row in live_rows))
        masked = [row for row in projection["files"] if row.get("path_masked")]
        self.assertEqual(len(masked), 3)
        directories = {row["directory"]: row for row in projection["coordinator_masking"]["directories"]}
        self.assertEqual(directories["emg"]["description"], "directory, 2 files")
        self.assertEqual(directories["masked-dir-001"]["description"], "directory, 1 file")
        self.assertIn("cannot be cited", projection["coordinator_masking"]["note"])
        # The packet binds the projection, not the live file, and the projection is stable.
        entry = next(row for row in json.loads(
            (self.fixture.attempt() / "source/input_manifest.json").read_text())["files"]
            if row["snapshot"] == "context/package_manifest.json")
        self.assertEqual(entry["sha256"], sha(shown))
        self.assertNotEqual(entry["sha256"], sha(live))
        exempt = run_coordinator.deposit_exemptions(self.root)
        self.assertEqual(run_coordinator.package_manifest_projection(live, exempt), shown)
        self.assertEqual(run_coordinator.live_context_bytes(
            self.root, "package_manifest.json", self.root / "package_manifest.json"), shown)

    def test_a_code_token_in_a_report_is_still_refused(self) -> None:
        write_deposit(self.root, code_named_deposit())
        report = self.root / "report/01_reproduction.md"
        original = report.read_text()
        self.fixture.quote_in_phase_one_report(f"The {code(1)} file was re-read.")
        refused = self.fixture.run_runner()
        self.assertEqual(refused.returncode, 1, refused.stderr + refused.stdout)
        self.assertIn("not provider-safe", refused.stderr)
        self.assertIn("Phase-1 report", refused.stderr)
        self.assertNotIn("package_manifest", refused.stderr)
        self.assertFalse((self.root / "coordinator").exists())
        # The same deposit with a value-free report reaches the provider.
        report.write_text(original)
        self.fixture.quote_in_phase_one_report("One EMG file per participant was re-read.")
        admitted = self.fixture.run_runner()
        self.assertEqual(admitted.returncode, 0, admitted.stderr + admitted.stdout)

    def test_a_normal_deposit_manifest_is_passed_through_byte_for_byte(self) -> None:
        live = write_deposit(self.root, {"data/study.csv": "id,score\n1,3\n", "analysis.R": "x <- 1\n"})
        exempt = run_coordinator.deposit_exemptions(self.root)
        self.assertIs(run_coordinator.package_manifest_projection(live, exempt), live)
        result = self.fixture.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.snapshot_manifest(), live)
        self.assertNotIn("coordinator_masking", json.loads(self.snapshot_manifest()))

    def test_a_code_token_outside_the_paths_still_refuses(self) -> None:
        live = write_deposit(self.root, code_named_deposit())
        manifest = json.loads(live)
        manifest["note"] = f"collected from {code(4)}"
        (self.root / "package_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        result = self.fixture.run_runner()
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertIn("context/package_manifest.json", result.stderr)
        self.assertIn("not provider-safe", result.stderr)
        self.assertFalse((self.root / "coordinator").exists())
        # Bytes that cannot be projected are returned unchanged, so the scan refuses them.
        raw = f"not json {code(5)}".encode()
        self.assertIs(run_coordinator.package_manifest_projection(raw), raw)

    def test_misplaced_output_recovery_compares_the_projection(self) -> None:
        write_deposit(self.root, code_named_deposit())
        result = self.fixture.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        root = self.root.resolve()          # the recovery tools work on the resolved audit root
        attempt = self.fixture.attempt().resolve()
        manifest = json.loads((attempt / "source/input_manifest.json").read_text())
        misplaced.validate_live_inputs(root, attempt, manifest, metadata_21=False)
        # A live manifest that no longer projects to the frozen bytes is stale.
        live = json.loads((self.root / "package_manifest.json").read_text())
        live["files"][0]["size"] += 1
        (self.root / "package_manifest.json").write_text(json.dumps(live, indent=2) + "\n")
        with self.assertRaisesRegex(misplaced.RecoveryError, "became stale"):
            misplaced.validate_live_inputs(root, attempt, manifest, metadata_21=False)


class MaskedManifestRecoveryTests(unittest.TestCase):
    """recover_coordinator on an attempt whose snapshot holds the masked projection."""

    def setUp(self) -> None:
        helper = test_recover_coordinator.CoordinatorRecoveryTests(
            methodName="test_elapsed_only_recovery_promotes_records_status_receipt_and_freezes")
        helper.fixture = test_run_coordinator.CoordinatorRunnerTests(
            methodName="test_success_is_content_bound_promotes_and_records_all_models_and_usage")
        helper.fixture.setUp()
        self.addCleanup(helper.fixture.tearDown)
        helper.root = helper.fixture.root
        write_deposit(helper.root, code_named_deposit())
        result = helper.fixture.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        helper.attempt = helper.fixture.attempt()
        helper.invocation_id = helper.attempt.name.split("_", 3)[-1]
        helper.attempt_relative = helper.attempt.relative_to(helper.root).as_posix()
        helper._quarantine_success()
        self.helper = helper

    def test_recovery_accepts_the_masked_snapshot(self) -> None:
        result = self.helper.run_recovery()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(len(self.helper.coordinator_rows()), 1)
        self.assertTrue((self.helper.root / "report/REPORT_v1.md").is_file())

    def test_recovery_refuses_a_changed_live_manifest(self) -> None:
        path = self.helper.root / "package_manifest.json"
        live = json.loads(path.read_text())
        live["files"][0]["sha256"] = "0" * 64
        os.chmod(path, 0o644)
        path.write_text(json.dumps(live, indent=2) + "\n")
        result = self.helper.run_recovery()
        self.assertIn("became stale", result.stderr)
        self.helper.assert_refused_without_promotion(result)


if __name__ == "__main__":
    unittest.main()
