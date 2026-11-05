#!/usr/bin/env python3
"""Read-only batch status across every audit in a discipline tree.

Nothing else in this skill takes a tree root. `registry/index.jsonl` lists finished audits only,
`reservations/` records who claimed a paper but never progress, and `lifecycle_journal.py recover`
is per-audit and takes a write lock. The failure signature of a stalled audit is an audit directory
that simply stops being written to, and at three papers an operator notices that from memory. At
146 they cannot: four audits in this tree sat half-finished for between five and twelve days before
anyone walked it by hand.

This finds audit directories by their marker files -- run.json, or a phase1/ directory -- rather
than by a fixed <journal>/<paper>/audit path, because a paper whose title contains a glob
metacharacter has its workspace relocated with I4R_PAPER_DIR and does not sit under its own paper
folder at all. Three such workspaces under .work/audits, idle five days, were invisible to the
first version of this. It opens nothing for writing, holds no lock (it tests a run.lock with a
non-blocking shared flock it drops at once; for a stopped critique round it asks `ps` whether the
driver pid the round recorded still runs), and starts no provider call, so it is safe to run
against a live batch.

    python3 scripts/status.py <tree-root>              # one line per audit
    python3 scripts/status.py <tree-root> --stuck      # only audits that need attention
    python3 scripts/status.py <tree-root> --json       # machine-readable
    python3 scripts/status.py <tree-root> --idle-days 2
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shlex
import signal
import sys
import unicodedata

# Trees that never hold a live audit. `.work` is NOT here: relocated workspaces live under
# .work/audits, and skipping it is what hid three of them.
SKIP_DIRS = {
    "skill", "comments", "registry", "reservations", "planning", "outputs", "portal_backups",
    "methodology_paper", ".claude", "Completed reports", "Examples of completed reports",
    # 2.5.0 layout: inputs, published records and archived runs never hold a live audit
    "data", "metadata", "old_runs", "PII Checker Results", "_survey_processing_ref",
}
# Never descend into these: they are an audit's own contents, some of them very large.
AUDIT_INTERNALS = {
    "pkg", "paper", "phase1", "critique", "work", "report", "release", "staging", "snapshots",
    "evidence", "node_modules", ".git",
}
# A sibling kept for the record, not a run anyone is waiting on.
ARCHIVED = re.compile(r"(?:\.failed-|\.archived-|_backup_|\.bak$|\.old$)")
MAX_SCAN_DEPTH = 4
# Anything older than this with an unfinished run is worth a look. Deliberately generous: a Phase-1
# fan-out can legitimately sit quiet for 45 to 90 minutes on a single role's provider call.
DEFAULT_IDLE_DAYS = 1.0
# 2.7.11: the separate single-pass baseline study (its own skill; `run_baseline.py status`) writes a run.json per
# paper into baseline_comments/<Journal>/<Paper>/, and archives of that folder keep them. They are not audits: 2.7.10
# listed every one as "no run.json" (54 rows on the live tree). Recognised by the record, so any archive name is too.
BASELINE_RUN_SCHEMA = "i4r-baseline-run-"
# 2.7.11: a stopped Phase 1 (run_phase1.py's status.json) waits for someone at once; the idle rule hid it for a day.
PHASE1_REMEDY = {
    "local_preflight_failed": "nothing reached a provider; clear the cause in phase1/status.json `error` (INSTALL, "
                              "'When the Phase-1 preflight pauses') and re-run run_phase1.py, which is free until it "
                              "dispatches",
    "capability_probe_failed": "clear the cause in phase1/status.json `error`, then re-run run_phase1.py",
    "sandbox_environment_fault": "a role hit a sandbox fault (phase1/status.json `environment_faults`); fix the host, "
                                 "then re-run run_phase1.py (completed roles are reused)",
    "snapshot_integrity_fault": "do what phase1/status.json `remedy` says, then re-run run_phase1.py",
    "snapshot_mode_unstable": "do what phase1/status.json `remedy` says, then re-run run_phase1.py",
}
# 2.7.11: a critique run that dispatched and stopped without its critic quorum journals a run-level pause with this
# reason, bound to its invocation's ROUND_STATUS.json (run_critique.sh); the next run of the round ends it
CRITIQUE_STOP_REASON = "critique_round_failed"
# the other journaled pauses, and what ends them
PAUSE_REMEDY = {
    "awaiting_authorization": "an attended round waits for its grant: review the plan the driver printed, "
                              "journal it with lifecycle_journal.py authorize-round <audit dir> N <template>, then "
                              "re-run the same run_critique.sh command",
}
# a lock directory's own pid file: Dropbox copies it when the lock is made and removed within one sync, and the round
# driver treats a lock without a readable pid as dead, so the copy changes nothing
LOCK_DIRS = re.compile(r"^\.runtime_lock(?:\.stale\..*)?$")


def _json(path: pathlib.Path, strict: bool = False):
    try:
        text = path.read_text(encoding="utf-8")
        return _strict_loads(text) if strict else json.loads(text)
    except (OSError, ValueError):
        return None


def _strict_loads(text: str):
    """JSON as `finalization_receipt.py check` reads registry files: json.loads takes a repeated key that it refuses."""
    if "validate_json_schema" not in sys.modules:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import validate_json_schema
    return validate_json_schema.strict_loads(text, source="registry file")


def _is_baseline_run(directory: pathlib.Path) -> bool:
    record = _json(directory / "run.json")
    return isinstance(record, dict) and str(record.get("schema_version") or "").startswith(BASELINE_RUN_SCHEMA)


def _short(text, limit: int = 160) -> str:
    """One line of operator text for the board, with any home-folder account name and e-mail address masked."""
    value = re.sub(r"(/(?:Users|home)/)[^/\s]+", r"\1<user>", " ".join(str(text).split()))
    value = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "<e-mail>", value)
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _pid_runs(value, needle: str) -> bool:
    """A pid recorded in a file is a live process on THIS host whose command line names `needle` (the OS reuses
    pids, and a pid written on another host means nothing here)."""
    try:
        pid = int(str(value).strip().splitlines()[0])
    except (ValueError, IndexError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)   # signal 0: an existence test, nothing is sent
    except PermissionError:
        pass
    except OSError:
        return False
    import subprocess
    try:
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return True   # cannot tell: a live pid counts, as the round driver's own lock check does
    return needle in command


def _rerun_command(audit: pathlib.Path, round_number: int) -> str:
    """The command that resumes a critique round with the settings it was started with, as wait_for_round.sh's
    rerun hint builds it: the CRITICS override of the newest invocation that dispatched (round 2+) and the round's
    diff manifest. Paths stay as <audit dir> placeholders."""
    base = audit / f"critique/round_{round_number}/invocations"
    roles = ""
    try:
        names = sorted((p.name for p in base.iterdir() if p.is_dir()), reverse=True)
    except OSError:
        names = []
    for name in names:
        attempts = base / name / "attempts"
        try:
            dispatched = attempts.is_dir() and any(attempts.iterdir())
        except OSError:
            dispatched = False
        if not dispatched:
            continue
        routing = _json(base / name / "ROUTING.json")
        if isinstance(routing, dict) and "CRITICS override" in str(routing.get("reason", "")) \
                and isinstance(routing.get("roles"), list):
            roles = " ".join(str(role) for role in routing["roles"])
        break
    parts = []
    if roles and round_number >= 2:
        parts.append("CRITICS=" + shlex.quote(roles))
    if (audit / f"critique/DIFF_MANIFEST_round_{round_number}.json").is_file():
        parts.append(f"DIFF_MANIFEST=<audit dir>/critique/DIFF_MANIFEST_round_{round_number}.json")
    parts.append(f"AUDIT_DIR=<audit dir> bash scripts/run_critique.sh {round_number}")
    return " ".join(parts) + " (with the same I4R_AUTO_AUTHORIZE and ADJUDICATE values)"


def _age_days(path: pathlib.Path) -> float | None:
    try:
        return (dt.datetime.now().timestamp() - path.stat().st_mtime) / 86400.0
    except OSError:
        return None


def _nfc(value: str) -> str:
    # NFC and case-folded: reserve_paper treats both spellings (APFS ignores case and Unicode form) as one paper
    return unicodedata.normalize("NFC", value).casefold()


def _slugs(journal: str, paper: str) -> list[str]:
    """reserve_paper.sh's reservation file names for journal/paper: the plain name, then the hashed one."""
    import hashlib
    raw = unicodedata.normalize("NFC", f"{journal}__{paper}")
    safe = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9._-]+", "_", raw))[:180]
    hashed = safe[:170] + "-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    if re.fullmatch(r"[A-Za-z0-9._-]*", raw) and len(raw.encode("utf-8")) <= 180:
        return [safe, hashed]
    return [hashed]


