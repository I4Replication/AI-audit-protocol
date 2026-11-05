"""2.7.11: release gates and document checks, each tested on disk with the real scripts.

1. (C11) A staged copy keeps its source's mode, and the critique driver freezes its round projections 0444. Path
   neutralisation, check 3d and check 3c rewrite STAGED copies, so a read-only record stopped staging on "Permission
   denied" (check 3c then called it a release it could not inspect). stage_release.sh gives the staged copies owner
   write; the audit's files keep their bytes and their mode.
2. (C19) Check 3c cannot read some deposit formats (.omv, .jasp, .xls, .npy, .h5, ...). It listed them as "not
   scanned" and then printed the same "ok:" a full read prints. A file it cannot read still never refuses; the verdict
   now says what it did not read, by format and count.
3. (C15) build_docx.sh (frozen) records a failure before its validation stage as "document build failed before
   promotion" and nothing else. scripts/build_document.py runs it unchanged and keeps what it printed beside
   last_failure.json.
4. Check 3c's self-test met a pdfinfo older than Poppler 21.06 (no -custom) as an "internal error" to report; it is
   a host problem with a remedy, and says so (exit status unchanged).

Each test fails on 2.7.10 (the negative controls pass on both). Every value is synthetic; home paths and addresses
are assembled, never spelled.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))

from tests import test_check_deposit_values as tcdv  # noqa: E402
from tests.test_document_release import policy_snapshot  # noqa: E402
from tests.test_release_pipeline import FIXTURE_ENV, make_audit, write  # noqa: E402

STAGE = SCRIPTS / "stage_release.sh"
HOME = "/" + "Users/"
GLOBAL_IP = ".".join(["93", "184", "216", "34"])     # assembled; a public documentation-free address


def environment(**extra: str) -> dict[str, str]:
    return {**os.environ, **FIXTURE_ENV, "LC_ALL": "C", "I4R_RESERVE_GRACE": "0", "PYTHONDONTWRITEBYTECODE": "1",
            **extra}


def foreground_job() -> None:
    """In the child, before exec: INT at its default, as for a terminal's foreground job."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def ignoring_int_and_hup() -> None:
    """In the child, before exec: INT and HUP ignored, as for a background job started with nohup."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

class StagedReadOnlyRecordsAreRewritten(unittest.TestCase):
    """C11: three read-only records, one per step that rewrites a staged copy."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-2711-stage-")
        self.root = pathlib.Path(self.temporary.name)
        self.addCleanup(self.cleanup)

    def cleanup(self) -> None:
        for path in self.root.rglob("*"):
            try:
                path.chmod(0o700 if path.is_dir() else 0o600)
            except OSError:
                pass
        self.temporary.cleanup()

    def test_read_only_records_are_masked_in_the_staged_copy_only(self) -> None:
        audit = make_audit(self.root)
        planted = {
            # check 3d: a home path in a work record (the account segment is masked)
            "work/a5_privacy_archive/evidence/scan.py": 'DATA = "' + HOME + 'synthacct/pkg/data.csv"\n',
            # check 3c: a public IP address in a critique record (masked with its class)
            "critique/round_1/critic_extra.md": f"The server log names host {GLOBAL_IP} once.\n",
            # neutralisation (check_release_privacy.neutralise_tree, frozen): the audit root in a work script
            "work/a1_reproduction/rerun.R": f'setwd("{audit.resolve()}/pkg")\n',
        }
        before = {}
        for relative, text in planted.items():
            write(audit / relative, text)
            (audit / relative).chmod(0o444)       # as the critique driver's projections and frozen snapshots are
            before[relative] = (audit / relative).read_bytes()
        release = self.root / "release"
        result = subprocess.run(["/bin/bash", str(STAGE), str(audit), str(release)], text=True,
                                capture_output=True, timeout=600, env=environment())
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-3000:])
        self.assertIn("release staged clean", result.stdout)
        self.assertNotIn("Permission denied", output)
        staged = {relative: (release / relative).read_text(encoding="utf-8") for relative in planted}
        self.assertIn(HOME + "<user-dir>/pkg/data.csv", staged["work/a5_privacy_archive/evidence/scan.py"])
        self.assertIn("[masked at release: an IP address]", staged["critique/round_1/critic_extra.md"])
        self.assertNotIn(GLOBAL_IP, staged["critique/round_1/critic_extra.md"])
        self.assertEqual(staged["work/a1_reproduction/rerun.R"], 'setwd("<audit>/pkg")\n')
        for relative, data in before.items():   # the audit's own files: same bytes, still read-only
            self.assertEqual((audit / relative).read_bytes(), data, relative)
            self.assertEqual(stat.S_IMODE((audit / relative).stat().st_mode), 0o444, relative)


