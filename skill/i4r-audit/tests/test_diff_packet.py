from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DIFF = ROOT / "scripts/make_diff_packet.py"
VERIFY_DIFF = ROOT / "scripts/verify_diff_packet.py"
SETTLED = ROOT / "scripts/make_settled.sh"
FIXTURE = json.loads((ROOT / "schemas/fixtures/v2_finding_row.json").read_text())


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DiffPacketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="i4r-diff-test.")
        self.audit = pathlib.Path(self.temp.name) / "audit"
        for relative in ("report", "work", "paper", "critique"):
            (self.audit / relative).mkdir(parents=True, exist_ok=True)
        (self.audit / "work/check_primary.txt").write_text("9\n")
        (self.audit / "work/check_primary.py").write_text("print(9)\n")
        (self.audit / "paper/paper.txt").write_text("paper\n")
        (self.audit / "report/REPORT_final.md").write_text("# Abstract\nStable.\n\n## 1. Findings\nAfter.\n")
        (self.audit / "work/report_before.md").write_text("# Abstract\nStable.\n\n## 1. Findings\nAfter.\n")
        (self.audit / "work/reviewer-a.json").write_text('{"verdict":"confirmed"}\n')
        (self.audit / "work/reviewer-b.json").write_text('{"verdict":"confirmed independently"}\n')
        self.confirmations = [
            {
                "reviewer_id": "auditor_reproduction", "invocation_id": "diff-inv-a",
                "evidence_record": {"path": "work/reviewer-a.json",
                                    "sha256": digest(self.audit / "work/reviewer-a.json")},
                "model_family": "claude", "outcome": "confirmed",
            },
            {
                "reviewer_id": "critic_stats", "invocation_id": "diff-inv-b",
                "evidence_record": {"path": "work/reviewer-b.json",
                                    "sha256": digest(self.audit / "work/reviewer-b.json")},
                "model_family": "gpt", "outcome": "confirmed",
            },
        ]
        invocation_specs = [
            ("diff-inv-a", "auditor_reproduction", "auditor", 0, "anthropic", "claude",
             "work/reviewer-a.json"),
            ("diff-inv-b", "critic_stats", "critic", 1, "openai", "gpt",
             "work/reviewer-b.json"),
        ]
        invocations = []
        for index, (invocation_id, role, role_class, round_number, provider, family,
                    output_path) in enumerate(invocation_specs, 1):
            invocations.append({
                "invocation_id": invocation_id, "role": role, "role_class": role_class,
                "round": round_number, "attempt": 1, "status": "completed",
                "provider": provider, "provider_call": True, "cache_usage": "none",
                "session_id": f"diff-session-{index}", "requested_model": "model-alias",
                "observed_model": f"served-{family}-{index}", "model_family": family,
                "model_source": "observed",
                "reasoning_effort": "high" if role_class == "critic" else "max",
                "started_at": "2026-08-27T10:00:00Z", "ended_at": "2026-08-27T10:01:00Z",
                "elapsed_seconds": 60, "input_artifact_sha256": "a" * 64,
                "prompt_sha256": "b" * 64, "output_artifact": output_path,
                "output_sha256": digest(self.audit / output_path),
                "usage": {"obtainable": True, "input_tokens": 10, "output_tokens": 5,
                          "cached_input_tokens": 0, "reasoning_tokens": 1,
                          "total_tokens": 16, "source": "api_response"},
                "error": None, "error_class": None, "legacy_source": None,
            })
        (self.audit / "invocations.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in invocations))
        (self.audit / "run.json").write_text(json.dumps({
            "audit_id": "diff-test",
            "rounds": [{"round": 1, "started_at": "2026-08-27T10:00:00Z",
                        "ended_at": "2026-08-27T11:00:00Z",
                        "integrated_at": "2026-08-27T12:00:00Z",
                        "consistency_check_run": True, "consistency_check_exit": 0}],
        }) + "\n")
        status = self.audit / "critique/round_1/ROUND_STATUS.json"
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(json.dumps({
            "audit_id": "diff-test", "round": 1, "outcome": "complete", "exit_code": 0,
            "quorum_met": True, "expected_roles": ["stats", "data", "fairness", "completeness"],
            "completed_roles": ["stats", "data", "fairness", "completeness"],
            "failed_roles": [],
        }) + "\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def row(self, finding_id: str, domain: str, *, settled: bool = False) -> dict:
        row = copy.deepcopy(FIXTURE)
        row["id"] = finding_id
        row["domain"] = domain
        row["claim"] = f"{finding_id} original claim"
        row["evidence"]["source_hashes"][0]["sha256"] = digest(
            self.audit / "work/check_primary.txt"
        )
        row["verifications"] = copy.deepcopy(self.confirmations)
        row["dependencies"]["artifact_paths"] = ["work/check_primary.txt", "work/check_primary.py"]
        row["settled"] = ({
            "approved": True,
            "valid_for_report_hash": digest(self.audit / "report/REPORT_final.md"),
        } if settled else None)
        row["changes_conclusion"] = False
        row["changes_conclusion_which"] = None
        row["disclosure_flag"] = False
        return row

    def write_rows(self, relative: str, rows: list[dict]) -> pathlib.Path:
        path = self.audit / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
        return path

    def run_diff(self, before: pathlib.Path, output: str = "packet.json") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3", str(DIFF), str(self.audit), "--round", "2",
                "--before-report", str(self.audit / "work/report_before.md"),
                "--after-report", str(self.audit / "report/REPORT_final.md"),
                "--before-findings", str(before),
                "--after-findings", str(self.audit / "work/findings.jsonl"),
                "--output", str(self.audit / "critique" / output),
            ],
            text=True,
            capture_output=True,
        )

    def test_routes_one_specialist_except_multi_domain_critical_changes(self) -> None:
        before_rows = [self.row("F1", "data"), self.row("F2", "statistics")]
        after_rows = copy.deepcopy(before_rows)
        after_rows[0]["claim"] = "Participant records changed"
        after_rows[1]["claim"] = "Statistical estimate changed"
        before = self.write_rows("work/findings.before.jsonl", before_rows)
        self.write_rows("work/findings.jsonl", after_rows)
        first = self.run_diff(before, "ordinary.json")
        self.assertEqual(first.returncode, 0, first.stderr)
        ordinary = json.loads((self.audit / "critique/ordinary.json").read_text())
        self.assertEqual(len(ordinary["recommended_roles"]), 1)
        self.assertEqual(ordinary["routing_mode"], "single_best_specialist")
        self.assertEqual(
            ordinary["required_global_gates"],
            ["consistency", "privacy", "checklist", "archive"],
        )
        verified = subprocess.run(
            ["python3", str(VERIFY_DIFF), str(self.audit),
             str(self.audit / "critique/ordinary.json"), "--round", "2"],
            text=True, capture_output=True,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        tampered = copy.deepcopy(ordinary)
        tampered["recommended_roles"] = ["fairness"]
        (self.audit / "critique/tampered.json").write_text(
            json.dumps(tampered, indent=2, sort_keys=True) + "\n"
        )
        rejected = subprocess.run(
            ["python3", str(VERIFY_DIFF), str(self.audit),
             str(self.audit / "critique/tampered.json"), "--round", "2"],
            text=True, capture_output=True,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("not the exact deterministic projection", rejected.stderr)

        for row in after_rows:
            row["changes_conclusion"] = True
            row["changes_conclusion_which"] = "Primary conclusion"
            row["claim"] += " critically"
        self.write_rows("work/findings.jsonl", after_rows)
        second = self.run_diff(before, "critical.json")
        self.assertEqual(second.returncode, 0, second.stderr)
        critical = json.loads((self.audit / "critique/critical.json").read_text())
        self.assertEqual(critical["recommended_roles"], ["stats", "data"])
        self.assertEqual(critical["critical_domains"], ["data", "statistics"])
        self.assertEqual(critical["routing_mode"], "multi_domain_critical")

        # The packet has no clock field and is byte-stable for identical content-bound inputs.
        third = self.run_diff(before, "critical-copy.json")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(
            (self.audit / "critique/critical.json").read_bytes(),
            (self.audit / "critique/critical-copy.json").read_bytes(),
        )

    def test_byte_identical_inputs_emit_content_bound_no_change_packet(self) -> None:
        rows = [self.row("F1", "data")]
        before = self.write_rows("work/findings.before.jsonl", rows)
        self.write_rows("work/findings.jsonl", copy.deepcopy(rows))
        result = self.run_diff(before, "no-change.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        packet = json.loads((self.audit / "critique/no-change.json").read_text())
        self.assertEqual(packet["change_state"], "no_changes")
        self.assertEqual(packet["changed_claim_ids"], [])
        self.assertEqual(packet["changed_sections"], [])
        self.assertEqual(packet["recommended_roles"], ["fairness"])
        self.assertEqual(packet["routing_mode"], "single_best_specialist")
        self.assertEqual(packet["inputs"]["before_report_sha256"],
                         packet["inputs"]["after_report_sha256"])
        self.assertEqual(packet["inputs"]["before_findings_sha256"],
                         packet["inputs"]["after_findings_sha256"])

    def test_settled_cache_is_bound_and_only_dependency_references_are_included(self) -> None:
        settled = self.row("F1", "statistics", settled=True)
        unreferenced = self.row("F9", "data", settled=True)
        changed_before = self.row("F2", "data")
        changed_after = copy.deepcopy(changed_before)
        changed_after["claim"] = "Participant count corrected"
        changed_after["dependencies"]["finding_ids"] = ["F1"]
        before = self.write_rows(
            "work/findings.before.jsonl", [settled, changed_before, unreferenced]
        )
        self.write_rows("work/findings.jsonl", [settled, changed_after, unreferenced])
        made = subprocess.run(
            ["bash", str(SETTLED), str(self.audit)], text=True, capture_output=True
        )
        self.assertEqual(made.returncode, 0, made.stderr)
        result = self.run_diff(before)
        self.assertEqual(result.returncode, 0, result.stderr)
        packet = json.loads((self.audit / "critique/packet.json").read_text())
        self.assertEqual([row["fact_id"] for row in packet["settled_fact_references"]], ["F1"])
        self.assertEqual(
            packet["settled_fact_references"][0]["valid_for_report_hash"],
            digest(self.audit / "report/REPORT_final.md"),
        )
        self.assertEqual(len(packet["settled_cache_sha256"]), 64)

        cache = json.loads((self.audit / "SETTLED.json").read_text())
        cache["facts"][0]["coordinator_approved"] = False
        (self.audit / "SETTLED.json").write_text(json.dumps(cache))
        rejected = self.run_diff(before, "rejected.json")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("lacks coordinator approval", rejected.stderr)

        # Reviewer labels cannot be detached from the canonical invocation/output binding.
        remade = subprocess.run(
            ["bash", str(SETTLED), str(self.audit)], text=True, capture_output=True
        )
        self.assertEqual(remade.returncode, 0, remade.stderr)
        cache = json.loads((self.audit / "SETTLED.json").read_text())
        cache["facts"][0]["confirmations"][0]["invocation_id"] = "diff-inv-b"
        (self.audit / "SETTLED.json").write_text(json.dumps(cache))
        false_independence = self.run_diff(before, "false-independence.json")
        self.assertNotEqual(false_independence.returncode, 0)
        self.assertIn("confirmation does not match canonical invocation", false_independence.stderr)

        # Appending later-round telemetry is valid; editing the bound Round-1 prefix is not.
        remade = subprocess.run(
            ["bash", str(SETTLED), str(self.audit)], text=True, capture_output=True
        )
        self.assertEqual(remade.returncode, 0, remade.stderr)
        ledger = self.audit / "invocations.jsonl"
        ledger_rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
        later = copy.deepcopy(ledger_rows[-1])
        later.update({"invocation_id": "diff-inv-later", "role": "critic_data", "round": 2,
                      "session_id": "diff-session-later"})
        ledger.write_text(ledger.read_text() + json.dumps(later, sort_keys=True) + "\n")
        run = json.loads((self.audit / "run.json").read_text())
        run["rounds"].append({"round": 2, "started_at": "2026-08-27T13:00:00Z",
                              "ended_at": "2026-08-27T13:10:00Z",
                              "integrated_at": "2026-08-27T13:20:00Z",
                              "consistency_check_run": True, "consistency_check_exit": 0})
        (self.audit / "run.json").write_text(json.dumps(run, sort_keys=True) + "\n")
        appended = self.run_diff(before, "appended-ledger.json")
        self.assertEqual(appended.returncode, 0, appended.stderr)

        ledger_rows[0]["session_id"] = "diff-session-X"
        ledger.write_text("".join(json.dumps(row, sort_keys=True) + "\n"
                                  for row in [*ledger_rows, later]))
        stale_ledger = self.run_diff(before, "stale-ledger.json")
        self.assertNotEqual(stale_ledger.returncode, 0)
        self.assertIn("invocation ledger bound prefix is stale", stale_ledger.stderr)

    def test_metadata_2_1_requested_only_confirmation_flows_from_settled_to_diff(self) -> None:
        run_path = self.audit / "run.json"
        run = json.loads(run_path.read_text())
        run["schema_version"] = "2.1.0"
        run_path.write_text(json.dumps(run, sort_keys=True) + "\n")

        ledger_path = self.audit / "invocations.jsonl"
        ledger = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
        terra = next(row for row in ledger if row["invocation_id"] == "diff-inv-b")
        terra.update({
            "requested_model": "gpt-5.6-terra",
            "observed_model": None,
            "served_model": None,
            "model_source": "declared",
            "identity_assurance": "requested_only",
        })
        ledger_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in ledger)
        )

        settled = self.row("F1", "statistics", settled=True)
        changed_before = self.row("F2", "data")
        changed_after = copy.deepcopy(changed_before)
        changed_after["claim"] = "Participant count corrected"
        changed_after["dependencies"]["finding_ids"] = ["F1"]
        before = self.write_rows("work/findings.before.jsonl", [settled, changed_before])
        self.write_rows("work/findings.jsonl", [settled, changed_after])

        made = subprocess.run(
            ["bash", str(SETTLED), str(self.audit)], text=True, capture_output=True
        )
        self.assertEqual(made.returncode, 0, made.stderr)
        result = self.run_diff(before, "requested-only.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        packet = json.loads((self.audit / "critique/requested-only.json").read_text())
        self.assertEqual(
            [row["fact_id"] for row in packet["settled_fact_references"]], ["F1"]
        )

        # The same invocation is inadmissible for historical metadata, whose contract required
        # an observed served-model identity.
        run["schema_version"] = "2.0.0"
        run_path.write_text(json.dumps(run, sort_keys=True) + "\n")
        legacy = self.run_diff(before, "legacy-requested-only.json")
        self.assertNotEqual(legacy.returncode, 0)
        self.assertIn("confirmation does not match canonical invocation", legacy.stderr)

    def test_stale_registry_report_and_source_hashes_fail_closed(self) -> None:
        settled = self.row("F1", "statistics", settled=True)
        changed_before = self.row("F2", "data")
        changed_after = copy.deepcopy(changed_before)
        changed_after["claim"] = "Participant count corrected"
        changed_after["dependencies"]["finding_ids"] = ["F1"]
        before = self.write_rows("work/findings.before.jsonl", [settled, changed_before])
        self.write_rows("work/findings.jsonl", [settled, changed_after])

        def remake() -> None:
            result = subprocess.run(["bash", str(SETTLED), str(self.audit)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

        remake()
        current = (self.audit / "work/findings.jsonl").read_text()
        (self.audit / "work/findings.jsonl").write_text(current.replace("count corrected", "count revised"))
        stale_registry = self.run_diff(before, "stale-registry.json")
        self.assertNotEqual(stale_registry.returncode, 0)
        self.assertIn("findings-registry hash", stale_registry.stderr)

        self.write_rows("work/findings.jsonl", [settled, changed_after])
        remake()
        (self.audit / "report/REPORT_final.md").write_text("# Abstract\nChanged report.\n")
        stale_report = self.run_diff(before, "stale-report.json")
        self.assertNotEqual(stale_report.returncode, 0)
        self.assertIn("report hash", stale_report.stderr)

        # Restore the report and row-scoped hash, then invalidate the underlying evidence.
        (self.audit / "report/REPORT_final.md").write_text("# Abstract\nStable.\n\n## 1. Findings\nAfter.\n")
        settled["settled"]["valid_for_report_hash"] = digest(self.audit / "report/REPORT_final.md")
        self.write_rows("work/findings.jsonl", [settled, changed_after])
        remake()
        (self.audit / "work/check_primary.txt").write_text("tampered\n")
        stale_source = self.run_diff(before, "stale-source.json")
        self.assertNotEqual(stale_source.returncode, 0)
        self.assertIn("evidence hash is stale", stale_source.stderr)


if __name__ == "__main__":
    unittest.main()