def _reservations(tree: pathlib.Path) -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}
    root = tree / "reservations"
    if not root.is_dir():
        return out
    for f in root.glob("*.json"):
        value = _json(f)
        if isinstance(value, dict):
            value["_file"] = f.name
            value["_age_days"] = _age_days(f)
            # `paper` is an absolute path on the CLAIMING machine, so only its last two parts can match here.
            # Keyed by (journal, paper) in NFC, as reserve_paper matches: two journals can hold papers with the same
            # folder name. A claim recorded without its journal ("." typed by an earlier version) keys as (None, paper).
            recorded = pathlib.PurePosixPath(os.path.normpath(str(value.get("paper") or ".")))
            journal = recorded.parent.name if len(recorded.parts) >= 2 else None
            name = recorded.name if recorded.name not in ("", ".", "..") else ""
            if name:
                out[(_nfc(journal) if journal and journal not in (".", "..") else None, _nfc(name))] = value
            # a claim recorded as "." (an earlier version) names nothing: it is found by its file name instead
            out[("\0file", f.stem.casefold())] = value
    return out


def _pointer(path: pathlib.Path) -> tuple[dict | None, str | None]:
    """registry/<id>/current.json as `finalization_receipt.py check` reads it, or why it cannot be used, in
    registry-audit's words (its _pointer_problem; naming no path)."""
    try:
        if path.is_symlink():
            return None, "is a symlink"
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return None, f"cannot be read ({getattr(exc, 'strerror', None) or type(exc).__name__})"
    try:
        pointer = _strict_loads(text)
    except ValueError as exc:
        return None, ("is empty" if not text.strip() else "is not valid JSON (truncated?)"
                      if isinstance(exc.__cause__, json.JSONDecodeError) else
                      "is not strict JSON (a repeated key, or NaN)")
    if not isinstance(pointer, dict):
        return None, "is not a JSON object"
    if not isinstance(pointer.get("version"), str) or not isinstance(pointer.get("pair_manifest_sha256"), str):
        return None, "names no filing (no version or pair_manifest_sha256 string)"
    return pointer, None


