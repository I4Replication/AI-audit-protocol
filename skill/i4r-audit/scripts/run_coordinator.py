#!/usr/bin/env python3
"""Run the content-bound Phase-1 coordinator and promote its four outputs fail-closed.

The provider sees an immutable snapshot, not the live audit.  The snapshot contains the five
canonical Phase-1 reports, the draft registry, explicitly privacy-reviewed evidence, the audit
policy, and only the references needed for adjudication and report drafting.  A completed call is
accepted only when its served model, usage, input hashes, and all required outputs validate.
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
import signal
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable


HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
FINDING_SCHEMA = SKILL / "schemas/v2/finding-row.schema.json"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SKILL / "schemas/tools"))
import claude_routes
import phase1_runtime
import provider_safety
import record_invocation
import lifecycle_journal
import validate_json_schema
import validate_v2

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    import lock_wait  # noqa: E402
except ImportError:  # a standalone copy (e.g. a release's scripts/) without its sibling helper
    class lock_wait:  # noqa: N801
        @staticmethod
        def acquire_exclusive(handle, label):
            import fcntl as _fcntl
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)


PHASE_ONE_REPORTS = {
    "auditor_reproduction": "report/01_reproduction.md",
    "auditor_data_integrity": "report/02_data_integrity.md",
    "auditor_text_vs_data": "report/03_text_vs_data.md",
    "auditor_code_methods": "report/04_code_methods.md",
    "auditor_privacy_archive": "report/05_privacy_archive.md",
}
REQUIRED_REFERENCES = (
    "reference/checklist.md",
    "reference/adjudication_guide.md",
    "reference/SCHEME.md",
    "reference/report_structure.md",
    "reference/house_citations.md",
    "schemas/v2/finding-row.schema.json",
)
REQUIRED_AUDIT_CONTEXT = (
    "run.json",
    "PAPER.txt",
    "package_manifest.json",
    "pipeline-policy.snapshot.json",
)
OUTPUT_TARGETS = {
    "REPORT_v1.md": "report/REPORT_v1.md",
    "ADJUDICATION.md": "report/ADJUDICATION.md",
    "findings.jsonl": "work/findings.jsonl",
    "retired_claims.txt": "report/retired_claims.txt",
}
WORKING_REPORT_TARGET = "report/REPORT_final.md"
SAFE_TEXT_SUFFIXES = {".json", ".md", ".txt"}
MACHINE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
# First alternative: an account name containing spaces, admitted only when a path separator
# follows it. Without it `/Users/<Anna Smith>/...` redacted to `<private-path> Smith/...` and
# the surname survived into run.json -- and, with the `/Users/` prefix now gone, straight
# past the release gate too, so the leak was one-way. The separator lookahead keeps ordinary
# prose out: `/Users/<alice> is missing` has no following separator and falls through to the
# space-free alternative, stopping at `alice`.
LOCAL_PATH = re.compile(
    r"(?:/" + "Users/" + r"|/home/|[A-Za-z]:[\\/]Users[\\/])"
    r"[\w.\-]+(?:[ ][\w.\-]+){1,5}(?=[/\\])"
    r"|(?:/" + "Users/" + r"[^/\s]+|/home/[^/\s]+|/private/(?:tmp|var)/[^\s]+|"
    r"[A-Za-z]:[\\/]Users[\\/][^\\/\s]+)", re.IGNORECASE
)
PARTICIPANT_CODE = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{1,3}_?P\d+(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])P\d{1,3}(?![A-Za-z0-9])"
)
DATE_OR_CLOCK = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|"
    r"\d{1,2}\.\d{1,2}\.\d{4}|\d{2}:\d{2}:\d{2}"
)
# One definition, shared with Phase-1 output admission (scripts/provider_safety.py, 2.6.5).
RAW_PARTICIPANT_CODE = provider_safety.RAW_PARTICIPANT_CODE
ROW_PARTICIPANT_CODE = provider_safety.ROW_PARTICIPANT_CODE
PLACEHOLDER = re.compile(
    r"(?im)^\s*(?:todo|tbd|placeholder|\[?insert\b[^\n]*|<[^>]*(?:insert|todo|tbd)[^>]*>)\s*$"
)


class CoordinatorError(ValueError):
    """Expected fail-closed coordinator error."""


class ProviderLaunchError(CoordinatorError):
    """The provider process was not created, so no provider call was confirmed."""


@dataclass(frozen=True)
class InputSpec:
    kind: str
    origin: str
    snapshot: str
    source: pathlib.Path | None = None
    content: bytes | None = None
    # A deterministic projection of the live source (2.6.8: the masked package manifest). The
    # snapshot holds derive(source bytes), and the live check recomputes it from the live file.
    derive: Callable[[bytes], bytes] | None = None

    def read(self) -> bytes:
        if self.content is not None:
            return self.content
        if self.source is None:
            raise CoordinatorError(f"input {self.origin} has no source")
        data = self.source.read_bytes()
        return self.derive(data) if self.derive is not None else data


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def wall_elapsed_seconds(started: dt.datetime, ended: dt.datetime) -> float:
    """Return the wall-clock duration represented by the persisted timestamps.

    ``time.monotonic()`` is intentionally not used here.  On macOS its clock can pause while the
    machine sleeps, whereas the invocation schema defines elapsed time by ``started_at`` and
    ``ended_at``.  Deriving the value from those timestamps keeps telemetry internally consistent
    across sleep/wake and wall-clock suspension events.  The provider envelope remains the source
    for any provider-reported active/API duration.
    """
    elapsed = (ended - started).total_seconds()
    if elapsed < 0:
        raise CoordinatorError("coordinator ended_at precedes started_at")
    return round(elapsed, 3)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def pretty_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: pathlib.Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def strict_object(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = validate_json_schema.strict_loads(
            path.read_text(encoding="utf-8"), source=str(path)
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise CoordinatorError(f"invalid JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CoordinatorError(f"expected a JSON object: {path}")
    return value


def audit_relative(root: pathlib.Path, path: pathlib.Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise CoordinatorError(f"path escapes the audit directory: {path}") from exc


def require_regular(path: pathlib.Path, label: str) -> pathlib.Path:
    if path.is_symlink() or not path.is_file():
        raise CoordinatorError(f"{label} must be a regular non-symlink file: {path}")
    return path


def validate_provider_visible_text(
    path: pathlib.Path, label: str, exempt: Iterable[str] = frozenset()
) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise CoordinatorError(f"{label} is not UTF-8 text: {path}") from exc
    return validate_provider_visible_string(text, label, exempt)


def deposit_exemptions(root: pathlib.Path) -> frozenset[str]:
    """Code-shaped variable names of this audit's deposit (provider_safety explains the rule)."""
    return provider_safety.deposit_exemptions(root / "pkg")


def validate_provider_visible_string(
    text: str, label: str, exempt: Iterable[str] = frozenset()
) -> str:
    """The provider-safety scan on text already in memory (frozen packet content).

    A code-shaped token is refused unless ``exempt`` names it: ``deposit_exemptions(root)`` admits
    only column and variable names of the deposit's own data files that never occur as a data
    value, which is the same rule Phase-1 output admission applies (provider_safety.py).
    """
    match = LOCAL_PATH.search(text)
    if match:
        raise CoordinatorError(
            f"{label} contains a private absolute path and is not provider-safe: {match.group(0)!r}"
        )
    hit = provider_safety.first_participant_code(text, exempt)
    if hit:
        raise CoordinatorError(
            f"{label} contains a concrete participant-code pattern (line {hit.line}) and is not "
            "provider-safe; a code-shaped token passes only when it is a column or variable name "
            "in one of the deposit's data files and never a data value"
        )
    return text


def validate_output_privacy(text: str, label: str) -> None:
    # A code-shaped token beside a date or clock is refused here with no deposit exemption, exactly
    # as check_consistency.sh refuses it on the same REPORT_final.md at every post-delta gate. The
    # 2.6.5 exemption covers the bare-token scan (validate_provider_visible_string) only.
    match = LOCAL_PATH.search(text)
    if match:
        raise CoordinatorError(f"{label} leaks a private absolute path: {match.group(0)!r}")
    for line_number, line in enumerate(text.splitlines(), 1):
        if PARTICIPANT_CODE.search(line) and DATE_OR_CLOCK.search(line):
            raise CoordinatorError(
                f"{label} line {line_number} links a participant code to a date or clock time"
            )


# --- the coordinator's copy of package_manifest.json (2.6.8) -------------------------------------
# package_manifest.json is provider-visible audit context, and the participant-code rule admits a
# code-shaped token only when it is a column name of the deposit. A deposit whose FILE NAMES are
# code-shaped (one file per participant, named after the code) therefore made every coordinator run
# refuse before any provider call, and recover_coordinator refused the same bytes: the only
# script-level stop an identifier in the deposit could trigger. The coordinator is now shown a
# masked projection instead. Each code-shaped path segment becomes a stable placeholder, every entry
# keeps its size and sha256, and the directories holding masked names are summarised as
# "directory, N files" -- the same path-free binding build_safe_evidence records (paths_emitted is
# false). The rule itself (provider_safety.RULE_STATEMENT) is unchanged: a placeholder is not a
# deposit path, so a code-named file still cannot be cited. A manifest that already passes the scan
# is returned byte for byte, so every existing packet, input hash and recovery is unchanged.
PACKAGE_MANIFEST = "package_manifest.json"
MASKING_NOTE = (
    "Path segments shaped like participant codes are replaced by stable placeholders in this copy "
    "of the manifest; each entry keeps its size and sha256. A placeholder is not a deposit path "
    "and cannot be cited as evidence: bind such evidence to the script that reads the files and "
    "describe them by directory and file count."
)
_PLACEHOLDER_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,10}")


def _code_shaped(segment: str, exempt: frozenset[str]) -> bool:
    return provider_safety.first_participant_code(segment, exempt) is not None


