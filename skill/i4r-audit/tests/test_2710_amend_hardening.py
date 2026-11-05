"""2.7.10: amend_claims_script.py refuses rather than guesses when it cannot know where an amendment stands.

An independent re-check of the tool's crash recovery found these. A journal that could not be read was read as empty,
so a journaled amendment looked like one stopped before its note, and --rollback-pending put the claims script and the
receipt back under the note. Nothing stopped two runs on one audit, and a run applied what it had rehearsed although
the claims script had changed meanwhile: a second note, a second filing, and every later run refused. A run cut off
between a journal write and its projections could never be resumed ("run.json journal watermark differs from the
canonical journal tip"). A signal could cut the registry filing, a rollback or a put-back in two, and hid
publish_completed.sh's refusal. A run started with nohup stopped when its terminal closed. After a reboot, a run took
its own pid or process group for the earlier run's and refused for good.

A verification of those fixes found more. Holding signals through the registry filing also held them while the run
waited, without a time limit, for another process's registry lock: only SIGKILL stopped it, and it said nothing of the
signal it held. A rollback before the note read the journal first, so an unreadable journal left the claims script
replaced. A record whose note the journal had lost sent --apply and --rollback-pending to each other. Any error
locking the audit folder was taken for another run. A signal while a timed-out child's group was ended cut that short.
A run started with SIGINT ignored (a non-interactive shell's &) kept it ignored: Ctrl-C did nothing, and its children
inherited it, so the SIGINT that ends their group reached none of their clean-up.

Each test runs the real tool on a published audit (tests.test_2710_rebuild_documents.published_fixture, through
tests.test_2710_amend_claims_script.AmendmentFixture), or calls the module's own functions in this process, and fails
on the candidate before the change it names. Where a run must stop, pause or meet a change at one point, a python -c
wrapper wraps one of the tool's own functions in the tool's process, for the audit only (never the rehearsal's scratch
copy), as the KILLED_* wrappers of test_2710_amend_claims_script do; no script of the skill carries a test hook. Every
run of the tool starts with SIGINT and SIGHUP at their defaults (amend_tests.signals_at_default) unless a test sets
them otherwise on purpose, so no outcome depends on how the suite was launched (nohup, &).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import io
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
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
sys.path.insert(0, str(SKILL / "scripts"))
from tests import test_2710_amend_claims_script as amend_tests  # noqa: E402
from tests.test_2710_rebuild_documents import (  # noqa: E402
    AUDIT_ID, ENV, JOURNAL, JOURNAL_PROJECTIONS, PAPER, TOOLS, digest, later_release, tree_digest)
import amend_claims_script as tool  # noqa: E402
import lifecycle_journal  # noqa: E402

AMEND, SCRIPTS = amend_tests.AMEND, SKILL / "scripts"
folders, wait_for, gone = amend_tests.folders, amend_tests.wait_for, amend_tests.gone
signals_at_default = amend_tests.signals_at_default
HELD = (f"{PAPER}: refused: another amend_claims_script.py run holds this audit (one run per audit at a time; find it "
        "with: pgrep -fl amend_claims_script); nothing was done")
UNREADABLE = "refused: the audit's journal cannot be read ("
MAKE_IT_READABLE = ("nothing more was done. Make run.events.jsonl readable again (a partial last line a crash left, its "
                    "permissions, or a copy from Dropbox's version history), then run this again.")
CHANGED = ("refused: the claims script changed while this run built and rehearsed (another run amended the audit "
           "meanwhile); nothing was changed. Run this again.")
STOPPED_AFTER_THE_NOTE = "stopped after the amendment was journaled: run --apply again to finish (it resumes)"
ALREADY_STOPPING = "SIGTERM: already stopping; the clean-up runs to its end"
HELD_UNTIL = "SIGTERM: held until the step under way ends; the run stops then"
STOPPED_BEFORE_THE_NOTE = "an amendment of this audit stopped before it was journaled"
LOCKED_HERE = (f"refused: the audit folder cannot be locked here ({os.strerror(errno.ENOTSUP)}), so a second run could "
               "not be kept out; nothing was done")


PART_WAY = "stopped part way: the audit's journal cannot be read ("


def stopped_part_way(record: str) -> str:
    """The rest of the part-way line, after the journal's reason."""
    return (f"so whether the note landed cannot be told and nothing was put back; {record}/ holds the backups. Make "
            "run.events.jsonl readable again (a partial last line a crash left, its permissions, or a copy from "
            "Dropbox's version history): --apply then resumes the amendment if its note landed; otherwise it refuses, "
            "and --rollback-pending puts the audit back.")


def lost_note(record: str) -> str:
    return (f"refused: {record}/ belongs to an amendment whose note was journaled (its plan was written after the note), "
            "yet no note in run.events.jsonl names it: the journal has lost events (an older copy restored, or one "
            "replaced by a sync?). Nothing was changed. Do not remove the folder: put back the run.events.jsonl that "
            "holds the note, and --apply then finishes the amendment.")


# python -c wrappers. argv[1] is the scripts folder the tool is imported from, argv[2:] the tool's own arguments.
PRELUDE = ("import json, os, pathlib, signal, sys, time\n"
           "sys.path.insert(0, sys.argv[1])\n"
           "import amend_claims_script as tool\n"
           "AUDIT = os.path.realpath(sys.argv[2])\n")
MAIN = "sys.exit(tool.main(sys.argv[2:]))\n"
# pause(): the first call writes the run's pid to $I4R_TEST_PAUSE_MARK and waits for <mark>.go, in the run's main
# thread, where its signal handler runs as anywhere else; later calls return at once.
PAUSE = PRELUDE + (
    "MARK = pathlib.Path(os.environ['I4R_TEST_PAUSE_MARK'])\n"
    "def pause():\n"
    "    if MARK.exists():\n"
    "        return\n"
    "    MARK.write_text(f'{os.getpid()}\\n')\n"
    "    deadline = time.monotonic() + 600\n"
    "    while not MARK.with_name(MARK.name + '.go').exists() and time.monotonic() < deadline:\n"
    "        time.sleep(0.05)\n")
# unreadable(): the audit's journal made unreadable in the middle of the run, after its first reads. $I4R_TEST_UNREADABLE
# says how: "torn" (a power cut in the middle of an append left a last line cut off) or "mode" (no read permission).
# With $I4R_TEST_SNAPSHOT set, the hashes of every file under $I4R_TEST_BASE at that moment are written there.
UNREADABLE_JOURNAL = PRELUDE + (
    "def unreadable():\n"
    "    journal = pathlib.Path(AUDIT) / 'run.events.jsonl'\n"
    "    if os.environ['I4R_TEST_UNREADABLE'] == 'torn':\n"
    "        with journal.open('ab') as handle:\n"
    "            handle.write(b'{\"schema_version\": \"2.1.0\", \"seq\": 9999, \"at\": \"2026-10-30T00:0')\n"
    "    else:\n"
    "        journal.chmod(0)\n"
    "    if os.environ.get('I4R_TEST_SNAPSHOT'):\n"
    "        hashes = tool.tree_hashes(pathlib.Path(os.environ['I4R_TEST_BASE']))\n"
    "        pathlib.Path(os.environ['I4R_TEST_SNAPSHOT']).write_text(json.dumps(hashes))\n")
# SIGKILL once the audit is backed up and its claims script replaced, before the note is journaled
KILLED_BEFORE_THE_NOTE = PRELUDE + (
    "real = tool.journal_plain_note\n"
    "def journal_plain_note(audit, *args, **kwargs):\n"
    "    if os.path.realpath(audit) == AUDIT:\n"
    "        os.kill(os.getpid(), signal.SIGKILL)\n"
    "    return real(audit, *args, **kwargs)\n"
    "tool.journal_plain_note = journal_plain_note\n") + MAIN
# SIGKILL once the note is journaled (and projected), before the receipt is created again
KILLED_AFTER_THE_NOTE = PRELUDE + (
    "real = tool.fr.create\n"
    "def create(root, *args, **kwargs):\n"
    "    if os.path.realpath(root) == AUDIT:\n"
    "        os.kill(os.getpid(), signal.SIGKILL)\n"
    "    return real(root, *args, **kwargs)\n"
    "tool.fr.create = create\n") + MAIN
