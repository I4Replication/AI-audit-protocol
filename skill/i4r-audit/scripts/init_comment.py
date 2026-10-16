#!/usr/bin/env python3
"""Create the schema-valid comment.json skeleton a fresh audit needs before its first projection.

    python3 scripts/init_comment.py "$AUDIT_DIR" --facts work/comment_facts.json

``generate_finding_artifacts.py --apply`` refuses to run without ``comment.json`` (it injects the
finding rows into an existing record), but nothing in the skill created that record: audit
A34's round-1 chain stopped at its first step for that reason. This tool writes the
non-finding skeleton once. Identity comes from ``run.json``; paper- and package-level facts come
from a small JSON file the coordinator fills in from the article and the Phase-1 reports. Fields
the facts file does not supply are null wherever the schema allows null, and derived rollups start
at zero; ``finalize_run.sh`` recomputes them. Every write is validated against
``schemas/comment.schema.json`` first, and an existing comment.json is never overwritten without
``--force``.

The facts file is a flat-or-nested JSON object mirroring the schema's sections, e.g.

    {"bibliographic": {"journal": "...", "year": 2025, "field": "..."},
     "design": {"type": "cross_sectional"},
     "sample": {"n": 131, "unit_of_observation": "participant", "human_subjects": true},
     "package": {"data_present": true, "code_present": true, "runs_as_shipped": "no"},
     "timing": {"publication_date": "2025-01-22", "audit_start_date": "2026-08-30"}}

Unknown keys are rejected, so a typo cannot silently drop a fact.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import pathlib
import sys
from typing import Any

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import validate_json_schema  # noqa: E402

SCHEMA_PATH = SKILL / "schemas" / "comment.schema.json"
SECTIONS = (
    "bibliographic", "design", "sample", "reported_statistics", "package", "governance",
    "disclosure", "rollup", "timing", "author_response", "run_ref", "publication_redaction",
)
WITHHELD = {
    "intent": "not_assessed",
    "misconduct": "not_assessed",
    "note": ("This programme reports only what the artifacts show. It makes no finding as to cause, "
             "intent, or good faith, and recommends no editorial action. 'consistent_with' fields "
             "locate findings against published editorial standards and are descriptive only."),
}



# Seeded into every comment.json so publish_completed.sh's assertion that both files carry a block
# is true, and so the operator is told which fields a human must clear before release.
#
# Deliberately EXCLUDES /governance/pii_description and /author_response/note. Both are in
# check_release_privacy.REDACTABLE_POINTER_PREFIXES, so declaring them here would mask them and
# stop the privacy gate inspecting two PII-bearing fields on EVERY audit -- a default-on loosening
# of a fail-closed check. None of the three below is allowlisted, so the gate keeps inspecting
# everything it inspects today; declaring them carries the human instruction, nothing more.
# Every remedy_class enum value needs a counter here: finalize_run.sh compares their
# sum against findings_total, so a value with no counter fails finalize loudly rather
# than dropping out of the published distribution silently.
ROLLUP_REMEDY_KEYS = (
    "one_line_code", "one_number", "reanalysis", "not_fixable", "disclose_in_paper")


COMMENT_REDACTION_BASELINE = {
    "fields_requiring_removal": [
        "/governance/ethics_identifier",
        "/findings/*/package_locus",
        "/findings/*/verification/verifiers/*/verifier",
    ],
    "safe_aggregates": [],
    "note": ("Fields a human must review before any external release. Two further pointers -- "
             "/governance/pii_description and /author_response/note -- are release-gate "
             "redactable but are NOT declared here: declaring one suppresses its inspection, so "
             "add it only when that field genuinely needs masking for this audit."),
    "cleared_by": None,
}

def null_skeleton(schema: dict[str, Any], section: str) -> dict[str, Any]:
    """Every property of a section set to the emptiest value its type allows."""
    props = schema["properties"][section].get("properties") or {}
    out: dict[str, Any] = {}
    for key, spec in props.items():
        types = spec.get("type")
        types = types if isinstance(types, list) else [types]
        if "null" in types or types == [None]:
            out[key] = None
        elif "array" in types:
            out[key] = []
        elif "object" in types:
            out[key] = {}
        elif "boolean" in types:
            out[key] = False
        elif "integer" in types or "number" in types:
            out[key] = 0
        elif "string" in types:
            enum = spec.get("enum")
            out[key] = enum[0] if enum else ""
        else:
            out[key] = None
    return out


def merge_facts(base: dict[str, Any], facts: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for section, values in facts.items():
        if section not in SECTIONS or not isinstance(values, dict):
            raise SystemExit(f"facts: unknown or non-object section {section!r}; allowed: {', '.join(SECTIONS)}")
        allowed = set((schema["properties"][section].get("properties") or {}))
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise SystemExit(f"facts.{section}: unknown keys {unknown}")
        if out.get(section) is None:
            out[section] = null_skeleton(schema, section)
        out[section].update(copy.deepcopy(values))
    return out


def build(run: dict[str, Any], facts: dict[str, Any], schema: dict[str, Any], today: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": "1.0.0",
        "audit_id": run["audit_id"],
        "comment_id": None,
        "safe_to_publish": False,
        "publication_redaction": copy.deepcopy(COMMENT_REDACTION_BASELINE),
        "enumeration_scheme": "v1.0",
        "findings": [],
        "assertions_withheld": copy.deepcopy(WITHHELD),
        "run_ref": {"run_json_path": "./run.json", "run_json_sha256": None,
                    "protocol_version": run.get("protocol_version")},
        "notes": None,
    }
    for section in ("bibliographic", "design", "sample", "reported_statistics", "package",
                    "governance", "disclosure", "rollup", "timing", "author_response"):
        record[section] = null_skeleton(schema, section)
    record["bibliographic"]["doi"] = run.get("paper_doi")
    record["bibliographic"]["title"] = run.get("paper_title")
    record["disclosure"].update({"flags": [], "urgency": "none"})
    record["rollup"].update({
        "impact_max": "none", "impact_max_rank": 0, "findings_total": 0,
        "count_by_remedy_class": {key: 0 for key in ROLLUP_REMEDY_KEYS},
        "count_by_impact": None, "tier_distribution": None,
    })
    record["timing"]["audit_date"] = today
    record["author_response"]["status"] = "not_notified"
    record = merge_facts(record, facts, schema)
    # run_ref.protocol_version must be a string if present in the schema as such
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--facts", type=pathlib.Path, default=None,
                        help="JSON object of paper/package facts, sections mirroring the schema")
    parser.add_argument("--force", action="store_true", help="overwrite an existing comment.json")
    parser.add_argument("--dry-run", action="store_true", help="validate and print; write nothing")
    args = parser.parse_args(argv)
    root = args.audit_dir.resolve()
    target = root / "comment.json"
    if target.exists() and not args.force and not args.dry_run:
        print(f"FATAL: {target} exists; pass --force to overwrite", file=sys.stderr)
        return 2
    run = json.loads((root / "run.json").read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    facts: dict[str, Any] = {}
    if args.facts:
        facts_path = args.facts if args.facts.is_absolute() else root / args.facts
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
        if not isinstance(facts, dict):
            print("FATAL: facts file must be a JSON object", file=sys.stderr)
            return 2
    record = build(run, facts, schema, dt.date.today().isoformat())
    errors = validate_json_schema.errors_for_value(record, SCHEMA_PATH)
    if errors:
        print("FATAL: skeleton fails schemas/comment.schema.json:\n  " + "\n  ".join(errors[:20]), file=sys.stderr)
        return 1
    text = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    if args.dry_run:
        print(text)
        return 0
    target.write_text(text, encoding="utf-8")
    print(f"wrote schema-valid comment.json skeleton for {run['audit_id']} ({len(facts)} fact sections)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
