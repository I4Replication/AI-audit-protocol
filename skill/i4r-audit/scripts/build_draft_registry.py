#!/usr/bin/env python3
"""Emit the coordinator's draft finding registry from the five completed Phase-1 roles.

    python3 scripts/build_draft_registry.py "$AUDIT_DIR"

run_coordinator.py requires ``work/registry_draft/*.jsonl`` rows that pass the full
finding-row schema and the semantic rules in schemas/tools/validate_v2.py. Nothing in the
skill previously produced them, and hand-writing 20-odd schema-valid rows is slow and
error-prone. This tool reads ``phase1/quorum.json``, locates each completed role's bound
``result.json`` (in the sample's attempt_NN_<uuid>/ or the focused repair's repair_<uuid>/, checked
against the digest Phase 1 recorded), and writes one draft row per raw role finding.

Rows are mechanical, not adjudicated: every row is status=draft, coordinator_approved=false,
ledger_disposition=INCLUDED, with provenance naming only the role that found it. Merging
duplicates across roles, setting MERGED/EXCLUDED dispositions and refining impact are the
coordinator's job, whether that is the operator editing this file or the Sonnet coordinator
call. Where a schema field has no source in the role result, the mapping below is a stated
heuristic and DRAFT_SUMMARY.md says so.

Every row is validated against the schema and validate_v2 before anything is written.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "schemas" / "tools"))

import validate_json_schema  # noqa: E402
import validate_v2  # noqa: E402

FINDING_ROW_SCHEMA = SKILL / "schemas" / "v2" / "finding-row.schema.json"
ROLE_DOMAIN = {
    "auditor_reproduction": "statistics",
    "auditor_data_integrity": "data",
    "auditor_text_vs_data": "text",
    "auditor_code_methods": "code_methods",
    "auditor_privacy_archive": "privacy_archive",
}
ROLE_SHORT = {
    "auditor_reproduction": "REP",
    "auditor_data_integrity": "DAT",
    "auditor_text_vs_data": "TXT",
    "auditor_code_methods": "COD",
    "auditor_privacy_archive": "PRV",
}
IMPACT_RANK = {
    "none": 0, "unverifiable": 1, "value_wrong": 2, "conclusion_narrowed": 3,
    "conclusion_unsupported": 4, "conclusion_contradicted": 5, "artefact_of_construction": 6,
}
MACHINE_ID = re.compile(r"[^A-Za-z0-9._:-]+")


def load_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recorded_result_sha256(root: pathlib.Path, entry: dict) -> str | None:
    """The result.json digest Phase 1 validated and promoted for one quorum binding.

    A current quorum entry names the role's input hash, and the completion receipt
    phase1/completions/<role>/<input_sha256>.json records ``result_sha256`` for the invocation it
    binds; a pre-receipt quorum entry carries ``result_sha256`` itself. None when neither exists."""
    if isinstance(entry.get("result_sha256"), str):
        return entry["result_sha256"]
    input_hash = entry.get("input_sha256")
    if not isinstance(input_hash, str) or not SHA256_HEX.fullmatch(input_hash):
        return None
    receipt_path = root / "phase1" / "completions" / entry["role"] / f"{input_hash}.json"
    if not receipt_path.is_file():
        return None
    receipt = load_json(receipt_path)
    if receipt.get("invocation_id") != entry["invocation_id"]:
        raise SystemExit(
            f"FATAL: phase1/completions/{entry['role']}/{input_hash}.json binds invocation "
            f"{receipt.get('invocation_id')!r}, not the quorum's {entry['invocation_id']}; restore "
            f"phase1/ exactly as run_phase1.py wrote it")
    digest = receipt.get("result_sha256")
    return digest if isinstance(digest, str) else None


def bound_result(
    root: pathlib.Path, role: str, invocation_id: str, expected_sha256: str | None = None,
) -> tuple[pathlib.Path, dict]:
    """The result.json of the attempt directory whose status.json carries the quorum's invocation id.

    Every attempt directory of the role is searched, as run_phase1.py's own recovery searches them
    (<input_sha256>/*/status.json): attempt_NN_<uuid> for a sample, repair_<uuid> for the focused
    output repair (run_phase1.try_focused_repair) that completes a role whose sample failed only
    structurally -- the quorum then binds the repair's invocation. Until 2.7.4 only attempt_* was
    searched, so the first audit to complete a role by repair (audit A15,
    auditor_code_methods) stopped here with "no bound result.json". Exactly one directory may carry
    the id, and when Phase 1 recorded the promoted result's digest the file must still hash to it."""
    role_root = root / "phase1" / "roles" / role
    matches = []
    for status_path in sorted(role_root.glob("*/*/status.json")):
        if load_json(status_path).get("invocation_id") == invocation_id:
            matches.append(status_path.parent)
    if len(matches) > 1:
        raise SystemExit(
            f"FATAL: {len(matches)} attempt directories of {role} carry invocation {invocation_id}: "
            + ", ".join(path.relative_to(root).as_posix() for path in matches))
    for attempt in matches:
        for candidate in ("output", "output.partial"):
            result_path = attempt / candidate / "result.json"
            if not result_path.is_file():
                continue
            if expected_sha256 is not None and sha256_file(result_path) != expected_sha256:
                raise SystemExit(
                    f"FATAL: {result_path.relative_to(root).as_posix()} does not hash to the "
                    f"result_sha256 Phase 1 recorded for {role} invocation {invocation_id}; restore "
                    f"it exactly as run_phase1.py promoted it")
            return result_path, load_json(result_path)
    raise SystemExit(
        f"FATAL: no bound result.json for {role} invocation {invocation_id} under "
        f"phase1/roles/{role}/<input_sha256>/{{attempt_*,repair_*}}/")


def heuristic_impact(finding: dict) -> str:
    text = str(finding.get("impact") or "").strip().lower()
    if finding.get("conclusion_changing"):
        return "conclusion_narrowed"
    if text.startswith("none") or text.startswith("no effect") or text.startswith("negligible"):
        return "none"
    if "unverifiable" in text or "cannot be verified" in text:
        return "unverifiable"
    return "value_wrong"


def heuristic_remedy_class(remedy: str) -> str:
    text = remedy.lower()
    if any(k in text for k in ("one-digit", "one number", "asterisk", " cell", "correct the table",
                               "printed value", "printed number", "reported value")):
        return "one_number"
    if any(k in text for k in ("re-estim", "refit", "reanaly", "constraint", "re-run the")):
        return "reanalysis"
    # Checked before one_line_code: a remedy that asks the PAPER to state something is not a code
    # change, and filing it as one inflated the bucket the programme reports as its headline.
    # "documentation"/"readme"/"manifest" stay one_line_code -- those are deposit-side files.
    if any(k in text for k in ("disclos", "state in the paper", "report the exclusion",
                               "in the manuscript", "in the article", "footnote", "limitation",
                               "acknowledge")):
        return "disclose_in_paper"
    if any(k in text for k in ("one-line", "one line", "path", "comment", "sessioninfo", "renv",
                               "manifest", "readme", "documentation", "note")):
        return "one_line_code"
    return "not_fixable"


# A finding about a hardcoded path necessarily quotes one. The deposit for audit
# A38 opened with setwd("C:/Users/<user>/...") and setwd("/Users/<user>/..."), and the
# code-and-methods role correctly reported that as a portability defect. The role's REPORT.md is
# scanned for absolute paths and was clean -- the auditor described the paths without quoting
# them -- but result.json is not scanned, so the literal path reached the draft registry and the
# coordinator's provider-safety gate refused the whole 27-row input.
#
# Masking the account segment keeps the finding entirely ("the scripts hardcode a Windows
# drive-letter home path") while removing the one part nobody downstream needs: the authors'
# account name. It is also the right thing for the authors, whose account name should not appear
# in a published I4R comment merely because it appears in their deposit.
# Masking only the account segment is not enough: the coordinator's provider-safety gate matches
# /Users/<anything-not-a-slash>, so a placeholder sitting in the account position still reads as a
# private path. Replace the whole home prefix -- drive letter, Users/home, and account -- with a
# shape-preserving marker. "The scripts hardcode a Windows user-directory path" survives intact;
# only the authors' account name is gone.
WINDOWS_HOME_PREFIX = re.compile(r"(?i)[A-Za-z]:[\\/]+" + "Us" + r"ers[\\/]+[^\\/\s'\"),:;]+[\\/]?")
UNIX_HOME_PREFIX = re.compile(r"(?i)/(?:" + "Us" + r"ers|home)/[^/\s'\"),:;]+/?")


def mask_home_accounts(value):
    """Replace home-directory prefixes in deposit text copied into a draft row.

    A finding about a hardcoded path necessarily quotes one. Audit A38's
    deposit opened with setwd("C:/Users/<user>/...") and setwd("/Users/<user>/..."), and the
    code-and-methods role correctly reported that as a portability defect. The role's REPORT.md
    is scanned for absolute paths and was clean -- the auditor described them without quoting --
    but result.json is not scanned, so the literal path reached the draft registry and the
    coordinator refused the whole 27-row input. Masking here keeps every finding and drops only
    the authors' account name, which should not appear in a published comment anyway.
    """
    if isinstance(value, str):
        value = WINDOWS_HOME_PREFIX.sub("<windows-user-dir>/", value)
        return UNIX_HOME_PREFIX.sub("<unix-user-dir>/", value)
    if isinstance(value, list):
        return [mask_home_accounts(item) for item in value]
    if isinstance(value, dict):
        return {key: mask_home_accounts(item) for key, item in value.items()}
    return value


def clip(text: str, limit: int) -> str:
    """Cut to ``limit`` characters without splitting a word.

    A cut inside a longer token can manufacture a participant-code shape (HR_P95th -> HR_P9) that
    Phase 1 never admitted and the coordinator's provider-safety scan then refuses (2.6.5)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if re.match(r"\w", text[limit]) and re.search(r"\w$", cut):
        head, _, _ = cut.rpartition(" ")
        cut = head if head else cut.rstrip("_0123456789")
    return cut


def build_row(role: str, invocation_id: str, finding: dict, env_fp: str) -> dict:
    finding = mask_home_accounts(finding)
    evidence = finding.get("evidence") or []
    first = evidence[0] if evidence else {}
    impact = heuristic_impact(finding)
    remedy_text = str(finding.get("remedy") or "").strip() or "not stated by the role"
    row_id = f"{ROLE_SHORT[role]}-{MACHINE_ID.sub('-', str(finding['id'])).strip('-')}"
    unverifiable = None
    if impact == "unverifiable":
        unverifiable = {
            "reason": "data_missing", "blocked_tier": 5,
            "attempted_check": clip(str(first.get("result") or finding["claim"]), 300),
            "basis": str(finding.get("impact") or "")[:500] or "see role report",
        }
    return {
        "schema_version": "2.0.0",
        "id": row_id,
        "claim": finding["claim"],
        "domain": ROLE_DOMAIN[role],
        "source_location": {
            "path": first.get("source_path") or "paper/paper.txt",
            "locator": clip(str(first.get("result") or finding["claim"]), 160).strip() or "see claim",
        },
        "claim_ref": None,
        "status": "draft",
        "ledger_disposition": {"status": "INCLUDED", "target": None, "reason": None},
        "coordinator_approved": False,
        "impact": impact,
        "impact_rank": IMPACT_RANK[impact],
        "verification_tier": 5 if impact == "unverifiable" else 2,
        "paper_only": False,
        "confidence": finding["status"],
        "evidence": {
            "source_hashes": [
                {"path": item["source_path"], "sha256": item["source_sha256"]}
                for item in evidence
            ],
            "rerun_command": f"see the {role} evidence manifest bound in phase1/quorum.json",
            "expected": "as reported in the paper",
            "observed": clip(str(first.get("result") or "see claim"), 1000),
            "tolerance_basis": None,
        },
        "unverifiable_basis": unverifiable,
        "verifications": [],
        "dependencies": {
            "environment_fingerprint": env_fp,
            "finding_ids": [],
            "artifact_paths": [],
        },
        "settled": None,
        "benign_explanations_ruled_out": [],
        "report_section": None,
        "verification_snippet": None,
        "disclosure_flag": bool(finding.get("disclosure_critical")),
        "remedy": {
            "class": heuristic_remedy_class(remedy_text),
            "description": remedy_text,
            "verified": None,
        },
        # A draft row asserts nothing. "This changes the paper's conclusion" is the strongest
        # claim the registry can carry, and validate_v2 requires two independent confirmations
        # for it at every stage, deliberately -- see
        # test_conclusion_change_requires_independence_even_before_promotion. One auditor's flag
        # is not that. Copying it here made a conclusion-changing finding unrepresentable and
        # aborted the whole draft registry; the flag is surfaced to the coordinator in
        # DRAFT_SUMMARY.md instead, and it sets the field once the confirmations exist.
        "changes_conclusion": False,
        "changes_conclusion_which": None,
        "provenance": {
            "origin_role": role,
            "independently_found_by": [role],
            "agreed_after_seeing": [],
            "round_introduced": 0,
            "source_records": [f"phase1/quorum.json#{invocation_id}"],
        },
    }


def environment_fingerprint(root: pathlib.Path, snapshot_sha: str) -> str:
    path = root / "phase1" / "snapshots" / snapshot_sha / "evidence" / "environment_fingerprint.json"
    if not path.is_file():
        return f"phase1 snapshot {snapshot_sha[:16]}"
    fp = load_json(path)
    parts = [f"{k}={v}" for k, v in sorted(fp.items()) if isinstance(v, (str, int)) and k != "schema_version"]
    return "; ".join(parts)[:400] or f"phase1 snapshot {snapshot_sha[:16]}"


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if any(value in {"-h", "--help"} for value in args):
        print(__doc__)
        print("  add --dry-run to validate and report without writing anything")
        return 0
    dry_run = "--dry-run" in args
    args = [value for value in args if value != "--dry-run"]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        print("  add --dry-run to validate and report without writing anything", file=sys.stderr)
        return 2
    root = pathlib.Path(args[0]).resolve()
    quorum = load_json(root / "phase1" / "quorum.json")
    if quorum.get("status") != "quorum_complete":
        raise SystemExit(f"FATAL: Phase-1 quorum is {quorum.get('status')!r}, not quorum_complete")
    env_fp = environment_fingerprint(root, quorum["source_snapshot_sha256"])

    rows: list[dict] = []
    summary: list[str] = ["# Draft registry summary", "",
                          "Mechanical rows, one per raw Phase-1 finding. Nothing is merged or",
                          "adjudicated; impact and remedy class are heuristics to be reviewed.", ""]
    for entry in quorum["roles"]:
        role, inv = entry["role"], entry["invocation_id"]
        _, result = bound_result(root, role, inv, recorded_result_sha256(root, entry))
        sci = result["scientific_fields"]
        for finding in sci["findings"]:
            rows.append(build_row(role, inv, finding, env_fp))
        claimed = [f["id"] for f in sci["findings"] if f.get("conclusion_changing")]
        summary.append(f"## {role}")
        summary.append(f"- findings: {len(sci['findings'])}")
        if claimed:
            summary.append(
                f"- **claimed conclusion-changing (UNCONFIRMED, one auditor): "
                f"{', '.join(claimed)}** — draft rows carry changes_conclusion=false and must KEEP "
                f"it false in this draft: the validator accepts changes_conclusion=true only with "
                f"confirmations from two model families, and every Phase-1 auditor is the same "
                f"family, so no pre-critique row can carry it whatever the number of auditors agreeing. "
                f"Say that the finding is candidate conclusion-changing in ADJUDICATION.md only -- not "
                f"in the claim text, which Appendix A projects verbatim; round 1 supplies the second family."
            )
        summary.append(f"- passed checks: {', '.join(sci['passed_check_ids']) or 'none'}")
        summary.append(f"- open questions: {', '.join(sci['open_question_ids']) or 'none'}")
        summary.append("")

    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("FATAL: duplicate draft row ids after sanitisation")
    for index, row in enumerate(rows, 1):
        errors = validate_json_schema.errors_for_value(row, FINDING_ROW_SCHEMA)
        if errors:
            raise SystemExit(f"FATAL: row {index} ({row['id']}) fails schema: {errors[:3]}")
        validate_v2.validate_finding_row(row, f"draft[{index}]")

    if dry_run:
        print(f"dry run: {len(rows)} schema-valid draft rows from {len(quorum['roles'])} roles; "
              f"nothing written")
        return 0
    out = root / "work" / "registry_draft"
    out.mkdir(parents=True, exist_ok=True)
    (out / "draft_findings.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    (out / "DRAFT_SUMMARY.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} schema-valid draft rows from {len(quorum['roles'])} roles to "
          f"work/registry_draft/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
