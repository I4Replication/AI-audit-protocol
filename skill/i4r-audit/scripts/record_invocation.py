#!/usr/bin/env python3
"""Validate and append one canonical model-attempt record to an audit ledger."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import sys
import tempfile
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
SCHEMA = SKILL / "schemas/v2/invocation-record.schema.json"
POLICY = SKILL / "config/pipeline-policy-v2.json"
POLICY_SNAPSHOT = "pipeline-policy.snapshot.json"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SKILL / "schemas/tools"))
import validate_json_schema
import validate_v2
import lifecycle_journal
import verifier_contract


# Public lifecycle API used by Phase-1/coordinator/critique runners.  Keep these
# wrappers here because this module was already the shared telemetry boundary.
AttemptBudgetExhausted = lifecycle_journal.AttemptBudgetExhausted
AuthorizationRequired = lifecycle_journal.AuthorizationRequired
JournalError = lifecycle_journal.JournalError


def reserve_invocation(root: pathlib.Path, reservation: dict[str, Any]) -> dict[str, Any]:
    return lifecycle_journal.reserve_invocation(pathlib.Path(root), reservation)


def mark_provider_request(
    root: pathlib.Path, invocation_id: str, *, status: str = "requested",
    at: str | None = None,
) -> dict[str, Any]:
    return lifecycle_journal.mark_provider_request(
        pathlib.Path(root), invocation_id, status=status, at=at,
    )


def mark_inference_start(
    root: pathlib.Path, invocation_id: str, *, at: str | None = None,
) -> dict[str, Any]:
    return lifecycle_journal.mark_inference_start(pathlib.Path(root), invocation_id, at=at)


def mark_response_received(
    root: pathlib.Path, invocation_id: str, *, artifact: str, sha256: str,
    at: str | None = None,
) -> dict[str, Any]:
    return lifecycle_journal.mark_response_received(
        pathlib.Path(root), invocation_id, artifact=artifact, sha256=sha256, at=at,
    )


def record_terminal(root: pathlib.Path, record: dict[str, Any]) -> dict[str, Any]:
    # No schema validation here, on purpose. Callers pass partially-shaped rows and rely on
    # lifecycle_journal.record_terminal to synthesize the lifecycle events (finalize_run.sh's
    # self-test fixture does exactly this), so the full record schema cannot be applied at this
    # boundary. What protects the journal is its own pre-commit proof: an unprojectable row is
    # refused with JournalError before anything is fsynced. Production runners validate the
    # complete record themselves before calling.
    return lifecycle_journal.record_terminal(pathlib.Path(root), record)


def record_cache_reuse(root: pathlib.Path, record: dict[str, Any]) -> dict[str, Any]:
    return lifecycle_journal.record_cache_reuse(pathlib.Path(root), record)


def correct_invocation(
    root: pathlib.Path, invocation_id: str, changes: dict[str, Any], *, reason: str,
    at: str | None = None,
) -> dict[str, Any]:
    return lifecycle_journal.correct_invocation(
        pathlib.Path(root), invocation_id, changes, reason=reason, at=at,
    )


def file_hash(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def policy_hash(policy: dict[str, Any]) -> str:
    data = json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def load_policy_snapshot(root: pathlib.Path) -> dict[str, Any]:
    path = root / POLICY_SNAPSHOT
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"immutable audit-scoped policy snapshot is missing: {path} -- run "
                         "scripts/init_run.sh on the audit first (SKILL.md section 1)")
    try:
        envelope = validate_json_schema.strict_loads(path.read_text(encoding="utf-8"), source=str(path))
        policy = envelope["policy"]
        if envelope.get("snapshot_version") != "1" or envelope.get("policy_sha256") != policy_hash(policy):
            raise ValueError("policy snapshot hash is invalid")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid audit-scoped policy snapshot: {path}: {exc}") from exc
    return policy


def initialize_policy_snapshot(root: pathlib.Path) -> dict[str, Any]:
    """Create the init-time snapshot once; ordinary validation is load-only."""
    return freeze_policy_snapshot(root)[0]


def freeze_policy_snapshot(root: pathlib.Path) -> tuple[dict[str, Any], bool]:
    """initialize_policy_snapshot, also saying whether THIS call created the file (its O_EXCL open won).

    init_run.sh removes a snapshot it froze when it stops before run.json binds it. The first 2.7.0 cut took
    "absent before the call" to mean "mine", so an init that loaded the snapshot a concurrent init had just
    created and bound removed it on the way out (2.7.0 review, init-2). Only the O_EXCL create knows.
    """
    path = root / POLICY_SNAPSHOT
    if path.exists() or path.is_symlink():
        return load_policy_snapshot(root), False
    policy = validate_json_schema.strict_loads(POLICY.read_text(encoding="utf-8"), source=str(POLICY))
    envelope = {"snapshot_version": "1", "policy_sha256": policy_hash(policy), "policy": policy}
    data = (json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_policy_snapshot(root), False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
    except BaseException:
        # This call created the file, so a partial write is this call's to take back: left in place it would
        # be refused as an invalid leftover by every later init in the directory.
        path.unlink(missing_ok=True)
        raise
    return load_policy_snapshot(root), True


def parse_time(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be an observed timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not an ISO date-time") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def configured_for(record: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any] | None:
    # The post-fan-out verifier (2.6.4) keys on role_class, never on a role name, and is checked
    # first so no verifier can fall into another role's route. verifier_route falls back to the
    # single route the Phase-1 auditors share for snapshots frozen before 2.6.4, and returns None
    # when there is no defensible route; validate_record then refuses the row.
    if verifier_contract.is_verifier(record):
        return verifier_contract.verifier_route(policy)
    role = record["role"]
    if role == "coordinator":
        return policy["models"]["coordinator"]
    phase_one = policy["models"]["phase_1_auditors"]
    if role in phase_one:
        return phase_one[role]
    # Targeted adjudication deliberately keeps max effort while the routine critic path uses
    # high.  The runtime emits targeted_adjudicator telemetry and a critic_synth result role;
    # accept either identity here so an adapter cannot accidentally route adjudication through
    # the cheaper ordinary-critic policy.
    if record["role_class"] == "adjudicator" or role in {"targeted_adjudicator", "critic_synth"}:
        return policy["models"]["targeted_adjudicator"]
    if record["role_class"] in {"critic", "synthesizer"} or role == "model_probe":
        return policy["models"]["critics"]
    return None


def historical_route_for(record: dict[str, Any], policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Routes a critic-path record may legitimately carry because it predates a journaled re-pin.

    scripts/repin_critic_route.py replaces an audit's critic route when the account cannot use the
    pinned model, and keeps the replaced route in ``policy["critic_route_history"]``. A record made
    before the re-pin is judged against the route in force then -- but only a model probe, or an
    attempt that did not complete: the re-pin refuses once any critic review has completed, so
    every completed review is always on the current route.
    """
    history = policy.get("critic_route_history")
    if not isinstance(history, list) or not history:
        return []
    if record.get("role_class") == "adjudicator" or record.get("role") in {"targeted_adjudicator", "critic_synth"}:
        key = "targeted_adjudicator"
    elif record.get("role_class") in {"critic", "synthesizer"} or record.get("role") == "model_probe":
        key = "critics"
    else:
        return []
    if record.get("status") == "completed" and record.get("role") != "model_probe":
        return []
    started = record.get("started_at") or record.get("ended_at")
    if not isinstance(started, str):
        return []
    routes = []
    for entry in history:
        if not isinstance(entry, dict) or not isinstance(entry.get(key), dict):
            continue
        # Both times are whole seconds, so a record that started in the second the re-pin was
        # journaled compares equal; it still predates the re-pin, because the re-pin refuses once
        # any review on the old route has completed and every later dispatch uses the new route.
        # A strict "<" made the 2.6.1 re-pin scenario fail whenever the failed probe and the
        # re-pin landed in the same second.
        if isinstance(entry.get("superseded_at"), str) and started <= entry["superseded_at"]:
            routes.append(entry[key])
    return routes


