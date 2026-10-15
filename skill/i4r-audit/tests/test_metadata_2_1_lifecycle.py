from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest


SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "schemas/tools"))

import lifecycle_journal
import migrate_metadata_2_1
from tests import test_document_release


def base_run() -> dict:
    return {
        "schema_version": "2.1.0", "audit_id": "audit-metadata-21",
        "started_at": "2026-08-30T10:00:00Z", "ended_at": None,
        "status": "initialized", "pause_reason": None,
        "post_delta_gates_passed": False, "round_target": 2, "round_cap": 5,
        "rounds": [], "rounds_run": 0, "stopping_reason": None,
        "invocations": [], "events": [],
        "human_effort": {"person_minutes_by_phase": {}},
    }


def reservation(invocation_id: str, role: str = "auditor_reproduction") -> dict:
    return {
        "invocation_id": invocation_id, "role": role, "role_class": "auditor",
        "round": 0, "attempt_kind": "role_inference", "provider": "anthropic",
        "requested_model": "sonnet-5", "reasoning_effort": "max",
        "input_artifact_sha256": "a" * 64, "prompt_sha256": "b" * 64,
    }


def usage(total: int = 15) -> dict:
    return {
        "obtainable": True, "input_tokens": 10, "output_tokens": 5,
        "cached_input_tokens": 0, "reasoning_tokens": 0,
        "total_tokens": total, "source": "api_response",
    }


def terminal(invocation_id: str, role: str = "auditor_reproduction") -> dict:
    return {
        "invocation_id": invocation_id, "role": role, "role_class": "auditor",
        "round": 0, "attempt": 1, "attempt_kind": "role_inference",
        "status": "completed", "provider": "anthropic", "provider_call": True,
        "provider_request_status": "accepted", "inference_start_status": "started",
        "cache_usage": "none", "session_id": "session-1",
        "requested_model": "sonnet-5", "observed_model": "claude-sonnet-5",
        "served_model": "claude-sonnet-5", "identity_assurance": "observed",
        "model_identity_evidence": None, "observed_model_ids": ["claude-sonnet-5"],
        "model_family": "claude", "model_source": "observed",
        "reasoning_effort": "max", "started_at": "2026-08-30T10:01:00Z",
        "ended_at": "2026-08-30T10:02:00Z", "elapsed_seconds": 60,
        "provider_duration_seconds": 60, "turn_count": 1,
        "provider_cost": {"amount": 0.1, "currency": "USD", "source": "api_response", "unavailable_reason": None},
        "input_artifact_sha256": "a" * 64, "prompt_sha256": "b" * 64,
        "output_artifact": "report/out.md", "output_sha256": "c" * 64,
        "usage": usage(), "usage_by_model": [], "unattributed_usage": usage(),
        "source_invocation_id": None, "error": None, "error_class": None,
        "legacy_source": None,
    }


