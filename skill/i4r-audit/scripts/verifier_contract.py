#!/usr/bin/env python3
"""The post-fan-out verifier contract, shared by every script that produces or consumes it (2.6.4).

A registry row can be promoted only with confirmations from two model families. Before 2.6.4 the
family-A confirmation could come only from the Phase-1 auditor whose report raised the row, so a
finding first raised by the operator, the coordinator or a critic could never be promoted. In
audit A23 three article-text findings that every critic had
confirmed were excluded for exactly that reason, after hand-made verification records had been
refused one gate after another.

The verifier is a packaged family-A role that closes the gap without weakening the two-family rule:

* it runs only on INCLUDED rows raised after fan-out (no Phase-1 auditor in the row's
  ``origin_role`` or ``independently_found_by``);
* it runs under the Phase-1 runtime contract (sandbox, no network, immutable packet, evidence
  manifest, source-hash joins, admission, one structural repair), blind to critic verdicts and the
  coordinator's reasoning, and returns CONFIRMED / REFUTED / UNVERIFIABLE per hypothesis;
* its invocation is ``role_class == "verifier"``, ``round == 0``, ``role == "verifier_<12 hex>"``
  (one role, and so one attempt budget, per batch of hypotheses);
* a confirmation binds to a row by the SHA-256 of the row's claim text, never by finding id,
  because ids are renumbered after the verifier has run;
* one verdict per claim text: a claim that already has a completed verifier verdict is never
  re-verified, and any non-CONFIRMED verdict for a claim text blocks confirmation of that text.

This module holds only pure, provider-free checks. Every consumer (integrate_round.py,
make_settled.sh, make_diff_packet.py, finalize_run.sh, run_verifier.py) calls the same functions,
so the producer and the gates cannot drift apart. When no verifier record exists every function
here is inert, which keeps audits that never use the verifier byte-identical to 2.6.3.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Any, Iterable

ROLE_CLASS = "verifier"
ROLE_PREFIX = "verifier_"
ROUND = 0
RESULT_VERSION = "i4r-verifier-result-1"
PACKET_VERSION = "i4r-verifier-packet-1"
COMPLETION_VERSION = "i4r-verifier-completion-1"
VERDICTS = ("CONFIRMED", "REFUTED", "UNVERIFIABLE")
# reference/adjudication_guide.md holds the same table in prose; keep them in step. OVERSTATED is
# deliberately absent: a verifier tests the claim as written and has no wording to propose.
VERDICT_OUTCOME = {"CONFIRMED": "confirmed", "REFUTED": "refuted", "UNVERIFIABLE": "inconclusive"}
ROUTE_KEYS = ("requested_model", "spawn_alias", "provider", "reasoning_effort")
ROLE_PATTERN = re.compile(r"^verifier_[0-9a-f]{12}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class VerifierContractError(ValueError):
    """A verifier record, output or eligibility claim that does not meet the contract."""


# ---------------------------------------------------------------------------------- identities
def claim_sha256(claim: str) -> str:
    """The binding key: the exact UTF-8 bytes of a registry row's claim text."""
    if not isinstance(claim, str) or not claim:
        raise VerifierContractError("a verifier hypothesis needs a non-empty claim")
    return hashlib.sha256(claim.encode("utf-8")).hexdigest()


def batch_role(claim_hashes: Iterable[str]) -> str:
    """The role name for one batch. The same hypotheses always map to the same role, so retrying a
    batch shares its three-start budget instead of opening a fresh one."""
    hashes = sorted(set(claim_hashes))
    if not hashes or not all(_HEX64.match(h) for h in hashes):
        raise VerifierContractError("a verifier batch needs at least one claim hash")
    return ROLE_PREFIX + hashlib.sha256("\n".join(hashes).encode("ascii")).hexdigest()[:12]


def is_verifier(invocation: dict[str, Any]) -> bool:
    return invocation.get("role_class") == ROLE_CLASS


def output_dir(role: str, input_hash: str) -> str:
    """Audit-relative, content-addressed promotion target. Never a fixed path: finalize re-hashes
    every completed row's output_artifact, so an overwritten target would fail every earlier row."""
    return f"work/verifier/{role}/{input_hash}"


def completion_receipt_path(role: str, input_hash: str) -> str:
    return f"phase1/completions/{role}/{input_hash}.json"


# -------------------------------------------------------------------------------- policy/roles
def load_snapshot_policy(audit: pathlib.Path) -> dict[str, Any]:
    path = audit / "pipeline-policy.snapshot.json"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifierContractError(f"cannot read the audit policy snapshot: {path}: {exc}") from exc
    policy = snapshot.get("policy") if isinstance(snapshot, dict) else None
    if not isinstance(policy, dict):
        raise VerifierContractError(f"audit policy snapshot has no policy object: {path}")
    return policy


