#!/usr/bin/env python3
"""Appendix A renders as its literal text, in columns that keep its words whole (2.6.8).

generate_finding_artifacts.render() writes the findings table. It is post-delta gate code and keeps
its bytes, so the fix lives in the document build. Before 2.6.8, pandoc read the claim cells as
markdown: 'F*df1/(F*df1+df2)' became italics and lost its asterisks. The table's dash widths also
left the #, Status and Section columns too narrow, so LibreOffice broke 'CONFIRMED', 'SPECULATIVE',
'Section' and two-digit finding numbers across lines. build_docx.sh now rewrites the table in its
converter input only, and refuses a build whose exact DOCX or PDF does not show the table literally
and with every word whole.

Every test builds its condition on disk and runs the real code. Each one fails on 2.6.7, except
the negative control that shows the untransformed generator block is read as markup.

The review fixes of 2.6.8 (fix X) add: column widths that keep a Section cell such as
'Section 10.2' on one line on Letter and A4; format characters (a zero-width space, a word joiner,
a soft hyphen) and scripts written without spaces never refuse a legitimate claim; the
stale-manifest refusal names its rebuild; and the document cache hits with the real pandoc.
"""
from __future__ import annotations

import copy
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unicodedata
import unittest
import zipfile


SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
BUILDER = SCRIPTS / "build_docx.sh"
HELPER = SCRIPTS / "document_release.py"
FIXTURE_ROW = SKILL / "schemas/fixtures/v2_finding_row.json"
sys.dont_write_bytecode = True
sys.path.insert(0, str(SKILL))
sys.path.insert(0, str(SCRIPTS))
import document_release  # noqa: E402
from tests.test_document_release import multi_page_pdf, policy_snapshot  # noqa: E402

WORDML_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    candidate = Path.home() / "miniconda3/envs/i4rdoc/bin" / name
    return str(candidate) if candidate.is_file() else None


PANDOC = find_tool("pandoc")
SOFFICE = shutil.which("soffice") or next(
    (path for path in ("/Applications/LibreOffice.app/Contents/MacOS/soffice",) if Path(path).is_file()),
    None)
POPPLER = bool(shutil.which("pdfinfo") and shutil.which("pdftotext"))


def load_generator():
    spec = importlib.util.spec_from_file_location(
        "i4r_appendix_generator", SCRIPTS / "generate_finding_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


GENERATOR = load_generator()

# Claims whose characters pandoc reads as markup when they reach it verbatim.
MARKDOWN_HEAVY = [
    "Two printed effect sizes exceed eta_p2 = F*df1/(F*df1+df2) by a factor of ten.",
    "The table reports a<b and c>d for the two arms.",
    "The cost is $x$ per unit in both arms.",
    "The script sets _flag_ before the model is fitted.",
    "The call `lm(y ~ x)` omits the covariate.",
    "The syntax reads data\\raw\\items.sav, which is absent.",
    "The supplement writes the growth term as x^2^.",
    "Emissions are given as CO~2~ equivalents.",
    "The link [t](u) in the appendix is dead.",
    "The deposit cites @k without a reference entry.",
    "Scores & ranks disagree, and &amp; is printed.",
    "The option a|b is undocumented.",
    "Two cells hold 'x' and 'y ' (the second with a trailing space).",
]
STATUSES = ("CONFIRMED", "LIKELY", "SPECULATIVE")


def finding_rows(claims: list[str]) -> list[dict]:
    base = json.loads(FIXTURE_ROW.read_text(encoding="utf-8"))
    rows = []
    for number, claim in enumerate(claims, 1):
        row = copy.deepcopy(base)
        row["id"] = f"F{number}"
        row["claim"] = claim
        row["confidence"] = STATUSES[(number - 1) % len(STATUSES)]
        row["report_section"] = "Section 10.2" if number >= 10 else f"Section {number + 2}"
        row["changes_conclusion"] = number == 2
        row["changes_conclusion_which"] = "The primary conclusion." if number == 2 else None
        rows.append(row)
    return rows


def generated_appendix(claims: list[str]) -> str:
    rows = finding_rows(claims)
    return GENERATOR.render(rows, rows)["appendix"]


def table_block(appendix: str) -> list[str]:
    return [line for line in appendix.splitlines() if line.startswith("|")]


def smart_plain(text: str) -> str:
    """The test's own model of pandoc smart typography, undone, plus whitespace collapsing."""
    for curly, plain in {"‘": "'", "’": "'", "“": '"', "”": '"',
                         "–": "--", "—": "---", "…": "...", " ": " "}.items():
        text = text.replace(curly, plain)
    return " ".join(text.split())


def pandoc_table(lines: list[str]) -> dict:
    result = subprocess.run(
        [PANDOC, "-f", "markdown+pipe_tables", "-t", "json"],
        input="\n".join(lines) + "\n", text=True, capture_output=True, timeout=60, check=True)
    tables = [block for block in json.loads(result.stdout)["blocks"] if block["t"] == "Table"]
    if len(tables) != 1:
        raise AssertionError(f"expected one table, pandoc produced {len(tables)}")
    return tables[0]


def body_cells(table: dict) -> list[list[list[dict]]]:
    """Inline lists of every body cell, row by row (pandoc 3 JSON table layout)."""
    rows = []
    for body in table["c"][4]:
        for row in body["c"][3] if isinstance(body, dict) else body[3]:
            cells = []
            for cell in row[1]:
                inlines: list[dict] = []
                for block in cell[4]:
                    if block["t"] not in ("Plain", "Para"):
                        inlines.append({"t": block["t"]})
                    else:
                        inlines.extend(block["c"])
                cells.append(inlines)
            rows.append(cells)
    return rows


def inline_kinds(inlines: list[dict]) -> set[str]:
    kinds: set[str] = set()
    for inline in inlines:
        kinds.add(inline["t"])
        content = inline.get("c")
        if isinstance(content, list):
            for part in content:
                if (isinstance(part, list) and part and isinstance(part[0], dict)
                        and "t" in part[0]):
                    kinds |= inline_kinds(part)
    return kinds


def plain_text(inlines: list[dict]) -> str:
    return "".join(inline["c"] if inline["t"] == "Str" else " " for inline in inlines
                   if inline["t"] in ("Str", "Space", "SoftBreak"))


def wordml_docx(path: Path, tables: list[list[list[str]]]) -> None:
    """A DOCX whose tables hold exactly these cell texts, each word in its own run.

    A soft hyphen is written the way pandoc writes it, as its own <w:softHyphen/> run.
    """
    def cell(text: str) -> str:
        runs = []
        for piece in re.findall(r"\S+\s*|\s+", text):
            for index, part in enumerate(piece.split("\u00ad")):
                if index:
                    runs.append("<w:r><w:softHyphen/></w:r>")
                if part:
                    runs.append(f'<w:r><w:t xml:space="preserve">{html.escape(part, quote=False)}'
                                "</w:t></w:r>")
        return f"<w:tc><w:p>{''.join(runs)}</w:p></w:tc>"
    body = "".join(
        "<w:tbl>" + "".join("<w:tr>" + "".join(cell(value) for value in row) + "</w:tr>"
                            for row in table) + "</w:tbl>"
        for table in tables)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml",
                         f'<w:document xmlns:w="{WORDML_NS}"><w:body>{body}</w:body></w:document>')


