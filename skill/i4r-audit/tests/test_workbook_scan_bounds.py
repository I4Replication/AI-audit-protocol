#!/usr/bin/env python3
"""workbook_summaries must bound its cost without misreporting what a workbook is.

Two defects, both from the size cap sitting above the suffix dispatch:

  * every oversize file of ANY kind was appended as an "oversize workbook" -- 34 in this tree
    (.zip, .rds, .mov, .sav) across 22 papers -- inflating workbook_count and telling the role
    to "read the file directly if a claim depends on it" about a movie;
  * the cap was a flat 20 MB, which dropped a 20.3 MB / 387k-row xlsx that costs 6.1 s, while
    size is a poor proxy for cost in the first place.

The real bound is time, and exceeding it must degrade one file's evidence rather than fail the
whole preflight after the paper is reserved and pkg/ frozen.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import time
import unittest


SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))


def _load():
    spec = importlib.util.spec_from_file_location(
        "phase1_runtime", SKILL / "scripts" / "phase1_runtime.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase1_runtime"] = module
    spec.loader.exec_module(module)
    return module


phase1 = _load()


class WorkbookScanBoundsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="i4r-workbook-")
        self.base = Path(self._tmp.name).resolve()
        self._saved = (
            phase1.WORKBOOK_SUMMARY_MAX_BYTES,
            phase1.WORKBOOK_SUMMARY_FILE_SECONDS,
            phase1.WORKBOOK_SUMMARY_TOTAL_SECONDS,
        )

    def tearDown(self) -> None:
        (phase1.WORKBOOK_SUMMARY_MAX_BYTES,
         phase1.WORKBOOK_SUMMARY_FILE_SECONDS,
         phase1.WORKBOOK_SUMMARY_TOTAL_SECONDS) = self._saved
        self._tmp.cleanup()

    def big(self, name: str, megabytes: int = 1) -> tuple[str, Path]:
        path = self.base / name
        path.write_bytes(b"\0" * (megabytes * 1024 * 1024))
        return (name, path)

    def csv(self, name: str, rows: int) -> tuple[str, Path]:
        path = self.base / name
        with path.open("w", encoding="utf-8") as handle:
            handle.write("participant,value,group\n")
            for index in range(rows):
                handle.write(f"p{index},{index % 97},{'A' if index % 2 else 'B'}\n")
        return (name, path)

    def test_non_tabular_files_are_not_workbooks_however_large(self) -> None:
        phase1.WORKBOOK_SUMMARY_MAX_BYTES = 1024  # every fixture is "oversize"
        files = [self.big(name) for name in
                 ("clip.mov", "model.rds", "deposit.zip", "survey.sav", "lib.dll")]
        result = phase1.workbook_summaries(files)
        self.assertEqual(result["workbook_count"], 0)
        self.assertEqual(result["workbooks"], [])

    def test_oversize_workbook_is_reported_as_a_workbook(self) -> None:
        phase1.WORKBOOK_SUMMARY_MAX_BYTES = 1024
        result = phase1.workbook_summaries([self.csv("values.csv", 5000)])
        self.assertEqual(result["workbook_count"], 1)
        row = result["workbooks"][0]
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("oversize", row["unavailable_reason"])
        # The row keeps the same shape as a summarised one, so consumers need no special case.
        self.assertEqual(row["format"], "csv")
        self.assertEqual(len(row["sha256"]), 64)

    def test_a_workbook_under_the_cap_is_summarised(self) -> None:
        result = phase1.workbook_summaries([self.csv("values.csv", 2000)])
        row = result["workbooks"][0]
        self.assertEqual(row["status"], "summarized")
        self.assertTrue(row["sheets"])

    def test_per_file_budget_degrades_one_file_not_the_preflight(self) -> None:
        phase1.WORKBOOK_SUMMARY_FILE_SECONDS = 0.01
        files = [self.csv("slow.csv", 200_000), self.csv("quick.csv", 5)]
        outer = phase1.MonotonicDeadline.after(60)
        result = phase1.workbook_summaries(files, outer)
        rows = {row["path"]: row for row in result["workbooks"]}
        self.assertEqual(rows["slow.csv"]["status"], "unavailable")
        self.assertIn("per-file", rows["slow.csv"]["unavailable_reason"])
        # The scan continued, and the shared deadline is untouched.
        self.assertEqual(rows["quick.csv"]["status"], "summarized")
        outer.check("still alive")

    def test_an_expired_shared_deadline_still_fails_the_preflight(self) -> None:
        """Negative control: degrading our own budget must not swallow the real deadline."""
        files = [self.csv("values.csv", 50_000)]
        expired = phase1.MonotonicDeadline(time.monotonic() - 1)
        with self.assertRaises(phase1.Phase1Error):
            phase1.workbook_summaries(files, expired)

    def test_total_scan_budget_bounds_a_directory_of_slow_files(self) -> None:
        phase1.WORKBOOK_SUMMARY_FILE_SECONDS = 5.0
        phase1.WORKBOOK_SUMMARY_TOTAL_SECONDS = 0.05
        files = [self.csv(f"slow_{index}.csv", 100_000) for index in range(4)]
        started = time.monotonic()
        result = phase1.workbook_summaries(files, phase1.MonotonicDeadline.after(60))
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(len(result["workbooks"]), 4)
        self.assertTrue(any(row["status"] == "unavailable" for row in result["workbooks"]))

    def test_xls_and_ods_are_workbooks_without_a_reader(self) -> None:
        result = phase1.workbook_summaries([self.big("legacy.xls"), self.big("open.ods")])
        self.assertEqual(result["workbook_count"], 2)
        for row in result["workbooks"]:
            self.assertEqual(row["status"], "unavailable")
            self.assertIn("deterministic reader", row["unavailable_reason"])


if __name__ == "__main__":
    unittest.main()