CONFIRMING_OUTCOMES = {"confirmed", "pass", "verified"}  # make_settled.sh's confirmation set
REVIEWER_CLASSES = {"critic", "adjudicator", "synthesizer"}  # finalize's critic_calls set


def verifier_compliance(
    root: pathlib.Path, invocations: list[dict[str, Any]],
    registry_rows: list[dict[str, Any]], policy: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]] | None:
    """Finalize's post-fan-out verifier checks (2.6.4), or None when there is nothing to check.

    ``invocations`` is every ledger row, cache replays included; ``registry_rows`` is
    work/findings.jsonl. With no role_class == "verifier" row this returns None and finalize
    emits nothing new, which keeps an audit that never ran the verifier byte-identical to 2.6.3.
    Otherwise it returns (verifier_calls, violations):

    * every completed verifier provider call must pass verifier_contract.invocation_problems
      (verifier_<12 hex> role, round 0, observed identity, the Phase-1 auditor family), must not
      share a family with any completed critic/adjudicator/synthesizer call, and must have a
      policy route;
    * every verifier row that did not complete (and is not a legacy_record) must still carry a
      verifier_<12 hex> role, round 0 and no critic family;
    * every call collect_verdicts cannot admit (no runner receipt, changed bytes, a result that
      does not parse, a record that run_verifier.readmit_record does not re-admit as the runner's
      own output ...) is a violation;
    * every registry verification that cites a verifier call as a confirmation must pass
      verifier_contract.confirmation_problem for that row's current claim text (scope).

    The Neudorfer rows (operator_verifier, auditor_operator_verification) carry role_class other
    and auditor, so nothing here ever reads them.
    """
    if not any(verifier_contract.is_verifier(row) for row in invocations):
        return None
    violations: list[str] = []
    by_id = {str(row.get("invocation_id")): row for row in invocations}

    def family(row: dict[str, Any]) -> str:
        return str(row.get("model_family") or "").strip().lower()

    families = verifier_contract.auditor_families(invocations)
    critic_families = {family(row) for row in invocations
                       if row.get("role_class") in REVIEWER_CLASSES
                       and row.get("status") == "completed" and row.get("provider_call") is True
                       and family(row)}
    calls = [row for row in invocations
             if verifier_contract.is_verifier(row) and row.get("provider_call") is True
             and row.get("status") == "completed"]
    try:
        route = verifier_contract.verifier_route(policy)
    except (AttributeError, TypeError, ValueError):
        route = None
    if calls and route is None:
        violations.append(
            "verifier calls have no policy route: the audit policy names no "
            "models.post_fan_out_verifier and its Phase-1 auditors do not share a single route")
    # A verifier row that did not complete (failed, timed out, cancelled, a replay that did not
    # finish) confirms nothing, but it still spends a (role, round) attempt budget, and verifiers_ok
    # promises that EVERY verifier call meets the identity contract (records-2). legacy_record is
    # the documented supersession and is judged by collect_verdicts instead.
    for row in invocations:
        if not verifier_contract.is_verifier(row) or row.get("status") in {"completed", "legacy_record"}:
            continue
        bad = []
        role = row.get("role")
        if not isinstance(role, str) or not verifier_contract.ROLE_PATTERN.match(role):
            bad.append(f"verifier role {role!r} is not verifier_<12 hex>")
        if type(row.get("round")) is not int or row.get("round") != verifier_contract.ROUND:
            bad.append(f"verifier {role} has round {row.get('round')!r}, not {verifier_contract.ROUND}")
        if family(row) and family(row) in critic_families:
            bad.append(f"verifier {role} family {family(row)!r} is a critic family "
                       f"{sorted(critic_families)}")
        violations.extend(f"verifier call {row.get('invocation_id')} (status {row.get('status')!r}): "
                          f"{problem}" for problem in bad)
    explained: set[str] = set()
    for call in calls:
        problems = verifier_contract.invocation_problems(call, families)
        if problems:
            explained.add("; ".join(problems))
        if family(call) and family(call) in critic_families:
            problems.append(f"verifier {call.get('role')} family {family(call)!r} is also a "
                            f"critic family {sorted(critic_families)}")
        violations.extend(f"verifier call {call.get('invocation_id')}: {problem}"
                          for problem in problems)
    verdicts, errors = verifier_contract.collect_verdicts(root, invocations)
    for error in errors:
        # A record-field failure is already reported above; say it once.
        if error.partition(": ")[2] not in explained:
            violations.append(f"verifier record {error}")

    try:
        roles = verifier_contract.required_roles(policy)
    except verifier_contract.VerifierContractError as exc:
        roles = None
        violations.append(f"verifier citations cannot be judged: {exc}")
    cited_by: dict[str, list[str]] = {}
    for row in registry_rows:
        if not isinstance(row, dict):
            continue
        finding_id = str(row.get("id") or row.get("finding_id") or "<missing id>")
        for value in row.get("verifications") or []:
            if not isinstance(value, dict):
                continue
            cited_id = str(value.get("invocation_id"))
            cited = by_id.get(cited_id)
            if cited is None:
                continue
            try:
                source: dict[str, Any] | None = verifier_contract.resolve_replay(cited, by_id)
            except verifier_contract.VerifierContractError:
                source = None
            if not verifier_contract.is_verifier(cited) and not (
                    source is not None and verifier_contract.is_verifier(source)):
                continue
            if source is None or not verifier_contract.is_verifier(source):
                violations.append(f"finding {finding_id} cites verifier invocation {cited_id}, "
                                  "which does not resolve to a verifier provider call")
                continue
            source_id = str(source.get("invocation_id"))
            findings = cited_by.setdefault(source_id, [])
            if finding_id not in findings:
                findings.append(finding_id)
            if source.get("status") != "completed":
                violations.append(f"finding {finding_id} cites verifier call {source_id}, which "
                                  f"did not complete (status {source.get('status')!r})")
                continue
            if str(value.get("outcome") or "").strip().lower() not in CONFIRMING_OUTCOMES:
                continue
            if roles is None:
                continue
            try:
                problem = verifier_contract.confirmation_problem(row, source_id, roles, verdicts)
            except verifier_contract.VerifierContractError as exc:
                problem = str(exc)
            if problem:
                violations.append(f"finding {finding_id} cites verifier call {source_id} as a "
                                  f"confirmation, but {problem}")

    listing = []
    for call in calls:
        invocation_id = str(call.get("invocation_id"))
        listing.append({
            "invocation_id": invocation_id,
            "role": call.get("role"),
            "round": call.get("round"),
            "model_family": call.get("model_family"),
            "claim_sha256s": sorted(digest for digest, entries in verdicts.items()
                                    if any(source == invocation_id for source, _ in entries)),
            "cited_by": cited_by.get(invocation_id, []),
        })
    return listing, violations