HEADER = ["#", "Finding", "Status", "Changes a published conclusion?", "Section"]
# Plain claims for the integrity checks: what a correct build shows is known independently.
INTEGRITY_CLAIMS = [
    "The printed ratio F*df1/(F*df1+df2) is ten times too large.",
    "A well-known exclusion rule is applied twice.",
    "The deposited frame counts do not match their labels.",
    "Attribution scores are summed before averaging.",
    "The trial count is misprinted in the methods.",
    "The random seed is never set.",
    "Two outcome columns are swapped.",
    "The reported sample excludes one site.",
    "The primary contrast uses the wrong reference.",
    "The supplement omits the tenth model.",
]


def integrity_report(root: Path, claims: list[str], *, extra: str = "") -> Path:
    report = root / "REPORT_final.md"
    report.write_text("# Audit\n\n## Abstract\n\nShort abstract.\n\n## 1. Body\n\nBody text.\n\n"
                      + generated_appendix(claims) + extra, encoding="utf-8")
    return report


def expected_rows(claims: list[str], *, finding_text=None) -> list[list[str]]:
    rows = [HEADER]
    for number, claim in enumerate(claims, 1):
        rows.append([str(number), finding_text(claim) if finding_text else claim,
                     STATUSES[(number - 1) % 3], "Yes" if number == 2 else "No",
                     "Section 10.2" if number >= 10 else f"Section {number + 2}"])
    return rows


def pdf_page_lines(rows: list[list[str]], *, split_status: str | None = None,
                   split_number: str | None = None, hyphen_break: str | None = None,
                   split_word: str | None = None,
                   finding_lines: dict[int, list[str]] | None = None) -> list[str]:
    """Rendered table lines as a PDF would show them, optionally with one defect injected.

    ``finding_lines`` gives the printed lines of chosen rows' Finding cells (row number from 1).
    """
    lines = ["Appendix A. Summary of findings", "Table 1. Summary of findings.",
             "# Finding Status Changes a published conclusion? Section"]
    for row in rows[1:]:
        number, finding, status, changes, section = row
        wrapped = textwrap.wrap(finding, width=40)
        if hyphen_break and hyphen_break in finding:
            head, tail = finding.split(hyphen_break, 1)
            left, right = hyphen_break.split("-", 1)
            wrapped = [head + left + "-", right + tail]
        if split_word and split_word[0] in finding:
            word, cut = split_word
            head, tail = finding.split(word, 1)
            wrapped = [head + word[:cut], word[cut:] + tail]
        if finding_lines and int(number) in finding_lines:
            wrapped = finding_lines[int(number)]
        first_number, next_number = number, ""
        if split_number == number:
            first_number, next_number = number[:1], number[1:]
        first_status, next_status = status, ""
        if split_status == status:
            first_status, next_status = status[:6], status[6:]
        lines.append(f"{first_number} {wrapped[0]} {first_status} {changes} {section}")
        continuation = " ".join(part for part in (next_number, *wrapped[1:], next_status) if part)
        if continuation:
            lines.append(continuation)
    return lines


def write_pdf(path: Path, table_lines: list[str]) -> None:
    pages = [["Audit", "Abstract", "Short abstract."]]
    for start in range(0, len(table_lines), 40):
        pages.append(table_lines[start:start + 40])
    path.write_bytes(multi_page_pdf(pages))


