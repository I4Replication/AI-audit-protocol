#!/usr/bin/env python3
"""Canonical metadata-2.1 lifecycle journal and deterministic projections.

``run.events.jsonl`` is the only mutable source of truth.  ``run.json``,
``invocations.jsonl`` and the metrics files are projections carrying the latest
journal sequence/hash watermark.  Provider processes are never launched here;
the small transition API lets runners persist each boundary before continuing.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import time
import math
import re
import os
import pathlib
import sys
import tempfile
import uuid
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

# The post-fan-out verifier's identity (role_class and round) has one definition, shared by every
# script that produces or consumes it. Every importer already puts this directory on sys.path; the
# guard only covers a direct import from elsewhere.
_SCRIPTS_DIR = str(pathlib.Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import verifier_contract  # noqa: E402


SCHEMA_VERSION = "2.1.0"
JOURNAL_NAME = "run.events.jsonl"
RUN_NAME = "run.json"
INVOCATIONS_NAME = "invocations.jsonl"
LOCK_NAME = ".run.events.lock"
HASH_RE = __import__("re").compile(r"^[0-9a-f]{64}$")

ATTEMPT_KINDS = {
    "local_preflight", "capability_probe", "schema_admission", "role_inference",
    "output_repair", "cache_replay",
}
BUDGETED_ATTEMPT_KINDS = {"role_inference", "output_repair"}


def _attempt_ceiling_source() -> str:
    """Where the ceiling in force came from, so run.json says which knob applied."""
    return "I4R_MAX_ATTEMPTS" if os.environ.get("I4R_MAX_ATTEMPTS", "").strip() else "protocol_default"


def max_attempts_per_role() -> int:
    """Accepted inference starts allowed per role and round.

    The protocol default is 3. On the first production audit every exhausted budget was
    consumed by harness defects rather than by the role failing at its task, so the ceiling
    is an operator setting: ``I4R_MAX_ATTEMPTS=<n>`` raises or lowers it for a run. It is
    read at call time so a long-running driver and a later resume agree.
    """
    raw = os.environ.get("I4R_MAX_ATTEMPTS", "").strip()
    if not raw:
        return 3
    try:
        value = int(raw)
    except ValueError as exc:
        raise JournalError(f"I4R_MAX_ATTEMPTS must be an integer, got {raw!r}") from exc
    if value < 1 or value > 20:
        raise JournalError("I4R_MAX_ATTEMPTS must be between 1 and 20")
    return value
PROVIDER_REQUEST_STATUSES = {
    "not_requested", "requested", "accepted", "rejected", "unknown",
}
INFERENCE_START_STATUSES = {"not_started", "started", "unknown"}
ROUND_STATES = (
    "prepared", "awaiting_authorization", "ready", "running",
    "quorum_complete", "integrated",
)


class JournalError(ValueError):
    """The canonical journal or a requested transition is invalid."""


class AttemptBudgetExhausted(JournalError):
    """A role already has three accepted inference starts."""


class AuthorizationRequired(JournalError):
    """A critique/adjudication provider call lacks a matching live grant."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any, field: str = "timestamp") -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise JournalError(f"{field} must be a non-empty RFC 3339 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JournalError(f"{field} is not an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise JournalError(f"{field} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def canonical_bytes(value: Any) -> bytes:
    # allow_nan=False: the journal is read back by a strict RFC 8259 reader; a NaN that got in
    # would make every later load fail, so refuse it at the writer.
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def event_hash(event: dict[str, Any]) -> str:
    return sha256_value({key: value for key, value in event.items() if key != "event_hash"})


def watermark(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {"journal_seq": 0, "journal_hash": None}
    return {"journal_seq": events[-1]["seq"], "journal_hash": events[-1]["event_hash"]}


def _atomic_write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def lock_timeout_seconds() -> float:
    """LOCK_TIMEOUT, the knob log_event.sh has always documented; 60 s when unset or invalid."""
    raw = os.environ.get("LOCK_TIMEOUT", "").strip()
    try:
        value = float(raw) if raw else 60.0
    except ValueError:
        value = 60.0
    return value if value > 0 else 60.0


@contextmanager
def journal_lock(root: pathlib.Path) -> Iterator[None]:
    """Serialize sequence, hash, and attempt allocation with one OS lock.

    An fcntl lock is released by the OS when its holder dies, so there is no stale lock to
    break. What CAN happen is a live holder that stops making progress -- SIGSTOP, a stalled
    write on a synced folder -- and an unbounded LOCK_EX then blocks every later journal write
    forever with no message. Poll instead, and refuse after LOCK_TIMEOUT seconds so the caller
    exits with the documented refusal rather than hanging.
    """
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / LOCK_NAME
    timeout = lock_timeout_seconds()
    with lock_path.open("a+b") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise JournalError(
                        f"journal lock {lock_path} is held by another process and was not "
                        f"released within LOCK_TIMEOUT={timeout:g}s; the holder is alive (an OS "
                        "lock dies with its process) -- find it before retrying"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _strict_object(raw: str, source: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JournalError(f"{source}: invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise JournalError(f"{source}: expected one JSON object")
    return value


def _virtualize_legacy_event(
    value: dict[str, Any], expected_seq: int, previous_hash: str | None,
) -> dict[str, Any]:
    """Read an immutable pre-2.1 prefix without rewriting its historical bytes."""
    event = copy.deepcopy(value)
    if event.get("seq") != expected_seq:
        raise JournalError(
            f"legacy journal sequence is not contiguous: expected {expected_seq}, "
            f"got {event.get('seq')!r}"
        )
    event.setdefault("schema_version", str(event.get("schema_version") or "legacy"))
    event.setdefault("previous_hash", previous_hash)
    event.setdefault("source", None)
    event.setdefault("pid", None)
    event["event_hash"] = event_hash(event)
    return event


def load_events(root: pathlib.Path, *, allow_legacy_prefix: bool = True) -> list[dict[str, Any]]:
    path = root / JOURNAL_NAME
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise JournalError(f"canonical journal is not a regular file: {path}")
    events: list[dict[str, Any]] = []
    previous_hash: str | None = None
    saw_hashed = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise JournalError(f"{path} line {number}: blank lines are not permitted")
        raw = _strict_object(line, f"{path} line {number}")
        is_hashed = raw.get("schema_version") == SCHEMA_VERSION and "event_hash" in raw
        if not is_hashed:
            if saw_hashed or not allow_legacy_prefix:
                raise JournalError(f"{path} line {number}: unchained event after 2.1 chain start")
            event = _virtualize_legacy_event(raw, number, previous_hash)
        else:
            saw_hashed = True
            event = raw
            if set(event) != {
                "schema_version", "seq", "at", "type", "payload", "pid", "source",
                "previous_hash", "event_hash",
            }:
                raise JournalError(f"{path} line {number}: invalid 2.1 event fields")
            if event.get("seq") != number:
                raise JournalError(f"{path} line {number}: seq must equal {number}")
            parse_time(event.get("at"), f"{path} line {number}.at")
            if not isinstance(event.get("type"), str) or not event["type"]:
                raise JournalError(f"{path} line {number}: type must be non-empty")
            if not isinstance(event.get("payload"), dict):
                raise JournalError(f"{path} line {number}: payload must be an object")
            if event.get("previous_hash") != previous_hash:
                raise JournalError(f"{path} line {number}: previous_hash breaks the chain")
            if not HASH_RE.fullmatch(str(event.get("event_hash") or "")):
                raise JournalError(f"{path} line {number}: event_hash is malformed")
            if event_hash(event) != event["event_hash"]:
                raise JournalError(f"{path} line {number}: event_hash mismatch")
        events.append(event)
        previous_hash = event["event_hash"]
    return events


def _new_event(
    seq: int, previous_hash: str | None, event_type: str, payload: dict[str, Any],
    *, at: str | None = None, source: str = "lifecycle_journal.py",
) -> dict[str, Any]:
    timestamp = at or utc_now()
    parse_time(timestamp)
    if not isinstance(event_type, str) or not event_type:
        raise JournalError("event type must be non-empty")
    if not isinstance(payload, dict):
        raise JournalError("event payload must be an object")
    event = {
        "schema_version": SCHEMA_VERSION,
        "seq": seq,
        "at": timestamp,
        "type": event_type,
        "payload": copy.deepcopy(payload),
        "pid": os.getpid(),
        "source": source,
        "previous_hash": previous_hash,
    }
    event["event_hash"] = event_hash(event)
    return event


ROUND_EVENT_TYPES = frozenset({
    "round_prepared", "round_authorization_required", "round_authorized", "round_ready",
    "round_start", "round_quorum_complete", "round_integrated", "round_paused", "round_end",
    "round_result",
})
ROUND_COUNT_EVENTS = frozenset({"round_result", "round_integrated"})
ROUND_COUNT_KEYS = ("blocking_items", "comment_defects", "paper_defects")
# every count run.schema.json types as a non-negative integer; only the first three form the defect split
ROUND_ALL_COUNT_KEYS = ROUND_COUNT_KEYS + ("factual_corrections", "non_blocking_items", "items_accepted",
                                           "items_contested", "unclassified_defects", "wall_clock_seconds")

# 2.6.9: an audit's round cap is frozen in its policy snapshot at init, and a finalize closes the audit. Raising
# the cap (policy_amendment) and continuing a finalized audit (audit_reopened) are tool-only, journaled operator
# acts: scripts/amend_round_cap.py and scripts/reopen_audit.py write them, scripts/round_cap_amendment.py verifies
# them at finalize and publication. The CLI appends refuse both types, and a hand-logged metadata_correction may
# not set the fields they own.
AMEND_EVENT = "policy_amendment"
REOPEN_EVENT = "audit_reopened"
# 2.7.4: the software environment, measured live by scripts/capture_environment.sh and journaled through
# record_environment; the latest one is projected into run.json's environment. Before 2.7.4 the capture wrote
# run.json directly, and the next journal append re-projected run.json without it.
ENVIRONMENT_EVENT = "environment_captured"
TOOL_ONLY_EVENTS = {
    AMEND_EVENT: "python3 scripts/amend_round_cap.py <audit-dir> --to <cap> --reason '<why>'",
    REOPEN_EVENT: "python3 scripts/reopen_audit.py <audit-dir> --reason '<why>' (or amend_round_cap.py --reopen)",
    ENVIRONMENT_EVENT: "bash scripts/capture_environment.sh <audit-dir> (it measures the machine it runs on)",
}
# The type a pre-2.7.4 operator logged by hand (three journals hold one). The projection never read it and never
# will: those audits keep the run.json they have. A new one is refused and pointed at the capture.
LEGACY_ENVIRONMENT_EVENT = "environment"
# 2.7.6: operator text that must not stay in run.json (an e-mail address, a home path, a participant code typed into
# a note) is masked by a later text_redaction event naming the earlier event's seq and one free-text field. The journal
# stays append-only; the projection, and everything read from run.json, carries "[redacted]".
REDACTION_EVENT = "text_redaction"
REDACTABLE_FIELDS = frozenset({"text", "notes", "note", "detail", "description", "recipient"})
TOOL_ONLY_RUN_FIELDS = ("round_cap", "round_target", "policy_amendments", "reopenings", "toolkit")
# Events of a round being RUN. A finalized audit must be reopened before any of them; a re-logged round_result or
# round_integrated (a count or integration-time correction before a re-finalize) is not a new run and stays allowed.
ROUND_RUN_EVENTS = frozenset({
    "round_prepared", "round_authorization_required", "round_authorized", "round_ready",
    "round_start", "round_quorum_complete", "round_paused", "round_end",
})
REVIEW_SCOPE_FIELDS = (
    "changed_claim_ids", "rereviewed_claim_ids", "unresolved_blocking_item_ids",
    "unresolved_conclusion_critical_item_ids", "unreviewed_sections",
)
POLICY_SNAPSHOT_NAME = "pipeline-policy.snapshot.json"


def unreopened_finalize(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last finalize event when no audit_reopened follows it (the audit is closed), else None."""
    closing = None
    for event in events:
        if event.get("type") == "finalize":
            closing = event
        elif event.get("type") == REOPEN_EVENT:
            closing = None
    return closing


def snapshot_round_cap(root: pathlib.Path) -> int | None:
    """critique.round_cap of the audit's frozen policy snapshot; None without a readable snapshot (fixtures)."""
    try:
        envelope = json.loads((pathlib.Path(root) / POLICY_SNAPSHOT_NAME).read_text(encoding="utf-8"))
        cap = envelope["policy"]["critique"]["round_cap"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return cap if isinstance(cap, int) and not isinstance(cap, bool) else None


def shipped_round_cap() -> int | None:
    try:
        policy = json.loads((pathlib.Path(__file__).resolve().parents[1] / "config/pipeline-policy-v2.json")
                            .read_text(encoding="utf-8"))
        cap = policy["critique"]["round_cap"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return cap if isinstance(cap, int) and not isinstance(cap, bool) else None


def initial_run_of(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """project_run's rule: the init event's initial_run, else the first migration anchor that carries one."""
    for event in events:
        candidate = (event.get("payload") or {}).get("initial_run")
        if isinstance(candidate, dict) and (event is events[0] or event.get("type") == "migration_anchor"):
            return candidate
    return None


def journaled_round_cap(events: list[dict[str, Any]], before_seq: int | None = None) -> int | None:
    """The audit's cap as its JOURNAL records it (2.6.9): the cap recorded at init, raised by each
    policy_amendment in turn (only those before journal seq ``before_seq`` when it is given). None when the init
    records no cap (a fixture). The snapshot's critique.round_cap must equal it: a snapshot edited and re-hashed by
    hand changes the file, never the journal."""
    initial = initial_run_of(events)
    cap = initial.get("round_cap") if isinstance(initial, dict) else None
    if isinstance(cap, bool) or not isinstance(cap, int):
        return None
    for event in events:
        if before_seq is not None and not (isinstance(event.get("seq"), int) and event["seq"] < before_seq):
            continue
        payload = event.get("payload") or {}
        if event.get("type") == AMEND_EVENT and payload.get("leaf") == "critique.round_cap" \
                and isinstance(payload.get("to"), int) and not isinstance(payload.get("to"), bool):
            cap = payload["to"]
    return cap


def _snapshot_trust_refusal(root: pathlib.Path) -> str | None:
    """Why the audit's policy snapshot cannot be trusted to give its cap (an invalid envelope, or a live v2 run.json
    that does not bind these exact bytes), else None. A workspace with no snapshot is a fixture: nothing to check."""
    if not (pathlib.Path(root) / POLICY_SNAPSHOT_NAME).exists():
        return None
    scripts = str(pathlib.Path(__file__).resolve().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import record_invocation   # imports this module; loaded lazily
    try:
        record_invocation.load_policy_snapshot(pathlib.Path(root))
        record_invocation.validate_policy_binding(pathlib.Path(root))
    except (OSError, ValueError) as exc:
        return (f"the audit's policy snapshot cannot be trusted for its round cap: {exc}. The snapshot changes only "
                "through scripts/amend_round_cap.py or scripts/repin_critic_route.py; restore the bytes run.json binds "
                "(its superseded archives and the journal name them) before running any round")
    return None


def round_refusal(root: pathlib.Path, events: list[dict[str, Any]], round_number: int, *,
                  check_snapshot: bool = True) -> str | None:
    """Why round ``round_number`` may not be prepared or run now (the audit's cap, or a finalize), else None.

    The cap is the JOURNALED one (journaled_round_cap), which the snapshot must match; with ``check_snapshot`` the
    snapshot's envelope and run.json's binding to it are proven first, so a hand-edited, re-hashed snapshot cap is
    refused here, before a round is prepared, authorized or paid for."""
    if check_snapshot:
        untrusted = _snapshot_trust_refusal(root)
        if untrusted:
            return untrusted
    snapshot_cap = snapshot_round_cap(root)
    journal_cap = journaled_round_cap(events)
    if journal_cap is not None and snapshot_cap is not None and journal_cap != snapshot_cap:
        amended = any(event.get("type") == AMEND_EVENT for event in events)
        return (f"the policy snapshot's round cap is {snapshot_cap}, but the journal records {journal_cap} (the cap at "
                f"init{', raised by each policy_amendment' if amended else ''}): a cap changes only through python3 "
                "scripts/amend_round_cap.py, never by editing pipeline-policy.snapshot.json; restore the snapshot "
                "run.json binds before running any round")
    cap = journal_cap if journal_cap is not None else snapshot_cap
    if cap is not None and round_number > cap:
        shipped = shipped_round_cap()
        if shipped is not None and round_number <= shipped:
            closed = unreopened_finalize(events) is not None
            return (f"round {round_number} exceeds this audit's round cap of {cap} (critique.round_cap in its "
                    f"pipeline-policy.snapshot.json, frozen at init; this skill ships {shipped}). An existing audit "
                    f"is not re-capped silently: raise it with a journaled amendment, python3 "
                    f"scripts/amend_round_cap.py \"$AUDIT_DIR\" --to {shipped} --reason '<why>'"
                    + (" --reopen (the audit is finalized)" if closed else "")
                    + " (INSTALL.md, 'Raising the round cap of an existing audit'); otherwise stop with "
                    "cap_with_unresolved")
        return (f"round {round_number} exceeds this audit's round cap of {cap} (critique.round_cap in its "
                f"pipeline-policy.snapshot.json); stop with cap_with_unresolved, listing what is unresolved")
    closing = unreopened_finalize(events)
    if closing is not None:
        return (f"this audit was finalized (journal seq {closing.get('seq')}, {closing.get('at')}) and has not been "
                f"reopened; a finalized audit runs no further round until a journaled reopen: python3 "
                f"scripts/reopen_audit.py \"$AUDIT_DIR\" --reason '<why>' (or amend_round_cap.py --reopen when the "
                f"cap must also rise). A published audit first moves its published set aside and is claimed again "
                f"(INSTALL.md, 'Correcting a published comment')")
    return None


def _round_result_payload(events: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    """A NEW round_result may not move the round's journaled integration time.

    The projection lets round_result set integrated_at (its payload value, else the event's own
    time), which is right for legacy audits that have no round_integrated event. On an audit that
    does, a round_result logged afterwards -- as the docs instruct -- silently replaced the
    journaled time, and the settled cache, which binds it, then failed in make_diff_packet with
    "Round-1 completion/integration/consistency gate is invalid" (the seventh production audit).
    Only appends are checked, so replaying an existing journal projects exactly as before.
    """
    number = payload.get("round")
    journaled = None
    for event in events:
        if event["type"] == "round_integrated" and (event.get("payload") or {}).get("round") == number:
            journaled = (event.get("payload") or {}).get("integrated_at") or event.get("at")
    if journaled is None:
        return payload
    try:
        parse_time(str(journaled))
    except (JournalError, ValueError):
        # the journaled time itself has no zone (logged before capture checked it): re-journal it first
        raise JournalError(
            f"round {number}'s journaled integration time {journaled!r} has no timezone; re-journal it with "
            f"`bash scripts/log_event.sh round_integrated round={number} integrated_at=<the same time with Z or an "
            f"offset>`, then log this round_result")
    given = payload.get("integrated_at")
    if given is not None and given != journaled:
        raise JournalError(
            f"round_result integrated_at={given} disagrees with round {number}'s journaled integration "
            f"time {journaled} (round_integrated); omit integrated_at or pass integrated_at={journaled}. "
            f"run.json's integrated_at is the time run_critique.sh closed the round's critique (critic quorum "
            f"and any targeted adjudication, journaled even for a final round that is never applied); "
            f"integrate_round.py apply's own time is critique/round_{number}/INTEGRATION_SUMMARY.json "
            f"integrated_at, which round_result does not take (finalize reads it from that file)")
    return {**payload, "integrated_at": journaled}


def environment_payload_problem(payload: Any) -> str | None:
    """Why an environment_captured payload may not be journaled, else None.

    The projection copies payload.environment into run.json as it stands, and finalize validates run.json against
    schemas/run.schema.json, so the value is judged here by that schema's own definition of environment (the rule
    has one statement): a malformed capture in the append-only journal could never be corrected. Every writer
    passes through _append_locked, so none can journal one that finalize would refuse."""
    if not isinstance(payload, dict) or set(payload) != {"environment"}:
        return 'the payload must be exactly {"environment": {...}}'
    environment = payload["environment"]
    if not isinstance(environment, dict):
        return "environment must be an object"
    try:
        parse_time(environment.get("captured_at"), "environment.captured_at")
    except JournalError as exc:
        return str(exc)
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    import validate_json_schema
    schema_path = pathlib.Path(__file__).resolve().parents[1] / "schemas" / "run.schema.json"
    context = validate_json_schema.Context(schema_path)
    definition = validate_json_schema.pointer(context.root.schema, "#/properties/environment")
    errors = validate_json_schema.finite_errors(environment) or validate_json_schema.validate(
        environment, definition, context, context.root, "/environment")
    return "; ".join(errors[:5]) if errors else None


def record_environment(root: pathlib.Path, environment: dict[str, Any]) -> dict[str, Any]:
    """Journal one live capture and project it into run.json.environment: capture_environment.sh's only write.

    Refused on a workspace with no canonical journal (run init_run.sh first) and on a finalized audit that has not
    been reopened: finalization_receipt.py check tolerates only POST_FINALIZATION_EVENTS after the finalize it
    binds, so a capture appended after it would stale the receipt for good."""
    root = pathlib.Path(root).resolve()
    with journal_lock(root):
        events = load_events(root)
        if not events:
            raise JournalError(f"{root / JOURNAL_NAME} is missing or empty: run init_run.sh first")
        closing = unreopened_finalize(events)
        if closing is not None:
            raise JournalError(
                f"this audit was finalized (journal seq {closing.get('seq')}, {closing.get('at')}) and has not been "
                "reopened; an environment appended now would stale its finalization receipt, and it would describe "
                "this machine today, not the one the audit ran on. Nothing was journaled")
        events = _append_locked(root, [{
            "type": ENVIRONMENT_EVENT, "payload": {"environment": environment},
            "source": "capture_environment.sh",
        }])
        project_all_locked(root, events)
        return events[-1]


def _append_locked(root: pathlib.Path, specs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    events = load_events(root)
    previous_hash = events[-1]["event_hash"] if events else None
    additions: list[dict[str, Any]] = []
    for spec in specs:
        payload = dict(spec.get("payload") or {})
        if str(spec["type"]) == "round_result":
            payload = _round_result_payload(events + additions, payload)
        if str(spec["type"]) == ENVIRONMENT_EVENT:
            problem = environment_payload_problem(payload)
            if problem:
                raise JournalError(f"{ENVIRONMENT_EVENT} was not journaled: {problem}")
        event = _new_event(
            len(events) + len(additions) + 1,
            previous_hash,
            str(spec["type"]),
            payload,
            at=spec.get("at"),
            source=str(spec.get("source") or "lifecycle_journal.py"),
        )
        additions.append(event)
        previous_hash = event["event_hash"]
    if additions:
        # Prove that the entire ordered batch can be projected before making
        # any append visible. A malformed middle event must leave the canonical
        # journal byte-exact, just as a malformed terminal row must not consume
        # an invocation attempt.
        #
        # ALL FOUR projections, not two. This proof used to run project_run() only, while
        # project_all_locked went on to run the metrics projection outside any proof. A terminal
        # row that project_run accepted but summarize_invocations.aggregate() could not read
        # (a bare `log_event.sh invocation_terminal invocation_id=x`, or a runner omitting one
        # telemetry key) was therefore fsynced into the append-only journal, after which every
        # append, projection rebuild, recovery and the finalize commit died on the same KeyError.
        # With the missing key immutable there was no sanctioned remedy at all.
        try:
            projected = prove_projectable(events + additions)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise JournalError(f"event batch is not projectable: {exc}") from exc
        _check_round_counts(projected, {
            event["payload"].get("round") for event in additions
            if event["type"] in ROUND_COUNT_EVENTS
            and any(key in event["payload"] for key in ROUND_COUNT_KEYS)
        })
        path = root / JOURNAL_NAME
        with path.open("ab") as handle:
            handle.write(b"".join(canonical_bytes(event) + b"\n" for event in additions))
            handle.flush()
            os.fsync(handle.fileno())
    return events + additions


def prove_projectable(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Run every projection project_all_locked will run, on a candidate event list.

    Returns the projected run. Raises whatever the projections raise, so a caller holding the
    journal lock can refuse the batch BEFORE it is made visible. This must stay in lock-step
    with project_all_locked: a projection that is written but not proven here is a way to
    wedge an audit permanently.
    """
    run = project_run(events)
    rows = run["invocations"]
    try:
        import summarize_invocations
    except ImportError:
        # A standalone schema migration without the metrics module can still prove the two
        # normative projections; project_all_locked skips the derivatives in the same case.
        return run
    require_phase_one = (
        run.get("schema_version") in {"2.0.0", "2.1.0"} and not run.get("normalization")
    )
    metrics = summarize_invocations.aggregate(
        rows, require_phase_one=require_phase_one, events=events, run=run,
    )
    summarize_invocations.markdown(metrics)
    return run


def _round_state_from_events(events: list[dict[str, Any]], number: int) -> str | None:
    state = None
    for event in events:
        payload = event.get("payload") or {}
        if payload.get("round") != number:
            continue
        if event["type"] == "round_prepared":
            state = "prepared"
        elif event["type"] in {"round_authorization_required", "round_paused"}:
            state = "awaiting_authorization"
        elif event["type"] in {"round_authorized", "round_ready"}:
            state = "ready"
        elif event["type"] == "round_start":
            state = "running"
        elif event["type"] == "round_quorum_complete":
            state = "quorum_complete"
        elif event["type"] in {"round_integrated", "round_result"}:
            state = "integrated"
    return state


def _reservation_states(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.get("payload") or {}
        invocation_id = payload.get("invocation_id")
        if not isinstance(invocation_id, str):
            continue
        if event["type"] == "invocation_reserved":
            states[invocation_id] = copy.deepcopy(payload)
            states[invocation_id]["reservation_seq"] = event["seq"]
        elif invocation_id in states and event["type"] == "provider_request":
            states[invocation_id]["provider_request_status"] = payload["status"]
            states[invocation_id]["provider_request_seq"] = event["seq"]
        elif invocation_id in states and event["type"] == "inference_start":
            states[invocation_id]["attempt"] = payload["attempt"]
            # Carry the ceiling out of the raw event so it can be projected. Journalling it was
            # only half the fix: a reader of invocations.jsonl or run.json still could not tell
            # budget exhaustion from completion, which is the whole reason it is recorded.
            for field in ("attempt_ceiling", "attempt_ceiling_source", "attempt_ceiling_applies"):
                if payload.get(field) is not None:
                    states[invocation_id][field] = payload[field]
            states[invocation_id]["inference_start_status"] = "started"
            states[invocation_id]["inference_start_seq"] = event["seq"]
        elif invocation_id in states and event["type"] == "provider_response":
            states[invocation_id]["response_received"] = True
            states[invocation_id]["response_artifact"] = payload.get("artifact")
            states[invocation_id]["response_sha256"] = payload.get("sha256")
            states[invocation_id]["provider_response_seq"] = event["seq"]
    return states


def _started_attempts(
    events: list[dict[str, Any]], role: Any, round_number: Any,
) -> list[int]:
    reservations = _reservation_states(events)
    result = []
    for state in reservations.values():
        if (
            state.get("role") == role
            and state.get("round") == round_number
            and state.get("attempt_kind") in BUDGETED_ATTEMPT_KINDS
            and isinstance(state.get("attempt"), int)
        ):
            result.append(state["attempt"])
    return sorted(result)


def initialize(root: pathlib.Path, initial_run: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a new 2.1 journal.  This never overwrites a non-empty journal."""
    root = root.resolve()
    with journal_lock(root):
        if load_events(root):
            raise JournalError("canonical journal already exists")
        if initial_run is None:
            run_path = root / RUN_NAME
            if not run_path.is_file():
                raise JournalError("run.json is required to initialize the journal")
            initial_run = _strict_object(run_path.read_text(encoding="utf-8"), str(run_path))
        base = copy.deepcopy(initial_run)
        base["schema_version"] = SCHEMA_VERSION
        base["events"] = []
        base["invocations"] = []
        base["journal_watermark"] = {"journal_seq": 0, "journal_hash": None}
        events = _append_locked(root, [{
            "type": "init",
            "payload": {"initial_run": base},
            "at": base.get("started_at") or utc_now(),
            "source": "init_run.sh",
        }])
        project_all_locked(root, events)
        return watermark(events)


def append_event(
    root: pathlib.Path, event_type: str, payload: dict[str, Any], *,
    at: str | None = None, source: str = "lifecycle_journal.py",
) -> dict[str, Any]:
    root = root.resolve()
    with journal_lock(root):
        events = _append_locked(root, [{
            "type": event_type, "payload": payload, "at": at, "source": source,
        }])
        project_all_locked(root, events)
        return events[-1]


def append_events(root: pathlib.Path, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append an ordered event batch and publish every projection once."""
    if not specs:
        raise JournalError("event batch is empty")
    root = root.resolve()
    with journal_lock(root):
        events = _append_locked(root, specs)
        project_all_locked(root, events)
        return events[-len(specs):]


AUTH_BINDING_FIELDS = (
    "purpose", "round_plan_sha256", "provider", "model_route", "data_classes",
    "policy_sha256", "expires_at",
)


def prepare_round(
    root: pathlib.Path, round_number: int, plan: dict[str, Any], *, at: str | None = None,
    supersede: bool = False,
) -> dict[str, Any]:
    """Journal a round plan.

    A round may be re-prepared with ``supersede=True`` while it has not been integrated or
    ended: the new plan becomes the one an authorization binds to, and the earlier plan stays
    in the hash chain. The first production audit lost a fully valid four-critic round because
    a driver fix changed the plan and the only recourse was refusal.
    """
    if not isinstance(round_number, int) or round_number < 1:
        raise JournalError("round number must be a positive integer")
    required = ("purpose", "provider", "model_route", "data_classes", "policy_sha256")
    missing = [key for key in required if key not in plan]
    if missing:
        raise JournalError("round plan missing " + ", ".join(missing))
    if not isinstance(plan["data_classes"], list) or not plan["data_classes"]:
        raise JournalError("round plan data_classes must be a non-empty array")
    if not HASH_RE.fullmatch(str(plan["policy_sha256"])):
        raise JournalError("round plan policy_sha256 is invalid")
    normalized = copy.deepcopy(plan)
    normalized["round_plan_sha256"] = sha256_value(plan)
    root = root.resolve()
    with journal_lock(root):
        events = load_events(root)
        # 2.6.9: the audit's own frozen cap, and no new round on a finalized audit until a journaled reopen
        refusal = round_refusal(root, events, round_number)
        if refusal:
            raise JournalError(refusal)
        already = any(
            event["type"] == "round_prepared"
            and (event.get("payload") or {}).get("round") == round_number
            for event in events
        )
        if already and not supersede:
            raise JournalError(f"round {round_number} is already prepared")
        if already and any(
            event["type"] in {"round_integrated", "round_end"}
            and (event.get("payload") or {}).get("round") == round_number
            for event in events
        ):
            raise JournalError(
                f"round {round_number} is finished and this run's plan differs from the one that ran (its inputs changed "
                f"-- an integration, or a rebuilt packet or settled cache -- or a CRITICS/ADJUDICATE override differs); "
                f"a finished round is not rerun -- a forced rerun needs its original, "
                f"byte-identical inputs. If more review is needed, {next_round_hint(events, round_number, root)}")
        events = _append_locked(root, [{
            "type": "round_prepared",
            "payload": {"round": round_number, "plan": normalized, "supersedes_prior_plan": already},
            "at": at,
        }])
        project_all_locked(root, events)
        return {"round": round_number, "state": "prepared", "plan": normalized, **watermark(events)}


def authorize_round(
    root: pathlib.Path, round_number: int, authorization: dict[str, Any], *,
    at: str | None = None,
) -> dict[str, Any]:
    """Bind a grant to the exact purpose/plan/route/data/policy and expiry."""
    root = root.resolve()
    with journal_lock(root):
        events = load_events(root)
        prepared = [
            event for event in events
            if event["type"] == "round_prepared"
            and (event.get("payload") or {}).get("round") == round_number
        ]
        if not prepared:
            raise JournalError(f"round {round_number} has no prepared plan")
        # The latest prepared plan is the live one; earlier plans it superseded stay in the chain.
        plan = (prepared[-1]["payload"].get("plan") or {})
        missing = [key for key in AUTH_BINDING_FIELDS if key not in authorization]
        if missing:
            raise JournalError("authorization missing " + ", ".join(missing))
        for key in AUTH_BINDING_FIELDS[:-1]:
            if authorization.get(key) != plan.get(key):
                raise JournalError(f"authorization {key} does not match the prepared round plan")
        expires = parse_time(authorization["expires_at"], "authorization.expires_at")
        granted = parse_time(at or utc_now(), "authorization.granted_at")
        if expires <= granted:
            raise JournalError("authorization is already expired")
        grant = copy.deepcopy(authorization)
        grant.setdefault("authorization_id", "auth-" + uuid.uuid4().hex)
        grant["granted_at"] = at or utc_now()
        grant["authorization_sha256"] = sha256_value({
            key: grant[key] for key in (*AUTH_BINDING_FIELDS, "authorization_id", "granted_at")
        })
        events = _append_locked(root, [{
            "type": "round_authorized",
            "payload": {"round": round_number, "authorization": grant},
            "at": grant["granted_at"],
        }])
        project_all_locked(root, events)
        return {"round": round_number, "state": "ready", "authorization": grant, **watermark(events)}


def _live_round_authorization(
    events: list[dict[str, Any]], round_number: int,
) -> dict[str, Any] | None:
    grants = [
        (event.get("payload") or {}).get("authorization")
        for event in events
        if event["type"] == "round_authorized"
        and (event.get("payload") or {}).get("round") == round_number
    ]
    grant = grants[-1] if grants else None
    if not isinstance(grant, dict):
        return None
    if parse_time(grant.get("expires_at"), "authorization.expires_at") <= parse_time(utc_now()):
        return None
    return grant


def next_round_hint(events: list[dict[str, Any]], round_number: int, root: pathlib.Path | None = None) -> str:
    """What to do after a refused rerun of a finished round: run a later round that is prepared but has not
    dispatched, or else prepare the round after the latest one (at the audit's cap: stop, or amend the cap)."""
    def rounds_with(kinds: set) -> set:
        return {int((e.get("payload") or {}).get("round") or 0) for e in events if e.get("type") in kinds}
    prepared = rounds_with({"round_prepared"})
    dispatched = rounds_with({"round_quorum_complete", "round_integrated", "round_end", "round_result"})
    waiting = sorted(n for n in prepared if n > round_number and n not in dispatched)
    if waiting:
        return f"run round {waiting[0]} (it is prepared)"
    # latest round of any kind: older journals have round_start/round_end without round_prepared
    latest = max(prepared | dispatched | rounds_with({"round_start"}) | {round_number})
    cap = journaled_round_cap(events)
    if cap is None and root is not None:
        cap = snapshot_round_cap(root)
    if cap is not None and latest >= cap:
        return (f"round {latest} is this audit's round cap ({cap}): finalize with cap_with_unresolved (listing what is "
                f"unresolved), or raise the cap with scripts/amend_round_cap.py (INSTALL.md, 'Raising the round cap "
                f"of an existing audit')")
    return f"prepare round {latest + 1}"


def _warn_late_phase_start(audit_dir: pathlib.Path, phase: Any, at: Any, batch_end: Any = None) -> None:
    """A start later than that phase's recorded end (also an at-less start: now) gives a negative duration until
    the end is re-logged; say so when it is written, not at finalize."""
    ended = [e.get("at") for e in load_events(pathlib.Path(audit_dir).resolve())
             if e.get("type") == "phase_end" and (e.get("payload") or {}).get("phase") == phase]
    if batch_end:   # the batch re-logs this phase's end: that end is the one that counts
        ended = [batch_end]
    start = str(at) if at else utc_now()
    if ended and parse_time(start) > parse_time(str(ended[-1])):
        print(f"note: phase {phase} was logged as ended at {ended[-1]}, before this start; log phase_end "
              f"phase={phase} again after it (or give the real start)", file=__import__("sys").stderr)


def check_hand_payload(event_type: Any, payload: Any) -> None:
    """Refuse at capture time the hand-logged values finalize would refuse weeks later.

    The journal is append-only, so a malformed gate exit status (the zsh PIPESTATUS mistake leaves
    it empty) or an out-of-enum round character cannot be corrected once written. The constraints
    are read from run.schema.json so this check cannot drift from what finalize validates.
    """
    if event_type in ("round_integrated", "round_result") and isinstance(payload, dict) \
            and payload.get("integrated_at") is not None:
        # finalize orders it against zoned times; a date or a zone-less time used to crash or skip its checks
        try:
            parse_time(str(payload["integrated_at"]))
        except (JournalError, ValueError) as exc:
            raise JournalError(f"{event_type} integrated_at {payload['integrated_at']!r} must be a timestamp "
                               f"with a timezone, e.g. 2026-09-25T03:09:13Z ({exc})") from exc
    if event_type in TOOL_ONLY_EVENTS:
        # 2.6.9: a raised cap or a reopened audit is an operator act with a reason, written by its tool
        raise JournalError(f"{event_type} events are written only by their tool, never logged by hand: "
                           f"{TOOL_ONLY_EVENTS[event_type]}")
    if event_type == LEGACY_ENVIRONMENT_EVENT:
        raise JournalError(f"{LEGACY_ENVIRONMENT_EVENT} is not a journal event run.json reads: the software "
                           f"environment is journaled as {ENVIRONMENT_EVENT}, only by "
                           f"{TOOL_ONLY_EVENTS[ENVIRONMENT_EVENT]}")
    if event_type == "metadata_correction" and isinstance(payload, dict) \
            and isinstance(payload.get("set"), dict) and "environment" in payload["set"]:
        # 2.7.4: the environment is a live measurement; a value typed later would describe another machine state
        raise JournalError("metadata_correction may not set environment by hand: it is journaled only as "
                           f"{ENVIRONMENT_EVENT}, by {TOOL_ONLY_EVENTS[ENVIRONMENT_EVENT]}")
    if event_type == "metadata_correction" and isinstance(payload, dict) \
            and isinstance(payload.get("set"), dict):
        owned = sorted(key for key in payload["set"] if key in TOOL_ONLY_RUN_FIELDS)
        if owned:
            raise JournalError(
                f"metadata_correction may not set {', '.join(owned)} by hand: the round cap and its amendments change "
                "only through scripts/amend_round_cap.py, reopenings through scripts/reopen_audit.py, and the "
                "policy-snapshot binding through repin_critic_route.py or amend_round_cap.py")
    if isinstance(payload, dict) and "at" in payload and not str(payload.get("at") or "").strip():
        # log_event.sh X at=$UNSET would otherwise record "now" silently
        raise JournalError(f"{event_type} at= is empty (an unset variable?); give the event's real time, or leave "
                           "at= out to record the current time")
    if not isinstance(payload, dict):
        return
    schema_path = pathlib.Path(__file__).resolve().parents[1] / "schemas" / "run.schema.json"
    defs = json.loads(schema_path.read_text(encoding="utf-8")).get("$defs", {})
    if event_type == "gate" and "exit_status" in payload:
        value = payload["exit_status"]
        spec = defs.get("gate_result", {}).get("properties", {}).get("exit_status", {})
        low, high = spec.get("minimum", 0), spec.get("maximum", 255)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise JournalError(
                f"gate exit_status must be an integer {low}-{high}, got {value!r}; capture it with "
                "`cmd > log 2>&1; RC=$?` and pass exit_status=$RC (zsh leaves ${PIPESTATUS[0]} empty)"
            )
    if event_type == "round_result" and payload.get("character") is not None:
        allowed = [v for v in defs.get("round", {}).get("properties", {}).get("character", {}).get("enum", [])
                   if v is not None]
        if allowed and payload["character"] not in allowed:
            raise JournalError(f"round_result character must be one of {allowed}, got {payload['character']!r}")
    gar = payload.get("generated_after_round")
    if event_type == "settled" and payload.get("used") in (True, "true") and "generated_after_round" in payload \
            and not (isinstance(gar, int) and not isinstance(gar, bool) and gar == 1):   # finalize tests == 1
        # finalize requires 1 (the round the cache was first generated after); catching it here saves a
        # finalize/publish cycle
        raise JournalError("settled generated_after_round must be 1 (the round the settled cache was first "
                           f"generated after), got {payload.get('generated_after_round')!r}")
    if event_type in ROUND_EVENT_TYPES:
        # Any typed round field the projection would copy: a bad value fails here, not at finalize's schema check.
        properties = defs.get("round", {}).get("properties", {})
        for key, value in payload.items():
            spec = properties.get(key)
            if not isinstance(spec, dict) or key in ROUND_ALL_COUNT_KEYS or key in ("round", "parallelism"):
                continue
            if event_type in ("round_start", "round_quorum_complete") and {"array", "object"} & set(
                    [spec.get("type")] if isinstance(spec.get("type"), str) else spec.get("type") or []):
                continue   # these two events' projection normalises lists (critics_run=a,b)
            problem = _round_field_problem(value, spec)
            if problem:
                raise JournalError(f"{event_type} {key} {problem}, got {value!r}")
    if event_type in ROUND_EVENT_TYPES and "round" in payload:
        number = payload["round"]
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise JournalError(f"{event_type} round must be an integer >= 1, got {number!r}")
    if event_type in ROUND_COUNT_EVENTS:
        for key in ROUND_ALL_COUNT_KEYS:
            value = payload.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise JournalError(f"{event_type} {key} must be an integer >= 0, got {value!r}")
    if event_type in ROUND_EVENT_TYPES:   # round_start and round_quorum_complete carry it too
        parallelism = payload.get("parallelism")
        if parallelism is not None and (isinstance(parallelism, bool) or not isinstance(parallelism, int)
                                        or parallelism < 1):
            raise JournalError(f"{event_type} parallelism must be an integer >= 1, got {parallelism!r}")


_JSON_TYPES = {
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str), "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None, "array": lambda v: isinstance(v, list), "object": lambda v: isinstance(v, dict),
}


def _round_field_problem(value: Any, spec: dict[str, Any]) -> str | None:
    """Type, enum and range of one round field (formats are left to finalize's schema check)."""
    types = spec.get("type")
    types = [types] if isinstance(types, str) else list(types or [])
    if types and not any(_JSON_TYPES.get(name, lambda v: True)(value) for name in types):
        return "must be " + " or ".join(types)
    if "enum" in spec and value not in spec["enum"]:
        return f"must be one of {spec['enum']}"
    if _JSON_TYPES["number"](value):
        if "minimum" in spec and value < spec["minimum"]:
            return f"must be >= {spec['minimum']}"
        if "maximum" in spec and value > spec["maximum"]:
            return f"must be <= {spec['maximum']}"
    return None


def _check_round_counts(run: dict[str, Any], rounds: set[Any]) -> None:
    """comment_defects + paper_defects classify the blocking items, so they cannot exceed them.

    Checked on the projected rows, under the journal lock, after the whole candidate batch is
    applied: a split across two events, two lines of one batch, or two concurrent writers is seen
    exactly as finalize will see it.
    """
    for row in run.get("rounds") or []:
        if not isinstance(row, dict) or row.get("round") not in rounds:
            continue
        counts = {key: row.get(key) for key in ROUND_COUNT_KEYS}
        if all(isinstance(value, int) and not isinstance(value, bool) for value in counts.values()) \
                and counts["comment_defects"] + counts["paper_defects"] > counts["blocking_items"]:
            raise JournalError(
                f"round {row.get('round')}: comment_defects + paper_defects classify the BLOCKING items, so "
                f"they cannot exceed blocking_items (got {counts['comment_defects']} + "
                f"{counts['paper_defects']} > {counts['blocking_items']}); count non-blocking defects in "
                "neither field")


def parse_kv(values: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for item in values:
        if "=" not in item:
            raise JournalError(f"expected key=value, got {item!r}")
        key, raw = item.split("=", 1)
        raw_json = key.endswith(":")
        if raw_json:
            key = key[:-1]
        if not key:
            raise JournalError("event payload key cannot be empty")
        if raw_json:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise JournalError(f"{key}: invalid JSON value") from exc
        elif raw in {"", "null"}:
            value = None
        elif raw == "true":
            value = True
        elif raw == "false":
            value = False
        else:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
                else:
                    # float() accepts nan/inf/Infinity. json.dumps would then write a bare NaN
                    # into the append-only journal, which strict_loads refuses and jq reads as
                    # null. A non-finite token stays the string it was typed as.
                    if not math.isfinite(value):
                        value = raw
        payload[key] = value
    return payload


VERIFIER_ROUND_ERROR = "a verifier invocation must be recorded at round 0"


def _refuse_verifier_off_round_zero(role_class: Any, round_number: Any) -> None:
    """Refuse a post-fan-out verifier identity at any round but 0, before anything is appended.

    A reservation at round > 0 enters the critique-round authorization gate: with no live grant
    it appends round_authorization_required, pauses the run and leaves a phantom round row in
    awaiting_authorization; with a grant on an integrated round it reopens that round. role_class
    and round are immutable once journaled, so a verifier written at the wrong round could never
    be corrected either. The check keys on role_class only (never on a role name): legacy rows
    named operator_verifier / auditor_operator_verification carry role_class other / auditor.
    """
    if role_class != verifier_contract.ROLE_CLASS:
        return
    # bool is an int subclass: round=False must not pass as round 0.
    if type(round_number) is not int or round_number != verifier_contract.ROUND:
        raise JournalError(VERIFIER_ROUND_ERROR)


# Every event that writes, moves or corrects an invocation's lifecycle. A raw CLI append of any of
# these for a verifier call is refused (_refuse_raw_verifier_events).
RAW_LIFECYCLE_TYPES = frozenset({
    "invocation_reserved", "provider_request", "inference_start", "provider_response",
    "invocation_terminal", "cache_reuse", "invocation_correction",
})
RAW_VERIFIER_ERROR = (
    "a post-fan-out verifier invocation is journaled only by scripts/run_verifier.py, through the "
    "lifecycle API; a lifecycle event for a role_class verifier call from the command line "
    "(lifecycle_journal.py append-event/append-kv/append-batch, log_event.sh, or "
    "record_invocation.py) is refused. To supersede a recorded verifier call, use "
    "record_invocation.py --correct <audit> <invocation_id> <correction.json>")
VERIFIER_UNRESERVED_ERROR = (
    "a verifier provider call must be reserved before its terminal record: scripts/run_verifier.py "
    "journals it through reserve, provider request, inference start and response")


def _event_parts(payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = payload if isinstance(payload, dict) else {}
    record = payload.get("record")
    return payload, (record if isinstance(record, dict) else {})


def _verifier_invocation_ids(events: list[dict[str, Any]]) -> set[str]:
    """Invocation ids the journal already records as role_class verifier.

    A projected row takes its role_class from its terminal record, else from its reservation
    (project_invocations), and role_class is immutable under correction, so a raw scan of the
    reservation and terminal events is the projection's answer without running a projection (and
    without any new way for an append on a damaged journal to fail).
    """
    ids: set[str] = set()
    for event in events:
        if event.get("type") not in RAW_LIFECYCLE_TYPES:
            continue
        payload, record = _event_parts(event.get("payload"))
        if verifier_contract.ROLE_CLASS in (payload.get("role_class"), record.get("role_class")):
            ids.update(str(value) for value in (payload.get("invocation_id"), record.get("invocation_id"))
                       if value is not None)
    return ids


def _refuse_raw_verifier_events(events: list[dict[str, Any]], specs: Iterable[dict[str, Any]]) -> None:
    """Refuse a CLI lifecycle event that names a verifier call, before anything is appended.

    Records-1 (2.6.4 review): four raw events through ``append-batch`` (reservation, request,
    start, terminal) made a verifier row no runner ever produced. The CLI appends (and
    log_event.sh, which calls them) therefore never carry a verifier lifecycle: an event of a
    RAW_LIFECYCLE_TYPES type is refused when it names role_class verifier in its payload or in
    payload.record, or when its invocation (or the call it replays or repairs) is already
    reserved or recorded as a verifier. Keyed on role_class only, never on a role name, so an
    audit with no verifier row appends exactly as before. The Python lifecycle API the runner
    uses, and record_invocation.py --correct, are not affected.
    """
    specs = [spec for spec in specs if isinstance(spec, dict)
             and str(spec.get("type")) in RAW_LIFECYCLE_TYPES]
    if not specs:
        return
    known = _verifier_invocation_ids(events)
    for spec in specs:
        payload, record = _event_parts(spec.get("payload"))
        if verifier_contract.ROLE_CLASS in (payload.get("role_class"), record.get("role_class")):
            raise JournalError(RAW_VERIFIER_ERROR)
        named = {str(value) for value in (
            payload.get("invocation_id"), record.get("invocation_id"),
            payload.get("source_invocation_id"), record.get("source_invocation_id"),
        ) if value is not None}
        if named & known:
            raise JournalError(RAW_VERIFIER_ERROR)


def refuse_cli_verifier_event(root: pathlib.Path, event_type: str, payload: dict[str, Any]) -> None:
    """The same refusal for one lifecycle event a command line is about to journal through the
    Python API (record_invocation.py --reserve, --provider-request, ...).

    It raises nothing but the verifier refusal: a journal it cannot read (a legacy v2.0 ledger
    audit, a damaged file) is left to the operation itself, which then fails or succeeds exactly
    as it did before this guard; the event's own role_class is still judged."""
    try:
        events = load_events(pathlib.Path(root).resolve())
    except (JournalError, OSError, UnicodeError):
        events = []
    _refuse_raw_verifier_events(events, [{"type": event_type, "payload": payload}])


def append_cli_events(root: pathlib.Path, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The CLI's append (append-event, append-kv, append-batch): append_events, with the raw
    verifier lifecycle refused under the same journal lock as the append itself."""
    if not specs:
        raise JournalError("event batch is empty")
    root = root.resolve()
    _refuse_unknown_types(specs)
    _refuse_private_operator_text(specs)
    with journal_lock(root):
        current = load_events(root)
        _check_redactions(current, specs)
        _refuse_raw_verifier_events(current, specs)
        _refuse_hand_logged_rounds(root, current, specs)
        events = _append_locked(root, specs)
        project_all_locked(root, events)
        return events[-len(specs):]


def _refuse_unknown_types(specs: Iterable[dict[str, Any]]) -> None:
    """2.7.6: an event type outside schemas/v2/lifecycle-event.schema.json's enum is refused before anything is
    written. The projection copies every event into run.json, finalize validates run.json against run.schema.json's
    closed enum, and the journal is append-only, so one typo ('phase_ended') used to make finalize impossible."""
    schema = json.loads((pathlib.Path(_SCRIPTS_DIR).parent / "schemas/v2/lifecycle-event.schema.json")
                        .read_text(encoding="utf-8"))
    known = set(schema["properties"]["type"]["enum"])
    for spec in specs:
        kind = spec.get("type")
        if kind not in known:
            raise JournalError(f"unknown event type {kind!r}: the journal accepts only "
                               f"schemas/v2/lifecycle-event.schema.json's types (log_event.sh lists them)")


def _strings(value: Any, where: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{where}[{index}]")


def _refuse_private_operator_text(specs: Iterable[dict[str, Any]]) -> None:
    """2.7.6: run.json projects every journaled event, the coordinator scans all of run.json before any call, and
    publication scans it again, while the journal is append-only. So text the operator logs is refused here when it
    holds what those scans refuse: a private absolute path, a participant-code-shaped token (the coordinator's own
    rule) or an e-mail address (the release privacy gate's rule; example.* domains pass)."""
    import check_release_privacy as release_privacy
    import email_scan
    import run_coordinator
    for spec in specs:
        for where, text in _strings(spec.get("payload") or {}, f"{spec.get('type')} payload"):
            try:
                run_coordinator.validate_provider_visible_string(text, where)
            except run_coordinator.CoordinatorError as exc:
                raise JournalError(f"{exc}. Logged text is provider-visible and published with run.json: describe it "
                                   "without the path or the identifier") from None
            for match in email_scan.finditer(release_privacy._EMAIL, text):
                if not release_privacy._EXAMPLE_EMAIL_DOMAIN.search(match.group(0)):
                    raise JournalError(f"{where} contains an e-mail address; logged text is provider-visible and "
                                       "published with run.json (name the recipient, e.g. 'I4R editorial queue')")


def _check_redactions(events: list[dict[str, Any]], specs: Iterable[dict[str, Any]]) -> None:
    """2.7.6: a text_redaction names an earlier event's seq and one of its free-text fields that holds a string."""
    by_seq = {event.get("seq"): event for event in events}
    for spec in specs:
        if spec.get("type") != REDACTION_EVENT:
            continue
        payload = spec.get("payload") or {}
        try:
            seq = int(payload.get("seq"))
        except (TypeError, ValueError):
            raise JournalError("text_redaction needs seq=<the journal seq of the event to mask>") from None
        field = payload.get("field")
        target = by_seq.get(seq)
        if target is None:
            raise JournalError(f"text_redaction: no journal event has seq {seq}")
        if field not in REDACTABLE_FIELDS:
            raise JournalError(f"text_redaction: field must be one of {sorted(REDACTABLE_FIELDS)}, not {field!r}")
        if not isinstance((target.get("payload") or {}).get(field), str):
            raise JournalError(f"text_redaction: event seq {seq} ({target.get('type')}) has no text field {field!r}")
        if not str(payload.get("reason") or "").strip():
            raise JournalError("text_redaction needs reason=<why the text is masked>")


def _refuse_hand_logged_rounds(root: pathlib.Path, events: list[dict[str, Any]],
                               specs: Iterable[dict[str, Any]]) -> None:
    """2.6.9: a hand-logged event of a round being RUN (ROUND_RUN_EVENTS) obeys the rule prepare_round obeys: no
    round above the audit's journaled cap, and none on a finalized audit before a journaled reopen. Judged in batch
    order, each line against the journal plus the lines before it. A re-logged round_result or round_integrated is
    not a new run and stays allowed."""
    seen = list(events)
    for spec in specs:
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        number = payload.get("round")
        if spec.get("type") in ROUND_RUN_EVENTS and isinstance(number, int) and not isinstance(number, bool):
            refusal = round_refusal(root, seen, number, check_snapshot=False)
            if refusal:
                raise JournalError(f"{spec.get('type')} round={number} is refused: {refusal}")
        seen.append({"type": spec.get("type"), "payload": payload, "seq": len(seen) + 1})


def reserve_invocation(root: pathlib.Path, reservation: dict[str, Any]) -> dict[str, Any]:
    """Persist an immutable invocation identity without consuming an attempt."""
    root = root.resolve()
    attempt_kind = reservation.get("attempt_kind")
    if attempt_kind not in ATTEMPT_KINDS:
        raise JournalError(f"unsupported attempt_kind: {attempt_kind!r}")
    schema_digest = reservation.get("schema_digest")
    if attempt_kind == "schema_admission" and schema_digest is None:
        raise JournalError("schema_admission reservation requires schema_digest")
    if schema_digest is not None and not HASH_RE.fullmatch(str(schema_digest)):
        raise JournalError("schema_digest must be a SHA-256 hex digest")
    required = ("role", "role_class", "round", "provider", "requested_model", "reasoning_effort")
    missing = [key for key in required if key not in reservation]
    if missing:
        raise JournalError("reservation missing " + ", ".join(missing))
    _refuse_verifier_off_round_zero(reservation.get("role_class"), reservation.get("round"))
    with journal_lock(root):
        events = load_events(root)
        round_number = reservation.get("round")
        reopen: list[dict[str, Any]] = []
        if isinstance(round_number, int) and round_number > 0:
            state = _round_state_from_events(events, round_number)
            grant = _live_round_authorization(events, round_number)
            if state in {"quorum_complete", "integrated"} and grant is not None:
                # A dispatch into a round that has already reached quorum is one of two
                # legitimate things: the adjudicator retry the driver itself tells the operator to
                # perform after `quorum_complete_adjudication_failed`, or an explicit forced rerun
                # of an integrated round. The grant is live, so the plan it authorized still
                # covers the call. Refusing here -- as this function did -- appended
                # round_authorization_required, regressed the round to awaiting_authorization and
                # the run to paused, and made both printed remedies dispatch nothing while
                # debiting the budget each time. Reopen the round in the same locked append.
                reopen.append({
                    "type": "round_start",
                    "payload": {"round": round_number, "reopened_from": state},
                })
            elif state not in {"ready", "running"} or grant is None:
                specs = [{
                    "type": "round_authorization_required",
                    "payload": {"round": round_number, "reason": "missing_authorization"},
                }]
                events = _append_locked(root, specs)
                project_all_locked(root, events)
                raise AuthorizationRequired(
                    f"round {round_number} is {state or 'unprepared'}; provider dispatch is paused"
                )
        invocation_id = reservation.get("invocation_id")
        if invocation_id is None:
            invocation_id = "inv-" + uuid.uuid4().hex
        if not isinstance(invocation_id, str) or not invocation_id:
            raise JournalError("invocation_id must be a non-empty string")
        if invocation_id in _reservation_states(events):
            raise JournalError(f"duplicate invocation_id: {invocation_id}")
        payload = copy.deepcopy(reservation)
        payload.update({
            "invocation_id": invocation_id,
            "attempt": None,
            "provider_request_status": "not_requested",
            "inference_start_status": "not_started",
        })
        events = _append_locked(root, [*reopen, {"type": "invocation_reserved", "payload": payload}])
        project_all_locked(root, events)
        candidate = len(_started_attempts(events, payload["role"], payload["round"])) + 1
        return {
            "invocation_id": invocation_id,
            "attempt": None,
            "attempt_candidate": candidate,
            **watermark(events),
        }


def mark_provider_request(
    root: pathlib.Path, invocation_id: str, *, status: str = "requested",
    at: str | None = None,
) -> dict[str, Any]:
    if status not in PROVIDER_REQUEST_STATUSES - {"not_requested"}:
        raise JournalError(f"invalid provider request status: {status}")
    root = root.resolve()
    with journal_lock(root):
        events = load_events(root)
        state = _reservation_states(events).get(invocation_id)
        if state is None:
            raise JournalError(f"unknown invocation_id: {invocation_id}")
        current = state.get("provider_request_status", "not_requested")
        allowed = {
            "not_requested": {"requested", "accepted", "rejected", "unknown"},
            "requested": {"accepted", "rejected", "unknown"},
        }
        if status not in allowed.get(current, set()):
            raise JournalError(f"provider request transition {current}->{status} is invalid")
        if state.get("schema_digest"):
            reservations = _reservation_states(events)
            admissions = sum(
                candidate_id != invocation_id
                and candidate.get("provider_request_status") == "rejected"
                and candidate.get("role") == state.get("role")
                and candidate.get("round") == state.get("round")
                and candidate.get("schema_digest") == state.get("schema_digest")
                for candidate_id, candidate in reservations.items()
            )
            if admissions >= 2:
                raise JournalError(
                    "two rejected schema-admission requests exhausted for this role/schema digest"
                )
        specs = []
        round_number = state.get("round")
        if isinstance(round_number, int) and round_number > 0:
            round_state = _round_state_from_events(events, round_number)
            if round_state == "ready":
                if _live_round_authorization(events, round_number) is None:
                    raise AuthorizationRequired(f"round {round_number} authorization is absent or expired")
                specs.append({
                    "type": "round_start",
                    "payload": {"round": round_number, "first_invocation_id": invocation_id},
                    "at": at,
                })
            elif round_state != "running":
                raise JournalError(f"round {round_number} cannot dispatch from state {round_state!r}")
        specs.append({
            "type": "provider_request",
            "payload": {"invocation_id": invocation_id, "status": status},
            "at": at,
        })
        events = _append_locked(root, specs)
        project_all_locked(root, events)
        return {
            "invocation_id": invocation_id,
            "provider_request_status": status,
            **watermark(events),
        }


def mark_inference_start(
    root: pathlib.Path, invocation_id: str, *, at: str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    with journal_lock(root):
        events = load_events(root)
        state = _reservation_states(events).get(invocation_id)
        if state is None:
            raise JournalError(f"unknown invocation_id: {invocation_id}")
        if state.get("inference_start_status") == "started":
            raise JournalError(f"inference already started: {invocation_id}")
        if state.get("provider_request_status") == "rejected":
            raise JournalError("provider-rejected request cannot start inference")
        attempts = _started_attempts(events, state.get("role"), state.get("round"))
        limit = max_attempts_per_role()
        if state.get("attempt_kind") in BUDGETED_ATTEMPT_KINDS and len(attempts) >= limit:
            raise AttemptBudgetExhausted(
                f"{limit} accepted inference starts exhausted for {state.get('role')} "
                f"round {state.get('round')} (raise with MAX_TRIES inside a critique round, "
                f"which run_critique.sh exports as I4R_MAX_ATTEMPTS; use I4R_MAX_ATTEMPTS "
                f"directly for Phase 1 and the coordinator)"
            )
        attempt = len(attempts) + 1 if state.get("attempt_kind") in BUDGETED_ATTEMPT_KINDS else 1
        specs = []
        if state.get("provider_request_status") in {"not_requested", "requested"}:
            specs.append({
                "type": "provider_request",
                "payload": {"invocation_id": invocation_id, "status": "accepted"},
                "at": at,
            })
        specs.append({
            "type": "inference_start",
            # The ceiling in force is journaled with the attempt it governs. Without it a
            # reader cannot tell whether a role that stopped at N attempts hit its budget or
            # simply finished, and the knob is read from the environment at call time.
            "payload": {
                "invocation_id": invocation_id, "attempt": attempt,
                "attempt_ceiling": limit,
                "attempt_ceiling_source": _attempt_ceiling_source(),
                "attempt_ceiling_applies": state.get("attempt_kind") in BUDGETED_ATTEMPT_KINDS,
            },
            "at": at,
        })
        events = _append_locked(root, specs)
        project_all_locked(root, events)
        return {
            "invocation_id": invocation_id,
            "attempt": attempt,
            "inference_start_status": "started",
            **watermark(events),
        }


def mark_response_received(
    root: pathlib.Path, invocation_id: str, *, artifact: str, sha256: str,
    at: str | None = None,
) -> dict[str, Any]:
    """Bind a received response before parsing/validation can crash."""
    if not isinstance(artifact, str) or not artifact or artifact.startswith("/") or ".." in pathlib.PurePosixPath(artifact).parts:
        raise JournalError("provider response artifact must be a safe relative path")
    if not HASH_RE.fullmatch(str(sha256)):
        raise JournalError("provider response sha256 is invalid")
    root = root.resolve()
    with journal_lock(root):
        events = load_events(root)
        state = _reservation_states(events).get(invocation_id)
        if state is None or state.get("inference_start_status") != "started":
            raise JournalError("provider response requires a recorded inference start")
        if state.get("response_received"):
            raise JournalError("provider response is already recorded")
        events = _append_locked(root, [{
            "type": "provider_response",
            "payload": {"invocation_id": invocation_id, "artifact": artifact, "sha256": sha256},
            "at": at,
        }])
        project_all_locked(root, events)
        return {
            "invocation_id": invocation_id, "response_received": True,
            "artifact": artifact, "sha256": sha256, **watermark(events),
        }


def _terminal_ids(events: list[dict[str, Any]]) -> set[str]:
    return {
        str((event.get("payload") or {}).get("invocation_id"))
        for event in events
        if event["type"] in {"invocation_terminal", "cache_reuse"}
    }


def record_terminal(
    root: pathlib.Path, record: dict[str, Any], *, synthesize_lifecycle: bool = True,
    terminal_event_type: str = "invocation_terminal",
) -> dict[str, Any]:
    """Append one terminal result and regenerate projections.

    ``synthesize_lifecycle`` keeps existing runners safe: a completed legacy-style row
    becomes reservation/request/start/terminal events in one locked append.  New runners
    call the explicit transitions and this function only appends the terminal event.
    """
    root = root.resolve()
    if terminal_event_type not in {"invocation_terminal", "cache_reuse"}:
        raise JournalError(f"invalid terminal event type: {terminal_event_type}")
    invocation_id = record.get("invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id:
        raise JournalError("terminal record needs invocation_id")
    with journal_lock(root):
        events = load_events(root)
        if invocation_id in _terminal_ids(events):
            raise JournalError(f"duplicate terminal invocation: {invocation_id}")
        states = _reservation_states(events)
        specs: list[dict[str, Any]] = []
        if invocation_id in states:
            # The projected row takes the terminal record's value and falls back to the
            # reservation's (project_invocations), so judge the identity the row will carry.
            state = states[invocation_id]
            _refuse_verifier_off_round_zero(
                record.get("role_class") if record.get("role_class") is not None
                else state.get("role_class"),
                record.get("round") if record.get("round") is not None else state.get("round"),
            )
        if invocation_id not in states:
            if not synthesize_lifecycle:
                raise JournalError(f"invocation was not reserved: {invocation_id}")
            _refuse_verifier_off_round_zero(record.get("role_class"), record.get("round"))
            # Synthesizing reservation/request/start for a verifier PROVIDER call would make a
            # lifecycle the runner never journaled (records-1). A cache replay makes no provider
            # call and is synthesized here by design (run_phase1's replay path), so it stays.
            if verifier_contract.is_verifier(record) and record.get("provider_call"):
                raise JournalError(VERIFIER_UNRESERVED_ERROR)
            reservation = {
                key: copy.deepcopy(record.get(key))
                for key in (
                    "invocation_id", "role", "role_class", "round", "attempt_kind",
                    "provider", "requested_model", "reasoning_effort",
                    "input_artifact_sha256", "prompt_sha256", "schema_digest",
                )
            }
            reservation["attempt_kind"] = reservation.get("attempt_kind") or (
                "cache_replay" if not record.get("provider_call") else "role_inference"
            )
            reservation.update({
                "attempt": None,
                "provider_request_status": "not_requested",
                "inference_start_status": "not_started",
            })
            specs.append({"type": "invocation_reserved", "payload": reservation})
            states[invocation_id] = reservation
        state = states[invocation_id]
        provider_status = record.get("provider_request_status")
        if provider_status is None:
            provider_status = "accepted" if record.get("provider_call") else "not_requested"
        inference_status = record.get("inference_start_status")
        if inference_status is None:
            inference_status = "started" if record.get("provider_call") else "not_started"
        if state.get("provider_request_status") == "not_requested" and provider_status != "not_requested":
            specs.append({
                "type": "provider_request",
                "payload": {"invocation_id": invocation_id, "status": provider_status},
            })
            state["provider_request_status"] = provider_status
        if state.get("inference_start_status") != "started" and inference_status == "started":
            attempts = _started_attempts(events, state.get("role"), state.get("round"))
            limit = max_attempts_per_role()
            if state.get("attempt_kind") in BUDGETED_ATTEMPT_KINDS and len(attempts) >= limit:
                raise AttemptBudgetExhausted(f"{limit} accepted inference starts exhausted")
            attempt = len(attempts) + 1 if state.get("attempt_kind") in BUDGETED_ATTEMPT_KINDS else 1
            supplied = record.get("attempt")
            if supplied is not None and supplied != attempt:
                raise JournalError(f"terminal attempt {supplied} disagrees with allocated attempt {attempt}")
            specs.append({
                "type": "inference_start",
                "payload": {
                    "invocation_id": invocation_id, "attempt": attempt,
                    "attempt_ceiling": limit,
                    "attempt_ceiling_source": _attempt_ceiling_source(),
                    "attempt_ceiling_applies": state.get("attempt_kind") in BUDGETED_ATTEMPT_KINDS,
                },
                "at": record.get("started_at"),
            })
            state["attempt"] = attempt
            state["inference_start_status"] = "started"
        merged = copy.deepcopy(record)
        merged.setdefault("attempt_kind", state.get("attempt_kind"))
        merged.setdefault("schema_digest", state.get("schema_digest"))
        merged["attempt"] = state.get("attempt") if inference_status == "started" else None
        merged.setdefault("provider_request_status", provider_status)
        merged.setdefault("inference_start_status", inference_status)
        merged.setdefault("source_invocation_id", None)
        merged.setdefault("turn_count", None)
        merged.setdefault("provider_cost", None)
        merged.setdefault("served_model", merged.get("observed_model"))
        if "identity_assurance" not in merged:
            merged["identity_assurance"] = (
                "observed" if merged.get("served_model") else
                "requested_only" if merged.get("requested_model") else "unavailable"
            )
        merged.setdefault("model_identity_evidence", None)
        merged.setdefault("usage_by_model", [])
        merged.setdefault(
            "unattributed_usage",
            None if merged.get("usage_by_model") else copy.deepcopy(merged.get("usage")),
        )
        merged.setdefault("provider_duration_seconds", merged.get("elapsed_seconds"))
        if merged.get("attempt_kind") == "cache_replay" and merged.get("source_invocation_id") not in _terminal_ids(events):
            raise JournalError("cache replay source_invocation_id is not a terminal canonical invocation")
        specs.append({
            "type": terminal_event_type, "payload": {
                "invocation_id": invocation_id, "record": merged,
            },
            "at": merged.get("ended_at"),
        })
        events = _append_locked(root, specs)
        project_all_locked(root, events)
        return {"record": merged, **watermark(events)}


def record_cache_reuse(root: pathlib.Path, record: dict[str, Any]) -> dict[str, Any]:
    record = copy.deepcopy(record)
    record.update({
        "attempt_kind": "cache_replay", "attempt": None, "provider_call": False,
        "provider_request_status": "not_requested", "inference_start_status": "not_started",
    })
    if not record.get("source_invocation_id"):
        raise JournalError("cache reuse requires source_invocation_id")
    return record_terminal(root, record, terminal_event_type="cache_reuse")


IMMUTABLE_INVOCATION_FIELDS = {
    "invocation_id", "role", "role_class", "round", "attempt_kind", "attempt",
    "provider", "provider_call", "provider_request_status", "inference_start_status",
    "source_invocation_id", "started_at", "ended_at", "input_artifact_sha256",
    "prompt_sha256",
}


def correct_invocation(
    root: pathlib.Path, invocation_id: str, changes: dict[str, Any], *, reason: str,
    at: str | None = None,
) -> dict[str, Any]:
    """Append a transparent correction to a terminal invocation projection.

    Lifecycle identity and attempt accounting are immutable.  Telemetry, model
    identity evidence, and output bindings may be corrected forward when the
    reason is recorded; the old terminal bytes remain in the hash chain.
    """
    if not isinstance(invocation_id, str) or not invocation_id:
        raise JournalError("invocation correction needs a non-empty invocation_id")
    if not isinstance(changes, dict) or not changes:
        raise JournalError("invocation correction set must be a non-empty object")
    forbidden = sorted(IMMUTABLE_INVOCATION_FIELDS.intersection(changes))
    if forbidden:
        raise JournalError(
            "invocation correction cannot change immutable fields: " + ", ".join(forbidden)
        )
    if not isinstance(reason, str) or not reason.strip():
        raise JournalError("invocation correction reason must be non-empty")
    root = root.resolve()
    with journal_lock(root):
        events = load_events(root)
        if invocation_id not in _terminal_ids(events):
            raise JournalError(f"cannot correct non-terminal invocation: {invocation_id}")
        events = _append_locked(root, [{
            "type": "invocation_correction",
            "payload": {
                "invocation_id": invocation_id,
                "set": copy.deepcopy(changes),
                "reason": reason.strip(),
            },
            "at": at,
        }])
        project_all_locked(root, events)
        return {
            "invocation_id": invocation_id,
            "corrected_fields": sorted(changes),
            **watermark(events),
        }


def _empty_round(number: int) -> dict[str, Any]:
    return {
        "round": number, "state": None, "prepared_at": None,
        "authorization": None, "authorization_wait_started_at": None,
        "authorization_wait_ended_at": None, "started_at": None, "ended_at": None,
        "wall_clock_seconds": None, "critics_run": [], "critics_failed": [],
        "parallelism": None, "report_version_reviewed": None, "blocking_items": None,
        "non_blocking_items": None, "comment_defects": None, "paper_defects": None,
        "unclassified_defects": None, "items_accepted": None, "items_contested": None,
        "integrated_at": None, "integrated_before_delivery": None,
        "consistency_check_run": None, "consistency_check_exit": None,
        "character": None, "factual_corrections": None, "notes": None,
    }


def _ensure_round(run: dict[str, Any], number: int) -> dict[str, Any]:
    rounds = run.setdefault("rounds", [])
    for row in rounds:
        if row.get("round") == number:
            for key, value in _empty_round(number).items():
                row.setdefault(key, copy.deepcopy(value))
            return row
    row = _empty_round(number)
    rounds.append(row)
    rounds.sort(key=lambda value: value.get("round", 0))
    return row


def _seconds(start: Any, end: Any) -> float | None:
    if not start or not end:
        return None
    return max(0.0, (parse_time(end) - parse_time(start)).total_seconds())


def _critic_names(value: Any) -> list[Any]:
    """run.json names critics as critic_<role>; the critique runtime journals bare role names in
    round_quorum_complete. Normalize at projection so both vocabularies project identically."""
    return [name if not isinstance(name, str) or name.startswith("critic_") else f"critic_{name}"
            for name in _listify(value)]


def _listify(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [part for part in re.split(r"[,\s]+", value) if part]
    return [value]


def _apply_event(run: dict[str, Any], event: dict[str, Any]) -> None:
    kind = event["type"]
    payload = event.get("payload") or {}
    at = event["at"]
    if kind == "phase_start":
        phase = run.setdefault("phases", {}).setdefault(payload["phase"], {"ran": False})
        phase["started_at"] = at
        if payload.get("notes") is not None:
            phase["notes"] = payload["notes"]
        run["status"] = "running"
    elif kind == "phase_end":
        phase = run.setdefault("phases", {}).setdefault(payload["phase"], {"ran": False})
        phase["ended_at"] = at
        phase["ran"] = phase.get("started_at") is not None
        duration = _seconds(phase.get("started_at"), at)
        # Derived whole seconds, minimum 0, as run.schema.json states. (The legacy jq path once
        # substituted null for anything under two seconds; that contradicted the schema and
        # misfired on every genuinely short phase, and is gone.)
        phase["wall_clock_seconds"] = int(duration) if duration is not None else None
        if payload.get("notes") is not None:
            phase["notes"] = payload["notes"]
    elif kind in {"round_prepared", "round_authorization_required", "round_authorized",
                  "round_ready", "round_start", "round_quorum_complete", "round_integrated",
                  "round_paused", "round_end", "round_result"}:
        number = payload.get("round")
        if not isinstance(number, int):
            return
        row = _ensure_round(run, number)
        if kind == "round_prepared":
            row.update({"state": "prepared", "prepared_at": at, "plan": payload.get("plan")})
            if payload.get("corrects_false_start") is True:
                row.update({
                    "started_at": None, "ended_at": None, "wall_clock_seconds": None,
                    "integrated_at": None,
                })
        elif kind in {"round_authorization_required", "round_paused"}:
            row.update({"state": "awaiting_authorization", "authorization_wait_started_at": at})
            run.update({"status": "paused", "pause_reason": "awaiting_authorization"})
        elif kind == "round_authorized":
            row.update({
                "state": "ready", "authorization": payload.get("authorization"),
                "authorization_wait_ended_at": at,
            })
            run.update({"status": "ready", "pause_reason": None})
        elif kind == "round_ready":
            row["state"] = "ready"
            run.update({"status": "ready", "pause_reason": None})
        elif kind == "round_start":
            if payload.get("reopened_from") and row.get("started_at"):
                # A reopen for adjudicator retry / forced rerun; the round's clock keeps its
                # first start, and the journal row records what it was reopened from.
                row.update({"state": "running", "reopened_from": payload["reopened_from"],
                            "reopened_at": at})
            else:
                row.update({"state": "running", "started_at": at})
            for key in ("parallelism", "report_version_reviewed", "critics_run", "critics_failed", "notes"):
                if key in payload:
                    row[key] = _listify(payload[key]) if key in {"critics_run", "critics_failed"} else payload[key]
            run.update({"status": "running", "pause_reason": None})
        elif kind == "round_quorum_complete":
            row.update({"state": "quorum_complete", "ended_at": at})
            duration = _seconds(row.get("started_at"), at)
            row["wall_clock_seconds"] = int(duration) if duration is not None else None
            # The quorum event is where the runtime declares which critics ran; the finalize gate
            # compares this list with the immutable projected route, so it must be projected.
            for key in ("critics_run", "critics_failed", "parallelism", "notes"):
                if key in payload:
                    row[key] = _critic_names(payload[key]) if key in {"critics_run", "critics_failed"} else payload[key]
        elif kind in {"round_integrated", "round_result"}:
            row["state"] = "integrated"
            row["integrated_at"] = payload.get("integrated_at") or at
            for key, value in payload.items():
                if key not in {"round", "at"} and key in row:
                    row[key] = value
            if (
                row.get("blocking_items") is not None
                and row.get("comment_defects") is not None
                and row.get("paper_defects") is not None
                and payload.get("unclassified_defects") is None
            ):
                row["unclassified_defects"] = (
                    row["blocking_items"] - row["comment_defects"] - row["paper_defects"]
                )
        elif kind == "round_end":
            row["ended_at"] = at
            duration = _seconds(row.get("started_at"), at)
            row["wall_clock_seconds"] = int(duration) if duration is not None else None
    elif kind == "publication_redaction":
        # Declares fields of this record that must be dropped before any extract is released.
        # The journal is append-only, so a private string that reached an event payload (an error
        # message naming a local path, for example) is redacted by declaration, never by rewriting.
        block = run.get("publication_redaction") or {}
        if not isinstance(block, dict):
            block = {}
        current = list(block.get("fields_requiring_removal") or [])
        for pointer in _listify(payload.get("fields_requiring_removal")):
            if isinstance(pointer, str) and pointer and pointer not in current:
                current.append(pointer)
        block["fields_requiring_removal"] = current
        if payload.get("note"):
            block["note"] = payload["note"]
        run["publication_redaction"] = block
    elif kind == "review_scope":
        for key in (
            "changed_claim_ids", "rereviewed_claim_ids", "unresolved_blocking_item_ids",
            "unresolved_conclusion_critical_item_ids", "unreviewed_sections",
        ):
            # _listify, as for critics_run: `changed_claim_ids=F2,F7` is the form log_event.sh's
            # own examples use for lists, and list("F2,F7") stored it as five one-character ids,
            # which the ordered-union gate at finalize then rejected with no hint why.
            run[key] = _listify(payload.get(key))
    elif kind == "gate":
        compliance = run.setdefault("compliance", {}) or {}
        run["compliance"] = compliance
        compliance.setdefault("gates_run", []).append({
            "gate": payload.get("gate"), "at": at, "round": payload.get("round"),
            "exit_status": payload.get("exit_status"),
            "passed": payload.get("exit_status") == 0,
            "log_path": payload.get("log_path"),
        })
        if payload.get("gate") == "post_delta" and payload.get("exit_status") == 0:
            run["post_delta_gates_passed"] = True
        # 2.7.7: operators logged a consistency check run before round 1 as round=0 (the <n-or-0> SKILL gives model and
        # tokens events); it stays in gates_run and makes no round row. Projecting a round-0 row left run.json's rounds
        # not contiguous from 1, which finalize refuses, and the append-only journal could never drop it.
        number = payload.get("round")
        if payload.get("gate") == "check_consistency" and isinstance(number, int) and not isinstance(number, bool) \
                and number >= 1:
            row = _ensure_round(run, number)
            row["consistency_check_run"] = True
            row["consistency_check_exit"] = payload.get("exit_status")
    elif kind == "model":
        assignments = run.setdefault("model_assignments", [])
        previous = next(
            (row for row in reversed(assignments) if row.get("role") == payload.get("role")),
            None,
        )
        model_id = payload.get("model_id")
        family = payload.get("model_family")
        auditor_family = (run.get("auditor_model") or {}).get("family")
        assignments.append({
            "role": payload.get("role"), "role_class": payload.get("role_class", "other"),
            "round": payload.get("round"), "model_id": model_id,
            "model_family": family, "provider": payload.get("provider"),
            "differs_from_auditor_family": (family or "unknown") != auditor_family,
            "auditor_family_at_time": auditor_family,
            "source": payload.get("source", "declared"),
            "replaces_model_id": payload.get("replaces_model_id") or (
                previous.get("model_id") if previous and previous.get("model_id") != model_id else None
            ),
            "intended": payload.get("intended", True), "recorded_at": at,
        })
    elif kind == "tokens":
        obtainable = payload.get("obtainable")
        if obtainable is None:
            obtainable = payload.get("total_tokens") is not None or payload.get("input_tokens") is not None
        run.setdefault("tokens", []).append({
            "role": payload.get("role"), "round": payload.get("round"),
            "obtainable": bool(obtainable), "input_tokens": payload.get("input_tokens"),
            "output_tokens": payload.get("output_tokens"),
            "cached_input_tokens": payload.get("cached_input_tokens"),
            "reasoning_tokens": payload.get("reasoning_tokens"),
            "total_tokens": payload.get("total_tokens"),
            "source": payload.get("source") or (None if obtainable else "unavailable"),
            "recorded_at": at,
        })
    elif kind == "cost":
        obtainable = payload.get("obtainable", payload.get("amount") is not None)
        run.setdefault("costs", []).append({
            "role": payload.get("role"), "round": payload.get("round"),
            "obtainable": bool(obtainable), "amount": payload.get("amount"),
            "currency": payload.get("currency") or ("USD" if payload.get("amount") is not None else None),
            "billing_model": payload.get("billing_model"),
            "source": payload.get("source") or (None if obtainable else "unavailable"),
            "recorded_at": at,
        })
    elif kind == "failure":
        run.setdefault("failure_events", []).append({
            "at": at, "kind": payload.get("kind"), "role": payload.get("role"),
            "round": payload.get("round"), "detail": payload.get("detail"),
            "retries": payload.get("retries"), "substitution": payload.get("substitution"),
            "resolved": payload.get("resolved"),
        })
    elif kind == "brief_correction":
        corrections = run.setdefault("brief_corrections", {"count": 0, "items": []})
        items = corrections.setdefault("items", [])
        items.append({
            "id": payload.get("id") or f"bc{len(items) + 1}", "at": at,
            "raised_by": payload.get("raised_by"), "field": payload.get("field"),
            "description": payload.get("description"), "accepted": payload.get("accepted"),
        })
        corrections["count"] = len(items)
    elif kind == "delivery":
        # each delivery replaces the record: a re-log after a rebuild inheriting the old artefact hash kept failing
        # finalize's check, and one without a recipient inherited publish's filing recipient and skipped every check
        run["delivery"] = {
            "delivered_at": payload.get("delivered_at") or at,
            "artifact_path": payload.get("artifact_path"),
            "artifact_sha256": payload.get("artifact_sha256"),
            "release_dir": payload.get("release_dir"),
            "recipient": payload.get("recipient"),
        }
    elif kind == "package":
        package = run.setdefault("package", {}) or {}
        run["package"] = package
        package.update({key: value for key, value in payload.items() if key != "at"})
    elif kind == "human_effort":
        effort = run.setdefault("human_effort", {}) or {}
        run["human_effort"] = effort
        values = effort.setdefault("person_minutes_by_phase", {})
        phase = payload.get("phase")
        # A notes-only human_effort event carries no phase: it records WHY no minutes exist, which
        # run.schema.json distinguishes from zero. Writing values[None] here put a null key in the
        # projection and then crashed json.dumps(sort_keys=True) on the mixed key types.
        if phase is not None:
            values[phase] = (values.get(phase) or 0) + (payload.get("person_minutes") or 0)
        if payload.get("notes") is not None:
            effort["notes"] = payload["notes"]
        if payload.get("people_involved") is not None:
            effort["people_involved"] = payload["people_involved"]
    elif kind == "pause_start":
        run["status"] = "paused"
        run["pause_reason"] = payload.get("reason") or "external_dependency"
    elif kind == "pause_end":
        run["status"] = payload.get("resume_status") or "ready"
        run["pause_reason"] = None
    elif kind == "settled":
        settled = run.setdefault("settled_facts", {}) or {}
        run["settled_facts"] = settled
        settled.update({key: value for key, value in payload.items() if key != "at"})
    elif kind == "records":
        run.setdefault("records_retained", []).append({
            "artifact": payload.get("artifact"), "path": payload.get("path"),
            "present": payload.get("present", False), "count": payload.get("count"),
            "bytes": payload.get("bytes"), "sha256": payload.get("sha256"),
            "retention": payload.get("retention"), "reason": payload.get("reason"),
        })
        run["records_retained_checked_at"] = at
    elif kind == "finalize":
        run["ended_at"] = at
        run["status"] = payload.get("status") or run.get("status")
    elif kind == ENVIRONMENT_EVENT:
        # 2.7.4: the latest capture is the record; an earlier one stays in the journal
        run["environment"] = copy.deepcopy(payload.get("environment"))
    elif kind == "metadata_correction":
        for key, value in (payload.get("set") or {}).items():
            run[key] = copy.deepcopy(value)
    elif kind == AMEND_EVENT:
        # 2.6.9: scripts/amend_round_cap.py rewrote the snapshot; the run follows it
        if payload.get("leaf") == "critique.round_cap" and isinstance(payload.get("to"), int):
            run["round_cap"] = payload["to"]
        if isinstance(payload.get("snapshot_sha256"), str):
            toolkit = dict(run.get("toolkit") or {})
            toolkit["pipeline_policy_snapshot_sha256"] = payload["snapshot_sha256"]
            run["toolkit"] = toolkit
        run.setdefault("policy_amendments", []).append({
            "seq": event.get("seq"), "at": at, "leaf": payload.get("leaf"), "from": payload.get("from"),
            "to": payload.get("to"), "reason": payload.get("reason"),
            "snapshot_sha256": payload.get("snapshot_sha256"),
        })
    elif kind == REOPEN_EVENT:
        # 2.6.9: a finalized audit continues. The close it reopens stays in the journal and in reopenings; the
        # stop, its evidence and the compliance verdict are cleared so the next finalize must establish them afresh.
        run.setdefault("reopenings", []).append({
            "seq": event.get("seq"), "at": at, "reason": payload.get("reason"), "purpose": payload.get("purpose"),
            "previous_finalized_at": payload.get("previous_finalized_at"),
            "previous_stopping_reason": payload.get("previous_stopping_reason"),
            "previous_rounds_run": payload.get("previous_rounds_run"),
            "previous_registry_version": payload.get("previous_registry_version"),
            "previous_publication": copy.deepcopy(payload.get("previous_publication")),
            "skill_version": (payload.get("toolkit") or {}).get("skill_version"),
        })
        run.update({"status": "ready", "pause_reason": None, "stopping_reason": None,
                    "stopping_reason_detail": None, "ended_at": None, "wall_clock_seconds": None})
        for key in REVIEW_SCOPE_FIELDS:
            run[key] = []
        if isinstance(run.get("compliance"), dict):
            compliance = copy.deepcopy(run["compliance"])   # the gate log is history; the verdict is not
            compliance["compliance_pass"] = False
            compliance["violations"] = ["not yet finalized: the audit was reopened after its last finalize"]
            run["compliance"] = compliance


def project_invocations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reservations = _reservation_states(events)
    rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["type"] == "invocation_correction":
            payload = event.get("payload") or {}
            invocation_id = payload.get("invocation_id")
            record = by_id.get(str(invocation_id))
            if record is None:
                raise JournalError(
                    f"invocation correction precedes terminal record: {invocation_id!r}"
                )
            changes = payload.get("set")
            if not isinstance(changes, dict) or not changes:
                raise JournalError("invocation correction set must be a non-empty object")
            forbidden = sorted(IMMUTABLE_INVOCATION_FIELDS.intersection(changes))
            if forbidden:
                raise JournalError(
                    "invocation correction changes immutable fields: " + ", ".join(forbidden)
                )
            if "status" in changes and changes["status"] != record.get("status"):
                # The row's original terminal status is the fact about what the call did.
                # Keep the first one across repeated corrections: a superseded failure must
                # stay countable as a failure. Two rows on the first production audit went
                # failed -> legacy_record, and run_metrics then reported 7 failures, not 9.
                record.setdefault("superseded_status", record.get("status"))
            record.update(copy.deepcopy(changes))
            record["journal_event"] = {
                "seq": event["seq"], "hash": event["event_hash"],
            }
            continue
        if event["type"] not in {"invocation_terminal", "cache_reuse"}:
            continue
        payload = event.get("payload") or {}
        record = copy.deepcopy(payload.get("record") or {})
        invocation_id = payload.get("invocation_id") or record.get("invocation_id")
        state = reservations.get(str(invocation_id), {})
        record.setdefault("invocation_id", invocation_id)
        for key in (
            "role", "role_class", "round", "attempt_kind", "provider", "requested_model",
            "reasoning_effort", "input_artifact_sha256", "prompt_sha256",
            "provider_request_status", "inference_start_status", "attempt",
            "attempt_ceiling", "attempt_ceiling_source", "attempt_ceiling_applies",
        ):
            if record.get(key) is None and state.get(key) is not None:
                record[key] = copy.deepcopy(state[key])
        record["journal_event"] = {
            "seq": event["seq"], "hash": event["event_hash"],
        }
        rows.append(record)
        by_id[str(invocation_id)] = record
    return rows


def _apply_text_redactions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """2.7.6: the events with every field a later text_redaction names replaced by "[redacted]" (copies only)."""
    targets: dict[int, set[str]] = {}
    for event in events:
        if event.get("type") == REDACTION_EVENT:
            payload = event.get("payload") or {}
            try:
                targets.setdefault(int(payload.get("seq")), set()).add(str(payload.get("field")))
            except (TypeError, ValueError):
                continue
    if not targets:
        return events
    redacted = []
    for event in events:
        fields = targets.get(event.get("seq")) if isinstance(event.get("seq"), int) else None
        if fields and isinstance(event.get("payload"), dict):
            event = dict(event, payload=dict(event["payload"]))
            for field in fields & REDACTABLE_FIELDS:
                if isinstance(event["payload"].get(field), str):
                    event["payload"][field] = "[redacted]"
        redacted.append(event)
    return redacted


def project_run(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        raise JournalError("cannot project a run from an empty journal")
    events = _apply_text_redactions(events)
    anchor_index = 0
    initial = (events[0].get("payload") or {}).get("initial_run")
    if not isinstance(initial, dict):
        for index, event in enumerate(events):
            if event.get("type") == "migration_anchor":
                candidate = (event.get("payload") or {}).get("initial_run")
                if isinstance(candidate, dict):
                    initial = candidate
                    anchor_index = index
                    break
    if not isinstance(initial, dict):
        raise JournalError("journal init event lacks payload.initial_run")
    run = copy.deepcopy(initial)
    run["schema_version"] = SCHEMA_VERSION
    run["events"] = []
    for event in events[anchor_index + 1:]:
        _apply_event(run, event)
    run["invocations"] = project_invocations(events)
    run["events"] = copy.deepcopy(events)
    run["journal_watermark"] = watermark(events)
    rounds = run.get("rounds") or []
    run["rounds_run"] = sum(row.get("state") == "integrated" for row in rounds)
    if run.get("ended_at") and run.get("started_at"):
        run["wall_clock_seconds"] = int(_seconds(run["started_at"], run["ended_at"]) or 0)
    return run


def project_all_locked(root: pathlib.Path, events: list[dict[str, Any]] | None = None) -> None:
    events = events if events is not None else load_events(root)
    run = project_run(events)
    rows = run["invocations"]
    _atomic_write(
        root / RUN_NAME,
        json.dumps(run, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n",
    )
    payload = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    _atomic_write(root / INVOCATIONS_NAME, payload)
    _atomic_write(
        root / "invocations.watermark.json",
        json.dumps(watermark(events), indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )
    # Metrics are projections too. Import lazily to avoid a module cycle at CLI
    # startup (summarize_invocations imports this module for journal timing).
    try:
        import summarize_invocations
        require_phase_one = (
            run.get("schema_version") in {"2.0.0", "2.1.0"}
            and not run.get("normalization")
        )
        metrics = summarize_invocations.aggregate(
            rows, require_phase_one=require_phase_one, events=events, run=run,
        )
        run["durations"] = copy.deepcopy(metrics.get("durations"))
        _atomic_write(
            root / RUN_NAME,
            json.dumps(run, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n",
        )
        _atomic_write(
            root / "run_metrics.json",
            json.dumps(metrics, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        _atomic_write(root / "RUN_METADATA.md", summarize_invocations.markdown(metrics).encode("utf-8"))
    except (ImportError, AttributeError):
        # A standalone schema migration can still rebuild the two normative
        # projections; the next summarize_invocations call deterministically
        # regenerates these human-facing derivatives.
        pass


def recover(root: pathlib.Path) -> dict[str, Any]:
    """Verify the chain and rebuild projections without making a provider call."""
    root = root.resolve()
    with journal_lock(root):
        events = load_events(root)
        if not events:
            raise JournalError("canonical journal is missing or empty")
        project_all_locked(root, events)
        incomplete = []
        terminal = _terminal_ids(events)
        for invocation_id, state in _reservation_states(events).items():
            if invocation_id not in terminal:
                request_status = state.get("provider_request_status")
                if state.get("response_received"):
                    recovery_action = "finalize_from_bound_response; do_not_duplicate_provider_call"
                elif request_status == "not_requested":
                    recovery_action = "dispatch_reserved_invocation; preserve_invocation_id"
                elif request_status == "rejected":
                    recovery_action = "terminalize_rejection; apply_typed_retry_policy"
                else:
                    recovery_action = "reconcile_provider_request; do_not_duplicate_provider_call"
                incomplete.append({
                    "invocation_id": invocation_id,
                    "provider_request_status": request_status,
                    "inference_start_status": state.get("inference_start_status"),
                    "attempt": state.get("attempt"),
                    "response_received": bool(state.get("response_received")),
                    "response_artifact": state.get("response_artifact"),
                    "response_sha256": state.get("response_sha256"),
                    "recovery_action": recovery_action,
                })
        return {
            "recovered": True, "provider_calls": 0, "incomplete_invocations": incomplete,
            **watermark(events),
        }


def _read_json(path: pathlib.Path) -> dict[str, Any]:
    return _strict_object(path.read_text(encoding="utf-8"), str(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("initialize")
    init.add_argument("audit_dir", type=pathlib.Path)
    event = sub.add_parser("append-event")
    event.add_argument("audit_dir", type=pathlib.Path)
    event.add_argument("type")
    event.add_argument("payload", type=pathlib.Path)
    event.add_argument("--at")
    kv = sub.add_parser("append-kv")
    kv.add_argument("audit_dir", type=pathlib.Path)
    kv.add_argument("type")
    kv.add_argument("values", nargs="*")
    batch = sub.add_parser("append-batch")
    batch.add_argument("audit_dir", type=pathlib.Path)
    batch.add_argument("events_jsonl", type=pathlib.Path)
    prepared = sub.add_parser("prepare-round")
    prepared.add_argument("audit_dir", type=pathlib.Path)
    prepared.add_argument("round", type=int)
    prepared.add_argument("plan", type=pathlib.Path)
    authorized = sub.add_parser("authorize-round")
    authorized.add_argument("audit_dir", type=pathlib.Path)
    authorized.add_argument("round", type=int)
    authorized.add_argument("authorization", type=pathlib.Path)
    projection = sub.add_parser("project")
    projection.add_argument("audit_dir", type=pathlib.Path)
    recovery = sub.add_parser("recover")
    recovery.add_argument("audit_dir", type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "initialize":
            result = initialize(args.audit_dir)
        elif args.command == "append-event":
            # The CLI appends go through append_cli_events: a raw verifier lifecycle is refused.
            payload = _read_json(args.payload)
            check_hand_payload(args.type, payload)
            emitted = append_cli_events(args.audit_dir, [{
                "type": args.type, "payload": payload, "at": args.at,
                "source": "lifecycle_journal.py",
            }])[-1]
            result = {"seq": emitted["seq"], "event_hash": emitted["event_hash"]}
        elif args.command == "append-kv":
            payload = parse_kv(args.values)
            check_hand_payload(args.type, payload)
            if args.type == "phase_start":
                _warn_late_phase_start(args.audit_dir, payload.get("phase"), payload.get("at"))
            at = payload.pop("at", None)
            emitted = append_cli_events(args.audit_dir, [{
                "type": args.type, "payload": payload, "at": at, "source": "log_event.sh",
            }])[-1]
            result = {"seq": emitted["seq"], "event_hash": emitted["event_hash"]}
        elif args.command == "append-batch":
            specs = []
            # every phase's last end in this batch (an end with no at is "now"), read before any start is judged
            batch_ends: dict = {}
            for pre_line in args.events_jsonl.read_text(encoding="utf-8").splitlines():
                try:
                    pre = json.loads(pre_line)
                except ValueError:
                    continue
                if isinstance(pre, dict) and pre.get("type") == "phase_end":
                    # the time the journal records (top-level at; none = now); the last end in batch order wins,
                    # as in the projection
                    batch_ends[(pre.get("payload") or {}).get("phase")] = pre.get("at") or utc_now()
            for number, line in enumerate(
                args.events_jsonl.read_text(encoding="utf-8").splitlines(), 1,
            ):
                value = _strict_object(line, f"{args.events_jsonl} line {number}")
                check_hand_payload(value.get("type"), value.get("payload"))
                if isinstance(value.get("payload"), dict) and "at" in value["payload"]:
                    # the writer ignores a payload "at" (it keeps it as data): refuse rather than record "now"
                    raise JournalError(f"{args.events_jsonl} line {number}: put \"at\" at the top level of the "
                                       "line, not inside \"payload\"")
                line_phase = (value.get("payload") or {}).get("phase")
                line_at = value.get("at")
                if value.get("type") == "phase_start":
                    _warn_late_phase_start(args.audit_dir, line_phase, line_at, batch_ends.get(line_phase))

                specs.append({
                    "type": value.get("type"), "payload": value.get("payload"),
                    "at": value.get("at"), "source": "log_event.sh",
                })
            emitted = append_cli_events(args.audit_dir, specs)
            result = {
                "events": len(emitted), "first_seq": emitted[0]["seq"],
                "last_seq": emitted[-1]["seq"], "event_hash": emitted[-1]["event_hash"],
            }
        elif args.command == "prepare-round":
            result = prepare_round(args.audit_dir, args.round, _read_json(args.plan))
        elif args.command == "authorize-round":
            result = authorize_round(
                args.audit_dir, args.round, _read_json(args.authorization),
            )
        elif args.command == "project":
            with journal_lock(args.audit_dir.resolve()):
                events = load_events(args.audit_dir.resolve())
                project_all_locked(args.audit_dir.resolve(), events)
                result = watermark(events)
        else:
            result = recover(args.audit_dir)
    except (OSError, JournalError) as exc:
        print(f"FATAL: {exc}", file=__import__("sys").stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
