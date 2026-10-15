"""2.7.4 (fix of 2026-10-05): a body page that opens with the word "appendix" is not back matter.

publish_completed.sh recounts the flattened publication's PDF with document_release.recount_candidate(), which takes
the first page whose first rendered line is a back-matter heading as the start of the back matter. The test used to
look only at the line's first word. On the MacIntyre 2026 audit (20261005-audit-312023cc), body page 8 opened
mid-sentence with "appendix output and pkg/FS_during.xlsx". The recount put the boundary on page 8 against the
manifest's page 10 and refused the publish. build_docx.sh, stage_release.sh and finalize_run.sh, which locate the
boundary from the report's own heading, had all accepted the document. The whole rendered line must now be the
heading: "Data and Code Availability", "References", or "Appendix" with an optional label and title
("Appendix A. Summary of findings").

No PDF renderer is needed: the recount tests stub the manifest loader and the page extraction, so they exercise the
boundary logic alone. No provider or model is called.
"""
from __future__ import annotations

import hashlib
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import document_release  # noqa: E402

HEADINGS = [
    "Data and Code Availability",
    "References",
    "Appendix A. Summary of findings",
    "Appendix B. Deposited and article files examined",
    "Appendix",
    "Appendix A",
    "Appendix A: Files examined",
    "  Data  and   Code Availability ",
]
BODY_LINES = [
    "appendix output and pkg/FS_during.xlsx. Impact. Wording only. Remedy. Describe condition",
    "References to the appendix follow in Section 13.",
    "references.",
    "Appendix A lists every finding with its section.",
    "Data and code availability is limited to Study 2.",
    "appendix",
    "Section 16. Hypotheses tested and not confirmed",
    "",
]


class HeadingLine(unittest.TestCase):
    def test_canonical_headings_are_back_matter(self) -> None:
        for line in HEADINGS:
            with self.subTest(line=line):
                self.assertTrue(document_release.pdf_line_starts_back_matter(line))

    def test_body_text_beginning_with_a_heading_word_is_not(self) -> None:
        for line in BODY_LINES:
            with self.subTest(line=line):
                self.assertFalse(document_release.pdf_line_starts_back_matter(line))


class Recount(unittest.TestCase):
    """recount_candidate() on a four-page document whose back matter starts on page 3."""

    def _recount(self, pages: list[str]) -> dict[str, int]:
        with tempfile.TemporaryDirectory() as tmp:
            pdf = pathlib.Path(tmp) / "comment.pdf"
            pdf.write_bytes(b"%PDF-1.7 stub for the boundary recount\n")
            manifest = {
                "build_kind": "canonical", "release_eligible": True,
                "artifacts": {"pdf": {"path": "report/comment.pdf", "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                                      "bytes": pdf.stat().st_size}},
                "pages": {"total_pages": 4, "body_pages": 2, "back_matter_pages": 2, "first_back_matter_page": 3,
                          "body_page_cap": document_release.CANONICAL_BODY_CAP},
            }
            with mock.patch.object(document_release, "load_manifest", return_value=manifest), \
                    mock.patch.object(document_release, "exact_page_count", return_value=len(pages)), \
                    mock.patch.object(document_release, "page_texts", return_value=pages):
                return document_release.recount_candidate(pathlib.Path(tmp) / "document_build_manifest.json", pdf)

    def test_body_page_opening_with_appendix_mid_sentence(self) -> None:
        pages = [
            "A comment on the article\nAbstract\nThe deposit reproduces the models.",
            "appendix output and pkg/FS_during.xlsx. Impact. Wording only. Remedy. Describe condition\nterms.",
            "Data and Code Availability\nAll values come from the deposited files.",
            "References\nBrodeur, A. (2026).",
        ]
        self.assertEqual(self._recount(pages), {"total_pages": 4, "body_pages": 2,
                                                "body_page_cap": document_release.CANONICAL_BODY_CAP})

    def test_a_real_heading_before_the_manifest_boundary_still_refuses(self) -> None:
        # Negative control: a page that does open with a back-matter heading moves the recounted boundary.
        pages = [
            "A comment on the article\nAbstract\nThe deposit reproduces the models.",
            "References\nBrodeur, A. (2026).",
            "Data and Code Availability\nAll values come from the deposited files.",
            "Appendix A. Summary of findings\n| # | Finding |",
        ]
        with self.assertRaisesRegex(document_release.DocumentReleaseError, "boundary differs from the document manifest"):
            self._recount(pages)


if __name__ == "__main__":
    unittest.main()