def _registry(tree: pathlib.Path) -> dict:
    """Audit ids filed into <tree>/registry, those some index row holds, and the index's conflicted copies.

    A filed audit that no row holds fails `finalization_receipt.py check`, so publish refuses it: before 2.7.9 a host
    whose Dropbox copy lagged rewrote index.jsonl without other hosts' rows (2.7.9 keeps each audit's own row in
    registry/<id>/index_row.json). `finalization_receipt.py registry-audit` says which can be recovered. Also the
    audits whose own row file is stale (an earlier filing's: a host on 2.7.8 or older re-filed the audit, or a sync
    conflict kept another filing's copy) or cannot be read or parsed; `registry-recover` replaces either. A row, in
    index.jsonl or index_row.json, holds an audit only while it is the audit's current filing (its current.json), as
    registry-audit counts: `check` refuses a superseded row as surely as none. A filed audit whose current.json cannot
    be used is counted as that alone, as registry-audit lists it: no row can be said to hold it, and registry-recover
    refuses it."""
    out = {"filed": set(), "rowed": set(), "conflicted_copies": 0, "stale_rows": set(), "broken_rows": set(),
           "unusable_pointers": {}}
    pointers: dict = {}
    root = tree / "registry"
    try:
        children = list(root.iterdir()) if root.is_dir() else []
    except OSError:
        children = []
    for child in children:
        try:
            pointer, current = None, child / "current.json"
            if current.is_file():
                out["filed"].add(child.name)
                pointer, problem = _pointer(current)
                pointers[child.name] = pointer
                if problem is not None:
                    out["unusable_pointers"][child.name] = problem
                    continue
            own = child / "index_row.json"
            if own.exists() or own.is_symlink():        # a directory too: check and registry-audit refuse it
                row = None if own.is_symlink() or not own.is_file() else _json(own, strict=True)
                if not isinstance(row, dict) or row.get("audit_id") != child.name:
                    out["broken_rows"].add(child.name)
                elif isinstance(pointer, dict) and (row.get("version"), row.get("pair_manifest_sha256")) != (
                        pointer.get("version"), pointer.get("pair_manifest_sha256")):
                    out["stale_rows"].add(child.name)   # an earlier filing's: index.jsonl may still hold the current
                    continue
                out["rowed"].add(child.name)            # a broken one too: registry-audit lists it as BROKEN instead
        except OSError:
            continue
    index = root / "index.jsonl"
    try:    # never opened unless a regular file (a FIFO blocks the sweep); registry-audit refuses any other
        lines = index.read_text(encoding="utf-8").splitlines() if index.is_file() and not index.is_symlink() else []
    except (OSError, ValueError):
        lines = []
    for line in lines:
        try:
            value = json.loads(line) if line.strip() else None
        except ValueError:
            continue
        audit_id = value.get("audit_id") if isinstance(value, dict) else None
        pointer = pointers.get(audit_id) if isinstance(audit_id, str) else None
        if isinstance(pointer, dict) and (value.get("version"), value.get("pair_manifest_sha256")) == (
                pointer.get("version"), pointer.get("pair_manifest_sha256")):
            out["rowed"].add(audit_id)
    out["conflicted_copies"] = sum(1 for _ in root.glob("index (*conflicted copy*"))
    return out


def find_audits(tree: pathlib.Path) -> list[pathlib.Path]:
    """Every audit directory under the tree, found by marker rather than by fixed depth."""
    found: list[pathlib.Path] = []

    def walk(directory: pathlib.Path, depth: int) -> None:
        if depth > MAX_SCAN_DEPTH:
            return
        try:
            children = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            return
        for child in children:
            if child.name in SKIP_DIRS or child.name in AUDIT_INTERNALS:
                continue
            # the baseline study's record of one paper is not an audit, nor is anything below it (it keeps no journal,
            # so an audit's run.json is never parsed twice)
            if (child / "run.json").is_file() and not (child / "run.events.jsonl").exists() and _is_baseline_run(child):
                continue
            # an intake that stopped before init_run.sh (it refused) has no run.json yet, but holds a reservation
            if (child / "run.json").is_file() or (child / "phase1").is_dir() or (
                    (child / "package_manifest.json").is_file() and (child / "pkg").is_dir()):
                found.append(child)
                continue  # an audit's own subtree holds no further audits
            walk(child, depth + 1)

    walk(tree, 0)
    return found


