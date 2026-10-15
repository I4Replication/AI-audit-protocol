#!/usr/bin/env python3
"""Integrate a completed critique round into the canonical record.

    python3 scripts/integrate_round.py <audit-dir> --round N plan
    python3 scripts/integrate_round.py <audit-dir> --round N apply \
        --decisions decisions.json [--contested contested.json]

``plan`` reads the round's critic and adjudicator outputs and writes
``critique/round_N/INTEGRATION_PLAN.json``: every claim id that received a verdict (tested negatives
included), the verdicts by role, the conflicts critics declared, and the verification records that
each included row could bind. ``apply`` then writes the registry, the response notes and the
adjudication block from that same plan, so the three never disagree.

Why this is a script and not coordinator improvisation: on audit A34 the
integration was ad-hoc code that did not survive the session. Four recording defects followed from
it — tested negatives and declared conflicts missing from the response notes, coordinator approval
reported as False for rows that were approved, and settled facts bound to records whose verdicts
read OVERSTATED rather than to the later round's literal confirmations.

decisions.json maps a finding id to the fields the coordinator decided, for example::

    {"F8": {"status": "confirmed", "coordinator_approved": true,
            "claim": "<corrected claim text>",
            "report_section": "Section 3",
            "verification_snippet": {"kind": "code", "path": "work/coordinator/verification_R/f8_slug.R",
                                     "basis": "recomputes the two coefficients from the deposit"},
            "applied_note": "what was changed in the report, in one line",
            "rebind_literal_confirmations": true}}

A row set ``changes_conclusion: true`` must also carry ``changes_conclusion_which`` (the published
conclusion it changes); apply copies both from the decision. A row that ends APPROVED (status
confirmed/verified/accepted and coordinator_approved true) must carry ``report_section`` ("Section N" or "Section N.N") and ``verification_snippet`` ({kind, path,
basis}; kind code, judgement or unverifiable) -- Phase-1 rows carry neither, so the decision must
supply them. ``apply`` refuses such a row up front and names both fields.

Every round binds a reviewer's record the first time it confirms a claim (a CONFIRMED, or an
OVERSTATED on a claim reworded in that decision). ``rebind_literal_confirmations`` (round > 1) also
replaces a reviewer's EARLIER record with this round's when this round's verdict literally reads
CONFIRMED -- the guarantee a settled fact needs is that its records confirm the claim text as published.
It is ignored for a claim reworded in the same decision (those verdicts are on the old wording).

contested.json maps "critic_<role>:<item_id>" (the critic file's name and the item id) to
``{"position": ..., "evidence": ...}``, both non-empty, for verdicts the coordinator does not accept
(or new issues it rejects). The position and evidence are written to the round's RESPONSE_notes.md;
ADJUDICATION.md marks a contested claim "Decision: CONTESTED (accepted in part)". A malformed entry is
refused before anything is written. Everything else is recorded as accepted. Model output is never edited:
verification records cite each reviewer invocation's own immutable output by path and sha256.

Post-fan-out verifier (2.6.4). A row raised after fan-out (no Phase-1 auditor in its origin_role or
independently_found_by) has no auditor to confirm it. A completed ``role_class == "verifier"`` call
(scripts/verifier_contract.py) that ended no later than this integration and returned a verdict for
the row's CURRENT claim text (bound by the claim's SHA-256, never by finding id) is bound to that
INCLUDED row in every round, replacing an earlier record of the same reviewer. After the decision
fields are applied, a verifier record whose claim hash no longer matches the row's claim is dropped:
a claim edit makes the old verdict inapplicable. So is a record whose call was corrected to
legacy_record (such a call is never bound, though its REFUTED or UNVERIFIABLE still blocks the claim
text) and a record on a row a Phase-1 auditor raised. A verifier record that fails admission stops
the integration, and the message names the remedy. With no verifier row in the ledger none of this
runs, and every output is unchanged.

Disclosure findings (2.6.8). Deposit personal data is reported in the comment, never withheld
(scripts/disclosure_policy.py). A decision that EXCLUDEs a disclosure-flagged row, clears its
``disclosure_flag``, or sets its status rejected or superseded (a change of state: a draft kept
unapproved is still under review, and so, from 2.6.9, is an approved row whose approval is withdrawn
while it stays INCLUDED and flagged) must carry a structured basis, never free text::

    {"F9": {"status": "rejected", "coordinator_approved": false,
            "ledger_disposition": {"status": "EXCLUDED", "target": null,
                                   "reason": "the column holds session dates, not birth dates"},
            "disclosure_exclusion_basis": "refuted"},
     "F4": {"disclosure_flag": false, "disclosure_exclusion_basis": "not_a_disclosure_defect",
            "disclosure_flag_reason": "a statistics finding the draft over-flagged"}}

The basis is one of refuted, duplicate_of:<finding id>, not_a_disclosure_defect or tested_negative.
A row withdrawn on a basis must end EXCLUDED, and its ledger reason is written opening with the
basis label ("Refuted: the column holds ..."), which is how finalize reads it back. A MERGE target
and a duplicate_of row must themselves end final (approved, a final status, INCLUDED). Clearing a
flag also needs a ``disclosure_flag_reason``, recorded under "Disclosure flags cleared" in the
round's live ADJUDICATION.md block (a re-apply supersedes the block, this section included).
``apply`` checks all of this before writing anything, against the rows the apply starts from (the
pre-integration backup for a re-apply), and refuses for an audit whose run.json
``protocol_version`` is 2.6.9 or later (``disclosure_policy.ENFORCED_FROM``); an older audit,
including one initialised on the live 2.6.8, gets the same messages as warnings.
Withholding language in a reason only warns.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import pathlib
import re
import shlex
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "schemas" / "tools"))
import validate_v2  # noqa: E402
import disclosure_policy  # noqa: E402  (a sibling module: this script's directory is on sys.path)

# reference/adjudication_guide.md holds the same table in prose; keep them in step.
VERDICT_OUTCOME = {"CONFIRMED": "confirmed", "OVERSTATED": "confirmed",
                   "REFUTED": "refuted", "UNVERIFIABLE": "inconclusive"}
CONFIRMING = ("confirmed", "pass", "verified")
OUTCOME_NOTE = {
    "CONFIRMED": "accepted: the claim as written was checked and held",
    "OVERSTATED": "accepted: the substance held and the requested wording change was applied, so the "
                  "record confirms the corrected claim",
    "REFUTED": "accepted as a correction: the claim as written did not hold and has been corrected",
    "UNVERIFIABLE": "noted: the critic could not verify the claim from the deposit",
}
UNVERIFIABLE_ON_UNVERIFIABLE_ROW = (
    "counted as a confirmation: this row's own claim is that the figure cannot be checked from the "
    "deposit, and the critic independently could not check it"
)


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def reviewer_rows(audit: pathlib.Path, invocation_prefix: str) -> dict[str, dict]:
    """Completed reviewer rows of this round, keyed by role.

    A cache replay (provider_call false) is resolved to the provider call it names, which is the row
    whose immutable output make_settled.sh joins; the replay copies that output byte for byte.
    """
    rows = load_jsonl(audit / "invocations.jsonl")
    by_id = {row.get("invocation_id"): row for row in rows}
    out: dict[str, dict] = {}
    for row in rows:
        if not (str(row.get("invocation_id") or "").startswith(invocation_prefix)
                and row.get("role_class") in ("critic", "adjudicator")
                and row.get("status") == "completed"):
            continue
        resolved = row
        if row.get("provider_call") is not True:
            source = by_id.get(row.get("source_invocation_id"))
            if not source or source.get("status") != "completed" or source.get("provider_call") is not True:
                raise SystemExit(f"{row.get('role')}: cache replay without a completed source provider call")
            if source.get("output_sha256") != row.get("output_sha256"):
                raise SystemExit(f"{row.get('role')}: replayed output differs from its source call")
            resolved = source
        out[str(row.get("role"))] = resolved
    return out


def auditor_rows(audit: pathlib.Path) -> dict[str, dict]:
    """Completed Phase-1 auditor rows whose recorded output is the report still on disk."""
    out: dict[str, dict] = {}
    for row in load_jsonl(audit / "invocations.jsonl"):
        if (row.get("role_class") != "auditor" or row.get("status") != "completed"
                or row.get("provider_call") is not True or row.get("identity_assurance") != "observed"):
            continue
        artifact = row.get("output_artifact")
        if artifact and (audit / artifact).is_file() and sha256(audit / artifact) == row.get("output_sha256"):
            out[str(row.get("role"))] = row
    return out


def verification_from(row: dict, outcome: str) -> dict:
    return {"reviewer_id": row["role"], "invocation_id": row["invocation_id"],
            "evidence_record": {"path": row["output_artifact"], "sha256": row["output_sha256"]},
            "model_family": row["model_family"], "outcome": outcome}


def withdrawn_by(decision: dict) -> bool:
    disposition = decision.get("ledger_disposition")
    return decision.get("status") == "rejected" or (
        isinstance(disposition, dict) and disposition.get("status") in ("EXCLUDED", "MERGED"))


def adjudicator_confirmed(entry: dict) -> bool:
    # a BLOCKING ruling is open whatever its verdict (finalize's rule), so it resolves nothing
    return any(v["role"] == "critic_synth" and v["verdict"] == "CONFIRMED" and not v.get("requested_correction")
               and v.get("priority") != "BLOCKING" for v in entry["verdicts"])


def backup_for(audit: pathlib.Path, rnd: int, invocation: str) -> pathlib.Path | None:
    """The pre-integration registry backup, if it was taken for THIS invocation of the round.

    A round run again (I4R_FORCE_RERUN, or after a renumber) was reviewed against the registry as it
    stood when it re-ran, not against the first integration's backup; a backup with no recorded
    invocation predates this check and is trusted as before."""
    backup = audit / f"work/findings.before_round{rnd}_integration.jsonl"
    if not backup.is_file():
        return None
    marker = backup.with_suffix(".invocation")
    if marker.is_file():
        return backup if marker.read_text(encoding="utf-8").strip() == invocation else None
    # No record (a backup written before this check): a round status newer than the backup means the
    # round ran again after it was taken (run_critique restores a finished round's status with cp -p).
    status = audit / f"critique/round_{rnd}/ROUND_STATUS.json"
    if status.is_file() and status.stat().st_mtime > backup.stat().st_mtime:
        return None
    return backup


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _instant(value: object, label: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit(f"FATAL: {label} is not an ISO date-time: {value!r}") from None
    if parsed.tzinfo is None:
        raise SystemExit(f"FATAL: {label} has no timezone: {value!r}")
    return parsed


def _verifier_contract():
    sys.path.insert(0, str(HERE))
    import verifier_contract  # noqa: E402  (2.6.4; imported only when a verifier row exists)
    return verifier_contract


INADMISSIBLE_REMEDY = (
    "Nothing is integrated until every verifier record in the ledger is admissible (an unknown "
    "record could hold a REFUTED for any claim). Remedy, in this order: (1) if the runner was "
    "killed after it journaled the call, so the receipt phase1/completions/<role>/<input>.json is "
    "missing, re-run that batch's own command, python3 scripts/run_verifier.py <audit-dir> "
    "--findings <the batch's rows, by their current ids>; it rebuilds the lost receipt from the "
    "retained response with no provider call. (2) Otherwise restore every changed file of the "
    "record (work/verifier/<role>/<input>/, its receipt, its frozen packet under "
    "phase1/role_packets/) to its recorded hash. (3) Only a genuinely tampered or foreign record "
    "is marked superseded: python3 scripts/record_invocation.py --correct <audit-dir> "
    "<invocation-id> <correction.json>, where correction.json is "
    "{\"set\": {\"status\": \"legacy_record\"}, \"reason\": \"...\"}; never do this for a lost "
    "receipt, which leaves that batch unrecoverable. Meanwhile make_settled.sh rejects every row "
    "that cites a verifier record."
)


class VerifierState:
    """Admitted post-fan-out verifier verdicts, computed once per integration.

    ``verdicts`` holds every admitted verdict in the ledger (the one-verdict-per-claim rule and the
    stale-claim drop need all of them), including the REFUTED/UNVERIFIABLE verdicts of calls
    corrected to legacy_record, which keep blocking their claim text; ``eligible`` only those of
    completed calls that ended no later than this integration, which are the only ones this
    integration may bind. A verifier record that fails admission (no runner receipt, a changed
    result, a wrong family, ...) is fatal.
    """

    def __init__(self, audit: pathlib.Path, at: str) -> None:
        self.present = False
        self.audit = audit
        self.verdicts: dict[str, list[tuple[str, str]]] = {}
        self.eligible: dict[str, list[tuple[str, str]]] = {}
        self.calls: dict[str, dict] = {}
        self.by_id: dict[str, dict] = {}
        self.roles: list[str] = []
        self._roles_loaded = False
        rows = load_jsonl(audit / "invocations.jsonl")
        if not any(row.get("role_class") == "verifier" for row in rows):
            return
        contract = self.contract = _verifier_contract()
        self.present = True
        self.by_id = {str(row.get("invocation_id")): row for row in rows}
        self.verdicts, errors = contract.collect_verdicts(audit, rows)
        if errors:
            raise SystemExit("FATAL: verifier record is not admissible: " + "; ".join(errors)
                             + "\n" + INADMISSIBLE_REMEDY)
        bound = _instant(at, "integration timestamp")
        for digest, entries in self.verdicts.items():
            for invocation_id, verdict in entries:
                call = self.by_id[invocation_id]
                # A call corrected to legacy_record is never bound: it confirms nothing, and its
                # REFUTED/UNVERIFIABLE blocks the claim text through ``verdicts`` alone.
                if call.get("status") != "completed":
                    continue
                if _instant(call.get("ended_at"), f"verifier {invocation_id} ended_at") > bound:
                    continue
                self.eligible.setdefault(digest, []).append((invocation_id, verdict))
                self.calls[invocation_id] = call
        if self.eligible:
            self.phase_one_roles()

    def phase_one_roles(self) -> list[str]:
        if not self._roles_loaded:
            try:
                self.roles = self.contract.required_roles(self.contract.load_snapshot_policy(self.audit))
            except self.contract.VerifierContractError as exc:
                raise SystemExit(f"FATAL: {exc}") from exc
            self._roles_loaded = True
        return self.roles

    def candidates(self, finding: dict) -> list[dict]:
        """Verification records this integration binds to an INCLUDED row raised after fan-out."""
        if not self.eligible or not self.contract.raised_after_fan_out(finding, self.roles):
            return []
        if not isinstance(finding.get("claim"), str) or not finding["claim"]:
            return []
        entries = self.eligible.get(self.contract.claim_sha256(finding["claim"])) or []
        return [verification_from(self.calls[invocation_id], self.contract.VERDICT_OUTCOME[verdict])
                for invocation_id, verdict in entries]

    def is_verifier_record(self, record: dict) -> bool:
        return (self.present and isinstance(record, dict)
                and (self.by_id.get(str(record.get("invocation_id"))) or {}).get("role_class") == "verifier")

    def applies(self, record: dict, claim: object) -> bool:
        """Whether a verifier record's call returned a verdict for this exact claim text."""
        try:
            source = self.contract.resolve_replay(self.by_id[str(record.get("invocation_id"))], self.by_id)
            digest = self.contract.claim_sha256(claim if isinstance(claim, str) else "")
        except self.contract.VerifierContractError:
            return False
        return any(invocation_id == source.get("invocation_id")
                   for invocation_id, _ in self.verdicts.get(digest) or [])

    def drop_reason(self, record: dict, row: dict) -> str | None:
        """Why a verifier record already on a row no longer belongs there, or None if it does."""
        cited = self.by_id[str(record.get("invocation_id"))]
        try:
            source = self.contract.resolve_replay(cited, self.by_id)
        except self.contract.VerifierContractError:
            source = {}
        if cited.get("status") != "completed" or source.get("status") != "completed":
            return ("its call is not a completed verifier call (it was corrected to legacy_record), "
                    "so it confirms nothing")
        if not self.contract.raised_after_fan_out(row, self.phase_one_roles()):
            return ("a Phase-1 auditor raised or independently found this row, so its auditor "
                    "confirms it")
        if not self.applies(record, row.get("claim")):
            return "the claim text is not the text it verified"
        return None

    def superseded_blocks(self, claim: object) -> list[tuple[str, str]]:
        """(invocation, verdict) of superseded calls whose non-CONFIRMED verdict blocks this text."""
        if not isinstance(claim, str) or not claim:
            return []
        return [(invocation_id, verdict)
                for invocation_id, verdict in self.verdicts.get(self.contract.claim_sha256(claim)) or []
                if (self.by_id.get(invocation_id) or {}).get("status") != "completed"]

    def verdict_of(self, record: dict, claim: str) -> str:
        """The literal verdict a bound verifier record carries for this claim text."""
        digest = self.contract.claim_sha256(claim)
        return next((verdict for invocation_id, verdict in self.verdicts.get(digest) or []
                     if invocation_id == record.get("invocation_id")), "?")