# SIGKILL once publish_completed.sh has filed the paper and journaled its delivery, before AMENDMENT.json is written;
# the journal's projections as they stood before the publication are copied to $I4R_TEST_PROJECTIONS first
KILLED_BEFORE_THE_RECORD = PRELUDE + (
    "import shutil\n"
    "SAVED = pathlib.Path(os.environ['I4R_TEST_PROJECTIONS'])\n"
    "real = tool.republish\n"
    "def republish(audit, *args, **kwargs):\n"
    "    SAVED.mkdir(parents=True, exist_ok=True)\n"
    "    for name in tool.fr.JOURNAL_PROJECTIONS - {'run.events.jsonl'}:\n"
    "        if (pathlib.Path(audit) / name).is_file():\n"
    "            shutil.copy2(pathlib.Path(audit) / name, SAVED / name)\n"
    "    return real(audit, *args, **kwargs)\n"
    "tool.republish = republish\n"
    "def write_record(*args, **kwargs):\n"
    "    os.kill(os.getpid(), signal.SIGKILL)\n"
    "tool.write_record = write_record\n") + MAIN
# paused mid-run: the note journaled, the audit re-filed, the plan saved, nothing moved yet
PAUSED_BEFORE_THE_REPUBLICATION = PAUSE + (
    "real = tool.republish\n"
    "def republish(*args, **kwargs):\n"
    "    pause()\n"
    "    return real(*args, **kwargs)\n"
    "tool.republish = republish\n") + MAIN
# paused inside the registry filing of the audit: current.json and its mirrors written, index_row.json, index.jsonl
# and the receipt not yet
PAUSED_IN_THE_FILING = PAUSE + (
    "REGISTRY = os.path.realpath(os.environ['I4R_TEST_REGISTRY']) + os.sep\n"
    "real = tool.fr.atomic_bytes\n"
    "def atomic_bytes(path, data):\n"
    "    if pathlib.Path(path).name == tool.fr.INDEX_ROW and os.path.realpath(path).startswith(REGISTRY):\n"
    "        pause()\n"
    "    return real(path, data)\n"
    "tool.fr.atomic_bytes = atomic_bytes\n") + MAIN
# paused as a rollback copies its first backup back
PAUSED_IN_THE_ROLLBACK = PAUSE + (
    "real = tool.restore_file\n"
    "def restore_file(backup, target):\n"
    "    pause()\n"
    "    return real(backup, target)\n"
    "tool.restore_file = restore_file\n") + MAIN
# paused as the previous comments/ and metadata/ are moved back
PAUSED_IN_THE_PUT_BACK = PAUSE + (
    "real = tool.moved_back\n"
    "def moved_back(*args, **kwargs):\n"
    "    pause()\n"
    "    return real(*args, **kwargs)\n"
    "tool.moved_back = moved_back\n") + MAIN
# right after the rehearsal, a teammate's finished amendment of this audit arrives (a sync): the whole tree is theirs
SYNCED_AFTER_THE_REHEARSAL = PRELUDE + (
    "real = tool.rehearse\n"
    "def rehearse(*args, **kwargs):\n"
    "    report = real(*args, **kwargs)\n"
    "    os.rename(os.environ['I4R_TEST_TREE'], os.environ['I4R_TEST_TREE_ASIDE'])\n"
    "    os.rename(os.environ['I4R_TEST_SYNCED_TREE'], os.environ['I4R_TEST_TREE'])\n"
    "    return report\n"
    "tool.rehearse = rehearse\n") + MAIN
# right after the rehearsal, another run replaces the claims script and re-binds its row, as rebind_claims_script does
CHANGED_AFTER_THE_REHEARSAL = PRELUDE + (
    "real = tool.rehearse\n"
    "def rehearse(audit, *args, **kwargs):\n"
    "    report = real(audit, *args, **kwargs)\n"
    "    claims = pathlib.Path(audit) / 'claims_script.R'\n"
    "    claims.write_bytes(claims.read_bytes() + b'# re-bound by another run\\n')\n"
    "    projection = pathlib.Path(audit) / tool.PROJECTION\n"
    "    value = json.loads(projection.read_text())\n"
    "    for row in value['artifacts']:\n"
    "        if row.get('path') == claims.name:\n"
    "            row['sha256'] = tool.sha256(claims)\n"
    "    projection.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\\n')\n"
    "    return report\n"
    "tool.rehearse = rehearse\n") + MAIN
# after a reboot, this --rollback-pending holds the pid the stopped run recorded, and the process group of its build;
# its command line names this tool, as the real one's does
UNDER_THE_STOPPED_RUNS_IDS = PRELUDE + (
    "for path in pathlib.Path(AUDIT).glob('amendments/*-claims_script/AMEND_PLAN.json'):\n"
    "    plan = json.loads(path.read_text())\n"
    "    plan['runner']['pid'], plan['build_group'] = os.getpid(), os.getpgrp()\n"
    "    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + '\\n')\n") + MAIN
# the claims script replaced and its row re-bound in the audit, then the journal made unreadable, and a failure: all
# before the note
FAILS_BEFORE_THE_NOTE = UNREADABLE_JOURNAL + (
    "real = tool.rebind_claims_script\n"
    "def rebind_claims_script(audit, *args, **kwargs):\n"
    "    rebound = real(audit, *args, **kwargs)\n"
    "    if os.path.realpath(audit) == AUDIT:\n"
    "        unreadable()\n"
    "        raise ValueError('a failure injected before the note')\n"
    "    return rebound\n"
    "tool.rebind_claims_script = rebind_claims_script\n") + MAIN
# --rebuild-documents: the documents rebuilt in the audit; as their text is compared with the published one there, the
# journal made unreadable, and a failure: all before the note
FAILS_AFTER_THE_BUILD = UNREADABLE_JOURNAL + (
    "real = tool.compare_documents\n"
    "def compare_documents(old_pdf, new_pdf, *args, **kwargs):\n"
    "    if os.path.realpath(new_pdf).startswith(AUDIT + os.sep):\n"
    "        unreadable()\n"
    "        raise SystemExit('a failure injected after the build in the audit, before the note')\n"
    "    return real(old_pdf, new_pdf, *args, **kwargs)\n"
    "tool.compare_documents = compare_documents\n") + MAIN
# the journal made unreadable as the audit's note is journaled: log_event.sh's append reads it first, and refuses
NOTE_REFUSED = UNREADABLE_JOURNAL + (
    "real = tool.log_event\n"
    "def log_event(audit, *args):\n"
    "    if os.path.realpath(audit) == AUDIT and args[:1] == ('note',):\n"
    "        unreadable()\n"
    "    return real(audit, *args)\n"
    "tool.log_event = log_event\n") + MAIN
# the audit's note journaled, then the journal made unreadable, and log_event reporting a failure
NOTE_LANDED_THEN_FAILED = UNREADABLE_JOURNAL + (
    "real = tool.log_event\n"
    "def log_event(audit, *args):\n"
    "    real(audit, *args)\n"
    "    if os.path.realpath(audit) == AUDIT and args[:1] == ('note',):\n"
    "        unreadable()\n"
    "        raise SystemExit('could not journal the note event: a failure injected after the note landed')\n"
    "tool.log_event = log_event\n") + MAIN
# says, on stderr, whether the run was started with SIGINT ignored
SAYS_HOW_IT_STARTED = PRELUDE + (
    "print('started with SIGINT ' + ('ignored' if signal.getsignal(signal.SIGINT) is signal.SIG_IGN else 'not ignored'),"
    " file=sys.stderr, flush=True)\n") + MAIN
# another process holds an exclusive lock on the audit folder until it is killed
HOLDS_THE_AUDIT = ("import fcntl, os, sys, time\n"
                   "descriptor = os.open(sys.argv[1], os.O_RDONLY)\n"
                   "fcntl.flock(descriptor, fcntl.LOCK_EX)\n"
                   "print('held', flush=True)\n"
                   "time.sleep(900)\n")
# another process (a teammate's filing on this machine, or a stuck one) holds the registry lock until it is killed
HOLDS_THE_REGISTRY = ("import fcntl, sys, time\n"
                      "handle = open(sys.argv[1], 'a+')\n"
                      "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
                      "print('held', flush=True)\n"
                      "time.sleep(900)\n")
# a process whose command line names the audit (argv[1]), as a publication's or the tool's own does
NAMES_THE_AUDIT = "import sys, time\nprint('ready', flush=True)\ntime.sleep(300)\n"
# a child that takes its time over SIGINT (it handles it and goes on), and meanwhile the operator's SIGTERM reaches the
# tool (argv[1]): sent once, from its SIGINT handler
SLOW_OVER_SIGINT = ("import os, signal, sys, time\n"
                    "sent = []\n"
                    "def interrupted(number, frame):\n"
                    "    if not sent:\n"
                    "        sent.append(number)\n"
                    "        os.kill(int(sys.argv[1]), signal.SIGTERM)\n"
                    "signal.signal(signal.SIGINT, interrupted)\n"
                    "print('ready', flush=True)\n"
                    "while True:\n"
                    "    time.sleep(0.1)\n")