def _later(first, second) -> bool:
    """True when timestamp `first` is strictly after `second` (RFC 3339, any offset); False if either is unreadable."""
    def instant(value):
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo else None
    a, b = instant(first), instant(second)
    return bool(a and b and a > b)


def _lock_is_held(path: pathlib.Path) -> bool:
    import fcntl
    try:
        with open(path, "rb") as handle:   # read-only is enough to take a shared flock
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return True   # unreadable: report it rather than hide it


def inspect(audit: pathlib.Path, reservations: dict[tuple, dict], registry: dict | None = None) -> dict:
    paper_dir = audit.parent
    loaded = _json(audit / "run.json")
    run = loaded if isinstance(loaded, dict) else {}
    # A conventional workspace is <journal>/<paper>/audit, so the paper is the parent. A
    # relocated one (I4R_PAPER_DIR) is named for its paper and its parent is just a holding
    # directory, which is why these used to all print as the paper "audits".
    relocated = not audit.name.startswith("audit")
    row = {
        "journal": audit.parent.name if relocated else paper_dir.parent.name,
        "paper": audit.name if relocated else paper_dir.name,
        "relocated": relocated,
        "audit_dir": str(audit),
        "archived": bool(ARCHIVED.search(audit.name) or ARCHIVED.search(paper_dir.name)),
        "audit_id": run.get("audit_id"),
        "schema_version": run.get("schema_version"),
        "status": run.get("status"),
        "stopping_reason": run.get("stopping_reason"),
        "rounds_run": run.get("rounds_run"),
        "findings": None,
        "phase1": None,
        "last_round": None,
        "last_round_outcome": None,
        "idle_days": _age_days(audit / "run.events.jsonl"),
        "locks_held": [],
        "conflicted_copies": 0,
        "reservation_age_days": None,
        "reservation_file": None,
        "attention": [],
    }
    # 2.7.11: "no run.json" only when there is none; a file still syncing or damaged reads as unreadable
    row["run_json"] = ("present" if isinstance(loaded, dict) else "unreadable"
                       if (audit / "run.json").exists() or (audit / "run.json").is_symlink() else "missing")
    row["pause_reason"] = run.get("pause_reason")
    row["phase1_started"] = (audit / "phase1").is_dir()
    # the run record's projected events (text redactions applied): a stop the operator explained ends on a note
    events = run.get("events") if isinstance(run.get("events"), list) else []
    last = events[-1] if events and isinstance(events[-1], dict) else {}
    if last.get("type") == "note" and isinstance(last.get("payload"), dict) and last["payload"].get("text"):
        row["last_note"] = _short(last["payload"]["text"])
    # a critique run that stopped without its quorum: the critique_round_failed pause no pause_end has closed. Any
    # pause_end closes it, whatever its reason, as the run projection (lifecycle_journal) closes every pause.
    critique_pause = None
    for event in events:
        payload = event.get("payload") if isinstance(event, dict) and isinstance(event.get("payload"), dict) else {}
        if isinstance(event, dict) and event.get("type") == "pause_end":
            critique_pause = None
        elif payload.get("reason") == CRITIQUE_STOP_REASON and event.get("type") == "pause_start":
            critique_pause = payload

    findings = audit / "work/findings.jsonl"
    if findings.is_file():
        try:
            row["findings"] = sum(1 for l in findings.read_text().splitlines() if l.strip())
        except OSError:
            pass

    p1 = _json(audit / "phase1/status.json")
    if isinstance(p1, dict):
        row["phase1"] = p1.get("status")
        if p1.get("status") in ("paused", "failed"):
            # its last run's stop, as run_phase1.py wrote it (provider-safe text); a later run that reached quorum
            # leaves quorum.json, so a stray failed re-run after it is not a stop
            failed = p1.get("failed_roles") if isinstance(p1.get("failed_roles"), dict) else {}
            detail = p1.get("remedy") or p1.get("error") or next(iter(failed.values()), None)
            row["phase1_stop"] = {
                "pause_reason": p1.get("pause_reason"),
                "failed_roles": sorted(failed),
                "detail": _short(detail) if detail else None,
                "quorum": (audit / "phase1/quorum.json").is_file(),
            }

    # the rounds the journal knows (a refused prepare leaves a round folder that never became a round); folders only
    # for audits whose run.json has no rounds yet
    # a finished audit's trailing prepared-but-unrun round is not part of it; a live audit shows the round it is
    # waiting on
    finished_run = run.get("status") == "complete"
    rounds = [r.get("round") for r in (run.get("rounds") or []) if isinstance(r, dict) and isinstance(r.get("round"), int)
              and not (finished_run and not r.get("started_at")
                       and r.get("state") in ("prepared", "awaiting_authorization", "ready"))]
    if not rounds:
        for d in (audit / "critique").glob("round_*"):
            m = re.fullmatch(r"round_(\d+)", d.name)
            if m:
                rounds.append(int(m.group(1)))
    if rounds:
        top = max(rounds)
        row["last_round"] = top
        st = _json(audit / f"critique/round_{top}/ROUND_STATUS.json")
        if isinstance(st, dict):
            row["last_round_outcome"] = st.get("outcome")

    # 2.7.11: a critique round whose last run stopped. The journaled pause (a 2.7.11 driver, after dispatch) names the
    # round; without one, the last round's ROUND_STATUS.json says failed (an older driver, or a run refused before it
    # dispatched) while the journal has not integrated that round. Not while a driver of that round runs on this host.
    round_states = {r.get("round"): r.get("state") for r in (run.get("rounds") or []) if isinstance(r, dict)}
    stop = None
    if isinstance(critique_pause, dict) and isinstance(critique_pause.get("round"), int):
        stop = {"round": critique_pause["round"], "journaled": True, "outcome": "failed",
                "failed_roles": [str(r) for r in critique_pause.get("failed_roles") or []],
                "completed_roles": [str(r) for r in critique_pause.get("completed_roles") or []],
                "invocation": critique_pause.get("driver_invocation"), "receipt": critique_pause.get("receipt")}
    elif rounds and row["last_round_outcome"] in ("failed", "quorum_complete_adjudication_failed") \
            and round_states.get(row["last_round"]) in ("prepared", "ready", "running"):
        stop = {"round": row["last_round"], "journaled": False, "outcome": row["last_round_outcome"],
                "failed_roles": [str(r) for r in st.get("failed_roles") or []],
                "completed_roles": [str(r) for r in st.get("completed_roles") or []],
                "invocation": st.get("invocation_id"),
                "detail": _short(st["detail"]) if st.get("detail") else None}
    if stop and run.get("status") not in ("complete", "aborted"):
        out = audit / f"critique/round_{stop['round']}"
        lock_pid = None
        try:
            lock_pid = (out / ".runtime_lock" / "pid").read_text(encoding="utf-8")
        except OSError:
            pass
        current = _json(out / "ROUND_STATUS.json")
        stop["driver_running_here"] = any(_pid_runs(pid, "run_critique") for pid in (
            lock_pid, current.get("pid") if isinstance(current, dict) else None) if pid is not None)
        stop["rerun"] = _rerun_command(audit, stop["round"])
        row["critique_stop"] = stop

    # A live lock on a quiet audit is the signature of a killed process.
    # Phase 1 leaves phase1/run.lock behind (it is an flock target, never deleted), so a lock FILE counts as held
    # only when a process still holds its flock
    for lock in list(audit.rglob("run.lock")) + list(audit.rglob(".runtime_lock")):
        if lock.is_file() and not _lock_is_held(lock):
            continue
        row["locks_held"].append(str(lock.relative_to(audit)))
    copies = list(audit.rglob("*conflicted copy*"))
    lock_copies = [copy for copy in copies if LOCK_DIRS.match(copy.parent.name)]
    row["conflicted_copies"] = len(copies) - len(lock_copies)
    row["lock_conflicted_copies"] = len(lock_copies)   # listed, never flagged
    if registry and row["audit_id"] in registry.get("unusable_pointers", {}):
        row["registry_pointer_unusable"] = registry["unusable_pointers"][row["audit_id"]]
    elif registry and row["audit_id"] in registry["filed"] and row["audit_id"] not in registry["rowed"]:
        row["registry_row_missing"] = True
    if registry and row["audit_id"] in registry.get("stale_rows", ()):
        row["registry_row_stale"] = True
    if registry and row["audit_id"] in registry.get("broken_rows", ()):
        row["registry_row_broken"] = True

    # A relocated workspace's parent is a holding directory, not the paper, so its name matches
    # nothing on the board. paper_source.json carries the true paper name when intake wrote one;
    # without it the honest answer is "unknown", not the holding directory's name.
    # paper_source.json names the paper exactly as it appears in data/, which is also the name the
    # reservation was claimed under. A run folder can differ from it: new_audit.sh drops ?*[]{}\
    # from the run folder name, and a relocated workspace is named for nothing at all.
    lookup = journal = None
    source = _json(audit / "paper_source.json")
    if isinstance(source, dict) and source.get("paper"):
        lookup, journal = source["paper"], source.get("journal")
        # label the row by the paper intake recorded: a run folder can be "<title> (2)" or drop ?*[]{}\ characters,
        # which made one paper's row read as another's title
        if row["paper"] != source["paper"]:
            row["run_folder"] = row["paper"]
            row["paper"] = source["paper"]
        if source.get("journal"):
            row["journal"] = source["journal"]
    elif not relocated:
        lookup, journal = paper_dir.name, paper_dir.parent.name
    res = None
    if lookup:
        res = reservations.get((_nfc(journal), _nfc(lookup))) if journal else None
        res = res or reservations.get((None, _nfc(lookup)))
        if res is None and not journal:
            same = [value for (kind, name), value in reservations.items()
                    if kind != "\0file" and name == _nfc(lookup)]
            res = same[0] if len(same) == 1 else None
        if res is None and journal:
            # by reservation file name, as reserve_paper names it (plain, then hashed)
            for stem in _slugs(journal, lookup):
                res = reservations.get(("\0file", stem.casefold()))
                if res is not None:
                    break
    # publish's own filing event: when, and where (release_dir is relative to the tree root)
    filed_at = filed_dir = None
    reopened = None   # 2.6.9: the journal's own record -- an audit_reopened after the last finalize
    try:
        for line in (audit / "run.events.jsonl").read_text(encoding="utf-8").splitlines():
            event = json.loads(line) if line.strip() else {}
            payload = event.get("payload") or {}
            if event.get("type") == "delivery" and payload.get("release_dir") \
                    and payload.get("recipient") == "comments/ and metadata/ (filed publication set)":
                filed_at, filed_dir = event.get("at") or filed_at, str(payload["release_dir"])
            elif event.get("type") == "finalize":
                reopened = False
            elif event.get("type") == "audit_reopened":
                reopened = payload.get("purpose") or True
    except (OSError, ValueError):
        filed_at = filed_dir = None
    if reopened:
        row["reopened"] = reopened
    if res:
        row["reservation_age_days"] = res.get("_age_days")
        row["reservation_file"] = res.get("_file")
        # a claim made after this audit's publication filing is the correction route's re-claim (INSTALL,
        # "Correcting a published comment"): the audit is reopened, not finished
        row["reclaimed_for_correction"] = _later(res.get("claimed_at"), filed_at)
    # filed, but its published set has since been moved away (correction step 1) and not filed again. An audit
    # published before publish journaled its filing has no event: its folders are named after journal and paper.
    if len(audit.parents) > 3 and audit.parents[2].name == "runs":
        tree_root = audit.parents[3]
        relative = filed_dir.split("/", 1)[-1] if filed_dir else f"{row['journal']}/{row['paper']}"
        comments_dir, metadata_dir = tree_root / "comments" / relative, tree_root / "metadata" / relative
        missing = not comments_dir.is_dir() and not metadata_dir.is_dir()
        if filed_dir:
            row["published_set_missing"] = missing
        elif missing and row["status"] == "complete" and res and _later(res.get("claimed_at"), run.get("ended_at")):
            # no filing event: a complete audit whose paper was claimed again after it finished and that has no
            # published set is reopened (a claim from before it finished is one publish never released)
            row["reclaimed_for_correction"] = True
    return row


