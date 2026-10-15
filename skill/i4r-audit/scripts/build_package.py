#!/usr/bin/env python3
"""Build or verify the deterministic, privacy-scanned i4r-audit installation ZIP."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile
import zipfile

from check_release_privacy import (
    DOCUMENT_SUFFIXES,
    InspectionError,
    TEXT_SUFFIXES,
    inspect_file,
    inspect_text_bytes,
)


ROOT = pathlib.Path(__file__).resolve().parent.parent
PREFIX = ROOT.name + "/"
EXCLUDED_PARTS = {"__pycache__", ".git", ".pytest_cache", "_history_aug27_baks"}
# `.html` used to be here, and the only file in the tree it excluded was CHEATSHEET.html --
# the team onboarding document, which therefore shipped in no release. The rendered
# CHEATSHEET.pdf is still excluded by name below, as a build output.
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".csv"}
# Excluded by exact name. R writes `.Rhistory` and `.RData` into whatever directory it was started
# in; an empty `.Rhistory` appeared in the skill root after the 2.6.3 ZIP was built, and the
# freshness check then failed with missing=['i4r-audit/.Rhistory'] on an unchanged package. The one
# set serves both the inclusion test and the symlink refusal, so the two can no longer disagree.
EXCLUDED_NAMES = {".DS_Store", "CHEATSHEET.pdf", "PACKAGE_MANIFEST.json", ".Rhistory", ".RData"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def included(path: pathlib.Path, root: pathlib.Path = ROOT) -> bool:
    rel = path.relative_to(root)
    if any(part in EXCLUDED_PARTS or part.startswith("_history") for part in rel.parts):
        return False
    if path.name in EXCLUDED_NAMES:
        return False
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    return path.is_file() and not path.is_symlink()


def source_entries(root: pathlib.Path = ROOT) -> dict[str, bytes]:
    root = root.resolve(strict=True)
    prefix = root.name + "/"
    entries: dict[str, bytes] = {}
    leaks: list[str] = []
    for path in sorted(root.rglob("*")):
        rel_path = path.relative_to(root)
        excluded = any(part in EXCLUDED_PARTS or part.startswith("_history") for part in rel_path.parts)
        if path.is_symlink():
            if not excluded and path.name not in EXCLUDED_NAMES:
                raise ValueError(f"symlinked package input is not allowed: {rel_path.as_posix()}")
            continue
        if not included(path, root):
            continue
        rel = prefix + rel_path.as_posix()
        data = path.read_bytes()
        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            issues = inspect_text_bytes(data, rel, identifiers=False)
            leaks.extend(f"{issue.location}: {issue.detail}" for issue in issues)
        elif suffix in DOCUMENT_SUFFIXES:
            try:
                issues = inspect_file(path, rel, identifiers=False)
            except (InspectionError, OSError) as exc:
                raise ValueError(f"privacy inspection incomplete for {rel}: {exc}") from exc
            if path.is_symlink() or path.read_bytes() != data:
                raise ValueError(f"package input changed during privacy inspection: {rel}")
            leaks.extend(f"{issue.location}: {issue.detail}" for issue in issues)
        entries[rel] = data
    if leaks:
        raise ValueError("private absolute paths in package inputs:\n  " + "\n  ".join(leaks[:30]))
    return entries


def package_manifest(entries: dict[str, bytes], root: pathlib.Path = ROOT) -> bytes:
    prefix = root.name + "/"
    policy_key = prefix + "config/pipeline-policy-v2.json"
    version_key = prefix + "VERSION"
    try:
        policy_data = entries[policy_key]
        version_data = entries[version_key]
    except KeyError as exc:
        raise ValueError(f"required package input is missing: {exc.args[0]}") from exc
    payload = {
        "package_version": version_data.decode("utf-8").strip(),
        "policy_sha256": digest(policy_data),
        "entries": [{"path": path, "size": len(data), "sha256": digest(data)}
                    for path, data in sorted(entries.items())
                    if path != prefix + "PACKAGE_MANIFEST.json"],
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def zip_info(name: str, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2026, 8, 27, 0, 0, 0))
    info.create_system = 3
    mode = stat.S_IFREG | (0o755 if executable else 0o644)
    info.external_attr = mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _validated_output(output: pathlib.Path, root: pathlib.Path = ROOT) -> pathlib.Path:
    expanded = output.expanduser()
    if expanded.is_symlink():
        raise ValueError(f"package output may not be a symlink: {expanded}")
    root = root.resolve(strict=True)
    try:
        resolved = expanded.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"package output cannot be resolved safely: {exc}") from exc
    nearest = resolved
    while not nearest.exists() and nearest != nearest.parent:
        nearest = nearest.parent
    physical_inside_root = False
    for parent in (nearest, *nearest.parents):
        try:
            if parent.samefile(root):
                physical_inside_root = True
                break
        except OSError:
            continue
    if resolved == root or root in resolved.parents or physical_inside_root:
        raise ValueError(f"package output must be outside source root {root}: {resolved}")
    return resolved


def _fsync_directory(directory: pathlib.Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(directory), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build(output: pathlib.Path) -> None:
    output = _validated_output(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    entries = source_entries()
    entries[PREFIX + "PACKAGE_MANIFEST.json"] = package_manifest(entries)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=str(output.parent)
    )
    os.close(descriptor)
    temporary = pathlib.Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(entries.items()):
                executable = name.startswith(PREFIX + "scripts/") and data.startswith(b"#!")
                archive.writestr(zip_info(name, executable), data)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
        _fsync_directory(output.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"built {output} ({len(entries)} files, sha256 {digest(output.read_bytes())})")


def check(output: pathlib.Path) -> None:
    output = _validated_output(output)
    expected = source_entries()
    expected[PREFIX + "PACKAGE_MANIFEST.json"] = package_manifest(expected)
    if not output.is_file():
        raise ValueError(f"package does not exist: {output}")
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        if names != sorted(expected):
            missing = sorted(set(expected) - set(names))
            extra = sorted(set(names) - set(expected))
            raise ValueError(f"package inventory is stale (missing={missing}, extra={extra})")
        bad = [name for name in names if archive.read(name) != expected[name]]
        if bad:
            raise ValueError("package content hashes are stale: " + ", ".join(bad))
        corrupt = archive.testzip()
        if corrupt:
            raise ValueError(f"ZIP CRC failure: {corrupt}")
    print(f"package freshness check: ok ({len(expected)} exact entries)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, default=ROOT.parent / "i4r-audit.zip")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        check(args.output) if args.check else build(args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