def required_roles(policy: dict[str, Any]) -> list[str]:
    roles = ((policy.get("phase_1") or {}).get("required_roles"))
    if not isinstance(roles, list) or not roles or not all(isinstance(r, str) and r for r in roles):
        raise VerifierContractError("policy has no phase_1.required_roles")
    return list(roles)


def verifier_route(policy: dict[str, Any]) -> dict[str, Any] | None:
    """The requested route a verifier must use.

    A 2.6.4 policy names it under models.post_fan_out_verifier. Audits initialised before 2.6.4
    carry a frozen snapshot without that key; for them the route is the single route shared by all
    Phase-1 auditors. If the auditors do not share one route there is no defensible verifier route,
    and None is returned so the caller refuses.
    """
    models = policy.get("models") or {}
    declared = models.get("post_fan_out_verifier")
    if isinstance(declared, dict) and all(declared.get(k) for k in ROUTE_KEYS):
        return {k: declared[k] for k in ROUTE_KEYS}
    auditors = models.get("phase_1_auditors") or {}
    routes = {tuple(entry.get(k) for k in ROUTE_KEYS) for entry in auditors.values()
              if isinstance(entry, dict)}
    if len(routes) != 1 or not all(next(iter(routes))):
        return None
    return dict(zip(ROUTE_KEYS, next(iter(routes))))


def raised_after_fan_out(row: dict[str, Any], roles: Iterable[str]) -> bool:
    """True when no Phase-1 auditor raised or independently found the row.

    round_introduced is not used: operator rows written at coordinator time carry 0.
    """
    provenance = row.get("provenance") or {}
    named = {provenance.get("origin_role"), *(provenance.get("independently_found_by") or [])}
    return not (named & set(roles))


def eligibility_problem(row: dict[str, Any], roles: Iterable[str]) -> str | None:
    """Why a row may not be sent to the verifier, or None if it may."""
    if (row.get("ledger_disposition") or {}).get("status") != "INCLUDED":
        return "it is not INCLUDED in the ledger"
    if not raised_after_fan_out(row, roles):
        return "a Phase-1 auditor raised or independently found it, so its auditor confirms it"
    if not isinstance(row.get("claim"), str) or not row["claim"].strip():
        return "it has no claim text"
    return None