class FindingsTableTransformTest(unittest.TestCase):
    """The converter-input rewrite, exercised on the generator's own output."""

    def test_the_generator_header_is_the_one_the_build_rewrites(self) -> None:
        appendix = generated_appendix(MARKDOWN_HEAVY)
        block = table_block(appendix)
        self.assertEqual(block[0], document_release.FINDINGS_TABLE_HEADER)
        self.assertTrue(document_release.is_findings_table(block))
        transformed = document_release.pandoc_findings_table(block)
        self.assertNotEqual(transformed, block, "the rewrite is a no-op on generator output")
        self.assertEqual(transformed[1], document_release.FINDINGS_TABLE_SEPARATOR)
        self.assertEqual(len(transformed), len(block))

    def test_cells_invert_the_generator_escape(self) -> None:
        for raw in ["a|b", "a\\|b", "ends with \\", "two || pipes", "\\\\|", "plain"]:
            line = "| 1 | " + GENERATOR.clean_cell(raw) + " | CONFIRMED | No | Section 3 |"
            self.assertEqual(document_release.findings_table_cells(line),
                             ["1", raw, "CONFIRMED", "No", "Section 3"], raw)

    def test_other_tables_pass_through_and_malformed_rows_refuse(self) -> None:
        other = ["| File | Role | Issue |", "|---|---|---|", "| a_b.csv | data | *none* |"]
        self.assertEqual(document_release.pandoc_findings_table(other), other)
        block = table_block(generated_appendix(MARKDOWN_HEAVY[:2]))
        broken = block[:2] + ["| 1 | a claim | CONFIRMED | No |"]
        with self.assertRaises(document_release.DocumentReleaseError):
            document_release.pandoc_findings_table(broken)

    def test_claim_tags_are_stripped_before_escaping(self) -> None:
        block = table_block(generated_appendix(["See the note [claim 3] for details."]))
        transformed = document_release.pandoc_findings_table(block)
        self.assertNotIn("claim 3", "\n".join(transformed))
        self.assertIn("| See the note for details. |", transformed[2])


@unittest.skipUnless(PANDOC, "pandoc is required")
class PandocLiteralRenderingTest(unittest.TestCase):
    """Real pandoc on the rewritten table: literal cells and the fixed column widths."""

    def setUp(self) -> None:
        self.block = table_block(generated_appendix(MARKDOWN_HEAVY))
        self.rows = [[str(number), claim] for number, claim in enumerate(MARKDOWN_HEAVY, 1)]

    def test_rewritten_cells_are_plain_text_with_exact_widths(self) -> None:
        table = pandoc_table(document_release.pandoc_findings_table(self.block))
        colspecs = [(spec[0]["t"], spec[1]["c"]) for spec in table["c"][2]]
        self.assertEqual(colspecs, [("AlignRight", 0.055), ("AlignDefault", 0.425),
                                    ("AlignDefault", 0.205), ("AlignCenter", 0.155),
                                    ("AlignDefault", 0.16)])
        cells = body_cells(table)
        self.assertEqual(len(cells), len(MARKDOWN_HEAVY))
        for number, (claim, row) in enumerate(zip(MARKDOWN_HEAVY, cells), 1):
            status = STATUSES[(number - 1) % 3]
            section = "Section 10.2" if number >= 10 else f"Section {number + 2}"
            for expected, inlines in zip([str(number), claim, status,
                                          "Yes" if number == 2 else "No", section], row):
                self.assertLessEqual(inline_kinds(inlines), {"Str", "Space"}, (claim, inlines))
                self.assertEqual(smart_plain(plain_text(inlines)), expected)

    def test_negative_control_the_generator_block_is_read_as_markup(self) -> None:
        table = pandoc_table(self.block)
        widths = [spec[1].get("c") for spec in table["c"][2]]
        self.assertNotEqual(widths, [0.055, 0.425, 0.205, 0.155, 0.16])
        kinds: set[str] = set()
        mismatched = 0
        for claim, row in zip(MARKDOWN_HEAVY, body_cells(table)):
            kinds |= inline_kinds(row[1])
            mismatched += smart_plain(plain_text(row[1])) != claim
        self.assertLessEqual({"Emph", "Math", "Code", "RawInline", "Link", "Superscript",
                              "Subscript", "Cite", "Quoted"}, kinds)
        self.assertGreaterEqual(mismatched, 10)