def _critique_stop(row: dict, idle: str) -> str | None:
    """A critique round whose last run stopped (journaled critique_round_failed pause, or a failed ROUND_STATUS.json
    on a round the journal has not integrated) and the command that resumes it."""
    stop = row.get("critique_stop") or {}
    if not stop:
        return None
    if stop.get("driver_running_here"):
        return None   # a run of that round is under way on this host; it ends the pause before it dispatches
    number = stop["round"]
    rerun = stop.get("rerun") or f"AUDIT_DIR=<audit dir> bash scripts/run_critique.sh {number}"
    roles = stop.get("failed_roles") or []
    failed = f"{len(roles)} critic(s) failed ({', '.join(roles)})" if roles else "no critic result recorded as failed"
    where = f" (receipt: {stop['receipt']})" if stop.get("receipt") else (
        f" (invocation {stop['invocation']})" if stop.get("invocation") else "")
    if stop.get("journaled"):
        return (f"critique round {number} stopped without its critic quorum, idle {idle}: {failed}; once the cause is "
                f"cleared, re-run the round: {rerun}. Finished critics come from cache and the re-run ends this pause "
                f"before it dispatches{where}")
    if stop.get("outcome") == "quorum_complete_adjudication_failed":
        return (f"critique round {number}: the critics reached quorum but the adjudicator failed, idle {idle}; re-run "
                f"the round to retry the adjudication (the critics' results are reused): {rerun}{where}")
    detail = f": {stop['detail']}" if stop.get("detail") else ""
    return (f"critique round {number}'s last run failed and no driver of it runs on this host, idle {idle}{detail}; "
            f"{failed}. Clear the cause, then re-run the round (finished critics come from cache): {rerun}{where}")


