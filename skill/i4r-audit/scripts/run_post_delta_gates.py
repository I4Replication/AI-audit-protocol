#!/usr/bin/env python3
"""Run and verify the four whole-document gates required after every delta round.

The accepted receipt is content-bound to the canonical report/findings, generated
projections, diff packet, intake manifest, policy snapshot, gate code, and exact
gate logs.  Failed attempts are written separately and are never accepted as a
completed receipt.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from typing import Any, Callable, Optional


HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
REQUIRED_GATES = ["consistency", "privacy", "checklist", "archive"]
RECEIPT_VERSION = "i4r-post-delta-gates-1"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CODE_PATHS = [
    "scripts/run_post_delta_gates.py",
    "scripts/check_consistency.sh",
    "scripts/check_release_privacy.py",
    "scripts/generate_finding_artifacts.py",
    "scripts/verify_diff_packet.py",
    "schemas/v2/post-delta-gates.schema.json",
]


class GateError(ValueError):
    """A fail-closed post-delta validation error."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def strict_json(path: pathlib.Path) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise GateError(f"{path}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise GateError(f"{path}: non-finite JSON number {value!r}")

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                          parse_constant=constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read strict JSON {path}: {exc}") from exc


def atomic_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def safe_file(root: pathlib.Path, relative: str) -> pathlib.Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative or
            pathlib.PurePosixPath(relative).is_absolute() or
            ".." in pathlib.PurePosixPath(relative).parts):
        raise GateError(f"unsafe audit-relative path: {relative!r}")
    lexical = root.joinpath(*pathlib.PurePosixPath(relative).parts)
    if lexical.is_symlink():
        raise GateError(f"symlink is not an accepted gate input: {relative}")
    resolved = lexical.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise GateError(f"gate input escapes the audit root: {relative}") from exc
    if not resolved.is_file():
        raise GateError(f"required gate input is missing: {relative}")
    return resolved