class DepositCoverageIsReported(tcdv.DepositFixture):
    """C19: a clean verdict names the deposit files it could not read; it still never refuses for them."""

    UNREADABLE = {"data/session.omv": b"PK\x03\x04synthetic", "data/analysis.jasp": b"PK\x03\x04synthetic",
                  "data/legacy.xls": b"\xd0\xcf\x11\xe0synthetic", "data/array.npy": b"\x93NUMPYsynthetic",
                  "data/signals.h5": b"\x89HDF\r\n\x1a\nsynthetic"}

    def test_unread_formats_are_named_in_the_verdict(self) -> None:
        for relative, data in self.UNREADABLE.items():
            tcdv.write(self.audit / "pkg" / relative, data)
        tcdv.write(self.release / "report/clean.md", tcdv.VALUE_FREE)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)   # never a refusal
        lines = result.stdout.splitlines()
        self.assertFalse(any(line.startswith("  ok: no deposit value") for line in lines), result.stdout)
        self.assertIn("ok, within what it could read: no deposit value", result.stdout)
        self.assertIn("NOT COVERED (never a refusal): 5 deposit file(s) not read", result.stdout)
        for suffix in (".h5 1", ".jasp 1", ".npy 1", ".omv 1", ".xls 1"):
            self.assertIn(suffix, result.stdout)
        self.assert_nothing_printed(result)

    def test_a_missing_deposit_copy_is_not_a_clean_read(self) -> None:
        shutil.rmtree(self.audit / "pkg")
        tcdv.write(self.release / "report/clean.md", tcdv.VALUE_FREE)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("NOT COVERED (never a refusal): no frozen deposit copy was found", result.stdout)

    def test_negative_control_a_fully_read_deposit_keeps_the_plain_verdict(self) -> None:
        tcdv.write(self.release / "report/clean.md", tcdv.VALUE_FREE)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  ok: no deposit value, e-mail address or IP address", result.stdout)
        self.assertNotIn("NOT COVERED", result.stdout)

    def test_negative_control_a_leak_still_refuses(self) -> None:
        tcdv.write(self.audit / "pkg/data/legacy.xls", b"\xd0\xcf\x11\xe0synthetic")
        tcdv.write(self.release / "report/REPORT_final.md", f"Row {tcdv.FULL_NAME} is listed.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("ok, within what it could read", result.stdout)
        self.assert_nothing_printed(result)


@unittest.skipUnless(shutil.which("pdftotext"), "pdftotext is required for the self-test's PDF controls")
class SelfTestNamesAnOldPoppler(unittest.TestCase):
    """New: a pdfinfo without -custom (Poppler before 21.06, first on PATH on hosts with an old base conda) made the
    check 3c self-test print "internal error ... report the error class". It is a host problem with a remedy."""

    def test_an_extractor_failure_is_not_an_internal_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-2711-poppler-") as directory:
            fake = pathlib.Path(directory) / "pdfinfo"
            fake.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            fake.chmod(0o755)
            result = subprocess.run([sys.executable, str(SCRIPTS / "check_deposit_values.py"), "--self-test"],
                                    text=True, capture_output=True, timeout=300,
                                    env=environment(PATH=f"{directory}{os.pathsep}{os.environ['PATH']}"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)    # unchanged: nothing certified
        self.assertIn("FAIL (not inspectable)", result.stderr)
        self.assertIn("pdfinfo exited 99", result.stderr)
        self.assertIn("Poppler 21.06 or newer", result.stderr)
        self.assertNotIn("internal error", result.stderr)


class BuildFailureOutputIsKept(unittest.TestCase):
    """C15: the build step SKILL.md runs keeps the reason a build failed, not only its stage."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-2711-build-")
        self.addCleanup(self.temporary.cleanup)
        self.audit = pathlib.Path(self.temporary.name) / "audit"
        policy_snapshot(self.audit)
        write(self.audit / "report/front_matter.md", '::: {custom-style="Title"}\n**A comment**\n:::\n')
        write(self.audit / "report/REPORT_final.md",
              "# Title\n\n## Abstract\n\n" + "This synthetic comment checks the failure record. " * 3
              + "\n\n## 1. Introduction\n\nText.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n## References\n\nNone.\n")

    def build(self) -> subprocess.CompletedProcess[str]:
        wrapper = SCRIPTS / "build_document.py"     # 2.7.11; on 2.7.10 SKILL.md ran build_docx.sh itself
        command = ([sys.executable, str(wrapper)] if wrapper.is_file() else ["bash", str(SCRIPTS / "build_docx.sh")])
        conda = pathlib.Path.home() / "miniconda3/envs/i4rdoc/bin"
        env = environment(PATH=f"{conda}:{os.environ['PATH']}" if conda.is_dir() else os.environ["PATH"])
        return subprocess.run([*command, str(self.audit), "comment"], text=True, capture_output=True, timeout=600,
                              env=env)

    def kept_reason(self) -> str:
        diagnostics = self.audit / "document_build_diagnostics"
        return "\n".join(path.read_text(encoding="utf-8") for path in sorted(diagnostics.iterdir())
                         if path.is_file())

    def test_a_missing_input_is_recorded_with_its_reason(self) -> None:
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        record = json.loads((self.audit / "document_build_diagnostics/last_failure.json").read_text())
        self.assertEqual(record["stage"], "preflight")          # build_docx.sh's own record is unchanged
        self.assertIn("captions.txt -- one caption per line", self.kept_reason())
        self.assertIn("at stage preflight", result.stderr)

    @unittest.skipUnless((shutil.which("pandoc") or (pathlib.Path.home() / "miniconda3/envs/i4rdoc/bin/pandoc").exists())
                         and (shutil.which("soffice")
                              or os.access("/Applications/LibreOffice.app/Contents/MacOS/soffice", os.X_OK))
                         and shutil.which("pdfinfo") and shutil.which("pdftotext"),
                         "build_docx.sh's preflight needs pandoc, LibreOffice and Poppler to reach the source transform")
    def test_a_table_without_a_caption_is_recorded_with_its_reason(self) -> None:
        write(self.audit / "report/captions.txt", "")
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1 table blocks but 0 captions", self.kept_reason())
        self.assertIn("at stage source_transform", result.stderr)
        self.assertIn("1 table blocks but 0 captions", result.stdout + result.stderr)


    @unittest.skipUnless((shutil.which("pandoc") or (pathlib.Path.home() / "miniconda3/envs/i4rdoc/bin/pandoc").exists())
                         and (shutil.which("soffice")
                              or os.access("/Applications/LibreOffice.app/Contents/MacOS/soffice", os.X_OK))
                         and shutil.which("pdfinfo") and shutil.which("pdftotext"),
                         "a canonical build needs pandoc, LibreOffice and Poppler")
    def test_a_successful_build_writes_no_diagnostic(self) -> None:
        wrapper = SCRIPTS / "build_document.py"
        self.assertTrue(wrapper.is_file(), "scripts/build_document.py is the build step SKILL.md runs")
        conda = pathlib.Path.home() / "miniconda3/envs/i4rdoc/bin"
        env = environment(PATH=f"{conda}:{os.environ['PATH']}" if conda.is_dir() else os.environ["PATH"])
        with tempfile.TemporaryDirectory(prefix="i4r-2711-build-ok-") as directory:
            audit = make_audit(pathlib.Path(directory))
            before = sorted(path.relative_to(audit).as_posix() for path in audit.rglob("*")
                            if "document_build_diagnostics" in path.parts)
            result = subprocess.run([sys.executable, str(wrapper), str(audit), "comment"], text=True,
                                    capture_output=True, timeout=600, env=env)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-3000:])
            after = sorted(path.relative_to(audit).as_posix() for path in audit.rglob("*")
                           if "document_build_diagnostics" in path.parts)
            self.assertEqual(after, before)
            self.assertNotIn("build_docx.sh failed", result.stderr)
            self.assertFalse(list((audit / ".document-build-transactions").glob("build.*")))


STUB_BUILDER = r"""#!/bin/bash
# A stand-in for build_docx.sh beside a copy of the wrapper: the wrapper runs the build_docx.sh in its own folder.
DIR="$(cd "$1" && pwd -P)"
record () {
  mkdir -p "$DIR/document_build_diagnostics"
  printf '{"stage": "%s", "message": "document build failed before promotion"}\n' "$1" \
    > "$DIR/document_build_diagnostics/last_failure.json"
}
case "$STUB_MODE" in
  ok) echo "built $DIR/report/comment.pdf"; exit 0 ;;
  fail)
    echo "reading $DIR/report/REPORT_final.md"
    echo "FATAL: build_docx.sh: write $DIR/report/captions.txt -- one caption per line" >&2
    echo "tool cache under $HOME/synthetic-cache" >&2
    record preflight; exit 1 ;;
  status7) echo "FATAL: synthetic" >&2; record pandoc; exit 7 ;;
  early) echo "FATAL: build_docx.sh: unknown option" >&2; exit 1 ;;
  trapped)
    trap 'echo trap >> "$DIR/traps"; record pandoc; exit 130' INT
    trap 'echo trap >> "$DIR/traps"; record pandoc; exit 143' TERM
    : > "$DIR/started"
    sleep 30
    echo "sleep was not interrupted" ;;
  untrapped) : > "$DIR/started"; sleep 30 ;;
  many) i=0; while [ $i -lt 3000 ]; do echo "line $i of the build"; i=$((i+1)); done; exit 0 ;;
  manyfail)
    i=0; while [ $i -lt 3000 ]; do echo "line $i of the build"; i=$((i+1)); done
    echo "FATAL: synthetic late failure" >&2; record pandoc; exit 3 ;;