def validate_policy_binding(root: pathlib.Path) -> None:
    """Require live v2 audits to bind the exact snapshot bytes captured at init."""
    run_path = root / "run.json"
    if not run_path.is_file():
        return
    run = validate_json_schema.strict_loads(run_path.read_text(encoding="utf-8"), source=str(run_path))
    if not isinstance(run, dict) or run.get("schema_version") not in {"2.0.0", "2.1.0"} or run.get("normalization"):
        return
    toolkit = run.get("toolkit") or {}
    expected = toolkit.get("pipeline_policy_snapshot_sha256")
    actual = file_hash(root / POLICY_SNAPSHOT)
    if expected != actual:
        raise ValueError("run.json does not bind the exact init-time pipeline policy snapshot")


def derived_model_family(record: dict[str, Any]) -> str | None:
    provider = str(record.get("provider") or "").lower()
    observed = str(record.get("served_model") or record.get("observed_model") or "").lower()
    if provider == "anthropic" or "claude" in observed:
        return "claude"
    if provider == "openai" or any(token in observed for token in ("gpt", "codex", "o1", "o3", "o4")):
        return "gpt"
    return None


ATTEMPT_CEILING_DEFAULT = 20  # the operator knob's maximum (MAX_TRIES / I4R_MAX_ATTEMPTS)