@unittest.skipUnless(POPPLER, "Poppler exact-PDF tools are required")
class FindingsTableIntegrityTest(unittest.TestCase):
    """The fail-closed build stage, on synthetic DOCX and PDF bytes."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-appendix-integrity-")
        self.root = Path(self.temporary.name)
        self.report = integrity_report(self.root, INTEGRITY_CLAIMS)
        self.rows = expected_rows(INTEGRITY_CLAIMS)
        self.docx = self.root / "document.docx"
        self.pdf = self.root / "document.pdf"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def check(self, *, rows=None, **defect) -> str:
        wordml_docx(self.docx, [rows or self.rows])
        write_pdf(self.pdf, pdf_page_lines(self.rows, **defect))
        return document_release.findings_table_integrity(self.report, self.docx, self.pdf)

    def test_a_literal_whole_table_passes_including_a_break_at_a_hyphen(self) -> None:
        detail = self.check(hyphen_break="well-known")
        self.assertIn("10 row(s)", detail)
        self.assertIn("literal source text", detail)

    def test_smart_typography_in_the_docx_is_not_a_difference(self) -> None:
        claims = ["The authors' 'quoted' value -- and more ... here."]
        report = integrity_report(self.root, claims)
        rows = expected_rows(claims, finding_text=lambda claim: claim.replace(
            "'", "’").replace("--", "–").replace("...", "…"))
        wordml_docx(self.docx, [rows])
        write_pdf(self.pdf, pdf_page_lines(expected_rows(claims)))
        document_release.findings_table_integrity(report, self.docx, self.pdf)

    def test_a_docx_that_lost_its_asterisks_is_refused(self) -> None:
        rows = copy.deepcopy(self.rows)
        rows[1][1] = rows[1][1].replace("F*df1/(F*df1+df2)", "Fdf1/(Fdf1+df2)")
        with self.assertRaisesRegex(document_release.DocumentReleaseError,
                                    r"row 1 \(Finding\) is not the literal source text"):
            self.check(rows=rows)

    def test_a_status_broken_across_lines_is_refused(self) -> None:
        with self.assertRaisesRegex(document_release.DocumentReleaseError, "'confirmed'"):
            self.check(split_status="CONFIRMED")

    def test_a_two_digit_number_broken_across_lines_is_refused(self) -> None:
        with self.assertRaisesRegex(document_release.DocumentReleaseError, "'10'"):
            self.check(split_number="10")

    def test_a_header_repeated_on_every_page_cannot_hide_a_broken_cell_word(self) -> None:
        header = "# Finding Status Changes a published conclusion? Section"
        whole = pdf_page_lines(self.rows)
        broken = [line.replace("Section 5", "Sectio", 1) + (" n 5" if "Section 5" in line else "")
                  for line in whole]
        self.assertNotEqual(broken, whole)
        for table_lines, refused in ((whole, False), (broken, True)):
            # One table row per page, each page opening with the repeated header row.
            pages = [["Audit", "Abstract", "Short abstract."], table_lines[:3] + table_lines[3:5]]
            pages += [[header, line] for line in table_lines[5:]]
            wordml_docx(self.docx, [self.rows])
            self.pdf.write_bytes(multi_page_pdf(pages))
            if refused:
                with self.assertRaisesRegex(document_release.DocumentReleaseError, "'section'"):
                    document_release.findings_table_integrity(self.report, self.docx, self.pdf)
            else:
                document_release.findings_table_integrity(self.report, self.docx, self.pdf)

    def test_a_short_finding_word_broken_mid_word_is_refused(self) -> None:
        with self.assertRaisesRegex(document_release.DocumentReleaseError, "'attribution'"):
            self.check(split_word=("Attribution", 6))

    def test_a_word_wider_than_the_finding_column_may_break(self) -> None:
        name = "study_three_intention_positive_noise_condition.mp4"
        claims = [f"The file {name} carries the wrong marker."]
        report = integrity_report(self.root, claims)
        rows = expected_rows(claims)
        wordml_docx(self.docx, [rows])
        write_pdf(self.pdf, pdf_page_lines(rows, split_word=(name, 44)))
        detail = document_release.findings_table_integrity(report, self.docx, self.pdf)
        self.assertIn("1 Finding word(s) wider than the column", detail)

    def test_section_signs_and_claim_tags_are_modelled_as_the_build_renders_them(self) -> None:
        claims = ["See §4.1 of the paper [claim 3] for the rule."]
        report = integrity_report(self.root, claims)
        rendered = expected_rows(claims, finding_text=lambda _: "See Section 4.1 of the paper for the rule.")
        wordml_docx(self.docx, [rendered])
        write_pdf(self.pdf, pdf_page_lines(rendered))
        document_release.findings_table_integrity(report, self.docx, self.pdf)
        verbatim = expected_rows(claims)
        wordml_docx(self.docx, [verbatim])
        with self.assertRaises(document_release.DocumentReleaseError):
            document_release.findings_table_integrity(report, self.docx, self.pdf)

    def test_a_docx_without_the_findings_table_is_refused(self) -> None:
        wordml_docx(self.docx, [[["File", "Role"], ["a.csv", "data"]]])
        write_pdf(self.pdf, pdf_page_lines(self.rows))
        with self.assertRaisesRegex(document_release.DocumentReleaseError, "0 findings table"):
            document_release.findings_table_integrity(self.report, self.docx, self.pdf)

    def test_a_report_without_a_generated_table_passes_without_reading_either_file(self) -> None:
        report = self.root / "plain.md"
        report.write_text("# Audit\n\n## Abstract\n\nText.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n",
                          encoding="utf-8")
        detail = document_release.findings_table_integrity(
            report, self.root / "missing.docx", self.root / "missing.pdf")
        self.assertIn("no generated findings table", detail)

    def test_the_cli_refuses_and_writes_a_diagnostic(self) -> None:
        rows = copy.deepcopy(self.rows)
        rows[1][1] = rows[1][1].replace("*", "")
        wordml_docx(self.docx, [rows])
        write_pdf(self.pdf, pdf_page_lines(self.rows))
        diagnostic = self.root / "failure.json"
        command = [sys.executable, str(HELPER), "findings-table-integrity",
                   "--report", str(self.report), "--docx", str(self.docx), "--pdf", str(self.pdf),
                   "--build-kind", "canonical", "--diagnostic-out", str(diagnostic)]
        refused = subprocess.run(command, text=True, capture_output=True, timeout=60)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("not the literal source text", refused.stderr)
        payload = json.loads(diagnostic.read_text(encoding="utf-8"))
        self.assertEqual(payload["stage"], "findings_table_integrity")
        wordml_docx(self.docx, [self.rows])
        passed = subprocess.run(command, text=True, capture_output=True, timeout=60)
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertIn("findings table integrity: 1 table(s)", passed.stdout)


class ColumnWidthTest(unittest.TestCase):
    """The widths hold each narrow column's widest fixed text on one line, on A4 as on Letter."""

    def test_each_narrow_column_holds_its_widest_text_on_the_narrowest_page(self) -> None:
        widths = document_release.FINDINGS_TABLE_WIDTHS
        self.assertAlmostEqual(sum(widths), 1.0)
        # The separator's dash-plus-colon counts are the widths pandoc applies.
        counts = [len(part) for part in document_release.FINDINGS_TABLE_SEPARATOR.strip("|").split("|")]
        self.assertEqual([count / sum(counts) for count in counts], list(widths))
        self.assertGreater(len(document_release.FINDINGS_TABLE_SEPARATOR), 72)
        for column, text in ((0, "99"), (2, "CONFIRMED"), (2, "SPECULATIVE"), (3, "published"),
                             (3, "conclusion?"), (4, "Section 10"), (4, "Section 10.2"),
                             (4, "Section 4.10")):
            room = (widths[column] * document_release.NARROWEST_TEXT_WIDTH_POINTS
                    - document_release.CELL_PADDING_POINTS)
            self.assertLessEqual(document_release.run_points(text), room,
                                 f"{text!r} does not fit column {HEADER[column]!r} on A4")
        self.assertGreaterEqual(widths[1], 0.42, "the Finding column lost more than a point")


