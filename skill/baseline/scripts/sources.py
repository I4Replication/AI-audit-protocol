"""The full-pipeline audits a baseline run can be compared with, read from the tree's records only.

A source is a published audit: metadata/<journal>/<paper>/ (run.json, comment.json) plus its working folder
runs/<journal>/<paper>/audit, whose Phase-1 snapshot (phase1/snapshots/<sha>/, bound by phase1/quorum.json) holds the
exact paper, deposit and deterministic indexes the five Phase-1 auditors saw. The baseline is given those same bytes.
"""
from __future__ import annotations

import collections
import json
import pathlib
from typing import Any

from baseline_common import BaselineError, sha256_file

NAMES = {"sonnet-5-5": "Sonnet 5.5", "sonnet-5": "Sonnet 5", "gpt-6.1-sol": "GPT-6.1 Sol",
         "gpt-5.6-terra": "GPT-5.6 Terra", "gpt-6-sol": "GPT-6 Sol"}


def _norm(model: Any) -> str | None:
    return NAMES.get(str(model).lower().replace("claude-", ""), str(model)) if model else None


def _route(invocations: list[dict], pred) -> tuple[str | None, str | None]:
    models, efforts = collections.Counter(), collections.Counter()
    for inv in invocations:
        if pred(inv.get("role") or "") and inv.get("status") == "completed":
            m = inv.get("observed_model") or inv.get("served_model") or inv.get("requested_model")
            if m:
                models[_norm(m)] += 1
            if inv.get("reasoning_effort"):
                efforts[inv["reasoning_effort"]] += 1
    return (models.most_common(1)[0][0] if models else None), (efforts.most_common(1)[0][0] if efforts else None)


def runs_index(tree: pathlib.Path) -> dict[str, pathlib.Path]:
    """audit_id -> runs/<journal>/<paper>/audit (published audits keep their working folder there)."""
    index: dict[str, pathlib.Path] = {}
    for run_json in sorted((tree / "runs").glob("*/*/audit/run.json")):
        try:
            audit_id = json.loads(run_json.read_text()).get("audit_id")
        except (OSError, json.JSONDecodeError):
            continue
        if audit_id:
            index[audit_id] = run_json.parent
    return index


def published(tree: pathlib.Path) -> list[dict[str, Any]]:
    runs = runs_index(tree)
    out = []
    for meta in sorted((tree / "metadata").glob("*/*")):
        if meta.name.startswith(".") or not (meta / "run.json").is_file() or not (meta / "comment.json").is_file():
            continue
        run = json.loads((meta / "run.json").read_text())
        comment = json.loads((meta / "comment.json").read_text())
        inv = run.get("invocations") or []
        claude, effort = _route(inv, lambda r: r.startswith("auditor_") or r == "coordinator")
        critic, _ = _route(inv, lambda r: r.startswith("critic_") and r != "critic_synth")
        audit = runs.get(run.get("audit_id"))
        snapshot = None
        if audit and (audit / "phase1" / "quorum.json").is_file():
            sha = json.loads((audit / "phase1" / "quorum.json").read_text()).get("source_snapshot_sha256")
            if sha and (audit / "phase1" / "snapshots" / sha / "snapshot_manifest.json").is_file():
                snapshot = sha
        out.append({
            "audit_id": run.get("audit_id"), "journal": meta.parent.name,
            "metadata_folder": meta.name, "runs_folder": audit.parent.name if audit else None,
            "protocol_version": run.get("protocol_version"), "filed": (run.get("ended_at") or "")[:10],
            "claude_model": claude, "claude_effort": effort, "critic_model": critic,
            "code_present": bool((comment.get("package") or {}).get("code_present")),
            "snapshot_sha256": snapshot,
        })
    return out


def source_dir(tree: pathlib.Path, paper: dict[str, Any]) -> pathlib.Path:
    audit = tree / "runs" / paper["journal"] / paper["runs_folder"] / "audit"
    if not (audit / "run.json").is_file() or json.loads((audit / "run.json").read_text()).get("audit_id") != paper["audit_id"]:
        raise BaselineError(f"{paper['audit_id']}: runs/{paper['journal']}/{paper['runs_folder']}/audit is not that audit")
    return audit


def verified_snapshot(audit: pathlib.Path, sha: str) -> tuple[pathlib.Path, dict[str, Any]]:
    """The Phase-1 snapshot, after checking every deposited file and index against its manifest."""
    snap = audit / "phase1" / "snapshots" / sha
    manifest = json.loads((snap / "snapshot_manifest.json").read_text())
    if manifest.get("snapshot_sha256") != sha:
        raise BaselineError(f"snapshot {sha[:12]}: its manifest names another snapshot")
    for row in manifest["source_files"]:
        path = snap / "raw" / row["path"]
        if not path.is_file() or sha256_file(path) != row["sha256"]:
            raise BaselineError(f"snapshot {sha[:12]}: raw/{row['path']} does not match its manifest")
    for row in manifest["common_evidence"]["artifacts"]:
        path = snap / "evidence" / row["path"]
        if not path.is_file() or sha256_file(path) != row["sha256"]:
            raise BaselineError(f"snapshot {sha[:12]}: evidence/{row['path']} does not match its manifest")
    return snap, manifest
