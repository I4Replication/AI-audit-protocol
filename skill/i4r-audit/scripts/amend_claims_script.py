#!/usr/bin/env python3
"""amend_claims_script.py <audit-dir> [--apply] [--reason TEXT]
       amend_claims_script.py <audit-dir> --rebuild-documents [--apply] [--accept-rendering-changes]
       amend_claims_script.py <audit-dir> --rollback-pending

Rebuilds a PUBLISHED audit's claims script with this release's make_claims_script.sh and republishes the paper in
place. Written for 2.7.10, whose builder gives the claims script the verification notebook's setup; scripts built
before then often stop with an error when a replicator runs them. The findings, the snippets, the claims map,
comment.json and the report are read, never written.

Both modes run the same steps; --rebuild-documents also rebuilds the comment's PDF and DOCX (below).
  1. The new script is built in a scratch folder and run on its own against the deposit (check_claims_script.sh,
     which keeps no log in the audit). It must pass and differ from the audit's.
  2. Before anything changes, comments/<journal>/<paper>/ and metadata/<journal>/<paper>/ must hold exactly their
     release manifest's files and the audit's current claims script, receipt, document manifest, comment.json, PDF
     and DOCX, and every file and folder --apply changes in the audit, the published folders and old_runs/ must be
     writable.
  3. The whole amendment is rehearsed on a full scratch copy of the audit, in a scratch tree with a registry of its
     own: the claims script and its projection row replaced, the note journaled, the receipt created and checked for
     publication, pkg/ made read-only, publish_completed.sh run. The dry run (the default) stops there. It writes
     nothing outside its scratch folders, and those are removed on every exit (a killed run may leave one).
  4. --apply backs up what it changes to <audit>/amendments/<time>-claims_script/ (its plan file says when the
     backups are complete), makes the change and journals a `note` (kind=claims_script_amendment,
     documents=unchanged or rebuilt, the old and new hashes, record=<that folder>). A failure or an interruption
     (Ctrl-C, SIGTERM, SIGHUP) before the note puts every file back. The note is the commit point: the receipt is
     created again and the audit re-filed in the registry it was filed in (run.json and comment.json are the pair;
     the registry keeps every earlier version).
  5. The plan names the folders in old_runs/archived_<date>_claims_amendment/ that comments/ and metadata/ of the
     paper go to before either moves. They move there, and publish_completed.sh republishes the paper with all its
     gates and journals its own `delivery`; if it refuses, both folders move back. pkg/ first gets back the read-only
     mode the synced folder strips (chmod a-w, no byte changes), since the archive gate refuses a writable deposit.
  6. AMENDMENT.json, in the record folder, records it all once that delivery names the republished set, with
     tree-relative paths only.
After the note, --apply resumes an unfinished amendment (receipt, filing, publication, record) and says "already
amended" once it is done; a finished one is history, so a later release can amend the audit again. A run killed while
it republishes (SIGKILL, a power cut) is resumed from the folders, the plan and the journal: a previous set already
moved is taken from the archive folder the plan names; a set publish_completed.sh made visible but never accepted (no
delivery) moves into the record folder and the paper is published again; what the stopped publish_completed.sh left
(its transaction folders and lock, generate_finding_artifacts.py's temporary notebook) is removed; while it still runs,
every run refuses. The dry run says what is published and where the previous set is. A run killed before the note
leaves a record folder that no note names: every later run refuses until --rollback-pending restores the audit from
that folder's backups. One run per audit at a time: a second run refuses while the first holds the audit folder's lock
(on this machine). A journal that cannot be read refuses every run, --rollback-pending included. A run started with
nohup keeps running when its terminal closes.

Without --rebuild-documents only the claims script and its projection row change, and comments/ is republished byte
for byte. An audit whose comment older document scripts built (2.7.4 and earlier) is refused, since its receipt cannot
be created again ("code identity differs from the release verifier"): amend it with --rebuild-documents.

--rebuild-documents [--apply] [--accept-rendering-changes] is for such an audit. Only an audit whose receipt checks
and whose documents fail on that code identity alone is accepted. The rehearsal also rebuilds the documents with
build_docx.sh under the bound PDF's name and compares the comment's text, old against new, in the PDF (pdftotext)
and the DOCX (word/document.xml). --apply goes ahead only when that text is the same, or with
--accept-rendering-changes; it backs up what the build changes and every file already in the build's own folders,
rebuilds in the audit and verifies again before the note.

Exit: 0 done (or dry run passed), 1 refused or failed, 2 usage, 128+N stopped by signal N (130 Ctrl-C, 143 SIGTERM).
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import difflib
import fcntl
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from xml.etree import ElementTree

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import document_release  # noqa: E402
import finalization_receipt as fr  # noqa: E402
import fs_retry  # noqa: E402
import lifecycle_journal  # noqa: E402

BUILDER = HERE / "make_claims_script.sh"
CHECK = HERE / "check_claims_script.sh"
PROJECTION = "work/finding_projections.manifest.json"
CLAIMS_REASON = "claims script rebuilt with the verification notebook's setup so it runs on its own (i4r-audit 2.7.10)"
RECORD = "AMENDMENT.json"
RECORD_RE = re.compile(r"amendments/[^/]+-claims_script")
CLAIMS_PLAN = "AMEND_PLAN.json"     # the plain amendment's plan
DOCS_PLAN = "REBUILD_PLAN.json"     # --rebuild-documents's plan
TEMP_MARK = ".amend-"               # in the name of every temporary file this tool writes beside a file it replaces


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def version() -> str:
    try:
        return (HERE.parent / "VERSION").read_text().strip()
    except OSError:
        return "unknown"


def display_name(audit: pathlib.Path) -> str:
    return audit.name if audit.name != "audit" else audit.parent.name


def shown(path: pathlib.Path, root: pathlib.Path) -> str:
    """A path as the operator should read it: relative to the tree root, never a local absolute path."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return os.path.relpath(path, root)


