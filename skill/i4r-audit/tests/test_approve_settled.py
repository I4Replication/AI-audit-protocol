"""approve_settled.py binds the settled-promotion approval to the current report and clears it elsewhere."""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "approve_settled.py"
FIXTURE = SKILL / "schemas" / "fixtures" / "v2_finding_row.json"


def make_audit(root: pathlib.Path) -> tuple[pathlib.Path, str]:
    (root / "report").mkdir(parents=True)
    (root / "work").mkdir(parents=True)
    report = root / "report" / "REPORT_final.md"
    report.write_text("# Comment\n\nBody.\n", encoding="utf-8")
    promotable = json.loads(FIXTURE.read_text(encoding="utf-8"))
    promotable["settled"] = {"approved": True, "valid_for_report_hash": "a" * 64}  # stale binding
    excluded = copy.deepcopy(promotable)
    excluded.update({"id": "F2", "coordinator_approved": False, "status": "rejected",
                     "ledger_disposition": {"status": "EXCLUDED", "target": None, "reason": "tested negative"}})
    excluded["settled"] = {"approved": True, "valid_for_report_hash": "b" * 64}  # must be cleared
    registry = root / "work" / "findings.jsonl"
    registry.write_text(json.dumps(promotable) + "\n" + json.dumps(excluded) + "\n", encoding="utf-8")
    return registry, hashlib.sha256(report.read_bytes()).hexdigest()


class ApproveSettledTest(unittest.TestCase):
    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)

    def test_binds_promotable_rows_and_clears_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            registry, report_sha = make_audit(root)
            before = registry.read_text(encoding="utf-8")
            dry = self.run_script(str(root), "--dry-run")
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertEqual(registry.read_text(encoding="utf-8"), before, "dry run must not write")
            result = self.run_script(str(root))
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = [json.loads(line) for line in registry.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["settled"], {"approved": True, "valid_for_report_hash": report_sha})
            self.assertIsNone(rows[1]["settled"])
            self.assertIn("1 rows", result.stdout)
            self.assertIn("1 cleared", result.stdout)

    def test_refuses_an_invalid_row_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            registry, _ = make_audit(root)
            rows = [json.loads(line) for line in registry.read_text(encoding="utf-8").splitlines()]
            rows[0]["verifications"] = rows[0]["verifications"][:1]  # promotable row with one confirmation
            before = "".join(json.dumps(r) + "\n" for r in rows)
            registry.write_text(before, encoding="utf-8")
            result = self.run_script(str(root))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("two confirmed reviewers", result.stderr)
            self.assertEqual(registry.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
