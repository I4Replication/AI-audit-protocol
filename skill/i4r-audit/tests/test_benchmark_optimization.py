from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/benchmark_optimization.py"
SEEDS = ROOT / "config/benchmark-seeded-fixtures.json"


def run_measurement(tokens: int, seconds: int, findings: list[str]) -> dict:
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
    }


def record(case_id: str, baseline: dict, optimized: dict) -> dict:
    return {
        "case_id": case_id,
        "source": "real",
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

    def write_input(self, records: list[dict]) -> pathlib.Path:
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
            run_measurement(1000, 1000, ["case-1-A", "case-1-B", "case-1-FP"]),
            run_measurement(600, 600, ["case-1-A", "case-1-B"]),
        )
        second = record(
            "case-2",
            run_measurement(1200, 500, ["case-2-A", "case-2-B"]),
            run_measurement(600, 300, ["case-2-A", "case-2-B"]),
        )
        output = self.root / "result.json"
        result = self.invoke(
            self.write_input([first, second]), "--output-json", str(output), "--require-gates"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(output.read_text())
        self.assertAlmostEqual(summary["performance"]["median_paired_token_reduction_fraction"], 0.45)
        self.assertAlmostEqual(summary["performance"]["median_paired_wall_clock_reduction_fraction"], 0.4)
        self.assertEqual(summary["release_gates"]["status"], "pass")
        self.assertGreaterEqual(
            summary["noninferiority"]["optimized_precision"],
            summary["noninferiority"]["baseline_precision"],
        )

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