esac
"""


class BuildDocumentWrapperMechanics(unittest.TestCase):
    """C15: build_document.py around a stand-in builder: exit status, signals, nothing on success, no stale pairing,
    and the kept output shows the audit folder as $AUDIT_DIR and the home folder as ~."""

    def setUp(self) -> None:
        wrapper = SCRIPTS / "build_document.py"
        self.assertTrue(wrapper.is_file(), "scripts/build_document.py is the build step SKILL.md runs")
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-2711-wrapper-")
        self.addCleanup(self.temporary.cleanup)
        root = pathlib.Path(self.temporary.name)
        scripts = root / "skill/scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(wrapper, scripts / "build_document.py")
        (scripts / "build_docx.sh").write_text(STUB_BUILDER, encoding="utf-8")
        self.wrapper = scripts / "build_document.py"
        self.home = root / "home"
        self.home.mkdir()
        self.audit = self.home / "runs/Journal/Paper/audit"
        (self.audit / "report").mkdir(parents=True)
        self.diagnostics = self.audit / "document_build_diagnostics"

    def start(self, mode: str, **popen: object) -> subprocess.Popen[str]:
        return subprocess.Popen([sys.executable, str(self.wrapper), str(self.audit), "comment"], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=environment(STUB_MODE=mode, HOME=str(self.home)), **popen)

    def run_mode(self, mode: str) -> tuple[int, str, str]:
        process = self.start(mode)
        out, err = process.communicate(timeout=60)
        return process.returncode, out, err

    def wait_started(self) -> None:
        for _ in range(200):
            if (self.audit / "started").exists():
                return
            time.sleep(0.05)
        self.fail("the stand-in builder never started")

    def test_success_writes_nothing(self) -> None:
        status, out, err = self.run_mode("ok")
        self.assertEqual(status, 0, err)
        self.assertIn("built ", out)
        self.assertFalse(self.diagnostics.exists())
        self.assertEqual(err, "")

    def test_failure_output_is_kept_with_private_folders_masked(self) -> None:
        status, out, err = self.run_mode("fail")
        self.assertEqual(status, 1)
        self.assertIn("at stage preflight", err)
        self.assertIn("captions.txt -- one caption per line", err)
        kept = (self.diagnostics / "last_failure_output.txt").read_text(encoding="utf-8")
        self.assertIn("FATAL: build_docx.sh: write $AUDIT_DIR/report/captions.txt", kept)
        self.assertIn("reading $AUDIT_DIR/report/REPORT_final.md", kept)
        self.assertIn("tool cache under ~/synthetic-cache", kept)
        self.assertNotIn(self.temporary.name, kept)
        self.assertNotIn(os.path.realpath(self.temporary.name), kept)
        self.assertNotIn(str(self.home), kept)
        record = json.loads((self.diagnostics / "last_failure.json").read_text())
        self.assertEqual(record["stage"], "preflight")       # the builder's own record is left as it wrote it

    def test_exit_status_is_the_builders(self) -> None:
        status, _out, err = self.run_mode("status7")
        self.assertEqual(status, 7, err)
        self.assertIn("build_docx.sh failed (exit 7) at stage pandoc", err)

    def test_a_failure_with_no_new_record_keeps_nothing_beside_an_older_one(self) -> None:
        self.diagnostics.mkdir()
        older = '{"stage": "validation", "message": "an older failure"}\n'
        (self.diagnostics / "last_failure.json").write_text(older, encoding="utf-8")
        status, _out, err = self.run_mode("early")
        self.assertEqual(status, 1)
        self.assertNotIn("at stage", err)
        self.assertIn("unknown option", err)
        self.assertFalse((self.diagnostics / "last_failure_output.txt").exists())
        self.assertEqual((self.diagnostics / "last_failure.json").read_text(encoding="utf-8"), older)

    def test_an_interrupt_reaches_the_builder_once_and_its_cleanup_runs(self) -> None:
        # As a terminal delivers ^C to its foreground job: to the wrapper's whole process group, with INT not ignored
        # (a suite run as a background job starts every child ignoring INT, which a foreground job never does).
        process = self.start("trapped", start_new_session=True, preexec_fn=foreground_job)
        self.wait_started()
        began = time.monotonic()
        os.killpg(process.pid, signal.SIGINT)
        _out, err = process.communicate(timeout=25)
        self.assertLess(time.monotonic() - began, 20, "the builder's foreground child was not interrupted")
        self.assertEqual(process.returncode, 130, err)
        self.assertEqual((self.audit / "traps").read_text().splitlines(), ["trap"])
        self.assertTrue((self.diagnostics / "last_failure_output.txt").is_file())

    def test_a_signal_the_wrapper_was_started_ignoring_stays_ignored(self) -> None:
        # A background job (INT ignored) or a nohup'd one (HUP ignored) keeps building through that signal, as
        # build_docx.sh run directly would; TERM still reaches it.
        process = self.start("trapped", start_new_session=True, preexec_fn=ignoring_int_and_hup)
        self.wait_started()
        os.killpg(process.pid, signal.SIGINT)
        os.killpg(process.pid, signal.SIGHUP)
        time.sleep(1)
        self.assertIsNone(process.poll(), "an ignored INT or HUP ended the build")
        process.send_signal(signal.SIGTERM)
        _out, err = process.communicate(timeout=25)
        self.assertEqual(process.returncode, 143, err)
        self.assertEqual((self.audit / "traps").read_text().splitlines(), ["trap"])

    def test_a_term_sent_to_the_wrapper_is_passed_on(self) -> None:
        process = self.start("trapped")
        self.wait_started()
        process.send_signal(signal.SIGTERM)
        _out, err = process.communicate(timeout=25)
        self.assertEqual(process.returncode, 143, err)
        self.assertEqual((self.audit / "traps").read_text().splitlines(), ["trap"])

    def run_into_closed_reader(self, mode: str, *, terminal: bool = False) -> tuple[int, str]:
        # Review EDGE-2: '... | head -1' (a pipe closed early) or a hung-up terminal (a pty whose master closed, EIO).
        # The wrapper must stop echoing, keep draining and return the builder's status, not Python's 120.
        reader, writer = os.openpty() if terminal else os.pipe()
        process = subprocess.Popen([sys.executable, str(self.wrapper), str(self.audit), "comment"], text=True,
                                   stdout=writer, stderr=subprocess.PIPE,
                                   env=environment(STUB_MODE=mode, HOME=str(self.home)))
        os.close(writer)
        os.read(reader, 64)
        os.close(reader)
        _out, err = process.communicate(timeout=60)
        return process.returncode, err

    def test_a_reader_that_closes_early_does_not_change_the_exit_status(self) -> None:
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                status, err = self.run_into_closed_reader("many", terminal=terminal)
                self.assertEqual(status, 0, err)
                self.assertNotIn("Exception ignored", err)
                self.assertNotIn("Traceback", err)
                shutil.rmtree(self.diagnostics, ignore_errors=True)
                status, err = self.run_into_closed_reader("manyfail", terminal=terminal)
                self.assertEqual(status, 3, err)
                self.assertNotIn("Exception ignored", err)
                self.assertIn("build_docx.sh failed (exit 3) at stage pandoc", err)
                kept = (self.diagnostics / "last_failure_output.txt").read_text(encoding="utf-8")
                self.assertIn("line 2999 of the build", kept)       # drained to the end after the reader left
                shutil.rmtree(self.diagnostics, ignore_errors=True)

    def test_a_signal_the_builder_does_not_trap_gives_128_plus_its_number(self) -> None:
        process = self.start("untrapped")
        self.wait_started()
        process.send_signal(signal.SIGTERM)
        _out, err = process.communicate(timeout=25)
        self.assertEqual(process.returncode, 128 + signal.SIGTERM, err)


if __name__ == "__main__":
    unittest.main()
