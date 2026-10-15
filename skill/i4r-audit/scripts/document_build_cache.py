#!/usr/bin/env python3
"""Content-addressed, validated cache transport for build_docx.sh.

The cache is a single ZIP per input key so refresh is one atomic replace. Restore validates the
entire archive in memory before creating output temporaries, then atomically replaces each public
artifact. The document build manifest is deliberately written by build_docx.sh only after both
artifacts are in place.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import zipfile


SCHEMA = "2.0.0"
DOCUMENT_MANIFEST_SCHEMA = "3.0.0"
MEMBERS = {"cache-manifest.json", "inputs.json", "document.docx", "document.pdf"}
LABEL = re.compile(r"^[a-z][a-z0-9_]*$")


class CacheMiss(Exception):
    pass


class InvalidCache(Exception):
    pass


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fsync_directory(directory: pathlib.Path) -> None:
    descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: pathlib.Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def parse_pairs(values: list[str], kind: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"{kind} must be LABEL=VALUE: {value!r}")
        label, item = value.split("=", 1)
        if not LABEL.fullmatch(label):
            raise ValueError(f"invalid {kind} label: {label!r}")
        if label in result:
            raise ValueError(f"duplicate {kind} label: {label}")
        result[label] = item
    return result


def regular_bytes(path: pathlib.Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular, non-symlink file: {path}")
    return path.read_bytes()


def command_key(args: argparse.Namespace) -> int:
    inputs = parse_pairs(args.input, "input")
    options = parse_pairs(args.option, "option")
    tools = parse_pairs(args.tool, "tool")
    if not inputs:
        raise ValueError("at least one --input is required")
    files: dict[str, dict[str, object]] = {}
    for label, raw_path in sorted(inputs.items()):
        data = regular_bytes(pathlib.Path(raw_path), f"input {label}")
        files[label] = {"sha256": digest(data), "bytes": len(data)}
    descriptor = {
        "schema_version": SCHEMA,
        "inputs": files,
        "options": dict(sorted(options.items())),
        "tools": dict(sorted(tools.items())),
    }
    payload = canonical(descriptor)
    key = digest(payload)
    if args.descriptor:
        atomic_write(pathlib.Path(args.descriptor), payload)
    print(key)
    return 0


def safe_target(path: pathlib.Path, root: pathlib.Path) -> pathlib.Path:
    root = root.resolve(strict=True)
    parent = path.parent.resolve(strict=True)
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"cache restore target escapes audit root: {path}") from exc
    target = parent / path.name
    if target.is_symlink() or target.is_dir():
        raise ValueError(f"cache restore target must not be a symlink or directory: {target}")
    return target


def read_valid_archive(archive_path: pathlib.Path, key: str) -> tuple[bytes, bytes, dict[str, object]]:
    if not archive_path.exists():
        raise CacheMiss()
    if archive_path.is_symlink() or not archive_path.is_file():
        raise InvalidCache("cache entry is not a regular file")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != MEMBERS:
                raise InvalidCache(f"unexpected or duplicate archive members: {names}")
            if archive.testzip() is not None:
                raise InvalidCache("cache archive has a CRC failure")
            blobs = {name: archive.read(name) for name in MEMBERS}
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise InvalidCache(f"unreadable cache archive: {exc}") from exc
    try:
        descriptor = json.loads(blobs["inputs.json"])
        manifest = json.loads(blobs["cache-manifest.json"])
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidCache(f"invalid cache metadata JSON: {exc}") from exc
    descriptor_bytes = canonical(descriptor)
    if descriptor_bytes != blobs["inputs.json"] or digest(descriptor_bytes) != key:
        raise InvalidCache("input descriptor is noncanonical or does not match the requested key")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA:
        raise InvalidCache("unsupported cache manifest schema")
    if manifest.get("cache_key") != key or manifest.get("inputs_sha256") != digest(descriptor_bytes):
        raise InvalidCache("cache manifest is bound to different inputs")
    validation = manifest.get("validation")
    if (not isinstance(validation, dict) or validation.get("status") != "passed" or
            validation.get("schema_version") != DOCUMENT_MANIFEST_SCHEMA or
            validation.get("build_kind") != "canonical" or
            validation.get("all_validations_passed") is not True):
        raise InvalidCache("cache entry was not produced by a passing document build")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {"docx", "pdf"}:
        raise InvalidCache("cache manifest needs exact DOCX and PDF records")
    for label, member in (("docx", "document.docx"), ("pdf", "document.pdf")):
        record = artifacts.get(label)
        data = blobs[member]
        if not isinstance(record, dict) or record.get("member") != member:
            raise InvalidCache(f"invalid {label} cache record")
        if record.get("bytes") != len(data) or record.get("sha256") != digest(data):
            raise InvalidCache(f"{label} bytes do not match the cache manifest")
    return blobs["document.docx"], blobs["document.pdf"], validation


def publish_pair(docx: pathlib.Path, pdf: pathlib.Path, docx_data: bytes, pdf_data: bytes,
                 root: pathlib.Path) -> None:
    docx = safe_target(docx, root)
    pdf = safe_target(pdf, root)
    staged: list[tuple[pathlib.Path, pathlib.Path, str]] = []
    try:
        for target, data in ((docx, docx_data), (pdf, pdf_data)):
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".cache-restore", dir=str(target.parent)
            )
            temporary = pathlib.Path(temporary_name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o644)
            if digest(temporary.read_bytes()) != digest(data):
                raise OSError(f"staged cache restore changed bytes for {target}")
            staged.append((temporary, target, digest(data)))
        # Each visible artifact changes by atomic same-directory replace. The caller publishes the
        # binding document_build_manifest.json only after both replacements complete.
        for temporary, target, _ in staged:
            os.replace(temporary, target)
        for directory in {docx.parent, pdf.parent}:
            fsync_directory(directory)
    finally:
        for temporary, _, _ in staged:
            if temporary.exists():
                temporary.unlink()


def command_restore(args: argparse.Namespace) -> int:
    archive = pathlib.Path(args.archive)
    docx_data, pdf_data, validation = read_valid_archive(archive, args.key)
    publish_pair(pathlib.Path(args.docx), pathlib.Path(args.pdf), docx_data, pdf_data,
                 pathlib.Path(args.root))
    if args.metadata_out:
        atomic_write(pathlib.Path(args.metadata_out), canonical(validation))
    print(f"document cache hit: {args.key}")
    return 0


def zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2026, 8, 27, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def command_store(args: argparse.Namespace) -> int:
    archive = pathlib.Path(args.archive)
    descriptor_bytes = regular_bytes(pathlib.Path(args.descriptor), "input descriptor")
    try:
        descriptor = json.loads(descriptor_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid input descriptor: {exc}") from exc
    descriptor_bytes = canonical(descriptor)
    if digest(descriptor_bytes) != args.key:
        raise ValueError("input descriptor does not match cache key")
    docx_data = regular_bytes(pathlib.Path(args.docx), "built DOCX")
    pdf_data = regular_bytes(pathlib.Path(args.pdf), "built PDF")
    metadata_bytes = regular_bytes(pathlib.Path(args.metadata), "validation metadata")
    try:
        validation = json.loads(metadata_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid validation metadata: {exc}") from exc
    if (not isinstance(validation, dict) or validation.get("status") != "passed" or
            validation.get("schema_version") != DOCUMENT_MANIFEST_SCHEMA or
            validation.get("build_kind") != "canonical" or
            validation.get("all_validations_passed") is not True):
        raise ValueError("only a canonical document-manifest-v3 passing build may enter the cache")
    manifest = {
        "schema_version": SCHEMA,
        "cache_key": args.key,
        "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inputs_sha256": digest(descriptor_bytes),
        "artifacts": {
            "docx": {"member": "document.docx", "sha256": digest(docx_data), "bytes": len(docx_data)},
            "pdf": {"member": "document.pdf", "sha256": digest(pdf_data), "bytes": len(pdf_data)},
        },
        "validation": validation,
    }
    archive.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive.name}.", suffix=".tmp", dir=str(archive.parent)
    )
    os.close(descriptor)
    temporary = pathlib.Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
            for name, data in (
                ("cache-manifest.json", canonical(manifest)),
                ("document.docx", docx_data),
                ("document.pdf", pdf_data),
                ("inputs.json", descriptor_bytes),
            ):
                output.writestr(zip_info(name), data)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        # Re-open and validate the exact staged archive before it becomes a cache hit candidate.
        read_valid_archive(temporary, args.key)
        os.replace(temporary, archive)
        fsync_directory(archive.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"document cache stored: {args.key}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    key = commands.add_parser("key")
    key.add_argument("--input", action="append", default=[])
    key.add_argument("--option", action="append", default=[])
    key.add_argument("--tool", action="append", default=[])
    key.add_argument("--descriptor")
    key.set_defaults(func=command_key)

    restore = commands.add_parser("restore")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--key", required=True)
    restore.add_argument("--docx", required=True)
    restore.add_argument("--pdf", required=True)
    restore.add_argument("--root", required=True)
    restore.add_argument("--metadata-out")
    restore.set_defaults(func=command_restore)

    store = commands.add_parser("store")
    store.add_argument("--archive", required=True)
    store.add_argument("--key", required=True)
    store.add_argument("--descriptor", required=True)
    store.add_argument("--docx", required=True)
    store.add_argument("--pdf", required=True)
    store.add_argument("--metadata", required=True)
    store.set_defaults(func=command_store)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except CacheMiss:
        return 3
    except InvalidCache as exc:
        print(f"INVALID CACHE: {exc}", file=sys.stderr)
        return 4
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"FATAL: document cache: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
