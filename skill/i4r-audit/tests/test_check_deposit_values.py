"""Stage check 3c (2.6.8): the audit's own outputs never reproduce a deposit's identifying values.

scripts/check_deposit_values.py hashes the values of a deposit's name, e-mail, birth-date, IP,
address and phone columns in memory under a one-run key, then refuses any release-bound text, PDF
or DOCX location that reproduces a multi-word name or address, a date of birth, an e-mail, IP or
phone value, and any e-mail, IPv4 or IPv6 address at all. A one-word match only warns. Every value
below is synthetic and out of range: invented names, birth dates in 1901 and 1902, reserved domains,
the reserved 240.0.0.0/4 block and the returned 3ffe::/16 block, so no control can be anyone's. The
generic e-mail and IP positives are assembled at run time, so this file never carries one literally.
The three real-looking dates in the version-string test are the ones the 2.6.8 review found inside
fixed strings (the XMP RDF namespace, Python 3.12.12, bash 3.2.57); a date alone identifies no one.

The ReviewDefectTests class holds one regression test per defect of the 2.6.8 review of this gate;
each fails on the pre-review candidate. The last classes drive the real stage_release.sh and
publish_completed.sh on the release-pipeline fixture (tests/test_release_pipeline.make_audit) with
a planted personal-data deposit.
"""
from __future__ import annotations

import collections
import gzip
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from typing import Any
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
import check_deposit_values as cdv  # noqa: E402

CHECK = SCRIPTS / "check_deposit_values.py"
AT = "@"
FULL_NAME = "Qorvelline Xandrithow"
SURNAME = "Brummelstedtova"
FIRST, LAST = "Ilkavette", "Wendrasshausen"
DEPOSIT_EMAIL = "qorvelline.x" + AT + "nowhere.invalid"
BIRTH_DMY, BIRTH_ISO, BIRTH_WORDS = "03.04.1901", "1901-04-03", "3 April 1901"
DEPOSIT_IP = ".".join(["240", "77", "78", "79"])
PHONE = "+49 30 9999 1901"
PLANTED = [FULL_NAME, SURNAME, FIRST, LAST, DEPOSIT_EMAIL, BIRTH_DMY, BIRTH_ISO, DEPOSIT_IP, PHONE,
           "Xandrithow", "Qorvelline"]
VALUE_FREE = (
    "The file data/survey.csv holds a column Name with the full names of 4 participants, an "
    "E-Mail column with 1 address, a Geburtsdatum column with 1 date of birth, an IPAddress "
    "column and a Telefon column; no anonymisation was claimed. Remedy: drop the five columns "
    "and replace the file in the repository.\n"
)
HAVE_PDF = shutil.which("pdftotext") is not None and shutil.which("pdfinfo") is not None
HAVE_R = bool(cdv.r_capability()["rscript"] and cdv.r_capability()["haven"])


def write(path: pathlib.Path, content: str | bytes) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def pdf_with_info(text: str, author: str) -> bytes:
    """A one-page PDF whose Info dictionary names an author (pdfinfo prints it)."""
    return cdv._minimal_pdf(text, info={"Author": author, "Title": "Comment"})


def listing(root: pathlib.Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


class DepositFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-deposit-values-test-")
        self.addCleanup(self.temporary.cleanup)
        base = pathlib.Path(self.temporary.name)
        self.base = base
        self.audit, self.release = base / "audit", base / "release"
        for directory in ("pkg/data", "report", "paper", "work"):
            (self.audit / directory).mkdir(parents=True)
        self.release.mkdir()
        write(self.audit / "pkg/data/survey.csv",
              "Name,E-Mail,Geburtsdatum,IPAddress,Telefon,score,study_name\n"
              f"{FULL_NAME},{DEPOSIT_EMAIL},{BIRTH_DMY},{DEPOSIT_IP},{PHONE},3,Placebo Arm\n"
              f"{SURNAME},,,,,4,Placebo Arm\n"
              "Young,,,,,5,Placebo Arm\n"
              "Respondent 1917,,,,,6,Placebo Arm\n")
        write(self.audit / "pkg/data/roster.csv", f"Vorname,Nachname,Gruppe\n{FIRST},{LAST},A\n")
        self.front_email = "corresponding.author" + AT + "uni-somewhere.edu"
        write(self.audit / "paper/paper.txt",
              "A synthetic article\nAnna Authorsen and Synthetic Author\n"
              "Correspondence: anna.authorsen" + AT + "uni-somewhere.edu\n\f"
              "Second page. The young athletes trained daily.\n")
        write(self.audit / "report/front_matter.md",
              '::: {custom-style="Authors"}\nSynthetic Commenter\n:::\n'
              "\\* Correspondence: Synthetic Commenter, " + self.front_email + "\n")

    def run_check(self, *extra: str, env: dict[str, str] | None = None
                  ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CHECK), str(self.audit), str(self.release), *extra],
            text=True, capture_output=True, check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})})

    def run_json(self, **kwargs: Any) -> tuple[subprocess.CompletedProcess[str], dict]:
        summary = self.base / "summary.json"
        if summary.exists():
            summary.unlink()
        result = self.run_check("--json", str(summary), **kwargs)
        data = json.loads(summary.read_text()) if summary.is_file() else {}
        return result, data

    @staticmethod
    def verdict(summary: dict, relative: str) -> str:
        prefix = relative + " "
        if any(item["location"].startswith(prefix) or item["location"] == relative
               for item in summary.get("hits", [])):
            return "refuse"
        if any(item["location"].startswith(prefix) for item in summary.get("warnings", [])):
            return "warn"
        return "pass"

    def assert_located(self, result: subprocess.CompletedProcess[str], relative: str) -> None:
        self.assertTrue(any(line.strip().startswith(relative + " ") for line in result.stdout.splitlines()),
                        f"{relative} not reported:\n{result.stdout}")

    def assert_nothing_printed(self, result: subprocess.CompletedProcess[str], *more: str) -> None:
        printed = (result.stdout + result.stderr).casefold()
        for value in [*PLANTED, *more]:
            self.assertNotIn(value.casefold(), printed)


