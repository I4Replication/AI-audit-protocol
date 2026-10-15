#!/usr/bin/env python3
"""Re-pin a live audit's critic route to the shipped one (config/pipeline-policy-v2.json).

    python3 scripts/repin_critic_route.py <audit-dir> --reason "<why>"

Since 2.7.5 the only target is the shipped route: critics and targeted adjudicator on gpt-6.1-sol. An audit
pinned to an older route (gpt-6-sol, gpt-5.6-terra) with no completed review can be moved up to it; no audit
can be moved to any other model. --critic-model and --adjudicator-model are still accepted, but only with
the shipped values. Efforts and provider never change.

2.7.5: from 2026-09-30 to 2026-10-07 at least twelve audits initialised on Sol were re-pinned to gpt-5.6-terra,
several within seconds of init, because their hosts' Codex accounts could not use Sol, so they ran their critique
on the older model. An audit initialised on 2.7.5 also freezes critique.critic_route_lock, which run_critique.sh
and the invocation ledger (record_invocation.validate_record) enforce whichever copy of this tool moved it. Whether a host can use the critic model is now checked before an audit starts
(scripts/check_critic_access.py, run by new_audit.sh); a host that cannot switches its Codex account, not
the audit's model.

Why this exists: an audit's critic route is frozen in its pipeline-policy.snapshot.json at
init_run.sh, and the critique driver refuses any other route. On 2026-09-23 a teammate's audit
was pinned to gpt-6-sol, which her ChatGPT account cannot use, and nothing in the skill could
change the pin without hand-editing hash-bound files -- which she correctly refused to do.

What it does, and what it refuses:
  * refuses once any critic or adjudicator review has COMPLETED, or any round has reached quorum
    or been integrated: every completed review of one audit must be on one route;
  * refuses while a critique driver holds a round lock;
  * refuses once the audit has a round-cap amendment (policy_amendment, 2.6.9): an amended snapshot may
    differ from the one it replaced only at the cap, so re-pin BEFORE amending;
  * rewrites the snapshot with the new route and keeps the replaced one in
    policy.critic_route_history (with the time and reason), so the model probe and failed
    attempts made before the re-pin still validate against the route in force when they ran;
  * archives the previous snapshot bytes read-only beside it, under a name no earlier rewrite used
    (pipeline-policy.snapshot.superseded-<time>[-N].json);
  * journals the change: a metadata_correction rebinding run.json's toolkit hash to the new
    snapshot bytes, and a note naming old route, new route and reason. The snapshot is archived and
    replaced under the journal lock before those events are appended, and put back if the append
    fails, so the journal never binds bytes that were not written.
The next `run_critique.sh N` prepares a fresh plan for the round (the snapshot is part of its input)
and supersedes the unfinished one.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
# 2.7.5: a re-pin may only move an audit TO the shipped critic route. Tests of the re-pin mechanics (archive,
# history, same-second rewrites) switch this off explicitly; operators never do.
SHIPPED_ROUTE_ONLY = True
sys.path.insert(0, str(HERE))
import lifecycle_journal  # noqa: E402
import record_invocation  # noqa: E402
import round_cap_amendment  # noqa: E402

SNAPSHOT = "pipeline-policy.snapshot.json"
MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,63}$")
REVIEW_ROLE_CLASSES = {"critic", "synthesizer", "adjudicator"}


class RepinError(Exception):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(root: pathlib.Path) -> list[dict]:
    path = root / "invocations.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _live_driver_lock(root: pathlib.Path) -> str | None:
    for pid_file in root.glob("critique/round_*/.runtime_lock/pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
        except (ValueError, OSError):
            continue
        return f"{pid_file.parent} (pid {pid})"
    return None


def repin(root: pathlib.Path, *, critic_model: str | None, adjudicator_model: str | None,
          reason: str, at: str | None = None, dry_run: bool = False) -> dict:
    root = root.resolve()
    if not dry_run and (not reason or not reason.strip()):
        raise RepinError("--reason is required: the journal must say why the pinned route changed")
    policy = record_invocation.load_policy_snapshot(root)          # verifies the envelope hash
    shipped = json.loads((HERE.parent / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))
    shipped_critic = shipped["models"]["critics"]["requested_model"]
    shipped_adjudicator = shipped["models"]["targeted_adjudicator"]["requested_model"]
    new_critic = critic_model or shipped_critic
    new_adjudicator = adjudicator_model or (critic_model or shipped_adjudicator)
    for label, model in (("critic", new_critic), ("adjudicator", new_adjudicator)):
        if not MODEL_ID.fullmatch(model):
            raise RepinError(f"{label} model id is not a plain model identifier: {model!r}")
    old_critics = policy["models"]["critics"]
    old_adjudicator = policy["models"]["targeted_adjudicator"]
    if old_critics["requested_model"] == new_critic and old_adjudicator["requested_model"] == new_adjudicator:
        return {"changed": False, "critics": new_critic, "targeted_adjudicator": new_adjudicator}
    # 2.7.5: a re-pin moves an audit only TO the shipped route, and never off the route an audit initialised on
    # 2.7.5 or later froze as critique.critic_route_lock (run_critique.sh refuses any other route on such an audit).
    # A request that changes nothing was answered above, so re-running an old command on an older-route audit is
    # still a no-op, not a refusal.
    if SHIPPED_ROUTE_ONLY:
        for flag, given, allowed in (("--critic-model", critic_model, shipped_critic),
                                     ("--adjudicator-model", adjudicator_model, shipped_adjudicator)):
            if given is not None and given != allowed:
                raise RepinError(
                    f"{flag} {given}: since 2.7.5 a re-pin can only move an audit to the shipped critic route "
                    f"(critics {shipped_critic}, adjudicator {shipped_adjudicator}). If this host's account cannot "
                    f"use {shipped_critic}, the account is changed, not the audit's model: check it with "
                    "scripts/check_critic_access.py, and run the round from an account that offers the model.")
        route_lock = ((policy.get("critique") or {}).get("critic_route_lock") or {}).get("model")
        if route_lock and (new_critic != route_lock or new_adjudicator != route_lock):
            raise RepinError(f"this audit froze critique.critic_route_lock={route_lock} at init; its critic route cannot "
                             f"move to critics {new_critic}, adjudicator {new_adjudicator}")

    completed_reviews = [row for row in _rows(root) if row.get("status") == "completed"
                         and row.get("role") != "model_probe"
                         and (row.get("role_class") in REVIEW_ROLE_CLASSES
                              or row.get("role") in {"targeted_adjudicator", "critic_synth"})]
    if completed_reviews:
        raise RepinError(
            f"{len(completed_reviews)} critic/adjudicator review(s) already completed on "
            f"{old_critics['requested_model']} (first: {completed_reviews[0].get('invocation_id')}); "
            "re-pinning now would put one audit's reviews on two routes. Finish on the current route, "
            "or start a new audit.")
    events = lifecycle_journal.load_events(root) if (root / "run.events.jsonl").is_file() else []
    finished = sorted({(e.get("payload") or {}).get("round") for e in events
                       if e["type"] in {"round_quorum_complete", "round_integrated", "round_end"}} - {None})
    if finished:
        raise RepinError(f"round(s) {finished} already reached quorum or were integrated on the current route")
    lock = _live_driver_lock(root)
    if lock:
        raise RepinError(f"a critique driver is running and holds {lock}; stop it before re-pinning")
    amended = [e.get("seq") for e in events if e.get("type") == lifecycle_journal.AMEND_EVENT]
    if amended:
        raise RepinError(
            f"this audit's round cap was amended (policy_amendment, journal seq {amended[0]}): an amended snapshot may "
            "differ from the one it replaced only at the cap, so its critic route is fixed. Re-pin BEFORE amending "
            "(INSTALL.md, 'Raising the round cap of an existing audit'); an amended audit whose account cannot use "
            "its pinned model is continued by starting a new audit")

    if dry_run:
        return {"changed": False, "dry_run": True, "critics": new_critic, "targeted_adjudicator": new_adjudicator,
                "previous": {"critics": old_critics["requested_model"],
                             "targeted_adjudicator": old_adjudicator["requested_model"]}}
    at = at or _now()
    new_policy = copy.deepcopy(policy)
    history = new_policy.setdefault("critic_route_history", [])
    entry = {"superseded_at": at, "reason": reason.strip(),
             "critics": {k: old_critics[k] for k in ("requested_model", "provider", "reasoning_effort")},
             "targeted_adjudicator": {k: old_adjudicator[k] for k in ("requested_model", "provider", "reasoning_effort")}}
    history.append(entry)
    new_policy["models"]["critics"]["requested_model"] = new_critic
    new_policy["models"]["targeted_adjudicator"]["requested_model"] = new_adjudicator
    probe = (new_policy.get("critique") or {}).get("model_probe")
    if isinstance(probe, dict) and "requested_model" in probe:
        probe["requested_model"] = new_critic
    envelope = {"snapshot_version": "1", "policy_sha256": record_invocation.policy_hash(new_policy),
                "policy": new_policy}
    data = (json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    new_sha = hashlib.sha256(data).hexdigest()

    path = root / SNAPSHOT
    old_bytes = path.read_bytes()
    # a fresh name: a same-second amend or an earlier re-pin already holds the plain one, read-only
    archive = round_cap_amendment.archive_name(root, at)
    specs = []
    if events:
        run_path = root / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8")) if run_path.is_file() else {}
        if isinstance(run.get("toolkit"), dict):
            toolkit = dict(run["toolkit"]); toolkit["pipeline_policy_snapshot_sha256"] = new_sha
            specs.append({"type": "metadata_correction", "payload": {"set": {"toolkit": toolkit}},
                          "at": at, "source": "repin_critic_route.py"})
        specs.append({"type": "note", "at": at, "source": "repin_critic_route.py", "payload": {"text": (
            f"Critic route re-pinned: critics {old_critics['requested_model']} -> {new_critic}, "
            f"targeted adjudicator {old_adjudicator['requested_model']} -> {new_adjudicator} "
            f"(efforts unchanged). Reason: {reason.strip()} Previous snapshot kept at {archive.name}; "
            "calls made before the re-pin validate against critic_route_history.")}})
    # Write order, crash-safe (as amend_round_cap.py): stage the new bytes, archive the old ones, then under the
    # journal lock replace the snapshot and append the rebinding; a failed append puts the old bytes back. The
    # journal therefore never binds a snapshot that was not written.
    fd, tmp = tempfile.mkstemp(prefix=SNAPSHOT + ".", dir=root)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    try:
        with open(archive, "xb") as handle:
            handle.write(old_bytes); handle.flush(); os.fsync(handle.fileno())
        os.chmod(archive, 0o400)
        with (lifecycle_journal.journal_lock(root) if events else contextlib.nullcontext()):
            if events and lifecycle_journal.load_events(root) != events:
                raise RepinError("the journal changed while the re-pin was being prepared; run it again")
            if path.read_bytes() != old_bytes:
                raise RepinError(f"{SNAPSHOT} changed while the re-pin was being prepared; run it again")
            os.replace(tmp, path)
            if specs:
                try:
                    appended = lifecycle_journal._append_locked(root, specs)
                except Exception:
                    restore_fd, restore_tmp = tempfile.mkstemp(prefix=SNAPSHOT + ".", dir=root)
                    with os.fdopen(restore_fd, "wb") as handle:
                        handle.write(old_bytes); handle.flush(); os.fsync(handle.fileno())
                    os.replace(restore_tmp, path)
                    raise
                lifecycle_journal.project_all_locked(root, appended)
    except Exception:
        pathlib.Path(tmp).unlink(missing_ok=True)
        if archive.exists() and archive.read_bytes() == old_bytes and path.read_bytes() == old_bytes:
            os.chmod(archive, 0o600)
            archive.unlink()
        raise
    record_invocation.load_policy_snapshot(root)
    record_invocation.validate_policy_binding(root)
    return {"changed": True, "critics": new_critic, "targeted_adjudicator": new_adjudicator,
            "previous": {"critics": old_critics["requested_model"],
                         "targeted_adjudicator": old_adjudicator["requested_model"]},
            "archived_snapshot": archive.name, "snapshot_sha256": new_sha}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--reason", default="")
    parser.add_argument("--critic-model")
    parser.add_argument("--adjudicator-model")
    parser.add_argument("--dry-run", action="store_true", help="run every check, report the change, write nothing")
    args = parser.parse_args(argv)
    try:
        result = repin(args.audit_dir, critic_model=args.critic_model,
                       adjudicator_model=args.adjudicator_model, reason=args.reason, dry_run=args.dry_run)
    except (RepinError, ValueError, OSError, lifecycle_journal.JournalError) as exc:
        print(f"FATAL: repin_critic_route.py: {exc}", file=sys.stderr)
        return 2
    if result.get("dry_run"):
        print(f"dry run: would re-pin critics {result['previous']['critics']} -> {result['critics']}, adjudicator "
              f"{result['previous']['targeted_adjudicator']} -> {result['targeted_adjudicator']}; every check passed, nothing written")
        return 0
    if not result["changed"]:
        print(f"already pinned to critics={result['critics']} adjudicator={result['targeted_adjudicator']}; nothing to do")
    else:
        print(f"re-pinned: critics {result['previous']['critics']} -> {result['critics']}, adjudicator "
              f"{result['previous']['targeted_adjudicator']} -> {result['targeted_adjudicator']}; "
              f"previous snapshot archived as {result['archived_snapshot']}")
        print("next: re-run the round, e.g.  CRITICS=\"...\" ADJUDICATE=auto bash scripts/run_critique.sh <N>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
