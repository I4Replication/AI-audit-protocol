"""The privacy gate must scan markup for private paths and only prose for identifiers.

Two defects, both found by staging a real, clean release:

  1. `inspect_docx` concatenated the raw XML of every part -- styles, themes, relationships --
     and ran the IDENTIFIER detector over the whole blob. A clean comment tripped it on document
     machinery alone: pandoc bookmark anchors (`X` + hex, which the hex exemption misses because
     of the prefix) and the reference document's style names (`Heading1Char` ...). Any comment
     with enough sections crosses the threshold.

  2. An auditor's working script names the snapshot it read, and the snapshot sits under the
     operator's home. That file is hash-bound into the role's evidence manifest, so the
     audit-side copy cannot be edited; the release copy is rewritten instead.

Every test builds the condition on disk and runs the shipped code over it.
"""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "crp_under_test", ROOT / "scripts" / "check_release_privacy.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["crp_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


CRP = _load()

# Assembled at runtime so this test file does not itself contain a literal home path -- the
# release gate scans its own test suite, and a literal here would block every release.
HOME = "/" + "Us" + "ers/someaccount"

_DOC_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>{body}</w:body></w:document>"
)


def _para(text):
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _docx(path, body_paragraphs, *, styles_extra="", rels_target="media/image1.png"):
    """A minimal but structurally real .docx: document, styles and a relationship part."""
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        # The nine style names that tripped the gate on a clean release.
        + "".join(f'<w:style w:styleId="Heading{n}Char"><w:name w:val="Heading {n} Char"/></w:style>'
                  for n in range(1, 10))
        + styles_extra
        + "</w:styles>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="http://x/image" Target="{rels_target}"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("word/document.xml",
                         _DOC_XML.format(body="".join(_para(t) for t in body_paragraphs)))
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/_rels/document.xml.rels", rels)


class DocxIdentifierScope(unittest.TestCase):
    def test_document_machinery_is_not_a_participant_identifier(self):
        """A clean comment whose markup carries 20+ opaque-shaped names must pass."""
        with tempfile.TemporaryDirectory() as tmp:
            docx = pathlib.Path(tmp) / "comment.docx"
            # Eleven pandoc bookmark anchors, exactly as pandoc emits them.
            anchors = "".join(
                f'<w:bookmarkStart w:id="{i}" w:name="X{i:02d}ef4ec62e9529aa44220f6625401fe147cafca"/>'
                for i in range(11))
            _docx(docx,
                  ["The reported effect does not reproduce from the deposited data.",
                   "Table 2 column 3 differs from the value the script produces."],
                  styles_extra=f'<w:latentStyles>{anchors}</w:latentStyles>')
            issues = CRP.inspect_docx(docx, "comment.docx")
            self.assertEqual(
                [], issues,
                "markup names were reported as participant identifiers: "
                + "; ".join(f"{i.category}:{i.detail}" for i in issues))

    def test_identifiers_in_visible_text_are_still_caught(self):
        """Negative control for defect 1: narrowing the scan must not disarm it."""
        with tempfile.TemporaryDirectory() as tmp:
            docx = pathlib.Path(tmp) / "leaky.docx"
            rows = [f"Row {i}: token Ab{i:03d}Xy7Qz9Kw participated." for i in range(30)]
            _docx(docx, rows)
            issues = CRP.inspect_docx(docx, "leaky.docx")
            self.assertTrue(
                any(i.category != "private_path" for i in issues),
                "an opaque per-row identifier listing in the visible text was not reported")

    def test_private_path_hidden_in_a_relationship_attribute_is_still_caught(self):
        """Negative control for defect 1: the PATH scan must keep reading raw markup.

        A relationship Target is never visible text, so a visible-text-only scan would miss it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            docx = pathlib.Path(tmp) / "linked.docx"
            _docx(docx, ["Nothing to see in the prose."],
                  rels_target=f"file:///{HOME}/Desktop/notes.docx")
            issues = CRP.inspect_docx(docx, "linked.docx")
            self.assertTrue(
                any(i.category == "private_path" for i in issues),
                "a private path in a relationship attribute was not reported")


class Base64IsNotAnIdentifier(unittest.TestCase):
    """A rendered figure in a staged notebook is ~1170 base64 tokens of exactly _OPAQUE's shape."""

    def _png_b64(self):
        import base64
        return base64.b64encode(bytes(range(256)) * 160).decode()

    def test_notebook_with_a_rendered_figure_passes(self):
        nb = {"cells": [{"cell_type": "code", "source": ["plt.scatter(x, y)"],
                         "outputs": [{"data": {"image/png": self._png_b64()}}]}]}
        issues = CRP.inspect_text(json.dumps(nb, indent=1), "work/figure.ipynb", identifiers=True)
        self.assertEqual([], [i for i in issues if i.category == "opaque_identifier_listing"], issues)

    def test_markdown_with_an_inline_data_uri_passes(self):
        md = "![fig](data:image/png;base64," + self._png_b64() + ")\n"
        issues = CRP.inspect_text(md, "report/x.md", identifiers=True)
        self.assertEqual([], issues)

    def test_a_real_listing_beside_a_figure_is_still_caught(self):
        """Negative control: stripping base64 must not disarm the detector."""
        rows = "\n".join(f"Row {i}: token Ab{i:03d}Xy7Qz9Kw participated." for i in range(30))
        text = rows + "\n![fig](data:image/png;base64," + self._png_b64() + ")\n"
        issues = CRP.inspect_text(text, "report/leak.md", identifiers=True)
        self.assertTrue(any(i.category == "opaque_identifier_listing" for i in issues), issues)


