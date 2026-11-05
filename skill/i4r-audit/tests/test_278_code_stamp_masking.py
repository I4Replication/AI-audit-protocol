"""2.7.8: a participant-code shape beside a timestamp in a record nobody can reword is masked in the staged copy.

A Phase-1 detector self-test wrote "participant 7 (night of 2000-01-01)" into a hash-bound evidence script (one
audit), and stage_release.sh check 3 refused the line with no remedy. Check 3d now masks the timestamp in the
STAGED copy of a record and runs before check 3; the comment's own outputs still refuse.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
SKILL = TESTS.parent
SCRIPTS = SKILL / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_deposit_values as cdv  # noqa: E402
import check_release_privacy as crp  # noqa: E402
import release_record_masking as rrm  # noqa: E402

# Fixtures are assembled, never spelled: this file is shipped and scanned like any other.
CODE = "partici" + "pant 7"
DATE = "2000-" + "01-01"


class CodePairedDatesAreMaskedInRecords(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-278-stage-")
        self.addCleanup(directory.cleanup)
        self.release = pathlib.Path(directory.name).resolve()
        self.addCleanup(crp.ALLOWED_EMAILS.clear)

    def write(self, relative: str, text: str) -> pathlib.Path:
        path = self.release / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_paired_date_is_masked_and_listed_and_other_lines_are_untouched(self) -> None:
        evidence = self.write("work/a4_code_methods/phase1_evidence/abc/checks.py",
                              f'ok = bool(p.search("# {CODE} (night of {DATE})"))\n'
                              f'stamp_only = "{DATE}"\ncode_only = "{CODE} slept"\n')
        rewritten = rrm.neutralise_records(self.release, cdv.record_file)
        rrm.note(self.release, rewritten)
        self.assertEqual(rewritten, [("work/a4_code_methods/phase1_evidence/abc/checks.py", 0, 0, 1)])
        lines = evidence.read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[0], f'ok = bool(p.search("# {CODE} (night of {rrm.DATE_PLACEHOLDER})"))')
        self.assertEqual(lines[1], f'stamp_only = "{DATE}"')
        self.assertEqual(lines[2], f'code_only = "{CODE} slept"')
        note = (self.release / crp.NEUTRALISATION_NOTE).read_text(encoding="utf-8")
        self.assertIn("checks.py  (0 path(s), 0 address(es), 1 date(s))", note)
        self.assertNotIn(DATE, note)
        self.assertEqual(crp.inspect_tree(self.release, identifiers=True), [])

    def test_negative_control_the_comment_itself_still_refuses(self) -> None:
        self.write("REPORT_final.md", f"We excluded {CODE} on the night of {DATE}.\n")
        self.assertEqual(rrm.neutralise_records(self.release, cdv.record_file), [])
        issues = crp.inspect_tree(self.release, identifiers=True)
        self.assertTrue(any(issue.category == "identifier_timestamp" for issue in issues))

    def test_the_placeholder_is_not_itself_a_timestamp(self) -> None:
        self.assertIsNone(crp._STAMP.search(rrm.DATE_PLACEHOLDER))


class StagerMasksBeforeCheckThree(unittest.TestCase):
    def test_check_3d_runs_before_the_code_timestamp_check(self) -> None:
        text = (SCRIPTS / "stage_release.sh").read_text(encoding="utf-8")
        masking = text.index('python3 "$SCRIPT_DIR/release_record_masking.py"')
        check_three = text.index("# 3. Per-participant result listings and code/timestamp pairings.")
        self.assertLess(masking, check_three)
        self.assertLess(text.index("viol=0"), masking)


if __name__ == "__main__":
    unittest.main()