def _stop(row: dict) -> str | None:
    """Why an unfinished audit stopped and what to do, when its record says it stopped: a journaled pause, a critique
    round whose last run stopped, or a Phase 1 whose last run paused or failed. Shown at once, whatever the idle time
    (2.7.10 waited a day). Not while this host runs it again (holds phase1/run.lock, or a live driver of that round).
    A re-run on another host shows as stopped until it starts dispatching: Phase 1 rewrites status.json, and a critique
    driver its ROUND_STATUS.json and the journal, only then."""
    idle = f"{row['idle_days']:.1f}d" if row.get("idle_days") is not None else "?"
    reason = row.get("pause_reason") if row.get("status") == "paused" else None
    if reason == CRITIQUE_STOP_REASON or (not reason and row.get("critique_stop")):
        message = _critique_stop(row, idle)
        if message or row.get("critique_stop"):
            return message
        return (f"paused ({reason}), idle {idle}: a critique run stopped without its quorum; its journal's last "
                "pause_start names the round and its receipt; re-run that round with run_critique.sh")
    stop = row.get("phase1_stop") or {}
    # a Phase 1 stop counts only before quorum and before any critique round: anything later supersedes it
    stop = stop if stop and not stop.get("quorum") and row.get("last_round") is None else {}
    phase1_pause = reason in PHASE1_REMEDY or bool(stop)
    if phase1_pause and "phase1/run.lock" in (row.get("locks_held") or []):
        return None
    if reason in PAUSE_REMEDY:
        return f"paused ({reason}), idle {idle}: {PAUSE_REMEDY[reason]}"
    if reason and reason not in PHASE1_REMEDY:
        return f"paused ({reason}), idle {idle}: its journal's pause event says why; resume the step it names"
    if not reason and not stop:
        if row.get("status") == "paused":
            return f"paused (no reason recorded), idle {idle}: its journal's last events say why"
        return None
    reason = reason or stop.get("pause_reason")
    detail = f" (phase1/status.json: {stop['detail']})" if stop.get("detail") else ""
    if reason in PHASE1_REMEDY:
        return f"Phase 1 paused ({reason}), idle {idle}: {PHASE1_REMEDY[reason]}{detail}"
    if stop.get("failed_roles"):
        roles = stop["failed_roles"]
        return (f"Phase 1 stopped, idle {idle}: {len(roles)} role(s) failed ({', '.join(roles)}); re-run "
                f"run_phase1.py once the cause is cleared (completed roles are reused){detail}")
    return f"Phase 1 stopped with an error, idle {idle}: clear it, then re-run run_phase1.py{detail}"


