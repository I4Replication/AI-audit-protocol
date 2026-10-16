from __future__ import annotations

import concurrent.futures
import hashlib
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


SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SKILL / "tests"))
import phase1_runtime
import record_invocation
import lifecycle_journal
import run_phase1
import fake_claude_phase1


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def auditor_route(root: pathlib.Path) -> dict:
    """The Claude route this audit's snapshot froze for the auditors (2.7.0: Sonnet 5.5 for an
    audit initialised from the shipped policy, Sonnet 5 for every audit initialised before)."""
    rows = record_invocation.load_policy_snapshot(root)["models"]["phase_1_auditors"].values()
    routes = {(row["requested_model"], row["spawn_alias"], row["reasoning_effort"]) for row in rows}
    assert len(routes) == 1, routes
    requested, alias, effort = routes.pop()
    return {"requested_model": requested, "spawn_alias": alias, "reasoning_effort": effort}


class Phase1RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-phase1-test-")
        self.base = pathlib.Path(self.temporary.name)
        self.root = self.make_audit("audit")
        self.fake = self.base / "fake-claude-phase1"
        shutil.copyfile(SKILL / "tests/fake_claude_phase1.py", self.fake)
        self.fake.chmod(0o755)
        self.state = self.base / "fake-state.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_audit(self, name: str, *, metadata21: bool = False) -> pathlib.Path:
        root = self.base / name
        for directory in ("paper", "pkg", "report", "work", "critique"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        (root / "paper/paper.txt").write_text(
            "Synthetic paper text for a bounded reproducibility fixture. " * 80 + "\n",
            encoding="utf-8",
        )
        (root / "pkg/data.csv").write_text(
            "participant,value,group\nparticipant_A1,1,A\nparticipant_A2,,B\n"
            "participant_A2,,B\ncontact@example.org,3,A\n",
            encoding="utf-8",
        )
        (root / "pkg/analysis.py").write_text(
            "if __name__ == '__main__':\n    print('aggregate analysis')\n",
            encoding="utf-8",
        )
        (root / "pkg/LICENSE").write_text("Synthetic permissive licence.\n", encoding="utf-8")
        # The repair-packet ratio is measured honestly against the immutable original packet.
        (root / "pkg/binary-input.bin").write_bytes(b"x" * 120_000)
        self.write_package_manifest(root)
        record_invocation.initialize_policy_snapshot(root)
        policy_hash = digest(root / "pipeline-policy.snapshot.json")
        (root / "run.json").write_text(json.dumps({
            "schema_version": "2.0.0",
            "audit_id": f"audit-{name}",
            "normalization": None,
            "toolkit": {"pipeline_policy_snapshot_sha256": policy_hash},
        }, indent=2) + "\n", encoding="utf-8")
        (root / "invocations.jsonl").write_text("", encoding="utf-8")
        if metadata21:
            lifecycle_journal.initialize(root)
        return root

    def write_package_manifest(self, root: pathlib.Path) -> None:
        rows = []
        for path in sorted((root / "pkg").rglob("*")):
            if path.is_file():
                rows.append({
                    "path": path.relative_to(root / "pkg").as_posix(),
                    "size": path.stat().st_size,
                    "sha256": digest(path),
                    "extension": path.suffix.lower().lstrip(".") or "<none>",
                })
        (root / "package_manifest.json").write_text(json.dumps({
            "schema_version": "2.0.0",
            "algorithm": "sha256",
            "package_sha256": hashlib.sha256("".join(
                f"{row['path']}\0{row['sha256']}\n" for row in rows
            ).encode()).hexdigest(),
            "file_count": len(rows),
            "file_types": {},
            "files": rows,
        }, indent=2) + "\n", encoding="utf-8")

    def run_runner(
        self,
        scenario: str = "success",
        *,
        root: pathlib.Path | None = None,
        state: pathlib.Path | None = None,
        target: str = "auditor_reproduction",
        timeout: int = 45,
        pre_dispatch_deadline: float | None = None,
        extra_env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({
            "FAKE_PHASE1_SCENARIO": scenario,
            "FAKE_PHASE1_TARGET": target,
            "FAKE_PHASE1_STATE": str(state or self.state),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        if extra_env:
            env.update(extra_env)
        command = [
            sys.executable,
            str(SCRIPTS / "run_phase1.py"),
            str(root or self.root),
            "--claude-bin", str(self.fake),
        ]
        if pre_dispatch_deadline is not None:
            command.extend([
                "--pre-dispatch-deadline-seconds", str(pre_dispatch_deadline),
            ])
        if extra_args:
            command.extend(extra_args)
        return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
           timeout=timeout, check=False)

    def events(self, state: pathlib.Path | None = None) -> list[dict]:
        path = state or self.state
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def starts(self, state: pathlib.Path | None = None) -> list[dict]:
        return [row for row in self.events(state) if row["kind"] == "start"]

    def ledger(self, root: pathlib.Path | None = None) -> list[dict]:
        path = (root or self.root) / "invocations.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_normal_path_is_one_probe_plus_five_parallel_first_try_auditors(self) -> None:
        started = time.monotonic()
        result = self.run_runner("slow_parallel")
        duration = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "quorum_complete")
        calls = self.starts()
        self.assertEqual(len(calls), 6)
        probes = [row for row in calls if row["role"] == "phase1_capability_probe"]
        roles = [row for row in calls if row["role"] in phase1_runtime.ROLE_SPECS]
        self.assertEqual(len(probes), 1)
        self.assertEqual({row["role"] for row in roles}, set(phase1_runtime.ROLE_SPECS))
        self.assertTrue(all(sum(call["role"] == row["role"] for call in roles) == 1 for row in roles))
        role_times = [row["at"] for row in roles]
        self.assertLess(max(role_times) - min(role_times), 0.22)
        self.assertLess(duration, 5)
        for role, spec in phase1_runtime.ROLE_SPECS.items():
            report = self.root / spec["report"]
            self.assertTrue(report.is_file(), role)
            self.assertIn(f"i4r-role-complete:{role}", report.read_text())
        records = [row for row in self.ledger() if row.get("role") in phase1_runtime.ROLE_SPECS]
        self.assertEqual(len(records), 5)
        self.assertTrue(all(row["status"] == "completed" and row["attempt"] == 1 for row in records))
        argv = roles[0]["argv"]
        self.assertIn("--safe-mode", argv)
        # --bare would refuse OAuth/keychain auth; the coordinator never passed it either.
        self.assertNotIn("--bare", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--strict-mcp-config", argv)
        route = auditor_route(self.root)
        self.assertEqual(argv[argv.index("--model") + 1], route["spawn_alias"])
        self.assertEqual(argv[argv.index("--effort") + 1], route["reasoning_effort"])
        self.assertTrue(all(row["requested_model"] == route["requested_model"] for row in records))
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "15")
        self.assertIn("Bash(python3 *)", argv)

    def test_failed_attempt_records_the_session_id_it_requested(self) -> None:
        # A provider rejection returns no session_id of its own, and the requested one was
        # minted inline as a positional argument and discarded, so a failed attempt had no
        # on-disk session identity at all -- exactly the case where you want one.
        result = self.run_runner("auth_failure")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        start = next(row for row in self.starts() if row["role"] == "auditor_reproduction")
        argv = start["argv"]
        requested = argv[argv.index("--session-id") + 1]
        record = next(row for row in self.ledger() if row["role"] == "auditor_reproduction")
        self.assertEqual(record["session_id"], requested)
        attempt = next((self.root / "phase1/roles/auditor_reproduction").glob("*/attempt_*"))
        status = json.loads((attempt / "status.json").read_text())
        self.assertEqual(status["requested_session_id"], requested)

    def test_sandbox_denial_is_recorded_not_fatal(self) -> None:
        # A denial means the sandbox refused a command and the model recovered. On audit
        # A34 the runner treated it as an authorization failure and discarded
        # five complete, schema-valid reports. It must complete the role and leave a receipt.
        result = self.run_runner("denials")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "quorum_complete")
        receipts = sorted((self.root / "phase1" / "roles").rglob("sandbox_denials.json"))
        self.assertTrue(receipts, "every denied attempt must leave sandbox_denials.json")
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual(receipt["schema_version"], "i4r-phase1-sandbox-denials-1")
        self.assertEqual(receipt["denial_count"], 1)
        self.assertEqual(receipt["denials"][0]["tool_name"], "Bash")
        self.assertIn("cp ./scratch", receipt["denials"][0]["command"])
        records = [row for row in self.ledger() if row.get("role") in phase1_runtime.ROLE_SPECS]
        self.assertTrue(all(row["status"] == "completed" for row in records))

    def test_probe_only_runs_no_auditor(self) -> None:
        result = self.run_runner(extra_args=["--probe-only"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "probe_only_complete")
        self.assertEqual(summary["auditor_inferences_started"], 0)
        self.assertLessEqual(summary["normalized_total_tokens"], summary["probe_max_normalized_tokens"])
        self.assertEqual([row["role"] for row in self.starts()], ["phase1_capability_probe"])
        self.assertFalse(any(row.get("role") in phase1_runtime.ROLE_SPECS for row in self.ledger()))
        # The canary must leave the audit in a state the full run resumes from, reusing the
        # cached probe rather than paying for a second one.
        full = self.run_runner()
        self.assertEqual(full.returncode, 0, full.stderr + full.stdout)
        full_summary = json.loads(full.stdout)
        self.assertEqual(full_summary["status"], "quorum_complete")
        self.assertTrue(full_summary["probe_reused"])
        probes = [row for row in self.starts() if row["role"] == "phase1_capability_probe"]
        self.assertEqual(len(probes), 1, "the full run must not re-run the probe")

    def test_deterministic_evidence_is_aggregate_and_snapshot_is_read_only(self) -> None:
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        summary = json.loads(result.stdout)
        snapshot = self.root / "phase1/snapshots" / summary["source_snapshot_sha256"]
        evidence = snapshot / "evidence"
        index = json.loads((evidence / "evidence_index.json").read_text())
        self.assertIs(index["participant_rows_emitted"], False)
        self.assertIs(index["model_conclusions_emitted"], False)
        books = json.loads((evidence / "workbook_summaries.json").read_text())
        csv_summary = books["workbooks"][0]["sheets"][0]
        self.assertEqual(csv_summary["duplicate_row_count"], 1)
        self.assertGreater(csv_summary["missing_by_column_index"][1], 0)
        derived = "\n".join(
            path.read_text(encoding="utf-8")
            for path in evidence.glob("*.json")
        )
        self.assertNotIn("contact@example.org", derived)
        self.assertNotIn("participant_A1", derived)
        privacy = json.loads((evidence / "privacy_scan.json").read_text())
        self.assertGreater(privacy["detector_match_counts"]["email"], 0)
        self.assertIs(privacy["matches_emitted"], False)
        role_start = next(
            row for row in self.starts() if row["role"] == "auditor_reproduction"
        )
        argv = role_start["argv"]
        source_dir = pathlib.Path(argv[argv.index("--add-dir") + 1])
        packet_dir = pathlib.Path(argv[argv.index("--add-dir") + 2])
        self.assertEqual(source_dir.resolve(), (snapshot / "raw").resolve())
        self.assertFalse((source_dir / "evidence").exists())
        packet_evidence = {
            path.relative_to(packet_dir / "evidence").as_posix()
            for path in (packet_dir / "evidence").rglob("*") if path.is_file()
        }
        self.assertEqual(
            packet_evidence,
            set(phase1_runtime.ROLE_SPECS["auditor_reproduction"]["evidence"]),
        )
        self.assertNotIn("privacy_scan.json", packet_evidence)
        settings = json.loads(pathlib.Path(argv[argv.index("--settings") + 1]).read_text())
        self.assertEqual(
            [pathlib.Path(value).resolve() for value in settings["sandbox"]["filesystem"]["allowRead"][:2]],
            [(snapshot / "raw").resolve(), packet_dir.resolve()],
        )
        self.assertNotIn(
            snapshot.resolve(),
            [pathlib.Path(value).resolve() for value in settings["sandbox"]["filesystem"]["allowRead"]],
        )
        for path in snapshot.rglob("*"):
            if path.is_file():
                self.assertFalse(path.stat().st_mode & stat.S_IWUSR, path)

    def test_source_or_sandbox_preflight_failure_starts_zero_provider_calls(self) -> None:
        target = self.root / "pkg/analysis.py"
        target.unlink()
        target.symlink_to(self.root / "pkg/LICENSE")
        started = time.monotonic()
        result = self.run_runner()
        duration = time.monotonic() - started
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertEqual(self.starts(), [])
        self.assertLess(duration, 60)
        status = json.loads((self.root / "phase1/status.json").read_text())
        self.assertEqual((status["status"], status["pause_reason"]), ("paused", "local_preflight_failed"))
        self.assertEqual(status["auditor_inferences_started"], 0)

    def test_glob_metacharacter_audit_root_pauses_before_any_provider_call(self) -> None:
        # The CLI compiles sandbox.filesystem entries as globs, so "?" in the audit path makes
        # every allowRead/allowWrite rule match nothing and every sandboxed Bash call is denied.
        # In production this shape spent 5.4M tokens across five roles and produced no report.
        # The fake CLI never emulates seatbelt, so before the guard existed this whole fan-out
        # ran green on this exact folder name -- the run must now stop in preflight instead.
        root = self.make_audit("A paper title that ends in a question mark?")
        state = self.base / "globroot-state.jsonl"
        result = self.run_runner(root=root, state=state)
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertEqual(self.starts(state), [])
        status = json.loads((root / "phase1/status.json").read_text())
        self.assertEqual((status["status"], status["pause_reason"]), ("paused", "local_preflight_failed"))
        self.assertEqual(status["auditor_inferences_started"], 0)

    def test_audit_root_with_safe_punctuation_is_not_rejected(self) -> None:
        # Negative control: audit A34's root held a colon and spaces and ran fine. The guard must
        # not become a blanket ban on punctuation in journal-article directory names.
        root = self.make_audit("Journal: A Study (2026) ~ final!")
        state = self.base / "safe-punct-state.jsonl"
        result = self.run_runner(root=root, state=state)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_output_fault_after_a_provider_call_is_terminal_and_consumes_budget(self) -> None:
        # A role that returns nothing still spent a full attempt. The bare Phase1Error from the
        # missing REPORT.md used to escape `except OutputValidationError`, so no invocation row
        # was written, the attempt stayed at state "response_received", and every later run
        # re-adopted it and re-raised the identical error -- a permanent, uncounted stall.
        root = self.make_audit("nooutput21", metadata21=True)
        state = self.base / "nooutput21-state.jsonl"
        first = self.run_runner("no_output", root=root, state=state)
        self.assertEqual(first.returncode, 2, first.stderr + first.stdout)
        rows = [row for row in self.ledger(root) if row["role"] == "auditor_reproduction"]
        starts = [row for row in self.starts(state) if row["role"] == "auditor_reproduction"]
        self.assertEqual(len(rows), len(starts))
        self.assertTrue(all(row["status"] == "failed" and row["provider_call"] for row in rows))
        self.assertTrue(all(row["error"] and row["error_class"] for row in rows))
        self.assertEqual(
            [json.loads(path.read_text())["state"] for path in sorted(
                (root / "phase1/roles/auditor_reproduction").rglob("status.json")
            )],
            ["failed"] * len(starts),
        )
        # Terminal, not stalled: the rerun must not re-adopt the dead attempt.
        second = self.run_runner("no_output", root=root, state=state)
        self.assertEqual(second.returncode, 2, second.stderr + second.stdout)
        self.assertIn("exhausted", json.loads(
            (root / "phase1/status.json").read_text()
        )["failed_roles"]["auditor_reproduction"])

    def test_post_call_verification_failure_leaves_one_terminal_invocation(self) -> None:
        # verify_snapshot runs after the call is paid for; its Phase1Error is not an
        # OutputValidationError and reached no recorder at all. Since 2.6.8 only a CONTENT change
        # fails it (a mode-only drift is re-frozen, see the next test), it is typed as an
        # environment fault, and the fan-out pauses instead of reporting the role failed.
        root = self.make_audit("tamper21", metadata21=True)
        state = self.base / "tamper21-state.jsonl"
        result = self.run_runner("snapshot_content_tamper", root=root, state=state)
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        starts = [row for row in self.starts(state) if row["role"] == "auditor_reproduction"]
        rows = [row for row in self.ledger(root) if row["role"] == "auditor_reproduction"]
        self.assertEqual(len(starts), 1, "a snapshot that changed under the call is not re-sampled")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIs(rows[0]["provider_call"], True)
        self.assertEqual(rows[0]["error_class"], "environment")
        self.assertIn("immutable snapshot source changed: source_files[", rows[0]["error"])
        self.assertNotIn("pkg/", rows[0]["error"], "no deposit file name in a recorded error")
        self.assertNotIn("paper/", rows[0]["error"], "no deposit file name in a recorded error")
        self.assertEqual(
            [json.loads(path.read_text())["state"] for path in sorted(
                (root / "phase1/roles/auditor_reproduction").rglob("status.json")
            )],
            ["failed"],
        )
        status = json.loads((root / "phase1/status.json").read_text())
        self.assertEqual((status["status"], status["pause_reason"]),
                         ("paused", "snapshot_integrity_fault"))
        self.assertIn("auditor_reproduction", status["environment_faults"])
        self.assertIn("phase1/snapshots/", status["remedy"])
        # Paused only through status.json: run.json's pause_reason enum has no such value.
        run = json.loads((root / "run.json").read_text())
        self.assertNotEqual(run.get("pause_reason"), "snapshot_integrity_fault")
        self.assertFalse([event for event in lifecycle_journal.load_events(root)
                          if event["type"] == "pause_start"])

    def test_post_call_mode_drift_is_refrozen_and_the_paid_output_kept(self) -> None:
        # The Dropbox File Provider shape: the frozen snapshot regains write bits during the call
        # while every byte is unchanged. 2.6.7 failed the paid attempt ("immutable snapshot source
        # is writable"); now the runner re-freezes in place, journals one note, and completes.
        root = self.make_audit("drift21", metadata21=True)
        state = self.base / "drift21-state.jsonl"
        result = self.run_runner("snapshot_mode_drift", root=root, state=state)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        starts = [row for row in self.starts(state) if row["role"] == "auditor_reproduction"]
        self.assertEqual(len(starts), 1, "the drifted attempt's output was kept, not re-sampled")
        rows = [row for row in self.ledger(root) if row["role"] == "auditor_reproduction"]
        self.assertEqual([row["status"] for row in rows], ["completed"])
        quorum = json.loads((root / "phase1/quorum.json").read_text())
        snapshot = root / "phase1/snapshots" / quorum["source_snapshot_sha256"]
        writable = [path for path in [snapshot, *snapshot.rglob("*")]
                    if path.stat().st_mode & 0o222]
        self.assertEqual(writable, [], "the snapshot is frozen again")
        phase1_runtime.verify_snapshot(snapshot)  # the strict check passes again, unchanged
        notes = [event["payload"] for event in lifecycle_journal.load_events(root)
                 if event["type"] == "note"
                 and event["payload"].get("kind") == "snapshot_mode_refrozen"]
        self.assertEqual(len(notes), 1, notes)
        note = notes[0]
        self.assertEqual(note["snapshot"], f"phase1/snapshots/{quorum['source_snapshot_sha256']}")
        self.assertEqual(note["snapshot_kind"], "phase1")
        self.assertTrue(note["context"].startswith(("post_call:", "pre_dispatch:")), note)
        self.assertEqual((note["path_count"], note["paths"]), (1, ["raw/paper"]))
        encoded = json.dumps(note)
        self.assertNotIn(str(root), encoded, "no absolute path in the journaled note")
        for name in ("data.csv", "analysis.py", "LICENSE", "paper.txt", "binary-input.bin"):
            self.assertNotIn(name, encoded, "no deposit file name in the journaled note")

    def test_pre_dispatch_check_refuses_before_any_attempt_or_reservation(self) -> None:
        # A snapshot whose content no longer verifies must stop a role before its attempt
        # directory, reservation or provider process exists -- not after the call is paid for.
        root = self.make_audit("predispatch21", metadata21=True)
        state = self.base / "predispatch21-state.jsonl"
        self.assertEqual(self.run_runner(root=root, state=state).returncode, 0)
        quorum = json.loads((root / "phase1/quorum.json").read_text())
        snapshot = root / "phase1/snapshots" / quorum["source_snapshot_sha256"]
        manifest = json.loads((snapshot / "snapshot_manifest.json").read_text())
        frozen = snapshot / "raw" / manifest["source_files"][0]["path"]
        frozen.chmod(0o644)
        with frozen.open("ab") as handle:
            handle.write(b"tamper\n")
        events_before = (root / "run.events.jsonl").read_bytes()
        attempts_before = sorted((root / "phase1/roles").rglob("attempt_*"))

        def no_dispatch(*_args, **_kwargs):
            raise AssertionError("dispatch reached run_process past a failed pre-dispatch check")

        original_process, original_reserve = run_phase1.run_process, run_phase1.lifecycle_reserve
        run_phase1.run_process = no_dispatch
        run_phase1.lifecycle_reserve = no_dispatch
        try:
            with self.assertRaises(phase1_runtime.SnapshotIntegrityError) as caught:
                run_phase1.role_command_and_attempt(
                    root, root / "phase1", snapshot, root / "phase1", "auditor_reproduction",
                    "0" * 64, "prompt", "0" * 64, str(self.fake), {}, 45, "15", "role_inference",
                    snapshot_manifest=manifest,
                )
        finally:
            run_phase1.run_process, run_phase1.lifecycle_reserve = original_process, original_reserve
        self.assertIsInstance(caught.exception, phase1_runtime.Phase1Error)
        self.assertEqual(sorted((root / "phase1/roles").rglob("attempt_*")), attempts_before)
        self.assertEqual((root / "run.events.jsonl").read_bytes(), events_before)
        self.assertTrue(frozen.stat().st_mode & 0o200, "a content failure changes no mode")

    def test_absolute_path_in_result_json_is_rejected_like_one_in_the_report(self) -> None:
        # REPORT.md has always been scanned; result.json never was. It is what feeds the draft
        # registry, so one unmasked path there made the coordinator refuse the whole 27-row
        # input -- every finding in the audit lost to one leak in one field.
        root = self.make_audit("resultleak")
        state = self.base / "resultleak-state.jsonl"
        result = self.run_runner("result_absolute_path", root=root, state=state)
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        rows = [row for row in self.ledger(root) if row["role"] == "auditor_reproduction"]
        self.assertTrue(rows, "the rejected attempt must still be recorded")
        self.assertTrue(any("result.json leaks a private absolute path" in (row.get("error") or "")
                            for row in rows), [row.get("error") for row in rows])
        # and the remedy is named, so the next attempt knows what to do
        self.assertTrue(any("windows-user-dir" in (row.get("error") or "") for row in rows))

    def test_a_leak_is_resampled_not_sent_to_focused_repair(self) -> None:
        """The leak sits in scientific_fields, which the focused repair must copy byte-equivalent,
        so a repair could never fix it: every such repair was a guaranteed-fail paid call. The
        fault is non-repairable now and says so; the role spends its budget on fresh samples."""
        root = self.make_audit("leak-resample")
        state = self.base / "leak-resample-state.jsonl"
        result = self.run_runner("result_absolute_path", root=root, state=state)
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        rows = [row for row in self.ledger(root) if row["role"] == "auditor_reproduction"]
        self.assertTrue(rows)
        self.assertFalse([row for row in rows if row.get("attempt_kind") == "output_repair"],
                         "a focused repair was dispatched for a fault it cannot fix")
        self.assertFalse(list((root / "phase1/roles/auditor_reproduction").glob("*/repair_*")))
        self.assertTrue(any("not focused-repairable" in (row.get("error") or "") for row in rows),
                        [row.get("error") for row in rows])
        # Positive control lives in test_one_focused_repair_preserves_scientific_fields_and_uses_
        # small_packet: a structural fault (repairable_output) DOES get one focused repair.

    def test_hanging_pdf_extractor_hits_one_global_deadline_before_any_inference(self) -> None:
        (self.root / "paper/paper.txt").unlink()
        (self.root / "paper/paper.pdf").write_bytes(b"%PDF-1.4\n% synthetic deadline fixture\n")
        fake_bin = self.base / "deadline-bin"
        fake_bin.mkdir()
        extractor = fake_bin / "pdftotext"
        extractor.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        extractor.chmod(0o755)
        started = time.monotonic()
        result = self.run_runner(
            pre_dispatch_deadline=0.6,
            extra_env={"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")},
            timeout=5,
        )
        duration = time.monotonic() - started
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertLess(duration, 2.5)
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.ledger(), [])
        status = json.loads((self.root / "phase1/status.json").read_text())
        self.assertEqual(status["pause_reason"], "local_preflight_failed")
        self.assertEqual(status["auditor_inferences_started"], 0)

    def test_symlinked_role_packet_store_fails_before_provider_or_external_write(self) -> None:
        phase = self.root / "phase1"
        phase.mkdir()
        external = self.base / "external-role-packets"
        external.mkdir()
        (phase / "role_packets").symlink_to(external, target_is_directory=True)
        result = self.run_runner()
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertEqual(self.starts(), [])
        self.assertEqual(list(external.iterdir()), [])
        status = json.loads((phase / "status.json").read_text())
        self.assertEqual(status["auditor_inferences_started"], 0)

    def test_canonical_timeout_and_budget_cannot_be_overridden(self) -> None:
        env = os.environ.copy()
        env["FAKE_PHASE1_STATE"] = str(self.state)
        for option, value in (("--timeout-seconds", "2699"), ("--max-budget-usd", "14.99")):
            result = subprocess.run([
                sys.executable, str(SCRIPTS / "run_phase1.py"), str(self.root),
                "--claude-bin", str(self.fake), option, value,
            ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("non-canonical and rejected", result.stderr)
        self.assertEqual(self.starts(), [])

    def test_failed_probe_pauses_before_auditors_and_is_bounded(self) -> None:
        result = self.run_runner("probe_failure")
        self.assertEqual(result.returncode, 4, result.stderr + result.stdout)
        calls = self.starts()
        self.assertEqual([row["role"] for row in calls], ["phase1_capability_probe"])
        self.assertFalse(any(row.get("role") in phase1_runtime.ROLE_SPECS for row in calls))
        record = self.ledger()[-1]
        self.assertEqual(record["role"], "phase1_capability_probe")
        total = record["usage"]["total_tokens"]
        self.assertLessEqual(total, run_phase1.probe_bound(record_invocation.load_policy_snapshot(self.root)))

    def test_successful_probe_and_role_completions_are_content_addressed_replays(self) -> None:
        first = self.run_runner()
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        calls_before = len(self.starts())
        ledger_before = len(self.ledger())
        second = self.run_runner()
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        summary = json.loads(second.stdout)
        self.assertIs(summary["probe_reused"], True)
        self.assertEqual(set(summary["roles"][0]), set(summary["roles"][0]))
        self.assertTrue(all(row["reused"] for row in summary["roles"]))
        self.assertEqual(len(self.starts()), calls_before)
        # A replay is not a fresh provider attempt and therefore cannot consume the three-call budget.
        provider_rows = [row for row in self.ledger() if row.get("provider_call")]
        self.assertEqual(len(provider_rows), ledger_before)

    def test_a_moved_audit_reuses_its_completed_roles(self) -> None:
        first = self.run_runner()
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        moved = self.base / "moved-audit"
        shutil.copytree(self.root, moved, symlinks=True)
        second = self.run_runner(root=moved)
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        summary = json.loads(second.stdout)
        self.assertTrue(all(row["reused"] for row in summary["roles"]), summary["roles"])

    def test_transient_failure_retries_but_authentication_failure_stops_immediately(self) -> None:
        transient = self.run_runner("transient_once")
        self.assertEqual(transient.returncode, 0, transient.stderr + transient.stdout)
        target_calls = [row for row in self.starts() if row["role"] == "auditor_reproduction"]
        self.assertEqual(len(target_calls), 2)
        target_records = [row for row in self.ledger() if row["role"] == "auditor_reproduction"]
        self.assertEqual([row["status"] for row in target_records], ["failed", "completed"])
        self.assertEqual([row["attempt"] for row in target_records], [1, 2])

        self.tearDown(); self.setUp()
        auth = self.run_runner("auth_failure")
        self.assertEqual(auth.returncode, 2, auth.stderr + auth.stdout)
        target_calls = [row for row in self.starts() if row["role"] == "auditor_reproduction"]
        self.assertEqual(len(target_calls), 1)
        record = next(row for row in self.ledger() if row["role"] == "auditor_reproduction")
        self.assertEqual(record["error_class"], "authentication")

    def test_metadata_21_rejections_do_not_consume_inference_attempts(self) -> None:
        root = self.make_audit("rejections21", metadata21=True)
        state = self.base / "rejections21-state.jsonl"
        transient = self.run_runner("transient_once", root=root, state=state)
        self.assertEqual(transient.returncode, 0, transient.stderr + transient.stdout)
        rows = [row for row in self.ledger(root) if row["role"] == "auditor_reproduction"]
        self.assertEqual([row["status"] for row in rows], ["failed", "completed"])
        self.assertEqual([row["attempt"] for row in rows], [None, 1])
        self.assertEqual(
            [row["provider_request_status"] for row in rows], ["rejected", "accepted"]
        )
        self.assertEqual(
            [row["inference_start_status"] for row in rows], ["not_started", "started"]
        )

        root = self.make_audit("auth21", metadata21=True)
        state = self.base / "auth21-state.jsonl"
        auth = self.run_runner("auth_failure", root=root, state=state)
        self.assertEqual(auth.returncode, 2, auth.stderr + auth.stdout)
        row = next(row for row in self.ledger(root) if row["role"] == "auditor_reproduction")
        self.assertIsNone(row["attempt"])
        self.assertEqual(row["provider_request_status"], "rejected")
        self.assertEqual(row["inference_start_status"], "not_started")
        self.assertEqual(row["identity_assurance"], "requested_only")
        events = lifecycle_journal.load_events(root)
        target_ids = {
            event["payload"]["invocation_id"] for event in events
            if event["type"] == "invocation_reserved"
            and event["payload"].get("role") == "auditor_reproduction"
        }
        started_ids = {
            event["payload"]["invocation_id"] for event in events
            if event["type"] == "inference_start"
        }
        self.assertFalse(target_ids & started_ids)

    def test_metadata_21_preinference_retry_circuit_breaker_survives_resume(self) -> None:
        root = self.make_audit("circuit21", metadata21=True)
        state = self.base / "circuit21-state.jsonl"
        first = self.run_runner("transient_always", root=root, state=state)
        self.assertEqual(first.returncode, 2, first.stderr + first.stdout)
        rows = [row for row in self.ledger(root) if row["role"] == "auditor_reproduction"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["attempt"] is None for row in rows))
        calls_before = len([
            row for row in self.starts(state) if row["role"] == "auditor_reproduction"
        ])
        second = self.run_runner("transient_always", root=root, state=state)
        self.assertEqual(second.returncode, 2, second.stderr + second.stdout)
        calls_after = len([
            row for row in self.starts(state) if row["role"] == "auditor_reproduction"
        ])
        self.assertEqual(calls_after, calls_before)
        rows = [row for row in self.ledger(root) if row["role"] == "auditor_reproduction"]
        self.assertEqual(len(rows), 2)

    def test_one_focused_repair_preserves_scientific_fields_and_uses_small_packet(self) -> None:
        result = self.run_runner("repairable_output")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        target_calls = [row for row in self.starts() if row["role"] == "auditor_reproduction"]
        self.assertEqual(len(target_calls), 2)
        records = [row for row in self.ledger() if row["role"] == "auditor_reproduction"]
        self.assertEqual([row["status"] for row in records], ["failed", "completed"])
        # Legacy schemas omit attempt_kind; metadata 2.1 records it explicitly.
        if "attempt_kind" in records[-1]:
            self.assertEqual([row["attempt_kind"] for row in records], ["role_inference", "output_repair"])
            self.assertEqual(records[-1]["source_invocation_id"], records[0]["invocation_id"])
        repair_attempt = next((self.root / "phase1/roles/auditor_reproduction").glob("*/repair_*"))
        repair_input = json.loads((repair_attempt / "input.json").read_text())
        original_attempt = next((self.root / "phase1/roles/auditor_reproduction").glob("*/attempt_*"))
        original_input = json.loads((original_attempt / "input.json").read_text())
        prior = json.loads((original_attempt / "output.partial/result.json").read_text())["scientific_fields"]
        repaired = json.loads((repair_attempt / "output.partial/result.json").read_text())["scientific_fields"]
        self.assertEqual(
            json.dumps(prior, sort_keys=True, separators=(",", ":")),
            json.dumps(repaired, sort_keys=True, separators=(",", ":")),
        )
        self.assertNotEqual(repair_input["input_sha256"], original_input["input_sha256"])
        frozen = json.loads((original_attempt / "repair_packet/SCIENTIFIC_PROJECTION.json").read_text())
        self.assertEqual(
            frozen["projection_sha256"],
            phase1_runtime.sha256_bytes(
                phase1_runtime.canonical_bytes(frozen["projection"])
            ),
        )
        self.assertEqual(
            frozen["projection"],
            phase1_runtime.scientific_projection(
                repair_attempt / "output.partial",
                "auditor_reproduction",
                json.loads(next((self.root / "phase1/snapshots").glob("*/snapshot_manifest.json")).read_text()),
            ),
        )

    def test_focused_repair_rejects_each_scientific_surface_mutation(self) -> None:
        surfaces = (
            "claim", "evidence_result", "impact", "remedy", "report_prose",
            "evidence_artifact",
        )
        for surface in surfaces:
            with self.subTest(surface=surface):
                root = self.make_audit(f"repair-{surface}")
                state = self.base / f"repair-{surface}.jsonl"
                result = self.run_runner(
                    f"repair_mutate_{surface}", root=root, state=state,
                )
                # The mutated repair is rejected and never promoted. The role then spends a
                # fresh attempt rather than dying with budget unused; the fake's fresh sample is
                # valid, so the round completes from that third call, not from the repair.
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                calls = [
                    row for row in self.starts(state)
                    if row["role"] == "auditor_reproduction"
                ]
                self.assertEqual(len(calls), 3, "initial, rejected repair, fresh attempt")
                self.assertIn("byte-frozen scientific projection", result.stderr)
                self.assertIn("focused repair rejected", result.stderr)
                report = root / "report/01_reproduction.md"
                self.assertTrue(report.exists())
                self.assertNotIn("MUTATED", report.read_text())
                ledger = [row for row in self.ledger(root) if row.get("role") == "auditor_reproduction"]
                self.assertEqual([row["status"] for row in ledger], ["failed", "failed", "completed"])

    def test_usage_attribution_covers_partitioned_unpartitioned_and_residual(self) -> None:
        def aggregate(**overrides: int) -> dict:
            counts = {
                "input_tokens": 100,
                "output_tokens": 40,
                "cached_input_tokens": 20,
                "cache_creation_input_tokens": 10,
                "reasoning_tokens": 5,
            }
            counts.update(overrides)
            return {
                "obtainable": True,
                **counts,
                "total_tokens": (
                    counts["input_tokens"] + counts["cached_input_tokens"]
                    + counts["cache_creation_input_tokens"] + counts["output_tokens"]
                ),
                "source": "cli_log",
                "unavailable_reasons": {},
                "total_tokens_kind": "derived",
                "total_tokens_components": [
                    "input_tokens", "cached_input_tokens",
                    "cache_creation_input_tokens", "output_tokens",
                ],
                "total_tokens_unavailable_reason": None,
            }

        complete_result = {"modelUsage": {"sonnet-observed": {
            "inputTokens": 100, "outputTokens": 40, "cacheReadInputTokens": 20,
            "cacheCreationInputTokens": 10, "reasoningTokens": 5,
        }}}
        partitions, unattributed = run_phase1.usage_attribution(
            complete_result, aggregate(),
        )
        self.assertEqual(len(partitions), 1)
        self.assertIsNone(unattributed)

        partitions, unattributed = run_phase1.usage_attribution({}, aggregate())
        self.assertEqual(partitions, [])
        self.assertEqual(unattributed, aggregate())

        partial_result = {"modelUsage": {
            "sonnet-observed": {
                "inputTokens": 80, "outputTokens": 30, "cacheReadInputTokens": 15,
                "cacheCreationInputTokens": 8, "reasoningTokens": 4,
            },
            "haiku-observed": {
                "inputTokens": 20, "cacheReadInputTokens": 5,
                "cacheCreationInputTokens": 2, "reasoningTokens": 1,
            },
        }}
        partitions, unattributed = run_phase1.usage_attribution(
            partial_result, aggregate(),
        )
        self.assertEqual(len(partitions), 2)
        self.assertIsNotNone(unattributed)
        assert unattributed is not None
        self.assertEqual(unattributed["output_tokens"], 10)
        self.assertEqual(unattributed["input_tokens"], 0)
        self.assertEqual(unattributed["total_tokens"], 10)

    def test_failed_new_build_does_not_replace_prior_passing_role_report(self) -> None:
        first = self.run_runner()
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        report = self.root / "report/01_reproduction.md"
        original = report.read_bytes()
        package = self.root / "pkg/data.csv"
        package.write_text(package.read_text() + "aggregate,4,C\n", encoding="utf-8")
        self.write_package_manifest(self.root)
        second = self.run_runner("invalid_evidence")
        self.assertEqual(second.returncode, 2, second.stderr + second.stdout)
        self.assertEqual(report.read_bytes(), original)
        current_input = json.loads((self.root / "phase1/status.json").read_text())["source_snapshot_sha256"]
        self.assertFalse((self.root / f"phase1/completions/auditor_reproduction/{current_input}.json").exists())

    def test_two_audits_can_run_concurrently_without_shared_attempt_state(self) -> None:
        other = self.make_audit("other")
        other_state = self.base / "other-state.jsonl"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.run_runner, "success", root=self.root, state=self.state)
            b = pool.submit(self.run_runner, "success", root=other, state=other_state)
            first, second = a.result(), b.result()
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.assertEqual(len(self.starts(self.state)), 6)
        self.assertEqual(len(self.starts(other_state)), 6)
        self.assertEqual(
            len([row for row in self.ledger(self.root) if row.get("role") in phase1_runtime.ROLE_SPECS]), 5
        )
        self.assertEqual(
            len([row for row in self.ledger(other) if row.get("role") in phase1_runtime.ROLE_SPECS]), 5
        )

    def test_metadata_21_records_reservation_start_terminal_and_cache_replay(self) -> None:
        root = self.make_audit("metadata21", metadata21=True)
        state = self.base / "metadata21-state.jsonl"
        first = self.run_runner(root=root, state=state)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        rows = self.ledger(root)
        providers = [row for row in rows if row.get("provider_call")]
        self.assertEqual(len(providers), 6)
        self.assertTrue(all(row["provider_request_status"] == "accepted" for row in providers))
        self.assertTrue(all(row["inference_start_status"] == "started" for row in providers))
        served = fake_claude_phase1.SERVED_BY_MODEL[auditor_route(root)["spawn_alias"]]
        self.assertTrue(all(row["served_model"] == served for row in providers))
        for role in phase1_runtime.ROLE_SPECS:
            record = next(row for row in providers if row["role"] == role)
            self.assertEqual((record["attempt"], record["attempt_kind"]), (1, "role_inference"))
            self.assertEqual(record["identity_assurance"], "observed")
            self.assertEqual(record["usage_by_model"][0]["identity_assurance"], "observed")
        events = lifecycle_journal.load_events(root)
        event_types = [row["type"] for row in events]
        self.assertEqual(event_types.count("invocation_reserved"), 6)
        self.assertEqual(event_types.count("inference_start"), 6)
        self.assertEqual(event_types.count("invocation_terminal"), 6)
        second = self.run_runner(root=root, state=state)
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        replay_rows = [row for row in self.ledger(root) if row.get("attempt_kind") == "cache_replay"]
        self.assertEqual(len(replay_rows), 6)
        self.assertTrue(all(row["attempt"] is None and not row["provider_call"] for row in replay_rows))
        self.assertTrue(all(row["source_invocation_id"] for row in replay_rows))

    def test_response_receipt_recovers_without_duplicate_provider_call(self) -> None:
        first = self.run_runner()
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        summary = json.loads(first.stdout)
        role = "auditor_reproduction"
        completion = next(row for row in summary["roles"] if row["role"] == role)
        attempt = next((self.root / "phase1/roles" / role / completion["input_sha256"]).glob("attempt_*"))
        status_path = attempt / "status.json"
        record = next(row for row in self.ledger() if row["invocation_id"] == completion["invocation_id"])
        stdout = attempt / "claude_stdout.json"
        stderr = attempt / "claude_stderr.txt"
        status_path.write_text(json.dumps({
            "schema_version": "i4r-phase1-attempt-1",
            "state": "response_received",
            "invocation_id": completion["invocation_id"],
            "attempt_kind": "role_inference",
            "attempt": 1,
            "provider_request_status": "accepted",
            "inference_start_status": "started",
            "returncode": 0,
            "timed_out": False,
            "started_at": record["started_at"],
            "ended_at": record["ended_at"],
            "stdout_sha256": digest(stdout),
            "stderr_sha256": digest(stderr),
        }, indent=2) + "\n", encoding="utf-8")
        # Simulate a process death after durable response receipt but before terminal journaling.
        rows = [row for row in self.ledger() if row["invocation_id"] != completion["invocation_id"]]
        (self.root / "invocations.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
        )
        (self.root / "phase1/completions" / role / f"{completion['input_sha256']}.json").unlink()
        (self.root / completion["report"]).unlink()
        shutil.rmtree((self.root / completion["evidence"]).parent)
        calls_before = len(self.starts())
        second = self.run_runner()
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.assertEqual(len(self.starts()), calls_before)
        recovered = [row for row in self.ledger() if row["role"] == role]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["invocation_id"], completion["invocation_id"])
        self.assertTrue((self.root / completion["report"]).is_file())

    def test_terminal_journal_rebuilds_missing_completion_receipt_without_new_call(self) -> None:
        first = self.run_runner()
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        summary = json.loads(first.stdout)
        role = "auditor_reproduction"
        completion = next(row for row in summary["roles"] if row["role"] == role)
        receipt = self.root / "phase1/completions" / role / f"{completion['input_sha256']}.json"
        receipt.unlink()
        attempt = next((self.root / "phase1/roles" / role / completion["input_sha256"]).glob("attempt_*"))
        status = json.loads((attempt / "status.json").read_text())
        status["state"] = "response_received"
        status["returncode"] = 0
        status["timed_out"] = False
        status["started_at"] = next(
            row["started_at"] for row in self.ledger() if row["invocation_id"] == completion["invocation_id"]
        )
        status["stdout_sha256"] = digest(attempt / "claude_stdout.json")
        status["stderr_sha256"] = digest(attempt / "claude_stderr.txt")
        (attempt / "status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
        calls_before = len(self.starts())
        second = self.run_runner()
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.assertEqual(len(self.starts()), calls_before)
        self.assertTrue(receipt.is_file())

    def test_metadata_21_preflight_pause_and_resume_are_canonical_without_attempt(self) -> None:
        root = self.make_audit("pause21", metadata21=True)
        state = self.base / "pause21-state.jsonl"
        target = root / "pkg/analysis.py"
        target.unlink()
        target.symlink_to(root / "pkg/LICENSE")
        failed = self.run_runner(root=root, state=state)
        self.assertEqual(failed.returncode, 3, failed.stderr + failed.stdout)
        run = json.loads((root / "run.json").read_text())
        self.assertEqual((run["status"], run["pause_reason"]), ("paused", "local_preflight_failed"))
        self.assertEqual(self.ledger(root), [])
        self.assertEqual(self.starts(state), [])
        events = lifecycle_journal.load_events(root)
        self.assertEqual(events[-1]["type"], "pause_start")
        target.unlink()
        target.write_text("if __name__ == '__main__':\n    print('aggregate analysis')\n", encoding="utf-8")
        self.write_package_manifest(root)
        resumed = self.run_runner(root=root, state=state)
        self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
        run = json.loads((root / "run.json").read_text())
        self.assertIsNone(run["pause_reason"])
        event_types = [event["type"] for event in lifecycle_journal.load_events(root)]
        self.assertIn("pause_end", event_types)
        self.assertIn("phase_start", event_types)
        self.assertIn("phase_end", event_types)


if __name__ == "__main__":
    unittest.main()
