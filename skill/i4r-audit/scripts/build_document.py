#!/usr/bin/env python3
"""build_document.py [build_docx.sh options] <audit-dir> <output-basename> [final-report.md]   (2.7.11)

Runs scripts/build_docx.sh unchanged, with exactly these arguments, prints what it prints, and keeps it when the build
fails. build_docx.sh is frozen (every document manifest binds its bytes). For a failure before its validation stage --
a missing input or tool, a table without a caption, pandoc, LibreOffice, promotion -- it writes
document_build_diagnostics/last_failure.json with the stage and only "document build failed before promotion": the
reason went to the terminal that ran it and nowhere else, and LibreOffice's own messages go nowhere at all. 42 of the
73 failure records in the tree on 2026-11-01 carried nothing more.

When build_docx.sh fails and writes a new last_failure.json, this wrapper writes what it printed (its last 400 lines)
to document_build_diagnostics/last_failure_output.txt beside it, with the audit folder shown as $AUDIT_DIR and the
home folder as ~, and prints the stage with the lines that say why. A failure that wrote no new record (a usage
error, a missing audit folder) is only printed: a kept output must never sit beside an older failure's record.

The exit status is build_docx.sh's (128 + N when a signal N it does not trap ended it), also when the terminal or a
pipe reading this wrapper's output closes early (it stops echoing and keeps draining). INT, TERM and HUP are passed
on once to build_docx.sh's whole process group, as a terminal would deliver them, and the wrapper waits for its
cleanup. A build that succeeds writes nothing. Nothing else is read or written. amend_claims_script.py and other
callers still run build_docx.sh directly and are unaffected.
"""
from __future__ import annotations

import collections
import json
import os
import pathlib
import signal
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
BUILDER = HERE / "build_docx.sh"
KEEP_LINES = 400
OUTPUT_NAME = "last_failure_output.txt"
RECORD_NAME = "last_failure.json"
GENERIC = "document build failed before promotion"
REASON_MARKERS = ("FATAL", "Error", "error:", "ERROR", "Traceback", "refused", "not found")
FORWARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def audit_dir(argv: list[str]) -> pathlib.Path | None:
    """The audit folder among build_docx.sh's arguments, read as build_docx.sh reads them."""
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in ("--no-cache", "--refresh-cache"):
            index += 1
        elif argument == "--preview-max-pages":
            index += 2
        elif argument.startswith("-"):
            return None          # build_docx.sh prints its usage or refuses the option itself
        else:
            return pathlib.Path(argument)
    return None


def signature(path: pathlib.Path) -> tuple[int, int, int, bytes] | None:
    """Enough of a file to tell whether this build replaced it (build_docx.sh moves or writes a new one)."""
    try:
        status = path.stat()
        return (status.st_ino, status.st_mtime_ns, status.st_size, path.read_bytes())
    except OSError:
        return None


def masker(folder: pathlib.Path | None):
    """Replace the audit folder with $AUDIT_DIR and the home folder with ~ (longest first), as given and resolved."""
    candidates: list[tuple[str, str]] = []
    if folder is not None:
        candidates.append((str(folder.absolute()), "$AUDIT_DIR"))
    home = os.environ.get("HOME") or ""
    if home:
        candidates.append((home, "~"))
    pairs: list[tuple[str, str]] = []
    for path, label in candidates:
        for form in {path.rstrip("/"), os.path.realpath(path).rstrip("/")}:
            if len(form) > 1:
                pairs.append((form, label))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    def mask(line: str) -> str:
        for prefix, label in pairs:
            line = line.replace(prefix, label)
        return line
    return mask


def main(argv: list[str]) -> int:
    folder = audit_dir(argv)
    diagnostics = folder / "document_build_diagnostics" if folder is not None else None
    record_before = signature(diagnostics / RECORD_NAME) if diagnostics is not None else None
    kept: collections.deque[str] = collections.deque(maxlen=KEEP_LINES)
    # Its own session, so that a terminal's INT or HUP reaches build_docx.sh once (passed on from here), never twice:
    # a second INT arriving after its trap reset would end it before it records the failure and removes its
    # transaction folder.
    process = subprocess.Popen(["bash", str(BUILDER), *argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               encoding="utf-8", errors="replace", start_new_session=True)
    assert process.stdout is not None

    def forward(signum: int, _frame: object) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signum)
            except OSError:
                pass
    # A signal this wrapper was started ignoring stays ignored, as it would for build_docx.sh run directly: a
    # background job ignores INT, and nohup ignores HUP, so the build keeps running through them.
    previous = {signum: signal.signal(signum, forward) for signum in FORWARDED
                if signal.getsignal(signum) != signal.SIG_IGN}
    echo = True
    try:
        for line in process.stdout:
            if echo:
                try:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                except (OSError, ValueError):
                    # A closed pipe (| head) or a hung-up terminal (EIO): stop echoing, and point fd 1 at /dev/null
                    # so the interpreter's final flush cannot fail and replace the builder's status with 120. Keep
                    # reading so the build is never blocked.
                    echo = False
                    try:
                        null = os.open(os.devnull, os.O_WRONLY)
                        os.dup2(null, sys.stdout.fileno())
                        os.close(null)
                    except (OSError, ValueError):
                        pass
            kept.append(line.rstrip("\n"))
        status = process.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if status < 0:                       # ended by a signal it does not trap
        status = 128 - status
    if status == 0:
        return 0
    stage = message = ""
    fresh = (diagnostics is not None and (diagnostics / RECORD_NAME).is_file()
             and signature(diagnostics / RECORD_NAME) != record_before)
    if fresh:
        try:
            record = json.loads((diagnostics / RECORD_NAME).read_text(encoding="utf-8"))
            stage, message = str(record.get("stage") or ""), str(record.get("message") or "")
        except (OSError, ValueError, AttributeError):
            pass
    reasons = [line for line in kept if any(marker in line for marker in REASON_MARKERS)][-10:]
    print(f"build_docx.sh failed (exit {status})" + (f" at stage {stage}" if stage else "") + ":", file=sys.stderr)
    for line in reasons or list(kept)[-5:]:
        print(f"  {line}", file=sys.stderr)
    if stage == "libreoffice":
        print("  (build_docx.sh discards LibreOffice's own messages; check that soffice converts a DOCX on this host)",
              file=sys.stderr)
    if fresh:
        mask = masker(folder)
        try:
            (diagnostics / OUTPUT_NAME).write_text("".join(mask(line) + "\n" for line in kept), encoding="utf-8")
            print(f"  what it printed is kept in {diagnostics.name}/{OUTPUT_NAME}"
                  + (" (last_failure.json records the stage only)" if message == GENERIC else ""), file=sys.stderr)
        except OSError as exc:
            print(f"  (could not keep the build output: {exc.__class__.__name__})", file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