def package_manifest_projection(raw: bytes, exempt: Iterable[str] = frozenset()) -> bytes:
    """The bytes of package_manifest.json the coordinator is shown (see the block comment above).

    Returns ``raw`` itself unless the manifest fails the provider-safety scan. Bytes that cannot be
    projected (not UTF-8, not JSON, no ``files`` array) are returned unchanged, so the caller's scan
    refuses them exactly as before.
    """
    allowed = frozenset(exempt)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    if provider_safety.first_participant_code(text, allowed) is None:
        return raw
    try:
        manifest = json.loads(text)
    except ValueError:
        return raw
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list):
        return raw
    placeholders: dict[tuple[str, ...], str] = {}
    counters = {"dir": 0, "file": 0}
    totals: dict[str, int] = {}
    masked_below: dict[str, int] = {}
    summarised: set[str] = set()
    projected: list[Any] = []
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str):
            projected.append(entry)
            continue
        parts = path.split("/")
        shown: list[str] = []
        masked = False
        for depth, segment in enumerate(parts):
            key = tuple(parts[: depth + 1])
            if key not in placeholders and _code_shaped(segment, allowed):
                kind = "file" if depth == len(parts) - 1 else "dir"
                counters[kind] += 1
                suffix = pathlib.PurePosixPath(segment).suffix if kind == "file" else ""
                if not _PLACEHOLDER_SUFFIX.fullmatch(suffix) or _code_shaped(suffix, allowed):
                    suffix = ""
                placeholders[key] = f"masked-{kind}-{counters[kind]:03d}{suffix}"
            if key in placeholders:
                shown.append(placeholders[key])
                masked = True
                if depth < len(parts) - 1:
                    summarised.add("/".join(shown))
            else:
                shown.append(segment)
        parent = "/".join(shown[:-1]) or "."
        # Every directory above the file, "." included: "directory, N files" counts recursively.
        ancestors = {"."} | {"/".join(shown[:depth]) for depth in range(1, len(shown))}
        for directory in ancestors:
            totals[directory] = totals.get(directory, 0) + 1
            if masked:
                masked_below[directory] = masked_below.get(directory, 0) + 1
        if masked:
            if placeholders.get(tuple(parts)):
                summarised.add(parent)
            row = dict(entry)
            row["path"] = "/".join(shown)
            row["path_masked"] = True
            projected.append(row)
        else:
            projected.append(entry)
    result = dict(manifest)
    result["files"] = projected
    result["coordinator_masking"] = {
        "note": MASKING_NOTE,
        "masked_file_entries": sum(1 for row in projected if isinstance(row, dict) and row.get("path_masked")),
        "directories": [
            {
                "directory": directory,
                "description": "directory, {0} file{1}".format(
                    totals.get(directory, 0), "" if totals.get(directory, 0) == 1 else "s"),
                "files": totals.get(directory, 0),
                "files_with_masked_names": masked_below.get(directory, 0),
            }
            for directory in sorted(summarised)
        ],
    }
    return (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def package_manifest_spec(root: pathlib.Path) -> InputSpec:
    """The context/package_manifest.json snapshot input: the file itself, or its projection."""
    path = require_regular(root / PACKAGE_MANIFEST, f"required audit context {PACKAGE_MANIFEST}")
    exempt = deposit_exemptions(root)
    raw = path.read_bytes()
    projected = package_manifest_projection(raw, exempt)
    try:
        validate_provider_visible_string(projected.decode("utf-8"), f"context/{PACKAGE_MANIFEST}", exempt)
    except UnicodeDecodeError as exc:
        raise CoordinatorError(f"context/{PACKAGE_MANIFEST} is not UTF-8 text: {path}") from exc
    if projected == raw:
        return InputSpec("audit_context", PACKAGE_MANIFEST, f"context/{PACKAGE_MANIFEST}", path)
    return InputSpec(
        "audit_context", PACKAGE_MANIFEST, f"context/{PACKAGE_MANIFEST}", path,
        derive=lambda data: package_manifest_projection(data, exempt),
    )


def live_context_bytes(root: pathlib.Path, origin: str, path: pathlib.Path) -> bytes:
    """What the snapshot holds for a live audit-context file (recovery tools compare these)."""
    data = path.read_bytes()
    if origin == PACKAGE_MANIFEST:
        return package_manifest_projection(data, deposit_exemptions(root))
    return data


def read_ledger(root: pathlib.Path) -> list[dict[str, Any]]:
    path = require_regular(root / "invocations.jsonl", "canonical invocation ledger")
    lines = [
        (number, line)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if line.strip()
    ]
    # Roles promote to a fixed canonical path, so a later completed attempt overwrites
    # an earlier one's bytes. Only the last completed row for each artifact describes the
    # file on disk; verifying a superseded row's hash would reject a correct ledger.
    current_row_for_artifact: dict[str, int] = {}
    for number, line in lines:
        try:
            peek = json.loads(line)
        except ValueError:
            continue
        if isinstance(peek, dict) and peek.get("status") == "completed":
            artifact = peek.get("output_artifact")
            if isinstance(artifact, str):
                current_row_for_artifact[artifact] = number
    rows: list[dict[str, Any]] = []
    for number, line in lines:
        try:
            row = validate_json_schema.strict_loads(
                line, source=f"{path} line {number}"
            )
            if not isinstance(row, dict):
                raise ValueError("record is not an object")
            superseded = (
                row.get("status") == "completed"
                and current_row_for_artifact.get(row.get("output_artifact")) != number
            )
            record_invocation.validate_record(
                row, root, verify_output_bytes=not superseded,
            )
        except (ValueError, OSError) as exc:
            raise CoordinatorError(f"invalid invocation ledger line {number}: {exc}") from exc
        rows.append(row)
    record_invocation.validate_attempt_sequences(rows)
    return rows


def sonnet5(model_id: str) -> bool:
    """The pre-2.7.0 served-model check, kept for importers: claude_routes.is_sonnet_5_legacy."""
    return claude_routes.is_sonnet_5_legacy(model_id)


def phase_one_bindings(
    root: pathlib.Path, rows: list[dict[str, Any]], policy: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    bindings: list[dict[str, Any]] = []
    report_hashes: dict[str, str] = {}
    configured = policy["models"]["phase_1_auditors"]
    if list(PHASE_ONE_REPORTS) != policy["phase_1"]["required_roles"]:
        raise CoordinatorError("runner Phase-1 roles differ from the audit-scoped policy quorum")
    for role, relative in PHASE_ONE_REPORTS.items():
        report = require_regular(root / relative, f"canonical Phase-1 report for {role}")
        text = validate_provider_visible_text(report, f"Phase-1 report {role}", deposit_exemptions(root))
        if len(text.strip()) < 1000 or PLACEHOLDER.search(text):
            raise CoordinatorError(f"canonical Phase-1 report is incomplete: {relative}")
        digest = sha256_file(report)
        matches = [
            row for row in rows
            if row.get("provider_call") is True
            and row.get("status") == "completed"
            and row.get("role") == role
            and row.get("role_class") == "auditor"
            and row.get("round") in (None, 0)
            and row.get("output_artifact") == relative
            and row.get("output_sha256") == digest
        ]
        if len(matches) != 1:
            raise CoordinatorError(
                f"{role} requires exactly one completed invocation bound to {relative}; "
                f"observed {len(matches)}"
            )
        row = matches[0]
        expected = configured[role]
        # The served model is judged on the auditor's own route: a Sonnet 5 row by the unchanged
        # pre-2.7.0 check, a Sonnet 5.5 row exactly (claude_routes.serves).
        if (
            row.get("requested_model") != expected["requested_model"]
            or row.get("provider") != expected["provider"]
            or row.get("reasoning_effort") != expected["reasoning_effort"]
            or row.get("model_family") != "claude"
            or not claude_routes.serves(expected["requested_model"], str(row.get("observed_model") or ""))
        ):
            raise CoordinatorError(f"{role} completed under the wrong model policy")
        report_hashes[relative] = digest
        bindings.append({
            "role": role,
            "invocation_id": row["invocation_id"],
            "report": relative,
            "report_sha256": digest,
            "requested_model": row["requested_model"],
            "observed_model": row["observed_model"],
            "observed_model_ids": row.get("observed_model_ids") or [row["observed_model"]],
            "reasoning_effort": row["reasoning_effort"],
        })
    return bindings, report_hashes


def load_draft_rows(paths: Iterable[pathlib.Path]) -> tuple[list[dict[str, Any]], set[str]]:
    rows: list[dict[str, Any]] = []
    ids: set[str] = set()
    for path in paths:
        if path.suffix.lower() != ".jsonl":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = validate_json_schema.strict_loads(
                    line, source=f"{path} line {number}"
                )
                errors = validate_json_schema.errors_for_value(row, FINDING_SCHEMA)
                if errors:
                    raise ValueError("; ".join(errors[:10]))
                validate_v2.validate_finding_row(row, f"draft[{path.name}:{number}]")
            except (ValueError, validate_v2.ValidationError) as exc:
                raise CoordinatorError(f"invalid draft finding {path}:{number}: {exc}") from exc
            finding_id = row["id"]
            if finding_id in ids:
                raise CoordinatorError(f"duplicate draft finding id: {finding_id}")
            ids.add(finding_id)
            rows.append(row)
    if not rows:
        raise CoordinatorError("at least one draft-registry JSONL row is required")
    return rows, ids


def resolve_draft_paths(root: pathlib.Path, supplied: list[pathlib.Path]) -> list[pathlib.Path]:
    if supplied:
        candidates = supplied
    else:
        directory = root / "work/registry_draft"
        candidates = sorted(
            path for path in directory.glob("*")
            if path.suffix.lower() in {".jsonl", ".md"}
        )
    if not candidates:
        raise CoordinatorError(
            "no draft registry supplied; use --draft-registry or create work/registry_draft"
        )
    resolved: list[pathlib.Path] = []
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else root / candidate
        require_regular(path, "draft registry input")
        audit_relative(root, path)
        validate_provider_visible_text(path, "draft registry input", deposit_exemptions(root))
        resolved.append(path.resolve())
    if len(resolved) != len(set(resolved)):
        raise CoordinatorError("duplicate --draft-registry path")
    return resolved


def load_safe_evidence(root: pathlib.Path, manifest_path: pathlib.Path) -> list[pathlib.Path]:
    manifest_path = manifest_path if manifest_path.is_absolute() else root / manifest_path
    require_regular(manifest_path, "safe-evidence manifest")
    audit_relative(root, manifest_path)
    safe_root = (root / "work/safe_evidence").resolve()
    if manifest_path.resolve() != safe_root / "manifest.json":
        raise CoordinatorError(
            "safe-evidence manifest must be work/safe_evidence/manifest.json"
        )
    # The manifest is copied into the provider-visible snapshot too.  Its paths are
    # constrained below, but free-text fields (for example checksum_closure) must
    # pass the same privacy boundary as the payloads.
    validate_provider_visible_text(manifest_path, "safe-evidence manifest", deposit_exemptions(root))
    manifest = strict_object(manifest_path)
    if manifest.get("schema_version") != "i4r-safe-evidence-1":
        raise CoordinatorError(
            "safe-evidence manifest requires schema_version=i4r-safe-evidence-1"
        )
    if set(manifest) != {"schema_version", "artifact_count", "artifacts", "checksum_closure"}:
        raise CoordinatorError("safe-evidence manifest has unsupported top-level fields")
    files = manifest.get("artifacts")
    if (
        not isinstance(files, list) or not files
        or manifest.get("artifact_count") != len(files)
        or not isinstance(manifest.get("checksum_closure"), str)
    ):
        raise CoordinatorError("safe-evidence manifest must list at least one file")
    resolved: list[pathlib.Path] = []
    seen: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
            raise CoordinatorError(f"safe-evidence artifacts[{index}] has an invalid shape")
        relative = item.get("path")
        digest = item.get("sha256")
        if not isinstance(relative, str) or not relative or pathlib.PurePosixPath(relative).is_absolute():
            raise CoordinatorError(f"safe-evidence artifacts[{index}].path must be relative")
        if ".." in pathlib.PurePosixPath(relative.replace("\\", "/")).parts:
            raise CoordinatorError(f"safe-evidence artifacts[{index}].path traverses upward")
        if relative in seen:
            raise CoordinatorError(f"duplicate safe-evidence path: {relative}")
        seen.add(relative)
        path = require_regular(safe_root / relative, "safe evidence")
        try:
            path.resolve().relative_to(safe_root)
        except ValueError as exc:
            raise CoordinatorError(
                f"safe evidence must stay under work/safe_evidence: {relative}"
            ) from exc
        if path.stat().st_size > 1024 * 1024:
            raise CoordinatorError(f"safe evidence exceeds the 1 MiB per-file limit: {relative}")
        actual = sha256_file(path)
        if not isinstance(digest, str) or digest != actual:
            raise CoordinatorError(f"safe-evidence hash mismatch: {relative}")
        if item["bytes"] != path.stat().st_size:
            raise CoordinatorError(f"safe-evidence byte count mismatch: {relative}")
        if path.suffix.lower() in SAFE_TEXT_SUFFIXES:
            validate_provider_visible_text(path, f"safe evidence {relative}", deposit_exemptions(root))
            resolved.append(path.resolve())
        elif path.suffix.lower() != ".py":
            raise CoordinatorError(
                f"safe-evidence construction artifact has an unsupported type: {relative}"
            )
    privacy = strict_object(require_regular(safe_root / "privacy_scan.json", "safe-evidence privacy receipt"))
    required_false = (
        "absolute_local_paths_emitted", "participant_identifiers_emitted",
        "row_values_or_signatures_emitted", "user_host_or_printer_values_emitted",
    )
    if (
        privacy.get("schema_version") != "i4r-safe-evidence-1"
        or privacy.get("passed") is not True
        or privacy.get("full_directory_rescan_passed") is not True
        or any(privacy.get(name) is not False for name in required_false)
    ):
        raise CoordinatorError("safe-evidence privacy receipt does not certify a clean full rescan")
    for field in ("generic_detector_match_counts", "source_derived_token_hit_counts"):
        counts = privacy.get(field)
        if not isinstance(counts, dict) or not counts or any(value != 0 for value in counts.values()):
            raise CoordinatorError(f"safe-evidence privacy receipt has nonzero {field}")
    self_tests = strict_object(require_regular(safe_root / "self_tests.json", "safe-evidence self-tests"))
    if (
        self_tests.get("schema_version") != "i4r-safe-evidence-1"
        or self_tests.get("passed") is not True
        or self_tests.get("tests_run") != self_tests.get("tests_passed")
        or not isinstance(self_tests.get("tests_run"), int)
        or self_tests["tests_run"] < 1
    ):
        raise CoordinatorError("safe-evidence detector self-tests are absent or failed")
    bindings = strict_object(require_regular(safe_root / "source_bindings.json", "safe-evidence source bindings"))
    if bindings.get("schema_version") != "i4r-safe-evidence-1" or bindings.get("paths_emitted") is not False:
        raise CoordinatorError("safe-evidence source bindings expose paths or have the wrong schema")
    checksums = require_regular(safe_root / "SHA256SUMS", "safe-evidence checksum closure")
    checksum_rows: dict[str, str] = {}
    for number, line in enumerate(checksums.read_text(encoding="utf-8").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
        if not match or match.group(2) in checksum_rows:
            raise CoordinatorError(f"invalid safe-evidence SHA256SUMS line {number}")
        checksum_rows[match.group(2)] = match.group(1)
    expected_checksum_names = seen | {"manifest.json"}
    if set(checksum_rows) != expected_checksum_names:
        raise CoordinatorError("safe-evidence checksum closure has missing or extra files")
    for name, expected in checksum_rows.items():
        if sha256_file(safe_root / name) != expected:
            raise CoordinatorError(f"safe-evidence checksum closure mismatch: {name}")
    return [manifest_path.resolve(), *resolved]


def specs_and_manifest(
    root: pathlib.Path,
    draft_paths: list[pathlib.Path],
    safe_paths: list[pathlib.Path],
    bindings: list[dict[str, Any]],
    extra_references: list[pathlib.Path],
    max_input_bytes: int,
    *,
    freeze_run_projection: bool = False,
) -> tuple[list[InputSpec], dict[str, Any], str]:
    specs: list[InputSpec] = []
    for role, relative in PHASE_ONE_REPORTS.items():
        specs.append(InputSpec("phase1_report", relative, f"reports/{pathlib.Path(relative).name}", root / relative))
    for path in draft_paths:
        relative = audit_relative(root, path)
        specs.append(InputSpec("draft_registry", relative, f"draft/{relative}", path))
    manifest_path, *evidence_paths = safe_paths
    manifest_relative = audit_relative(root, manifest_path)
    specs.append(InputSpec("safe_evidence_manifest", manifest_relative, "evidence/SAFE_EVIDENCE.json", manifest_path))
    for path in evidence_paths:
        relative = audit_relative(root, path)
        specs.append(InputSpec("safe_evidence", relative, f"evidence/{relative}", path))
    for relative in REQUIRED_AUDIT_CONTEXT:
        path = require_regular(root / relative, f"required audit context {relative}")
        # The same provider-safety scan every other snapshot input gets. These four were the
        # only class that skipped it, and PAPER.txt is operator-written free text that SKILL.md
        # asks to carry the "package location" -- the natural thing to write there is the
        # absolute path, account name included. recover_coordinator.validate_snapshot_tree and
        # recover_misplaced_coordinator_outputs both refuse the same bytes; the runner was the
        # one tool of three that let them reach the model.
        if relative == "run.json" and freeze_run_projection:
            # Metadata 2.1 lifecycle transitions intentionally update the live
            # projection after this packet is content-bound.  Carry the exact
            # pre-reservation bytes as immutable packet content so those
            # journal-derived updates are not mistaken for source staleness.
            content = path.read_bytes()
            try:
                validate_provider_visible_string(
                    content.decode("utf-8"), f"context/{relative}", deposit_exemptions(root)
                )
            except UnicodeDecodeError as exc:
                raise CoordinatorError(f"context/{relative} is not UTF-8 text: {path}") from exc
            except CoordinatorError as exc:
                # 2.7.6: the journal is append-only; logged text is masked in run.json by a later event
                raise CoordinatorError(
                    f"{exc}. If the text is one a log_event.sh call wrote (a note, failure detail, brief correction, "
                    "delivery recipient or notes field), mask it with: bash scripts/log_event.sh text_redaction "
                    "seq=<that event's seq in run.events.jsonl> field=<text|notes|note|detail|description|recipient> "
                    "reason='<why>'") from None
            specs.append(InputSpec(
                "audit_context", relative, f"context/{relative}", content=content,
            ))
        elif relative == PACKAGE_MANIFEST:
            # 2.6.8: code-shaped file names are masked (package_manifest_projection).
            specs.append(package_manifest_spec(root))
        else:
            validate_provider_visible_text(path, f"context/{relative}", deposit_exemptions(root))
            specs.append(InputSpec("audit_context", relative, f"context/{relative}", path))
    references = [SKILL / relative for relative in REQUIRED_REFERENCES]
    references.extend(extra_references)
    for path in references:
        require_regular(path, "coordinator reference")
        try:
            relative = path.resolve().relative_to(SKILL).as_posix()
        except ValueError as exc:
            raise CoordinatorError(f"extra reference must be inside the skill: {path}") from exc
        validate_provider_visible_text(path, f"coordinator reference {relative}")
        specs.append(InputSpec("reference", relative, f"references/{relative}", path.resolve()))
    binding_bytes = pretty_bytes({"schema_version": "1.0.0", "bindings": bindings})
    specs.append(InputSpec(
        "phase1_bindings", "generated:phase1_bindings.json", "context/phase1_bindings.json",
        content=binding_bytes,
    ))
    if len({spec.snapshot for spec in specs}) != len(specs):
        raise CoordinatorError("coordinator snapshot contains duplicate logical paths")
    entries = []
    total = 0
    for spec in sorted(specs, key=lambda item: item.snapshot):
        data = spec.read()
        total += len(data)
        entries.append({
            "kind": spec.kind,
            "origin": spec.origin,
            "snapshot": spec.snapshot,
            "sha256": sha256_bytes(data),
            "bytes": len(data),
        })
    if total > max_input_bytes:
        raise CoordinatorError(
            f"provider-visible coordinator snapshot is {total} bytes; limit is {max_input_bytes}"
        )
    descriptor = {"schema_version": "1.0.0", "files": entries}
    digest = sha256_bytes(canonical_bytes(descriptor))
    manifest = dict(descriptor, input_artifact_sha256=digest, total_bytes=total)
    return specs, manifest, digest


def materialize_snapshot(
    root: pathlib.Path, attempt: pathlib.Path, specs: list[InputSpec], manifest: dict[str, Any]
) -> pathlib.Path:
    source = attempt / "source"
    source.mkdir(parents=True, exist_ok=False)
    by_snapshot = {entry["snapshot"]: entry for entry in manifest["files"]}
    for spec in specs:
        destination = source / spec.snapshot
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = spec.read()
        expected = by_snapshot[spec.snapshot]
        if sha256_bytes(data) != expected["sha256"] or len(data) != expected["bytes"]:
            raise CoordinatorError(f"live input changed before snapshot: {spec.origin}")
        destination.write_bytes(data)
        os.chmod(destination, 0o444)
    (source / "input_manifest.json").write_bytes(pretty_bytes(manifest))
    os.chmod(source / "input_manifest.json", 0o444)
    for directory, subdirs, _files in os.walk(source, topdown=False):
        for name in subdirs:
            os.chmod(pathlib.Path(directory) / name, 0o555)
        os.chmod(directory, 0o555)
    verify_snapshot(source, manifest)
    return source


def verify_snapshot(source: pathlib.Path, manifest: dict[str, Any]) -> None:
    for entry in manifest["files"]:
        path = source / entry["snapshot"]
        require_regular(path, "immutable coordinator snapshot input")
        if sha256_file(path) != entry["sha256"] or path.stat().st_size != entry["bytes"]:
            raise CoordinatorError(f"immutable snapshot changed: {entry['snapshot']}")


def verify_live_inputs(specs: Iterable[InputSpec], manifest: dict[str, Any]) -> None:
    expected = {entry["snapshot"]: entry for entry in manifest["files"]}
    for spec in specs:
        if spec.source is None:
            continue
        require_regular(spec.source, f"live coordinator input {spec.origin}")
        entry = expected[spec.snapshot]
        if spec.derive is not None:
            # A projected input (the masked package manifest): recompute it from the live file.
            data = spec.read()
            if sha256_bytes(data) != entry["sha256"] or len(data) != entry["bytes"]:
                raise CoordinatorError(f"live coordinator input became stale: {spec.origin}")
            continue
        if sha256_file(spec.source) != entry["sha256"] or spec.source.stat().st_size != entry["bytes"]:
            raise CoordinatorError(f"live coordinator input became stale: {spec.origin}")


def guard_stranded_attempts(root: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    """Refuse a new call while an unledgered attempt might have reached the provider.

    A crash can occur after process creation but before a terminal invocation row is appended.
    Such an attempt cannot safely be ignored: doing so would reset both telemetry and the
    three-provider-call budget.  Recovery must first append or otherwise resolve it.
    """
    ledger_ids = {
        row.get("invocation_id") for row in rows
        if row.get("role") == "coordinator" and row.get("round") is None
    }
    base = root / "coordinator/invocations"
    if not base.is_dir():
        return
    for status_path in sorted(base.glob("*/*/status.json")):
        status = strict_object(require_regular(status_path, "coordinator attempt status"))
        invocation_id = status.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id:
            raise CoordinatorError(f"coordinator attempt status lacks invocation_id: {status_path}")
        if invocation_id in ledger_ids:
            continue
        state = status.get("state")
        provider_started = status.get("provider_call_started")
        if provider_started is True or state in {"launching", "running"}:
            relative = audit_relative(root, status_path.parent)
            raise CoordinatorError(
                f"unledgered coordinator attempt may have reached the provider: {relative}; "
                "recover it before retrying: if the attempt holds invocation_record.json (the "
                "provider responded and promotion failed), run scripts/recover_coordinator.py; "
                "if it does not (the runner died before any response), append a terminal failed "
                "record with scripts/record_invocation.py <audit-dir> <terminal-record.json> so "
                "the attempt is charged and the guard clears."
            )


def completed_for_input(
    rows: list[dict[str, Any]], input_hash: str,
    root: pathlib.Path | None = None, manifest: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    completed = [
        row for row in rows
        if row.get("provider_call") is True
        and row.get("role") == "coordinator"
        and row.get("round") is None
        and row.get("status") == "completed"
    ]
    if len(completed) > 1:
        raise CoordinatorError("multiple completed coordinator provider calls are recorded")
    if not completed:
        return None
    row = completed[0]
    if row.get("input_artifact_sha256") == input_hash:
        return row
    # Under metadata 2.1, context/run.json is a projection of the whole journal, so EVERY later
    # event -- including this coordinator call's own terminal row -- changes its bytes and with
    # them the packet hash. The idempotent reuse path was therefore unreachable on every audit
    # init_run.sh creates: a repeat invocation hard-failed with a message accusing the frozen
    # inputs of drifting when nothing but the journal's own record of the call had moved. Compare
    # the packets file by file; if run.json is the only difference, this IS the same call.
    drifted = _drifted_inputs(row, root, manifest) if root is not None and manifest is not None else None
    if drifted is not None and drifted <= {"context/run.json"}:
        return row
    detail = ""
    if drifted:
        detail = " (changed: " + ", ".join(sorted(drifted)) + ")"
    raise CoordinatorError(
        "a coordinator call already completed for different immutable inputs" + detail
    )


def _drifted_inputs(
    row: dict[str, Any], root: pathlib.Path, manifest: dict[str, Any],
) -> set[str] | None:
    """Snapshot paths whose bytes differ between the completed call's packet and this one.

    None when the completed attempt's manifest cannot be found, so the caller falls back to
    the strict verdict rather than guessing.
    """
    previous_hash = row.get("input_artifact_sha256")
    invocation_id = row.get("invocation_id")
    if not isinstance(previous_hash, str) or not isinstance(invocation_id, str):
        return None
    base = root / "coordinator/invocations" / previous_hash
    candidates = sorted(base.glob(f"attempt_*_{invocation_id}/source/input_manifest.json"))
    if len(candidates) != 1:
        return None
    try:
        previous = json.loads(candidates[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    before = {entry["snapshot"]: entry["sha256"] for entry in previous.get("files", [])
              if isinstance(entry, dict) and "snapshot" in entry}
    after = {entry["snapshot"]: entry["sha256"] for entry in manifest.get("files", [])
             if isinstance(entry, dict) and "snapshot" in entry}
    return {path for path in set(before) | set(after) if before.get(path) != after.get(path)}


def _wrote_files(directory: pathlib.Path) -> bool:
    """Whether the call left any regular file (not a symlink) in its output workspace (2.7.6)."""
    if not directory.is_dir():
        return False
    for base, _dirs, files in os.walk(directory, followlinks=False):
        for name in files:
            if not (pathlib.Path(base) / name).is_symlink():
                return True
    return False


def next_attempt(rows: list[dict[str, Any]], *, accepted_only: bool = False) -> int:
    attempts = [
        row["attempt"] for row in rows
        if row.get("provider_call") is True
        and row.get("role") == "coordinator"
        and row.get("round") is None
        and (not accepted_only or row.get("inference_start_status") == "started")
    ]
    limit = lifecycle_journal.max_attempts_per_role()
    if len(attempts) >= limit:
        raise CoordinatorError(
            f"{limit}-attempt coordinator provider budget is exhausted "
            "(raise with I4R_MAX_ATTEMPTS, or recover a quarantined attempt without a provider call)"
        )
    expected = len(attempts) + 1
    if attempts != list(range(1, expected)):
        raise CoordinatorError(f"coordinator attempts are not contiguous: {attempts}")
    return expected


def build_prompt(
    audit_id: str, invocation_id: str, input_hash: str, requested_model: str | None = None,
    effort: str = claude_routes.LEGACY_EFFORT,
) -> str:
    # 2.7.0: the model named is the snapshot coordinator route's. None and sonnet-5 render the
    # 2.6.10 bytes exactly, which recover_coordinator.py compares a quarantined prompt against.
    # 2.7.3: so is the effort (the route's reasoning_effort). The default and "max" render the 2.7.2
    # bytes exactly; a new audit's coordinator (xhigh) is told "at xhigh effort".
    model = claude_routes.display_name(requested_model)
    model_id = claude_routes.model_id(requested_model)
    return f"""You are the I4R coordinator. Use {model} at {effort} effort.

This invocation is content-bound: audit_id={audit_id}; invocation_id={invocation_id}; input_artifact_sha256={input_hash}. Read ../source/input_manifest.json and every file under ../source/reports before drafting. Use the draft registry, privacy-reviewed evidence, Phase-1 invocation bindings, policy snapshot, audit context, and the six supplied references. The snapshot is read-only and exhaustive. Do not access the live audit, the internet, authors, journals, repositories, raw participant data, or any path outside ../source and the current output directory. Do not rerun analyses. Never invent an evidence hash, invocation, reviewer, independent confirmation, or exact result. Cite every evidence path by its `origin` value in input_manifest.json, NOT by the `snapshot` path you read it from: `report/01_reproduction.md`, not `reports/01_reproduction.md`; `work/safe_evidence/privacy_scan.json`, not `evidence/work/safe_evidence/privacy_scan.json`. The registry is read later against the audit directory, where only the `origin` spelling exists. This is the pre-critique draft: every output row must have coordinator_approved=false, settled=null AND changes_conclusion=false. The validator accepts changes_conclusion=true only when the row's confirmations span two model families, and every Phase-1 confirmation is Sonnet, so a pre-critique row can never carry it -- however many auditors agree. Record a candidate conclusion-changing finding as such in ADJUDICATION.md only, never in the row's claim text: the claim is projected verbatim into Appendix A and the claim cross-reference and outlives this draft, so review-status language there contradicts the record once critics confirm it; round 1 supplies the second family. Every `verifications[]` entry is a join to canonical telemetry and is checked field by field: `reviewer_id` is the auditor role name; `invocation_id`, `evidence_record.path` and `evidence_record.sha256` are copied verbatim from that role's completed binding in the Phase-1 quorum (the `report` path and `report_sha256`); and `model_family` is exactly `claude` for every Phase-1 auditor -- the ledger's family string, never a model name such as `{model_id}` or `Sonnet`, which is rejected as disagreeing with telemetry. INCLUDED and MERGED candidates must remain status=draft; tested negatives may remain rejected or superseded. Critic review and later integration are the earliest point at which promotion can be considered.

Adjudicate every draft row and every distinct Phase-1 item. Mechanically merge genuine duplicates by stable claim ID. Recompute only from the supplied safe evidence. Preserve uncertainty and tested negatives. Do not infer intent. The public report must contain no audit-process narration, private absolute path, participant identifier, or row-level participant value. Use workspace-relative evidence paths only. The comment is about the paper, never about how the comment was written: do not mention drafts, revisions, rounds, critics, reviewers, or the audit's own process anywhere in REPORT_v1.md, and do not paraphrase the drafting rules from the references into the text. The consistency gate rejects phrases such as "earlier draft", "round 1", "our critics", "a critic", "we initially", "intermediate draft" and "we withdraw"; the hypotheses-not-confirmed section simply lists tests of the paper. Write privacy and archive-hygiene findings as data-stewardship observations for the authors and the repository: report counts of unique attribute combinations and the banding or collapsing that would remove them, and never describe, imply, or reason through a procedure for identifying a person from the file. Personal data in the deposit is reported, never withheld: every Phase-1 finding that the deposit holds identifying data, where the safe evidence supports it, stays INCLUDED (or MERGED into an included row) and is reported in REPORT_v1.md as a disclosure-flagged finding naming the identifier kind, the manifest-relative file and column (or the directory and a file count when file names are participant codes), and the rows or participants affected. Never EXCLUDE, defer or withhold such a finding for privacy reasons, and never reproduce an identifying value. A disclosure finding the safe evidence refutes is not a privacy matter but a wrong claim: EXCLUDE it as a tested negative whose reason opens with "Refuted:" or "Tested negative:", like any other refuted claim.

Your working directory is this attempt's private output directory; its parent holds the read-only snapshot, which is why the snapshot is addressed as ../source. Write the four required files into the working directory by plain filename only: REPORT_v1.md, not ./output/REPORT_v1.md and never ../REPORT_v1.md. The parent directory is read-only during your run and a file written anywhere else is not found by the validator and fails the attempt. The four files are:

1. REPORT_v1.md — a complete I4R paper-style draft. Its level-2 headings are checked literally, so use exactly these forms: `## Abstract` (at most 250 words, no table); numbered body sections beginning `## 1. Introduction` and ending with `## N. Conclusion`; `## Data and Code Availability`; `## References`; and `## Appendix A. Summary of findings`. There must be no Recommendations section. The References section must contain these two protocol citations exactly as written here, anchors included, and the Introduction must cite both once as the protocol this comment follows, in the form: This comment was produced under the Institute for Replication's AI-assisted reproducibility protocol ([Brodeur et al., 2026](#ref-AI-Games); [Institute for Replication, 2026](#ref-AI-vertical)).

[]{{#ref-AI-Games}}Brodeur, A., Valenta, D., Marcoci, A., Aparicio, J. P., Mikola, D., Barbarioli, B., Alexander, R., Deer, L., Stafford, T., Vilhuber, L., Bensch, G., et al. (2026). AI-assisted teams outperform AI-led teams but not human-only teams in assessing research reproducibility in quantitative social science. *Proceedings of the National Academy of Sciences*, 123(22), e2524747123. <https://doi.org/10.1073/pnas.2524747123> · <https://github.com/I4Replication/AI-Games>

[]{{#ref-AI-vertical}}Institute for Replication (2026). Reproducing with AI Across the Expertise Ladder. Pre-analysis plan, OSF: <https://osf.io/dkfzt/> · <https://github.com/I4Replication/AI-vertical>

A report whose References lack the literal strings AI-Games and AI-vertical is rejected. Body sections give What/Evidence/Impact/Remedy treatments and record hypotheses tested and not confirmed. Include exactly one invisible join marker for each registry row: <!-- finding:ID --> for INCLUDED, <!-- tested-negative:ID --> for EXCLUDED, or <!-- merged:ID->TARGET --> for MERGED.
2. ADJUDICATION.md — the complete internal log. For every registry row include exactly one <!-- adjudication:ID --> marker and record Sources, Question, Checks, Decision, Evidence, and Required propagation. Preserve withdrawn decisions.
3. findings.jsonl — one compact JSON object per line, no envelope, each conforming to schemas/v2/finding-row.schema.json. Retain every input draft ID, give every row a disposition, and cite only hashes present in the draft registry, safe-evidence manifest, or canonical Phase-1 report bindings. New Phase-1 findings are allowed when they cite those sources.
4. retired_claims.txt — exact retired prose fragments, one per line; # comments and an otherwise empty file are allowed. Never list a live claim.

Work plan. Every tool call re-reads your whole context, so cost scales with turns, not with care. Read the input manifest, the five reports and the draft registry first, in as few calls as possible. Then write findings.jsonl before anything else, because the other three files are derived from it, and write ADJUDICATION.md and REPORT_v1.md early and extend them as you go rather than composing each in a single final write: a file on disk is progress the operator can see, an empty output directory after twenty minutes is indistinguishable from a hung call. Aim to finish in well under 40 turns.

Do not write placeholders or partial skeletons. Validate the four files against each other before returning. Your final response should state only that the four files are complete; the files are authoritative.
"""


def sandbox_settings(root: pathlib.Path, attempt: pathlib.Path, source: pathlib.Path, output: pathlib.Path) -> dict[str, Any]:
    # The coordinator emits the same raw-path sandbox rules as Phase 1 and had no contract
    # validator at all, so a root the CLI cannot express as a glob would have disarmed the
    # coordinator's sandbox silently as well.
    phase1_runtime.assert_sandbox_expressible([root, attempt, source, output], root)
    protected = [root / name for name in ("paper", "pkg", "report", "work", "critique")]
    protected.extend(root / name for name in (
        "run.json", "run.events.jsonl", "invocations.jsonl", "package_manifest.json",
        "pipeline-policy.snapshot.json",
    ))
    sibling_attempts = [
        path for path in (root / "coordinator/invocations").glob("*/*")
        if path.resolve() != attempt.resolve()
    ]
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
            "network": {"allowedDomains": [], "deniedDomains": ["*"]},
            "filesystem": {
                "allowRead": [str(source), str(output)],
                "allowWrite": [str(output)],
                "denyRead": [str(path) for path in protected + sibling_attempts],
                "denyWrite": [str(path) for path in protected + sibling_attempts + [source]],
            },
        }
    }


def claude_command(
    claude: str,
    settings: pathlib.Path,
    source: pathlib.Path,
    session_id: str,
    model_alias: str,
    effort: str,
    max_budget_usd: str,
) -> list[str]:
    tools = "Read,Glob,Grep,Write,Edit"
    return [
        claude,
        "--print",
        "--output-format", "json",
        "--model", model_alias,
        "--effort", effort,
        "--safe-mode",
        "--no-chrome",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--settings", str(settings),
        "--add-dir", str(source),
        "--no-session-persistence",
        "--session-id", session_id,
        "--max-budget-usd", max_budget_usd,
        "--permission-mode", "dontAsk",
        "--tools", tools,
        "--allowedTools", tools,
    ]


def run_provider(
    command: list[str],
    cwd: pathlib.Path,
    prompt: str,
    timeout_seconds: int,
    on_started: Callable[[int], None] | None = None,
    *,
    route: dict[str, Any] | None = None,
) -> tuple[int, str, str, bool]:
    try:
        # 2.7.0: never the variables that remap what the alias `sonnet` serves (REMAPPING_ENV).
        process = subprocess.Popen(
            command,
            cwd=cwd,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=claude_routes.spawn_env(route=route),
        )
    except OSError as exc:
        raise ProviderLaunchError(f"Claude CLI could not be launched: {exc}") from exc
    if on_started is not None:
        try:
            on_started(process.pid)
        except BaseException:
            # A process exists at this point.  Stop it before propagating so a failed durable
            # start marker cannot leave an unobserved provider process consuming resources.
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            raise
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout_seconds)
        return process.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return 124, stdout or "", stderr or "", True


def parse_provider_result(stdout: str) -> dict[str, Any]:
    try:
        value = json.loads(stdout)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.splitlines()):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise CoordinatorError("Claude CLI did not emit a JSON result object")


def observed_models(
    result: dict[str, Any], requested_model: str | None = None,
) -> tuple[str | None, list[str], int]:
    """(primary, served ids, primary count) of one call, judged on the route's requested_model.

    None and sonnet-5 are the pre-2.7.0 check, unchanged. On the Sonnet 5.5 route a served Sonnet
    that is not 5.5 is counted with the primary, so a mixed call is refused as two primaries and a
    call served no Sonnet 5.5 counts zero (claude_routes.primaries); a helper such as Haiku is
    never counted. A caller accepts the call only on a count of exactly one.
    """
    model_usage = result.get("modelUsage")
    ids: list[str] = []
    if isinstance(model_usage, dict):
        ids.extend(str(key) for key in model_usage if isinstance(key, str) and key)
    if not ids:
        model = result.get("model") or result.get("model_id")
        if isinstance(model, str) and model:
            ids.append(model)
    ids = list(dict.fromkeys(ids))
    primaries, conflicts = claude_routes.primaries(requested_model, ids)
    count = (len(primaries) + len(conflicts)) if primaries else 0
    primary = primaries[0] if count == 1 else (ids[0] if ids else None)
    return primary, ids, count


def nonnegative(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def extract_usage(result: dict[str, Any]) -> tuple[dict[str, Any], str]:
    snake = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cached_input_tokens": "cache_read_input_tokens",
        "cache_creation_input_tokens": "cache_creation_input_tokens",
        "reasoning_tokens": "reasoning_tokens",
    }
    camel = {
        "input_tokens": "inputTokens",
        "output_tokens": "outputTokens",
        "cached_input_tokens": "cacheReadInputTokens",
        "cache_creation_input_tokens": "cacheCreationInputTokens",
        "reasoning_tokens": "reasoningTokens",
    }
    raw_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    per_model = result.get("modelUsage") if isinstance(result.get("modelUsage"), dict) else {}
    # Claude's top-level usage describes the primary Sonnet path only.  When modelUsage exists,
    # sum its mutually exclusive camelCase components across every served model (including
    # auxiliary Haiku) and never mix that total with the primary-only object.
    if per_model:
        counts: dict[str, int | None] = {}
        for name, source in camel.items():
            values = [
                nonnegative(item.get(source))
                for item in per_model.values() if isinstance(item, dict)
            ]
            counts[name] = (
                sum(values) if len(values) == len(per_model)
                and values and all(value is not None for value in values) else None
            )
    else:
        counts = {name: nonnegative(raw_usage.get(source)) for name, source in snake.items()}
    reasons = {
        name: "Claude CLI response did not expose this token component"
        for name, count in counts.items() if count is None
    }
    reported_total = None if per_model else nonnegative(raw_usage.get("total_tokens"))
    total_components = [
        "input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens"
    ]
    if reported_total is not None:
        total = reported_total
        total_kind = "provider_reported"
        total_components_used: list[str] = []
        total_reason = None
    elif all(counts[name] is not None for name in total_components):
        total = sum(int(counts[name]) for name in total_components)
        total_kind = "derived"
        total_components_used = total_components
        total_reason = None
    else:
        total = None
        total_kind = "unavailable"
        total_components_used = []
        total_reason = "Claude CLI did not expose enough disjoint components for an exact total"
    any_observed = any(value is not None for value in counts.values()) or total is not None
    usage = {
        "obtainable": any_observed,
        **counts,
        "total_tokens": total,
        "source": "cli_log" if any_observed else "unavailable",
        "unavailable_reasons": reasons,
        "total_tokens_kind": total_kind,
        "total_tokens_components": total_components_used,
        "total_tokens_unavailable_reason": total_reason,
    }
    read = counts["cached_input_tokens"] or 0
    write = counts["cache_creation_input_tokens"] or 0
    cache = "read_write" if read and write else "read" if read else "write" if write else "none"
    return usage, cache


def complete_usage(usage: dict[str, Any]) -> bool:
    return (
        usage.get("input_tokens") is not None
        and usage.get("output_tokens") is not None
        and usage.get("total_tokens") is not None
        and usage.get("total_tokens_kind") in {"provider_reported", "derived"}
    )


def usage_by_model(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return exact provider-exposed model partitions without inventing a split."""
    raw = result.get("modelUsage")
    if not isinstance(raw, dict):
        return []
    partitions = []
    for model, model_usage in raw.items():
        if not isinstance(model, str) or not model or not isinstance(model_usage, dict):
            continue
        usage, _cache = extract_usage({"modelUsage": {model: model_usage}})
        partitions.append({
            "model": model, "identity_assurance": "observed", "usage": usage,
        })
    return partitions


def result_turn_count(result: dict[str, Any]) -> int | None:
    for key in ("num_turns", "numTurns", "turn_count", "turnCount"):
        value = nonnegative(result.get(key))
        if value is not None:
            return value
    return None


def result_provider_duration_seconds(result: dict[str, Any]) -> float | None:
    for key in ("api_duration_seconds", "apiDurationSeconds"):
        value = result.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
    for key in ("duration_api_ms", "durationApiMs"):
        value = result.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value) / 1000.0
    return None


def result_provider_cost(result: dict[str, Any]) -> dict[str, Any]:
    amount = result.get("total_cost_usd", result.get("totalCostUsd"))
    if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount >= 0:
        return {
            "amount": float(amount), "currency": "USD", "source": "cli_log",
            "unavailable_reason": None,
        }
    return {
        "amount": None, "currency": "USD", "source": "unavailable",
        "unavailable_reason": "Claude CLI response did not expose provider cost",
    }


def inference_evidence_complete(
    served_model: str | None, usage: dict[str, Any], turn_count: int | None,
    provider_duration_seconds: float | None,
) -> bool:
    token_values = (
        usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens"),
    )
    return (
        isinstance(served_model, str) and bool(served_model)
        and any(isinstance(value, int) and not isinstance(value, bool) and value > 0
                for value in token_values)
        and isinstance(turn_count, int) and not isinstance(turn_count, bool) and turn_count > 0
        and isinstance(provider_duration_seconds, (int, float))
        and not isinstance(provider_duration_seconds, bool)
        and provider_duration_seconds > 0
    )


def redact_private_paths(message: str, *roots: pathlib.Path) -> str:
    """Replace absolute audit paths in a message with audit-relative form.

    Error strings are journaled and projected into run.json, which is later a provider-visible
    input and a release artefact. The first production audit's third coordinator attempt
    journaled ``... must be a regular non-symlink file: /Users/<account>/...`` and the live
    projection then failed the provider-safety check. Redact before journaling, never after.
    """
    text = message
    for root in roots:
        for form in (str(root), str(pathlib.Path(root).resolve())):
            if form and form in text:
                text = text.replace(form + "/", "").replace(form, "<audit>")
    return LOCAL_PATH.sub("<private-path>", text)


# Claude CLI 2.1.206: "API Error: Claude's response exceeded the 64000 output token maximum. To configure
# this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable." (the number is the request's
# max_tokens).
OUTPUT_LIMIT = re.compile(r"response exceeded the \d+ output token maximum")


def classify_error(message: str, *, timed_out: bool = False, schema: bool = False) -> str:
    lower = message.lower()
    if schema:
        return "schema"
    if timed_out:
        return "timeout"
    # A provider-side content-safety classifier refused the request. This is not a skill
    # gate and not retryable with the same content: the first live coordinator call was
    # refused this way at turn 33 after 36 minutes. Name it so the operator sees the cause
    # and the documented mitigation (stewardship framing of privacy findings) immediately.
    if any(term in lower for term in (
        "safety measures", "flagged this message", "cyber verification", "cyber safeguard",
    )):
        return "provider_safety_refusal"
    # 2.7.4: the CLI cut one response at its per-response output cap four times in a row (its own
    # three in-context resumes, then the failure). The provider accepted the call and ran inference;
    # the overflow is a property of that sample, not of the audit, so it has its own class and one
    # fresh attempt (OUTPUT_LIMIT_RETRIES).
    if OUTPUT_LIMIT.search(lower):
        return "output_limit"
    if any(term in lower for term in ("authentication", "unauthorized", "api key", "login")):
        return "authentication"
    if any(term in lower for term in ("permission", "authorization", "forbidden")):
        return "authorization"
    if any(term in lower for term in ("quota", "credit", "billing", "budget")):
        return "quota"
    if any(term in lower for term in ("model not found", "unsupported model", "invalid model")):
        return "model"
    if any(term in lower for term in ("capability", "unsupported capability", "tool unsupported")):
        return "capability"
    if any(term in lower for term in ("rate limit", "429", "overloaded", "502", "503", "504", "5xx")):
        return "transient"
    if "served sonnet" in lower or "sonnet 5 primary" in lower:
        return "model"
    if any(term in lower for term in ("network", "connection", "transport")):
        return "transport"
    return "permanent"


def known_evidence(
    draft_rows: list[dict[str, Any]],
    report_hashes: dict[str, str],
    root: pathlib.Path,
    safe_paths: list[pathlib.Path],
    input_manifest_files: list[dict[str, Any]] | None = None,
) -> tuple[set[tuple[str, str]], set[str], dict[str, str]]:
    pairs = set(report_hashes.items())
    paths = set(report_hashes)
    for row in draft_rows:
        for item in row["evidence"]["source_hashes"]:
            pairs.add((item["path"], item["sha256"]))
            paths.add(item["path"])
        paths.update(row["dependencies"]["artifact_paths"])
    for path in safe_paths[1:]:
        relative = audit_relative(root, path)
        pairs.add((relative, sha256_file(path)))
        paths.add(relative)
    # The deposit's own manifest. package_manifest.json is in REQUIRED_AUDIT_CONTEXT, so the
    # coordinator is GIVEN these hashes and told the snapshot is exhaustive -- and was then failed
    # for citing one, because the bound set was assembled only from what the five roles happened to
    # bind. A deposited file no role cited was uncitable, which is precisely backwards for a
    # tested negative: the coordinator's job there is to record that it looked at a file and found
    # nothing. This is not a loosening of "never invent an evidence hash". These hashes are
    # computed by init_run.sh from the frozen read-only pkg/ tree, so they are stronger provenance
    # than a role's assertion, and a citation still has to match one exactly.
    manifest_path = root / "package_manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CoordinatorError(f"package_manifest.json is not valid JSON: {exc}") from exc
        for entry in manifest.get("files", []):
            relative = entry.get("path")
            digest = entry.get("sha256")
            if not isinstance(relative, str) or not isinstance(digest, str):
                continue
            # Manifest paths are deposit-relative; every other binding is audit-relative.
            deposited = f"pkg/{relative}"
            pairs.add((deposited, digest))
            paths.add(deposited)
    # The model only ever sees the SNAPSHOT, whose spelling differs from the audit-relative keys
    # above: `report/01_reproduction.md` is mounted at `reports/01_reproduction.md`, and
    # `work/safe_evidence/privacy_scan.json` at `evidence/work/safe_evidence/...`. The runtime
    # prompt even says to "read every file under ../source/reports". A model citing what it was
    # handed was failed for "an unbound evidence hash" -- twice, on two prefixes, on one audit.
    #
    # The answer is NOT to admit the snapshot spelling into the bound set: findings.jsonl is a
    # durable artifact and every downstream tool resolves its paths against the audit root, so a
    # snapshot path stored there fails later and further away (the critique driver died on
    # "evidence path is missing or is not a regular file: reports/01_reproduction.md"). Return the
    # mapping instead, and let the caller rewrite citations to canonical form before they are
    # validated or stored. The bound set stays strictly audit-relative.
    canonical: dict[str, str] = {}
    for entry in input_manifest_files or []:
        snapshot_path, origin = entry.get("snapshot"), entry.get("origin")
        if snapshot_path and origin and origin in paths and snapshot_path != origin:
            canonical[snapshot_path] = origin
    return pairs, paths, canonical


def parse_findings(path: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = validate_json_schema.strict_loads(line, source=f"{path} line {number}")
            errors = validate_json_schema.errors_for_value(row, FINDING_SCHEMA)
            if errors:
                raise ValueError("; ".join(errors[:20]))
            validate_v2.validate_finding_row(row, f"findings[{number}]")
        except (ValueError, validate_v2.ValidationError) as exc:
            raise CoordinatorError(f"invalid findings.jsonl line {number}: {exc}") from exc
        finding_id = row["id"]
        if not MACHINE_ID.fullmatch(finding_id):
            raise CoordinatorError(f"finding ID is not machine-safe: {finding_id!r}")
        if finding_id in seen:
            raise CoordinatorError(f"duplicate finding ID: {finding_id}")
        seen.add(finding_id)
        rows.append(row)
    if not rows:
        raise CoordinatorError("findings.jsonl is empty")
    return rows


def normalized_claim(text: str) -> str:
    value = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    value = re.sub(r"\b(the|a|an|of|its|their|this|that)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def canonicalize_citations(
    rows: list[dict[str, Any]], canonical: dict[str, str],
) -> bool:
    """Rewrite snapshot-spelled evidence paths to their audit-relative form. Returns True if any
    row changed. The model cites the snapshot because that is all it can see; the registry has to
    hold paths that resolve against the audit root."""
    changed = False
    for row in rows:
        for item in row["evidence"]["source_hashes"]:
            replacement = canonical.get(item["path"])
            if replacement:
                item["path"] = replacement
                changed = True
        artifacts = row["dependencies"]["artifact_paths"]
        for index, artifact in enumerate(artifacts):
            replacement = canonical.get(artifact)
            if replacement:
                artifacts[index] = replacement
                changed = True
    return changed


def validate_outputs(
    output: pathlib.Path,
    draft_ids: set[str],
    known_pairs: set[tuple[str, str]],
    known_paths: set[str],
    invocation_rows: list[dict[str, Any]],
    canonical_paths: dict[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    paths: dict[str, pathlib.Path] = {}
    texts: dict[str, str] = {}
    for name in OUTPUT_TARGETS:
        path = output / name
        require_regular(path, f"required coordinator output {name}")
        paths[name] = path
        try:
            texts[name] = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise CoordinatorError(f"coordinator output is not UTF-8: {name}") from exc
        validate_output_privacy(texts[name], name)
    report = texts["REPORT_v1.md"]
    if len(report.strip()) < 1000 or PLACEHOLDER.search(report):
        raise CoordinatorError("REPORT_v1.md is a partial skeleton or contains placeholders")
    required_report_patterns = {
        "Abstract": r"(?m)^## Abstract\s*$",
        "numbered section": r"(?m)^## 1\.\s+",
        "Conclusion": r"(?im)^## \d+\.\s+Conclusion\s*$",
        "Data and Code Availability": r"(?im)^## Data and Code Availability\s*$",
        "References": r"(?im)^## References\s*$",
        "Appendix A": r"(?im)^## Appendix A\.\s+Summary of findings\s*$",
    }
    missing = [name for name, pattern in required_report_patterns.items() if not re.search(pattern, report)]
    if missing:
        raise CoordinatorError("REPORT_v1.md lacks required sections: " + ", ".join(missing))
    abstract_match = re.search(r"(?ms)^## Abstract\s*\n(.*?)(?=^## )", report)
    abstract = abstract_match.group(1) if abstract_match else ""
    if len(abstract.split()) > 250 or any(line.lstrip().startswith("|") for line in abstract.splitlines()):
        raise CoordinatorError("REPORT_v1.md Abstract exceeds 250 words or contains a table")
    if re.search(r"(?im)^## (?:\d+\.\s+)?Recommendations\s*$", report):
        raise CoordinatorError("REPORT_v1.md contains a forbidden Recommendations section")
    for citation in ("AI-Games", "AI-vertical"):
        if citation not in report:
            raise CoordinatorError(f"REPORT_v1.md lacks required citation marker {citation}")
    adjudication = texts["ADJUDICATION.md"]
    if len(adjudication.strip()) < 250 or PLACEHOLDER.search(adjudication):
        raise CoordinatorError("ADJUDICATION.md is a partial skeleton or contains placeholders")
    for label in ("Sources:", "Question:", "Checks:", "Decision:", "Evidence:", "Required propagation:"):
        if label not in adjudication:
            raise CoordinatorError(f"ADJUDICATION.md lacks required field {label}")
    findings = parse_findings(paths["findings.jsonl"])
    if canonicalize_citations(findings, canonical_paths or {}):
        # Rewritten before promotion, so the stored artifact carries audit-relative paths and the
        # recorded output hash is taken over what was actually stored.
        paths["findings.jsonl"].write_text(
            "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in findings),
            encoding="utf-8")
        texts["findings.jsonl"] = paths["findings.jsonl"].read_text(encoding="utf-8")
    final_ids = {row["id"] for row in findings}
    missing_drafts = sorted(draft_ids - final_ids)
    if missing_drafts:
        raise CoordinatorError("coordinator dropped draft registry IDs: " + ", ".join(missing_drafts))
    completed_invocations = {
        row["invocation_id"]: row for row in invocation_rows if row.get("status") == "completed"
    }
    for row in findings:
        finding_id = row["id"]
        disposition = row["ledger_disposition"]
        if row["coordinator_approved"] is not False:
            raise CoordinatorError(
                f"{finding_id} is prematurely coordinator-approved before critic review"
            )
        if row["settled"] is not None:
            raise CoordinatorError(f"{finding_id} is prematurely settled before critic review")
        if disposition["status"] in {"INCLUDED", "MERGED"} and row["status"] != "draft":
            raise CoordinatorError(
                f"{finding_id} must remain status=draft until critic review is integrated"
            )
        source_hashes = row["evidence"]["source_hashes"]
        if not source_hashes:
            raise CoordinatorError(f"{finding_id} has no evidence source hash")
        for item in source_hashes:
            pair = (item["path"], item["sha256"])
            if pair not in known_pairs:
                raise CoordinatorError(f"{finding_id} cites an unbound evidence hash: {pair[0]}")
        for artifact in row["dependencies"]["artifact_paths"]:
            if artifact not in known_paths:
                raise CoordinatorError(f"{finding_id} cites an unbound dependency: {artifact}")
        for verification in row["verifications"]:
            invocation = completed_invocations.get(verification["invocation_id"])
            if invocation is None:
                raise CoordinatorError(
                    f"{finding_id} verification cites a non-completed invocation: "
                    f"{verification['invocation_id']}"
                )
            evidence = verification["evidence_record"]
            if (
                invocation.get("output_artifact") != evidence["path"]
                or invocation.get("output_sha256") != evidence["sha256"]
                or invocation.get("model_family") != verification["model_family"]
            ):
                raise CoordinatorError(f"{finding_id} verification disagrees with canonical telemetry")
        if disposition["status"] == "INCLUDED":
            marker = f"<!-- finding:{finding_id} -->"
        elif disposition["status"] == "EXCLUDED":
            marker = f"<!-- tested-negative:{finding_id} -->"
        else:
            marker = f"<!-- merged:{finding_id}->{disposition['target']} -->"
        if report.count(marker) != 1:
            raise CoordinatorError(f"REPORT_v1.md must contain exactly one join marker {marker}")
        adjudication_marker = f"<!-- adjudication:{finding_id} -->"
        if adjudication.count(adjudication_marker) != 1:
            raise CoordinatorError(
                f"ADJUDICATION.md must contain exactly one join marker {adjudication_marker}"
            )
    # The reverse join: a report marker naming an id with no registry row claims a finding that has
    # no evidence, verification or Appendix A row behind it.
    for kind, marked_id in re.findall(r"<!-- (finding|tested-negative|merged):([^\s>][^\s>]*?)(?:->[^\s>]+)? -->", report):
        if marked_id not in final_ids:
            raise CoordinatorError(
                f"REPORT_v1.md has a <!-- {kind}:{marked_id} --> marker but findings.jsonl has no row "
                f"{marked_id}; add the row or remove the marker"
            )
    retired_lines = [
        line.strip() for line in texts["retired_claims.txt"].splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(retired_lines) != len(set(retired_lines)):
        raise CoordinatorError("retired_claims.txt contains duplicate live checks")
    normalized_report = normalized_claim(report)
    for claim in retired_lines:
        normalized = normalized_claim(claim)
        if normalized and normalized in normalized_report:
            raise CoordinatorError(f"retired claim survives in REPORT_v1.md: {claim[:80]}")
    artifacts = {
        name: {
            "canonical_path": target,
            "sha256": sha256_file(paths[name]),
            "bytes": paths[name].stat().st_size,
        }
        for name, target in OUTPUT_TARGETS.items()
    }
    # Round 1 consumes REPORT_final.md. Preserve REPORT_v1.md as the initial draft and promote an
    # identical working copy in the same transaction so critics cannot receive a stale report.
    artifacts["REPORT_final.md"] = {
        "canonical_path": WORKING_REPORT_TARGET,
        "source_output": "REPORT_v1.md",
        "sha256": artifacts["REPORT_v1.md"]["sha256"],
        "bytes": artifacts["REPORT_v1.md"]["bytes"],
    }
    return artifacts, findings


def freeze_attempt(attempt: pathlib.Path) -> None:
    for directory, subdirs, files in os.walk(attempt, topdown=False):
        for name in files:
            try:
                os.chmod(pathlib.Path(directory) / name, 0o444)
            except FileNotFoundError:
                pass
        for name in subdirs:
            try:
                os.chmod(pathlib.Path(directory) / name, 0o555)
            except FileNotFoundError:
                pass
        os.chmod(directory, 0o555)


def invocation_present(root: pathlib.Path, invocation_id: str) -> bool:
    ledger = root / "invocations.jsonl"
    if not ledger.is_file():
        return False
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                if json.loads(line).get("invocation_id") == invocation_id:
                    return True
            except json.JSONDecodeError:
                return False
    return False


def ledger_record(root: pathlib.Path, invocation_id: str) -> dict[str, Any] | None:
    for row in read_ledger(root):
        if row.get("invocation_id") == invocation_id:
            return row
    return None


def append_terminal_record(
    root: pathlib.Path, record: dict[str, Any], *, metadata_21: bool,
) -> None:
    record_invocation.validate_record(record, root)
    if metadata_21:
        # Reservation/request/start were persisted before this point.  Calling
        # record_terminal directly prevents the compatibility path from
        # synthesizing lifecycle events after the provider has already run.
        record_invocation.record_terminal(root, record)
    else:
        record_invocation.append(root, record)


def promote_and_append(
    root: pathlib.Path,
    output: pathlib.Path,
    artifacts: dict[str, dict[str, Any]],
    record: dict[str, Any],
    specs: list[InputSpec],
    manifest: dict[str, Any],
    *,
    transaction_name: str | None = None,
    metadata_21: bool = False,
    append_record: Callable[[pathlib.Path, dict[str, Any]], None] | None = None,
) -> None:
    """Promote validated outputs to their canonical paths and bind the ledger, atomically.

    ``append_record`` is the ledger step run after the files are in place. The default
    appends a new terminal record. recover_misplaced_coordinator_outputs.py passes a
    closure that journals an ``invocation_correction`` for an already-terminal failed
    invocation instead, so the transaction, staging, backup and rollback logic here is
    shared rather than duplicated.
    """
    if append_record is None:
        def append_record(record_root: pathlib.Path, value: dict[str, Any]) -> None:
            append_terminal_record(record_root, value, metadata_21=metadata_21)
    coordinator = root / "coordinator"
    transactions = coordinator / "transactions"
    transactions.mkdir(parents=True, exist_ok=True)
    lock_path = coordinator / "promotion.lock"
    with lock_path.open("a+") as lock:
        lock_wait.acquire_exclusive(lock, "the coordinator promotion lock")
        verify_live_inputs(specs, manifest)
        name = transaction_name or record["invocation_id"]
        if not MACHINE_ID.fullmatch(name):
            raise CoordinatorError(f"unsafe coordinator transaction name: {name!r}")
        transaction = transactions / name
        transaction.mkdir(exist_ok=False)
        staging = transaction / "staging"
        backups = transaction / "backups"
        staging.mkdir(); backups.mkdir()
        journal_entries = []
        for name, artifact in artifacts.items():
            target_relative = artifact["canonical_path"]
            source_name = artifact.get("source_output", name)
            source = output / source_name
            if sha256_file(source) != artifacts[name]["sha256"]:
                raise CoordinatorError(f"validated coordinator output changed: {name}")
            target = root / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise CoordinatorError(f"canonical promotion target is unsafe: {target_relative}")
            staged = staging / name
            shutil.copyfile(source, staged)
            os.chmod(staged, 0o600)
            backup = backups / name
            existed = target.is_file()
            if existed:
                shutil.copyfile(target, backup)
            journal_entries.append({
                "name": name,
                "target": target_relative,
                "sha256": artifacts[name]["sha256"],
                "existed": existed,
                "backup": backup.name if existed else None,
            })
        journal = {
            "schema_version": "1.0.0",
            "state": "backups_ready",
            "invocation_id": record["invocation_id"],
            "entries": journal_entries,
        }
        atomic_write(transaction / "journal.json", pretty_bytes(journal))
        replaced: list[dict[str, Any]] = []
        try:
            for entry in journal_entries:
                os.replace(staging / entry["name"], root / entry["target"])
                replaced.append(entry)
            append_record(root, record)
        except Exception:
            if invocation_present(root, record["invocation_id"]):
                return
            for entry in reversed(replaced):
                target = root / entry["target"]
                if entry["existed"]:
                    os.replace(backups / entry["name"], target)
                elif target.exists() and not target.is_symlink():
                    target.unlink()
            raise
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    shutil.rmtree(transaction, ignore_errors=True)


def make_record(
    *,
    invocation_id: str,
    attempt_number: int | None,
    status: str,
    session_id: str | None,
    requested_model: str,
    observed_model: str | None,
    observed_ids: list[str],
    effort: str,
    started: dt.datetime,
    ended: dt.datetime,
    input_hash: str,
    prompt_hash: str,
    output_artifact: str | None,
    output_hash: str | None,
    usage: dict[str, Any],
    cache_usage: str,
    error: str | None,
    error_class: str | None,
    provider_call: bool = True,
    metadata_21: bool = False,
    provider_request_status: str | None = None,
    inference_start_status: str | None = None,
    turn_count: int | None = None,
    provider_duration_seconds: float | None = None,
    provider_cost: dict[str, Any] | None = None,
    usage_partitions: list[dict[str, Any]] | None = None,
    identity_artifact: str | None = None,
    identity_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "invocation_id": invocation_id,
        "role": "coordinator",
        "role_class": "coordinator",
        "round": None,
        "attempt": attempt_number,
        "status": status,
        "provider": "anthropic",
        "provider_call": provider_call,
        "cache_usage": cache_usage if provider_call else "not_applicable",
        "session_id": session_id,
        "requested_model": requested_model,
        "observed_model": observed_model,
        "model_family": "claude",
        "model_source": "observed" if observed_model else "unavailable",
        "reasoning_effort": effort,
        "started_at": iso(started),
        "ended_at": iso(ended),
        "elapsed_seconds": wall_elapsed_seconds(started, ended),
        "input_artifact_sha256": input_hash,
        "prompt_sha256": prompt_hash,
        "output_artifact": output_artifact,
        "output_sha256": output_hash,
        "usage": usage,
        "error": error,
        "error_class": error_class,
        "legacy_source": None,
    }
    if observed_ids:
        record["observed_model_ids"] = observed_ids
    if metadata_21:
        partitions = list(usage_partitions or [])
        record.update({
            "attempt_kind": "role_inference",
            "provider_request_status": provider_request_status or (
                "accepted" if inference_start_status == "started" else
                "not_requested" if not provider_call else "unknown"
            ),
            "inference_start_status": inference_start_status or (
                "started" if attempt_number is not None else "not_started"
            ),
            "source_invocation_id": None,
            "turn_count": turn_count,
            "provider_cost": provider_cost,
            "served_model": observed_model,
            "identity_assurance": "observed" if observed_model else "requested_only",
            "model_identity_evidence": {
                "source": "cli_log" if identity_artifact else "unavailable",
                "raw_ids": list(observed_ids),
                "artifact": identity_artifact,
                "artifact_sha256": identity_artifact_sha256,
            },
            "usage_by_model": partitions,
            "unattributed_usage": None if partitions else json.loads(json.dumps(usage)),
            "provider_duration_seconds": provider_duration_seconds,
        })
        if observed_model is None:
            record["model_source"] = "declared"
    return record


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def run_once(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--safe-evidence-manifest", required=True, type=pathlib.Path)
    parser.add_argument("--draft-registry", action="append", default=[], type=pathlib.Path)
    parser.add_argument("--reference", action="append", default=[], type=pathlib.Path,
                        help="additional skill-local reference; repeatable")
    parser.add_argument("--claude-bin", default=None,
                        help="Claude CLI executable (fake runner in tests)")
    parser.add_argument("--timeout-min", type=positive_int, default=120)
    parser.add_argument("--max-budget-usd", default="20")
    parser.add_argument("--max-input-mib", type=positive_int, default=20)
    args = parser.parse_args(argv)
    global _LAST_ERROR_CLASS
    _LAST_ERROR_CLASS = None

    root = args.audit_dir.resolve()
    attempt: pathlib.Path | None = None
    status_path: pathlib.Path | None = None
    invocation_id: str | None = None
    attempt_number: int | None = None
    input_hash: str | None = None
    prompt_hash: str | None = None
    session_id: str | None = None
    started: dt.datetime | None = None
    ended: dt.datetime | None = None
    provider_started = False
    provider_pid: int | None = None
    stdout = ""
    stderr = ""
    timed_out = False
    returncode: int | None = None
    coordinator_policy: dict[str, Any] | None = None
    audit_id: str | None = None
    metadata_21 = False
    lifecycle_reserved = False
    inference_started = False
    provider_request_status = "not_requested"
    turn_count: int | None = None
    provider_duration_seconds: float | None = None
    provider_cost: dict[str, Any] | None = None
    usage_partitions: list[dict[str, Any]] = []
    identity_artifact: str | None = None
    identity_artifact_sha256: str | None = None
    try:
        if not root.is_dir():
            raise CoordinatorError(f"audit directory does not exist: {root}")
        policy = record_invocation.load_policy_snapshot(root)
        record_invocation.validate_policy_binding(root)
        coordinator_policy = policy["models"]["coordinator"]
        # 2.7.0: exactly one of the packaged Claude routes, as this audit's snapshot froze it
        # (Sonnet 5 before 2.7.0, Sonnet 5.5 after; at max before 2.7.3, at xhigh after); never config/.
        if coordinator_policy not in claude_routes.LIVE_ROUTES:
            raise CoordinatorError(
                "audit-scoped coordinator policy is not a packaged Claude route: "
                f"{coordinator_policy} (allowed: "
                + "; ".join(claude_routes.describe(route) for route in claude_routes.LIVE_ROUTES)
                + "); a frozen snapshot is never edited"
            )
        run = strict_object(require_regular(root / "run.json", "run metadata"))
        audit_id = run.get("audit_id")
        metadata_21 = run.get("schema_version") == "2.1.0"
        if run.get("schema_version") not in {"2.0.0", "2.1.0"} or not isinstance(audit_id, str) or not audit_id:
            raise CoordinatorError("run.json must be live schema 2.0.0 or 2.1.0 with a non-empty audit_id")
        rows = read_ledger(root)
        bindings, report_hashes = phase_one_bindings(root, rows, policy)
        draft_paths = resolve_draft_paths(root, args.draft_registry)
        draft_rows, draft_ids = load_draft_rows(draft_paths)
        safe_paths = load_safe_evidence(root, args.safe_evidence_manifest)
        extras = [path if path.is_absolute() else SKILL / path for path in args.reference]
        specs, input_manifest, input_hash = specs_and_manifest(
            root, draft_paths, safe_paths, bindings, extras,
            args.max_input_mib * 1024 * 1024,
            freeze_run_projection=metadata_21,
        )
        guard_stranded_attempts(root, rows)
        completed = completed_for_input(rows, input_hash, root, input_manifest)
        if completed is not None:
            print(json.dumps({
                "status": "completed",
                "invocation_id": completed["invocation_id"],
                "output_artifact": completed["output_artifact"],
                "reused": True,
            }, sort_keys=True))
            return 0
        attempt_number = next_attempt(rows, accepted_only=metadata_21)
        invocation_id = f"coordinator-{uuid.uuid4()}"
        attempt = (
            root / "coordinator/invocations" / input_hash
            / f"attempt_{attempt_number:02d}_{invocation_id}"
        )
        attempt.mkdir(parents=True, exist_ok=False)
        output = attempt / "output.partial"
        output.mkdir()
        source = materialize_snapshot(root, attempt, specs, input_manifest)
        known_pairs, known_paths, canonical_paths = known_evidence(
            draft_rows, report_hashes, root, safe_paths, input_manifest.get("files"))
        prompt = build_prompt(audit_id, invocation_id, input_hash, coordinator_policy["requested_model"],
                              coordinator_policy["reasoning_effort"])
        prompt_hash = sha256_bytes(prompt.encode("utf-8"))
        (attempt / "prompt.txt").write_text(prompt, encoding="utf-8")
        settings_value = sandbox_settings(root, attempt, source, output)
        settings_path = attempt / "claude_settings.json"
        settings_path.write_bytes(pretty_bytes(settings_value))
        session_id = str(uuid.uuid4())
        claude = args.claude_bin or shutil.which("claude")
        if not claude:
            raise CoordinatorError("claude CLI not found")
        command = claude_command(
            claude, settings_path, source, session_id,
            coordinator_policy["spawn_alias"], coordinator_policy["reasoning_effort"],
            args.max_budget_usd,
        )
        status_path = attempt / "status.json"
        started = utc_now()
        if metadata_21:
            reserved = record_invocation.reserve_invocation(root, {
                "invocation_id": invocation_id,
                "role": "coordinator", "role_class": "coordinator", "round": None,
                "attempt_kind": "role_inference",
                "provider": coordinator_policy["provider"],
                "requested_model": coordinator_policy["requested_model"],
                "reasoning_effort": coordinator_policy["reasoning_effort"],
                "input_artifact_sha256": input_hash,
                "prompt_sha256": prompt_hash,
            })
            lifecycle_reserved = True
            attempt_number = reserved["attempt_candidate"]
        atomic_write(status_path, pretty_bytes({
            "schema_version": "1.0.0",
            "state": "launching",
            "audit_id": audit_id,
            "invocation_id": invocation_id,
            "attempt": None if metadata_21 else attempt_number,
            "attempt_candidate": attempt_number if metadata_21 else None,
            "input_artifact_sha256": input_hash,
            "provider_call_started": False,
            "started_at": iso(started),
        }))

        def mark_provider_started(pid: int) -> None:
            nonlocal provider_started, provider_pid, provider_request_status
            # Popen has returned successfully, so this flag remains truthful even if the durable
            # write itself fails and the child must be terminated by run_provider.
            provider_started = True
            provider_pid = pid
            if metadata_21:
                record_invocation.mark_provider_request(
                    root, invocation_id, status="requested",
                )
                provider_request_status = "requested"
            atomic_write(status_path, pretty_bytes({
                "schema_version": "1.0.0",
                "state": "running",
                "audit_id": audit_id,
                "invocation_id": invocation_id,
                "attempt": None if metadata_21 else attempt_number,
                "attempt_candidate": attempt_number if metadata_21 else None,
                "input_artifact_sha256": input_hash,
                "provider_call_started": True,
                "provider_pid": pid,
                "started_at": iso(started),
            }))
            # The CLI's built-in Write tool is not confined by the sandbox's filesystem
            # rules the way shell commands are; only cwd and prose steer it. The third
            # live coordinator call wrote all four outputs to this attempt directory, one
            # level above output.partial, and the attempt was rejected. Make the attempt
            # root read-only for the duration of the call so a stray write fails loudly
            # inside the model's session instead of silently landing in the wrong place.
            os.chmod(attempt, 0o555)

        try:
            returncode, stdout, stderr, timed_out = run_provider(
                command, output, prompt, args.timeout_min * 60, mark_provider_started,
                route=coordinator_policy,
            )
        finally:
            os.chmod(attempt, 0o755)
        ended = utc_now()
        (attempt / "claude_stdout.json").write_text(stdout, encoding="utf-8")
        (attempt / "claude_stderr.txt").write_text(stderr, encoding="utf-8")
        result: dict[str, Any] = {}
        parse_error: str | None = None
        try:
            result = parse_provider_result(stdout)
        except CoordinatorError as exc:
            parse_error = str(exc)
        primary, all_models, primary_count = observed_models(
            result, coordinator_policy["requested_model"],
        )
        usage, cache_usage = extract_usage(result)
        usage_partitions = usage_by_model(result)
        turn_count = result_turn_count(result)
        provider_duration_seconds = result_provider_duration_seconds(result)
        provider_cost = result_provider_cost(result)
        identity_artifact = audit_relative(root, attempt / "claude_stdout.json")
        identity_artifact_sha256 = sha256_file(attempt / "claude_stdout.json")
        inference_proven = inference_evidence_complete(
            primary, usage, turn_count, provider_duration_seconds,
        )
        # 2.7.6: a call stopped at --timeout-min prints no result envelope, so it was booked as never started and
        # consumed no attempt: repeated 120-minute calls were unbounded. As Phase 1 has done since 2.7.2, a call that
        # timed out, or that left no result envelope but wrote into its (fresh) output workspace, ran inference and is
        # charged an attempt. An envelope that reports a pre-inference rejection is still believed.
        ran_inference = inference_proven or timed_out or (parse_error is not None and _wrote_files(output))
        if metadata_21 and ran_inference:
            started_result = record_invocation.mark_inference_start(root, invocation_id)
            attempt_number = started_result["attempt"]
            inference_started = True
            provider_request_status = "accepted"
            record_invocation.mark_response_received(
                root, invocation_id,
                artifact=identity_artifact,
                sha256=identity_artifact_sha256,
            )
        observed_session = result.get("session_id") if isinstance(result.get("session_id"), str) else session_id
        provider_error = bool(result.get("is_error")) or result.get("subtype") in {"error", "failed"}
        validation_errors: list[str] = []
        if returncode != 0:
            validation_errors.append(f"Claude CLI exited {returncode}")
        if provider_error:
            validation_errors.append("Claude result envelope reports an error")
            provider_message = result.get("error") or result.get("message") or result.get("result")
            if isinstance(provider_message, str) and provider_message.strip():
                validation_errors.append(provider_message.strip())
        if parse_error:
            validation_errors.append(parse_error)
        if primary_count != 1:
            validation_errors.append(
                f"expected exactly one served "
                f"{claude_routes.short_name(coordinator_policy['requested_model'])} primary; "
                f"observed {primary_count}: {all_models!r}"
            )
        if not complete_usage(usage):
            validation_errors.append("completed invocation lacks exact input/output/total usage")
        if metadata_21 and not inference_proven:
            validation_errors.append(
                "response does not jointly prove served model, nonzero tokens, turns, and API duration"
            )
        try:
            verify_snapshot(source, input_manifest)
            verify_live_inputs(specs, input_manifest)
        except CoordinatorError as exc:
            validation_errors.append(str(exc))
        artifacts: dict[str, dict[str, Any]] | None = None
        findings: list[dict[str, Any]] = []
        if not validation_errors:
            try:
                artifacts, findings = validate_outputs(
                    output, draft_ids, known_pairs, known_paths, rows,
                    canonical_paths
                )
            except CoordinatorError as exc:
                validation_errors.append(str(exc))
                misplaced = [
                    name for name in OUTPUT_TARGETS
                    if not (output / name).is_file() and (attempt / name).is_file()
                    and not (attempt / name).is_symlink()
                ]
                if len(misplaced) == len(OUTPUT_TARGETS):
                    validation_errors.append(
                        "all four coordinator outputs were written to the attempt directory "
                        "instead of output.partial; the response is intact and can be recovered "
                        "without another provider call: python3 scripts/"
                        "recover_misplaced_coordinator_outputs.py <audit-dir> --attempt <this attempt>"
                    )
        denials = result.get("permission_denials") if isinstance(result.get("permission_denials"), list) else []
        denial_summary = {
            "count": len(denials),
            "tool_names": sorted({
                str(item.get("tool_name")) for item in denials
                if isinstance(item, dict) and item.get("tool_name")
            }),
            "raw_stdout_sha256": sha256_file(attempt / "claude_stdout.json"),
        }
        (attempt / "permission_denials.json").write_bytes(pretty_bytes(denial_summary))
        if not validation_errors and artifacts is not None:
            bundle = {
                "schema_version": "1.0.0",
                "audit_id": audit_id,
                "invocation_id": invocation_id,
                "input_artifact_sha256": input_hash,
                "prompt_sha256": prompt_hash,
                "observed_model": primary,
                "observed_model_ids": all_models,
                "provider_stdout_sha256": sha256_file(attempt / "claude_stdout.json"),
                "outputs": artifacts,
                "finding_ids": [row["id"] for row in findings],
            }
            bundle_path = attempt / "coordinator_bundle.json"
            bundle_path.write_bytes(pretty_bytes(bundle))
            bundle_relative = audit_relative(root, bundle_path)
            record = make_record(
                invocation_id=invocation_id,
                attempt_number=attempt_number if (not metadata_21 or inference_started) else None,
                status="completed", session_id=observed_session,
                requested_model=coordinator_policy["requested_model"],
                observed_model=primary, observed_ids=all_models,
                effort=coordinator_policy["reasoning_effort"], started=started, ended=ended,
                input_hash=input_hash, prompt_hash=prompt_hash,
                output_artifact=bundle_relative, output_hash=sha256_file(bundle_path),
                usage=usage, cache_usage=cache_usage, error=None, error_class=None,
                metadata_21=metadata_21,
                provider_request_status=provider_request_status,
                inference_start_status="started" if inference_started else "not_started",
                turn_count=turn_count, provider_duration_seconds=provider_duration_seconds,
                provider_cost=provider_cost, usage_partitions=usage_partitions,
                identity_artifact=identity_artifact,
                identity_artifact_sha256=identity_artifact_sha256,
            )
            (attempt / "invocation_record.json").write_bytes(pretty_bytes(record))
            promote_and_append(
                root, output, artifacts, record, specs, input_manifest,
                metadata_21=metadata_21,
            )
            final_status = {
                "schema_version": "1.0.0", "state": "completed", "audit_id": audit_id,
                "invocation_id": invocation_id, "attempt": attempt_number,
                "input_artifact_sha256": input_hash, "provider_call_started": True,
                "provider_pid": provider_pid, "started_at": iso(started),
                "ended_at": iso(ended),
                "elapsed_seconds": wall_elapsed_seconds(started, ended),
                "returncode": returncode, "observed_model": primary,
                "observed_model_ids": all_models, "usage": usage,
                "permission_denials": denial_summary, "outputs": artifacts,
            }
            atomic_write(status_path, pretty_bytes(final_status))
            freeze_attempt(attempt)
            print(json.dumps({
                "status": "completed", "invocation_id": invocation_id,
                "attempt_directory": audit_relative(root, attempt),
                "observed_model": primary, "observed_model_ids": all_models,
                "outputs": {name: item["canonical_path"] for name, item in artifacts.items()},
            }, sort_keys=True))
            return 0
        error = redact_private_paths(
            " | ".join(validation_errors + ([stderr.strip()] if stderr.strip() else [])), root,
        )[:4000]
        schema_failure = any(
            token in error.lower() for token in (
                "required coordinator output", "findings.jsonl", "report_v1", "adjudication",
                "join marker", "evidence hash", "became stale", "immutable snapshot",
                "usage",
            )
        )
        if metadata_21 and not inference_started:
            schema_failure = any(
                token in error.lower()
                for token in ("schema rejected", "invalid schema", "schema admission")
            )
        error_class = classify_error(error, timed_out=timed_out, schema=schema_failure)
        _LAST_ERROR_CLASS = error_class
        if metadata_21 and provider_started and not inference_started:
            terminal_request_status = (
                "rejected" if error_class in {
                    "authentication", "authorization", "quota", "model", "capability",
                    "schema", "deterministic_schema",
                } else "unknown"
            )
            record_invocation.mark_provider_request(
                root, invocation_id, status=terminal_request_status,
            )
            provider_request_status = terminal_request_status
        record = make_record(
            invocation_id=invocation_id,
            attempt_number=attempt_number if (not metadata_21 or inference_started) else None,
            status="timed_out" if timed_out else "failed", session_id=observed_session,
            requested_model=coordinator_policy["requested_model"], observed_model=primary,
            observed_ids=all_models, effort=coordinator_policy["reasoning_effort"],
            started=started, ended=ended, input_hash=input_hash,
            prompt_hash=prompt_hash, output_artifact=None, output_hash=None, usage=usage,
            cache_usage=cache_usage, error=error or "unknown coordinator failure",
            error_class=error_class,
            metadata_21=metadata_21,
            provider_request_status=provider_request_status,
            inference_start_status="started" if inference_started else "not_started",
            turn_count=turn_count, provider_duration_seconds=provider_duration_seconds,
            provider_cost=provider_cost, usage_partitions=usage_partitions,
            identity_artifact=identity_artifact,
            identity_artifact_sha256=identity_artifact_sha256,
        )
        (attempt / "invocation_record.json").write_bytes(pretty_bytes(record))
        append_terminal_record(root, record, metadata_21=metadata_21)
        atomic_write(status_path, pretty_bytes({
            "schema_version": "1.0.0", "state": record["status"], "audit_id": audit_id,
            "invocation_id": invocation_id, "attempt": record["attempt"],
            "attempt_candidate": attempt_number if metadata_21 else None,
            "input_artifact_sha256": input_hash, "provider_call_started": True,
            "provider_pid": provider_pid, "started_at": iso(started),
            "ended_at": iso(ended),
            "elapsed_seconds": wall_elapsed_seconds(started, ended),
            "returncode": returncode, "observed_model": primary,
            "observed_model_ids": all_models, "usage": usage,
            "permission_denials": denial_summary, "error": error,
        }))
        freeze_attempt(attempt)
        print(json.dumps({
            "status": record["status"], "invocation_id": invocation_id,
            "attempt_directory": audit_relative(root, attempt), "error": error,
        }, sort_keys=True))
        return 2
    except (CoordinatorError, OSError, KeyError, TypeError, ValueError) as exc:
        # Once Popen succeeds, every exit path must leave a terminal provider-call row.  Also
        # treat a canonical ledger row as authoritative when only the later status/freeze step
        # failed; retrying that completed call would waste tokens and violate idempotence.
        if (
            invocation_id is not None and attempt is not None and status_path is not None
            and (not metadata_21 or lifecycle_reserved)
        ):
            try:
                existing = ledger_record(root, invocation_id)
                if existing is not None:
                    cleanup_error: Exception | None = None
                    try:
                        atomic_write(status_path, pretty_bytes({
                            "schema_version": "1.0.0",
                            "state": existing["status"],
                            "audit_id": audit_id,
                            "invocation_id": invocation_id,
                            "attempt": existing["attempt"],
                            "input_artifact_sha256": existing["input_artifact_sha256"],
                            "provider_call_started": existing["provider_call"],
                            "provider_pid": provider_pid,
                            "started_at": existing["started_at"],
                            "ended_at": existing["ended_at"],
                            "elapsed_seconds": existing["elapsed_seconds"],
                            "returncode": returncode,
                            "observed_model": existing["observed_model"],
                            "observed_model_ids": existing.get("observed_model_ids", []),
                            "usage": existing["usage"],
                            "error": existing["error"],
                        }))
                        freeze_attempt(attempt)
                    except (OSError, ValueError) as cleanup_exc:
                        cleanup_error = cleanup_exc
                    if existing["status"] == "completed":
                        print(
                            "WARNING: coordinator completed canonically but local status cleanup "
                            f"failed: {exc}"
                            + (f"; retry also failed: {cleanup_error}" if cleanup_error else ""),
                            file=sys.stderr,
                        )
                        return 0
                    print(json.dumps({
                        "status": existing["status"],
                        "invocation_id": invocation_id,
                        "attempt_directory": audit_relative(root, attempt),
                        "error": existing.get("error"),
                    }, sort_keys=True))
                    return 2

                if (
                    started is not None and attempt_number is not None
                    and input_hash is not None and prompt_hash is not None
                    and coordinator_policy is not None and audit_id is not None
                ):
                    terminal_ended = ended or utc_now()
                    if terminal_ended < started:
                        terminal_ended = started
                    parsed: dict[str, Any] = {}
                    if stdout:
                        try:
                            parsed = parse_provider_result(stdout)
                        except CoordinatorError:
                            pass
                    primary, all_models, _primary_count = observed_models(
                        parsed, coordinator_policy["requested_model"],
                    )
                    usage, cache_usage = extract_usage(parsed)
                    usage_partitions = usage_by_model(parsed)
                    turn_count = result_turn_count(parsed)
                    provider_duration_seconds = result_provider_duration_seconds(parsed)
                    provider_cost = result_provider_cost(parsed)
                    stdout_path = attempt / "claude_stdout.json"
                    if stdout_path.is_file():
                        identity_artifact = audit_relative(root, stdout_path)
                        identity_artifact_sha256 = sha256_file(stdout_path)
                    if metadata_21:
                        state = lifecycle_journal._reservation_states(
                            lifecycle_journal.load_events(root)
                        ).get(invocation_id, {})
                        provider_request_status = state.get(
                            "provider_request_status", provider_request_status,
                        )
                        inference_started = state.get("inference_start_status") == "started"
                        if inference_started:
                            attempt_number = state.get("attempt")
                        proof = inference_evidence_complete(
                            primary, usage, turn_count, provider_duration_seconds,
                        )
                        if provider_started and proof and not inference_started:
                            started_result = record_invocation.mark_inference_start(
                                root, invocation_id,
                            )
                            attempt_number = started_result["attempt"]
                            inference_started = True
                            provider_request_status = "accepted"
                        if (
                            inference_started and not state.get("response_received")
                            and identity_artifact is not None
                            and identity_artifact_sha256 is not None
                        ):
                            record_invocation.mark_response_received(
                                root, invocation_id, artifact=identity_artifact,
                                sha256=identity_artifact_sha256,
                            )
                    observed_session = (
                        parsed.get("session_id")
                        if isinstance(parsed.get("session_id"), str)
                        else (session_id if provider_started else None)
                    )
                    message = str(exc)
                    if stderr.strip():
                        message = f"{message} | {stderr.strip()}"
                    provider_message = parsed.get("error") or parsed.get("message")
                    if isinstance(provider_message, str) and provider_message.strip():
                        message = f"{message} | {provider_message.strip()}"
                    message = message[:4000] or "unknown coordinator failure"
                    error_class = classify_error(message, timed_out=timed_out)
                    _LAST_ERROR_CLASS = error_class
                    if metadata_21 and provider_started and not inference_started:
                        terminal_request_status = (
                            "rejected" if error_class in {
                                "authentication", "authorization", "quota", "model",
                                "capability", "schema", "deterministic_schema",
                            } else "unknown"
                        )
                        state = lifecycle_journal._reservation_states(
                            lifecycle_journal.load_events(root)
                        ).get(invocation_id, {})
                        current_request = state.get("provider_request_status", "not_requested")
                        if current_request != terminal_request_status:
                            record_invocation.mark_provider_request(
                                root, invocation_id, status=terminal_request_status,
                            )
                        provider_request_status = terminal_request_status
                    record = make_record(
                        invocation_id=invocation_id,
                        attempt_number=(
                            attempt_number if (not metadata_21 or inference_started) else None
                        ),
                        status="timed_out" if timed_out else "failed",
                        session_id=observed_session,
                        requested_model=coordinator_policy["requested_model"],
                        observed_model=primary,
                        observed_ids=all_models,
                        effort=coordinator_policy["reasoning_effort"],
                        started=started,
                        ended=terminal_ended,
                        input_hash=input_hash,
                        prompt_hash=prompt_hash,
                        output_artifact=None,
                        output_hash=None,
                        usage=usage,
                        cache_usage=cache_usage,
                        error=message,
                        error_class=error_class,
                        provider_call=provider_started,
                        metadata_21=metadata_21,
                        provider_request_status=provider_request_status,
                        inference_start_status=(
                            "started" if inference_started else "not_started"
                        ),
                        turn_count=turn_count,
                        provider_duration_seconds=provider_duration_seconds,
                        provider_cost=provider_cost,
                        usage_partitions=usage_partitions,
                        identity_artifact=identity_artifact,
                        identity_artifact_sha256=identity_artifact_sha256,
                    )
                    atomic_write(attempt / "invocation_record.json", pretty_bytes(record))
                    append_terminal_record(root, record, metadata_21=metadata_21)
                    atomic_write(status_path, pretty_bytes({
                        "schema_version": "1.0.0",
                        "state": record["status"],
                        "audit_id": audit_id,
                        "invocation_id": invocation_id,
                        "attempt": record["attempt"],
                        "attempt_candidate": attempt_number if metadata_21 else None,
                        "input_artifact_sha256": input_hash,
                        "provider_call_started": provider_started,
                        "provider_pid": provider_pid,
                        "started_at": iso(started),
                        "ended_at": iso(terminal_ended),
                        "elapsed_seconds": wall_elapsed_seconds(started, terminal_ended),
                        "returncode": returncode,
                        "observed_model": primary,
                        "observed_model_ids": all_models,
                        "usage": usage,
                        "error": message,
                    }))
                    freeze_attempt(attempt)
                    print(json.dumps({
                        "status": record["status"],
                        "invocation_id": invocation_id,
                        "attempt_directory": audit_relative(root, attempt),
                        "provider_call_started": provider_started,
                        "error": message,
                    }, sort_keys=True))
                    return 2
            except (CoordinatorError, OSError, KeyError, TypeError, ValueError) as durable_exc:
                print(
                    f"FATAL: {exc}; additionally failed to persist terminal coordinator state: "
                    f"{durable_exc}",
                    file=sys.stderr,
                )
                return 1
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


# The class of the most recent recorded provider failure, so main() can decide whether the
# attempt it just watched fail is worth another one without re-reading the ledger.
_LAST_ERROR_CLASS: str | None = None
RETRYABLE_ERROR_CLASSES = frozenset({"transport", "transient", "output_limit"})
# 2.7.4: an output-cap overflow costs a whole coordinator call (33 minutes and USD 7.11 on the first), so
# it earns one fresh attempt per invocation, not the transient budget.
OUTPUT_LIMIT_RETRIES = 1


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return value if value >= 0 else default


def main(argv: list[str] | None = None) -> int:
    """Run one coordinator attempt, and retry it in this same invocation on a transport or
    transient failure.

    The runner used to record such a failure faithfully and exit 2, leaving each further attempt
    to a human re-run -- correct, but in an unattended pipeline it turned a 39-minute
    `Connection closed mid-response` into a stalled audit until someone noticed. Every attempt is
    still its own reservation and its own terminal row; the attempt budget (I4R_MAX_ATTEMPTS)
    still bounds the total, and next_attempt() refusing is a permanent stop. Only the two
    retryable classes loop; authentication, quota, model, schema and provider-safety refusals
    stop at once, as before. I4R_COORDINATOR_TRANSIENT_RETRIES caps the in-invocation retries
    (default 2, so at most the three-attempt budget in one run); RETRY_BACKOFF_SECONDS (default
    30) scales a linear backoff between them.
    """
    retries = min(int(_env_number("I4R_COORDINATOR_TRANSIENT_RETRIES", 2)), 20)   # 2.7.6: bounded like I4R_MAX_ATTEMPTS
    backoff = _env_number("RETRY_BACKOFF_SECONDS", 30.0)
    made = 0
    output_limit_made = 0
    while True:
        code = run_once(argv)
        if code != 2 or _LAST_ERROR_CLASS not in RETRYABLE_ERROR_CLASSES or made >= retries:
            return code
        if _LAST_ERROR_CLASS == "output_limit":
            if output_limit_made >= OUTPUT_LIMIT_RETRIES:
                return code
            output_limit_made += 1
        made += 1
        delay = backoff * made
        print(
            f"[retry] coordinator attempt failed with a {_LAST_ERROR_CLASS} error; retrying in "
            f"{delay:g}s (in-invocation retry {made}/{retries}; the attempt budget still applies)",
            file=sys.stderr, flush=True,
        )
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