def fake_rscript(path: Path, mark: Path) -> None:
    """An Rscript for the tool's PATH. In the rehearsal's publish_completed.sh (I4R_PKG_DIR in its scratch copy) it
    writes its pid, its process group and whether it started with SIGINT ignored to `mark`, then sleeps; on SIGINT,
    Python's own handler (there only when SIGINT was not ignored at its start) runs its clean-up, which writes
    <mark>.cleaned, and it ends by SIGINT. Anywhere else it is the real Rscript."""
    path.write_text("\n".join((
        f"#!{sys.executable}",
        "import os, signal, sys, time",
        f"MARK = {str(mark)!r}",
        f"RSCRIPT = {TOOLS['Rscript']!r}",
        "if 'i4r-amend-rehearsal-' in os.environ.get('I4R_PKG_DIR', ''):",
        "    started = 'ignored' if signal.getsignal(signal.SIGINT) is signal.SIG_IGN else 'default'",
        "    with open(MARK + '.tmp', 'w') as handle:",
        "        handle.write(f'{os.getpid()} {os.getpgrp()} {started}\\n')",
        "    os.replace(MARK + '.tmp', MARK)",
        "    try:",
        "        time.sleep(300)",
        "    except KeyboardInterrupt:",
        "        with open(MARK + '.cleaned', 'w') as handle:",
        "            handle.write('cleaned up on SIGINT\\n')",
        "        signal.signal(signal.SIGINT, signal.SIG_DFL)",
        "        os.kill(os.getpid(), signal.SIGINT)",
        "        raise",
        "os.execv(RSCRIPT, [RSCRIPT] + sys.argv[1:])",
        "")))
    path.chmod(0o755)


def kill_group(group: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, signal.SIGKILL)


@contextlib.contextmanager
def stderr_into(path: Path):
    """File descriptor 2 written to path: Signals.handle writes there with os.write."""
    saved = os.dup(2)
    try:
        with path.open("wb") as handle:
            os.dup2(handle.fileno(), 2)
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


class SignalsAndOwnIdsTest(unittest.TestCase):
    """The module's own Signals and publish_running, called in this process."""

    def setUp(self) -> None:
        self.saved = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(number, handler) for number, handler in self.saved.items()])
        work = tempfile.TemporaryDirectory(prefix="i4r-2710-signals-")
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)

    def test_install_keeps_an_ignored_hangup_and_always_takes_sigint_and_sigterm(self) -> None:
        # candidate: install() replaced nohup's SIG_IGN with its handler, so a closed terminal stopped the run; then it
        # kept every signal the run was started ignoring ignored, so a run started with & ignored Ctrl-C and SIGINT,
        # and its children inherited SIGINT ignored, which the SIGINT that ends their group could not reach
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):   # `nohup tool &` from a non-interactive shell
            signal.signal(number, signal.SIG_IGN)
        signals = tool.Signals()
        signals.install()
        try:
            self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN, "nohup's SIGHUP is no longer ignored")
            self.assertEqual(signal.getsignal(signal.SIGINT), signals.handle, "SIGINT stays ignored")
            self.assertEqual(signal.getsignal(signal.SIGTERM), signals.handle, "SIGTERM stays ignored")
        finally:
            signals.restore()
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            self.assertIs(signal.getsignal(number), signal.SIG_IGN, signal.Signals(number).name)

        # started at a terminal: each is the run's, and restore() puts each back
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        signals = tool.Signals()
        signals.install()
        try:
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                self.assertEqual(signal.getsignal(number), signals.handle, signal.Signals(number).name)
        finally:
            signals.restore()
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_DFL)

    def test_a_held_signal_says_so_and_stops_the_run_once_the_step_ends(self) -> None:
        # candidate: a signal held while the note was journaled or the audit re-filed was recorded without a word
        log = self.work / "stderr"
        signals = tool.Signals()
        reached = False
        with stderr_into(log), self.assertRaises(tool.Interrupted) as caught:
            with signals.held():
                signals.handle(signal.SIGTERM, None)
                reached = True   # held: the step under way goes on
        self.assertTrue(reached, "the held signal cut the step short")
        self.assertEqual(caught.exception.signum, signal.SIGTERM)
        self.assertEqual(log.read_text().splitlines(), [HELD_UNTIL])

    def test_a_signal_once_a_rollback_has_begun_is_never_raised(self) -> None:
        # candidate: only a signal the run had raised itself made it ignore later ones, so one arriving in a rollback
        # that a failure had started cut it in two, and one held through a put-back was raised as the hold ended
        with self.assertRaises(tool.Interrupted):
            tool.Signals().handle(signal.SIGTERM, None)   # a first signal stops the run, as before
        held = tool.Signals()
        with stderr_into(self.work / "held"), self.assertRaises(tool.Interrupted):
            with held.held():   # one held while the note is journaled stops it once the note is written
                held.handle(signal.SIGTERM, None)
        log = self.work / "stderr"
        signals = tool.Signals()
        signals.stopping()   # a rollback or a put-back begins
        with stderr_into(log):
            signals.handle(signal.SIGTERM, None)
            with signals.held():
                signals.handle(signal.SIGINT, None)
        self.assertEqual(signals.received, [signal.SIGTERM, signal.SIGINT])
        self.assertEqual(log.read_text().splitlines(), [ALREADY_STOPPING, ALREADY_STOPPING.replace("SIGTERM", "SIGINT")])

    def names_the_audit(self, audit: Path, **popen) -> subprocess.Popen:
        process = subprocess.Popen([sys.executable, "-c", NAMES_THE_AUDIT, str(audit)], stdout=subprocess.PIPE,
                                   text=True, **popen)
        self.addCleanup(lambda: (process.kill(), process.wait(), process.stdout.close()))
        self.assertEqual(process.stdout.readline().strip(), "ready")   # its command line is set
        return process

    def test_publish_running_never_takes_this_runs_own_process_group_for_a_publication(self) -> None:
        # candidate: after a reboot the resumed --apply's own process group could hold the publish_group an earlier
        # run recorded, and its command names the audit: it refused for good, advising kill -TERM -- -<its own group>
        audit = self.work / "runs" / JOURNAL / PAPER / "audit"
        audit.mkdir(parents=True)
        own = os.getpgrp()
        member = self.names_the_audit(audit)   # in this process's group, as the resumed tool and its shell are
        self.assertEqual(os.getpgid(member.pid), own)
        self.assertTrue(tool.group_alive(own))
        self.assertFalse(tool.publish_running(own, audit), "this run's own process group taken for a publication")
        self.assertLessEqual({os.getpid(), own, os.getppid()}, tool.own_ids())
        other = self.names_the_audit(audit, start_new_session=True)   # a live group of another run's
        self.assertTrue(tool.publish_running(other.pid, audit), "a live publication of the audit no longer counts")


