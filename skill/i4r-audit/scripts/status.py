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
first version of this. It opens nothing for writing, takes no lock, and starts no provider call,
so it is safe to run against a live batch.

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


def _json(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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


def inspect(audit: pathlib.Path, reservations: dict[tuple, dict]) -> dict:
    paper_dir = audit.parent
    run = _json(audit / "run.json") or {}
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

    findings = audit / "work/findings.jsonl"
    if findings.is_file():
        try:
            row["findings"] = sum(1 for l in findings.read_text().splitlines() if l.strip())
        except OSError:
            pass

    p1 = _json(audit / "phase1/status.json")
    if isinstance(p1, dict):
        row["phase1"] = p1.get("status")

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

    # A live lock on a quiet audit is the signature of a killed process.
    # Phase 1 leaves phase1/run.lock behind (it is an flock target, never deleted), so a lock FILE counts as held
    # only when a process still holds its flock
    for lock in list(audit.rglob("run.lock")) + list(audit.rglob(".runtime_lock")):
        if lock.is_file() and not _lock_is_held(lock):
            continue
        row["locks_held"].append(str(lock.relative_to(audit)))
    row["conflicted_copies"] = sum(1 for _ in audit.rglob("*conflicted copy*"))

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
    if row["status"] is None:
        why.append("no run.json: intake started but never initialised")
    elif not finished and idle is not None and idle >= idle_days:
        shown_status = ("reopened (journaled: %s)" % row["reopened"] if isinstance(row.get("reopened"), str)
                        else "reopened for correction" if reopened else row["status"])
        why.append(f"unfinished and idle {idle:.1f}d (status={shown_status})")
    if row["status"] == "complete" and row.get("published_set_missing") and not row.get("reclaimed_for_correction"):
        why.append("complete, but its published comments/ and metadata/ folders are gone (a correction started "
                   "without re-claiming the paper?)")
    if row["status"] == "aborted":
        why.append("aborted")
    if row["conflicted_copies"]:
        why.append(f"{row['conflicted_copies']} Dropbox conflicted copies in the audit tree")
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
    rows = []
    for audit in sorted(find_audits(tree)):
        try:
            row = inspect(audit, reservations)
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
        print()

    # Non-zero when something needs attention, so a batch driver can branch on it.
    return 1 if any(r["attention"] for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
