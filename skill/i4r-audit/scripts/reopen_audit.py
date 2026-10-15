#!/usr/bin/env python3
"""Reopen a finalized audit so it may run another critique round, as a journaled and reasoned act (2.6.9).

    python3 scripts/reopen_audit.py <audit-dir> --reason "<why>" \
        [--purpose correct_published|continue_critique] [--archived-set <folder>] [--unconfirmed-claim] [--dry-run]

A finalize closes an audit: from 2.6.9 the lifecycle journal and run_critique.sh refuse any new round on
a finalized audit until an audit_reopened event follows its last finalize. This tool writes that event
for a correction inside the audit's existing round cap (INSTALL.md, 'Correcting a published comment').
To continue past the cap, use scripts/amend_round_cap.py --reopen instead, which reopens and raises the
cap in one journal lock.

The event records the close it reopens (finalize seq and time, stopping reason, rounds run, the
finalization receipt's hash and registry version, the filed publication if any and where its set was
moved) and the skill that reopened it. The superseded finalization_receipt.json is kept read-only as
finalization_receipt.superseded-<finalize seq>.json (the payload names it and its hash): a re-finalize
rewrites the receipt in place. The projection clears the stop, its evidence (the review_scope fields), ended_at and the
compliance verdict, so the next finalize must establish them afresh; the gate log and every earlier
event stay. A correction that needs no new round (an Appendix-only re-file) needs no reopen.

Refused: a run that is not live metadata 2.1; an audit that is not finalized; an empty reason; a live
critique driver; an interrupted finalize; a published audit whose filed set is still in place or whose
paper is not claimed again since the filing (the paper is named by the audit's paper_source.json; a
re-claim that cannot be confirmed is refused unless --unconfirmed-claim); and a reopen whose resulting
journal would fail scripts/round_cap_amendment.py (checked before anything is written, in the dry run
too).
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lifecycle_journal  # noqa: E402
import round_cap_amendment as rca  # noqa: E402

SOURCE = "reopen_audit.py"


def reopen(root: pathlib.Path, *, reason: str, purpose: str = "correct_published", dry_run: bool = False,
           at: str | None = None, archived_set: pathlib.Path | None = None,
           unconfirmed_claim_ok: bool = False) -> dict:
    root = pathlib.Path(root).resolve()
    if not dry_run and (not reason or not reason.strip()):
        raise rca.AmendmentError("--reason is required: the journal must say why a finalized audit is reopened")
    run = rca.read_run(root)
    events = rca.require_live_21(root, run)
    lock = rca.live_driver_lock(root)
    if lock:
        raise rca.AmendmentError(f"a critique driver is running and holds {lock}; let it finish or stop it first")
    at = at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    spec, notes = rca.reopen_spec(root, events, run, reason=reason if reason.strip() else "dry run",
                                  purpose=purpose, at=at, source=SOURCE, archived_set=archived_set,
                                  unconfirmed_claim_ok=unconfirmed_claim_ok)
    # the state the reopen would leave, proven before anything is written (a stray round event after the close is
    # a warning on a pre-2.6.9 record and a permanent violation once the audit is reopened)
    violations = rca.prospective(root, events, [spec])
    if violations:
        raise rca.AmendmentError("the reopened audit would fail its round-cap check, so nothing was written: "
                                 + "; ".join(violations))
    result = {"dry_run": dry_run, "notes": notes, "previous_finalized_at": spec["payload"]["previous_finalized_at"],
              "previous_stopping_reason": spec["payload"]["previous_stopping_reason"]}
    if dry_run:
        return result
    receipt_copy = rca.preserve_receipt(root, spec)
    try:
        with lifecycle_journal.journal_lock(root):
            if lifecycle_journal.load_events(root) != events:
                raise rca.AmendmentError("the journal changed while the reopen was being prepared; run it again")
            appended = lifecycle_journal._append_locked(root, [spec])
            lifecycle_journal.project_all_locked(root, appended)
    except Exception:
        if not any(event.get("type") == rca.REOPEN_EVENT and event.get("at") == at
                   for event in lifecycle_journal.load_events(root)[len(events):]):
            rca.discard(receipt_copy)
        raise
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--reason", default="")
    parser.add_argument("--purpose", default="correct_published", choices=rca.PURPOSES)
    parser.add_argument("--archived-set", type=pathlib.Path,
                        help="the folder the published set was moved to (default: found under "
                             "old_runs/<Journal>/<Paper>.published-*)")
    parser.add_argument("--unconfirmed-claim", action="store_true",
                        help="reopen a published audit outside a tree although its paper's re-claim cannot be confirmed")
    parser.add_argument("--dry-run", action="store_true", help="run every check, report the change, write nothing")
    args = parser.parse_args(argv)
    try:
        result = reopen(args.audit_dir, reason=args.reason, purpose=args.purpose, dry_run=args.dry_run,
                        archived_set=args.archived_set, unconfirmed_claim_ok=args.unconfirmed_claim)
    except (rca.AmendmentError, ValueError, OSError, lifecycle_journal.JournalError) as exc:
        print(f"FATAL: reopen_audit.py: {exc}", file=sys.stderr)
        return 2
    for note in result["notes"]:
        print(f"note: {note}", file=sys.stderr)
    print(("would reopen" if result["dry_run"] else "reopened")
          + f" the audit finalized at {result['previous_finalized_at']} ({result['previous_stopping_reason']})"
          + ("; every check passed, nothing written" if result["dry_run"] else
             "; its stop, review scope and compliance verdict are cleared until the next finalize"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