class HoldAndChildrenTest(unittest.TestCase):
    """The module's hold() and run_child(), called in this process."""

    def setUp(self) -> None:
        work = tempfile.TemporaryDirectory(prefix="i4r-2710-hold-")
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)

    def test_a_lock_the_file_system_refuses_is_not_taken_for_another_run(self) -> None:
        # candidate: hold() took any error of flock for another run's lock ("another amend_claims_script.py run holds
        # this audit"), naming a run that does not exist, on a file system without flock (an NFS mount, say)
        audit = self.work / "runs" / JOURNAL / PAPER / "audit"
        audit.mkdir(parents=True)
        for name in (tool.fr.RECEIPT, "run.json", "claims_script.R"):   # as main() wants a finalized audit
            (audit / name).write_text("{}\n")

        def unsupported(descriptor: int, operation: int) -> None:
            raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))

        free = os.open(os.devnull, os.O_RDONLY)   # the lowest free descriptor: the one hold() opens
        os.close(free)
        handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(tool, "SIGNALS", tool.Signals()), mock.patch.object(tool.fcntl, "flock", unsupported), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as caught:
            tool.main([str(audit), "--apply"])
        self.assertEqual(caught.exception.code, f"{PAPER}: {LOCKED_HERE}")
        self.assertNotIn("holds this audit", out.getvalue() + err.getvalue() + str(caught.exception.code))
        again = os.open(os.devnull, os.O_RDONLY)
        os.close(again)
        self.assertEqual(again, free, "hold() left the audit folder's descriptor open")
        self.assertEqual({number: signal.getsignal(number) for number in handlers}, handlers,
                         "the run's signal handlers were installed before the audit was held")

        # another run's lock is still "another run" (BlockingIOError), and a folder that cannot be opened says so
        first = tool.hold(audit)
        self.assertIsInstance(first, int)
        try:
            self.assertIsNone(tool.hold(audit), "a second lock on the audit folder was taken")
        finally:
            os.close(first)
        audit.chmod(0o311)   # no read permission: the folder cannot be opened to lock it
        self.addCleanup(audit.chmod, 0o755)
        with self.assertRaises(SystemExit) as caught:
            tool.hold(audit)
        self.assertEqual(caught.exception.code, f"{PAPER}: refused: the audit folder cannot be opened to lock it "
                                                f"({os.strerror(errno.EACCES)}); nothing was done")

    def test_a_signal_while_a_timed_out_childs_group_is_ended_does_not_cut_that_short(self) -> None:
        # candidate: after a child's timeout, end_group ran with the run not yet stopping, so a signal in its grace
        # period raised Interrupted inside it: its SIGTERM and SIGKILL steps were skipped and the group lived on
        signals = tool.Signals()
        children: list[subprocess.Popen] = []

        def started(process: subprocess.Popen) -> None:
            children.append(process)
            self.addCleanup(kill_group, process.pid)
            process.stdout.readline()   # its SIGINT handler is in place

        log = self.work / "stderr"
        with mock.patch.object(tool, "SIGNALS", signals):
            signals.install()
            try:
                with stderr_into(log):
                    try:
                        tool.run_child([sys.executable, "-c", SLOW_OVER_SIGINT, str(os.getpid())], timeout=2,
                                       on_start=started)
                        outcome = "the child ended by itself"
                    except subprocess.TimeoutExpired:
                        outcome = "timed out"
                    except tool.Interrupted as exc:
                        outcome = f"{exc} cut end_group short"
            finally:
                signals.restore()
        self.assertEqual(outcome, "timed out")
        self.assertEqual(len(children), 1)
        self.assertTrue(gone(children[0].pid, group=True), "the timed-out child's process group is still alive")
        self.assertEqual(signals.received, [signal.SIGTERM], "the SIGTERM sent during end_group did not arrive")
        self.assertIn(ALREADY_STOPPING, log.read_text().splitlines())