def validate_attempt_sequences(rows: list[dict[str, Any]], ceiling: int = ATTEMPT_CEILING_DEFAULT) -> None:
    """Accepted inference starts must form ordered, contiguous 1..N sequences within the operator
    attempt ceiling. Critic and auditor attempts are numbered round-wide by the persistent budget,
    so they group by (role, round) across invocations. A capability probe is numbered within the
    driver invocation that ran it (the probe cache decides whether a new invocation re-probes), so
    probe sequences group per invocation as well."""
    groups: dict[tuple[Any, ...], list[int]] = {}
    for row in rows:
        if row.get("provider_call") and row.get("inference_start_status", "started") == "started":
            if not isinstance(row.get("attempt"), int):
                raise ValueError("accepted inference start requires an integer attempt")
            key: tuple[Any, ...] = (row.get("role"), row.get("round"))
            if row.get("attempt_kind") == "capability_probe":
                key = key + (str(row.get("invocation_id") or "").split(":")[0],)
            groups.setdefault(key, []).append(row.get("attempt"))
    for key, attempts in groups.items():
        role, round_number = key[0], key[1]
        if len(attempts) > ceiling or attempts != list(range(1, len(attempts) + 1)):
            raise ValueError(
                f"provider attempts for {role} round {round_number} must be ordered, contiguous 1..N, "
                f"N<={ceiling}; observed {attempts}"
            )


def validate_record(
    record: dict[str, Any], root: pathlib.Path, *, verify_output_bytes: bool = True,
) -> None:
    """Validate one invocation record.

    ``verify_output_bytes`` re-reads the recorded output artifact and compares its
    hash.  That is right when appending a fresh record, but wrong when re-reading a
    historical ledger: roles promote to a fixed canonical path, so a later completed
    attempt legitimately overwrites an earlier attempt's bytes.  Re-checking a
    superseded row then fails on a ledger that is entirely correct.  Callers reading
    history pass False for rows a later completed row has superseded.
    """
    errors = validate_json_schema.errors_for_value(record, SCHEMA)
    if errors:
        raise ValueError("JSON Schema errors:\n  " + "\n  ".join(errors[:30]))
    try:
        validate_v2.validate_invocation(record)
    except validate_v2.ValidationError as exc:
        raise ValueError(str(exc)) from exc
    policy = load_policy_snapshot(root)
    validate_policy_binding(root)
    configured = configured_for(record, policy) if record.get("status") != "legacy_record" else None
    if (configured is None and verifier_contract.is_verifier(record)
            and record.get("status") != "legacy_record"):
        raise ValueError(
            "no verifier route: the audit policy snapshot names no models.post_fan_out_verifier "
            "and its Phase-1 auditors do not share a single route")
    if configured:
        expected = {
            "requested_model": configured["requested_model"],
            "provider": configured["provider"],
            "reasoning_effort": configured["reasoning_effort"],
        }
        wrong = [f"{key}={record.get(key)!r}, expected {value!r}"
                 for key, value in expected.items() if record.get(key) != value]
        if wrong and any(all(record.get(key) == route.get(key) for key in expected)
                         for route in historical_route_for(record, policy)):
            wrong = []
        if wrong:
            raise ValueError("recorded request violates pipeline policy: " + "; ".join(wrong))
    # 2.7.5: an audit initialised on 2.7.5 or later froze critique.critic_route_lock. A completed critic or
    # adjudicator review on any other model (an older copy of the driver, run after an older copy of
    # repin_critic_route.py moved the route) never validates, so integrate_round.py, summarize_invocations.py --check
    # and finalize_run.sh refuse the audit and it cannot be published. Probes and unfinished attempts are not reviews.
    route_lock = ((policy.get("critique") or {}).get("critic_route_lock") or {}).get("model")
    if (route_lock and record.get("status") == "completed" and record.get("requested_model") != route_lock
            and (record.get("role_class") in {"critic", "synthesizer", "adjudicator"}
                 or record.get("role") in {"targeted_adjudicator", "critic_synth"})):
        raise ValueError(
            f"a completed {record.get('role')} review on {record.get('requested_model')!r} violates this audit's "
            f"critique.critic_route_lock={route_lock!r} (2.7.5): it was run off the locked critic route, so the audit "
            "cannot be integrated, finalized or published; restore the route (scripts/repin_critic_route.py, no "
            "--critic-model) and re-run the round on it from the canonical skill")
    if record.get("status") == "started":
        raise ValueError("append-only invocation rows must be terminal; 'started' cannot be updated in place")
    if record.get("attempt_kind") is not None and record["attempt_kind"] not in lifecycle_journal.ATTEMPT_KINDS:
        raise ValueError(f"unsupported attempt_kind: {record['attempt_kind']!r}")
    provider_status = record.get("provider_request_status")
    if provider_status is not None and provider_status not in lifecycle_journal.PROVIDER_REQUEST_STATUSES:
        raise ValueError(f"unsupported provider_request_status: {provider_status!r}")
    inference_status = record.get("inference_start_status")
    if inference_status is not None and inference_status not in lifecycle_journal.INFERENCE_START_STATUSES:
        raise ValueError(f"unsupported inference_start_status: {inference_status!r}")
    if inference_status == "started" and not isinstance(record.get("attempt"), int):
        raise ValueError("inference_start_status=started requires an allocated attempt")
    if inference_status == "not_started" and record.get("attempt") is not None:
        raise ValueError("a non-started inference cannot consume an attempt number")
    if record.get("attempt_kind") == "cache_replay":
        if record.get("provider_call") or record.get("source_invocation_id") is None:
            raise ValueError("cache_replay requires no provider call and a source_invocation_id")
    if record.get("provider_call"):
        for field in ("input_artifact_sha256", "prompt_sha256"):
            if not isinstance(record.get(field), str):
                raise ValueError(f"provider call requires {field}")
        family = derived_model_family(record)
        if family is not None and record.get("model_family") != family:
            raise ValueError(
                f"model_family={record.get('model_family')!r} disagrees with family {family!r} "
                "derived from observed model/provider"
            )
    if record.get("status") != "legacy_record":
        started = parse_time(record.get("started_at"), "started_at")
        ended = parse_time(record.get("ended_at"), "ended_at")
        observed_elapsed = (ended - started).total_seconds()
        elapsed = record.get("elapsed_seconds")
        if observed_elapsed < 0 or not isinstance(elapsed, (int, float)) or abs(observed_elapsed-float(elapsed)) > 2:
            raise ValueError("timestamps and elapsed_seconds are inconsistent")
    assurance = record.get("identity_assurance")
    served = record.get("served_model", record.get("observed_model"))
    if assurance == "observed" and not served:
        raise ValueError("identity_assurance=observed requires served_model")
    if assurance == "requested_only" and served is not None:
        raise ValueError("identity_assurance=requested_only requires served_model=null")
    if record.get("served_model") is not None and record.get("observed_model") not in {
        None, record.get("served_model"),
    }:
        raise ValueError("served_model and legacy observed_model disagree")
    if record.get("status") == "completed":
        if record.get("error") is not None or record.get("error_class") is not None:
            raise ValueError("completed invocation cannot carry an error")
    elif record.get("status") in {"failed", "cancelled", "timed_out"}:
        if not isinstance(record.get("error"), str) or not record["error"].strip() or record.get("error_class") is None:
            raise ValueError("failed/cancelled/timed-out invocation requires error and error_class")
    if record.get("provider_call") is False and record.get("cache_usage") not in {
        "read", "probe_cache", "not_applicable",
    }:
        raise ValueError(
            "non-provider record requires cache_usage=read, probe_cache, or not_applicable"
        )
    if record.get("provider_call") is True and record.get("cache_usage") == "not_applicable":
        raise ValueError("provider call may not use cache_usage=not_applicable")
    artifact = record.get("output_artifact")
    expected_hash = record.get("output_sha256")
    if record.get("status") == "completed":
        output = (root / artifact).resolve()
        try:
            output.relative_to(root)
        except ValueError as exc:
            raise ValueError("output_artifact escapes the audit directory") from exc
        if verify_output_bytes:
            if not output.is_file():
                raise ValueError(f"completed invocation output is missing: {artifact}")
            actual = file_hash(output)
            if actual != expected_hash:
                raise ValueError(f"completed invocation output hash mismatch: {actual} != {expected_hash}")


