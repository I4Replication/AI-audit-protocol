#!/usr/bin/env python3
"""Run the final explicitly authorized, contract-narrowed completeness retry.

The ordinary driver correctly treats semantic/schema failures as permanent.  This tool is only for
the observed Round-1 failure where the critic completed substantive work but paraphrased
``completed_checks`` instead of copying its sole ``assigned_checks`` entry.  Attempt 2 then failed
before inference because the provider rejects array-valued ``const``.  This tool preserves both
failures and the marker, narrows the attempt-3 provider schema with a provider-supported exact
singleton array, calls only the completeness critic, and publishes a same-input cache object after
full validation.  It cannot run after the round-wide three-attempt cap is consumed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
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
import test_run_critique_runtime as runtime  # type: ignore


ROUND = 1
ROLE = "completeness"
MODEL = "gpt-5.6-terra"
EFFORT = "high"
EXPECTED_FAILURE = (
    "canonical critic semantic error: critic_result.coverage: "
    "completed and skipped checks must be assigned"
)
EXPECTED_ATTEMPT2_INVOCATION = "20260828T210446Z.r1.completeness.a2.42d89e81"
EXPECTED_ATTEMPT2_EVENTS_SHA256 = (
    "f731d0a27180dea62e447a75243cd429a6463befbfd22d01e5470d0bda9acba5"
)
EXPECTED_ATTEMPT2_STATUS_SHA256 = (
    "b99367f79efce31bcc794834fedd4b760a49a2a5babf50a299a746bf1e35fb5f"
)
EXPECTED_ATTEMPT2_SCHEMA_SHA256 = (
    "b43247834c8f4201131a4ccf5fe3c9383baac1bb4be27e56057824ee621c36f4"
)
EXPECTED_ATTEMPT2_LOCAL_TELEMETRY_SHA256 = (
    "8f1ae4c4db084fee2ee26dcd53895b3ccfdca0c1a448d12a0d6c5ae1f63dec4f"
)
EXPECTED_PERMANENT_MARKER_SHA256 = (
    "cc806b33dc962f96fb2795d7e415d02f5ee9dbbdd6d1f59547fbbfb0e6325292"
)
EXPECTED_ATTEMPT1_RESPONSE_SHA256 = (
    "9f708bcd0dec318c7e939802edb5a446c4d1e106a2dddde586c63f8cc822c46e"
)
CANONICAL_CHECK = (
    "Review the complete draft report and complete findings registry for claim/remedy/evidence "
    "coverage, including secondary entry points, supplements, ancillary measures, alternate "
    "toolchains, figures/tables, mechanical run paths, treatment fidelity, data-integrity "
    "signatures, and privacy/archive hygiene; run bounded high-risk checks."
)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_text(path: pathlib.Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def rows(path: pathlib.Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"ledger line {number} is not an object")
        result.append(value)
    return result


def exact_singleton_string_array_schema(
        value: str, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Constrain an array to one exact string without an unsupported array-valued const."""
    schema = dict(base or {})
    schema.pop("const", None)
    schema["type"] = "array"
    schema["minItems"] = 1
    schema["maxItems"] = 1
    item_schema = dict(schema.get("items") or {})
    item_schema.pop("const", None)
    item_schema["type"] = "string"
    item_schema["enum"] = [value]
    schema["items"] = item_schema
    return schema


