#!/usr/bin/env python3
"""Run the packaged family-A post-fan-out verifier on registry rows raised after Phase 1 (2.6.4).

    python3 scripts/run_verifier.py <audit-dir> --findings F13 F14 [--dry-run]

A registry row is promotable only with confirmations from two model families. Before 2.6.4 the
family-A (Claude) confirmation could come only from the Phase-1 auditor whose report raised the
row, so a finding first raised after fan-out -- by the operator, the coordinator or a critic --
could never be promoted. In the Neudorfer audit (20260923-audit-3309cfcf) three true article-text
findings (F13-F15) were excluded for exactly that reason, after hand-made verification records
were refused by make_settled (role_class other) and then by finalize (a record relabelled as an
auditor counted as a sixth auditor).

This runner is the packaged role that closes the gap. It sends a batch of INCLUDED, post-fan-out
rows to one blind call on the Phase-1 auditors' own Claude route (Sonnet 5 at max for an audit
initialised before 2.7.0, Sonnet 5.5 at max on 2.7.0 to 2.7.2, Sonnet 5.5 at xhigh from 2.7.3), under
the exact Phase-1 runtime contract:

1. refuse, with zero provider calls and no journal event, anything the contract does not admit:
   a pre-metadata-2.1 audit, a Phase 1 without a complete quorum, a snapshot that no longer
   verifies, an ineligible row (verifier_contract.eligibility_problem), a row with no admissible
   paper/ or pkg/ source, and a claim text that already has an admitted verdict
   (verifier_contract.collect_verdicts: one verdict per claim text);
2. reuse -- never rebuild -- the snapshot phase1/quorum.json names, build the blind packet (the
   claim verbatim, its hash, its source locus and snapshot-bound paper/pkg hashes; nothing a
   reviewer or the coordinator concluded) and freeze it under
   phase1/role_packets/<snapshot>/<role>/<packet_hash>/;
3. run the provider-free preflight in the verifier's own workspace, then reuse the capability
   probe cache;
4. dispatch through run_phase1.run_role with the auditors' CLI flags, sandbox, timeout, cost cap,
   retry and budget semantics (3 accepted starts per (role, 0) by default, I4R_MAX_ATTEMPTS up
   to 20) and one structural focused repair, plus extra read denials for the coordinator's
   reasoning and the settled/critic artefacts;
5. admit the output with the verifier validator below, which reuses the generic Phase-1 checks
   (phase1_runtime.read_role_output ... check_output_tree) and adds the verifier rules, promote it
   to work/verifier/<role>/<input_hash>/, record the terminal row (role_class verifier, round 0,
   output_artifact = result.json, input_artifact_sha256 = input_hash) through the lifecycle API,
   and write the i4r-verifier-completion-1 receipt that verifier_contract.verify_record requires.

The runner writes only phase1/verifier/<role>/status.json for its own status. It never writes
phase1/status.json or phase1/quorum.json and never emits phase or pause events, so the fan-out
record of the audit is untouched. A completed batch replays from its receipt as a cache_replay
row; a batch whose receipt was lost after its terminal row (a kill between the two) rebuilds the
receipt on the same command, with no provider call. Every gate re-admits a verifier call through
readmit_record below (verifier_contract.verify_record calls it), so only what this runner
produced counts. Each attempt's sandbox denies every other attempt's output, the other roles'
packets and the finalization/release/tree records (BatchPlan.deny_read), computed per attempt.
Exit codes: 0 completed or replayed (or a --dry-run plan), 1 usage or unexpected error,
2 the verifier call failed, 3 local preflight failure or sandbox environment fault, 4 capability
probe failure, 5 refused.
"""

from __future__ import annotations

import argparse
import decimal
import fcntl
import functools
import json
import os
import pathlib
import posixpath
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
import claude_routes  # noqa: E402
import phase1_runtime as runtime  # noqa: E402
import run_phase1  # noqa: E402
import lifecycle_journal  # noqa: E402
import verifier_contract as contract  # noqa: E402


STATUS_VERSION = "i4r-verifier-status-1"
PACKET_LAYOUT_VERSION = "i4r-verifier-packet-layout-1"
PROJECTION_VERSION = "i4r-verifier-scientific-projection-1"
COMPLETION_MARKER = "<!-- i4r-role-complete:verifier -->"
REPORT_HEADINGS = ("Result summary", "Hypotheses tested", "Evidence inventory")
# Routing aids only, exactly as for the auditors; none carries a model conclusion.
ASSIGNED_EVIDENCE = ("pdf_text.txt", "file_inventory.json", "workbook_summaries.json", "code_entrypoints.json")
SOURCE_PREFIXES = ("paper/", "pkg/")
RESULT_KEYS = frozenset({
    "schema_version", "role", "status", "completion_marker", "source_snapshot_sha256",
    "hypotheses_sha256", "report", "evidence_manifest", "scientific_fields",
})
VERDICT_KEYS = frozenset({"hypothesis_id", "claim_sha256", "verdict", "evidence", "rerun", "basis"})
RECEIPT_KEYS = frozenset({
    "schema_version", "status", "role", "input_sha256", "source_snapshot_sha256", "packet_sha256",
    "invocation_id", "attempt", "attempt_kind", "source_invocation_id", "result", "result_sha256",
    "report", "report_sha256", "evidence", "evidence_manifest_sha256", "hypotheses",
    "source_bindings",
})
# Blind framing: beyond the Phase-1 protected list (paper/, pkg/, report/, work/, critique/, the
# journal and ledgers), the verifier may not read the coordinator's reasoning or any artefact
# that records a reviewer's verdict on the claims it tests.
EXTRA_DENY_READ = (
    "coordinator", "SETTLED.json", "SETTLED.md", "comment.json", "claims_map.tsv",
    "verification_snippets", "WRAPUP.txt",
    # Earlier batches' receipts name finding ids, and their status files record which rows were
    # sent: both would tell a blind verifier what the operator already believes.
    "phase1/completions", "phase1/verifier",
    # Promotion staging, and the orphaned trees a killed promotion leaves behind.
    "phase1/transactions",
)
# The deny list is a deny list: whatever it does not name is readable. Beyond the fixed names
# above, each attempt also denies (BatchPlan.deny_read, computed for that attempt's read roots):
# every other attempt's retained output under phase1/roles (earlier verifier samples, including
# rejected verdicts on the same claim, and full copies of the five auditors' reports) and every
# other verifier batch's frozen packet; the artefacts finalization writes at the audit root, which
# exist whenever a round is reopened after finalize; the release staged beside the audit; and this
# audit's own published records in its tree (tree_records_of_audit). Only what exists is denied
# (2.6.6, _holds_something), because every denial costs sandbox profile on every Bash call.
LATE_ROOT_ARTIFACTS = (
    "claims_script.R", "verification.Rmd", "verification.html", "verification_summary.csv",
    "verification_manifest.json", "RUN_METADATA.md", "run_metrics.json",
    "finalization_receipt.json", "RELEASE_MANIFEST.md", "release-manifest.json",
    "document_build_manifest.json",
)
# Written from the invocation ledger at every summary, long before finalization adds results.
TELEMETRY_ROOT_ARTIFACTS = ("RUN_METADATA.md", "run_metrics.json")
FINALIZATION_MARKERS = ("finalization_receipt.json", "release-manifest.json", "RELEASE_MANIFEST.md",
                        "document_build_manifest.json")
LATE_ROOT_PREFIX = "verification"  # verification.Rmd/.html/_summary.csv/_manifest.json and kin
# scripts/tree_layout.sh: a tree root is recognised by its markers, never by its depth alone.
TREE_MARKERS = ("data", "reservations", "Completed reports")
# The routes the packaged runtime dispatches, parses and records (2.7.0: claude_routes.LIVE_ROUTES,
# Sonnet 5 or Sonnet 5.5 at max effort; 2.7.3 adds Sonnet 5.5 at xhigh, what a new audit freezes;
# execution_errors judges the served model on the dispatched
# route and the terminal record is written for it). A verifier also runs only on the Phase-1
# auditors' own route, whose capability probe it reuses.
PACKAGED_ROUTES = claude_routes.LIVE_ROUTES
# The one route every audit initialised before 2.7.0 froze; kept for importers (the test fixtures
# build their legacy verifier packets on it).
PACKAGED_ROUTE = dict(claude_routes.SONNET_5)
HYPOTHESIS_ID = re.compile(r"^H[1-9][0-9]*$")

EXIT_COMPLETED, EXIT_ERROR, EXIT_FAILED, EXIT_PAUSED, EXIT_PROBE, EXIT_REFUSED = 0, 1, 2, 3, 4, 5


# ------------------------------------------------------------------------------ the contract rule
# Every rule the verifier validator can reject an output on, with the phrase(s) of the verifier
# prompt that state it. tests/test_prompt_contract.py (VerifierPromptNamesValidatorRules) renders
# the prompt and fails if any phrase is missing, drives the validator into every rule, and fails
# on a rejection whose rule is not registered here. _reject() refuses an unregistered rule, so a
# new rejection cannot be added without stating it to the model.
VERIFIER_RULES: dict[str, tuple[str, ...]] = {
    # Generic Phase-1 admission checks (phase1_runtime), shared with the auditors.
    "output_files": ("exactly REPORT.md, result.json, evidence/manifest.json",),
    "report_utf8": ("REPORT.md is UTF-8 text",),
    "result_json": ("a single JSON object with no duplicate keys",),
    "report_hash": ('`report` is exactly {"path": "REPORT.md", "sha256": <hash>}',),
    "manifest_hash": ('`evidence_manifest` is exactly {"path": "evidence/manifest.json", "sha256": <hash>}',),
    "report_absolute_path": ("Never write an absolute filesystem path anywhere in REPORT.md or result.json",
                             "/" + "Users/", "/home/", "/private/tmp/", "/private/var/"),
    "result_absolute_path": ("anywhere in REPORT.md or result.json", "<unix-user-dir>"),
    "participant_date": ("No line may place a participant identifier and a date or clock time together",),
    "report_participant_code": ("Never write a token shaped like a participant code",
                                "such as SUBJ_P12 or EMG_P01", "The rule is mechanical",
                                "must not be cited in a source join or an evidence path"),
    "result_participant_code": ("anywhere in the report OR in result.json",
                                "describe participants by counts or row positions",
                                "bind that evidence to another manifest entry",
                                "is not an identifier and may be quoted"),
    "report_join_missing": ("at least one exact source join", "<!-- source:path@sha256 -->"),
    "report_join_unbound": ("must be copied exactly from ROLE_PACKET.source_manifest",),
    "manifest_header": ("The manifest has schema_version `i4r-verifier-result-1`, role, "
                        "source_snapshot_sha256", "non-empty artifacts rows"),
    "manifest_bindings_missing": ("non-empty source_bindings",),
    "manifest_binding_unbound": ("objects keyed `path` and `sha256`",),
    "output_symlink": ("no symlink or special file",),
    "artifact_row": ("rows with path, sha256, and bytes", "listed once"),
    "artifact_path": ("must NOT begin with `evidence/` or `./`",),
    "artifact_hash": ("sha256 and bytes must match the final file",),
    "manifest_closure": ("closes over every evidence file except itself",),
    "closed_file_set": ("CLOSED contract",),
    # Verifier-specific rules.
    "result_keys": ("exactly these top-level keys",),
    "result_header": ("schema_version `i4r-verifier-result-1`", "status `complete`",
                      "completion_marker true (a JSON boolean)"),
    "result_snapshot": ("source_snapshot_sha256 copied from ROLE_PACKET",),
    "hypotheses_hash": ("hypotheses_sha256 copied from ROLE_PACKET.hypotheses_sha256",),
    "scientific_fields": ("scientific_fields has exactly one key, `verdicts`",),
    "verdict_keys": ("exactly hypothesis_id, claim_sha256, verdict, evidence, rerun and basis",),
    "verdict_set": ("exactly one verdict per packet hypothesis",),
    "verdict_claim_hash": ("claim_sha256 copied exactly from that hypothesis",),
    "verdict_value": ("exactly one of CONFIRMED, REFUTED or UNVERIFIABLE",),
    "verdict_evidence": ("non-empty array of exact {source_path,source_sha256,result} records",),
    "verdict_rerun": ("`rerun` is exactly", "`rerun.artifact`", "CONFIRMED and REFUTED need a rerun artifact",
                      "UNVERIFIABLE may set both to null"),
    "verdict_basis": ("`basis` is a non-empty string",),
    "report_placeholder": ("TODO, TBD, PLACEHOLDER", "must not be empty"),
    "report_headings": ("## Result summary", "## Hypotheses tested", "## Evidence inventory"),
    "report_marker": ("exactly one " + COMPLETION_MARKER,),
    "report_blocks": ("### H1: CONFIRMED", "followed by at least one line of prose",
                      "no other level-3 heading in that section"),
    "frozen_projection": ("format-only repair",),
}


def _reject(rule: str, message: str, *, repairable: bool = False) -> None:
    if rule not in VERIFIER_RULES:
        raise AssertionError(f"verifier rejection rule {rule!r} is not registered in VERIFIER_RULES")
    raise runtime.OutputValidationError(message, repairable=repairable, rule=rule)