class HardeningTest(amend_tests.AmendmentFixture):
    """The real tool on a published audit, stopped, paused or changed at the point each fix names."""

    def setUp(self) -> None:
        super().setUp()
        self.journal = self.audit / "run.events.jsonl"
        self.mark = self.work / "pause.pid"

    def state(self) -> tuple[dict[str, str], list[str]]:
        return tree_digest(self.base), folders(self.base)

    def notes(self) -> list[dict]:
        return [event for event in self.events() if event["type"] == "note"
                and event["payload"].get("kind") == "claims_script_amendment"]

    def versions(self) -> list[str]:
        return sorted(p.name for p in (self.registry / AUDIT_ID / "versions").iterdir() if not p.name.startswith("."))

    def records(self) -> list[Path]:
        return sorted(self.audit.glob("amendments/*-claims_script"))

    def paused_env(self, **extra: str) -> dict[str, str]:
        return self.env(I4R_TEST_PAUSE_MARK=str(self.mark), **extra)

    def wrapped(self, code: str, *args: str, scripts: Path = SCRIPTS,
                env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", code, str(scripts), str(self.audit), *args], capture_output=True,
                              text=True, env=env or self.env(), timeout=900, preexec_fn=signals_at_default)

    def start_wrapped(self, code: str, *args: str, scripts: Path = SCRIPTS, env: dict[str, str] | None = None,
                      prefix: tuple[str, ...] = (), preexec_fn=signals_at_default, **popen) -> subprocess.Popen:
        process = subprocess.Popen([*prefix, sys.executable, "-c", code, str(scripts), str(self.audit), *args],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=env or self.paused_env(), preexec_fn=preexec_fn, **popen)
        self.addCleanup(self.end, process)
        return process

    @staticmethod
    def end(process: subprocess.Popen) -> None:
        """A run a failed test left: killed, and its pipes closed."""
        if process.poll() is None:
            process.kill()
        with contextlib.suppress(Exception):
            process.communicate(timeout=60)

    def wait_paused(self, process: subprocess.Popen) -> None:
        wait_for(lambda: self.mark.is_file() and self.mark.read_text().strip().isdigit(), 900,
                 "the run to reach its pause", process)

    def go_on(self) -> None:
        self.mark.with_name(self.mark.name + ".go").write_text("go\n")

    def registry_checks(self, scripts: Path = SCRIPTS) -> list[subprocess.CompletedProcess]:
        receipt = str(scripts / "finalization_receipt.py")
        return [subprocess.run(command, capture_output=True, text=True, env=ENV, timeout=300) for command in (
            [sys.executable, receipt, "registry-audit", str(self.registry)],
            [sys.executable, receipt, "check", "--require-registry", str(self.audit)])]

    def pending_record(self) -> Path:
        records = self.records()
        self.assertEqual(len(records), 1, records)
        self.assertEqual(self.notes(), [], "the run was stopped before its note")
        self.assertEqual(json.loads((records[0] / "AMEND_PLAN.json").read_text())["state"], "backed_up")
        return records[0]

    def assert_finished(self, returncode: int, out: str, err: str, comments_before: dict, metadata_before: dict,
                        *, release: Path | None = None) -> None:
        """The amendment is finished, once: republished, the receipt checks for publication, one note, and a further
        --apply has nothing to do (with --rebuild-documents and that release's tool, given a release)."""
        self.assertEqual(returncode, 0, out + err)
        self.assertIn("amended and republished", out)
        self.assert_republished(comments_before, metadata_before, comment_unchanged=release is None,
                                scripts=(release or AMEND).parent)
        self.assertEqual(len(self.notes()), 1, "one amendment, one note")
        again = (self.tool("--rebuild-documents", "--apply", script=release) if release is not None
                 else self.tool("--apply"))
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("already amended and republished: nothing to do", again.stdout)

    def test_a_journal_that_cannot_be_read_refuses_every_run_rollback_pending_included(self) -> None:
        # candidate: read as empty, a journaled amendment looked stopped before its note; every run sent the operator
        # to --rollback-pending, which put the claims script and the receipt back under the note and removed the plan
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        lock = self.metadata.parent / f"{PAPER}.publish.lock"
        lock.write_text(f"{os.getpid()}\n")   # publish_completed.sh refuses: journaled and re-filed, not republished
        refused = self.tool("--apply")
        lock.unlink()
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("publish_completed.sh refused the amended audit", refused.stdout)
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(list(self.audit.glob("amendments/*/AMENDMENT.json")), [], "the amendment is unfinished")
        readable, mode = self.journal.read_bytes(), self.journal.stat().st_mode & 0o777
        line = len(self.events()) + 1
        torn = ('{"schema_version": "2.1.0", "seq": %d, "at": "2026-10-30T00:0' % line).encode()
        before = self.state()
        for how, detail in (("a torn last line", f"(run.events.jsonl line {line}: invalid JSON"),
                            ("no read permission", "([Errno 13] Permission denied: 'run.events.jsonl')")):
            if how == "a torn last line":   # a power cut in the middle of an append
                with self.journal.open("ab") as handle:
                    handle.write(torn)
            else:
                self.journal.chmod(0)
            try:
                for args in ((), ("--apply",), ("--rollback-pending",), ("--rebuild-documents", "--apply")):
                    done = self.tool(*args)
                    shown = f"{how}, {' '.join(args) or 'dry run'}:\n{done.stdout}{done.stderr}"
                    self.assertEqual(done.returncode, 1, shown)
                    refusal = [line for line in done.stderr.splitlines() if UNREADABLE in line]
                    self.assertEqual(len(refusal), 1, shown)
                    self.assertIn(detail, refusal[0], shown)
                    self.assertIn(MAKE_IT_READABLE, refusal[0])
                    for local in (str(self.work), str(self.work.resolve())):
                        self.assertNotIn(local, refusal[0], "the refusal names a local path")
                    self.assertNotIn("rolled back", done.stdout + done.stderr)
                if how == "no read permission":
                    self.assertEqual(self.journal.stat().st_mode & 0o777, 0, "a run changed the journal's mode")
            finally:   # the journal repaired
                self.journal.chmod(mode)
                self.journal.write_bytes(readable)
            self.assertEqual(self.state(), before, f"a run changed the tree while the journal had {how}")
            self.assertEqual(self.leftovers(), [])

        done = self.tool("--apply")   # readable again: --apply finishes the amendment
        self.assertIn("an amendment of the claims script is journaled", done.stdout)
        self.assert_finished(done.returncode, done.stdout, done.stderr, comments_before, metadata_before)

    def test_a_second_run_refuses_while_another_holds_the_audit(self) -> None:
        # candidate: nothing stopped a second run on the audit; one that overlapped the first journaled a second note,
        # filed the audit again and left every later run refusing
        holder = subprocess.Popen([sys.executable, "-c", HOLDS_THE_AUDIT, str(self.audit)], stdout=subprocess.PIPE,
                                  text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), "held")
        before = self.state()
        for args in ((), ("--apply",), ("--rollback-pending",), ("--rebuild-documents",)):
            done = self.tool(*args)
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn(HELD, done.stdout)
        self.assertEqual(self.state(), before, "a refused run changed the tree")
        self.assertEqual(self.leftovers(), [])
        holder.kill()
        holder.wait()   # an fcntl lock ends with the process that holds it

        # the lock released, a run works, and holds the audit in turn to its end (paused once its note is journaled)
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        versions = self.versions()
        process = self.start_wrapped(PAUSED_BEFORE_THE_REPUBLICATION, "--apply")
        self.wait_paused(process)
        during = self.state()
        for args in ((), ("--apply",)):
            done = self.tool(*args)
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn(HELD, done.stdout)
        self.assertEqual(self.state(), during, "a second run changed the tree while the first ran")
        self.go_on()
        out, err = process.communicate(timeout=900)
        self.assert_finished(process.returncode, out, err, comments_before, metadata_before)
        self.assertEqual(len(self.versions()), len(versions) + 1, "the audit was filed more than once")

    def test_a_run_started_with_nohup_finishes_through_a_hangup(self) -> None:
        # candidate: install() replaced nohup's SIG_IGN, so the hangup of a closed terminal stopped the run (129)
        nohup = shutil.which("nohup")
        if nohup is None:
            self.skipTest("nohup is not on PATH")
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        # preexec_fn sets SIGHUP to its default first; nohup then ignores it for the run, whatever the runner's
        process = self.start_wrapped(PAUSED_BEFORE_THE_REPUBLICATION, "--apply", prefix=(nohup,),
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        self.wait_paused(process)   # mid-run: the note journaled and the audit re-filed
        os.killpg(process.pid, signal.SIGHUP)   # the terminal closes: SIGHUP to the job's process group
        time.sleep(1)
        self.assertIsNone(process.poll(), "the hangup stopped the run")
        self.go_on()
        out, err = process.communicate(timeout=900)
        self.assertNotIn("SIGHUP", err)
        self.assert_finished(process.returncode, out, err, comments_before, metadata_before)

    def test_a_run_started_with_sigint_ignored_stops_on_it_and_its_children_clean_up(self) -> None:
        # candidate: a run started with SIGINT ignored (`tool &` from a non-interactive shell, nohup ... & included)
        # kept it ignored, so Ctrl-C and kill -INT did nothing; its children started with it ignored too, so the
        # SIGINT that ends their group reached none of their clean-up, and they ended only by SIGTERM 10 s later
        mark = self.work / "rscript.mark"
        fake = self.work / "fakebin"
        fake.mkdir()
        fake_rscript(fake / "Rscript", mark)
        before = self.state()

        def as_a_background_job() -> None:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGHUP, signal.SIG_DFL)

        process = self.start_wrapped(SAYS_HOW_IT_STARTED, env=self.env(PATH=f"{fake}{os.pathsep}{ENV['PATH']}"),
                                     preexec_fn=as_a_background_job)
        wait_for(mark.is_file, 900, "the rehearsal's claims run", process)
        pid, group, started = mark.read_text().split()
        self.addCleanup(kill_group, int(group))
        self.assertEqual(started, "default", "the run's child started with SIGINT ignored")
        self.assertNotEqual(int(group), process.pid, "the child is in a process group of its own")
        process.send_signal(signal.SIGINT)
        out, err = process.communicate(timeout=300)
        self.assertIn("started with SIGINT ignored", err)
        self.assertEqual(process.returncode, 130, out + err)
        self.assertIn("stopped by SIGINT", err)
        self.assertTrue(mark.with_name(mark.name + ".cleaned").is_file(),
                        "the child's clean-up did not run on the SIGINT that ends its group")
        wait_for(lambda: gone(int(pid)) and gone(int(group), group=True), 60, "the child's process group to end")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.leftovers(), [], "a scratch folder outlived the run")

    def test_projections_a_cut_off_journal_write_left_behind_are_rebuilt_before_the_resume(self) -> None:
        # candidate: every --apply refused for good ("the receipt could not be created again: run.json journal
        # watermark differs from the canonical journal tip"), while the dry run said --apply finishes it
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        projected = {name: (self.audit / name).read_bytes() for name in sorted(JOURNAL_PROJECTIONS)
                     if name != self.journal.name and (self.audit / name).is_file()}
        killed = self.wrapped(KILLED_AFTER_THE_NOTE, "--apply")
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        self.assertEqual(len(self.notes()), 1)
        tip = lifecycle_journal.watermark(lifecycle_journal.load_events(self.audit))
        self.assertEqual(json.loads((self.audit / "run.json").read_text())["journal_watermark"], tip)
        for name, data in projected.items():   # as a run cut off between the note's line and its projections leaves it
            (self.audit / name).write_bytes(data)
        self.assert_projections_rebuilt(comments_before, metadata_before)

    def test_projections_behind_a_journaled_delivery_are_rebuilt_before_the_record(self) -> None:
        # candidate: the amended set was published and its delivery journaled, but every run refused ("the receipt
        # binds the amendment's files but does not check: run.json journal watermark differs from the canonical
        # journal tip") and AMENDMENT.json was never written
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        saved = self.work / "projections-before-the-publication"
        killed = self.wrapped(KILLED_BEFORE_THE_RECORD, "--apply", env=self.env(I4R_TEST_PROJECTIONS=str(saved)))
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        self.assertEqual(self.events()[-1]["type"], "delivery", "publish_completed.sh filed the paper")
        self.assertEqual(list(self.audit.glob("amendments/*/AMENDMENT.json")), [])
        for path in saved.iterdir():   # as a run cut off between the delivery's line and its projections leaves it
            (self.audit / path.name).write_bytes(path.read_bytes())
        self.assert_projections_rebuilt(comments_before, metadata_before)
        self.assertEqual(self.record()["delivery_event"], len(self.events()), "AMENDMENT.json names the delivery")

    def assert_projections_rebuilt(self, comments_before: dict, metadata_before: dict) -> None:
        """The journal's projections name an earlier event than its last: the dry run says so, and that --apply
        rebuilds them, and changes nothing; --apply rebuilds them and finishes the amendment."""
        tip = lifecycle_journal.watermark(lifecycle_journal.load_events(self.audit))
        self.assertNotEqual(json.loads((self.audit / "run.json").read_text())["journal_watermark"], tip)
        before = self.state()
        dry = self.tool()
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("an amendment of the claims script is journaled", dry.stdout)
        self.assertIn("the journal's projections (run.json and the rest) are behind its last event: a journal write "
                      "was cut off", dry.stdout)
        self.assertIn("dry run: --apply rebuilds them (lifecycle_journal.py recover), then finishes it (receipt, "
                      "registry, publication)", dry.stdout)
        self.assertEqual(self.state(), before, "the dry run changed something")

        done = self.tool("--apply")
        self.assertIn("rebuilt them from the journal (lifecycle_journal.py recover)", done.stdout)
        self.assert_finished(done.returncode, done.stdout, done.stderr, comments_before, metadata_before)
        tip = lifecycle_journal.watermark(lifecycle_journal.load_events(self.audit))
        self.assertEqual(json.loads((self.audit / "invocations.watermark.json").read_text()), tip)
        for name in ("run.json", "run_metrics.json"):
            self.assertEqual(json.loads((self.audit / name).read_text())["journal_watermark"], tip, name)

    def test_rollback_pending_refuses_a_record_whose_plan_names_a_publication(self) -> None:
        # candidate: --rollback-pending rolled back any record no note named, one whose amendment was journaled too;
        # then it refused saying "--apply finishes it", while --apply refused that record sending the operator back to
        # --rollback-pending
        killed = self.wrapped(KILLED_BEFORE_THE_NOTE, "--apply")
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        record = self.pending_record()
        plan_path = record / "AMEND_PLAN.json"
        plan = json.loads(plan_path.read_text())
        self.assertNotIn("publication", plan)
        # the plan as a journaled amendment saves it before it moves the published folders (its note not in the journal)
        day = plan["amended_at"][:10]
        plan["publication"] = {"archive": {kind: f"old_runs/archived_{day}_claims_amendment/{kind}/{JOURNAL}/{PAPER}"
                                           for kind in ("comments", "metadata")}}
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
        before = self.state()
        for args in (("--rollback-pending",), (), ("--apply",)):
            done = self.tool(*args)
            shown = f"{' '.join(args) or 'dry run'}:\n{done.stdout}{done.stderr}"
            self.assertEqual(done.returncode, 1, shown)
            self.assertIn(lost_note(record.relative_to(self.audit).as_posix()), done.stdout, shown)
            self.assertNotIn("Put the audit back first with --rollback-pending", done.stdout, shown)
            self.assertNotIn("rolled back:", done.stdout, shown)
        self.assertEqual(self.state(), before, "a run changed the tree")

    def test_a_note_the_journal_lost_is_refused_by_every_run_until_the_journal_is_put_back(self) -> None:
        # candidate: a record whose plan was saved after the note, but before the plan named a publication, was taken
        # for one stopped before its note once the journal lost the note: --rollback-pending put the claims script and
        # the receipt back under an amendment that was journaled
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        versions = self.versions()
        older = self.journal.read_bytes()   # the journal as an older copy holds it: without the note
        killed = self.wrapped(KILLED_AFTER_THE_NOTE, "--apply")
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        self.assertEqual(len(self.notes()), 1)
        records = self.records()
        self.assertEqual(len(records), 1, records)
        plan = json.loads((records[0] / "AMEND_PLAN.json").read_text())
        self.assertIs(plan.get("journaled"), True, "the plan is marked right after the note")
        self.assertNotIn("publication", plan)
        record = records[0].relative_to(self.audit).as_posix()
        with_the_note = self.journal.read_bytes()
        self.journal.write_bytes(older)   # an older copy restored over it (a sync, a restore): the note is lost
        before, left = self.state(), self.leftovers()   # the killed run's scratch folder stays (SIGKILL)
        for args in ((), ("--apply",), ("--rollback-pending",), ("--rebuild-documents", "--apply")):
            done = self.tool(*args)
            shown = f"{' '.join(args) or 'dry run'}:\n{done.stdout}{done.stderr}"
            self.assertEqual(done.returncode, 1, shown)
            self.assertIn(lost_note(record), done.stdout, shown)
            self.assertNotIn("Put the audit back first with --rollback-pending", done.stdout, shown)
            self.assertNotIn("rolled back", done.stdout + done.stderr, shown)
        self.assertEqual(self.state(), before, "a run changed the tree")
        self.assertEqual(self.leftovers(), left, "a refused run left a scratch folder")

        self.journal.write_bytes(with_the_note)   # the journal that holds the note put back: --apply finishes it
        done = self.tool("--apply")
        self.assertIn("an amendment of the claims script is journaled", done.stdout)
        self.assert_finished(done.returncode, done.stdout, done.stderr, comments_before, metadata_before)
        self.assertEqual(len(self.versions()), len(versions) + 1, "the audit was filed more than once")

    def test_rollback_pending_never_takes_its_own_pid_or_group_for_the_stopped_runs(self) -> None:
        # candidate: after a reboot, a --rollback-pending holding the pid the stopped run recorded refused for good
        # ("is still running (pid N); stop it first"), and so did one holding its build's process group
        before = self.state()
        killed = self.wrapped(KILLED_BEFORE_THE_NOTE, "--apply")
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        self.pending_record()
        done = self.wrapped(UNDER_THE_STOPPED_RUNS_IDS, "--rollback-pending")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("rolled back: the run that wrote amendments/", done.stdout)
        self.assertEqual(self.state(), before, "the audit is not as it was before the stopped run")

    def test_a_failure_before_the_note_rolls_back_at_once_although_the_journal_cannot_be_read(self) -> None:
        # candidate: the rollback read the journal first, to tell whether the note had landed; unreadable, it refused
        # ("nothing more was done") and left the claims script replaced, though no note could have landed yet
        before = self.state()
        mode = self.journal.stat().st_mode & 0o777
        self.addCleanup(self.journal.chmod, mode)
        done = self.wrapped(FAILS_BEFORE_THE_NOTE, "--apply", env=self.env(I4R_TEST_UNREADABLE="mode"))
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0, "the journal was not made unreadable")
        self.journal.chmod(mode)
        output = done.stdout + done.stderr
        self.assertEqual(done.returncode, 1, output)
        self.assertIn("the claims script could not be re-bound: a failure injected before the note", done.stderr)
        self.assertIn("rolled back: the audit is as it was before this run", done.stderr)
        self.assertNotIn("NOT", done.stderr)
        self.assertNotIn(UNREADABLE, output)
        self.assertNotIn("stopped part way", output)
        self.assertEqual(self.state(), before, "the rollback left a change behind")
        self.assertEqual(self.leftovers(), [])

    def test_with_rebuild_documents_a_failure_before_the_note_rolls_back_at_once_although_the_journal_cannot_be_read(
            self) -> None:
        # candidate: the same in --rebuild-documents: the rebuilt documents and the new claims script stayed in the
        # audit under the old receipt. This mode's rollback compares the whole audit with the state it started from,
        # so the journal, which the run never wrote but a crash tore under it, is the one difference it reports; its
        # record folder stays, and --rollback-pending removes it once the journal is readable again.
        release = later_release(self.work / "release")
        before = self.state()
        readable = self.journal.read_bytes()
        done = self.wrapped(FAILS_AFTER_THE_BUILD, "--rebuild-documents", "--apply", scripts=release.parent,
                            env=self.env(I4R_TEST_UNREADABLE="torn"))
        torn = self.journal.read_bytes()
        self.journal.write_bytes(readable)
        output = done.stdout + done.stderr
        self.assertTrue(len(torn) > len(readable) and torn.startswith(readable), "the journal was not torn")
        self.assertEqual(done.returncode, 1, output)
        self.assertIn("comment text, PDF: same text", done.stdout)
        self.assertIn("a failure injected after the build in the audit, before the note", done.stderr)
        records = self.records()
        self.assertEqual(len(records), 1, records)
        record = records[0].relative_to(self.audit).as_posix()
        self.assertIn(f"NOT rolled back in full; {record}/ keeps the backups, and --rollback-pending tries again:\n"
                      "  NOT RESTORED: run.events.jsonl still differs\n", done.stderr)
        self.assertEqual(done.stderr.count("NOT RESTORED"), 1, done.stderr)
        self.assertNotIn(UNREADABLE, output)
        self.assertNotIn("stopped part way", output)
        amendments = (self.audit / "amendments").relative_to(self.base).as_posix()

        def outside_the_record(state: tuple[dict[str, str], list[str]]) -> tuple[dict[str, str], list[str]]:
            return ({rel: sha for rel, sha in state[0].items() if not rel.startswith(amendments + "/")},
                    [rel for rel in state[1] if rel != amendments and not rel.startswith(amendments + "/")])

        self.assertEqual(outside_the_record(self.state()), before, "the rollback left a change of the run's behind")
        self.assertEqual(self.leftovers(), [])
        rolled = self.tool("--rollback-pending", script=release)
        self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
        self.assertIn(f"rolled back: the run that wrote {record}/ stopped before its note", rolled.stdout)
        self.assertEqual(self.state(), before)

    def failed_at_the_note(self, code: str) -> tuple[subprocess.CompletedProcess, str]:
        """--apply whose note's log_event fails with the journal torn: it stops part way, says so, and changes nothing
        from the moment the journal was torn. The torn line is removed again; (the run, its record folder)."""
        snapshot = self.work / "snapshot.json"
        done = self.wrapped(code, "--apply", env=self.env(I4R_TEST_UNREADABLE="torn", I4R_TEST_SNAPSHOT=str(snapshot),
                                                          I4R_TEST_BASE=str(self.base)))
        output = done.stdout + done.stderr
        self.assertEqual(done.returncode, 1, output)
        self.assertEqual(tool.tree_hashes(self.base), json.loads(snapshot.read_text()),
                         "the run changed something once its note had failed")
        records = self.records()
        self.assertEqual(len(records), 1, records)
        record = records[0].relative_to(self.audit).as_posix()
        part_way = [line for line in done.stderr.splitlines() if line.startswith(PART_WAY)]
        self.assertEqual(len(part_way), 1, output)
        self.assertIn("invalid JSON", part_way[0])
        self.assertIn(stopped_part_way(record), part_way[0])
        self.assertNotIn(UNREADABLE, done.stderr)   # said once, as the reason of the stop, not as a refusal
        self.assertNotIn("rolled back", output)
        self.assertNotIn(STOPPED_AFTER_THE_NOTE, output)
        self.assertEqual(json.loads((records[0] / "AMEND_PLAN.json").read_text())["state"], "backed_up")
        self.assertEqual(self.leftovers(), [])
        torn = self.journal.read_bytes()
        self.journal.write_bytes(torn[:torn.rindex(b"\n") + 1])   # the torn line removed
        return done, record

    def test_a_note_an_unreadable_journal_refused_stops_part_way_and_rollback_pending_puts_the_audit_back(
            self) -> None:
        # candidate: an unreadable journal skipped every rollback, and the run said only "nothing more was done"
        before = self.state()
        done, record = self.failed_at_the_note(NOTE_REFUSED)
        self.assertIn("could not journal the note event", done.stderr)
        self.assertEqual(self.notes(), [], "the note landed")
        self.assertNotEqual(digest(self.audit / "claims_script.R"), before[0][
            (self.audit / "claims_script.R").relative_to(self.base).as_posix()], "nothing was put back")
        repaired = self.state()
        refused = self.tool("--apply")   # its note did not land: --apply refuses, naming --rollback-pending
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn(STOPPED_BEFORE_THE_NOTE, refused.stdout)
        self.assertIn(f"{record}/ holds what it backed up. Put the audit back first with --rollback-pending",
                      refused.stdout)
        self.assertEqual(self.state(), repaired)
        rolled = self.tool("--rollback-pending")
        self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
        self.assertIn(f"rolled back: the run that wrote {record}/ stopped before its note", rolled.stdout)
        self.assertEqual(self.state(), before, "the audit is not as it was before the stopped run")

    def test_a_note_that_landed_before_the_journal_became_unreadable_stops_part_way_and_apply_finishes(self) -> None:
        # candidate: the same stop said only "nothing more was done", although the claims script stayed replaced
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        versions, receipt = self.versions(), (self.audit / tool.fr.RECEIPT).read_bytes()
        done, record = self.failed_at_the_note(NOTE_LANDED_THEN_FAILED)
        self.assertIn("could not journal the note event: a failure injected after the note landed", done.stderr)
        notes = self.notes()
        self.assertEqual([note["payload"]["record"] for note in notes], [record], "the note landed")
        self.assertEqual((self.audit / tool.fr.RECEIPT).read_bytes(), receipt, "the receipt was created again")
        self.assertEqual(self.versions(), versions, "the audit was filed")
        held = self.tool("--rollback-pending")   # its note landed: no rollback, --apply finishes it
        self.assertEqual(held.returncode, 1, held.stdout + held.stderr)
        self.assertIn(f"refused: the amendment in {record} is journaled, so it cannot be rolled back; --apply "
                      "finishes it", held.stdout)
        done = self.tool("--apply")
        self.assertIn("an amendment of the claims script is journaled", done.stdout)
        self.assert_finished(done.returncode, done.stdout, done.stderr, comments_before, metadata_before)
        self.assertEqual(len(self.versions()), len(versions) + 1)

    def test_a_claims_script_a_teammate_amended_meanwhile_is_refused_before_apply(self) -> None:
        # candidate: --apply applied what it had rehearsed to the script it no longer had: a second note (from and to
        # the same script) and a second filing, then "refused: the published folders are not as an interrupted
        # --apply leaves them" (neither the publication from before the amendment nor the amended one)
        theirs = self.work / "teammate" / "base"
        shutil.copytree(self.base, theirs, symlinks=True)
        amended = subprocess.run([sys.executable, str(AMEND), str(theirs / self.audit.relative_to(self.base)),
                                  "--apply"], capture_output=True, text=True, env=self.env(), timeout=900,
                                 preexec_fn=signals_at_default)
        self.assertEqual(amended.returncode, 0, amended.stdout + amended.stderr)
        synced = tree_digest(theirs), folders(theirs)
        done = self.wrapped(SYNCED_AFTER_THE_REHEARSAL, "--apply", env=self.env(
            I4R_TEST_TREE=str(self.base), I4R_TEST_TREE_ASIDE=str(self.work / "before-the-sync"),
            I4R_TEST_SYNCED_TREE=str(theirs)))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("rehearsal: claims script replaced", done.stdout)
        self.assertIn(CHANGED, done.stdout)
        self.assertEqual(self.state(), synced, "the run changed the tree after the claims script had changed")
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.leftovers(), [])
        check = self.receipt_check()
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        again = self.tool("--apply")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("already amended and republished: nothing to do", again.stdout)

    def test_with_rebuild_documents_a_claims_script_changed_meanwhile_is_refused_before_apply(self) -> None:
        # candidate: the documents mode did not read the claims script again either; here only the published set's
        # check stopped it, for another reason ("the published claims script ... is not the audit's")
        release = later_release(self.work / "release")
        claims, projection = self.audit / "claims_script.R", self.audit / tool.PROJECTION
        original = claims.read_bytes()
        before = self.state()
        done = self.wrapped(CHANGED_AFTER_THE_REHEARSAL, "--rebuild-documents", "--apply", scripts=release.parent)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("comment text, PDF: same text", done.stdout)
        self.assertIn(CHANGED, done.stdout)
        self.assertEqual(claims.read_bytes(), original + b"# re-bound by another run\n")
        rows = [row["sha256"] for row in json.loads(projection.read_text())["artifacts"] if row["path"] == claims.name]
        self.assertEqual(rows, [digest(claims)])
        expected = dict(before[0])
        for path in (claims, projection):   # the other run's change, and nothing of this run's
            expected[path.relative_to(self.base).as_posix()] = digest(path)
        self.assertEqual(self.state(), (expected, before[1]), "the run changed the tree")
        self.assertEqual(self.leftovers(), [])

    def signal_in_the_filing(self, versions: list[str], *args: str, scripts: Path = SCRIPTS) -> tuple[str, str]:
        """--apply paused inside the audit's registry filing, sent SIGTERM there, then let go: it says the signal is
        held, and stops (143) only once the filing is whole, and the registry and the receipt agree."""
        process = self.start_wrapped(PAUSED_IN_THE_FILING, *args, "--apply", scripts=scripts,
                                     env=self.paused_env(I4R_TEST_REGISTRY=str(self.registry)))
        self.wait_paused(process)
        pointer = json.loads((self.registry / AUDIT_ID / "current.json").read_text())
        self.assertNotIn(pointer["version"], versions, "the pause is inside the filing, after current.json")
        process.send_signal(signal.SIGTERM)
        time.sleep(2)
        self.assertIsNone(process.poll(), "SIGTERM cut the registry filing in two")
        self.go_on()
        out, err = process.communicate(timeout=900)
        self.assertEqual(process.returncode, 143, out + err)
        self.assertIn(HELD_UNTIL, err.splitlines(), "the run did not say it held the signal")
        self.assertIn("stopped by SIGTERM", err)
        for check in self.registry_checks(scripts):   # registry-audit, and the receipt check against the registry
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertEqual(len(self.versions()), len(versions) + 1)
        return out, err

    def test_a_signal_while_the_audit_is_re_filed_waits_for_the_filing_to_end(self) -> None:
        # candidate: SIGTERM cut fr.create's filing in two: current.json named a version no index row or receipt
        # held (registry-audit: MISSING), and the resume filed the audit once more
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        versions = self.versions()
        out, err = self.signal_in_the_filing(versions)
        self.assertIn(STOPPED_AFTER_THE_NOTE, err)
        done = self.tool("--apply")
        self.assert_finished(done.returncode, done.stdout, done.stderr, comments_before, metadata_before)
        self.assertEqual(len(self.versions()), len(versions) + 1, "the resume filed the audit again")

    def test_a_signal_while_rebuild_documents_re_files_the_audit_waits_for_the_filing_to_end(self) -> None:
        # candidate: the filing of --rebuild-documents --apply was cut in two the same way
        release = later_release(self.work / "release")
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        versions = self.versions()
        out, err = self.signal_in_the_filing(versions, "--rebuild-documents", scripts=release.parent)
        self.assertIn(STOPPED_AFTER_THE_NOTE, err)
        done = self.tool("--rebuild-documents", "--apply", script=release)
        self.assertIn("a --rebuild-documents amendment is journaled", done.stdout)
        self.assert_finished(done.returncode, done.stdout, done.stderr, comments_before, metadata_before,
                             release=release)
        self.assertEqual(len(self.versions()), len(versions) + 1, "the resume filed the audit again")

    def test_a_signal_while_a_resumed_apply_re_files_the_audit_waits_for_the_filing_to_end(self) -> None:
        # candidate: the filing of a resumed --apply (killed after its note) was cut in two the same way
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        versions = self.versions()
        killed = self.wrapped(KILLED_AFTER_THE_NOTE, "--apply")
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        self.assertEqual(self.versions(), versions, "killed before the filing")
        out, err = self.signal_in_the_filing(versions)
        self.assertIn("an amendment of the claims script is journaled", out)
        self.assertIn("the receipt was not created again after it", out)
        done = self.tool("--apply")
        self.assert_finished(done.returncode, done.stdout, done.stderr, comments_before, metadata_before)
        self.assertEqual(len(self.versions()), len(versions) + 1, "the resume filed the audit again")

    def signal_in_the_registry_wait(self, *args: str, script: Path = AMEND) -> tuple[str, str]:
        """--apply waits, after its note, for the registry lock another process holds; SIGTERM there stops it at
        once (143) while that process still holds the lock, without holding the signal, and nothing is filed: the
        registry and the receipt are as they were. The other process then lets the lock go."""
        lock = self.registry / ".registry.lock"
        holder = subprocess.Popen([sys.executable, "-c", HOLDS_THE_REGISTRY, str(lock)], stdout=subprocess.PIPE,
                                  text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), "held")
        registry, receipt = tree_digest(self.registry), (self.audit / tool.fr.RECEIPT).read_bytes()
        out_path, err_path = self.work / "registry-wait.out", self.work / "registry-wait.err"
        with out_path.open("w") as out_file, err_path.open("w") as err_file:
            process = subprocess.Popen([sys.executable, str(script), str(self.audit), *args, "--apply"],
                                       stdout=out_file, stderr=err_file, text=True, env=self.env(),
                                       preexec_fn=signals_at_default)
        self.addCleanup(self.end, process)

        def waiting() -> bool:
            if process.poll() is not None:
                raise AssertionError(f"the tool exited {process.returncode} before the registry lock wait:\n"
                                     f"{out_path.read_text()}\n{err_path.read_text()}")
            return "waiting: the registry lock" in err_path.read_text()

        wait_for(waiting, 900, "the registry lock wait")
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=120)   # the holder keeps the lock for 900 s
        except subprocess.TimeoutExpired:
            self.fail("SIGTERM did not stop a run waiting for another process's registry lock:\n"
                      + err_path.read_text())
        self.assertIsNone(holder.poll(), "the registry lock was let go before the run stopped")
        out, err = out_path.read_text(), err_path.read_text()
        self.assertEqual(process.returncode, 143, out + err)
        self.assertIn("stopped by SIGTERM", err)
        self.assertNotIn("held until", err)
        self.assertEqual(tree_digest(self.registry), registry, "the audit was filed")
        self.assertEqual((self.audit / tool.fr.RECEIPT).read_bytes(), receipt, "the receipt was created again")
        self.assertEqual(len(self.notes()), 1)
        audit_check = self.registry_checks()[0]
        self.assertEqual(audit_check.returncode, 0, audit_check.stdout + audit_check.stderr)
        holder.kill()   # the other filing ends, and with it its lock
        holder.wait()
        return out, err

    def test_a_signal_while_apply_waits_for_another_filings_registry_lock_stops_it_at_once(self) -> None:
        # candidate: the signal was held through the wait for the registry lock, which has no time limit, without a
        # word: only SIGKILL stopped a run that waited on a teammate's filing or a stuck one
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        versions = self.versions()
        out, err = self.signal_in_the_registry_wait()
        self.assertIn(STOPPED_AFTER_THE_NOTE, err)
        self.assertEqual(self.versions(), versions)
        done = self.tool("--apply")
        self.assertIn("an amendment of the claims script is journaled", done.stdout)
        self.assertIn("the receipt was not created again after it", done.stdout)
        self.assert_finished(done.returncode, done.stdout, done.stderr, comments_before, metadata_before)
        self.assertEqual(len(self.versions()), len(versions) + 1, "the audit was filed more than once")

    def test_a_signal_while_rebuild_documents_waits_for_the_registry_lock_stops_it_at_once(self) -> None:
        # candidate: the same wait of --rebuild-documents --apply could not be stopped either
        release = later_release(self.work / "release")
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        versions = self.versions()
        out, err = self.signal_in_the_registry_wait("--rebuild-documents", script=release)
        self.assertIn(STOPPED_AFTER_THE_NOTE, err)
        self.assertEqual(self.versions(), versions)
        done = self.tool("--rebuild-documents", "--apply", script=release)
        self.assertIn("a --rebuild-documents amendment is journaled", done.stdout)
        self.assert_finished(done.returncode, done.stdout, done.stderr, comments_before, metadata_before,
                             release=release)
        self.assertEqual(len(self.versions()), len(versions) + 1, "the audit was filed more than once")

    def test_a_signal_while_a_resumed_apply_waits_for_the_registry_lock_stops_it_at_once(self) -> None:
        # candidate: the same wait of a resumed --apply (killed after its note) could not be stopped either
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        versions = self.versions()
        killed = self.wrapped(KILLED_AFTER_THE_NOTE, "--apply")
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        out, err = self.signal_in_the_registry_wait()
        self.assertIn("an amendment of the claims script is journaled", out)
        self.assertIn("the receipt was not created again after it", out)
        self.assertEqual(self.versions(), versions)
        done = self.tool("--apply")
        self.assert_finished(done.returncode, done.stdout, done.stderr, comments_before, metadata_before)
        self.assertEqual(len(self.versions()), len(versions) + 1, "the audit was filed more than once")

    def test_a_signal_during_a_rollback_does_not_cut_it_in_two(self) -> None:
        # candidate: a SIGTERM while a failure's rollback copied the backups back stopped it half way: the claims
        # script and its projection row stayed replaced, and the record folder stayed (only --rollback-pending helped)
        before = self.state()
        lock = (self.audit / ".run.events.lock").open("a+b")   # the journal cannot be written: the note fails
        self.addCleanup(lock.close)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            process = self.start_wrapped(PAUSED_IN_THE_ROLLBACK, "--apply", env=self.paused_env(LOCK_TIMEOUT="2"))
            self.wait_paused(process)   # the note failed, and the rollback has begun
            process.send_signal(signal.SIGTERM)
            time.sleep(1)
            self.go_on()
            out, err = process.communicate(timeout=900)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        self.assertEqual(process.returncode, 1, out + err)
        self.assertIn("could not journal the note event", err)
        self.assertIn(ALREADY_STOPPING, err)
        self.assertIn("rolled back: the audit is as it was before this run", err)
        self.assertNotIn("NOT", err)
        self.assertNotIn("stopped by SIGTERM", err)
        self.assertEqual(self.state(), before, "the rollback left a change behind")
        self.assertEqual(self.leftovers(), [])

    def test_a_signal_during_the_put_back_keeps_publish_completeds_refusal(self) -> None:
        # candidate: a SIGTERM held while the previous folders were moved back was raised as the hold ended, so the
        # run said only that it had stopped (143), never that publish_completed.sh had refused, or why
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        lock = self.metadata.parent / f"{PAPER}.publish.lock"
        lock.write_text(f"{os.getpid()}\n")   # a live publication of this paper holds it: publish_completed.sh refuses
        process = self.start_wrapped(PAUSED_IN_THE_PUT_BACK, "--apply")
        self.wait_paused(process)   # the publication refused, and the put-back has begun
        process.send_signal(signal.SIGTERM)
        time.sleep(1)
        self.go_on()
        out, err = process.communicate(timeout=900)
        lock.unlink()
        self.assertEqual(process.returncode, 1, out + err)
        self.assertIn("stopped: publish_completed.sh refused the amended audit; the previous comments/ and metadata/ "
                      "folders are back in place (the audit itself stays amended)", out)
        self.assertIn(ALREADY_STOPPING, err)
        self.assertNotIn("stopped by SIGTERM", err)
        self.assertNotIn(STOPPED_AFTER_THE_NOTE, err)
        self.assertEqual(tree_digest(self.comments), comments_before)
        self.assertEqual(tree_digest(self.metadata), metadata_before)

        done = self.tool("--apply")
        self.assertIn("an amendment of the claims script is journaled", done.stdout)
        self.assert_finished(done.returncode, done.stdout, done.stderr, comments_before, metadata_before)


if __name__ == "__main__":
    unittest.main()
