from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SKILL / "tests"))
import claude_routes
import record_invocation
import lifecycle_journal
import run_coordinator
import fake_claude_coordinator


ROLES = {
    "auditor_reproduction": "report/01_reproduction.md",
    "auditor_data_integrity": "report/02_data_integrity.md",
    "auditor_text_vs_data": "report/03_text_vs_data.md",
    "auditor_code_methods": "report/04_code_methods.md",
    "auditor_privacy_archive": "report/05_privacy_archive.md",
}


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invocation(role: str, report: str, report_hash: str, route: dict | None = None) -> dict:
    """A completed auditor row on ``route`` (the auditor's snapshot row; default the Sonnet 5 row
    every pre-2.7.0 fixture wrote), served what tests/fake_claude_coordinator.py serves that route."""
    route = route or claude_routes.SONNET_5
    served, _second = fake_claude_coordinator.SERVED_BY_MODEL[route["spawn_alias"]]
    return {
        "invocation_id": f"inv-{role}",
        "role": role,
        "role_class": "auditor",
        "round": 0,
        "attempt": 1,
        "status": "completed",
        "provider": "anthropic",
        "provider_call": True,
        "cache_usage": "read_write",
        "session_id": f"session-{role}",
        "requested_model": route["requested_model"],
        "observed_model": served,
        "observed_model_ids": [served, fake_claude_coordinator.HELPER],
        "model_family": "claude",
        "model_source": "observed",
        "reasoning_effort": route["reasoning_effort"],
        "started_at": "2026-08-28T12:00:00Z",
        "ended_at": "2026-08-28T12:01:00Z",
        "elapsed_seconds": 60,
        "input_artifact_sha256": "a" * 64,
        "prompt_sha256": "b" * 64,
        "output_artifact": report,
        "output_sha256": report_hash,
        "usage": {
            "obtainable": True,
            "input_tokens": 100,
            "output_tokens": 20,
            "cached_input_tokens": 10,
            "reasoning_tokens": 5,
            "total_tokens": 135,
            "source": "api_response",
        },
        "error": None,
        "error_class": None,
        "legacy_source": None,
    }


def draft_row(report_hash: str) -> dict:
    row = json.loads((SKILL / "schemas/fixtures/v2_finding_row.json").read_text())
    row.update({
        "status": "draft",
        "coordinator_approved": False,
        "verifications": [],
        "settled": None,
        "report_section": None,
        "verification_snippet": None,
    })
    row["evidence"] = {
        "source_hashes": [{"path": "report/01_reproduction.md", "sha256": report_hash}],
        "rerun_command": "review report/01_reproduction.md",
        "expected": "reported value",
        "observed": "different bounded value",
        "tolerance_basis": "Exact comparison in the supplied report.",
    }
    row["dependencies"] = {
        "environment_fingerprint": None,
        "finding_ids": [],
        "artifact_paths": ["report/01_reproduction.md"],
    }
    row["provenance"] = {
        "origin_role": "auditor_reproduction",
        "independently_found_by": ["auditor_reproduction"],
        "agreed_after_seeing": [],
        "round_introduced": 0,
        "source_records": ["report/01_reproduction.md"],
    }
    row["remedy"] = {
        "class": "reanalysis",
        "description": "Reconcile the value with the authoritative analysis output.",
        "verified": False,
    }
    return row


class CoordinatorRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-coordinator-test-")
        self.root = pathlib.Path(self.temporary.name) / "audit"
        self.root.mkdir()
        for directory in ("report", "work/registry_draft", "work/safe_evidence"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        record_invocation.initialize_policy_snapshot(self.root)
        # 2.7.0: every Claude row and expectation below follows the route this audit's snapshot
        # froze (the shipped policy's, unless a caller patched what is frozen).
        policy = record_invocation.load_policy_snapshot(self.root)
        self.route = claude_routes.project(policy["models"]["coordinator"])
        self.auditor_routes = {role: claude_routes.project(policy["models"]["phase_1_auditors"][role])
                               for role in ROLES}
        self.served, _second = fake_claude_coordinator.SERVED_BY_MODEL[self.route["spawn_alias"]]
        policy_hash = digest(self.root / "pipeline-policy.snapshot.json")
        (self.root / "run.json").write_text(json.dumps({
            "schema_version": "2.0.0",
            "audit_id": "audit-test-coordinator",
            "normalization": None,
            "toolkit": {"pipeline_policy_snapshot_sha256": policy_hash},
        }) + "\n")
        (self.root / "PAPER.txt").write_text(
            "Test paper. DOI: 10.0000/example. Replication package: local deposit.\n"
        )
        (self.root / "package_manifest.json").write_text(
            json.dumps({"schema_version": "2.0.0", "files": []}) + "\n"
        )
        records = []
        for role, relative in ROLES.items():
            path = self.root / relative
            path.write_text(
                f"# {role}\n\n" +
                ("Complete independent checks, evidence, negative results, benign explanations, "
                 "open questions, and an evidence inventory are recorded here. " * 15) + "\n"
            )
            records.append(invocation(role, relative, digest(path), self.auditor_routes[role]))
        (self.root / "invocations.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in records)
        )
        row = draft_row(digest(self.root / "report/01_reproduction.md"))
        self.draft = self.root / "work/registry_draft/proposed.jsonl"
        self.draft.write_text(json.dumps(row, sort_keys=True) + "\n")
        (self.root / "work/registry_draft/GROUPING.md").write_text(
            "# Grouping\n\nF1 remains a distinct draft candidate.\n"
        )
        safe = self.root / "work/safe_evidence/summary.md"
        safe.write_text("# Safe evidence\n\nAggregate comparison only; no row-level values.\n")
        safe_root = safe.parent
        privacy = safe_root / "privacy_scan.json"
        privacy.write_text(json.dumps({
            "schema_version": "i4r-safe-evidence-1", "passed": True,
            "full_directory_rescan_passed": True, "absolute_local_paths_emitted": False,
            "participant_identifiers_emitted": False, "row_values_or_signatures_emitted": False,
            "user_host_or_printer_values_emitted": False,
            "generic_detector_match_counts": {"participant_identifier": 0, "row_level_numeric_signature": 0},
            "source_derived_token_hit_counts": {"absolute_path": 0, "document_identity": 0},
        }, indent=2) + "\n")
        self_tests = safe_root / "self_tests.json"
        self_tests.write_text(json.dumps({
            "schema_version": "i4r-safe-evidence-1", "passed": True,
            "tests_run": 2, "tests_passed": 2,
        }) + "\n")
        bindings = safe_root / "source_bindings.json"
        bindings.write_text(json.dumps({
            "schema_version": "i4r-safe-evidence-1", "paths_emitted": False,
            "workbook": {"sha256": "a" * 64, "bytes": 10},
        }) + "\n")
        payloads = [privacy, safe, self_tests, bindings]
        self.safe_manifest = safe_root / "manifest.json"
        manifest = {
            "schema_version": "i4r-safe-evidence-1",
            "artifact_count": len(payloads),
            "artifacts": [
                {"path": path.name, "sha256": digest(path), "bytes": path.stat().st_size}
                for path in payloads
            ],
            "checksum_closure": "SHA256SUMS hashes every payload plus manifest.json; SHA256SUMS excludes itself",
        }
        self.safe_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
        checksum_paths = [*payloads, self.safe_manifest]
        (safe_root / "SHA256SUMS").write_text("".join(
            f"{digest(path)}  {path.name}\n" for path in checksum_paths
        ))
        self.fake = pathlib.Path(self.temporary.name) / "fake-claude"
        shutil.copyfile(SKILL / "tests/fake_claude_coordinator.py", self.fake)
        self.fake.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_runner(self, scenario: str = "success", *, mutate: pathlib.Path | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["FAKE_COORDINATOR_SCENARIO"] = scenario
        if mutate is not None:
            env["FAKE_COORDINATOR_MUTATE"] = str(mutate)
        return subprocess.run([
            sys.executable,
            str(SCRIPTS / "run_coordinator.py"),
            str(self.root),
            "--safe-evidence-manifest", str(self.safe_manifest),
            "--claude-bin", str(self.fake),
            "--timeout-min", "1",
        ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)

    def run_main(self, scenario: str = "success") -> int:
        args = [
            str(self.root),
            "--safe-evidence-manifest", str(self.safe_manifest),
            "--claude-bin", str(self.fake),
            "--timeout-min", "1",
        ]
        with mock.patch.dict(os.environ, {"FAKE_COORDINATOR_SCENARIO": scenario}):
            return run_coordinator.main(args)

    def ledger(self) -> list[dict]:
        return [json.loads(line) for line in (self.root / "invocations.jsonl").read_text().splitlines() if line]

    def upgrade_to_metadata_21(self) -> None:
        seeded = self.ledger()
        existing = json.loads((self.root / "run.json").read_text())
        existing.update({
            "schema_version": "2.1.0",
            "started_at": "2026-08-28T11:00:00Z", "ended_at": None,
            "status": "initialized", "pause_reason": None,
            "post_delta_gates_passed": False,
            "round_target": 2, "round_cap": 7, "rounds": [], "rounds_run": 0,
            "stopping_reason": None, "invocations": [], "events": [],
            "human_effort": {"person_minutes_by_phase": {}},
        })
        lifecycle_journal.initialize(self.root, existing)
        for row in seeded:
            lifecycle_journal.record_terminal(self.root, row)

    def lifecycle_events(self) -> list[dict]:
        return lifecycle_journal.load_events(self.root, allow_legacy_prefix=False)

    def attempt(self) -> pathlib.Path:
        attempts = list((self.root / "coordinator/invocations").glob("*/*"))
        self.assertEqual(len(attempts), 1)
        return attempts[0]

    def test_success_is_content_bound_promotes_and_records_all_models_and_usage(self) -> None:
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "completed")
        for target in (
            "report/REPORT_v1.md", "report/ADJUDICATION.md",
            "work/findings.jsonl", "report/retired_claims.txt", "report/REPORT_final.md",
        ):
            self.assertTrue((self.root / target).is_file(), target)
        self.assertEqual(
            (self.root / "report/REPORT_v1.md").read_bytes(),
            (self.root / "report/REPORT_final.md").read_bytes(),
        )
        coordinator = self.ledger()[-1]
        self.assertEqual(coordinator["role"], "coordinator")
        self.assertEqual((coordinator["requested_model"], coordinator["reasoning_effort"]),
                         (self.route["requested_model"], self.route["reasoning_effort"]))
        self.assertEqual(coordinator["observed_model"], self.served)
        self.assertEqual(
            coordinator["observed_model_ids"],
            [self.served, fake_claude_coordinator.HELPER],
        )
        usage = coordinator["usage"]
        self.assertEqual(usage["input_tokens"], 105)
        self.assertEqual(usage["output_tokens"], 42)
        self.assertEqual(usage["cached_input_tokens"], 10)
        self.assertEqual(usage["cache_creation_input_tokens"], 5)
        self.assertEqual(usage["total_tokens"], 162)
        self.assertEqual(usage["total_tokens_kind"], "derived")
        attempt = self.attempt()
        manifest = json.loads((attempt / "source/input_manifest.json").read_text())
        reports = [row for row in manifest["files"] if row["kind"] == "phase1_report"]
        self.assertEqual(len(reports), 5)
        args = json.loads((attempt / "output.partial/fake_args.json").read_text())["argv"]
        self.assertEqual(args[args.index("--model") + 1], self.route["spawn_alias"])
        self.assertEqual(args[args.index("--effort") + 1], self.route["reasoning_effort"])
        self.assertNotIn("Bash", args[args.index("--tools") + 1])
        settings = json.loads((attempt / "claude_settings.json").read_text())
        self.assertEqual(settings["sandbox"]["network"]["deniedDomains"], ["*"])
        self.assertFalse(attempt.stat().st_mode & stat.S_IWUSR)

    def test_metadata_21_success_uses_explicit_bound_lifecycle(self) -> None:
        self.upgrade_to_metadata_21()
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        coordinator = self.ledger()[-1]
        self.assertEqual(coordinator["attempt_kind"], "role_inference")
        self.assertEqual(coordinator["provider_request_status"], "accepted")
        self.assertEqual(coordinator["inference_start_status"], "started")
        self.assertEqual(coordinator["attempt"], 1)
        self.assertEqual(coordinator["served_model"], self.served)
        self.assertEqual(coordinator["identity_assurance"], "observed")
        self.assertEqual(coordinator["turn_count"], 2)
        self.assertEqual(coordinator["provider_duration_seconds"], 1.25)
        self.assertEqual(coordinator["provider_cost"]["amount"], 0.0125)
        self.assertEqual(
            [row["model"] for row in coordinator["usage_by_model"]],
            [self.served, fake_claude_coordinator.HELPER],
        )
        self.assertIsNone(coordinator["unattributed_usage"])
        events = [
            event for event in self.lifecycle_events()
            if (event.get("payload") or {}).get("invocation_id") == coordinator["invocation_id"]
        ]
        self.assertEqual(
            [event["type"] for event in events],
            ["invocation_reserved", "provider_request", "provider_request",
             "inference_start", "provider_response", "invocation_terminal"],
        )
        self.assertEqual(events[1]["payload"]["status"], "requested")
        self.assertEqual(events[2]["payload"]["status"], "accepted")

    def test_metadata_21_pre_inference_rejections_do_not_consume_attempt(self) -> None:
        expected = {
            "authentication_rejection": "authentication",
            "quota_rejection": "quota",
            "schema_rejection": "schema",
            "capability_rejection": "capability",
        }
        for index, (scenario, error_class) in enumerate(expected.items()):
            with self.subTest(scenario=scenario):
                if index:
                    self.tearDown(); self.setUp()
                self.upgrade_to_metadata_21()
                result = self.run_runner(scenario)
                self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
                coordinator = self.ledger()[-1]
                self.assertEqual(coordinator["error_class"], error_class)
                self.assertIsNone(coordinator["attempt"])
                self.assertEqual(coordinator["provider_request_status"], "rejected")
                self.assertEqual(coordinator["inference_start_status"], "not_started")
                invocation_events = [
                    event["type"] for event in self.lifecycle_events()
                    if (event.get("payload") or {}).get("invocation_id")
                    == coordinator["invocation_id"]
                ]
                self.assertNotIn("inference_start", invocation_events)
                self.assertNotIn("provider_response", invocation_events)

    def test_metadata_21_rejection_leaves_attempt_one_for_next_inference(self) -> None:
        self.upgrade_to_metadata_21()
        rejected = self.run_runner("authentication_rejection")
        self.assertEqual(rejected.returncode, 2, rejected.stderr + rejected.stdout)
        completed = self.run_runner()
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        coordinators = [row for row in self.ledger() if row["role"] == "coordinator"]
        self.assertEqual([row["attempt"] for row in coordinators], [None, 1])

    def test_metadata_21_output_validation_failure_binds_inference_response(self) -> None:
        self.upgrade_to_metadata_21()
        result = self.run_runner("bad_findings")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        coordinator = self.ledger()[-1]
        self.assertEqual((coordinator["attempt"], coordinator["error_class"]), (1, "schema"))
        invocation_events = [
            event["type"] for event in self.lifecycle_events()
            if (event.get("payload") or {}).get("invocation_id") == coordinator["invocation_id"]
        ]
        self.assertLess(invocation_events.index("provider_response"),
                        invocation_events.index("invocation_terminal"))

    def test_metadata_21_missing_served_id_is_requested_only_without_attempt(self) -> None:
        self.upgrade_to_metadata_21()
        result = self.run_runner("no_served_model")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        coordinator = self.ledger()[-1]
        self.assertIsNone(coordinator["served_model"])
        self.assertEqual(coordinator["identity_assurance"], "requested_only")
        self.assertIsNone(coordinator["attempt"])

    def test_metadata_21_reserves_before_popen_and_requests_only_after_launch(self) -> None:
        self.upgrade_to_metadata_21()
        real_popen = run_coordinator.subprocess.Popen
        observed = {"checked": False}

        def checked_popen(*args, **kwargs):
            coordinator_events = []
            for event in self.lifecycle_events():
                payload = event.get("payload") or {}
                if (
                    event["type"] == "invocation_reserved"
                    and payload.get("role") == "coordinator"
                ) or (
                    event["type"] == "provider_request"
                    and str(payload.get("invocation_id", "")).startswith("coordinator-")
                ):
                    coordinator_events.append(event)
            self.assertEqual([event["type"] for event in coordinator_events],
                             ["invocation_reserved"])
            observed["checked"] = True
            return real_popen(*args, **kwargs)

        with mock.patch.object(run_coordinator.subprocess, "Popen", side_effect=checked_popen):
            result = self.run_main()
        self.assertEqual(result, 0)
        self.assertTrue(observed["checked"])

    def test_metadata_21_popen_failure_is_terminal_without_request_or_attempt(self) -> None:
        self.upgrade_to_metadata_21()
        with mock.patch.object(
            run_coordinator.subprocess, "Popen", side_effect=OSError("planted launch failure")
        ):
            result = self.run_main()
        self.assertEqual(result, 2)
        coordinator = self.ledger()[-1]
        self.assertFalse(coordinator["provider_call"])
        self.assertIsNone(coordinator["attempt"])
        self.assertEqual(coordinator["provider_request_status"], "not_requested")
        self.assertEqual(coordinator["inference_start_status"], "not_started")
        invocation_events = [
            event["type"] for event in self.lifecycle_events()
            if (event.get("payload") or {}).get("invocation_id") == coordinator["invocation_id"]
        ]
        self.assertEqual(invocation_events, ["invocation_reserved", "invocation_terminal"])

    def test_wall_elapsed_uses_persisted_timestamp_interval(self) -> None:
        started = dt.datetime(2026, 8, 28, 10, 0, 0, 125000, tzinfo=dt.timezone.utc)
        ended = dt.datetime(2026, 8, 28, 11, 2, 3, 581000, tzinfo=dt.timezone.utc)
        self.assertEqual(run_coordinator.wall_elapsed_seconds(started, ended), 3723.456)
        with self.assertRaisesRegex(run_coordinator.CoordinatorError, "precedes"):
            run_coordinator.wall_elapsed_seconds(ended, started)

    def test_inference_start_proof_requires_all_four_independent_signals(self) -> None:
        usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
        self.assertTrue(run_coordinator.inference_evidence_complete(
            "claude-sonnet-5", usage, 1, 0.5,
        ))
        self.assertFalse(run_coordinator.inference_evidence_complete(None, usage, 1, 0.5))
        self.assertFalse(run_coordinator.inference_evidence_complete(
            "claude-sonnet-5", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            1, 0.5,
        ))
        self.assertFalse(run_coordinator.inference_evidence_complete(
            "claude-sonnet-5", usage, 0, 0.5,
        ))
        self.assertFalse(run_coordinator.inference_evidence_complete(
            "claude-sonnet-5", usage, 1, 0,
        ))

    def test_popen_failure_is_terminal_and_does_not_claim_a_provider_call(self) -> None:
        with mock.patch.object(
            run_coordinator.subprocess, "Popen", side_effect=OSError("planted launch failure")
        ):
            result = self.run_main()
        self.assertEqual(result, 2)
        attempt = self.attempt()
        status = json.loads((attempt / "status.json").read_text())
        self.assertEqual(status["state"], "failed")
        self.assertIs(status["provider_call_started"], False)
        coordinator = self.ledger()[-1]
        self.assertEqual(coordinator["status"], "failed")
        self.assertIs(coordinator["provider_call"], False)
        self.assertEqual(coordinator["cache_usage"], "not_applicable")

    def test_post_provider_exception_is_terminal_and_consumes_attempt_budget(self) -> None:
        with mock.patch.object(
            run_coordinator, "promote_and_append", side_effect=OSError("planted promotion failure")
        ):
            result = self.run_main()
        self.assertEqual(result, 2)
        attempt = self.attempt()
        status = json.loads((attempt / "status.json").read_text())
        self.assertEqual(status["state"], "failed")
        self.assertIs(status["provider_call_started"], True)
        coordinator = self.ledger()[-1]
        self.assertEqual((coordinator["status"], coordinator["attempt"]), ("failed", 1))
        self.assertIs(coordinator["provider_call"], True)
        self.assertIn("planted promotion failure", coordinator["error"])

    def test_completed_ledger_survives_status_write_failure_and_is_reused(self) -> None:
        real_atomic_write = run_coordinator.atomic_write
        failed = False

        def fail_completed_status(path: pathlib.Path, data: bytes, mode: int = 0o600) -> None:
            nonlocal failed
            payload = json.loads(data)
            if path.name == "status.json" and payload.get("state") == "completed" and not failed:
                failed = True
                raise OSError("planted completed-status failure")
            real_atomic_write(path, data, mode)

        with mock.patch.object(run_coordinator, "atomic_write", side_effect=fail_completed_status):
            result = self.run_main()
        self.assertEqual(result, 0)
        coordinator = self.ledger()[-1]
        self.assertEqual(coordinator["status"], "completed")
        attempts_before = list((self.root / "coordinator/invocations").glob("*/*"))
        rerun = self.run_runner()
        self.assertEqual(rerun.returncode, 0, rerun.stderr + rerun.stdout)
        self.assertIs(json.loads(rerun.stdout)["reused"], True)
        self.assertEqual(
            list((self.root / "coordinator/invocations").glob("*/*")), attempts_before
        )

    def test_metadata_2_1_repeat_invocation_reuses_the_completed_call(self) -> None:
        """Under 2.1, run.json is a journal projection that moves on every event -- including the
        coordinator's own terminal row -- so the packet hash never matched again and the reuse
        path was unreachable: a repeat invocation hard-failed with "completed for different
        immutable inputs" when nothing but the journal's record of the call had changed."""
        self.upgrade_to_metadata_21()
        first = self.run_runner()
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        attempts_before = sorted((self.root / "coordinator/invocations").glob("*/*"))
        rerun = self.run_runner()
        self.assertEqual(rerun.returncode, 0, rerun.stderr + rerun.stdout)
        self.assertIs(json.loads(rerun.stdout)["reused"], True)
        self.assertEqual(sorted((self.root / "coordinator/invocations").glob("*/*")), attempts_before)

    def test_metadata_2_1_changed_context_still_refuses_reuse(self) -> None:
        """Negative control: only run.json may differ; a changed package manifest is real drift.

        (A Phase-1 report is bound by its ledger row's output hash, so editing one trips the
        ledger check before the packet comparison is reached; the manifest is bound only by the
        packet.)"""
        self.upgrade_to_metadata_21()
        self.assertEqual(self.run_runner().returncode, 0)
        manifest = self.root / "package_manifest.json"
        manifest.write_text(json.dumps({"schema_version": "2.0.0", "files": [],
                                        "note": "edited after the coordinator ran"}) + "\n")
        rerun = self.run_runner()
        self.assertNotEqual(rerun.returncode, 0)
        self.assertIn("different immutable inputs", rerun.stderr)
        self.assertIn("context/package_manifest.json", rerun.stderr)

    def test_audit_context_with_a_private_path_is_refused_before_dispatch(self) -> None:
        """PAPER.txt is operator-written free text that SKILL.md asks to carry the package
        location. It was the one class of snapshot input mounted without the provider-safety
        scan, so a home path went to the model and recover_coordinator later refused the same
        bytes."""
        (self.root / "PAPER.txt").write_text(
            "Package location: /" + "Users/jsmith/Dropbox/deposits/example/replication_package\n")
        result = self.run_runner()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provider-safe", result.stderr)
        self.assertIn("context/PAPER.txt", result.stderr)
        self.assertFalse(list((self.root / "coordinator/invocations").glob("*/*")),
                         "an attempt directory was created despite the refusal")

    def test_a_transport_failure_is_retried_within_the_same_invocation(self) -> None:
        """The runner used to record the failure and exit; each further attempt needed a human."""
        self.upgrade_to_metadata_21()
        state = self.root.parent / "fake-coordinator-state.txt"
        with mock.patch.dict(os.environ, {"FAKE_COORDINATOR_STATE": str(state),
                                          "RETRY_BACKOFF_SECONDS": "0"}):
            result = self.run_runner("transport_then_success")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("[retry] coordinator attempt failed with a transport error", result.stderr)
        rows = [r for r in self.ledger() if r["role"] == "coordinator"]
        self.assertEqual([r["status"] for r in rows], ["failed", "completed"])
        self.assertEqual(rows[0]["error_class"], "transport")
        self.assertEqual(len(list((self.root / "coordinator/invocations").glob("*/*"))), 2)

    def test_a_permanent_failure_is_not_retried(self) -> None:
        """Negative control: only transport/transient loop; a provider error stops at once."""
        self.upgrade_to_metadata_21()
        with mock.patch.dict(os.environ, {"RETRY_BACKOFF_SECONDS": "0"}):
            result = self.run_runner("provider_error")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("[retry]", result.stderr)
        self.assertEqual(len(list((self.root / "coordinator/invocations").glob("*/*"))), 1)

    def test_unledgered_running_attempt_blocks_retry(self) -> None:
        stranded = self.root / "coordinator/invocations" / ("c" * 64) / "attempt_01_stranded"
        stranded.mkdir(parents=True)
        (stranded / "status.json").write_text(json.dumps({
            "schema_version": "1.0.0",
            "state": "running",
            "audit_id": "audit-test-coordinator",
            "invocation_id": "coordinator-stranded",
            "attempt": 1,
            "input_artifact_sha256": "c" * 64,
            "provider_call_started": True,
        }))
        result = self.run_runner()
        self.assertEqual(result.returncode, 1)
        self.assertIn("recover it before retrying", result.stderr)
        self.assertFalse(any(
            row.get("role") == "coordinator" and row.get("provider_call")
            for row in self.ledger()
        ))

    def test_schema_failure_never_replaces_existing_canonical_outputs(self) -> None:
        originals = {}
        for target in (
            "report/REPORT_v1.md", "report/ADJUDICATION.md",
            "work/findings.jsonl", "report/retired_claims.txt", "report/REPORT_final.md",
        ):
            path = self.root / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"original {target}\n")
            originals[target] = path.read_bytes()
        result = self.run_runner("bad_findings")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        for target, content in originals.items():
            self.assertEqual((self.root / target).read_bytes(), content)
        record = self.ledger()[-1]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_class"], "schema")
        self.assertIsNone(record["output_artifact"])

    def test_attempt_root_is_read_only_during_the_call_so_misplaced_writes_fail(self) -> None:
        # The CLI's Write tool ignores the sandbox filesystem rules. The third live coordinator
        # call wrote all four outputs one level above output.partial and lost a $7 attempt.
        # The runner now makes the attempt root read-only for the call's duration.
        result = self.run_runner("misplaced_outputs")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        attempts = sorted((self.root / "coordinator/invocations").glob("*/attempt_*"))
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        for name in run_coordinator.OUTPUT_TARGETS:
            self.assertFalse((attempt / name).exists(), f"{name} must not land in the attempt root")
            self.assertFalse((self.root / run_coordinator.OUTPUT_TARGETS[name]).exists())
        # The runner restores write access after the call (then freezes the finished attempt),
        # so its own post-call artifacts must exist in the attempt root.
        for own in ("claude_stdout.json", "claude_stderr.txt", "permission_denials.json", "status.json"):
            self.assertTrue((attempt / own).is_file(), f"runner could not write {own} after the call")
        records = [row for row in self.ledger() if row.get("role") == "coordinator"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "failed")

    def test_missing_output_and_incomplete_usage_fail_closed(self) -> None:
        for scenario in ("missing_output", "partial_usage"):
            with self.subTest(scenario=scenario):
                if scenario != "missing_output":
                    self.tearDown(); self.setUp()
                result = self.run_runner(scenario)
                self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
                self.assertFalse((self.root / "report/REPORT_v1.md").exists())
                self.assertEqual(self.ledger()[-1]["status"], "failed")

    def test_wrong_or_ambiguous_primary_model_fails_and_preserves_ids(self) -> None:
        for scenario, expected_count in (("wrong_model", 1), ("two_sonnet", 2)):
            with self.subTest(scenario=scenario):
                if scenario != "wrong_model":
                    self.tearDown(); self.setUp()
                result = self.run_runner(scenario)
                self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
                record = self.ledger()[-1]
                self.assertEqual(record["status"], "failed")
                self.assertEqual(len(record["observed_model_ids"]), expected_count)
                self.assertFalse((self.root / "report/REPORT_v1.md").exists())

    def test_live_input_change_after_call_is_rejected_as_stale(self) -> None:
        result = self.run_runner("success", mutate=self.draft)
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIn("became stale", self.ledger()[-1]["error"])
        self.assertFalse((self.root / "report/REPORT_v1.md").exists())

    def test_pre_terra_rows_cannot_be_approved_or_settled(self) -> None:
        result = self.run_runner("premature_state")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        record = self.ledger()[-1]
        self.assertEqual(record["status"], "failed")
        self.assertIn("prematurely coordinator-approved", record["error"])
        self.assertFalse((self.root / "report/REPORT_v1.md").exists())

    def test_safe_evidence_rejects_private_paths_and_participant_codes_before_provider(self) -> None:
        unsafe_values = (
            "Local command: /" + "Users/example/private/audit.R\n",
            "Unsafe row: SA_P1,2022-05-09,118,74\n",
        )
        for index, value in enumerate(unsafe_values):
            with self.subTest(value=value):
                if index:
                    self.tearDown(); self.setUp()
                safe = self.root / "work/safe_evidence/summary.md"
                safe.write_text(value)
                manifest = json.loads(self.safe_manifest.read_text())
                entry = next(item for item in manifest["artifacts"] if item["path"] == "summary.md")
                entry["sha256"] = digest(safe)
                entry["bytes"] = safe.stat().st_size
                self.safe_manifest.write_text(json.dumps(manifest) + "\n")
                result = self.run_runner()
                self.assertEqual(result.returncode, 1)
                self.assertIn("not provider-safe", result.stderr)
                self.assertFalse((self.root / "coordinator").exists())

    def quote_in_phase_one_report(self, sentence: str) -> None:
        """Append to the reproduction report and re-bind its invocation record and draft row."""
        path = self.root / "report/01_reproduction.md"
        path.write_text(path.read_text() + sentence + "\n")
        records = [invocation(role, relative, digest(self.root / relative), self.auditor_routes[role])
                   for role, relative in ROLES.items()]
        (self.root / "invocations.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in records)
        )
        self.draft.write_text(json.dumps(draft_row(digest(path)), sort_keys=True) + "\n")

    def test_phase_one_report_quoting_deposit_column_names_reaches_the_provider(self) -> None:
        # 2.6.5: the video-speed audit's reports quoted SPSS columns INT_P05/DUR_P15 and the
        # runner refused them before any provider call. A code-shaped column name of the deposit
        # itself is not a participant code.
        (self.root / "pkg").mkdir()
        (self.root / "pkg/study.csv").write_text("id,INT_P05,DUR_P15\n1,3.2,1.1\n2,2.9,1.4\n")
        self.quote_in_phase_one_report("The INT_P05 and DUR_P15 means reproduce exactly.")
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue((self.root / "report/REPORT_v1.md").exists())

    def test_code_shaped_token_without_deposit_evidence_is_still_refused(self) -> None:
        # Negative controls: no deposit column of that name, or a column whose name is also a
        # participant value, admits nothing -- the pre-2.6.5 verdict.
        for layout in (None, "id,INT_P05\nINT_P05,3\n"):
            with self.subTest(layout=layout):
                self.tearDown(); self.setUp()
                if layout is not None:
                    (self.root / "pkg").mkdir()
                    (self.root / "pkg/study.csv").write_text(layout)
                self.quote_in_phase_one_report("The INT_P05 mean reproduces exactly.")
                result = self.run_runner()
                self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
                self.assertIn("not provider-safe", result.stderr)
                self.assertFalse((self.root / "coordinator").exists())

    def test_safe_evidence_manifest_is_privacy_scanned_before_provider(self) -> None:
        manifest = json.loads(self.safe_manifest.read_text())
        manifest["checksum_closure"] = (
            "built at /" + "Users/private-account/audit/SHA256SUMS"
        )
        self.safe_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
        result = self.run_runner()
        self.assertEqual(result.returncode, 1)
        self.assertIn("safe-evidence manifest contains a private absolute path", result.stderr)
        self.assertFalse((self.root / "coordinator").exists())

    def test_missing_phase_one_quorum_stops_before_provider(self) -> None:
        (self.root / "report/04_code_methods.md").unlink()
        result = self.run_runner()
        self.assertEqual(result.returncode, 1)
        self.assertIn("completed invocation output is missing", result.stderr)
        self.assertFalse((self.root / "coordinator").exists())
        self.assertEqual(len(self.ledger()), 5)

    def test_mid_promotion_failure_rolls_back_every_canonical_output(self) -> None:
        output = self.root / "coordinator/test-output"
        output.mkdir(parents=True)
        originals = {}
        artifacts = {}
        for name, relative in run_coordinator.OUTPUT_TARGETS.items():
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"old {name}\n")
            originals[relative] = target.read_bytes()
            source = output / name
            source.write_text(f"new {name}\n")
            artifacts[name] = {
                "canonical_path": relative,
                "sha256": digest(source),
                "bytes": source.stat().st_size,
            }
        final_target = self.root / run_coordinator.WORKING_REPORT_TARGET
        final_target.write_text("old REPORT_final.md\n")
        originals[run_coordinator.WORKING_REPORT_TARGET] = final_target.read_bytes()
        artifacts["REPORT_final.md"] = {
            "canonical_path": run_coordinator.WORKING_REPORT_TARGET,
            "source_output": "REPORT_v1.md",
            "sha256": artifacts["REPORT_v1.md"]["sha256"],
            "bytes": artifacts["REPORT_v1.md"]["bytes"],
        }
        bundle = self.root / "coordinator/test-bundle.json"
        bundle.write_text("{}\n")
        record = {
            "invocation_id": "coordinator-rollback-test",
            "role": "coordinator", "role_class": "coordinator", "round": None,
            "attempt": 1, "status": "completed", "provider": "anthropic",
            "provider_call": True, "cache_usage": "none", "session_id": "session-rollback",
            "requested_model": self.route["requested_model"], "observed_model": self.served,
            "observed_model_ids": [self.served], "model_family": "claude",
            "model_source": "observed", "reasoning_effort": self.route["reasoning_effort"],
            "started_at": "2026-08-28T12:00:00Z", "ended_at": "2026-08-28T12:01:00Z",
            "elapsed_seconds": 60, "input_artifact_sha256": "a" * 64,
            "prompt_sha256": "b" * 64,
            "output_artifact": "coordinator/test-bundle.json", "output_sha256": digest(bundle),
            "usage": {"obtainable": True, "input_tokens": 10, "output_tokens": 5,
                      "cached_input_tokens": 0, "reasoning_tokens": 0,
                      "total_tokens": 15, "source": "api_response"},
            "error": None, "error_class": None, "legacy_source": None,
        }
        real_replace = os.replace

        def fail_on_second_target(source: str | os.PathLike, destination: str | os.PathLike) -> None:
            if pathlib.Path(destination) == self.root / "report/ADJUDICATION.md":
                raise OSError("planted promotion failure")
            real_replace(source, destination)

        with mock.patch.object(run_coordinator.os, "replace", side_effect=fail_on_second_target):
            with self.assertRaisesRegex(OSError, "planted promotion failure"):
                run_coordinator.promote_and_append(
                    self.root, output, artifacts, record, [], {"files": []}
                )
        for relative, content in originals.items():
            self.assertEqual((self.root / relative).read_bytes(), content)
        self.assertFalse(any(row.get("role") == "coordinator" for row in self.ledger()))


if __name__ == "__main__":
    unittest.main()
