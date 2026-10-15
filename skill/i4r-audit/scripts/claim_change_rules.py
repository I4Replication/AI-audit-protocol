#!/usr/bin/env python3
"""Which later-round findings the specialist critics re-review (2.7.4).

A later round's diff packet lists every finding whose registry row differs from the round before
(``changed_claim_ids``). After an integration nearly every row differs, because binding a round's confirmations
rewrites ``verifications`` and approval rewrites ``settled`` and ``coordinator_approved``, so every later round sent
every finding to every routed critic. Measured on 53 later rounds of 27 audits, those bookkeeping-only rows were
82% of the ids, and also the source of 19 of the 36 non-CONFIRMED verdicts in those rounds (9 led to a rewording).
So the completeness critic keeps reviewing all of them, and only the specialists are narrowed.

The rule is frozen per audit, in the policy snapshot leaf
``critique.later_rounds.routing_manifest.claim_change_rule``:

- absent: ``LEGACY_RULE``. Every audit initialised before 2.7.4 froze no leaf; its packets, the critic snapshots
  built from them and the post-delta receipts that bind them replay byte for byte. There is NO fallback to the
  packaged policy: an audit without the leaf is legacy for its whole life.
- ``SPECIALIST_RULE``: the packet also lists ``specialist_claim_ids``, the changed findings that are substantive:
  added or removed, a registry difference other than bookkeeping, or a finding whose report section's text changed.
  The critique runtime gives the specialists only those; completeness still gets every changed finding, and a
  specialist left with none is not dispatched.

Stdlib only and imports nothing beyond json/pathlib, like probe_bounds.py: the drift check loads it by path. The
post-delta receipt's CODE_PATHS files never import it (make_diff_packet.py and the critique runtime, which do, are
not CODE_PATHS files).
"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Any, Callable, Iterable, Mapping

LEGACY_RULE = "all_fields"
SPECIALIST_RULE = "substantive_specialists_1"
LIVE_RULES: tuple[str, ...] = (LEGACY_RULE, SPECIALIST_RULE)
SHIPPED_RULE = SPECIALIST_RULE
LEAF = ("critique", "later_rounds", "routing_manifest", "claim_change_rule")
CONFIRMING = frozenset({"confirmed", "pass", "verified"})
_TOP_SECTION = re.compile(r"^(Section \d+)(?:\.\d+)*$")


class RuleError(ValueError):
    pass


def rule_of(policy: Mapping[str, Any] | None) -> str:
    """The rule a policy freezes: LEGACY_RULE when it has no leaf; refuse any value that is not a frozen live rule."""
    node: Any = policy
    for key in LEAF:
        if not isinstance(node, Mapping) or key not in node:
            return LEGACY_RULE
        node = node[key]
    if node not in LIVE_RULES or node == LEGACY_RULE:
        # LEGACY_RULE is implied by absence and never frozen, so a frozen "all_fields" is refused too
        raise RuleError(f"{'.'.join(LEAF)} is {node!r}, not a packaged claim-change rule "
                        f"({SPECIALIST_RULE!r}, or no leaf for the legacy rule)")
    return node


def frozen_rule(root: pathlib.Path) -> str:
    """The audit's rule, read ONLY from its own snapshot: LEGACY_RULE when there is none or it froze no leaf."""
    path = root / "pipeline-policy.snapshot.json"
    if not path.exists():
        return LEGACY_RULE
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuleError(f"cannot read the audit policy snapshot {path}: {exc}") from exc
    return rule_of(snapshot.get("policy") if isinstance(snapshot, Mapping) else None)


def _outcome(record: Any) -> str:
    return str(record.get("outcome") or "").lower() if isinstance(record, Mapping) else ""


def verifications_bookkeeping(before: Any, after: Any) -> bool:
    """Binding, rebinding or adding confirmations is bookkeeping; losing one, or a new non-confirming record (a
    dispute the coordinator contested rather than acted on), is not."""
    if not isinstance(before or [], list) or not isinstance(after or [], list):
        return False
    b, a = list(before or []), list(after or [])
    if not all(isinstance(v, Mapping) for v in b + a):
        return False
    confirmed_before = {v.get("reviewer_id") for v in b if _outcome(v) in CONFIRMING}
    confirmed_after = {v.get("reviewer_id") for v in a if _outcome(v) in CONFIRMING}

    def disputes(rows: list) -> list:
        return sorted((str(v.get("reviewer_id")), _outcome(v), str(v.get("invocation_id")))
                      for v in rows if _outcome(v) not in CONFIRMING)
    old = disputes(b)
    return confirmed_before <= confirmed_after and not [x for x in disputes(a) if x not in old]


def substantive_paths(before: Mapping[str, Any], after: Mapping[str, Any],
                      changed_paths: Callable[[Any, Any], list]) -> list:
    """changed_paths(before, after) without the bookkeeping ones: settled.*, an approval being bound
    (coordinator_approved not True -> True), and a bookkeeping-only verifications change."""
    out = []
    for path in changed_paths(before, after):
        top = str(path).split(".", 1)[0].split("[", 1)[0]
        if top == "settled":
            continue
        if (top == "coordinator_approved" and before.get("coordinator_approved") is not True
                and after.get("coordinator_approved") is True):
            continue
        if top == "verifications" and verifications_bookkeeping(before.get("verifications"),
                                                                after.get("verifications")):
            continue
        out.append(path)
    return out


def top_section(report_section: Any) -> str | None:
    """'Section 5.1' -> 'Section 5' (the granularity the packet's changed_sections use); anything else -> None."""
    match = _TOP_SECTION.match(str(report_section or "").strip())
    return match.group(1) if match else None


def specialist_claim_ids(changed: Iterable[str], before_by_id: Mapping[str, Mapping[str, Any]],
                         after_by_id: Mapping[str, Mapping[str, Any]], changed_paths: Callable[[Any, Any], list],
                         changed_sections: Iterable[str]) -> list[str]:
    """The changed findings the specialists re-review, in ``changed`` order."""
    # compared at the top level on both sides ("Section 5.1" in a heading or a row counts as Section 5): coarse, so it
    # errs toward a specialist look
    sections = {top_section(name) for name in changed_sections} - {None}
    keep = []
    for finding_id in changed:
        before, after = before_by_id.get(finding_id), after_by_id.get(finding_id)
        if before is None or after is None:
            keep.append(finding_id)  # added or removed
            continue
        if substantive_paths(before, after, changed_paths):
            keep.append(finding_id)
            continue
        if {top_section(before.get("report_section")), top_section(after.get("report_section"))} & sections:
            keep.append(finding_id)  # its report text changed
    return keep