def append(root: pathlib.Path, record: dict[str, Any]) -> None:
    # Metadata 2.1 is journal-first. Existing v2.0 fixtures and historical audits
    # retain their immutable direct ledger behavior.
    run_path = root / "run.json"
    if run_path.is_file():
        try:
            run = validate_json_schema.strict_loads(
                run_path.read_text(encoding="utf-8"), source=str(run_path),
            )
        except (OSError, ValueError):
            run = None
        if isinstance(run, dict) and run.get("schema_version") == "2.1.0":
            lifecycle_journal.record_terminal(root, record)
            return
    ledger = root / "invocations.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        existing = []
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = validate_json_schema.strict_loads(
                    line, source=f"invocations.jsonl line {line_number}"
                )
                validate_record(value, root)
            except ValueError as exc:
                raise ValueError(f"existing invocation ledger line {line_number} is invalid: {exc}") from exc
            existing.append(value)
        validate_attempt_sequences(existing)
        if record["invocation_id"] in {row["invocation_id"] for row in existing}:
            raise ValueError(f"duplicate invocation_id: {record['invocation_id']}")
        if record.get("provider_call"):
            same = [row for row in existing if row.get("provider_call") and
                    (row.get("role"), row.get("round")) == (record.get("role"), record.get("round"))]
            if len(same) >= 3:
                raise ValueError(f"three-attempt budget exhausted for {record['role']} round {record['round']}")
            attempts = [row.get("attempt") for row in same]
            if record.get("attempt") in attempts:
                raise ValueError(f"duplicate attempt {record['attempt']} for {record['role']} round {record['round']}")
            expected_attempt = len(same) + 1
            if record.get("attempt") != expected_attempt:
                raise ValueError(f"attempt must be contiguous across resumes: expected {expected_attempt}")
        validate_attempt_sequences(existing + [record])
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def self_test() -> int:
    # 2.7.0: the Claude route is the one the snapshot below freezes from the shipped policy
    # (Sonnet 5 through 2.6.10, Sonnet 5.5 since), so the self-test holds for either.
    shipped = validate_json_schema.strict_loads(POLICY.read_text(encoding="utf-8"), source=str(POLICY))
    route = shipped["models"]["phase_1_auditors"]["auditor_reproduction"]
    served = f"claude-{route['requested_model']}-20260801"
    with tempfile.TemporaryDirectory(prefix="i4r-invocation-") as directory:
        root = pathlib.Path(directory).resolve()
        (root / "work").mkdir()
        output = root / "work/auditor.md"
        output.write_text("complete\n")
        record = {
            "invocation_id": "test-auditor-1", "role": "auditor_reproduction",
            "role_class": "auditor", "round": 0, "attempt": 1, "status": "completed",
            "provider": "anthropic", "provider_call": True, "cache_usage": "none",
            "session_id": "session-test", "requested_model": route["requested_model"],
            "observed_model": served, "model_family": "claude",
            "observed_model_ids": [served, "claude-haiku-4-5-20251001"],
            "model_source": "observed", "reasoning_effort": route["reasoning_effort"],
            "started_at": "2026-08-27T10:00:00Z", "ended_at": "2026-08-27T10:01:00Z",
            "elapsed_seconds": 60, "input_artifact_sha256": "a" * 64,
            "prompt_sha256": "b" * 64, "output_artifact": "work/auditor.md",
            "output_sha256": file_hash(output),
            "usage": {"obtainable": True, "input_tokens": 10, "output_tokens": 5,
                      "cached_input_tokens": 0, "reasoning_tokens": 2, "total_tokens": 17,
                      "source": "api_response"},
            "error": None, "error_class": None, "legacy_source": None,
        }
        try:
            validate_record(record, root)
        except ValueError as exc:
            if "snapshot is missing" not in str(exc):
                raise
        else:
            raise AssertionError("ordinary validation silently backfilled a missing policy snapshot")
        initialize_policy_snapshot(root)
        # init_run.sh removes a snapshot on its way out only when --init-policy says this call created it (2.7.0)
        fresh = root / "fresh-audit"
        fresh.mkdir()
        if (freeze_policy_snapshot(fresh) != (load_policy_snapshot(root), True)
                or freeze_policy_snapshot(root)[1] or freeze_policy_snapshot(fresh)[1]):
            raise AssertionError("freeze_policy_snapshot misreported which call created the snapshot")
        validate_record(record, root); append(root, record)
        partial = dict(record, invocation_id="test-claude-partial")
        partial["usage"] = {
            "obtainable": True,
            "input_tokens": 2,
            "output_tokens": 163,
            "cached_input_tokens": 30597,
            "cache_creation_input_tokens": 10876,
            "reasoning_tokens": None,
            "total_tokens": 41638,
            "source": "cli_log",
            "unavailable_reasons": {
                "reasoning_tokens": "provider CLI does not expose separate reasoning tokens",
            },
            "total_tokens_kind": "derived",
            "total_tokens_components": [
                "input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens",
            ],
            "total_tokens_unavailable_reason": None,
        }
        validate_record(partial, root)
        wrong_partial = dict(partial, invocation_id="bad-derived-total")
        wrong_partial["usage"] = dict(partial["usage"], total_tokens=41639)
        try:
            validate_record(wrong_partial, root)
        except ValueError:
            pass
        else:
            raise AssertionError("incorrectly summed derived token total was accepted")
        no_provider = dict(record, invocation_id="test-no-provider", provider_call=False,
                           cache_usage="not_applicable")
        validate_record(no_provider, root)
        invalid_no_provider = dict(no_provider, invocation_id="bad-no-provider", cache_usage="write")
        try:
            validate_record(invalid_no_provider, root)
        except ValueError:
            pass
        else:
            raise AssertionError("non-provider record accepted cache_usage=write")
        try:
            append(root, record)
        except ValueError:
            pass
        else:
            raise AssertionError("duplicate invocation ID was accepted")
        bad = dict(record, invocation_id="bad", requested_model="opus-5",
                   observed_model="claude-opus-5-20260801")
        try:
            validate_record(bad, root)
        except ValueError:
            pass
        else:
            raise AssertionError("Opus was accepted for a Sonnet-only baseline role")
        # The critic-path rows take their routes from the snapshot frozen above, never a literal, so this holds
        # for whichever critic route the shipped policy names (the literals said gpt-5.6-terra).
        frozen_models = load_policy_snapshot(root)["models"]
        adjudicator, critics = frozen_models["targeted_adjudicator"], frozen_models["critics"]
        if adjudicator["reasoning_effort"] == critics["reasoning_effort"]:
            raise AssertionError("the effort negative controls below need the targeted adjudicator's and the "
                                 "critics' efforts to differ")

        def critic_path_route(route: dict[str, Any]) -> dict[str, Any]:
            """The fields a completed call on ``route`` records; the served id is the route plus a date."""
            served_id = f"{route['requested_model']}-2026-08-01"
            family = derived_model_family({"provider": route["provider"], "observed_model": served_id})
            if family is None:
                raise AssertionError(f"no model family derives from the critic-path route {route!r}")
            return {"provider": route["provider"], "requested_model": route["requested_model"],
                    "observed_model": served_id, "observed_model_ids": [served_id],
                    "model_family": family, "reasoning_effort": route["reasoning_effort"]}

        target_output = root / "work/targeted.json"
        target_output.write_text("{}\n")
        target = dict(
            record,
            invocation_id="test-targeted-1",
            role="targeted_adjudicator",
            role_class="adjudicator",
            round=1,
            **critic_path_route(adjudicator),
            output_artifact="work/targeted.json",
            output_sha256=file_hash(target_output),
        )
        validate_record(target, root)
        wrong_target_effort = dict(target, invocation_id="bad-target-effort",
                                   reasoning_effort=critics["reasoning_effort"])
        try:
            validate_record(wrong_target_effort, root)
        except ValueError:
            pass
        else:
            raise AssertionError("targeted adjudication accepted ordinary critic effort")
        critic = dict(
            target,
            invocation_id="test-critic-1",
            role="critic_stats",
            role_class="critic",
            **critic_path_route(critics),
        )
        validate_record(critic, root)
        wrong_critic_effort = dict(critic, invocation_id="bad-critic-effort",
                                   reasoning_effort=adjudicator["reasoning_effort"])
        try:
            validate_record(wrong_critic_effort, root)
        except ValueError:
            pass
        else:
            raise AssertionError("ordinary critic accepted targeted-adjudicator effort")
        # The post-fan-out verifier (2.6.4) is routed by role_class to verifier_route: the declared
        # models.post_fan_out_verifier, or for an older snapshot the one route the Phase-1 auditors
        # share. Positive control first, then one negative per route field and one per family.
        verifier_output = root / "work/verifier.json"
        verifier_output.write_text("{}\n")
        verifier = dict(
            record, invocation_id="test-verifier-1",
            role=verifier_contract.batch_role([verifier_contract.claim_sha256("A fixture claim.")]),
            role_class="verifier", round=0, output_artifact="work/verifier.json",
            output_sha256=file_hash(verifier_output),
        )
        validate_record(verifier, root)
        for label, changes in (
            ("an Opus request", {"requested_model": "opus-5",
                                 "observed_model": "claude-opus-5-20260801",
                                 "observed_model_ids": ["claude-opus-5-20260801"]}),
            ("the critic family", {key: value for key, value in critic_path_route(critics).items()
                                   if key != "reasoning_effort"}),
            ("high effort", {"reasoning_effort": "high"}),
        ):
            try:
                validate_record(dict(verifier, invocation_id="bad-verifier", **changes), root)
            except ValueError as exc:
                if "recorded request violates pipeline policy" not in str(exc):
                    raise AssertionError(f"verifier with {label} failed for the wrong reason: {exc}")
            else:
                raise AssertionError(f"a verifier with {label} was accepted")
        # A name is never the key: the same row labelled role_class other gets no verifier route.
        validate_record(dict(verifier, invocation_id="other-verifier-name", role_class="other",
                             reasoning_effort="high"), root)
        # A snapshot that declares the verifier route overrides the auditor fallback, and one
        # with neither a declared route nor a single shared auditor route refuses the verifier.
        snapshot_policy = load_policy_snapshot(root)
        for label, mutate, accepted, refused in (
            ("declared", lambda models: models.__setitem__("post_fan_out_verifier", {
                "requested_model": route["requested_model"], "spawn_alias": route["spawn_alias"],
                "provider": "anthropic", "reasoning_effort": "high"}), "high", "max"),
            ("undefined", lambda models: (
                models.pop("post_fan_out_verifier", None),
                models["phase_1_auditors"]["auditor_code_methods"].__setitem__(
                    "reasoning_effort", "high")), None, "max"),
        ):
            other = root / f"policy-{label}"
            (other / "work").mkdir(parents=True)
            (other / "work/verifier.json").write_bytes(verifier_output.read_bytes())
            policy = json.loads(json.dumps(snapshot_policy))
            mutate(policy["models"])
            envelope = {"snapshot_version": "1", "policy_sha256": policy_hash(policy), "policy": policy}
            (other / POLICY_SNAPSHOT).write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n")
            if accepted is not None:
                validate_record(dict(verifier, reasoning_effort=accepted), other)
            try:
                validate_record(dict(verifier, reasoning_effort=refused), other)
            except ValueError as exc:
                expected = ("recorded request violates pipeline policy" if accepted is not None
                            else "no verifier route")
                if expected not in str(exc):
                    raise AssertionError(f"{label} verifier route refused for the wrong reason: {exc}")
            else:
                raise AssertionError(f"the {label} verifier route accepted effort {refused!r}")
        bad_family = dict(record, invocation_id="bad-family", model_family="gpt")
        try:
            validate_record(bad_family, root)
        except ValueError:
            pass
        else:
            raise AssertionError("typed model family was trusted over provider/served identity")
        try:
            validate_attempt_sequences([dict(record, attempt=2)])
        except ValueError:
            pass
        else:
            raise AssertionError("non-contiguous existing attempt ledger was accepted")
        snapshot = root / POLICY_SNAPSHOT
        run = {"schema_version": "2.0.0", "normalization": None,
               "toolkit": {"pipeline_policy_snapshot_sha256": file_hash(snapshot)}}
        (root / "run.json").write_text(json.dumps(run) + "\n", encoding="utf-8")
        validate_record(record, root)
        envelope = validate_json_schema.strict_loads(snapshot.read_text(), source=str(snapshot))
        snapshot.write_text(json.dumps(envelope, separators=(",", ":")) + "\n", encoding="utf-8")
        try:
            validate_record(record, root)
        except ValueError:
            pass
        else:
            raise AssertionError("self-consistent policy snapshot rewrite escaped the init-time run binding")
    print("record_invocation self-test: ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_dir", nargs="?", type=pathlib.Path)
    parser.add_argument("record", nargs="?", type=pathlib.Path,
                        help="JSON object file; use '-' to read stdin")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--init-policy", type=pathlib.Path,
                        help="create/validate the immutable audit-scoped policy snapshot and exit")
    parser.add_argument("--reserve", nargs=2, metavar=("AUDIT_DIR", "RESERVATION_JSON"),
                        help="reserve an invocation ID without consuming an attempt")
    parser.add_argument("--provider-request", nargs=2, metavar=("AUDIT_DIR", "INVOCATION_ID"),
                        help="record a provider-request lifecycle transition")
    parser.add_argument("--request-status", default="requested",
                        choices=sorted(lifecycle_journal.PROVIDER_REQUEST_STATUSES - {"not_requested"}))
    parser.add_argument("--inference-start", nargs=2, metavar=("AUDIT_DIR", "INVOCATION_ID"),
                        help="atomically allocate an accepted-inference attempt")
    parser.add_argument("--response-received", nargs=4,
                        metavar=("AUDIT_DIR", "INVOCATION_ID", "ARTIFACT", "SHA256"),
                        help="bind a received provider response before parsing it")
    parser.add_argument("--cache-reuse", nargs=2, metavar=("AUDIT_DIR", "RECORD_JSON"),
                        help="record a cache replay bound to its source invocation")
    parser.add_argument(
        "--correct", nargs=3,
        metavar=("AUDIT_DIR", "INVOCATION_ID", "CORRECTION_JSON"),
        help="append a correction object containing non-empty 'set' and 'reason' fields",
    )
    parser.add_argument("--recover", type=pathlib.Path,
                        help="verify the journal and rebuild projections without a provider call")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.init_policy is not None:
        root = args.init_policy.resolve(); root.mkdir(parents=True, exist_ok=True)
        try: policy, created = freeze_policy_snapshot(root)
        except (OSError, ValueError) as exc:
            print(f"FATAL: {exc}", file=sys.stderr); return 1
        # "created" only when this call's O_EXCL create won; init_run.sh arms its cleanup on that word alone.
        print(f"policy snapshot {'created' if created else 'loaded'}: {root / POLICY_SNAPSHOT} ({policy_hash(policy)})")
        return 0
    try:
        if args.reserve:
            root = pathlib.Path(args.reserve[0]).resolve()
            reservation = validate_json_schema.strict_loads(
                pathlib.Path(args.reserve[1]).read_text(encoding="utf-8"),
                source=args.reserve[1],
            )
            if isinstance(reservation, dict):
                lifecycle_journal.refuse_cli_verifier_event(root, "invocation_reserved", reservation)
            result = reserve_invocation(root, reservation)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.provider_request:
            lifecycle_journal.refuse_cli_verifier_event(
                pathlib.Path(args.provider_request[0]), "provider_request",
                {"invocation_id": args.provider_request[1]})
            result = mark_provider_request(
                pathlib.Path(args.provider_request[0]).resolve(),
                args.provider_request[1], status=args.request_status,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.inference_start:
            lifecycle_journal.refuse_cli_verifier_event(
                pathlib.Path(args.inference_start[0]), "inference_start",
                {"invocation_id": args.inference_start[1]})
            result = mark_inference_start(
                pathlib.Path(args.inference_start[0]).resolve(), args.inference_start[1],
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.response_received:
            lifecycle_journal.refuse_cli_verifier_event(
                pathlib.Path(args.response_received[0]), "provider_response",
                {"invocation_id": args.response_received[1]})
            result = mark_response_received(
                pathlib.Path(args.response_received[0]).resolve(),
                args.response_received[1], artifact=args.response_received[2],
                sha256=args.response_received[3],
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.cache_reuse:
            root = pathlib.Path(args.cache_reuse[0]).resolve()
            record = validate_json_schema.strict_loads(
                pathlib.Path(args.cache_reuse[1]).read_text(encoding="utf-8"),
                source=args.cache_reuse[1],
            )
            if isinstance(record, dict):
                lifecycle_journal.refuse_cli_verifier_event(
                    root, "cache_reuse", {"invocation_id": record.get("invocation_id"), "record": record})
            validate_record(record, root)
            result = record_cache_reuse(root, record)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.correct:
            root = pathlib.Path(args.correct[0]).resolve()
            correction = validate_json_schema.strict_loads(
                pathlib.Path(args.correct[2]).read_text(encoding="utf-8"),
                source=args.correct[2],
            )
            if not isinstance(correction, dict):
                raise ValueError("correction must be a JSON object")
            result = correct_invocation(
                root, args.correct[1], correction.get("set"),
                reason=correction.get("reason"), at=correction.get("at"),
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.recover is not None:
            result = lifecycle_journal.recover(args.recover.resolve())
            print(json.dumps(result, sort_keys=True))
            return 0
    except (OSError, ValueError, lifecycle_journal.JournalError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    if args.audit_dir is None or args.record is None:
        parser.error("audit_dir and record are required")
    root = args.audit_dir.resolve()
    if not root.is_dir():
        parser.error(f"audit directory does not exist: {root}")
    try:
        raw = sys.stdin.read() if str(args.record) == "-" else args.record.read_text(encoding="utf-8")
        record = validate_json_schema.strict_loads(raw, source=str(args.record))
        if not isinstance(record, dict):
            raise ValueError("record must be a JSON object")
        # A verifier call is journaled only by scripts/run_verifier.py (records-1); --correct,
        # the documented way to supersede one, is not affected.
        lifecycle_journal.refuse_cli_verifier_event(
            root, "invocation_terminal", {"invocation_id": record.get("invocation_id"), "record": record})
        validate_record(record, root)
        append(root, record)
    except (OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    print(f"recorded invocation {record['invocation_id']} in {root / 'invocations.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
