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
import document_release
import lifecycle_journal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    import lock_wait  # noqa: E402
except ImportError:  # a standalone copy (e.g. a release's scripts/) without its sibling helper
    class lock_wait:  # noqa: N801
        @staticmethod
        def acquire_exclusive(handle, label):
            import fcntl as _fcntl
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)

RECEIPT = "finalization_receipt.json"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,199}$")
CURRENT_RUN_SCHEMAS = {"2.0.0", "2.1.0"}
# registry/<audit_id>/index_row.json: the audit's own index row, written only by its own filing (2.7.9).
INDEX_ROW = "index_row.json"
# Dropbox keeps the losing side of a sync conflict beside the file as
# "index (<someone>'s conflicted copy <date>).jsonl". Read for rows, never renamed or deleted.
INDEX_CONFLICTS = "index (*conflicted copy*"
# The remedy for an audit's own row file that is stale or cannot be used.
RECOVER_HINT = ("; `finalization_receipt.py registry-recover <tree>/registry <audit dir>` replaces it with the "
                "row the receipt hashes to")


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
    try:
        manifest = document_release.verify_release(
            root, manifest_path=path, builder=HERE / "build_docx.sh",
            boundary="finalization-receipt",
        )
    except document_release.DocumentReleaseError as exc:
        raise ValueError(str(exc)) from exc
    bound: list[str] = ["document_build_manifest.json"]
    inputs = manifest.get("inputs") or {}
    artifacts = manifest.get("artifacts") or {}
    for label in ("report", "front_matter", "captions", "policy_snapshot"):
        item = inputs.get(label)
        if not isinstance(item, dict):
            raise ValueError(f"document-build manifest lacks {label} binding")
        artifact = contained(root, item.get("path"))
        if item.get("sha256") != sha256(artifact):
            raise ValueError(f"document-build manifest {label} hash is stale")
        bound.append(item["path"])
    pdf = artifacts.get("pdf")
    if not isinstance(pdf, dict):
        raise ValueError("document-build manifest lacks PDF binding")
    pdf_artifact = contained(root, pdf.get("path"))
    if pdf.get("sha256") != sha256(pdf_artifact):
        raise ValueError("document-build manifest PDF hash is stale")
    bound.append(pdf["path"])
    docx = artifacts.get("docx")
    if docx is not None:
        if not isinstance(docx, dict):
            raise ValueError("document-build manifest docx binding is malformed")
        artifact = contained(root, docx.get("path"))
        if docx.get("sha256") != sha256(artifact):
            raise ValueError("document-build manifest docx hash is stale")
        bound.append(docx["path"])
    if (inputs["report"].get("path") != "report/REPORT_final.md" or
            not str(artifacts["pdf"].get("path", "")).startswith("report/")):
        raise ValueError("document build must bind REPORT_final.md to a PDF under report/")
    return manifest, bound


def validate_metadata_21_binding(root: pathlib.Path, run: dict[str, Any]) -> None:
    """Prove every 2.1 projection names the tip of the canonical hash chain."""
    events = lifecycle_journal.load_events(root, allow_legacy_prefix=True)
    if not events:
        raise ValueError("metadata 2.1 canonical journal is empty")
    expected = lifecycle_journal.watermark(events)
    if run.get("journal_watermark") != expected:
        raise ValueError("run.json journal watermark differs from the canonical journal tip")
    invocation_watermark = strict_json(root / "invocations.watermark.json")
    if invocation_watermark != expected:
        raise ValueError("invocations.watermark.json differs from the canonical journal tip")
    metrics = strict_json(root / "run_metrics.json")
    if not isinstance(metrics, dict) or metrics.get("journal_watermark") != expected:
        raise ValueError("run_metrics.json differs from the canonical journal tip")


def artifact_paths(root: pathlib.Path, run: dict[str, Any]) -> list[str]:
    base = [
        "run.json", "comment.json", "verification_manifest.json",
        "report/REPORT_final.md",
    ]
    if run.get("schema_version") in CURRENT_RUN_SCHEMAS:
        base += [
            "pipeline-policy.snapshot.json", "invocations.jsonl",
            "work/findings.jsonl", "work/finding_projections.manifest.json",
        ]
    if run.get("schema_version") == "2.1.0":
        validate_metadata_21_binding(root, run)
        base += [
            "run.events.jsonl", "invocations.watermark.json", "run_metrics.json",
            "RUN_METADATA.md",
        ]
    claims = [path for path in root.glob("claims_script.*") if path.is_file() and not path.is_symlink()]
    if run.get("schema_version") in CURRENT_RUN_SCHEMAS and len(claims) != 1:
        raise ValueError(f"expected exactly one regular claims_script.*, found {len(claims)}")
    if len(claims) > 1:
        raise ValueError(f"expected at most one regular claims_script.* for legacy audit, found {len(claims)}")
    if claims:
        base.append(claims[0].relative_to(root).as_posix())
    _, document_paths = document_binding(root, required=run.get("schema_version") == "2.1.0")
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