def classify(row: dict, idle_days: float) -> dict:
    why = row["attention"]
    idle = row["idle_days"]
    # a reopened correction (re-claimed, or its published set moved away) is not finished until filed again
    reopened = bool(row.get("reclaimed_for_correction") or row.get("published_set_missing") or row.get("reopened"))
    finished = row["status"] == "complete" and not reopened

    if row.get("archived"):
        # Kept for the record. Reporting these as stalled work is how a sweep of 146 papers
        # fills with noise nobody can act on.
        return row
    if row.get("unreadable"):
        why.append(f"could not be read: {row['unreadable']}")
        return row
    stopped = None if row["status"] in (None, "aborted") or finished else _stop(row)   # an aborted run resumes nothing
    if row["status"] is None and row.get("run_json") == "unreadable":
        why.append("run.json cannot be read (Dropbox still syncing it, or damaged): let the sync finish; a "
                   "metadata-2.1 audit rebuilds it from its journal with lifecycle_journal.py project <audit dir>")
    elif row["status"] is None and row.get("run_json") == "present":
        why.append("its run.json names no status (not a metadata-2.x audit record)")
    elif row["status"] is None:
        why.append("no run.json: intake started but never initialised. Run the init_run.sh command new_audit.sh "
                   "printed (a refusal names its cause; for a skill version that differs from the tree's skill/, "
                   "reinstall first), or, if it was abandoned, move this folder under old_runs/ and release the "
                   "paper's claim (reserve_paper.sh release data/<Journal>/<Paper>)")
    elif stopped:
        why.append(stopped)
    elif not finished and idle is not None and idle >= idle_days:
        shown_status = ("reopened (journaled: %s)" % row["reopened"] if isinstance(row.get("reopened"), str)
                        else "reopened for correction" if reopened else row["status"])
        message = f"unfinished and idle {idle:.1f}d (status={shown_status})"
        if row.get("phase1_started") is False and not row.get("rounds_run") and not reopened:
            message += ("; initialised but Phase 1 never started: start it with run_phase1.py <audit dir>, or, if "
                        "it was set aside, move the audit folder under old_runs/ (new_audit.sh refuses the paper "
                        "while it sits in runs/)" + (" and release its claim" if row["reservation_age_days"]
                                                       is not None else ""))
        elif row.get("phase1") == "running":
            message += ("; Phase 1 last said running: if no process runs it on any host, recover with "
                        "run_phase1.py <audit dir> --recover-stranded, then re-run it (INSTALL, 'When something dies "
                        "mid-Phase-1')")
        why.append(message)
    if row["status"] == "complete" and row.get("published_set_missing") and not row.get("reclaimed_for_correction"):
        why.append("complete, but its published comments/ and metadata/ folders are gone (a correction started "
                   "without re-claiming the paper?)")
    if row["status"] == "aborted":
        why.append("aborted")
    if row["conflicted_copies"]:
        why.append(f"{row['conflicted_copies']} Dropbox conflicted copies in the audit tree")
    if row.get("registry_pointer_unusable") == "is a symlink":
        why.append("filed into registry/, but its registry/<id>/current.json is a symlink: its receipt check and "
                   "publish follow one that stays inside the registry, but registry-audit and registry-recover refuse "
                   "it; replace the link with a copy of its target")
    elif row.get("registry_pointer_unusable"):
        why.append(f"filed into registry/, but its registry/<id>/current.json {row['registry_pointer_unusable']}, and "
                   "its receipt check, publish, registry-audit and registry-recover all need it: let Dropbox finish "
                   "syncing it, or restore it from the folder's version history")
    if row.get("registry_row_missing"):
        why.append("filed into registry/, but no index row holds its current filing, so its receipt check and publish "
                   "refuse it (finalization_receipt.py registry-audit <tree>/registry)")
    if row.get("registry_row_stale"):
        why.append("its registry/<id>/index_row.json is from an earlier filing (re-filed by a host on 2.7.8 or "
                   "older, or a sync conflict), so its receipt check falls back to index.jsonl; replace it with "
                   "finalization_receipt.py registry-recover <tree>/registry <audit dir>")
    if row.get("registry_row_broken"):
        why.append("its registry/<id>/index_row.json cannot be read or parsed, so its receipt check and publish refuse "
                   "it (finalization_receipt.py registry-recover <tree>/registry <audit dir>)")
    if row["locks_held"] and not finished and idle is not None and idle >= idle_days:
        why.append(f"holds {len(row['locks_held'])} lock(s) while idle — a process still holds it (hung?), or a "
                   "lock directory a killed run left behind")
    if finished and row["reservation_age_days"] is not None and not row.get("reclaimed_for_correction"):
        why.append("finished but its paper reservation was never released")
    if not finished and row["reservation_age_days"] is not None and row["reservation_age_days"] >= 7:
        why.append(f"reservation held {row['reservation_age_days']:.0f}d")
    return row