class DocxTablesKeepTheirRows(unittest.TestCase):
    """The identifier scan on a DOCX must see the reader's line structure."""

    def test_a_participant_table_in_a_docx_is_caught_like_markdown(self):
        rows = "".join(
            f"<w:tr><w:tc><w:p><w:r><w:t>participant {i}</w:t></w:r></w:p></w:tc>"
            f"<w:tc><w:p><w:r><w:t>12:0{i % 10}:00</w:t></w:r></w:p></w:tc></w:tr>" for i in range(1, 26))
        with tempfile.TemporaryDirectory() as tmp:
            docx = pathlib.Path(tmp) / "table.docx"
            body = _para("Appendix B lists the sessions.") + f"<w:tbl>{rows}</w:tbl>"
            with zipfile.ZipFile(docx, "w") as archive:
                archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
                archive.writestr("word/document.xml", _DOC_XML.format(body=body))
            issues = CRP.inspect_docx(docx, "table.docx")
            kinds = {i.category for i in issues}
            self.assertIn("identifier_timestamp", kinds, issues)
            # The same rows as Markdown lines give the same verdict.
            md = "\n".join(f"participant {i} | 12:0{i % 10}:00" for i in range(1, 26))
            self.assertIn("identifier_timestamp", {i.category for i in CRP.inspect_text(md, "x.md", identifiers=True)})


class FictitiousAccountsArePlaceholders(unittest.TestCase):
    """A detector's own synthetic control string is not a leak.

    An auditor's privacy scanner carried `path /Users/janedoe/data.csv` beside a fake email and a
    fake SSN as its known-present self-test, and that one line blocked a clean release.
    """

    def test_a_self_test_fixture_line_passes(self):
        line = ('known = "Contact Jane Doe at jane.doe@example.com, SSN 123-45-6789, path /'
                + 'Users/janedoe/data.csv"\n')
        self.assertEqual([], CRP.inspect_text(line, "scripts/analysis.py"))
        for name in ("johndoe", "jdoe", "testuser", "tester", "alice"):
            self.assertEqual([], CRP.inspect_text("x = '/" + f"Users/{name}/f.csv'", "x"), name)

    def test_a_real_looking_account_still_blocks(self):
        """Negative control: the exemption is a fixed list, not a pattern over names."""
        for name in ("mquentin", "jsmith", "ltanaka", "janedoe2", "testerman", "atester"):
            issues = CRP.inspect_text("x = '/" + f"Users/{name}/f.csv'", "x")
            self.assertTrue(any(i.category == "private_path" for i in issues), name)