class VerifierRefusal(run_phase1.RunnerError):
    """The batch may not be sent. Raised before any provider call or journal event."""

    def __init__(self, reason: str, remedy: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.remedy = remedy


class PreflightFailure(run_phase1.RunnerError):
    """Provider-free preparation failed; nothing was dispatched."""


# ------------------------------------------------------------------------------------ the prompt
def build_verifier_prompt(role: str, audit_id: str, packet: dict[str, Any]) -> str:
    """The verifier's instructions. States every rule in VERIFIER_RULES (the contract rule)."""
    hypotheses = packet.get("hypotheses") or []
    listed = "\n".join(
        f"  {row['hypothesis_id']}: claim_sha256={row['claim_sha256']}" for row in hypotheses
    )
    home = run_phase1.HOME_SEGMENT
    # 2.7.0: the model named is the packet's own route. readmit_record rebuilds this prompt from
    # the frozen packet, so a Sonnet 5 packet, or one with no model block, renders the 2.6.10 bytes.
    # 2.7.3: the effort too (claude_routes.effort_of). Every packet frozen before 2.7.3 is a max packet
    # (or has no model block) and renders "at max effort", byte for byte, so its records re-admit.
    model = claude_routes.display_name(claude_routes.requested_model_of(packet))
    effort = claude_routes.effort_of(packet)
    return f"""You are the independent I4R post-fan-out verifier. Use {model} at {effort} effort.

Audit identity: {audit_id}
Role: {role}
Immutable source snapshot: {packet.get('source_snapshot_sha256')}
hypotheses_sha256: {packet.get('hypotheses_sha256')}
Hypotheses to test ({len(hypotheses)}):
{listed}

Read ROLE_PACKET.json in the role packet directory. Its `hypotheses` array gives each claim you
must test, verbatim, with its hypothesis_id, its claim_sha256, the source_location the claim is
about, and the source_hashes of the deposited paper/ and pkg/ files that bear on it. Test every
hypothesis against the raw, read-only paper/ and pkg/ material exposed directly under the source
directory. The indexes under the packet's evidence/ directory are routing aids, never sources.

The claims were raised after the Phase-1 audit and you are their independent check: do not
assume any of them is true. Test each claim as written, every element of it. The verdict is
CONFIRMED only when the deposit supports the whole claim; REFUTED when the deposit contradicts
the claim or any element of it; UNVERIFIABLE when the deposit lacks what the test needs, and
then `basis` names exactly what the deposit lacks. Do not access the live audit, another role or
attempt, the internet, plugins, browsers, MCP servers, authors, or journals. Never infer intent.
Copy code that may write beside its inputs into this attempt output directory before running
it, then promote or delete those working copies before you return.

Work plan. Every tool call re-reads your whole context, so cost scales with the number of turns.
Read the packet and the raw sources first; then write ONE check script (Python or R) per
hypothesis, or one for all of them, under evidence/, that performs the test and writes its
results as JSON files under evidence/; run it once and fix it rather than issuing one command per
check; then write REPORT.md from those results.

Return only after these three outputs are complete:

* REPORT.md with exactly these level-2 sections: ## Result summary, ## Hypotheses tested and
  ## Evidence inventory. Under ## Hypotheses tested write one block per hypothesis: a level-3
  heading of exactly the form `### H1: CONFIRMED` -- the hypothesis_id, a colon, one space, and
  the verdict word that result.json records for that hypothesis -- followed by at least one line
  of prose saying what you checked and what it showed. Use no other level-3 heading in that
  section, and give each hypothesis exactly one block. Include at least one exact source join
  copied from ROLE_PACKET.source_manifest as <!-- source:path@sha256 --> and write exactly one
  {COMPLETION_MARKER} marker. REPORT.md is UTF-8 text and must not be empty. Never write an
  absolute filesystem path anywhere in REPORT.md or result.json: refer to deposited files by
  their manifest-relative paths such as pkg/analysis.R. `{home}`, `/home/`, `/private/tmp/` and
  `/private/var/` all count, including a path you found inside the deposit: mask its
  home-directory prefix as `<windows-user-dir>/rest/of/path` or `<unix-user-dir>/rest/of/path`.
  No line may place a participant identifier and a date or clock time together. {runtime.provider_safety.RULE_STATEMENT} No line
  anywhere in the report may consist solely of TODO, TBD, PLACEHOLDER, or a bracketed insert
  marker.
* evidence/manifest.json plus the evidence files. The manifest has schema_version
  `i4r-verifier-result-1`, role, source_snapshot_sha256, non-empty source_bindings copied exactly
  from ROLE_PACKET.source_manifest as objects keyed `path` and `sha256`, and non-empty artifacts
  rows with path, sha256, and bytes, each file listed once. Every source hash you cite anywhere --
  report joins, source_bindings, and verdict evidence -- must be copied exactly from
  ROLE_PACKET.source_manifest, which lists only the deposited paper/ and pkg/ files. NEVER cite
  the packet's ROLE_PACKET.assigned_evidence index files (pdf_text.txt, file_inventory.json,
  workbook_summaries.json, code_entrypoints.json) as sources. Each artifact `path` is relative to
  the evidence directory itself and must NOT begin with `evidence/` or `./`: a file written to
  evidence/h1_check.py is the row path `h1_check.py`. Its sha256 and bytes must match the final
  file. The manifest closes over every evidence file except itself.
* result.json, a single JSON object with no duplicate keys and exactly these top-level keys:
  schema_version `i4r-verifier-result-1`, role `{role}`, status `complete`, completion_marker true
  (a JSON boolean), source_snapshot_sha256 copied from ROLE_PACKET, hypotheses_sha256 copied from
  ROLE_PACKET.hypotheses_sha256, report, evidence_manifest and scientific_fields. `report` is
  exactly {{"path": "REPORT.md", "sha256": <hash>}} and `evidence_manifest` is exactly
  {{"path": "evidence/manifest.json", "sha256": <hash>}}: those path strings are compared
  literally. scientific_fields has exactly one key, `verdicts`: an array with exactly one verdict
  per packet hypothesis, no more and no fewer. Each verdict has exactly hypothesis_id,
  claim_sha256, verdict, evidence, rerun and basis. hypothesis_id is the packet's id;
  claim_sha256 copied exactly from that hypothesis in ROLE_PACKET; verdict is exactly one of
  CONFIRMED, REFUTED or UNVERIFIABLE; evidence is a non-empty array of exact
  {{source_path,source_sha256,result}} records, each naming a DEPOSITED file from
  ROLE_PACKET.source_manifest with that manifest's exact sha256 and saying in `result` what you
  observed; `rerun` is exactly {{"artifact": ..., "command": ...}}, where `rerun.artifact` is the
  evidence/-prefixed path of a check script listed in the manifest (the manifest row
  `h1_check.py` is `evidence/h1_check.py`) and `rerun.command` is the non-empty command that
  reruns it. CONFIRMED and REFUTED need a rerun artifact; UNVERIFIABLE may set both to null.
  `basis` is a non-empty string of one or two sentences.
  The verdicts, the report bytes, and every evidence-artifact byte are authoritative during a
  focused format-only repair and must not change in such a repair.

The output directory is a CLOSED contract. When you return it must contain exactly REPORT.md,
result.json, evidence/manifest.json, and the evidence files listed in that manifest, with no
symlink or special file -- nothing else. Any scratch copy, working script, intermediate log or
staging directory you created must either be promoted into evidence/ and listed in the manifest,
or deleted before you finish.

Write result.json last. The report {{path,sha256}} and evidence_manifest {{path,sha256}} entries
must hash the final bytes on disk, so make every edit to REPORT.md and to each evidence file
first, then compute the hashes, then write result.json. Validate all hashes and paths locally
before returning. Your final response is only a short completion notice; the files are
authoritative.
"""


# ------------------------------------------------------------------------ the admission contract
def _packet_expectations(packet: dict[str, Any]) -> dict[str, str]:
    return {row["hypothesis_id"]: row["claim_sha256"] for row in packet["hypotheses"]}


def _rerun_artifact_shape(value: Any) -> str | None:
    """The evidence-relative path a rerun artifact names, or None when it is not well formed."""
    if not isinstance(value, str) or not value.startswith("evidence/"):
        return None
    relative = value[len("evidence/"):]
    if not relative or posixpath.normpath(relative) != relative or relative.startswith(("./", "/")) \
            or relative == "manifest.json" or ".." in pathlib.PurePosixPath(relative).parts:
        return None
    return relative


def validated_verdicts(
    scientific: Any, packet: dict[str, Any], snapshot_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Validate scientific_fields.verdicts against the packet and the snapshot."""
    if not isinstance(scientific, dict) or set(scientific) != {"verdicts"}:
        _reject("scientific_fields", "verifier scientific_fields must have exactly one key, verdicts")
    verdicts = scientific["verdicts"]
    if not isinstance(verdicts, list) or not verdicts:
        _reject("scientific_fields", "verifier scientific_fields.verdicts must be a non-empty array")
    expected = _packet_expectations(packet)
    known = runtime.source_hashes(snapshot_manifest)
    seen: list[str] = []
    for index, item in enumerate(verdicts):
        if not isinstance(item, dict) or set(item) != VERDICT_KEYS:
            _reject("verdict_keys", f"verdicts[{index}] must have exactly hypothesis_id, claim_sha256, "
                    "verdict, evidence, rerun and basis")
        hypothesis_id = item["hypothesis_id"]
        if not isinstance(hypothesis_id, str) or hypothesis_id not in expected or hypothesis_id in seen:
            _reject("verdict_set", f"verdicts[{index}].hypothesis_id {hypothesis_id!r} is not a packet "
                    "hypothesis or is repeated; give exactly one verdict per packet hypothesis")
        seen.append(hypothesis_id)
        if item["claim_sha256"] != expected[hypothesis_id]:
            # Never repairable: a verdict bound to another hash is a verdict on another claim.
            _reject("verdict_claim_hash", f"verdict {hypothesis_id} claim_sha256 is not the packet's "
                    f"{expected[hypothesis_id]}")
        if item["verdict"] not in contract.VERDICTS:
            _reject("verdict_value", f"verdict {hypothesis_id} is {item['verdict']!r}, not one of "
                    "CONFIRMED, REFUTED or UNVERIFIABLE")
        evidence = item["evidence"]
        if not isinstance(evidence, list) or not evidence:
            _reject("verdict_evidence", f"verdict {hypothesis_id} evidence must be a non-empty array")
        for evidence_index, row in enumerate(evidence):
            # An exact join: both sides are strings and the snapshot holds that path at that hash.
            # known.get(path) != sha alone admitted a file absent from the deposit with a null
            # hash (None == None), and raised TypeError on an unhashable path.
            if (
                not isinstance(row, dict) or set(row) != {"source_path", "source_sha256", "result"}
                or not isinstance(row.get("source_path"), str)
                or not isinstance(row.get("source_sha256"), str)
                or known.get(row["source_path"]) != row["source_sha256"]
                or not isinstance(row.get("result"), str) or not row["result"].strip()
            ):
                _reject("verdict_evidence", f"verdict {hypothesis_id} evidence[{evidence_index}] is not an "
                        "exact {source_path,source_sha256,result} record bound to a deposited source")
        rerun = item["rerun"]
        if not isinstance(rerun, dict) or set(rerun) != {"artifact", "command"}:
            _reject("verdict_rerun", f"verdict {hypothesis_id} rerun must be exactly {{artifact, command}}")
        if rerun["artifact"] is None:
            if item["verdict"] != "UNVERIFIABLE":
                _reject("verdict_rerun", f"verdict {hypothesis_id} is {item['verdict']}: CONFIRMED and "
                        "REFUTED need a rerun artifact")
            if rerun["command"] is not None:
                _reject("verdict_rerun", f"verdict {hypothesis_id} has no rerun artifact, so its rerun "
                        "command must be null too")
        else:
            if _rerun_artifact_shape(rerun["artifact"]) is None:
                _reject("verdict_rerun", f"verdict {hypothesis_id} rerun.artifact {rerun['artifact']!r} "
                        "is not an evidence/-prefixed evidence file path")
            if not isinstance(rerun["command"], str) or not rerun["command"].strip():
                _reject("verdict_rerun", f"verdict {hypothesis_id} rerun.command must be a non-empty string")
        if not isinstance(item["basis"], str) or not item["basis"].strip():
            _reject("verdict_basis", f"verdict {hypothesis_id} basis must be a non-empty string")
    missing = sorted(set(expected) - set(seen))
    if missing:
        _reject("verdict_set", f"verifier result has no verdict for {', '.join(missing)}; give exactly "
                "one verdict per packet hypothesis")
    return scientific


def _check_result_header(result: dict[str, Any], role: str, snapshot_manifest: dict[str, Any]) -> None:
    if set(result) != RESULT_KEYS:
        extra = sorted(set(result) - RESULT_KEYS)
        absent = sorted(RESULT_KEYS - set(result))
        _reject("result_keys", f"verifier result.json must have exactly these top-level keys; "
                f"missing={absent} extra={extra}")
    if (
        result.get("schema_version") != contract.RESULT_VERSION
        or result.get("role") != role
        or result.get("status") != "complete"
        or result.get("completion_marker") is not True
    ):
        _reject("result_header", "verifier result is not a completed i4r-verifier-result-1 result "
                "for this role")
    if result.get("source_snapshot_sha256") != snapshot_manifest["snapshot_sha256"]:
        _reject("result_snapshot", "verifier result does not bind the immutable source snapshot")


def _check_hypothesis_blocks(report_text: str, verdicts: list[dict[str, Any]]) -> None:
    # Exactly one section, and every verdict block inside it: a second "## Hypotheses tested" (or
    # a "### H1: REFUTED" anywhere else) would leave a promoted report that contradicts itself.
    if len(re.findall(r"(?im)^##\s+Hypotheses tested\s*$", report_text)) != 1:
        _reject("report_blocks", "REPORT.md must have exactly one ## Hypotheses tested section")
    all_blocks = re.findall(
        r"(?m)^###[ \t]+H[1-9][0-9]*:[ \t]+(?:CONFIRMED|REFUTED|UNVERIFIABLE)\b", report_text)
    section = re.search(r"(?ims)^##\s+Hypotheses tested\s*$\n(.*?)(?=^##\s|\Z)", report_text)
    body = section.group(1) if section else ""
    headings = list(re.finditer(r"(?m)^###[ \t]+(.*?)[ \t]*$", body))
    found: dict[str, str] = {}
    for index, heading in enumerate(headings):
        match = re.fullmatch(r"(H[1-9][0-9]*):[ \t]+(CONFIRMED|REFUTED|UNVERIFIABLE)", heading.group(1))
        if match is None:
            _reject("report_blocks", f"Hypotheses tested heading {heading.group(0).strip()!r} is not of "
                    "the form `### H1: CONFIRMED`")
        hypothesis_id, verdict = match.groups()
        if hypothesis_id in found:
            _reject("report_blocks", f"REPORT.md has more than one block for {hypothesis_id}")
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        if not body[heading.end():end].strip():
            _reject("report_blocks", f"REPORT.md block for {hypothesis_id} has no prose")
        found[hypothesis_id] = verdict
    if len(all_blocks) != len(found):
        _reject("report_blocks", "a hypothesis verdict block appears outside ## Hypotheses tested")
    expected = {row["hypothesis_id"]: row["verdict"] for row in verdicts}
    if set(found) != set(expected):
        _reject("report_blocks", "REPORT.md Hypotheses tested blocks do not match the verdicts: "
                f"missing={sorted(set(expected) - set(found))} extra={sorted(set(found) - set(expected))}")
    for hypothesis_id, verdict in sorted(expected.items()):
        if found[hypothesis_id] != verdict:
            _reject("report_blocks", f"REPORT.md names {found[hypothesis_id]} for {hypothesis_id} but "
                    f"result.json records {verdict}")


def verifier_projection(
    output: pathlib.Path, role: str, snapshot_manifest: dict[str, Any], packet: dict[str, Any],
) -> dict[str, Any]:
    """The byte-frozen surface of a verifier output: verdicts, report prose, evidence artifacts."""
    with runtime._admission_rule("output_files"):
        report = runtime.regular_file(output / "REPORT.md", "role report")
        result = runtime.strict_json(runtime.regular_file(output / "result.json", "role result"))
        runtime.regular_file(output / "evidence" / "manifest.json", "role evidence manifest")
    if (
        result.get("schema_version") != contract.RESULT_VERSION
        or result.get("role") != role
        or result.get("status") != "complete"
        or result.get("completion_marker") is not True
        or result.get("source_snapshot_sha256") != snapshot_manifest.get("snapshot_sha256")
    ):
        _reject("result_header", "verifier result is not eligible for focused repair")
    scientific = validated_verdicts(result.get("scientific_fields"), packet, snapshot_manifest)
    report_bytes = report.read_bytes()
    return {
        "schema_version": PROJECTION_VERSION,
        "role": role,
        "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
        "scientific_fields": scientific,
        "verdict_sha256": {
            row["hypothesis_id"]: runtime.sha256_bytes(runtime.canonical_bytes(row))
            for row in scientific["verdicts"]
        },
        "report_prose": {
            "path": "REPORT.md", "bytes": len(report_bytes),
            "sha256": runtime.sha256_bytes(report_bytes),
        },
        "evidence_artifacts": runtime.frozen_evidence_artifacts(output / "evidence"),
    }


def validate_verifier_output(
    output: pathlib.Path,
    role: str,
    snapshot_manifest: dict[str, Any],
    packet: dict[str, Any],
    *,
    expected_scientific_projection: dict[str, Any] | None = None,
    deposit_exemptions: Iterable[str] = frozenset(),
    participant_code_rule: bool = True,
) -> runtime.ValidatedRoleOutput:
    """Admit one verifier output, or reject it as an invalid sample (never a runner crash)."""
    try:
        return _validated_verifier_output(
            output, role, snapshot_manifest, packet,
            expected_scientific_projection=expected_scientific_projection,
            deposit_exemptions=deposit_exemptions,
            participant_code_rule=participant_code_rule,
        )
    except runtime.OutputValidationError:
        raise
    except runtime.Phase1Error as exc:
        raise runtime.OutputValidationError(str(exc)) from exc
    except (TypeError, KeyError, AttributeError) as exc:
        # Every fault reachable from the provider's own output tree is a property of this sample:
        # an unexpected JSON shape must spend a fresh sample, not end the batch as "permanent".
        raise runtime.OutputValidationError(
            f"verifier output is malformed: {type(exc).__name__}: {exc}", rule="result_json",
        ) from exc


def _validated_verifier_output(
    output: pathlib.Path,
    role: str,
    snapshot_manifest: dict[str, Any],
    packet: dict[str, Any],
    *,
    expected_scientific_projection: dict[str, Any] | None = None,
    deposit_exemptions: Iterable[str] = frozenset(),
    participant_code_rule: bool = True,
) -> runtime.ValidatedRoleOutput:
    report, result_path, manifest_path, report_text, result = runtime.read_role_output(output)
    _check_result_header(result, role, snapshot_manifest)
    scientific = validated_verdicts(result["scientific_fields"], packet, snapshot_manifest)
    if expected_scientific_projection is not None:
        # Checked before anything a repair may legitimately fix, so a repair that touched a
        # verdict, the report or an evidence artifact is always named for what it did.
        observed = verifier_projection(output, role, snapshot_manifest, packet)
        if runtime.canonical_bytes(observed) != runtime.canonical_bytes(expected_scientific_projection):
            _reject("frozen_projection", "focused repair changed the byte-frozen verifier projection "
                    "(verdicts, report prose or evidence artifacts)")
    if result["hypotheses_sha256"] != packet["hypotheses_sha256"]:
        _reject("hypotheses_hash", "verifier result hypotheses_sha256 must be the packet's "
                f"{packet['hypotheses_sha256']}", repairable=True)
    runtime.check_result_bindings(result, report, manifest_path)
    if not report_text.strip() or runtime.PLACEHOLDER.search(report_text):
        _reject("report_placeholder", "verifier report is empty or contains placeholders")
    missing = [heading for heading in REPORT_HEADINGS
               if not re.search(rf"(?im)^##\s+{re.escape(heading)}\s*$", report_text)]
    if missing:
        _reject("report_headings", "verifier report lacks sections: " + ", ".join(missing))
    if report_text.count(COMPLETION_MARKER) != 1:
        _reject("report_marker", f"verifier report must carry exactly one {COMPLETION_MARKER}")
    _check_hypothesis_blocks(report_text, scientific["verdicts"])
    runtime.check_output_hygiene(report_text, result, deposit_exemptions,
                                 participant_code_rule=participant_code_rule)
    known = runtime.source_hashes(snapshot_manifest)
    bindings = runtime.check_report_joins(report_text, known)
    try:
        manifest = runtime.check_evidence_manifest(
            manifest_path, role, snapshot_manifest, known, schema_version=contract.RESULT_VERSION,
        )
    except TypeError:  # an unhashable binding path; the shared auditor check is left as 2.6.3
        _reject("manifest_binding_unbound", "role evidence manifest contains an unbound source hash",
                repairable=True)
    # The shared check compares known.get(path) with sha, so a binding to a file absent from the
    # deposit with a null hash passed (None == None). Require an exact join of two strings.
    for binding in manifest.get("source_bindings") or []:
        if not (
            isinstance(binding, dict)
            and isinstance(binding.get("path"), str) and isinstance(binding.get("sha256"), str)
            and known.get(binding["path"]) == binding["sha256"]
        ):
            _reject("manifest_binding_unbound", "role evidence manifest contains an unbound source hash",
                    repairable=True)
    listed = runtime.check_output_tree(output, manifest)
    for row in scientific["verdicts"]:
        artifact = row["rerun"]["artifact"]
        if artifact is not None and _rerun_artifact_shape(artifact) not in listed:
            _reject("verdict_rerun", f"verdict {row['hypothesis_id']} rerun.artifact {artifact!r} is not "
                    "listed in the evidence manifest")
    try:
        contract.parse_result(result)
    except contract.VerifierContractError as exc:  # the downstream gates must accept what we admit
        _reject("result_header", f"verifier result does not parse downstream: {exc}")
    return runtime.ValidatedRoleOutput(
        role=role,
        report=report,
        evidence_manifest=manifest_path,
        result=result_path,
        report_sha256=runtime.sha256_file(report),
        evidence_manifest_sha256=runtime.sha256_file(manifest_path),
        result_sha256=runtime.sha256_file(result_path),
        scientific_fields=scientific,
        source_bindings=bindings,
    )


def verifier_output_contract(packet: dict[str, Any]) -> runtime.OutputContract:
    def projection(output: pathlib.Path, role: str, snapshot_manifest: dict[str, Any]) -> dict[str, Any]:
        try:
            return verifier_projection(output, role, snapshot_manifest, packet)
        except runtime.Phase1Error as exc:
            # run_role spends a fresh sample on a RunnerError from the repair path.
            raise run_phase1.RunnerError(f"verifier output cannot be frozen for repair: {exc}") from exc

    return runtime.OutputContract(
        name=contract.ROLE_CLASS,
        validate=functools.partial(validate_verifier_output, packet=packet),
        projection=projection,
    )


# ------------------------------------------------------------------------------ batch selection
@dataclass(frozen=True)
class Hypothesis:
    hypothesis_id: str
    finding_id: str
    claim: str
    claim_sha256: str
    source_location: dict[str, str]
    source_hashes: tuple[tuple[str, str], ...]
    dropped_source_hashes: tuple[tuple[str, str], ...]

    def packet_row(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "claim": self.claim,
            "claim_sha256": self.claim_sha256,
            "source_location": dict(self.source_location),
            "source_hashes": [{"path": path, "sha256": digest} for path, digest in self.source_hashes],
        }


@dataclass
class BatchPlan:
    root: pathlib.Path
    phase_root: pathlib.Path
    audit_id: str
    route: dict[str, Any]
    snapshot: pathlib.Path
    snapshot_manifest: dict[str, Any]
    hypotheses: list[Hypothesis]
    role: str
    packet: dict[str, Any]
    prompt: str
    packet_hash: str
    # 2.7.1: the probe bound this audit's snapshot froze (run_phase1.probe_bound), set by plan_batch.
    # The verifier reuses the auditors' probe, whose key hashes the bound, so it must be the audit's
    # own; None (a plan not built by plan_batch) is refused by run_phase1.probe_contract, never
    # replaced by the shipped bound.
    probe_bound: int | None = None
    cli: str | None = None
    probe_key: str | None = None
    input_hash: str | None = None
    replay: bool = False
    # The ledger already holds this batch's completed call but its receipt is missing: the runner
    # was killed between the terminal event and the receipt. Rebuild it; never dispatch again.
    recover_receipt: bool = False
    # Completed verifier records the shared contract does not admit (verify_record errors). They
    # are not verdicts, so they neither block nor confirm anything; they are reported, not hidden.
    inadmissible_records: tuple[str, ...] = ()
    # Snapshot entries that carry a write bit (mode-only drift, content verified), counted by
    # bind_inputs without any chmod. A real run re-freezes them after the pre-dispatch deadline; a
    # dry run only reports the count as would_refreeze.
    would_refreeze: int = 0

    @property
    def status_path(self) -> pathlib.Path:
        return self.phase_root / "verifier" / self.role / "status.json"

    @property
    def extra_deny_read(self) -> tuple[pathlib.Path, ...]:
        """The fixed read denials every verifier attempt carries (see deny_read for the rest)."""
        return tuple(self.root / name for name in EXTRA_DENY_READ)

    def deny_read(self, read_roots: tuple[pathlib.Path, ...]) -> tuple[pathlib.Path, ...]:
        """Every read denial of one attempt, computed when that attempt's settings are written.

        ``read_roots`` are the attempt's allowRead roots (source, packet, output). Sibling entries
        under phase1/roles and phase1/role_packets are denied unless they lead to one of them, so
        an attempt reads its own packet and output (and a repair its source attempt's frozen
        packet) but no other sample. Auditors never call this.
        """
        root = self.root
        denied: list[pathlib.Path] = list(self.extra_deny_read)
        denied += _sibling_denials(self.phase_root / "roles", read_roots, depth=3)
        # Another verifier batch's packet names the claims it tested. An auditor's packet holds
        # only the public brief, checklist and evidence indexes the verifier's own packet repeats,
        # so it is not denied (2.6.6: every denial costs sandbox profile size, see below).
        auditors = set(runtime.ROLE_SPECS)
        denied += [path for path in _sibling_denials(self.phase_root / "role_packets", read_roots, depth=3)
                   if not (path.name in auditors and path.parent.parent == self.phase_root / "role_packets")]
        late = [root / name for name in LATE_ROOT_ARTIFACTS]
        try:
            late += sorted(path for path in root.iterdir()
                           if path.name.startswith(LATE_ROOT_PREFIX) and path not in late)
        except OSError:
            pass
        if not _finalized(root):
            # Before finalization RUN_METADATA.md and run_metrics.json hold only call and token
            # telemetry (summarize_invocations.py); the rest of this list does not exist yet.
            late = [path for path in late if path.name not in TELEMETRY_ROOT_ARTIFACTS]
        denied += late
        outside = [root.parent / "release"]
        tree = tree_root_of_audit(root)
        if tree is not None:
            outside += tree_records_of_audit(tree, root, self.audit_id)
        # Never deny a path that holds the audit itself (and so every read root).
        denied += [path for path in outside if path != root and path not in root.parents]
        unique: list[pathlib.Path] = []
        for path in denied:
            if path not in unique:
                unique.append(path)
        # Only what can be read now. The CLI's macOS sandbox writes, for every denied path, an
        # unlink and a create rule for the path and each of its ancestors, each carrying a
        # 160-byte message, into a profile passed on the command line of EVERY Bash call. On the
        # tenth production audit (a 406-character, 16-level working directory) the 47 denials
        # overflowed ARG_MAX: every command failed with E2BIG, the verifier worked 45 minutes
        # without a shell and timed out. A path that does not exist, or an empty directory, holds
        # nothing to read when the attempt starts, and no other writer runs during a verifier call.
        return tuple(path for path in unique if _holds_something(path))

    def claim_hashes(self) -> list[str]:
        return [row.claim_sha256 for row in self.hypotheses]

    def receipt_hypotheses(self) -> list[dict[str, str]]:
        return [
            {"hypothesis_id": row.hypothesis_id, "claim_sha256": row.claim_sha256,
             "finding_id_at_run": row.finding_id}
            for row in self.hypotheses
        ]


def tree_root_of_audit(audit: pathlib.Path) -> pathlib.Path | None:
    """The discipline tree an audit sits in, or None -- the Python mirror of tree_layout.sh.

    i4r_root_of_audit: <root>/runs/<Journal>/<Paper>/audit (new) or <root>/<Journal>/<Paper>/audit
    (legacy); i4r_is_tree_root: the root must carry data/, reservations/ or "Completed reports/".
    A scratch parent, a filesystem root or a path too shallow for either layout is no tree.
    """
    parents = audit.parents
    if len(parents) >= 4 and parents[2].name == "runs":
        candidate = parents[3]
    elif len(parents) >= 3:
        candidate = parents[2]
    else:
        return None
    if candidate == pathlib.Path(candidate.anchor):
        return None
    if any((candidate / marker).is_dir() for marker in TREE_MARKERS):
        return candidate
    return None


def _finalized(root: pathlib.Path) -> bool:
    """Whether finalization has written its artefacts at the audit root (a reopened round)."""
    return any(os.path.lexists(root / name) for name in FINALIZATION_MARKERS)


def _holds_something(path: pathlib.Path) -> bool:
    """Whether a denial of this path protects anything: it exists and is not an empty directory."""
    if not os.path.lexists(path):
        return False
    if path.is_dir() and not path.is_symlink():
        try:
            return any(True for _ in path.iterdir())
        except OSError:
            return True  # unreadable here: keep the denial
    return True


def tree_records_of_audit(tree: pathlib.Path, audit: pathlib.Path, audit_id: str) -> list[pathlib.Path]:
    """This audit's own published records in its tree: what a reopened round must not read.

    Before 2.6.6 the whole of registry/, comments/, metadata/, old_runs/ and "Completed reports/"
    was denied; other papers' records say nothing about this audit, and the four trees cost four
    denials' worth of sandbox profile on every Bash call. An earlier audit of the same paper is
    still denied through old_runs/<Journal>/<Paper>.
    """
    journal, paper = audit.parent.parent.name, audit.parent.name
    records = [tree / "registry" / audit_id]
    for name in ("comments", "metadata", "old_runs", "Completed reports"):
        records.append(tree / name / journal / paper)
    return records


def _sibling_denials(
    base: pathlib.Path, read_roots: tuple[pathlib.Path, ...], *, depth: int,
) -> list[pathlib.Path]:
    """Every entry under ``base``, down to ``depth`` levels, that does not lead to a read root."""
    chains = []
    for read_root in read_roots:
        try:
            chains.append(pathlib.Path(read_root).relative_to(base).parts)
        except ValueError:
            continue
    denied: list[pathlib.Path] = []

    def walk(directory: pathlib.Path, level: int, active: list[tuple[str, ...]]) -> None:
        if directory.is_symlink() or not directory.is_dir():
            return
        for entry in sorted(directory.iterdir()):
            passing = [chain for chain in active if len(chain) > level and chain[level] == entry.name]
            if not passing:
                denied.append(entry)
            elif level + 1 < depth:
                walk(entry, level + 1, passing)

    walk(base, 0, chains)
    return denied


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate object key {key!r}")
        value[key] = item
    return value


def load_registry(root: pathlib.Path) -> list[dict[str, Any]]:
    path = root / "work" / "findings.jsonl"
    if path.is_symlink() or not path.is_file():
        raise VerifierRefusal(
            "the findings registry work/findings.jsonl is missing",
            "run the verifier after the coordinator has written the registry",
        )
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=_unique_object)
        except ValueError as exc:
            raise VerifierRefusal(
                f"work/findings.jsonl line {number} is not one JSON object: {exc}",
                "repair the registry line before verifying",
            ) from exc
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise VerifierRefusal(
                f"work/findings.jsonl line {number} has no finding id",
                "repair the registry line before verifying",
            )
        rows.append(row)
    return rows


def _source_location(row: dict[str, Any]) -> dict[str, str]:
    location = row.get("source_location")
    if (
        not isinstance(location, dict)
        or not isinstance(location.get("path"), str) or not location["path"].strip()
        or not isinstance(location.get("locator"), str) or not location["locator"].strip()
    ):
        raise VerifierRefusal(
            f"{row.get('id')} has no source_location {{path, locator}}",
            "give the row the scientific source locus of its claim, then re-run",
        )
    return {"path": location["path"], "locator": location["locator"]}


def select_hypotheses(
    rows: list[dict[str, Any]], requested: list[str], roles: list[str],
    snapshot_manifest: dict[str, Any], deposit_exemptions: Iterable[str] = frozenset(),
) -> list[Hypothesis]:
    """The batch's hypotheses in their deterministic order (by claim hash), or a refusal."""
    if not requested:
        raise VerifierRefusal("no finding ids were given", "pass --findings F13 F14 ...")
    duplicates_requested = sorted({fid for fid in requested if requested.count(fid) > 1})
    if duplicates_requested:
        raise VerifierRefusal(
            f"finding id(s) listed more than once: {', '.join(duplicates_requested)}",
            "list each finding once",
        )
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["id"] in by_id:
            raise VerifierRefusal(
                f"work/findings.jsonl holds finding id {row['id']} more than once",
                "repair the registry so every id is unique before verifying",
            )
        by_id[row["id"]] = row
    unknown = [fid for fid in requested if fid not in by_id]
    if unknown:
        raise VerifierRefusal(
            f"unknown finding id(s): {', '.join(unknown)}",
            "use the current ids in work/findings.jsonl",
        )
    known = runtime.source_hashes(snapshot_manifest)
    selected: list[tuple[str, dict[str, Any], str]] = []
    for fid in requested:
        row = by_id[fid]
        problem = contract.eligibility_problem(row, roles)
        if problem is not None:
            if "not INCLUDED" in problem:
                remedy = "the verifier tests only rows the ledger INCLUDES"
            elif "Phase-1 auditor" in problem:
                remedy = ("its Phase-1 auditor already supplies the family-A confirmation "
                          "(integrate_round binds it); do not send it to the verifier")
            else:
                remedy = "give the row its claim text, then re-run"
            raise VerifierRefusal(f"{fid} is not eligible for the verifier: {problem}", remedy)
        # The claim goes into the packet verbatim and the prompt says to test it as written, so a
        # code-shaped token in it would be echoed into REPORT.md and refused, non-repairably, at
        # admission (report_participant_code). Refuse before any provider call instead (2.6.5).
        hit = runtime.provider_safety.first_participant_code(str(row["claim"]), deposit_exemptions)
        if hit is not None:
            raise VerifierRefusal(
                f"{fid}'s claim text contains a participant-code-shaped token that is not a column "
                "name in the deposit's data files, which the verifier would have to repeat and "
                "admission refuses",
                "reword the claim without the code-shaped token (describe the file, variable or "
                "participant in words), then re-run",
            )
        selected.append((fid, row, contract.claim_sha256(row["claim"])))
    hashes = [digest for _fid, _row, digest in selected]
    shared = sorted({fid for fid, _row, digest in selected if hashes.count(digest) > 1})
    if shared:
        raise VerifierRefusal(
            f"rows {', '.join(shared)} share one claim text; a verdict binds to the claim text",
            "send one of them (the verdict then binds to every row with that exact claim)",
        )
    hypotheses: list[Hypothesis] = []
    for index, (fid, row, digest) in enumerate(sorted(selected, key=lambda item: item[2]), 1):
        location = _source_location(row)
        cited = (row.get("evidence") or {}).get("source_hashes") if isinstance(row.get("evidence"), dict) else None
        kept: list[tuple[str, str]] = []
        dropped: list[tuple[str, str]] = []
        for entry in cited if isinstance(cited, list) else []:
            if not isinstance(entry, dict):
                continue
            path, value = entry.get("path"), entry.get("sha256")
            if not isinstance(path, str) or not isinstance(value, str):
                continue
            if path.startswith(SOURCE_PREFIXES) and known.get(path) == value:
                if (path, value) not in kept:
                    kept.append((path, value))
            else:
                dropped.append((path, value))
        if not kept:
            raise VerifierRefusal(
                f"{fid} has no admissible paper/ or pkg/ source: no evidence.source_hashes entry is a "
                "paper/ or pkg/ file with the Phase-1 snapshot's sha256",
                "cite the deposited file the claim rests on with its snapshot hash (see "
                f"phase1/snapshots/{snapshot_manifest['snapshot_sha256']}/snapshot_manifest.json) "
                "in evidence.source_hashes, then re-run",
            )
        hypotheses.append(Hypothesis(
            hypothesis_id=f"H{index}", finding_id=fid, claim=row["claim"], claim_sha256=digest,
            source_location=location, source_hashes=tuple(kept),
            dropped_source_hashes=tuple(dropped),
        ))
    return hypotheses


def make_verifier_packet(
    role: str, snapshot_manifest: dict[str, Any], route: dict[str, Any],
    hypotheses: list[Hypothesis],
) -> dict[str, Any]:
    """The blind packet: claims, their hashes, loci and snapshot-bound sources -- nothing else.

    Withheld on purpose: finding ids (the H-ids are opaque; the mapping stays in the runner's
    receipt), status, confidence, impact, tier, verifications, provenance, ledger disposition,
    approvals, expected/observed/rerun_command/tolerance, benign explanations, remedy,
    changes_conclusion, verification snippet, settled, dependencies, and the auditors'
    CHECKLIST.md/BRIEF_TEMPLATE.md scope.
    """
    evidence_by_name = {row["path"]: row for row in snapshot_manifest["common_evidence"]["artifacts"]}
    rows = [hypothesis.packet_row() for hypothesis in hypotheses]
    return {
        "schema_version": contract.PACKET_VERSION,
        "role": role,
        "source_snapshot_sha256": snapshot_manifest["snapshot_sha256"],
        "source_manifest": snapshot_manifest["source_files"],
        "assigned_evidence": [evidence_by_name[name] for name in ASSIGNED_EVIDENCE if name in evidence_by_name],
        "raw_access": {"paper": "raw/paper", "package": "raw/pkg", "read_only": True},
        "model": {
            "requested_model": route["requested_model"],
            "spawn_alias": route["spawn_alias"],
            "reasoning_effort": route["reasoning_effort"],
        },
        "hypotheses": rows,
        "hypotheses_sha256": runtime.sha256_bytes(runtime.canonical_bytes(rows)),
        "output_contract": {
            "report": "REPORT.md",
            "result": "result.json",
            "evidence_manifest": "evidence/manifest.json",
            "result_schema_version": contract.RESULT_VERSION,
            "completion_marker": COMPLETION_MARKER,
        },
    }


def pre_265_prompt(prompt: str) -> str | None:
    """The verifier prompt as 2.6.4 and earlier built it, or None.

    2.6.5 changed the prompt only by inserting the participant-code rule (RULE_STATEMENT and one
    space); removing it gives the exact earlier prompt, so an earlier record's frozen packet hash
    can still be checked.
    """
    statement = runtime.provider_safety.RULE_STATEMENT + " "
    return prompt.replace(statement, "", 1) if prompt.count(statement) == 1 else None


def packet_contract_hash(packet: dict[str, Any], prompt: str) -> str:
    return runtime.sha256_bytes(runtime.canonical_bytes({
        "packet_layout_version": PACKET_LAYOUT_VERSION,
        "packet": packet,
        "prompt_sha256": runtime.sha256_bytes(prompt.encode("utf-8")),
    }))


def materialize_verifier_packet(
    plan: BatchPlan, deadline: runtime.MonotonicDeadline | None = None,
) -> pathlib.Path:
    """Freeze the packet under phase1/role_packets/<snapshot>/<role>/<packet_hash>/ and verify it."""
    phase_root, snapshot, role = plan.phase_root, plan.snapshot, plan.role
    base = phase_root / "role_packets" / plan.snapshot_manifest["snapshot_sha256"]
    runtime.validate_destination(phase_root, base, "immutable role packet store")
    base.mkdir(parents=True, exist_ok=True)
    target = base / role / plan.packet_hash
    runtime.validate_destination(phase_root, target, f"{role} immutable packet")
    if not target.exists():
        (base / role).mkdir(parents=True, exist_ok=True)
        temporary = pathlib.Path(tempfile.mkdtemp(prefix=f"{role}-", dir=base / role))
        try:
            runtime.atomic_write(temporary / "ROLE_PACKET.json", runtime.pretty_bytes(plan.packet))
            for row in plan.packet["assigned_evidence"]:
                runtime.check_deadline(deadline, f"copying {role} evidence indexes")
                source = runtime.beneath(snapshot / "evidence", snapshot / "evidence" / row["path"],
                                         f"{role} assigned evidence")
                runtime.regular_file(source, f"{role} assigned evidence")
                if source.stat().st_size != row["bytes"] or runtime.sha256_file(source) != row["sha256"]:
                    raise run_phase1.RunnerError(f"assigned evidence changed before packet creation: {row['path']}")
                destination = runtime.beneath(temporary, temporary / "evidence" / row["path"],
                                              f"{role} packet evidence")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                if runtime.sha256_file(destination) != row["sha256"]:
                    raise run_phase1.RunnerError(f"assigned evidence copy changed bytes: {row['path']}")
            files = sorted(path for path in temporary.rglob("*") if path.is_file() and not path.is_symlink())
            runtime.atomic_write(temporary / "packet_manifest.json", runtime.pretty_bytes({
                "schema_version": contract.PACKET_VERSION,
                "role": role,
                "packet_sha256": plan.packet_hash,
                "files": [
                    {"path": path.relative_to(temporary).as_posix(), "sha256": runtime.sha256_file(path),
                     "bytes": path.stat().st_size}
                    for path in files
                ],
            }))
            runtime.freeze_and_publish(temporary, target, deadline)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
    if verify_frozen_packet(target, role, plan.packet_hash, deadline) != plan.packet:
        raise run_phase1.RunnerError("immutable verifier packet does not hold the planned packet")
    return target


def verify_frozen_packet(
    target: pathlib.Path, role: str, packet_hash: str,
    deadline: runtime.MonotonicDeadline | None = None,
) -> dict[str, Any]:
    """Verify a frozen packet directory against its packet_manifest.json; return ROLE_PACKET.json.

    The manifest must name this role and packet hash, bind every file by size and hash, and close
    over the directory (no symlink, no unlisted file). Used before dispatch and on re-admission.
    """
    manifest = runtime.strict_json(runtime.regular_file(target / "packet_manifest.json", "verifier packet manifest"))
    if (
        manifest.get("schema_version") != contract.PACKET_VERSION
        or manifest.get("packet_sha256") != packet_hash
        or manifest.get("role") != role
        or not isinstance(manifest.get("files"), list)
    ):
        raise run_phase1.RunnerError(f"immutable verifier packet collision for {role}")
    bound: set[str] = set()
    for row in manifest["files"]:
        runtime.check_deadline(deadline, f"verifying {role} packet")
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise run_phase1.RunnerError(f"invalid immutable verifier packet row for {role}")
        path = runtime.beneath(target, target / row["path"], "verifier packet file")
        runtime.regular_file(path, "verifier packet file")
        relative = path.relative_to(target.resolve()).as_posix()
        if relative in bound:
            raise run_phase1.RunnerError(f"duplicate immutable verifier packet path: {relative}")
        bound.add(relative)
        if path.stat().st_size != row["bytes"] or runtime.sha256_file(path) != row["sha256"]:
            raise run_phase1.RunnerError(f"immutable verifier packet changed: {row['path']}")
    actual: set[str] = set()
    for path in target.rglob("*"):
        if path.is_symlink():
            raise run_phase1.RunnerError(f"immutable verifier packet contains a symlink: {path.name}")
        if path.is_file():
            actual.add(path.relative_to(target).as_posix())
    if actual != bound | {"packet_manifest.json"}:
        raise run_phase1.RunnerError("immutable verifier packet manifest does not close over its files")
    return runtime.strict_json(target / "ROLE_PACKET.json")


# ------------------------------------------------------------------------------- the refusal gates
def _quorum(phase_root: pathlib.Path, audit_id: str) -> dict[str, Any]:
    path = phase_root / "quorum.json"
    remedy = "complete Phase 1 first: python3 scripts/run_phase1.py <audit-dir>"
    if path.is_symlink() or not path.is_file():
        raise VerifierRefusal("phase1/quorum.json is missing, so Phase 1 has no complete quorum", remedy)
    try:
        quorum = runtime.strict_json(path)
    except runtime.Phase1Error as exc:
        raise VerifierRefusal(f"phase1/quorum.json is unreadable: {exc}", remedy) from exc
    if quorum.get("status") != "quorum_complete":
        raise VerifierRefusal(
            f"phase1/quorum.json status is {quorum.get('status')!r}, not quorum_complete", remedy)
    body = dict(quorum)
    digest = body.pop("quorum_sha256", None)
    if digest != runtime.sha256_bytes(runtime.canonical_bytes(body)):
        raise VerifierRefusal(
            "phase1/quorum.json does not hash to its own quorum_sha256",
            "restore phase1/quorum.json exactly as Phase 1 wrote it; never edit it",
        )
    if quorum.get("audit_id") != audit_id:
        raise VerifierRefusal(
            f"phase1/quorum.json binds audit {quorum.get('audit_id')!r}, not {audit_id!r}",
            "run the verifier on the audit whose Phase 1 produced this quorum",
        )
    if not isinstance(quorum.get("source_snapshot_sha256"), str):
        raise VerifierRefusal("phase1/quorum.json names no source snapshot", remedy)
    return quorum


def _snapshot(phase_root: pathlib.Path, quorum: dict[str, Any]) -> tuple[pathlib.Path, dict[str, Any]]:
    digest = quorum["source_snapshot_sha256"]
    snapshot = phase_root / "snapshots" / digest
    remedy = ("the snapshot bound by phase1/quorum.json must verify as frozen; restore it (it is "
              "read-only and content-addressed) or re-run Phase 1 -- the verifier never rebuilds one")
    try:
        runtime.validate_destination(phase_root, snapshot, "Phase-1 snapshot")
        manifest = runtime.strict_json(runtime.regular_file(snapshot / "snapshot_manifest.json", "snapshot manifest"))
    except runtime.Phase1Error as exc:
        raise VerifierRefusal(f"verified snapshot mismatch: the quorum's snapshot is unreadable: {exc}", remedy) from exc
    body = dict(manifest)
    stated = body.pop("snapshot_sha256", None)
    if stated != digest or runtime.sha256_bytes(runtime.canonical_bytes(body)) != digest:
        raise VerifierRefusal(
            f"verified snapshot mismatch: phase1/snapshots/{digest} does not hash to the quorum's snapshot",
            remedy)
    return snapshot, manifest


def _refuse_if_stranded(root: pathlib.Path, phase_root: pathlib.Path) -> None:
    try:
        run_phase1.guard_stranded_attempts(root, phase_root)
    except run_phase1.RunnerError as exc:
        raise VerifierRefusal(
            str(exc),
            "wait for the live call, or clear a killed one with "
            "python3 scripts/run_phase1.py <audit-dir> --recover-stranded",
        ) from exc


def plan_batch(
    root: pathlib.Path, phase_root: pathlib.Path, requested: list[str], args: argparse.Namespace,
) -> BatchPlan:
    """Every provider-free refusal gate that needs no subprocess, in order. Writes nothing."""
    if not run_phase1.metadata_21(root) or not (root / lifecycle_journal.JOURNAL_NAME).is_file():
        raise VerifierRefusal(
            "the audit is not a metadata-2.1 audit (run.json schema_version is not 2.1.0 or "
            "run.events.jsonl is missing); a verifier call is admitted only through the 2.1 "
            "lifecycle journal",
            "migrate the audit with python3 scripts/migrate_metadata_2_1.py first; a pre-2.1 "
            "audit cannot admit a verifier confirmation",
        )
    try:
        policy, configured = run_phase1.load_policy(root)
        probe_bound = run_phase1.probe_bound(policy)
        audit_timeout = run_phase1.phase1_timeout(policy)
        run = run_phase1.load_run(root)
        audit_policy = contract.load_snapshot_policy(root)
        roles = contract.required_roles(audit_policy)
    except (run_phase1.RunnerError, ValueError) as exc:
        raise VerifierRefusal(
            f"the audit does not bind the packaged Phase-1 contract: {exc}",
            "run the verifier only on a canonical audit initialised by scripts/init_run.sh",
        ) from exc
    # 2.7.2: the audit's own frozen timeout (run_phase1.phase1_timeout); --timeout-seconds defaults to it.
    if args.timeout_seconds is None:
        args.timeout_seconds = audit_timeout
    if (
        args.timeout_seconds != audit_timeout
        or decimal.Decimal(args.max_budget_usd)
        != decimal.Decimal(str(policy["phase_1"]["runtime"]["max_provider_budget_usd"]))
    ):
        raise VerifierRefusal(
            f"a verifier call runs under the policy-bound {audit_timeout}-second/$15 Phase-1 limits; "
            "timeout or budget overrides are non-canonical",
            "omit --timeout-seconds and --max-budget-usd",
        )
    route = contract.verifier_route(audit_policy)
    if route is None:
        raise VerifierRefusal(
            "the audit policy names no verifier route and its Phase-1 auditors share no single route",
            "declare models.post_fan_out_verifier in the policy of a new audit; a frozen snapshot "
            "is never edited",
        )
    if route not in PACKAGED_ROUTES:
        raise VerifierRefusal(
            f"the verifier route {route} is not a packaged Claude route ("
            + "; ".join(claude_routes.describe(candidate) for candidate in PACKAGED_ROUTES) + ")",
            "the packaged runtime dispatches and records only the routes listed (Sonnet 5 at max; Sonnet 5.5 "
            "at max or xhigh); a frozen snapshot is never edited",
        )
    # load_policy has bound all five auditors to one packaged route.
    auditor_route = claude_routes.project(next(iter(configured.values())))
    if route != auditor_route:
        raise VerifierRefusal(
            f"the verifier route {route} is not the Phase-1 auditors' route {auditor_route}",
            "the verifier is family A on the auditors' own route and reuses their capability probe; "
            "declare models.post_fan_out_verifier equal to the auditors' route in the policy of a new "
            "audit -- a frozen snapshot is never edited",
        )
    quorum = _quorum(phase_root, run["audit_id"])
    snapshot, snapshot_manifest = _snapshot(phase_root, quorum)
    ledger = run_phase1.read_ledger(root)
    families = contract.auditor_families(ledger)
    if families != {"claude"}:
        raise VerifierRefusal(
            f"the completed Phase-1 auditor family set is {sorted(families)}, not exactly ['claude']",
            "the verifier is family A and must match the Phase-1 auditors; complete Phase 1 first",
        )
    hypotheses = select_hypotheses(load_registry(root), requested, roles, snapshot_manifest,
                                   runtime.snapshot_deposit_exemptions(snapshot))
    role = contract.batch_role(row.claim_sha256 for row in hypotheses)
    packet = make_verifier_packet(role, snapshot_manifest, route, hypotheses)
    prompt = build_verifier_prompt(role, run["audit_id"], packet)
    _refuse_if_stranded(root, phase_root)
    return BatchPlan(
        root=root, phase_root=phase_root, audit_id=run["audit_id"], route=route,
        snapshot=snapshot, snapshot_manifest=snapshot_manifest, hypotheses=hypotheses,
        role=role, packet=packet, prompt=prompt, packet_hash=packet_contract_hash(packet, prompt),
        probe_bound=probe_bound,
    )


def bind_inputs(plan: BatchPlan, claude: str, deadline: runtime.MonotonicDeadline) -> None:
    """Verify the snapshot, key the probe and the input hash, and decide replay vs dispatch.

    Still provider-free, journal-free and chmod-free; the only subprocess is `claude --version`.
    It runs inside the SIGALRM pre-dispatch deadline, so the snapshot's content and inventory are
    verified strictly but its modes are only counted (plan.would_refreeze): mode-only drift on the
    days-old Phase-1 snapshot is re-frozen by the caller once the deadline is disarmed, and never
    under --dry-run. A content change is still a refusal, with no journal event.
    """
    plan.cli = run_phase1.cli_version(claude, deadline)
    try:
        plan.would_refreeze = len(runtime.verify_snapshot(
            plan.snapshot, plan.snapshot_manifest, deadline, lenient_modes=True))
    except runtime.Phase1Error as exc:
        if "deadline exceeded" in str(exc):
            raise
        raise VerifierRefusal(
            f"verified snapshot mismatch: {exc}",
            "the snapshot bound by phase1/quorum.json must verify as frozen; restore it or re-run "
            "Phase 1 -- the verifier never rebuilds a snapshot",
        ) from exc
    plan.probe_key, _components = run_phase1.probe_key(plan.cli, plan.route, plan.root,
                                                        bound=plan.probe_bound)
    plan.input_hash = runtime.role_input_hash(plan.packet, plan.prompt, plan.probe_key)
    if run_phase1.completion_path(plan.phase_root, plan.role, plan.input_hash).exists():
        plan.replay = True
        return
    ledger = run_phase1.read_ledger(plan.root)
    if any(
        row.get("role") == plan.role and row.get("role_class") == contract.ROLE_CLASS
        and row.get("status") == "completed" and row.get("provider_call") is True
        and row.get("input_artifact_sha256") == plan.input_hash
        for row in ledger
    ):
        # This batch's own call is journaled as completed but its receipt is missing: the runner
        # was killed (or the receipt write failed) between the terminal event and the receipt.
        # Judged below it is an inadmissible record and every batch -- this one included -- would
        # be refused for good. Rebuild the receipt from the retained response instead (no
        # provider call, no budget); the rebuilt record is re-admitted in full before it counts.
        plan.recover_receipt = True
        return
    verdicts, errors = contract.collect_verdicts(plan.root, ledger)
    plan.inadmissible_records = tuple(errors)
    if errors:
        # Fail closed. An inadmissible record could be a tampered or broken REFUTED verdict, and
        # dropping it silently would let its claim be sent again -- verdict shopping by accident.
        raise VerifierRefusal(
            "the ledger holds a completed verifier record that is not admissible: " + "; ".join(errors),
            "if the runner was killed after journaling a call (its receipt under "
            "phase1/completions/ is missing), re-run the same run_verifier.py command -- that "
            "batch's own --findings -- to rebuild the lost receipt with no provider call. "
            "Otherwise restore the record's promoted files (work/verifier/..., "
            "phase1/completions/...) to their recorded hashes; only for files that were genuinely "
            "tampered with, mark the row superseded with python3 scripts/record_invocation.py "
            "--correct <audit-dir> <invocation-id> <correction.json>, where correction.json is "
            "{\"set\": {\"status\": \"legacy_record\"}, \"reason\": \"...\"} -- never for a lost "
            "receipt, which leaves that batch unrecoverable. integrate_round.py refuses the same state",
        )
    for row in plan.hypotheses:
        prior = verdicts.get(row.claim_sha256)
        if prior:
            summary = ", ".join(f"{verdict} from {invocation}" for invocation, verdict in prior)
            raise VerifierRefusal(
                f"{row.finding_id}'s claim text already has an admitted verifier verdict ({summary})",
                "one verdict per claim text: a claim is never re-verified. Only an edited claim "
                "(a changed finding) can be sent again, and the old verdict then no longer applies",
            )
    limit = lifecycle_journal.max_attempts_per_role()
    if run_phase1.role_attempt_count(plan.root, plan.role) >= limit:
        raise VerifierRefusal(
            f"verifier batch {plan.role} has exhausted its {limit} accepted inference starts",
            "raise the ceiling with I4R_MAX_ATTEMPTS=<1..20> (the same knob as Phase 1), then re-run",
        )
    if run_phase1.role_preinference_request_count(plan.root, plan.role) >= 2:
        raise VerifierRefusal(
            f"verifier batch {plan.role} exhausted two pre-inference provider-request retries",
            "the provider never accepted the request twice in a row; check credentials and quota",
        )


# ------------------------------------------------------------------ promotion, receipt and replay
@dataclass(frozen=True)
class VerifierCompletion:
    role: str
    status: str
    input_sha256: str
    invocation_id: str
    attempt: int | None
    attempt_kind: str
    result: str
    result_sha256: str
    report: str
    report_sha256: str
    evidence: str
    evidence_manifest_sha256: str
    hypotheses: tuple[dict[str, str], ...]
    reused: bool
    source_invocation_id: str | None = None


def _receipt(
    plan: BatchPlan, *, invocation_id: str, attempt: int | None, attempt_kind: str,
    source_invocation_id: str | None, validated: runtime.ValidatedRoleOutput,
) -> dict[str, Any]:
    base = contract.output_dir(plan.role, plan.input_hash)
    return {
        "schema_version": contract.COMPLETION_VERSION,
        "status": "completed",
        "role": plan.role,
        "input_sha256": plan.input_hash,
        "source_snapshot_sha256": plan.snapshot_manifest["snapshot_sha256"],
        "packet_sha256": plan.packet_hash,
        "invocation_id": invocation_id,
        "attempt": attempt,
        "attempt_kind": attempt_kind,
        "source_invocation_id": source_invocation_id,
        "result": f"{base}/result.json",
        "result_sha256": validated.result_sha256,
        "report": f"{base}/REPORT.md",
        "report_sha256": validated.report_sha256,
        "evidence": f"{base}/evidence/manifest.json",
        "evidence_manifest_sha256": validated.evidence_manifest_sha256,
        "hypotheses": plan.receipt_hypotheses(),
        "source_bindings": [{"path": path, "sha256": digest} for path, digest in validated.source_bindings],
    }


def _completion(receipt: dict[str, Any], *, reused: bool) -> VerifierCompletion:
    return VerifierCompletion(
        role=receipt["role"], status="completed", input_sha256=receipt["input_sha256"],
        invocation_id=receipt["invocation_id"], attempt=receipt.get("attempt"),
        attempt_kind=receipt["attempt_kind"], result=receipt["result"],
        result_sha256=receipt["result_sha256"], report=receipt["report"],
        report_sha256=receipt["report_sha256"], evidence=receipt["evidence"],
        evidence_manifest_sha256=receipt["evidence_manifest_sha256"],
        hypotheses=tuple(receipt["hypotheses"]), reused=reused,
        source_invocation_id=receipt.get("source_invocation_id"),
    )


def validate_verifier_receipt(
    plan: BatchPlan, root: pathlib.Path, path: pathlib.Path, input_hash: str,
) -> VerifierCompletion:
    """Replay a completed batch from its receipt: re-admit the record, then journal a cache replay."""
    receipt = runtime.strict_json(runtime.regular_file(path, "verifier completion receipt"))
    if (
        set(receipt) != RECEIPT_KEYS
        or receipt.get("schema_version") != contract.COMPLETION_VERSION
        or receipt.get("status") != "completed"
        or receipt.get("input_sha256") != input_hash
        or receipt.get("role") != plan.role
        or receipt.get("packet_sha256") != plan.packet_hash
        or receipt.get("source_snapshot_sha256") != plan.snapshot_manifest["snapshot_sha256"]
        or [row.get("claim_sha256") for row in receipt.get("hypotheses") or []] != plan.claim_hashes()
    ):
        raise run_phase1.RunnerError(f"invalid verifier completion receipt: {path}")
    ledger = run_phase1.read_ledger(root)
    matches = [row for row in ledger if row.get("invocation_id") == receipt["invocation_id"]]
    if len(matches) != 1 or matches[0].get("status") != "completed" or matches[0].get("provider_call") is not True:
        raise run_phase1.RunnerError(f"verifier completion lacks one canonical inference record: {plan.role}")
    by_id = {str(row.get("invocation_id")): row for row in ledger}
    try:
        contract.verify_record(root, matches[0], by_id, contract.auditor_families(ledger))
    except (contract.VerifierContractError, OSError, ValueError) as exc:
        raise run_phase1.RunnerError(f"completed verifier record is not admissible: {exc}") from exc
    run_phase1.record_cache_replay(
        root,
        role=plan.role,
        role_class=contract.ROLE_CLASS,
        round_number=contract.ROUND,
        source_invocation_id=receipt["invocation_id"],
        input_hash=input_hash,
        output_artifact=receipt["result"],
        output_sha256=receipt["result_sha256"],
        cache_usage="read",
        route=plan.route,
    )
    return _completion(receipt, reused=True)


# ------------------------------------------------------------------------------- re-admission
RECEIPT_HYPOTHESIS_KEYS = frozenset({"hypothesis_id", "claim_sha256", "finding_id_at_run"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def readmit_record(audit: pathlib.Path, source: dict[str, Any], receipt: dict[str, Any]) -> None:
    """Re-admit one promoted verifier call exactly as this runner admitted it, or raise.

    verifier_contract.verify_record calls this last, after its own record, receipt, result and
    hypothesis checks, so every gate that counts a verifier verdict (collect_verdicts: the
    runner's refusal, integrate_round, make_settled, make_diff_packet, finalize) re-proves it.
    Four files that hash to what a receipt says prove nothing by themselves -- anyone can write
    all four, and the Neudorfer make_record.py route did. This requires what only the runner
    produces: its exact receipt (RECEIPT_KEYS) bound to this call; the snapshot phase1/quorum.json
    binds (the manifest self-checked, the raw deposit not re-hashed); the frozen packet under
    phase1/role_packets/<snapshot>/<role>/<packet_sha256>/, closed by its manifest and hashing,
    with the prompt this runner builds, to the receipt's packet_sha256; the receipt's hypotheses
    exactly the packet's; the promoted tree passing validate_verifier_output (evidence joins,
    rerun artifacts, headings, blocks, the closed file set) with the receipt's hashes and source
    bindings; and the call's retained model-identity evidence, when it names one.

    ``source`` is the resolved provider call (a ledger row), ``receipt`` the parsed completion
    receipt. Raises verifier_contract.VerifierContractError on any failure.
    """
    try:
        _readmit(pathlib.Path(audit), source, receipt)
    except Exception as exc:  # fail closed on anything, whatever module raised it
        raise contract.VerifierContractError(
            f"verifier call {source.get('invocation_id') if isinstance(source, dict) else None} "
            f"is not the runner's admitted output: {exc}") from exc


# Files an operating system writes into any folder a person opens (Finder, Explorer, a synced
# client). They are never model output, and a promoted tree is browsed by people: one .DS_Store
# would otherwise make a sound record inadmissible and fail every consumer closed.
OS_METADATA_NAMES = (".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r")


def _is_os_metadata(name: str) -> bool:
    return name in OS_METADATA_NAMES or name.startswith("._")


def _validate_promoted_tree(tree: pathlib.Path, role: str, manifest: dict[str, Any],
                            packet: dict[str, Any],
                            deposit_exemptions: Iterable[str] = frozenset(),
                            participant_code_rule: bool = True) -> runtime.ValidatedRoleOutput:
    """validate_verifier_output on the promoted tree, ignoring operating-system metadata files.

    Only the exact names above are ignored; any other extra file still fails the closed-set rule.
    Symlinks are copied as symlinks, so the symlink refusal still fires on the clean copy.
    """
    if not any(_is_os_metadata(path.name) for path in tree.rglob("*")):
        return validate_verifier_output(tree, role, manifest, packet,
                                        deposit_exemptions=deposit_exemptions,
                                        participant_code_rule=participant_code_rule)
    with tempfile.TemporaryDirectory(prefix="i4r-readmit-") as tmp:
        clean = pathlib.Path(tmp) / "tree"
        shutil.copytree(tree, clean, symlinks=True,
                        ignore=lambda _dir, names: [n for n in names if _is_os_metadata(n)])
        return validate_verifier_output(clean, role, manifest, packet,
                                        deposit_exemptions=deposit_exemptions,
                                        participant_code_rule=participant_code_rule)


def _readmit(audit: pathlib.Path, source: dict[str, Any], receipt: dict[str, Any]) -> None:
    fail = contract.VerifierContractError
    if not isinstance(source, dict):
        raise fail("the invocation record is not an object")
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        found = set(receipt) if isinstance(receipt, dict) else set()
        raise fail("the completion receipt is not the runner's: missing "
                   f"{sorted(RECEIPT_KEYS - found)} extra {sorted(found - RECEIPT_KEYS)}")
    role, input_hash = source.get("role"), source.get("input_artifact_sha256")
    if not isinstance(role, str) or not contract.ROLE_PATTERN.match(role):
        raise fail(f"role {role!r} is not verifier_<12 hex>")
    if not isinstance(input_hash, str) or not _HEX64.match(input_hash):
        raise fail("the call names no input hash")
    base = contract.output_dir(role, input_hash)
    expected = {
        "schema_version": contract.COMPLETION_VERSION, "status": "completed", "role": role,
        "input_sha256": input_hash, "invocation_id": source.get("invocation_id"),
        "attempt": source.get("attempt"), "attempt_kind": source.get("attempt_kind"),
        "source_invocation_id": source.get("source_invocation_id"),
        "result": f"{base}/result.json", "result_sha256": source.get("output_sha256"),
        "report": f"{base}/REPORT.md", "evidence": f"{base}/evidence/manifest.json",
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise fail(f"receipt {key} {receipt.get(key)!r} is not the call's {value!r}")
    if receipt["attempt_kind"] not in ("role_inference", "output_repair"):
        raise fail(f"receipt attempt_kind {receipt['attempt_kind']!r} is not a verifier inference")
    # The snapshot the quorum binds. Its manifest must hash to itself; the raw deposit is not
    # re-hashed here (the runner verified it before and after the call).
    run = runtime.strict_json(runtime.regular_file(audit / "run.json", "run.json"))
    audit_id = run.get("audit_id")
    if not isinstance(audit_id, str) or not audit_id:
        raise fail("run.json names no audit_id")
    phase_root = audit / "phase1"
    _snapshot_dir, manifest = _snapshot(phase_root, _quorum(phase_root, audit_id))
    snapshot_sha = manifest["snapshot_sha256"]
    if receipt["source_snapshot_sha256"] != snapshot_sha:
        raise fail("the receipt names another source snapshot than phase1/quorum.json binds")
    packet_hash = receipt["packet_sha256"]
    if not isinstance(packet_hash, str) or not _HEX64.match(packet_hash):
        raise fail("the receipt names no packet hash")
    target = phase_root / "role_packets" / snapshot_sha / role / packet_hash
    runtime.validate_destination(phase_root, target, "frozen verifier packet")
    if not target.is_dir():
        raise fail(f"no frozen packet at phase1/role_packets/{snapshot_sha}/{role}/{packet_hash}")
    packet = verify_frozen_packet(target, role, packet_hash)
    if packet.get("role") != role or packet.get("source_snapshot_sha256") != snapshot_sha:
        raise fail("the frozen packet names another role or snapshot")
    # A record made before 2.6.5 was frozen with the prompt that version built: the same prompt
    # without the participant-code rule sentence. It is re-admitted under that contract, with the
    # rule not applied to its output, because the verifier was never told it; any other prompt
    # is still refused.
    prompt = build_verifier_prompt(role, audit_id, packet)
    legacy = pre_265_prompt(prompt)
    if packet_contract_hash(packet, prompt) == packet_hash:
        participant_code_rule = True
    elif legacy is not None and packet_contract_hash(packet, legacy) == packet_hash:
        participant_code_rule = False
    else:
        raise fail("the frozen packet and the runner's prompt do not hash to the receipt's packet_sha256")
    hypotheses = packet.get("hypotheses")
    if not isinstance(hypotheses, list) or not hypotheses or not all(isinstance(h, dict) for h in hypotheses):
        raise fail("the frozen packet has no hypotheses")
    if contract.batch_role(h.get("claim_sha256") for h in hypotheses) != role:
        raise fail("the frozen packet's hypotheses do not name this batch role")
    rows = receipt["hypotheses"]
    if (
        not isinstance(rows, list)
        or not all(isinstance(row, dict) and set(row) == RECEIPT_HYPOTHESIS_KEYS
                   and isinstance(row.get("finding_id_at_run"), str) for row in rows)
        or [(row["hypothesis_id"], row["claim_sha256"]) for row in rows]
        != [(h.get("hypothesis_id"), h.get("claim_sha256")) for h in hypotheses]
    ):
        raise fail("the receipt's hypotheses are not the frozen packet's")
    validated = _validate_promoted_tree(audit / base, role, manifest, packet,
                                        runtime.snapshot_deposit_exemptions(_snapshot_dir),
                                        participant_code_rule=participant_code_rule)
    for key, value in (("result_sha256", validated.result_sha256),
                       ("report_sha256", validated.report_sha256),
                       ("evidence_manifest_sha256", validated.evidence_manifest_sha256)):
        if receipt[key] != value:
            raise fail(f"receipt {key} does not hash the promoted tree")
    if receipt["source_bindings"] != [{"path": path, "sha256": digest}
                                      for path, digest in validated.source_bindings]:
        raise fail("receipt source_bindings are not the promoted report's source joins")
    evidence = source.get("model_identity_evidence")
    if isinstance(evidence, dict) and evidence.get("artifact") is not None \
            and evidence.get("artifact_sha256") is not None:
        artifact = contract._regular(audit, evidence["artifact"], "model identity evidence")
        if contract._sha256(artifact) != evidence["artifact_sha256"]:
            raise fail(f"model identity evidence changed after it was recorded: {evidence['artifact']}")


def _write_attempt_completed(execution: run_phase1.ProviderExecution, attempt_kind: str,
                             input_hash: str, result_sha256: str) -> None:
    runtime.atomic_write(execution.attempt_dir / "status.json", runtime.pretty_bytes({
        "schema_version": "i4r-phase1-attempt-1",
        "state": "completed",
        "invocation_id": execution.reservation["invocation_id"],
        "attempt": execution.attempt,
        "attempt_kind": attempt_kind,
        "provider_request_status": "accepted",
        "inference_start_status": "started",
        "input_artifact_sha256": input_hash,
        "output_sha256": result_sha256,
        "ended_at": run_phase1.iso(execution.ended_at),
    }))


def promote_verifier(
    plan: BatchPlan,
    root: pathlib.Path,
    phase_root: pathlib.Path,
    execution: run_phase1.ProviderExecution,
    validated: runtime.ValidatedRoleOutput,
    role: str,
    input_hash: str,
    prompt_hash: str,
    attempt_kind: str,
    source_invocation_id: str | None,
) -> VerifierCompletion:
    """Promote the whole validated tree to work/verifier/<role>/<input_hash>/, then record."""
    target_rel = contract.output_dir(role, input_hash)
    target = root / target_rel
    receipt_path = run_phase1.completion_path(phase_root, role, input_hash)
    lock_path = phase_root / "promotion.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if receipt_path.exists():
                return validate_verifier_receipt(plan, root, receipt_path, input_hash)
            attempt_input = runtime.strict_json(execution.attempt_dir / "input.json")
            if attempt_input.get("input_sha256") != input_hash:
                raise run_phase1.RunnerError(
                    f"verifier attempt is bound to {attempt_input.get('input_sha256')}, not the batch "
                    f"input {input_hash}")
            runtime.validate_destination(root, target, "verifier promotion target")
            transaction = phase_root / "transactions" / execution.reservation["invocation_id"]
            transaction.mkdir(parents=True, exist_ok=False)
            staged = transaction / "output"
            shutil.copytree(execution.output, staged, symlinks=False)
            if (
                runtime.sha256_file(staged / "REPORT.md") != validated.report_sha256
                or runtime.sha256_file(staged / "result.json") != validated.result_sha256
                or runtime.sha256_file(staged / "evidence" / "manifest.json") != validated.evidence_manifest_sha256
            ):
                raise run_phase1.RunnerError(f"validated verifier output changed before promotion: {role}")
            created = False
            terminal_recorded = False
            try:
                if target.exists() or target.is_symlink():
                    bound = any(
                        row.get("output_artifact") == f"{target_rel}/result.json"
                        for row in run_phase1.read_ledger(root)
                    )
                    if bound or target.is_symlink() or not target.is_dir():
                        raise run_phase1.RunnerError(
                            f"verifier promotion target {target_rel} is already bound or unsafe; "
                            "recover it rather than overwrite it")
                    # A promotion that died before its terminal event: nothing references it.
                    # Keep it for diagnosis, out of the content-addressed path.
                    os.replace(target, phase_root / "transactions" / f"orphaned-{uuid.uuid4()}")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, target)
                created = True
                record = run_phase1.make_terminal_record(
                    execution, role, attempt_kind, input_hash, prompt_hash,
                    status="completed", output_artifact=f"{target_rel}/result.json",
                    output_hash=validated.result_sha256, error=None, error_class=None,
                    source_invocation_id=source_invocation_id,
                )
                run_phase1.terminal_append(root, record)
                terminal_recorded = True
                receipt = _receipt(
                    plan, invocation_id=execution.reservation["invocation_id"],
                    attempt=execution.attempt, attempt_kind=attempt_kind,
                    source_invocation_id=source_invocation_id, validated=validated,
                )
                runtime.atomic_write(receipt_path, runtime.pretty_bytes(receipt))
                _write_attempt_completed(execution, attempt_kind, input_hash, validated.result_sha256)
            except BaseException:
                # Once the terminal event exists the promoted tree must stay at its recorded hash.
                if not terminal_recorded and created:
                    shutil.rmtree(target, ignore_errors=True)
                raise
            shutil.rmtree(transaction, ignore_errors=True)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return _completion(receipt, reused=False)


def rebuild_verifier_receipt(
    plan: BatchPlan,
    root: pathlib.Path,
    phase_root: pathlib.Path,
    role: str,
    input_hash: str,
    snapshot_manifest: dict[str, Any],
    attempt_dir: pathlib.Path,
    status_path: pathlib.Path,
    status: dict[str, Any],
    row: dict[str, Any],
    validated: runtime.ValidatedRoleOutput,
) -> VerifierCompletion:
    """A completed terminal row exists but its receipt does not: rebuild it from the promoted tree."""
    target_rel = contract.output_dir(role, input_hash)
    target = root / target_rel
    for name, expected in (("result.json", validated.result_sha256), ("REPORT.md", validated.report_sha256),
                           ("evidence/manifest.json", validated.evidence_manifest_sha256)):
        path = runtime.regular_file(target / name, "terminal-recovery verifier output")
        if runtime.sha256_file(path) != expected:
            raise run_phase1.RunnerError(f"terminal-recovery verifier {name} hash disagrees with response")
    if row.get("output_artifact") != f"{target_rel}/result.json" or row.get("output_sha256") != validated.result_sha256:
        raise run_phase1.RunnerError("terminal-recovery verifier row does not bind the promoted result")
    attempt_input = runtime.strict_json(attempt_dir / "input.json")
    receipt = _receipt(
        plan, invocation_id=str(status.get("invocation_id")), attempt=row.get("attempt"),
        attempt_kind=attempt_input.get("attempt_kind", "role_inference"),
        source_invocation_id=attempt_input.get("source_invocation_id"), validated=validated,
    )
    receipt_path = run_phase1.completion_path(phase_root, role, input_hash)
    runtime.atomic_write(receipt_path, runtime.pretty_bytes(receipt))
    runtime.atomic_write(status_path, runtime.pretty_bytes({
        "schema_version": "i4r-phase1-attempt-1", "state": "completed",
        "invocation_id": status.get("invocation_id"), "attempt": row.get("attempt"),
        "attempt_kind": receipt["attempt_kind"], "recovered_after_terminal": True,
        "ended_at": status.get("ended_at"),
    }))
    return validate_verifier_receipt(plan, root, receipt_path, input_hash)


def verifier_targets(plan: BatchPlan) -> run_phase1.RoleTargets:
    return run_phase1.RoleTargets(
        output=verifier_output_contract(plan.packet),
        promote=functools.partial(promote_verifier, plan),
        validate_receipt=functools.partial(validate_verifier_receipt, plan),
        rebuild_receipt=functools.partial(rebuild_verifier_receipt, plan),
        extra_deny_read=plan.deny_read,
        repair_records_original_input=True,
        freeze_recovered_repairs=True,
    )


# ------------------------------------------------------------------------------------ the runner
def write_status(plan: BatchPlan, status: str, **fields: Any) -> None:
    """phase1/verifier/<role>/status.json -- the verifier's own status, never phase1/status.json.

    Finding ids appear only once the call is over: while it runs the file is readable from inside
    the sandbox, and the packet keeps the verifier blind to ids.
    """
    value = {
        "schema_version": STATUS_VERSION,
        "role": plan.role,
        "status": status,
        "source_snapshot_sha256": plan.snapshot_manifest["snapshot_sha256"],
        "packet_sha256": plan.packet_hash,
        "input_sha256": plan.input_hash,
        "claim_sha256s": plan.claim_hashes(),
        **fields,
    }
    try:
        runtime.validate_destination(plan.root, plan.status_path, "verifier status file")
    except runtime.Phase1Error as exc:  # never write through an unsafe path, even to report
        print(f"  [warn ] verifier status not written: {exc}", file=sys.stderr)
        return
    runtime.atomic_write(plan.status_path, runtime.pretty_bytes(value))


def dry_run_summary(plan: BatchPlan) -> dict[str, Any]:
    return {
        "status": "dry_run",
        "role": plan.role,
        "role_class": contract.ROLE_CLASS,
        "round": contract.ROUND,
        "would_replay": plan.replay,
        "would_rebuild_receipt": plan.recover_receipt,
        # Mode-only drift (content verified): entries a real run would re-freeze. Nothing changed.
        "would_refreeze": plan.would_refreeze,
        "input_sha256": plan.input_hash,
        "probe_key": plan.probe_key,
        "packet_sha256": plan.packet_hash,
        "prompt_sha256": runtime.sha256_bytes(plan.prompt.encode("utf-8")),
        "source_snapshot_sha256": plan.snapshot_manifest["snapshot_sha256"],
        "hypotheses": [
            {
                "hypothesis_id": row.hypothesis_id, "finding_id": row.finding_id,
                "claim_sha256": row.claim_sha256,
                "dropped_source_hashes": [{"path": p, "sha256": s} for p, s in row.dropped_source_hashes],
            }
            for row in plan.hypotheses
        ],
        "packet": plan.packet,
        "packet_files": ["ROLE_PACKET.json", *[f"evidence/{row['path']}" for row in plan.packet["assigned_evidence"]],
                         "packet_manifest.json"],
        "packet_dir": f"phase1/role_packets/{plan.snapshot_manifest['snapshot_sha256']}/{plan.role}/{plan.packet_hash}",
        "extra_deny_read": list(EXTRA_DENY_READ),
        "promotion_target": contract.output_dir(plan.role, plan.input_hash) if plan.input_hash else None,
        "completion_receipt": contract.completion_receipt_path(plan.role, plan.input_hash) if plan.input_hash else None,
        "attempt_ceiling": lifecycle_journal.max_attempts_per_role(),
        "inadmissible_verifier_records": list(plan.inadmissible_records),
        "provider_calls": 0,
    }


def summary(plan: BatchPlan, completion: VerifierCompletion) -> dict[str, Any]:
    verdicts = contract.parse_result(json.loads((plan.root / completion.result).read_text(encoding="utf-8")))
    return {
        "status": "replayed" if completion.reused else "completed",
        "role": completion.role,
        "input_sha256": completion.input_sha256,
        "invocation_id": completion.invocation_id,
        "result": completion.result,
        "result_sha256": completion.result_sha256,
        "completion_receipt": contract.completion_receipt_path(completion.role, completion.input_sha256),
        "hypotheses": [
            {**row, "verdict": verdicts.get(row["claim_sha256"])} for row in completion.hypotheses
        ],
    }


def refuse(exc: VerifierRefusal) -> int:
    print(json.dumps({"status": "refused", "reason": exc.reason, "remedy": exc.remedy}, sort_keys=True))
    print(f"REFUSED: {exc.reason}\n  Remedy: {exc.remedy}", file=sys.stderr)
    return EXIT_REFUSED


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, which is this runner's EXIT_FAILED ("the call failed")."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, f"{self.prog}: error: {message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--findings", nargs="+", required=True, metavar="ID",
                        help="registry ids of the INCLUDED post-fan-out rows to verify (one batch)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the planned hypotheses, role and packet; no provider call, no journal "
                             "event, no chmod (snapshot mode drift is reported as would_refreeze)")
    parser.add_argument("--claude-bin", default=None, help="Claude CLI executable (fake runner in tests)")
    parser.add_argument("--probe-cache-dir", type=pathlib.Path, default=None)
    parser.add_argument("--timeout-seconds", type=run_phase1.bounded_timeout, default=None)
    parser.add_argument("--pre-dispatch-deadline-seconds", type=run_phase1.bounded_pre_dispatch_deadline,
                        default=run_phase1.DEFAULT_PRE_DISPATCH_DEADLINE_SECONDS)
    parser.add_argument("--max-budget-usd", type=run_phase1.bounded_budget, default=run_phase1.DEFAULT_MAX_BUDGET_USD)
    args = parser.parse_args(argv)
    requested = [part for value in args.findings for part in value.split(",") if part.strip()]
    requested = [part.strip() for part in requested]

    root = args.audit_dir.resolve()
    if not root.is_dir():
        print(f"ERROR: audit directory does not exist: {root}", file=sys.stderr)
        return EXIT_ERROR
    phase_root = root / "phase1"
    if phase_root.is_symlink() or not phase_root.is_dir():
        return refuse(VerifierRefusal(
            "the audit has no Phase-1 runtime directory (phase1/)",
            "complete Phase 1 first: python3 scripts/run_phase1.py <audit-dir>",
        ))
    claude = args.claude_bin or shutil.which("claude")
    if not claude:
        print("ERROR: Claude CLI not found", file=sys.stderr)
        return EXIT_ERROR
    cache_dir = (args.probe_cache_dir or (phase_root / "probe_cache")).resolve()
    with (phase_root / "run.lock").open("a+") as run_lock:
        fcntl.flock(run_lock.fileno(), fcntl.LOCK_EX)
        try:
            return _run_locked(root, phase_root, requested, args, claude, cache_dir)
        finally:
            fcntl.flock(run_lock.fileno(), fcntl.LOCK_UN)


def _run_locked(
    root: pathlib.Path, phase_root: pathlib.Path, requested: list[str], args: argparse.Namespace,
    claude: str, cache_dir: pathlib.Path,
) -> int:
    plan: BatchPlan | None = None
    try:
        plan = plan_batch(root, phase_root, requested, args)
        deadline = runtime.MonotonicDeadline.after(args.pre_dispatch_deadline_seconds)
        started = time.monotonic()
        with run_phase1.hard_pre_dispatch_deadline(deadline):
            bind_inputs(plan, claude, deadline)
            if args.dry_run:
                print(json.dumps(dry_run_summary(plan), indent=2, sort_keys=True))
                return EXIT_COMPLETED
            if not plan.replay and not plan.recover_receipt:
                packet_dir = materialize_verifier_packet(plan, deadline)
                workspace = phase_root / "verifier" / plan.role / "preflight" / plan.snapshot_manifest["snapshot_sha256"]
                preflight = runtime.provider_free_preflight(
                    root, phase_root, plan.snapshot, plan.snapshot_manifest, {plan.role: packet_dir},
                    deadline,
                    destinations=[
                        (root / contract.output_dir(plan.role, plan.input_hash), "verifier promotion target"),
                        (run_phase1.completion_path(phase_root, plan.role, plan.input_hash),
                         "verifier completion receipt"),
                        (plan.status_path, "verifier status file"),
                    ],
                    workspace=workspace,
                    extra_deny_read=plan.deny_read,
                    lenient_modes=True,
                )
                preflight["deadline_seconds"] = args.pre_dispatch_deadline_seconds
                preflight["elapsed_seconds"] = round(time.monotonic() - started, 3)
                runtime.atomic_write(workspace / "receipt.json", runtime.pretty_bytes(preflight))
        # Outside the SIGALRM deadline (never under --dry-run, which returned above): the one
        # re-freeze walk, content and inventory re-verified before any chmod, journaled once.
        _refreeze_bound_snapshot(plan)
    except VerifierRefusal as exc:
        return refuse(exc)
    except runtime.SnapshotModeUnstableError as exc:
        # Content verified, but the modes would not stay frozen: nothing was dispatched.
        message = run_phase1.run_coordinator.redact_private_paths(str(exc), root)
        remedy = ("stop the sync client, or move the audit off the synced folder, then re-run this "
                  "batch; deleting the snapshot does not help while its modes keep changing")
        write_status(plan, "paused", pause_reason=runtime.SNAPSHOT_MODE_UNSTABLE, error=message,
                     remedy=remedy, verifier_inferences_started=0)
        print(json.dumps({"status": "paused", "pause_reason": runtime.SNAPSHOT_MODE_UNSTABLE,
                          "error": message, "remedy": remedy}, sort_keys=True))
        return EXIT_PAUSED
    except Exception as exc:  # provider-free preparation failed: nothing dispatched, no pause event
        message = run_phase1.run_coordinator.redact_private_paths(str(exc), root)
        if plan is not None and not args.dry_run:
            write_status(plan, "preflight_failed", pause_reason="local_preflight_failed",
                         verifier_inferences_started=0, error=message)
        print(json.dumps({"status": "preflight_failed", "error": message}, sort_keys=True))
        print(f"PAUSED: verifier local preflight failed: {message}", file=sys.stderr)
        return EXIT_PAUSED
    assert plan is not None and plan.input_hash is not None
    for note in plan.inadmissible_records:
        print(f"  [note ] inadmissible verifier record (not a verdict): {note}", file=sys.stderr)
    targets = verifier_targets(plan)
    if plan.recover_receipt:
        try:
            completion, _execution = run_phase1.recover_received_role_attempt(
                root, phase_root, plan.role, plan.input_hash, plan.snapshot_manifest,
                route=plan.route, targets=targets,
                deposit_exemptions=runtime.snapshot_deposit_exemptions(plan.snapshot),
            )
            if completion is None:
                raise run_phase1.RunnerError(
                    f"the ledger holds a completed {plan.role} call for this input but no retained "
                    "response (an attempt at state response_received) to rebuild its receipt from")
        except Exception as exc:
            message = run_phase1.run_coordinator.redact_private_paths(str(exc), root)
            write_status(plan, "failed", error=message)
            print(f"ERROR: verifier receipt rebuild failed: {message}", file=sys.stderr)
            return EXIT_FAILED
        return _finish(plan, completion)
    if plan.replay:
        try:
            completion = targets.validate_receipt(
                root, run_phase1.completion_path(phase_root, plan.role, plan.input_hash), plan.input_hash)
        except Exception as exc:
            message = run_phase1.run_coordinator.redact_private_paths(str(exc), root)
            write_status(plan, "failed", error=message)
            print(f"ERROR: verifier replay failed: {message}", file=sys.stderr)
            return EXIT_FAILED
        return _finish(plan, completion)
    write_status(plan, "preflight_complete", verifier_inferences_started=0)
    try:
        probe_key_value, _probe, probe_reused = run_phase1.run_capability_probe(
            root, phase_root, cache_dir, claude, plan.cli, plan.route, args.timeout_seconds,
            bound=plan.probe_bound,
        )
    except Exception as exc:
        message = run_phase1.run_coordinator.redact_private_paths(str(exc), root)
        write_status(plan, "probe_failed", pause_reason="capability_probe_failed",
                     verifier_inferences_started=0, error=message)
        print(f"PAUSED: capability probe failed: {message}", file=sys.stderr)
        return EXIT_PROBE
    if probe_key_value != plan.probe_key:
        write_status(plan, "failed", error="the capability probe key changed after planning")
        print("ERROR: the capability probe key changed after planning", file=sys.stderr)
        return EXIT_ERROR
    write_status(plan, "running", probe_key=probe_key_value, probe_reused=probe_reused)
    packet_dir = (phase_root / "role_packets" / plan.snapshot_manifest["snapshot_sha256"]
                  / plan.role / plan.packet_hash)
    try:
        completion = run_phase1.run_role(
            root, phase_root, plan.snapshot, plan.snapshot_manifest, plan.role, packet_dir,
            plan.packet, plan.prompt, probe_key_value, claude, plan.route,
            args.timeout_seconds, args.max_budget_usd, targets=targets,
        )
    except run_phase1.EnvironmentFaultError as exc:
        message = run_phase1.run_coordinator.redact_private_paths(str(exc), root)
        write_status(plan, "paused", pause_reason="sandbox_environment_fault", error=message)
        print(json.dumps({"status": "paused", "pause_reason": "sandbox_environment_fault",
                          "error": message}, sort_keys=True))
        return EXIT_PAUSED
    except runtime.SnapshotIntegrityError as exc:
        # The Phase-1 snapshot changed around the call (or failed its pre-dispatch check): the
        # host's fault, not the verifier's. Pause like a sandbox fault; the verifier status file
        # carries it, and run.json never sees a pause (its enum has no such reason). A mode that
        # would not stay frozen, with the content verified, has its own reason and remedy.
        message = run_phase1.run_coordinator.redact_private_paths(str(exc), root)
        if isinstance(exc, runtime.SnapshotModeUnstableError):
            reason = runtime.SNAPSHOT_MODE_UNSTABLE
            remedy = ("the Phase-1 snapshot verified against its manifest but its modes kept "
                      "changing; stop the sync client, or move the audit off the synced folder, "
                      "then re-run this batch")
        else:
            reason = "snapshot_integrity_fault"
            remedy = ("the Phase-1 snapshot bound by phase1/quorum.json no longer verifies; restore "
                      "it (it is read-only and content-addressed) or re-run Phase 1, then re-run "
                      "this batch")
        write_status(plan, "paused", pause_reason=reason, error=message, remedy=remedy)
        print(json.dumps({"status": "paused", "pause_reason": reason,
                          "error": message, "remedy": remedy}, sort_keys=True))
        return EXIT_PAUSED
    except Exception as exc:
        message = run_phase1.run_coordinator.redact_private_paths(str(exc), root)
        write_status(plan, "failed", error=message, hypotheses=plan.receipt_hypotheses())
        print(json.dumps({"status": "failed", "role": plan.role, "error": message}, sort_keys=True),
              file=sys.stderr)
        return EXIT_FAILED
    return _finish(plan, completion)


def _refreeze_bound_snapshot(plan: BatchPlan) -> None:
    """Re-freeze the bound snapshot's mode drift, outside the pre-dispatch deadline (2.6.8).

    bind_inputs only counted it (plan.would_refreeze) inside the SIGALRM deadline. The content and
    inventory are verified again before any chmod; a change since is the same refusal bind_inputs
    gives. The note is journaled by the one process that ran chmod.
    """
    if not plan.would_refreeze and not runtime.snapshot_has_mode_drift(plan.snapshot):
        return
    try:
        runtime.verify_snapshot(
            plan.snapshot, plan.snapshot_manifest, None, refreeze=True,
            on_refreeze=lambda facts: run_phase1.journal_snapshot_refreeze(
                plan.root, plan.snapshot, facts, "verifier_bind_inputs",
                source="run_verifier.py"),
        )
    except runtime.SnapshotModeUnstableError:
        raise
    except runtime.Phase1Error as exc:
        raise VerifierRefusal(
            f"verified snapshot mismatch: {exc}",
            "the snapshot bound by phase1/quorum.json must verify as frozen; restore it or re-run "
            "Phase 1 -- the verifier never rebuilds a snapshot",
        ) from exc


def _finish(plan: BatchPlan, completion: VerifierCompletion) -> int:
    value = summary(plan, completion)
    write_status(plan, value["status"], invocation_id=completion.invocation_id,
                 result=completion.result, result_sha256=completion.result_sha256,
                 hypotheses=value["hypotheses"])
    print(json.dumps(value, indent=2, sort_keys=True))
    return EXIT_COMPLETED


if __name__ == "__main__":
    raise SystemExit(main())