# ------------------------------------------------------------------------------ invocations
def resolve_replay(invocation: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Follow cache replays to the provider call that did the work."""
    seen: set[str] = set()
    current = invocation
    while current.get("provider_call") is not True:
        source = current.get("source_invocation_id")
        if not source or source in seen or source not in by_id:
            raise VerifierContractError(
                f"verifier replay has no resolvable source call: {invocation.get('invocation_id')}")
        seen.add(source)
        current = by_id[source]
    return current


def auditor_families(invocations: Iterable[dict[str, Any]]) -> set[str]:
    """Families of the completed Phase-1 auditor provider calls (round 0, not legacy)."""
    return {str(row.get("model_family")).strip().lower() for row in invocations
            if row.get("role_class") == "auditor" and row.get("status") == "completed"
            and row.get("provider_call") is True and row.get("round") in (0, None)
            and row.get("model_family")}


def invocation_problems(invocation: dict[str, Any], families: set[str]) -> list[str]:
    """Record-level contract violations for a completed verifier provider call."""
    problems = []
    role = str(invocation.get("role") or "")
    if not ROLE_PATTERN.match(role):
        problems.append(f"verifier role {role!r} is not verifier_<12 hex>")
    if invocation.get("round") != ROUND:
        problems.append(f"verifier {role} has round {invocation.get('round')!r}, not {ROUND}")
    if invocation.get("identity_assurance") != "observed":
        problems.append(f"verifier {role} has no observed served-model identity")
    family = str(invocation.get("model_family") or "").strip().lower()
    if not families:
        problems.append("no completed Phase-1 auditor family to compare the verifier with")
    elif family not in families or len(families) != 1:
        problems.append(f"verifier {role} family {family!r} is not the Phase-1 auditor family "
                        f"{sorted(families)}")
    if not isinstance(invocation.get("output_artifact"), str) or not invocation.get("output_sha256"):
        problems.append(f"verifier {role} names no output artifact")
    return problems


# ----------------------------------------------------------------------------------- outputs
def _regular(audit: pathlib.Path, relative: str, label: str) -> pathlib.Path:
    if not isinstance(relative, str) or not relative or relative.startswith(("/", "\\")) \
            or ".." in pathlib.PurePosixPath(relative).parts:
        raise VerifierContractError(f"{label} path is not audit-relative: {relative!r}")
    path = audit / relative
    current = audit
    for part in pathlib.PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise VerifierContractError(f"{label} path passes through a symlink: {relative}")
    if not path.is_file():
        raise VerifierContractError(f"{label} is not a regular file: {relative}")
    return path


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_result(data: Any) -> dict[str, str]:
    """Validate the downstream-relevant shape of a verifier result.json; return
    {claim_sha256: verdict}. The runner's admission validator enforces the full contract (evidence
    joins, rerun artifacts, closed file set) before the result is ever promoted."""
    if not isinstance(data, dict):
        raise VerifierContractError("verifier result is not a JSON object")
    if data.get("schema_version") != RESULT_VERSION:
        raise VerifierContractError(f"verifier result schema_version is not {RESULT_VERSION}")
    if data.get("status") != "complete" or data.get("completion_marker") is not True:
        raise VerifierContractError("verifier result is not complete")
    verdicts = ((data.get("scientific_fields") or {}).get("verdicts"))
    if not isinstance(verdicts, list) or not verdicts:
        raise VerifierContractError("verifier result has no verdicts")
    out: dict[str, str] = {}
    ids: set[str] = set()
    for item in verdicts:
        if not isinstance(item, dict):
            raise VerifierContractError("verifier verdict is not an object")
        digest, verdict, hid = item.get("claim_sha256"), item.get("verdict"), item.get("hypothesis_id")
        if not isinstance(hid, str) or not hid or hid in ids:
            raise VerifierContractError(f"verifier verdict has a missing or repeated hypothesis_id: {hid!r}")
        if not isinstance(digest, str) or not _HEX64.match(digest) or digest in out:
            raise VerifierContractError(f"verifier verdict {hid} has a bad or repeated claim_sha256")
        if verdict not in VERDICTS:
            raise VerifierContractError(f"verifier verdict {hid} is {verdict!r}, not one of {VERDICTS}")
        ids.add(hid)
        out[digest] = verdict
    return out


def verify_record(audit: pathlib.Path, invocation: dict[str, Any],
                  by_id: dict[str, dict[str, Any]], families: set[str]) -> dict[str, str]:
    """Admit one completed verifier invocation as the runner produced it, or raise.

    Beyond the record fields this requires the runner's completion receipt, which binds the
    invocation id, the result, report and evidence-manifest hashes and the hypotheses it was run
    on. A hand-made ledger row (the A23 make_record.py shape) has no receipt, no evidence
    manifest and no hash-bound result, and fails here.
    """
    source = resolve_replay(invocation, by_id)
    if source.get("status") != "completed" or not is_verifier(source):
        raise VerifierContractError(
            f"verifier confirmation is not a completed verifier call: {invocation.get('invocation_id')}")
    problems = invocation_problems(source, families)
    if problems:
        raise VerifierContractError("; ".join(problems))
    result_path = _regular(audit, source["output_artifact"], "verifier result")
    if _sha256(result_path) != source["output_sha256"]:
        raise VerifierContractError(f"verifier result changed after it was recorded: {source['output_artifact']}")
    receipt_rel = completion_receipt_path(source["role"], str(source.get("input_artifact_sha256") or ""))
    try:
        receipt = json.loads(_regular(audit, receipt_rel, "verifier completion receipt").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise VerifierContractError(f"verifier completion receipt is not JSON: {receipt_rel}") from exc
    if not isinstance(receipt, dict):
        raise VerifierContractError(f"verifier completion receipt is not a JSON object: {receipt_rel}")
    if (receipt.get("schema_version") != COMPLETION_VERSION
            or receipt.get("status") != "completed"
            or receipt.get("input_sha256") != source.get("input_artifact_sha256")
            or receipt.get("invocation_id") != source.get("invocation_id")
            or receipt.get("role") != source.get("role")
            or receipt.get("result") != source["output_artifact"]
            or receipt.get("result_sha256") != source["output_sha256"]):
        raise VerifierContractError(f"verifier completion receipt does not bind this call: {receipt_rel}")
    for key, label in (("report", "verifier report"), ("evidence", "verifier evidence manifest")):
        path = _regular(audit, receipt.get(key), label)
        if _sha256(path) != receipt.get(f"{key}_sha256" if key == "report" else "evidence_manifest_sha256"):
            raise VerifierContractError(f"{label} changed after promotion: {receipt.get(key)}")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise VerifierContractError(f"verifier result is not JSON: {source['output_artifact']}") from exc
    if isinstance(result, dict) and result.get("role") != source.get("role"):
        raise VerifierContractError(
            f"verifier result names role {result.get('role')!r}, not {source.get('role')!r}")
    verdicts = parse_result(result)
    bound = {h.get("claim_sha256") for h in receipt.get("hypotheses") or [] if isinstance(h, dict)}
    if bound != set(verdicts):
        raise VerifierContractError(
            f"verifier result verdicts differ from the hypotheses its receipt binds: {source['output_artifact']}")
    _readmit(audit, source, receipt)
    return verdicts


def _readmit(audit: pathlib.Path, source: dict[str, Any], receipt: dict[str, Any]) -> None:
    """Re-admit the call through the runner's own admission (run_verifier.readmit_record).

    The checks above prove only that four files hash to what a receipt says, and anyone can write
    all four: a hand-made record with a nine-key receipt, a '{}' evidence manifest and a report
    reading 'hand made' passed them, and make_settled, finalize and publish with them. The runner
    is imported here, lazily, because run_verifier imports run_phase1, which imports
    record_invocation and lifecycle_journal, which import this module. Any failure to load it is
    a refusal: a verdict nobody can re-admit is not a verdict (fail closed).
    """
    try:
        import importlib
        import sys
        here = str(pathlib.Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.append(here)
        runner = importlib.import_module("run_verifier")
        readmit = runner.readmit_record
    except Exception as exc:  # ImportError, or a runner without the re-admission contract
        raise VerifierContractError(
            f"verifier record {source.get('invocation_id')} cannot be re-admitted: the packaged "
            f"runner (scripts/run_verifier.py) does not load: {type(exc).__name__}: {exc}") from exc
    try:
        readmit(audit, source, receipt)
    except VerifierContractError:
        raise
    except Exception as exc:  # the runner raised another module's class, or crashed: refuse
        raise VerifierContractError(str(exc)) from exc


def collect_verdicts(audit: pathlib.Path, invocations: list[dict[str, Any]]
                     ) -> tuple[dict[str, list[tuple[str, str]]], list[str]]:
    """Every admitted verifier verdict in the ledger, keyed by claim hash.

    Returns ({claim_sha256: [(invocation_id, verdict), ...]}, [errors]). Cache replays resolve to
    their source call and are counted once. With no verifier rows this returns ({}, []).
    """
    by_id = {str(row.get("invocation_id")): row for row in invocations}
    families = auditor_families(invocations)
    verdicts: dict[str, list[tuple[str, str]]] = {}
    errors: list[str] = []
    seen: set[str] = set()
    for row in invocations:
        if not is_verifier(row) or row.get("status") not in ("completed", "legacy_record"):
            continue
        # A call corrected to legacy_record never confirms anything, but a REFUTED or UNVERIFIABLE
        # verdict it returned still blocks its claim text while its record verifies. Otherwise the
        # documented correction tool would erase a non-confirming verdict and let the same claim be
        # sent again -- the one-verdict-per-claim rule bypassed by a status change.
        superseded = row.get("status") == "legacy_record"
        if superseded and row.get("superseded_status") not in (None, "completed"):
            continue
        try:
            source = resolve_replay({**row, "status": "completed"} if superseded else row, by_id)
            if source.get("invocation_id") in seen:
                continue
            seen.add(str(source.get("invocation_id")))
            blocking_only = superseded or source.get("status") == "legacy_record"
            checked = {**source, "status": "completed"} if blocking_only else source
            for digest, verdict in verify_record(audit, checked, by_id, families).items():
                if blocking_only and verdict == "CONFIRMED":
                    continue
                verdicts.setdefault(digest, []).append((str(source["invocation_id"]), verdict))
        except (VerifierContractError, OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            # A superseded record that no longer verifies carries no verdict anyone can trust;
            # it neither blocks nor confirms, and it is not an admission error.
            if not superseded:
                errors.append(f"{row.get('invocation_id')}: {exc}")
    return verdicts, errors


def confirmation_problem(row: dict[str, Any], invocation_id: str, roles: Iterable[str],
                         verdicts: dict[str, list[tuple[str, str]]]) -> str | None:
    """Why a verifier record on this row is not a family-A confirmation, or None if it is.

    The record must cite a verifier call that returned CONFIRMED for this row's current claim text,
    the row must be raised after fan-out, and no verifier call may have returned anything but
    CONFIRMED for the same claim text (one verdict per claim; no re-verification until CONFIRMED).
    """
    if not raised_after_fan_out(row, roles):
        return "the row was raised by a Phase-1 auditor, so the verifier cannot confirm it"
    digest = claim_sha256(row.get("claim") or "")
    entries = verdicts.get(digest) or []
    own = [verdict for inv, verdict in entries if inv == invocation_id]
    if not own:
        return "the cited verifier call returned no verdict for this row's current claim text"
    if own != ["CONFIRMED"]:
        return f"the cited verifier call returned {own[0]} for this claim"
    others = sorted({verdict for inv, verdict in entries if inv != invocation_id and verdict != "CONFIRMED"})
    if others:
        return f"another verifier call returned {others[0]} for the same claim text"
    return None
