"""A superseded completed ledger row must not be hash-checked against current bytes.

Roles promote to a fixed canonical path (report/02_data_integrity.md). When a role
completes twice, attempt 2 overwrites attempt 1's bytes at that path. The ledger is
append-only, so attempt 1's completed row stays in the file with the old hash. Re-verifying
it against the file on disk rejected an entirely correct ledger on audit
A34 and blocked the coordinator. read_ledger must verify bytes only for the latest
completed row per artifact; record_invocation.validate_record must accept the flag.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import record_invocation  # noqa: E402
import run_coordinator  # noqa: E402


class ReadLedgerSupersession(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        rows = [
            {"status": "completed", "output_artifact": "report/02.md", "output_sha256": "a" * 64},
            {"status": "failed", "output_artifact": None, "output_sha256": None},
            {"status": "completed", "output_artifact": "report/05.md", "output_sha256": "b" * 64},
            {"status": "completed", "output_artifact": "report/02.md", "output_sha256": "c" * 64},
        ]
        (self.root / "invocations.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_only_latest_completed_row_per_artifact_is_byte_verified(self) -> None:
        seen: list[tuple[str | None, str | None, bool]] = []

        def fake_validate(row, root, *, verify_output_bytes=True):
            seen.append((row.get("status"), row.get("output_sha256"), verify_output_bytes))

        with mock.patch.object(record_invocation, "validate_record", fake_validate), \
                mock.patch.object(record_invocation, "validate_attempt_sequences", lambda rows: None):
            rows = run_coordinator.read_ledger(self.root)
        self.assertEqual(len(rows), 4)
        by_hash = {digest: verify for status, digest, verify in seen if status == "completed"}
        self.assertFalse(by_hash["a" * 64], "superseded attempt 1 must not be byte-verified")
        self.assertTrue(by_hash["c" * 64], "latest attempt for report/02.md must be verified")
        self.assertTrue(by_hash["b" * 64], "sole completed row for report/05.md must be verified")
        failed = [verify for status, _, verify in seen if status == "failed"]
        self.assertEqual(failed, [True], "non-completed rows keep the default and never hash bytes")


class ValidateRecordFlag(unittest.TestCase):
    def test_flag_skips_only_the_byte_comparison(self) -> None:
        source = (SKILL / "scripts" / "record_invocation.py").read_text(encoding="utf-8")
        self.assertIn("verify_output_bytes: bool = True", source)
        # The path-escape check must stay unconditional; only the is_file/hash pair is gated.
        gated = source.index("if verify_output_bytes:")
        escape = source.index("output_artifact escapes the audit directory")
        self.assertLess(escape, gated, "escape check must run before the gated byte check")


if __name__ == "__main__":
    unittest.main()
