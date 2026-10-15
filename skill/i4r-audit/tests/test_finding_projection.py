from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts/generate_finding_artifacts.py"
FIXTURE = ROOT / "schemas/fixtures/v2_finding_row.json"


class FindingProjectionIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-findings-")
        self.audit = pathlib.Path(self.temporary.name) / "audit"
        for relative in ("work", "report", "pkg", "paper"):
            (self.audit / relative).mkdir(parents=True, exist_ok=True)
        source = self.audit / "work/check_primary.txt"
        source.write_text("observed=9\n")
        script = self.audit / "work/check_primary.R"
        script.write_text("observed <- 9\nexpect('C-1', value=9)\ncheck('C-1', value=observed)\n")
        self.row = json.loads(FIXTURE.read_text())
        self.row["evidence"]["source_hashes"] = [{
            "path": "work/check_primary.txt",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }]
        self.row["report_section"] = "Section 1"
        self.row["settled"] = None
        (self.audit / "work/findings.jsonl").write_text(json.dumps(self.row) + "\n")
        (self.audit / "report/REPORT_final.md").write_text(
            "# Independent audit\n\n## 1. Findings\n\nThe rerun differs from the printed value.\n")
        (self.audit / "comment.json").write_text(json.dumps({"findings": []}) + "\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_generator(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(GENERATOR), str(self.audit), *args],
            text=True, capture_output=True, timeout=30,
        )

    def test_apply_refuses_when_no_row_is_approved(self) -> None:
        # Before critique every row is unapproved. Applying would have replaced the drafted Appendix A
        # with an empty table and stripped every claim tag (first production audit, round 1).
        unapproved = copy.deepcopy(self.row)
        unapproved["coordinator_approved"] = False
        unapproved["status"] = "draft"
        unapproved["settled"] = None
        (self.audit / "work/findings.jsonl").write_text(json.dumps(unapproved) + "\n")
        before = (self.audit / "report/REPORT_final.md").read_text()
        applied = self.run_generator("--apply")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("refused", applied.stdout + applied.stderr)
        self.assertEqual((self.audit / "report/REPORT_final.md").read_text(), before)
        self.assertFalse((self.audit / "report/APPENDIX_A.md").exists())
        preview = self.run_generator()
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        self.assertIn("generated 0 findings", preview.stdout)

    def test_atomic_apply_and_exact_check(self) -> None:
        applied = self.run_generator("--apply")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        checked = self.run_generator("--check")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        report = (self.audit / "report/REPORT_final.md").read_text()
        self.assertEqual(report.count("[claim 1]"), 1)
        self.assertIn("## Appendix A", report)
        self.assertTrue((self.audit / "verification.Rmd").is_file())
        self.assertEqual(len(list(self.audit.glob("claims_script.*"))), 1)
        plan = json.loads((self.audit / "work/verification_plan.json").read_text())
        self.assertEqual(
            [row["invocation_id"] for row in plan[0]["verifications"]],
            ["inv-auditor-reproduction-1", "inv-critic-stats-r1-1"],
        )

        comment = json.loads((self.audit / "comment.json").read_text())
        comment["findings"][0]["claim"] = "manually divergent"
        (self.audit / "comment.json").write_text(json.dumps(comment) + "\n")
        divergent = self.run_generator("--check")
        self.assertNotEqual(divergent.returncode, 0)

    def test_notebook_claim_or_snippet_swap_fails_even_if_manifest_hash_is_rekeyed(self) -> None:
        self.assertEqual(self.run_generator("--apply").returncode, 0)
        notebook = self.audit / "verification.Rmd"
        text = notebook.read_text()
        self.assertIn("I4R-SNIPPET-SHA256", text)
        notebook.write_text(text.replace("observed <- 9", "observed <- 8", 1))
        manifest_path = self.audit / "work/finding_projections.manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for row in manifest["artifacts"]:
            if row["path"] == "verification.Rmd":
                row["sha256"] = hashlib.sha256(notebook.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        rejected = self.run_generator("--check")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("wrong code snippet", rejected.stdout + rejected.stderr)

    def test_missing_or_escaping_snippet_cannot_partially_overwrite(self) -> None:
        self.assertEqual(self.run_generator("--apply").returncode, 0)
        protected = [
            self.audit / "report/REPORT_final.md", self.audit / "report/LEDGER.md",
            self.audit / "comment.json", self.audit / "claims_map.tsv",
            self.audit / "verification.Rmd",
        ]
        before = {path: path.read_bytes() for path in protected}
        missing = copy.deepcopy(self.row)
        missing["verification_snippet"]["path"] = "work/does-not-exist.R"
        (self.audit / "work/findings.jsonl").write_text(json.dumps(missing) + "\n")
        failed = self.run_generator("--apply")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(before, {path: path.read_bytes() for path in protected})

        outside = pathlib.Path(self.temporary.name) / "private.R"
        outside.write_text("secret <- TRUE\n")
        link = self.audit / "work/linked-private.R"
        link.symlink_to(outside)
        escaping = copy.deepcopy(self.row)
        escaping["verification_snippet"]["path"] = "work/linked-private.R"
        (self.audit / "work/findings.jsonl").write_text(json.dumps(escaping) + "\n")
        escaped = self.run_generator("--apply")
        self.assertNotEqual(escaped.returncode, 0)
        self.assertEqual(before, {path: path.read_bytes() for path in protected})

    def test_non_r_code_snippet_fails_before_projection(self) -> None:
        python_source = self.audit / "work/check_primary.py"
        python_source.write_text("print(9)\n")
        row = copy.deepcopy(self.row)
        row["verification_snippet"]["path"] = "work/check_primary.py"
        (self.audit / "work/findings.jsonl").write_text(json.dumps(row) + "\n")

        failed = self.run_generator("--apply")

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("must be R source", failed.stdout + failed.stderr)
        self.assertFalse((self.audit / "work/finding_projections.manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
