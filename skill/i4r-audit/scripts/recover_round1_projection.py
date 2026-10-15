#!/usr/bin/env python3
"""Publish Round 1 from the original bound packet and validated adjudication cache.

The normal cache-only replay cannot reuse the content-addressed target snapshot because its
existing-snapshot check mistakenly binds a new invocation-specific full-packet hash.  This recovery
does not call a provider or alter any scientific payload.  It reuses the original packet that the
target snapshot names, validates every critic/adjudicator artifact and invocation join through the
normal projection primitive, then installs the normal immutable projection and terminal status.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import sys
import tempfile
import uuid
from types import SimpleNamespace
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import retry_completeness_once as common  # type: ignore
import test_run_critique_runtime as runtime  # type: ignore


AUDIT_ID = "20260101-audit-00000001"
ROUND = 1
ROUND_INPUT_SHA = "3f8efbc5a7c44c4cc4ecfcb9263f8374e872fa0c7a7c29b1971ce7db3f240aa5"
REPORT_SHA = "fb543c06a4b7430e921c3533ca014772714ee17785ce3b6c294819c1d50834ca"
SOURCE_INVOCATION = "20260101T000002Z.r1.00002.00002"
FAILED_REPLAY_INVOCATION = "20260101T000003Z.r1.00003.00003"
TARGET_SHA = "22ce2effd6022a88224c45a06e057364e4624ac509feabadc29e1abe1e25035e"
ROLES = ["stats", "data", "fairness", "completeness"]
EXPECTED = {
    "current_status": "26d21068d66bb72c8fd0237d3af2292662fe71bde2de395f6fce52ce17fe27ae",
    "claim_merge": "9989e5cb473eb665da2314e1dd729b6a54d4ed1a6ba656815e0d317ca3ca4914",
    "packet": "481d8dc457e0e9672737de51ab3cb3a4d5bd4eb0b3d412193c45e78fab895b3a",
    "routing": "6605567245dff4234422a68feddd122bd5e065bcfa8982673864fe9278eca9fa",
    "target_manifest": "d2a3211c305d3d7002a58560b99746c849d1398afe69d6b6817d3a7006f86b02",
    "target_json": "c143ff81b7e3632f7af8fbb59d1a221f87641de1630a67082be85313fdf5a675",
    "target_md": "6bd37c45a1ca02a0497b1dd0b94c53320a0a718f9b06fa592553e2ef5a1846c5",
    "target_complete": "53fea0652d77b221090b36d9a0f2b1dad070f4bf34ff5704ed5e531541cd1088",
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_copy(source: pathlib.Path, target: pathlib.Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    shutil.copyfile(source, temporary)
    with temporary.open("rb+") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def validate_preconditions(audit: pathlib.Path) -> dict[str, Any]:
    round_dir = audit / "critique/round_1"
    source = round_dir / "invocations" / SOURCE_INVOCATION
    target = round_dir / "target_snapshots" / TARGET_SHA
    cache = round_dir / "adjudication_cache" / TARGET_SHA
    paths = {
        "current_status": round_dir / "ROUND_STATUS.json",
        "claim_merge": source / "CLAIM_MERGE.json",
        "packet": source / "adjudication_packet.json",
        "routing": source / "ROUTING.json",
        "target_manifest": target / "MANIFEST.json",
        "target_json": cache / "output.json",
        "target_md": cache / "output.md",
        "target_complete": cache / "complete.json",
    }
    for name, path in paths.items():
        if common.sha(path) != EXPECTED[name]:
            raise ValueError(f"projection recovery prerequisite changed: {name}")
    if (round_dir / "CURRENT_INVOCATION").read_text(encoding="utf-8").strip() \
            != FAILED_REPLAY_INVOCATION:
        raise ValueError("current invocation is not the reviewed cache-replay failure")
    status = common.load(paths["current_status"])
    if status.get("invocation_id") != FAILED_REPLAY_INVOCATION \
            or status.get("outcome") != "failed" or status.get("quorum_met") is not True \
            or status.get("provider_calls") != 0 \
            or status.get("detail") != "immutable targeted-adjudication snapshot failed":
        raise ValueError("terminal state is not the reviewed projection-only failure")
    if (round_dir / "CURRENT_PROJECTION").exists() \
            or (round_dir / "projections").exists() and any((round_dir / "projections").iterdir()):
        raise ValueError("an immutable Round-1 projection already exists")

    packet = common.load(paths["packet"])
    if packet.get("audit_id") != AUDIT_ID or packet.get("round") != ROUND \
            or packet.get("input_sha256") != ROUND_INPUT_SHA \
            or packet.get("report_sha256") != REPORT_SHA \
            or packet.get("claim_merge_sha256") != EXPECTED["claim_merge"] \
            or len(packet.get("triggers", [])) != 5:
        raise ValueError("original adjudication packet identity changed")
    routing = common.load(paths["routing"])
    if routing.get("roles") != ROLES or routing.get("round") != ROUND \
            or routing.get("claim_assignment_mode") != "unscoped_round_1":
        raise ValueError("original Round-1 routing changed")

    round_snapshot = round_dir / "snapshots" / ROUND_INPUT_SHA
    round_manifest = common.load(round_snapshot / "MANIFEST.json")
    runtime.verify_read_only_inventory(round_snapshot, round_manifest)
    target_manifest = common.load(paths["target_manifest"])
    runtime.verify_read_only_inventory(target, target_manifest)
    if target_manifest.get("packet_sha256") != EXPECTED["packet"] \
            or target_manifest.get("target_input_sha256") != TARGET_SHA:
        raise ValueError("target snapshot is not bound to the original full packet")

    source_results = source / "results"
    for role in ROLES:
        runtime.validate_completion(
            source_results / f"{role}.complete.json",
            source_results / f"critic_{role}.json",
            source_results / f"critic_{role}.md",
            audit_id=AUDIT_ID, input_sha=ROUND_INPUT_SHA, report_sha=REPORT_SHA,
            round_number=ROUND, role=role,
        )
    runtime.validate_completion(
        paths["target_complete"], paths["target_json"], paths["target_md"],
        audit_id=AUDIT_ID, input_sha=TARGET_SHA, report_sha=REPORT_SHA,
        round_number=ROUND, role="targeted_adjudicator",
    )
    with tempfile.TemporaryDirectory(prefix="i4r-projection-validation-") as temporary:
        rendered = pathlib.Path(temporary) / "rendered.md"
        runtime.command_validate_adjudication(SimpleNamespace(
            input=str(paths["target_json"]), packet=str(paths["packet"]),
            input_sha=TARGET_SHA, schema="", markdown=str(rendered),
        ))
        if rendered.read_bytes() != paths["target_md"].read_bytes():
            raise ValueError("target cache Markdown is not the canonical rendering")
    return {
        "round_dir": round_dir, "source": source, "source_results": source_results,
        "target": target, "cache": cache, "paths": paths,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    audit = pathlib.Path(args.audit_dir).resolve()
    if audit.is_symlink() or not audit.is_dir():
        raise ValueError("audit directory is missing or symlinked")
    state = validate_preconditions(audit)
    preview = {
        "recovery_version": "i4r-round1-projection-recovery-1", "dry_run": args.dry_run,
        "audit_id": AUDIT_ID, "round": ROUND, "input_sha256": ROUND_INPUT_SHA,
        "report_sha256": REPORT_SHA, "source_invocation": SOURCE_INVOCATION,
        "failed_replay_invocation": FAILED_REPLAY_INVOCATION,
        "target_input_sha256": TARGET_SHA, "provider_calls": 0,
    }
    if args.dry_run:
        return preview

    common.acquire_round_lock(state["round_dir"])
    started = now()
    invocation_id = (
        f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        f".r1.projection.recovery.{uuid.uuid4().hex[:8]}"
    )
    invocation = state["round_dir"] / "invocations" / invocation_id
    results = invocation / "results"
    results.mkdir(parents=True, exist_ok=False)
    for role in ROLES:
        for name in (f"critic_{role}.json", f"critic_{role}.md", f"{role}.complete.json"):
            shutil.copyfile(state["source_results"] / name, results / name)
    shutil.copyfile(state["paths"]["target_json"], results / "TARGETED_ADJUDICATION.json")
    shutil.copyfile(state["paths"]["target_md"], results / "TARGETED_ADJUDICATION.md")
    shutil.copyfile(
        state["paths"]["target_complete"], results / "targeted_adjudicator.complete.json")
    for name in ("CLAIM_MERGE.json", "adjudication_packet.json", "ROUTING.json"):
        shutil.copyfile(state["source"] / name, invocation / name)

    runtime.command_projection(SimpleNamespace(
        out=str(state["round_dir"]), results=str(results), invocation=invocation_id,
        input_sha=ROUND_INPUT_SHA, report_sha=REPORT_SHA, audit_id=AUDIT_ID,
        roles=" ".join(ROLES), adjudication="complete",
        routing=str(invocation / "ROUTING.json"),
        packet=str(invocation / "adjudication_packet.json"), round=ROUND,
        target_input_sha=TARGET_SHA,
    ))
    projection = state["round_dir"] / "projections" / invocation_id
    manifest = common.load(projection / "MANIFEST.json")
    if manifest.get("invocation_id") != invocation_id \
            or manifest.get("roles") != ROLES or manifest.get("adjudication") != "complete" \
            or manifest.get("target_input_sha256") != TARGET_SHA:
        raise ValueError("normal projection primitive returned an invalid identity")

    stale = invocation / "stale_projections"
    stale.mkdir()
    root_names = [
        *(f"critic_{role}.{suffix}" for role in ROLES for suffix in ("json", "md")),
        "CRITIQUE.json", "CRITIQUE.md", "CURRENT_ROUTING.json",
        "CURRENT_ADJUDICATION_PACKET.json", "adjudication_packet.json",
    ]
    for name in root_names:
        current = state["round_dir"] / name
        if current.exists() or current.is_symlink():
            os.replace(current, stale / name)
    for role in ROLES:
        for suffix in ("json", "md"):
            name = f"critic_{role}.{suffix}"
            atomic_copy(projection / name, state["round_dir"] / name)
    atomic_copy(projection / "CRITIQUE.json", state["round_dir"] / "CRITIQUE.json")
    atomic_copy(projection / "CRITIQUE.md", state["round_dir"] / "CRITIQUE.md")
    atomic_copy(projection / "ROUTING.json", state["round_dir"] / "CURRENT_ROUTING.json")
    for name in ("CURRENT_ADJUDICATION_PACKET.json", "adjudication_packet.json"):
        atomic_copy(projection / "adjudication_packet.json", state["round_dir"] / name)
    common.atomic_text(state["round_dir"] / "CURRENT_PROJECTION", invocation_id + "\n")
    common.atomic_text(state["round_dir"] / "CURRENT_INVOCATION", invocation_id + "\n")

    ended = now()
    status = {
        "status_version": "i4r-round-status-2", "audit_id": AUDIT_ID,
        "invocation_id": invocation_id, "pid": os.getpid(), "round": ROUND,
        "outcome": "complete", "exit_code": 0,
        "detail": "exact role quorum met; routine synthesis omitted; adjudication=complete",
        "started_at": started, "ended_at": ended, "input_sha256": ROUND_INPUT_SHA,
        "report_sha256": REPORT_SHA,
        "snapshot": f"critique/round_1/snapshots/{ROUND_INPUT_SHA}",
        "expected_roles": ROLES, "completed_roles": ROLES, "failed_roles": [],
        "quorum_met": True, "provider_calls": 0, "cache_hits": 5,
        "adjudication": "complete", "adjudication_trigger_count": 5,
    }
    common.atomic_json(invocation / "ROUND_STATUS.json", status)
    common.atomic_json(state["round_dir"] / "ROUND_STATUS.json", status)
    receipt = {
        **preview, "dry_run": False, "invocation_id": invocation_id,
        "started_at": started, "ended_at": ended,
        "projection": projection.relative_to(audit).as_posix(),
        "projection_manifest_sha256": common.sha(projection / "MANIFEST.json"),
        "round_status_sha256": common.sha(state["round_dir"] / "ROUND_STATUS.json"),
        "target_result_sha256": common.sha(state["paths"]["target_json"]),
        "provider_calls": 0,
    }
    common.atomic_json(invocation / "RECOVERY_RECEIPT.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_dir")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as exc:
        print(f"Round-1 projection recovery refused: {exc}", file=sys.stderr)
        return 1
    finally:
        common.release_round_lock()
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
