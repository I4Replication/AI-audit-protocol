#!/usr/bin/env python3
"""Verify every critique round against its immutable runtime projection."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import shlex
import sys
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SKILL / "schemas/tools"))
import validate_json_schema
import validate_v2
import verify_diff_packet
import test_run_critique_runtime as critique_runtime
import run_post_delta_gates
import round_cap_amendment

# registry fields the projection publishes (Appendix A, comment.json) besides the claim text
# (report_section falls back to claim_ref in Appendix A, the claim tags and comment.json's section). Approval owns
# id, status, coordinator_approved, settled, verifications and ledger_disposition.
PROJECTED_FIELDS = ("confidence", "changes_conclusion", "changes_conclusion_which", "report_section", "claim_ref",
                    "impact", "impact_rank", "disclosure_flag", "remedy", "paper_only", "verification_tier",
                    "unverifiable_basis", "benign_explanations_ruled_out", "provenance", "verification_snippet")
UNRUN_ROUND_STATES = ("prepared", "awaiting_authorization", "ready")
PROJECTED_EVIDENCE_FIELDS = ("expected", "observed", "rerun_command", "tolerance", "tolerance_basis",
                             "source_hashes")   # source_hashes' paths are comment.json's artifact / package_locus
# binding a deferred approval must supply these (integrate_round.approval_fields_missing, validate_v2): a row the last
# round reviewed unapproved may gain them after it, from a missing/null value only
APPROVAL_SUPPLIED_FIELDS = ("report_section", "verification_snippet")


def changed_after_review(reviewed_row: dict, row: dict) -> list:
    """Published fields of `row` that differ from the reviewed snapshot row, beyond what binding a deferred
    approval may add."""
    # a deferred approval is a row the round reviewed INCLUDED but not yet approved (not one withdrawn then)
    deferred = reviewed_row.get("coordinator_approved") is not True \
        and (reviewed_row.get("ledger_disposition") or {}).get("status") == "INCLUDED"
    moved = [field for field in PROJECTED_FIELDS if reviewed_row.get(field) != row.get(field)
             and not (deferred and field in APPROVAL_SUPPLIED_FIELDS and reviewed_row.get(field) is None)]
    reviewed_evidence = reviewed_row.get("evidence") if isinstance(reviewed_row.get("evidence"), dict) else {}
    live_evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    return moved + [f"evidence.{field}" for field in PROJECTED_EVIDENCE_FIELDS
                    if reviewed_evidence.get(field) != live_evidence.get(field)]


SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")



TARGET_SNAPSHOT_VERSIONS = {"i4r-target-snapshot-3", "i4r-target-snapshot-4"}
_CEILING_WARNED = False

def digest(path: pathlib.Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def claims_match_assignment(values: list[str], assigned: list[str]) -> bool:
    """True when a critic covered exactly its assigned claims, in any order and case.

    This is the rule the critique runtime enforces when it projects a round
    (test_run_critique_runtime.py, command_projection): each id once, compared casefolded as a set.
    No prompt states an ordering rule and the provider schema cannot express one, so a plain list
    comparison here refused, at finalization, a round the runtime had accepted -- the tenth
    production audit's round-2 completeness critic listed F39 before F3.
    """
    folded = [value.casefold() for value in values]
    return len(folded) == len(set(folded)) and set(folded) == {value.casefold() for value in assigned}


def admit_repair_prompt(hashes: dict[str, Any], expected_hashes: dict[str, Any],
                        round_dir: pathlib.Path, role: str) -> dict[str, Any]:
    """Allow the prompt hash of a focused repair, and nothing else.

    A focused output repair re-dispatches the role with an augmented prompt and stamps THAT
    prompt's hash into the result, while the rendered prompt on the snapshot is the original.
    Requiring equality made a repaired role unfinalizable -- and the repair path is a
    documented, journalled part of the contract, so an audit could complete every round and then
    be unable to finalize. The binding stays real: the recorded hash must still match a prompt
    file that exists on disk for THIS role and THIS round, and every other input hash must match
    exactly. Returns the expected hashes, relaxed only where that holds.
    """
    if hashes == expected_hashes or "prompt_sha256" not in hashes:
        return expected_hashes
    repair_prompts = {
        digest(candidate)
        for candidate in (round_dir / "invocations").glob(
            f"*/attempts/{role}/*/repair_prompt.txt")
        if candidate.is_file()
    }
    if hashes["prompt_sha256"] in repair_prompts:
        expected_hashes = dict(expected_hashes)
        expected_hashes["prompt_sha256"] = hashes["prompt_sha256"]
    return expected_hashes


def verify_snapshot_diff_binding(audit: pathlib.Path, diff_path: pathlib.Path, number: int,
                                 snapshot_dir: pathlib.Path) -> None:
    packet = load(diff_path)
    if packet.get("packet_version") != "i4r-diff-packet-2" or packet.get("round") != number:
        raise ValueError(f"round {number}: snapshot diff packet identity mismatch")
    paths = packet.get("input_paths") or {}
    inputs = packet.get("inputs") or {}
    expected = {
        "after_report_sha256": snapshot_dir / "source/REPORT_TO_REVIEW.md",
        "after_findings_sha256": snapshot_dir / "source/context/work/findings.jsonl",
    }
    for key in ("before_report", "before_findings"):
        relative = paths.get(key)
        if not isinstance(relative, str) or not relative or relative.startswith("/") or ".." in relative.split("/"):
            raise ValueError(f"round {number}: snapshot diff packet has an unsafe {key} path")
        expected[f"{key}_sha256"] = audit / relative
    for key, path in expected.items():
        if not path.is_file() or digest(path) != inputs.get(key):
            hint = ""
            if key.startswith("before_"):
                # usually a renumber that rewrote round N-1's before-files after round N ran: the bytes round N
                # was built from are still in round N-1's snapshots
                name = "REPORT_TO_REVIEW.md" if key == "before_report_sha256" else "context/work/findings.jsonl"
                relative = paths[key[: -len("_sha256")]]
                # round N-1's snapshots, and the post-delta gates' payload copy of the inputs they checked
                candidates = sorted(audit.glob(f"critique/round_{number - 1}/snapshots/*/source/{name}")) + sorted(
                    audit.glob(f"critique/round_*/post_delta/*/payload/{relative}")) + (
                    # integrate_round's write-once backup (make_diff_packet's step-1 hint copies it after integration)
                    sorted(audit.glob(f"work/findings.before_round{number - 1}_integration.jsonl"))
                    if key == "before_findings_sha256" else [])
                for candidate in candidates:
                    if digest(candidate) == inputs.get(key):
                        hint = (f". It changed after round {number} ran (a renumber must leave it alone; "
                                f"make_diff_packet translates its ids). Restore it: "
                                f"chmod u+w {shlex.quote(str(path))} && cp {shlex.quote(str(candidate))} {shlex.quote(str(path))}")
                        break
                if not hint and key == "before_report_sha256":
                    # the bound bytes may be the snapshot replayed through a renumber (the N=1 recipe rewrites the
                    # step-1 copy): the same candidates make_diff_packet restores from
                    import renumber_findings
                    reviewed = sorted(audit.glob(f"critique/round_{number - 1}/snapshots/*/source/REPORT_TO_REVIEW.md"))
                    match = next((text for kind, text in renumber_findings.bound_report_candidates(audit, relative, reviewed)
                                  if kind == "text" and hashlib.sha256(text.encode("utf-8")).hexdigest() == inputs.get(key)),
                                 None)
                    if match is not None and relative == f"critique/round_{number - 1}/REPORT_BEFORE_INTEGRATION.md":
                        hint = (f". It changed after round {number} ran; the bytes round {number} was built from can be "
                                f"rebuilt: python3 {shlex.quote(str(HERE / 'make_diff_packet.py'))} {shlex.quote(str(audit.resolve()))} --round {number} "
                                f"--before-report {shlex.quote(str(audit.resolve() / f'critique/round_{number - 1}/REPORT_BEFORE_INTEGRATION.md'))} "
                                f"--after-report {shlex.quote(str(audit.resolve() / 'report/REPORT_final.md'))} --before-findings "
                                f"{shlex.quote(str(audit.resolve() / f'critique/round_{number - 1}/FINDINGS_BEFORE_INTEGRATION.jsonl'))} "
                                f"--output {shlex.quote(str(audit.resolve() / f'work/DIFF_MANIFEST_round_{number}.rebuilt.json'))} "
                                "(it writes a restore copy and prints the cp command)")
                    elif match is not None:
                        # a path make_diff_packet will not restore (older audits bound e.g. a .renumbered.md copy)
                        restored = path.with_name(path.name + ".restored")
                        try:
                            restored.write_text(match, encoding="utf-8")
                            hint = (f". It changed after round {number} ran; the bytes it was built from are in "
                                    f"{restored}: chmod u+w {shlex.quote(str(path))} && cp {shlex.quote(str(restored))} {shlex.quote(str(path))}")
                        except OSError:
                            hint = f". It changed after round {number} ran; restore it from a backup of the audit folder"
            raise ValueError(f"round {number}: snapshot diff packet {key} is not bound to its input{hint}")



def load(path: pathlib.Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"required immutable round file is missing or symlinked: {path}")
    value = validate_json_schema.strict_loads(path.read_text(encoding="utf-8"), source=str(path))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def verify_inventory(root: pathlib.Path, expected: Any, manifest_name: str = "MANIFEST.json") -> None:
    if not isinstance(expected, dict):
        raise ValueError(f"{root}: files_sha256 must be an object")
    actual: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink inside immutable round tree: {path}")
        if path.is_file() and path.relative_to(root).as_posix() != manifest_name:
            actual[path.relative_to(root).as_posix()] = digest(path)
    if actual != expected:
        raise ValueError(f"immutable file inventory differs under {root}")


def verify_result_evidence(result: dict[str, Any], snapshot: pathlib.Path) -> None:
    inventory_paths = (
        snapshot / "source/context/EVIDENCE_HASHES.json",
        snapshot / "source/EVIDENCE_HASHES.json",
    )
    found = [path for path in inventory_paths if path.is_file() and not path.is_symlink()]
    if len(found) != 1:
        raise ValueError(f"{snapshot}: expected exactly one frozen evidence inventory")
    inventory_path = found[0]
    if digest(inventory_path) != result["input_hashes"]["evidence_manifest_sha256"]:
        raise ValueError("critic evidence inventory hash is not bound to its result")
    inventory = load(inventory_path)
    snapshot_manifest = load(snapshot / "MANIFEST.json")
    manifest_hashes = snapshot_manifest.get("files_sha256")
    for collection in ("items", "new_issues"):
        for index, assessment in enumerate(result[collection]):
            evidence = assessment["evidence"]
            bindings = list(evidence["artifact_refs"])
            if evidence["reproducible_script"] is not None:
                bindings.append(evidence["reproducible_script"])
            for binding_index, binding in enumerate(bindings):
                if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
                    raise ValueError(
                        f"{collection}[{index}] evidence binding {binding_index} is malformed"
                    )
                rel = binding["path"]
                if (not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) or
                        re.match(r"^[A-Za-z]:[\\/]", rel) or
                        ".." in pathlib.PurePosixPath(rel.replace("\\", "/")).parts):
                    raise ValueError(f"unsafe critic evidence path: {rel!r}")
                row = inventory.get(rel)
                if not isinstance(row, dict) or set(row) != {"sha256", "size"}:
                    raise ValueError(f"critic evidence is not in frozen inventory: {rel}")
                artifact = snapshot / rel
                cursor = snapshot
                for part in pathlib.PurePosixPath(rel.replace("\\", "/")).parts:
                    cursor = cursor / part
                    if cursor.is_symlink():
                        raise ValueError(f"symlinked critic evidence path: {rel}")
                if not artifact.is_file():
                    raise ValueError(f"missing critic evidence artifact: {rel}")
                actual = digest(artifact)
                manifest_rel = artifact.relative_to(snapshot).as_posix()
                if (binding["sha256"] != actual or row.get("sha256") != actual or
                        row.get("size") != artifact.stat().st_size or
                        not isinstance(manifest_hashes, dict) or
                        manifest_hashes.get(manifest_rel) != actual):
                    raise ValueError(f"critic evidence hash binding differs: {rel}")


def _gated_before_start(audit: pathlib.Path, number: int) -> bool:
    """A passing gate=post_delta event for round N precedes the round's last round_start (vacuous with no start)."""
    try:
        lines = (audit / "run.events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return True   # the journal checks elsewhere refuse a missing journal
    passed_at = []
    last_start = None
    for index, line in enumerate(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, dict) or payload.get("round") != number:
            continue
        if event.get("type") == "round_start":
            last_start = index
        elif (event.get("type") == "gate" and payload.get("gate") == "post_delta"
              and payload.get("exit_status") in (0, "0")
              # the event run_post_delta_gates journals names its receipt; a bare hand-logged pass does not count
              and payload.get("log_path") == f"critique/round_{number}/POST_DELTA_GATES.json"):
            passed_at.append(index)
    return last_start is None or any(index < last_start for index in passed_at)


def verify(audit: pathlib.Path, *, require_post_delta_gates: bool = False, stopping_reason: str | None = None) -> int:
    """The whole check. A stale post-delta receipt is reported last (or together with another failure): the rules
    the release chain cannot fix (prose, published fields, INCLUDED set, open items) come first, so an operator does
    not redo the whole restage chain only to meet them afterwards."""
    deferred: list = []
    try:
        rounds_joined = _verify(audit, require_post_delta_gates=require_post_delta_gates, deferred=deferred,
                                stopping_reason=stopping_reason)
    except ValueError as exc:
        if deferred:
            raise ValueError(f"{exc}" + "".join(f"\n  Also: {item}" for item in deferred)) from exc
        raise
    if deferred:
        raise ValueError(f"{deferred[0]}" + "".join(f"\n  Also: {item}" for item in deferred[1:]))
    print(f"immutable critique projections: {rounds_joined} rounds exactly joined")
    return 0


def _verify(audit: pathlib.Path, *, require_post_delta_gates: bool = False, deferred: list,
           stopping_reason: str | None = None) -> int:
    global _CEILING_WARNED
    audit = audit.resolve()
    run = load(audit / "run.json")
    if run.get("schema_version") not in {"2.0.0", "2.1.0"}:
        print("legacy run: immutable round projection gate not applicable")
        return 0
    audit_id = run.get("audit_id")
    rounds = run.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        raise ValueError("v2 finalization requires recorded critique rounds")
    # 2.6.9: a raised round cap or a reopened audit must be the journaled operator act, never a hand edit; the
    # release boundary (finalize, publish) re-checks the whole chain (scripts/round_cap_amendment.py)
    amendment_violations, amendment_warnings = round_cap_amendment.verify(audit, run)
    for message in amendment_warnings:
        print(f"warning: {message}", file=sys.stderr)
    if amendment_violations:
        raise ValueError("round-cap amendment or reopening is inconsistent: " + "; ".join(amendment_violations)
                         + " (INSTALL.md, 'Raising the round cap of an existing audit')")
    policy = load(audit / "pipeline-policy.snapshot.json").get("policy")
    if not isinstance(policy, dict):
        raise ValueError("policy snapshot lacks policy")
    try:
        critic_model = policy["models"]["critics"]
        targeted_model = policy["models"]["targeted_adjudicator"]
    except (KeyError, TypeError) as exc:
        raise ValueError("policy snapshot lacks split critic/adjudicator model routing") from exc
    ledger_rows = []
    ledger_path = audit / "invocations.jsonl"
    for number, line in enumerate(ledger_path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            row = validate_json_schema.strict_loads(line, source=f"{ledger_path}:{number}")
            if not isinstance(row, dict):
                raise ValueError(f"invocation ledger line {number} is not an object")
            validate_v2.validate_invocation(row)
            ledger_rows.append(row)

    expected_numbers = list(range(1, len(rounds) + 1))
    if [row.get("round") for row in rounds] != expected_numbers:
        raise ValueError("run critique rounds are not unique and contiguous")
    # a round prepared (or authorized) but never run -- no critic call, no projection -- is not part of the review:
    # the audit may stop at the round before it (a trailing one only; a gap in the middle stays an error below)
    # never started: a round whose authorization lapsed mid-run is back in those states but keeps started_at
    # ... and no critic or adjudicator call in the ledger (a failed round re-prepared has started_at cleared)
    called_rounds = {row.get("round") for row in ledger_rows if row.get("role_class") in ("critic", "adjudicator")}
    while rounds and rounds[-1].get("state") in UNRUN_ROUND_STATES and not rounds[-1].get("started_at") \
            and rounds[-1]["round"] not in called_rounds \
            and not (audit / f"critique/round_{rounds[-1]['round']}/CURRENT_PROJECTION").is_file():
        print(f"note: round {rounds[-1]['round']} was prepared but never run; the review ends at round "
              f"{rounds[-1]['round'] - 1}", file=sys.stderr)
        rounds = rounds[:-1]
    rereviewed_change_ids: list[str] = []
    last_reviewed_report: tuple[Any, Any] = (None, None)
    last_reviewed_report_path = None
    last_reviewed_registry: pathlib.Path | None = None
    last_projection: pathlib.Path | None = None
    for row in rounds:
        number = row["round"]
        round_dir = audit / f"critique/round_{number}"
        pointer_path = round_dir / "CURRENT_PROJECTION"
        if pointer_path.is_symlink() or not pointer_path.is_file():
            raise ValueError(
                f"round {number}: immutable current-projection pointer is missing -- the round started (or called a "
                f"critic) but did not finish with a quorum. Finish it (re-run scripts/run_critique.sh {number}; for a "
                "failed round the driver's own message names the retry), then integrate or leave it as the last round")
        invocation_id = pointer_path.read_text(encoding="utf-8").strip()
        if not SAFE_ID.fullmatch(invocation_id):
            raise ValueError(f"round {number}: unsafe current projection ID")
        projection = round_dir / "projections" / invocation_id
        try:
            projection.resolve().relative_to(round_dir.resolve())
        except ValueError as exc:
            raise ValueError(f"round {number}: projection escapes round directory") from exc
        manifest = load(projection / "MANIFEST.json")
        last_projection = projection
        if (manifest.get("projection_version") != "i4r-round-projection-1" or
                manifest.get("invocation_id") != invocation_id or
                manifest.get("audit_id") != audit_id or manifest.get("round") != number):
            raise ValueError(f"round {number}: projection identity mismatch")
        roles = manifest.get("roles")
        if not isinstance(roles, list) or not roles or len(roles) != len(set(roles)):
            raise ValueError(f"round {number}: projection role quorum is malformed")
        verify_inventory(projection, manifest.get("files_sha256"))
        routing = load(projection / "ROUTING.json")
        if (routing.get("routing_version") != "i4r-round-routing-2" or
                routing.get("round") != number or routing.get("roles") != roles):
            raise ValueError(f"round {number}: routing does not match immutable projection")
        assignment_mode = routing.get("claim_assignment_mode")
        assignments = routing.get("claim_assignments")
        if not isinstance(assignments, dict) or set(assignments) != set(roles):
            raise ValueError(f"round {number}: claim assignments do not match routed roles")
        # Routing contract (i4r-round-routing-2, written by the critique runtime): every round
        # assigns claims to roles by domain; completeness carries the whole scope and the union of
        # all assignments is the full scope. Round 1's scope is every canonical registry row; a
        # later round's scope is the diff packet's changed_claim_ids (checked below).
        for role in roles:
            values = assignments[role]
            if (not isinstance(values, list) or len(values) != len(set(values)) or
                    any(not isinstance(value, str) or not value for value in values)):
                raise ValueError(f"round {number}: claim assignment is malformed for {role}")
        if number == 1:
            if assignment_mode != "domain_routed_round_1":
                raise ValueError("round 1 must use domain-routed claim assignments")
        elif assignment_mode != "domain_routed_delta":
            raise ValueError(f"round {number}: later-round claim assignment is not domain-routed")
        declared = row.get("critics_run")
        projected = [f"critic_{role}" for role in roles]
        if declared != projected:
            raise ValueError(
                f"round {number}: run critics_run {declared!r} differs from immutable route {projected!r}"
            )
        if number == 1 and projected != policy["critique"]["round_1"]["roles"]:
            raise ValueError("round 1 immutable route differs from canonical four-role policy")
        if number > 1:
            if routing.get("diff_manifest_present") is not True:
                raise ValueError(f"round {number}: later round lacks its generated diff manifest")
            diff_path = round_dir / "snapshots" / manifest["input_sha256"] / "source/context/DIFF_MANIFEST.json"
            if digest(diff_path) != routing.get("diff_manifest_sha256"):
                raise ValueError(f"round {number}: routed diff-manifest hash is stale")
            # The snapshot packet describes the state the round reviewed. Its deterministic
            # recomputation was verified against the live state by run_post_delta_gates.py when
            # that state was current (content-bound receipt); after integration the live files
            # have moved on, so here the packet is verified by its recorded bindings: version,
            # round, and the SHA-256 of each bound input against the immutable snapshot copies
            # (after-state) and the preserved pre-integration files (before-state).
            snapshot_dir = round_dir / "snapshots" / manifest["input_sha256"]
            verify_snapshot_diff_binding(audit, diff_path, number, snapshot_dir)
            diff = load(diff_path)
            expected_claims = diff.get("changed_claim_ids")
            if not isinstance(expected_claims, list):
                raise ValueError(f"round {number}: diff packet lacks changed_claim_ids")
            scope = set(expected_claims)
            assigned_union = set().union(*(set(assignments[role]) for role in roles))
            if assigned_union != scope or any(not set(assignments[role]) <= scope for role in roles):
                raise ValueError(f"round {number}: routed claim assignments differ from diff packet")

        snapshot = round_dir / "snapshots" / manifest["input_sha256"]
        snapshot_manifest = load(snapshot / "MANIFEST.json")
        last_reviewed_registry = snapshot / "source/context/work/findings.jsonl"
        if number == 1:
            registry_path = snapshot / "source/context/work/findings.jsonl"
            registry_ids = set()
            for line in registry_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    registry_ids.add(json.loads(line)["id"])
            assigned_union = set().union(*(set(assignments[role]) for role in roles))
            if assigned_union != registry_ids:
                raise ValueError("round 1 routed claim assignments do not cover the canonical registry")
        if (snapshot_manifest.get("snapshot_version") != "i4r-runtime-snapshot-5" or
                snapshot_manifest.get("input_sha256") != manifest.get("input_sha256") or
                snapshot_manifest.get("report_sha256") != manifest.get("report_sha256") or
                snapshot_manifest.get("round") != number or snapshot_manifest.get("roles") != roles or
                snapshot_manifest.get("audit_id") != audit_id):
            raise ValueError(f"round {number}: content-addressed input snapshot identity mismatch")
        if (snapshot_manifest.get("model") != critic_model.get("requested_model") or
                snapshot_manifest.get("effort") != critic_model.get("reasoning_effort")):
            raise ValueError(
                f"round {number}: ordinary critic snapshot model/effort differs from policy"
            )
        verify_inventory(snapshot, snapshot_manifest.get("files_sha256"))
        if digest(snapshot / "source/REPORT_TO_REVIEW.md") != manifest.get("report_sha256"):
            raise ValueError(f"round {number}: report snapshot hash is stale")
        last_reviewed_report = (number, manifest.get("report_sha256"))
        last_reviewed_report_path = snapshot / "source/REPORT_TO_REVIEW.md"

        def exact_invocation(result: dict[str, Any], result_path: pathlib.Path,
                             expected_role: str, expected_input_sha: str,
                             configured_model: dict[str, Any]) -> None:
            matches = [item for item in ledger_rows
                       if item.get("invocation_id") == result.get("invocation_id")]
            if len(matches) != 1:
                raise ValueError(
                    f"round {number} {expected_role}: result does not join one exact invocation ID"
                )
            telemetry = matches[0]
            expected = {
                "provider_call": True, "status": "completed", "round": number,
                "role": expected_role, "input_artifact_sha256": expected_input_sha,
                "prompt_sha256": result["input_hashes"]["prompt_sha256"],
                "output_sha256": digest(result_path),
                "requested_model": configured_model["requested_model"],
                "provider": configured_model["provider"],
                "reasoning_effort": configured_model["reasoning_effort"],
            }
            if any(telemetry.get(key) != value for key, value in expected.items()):
                raise ValueError(
                    f"round {number} {expected_role}: telemetry/output join is not exact"
                )

        actual_assessments: dict[str, list[str]] = {}
        result_rows: list[tuple[str, dict[str, Any]]] = []
        for role in roles:
            result_path = projection / f"critic_{role}.json"
            markdown_path = projection / f"critic_{role}.md"
            sidecar = load(projection / f"{role}.complete.json")
            result = load(result_path)
            schema_errors = validate_json_schema.errors_for_value(
                result, SKILL / "schemas/v2/critic-result.schema.json",
            )
            if schema_errors:
                raise ValueError(
                    f"round {number} critic_{role}: critic-result schema errors: "
                    + "; ".join(schema_errors[:20])
                )
            validate_v2.validate_critic_result(result)
            verify_result_evidence(result, snapshot)
            expected_sidecar = {
                "completion_version": "i4r-runtime-completion-2", "audit_id": audit_id,
                "input_sha256": manifest["input_sha256"], "report_sha256": manifest["report_sha256"],
                "round": number, "role": role, "completion_marker": True,
                "output_sha256": digest(result_path), "markdown_sha256": digest(markdown_path),
            }
            if any(sidecar.get(key) != value for key, value in expected_sidecar.items()):
                raise ValueError(f"round {number} critic_{role}: completion sidecar mismatch")
            # The operator attempt ceiling is a runtime knob (MAX_TRIES, 1..20), not a scientific
            # parameter; a policy snapshot frozen before the key existed implies the knob's maximum.
            _critique_policy = policy.get("critique") or {}
            attempt_ceiling = _critique_policy.get("max_attempts_per_role_operator_ceiling", 20)
            if ("max_attempts_per_role_operator_ceiling" not in _critique_policy
                    and not _CEILING_WARNED):
                # Mirror finalize_run.sh: say so rather than silently widening 3 -> 20. Warn once
                # per run, not once per critic per round -- this sits inside a nested loop.
                print("WARNING: policy snapshot predates max_attempts_per_role_operator_ceiling; "
                      "assuming the knob maximum of 20", file=sys.stderr)
                _CEILING_WARNED = True
            if not isinstance(sidecar.get("attempts"), int) or not 1 <= sidecar["attempts"] <= attempt_ceiling:
                raise ValueError(f"round {number} critic_{role}: invalid round-wide attempt count")
            if (result.get("audit_id") != audit_id or result.get("round") != number or
                    result.get("role") != f"critic_{role}" or
                    result.get("report_sha256") != manifest["report_sha256"] or
                    result.get("status") != "complete" or result.get("completion_marker") is not True):
                raise ValueError(f"round {number} critic_{role}: result identity/completion mismatch")
            hashes = result["input_hashes"]
            expected_hashes = {
                "snapshot_sha256": manifest["input_sha256"],
                "report_sha256": manifest["report_sha256"],
                "prompt_sha256": digest(snapshot / f"rendered/prompt_{role}.txt"),
                "policy_sha256": digest(snapshot / "source/context/PIPELINE_POLICY.json"),
                "evidence_manifest_sha256": digest(
                    snapshot / "source/context/EVIDENCE_HASHES.json"),
            }
            expected_hashes = admit_repair_prompt(hashes, expected_hashes, round_dir, role)
            if hashes != expected_hashes:
                raise ValueError(f"round {number} critic_{role}: input-hash binding mismatch")
            assessed_claims = [item["finding_id"] for item in result["items"]]
            actual_assessments[role] = assessed_claims
            result_rows.append((role, result))
            if number > 1 and not (claims_match_assignment(result["covered_claim_ids"], assignments[role])
                                   and claims_match_assignment(assessed_claims, assignments[role])):
                raise ValueError(
                    f"round {number} critic_{role}: actual claim assessments are not assigned"
                )
            exact_invocation(
                result, result_path, f"critic_{role}", manifest["input_sha256"], critic_model,
            )

        actual_rereviewed = list(dict.fromkeys(
            claim_id for role in roles for claim_id in actual_assessments[role]
        ))
        if manifest.get("claim_assessments") != actual_assessments:
            raise ValueError(f"round {number}: projection claim assessments are not actual results")
        if manifest.get("rereviewed_claim_ids") != actual_rereviewed:
            raise ValueError(f"round {number}: rereview projection is not derived from assessments")
        if number > 1:
            # Domain routing reassesses each changed claim in the role(s) it was assigned to; the
            # union across roles, in any order, must be exactly the packet's changed claims.
            if set(actual_rereviewed) != set(expected_claims) or len(set(expected_claims)) != len(expected_claims):
                raise ValueError(f"round {number}: changed claims lack exact completed reassessment")
            # The run's changed-claim scope follows the generated packets' own order; the order in
            # which roles happened to assess them is not part of the contract.
            for claim_id in expected_claims:
                if claim_id not in rereviewed_change_ids:
                    rereviewed_change_ids.append(claim_id)

        merge_path = projection / "CLAIM_MERGE.json"
        merge = load(merge_path)
        packet = load(projection / "adjudication_packet.json")
        if (manifest.get("claim_merge_sha256") != digest(merge_path) or
                packet.get("claim_merge_sha256") != digest(merge_path)):
            raise ValueError(f"round {number}: deterministic claim merge hash is unbound")
        frozen_registry = snapshot / "source/context/work/findings.jsonl"
        expected_merge = critique_runtime.build_claim_merge(
            result_rows, critique_runtime.load_registry_file(frozen_registry),
            packet.get("triggers", []), audit_id=audit_id, round_number=number,
            input_sha=manifest["input_sha256"], report_sha=manifest["report_sha256"],
        )
        if merge != expected_merge:
            raise ValueError(f"round {number}: claim merge differs from exact role assessments")
        rebuilt_rows = critique_runtime.unresolved_rows_for_packet(
            packet.get("triggers", []), merge,
        )
        if (packet.get("packet_version") != "i4r-targeted-adjudication-packet-3" or
                packet.get("unresolved_rows") != [
                    row for row in rebuilt_rows if row["adjudication_ready"]
                ] or packet.get("deferred_rows") != [
                    row for row in rebuilt_rows if not row["adjudication_ready"]
                ]):
            raise ValueError(f"round {number}: targeted packet includes non-unresolved merge rows")

        adjudication = manifest.get("adjudication")
        triggers = packet.get("triggers")
        if not isinstance(triggers, list):
            raise ValueError(f"round {number}: adjudication packet lacks trigger array")
        target_names = ("CRITIQUE.json", "CRITIQUE.md", "targeted_adjudicator.complete.json")
        if adjudication == "complete":
            if not triggers:
                raise ValueError(f"round {number}: completed targeted adjudication has no trigger")
            target_sha = manifest.get("target_input_sha256")
            if not isinstance(target_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", target_sha):
                raise ValueError(f"round {number}: completed adjudication lacks target input hash")
            target = round_dir / "target_snapshots" / target_sha
            target_manifest = load(target / "MANIFEST.json")
            # -4 added the shared output contract to the adjudicator prompt. A -3 snapshot is
            # still a valid historical record, so an audit that already ran a round under it must
            # keep validating; only new snapshots are built at -4.
            if (target_manifest.get("snapshot_version") not in TARGET_SNAPSHOT_VERSIONS or
                    target_manifest.get("target_input_sha256") != target_sha or
                    target_manifest.get("round_input_sha256") != manifest["input_sha256"] or
                    target_manifest.get("report_sha256") != manifest["report_sha256"] or
                    target_manifest.get("round") != number or
                    target_manifest.get("audit_id") != audit_id):
                raise ValueError(f"round {number}: targeted snapshot identity mismatch")
            if (target_manifest.get("model") != targeted_model.get("requested_model") or
                    target_manifest.get("effort") != targeted_model.get("reasoning_effort")):
                raise ValueError(
                    f"round {number}: targeted adjudicator snapshot model/effort differs from policy"
                )
            verify_inventory(target, target_manifest.get("files_sha256"))
            target_result_path = projection / "CRITIQUE.json"
            target_markdown = projection / "CRITIQUE.md"
            target_result = load(target_result_path)
            target_sidecar = load(projection / "targeted_adjudicator.complete.json")
            schema_errors = validate_json_schema.errors_for_value(
                target_result, SKILL / "schemas/v2/critic-result.schema.json",
            )
            if schema_errors:
                raise ValueError(
                    f"round {number} targeted adjudicator schema errors: "
                    + "; ".join(schema_errors[:20])
                )
            validate_v2.validate_critic_result(target_result)
            verify_result_evidence(target_result, target)
            target_expected_sidecar = {
                "completion_version": "i4r-runtime-completion-2", "audit_id": audit_id,
                "input_sha256": target_sha, "report_sha256": manifest["report_sha256"],
                "round": number, "role": "targeted_adjudicator", "completion_marker": True,
                "output_sha256": digest(target_result_path),
                "markdown_sha256": digest(target_markdown),
            }
            if any(target_sidecar.get(key) != value
                   for key, value in target_expected_sidecar.items()):
                raise ValueError(f"round {number}: targeted adjudication sidecar mismatch")
            if (target_result.get("audit_id") != audit_id or target_result.get("round") != number or
                    target_result.get("role") != "critic_synth" or
                    target_result.get("report_sha256") != manifest["report_sha256"] or
                    target_result.get("status") != "complete" or
                    target_result.get("completion_marker") is not True or
                    target_result.get("input_hashes") != target_manifest.get("input_hashes")):
                raise ValueError(f"round {number}: targeted result identity/input binding mismatch")
            unresolved_ids = [item.get("row_id") for item in packet.get("unresolved_rows", [])]
            covered_items = [item.get("finding_id") for item in target_result["items"]]
            if (target_result["coverage"]["assigned_checks"] != unresolved_ids or
                    target_result["covered_claim_ids"] != unresolved_ids or
                    covered_items != unresolved_ids):
                raise ValueError(f"round {number}: targeted result does not cover unresolved rows")
            provider_packet = load(target / "source/adjudication_packet.json")
            if set(provider_packet) != {
                    "packet_version", "audit_id", "round", "input_sha256", "report_sha256",
                    "policy", "claim_merge_sha256", "unresolved_rows"}:
                raise ValueError(f"round {number}: target snapshot exposes non-delta packet fields")
            if (provider_packet.get("packet_version") != "i4r-targeted-adjudication-input-2" or
                    provider_packet.get("unresolved_rows") != packet.get("unresolved_rows")):
                raise ValueError(f"round {number}: target snapshot unresolved rows differ")
            if (target / "source/critics").exists() or (target / "source/critics").is_symlink():
                raise ValueError(f"round {number}: target snapshot exposes full critic outputs")
            exact_invocation(
                target_result, target_result_path, "critic_synth", target_sha, targeted_model,
            )
        else:
            if manifest.get("target_input_sha256") is not None:
                raise ValueError(f"round {number}: no-target projection declares target input")
            if any((projection / name).exists() or (projection / name).is_symlink()
                   for name in target_names):
                raise ValueError(f"round {number}: no-target projection contains targeted output")

        status = load(round_dir / "ROUND_STATUS.json")
        if (status.get("status_version") != "i4r-round-status-2" or
                status.get("invocation_id") != invocation_id or status.get("audit_id") != audit_id or
                status.get("round") != number or status.get("input_sha256") != manifest["input_sha256"] or
                status.get("report_sha256") != manifest["report_sha256"] or
                status.get("expected_roles") != roles or status.get("completed_roles") != roles or
                status.get("failed_roles") != [] or status.get("quorum_met") is not True or
                status.get("outcome") != "complete" or status.get("exit_code") != 0):
            raise ValueError(f"round {number}: terminal status does not prove exact successful quorum")
        # Runtime critique validation happens before the coordinator integrates the
        # delta and runs the whole-document gates. Finalization/publication opt in
        # to this receipt requirement, preserving a strict release boundary.
        if number > 1 and require_post_delta_gates:
            receipt = round_dir / "POST_DELTA_GATES.json"
            try:
                run_post_delta_gates.verify_receipt(
                    audit, receipt, require_current=(number == len(rounds))
                )
                # The gates run BEFORE a round (SKILL). A round whose only passing gate event was journaled after its
                # last start ran ungated -- a receipt produced later (packet rebuilt from a later state) proves nothing
                # about what its critics saw. Re-gating after the fact is fine; the pass before the start must exist.
                # Checked for the LAST round: the report being released must have passed the gates before the
                # round that reviewed it started. An earlier round that ran ungated is covered by a later round
                # gated before its own start (which re-reviews the changed state).
                if number == len(rounds) and not _gated_before_start(audit, number):
                    raise ValueError(
                        f"round {number} (the last round) ran without passing post-delta gates (no gate=post_delta "
                        f"pass was journaled before its last round_start); a receipt made afterwards does not cover "
                        f"what its critics reviewed. If its inputs are unchanged since it ran, re-run its gates "
                        f"(python3 scripts/run_post_delta_gates.py {shlex.quote(str(audit))} --round {number} --report "
                        f"{shlex.quote(str(audit / 'report/REPORT_final.md'))}, which journals the pass), then rerun "
                        f"it with I4R_FORCE_RERUN=1 added to its run_critique.sh command; otherwise integrate it and "
                        f"run another critique round, gating it first")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                missing = not receipt.is_file()
                if not missing:
                    remedy = ""
                elif number == len(rounds):
                    remedy = (f". Run them: python3 scripts/run_post_delta_gates.py \"$AUDIT_DIR\" --round {number} "
                              f"--report \"$AUDIT_DIR/report/REPORT_final.md\" (if round {number} ran before any "
                              f"gate passed, then also rerun it with I4R_FORCE_RERUN=1, or integrate it and run another "
                              f"gated round)")
                else:
                    remedy = (f". An earlier round's receipt cannot be rebuilt once later rounds changed the report: "
                              f"restore critique/round_{number}/POST_DELTA_GATES.json and its post_delta/ snapshot "
                              f"from a backup of this audit; if it never existed (the round ran before any finding "
                              f"was projected), this audit cannot be finalized as is -- move it under old_runs/ and "
                              f"redo the critique rounds in a fresh audit")
                deferred.append(ValueError(
                    f"round {number}: post-delta whole-document gates are not current: {exc}" + remedy))
    # finalize runs this before it writes the requested stopping reason, so it passes it in: checked
    # only from run.json, a wrong review_scope passed finalize and failed at publish.
    # converged means the last round found nothing to correct and was NOT integrated, so the report
    # being released is the one that round reviewed. A release-time edit (a reworded sentence, a new
    # caveat) would otherwise ship as "converged" although no critic saw it.
    reviewed_round, reviewed_sha = last_reviewed_report
    # converged: the last round returned no factual correction and no unresolved blocking item. The
    # operator's logged round_result is not evidence of that; the critics' own outputs are. A correction
    # the coordinator rejects is recorded, with its position, in critique/round_N/CONTESTED.json
    # ({"<role>:<item_id>": {"position": ..., "evidence": ...}}, the shape integrate_round takes).
    # target_complete too, integrated or not: after the last round only projection edits reach the report (the
    # prose check below), so an "applied" correction there is only a note -- it must be contested instead
    stop_now = stopping_reason or run.get("stopping_reason")
    last_unintegrated = bool(reviewed_round) and not (
        audit / f"critique/round_{reviewed_round}/INTEGRATION_SUMMARY.json").is_file()
    if reviewed_round and stop_now in ("converged", "target_complete") and not last_unintegrated:
        # an integration of the last round that recorded a factual correction claims a report change no critic
        # reviewed (and the prose rule forbids): only binding a deferred approval may follow the last round
        summary_path = audit / f"critique/round_{reviewed_round}/INTEGRATION_SUMMARY.json"
        try:
            summary_doc = load(summary_path)
            value = summary_doc.get("factual_corrections", "absent")
        except (ValueError, TypeError, OSError, AttributeError):
            value = None
        if value == "absent":
            # a summary written before the count existed: nothing to check against (the prose and published-field
            # rules still hold the report and registry to what the round reviewed)
            print(f"note: critique/round_{reviewed_round}/INTEGRATION_SUMMARY.json has no factual_corrections count "
                  "(an older integrate_round); its corrections were not checked", file=sys.stderr)
            value = 0
        if not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
            # integrate_round always writes a non-negative integer; anything else is a hand edit or corruption
            raise ValueError(
                f"{stop_now}, but critique/round_{reviewed_round}/INTEGRATION_SUMMARY.json does not record a valid "
                f"factual_corrections count ({value!r}); re-apply that round's integration (integrate_round.py) to "
                "rewrite it")
        last_corrections = value
        recorded_contests = summary_doc.get("contested_sha256") if isinstance(summary_doc, dict) else None
        round_contested = audit / f"critique/round_{reviewed_round}/CONTESTED.json"
        if recorded_contests:
            try:
                current = json.loads(round_contested.read_text(encoding="utf-8")) if round_contested.is_file() else {}
            except ValueError:
                current = None
            if not isinstance(current, dict):
                current = None
            current_hash = hashlib.sha256(json.dumps(current, sort_keys=True).encode("utf-8")).hexdigest() \
                if isinstance(current, dict) else None
            if current is None:
                raise ValueError(
                    f"{stop_now}, but {shlex.quote(str(round_contested))} cannot be read as a JSON object; fix it (the "
                    f"shape is {{\"critic_<role>:<item_id>\": {{\"position\": ..., \"evidence\": ...}}}}), then re-run")
            if current_hash != recorded_contests and not round_contested.is_file():
                raise ValueError(
                    f"{stop_now}, but round {reviewed_round}'s integration applied contests and "
                    f"{shlex.quote(str(round_contested))} is missing (finalize and the release read that file): put "
                    "the contested file that integration was given there (cp <the file passed to --contested> "
                    f"{shlex.quote(str(round_contested))}), then restage")
            if current_hash != recorded_contests:
                raise ValueError(
                    f"{stop_now}, but critique/round_{reviewed_round}/CONTESTED.json is not the set of contests round "
                    f"{reviewed_round}'s integration applied (its INTEGRATION_SUMMARY.json records another), so the "
                    "release would ship contests the notes do not record. Re-apply that integration with the current "
                    f"file:\n  python3 {shlex.quote(str(pathlib.Path(__file__).resolve().parent / 'integrate_round.py'))} "
                    f"{shlex.quote(str(audit))} --round {reviewed_round} apply --decisions <that round's decisions file> "
                    f"--contested {shlex.quote(str(round_contested))}\nor restore the file it was applied with")
        if last_corrections > 0:
            raise ValueError(
                f"{stop_now}, but round {reviewed_round}'s integration recorded {last_corrections} factual correction(s) "
                f"(critique/round_{reviewed_round}/INTEGRATION_SUMMARY.json), and after the last round the report may "
                "not change. Revert the report prose for those corrections and re-apply that integration with the "
                "same decisions minus the applied_notes/rewordings for them, contesting those verdicts instead:\n  "
                f"python3 {shlex.quote(str(pathlib.Path(__file__).resolve().parent / 'integrate_round.py'))} "
                f"{shlex.quote(str(audit))} --round {reviewed_round} apply --decisions <that round's decisions file> "
                f"--contested {shlex.quote(str(audit / f'critique/round_{reviewed_round}/CONTESTED.json'))}\n"
                "or run another critique round on the correction")
    if reviewed_round and stop_now in ("converged", "target_complete"):
        last_dir = audit / f"critique/round_{reviewed_round}"
        contested_path = last_dir / "CONTESTED.json"
        contested = load(contested_path) if contested_path.is_file() else {}
        open_items = []
        known_keys = set()

        def recorded(key: str) -> bool:
            known_keys.add(key)
            entry = contested.get(key)
            return isinstance(entry, dict) and all(
                isinstance(entry.get(k), str) and entry[k].strip() for k in ("position", "evidence"))
        # the hash-verified projection of the last round, not the loose root-level copies (which a failed
        # rerun or a hand edit can change without any check noticing)
        critic_root = last_projection if last_projection is not None else last_dir
        # The targeted adjudication (CRITIQUE.json, result role critic_synth) is the tie-breaker: its
        # CONFIRMED ruling on a finding resolves the critics' conflicting item there, and its own
        # OVERSTATED/REFUTED ruling is an open correction like any critic's.
        # converged needs "no factual correction" (any requested correction counts); target_complete needs
        # no unresolved BLOCKING item and no OVERSTATED/REFUTED verdict left unanswered (SKILL.md), so an optional
        # suggestion on a CONFIRMED item does not hold it up
        strict = stop_now == "converged"
        # A verdict on a finding already withdrawn (status rejected, or EXCLUDED/MERGED) asks nothing of the report,
        # as integrate_round treats it ("not applied: the finding was not included"). Round ids are translated
        # through renumbers recorded after this round ran; an untranslatable legacy renumber skips nothing.
        import later_disputes
        withdrawn_ids: set = set()
        live_rows_path = audit / "work/findings.jsonl"
        if live_rows_path.is_file() and not later_disputes.legacy_renumbered(audit):
            for line in live_rows_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    if str(row.get("status") or "").lower() == "rejected" or (
                            (row.get("ledger_disposition") or {}).get("status") in ("EXCLUDED", "MERGED")):
                        withdrawn_ids.add(row.get("id"))
        round_maps = []
        history_file = audit / "work/id_map_history.jsonl"
        invocation_file = last_dir / "CURRENT_INVOCATION"
        round_invocation = invocation_file.read_text(encoding="utf-8").strip() if invocation_file.is_file() else None
        if history_file.is_file() and round_invocation:
            for line in history_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    ran = entry.get("rounds_run_before_renumber") or {}
                    if isinstance(ran, dict) and ran.get(f"round_{reviewed_round}") == round_invocation:
                        round_maps.append(entry.get("map") or {})

        # "already withdrawn" means withdrawn in the registry the round reviewed too: a finding live at review and
        # withdrawn afterwards changed the release after the last round, so its verdicts still count
        reviewed_withdrawn: set = set()
        if last_reviewed_registry is not None and last_reviewed_registry.is_file():
            for line in last_reviewed_registry.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    if str(row.get("status") or "").lower() == "rejected" or (
                            (row.get("ledger_disposition") or {}).get("status") in ("EXCLUDED", "MERGED")):
                        reviewed_withdrawn.add(later_disputes._translate(row.get("id"), round_maps))

        def withdrawn(item: dict) -> bool:
            finding = later_disputes._translate(item.get("finding_id"), round_maps)
            return finding in withdrawn_ids and finding in reviewed_withdrawn
        adjudicated_confirmed = set()
        adjudication_path = critic_root / "CRITIQUE.json"
        if adjudication_path.is_file():
            adjudication = load(adjudication_path)
            for item in adjudication.get("items") or []:
                known_keys.add(f"critic_synth:{item.get('item_id')}")
                if item.get("verdict") == "CONFIRMED" and not item.get("requested_correction") \
                        and item.get("priority") != "BLOCKING":   # a BLOCKING ruling resolves nothing
                    adjudicated_confirmed.add(item.get("finding_id"))
                elif (item.get("verdict") in ("OVERSTATED", "REFUTED") or item.get("priority") == "BLOCKING"
                      or (strict and item.get("requested_correction"))) and not withdrawn(item) \
                        and not recorded(f"critic_synth:{item.get('item_id')}"):
                    open_items.append(f"critic_synth:{item.get('item_id')} ({item.get('finding_id')}: "
                                      f"{item.get('verdict')}"
                                      + (", BLOCKING" if item.get("priority") == "BLOCKING" else "")
                                      + ", targeted adjudication)")
        for critic_path in sorted(critic_root.glob("critic_*.json")):
            critic = load(critic_path)
            role = critic_path.stem
            for item in critic.get("items") or []:
                key = f"{role}:{item.get('item_id')}"
                if recorded(key):
                    continue
                if item.get("finding_id") in adjudicated_confirmed or withdrawn(item):
                    continue
                if item.get("verdict") in ("OVERSTATED", "REFUTED") or item.get("priority") == "BLOCKING" \
                        or (strict and item.get("requested_correction")):
                    open_items.append(f"{key} ({item.get('finding_id')}: {item.get('verdict')}"
                                      + (", BLOCKING" if item.get("priority") == "BLOCKING" else "") + ")")
            for issue in critic.get("new_issues") or []:
                key = f"{role}:{issue.get('item_id')}"
                if not recorded(key) and issue.get("priority") == "BLOCKING":
                    open_items.append(f"{key} (new BLOCKING issue)")
        if open_items:
            stray = sorted(k for k in contested if k not in known_keys)
            empty = sorted(k for k in contested if k in known_keys and not recorded(k))
            raise ValueError(
                f"{stop_now}, but round {reviewed_round}'s critics asked for corrections nobody resolved"
                + (f" (round {reviewed_round} has no INTEGRATION_SUMMARY.json, so it counts as not integrated)"
                   if stop_now == "target_complete" and last_unintegrated else "") + ": "
                + "; ".join(open_items[:8])
                + (". Integrate them and run another round, or record the " if last_unintegrated or stop_now == "converged"
                   else f". Round {reviewed_round} was integrated after its review, and only projection edits may follow the "
                   "last round: run another critique round on these, or record the ")
                + f"coordinator's position on each in critique/round_{reviewed_round}/CONTESTED.json as "
                '{"critic_<role>:<item_id>": {"position": "...", "evidence": "..."}} (both non-empty)'
                + (f". CONTESTED.json entries that match no item of that round (use the keys above): {stray}"
                   if stray else "")
                + (f". CONTESTED.json entries without a non-empty position and evidence: {empty}" if empty else "")
                + ". A CONTESTED.json written after staging is not in the staged release: stage again into a fresh "
                "directory (stage_release.sh), re-log delivery (`bash scripts/log_event.sh delivery artifact_path=report/<output-name>.pdf recipient=...`) and then phase_end phase=release, then re-run finalize")
        # The contest record justifies the converged stop, so the staged release must carry the same file.
        # stage_release.sh records the hashes of the CONTESTED.json files it staged (no paths); no record means
        # nothing proves the release carries it (stage_release.sh runs before finalize).
        staged_record = audit / "work/RELEASE_STAGED.json"
        if contested_path.is_file():
            relative = str(contested_path.relative_to(audit))
            staged_hash = ((load(staged_record) if staged_record.is_file() else {}).get("contested") or {}).get(relative)
            if not staged_record.is_file():
                deferred.append(ValueError(   # a staging freshness error: reported after the content rules
                    f"{stop_now} rests on {relative}, but there is no staging record (work/RELEASE_STAGED.json, written "
                    "by this version's stage_release.sh), so nothing shows the release carries it. Stage into a fresh "
                    "directory (stage_release.sh), re-log delivery (`bash scripts/log_event.sh delivery artifact_path=report/<output-name>.pdf recipient=...`) and then phase_end phase=release, then re-run finalize"))
            elif staged_hash != hashlib.sha256(contested_path.read_bytes()).hexdigest():
                deferred.append(ValueError(
                    f"{stop_now} rests on {relative}, but the last staged release "
                    f"{'carries a different copy' if staged_hash else 'does not carry it'} (it was written or "
                    "changed after staging). Stage again into a fresh directory (stage_release.sh), re-log "
                    "delivery (`bash scripts/log_event.sh delivery artifact_path=report/<output-name>.pdf recipient=...`) and then "
                    "phase_end phase=release, then re-run finalize"))
    # SETTLED.json must not rest on a reviewer that disputed the fact in a later, never-integrated round
    # (make_settled excludes such facts; a SETTLED.json built before that round still lists them).
    settled_path = audit / "SETTLED.json"
    if settled_path.is_file():
        import later_disputes
        disputes = later_disputes.later_disputes(audit)
        invocation_round_of = later_disputes.invocation_rounds(audit)   # not "rounds": that is the run's round list
        stale = []
        for fact in load(settled_path).get("facts") or []:
            who = later_disputes.stale_confirmations(audit, fact.get("id"), fact.get("confirmations") or [],
                                                     disputes, invocation_round_of)
            if who:
                stale.append(f"{fact.get('id')} ({', '.join(who)})")
        if stale:
            raise ValueError(
                "SETTLED.json lists facts whose confirming reviewers later called them OVERSTATED/REFUTED in a round "
                f"that was not integrated: {stale}. Re-run make_settled.sh (it excludes them), re-log the settled "
                "event with the new entries count, rebuild the latest diff packet and post-delta gates, stage again "
                "into a fresh directory (stage_release.sh) and re-log delivery (`bash scripts/log_event.sh delivery artifact_path=report/<output-name>.pdf recipient=...`) and then phase_end phase=release, then re-run finalize")
        # the staged release must carry the settled cache finalize accepts
        staged_record = audit / "work/RELEASE_STAGED.json"
        if staged_record.is_file():
            staged_settled = load(staged_record).get("settled") or {}
            changed = [name for name in ("SETTLED.json", "SETTLED.md")
                       if (audit / name).is_file() and staged_settled.get(name) != digest(audit / name)]
            if changed:
                deferred.append(ValueError(
                    f"{', '.join(changed)} changed after the release was staged, so the staged copy is stale. Stage "
                    "again into a fresh directory (stage_release.sh), re-log delivery (`bash scripts/log_event.sh delivery artifact_path=report/<output-name>.pdf recipient=...`) and then phase_end phase=release, then re-run finalize"))
    # An INCLUDED finding that was never approved is in the report (its marker stays) but not in the published
    # comment.json: a stop that claims the review is complete must not ship that split silently.
    if (stopping_reason or run.get("stopping_reason")) in {"target_complete", "converged"}:
        live_registry = audit / "work/findings.jsonl"
        if live_registry.is_file():
            pending = [row.get("id") for row in (json.loads(line) for line in
                                                 live_registry.read_text(encoding="utf-8").splitlines() if line.strip())
                       if (row.get("ledger_disposition") or {}).get("status") == "INCLUDED"
                       and (row.get("coordinator_approved") is not True
                            or str(row.get("status") or "").lower() not in {"confirmed", "verified", "accepted"})]
            if pending:
                raise ValueError(
                    f"{stopping_reason or run.get('stopping_reason')}, but INCLUDED findings {pending} are not both "
                    "final (status confirmed/verified/accepted) and coordinator_approved, so the report carries them "
                    "and the published comment.json (generate_finding_artifacts' rule) would not. Approve each "
                    "after a round that confirms it (the confirming records bind at that integration), or withdraw "
                    "it (status rejected, EXCLUDED, remove its marker, renumber -- a report change, so it needs another critique round); at the round cap, stop with "
                    "cap_with_unresolved and list them as unresolved"
                    + (". For converged, approving means integrating the final round, which changes the report "
                       "and so needs one more critique round; target_complete accepts a final round integrated only "
                       "to bind approvals" if (stopping_reason or run.get("stopping_reason")) == "converged" else ""))
    # Every published claim must be one the last round's critics saw. A claim reworded at release (or
    # at the last integration) and re-packeted passed the post-delta gates and the union check, which
    # read the packets rounds were dispatched with, so target_complete shipped unreviewed wording.
    if ((stopping_reason or run.get("stopping_reason")) in {"target_complete", "converged"}
            and last_reviewed_registry is not None and last_reviewed_registry.is_file()):
        snapshot_rows = [json.loads(line) for line in
                         last_reviewed_registry.read_text(encoding="utf-8").splitlines() if line.strip()]
        # Per finding: the text a critic reviewed AS THIS FINDING's claim. Snapshot ids are translated
        # through renumbers recorded after that invocation ran; with only a legacy map (no history) the
        # check falls back to "some reviewed row had this text".
        history_path = audit / "work/id_map_history.jsonl"
        history = ([json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                   if history_path.is_file() else None)
        def _moved(path: pathlib.Path) -> bool:
            try:
                return any(k != v for k, v in json.loads(path.read_text(encoding="utf-8")).items())
            except (OSError, ValueError, AttributeError):
                return path.is_file()
        # renumbered without a history (2.6.3 kept only work/id_map.json): ids cannot be translated exactly
        import later_disputes
        legacy = later_disputes.legacy_renumbered(audit)   # one rule for every tool (F<n> moves only)
        pointer = audit / f"critique/round_{reviewed_round}/CURRENT_INVOCATION"
        invocation = pointer.read_text(encoding="utf-8").strip() if pointer.is_file() else None
        reviewed_by_id: dict[Any, Any] = {row.get("id"): row.get("claim") for row in snapshot_rows}
        reviewed_row_by_id: dict[Any, Any] = {row.get("id"): row for row in snapshot_rows}
        for entry in (history or []):
            ran = entry.get("rounds_run_before_renumber") or {}
            key = f"round_{reviewed_round}"
            if key in ran and not (isinstance(ran, dict) and invocation and ran[key] != invocation):
                mapping = entry.get("map") or {}
                reviewed_by_id = {mapping.get(k, k): v for k, v in reviewed_by_id.items()}
                reviewed_row_by_id = {mapping.get(k, k): v for k, v in reviewed_row_by_id.items()}
        # legacy matching by claim text uses the rows the round reviewed as INCLUDED only: a withdrawn or merged row's
        # text is not a published claim that was reviewed
        reviewed_included_rows = [row for row in snapshot_rows
                                  if (row.get("ledger_disposition") or {}).get("status") == "INCLUDED"]
        reviewed_texts = {row.get("claim") for row in reviewed_included_rows}
        live_path = audit / "work/findings.jsonl"
        unreviewed = []
        changed_fields: list = []
        live_included: set = set()
        # legacy renumber (ids untranslatable): pair a live row with the reviewed row of the identical, unique claim
        text_counts: dict = {}
        for snap in reviewed_included_rows:
            text_counts[snap.get("claim")] = text_counts.get(snap.get("claim"), 0) + 1
        reviewed_by_text = {snap.get("claim"): snap for snap in reviewed_included_rows
                            if text_counts[snap.get("claim")] == 1}
        live_included_texts: list = []
        unpaired: list = []
        for line in (live_path.read_text(encoding="utf-8").splitlines() if live_path.is_file() else []):
            if not line.strip():
                continue
            row = json.loads(line)
            if (row.get("ledger_disposition") or {}).get("status") != "INCLUDED":
                continue
            live_included.add(row.get("id"))
            live_included_texts.append(row.get("claim"))
            seen = row.get("claim") in reviewed_texts if legacy else reviewed_by_id.get(row.get("id")) == row.get("claim")
            if not seen:
                unreviewed.append(str(row.get("id")))
                continue
            # the other fields Appendix A and comment.json publish: after the last round only a deferred approval
            # (status, coordinator_approved, settled, bound verifications) may change a reviewed finding
            reviewed_row = (reviewed_by_text.get(row.get("claim")) if legacy else reviewed_row_by_id.get(row.get("id")))
            if reviewed_row is None:
                unpaired.append(str(row.get("id")))   # legacy with a duplicated INCLUDED claim text
                continue
            moved = changed_after_review(reviewed_row, row)
            if moved:
                changed_fields.append(f"{row.get('id')} ({', '.join(moved)})")
        # the published set itself: a finding INCLUDED at review and withdrawn since, or brought back, changed the
        # release after the last round (in a report without markers nothing else notices)
        if legacy:
            # ids cannot be translated: the published set is compared as a multiset of claim texts
            reviewed_multiset = sorted(str(r.get("claim")) for r in reviewed_included_rows)
            live_multiset = sorted(str(t) for t in live_included_texts)
            if reviewed_multiset != live_multiset:
                gone = [t for t in reviewed_multiset if reviewed_multiset.count(t) > live_multiset.count(t)]
                new_texts = [t for t in live_multiset if live_multiset.count(t) > reviewed_multiset.count(t)]
                raise ValueError(
                    f"{stopping_reason or run.get('stopping_reason')}, but the INCLUDED findings differ from the ones round "
                    f"{reviewed_round} reviewed (its snapshot registry; compared by claim text, as this audit was "
                    "renumbered without a history)"
                    + (f"; included or reworded since: {sorted(set(new_texts))[:3]}" if new_texts else "")
                    + (f"; withdrawn or reworded since: {sorted(set(gone))[:3]}" if gone else "")
                    + ". That needs another critique round (at the round cap, cap_with_unresolved with it listed)")
            if unpaired:
                print(f"note: {', '.join(unpaired)} share a claim text with another INCLUDED finding, so their other "
                      "published fields could not be compared with the reviewed rows (legacy renumber, no history)",
                      file=sys.stderr)
        if not legacy:
            reviewed_included = {fid for fid, snap in reviewed_row_by_id.items()
                                 if (snap.get("ledger_disposition") or {}).get("status") == "INCLUDED"}
            added, removed = sorted(live_included - reviewed_included, key=str), sorted(reviewed_included - live_included, key=str)
            if added or removed:
                raise ValueError(
                    f"{stopping_reason or run.get('stopping_reason')}, but the INCLUDED findings differ from the ones round "
                    f"{reviewed_round} reviewed (its snapshot registry)"
                    + (f"; included since: {added}" if added else "") + (f"; withdrawn since: {removed}" if removed else "")
                    + ". Including or withdrawing a finding after the last round needs another critique round (at the "
                    "round cap, cap_with_unresolved with it listed as unresolved)")
        if changed_fields:
            raise ValueError(
                f"{stopping_reason or run.get('stopping_reason')}, but published fields of {'; '.join(changed_fields)} "
                f"differ from what round {reviewed_round} reviewed (its snapshot registry). After the last round a finding may "
                "change only by binding a deferred approval (status, approval, bound verifications, and fields it "
                "supplies that were empty at review: report_section, verification_snippet): "
                "revert the change, or run another critique round on it (at the round cap, cap_with_unresolved with it "
                "listed as unresolved)")
        if unreviewed:
            raise ValueError(
                f"{stopping_reason or run.get('stopping_reason')}, but the claim text of {', '.join(unreviewed)} is not "
                f"the text round {reviewed_round} reviewed (its snapshot registry). A changed claim needs another "
                "critique round (or, at the round cap, cap_with_unresolved with it listed as unresolved)")
    if (stopping_reason or run.get("stopping_reason")) == "converged" and reviewed_sha:
        if digest(audit / "report/REPORT_final.md") != reviewed_sha:
            raise ValueError(
                f"converged, but report/REPORT_final.md differs from the report round {reviewed_round} reviewed "
                f"(critique/round_{reviewed_round}/snapshots/.../source/REPORT_TO_REVIEW.md). Revert the "
                "release-time edit, or run another critique round on it (at the round cap, cap_with_unresolved "
                "with the section in review_scope's unreviewed_sections is the remaining honest stop)")
    # target_complete may carry projection changes made after the last round (binding a deferred approval adds its
    # Appendix A row and claim tag; a renumber moves marker ids) but no prose the critics did not review
    if (stopping_reason or run.get("stopping_reason")) == "target_complete" and last_reviewed_report_path is not None \
            and last_reviewed_report_path.is_file():
        def prose(text: str) -> list:
            text = re.sub(r"(?ms)^<!-- I4R-GENERATED-CLAIM-TAGS:START -->.*?^<!-- I4R-GENERATED-CLAIM-TAGS:END -->\s*",
                          "", text)
            text = re.sub(r"\s*\[claim\s+[1-9][0-9]*\]", "", text, flags=re.I)
            start = re.search(r"(?m)^##\s+Appendix\s+A\b.*$", text, re.I)
            if start:
                following = re.search(r"(?m)^##\s+", text[start.end():])
                text = text[:start.start()] + (text[start.end() + following.start():] if following else "")
            text = re.sub(r"<!--\s*(finding|tested-negative|merged):[^\s>]+(?:->[^\s>]+)?\s*-->", r"<!-- \1 -->", text)
            # whitespace inside a line does not render, so it is not an edit
            return [" ".join(line.split()) for line in text.splitlines() if line.strip()]
        reviewed_text = last_reviewed_report_path.read_text(encoding="utf-8")
        final_text = (audit / "report/REPORT_final.md").read_text(encoding="utf-8")
        reviewed_lines, final_lines = prose(reviewed_text), prose(final_text)
        if reviewed_lines != final_lines:
            def marker_kinds(lines: list) -> list:   # outside the generated blocks, as prose() masks them
                return sorted(re.findall(r"<!-- (finding|tested-negative|merged) -->", "\n".join(lines)))
            if marker_kinds(reviewed_lines) != marker_kinds(final_lines):
                raise ValueError(
                    f"target_complete, but report/REPORT_final.md's finding markers differ in kind or number from the "
                    f"report round {reviewed_round}'s critics reviewed (reviewed: {marker_kinds(reviewed_lines)}, now: "
                    f"{marker_kinds(final_lines)}); only marker ids may change (a renumber). Adding, removing or "
                    "re-kinding a marker (a withdrawal, a merge, a new finding) is a report change: revert it, or run "
                    "another critique round (at the round cap, cap_with_unresolved)")
            added = [line for line in final_lines if line not in reviewed_lines][:3]
            removed = [line for line in reviewed_lines if line not in final_lines][:3]
            examples = [f"added: {line}" for line in added] + [f"removed: {line}" for line in removed]
            if not examples:   # a moved or duplicated line
                first = next((f"{a!r} -> {b!r}" for a, b in zip(reviewed_lines, final_lines) if a != b), "a line count change")
                examples = [f"first difference: {first}"]
            raise ValueError(
                f"target_complete, but report/REPORT_final.md has prose that round {reviewed_round}'s critics did not "
                f"review (outside the generated Appendix A, claim tags and finding marker ids), e.g. {examples}. Revert "
                "it, or run another critique round on it (at the round cap, cap_with_unresolved with the section in "
                "review_scope's unreviewed_sections)")
    if (stopping_reason or run.get("stopping_reason")) in {"target_complete", "converged"}:
        if run.get("changed_claim_ids") != rereviewed_change_ids:
            raise ValueError(
                "run changed_claim_ids is not the exact ordered union of generated later-round "
                "diffs.\n"
                "  The expected list is read from each round's SNAPSHOT-bound packet\n"
                "  (critique/round_N/snapshots/<input_sha>/source/context/DIFF_MANIFEST.json),\n"
                "  which is what that round was actually dispatched with. Log a review_scope event\n"
                "  carrying exactly those ids, in that order.\n"
                "  Note that the round-N packet describes the round-(N-1) INTEGRATION delta: it is\n"
                "  built from round N-1's preserved before-files, not from round N's own snapshots.\n"
                "  Regenerating it from the wrong pair changes the claim set and lands here.\n"
                f"  expected: {rereviewed_change_ids}\n  logged:   {run.get('changed_claim_ids')}"
            )
        if set(run.get("rereviewed_claim_ids") or []) != set(rereviewed_change_ids):
            raise ValueError(
                "run rereviewed_claim_ids is not exactly joined to assigned completed claim coverage: "
                f"expected the set {sorted(set(rereviewed_change_ids))}, logged {run.get('rereviewed_claim_ids')}"
            )
    return len(rounds)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument(
        "--require-post-delta-gates",
        action="store_true",
        help="require current passing whole-document gate receipts for rounds 2+",
    )
    parser.add_argument("--stopping-reason", help="the stopping reason finalize is about to record")
    args = parser.parse_args()
    try:
        return verify(
            args.audit_dir,
            require_post_delta_gates=args.require_post_delta_gates,
            stopping_reason=args.stopping_reason,
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
