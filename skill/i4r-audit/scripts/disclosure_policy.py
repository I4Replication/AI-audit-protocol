#!/usr/bin/env python3
"""Deposit personal data is reported in the comment, never withheld (2.6.8).

Programme policy since 2026-09-27: an audit is never blocked, skipped, parked or paused because the
replication deposit holds personally identifying information. The paper is audited normally and the
personal data is reported in the comment as a disclosure-flagged finding that names the identifier
kind, the file and column (or the directory and a file count), the rows or participants affected,
whether anonymisation was claimed, and a verified remedy -- never an identifying value, and never a
procedure for re-identifying anyone. No output-sanitizing gate is loosened.

A disclosure finding the deposit does not support is refuted like any other claim. What may take a
disclosure-flagged row out of the comment is therefore a STRUCTURED basis, never free text: one of
``refuted``, ``duplicate_of:<finding id>`` (the finding that carries it, which must end final),
``not_a_disclosure_defect`` or ``tested_negative``. Intent is not inferred from prose.

Two scripts enforce the reporting half, and both read the rule from here so they cannot drift:

* ``integrate_round.py apply`` (``decision_review``). A decision that EXCLUDEs a disclosure-flagged
  row, clears its ``disclosure_flag``, or sets its status rejected or superseded (each a change from
  the row's current state) must carry a decision-level ``disclosure_exclusion_basis`` from the
  allowlist; clearing the flag also needs a ``disclosure_flag_reason`` (recorded in
  ADJUDICATION.md). Withdrawing the coordinator approval of a flagged row that stays INCLUDED and
  flagged is not a withdrawal from the comment (2.6.9): the row is still under review, exactly as a
  draft kept unapproved is, and finalize requires it final (or deferred at an unfinished stop). A
  row that leaves the comment on a basis must end EXCLUDED, and its ledger reason is written
  opening with the basis label ("Refuted: ...", "Duplicate of F7: ..."), which is
  how finalize reads the basis back from the registry. A MERGE, and a ``duplicate_of`` basis, must
  name a row that itself ends final: approved, status confirmed, verified or accepted, and
  INCLUDED (a MERGED chain is followed to its end).
* ``finalize_run.sh`` requires a reported finding when ``governance.pii_present`` is true or
  ``disclosure.flags`` is non-empty, and (``registry_review``) that every disclosure-flagged row of
  ``work/findings.jsonl`` is final, MERGED into a final row, or EXCLUDED with a reason that opens
  with an allowlisted basis label. One exception (2.6.9, ``deferred_rows``): at a stop that ends the
  review unfinished (``cap_with_unresolved`` or ``aborted``) a flagged row still INCLUDED and awaiting
  its approval -- 2.6.8's deferred approval, which no remaining round can bind -- is a warning when
  the stop lists it as unresolved and the comment's ``disclosure.flags`` and ``disclosure.statement``
  report the exposure. Otherwise an audit that reached its round cap could never be filed. The stop
  may list the row by its finding id (``F7``) or by the id of a critic item that assessed it
  (``critic_stats:S3``, or a bare ``S3`` when only one finding answers to it), as the adjudication
  guide says to copy the ids from the round records (``critic_item_findings``).

Both are errors only for audits initialised at skill 2.6.9 or later (``run.json``
``protocol_version``; ``ENFORCED_FROM``), or reopened by such a skill; an older audit gets the same
messages as warnings, so an audit in flight is not refused mid-way and a published audit can still be
re-finalized to re-file a corrected document. That includes audits initialised on the live 2.6.8
release: its SKILL.md and prompts still told the operator to pause for a disclosure decision, so its
audits (audit A27, published with governance.pii_present true, empty
disclosure.flags and statement, and five flagged comment findings) were never asked for the
comment-level fields these checks require.

``withholding_reason`` (withholding language: withheld, deferred, too sensitive, a private channel,
kept out of the comment, privacy given as the ground) is a secondary WARNING only. It neither
refuses a decision nor excuses one: a refuted row may be described in privacy words, and a
withholding reason is refused because it lacks an allowlisted basis, not because of its words.
"""
from __future__ import annotations

