#!/usr/bin/env python3
"""Create and verify a hash-bound finalization/registry receipt.

The receipt is not a signature.  It is a fail-closed transaction record that makes
post-finalize edits, half-filed registry pairs, stale document builds, and fabricated
"complete" flags detectable before publication.
"""
from __future__ import annotations

import argparse
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


HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
import validate_json_schema

RECEIPT = "finalization_receipt.json"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,199}$")


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def strict_json(path: pathlib.Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"required regular file is missing or symlinked: {path}")
    return validate_json_schema.strict_loads(path.read_text(encoding="utf-8"), source=str(path))


def contained(root: pathlib.Path, relative: str) -> pathlib.Path:
    if not isinstance(relative, str) or not relative or pathlib.PurePosixPath(relative).is_absolute():
        raise ValueError(f"unsafe relative artifact path: {relative!r}")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"artifact path escapes audit root: {relative}") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"receipt artifact is missing or symlinked: {relative}")
    return path


def atomic_bytes(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: pathlib.Path, value: Any) -> None:
    atomic_bytes(path, (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode())


def document_binding(root: pathlib.Path, *, required: bool) -> tuple[dict[str, Any] | None, list[str]]:
    path = root / "document_build_manifest.json"
    if not path.exists():
        if required:
            raise ValueError("document_build_manifest.json is required for publication")
        return None, []
    manifest = strict_json(path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "2.0.0":
        raise ValueError("invalid document-build manifest version")
    bound: list[str] = ["document_build_manifest.json"]
    for label in ("report", "pdf"):
        item = manifest.get(label)
        if not isinstance(item, dict):
            raise ValueError(f"document-build manifest lacks {label} binding")
        artifact = contained(root, item.get("path"))
        if item.get("sha256") != sha256(artifact):
            raise ValueError(f"document-build manifest {label} hash is stale")
        bound.append(item["path"])
    docx = manifest.get("docx")
    if docx is not None:
        if not isinstance(docx, dict):
            raise ValueError("document-build manifest docx binding is malformed")
        artifact = contained(root, docx.get("path"))
        if docx.get("sha256") != sha256(artifact):
            raise ValueError("document-build manifest docx hash is stale")
        bound.append(docx["path"])
    if (manifest["report"].get("path") != "report/REPORT_final.md" or
            not str(manifest["pdf"].get("path", "")).startswith("report/")):
        raise ValueError("document build must bind REPORT_final.md to a PDF under report/")
    return manifest, bound


def artifact_paths(root: pathlib.Path, run: dict[str, Any]) -> list[str]:
    base = [
        "run.json", "comment.json", "verification_manifest.json",
        "report/REPORT_final.md",
    ]
    if run.get("schema_version") == "2.0.0":
        base += [
            "pipeline-policy.snapshot.json", "invocations.jsonl",
            "work/findings.jsonl", "work/finding_projections.manifest.json",
        ]
    claims = [path for path in root.glob("claims_script.*") if path.is_file() and not path.is_symlink()]
    if run.get("schema_version") == "2.0.0" and len(claims) != 1:
        raise ValueError(f"expected exactly one regular claims_script.*, found {len(claims)}")
    if len(claims) > 1:
        raise ValueError(f"expected at most one regular claims_script.* for legacy audit, found {len(claims)}")
    if claims:
        base.append(claims[0].relative_to(root).as_posix())
    _, document_paths = document_binding(root, required=False)
    base.extend(document_paths)
    paths = sorted(set(base))
    for relative in paths:
        contained(root, relative)
    return paths


def validate_projection_claim_binding(root: pathlib.Path, claims_relative: str) -> None:
    projection = strict_json(root / "work/finding_projections.manifest.json")
    artifacts = projection.get("artifacts") if isinstance(projection, dict) else None
    if not isinstance(artifacts, list):
        raise ValueError("finding projection manifest has no artifact bindings")
    matches = [row for row in artifacts if isinstance(row, dict) and row.get("path") == claims_relative]
    if len(matches) != 1 or matches[0].get("sha256") != sha256(root / claims_relative):
        raise ValueError("claims script is not exactly bound by the finding projection manifest")


def registry_file(root: pathlib.Path, registry: pathlib.Path, run: dict[str, Any],
                  comment: dict[str, Any]) -> dict[str, Any]:
    audit_id = run.get("audit_id")
    if not isinstance(audit_id, str) or not SAFE_ID.fullmatch(audit_id):
        raise ValueError(f"unsafe audit_id for registry path: {audit_id!r}")
    registry.mkdir(parents=True, exist_ok=True)
    if registry.is_symlink():
        raise ValueError("registry root may not be a symlink")
    lock_path = registry / ".registry.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        index_path = registry / "index.jsonl"
        index_rows: list[dict[str, Any]] = []
        if index_path.exists():
            if index_path.is_symlink() or not index_path.is_file():
                raise ValueError("registry index is not a regular file")
            for number, line in enumerate(index_path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                row = validate_json_schema.strict_loads(line, source=f"{index_path}:{number}")
                if not isinstance(row, dict) or not isinstance(row.get("audit_id"), str):
                    raise ValueError(f"registry index line {number} is not a valid audit row")
                if row["audit_id"] != audit_id:
                    index_rows.append(row)

        audit_dir = registry / audit_id
        versions = audit_dir / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        pair_hash = hashlib.sha256((sha256(root / "run.json") + sha256(root / "comment.json")).encode()).hexdigest()
        version = f"{stamp}-{pair_hash[:12]}"
        temporary = pathlib.Path(tempfile.mkdtemp(prefix=".registry-version.", dir=versions))
        final_version = versions / version
        try:
            for name in ("run.json", "comment.json"):
                shutil.copyfile(root / name, temporary / name)
                with (temporary / name).open("rb") as handle:
                    os.fsync(handle.fileno())
            pair_manifest = {
                "schema_version": "2.0.0", "audit_id": audit_id, "version": version,
                "run_sha256": sha256(temporary / "run.json"),
                "comment_sha256": sha256(temporary / "comment.json"),
            }
            atomic_json(temporary / "pair_manifest.json", pair_manifest)
            os.rename(temporary, final_version)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        pair_manifest_hash = sha256(final_version / "pair_manifest.json")
        pointer = {
            "schema_version": "2.0.0", "audit_id": audit_id, "version": version,
            "pair_manifest_sha256": pair_manifest_hash,
        }
        audit_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(audit_dir / "current.json", pointer)
        # Compatibility mirrors are non-canonical; current.json + immutable version is the pair.
        atomic_bytes(audit_dir / "run.json", (final_version / "run.json").read_bytes())
        atomic_bytes(audit_dir / "comment.json", (final_version / "comment.json").read_bytes())
        row = {
            "audit_id": audit_id,
            "filed_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "version": version,
            "pair_manifest_sha256": pair_manifest_hash,
            "doi": run.get("paper_doi"),
            "journal": (comment.get("bibliographic") or {}).get("journal"),
            "findings_total": (comment.get("rollup") or {}).get("findings_total"),
            "rounds_run": run.get("rounds_run"),
            "stopping_reason": run.get("stopping_reason"),
            "compliance_pass": (run.get("compliance") or {}).get("compliance_pass"),
            "token_coverage": (run.get("token_accounting") or {}).get("coverage_fraction"),
        }
        index_rows.append(row)
        data = "".join(json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n" for item in index_rows)
        atomic_bytes(index_path, data.encode())
        index_row_sha256 = hashlib.sha256(
            json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return {
        "filed": True,
        # Keep the portable receipt free of workstation/account paths.  The
        # audit-local copy resolves this locator relative to the audit root;
        # the copy filed with the completed report is an archival binding and
        # therefore does not need a machine-specific absolute path.
        "root": os.path.relpath(registry.resolve(), root.resolve()),
        "current_pointer": f"{audit_id}/current.json",
        "version": version,
        "pair_manifest_sha256": pair_manifest_hash,
        "index_row_sha256": index_row_sha256,
    }


def create(root: pathlib.Path, registry: pathlib.Path | None) -> dict[str, Any]:
    root = root.resolve()
    run = strict_json(root / "run.json")
    comment = strict_json(root / "comment.json")
    if not isinstance(run, dict) or not isinstance(comment, dict):
        raise ValueError("run/comment metadata must be JSON objects")
    if run.get("audit_id") != comment.get("audit_id"):
        raise ValueError("run/comment audit IDs differ")
    if not run.get("ended_at") or run.get("status") != "complete":
        raise ValueError("run has not been successfully finalized")
    paths = artifact_paths(root, run)
    if run.get("schema_version") == "2.0.0":
        claims = next(relative for relative in paths
                      if pathlib.PurePosixPath(relative).name.startswith("claims_script."))
        validate_projection_claim_binding(root, claims)
    artifacts = [{"path": relative, "sha256": sha256(root / relative),
                  "bytes": (root / relative).stat().st_size} for relative in paths]
    registry_info = (registry_file(root, registry.resolve(), run, comment) if registry is not None
                     else {"filed": False, "root": None, "current_pointer": None,
                           "version": None, "pair_manifest_sha256": None,
                           "index_row_sha256": None})
    receipt = {
        "schema_version": "2.0.0",
        "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created_by": "finalize_run.sh/finalization_receipt.py",
        "audit_id": run["audit_id"],
        "artifacts": artifacts,
        "registry": registry_info,
    }
    atomic_json(root / RECEIPT, receipt)
    return receipt


def check(root: pathlib.Path, *, require_registry: bool, require_publication: bool) -> dict[str, Any]:
    root = root.resolve()
    receipt = strict_json(root / RECEIPT)
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version", "created_at", "created_by", "audit_id", "artifacts", "registry"
    } or receipt.get("schema_version") != "2.0.0":
        raise ValueError("finalization receipt has an invalid envelope")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("finalization receipt has no artifact bindings")
    seen: set[str] = set()
    for row in artifacts:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise ValueError("finalization receipt contains a malformed artifact row")
        relative = row["path"]
        if relative in seen:
            raise ValueError(f"duplicate finalization artifact: {relative}")
        seen.add(relative)
        path = contained(root, relative)
        if row["sha256"] != sha256(path) or row["bytes"] != path.stat().st_size:
            raise ValueError(f"finalization receipt is stale for {relative}")
    run = strict_json(root / "run.json")
    comment = strict_json(root / "comment.json")
    if receipt.get("audit_id") != run.get("audit_id") or run.get("audit_id") != comment.get("audit_id"):
        raise ValueError("receipt/run/comment audit IDs differ")
    registry = receipt.get("registry")
    if not isinstance(registry, dict) or set(registry) != {
        "filed", "root", "current_pointer", "version", "pair_manifest_sha256",
        "index_row_sha256",
    }:
        raise ValueError("finalization receipt registry binding is malformed")
    if require_registry and registry.get("filed") is not True:
        raise ValueError("audit was finalized without registry filing")
    if registry.get("filed"):
        locator = registry.get("root")
        if (not isinstance(locator, str) or not locator or
                pathlib.PurePath(locator).is_absolute()):
            raise ValueError("registry root locator must be a nonempty audit-relative path")
        registry_root = (root / locator).resolve()
        pointer_path = (registry_root / registry["current_pointer"]).resolve()
        try:
            pointer_path.relative_to(registry_root)
        except ValueError as exc:
            raise ValueError("registry pointer escapes registry root") from exc
        pointer = strict_json(pointer_path)
        if (pointer.get("audit_id") != run.get("audit_id") or
                pointer.get("version") != registry.get("version") or
                pointer.get("pair_manifest_sha256") != registry.get("pair_manifest_sha256")):
            raise ValueError("registry current pointer no longer matches the finalization receipt")
        version_dir = pointer_path.parent / "versions" / pointer["version"]
        pair_path = version_dir / "pair_manifest.json"
        if sha256(pair_path) != pointer["pair_manifest_sha256"]:
            raise ValueError("registry pair manifest hash is stale")
        pair = strict_json(pair_path)
        if (pair.get("run_sha256") != sha256(root / "run.json") or
                pair.get("comment_sha256") != sha256(root / "comment.json") or
                pair.get("run_sha256") != sha256(version_dir / "run.json") or
                pair.get("comment_sha256") != sha256(version_dir / "comment.json")):
            raise ValueError("registry immutable pair does not match current finalized metadata")
        index_matches = []
        index_path = registry_root / "index.jsonl"
        for number, line in enumerate(index_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = validate_json_schema.strict_loads(line, source=f"{index_path}:{number}")
            if isinstance(row, dict) and row.get("audit_id") == run.get("audit_id"):
                index_matches.append(row)
        if len(index_matches) != 1:
            raise ValueError("registry index does not contain exactly one row for this audit")
        row_hash = hashlib.sha256(json.dumps(index_matches[0], sort_keys=True,
                                             separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        if row_hash != registry.get("index_row_sha256"):
            raise ValueError("registry index row no longer matches the finalization receipt")
    if require_publication:
        if (run.get("status") != "complete" or not (run.get("compliance") or {}).get("compliance_pass")
                or (run.get("token_accounting") or {}).get("coverage_fraction") != 1):
            raise ValueError("finalized run is not publication-compliant with 100% telemetry")
        document, document_paths = document_binding(root, required=True)
        if not set(document_paths) <= seen:
            raise ValueError("document build happened after finalization; finalize again")
        final_pdfs = [path for path in (root / "report").glob("*.pdf")
                      if path.is_file() and not path.is_symlink()
                      and "_proxy" not in path.name and "preview" not in path.name]
        expected_pdf = contained(root, document["pdf"]["path"])
        if len(final_pdfs) != 1 or final_pdfs[0].resolve() != expected_pdf:
            raise ValueError("the sole publishable PDF is not the report-bound document build")
        claims = [path for path in seen if pathlib.PurePosixPath(path).name.startswith("claims_script.")]
        if len(claims) != 1:
            raise ValueError("receipt does not bind exactly one claims script")
        if run.get("schema_version") == "2.0.0":
            validate_projection_claim_binding(root, claims[0])
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create_parser = sub.add_parser("create")
    create_parser.add_argument("audit_dir", type=pathlib.Path)
    create_parser.add_argument("--registry", type=pathlib.Path)
    check_parser = sub.add_parser("check")
    check_parser.add_argument("audit_dir", type=pathlib.Path)
    check_parser.add_argument("--require-registry", action="store_true")
    check_parser.add_argument("--require-publication", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create(args.audit_dir, args.registry)
            print(f"finalization receipt created for {result['audit_id']}")
        else:
            result = check(args.audit_dir, require_registry=args.require_registry,
                           require_publication=args.require_publication)
            print(f"finalization receipt valid for {result['audit_id']}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
