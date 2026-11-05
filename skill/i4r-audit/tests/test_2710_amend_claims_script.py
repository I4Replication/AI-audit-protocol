"""2.7.10: amend_claims_script.py's plain amendment runs on --rebuild-documents's machinery, and both modes survive an
interruption.

Reviews of the candidate found that the plain amendment checked the published set only after its commit point (no
rehearsal), could not resume a run cut off between the note and the receipt, republished metadata/ by hand with a
delivery of its own and bare renames, and refused an audit with older documents by repeating a remedy that damages a
published audit. They found that --rebuild-documents rolled back Ctrl-C only, could not restore a failed build's
earlier diagnostic, left a killed run's record with no way back, routed a finished amendment to its resume path for
good, and recorded absolute paths in AMENDMENT.json; and that the standalone check wrote its log into the audit, even
in a dry run. A verifier then found that a run killed while it republished the paper (SIGKILL, a power cut), in either
mode, could not be finished: the resumed --apply knew where the previous folders had gone only from the killed
process's memory, so it exited 1 with a false ERROR and wrote an AMENDMENT.json naming no previous set, for good.
Every test here builds its condition on disk around a real published audit
(tests.test_2710_rebuild_documents.published_fixture: a comment rendered by the real build_docx.sh, a claims script
this release's builder would not write, filed and published by publish_completed.sh), runs the real tool, and fails
on the candidate before the change it names.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
sys.path.insert(0, str(SKILL / "scripts"))
from tests.test_2710_rebuild_documents import (  # noqa: E402
    AUDIT_ID, ENV, JOURNAL, JOURNAL_PROJECTIONS, OLDER_HARNESS, PAPER, READY, TOOLS, digest, later_release,
    published_fixture, tree_digest)
import finalization_receipt as fr  # noqa: E402

AMEND = SKILL / "scripts/amend_claims_script.py"
FILING = "comments/ and metadata/ (filed publication set)"   # publish_completed.sh's own delivery
PROJECTION = "work/finding_projections.manifest.json"
# A test hook for build_docx.sh: in the audit named by I4R_TEST_BUILD_AUDIT (never the rehearsal's scratch copy) it
# writes its pid to I4R_TEST_BUILD_MARK, then sleeps, or fails with I4R_TEST_BUILD_MODE=fail. It sits after the
# build's traps, so a signal or a failure runs the build's own clean-up (the diagnostic, the transaction folder).
BUILD_HOOK = ("trap 'exit 143' TERM\n",
              "trap 'exit 143' TERM\n"
              'if [ -n "${I4R_TEST_BUILD_MARK:-}" ] && [ "$DIR" = "${I4R_TEST_BUILD_AUDIT:-}" ]; then\n'
              '  echo "$$" > "$I4R_TEST_BUILD_MARK"\n'
              '  if [ "${I4R_TEST_BUILD_MODE:-}" = fail ]; then exit 1; fi\n'
              "  sleep 120\n"
              "fi\n")
# a line every claims script carries: a later release whose builder writes it differently rebuilds every script
BUILDER_LINE = "## Run it in a UTF-8 locale. Every check passed, with these helpers, in the audit's verification notebook."
# A test hook for publish_completed.sh: in the audit named by I4R_TEST_PUBLISH_AUDIT (never the rehearsal's scratch
# copy), at the point I4R_TEST_PUBLISH_AT names, it writes its pid (its process group's) to I4R_TEST_PUBLISH_MARK and
# sleeps. start: before its first gate. prep: its transaction folder beside metadata/ holds the set, under its lock.
# renamed: the set is visible in comments/ and metadata/, before the checks that accept it and the delivery.
PUBLISH_HOOK = ('i4r_test_publish_hook () {\n'
                '  if [ -n "${I4R_TEST_PUBLISH_MARK:-}" ] && [ "$A" = "${I4R_TEST_PUBLISH_AUDIT:-}" ] '
                '&& [ "$1" = "${I4R_TEST_PUBLISH_AT:-}" ]; then\n'
                '    echo "$$" > "$I4R_TEST_PUBLISH_MARK"; sleep 120\n'
                '  fi\n'
                '}\n')
PUBLISH_POINTS = (('. "$SCRIPT_DIR/tree_layout.sh"\n', PUBLISH_HOOK + "i4r_test_publish_hook start\n"),
                  ('for source in "${PUBLISH_SOURCES[@]}"; do cp "$source" "$PREP/" || exit 1; done\n',
                   "i4r_test_publish_hook prep\n"),
                  ('PUBLISHED_DEST="$DEST"; PUBLISHED_CDEST="$CDEST"\n', "i4r_test_publish_hook renamed\n"))
# A run killed (SIGKILL) right after the tool moved its first published folder (comments/) into old_runs/.
KILLED_AFTER_FIRST_ARCHIVE_MOVE = (
    "import os, signal, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import amend_claims_script as tool\n"
    "real = tool.fs_retry.replace_with_retry\n"
    "def replace(source, destination, *args, **kwargs):\n"
    "    real(source, destination, *args, **kwargs)\n"
    "    if os.sep + 'old_runs' + os.sep in str(destination):\n"
    "        os.kill(os.getpid(), signal.SIGKILL)\n"
    "tool.fs_retry.replace_with_retry = replace\n"
    "sys.exit(tool.main(sys.argv[2:]))\n")
# A run killed (SIGKILL) once publish_completed.sh has filed the paper and journaled its delivery, before AMENDMENT.json.
KILLED_BEFORE_THE_RECORD = (
    "import os, signal, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import amend_claims_script as tool\n"
    "def write_record(*args, **kwargs):\n"
    "    os.kill(os.getpid(), signal.SIGKILL)\n"
    "tool.write_record = write_record\n"
    "sys.exit(tool.main(sys.argv[2:]))\n")


def signals_at_default() -> None:
    """preexec_fn for every run of the tool here: SIGINT and SIGHUP at their defaults, as a command typed at a terminal
    has them. A runner started with nohup (or with & from a non-interactive shell) would pass them on ignored, and the
    tool keeps an ignored SIGHUP (nohup's), so a test's outcome would depend on how the suite was launched."""
    for number in (signal.SIGINT, signal.SIGHUP):
        signal.signal(number, signal.SIG_DFL)


def hooked_release(target: Path, *, documents: bool = False) -> Path:
    """A copy of this skill whose publish_completed.sh carries PUBLISH_HOOK (and whose document_release.py differs,
    as the next release's would, with documents=True)."""
    amend = later_release(target, append=("document_release.py",) if documents else ())
    script = target / "scripts/publish_completed.sh"
    text = script.read_text(encoding="utf-8")
    for anchor, hook in PUBLISH_POINTS:
        if text.count(anchor) != 1:
            raise AssertionError(f"publish_completed.sh no longer contains {anchor!r} once")
        text = text.replace(anchor, anchor + hook)
    script.write_text(text, encoding="utf-8")
    return amend


def folders(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_dir() and not p.is_symlink())


def strings(value: object):
    if isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


def wait_for(predicate, timeout: float, what: str, process: subprocess.Popen | None = None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if process is not None and process.poll() is not None:
            out, err = process.communicate()
            raise AssertionError(f"the tool exited {process.returncode} before {what}:\n{out}\n{err}")
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def gone(pid: int, group: bool = False) -> bool:
    try:
        (os.killpg if group else os.kill)(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


@unittest.skipUnless(READY, "pandoc, LibreOffice, Poppler and R are required")
class AmendmentFixture(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._fixture = tempfile.TemporaryDirectory(prefix="i4r-2710-amend-fixture-")
        cls.fixture = Path(cls._fixture.name) / "base"
        cls.fixture.mkdir()
        published_fixture(cls.fixture)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._fixture.cleanup()

    def setUp(self) -> None:
        work = tempfile.TemporaryDirectory(prefix="i4r-2710-amend-")
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.base = self.work / "base"
        shutil.copytree(self.fixture, self.base, symlinks=True)
        self.tree = self.base / "Discipline"
        self.audit = self.tree / "runs" / JOURNAL / PAPER / "audit"
        self.comments = self.tree / "comments" / JOURNAL / PAPER
        self.metadata = self.tree / "metadata" / JOURNAL / PAPER
        self.registry = self.base / "_registry"
        self.tmp = self.work / "tmp"   # the tool's TMPDIR: its scratch folders must not outlive it
        self.tmp.mkdir()

    def env(self, **extra: str) -> dict[str, str]:
        return {**ENV, "TMPDIR": str(self.tmp), **extra}

    def tool(self, *args: str, script: Path = AMEND, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(script), str(self.audit), *args], capture_output=True, text=True,
                              env=env or self.env(), timeout=900, preexec_fn=signals_at_default)

    def start(self, *args: str, script: Path = AMEND, env: dict[str, str] | None = None) -> subprocess.Popen:
        process = subprocess.Popen([sys.executable, str(script), str(self.audit), *args], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=env or self.env(),
                                   preexec_fn=signals_at_default)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def receipt_check(self, scripts: Path = SKILL / "scripts") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(scripts / "finalization_receipt.py"), "check",
                               "--require-registry", "--require-publication", str(self.audit)],
                              capture_output=True, text=True, env=ENV, timeout=300)

    def events(self) -> list[dict]:
        return [json.loads(line) for line in (self.audit / "run.events.jsonl").read_text().splitlines()]

    def record(self) -> dict:
        records = list(self.audit.glob("amendments/*-claims_script/AMENDMENT.json"))
        self.assertEqual(len(records), 1, records)
        return json.loads(records[0].read_text())

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.tmp.iterdir() if p.name.startswith("i4r-amend-"))

    def assert_no_local_path(self, record: dict) -> None:
        for text in strings(record):
            self.assertFalse(text.startswith("/"), text)
            for local in (str(self.work), str(self.work.resolve()), str(Path.home())):
                self.assertNotIn(local, text)

    def assert_republished(self, comments_before: dict, metadata_before: dict, *, comment_unchanged: bool,
                           scripts: Path = SKILL / "scripts") -> None:
        check = self.receipt_check(scripts)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        manifest = json.loads((self.metadata / "release-manifest.json").read_text())
        files = {row["file"]: row["sha256"] for row in manifest["files"]}
        on_disk = {**tree_digest(self.comments), **tree_digest(self.metadata)}
        on_disk.pop("release-manifest.json")
        self.assertEqual(on_disk, files, "the published folders hold exactly the release manifest's files")
        for name, own in (("comment.pdf", "report/comment.pdf"), ("comment.docx", "report/comment.docx"),
                          ("claims_script.R", "claims_script.R"), ("finalization_receipt.json", fr.RECEIPT),
                          ("document_build_manifest.json", "document_build_manifest.json")):
            self.assertEqual(files[name], digest(self.audit / own), name)
        if comment_unchanged:
            self.assertEqual(tree_digest(self.comments), comments_before, "comments/ is not byte for byte the same")
        archives = list(self.tree.glob("old_runs/archived_*_claims_amendment"))
        self.assertEqual(len(archives), 1, archives)
        self.assertEqual(tree_digest(archives[0] / "comments" / JOURNAL / PAPER), comments_before)
        self.assertEqual(tree_digest(archives[0] / "metadata" / JOURNAL / PAPER), metadata_before)


class PlainAmendmentTest(AmendmentFixture):
    """Without --rebuild-documents: only the claims script and its projection row change."""

    def test_the_dry_run_rehearses_the_whole_amendment_and_changes_nothing(self) -> None:
        # candidate: the dry run built and ran the script only; nothing was rehearsed
        (self.audit / "pkg").chmod(0o755)   # the synced folder strips intake's read-only mode
        before = tree_digest(self.base)
        done = self.tool()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("claims script runs on its own: 1 checks, 1 PASS, 0 FAIL", done.stdout)
        self.assertIn("pkg/: 1 entries have write permission", done.stdout)
        self.assertIn("rehearsal: claims script replaced, note journaled, receipt created and filed, publication "
                      "check passed, publish_completed.sh passed (privacy, check 3c, recount) in a scratch tree, and "
                      "comments/ came out byte for byte the same", done.stdout)
        self.assertIn("dry run: nothing changed (--apply to replace the claims script and republish comments/ and "
                      "metadata/)", done.stdout)
        self.assertEqual(tree_digest(self.base), before, "the dry run wrote outside its scratch folders")
        self.assertEqual((self.audit / "pkg").stat().st_mode & 0o777, 0o755, "the dry run changed pkg/'s mode")
        self.assertEqual(self.leftovers(), [], "a scratch folder outlived the run")

    def test_apply_republishes_both_folders_with_the_comment_byte_for_byte_and_once(self) -> None:
        # candidate: metadata/ was rebuilt by hand and a delivery of the tool's own was journaled; comments/ stayed
        (self.audit / "pkg").chmod(0o755)
        audit_before = tree_digest(self.audit)
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        pointer_before = json.loads((self.registry / AUDIT_ID / "current.json").read_text())
        done = self.tool("--apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("pkg/: 1 entries made read-only again for the archive gate", done.stdout)
        self.assertIn("amended and republished", done.stdout)
        self.assertIn("comments/ is byte for byte the same", done.stdout)
        self.assert_republished(comments_before, metadata_before, comment_unchanged=True)
        for name in ("comment.json", "document_build_manifest.json"):
            self.assertEqual(digest(self.metadata / name), metadata_before[name], name)
        pointer = json.loads((self.registry / AUDIT_ID / "current.json").read_text())
        self.assertNotEqual(pointer["version"], pointer_before["version"], "the audit was not re-filed")
        audit_check = subprocess.run([sys.executable, str(SKILL / "scripts/finalization_receipt.py"),
                                      "registry-audit", str(self.registry)], capture_output=True, text=True, env=ENV)
        self.assertEqual(audit_check.returncode, 0, audit_check.stdout + audit_check.stderr)

        after = tree_digest(self.audit)
        changed = {name for name in audit_before if after.get(name) != audit_before[name]}
        self.assertEqual(changed - JOURNAL_PROJECTIONS, {"claims_script.R", PROJECTION, fr.RECEIPT})
        created = set(after) - set(audit_before)
        self.assertTrue(created and all(name.startswith("amendments/") for name in created), created)
        self.assertNotIn(OLDER_HARNESS, (self.audit / "claims_script.R").read_text())
        old_rows = {r["path"]: r["sha256"] for r in json.loads(
            (next(self.audit.glob("amendments/*-claims_script")) / PROJECTION).read_text())["artifacts"]}
        new_rows = {r["path"]: r["sha256"] for r in json.loads((self.audit / PROJECTION).read_text())["artifacts"]}
        self.assertEqual({k for k in old_rows.keys() | new_rows.keys() if old_rows.get(k) != new_rows.get(k)},
                         {"claims_script.R"}, "only the claims script's projection row is re-bound")

        events = self.events()
        self.assertEqual([event["type"] for event in events[-2:]], ["note", "delivery"])
        note, delivery = events[-2]["payload"], events[-1]["payload"]
        self.assertEqual((note["kind"], note["documents"], note["path"]),
                         ("claims_script_amendment", "unchanged", "claims_script.R"))
        self.assertEqual((note["from_sha256"], note["to_sha256"]),
                         (audit_before["claims_script.R"], after["claims_script.R"]))
        self.assertRegex(note["record"], r"^amendments/[0-9TZ-]+-claims_script$")
        self.assertEqual(delivery["recipient"], FILING, "the republication is publish_completed.sh's own filing")
        self.assertEqual(delivery["artifact_sha256"], digest(self.metadata / "release-manifest.json"))

        record = self.record()
        self.assertEqual((record["mode"], record["comments_unchanged"]), ("metadata_only", True))
        self.assertEqual(record["claims_script"]["to_sha256"], after["claims_script.R"])
        self.assertEqual(record["pkg_made_read_only"], 1)
        self.assert_no_local_path(record)

        published = {**tree_digest(self.comments), **tree_digest(self.metadata)}
        journal = (self.audit / "run.events.jsonl").read_bytes()
        again = self.tool("--apply")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("already amended and republished: nothing to do", again.stdout)
        self.assertEqual({**tree_digest(self.comments), **tree_digest(self.metadata)}, published)
        self.assertEqual((self.audit / "run.events.jsonl").read_bytes(), journal)
        self.assertEqual(json.loads((self.registry / AUDIT_ID / "current.json").read_text())["version"],
                         pointer["version"], "a second --apply filed the audit again")

    def test_an_interruption_between_the_note_and_the_receipt_is_resumed(self) -> None:
        # candidate: SIGTERM killed the run with no message, and every later run refused ("receipt is stale")
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        lock = (self.registry / ".registry.lock").open("a+")   # a teammate's filing holds the registry
        self.addCleanup(lock.close)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        errors = self.work / "apply.err"
        with errors.open("w") as stderr:
            process = subprocess.Popen([sys.executable, str(AMEND), str(self.audit), "--apply"],
                                       stdout=subprocess.PIPE, stderr=stderr, text=True, env=self.env(),
                                       preexec_fn=signals_at_default)
            self.addCleanup(lambda: process.poll() is None and process.kill())
            wait_for(lambda: "waiting: the registry lock" in errors.read_text(), 600, "the registry lock wait",
                     process)
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=300)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        text = errors.read_text()
        self.assertEqual(process.returncode, 143, text)
        self.assertIn("stopped after the amendment was journaled: run --apply again to finish (it resumes)", text)
        self.assertIn("stopped by SIGTERM", text)
        self.assertNotEqual(self.receipt_check().returncode, 0, "the receipt should still bind the old script")

        dry = self.tool()
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("an amendment of the claims script is journaled", dry.stdout)
        self.assertIn("the receipt was not created again after it", dry.stdout)
        self.assertIn("dry run: --apply finishes it (receipt, registry, publication)", dry.stdout)
        resumed = self.tool("--apply")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("amended and republished", resumed.stdout)
        self.assert_republished(comments_before, metadata_before, comment_unchanged=True)
        notes = [e for e in self.events() if e["type"] == "note"
                 and e["payload"].get("kind") == "claims_script_amendment"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(self.record()["claims_script"]["to_sha256"], digest(self.audit / "claims_script.R"))

    def test_a_refused_publication_puts_both_folders_back_and_apply_resumes(self) -> None:
        # candidate: metadata/ was swapped by hand; publish_completed.sh and its gates never ran
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        lock = self.metadata.parent / f"{PAPER}.publish.lock"
        lock.write_text(f"{os.getpid()}\n")   # a live publication of this paper holds it
        done = self.tool("--apply")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("publish_completed.sh refused the amended audit; the previous comments/ and metadata/ folders "
                      "are back in place", done.stdout)
        self.assertEqual(tree_digest(self.comments), comments_before)
        self.assertEqual(tree_digest(self.metadata), metadata_before)
        self.assertEqual([p for p in self.tree.glob("old_runs/**/*") if p.is_file()], [])
        self.assertEqual(list(self.tree.glob("old_runs/*")), [], "the archive folders the move back left empty")
        check = self.receipt_check()
        self.assertEqual(check.returncode, 0, "the audit itself stays amended: " + check.stdout + check.stderr)

        lock.unlink()
        resumed = self.tool("--apply")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("an amendment of the claims script is journaled", resumed.stdout)
        self.assertIn("amended and republished", resumed.stdout)
        self.assert_republished(comments_before, metadata_before, comment_unchanged=True)
        self.assertTrue(self.record()["comments_unchanged"])

    def test_a_comment_older_document_scripts_built_is_refused_naming_rebuild_documents(self) -> None:
        # candidate: refused with document_release's remedy (rebuild with build_docx.sh, finalize again), which
        # breaks a published audit's receipt
        amend = later_release(self.work / "release")
        before = tree_digest(self.base)
        for args in ((), ("--apply",)):
            done = self.tool(*args, script=amend)
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn("were built by older document scripts", done.stdout)
            self.assertIn("Amend this audit with --rebuild-documents", done.stdout)
            self.assertNotIn("Rebuild the document with build_docx.sh", done.stdout + done.stderr)
            self.assertNotIn("re-run finalize_run.sh", done.stdout + done.stderr)
        self.assertEqual(tree_digest(self.base), before)

    def test_the_standalone_check_never_writes_into_the_audit(self) -> None:
        # candidate: check_claims_script.sh kept a failed run's log in <audit>/work/claims_check.log, in a dry run too
        fake = self.work / "fakebin"
        fake.mkdir()
        (fake / "Rscript").write_text("#!/bin/bash\necho 'Error: a package this host lacks' >&2\nexit 1\n")
        (fake / "Rscript").chmod(0o755)
        env = self.env(PATH=f"{fake}{os.pathsep}{ENV['PATH']}")
        before = tree_digest(self.base)
        for script, args in ((AMEND, ()), (AMEND, ("--apply",)),
                             (later_release(self.work / "release"), ("--rebuild-documents",))):
            done = self.tool(*args, script=script, env=env)
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn("refused: the rebuilt claims script does not run cleanly on its own", done.stdout)
            self.assertFalse((self.audit / "work/claims_check.log").exists(), "the check wrote into the audit")
            self.assertEqual(tree_digest(self.base), before)
        self.assertEqual(self.leftovers(), [])

    def test_a_file_apply_must_write_that_is_read_only_is_refused_before_any_change(self) -> None:
        # candidate: --apply copied the new script over it, failed, and the rollback itself failed with a traceback
        (self.audit / "claims_script.R").chmod(0o444)
        before = tree_digest(self.base)
        for args in ((), ("--apply",)):
            done = self.tool(*args)
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn("refused: --apply changes these and this user may not write them: "
                          f"runs/{JOURNAL}/{PAPER}/audit/claims_script.R; nothing was changed", done.stdout)
        self.assertEqual(tree_digest(self.base), before)
        self.assertFalse((self.audit / "amendments").exists())

    def test_a_read_only_archive_folder_is_refused_before_any_change(self) -> None:
        # candidate: the dry run passed, and --apply journaled its note before the move into the folder failed
        day = time.strftime("%Y-%m-%d", time.gmtime())
        archive = self.tree / "old_runs" / f"archived_{day}_claims_amendment"
        (archive / "comments" / "Another-Journal" / "Another-Paper").mkdir(parents=True)   # a teammate's amendment
        archive.chmod(0o555)   # synced without write permission
        self.addCleanup(archive.chmod, 0o755)
        before, events = tree_digest(self.base), len(self.events())
        for args in ((), ("--apply",)):
            done = self.tool(*args)
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn("refused: --apply changes these and this user may not write them: "
                          f"old_runs/archived_{day}_claims_amendment; nothing was changed", done.stdout)
        self.assertEqual(tree_digest(self.base), before)
        self.assertEqual(len(self.events()), events, "nothing was journaled")
        self.assertFalse((self.audit / "amendments").exists())

    def test_a_hangup_during_the_rehearsal_removes_its_scratch_folders(self) -> None:
        # candidate: there was no rehearsal, and SIGHUP killed the run without removing its scratch folder
        mark = self.work / "rscript.pid"
        slow = self.work / "slowbin"
        slow.mkdir()
        (slow / "Rscript").write_text(
            "#!/bin/bash\n"   # the rehearsal's publish_completed.sh runs the claims script in the scratch copy
            f'case "${{I4R_PKG_DIR:-}}" in *i4r-amend-rehearsal-*) echo "$$" > "{mark}"; sleep 120 ;; esac\n'
            f'exec "{TOOLS["Rscript"]}" "$@"\n')
        (slow / "Rscript").chmod(0o755)
        before = tree_digest(self.base)
        process = self.start(env=self.env(PATH=f"{slow}{os.pathsep}{ENV['PATH']}"))
        wait_for(lambda: mark.is_file() and mark.read_text().strip().isdigit(), 600, "the rehearsal's claims run",
                 process)
        process.send_signal(signal.SIGHUP)
        out, err = process.communicate(timeout=300)
        self.assertEqual(process.returncode, 129, out + err)
        self.assertIn("stopped by SIGHUP", err)
        self.assertEqual(tree_digest(self.base), before)
        self.assertEqual(self.leftovers(), [], "a scratch folder outlived the run")
        wait_for(lambda: gone(int(mark.read_text())), 30, "the rehearsal's child processes to end")


class RepublicationInterruptionTest(AmendmentFixture):
    """A run killed while it republishes the paper is finished by the next --apply, in either mode, which takes the
    previous sets from the archive folders the plan named before anything moved."""

    def setUp(self) -> None:
        super().setUp()
        self.mark = self.work / "publish.pid"

    def hooked(self, at: str) -> dict[str, str]:
        return self.env(I4R_TEST_PUBLISH_MARK=str(self.mark), I4R_TEST_PUBLISH_AUDIT=os.path.realpath(self.audit),
                        I4R_TEST_PUBLISH_AT=at)

    def publish_started(self, process: subprocess.Popen) -> int:
        wait_for(lambda: self.mark.is_file() and self.mark.read_text().strip().isdigit(), 900,
                 "publish_completed.sh in the audit", process)
        group = int(self.mark.read_text())
        self.addCleanup(self.end_group, group)
        return group

    @staticmethod
    def end_group(group: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, signal.SIGKILL)

    def power_cut(self, process: subprocess.Popen, group: int) -> None:
        """SIGKILL to the tool and to the publication's whole process group: nothing cleans up."""
        process.kill()
        process.communicate(timeout=60)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(group, signal.SIGKILL)
        wait_for(lambda: gone(group, group=True), 60, "the publication's processes to end")

    def archived(self, kind: str) -> Path:
        found = list(self.tree.glob(f"old_runs/archived_*_claims_amendment/{kind}/{JOURNAL}/{PAPER}"))
        self.assertEqual(len(found), 1, found)
        return found[0]

    def assert_finished(self, done: subprocess.CompletedProcess, comments_before: dict, metadata_before: dict, *,
                        script: Path, documents: bool = False) -> dict:
        """--apply finished the amendment: the paper republished once, AMENDMENT.json naming the previous sets, and a
        further --apply has nothing to do."""
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("ERROR", done.stdout + done.stderr)
        self.assertIn("amended and republished", done.stdout)
        self.assert_republished(comments_before, metadata_before, comment_unchanged=not documents,
                                scripts=script.parent)
        record = self.record()
        for kind in ("comments", "metadata"):
            self.assertEqual(record[f"previous_{kind}"], self.archived(kind).relative_to(self.tree).as_posix())
        if documents:
            self.assertNotIn("comments_unchanged", record)
        else:
            self.assertIn("comments/ is byte for byte the same", done.stdout)
            self.assertTrue(record["comments_unchanged"])
        self.assert_no_local_path(record)
        events = self.events()
        notes = [index for index, event in enumerate(events) if event["type"] == "note"
                 and event["payload"].get("kind") == "claims_script_amendment"]
        self.assertEqual(len(notes), 1)
        deliveries = [index for index, event in enumerate(events) if index > notes[0] and event["type"] == "delivery"]
        self.assertEqual(len(deliveries), 1, "publish_completed.sh journals one filing after the note")
        delivery = events[deliveries[0]]["payload"]
        self.assertEqual((delivery["recipient"], delivery["artifact_sha256"]),
                         (FILING, digest(self.metadata / "release-manifest.json")))
        self.assertEqual(record["delivery_event"], deliveries[0] + 1)
        again = self.tool("--apply", *(("--rebuild-documents",) if documents else ()), script=script)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("already amended and republished: nothing to do", again.stdout)
        return record

    def test_a_power_cut_while_publish_completed_runs_is_finished_by_apply(self) -> None:
        # candidate: the resumed --apply exited 1 ("the republished comments/ is not byte for byte the previous one
        # ((no previous comments/ was archived))") and wrote previous_comments=null, previous_metadata=null and
        # comments_unchanged=false, which every later --apply accepted as finished
        amend = hooked_release(self.work / "release")
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        process = self.start("--apply", script=amend, env=self.hooked("prep"))
        self.power_cut(process, self.publish_started(process))
        self.assertFalse(self.comments.exists() or self.metadata.exists(), "nothing is published")
        self.assertEqual(tree_digest(self.archived("comments")), comments_before)
        self.assertEqual(tree_digest(self.archived("metadata")), metadata_before)
        transaction = list(self.metadata.parent.glob(f".{PAPER}.publish.*"))
        lock = self.metadata.parent / f"{PAPER}.publish.lock"
        self.assertEqual(len(transaction), 1, "publish_completed.sh's transaction folder, with a copy of the set")
        self.assertTrue(lock.is_file(), "publish_completed.sh's lock")
        notebook = self.audit / "work/.verification-regenerated.cut0ff.Rmd"   # a cut during its --check leaves one
        notebook.write_text("partial\n")
        before = tree_digest(self.base)

        dry = self.tool(script=amend)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("an amendment of the claims script is journaled", dry.stdout)
        for kind in ("comments", "metadata"):
            self.assertIn(f"{kind}/{JOURNAL}/{PAPER}: nothing is published there; the previous set is in "
                          f"{self.archived(kind).relative_to(self.tree).as_posix()}", dry.stdout)
        self.assertIn("left by the publish_completed.sh run that was stopped", dry.stdout)
        for leftover in (transaction[0], lock, notebook):
            self.assertIn(leftover.relative_to(self.tree).as_posix(), dry.stdout)
        self.assertIn("dry run: the publication is not the amended one yet; --apply republishes it", dry.stdout)
        self.assertEqual(tree_digest(self.base), before, "the dry run changed something")

        done = self.tool("--apply", script=amend)
        self.assertIn("removed what the stopped publish_completed.sh left", done.stdout)
        self.assertEqual(list(self.metadata.parent.glob(f".{PAPER}.*")) + list(self.comments.parent.glob(".*")), [])
        self.assertFalse(lock.exists() or notebook.exists())
        record = self.assert_finished(done, comments_before, metadata_before, script=amend)
        self.assertEqual(len(record["removed_leftovers"]), 3, record["removed_leftovers"])

    def test_the_tool_killed_alone_waits_for_the_publication_it_started(self) -> None:
        # candidate: the next --apply ran beside the orphaned publish_completed.sh, which still held the paper
        amend = hooked_release(self.work / "release")
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        process = self.start("--apply", script=amend, env=self.hooked("start"))
        group = self.publish_started(process)
        process.kill()   # SIGKILL to the tool only: its publish_completed.sh goes on in its own process group
        process.communicate(timeout=60)
        try:
            before = tree_digest(self.base)
            for args in ((), ("--apply",)):
                held = self.tool(*args, script=amend)
                self.assertEqual(held.returncode, 1, held.stdout + held.stderr)
                self.assertIn("refused: publish_completed.sh, which an earlier --apply of this amendment started, is "
                              f"still running (process group {group}); wait for it to end, or end it with: "
                              f"kill -TERM -- -{group}", held.stdout)
            self.assertEqual(tree_digest(self.base), before)
        finally:   # the operator ends it as the refusal says
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGTERM)
            wait_for(lambda: gone(group, group=True), 60, "the publication to end")
        done = self.tool("--apply", script=amend)
        self.assert_finished(done, comments_before, metadata_before, script=amend)

    def test_a_filing_cut_off_before_publish_completed_accepted_it_is_published_again(self) -> None:
        # candidate: the visible set was taken as finished although publish_completed.sh never accepted it, and its
        # delivery was never journaled
        amend = hooked_release(self.work / "release")
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        process = self.start("--apply", script=amend, env=self.hooked("renamed"))
        self.power_cut(process, self.publish_started(process))
        self.assertTrue(self.comments.is_dir() and self.metadata.is_dir(), "the set was made visible")
        self.assertEqual(self.events()[-1]["type"], "note", "and never accepted: no delivery")
        claims = self.metadata / "claims_script.R"
        filed = claims.read_bytes()
        claims.chmod(0o644)
        claims.write_bytes(filed + b"# edited by hand\n")   # then a hand edit: neither set the amendment knows
        conflict = self.tool(script=amend)
        self.assertEqual(conflict.returncode, 1, conflict.stdout + conflict.stderr)
        self.assertIn("refused: the published folders are not as an interrupted --apply leaves them: "
                      f"metadata/{JOURNAL}/{PAPER} is published again, not with the amended audit", conflict.stdout)
        claims.write_bytes(filed)

        before = tree_digest(self.base)
        dry = self.tool(script=amend)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        for kind in ("comments", "metadata"):
            self.assertIn(f"{kind}/{JOURNAL}/{PAPER}: the amended set, made visible by publish_completed.sh, which "
                          "was stopped before it accepted and journaled it", dry.stdout)
        self.assertEqual(tree_digest(self.base), before, "the dry run changed something")
        done = self.tool("--apply", script=amend)
        record = self.assert_finished(done, comments_before, metadata_before, script=amend)
        kept = [self.tree / path for path in record["unaccepted_publications"]]
        self.assertEqual(sorted(path.name for path in kept), ["comments", "metadata"])
        self.assertEqual(tree_digest(next(p for p in kept if p.name == "comments")), comments_before)
        self.assertIn(f"{PAPER}.publish.lock", " ".join(record["removed_leftovers"]))

    def test_a_kill_between_the_two_archive_moves_is_finished_by_apply(self) -> None:
        # candidate: the resumed --apply archived metadata/ only, so the record named no previous comments/
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        killed = subprocess.run([sys.executable, "-c", KILLED_AFTER_FIRST_ARCHIVE_MOVE, str(SKILL / "scripts"),
                                 str(self.audit), "--apply"], capture_output=True, text=True, env=self.env(),
                                timeout=900, preexec_fn=signals_at_default)
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        self.assertFalse(self.comments.exists())
        self.assertEqual(tree_digest(self.metadata), metadata_before, "metadata/ was not moved yet")
        archive = self.archived("comments")
        shown = archive.relative_to(self.tree).as_posix()
        dry = self.tool()
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn(f"comments/{JOURNAL}/{PAPER}: nothing is published there; the previous set is in {shown}",
                      dry.stdout)
        self.assertIn(f"metadata/{JOURNAL}/{PAPER}: the previous set is still published there (--apply archives it "
                      f"to {shown.replace('/comments/', '/metadata/')})", dry.stdout)

        away = self.work / "moved-by-a-teammate"   # the previous set is then nowhere the amendment knows
        shutil.move(str(archive), str(away))
        before = tree_digest(self.base)
        refused = self.tool("--apply")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn(f"nothing is published at comments/{JOURNAL}/{PAPER}, and the previous set is not in {shown} "
                      "either", refused.stdout)
        self.assertEqual(tree_digest(self.base), before)
        shutil.move(str(away), str(archive))
        done = self.tool("--apply")
        self.assert_finished(done, comments_before, metadata_before, script=AMEND)

    def test_a_kill_after_the_filing_before_the_record_is_finished_by_apply(self) -> None:
        # candidate: the resumed --apply found the previous folders by a search of old_runs/, and recorded neither
        # the filing's delivery nor publish_completed.sh's lines
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        killed = subprocess.run([sys.executable, "-c", KILLED_BEFORE_THE_RECORD, str(SKILL / "scripts"),
                                 str(self.audit), "--apply"], capture_output=True, text=True, env=self.env(),
                                timeout=900, preexec_fn=signals_at_default)
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        self.assertEqual(list(self.audit.glob("amendments/*/AMENDMENT.json")), [])
        delivery = max(index for index, event in enumerate(self.events()) if event["type"] == "delivery")
        dry = self.tool()
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn(f"the amended set is published in comments/{JOURNAL}/{PAPER} and metadata/{JOURNAL}/{PAPER}, "
                      f"filed by publish_completed.sh (its delivery is event {delivery + 1})", dry.stdout)
        self.assertIn("the previous metadata/ set is in "
                      f"{self.archived('metadata').relative_to(self.tree).as_posix()}", dry.stdout)
        self.assertIn("dry run: republished, but AMENDMENT.json is missing; --apply writes it", dry.stdout)
        done = self.tool("--apply")
        record = self.assert_finished(done, comments_before, metadata_before, script=AMEND)
        self.assertEqual(record["publish"], [f"published: comments/{JOURNAL}/{PAPER}",
                                             f"published: metadata/{JOURNAL}/{PAPER}"])

    def test_a_power_cut_while_publishing_a_rebuilt_comment_is_finished_by_apply(self) -> None:
        # candidate: the resumed --rebuild-documents --apply exited 0 with previous_comments=null and
        # previous_metadata=null in AMENDMENT.json, for good
        amend = hooked_release(self.work / "release", documents=True)
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        process = self.start("--rebuild-documents", "--apply", script=amend, env=self.hooked("start"))
        self.power_cut(process, self.publish_started(process))
        self.assertFalse(self.comments.exists() or self.metadata.exists(), "nothing is published")
        dry = self.tool("--rebuild-documents", script=amend)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("a --rebuild-documents amendment is journaled", dry.stdout)
        for kind in ("comments", "metadata"):
            self.assertIn(f"{kind}/{JOURNAL}/{PAPER}: nothing is published there; the previous set is in "
                          f"{self.archived(kind).relative_to(self.tree).as_posix()}", dry.stdout)
        done = self.tool("--rebuild-documents", "--apply", script=amend)
        self.assert_finished(done, comments_before, metadata_before, script=amend, documents=True)


class DocumentsInterruptionTest(AmendmentFixture):
    """--rebuild-documents with a later release whose build_docx.sh carries BUILD_HOOK."""

    def setUp(self) -> None:
        super().setUp()
        self.release = later_release(self.work / "release", edits={"build_docx.sh": BUILD_HOOK})
        self.mark = self.work / "build.pid"

    def hooked(self, mode: str = "") -> dict[str, str]:
        return self.env(I4R_TEST_BUILD_MARK=str(self.mark), I4R_TEST_BUILD_AUDIT=os.path.realpath(self.audit),
                        I4R_TEST_BUILD_MODE=mode)

    def build_started(self, process: subprocess.Popen) -> int:
        wait_for(lambda: self.mark.is_file() and self.mark.read_text().strip().isdigit(), 900,
                 "the build in the audit", process)
        return int(self.mark.read_text())

    def test_sigterm_during_the_build_in_the_audit_rolls_back(self) -> None:
        # candidate: only Ctrl-C was rolled back; SIGTERM left the new claims script under the old receipt
        before, folders_before = tree_digest(self.base), folders(self.base)
        process = self.start("--rebuild-documents", "--apply", script=self.release, env=self.hooked())
        build = self.build_started(process)
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=300)
        self.assertEqual(process.returncode, 143, out + err)
        self.assertIn("rolled back: the audit is as it was before this run", err)
        self.assertNotIn("NOT RESTORED", err)
        self.assertIn("stopped by SIGTERM", err)
        wait_for(lambda: gone(build, group=True), 30, "the build's processes to end")
        self.assertEqual(tree_digest(self.base), before, "the rollback left a change behind")
        self.assertEqual(folders(self.base), folders_before, "the rollback left a folder the run created")
        self.assertEqual(self.leftovers(), [], "a scratch folder outlived the run")

    def test_a_failed_build_in_the_audit_restores_its_earlier_diagnostic(self) -> None:
        # candidate: "NOT RESTORED: document_build_diagnostics/last_failure.json still differs"
        diagnostic = self.audit / "document_build_diagnostics/last_failure.json"
        diagnostic.parent.mkdir()
        diagnostic.write_text('{"stage": "an earlier build failure"}\n')
        before, folders_before = tree_digest(self.base), folders(self.base)
        done = self.tool("--rebuild-documents", "--apply", script=self.release, env=self.hooked("fail"))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("build_docx.sh failed in the audit", done.stderr)
        self.assertIn("rolled back: the audit is as it was before this run", done.stderr)
        self.assertNotIn("NOT RESTORED", done.stderr)
        self.assertEqual(diagnostic.read_text(), '{"stage": "an earlier build failure"}\n')
        self.assertEqual(tree_digest(self.base), before)
        self.assertEqual(folders(self.base), folders_before)

    def test_rollback_pending_restores_a_run_killed_before_its_note(self) -> None:
        # candidate: --rollback-pending did not exist, and a killed run's audit was refused by every later run
        before, folders_before = tree_digest(self.base), folders(self.base)
        process = self.start("--rebuild-documents", "--apply", script=self.release, env=self.hooked())
        build = self.build_started(process)
        process.kill()   # SIGKILL: nothing is cleaned up; the build goes on in its own process group
        process.communicate(timeout=60)
        try:
            refused = self.tool(script=self.release, env=self.hooked())
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn("stopped before it was journaled", refused.stdout)
            self.assertIn("--rollback-pending", refused.stdout)
            held = self.tool("--rollback-pending", script=self.release)
            self.assertEqual(held.returncode, 1, held.stdout + held.stderr)
            self.assertIn(f"is still running (process group {build})", held.stdout)
        finally:   # the operator ends the build the refusal names
            with contextlib.suppress(ProcessLookupError):
                os.killpg(build, signal.SIGTERM)
            wait_for(lambda: gone(build, group=True), 60, "the build to end")
        done = self.tool("--rollback-pending", script=self.release)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("rolled back: the run that wrote amendments/", done.stdout)
        self.assertEqual(tree_digest(self.base), before)
        self.assertEqual(folders(self.base), folders_before)
        again = self.tool("--rollback-pending", script=self.release)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("nothing to roll back", again.stdout)
        usage = self.tool("--rollback-pending", "--apply", script=self.release)
        self.assertEqual(usage.returncode, 2, usage.stdout + usage.stderr)

    def test_a_finished_amendment_does_not_hold_back_a_later_release(self) -> None:
        # candidate: the finished amendment's note routed every later --rebuild-documents to its resume path, and
        # AMENDMENT.json copied publish_completed.sh's absolute "published:" lines
        first = later_release(self.work / "release1")
        done = self.tool("--rebuild-documents", "--apply", script=first)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        record = self.record()
        self.assertEqual(record["publish"], [f"published: comments/{JOURNAL}/{PAPER}",
                                             f"published: metadata/{JOURNAL}/{PAPER}"])
        self.assert_no_local_path(record)
        # the next release changes the document verifier again, and the claims builder
        second = later_release(self.work / "release2", append=("document_release.py", "document_release.py"),
                               edits={"make_claims_script.sh": (BUILDER_LINE, BUILDER_LINE + " (a later release)")})
        before = tree_digest(self.base)
        dry = self.tool("--rebuild-documents", script=second)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertNotIn("amendment is journaled", dry.stdout)
        self.assertIn("were built by older document scripts (document_release.py)", dry.stdout)
        self.assertIn("comment text, PDF: same text", dry.stdout)
        self.assertIn("dry run: nothing changed", dry.stdout)
        self.assertEqual(tree_digest(self.base), before)


if __name__ == "__main__":
    unittest.main()
