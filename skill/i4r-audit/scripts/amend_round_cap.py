#!/usr/bin/env python3
"""Raise an existing audit's critique round cap, as a journaled and reasoned operator act (2.6.9).

    python3 scripts/amend_round_cap.py <audit-dir> --reason "<why>" [--to 7] [--reopen] [--dry-run]
        [--accept-rerun-cost] [--archived-set <folder>] [--unconfirmed-claim]

Why this exists: an audit's round cap is frozen in its pipeline-policy.snapshot.json at init_run.sh.
2.6.9 raised the programme's cap from 5 to 7, which only audits initialised afterwards receive. An
audit that stopped at its old cap with unresolved scope (cap_with_unresolved), or that reached it
unfinished, can only continue after this tool raises its own cap; a hand edit breaks the snapshot's
hash binding, and init_run.sh --force restarts the whole journal.

What it does, in one journal lock, and only after every check passed:
  * rewrites the snapshot with critique.round_cap = --to (only the shipped cap, which must be higher
    than the current one), the stop text that used to name the old cap replaced by the shipped
    number-free text, and a critique.round_cap_amendments history entry {from, to, amended_at, reason,
    the replaced snapshot's hashes and archive name, skill version, shipped policy hash};
  * archives the replaced snapshot bytes read-only (0400) beside it as
    pipeline-policy.snapshot.superseded-<time>.json;
  * journals [audit_reopened (with --reopen)] then policy_amendment; the projection sets run.round_cap
    and the run's snapshot binding, and appends run.policy_amendments (and run.reopenings).

What it refuses: a run that is not live metadata 2.1; an invalid snapshot, a run not bound to it, or a
snapshot cap other than the journaled one; a --to that is not higher than the current cap or is above
the shipped cap; an empty reason; a live critique driver; a round already prepared above the current
cap; a round prepared or started and not finished for which a completed review is cached (its relaunch
would pay again for each: finish and integrate it first, or pass --accept-rerun-cost, which prints that
count; with nothing cached it only notes the round), checked after the prospective round-cap check; a
finalized audit without --reopen (and --reopen on one that is not finalized); a published audit whose
filed set is still in place, or whose paper is not claimed again (named by its paper_source.json; a
re-claim that cannot be confirmed is refused unless --unconfirmed-claim); an interrupted finalize; and
any amendment whose resulting state would fail scripts/round_cap_amendment.py. That last check runs on
the prospective state (the events projected onto the journal, the new snapshot and the archive) before
anything is written, in the dry run as in the real run. With --reopen the superseded
finalization_receipt.json is kept read-only as finalization_receipt.superseded-<finalize seq>.json, and
the journal records it and where the published set was moved.

Re-pin a critic route (repin_critic_route.py) BEFORE amending: once an audit has an amendment its route is
fixed, and the re-pin tool refuses.

Every round already run keeps binding the policy frozen in its own round snapshot, so rounds up to the
old cap validate unchanged; rounds above it bind the amended snapshot. finalize_run.sh and publication
re-check the whole chain (scripts/round_cap_amendment.py). Runbook: INSTALL.md, 'Raising the round cap
of an existing audit'.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lifecycle_journal  # noqa: E402
import record_invocation  # noqa: E402
import round_cap_amendment as rca  # noqa: E402

SNAPSHOT = rca.SNAPSHOT
SOURCE = rca.AMEND_SOURCE


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shipped_policy() -> dict:
    return json.loads((HERE.parent / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))


REVIEW_ROLE_CLASSES = {"critic", "synthesizer", "adjudicator"}


def rerun_cost(run: dict, unfinished: dict[int, dict]) -> dict[int, int]:
    """Completed critic/adjudicator reviews of each unfinished round: what its relaunch would pay for again."""
    cost = {number: 0 for number in unfinished}
    for row in run.get("invocations") or []:
        if isinstance(row, dict) and row.get("round") in cost and row.get("status") == "completed" \
                and row.get("role") != "model_probe" and (row.get("role_class") in REVIEW_ROLE_CLASSES
                                                          or row.get("role") in {"targeted_adjudicator", "critic_synth"}):
            cost[row["round"]] += 1
    return cost


def amend(root: pathlib.Path, *, to: int | None, reason: str, reopen: bool = False,
          purpose: str = "continue_critique", dry_run: bool = False, at: str | None = None,
          accept_rerun_cost: bool = False, archived_set: pathlib.Path | None = None,
          unconfirmed_claim_ok: bool = False) -> dict:
    root = pathlib.Path(root).resolve()
    if not dry_run and (not reason or not reason.strip()):
        raise rca.AmendmentError("--reason is required: the journal must say why this audit's round cap is raised")
    run = rca.read_run(root)
    events = rca.require_live_21(root, run)
    policy = record_invocation.load_policy_snapshot(root)          # verifies the envelope hash
    record_invocation.validate_policy_binding(root)                 # run.json binds these exact bytes
    shipped = _shipped_policy()
    shipped_cap = shipped["critique"]["round_cap"]
    current = (policy.get("critique") or {}).get("round_cap")
    target = shipped_cap if to is None else to
    if not isinstance(current, int) or isinstance(current, bool):
        raise rca.AmendmentError(f"the snapshot's critique.round_cap is not an integer: {current!r}")
    journaled = lifecycle_journal.journaled_round_cap(events)
    if journaled is not None and journaled != current:
        raise rca.AmendmentError(f"the snapshot's round cap {current} differs from the cap the journal records "
                                 f"({journaled}); a cap changes only through this tool: restore the snapshot run.json "
                                 "binds before amending")
    if target <= current:
        raise rca.AmendmentError(f"--to {target} does not raise this audit's cap of {current}; a cap is only raised")
    if target > shipped_cap:
        raise rca.AmendmentError(f"--to {target} is above the cap this skill ships ({shipped_cap}); install the release "
                                 "that ships it first")
    if target not in rca.LIVE_ROUND_CAPS:
        raise rca.AmendmentError(f"--to must be one of the live caps {list(rca.LIVE_ROUND_CAPS)} above {current}")
    if run.get("round_cap") != current:
        raise rca.AmendmentError(f"run.json round_cap {run.get('round_cap')} differs from the snapshot's {current}; "
                                 "finalize reports that mismatch; resolve it before amending")
    lock = rca.live_driver_lock(root)
    if lock:
        raise rca.AmendmentError(f"a critique driver is running and holds {lock}; let it finish or stop it first")
    above = sorted({number for number in (rca._round_of(event) for event in events
                                          if event.get("type") == "round_prepared")
                    if isinstance(number, int) and number > current})
    if above:
        raise rca.AmendmentError(f"round(s) {above} are already prepared above the cap of {current}; the journal must "
                                 "show every such round prepared after the amendment")
    notes: list[str] = []
    rerun_refusal: str | None = None
    # A round prepared or started and not finished freezes this snapshot into its round snapshot: rewriting it
    # supersedes the round's plan on relaunch, and every critic review and adjudication already completed and cached
    # for it is dispatched (and paid for) again.
    unfinished = rca.unfinished_rounds(events)
    if unfinished:
        cost = rerun_cost(run, unfinished)
        described = "; ".join(
            f"round {number} ({info['state']}, journal seq {info['seq']}): {cost[number]} completed critic/adjudicator "
            "review(s) would be dispatched again" for number, info in sorted(unfinished.items()))
        message = (f"{described}. The amendment rewrites pipeline-policy.snapshot.json, which the round's snapshot "
                   "copies, so relaunching the round supersedes its plan and pays again for every review already "
                   "cached for it (and its targeted adjudication)")
        if sum(cost[number] for number in unfinished) == 0:
            # Nothing completed is cached for the unfinished round(s), so relaunching pays for nothing again.
            notes.append(message.replace("pays again for every review already cached for it",
                                         "no completed review is cached for it, so nothing is paid again"))
        elif accept_rerun_cost:
            notes.append("--accept-rerun-cost: " + message)
        else:
            # Raised only after the prospective check below, so an amendment that would fail its own round-cap
            # check reports that, the accurate reason, rather than a cost the operator could accept.
            rerun_refusal = (message + ". Finish and integrate the round first (run_critique.sh, then "
                             "integrate_round.py), or pass --accept-rerun-cost to amend anyway")
    closed = lifecycle_journal.unreopened_finalize(events) is not None
    if closed and not reopen:
        raise rca.AmendmentError(
            "this audit is finalized (its last finalize has no later audit_reopened); a finalized audit runs no "
            "further round until it is reopened: pass --reopen (a published audit first moves its filed set aside "
            "and is claimed again, INSTALL.md 'Correcting a published comment' steps 1-2)")
    at = at or _now()
    specs: list[dict] = []
    if reopen:
        if not closed:
            raise rca.AmendmentError("--reopen: the audit is not finalized, so there is nothing to reopen; drop it")
        spec, reopen_notes = rca.reopen_spec(root, events, run, reason=reason if reason.strip() else "dry run",
                                             purpose=purpose, at=at, source=SOURCE, archived_set=archived_set,
                                             unconfirmed_claim_ok=unconfirmed_claim_ok)
        specs.append(spec)
        notes.extend(reopen_notes)

    old_bytes = (root / SNAPSHOT).read_bytes()
    archive = rca.archive_name(root, at)
    new_policy = copy.deepcopy(policy)
    critique = new_policy["critique"]
    changed_leaves = ["critique.round_cap"]
    critique["round_cap"] = target
    shipped_stop = ((shipped["critique"].get("stopping") or {}).get("cap_with_unresolved"))
    stopping = critique.get("stopping")
    if isinstance(stopping, dict) and isinstance(shipped_stop, str) and stopping.get("cap_with_unresolved") != shipped_stop:
        stopping["cap_with_unresolved"] = shipped_stop   # the old text named the old cap ("stop at round 5 ...")
        changed_leaves.append("critique.stopping.cap_with_unresolved")
    entry = {
        "from": current, "to": target, "amended_at": at, "reason": reason.strip() or "dry run",
        "previous_policy_sha256": record_invocation.policy_hash(policy),
        "previous_snapshot_sha256": rca.sha256_file(root / SNAPSHOT),
        "archived_snapshot": archive.name, "changed_leaves": changed_leaves,
        "skill_version": rca.skill_version(),
        "canonical_policy_sha256": record_invocation.policy_hash(shipped),
    }
    critique.setdefault("round_cap_amendments", []).append(entry)
    data = rca.envelope_bytes(new_policy)
    new_sha = hashlib.sha256(data).hexdigest()
    toolkit = rca.host_toolkit()
    specs.append({"type": rca.AMEND_EVENT, "at": at, "source": SOURCE, "payload": {
        "leaf": rca.LEAF, "from": current, "to": target, "reason": entry["reason"],
        "previous_snapshot_sha256": entry["previous_snapshot_sha256"], "snapshot_sha256": new_sha,
        "previous_policy_sha256": entry["previous_policy_sha256"],
        "policy_sha256": record_invocation.policy_hash(new_policy),
        "archived_snapshot": archive.name, "changed_leaves": changed_leaves,
        "skill_version": toolkit["skill_version"], "scripts_sha256": toolkit["scripts_sha256"],
    }})
    # The state this amendment would leave, proven BEFORE anything is written (dry run and real run alike): the
    # events projected onto the journal, the new snapshot and the archive of the replaced one.
    overlay = {SNAPSHOT: data, archive.name: old_bytes}
    violations = rca.prospective(root, events, specs, overlay)
    if violations:
        raise rca.AmendmentError("the amended audit would fail its own round-cap check, so nothing was written: "
                                 + "; ".join(violations))
    if rerun_refusal:
        raise rca.AmendmentError(rerun_refusal)
    result = {"changed": not dry_run, "dry_run": dry_run, "from": current, "to": target, "reopen": reopen,
              "archived_snapshot": archive.name, "snapshot_sha256": new_sha, "notes": notes,
              "changed_leaves": changed_leaves}
    if dry_run:
        return result

    # Write order, crash-safe: the new snapshot bytes are staged first; the old bytes (and, with --reopen, the
    # superseded finalization receipt) are archived; then, under the journal lock, the snapshot is replaced and the
    # events appended. A failed append puts the old bytes back and removes the archives this run wrote.
    fd, tmp = tempfile.mkstemp(prefix=SNAPSHOT + ".", dir=root)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    receipt_copy = None
    try:
        with open(archive, "xb") as handle:
            handle.write(old_bytes); handle.flush(); os.fsync(handle.fileno())
        os.chmod(archive, 0o400)
        if reopen:
            receipt_copy = rca.preserve_receipt(root, specs[0])
        with lifecycle_journal.journal_lock(root):
            if lifecycle_journal.load_events(root) != events:
                raise rca.AmendmentError("the journal changed while the amendment was being prepared; run it again")
            os.replace(tmp, root / SNAPSHOT)
            try:
                # proves the whole batch projectable before anything is appended; a refusal appends nothing
                appended = lifecycle_journal._append_locked(root, specs)
            except Exception:
                restore_fd, restore_tmp = tempfile.mkstemp(prefix=SNAPSHOT + ".", dir=root)
                with os.fdopen(restore_fd, "wb") as handle:
                    handle.write(old_bytes); handle.flush(); os.fsync(handle.fileno())
                os.replace(restore_tmp, root / SNAPSHOT)
                raise
            lifecycle_journal.project_all_locked(root, appended)
    except Exception:
        pathlib.Path(tmp).unlink(missing_ok=True)
        if archive.exists() and archive.read_bytes() == old_bytes and (root / SNAPSHOT).read_bytes() == old_bytes:
            rca.discard(archive)
            rca.discard(receipt_copy)
        raise
    record_invocation.load_policy_snapshot(root)
    record_invocation.validate_policy_binding(root)
    violations, _ = rca.verify(root)
    if violations:
        raise rca.AmendmentError("the amendment was written, but its own check failed: " + "; ".join(violations))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--reason", default="")
    parser.add_argument("--to", type=int, help="the new cap (default: the cap this skill ships)")
    parser.add_argument("--reopen", action="store_true",
                        help="also reopen a finalized audit (journaled before the amendment)")
    parser.add_argument("--accept-rerun-cost", action="store_true",
                        help="amend although a round is prepared or started and unfinished: its relaunch pays again "
                             "for every review already cached for it (the refusal prints the count)")
    parser.add_argument("--archived-set", type=pathlib.Path,
                        help="with --reopen: the folder the published set was moved to (default: found under "
                             "old_runs/<Journal>/<Paper>.published-*)")
    parser.add_argument("--unconfirmed-claim", action="store_true",
                        help="with --reopen of a published audit outside a tree: reopen although the paper's "
                             "re-claim cannot be confirmed")
    parser.add_argument("--dry-run", action="store_true", help="run every check, report the change, write nothing")
    args = parser.parse_args(argv)
    try:
        result = amend(args.audit_dir, to=args.to, reason=args.reason, reopen=args.reopen, dry_run=args.dry_run,
                       accept_rerun_cost=args.accept_rerun_cost, archived_set=args.archived_set,
                       unconfirmed_claim_ok=args.unconfirmed_claim)
    except (rca.AmendmentError, ValueError, OSError, lifecycle_journal.JournalError) as exc:
        print(f"FATAL: amend_round_cap.py: {exc}", file=sys.stderr)
        return 2
    for note in result["notes"]:
        print(f"note: {note}", file=sys.stderr)
    verb = "would raise" if result["dry_run"] else "raised"
    print(f"{verb} the round cap {result['from']} -> {result['to']}"
          + (" and reopen the finalized audit" if result["reopen"] else "")
          + ("; every check passed, nothing written" if result["dry_run"] else
             f"; the replaced snapshot is archived read-only as {result['archived_snapshot']}"))
    if not result["dry_run"]:
        print("next: rewrite WRAPUP.txt for the next round (round N of cap "
              f"{result['to']}, amended from {result['from']}), rebuild its packet with make_diff_packet.py, run its "
              "post-delta gates, then run_critique.sh (INSTALL.md, 'Raising the round cap of an existing audit')")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
