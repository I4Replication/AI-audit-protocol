"""2.7.11: predict the document build's false abstract-end refusals from the report, before the build.

document_release.abstract_end_page cannot change (every published comment's document manifest binds its hash). It
refused nine audits' builds whose abstract did end on page 1: when the abstract's closing words recur later in the
comment (two pages match) and when a hyphenated compound among them breaks across a PDF line (no page matches).
scripts/check_abstract_end.py predicts both from REPORT_final.md with the locator's own tokenizer. Each case below
writes a report on disk, runs the real check, and feeds the same report with the pages its PDF would show to the
real (frozen) locator, so the prediction and the gate are compared on the same input.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import document_release  # noqa: E402

CLOSING = "No finding changes the published conclusions about the motor learning effect in this sample."   # 14 words


def report(abstract_end: str, later: str = "The remaining sections give the detail.") -> str:
    return ("# Comment\n\n## Abstract\n\nWe reproduced the main analysis of the article from its deposit and checked "
            f"every reported value against the data. {abstract_end}\n\n## 1. Introduction\n\nThe article reports a "
            f"training study.\n\n## 2. Conclusion\n\n{later}\n")


class AbstractEndCheck(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-2711-abstract-")
        self.addCleanup(directory.cleanup)
        self.audit = pathlib.Path(directory.name)
        (self.audit / "report").mkdir()

    def run_check(self, text: str) -> subprocess.CompletedProcess[str]:
        (self.audit / "report" / "REPORT_final.md").write_text(text, encoding="utf-8")
        return subprocess.run([sys.executable, str(SKILL / "scripts" / "check_abstract_end.py"), str(self.audit)],
                              text=True, capture_output=True, timeout=60, env={"PYTHONDONTWRITEBYTECODE": "1"})

    def test_closing_words_repeated_later_will_refuse_and_the_locator_does(self) -> None:
        text = report(CLOSING, later=f"In short: {CLOSING}")
        result = self.run_check(text)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("recur later in the comment", result.stdout)
        pages = ["We reproduced ... data. " + CLOSING, "1. Introduction ...", "2. Conclusion In short: " + CLOSING]
        with self.assertRaisesRegex(document_release.DocumentReleaseError, r"matches=\[1, 3\]"):
            document_release.abstract_end_page(text, pages)

    def test_only_the_last_eight_words_repeated_later_may_refuse(self) -> None:
        # Review DT-7 A1: the 'last 8 recur' path (exit 3) caught A97's refusals; its last 12 do not recur
        text = report(CLOSING, later="Elsewhere we say little about the motor learning effect in this sample.")
        result = self.run_check(text)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("last 8 words recur later in the comment", result.stdout)
        # page 1's last 12 broken (a word the PDF puts between them), so the locator falls back to 8: two pages
        pages = ["We reproduced ... data. No finding changes the published 2 conclusions about the motor learning "
                 "effect in this sample.", "1. Introduction ...",
                 "2. Conclusion Elsewhere we say little about the motor learning effect in this sample."]
        with self.assertRaisesRegex(document_release.DocumentReleaseError, r"matches=\[1, 3\]"):
            document_release.abstract_end_page(text, pages)
        self.assertEqual(document_release.abstract_end_page(text, pages[:2] + ["2. Conclusion"]), 1)

    def test_a_hyphenated_word_at_the_end_may_refuse_and_does_when_the_line_breaks_there(self) -> None:
        ending = "The deposit holds the de-identified records."
        result = self.run_check(report(ending))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("de-identified", result.stdout)
        text = report(ending)
        broken = "We reproduced the main analysis of the article from its deposit and checked every reported\n" \
                 "value against the data. The deposit holds the de-\nidentified records."
        with self.assertRaisesRegex(document_release.DocumentReleaseError, r"matches=\[\]"):
            document_release.abstract_end_page(text, [broken, "1. Introduction", "2. Conclusion"])
        self.assertEqual(document_release.abstract_end_page(text, [broken.replace("de-\n", "de-"), "x", "y"]), 1)

    def test_emphasis_inside_a_word_at_the_end_will_refuse(self) -> None:
        text = report("Every claim rests on the reported *p*-values.")
        result = self.run_check(text)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("markup inside a word", result.stdout)
        rendered = "We reproduced the main analysis ... against the data. Every claim rests on the reported p-values."
        with self.assertRaisesRegex(document_release.DocumentReleaseError, r"matches=\[\]"):
            document_release.abstract_end_page(text, [rendered, "1.", "2."])

    def test_negative_control_a_plain_ending_passes_and_the_locator_finds_page_one(self) -> None:
        text = report(CLOSING)
        result = self.run_check(text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("abstract end: ok", result.stdout)
        self.assertEqual(document_release.abstract_end_page(text, ["Abstract ... " + CLOSING, "1.", "2."]), 1)

    def test_underscore_emphasis_inside_a_word_at_the_end_will_refuse(self) -> None:
        text = report("Every claim rests on the reported _p_-values.")
        result = self.run_check(text)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("markup inside a word", result.stdout)
        rendered = "We reproduced ... against the data. Every claim rests on the reported p-values."
        with self.assertRaisesRegex(document_release.DocumentReleaseError, r"matches=\[\]"):
            document_release.abstract_end_page(text, [rendered, "1.", "2."])

    def test_controls_a_name_with_underscores_and_a_title_repeating_the_ending_pass(self) -> None:
        # an underscore inside a name is not emphasis, and the front matter shares page 1 with the abstract
        text = report("Its main script is clean_scores_final.R and runs without error.").replace(
            "# Comment", "# Comment: its main script is clean_scores_final.R and runs without error")
        result = self.run_check(text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        page_one = ("Comment: its main script is clean_scores_final.R and runs without error. We reproduced ... data. "
                    "Its main script is clean_scores_final.R and runs without error.")
        self.assertEqual(document_release.abstract_end_page(text, [page_one, "1.", "2."]), 1)

    def test_a_table_caption_repeating_the_ending_will_refuse(self) -> None:
        (self.audit / "report" / "captions.txt").write_text(f"Summary. {CLOSING}\n", encoding="utf-8")
        text = report(CLOSING)
        result = self.run_check(text)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        with self.assertRaisesRegex(document_release.DocumentReleaseError, r"matches=\[1, 2\]"):
            document_release.abstract_end_page(text, ["Abstract ... " + CLOSING, "Table 1. Summary. " + CLOSING])

    def test_a_report_without_an_abstract_is_named(self) -> None:
        result = self.run_check("# Comment\n\n## 1. Introduction\n\nText.\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("lacks an Abstract", result.stderr)


if __name__ == "__main__":
    unittest.main()
