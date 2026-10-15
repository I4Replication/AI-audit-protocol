from __future__ import annotations

import json
import hashlib
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/benchmark_optimization.py"
SEEDS = ROOT / "config/benchmark-seeded-fixtures.json"
REGRESSION_REPLAY = ROOT / "tests/fixtures/regression-benchmark-replay.json"


def run_measurement(
    tokens: int,
    seconds: int,
    findings: list[str],
    *,
    cache_read_tokens: int | None = None,
    phase1_turn_counts: list[int] | None = None,
    failed_billable_tokens: int = 0,
    local_preflight_billable_tokens: int = 0,
    schema_admission_billable_tokens: int = 0,
) -> dict:
    cached = tokens if cache_read_tokens is None else cache_read_tokens
    return {
        "total_tokens": tokens,
        "wall_clock_seconds": seconds,
        "reported_finding_ids": findings,
        "telemetry_coverage": {
            "model": 1,
            "reasoning_effort": 1,
            "latency": 1,
            "outcome": 1,
            "tokens": 1,
        },
        "accepted_partial_or_stale_outputs": 0,
        "settled_spot_check_failures": 0,
        "operational_metrics": {
            "phase1_turn_counts": phase1_turn_counts or [40, 40, 40, 40, 40],
            "claude_cache_read_tokens": cached,
            "accounted_tokens": tokens,
            "failed_billable_tokens": failed_billable_tokens,
            "local_preflight_billable_tokens": local_preflight_billable_tokens,
            "schema_admission_billable_tokens": schema_admission_billable_tokens,
        },
    }


def record(case_id: str, baseline: dict, optimized: dict) -> dict:
    return {
        "case_id": case_id,
        "source": "real",
        "measurement_status": "measured",
        "failure_domains": ["statistics"],
        "ground_truth": {
            "substantive_finding_ids": [f"{case_id}-A", f"{case_id}-B"],
            "conclusion_changing_ids": [f"{case_id}-A"],
            "disclosure_critical_ids": [],
        },
        "baseline": baseline,
        "optimized": optimized,
    }


class BenchmarkOptimizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="i4r-benchmark-test.")
        self.root = pathlib.Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def canonical_sha256(value: object) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def file_sha256(path: pathlib.Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def bind_measurement(self, case: dict, variant: str) -> None:
        run = case[variant]
        directory = self.root / ".measurements" / case["case_id"] / variant
        directory.mkdir(parents=True, exist_ok=True)
        event = {
            "schema_version": "2.1.0", "seq": 1,
            "at": "2026-08-30T10:00:00Z", "type": "init",
            "payload": {"case_id": case["case_id"], "variant": variant},
            "pid": None, "source": "benchmark-test", "previous_hash": None,
        }
        event["event_hash"] = self.canonical_sha256(event)
        journal = directory / "run.events.jsonl"
        journal.write_text(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        watermark = {"journal_seq": 1, "journal_hash": event["event_hash"]}
        metrics = directory / "run_metrics.json"
        metrics.write_text(json.dumps({
            "schema_version": "2.1.0", "journal_watermark": watermark,
            "fixture": {"case_id": case["case_id"], "variant": variant},
        }, sort_keys=True) + "\n")
        artifact = directory / "accepted-findings.json"
        artifact.write_text(json.dumps(run["reported_finding_ids"], sort_keys=True) + "\n")
        measurement = {key: value for key, value in run.items()
                       if key != "measurement_manifest"}
        manifest = directory / "measurement_manifest.json"
        manifest.write_text(json.dumps({
            "schema_version": "i4r-benchmark-measurement-manifest-1",
            "case_id": case["case_id"], "variant": variant,
            "measurement_sha256": self.canonical_sha256(measurement),
            "journal": {
                "path": "run.events.jsonl", "sha256": self.file_sha256(journal),
                **watermark,
            },
            "run_metrics": {
                "path": "run_metrics.json", "sha256": self.file_sha256(metrics),
            },
            "artifacts": [{
                "kind": "accepted_findings", "path": "accepted-findings.json",
                "sha256": self.file_sha256(artifact),
            }],
        }, indent=2, sort_keys=True) + "\n")
        run["measurement_manifest"] = {
            "path": manifest.relative_to(self.root).as_posix(),
            "sha256": self.file_sha256(manifest),
        }

    def write_input(self, records: list[dict], *, bind: bool = True) -> pathlib.Path:
        records = json.loads(json.dumps(records))
        if bind:
            for case in records:
                if case.get("measurement_status") == "measured":
                    self.bind_measurement(case, "baseline")
                    self.bind_measurement(case, "optimized")
        path = self.root / "input.json"
        path.write_text(json.dumps({"schema_version": "i4r-benchmark-input-1", "records": records}))
        return path

    def invoke(self, input_path: pathlib.Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(SCRIPT), str(input_path), *extra], text=True, capture_output=True
        )

    def test_computes_median_reductions_and_accuracy_noninferiority(self) -> None:
        first = record(
            "case-1",
            run_measurement(
                1000, 1000, ["case-1-A", "case-1-B", "case-1-FP"],
                cache_read_tokens=800,
            ),
            run_measurement(
                600, 600, ["case-1-A", "case-1-B"], cache_read_tokens=300,
                failed_billable_tokens=50,
            ),
        )
        second = record(
            "case-2",
            run_measurement(1200, 500, ["case-2-A", "case-2-B"], cache_read_tokens=1000),
            run_measurement(
                600, 300, ["case-2-A", "case-2-B"], cache_read_tokens=400,
                failed_billable_tokens=40,
            ),
        )
        output = self.root / "result.json"
        result = self.invoke(
            self.write_input([first, second]), "--output-json", str(output), "--require-gates"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(output.read_text())
        self.assertAlmostEqual(summary["performance"]["median_paired_token_reduction_fraction"], 0.45)
        self.assertAlmostEqual(summary["performance"]["median_paired_wall_clock_reduction_fraction"], 0.4)
        self.assertEqual(summary["performance"]["median_phase1_turn_count"], 40)
        self.assertAlmostEqual(
            summary["performance"]["claude_cache_read_token_reduction_fraction"],
            1 - 700 / 1800,
        )
        self.assertAlmostEqual(
            summary["optimized_safety"]["failed_billable_token_fraction"], 90 / 1200
        )
        self.assertEqual(summary["release_gates"]["status"], "pass")
        self.assertGreaterEqual(
            summary["noninferiority"]["optimized_precision"],
            summary["noninferiority"]["baseline_precision"],
        )
        provenance = summary["per_case"][0]["measurement_provenance"]
        self.assertRegex(provenance["baseline"]["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(provenance["optimized"]["journal_watermark"]["journal_seq"], 1)

    def test_accuracy_and_efficiency_regressions_fail_release_gate(self) -> None:
        bad = record(
            "case-bad",
            run_measurement(1000, 1000, ["case-bad-A", "case-bad-B"]),
            run_measurement(900, 900, ["case-bad-B", "case-bad-FP"]),
        )
        bad["optimized"]["telemetry_coverage"]["tokens"] = 0.5
        bad["optimized"]["accepted_partial_or_stale_outputs"] = 1
        output = self.root / "bad.json"
        result = self.invoke(
            self.write_input([bad]), "--output-json", str(output), "--require-gates"
        )
        self.assertEqual(result.returncode, 1)
        summary = json.loads(output.read_text())
        self.assertEqual(summary["release_gates"]["status"], "fail")
        checks = summary["release_gates"]["checks"]
        self.assertFalse(checks["zero_missed_conclusion_or_disclosure_critical"])
        self.assertFalse(checks["substantive_recall_within_two_percentage_points"])
        self.assertFalse(checks["precision_no_worse"])
        self.assertFalse(checks["telemetry_coverage_100_percent"])
        self.assertFalse(checks["median_token_reduction_at_least_30_percent"])

    def test_operational_gate_boundaries_pass_exactly(self) -> None:
        boundary = record(
            "case-boundary",
            run_measurement(
                1000, 1000, ["case-boundary-A", "case-boundary-B"],
                cache_read_tokens=1000,
            ),
            run_measurement(
                500, 650, ["case-boundary-A", "case-boundary-B"],
                cache_read_tokens=500,
                phase1_turn_counts=[45, 45, 45, 45, 45],
                failed_billable_tokens=50,
            ),
        )
        output = self.root / "boundary.json"
        result = self.invoke(
            self.write_input([boundary]), "--output-json", str(output), "--require-gates"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(output.read_text())
        checks = summary["release_gates"]["checks"]
        self.assertTrue(checks["median_phase1_turn_count_at_most_45"])
        self.assertTrue(checks["claude_cache_read_token_reduction_at_least_50_percent"])
        self.assertTrue(checks["failed_billable_token_fraction_at_most_10_percent"])
        self.assertTrue(checks["zero_preflight_and_schema_admission_billable_tokens"])

    def test_each_operational_regression_fails_its_release_gate(self) -> None:
        bad = record(
            "case-operational-bad",
            run_measurement(
                1000, 1000, ["case-operational-bad-A", "case-operational-bad-B"],
                cache_read_tokens=800,
            ),
            run_measurement(
                600, 600, ["case-operational-bad-A", "case-operational-bad-B"],
                cache_read_tokens=500,
                phase1_turn_counts=[46, 46, 46, 46, 46],
                failed_billable_tokens=61,
                local_preflight_billable_tokens=1,
                schema_admission_billable_tokens=2,
            ),
        )
        output = self.root / "operational-bad.json"
        result = self.invoke(
            self.write_input([bad]), "--output-json", str(output), "--require-gates"
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        summary = json.loads(output.read_text())
        checks = summary["release_gates"]["checks"]
        self.assertFalse(checks["median_phase1_turn_count_at_most_45"])
        self.assertFalse(checks["claude_cache_read_token_reduction_at_least_50_percent"])
        self.assertFalse(checks["failed_billable_token_fraction_at_most_10_percent"])
        self.assertFalse(checks["zero_preflight_and_schema_admission_billable_tokens"])
        self.assertEqual(summary["optimized_safety"]["local_preflight_billable_tokens"], 1)
        self.assertEqual(summary["optimized_safety"]["schema_admission_billable_tokens"], 2)

    def test_operational_gates_fail_independently(self) -> None:
        passing = record(
            "case-isolated",
            run_measurement(
                1000, 1000, ["case-isolated-A", "case-isolated-B"],
                cache_read_tokens=1000,
            ),
            run_measurement(
                600, 650, ["case-isolated-A", "case-isolated-B"],
                cache_read_tokens=500,
                phase1_turn_counts=[45, 45, 45, 45, 45],
                failed_billable_tokens=60,
            ),
        )
        cases = (
            (
                "phase1_turn_counts", [46, 46, 46, 46, 46],
                "median_phase1_turn_count_at_most_45",
            ),
            (
                "claude_cache_read_tokens", 501,
                "claude_cache_read_token_reduction_at_least_50_percent",
            ),
            (
                "failed_billable_tokens", 61,
                "failed_billable_token_fraction_at_most_10_percent",
            ),
            (
                "local_preflight_billable_tokens", 1,
                "zero_preflight_and_schema_admission_billable_tokens",
            ),
            (
                "schema_admission_billable_tokens", 1,
                "zero_preflight_and_schema_admission_billable_tokens",
            ),
        )
        new_gate_names = {
            "median_phase1_turn_count_at_most_45",
            "claude_cache_read_token_reduction_at_least_50_percent",
            "failed_billable_token_fraction_at_most_10_percent",
            "zero_preflight_and_schema_admission_billable_tokens",
        }
        for field, value, failed_gate in cases:
            with self.subTest(field=field):
                candidate = json.loads(json.dumps(passing))
                candidate["optimized"]["operational_metrics"][field] = value
                output = self.root / f"isolated-{field}.json"
                result = self.invoke(
                    self.write_input([candidate]), "--output-json", str(output), "--require-gates"
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                checks = json.loads(output.read_text())["release_gates"]["checks"]
                self.assertFalse(checks[failed_gate])
                self.assertTrue(all(
                    checks[name] for name in new_gate_names if name != failed_gate
                ))

    def test_missing_operational_measurements_are_compatible_but_indeterminate(self) -> None:
        legacy = record(
            "case-legacy",
            run_measurement(1000, 1000, ["case-legacy-A", "case-legacy-B"]),
            run_measurement(500, 500, ["case-legacy-A", "case-legacy-B"]),
        )
        legacy["baseline"].pop("operational_metrics")
        legacy["optimized"].pop("operational_metrics")
        output = self.root / "legacy.json"
        result = self.invoke(
            self.write_input([legacy]), "--output-json", str(output),
            "--require-complete", "--require-gates",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        summary = json.loads(output.read_text())
        self.assertEqual(summary["release_gates"]["status"], "indeterminate")
        self.assertEqual(
            summary["selection"]["operationally_incomplete_case_ids"], ["case-legacy"]
        )

    def test_malformed_operational_measurements_are_rejected(self) -> None:
        invalid = record(
            "case-invalid",
            run_measurement(1000, 1000, ["case-invalid-A", "case-invalid-B"]),
            run_measurement(500, 500, ["case-invalid-A", "case-invalid-B"]),
        )
        mutations = (
            ("phase1_turn_counts", [1, 2, 3, 4]),
            ("failed_billable_tokens", 501),
            ("local_preflight_billable_tokens", -1),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                candidate = json.loads(json.dumps(invalid))
                candidate["optimized"]["operational_metrics"][field] = value
                result = self.invoke(self.write_input([candidate]))
                self.assertEqual(result.returncode, 2)
                self.assertIn("FATAL:", result.stderr)

    def test_regression_replay_fixture_is_projected_and_cannot_claim_speedup(self) -> None:
        output = self.root / "regression.json"
        result = self.invoke(str(REGRESSION_REPLAY), "--output-json", str(output), "--require-gates")
        self.assertEqual(result.returncode, 1, result.stderr)
        summary = json.loads(output.read_text())
        self.assertEqual(summary["selection"]["measured_case_ids"], [])
        self.assertEqual(summary["selection"]["projected_case_ids"], ["regression-principal-replay"])
        self.assertIsNone(summary["performance"]["median_paired_token_reduction_fraction"])
        self.assertIsNone(summary["performance"]["median_paired_wall_clock_reduction_fraction"])
        self.assertIsNone(summary["noninferiority"]["optimized_recall"])
        self.assertEqual(summary["release_gates"]["status"], "indeterminate")

    def test_measured_case_without_hash_bound_manifests_is_rejected(self) -> None:
        case = record(
            "case-unbound",
            run_measurement(1000, 1000, ["case-unbound-A", "case-unbound-B"]),
            run_measurement(500, 500, ["case-unbound-A", "case-unbound-B"]),
        )
        result = self.invoke(self.write_input([case], bind=False))
        self.assertEqual(result.returncode, 2)
        self.assertIn("lacks a hash-bound measurement_manifest", result.stderr)

    def test_hash_bound_measured_pair_is_admitted(self) -> None:
        case = record(
            "case-bound-provenance",
            run_measurement(
                1000, 1000,
                ["case-bound-provenance-A", "case-bound-provenance-B"],
                cache_read_tokens=1000,
            ),
            run_measurement(
                500, 650,
                ["case-bound-provenance-A", "case-bound-provenance-B"],
                cache_read_tokens=500, failed_billable_tokens=50,
            ),
        )
        output = self.root / "bound-result.json"
        result = self.invoke(
            self.write_input([case]), "--output-json", str(output), "--require-gates",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(output.read_text())
        self.assertEqual(summary["selection"]["measured_case_ids"], ["case-bound-provenance"])
        self.assertEqual(summary["release_gates"]["status"], "pass")

    def test_forged_measurement_and_stale_journal_provenance_are_rejected(self) -> None:
        case = record(
            "case-provenance",
            run_measurement(1000, 1000, ["case-provenance-A", "case-provenance-B"]),
            run_measurement(500, 500, ["case-provenance-A", "case-provenance-B"]),
        )
        input_path = self.write_input([case])
        payload = json.loads(input_path.read_text())
        payload["records"][0]["optimized"]["total_tokens"] = 499
        input_path.write_text(json.dumps(payload))
        forged = self.invoke(input_path)
        self.assertEqual(forged.returncode, 2)
        self.assertIn("numeric measurement is stale or forged", forged.stderr)

        input_path = self.write_input([case])
        payload = json.loads(input_path.read_text())
        manifest_ref = payload["records"][0]["baseline"]["measurement_manifest"]["path"]
        manifest = json.loads((self.root / manifest_ref).read_text())
        journal = (self.root / manifest_ref).parent / manifest["journal"]["path"]
        journal.write_text(journal.read_text().replace('"seq":1', '"seq":2'))
        stale = self.invoke(input_path)
        self.assertEqual(stale.returncode, 2)
        self.assertIn("SHA-256 is stale or forged", stale.stderr)

    def test_unmeasured_seeded_cases_are_indeterminate_and_real_cases_are_capped(self) -> None:
        records = []
        for index in range(9):
            case_id = f"real-{index}"
            records.append(record(
                case_id,
                run_measurement(1000, 1000, [f"{case_id}-A", f"{case_id}-B"]),
                run_measurement(600, 600, [f"{case_id}-A", f"{case_id}-B"]),
            ))
        output = self.root / "incomplete.json"
        result = self.invoke(
            self.write_input(records), "--seeded-fixtures", str(SEEDS),
            "--output-json", str(output), "--require-complete",
        )
        self.assertEqual(result.returncode, 1)
        summary = json.loads(output.read_text())
        self.assertEqual(summary["selection"]["retained_real_records"], 8)
        self.assertEqual(summary["selection"]["omitted_real_case_ids"], ["real-8"])
        self.assertEqual(summary["selection"]["retained_seeded_records"], 5)
        self.assertEqual(len(summary["selection"]["incomplete_case_ids"]), 5)
        self.assertEqual(summary["release_gates"]["status"], "indeterminate")


if __name__ == "__main__":
    unittest.main()