def main() -> int:
    # `status.py <tree> | head` is the obvious way to use this, and it made the tool die with a
    # BrokenPipeError traceback and a non-zero exit that looked like a real failure. Restoring the
    # default SIGPIPE disposition makes it exit quietly the way every other read-only CLI does.
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tree", type=pathlib.Path)
    ap.add_argument("--stuck", action="store_true", help="only audits needing attention")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--idle-days", type=float, default=DEFAULT_IDLE_DAYS)
    args = ap.parse_args()

    tree = args.tree.resolve()
    if not tree.is_dir():
        print(f"FATAL: not a directory: {tree}", file=sys.stderr)
        return 2

    reservations = _reservations(tree)
    registry = _registry(tree)
    rows = []
    for audit in sorted(find_audits(tree)):
        try:
            row = inspect(audit, reservations, registry)
        except Exception as exc:  # one damaged audit must not end the sweep
            row = {
                "journal": audit.parent.parent.name, "paper": audit.parent.name,
                "audit_dir": str(audit), "archived": False, "relocated": False,
                "unreadable": f"{type(exc).__name__}: {exc}",
                "audit_id": None, "schema_version": None, "status": None,
                "stopping_reason": None, "rounds_run": None, "findings": None, "phase1": None,
                "last_round": None, "last_round_outcome": None, "idle_days": None,
                "locks_held": [], "conflicted_copies": 0, "reservation_age_days": None,
                "reservation_file": None, "attention": [],
            }
        rows.append(classify(row, args.idle_days))

    shown = [r for r in rows if r["attention"]] if args.stuck else rows
    if args.as_json:
        print(json.dumps(shown, indent=1))
        # Same exit contract as the text output: a batch driver branches on it either way.
        return 1 if any(r["attention"] for r in rows) else 0

    # the registry also holds audits this walk never visits (old_runs/), so its own count is shown as well
    unusable = registry["unusable_pointers"]
    unrowed = len(registry["filed"] - registry["rowed"] - set(unusable))
    if unrowed or unusable or registry["conflicted_copies"] or registry["stale_rows"] or registry["broken_rows"]:
        # the current.json count only when there is one, so the line reads as before otherwise
        print(f"registry: {unrowed} filed audit(s) without an index row, "
              + (f"{len(unusable)} whose current.json cannot be used, " if unusable else "")
              + f"{registry['conflicted_copies']} Dropbox "
              f"conflicted copies of index.jsonl, {len(registry['stale_rows'])} stale and "
              f"{len(registry['broken_rows'])} unusable index_row.json "
              "(finalization_receipt.py registry-audit <tree>/registry)\n")
    if not shown:
        print(f"{len(rows)} audit(s) found; none need attention.")
        return 0

    print(f"{len(rows)} audit(s); {sum(1 for r in rows if r['attention'])} need attention\n")
    for r in sorted(shown, key=lambda x: (-(x["idle_days"] or 0))):
        idle = f"{r['idle_days']:.1f}d" if r["idle_days"] is not None else "  ?  "
        reopened_row = r["status"] == "complete" and (r.get("reclaimed_for_correction") or r.get("published_set_missing"))
        head = f"{('reopened' if reopened_row else r['status']) or 'no-run.json':<10} idle {idle:>6}  r{r['last_round'] or 0}"
        run = f"  [run folder: {r['run_folder']}]" if r.get("run_folder") else ""
        print(f"{head}  {(r.get('journal') or '?')}/{r['paper']}"[:len(head) + 2 + 72] + run)
        detail = (f"           phase1={r['phase1']} findings={r['findings']} "
                  f"rounds_run={r['rounds_run']} audit_id={r['audit_id']}")
        print(detail)
        for w in r["attention"]:
            print(f"           ! {w}")
        if r["attention"] and r.get("last_note"):
            print(f"           note: {r['last_note']}")   # the journal's last word, when it is a note
        print()

    # Non-zero when something needs attention, so a batch driver can branch on it.
    return 1 if any(r["attention"] for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