class HeaderClassificationTests(unittest.TestCase):
    def test_personal_data_headers_and_look_alikes(self) -> None:
        positives = {
            "Name": "name", "full_name": "name", "Vorname": "name", "Nachname": "name",
            "RecipientLastName": "name", "first name": "name", "Surname": "name",
            "E-Mail": "email", "email_address": "email", "Mailadresse": "email",
            "Geburtsdatum": "birthdate", "DOB": "birthdate", "date_of_birth": "birthdate",
            "IPAddress": "ip", "ip": "ip", "Adresse": "address", "street_address": "address",
            "Telefon": "phone", "phone_number": "phone", "mobile_no": "phone",
        }
        for header, cls in positives.items():
            with self.subTest(header=header):
                self.assertEqual((cdv.classify_header(header) or ("",))[0], cls)
        for header in ("study_name", "file_name", "team name", "IPAQ_total", "tip", "zip",
                       "score", "session", "mailing_wave", "nom_etude", "nombre_de_sesiones"):
            with self.subTest(header=header):
                self.assertIsNone(cdv.classify_header(header))
        self.assertEqual(cdv.classify_column("v12", "What is your name?"), ("name", "full"))


class CollectionAndMatchingTests(DepositFixture):
    def test_nothing_value_derived_is_written_and_every_run_rebuilds(self) -> None:
        write(self.release / "report/clean.md", VALUE_FREE)
        before = listing(self.audit)
        first, summary = self.run_json()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(listing(self.audit), before, "the check wrote into the audit")
        self.assertIn("hashed in memory under a one-run key", first.stdout)
        text = json.dumps(summary).casefold()
        for value in PLANTED:
            self.assertNotIn(value.casefold(), text)
        self.assertEqual(summary["deposit"]["pii_columns"],
                         {"birthdate": 1, "email": 1, "ip": 1, "name": 3, "phone": 1})
        write(self.audit / "pkg/data/extra.csv", "Name\nZorvanquell Ixbretta\n")
        again, summary = self.run_json()
        self.assertEqual(summary["deposit"]["pii_columns"]["name"], 4)

    def test_leaked_values_are_refused_by_location_and_never_printed(self) -> None:
        refusals = {
            "report/full.md": f"One row reads {FULL_NAME.upper()}.\n",
            # The whole-cell "First Last" value quoted surname first, with and without a comma.
            "report/full_reversed.md": "One row reads Xandrithow Qorvelline.\n",
            "report/full_reversed_comma.md": "One row reads XANDRITHOW, Qorvelline.\n",
            "report/pair.md": f"Listed as {LAST}, {FIRST}.\n",
            "report/pair_order.md": f"Listed as {FIRST} {LAST}.\n",
            "report/dob.md": f"Born {BIRTH_WORDS}.\n",
            "report/dob_iso.md": f"Born {BIRTH_ISO}.\n",
            "report/ip.md": f"Logged from {DEPOSIT_IP}.\n",
            "report/phone.md": f"Call {PHONE.replace(' ', '-')}.\n",
            "comment.json": json.dumps({"observed": f"row carries {DEPOSIT_EMAIL}"}) + "\n",
            "verification.Rmd": f"# observed {DEPOSIT_EMAIL.upper()}\n",
        }
        warnings = {
            "report/part.md": "Mr Xandrithow declined.\n",
            "report/single.md": f"The name {SURNAME} appears.\n",
        }
        for relative, text in {**refusals, **warnings}.items():
            write(self.release / relative, text)
        result, summary = self.run_json()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for relative in refusals:
            with self.subTest(relative=relative):
                self.assertEqual(self.verdict(summary, relative), "refuse", result.stdout)
        for relative in warnings:
            with self.subTest(relative=relative):
                self.assertEqual(self.verdict(summary, relative), "warn", result.stdout)
        self.assertIn("matches deposit column 'Name' in data/survey.csv", result.stdout)
        self.assertIn("reword the finding to describe the identifier by kind, file, column and count",
                      result.stdout)
        self.assert_nothing_printed(result)

    def test_value_free_disclosure_description_passes(self) -> None:
        write(self.release / "report/REPORT_final.md", VALUE_FREE)
        write(self.release / "LEDGER.md", "| finding 3 | data/survey.csv, column Name: 4 participant names |\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("personal-data columns: birthdate 1, email 1, ip 1, name 3, phone 1",
                      result.stdout)

    def test_one_word_values_follow_the_conservative_rule(self) -> None:
        write(self.audit / "pkg/data/more.csv",
              "Name\nGrace\nWalker\nVrok\nZorvanquell\nTrainingsgruppe\n")
        write(self.audit / "paper/paper.txt",
              (self.audit / "paper/paper.txt").read_text() + "Each trainingsgruppe met twice.\n")
        cases = {
            # ordinary English words in a name column never count
            "report/ordinary.md": ("Grace was shown to young athletes; Walker scored.\n", "pass"),
            # four letters is below the five-character floor
            "report/short.md": ("Vrok answered.\n", "pass"),
            # a one-word value in lower case is prose, not a proper noun
            "report/lower.md": ("the zorvanquell condition\n", "pass"),
            # a word the paper writes in lower case is the paper's vocabulary
            "report/paper_word.md": ("Trainingsgruppe sizes differed.\n", "pass"),
            # a capitalised surname that is no English word warns, and never refuses
            "report/surname.md": ("Zorvanquell withdrew.\n", "warn"),
        }
        for relative, (text, _expected) in cases.items():
            write(self.release / relative, text)
        result, summary = self.run_json()
        self.assertEqual(result.returncode, 0, result.stdout)
        for relative, (_text, expected) in cases.items():
            with self.subTest(relative=relative):
                self.assertEqual(self.verdict(summary, relative), expected, result.stdout)
        self.assert_nothing_printed(result, "Zorvanquell")

    def test_dictionary_and_identifier_name_columns_are_dismissed(self) -> None:
        write(self.audit / "pkg/data/definition.csv",
              "type,name,label\ntext,Vendelquistor,Question one\ntext,consent_given,Consent\n")
        write(self.audit / "pkg/data/items.csv", "name,score\nitem_1,1\nitem_2,2\nHarrowmickle,3\n")
        write(self.release / "report/vars.md",
              "The variables Vendelquistor and Harrowmickle are described.\n")
        write(self.release / "report/person.md", f"But {SURNAME} is a person.\n")
        result, summary = self.run_json()
        self.assertEqual(self.verdict(summary, "report/vars.md"), "pass", result.stdout)
        self.assertEqual(self.verdict(summary, "report/person.md"), "warn", result.stdout)
        self.assertIn("dismissed as variable names or identifiers: name 2", result.stdout)
        self.assertEqual(summary["deposit"]["pii_columns_dismissed"], {"name": 2})
        self.assertEqual(summary["deposit"]["pii_columns"]["name"], 3)

    def test_a_mostly_pseudonymised_name_column_keeps_its_real_full_names(self) -> None:
        rows = "".join(f"Respondent {number}\n" for number in range(1, 10))
        write(self.audit / "pkg/data/pseudonyms.csv", f"Name\n{rows}Quimbarra Selvenhorst\n")
        write(self.release / "report/leftover.md", "One row still reads Quimbarra Selvenhorst.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assert_located(result, "report/leftover.md")
        self.assertIn("dismissed as variable names or identifiers: name 1", result.stdout)
        self.assert_nothing_printed(result, "Quimbarra", "Selvenhorst")

    def test_public_values_and_the_generic_e_mail_detector(self) -> None:
        contact = "a.authorsen" + AT + "old-uni.edu"
        office = "lab.office" + AT + "old-uni.edu"
        stranger = "zq.roe" + AT + "uni-" + "nowhere.edu"
        write(self.audit / "pkg/code/analysis.R", f"# contact: {contact}; data office: {office}\n")
        cases = {
            "report/byline.md": ("Anna Authorsen wrote to anna.authorsen" + AT + "uni-somewhere.edu.\n", False),
            "report/front.md": (f"Correspondence: {self.front_email}\n", False),
            "report/docs.md": ("See someone" + AT + "example.org or x" + AT + "host.test.\n", False),
            "work/author_copy.R": (f"# contact: {contact}\n", False),
            "work/model.py": ("beta = X.T" + AT + "X.values\nz = x" + AT + "W.to(device)\n", False),
            "report/office.md": (f"Data office: {office}\n", True),
            "report/stranger.md": (f"Write to {stranger}.\n", True),
        }
        for relative, (text, _positive) in cases.items():
            write(self.release / relative, text)
        result = self.run_check()
        for relative, (_text, positive) in cases.items():
            with self.subTest(relative=relative):
                found = any(line.strip().startswith(relative + " ") for line in result.stdout.splitlines())
                self.assertEqual(found, positive, result.stdout)
        self.assert_nothing_printed(result, office, stranger)

    def test_generic_ipv4_rules(self) -> None:
        reserved = ".".join(["240", "201", "202", "203"])
        cases = {
            "report/private.md": ("Router 10.200.100.50 and 192.168.4.20, documentation 192.0.2.10.\n", False),
            "report/versions.md": ("Producer: LibreOffice 24.2.4.2; IBM SPSS Statistics 29.0.1.0; "
                                   "section 2.4.1.3.\n", False),
            "run.json": ('{"toolkit": {"host_os": "Linux 5.15.153.1-microsoft-standard"}}\n', False),
            "report/public.md": (f"Logged from {reserved}.\n", True),
            "report/cued.md": ("The server at " + ".".join(["240", "2", "4", "2"]) + " answered.\n", True),
            # the deposit's own IP column is caught whatever its shape or range
            "report/deposit_ip.md": (f"Participant address {DEPOSIT_IP}.\n", True),
        }
        for relative, (text, _positive) in cases.items():
            write(self.release / relative, text)
        write(self.audit / "pkg/data/net.csv", "IPAddress\n192.0.2.99\n")
        write(self.release / "report/deposit_doc_range.md", "Seen at 192.0.2.99 once.\n")
        result = self.run_check()
        for relative, (_text, positive) in cases.items():
            with self.subTest(relative=relative):
                found = any(line.strip().startswith(relative + " ") for line in result.stdout.splitlines())
                self.assertEqual(found, positive, result.stdout)
        self.assert_located(result, "report/deposit_doc_range.md")
        self.assert_nothing_printed(result, reserved, "192.0.2.99")

    def test_unreadable_deposit_files_are_reported_and_never_refuse(self) -> None:
        write(self.audit / "pkg/data/table.parquet", b"PAR1synthetic")
        write(self.audit / "pkg/data/broken.xlsx", b"not a workbook")
        write(self.audit / "pkg/data/blob.csv", b"\x00\x01\x02binary")
        write(self.audit / "pkg/data/survey.sav", b"$FL2synthetic")
        write(self.release / "report/clean.md", VALUE_FREE)
        # Without R, an SPSS file is reported as not scanned, like any unreadable file.
        result = self.run_check(env={"I4R_DEPOSIT_VALUES_NO_R": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("4 not scanned", result.stdout)
        self.assertIn("not scanned (never a refusal): data/survey.sav -- Rscript is not available",
                      result.stdout)
        self.assertIn("unsupported data format", result.stdout)

    def test_a_reader_crash_is_reported_as_not_scanned(self) -> None:
        write(self.release / "report/clean.md", VALUE_FREE)
        lines: list[str] = []
        with mock.patch.object(cdv, "read_delimited", side_effect=KeyError(FULL_NAME)):
            status = cdv.run_check(self.audit, self.release, out=lines.append)
        printed = "\n".join(lines)
        self.assertEqual(status, 0, printed)
        self.assertIn("reader error (KeyError)", printed)
        self.assertNotIn(FULL_NAME.casefold(), printed.casefold())

    def test_containers_json_and_workbooks_are_read(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("inner/contacts.csv", "Name\nOstravinsky Quellmarr\n")
        write(self.audit / "pkg/archive.zip", buffer.getvalue())
        write(self.audit / "pkg/names.csv.gz", gzip.compress(b"Surname,Forename\nPelvratsky,Idrunella\n"))
        write(self.audit / "pkg/records.json", json.dumps(
            [{"firstName": "Quilmara", "lastName": "Dostrevna", "age": 30}]))
        write(self.audit / "pkg/roster.xlsx", cdv._minimal_xlsx(
            [["Participant name", "Group"], ["Tessaly Vornquist", "A"]]))
        leaks = {"report/zip.md": "Ostravinsky Quellmarr left.\n",
                 "report/gz.md": "Idrunella Pelvratsky left.\n",
                 "report/json.md": "Dostrevna Quilmara left.\n",
                 "report/xlsx.md": "tessaly vornquist left.\n"}
        for relative, text in leaks.items():
            write(self.release / relative, text)
        result, summary = self.run_json()
        self.assertEqual(result.returncode, 1, result.stdout)
        for relative in leaks:
            with self.subTest(relative=relative):
                self.assertEqual(self.verdict(summary, relative), "refuse", result.stdout)
        self.assert_nothing_printed(result, "Ostravinsky", "Pelvratsky", "Quilmara", "Dostrevna",
                                    "Tessaly", "Vornquist", "Quellmarr", "Idrunella")

    def test_a_value_shaped_header_is_never_printed(self) -> None:
        # A headerless table: its first row is read as the header, and that "header" is a value.
        header_value = "mail.qorvelline" + AT + "nowhere.invalid"
        second = "mail.xandrithow" + AT + "nowhere.invalid"
        write(self.audit / "pkg/data/headerless.csv", f"{header_value}\n{second}\n")
        write(self.release / "report/leak.md", f"Contact {second}.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("matches deposit column 'column 1' in data/headerless.csv", result.stdout)
        self.assert_nothing_printed(result, header_value, second)

    @unittest.skipUnless(HAVE_PDF, "pdftotext and pdfinfo are required")
    def test_pdf_text_metadata_and_docx_are_scanned(self) -> None:
        write(self.release / "report/comment.pdf", pdf_with_info("Clean text.", DEPOSIT_EMAIL))
        write(self.release / "report/text.pdf", cdv._minimal_pdf(f"Row: {FULL_NAME}"))
        write(self.release / "report/comment.docx", cdv._minimal_docx(["Clean.", f"Row {FULL_NAME}"]))
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("report/comment.pdf metadata line", result.stdout)
        self.assertIn("report/text.pdf page 1 line 1", result.stdout)
        self.assertIn("report/comment.docx paragraph 2", result.stdout)
        self.assert_nothing_printed(result)
        # A document that cannot be read is not certified, with its own status and remedy.
        write(self.release / "report/text.pdf", b"%PDF-1.4 truncated")
        broken = self.run_check()
        self.assertEqual(broken.returncode, 2, broken.stdout)
        self.assertIn("is not certified", broken.stdout)
        self.assertIn("not a value in the text", broken.stdout)
        self.assertNotIn("Remedy: reword", broken.stdout)

    def test_a_stale_value_store_is_reported_and_never_staged(self) -> None:
        write(self.release / "report/clean.md", VALUE_FREE)
        write(self.audit / "work" / cdv.STORE_NAME, '{"schema_version": "i4r-deposit-values-1"}\n')
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"work/{cdv.STORE_NAME} is a value store left by a pre-release build",
                      result.stdout)
        shutil.copyfile(self.audit / "work" / cdv.STORE_NAME, self.release / cdv.STORE_NAME)
        copied = self.run_check()
        self.assertEqual(copied.returncode, 1)
        self.assertIn("hash store from a pre-release build", copied.stdout)
        # There is no store option any more.
        self.assertEqual(self.run_check("--store", str(self.base / "x.json")).returncode, 2)

    def test_a_large_table_is_streamed_within_bounds(self) -> None:
        letters = "abcdefghijklmnopqrstuvwxyz"

        def word(number: int) -> str:
            text = ""
            for _ in range(5):
                number, rest = divmod(number, 26)
                text += letters[rest]
            return text.capitalize()

        path = self.audit / "pkg/data/big.csv"
        with path.open("w", encoding="utf-8") as handle:
            handle.write("Name,score,comment\n")
            for number in range(300_000):
                handle.write(f"Zq{word(number).lower()} Vex{word(number * 7).lower()},{number},"
                             "a comment that pads the row to realistic width\n")
        self.assertGreater(path.stat().st_size, 20 * 1024 * 1024)
        write(self.release / "report/clean.md", VALUE_FREE)
        started = time.monotonic()
        result = self.run_check()
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(elapsed, 120, f"a 20 MB table took {elapsed:.0f} s")


class ReviewDefectTests(DepositFixture):
    """One regression per defect of the 2.6.8 review of this gate (each fails before the fix)."""

    def test_boilerplate_eponyms_and_one_word_names_never_refuse(self) -> None:
        # BLOCKER (check_deposit_values.py:1691). One-word values in a name column, and a full name
        # made of words the skill's own fixed texts print: the house citation, run.json telemetry,
        # SETTLED, the skill's scripts and a statistical eponym in a critic report.
        house = (SKILL / "reference/house_citations.md").read_text(encoding="utf-8")
        pair = cdv.house_citation_pair()
        self.assertIsNotNone(pair)
        write(self.audit / "pkg/data/more.csv",
              "Vorname,Nachname,Name\nAlexander,Welch,Claude Response\nClaude,Holm,"
              f"{pair}\nPearson,Wilcoxon,Stafford Vilhuber\n")
        telemetry = {"invocations": [{"usage": {"unavailable_reasons": {
            "output_tokens": "Claude Response did not expose this token component"}}}],
            "toolkit": {"bash_version": "3.2.57(1)-release"}, "paper_title": "A synthetic article"}
        write(self.release / "report/REPORT_final.md",
              "# Comment\n\nAlexander and Claude were not consulted; a Welch test and a Holm "
              "correction were run.\n\n## References\n\n" + house)
        write(self.release / "run.json", json.dumps(telemetry, indent=2) + "\n")
        write(self.release / "SETTLED.md", f"- Confirmed by: Claude Response and {pair}\n")
        write(self.release / "scripts/helper.py", "# Stafford Vilhuber wrote the house citation\n")
        write(self.release / "critique/round_1/critic_stats.md", "Pearson and Wilcoxon tests.\n")
        result, summary = self.run_json()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(summary["hits"], [])
        self.assertTrue(summary["warnings"], "one-word matches are reported as warnings")
        self.assertIn("never a refusal", result.stdout)
        self.assert_nothing_printed(result, "Claude Response")

    def test_version_strings_and_xmp_namespaces_are_not_birth_dates(self) -> None:
        # MAJOR (check_deposit_values.py:338): fixed strings read as dates of birth.
        write(self.audit / "pkg/data/dates.csv",
              "Geburtsdatum\n22.02.1999\n03.12.2012\n03.02.1957\n10.12.2014\n")
        write(self.release / "report/env.md",
              "Run with Python 3.12.12 and bash 3.2.57; pandoc v3.12.12; see "
              "http://www.w3.org/1999/02/22-rdf-syntax-ns#; speeds 10/12/14 km/h; items FIND-10/12/14.\n")
        write(self.release / "work/tool.py", '__version__ = "3.12.12"\n')
        write(self.release / "SETTLED.json", json.dumps({"python_version": "3.12.12"}) + "\n")
        write(self.release / "run.json", json.dumps({"toolkit": {"bash_version": "3.2.57"}}) + "\n")
        if HAVE_PDF:
            write(self.release / "report/comment.pdf", cdv._minimal_pdf(
                "Clean text.", xmp=cdv._xmp_packet("http://www.w3.org/1999/02/22-rdf-syntax-ns#")))
        write(self.release / "report/leak.md", "Born 22.02.1999.\n")
        result, summary = self.run_json()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual({item["location"].split(" ", 1)[0] for item in summary["hits"]},
                         {"report/leak.md"}, result.stdout)

    def test_month_name_birth_dates_are_collected_and_matched(self) -> None:
        # MAJOR (check_deposit_values.py:409): dd-Mon-yyyy, yyyy-Mon-dd, ordinals, French months.
        write(self.audit / "pkg/data/dates.csv",
              "Geburtsdatum\n07-Aug-1902\n1903-Sep-04\n5th June 1904\n6 avril 1905\n")
        write(self.audit / "pkg/data/odd.csv", "DOB\nsometime\nlater\n")
        cases = {
            "report/a.md": "Born 1902-08-07.\n", "report/b.md": "Born 07-AUG-1902.\n",
            "report/c.md": "Born 4 September 1903.\n", "report/d.md": "Born June 5, 1904.\n",
            "report/e.md": "Née le 6 avril 1905.\n", "report/f.md": "Nacido el 6 de abril de 1905.\n",
        }
        for relative, text in cases.items():
            write(self.release / relative, text)
        result, summary = self.run_json()
        for relative in cases:
            with self.subTest(relative=relative):
                self.assertEqual(self.verdict(summary, relative), "refuse", result.stdout)
        # A personal-data column that yields no value is listed, never silently counted.
        self.assertIn("column 'DOB' in data/odd.csv looks like a date of birth column but yielded "
                      "no value", result.stdout)

    def test_ipv6_addresses_are_collected_and_detected(self) -> None:
        # MAJOR (check_deposit_values.py:583).
        write(self.audit / "pkg/data/qualtrics.csv", "IPAddress\n2001:db8:85a3::8a2e:370:7334\n")
        public = ":".join(["3ffe", "b00", "0", "0", "1", "0", "0", "a"])
        cases = {
            "report/deposit.md": ("From 2001:0DB8:85A3:0000:0000:8A2E:0370:7334.\n", "refuse"),
            "report/generic.md": (f"From {public}.\n", "refuse"),
            "report/exempt.md": ("Documentation 2001:db8::10, link-local fe80::1, ULA fd00::1, "
                                 "std::vector, 12:30:45.\n", "pass"),
        }
        for relative, (text, _expected) in cases.items():
            write(self.release / relative, text)
        result, summary = self.run_json()
        for relative, (_text, expected) in cases.items():
            with self.subTest(relative=relative):
                self.assertEqual(self.verdict(summary, relative), expected, result.stdout)
        self.assertEqual(summary["deposit"]["pii_columns"].get("ip"), 2)
        self.assertGreaterEqual(summary["values"]["ip"], 2)
        self.assert_nothing_printed(result, "2001:db8:85a3", public)

    def test_sheet_member_and_file_names_are_never_printed(self) -> None:
        # MAJOR (check_deposit_values.py:746) and MINOR (:673).
        write(self.audit / "pkg/data/per_person.xlsx", cdv._minimal_xlsx(
            [["Name"], ["Tessaly Vornquist"]], sheet_name="Vendelquist Oranamoor"))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Ostravinsky Quellmarr.csv", "Name\nOstravinsky Quellmarr\n")
        write(self.audit / "pkg/data/bundle.zip", buffer.getvalue())
        write(self.audit / "pkg/data/Survey 2021 - Group A.csv", "Name\nZelvarine Oquastrom\n")
        write(self.audit / "pkg/data/export-1901-04-03.csv", "Name\nMarvolette Quistrand\n")
        files = sorted(path.relative_to(self.audit / "pkg").as_posix()
                       for path in (self.audit / "pkg").rglob("*") if path.is_file())
        write(self.audit / "package_manifest.json", json.dumps(
            {"files": [{"path": path, "size": (self.audit / "pkg" / path).stat().st_size}
                       for path in files]}))
        dated_index = files.index("data/export-1901-04-03.csv") + 1
        write(self.release / "report/a.md", "Tessaly Vornquist left.\n")
        write(self.release / "report/b.md", "Ostravinsky Quellmarr left.\n")
        write(self.release / "report/c.md", "Zelvarine Oquastrom left.\n")
        write(self.release / "report/d.md", "Marvolette Quistrand left.\n")
        result, summary = self.run_json()
        sources = {item["location"].split(" ", 1)[0]: item["source"] for item in summary["hits"]}
        self.assertIn("data/per_person.xlsx sheet 1", sources["report/a.md"])
        self.assertIn("data/bundle.zip member 1", sources["report/b.md"])
        self.assertIn("in data/Survey 2021 - Group A.csv", sources["report/c.md"])
        self.assertIn(f"in manifest file #{dated_index}", sources["report/d.md"])
        printed = result.stdout + json.dumps(summary)
        for value in ("Vendelquist", "Oranamoor", "Ostravinsky", "1901-04-03", "Tessaly",
                      "Zelvarine", "Marvolette"):
            self.assertNotIn(value.casefold(), printed.casefold())

    def test_capped_reads_are_reported(self) -> None:
        # MINOR (check_deposit_values.py:768): row, distinct-value and zip-member caps.
        write(self.audit / "pkg/data/long.csv",
              "Name\n" + "".join(f"Zqar Vexel{chr(97 + number)}\n" for number in range(8)))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("big.csv", "Name\n" + "x\n" * 5000)
        write(self.audit / "pkg/data/bundle.zip", buffer.getvalue())
        write(self.release / "report/clean.md", VALUE_FREE)
        lines: list[str] = []
        with mock.patch.object(cdv, "MAX_ROWS", 5), mock.patch.object(cdv, "MAX_DISTINCT_PER_COLUMN", 3), \
                mock.patch.object(cdv, "MAX_MEMBER_BYTES", 1000):
            status = cdv.run_check(self.audit, self.release, json_path=self.base / "s.json",
                                   out=lines.append)
        printed = "\n".join(lines)
        self.assertEqual(status, 0, printed)
        summary = json.loads((self.base / "s.json").read_text())
        truncated = {row["file"]: row["reason"] for row in summary["deposit"]["truncated"]}
        self.assertIn("first 5 rows read", truncated["data/long.csv"])
        self.assertIn("first 3 distinct values used", truncated["data/long.csv"])
        self.assertIn("member 1: larger than the", truncated["data/bundle.zip"])
        self.assertIn("read in part (never a refusal): data/long.csv", printed)
        self.assertIn("read in part", printed.split("\n")[0])

    def test_romance_and_dutch_headers_are_recognised(self) -> None:
        # MINOR (check_deposit_values.py:157).
        expected = {
            "Nom": ("name", "full"), "Prénom": ("name", "first"), "Nom de famille": ("name", "last"),
            "Date de naissance": ("birthdate", ""), "Adresse électronique": ("email", ""),
            "Nombre": ("name", "full"), "Apellido": ("name", "last"),
            "fecha_nacimiento": ("birthdate", ""), "Correo electrónico": ("email", ""),
            "Nome": ("name", "full"), "Cognome": ("name", "last"), "data di nascita": ("birthdate", ""),
            "Sobrenome": ("name", "last"), "data_nasc": ("birthdate", ""),
            "Naam": ("name", "full"), "Voornaam": ("name", "first"), "Achternaam": ("name", "last"),
            "Geboortedatum": ("birthdate", ""), "nom de naissance": ("name", "last"),
            "clientIP": ("ip", ""), "RemoteIp": ("ip", ""),
        }
        for header, cls in expected.items():
            with self.subTest(header=header):
                self.assertEqual(cdv.classify_header(header), cls)
        self.assertEqual(cdv.classify_column("v3", "Quel est votre nom ?"), ("name", "full"))
        write(self.audit / "pkg/data/roster_fr.csv",
              "Nom;Prénom;Date de naissance\nXorbelline;Quiravette;05/06/1902\n")
        write(self.release / "report/fr.md", "Quiravette Xorbelline, née le 5 juin 1902.\n")
        result, summary = self.run_json()
        self.assertEqual(self.verdict(summary, "report/fr.md"), "refuse", result.stdout)
        kinds = {item["kind"] for item in summary["hits"]}
        self.assertIn("a name from the deposit", kinds)
        self.assertIn("a date of birth from the deposit", kinds)

    def test_the_key_never_leaves_the_process(self) -> None:
        # MINOR (check_deposit_values.py:1475): no key beside the hashes, no store on disk.
        store = cdv.build_store(self.audit)
        self.assertNotIn("salt", store)
        again = cdv.build_store(self.audit)
        self.assertNotEqual(store["hasher"]("phrase", "x"), again["hasher"]("phrase", "x"))
        self.assertEqual(repr(store["hasher"]), "Hasher(<one-run key>)")
        write(self.release / "report/clean.md", VALUE_FREE)
        before = listing(self.base)
        self.assertEqual(self.run_check().returncode, 0)
        self.assertEqual(listing(self.base), before)

    def test_front_matter_house_citations_and_references_are_allowlisted(self) -> None:
        # MINOR (check_deposit_values.py:1816): the whole front matter, as documented.
        write(self.audit / "report/front_matter.md",
              (self.audit / "report/front_matter.md").read_text()
              + "**Replication package:** checked by Zorvanquell Ixbretta.\n")
        write(self.audit / "pkg/data/names.csv",
              "Name\nZorvanquell Ixbretta\nMarvolette Quistrand\n")
        write(self.release / "report/REPORT_final.md",
              "# Comment\n\nAs Marvolette Quistrand showed.\n\n## References\n\n"
              "Quistrand, Marvolette (1901). A synthetic study.\n")
        write(self.release / "report/a.md", "Zorvanquell Ixbretta checked the package.\n")
        result, summary = self.run_json()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(summary["hits"], [])

    def test_markup_hyphenation_and_encoding_do_not_hide_a_value(self) -> None:
        # MINOR (check_deposit_values.py:1794). Both names end in an ordinary word, which is never
        # kept as a one-word value, so only the whole name can match.
        write(self.audit / "pkg/data/ordinary.csv", "Name\nCider Tower\n")
        write(self.audit / "pkg/data/latin1.csv",
              ("Name,comment\n" + ",padding that keeps the name past the head\n" * 3000).encode()
              + "Brümmelstädt Tower,x\n".encode("cp1252"))
        cases = {
            "report/bold.html": "<p>One row: Ci<b>der</b> Tower</p>\n",
            "report/span.htm": '<td><span class="x">Ci</span>der Tower</td>\n',
            "report/hyphen.md": "The row reads Cider To-\nwer here.\n",
            "report/soft.md": "The row reads Ci\u00adder Tower.\n",
            "report/latin.md": "Brümmelstädt Tower withdrew.\n",
            "report/escaped.json": '{"observed": "\\u0051orvelline Xandrithow"}\n',
        }
        for relative, text in cases.items():
            write(self.release / relative, text)
        write(self.release / "report/latin1_release.md",
              "Brümmelstädt Tower again.\n".encode("cp1252"))
        result, summary = self.run_json()
        for relative in [*cases, "report/latin1_release.md"]:
            with self.subTest(relative=relative):
                self.assertEqual(self.verdict(summary, relative), "refuse", result.stdout)
        self.assertEqual([item["location"] for item in summary["hits"]
                          if item["location"].startswith("report/hyphen.md")],
                         ["report/hyphen.md line 1"])

    def test_skill_generated_files_meet_only_the_address_detectors(self) -> None:
        # BLOCKER, second half: value matching skips provenance; e-mail and IP detectors do not.
        stranger = "zq.roe" + AT + "uni-" + "nowhere.edu"
        write(self.release / "ENV.txt", f"Python 3.12.12; {FULL_NAME}\n")
        write(self.release / "document_build_manifest.json", json.dumps({"note": FULL_NAME}) + "\n")
        write(self.release / "run.json", json.dumps({
            "invocations": [{"error": FULL_NAME}],
            "events": [{"payload": {"text": f"operator note: {FULL_NAME}"}}],
            "toolkit": {"maintainer": stranger}}, indent=2) + "\n")
        write(self.release / "comment.json", json.dumps({
            "findings": [{"provenance": {"auditor_report": FULL_NAME},
                          "evidence": {"observed": "row 3 of data/survey.csv"}}]}, indent=2) + "\n")
        result, summary = self.run_json()
        run_hits = sorted((item["location"], item["kind"]) for item in summary["hits"]
                          if item["location"].startswith("run.json"))
        self.assertEqual([kind for _location, kind in run_hits],
                         ["a name from the deposit", "an e-mail address"], result.stdout)
        for relative in ("ENV.txt", "document_build_manifest.json", "comment.json"):
            with self.subTest(relative=relative):
                self.assertEqual(self.verdict(summary, relative), "pass", result.stdout)
        self.assert_nothing_printed(result, stranger)


@unittest.skipUnless(HAVE_R, "Rscript with haven is required")
class RReaderTests(DepositFixture):
    def test_spss_names_and_variable_labels_are_read_through_r(self) -> None:
        sav = self.audit / "pkg/data/extra.sav"
        script = ('df <- data.frame(id = 1L, v7 = "Vorsterkampff Quenmoor", Nachname = "Quenbrillo",'
                  ' clientIP = "10.201.7.9", stringsAsFactors = FALSE);'
                  ' attr(df$v7, "label") <- "What is your name?";'
                  ' haven::write_sav(df, commandArgs(TRUE)[[1]])')
        written = subprocess.run([cdv.r_capability()["rscript"], "--vanilla", "-e", script, str(sav)],
                                 capture_output=True, check=False)
        self.assertEqual(written.returncode, 0, written.stderr)
        write(self.release / "report/r.md", "Vorsterkampff Quenmoor left.\n")
        write(self.release / "report/r_single.md", "Quenbrillo left.\n")
        # MINOR (check_deposit_values.py:257): a camelCase IP column, private range, from SPSS.
        write(self.release / "report/r_ip.md", "Seen from 10.201.7.9.\n")
        result, summary = self.run_json()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.verdict(summary, "report/r.md"), "refuse", result.stdout)
        self.assertEqual(self.verdict(summary, "report/r_single.md"), "warn", result.stdout)
        self.assertEqual(self.verdict(summary, "report/r_ip.md"), "refuse", result.stdout)
        self.assertIn("through R", result.stdout)
        self.assert_nothing_printed(result, "Vorsterkampff", "Quenbrillo", "Quenmoor", "10.201.7.9")

    def test_r_objects_are_numbered_never_named(self) -> None:
        rdata = self.audit / "pkg/data/objects.RData"
        script = ('e <- new.env(); assign("Vendelquist Oranamoor", data.frame(Name = "Talvorine Esquimant",'
                  ' stringsAsFactors = FALSE), envir = e); save(list = ls(e), envir = e,'
                  ' file = commandArgs(TRUE)[[1]])')
        written = subprocess.run([cdv.r_capability()["rscript"], "--vanilla", "-e", script,
                                  str(rdata)], capture_output=True, check=False)
        self.assertEqual(written.returncode, 0, written.stderr)
        write(self.release / "report/r.md", "Talvorine Esquimant left.\n")
        result, summary = self.run_json()
        self.assertEqual(self.verdict(summary, "report/r.md"), "refuse", result.stdout)
        self.assertIn("data/objects.RData object 1", result.stdout)
        self.assertNotIn("vendelquist", (result.stdout + json.dumps(summary)).casefold())


class SelfTestTests(unittest.TestCase):
    def test_self_test_passes_and_catches_disabled_paths(self) -> None:
        lines: list[str] = []
        self.assertTrue(cdv.self_test(lines.append), "\n".join(lines))
        # MINOR (check_deposit_values.py:2018): every path the self-test certifies must be one
        # whose loss it notices.
        disabled = {
            "identifier_column": lambda values: False,
            "email_exempt_domain": lambda address: True,
            "classify_header": lambda header, label=False: None,
            "paper_texts": lambda audit: [],
            "public_texts": lambda audit, release: [],
            "strip_markup": lambda text, inline=True: text,
            "_json_segments": lambda relative, text: None,
            "dehyphenate": lambda text: None,
            "last_first": lambda text: None,
            "pdf_metadata_for_values": lambda info, meta: "",
            "read_zip": mock.Mock(side_effect=cdv.NotScanned("disabled")),
            "read_json": mock.Mock(side_effect=cdv.NotScanned("disabled")),
            "ipv6_canonical": lambda text: None,
            "provenance_file": lambda relative: False,
            "value_matched_field": lambda relative, keys: True,
            "deposit_label": lambda relative, number, listed: relative,
        }
        for name, broken in disabled.items():
            with self.subTest(disabled=name), mock.patch.object(cdv, name, broken):
                out: list[str] = []
                self.assertFalse(cdv.self_test(out.append), f"{name} disabled, self-test still passed")
                self.assertIn("FAIL", "\n".join(out))
        for name in ("add_full_name", "_names"):
            with self.subTest(disabled=name), \
                    mock.patch.object(cdv.Collector if name == "add_full_name" else cdv.Matcher,
                                      name, lambda *args, **kwargs: False):
                self.assertFalse(cdv.self_test(lambda line: None), f"{name} disabled")

    def test_command_line_self_test(self) -> None:
        result = subprocess.run([sys.executable, str(CHECK), "--self-test"], text=True,
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("deposit-value detector self-tested", result.stdout)


class ExitStatusTests(DepositFixture):
    def test_an_internal_error_is_exit_2_with_its_class_only(self) -> None:
        # MINOR (publish_completed.sh:312, second half).
        write(self.release / "report/clean.md", VALUE_FREE)
        with mock.patch.object(cdv, "scan_release", side_effect=RuntimeError(FULL_NAME)), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            status = cdv.main([str(self.audit), str(self.release)])
        self.assertEqual(status, 2)
        self.assertIn("stopped on RuntimeError", stderr.getvalue())
        self.assertNotIn(FULL_NAME.casefold(), stderr.getvalue().casefold())


# ------------------------------------------------------------------------------------------------
# Integration: stage_release.sh (check 3c) and publish_completed.sh on the release fixture.
# ------------------------------------------------------------------------------------------------
from tests.test_release_pipeline import FIXTURE_ENV, PUBLISH, STAGE, make_audit  # noqa: E402

FIXTURE_EMAIL = "quenbrillo.v" + AT + "nowhere.invalid"
FIXTURE_NAME = "Quenbrillo Vastermoor"
# Full names made of words the fixture's own fixed texts print: its report title and front matter
# ("Independent audit"), its References line ("AI-Games protocol"), SETTLED.md ("Settled facts") and
# run.json's redaction boilerplate ("Internal registry record"). None of them may refuse.
BOILERPLATE_NAMES = ("Independent Audit", "Games Protocol", "Settled Facts", "Internal Registry")


def plant_deposit(leak: bool):
    """A before_registry hook: a personal-data deposit, and optionally a finding that quotes it."""
    def hook(audit: pathlib.Path, finding: dict) -> None:
        pkg = audit / "pkg"
        os.chmod(pkg, 0o755)
        write(pkg / "data/roster.csv",
              f"Name,E-Mail,Geburtsdatum,score\nQuenbrillo Vastermoor,{FIXTURE_EMAIL},03.04.1901,3\n"
              + "".join(f"{name},,,4\n" for name in BOILERPLATE_NAMES))
        rows, types = [], collections.Counter()
        for path in sorted(p for p in pkg.rglob("*") if p.is_file()):
            info = path.stat()
            extension = path.suffix.lower().lstrip(".")
            types[extension] += 1
            rows.append({"path": path.relative_to(pkg).as_posix(), "size": info.st_size,
                         "mtime_ns": info.st_mtime_ns, "extension": extension,
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        tree = hashlib.sha256("".join(f"{r['path']}\0{r['sha256']}\n" for r in rows).encode()).hexdigest()
        write(audit / "package_manifest.json", json.dumps({
            "schema_version": "2.0.0", "algorithm": "sha256", "package_sha256": tree,
            "file_count": len(rows), "file_types": dict(sorted(types.items())), "files": rows,
        }, indent=2) + "\n")
        os.chmod(pkg / "data/roster.csv", 0o444)
        os.chmod(pkg / "data", 0o555)
        os.chmod(pkg, 0o555)
        if leak:
            # evidence.observed is projected verbatim into comment.json: the leak route no
            # earlier gate knew.
            # 2.6.8 QA: check_release_privacy refuses any e-mail at the post-delta privacy gate, so the
            # leak that reaches staging is a deposit name no earlier gate knows.
            finding["evidence"]["observed"] = f"the roster row names {FIXTURE_NAME}"
    return hook


class StageAndPublishIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-deposit-values-stage-")
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        # 2.6.8 QA: publish needs a discipline tree, or the fixture opt-in (tests/test_release_pipeline.py)
        self.environment = {**os.environ, "LC_ALL": "C", "I4R_RESERVE_GRACE": "0", **FIXTURE_ENV}

    def run_script(self, script: pathlib.Path, *arguments: pathlib.Path,
                   environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/bin/bash", str(script), *(str(value) for value in arguments)],
                              text=True, capture_output=True, timeout=300,
                              env=environment or self.environment)

    def test_a_value_leaked_through_evidence_refuses_staging_and_publication(self) -> None:
        audit = make_audit(self.root, before_registry=plant_deposit(leak=True))
        self.assertIn(FIXTURE_NAME, (audit / "comment.json").read_text())
        release = self.root / "stage-leak"
        staged = self.run_script(STAGE, audit, release)
        self.assertNotEqual(staged.returncode, 0, staged.stdout + staged.stderr)
        self.assertIn("deposit-value detector self-tested", staged.stdout)
        self.assertIn("comment.json line", staged.stdout)
        self.assertIn("a name from the deposit (matches deposit column 'Name' in "
                      "data/roster.csv)", staged.stdout)
        self.assertIn("RELEASE BLOCKED", staged.stdout)
        self.assertNotIn("release staged clean", staged.stdout)
        self.assertNotIn("could not inspect", staged.stdout)
        marker = (release / "RELEASE_REJECTED").read_text().splitlines()
        self.assertEqual(marker[1], "failed during: enforcing exclusions")
        self.assertNotIn(FIXTURE_NAME, staged.stdout + staged.stderr)
        self.assertFalse(list(release.rglob(cdv.STORE_NAME)))
        self.assertFalse((audit / "work" / cdv.STORE_NAME).exists())
        completed = self.root / "completed"
        published = self.run_script(PUBLISH, audit, completed)
        self.assertNotEqual(published.returncode, 0, published.stdout + published.stderr)
        self.assertIn("reproduces an identifying value from the deposit", published.stderr)
        self.assertIn("reword the finding", published.stderr)
        self.assertNotIn(FIXTURE_NAME, published.stdout + published.stderr)
        self.assertFalse(list(completed.rglob("comment.json")) if completed.exists() else [])

    def test_a_value_free_pii_deposit_stages_and_publishes(self) -> None:
        # The fixture's boilerplate collides with four planted full names; none refuses.
        audit = make_audit(self.root, before_registry=plant_deposit(leak=False))
        release = self.root / "stage-clean"
        staged = self.run_script(STAGE, audit, release)
        self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
        self.assertIn("personal-data columns: birthdate 1, email 1, name 1", staged.stdout)
        self.assertIn("ok: no deposit value, e-mail address or IP address", staged.stdout)
        self.assertFalse((release / "RELEASE_REJECTED").exists())
        self.assertFalse(list(release.rglob(cdv.STORE_NAME)))
        published = self.run_script(PUBLISH, audit, self.root / "completed")
        self.assertEqual(published.returncode, 0, published.stdout + published.stderr)

    @unittest.skipUnless(HAVE_PDF, "pdftotext and pdfinfo are required")
    def test_an_uninspectable_document_is_its_own_refusal(self) -> None:
        # MINOR (publish_completed.sh:312): exit 2 is not "reword the text". A pdftotext that only
        # the other gates' invocations can use makes the release uninspectable for check 3c alone.
        real = shutil.which("pdftotext")
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        write(fake_bin / "pdftotext",
              "#!/bin/sh\ncase \" $* \" in\n"
              "  *\" -nopgbrk \"*|*\" -layout \"*|*i4r-deposit-values-selftest-*) exec "
              f"'{real}' \"$@\" ;;\nesac\nexit 3\n")
        os.chmod(fake_bin / "pdftotext", 0o755)
        environment = {**self.environment, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
        audit = make_audit(self.root, before_registry=plant_deposit(leak=False))
        release = self.root / "stage-uninspectable"
        staged = self.run_script(STAGE, audit, release, environment=environment)
        self.assertNotEqual(staged.returncode, 0, staged.stdout + staged.stderr)
        self.assertIn("check 3c could not inspect this release", staged.stdout)
        self.assertIn("RELEASE BLOCKED", staged.stdout)
        self.assertNotIn("Remedy: reword", staged.stdout)
        published = self.run_script(PUBLISH, audit, self.root / "completed", environment=environment)
        self.assertNotEqual(published.returncode, 0, published.stdout + published.stderr)
        self.assertIn("check 3c could not inspect the publication set", published.stderr)
        self.assertNotIn("reproduces an identifying value", published.stderr)


class ScriptIndexTests(unittest.TestCase):
    def test_every_gate_the_release_scripts_run_is_indexed(self) -> None:
        # Integration minor (scripts/README.md:19): an operator looking up the script behind a
        # refusal finds it.
        index = (SCRIPTS / "README.md").read_text(encoding="utf-8")
        for script in ("stage_release.sh", "publish_completed.sh"):
            called = set(re.findall(r'"\$SCRIPT_DIR/([\w.-]+\.py)"', (SCRIPTS / script).read_text()))
            self.assertIn("check_deposit_values.py", called)
            for name in sorted(called):
                with self.subTest(script=script, called=name):
                    self.assertIn(f"`{name}`", index)


if __name__ == "__main__":
    unittest.main()