def reject_composite_consts(schema: Any, path: str = "#") -> None:
    """Reject list/object consts that pass local JSON Schema but fail the provider subset."""
    if isinstance(schema, dict):
        if "const" in schema and isinstance(schema["const"], (list, dict)):
            raise ValueError(f"{path}: provider rejects composite const")
        for key, value in schema.items():
            reject_composite_consts(value, f"{path}/{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            reject_composite_consts(value, f"{path}/{index}")


def constrain_artifact_bindings(schema: dict[str, Any], snapshot: pathlib.Path) -> None:
    """Expose only immutable evidence-inventory paths and hashes to structured output."""
    inventory_path = snapshot / "source" / "context" / "EVIDENCE_HASHES.json"
    inventory = load(inventory_path)
    if not isinstance(inventory, dict) or not inventory:
        raise ValueError("frozen evidence inventory is missing or empty")
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
    binding = schema["$defs"]["artifact_binding"]["properties"]
    binding["path"]["enum"] = paths
    binding["sha256"]["enum"] = sorted(set(hashes))


def pid_is_alive(path: pathlib.Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    value = path.read_text(encoding="utf-8", errors="strict").strip()
    if not value.isdigit() or int(value) <= 1:
        raise ValueError(f"invalid prior provider PID record: {path}")
    try:
        os.kill(int(value), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


ROUND_LOCK: pathlib.Path | None = None


def acquire_round_lock(round_dir: pathlib.Path) -> None:
    global ROUND_LOCK
    lock = round_dir / ".runtime_lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValueError("Round 1 already has a runtime lock; final retry refused") from exc
    try:
        atomic_text(lock / "pid", f"{os.getpid()}\n")
    except BaseException:
        lock.rmdir()
        raise
    ROUND_LOCK = lock


def release_round_lock() -> None:
    global ROUND_LOCK
    if ROUND_LOCK is None:
        return
    try:
        (ROUND_LOCK / "pid").unlink(missing_ok=True)
        ROUND_LOCK.rmdir()
    finally:
        ROUND_LOCK = None


def validate_preconditions(audit: pathlib.Path) -> dict[str, Any]:
    round_dir = audit / "critique" / "round_1"
    status = load(round_dir / "ROUND_STATUS.json")
    if not isinstance(status, dict) or status.get("outcome") != "failed" \
            or status.get("failed_roles") != [ROLE] \
            or status.get("input_sha256") != (
                "3f8efbc5a7c44c4cc4ecfcb9263f8374e872fa0c7a7c29b1971ce7db3f240aa5"
            ):
        raise ValueError("current Round-1 status is not the exact completeness-only failure")
    input_sha = status["input_sha256"]
    snapshot = audit / status["snapshot"]
    manifest = load(snapshot / "MANIFEST.json")
    if manifest.get("input_sha256") != input_sha or manifest.get("round") != ROUND \
            or manifest.get("model") != MODEL or manifest.get("effort") != EFFORT:
        raise ValueError("immutable snapshot identity does not match the failed round")
    if manifest.get("runtime_helper_sha256") != sha(pathlib.Path(runtime.__file__).resolve()):
        raise ValueError("runtime helper changed since the same-input snapshot was created")
    if manifest.get("runtime_script_sha256") != sha(HERE / "run_critique.sh"):
        raise ValueError("critique driver changed since the same-input snapshot was created")
    runtime.verify_read_only_inventory(snapshot, manifest)

    counter = round_dir / "attempt_budget" / "completeness.count"
    permanent = round_dir / "attempt_budget" / "completeness.permanent"
    if counter.read_text(encoding="utf-8").strip() != "2":
        raise ValueError("completeness attempt counter is not exactly 2")
    if EXPECTED_FAILURE not in permanent.read_text(encoding="utf-8", errors="replace"):
        raise ValueError("persistent failure marker is not the reviewed coverage-string failure")
    if sha(permanent) != EXPECTED_PERMANENT_MARKER_SHA256:
        raise ValueError("persistent failure marker differs from the reviewed attempt-1 artifact")
    ledger = rows(audit / "invocations.jsonl")
    attempts = [
        row for row in ledger if row.get("round") == ROUND
        and row.get("role") == "critic_completeness" and row.get("provider_call") is True
    ]
    attempts.sort(key=lambda row: row.get("attempt", 0))
    if len(attempts) != 2 or [row.get("attempt") for row in attempts] != [1, 2]:
        raise ValueError("canonical ledger does not contain exactly completeness attempts 1 and 2")
    if attempts[0].get("status") != "failed" or attempts[0].get("error_class") != "schema":
        raise ValueError("canonical ledger does not preserve rejected attempt 1")
    attempt1_response = audit / str(attempts[0].get("output_artifact", ""))
    if sha(attempt1_response) != EXPECTED_ATTEMPT1_RESPONSE_SHA256 \
            or attempts[0].get("output_sha256") != EXPECTED_ATTEMPT1_RESPONSE_SHA256:
        raise ValueError("attempt 1 response differs from the reviewed semantic failure")
    if attempts[1].get("status") != "failed" or attempts[1].get("output_sha256") is not None:
        raise ValueError("canonical ledger does not preserve pre-inference attempt 2 failure")
    attempt2 = round_dir / "invocations" / EXPECTED_ATTEMPT2_INVOCATION
    attempt2_status = attempt2 / "RETRY_STATUS.json"
    attempt2_events = attempt2 / "attempts" / ROLE / "2" / "events.jsonl"
    attempt2_schema = attempt2 / "attempts" / ROLE / "2" / "output.schema.json"
    attempt2_response = attempt2 / "attempts" / ROLE / "2" / "response.json"
    attempt2_telemetry = attempt2 / "invocations.jsonl"
    if sha(attempt2_status) != EXPECTED_ATTEMPT2_STATUS_SHA256 \
            or sha(attempt2_events) != EXPECTED_ATTEMPT2_EVENTS_SHA256:
        raise ValueError("attempt 2 immutable failure artifacts differ from the reviewed hashes")
    if sha(attempt2_schema) != EXPECTED_ATTEMPT2_SCHEMA_SHA256 \
            or sha(attempt2_telemetry) != EXPECTED_ATTEMPT2_LOCAL_TELEMETRY_SHA256:
        raise ValueError("attempt 2 schema or local telemetry differs from the reviewed hashes")
    status2 = load(attempt2_status)
    if status2.get("outcome") != "failed" or status2.get("attempt") != 2 \
            or status2.get("invocation_id") != EXPECTED_ATTEMPT2_INVOCATION \
            or status2.get("call_id") != attempts[1].get("invocation_id"):
        raise ValueError("attempt 2 status and canonical ledger identity do not match")
    event_text = attempt2_events.read_text(encoding="utf-8", errors="strict")
    if "invalid_json_schema" not in event_text or "Unexpected constant value" not in event_text:
        raise ValueError("attempt 2 is not the reviewed array-const provider rejection")
    if attempt2_response.exists():
        raise ValueError("attempt 2 unexpectedly has a provider response")
    if pid_is_alive(attempt2 / "attempts" / ROLE / "2" / "provider.pid"):
        raise ValueError("attempt 2 still has a live provider process")

    cache_root = round_dir / "result_cache" / input_sha
    for cached_role in ("stats", "data", "fairness"):
        cache = cache_root / cached_role
        runtime.validate_completion(
            cache / "complete.json", cache / "output.json", cache / "output.md",
            audit_id=status["audit_id"], input_sha=input_sha,
            report_sha=status["report_sha256"], round_number=ROUND, role=cached_role,
        )
    completeness_cache = cache_root / ROLE
    if completeness_cache.exists() or completeness_cache.is_symlink():
        raise ValueError("same-input completeness cache already exists")
    return {
        "status": status, "snapshot": snapshot, "manifest": manifest,
        "counter": counter, "permanent": permanent, "cache_root": cache_root,
        "attempt1_invocation_id": attempts[0]["invocation_id"],
        "attempt1_output_sha256": attempts[0].get("output_sha256"),
        "attempt2_invocation_id": attempts[1]["invocation_id"],
    }


def build_schema(base: pathlib.Path, output: pathlib.Path, call_id: str) -> None:
    schema = load(base)
    properties = schema["properties"]
    properties["invocation_id"] = runtime.json_const_schema(
        call_id, properties["invocation_id"])
    coverage = properties["coverage"]["properties"]
    coverage["assigned_checks"] = exact_singleton_string_array_schema(
        CANONICAL_CHECK, coverage["assigned_checks"])
    coverage["completed_checks"] = exact_singleton_string_array_schema(
        CANONICAL_CHECK, coverage["completed_checks"])
    # Completeness owns the unassigned audit surface and has no preassigned claim IDs.
    properties["covered_claim_ids"]["maxItems"] = 0
    properties["items"]["maxItems"] = 0
    constrain_artifact_bindings(schema, base.parent.parent)
    reject_composite_consts(schema)
    runtime.atomic_provider_schema(output, schema)


def write_status(path: pathlib.Path, **values: Any) -> None:
    atomic_json(path, {"retry_version": "i4r-completeness-retry-2", **values})


def run(args: argparse.Namespace) -> dict[str, Any]:
    audit = pathlib.Path(args.audit_dir).resolve()
    if audit.is_symlink() or not audit.is_dir():
        raise ValueError("audit directory is missing or symlinked")
    state = validate_preconditions(audit)
    status = state["status"]
    snapshot: pathlib.Path = state["snapshot"]
    base_schema = snapshot / "schemas" / "critic_completeness.schema.json"
    prompt = snapshot / "rendered" / "prompt_completeness.txt"
    if not base_schema.is_file() or base_schema.is_symlink() \
            or not prompt.is_file() or prompt.is_symlink():
        raise ValueError("immutable completeness prompt/schema is missing or symlinked")
    dry_schema = load(base_schema)
    dry_properties = dry_schema["properties"]
    dry_properties["invocation_id"] = runtime.json_const_schema(
        "i4r-completeness-retry-dryrun", dry_properties["invocation_id"])
    dry_coverage = dry_properties["coverage"]["properties"]
    for name in ("assigned_checks", "completed_checks"):
        dry_coverage[name] = exact_singleton_string_array_schema(
            CANONICAL_CHECK, dry_coverage[name])
    dry_properties["covered_claim_ids"]["maxItems"] = 0
    dry_properties["items"]["maxItems"] = 0
    constrain_artifact_bindings(dry_schema, base_schema.parent.parent)
    reject_composite_consts(dry_schema)
    runtime.validate_provider_schema(dry_schema)
    preview = {
        "retry_version": "i4r-completeness-retry-2",
        "dry_run": bool(args.dry_run),
        "audit_id": status["audit_id"], "round": ROUND, "role": ROLE,
        "attempt": 3, "input_sha256": status["input_sha256"],
        "report_sha256": status["report_sha256"],
        "attempt1_invocation_id": state["attempt1_invocation_id"],
        "attempt2_invocation_id": state["attempt2_invocation_id"],
        "persistent_marker_preserved": True,
        "provider_calls_planned": 0 if args.dry_run else 1,
        "contract_narrowing": {
            "assigned_checks": [CANONICAL_CHECK], "completed_checks": [CANONICAL_CHECK],
        },
    }
    if args.dry_run:
        return preview

    acquire_round_lock(audit / "critique" / "round_1")
    codex = pathlib.Path(args.codex).resolve()
    if codex.is_symlink() or not codex.is_file() or not os.access(codex, os.X_OK):
        raise ValueError("Codex executable is unavailable or symlinked")
    invocation_id = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.r1.completeness.a3.{uuid.uuid4().hex[:8]}"
    call_started = now(); started_epoch = epoch()
    call_id = f"{invocation_id}:completeness:3:{started_epoch}:{uuid.uuid4().hex[:8]}"
    invocation = audit / "critique" / "round_1" / "invocations" / invocation_id
    attempt_dir = invocation / "attempts" / ROLE / "3"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    retry_status = invocation / "RETRY_STATUS.json"
    schema = attempt_dir / "output.schema.json"
    response = attempt_dir / "response.json"
    events = attempt_dir / "events.jsonl"
    stderr = attempt_dir / "stderr.txt"
    markdown = attempt_dir / "response.md"
    validation = attempt_dir / "validation.txt"
    sidecar = attempt_dir / "complete.json"
    pid_file = attempt_dir / "provider.pid"
    build_schema(base_schema, schema, call_id)
    write_status(
        retry_status, audit_id=status["audit_id"], invocation_id=invocation_id,
        call_id=call_id, round=ROUND, role=ROLE, attempt=3, outcome="ready",
        started_at=call_started, ended_at=None, provider_call=False,
        input_sha256=status["input_sha256"], report_sha256=status["report_sha256"],
        persistent_marker=str(state["permanent"].relative_to(audit)),
    )
    # Claim the final round-wide attempt only after every immutable input and output
    # location has been prepared.  The rejected permanent marker is intentionally untouched.
    atomic_text(state["counter"], "3\n")
    write_status(
        retry_status, audit_id=status["audit_id"], invocation_id=invocation_id,
        call_id=call_id, round=ROUND, role=ROLE, attempt=3, outcome="running",
        started_at=call_started, ended_at=None, provider_call=True,
        input_sha256=status["input_sha256"], report_sha256=status["report_sha256"],
        persistent_marker=str(state["permanent"].relative_to(audit)),
    )

    command = [
        sys.executable, str(pathlib.Path(runtime.__file__).resolve()), "provider-exec",
        "--codex", str(codex), "--model", MODEL, "--effort", EFFORT,
        "--workspace", str(audit), "--schema", str(schema),
        "--response", str(response), "--pid-file", str(pid_file),
    ]
    with prompt.open("rb") as stdin, events.open("wb") as stdout, stderr.open("wb") as error:
        process = subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=error)
        try:
            provider_rc = process.wait()
        except BaseException:
            try: os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError): pass
            process.wait()
            raise
    ended = now(); ended_epoch = epoch(); elapsed = max(0, ended_epoch - started_epoch)
    valid = False
    validation_error = ""
    if provider_rc == 0 and response.is_file() and response.stat().st_size:
        try:
            runtime.command_validate_critic(SimpleNamespace(
                input=str(response), role=ROLE, round=ROUND,
                input_sha=status["input_sha256"], report_sha=status["report_sha256"],
                audit_id=status["audit_id"], schema=str(schema), markdown=str(markdown),
            ))
            valid = True
            atomic_text(validation, "valid\n")
        except Exception as exc:
            validation_error = str(exc)
            atomic_text(validation, "invalid: " + validation_error + "\n")
    else:
        validation_error = f"provider exit={provider_rc}; response missing or empty"
        atomic_text(validation, validation_error + "\n")

    telemetry = SimpleNamespace(
        local_log=str(invocation / "invocations.jsonl"),
        global_log=str(audit / "invocations.jsonl"), events=str(events),
        response=str(response), stderr=str(stderr), prompt=str(prompt),
        invocation=invocation_id, call_id=call_id, input_sha=status["input_sha256"],
        round=ROUND, role=ROLE, attempt=3, provider_call=1,
        outcome="complete" if valid else "failed",
        failure_class="" if valid else "permanent",
        error_class="" if valid else "schema" if provider_rc == 0 else "permanent",
        cache_usage="write" if valid else "none", exit_code=provider_rc,
        model=MODEL, effort=EFFORT, started=call_started, ended=ended,
        elapsed=elapsed, workspace=str(audit),
    )
    runtime.command_telemetry(telemetry)
    if not valid:
        failure_receipt = {
            **preview, "dry_run": False, "provider_calls_planned": 1,
            "invocation_id": invocation_id, "provider_call_id": call_id,
            "provider_called": True, "started_at": call_started, "ended_at": ended,
            "outcome": "failed", "provider_exit_code": provider_rc,
            "error": validation_error, "schema_sha256": sha(schema),
            "prompt_sha256": sha(prompt),
        }
        atomic_json(invocation / "RETRY_RECEIPT.json", failure_receipt)
        write_status(
            retry_status, audit_id=status["audit_id"], invocation_id=invocation_id,
            call_id=call_id, round=ROUND, role=ROLE, attempt=3, outcome="failed",
            started_at=call_started, ended_at=ended, provider_call=True,
            input_sha256=status["input_sha256"], report_sha256=status["report_sha256"],
            error=validation_error,
            persistent_marker=str(state["permanent"].relative_to(audit)),
        )
        raise ValueError("contract-narrowed completeness attempt 3 failed: " + validation_error)

    runtime.command_complete(SimpleNamespace(
        output=str(sidecar), input_sha=status["input_sha256"],
        report_sha=status["report_sha256"], audit_id=status["audit_id"],
        round=ROUND, role=ROLE, attempts=3, response=str(response), markdown=str(markdown),
    ))
    runtime.validate_completion(
        sidecar, response, markdown, audit_id=status["audit_id"],
        input_sha=status["input_sha256"], report_sha=status["report_sha256"],
        round_number=ROUND, role=ROLE,
    )
    cache_target = state["cache_root"] / ROLE
    stage = state["cache_root"] / f".{ROLE}.{invocation_id}.tmp"
    stage.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(response, stage / "output.json")
    shutil.copyfile(markdown, stage / "output.md")
    shutil.copyfile(sidecar, stage / "complete.json")
    for path in stage.iterdir(): path.chmod(0o444)
    stage.chmod(0o555)
    os.rename(stage, cache_target)
    receipt = {
        **preview, "dry_run": False, "provider_calls_planned": 1,
        "invocation_id": invocation_id, "provider_call_id": call_id,
        "provider_called": True, "started_at": call_started, "ended_at": ended,
        "response_sha256": sha(response), "schema_sha256": sha(schema),
        "prompt_sha256": sha(prompt), "cache_path": cache_target.relative_to(audit).as_posix(),
        "cache_complete_sha256": sha(cache_target / "complete.json"),
    }
    atomic_json(invocation / "RETRY_RECEIPT.json", receipt)
    write_status(
        retry_status, audit_id=status["audit_id"], invocation_id=invocation_id,
        call_id=call_id, round=ROUND, role=ROLE, attempt=3, outcome="complete",
        started_at=call_started, ended_at=ended, provider_call=True,
        input_sha256=status["input_sha256"], report_sha256=status["report_sha256"],
        response_sha256=sha(response), cache_path=cache_target.relative_to(audit).as_posix(),
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
        print(f"completeness retry refused: {exc}", file=sys.stderr)
        return 1
    finally:
        release_round_lock()
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