class Metadata21LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="i4r-metadata21-")
        self.root = pathlib.Path(self.temp.name)
        lifecycle_journal.initialize(self.root, base_run())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_hash_chain_and_all_projection_watermarks_match(self) -> None:
        reserved = lifecycle_journal.reserve_invocation(self.root, reservation("inv-one"))
        self.assertIsNone(reserved["attempt"])
        lifecycle_journal.mark_provider_request(self.root, "inv-one")
        started = lifecycle_journal.mark_inference_start(self.root, "inv-one")
        self.assertEqual(started["attempt"], 1)
        lifecycle_journal.record_terminal(self.root, terminal("inv-one"))

        events = lifecycle_journal.load_events(self.root, allow_legacy_prefix=False)
        mark = lifecycle_journal.watermark(events)
        run = json.loads((self.root / "run.json").read_text())
        metrics = json.loads((self.root / "run_metrics.json").read_text())
        invocation_mark = json.loads((self.root / "invocations.watermark.json").read_text())
        self.assertEqual(run["journal_watermark"], mark)
        self.assertEqual(metrics["journal_watermark"], mark)
        self.assertEqual(invocation_mark, mark)
        self.assertEqual(len(run["invocations"]), 1)
        self.assertEqual(run["invocations"][0]["journal_event"]["hash"], events[-1]["event_hash"])

    def test_only_accepted_inference_starts_consume_three_attempt_budget(self) -> None:
        ids = [f"inv-budget-{index}" for index in range(4)]
        for invocation_id in ids:
            lifecycle_journal.reserve_invocation(self.root, reservation(invocation_id, "same-role"))
            lifecycle_journal.mark_provider_request(self.root, invocation_id)

        def start(invocation_id: str):
            try:
                return lifecycle_journal.mark_inference_start(self.root, invocation_id)["attempt"]
            except lifecycle_journal.AttemptBudgetExhausted:
                return "exhausted"

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(start, ids))
        self.assertEqual(sorted(value for value in outcomes if isinstance(value, int)), [1, 2, 3])
        self.assertEqual(outcomes.count("exhausted"), 1)

        no_attempt = lifecycle_journal.reserve_invocation(self.root, {
            **reservation("inv-preflight", "same-role"), "attempt_kind": "local_preflight",
        })
        self.assertIsNone(no_attempt["attempt"])
        events = lifecycle_journal.load_events(self.root)
        starts = [event for event in events if event["type"] == "inference_start"]
        self.assertEqual(len(starts), 3)

    def test_schema_admission_is_capped_by_role_and_digest_without_attempts(self) -> None:
        digest = "e" * 64
        for index in range(2):
            invocation_id = f"inv-schema-{index}"
            lifecycle_journal.reserve_invocation(self.root, {
                **reservation(invocation_id, "critic_stats"),
                "attempt_kind": "role_inference", "schema_digest": digest,
            })
            lifecycle_journal.mark_provider_request(self.root, invocation_id)
            lifecycle_journal.mark_provider_request(
                self.root, invocation_id, status="rejected",
            )
        lifecycle_journal.reserve_invocation(self.root, {
            **reservation("inv-schema-2", "critic_stats"),
            "attempt_kind": "role_inference", "schema_digest": digest,
        })
        with self.assertRaises(lifecycle_journal.JournalError):
            lifecycle_journal.mark_provider_request(self.root, "inv-schema-2")
        events = lifecycle_journal.load_events(self.root)
        self.assertFalse(any(event["type"] == "inference_start" for event in events))

    def test_missing_authorization_pauses_without_reservation_or_provider_request(self) -> None:
        policy = "d" * 64
        plan = {
            "purpose": "round-1 critique", "provider": "openai",
            "model_route": "gpt-5.6-terra/high", "data_classes": ["report", "evidence"],
            "policy_sha256": policy,
        }
        lifecycle_journal.prepare_round(self.root, 1, plan)
        with self.assertRaises(lifecycle_journal.AuthorizationRequired):
            lifecycle_journal.reserve_invocation(self.root, {
                **reservation("inv-critic", "critic_stats"), "role_class": "critic",
                "round": 1, "provider": "openai", "requested_model": "gpt-5.6-terra",
                "reasoning_effort": "high",
            })
        events = lifecycle_journal.load_events(self.root)
        self.assertFalse(any(
            (event.get("payload") or {}).get("invocation_id") == "inv-critic"
            for event in events
        ))
        run = json.loads((self.root / "run.json").read_text())
        self.assertEqual((run["status"], run["pause_reason"]), ("paused", "awaiting_authorization"))

        prepared = next(event for event in events if event["type"] == "round_prepared")
        round_plan = prepared["payload"]["plan"]
        authorization = {
            key: round_plan[key]
            for key in ("purpose", "round_plan_sha256", "provider", "model_route", "data_classes", "policy_sha256")
        }
        authorization["expires_at"] = "2099-01-01T00:00:00Z"
        lifecycle_journal.authorize_round(self.root, 1, authorization)
        lifecycle_journal.reserve_invocation(self.root, {
            **reservation("inv-critic", "critic_stats"), "role_class": "critic",
            "round": 1, "provider": "openai", "requested_model": "gpt-5.6-terra",
            "reasoning_effort": "high",
        })
        lifecycle_journal.mark_provider_request(self.root, "inv-critic")
        events = lifecycle_journal.load_events(self.root)
        request_index = next(index for index, event in enumerate(events) if
                             event["type"] == "provider_request" and
                             event["payload"].get("invocation_id") == "inv-critic")
        self.assertEqual(events[request_index - 1]["type"], "round_start")

    def test_unprepared_dispatch_pause_can_be_prepared_and_authorized_forward(self) -> None:
        critic = {
            **reservation("inv-unprepared", "critic_data"), "role_class": "critic",
            "round": 1, "provider": "openai", "requested_model": "gpt-5.6-terra",
            "reasoning_effort": "high",
        }
        with self.assertRaises(lifecycle_journal.AuthorizationRequired):
            lifecycle_journal.reserve_invocation(self.root, critic)
        events = lifecycle_journal.load_events(self.root)
        self.assertFalse(any(event["type"] == "round_prepared" for event in events))
        self.assertFalse(any(event["type"] == "invocation_reserved" for event in events))

        plan = {
            "purpose": "round-1 critique", "provider": "openai",
            "model_route": "gpt-5.6-terra/high", "data_classes": ["report", "evidence"],
            "policy_sha256": "f" * 64,
        }
        prepared = lifecycle_journal.prepare_round(self.root, 1, plan)
        authorization = {
            key: prepared["plan"][key]
            for key in ("purpose", "round_plan_sha256", "provider", "model_route",
                        "data_classes", "policy_sha256")
        }
        authorization["expires_at"] = "2099-01-01T00:00:00Z"
        lifecycle_journal.authorize_round(self.root, 1, authorization)
        lifecycle_journal.reserve_invocation(self.root, critic)

    def test_recovery_at_each_lifecycle_boundary_never_calls_provider(self) -> None:
        lifecycle_journal.reserve_invocation(self.root, reservation("inv-crash"))
        recovered = lifecycle_journal.recover(self.root)
        self.assertEqual(recovered["provider_calls"], 0)
        self.assertIsNone(recovered["incomplete_invocations"][0]["attempt"])

        lifecycle_journal.mark_provider_request(self.root, "inv-crash")
        recovered = lifecycle_journal.recover(self.root)
        self.assertEqual(recovered["provider_calls"], 0)
        self.assertIsNone(recovered["incomplete_invocations"][0]["attempt"])

        lifecycle_journal.mark_inference_start(self.root, "inv-crash")
        response = self.root / "work/response.json"
        response.parent.mkdir()
        response.write_text("{}\n")
        lifecycle_journal.mark_response_received(
            self.root, "inv-crash", artifact="work/response.json",
            sha256=hashlib.sha256(response.read_bytes()).hexdigest(),
        )
        recovered = lifecycle_journal.recover(self.root)
        incomplete = recovered["incomplete_invocations"][0]
        self.assertEqual(incomplete["attempt"], 1)
        self.assertTrue(incomplete["response_received"])
        self.assertIn("do_not_duplicate_provider_call", incomplete["recovery_action"])

        # Simulate a crash after terminal journaling but before any projection rename.
        with lifecycle_journal.journal_lock(self.root):
            events = lifecycle_journal.load_events(self.root)
            events = lifecycle_journal._append_locked(self.root, [{
                "type": "invocation_terminal",
                "payload": {"invocation_id": "inv-crash", "record": terminal("inv-crash")},
                "at": "2026-08-30T10:02:00Z",
            }])
        (self.root / "invocations.jsonl").write_text("")
        recovered = lifecycle_journal.recover(self.root)
        self.assertEqual(recovered["provider_calls"], 0)
        rows = [json.loads(line) for line in (self.root / "invocations.jsonl").read_text().splitlines()]
        self.assertEqual([row["invocation_id"] for row in rows], ["inv-crash"])
        self.assertEqual(rows[0]["attempt"], 1)

    def test_tampered_journal_is_rejected(self) -> None:
        lifecycle_journal.append_event(self.root, "note", {"text": "bound"})
        path = self.root / "run.events.jsonl"
        lines = path.read_text().splitlines()
        value = json.loads(lines[-1])
        value["payload"]["text"] = "tampered"
        lines[-1] = json.dumps(value, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")
        with self.assertRaises(lifecycle_journal.JournalError):
            lifecycle_journal.recover(self.root)

    def test_invocation_correction_is_append_only_and_preserves_attempt_identity(self) -> None:
        lifecycle_journal.record_terminal(self.root, terminal("inv-correct"))
        before = (self.root / "run.events.jsonl").read_bytes()
        result = lifecycle_journal.correct_invocation(
            self.root, "inv-correct", {"output_sha256": "d" * 64},
            reason="final deterministic report projection changed the bound bytes",
        )
        after = (self.root / "run.events.jsonl").read_bytes()
        self.assertTrue(after.startswith(before))
        rows = [json.loads(line) for line in (self.root / "invocations.jsonl").read_text().splitlines()]
        self.assertEqual(rows[0]["output_sha256"], "d" * 64)
        self.assertEqual(rows[0]["attempt"], 1)
        self.assertEqual(rows[0]["journal_event"]["seq"], result["journal_seq"])
        with self.assertRaises(lifecycle_journal.JournalError):
            lifecycle_journal.correct_invocation(
                self.root, "inv-correct", {"attempt": 2}, reason="not allowed",
            )

    def test_cache_reuse_has_its_own_terminal_event_and_no_attempt(self) -> None:
        lifecycle_journal.record_terminal(self.root, terminal("inv-source"))
        replay = terminal("inv-replay")
        replay.update({
            "provider_call": False, "cache_usage": "read", "session_id": None,
            "attempt": None, "source_invocation_id": "inv-source",
        })
        lifecycle_journal.record_cache_reuse(self.root, replay)
        events = lifecycle_journal.load_events(self.root)
        self.assertEqual(events[-1]["type"], "cache_reuse")
        rows = [json.loads(line) for line in (self.root / "invocations.jsonl").read_text().splitlines()]
        projected = next(row for row in rows if row["invocation_id"] == "inv-replay")
        self.assertEqual(projected["attempt_kind"], "cache_replay")
        self.assertIsNone(projected["attempt"])
        self.assertEqual(projected["inference_start_status"], "not_started")

    def test_elapsed_provider_union_wait_and_idle_are_not_conflated(self) -> None:
        first = terminal("inv-duration-a", "duration-a")
        first.update({
            "started_at": "2026-08-30T10:01:00Z", "ended_at": "2026-08-30T10:03:00Z",
            "elapsed_seconds": 120, "provider_duration_seconds": 120,
        })
        second = terminal("inv-duration-b", "duration-b")
        second.update({
            "started_at": "2026-08-30T10:02:00Z", "ended_at": "2026-08-30T10:04:00Z",
            "elapsed_seconds": 120, "provider_duration_seconds": 120,
        })
        lifecycle_journal.record_terminal(self.root, first)
        lifecycle_journal.record_terminal(self.root, second)
        lifecycle_journal.append_event(
            self.root, "retry_backoff_start", {"role": "duration-b"},
            at="2026-08-30T10:04:00Z",
        )
        lifecycle_journal.append_event(
            self.root, "retry_backoff_end", {"role": "duration-b"},
            at="2026-08-30T10:05:00Z",
        )
        run = json.loads((self.root / "run.json").read_text())
        durations = run["durations"]
        self.assertEqual(durations["audit_elapsed_seconds"], 300)
        self.assertEqual(durations["provider_duration_sum_seconds"], 240)
        self.assertEqual(durations["provider_active_interval_union_seconds"], 180)
        self.assertEqual(durations["retry_backoff_seconds"], 60)
        self.assertEqual(durations["idle_seconds"], 60)


class FessiMigrationProjectionTests(unittest.TestCase):
    def test_fessi_projection_retains_38_rows_and_corrects_only_forward(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-fessi-migrate-") as directory:
            root, pdf = test_document_release.audit_fixture(
                pathlib.Path(directory), body_pages=14, back_pages=3, preview=True,
            )
            neutral_pdf = pdf.with_name("historical-review.pdf")
            pdf.rename(neutral_pdf)
            test_document_release.create_manifest(root, neutral_pdf, preview=True)
            neutral_pdf.with_suffix(".manifest.json").rename(
                neutral_pdf.with_name("build-receipt.json")
            )
            run = base_run()
            run.update({
                # the fixture's snapshot is the shipped policy (cap 7); 2.6.9 prepares a round only when the journaled
                # cap (the one recorded at init) matches the snapshot's
                "schema_version": "2.0.0", "round_cap": 7,
                "rounds": [
                    {"round": 1, "started_at": "2026-08-28T10:00:00Z", "ended_at": "2026-08-28T11:00:00Z", "integrated_at": "2026-08-28T12:00:00Z"},
                    {"round": 2, "started_at": "2026-08-29T10:00:00Z", "ended_at": None, "integrated_at": None},
                ],
                "events": [], "invocations": [], "normalization": None,
            })
            (root / "run.json").write_text(json.dumps(run) + "\n")
            template = json.loads((SKILL / "schemas/fixtures/v2_invocation_record.json").read_text())
            rows = []
            for index in range(38):
                row = copy.deepcopy(template)
                row.update({
                    "invocation_id": f"inv-fessi-{index:02d}", "role": f"legacy-role-{index:02d}",
                    # the Fessi audit ran on Terra; the migration's served-identity rule is Terra-specific by design
                    "requested_model": "gpt-5.6-terra", "observed_model": "gpt-5.6-terra",
                    "observed_model_ids": ["gpt-5.6-terra"], "model_family": "gpt",
                    "model_source": "observed", "provider": "openai", "reasoning_effort": "high",
                })
                rows.append(row)
            (root / "invocations.jsonl").write_text("".join(
                json.dumps(row, sort_keys=True) + "\n" for row in rows
            ))
            result = migrate_metadata_2_1.migrate(
                root, fessi=True, expected_invocations=38,
            )
            self.assertEqual(result["provider_calls_during_migration"], 0)
            self.assertFalse(result["round_1_rerun"])
            projected = [json.loads(line) for line in (root / "invocations.jsonl").read_text().splitlines()]
            self.assertEqual(len(projected), 38)
            self.assertTrue(all(row["served_model"] is None for row in projected))
            self.assertTrue(all(row["identity_assurance"] == "requested_only" for row in projected))
            migrated_run = json.loads((root / "run.json").read_text())
            round_two = next(row for row in migrated_run["rounds"] if row["round"] == 2)
            self.assertEqual(round_two["state"], "awaiting_authorization")
            self.assertIsNone(round_two["started_at"])
            self.assertEqual(migrated_run["diagnostic_artifacts"][0]["disposition"], "diagnostic_only")
            self.assertEqual(
                migrated_run["diagnostic_artifacts"][0]["path"],
                "report/previews/historical-review.pdf",
            )
            self.assertIn("exact PDF recount", migrated_run["diagnostic_artifacts"][0]["reason"])
            self.assertFalse(any(
                event["type"] == "round_start" and event["payload"].get("round") == 1
                for event in migrated_run["events"]
            ))
            self.assertFalse(any(
                event["type"] == "round_prepared" and event["payload"].get("round") == 2
                for event in migrated_run["events"]
            ))

            critic = {
                **reservation("inv-fessi-round2", "critic_completeness"),
                "role_class": "critic", "round": 2, "provider": "openai",
                "requested_model": "gpt-5.6-terra", "reasoning_effort": "high",
            }
            with self.assertRaises(lifecycle_journal.AuthorizationRequired):
                lifecycle_journal.reserve_invocation(root, critic)
            before = lifecycle_journal.load_events(root)
            self.assertFalse(any(
                event["type"] == "provider_request"
                and event["payload"].get("invocation_id") == "inv-fessi-round2"
                for event in before
            ))

            plan = {
                "purpose": "Fessi Round-2 delta critique", "provider": "openai",
                "model_route": "gpt-5.6-terra/high",
                "data_classes": ["report", "claims", "evidence"],
                "policy_sha256": "d" * 64,
            }
            prepared = lifecycle_journal.prepare_round(root, 2, plan)
            authorization = {
                key: prepared["plan"][key]
                for key in (
                    "purpose", "round_plan_sha256", "provider", "model_route",
                    "data_classes", "policy_sha256",
                )
            }
            authorization["expires_at"] = "2099-01-01T00:00:00Z"
            lifecycle_journal.authorize_round(root, 2, authorization)
            lifecycle_journal.reserve_invocation(root, critic)
            lifecycle_journal.mark_provider_request(root, "inv-fessi-round2")
            after = lifecycle_journal.load_events(root)
            request_index = next(
                index for index, event in enumerate(after)
                if event["type"] == "provider_request"
                and event["payload"].get("invocation_id") == "inv-fessi-round2"
            )
            self.assertEqual(after[request_index - 1]["type"], "round_start")

    def test_fessi_diagnostic_name_without_manifest_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-fessi-name-only-") as directory:
            root = pathlib.Path(directory)
            path = root / "report/fessi-max14.pdf"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"not a manifest-bound PDF")
            self.assertEqual(migrate_metadata_2_1.diagnostic_artifacts(root), [])


if __name__ == "__main__":
    unittest.main()