import re
from typing import Any

# The first skill release whose audits must report deposit personal data (errors, not warnings). 2.6.9, not
# 2.6.8: the live 2.6.8 (the QA release) shipped without this rule and its prompts still said to pause.
ENFORCED_FROM = (2, 6, 9)

_VERSION = re.compile(r"\A\s*v?(\d+)\.(\d+)\.(\d+)(?![0-9])")

WITHHOLDING = re.compile(
    r"""
      \bwithh[eo]l\w*
    | \btoo\s+sensitive\b
    | \bresponsible[\s-]+disclosure\b
    | \bdisclosure\s+(?:e-?mails?|letters?|channels?|routes?|process|procedure|decisions?|track)\b
    | \bprivately\b
    | \bprivate\s+(?:channels?|routes?|e-?mails?|correspondence|notifications?)\b
    | \b(?:defer|postpon)\w*
    | \bpending\s+(?:[\w'-]+\s+){0,2}?(?:notification|disclosure|clearance)\b
    | \b(?:for|on|due\s+to|because\s+of|out\s+of|given|citing|to\s+protect)\s+(?:[\w'-]+\s+){0,3}?
        (?:privacy|confidentiality|data[\s-]+protection|gdpr|sensitivity|anonymity|identities)\b
    | \bprivacy[\s-]+(?:reasons?|grounds)\b
    | \b(?:keep|kept|keeping|leave|left|leaving|move|moved|moving|hold|held|holding|coarsen\w*)\s+
        (?:[\w'-]+\s+){0,2}?(?:out\s+of|outside|from)\s+(?:the\s+)?(?:public\s+)?(?:comment|report)\b
    | \b(?:not|never)\s+(?:be\s+)?(?:reported|published|disclosed)\s+
        (?:publicly|in\s+(?:the\s+)?(?:public\s+)?(?:comment|report))\b
    | \bmap\s+to\s+(?:it|the\s+(?:exposure|data|identifiers?))\b
    | \b(?:remov|drop|omit|strip|delet)\w*\s+(?:[\w'-]+\s+){0,2}?(?:from|out\s+of)\s+
        (?:the\s+)?(?:public\s+)?comment\b
    | \bre-?identif\w*
    | \bgdpr\b
    | \bnaming\s+(?:the\s+)?(?:file|files|column|columns|participants?)\s+(?:could|would|might|may)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

REMEDY = ("Deposit personal data is reported in the comment, never withheld: keep the row INCLUDED "
          "and approved (or MERGE it into a final row) and describe it value-free -- identifier kind, "
          "file and column, rows or participants affected, verified remedy. Only a reproduced value, "
          "a new linkage or a re-identification procedure is corrected, never the finding itself.")

# The decision key that states why a disclosure-flagged row may leave the comment.
BASIS_KEY = "disclosure_exclusion_basis"
# Basis -> the label an EXCLUDED row's ledger reason opens with (finalize reads the basis back from it).
BASIS_LABELS = {
    "refuted": "Refuted",
    "not_a_disclosure_defect": "Not a disclosure defect",
    "tested_negative": "Tested negative",
}
ALLOWED_BASES = "refuted, duplicate_of:<finding id>, not_a_disclosure_defect or tested_negative"
LABELS_TEXT = "'Refuted:', 'Duplicate of <finding id>:', 'Not a disclosure defect:' or 'Tested negative:'"
FINAL_STATUSES = frozenset({"confirmed", "verified", "accepted"})
LEGACY_NOTE = " [warning only: this audit was initialised before skill 2.6.9]"
# Stops that end the review unfinished: an approval deferred to a later round can no longer be bound.
UNFINISHED_STOPS = frozenset({"cap_with_unresolved", "aborted"})

_FINDING_ID = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_BASIS = re.compile(r"\A(?:(refuted|not_a_disclosure_defect|tested_negative)|duplicate_of:(" + _FINDING_ID + r"))\Z")
_REASON_BASIS = re.compile(
    r"\A\s*(?:(refuted)|(not\s+a\s+disclosure\s+defect)|(tested[\s-]+negative)"
    r"|duplicate\s+of\s+(" + _FINDING_ID + r"))(?=[\s:;,.)]|\Z)",
    re.IGNORECASE,
)


def skill_version(value: Any) -> tuple[int, int, int] | None:
    """The leading ``X.Y.Z`` of a protocol_version string, or None when it has none."""
    match = _VERSION.match(str(value or ""))
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def enforced(protocol_version: Any, run: Any = None) -> bool:
    """True when an audit initialised at this protocol_version must report (2.6.9 or later), or when an older
    audit was reopened (2.6.9, run.reopenings) by a skill at or above that version: a reopened audit is re-filed
    under the rule in force when it is reopened, not the one it was initialised under."""
    version = skill_version(protocol_version)
    if version is not None and version >= ENFORCED_FROM:
        return True
    reopenings = run.get("reopenings") if isinstance(run, dict) else None
    for entry in reopenings if isinstance(reopenings, list) else []:
        reopened_by = skill_version((entry or {}).get("skill_version")) if isinstance(entry, dict) else None
        if reopened_by is not None and reopened_by >= ENFORCED_FROM:
            return True
    return False


def withholding_reason(text: Any) -> str | None:
    """The withholding phrase in ``text`` (lower-cased, whitespace collapsed), or None. A warning only."""
    if not isinstance(text, str):
        return None
    match = WITHHOLDING.search(text)
    return " ".join(match.group(0).lower().split()) if match else None


def parse_basis(value: Any) -> tuple[str, str | None] | None:
    """``("refuted", None)``, ``("duplicate_of", "F7")`` ...; None when ``value`` is not allowlisted."""
    match = _BASIS.match(value) if isinstance(value, str) else None
    if not match:
        return None
    return (match.group(1), None) if match.group(1) else ("duplicate_of", match.group(2))


def reason_basis(reason: Any) -> tuple[str, str | None] | None:
    """The allowlisted basis a ledger reason opens with ("Refuted: ..." -> ("refuted", None)), or None."""
    match = _REASON_BASIS.match(reason) if isinstance(reason, str) else None
    if not match:
        return None
    for kind, group in (("refuted", 1), ("not_a_disclosure_defect", 2), ("tested_negative", 3)):
        if match.group(group):
            return (kind, None)
    return ("duplicate_of", match.group(4))


def basis_label(basis: tuple[str, str | None]) -> str:
    kind, target = basis
    return f"Duplicate of {target}" if kind == "duplicate_of" else BASIS_LABELS[kind]


def labelled_reason(reason: Any, basis: tuple[str, str | None]) -> Any:
    """``reason`` opening with the label of ``basis``; unchanged when it already does or is empty."""
    if not (isinstance(reason, str) and reason.strip()) or reason_basis(reason) == basis:
        return reason
    return f"{basis_label(basis)}: {reason.strip()}"


def disposition_status(row: dict[str, Any]) -> str | None:
    disposition = row.get("ledger_disposition")
    return disposition.get("status") if isinstance(disposition, dict) else None


def is_final(row: dict[str, Any] | None) -> bool:
    """The row reaches the comment: approved, a final status, and INCLUDED."""
    return (isinstance(row, dict) and row.get("status") in FINAL_STATUSES
            and row.get("coordinator_approved") is True and disposition_status(row) == "INCLUDED")


def resolves_final(finding_id: Any, rows_by_id: dict[Any, dict[str, Any]]) -> bool:
    """``finding_id`` is a final row, or a MERGED chain from it ends at one."""
    seen: set[Any] = set()
    while finding_id in rows_by_id and finding_id not in seen:
        seen.add(finding_id)
        row = rows_by_id[finding_id]
        if is_final(row):
            return True
        if disposition_status(row) != "MERGED":
            return False
        finding_id = (row.get("ledger_disposition") or {}).get("target")
    return False


def withheld_row(row: dict[str, Any]) -> str | None:
    """The withholding phrase when a disclosure-flagged row is EXCLUDED for it, else None (a warning)."""
    if row.get("disclosure_flag") is not True or disposition_status(row) != "EXCLUDED":
        return None
    return withholding_reason((row.get("ledger_disposition") or {}).get("reason"))


def _listing(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _overlay(row: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    end = dict(row)
    for key in ("status", "coordinator_approved", "disclosure_flag", "ledger_disposition"):
        if key in decision:
            end[key] = decision[key]
    return end


def decision_review(rows: list[dict[str, Any]], decisions: dict[str, Any]) -> tuple[list[str], list[str]]:
    """``(problems, warnings)`` for applying ``decisions`` to ``rows``.

    ``problems`` refuse the apply for an audit at 2.6.9 or later and are warnings before it;
    ``warnings`` (withholding language) never refuse. See the module docstring for the rule.
    """
    by_id = {row.get("id"): row for row in rows}
    ends = {finding_id: _overlay(row, decisions[finding_id])
            if isinstance(decisions.get(finding_id), dict) else row for finding_id, row in by_id.items()}
    problems: list[str] = []
    warnings: list[str] = []
    for finding_id, decision in decisions.items():
        row = by_id.get(finding_id)
        if row is None or not isinstance(decision, dict):
            continue
        was_flagged = row.get("disclosure_flag") is True
        flagged = was_flagged or decision.get("disclosure_flag") is True
        basis = parse_basis(decision.get(BASIS_KEY))
        if BASIS_KEY in decision and basis is None:
            problems.append(
                f"{finding_id}: {BASIS_KEY} {decision.get(BASIS_KEY)!r} is not an allowlisted basis. "
                f"Use one of {ALLOWED_BASES}; anything else is refused.")
        if basis and basis[0] == "duplicate_of":
            target = basis[1]
            if target == finding_id or target not in by_id:
                problems.append(f"{finding_id}: {BASIS_KEY} duplicate_of:{target} names no other row of "
                                "work/findings.jsonl.")
            elif not resolves_final(target, ends):
                problems.append(
                    f"{finding_id}: {BASIS_KEY} duplicate_of:{target} names a row that does not end "
                    "final (approved, status confirmed, verified or accepted, and INCLUDED), so the "
                    f"finding would leave the comment. {REMEDY}")
        if not flagged:
            continue
        end = ends[finding_id]
        end_status = disposition_status(end)
        clearing = was_flagged and decision.get("disclosure_flag") is False
        disposition = decision.get("ledger_disposition") if isinstance(decision.get("ledger_disposition"), dict) else {}
        if end_status == "MERGED" and not clearing:
            # A MERGE into a final row keeps the finding in the comment: no basis is needed.
            target = (end.get("ledger_disposition") or {}).get("target")
            if not resolves_final(target, ends):
                problems.append(
                    f"{finding_id} is disclosure-flagged and the decision MERGEs it into {target!r}, "
                    "which does not end final (approved, status confirmed, verified or accepted, and "
                    f"INCLUDED), so the finding would leave the comment. {REMEDY}")
            continue
        # What the decision CHANGES: a draft row kept unapproved while its claim is revised is still
        # under review, not withdrawn (finalize requires it final at the end). So is an approved row whose
        # approval is withdrawn while it stays INCLUDED and flagged (2.6.9): integrate_round's own remedy for an
        # OVERSTATED verdict on an approved row is to leave it unapproved until a later round confirms it, and
        # finalize accepts that deferred approval at an unfinished stop. Only leaving the comment needs a basis.
        withdrawing = []
        if disposition.get("status") == "EXCLUDED" and disposition_status(row) != "EXCLUDED":
            withdrawing.append("EXCLUDEs it")
        if decision.get("status") in ("rejected", "superseded") and row.get("status") != decision["status"]:
            withdrawing.append(f"sets its status {decision['status']}")
        if decision.get("coordinator_approved") is False and row.get("coordinator_approved") is True \
                and end_status not in ("INCLUDED", "EXCLUDED"):
            withdrawing.append("withdraws its coordinator approval")
        if clearing:
            withdrawing.append("clears its disclosure_flag")
        if withdrawing and BASIS_KEY not in decision:
            problems.append(
                f"{finding_id} is disclosure-flagged and the decision {_listing(withdrawing)} without a "
                f"{BASIS_KEY}. A disclosure finding leaves the comment only on a stated basis: "
                f"{ALLOWED_BASES}; anything else is refused. {REMEDY} A row still under review may stay "
                "unapproved and INCLUDED; by finalization it must be final, MERGED into a final row, or EXCLUDED on a "
                "basis (at cap_with_unresolved or aborted, listed as unresolved by its finding id or a critic item "
                "id that assessed it, and reported in the comment's disclosure).")
        if clearing:
            reason = decision.get("disclosure_flag_reason")
            if not (isinstance(reason, str) and reason.strip()):
                problems.append(
                    f"{finding_id}: the decision clears disclosure_flag but states no "
                    "disclosure_flag_reason. Clear the flag only when the finding is not a disclosure "
                    f"defect (for example, it was refuted): give the {BASIS_KEY} and say why in "
                    "disclosure_flag_reason, which is recorded in ADJUDICATION.md.")
            phrase = withholding_reason(reason)
            if phrase:
                warnings.append(f"{finding_id}: disclosure_flag_reason reads like withholding language "
                                f"('{phrase}'). A finding is never withheld; the {BASIS_KEY} decides.")
        elif basis and withdrawing and end_status != "EXCLUDED":
            problems.append(
                f"{finding_id} is disclosure-flagged and the decision {_listing(withdrawing)} on basis "
                f"{decision.get(BASIS_KEY)} but leaves it {end_status}. A disclosure finding that leaves "
                "the comment is EXCLUDED with its reason (or MERGED into a final row): set its "
                "ledger_disposition.")
        if end_status == "EXCLUDED":
            phrase = withholding_reason((end.get("ledger_disposition") or {}).get("reason"))
            if phrase:
                warnings.append(f"{finding_id}: the EXCLUDED reason reads like withholding language "
                                f"('{phrase}'). A finding is never withheld; the {BASIS_KEY} decides.")
    return problems, warnings


def decision_refusals(rows: list[dict[str, Any]], decisions: dict[str, Any]) -> list[str]:
    """The ``problems`` of ``decision_review``: why ``decisions`` may not be applied at 2.6.9+."""
    return decision_review(rows, decisions)[0]


def _translate(finding_id: Any, maps: list[dict[str, Any]]) -> Any:
    for mapping in maps:
        finding_id = mapping.get(finding_id, finding_id)
    return finding_id


def critic_item_findings(audit: Any) -> dict[str, set[str]]:
    """Critic item ids -> the registry finding ids (today's) the item assessed, from every round's records.

    The adjudication guide says to copy a stop's unresolved ids "from the round records and diff
    manifests", and run.schema.json calls them critique item ids, so an operator lists a deferred
    disclosure row as ``critic_stats:S3`` as readily as ``F7``. Each round's current projection (else the
    round folder) is read: ``critic_<role>.json`` and the targeted adjudication's ``CRITIQUE.json``
    (``critic_synth``). An item is keyed ``<role>:<item_id>`` and by its bare ``item_id``; its
    ``finding_id`` is translated through the renumbers recorded after that round's invocation
    (``work/id_map_history.jsonl``). An audit renumbered by a version that kept no history gets no
    translation, so nothing is matched through an item there (list the row by its finding id).
    """
    import json
    import pathlib
    root = pathlib.Path(audit)
    try:
        import later_disputes   # a sibling module: one legacy rule for every tool
        if later_disputes.legacy_renumbered(root):
            return {}
    except Exception:           # noqa: BLE001 -- a helper that cannot decide translates nothing
        return {}
    history: list[dict[str, Any]] = []
    history_path = root / "work/id_map_history.jsonl"
    if not history_path.is_file() and any((root / name).is_file()
                                          for name in ("work/id_map.json", "work/id_map_last.json")):
        # Renumbered with no history (later_disputes' rule sees only maps keyed by F-ids, so a composed map from
        # original ids is missed): an item's finding_id may name a row that has since moved. Translate nothing.
        return {}
    try:
        for line in (history_path.read_text(encoding="utf-8").splitlines() if history_path.is_file() else []):
            if line.strip():
                entry = json.loads(line)
                if isinstance(entry, dict):
                    history.append(entry)
    except (OSError, ValueError):
        return {}
    found: dict[str, set[str]] = {}
    for directory in sorted((root / "critique").glob("round_*")):
        try:
            number = int(directory.name.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        invocation_path = directory / "CURRENT_INVOCATION"
        invocation = invocation_path.read_text(encoding="utf-8").strip() if invocation_path.is_file() else None
        maps = [entry.get("map") or {} for entry in history
                if isinstance(entry.get("rounds_run_before_renumber"), dict)
                and f"round_{number}" in entry["rounds_run_before_renumber"]
                and (invocation is None or entry["rounds_run_before_renumber"][f"round_{number}"] == invocation)]
        pointer = directory / "CURRENT_PROJECTION"
        base = directory / "projections" / pointer.read_text(encoding="utf-8").strip() \
            if pointer.is_file() else directory
        if not base.is_dir():
            base = directory
        for path in sorted(base.glob("critic_*.json")) + [base / "CRITIQUE.json"]:
            try:
                record = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            role = record.get("role") if isinstance(record.get("role"), str) else (
                "critic_synth" if path.name == "CRITIQUE.json" else path.stem)
            for item in record.get("items") or []:
                if not isinstance(item, dict) or not item.get("item_id") or not item.get("finding_id"):
                    continue
                finding = str(_translate(item["finding_id"], maps))
                for key in (f"{role}:{item['item_id']}", str(item["item_id"])):
                    found.setdefault(key, set()).add(finding)
    return found


def listed_ids(row_id: Any, unresolved: Any = (), item_findings: dict[str, Any] | None = None) -> bool:
    """The stop lists ``row_id``: by its finding id, or by a critic item id that assessed only it."""
    for item in unresolved or []:
        if str(item) == str(row_id):
            return True
        findings = (item_findings or {}).get(str(item))
        if findings and set(map(str, findings)) == {str(row_id)}:
            return True
    return False


def deferred_rows(rows: list[tuple[int, dict[str, Any]]], stopping_reason: Any = None,
                  unresolved: Any = (), reported: bool = False,
                  item_findings: dict[str, Any] | None = None) -> list[Any]:
    """Ids of the flagged rows ``registry_review`` accepts, with a warning, at an unfinished stop.

    Such a row is still INCLUDED and awaiting approval (a deferred approval that no remaining round
    can bind), and the stop is ``cap_with_unresolved`` or ``aborted``, lists it among its unresolved
    items (by its finding id, or by a critic item id that ``item_findings`` maps to it alone --
    ``critic_item_findings``), and the comment reports the exposure (``reported``: non-empty
    ``disclosure.flags`` and ``disclosure.statement``). The finding's prose stays in the report;
    comment.json numbers only approved findings (generate_finding_artifacts), so it ships as a
    numbering gap.
    """
    if stopping_reason not in UNFINISHED_STOPS or not reported:
        return []
    return [row.get("id") for _, row in rows
            if row.get("disclosure_flag") is True and not is_final(row)
            and disposition_status(row) == "INCLUDED" and listed_ids(row.get("id"), unresolved, item_findings)]


def registry_review(rows: list[tuple[int, dict[str, Any]]], stopping_reason: Any = None,
                    unresolved: Any = (), reported: bool = False,
                    item_findings: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    """``(problems, warnings)`` for the disclosure-flagged rows of ``work/findings.jsonl``.

    ``rows`` are ``(line number, row)`` pairs. Every flagged row must be final, MERGED into a final
    row, or EXCLUDED with a reason opening with an allowlisted basis label; a ``Duplicate of`` label
    must name a row that ends final. The rows ``deferred_rows`` names (same arguments) only warn.
    """
    by_id = {row.get("id"): row for _, row in rows}
    problems: list[str] = []
    warnings: list[str] = []
    deferred = set(deferred_rows(rows, stopping_reason, unresolved, reported, item_findings))
    for number, row in rows:
        if row.get("disclosure_flag") is not True or is_final(row):
            continue
        where = f"registry row {row.get('id')} (work/findings.jsonl line {number})"
        if row.get("id") in deferred:
            warnings.append(
                f"{where} is disclosure-flagged and still awaiting approval at {stopping_reason}; it is "
                "listed as unresolved and the comment's disclosure flags and statement report the "
                "exposure, so it is filed in the report prose, outside the numbered findings.")
            continue
        disposition = row.get("ledger_disposition") if isinstance(row.get("ledger_disposition"), dict) else {}
        status = disposition.get("status")
        if status == "MERGED":
            if not resolves_final(disposition.get("target"), by_id):
                problems.append(f"{where} is disclosure-flagged and MERGED into {disposition.get('target')!r}, "
                                f"which is not a final row, so the finding is in no comment. {REMEDY}")
            continue
        if status == "EXCLUDED":
            basis = reason_basis(disposition.get("reason"))
            phrase = withholding_reason(disposition.get("reason"))
            if basis is None:
                problems.append(
                    f"{where} is disclosure-flagged and EXCLUDED without an allowlisted basis"
                    + (f" (the reason says '{phrase}', which is withholding language)" if phrase else "")
                    + f". PII findings are reported, not withheld: a disclosure row leaves the comment only "
                    f"when its ledger reason opens with {LABELS_TEXT} (integrate_round.py writes the label "
                    f"from a decision's {BASIS_KEY}). {REMEDY}")
            elif basis[0] == "duplicate_of" and (basis[1] == row.get("id") or not resolves_final(basis[1], by_id)):
                problems.append(f"{where} is EXCLUDED as a duplicate of {basis[1]}, which is not another "
                                f"final row, so the finding is in no comment. {REMEDY}")
            elif phrase:
                warnings.append(f"{where}: the EXCLUDED reason reads like withholding language ('{phrase}'); "
                                "accepted on its stated basis.")
            continue
        unfinished = ""
        if status == "INCLUDED" and stopping_reason in UNFINISHED_STOPS:
            unfinished = (f" At {stopping_reason} a row still awaiting approval is accepted once the stop lists it "
                          f"as unresolved (review_scope unresolved_blocking_item_ids or "
                          f"unresolved_conclusion_critical_item_ids) by its finding id ({row.get('id')}) or by the id of "
                          "a critic item that assessed it (critic_<role>:<item_id>), and the comment's disclosure.flags "
                          "and disclosure.statement report the exposure.")
        problems.append(
            f"{where} is disclosure-flagged but not final (status {row.get('status')}, coordinator_approved "
            f"{row.get('coordinator_approved')}, {status}), so it reaches no comment. Approve it, MERGE "
            f"it into a final row, or EXCLUDE it with a reason opening with {LABELS_TEXT}.{unfinished} {REMEDY}")
    return problems, warnings
