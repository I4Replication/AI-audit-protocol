#!/usr/bin/env python3
"""Recompute a generated critique diff packet and require byte-for-byte identity."""
from __future__ import annotations

import argparse
import json
import shlex
import os
import pathlib
import subprocess
import sys
import tempfile
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import validate_json_schema


EXPECTED_PATH_KEYS = {
    "before_report", "after_report", "before_findings", "after_findings"
}


def contained(root: pathlib.Path, relative: Any) -> pathlib.Path:
    if (not isinstance(relative, str) or not relative or
            pathlib.PurePosixPath(relative).is_absolute() or "\\" in relative):
        raise ValueError(f"unsafe diff input path: {relative!r}")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"diff input escapes audit root: {relative}") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"diff input is missing or symlinked: {relative}")
    return path


def verify(root: pathlib.Path, manifest: pathlib.Path, round_number: int | None = None) -> dict[str, Any]:
    root = root.resolve()
    manifest = manifest.resolve()
    value = validate_json_schema.strict_loads(
        manifest.read_text(encoding="utf-8"), source=str(manifest)
    )
    if not isinstance(value, dict) or value.get("packet_version") != "i4r-diff-packet-2":
        raise ValueError("unsupported or malformed generated diff packet")
    observed_round = value.get("round")
    if not isinstance(observed_round, int) or observed_round < 2:
        raise ValueError("diff packet round must be an integer >= 2")
    if round_number is not None and observed_round != round_number:
        raise ValueError("diff packet round does not match requested runtime round")
    paths = value.get("input_paths")
    if not isinstance(paths, dict) or set(paths) != EXPECTED_PATH_KEYS:
        raise ValueError("diff packet lacks its exact generated input-path bindings")
    resolved = {key: contained(root, paths[key]) for key in sorted(EXPECTED_PATH_KEYS)}
    if resolved["after_report"] != root / "report/REPORT_final.md":
        raise ValueError("diff packet after-report path is not canonical")
    if resolved["after_findings"] != root / "work/findings.jsonl":
        raise ValueError("diff packet after-findings path is not canonical")

    fd, temporary_name = tempfile.mkstemp(prefix="i4r-diff-recompute.", suffix=".json")
    os.close(fd)
    temporary = pathlib.Path(temporary_name)
    try:
        command = [
            sys.executable, str(HERE / "make_diff_packet.py"), str(root),
            "--round", str(observed_round),
            "--before-report", str(resolved["before_report"]),
            "--after-report", str(resolved["after_report"]),
            "--before-findings", str(resolved["before_findings"]),
            "--after-findings", str(resolved["after_findings"]),
            "--output", str(temporary),
            "--no-remedy-file",
        ]
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise ValueError(f"could not recompute diff packet: {detail[-4000:]}")
        if temporary.read_bytes() != manifest.read_bytes():
            # name what moved: the report, the registry, the settled cache, the before-files ...
            try:
                fresh, bound = json.loads(temporary.read_text(encoding="utf-8")), value
                changed = sorted(key for key in set(fresh.get("inputs") or {}) | set(bound.get("inputs") or {})
                                 if (fresh.get("inputs") or {}).get(key) != (bound.get("inputs") or {}).get(key))
                changed += [key for key in ("settled_cache_sha256",) if fresh.get(key) != bound.get(key)]
            except (OSError, ValueError, AttributeError):
                changed = []
            rebuild = (f"python3 {shlex.quote(str(HERE / 'make_diff_packet.py'))} {shlex.quote(str(root))} --round "
                       f"{observed_round} --before-report {shlex.quote(str(resolved['before_report']))} --after-report "
                       f"{shlex.quote(str(resolved['after_report']))} --before-findings "
                       f"{shlex.quote(str(resolved['before_findings']))} --after-findings "
                       f"{shlex.quote(str(resolved['after_findings']))} --output {shlex.quote(str(manifest))}")
            raise ValueError(
                "diff packet is not the exact deterministic projection of its bound inputs"
                + (f"; changed since it was built: {', '.join(changed)}" if changed else "")
                + f". Rebuild it: {rebuild} (then re-run its post-delta gates)")
    finally:
        temporary.unlink(missing_ok=True)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("manifest", type=pathlib.Path)
    parser.add_argument("--round", type=int)
    args = parser.parse_args()
    try:
        value = verify(args.audit_dir, args.manifest, args.round)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    print(f"generated diff packet verified for round {value['round']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
