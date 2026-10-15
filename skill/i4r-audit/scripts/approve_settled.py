#!/usr/bin/env python3
"""Bind the coordinator's settled-fact promotion approval to the current canonical report.

    python3 scripts/approve_settled.py <audit-dir> [--dry-run]

make_settled.sh promotes a registry row only when, besides its status, approval and two-family
confirmations, the row carries an explicit ``settled`` object::

    {"approved": true, "valid_for_report_hash": "<sha256 of report/REPORT_final.md>"}

On audit A34 that object was never written, so the cache promoted zero facts and a
critique round consumed no settled facts. This helper writes it for every row that already meets
the promotion rule (INCLUDED, status confirmed/verified/accepted, coordinator_approved) and clears it
on every other row, validates every row with ``validate_v2.validate_finding_row``, and writes
``work/findings.jsonl`` atomically. Run it after each integration that changes REPORT_final.md and
before ``make_settled.sh``; it changes no scientific field.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shlex
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "schemas" / "tools"))
import validate_v2  # noqa: E402

PROMOTABLE_STATUS = {"confirmed", "verified", "accepted"}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 2
    audit = pathlib.Path(argv[0]).resolve()
    dry_run = "--dry-run" in argv[1:]
    report = audit / "report" / "REPORT_final.md"
    registry = audit / "work" / "findings.jsonl"
    if not report.is_file() or not registry.is_file():
        print("FATAL: report/REPORT_final.md and work/findings.jsonl are both required", file=sys.stderr)
        return 1
    report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
    rows = [json.loads(line) for line in registry.read_text(encoding="utf-8").splitlines() if line.strip()]
    approved = cleared = changed = 0
    for row in rows:
        before = row.get("settled")
        promotable = (row.get("ledger_disposition", {}).get("status") == "INCLUDED"
                      and row.get("status") in PROMOTABLE_STATUS and row.get("coordinator_approved") is True)
        if promotable:
            row["settled"] = {"approved": True, "valid_for_report_hash": report_sha}
            approved += 1
        elif row.get("settled") is not None:
            row["settled"] = None
            cleared += 1
        changed += row.get("settled") != before
    seen: set[str] = set()
    for index, row in enumerate(rows, 1):
        if row.get("id") in seen:
            print(f"FATAL: duplicate finding id {row.get('id')} at line {index}", file=sys.stderr)
            return 1
        seen.add(row.get("id"))
        try:
            validate_v2.validate_finding_row(row, f"line {index} ({row.get('id')})")
        except (ValueError, validate_v2.ValidationError) as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            return 1
    if dry_run:
        print(f"dry run: {approved} rows would carry settled approval bound to {report_sha[:12]}; {cleared} cleared")
        return 0
    tmp = registry.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    tmp.replace(registry)
    # SKILL/INSTALL order: approve -> project -> approve -> project -> make_settled.
    nxt = (f"python3 scripts/generate_finding_artifacts.py {shlex.quote(str(audit))} --apply; "
           f"if that projection changed the report, approve and project once more (the documented approve, project, approve, project); "
           f"then bash scripts/make_settled.sh {shlex.quote(str(audit))} {shlex.quote(str(registry))}")
    print(f"settled approval bound to report {report_sha[:12]} on {approved} rows ({changed} changed); "
          f"{cleared} cleared; next: {nxt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
