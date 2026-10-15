#!/usr/bin/env python3
"""Retry the exact failed Round-1 targeted adjudication once with a compatible schema.

The first Terra/max adjudicator call was rejected before inference because the provider does not
accept an array-valued JSON Schema ``const``.  This run-specific recovery preserves the immutable
critic packet and target snapshot, replaces only that composite const with an equivalent bounded
array contract, validates the full response, records telemetry, and atomically publishes the normal
targeted-adjudication cache.  It may claim only attempt 2 of the round-wide three-attempt budget.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import uuid
from types import SimpleNamespace
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import retry_completeness_once as common  # type: ignore
import test_run_critique_runtime as runtime  # type: ignore


ROUND = 1
MODEL = "gpt-5.6-terra"
EFFORT = "max"
AUDIT_ID = "20260101-audit-00000001"
ROUND_INPUT_SHA = "3f8efbc5a7c44c4cc4ecfcb9263f8374e872fa0c7a7c29b1971ce7db3f240aa5"
REPORT_SHA = "fb543c06a4b7430e921c3533ca014772714ee17785ce3b6c294819c1d50834ca"
FAILED_INVOCATION = "20260101T000002Z.r1.00002.00002"
TARGET_SHA = "22ce2effd6022a88224c45a06e057364e4624ac509feabadc29e1abe1e25035e"
ROW_IDS = ["P1", "P2", "P20", "P5", "P6"]
EXPECTED_HASHES = {
    "ROUND_STATUS.json": "36570906b481bdda10a98aad14ab312a51a40967bdefa490ec6c716b0100038b",
    "CLAIM_MERGE.json": "9989e5cb473eb665da2314e1dd729b6a54d4ed1a6ba656815e0d317ca3ca4914",
    "adjudication_packet.json": "481d8dc457e0e9672737de51ab3cb3a4d5bd4eb0b3d412193c45e78fab895b3a",
    "target_snapshot.json": "f556cdca4f7423bc37521f084ee3be5201bdae960cdc7546b4374038ab179696",
    "attempt1.events.jsonl": "a345ea00d351f256124e4ef0baa9bf85efcad7b7ec0660691178ee851176dfe2",
    "attempt1.schema.json": "22d3b03a62156523a6e55f5084b5ebfcf06da2dd25e8b348300d8ad32f331c21",
    "failed_invocations.jsonl": "e62c74fbccec6ce5e9c28f53f29247b6782c909628173abaa4545b8a4e91b461",
    "permanent": "ecb4103d63c7def70af2b9878396240c44195c7bfbc4a6a3bb26fb8b2dc39ddf",
    "target.MANIFEST.json": "d2a3211c305d3d7002a58560b99746c849d1398afe69d6b6817d3a7006f86b02",
    "target.output.schema.json": "f788e4e1d17bc223da7a9270f64ff34aa7565cad40dc0e76f0428c5ee82c8057",
    "target.prompt.txt": "ba6c3fdedc7f09c2f51caf82632fd0f60d6fba9a273a6e04db977365f2db7633",
    "target.packet.json": "6894e8d4d49237517507b8bdc3e05491c7c7f678e65ce86a8443852f0c803fdb",
    "target.evidence.json": "036e6295d67be1b4790852ec110f7904bc0a55defcdd8b2d8a3d6c2e75deb8f4",
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def exact_array(values: list[str], base: dict[str, Any]) -> dict[str, Any]:
    schema = dict(base)
    schema.pop("const", None)
    schema["type"] = "array"
    schema["minItems"] = len(values)
    schema["maxItems"] = len(values)
    item_schema = dict(schema.get("items") or {})
    item_schema.pop("const", None)
    item_schema["type"] = "string"
    item_schema["enum"] = values
    schema["items"] = item_schema
    return schema


def constrain_bindings(schema: dict[str, Any], target: pathlib.Path) -> None:
    inventory = common.load(target / "source" / "EVIDENCE_HASHES.json")
    if not isinstance(inventory, dict) or not inventory:
        raise ValueError("target evidence inventory is missing or empty")
    paths: list[str] = []
    hashes: list[str] = []
    for relative, row in sorted(inventory.items()):
        if not isinstance(relative, str) or not isinstance(row, dict) \
                or set(row) != {"sha256", "size"}:
            raise ValueError("target evidence inventory is malformed")
        digest = row.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("target evidence inventory has an invalid digest")
        paths.append(relative)
        hashes.append(digest)
    binding = schema["$defs"]["artifact_binding"]["properties"]
    binding["path"]["enum"] = paths
    binding["sha256"]["enum"] = sorted(set(hashes))


def narrow_schema(base: pathlib.Path, call_id: str, target: pathlib.Path) -> dict[str, Any]:
    schema = common.load(base)
    properties = schema["properties"]
    properties["covered_claim_ids"] = exact_array(ROW_IDS, properties["covered_claim_ids"])
    properties["invocation_id"] = runtime.json_const_schema(
        call_id, properties["invocation_id"])
    coverage = properties["coverage"]["properties"]
    for name in ("assigned_checks", "completed_checks"):
        coverage[name] = exact_array(ROW_IDS, coverage[name])
    coverage["skipped_checks"]["maxItems"] = 0
    properties["items"]["minItems"] = len(ROW_IDS)
    properties["items"]["maxItems"] = len(ROW_IDS)
    properties["new_issues"]["maxItems"] = 0
    assessment = schema["$defs"]["assessment"]["properties"]
    assessment["item_id"] = {"type": "string", "enum": ROW_IDS}
    assessment["finding_id"] = {"type": "string", "enum": ROW_IDS}
    constrain_bindings(schema, target)
    common.reject_composite_consts(schema)
    runtime.validate_provider_schema(schema)
    return schema


def validate_preconditions(audit: pathlib.Path) -> dict[str, Any]:
    round_dir = audit / "critique" / "round_1"
    failed = round_dir / "invocations" / FAILED_INVOCATION
    target = round_dir / "target_snapshots" / TARGET_SHA
    paths = {
        "ROUND_STATUS.json": round_dir / "ROUND_STATUS.json",
        "CLAIM_MERGE.json": failed / "CLAIM_MERGE.json",
        "adjudication_packet.json": failed / "adjudication_packet.json",
        "target_snapshot.json": failed / "target_snapshot.json",
        "attempt1.events.jsonl": failed / "attempts/targeted_adjudicator/1/events.jsonl",
        "attempt1.schema.json": failed / "attempts/targeted_adjudicator/1/output.schema.json",
        "failed_invocations.jsonl": failed / "invocations.jsonl",
        "permanent": round_dir / "attempt_budget/targeted_adjudicator.permanent",
        "target.MANIFEST.json": target / "MANIFEST.json",
        "target.output.schema.json": target / "output.schema.json",
        "target.prompt.txt": target / "prompt.txt",
        "target.packet.json": target / "source/adjudication_packet.json",
        "target.evidence.json": target / "source/EVIDENCE_HASHES.json",
    }
    for name, path in paths.items():
        if common.sha(path) != EXPECTED_HASHES[name]:
            raise ValueError(f"immutable prerequisite changed: {name}")

    status = common.load(paths["ROUND_STATUS.json"])
    if status.get("outcome") != "failed" or status.get("adjudication") != "failed" \
            or status.get("quorum_met") is not True or status.get("failed_roles") != [] \
            or status.get("invocation_id") != FAILED_INVOCATION \
            or status.get("input_sha256") != ROUND_INPUT_SHA \
            or status.get("report_sha256") != REPORT_SHA:
        raise ValueError("Round 1 is not the reviewed adjudication-only failure")

    packet = common.load(paths["adjudication_packet.json"])
    if packet.get("audit_id") != AUDIT_ID or packet.get("round") != ROUND \
            or packet.get("input_sha256") != ROUND_INPUT_SHA \
            or packet.get("report_sha256") != REPORT_SHA \
            or [row.get("row_id") for row in packet.get("unresolved_rows", [])] != ROW_IDS \
            or len(packet.get("triggers", [])) != len(ROW_IDS):
        raise ValueError("targeted adjudication packet identity or rows changed")

    meta = common.load(paths["target_snapshot.json"])
    if meta.get("target_input_sha256") != TARGET_SHA \
            or pathlib.Path(meta.get("snapshot", "")).resolve() != target.resolve() \
            or pathlib.Path(meta.get("schema", "")).resolve() != paths[
                "target.output.schema.json"].resolve() \
            or pathlib.Path(meta.get("prompt", "")).resolve() != paths[
                "target.prompt.txt"].resolve():
        raise ValueError("target snapshot metadata changed")
    manifest = common.load(paths["target.MANIFEST.json"])
    if manifest.get("target_input_sha256") != TARGET_SHA \
            or manifest.get("round_input_sha256") != ROUND_INPUT_SHA \
            or manifest.get("report_sha256") != REPORT_SHA \
            or manifest.get("model") != MODEL or manifest.get("effort") != EFFORT \
            or manifest.get("runtime_helper_sha256") != common.sha(
                pathlib.Path(runtime.__file__).resolve()):
        raise ValueError("target snapshot manifest or runtime binding changed")
    target_identity = [
        "i4r-target-snapshot-2", runtime.PROVIDER_SCHEMA_GENERATION_VERSION,
        MODEL, EFFORT, common.sha(pathlib.Path(runtime.__file__).resolve()),
        common.sha(HERE / "run_critique.sh"),
    ]
    if runtime.hash_tree(target / "source", target_identity) != TARGET_SHA:
        raise ValueError("target snapshot source or runtime identity changed")
    runtime.verify_read_only_inventory(target, manifest)

    count = round_dir / "attempt_budget/targeted_adjudicator.count"
    if count.read_text(encoding="utf-8").strip() != "1":
        raise ValueError("targeted adjudicator attempt counter is not exactly 1")
    event_text = paths["attempt1.events.jsonl"].read_text(encoding="utf-8", errors="strict")
    if "invalid_json_schema" not in event_text or "Unexpected constant value" not in event_text \
            or "covered_claim_ids" not in event_text:
        raise ValueError("attempt 1 is not the reviewed pre-inference composite-const failure")
    first_response = failed / "attempts/targeted_adjudicator/1/response.json"
    if first_response.exists() or common.pid_is_alive(
            failed / "attempts/targeted_adjudicator/1/provider.pid"):
        raise ValueError("attempt 1 unexpectedly has a response or live provider")

    ledger = common.rows(audit / "invocations.jsonl")
    attempts = [row for row in ledger if row.get("round") == ROUND \
                and row.get("role") == "critic_synth" and row.get("provider_call") is True]
    if len(attempts) != 1 or attempts[0].get("attempt") != 1 \
            or attempts[0].get("status") != "failed" \
            or attempts[0].get("error_class") != "schema" \
            or attempts[0].get("input_artifact_sha256") != TARGET_SHA \
            or attempts[0].get("requested_model") != MODEL \
            or attempts[0].get("reasoning_effort") != EFFORT \
            or attempts[0].get("output_sha256") is not None:
        raise ValueError("canonical ledger does not contain exactly failed adjudicator attempt 1")

    role_cache = round_dir / "result_cache" / ROUND_INPUT_SHA
    for role in ("stats", "data", "fairness", "completeness"):
        cache = role_cache / role
        runtime.validate_completion(
            cache / "complete.json", cache / "output.json", cache / "output.md",
            audit_id=AUDIT_ID, input_sha=ROUND_INPUT_SHA, report_sha=REPORT_SHA,
            round_number=ROUND, role=role,
        )
    target_cache = round_dir / "adjudication_cache" / TARGET_SHA
    if target_cache.exists() or target_cache.is_symlink() \
            or target_cache.with_suffix(".conflict").exists():
        raise ValueError("same-input targeted adjudication cache or conflict already exists")
    return {
        "round_dir": round_dir, "failed": failed, "target": target, "packet": packet,
        "packet_path": paths["adjudication_packet.json"], "base_schema": paths[
            "target.output.schema.json"], "prompt": paths["target.prompt.txt"],
        "counter": count, "permanent": paths["permanent"], "target_cache": target_cache,
        "attempt1_invocation_id": attempts[0]["invocation_id"],
    }


def write_status(path: pathlib.Path, **values: Any) -> None:
    common.atomic_json(path, {"retry_version": "i4r-targeted-adjudication-retry-1", **values})


def run(args: argparse.Namespace) -> dict[str, Any]:
    audit = pathlib.Path(args.audit_dir).resolve()
    if audit.is_symlink() or not audit.is_dir():
        raise ValueError("audit directory is missing or symlinked")
    state = validate_preconditions(audit)
    dry_schema = narrow_schema(
        state["base_schema"], "i4r-targeted-adjudication-retry-dryrun", state["target"])
    preview = {
        "retry_version": "i4r-targeted-adjudication-retry-1", "dry_run": args.dry_run,
        "audit_id": AUDIT_ID, "round": ROUND, "role": "targeted_adjudicator",
        "attempt": 2, "round_input_sha256": ROUND_INPUT_SHA,
        "target_input_sha256": TARGET_SHA, "report_sha256": REPORT_SHA,
        "row_ids": ROW_IDS, "attempt1_invocation_id": state["attempt1_invocation_id"],
        "persistent_marker_preserved": True, "provider_calls_planned": 0 if args.dry_run else 1,
        "composite_consts": 0,
    }
    if args.dry_run:
        # Exercise the complete provider preflight in memory without writing audit state.
        runtime.validate_provider_schema(dry_schema)
        return preview

    common.acquire_round_lock(state["round_dir"])
    codex = pathlib.Path(args.codex).resolve()
    if codex.is_symlink() or not codex.is_file() or not os.access(codex, os.X_OK):
        raise ValueError("Codex executable is unavailable or symlinked")
    invocation_id = (
        f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        f".r1.adjudicator.a2.{uuid.uuid4().hex[:8]}"
    )
    started = now(); started_epoch = epoch()
    call_id = f"{invocation_id}:targeted_adjudicator:2:{started_epoch}:{uuid.uuid4().hex[:8]}"
    invocation = state["round_dir"] / "invocations" / invocation_id
    attempt_dir = invocation / "attempts/targeted_adjudicator/2"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    status_path = invocation / "RETRY_STATUS.json"
    schema = attempt_dir / "output.schema.json"
    response = attempt_dir / "response.json"
    events = attempt_dir / "events.jsonl"
    stderr = attempt_dir / "stderr.txt"
    markdown = attempt_dir / "response.md"
    validation = attempt_dir / "validation.txt"
    sidecar = attempt_dir / "complete.json"
    pid_file = attempt_dir / "provider.pid"
    runtime.atomic_provider_schema(
        schema, narrow_schema(state["base_schema"], call_id, state["target"]))
    write_status(
        status_path, audit_id=AUDIT_ID, invocation_id=invocation_id, call_id=call_id,
        round=ROUND, role="targeted_adjudicator", attempt=2, outcome="ready",
        started_at=started, ended_at=None, provider_call=False,
        round_input_sha256=ROUND_INPUT_SHA, target_input_sha256=TARGET_SHA,
        report_sha256=REPORT_SHA, persistent_marker=str(state["permanent"].relative_to(audit)),
    )
    common.atomic_text(state["counter"], "2\n")
    write_status(
        status_path, audit_id=AUDIT_ID, invocation_id=invocation_id, call_id=call_id,
        round=ROUND, role="targeted_adjudicator", attempt=2, outcome="running",
        started_at=started, ended_at=None, provider_call=True,
        round_input_sha256=ROUND_INPUT_SHA, target_input_sha256=TARGET_SHA,
        report_sha256=REPORT_SHA, persistent_marker=str(state["permanent"].relative_to(audit)),
    )

    command = [
        sys.executable, str(pathlib.Path(runtime.__file__).resolve()), "provider-exec",
        "--codex", str(codex), "--model", MODEL, "--effort", EFFORT,
        "--workspace", str(audit), "--schema", str(schema),
        "--response", str(response), "--pid-file", str(pid_file),
    ]
    with state["prompt"].open("rb") as stdin, events.open("wb") as stdout, \
            stderr.open("wb") as error:
        process = subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=error)
        try:
            provider_rc = process.wait()
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
            raise
    ended = now(); ended_epoch = epoch(); elapsed = max(0, ended_epoch - started_epoch)
    valid = False
    validation_error = ""
    if provider_rc == 0 and response.is_file() and response.stat().st_size:
        try:
            runtime.command_validate_adjudication(SimpleNamespace(
                input=str(response), packet=str(state["packet_path"]), input_sha=TARGET_SHA,
                schema=str(schema), markdown=str(markdown),
            ))
            obj = common.load(response)
            if obj.get("covered_claim_ids") != ROW_IDS \
                    or [item.get("finding_id") for item in obj.get("items", [])] != ROW_IDS \
                    or obj.get("new_issues") != []:
                raise ValueError("adjudication output order or item join differs from packet rows")
            valid = True
            common.atomic_text(validation, "valid\n")
        except Exception as exc:
            validation_error = str(exc)
            common.atomic_text(validation, "invalid: " + validation_error + "\n")
    else:
        validation_error = f"provider exit={provider_rc}; response missing or empty"
        common.atomic_text(validation, validation_error + "\n")

    event_text = events.read_text(encoding="utf-8", errors="replace") if events.exists() else ""
    error_class = "" if valid else "schema" if (
        provider_rc == 0 or "invalid_json_schema" in event_text) else "permanent"
    runtime.command_telemetry(SimpleNamespace(
        local_log=str(invocation / "invocations.jsonl"), global_log=str(audit / "invocations.jsonl"),
        events=str(events), response=str(response), stderr=str(stderr), prompt=str(state["prompt"]),
        invocation=invocation_id, call_id=call_id, input_sha=TARGET_SHA,
        round=ROUND, role="targeted_adjudicator", attempt=2, provider_call=1,
        outcome="complete" if valid else "failed", failure_class="" if valid else "permanent",
        error_class=error_class, cache_usage="write" if valid else "none", exit_code=provider_rc,
        model=MODEL, effort=EFFORT, started=started, ended=ended, elapsed=elapsed,
        workspace=str(audit),
    ))
    if not valid:
        receipt = {
            **preview, "dry_run": False, "provider_calls_planned": 1,
            "invocation_id": invocation_id, "provider_call_id": call_id, "provider_called": True,
            "started_at": started, "ended_at": ended, "outcome": "failed",
            "provider_exit_code": provider_rc, "error": validation_error,
            "schema_sha256": common.sha(schema), "prompt_sha256": common.sha(state["prompt"]),
        }
        common.atomic_json(invocation / "RETRY_RECEIPT.json", receipt)
        write_status(
            status_path, audit_id=AUDIT_ID, invocation_id=invocation_id, call_id=call_id,
            round=ROUND, role="targeted_adjudicator", attempt=2, outcome="failed",
            started_at=started, ended_at=ended, provider_call=True,
            round_input_sha256=ROUND_INPUT_SHA, target_input_sha256=TARGET_SHA,
            report_sha256=REPORT_SHA, error=validation_error,
            persistent_marker=str(state["permanent"].relative_to(audit)),
        )
        raise ValueError("contract-narrowed targeted adjudicator attempt 2 failed: " + validation_error)

    runtime.command_complete(SimpleNamespace(
        output=str(sidecar), input_sha=TARGET_SHA, report_sha=REPORT_SHA, audit_id=AUDIT_ID,
        round=ROUND, role="targeted_adjudicator", attempts=2,
        response=str(response), markdown=str(markdown),
    ))
    runtime.validate_completion(
        sidecar, response, markdown, audit_id=AUDIT_ID, input_sha=TARGET_SHA,
        report_sha=REPORT_SHA, round_number=ROUND, role="targeted_adjudicator",
    )
    cache = state["target_cache"]
    cache.parent.mkdir(parents=True, exist_ok=True)
    stage = cache.parent / f".{TARGET_SHA}.{invocation_id}.tmp"
    stage.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(response, stage / "output.json")
    shutil.copyfile(markdown, stage / "output.md")
    shutil.copyfile(sidecar, stage / "complete.json")
    for path in stage.iterdir():
        path.chmod(0o444)
    stage.chmod(0o555)
    os.rename(stage, cache)
    receipt = {
        **preview, "dry_run": False, "provider_calls_planned": 1,
        "invocation_id": invocation_id, "provider_call_id": call_id, "provider_called": True,
        "started_at": started, "ended_at": ended, "outcome": "complete",
        "response_sha256": common.sha(response), "schema_sha256": common.sha(schema),
        "prompt_sha256": common.sha(state["prompt"]),
        "cache_path": cache.relative_to(audit).as_posix(),
        "cache_complete_sha256": common.sha(cache / "complete.json"),
    }
    common.atomic_json(invocation / "RETRY_RECEIPT.json", receipt)
    write_status(
        status_path, audit_id=AUDIT_ID, invocation_id=invocation_id, call_id=call_id,
        round=ROUND, role="targeted_adjudicator", attempt=2, outcome="complete",
        started_at=started, ended_at=ended, provider_call=True,
        round_input_sha256=ROUND_INPUT_SHA, target_input_sha256=TARGET_SHA,
        report_sha256=REPORT_SHA, response_sha256=common.sha(response),
        cache_path=cache.relative_to(audit).as_posix(),
        persistent_marker=str(state["permanent"].relative_to(audit)),
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_dir")
    parser.add_argument("--codex", default=None,
                        help="Codex CLI (default: CODEX, I4R_CODEX_BIN, else the newest working CLI; see codex_cli.py)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.codex is None:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import codex_cli
        args.codex, _how = codex_cli.resolve()
        if args.codex is None:
            parser.error("no working Codex CLI found; set CODEX or pass --codex (python3 scripts/codex_cli.py --explain)")
    try:
        result = run(args)
    except Exception as exc:
        print(f"targeted adjudication retry refused: {exc}", file=sys.stderr)
        return 1
    finally:
        common.release_round_lock()
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
