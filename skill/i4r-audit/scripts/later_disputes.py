#!/usr/bin/env python3
"""Reviewers that dispute a finding in a critique round that was never integrated.

The final clean round is not integrated, so integrate_round never drops the record of a reviewer that
called a finding OVERSTATED/REFUTED there. A settled fact must not rest on such a reviewer; make_settled
excludes the fact and finalize refuses a SETTLED.json that still lists it. Both read this one rule.

Ids are today's: a round's verdicts name findings by the ids they had when its invocation ran, so
renumbers recorded after that invocation (work/id_map_history.jsonl) are applied in order. The targeted
adjudication's CONFIRMED ruling (no correction requested) resolves the critics' dispute on a finding.
"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Any


def _translate(finding_id: Any, maps: list[dict[str, str]]) -> Any:
    for mapping in maps:
        finding_id = mapping.get(finding_id, finding_id)
    return finding_id


def later_disputes(audit: pathlib.Path) -> dict[str, dict[str, int]]:
    """Return {today's finding id: {reviewer role: round}} for rounds after the last integrated one."""
    rounds: dict[int, pathlib.Path] = {}
    for path in (audit / "critique").glob("round_*"):
        try:
            rounds[int(path.name.split("_", 1)[1])] = path
        except ValueError:
            continue
    # run.json may mark the final round integrated (its round_result); only integrate_round writes a summary
    last_summary = max([n for n, d in rounds.items() if (d / "INTEGRATION_SUMMARY.json").is_file()] or [0])
    history_path = audit / "work/id_map_history.jsonl"
    history = ([json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip()]
               if history_path.is_file() else [])
    legacy = not history_path.is_file() and _legacy_renumbered(audit)
    disputes: dict[str, dict[str, int]] = {}
    for number, directory in sorted(rounds.items()):
        pointer = directory / "CURRENT_PROJECTION"
        if number <= last_summary or not pointer.is_file():
            continue
        invocation_path = directory / "CURRENT_INVOCATION"
        invocation = invocation_path.read_text(encoding="utf-8").strip() if invocation_path.is_file() else None
        maps = []
        for entry in history:
            ran = entry.get("rounds_run_before_renumber") or {}
            if isinstance(ran, dict) and ran.get(f"round_{number}") == invocation and invocation:
                maps.append(entry.get("map") or {})
        projection = directory / "projections" / pointer.read_text(encoding="utf-8").strip()
        resolved = set()
        adjudication = projection / "CRITIQUE.json"
        if adjudication.is_file():
            for item in json.loads(adjudication.read_text(encoding="utf-8")).get("items") or []:
                finding = _translate(item.get("finding_id"), maps)
                if item.get("verdict") == "CONFIRMED" and not item.get("requested_correction") \
                        and item.get("priority") != "BLOCKING":   # a BLOCKING ruling resolves nothing
                    resolved.add(finding)
                elif item.get("verdict") in ("OVERSTATED", "REFUTED"):
                    disputes.setdefault("*" if legacy else finding, {})["critic_synth"] = number
        for critic in sorted(projection.glob("critic_*.json")):
            for item in json.loads(critic.read_text(encoding="utf-8")).get("items") or []:
                finding = _translate(item.get("finding_id"), maps)
                if item.get("verdict") in ("OVERSTATED", "REFUTED") and finding not in resolved:
                    # a renumber by an earlier version kept no history: the ids cannot be translated, so the
                    # dispute applies to every finding this reviewer confirms (fail closed)
                    disputes.setdefault("*" if legacy else finding, {})[critic.stem] = number
    return disputes


def _moves_final_ids(path: pathlib.Path) -> bool:
    """A map that moved an F<n> id after critique (the pre-round-1 draft->F renumber moves no F id)."""
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path.is_file()
    return isinstance(mapping, dict) and any(
        re.fullmatch(r"F[1-9][0-9]*", str(old)) and old != new for old, new in mapping.items())


def _legacy_renumbered(audit: pathlib.Path) -> bool:
    return any(_moves_final_ids(audit / name) for name in ("work/id_map_last.json", "work/id_map.json"))


def legacy_renumbered(audit: pathlib.Path) -> bool:
    """One rule for every tool: ids were renumbered after critique by a version that kept no exact history --
    a no-history map that moved an F<n> id, or a history whose first entry says one came before it."""
    history = audit / "work/id_map_history.jsonl"
    if not history.is_file():
        return _legacy_renumbered(audit)
    first = next((line for line in history.read_text(encoding="utf-8").splitlines() if line.strip()), "{}")
    try:
        return bool(json.loads(first).get("legacy_prior"))
    except ValueError:
        return True


def invocation_rounds(audit: pathlib.Path) -> dict[str, Any]:
    path = audit / "invocations.jsonl"
    rounds: dict[str, Any] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rounds[row.get("invocation_id")] = row.get("round")
    return rounds


def stale_confirmations(audit: pathlib.Path, finding_id: Any, confirmations: list[dict[str, Any]],
                        disputes: dict[str, dict[str, int]] | None = None,
                        rounds: dict[str, Any] | None = None) -> list[str]:
    """The confirmations a LATER round disputes: the dispute's round must exceed the record's own round, so a
    round's OVERSTATED never disputes the record that same round produced (a reworded claim's binding), even in
    audits integrated before INTEGRATION_SUMMARY.json existed."""
    disputes = later_disputes(audit) if disputes is None else disputes
    rounds = invocation_rounds(audit) if rounds is None else rounds
    against = {**disputes.get("*", {}), **disputes.get(finding_id, {})}
    stale = []
    for confirmation in confirmations:
        role = confirmation.get("reviewer_id")
        if role not in against:
            continue
        record_round = rounds.get(confirmation.get("invocation_id"))
        if isinstance(record_round, int) and record_round >= against[role]:
            continue
        stale.append(f"{role} (round {against[role]})")
    return sorted(stale)