def layout(audit: pathlib.Path) -> tuple[pathlib.Path, str, str]:
    """The tree root, journal and paper names, as publish_completed.sh derives them (paper_source.json wins)."""
    out = subprocess.run(["bash", "-c", '. "$1"; i4r_root_of_audit "$2"; i4r_journal_of_audit "$2"; '
                          'i4r_paper_name_of_audit "$2"', "_", str(HERE / "tree_layout.sh"), str(audit)],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    root, journal, paper = pathlib.Path(out[0]), out[1], out[2]
    source = audit / "paper_source.json"
    if source.is_file():
        data = json.loads(source.read_text())
        if data.get("journal") and data.get("paper"):
            journal, paper = data["journal"], data["paper"]
    return root, journal, paper


# --- signals and child processes -----------------------------------------------------------------------------------
class Interrupted(KeyboardInterrupt):
    """SIGINT, SIGTERM or SIGHUP, raised where the run is so that its rollback or clean-up runs."""

    def __init__(self, signum: int):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


class Signals:
    """SIGINT, SIGTERM and SIGHUP stop the run alike (only Ctrl-C used to be rolled back: a closed terminal or a
    tool's timeout left the audit half amended). The first one raises Interrupted. One that arrives while a step
    that must not be cut in two runs (the note being journaled, the audit re-filed) is held until that step ends, and
    the run says so. Later ones, and any that arrives once a rollback or a put-back has begun, are ignored, so it runs
    to its end; SIGKILL still ends the run. A SIGHUP the run was started ignoring stays ignored: under nohup it
    stopped the run when its terminal closed. SIGINT and SIGTERM are always the run's own, so its children start with
    them at their defaults and end_group's SIGINT reaches their clean-up."""

    def __init__(self) -> None:
        self.received: list[int] = []
        self.holding = 0
        self.raised = False
        self.previous: dict[int, object] = {}

    def install(self) -> None:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            if number == signal.SIGHUP and signal.getsignal(number) is signal.SIG_IGN:
                continue
            self.previous[number] = signal.signal(number, self.handle)

    def stopping(self) -> None:
        """A rollback or a put-back begins, whatever started it: a signal from now on cannot cut it in two."""
        self.raised = True

    def restore(self) -> None:
        for number, handler in self.previous.items():
            signal.signal(number, handler)
        self.previous.clear()

    def handle(self, signum: int, frame: object) -> None:
        self.received.append(signum)
        if self.raised:   # os.write: a print here could land inside another write to stderr and raise
            with contextlib.suppress(OSError):
                os.write(2, f"{signal.Signals(signum).name}: already stopping; the clean-up runs to its end\n".encode())
            return
        if self.holding:
            with contextlib.suppress(OSError):
                os.write(2, f"{signal.Signals(signum).name}: held until the step under way ends; the run stops "
                            "then\n".encode())
            return
        self.raised = True
        raise Interrupted(signum)

    @contextlib.contextmanager
    def held(self):
        self.holding += 1
        try:
            yield
        finally:
            self.holding -= 1
        if not self.holding and self.received and not self.raised:
            self.raised = True
            raise Interrupted(self.received[0])


SIGNALS = Signals()


def end_group(process: subprocess.Popen) -> None:
    """The child's whole process group ended. SIGINT first: build_docx.sh and publish_completed.sh run their clean-up
    on it, and a Python helper removes its temporary folders (SIGTERM ends one without). Then SIGTERM, then SIGKILL,
    for whatever is left of the group."""
    for number, grace in ((signal.SIGINT, 10.0), (signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(process.pid, number)
        except (ProcessLookupError, PermissionError):
            break
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and group_alive(process.pid):
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.1)   # the leader is reaped as soon as it exits
        if not group_alive(process.pid):
            break
    with contextlib.suppress(Exception):
        process.wait(timeout=5)
    for stream in (process.stdout, process.stderr):
        with contextlib.suppress(Exception):
            stream.close()


def run_child(command: list[str], *, timeout: float | None = None, env: dict[str, str] | None = None,
              on_start=None) -> subprocess.CompletedProcess:
    """subprocess.run in a process group of its own. A terminal's Ctrl-C then reaches this tool only, which ends the
    child's whole group before it rolls back or cleans up: a build left running wrote into the audit afterwards."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                               errors="replace", env=env, start_new_session=True)
    try:
        if on_start is not None:
            on_start(process)
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        SIGNALS.stopping()   # every caller ends the run after this: a signal must not cut end_group short
        end_group(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def check_env() -> dict[str, str]:
    """check_claims_script.sh keeps a failed run's log in <audit>/work/ unless told not to (I4R_CLAIMS_LOG_DIR)."""
    return {**os.environ, "I4R_CLAIMS_LOG_DIR": "none"}


def publish_timeout() -> int:
    try:
        claims = max(int(os.environ.get("I4R_CLAIMS_TIMEOUT", "1800")), 0)
    except ValueError:
        claims = 1800
    return DOCS_TIMEOUT + claims   # publish_completed.sh runs the claims script once more


# --- the new claims script ------------------------------------------------------------------------------------------
def build(audit: pathlib.Path, scratch: pathlib.Path, ext: str) -> pathlib.Path:
    """The new claims script, built from the audit's own bound inputs in a scratch folder."""
    stage = scratch / "stage"
    (stage / "report").mkdir(parents=True)
    for name in ("comment.json", "claims_map.tsv"):
        shutil.copy2(audit / name, stage / name)
    shutil.copytree(audit / "verification_snippets", stage / "verification_snippets", symlinks=False)
    if (audit / "report/REPORT_final.md").is_file():
        shutil.copy2(audit / "report/REPORT_final.md", stage / "report/REPORT_final.md")
    if (audit / "pkg").is_dir():
        (stage / "pkg").symlink_to(audit / "pkg", target_is_directory=True)   # listed, never written
    # 2.7.11: the harness the audit froze (scripts/claims_harness.py) and the records its portable_1 harness takes the
    # program versions from. An audit that froze no leaf (every one published before 2.7.11) builds as before.
    for name in ("pipeline-policy.snapshot.json", "run.json"):
        if (audit / name).is_file():
            shutil.copy2(audit / name, stage / name)
    if (audit / "phase1").is_dir():
        (stage / "phase1").symlink_to(audit / "phase1", target_is_directory=True)   # read, never written
    done = run_child(["bash", str(BUILDER), str(stage)])
    script = stage / f"claims_script.{ext}"
    if done.returncode != 0 or not script.is_file():
        raise SystemExit("the builder refused:\n" + (done.stdout + done.stderr)[-1500:])
    if [p.name for p in stage.glob("claims_script.*")] != [script.name]:
        raise SystemExit("the builder produced a claims script in another language than the published one")
    return script


def snippets_inlined(audit: pathlib.Path, script_text: str) -> list[str]:
    """Code snippets the claims map names whose text is not in the new script (must be none)."""
    missing = []
    for line in (audit / "claims_map.tsv").read_text().splitlines():
        if "\t" in line and line.split("\t", 1)[1].startswith("snippet:"):
            path = audit / line.split("\t", 1)[1][8:]
            if path.read_text().rstrip("\n") not in script_text:
                missing.append(path.name)
    return missing


def run_check(audit: pathlib.Path, script: pathlib.Path) -> tuple[bool, str]:
    """check_claims_script.sh on the rebuilt script, against the audit's deposit. It writes nothing into the audit,
    not even its log on a failure (I4R_CLAIMS_LOG_DIR=none): a dry run leaves the audit as it is."""
    done = run_child(["bash", str(CHECK), str(audit), str(script)], env=check_env())
    text = (done.stdout + done.stderr).strip()
    return done.returncode == 0, text


def print_check_failure(run_text: str) -> None:
    print("refused: the rebuilt claims script does not run cleanly on its own:")
    print("\n".join("    " + line for line in run_text.splitlines()[1:30]))
    print("  (the rebuilt script was in a scratch folder this tool removes on exit; the lines above are its run's "
          "output, and the audit keeps no log of it)")


def log_event(audit: pathlib.Path, *args: str) -> None:
    """One journal event. A signal waits until it is written: log_event.sh cut off half way would leave the journal
    and its projections apart."""
    with SIGNALS.held():
        done = run_child(["bash", str(HERE / "log_event.sh"), "--run", str(audit / "run.json"), *args])
    if done.returncode != 0:
        raise SystemExit(f"could not journal the {args[0]} event:\n{(done.stdout + done.stderr)[-800:]}")


def registry_root(audit: pathlib.Path, receipt: dict) -> pathlib.Path:
    reg = receipt.get("registry") or {}
    if reg.get("filed") is not True or not reg.get("root"):
        raise SystemExit("the audit was not filed in a registry; amend only filed, published audits")
    return (audit / reg["root"]).resolve()


def registry_free(registry: pathlib.Path) -> None:
    """Waits, still stoppable by a signal, until no other filing holds the registry lock, so the filing held against
    signals right after never waits on another process (a teammate's filing, a stuck one). A filing that takes the
    lock in between makes the held one wait as before."""
    if not registry.is_dir():   # fr.create makes it, as before
        return
    with (registry / ".registry.lock").open("a+") as handle:
        fr.lock_wait.acquire_exclusive(handle, "the registry lock")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# --- files: atomic writes, backups, the record folder ----------------------------------------------------------------
def atomic_write_bytes(path: pathlib.Path, data: bytes, mode: int | None = None) -> None:
    """path replaced in one rename (fs_retry.replace_with_retry, for WSL hosts whose Windows programs hold new files):
    it is the old file or the new one, never half of either. It keeps the replaced file's mode."""
    if mode is None:
        mode = (path.stat().st_mode & 0o777) if path.is_file() and not path.is_symlink() else 0o644
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}{TEMP_MARK}", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        fs_retry.replace_with_retry(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def atomic_write_json(path: pathlib.Path, value: object) -> None:
    atomic_write_bytes(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def restore_file(backup: pathlib.Path, target: pathlib.Path) -> None:
    """A backed-up file put back in one rename, with its bytes, mode and times."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}{TEMP_MARK}restore.{os.getpid()}"
    if os.path.lexists(temporary):
        os.unlink(temporary)
    try:
        if backup.is_symlink():
            os.symlink(os.readlink(backup), temporary)
        else:
            shutil.copy2(backup, temporary)
        if target.is_dir() and not target.is_symlink():
            raise IsADirectoryError(f"{target.name} is now a folder")
        fs_retry.replace_with_retry(temporary, target)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def file_digest(path: pathlib.Path) -> str:
    return "symlink:" + os.readlink(path) if path.is_symlink() else sha256(path)


def begin_record(audit: pathlib.Path, record_dir: pathlib.Path, plan_name: str, plan: dict,
                 backups: list[str]) -> None:
    """The record folder, a copy of every file the amendment may change, and the plan. The plan says
    "backing_up" until every copy is complete and "backed_up" from then on: only then does anything in the audit
    change, so --rollback-pending never copies back a half-written backup."""
    try:
        record_dir.mkdir(parents=True)
    except FileExistsError:
        raise SystemExit(f"refused: {record_dir.relative_to(audit).as_posix()} already exists; run again") from None
    plan["state"] = "backing_up"
    atomic_write_json(record_dir / plan_name, plan)
    digests = {}
    for rel in backups:
        target = record_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(audit / rel, target, follow_symlinks=False)
        digests[rel] = file_digest(target)
    plan["backup_sha256"] = digests
    plan["state"] = "backed_up"
    atomic_write_json(record_dir / plan_name, plan)


def runner() -> dict:
    """Who is amending: this process and this machine (by a hash of its name, so no host name is kept)."""
    return {"pid": os.getpid(), "host": hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12]}


def process_alive(pid: object, needle: str | None = None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if needle is None:
        return True
    done = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return done.returncode != 0 or needle in done.stdout


def own_ids() -> set[int]:
    """This process, its group, its parent and the parent's group. After a reboot one of them can hold the pid or the
    process group an earlier run recorded: that is not the earlier run, which is over."""
    ids = {os.getpid(), os.getpgrp(), os.getppid()}
    with contextlib.suppress(OSError):
        ids.add(os.getpgid(os.getppid()))
    return ids


def hold(audit: pathlib.Path) -> int | None:
    """One run per audit on this machine: an exclusive lock on the audit folder itself (no lock file is written), held
    to the end of the run. None when another run holds it: two overlapping runs could each journal a note and file the
    audit again."""
    try:
        descriptor = os.open(audit, os.O_RDONLY)
    except OSError as exc:
        raise SystemExit(f"{display_name(audit)}: refused: the audit folder cannot be opened to lock it "
                         f"({exc.strerror}); nothing was done") from None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except OSError as exc:
        os.close(descriptor)
        raise SystemExit(f"{display_name(audit)}: refused: the audit folder cannot be locked here ({exc.strerror}), "
                         "so a second run could not be kept out; nothing was done") from None
    return descriptor


def group_alive(pgid: object) -> bool:
    if not isinstance(pgid, int) or pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def remove_tree(path: pathlib.Path) -> None:
    """rmtree that also removes read-only folders (a 2.7.9 Phase-1 snapshot is frozen 0555)."""
    if not path.exists():
        return
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    for dirpath, dirnames, _ in os.walk(path):
        for name in dirnames:
            target = os.path.join(dirpath, name)
            if not os.path.islink(target):
                with contextlib.suppress(OSError):
                    os.chmod(target, 0o700)
    shutil.rmtree(path, ignore_errors=True)


def tree_state(root: pathlib.Path) -> tuple[dict[str, str], set[str]]:
    """Every file under root by its sha256 (a symlink by its target, never followed), and every folder; .DS_Store is
    not counted."""
    hashes: dict[str, str] = {}
    folders: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        base = pathlib.Path(dirpath)
        for name in dirnames + filenames:
            path = base / name
            if name == ".DS_Store":
                continue
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                hashes[relative] = "symlink:" + os.readlink(path)
            elif name in filenames:
                hashes[relative] = sha256(path) if path.is_file() else "special"
            else:
                folders.add(relative)
    return hashes, folders


def tree_hashes(root: pathlib.Path) -> dict[str, str]:
    return tree_state(root)[0]


def tree_changes(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    return {"changed": sorted(k for k in before.keys() & after.keys() if before[k] != after[k]),
            "created": sorted(after.keys() - before.keys()),
            "removed": sorted(before.keys() - after.keys())}


def restore(audit: pathlib.Path, record_dir: pathlib.Path, plan: dict,
            before: tuple[dict[str, str], set[str]] | None) -> list[str]:
    """The audit put back from the record folder: the files the run created removed (with `before`, anything new;
    without it, after a killed run, anything new in build_docx.sh's own folders and a replacement's temporary file),
    every backup restored, the folders the run created removed, and the build folders' times reset. What could not be
    put back is returned; the record folder is removed only when nothing is left."""
    problems: list[str] = []
    record_rel = record_dir.relative_to(audit).as_posix()

    def ours(rel: str) -> bool:
        return rel == "amendments" or rel == record_rel or rel.startswith(record_rel + "/")

    footprint = plan.get("footprint_before")
    known_files = set((footprint or {}).get("files") or [])
    known_dirs = dict((footprint or {}).get("dirs") or {})
    if before is not None:
        created = [rel for rel in tree_state(audit)[0].keys() - before[0].keys() if not ours(rel)]
    else:
        created = [rel for rel in footprint_state(audit)[0] if rel not in known_files] if footprint is not None else []
    for rel in plan.get("backed_up") or []:   # a temporary file of a replacement cut short
        target = audit / rel
        created += [p.relative_to(audit).as_posix() for p in target.parent.glob(f".{glob_escape(target.name)}"
                                                                                   f"{TEMP_MARK}*")]
    for rel in sorted(set(created)):
        try:
            (audit / rel).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            problems.append(f"{rel}: {exc}")
    digests = plan.get("backup_sha256") or {}
    for rel in plan.get("backed_up") or []:
        target = audit / rel
        try:
            if (os.path.lexists(target) and digests.get(rel) and file_digest(target) == digests[rel]
                    and target.is_symlink() == (record_dir / rel).is_symlink()):
                continue
            restore_file(record_dir / rel, target)
        except OSError as exc:
            problems.append(f"{rel}: {exc}")
    if before is not None:
        new_dirs = [rel for rel in tree_state(audit)[1] - before[1] if not ours(rel)]
    else:
        new_dirs = [rel for rel in footprint_state(audit)[1] if rel not in known_dirs] if footprint is not None else []
    for rel in sorted(new_dirs, key=lambda r: r.count("/"), reverse=True):
        folder = audit / rel
        with contextlib.suppress(OSError):
            (folder / ".DS_Store").unlink()
        try:
            folder.rmdir()
        except OSError as exc:
            problems.append(f"{rel}/: {exc}")
    for rel, mtime in sorted(known_dirs.items(), key=lambda item: item[0].count("/"), reverse=True):
        with contextlib.suppress(OSError, TypeError):
            if (audit / rel).is_dir():
                os.utime(audit / rel, (mtime, mtime))
    for rel in plan.get("backed_up") or []:
        target = audit / rel
        if not os.path.lexists(target) or (digests.get(rel) and file_digest(target) != digests[rel]):
            problems.append(f"{rel} still differs from its backup")
    if before is not None:
        left = tree_changes(before[0], tree_state(audit)[0])
        problems += [f"{rel} still differs" for kind in ("changed", "removed", "created") for rel in left[kind]
                     if not ours(rel) and f"{rel} still differs from its backup" not in problems]
    if not problems:
        remove_tree(record_dir)
        if not plan.get("amendments_existed", True):
            with contextlib.suppress(OSError):
                (audit / "amendments").rmdir()
    return sorted(set(problems))


def undo(audit: pathlib.Path, record_dir: pathlib.Path, plan: dict,
         before: tuple[dict[str, str], set[str]] | None) -> list[str]:
    """Before the note: the audit put back. A plan still backing up means nothing in the audit has changed yet."""
    if plan.get("state") == "backed_up":
        return restore(audit, record_dir, plan, before)
    if record_dir.is_dir() and plan.get("state") == "backing_up":
        remove_tree(record_dir)
        if not plan.get("amendments_existed", True):
            with contextlib.suppress(OSError):
                (audit / "amendments").rmdir()
    return []


def mark_journaled(record_dir: pathlib.Path, plan: dict) -> None:
    """Right after the note: the plan says so, and --rollback-pending never takes the record for one stopped before
    its note, whatever the journal later holds."""
    plan["journaled"] = True
    with contextlib.suppress(OSError):   # the publication's first save writes it too
        save_plan(record_dir, plan)


def stopped_unknown(audit: pathlib.Path, record_rel: str) -> bool:
    """A run stopped while its note was being journaled, with a journal that cannot be read: whether the note landed
    cannot be told, so nothing is put back (said so on stderr)."""
    try:
        journal_events(audit)
    except JournalUnreadable as unread:
        print(f"stopped part way: the audit's journal cannot be read ({unread.reason}), so whether the note landed "
              f"cannot be told and nothing was put back; {record_rel}/ holds the backups. {JOURNAL_REMEDY}: --apply "
              "then resumes the amendment if its note landed; otherwise it refuses, and --rollback-pending puts the "
              "audit back.", file=sys.stderr)
        return True
    return False


def report_undo(problems: list[str], record_rel: str) -> None:
    if not problems:
        print("rolled back: the audit is as it was before this run", file=sys.stderr)
    else:
        print(f"NOT rolled back in full; {record_rel}/ keeps the backups, and --rollback-pending tries again:"
              + "".join(f"\n  NOT RESTORED: {p}" for p in problems), file=sys.stderr)


# ---------------------------------------------------------------------------------------------------------------------
# --rebuild-documents (2.7.10). A comment built on 2.7.4 or earlier carries a document_build_manifest.json naming that
# release's document scripts; 2.7.5 changed document_release.py, so fr.artifact_paths refuses the manifest ("code
# identity differs from the release verifier") and neither the receipt nor the claims script can be bound again. This
# mode rebuilds the comment's PDF and DOCX with this release's build_docx.sh first, after proving on a full scratch copy
# that a reader sees the same comment: the comment's sources (REPORT_final.md, front matter, captions) are bound by the
# receipt and never written, so a difference can only come from rendering, and the text of both documents is compared.
# ---------------------------------------------------------------------------------------------------------------------
DOCS_REASON = "comment documents rebuilt with the current builder so the claims script can be re-bound"
DOCS_STALE_CODE = "code identity differs from the release verifier"   # document_release.expected_code's refusal
DOCS_MANIFEST = "document_build_manifest.json"
DOCS_CODE = {"builder": "build_docx.sh", "verifier": "document_release.py", "cache_helper": "document_build_cache.py"}
# Where build_docx.sh writes besides the bound PDF, DOCX and manifest: its cache, its private transactions (it sweeps
# those older than an hour), its failure diagnostic and its lock. A build that touches anything else is refused.
DOCS_FOOTPRINT = (".cache/document-builds/", ".document-build-transactions/", "document_build_diagnostics/")
DOCS_FOOTPRINT_FILES = (".document-build.lock",)
DOCS_TIMEOUT = 1800
WORDML = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NORMALISATION = ("whitespace collapsed; a line ending in a hyphen or slash joined to the next; soft hyphens and "
                 "ligatures undone; a header or footer line repeated on every PDF page (a running title, a build "
                 "date, a page number) dropped")
# typography a renderer may set differently without changing a word or a number
TYPOGRAPHY = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
                            "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
                            "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
                            "―": "-", "−": "-", "…": "..."})
LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
                           "ﬅ": "st", "ﬆ": "st", "­": None})
LINE_END_BREAK = re.compile(r"(?<=\w)([-‐‑/])[ \t]*\n\s*(?=\w)")
TEXT_RESULTS = {
    "same": "same text",
    "typography": "differs in typography only (quotes, dashes): the same words and numbers in the same order",
    "order": "differs in order only: the same words and numbers in another order (table layout or reading order)",
    "table_header_rows": "differs only by a table header row repeated on a different number of pages",
    "words": "WORDS OR NUMBERS DIFFER",
}


def reason_only(text: str) -> str:
    """A check's reason without document_release's remedy ("Rebuild the document with build_docx.sh, then re-run
    finalize_run.sh"): followed on a published audit, it leaves the receipt and the publication apart."""
    return text.split(" Rebuild the document with build_docx.sh")[0]


def in_build_footprint(relative: str, bound: set[str]) -> bool:
    return relative in bound or relative in DOCS_FOOTPRINT_FILES or relative.startswith(DOCS_FOOTPRINT)


def footprint_state(audit: pathlib.Path) -> tuple[list[str], dict[str, float]]:
    """The files in build_docx.sh's own folders (and its lock), and those folders with their modification times."""
    files: list[str] = []
    folders: dict[str, float] = {}
    for top in [".cache"] + [prefix.rstrip("/") for prefix in DOCS_FOOTPRINT]:
        path = audit / top
        if path.is_symlink() or not path.is_dir():
            if path.is_symlink() or path.is_file():
                files.append(top)
            continue
        folders[top] = path.stat().st_mtime
        if top == ".cache":   # only its document-builds/ folder is the build's
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            base = pathlib.Path(dirpath)
            for name in dirnames:
                entry = base / name
                if entry.is_symlink():
                    files.append(entry.relative_to(audit).as_posix())
                else:
                    folders[entry.relative_to(audit).as_posix()] = entry.stat().st_mtime
            files += [(base / name).relative_to(audit).as_posix() for name in filenames]
    files += [name for name in DOCS_FOOTPRINT_FILES if os.path.lexists(audit / name)]
    return sorted(set(files)), folders


def quietly(function, *args, **kwargs):
    """Call a receipt function without the 'document boundary ...' lines the document verifier prints."""
    with contextlib.redirect_stdout(io.StringIO()):
        return function(*args, **kwargs)


def tail(done: subprocess.CompletedProcess, lines: int = 25) -> str:
    text = ((done.stdout or "") + (done.stderr or "")).strip().splitlines()
    return "\n".join("    " + line for line in text[-lines:])


def bound_documents(audit: pathlib.Path) -> tuple[str, str | None]:
    artifacts = json.loads((audit / DOCS_MANIFEST).read_text()).get("artifacts") or {}
    pdf = (artifacts.get("pdf") or {}).get("path")
    docx = (artifacts.get("docx") or {}).get("path") if artifacts.get("docx") else None
    if not isinstance(pdf, str) or not re.fullmatch(r"report/[^/]+\.pdf", pdf):
        raise SystemExit("refused: the document manifest does not bind a PDF under report/")
    return pdf, docx


def pkg_writable(audit: pathlib.Path) -> list[pathlib.Path]:
    """Entries of pkg/ with a write bit. The synced folder strips the read-only mode intake gave the deposit, and the
    post-delta archive gate that publish_completed.sh replays refuses it ("archive package directory is not read-only")."""
    package = audit / "pkg"
    if package.is_symlink() or not package.is_dir():
        return []
    entries = [package] + sorted(package.rglob("*"))
    return [p for p in entries if not p.is_symlink() and (p.is_dir() or p.is_file()) and p.stat().st_mode & 0o222]


def make_pkg_read_only(audit: pathlib.Path) -> int:
    """chmod a-w on pkg/ (INSTALL.md, re-filing a published comment, step 2): modes only, no byte of the deposit."""
    entries = pkg_writable(audit)
    for path in sorted(entries, key=lambda p: (p.is_dir(), -len(p.parts))):   # files, then folders deepest first
        path.chmod(path.stat().st_mode & ~0o222)
    return len(entries)


def published_dirs(root: pathlib.Path, journal: str, paper: str) -> tuple[pathlib.Path, pathlib.Path]:
    """comments/<journal>/<paper> and metadata/<journal>/<paper>, as publish_completed.sh names them."""
    out = subprocess.run(["bash", "-c", '. "$1"; i4r_published_comment_dir "$2" "$3" "$4"; echo; '
                          'i4r_published_metadata_dir "$2" "$3" "$4"', "_", str(HERE / "tree_layout.sh"), str(root),
                          journal, paper], capture_output=True, text=True, check=True).stdout.splitlines()
    return pathlib.Path(out[0]), pathlib.Path(out[1])


# --- the comment's text, old against new ------------------------------------------------------------------------------
def pdf_pages(pdf: pathlib.Path) -> list[str]:
    done = subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"], capture_output=True)
    if done.returncode != 0:
        raise SystemExit(f"refused: pdftotext could not read {pdf.name}: "
                         f"{done.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    pages = done.stdout.decode("utf-8", "replace").split("\f")
    while pages and not pages[-1].strip():
        pages.pop()
    return pages


def without_running_lines(pages: list[str]) -> list[str]:
    """The pages without a header or footer line that every page repeats: a running title, a build date or a page
    number is set by the build, not written in the comment."""
    lines = [[line.strip() for line in page.splitlines() if line.strip()] for page in pages]
    if len(lines) < 3 or not all(lines):
        return pages
    for end in (0, -1):
        values = [page[end] for page in lines]
        if len(set(values)) == 1 or values == [str(number) for number in range(1, len(values) + 1)]:
            lines = [page[1:] if end == 0 else page[:-1] for page in lines]
            if not all(lines):
                break
    return ["\n".join(page) for page in lines]


def normalised_tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text).translate(LIGATURES).replace("\r\n", "\n")
    return LINE_END_BREAK.sub(r"\1", text).split()


def wordml_text(element: ElementTree.Element) -> str:
    parts = []
    for node in element.iter():
        if node.tag == WORDML + "t":
            parts.append(node.text or "")
        elif node.tag == WORDML + "tab":
            parts.append("\t")
        elif node.tag in (WORDML + "br", WORDML + "cr", WORDML + "p"):
            parts.append("\n")
        elif node.tag == WORDML + "noBreakHyphen":
            parts.append("-")
    return "".join(parts)


def docx_body(docx: pathlib.Path) -> ElementTree.Element:
    with zipfile.ZipFile(docx) as archive:
        return ElementTree.fromstring(archive.read("word/document.xml"))


def docx_header_rows(docx: pathlib.Path | None) -> list[list[str]]:
    """Each table row the DOCX repeats at the top of every page its table spans."""
    if docx is None:
        return []
    rows = []
    for row in docx_body(docx).iter(WORDML + "tr"):
        properties = row.find(WORDML + "trPr")
        if properties is not None and properties.find(WORDML + "tblHeader") is not None:
            rows.append(normalised_tokens(wordml_text(row)))
    return [row for row in rows if row]


def short_diff(old: list[str], new: list[str], limit: int = 200) -> list[str]:
    """A unified diff of the normalised words: '-' only in the old document, '+' only in the new."""
    def clip(words: list[str]) -> str:
        text = " ".join(words)
        return text if len(text) <= 240 else text[:110] + " ... " + text[-110:]
    lines: list[str] = []
    for group in difflib.SequenceMatcher(None, old, new, autojunk=False).get_grouped_opcodes(6):
        first, last = group[0], group[-1]
        lines.append(f"@@ words -{first[1] + 1},{last[2] - first[1]} +{first[3] + 1},{last[4] - first[3]} @@")
        for tag, a1, a2, b1, b2 in group:
            if tag == "equal":
                lines.append("  " + clip(old[a1:a2]))
            if tag in ("replace", "delete"):
                lines.append("- " + clip(old[a1:a2]))
            if tag in ("replace", "insert"):
                lines.append("+ " + clip(new[b1:b2]))
    return lines if len(lines) <= limit else lines[:limit] + [f"... {len(lines) - limit} more diff lines"]


def compare_tokens(old: list[str], new: list[str], header_rows: list[list[str]]) -> dict:
    if old == new:
        return {"result": "same", "words": len(old)}
    fold_old = [token.translate(TYPOGRAPHY) for token in old]
    fold_new = [token.translate(TYPOGRAPHY) for token in new]
    delta = collections.Counter(fold_new)
    delta.subtract(collections.Counter(fold_old))
    delta = collections.Counter({token: count for token, count in delta.items() if count})
    if fold_old == fold_new:
        result = "typography"
    elif not delta:
        result = "order"
    elif any(set(delta) == set(row) and len({delta[t] / row[t] for t in row}) == 1
             and float(delta[next(iter(row))] / row[next(iter(row))]).is_integer()
             for row in (collections.Counter(t.translate(TYPOGRAPHY) for t in r) for r in header_rows)):
        result = "table_header_rows"
    else:
        result = "words"
    return {"result": result, "words_old": len(old), "words_new": len(new),
            "only_old": sorted(token for token, count in delta.items() if count < 0)[:40],
            "only_new": sorted(token for token, count in delta.items() if count > 0)[:40],
            "diff": short_diff(old, new)}


def compare_documents(old_pdf: pathlib.Path, new_pdf: pathlib.Path, old_docx: pathlib.Path | None,
                      new_docx: pathlib.Path | None) -> dict:
    """The comment's text in both documents, old against new, after NORMALISATION."""
    header_rows = docx_header_rows(old_docx) + docx_header_rows(new_docx)
    pdf_tokens = [normalised_tokens("\n".join(without_running_lines(pdf_pages(p)))) for p in (old_pdf, new_pdf)]
    result: dict = {"normalisation": NORMALISATION, "pdf": compare_tokens(*pdf_tokens, header_rows)}
    if old_docx is not None or new_docx is not None:
        if old_docx is None or new_docx is None:
            result["docx"] = {"result": "words", "diff": ["- (no DOCX)" if old_docx is None else "+ (no DOCX)"]}
        else:
            docx_tokens = [normalised_tokens(wordml_text(docx_body(d))) for d in (old_docx, new_docx)]
            result["docx"] = compare_tokens(*docx_tokens, header_rows)
    result["same"] = all(result[kind]["result"] == "same" for kind in ("pdf", "docx") if kind in result)
    return result


def text_outcome(text: dict) -> dict:
    """What two comparisons must share to be the same result: per document, its result and its diff."""
    return {kind: (text[kind]["result"], text[kind].get("diff", [])) for kind in ("pdf", "docx") if kind in text}


def print_text(text: dict, indent: str = "  ", diff_lines: int = 14) -> None:
    for kind in ("pdf", "docx"):
        if kind not in text:
            continue
        row = text[kind]
        print(f"{indent}comment text, {kind.upper()}: {TEXT_RESULTS[row['result']]}"
              + (f" ({row['words']} words)" if row["result"] == "same" else
                 f" ({row.get('words_old', '?')} words old, {row.get('words_new', '?')} new)"))
        if row["result"] != "same":
            if row.get("only_old") or row.get("only_new"):
                print(f"{indent}  words only in the old: {' '.join(row.get('only_old') or []) or '(none)'}")
                print(f"{indent}  words only in the new: {' '.join(row.get('only_new') or []) or '(none)'}")
            first = row.get("diff", [])[:diff_lines]
            for line in first:
                print(f"{indent}  | {line}")
            if len(row.get("diff", [])) > len(first):
                print(f"{indent}  | ... ({len(row['diff']) - len(first)} more lines, all in AMENDMENT.json on --apply)")


# --- the journal: amendments journaled, finished, stopped before their note ------------------------------------------
def without_audit_path(text: str, audit: pathlib.Path) -> str:
    for path in {str(audit), str(audit.resolve())}:
        text = text.replace(path + "/", "").replace(path, ".")
    return text


class JournalUnreadable(SystemExit):
    """The refusal of a run whose journal cannot be read; `reason` is the loader's error without local paths."""

    def __init__(self, reason: str):
        super().__init__(f"refused: the audit's journal cannot be read ({reason}); nothing more was done. "
                         f"{JOURNAL_REMEDY}, then run this again.")
        self.reason = reason


JOURNAL_REMEDY = ("Make run.events.jsonl readable again (a partial last line a crash left, its permissions, or a copy "
                  "from Dropbox's version history)")


def journal_events(audit: pathlib.Path) -> list[dict]:
    """The audit's journal (none yet: empty). One that cannot be read refuses the run: read as empty, a journaled
    amendment looked like one stopped before its note, and --rollback-pending put the audit back under its note."""
    try:
        return lifecycle_journal.load_events(audit, allow_legacy_prefix=True)
    except Exception as exc:
        raise JournalUnreadable(without_audit_path(str(exc), audit)[-300:]) from None


def projections_behind(audit: pathlib.Path) -> bool:
    """The journal's projections (run.json, invocations.watermark.json, run_metrics.json) name an earlier event than
    its last: a run cut off in the few milliseconds between a journal write and the projections it rewrites."""
    tip = lifecycle_journal.watermark(journal_events(audit))
    for rel, key in (("run.json", "journal_watermark"), ("invocations.watermark.json", None),
                     ("run_metrics.json", "journal_watermark")):
        try:
            value = json.loads((audit / rel).read_text())
        except (OSError, ValueError):
            return True
        if not isinstance(value, dict) or (value if key is None else value.get(key)) != tip:
            return True
    return False


def amendment_notes(events: list[dict]) -> list[tuple[int, dict]]:
    return [(index, event.get("payload") or {}) for index, event in enumerate(events)
            if event.get("type") == "note" and (event.get("payload") or {}).get("kind") == "claims_script_amendment"]


def note_for_record(audit: pathlib.Path, record_rel: str) -> tuple[int, dict] | None:
    """The journaled note of the amendment whose record folder is record_rel."""
    found = [(index, payload) for index, payload in amendment_notes(journal_events(audit))
             if payload.get("record") == record_rel]
    return found[-1] if found else None


def note_hashes(payload: dict) -> dict:
    """The files a journaled amendment installed, by path: its claims script, and its documents (rebuilt, or the
    document manifest it left unchanged)."""
    hashes = {payload.get("path"): payload.get("to_sha256")}
    if payload.get("documents") == "rebuilt":
        hashes[payload.get("pdf_path")] = payload.get("pdf_to_sha256")
        if payload.get("docx_path"):
            hashes[payload["docx_path"]] = payload.get("docx_to_sha256")
        hashes[DOCS_MANIFEST] = payload.get("document_manifest_to_sha256")
    elif payload.get("document_manifest_sha256"):
        hashes[DOCS_MANIFEST] = payload["document_manifest_sha256"]
    return hashes


def receipt_binds(audit: pathlib.Path, hashes: dict) -> bool:
    try:
        receipt = json.loads((audit / fr.RECEIPT).read_text())
    except (OSError, ValueError):
        return False
    rows = {row.get("path"): row.get("sha256") for row in receipt.get("artifacts") or [] if isinstance(row, dict)}
    return all(isinstance(rel, str) and isinstance(digest, str) and rows.get(rel) == digest
               for rel, digest in hashes.items())


def journaled_amendment(audit: pathlib.Path) -> dict | None:
    """The latest journaled amendment and whether it is finished: its record folder holds AMENDMENT.json and the
    receipt binds what it installed. None when there is none, or when the journal has gained an event since then
    that a finished audit cannot (a re-finalization): the amendment is history then."""
    events = journal_events(audit)
    notes = amendment_notes(events)
    if not notes:
        return None
    index, payload = notes[-1]
    if any(str(event.get("type")) not in fr.POST_FINALIZATION_EVENTS for event in events[index + 1:]):
        return None
    record_rel = str(payload.get("record") or "")
    if not RECORD_RE.fullmatch(record_rel):
        return None
    record_dir = audit / record_rel
    finished = (record_dir / RECORD).is_file() and receipt_binds(audit, note_hashes(payload))
    return {"index": index, "payload": payload, "record_rel": record_rel, "record_dir": record_dir,
            "documents": payload.get("documents") == "rebuilt", "finished": finished}


def pending_records(audit: pathlib.Path) -> list[pathlib.Path]:
    """Record folders of runs stopped before their note: no AMENDMENT.json, and no journaled note names them."""
    folder = audit / "amendments"
    if not folder.is_dir():
        return []
    named = {str(payload.get("record")) for _, payload in amendment_notes(journal_events(audit))}
    return [path for path in sorted(folder.glob("*-claims_script")) if path.is_dir() and not path.is_symlink()
            and not (path / RECORD).exists() and path.relative_to(audit).as_posix() not in named]


def lost_note(rel: str) -> str:
    return (f"refused: {rel}/ belongs to an amendment whose note was journaled (its plan was written after the note), "
            "yet no note in run.events.jsonl names it: the journal has lost events (an older copy restored, or one "
            "replaced by a sync?). Nothing was changed. Do not remove the folder: put back the run.events.jsonl that "
            "holds the note, and --apply then finishes the amendment.")


def journaled_plan(plan: dict) -> bool:
    """The plan was saved after the note: marked so right after it, or naming where the paper is republished."""
    return bool(plan.get("journaled")) or plan.get("publication") is not None


def plan_of(record_dir: pathlib.Path) -> tuple[str | None, dict | None]:
    """(the plan file's name, the plan); None for a plan that cannot be read or is not an object."""
    for name in (CLAIMS_PLAN, DOCS_PLAN):
        if (record_dir / name).is_file():
            try:
                plan = json.loads((record_dir / name).read_text())
            except (OSError, ValueError):
                return name, None
            return name, plan if isinstance(plan, dict) else None
    return None, {}


def rollback_pending(audit: pathlib.Path, pending: list[pathlib.Path]) -> int:
    """--rollback-pending: a run stopped before its note (SIGKILL, a power cut) is undone from its record folder."""
    print(f"{display_name(audit)}:")
    if not pending:
        journaled = journaled_amendment(audit)
        if journaled and not journaled["finished"]:
            print(f"refused: the amendment in {journaled['record_rel']} is journaled, so it cannot be rolled back; "
                  f"--apply{' --rebuild-documents' if journaled['documents'] else ''} finishes it")
            return 1
        print("  nothing to roll back: no amendment was stopped before its note")
        return 0
    status = 0
    for record_dir in pending:
        rel = record_dir.relative_to(audit).as_posix()
        plan_name, plan = plan_of(record_dir)
        if plan_name is None:
            if any(p.is_file() and p.name != ".DS_Store" for p in record_dir.rglob("*")):
                print(f"refused: {rel}/ holds files but no plan of this tool's; look at it by hand")
                status = 1
                continue
            remove_tree(record_dir)
            print(f"  removed {rel}/: the run stopped before it backed anything up, and nothing in the audit changed")
            continue
        if plan is None:
            print(f"refused: {rel}/{plan_name} cannot be read; look at it by hand")
            status = 1
            continue
        if journaled_plan(plan):
            print(lost_note(rel))
            status = 1
            continue
        who = plan.get("runner") or {}
        if who.get("host") and who.get("host") != runner()["host"]:
            print(f"refused: the run that wrote {rel}/ was started on another machine; roll it back there (it may "
                  "still be running)")
            status = 1
            continue
        if who.get("pid") not in own_ids() and process_alive(who.get("pid"), "amend_claims_script"):
            print(f"refused: the run that wrote {rel}/ is still running (pid {who.get('pid')}); stop it first")
            status = 1
            continue
        if plan.get("build_group") not in own_ids() and group_alive(plan.get("build_group")):
            group = plan.get("build_group")
            print(f"refused: the document build that run started is still running (process group {group}); wait "
                  f"for it to end, or end it with: kill -TERM -- -{group}")
            status = 1
            continue
        SIGNALS.stopping()   # a rollback begins: a later signal must not cut it in two
        if plan.get("state") != "backed_up":
            undo(audit, record_dir, plan, None)
            print(f"  removed {rel}/: the run stopped while backing up, before anything in the audit changed")
            continue
        problems = restore(audit, record_dir, plan, None)
        if problems:
            print(f"refused: {rel}/ could not put everything back:"
                  + "".join(f"\n  NOT RESTORED: {p}" for p in problems))
            status = 1
            continue
        print(f"  rolled back: the run that wrote {rel}/ stopped before its note; its "
              f"{len(plan.get('backed_up') or [])} backed-up files are restored, what its document build created is "
              "removed, and so is the folder")
    return status


# --- before anything changes: the published set, what --apply writes -------------------------------------------------
def published_problems(audit: pathlib.Path, root: pathlib.Path, comments: pathlib.Path, meta: pathlib.Path,
                       claims_name: str, pdf_rel: str, docx_rel: str | None) -> tuple[list[str], list[str]]:
    """(the published files that are not the audit's, by label; anything else wrong with the published set)."""
    if not meta.is_dir() or not comments.is_dir():
        missing = [shown(path, root) for path in (comments, meta) if not path.is_dir()]
        return [], [f"nothing is published at {' and '.join(missing)}"]
    pairs = {"claims script": (meta / claims_name, audit / claims_name),
             "receipt": (meta / fr.RECEIPT, audit / fr.RECEIPT),
             "document manifest": (meta / DOCS_MANIFEST, audit / DOCS_MANIFEST),
             "comment.json": (meta / "comment.json", audit / "comment.json"),
             "PDF": (comments / pathlib.PurePosixPath(pdf_rel).name, audit / pdf_rel)}
    if docx_rel:
        pairs["DOCX"] = (comments / pathlib.PurePosixPath(docx_rel).name, audit / docx_rel)
    differ = [label for label, (theirs, own) in pairs.items()
              if theirs.is_symlink() or not theirs.is_file() or sha256(theirs) != sha256(own)]
    try:
        rows = {row["file"]: row for row in json.loads((meta / "release-manifest.json").read_text())["files"]}
    except (OSError, ValueError, KeyError, TypeError):
        return differ, [f"{shown(meta, root)} has no readable release-manifest.json"]
    entries = {folder: [p for p in folder.iterdir() if p.name != ".DS_Store"] for folder in (comments, meta)}
    other = [f"{shown(p, root)} is not a regular file" for found in entries.values() for p in found
             if p.is_symlink() or not p.is_file()]
    in_comments = {p.name for p in entries[comments]}
    in_meta = {p.name for p in entries[meta]} - {"release-manifest.json"}
    comment_files = {pathlib.PurePosixPath(rel).name for rel in (pdf_rel, docx_rel) if rel}
    if in_comments != comment_files:
        other.append(f"{shown(comments, root)} holds {', '.join(sorted(in_comments)) or 'nothing'}, not the "
                     f"comment's {', '.join(sorted(comment_files))}")
    if in_comments | in_meta != set(rows):
        other.append("the published folders do not hold exactly the files their release-manifest.json lists")
    for file_name, row in sorted(rows.items()):
        where = comments / file_name if file_name in in_comments else meta / file_name
        if (where.is_symlink() or not where.is_file() or sha256(where) != row.get("sha256")
                or ("bytes" in row and where.stat().st_size != row["bytes"])):
            other.append(f"the published {file_name} no longer matches its release-manifest row")
    return differ, other


def unwritable(audit: pathlib.Path, root: pathlib.Path, journal: str, paper: str, claims_name: str,
               bound: set[str] | None) -> list[str]:
    """Every file or folder --apply changes in the audit, the published folders and old_runs/ that this user may not
    write, checked before anything changes: a read-only claims script used to break the rollback itself. (The
    registry is written after the note; a failure there is resumed.)"""
    comments, meta = published_dirs(root, journal, paper)
    paths = [audit, audit / "work", audit / "amendments", audit / claims_name, audit / PROJECTION,
             audit / fr.RECEIPT, audit / ".run.events.lock"]
    paths += [audit / name for name in sorted(fr.JOURNAL_PROJECTIONS)]
    if bound is not None:   # --rebuild-documents: the documents and build_docx.sh's own folders
        paths += [audit / "report", audit / ".cache"] + [audit / rel for rel in sorted(bound)]
        files, folders = footprint_state(audit)
        paths += [audit / rel for rel in files] + [audit / rel for rel in folders]
    paths += [comments, meta, comments.parent, meta.parent]
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    for kind in KINDS:   # the folder each previous set moves into, or the nearest one that exists (old_runs/, the tree)
        folder = root / "old_runs" / f"archived_{day}_claims_amendment" / kind / journal
        while not os.path.lexists(folder) and folder != root:
            folder = folder.parent
        paths.append(folder)
    return sorted({shown(path, root) for path in paths
                   if os.path.lexists(path) and not path.is_symlink() and not os.access(path, os.W_OK)})


def preflight(audit: pathlib.Path, root: pathlib.Path, journal: str, paper: str, claims_name: str, pdf_rel: str,
              docx_rel: str | None, documents: bool) -> bool:
    """The published set and the permissions --apply needs, before anything changes (in the dry run too). Prints
    the refusal and returns False when one fails."""
    comments, meta = published_dirs(root, journal, paper)
    differ, other = published_problems(audit, root, comments, meta, claims_name, pdf_rel, docx_rel)
    if differ:
        print(f"refused: the published {', '.join(differ)} at {shown(comments, root)} and {shown(meta, root)} "
              f"{'is' if len(differ) == 1 else 'are'} not the audit's; resolve that first")
        return False
    if other:
        print("refused: the published set does not match its release manifest, so it cannot be republished as it "
              "is: " + "; ".join(other[:6]))
        return False
    blocked = unwritable(audit, root, journal, paper, claims_name,
                         ({pdf_rel, DOCS_MANIFEST} | ({docx_rel} if docx_rel else set())) if documents else None)
    if blocked:
        print_unwritable(blocked)
        return False
    return True


def print_unwritable(blocked: list[str]) -> None:
    print("refused: --apply changes these and this user may not write them: " + ", ".join(blocked[:10])
          + (f" (and {len(blocked) - 10} more)" if len(blocked) > 10 else "") + "; nothing was changed")


# --- the rehearsal on a scratch copy ---------------------------------------------------------------------------------
def documents_stale_only(copy: pathlib.Path) -> list[str]:
    """The document scripts whose recorded identity is not this release's. On a SCRATCH COPY only: each stale identity
    is set to this release's and fr.artifact_paths must then pass, so nothing but the code identity is stale."""
    path = copy / DOCS_MANIFEST
    original = path.read_bytes()
    manifest = json.loads(original)
    code = manifest.get("code") if isinstance(manifest.get("code"), dict) else {}
    stale = []
    for key, name in DOCS_CODE.items():
        current = document_release.code_identity(HERE / name, name)
        if code.get(key) != current:
            stale.append(name)
            code[key] = current
    if not stale:
        raise SystemExit("refused: the document manifest names this release's document scripts, yet it does not "
                         "check; this is not a stale document build")
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    try:
        quietly(fr.artifact_paths, copy, json.loads((copy / "run.json").read_text()))
    except ValueError as exc:
        raise SystemExit("refused: the comment's documents fail for a reason other than the document scripts' code "
                         f"identity, so rebuilding them is not this tool's to decide: {exc}") from None
    finally:
        path.write_bytes(original)
    return stale


def journal_claims_note(audit: pathlib.Path, *fields: str) -> None:
    log_event(audit, "note", "kind=claims_script_amendment", "documents=rebuilt", *fields, f"text={DOCS_REASON}")


def summary_line(run_text: str) -> str:
    return next((line for line in run_text.splitlines() if "checks," in line),
                run_text.splitlines()[0] if run_text else "")


def note_fields(claims_name: str, old_sha: str, new_sha: str, run_text: str, pdf_rel: str, docx_rel: str | None,
                old_docs: dict[str, str], new_docs: dict[str, str], record: str, outcome: str) -> list[str]:
    fields = [f"path={claims_name}", f"from_sha256={old_sha}", f"to_sha256={new_sha}",
              f"builder=i4r-audit {version()}", f"standalone_run={summary_line(run_text)}",
              f"pdf_path={pdf_rel}", f"pdf_from_sha256={old_docs[pdf_rel]}", f"pdf_to_sha256={new_docs[pdf_rel]}"]
    if docx_rel:
        fields += [f"docx_path={docx_rel}", f"docx_from_sha256={old_docs[docx_rel]}",
                   f"docx_to_sha256={new_docs[docx_rel]}"]
    return fields + [f"document_manifest_from_sha256={old_docs[DOCS_MANIFEST]}",
                     f"document_manifest_to_sha256={new_docs[DOCS_MANIFEST]}",
                     f"comment_text={outcome}", f"record={record}"]


def journal_plain_note(audit: pathlib.Path, claims_name: str, old_sha: str, new_sha: str, run_text: str,
                       manifest_sha: str, record: str, reason: str) -> None:
    log_event(audit, "note", "kind=claims_script_amendment", "documents=unchanged", f"path={claims_name}",
              f"from_sha256={old_sha}", f"to_sha256={new_sha}", f"builder=i4r-audit {version()}",
              f"standalone_run={summary_line(run_text)}", f"document_manifest_sha256={manifest_sha}",
              f"record={record}", f"text={reason}")


def rebind_claims_script(audit: pathlib.Path, claims_name: str, new_script: pathlib.Path,
                         projection: dict, rows: list[dict]) -> str:
    """The claims script replaced and its row of the projection manifest re-bound, each in one rename."""
    atomic_write_bytes(audit / claims_name, new_script.read_bytes())
    rows[0]["sha256"] = sha256(audit / claims_name)
    atomic_write_bytes(audit / PROJECTION, (json.dumps(projection, indent=2, ensure_ascii=False) + "\n").encode())
    fr.validate_projection_claim_binding(audit, claims_name)
    return rows[0]["sha256"]


def rehearse(audit: pathlib.Path, journal: str, paper: str, new_script: pathlib.Path, claims_name: str,
             run_text: str, *, documents: bool, stem: str | None = None, reason: str = CLAIMS_REASON) -> dict:
    """The whole amendment on a full scratch copy of the audit, in a scratch tree with a registry of its own: (the
    documents rebuilt,) the claims script replaced, the note journaled, the receipt created and filed, pkg/ made
    read-only and publish_completed.sh run. Nothing outside the scratch folder is written, and it is removed on every
    exit."""
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="i4r-amend-docs-" if documents else "i4r-amend-rehearsal-"))
    try:
        tree = scratch / "tree"
        (tree / "data").mkdir(parents=True)
        copy = tree / "runs" / journal / paper / "audit"
        copy.parent.mkdir(parents=True)
        shutil.copytree(audit, copy, symlinks=True)
        pdf_rel, docx_rel = bound_documents(copy)
        bound = {pdf_rel, DOCS_MANIFEST} | ({docx_rel} if docx_rel else set())
        report: dict = {}
        if documents:
            manifest_before = json.loads((copy / DOCS_MANIFEST).read_text())
            report["stale_code"] = documents_stale_only(copy)
            before = tree_hashes(copy)
            try:
                built = run_child(["bash", str(HERE / "build_docx.sh"), str(copy), str(stem)], timeout=DOCS_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise SystemExit(f"refused: build_docx.sh ran past {DOCS_TIMEOUT} s on the scratch copy") from None
            if built.returncode != 0:
                raise SystemExit("refused: build_docx.sh failed on the scratch copy:\n" + tail(built))
            changes = tree_changes(before, tree_hashes(copy))
            report["build_changes"] = changes
            new_pdf_rel, new_docx_rel = bound_documents(copy)
            if (new_pdf_rel, new_docx_rel) != (pdf_rel, docx_rel):
                raise SystemExit(f"refused: the rebuild binds {new_pdf_rel}, {new_docx_rel}, not {pdf_rel}, "
                                 f"{docx_rel}")
            outside = [p for kind in changes.values() for p in kind if not in_build_footprint(p, bound)]
            if outside:
                raise SystemExit(f"refused: the build changed files outside its own: {', '.join(outside[:8])}")
            try:
                quietly(fr.artifact_paths, copy, json.loads((copy / "run.json").read_text()))
                report["artifact_paths"] = "pass"
            except ValueError as exc:
                raise SystemExit(f"refused: fr.artifact_paths still fails after the rebuild: {exc}") from None
            manifest = json.loads((copy / DOCS_MANIFEST).read_text())
            report.update({"pages_before": manifest_before.get("pages"), "pages_after": manifest.get("pages"),
                           "release_eligible": manifest.get("release_eligible") is True,
                           "renderers_before": {k: v.get("identity") for k, v in
                                                (manifest_before.get("renderers") or {}).items()},
                           "renderers_after": {k: v.get("identity") for k, v in
                                               (manifest.get("renderers") or {}).items()}})
            report["text"] = compare_documents(audit / pdf_rel, copy / pdf_rel,
                                               audit / docx_rel if docx_rel else None,
                                               copy / docx_rel if docx_rel else None)
        # the rest of the amendment, exactly as --apply does it in the audit
        old_docs = {rel: sha256(audit / rel) for rel in bound}
        new_docs = {rel: sha256(copy / rel) for rel in bound}
        projection = json.loads((copy / PROJECTION).read_text())
        rows = [r for r in projection["artifacts"] if r.get("path") == claims_name]
        if len(rows) != 1:
            raise SystemExit("refused: the projection manifest does not bind the claims script exactly once")
        new_sha = rebind_claims_script(copy, claims_name, new_script, projection, rows)
        if documents:
            journal_claims_note(copy, *note_fields(claims_name, sha256(audit / claims_name), new_sha, run_text,
                                                   pdf_rel, docx_rel, old_docs, new_docs, "amendments/(rehearsal)",
                                                   "rehearsal"))
        else:
            journal_plain_note(copy, claims_name, sha256(audit / claims_name), new_sha, run_text,
                               new_docs[DOCS_MANIFEST], "amendments/(rehearsal)", reason)
        try:
            quietly(fr.create, copy, tree / "registry")
            quietly(fr.check, copy, require_registry=True, require_publication=True)
        except ValueError as exc:
            raise SystemExit(f"refused: the receipt created again on the scratch copy does not check: {exc}") from None
        report["pkg_writable"] = make_pkg_read_only(copy)   # as --apply does it before publishing
        try:
            published = run_child(["bash", str(HERE / "publish_completed.sh"), str(copy)], timeout=publish_timeout(),
                                  env=check_env())
        except subprocess.TimeoutExpired:
            raise SystemExit(f"refused: publish_completed.sh ran past {publish_timeout()} s in the rehearsal") from None
        if published.returncode != 0:
            raise SystemExit("refused: publish_completed.sh refused the amended scratch copy:\n" + tail(published))
        if documents:
            report["rehearsal"] = ("receipt created and filed, publication check passed, publish_completed.sh passed "
                                   "(privacy, check 3c, recount) in a scratch tree")
        else:
            filed = tree / "comments" / journal / paper
            names = sorted(p.name for p in filed.iterdir() if p.name != ".DS_Store") if filed.is_dir() else []
            expected = sorted(pathlib.PurePosixPath(rel).name for rel in (pdf_rel, docx_rel) if rel)
            if names != expected or any(sha256(filed / pathlib.PurePosixPath(rel).name) != old_docs[rel]
                                        for rel in (pdf_rel, docx_rel) if rel):
                raise SystemExit("refused: the rehearsal's comments/ is not the published comment byte for byte "
                                 f"({', '.join(names) or 'nothing'})")
            report["rehearsal"] = ("claims script replaced, note journaled, receipt created and filed, publication "
                                   "check passed, publish_completed.sh passed (privacy, check 3c, recount) in a "
                                   "scratch tree, and comments/ came out byte for byte the same")
        return report
    finally:
        remove_tree(scratch)


def print_pkg(report: dict) -> None:
    if report.get("pkg_writable"):
        print(f"  pkg/: {report['pkg_writable']} entries have write permission (the synced folder strips intake's "
              "read-only mode) and the archive gate refuses that, so --apply makes them read-only again (chmod a-w, "
              "no byte changes) before publishing")


def print_rehearsal(report: dict, pdf_rel: str, docx_rel: str | None) -> None:
    print(f"  documents: {pdf_rel}{', ' + docx_rel if docx_rel else ''} were built by older document scripts "
          f"({', '.join(report['stale_code'])}); nothing else about them is stale")
    print("  rebuilt with build_docx.sh on a full scratch copy; files the build changed or created:")
    for kind in ("changed", "created", "removed"):
        names = report["build_changes"][kind]
        print(f"    {kind}: {', '.join(names) if names else '(none)'}")
    before, after = report.get("pages_before") or {}, report.get("pages_after") or {}
    print(f"  fr.artifact_paths after the rebuild: {report['artifact_paths']}")
    print(f"  new manifest: {after.get('body_pages')} body / {after.get('total_pages')} total pages "
          f"(cap {after.get('body_page_cap')}; were {before.get('body_pages')} / {before.get('total_pages')}), "
          f"release eligible: {'yes' if report['release_eligible'] else 'NO'}")
    changed = {k: f"{report['renderers_before'].get(k)} -> {v}" for k, v in report["renderers_after"].items()
               if report["renderers_before"].get(k) != v}
    if changed:
        print("  renderers: " + "; ".join(f"{v}" for v in changed.values()))
    print_text(report["text"])
    print_pkg(report)
    print(f"  rehearsal: {report['rehearsal']}")


# --- after the note: the publication and the record ------------------------------------------------------------------
# A run can be killed at any point of the republication (SIGKILL, a power cut), and nothing but the disk and the journal
# then says where it stood. So before the first folder moves, the plan names the archive folders the previous set goes
# to, and the publish_completed.sh run is recorded while it runs; a resumed --apply reads both, and reads the published
# set's state from the folders themselves and from publish_completed.sh's delivery in the journal.
KINDS = ("comments", "metadata")   # archived in this order, moved back in the reverse one


def published_current(audit: pathlib.Path, meta: pathlib.Path, comments: pathlib.Path, claims_name: str,
                      pdf_rel: str, docx_rel: str | None) -> bool:
    """The published set is the audit's current one: its claims script, receipt, document manifest, PDF and DOCX."""
    pairs = [(meta / claims_name, audit / claims_name), (meta / fr.RECEIPT, audit / fr.RECEIPT),
             (meta / DOCS_MANIFEST, audit / DOCS_MANIFEST),
             (comments / pathlib.PurePosixPath(pdf_rel).name, audit / pdf_rel)]
    if docx_rel:
        pairs.append((comments / pathlib.PurePosixPath(docx_rel).name, audit / docx_rel))
    return all(a.is_file() and b.is_file() and sha256(a) == sha256(b) for a, b in pairs)


def glob_escape(text: str) -> str:
    return re.sub(r"([*?\[\]])", r"[\1]", text)


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def save_plan(record_dir: pathlib.Path, plan: dict) -> None:
    """The plan rewritten in one rename."""
    atomic_write_json(record_dir / (CLAIMS_PLAN if plan.get("mode") == "metadata_only" else DOCS_PLAN), plan)


def archive_targets(root: pathlib.Path, journal: str, paper: str, stamp: str) -> dict[str, pathlib.Path]:
    """Where the previous comments/ and metadata/ go: old_runs/archived_<date>_claims_amendment/<kind>/<journal>/
    <paper>, or <paper>__<amendment time> when an earlier amendment that day took that name."""
    base = root / "old_runs" / f"archived_{stamp[:10]}_claims_amendment"
    targets = {}
    for kind in KINDS:
        target = base / kind / journal / paper
        targets[kind] = target.with_name(f"{paper}__{stamp}") if os.path.lexists(target) else target
    return targets


def delivery_after(audit: pathlib.Path, note_index: int, release_sha: str) -> int | None:
    """The journal index of publish_completed.sh's delivery of the set whose release manifest hashes to release_sha,
    after the amendment's note. publish_completed.sh journals it once every gate has passed on the filed set: a set it
    made visible without one was never accepted (the run was cut off after its rename, or the journal refused it)."""
    for index, event in enumerate(journal_events(audit)):
        if (index > note_index and event.get("type") == "delivery"
                and (event.get("payload") or {}).get("artifact_sha256") == release_sha):
            return index
    return None


def publish_running(group: object, audit: pathlib.Path) -> bool:
    """The publish_completed.sh an earlier run started (process group `group`) still runs. A group none of whose
    processes names this tool's scripts or the audit has a reused id and does not count; when ps cannot tell, it runs."""
    if group in own_ids() or not group_alive(group):
        return False
    done = subprocess.run(["ps", "-A", "-o", "pgid=,command="], capture_output=True, text=True)
    members = [row[1] if len(row) > 1 else "" for row in (line.split(None, 1) for line in done.stdout.splitlines())
               if row and row[0] == str(group)]
    if done.returncode != 0 or not members:
        return True
    needles = {str(HERE), str(HERE.resolve()), str(audit), os.path.realpath(audit)}
    return any(needle in command for command in members for needle in needles)


def publish_leftovers(audit: pathlib.Path, paths: dict[str, pathlib.Path], publication: dict) -> list[pathlib.Path]:
    """What a publish_completed.sh run cut off by SIGKILL leaves and its own clean-up would have removed: its transaction
    folders beside comments/ and metadata/ (.<paper>.publish.*, .<paper>.rollback.*), its lock (which holds its pid)
    and the temporary notebook generate_finding_artifacts.py --check writes into work/. Only what that run made
    counts: entries made since it started, a lock naming its pid."""
    since = float(publication.get("publish_started") or 0) - 2   # file systems that keep times to 2 s
    group = publication.get("publish_group")
    paper = paths["metadata"].name
    transaction = re.compile(re.escape(f".{paper}.") + r"(publish|rollback)\.[A-Za-z0-9]+")
    found: list[pathlib.Path] = []
    for parent in sorted({paths["comments"].parent, paths["metadata"].parent}):
        with contextlib.suppress(OSError):
            found += [entry for entry in parent.iterdir() if transaction.fullmatch(entry.name) and entry.is_dir()
                      and not entry.is_symlink() and entry.stat().st_mtime >= since]
    lock = paths["metadata"].with_name(paths["metadata"].name + ".publish.lock")   # publish_completed.sh's LOCK
    with contextlib.suppress(OSError, ValueError, IndexError):
        if lock.is_file() and not lock.is_symlink() and int(lock.read_text().split()[0]) == group:
            found.append(lock)
    with contextlib.suppress(OSError):
        found += [entry for entry in (audit / "work").glob(".verification-regenerated.*.Rmd")
                  if entry.is_file() and not entry.is_symlink() and entry.stat().st_mtime >= since]
    return sorted(set(found))


def published_as(plan: dict, kind: str, folder: pathlib.Path) -> str:
    """What a published folder holds, by the plan's hashes: 'previous' (as published before the amendment), 'amended',
    'either' (the plain amendment's comments/, whose PDF and DOCX are the same before and after) or 'other'."""
    claims, documents = plan["claims_script"], plan["documents"]
    try:
        if kind == "metadata":
            digest = sha256(folder / claims["path"])
            return {claims["from_sha256"]: "previous", claims["to_sha256"]: "amended"}.get(digest, "other")
        rows = [documents["pdf"]] + ([documents["docx"]] if documents.get("docx") else [])
        names = [pathlib.PurePosixPath(row["path"]).name for row in rows]
        entries = [p for p in folder.iterdir() if p.name != ".DS_Store"]
        if sorted(p.name for p in entries) != sorted(names) or any(p.is_symlink() or not p.is_file() for p in entries):
            return "other"
        found = {p.name: sha256(p) for p in entries}
        if documents.get("unchanged"):
            return "either" if all(found[name] == row["sha256"] for name, row in zip(names, rows)) else "other"
        for which, key in (("previous", "from_sha256"), ("amended", "to_sha256")):
            if all(found[name] == row.get(key) for name, row in zip(names, rows)):
                return which
        return "other"
    except (OSError, KeyError, TypeError):
        return "other"


def publication_state(audit: pathlib.Path, root: pathlib.Path, journal: str, paper: str, plan: dict,
                      note_index: int) -> dict:
    """Where this amendment's republication stands, from the disk and the journal: per folder, whether something is
    published there and whether the plan's archive folder holds the previous set; whether the published set is the
    amended audit's, filed and accepted by publish_completed.sh (its delivery after the note); whether a
    publish_completed.sh an earlier run started still runs, and what one that was stopped left. A folder in a state
    no --apply leaves is a conflict."""
    publication = plan.get("publication") or {}
    paths = dict(zip(KINDS, published_dirs(root, journal, paper)))
    targets = {kind: root / rel for kind, rel in (publication.get("archive") or {}).items() if kind in KINDS}
    state: dict = {"paths": paths, "running": None, "done": False, "delivery": None, "aside": [], "archive": [],
                   "conflicts": [], "lines": [], "leftovers": []}
    group = publication.get("publish_group")
    if group is not None:
        if publish_running(group, audit):
            state["running"] = group
            return state
        state["leftovers"] = publish_leftovers(audit, paths, publication)
    here = {kind: os.path.lexists(paths[kind]) for kind in KINDS}
    archived = {kind: kind in targets and os.path.lexists(targets[kind]) for kind in KINDS}
    where = {kind: shown(targets[kind], root) if kind in targets else None for kind in KINDS}
    if all(here.values()):
        docx = plan["documents"].get("docx") or {}
        differ, other = published_problems(audit, root, paths["comments"], paths["metadata"],
                                           plan["claims_script"]["path"], plan["documents"]["pdf"]["path"],
                                           docx.get("path"))
        if not differ and not other:
            state["delivery"] = delivery_after(audit, note_index, sha256(paths["metadata"] / "release-manifest.json"))
            state["done"] = state["delivery"] is not None
    if state["done"]:
        state["lines"].append(f"the amended set is published in {shown(paths['comments'], root)} and "
                              f"{shown(paths['metadata'], root)}, filed by publish_completed.sh (its delivery is "
                              f"event {state['delivery'] + 1})")
        state["lines"] += [f"the previous {kind}/ set is in {where[kind]}" if archived[kind] else
                           f"the previous {kind}/ set is not in {where[kind] or 'old_runs/'}" for kind in KINDS]
        return state
    for kind in KINDS:
        path = shown(paths[kind], root)
        if here[kind]:
            held = published_as(plan, kind, paths[kind])
            if archived[kind] and held in ("amended", "either"):
                state["aside"].append(kind)
                state["lines"].append(f"{path}: the amended set, made visible by publish_completed.sh, which was "
                                      "stopped before it accepted and journaled it (--apply moves it into the record "
                                      f"folder and publishes again); the previous set is in {where[kind]}")
            elif archived[kind]:
                state["conflicts"].append(f"{path} is published again, not with the amended audit, and the previous "
                                          f"set is already in {where[kind]}")
            elif held in ("previous", "either"):
                state["archive"].append(kind)
                state["lines"].append(f"{path}: the previous set is still published there (--apply archives it"
                                      + (f" to {where[kind]})" if where[kind] else ")"))
            else:
                state["conflicts"].append(f"{path} holds neither the publication from before the amendment nor the "
                                          "amended one")
        elif archived[kind]:
            state["lines"].append(f"{path}: nothing is published there; the previous set is in {where[kind]}")
        else:
            state["conflicts"].append(f"nothing is published at {path}, and the previous set is not in "
                                      f"{where[kind] or 'old_runs/'} either")
    return state


def print_publication(state: dict, root: pathlib.Path) -> None:
    for line in state["lines"]:
        print(f"  {line}")
    if state["leftovers"]:
        print("  left by the publish_completed.sh run that was stopped (its clean-up never ran; --apply removes it): "
              + ", ".join(shown(path, root) for path in state["leftovers"]))


def remove_leftovers(leftovers: list[pathlib.Path], root: pathlib.Path) -> list[str]:
    removed = []
    for path in leftovers:
        if path.is_dir() and not path.is_symlink():
            remove_tree(path)
        else:
            with contextlib.suppress(OSError):
                path.unlink()
        if os.path.lexists(path):
            print(f"  could not remove {shown(path, root)}; remove it by hand")
        else:
            removed.append(shown(path, root))
    if removed:
        print("  removed what the stopped publish_completed.sh left: " + ", ".join(removed))
    return removed


def move_back(root: pathlib.Path, paths: dict[str, pathlib.Path], targets: dict[str, pathlib.Path]) -> list[str]:
    """Every previous folder this amendment archived (in this run or an earlier one) moved back to where it was
    published, and the archive folders that leaves empty removed. A signal waits until both are back: cut in two, the
    paper was left with one folder published. What could not be moved back is returned, with where the previous set
    is."""
    with SIGNALS.held():
        return moved_back(root, paths, targets)


def moved_back(root: pathlib.Path, paths: dict[str, pathlib.Path], targets: dict[str, pathlib.Path]) -> list[str]:
    problems = []
    archive = root / "old_runs"
    for kind in reversed(KINDS):
        source, target = paths[kind], targets.get(kind)
        if target is None or not os.path.lexists(target):
            continue
        if os.path.lexists(source):
            problems.append(f"{shown(source, root)} exists again (look at it); the previous one is in "
                            f"{shown(target, root)}")
            continue
        try:
            fs_retry.replace_with_retry(target, source)
        except OSError as exc:
            problems.append(f"could not move {shown(target, root)} back to {shown(source, root)} ({exc}): the "
                            f"previous set is in {shown(target, root)}, and nothing is published at "
                            f"{shown(source, root)} until --apply runs again")
            continue
        folder = target.parent
        while folder != archive and folder.is_relative_to(archive):
            try:
                folder.rmdir()
            except OSError:
                break
            folder = folder.parent
    return problems


def republish(audit: pathlib.Path, root: pathlib.Path, record_dir: pathlib.Path, plan: dict,
              state: dict) -> subprocess.CompletedProcess:
    """A set publish_completed.sh made visible but never accepted moves into the record folder, the previous
    comments/ and metadata/ move to the archive folders the plan names, then publish_completed.sh files the amended
    audit as it files any audit and journals its delivery. A refused or interrupted publication moves the previous set
    back. Nothing is ever deleted: a folder that cannot be moved back is named with where the previous set is."""
    publication = plan["publication"]
    paths = state["paths"]
    targets = {kind: root / rel for kind, rel in publication["archive"].items()}

    def put_back() -> list[str]:
        return move_back(root, paths, targets)

    try:
        if state["aside"]:
            aside = record_dir / "unaccepted_publication" / utc_stamp()
            aside.mkdir(parents=True, exist_ok=True)
            for kind in state["aside"]:
                fs_retry.replace_with_retry(paths[kind], aside / kind)
        for kind in state["archive"]:
            targets[kind].parent.mkdir(parents=True, exist_ok=True)
            fs_retry.replace_with_retry(paths[kind], targets[kind])
    except BaseException as exc:
        SIGNALS.stopping()
        problems = put_back()
        if isinstance(exc, OSError):
            raise SystemExit(f"could not archive the published folders ({exc}); nothing was republished"
                             + "".join(f"\n  {p}" for p in problems)) from None
        print("interrupted while archiving the published folders; " + ("both are in place" if not problems
              else "".join(f"\n  {p}" for p in problems)), file=sys.stderr)
        raise

    def started(process: subprocess.Popen) -> None:   # a resumed --apply waits for it, then removes what it left
        publication.update({"publish_group": process.pid, "publish_started": time.time()})
        save_plan(record_dir, plan)

    try:
        done = run_child(["bash", str(HERE / "publish_completed.sh"), str(audit)], timeout=publish_timeout(),
                         env=check_env(), on_start=started)
    except subprocess.TimeoutExpired:
        done = subprocess.CompletedProcess([], 1, "", f"publish_completed.sh ran past {publish_timeout()} s")
    except BaseException as exc:   # run_child has ended the publication's whole process group
        SIGNALS.stopping()
        publication["publish_group"] = None
        with contextlib.suppress(OSError):
            save_plan(record_dir, plan)
        problems = put_back()
        back = ("the previous comments/ and metadata/ folders are back in place" if not problems
                else "".join(f"\n  {p}" for p in problems))
        if isinstance(exc, OSError):
            raise SystemExit(f"publish_completed.sh could not be run ({exc}); {back}") from None
        print(f"interrupted during publish_completed.sh; {back}", file=sys.stderr)
        raise
    publication["publish_group"] = None
    if done.returncode == 0:
        publication["publish"] = [f"published: {shown(pathlib.Path(line[len('published: '):]), root)}"
                                  for line in done.stdout.splitlines() if line.startswith("published: ")]
    with contextlib.suppress(OSError):
        save_plan(record_dir, plan)
    if done.returncode != 0:
        SIGNALS.stopping()
        problems = put_back()
        if problems:
            raise SystemExit("publish_completed.sh refused the amended audit, and the previous folders could not all "
                             "be moved back (the audit itself stays amended):" + "".join(f"\n  {p}" for p in problems)
                             + "\nFix the cause and run --apply again: it resumes with the publication.\n" + tail(done))
        raise SystemExit("publish_completed.sh refused the amended audit; the previous comments/ and metadata/ "
                         "folders are back in place (the audit itself stays amended). Fix the cause and run --apply "
                         "again: it resumes with the publication.\n" + tail(done))
    return done


def scrubbed(value: object, root: pathlib.Path) -> object:
    """A record's value with no local absolute path: a path inside the tree becomes tree-relative, one under the home
    folder starts with ~."""
    if isinstance(value, dict):
        return {key: scrubbed(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [scrubbed(item, root) for item in value]
    if isinstance(value, str):
        for prefix in sorted({str(root), str(root.resolve())}, key=len, reverse=True):
            value = value.replace(prefix + "/", "").replace(prefix, ".")
        home = str(pathlib.Path.home())
        return value.replace(home, "~") if len(home) > 1 else value
    return value


def write_record(record_dir: pathlib.Path, plan: dict, published: dict, stamp: str, root: pathlib.Path) -> None:
    documents = plan.get("mode", "rebuild_documents") == "rebuild_documents"
    record = {"schema_version": "1.0.0", "kind": "claims_script_amendment",
              "mode": "rebuild_documents" if documents else "metadata_only",
              "audit_id": plan.get("audit_id"), "amended_at": plan.get("amended_at", stamp),
              "builder": f"i4r-audit {version()}", "reason": DOCS_REASON if documents else plan.get("reason")}
    if documents:
        record["claims_reason"] = plan.get("claims_reason")
    record.update({"claims_script": plan.get("claims_script"), "standalone_run": plan.get("standalone_run"),
                   "publication_check_before": plan.get("publication_check_before"),
                   "registry": plan.get("registry"), "documents": plan.get("documents")})
    if documents:
        record.update({"text_comparison": plan.get("text_comparison"),
                       "accepted_rendering_changes": plan.get("accepted_rendering_changes")})
    record["backed_up"] = plan.get("backed_up")
    record.update(published)
    atomic_write_json(record_dir / RECORD, scrubbed(record, root))


def comment_changes(root: pathlib.Path, comments: pathlib.Path, plan: dict, previous: str | None) -> list[str]:
    """Files whose bytes differ between the republished comments/ and the comment published before the amendment:
    the PDF and DOCX the plan recorded then, and the archived folder byte for byte (without --rebuild-documents)."""
    expected = {pathlib.PurePosixPath(row["path"]).name: row["sha256"]
                for row in (plan["documents"]["pdf"], plan["documents"].get("docx")) if row}
    now = tree_hashes(comments)
    differ = {name for name in expected.keys() | now.keys() if expected.get(name) != now.get(name)}
    if previous is not None:
        old = tree_hashes(root / previous)
        differ |= {name for name in old.keys() | now.keys() if old.get(name) != now.get(name)}
    return sorted(differ)


def record_publication(root: pathlib.Path, record_dir: pathlib.Path, plan: dict, paths: dict[str, pathlib.Path],
                       delivery: int, stamp: str) -> int:
    """AMENDMENT.json, once publish_completed.sh's delivery names the republished set. The previous sets are the
    plan's archive folders, whichever run moved them there."""
    documents = plan.get("mode", "rebuild_documents") == "rebuild_documents"
    publication = plan.get("publication") or {}
    targets = {kind: root / rel for kind, rel in (publication.get("archive") or {}).items() if kind in KINDS}
    previous = {kind: shown(targets[kind], root) if kind in targets and os.path.lexists(targets[kind]) else None
                for kind in KINDS}
    published = {"comments": shown(paths["comments"], root), "metadata": shown(paths["metadata"], root),
                 "previous_comments": previous["comments"], "previous_metadata": previous["metadata"],
                 "release_manifest_sha256": sha256(paths["metadata"] / "release-manifest.json"),
                 "delivery_event": delivery + 1, "publish": publication.get("publish"),
                 "pkg_made_read_only": publication.get("pkg_made_read_only")}
    aside = record_dir / "unaccepted_publication"
    if aside.is_dir():
        published["unaccepted_publications"] = sorted(shown(p, root) for p in aside.glob("*/*") if p.is_dir())
    if publication.get("removed_leftovers"):
        published["removed_leftovers"] = publication["removed_leftovers"]
    status = 0
    if not documents:
        differ = comment_changes(root, paths["comments"], plan, previous["comments"])
        published["comments_unchanged"] = not differ
        if differ:
            print(f"ERROR: the republished comments/ is not byte for byte the comment published before the amendment "
                  f"({', '.join(differ)}); the previous one is in {previous['comments']}")
            status = 1
    write_record(record_dir, plan, published, stamp, root)
    print(f"  amended and republished: {published['comments']} and {published['metadata']} (previous sets in "
          f"{previous['comments']} and {previous['metadata']})"
          + ("; comments/ is byte for byte the same" if not documents and not status else ""))
    for kind in KINDS:
        if previous[kind] is None:
            print(f"  WARNING: the previous {kind}/ set is no longer in "
                  f"{shown(targets[kind], root) if kind in targets else 'old_runs/'}, so AMENDMENT.json names none")
    return status


def finish(audit: pathlib.Path, root: pathlib.Path, journal: str, paper: str, record_dir: pathlib.Path, plan: dict,
           stamp: str, *, apply: bool = True, resuming: bool = False) -> int:
    """After the commit point: the receipt checks for publication, then the paper is republished and recorded. An
    interruption from here on leaves the amendment journaled, and --apply finishes it."""
    try:
        return publish_and_record(audit, root, journal, paper, record_dir, plan, stamp, apply=apply,
                                  resuming=resuming)
    except Interrupted:
        print("stopped after the amendment was journaled: run --apply again to finish (it resumes)", file=sys.stderr)
        raise


def publish_and_record(audit: pathlib.Path, root: pathlib.Path, journal: str, paper: str, record_dir: pathlib.Path,
                       plan: dict, stamp: str, *, apply: bool, resuming: bool) -> int:
    try:
        quietly(fr.check, audit, require_registry=True, require_publication=True)
    except ValueError as exc:
        print(f"stopped: the receipt created again does not pass the publication check: {reason_only(str(exc))}\n"
              "  the amendment is journaled; fix the cause and run --apply again (it resumes)")
        return 1
    note = note_for_record(audit, record_dir.relative_to(audit).as_posix())
    if note is None:
        print("stopped: no journaled note names this amendment's record folder")
        return 1
    state = publication_state(audit, root, journal, paper, plan, note[0])
    if state["running"] is not None:
        group = state["running"]
        print(f"refused: publish_completed.sh, which an earlier --apply of this amendment started, is still running "
              f"(process group {group}); wait for it to end, or end it with: kill -TERM -- -{group}")
        return 1
    if resuming or state["conflicts"] or state["aside"] or state["leftovers"]:
        print_publication(state, root)
    if state["conflicts"]:
        print("refused: the published folders are not as an interrupted --apply leaves them: "
              + "; ".join(state["conflicts"]) + ". Look at them (a teammate's sync, a move by hand), put the previous "
              "set back where it was published, then run --apply again")
        return 1
    if state["done"] and (record_dir / RECORD).is_file():
        print("  already amended and republished: nothing to do")
        return 0
    if not apply:
        print("  dry run: republished, but AMENDMENT.json is missing; --apply writes it" if state["done"] else
              "  dry run: the publication is not the amended one yet; --apply republishes it")
        return 0
    publication = plan.setdefault("publication", {})
    if publication.get("publish_group") is not None:   # that publish_completed.sh is over: what it left goes
        publication["removed_leftovers"] = (publication.get("removed_leftovers") or []) + remove_leftovers(
            state["leftovers"], root)
        publication["publish_group"] = None
    if state["done"]:
        with contextlib.suppress(OSError):
            save_plan(record_dir, plan)
        return record_publication(root, record_dir, plan, state["paths"], state["delivery"], stamp)
    try:
        made_read_only = make_pkg_read_only(audit)
    except OSError as exc:
        print(f"stopped: pkg/ could not be made read-only for the archive gate ({exc}); the amendment is "
              "journaled: fix it and run --apply again (it resumes)")
        return 1
    if made_read_only:
        print(f"  pkg/: {made_read_only} entries made read-only again for the archive gate (chmod a-w)")
    publication["pkg_made_read_only"] = int(publication.get("pkg_made_read_only") or 0) + made_read_only
    if not publication.get("archive"):
        targets = archive_targets(root, journal, paper, stamp)
        taken = [shown(target, root) for target in targets.values() if os.path.lexists(target)]
        if taken:
            print(f"stopped: {' and '.join(taken)} already exists; the amendment is journaled: move it out of the way "
                  "and run --apply again (it resumes)")
            return 1
        publication["archive"] = {kind: shown(target, root) for kind, target in targets.items()}
    try:
        save_plan(record_dir, plan)   # where the previous set goes is on disk before anything moves
    except OSError as exc:
        print(f"stopped: the plan could not be written ({exc}); nothing was moved. Fix it and run --apply again "
              "(it resumes)")
        return 1
    try:
        done = republish(audit, root, record_dir, plan, state)
    except SystemExit as exc:
        print(f"stopped: {exc}")
        return 1
    delivery = delivery_after(audit, note[0], sha256(state["paths"]["metadata"] / "release-manifest.json"))
    if delivery is None:
        print("stopped: publish_completed.sh filed the paper, but its delivery is not in the journal, so the filing "
              "was never accepted (its output is below). The amendment is journaled: fix the cause and run --apply "
              "again; it moves this filing into the record folder and publishes again.\n" + tail(done))
        return 1
    return record_publication(root, record_dir, plan, state["paths"], delivery, stamp)


def resume(args: argparse.Namespace, audit: pathlib.Path, amendment: dict) -> int:
    """A journaled amendment is unfinished: finish what is left of it (receipt, filing, publication, record)."""
    index, payload = amendment["index"], amendment["payload"]
    record_rel, record_dir, documents = amendment["record_rel"], amendment["record_dir"], amendment["documents"]
    root, journal, paper = layout(audit)
    plan_name, plan = plan_of(record_dir) if record_dir.is_dir() else (None, {})
    wanted_plan = DOCS_PLAN if documents else CLAIMS_PLAN
    print(f"{display_name(audit)}:")
    if documents:
        print(f"  a --rebuild-documents amendment is journaled (event {index + 1}, {record_rel})")
    else:
        print(f"  an amendment of the claims script is journaled (event {index + 1}, {record_rel})")
    expected = note_hashes(payload)
    drifted = [rel for rel, digest in expected.items()
               if not isinstance(rel, str) or not (audit / rel).is_file() or sha256(audit / rel) != digest]
    if drifted or not plan or plan_name != wanted_plan:
        print("refused: the audit's documents are not the ones the amendment journaled, or its plan is missing: "
              + ", ".join(map(str, drifted or [wanted_plan])))
        return 1
    if projections_behind(audit):
        print("  the journal's projections (run.json and the rest) are behind its last event: a journal write was cut "
              "off")
        if not args.apply:
            print("  dry run: --apply rebuilds them (lifecycle_journal.py recover), then finishes it (receipt, "
                  "registry, publication)")
            return 0
        try:
            lifecycle_journal.recover(audit)
        except Exception as exc:
            print(f"refused: they could not be rebuilt (lifecycle_journal.py recover): "
                  f"{without_audit_path(str(exc), audit)[-300:]}. Fix the cause and run --apply again (it resumes)")
            return 1
        print("  rebuilt them from the journal (lifecycle_journal.py recover)")
    if args.apply and not journaled_plan(plan):
        mark_journaled(record_dir, plan)   # a run stopped at its note may have left it unmarked
    if not receipt_binds(audit, expected):
        try:
            quietly(fr.check, audit, require_registry=True, require_publication=False)
            why = "it binds other files than the amendment's"
        except ValueError as exc:
            why = str(exc)
        print(f"  the receipt was not created again after it ({reason_only(why)[:160]})")
        if not args.apply:
            print("  dry run: --apply finishes it (receipt, registry, publication)")
            return 0
        try:
            receipt = json.loads((audit / fr.RECEIPT).read_text())
            registry_free(registry_root(audit, receipt))
            with SIGNALS.held():   # the registry filing is not cut in two
                quietly(fr.create, audit, registry_root(audit, receipt))
        except (ValueError, OSError) as exc:
            print(f"stopped: the receipt could not be created again: {exc}")
            return 1
    else:
        try:
            quietly(fr.check, audit, require_registry=True, require_publication=False)
        except ValueError as exc:
            print(f"refused: the receipt binds the amendment's files but does not check: {exc}")
            return 1
    stamp = plan.get("amended_at") or utc_stamp()
    return finish(audit, root, journal, paper, record_dir, plan, stamp, apply=args.apply, resuming=True)


def finished_and_current(audit: pathlib.Path, amendment: dict | None, root: pathlib.Path, journal: str, paper: str,
                         documents: bool | None = None) -> bool:
    """The latest journaled amendment (of this mode, unless None) is finished, and the audit and its publication
    hold what it installed."""
    if not amendment or not amendment["finished"] or (documents is not None and amendment["documents"] != documents):
        return False
    payload = amendment["payload"]
    claims = audit / str(payload.get("path"))
    if not claims.is_file() or sha256(claims) != payload.get("to_sha256"):
        return False
    comments, meta = published_dirs(root, journal, paper)
    pdf_rel, docx_rel = bound_documents(audit)
    return published_current(audit, meta, comments, claims.name, pdf_rel, docx_rel)


# --- the plain amendment ---------------------------------------------------------------------------------------------
def amend_claims(args: argparse.Namespace, audit: pathlib.Path, old_script: pathlib.Path, receipt: dict,
                 registry: pathlib.Path, publication_note: str | None) -> int:
    """Without --rebuild-documents: only the claims script and its projection row change."""
    name = display_name(audit)
    try:   # everything the receipt binds must still be bindable on this release, before anything is changed
        quietly(fr.artifact_paths, audit, json.loads((audit / "run.json").read_text()))
    except ValueError as exc:
        text = str(exc)
        if DOCS_STALE_CODE in text:
            print(f"refused: the comment's PDF and DOCX were built by older document scripts "
                  f"({text.split(' (the skill')[0]}), so the finalization receipt cannot be created again on this "
                  "release and the claims script cannot be re-bound. Amend this audit with --rebuild-documents, which "
                  "rebuilds them on a full scratch copy first, compares the comment's text and republishes both "
                  "folders (INSTALL.md, 'Comments built by older document scripts'). Do not rebuild a published "
                  "audit's documents by hand: its receipt and publication would no longer match them.")
            return 1
        print(f"refused: the finalization receipt cannot be created again on this release, so the claims script "
              f"cannot be re-bound: {reason_only(text)}")
        return 1
    root, journal, paper = layout(audit)
    pdf_rel, docx_rel = bound_documents(audit)
    claims_name = old_script.name
    old_sha = sha256(old_script)
    with tempfile.TemporaryDirectory(prefix="i4r-amend-") as tmp:
        new_script = build(audit, pathlib.Path(tmp), old_script.suffix[1:])
        missing = snippets_inlined(audit, new_script.read_text())
        if missing:
            print(f"refused: the rebuilt script does not carry these snippets verbatim: {', '.join(missing)}")
            return 1
        ok, run_text = run_check(audit, new_script)
        new_sha = sha256(new_script)
        print(f"{name}:")
        print(f"  claims script {old_sha[:12]} -> {new_sha[:12]} ({'unchanged' if old_sha == new_sha else 'rebuilt'})")
        print(f"  standalone run: {run_text.splitlines()[0] if run_text else '(no output)'}")
        print(f"  publication check before the amendment: "
              f"{'pass' if publication_note is None else 'FAILS: ' + reason_only(publication_note)[:160]}")
        if not ok:
            print_check_failure(run_text)
            return 1
        if old_sha == new_sha:
            comments, meta = published_dirs(root, journal, paper)
            if not published_current(audit, meta, comments, claims_name, pdf_rel, docx_rel):
                print(f"refused: the claims script is already this release's, but the published set at "
                      f"{shown(comments, root)} and {shown(meta, root)} is not the audit's; resolve that first")
                return 1
            if finished_and_current(audit, journaled_amendment(audit), root, journal, paper):
                print("  already amended and republished: nothing to do")
            else:
                print("  the claims script is already this release's and is the published one: nothing to do")
            return 0
        if not preflight(audit, root, journal, paper, claims_name, pdf_rel, docx_rel, False):
            return 1
        report = rehearse(audit, journal, paper, new_script, claims_name, run_text, documents=False,
                          reason=args.reason)
        print_pkg(report)
        print(f"  rehearsal: {report['rehearsal']}")
        if not args.apply:
            print("  dry run: nothing changed (--apply to replace the claims script and republish comments/ and "
                  "metadata/)")
            return 0
        if sha256(old_script) != old_sha:
            print("refused: the claims script changed while this run built and rehearsed (another run amended the "
                  "audit meanwhile); nothing was changed. Run this again.")
            return 1
        return apply_claims(args, audit, old_script, new_script, receipt, registry, publication_note, run_text,
                            root, journal, paper, pdf_rel, docx_rel)


def apply_claims(args: argparse.Namespace, audit: pathlib.Path, old_script: pathlib.Path, new_script: pathlib.Path,
                 receipt: dict, registry: pathlib.Path, publication_note: str | None, run_text: str,
                 root: pathlib.Path, journal: str, paper: str, pdf_rel: str, docx_rel: str | None) -> int:
    claims_name, old_sha, new_sha = old_script.name, sha256(old_script), sha256(new_script)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    record_dir = audit / "amendments" / f"{stamp}-claims_script"
    record_rel = record_dir.relative_to(audit).as_posix()
    projection = json.loads((audit / PROJECTION).read_text())
    rows = [r for r in projection["artifacts"] if r.get("path") == claims_name]
    if len(rows) != 1 or rows[0].get("sha256") != old_sha:
        print("refused: the projection manifest does not bind the old claims script exactly")
        return 1
    if not preflight(audit, root, journal, paper, claims_name, pdf_rel, docx_rel, False):   # again, after the rehearsal
        return 1
    documents = {"unchanged": True,
                 "pdf": {"path": pdf_rel, "sha256": sha256(audit / pdf_rel)},
                 "docx": {"path": docx_rel, "sha256": sha256(audit / docx_rel)} if docx_rel else None,
                 "document_manifest": {"path": DOCS_MANIFEST, "sha256": sha256(audit / DOCS_MANIFEST)}}
    backups = [claims_name, PROJECTION, fr.RECEIPT]
    plan = {"mode": "metadata_only", "audit_id": receipt.get("audit_id"), "amended_at": stamp, "reason": args.reason,
            "claims_script": {"path": claims_name, "from_sha256": old_sha, "to_sha256": new_sha},
            "standalone_run": run_text.splitlines()[0] if run_text else None,
            "publication_check_before": reason_only(publication_note) if publication_note else "pass",
            "registry": shown(registry, root) if registry.is_relative_to(root) else "registry",
            "documents": documents, "backed_up": backups,
            "amendments_existed": (audit / "amendments").exists(), "runner": runner()}
    journaled = noting = False
    try:
        begin_record(audit, record_dir, CLAIMS_PLAN, plan, backups)
        try:
            rebind_claims_script(audit, claims_name, new_script, projection, rows)
            quietly(fr.artifact_paths, audit, json.loads((audit / "run.json").read_text()))
        except ValueError as exc:
            raise SystemExit(f"the claims script could not be re-bound: {exc}") from None
        noting = True   # from here the note may land
        journal_plain_note(audit, claims_name, old_sha, new_sha, run_text, documents["document_manifest"]["sha256"],
                           record_rel, args.reason)
        journaled = True
        mark_journaled(record_dir, plan)
        # the commit point: the receipt is created again and the audit re-filed where it was filed
        try:
            registry_free(registry)
            with SIGNALS.held():   # the registry filing is not cut in two
                quietly(fr.create, audit, registry)
        except (ValueError, OSError) as exc:
            raise SystemExit(f"the receipt could not be created again: {exc}") from None
    except BaseException:
        SIGNALS.stopping()
        if not journaled and noting and stopped_unknown(audit, record_rel):
            raise
        if not journaled and noting and note_for_record(audit, record_rel) is not None:
            journaled = True   # log_event reported a failure, or a signal came, after the note landed
            mark_journaled(record_dir, plan)
        if not journaled:   # nothing is journaled or filed yet: put the audit back exactly as it was
            report_undo(undo(audit, record_dir, plan, None), record_rel)
        else:
            print("stopped after the amendment was journaled: run --apply again to finish (it resumes)",
                  file=sys.stderr)
        raise
    return finish(audit, root, journal, paper, record_dir, plan, stamp)


# --- --rebuild-documents ---------------------------------------------------------------------------------------------
def rebuild_documents(args: argparse.Namespace, audit: pathlib.Path, old_script: pathlib.Path, receipt: dict,
                      registry: pathlib.Path, publication_note: str | None) -> int:
    """--rebuild-documents for an audit that has no unfinished documents amendment."""
    name = display_name(audit)
    try:   # as the plain amendment: everything the receipt binds must be bindable, but for the documents' code identity
        quietly(fr.artifact_paths, audit, json.loads((audit / "run.json").read_text()))
    except ValueError as exc:
        if DOCS_STALE_CODE not in str(exc):
            print(f"refused: the finalization receipt cannot be created again on this release, so the claims script "
                  f"cannot be re-bound: {reason_only(str(exc))}")
            return 1
    else:
        root, journal, paper = layout(audit)
        amendment = journaled_amendment(audit)
        if finished_and_current(audit, amendment, root, journal, paper, documents=True):
            print(f"{name}:")
            print(f"  a --rebuild-documents amendment is journaled and finished (event {amendment['index'] + 1}, "
                  f"{amendment['record_rel']})")
            print("  already amended and republished: nothing to do")
            return 0
        print("refused: the comment's documents are current on this release; amend the claims script without "
              "--rebuild-documents")
        return 1
    for tool in ("pdftotext", "pdfinfo"):
        if shutil.which(tool) is None:
            print(f"refused: {tool} is not on PATH (Poppler; e.g. PATH=\"$HOME/miniconda3/envs/i4rdoc/bin:$PATH\")")
            return 1
    root, journal, paper = layout(audit)
    pdf_rel, docx_rel = bound_documents(audit)
    stem = pathlib.PurePosixPath(pdf_rel).stem
    claims_name = old_script.name
    old_sha = sha256(old_script)
    with tempfile.TemporaryDirectory(prefix="i4r-amend-") as tmp:
        new_script = build(audit, pathlib.Path(tmp), old_script.suffix[1:])
        missing = snippets_inlined(audit, new_script.read_text())
        if missing:
            print(f"refused: the rebuilt script does not carry these snippets verbatim: {', '.join(missing)}")
            return 1
        ok, run_text = run_check(audit, new_script)
        new_sha = sha256(new_script)
        print(f"{name}:")
        print(f"  claims script {old_sha[:12]} -> {new_sha[:12]} ({'unchanged' if old_sha == new_sha else 'rebuilt'})")
        print(f"  standalone run: {run_text.splitlines()[0] if run_text else '(no output)'}")
        print(f"  publication check before the amendment: FAILS: {reason_only(publication_note or '')[:160]}")
        if not ok:
            print_check_failure(run_text)
            return 1
        if old_sha == new_sha:
            print("refused: the claims script is already this release's; there is nothing to re-bind")
            return 1
        if not preflight(audit, root, journal, paper, claims_name, pdf_rel, docx_rel, True):
            return 1
        report = rehearse(audit, journal, paper, new_script, claims_name, run_text, documents=True, stem=stem)
        print_rehearsal(report, pdf_rel, docx_rel)
        if not args.apply:
            print("  dry run: nothing changed (--apply to rebuild, re-bind and republish"
                  + ("" if report["text"]["same"] else "; the text differs, so only with --accept-rendering-changes")
                  + ")")
            return 0
        if not report["text"]["same"] and not args.accept_rendering_changes:
            print("refused: the rebuilt comment's text differs from the published one (above). Apply only a "
                  "rendering difference, never one in words or numbers, with --accept-rendering-changes")
            return 1
        if sha256(old_script) != old_sha:
            print("refused: the claims script changed while this run built and rehearsed (another run amended the "
                  "audit meanwhile); nothing was changed. Run this again.")
            return 1
        return apply_documents(args, audit, old_script, receipt, registry, publication_note, new_script, run_text,
                               report, root, journal, paper, pdf_rel, docx_rel, stem)


def apply_documents(args: argparse.Namespace, audit: pathlib.Path, old_script: pathlib.Path, receipt: dict,
                    registry: pathlib.Path, publication_note: str | None, new_script: pathlib.Path, run_text: str,
                    report: dict, root: pathlib.Path, journal: str, paper: str, pdf_rel: str, docx_rel: str | None,
                    stem: str) -> int:
    claims_name, old_sha, new_sha = old_script.name, sha256(old_script), sha256(new_script)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    record_dir = audit / "amendments" / f"{stamp}-claims_script"
    record_rel = record_dir.relative_to(audit).as_posix()
    projection = json.loads((audit / PROJECTION).read_text())
    rows = [r for r in projection["artifacts"] if r.get("path") == claims_name]
    if len(rows) != 1 or rows[0].get("sha256") != old_sha:
        print("refused: the projection manifest does not bind the old claims script exactly")
        return 1
    bound = {pdf_rel, DOCS_MANIFEST} | ({docx_rel} if docx_rel else set())
    if not preflight(audit, root, journal, paper, claims_name, pdf_rel, docx_rel, True):   # again, after the rehearsal
        return 1
    predicted = report["build_changes"]
    footprint_files, footprint_dirs = footprint_state(audit)
    # every file the build may change or remove, including those its dry run left alone (a failing build writes
    # document_build_diagnostics/last_failure.json): the rollback restores each
    backups = sorted({claims_name, PROJECTION, fr.RECEIPT} | bound | set(predicted["changed"])
                     | set(predicted["removed"]) | set(footprint_files))
    backups = [rel for rel in backups if os.path.lexists(audit / rel)]
    old_docs = {rel: sha256(audit / rel) for rel in bound}
    plan = {"mode": "rebuild_documents", "audit_id": receipt.get("audit_id"), "amended_at": stamp,
            "claims_reason": args.reason,
            "claims_script": {"path": claims_name, "from_sha256": old_sha, "to_sha256": new_sha},
            "standalone_run": run_text.splitlines()[0] if run_text else None,
            "publication_check_before": reason_only(publication_note) if publication_note else "pass",
            "registry": shown(registry, root) if registry.is_relative_to(root) else "registry",
            "documents": {"output_name": stem, "stale_code": report["stale_code"],
                          "pdf": {"path": pdf_rel, "from_sha256": old_docs[pdf_rel]},
                          "docx": {"path": docx_rel, "from_sha256": old_docs[docx_rel]} if docx_rel else None,
                          "document_manifest": {"path": DOCS_MANIFEST, "from_sha256": old_docs[DOCS_MANIFEST]},
                          "pages_before": report["pages_before"], "pages_after": report["pages_after"],
                          "release_eligible": report["release_eligible"],
                          "renderers_before": report["renderers_before"], "renderers_after": report["renderers_after"],
                          "build_changes_dry_run": predicted},
            "text_comparison": report["text"], "accepted_rendering_changes": not report["text"]["same"],
            "backed_up": backups, "footprint_before": {"files": footprint_files, "dirs": footprint_dirs},
            "amendments_existed": (audit / "amendments").exists(), "runner": runner()}
    before = tree_state(audit)
    journaled = noting = False

    def started(process: subprocess.Popen) -> None:   # --rollback-pending waits for a build its run left behind
        plan["build_group"] = process.pid
        atomic_write_json(record_dir / DOCS_PLAN, plan)

    try:
        begin_record(audit, record_dir, DOCS_PLAN, plan, backups)
        try:
            rebind_claims_script(audit, claims_name, new_script, projection, rows)
        except ValueError as exc:
            raise SystemExit(f"the claims script could not be re-bound: {exc}") from None
        try:
            built = run_child(["bash", str(HERE / "build_docx.sh"), str(audit), stem], timeout=DOCS_TIMEOUT,
                              on_start=started)
        except subprocess.TimeoutExpired:
            raise SystemExit(f"build_docx.sh ran past {DOCS_TIMEOUT} s in the audit") from None
        if built.returncode != 0:
            raise SystemExit("build_docx.sh failed in the audit:\n" + tail(built))
        changes = tree_changes(before[0], tree_hashes(audit))
        changes = {kind: [p for p in paths if p not in (claims_name, PROJECTION) and not p.startswith(record_rel + "/")]
                   for kind, paths in changes.items()}
        outside = [p for kind in changes.values() for p in kind if not in_build_footprint(p, bound)]
        unsaved = [p for p in changes["changed"] + changes["removed"] if p not in backups]
        if outside or unsaved:
            raise SystemExit("the build in the audit changed files the dry run did not: "
                             + ", ".join((outside + unsaved)[:8]))
        if bound_documents(audit) != (pdf_rel, docx_rel):
            raise SystemExit("the rebuild in the audit binds other documents than the dry run's")
        try:
            quietly(fr.artifact_paths, audit, json.loads((audit / "run.json").read_text()))
        except ValueError as exc:
            raise SystemExit(f"fr.artifact_paths fails after the rebuild in the audit: {exc}") from None
        text = compare_documents(record_dir / pdf_rel, audit / pdf_rel, record_dir / docx_rel if docx_rel else None,
                                 audit / docx_rel if docx_rel else None)
        if text_outcome(text) != text_outcome(report["text"]):
            raise SystemExit("the comment's text after the rebuild in the audit is not what the dry run found:\n"
                             + json.dumps(text_outcome(text), ensure_ascii=False)[:1500])
        pages = json.loads((audit / DOCS_MANIFEST).read_text()).get("pages")
        if pages != report["pages_after"]:
            raise SystemExit(f"the rebuilt document's pages {pages} differ from the dry run's {report['pages_after']}")
        new_docs = {rel: sha256(audit / rel) for rel in bound}
        for kind, rel in (("pdf", pdf_rel), ("docx", docx_rel), ("document_manifest", DOCS_MANIFEST)):
            if rel:
                plan["documents"][kind]["to_sha256"] = new_docs[rel]
        plan["documents"]["build_changes"] = changes
        atomic_write_json(record_dir / DOCS_PLAN, plan)
        outcome = "same" if report["text"]["same"] else "accepted:" + ",".join(
            f"{k}={report['text'][k]['result']}" for k in ("pdf", "docx") if k in report["text"])
        noting = True   # from here the note may land
        journal_claims_note(audit, *note_fields(claims_name, old_sha, new_sha, run_text, pdf_rel, docx_rel, old_docs,
                                                new_docs, record_rel, outcome))
        journaled = True
        mark_journaled(record_dir, plan)
        # the commit point: the receipt is created again and the audit re-filed where it was filed
        try:
            registry_free(registry)
            with SIGNALS.held():   # the registry filing is not cut in two
                quietly(fr.create, audit, registry)
        except (ValueError, OSError) as exc:
            raise SystemExit(f"the receipt could not be created again: {exc}") from None
    except BaseException:
        SIGNALS.stopping()
        if not journaled and noting and stopped_unknown(audit, record_rel):
            raise
        if not journaled and noting and note_for_record(audit, record_rel) is not None:
            journaled = True   # log_event reported a failure, or a signal came, after the note landed
            mark_journaled(record_dir, plan)
        if not journaled:   # nothing is journaled or filed yet: put the audit back exactly as it was
            report_undo(undo(audit, record_dir, plan, before), record_rel)
        else:
            print("stopped after the amendment was journaled: run --apply again to finish (it resumes)",
                  file=sys.stderr)
        raise
    return finish(audit, root, journal, paper, record_dir, plan, stamp)


# --- the command -----------------------------------------------------------------------------------------------------
def amend(args: argparse.Namespace, audit: pathlib.Path, old_script: pathlib.Path) -> int:
    pending = pending_records(audit)
    if args.rollback_pending:
        return rollback_pending(audit, pending)
    lost = [path for path in pending if journaled_plan(plan_of(path)[1] or {})]
    if lost:
        print(lost_note(lost[-1].relative_to(audit).as_posix()))
        return 1
    if pending:
        print(f"refused: an amendment of this audit stopped before it was journaled (a run killed or cut off), and "
              f"{pending[-1].relative_to(audit).as_posix()}/ holds what it backed up. Put the audit back first with "
              "--rollback-pending, then run this again.")
        return 1
    amendment = journaled_amendment(audit)
    hint = ""
    if amendment and not amendment["finished"]:
        if amendment["documents"] == args.rebuild_documents:   # finish it (its receipt may be stale)
            return resume(args, audit, amendment)
        hint = (f"an unfinished {'--rebuild-documents ' if amendment['documents'] else ''}amendment is journaled "
                f"({amendment['record_rel']}): finish it with --apply"
                + (" --rebuild-documents" if amendment["documents"] else " (without --rebuild-documents)"))
    try:
        quietly(fr.check, audit, require_registry=True, require_publication=False)
    except ValueError as exc:
        print(f"refused: the finalization receipt does not check before the amendment: {reason_only(str(exc))}"
              + (f"\n  {hint}" if hint else ""))
        return 1
    if hint:
        print(f"refused: {hint}")
        return 1
    publication_note = None
    try:
        quietly(fr.check, audit, require_registry=True, require_publication=True)
    except ValueError as exc:
        publication_note = str(exc)
    receipt = json.loads((audit / fr.RECEIPT).read_text())
    registry = registry_root(audit, receipt)
    if args.rebuild_documents:
        return rebuild_documents(args, audit, old_script, receipt, registry, publication_note)
    return amend_claims(args, audit, old_script, receipt, registry, publication_note)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("audit_dir", type=pathlib.Path)
    ap.add_argument("--apply", action="store_true", help="replace and republish (default: a dry run)")
    ap.add_argument("--reason", default=CLAIMS_REASON)
    ap.add_argument("--rebuild-documents", action="store_true",
                    help="first rebuild the comment's PDF and DOCX with this release's build_docx.sh: only for an audit "
                         "whose document manifest older document scripts wrote, and nothing else is stale")
    ap.add_argument("--accept-rendering-changes", action="store_true",
                    help="with --rebuild-documents --apply: go ahead although the rebuilt comment's text differs (a "
                         "rendering difference only, never words or numbers; recorded in AMENDMENT.json)")
    ap.add_argument("--rollback-pending", action="store_true",
                    help="put the audit back from the record folder of an amendment stopped before its note (a run "
                         "killed with SIGKILL, or cut off by a power cut); every other run refuses until then")
    args = ap.parse_args(argv)
    if args.accept_rendering_changes and not args.rebuild_documents:
        ap.error("--accept-rendering-changes goes with --rebuild-documents")
    if args.rollback_pending and (args.apply or args.rebuild_documents):
        ap.error("--rollback-pending goes alone: it only puts the audit back")
    audit = args.audit_dir.absolute()
    if not (audit / fr.RECEIPT).is_file() or not (audit / "run.json").is_file():
        print(f"{audit}: not a finalized audit (no {fr.RECEIPT})", file=sys.stderr)
        return 2
    claims = [p for p in audit.glob("claims_script.*") if p.is_file() and not p.is_symlink()]
    if len(claims) != 1:
        print(f"{audit}: expected exactly one claims_script.*", file=sys.stderr)
        return 1
    lock = hold(audit)
    if lock is None:
        print(f"{display_name(audit)}: refused: another amend_claims_script.py run holds this audit (one run per audit "
              "at a time; find it with: pgrep -fl amend_claims_script); nothing was done")
        return 1
    try:
        SIGNALS.install()
        return amend(args, audit, claims[0])
    except Interrupted as exc:
        print(f"stopped by {exc}", file=sys.stderr)
        return 128 + exc.signum
    finally:
        SIGNALS.restore()
        os.close(lock)


if __name__ == "__main__":
    sys.exit(main())
