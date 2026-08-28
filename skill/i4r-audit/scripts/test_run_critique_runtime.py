#!/usr/bin/env python3
"""Runtime primitives for run_critique.sh.

The production driver stays Bash 3.2 compatible; JSON, hashing, schemas, and atomic metadata live
here so they have one deterministic implementation that the fake-Codex test suite can exercise.
Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
from typing import Any


ROLES = ("stats", "data", "fairness", "completeness")
VERDICTS = ("CONFIRMED", "REFUTED", "OVERSTATED", "UNVERIFIABLE")
PRIORITIES = ("BLOCKING", "NON_BLOCKING")
CONFIDENCE = ("CONFIRMED", "LIKELY", "SPECULATIVE")
SKILL_ROOT = pathlib.Path(__file__).resolve().parent.parent
CRITIC_SCHEMA = SKILL_ROOT / "schemas/v2/critic-result.schema.json"
SCHEMA_VALIDATOR = SKILL_ROOT / "scripts/validate_json_schema.py"
SEMANTIC_VALIDATOR = SKILL_ROOT / "schemas/tools/validate_v2.py"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
sys.path.insert(0, str(SKILL_ROOT / "schemas/tools"))
import validate_json_schema  # type: ignore  # local, dependency-free runtime module
import validate_v2  # type: ignore  # local, dependency-free runtime module


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path: os.PathLike[str] | str) -> Any:
    return json.loads(pathlib.Path(path).read_text())


def atomic_json(path: os.PathLike[str] | str, obj: Any) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(f"{path}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def sha_file(path: os.PathLike[str] | str) -> str:
    h = hashlib.sha256()
    with pathlib.Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_policy_snapshot(path: pathlib.Path, workspace: pathlib.Path) -> dict[str, Any]:
    """Validate the immutable, audit-scoped policy envelope and its optional run binding."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"immutable audit-scoped policy snapshot is missing: {path}")
    envelope = validate_json_schema.strict_loads(path.read_text(encoding="utf-8"), source=str(path))
    if not isinstance(envelope, dict) or set(envelope) != {
            "snapshot_version", "policy_sha256", "policy"}:
        raise ValueError("pipeline policy snapshot envelope has unexpected fields")
    if envelope["snapshot_version"] != "1" or not isinstance(envelope["policy"], dict):
        raise ValueError("unsupported pipeline policy snapshot envelope")
    canonical = json.dumps(
        envelope["policy"], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    if envelope["policy_sha256"] != hashlib.sha256(canonical).hexdigest():
        raise ValueError("pipeline policy snapshot canonical hash is invalid")
    run_path = workspace / "run.json"
    if run_path.is_file():
        run = validate_json_schema.strict_loads(run_path.read_text(encoding="utf-8"),
                                                source=str(run_path))
        if isinstance(run, dict) and run.get("schema_version") == "2.0.0" and not run.get("normalization"):
            expected = (run.get("toolkit") or {}).get("pipeline_policy_snapshot_sha256")
            if expected != sha_file(path):
                raise ValueError("run.json does not bind the exact audit-scoped policy snapshot")
    return envelope


def configured_model_route(envelope: dict[str, Any], route: str) -> tuple[str, str]:
    """Return the exact model/effort pair for a critique runtime route."""
    try:
        config = envelope["policy"]["models"][route]
        model = config["requested_model"]
        effort = config["reasoning_effort"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"pipeline policy lacks models.{route} routing") from exc
    if not isinstance(model, str) or not model or not isinstance(effort, str) or not effort:
        raise ValueError(f"pipeline policy models.{route} routing is malformed")
    return model, effort


def hash_tree(root: pathlib.Path, extra: list[str]) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(path.relative_to(root).as_posix().encode() + b"\0")
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        h.update(b"\0")
    for value in extra:
        h.update(value.encode() + b"\0")
    return h.hexdigest()


def make_read_only(root: pathlib.Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def discard_tree(root: pathlib.Path) -> None:
    if not root.exists():
        return
    for path in [root, *root.rglob("*")]:
        try:
            path.chmod(0o700 if path.is_dir() else 0o600)
        except OSError:
            pass
    shutil.rmtree(root, ignore_errors=True)


def verify_read_only_inventory(root: pathlib.Path, manifest: dict[str, Any]) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            raise ValueError(f"symlink inside immutable snapshot: {path}")
    hashes = manifest.get("files_sha256")
    actual_files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path != root / "MANIFEST.json"
    )
    actual_names = {path.relative_to(root).as_posix() for path in actual_files}
    if not isinstance(hashes, dict) or set(hashes) != actual_names:
        raise ValueError(f"immutable file inventory differs: {root}")
    for path in actual_files:
        rel = path.relative_to(root).as_posix()
        if hashes[rel] != sha_file(path):
            raise ValueError(f"immutable file hash differs: {rel}")
    for path in [root, *root.rglob("*")]:
        if path.stat().st_mode & 0o222:
            raise ValueError(f"immutable path regained write permission: {path}")


def safe_relative_path(value: Any, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty relative path")
    normalized = value.replace("\\", "/")
    path = pathlib.PurePosixPath(normalized)
    if (path.is_absolute() or re.match(r"^[A-Za-z]:/", normalized) or
            any(part in ("", ".", "..") for part in path.parts)):
        raise ValueError(f"{label} must be a normalized relative path without traversal")
    return path


def regular_without_symlinks(root: pathlib.Path, relative: Any, label: str) -> pathlib.Path:
    """Resolve a workspace-relative regular file while rejecting every symlink component."""
    rel = safe_relative_path(relative, label)
    candidate = root
    for part in rel.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError(f"{label} contains a symlink component: {relative}")
    if not candidate.is_file():
        raise ValueError(f"{label} is missing or is not a regular file: {relative}")
    return candidate


def snapshot_for_result(result_path: pathlib.Path, round_number: int,
                        input_sha: str) -> pathlib.Path:
    """Find the exact immutable regular or targeted snapshot bound by a result path."""
    resolved = result_path.resolve()
    audit_root = None
    for parent in (resolved.parent, *resolved.parents):
        if parent.name == "critique":
            audit_root = parent.parent
            break
    if audit_root is None:
        raise ValueError(f"could not locate audit root for critic result: {result_path}")
    round_root = audit_root / f"critique/round_{round_number}"
    candidates = [
        round_root / "snapshots" / input_sha,
        round_root / "target_snapshots" / input_sha,
    ]
    found = [path for path in candidates if path.is_dir() and not path.is_symlink()]
    if len(found) != 1:
        raise ValueError(f"result input hash does not identify exactly one immutable snapshot: {input_sha}")
    return found[0]


def evidence_inventory_path(snapshot: pathlib.Path) -> pathlib.Path:
    regular = snapshot / "source/context/EVIDENCE_HASHES.json"
    targeted = snapshot / "source/EVIDENCE_HASHES.json"
    found = [path for path in (regular, targeted) if path.is_file() and not path.is_symlink()]
    if len(found) != 1:
        raise ValueError("snapshot does not contain exactly one evidence inventory")
    return found[0]


def verify_evidence_bindings(obj: dict[str, Any], result_path: pathlib.Path,
                             round_number: int, input_sha: str) -> None:
    """Rehash the frozen snapshot and every cited artifact after the provider call."""
    snapshot = snapshot_for_result(result_path, round_number, input_sha)
    manifest = load(snapshot / "MANIFEST.json")
    verify_read_only_inventory(snapshot, manifest)
    inventory_path = evidence_inventory_path(snapshot)
    if sha_file(inventory_path) != obj["input_hashes"]["evidence_manifest_sha256"]:
        raise ValueError("critic evidence-manifest hash does not bind the immutable snapshot")
    inventory = load(inventory_path)
    if not isinstance(inventory, dict):
        raise ValueError("critic evidence inventory must be an object")
    manifest_hashes = manifest.get("files_sha256")
    if not isinstance(manifest_hashes, dict):
        raise ValueError("snapshot manifest lacks a file inventory")

    bindings: list[tuple[str, dict[str, Any]]] = []
    for collection in ("items", "new_issues"):
        for index, assessment in enumerate(obj[collection]):
            evidence = assessment["evidence"]
            bindings.extend(
                (f"{collection}[{index}].evidence.artifact_refs[{ref_index}]", binding)
                for ref_index, binding in enumerate(evidence["artifact_refs"])
            )
            if evidence["reproducible_script"] is not None:
                bindings.append((f"{collection}[{index}].evidence.reproducible_script",
                                 evidence["reproducible_script"]))
    for label, binding in bindings:
        exact_keys(binding, {"path", "sha256"}, label)
        rel = safe_relative_path(binding["path"], f"{label}.path").as_posix()
        if not isinstance(binding["sha256"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", binding["sha256"]):
            raise ValueError(f"{label}.sha256 is not lowercase SHA-256")
        inventory_row = inventory.get(rel)
        if not isinstance(inventory_row, dict) or set(inventory_row) != {"sha256", "size"}:
            raise ValueError(f"{label}.path is not in the allowed frozen evidence inventory: {rel}")
        artifact = regular_without_symlinks(snapshot, rel, f"{label}.path")
        actual = sha_file(artifact)
        manifest_rel = artifact.relative_to(snapshot).as_posix()
        if (binding["sha256"] != actual or inventory_row["sha256"] != actual or
                inventory_row["size"] != artifact.stat().st_size or
                manifest_hashes.get(manifest_rel) != actual):
            raise ValueError(f"{label} does not match the frozen artifact hash: {rel}")


def within(root: pathlib.Path, candidate: pathlib.Path, label: str) -> pathlib.Path:
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes the audit workspace: {candidate}") from exc
    return resolved


def clean_roles(values: list[str]) -> list[str]:
    out: list[str] = []
    for raw in values:
        role = str(raw).strip().lower()
        if role.startswith("critic_"):
            role = role[len("critic_"):]
        if not role:
            continue
        if role not in ROLES:
            raise ValueError(f"unknown critic role: {role}")
        if role not in out:
            out.append(role)
    return out


def command_route(args: argparse.Namespace) -> int:
    manifest = None
    manifest_sha = None
    if args.manifest:
        path = pathlib.Path(args.manifest)
        manifest = load(path)
        if not isinstance(manifest, dict):
            raise ValueError("diff manifest must be a JSON object")
        if manifest.get("round", args.round) != args.round:
            raise ValueError("diff manifest round does not match requested round")
        if args.round >= 2:
            for field in ("changed_claim_ids", "changed_sections"):
                if field not in manifest or not isinstance(manifest[field], list):
                    raise ValueError(f"diff manifest requires array field {field}")
        manifest_sha = sha_file(path)

    if args.round == 1:
        roles = list(ROLES)
        if args.override and set(clean_roles(re.split(r"[\s,]+", args.override))) != set(roles):
            raise ValueError("round 1 requires the exact stats, data, fairness, completeness quorum")
        reason = "round 1 full four-role coverage"
    elif args.override:
        roles = clean_roles(re.split(r"[\s,]+", args.override))
        if not set(roles) & {"stats", "data", "fairness"}:
            raise ValueError("later-round CRITICS override requires at least one specialist")
        specialists = [role for role in roles if role != "completeness"]
        if len(specialists) > 1 and not (
                manifest and manifest.get("routing_mode") == "multi_domain_critical"
                and len(manifest.get("critical_domains") or []) > 1):
            raise ValueError("multiple later-round specialists require multi-domain critical changes")
        if "completeness" not in roles:
            roles.append("completeness")
        reason = "CRITICS override, with completeness retained"
    else:
        chosen: list[str] = []
        reason = "no diff manifest; policy fairness fallback"
        if manifest is not None:
            recommended = manifest.get("recommended_roles") or []
            if isinstance(recommended, str):
                recommended = re.split(r"[\s,]+", recommended)
            recommended = [r for r in clean_roles(list(recommended)) if r != "completeness"]
            if recommended:
                multi_critical = (manifest.get("routing_mode") == "multi_domain_critical" and
                                  len(manifest.get("critical_domains") or []) > 1)
                chosen.extend(recommended if multi_critical else recommended[:1])
                reason = ("multi-domain critical specialists recommended by diff manifest"
                          if multi_critical else "one best specialist recommended by diff manifest")

            scores = manifest.get("role_scores") or {}
            if not chosen and isinstance(scores, dict):
                candidates: list[str] = []
                for role in ("stats", "data", "fairness"):
                    score = scores.get(role)
                    if isinstance(score, (int, float)) and float(score) > 0:
                        candidates.append(role)
                if candidates:
                    chosen.append(max(candidates, key=lambda role: (float(scores[role]),
                                                                   -("stats", "data", "fairness").index(role))))
                    reason = "one highest-scoring specialist in diff manifest"

            if not chosen:
                fields: list[str] = []
                for key in ("domains", "changed_claim_types", "risk_tags"):
                    value = manifest.get(key) or []
                    fields.extend([value] if isinstance(value, str) else map(str, value))
                text = " ".join(fields).lower()
                patterns = {
                    "stats": r"stat|model|infer|estimate|effect|p.?value|regress|analysis|power|interval",
                    "data": r"data|duplicate|missing|archive|file|variable|record|integrity|sample|participant|exclusion|outcome|manifest|repository",
                    "fairness": r"fair|framing|wording|impact|privacy|disclos|author|conclusion|proportion|licen|remed",
                }
                matched = [
                    role for role, pattern in patterns.items() if re.search(pattern, text)
                ]
                if matched:
                    chosen.append(matched[0])
                    reason = "one best-matching specialist from diff-manifest risk tags"
        roles = clean_roles(chosen or ["fairness"])
        roles.append("completeness")

    if args.round == 1:
        claim_assignment_mode = "unscoped_round_1"
        claim_assignments = {role: [] for role in roles}
    else:
        raw_claim_ids = manifest.get("changed_claim_ids") if manifest is not None else []
        if not isinstance(raw_claim_ids, list) or any(
                not isinstance(value, str) or not value.strip() for value in raw_claim_ids):
            raise ValueError("later-round changed_claim_ids must contain only nonempty strings")
        claim_ids = list(dict.fromkeys(value.strip() for value in raw_claim_ids))
        claim_assignment_mode = "exact_changed_claims"
        claim_assignments = {role: claim_ids for role in roles}
    atomic_json(
        args.output,
        {
            "routing_version": "i4r-round-routing-1",
            "round": args.round,
            "roles": roles,
            "reason": reason,
            "diff_manifest_present": manifest is not None,
            "diff_manifest_sha256": manifest_sha,
            "claim_assignment_mode": claim_assignment_mode,
            "claim_assignments": claim_assignments,
        },
    )
    return 0


def critic_schema(
    role: str, round_number: int, report_sha: str, audit_id: str,
    invocation_id: str, input_hashes: dict[str, str],
    covered_claim_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Specialize the canonical shared v2 schema without mutating it."""
    def codex_subset(value: Any) -> Any:
        if isinstance(value, list):
            return [codex_subset(item) for item in value]
        if not isinstance(value, dict):
            return value
        out = {}
        for key, item in value.items():
            # Cross-field conditionals are enforced by validate_critic_obj after generation. The
            # provider schema uses the conservative Structured Outputs subset accepted by Codex.
            if key in ("$id", "allOf", "if", "then", "else"):
                continue
            out["anyOf" if key == "oneOf" else key] = codex_subset(item)
        return out

    schema = codex_subset(load(CRITIC_SCHEMA))
    schema["properties"]["schema_version"] = {"const": "2.0.0"}
    schema["properties"]["audit_id"] = {"const": audit_id}
    schema["properties"]["invocation_id"] = {"const": invocation_id}
    schema["properties"]["round"] = {"const": round_number}
    schema["properties"]["role"] = {"const": f"critic_{role}"}
    schema["properties"]["report_sha256"] = {"const": report_sha}
    for name, digest in input_hashes.items():
        schema["properties"]["input_hashes"]["properties"][name] = {"const": digest}
    if covered_claim_ids is not None:
        schema["properties"]["covered_claim_ids"] = {"const": covered_claim_ids}
        items = schema["properties"]["items"]
        items["minItems"] = len(covered_claim_ids)
        items["maxItems"] = len(covered_claim_ids)
        if covered_claim_ids:
            schema["$defs"]["assessment"]["properties"]["finding_id"] = {
                "enum": covered_claim_ids,
            }
    schema["properties"]["status"] = {"const": "complete"}
    schema["properties"]["completion_marker"] = {"const": True}
    declaration = schema["properties"]["completeness_declaration"]["properties"]
    declaration["all_assigned_checks_accounted_for"] = {"const": True}
    declaration["all_assessments_evidenced"] = {"const": True}
    declaration["unresolved_omissions"]["maxItems"] = 0
    schema["properties"]["coverage"]["properties"]["assigned_checks"]["minItems"] = 1
    schema["properties"]["coverage"]["properties"]["completed_checks"]["minItems"] = 1
    schema["properties"]["overall"]["properties"]["summary"]["minLength"] = 20
    return schema


def render_template(template: str, context: pathlib.Path, round_number: int) -> str:
    for key, filename in (
        ("PAPER", "PAPER.txt"), ("ENV", "ENV.txt"), ("SLOW", "SLOW.txt"),
        ("WRAPUP", "WRAPUP.txt"), ("SETTLED", "SETTLED.md"),
    ):
        template = template.replace("{{" + key + "}}", (context / filename).read_text().strip())
    return template.replace("${ROUND}", str(round_number))


def command_snapshot(args: argparse.Namespace) -> int:
    workspace = pathlib.Path(args.workspace).resolve()
    report = pathlib.Path(args.report).resolve()
    prompts = pathlib.Path(args.prompts).resolve()
    out = pathlib.Path(args.out).resolve()
    roles = args.roles.split()
    for role in roles:
        if role not in ROLES:
            raise ValueError(f"unknown role in snapshot: {role}")
    audit_id = workspace.name
    run_record = workspace / "run.json"
    if run_record.exists():
        candidate = load(run_record).get("audit_id")
        if isinstance(candidate, str) and candidate.strip():
            audit_id = candidate.strip()
    if len(audit_id) < 3:
        audit_id = f"audit-{hashlib.sha256(str(workspace).encode()).hexdigest()[:12]}"

    source = pathlib.Path(tempfile.mkdtemp(prefix=".source.", dir=out))
    staging: pathlib.Path | None = None
    try:
        (source / "prompts").mkdir()
        (source / "context").mkdir()
        shutil.copyfile(report, source / "REPORT_TO_REVIEW.md")
        shutil.copyfile(prompts / "common.txt", source / "prompts/common.txt")
        for role in roles:
            shutil.copyfile(prompts / f"role_{role}.txt", source / f"prompts/role_{role}.txt")
        shutil.copyfile(args.routing, source / "ROUTING.json")

        contexts = {
            "PAPER.txt": None,
            "ENV.txt": "python3 with the scientific stack\n",
            "SLOW.txt": "any job over ~10 minutes\n",
            "WRAPUP.txt": None,
            "SETTLED.md": "No settled-facts file exists yet; derive only what your assigned criticism needs.\n",
        }
        for filename, default in contexts.items():
            candidate = workspace / filename
            if filename == "SETTLED.md":
                (source / f"context/{filename}").write_text(default)
            elif candidate.exists():
                shutil.copyfile(candidate, source / f"context/{filename}")
            elif default is None:
                raise ValueError(f"required context file missing: {candidate}")
            else:
                (source / f"context/{filename}").write_text(default)
        policy = workspace / "pipeline-policy.snapshot.json"
        policy_envelope = validate_policy_snapshot(policy, workspace)
        configured_model, configured_effort = configured_model_route(
            policy_envelope, "critics",
        )
        if (args.model, args.effort) != (configured_model, configured_effort):
            raise ValueError(
                "ordinary critic snapshot model/effort differs from models.critics policy"
            )
        shutil.copyfile(policy, source / "context/PIPELINE_POLICY.json")
        for relative in (
            "work/findings.jsonl", "work/pkg_manifest.txt", "verification_manifest.json",
            "comment.json", "RELEASE_MANIFEST.md",
        ):
            candidate = workspace / relative
            if candidate.is_file():
                destination = source / "context" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(candidate, destination)

        frozen_evidence: dict[str, pathlib.Path] = {}
        registry = workspace / "work/findings.jsonl"
        if registry.is_file():
            for line_number, line in enumerate(registry.read_text().splitlines(), 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"findings registry line {line_number} is not an object")
                evidence = row.get("evidence") or {}
                for source_hash in evidence.get("source_hashes") or []:
                    if not isinstance(source_hash, dict) or not isinstance(source_hash.get("path"), str):
                        raise ValueError(f"findings registry line {line_number} has malformed source_hashes")
                    relative = safe_relative_path(source_hash["path"], "evidence path").as_posix()
                    artifact = regular_without_symlinks(workspace, relative, "evidence path")
                    actual = sha_file(artifact)
                    expected = source_hash.get("sha256")
                    if not isinstance(expected, str) or expected != actual:
                        raise ValueError(f"referenced evidence hash changed: {relative}")
                    frozen_evidence[relative] = artifact
                dependencies = row.get("dependencies") or {}
                for relative in dependencies.get("artifact_paths") or []:
                    if not isinstance(relative, str):
                        raise ValueError(f"findings registry line {line_number} has malformed artifact path")
                    normalized = safe_relative_path(relative, "dependency artifact").as_posix()
                    candidate = workspace / normalized
                    if candidate.exists() or candidate.is_symlink():
                        frozen_evidence[normalized] = regular_without_symlinks(
                            workspace, normalized, "dependency artifact")
        for relative, artifact in sorted(frozen_evidence.items()):
            destination = source / "evidence" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact, destination)
        if args.round >= 2 and not args.diff_manifest:
            raise ValueError("later-round snapshot requires a verified diff manifest")
        if args.diff_manifest:
            shutil.copyfile(args.diff_manifest, source / "context/DIFF_MANIFEST.json")
            diff_for_context = load(args.diff_manifest)
            settled_refs = diff_for_context.get("settled_fact_references")
            if not isinstance(settled_refs, list) or any(
                    not isinstance(item, dict) for item in settled_refs):
                raise ValueError("diff manifest settled_fact_references must be an array of objects")
            settled_lines = ["# Settled facts referenced by this exact diff packet", ""]
            if settled_refs:
                settled_lines.extend(
                    "- `" + json.dumps(item, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False).replace("`", "\\u0060") + "`"
                    for item in settled_refs
                )
            else:
                settled_lines.append("No settled facts are referenced by this delta review.")
            (source / "context/SETTLED.md").write_text("\n".join(settled_lines) + "\n")

        # The only paths a critic may cite are immutable scientific inputs in this snapshot.
        # Prompts, routing, schemas, and the evidence inventory itself are deliberately excluded.
        allowed_evidence = [source / "REPORT_TO_REVIEW.md"]
        allowed_evidence.extend(
            path for path in sorted((source / "context").rglob("*"))
            if path.is_file() and path.name != "EVIDENCE_HASHES.json"
        )
        if (source / "evidence").is_dir():
            allowed_evidence.extend(
                path for path in sorted((source / "evidence").rglob("*")) if path.is_file()
            )
        evidence_inventory = {
            "source/" + path.relative_to(source).as_posix(): {
                "sha256": sha_file(path), "size": path.stat().st_size,
            }
            for path in allowed_evidence
        }
        atomic_json(source / "context/EVIDENCE_HASHES.json", evidence_inventory)

        script_sha = sha_file(args.runtime_script)
        helper_sha = sha_file(__file__)
        canonical_schema_sha = sha_file(CRITIC_SCHEMA)
        schema_validator_sha = sha_file(SCHEMA_VALIDATOR)
        semantic_validator_sha = sha_file(SEMANTIC_VALIDATOR)
        identity_parts = [
            "runtime-contract-3", str(args.round), args.model, args.effort,
            " ".join(roles), audit_id, script_sha, helper_sha, canonical_schema_sha,
            schema_validator_sha, semantic_validator_sha,
        ]
        input_sha = hash_tree(source, identity_parts)
        report_sha = sha_file(source / "REPORT_TO_REVIEW.md")
        if args.diff_manifest:
            diff = load(args.diff_manifest)
            if diff.get("audit_id", audit_id) != audit_id:
                raise ValueError("diff manifest audit_id does not match this audit")
            inputs = diff.get("inputs") or {}
            if isinstance(inputs, dict) and inputs.get("after_report_sha256", report_sha) != report_sha:
                raise ValueError("diff manifest is stale for the current report")
        snapshot = out / "snapshots" / input_sha
        snapshot_rel = f"critique/round_{args.round}/snapshots/{input_sha}"
        if snapshot.exists():
            manifest = load(snapshot / "MANIFEST.json")
            expected_manifest = {
                "snapshot_version": "i4r-runtime-snapshot-3",
                "input_sha256": input_sha,
                "report_sha256": report_sha,
                "round": args.round,
                "model": args.model,
                "effort": args.effort,
                "roles": roles,
                "audit_id": audit_id,
                "runtime_script_sha256": script_sha,
                "runtime_helper_sha256": helper_sha,
                "canonical_critic_schema_sha256": canonical_schema_sha,
                "schema_validator_sha256": schema_validator_sha,
                "semantic_validator_sha256": semantic_validator_sha,
            }
            if any(manifest.get(key) != value for key, value in expected_manifest.items()):
                raise ValueError(f"corrupt snapshot: {snapshot}")
            if hash_tree(snapshot / "source", identity_parts) != input_sha:
                raise ValueError(f"snapshot source no longer matches its content address: {snapshot}")
            verify_read_only_inventory(snapshot, manifest)
        else:
            staging = out / "snapshots" / f".{input_sha}.{os.getpid()}.tmp"
            staging.mkdir(parents=True)
            shutil.move(str(source), staging / "source")
            source = staging / "source"
            (staging / "rendered").mkdir()
            (staging / "schemas").mkdir()
            context = source / "context"
            common = (source / "prompts/common.txt").read_text()
            policy_sha = sha_file(source / "context/PIPELINE_POLICY.json")
            evidence_sha = sha_file(source / "context/EVIDENCE_HASHES.json")
            routing = load(source / "ROUTING.json")
            if routing.get("claim_assignment_mode") not in {
                    "unscoped_round_1", "exact_changed_claims"}:
                raise ValueError("routing lacks a supported claim-assignment mode")
            assignments = routing.get("claim_assignments")
            if not isinstance(assignments, dict) or set(assignments) != set(roles):
                raise ValueError("routing claim assignments do not match routed roles")
            for assigned_role in roles:
                if not isinstance(assignments[assigned_role], list) or len(
                        assignments[assigned_role]) != len(set(assignments[assigned_role])):
                    raise ValueError(f"routing claim assignment is malformed for {assigned_role}")
            for role in roles:
                role_prompt = (source / f"prompts/role_{role}.txt").read_text()
                rendered = render_template(common, context, args.round)
                rendered += "\n\n" + render_template(role_prompt, context, args.round)
                if args.round >= 2:
                    rendered += f"""

## Later-round delta scope (supersedes full-document review instructions above)

Start from `{snapshot_rel}/source/context/DIFF_MANIFEST.json`: review its changed_claims,
changed_section_bodies, abstract_sentence_changes, appendix_row_changes, dependencies, and the exact
claim assignment below. Open only the immediate immutable report or findings-registry context needed
to decide those assigned changes. Do not reread the whole report or registry. Whole-document
consistency, privacy, checklist, and archive checks run as deterministic gates after this delta review.
"""
                logical_invocation_id = (
                    f"{audit_id}:r{args.round}:critic_{role}:{input_sha[:16]}"
                )
                rendered += f"""

## Runtime output contract (supersedes the earlier file-writing instruction)

The workspace is read-only. Do not create or edit a file. Review the immutable report at
`{snapshot_rel}/source/REPORT_TO_REVIEW.md`; never use `report/REPORT_TO_REVIEW.md`.
Return one JSON object matching the supplied output schema. Do not wrap it in Markdown.
`status=complete` and `completion_marker=true` mean every nonempty `assigned_checks` entry is in
`completed_checks`; skipped checks are not supported. For every machine-assigned
`covered_claim_ids` entry, return exactly one claim-level `items` assessment whose `finding_id` is
that exact ID. Nonempty claim coverage with an empty or missing assessment is invalid and does not
count as review. Every evidence artifact reference (and a non-null reproducible script) must be an
exact `{{"path":"source/...","sha256":"..."}}` binding copied from the frozen evidence inventory;
live-workspace, unlisted, unhashed, symlinked, or post-call-mutated artifacts are invalid. Use the
canonical v2 fields from the supplied schema.

runtime_role={role}
runtime_schema_role=critic_{role}
runtime_round={args.round}
runtime_audit_id={audit_id}
runtime_input_sha256={input_sha}
runtime_report_sha256={report_sha}
runtime_snapshot={snapshot_rel}
runtime_diff_manifest={snapshot_rel + '/source/context/DIFF_MANIFEST.json' if args.diff_manifest else 'none'}
runtime_findings_registry={snapshot_rel + '/source/context/work/findings.jsonl' if (source / 'context/work/findings.jsonl').is_file() else 'none'}
runtime_evidence_inventory={snapshot_rel}/source/context/EVIDENCE_HASHES.json
runtime_pipeline_policy={snapshot_rel}/source/context/PIPELINE_POLICY.json
runtime_claim_assignment_mode={routing['claim_assignment_mode']}
runtime_covered_claim_ids={json.dumps(assignments[role], separators=(',', ':'))}
"""
                prompt_path = staging / f"rendered/prompt_{role}.txt"
                prompt_path.write_text(rendered)
                input_hashes = {
                    "snapshot_sha256": input_sha,
                    "report_sha256": report_sha,
                    "prompt_sha256": sha_file(prompt_path),
                    "policy_sha256": policy_sha,
                    "evidence_manifest_sha256": evidence_sha,
                }
                atomic_json(
                    staging / f"schemas/critic_{role}.schema.json",
                    critic_schema(
                        role, args.round, report_sha, audit_id, logical_invocation_id,
                        input_hashes,
                        assignments[role] if routing["claim_assignment_mode"] ==
                        "exact_changed_claims" else None,
                    ),
                )
            atomic_json(
                staging / "schemas/targeted_adjudication.schema.json",
                critic_schema(
                    "synth", args.round, report_sha, audit_id,
                    f"{audit_id}:r{args.round}:critic_synth:{input_sha[:16]}",
                    {
                        "snapshot_sha256": input_sha,
                        "report_sha256": report_sha,
                        "prompt_sha256": "0" * 64,
                        "policy_sha256": policy_sha,
                        "evidence_manifest_sha256": evidence_sha,
                    },
                ),
            )
            atomic_json(
                staging / "schemas/probe.schema.json",
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["status"],
                    "properties": {"status": {"const": "ok"}},
                },
            )
            file_hashes = {
                path.relative_to(staging).as_posix(): sha_file(path)
                for path in sorted(staging.rglob("*")) if path.is_file()
            }
            atomic_json(staging / "MANIFEST.json", {
                "snapshot_version": "i4r-runtime-snapshot-3",
                "input_sha256": input_sha,
                "report_sha256": report_sha,
                "round": args.round,
                "model": args.model,
                "effort": args.effort,
                "roles": roles,
                "audit_id": audit_id,
                "routing_reason": routing["reason"],
                "runtime_script_sha256": script_sha,
                "runtime_helper_sha256": helper_sha,
                "canonical_critic_schema_sha256": canonical_schema_sha,
                "schema_validator_sha256": schema_validator_sha,
                "semantic_validator_sha256": semantic_validator_sha,
                "files_sha256": file_hashes,
            })
            make_read_only(staging)
            os.rename(staging, snapshot)
        print(
            json.dumps(
                {
                    "audit_id": audit_id,
                    "input_sha256": input_sha,
                    "report_sha256": report_sha,
                    "snapshot": str(snapshot),
                    "snapshot_rel": snapshot_rel,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        if staging is not None and staging.exists():
            discard_tree(staging)
        if source.exists() and source.name.startswith(".source."):
            shutil.rmtree(source, ignore_errors=True)


def command_bind_contract(args: argparse.Namespace) -> int:
    """Bind one immutable role schema to the exact provider-attempt invocation ID."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,239}", args.invocation_id):
        raise ValueError("attempt invocation ID is not a bounded machine-safe identifier")
    schema = load(args.base)
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        raise ValueError("base critic contract is malformed")
    schema["properties"]["invocation_id"] = {"const": args.invocation_id}
    atomic_json(args.output, schema)
    return 0


def command_critic_equivalent(args: argparse.Namespace) -> int:
    """Compare scientific payloads while ignoring the unique provider-attempt identity."""
    left, right = load(args.left), load(args.right)
    validate_v2.validate_critic_result(left)
    validate_v2.validate_critic_result(right)
    for value in (left, right):
        value.pop("invocation_id", None)
    if left != right:
        raise ValueError("critic payloads differ beyond invocation identity")
    return 0


def command_target_snapshot(args: argparse.Namespace) -> int:
    workspace = pathlib.Path(args.workspace).resolve()
    out = pathlib.Path(args.out).resolve()
    round_snapshot = pathlib.Path(args.round_snapshot).resolve()
    packet_path = pathlib.Path(args.packet).resolve()
    base_schema = pathlib.Path(args.schema).resolve()
    packet = load(packet_path)
    exact_keys(packet, {
        "packet_version", "audit_id", "round", "input_sha256", "report_sha256", "policy",
        "triggers", "claim_merge_sha256", "unresolved_rows", "critic_outputs",
    }, "adjudication packet")
    if packet["packet_version"] != "i4r-targeted-adjudication-packet-2":
        raise ValueError("unsupported adjudication packet version")
    if not packet["triggers"]:
        raise ValueError("targeted adjudication requires at least one trigger")

    source = pathlib.Path(tempfile.mkdtemp(prefix=".target-source.", dir=out))
    staging: pathlib.Path | None = None
    try:
        merge_path = packet_path.with_name("CLAIM_MERGE.json")
        if merge_path.is_symlink() or not merge_path.is_file() or sha_file(
                merge_path) != packet["claim_merge_sha256"]:
            raise ValueError("adjudication packet does not bind the exact deterministic claim merge")
        report = round_snapshot / "source/REPORT_TO_REVIEW.md"
        if not report.is_file():
            raise ValueError("round snapshot report is missing")
        shutil.copyfile(report, source / "REPORT_TO_REVIEW.md")
        shutil.copyfile(base_schema, source / "base.schema.json")
        policy_source = round_snapshot / "source/context/PIPELINE_POLICY.json"
        policy_envelope = validate_policy_snapshot(policy_source, workspace)
        critic_model, critic_effort = configured_model_route(policy_envelope, "critics")
        round_manifest = load(round_snapshot / "MANIFEST.json")
        if (round_manifest.get("model"), round_manifest.get("effort")) != (
                critic_model, critic_effort):
            raise ValueError("targeted adjudication is not based on the policy-bound critic snapshot")
        adjudicator_model, adjudicator_effort = configured_model_route(
            policy_envelope, "targeted_adjudicator",
        )
        if (args.model, args.effort) != (adjudicator_model, adjudicator_effort):
            raise ValueError(
                "target snapshot model/effort differs from models.targeted_adjudicator policy"
            )
        shutil.copyfile(policy_source, source / "PIPELINE_POLICY.json")

        copied_roles = []
        for index, entry in enumerate(packet["critic_outputs"]):
            exact_keys(entry, {
                "role", "json_path", "json_sha256", "markdown_path", "markdown_sha256",
                "completion_path", "completion_sha256",
            }, f"critic_outputs[{index}]")
            role = entry["role"]
            if role not in {f"critic_{value}" for value in ROLES} or role in copied_roles:
                raise ValueError(f"invalid or duplicate target critic role: {role}")
            copied_roles.append(role)
            for path_key, hash_key, suffix in (
                ("json_path", "json_sha256", ".json"),
                ("markdown_path", "markdown_sha256", ".md"),
                ("completion_path", "completion_sha256", ".complete.json"),
            ):
                artifact = within(workspace, workspace / entry[path_key], path_key)
                if artifact.is_symlink() or not artifact.is_file() or sha_file(
                        artifact) != entry[hash_key]:
                    raise ValueError(f"target critic artifact changed: {entry[path_key]}")
        unresolved_rows = packet["unresolved_rows"]
        if not isinstance(unresolved_rows, list) or not unresolved_rows:
            raise ValueError("targeted adjudication packet requires unresolved rows")
        row_ids = []
        for index, row in enumerate(unresolved_rows):
            exact_keys(row, {
                "row_id", "claim_id", "trigger_ids", "trigger_kinds", "roles", "details",
                "claim_merge",
            }, f"unresolved_rows[{index}]")
            nonempty(row["row_id"], f"unresolved_rows[{index}].row_id")
            if row["row_id"] in row_ids:
                raise ValueError("target unresolved row IDs must be unique")
            row_ids.append(row["row_id"])
            if not isinstance(row["trigger_ids"], list) or not row["trigger_ids"]:
                raise ValueError(f"unresolved_rows[{index}] lacks trigger IDs")

        # The adjudicator receives only unresolved merged rows, never paths to or copies of the
        # full critic outputs. The full packet remains outside the provider snapshot for projection.
        atomic_json(source / "adjudication_packet.json", {
            "packet_version": "i4r-targeted-adjudication-input-1",
            "audit_id": packet["audit_id"],
            "round": packet["round"],
            "input_sha256": packet["input_sha256"],
            "report_sha256": packet["report_sha256"],
            "policy": packet["policy"],
            "claim_merge_sha256": packet["claim_merge_sha256"],
            "unresolved_rows": unresolved_rows,
        })

        evidence_inventory = {}
        for path in (
            source / "adjudication_packet.json", source / "REPORT_TO_REVIEW.md",
            source / "PIPELINE_POLICY.json",
        ):
            rel = "source/" + path.relative_to(source).as_posix()
            evidence_inventory[rel] = {
                "sha256": sha_file(path), "size": path.stat().st_size,
            }
        atomic_json(source / "EVIDENCE_HASHES.json", evidence_inventory)

        target_identity = [
            "i4r-target-snapshot-2", args.model, args.effort, sha_file(__file__),
            sha_file(args.runtime_script),
        ]
        target_sha = hash_tree(source, target_identity)
        snapshot = out / "target_snapshots" / target_sha
        snapshot_rel = f"critique/round_{packet['round']}/target_snapshots/{target_sha}"
        expected_manifest = {
            "snapshot_version": "i4r-target-snapshot-2",
            "target_input_sha256": target_sha,
            "packet_sha256": sha_file(packet_path),
            "round_input_sha256": packet["input_sha256"],
            "report_sha256": packet["report_sha256"],
            "round": packet["round"],
            "audit_id": packet["audit_id"],
            "model": args.model,
            "effort": args.effort,
            "roles": copied_roles,
        }
        if snapshot.exists():
            manifest = load(snapshot / "MANIFEST.json")
            if any(manifest.get(key) != value for key, value in expected_manifest.items()):
                raise ValueError(f"corrupt target snapshot: {snapshot}")
            if hash_tree(snapshot / "source", target_identity) != target_sha:
                raise ValueError("target snapshot source no longer matches its content address")
            verify_read_only_inventory(snapshot, manifest)
        else:
            staging = out / "target_snapshots" / f".{target_sha}.{os.getpid()}.tmp"
            staging.mkdir(parents=True)
            shutil.move(str(source), staging / "source")
            source = staging / "source"
            schema = load(source / "base.schema.json")
            coverage = schema["properties"]["coverage"]["properties"]
            exact_array = {"type": "array", "minItems": len(row_ids),
                           "maxItems": len(row_ids), "uniqueItems": True,
                           "items": {"enum": row_ids}}
            coverage["assigned_checks"] = exact_array
            coverage["completed_checks"] = dict(exact_array)
            coverage["skipped_checks"]["maxItems"] = 0
            schema["$defs"]["assessment"]["properties"]["item_id"] = {"enum": row_ids}
            schema["$defs"]["assessment"]["properties"]["finding_id"] = {"enum": row_ids}
            schema["properties"]["items"]["minItems"] = len(row_ids)
            schema["properties"]["items"]["maxItems"] = len(row_ids)
            schema["properties"]["covered_claim_ids"] = {"const": row_ids}
            logical_invocation_id = (
                f"{packet['audit_id']}:r{packet['round']}:critic_synth:{target_sha[:16]}"
            )
            schema["properties"]["invocation_id"] = {"const": logical_invocation_id}
            prompt = f"""I4R_TARGETED_ADJUDICATION
Resolve only the enumerated triggers; do not synthesize or restate the whole critique.
Use only `unresolved_rows` in the immutable packet; resolved merge rows and full critic outputs are
not present in this snapshot. Use the immutable report only for immediate context. Do not edit files.
Return exactly one raw JSON object matching the supplied schema; no Markdown fence or preamble.
runtime_packet={snapshot_rel}/source/adjudication_packet.json
runtime_report={snapshot_rel}/source/REPORT_TO_REVIEW.md
runtime_evidence_inventory={snapshot_rel}/source/EVIDENCE_HASHES.json
runtime_snapshot={snapshot_rel}
runtime_input_sha256={target_sha}
runtime_round_input_sha256={packet['input_sha256']}
runtime_report_sha256={packet['report_sha256']}
runtime_audit_id={packet['audit_id']}
runtime_round={packet['round']}
"""
            prompt_path = staging / "prompt.txt"
            prompt_path.write_text(prompt)
            target_hashes = {
                "snapshot_sha256": target_sha,
                "report_sha256": packet["report_sha256"],
                "prompt_sha256": sha_file(prompt_path),
                "policy_sha256": sha_file(source / "PIPELINE_POLICY.json"),
                "evidence_manifest_sha256": sha_file(source / "EVIDENCE_HASHES.json"),
            }
            for name, digest in target_hashes.items():
                schema["properties"]["input_hashes"]["properties"][name] = {"const": digest}
            atomic_json(staging / "output.schema.json", schema)
            file_hashes = {
                path.relative_to(staging).as_posix(): sha_file(path)
                for path in sorted(staging.rglob("*")) if path.is_file()
            }
            atomic_json(staging / "MANIFEST.json", {
                **expected_manifest, "input_hashes": target_hashes, "files_sha256": file_hashes,
            })
            make_read_only(staging)
            os.rename(staging, snapshot)
        print(json.dumps({
            "target_input_sha256": target_sha,
            "packet_sha256": sha_file(packet_path),
            "snapshot": str(snapshot),
            "snapshot_rel": snapshot_rel,
            "prompt": str(snapshot / "prompt.txt"),
            "schema": str(snapshot / "output.schema.json"),
        }, sort_keys=True))
        return 0
    finally:
        if staging is not None and staging.exists():
            discard_tree(staging)
        if source.exists() and source.name.startswith(".target-source."):
            shutil.rmtree(source, ignore_errors=True)


def command_projection(args: argparse.Namespace) -> int:
    out = pathlib.Path(args.out).resolve()
    results = pathlib.Path(args.results).resolve()
    roles = args.roles.split()
    if not roles or any(role not in ROLES for role in roles) or len(roles) != len(set(roles)):
        raise ValueError("projection requires unique known roles")
    target = out / "projections" / args.invocation
    if target.exists():
        raise ValueError(f"projection invocation already exists: {target}")
    staging = out / "projections" / f".{args.invocation}.{os.getpid()}.tmp"
    staging.mkdir(parents=True)
    try:
        audit_root = out.parent.parent
        round_snapshot = out / "snapshots" / args.input_sha
        round_manifest = load(round_snapshot / "MANIFEST.json")
        round_policy = validate_policy_snapshot(
            round_snapshot / "source/context/PIPELINE_POLICY.json", audit_root,
        )
        critic_model, critic_effort = configured_model_route(round_policy, "critics")
        if (round_manifest.get("snapshot_version") != "i4r-runtime-snapshot-3" or
                round_manifest.get("input_sha256") != args.input_sha or
                round_manifest.get("report_sha256") != args.report_sha or
                round_manifest.get("round") != args.round or
                (round_manifest.get("model"), round_manifest.get("effort")) !=
                (critic_model, critic_effort)):
            raise ValueError("ordinary critic snapshot model, effort, or identity differs")
        verify_read_only_inventory(round_snapshot, round_manifest)
        ledger_path = audit_root / "invocations.jsonl"
        if not ledger_path.is_file():
            raise ValueError("canonical invocation ledger is missing")
        ledger = []
        for line_number, line in enumerate(ledger_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = validate_json_schema.strict_loads(
                line, source=f"{ledger_path}:line {line_number}",
            )
            validate_v2.validate_invocation(row)
            ledger.append(row)
        routing_obj = load(args.routing)
        assignment_mode = routing_obj.get("claim_assignment_mode")
        assignments = routing_obj.get("claim_assignments")
        if (assignment_mode not in {"unscoped_round_1", "exact_changed_claims"} or
                not isinstance(assignments, dict) or set(assignments) != set(roles)):
            raise ValueError("projection routing has malformed claim assignments")
        if args.round == 1 and assignment_mode != "unscoped_round_1":
            raise ValueError("round 1 projection must declare unscoped claim assignment")
        if args.round > 1 and assignment_mode != "exact_changed_claims":
            raise ValueError("later-round projection must declare exact changed-claim assignment")
        packet_path = pathlib.Path(args.packet)
        packet_obj = load(packet_path)
        exact_keys(packet_obj, {
            "packet_version", "audit_id", "round", "input_sha256", "report_sha256", "policy",
            "triggers", "claim_merge_sha256", "unresolved_rows", "critic_outputs",
        }, "projection adjudication packet")
        if (packet_obj["packet_version"] != "i4r-targeted-adjudication-packet-2" or
                packet_obj["audit_id"] != args.audit_id or packet_obj["round"] != args.round or
                packet_obj["input_sha256"] != args.input_sha or
                packet_obj["report_sha256"] != args.report_sha):
            raise ValueError("projection adjudication packet identity differs")
        merge_path = packet_path.with_name("CLAIM_MERGE.json")
        if merge_path.is_symlink() or not merge_path.is_file() or sha_file(
                merge_path) != packet_obj["claim_merge_sha256"]:
            raise ValueError("projection packet does not bind its deterministic claim merge")
        merge_obj = load(merge_path)
        if (merge_obj.get("merge_version") != "i4r-claim-merge-1" or
                merge_obj.get("audit_id") != args.audit_id or
                merge_obj.get("round") != args.round or
                merge_obj.get("input_sha256") != args.input_sha or
                merge_obj.get("report_sha256") != args.report_sha or
                merge_obj.get("roles") != [f"critic_{role}" for role in roles]):
            raise ValueError("claim merge identity or role quorum differs")
        output_metadata = {
            entry.get("role"): entry for entry in packet_obj["critic_outputs"]
            if isinstance(entry, dict)
        }
        if set(output_metadata) != {f"critic_{role}" for role in roles}:
            raise ValueError("adjudication packet critic-output quorum differs")
        claim_assessments: dict[str, list[str]] = {}
        projection_rows: list[tuple[str, dict[str, Any]]] = []

        def require_invocation_link(
            obj: dict[str, Any], response: pathlib.Path, expected_role: str,
            expected_model: str, expected_effort: str,
        ) -> None:
            matches = [row for row in ledger if row["invocation_id"] == obj["invocation_id"]]
            if len(matches) != 1:
                raise ValueError(
                    f"critic result {obj['invocation_id']} does not join exactly one invocation"
                )
            row = matches[0]
            expected = {
                "role": expected_role, "round": args.round, "status": "completed",
                "provider_call": True,
                "requested_model": expected_model,
                "reasoning_effort": expected_effort,
                "input_artifact_sha256": obj["input_hashes"]["snapshot_sha256"],
                "prompt_sha256": obj["input_hashes"]["prompt_sha256"],
                "output_sha256": sha_file(response),
            }
            wrong = [name for name, value in expected.items() if row.get(name) != value]
            if wrong:
                raise ValueError(
                    f"critic result {obj['invocation_id']} invocation linkage differs: "
                    + ", ".join(wrong)
                )

        for role in roles:
            response = results / f"critic_{role}.json"
            markdown = results / f"critic_{role}.md"
            sidecar = results / f"{role}.complete.json"
            validate_completion(
                sidecar, response, markdown, audit_id=args.audit_id, input_sha=args.input_sha,
                report_sha=args.report_sha, round_number=args.round, role=role,
            )
            obj = load(response)
            validate_critic_obj(obj, argparse.Namespace(
                audit_id=args.audit_id, report_sha=args.report_sha, round=args.round, role=role,
                input_sha=args.input_sha, result_path=str(response),
            ))
            if assignment_mode == "exact_changed_claims" and obj["covered_claim_ids"] != assignments[role]:
                raise ValueError(f"critic_{role} coverage differs from its machine assignment")
            assessed = [item["finding_id"] for item in obj["items"]]
            if assignment_mode == "exact_changed_claims" and assessed != assignments[role]:
                raise ValueError(f"critic_{role} actual assessments differ from its assignment")
            claim_assessments[role] = assessed
            projection_rows.append((role, obj))
            require_invocation_link(
                obj, response, f"critic_{role}", critic_model, critic_effort,
            )
            if markdown.read_text() != render_critic(obj):
                raise ValueError(f"critic_{role} Markdown is not the canonical rendering")
            metadata = output_metadata[f"critic_{role}"]
            if (metadata.get("json_sha256") != sha_file(response) or
                    metadata.get("markdown_sha256") != sha_file(markdown) or
                    metadata.get("completion_sha256") != sha_file(sidecar)):
                raise ValueError(f"critic_{role} packet hashes differ from completed outputs")
            for path in (response, markdown, sidecar):
                shutil.copyfile(path, staging / path.name)

        frozen_registry = (
            out / "snapshots" / args.input_sha / "source/context/work/findings.jsonl"
        )
        rebuilt_merge = build_claim_merge(
            projection_rows, load_registry_file(frozen_registry), packet_obj["triggers"],
            audit_id=args.audit_id, round_number=args.round, input_sha=args.input_sha,
            report_sha=args.report_sha,
        )
        if rebuilt_merge != merge_obj:
            raise ValueError("deterministic claim merge differs from completed critic assessments")
        if unresolved_rows_for_packet(packet_obj["triggers"], merge_obj) != packet_obj["unresolved_rows"]:
            raise ValueError("adjudication packet unresolved rows differ from deterministic merge")

        if args.adjudication == "complete":
            target_snapshot = out / "target_snapshots" / args.target_input_sha
            target_manifest = load(target_snapshot / "MANIFEST.json")
            target_policy = validate_policy_snapshot(
                target_snapshot / "source/PIPELINE_POLICY.json", audit_root,
            )
            if target_policy != round_policy:
                raise ValueError("target snapshot policy differs from the ordinary critic snapshot")
            adjudicator_model, adjudicator_effort = configured_model_route(
                target_policy, "targeted_adjudicator",
            )
            if (target_manifest.get("snapshot_version") != "i4r-target-snapshot-2" or
                    target_manifest.get("target_input_sha256") != args.target_input_sha or
                    target_manifest.get("round_input_sha256") != args.input_sha or
                    target_manifest.get("report_sha256") != args.report_sha or
                    target_manifest.get("round") != args.round or
                    (target_manifest.get("model"), target_manifest.get("effort")) !=
                    (adjudicator_model, adjudicator_effort)):
                raise ValueError("targeted-adjudicator snapshot model, effort, or identity differs")
            verify_read_only_inventory(target_snapshot, target_manifest)
            response = results / "TARGETED_ADJUDICATION.json"
            markdown = results / "TARGETED_ADJUDICATION.md"
            sidecar = results / "targeted_adjudicator.complete.json"
            validate_completion(
                sidecar, response, markdown, audit_id=args.audit_id,
                input_sha=args.target_input_sha, report_sha=args.report_sha,
                round_number=args.round, role="targeted_adjudicator",
            )
            obj = load(response)
            validate_critic_obj(obj, argparse.Namespace(
                audit_id=args.audit_id, report_sha=args.report_sha, round=args.round, role="synth",
                input_sha=args.target_input_sha, result_path=str(response),
            ))
            expected_rows = [row["row_id"] for row in packet_obj["unresolved_rows"]]
            if ([item["finding_id"] for item in obj["items"]] != expected_rows or
                    obj["covered_claim_ids"] != expected_rows):
                raise ValueError("targeted adjudication actual assessments differ from unresolved rows")
            require_invocation_link(
                obj, response, "critic_synth", adjudicator_model, adjudicator_effort,
            )
            if markdown.read_text() != render_critic(obj):
                raise ValueError("targeted adjudication Markdown is not the canonical rendering")
            shutil.copyfile(response, staging / "CRITIQUE.json")
            shutil.copyfile(markdown, staging / "CRITIQUE.md")
            shutil.copyfile(sidecar, staging / sidecar.name)
        elif args.target_input_sha:
            raise ValueError("target input SHA is only valid for completed targeted adjudication")
        else:
            forbidden = (
                results / "TARGETED_ADJUDICATION.json",
                results / "TARGETED_ADJUDICATION.md",
                results / "targeted_adjudicator.complete.json",
            )
            if any(path.exists() or path.is_symlink() for path in forbidden):
                raise ValueError("no-target projection contains targeted adjudication artifacts")

        for path, name in (
            (pathlib.Path(args.routing), "ROUTING.json"),
            (packet_path, "adjudication_packet.json"),
            (merge_path, "CLAIM_MERGE.json"),
        ):
            if not path.is_file():
                raise ValueError(f"projection metadata is missing: {path}")
            shutil.copyfile(path, staging / name)
        file_hashes = {
            path.relative_to(staging).as_posix(): sha_file(path)
            for path in sorted(staging.rglob("*")) if path.is_file()
        }
        atomic_json(staging / "MANIFEST.json", {
            "projection_version": "i4r-round-projection-1",
            "invocation_id": args.invocation,
            "audit_id": args.audit_id,
            "round": args.round,
            "input_sha256": args.input_sha,
            "report_sha256": args.report_sha,
            "roles": roles,
            "adjudication": args.adjudication,
            "target_input_sha256": args.target_input_sha or None,
            "claim_assessments": claim_assessments,
            "rereviewed_claim_ids": list(dict.fromkeys(
                claim_id for role in roles for claim_id in claim_assessments[role]
            )),
            "claim_merge_sha256": sha_file(merge_path),
            "files_sha256": file_hashes,
        })
        make_read_only(staging)
        os.rename(staging, target)
        print(json.dumps({"projection": str(target), "invocation_id": args.invocation}, sort_keys=True))
        return 0
    finally:
        if staging.exists():
            discard_tree(staging)


def exact_keys(obj: Any, keys: set[str], where: str) -> None:
    if not isinstance(obj, dict) or set(obj) != keys:
        got = sorted(obj) if isinstance(obj, dict) else type(obj).__name__
        raise ValueError(f"{where} keys differ: {got}")


def nonempty(value: Any, where: str, minimum: int = 1) -> None:
    if not isinstance(value, str) or len(value.strip()) < minimum:
        raise ValueError(f"{where} must be a non-empty string")


def validate_critic_obj(obj: Any, args: argparse.Namespace) -> None:
    schema_errors = validate_json_schema.errors_for_value(obj, CRITIC_SCHEMA)
    if schema_errors:
        raise ValueError("canonical critic JSON Schema errors: " + "; ".join(schema_errors[:20]))
    try:
        validate_v2.validate_critic_result(obj)
    except validate_v2.ValidationError as exc:
        raise ValueError(f"canonical critic semantic error: {exc}") from exc
    contract_path = getattr(args, "schema", "")
    if contract_path:
        contract_errors = validate_json_schema.errors_for_value(obj, pathlib.Path(contract_path))
        if contract_errors:
            raise ValueError("specialized critic contract errors: " + "; ".join(contract_errors[:20]))
    root_keys = {
        "schema_version", "audit_id", "invocation_id", "round", "role", "report_sha256",
        "input_hashes", "status", "completion_marker", "completeness_declaration", "coverage",
        "covered_claim_ids", "conflicts", "settled_spot_checks", "items", "new_issues", "overall",
    }
    exact_keys(obj, root_keys, "root")
    if obj["schema_version"] != "2.0.0" or obj["status"] != "complete":
        raise ValueError("v2 output status is not complete")
    if obj["completion_marker"] is not True:
        raise ValueError("complete output lacks completion_marker=true")
    if obj["audit_id"] != args.audit_id:
        raise ValueError("audit id mismatch")
    if obj["role"] != f"critic_{args.role}" or obj["round"] != args.round:
        raise ValueError("role or round mismatch")
    if obj["report_sha256"] != args.report_sha:
        raise ValueError("report hash mismatch")
    exact_keys(obj["input_hashes"], {
        "snapshot_sha256", "report_sha256", "prompt_sha256", "policy_sha256",
        "evidence_manifest_sha256",
    }, "input_hashes")
    if obj["input_hashes"]["report_sha256"] != args.report_sha:
        raise ValueError("input_hashes.report_sha256 mismatch")
    expected_input = getattr(args, "input_sha", "")
    if expected_input and obj["input_hashes"]["snapshot_sha256"] != expected_input:
        raise ValueError("input_hashes.snapshot_sha256 mismatch")
    for name, digest in obj["input_hashes"].items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"input_hashes.{name} is not lowercase SHA-256")

    exact_keys(obj["completeness_declaration"], {
        "all_assigned_checks_accounted_for", "all_assessments_evidenced",
        "unresolved_omissions",
    }, "completeness_declaration")
    declaration = obj["completeness_declaration"]
    if declaration["all_assigned_checks_accounted_for"] is not True:
        raise ValueError("complete output must account for every assigned check")
    if declaration["all_assessments_evidenced"] is not True:
        raise ValueError("complete output must declare every assessment evidenced")
    if declaration["unresolved_omissions"] != []:
        raise ValueError("complete output cannot declare unresolved omissions")

    exact_keys(obj["coverage"], {"assigned_checks", "completed_checks", "skipped_checks"}, "coverage")
    coverage = obj["coverage"]
    assigned = coverage["assigned_checks"]
    completed = coverage["completed_checks"]
    skipped = coverage["skipped_checks"]
    if not isinstance(assigned, list) or not assigned:
        raise ValueError("assigned_checks must be nonempty; a skeleton is not complete")
    if not isinstance(completed, list) or not isinstance(skipped, list):
        raise ValueError("coverage arrays are malformed")
    if skipped:
        raise ValueError("skipped checks are unsupported and cannot count as completed review")
    for name, values in (("assigned_checks", assigned), ("completed_checks", completed)):
        for index, value in enumerate(values):
            nonempty(value, f"coverage.{name}[{index}]")
        if len(values) != len(set(values)):
            raise ValueError(f"coverage.{name} contains duplicates")
    skipped_names = []
    for index, row in enumerate(skipped):
        exact_keys(row, {"check", "reason"}, f"coverage.skipped_checks[{index}]")
        nonempty(row["check"], f"coverage.skipped_checks[{index}].check")
        nonempty(row["reason"], f"coverage.skipped_checks[{index}].reason")
        skipped_names.append(row["check"])
    if set(completed) & set(skipped_names):
        raise ValueError("a check cannot be both completed and skipped")
    if set(assigned) != set(completed) | set(skipped_names):
        raise ValueError("complete output must account for every assigned check exactly")

    covered_claim_ids = obj["covered_claim_ids"]
    if not isinstance(covered_claim_ids, list):
        raise ValueError("covered_claim_ids must be an array")
    for index, claim_id in enumerate(covered_claim_ids):
        nonempty(claim_id, f"covered_claim_ids[{index}]")
    if len({value.casefold() for value in covered_claim_ids}) != len(covered_claim_ids):
        raise ValueError("covered_claim_ids contains duplicates")
    covered_claims = {value.casefold() for value in covered_claim_ids}
    if not isinstance(obj["conflicts"], list):
        raise ValueError("conflicts must be an array")
    conflict_ids = set()
    for index, row in enumerate(obj["conflicts"]):
        where = f"conflicts[{index}]"
        exact_keys(row, {"conflict_id", "claim_ids", "description"}, where)
        nonempty(row["conflict_id"], f"{where}.conflict_id")
        nonempty(row["description"], f"{where}.description")
        if row["conflict_id"].casefold() in conflict_ids:
            raise ValueError("duplicate conflict_id")
        conflict_ids.add(row["conflict_id"].casefold())
        if not isinstance(row["claim_ids"], list) or not row["claim_ids"]:
            raise ValueError(f"{where}.claim_ids must be nonempty")
        conflict_claims = set()
        for claim_index, claim_id in enumerate(row["claim_ids"]):
            nonempty(claim_id, f"{where}.claim_ids[{claim_index}]")
            conflict_claims.add(claim_id.casefold())
        if len(conflict_claims) != len(row["claim_ids"]):
            raise ValueError(f"{where}.claim_ids contains duplicates")
        if not conflict_claims <= covered_claims:
            raise ValueError(f"{where} references a claim not declared as covered")

    if not isinstance(obj["settled_spot_checks"], list):
        raise ValueError("settled_spot_checks must be an array")
    for index, row in enumerate(obj["settled_spot_checks"]):
        exact_keys(row, {"fact_id", "status", "evidence"}, f"settled_spot_checks[{index}]")
        nonempty(row["fact_id"], f"settled_spot_checks[{index}].fact_id")
        nonempty(row["evidence"], f"settled_spot_checks[{index}].evidence")
        if row["status"] not in ("passed", "failed"):
            raise ValueError("unsupported settled spot-check status")

    assessment_keys = {
        "item_id", "finding_id", "claim", "verdict", "priority", "confidence", "evidence",
        "unverifiable_basis", "requested_correction",
    }
    evidence_keys = {"artifact_refs", "operation", "result", "reproducible_script"}
    basis_keys = {"missing_or_inaccessible", "attempted", "effect_on_verdict"}
    ids: list[str] = []
    assessed_claims: list[str] = []
    blocking = 0
    for collection, is_new in (("items", False), ("new_issues", True)):
        rows = obj[collection]
        if not isinstance(rows, list):
            raise ValueError(f"{collection} must be an array")
        for index, row in enumerate(rows):
            where = f"{collection}[{index}]"
            exact_keys(row, assessment_keys, where)
            nonempty(row["item_id"], f"{where}.item_id")
            nonempty(row["claim"], f"{where}.claim")
            if is_new:
                if row["finding_id"] is not None:
                    raise ValueError(f"{where}.finding_id must be null for a new issue")
            else:
                nonempty(row["finding_id"], f"{where}.finding_id")
                if row["finding_id"].casefold() not in covered_claims:
                    raise ValueError(f"{where}.finding_id is not declared in covered_claim_ids")
            if row["verdict"] not in VERDICTS or row["priority"] not in PRIORITIES:
                raise ValueError(f"unsupported verdict or priority in {where}")
            if row["confidence"] not in CONFIDENCE:
                raise ValueError(f"unsupported confidence in {where}")
            exact_keys(row["evidence"], evidence_keys, f"{where}.evidence")
            evidence = row["evidence"]
            if not isinstance(evidence["artifact_refs"], list) or not evidence["artifact_refs"]:
                raise ValueError(f"{where}.evidence.artifact_refs must be nonempty")
            for ref_index, ref in enumerate(evidence["artifact_refs"]):
                ref_where = f"{where}.evidence.artifact_refs[{ref_index}]"
                exact_keys(ref, {"path", "sha256"}, ref_where)
                safe_relative_path(ref["path"], f"{ref_where}.path")
                if not isinstance(ref["sha256"], str) or not re.fullmatch(
                        r"[0-9a-f]{64}", ref["sha256"]):
                    raise ValueError(f"{ref_where}.sha256 is not lowercase SHA-256")
            nonempty(evidence["operation"], f"{where}.evidence.operation")
            nonempty(evidence["result"], f"{where}.evidence.result")
            if evidence["reproducible_script"] is not None:
                script = evidence["reproducible_script"]
                exact_keys(script, {"path", "sha256"}, f"{where}.evidence.reproducible_script")
                safe_relative_path(script["path"],
                                   f"{where}.evidence.reproducible_script.path")
                if not isinstance(script["sha256"], str) or not re.fullmatch(
                        r"[0-9a-f]{64}", script["sha256"]):
                    raise ValueError(
                        f"{where}.evidence.reproducible_script.sha256 is not lowercase SHA-256")
            if row["verdict"] == "UNVERIFIABLE":
                exact_keys(row["unverifiable_basis"], basis_keys, f"{where}.unverifiable_basis")
                for key in basis_keys:
                    nonempty(row["unverifiable_basis"][key], f"{where}.unverifiable_basis.{key}")
            elif row["unverifiable_basis"] is not None:
                raise ValueError(f"{where}.unverifiable_basis must be null")
            if row["verdict"] in ("REFUTED", "OVERSTATED"):
                nonempty(row["requested_correction"], f"{where}.requested_correction")
            elif row["requested_correction"] is not None:
                nonempty(row["requested_correction"], f"{where}.requested_correction")
            ids.append(row["item_id"].casefold())
            if not is_new:
                assessed_claims.append(row["finding_id"].casefold())
            blocking += int(row["priority"] == "BLOCKING")
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate item_id across items and new_issues")
    if len(assessed_claims) != len(set(assessed_claims)):
        raise ValueError("each covered claim must have exactly one claim-level assessment")
    if set(assessed_claims) != covered_claims:
        missing = sorted(covered_claims - set(assessed_claims))
        raise ValueError(
            "covered_claim_ids must have exactly one complete assessment"
            + (": missing " + ", ".join(missing) if missing else "")
        )

    exact_keys(obj["overall"], {"publishable", "blocking_count", "summary"}, "overall")
    overall = obj["overall"]
    if not isinstance(overall["publishable"], bool):
        raise ValueError("overall.publishable must be boolean")
    if not isinstance(overall["blocking_count"], int) or isinstance(overall["blocking_count"], bool):
        raise ValueError("overall.blocking_count must be an integer")
    if overall["blocking_count"] != blocking:
        raise ValueError("overall.blocking_count differs from assessments")
    if overall["publishable"] != (blocking == 0):
        raise ValueError("overall.publishable must be false exactly when blocking items exist")
    nonempty(overall["summary"], "overall.summary", 20)
    result_path = getattr(args, "result_path", "")
    if result_path:
        verify_evidence_bindings(
            obj, pathlib.Path(result_path), args.round,
            obj["input_hashes"]["snapshot_sha256"],
        )


def render_critic(obj: dict[str, Any]) -> str:
    role, round_number = obj["role"], obj["round"]
    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        f"# Independent critique — {role} — round {round_number}", "",
        "## Overall verdict", "", obj["overall"]["summary"], "",
        "**Publishable as-is:** " + ("yes" if obj["overall"]["publishable"] else "no"), "",
        "## Per-item review", "",
        "| Item | Finding | Verdict | Priority | Claim |", "|---|---|---|---|---|",
    ]
    assessments = obj["items"] + obj["new_issues"]
    if assessments:
        for item in assessments:
            lines.append(
                f"| {cell(item['item_id'])} | {cell(item['finding_id'] or 'new')} | "
                f"{item['verdict']} | {item['priority']} | {cell(item['claim'])} |"
            )
    else:
        lines.append("| — | — | — | — | No verified issue was established. |")
    disputed = [x for x in assessments if x["verdict"] != "CONFIRMED" or x["priority"] == "BLOCKING"]
    if disputed:
        lines += ["", "## Evidence for disputed or blocking items", ""]
        for item in disputed:
            evidence = item["evidence"]
            lines += [
                f"### {item['item_id']}", "", f"**Operation:** {evidence['operation']}", "",
                f"**Result:** {evidence['result']}", "",
                "**Artifacts:** " + ", ".join(
                    f"`{x['path']}` (`{x['sha256'][:12]}…`)"
                    for x in evidence["artifact_refs"]
                ), "",
                "**Requested correction:** " + (item["requested_correction"] or "None."), "",
            ]
    lines += ["## Coverage", ""]
    lines += ["- Completed: " + x for x in obj["coverage"]["completed_checks"]]
    lines += [f"- Skipped: {x['check']} — {x['reason']}" for x in obj["coverage"]["skipped_checks"]]
    confirmed = [x for x in assessments if x["verdict"] == "CONFIRMED"]
    lines += ["", "## What is solid", ""]
    lines += ["- " + x["claim"] for x in confirmed] or ["No assessed claim required an affirmative confirmation."]
    lines += ["", "<!-- I4R_CRITIC_COMPLETE -->"]
    return "\n".join(lines).rstrip() + "\n"


def command_validate_critic(args: argparse.Namespace) -> int:
    obj = load(args.input)
    args.result_path = args.input
    validate_critic_obj(obj, args)
    path = pathlib.Path(args.markdown)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(f"{path}.tmp.{os.getpid()}")
    tmp.write_text(render_critic(obj))
    os.replace(tmp, path)
    return 0


def command_complete(args: argparse.Namespace) -> int:
    markdown = pathlib.Path(args.markdown)
    if not markdown.read_text().rstrip().endswith("<!-- I4R_CRITIC_COMPLETE -->"):
        raise ValueError("Markdown completion sentinel is missing")
    atomic_json(
        args.output,
        {
            "completion_version": "i4r-runtime-completion-2",
            "audit_id": args.audit_id,
            "input_sha256": args.input_sha,
            "report_sha256": args.report_sha,
            "round": args.round,
            "role": args.role,
            "attempts": args.attempts,
            "completion_marker": True,
            "output_sha256": sha_file(args.response),
            "markdown_sha256": sha_file(markdown),
        },
    )
    return 0


def validate_completion(
    sidecar_path: pathlib.Path, response_path: pathlib.Path, markdown_path: pathlib.Path,
    *, audit_id: str, input_sha: str, report_sha: str, round_number: int, role: str,
) -> None:
    sidecar = load(sidecar_path)
    exact_keys(sidecar, {
        "completion_version", "audit_id", "input_sha256", "report_sha256", "round", "role",
        "attempts", "completion_marker", "output_sha256", "markdown_sha256",
    }, "completion sidecar")
    expected = {
        "completion_version": "i4r-runtime-completion-2", "audit_id": audit_id,
        "input_sha256": input_sha, "report_sha256": report_sha, "round": round_number,
        "role": role, "completion_marker": True,
    }
    if any(sidecar.get(key) != value for key, value in expected.items()):
        raise ValueError("completion sidecar identity mismatch")
    if not isinstance(sidecar["attempts"], int) or sidecar["attempts"] < 0:
        raise ValueError("completion attempt count is invalid")
    if sidecar["output_sha256"] != sha_file(response_path):
        raise ValueError("completed JSON hash mismatch")
    if sidecar["markdown_sha256"] != sha_file(markdown_path):
        raise ValueError("completed Markdown hash mismatch")
    if not markdown_path.read_text().rstrip().endswith("<!-- I4R_CRITIC_COMPLETE -->"):
        raise ValueError("completed Markdown sentinel is missing")


def command_verify_complete(args: argparse.Namespace) -> int:
    response, markdown = pathlib.Path(args.response), pathlib.Path(args.markdown)
    validate_completion(
        pathlib.Path(args.sidecar), response, markdown, audit_id=args.audit_id,
        input_sha=args.input_sha, report_sha=args.report_sha, round_number=args.round, role=args.role,
    )
    obj = load(response)
    args.result_path = str(response)
    validate_critic_obj(obj, args)
    if markdown.read_text() != render_critic(obj):
        raise ValueError("Markdown is not the deterministic rendering of completed JSON")
    return 0


def walk_telemetry(
    value: Any, usage: dict[str, int], extras: dict[str, int],
    models: list[str], sessions: list[str],
) -> None:
    usage_keys = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cached_input_tokens": "cached_input_tokens",
        "cache_read_input_tokens": "cached_input_tokens",
        "reasoning_tokens": "reasoning_tokens",
        "reasoning_output_tokens": "reasoning_tokens",
        "total_tokens": "total_tokens",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if key in usage_keys and isinstance(item, (int, float)):
                normalized = usage_keys[key]
                usage[normalized] = max(int(item), usage.get(normalized, 0))
            if key == "cache_write_input_tokens" and isinstance(item, (int, float)):
                extras[key] = max(int(item), extras.get(key, 0))
            if key in ("model", "model_id") and isinstance(item, str) and len(item) < 100:
                models.append(item)
            if key in ("thread_id", "session_id", "conversation_id") and isinstance(item, str):
                sessions.append(item)
            walk_telemetry(item, usage, extras, models, sessions)
    elif isinstance(value, list):
        for item in value:
            walk_telemetry(item, usage, extras, models, sessions)


def model_family(model: str | None) -> str:
    value = (model or "").lower()
    if value.startswith(("gpt-", "chatgpt-")):
        return "gpt"
    if value.startswith("o") and len(value) > 1 and value[1].isdigit():
        return "openai-o-series"
    if "claude" in value or "opus" in value or "sonnet" in value or "haiku" in value:
        return "claude"
    if "gemini" in value:
        return "gemini"
    return value.split("-")[0] if value else "unknown"


def append_locked(paths: list[pathlib.Path], row: dict[str, Any]) -> None:
    line = (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode()
    lock = pathlib.Path(str(paths[-1]) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def command_telemetry(args: argparse.Namespace) -> int:
    usage: dict[str, int] = {}
    usage_extras: dict[str, int] = {}
    models: list[str] = []
    sessions: list[str] = []
    parsed_events: list[dict[str, Any]] = []
    if args.events and pathlib.Path(args.events).exists():
        for line in pathlib.Path(args.events).read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    parsed_events.append(event)
            except Exception:
                continue
    terminal = [event for event in parsed_events if event.get("type") in {
        "turn.completed", "turn.failed", "turn.cancelled",
    }]
    for event in parsed_events:
        if event.get("type") == "thread.started":
            walk_telemetry(
                {key: event.get(key) for key in ("thread_id", "session_id", "model", "model_id")},
                {}, {}, models, sessions,
            )
    if terminal:
        for event in terminal:
            walk_telemetry(event.get("usage") or {}, usage, usage_extras, models, sessions)
            for key in ("model", "model_id"):
                if isinstance(event.get(key), str):
                    models.append(event[key])
    else:
        for event in parsed_events:
            walk_telemetry(event, usage, usage_extras, models, sessions)
    response_sha = sha_file(args.response) if args.response and pathlib.Path(args.response).is_file() else None
    prompt_sha = sha_file(args.prompt) if args.prompt and pathlib.Path(args.prompt).is_file() else None
    workspace = pathlib.Path(args.workspace).resolve()

    def relative(path: str | None) -> str | None:
        if not path:
            return None
        try:
            return str(pathlib.Path(path).resolve().relative_to(workspace))
        except Exception:
            return str(path)

    canonical_usage = {
        "obtainable": bool(usage),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cached_input_tokens": usage.get("cached_input_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "source": "cli_log" if usage else "unavailable",
    }
    role = (
        f"critic_{args.role}" if args.role in ROLES
        else "critic_synth" if args.role == "targeted_adjudicator"
        else args.role
    )
    role_class = (
        "critic" if args.role in ROLES
        else "adjudicator" if args.role == "targeted_adjudicator"
        else "other"
    )
    completed = args.outcome in ("complete", "cache_hit")
    observed_model = models[-1] if models else (args.model if completed else None)
    model_source = "observed" if models else ("declared" if completed else "unavailable")
    error_text = None
    if not completed:
        stderr_tail = ""
        if args.stderr and pathlib.Path(args.stderr).exists():
            stderr_tail = " | ".join(pathlib.Path(args.stderr).read_text(errors="replace").splitlines()[-5:])
        error_text = (args.failure_class + (": " + stderr_tail if stderr_tail else "")) or "unknown failure"
    local_usage = dict(canonical_usage)
    local_usage["cache_write_input_tokens"] = usage_extras.get("cache_write_input_tokens")
    local_row = {
        "telemetry_version": "i4r-invocation-1",
        "invocation_id": args.invocation,
        "call_id": args.call_id,
        "input_sha256": args.input_sha,
        "round": args.round,
        "role": args.role,
        "attempt": args.attempt,
        "provider_call": bool(args.provider_call),
        "outcome": args.outcome,
        "failure_class": args.failure_class or None,
        "exit_code": args.exit_code,
        "requested_model": args.model,
        "requested_effort": args.effort,
        "served_model": observed_model,
        "session_id": sessions[-1] if sessions else None,
        "started_at": args.started,
        "ended_at": args.ended,
        "elapsed_seconds": args.elapsed,
        "usage": local_usage,
        "response_sha256": response_sha,
        "prompt_sha256": prompt_sha,
        "cache_usage": args.cache_usage,
        "events_path": relative(args.events),
        "response_path": relative(args.response),
        "stderr_path": relative(args.stderr),
    }
    canonical_status = (
        "completed" if completed else "cancelled" if args.outcome == "cancelled"
        else "timed_out" if args.outcome == "timed_out" else "failed"
    )
    canonical_row = {
        "invocation_id": args.call_id,
        "role": role,
        "role_class": role_class,
        "round": args.round,
        "attempt": max(1, args.attempt),
        "status": canonical_status,
        "provider": "openai",
        "provider_call": bool(args.provider_call),
        "cache_usage": args.cache_usage,
        "requested_model": args.model,
        "observed_model": observed_model,
        "model_family": model_family(observed_model or args.model),
        "model_source": model_source,
        "reasoning_effort": args.effort,
        "session_id": sessions[-1] if sessions else None,
        "started_at": args.started,
        "ended_at": args.ended,
        "elapsed_seconds": args.elapsed,
        "input_artifact_sha256": args.input_sha,
        "prompt_sha256": prompt_sha,
        "output_artifact": relative(args.response) if response_sha else None,
        "output_sha256": response_sha,
        "usage": canonical_usage,
        "error_class": args.error_class or None,
        "error": error_text,
        "legacy_source": None,
    }
    scripts_dir = pathlib.Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import record_invocation
    if pathlib.Path(args.global_log).resolve() != workspace / "invocations.jsonl":
        raise ValueError("canonical telemetry path must be <audit>/invocations.jsonl")
    record_invocation.validate_record(canonical_row, workspace)
    record_invocation.append(workspace, canonical_row)
    append_locked([pathlib.Path(args.local_log)], local_row)
    return 0


def command_failure(args: argparse.Namespace) -> int:
    log = pathlib.Path(args.log)
    tail = "\n".join(log.read_text(errors="replace").splitlines()[-20:]) if log.exists() else ""
    atomic_json(
        args.output,
        {"role": args.role, "attempts": args.attempts, "failure_class": args.failure_class, "error_tail": tail},
    )
    return 0


def command_budget_used(args: argparse.Namespace) -> int:
    def integer(path: pathlib.Path) -> int:
        try:
            value = int(path.read_text().splitlines()[0])
            return value if value >= 0 else 0
        except (OSError, ValueError, IndexError):
            return 0

    counter = pathlib.Path(args.counter)
    used = integer(counter)
    legacy_root = pathlib.Path(args.legacy_root)
    if legacy_root.is_dir():
        for legacy in legacy_root.glob(f"*/{args.budget_role}.count"):
            used = max(used, integer(legacy))
    canonical_role = (
        f"critic_{args.budget_role}" if args.budget_role in ROLES
        else "critic_synth" if args.budget_role == "targeted_adjudicator"
        else args.budget_role
    )
    ledger = pathlib.Path(args.ledger)
    call_ids = set()
    if ledger.is_file():
        for line_number, line in enumerate(ledger.read_text(errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"canonical invocation ledger line {line_number} is malformed") from exc
            if (
                row.get("round") == args.round
                and row.get("role") == canonical_role
                and row.get("provider_call") is True
            ):
                call_ids.add(row.get("invocation_id"))
    print(max(used, len(call_ids)))
    return 0


def command_provider_exec(args: argparse.Namespace) -> int:
    """Exec Codex in a fresh session so the driver can reap its whole process group."""
    os.setsid()
    pid_file = pathlib.Path(args.pid_file)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = pathlib.Path(f"{pid_file}.tmp.{os.getpid()}")
    temporary.write_text(f"{os.getpid()}\n")
    os.replace(temporary, pid_file)
    argv = [
        args.codex, "exec", "--skip-git-repo-check", "--ignore-user-config", "--color", "never",
        "--json", "--ephemeral", "-m", args.model, "-c",
        f"model_reasoning_effort={args.effort}", "-s", "read-only", "-C", args.workspace,
        "--output-schema", args.schema, "-o", args.response, "-",
    ]
    os.execv(args.codex, argv)
    return 127


def command_status(args: argparse.Namespace) -> int:
    roles = args.roles.split()
    results = pathlib.Path(args.results)
    completed = []
    for role in roles:
        try:
            validate_completion(
                results / f"{role}.complete.json", results / f"critic_{role}.json",
                results / f"critic_{role}.md", audit_id=args.audit_id, input_sha=args.input_sha,
                report_sha=args.report_sha, round_number=args.round, role=role,
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
        completed.append(role)
    failed = [] if args.outcome == "running" else [r for r in roles if r not in completed]
    calls = cache_hits = 0
    telemetry = pathlib.Path(args.telemetry)
    if telemetry.exists():
        for line in telemetry.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            calls += int(bool(row.get("provider_call")))
            cache_hits += int(row.get("outcome") == "cache_hit")
    packet = results.parent / "adjudication_packet.json"
    trigger_count = len(load(packet).get("triggers", [])) if packet.exists() else 0
    obj = {
        "status_version": "i4r-round-status-2",
        "audit_id": args.audit_id,
        "invocation_id": args.invocation,
        "input_sha256": args.input_sha,
        "report_sha256": args.report_sha,
        "round": args.round,
        "pid": args.pid,
        "outcome": args.outcome,
        "exit_code": None if args.outcome == "running" else args.exit_code,
        "expected_roles": roles,
        "completed_roles": completed,
        "failed_roles": failed,
        "quorum_met": set(completed) == set(roles),
        "adjudication": args.adjudication,
        "adjudication_trigger_count": trigger_count,
        "provider_calls": calls,
        "cache_hits": cache_hits,
        "snapshot": args.snapshot,
        "started_at": args.started,
        "ended_at": args.ended or None,
        "detail": args.detail or None,
    }
    atomic_json(args.local_status, obj)
    atomic_json(args.current_status, obj)
    return 0


def adjudication_triggers(
    rows: list[tuple[str, dict[str, Any]]],
    registry_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project only the canonical automatic targeted-adjudication trigger set."""
    triggers: list[dict[str, Any]] = []
    for role, obj in rows:
        for spot_check in obj["settled_spot_checks"]:
            if spot_check["status"] == "failed":
                triggers.append({
                    "kind": "settled_fact_cache_conflict",
                    "item_id": spot_check["fact_id"],
                    "roles": [f"critic_{role}"],
                    "detail": "failed settled-fact spot check conflicts with cache: "
                              + spot_check["evidence"],
                })

    by_id: dict[str, list[tuple[str, dict[str, Any]]]] = collections.defaultdict(list)
    for role, obj in rows:
        for finding in obj["items"]:
            key = finding["finding_id"] or finding["item_id"]
            by_id[key.casefold()].append((role, finding))
    for key in sorted(by_id):
        values = by_id[key]
        verdicts = {finding["verdict"] for _, finding in values}
        if len(verdicts) > 1:
            triggers.append({
                "kind": "verdict_conflict",
                "item_id": values[0][1]["finding_id"] or values[0][1]["item_id"],
                "roles": [f"critic_{role}" for role, _ in values],
                "detail": "conflicting verdicts: " + ", ".join(sorted(verdicts)),
            })
    for key, canonical in sorted(registry_by_id.items()):
        values = by_id.get(key, [])
        if canonical["changes_conclusion"] and values and any(
                assessment["verdict"] != "CONFIRMED" for _, assessment in values):
            triggers.append({
                "kind": "disputed_conclusion_changing_claim",
                "item_id": canonical["id"],
                "roles": [f"critic_{role}" for role, _ in values],
                "detail": "conclusion-changing canonical claim has a disputed critic verdict",
            })
        if canonical["impact_rank"] >= 3:
            evidence_roles = {
                role for role, assessment in values
                if assessment.get("evidence") and assessment["evidence"].get("artifact_refs")
            }
            if len(evidence_roles) < 2:
                implicated_roles = [f"critic_{role}" for role, _ in values]
                if not implicated_roles:
                    implicated_roles = [f"critic_{role}" for role, _ in rows]
                triggers.append({
                    "kind": "high_impact_evidence_gap",
                    "item_id": canonical["id"],
                    "roles": implicated_roles,
                    "detail": (
                        f"high-impact finding has {len(evidence_roles)} independently bound "
                        "critic evidence record(s); two are required"
                    ),
                })
    return triggers


def load_registry_file(registry_path: pathlib.Path) -> dict[str, dict[str, Any]]:
    registry_by_id: dict[str, dict[str, Any]] = {}
    if not (registry_path.exists() or registry_path.is_symlink()):
        return registry_by_id
    if registry_path.is_symlink() or not registry_path.is_file():
        raise ValueError("canonical findings registry must be a regular non-symlink file")
    for line_number, line in enumerate(registry_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        finding = validate_json_schema.strict_loads(
            line, source=f"{registry_path}:line {line_number}",
        )
        schema_errors = validate_json_schema.errors_for_value(
            finding, SKILL_ROOT / "schemas/v2/finding-row.schema.json",
        )
        if schema_errors:
            raise ValueError(
                f"findings registry line {line_number} schema errors: "
                + "; ".join(schema_errors[:20])
            )
        try:
            validate_v2.validate_finding_row(finding, f"findings line {line_number}")
        except validate_v2.ValidationError as exc:
            raise ValueError(f"invalid canonical finding: {exc}") from exc
        key = finding["id"].casefold()
        if key in registry_by_id:
            raise ValueError(f"duplicate canonical finding ID: {finding['id']}")
        registry_by_id[key] = finding
    return registry_by_id


def load_registry_by_id(workspace: pathlib.Path) -> dict[str, dict[str, Any]]:
    return load_registry_file(workspace / "work/findings.jsonl")


def build_claim_merge(
    rows: list[tuple[str, dict[str, Any]]],
    registry_by_id: dict[str, dict[str, Any]],
    triggers: list[dict[str, Any]],
    *, audit_id: str, round_number: int, input_sha: str, report_sha: str,
) -> dict[str, Any]:
    """Deterministically merge role assessments without asking a model to repeat them."""
    grouped: dict[str, list[tuple[str, dict[str, Any], str]]] = collections.defaultdict(list)
    display_ids: dict[str, str] = {}
    for role, obj in rows:
        for collection in ("items", "new_issues"):
            for assessment in obj[collection]:
                claim_id = assessment["finding_id"] or assessment["item_id"]
                key = claim_id.casefold()
                display_ids.setdefault(key, claim_id)
                grouped[key].append((role, assessment, collection))
    trigger_by_claim: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for trigger in triggers:
        claim_id = trigger.get("item_id")
        if isinstance(claim_id, str) and claim_id:
            trigger_by_claim[claim_id.casefold()].append(trigger)

    role_order = {role: index for index, role in enumerate(ROLES)}
    claims: list[dict[str, Any]] = []
    for key in sorted(grouped, key=lambda item: (display_ids[item].casefold(), display_ids[item])):
        canonical = registry_by_id.get(key)
        role_results = []
        for role, assessment, collection in sorted(
                grouped[key], key=lambda row: (role_order.get(row[0], len(ROLES)),
                                               row[1]["item_id"].casefold())):
            evidence = assessment["evidence"]
            role_results.append({
                "role": f"critic_{role}",
                "assessment_kind": "claim" if collection == "items" else "new_issue",
                "item_id": assessment["item_id"],
                "claim": assessment["claim"],
                "verdict": assessment["verdict"],
                "priority": assessment["priority"],
                "confidence": assessment["confidence"],
                "evidence_bindings": {
                    "artifact_refs": evidence["artifact_refs"],
                    "reproducible_script": evidence["reproducible_script"],
                },
                "requested_correction": assessment["requested_correction"],
            })
        verdicts = sorted({row["verdict"] for row in role_results})
        explicit_conflict = any(
            key in {str(claim_id).casefold() for claim_id in conflict.get("claim_ids", [])}
            for _, obj in rows for conflict in obj["conflicts"]
        )
        matching_triggers = trigger_by_claim.get(key, [])
        corrections = sorted({
            row["requested_correction"] for row in role_results
            if row["requested_correction"] is not None
        })
        claims.append({
            "claim_id": canonical["id"] if canonical is not None else display_ids[key],
            "claim": canonical["claim"] if canonical is not None else role_results[0]["claim"],
            "source_location": canonical.get("source_location") if canonical is not None else None,
            "role_results": role_results,
            "verdicts": verdicts,
            "agreement": len(verdicts) == 1 and not explicit_conflict,
            "conflict": len(verdicts) > 1 or explicit_conflict,
            "requested_corrections": corrections,
            "trigger_ids": [trigger["trigger_id"] for trigger in matching_triggers],
            "unresolved": bool(matching_triggers),
        })
    unresolved_ids = [row["claim_id"] for row in claims if row["unresolved"]]
    return {
        "merge_version": "i4r-claim-merge-1",
        "audit_id": audit_id,
        "round": round_number,
        "input_sha256": input_sha,
        "report_sha256": report_sha,
        "roles": [f"critic_{role}" for role, _ in sorted(
            rows, key=lambda row: role_order.get(row[0], len(ROLES)))],
        "claims": claims,
        "unresolved_claim_ids": unresolved_ids,
    }


def unresolved_rows_for_packet(
    triggers: list[dict[str, Any]], merge: dict[str, Any],
) -> list[dict[str, Any]]:
    """Aggregate triggers so targeted adjudication sees one bounded row per unresolved subject."""
    merge_by_id = {row["claim_id"].casefold(): row for row in merge["claims"]}
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    display: dict[str, str] = {}
    for trigger in triggers:
        subject = trigger.get("item_id") or trigger["trigger_id"]
        key = subject.casefold()
        display.setdefault(key, subject)
        grouped[key].append(trigger)
    out = []
    for key in sorted(grouped, key=lambda value: (display[value].casefold(), display[value])):
        values = grouped[key]
        roles = sorted({role for trigger in values for role in trigger["roles"]})
        out.append({
            "row_id": display[key],
            "claim_id": display[key],
            "trigger_ids": [trigger["trigger_id"] for trigger in values],
            "trigger_kinds": [trigger["kind"] for trigger in values],
            "roles": roles,
            "details": [trigger["detail"] for trigger in values],
            "claim_merge": merge_by_id.get(key),
        })
    return out


def command_packet(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.results)
    workspace = pathlib.Path(args.workspace).resolve()
    rows = []
    for path in sorted(root.glob("critic_*.json")):
        obj = load(path)
        role = obj["role"].removeprefix("critic_")
        validate_critic_obj(
            obj,
            argparse.Namespace(
                role=role, round=args.round, audit_id=args.audit_id, report_sha=args.report_sha,
                input_sha=args.input_sha, result_path=str(path),
            ),
        )
        markdown = root / f"critic_{role}.md"
        complete = root / f"{role}.complete.json"
        validate_completion(
            complete, path, markdown, audit_id=args.audit_id, input_sha=args.input_sha,
            report_sha=args.report_sha, round_number=args.round, role=role,
        )
        rows.append((role, obj))
    if not rows:
        raise ValueError("cannot build an adjudication packet without completed critics")
    snapshot = snapshot_for_result(root / f"critic_{rows[0][0]}.json", args.round,
                                   args.input_sha)
    registry_by_id = load_registry_file(snapshot / "source/context/work/findings.jsonl")

    triggers = adjudication_triggers(rows, registry_by_id)
    if args.mode == "1" and not triggers:
        triggers.append({
            "kind": "forced", "item_id": None,
            "roles": [f"critic_{role}" for role, _ in rows],
            "detail": "targeted adjudication explicitly requested",
        })
    deduped = []
    seen = set()
    for trigger in triggers:
        signature = (trigger["kind"], trigger["item_id"], tuple(trigger["roles"]), trigger["detail"])
        if signature in seen:
            continue
        seen.add(signature)
        trigger["trigger_id"] = f"T{len(deduped) + 1}"
        deduped.append(trigger)
    merge = build_claim_merge(
        rows, registry_by_id, deduped, audit_id=args.audit_id, round_number=args.round,
        input_sha=args.input_sha, report_sha=args.report_sha,
    )
    merge_path = pathlib.Path(args.output).with_name("CLAIM_MERGE.json")
    atomic_json(merge_path, merge)
    unresolved_rows = unresolved_rows_for_packet(deduped, merge)
    critic_outputs = []
    for role, _ in rows:
        json_path = root / f"critic_{role}.json"
        markdown_path = root / f"critic_{role}.md"
        complete_path = root / f"{role}.complete.json"
        paths = [json_path, markdown_path, complete_path]
        relatives = []
        for path in paths:
            path = within(workspace, path, "critic output")
            relatives.append(path.relative_to(workspace).as_posix())
        critic_outputs.append({
            "role": f"critic_{role}",
            "json_path": relatives[0],
            "json_sha256": sha_file(json_path),
            "markdown_path": relatives[1],
            "markdown_sha256": sha_file(markdown_path),
            "completion_path": relatives[2],
            "completion_sha256": sha_file(complete_path),
        })
    atomic_json(
        args.output,
        {
            "packet_version": "i4r-targeted-adjudication-packet-2",
            "audit_id": args.audit_id,
            "round": args.round,
            "input_sha256": args.input_sha,
            "report_sha256": args.report_sha,
            "policy": args.mode,
            "triggers": deduped,
            "claim_merge_sha256": sha_file(merge_path),
            "unresolved_rows": unresolved_rows,
            "critic_outputs": critic_outputs,
        },
    )
    print(len(deduped))
    return 0


def command_validate_adjudication(args: argparse.Namespace) -> int:
    obj, packet = load(args.input), load(args.packet)
    validate_critic_obj(
        obj,
        argparse.Namespace(
            role="synth", round=packet["round"], audit_id=packet["audit_id"],
            report_sha=packet["report_sha256"], input_sha=args.input_sha,
            schema=args.schema, result_path=args.input,
        ),
    )
    expected = {x["row_id"] for x in packet["unresolved_rows"]}
    assigned = set(obj["coverage"]["assigned_checks"])
    got = {item["item_id"] for item in obj["items"] + obj["new_issues"]}
    if assigned != expected:
        raise ValueError(f"assigned trigger mismatch: expected {sorted(expected)}, got {sorted(assigned)}")
    if got != expected:
        raise ValueError(f"trigger coverage mismatch: expected {sorted(expected)}, got {sorted(got)}")
    if set(obj["covered_claim_ids"]) != expected:
        raise ValueError(
            f"covered claim mismatch: expected {sorted(expected)}, "
            f"got {sorted(obj['covered_claim_ids'])}"
        )
    path = pathlib.Path(args.markdown)
    tmp = pathlib.Path(f"{path}.tmp.{os.getpid()}")
    tmp.write_text(render_critic(obj))
    os.replace(tmp, path)
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    subs = root.add_subparsers(dest="command", required=True)

    p = subs.add_parser("route")
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--override", default="")
    p.add_argument("--manifest", default="")
    p.add_argument("--output", required=True)
    p.set_defaults(func=command_route)

    p = subs.add_parser("snapshot")
    for key in ("workspace", "report", "prompts", "out", "model", "effort", "roles", "routing", "runtime_script"):
        p.add_argument("--" + key.replace("_", "-"), required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--diff-manifest", default="")
    p.set_defaults(func=command_snapshot)

    p = subs.add_parser("bind-contract")
    p.add_argument("--base", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--invocation-id", required=True)
    p.set_defaults(func=command_bind_contract)

    p = subs.add_parser("critic-equivalent")
    p.add_argument("--left", required=True)
    p.add_argument("--right", required=True)
    p.set_defaults(func=command_critic_equivalent)

    p = subs.add_parser("target-snapshot")
    for key in (
        "workspace", "out", "round_snapshot", "packet", "schema", "model", "effort",
        "runtime_script",
    ):
        p.add_argument("--" + key.replace("_", "-"), required=True)
    p.set_defaults(func=command_target_snapshot)

    p = subs.add_parser("projection")
    for key in (
        "out", "results", "invocation", "input_sha", "report_sha", "audit_id", "roles",
        "adjudication", "routing", "packet",
    ):
        p.add_argument("--" + key.replace("_", "-"), required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--target-input-sha", default="")
    p.set_defaults(func=command_projection)

    p = subs.add_parser("validate-critic")
    p.add_argument("--input", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--input-sha", required=True)
    p.add_argument("--report-sha", required=True)
    p.add_argument("--audit-id", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--markdown", required=True)
    p.set_defaults(func=command_validate_critic)

    p = subs.add_parser("complete")
    p.add_argument("--output", required=True)
    p.add_argument("--input-sha", required=True)
    p.add_argument("--report-sha", required=True)
    p.add_argument("--audit-id", required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--attempts", type=int, required=True)
    p.add_argument("--response", required=True)
    p.add_argument("--markdown", required=True)
    p.set_defaults(func=command_complete)

    p = subs.add_parser("verify-complete")
    p.add_argument("--sidecar", required=True)
    p.add_argument("--response", required=True)
    p.add_argument("--markdown", required=True)
    p.add_argument("--input-sha", required=True)
    p.add_argument("--report-sha", required=True)
    p.add_argument("--audit-id", required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--role", required=True)
    p.set_defaults(func=command_verify_complete)

    p = subs.add_parser("telemetry")
    p.add_argument("--local-log", required=True)
    p.add_argument("--global-log", required=True)
    p.add_argument("--events", default="")
    p.add_argument("--response", default="")
    p.add_argument("--stderr", default="")
    p.add_argument("--prompt", default="")
    p.add_argument("--invocation", required=True)
    p.add_argument("--call-id", required=True)
    p.add_argument("--input-sha", required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--attempt", type=int, required=True)
    p.add_argument("--provider-call", type=int, choices=(0, 1), required=True)
    p.add_argument("--outcome", required=True)
    p.add_argument("--failure-class", default="")
    p.add_argument("--error-class", default="")
    p.add_argument(
        "--cache-usage", choices=("none", "read", "write", "read_write", "probe_cache"),
        required=True,
    )
    p.add_argument("--exit-code", type=int, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--effort", required=True)
    p.add_argument("--started", required=True)
    p.add_argument("--ended", required=True)
    p.add_argument("--elapsed", type=int, required=True)
    p.add_argument("--workspace", required=True)
    p.set_defaults(func=command_telemetry)

    p = subs.add_parser("failure")
    p.add_argument("--output", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--attempts", type=int, required=True)
    p.add_argument("--failure-class", required=True)
    p.add_argument("--log", required=True)
    p.set_defaults(func=command_failure)

    p = subs.add_parser("budget-used")
    p.add_argument("--ledger", required=True)
    p.add_argument("--counter", required=True)
    p.add_argument("--legacy-root", required=True)
    p.add_argument("--budget-role", required=True)
    p.add_argument("--round", type=int, required=True)
    p.set_defaults(func=command_budget_used)

    p = subs.add_parser("provider-exec")
    for key in ("codex", "model", "effort", "workspace", "schema", "response", "pid-file"):
        p.add_argument("--" + key, required=True)
    p.set_defaults(func=command_provider_exec)

    p = subs.add_parser("status")
    p.add_argument("--local-status", required=True)
    p.add_argument("--current-status", required=True)
    p.add_argument("--results", required=True)
    p.add_argument("--telemetry", required=True)
    p.add_argument("--invocation", required=True)
    p.add_argument("--input-sha", required=True)
    p.add_argument("--report-sha", required=True)
    p.add_argument("--audit-id", required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--outcome", choices=("running", "complete", "failed"), required=True)
    p.add_argument("--exit-code", type=int, default=0)
    p.add_argument("--roles", required=True)
    p.add_argument("--adjudication", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--started", required=True)
    p.add_argument("--ended", default="")
    p.add_argument("--detail", default="")
    p.set_defaults(func=command_status)

    p = subs.add_parser("packet")
    p.add_argument("--results", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--input-sha", required=True)
    p.add_argument("--report-sha", required=True)
    p.add_argument("--audit-id", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--mode", choices=("auto", "0", "1"), required=True)
    p.set_defaults(func=command_packet)

    p = subs.add_parser("validate-adjudication")
    p.add_argument("--input", required=True)
    p.add_argument("--packet", required=True)
    p.add_argument("--input-sha", required=True)
    p.add_argument("--schema", default="")
    p.add_argument("--markdown", required=True)
    p.set_defaults(func=command_validate_adjudication)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"runtime error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