def index_row(run: dict[str, Any], comment: dict[str, Any], *, version: str,
              pair_manifest_sha256: str, filed_at: str) -> dict[str, Any]:
    return {
        "audit_id": run.get("audit_id"),
        "filed_at": filed_at,
        "version": version,
        "pair_manifest_sha256": pair_manifest_sha256,
        "doi": run.get("paper_doi"),
        "journal": (comment.get("bibliographic") or {}).get("journal"),
        "findings_total": (comment.get("rollup") or {}).get("findings_total"),
        "rounds_run": run.get("rounds_run"),
        "stopping_reason": run.get("stopping_reason"),
        "compliance_pass": (run.get("compliance") or {}).get("compliance_pass"),
        "token_coverage": (run.get("token_accounting") or {}).get("coverage_fraction"),
        # coverage_fraction counts an honestly-unavailable call as covered, so 1.0 does
        # not mean full accounting. Carry the qualifier into the row a human compares
        # audits with, or the registry reads as though every run was fully accounted.
        "token_totals_are_lower_bounds": (
            run.get("token_accounting") or {}).get("totals_are_lower_bounds"),
        "invocations_with_unavailable_total": (
            run.get("token_accounting") or {}).get("invocations_with_unavailable_total"),
    }


def index_line(row: dict[str, Any]) -> bytes:
    """One row exactly as index.jsonl and registry/<id>/index_row.json hold it."""
    return (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode()


def index_row_hash(row: dict[str, Any]) -> str:
    """The receipt's index_row_sha256: the row's compact form, not the bytes on disk."""
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _index_file_rows(path: pathlib.Path, *, strict: bool) -> list[dict[str, Any]]:
    """The audit rows of one index file. index.jsonl is strict, as before: a bad line refuses. A
    conflicted copy is only a salvage source, so a line it cannot give is skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        if strict:
            raise
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = validate_json_schema.strict_loads(line, source=f"{path}:{number}")
            if not isinstance(row, dict) or not isinstance(row.get("audit_id"), str):
                raise ValueError(f"registry index line {number} is not a valid audit row")
        except ValueError:
            if strict:
                raise
            continue
        rows.append(row)
    return rows


def _own_row(path: pathlib.Path) -> tuple[dict[str, Any] | None, str | None]:
    """registry/<id>/index_row.json, or why it cannot be used (naming no path: the reason reaches logs)."""
    try:
        if path.is_symlink() or not path.is_file():
            return None, "is a symlink or not a regular file"
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return None, f"cannot be read ({getattr(exc, 'strerror', None) or type(exc).__name__})"
    try:
        row = validate_json_schema.strict_loads(text, source=INDEX_ROW)
    except ValueError:
        return None, "is empty" if not text.strip() else "is not valid JSON (truncated?)"
    if not isinstance(row, dict) or row.get("audit_id") != path.parent.name:
        return None, "does not hold that audit's row"
    return row, None


def _pointer_problem(path: pathlib.Path) -> str | None:
    """Why registry/<id>/current.json cannot be used, read as strict_json reads it (naming no path), or None.
    registry-audit and registry-recover need it to tell which row is the audit's current filing; `check` too, except
    that it follows a symlink that stays inside the registry (status.py says the same)."""
    try:
        if path.is_symlink():
            return "is a symlink"
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return f"cannot be read ({getattr(exc, 'strerror', None) or type(exc).__name__})"
    try:
        pointer = validate_json_schema.strict_loads(text, source="current.json")
    except ValueError as exc:
        return ("is empty" if not text.strip() else "is not valid JSON (truncated?)"
                if isinstance(exc.__cause__, json.JSONDecodeError) else "is not strict JSON (a repeated key, or NaN)")
    if not isinstance(pointer, dict):
        return "is not a JSON object"
    if not isinstance(pointer.get("version"), str) or not isinstance(pointer.get("pair_manifest_sha256"), str):
        return "names no filing (no version or pair_manifest_sha256 string)"
    return None


def index_sources(registry: pathlib.Path, *, skip: str | None = None) -> tuple[
        list[dict[str, Any]], dict[str, dict[str, Any]], list[tuple[str, list[dict[str, Any]]]], dict[str, str]]:
    """Every index row the registry holds: index.jsonl's, each registry/<id>/index_row.json, and
    those in Dropbox conflicted copies of the index; and the row files that cannot be used, with why.

    A row file that cannot be used (empty, truncated, unreadable) is left out rather than refused: with
    one file per audit, one bad file would otherwise stop every filing of every audit. Nothing is lost,
    because the view keeps index.jsonl's row for that audit, and its own `check` still refuses. `skip`
    is the audit being filed or recovered, whose file is about to be replaced and is not read.
    """
    index_path = registry / "index.jsonl"
    listed: list[dict[str, Any]] = []
    if index_path.exists() or index_path.is_symlink():
        if index_path.is_symlink() or not index_path.is_file():
            raise ValueError("registry index is not a regular file")
        listed = _index_file_rows(index_path, strict=True)
    own: dict[str, dict[str, Any]] = {}
    broken: dict[str, str] = {}
    for path in sorted(registry.glob("*/" + INDEX_ROW)):
        if path.parent.name == skip:
            continue
        row, problem = _own_row(path)
        if row is None:
            broken[path.parent.name] = str(problem)
        else:
            own[path.parent.name] = row
    conflicts = [(path.name, _index_file_rows(path, strict=False))
                 for path in sorted(registry.glob(INDEX_CONFLICTS))
                 if path.is_file() and not path.is_symlink()]
    return listed, own, conflicts, broken


def _warn_broken(broken: dict[str, str]) -> None:
    for audit_id, problem in sorted(broken.items()):
        print(f"WARNING: registry/{audit_id}/{INDEX_ROW} {problem}, so it was left out of index.jsonl (the row "
              "index.jsonl holds for that audit is kept). That audit's `check` refuses until it is replaced: "
              "finalization_receipt.py registry-recover <tree>/registry <its audit dir>", file=sys.stderr)


def _is_current_filing(registry: pathlib.Path, row: dict[str, Any],
                       pointers: dict[str, Any] | None = None) -> bool:
    """Whether the row describes its audit's current filing (registry/<id>/current.json)."""
    audit_id = row.get("audit_id")
    if not isinstance(audit_id, str) or not SAFE_ID.fullmatch(audit_id):
        return False
    if pointers is not None and audit_id in pointers:
        pointer = pointers[audit_id]
    else:
        try:
            pointer = strict_json(registry / audit_id / "current.json")
        except (OSError, ValueError):
            pointer = None
        if pointers is not None:
            pointers[audit_id] = pointer
    return (isinstance(pointer, dict) and pointer.get("version") == row.get("version")
            and pointer.get("pair_manifest_sha256") == row.get("pair_manifest_sha256"))


def _standing(registry: pathlib.Path, row: dict[str, Any], pointers: dict[str, Any]) -> int:
    """A row against this host's registry/<id>/current.json, which can lag the index files (Dropbox syncs file by
    file): 1 if current.json names it; 2 if it is a later filing whose versions/ folder has not reached this host,
    so current.json lags it; -1 if this host holds its versions/ folder and current.json names a filing stamped no
    earlier, so it was superseded; 0 if nothing here tells (a version stamp is a host's clock)."""
    if _is_current_filing(registry, row, pointers):
        return 1
    pointer, version = pointers.get(row.get("audit_id")), row.get("version")
    if (not isinstance(pointer, dict) or not isinstance(pointer.get("version"), str)
            or not isinstance(version, str) or not SAFE_ID.fullmatch(version)):
        return 0
    try:
        here = (registry / row["audit_id"] / "versions" / version).is_dir()
    except OSError:
        return 0
    if version > pointer["version"]:
        return 0 if here else 2
    return -1 if here else 0


def index_view(registry: pathlib.Path, sources: tuple[Any, Any, Any],
               filed: dict[str, Any] | None = None) -> bytes:
    """index.jsonl as a view of the registry rather than a file each filing reads, edits and rewrites.

    Filing used to rewrite index.jsonl from this host's copy, and the host-local lock cannot see
    another machine: a host whose Dropbox copy lagged wrote an index without rows other hosts had
    filed, and Dropbox kept one version. Eleven filed audits lost their row that way (2026-09-28 to
    10-18) and no longer passed `check`. The view is the union of index.jsonl, every audit's own
    index_row.json, and each conflicted copy's row that is still its audit's current filing (an
    audit removed from the registry is not brought back); one row per audit, its current filing
    (registry/<id>/current.json) ahead of any other, then the newest, and the audit being filed
    replaces its own row as before. The current filing comes first because a version stamp is a
    host's clock: a superseded row stamped by a host whose clock ran ahead sorts above the row
    that replaced it. This host's current.json can lag its index.jsonl, though, so a row for a later
    filing whose versions/ folder has not reached this host comes before it (`_standing`), and a
    conflicted copy, an older index, only fills a missing row or replaces one this host knows was
    superseded: otherwise a host that had index.jsonl but not yet current.json from a re-filing put
    the earlier filing's row back. Ordered by filing time, so any host holding the same rows writes
    the same bytes.
    """
    listed, own, conflicts = sources[:3]
    best: dict[str, tuple[tuple[int, str, str, int], dict[str, Any]]] = {}
    pointers: dict[str, Any] = {}

    def offer(row: dict[str, Any], rank: int) -> None:
        key = (max(_standing(registry, row, pointers), 0), str(row.get("version")), str(row.get("filed_at")), rank)
        held = best.get(row["audit_id"])
        if held is None or key > held[0]:
            best[row["audit_id"]] = (key, row)

    for row in listed:
        offer(row, 1)
    for row in own.values():
        offer(row, 2)
    for _, rows in conflicts:
        for row in rows:
            held = best.get(row["audit_id"])
            if _is_current_filing(registry, row, pointers) and (
                    held is None or _standing(registry, held[1], pointers) < 0):
                offer(row, 0)
    if filed is not None:
        best[filed["audit_id"]] = ((3, "", "", 3), filed)
    rows = sorted((row for _, row in best.values()),
                  key=lambda row: (str(row.get("filed_at")), str(row.get("version")), row["audit_id"]))
    return b"".join(index_line(row) for row in rows)


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
        lock_wait.acquire_exclusive(lock, "the registry lock")
        index_path = registry / "index.jsonl"
        # read (and refuse a malformed index) before anything is filed, as before; this audit's own row
        # file is not read, because this filing replaces it
        sources = index_sources(registry, skip=audit_id)
        _warn_broken(sources[3])

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
        row = index_row(run, comment, version=version, pair_manifest_sha256=pair_manifest_hash,
                        filed_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        # The audit's own row, one writer, is what `check` reads; index.jsonl is rebuilt from the rows.
        atomic_bytes(audit_dir / INDEX_ROW, index_line(row))
        atomic_bytes(index_path, index_view(registry, sources, filed=row))
        index_row_sha256 = index_row_hash(row)
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
    if run.get("schema_version") in CURRENT_RUN_SCHEMAS:
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


# The canonical journal is append-only, and `publish_completed.sh` journals the filing AFTER the
# receipt has been written and verified -- deliberately, so the run.json it files a moment earlier
# does not claim to contain its own filing record. The consequence went unnoticed until an audit
# was checked after publication: every one of these six projections changes when that row lands,
# so `check` failed on EVERY published audit, which is precisely where the receipt matters. Both
# audits published before this was found were stale.
#
# A later append does not alter what was finalized. These are therefore verified by PREFIX: find
# the journal position the receipt bound (by recomputing the watermark file it recorded, which
# commits to the whole chain up to that point), require that position's chain hash to still be
# present and intact, and require every appended row to be one a FINISHED audit may legitimately
# gain. Anything else -- a new round, another finalize, a metadata correction -- is a real
# staleness and still fails.
JOURNAL_PROJECTIONS = frozenset({
    "run.json", "invocations.jsonl", "invocations.watermark.json",
    "run.events.jsonl", "run_metrics.json", "RUN_METADATA.md",
})
POST_FINALIZATION_EVENTS = frozenset({"delivery", "note", "records", "package", "human_effort"})


def _watermark_bytes(events: list[dict[str, Any]]) -> bytes:
    """Byte-for-byte what lifecycle_journal writes to invocations.watermark.json."""
    return json.dumps(lifecycle_journal.watermark(events),
                      indent=2, sort_keys=True).encode("utf-8") + b"\n"


def journal_extension(root: pathlib.Path, bound: dict[str, dict[str, Any]]) -> list[str] | None:
    """Types of the rows appended since the receipt, or None if the receipt is genuinely stale."""
    row = bound.get("invocations.watermark.json")
    if not row:
        return None
    events = lifecycle_journal.load_events(root, allow_legacy_prefix=True)
    if not events:
        return None
    position = None
    for count in range(len(events), 0, -1):
        payload = _watermark_bytes(events[:count])
        if len(payload) == row["bytes"] and hashlib.sha256(payload).hexdigest() == row["sha256"]:
            position = count
            break
    if position is None:
        return None
    appended = events[position:]
    types = [str(event.get("type")) for event in appended]
    if any(kind not in POST_FINALIZATION_EVENTS for kind in types):
        return None
    return types


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
    mismatched: dict[str, dict[str, Any]] = {}
    for row in artifacts:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise ValueError("finalization receipt contains a malformed artifact row")
        relative = row["path"]
        if relative in seen:
            raise ValueError(f"duplicate finalization artifact: {relative}")
        seen.add(relative)
        path = contained(root, relative)
        if row["sha256"] != sha256(path) or row["bytes"] != path.stat().st_size:
            mismatched[relative] = row
    bound = {row["path"]: row for row in artifacts if isinstance(row, dict)}
    appended_types: list[str] = []
    stale = ""
    if mismatched:
        off_journal = sorted(set(mismatched) - JOURNAL_PROJECTIONS)
        if off_journal:
            raise ValueError(f"finalization receipt is stale for {off_journal[0]}")
        extension = journal_extension(root, bound)
        if extension is None:
            raise ValueError(
                "finalization receipt is stale for " + sorted(mismatched)[0]
                + ": the canonical journal changed in a way a finished audit cannot. Re-run "
                  "finalize_run.sh, or investigate what appended to run.events.jsonl.")
        appended_types = extension
    run = strict_json(root / "run.json")
    if mismatched:
        # The projections are newer than the receipt; they must still be the tip of the journal
        # they were projected from, or something wrote them by hand.
        validate_metadata_21_binding(root, run)
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
        # The filed pair is immutable and must always match its own manifest.
        if (pair.get("run_sha256") != sha256(version_dir / "run.json") or
                pair.get("comment_sha256") != sha256(version_dir / "comment.json")):
            raise ValueError("registry immutable pair does not match the copies it filed")
        # comment.json is derived from the findings, not from the journal, so it must still match
        # the filed copy whatever the journal has since recorded.
        if pair.get("comment_sha256") != sha256(root / "comment.json"):
            raise ValueError("registry immutable pair does not match current finalized metadata")
        # run.json is a journal projection. It legitimately moves on when the filing itself is
        # journaled -- see JOURNAL_PROJECTIONS above -- and the prefix check has already proved
        # the finalized history is intact underneath it.
        if not appended_types and pair.get("run_sha256") != sha256(root / "run.json"):
            raise ValueError("registry immutable pair does not match current finalized metadata")
        index_matches = []
        own_row = pointer_path.parent / INDEX_ROW
        legacy = ""
        if own_row.exists() or own_row.is_symlink():
            # Filed since 2.7.9 (or recovered): the audit's own row decides. index.jsonl is a view
            # that a host whose Dropbox copy lags can still write without this row.
            try:
                row = strict_json(own_row)
            except (OSError, ValueError) as exc:
                raise ValueError(f"{exc}{RECOVER_HINT}") from exc
            if not isinstance(row, dict) or row.get("audit_id") != run.get("audit_id"):
                raise ValueError(f"registry {INDEX_ROW} does not hold this audit's row{RECOVER_HINT}")
            if (row.get("version") == registry.get("version")
                    and row.get("pair_manifest_sha256") == registry.get("pair_manifest_sha256")):
                index_matches.append(row)
            else:
                # An earlier filing's row: a host on 2.7.8 or older re-filed the audit (it does not
                # write this file), or a sync conflict kept another filing's copy. index.jsonl decides,
                # as for a receipt filed before 2.7.9, and its row must still hash to the receipt.
                stale = f" (its {INDEX_ROW} is from an earlier filing{RECOVER_HINT})"
        else:
            legacy = (f" (filed before {INDEX_ROW} existed; `finalization_receipt.py registry-audit <tree>/registry` "
                      "says whether its row can be recovered)")
        if not index_matches:
            index_path = registry_root / "index.jsonl"
            for number, line in enumerate(index_path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                row = validate_json_schema.strict_loads(line, source=f"{index_path}:{number}")
                if isinstance(row, dict) and row.get("audit_id") == run.get("audit_id"):
                    index_matches.append(row)
            if len(index_matches) != 1:
                raise ValueError("registry index does not contain exactly one row for this audit" + (stale or legacy))
        row_hash = index_row_hash(index_matches[0])
        if row_hash != registry.get("index_row_sha256"):
            raise ValueError("registry index row no longer matches the finalization receipt" + (stale or legacy))
    if require_publication:
        if run.get("schema_version") != "2.1.0":
            raise ValueError("new publication requires metadata schema 2.1.0")
        validate_metadata_21_binding(root, run)
        if (run.get("status") != "complete" or not (run.get("compliance") or {}).get("compliance_pass")
                or (run.get("token_accounting") or {}).get("coverage_fraction") != 1):
            violations = (run.get("compliance") or {}).get("violations") or []
            coverage = (run.get("token_accounting") or {}).get("coverage_fraction")
            raise ValueError(
                "finalized run is not publication-compliant with 100% telemetry "
                f"(status {run.get('status')!r}, token coverage {coverage!r})"
                + "".join(f"\n  VIOLATION: {v}" for v in violations)
                + ("\n  Fix each, then rerun finalize_run.sh." if violations else ""))
        document, document_paths = document_binding(root, required=True)
        if not set(document_paths) <= seen:
            raise ValueError("document build happened after finalization; finalize again")
        final_pdfs = [path for path in (root / "report").iterdir()
                      if path.suffix.lower() == ".pdf"
                      and path.is_file() and not path.is_symlink()
                      and "_proxy" not in path.name.lower() and "preview" not in path.name.lower()]
        expected_pdf = contained(root, document["artifacts"]["pdf"]["path"])
        if len(final_pdfs) != 1 or final_pdfs[0].resolve() != expected_pdf:
            raise ValueError("the sole publishable PDF is not the report-bound document build")
        claims = [path for path in seen if pathlib.PurePosixPath(path).name.startswith("claims_script.")]
        if len(claims) != 1:
            raise ValueError("receipt does not bind exactly one claims script")
        if run.get("schema_version") in CURRENT_RUN_SCHEMAS:
            validate_projection_claim_binding(root, claims[0])
    if appended_types:
        # Not a hidden pass: say what the journal gained, so an operator sees it.
        receipt = dict(receipt)
        receipt["journal_appended"] = appended_types
    if stale:
        # Nor this one: the audit's own row file is an earlier filing's, and index.jsonl's row was checked.
        receipt = dict(receipt)
        receipt["index_row_stale"] = True
    return receipt


def registry_folder(path: pathlib.Path) -> pathlib.Path:
    """The registry the operator commands work on: <tree>/registry, or the tree root it sits in (status.py and
    publish take the tree root). Anything else is refused before a lock file is opened in it: given another
    folder, registry-audit reported "0 filed audit(s)" and registry-recover left a .registry.lock behind."""
    resolved = path.resolve()
    for candidate in (resolved, resolved / "registry"):
        if candidate.is_dir() and (candidate.name == "registry" or (candidate / "index.jsonl").is_file()
                                   or any(candidate.glob("*/current.json"))):
            return candidate
    raise ValueError(f"{path} is not a registry folder (no index.jsonl, no <audit_id>/current.json); "
                     "pass <tree>/registry")


def registry_audit(registry: pathlib.Path) -> dict[str, Any]:
    """Read-only: filed audits that no current index row holds, those a conflicted copy can give back, and
    audits' own row files that are stale (an earlier filing's) or cannot be used. A filed audit whose current.json
    cannot be used is listed as that alone: without it no row can be said to hold the audit, and registry-recover
    refuses it."""
    registry = registry_folder(registry)
    listed, own, conflicts, broken = index_sources(registry)
    pointers: dict[str, Any] = {}
    in_index = {row["audit_id"] for row in listed if _is_current_filing(registry, row, pointers)}
    filed = sorted(path.name for path in registry.iterdir()
                   if path.is_dir() and SAFE_ID.fullmatch(path.name) and (path / "current.json").is_file())
    report: dict[str, Any] = {
        "filed": len(filed), "index_rows": len(listed), "own_rows": len(own),
        "conflicted_copies": [name for name, _ in conflicts], "missing": {}, "lagging": [],
        "stale": {}, "broken": broken, "unusable": {},
    }
    for audit_id in filed:
        problem = _pointer_problem(registry / audit_id / "current.json")
        if problem is not None:
            report["unusable"][audit_id] = problem
            broken.pop(audit_id, None)
            continue
        if audit_id in own and not _is_current_filing(registry, own[audit_id], pointers):
            report["stale"][audit_id] = audit_id in in_index
            continue
        if audit_id in own:
            if audit_id not in in_index:
                report["lagging"].append(audit_id)
            continue
        if audit_id in in_index or audit_id in broken:
            continue
        report["missing"][audit_id] = next(
            (name for name, rows in conflicts for row in rows
             if row["audit_id"] == audit_id and _is_current_filing(registry, row, pointers)), None)
    return report


def _rebuilt_rows(audit_dir: pathlib.Path, pointer: dict[str, Any]) -> list[dict[str, Any]]:
    """The row the filing wrote, rebuilt from the immutable pair it filed. filed_at is the one value
    not stored there: it is the clock a moment after the version stamp, so every second of the
    following minute is offered, and only the receipt's hash can pick one."""
    version = pointer.get("version")
    version_dir = audit_dir / "versions" / str(version)
    try:
        stamp = dt.datetime.strptime(str(version)[:15], "%Y%m%dT%H%M%S")
        run, comment = strict_json(version_dir / "run.json"), strict_json(version_dir / "comment.json")
    except (OSError, ValueError):
        return []
    if not isinstance(run, dict) or not isinstance(comment, dict):
        return []
    return [index_row(run, comment, version=version,
                      pair_manifest_sha256=pointer.get("pair_manifest_sha256"),
                      filed_at=(stamp + dt.timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            for offset in range(60)]


def registry_recover(registry: pathlib.Path, receipt_path: pathlib.Path) -> str:
    """Write a filed audit's missing index_row.json, only from a row that hashes to its receipt.

    The candidates are the rows index.jsonl and the conflicted copies hold for the audit, and the row
    rebuilt from its filed pair. None is trusted: the receipt's index_row_sha256 binds every field, so
    nothing that was not filed can pass. The receipt must be the audit's current filing. A held
    index_row.json is replaced only when it is stale (not the current filing: a host on 2.7.8 or older
    re-filed the audit, or a sync conflict kept another filing's copy) or cannot be used; one that
    claims the current filing and differs is left for a person to look at.
    """
    registry = registry_folder(registry)
    if receipt_path.is_dir():
        receipt_path = receipt_path / RECEIPT
    receipt = strict_json(receipt_path)
    binding = receipt.get("registry") if isinstance(receipt, dict) else None
    audit_id = receipt.get("audit_id") if isinstance(receipt, dict) else None
    if (not isinstance(binding, dict) or binding.get("filed") is not True
            or not isinstance(audit_id, str) or not SAFE_ID.fullmatch(audit_id)
            or not re.fullmatch(r"[0-9a-f]{64}", str(binding.get("index_row_sha256")))):
        raise ValueError(f"{receipt_path.name} records no registry filing")
    audit_dir = registry / audit_id
    wanted = binding["index_row_sha256"]
    with (registry / ".registry.lock").open("a+") as lock:
        lock_wait.acquire_exclusive(lock, "the registry lock")
        pointer = strict_json(audit_dir / "current.json")
        if (not isinstance(pointer, dict) or pointer.get("version") != binding.get("version")
                or pointer.get("pair_manifest_sha256") != binding.get("pair_manifest_sha256")):
            raise ValueError(f"{audit_id}: the receipt is not the audit's current registry filing; nothing written")
        row_path = audit_dir / INDEX_ROW
        replaced = ""
        if row_path.exists() or row_path.is_symlink():
            held, problem = _own_row(row_path)
            if held is not None and index_row_hash(held) == wanted:
                return f"{audit_id}: {INDEX_ROW} already holds the receipt's row"
            if held is not None and _is_current_filing(registry, held):
                raise ValueError(f"{audit_id}: {INDEX_ROW} exists and differs from the receipt; not overwritten")
            replaced = (f" (replaced one that {problem})" if held is None else
                        f" (replaced a stale one, from filing {held.get('version')})")
        sources = index_sources(registry, skip=audit_id)
        _warn_broken(sources[3])
        listed, _, conflicts, _ = sources
        candidates = [("index.jsonl", row) for row in listed if row["audit_id"] == audit_id]
        candidates += [(name, row) for name, rows in conflicts for row in rows if row["audit_id"] == audit_id]
        candidates += [(f"versions/{pointer['version']}", row) for row in _rebuilt_rows(audit_dir, pointer)]
        for source, row in candidates:
            if index_row_hash(row) == wanted:
                atomic_bytes(row_path, index_line(row))
                atomic_bytes(registry / "index.jsonl", index_view(registry, sources, filed=row))
                return f"{audit_id}: recovered {INDEX_ROW} from {source}{replaced}"
        raise ValueError(f"{audit_id}: no candidate row hashes to the receipt's index_row_sha256; nothing written")


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
    audit_parser = sub.add_parser(
        "registry-audit", help="read-only: filed audits that no index row holds, stale or unusable index_row.json, "
                               "and unusable current.json")
    audit_parser.add_argument("registry", type=pathlib.Path)
    recover_parser = sub.add_parser(
        "registry-recover",
        help="write a filed audit's missing, stale or unusable index_row.json from a row its receipt hashes to")
    recover_parser.add_argument("registry", type=pathlib.Path)
    recover_parser.add_argument("receipts", type=pathlib.Path, nargs="+",
                                help="an audit directory, or a finalization_receipt.json (e.g. metadata/<J>/<P>/)")
    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create(args.audit_dir, args.registry)
            print(f"finalization receipt created for {result['audit_id']}")
        elif args.command == "registry-audit":
            report = registry_audit(args.registry)
            print(f"registry: {report['filed']} filed audit(s), {report['index_rows']} row(s) in index.jsonl, "
                  f"{report['own_rows']} {INDEX_ROW}, {len(report['conflicted_copies'])} conflicted "
                  "copies of the index")
            recover = ("finalization_receipt.py registry-recover <registry> <its audit dir or "
                       "finalization_receipt.json>")
            for audit_id, copy in report["missing"].items():
                where = (f"its current row is in '{copy}'" if copy else
                         "no index file holds it; registry-recover also rebuilds it from versions/")
                print(f"  MISSING {audit_id}: no index row holds its current filing ({where}). "
                      f"Recover with: {recover}")
            for audit_id, held in report["stale"].items():
                meanwhile = ("index.jsonl holds its current row, which `check` uses meanwhile" if held else
                             "index.jsonl does not hold its current row either, so `check` refuses it")
                print(f"  STALE {audit_id}: {INDEX_ROW} is from an earlier filing (re-filed by a host on 2.7.8 or "
                      f"older, or a sync conflict kept another filing's copy); {meanwhile}. Replace it with: {recover}")
            for audit_id, problem in sorted(report["broken"].items()):
                print(f"  BROKEN {audit_id}: {INDEX_ROW} {problem}; filings leave it out and `check` of that audit "
                      f"refuses. Replace it with: {recover}")
            for audit_id, problem in report["unusable"].items():
                print(f"  UNUSABLE {audit_id}: current.json {problem}" + (
                    ": check follows one that stays inside the registry, but registry-audit and registry-recover "
                    "refuse it; replace the link with a copy of its target" if problem == "is a symlink" else
                    "; check, registry-audit and registry-recover all need it: let Dropbox finish syncing it, or "
                    "restore it from the folder's version history"))
            for audit_id in report["lagging"]:
                print(f"  lagging {audit_id}: {INDEX_ROW} holds its current row, index.jsonl not yet (the next filing "
                      "rewrites the view)")
            return 1 if report["missing"] or report["stale"] or report["broken"] or report["unusable"] else 0
        elif args.command == "registry-recover":
            failed = 0
            for receipt in args.receipts:
                try:
                    print(registry_recover(args.registry, receipt))
                except (OSError, ValueError) as exc:
                    print(f"REFUSED: {exc}", file=sys.stderr)
                    failed = 1
            return failed
        else:
            result = check(args.audit_dir, require_registry=args.require_registry,
                           require_publication=args.require_publication)
            appended = result.get("journal_appended")
            if appended:
                print(f"finalization receipt valid for {result['audit_id']} "
                      f"(journal extended after finalization by {len(appended)} row(s): "
                      f"{', '.join(sorted(set(appended)))})")
            else:
                print(f"finalization receipt valid for {result['audit_id']}")
            if result.get("index_row_stale"):
                print(f"NOTE: registry/{result['audit_id']}/{INDEX_ROW} is from an earlier filing, so the row in "
                      f"index.jsonl was checked{RECOVER_HINT}", file=sys.stderr)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
