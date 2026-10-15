"""A finalization receipt must still verify after the audit has been published.

`publish_completed.sh` journals the filing itself, deliberately, AFTER writing and verifying the
receipt -- so the run.json it files a moment earlier does not claim to contain its own filing
record. Six receipt-bound artifacts are projections of that journal, so the append changed all
six and `finalization_receipt.py check` failed on every published audit from then on. Both audits
published before this was noticed were stale, and the failure names an artifact
(`invocations.watermark.json`) that gives an operator no idea the cause is publication itself.

The journal is append-only and hash-chained, so an append does not alter what was finalized. The
check now verifies those six by prefix. These tests hold the line on both sides: an ordinary
publication verifies, and everything that is genuinely stale still fails.
"""
import json
import os
# only the fixtures' subprocesses get these (publish into a scratch root, file into a fresh registry)
FIXTURE_ENV = {"I4R_PUBLISH_ANY_ROOT": "1", "I4R_CREATE_REGISTRY": "1"}
import pathlib
import subprocess
import tempfile
import unittest

from tests.test_release_pipeline import make_audit, write

SKILL = pathlib.Path(__file__).resolve().parents[1]
PUBLISH = SKILL / "scripts" / "publish_completed.sh"
RECEIPT = SKILL / "scripts" / "finalization_receipt.py"
LOG_EVENT = SKILL / "scripts" / "log_event.sh"


class ReceiptAfterPublicationTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-receipt-publication-")
        self.root = pathlib.Path(self.temporary.name)
        self.environment = {**os.environ, **FIXTURE_ENV}
        self.environment.update({"LC_ALL": "C", "I4R_RESERVE_GRACE": "0"})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def publish(self, paper: str) -> pathlib.Path:
        audit = make_audit(self.root, paper=paper)
        result = subprocess.run(
            ["/bin/bash", str(PUBLISH), str(audit), str(self.root / "completed")],
            text=True, capture_output=True, timeout=60, env=self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return audit

    def check(self, audit: pathlib.Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", str(RECEIPT), "check", str(audit)],
            text=True, capture_output=True, timeout=60, env=self.environment)

    def log(self, audit: pathlib.Path, *arguments: str) -> None:
        result = subprocess.run(
            ["/bin/bash", str(LOG_EVENT), "--run", str(audit / "run.json"), *arguments],
            text=True, capture_output=True, timeout=60, env=self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_receipt_verifies_after_publication_and_names_the_append(self) -> None:
        audit = self.publish("Paper-Published-Receipt")
        # The filing really did land in the journal, so this is not a vacuous pass.
        types = [json.loads(line)["type"]
                 for line in (audit / "run.events.jsonl").read_text().splitlines() if line.strip()]
        self.assertEqual("delivery", types[-1])

        result = self.check(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("journal extended after finalization", result.stdout)
        self.assertIn("delivery", result.stdout)

    def test_an_event_a_finished_audit_cannot_gain_still_fails(self) -> None:
        """Negative control: only rows a filed audit may legitimately gain are tolerated."""
        audit = self.publish("Paper-Reopened")
        self.log(audit, "gate", "gate=check_consistency", "round=1", "exit_status=0")
        result = self.check(audit)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("finalization receipt is stale", result.stdout + result.stderr)

    def test_a_changed_deliverable_still_fails_even_with_a_legitimate_append(self) -> None:
        """Negative control: the relaxation is for journal projections and nothing else."""
        audit = self.publish("Paper-Edited-Report")
        report = audit / "report/REPORT_final.md"
        write(report, report.read_text() + "\nAn edit made after finalization.\n")
        result = self.check(audit)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("report/REPORT_final.md", result.stdout + result.stderr)

    def test_a_rewritten_journal_row_still_fails(self) -> None:
        """Negative control: the prefix must be intact, not merely long enough."""
        audit = self.publish("Paper-Tampered-Journal")
        events = [line for line in (audit / "run.events.jsonl").read_text().splitlines() if line.strip()]
        first = json.loads(events[0])
        first["payload"] = dict(first.get("payload") or {}, tampered=True)
        events[0] = json.dumps(first, sort_keys=True)
        write(audit / "run.events.jsonl", "\n".join(events) + "\n")
        result = self.check(audit)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
