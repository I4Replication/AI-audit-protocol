"""Every way the critique validators reject a response must be either stated or unemittable.

Nine separate round-killing defects on audit A38 shared one shape: a rule was
enforced in one place and expressed in another, or not at all. The worst two --
``new_issues[].finding_id`` and the targeted adjudicator's evidence-inventory path -- each cost a
full round, and the adjudicator one cost it twice.

The guard that was supposed to catch this class checked three of the roughly sixty rejection
messages, against one prompt, and did not know the targeted-adjudicator prompt existed.

This test enumerates every rejection message the two critique validators can emit and requires
each to be CLASSIFIED. A message is either:

  STATED          -- a model can violate it while emitting schema-valid JSON, so the contract must
                     say so; the mapped phrase has to appear in the rendered contract for BOTH
                     roles, because both are graded by the same validator.
  SCHEMA_ENFORCED -- the provider schema makes it unemittable (const, enum, type, or a bound the
                     runtime narrows), so prose would be noise.

Adding a rejection to either validator without classifying it here fails this test. That is the
point: it forces the author to decide whether a model can trip the new rule, rather than finding
out when a round dies.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

ROLE_INVENTORIES = {
    "critic": "source/context/EVIDENCE_HASHES.json",
    "targeted_adjudicator": "source/EVIDENCE_HASHES.json",
}

# message substring -> phrase that must appear in the rendered contract
STATED = {
    "conflicts may reference only covered claim IDs": "conflicts[].claim_ids",
    "skipped checks are unsupported and cannot count as completed review": "skipped_checks",
    "a check may not be both completed and skipped": "skipped_checks",
    "a check cannot be both completed and skipped": "skipped_checks",
    "duplicate skipped check": "skipped_checks",
    "assessment finding_id must be declared in covered_claim_ids": "covered_claim_id",
    "each covered claim must have exactly one claim-level assessment": "covered_claim_id",
    "complete output must account for every assigned check": "assigned check",
    "complete output must account for every assigned check exactly": "assigned check",
    "complete output requires at least one completed check": "assigned check",
    "assigned_checks must be nonempty; a skeleton is not complete": "assigned check",
    "completed and skipped checks must be assigned": "assigned check",
    "duplicate item ID": "item_id",
    "duplicate item_id across items and new_issues": "item_id",
    "duplicate conflict ID": "conflict",
    "duplicate conflict_id": "conflict",
    "overall.blocking_count differs from assessments": "blocking_count",
    "overall.publishable must be false exactly when blocking items exist": "publishable",
    "must be false exactly when blocking items exist": "publishable",
    "complete output must declare every assessment evidenced": "evidenced",
    "complete output cannot declare unresolved omissions": "complete",
    "complete output lacks completion_marker=true": "completion_marker",
    "covered_claim_ids contains duplicates": "covered_claim_id",
    ".path is not in the allowed frozen evidence inventory:": "frozen evidence inventory",
    ".sha256 is not lowercase SHA-256": "sha256",
    "does not match the frozen artifact hash:": "byte-for-byte",
    "must be a non-empty string": "non-empty string",
    "expected non-empty string": "non-empty string",
    "must be a string of at least  non-space characters": "20 characters",
    "expected lowercase SHA-256 hex": "sha256",
    "duplicate artifact reference": "no duplicate artifact reference",
    "duplicate value": "Every array is a set",
    "must be null unless verdict is UNVERIFIABLE": "object only for UNVERIFIABLE",
    "new issues must use null finding_id": "new_issues[].finding_id",
    ".finding_id must be null for a new issue": "new_issues[].finding_id",
    ".finding_id is not declared in covered_claim_ids": "covered_claim_id",
    "references a claim not declared as covered": "covered_claim_id",
    ".claim_ids contains duplicates": "conflicts[].claim_ids",
    ".claim_ids must be nonempty": "conflicts[].claim_ids",
    ".evidence.artifact_refs must be nonempty": "artifact_refs",
    ".evidence.reproducible_script.sha256 is not lowercase SHA-256": "sha256",
    "input_hashes. is not lowercase SHA-256": "sha256",
    ".unverifiable_basis must be null": "unverifiable_basis",
}

# A model cannot influence these: they are assertions about the harness's own frozen inputs.
RUNTIME_INVARIANT = {
    "critic evidence inventory must be an object",
    "snapshot manifest lacks a file inventory",
    "critic evidence-manifest hash does not bind the immutable snapshot",
    # A wrapper prefix the driver adds around a nested failure, not a rule of its own.
    "canonical critic semantic error:",
    # Snapshot and filesystem integrity, surfaced by the call-graph closure (2.4.8): nothing a
    # model writes can trigger these; they describe the frozen snapshot or the host.
    "contains a symlink component:", "could not locate audit root for critic result:",
    # 2.6.8 fix A: these name the snapshot they are about ("... in round snapshot <rel>").
    "immutable file hash differs:  in", "immutable file inventory differs:",
    "is missing or is not a regular file:",
    "result input hash does not identify exactly one immutable snapshot:",
    "snapshot does not contain exactly one evidence inventory",
    "symlink inside immutable snapshot:  in",
    "immutable path regained write permission: \n  A snapshot is frozen before it is published, "
    "so this means something changed its mode afterwards -- a synced folder re-applying "
    "permissions is the usual cause.\n  Remedy: chmod -R a-w the path above, or delete it (it is "
    "content-addressed and will be rebuilt identically).",
    # 2.6.8: SnapshotIntegrityError (exit 5). A manifest the harness wrote, and a mode that a
    # re-freeze could not make stick, are host faults a model cannot reach.
    "corrupt snapshot manifest: \n  MANIFEST.json is not an object.",
    "corrupt snapshot manifest: \n  MANIFEST.json is unreadable ().",
    "corrupt snapshot: \n  The directory exists but has no MANIFEST.json. An empty directory "
    "resurrected by a synced folder looks exactly like this.",
    # 2.6.8 fix A: SnapshotModeUnstableError, after bounded re-freezes; the content verified.
    ": : content verified against its manifest, but its modes keep changing:  path(s) regained a "
    "write bit after  re-freezes.\n  Remedy: stop the sync client, or move the audit off the "
    "synced folder, then rerun; deleting the snapshot does not help while something keeps "
    "changing its modes.",
}

# The provider schema makes these unemittable: identity fields are const-bound, statuses and roles
# are enums, types and array shapes are pinned, and the runtime narrows the id enums per role.
SCHEMA_ENFORCED = {
    "critic_result.completeness_declaration", "critic_result.completion_marker",
    "critic_result.coverage", "critic_result.coverage.assigned_checks",
    "critic_result.coverage.completed_checks", "critic_result.coverage.skipped_checks",
    "critic_result.input_hashes.report_sha256", "critic_result.invocation_id",
    "critic_result.items", "critic_result.overall.blocking_count",
    "critic_result.overall.publishable", "critic_result.role", "critic_result.schema_version",
    "critic_result.status", "expected boolean", "must be a bounded machine-safe identifier",
    "must equal 2.0.0", "must equal report_sha256", "must not be empty",
    "unsupported critic role", "unsupported status", "must be true exactly when status is complete",
    "must declare complete exactly when status is complete", "duplicate settled fact spot-check",
    "audit id mismatch", "conflicts must be an array", "coverage arrays are malformed",
    "covered_claim_ids must be an array", "input_hashes.report_sha256 mismatch",
    "input_hashes.snapshot_sha256 mismatch", "overall.blocking_count must be an integer",
    "overall.publishable must be boolean", "report hash mismatch", "role or round mismatch",
    "settled_spot_checks must be an array", "unsupported settled spot-check status",
    "v2 output status is not complete",
    # constrain_evidence_bindings pins artifact_binding.path to the inventory enum, so a
    # traversal or non-relative path can no longer be emitted at all.
    "must be a nonempty relative path",
    "must be a normalized relative path without traversal",
    # Path labels and enum/type violations: the provider schema pins the shape, and the runtime
    # narrows item_id/finding_id to per-role enums.
    "critic_result.[].finding_id", "critic_result.[].item_id",
    "critic_result.completeness_declaration.",
    "critic_result.settled_spot_checks[].fact_id",
    "critic_result.settled_spot_checks[].status",
    "unsupported confidence in", "unsupported verdict or priority in",
    "must be an array", "coverage. contains duplicates",
    # Surfaced when the guard started walking validate_v2's helpers (2.4.8): type and enum
    # rejections the provider schema pins, plus the path labels those helpers format.
    "expected array", "expected integer", "expected object",
    "unsupported confidence", "unsupported priority", "unsupported verdict",
    "artifact binding permits only path and sha256",
    "must be workspace-relative and may not traverse upward",
    ".artifact_refs", ".artifact_refs[]", ".unverifiable_basis",
}


# Rejections do not all live in the entry points. `verify_evidence_bindings` is where the message
# that killed round 2 of audit A38 is raised, and `nonempty`/`exact_keys` carry
# eighteen and thirteen more. An extractor that scanned only the two entry points would have
# missed the very defect this file exists to prevent, so the helpers are scanned as well.
REJECTION_HELPERS = ("verify_evidence_bindings", "safe_relative_path", "nonempty", "exact_keys")


def reachable_functions(tree: ast.Module, roots: tuple[str, ...]) -> set[str]:
    """Every module-level function reachable from `roots` through same-module calls.

    The four-name REJECTION_HELPERS tuple this replaces missed 26 reachable rejections, among
    them a live enforced-but-unstated rule (`duplicate artifact reference`). A hard-coded list of
    helpers is exactly the kind of rule-in-one-place this test exists to forbid.
    """
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    seen: set[str] = set()
    frontier = [name for name in roots if name in functions]
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        for sub in ast.walk(functions[name]):
            if isinstance(sub, ast.Call):
                callee = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
                if callee in functions and callee not in seen:
                    frontier.append(callee)
    return seen


def rejection_messages(path: pathlib.Path, functions: tuple[str, ...]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = tuple(reachable_functions(tree, functions))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in functions):
            continue
        for sub in ast.walk(node):
            call = sub.exc if isinstance(sub, ast.Raise) and isinstance(sub.exc, ast.Call) else sub
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            # SnapshotIntegrityError (2.6.8) is the ValueError the snapshot checks raise so the
            # driver can tell a host fault from a schema-invalid response; it is still a rejection.
            if name not in ("_fail", "ValueError", "SnapshotIntegrityError",
                            "SnapshotModeUnstableError"):
                continue
            for argument in call.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str) \
                        and len(argument.value) > 12:
                    found.add(argument.value)
                # Most helper messages are f-strings; keep their literal spine.
                if isinstance(argument, ast.JoinedStr):
                    literal = "".join(
                        part.value for part in argument.values
                        if isinstance(part, ast.Constant)
                    ).strip()
                    if len(literal) > 12:
                        found.add(literal)
    return found


def rendered_contract(inventory_path: str) -> str:
    text = (SKILL / "prompts/contract.txt").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.split("\n") if not line.startswith("#"))
    return " ".join(body.replace("{{INVENTORY}}", inventory_path).split())


class EveryRejectionIsClassified(unittest.TestCase):
    def all_messages(self) -> set[str]:
        return (
            rejection_messages(
                SKILL / "schemas/tools/validate_v2.py", ("validate_critic_result",))
            | rejection_messages(
                SKILL / "scripts/test_run_critique_runtime.py",
                ("validate_critic_obj", "validate_adjudication_obj") + REJECTION_HELPERS)
        )

    def test_no_rejection_is_unclassified(self) -> None:
        unclassified = sorted(
            message for message in self.all_messages()
            if message not in STATED and message not in SCHEMA_ENFORCED
            and message not in RUNTIME_INVARIANT
        )
        self.assertEqual(
            unclassified, [],
            "These validator rejections are neither stated in prompts/contract.txt nor made "
            "unemittable by the provider schema. Classify each one: add it to STATED with the "
            "contract phrase that covers it (and put that phrase in the contract), or to "
            "SCHEMA_ENFORCED if the schema genuinely prevents it.",
        )

    def test_every_stated_rule_reaches_both_roles(self) -> None:
        # The adjudicator is graded by validate_critic_obj exactly as critics are, so a rule that
        # only reaches the critic prompt is a rule the adjudicator can be killed by unseen.
        live = self.all_messages()
        for message, phrase in sorted(STATED.items()):
            if message not in live:
                continue
            for role, inventory in ROLE_INVENTORIES.items():
                with self.subTest(role=role, rule=message[:48]):
                    self.assertIn(phrase.lower(), rendered_contract(inventory).lower())

    def test_the_classification_does_not_rot(self) -> None:
        # A message removed from the validators should be removed here too, or the table slowly
        # becomes a list of rules that no longer exist and stops meaning anything.
        live = self.all_messages()
        stale = (sorted(set(STATED) - live) + sorted(SCHEMA_ENFORCED - live)
                 + sorted(RUNTIME_INVARIANT - live))
        self.assertEqual(stale, [], "classified messages no longer emitted by either validator")


class BothRolesGetTheirOwnInventoryPath(unittest.TestCase):
    def test_each_role_is_told_the_path_that_exists_in_its_snapshot(self) -> None:
        for role, inventory in ROLE_INVENTORIES.items():
            with self.subTest(role=role):
                self.assertIn(inventory, rendered_contract(inventory))

    def test_the_contract_is_the_single_source(self) -> None:
        # The contract used to live inside common.txt, which is rendered only for critics. If it
        # moves back, the adjudicator silently stops being told any of this again.
        common = (SKILL / "prompts/common.txt").read_text(encoding="utf-8")
        self.assertNotIn("new_issues[].finding_id", common)
        self.assertIn("{{INVENTORY}}", (SKILL / "prompts/contract.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