def strip_format_characters(text: str) -> str:
    """What pdftotext prints for text holding format characters: nothing for each of them."""
    return "".join(character for character in text if unicodedata.category(character) != "Cf")


@unittest.skipUnless(POPPLER, "Poppler exact-PDF tools are required")
class FormatCharacterIntegrityTest(unittest.TestCase):
    """Invisible characters in a claim: the words print whole, so the build must pass."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-appendix-format-")
        self.root = Path(self.temporary.name)
        self.docx = self.root / "document.docx"
        self.pdf = self.root / "document.pdf"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def check(self, claims: list[str], finding_lines: dict[int, list[str]] | None = None) -> str:
        report = integrity_report(self.root, claims)
        wordml_docx(self.docx, [expected_rows(claims)])
        printed = expected_rows(claims, finding_text=strip_format_characters)
        write_pdf(self.pdf, pdf_page_lines(printed, finding_lines=finding_lines))
        return document_release.findings_table_integrity(report, self.docx, self.pdf)

    def test_a_zero_width_space_or_word_joiner_inside_a_word_passes(self) -> None:
        detail = self.check(["The column name total\u200bscore holds the sum.",
                             "The column name total\u2060score holds the sum.",
                             "The file carries a stray byte order mark total\ufeffscore."])
        self.assertIn("3 row(s)", detail)

    def test_a_word_may_break_at_its_zero_width_space_or_soft_hyphen(self) -> None:
        claims = ["The column name total\u200bscore holds the sum.",
                  "The column soft\u00adhyphen is unlabelled in the codebook.",
                  "The column name total\u200bscore holds the sum again.",
                  "The column soft\u00adhyphen is unlabelled in the codebook again."]
        # Rows 1 and 2 break at the opportunity; rows 3 and 4 print the word unbroken.
        detail = self.check(claims, finding_lines={
            1: ["The column name total", "score holds the sum."],
            2: ["The column soft-", "hyphen is unlabelled in the codebook."],
        })
        self.assertIn("4 row(s)", detail)

    def test_a_word_broken_away_from_its_break_opportunity_is_refused(self) -> None:
        claims = ["The column soft\u00adhyphen is unlabelled in the codebook."]
        with self.assertRaisesRegex(document_release.DocumentReleaseError,
                                    r"'softhyphen'.*a character of the claim text"):
            self.check(claims, finding_lines={
                1: ["The column so", "fthyphen is unlabelled in the codebook."]})

    def test_each_character_of_an_unspaced_script_is_a_token(self) -> None:
        term = "\u8fd0\u52a8\u52a8\u673a\u91cf\u8868"
        tokens = document_release.table_tokens(f"The item {term} is mistranslated.")
        self.assertEqual(tokens, ["the", "item", *term, "is", "mistranslated"])
        broken = document_release.table_tokens(f"The item {term[:2]}\n{term[2:]} is mistranslated.")
        self.assertEqual(broken, tokens)
        self.assertEqual(document_release.table_tokens("total\u200bscore total\u2060score"),
                         ["totalscore", "totalscore"])

    def test_a_right_to_left_run_wrapped_in_bidi_controls_stays_its_own_word(self) -> None:
        # pdftotext wraps a right-to-left run in an embedding and writes no space after it. The
        # controls are format characters, but deleting them would glue the Hebrew word to 'is'.
        hebrew = "\u05e9\u05dc\u05d5\u05dd \u05e2\u05d5\u05dc\u05dd"
        claim = f"The Hebrew label {hebrew} is untranslated."
        printed = f"The Hebrew label \u202b{hebrew}\u202cis untranslated."
        expected = document_release.table_tokens(claim)
        self.assertEqual(expected, ["the", "hebrew", "label", *hebrew.split(), "is", "untranslated"])
        self.assertEqual(document_release.table_tokens(printed), expected)
        for control in ("\u200f", "\u2067", "\u2069", "\u202e"):
            self.assertEqual(document_release.table_tokens(f"{hebrew}{control}is"), [*hebrew.split(), "is"])
        # Negative control: an ordinary zero-width format character still joins its neighbours.
        self.assertEqual(document_release.table_tokens("total\u200bscore"), ["totalscore"])


class StaleManifestRemedyTest(unittest.TestCase):
    """A manifest written by an older builder is refused with its rebuild named."""

    @unittest.skipUnless(POPPLER, "Poppler exact-PDF tools are required")
    def test_the_code_identity_refusal_names_the_rebuild(self) -> None:
        from tests.test_document_release import audit_fixture
        with tempfile.TemporaryDirectory(prefix="i4r-stale-manifest-") as name:
            root = Path(name)
            audit, pdf = audit_fixture(root, body_pages=2, back_pages=1)
            older = root / "older/build_docx.sh"
            older.parent.mkdir()
            older.write_bytes(BUILDER.read_bytes() + b"# an older builder\n")
            command = [
                sys.executable, str(HELPER), "create-manifest", "--audit", str(audit),
                "--report", str(audit / "report/REPORT_final.md"),
                "--front-matter", str(audit / "report/front_matter.md"),
                "--captions", str(audit / "report/captions.txt"),
                "--pdf", str(pdf), "--pdf-relative", pdf.relative_to(audit).as_posix(),
                "--builder", str(older), "--cache-helper", str(SCRIPTS / "document_build_cache.py"),
                "--pandoc-identity", "test-pandoc", "--renderer-identity", "test-libreoffice",
                "--pdfinfo-identity", "test-pdfinfo", "--pdftotext-identity", "test-pdftotext",
                "--build-kind", "canonical", "--cap", "10",
                "--manifest-out", str(audit / "document_build_manifest.json"),
                "--diagnostic-out", str(audit / "document_build_diagnostics/last_failure.json"),
            ]
            created = subprocess.run(command, text=True, capture_output=True, timeout=60)
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
            refused = subprocess.run(
                [sys.executable, str(HELPER), "verify", str(audit), "--builder", str(BUILDER),
                 "--boundary", "stale-test"], text=True, capture_output=True, timeout=60)
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn("builder code identity differs from the release verifier", refused.stderr)
            self.assertIn("rebuild with scripts/build_docx.sh <audit-dir> comment", refused.stderr)
            self.assertIn("Appendix A rendering and document manifests", refused.stderr)
            self.assertNotIn(str(root), refused.stderr)


ABSTRACT = ("This synthetic audit checks that the findings table renders as literal text in "
            "columns wide enough for every status and ends with eight distinct closing words here")


def build_fixture(root: Path, claims: list[str], *, sections: list[str] | None = None) -> Path:
    audit = root / "audit"
    report = audit / "report"
    report.mkdir(parents=True)
    other = ("\n## Appendix B. Deposited files\n\n| File | Role | Issue |\n|---|---|---|\n"
             "| a_b.csv | data | *none* |\n")
    body = (
        "# Independent audit\n\n## Abstract\n\n" + ABSTRACT + "\n\n## 1. Findings\n\n"
        "The findings are listed in Appendix A.\n\n## Data and Code Availability\n\n"
        "Sanitized availability statement.\n\n"
    )
    rows = finding_rows(claims)
    for row, section in zip(rows, sections or []):
        row["report_section"] = section
    rendered = GENERATOR.render(rows, rows)
    text = GENERATOR.inject_report(body, rendered["appendix"], rendered["claim_tags"]) + other
    (report / "REPORT_final.md").write_text(text, encoding="utf-8")
    (report / "front_matter.md").write_text("Independent audit\n\n", encoding="utf-8")
    (report / "captions.txt").write_text("Summary of findings\nDeposited files\n", encoding="utf-8")
    policy_snapshot(audit)
    return audit


def render_a4(docx: Path, directory: Path) -> Path:
    """Re-render a built DOCX on A4 with 1in margins, the narrowest page the widths allow for."""
    directory.mkdir(parents=True)
    with zipfile.ZipFile(docx) as source:
        blobs = {name: source.read(name) for name in source.namelist()}
    document = blobs["word/document.xml"].decode("utf-8")
    if "<w:sectPr>" not in document:
        raise AssertionError("the built DOCX has no section properties")
    document = document.replace("<w:sectPr>", (
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" '
        'w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>'), 1)
    blobs["word/document.xml"] = document.encode("utf-8")
    target = directory / "a4.docx"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as output:
        for name, data in blobs.items():
            output.writestr(name, data)
    profile = tempfile.mkdtemp(prefix="i4r-lo-profile-")
    try:
        subprocess.run([SOFFICE, "--headless", f"-env:UserInstallation=file://{profile}",
                        "--convert-to", "pdf", "--outdir", str(directory), str(target)],
                       capture_output=True, timeout=300, check=True)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return directory / "a4.pdf"


def write_executable(path: Path, source: str) -> None:
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    path.chmod(0o755)


class BuildWiringTest(unittest.TestCase):
    """build_docx.sh with fake converters: the converter input and the fail-closed stage."""

    def test_converter_input_is_rewritten_and_a_table_less_docx_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-appendix-wiring-") as name:
            root = Path(name)
            audit = build_fixture(root, MARKDOWN_HEAVY)
            source = audit / "report/REPORT_final.md"
            before = source.read_bytes()
            fake = root / "bin"
            fake.mkdir()
            capture = root / "converter-input.md"
            write_executable(fake / "pandoc", f'''
                #!{sys.executable}
                import pathlib, shutil, sys, zipfile
                args = sys.argv[1:]
                if args == ["--version"]:
                    print("pandoc fake-1.0"); raise SystemExit(0)
                if args[:2] == ["--print-default-data-file", "reference.docx"]:
                    sys.stdout.buffer.write(b"fake-reference"); raise SystemExit(0)
                shutil.copyfile(args[0], {str(capture)!r})
                out = pathlib.Path(args[args.index("-o") + 1])
                with zipfile.ZipFile(out, "w") as archive:
                    archive.writestr("word/document.xml",
                        '<w:document xmlns:w="{WORDML_NS}"><w:body><w:p/></w:body></w:document>')
                    archive.writestr("word/_rels/document.xml.rels", "<Relationships/>")
            ''')
            write_executable(fake / "soffice", f'''
                #!{sys.executable}
                import pathlib, sys
                args = sys.argv[1:]
                if args == ["--version"]:
                    print("LibreOffice fake-1.0"); raise SystemExit(0)
                outdir = pathlib.Path(args[args.index("--outdir") + 1])
                (outdir / (pathlib.Path(args[-1]).stem + ".pdf")).write_bytes(b"%PDF-1.4 fake")
            ''')
            for tool in ("pdfinfo", "pdftotext"):
                write_executable(fake / tool, f'''
                    #!/bin/sh
                    if [ "${{1:-}}" = "-v" ]; then echo "{tool} fake-1.0" >&2; exit 0; fi
                    echo "{tool} must not be reached" >&2; exit 7
                ''')
            env = dict(os.environ, PATH=str(fake) + os.pathsep + os.environ.get("PATH", ""),
                       PANDOC=str(fake / "pandoc"))
            result = subprocess.run(["bash", str(BUILDER), "--no-cache", str(audit), "comment"],
                                    text=True, capture_output=True, env=env, timeout=120)
            converted = capture.read_text(encoding="utf-8").splitlines()
            self.assertIn("|" + "-" * 10 + ":|" + "-" * 85 + "|" + "-" * 41 + "|:" + "-" * 29
                          + ":|" + "-" * 32 + "|", converted)
            self.assertIn(r"| 1 | Two printed effect sizes exceed eta\_p2 = F\*df1/(F\*df1+df2) "
                          r"by a factor of ten. | CONFIRMED | No | Section 3 |", converted)
            self.assertFalse(any(line.startswith("|---:|" + "-" * 64) for line in converted))
            for line in ("| File | Role | Issue |", "|---|---|---|", "| a_b.csv | data | *none* |"):
                self.assertIn(line, converted)
            self.assertNotIn("I4R-GENERATED-CLAIM-TAGS", "\n".join(converted))
            self.assertEqual(source.read_bytes(), before, "the build changed REPORT_final.md")

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("the exact DOCX has 0 findings table(s)", result.stderr)
            diagnostic = json.loads((audit / "document_build_diagnostics/last_failure.json")
                                    .read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["stage"], "findings_table_integrity")
            self.assertFalse((audit / "document_build_manifest.json").exists())


@unittest.skipUnless(PANDOC and SOFFICE and POPPLER, "pandoc, LibreOffice and Poppler are required")
class RealRenderTest(unittest.TestCase):
    """The real pipeline: pandoc, LibreOffice and the exact PDF."""

    def test_the_rendered_appendix_is_literal_and_every_word_is_whole(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-appendix-render-") as name:
            root = Path(name)
            audit = build_fixture(root, MARKDOWN_HEAVY)
            result = subprocess.run(["bash", str(BUILDER), "--no-cache", str(audit), "comment"],
                                    text=True, capture_output=True, timeout=300,
                                    env=dict(os.environ, PANDOC=PANDOC))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            pdf = audit / "report/comment.pdf"
            docx = audit / "report/comment.docx"
            manifest = json.loads((audit / "document_build_manifest.json").read_text())
            self.assertEqual(manifest["pages"]["abstract_ending_page"], 1)
            self.assertEqual(manifest["pages"]["body_pages"], 1)

            first = manifest["pages"]["first_back_matter_page"]
            # Reading order keeps each cell's lines together; with whitespace removed, a literal
            # that wrapped at a space or after a slash reads back whole.
            reading = subprocess.run(["pdftotext", "-f", str(first), str(pdf), "-"],
                                     text=True, capture_output=True, check=True).stdout
            squeezed = re.sub(r"\s+", "", reading)
            for literal in ("F*df1/(F*df1+df2)", "a<b and c>d", "$x$", "_flag_", "`lm(y ~ x)`",
                            "data\\raw\\items.sav", "x^2^", "CO~2~", "[t](u)", "@k", "&amp;",
                            "a|b", "'x' and 'y ' (the"):
                self.assertIn(re.sub(r"\s+", "", literal), squeezed, literal)
            text = subprocess.run(["pdftotext", "-f", str(first), "-layout", str(pdf), "-"],
                                  text=True, capture_output=True, check=True).stdout
            tokens = re.findall(r"[A-Za-z0-9?]+", text)
            for fragment in ("CONFIR", "SPECU", "LATIVE", "Sectio", "publishe", "conclusi"):
                self.assertNotIn(fragment, tokens)
            for whole in ("CONFIRMED", "LIKELY", "SPECULATIVE", "Section", "published",
                          "conclusion?", "10", "11", "12", "13"):
                self.assertIn(whole, tokens)

            with zipfile.ZipFile(docx) as archive:
                document = archive.read("word/document.xml").decode("utf-8")
            table = next(chunk for chunk in re.findall(r"<w:tbl>.*?</w:tbl>", document, re.S)
                         if "Changes a published conclusion?" in chunk)
            cells = [smart_plain(html.unescape("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", cell))))
                     for cell in re.findall(r"<w:tc>.*?</w:tc>", table, re.S)]
            findings = cells[len(HEADER) + 1::len(HEADER)]
            self.assertEqual(findings, MARKDOWN_HEAVY)
            self.assertIn("findings table integrity: 1 table(s), 13 row(s)", result.stdout)

    def test_bytes_restored_from_the_cache_are_checked_too(self) -> None:
        # The real pandoc, unwrapped: it stamps the current time into the reference document it
        # prints, and before build_docx.sh pinned SOURCE_DATE_EPOCH for that print the cache key
        # changed on every build, so the second build below could never hit.
        with tempfile.TemporaryDirectory(prefix="i4r-appendix-cache-") as name:
            root = Path(name)
            audit = build_fixture(root, MARKDOWN_HEAVY[:3])
            command = ["bash", str(BUILDER), str(audit), "comment"]
            environment = dict(os.environ, PANDOC=PANDOC)
            environment.pop("SOURCE_DATE_EPOCH", None)
            started = time.monotonic()
            stored = subprocess.run(command, text=True, capture_output=True, timeout=300,
                                    env=environment)
            self.assertEqual(stored.returncode, 0, stored.stdout + stored.stderr)
            self.assertIn("document cache stored:", stored.stdout)
            # Zip timestamps have a two-second resolution; make sure the clock has moved on.
            time.sleep(max(0.0, 2.2 - (time.monotonic() - started)))
            restored = subprocess.run(command, text=True, capture_output=True, timeout=300,
                                      env=environment)
            self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
            self.assertIn("document cache hit:", restored.stdout)
            self.assertEqual(len(list((audit / ".cache/document-builds").glob("*.zip"))), 1)
            hit = restored.stdout.index("document cache hit:")
            check = restored.stdout.find("findings table integrity: 1 table(s), 3 row(s)")
            self.assertGreater(check, hit, restored.stdout)
            self.assertLess(check, restored.stdout.index("document manifest v3:"))

    def test_section_cells_stay_on_one_line_on_letter_and_a4(self) -> None:
        claims = [f"Finding {number} is short." for number in range(1, 13)]
        sections = ["Section 3", "Section 4", "Section 9.1", "Section 4.10", "Section 5",
                    "Section 12", "Section 7", "Section 10", "Section 11", "Section 10.2",
                    "Section 8.3", "Section 10.2"]
        with tempfile.TemporaryDirectory(prefix="i4r-appendix-sections-") as name:
            root = Path(name)
            audit = build_fixture(root, claims, sections=sections)
            result = subprocess.run(["bash", str(BUILDER), "--no-cache", str(audit), "comment"],
                                    text=True, capture_output=True, timeout=300,
                                    env=dict(os.environ, PANDOC=PANDOC))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            letter = audit / "report/comment.pdf"
            a4 = render_a4(audit / "report/comment.docx", root / "a4")
            for label, pdf in (("Letter", letter), ("A4", a4)):
                size = subprocess.run(["pdfinfo", str(pdf)], text=True, capture_output=True,
                                      check=True).stdout
                self.assertIn("(letter)" if label == "Letter" else "(A4)", size)
                text = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], text=True,
                                      capture_output=True, check=True).stdout
                table = text[text.index("Appendix A"):text.index("Appendix B")]
                for section in sorted(set(sections)):
                    printed = len(re.findall(re.escape(section) + r"(?![\d.])", table))
                    self.assertEqual(printed, sections.count(section),
                                     f"{label}: {section!r} is not on one line")
                self.assertEqual(len(re.findall(r"\bSPECULATIVE\b", table)), 4, label)
                self.assertIn("conclusion?", table, label)

    def test_invisible_and_unspaced_characters_pass_where_they_break(self) -> None:
        term = "\u8fd0\u52a8\u52a8\u673a\u91cf\u8868"
        claims = ([f"{'x' * pad} The name totalscore\u200bcomposite is unlabelled." for pad in range(4, 13)]
                  + [f"{'x' * pad} The name totalscore\u00adcomposite is unlabelled." for pad in range(4, 13)]
                  + [f"{'x' * pad} The questionnaire item named {term} is mistranslated." for pad in range(1, 7)]
                  + ["The column name total\u2060score holds the sum of the items."])
        with tempfile.TemporaryDirectory(prefix="i4r-appendix-format-render-") as name:
            root = Path(name)
            audit = build_fixture(root, claims)
            result = subprocess.run(["bash", str(BUILDER), "--no-cache", str(audit), "comment"],
                                    text=True, capture_output=True, timeout=300,
                                    env=dict(os.environ, PANDOC=PANDOC))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"findings table integrity: 1 table(s), {len(claims)} row(s)", result.stdout)
            text = subprocess.run(["pdftotext", "-layout", str(audit / "report/comment.pdf"), "-"],
                                  text=True, capture_output=True, check=True).stdout
            lines = [line.strip() for line in text.splitlines()]
            # The fixture exercises every path: each kind of word prints whole and broken. A
            # broken first line is followed, on the same layout line, by the row's status.
            status = r"\s{2,}(?:CONFIRMED|LIKELY|SPECULATIVE)\b"
            self.assertTrue(any("totalscorecomposite" in line for line in lines))
            self.assertTrue(any(re.search(r"\btotalscore" + status, line) for line in lines),
                            "no row broke at its zero-width space")
            self.assertTrue(any(re.search(r"\btotalscore-" + status, line) for line in lines),
                            "no row broke at its soft hyphen")
            self.assertTrue(any(term in line for line in lines))
            self.assertTrue(any(term[:2] in line and term not in line for line in lines),
                            "no row broke between two ideographs")
            self.assertTrue(any("totalscore holds" in line for line in lines))

if __name__ == "__main__":
    unittest.main()
