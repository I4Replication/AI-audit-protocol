"""2.7.11: new_audit.sh no longer takes an archive silently when author files or folders sit beside it.

On 2026-10-26/27 SPSS, Mplus and JASP files sat beside the OSF archive of five papers. new_audit.sh took the archive
alone without a word, and the operators then took it alone, passed the paper folder (the archive copied unextracted)
or built a folder by hand, three different ways. These tests drive the real new_audit.sh, setup_workspace.sh and
init_run.sh on a scratch tree: such a folder is refused naming the archive and every entry, `--loose include` adds
the entries under pkg/beside_archive/ beside the extracted archive, `--loose exclude` takes the archive alone, and
the choice is recorded in paper_source.json and journaled once at init.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import zipfile

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SKILL / "tests"))
import test_tree_layout as layout  # noqa: E402  (sh, make_paper)


class ArchiveWithEntriesBesideIt(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-2711-beside-")
        self.addCleanup(directory.cleanup)
        self.addCleanup(lambda: subprocess.run(["chmod", "-R", "u+w", directory.name], capture_output=True))
        self.root = pathlib.Path(directory.name).resolve()
        (self.root / "reservations").mkdir()
        self.paper = layout.make_paper(self.root, "J", "P")   # article.pdf and pkg.zip (analysis.R, data.csv)
        self.audit = self.root / "runs" / "J" / "P" / "audit"

    def beside(self) -> None:
        (self.paper / "study1.sav").write_bytes(b"spss")
        (self.paper / "model.inp").write_text("TITLE: m;\n")
        (self.paper / "extra").mkdir()
        (self.paper / "extra" / "syntax.sps").write_text("FREQUENCIES x.\n")

    def new_audit(self, *arguments: str) -> subprocess.CompletedProcess:
        return layout.sh("/bin/bash", SCRIPTS / "new_audit.sh", self.paper, *arguments,
                         env={"PYTHONDONTWRITEBYTECODE": "1"})

    def pkg_files(self) -> list[str]:
        pkg = self.audit / "pkg"
        return sorted(path.relative_to(pkg).as_posix() for path in pkg.rglob("*") if path.is_file())

    def source(self) -> dict:
        return json.loads((self.audit / "paper_source.json").read_text())

    def test_entries_beside_an_archive_are_refused_naming_both_and_nothing_is_reserved(self) -> None:
        self.beside()
        result = self.new_audit()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        for name in ("pkg.zip", "study1.sav", "model.inp", "extra/ (a folder)", "--loose include", "--loose exclude"):
            self.assertIn(name, result.stderr)
        self.assertIn("move it out of data/, or choose exclude", result.stderr)     # INSTALL's wording (DT-9)
        self.assertFalse((self.root / "runs").exists())
        self.assertEqual(list((self.root / "reservations").iterdir()), [])

    def test_an_explicit_archive_with_entries_beside_it_is_refused_too(self) -> None:
        self.beside()
        result = self.new_audit("--package", str(self.paper / "pkg.zip"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--package", result.stderr)
        self.assertFalse((self.root / "runs").exists())

    def test_include_adds_the_entries_under_beside_archive_and_records_and_journals_it(self) -> None:
        self.beside()
        result = self.new_audit("--loose", "include")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pkg_files(), ["analysis.R", "beside_archive/extra/syntax.sps",
                                            "beside_archive/model.inp", "beside_archive/study1.sav", "data.csv"])
        self.assertEqual(self.source()["package"], {
            "kind": "archive", "name": "pkg.zip",
            "beside_archive": {"choice": "include", "entries": ["extra/", "model.inp", "study1.sav"]}})
        manifest = json.loads((self.audit / "package_manifest.json").read_text())
        self.assertEqual(manifest["file_count"], 5)
        init = layout.sh("bash", SCRIPTS / "init_run.sh", self.audit, "--audit-id", "t-2711-include",
                         "--protocol-version", "test", "--gate-set-version", "test",
                         env={"PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
        notes = [json.loads(line)["payload"] for line in (self.audit / "run.events.jsonl").read_text().splitlines()
                 if json.loads(line)["type"] == "note"]
        self.assertEqual([(note["kind"], note["choice"]) for note in notes], [("intake_package", "include")])
        self.assertIn("3 entries beside the package archive (.inp, .sav, folder)", notes[0]["text"])

    def test_exclude_takes_the_archive_alone_and_records_what_was_left_out(self) -> None:
        self.beside()
        result = self.new_audit("--loose", "exclude")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pkg_files(), ["analysis.R", "data.csv"])
        self.assertEqual(self.source()["package"]["beside_archive"],
                         {"choice": "exclude", "entries": ["extra/", "model.inp", "study1.sav"]})

    def test_negative_control_a_lone_archive_is_taken_as_before_and_journals_nothing(self) -> None:
        result = self.new_audit()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pkg_files(), ["analysis.R", "data.csv"])
        self.assertEqual(self.source(), {"schema_version": "i4r-paper-source-1", "journal": "J", "paper": "P"})
        init = layout.sh("bash", SCRIPTS / "init_run.sh", self.audit, "--audit-id", "t-2711-lone",
                         "--protocol-version", "test", "--gate-set-version", "test",
                         env={"PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
        self.assertNotIn('"type":"note"', (self.audit / "run.events.jsonl").read_text())

    def init(self, *extra: str) -> subprocess.CompletedProcess:
        return layout.sh("bash", SCRIPTS / "init_run.sh", self.audit, "--audit-id", "t-2711-reinit",
                         "--protocol-version", "test", "--gate-set-version", "test", *extra,
                         env={"PYTHONDONTWRITEBYTECODE": "1"})

    def intake_notes(self) -> list[dict]:
        events = [json.loads(line) for line in (self.audit / "run.events.jsonl").read_text().splitlines()]
        return [event["payload"] for event in events
                if event["type"] == "note" and event["payload"].get("kind") == "intake_package"]

    def test_a_forced_re_init_journals_the_choice_once_in_its_new_journal(self) -> None:
        self.beside()
        result = self.new_audit("--loose", "exclude")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first = self.init()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        again = self.init("--force")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual([note["choice"] for note in self.intake_notes()], ["exclude"])

    @unittest.skipUnless(os.path.exists(str(pathlib.Path(tempfile.gettempdir()).resolve()).swapcase()),
                         "needs a case-insensitive file system (APFS default)")
    def test_an_archive_named_in_another_case_is_still_seen_as_inside_the_paper_folder(self) -> None:
        # bash's pwd -P keeps the case typed, so comparing physical paths as strings missed the paper folder
        self.beside()
        typed = self.paper.parent / self.paper.name.swapcase() / "pkg.zip"
        result = self.new_audit("--package", str(typed))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--loose include", result.stderr)
        self.assertFalse((self.root / "runs").exists())

    def test_an_archive_holding_its_own_beside_archive_folder_is_refused_on_include(self) -> None:
        with zipfile.ZipFile(self.paper / "pkg.zip", "w") as archive:
            archive.writestr("analysis.R", "x <- 1\n")
            archive.writestr("beside_archive/notes.txt", "n\n")
        self.beside()
        result = self.new_audit("--loose", "include")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("top-level beside_archive entry", result.stdout + result.stderr)
        self.assertFalse((self.audit / "package_manifest.json").exists())
        # refused after the claim (INSTALL says so), with the commands that undo it printed
        self.assertIn("reserve_paper.sh", result.stderr)
        self.assertIn("release", result.stderr)

    def test_a_mixed_case_zip_is_an_archive_too(self) -> None:
        # Review EDGE-7: the package is found with -iname '*.zip' and unzipped whatever its case, so pkg.Zip with
        # files beside it is refused (not silently taken alone), include extracts it, and a corrupt one is refused
        # by the integrity check before the claim.
        (self.paper / "pkg.zip").rename(self.paper / "pkg.Zip")
        self.beside()
        refused = self.new_audit()
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("--loose include", refused.stderr)
        self.assertFalse((self.root / "runs").exists())
        result = self.new_audit("--loose", "include")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pkg_files(), ["analysis.R", "beside_archive/extra/syntax.sps",
                                            "beside_archive/model.inp", "beside_archive/study1.sav", "data.csv"])

    def test_a_truncated_mixed_case_zip_is_refused_before_the_claim(self) -> None:
        archive = self.paper / "pkg.zip"
        data = archive.read_bytes()
        archive.unlink()
        (self.paper / "pkg.Zip").write_bytes(data[:-40])
        result = self.new_audit()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("corrupt or truncated", result.stderr)
        self.assertEqual(list((self.root / "reservations").iterdir()), [])

    def test_a_conflicted_copy_beside_the_archive_is_named_in_the_paper_folder(self) -> None:
        # Review EDGE-8: the refusal named new_audit.sh's staging copy (removed on exit), not the entry to delete
        self.beside()
        (self.paper / "extra" / "a (Someone's conflicted copy 2026-10-01).sav").write_bytes(b"spss")
        result = self.new_audit("--loose", "include")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"{self.paper}/extra/a (Someone's conflicted copy 2026-10-01).sav", result.stderr)
        self.assertNotIn("i4r-beside.", result.stderr)
        self.assertEqual(list((self.root / "reservations").iterdir()), [])

    @unittest.skipUnless(os.path.exists(str(pathlib.Path(tempfile.gettempdir()).resolve()).swapcase()),
                         "needs a case-insensitive file system (APFS default)")
    def test_the_paper_folder_typed_in_another_case_is_the_loose_files_package(self) -> None:
        # Review DT-7 I1: the loose-files test compares folders (-ef), not path strings
        result = self.new_audit("--package", str(self.paper.parent / self.paper.name.swapcase()))
        self.assertIn("the paper folder is the package, so its .zip archive is copied", result.stdout,
                      result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
