#!/usr/bin/env python3
"""Runtime primitives for run_critique.sh.

The production driver stays Bash 3.2 compatible; JSON, hashing, schemas, and atomic metadata live
here so they have one deterministic implementation that the fake-Codex test suite can exercise.
Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import collections
import copy
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
import time
from typing import Any


ROLES = ("stats", "data", "fairness", "completeness")
VERDICTS = ("CONFIRMED", "REFUTED", "OVERSTATED", "UNVERIFIABLE")
PRIORITIES = ("BLOCKING", "NON_BLOCKING")
CONFIDENCE = ("CONFIRMED", "LIKELY", "SPECULATIVE")
SKILL_ROOT = pathlib.Path(__file__).resolve().parent.parent
CRITIC_SCHEMA = SKILL_ROOT / "schemas/v2/critic-result.schema.json"
SCHEMA_VALIDATOR = SKILL_ROOT / "scripts/validate_json_schema.py"
SEMANTIC_VALIDATOR = SKILL_ROOT / "schemas/tools/validate_v2.py"
PROVIDER_SCHEMA_GENERATION_VERSION = "i4r-openai-structured-output-4"
RETRYABLE_PROVIDER_ERROR_CLASSES = ("transport", "throttling", "provider_5xx")
PROVIDER_SCHEMA_KEYWORDS = frozenset({
    "$schema", "$defs", "$ref", "title", "description", "type", "properties", "required",
    "additionalProperties", "items", "enum", "const", "anyOf", "minimum", "maximum",
    "minItems", "maxItems", "format", "pattern",
})
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
sys.path.insert(0, str(SKILL_ROOT / "schemas/tools"))
import validate_json_schema  # type: ignore  # local, dependency-free runtime module
import validate_v2  # type: ignore  # local, dependency-free runtime module


ROLE_CHECK_IDS = {
    "stats": [
        "stats.claim_assessments",
        "stats.quantitative_methods",
        "stats.uncertainty_and_multiplicity",
    ],
    "data": [
        "data.claim_assessments",
        "data.provenance_and_integrity",
        "data.sample_flow_and_derivations",
    ],
    "fairness": [
        "fairness.claim_assessments",
        "fairness.disclosure_and_framing",
        "fairness.remedy_proportionality",
    ],
    "completeness": [
        "completeness.claim_joins",
        "completeness.whole_report",
        "completeness.archive_privacy_and_checklist",
    ],
}

DOMAIN_SPECIALIST = {
    "statistics": "stats",
    "code_methods": "stats",
    "data": "data",
    "text": "data",
    "privacy_archive": "fairness",
    "framing": "fairness",
    "other": "fairness",
}


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


def atomic_text(path: os.PathLike[str] | str, value: str) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(f"{path}.tmp.{os.getpid()}")
    tmp.write_text(value)
    os.replace(tmp, path)


def json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def json_const_schema(value: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a provider-compatible schema narrowed to one scalar or string array.

    General JSON Schema permits ``const`` without ``type``. The provider does not, so retain the
    canonical field's other constraints while narrowing scalar values to their exact JSON type.
    The provider also rejects array-valued ``const``. Represent the exact expected string members
    with a fixed length and item enum; downstream semantic validation retains the order-sensitive
    equality check that the provider subset cannot express.
    """
    schema = dict(base or {})
    if isinstance(value, list):
        if any(not isinstance(item, str) or not item for item in value):
            raise TypeError("provider exact-array constraints require nonempty strings")
        if len(set(value)) != len(value):
            raise ValueError("provider exact-array constraints require unique values")
        item_schema = dict(schema.get("items") or {})
        item_schema["type"] = "string"
        if value:
            item_schema["enum"] = value
        else:
            item_schema.pop("enum", None)
        schema.pop("const", None)
        schema["type"] = "array"
        schema["minItems"] = len(value)
        schema["maxItems"] = len(value)
        schema["items"] = item_schema
        return schema
    json_type = json_type_name(value)
    schema["type"] = json_type
    schema["const"] = value
    return schema


