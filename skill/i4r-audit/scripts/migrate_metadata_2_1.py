#!/usr/bin/env python3
"""Append-only migration of a historical audit to metadata 2.1.

The regression mode (audit A01) preserves every invocation, leaves Round 1 untouched,
corrects the false Round-2 start to awaiting authorization, keeps Terra served
identity unverified, and marks max-14 document artifacts diagnostic-only.
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys
from collections import defaultdict
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
TOOLS = HERE.parent / "schemas/tools"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TOOLS))

import lifecycle_journal  # noqa: E402
import document_release  # noqa: E402
from normalize_to_v2_1 import normalize_run, normalize_invocation  # noqa: E402


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def read_invocations(root: pathlib.Path, run: dict[str, Any]) -> list[dict[str, Any]]:
    ledger = root / lifecycle_journal.INVOCATIONS_NAME
    if ledger.is_file():
        result = []
        for number, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{ledger} line {number} is not an object")
            result.append(value)
        return result
    return list(run.get("invocations") or [])


def _terra_requested(record: dict[str, Any]) -> bool:
    return "terra" in str(record.get("requested_model") or "").lower()


def migrate_invocations(rows: list[dict[str, Any]], *, regression: bool) -> list[dict[str, Any]]:
    result = []
    attempt_counts: dict[tuple[Any, Any], int] = defaultdict(int)
    for source in rows:
        row = normalize_invocation(source)
        row.pop("journal_event", None)
        if regression and _terra_requested(row):
            row["served_model"] = None
            row["observed_model"] = None
            row.pop("observed_model_ids", None)
            row["identity_assurance"] = "requested_only"
            row["model_source"] = "declared"
            row["model_identity_evidence"] = {
                "source": "adapter", "raw_ids": [], "artifact": None,
                "artifact_sha256": None,
            }
        pre_inference = (
            row.get("status") != "completed"
            and row.get("error_class") in {
                "schema", "deterministic_schema", "authentication", "authorization",
                "quota", "model", "unsupported_model", "capability",
            }
            and not row.get("usage", {}).get("obtainable")
        )
        if pre_inference:
            row["attempt_kind"] = (
                "schema_admission" if row.get("error_class") in {"schema", "deterministic_schema"}
                else "capability_probe"
            )
            row["provider_request_status"] = "rejected"
            row["inference_start_status"] = "not_started"
            row["attempt"] = None
        elif row.get("provider_call"):
            row["attempt_kind"] = row.get("attempt_kind") or "role_inference"
            row["provider_request_status"] = "accepted"
            row["inference_start_status"] = "started"
            key = (row.get("role"), row.get("round"))
            attempt_counts[key] += 1
            row["attempt"] = attempt_counts[key]
        else:
            row["attempt_kind"] = "cache_replay"
            row["provider_request_status"] = "not_requested"
            row["inference_start_status"] = "not_started"
            row["attempt"] = None
        result.append(row)
    return result


def invocation_specs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for row in rows:
        invocation_id = row["invocation_id"]
        reservation = {
            key: copy.deepcopy(row.get(key))
            for key in (
                "invocation_id", "role", "role_class", "round", "attempt_kind",
                "provider", "requested_model", "reasoning_effort",
                "input_artifact_sha256", "prompt_sha256",
            )
        }
        reservation.update({
            "attempt": None, "provider_request_status": "not_requested",
            "inference_start_status": "not_started",
        })
        specs.append({"type": "invocation_reserved", "payload": reservation})
        if row["provider_request_status"] != "not_requested":
            specs.append({
                "type": "provider_request",
                "payload": {"invocation_id": invocation_id, "status": row["provider_request_status"]},
            })
        if row["inference_start_status"] == "started":
            specs.append({
                "type": "inference_start",
                "payload": {"invocation_id": invocation_id, "attempt": row["attempt"]},
                "at": row.get("started_at"),
            })
        specs.append({
            "type": "invocation_terminal",
            "payload": {"invocation_id": invocation_id, "record": row},
            "at": row.get("ended_at"),
        })
    return specs


def _exact_document_pages(pdf: pathlib.Path) -> dict[str, int | None]:
    total = document_release.exact_page_count(pdf)
    texts = document_release.page_texts(pdf, total)
    boundaries = []
    for number, text in enumerate(texts, 1):
        lines = document_release.page_lines(text)
        if lines and document_release.pdf_line_starts_back_matter(lines[0]):
            boundaries.append(number)
    first_back = min(boundaries) if boundaries else None
    body = total if first_back is None else first_back - 1
    return {
        "total_pages": total, "body_pages": body,
        "back_matter_pages": total - body, "first_back_matter_page": first_back,
    }


def diagnostic_artifacts(root: pathlib.Path) -> list[dict[str, str]]:
    """Identify max-14 diagnostics only from a valid v3 manifest and exact PDF bytes."""
    # Discovery is content-based so neither the PDF nor its manifest needs a
    # historical "max14"/"manifest" filename to receive the safe disposition.
    candidates = sorted(set(root.rglob("*.json")))
    result: list[dict[str, str]] = []
    for manifest_path in candidates:
        if not manifest_path.is_file() or manifest_path.is_symlink():
            continue
        try:
            envelope = read_json(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if envelope.get("schema_version") != document_release.MANIFEST_SCHEMA:
            continue
        try:
            manifest = document_release.load_manifest(manifest_path)
            pdf = document_release.verify_record(
                root, (manifest.get("artifacts") or {}).get("pdf"), "diagnostic PDF",
            )
            observed = _exact_document_pages(pdf)
        except (OSError, document_release.DocumentReleaseError) as exc:
            raise ValueError(
                f"stale or unverifiable document manifest {manifest_path}: {exc}"
            ) from exc
        recorded = manifest.get("pages") or {}
        for key, value in observed.items():
            if recorded.get(key) != value:
                raise ValueError(
                    f"document manifest {manifest_path} has stale {key}: "
                    f"recorded={recorded.get(key)!r}, exact={value!r}"
                )
        if observed["body_pages"] != 14:
            continue
        result.append({
            "path": pdf.relative_to(root.resolve()).as_posix(),
            "disposition": "diagnostic_only",
            "reason": (
                "v3 document manifest plus exact PDF recount confirms 14 body pages; "
                "the artifact is not release-bound"
            ),
        })
    return result


def corrected_regression_rounds(base: dict[str, Any]) -> list[dict[str, Any]]:
    rounds = copy.deepcopy(base.get("rounds") or [])
    replacement = lifecycle_journal._empty_round(2)
    replaced = False
    for index, row in enumerate(rounds):
        if row.get("round") == 2:
            rounds[index] = replacement
            replaced = True
            break
    if not replaced:
        rounds.append(replacement)
    rounds.sort(key=lambda row: row.get("round", 0))
    return rounds


def migrate(root: pathlib.Path, *, regression: bool, expected_invocations: int | None) -> dict[str, Any]:
    root = root.resolve()
    run_path = root / lifecycle_journal.RUN_NAME
    original_run = read_json(run_path)
    source_rows = read_invocations(root, original_run)
    if expected_invocations is not None and len(source_rows) != expected_invocations:
        raise ValueError(
            f"expected {expected_invocations} invocation records, found {len(source_rows)}"
        )
    normalized = normalize_run(original_run)
    rows = migrate_invocations(source_rows, regression=regression)
    base = copy.deepcopy(normalized)
    base["invocations"] = []
    base["events"] = []
    base["journal_watermark"] = {"journal_seq": 0, "journal_hash": None}
    base["schema_version"] = lifecycle_journal.SCHEMA_VERSION

    with lifecycle_journal.journal_lock(root):
        events = lifecycle_journal.load_events(root)
        specs: list[dict[str, Any]] = []
        if not events:
            specs.append({
                "type": "init", "payload": {"initial_run": base},
                "at": base.get("started_at"), "source": "migrate_metadata_2_1.py",
            })
        elif not isinstance((events[0].get("payload") or {}).get("initial_run"), dict):
            specs.append({
                "type": "migration_anchor",
                "payload": {
                    "initial_run": base,
                    "legacy_prefix_events": len(events),
                    "legacy_prefix_watermark": lifecycle_journal.watermark(events),
                },
                "source": "migrate_metadata_2_1.py",
            })
        elif original_run.get("schema_version") == lifecycle_journal.SCHEMA_VERSION:
            raise ValueError("audit already has a metadata-2.1 canonical journal")
        specs.extend(invocation_specs(rows))
        if regression:
            # Round 1 is intentionally not rerun or synthesized. Only the false
            # Round-2 start and release disposition receive corrective events.
            # No synthetic round_prepared event is created: a future run must
            # prepare and authorize its exact, hash-bound Round-2 plan normally.
            diagnostics = diagnostic_artifacts(root)
            specs.extend([
                {
                    "type": "metadata_correction",
                    "payload": {"set": {
                        "rounds": corrected_regression_rounds(base),
                        "diagnostic_artifacts": diagnostics,
                    }, "reason": "remove the false Round-2 start; no provider dispatch occurred"},
                    "source": "migrate_metadata_2_1.py",
                },
                {
                    "type": "round_authorization_required",
                    "payload": {
                        "round": 2, "reason": "awaiting_authorization",
                        "plan_required": True,
                    },
                    "source": "migrate_metadata_2_1.py",
                },
            ])
        events = lifecycle_journal._append_locked(root, specs)
        lifecycle_journal.project_all_locked(root, events)

    projected_rows = [
        json.loads(line)
        for line in (root / lifecycle_journal.INVOCATIONS_NAME).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(projected_rows) != len(rows):
        raise ValueError("migration projection lost or duplicated invocation records")
    return {
        "schema_version": lifecycle_journal.SCHEMA_VERSION,
        "invocations_projected": len(projected_rows), "provider_calls_during_migration": 0,
        "round_1_rerun": False, "round_2_state": "awaiting_authorization" if regression else None,
        **lifecycle_journal.watermark(events),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--regression", action="store_true")
    parser.add_argument("--expected-invocations", type=int)
    args = parser.parse_args(argv)
    expected = args.expected_invocations
    if args.regression and expected is None:
        expected = 38
    try:
        result = migrate(args.audit_dir, regression=args.regression, expected_invocations=expected)
    except (OSError, ValueError, json.JSONDecodeError, lifecycle_journal.JournalError) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