def relative_path(root: pathlib.Path, path: pathlib.Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise GateError(f"path is outside the audit root: {path}") from exc


def binding(root: pathlib.Path, relative: str) -> dict[str, Any]:
    path = safe_file(root, relative)
    return {"path": relative, "sha256": sha256_file(path), "bytes": path.stat().st_size}


def snapshot_inventory(root: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise GateError(f"snapshot contains a symlink: {path.relative_to(root)}")
        if path.is_file():
            rows.append({"path": path.relative_to(root).as_posix(),
                         "sha256": sha256_file(path), "bytes": path.stat().st_size})
    return rows


def projection_paths(root: pathlib.Path) -> list[str]:
    manifest = strict_json(safe_file(root, "work/finding_projections.manifest.json"))
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(artifacts, list) or not artifacts:
        raise GateError("finding projection manifest has no artifact inventory")
    paths = ["work/findings.jsonl", "work/finding_projections.manifest.json", "comment.json"]
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise GateError("finding projection manifest has a malformed artifact row")
        path = safe_file(root, item["path"])
        if sha256_file(path) != item.get("sha256"):
            raise GateError(f"finding projection manifest is stale: {item['path']}")
        paths.append(item["path"])
    return sorted(set(paths))


def diff_inputs(root: pathlib.Path, round_number: int) -> tuple[str, dict[str, Any]]:
    relative = f"critique/DIFF_MANIFEST_round_{round_number}.json"
    manifest_path = safe_file(root, relative)
    sys.path.insert(0, str(HERE))
    import verify_diff_packet
    try:
        value = verify_diff_packet.verify(root, manifest_path, round_number)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise GateError(f"generated diff packet is invalid: {exc}") from exc
    required = value.get("required_global_gates")
    if required != REQUIRED_GATES or len(set(required or [])) != len(REQUIRED_GATES):
        raise GateError(
            f"diff packet required_global_gates must be exactly {REQUIRED_GATES!r}"
        )
    return relative, value


def verify_archive(root: pathlib.Path) -> str:
    manifest_path = safe_file(root, "package_manifest.json")
    manifest = strict_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "2.0.0":
        raise GateError("package_manifest.json must be the canonical v2 intake manifest")
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise GateError("package_manifest.json files must be an array")
    package = root / "pkg"
    if package.is_symlink() or not package.is_dir():
        raise GateError("pkg must be a real audit-local directory")
    for directory in [package] + sorted(path for path in package.rglob("*") if path.is_dir()):
        if directory.is_symlink():
            raise GateError(f"archive package contains a symlink: {directory.relative_to(package)}")
        if stat.S_IMODE(directory.stat().st_mode) & 0o222:
            label = directory.relative_to(package).as_posix() or "."
            raise GateError(f"archive package directory is not read-only: pkg/{label}")
    observed: list[dict[str, Any]] = []
    types: Counter[str] = Counter()
    for path in sorted(package.rglob("*")):
        if path.is_symlink():
            raise GateError(f"archive package contains a symlink: {path.relative_to(package)}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise GateError(f"archive package contains a non-regular entry: {path.relative_to(package)}")
        relative = path.relative_to(package).as_posix()
        extension = path.suffix.lower().lstrip(".") or "<none>"
        types[extension] += 1
        observed.append({"path": relative, "size": path.stat().st_size,
                         "sha256": sha256_file(path), "extension": extension})
        if stat.S_IMODE(path.stat().st_mode) & 0o222:
            raise GateError(f"archive package is not read-only: pkg/{relative}")
    declared: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise GateError("package manifest contains a malformed file row")
        if item["path"] in seen:
            raise GateError(f"package manifest repeats path: {item['path']}")
        seen.add(item["path"])
        declared.append({key: item.get(key) for key in ("path", "size", "sha256", "extension")})
    if declared != observed:
        raise GateError("pkg contents do not exactly match the intake manifest")
    tree = sha256_bytes("".join(
        f"{item['path']}\0{item['sha256']}\n" for item in observed
    ).encode("utf-8"))
    if (manifest.get("package_sha256") != tree or manifest.get("file_count") != len(observed) or
            manifest.get("file_types") != dict(sorted(types.items()))):
        raise GateError("package manifest summary or package tree hash is stale")
    return f"{len(observed)} files; package_sha256={tree}"


def run_command(command: list[str]) -> tuple[int, str]:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    return result.returncode, result.stdout


def consistency_gate(snapshot: pathlib.Path) -> tuple[int, str]:
    report = snapshot / "report/REPORT_final.md"
    command = ["bash", str(HERE / "check_consistency.sh"), str(report)]
    retired = snapshot / "report/retired_claims.txt"
    if retired.is_file():
        command.append(str(retired))
    return run_command(command)


def privacy_gate(snapshot: pathlib.Path, scanned: list[str]) -> tuple[int, str]:
    sys.path.insert(0, str(HERE))
    import check_release_privacy
    issues = []
    try:
        for relative in scanned:
            path = safe_file(snapshot, relative)
            issues.extend(check_release_privacy.inspect_file(
                path, relative, identifiers=True
            ))
    except (OSError, check_release_privacy.InspectionError, GateError) as exc:
        return 1, f"FAIL: privacy inspection was incomplete: {exc}\n"
    if issues:
        return 1, check_release_privacy.format_issues(issues) + "\n"
    return 0, f"ok: {len(scanned)} release-bound artifacts passed privacy inspection\n"


def checklist_gate(root: pathlib.Path) -> tuple[int, str]:
    return run_command([
        sys.executable, str(HERE / "generate_finding_artifacts.py"), str(root), "--check"
    ])


def checklist_snapshot_gate(snapshot: pathlib.Path) -> tuple[int, str]:
    """Replay the deterministic registry projections without needing large evidence files."""
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(SKILL / "schemas/tools"))
    import generate_finding_artifacts as generator
    try:
        registry = snapshot / "work/findings.jsonl"
        rows = generator.load_rows(registry)
        generator.validate_rows(rows, SKILL / "schemas/v2/finding-row.schema.json")
        final = [row for row in rows
                 if row.get("status") in generator.FINAL_STATUSES
                 and row.get("coordinator_approved") is True
                 and (row.get("ledger_disposition") or {}).get("status") == "INCLUDED"]
        final.sort(key=generator.number)
        if [generator.number(row) for row in final] != list(range(1, len(final) + 1)):
            raise GateError("snapshot final finding IDs are not contiguous")
        rendered = generator.render(rows, final)
        expected = {
            "report/LEDGER.md": rendered["ledger"],
            "report/APPENDIX_A.md": rendered["appendix"],
            "work/claim_tags.generated.md": rendered["claim_tags"],
            "claims_map.tsv": rendered["claims_map"],
            "work/verification_plan.json": rendered["verification_plan"],
            "work/comment_findings.generated.json": rendered["comment_findings"],
            "work/settled_candidates.json": rendered["settled_candidates"],
        }
        for relative, text_value in expected.items():
            if safe_file(snapshot, relative).read_text(encoding="utf-8") != text_value:
                raise GateError(f"snapshot finding projection is divergent: {relative}")
        comment = strict_json(safe_file(snapshot, "comment.json"))
        projected_findings = [generator.comment_projection(row) for row in final]
        if comment.get("findings") != projected_findings:
            raise GateError("snapshot comment findings diverge from the registry")
        report_path = safe_file(snapshot, "report/REPORT_final.md")
        report = report_path.read_text(encoding="utf-8")
        if report != generator.inject_report(report, rendered["appendix"], rendered["claim_tags"]):
            raise GateError("snapshot report tags/Appendix diverge from the registry")
    except (OSError, UnicodeError, SystemExit, GateError) as exc:
        return 1, f"FAIL: {exc}\n"
    return 0, f"ok: {len(final)} canonical findings project exactly in the snapshot\n"


def archive_gate(root: pathlib.Path) -> tuple[int, str]:
    try:
        detail = verify_archive(root)
    except GateError as exc:
        return 1, f"FAIL: {exc}\n"
    return 0, f"ok: intake archive is exact and read-only ({detail})\n"


def copy_snapshot(root: pathlib.Path, destination: pathlib.Path, paths: list[str]) -> None:
    for relative in paths:
        source = safe_file(root, relative)
        target = destination.joinpath(*pathlib.PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        if sha256_bytes(data) != sha256_file(source):
            raise GateError(f"gate input changed while being snapshotted: {relative}")
        target.write_bytes(data)


def code_bindings() -> list[dict[str, Any]]:
    return [binding(SKILL, relative) for relative in CODE_PATHS]


def schema_check(receipt: dict[str, Any]) -> None:
    sys.path.insert(0, str(HERE))
    import validate_json_schema
    errors = validate_json_schema.errors_for_value(
        receipt, SKILL / "schemas/v2/post-delta-gates.schema.json"
    )
    if errors:
        raise GateError("post-delta receipt schema failure: " + "; ".join(errors[:20]))


def live_bindings(root: pathlib.Path, report_relative: str, findings_relative: str,
                  diff_relative: str, diff_value: dict[str, Any]) -> dict[str, Any]:
    result = {
        "report": binding(root, report_relative),
        "findings": binding(root, findings_relative),
        "diff_manifest": binding(root, diff_relative),
        "package_manifest": binding(root, "package_manifest.json"),
        "policy_snapshot": binding(root, "pipeline-policy.snapshot.json"),
    }
    for label, relative in sorted((diff_value.get("input_paths") or {}).items()):
        result[f"diff_{label}"] = binding(root, relative)
    return result


def compare_bindings(root: pathlib.Path, bindings: dict[str, Any]) -> None:
    for label, item in bindings.items():
        if not isinstance(item, dict):
            raise GateError(f"malformed live binding: {label}")
        path = safe_file(root, item.get("path"))
        if sha256_file(path) != item.get("sha256") or path.stat().st_size != item.get("bytes"):
            raise GateError(f"post-delta live binding is stale: {label}")


def gate_result(name: str, result: tuple[int, str], log_path: pathlib.Path,
                snapshot_root: pathlib.Path) -> dict[str, Any]:
    exit_status, output = result
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    return {
        "name": name,
        "status": "passed" if exit_status == 0 else "failed",
        "exit_status": exit_status,
        "log": {
            "path": log_path.relative_to(snapshot_root).as_posix(),
            "sha256": sha256_file(log_path),
            "bytes": log_path.stat().st_size,
        },
    }


def make_receipt(root: pathlib.Path, round_number: int, report: pathlib.Path) -> pathlib.Path:
    if round_number < 2:
        raise GateError("post-delta gates apply only to round 2 or later")
    report_relative = relative_path(root, report)
    if report_relative != "report/REPORT_final.md":
        raise GateError("post-delta gates must bind canonical report/REPORT_final.md")
    findings_relative = "work/findings.jsonl"
    diff_relative, diff_value = diff_inputs(root, round_number)
    scanned = projection_paths(root)
    if report_relative not in scanned:
        raise GateError("canonical report is absent from the finding-projection inventory")
    bindings_before = live_bindings(
        root, report_relative, findings_relative, diff_relative, diff_value
    )
    snapshot_paths = set(scanned)
    snapshot_paths.update(item["path"] for item in bindings_before.values())
    retired = root / "report/retired_claims.txt"
    if retired.is_file():
        snapshot_paths.add("report/retired_claims.txt")

    round_dir = root / f"critique/round_{round_number}"
    if round_dir.is_symlink() or not round_dir.is_dir():
        raise GateError(f"critique/round_{round_number} is missing or unsafe")
    parent = round_dir / "post_delta"
    parent.mkdir(parents=True, exist_ok=True)
    temporary = pathlib.Path(tempfile.mkdtemp(prefix=".gate-snapshot.", dir=parent))
    payload = temporary / "payload"
    try:
        copy_snapshot(root, payload, sorted(snapshot_paths))
        functions: dict[str, Callable[[], tuple[int, str]]] = {
            "consistency": lambda: consistency_gate(payload),
            "privacy": lambda: privacy_gate(payload, scanned),
            "checklist": lambda: checklist_gate(root),
            "archive": lambda: archive_gate(root),
        }
        results: dict[str, tuple[int, str]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {name: executor.submit(functions[name]) for name in REQUIRED_GATES}
            for name in REQUIRED_GATES:
                try:
                    results[name] = futures[name].result()
                except Exception as exc:  # fail closed, preserving the other gate logs
                    results[name] = (1, f"FAIL: gate raised {type(exc).__name__}: {exc}\n")
        gates = [gate_result(name, results[name], temporary / f"logs/{name}.log", temporary)
                 for name in REQUIRED_GATES]

        # Prove the live inputs did not change while the four independent gates ran.
        compare_bindings(root, bindings_before)
        diff_inputs(root, round_number)
        projection_paths(root)
        snapshot_rows = snapshot_inventory(temporary)
        fingerprint_payload = {
            "round": round_number,
            "bindings": bindings_before,
            "snapshot_files": snapshot_rows,
            "gate_code": code_bindings(),
            "gate_logs": {
                name: {"exit_status": results[name][0],
                       "sha256": sha256_file(temporary / f"logs/{name}.log")}
                for name in REQUIRED_GATES
            },
        }
        fingerprint = sha256_bytes(json.dumps(
            fingerprint_payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"))
        final_snapshot = parent / fingerprint
        if final_snapshot.exists():
            if snapshot_inventory(final_snapshot) != snapshot_rows:
                raise GateError("content-address collision in post-delta snapshot")
            shutil.rmtree(temporary)
        else:
            os.replace(temporary, final_snapshot)
            for path in sorted(final_snapshot.rglob("*"), key=lambda value: -len(value.parts)):
                os.chmod(path, 0o555 if path.is_dir() else 0o444)
            os.chmod(final_snapshot, 0o555)
        snapshot_relative = final_snapshot.relative_to(root).as_posix()
        gates = []
        for name in REQUIRED_GATES:
            log = final_snapshot / f"logs/{name}.log"
            gates.append({
                "name": name, "status": "passed" if results[name][0] == 0 else "failed",
                "exit_status": results[name][0],
                "log": {"path": f"logs/{name}.log", "sha256": sha256_file(log),
                        "bytes": log.stat().st_size},
            })
        run = strict_json(safe_file(root, "run.json"))
        audit_id = run.get("audit_id") if isinstance(run, dict) else None
        if not isinstance(audit_id, str) or not audit_id:
            raise GateError("run.json lacks audit_id")
        receipt = {
            "receipt_version": RECEIPT_VERSION,
            "audit_id": audit_id,
            "round": round_number,
            "required_gates": REQUIRED_GATES,
            "completed_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "snapshot": {"path": snapshot_relative, "fingerprint": fingerprint,
                         "files": snapshot_rows},
            "live_bindings": bindings_before,
            "gate_code": code_bindings(),
            "privacy_scanned_paths": scanned,
            "gates": gates,
        }
        schema_check(receipt)
        if any(item["status"] != "passed" for item in gates):
            atomic_json(round_dir / "POST_DELTA_GATES.partial.json", receipt)
            raise GateError("one or more post-delta gates failed; partial receipt is not accepted")
        accepted = round_dir / "POST_DELTA_GATES.json"
        atomic_json(accepted, receipt)
        partial = round_dir / "POST_DELTA_GATES.partial.json"
        if partial.exists():
            partial.unlink()
        return accepted
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_inventory(snapshot: pathlib.Path, declared: list[dict[str, Any]]) -> None:
    if not isinstance(declared, list):
        raise GateError("post-delta snapshot inventory must be an array")
    observed = snapshot_inventory(snapshot)
    if observed != declared:
        raise GateError("post-delta snapshot inventory is stale, incomplete, or reordered")


def verify_log(snapshot: pathlib.Path, item: dict[str, Any]) -> None:
    log = item.get("log") if isinstance(item, dict) else None
    if not isinstance(log, dict):
        raise GateError("post-delta gate lacks its log binding")
    path = safe_file(snapshot, log.get("path"))
    if sha256_file(path) != log.get("sha256") or path.stat().st_size != log.get("bytes"):
        raise GateError(f"post-delta gate log is stale: {item.get('name')}")


def rerun_current(root: pathlib.Path, receipt: dict[str, Any]) -> None:
    compare_bindings(root, receipt["live_bindings"])
    round_number = receipt["round"]
    diff_inputs(root, round_number)
    scanned = projection_paths(root)
    if scanned != receipt.get("privacy_scanned_paths"):
        raise GateError("privacy scan quorum changed after the post-delta receipt")
    checks = {
        "consistency": consistency_gate(root),
        "privacy": privacy_gate(root, scanned),
        "checklist": checklist_gate(root),
        "archive": archive_gate(root),
    }
    failed = [f"{name}: {output.strip()[:300]}" for name, (status, output) in checks.items()
              if status != 0]
    if failed:
        raise GateError("current post-delta gate replay failed: " + " | ".join(failed))


def verify_receipt(root: pathlib.Path, receipt_path: pathlib.Path,
                   require_current: bool = False) -> dict[str, Any]:
    root = root.resolve()
    receipt_path = receipt_path.resolve()
    try:
        receipt_path.relative_to(root)
    except ValueError as exc:
        raise GateError("post-delta receipt escapes the audit root") from exc
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise GateError("post-delta receipt is missing or symlinked")
    receipt = strict_json(receipt_path)
    if not isinstance(receipt, dict):
        raise GateError("post-delta receipt must be an object")
    schema_check(receipt)
    if receipt.get("required_gates") != REQUIRED_GATES:
        raise GateError("post-delta receipt does not require the canonical four gates")
    gates = receipt.get("gates")
    if ([item.get("name") for item in gates] != REQUIRED_GATES or
            any(item.get("status") != "passed" or item.get("exit_status") != 0 for item in gates)):
        raise GateError("post-delta receipt lacks the exact passing gate quorum")
    snapshot_info = receipt.get("snapshot")
    snapshot = safe_file(root, snapshot_info.get("path") + "/payload/report/REPORT_final.md").parents[2]
    # The resolved directory itself must match the receipt rather than an equivalent nested path.
    expected_snapshot = (root / snapshot_info["path"]).resolve()
    if snapshot != expected_snapshot or expected_snapshot.is_symlink():
        raise GateError("post-delta snapshot path is unsafe or inconsistent")
    if expected_snapshot.name != snapshot_info.get("fingerprint"):
        raise GateError("post-delta snapshot directory does not match its fingerprint")
    canonical_snapshot = (
        f"critique/round_{receipt['round']}/post_delta/{snapshot_info['fingerprint']}"
    )
    if snapshot_info.get("path") != canonical_snapshot:
        raise GateError("post-delta snapshot is filed under the wrong round")
    verify_inventory(expected_snapshot, snapshot_info.get("files"))
    for item in gates:
        if item.get("log", {}).get("path") != f"logs/{item['name']}.log":
            raise GateError(f"post-delta gate has a noncanonical log path: {item['name']}")
        verify_log(expected_snapshot, item)
    if receipt.get("gate_code") != code_bindings():
        raise GateError("post-delta receipt was produced by stale gate code")
    fingerprint_payload = {
        "round": receipt["round"],
        "bindings": receipt["live_bindings"],
        "snapshot_files": snapshot_info["files"],
        "gate_code": receipt["gate_code"],
        "gate_logs": {
            item["name"]: {"exit_status": item["exit_status"],
                            "sha256": item["log"]["sha256"]}
            for item in gates
        },
    }
    observed_fingerprint = sha256_bytes(json.dumps(
        fingerprint_payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))
    if observed_fingerprint != snapshot_info.get("fingerprint"):
        raise GateError("post-delta receipt fingerprint is not reproducible")
    payload = expected_snapshot / "payload"
    expected_scanned = projection_paths(payload)
    if receipt.get("privacy_scanned_paths") != expected_scanned:
        raise GateError("post-delta privacy scan omitted or added release-bound artifacts")
    replay = {
        "consistency": consistency_gate(payload),
        "privacy": privacy_gate(payload, expected_scanned),
        "checklist": checklist_snapshot_gate(payload),
        "archive": archive_gate(root),
    }
    replay_failed = [f"{name}: {output.strip()[:300]}"
                     for name, (status, output) in replay.items() if status != 0]
    if replay_failed:
        raise GateError("post-delta receipt replay failed: " + " | ".join(replay_failed))
    run = strict_json(safe_file(root, "run.json"))
    if receipt.get("audit_id") != run.get("audit_id"):
        raise GateError("post-delta receipt audit_id does not match run.json")
    if receipt_path != root / f"critique/round_{receipt['round']}/POST_DELTA_GATES.json":
        raise GateError("post-delta receipt is not in its canonical round path")
    if require_current:
        rerun_current(root, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--report", type=pathlib.Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--require-current", action="store_true")
    args = parser.parse_args()
    root = args.audit_dir.resolve()
    try:
        if args.check:
            receipt_path = root / f"critique/round_{args.round}/POST_DELTA_GATES.json"
            verify_receipt(root, receipt_path, require_current=args.require_current)
            print(f"post-delta gates verified for round {args.round}")
        else:
            report = (args.report or root / "report/REPORT_final.md").resolve()
            receipt = make_receipt(root, args.round, report)
            print(f"post-delta gates passed; wrote {receipt}")
    except (GateError, OSError, json.JSONDecodeError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