class AuditPathNeutralisation(unittest.TestCase):
    """Only the audit's own root is rewritten. Everything else still blocks the release."""

    def _release(self, tmp):
        release = pathlib.Path(tmp) / "release"
        (release / "work" / "a1_reproduction").mkdir(parents=True)
        return release

    def test_snapshot_path_is_rewritten_and_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_root = f"{HOME}/Dropbox/Journal/Paper/audit"
            release = self._release(tmp)
            script = release / "work" / "a1_reproduction" / "reproduction_analysis.R"
            script.write_text(
                "library(haven)\n"
                f'RAW_PKG <- "{audit_root}/phase1/snapshots/abc123/raw/pkg"\n'
                'd <- read_sav(file.path(RAW_PKG, "data.sav"))\n')

            rewritten = CRP.neutralise_tree(release / "work", {audit_root})
            note = CRP.write_neutralisation_note(release, rewritten)

            out = script.read_text()
            self.assertNotIn(HOME, out)
            self.assertIn(CRP.AUDIT_PLACEHOLDER, out)
            # Everything but the audit root survives: the reader can still see WHICH snapshot.
            self.assertIn("phase1/snapshots/abc123/raw/pkg", out)
            self.assertIn("library(haven)", out)
            # And the file now passes the gate that blocked the release.
            self.assertEqual([], CRP.inspect_text(out, "reproduction_analysis.R"))
            # The substitution is disclosed, not silent.
            self.assertIsNotNone(note)
            self.assertEqual(CRP.NEUTRALISATION_NOTE, note.name)
            self.assertIn("a1_reproduction/reproduction_analysis.R", note.read_text())

    def test_a_split_literal_naming_the_tree_root_is_rewritten(self):
        """2.6.2: an auditor split the audit path across string literals, so only the tree root
        (ours) appeared whole; the release blocked on the operator's own account name."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = f"{HOME}/Dropbox/Sport_exercise_psych"
            release = self._release(tmp)
            script = release / "work" / "a1_reproduction" / "checks.py"
            script.write_text('RAW = Path(\n    "' + tree + '/runs/"\n    "Journal/Paper/"\n    "audit/phase1/snapshots/abc/raw"\n)\n'
                              'OTHER = "' + tree + '2/elsewhere.csv"\n')
            CRP.neutralise_tree(release / "work", {f"{tree}/runs/Journal/Paper/audit"}, {tree})
            out = script.read_text()
            self.assertIn('"<tree>/runs/"', out)
            self.assertNotIn(f'"{tree}/runs/"', out)
            # a sibling of the tree root is not the tree root: it still blocks
            self.assertIn(f"{tree}2/elsewhere.csv", out)
            self.assertTrue(any(i.category == "private_path" for i in CRP.inspect_text(out, "checks.py")))

    def test_a_path_outside_the_audit_is_left_to_fail(self):
        """Negative control, and the whole point of the narrowing.

        A script reading from somebody's private directory is a leak the gate must still catch.
        Rewriting it would hide that the auditor read from outside the snapshot at all.
        """
        with tempfile.TemporaryDirectory() as tmp:
            audit_root = f"{HOME}/Dropbox/Journal/Paper/audit"
            release = self._release(tmp)
            script = release / "work" / "reproduce.sas"
            elsewhere = "/" + "home/realperson/private/input"
            script.write_text(f"%include '{elsewhere}';\n")

            self.assertEqual([], CRP.neutralise_tree(release / "work", {audit_root}))
            self.assertEqual(f"%include '{elsewhere}';\n", script.read_text())
            self.assertTrue(
                [i for i in CRP.inspect_text(script.read_text(), "reproduce.sas")
                 if i.category == "private_path"],
                "a private path outside the audit was neither rewritten nor reported")

    def test_a_cp1252_crlf_script_is_rewritten_and_keeps_its_bytes_otherwise(self):
        """The neutraliser must read what the detector reads: latin-1 fallback, bytes out."""
        with tempfile.TemporaryDirectory() as tmp:
            audit_root = f"{HOME}/Dropbox/Journal/Paper/audit"
            release = self._release(tmp)
            script = release / "work" / "analysis.R"
            body = ("# Análisis — comentario acentuado\r\n"
                    f'setwd("{audit_root}/phase1/snapshots/abc/raw/pkg")\r\n'
                    'd <- read.csv("data.csv")\r\n')
            script.write_bytes(body.encode("cp1252"))
            rewritten = CRP.neutralise_tree(release / "work", {audit_root})
            self.assertEqual([("analysis.R", 1)], rewritten)
            out = script.read_bytes()
            self.assertIn(b"\r\n", out, "line endings were rewritten")
            self.assertNotIn(HOME.encode("latin-1"), out)
            # and the detector, reading the same bytes its own way, is now satisfied
            self.assertEqual([], CRP.inspect_text_bytes(out, "analysis.R"))

    def test_an_nfd_spelled_audit_root_is_rewritten(self):
        import unicodedata
        with tempfile.TemporaryDirectory() as tmp:
            audit_root = f"{HOME}/Dropbox/Écoles/Paper/audit"
            release = self._release(tmp)
            script = release / "work" / "a.py"
            script.write_text(f'ROOT = "{unicodedata.normalize("NFD", audit_root)}/pkg"\n', encoding="utf-8")
            self.assertEqual([("a.py", 1)], CRP.neutralise_tree(release / "work", {audit_root}))
            self.assertIn(CRP.AUDIT_PLACEHOLDER, script.read_text(encoding="utf-8"))

    def test_relative_paths_and_urls_are_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = self._release(tmp)
            keep = 'd <- read.csv("data/users/alice.csv")\nu <- "https://example.org/users/profile"\n'
            script = release / "work" / "clean.R"
            script.write_text(keep)
            self.assertEqual([], CRP.neutralise_tree(release / "work",
                                                     {f"{HOME}/Dropbox/Journal/Paper/audit"}))
            self.assertEqual(keep, script.read_text())
            self.assertIsNone(CRP.write_neutralisation_note(release, []))
            self.assertFalse((release / CRP.NEUTRALISATION_NOTE).exists())

    def test_binary_and_non_text_files_are_not_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = self._release(tmp)
            blob = release / "work" / "fig.png"
            payload = b"\x89PNG\r\n\x1a\n" + HOME.encode() + b"\x00\xff"
            blob.write_bytes(payload)
            self.assertEqual([], CRP.neutralise_tree(release / "work", {HOME}))
            self.assertEqual(payload, blob.read_bytes())


if __name__ == "__main__":
    unittest.main()