def validate_provider_schema(schema: Any) -> None:
    """Fail closed on schema constructs outside the strict provider-supported subset."""
    errors: list[str] = []

    def visit(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            errors.append(f"{path}: schema node must be an object")
            return
        unsupported = sorted(set(node) - PROVIDER_SCHEMA_KEYWORDS)
        if unsupported:
            errors.append(f"{path}: unsupported provider keywords {unsupported}")
        declared = node.get("type")
        declared_types = declared if isinstance(declared, list) else [declared]
        if "const" in node:
            actual = json_type_name(node["const"])
            if actual in {"array", "object"}:
                errors.append(f"{path}: provider const cannot contain a composite JSON value")
            if actual not in declared_types and not (
                    actual == "integer" and "number" in declared_types):
                errors.append(
                    f"{path}: const type {actual!r} is not explicitly declared by {declared!r}")
        if "enum" in node:
            if declared is None:
                errors.append(f"{path}: enum lacks an explicit type")
            elif not isinstance(node["enum"], list) or not node["enum"]:
                errors.append(f"{path}: enum must be a nonempty array")
        if "pattern" in node and (
                not isinstance(node["pattern"], str) or any(
                    marker in node["pattern"] for marker in ("(?=", "(?!", "(?<=", "(?<!"))):
            errors.append(f"{path}: pattern uses unsupported regex lookaround")
        if declared == "object" or (
                isinstance(declared, list) and "object" in declared_types):
            properties = node.get("properties")
            required = node.get("required")
            if not isinstance(properties, dict):
                errors.append(f"{path}: object schema lacks properties")
            if node.get("additionalProperties") is not False:
                errors.append(f"{path}: object schema must set additionalProperties=false")
            if isinstance(properties, dict) and (
                    not isinstance(required, list) or set(required) != set(properties)):
                errors.append(f"{path}: every object property must be required")
        properties = node.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                visit(child, f"{path}/properties/{name}")
        definitions = node.get("$defs")
        if isinstance(definitions, dict):
            for name, child in definitions.items():
                visit(child, f"{path}/$defs/{name}")
        if "items" in node:
            visit(node["items"], f"{path}/items")
        alternatives = node.get("anyOf")
        if isinstance(alternatives, list):
            for index, child in enumerate(alternatives):
                visit(child, f"{path}/anyOf/{index}")

    visit(schema, "#")
    if errors:
        raise ValueError("provider JSON Schema is incompatible: " + "; ".join(errors[:20]))


def atomic_provider_schema(path: os.PathLike[str] | str, schema: dict[str, Any]) -> None:
    validate_provider_schema(schema)
    atomic_json(path, schema)


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


def provider_failure_classification(text: str) -> tuple[str, str]:
    """Return one policy-typed failure/retry decision from provider diagnostics.

    Bare timeout and vague retry wording are deliberately permanent. A retry requires an explicit
    transport condition, throttling/429, or provider HTTP 5xx classification.
    """
    if not isinstance(text, str):
        raise ValueError("provider failure diagnostics must be text")
    rules = (
        ("deterministic_schema", r"invalid[_ ]json[_ ]schema|output schema|response[_ ]format[^\n]*schema|schema[^\n]*(?:invalid|mismatch)"),
        ("quota", r"out of credits|insufficient[_ ]quota|quota exceeded"),
        ("authentication", r"unauthorized|authentication|invalid api key"),
        ("authorization", r"forbidden|authorization|permission denied"),
        ("unsupported_model", r"model[^\n]*(?:not supported|unsupported)"),
        ("capability", r"invalid[^\n]*effort|context[^\n]*(?:too long|length)|unknown option"),
        ("throttling", r"(?:^|[^0-9])429(?:[^0-9]|$)|rate.?limit|throttl"),
        (
            "provider_5xx",
            r"\b(?:http(?:/[0-9.]+)?|status(?:\s+code)?|code)\b[^0-9\n]{0,12}5[0-9][0-9]\b|"
            r"\b5[0-9][0-9]\s+(?:internal server error|bad gateway|service unavailable|gateway timeout)\b",
        ),
        (
            "transport",
            r"\btransport(?:[_ -]+(?:error|failure|unavailable))?\b|stream disconnected|"
            r"connection[^\n]*(?:refused|reset|closed|aborted|broken|timed? ?out)|"
            r"econn(?:reset|refused|aborted)|broken pipe|network is unreachable|"
            r"failed to lookup|name or service not known|temporary failure in name resolution",
        ),
        ("timeout", r"timed? ?out|timeout"),
    )
    for error_class, pattern in rules:
        if re.search(pattern, text, re.I):
            failure_class = (
                "transient" if error_class in RETRYABLE_PROVIDER_ERROR_CLASSES else "permanent"
            )
            return failure_class, error_class
    return "permanent", "permanent"


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


class SnapshotIntegrityError(ValueError):
    """A frozen snapshot's content, inventory, identity or manifest no longer matches.

    An environment fault, not a property of any provider response: no model output can cause it,
    and re-sampling cannot cure it. main() exits 5 on it so the driver can tell it apart from a
    schema-invalid response (exit 1). It stays a ValueError so every existing `except ValueError`
    caller keeps its behaviour.
    """


# Opt-in, exported only by run_critique.sh. Unset, every check below is strict and changes nothing
# on disk, which the library, one-off recovery scripts and compatibility sweeps over published
# audits rely on.
class SnapshotModeUnstableError(SnapshotIntegrityError):
    """The snapshot's content verified, but its modes would not stay frozen (2.6.8).

    Raised by ensure_frozen after a bounded number of re-freezes when something on the host keeps
    giving a frozen entry its write bit back. It is a SnapshotIntegrityError, so every caller and
    the exit code (5) treat it as the host fault it is -- no re-sample, no repair, no permanent
    marker -- but it is not a content change: its message starts with SNAPSHOT_MODE_UNSTABLE, so
    the driver can word it as such (failure_class snapshot_mode_unstable) and not tell the operator
    to delete a snapshot, which cannot help while the modes keep changing.
    """


SNAPSHOT_REFREEZE_ENV = "I4R_SNAPSHOT_REFREEZE"
SNAPSHOT_REFREEZE_LOCK = ".snapshot_refreeze.lock"
SNAPSHOT_INTEGRITY_EXIT = 5
SNAPSHOT_MODE_UNSTABLE = "snapshot_mode_unstable"
# A re-freeze is repeated this many times, with a short growing pause, before a mode that keeps
# changing is reported as SnapshotModeUnstableError: one File Provider reset is repaired quietly.
SNAPSHOT_REFREEZE_ATTEMPTS = 3
SNAPSHOT_REFREEZE_RETRY_SECONDS = 0.1
# A frozen snapshot has 0555 directories and 0444 files, so a plain `rm -rf` fails on every entry:
# the remedy is always the full command. {dir} is audit-relative for the standard layout.
SNAPSHOT_DELETE_REMEDY = (
    "  Remedy: in the audit directory run `chmod -R u+w {dir} && rm -rf {dir}`; the snapshot is "
    "content-addressed and will be rebuilt identically."
)


def snapshot_relative(root: pathlib.Path) -> str:
    """critique/round_N/{snapshots,target_snapshots}/<sha>, audit-relative, for the standard
    layout; the path as given otherwise (a test or one-off location)."""
    parent = root.parent
    if parent.name in ("snapshots", "target_snapshots") \
            and re.fullmatch(r"round_[1-9][0-9]*", parent.parent.name) \
            and parent.parent.parent.name == "critique":
        return f"critique/{parent.parent.name}/{parent.name}/{root.name}"
    return str(root)


def snapshot_where(root: pathlib.Path) -> str:
    """'round snapshot critique/round_1/snapshots/<sha>' or 'target snapshot ...': which frozen
    tree a message is about, since a round and a target snapshot are both in play at once."""
    kind = {"snapshots": "round", "target_snapshots": "target"}.get(root.parent.name, "immutable")
    return f"{kind} snapshot {snapshot_relative(root)}"


def snapshot_delete_remedy(root: pathlib.Path) -> str:
    return SNAPSHOT_DELETE_REMEDY.format(dir=snapshot_relative(root))


def verify_snapshot_integrity(root: pathlib.Path, manifest: dict[str, Any]) -> None:
    """Symlinks, inventory closure and per-file hashes: everything except modes."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            raise SnapshotIntegrityError(
                f"symlink inside immutable snapshot: {path.relative_to(root).as_posix()} in "
                f"{snapshot_where(root)}")
    hashes = manifest.get("files_sha256")
    actual_files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path != root / "MANIFEST.json"
    )
    actual_names = {path.relative_to(root).as_posix() for path in actual_files}
    if not isinstance(hashes, dict) or set(hashes) != actual_names:
        raise SnapshotIntegrityError(f"immutable file inventory differs: {snapshot_where(root)}")
    for path in actual_files:
        rel = path.relative_to(root).as_posix()
        if hashes[rel] != sha_file(path):
            raise SnapshotIntegrityError(
                f"immutable file hash differs: {rel} in {snapshot_where(root)}")


def writable_snapshot_entries(root: pathlib.Path) -> list[pathlib.Path]:
    """Every entry of a frozen tree, root first, that carries any write bit."""
    return [path for path in [root, *root.rglob("*")] if path.stat().st_mode & 0o222]


def snapshot_mode_error(path: pathlib.Path) -> ValueError:
    """The strict (refreeze disabled) mode failure, worded exactly as before 2.6.8."""
    return ValueError(
        f"immutable path regained write permission: {path}\n"
        "  A snapshot is frozen before it is published, so this means something "
        "changed its mode afterwards -- a synced folder re-applying permissions is "
        "the usual cause.\n"
        "  Remedy: chmod -R a-w the path above, or delete it (it is "
        "content-addressed and will be rebuilt identically)."
    )


def verify_read_only_inventory(root: pathlib.Path, manifest: dict[str, Any]) -> None:
    """The strict check: integrity first, then any write bit is an error. Changes nothing."""
    verify_snapshot_integrity(root, manifest)
    writable = writable_snapshot_entries(root)
    if writable:
        raise snapshot_mode_error(writable[0])


def snapshot_refreeze_enabled(refreeze: bool | None) -> bool:
    if refreeze is not None:
        return bool(refreeze)
    return os.environ.get(SNAPSHOT_REFREEZE_ENV, "") == "1"


def _snapshot_layout(root: pathlib.Path) -> tuple[str, int | None, pathlib.Path]:
    """(kind, round, lock directory) for <audit>/critique/round_N/{snapshots,target_snapshots}/<sha>."""
    kind = {"snapshots": "round", "target_snapshots": "target"}.get(root.parent.name, "other")
    lock_dir = root.parent.parent if kind != "other" else root.parent
    match = re.fullmatch(r"round_([1-9][0-9]*)", lock_dir.name)
    return kind, int(match.group(1)) if match else None, lock_dir


def _depth_limited(root: pathlib.Path, paths: list[pathlib.Path]) -> list[str]:
    """Directories at most two levels deep, never a file name and never an absolute path.

    Everything deeper than source/evidence can carry deposit directory and file names, and the
    note lands in run.events.jsonl, which is a release projection and coordinator context.
    """
    names = set()
    for path in paths:
        parts = path.relative_to(root).parts
        if not path.is_dir():
            parts = parts[:-1]
        names.add("/".join(parts[:2]) or ".")
    return sorted(names)


def _journal_refreeze(root: pathlib.Path, audit_root: pathlib.Path | None, context: str,
                      refrozen: list[pathlib.Path], invocation: str | None) -> None:
    kind, round_number, _lock_dir = _snapshot_layout(root)
    try:
        snapshot_rel = root.resolve().relative_to(pathlib.Path(audit_root).resolve()).as_posix() \
            if audit_root is not None else root.name
    except ValueError:
        snapshot_rel = root.name
    paths = _depth_limited(root, refrozen)
    print(
        f"  [note ] snapshot mode drift re-frozen: {snapshot_rel} ({len(refrozen)} path(s) under "
        f"{', '.join(paths)}; context {context}); content verified unchanged, nothing re-run",
        file=sys.stderr,
    )
    journal_root = _metadata_21_root(str(audit_root)) if audit_root is not None else None
    if journal_root is None:
        return
    payload = {
        "text": (
            f"Frozen {kind} snapshot regained write permission on {len(refrozen)} path(s) with its "
            "content unchanged (a synced folder re-applying a mode is the usual cause); it was "
            "re-frozen in place. Hashes, cache keys and the round plan are unaffected."
        ),
        "kind": "snapshot_mode_refrozen",
        "round": round_number,
        "snapshot": snapshot_rel,
        "snapshot_kind": kind,
        "context": context,
        "invocation": invocation,
        "path_count": len(refrozen),
        "paths": paths,
    }
    try:
        import lifecycle_journal
        lifecycle_journal.append_event(journal_root, "note", payload, source="run_critique.sh")
    except Exception as exc:  # the note is a record of a harmless chmod; never fail a call over it
        print(f"  [warn ] could not journal the snapshot re-freeze note: {exc}", file=sys.stderr)


def ensure_frozen(root: pathlib.Path, manifest: dict[str, Any], *, audit_root: pathlib.Path | None,
                  context: str, refreeze: bool | None = None,
                  invocation: str | None = None) -> list[str]:
    """Verify a frozen snapshot; with refreezing enabled, repair mode-only drift in place.

    Integrity (symlinks, inventory, hashes) is checked first and raises SnapshotIntegrityError
    without touching any mode, so the operator can inspect what changed. Write bits alone are the
    File Provider re-applying a directory mode: with refreeze off (the default unless
    I4R_SNAPSHOT_REFREEZE=1) that is today's strict ValueError; with it on, the tree is re-frozen
    under a lock, re-checked, and the one process that ran chmod journals a single note. Returns
    the snapshot-relative paths that were re-frozen (empty when nothing was).
    """
    verify_snapshot_integrity(root, manifest)
    writable = writable_snapshot_entries(root)
    if not writable:
        return []
    if not snapshot_refreeze_enabled(refreeze):
        raise snapshot_mode_error(writable[0])
    _kind, _round, lock_dir = _snapshot_layout(root)
    # A regular file under flock, not a mkdir lock: a synced folder can resurrect a deleted
    # directory, but the OS releases an flock when its holder dies.
    with (lock_dir / SNAPSHOT_REFREEZE_LOCK).open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            writable = writable_snapshot_entries(root)
            if not writable:
                return []  # another process re-froze it while this one waited
            # The inventory and hashes once more under the lock, right before the first chmod: a
            # change that landed while this process waited must be refused, never sealed.
            verify_snapshot_integrity(root, manifest)
            make_read_only(root)
            remaining = writable_snapshot_entries(root)
            passes = 1
            while remaining and passes < SNAPSHOT_REFREEZE_ATTEMPTS:
                # One more reset by the synced folder is repaired quietly; a mode that keeps
                # changing is reported below as exactly that, not as a content change.
                time.sleep(SNAPSHOT_REFREEZE_RETRY_SECONDS * passes)
                verify_snapshot_integrity(root, manifest)
                make_read_only(root)
                remaining = writable_snapshot_entries(root)
                passes += 1
            if remaining:
                verify_snapshot_integrity(root, manifest)  # a content change outranks the modes
                raise SnapshotModeUnstableError(
                    f"{SNAPSHOT_MODE_UNSTABLE}: {snapshot_where(root)}: content verified against "
                    f"its manifest, but its modes keep changing: {len(remaining)} path(s) regained "
                    f"a write bit after {passes} re-freezes.\n"
                    "  Remedy: stop the sync client, or move the audit off the synced folder, then "
                    "rerun; deleting the snapshot does not help while something keeps changing "
                    "its modes."
                )
            # The content once more, now that nothing can be written: the integrity pass above ran
            # outside the lock and before the chmod, and a re-freeze must never seal a change.
            verify_snapshot_integrity(root, manifest)
            _journal_refreeze(root, audit_root, context, writable, invocation)
            return [path.relative_to(root).as_posix() or "." for path in writable]
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_snapshot_manifest(snapshot: pathlib.Path) -> dict[str, Any]:
    """MANIFEST.json of a frozen snapshot; a missing or unreadable one is an integrity fault."""
    path = snapshot / "MANIFEST.json"
    if path.is_symlink() or not path.is_file():
        raise SnapshotIntegrityError(
            f"corrupt snapshot: {snapshot_where(snapshot)}\n"
            "  The directory exists but has no MANIFEST.json. An empty directory "
            f"resurrected by a synced folder looks exactly like this.\n"
            f"{snapshot_delete_remedy(snapshot)}"
        )
    try:
        manifest = load(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(
            f"corrupt snapshot manifest: {snapshot_where(snapshot)}\n  MANIFEST.json is "
            f"unreadable ({exc}).\n{snapshot_delete_remedy(snapshot)}"
        ) from exc
    if not isinstance(manifest, dict):
        raise SnapshotIntegrityError(
            f"corrupt snapshot manifest: {snapshot_where(snapshot)}\n  MANIFEST.json is not an "
            f"object.\n{snapshot_delete_remedy(snapshot)}"
        )
    return manifest


# Round-snapshot MANIFEST.json fields that no launch can change for the directory found at a
# content address: the address itself, the snapshot format, and the schema generation that the
# address binds. A difference in one of them at reuse is corruption, never "a different launch".
ROUND_SNAPSHOT_CORRUPTION_KEYS = frozenset({
    "snapshot_version", "provider_schema_generation_version", "input_sha256",
})


def round_identity_parts(manifest: dict[str, Any]) -> list[str]:
    """The extra hash_tree inputs of a round snapshot's content address, from its identity."""
    return [
        "runtime-contract-6", manifest["provider_schema_generation_version"],
        str(manifest["round"]), manifest["model"], manifest["effort"],
        " ".join(manifest["roles"]), manifest["audit_id"],
        manifest["canonical_critic_schema_sha256"],
    ]


def target_identity_parts(manifest: dict[str, Any], runtime_script_sha: str) -> list[str]:
    """The extra hash_tree inputs of a target snapshot's content address.

    The driver's hash is bound here but not recorded in the target manifest, so a caller that
    recomputes the address must supply it.
    """
    return [
        "i4r-target-snapshot-4", manifest["provider_schema_generation_version"],
        manifest["model"], manifest["effort"], manifest["runtime_helper_sha256"],
        runtime_script_sha,
    ]


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


def _result_invocation(result_path: pathlib.Path) -> str | None:
    """The driver invocation a result file belongs to (critique/round_N/invocations/<id>/...)."""
    parts = result_path.resolve().parts
    for index in range(len(parts) - 4, -1, -1):
        if parts[index] == "critique" and parts[index + 2] == "invocations":
            return parts[index + 3]
    return None  # a result cache or projection copy


def verify_result_snapshot(result_path: pathlib.Path, round_number: int, input_sha: str,
                           role: str) -> tuple[pathlib.Path, dict[str, Any]]:
    """Prove the frozen snapshot a result is bound to still intact (re-freezing mode drift).

    Content, inventory and manifest faults raise SnapshotIntegrityError, which main() turns into
    exit 5; mode-only drift is re-frozen when I4R_SNAPSHOT_REFREEZE=1 and is otherwise today's
    strict mode error.
    """
    snapshot = snapshot_for_result(result_path, round_number, input_sha)
    manifest = load_snapshot_manifest(snapshot)
    ensure_frozen(snapshot, manifest, audit_root=snapshot.parents[3],
                  context=f"post_call:{role}", invocation=_result_invocation(result_path))
    return snapshot, manifest


def verify_evidence_bindings(obj: dict[str, Any], result_path: pathlib.Path,
                             round_number: int, input_sha: str, *,
                             snapshot_verified: bool = False) -> None:
    """Rehash the frozen snapshot and every cited artifact after the provider call."""
    if snapshot_verified:
        snapshot = snapshot_for_result(result_path, round_number, input_sha)
        manifest = load_snapshot_manifest(snapshot)
    else:
        snapshot, manifest = verify_result_snapshot(result_path, round_number, input_sha,
                                                    obj["role"])
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
    workspace = pathlib.Path(args.workspace).resolve()
    registry_by_id = load_registry_file(workspace / "work/findings.jsonl")
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
            recommended = (manifest or {}).get("recommended_roles") or []
            recommended = [recommended] if isinstance(recommended, str) else list(recommended)
            if recommended and "completeness" not in recommended:
                recommended.append("completeness")
            raise ValueError(
                "multiple later-round specialists require multi-domain critical changes; this packet "
                f"(routing_mode {(manifest or {}).get('routing_mode')}) recommends "
                + (" ".join(recommended) if recommended else "one specialist plus completeness")
                + ". Run with CRITICS=\"" + (" ".join(recommended) if recommended else "<specialist> completeness") + "\"")
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
        claim_assignment_mode = "domain_routed_round_1"
        claim_ids = [row["id"] for row in registry_by_id.values()]
    else:
        raw_claim_ids = manifest.get("changed_claim_ids") if manifest is not None else []
        if not isinstance(raw_claim_ids, list) or any(
                not isinstance(value, str) or not value.strip() for value in raw_claim_ids):
            raise ValueError("later-round changed_claim_ids must contain only nonempty strings")
        claim_ids = list(dict.fromkeys(value.strip() for value in raw_claim_ids))
        missing = [claim_id for claim_id in claim_ids if claim_id.casefold() not in registry_by_id]
        if missing:
            raise ValueError(
                "later-round changed claims are absent from the canonical registry: "
                + ", ".join(missing)
            )
        claim_assignment_mode = "domain_routed_delta"
    # 2.7.4: a packet built under the specialist claim-change rule (scripts/claim_change_rules.py; only a new
    # audit's packet carries the key) also lists the substantive changes. The specialists get only those, the
    # completeness critic still gets every changed finding, and a specialist left with none is not dispatched.
    specialist_scope: set[str] | None = None
    if args.round > 1 and manifest is not None and "claim_change_rule" in manifest:
        if manifest.get("claim_change_rule") != "substantive_specialists_1":
            raise ValueError(f"unknown claim_change_rule in the diff manifest: {manifest.get('claim_change_rule')!r}")
        raw_scope = manifest.get("specialist_claim_ids")
        if not isinstance(raw_scope, list) or any(not isinstance(value, str) or not value.strip() for value in raw_scope):
            raise ValueError("later-round specialist_claim_ids must be a list of nonempty strings")
        specialist_scope = {value.strip().casefold() for value in raw_scope}
        outside = sorted(value for value in specialist_scope if value not in {c.casefold() for c in claim_ids})
        if outside:
            raise ValueError("specialist_claim_ids are not changed claims: " + ", ".join(outside))
        # the mode stays domain_routed_delta: every consumer pins it; the scope is in the manifest itself

    # Every route lists its roles in the canonical ROLES order. The deterministic claim merge orders its roles that
    # way, and the round projection compares the two lists. A CRITICS override written as "completeness fairness"
    # used to pass routing and both critics, then fail the projection with "claim merge identity or role quorum
    # differs" (MacIntyre 2026 audit, round 2).
    roles = [role for role in ROLES if role in roles]
    claim_assignments = {role: [] for role in roles}
    specialists = [role for role in roles if role != "completeness"]
    for claim_id in claim_ids:
        row = registry_by_id.get(claim_id.casefold())
        if row is None:
            # A missing registry is tolerated only for a claim-free fixture. Production diff
            # routing above is fail-closed, and Round 1 simply has no claim rows to assign.
            continue
        if "completeness" in claim_assignments:
            claim_assignments["completeness"].append(row["id"])
        if specialist_scope is not None and claim_id.casefold() not in specialist_scope:
            continue
        natural = DOMAIN_SPECIALIST[row["domain"]]
        chosen = natural if natural in claim_assignments else (specialists[0] if specialists else None)
        if chosen is not None:
            claim_assignments[chosen].append(row["id"])
        # Disclosure-critical claims always receive a fairness pass when that specialist is routed.
        if row["disclosure_flag"] and "fairness" in claim_assignments:
            claim_assignments["fairness"].append(row["id"])
    for role in roles:
        claim_assignments[role] = list(dict.fromkeys(claim_assignments[role]))
    if specialist_scope is not None and not args.override:  # an explicit CRITICS override is never second-guessed
        dropped = [role for role in roles if role != "completeness" and not claim_assignments[role]]
        kept = [role for role in roles if role != "completeness" and role not in dropped]
        if dropped and kept:
            roles = [role for role in roles if role not in dropped]
            claim_assignments = {role: claim_assignments[role] for role in roles}
            reason += f"; {', '.join(dropped)} not dispatched (no substantive change in scope)"
        elif dropped:
            # No substantive change for any specialist (only bookkeeping, or prose outside every finding's section).
            # Every round after round 1 keeps at least one specialist (finalize requires one), so this round keeps
            # the first routed specialist with the legacy, unnarrowed assignment.
            first = next(role for role in roles if role != "completeness")
            roles = [first, "completeness"]
            assigned: list[str] = []
            for claim_id in claim_ids:
                row = registry_by_id.get(claim_id.casefold())
                if row is not None:
                    assigned.append(row["id"])
            claim_assignments = {first: list(dict.fromkeys(assigned)),
                                 "completeness": claim_assignments["completeness"]}
            reason += (f"; no substantive change for any specialist, so {first} keeps the unnarrowed scope "
                       "(a later round always keeps one specialist)")
    atomic_json(
        args.output,
        {
            "routing_version": "i4r-round-routing-2",
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


def constrain_evidence_bindings(schema: dict[str, Any], inventory: dict[str, Any]) -> None:
    """Restrict artifact_binding.path/sha256 to the frozen inventory the role was given.

    The canonical schema guards `path` with a traversal `pattern`, but codex_subset strips it
    (lookarounds are outside the provider subset), so any string was emittable. The model then
    cited a path that is real in its own snapshot but absent from its inventory, and
    verify_evidence_bindings killed the whole response for one bad ref out of ten -- which is how
    round 2 of the second production audit lost its targeted adjudication.

    Both one-off recovery scripts already do this (retry_completeness_once.constrain_bindings,
    retry_targeted_adjudication_once.constrain_bindings); production never did. Constraining here
    makes the bad citation unemittable rather than fatal after the fact.
    """
    paths: list[str] = []
    hashes: list[str] = []
    for relative, row in sorted(inventory.items()):
        if not isinstance(relative, str) or not isinstance(row, dict) \
                or set(row) != {"sha256", "size"}:
            raise ValueError("frozen evidence inventory is malformed")
        digest = row.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("frozen evidence inventory contains an invalid hash")
        paths.append(relative)
        hashes.append(digest)
    if not paths:
        return
    binding = schema["$defs"]["artifact_binding"]["properties"]
    binding["path"] = {"type": "string", "enum": paths}
    binding["sha256"] = {"type": "string", "enum": sorted(set(hashes))}


def critic_schema(
    role: str, round_number: int, report_sha: str, audit_id: str,
    invocation_id: str, input_hashes: dict[str, str],
    covered_claim_ids: list[str] | None = None,
    assigned_check_ids: list[str] | None = None,
    evidence_inventory: dict[str, Any] | None = None,
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
            # The canonical schema and validate_critic_obj retain every omitted constraint.
            if key in (
                    "$id", "allOf", "if", "then", "else", "uniqueItems",
                    "minLength", "maxLength"):
                continue
            if key == "pattern" and any(
                    marker in str(item) for marker in ("(?=", "(?!", "(?<=", "(?<!")):
                continue
            out["anyOf" if key == "oneOf" else key] = codex_subset(item)
        return out

    schema = codex_subset(load(CRITIC_SCHEMA))
    properties = schema["properties"]
    properties["schema_version"] = json_const_schema(
        "2.0.0", properties["schema_version"])
    properties["audit_id"] = json_const_schema(audit_id, properties["audit_id"])
    properties["invocation_id"] = json_const_schema(
        invocation_id, properties["invocation_id"])
    properties["round"] = json_const_schema(round_number, properties["round"])
    properties["role"] = json_const_schema(f"critic_{role}", properties["role"])
    properties["report_sha256"] = json_const_schema(
        report_sha, properties["report_sha256"])
    for name, digest in input_hashes.items():
        hash_properties = properties["input_hashes"]["properties"]
        hash_properties[name] = json_const_schema(digest, hash_properties[name])
    if covered_claim_ids is not None:
        properties["covered_claim_ids"] = json_const_schema(
            covered_claim_ids, properties["covered_claim_ids"])
        items = properties["items"]
        items["minItems"] = len(covered_claim_ids)
        items["maxItems"] = len(covered_claim_ids)
        if covered_claim_ids:
            schema["$defs"]["assessment"]["properties"]["finding_id"] = {
                "type": "string",
                "enum": covered_claim_ids,
            }
    if assigned_check_ids is not None:
        coverage = properties["coverage"]["properties"]
        coverage["assigned_checks"] = json_const_schema(
            assigned_check_ids, coverage["assigned_checks"])
        # completed_checks stays pinned: _repair_candidate reads it back out of the contract via
        # _exact_array_from_contract to derive the corrected administrative values, so it is the
        # focused-repair path's source of truth, not merely a completeness assertion.
        coverage["completed_checks"] = json_const_schema(
            assigned_check_ids, coverage["completed_checks"])
    # status, completion_marker and the two declaration booleans were pinned as consts, so the
    # decoder was COMPELLED to assert that every assigned check was accounted for and every
    # assessment evidenced, whatever the model had actually done -- a manufactured completeness
    # declaration in a reproducibility tool's own record. Nothing downstream loosens: validate_v2
    # already accepts partial|complete|failed and ties completion_marker to status, and
    # validate_critic_obj still admits ONLY status=complete, so a truthful partial is retained as
    # a failed attempt and re-sampled, exactly as the contract describes.
    declaration = properties["completeness_declaration"]["properties"]
    if assigned_check_ids is None:
        schema["properties"]["coverage"]["properties"]["assigned_checks"]["minItems"] = 1
        schema["properties"]["coverage"]["properties"]["completed_checks"]["minItems"] = 1
    if evidence_inventory is not None:
        constrain_evidence_bindings(schema, evidence_inventory)
    return schema


def render_contract(prompts: pathlib.Path, inventory_path: str) -> str:
    """The shared output contract, addressed to whichever snapshot layout the role actually has.

    Both the critic prompts and the targeted-adjudicator prompt are graded by the same validator
    (command_validate_adjudication runs validate_critic_obj), so both must be shown the same
    contract. Lines beginning with '#' are rationale for maintainers and are stripped before the
    text reaches a model.
    """
    text = (prompts / "contract.txt").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.split("\n") if not line.startswith("#"))
    return body.replace("{{INVENTORY}}", inventory_path).strip() + "\n"


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
    # mkdtemp creates the directory 0700, and this is the directory that is later moved across
    # parents into staging. In both observed File Provider drifts (2026-09-24 and 2026-09-27) the
    # path that regained write permission was exactly this one, reset to 0700 after the freeze, as
    # if the provider re-applied the mode it recorded at creation. Give it an ordinary mode from
    # the start; make_read_only still sets the frozen mode, and hash_tree ignores modes.
    source.chmod(0o755)
    staging: pathlib.Path | None = None
    try:
        (source / "prompts").mkdir()
        (source / "context").mkdir()
        shutil.copyfile(report, source / "REPORT_TO_REVIEW.md")
        shutil.copyfile(prompts / "common.txt", source / "prompts/common.txt")
        shutil.copyfile(prompts / "contract.txt", source / "prompts/contract.txt")
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

        routing = load(source / "ROUTING.json")
        if routing.get("claim_assignment_mode") not in {
                "domain_routed_round_1", "domain_routed_delta"}:
            raise ValueError("routing lacks the required domain-routed claim-assignment mode")
        assignments = routing.get("claim_assignments")
        if not isinstance(assignments, dict) or set(assignments) != set(roles):
            raise ValueError("routing claim assignments do not match routed roles")
        for assigned_role in roles:
            values = assignments[assigned_role]
            if (not isinstance(values, list) or len(values) != len(set(values)) or
                    any(not isinstance(value, str) or not value for value in values)):
                raise ValueError(f"routing claim assignment is malformed for {assigned_role}")

        registry_rows = load_registry_file(source / "context/work/findings.jsonl")
        (source / "role_packets").mkdir()
        for role in roles:
            assigned_rows = []
            evidence_paths = []
            for claim_id in assignments[role]:
                row = registry_rows.get(claim_id.casefold())
                if row is None:
                    raise ValueError(f"role {role} references missing canonical claim {claim_id}")
                assigned_rows.append({
                    "id": row["id"], "claim": row["claim"], "domain": row["domain"],
                    "impact": row["impact"], "impact_rank": row["impact_rank"],
                    "changes_conclusion": row["changes_conclusion"],
                    "disclosure_flag": row["disclosure_flag"],
                    "source_location": row["source_location"], "remedy": row["remedy"],
                })
                for binding in (row.get("evidence") or {}).get("source_hashes") or []:
                    evidence_paths.append("source/evidence/" + binding["path"])
            atomic_json(source / f"role_packets/{role}.json", {
                "packet_version": "i4r-critic-role-packet-1",
                "role": f"critic_{role}", "round": args.round,
                "assignment_mode": routing["claim_assignment_mode"],
                "assigned_check_ids": ROLE_CHECK_IDS[role],
                "assigned_claim_ids": assignments[role], "assigned_claims": assigned_rows,
                "report": "source/REPORT_TO_REVIEW.md",
                "evidence_inventory": "source/context/EVIDENCE_HASHES.json",
                "relevant_evidence_paths": list(dict.fromkeys(evidence_paths)),
            })

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
        # The input identity covers what the critic is asked to review and how: the frozen
        # source tree, the round, model, effort, roles, audit and the critic output contract.
        # It deliberately excludes the driver, helper and validator code hashes. Those are
        # recorded in MANIFEST.json as telemetry, but binding them into the identity meant every
        # bug fix to the runtime orphaned an authorized round and its valid cached reviews; the
        # first production audit lost a complete four-critic round that way.
        identity_parts = round_identity_parts({
            "provider_schema_generation_version": PROVIDER_SCHEMA_GENERATION_VERSION,
            "round": args.round, "model": args.model, "effort": args.effort,
            "roles": roles, "audit_id": audit_id,
            "canonical_critic_schema_sha256": canonical_schema_sha,
        })
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
        if snapshot.exists() or snapshot.is_symlink():
            if snapshot.is_symlink() or not snapshot.is_dir():
                raise SnapshotIntegrityError(
                    f"corrupt snapshot: {snapshot_where(snapshot)} is not a real directory\n"
                    + snapshot_delete_remedy(snapshot))
            # A missing, unreadable or malformed MANIFEST.json, a content-address mismatch and a
            # hash or inventory failure are integrity faults (exit 5): nothing a relaunch with
            # other settings can cure. A different launch below is not; it stays a plain error.
            manifest = load_snapshot_manifest(snapshot)
            # The reuse check compares the identity-bearing fields only. The driver, helper and
            # validator hashes stay in MANIFEST.json as telemetry of the snapshot's creator, but
            # comparing them here re-created the orphaning the identity change removed: a same-
            # input relaunch after any runtime bug fix read its own valid snapshot as corrupt.
            expected_manifest = {
                "snapshot_version": "i4r-runtime-snapshot-5",
                "provider_schema_generation_version": PROVIDER_SCHEMA_GENERATION_VERSION,
                "input_sha256": input_sha,
                "report_sha256": report_sha,
                "round": args.round,
                "model": args.model,
                "effort": args.effort,
                "roles": roles,
                "audit_id": audit_id,
                "canonical_critic_schema_sha256": canonical_schema_sha,
            }
            mismatched = {key: (manifest.get(key), value)
                          for key, value in expected_manifest.items() if manifest.get(key) != value}
            # The content address itself, the snapshot format and the schema generation (bound
            # into the address) cannot differ for a directory found at this address unless its
            # MANIFEST.json was damaged: that is corruption (exit 5), classified exactly as the
            # pre-dispatch check classifies it. Only the launch parameters can differ legitimately.
            corrupted = sorted(key for key in mismatched if key in ROUND_SNAPSHOT_CORRUPTION_KEYS)
            if corrupted:
                raise SnapshotIntegrityError(
                    f"corrupt snapshot manifest: {snapshot_where(snapshot)}\n"
                    f"  MANIFEST.json {', '.join(corrupted)} does not match the directory's "
                    "content address and format, which no launch can change.\n"
                    + snapshot_delete_remedy(snapshot))
            if mismatched:
                detail = "; ".join(f"{k}: snapshot has {a!r}, this launch asks for {b!r}"
                                   for k, (a, b) in sorted(mismatched.items()))
                raise ValueError(
                    f"snapshot was built for a different launch: {snapshot}\n"
                    f"  {detail}\n"
                    "  The snapshot is content-addressed on the SOURCE tree only, so the same "
                    "directory is found when roles, model, effort or round change.\n"
                    f"  Remedy: in the audit directory run `chmod -R u+w {snapshot_relative(snapshot)} "
                    f"&& rm -rf {snapshot_relative(snapshot)}`; it will be rebuilt for this launch."
                )
            for key in ("runtime_script_sha256", "runtime_helper_sha256",
                        "schema_validator_sha256", "semantic_validator_sha256"):
                if not isinstance(manifest.get(key), str) or len(manifest[key]) != 64:
                    raise SnapshotIntegrityError(
                        f"corrupt snapshot manifest: {snapshot_where(snapshot)}\n"
                        f"  MANIFEST.json field {key!r} is missing or not a SHA-256 hex digest.\n"
                        + snapshot_delete_remedy(snapshot)
                    )
            if hash_tree(snapshot / "source", identity_parts) != input_sha:
                raise SnapshotIntegrityError(
                    "snapshot source no longer matches its content address: "
                    f"{snapshot_where(snapshot)}\n" + snapshot_delete_remedy(snapshot))
            ensure_frozen(snapshot, manifest, audit_root=workspace, context="snapshot_reuse")
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
            assignments = routing["claim_assignments"]
            for role in roles:
                role_prompt = (source / f"prompts/role_{role}.txt").read_text()
                rendered = render_template(common, context, args.round)
                rendered += "\n\n" + render_contract(
                    source / "prompts", "source/context/EVIDENCE_HASHES.json")
                rendered += "\n\n" + render_template(role_prompt, context, args.round)
                if args.round >= 2:
                    rendered += f"""

## Later-round delta scope (supersedes full-document review instructions above)

Start from `source/context/DIFF_MANIFEST.json`: review its changed_claims,
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

The workspace is the exact immutable role snapshot and is read-only. Do not create or edit a file.
Start with `source/role_packets/{role}.json`, then review `source/REPORT_TO_REVIEW.md`. The role
packet is your assignment, not evidence: it is deliberately absent from the frozen inventory, so
citing it in `artifact_refs` fails your whole role. Cite the inputs it points you at instead.
Open only the
evidence paths named in the packet unless a claim-specific verdict requires another artifact listed
in `source/context/EVIDENCE_HASHES.json`. No live audit workspace is available to this session.
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
runtime_snapshot=.
runtime_role_packet=source/role_packets/{role}.json
runtime_diff_manifest={'source/context/DIFF_MANIFEST.json' if args.diff_manifest else 'none'}
runtime_findings_registry={'source/context/work/findings.jsonl' if (source / 'context/work/findings.jsonl').is_file() else 'none'}
runtime_evidence_inventory=source/context/EVIDENCE_HASHES.json
runtime_pipeline_policy=source/context/PIPELINE_POLICY.json
runtime_claim_assignment_mode={routing['claim_assignment_mode']}
runtime_covered_claim_ids={json.dumps(assignments[role], separators=(',', ':'))}
runtime_assigned_check_ids={json.dumps(ROLE_CHECK_IDS[role], separators=(',', ':'))}
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
                atomic_provider_schema(
                    staging / f"schemas/critic_{role}.schema.json",
                    critic_schema(
                        role, args.round, report_sha, audit_id, logical_invocation_id,
                        input_hashes, assignments[role], ROLE_CHECK_IDS[role],
                        evidence_inventory=evidence_inventory,
                    ),
                )
            atomic_provider_schema(
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
                    evidence_inventory=evidence_inventory,
                ),
            )
            atomic_provider_schema(
                staging / "schemas/probe.schema.json",
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["status", "critic_contract", "adjudicator_contract"],
                    "properties": {
                        "status": {"type": "string", "const": "ok"},
                        "critic_contract": {
                            "type": "object", "additionalProperties": False,
                            "required": ["role", "covered_claim_ids", "evidence"],
                            "properties": {
                                "role": {"type": "string", "const": "critic_stats"},
                                "covered_claim_ids": json_const_schema(
                                    ["probe-claim"],
                                    {"type": "array", "items": {"type": "string"}},
                                ),
                                "evidence": {
                                    "type": "object", "additionalProperties": False,
                                    "required": ["artifact_refs", "operation", "result"],
                                    "properties": {
                                        "artifact_refs": {
                                            "type": "array", "minItems": 1, "maxItems": 1,
                                            "items": {
                                                "type": "object", "additionalProperties": False,
                                                "required": ["path", "sha256"],
                                                "properties": {
                                                    "path": {"type": "string", "const": "source/probe.txt"},
                                                    "sha256": {"type": "string", "const": "0" * 64},
                                                },
                                            },
                                        },
                                        "operation": {"type": "string", "const": "probe operation"},
                                        "result": {"type": "string", "const": "probe result"},
                                    },
                                },
                            },
                        },
                        "adjudicator_contract": {
                            "type": "object", "additionalProperties": False,
                            "required": ["role", "covered_claim_ids", "decision_question"],
                            "properties": {
                                "role": {"type": "string", "const": "critic_synth"},
                                "covered_claim_ids": json_const_schema(
                                    ["probe-claim"],
                                    {"type": "array", "items": {"type": "string"}},
                                ),
                                "decision_question": {
                                    "type": "string", "const": "Is the evidence complete?"
                                },
                            },
                        },
                    },
                },
            )
            file_hashes = {
                path.relative_to(staging).as_posix(): sha_file(path)
                for path in sorted(staging.rglob("*")) if path.is_file()
            }
            atomic_json(staging / "MANIFEST.json", {
                "snapshot_version": "i4r-runtime-snapshot-5",
                "provider_schema_generation_version": PROVIDER_SCHEMA_GENERATION_VERSION,
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
    schema["properties"]["invocation_id"] = json_const_schema(
        args.invocation_id, schema["properties"]["invocation_id"])
    atomic_provider_schema(args.output, schema)
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
        "triggers", "claim_merge_sha256", "unresolved_rows", "deferred_rows", "critic_outputs",
    }, "adjudication packet")
    if packet["packet_version"] != "i4r-targeted-adjudication-packet-3":
        raise ValueError("unsupported adjudication packet version")
    if not packet["triggers"]:
        raise ValueError("targeted adjudication requires at least one trigger")

    source = pathlib.Path(tempfile.mkdtemp(prefix=".target-source.", dir=out))
    source.chmod(0o755)  # see command_snapshot: never let the moved directory be born 0700
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
                "decision_question", "adjudication_ready", "defer_reasons", "claim_merge",
            }, f"unresolved_rows[{index}]")
            nonempty(row["row_id"], f"unresolved_rows[{index}].row_id")
            if row["row_id"] in row_ids:
                raise ValueError("target unresolved row IDs must be unique")
            row_ids.append(row["row_id"])
            if not isinstance(row["trigger_ids"], list) or not row["trigger_ids"]:
                raise ValueError(f"unresolved_rows[{index}] lacks trigger IDs")
            if row["adjudication_ready"] is not True or row["defer_reasons"] != []:
                raise ValueError(f"unresolved_rows[{index}] is not evidence-complete")
            nonempty(row["decision_question"], f"unresolved_rows[{index}].decision_question")

        round_inventory = load(round_snapshot / "source/context/EVIDENCE_HASHES.json")
        round_manifest_hashes = round_manifest.get("files_sha256", {})
        evidence_paths: dict[str, str] = {}
        for row in unresolved_rows:
            claim_merge = row["claim_merge"]
            if not isinstance(claim_merge, dict):
                raise ValueError("evidence-complete adjudication row lacks a claim merge")
            for binding in claim_merge["canonical_evidence"]["source_hashes"]:
                evidence_paths[binding["path"]] = binding["sha256"]
            for role_result in claim_merge["role_results"]:
                for binding in role_result["evidence_bindings"]["artifact_refs"]:
                    evidence_paths[binding["path"]] = binding["sha256"]
                script = role_result["evidence_bindings"].get("reproducible_script")
                if script:
                    evidence_paths[script["path"]] = script["sha256"]
        copied_evidence = []
        for relative, expected_sha in sorted(evidence_paths.items()):
            normalized = safe_relative_path(relative, "target evidence path").as_posix()
            if not normalized.startswith("source/"):
                raise ValueError(f"target evidence path is outside the frozen source tree: {normalized}")
            inventory_row = round_inventory.get(normalized)
            artifact = regular_without_symlinks(round_snapshot, normalized, "target evidence path")
            if (not isinstance(inventory_row, dict) or inventory_row.get("sha256") != expected_sha
                    or sha_file(artifact) != expected_sha
                    or round_manifest_hashes.get(normalized) != expected_sha):
                raise ValueError(f"target evidence is not bound by the round snapshot: {normalized}")
            destination = source / normalized.removeprefix("source/")
            if destination.exists():
                if sha_file(destination) != expected_sha:
                    raise ValueError(f"target evidence destination collision: {normalized}")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(artifact, destination)
            copied_evidence.append(destination)

        # The adjudicator receives only unresolved merged rows, never paths to or copies of the
        # full critic outputs. The full packet remains outside the provider snapshot for projection.
        atomic_json(source / "adjudication_packet.json", {
            "packet_version": "i4r-targeted-adjudication-input-2",
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
            *copied_evidence,
        ):
            rel = "source/" + path.relative_to(source).as_posix()
            evidence_inventory[rel] = {
                "sha256": sha_file(path), "size": path.stat().st_size,
            }
        atomic_json(source / "EVIDENCE_HASHES.json", evidence_inventory)

        target_identity = target_identity_parts({
            "provider_schema_generation_version": PROVIDER_SCHEMA_GENERATION_VERSION,
            "model": args.model, "effort": args.effort,
            "runtime_helper_sha256": sha_file(__file__),
        }, sha_file(args.runtime_script))
        target_sha = hash_tree(source, target_identity)
        snapshot = out / "target_snapshots" / target_sha
        snapshot_rel = f"critique/round_{packet['round']}/target_snapshots/{target_sha}"
        expected_manifest = {
            "snapshot_version": "i4r-target-snapshot-4",
            "provider_schema_generation_version": PROVIDER_SCHEMA_GENERATION_VERSION,
            "runtime_helper_sha256": sha_file(__file__),
            "target_input_sha256": target_sha,
            "adjudication_input_sha256": sha_file(source / "adjudication_packet.json"),
            "round_input_sha256": packet["input_sha256"],
            "report_sha256": packet["report_sha256"],
            "round": packet["round"],
            "audit_id": packet["audit_id"],
            "model": args.model,
            "effort": args.effort,
            "roles": copied_roles,
        }
        if snapshot.exists() or snapshot.is_symlink():
            if snapshot.is_symlink() or not snapshot.is_dir():
                raise SnapshotIntegrityError(
                    f"corrupt snapshot: {snapshot_where(snapshot)} is not a real directory\n"
                    + snapshot_delete_remedy(snapshot))
            manifest = load_snapshot_manifest(snapshot)
            # Every expected field is bound into the content address, so any difference here is
            # a damaged manifest, not a different launch.
            if any(manifest.get(key) != value for key, value in expected_manifest.items()):
                raise SnapshotIntegrityError(
                    f"corrupt target snapshot: {snapshot_where(snapshot)}\n"
                    + snapshot_delete_remedy(snapshot))
            if hash_tree(snapshot / "source", target_identity) != target_sha:
                raise SnapshotIntegrityError(
                    "target snapshot source no longer matches its content address: "
                    f"{snapshot_where(snapshot)}\n" + snapshot_delete_remedy(snapshot))
            ensure_frozen(snapshot, manifest, audit_root=workspace,
                          context="target_snapshot_reuse")
        else:
            staging = out / "target_snapshots" / f".{target_sha}.{os.getpid()}.tmp"
            staging.mkdir(parents=True)
            shutil.move(str(source), staging / "source")
            source = staging / "source"
            schema = load(source / "base.schema.json")
            coverage = schema["properties"]["coverage"]["properties"]
            exact_array = {"type": "array", "minItems": len(row_ids),
                           "maxItems": len(row_ids),
                           "items": {"type": "string", "enum": row_ids}}
            coverage["assigned_checks"] = exact_array
            coverage["completed_checks"] = dict(exact_array)
            coverage["skipped_checks"]["maxItems"] = 0
            schema["$defs"]["assessment"]["properties"]["item_id"] = {
                "type": "string", "enum": row_ids}
            schema["$defs"]["assessment"]["properties"]["finding_id"] = {
                "type": "string", "enum": row_ids}
            schema["properties"]["items"]["minItems"] = len(row_ids)
            schema["properties"]["items"]["maxItems"] = len(row_ids)
            # command_validate_adjudication requires items+new_issues to equal the trigger set
            # exactly, and items alone already does; any new issue therefore made the response a
            # strict superset and was rejected whatever it said. The schema left new_issues
            # unbounded and the prompt said nothing, so the model could not know. Make it
            # unemittable rather than fatal after the fact -- retry_targeted_adjudication_once.py
            # has done exactly this since it was written.
            schema["properties"]["new_issues"]["maxItems"] = 0
            schema["properties"]["covered_claim_ids"] = json_const_schema(
                row_ids, schema["properties"]["covered_claim_ids"])
            logical_invocation_id = (
                f"{packet['audit_id']}:r{packet['round']}:critic_synth:{target_sha[:16]}"
            )
            schema["properties"]["invocation_id"] = json_const_schema(
                logical_invocation_id, schema["properties"]["invocation_id"])
            constrain_evidence_bindings(schema, evidence_inventory)
            # The adjudicator is graded by the full critic validator
            # (command_validate_adjudication -> validate_critic_obj), so it must be shown the same
            # contract the critics get -- rendered against ITS snapshot layout, where the evidence
            # inventory sits at source/EVIDENCE_HASHES.json rather than source/context/.
            adjudicator_contract = render_contract(
                round_snapshot / "source/prompts", "source/EVIDENCE_HASHES.json")
            prompt = f"""I4R_TARGETED_ADJUDICATION
Resolve only the enumerated triggers; do not synthesize or restate the whole critique.
Use only evidence-complete `unresolved_rows` in the immutable packet; resolved merge rows and full
critic outputs are not present. Answer each decision_question from the bound canonical source and
critic evidence copied into this read-only snapshot. Do not edit files.
Return exactly one raw JSON object matching the supplied schema; no Markdown fence or preamble.
runtime_packet=source/adjudication_packet.json
runtime_report=source/REPORT_TO_REVIEW.md
runtime_evidence_inventory=source/EVIDENCE_HASHES.json
runtime_snapshot=.
runtime_input_sha256={target_sha}
runtime_round_input_sha256={packet['input_sha256']}
runtime_report_sha256={packet['report_sha256']}
runtime_audit_id={packet['audit_id']}
runtime_round={packet['round']}

{adjudicator_contract}
Two rules differ for this role. Your `covered_claim_ids` and `items` are exactly the enumerated
trigger rows, so `new_issues` must be empty: raise nothing outside the triggers you were given.
Your evidence inventory is `source/EVIDENCE_HASHES.json`, not the critic path.
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
                hash_properties = schema["properties"]["input_hashes"]["properties"]
                hash_properties[name] = json_const_schema(digest, hash_properties[name])
            atomic_provider_schema(staging / "output.schema.json", schema)
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
        if (round_manifest.get("snapshot_version") != "i4r-runtime-snapshot-5" or
                round_manifest.get("input_sha256") != args.input_sha or
                round_manifest.get("report_sha256") != args.report_sha or
                round_manifest.get("round") != args.round or
                (round_manifest.get("model"), round_manifest.get("effort")) !=
                (critic_model, critic_effort)):
            raise ValueError("ordinary critic snapshot model, effort, or identity differs")
        ensure_frozen(round_snapshot, round_manifest, audit_root=audit_root,
                      context="projection", invocation=args.invocation)
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
        if (assignment_mode not in {"domain_routed_round_1", "domain_routed_delta"} or
                not isinstance(assignments, dict) or set(assignments) != set(roles)):
            raise ValueError("projection routing has malformed claim assignments")
        if args.round == 1 and assignment_mode != "domain_routed_round_1":
            raise ValueError("round 1 projection must declare domain-routed assignment")
        if args.round > 1 and assignment_mode != "domain_routed_delta":
            raise ValueError("later-round projection must declare domain-routed delta assignment")
        packet_path = pathlib.Path(args.packet)
        packet_obj = load(packet_path)
        exact_keys(packet_obj, {
            "packet_version", "audit_id", "round", "input_sha256", "report_sha256", "policy",
            "triggers", "claim_merge_sha256", "unresolved_rows", "deferred_rows", "critic_outputs",
        }, "projection adjudication packet")
        if (packet_obj["packet_version"] != "i4r-targeted-adjudication-packet-3" or
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
            # Set comparison, casefolded, matching validate_critic_obj:1819 and
            # validate_v2.validate_critic_result. These were plain list `!=`, so a critic that
            # covered exactly its assignment but emitted the ids in a different order -- or in a
            # different case -- was rejected here. No prompt states an ordering rule and the
            # provider schema cannot express one (see json_const_schema's own docstring), so the
            # requirement was unsatisfiable except by luck. Worse, this runs in the projection,
            # AFTER every critic has passed validation and been written to result_cache: a rerun
            # replays the identical cached output and dies at the same line, so the round could
            # only be freed by deleting the cache by hand.
            expected_claims = {value.casefold() for value in assignments[role]}
            if {value.casefold() for value in obj["covered_claim_ids"]} != expected_claims:
                raise ValueError(f"critic_{role} coverage differs from its machine assignment")
            assessed = [item["finding_id"] for item in obj["items"]]
            if {value.casefold() for value in assessed} != expected_claims:
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
        rebuilt_rows = unresolved_rows_for_packet(packet_obj["triggers"], merge_obj)
        if ([row for row in rebuilt_rows if row["adjudication_ready"]]
                != packet_obj["unresolved_rows"] or
                [row for row in rebuilt_rows if not row["adjudication_ready"]]
                != packet_obj["deferred_rows"]):
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
            if (target_manifest.get("snapshot_version") != "i4r-target-snapshot-4" or
                    target_manifest.get("target_input_sha256") != args.target_input_sha or
                    target_manifest.get("round_input_sha256") != args.input_sha or
                    target_manifest.get("report_sha256") != args.report_sha or
                    target_manifest.get("round") != args.round or
                    (target_manifest.get("model"), target_manifest.get("effort")) !=
                    (adjudicator_model, adjudicator_effort)):
                raise ValueError("targeted-adjudicator snapshot model, effort, or identity differs")
            ensure_frozen(target_snapshot, target_manifest, audit_root=audit_root,
                          context="projection", invocation=args.invocation)
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
        if minimum > 1:
            # Say the bound actually applied. "must be a non-empty string" sent a maintainer
            # looking for an empty field that was not there (overall.summary at 19 chars).
            raise ValueError(f"{where} must be a string of at least {minimum} non-space characters")
        raise ValueError(f"{where} must be a non-empty string")


def validate_critic_obj(obj: Any, args: argparse.Namespace) -> None:
    result_path = getattr(args, "result_path", "")
    expected_input = getattr(args, "input_sha", "")
    snapshot_verified = False
    if result_path and expected_input:
        # The frozen snapshot comes first, whatever the response says: if it changed during the
        # call the response cannot be judged at all, and the driver must see an environment fault
        # (exit 5), never a schema error it would re-sample against the same broken snapshot.
        verify_result_snapshot(pathlib.Path(result_path), args.round, expected_input,
                               f"critic_{args.role}")
        snapshot_verified = True
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
    if result_path:
        # snapshot_sha256 was checked against the expected input above, so this is the snapshot
        # already proven intact; it is not rehashed twice.
        verify_evidence_bindings(
            obj, pathlib.Path(result_path), args.round,
            obj["input_hashes"]["snapshot_sha256"], snapshot_verified=snapshot_verified,
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


REPAIR_SCIENTIFIC_FIELDS = (
    "covered_claim_ids", "conflicts", "settled_spot_checks", "items", "new_issues", "overall",
)


def _scientific_value(field: str, value: Any) -> Any:
    """The scientific content of a field, with its one administrative key normalised.

    A new issue has no registry row yet, so its `finding_id` must be null. Critics often fill it
    with the nearest existing id; that is an administrative slip, not science. Normalising it here
    lets the bounded repair path fix it while every other byte of `new_issues` stays frozen.
    """
    value = copy.deepcopy(value)
    if field == "new_issues" and isinstance(value, list):
        for issue in value:
            if isinstance(issue, dict) and "finding_id" in issue:
                issue["finding_id"] = None
    return value


def _exact_array_from_contract(schema: dict[str, Any], field_path: tuple[str, ...]) -> list[str]:
    node: Any = schema
    for key in field_path:
        node = node[key]
    if not isinstance(node, dict):
        raise ValueError("repair contract array is malformed")
    minimum, maximum = node.get("minItems"), node.get("maxItems")
    enum = (node.get("items") or {}).get("enum")
    if (not isinstance(enum, list) or not all(isinstance(value, str) for value in enum)
            or minimum != maximum or minimum != len(enum)):
        raise ValueError("repair requires an exact provider-contract array")
    return list(enum)


def _const_from_contract(schema: dict[str, Any], field_path: tuple[str, ...]) -> Any:
    node: Any = schema
    for key in field_path:
        node = node[key]
    if not isinstance(node, dict) or "const" not in node:
        raise ValueError("repair requires an exact provider-contract scalar")
    return node["const"]


def _repair_candidate(obj: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Repair only invocation/coverage administration, then prove the science is already valid."""
    if not isinstance(obj, dict):
        raise ValueError("provider response is not a JSON object")
    for field in REPAIR_SCIENTIFIC_FIELDS:
        if field not in obj:
            raise ValueError(f"scientific field {field} is missing and cannot be repaired")
    schema = load(args.schema)
    candidate = {field: _scientific_value(field, obj[field]) for field in REPAIR_SCIENTIFIC_FIELDS}
    candidate.update({
        "schema_version": "2.0.0",
        "audit_id": args.audit_id,
        "invocation_id": _const_from_contract(schema, ("properties", "invocation_id")),
        "round": args.round,
        "role": f"critic_{args.role}",
        "report_sha256": args.report_sha,
        "input_hashes": {
            name: _const_from_contract(
                schema, ("properties", "input_hashes", "properties", name)
            )
            for name in (
                "snapshot_sha256", "report_sha256", "prompt_sha256", "policy_sha256",
                "evidence_manifest_sha256",
            )
        },
        "status": "complete",
        "completion_marker": True,
        "completeness_declaration": {
            "all_assigned_checks_accounted_for": True,
            "all_assessments_evidenced": True,
            "unresolved_omissions": [],
        },
        "coverage": {
            "assigned_checks": _exact_array_from_contract(
                schema, ("properties", "coverage", "properties", "assigned_checks")
            ),
            "completed_checks": _exact_array_from_contract(
                schema, ("properties", "coverage", "properties", "completed_checks")
            ),
            "skipped_checks": [],
        },
    })
    validation_args = argparse.Namespace(**vars(args))
    validation_args.result_path = args.input
    validate_critic_obj(candidate, validation_args)
    return candidate


def _canonical_field_hashes(obj: dict[str, Any]) -> dict[str, str]:
    return {
        field: hashlib.sha256(
            json.dumps(_scientific_value(field, obj[field]), sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        for field in REPAIR_SCIENTIFIC_FIELDS
    }


def command_prepare_repair(args: argparse.Namespace) -> int:
    original = load(args.input)
    preliminary_candidate = _repair_candidate(original, args)
    diagnostics = pathlib.Path(args.diagnostics).read_text(errors="replace")[:4000]
    original_text = pathlib.Path(args.input).read_text(errors="replace")
    runtime_lines = []
    for line in pathlib.Path(args.original_prompt).read_text(errors="replace").splitlines():
        if re.fullmatch(r"runtime_[a-z0-9_]+=[^\n]+", line):
            runtime_lines.append(line)
    expected_admin = {
        key: preliminary_candidate[key]
        for key in (
            "schema_version", "audit_id", "invocation_id", "round", "role", "report_sha256",
            "status", "completion_marker", "completeness_declaration", "coverage",
        )
    }
    prompt = "\n".join([
        "I4R_OUTPUT_REPAIR",
        *runtime_lines,
        "The prior response is scientifically usable but failed only its output contract.",
        "Return one JSON object matching the bound schema. Preserve every value in these fields "
        "exactly: covered_claim_ids, conflicts, settled_spot_checks, items, new_issues, overall.",
        "Replace only the administrative fields with EXPECTED_ADMIN. The one exception inside "
        "new_issues: every new_issues[].finding_id must be null. Do not add findings, change "
        "verdicts, edit evidence, or use tools.",
        "VALIDATOR_DIAGNOSTICS:", diagnostics,
        "EXPECTED_ADMIN:", json.dumps(expected_admin, sort_keys=True, separators=(",", ":")),
        "PRIOR_RESPONSE:", original_text,
        "",
    ])
    snapshot = pathlib.Path(args.snapshot)
    accessible_bytes = pathlib.Path(args.original_prompt).stat().st_size
    accessible_bytes += sum(
        path.stat().st_size for path in (snapshot / "source").rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    maximum = accessible_bytes // 5
    prompt_bytes = len(prompt.encode("utf-8"))
    if maximum <= 0 or prompt_bytes > maximum:
        raise ValueError(
            f"focused repair packet is {prompt_bytes} bytes; 20% limit is {maximum} bytes"
        )
    atomic_text(args.output, prompt)
    # Bind the repaired result to the actual focused prompt. Input hashes are deliberately omitted
    # from EXPECTED_ADMIN above so this update cannot create a prompt-hash self-reference.
    repair_schema = load(args.schema)
    prompt_hash_node = repair_schema["properties"]["input_hashes"]["properties"]["prompt_sha256"]
    prompt_hash_node["const"] = sha_file(args.output)
    atomic_provider_schema(args.schema, repair_schema)
    candidate = _repair_candidate(original, args)
    atomic_json(args.receipt, {
        "repair_version": "i4r-focused-output-repair-1",
        "original_response_sha256": sha_file(args.input),
        "validator_diagnostics_sha256": sha_file(args.diagnostics),
        "provider_schema_sha256": sha_file(args.schema),
        "scientific_field_sha256": _canonical_field_hashes(original),
        "repaired_contract_prompt_sha256": candidate["input_hashes"]["prompt_sha256"],
        "original_packet_bytes": accessible_bytes,
        "repair_prompt_bytes": prompt_bytes,
        "maximum_repair_bytes": maximum,
        "ratio": prompt_bytes / accessible_bytes,
    })
    return 0


def command_validate_repair(args: argparse.Namespace) -> int:
    before, after = load(args.before), load(args.input)
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ValueError("repair comparison requires two JSON objects")
    before_hashes = _canonical_field_hashes(before)
    after_hashes = _canonical_field_hashes(after)
    if before_hashes != after_hashes:
        changed = [field for field in REPAIR_SCIENTIFIC_FIELDS
                   if before_hashes.get(field) != after_hashes.get(field)]
        raise ValueError("focused repair changed scientific fields: " + ", ".join(changed))
    args.result_path = args.input
    validate_critic_obj(after, args)
    markdown = pathlib.Path(args.markdown)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(f"{markdown}.tmp.{os.getpid()}")
    tmp.write_text(render_critic(after))
    os.replace(tmp, markdown)
    atomic_json(args.receipt, {
        "repair_validation_version": "i4r-focused-output-repair-validation-1",
        "before_sha256": sha_file(args.before),
        "after_sha256": sha_file(args.input),
        "scientific_field_sha256": before_hashes,
        "scientific_fields_byte_equivalent": True,
    })
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


def canonicalize_codex_usage(
    usage: dict[str, int], usage_extras: dict[str, int],
) -> dict[str, Any]:
    """Return schema-v2 usage without double-counting Codex token subsets.

    Codex ``turn.completed`` events may omit ``total_tokens``.  In those events,
    ``cached_input_tokens`` is a subset of ``input_tokens`` and
    ``reasoning_output_tokens`` (normalized to ``reasoning_tokens`` above) is a
    subset of ``output_tokens``.  Cache-write input is likewise diagnostic cache
    detail, not an additional token class.  Therefore the only exact derived
    total supported here is ``input_tokens + output_tokens``.
    """
    component_values = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cached_input_tokens": usage.get("cached_input_tokens"),
        "cache_creation_input_tokens": usage_extras.get("cache_write_input_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
    }
    reported_total = usage.get("total_tokens")
    if reported_total is not None:
        total_value = reported_total
        total_kind = "provider_reported"
        total_components: list[str] = []
        total_unavailable_reason = None
    elif all(component_values[name] is not None for name in ("input_tokens", "output_tokens")):
        total_value = component_values["input_tokens"] + component_values["output_tokens"]
        total_kind = "derived"
        total_components = ["input_tokens", "output_tokens"]
        total_unavailable_reason = None
    else:
        total_value = None
        total_kind = "unavailable"
        total_components = []
        total_unavailable_reason = (
            "CLI log exposed neither a provider total nor both non-overlapping "
            "input_tokens and output_tokens"
        )

    any_usage = (
        any(value is not None for value in component_values.values())
        or total_value is not None
    )
    unavailable_reasons = {
        field: "CLI log did not expose this token component"
        for field, value in component_values.items() if value is None
    }
    return {
        "obtainable": any_usage,
        **component_values,
        "total_tokens": total_value,
        "source": "cli_log" if any_usage else "unavailable",
        "unavailable_reasons": unavailable_reasons,
        "total_tokens_kind": total_kind,
        "total_tokens_components": total_components,
        "total_tokens_unavailable_reason": total_unavailable_reason,
    }


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
    metadata_21_root = _metadata_21_root(args.workspace)

    def relative(path: str | None) -> str | None:
        if not path:
            return None
        try:
            return str(pathlib.Path(path).resolve().relative_to(workspace))
        except Exception:
            return str(path)

    canonical_usage = canonicalize_codex_usage(usage, usage_extras)
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

    # A generic CLI ``model`` field commonly echoes the requested alias.  Metadata 2.1 only
    # calls a model "served" when the adapter supplies an explicit served/model ID field; this
    # prevents gpt-5.6-terra from being relabelled as an observed deployment identity.
    explicit_served_ids: list[str] = []
    def collect_served_ids(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"served_model", "served_model_id", "model_id"} \
                        and isinstance(item, str) and 0 < len(item) < 100:
                    explicit_served_ids.append(item)
                collect_served_ids(item)
        elif isinstance(value, list):
            for item in value:
                collect_served_ids(item)
    for event in parsed_events:
        collect_served_ids(event)
    explicit_served_ids = list(dict.fromkeys(explicit_served_ids))
    if metadata_21_root is not None:
        observed_model = explicit_served_ids[-1] if explicit_served_ids else None
        model_source = "observed" if observed_model else ("declared" if args.model else "unavailable")
    else:
        # Preserve the v2.0 historical projection contract. New audits are always initialized as
        # 2.1 and therefore take the honest requested-only branch above.
        observed_model = models[-1] if models else (args.model if completed else None)
        model_source = "observed" if models else ("declared" if completed else "unavailable")
    error_text = None
    if not completed:
        # The provider's own error event outranks stderr. On a teammate's host codex-cli 0.156.1
        # printed "failed to load models cache: missing field ..." to stderr before the real 400
        # ("The gpt-6-sol model is not supported when using Codex with a ChatGPT account"), which
        # arrived only as a JSON error event -- and the ledger recorded the cache warning as the
        # reason the call failed. Known-benign CLI warnings are dropped from the stderr fallback.
        provider_message = ""
        for event in parsed_events:
            if isinstance(event, dict):
                if event.get("type") == "error" and isinstance(event.get("message"), str):
                    provider_message = event["message"]
                elif event.get("type") == "turn.failed" and isinstance((event.get("error") or {}).get("message"), str):
                    provider_message = event["error"]["message"]
        stderr_tail = ""
        if args.stderr and pathlib.Path(args.stderr).exists():
            lines = [line for line in pathlib.Path(args.stderr).read_text(errors="replace").splitlines()
                     if line.strip() and "failed to load models cache" not in line]
            stderr_tail = " | ".join(lines[-5:])
        detail = " ".join(provider_message.split())[:500] if provider_message else stderr_tail
        error_text = (args.failure_class + (": " + detail if detail else "")) or "unknown failure"
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
        **({"observed_model_ids": (
            explicit_served_ids if metadata_21_root is not None else list(dict.fromkeys(models))
        )} if (explicit_served_ids if metadata_21_root is not None else models) else {}),
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
    if metadata_21_root is not None:
        import lifecycle_journal
        events = lifecycle_journal.load_events(metadata_21_root)
        states = lifecycle_journal._reservation_states(events)
        state = states.get(args.call_id)
        if args.provider_call and state is None:
            raise ValueError("metadata 2.1 provider call lacks an explicit lifecycle reservation")
        provider_request_status = (
            state.get("provider_request_status", "unknown") if state is not None
            else "not_requested"
        )
        inference_start_status = (
            state.get("inference_start_status", "not_started") if state is not None
            else "not_started"
        )
        canonical_row["attempt"] = state.get("attempt") if state is not None else None
        canonical_row.update({
            "attempt_kind": args.attempt_kind,
            "provider_request_status": provider_request_status,
            "inference_start_status": inference_start_status,
            "source_invocation_id": args.source_invocation_id or None,
            "turn_count": len(terminal) if terminal else (0 if args.provider_call else None),
            "provider_cost": {
                "amount": None, "currency": None, "source": "unavailable",
                "unavailable_reason": "Codex CLI event stream did not expose provider cost",
            },
            "served_model": observed_model,
            "identity_assurance": (
                "observed" if observed_model else "requested_only" if args.model else "unavailable"
            ),
            "model_identity_evidence": {
                "source": "cli_log" if parsed_events else "adapter",
                "raw_ids": explicit_served_ids or list(dict.fromkeys(models)),
                "artifact": relative(args.events) if args.events and pathlib.Path(args.events).is_file() else None,
                "artifact_sha256": (
                    sha_file(args.events) if args.events and pathlib.Path(args.events).is_file() else None
                ),
            },
            "usage_by_model": ([{
                "model": explicit_served_ids[0],
                "identity_assurance": "observed",
                "usage": copy.deepcopy(canonical_usage),
            }] if len(explicit_served_ids) == 1 else []),
            "unattributed_usage": (
                None if len(explicit_served_ids) == 1 else copy.deepcopy(canonical_usage)
            ),
            "provider_duration_seconds": args.elapsed if args.provider_call else 0,
        })
        if not args.provider_call and not canonical_row["source_invocation_id"]:
            # A cache replay must point at the billable canonical call that produced the bytes.
            # This deliberately invalidates historical cache entries that lack provenance.
            raise ValueError("metadata 2.1 cache replay lacks source_invocation_id")
    record_invocation.validate_record(canonical_row, workspace)
    if metadata_21_root is not None and not args.provider_call:
        record_invocation.record_cache_reuse(workspace, canonical_row)
    else:
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
    ledger = pathlib.Path(args.ledger)
    metadata_root = _metadata_21_root(str(ledger.parent))
    canonical_role = (
        f"critic_{args.budget_role}" if args.budget_role in ROLES
        else "critic_synth" if args.budget_role == "targeted_adjudicator"
        else args.budget_role
    )
    if metadata_root is not None:
        import lifecycle_journal
        states = lifecycle_journal._reservation_states(lifecycle_journal.load_events(metadata_root))
        journalled = sum(
            state.get("role") == canonical_role
            and state.get("round") == args.round
            and state.get("attempt_kind") in lifecycle_journal.BUDGETED_ATTEMPT_KINDS
            and state.get("inference_start_status") == "started"
            for state in states.values()
        )
        # The journal only counts calls the provider ACCEPTED (inference_start_status started).
        # claim_attempt writes the counter before every dispatch, so it also counts the calls
        # that were cut before acceptance -- and those cost real time and real money. Reading
        # only the journal made this branch return 0 for two whole classes of dispatch:
        #
        #   * the model probe, which dispatches with attempt_kind "capability_probe" and is
        #     deliberately NOT in BUDGETED_ATTEMPT_KINDS, so its own guard ("model probe
        #     exhausted its round-wide budget") could never fire;
        #   * any role call killed before the provider stream proved acceptance -- a wall-clock
        #     timeout, or a transport failure on connect. Classified transient, as they should
        #     be, they consumed no budget, so the retry loop re-dispatched the same attempt
        #     number forever. The wall-clock ceiling turned that from unreachable into routine.
        #
        # max() can only ever raise the count, so a resumed or re-input round still cannot reset
        # a budget it has already spent.
        journalled = max(journalled, integer(counter))
        print(journalled)
        return 0
    used = integer(counter)
    legacy_root = pathlib.Path(args.legacy_root)
    if legacy_root.is_dir():
        for legacy in legacy_root.glob(f"*/{args.budget_role}.count"):
            used = max(used, integer(legacy))
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


def command_classify_provider_failure(args: argparse.Namespace) -> int:
    workspace = pathlib.Path(args.workspace).resolve()
    envelope = validate_policy_snapshot(pathlib.Path(args.policy), workspace)
    critique = (envelope.get("policy") or {}).get("critique") or {}
    retryable = critique.get("retryable_error_classes")
    if retryable != list(RETRYABLE_PROVIDER_ERROR_CLASSES):
        raise ValueError(
            "policy retryable_error_classes must be exactly transport, throttling, provider_5xx"
        )
    required_nonretryable = {
        "authentication", "authorization", "quota", "unsupported_model", "capability",
        "deterministic_schema",
    }
    if not required_nonretryable.issubset(set(critique.get("non_retryable_error_classes") or [])):
        raise ValueError("policy non_retryable_error_classes omits a required typed stop class")
    log = pathlib.Path(args.log)
    text = log.read_text(errors="replace") if log.is_file() else ""
    failure_class, error_class = provider_failure_classification(text)
    print(f"{failure_class} {error_class}")
    return 0


def command_seal_request_dir(args: argparse.Namespace) -> int:
    workspace = pathlib.Path(args.workspace).resolve()
    directory = within(workspace, pathlib.Path(args.directory), "provider request directory")
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("provider request directory must be a regular directory")
    manifest_path = directory / "REQUEST_MANIFEST.json"
    if manifest_path.exists():
        raise ValueError("provider request directory is already sealed")
    artifacts = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"provider request directory contains a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"provider request directory contains a special file: {path}")
        artifacts.append({
            "path": path.relative_to(directory).as_posix(),
            "sha256": sha_file(path),
            "bytes": path.stat().st_size,
        })
    atomic_json(manifest_path, {
        "manifest_version": "i4r-provider-request-1",
        "request_id": args.request_id,
        "invocation_id": args.invocation_id,
        "attempt_kind": args.attempt_kind,
        "artifacts": artifacts,
    })
    for path in sorted(directory.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    directory.chmod(0o555)
    return 0


def command_retry_backoff(args: argparse.Namespace) -> int:
    root = _metadata_21_root(args.workspace)
    if root is None:
        print(json.dumps({"metadata_version": "legacy"}))
        return 0
    if args.round < 1 or args.attempt < 1 or args.delay_seconds < 0:
        raise ValueError("retry backoff round/attempt must be positive and delay nonnegative")
    # Provider-retryable classes (throttling, transport, 5xx), the schema re-sample, and our own
    # wall-clock timeout. A schema-invalid response is ordinary sampling failure, so the round
    # re-samples within MAX_TRIES instead of discarding the role; the error class stays "schema"
    # in telemetry. "timeout" here is the driver killing a call that was still running -- NOT a
    # provider reporting a timeout of its own, which provider_failure_classification keeps
    # permanent because a provider that times out usually times out again. Without this the
    # ceiling was worse than the unbounded wait it replaced: the call was cut at attempt 1 and
    # then the round died, with the whole retry budget unspent.
    if (args.error_class not in RETRYABLE_PROVIDER_ERROR_CLASSES
            and args.error_class not in {"schema", "timeout"}):
        raise ValueError("retry backoff requires a policy-retryable typed error class or a schema re-sample")
    import lifecycle_journal
    events = lifecycle_journal.load_events(root)
    matching = [
        event for event in events
        if event["type"] in {"retry_backoff_start", "retry_backoff_end"}
        and (event.get("payload") or {}).get("retry_id") == args.retry_id
    ]
    starts = [event for event in matching if event["type"] == "retry_backoff_start"]
    ends = [event for event in matching if event["type"] == "retry_backoff_end"]
    if args.phase == "start":
        if starts or ends:
            raise ValueError("retry backoff identifier is already journaled")
        event_type = "retry_backoff_start"
    else:
        if len(starts) != 1 or ends:
            raise ValueError("retry backoff end lacks exactly one unmatched start")
        event_type = "retry_backoff_end"
    event = lifecycle_journal.append_event(
        root,
        event_type,
        {
            "retry_id": args.retry_id,
            "round": args.round,
            "role": args.role,
            "attempt": args.attempt,
            "error_class": args.error_class,
            "delay_seconds": args.delay_seconds,
        },
        at=args.at,
        source="run_critique.sh",
    )
    print(json.dumps({"metadata_version": "2.1", "event": event}, sort_keys=True))
    return 0


def _metadata_21_root(path: str) -> pathlib.Path | None:
    root = pathlib.Path(path).resolve()
    run_path = root / "run.json"
    if not run_path.is_file():
        return None
    run = load(run_path)
    return root if run.get("schema_version") == "2.1.0" else None


def command_round_authorization(args: argparse.Namespace) -> int:
    root = _metadata_21_root(args.workspace)
    if root is None:
        atomic_json(args.output, {"metadata_version": "legacy", "state": "not_applicable"})
        return 0
    import lifecycle_journal
    policy_sha = sha_file(args.policy)
    plan = {
        "purpose": f"I4R adversarial critique round {args.round}",
        "provider": "openai",
        "model_route": (
            f"critics={args.model}/{args.effort};roles={args.roles};"
            f"targeted_adjudicator={args.adjudicator_model}/{args.adjudicator_effort}"
        ),
        "data_classes": [
            "report", "canonical_findings", "deterministic_evidence", "package_metadata",
        ],
        "policy_sha256": policy_sha,
        "input_bindings": {
            "snapshot_sha256": args.input_sha,
            "report_sha256": args.report_sha,
            "routing_sha256": sha_file(args.routing),
            "diff_manifest_sha256": sha_file(args.diff_manifest) if args.diff_manifest else None,
        },
    }
    events = lifecycle_journal.load_events(root)
    prepared = [
        event for event in events
        if event["type"] == "round_prepared"
        and (event.get("payload") or {}).get("round") == args.round
    ]
    if not prepared:
        result = lifecycle_journal.prepare_round(root, args.round, plan)
        normalized_plan = result["plan"]
        events = lifecycle_journal.load_events(root)
    else:
        # The latest prepared plan is live; superseded ones stay in the chain.
        normalized_plan = (prepared[-1].get("payload") or {}).get("plan")
        expected = copy.deepcopy(plan)
        expected["round_plan_sha256"] = lifecycle_journal.sha256_value(plan)
        if normalized_plan != expected:
            if not getattr(args, "supersede_plan", False):
                prepared_plan = normalized_plan if isinstance(normalized_plan, dict) else {}
                differing = []
                for key in sorted(set(prepared_plan) | set(expected)):
                    old_value, new_value = prepared_plan.get(key), expected.get(key)
                    if key == "round_plan_sha256" or old_value == new_value:
                        continue
                    if isinstance(old_value, dict) and isinstance(new_value, dict):
                        # name the nested binding (routing vs report vs registry), not just "input_bindings"
                        differing += [f"{key}.{sub}" for sub in sorted(set(old_value) | set(new_value))
                                      if old_value.get(sub) != new_value.get(sub)]
                    else:
                        differing.append(key)
                raise ValueError(
                    "prepared critique round plan differs from this run's plan in "
                    + (", ".join(differing) or "its hash")
                    + " (a CRITICS/ADJUDICATE override, or a report, registry or packet change since it was "
                    "prepared); if that change is intended, prepare it again with I4R_SUPERSEDE_PLAN=1 added to the "
                    "same run_critique.sh command (the new plan then needs its own authorization and post-delta gates); "
                    "otherwise rerun with the settings the round was prepared with"
                )
            result = lifecycle_journal.prepare_round(root, args.round, plan, supersede=True)
            normalized_plan = result["plan"]
            events = lifecycle_journal.load_events(root)
    authorization = lifecycle_journal._live_round_authorization(events, args.round)
    state = lifecycle_journal._round_state_from_events(events, args.round)
    if authorization is not None and state in {"ready", "running", "quorum_complete", "integrated"}:
        atomic_json(args.output, {
            "metadata_version": "2.1", "round": args.round, "state": state,
            "plan": normalized_plan, "authorization_sha256": authorization["authorization_sha256"],
        })
        return 0
    template = {
        key: copy.deepcopy(normalized_plan[key])
        for key in lifecycle_journal.AUTH_BINDING_FIELDS[:-1]
    }
    template["expires_at"] = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    template_path = pathlib.Path(args.authorization_template)
    atomic_json(template_path, template)
    if not any(
        event["type"] == "round_authorization_required"
        and (event.get("payload") or {}).get("round") == args.round
        and (event.get("payload") or {}).get("plan_sha256") == normalized_plan["round_plan_sha256"]
        for event in events
    ):
        lifecycle_journal.append_event(root, "round_authorization_required", {
            "round": args.round, "reason": "awaiting_authorization",
            "plan_sha256": normalized_plan["round_plan_sha256"],
            "authorization_template": template_path.resolve().relative_to(root).as_posix(),
        })
    atomic_json(args.output, {
        "metadata_version": "2.1", "round": args.round,
        "state": "awaiting_authorization", "plan": normalized_plan,
        "authorization_template": template_path.resolve().relative_to(root).as_posix(),
    })
    return 3


def command_round_transition(args: argparse.Namespace) -> int:
    root = _metadata_21_root(args.workspace)
    if root is None:
        print(json.dumps({"metadata_version": "legacy"}))
        return 0
    import lifecycle_journal
    events = lifecycle_journal.load_events(root)
    state = lifecycle_journal._round_state_from_events(events, args.round)
    if args.transition == "quorum_complete":
        if state in {"quorum_complete", "integrated"}:
            print(json.dumps({"round": args.round, "state": state, "idempotent": True}))
            return 0
        if state not in {"ready", "running"}:
            raise ValueError(f"round {args.round} cannot reach quorum from {state!r}")
        event = lifecycle_journal.append_event(
            root, "round_quorum_complete",
            {"round": args.round, "critics_run": [f"critic_{role}" for role in args.roles.split()]}, at=args.at,
            source="run_critique.sh",
        )
        print(json.dumps({"round": args.round, "state": "quorum_complete", "event": event}, sort_keys=True))
        return 0
    if state == "integrated":
        print(json.dumps({"round": args.round, "state": state, "idempotent": True}))
        return 0
    if state != "quorum_complete":
        raise ValueError(f"round {args.round} cannot integrate from {state!r}")
    payload = {"round": args.round, "integrated_at": args.at}
    if args.report_sha256:
        if not re.fullmatch(r"[0-9a-f]{64}", args.report_sha256):
            raise ValueError("--report-sha256 must be a SHA-256 hex digest")
        # the exact draft this round's critics reviewed: the report its snapshot froze (the plan's
        # input_bindings.report_sha256); a path such as report/REPORT_final.md names a different
        # revision in every round
        payload["report_version_reviewed"] = args.report_sha256
    event = lifecycle_journal.append_event(
        root, "round_integrated", payload, at=args.at, source="run_critique.sh",
    )
    print(json.dumps({"round": args.round, "state": "integrated", "event": event}, sort_keys=True))
    return 0


def command_lifecycle_before_provider(args: argparse.Namespace) -> int:
    root = _metadata_21_root(args.workspace)
    if root is None:
        print(json.dumps({"metadata_version": "legacy"}))
        return 0
    import lifecycle_journal
    reservation = {
        "invocation_id": args.invocation_id,
        "role": args.role,
        "role_class": args.role_class,
        "round": args.round,
        "attempt_kind": args.attempt_kind,
        "provider": "openai",
        "requested_model": args.model,
        "reasoning_effort": args.effort,
        "input_artifact_sha256": args.input_sha,
        "prompt_sha256": sha_file(args.prompt),
    }
    if args.schema_digest:
        reservation["schema_digest"] = args.schema_digest
    reserved = lifecycle_journal.reserve_invocation(root, reservation)
    requested = lifecycle_journal.mark_provider_request(
        root, args.invocation_id, status="requested", at=args.at,
    )
    print(json.dumps({"metadata_version": "2.1", **reserved, **requested}, sort_keys=True))
    return 0


def command_lifecycle_after_provider(args: argparse.Namespace) -> int:
    root = _metadata_21_root(args.workspace)
    if root is None:
        print(json.dumps({"metadata_version": "legacy"}))
        return 0
    import lifecycle_journal
    parsed = []
    events_path = pathlib.Path(args.events)
    if events_path.is_file():
        for line in events_path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                parsed.append(row)
    inference_started = any(row.get("type") == "thread.started" for row in parsed)
    if inference_started:
        transition = lifecycle_journal.mark_inference_start(
            root, args.invocation_id, at=args.started_at,
        )
        response = pathlib.Path(args.response)
        if response.is_file() and response.stat().st_size:
            lifecycle_journal.mark_response_received(
                root, args.invocation_id,
                artifact=response.resolve().relative_to(root).as_posix(),
                sha256=sha_file(response), at=args.ended_at,
            )
    else:
        failure_text = ""
        for path_value in (args.stderr, args.events):
            path = pathlib.Path(path_value)
            if path.is_file():
                failure_text += path.read_text(errors="replace")
        rejected = bool(re.search(
            r"invalid[_ ]json[_ ]schema|output schema|unauthorized|authentication|forbidden|"
            r"quota|unsupported model|model is not supported|invalid[^\n]*effort",
            failure_text, re.I,
        ))
        transition = lifecycle_journal.mark_provider_request(
            root, args.invocation_id, status="rejected" if rejected else "unknown",
            at=args.ended_at,
        )
    print(json.dumps({
        "metadata_version": "2.1", "inference_started": inference_started, **transition,
    }, sort_keys=True))
    return 0


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
    failed = [] if args.outcome in {"running", "paused"} else [r for r in roles if r not in completed]
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
    scope: set[str] | None = None,
    bookkeeping: set[str] | None = None,
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
        # A MERGED or EXCLUDED row is not a live claim: its evidence obligation belongs to the row it
        # was merged into (or it is a tested negative), and critics are never routed to it. On the
        # seventh production audit two conclusion-narrowing rows merged into F1 raised this trigger
        # in round 3 -- a round the critics were correctly not asked to cover them in.
        disposition = (canonical.get("ledger_disposition") or {}).get("status")
        # A later round re-reviews only the changed claims (its DIFF_MANIFEST scope). An unchanged
        # high-impact finding the critics were not routed to is not an evidence gap of that round;
        # flagging it made every scoped round end deferred_evidence_gaps on the same finding.
        # The completeness critic's global pass may mention an unchanged finding; a CONFIRMED mention
        # is not a routed review either (a disputed one is caught by the conflict triggers above).
        # 2.7.4: under the specialist claim-change rule a bookkeeping-only finding (its registry row changed only by
        # binding confirmations or settlement) is reviewed by completeness alone, so it is held to the same rule.
        out_of_scope = (
            (scope is not None and canonical["id"] not in scope)
            or (bookkeeping is not None and canonical["id"] in bookkeeping)
        ) and all(assessment["verdict"] == "CONFIRMED" for _, assessment in values)
        if canonical["impact_rank"] >= 3 and disposition in (None, "INCLUDED") and not out_of_scope:
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
                    "operation": evidence["operation"],
                    "result": evidence["result"],
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
            "canonical_evidence": ({
                "source_hashes": [
                    {"path": "source/evidence/" + binding["path"],
                     "sha256": binding["sha256"]}
                    for binding in canonical["evidence"]["source_hashes"]
                ],
                "rerun_command": canonical["evidence"]["rerun_command"],
                "expected": canonical["evidence"]["expected"],
                "observed": canonical["evidence"]["observed"],
                "tolerance_basis": canonical["evidence"]["tolerance_basis"],
            } if canonical is not None else None),
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
        claim_merge = merge_by_id.get(key)
        role_results = claim_merge.get("role_results", []) if claim_merge else []
        evidence_complete_roles = {
            result["role"] for result in role_results
            if (result.get("evidence_bindings", {}).get("artifact_refs")
                and result["evidence_bindings"].get("operation")
                and result["evidence_bindings"].get("result"))
        }
        canonical_evidence = claim_merge.get("canonical_evidence") if claim_merge else None
        defer_reasons = []
        if claim_merge is None:
            defer_reasons.append("canonical finding or merged critic record is absent")
        if not canonical_evidence or not canonical_evidence.get("source_hashes"):
            defer_reasons.append("canonical finding lacks bound source evidence")
        if len(evidence_complete_roles) < 2:
            defer_reasons.append("fewer than two evidence-complete critic records")
        trigger_kinds = [trigger["kind"] for trigger in values]
        question = (
            f"For canonical finding {display[key]}, decide only the conflicts or disputed risk "
            f"identified by {', '.join(trigger_kinds)} using the bound critic and source evidence."
        )
        out.append({
            "row_id": display[key],
            "claim_id": display[key],
            "trigger_ids": [trigger["trigger_id"] for trigger in values],
            "trigger_kinds": trigger_kinds,
            "roles": roles,
            "details": [trigger["detail"] for trigger in values],
            "decision_question": question,
            "adjudication_ready": not defer_reasons,
            "defer_reasons": defer_reasons,
            "claim_merge": claim_merge,
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

    scope = None
    bookkeeping = None
    manifest_path = snapshot / "source/context/DIFF_MANIFEST.json"
    if args.round > 1 and manifest_path.is_file():
        manifest_obj = load(manifest_path)
        scope = {str(item) for item in (manifest_obj.get("changed_claim_ids") or [])}
        if "claim_change_rule" in manifest_obj:  # only a new audit's packet carries it (2.7.4)
            bookkeeping = {str(item) for item in (manifest_obj.get("bookkeeping_only_claim_ids") or [])}
    triggers = adjudication_triggers(rows, registry_by_id, scope, bookkeeping)
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
    all_unresolved_rows = unresolved_rows_for_packet(deduped, merge)
    unresolved_rows = [row for row in all_unresolved_rows if row["adjudication_ready"]]
    deferred_rows = [row for row in all_unresolved_rows if not row["adjudication_ready"]]
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
            "packet_version": "i4r-targeted-adjudication-packet-3",
            "audit_id": args.audit_id,
            "round": args.round,
            "input_sha256": args.input_sha,
            "report_sha256": args.report_sha,
            "policy": args.mode,
            "triggers": deduped,
            "claim_merge_sha256": sha_file(merge_path),
            "unresolved_rows": unresolved_rows,
            "deferred_rows": deferred_rows,
            "critic_outputs": critic_outputs,
        },
    )
    print(len(unresolved_rows))
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


def _verify_snapshot_for_dispatch(args: argparse.Namespace, snapshot: pathlib.Path,
                                  workspace: pathlib.Path) -> list[str]:
    expected = args.expected_input_sha
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise SnapshotIntegrityError(
            "immutable snapshot is missing or is not a real directory: "
            f"{snapshot_where(snapshot)}\n" + snapshot_delete_remedy(snapshot))
    snapshot = snapshot.resolve()
    if snapshot.parent.name != ("snapshots" if args.kind == "round" else "target_snapshots"):
        raise ValueError(f"not a {args.kind} snapshot location: {snapshot}")
    if snapshot.name != expected:
        raise SnapshotIntegrityError(
            f"snapshot directory is not the expected content address: {snapshot_where(snapshot)}")
    manifest = load_snapshot_manifest(snapshot)
    field = "input_sha256" if args.kind == "round" else "target_input_sha256"
    if manifest.get(field) != expected:
        raise SnapshotIntegrityError(
            f"corrupt snapshot manifest: {snapshot_where(snapshot)}\n  MANIFEST.json {field} is "
            "not the directory's content address.\n" + snapshot_delete_remedy(snapshot))
    try:
        if args.kind == "round":
            identity = round_identity_parts(manifest)
        else:
            identity = target_identity_parts(manifest, sha_file(args.runtime_script))
    except (KeyError, TypeError) as exc:
        raise SnapshotIntegrityError(
            f"corrupt snapshot manifest: {snapshot_where(snapshot)}\n  MANIFEST.json lacks an "
            f"identity field ({exc}).\n" + snapshot_delete_remedy(snapshot)) from exc
    if hash_tree(snapshot / "source", identity) != expected:
        raise SnapshotIntegrityError(
            "snapshot source no longer matches its content address: "
            f"{snapshot_where(snapshot)}\n" + snapshot_delete_remedy(snapshot))
    return ensure_frozen(snapshot, manifest, audit_root=workspace,
                         context=f"pre_dispatch:{args.context}",
                         refreeze=True, invocation=args.invocation)


def command_verify_snapshot(args: argparse.Namespace) -> int:
    """Prove a frozen snapshot intact immediately before a paid dispatch.

    The driver runs this before every claim and reservation (probe, critic attempt, focused
    repair, targeted adjudicator), so a snapshot that fails it costs no attempt, no reservation, no
    telemetry row and no call. Mode-only drift is re-frozen in place and journaled once, whatever
    I4R_SNAPSHOT_REFREEZE says: this command exists only for the driver. Exit 0 when clean or
    re-frozen, 5 when the content, inventory, manifest or identity differs (nothing is chmod'ed),
    1 on any other error.
    """
    workspace = pathlib.Path(args.workspace).resolve()
    if not re.fullmatch(r"[0-9a-f]{64}", args.expected_input_sha):
        raise ValueError("--expected-input-sha must be a lowercase SHA-256 digest")
    if args.kind == "target" and not args.runtime_script:
        raise ValueError("--runtime-script is required for a target snapshot")
    snapshot = pathlib.Path(args.snapshot)
    try:
        refrozen = _verify_snapshot_for_dispatch(args, snapshot, workspace)
    except SnapshotIntegrityError as exc:
        root = _metadata_21_root(str(workspace))
        if root is not None:
            try:
                snapshot_rel = snapshot.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                snapshot_rel = snapshot.name
            unstable = isinstance(exc, SnapshotModeUnstableError)
            try:
                import lifecycle_journal
                lifecycle_journal.append_event(root, "note", {
                    "text": (
                        f"The frozen {args.kind} snapshot's content verified, but its modes kept "
                        "changing after repeated re-freezes, so no provider call was made for this "
                        "dispatch." if unstable else
                        f"The frozen {args.kind} snapshot failed its pre-dispatch integrity "
                        "check, so no provider call was made for this dispatch."),
                    "kind": SNAPSHOT_MODE_UNSTABLE if unstable else "snapshot_integrity_refused",
                    "round": args.round,
                    "snapshot": snapshot_rel,
                    "snapshot_kind": args.kind,
                    "context": f"pre_dispatch:{args.context}",
                    "invocation": args.invocation,
                }, source="run_critique.sh")
            except Exception as exc:  # the refusal stands whether or not it could be journaled
                print(f"  [warn ] could not journal the snapshot refusal note: {exc}",
                      file=sys.stderr)
        raise
    print(json.dumps({
        "status": "refrozen" if refrozen else "clean",
        "refrozen_paths": len(refrozen),
        "snapshot_kind": args.kind,
    }, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    subs = root.add_subparsers(dest="command", required=True)

    p = subs.add_parser("route")
    p.add_argument("--workspace", required=True)
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

    p = subs.add_parser("prepare-repair")
    for key in (
        "input", "diagnostics", "original_prompt", "snapshot", "schema", "output", "receipt",
        "role", "input_sha", "report_sha", "audit_id",
    ):
        p.add_argument("--" + key.replace("_", "-"), required=True)
    p.add_argument("--round", type=int, required=True)
    p.set_defaults(func=command_prepare_repair)

    p = subs.add_parser("validate-repair")
    for key in (
        "before", "input", "role", "input_sha", "report_sha", "audit_id", "schema",
        "markdown", "receipt",
    ):
        p.add_argument("--" + key.replace("_", "-"), required=True)
    p.add_argument("--round", type=int, required=True)
    p.set_defaults(func=command_validate_repair)

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
    p.add_argument(
        "--attempt-kind",
        choices=("local_preflight", "capability_probe", "schema_admission", "role_inference", "output_repair", "cache_replay"),
        required=True,
    )
    p.add_argument("--source-invocation-id", default="")
    p.add_argument("--outcome", required=True)
    p.add_argument("--failure-class", default="")
    p.add_argument("--error-class", default="")
    p.add_argument(
        "--cache-usage", choices=("none", "read", "write", "read_write", "probe_cache", "not_applicable"),
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

    p = subs.add_parser("classify-provider-failure")
    for key in ("workspace", "policy", "log"):
        p.add_argument("--" + key, required=True)
    p.set_defaults(func=command_classify_provider_failure)

    p = subs.add_parser("seal-request-dir")
    for key in ("workspace", "directory", "request_id", "invocation_id"):
        p.add_argument("--" + key.replace("_", "-"), required=True)
    p.add_argument(
        "--attempt-kind", choices=("capability_probe", "schema_admission"), required=True,
    )
    p.set_defaults(func=command_seal_request_dir)

    p = subs.add_parser("retry-backoff")
    p.add_argument("--workspace", required=True)
    p.add_argument("--phase", choices=("start", "end"), required=True)
    p.add_argument("--retry-id", required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--attempt", type=int, required=True)
    # "timeout" here is OUR wall-clock ceiling cutting a call that was still running, not the
    # provider reporting a timeout of its own -- provider_failure_classification deliberately
    # keeps that wording permanent, because a provider that times out on its own usually times
    # out again. A call we killed mid-flight has earned a retry, and does back off before it.
    p.add_argument("--error-class",
                   choices=(*RETRYABLE_PROVIDER_ERROR_CLASSES, "schema", "timeout"),
                   required=True)
    p.add_argument("--delay-seconds", type=int, required=True)
    p.add_argument("--at", required=True)
    p.set_defaults(func=command_retry_backoff)

    p = subs.add_parser("round-authorization")
    for key in (
        "workspace", "policy", "routing", "input_sha", "report_sha", "roles", "model",
        "effort", "adjudicator_model", "adjudicator_effort", "output",
        "authorization_template",
    ):
        p.add_argument("--" + key.replace("_", "-"), required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--diff-manifest", default="")
    p.add_argument("--supersede-plan", action="store_true",
                   help="if the prepared plan differs from the current input and the round is "
                        "unfinished, journal a superseding plan instead of refusing")
    p.set_defaults(func=command_round_authorization)

    p = subs.add_parser("lifecycle-before-provider")
    for key in (
        "workspace", "invocation_id", "role", "role_class", "attempt_kind", "model",
        "effort", "input_sha", "prompt", "at",
    ):
        p.add_argument("--" + key.replace("_", "-"), required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--schema-digest", default="")
    p.set_defaults(func=command_lifecycle_before_provider)

    p = subs.add_parser("lifecycle-after-provider")
    for key in (
        "workspace", "invocation_id", "events", "response", "stderr", "started_at", "ended_at",
    ):
        p.add_argument("--" + key.replace("_", "-"), required=True)
    p.set_defaults(func=command_lifecycle_after_provider)

    p = subs.add_parser("round-transition")
    p.add_argument("--workspace", required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--transition", choices=("quorum_complete", "integrated"), required=True)
    p.add_argument("--roles", required=True)
    p.add_argument("--at", required=True)
    p.add_argument("--report-sha256", default="")
    p.set_defaults(func=command_round_transition)

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
    # quorum_complete_adjudication_failed is a distinct terminal state: the critic quorum was met,
    # journaled and projected, and only the targeted adjudication failed. It must not collapse to
    # "failed", or the status hides that the round's expensive reviews are already on disk.
    p.add_argument("--outcome", required=True, choices=(
        "running", "paused", "complete", "failed", "quorum_complete_adjudication_failed"))
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

    p = subs.add_parser("verify-snapshot")
    p.add_argument("--workspace", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--kind", choices=("round", "target"), required=True)
    p.add_argument("--expected-input-sha", required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--context", required=True)
    p.add_argument("--invocation", required=True)
    p.add_argument("--runtime-script", default="")
    p.set_defaults(func=command_verify_snapshot)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except SnapshotIntegrityError as exc:
        # Distinct from 1 (any other failure, including a schema-invalid response), 2 (argparse)
        # and 3 (round authorization): the driver must never re-sample, repair or mark a role
        # permanent over a snapshot that changed under it.
        print(f"runtime environment error: {exc}", file=sys.stderr)
        return SNAPSHOT_INTEGRITY_EXIT
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"runtime error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