def build_plan(audit: pathlib.Path, rnd: int, at: str | None = None) -> dict:
    """Write and return the round's integration plan; ``at`` is the integration time (default now)."""
    return _build_plan(audit, rnd, at)[0]


def _build_plan(audit: pathlib.Path, rnd: int, at: str | None = None) -> tuple[dict, VerifierState]:
    round_dir = audit / f"critique/round_{rnd}"
    pointer = round_dir / "CURRENT_INVOCATION"
    if not pointer.is_file():
        raise SystemExit(f"FATAL: {pointer} not found: round {rnd} has no current run to integrate (run "
                         f"scripts/run_critique.sh {rnd} first, or check the audit directory)")
    invocation = pointer.read_text(encoding="utf-8").strip()
    if not invocation:
        # an empty pointer is a prefix of every invocation id: every round's critics would be integrated as this one's
        raise SystemExit(f"FATAL: {pointer} is empty; it must name round {rnd}'s current invocation (see "
                         f"critique/round_{rnd}/invocations/)")
    reviewers = reviewer_rows(audit, invocation)
    auditors = auditor_rows(audit)
    verifier = VerifierState(audit, at or utc_now())
    # The plan describes what the critics reviewed. apply writes a one-time backup of the registry before
    # its first change; a re-run must plan from that, not from the registry the first apply already
    # rewrote (reworded claims, withdrawn rows), or its notes misstate the reviewed wording and relabel
    # a withdrawal as a tested negative.
    backup = backup_for(audit, rnd, invocation)
    findings = load_jsonl(backup if backup is not None else audit / "work/findings.jsonl")
    by_claim: dict[str, list[dict]] = collections.defaultdict(list)
    new_issues: list[dict] = []
    conflicts: list[dict] = []
    spot_failures: list[dict] = []
    overall: dict[str, dict] = {}
    for role, row in sorted(reviewers.items()):
        obj = json.loads((audit / row["output_artifact"]).read_text(encoding="utf-8"))
        overall[role] = {k: (obj.get("overall") or {}).get(k) for k in ("publishable", "blocking_count", "summary")}
        overall[role]["attempt"] = row.get("attempt")
        for item in obj.get("items") or []:
            by_claim[item.get("finding_id") or item["item_id"]].append({
                "role": role, "item_id": item["item_id"], "verdict": item["verdict"],
                "priority": item["priority"], "confidence": item["confidence"],
                "requested_correction": item.get("requested_correction"), "claim": item.get("claim"),
                "result": (item.get("evidence") or {}).get("result")})
        for issue in obj.get("new_issues") or []:
            new_issues.append({"role": role, **{k: issue.get(k) for k in
                               ("item_id", "finding_id", "claim", "verdict", "priority",
                                "confidence", "requested_correction")}})
        for conflict in obj.get("conflicts") or []:
            conflicts.append({"role": role, **conflict})
        for check in obj.get("settled_spot_checks") or []:
            if check.get("status") == "failed":
                spot_failures.append({"role": role, **check})

    plan_rows = []
    for finding in findings:
        included = finding["ledger_disposition"]["status"] == "INCLUDED"
        verdicts = by_claim.get(finding["id"], [])
        if not included and not verdicts:
            continue
        candidates = []
        if included:
            for role in dict.fromkeys([finding["provenance"]["origin_role"],
                                       *finding["provenance"]["independently_found_by"]]):
                if role in auditors:
                    candidates.append(verification_from(auditors[role], "confirmed"))
            for verdict in verdicts:
                outcome = VERDICT_OUTCOME[verdict["verdict"]]
                if verdict["verdict"] == "UNVERIFIABLE" and finding.get("impact") == "unverifiable":
                    outcome = "confirmed"
                candidates.append(verification_from(reviewers[verdict["role"]], outcome))
        plan_row = {
            "id": finding["id"], "claim": finding["claim"], "status": finding["status"],
            "included": included, "coordinator_approved": finding.get("coordinator_approved"),
            "impact": finding.get("impact"), "changes_conclusion": finding.get("changes_conclusion"),
            "verdicts": verdicts, "candidate_verifications": candidates}
        # Post-fan-out verifier records (2.6.4): a separate key, emitted only when non-empty, so a
        # plan without them is byte-identical to 2.6.3 and apply binds them in every round.
        verifier_candidates = verifier.candidates(finding) if included else []
        if verifier_candidates:
            plan_row["verifier_verifications"] = verifier_candidates
        plan_rows.append(plan_row)

    # items whose finding_id is null or names no registry row (the schema allows it): kept, so they reach the
    # round's notes and counts instead of vanishing
    registry_ids = {finding["id"] for finding in findings}
    general_items = [verdict for key, verdicts in by_claim.items() if key not in registry_ids for verdict in verdicts]
    plan = {"plan_version": "i4r-round-integration-1", "round": rnd, "invocation": invocation,
            "reviewers": {r: {k: row.get(k) for k in ("invocation_id", "output_artifact",
                                                      "output_sha256", "attempt", "model_family")}
                          for r, row in reviewers.items()},
            "auditors": {r: row["invocation_id"] for r, row in auditors.items()},
            "overall": overall, "findings": plan_rows, "new_issues": new_issues, "general_items": general_items,
            "conflicts": conflicts, "settled_spot_check_failures": spot_failures}
    bound_calls = list(dict.fromkeys(v["invocation_id"] for row in plan_rows
                                     for v in row.get("verifier_verifications", ())))
    if bound_calls:
        # The verifier calls whose verdicts this plan binds, with every verdict each returned.
        plan["verifiers"] = {
            invocation_id: {
                **{k: verifier.calls[invocation_id].get(k)
                   for k in ("role", "output_artifact", "output_sha256", "model_family", "ended_at")},
                "verdicts": {digest: verdict for digest, entries in sorted(verifier.eligible.items())
                             for inv, verdict in entries if inv == invocation_id},
            } for invocation_id in bound_calls}
    (round_dir / "INTEGRATION_PLAN.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return plan, verifier


APPROVED_STATUSES = {"confirmed", "verified", "accepted"}


def approval_fields_missing(row: dict) -> list[str]:
    """The fields validate_v2 demands of an approved row that this row lacks."""
    if not (row.get("status") in APPROVED_STATUSES and row.get("coordinator_approved") is True):
        return []
    missing = []
    if not (isinstance(row.get("report_section"), str) and row["report_section"].strip()):
        missing.append("report_section")
    snippet = row.get("verification_snippet")
    if not (isinstance(snippet, dict) and all(snippet.get(k) for k in ("kind", "path", "basis"))):
        missing.append("verification_snippet")
    if row.get("changes_conclusion") is True and not (
            isinstance(row.get("changes_conclusion_which"), str) and row["changes_conclusion_which"].strip()):
        missing.append("changes_conclusion_which")
    return missing


def later_rounds(audit: pathlib.Path, rnd: int) -> list[int]:
    """Rounds after rnd that have run (a status) or been integrated: their critics reviewed, and their
    backups recorded, the registry as it stood after rnd's integration."""
    found = set()
    # critic/adjudicator calls per round from the ledger (the status's provider_calls also counts the model
    # probe, so a round that only failed its probe would look dispatched)
    dispatched = set()
    ledger = audit / "invocations.jsonl"
    if ledger.is_file():
        for row in load_jsonl(ledger):
            if row.get("role_class") in ("critic", "adjudicator") and row.get("provider_call") is True \
                    and isinstance(row.get("round"), int):
                dispatched.add(row["round"])
    for name in ("ROUND_STATUS.json", "INTEGRATION_SUMMARY.json"):
        for path in audit.glob(f"critique/round_*/{name}"):
            number = path.parent.name.split("_", 1)[1]
            if not (number.isdigit() and int(number) > rnd):
                continue
            if name == "ROUND_STATUS.json":
                try:
                    status = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    status = {}
                # only rounds that dispatched count: a prepared (paused) round, or one refused at a
                # precondition, wrote a status but no critic reviewed anything
                # a round whose critics are still running is reviewing the registry right now
                if status.get("outcome") != "running" and not status.get("completed_roles") \
                        and int(number) not in dispatched:
                    continue
            found.add(int(number))
    return sorted(found)


def apply(audit: pathlib.Path, rnd: int, decisions: dict, contested: dict, now: str,
          contested_path: str | None = None) -> int:
    later = later_rounds(audit, rnd)
    if later:
        # Checked before anything is rebuilt. Round M reviewed the registry round rnd's integration left; a
        # re-apply now would undo round M's integration or change what M reviewed underneath it (a claim
        # reworded here would be bound to M's confirmation of the old wording).
        verb = "re-applying" if (audit / f"critique/round_{rnd}/INTEGRATION_SUMMARY.json").is_file() else "applying"
        print(f"FATAL: round {later[0]} has already run after round {rnd}; {verb} round {rnd} now would change "
              f"the registry round {later[0]} reviewed. Make the change in the next round's integration instead.",
              file=sys.stderr)
        return 1
    # 2.6.8: a disclosure finding is reported, never withheld (scripts/disclosure_policy.py). Checked
    # before anything is written, so a refused decision leaves the plan, the backup and the registry
    # exactly as they were. Refusals only for an audit initialised at 2.6.9 or later; an older audit
    # (the live 2.6.8 included) gets the same messages as warnings, so an audit in flight is not stopped
    # by the upgrade.
    run_path = audit / "run.json"
    try:
        run_record = json.loads(run_path.read_text(encoding="utf-8"))
        protocol_version = run_record.get("protocol_version")
    except (OSError, ValueError, AttributeError):
        run_record, protocol_version = None, None
    # Judged against the rows this apply starts from: a re-apply of the same invocation starts from
    # its pre-integration backup (backup_for, below), not from the registry the first apply rewrote.
    pointer = audit / f"critique/round_{rnd}/CURRENT_INVOCATION"
    invocation = pointer.read_text(encoding="utf-8").strip() if pointer.is_file() else ""
    start = backup_for(audit, rnd, invocation) if invocation else None
    problems, notes = disclosure_policy.decision_review(
        load_jsonl(start if start is not None else audit / "work/findings.jsonl"), decisions)
    for message in notes:
        print(f"WARNING: {message}", file=sys.stderr)
    if problems and disclosure_policy.enforced(protocol_version, run_record):   # 2.6.9: or reopened under 2.6.9+
        for message in problems:
            print(f"FATAL: {message}", file=sys.stderr)
        return 1
    for message in problems:
        print(f"WARNING: {message}{disclosure_policy.LEGACY_NOTE}", file=sys.stderr)
    plan, verifier = _build_plan(audit, rnd, now)
    round_dir = audit / f"critique/round_{rnd}"
    # a contest keyed to no verdict (a typo, or "stats:" for "critic_stats:") was silently ignored and the verdict
    # recorded as accepted
    # every item and new issue of every reviewer output (finalize's open-item check reads them all, including
    # items with no finding_id, which the plan's per-finding rows leave out)
    valid_keys = {f"{v['role']}:{v['item_id']}" for entry in plan["findings"] for v in entry["verdicts"]} \
        | {f"{issue.get('role')}:{issue.get('item_id')}" for issue in plan["new_issues"]}
    for role, reviewer in plan["reviewers"].items():
        try:
            output = json.loads((audit / str(reviewer.get("output_artifact"))).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        valid_keys |= {f"{role}:{item.get('item_id')}" for kind in ("items", "new_issues")
                       for item in (output.get(kind) or []) if isinstance(item, dict)}
    stray_contests = sorted(k for k in contested if k not in valid_keys)
    if stray_contests:
        print(f"FATAL: --contested keys match no verdict of round {rnd}: {stray_contests}. Valid keys "
              f"(critic_<role>:<item_id>): {sorted(valid_keys)}", file=sys.stderr)
        return 1
    # contesting a verdict while the same decision acts on it records the correction as contested and undercounts
    # factual_corrections. A withdrawal answers every verdict on the finding; a rewording answers the verdicts it
    # implements, so it conflicts only when every correction-requesting verdict on that finding is contested (one
    # critic's correction may be applied while another's, e.g. "withdraw it", is contested)
    self_contradicting = []
    for entry in plan["findings"]:
        decision = decisions.get(entry["id"]) or {}
        keys = [f"{v['role']}:{v['item_id']}" for v in entry["verdicts"]]
        contested_here = [k for k in keys if k in contested]
        if not contested_here:
            continue
        reworded = "claim" in decision and decision.get("claim") != entry.get("claim")
        correcting = [f"{v['role']}:{v['item_id']}" for v in entry["verdicts"]
                      if v["verdict"] in ("OVERSTATED", "REFUTED") and v.get("requested_correction")]
        if withdrawn_by(decision) or (reworded and correcting and all(k in contested for k in correcting)):
            self_contradicting += [f"{k} ({entry['id']})" for k in contested_here]
    self_contradicting.sort()
    if self_contradicting:
        print(f"FATAL: --contested contests verdicts whose findings these decisions reword or withdraw: "
              f"{self_contradicting}. Acting on a verdict answers it; remove those entries from the contested file "
              "(and from critique/round_N/CONTESTED.json), then re-run apply.", file=sys.stderr)
        return 1
    stale_contest = round_dir / "CONTESTED.json"
    passed_same = contested_path is not None and pathlib.Path(contested_path).is_file() \
        and stale_contest.is_file() and pathlib.Path(contested_path).samefile(stale_contest)
    # finalize reads critique/round_N/CONTESTED.json: contests applied from another file are written there (below),
    # and a different file already there must be reconciled first
    write_round_contests = False
    if contested_path is not None and not passed_same:
        try:
            existing = json.loads(stale_contest.read_text(encoding="utf-8")) if stale_contest.is_file() else None
        except ValueError:
            existing = "unreadable"
        if existing is None:
            write_round_contests = bool(contested)
        elif existing != contested:
            print(f"FATAL: --contested {contested_path} differs from {stale_contest.relative_to(audit)}, which finalize "
                  "and the release read. Pass that file with --contested (after updating it), or remove it so apply "
                  "writes the contests passed; nothing was written.", file=sys.stderr)
            return 1
    elif stale_contest.is_file() and contested_path is None:
        print(f"note: {stale_contest.relative_to(audit)} exists but --contested was not passed, so none of its entries is "
              "applied here; pass it with --contested for the contests you keep (finalize reads the file for the last "
              "round), and remove only entries for verdicts this integration now acts on", file=sys.stderr)
    registry_path = audit / "work/findings.jsonl"
    # apply writes the registry, the response notes and the adjudication block together; check the
    # last one exists before touching the first, so a missing log cannot leave a partial write.
    if not (audit / "report/ADJUDICATION.md").is_file():
        print(f"FATAL: {audit / 'report/ADJUDICATION.md'} not found; the coordinator creates it, "
              "and apply appends the round's adjudication block to it", file=sys.stderr)
        return 1
    backup = backup_for(audit, rnd, plan["invocation"])
    fresh_backup: list = []   # a backup this call created; a refused apply removes it (it is write-once)

    def refuse() -> int:
        for path in fresh_backup:
            path.unlink(missing_ok=True)
        return 1
    if backup is None:
        # first apply for this invocation: record what its critics reviewed
        backup = audit / f"work/findings.before_round{rnd}_integration.jsonl"
        marker = backup.with_suffix(".invocation")
        rows = load_jsonl(registry_path)
        backup.write_bytes(registry_path.read_bytes())
        marker.write_text(plan["invocation"] + "\n", encoding="utf-8")
        fresh_backup += [backup, marker]
    else:
        # a re-apply of the same invocation starts from what the critics reviewed, so a decision taken
        # back (a rewording, a withdrawal) is undone rather than silently kept from the first apply
        rows = load_jsonl(backup)
        # ...which would also discard a row added or a claim edited by hand since that apply (the
        # coordinator adds a critic's new issue by hand for the verifier). Refuse rather than drop it.
        written = backup.with_name(f"findings.after_round{rnd}_integration.claims.json")
        if written.is_file():
            try:
                before = json.loads(written.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                before = None
            live = {row.get("id"): row.get("claim") for row in load_jsonl(registry_path)}
            # a renumber moves ids but keeps every claim: only added, removed or edited claims count
            if isinstance(before, dict) and live != before and sorted(map(str, live.values())) != sorted(map(str, before.values())):
                changed = sorted(str(key) for key in set(live) | set(before) if live.get(key) != before.get(key))
                print(f"FATAL: work/findings.jsonl changed since round {rnd}'s apply (rows added, removed or with a "
                      f"different claim: {', '.join(changed[:8])}{'...' if len(changed) > 8 else ''}). A re-apply "
                      f"starts from what round {rnd}'s critics reviewed and would discard that. Make a claim change "
                      "through the next round's decisions; keep a row added for a later round (a critic's new "
                      "issue) and integrate it with that round instead of re-applying this one.", file=sys.stderr)
                return 1
    # A decisions file keyed by anything but registry ids (the plan file itself, a wrapper object)
    # would otherwise apply nothing and still report success.
    unknown = sorted(set(decisions) - {row["id"] for row in rows})
    if unknown:
        print("FATAL: decisions file has keys that are not finding ids in work/findings.jsonl: "
              + ", ".join(unknown[:8]) + ("..." if len(unknown) > 8 else "")
              + ". It must map finding id -> decided fields (see --help); it is not INTEGRATION_PLAN.json.",
              file=sys.stderr)
        return refuse()
    backup_rows = {row["id"]: dict(row) for row in rows}
    plan_by_id = {row["id"]: row for row in plan["findings"]}
    # Only this invocation's reviewers can back this round. A record bound from an earlier run of the
    # same round (a forced rerun, a rerun after a renumber, a legacy backup, an adjudicator-retry replay
    # resolved to an older call) reviewed an earlier registry; drop it on every apply, not just the first.
    ledger = {row.get("invocation_id"): row for row in load_jsonl(audit / "invocations.jsonl")}
    current = {r.get("invocation_id") for r in plan["reviewers"].values()}
    for row in rows:
        row["verifications"] = [
            v for v in row.get("verifications") or []
            if not ((ledger.get(v.get("invocation_id")) or {}).get("round") == rnd
                    and (ledger.get(v.get("invocation_id")) or {}).get("role_class") in ("critic", "adjudicator")
                    and v.get("invocation_id") not in current)]

    # An uncontested OVERSTATED/REFUTED verdict that asked for a correction must be acted on: either the
    # decision changes the claim text, or it says what was changed (applied_note), or the verdict is
    # contested. Otherwise the notes claimed "correction applied" for nothing, and the overstated claim
    # was bound as confirmed and later settled.
    # The targeted adjudication's CONFIRMED ruling (no correction requested) resolves the critics' items on
    # that finding, as finalize's converged gate treats it (INSTALL.md, CONTESTED.json).
    unacted = []
    for entry in plan["findings"]:
        if not entry["included"] or adjudicator_confirmed(entry):
            continue
        decision = decisions.get(entry["id"]) or {}
        acted = ("claim" in decision and decision["claim"] != entry.get("claim")) or decision.get("applied_note") \
            or withdrawn_by(decision)
        for verdict in entry["verdicts"]:
            if verdict["verdict"] in ("OVERSTATED", "REFUTED") and verdict.get("requested_correction") \
                    and f"{verdict['role']}:{verdict['item_id']}" not in contested and not acted:
                unacted.append(f"{entry['id']} ({verdict['role']}:{verdict['item_id']} {verdict['verdict']})")
    if unacted:
        print("FATAL: these verdicts asked for a correction the decisions do not act on: " + ", ".join(unacted)
              + ". (If this round may end the audit, only a contest, or another critique round, answers them: after "
              "the last round the report may not change.) For each, change the finding's \"claim\" (and the report "
              "prose), give an \"applied_note\" "
              "saying what changed, withdraw the finding (\"status\": \"rejected\" with a ledger_disposition), or "
              f"contest it in a JSON file passed as --contested <file> (e.g. {shlex.quote(str(audit / 'critique' / f'round_{rnd}' / 'CONTESTED.json'))}) holding "
              "{\"critic_<role>:<item_id>\": {\"position\": ..., \"evidence\": ...}}.", file=sys.stderr)
        return refuse()

    unbound: dict[str, list[str]] = {}
    bound_now: set[tuple[str, str]] = set()
    verifier_dropped: dict[str, list[dict]] = {}

    for row in rows:
        entry = plan_by_id.get(row["id"])
        decision = decisions.get(row["id"]) or {}
        if entry and entry["included"]:
            # a refuted/inconclusive record does not stop that reviewer's later confirmation from binding
            existing = {v["reviewer_id"] for v in row["verifications"] if v["outcome"] in CONFIRMING}
            # an OVERSTATED record confirms the CORRECTED claim only: bind it only if the claim changed here
            reworded = "claim" in decision and decision["claim"] != entry.get("claim")
            overstated = {v["role"] for v in entry["verdicts"] if v["verdict"] == "OVERSTATED"}
            if rnd > 1 and not adjudicator_confirmed(entry):
                # A reviewer that again calls the current wording OVERSTATED/REFUTED no longer confirms it: an
                # earlier record of it (bound to a previous rewording) must not keep counting for SETTLED. The
                # targeted adjudication's CONFIRMED ruling resolves that dispute (as make_settled and finalize read it).
                disputed = {v["role"] for v in entry["verdicts"] if v["verdict"] in ("OVERSTATED", "REFUTED")}
                if disputed and not reworded:
                    dropped = [v["reviewer_id"] for v in row["verifications"] if v["reviewer_id"] in disputed]
                    if dropped:
                        unbound[row["id"]] = dropped
                    row["verifications"] = [v for v in row["verifications"] if v["reviewer_id"] not in disputed]
                    existing -= disputed
            # Every round binds a reviewer's record the first time it confirms the claim: a literal CONFIRMED, or
            # an OVERSTATED when the claim is reworded here (the record then confirms the corrected wording).
            for candidate in entry["candidate_verifications"]:
                if candidate["reviewer_id"] in overstated and not reworded:
                    if rnd <= 1:
                        unbound.setdefault(row["id"], []).append(candidate["reviewer_id"])
                    continue
                if candidate["reviewer_id"] in existing:
                    continue
                if candidate["outcome"] in CONFIRMING:
                    row["verifications"] = [v for v in row["verifications"]
                                            if v["reviewer_id"] != candidate["reviewer_id"]]
                elif any(v["reviewer_id"] == candidate["reviewer_id"] for v in row["verifications"]):
                    continue   # keep the reviewer's first non-confirming record
                row["verifications"].append(candidate)
                if candidate["outcome"] in CONFIRMING:
                    existing.add(candidate["reviewer_id"])
                    bound_now.add((row["id"], candidate["reviewer_id"]))
            if rnd > 1 and decision.get("rebind_literal_confirmations"):
                if reworded:
                    # this round's CONFIRMED verdicts are on the wording just replaced
                    print(f"note: {row['id']}: rebind_literal_confirmations ignored -- the claim is reworded in this "
                          "decision, so this round's CONFIRMED verdicts are on the old wording", file=sys.stderr)
                else:
                    literal = {v["role"] for v in entry["verdicts"] if v["verdict"] == "CONFIRMED"}
                    for candidate in entry["candidate_verifications"]:
                        if candidate["reviewer_id"] in literal and candidate["outcome"] == "confirmed":
                            row["verifications"] = [v for v in row["verifications"]
                                                    if v["reviewer_id"] != candidate["reviewer_id"]]
                            row["verifications"].append(candidate)
                            bound_now.add((row["id"], candidate["reviewer_id"]))
            # Verifier records bind in every round, not only round 1: a row raised after fan-out
            # may first be verified before any later round. A reviewer appears once per row.
            for candidate in entry.get("verifier_verifications", ()):
                row["verifications"] = [v for v in row["verifications"]
                                        if v["reviewer_id"] != candidate["reviewer_id"]]
                row["verifications"].append(candidate)
        for key in ("status", "coordinator_approved", "claim", "impact", "changes_conclusion",
                    "changes_conclusion_which", "report_section", "benign_explanations_ruled_out", "verification_snippet",
                    "remedy", "disclosure_flag", "confidence", "ledger_disposition", "unverifiable_basis"):
            if key in decision:
                row[key] = decision[key]
        # a withdrawn finding is no longer an approved one (the notes printed "rejected, coordinator_approved True")
        if withdrawn_by(decision) and "coordinator_approved" not in decision:
            row["coordinator_approved"] = False
        # unverifiable_basis belongs to impact "unverifiable" only (schema): leaving it clears the basis
        if isinstance(decision.get("impact"), str) and decision["impact"] != "unverifiable" \
                and "unverifiable_basis" not in decision:
            row["unverifiable_basis"] = None
        # impact_rank is derived, never decided: a decision that re-classifies impact would
        # otherwise keep the old rank and fail validation with no field left to set.
        if isinstance(decision.get("impact"), str) and decision["impact"] in validate_v2.IMPACT_RANK:
            row["impact_rank"] = validate_v2.IMPACT_RANK[decision["impact"]]
        for key, path in (("evidence_rerun_command", "rerun_command"),
                          ("evidence_observed", "observed"), ("evidence_expected", "expected")):
            if key in decision:
                row["evidence"][path] = decision[key]
        basis = disclosure_policy.parse_basis(decision.get(disclosure_policy.BASIS_KEY))
        flagged = (row.get("disclosure_flag") is True
                   or (backup_rows.get(row["id"]) or {}).get("disclosure_flag") is True)
        if basis and flagged and disclosure_policy.disposition_status(row) == "EXCLUDED":
            # The registry row cannot carry the decision key, so its ledger reason opens with the
            # basis label ("Refuted: ...") and finalize reads the basis back from there.
            row["ledger_disposition"] = dict(row["ledger_disposition"], reason=disclosure_policy.labelled_reason(
                row["ledger_disposition"].get("reason"), basis))
        if verifier.present:
            # A verifier verdict binds to the claim text it was run on. Once the claim is edited
            # (by this decision or by hand since) the record no longer applies; left in place it
            # would reach make_settled as a confirmation of a text nobody verified. A record whose
            # call was superseded, or on a row a Phase-1 auditor raised (make_settled rejects that
            # row), is dropped the same way, each with its reason.
            stale = [(v, reason) for v in row["verifications"] if verifier.is_verifier_record(v)
                     for reason in [verifier.drop_reason(v, row)] if reason]
            if stale:
                dropped = [v for v, _ in stale]
                row["verifications"] = [v for v in row["verifications"] if v not in dropped]
                verifier_dropped[row["id"]] = stale

    missing = [(row["id"], approval_fields_missing(row)) for row in rows if approval_fields_missing(row)]
    if missing:
        for finding_id, fields in missing:
            print(f"FATAL: {finding_id} is approved but its decision supplies no {' or '.join(fields)}. "
                  "Add them to its entry in decisions.json, e.g. \"report_section\": \"Section 3\", "
                  "\"verification_snippet\": {\"kind\": \"code\", \"path\": "
                  "\"work/coordinator/verification_R/fN_slug.R\", \"basis\": \"...\"}", file=sys.stderr)
        return refuse()
    seen: set[str] = set()
    for index, row in enumerate(rows, 1):
        if row["id"] in seen:
            print(f"FATAL: duplicate finding id {row['id']}", file=sys.stderr)
            return refuse()
        seen.add(row["id"])
        # the registry schema too, before anything is written: a decision value of the wrong type (e.g. a string
        # remedy) otherwise lands in work/findings.jsonl and fails only later, in make_diff_packet or the projection
        import validate_json_schema
        schema_errors = list(validate_json_schema.errors_for_value(
            row, pathlib.Path(__file__).resolve().parent.parent / "schemas/v2/finding-row.schema.json"))
        if schema_errors:
            cause = (f"Fix the decision for {row['id']} (the field named above)" if row["id"] in decisions else
                     f"No decision touches {row['id']}: the row itself (as round {rnd} reviewed it) is invalid; fix it "
                     "with a decision for that field, or repair the registry and run the round again")
            print(f"FATAL: {row['id']} would not match the finding schema after these decisions: "
                  + "; ".join(schema_errors[:5]) + f". {cause}, then re-run apply; nothing was written.", file=sys.stderr)
            return refuse()
        try:
            validate_v2.validate_finding_row(row, f"line {index} ({row['id']})")
        except (ValueError, validate_v2.ValidationError) as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            if row["id"] in unbound and row.get("coordinator_approved") and ".verifications" in str(exc):
                who = ", ".join(unbound[row["id"]])
                entry = plan_by_id.get(row["id"]) or {"verdicts": []}
                bound = {v["reviewer_id"] for v in row["verifications"]}
                rebindable = rnd > 1 and any(v["verdict"] == "CONFIRMED" and v["role"] not in bound
                                             for v in entry["verdicts"])
                print(f"  {row['id']}: {who} called the claim OVERSTATED/REFUTED and its text is unchanged, so "
                      + ("that record does not confirm it (an applied_note does not make it a confirmation)"
                         if rnd <= 1 else "the earlier record no longer confirms it")
                      + "; contesting the verdict does not restore it. Reword the claim (\"claim\"), "
                      + ("rebind this round's literal confirmations (\"rebind_literal_confirmations\": true), "
                         if rebindable else "")
                      + "leave it unapproved (\"coordinator_approved\": false) until a later round confirms it "
                        "literally, or withdraw it.", file=sys.stderr)
            return refuse()
    tmp = registry_path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(registry_path)
    # what this apply wrote (ids and claims), so a later re-apply can tell whether it would discard hand edits
    (audit / f"work/findings.after_round{rnd}_integration.claims.json").write_text(
        json.dumps({r.get("id"): r.get("claim") for r in rows}, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8")

    accepted = contested_n = 0
    corrected_ids = set()  # factual_corrections counts corrected FINDINGS, not verdicts on them
    by_id = {row["id"]: row for row in rows}
    verifier_bound = {entry["id"]: [c for c in entry.get("verifier_verifications", ())
                                    if c in by_id[entry["id"]]["verifications"]]
                      for entry in plan["findings"] if entry.get("verifier_verifications")}
    lines = [f"# Round {rnd} critique: coordinator response", "",
             f"Invocation `{plan['invocation']}`; integrated {now}. Reviewers: "
             + ", ".join(f"{r} (attempt {o['attempt']})" for r, o in sorted(plan["overall"].items())) + ".", ""]
    for role, o in sorted(plan["overall"].items()):
        lines.append(f"- **{role}**: publishable={o['publishable']}, blocking={o['blocking_count']}. {o['summary']}")
    lines += ["", "## Per-claim responses", ""]
    adjudication = ["", "---", "", f"<!-- adjudication:round-{rnd} -->",
                    f"## Round {rnd} critique integration ({now})", "",
                    f"Codex critics reviewed the frozen draft (invocation `{plan['invocation']}`). Verdicts were "
                    "merged by claim; each accepted correction was applied to the report and, where the claim text "
                    "changed, to the registry row. " + ("Confirmations from a Phase-1 auditor and a round-1 critic "
                    "are bound to the included rows as verification records." if rnd <= 1 else
                    "Literal confirmations of corrected claims replace the records they supersede.")
                    + (" Post-fan-out verifier verdicts are bound, by the SHA-256 of the claim text they "
                       "were run on, to the included rows no Phase-1 auditor raised."
                       if any(verifier_bound.values()) else ""), ""]
    live = {row["id"]: row for row in load_jsonl(registry_path)}
    for entry in plan["findings"]:
        row = live[entry["id"]]
        decision = decisions.get(entry["id"]) or {}
        marker = "" if entry["included"] else " (tested negative)"
        disposition = row.get("ledger_disposition") or {}
        # A row the coordinator withdrew: the critics' CONFIRMED verdicts were overridden, not
        # accepted, and the reason must survive in the notes and ADJUDICATION.md, not only the JSONL.
        # A row withdrawn in an EARLIER round is no longer INCLUDED at plan time, but it is still a withdrawn
        # finding, not a tested negative, whenever its status says rejected.
        withdrawn = row.get("status") == "rejected" or (
            entry["included"] and disposition.get("status") in ("EXCLUDED", "MERGED"))
        if withdrawn and not entry["included"]:
            marker = " (withdrawn in an earlier round)"
        withdrawn_reason = "; ".join(str(x) for x in (disposition.get("reason"), decision.get("applied_note")) if x) \
            or "no reason recorded"
        lines.append(f"### {entry['id']}{marker}: {row['claim'][:140]}{'…' if len(row['claim']) > 140 else ''}")
        if entry.get("claim") is not None and entry["claim"] != row["claim"]:
            # The verdicts below were given on the wording the critics reviewed, not the heading's.
            lines.append(f"- reviewed wording (reworded at this integration): {entry['claim']}")
        for verdict in entry["verdicts"]:
            key = f"{verdict['role']}:{verdict['item_id']}"
            if key in contested:
                contested_n += 1
                position = contested[key]
                lines.append(f"- {verdict['role']}: **{verdict['verdict']}** ({verdict['priority']}, "
                             f"{verdict['confidence']}) — **contested**: {position.get('position','')} "
                             f"Evidence: {position.get('evidence','')}")
                continue
            accepted += 1
            note = OUTCOME_NOTE.get(verdict["verdict"], "noted")
            if verdict["verdict"] == "UNVERIFIABLE" and row.get("impact") == "unverifiable":
                note = UNVERIFIABLE_ON_UNVERIFIABLE_ROW
            if withdrawn and verdict["verdict"] == "CONFIRMED":
                note = "overridden: the claim held, but the coordinator withdrew the finding (see decision)"
            # only an included row's correction can have been applied to the report
            acted = entry["included"] and (decision.get("applied_note")
                                           or ("claim" in decision and decision["claim"] != entry.get("claim")))
            withdrawn_now = entry["included"] and withdrawn_by(decision)
            reworded_here = "claim" in decision and decision["claim"] != entry.get("claim")
            overruled = (entry["included"] and not withdrawn_now and not reworded_here
                         and adjudicator_confirmed(entry) and verdict["role"] != "critic_synth")
            if withdrawn_now and verdict["verdict"] in ("OVERSTATED", "REFUTED"):
                note = "accepted: the coordinator withdrew the finding (see decision)"
            elif overruled and verdict["verdict"] in ("OVERSTATED", "REFUTED"):
                note = "overruled: the targeted adjudication confirmed the claim as written"
                acted = False
            elif verdict["verdict"] == "OVERSTATED" and entry["included"] and acted and not reworded_here:
                note = ("accepted: the requested change was applied in the report; the claim text is unchanged, "
                        "so this record does not confirm it"
                        + (" and an earlier record of this reviewer was unbound"
                           if rnd > 1 and verdict["role"] in unbound.get(entry["id"], []) else ""))
            elif verdict["verdict"] == "OVERSTATED" and entry["included"] and reworded_here \
                    and (entry["id"], verdict["role"]) not in bound_now:
                note = ("accepted: the claim was reworded as requested; this reviewer already had a record on the "
                        "finding, which was kept (this round's record is not bound)")
            correction = verdict["verdict"] in ("OVERSTATED", "REFUTED") and verdict.get("requested_correction")
            if correction and not acted and not withdrawn_now:
                if not entry["included"]:
                    note = ("not applied: the finding was not included in this round's report"
                            + (" (withdrawn earlier)" if withdrawn else ""))
                elif overruled and not note.startswith("overruled:"):
                    note = "overruled: the targeted adjudication confirmed the claim as written"
            lines.append(f"- {verdict['role']}: **{verdict['verdict']}** ({verdict['priority']}, "
                         f"{verdict['confidence']}) — {note}.")
            if correction:
                lines.append(f"  - requested: {verdict['requested_correction']}")
                if acted or withdrawn_now:
                    corrected_ids.add(entry["id"])
                    lines.append(f"  - applied: {decision.get('applied_note') or ('claim reworded: ' + row['claim'] if not withdrawn_now else 'finding withdrawn')}")
                elif decision.get("applied_note") and entry["included"]:
                    # e.g. the adjudication overruled the critic but the coordinator still edited the report
                    lines.append(f"  - applied (not counted as a correction): {decision['applied_note']}")
                else:
                    lines.append("  - applied: nothing")
        verifier_lines = []
        for record in verifier_bound.get(entry["id"], ()):
            verifier_lines.append(
                f"- verifier `{record['reviewer_id']}` ({record['model_family']}; invocation "
                f"`{record['invocation_id']}`): **{verifier.verdict_of(record, row['claim'])}** on the "
                f"current claim text — bound as a `{record['outcome']}` verification record.")
        for record, reason in verifier_dropped.get(entry["id"], ()):
            verifier_lines.append(
                f"- verifier `{record['reviewer_id']}` (invocation `{record['invocation_id']}`): record "
                f"dropped — {reason}.")
        blocks = verifier.superseded_blocks(row.get("claim")) if verifier.present and entry["included"] else []
        if blocks and verifier.contract.raised_after_fan_out(row, verifier.phase_one_roles()):
            for invocation_id, verdict in blocks:
                verifier_lines.append(
                    f"- verifier invocation `{invocation_id}` (corrected to legacy_record): returned "
                    f"**{verdict}** for the current claim text — not bound, but it still blocks a "
                    "verifier confirmation of this text.")
        lines += verifier_lines
        families = sorted({v["model_family"] for v in row["verifications"]
                           if v["outcome"] in ("confirmed", "pass", "verified")})
        lines.append(f"- decision: status `{row['status']}`, coordinator_approved "
                     f"`{row['coordinator_approved']}`; confirming families {families}.")
        if withdrawn:
            lines.append(f"- withdrawn ({disposition.get('status') or row.get('status')}): {withdrawn_reason}")
        lines.append("")
        adjudication.append(f"<!-- adjudication:{entry['id']}:round{rnd} -->")
        adjudication.append(f"### A-{entry['id']} round {rnd} — "
                            + "; ".join(f"{v['role'].removeprefix('critic_')} {v['verdict']}"
                                        for v in entry["verdicts"]))
        was_contested = any(f"{v['role']}:{v['item_id']}" in contested for v in entry["verdicts"])
        adjudication.append(
            ("Decision: CONTESTED (accepted in part). " if was_contested else "")
            + f"Status `{row['status']}`, approved `{row['coordinator_approved']}`. "
            + (f"Claim text revised: {row['claim']}" if entry.get("claim") != row["claim"] else "Claim text unchanged.")
            + (f" Withdrawn ({disposition.get('status') or row.get('status')}): {withdrawn_reason}" if withdrawn else ""))
        for verdict in entry["verdicts"]:
            if verdict["verdict"] != "CONFIRMED":
                adjudication.append(f"- {verdict['role']} {verdict['verdict']}: {verdict.get('result') or ''} "
                                    f"Correction: {verdict.get('requested_correction') or 'none'}")
        adjudication += verifier_lines
        adjudication.append("")

    cleared = [(finding_id, decision["disclosure_flag_reason"], decision.get(disclosure_policy.BASIS_KEY))
               for finding_id, decision in decisions.items()
               if isinstance(decision, dict) and decision.get("disclosure_flag") is False
               and isinstance(decision.get("disclosure_flag_reason"), str)
               and (backup_rows.get(finding_id) or {}).get("disclosure_flag") is True]

    def cleared_line(finding_id: str, reason: str, basis: object) -> str:
        # Each entry carries its own marker, so a re-applied round records a flag it clears later.
        return (f"- {finding_id}: {reason}" + (f" (basis: {basis})" if isinstance(basis, str) else "")
                + f" <!-- adjudication:round-{rnd}:disclosure-cleared:{finding_id} -->")

    if cleared:
        # Emitted only when a flag was cleared, so every other integration is byte-identical.
        adjudication.append("### Disclosure flags cleared")
        adjudication += [cleared_line(*entry) for entry in cleared]
        adjudication.append("")
    lines += ["## Conflicts declared by critics", ""]
    for conflict in plan["conflicts"]:
        lines.append(f"- {conflict['role']} {conflict.get('conflict_id','')} on "
                     f"{', '.join(conflict.get('claim_ids') or [])}: {conflict.get('description','')} — resolved by "
                     "the wording changes recorded above for those claims.")
    if not plan["conflicts"]:
        lines.append("- none declared")
    general = plan.get("general_items") or []
    if general:
        lines += ["", "## Items not tied to a registry finding", ""]
        for item in general:
            key = f"{item['role']}:{item['item_id']}"
            if key in contested:
                contested_n += 1
                lines.append(f"- {item['role']} {item['item_id']} ({item['verdict']}, {item['priority']}): "
                             f"{item.get('claim') or ''} — **contested**: {contested[key].get('position','')}")
            else:
                accepted += 1
                lines.append(f"- {item['role']} {item['item_id']} ({item['verdict']}, {item['priority']}): "
                             f"{item.get('claim') or ''} — accepted: {item.get('requested_correction') or 'noted'}")
        open_general = [f"{item['role']}:{item['item_id']}" for item in general
                        if item["verdict"] in ("OVERSTATED", "REFUTED") and item.get("requested_correction")
                        and f"{item['role']}:{item['item_id']}" not in contested]
        if open_general:
            print(f"note: {open_general} ask for a correction but name no registry finding, so no decision can act on "
                  "them; they are recorded as accepted. If the report was not changed for them, contest them "
                  "(finalize refuses an uncontested one in the last round).", file=sys.stderr)
    lines += ["", "## New issues raised by critics", ""]
    for issue in plan["new_issues"]:
        key = f"{issue['role']}:{issue['item_id']}"
        if key in contested:
            contested_n += 1
            lines.append(f"- {issue['role']} {issue['item_id']}: {issue['claim']} — **contested**: "
                         f"{contested[key].get('position','')}")
        else:
            accepted += 1
            lines.append(f"- {issue['role']} {issue['item_id']} ({issue['verdict']}, {issue['priority']}): "
                         f"{issue['claim']} — accepted: {issue.get('requested_correction') or 'noted'}")
    if not plan["new_issues"]:
        lines.append("- none")
    lines += ["", "## Settled spot-check failures", "",
              f"- {plan['settled_spot_check_failures'] or 'none'}", ""]
    (round_dir / "RESPONSE_notes.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    log = audit / "report/ADJUDICATION.md"
    text = log.read_text(encoding="utf-8")
    block = "\n".join(adjudication) + f"<!-- adjudication:round-{rnd}:end -->\n"
    if f"<!-- adjudication:round-{rnd} -->" in text:
        # A rerun supersedes this round's block. The log is append-only (adjudication_guide: preserve
        # superseded entries as WITHDRAWN), and anything the coordinator wrote after the block must
        # survive, so the old block is relabelled -- its markers renamed so no gate reads it as live --
        # and the new block is appended.
        text = text.replace(f"<!-- adjudication:round-{rnd} -->",
                            f"<!-- withdrawn-adjudication:round-{rnd} -->\n"
                            f"**WITHDRAWN: superseded by a re-run of round {rnd} integration below.**")
        text = text.replace(f"<!-- adjudication:round-{rnd}:end -->", f"<!-- withdrawn-adjudication:round-{rnd}:end -->")
        text = re.sub(r"<!-- adjudication:(\S+):round%d -->" % rnd, r"<!-- withdrawn-adjudication:\1:round%d -->" % rnd, text)
        text = text.replace(f"<!-- adjudication:round-{rnd}:disclosure-cleared:",
                            f"<!-- withdrawn-adjudication:round-{rnd}:disclosure-cleared:")
    log.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")

    summary = {"round": rnd, "items_accepted": accepted, "items_contested": contested_n,
               "factual_corrections": len(corrected_ids), "conflicts_declared": len(plan["conflicts"]),
               "claims_with_verdicts": len(plan["findings"]), "integrated_at": now,
               "invocation": plan["invocation"],
               # the contests this integration applied, canonically hashed: finalize compares the round's
               # CONTESTED.json with it, so the shipped file cannot drift from the notes written here
               "contested_sha256": hashlib.sha256(json.dumps(contested, sort_keys=True).encode("utf-8")).hexdigest()}
    if write_round_contests:
        stale_contest.write_text(json.dumps(contested, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"note: wrote the contests passed to {stale_contest.relative_to(audit)} (finalize and the release read that "
              "file)", file=sys.stderr)
    (round_dir / "INTEGRATION_SUMMARY.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument("--decisions", type=pathlib.Path)
    parser.add_argument("--contested", type=pathlib.Path)
    parser.add_argument("--at", help="integration timestamp (defaults to now, UTC)")
    args = parser.parse_args()
    audit = args.audit_dir.resolve()
    loaded = {}
    for flag, path in (("--decisions", args.decisions), ("--contested", args.contested)):
        try:
            loaded[flag] = json.loads(path.read_text(encoding="utf-8")) if path else {}
        except (OSError, json.JSONDecodeError) as exc:
            example = ("\"$AUDIT_DIR/work/round{0}_decisions.json\"" if flag == "--decisions"
                       else "\"$AUDIT_DIR/critique/round_{0}/CONTESTED.json\"").format(args.round)
            print(f"FATAL: {flag} takes the path of a JSON file (e.g. {example}; a relative path is read from the "
                  f"current directory), and {path} could not be read as one: {exc}", file=sys.stderr)
            return 1
        if not isinstance(loaded[flag], dict):
            print(f"FATAL: {flag} {path} must hold a JSON object", file=sys.stderr)
            return 1
    decisions = {k: v for k, v in loaded["--decisions"].items() if not k.startswith("_")}
    bad = sorted(k for k, v in decisions.items() if not isinstance(v, dict))
    if bad:
        print(f"FATAL: --decisions entries must be objects of decided fields, e.g. {{\"F1\": {{\"status\": "
              f"\"confirmed\", \"coordinator_approved\": true}}}}; not an object: {bad}", file=sys.stderr)
        return 1
    # a mistyped key (e.g. "report_sections") was silently dropped: refuse it before anything is written
    decidable = {"status", "coordinator_approved", "claim", "impact", "changes_conclusion", "changes_conclusion_which",
                 "report_section", "benign_explanations_ruled_out", "verification_snippet", "remedy", "disclosure_flag",
                 "confidence", "ledger_disposition", "unverifiable_basis", "evidence_rerun_command", "evidence_observed",
                 "evidence_expected", "applied_note", "rebind_literal_confirmations",
                 disclosure_policy.BASIS_KEY, "disclosure_flag_reason"}
    unknown_keys = sorted({f"{fid}.{key}" for fid, decision in decisions.items() for key in decision
                           if key not in decidable and not key.startswith("_")})
    if unknown_keys:
        print(f"FATAL: --decisions has fields apply does not know: {unknown_keys}. Decidable fields: "
              f"{sorted(decidable)} (keys starting with _ are comments)", file=sys.stderr)
        return 1
    contested = loaded["--contested"]
    # the converged gate's rule, checked before anything is written
    malformed = sorted(k for k, v in contested.items() if not (isinstance(v, dict) and all(
        isinstance(v.get(field), str) and v[field].strip() for field in ("position", "evidence"))))
    if malformed:
        print("FATAL: --contested entries must be {\"position\": \"...\", \"evidence\": \"...\"} with both "
              f"non-empty (finalize's converged gate uses the same rule); malformed: {malformed}", file=sys.stderr)
        return 1
    status_path = audit / f"critique/round_{args.round}/ROUND_STATUS.json"
    if not status_path.is_file():
        print(f"FATAL: {status_path} not found: round {args.round} has not run in {audit} (check the audit "
              f"directory argument, or run scripts/run_critique.sh {args.round} first)", file=sys.stderr)
        return 2
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if not (status.get("outcome") == "complete" and status.get("quorum_met") is True
            and status.get("exit_code") == 0 and status.get("failed_roles") == []):
        print(f"FATAL: round {args.round} is not complete; integrate nothing", file=sys.stderr)
        return 2
    state_path = audit / "work/renumber_state.json"
    pointer = audit / f"critique/round_{args.round}/CURRENT_INVOCATION"
    if state_path.is_file() and pointer.is_file():
        ran = (json.loads(state_path.read_text(encoding="utf-8")).get("rounds_run_before_renumber") or {})
        if ran.get(f"round_{args.round}") == pointer.read_text(encoding="utf-8").strip():
            # The critics' outputs, the decisions file and the round's snapshots name findings by the ids
            # they had when this invocation ran. After a later renumber (the withdrawal recipe) a rerun
            # would join old-id verdicts to the new rows: a confirmed finding marked rejected, a withdrawn
            # one confirmed. Recorded by content, not file times, so a copy or clone keeps it.
            later = later_rounds(audit, args.round)
            if later:
                # the real blocker: undoing the renumber would not help once a later round has run
                import lifecycle_journal
                hint = lifecycle_journal.next_round_hint(lifecycle_journal.load_events(audit), args.round)
                print(f"FATAL: round {args.round} cannot be re-integrated: later round(s) {later} already ran (and its "
                      f"findings were renumbered since). A finished round is not run again; {hint} if more review is "
                      "needed.", file=sys.stderr)
                return 2
            print(f"FATAL: the findings were renumbered after round {args.round}'s invocation ran "
                  f"(work/renumber_state.json), so its verdicts and decisions carry the old ids; do not "
                  f"re-integrate round {args.round}. To redo it, restore the pre-renumber registry, report, work/id_map.json "
                  f"and work/id_map_last.json and every file the renumber's --also rewrote, remove its last line from "
                  f"work/id_map_history.jsonl and delete work/renumber_state.json; otherwise review the change in "
                  f"round {args.round + 1} (a finished round cannot be run again).", file=sys.stderr)
            return 2
    legacy_map = audit / "work/id_map_last.json"
    if not legacy_map.is_file():
        legacy_map = audit / "work/id_map.json"     # 2.6.3 kept only the composed map
    if not state_path.is_file() and legacy_map.is_file() and not (audit / "work/id_map_history.jsonl").is_file():
        # Renumbered by a version that wrote no renumber_state.json: fall back to its rule (map newer
        # than the round's status). File times can be wrong after a copy, so say how to proceed.
        import later_disputes   # one rule for every tool: only a map that moved an F<n> id
        moved = later_disputes._moves_final_ids(legacy_map)
        if moved and legacy_map.stat().st_mtime > status_path.stat().st_mtime:
            print(f"FATAL: {legacy_map.relative_to(audit)} (a renumber by an earlier skill version) is newer than round "
                  f"{args.round}'s ROUND_STATUS.json, so the round's verdicts may carry pre-renumber ids; do not "
                  f"re-integrate it. If the renumber really came before the round ran (a copy reset the file "
                  f"times), record that with: echo '{{\"rounds_run_before_renumber\": {{}}}}' > "
                  f"{shlex.quote(str(audit / 'work/renumber_state.json'))}", file=sys.stderr)
            return 2
    if args.mode == "plan":
        plan = build_plan(audit, args.round)
        summary = {"claims_with_verdicts": len(plan["findings"]),
                   "reviewers": sorted(plan["reviewers"]),
                   "conflicts": len(plan["conflicts"]),
                   "new_issues": len(plan["new_issues"])}
        bound = {row["id"]: [v["reviewer_id"] for v in row["verifier_verifications"]]
                 for row in plan["findings"] if row.get("verifier_verifications")}
        if bound:
            summary["verifier_verifications"] = bound
        print(json.dumps(summary, indent=2))
        return 0
    now = args.at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return apply(audit, args.round, decisions, contested, now,
                 contested_path=str(args.contested) if args.contested is not None else None)


if __name__ == "__main__":
    raise SystemExit(main())
