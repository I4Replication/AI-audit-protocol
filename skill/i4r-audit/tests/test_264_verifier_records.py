#!/usr/bin/env python3
"""2.6.4: post-fan-out verifier invocation records, the journal, and finalisation.

The Neudorfer audit (20260923-audit-3309cfcf) excluded three article-text findings (F13-F15) that
every critic had confirmed. They had been raised after fan-out, and before 2.6.4 a family-A
confirmation could come only from the Phase-1 auditor that raised a row. The hand-made records
tried as a workaround were refused one gate after another: role_class ``other`` by make_settled,
and a record relabelled ``role_class auditor`` by finalize, which counted it as a sixth auditor
("canonical auditor quorum differs: missing=[] extra=['auditor_operator_verification']"). The
operator finally corrected all three rows to ``legacy_record``.

These tests cover the records side of the packaged verifier: a ``role_class verifier`` call at
round 0 is routed by policy, listed in compliance apart from the five auditors, admitted only as
the runner's own output, bound to rows by claim hash, and refused at any other round before the
journal is touched. Every verifier call a test journals is made by
``tests/verifier_fixtures.record_verifier``, which records it exactly as scripts/run_verifier.py
does (real lifecycle API, frozen packet, validated promoted tree, the runner's receipt): since
2.6.4 every consumer re-admits a verifier record through ``run_verifier.readmit_record``, so a
hand-written receipt or output tree is refused, and so is the hand record
``verifier_fixtures.forge_hand_record`` makes (the Neudorfer ``make_record.py`` shape).

The 2.6.4 review's records findings are pinned here too, each by building the state on disk and
running the real scripts:

* records-1: a hand-made record is "not the runner's admitted output" at finalize (compliance
  fails, publish is refused), and the raw journal routes that made one (``lifecycle_journal.py
  append-batch``/``append-event``/``append-kv``, log_event.sh, record_invocation.py's lifecycle
  flags, and a synthesized terminal for an unreserved verifier provider call) are refused before
  anything is appended;
* records-2: a verifier row that did not complete must still carry a verifier role, round 0 and
  no critic family;
* records-3: the human-verification roll-up recognises the verifier by role_class, never by a
  ``verifier_`` name, so a person code such as ``verifier_1`` stays human.

With ``I4R_V263_SKILL`` set to a pristine 2.6.3 tree, two goldens pin the no-verifier invariance of
these changes byte for byte: the CLI appends (journal, projections, metrics) and the finalized
comment.json for a ``verifier_``-named person code.

Audits are built with tests.test_release_pipeline.make_audit and judged by the real
finalize_run.sh and publish_completed.sh.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests import verifier_fixtures
from tests.test_release_pipeline import (
    FINALIZE, LOG_EVENT, PUBLISH, SKILL, SUMMARIZE, digest, make_audit, write,
)

sys.path.insert(0, str(SKILL / "scripts"))
import lifecycle_journal  # noqa: E402
import record_invocation  # noqa: E402
import verifier_contract  # noqa: E402

LIFECYCLE = SKILL / "scripts/lifecycle_journal.py"
RECORD = SKILL / "scripts/record_invocation.py"
PRISTINE = Path(os.environ["I4R_V263_SKILL"]).resolve() if os.environ.get("I4R_V263_SKILL") else None

# The compliance keys finalize_run.sh 2.6.3 wrote, in its order. An audit without a verifier row
# must keep exactly these (no-verifier invariance).
# The journal projection writes them sorted.
COMPLIANCE_KEYS_263 = sorted([
    "phases_ran", "all_five_auditors_ran", "critique_integrated_before_delivery",
    "rounds_integrated_before_delivery", "rounds_not_integrated", "converged",
    "critics_independent_all_rounds", "no_silent_model_swap", "gates_run",
    "consistency_check_after_every_round", "compliance_pass", "violations",
])
VERIFIER_KEYS = {"verifier_calls", "verifiers_ok"}
POST_FAN_OUT_CLAIM = "The abstract reports 24 participants; the participant table lists 22."
POST_FAN_OUT_ROLE = verifier_contract.batch_role([verifier_contract.claim_sha256(POST_FAN_OUT_CLAIM)])
# make_audit's round 1 is integrated at 10:30; the verifier ran before that integration.
T_VERIFIER = ("2026-08-27T10:21:00Z", "2026-08-27T10:25:00Z")
SONNET = {"provider": "anthropic", "requested_model": "sonnet-5",
          "served_model": "claude-sonnet-5-20260801", "model_family": "claude",
          "reasoning_effort": "max"}
NOT_THE_RUNNERS = "is not the runner's admitted output"


def claude_route(audit: Path) -> dict[str, Any]:
    """SONNET's shape on the verifier route this audit's own snapshot froze (2.7.0: Sonnet 5.5 for
    make_audit, which initialises from the shipped policy). SONNET stays the Sonnet 5 route of the
    journal-only shapes, which the journal guards refuse whatever model they name."""
    return {**verifier_fixtures.audit_route(audit), "model_family": "claude"}


def critic_family_route(audit: Path) -> dict[str, Any]:
    """The critic route this audit's own snapshot froze, in the same shape (2.7.0: gpt-6.1-sol for make_audit)."""
    return {**verifier_fixtures.critic_route(audit), "model_family": "gpt"}


def rows_of(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def usage() -> dict[str, Any]:
    return {"obtainable": True, "input_tokens": 100, "output_tokens": 20,
            "cached_input_tokens": 10, "reasoning_tokens": 5, "total_tokens": 125,
            "source": "api_response"}


def record_verifier(audit: Path, claims: list[str], **options: Any) -> dict[str, Any]:
    """One verifier call exactly as scripts/run_verifier.py records it (the fixture helper), at
    T_VERIFIER unless the test names other times."""
    options.setdefault("started_at", T_VERIFIER[0])
    options.setdefault("ended_at", T_VERIFIER[1])
    return verifier_fixtures.record_verifier(audit, claims, **options)


def legacy_terminal_record(invocation_id: str, role: str, role_class: str, round_number: int, *,
                           output: str | None, output_sha256: str | None, started_at: str,
                           ended_at: str, route: dict[str, Any] = SONNET, status: str = "completed",
                           input_hash: str = "f" * 64) -> dict[str, Any]:
    """A terminal row for a NON-runner call: the Neudorfer out-of-protocol shapes (role_class
    other/auditor) and the refused inputs of the journal guards. Never admitted as a verifier."""
    return {
        "invocation_id": invocation_id, "role": role, "role_class": role_class,
        "round": round_number, "attempt": 1, "attempt_kind": "role_inference",
        "status": status, "provider": route["provider"], "provider_call": True,
        "cache_usage": "none", "session_id": f"session-{invocation_id}",
        "requested_model": route["requested_model"], "observed_model": route["served_model"],
        "served_model": route["served_model"], "observed_model_ids": [route["served_model"]],
        "model_family": route["model_family"], "model_source": "observed",
        "identity_assurance": "observed", "reasoning_effort": route["reasoning_effort"],
        "started_at": started_at, "ended_at": ended_at,
        "elapsed_seconds": int((lifecycle_journal.parse_time(ended_at)
                                - lifecycle_journal.parse_time(started_at)).total_seconds()),
        "input_artifact_sha256": input_hash, "prompt_sha256": "e" * 64,
        "output_artifact": output, "output_sha256": output_sha256,
        "usage": usage(), "error": None if status == "completed" else "provider timed out",
        "error_class": None if status == "completed" else "transient", "legacy_source": None,
    }


def forged_batch(role: str, role_class: str = "verifier", *, invocation_id: str = "hand-verifier-1",
                 round_number: int = 0, status: str = "completed", route: dict[str, Any] = SONNET,
                 output: str | None = None, output_sha256: str | None = None,
                 reservation_class: str | None = None) -> list[dict[str, Any]]:
    """The four raw lifecycle events the review's forge.py appended with ``append-batch``
    (reservation, provider request, inference start, terminal)."""
    at0, at1 = T_VERIFIER
    record = legacy_terminal_record(invocation_id, role, role_class, round_number, output=output,
                                    output_sha256=output_sha256, started_at=at0, ended_at=at1,
                                    route=route, status=status, input_hash="c" * 64)
    record.update({"provider_request_status": "accepted", "inference_start_status": "started",
                   "source_invocation_id": None, "turn_count": None, "provider_cost": None,
                   "model_identity_evidence": None, "usage_by_model": [], "unattributed_usage": None,
                   "provider_duration_seconds": record["elapsed_seconds"], "schema_digest": None})
    return [
        {"type": "invocation_reserved", "at": at0, "payload": {
            "invocation_id": invocation_id, "role": role,
            "role_class": reservation_class if reservation_class is not None else role_class,
            "round": round_number, "attempt_kind": "role_inference", "input_artifact_sha256": "c" * 64,
            "prompt_sha256": "e" * 64, "attempt": None, "provider_request_status": "not_requested",
            "inference_start_status": "not_started", "provider": route["provider"],
            "requested_model": route["requested_model"], "reasoning_effort": route["reasoning_effort"]}},
        {"type": "provider_request", "at": at0,
         "payload": {"invocation_id": invocation_id, "status": "accepted"}},
        {"type": "inference_start", "at": at0, "payload": {"invocation_id": invocation_id, "attempt": 1}},
        {"type": "invocation_terminal", "at": at1,
         "payload": {"invocation_id": invocation_id, "record": record}},
    ]


def verification_of(info: dict[str, Any], outcome: str = "confirmed") -> dict[str, Any]:
    return {"reviewer_id": info["role"], "invocation_id": info["invocation_id"],
            "evidence_record": {"path": info["result"], "sha256": info["result_sha256"]},
            "model_family": "claude", "outcome": outcome}


def raised_after_fan_out(row: dict[str, Any], origin: str = "coordinator") -> None:
    row["provenance"] = {"origin_role": origin, "independently_found_by": [origin],
                         "agreed_after_seeing": [], "round_introduced": 0,
                         "source_records": ["report/REPORT_final.md"]}


class _AuditCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-264-verifier-")
        self.root = Path(self.temporary.name)
        self.environment = os.environ.copy()
        self.environment.update({"LC_ALL": "C", "I4R_RESERVE_GRACE": "0"})
        self.environment.pop("I4R_MAX_ATTEMPTS", None)

    def tearDown(self) -> None:
        # Post-delta snapshots and pkg/ are read-only by design; restore write for cleanup.
        subprocess.run(["chmod", "-R", "u+w", str(self.root)], check=False)
        self.temporary.cleanup()

    def bash(self, script: Path, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/bin/bash", str(script), *(str(value) for value in arguments)],
                              text=True, capture_output=True, timeout=120, env=self.environment)

    def python(self, script: Path, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(script), *(str(value) for value in arguments)],
                              text=True, capture_output=True, timeout=120, env=self.environment)

    def finalize(self, audit: Path) -> subprocess.CompletedProcess[str]:
        # finalize writes only into an existing registry (a scratch one here, outside any tree)
        (self.root / "_registry").mkdir(parents=True, exist_ok=True)
        return self.bash(FINALIZE, audit, "--stopping-reason", "target_complete",
                         "--registry", self.root / "_registry")

    def publish(self, audit: Path) -> subprocess.CompletedProcess[str]:
        # the fixture audit is not inside a discipline tree; publish into the scratch folder anyway
        return subprocess.run(["/bin/bash", str(PUBLISH), str(audit), str(self.root / "completed")],
                              text=True, capture_output=True, timeout=120,
                              env={**self.environment, "I4R_PUBLISH_ANY_ROOT": "1"})

    def compliance(self, audit: Path) -> dict[str, Any]:
        return json.loads((audit / "run.json").read_text())["compliance"]

    def policy(self, audit: Path) -> dict[str, Any]:
        return record_invocation.load_policy_snapshot(audit)

    def journal_state(self, audit: Path) -> list[bytes]:
        return [(audit / name).read_bytes()
                for name in ("run.events.jsonl", "run.json", "invocations.jsonl")]

    def assert_refused_before_append(self, audit: Path, result: subprocess.CompletedProcess[str],
                                     before: list[bytes], label: str) -> None:
        self.assertNotEqual(result.returncode, 0, f"{label}: {result.stdout}{result.stderr}")
        self.assertIn("scripts/run_verifier.py", result.stderr, label)
        self.assertEqual(self.journal_state(audit), before,
                         f"{label}: a refused raw verifier event changed the journal or a projection")


class VerifierRecordsTest(_AuditCase):
    # ------------------------------------------------------------------ finalize-level positive
    def test_verifier_call_is_listed_apart_from_the_five_auditors_and_releases(self) -> None:
        audit = make_audit(self.root)
        info = record_verifier(audit, [POST_FAN_OUT_CLAIM], finding_ids=["F3"])
        result = self.finalize(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        compliance = self.compliance(audit)
        self.assertEqual(compliance["violations"], [])
        self.assertTrue(compliance["compliance_pass"])
        self.assertTrue(compliance["all_five_auditors_ran"])
        self.assertTrue(compliance["critics_independent_all_rounds"])
        self.assertTrue(compliance["verifiers_ok"])
        self.assertEqual(compliance["verifier_calls"], [{
            "invocation_id": info["invocation_id"], "role": info["role"], "round": 0,
            "model_family": "claude", "claim_sha256s": info["claim_sha256s"], "cited_by": [],
        }])
        self.assertEqual(list(compliance), sorted([*COMPLIANCE_KEYS_263, *VERIFIER_KEYS]))
        metrics = json.loads((audit / "run_metrics.json").read_text())
        self.assertTrue(metrics["phase_one"]["exact_quorum"])
        self.assertEqual(metrics["phase_one"]["extra_completed_auditor_roles"], [])
        publish = self.publish(audit)
        self.assertEqual(publish.returncode, 0, publish.stdout + publish.stderr)

    def test_confirmed_verifier_promotes_a_row_raised_after_fan_out(self) -> None:
        """The whole Neudorfer path: a coordinator-raised row confirmed by the verifier (family A)
        and a critic (family B) is promoted, released, and counts no human verification."""
        recorded: dict[str, Any] = {}

        def verify_f1(audit: Path, finding: dict[str, Any]) -> None:
            info = record_verifier(audit, [finding["claim"]], finding_ids=["F1"])
            recorded.update(info)
            raised_after_fan_out(finding)
            critic = [v for v in finding["verifications"] if v["reviewer_id"] == "critic_stats"]
            finding["verifications"] = [verification_of(info), *critic]

        audit = make_audit(self.root, before_registry=verify_f1)
        compliance = self.compliance(audit)
        self.assertEqual(compliance["violations"], [])
        self.assertTrue(compliance["compliance_pass"])
        self.assertTrue(compliance["all_five_auditors_ran"])
        self.assertEqual(compliance["verifier_calls"], [{
            "invocation_id": recorded["invocation_id"], "role": recorded["role"], "round": 0,
            "model_family": "claude", "claim_sha256s": recorded["claim_sha256s"], "cited_by": ["F1"],
        }])
        settled = json.loads((audit / "SETTLED.json").read_text())
        fact = next(item for item in settled["facts"] if item["id"] == "F1")
        self.assertEqual({item["reviewer_id"] for item in fact["confirmations"]},
                         {recorded["role"], "critic_stats"})
        comment = json.loads((audit / "comment.json").read_text())
        verifiers = [item["verifier"] for item in comment["findings"][0]["verification"]["verifiers"]]
        self.assertIn(recorded["role"], verifiers)
        # records-3: counted as a model because it is the role of a role_class verifier call.
        self.assertEqual(comment["rollup"]["n_human_verified"], 0)
        publish = self.publish(audit)
        self.assertEqual(publish.returncode, 0, publish.stdout + publish.stderr)

    # ------------------------------------------------------------------ finalize-level negatives
    def test_critic_family_verifier_is_refused(self) -> None:
        audit = make_audit(self.root)
        info = record_verifier(audit, [POST_FAN_OUT_CLAIM], family="gpt")
        result = self.finalize(audit)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("recorded request violates pipeline policy", result.stdout + result.stderr)
        self.assertIn("FINALIZE FAILED", result.stdout + result.stderr)
        publish = self.publish(audit)
        self.assertNotEqual(publish.returncode, 0, publish.stdout + publish.stderr)
        # finalize never reaches its compliance vector here (the route check refuses the ledger
        # first), so judge the same on-disk state with the checks derive_run calls.
        rows = rows_of(audit / "invocations.jsonl")
        listing, violations = record_invocation.verifier_compliance(
            audit, rows, rows_of(audit / "work/findings.jsonl"), self.policy(audit))
        self.assertEqual([item["invocation_id"] for item in listing], [info["invocation_id"]])
        self.assertTrue(any("is not the Phase-1 auditor family ['claude']" in item
                            for item in violations), violations)
        self.assertTrue(any("is also a critic family ['gpt']" in item for item in violations),
                        violations)
        self.assertEqual(listing[0]["claim_sha256s"], [])

    def test_verifier_without_the_runner_receipt_is_not_admitted(self) -> None:
        audit = make_audit(self.root)
        # A twin of the audit, taken before the call, records the identical call (same id, same
        # times, same bytes) WITH the receipt: that receipt is exactly the one the runner writes.
        twin = self.root / "twin" / audit.relative_to(self.root)
        shutil.copytree(audit, twin, symlinks=True)
        info = record_verifier(audit, [POST_FAN_OUT_CLAIM], receipt=False,
                               invocation_id="inv-verifier-lost-receipt")
        twin_info = record_verifier(twin, [POST_FAN_OUT_CLAIM], invocation_id="inv-verifier-lost-receipt")
        self.assertEqual((twin_info["input_hash"], twin_info["result_sha256"]),
                         (info["input_hash"], info["result_sha256"]))
        receipt = verifier_contract.completion_receipt_path(info["role"], info["input_hash"])
        self.assertFalse((audit / receipt).exists())
        result = self.finalize(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        compliance = self.compliance(audit)
        self.assertFalse(compliance["compliance_pass"])
        self.assertFalse(compliance["verifiers_ok"])
        self.assertTrue(compliance["all_five_auditors_ran"])
        self.assertEqual(compliance["verifier_calls"][0]["invocation_id"], info["invocation_id"])
        self.assertEqual(compliance["verifier_calls"][0]["claim_sha256s"], [])
        self.assertTrue(any(
            item.startswith(f"verifier record {info['invocation_id']}:")
            and "verifier completion receipt is not a regular file" in item
            for item in compliance["violations"]), compliance["violations"])
        publish = self.publish(audit)
        self.assertNotEqual(publish.returncode, 0, publish.stdout + publish.stderr)
        self.assertIn("not publication-compliant", publish.stdout + publish.stderr)
        # The negative's control: the runner's receipt for this very call repairs it.
        (audit / receipt).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(twin / receipt, audit / receipt)
        result = self.finalize(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.compliance(audit)["compliance_pass"], self.compliance(audit)["violations"])

    def test_verifier_recorded_as_an_auditor_is_still_an_extra_auditor(self) -> None:
        with self.subTest("round 2, the Neudorfer auditor_operator_verification shape"):
            audit = make_audit(self.root / "round-2")
            output = "work/coordinator/verifier_r2/output/verification_result.json"
            write(audit / output, json.dumps({"verifications": [
                {"claim_id": "C1", "verdict": "CONFIRMED", "evidence": "quoted text",
                 "notes": None}]}) + "\n")
            # The synthesized lifecycle is how that row was appended: no reservation gate.
            lifecycle_journal.record_terminal(audit, legacy_terminal_record(
                "masquerade-round-2", POST_FAN_OUT_ROLE, "auditor", 2, output=output,
                output_sha256=digest(audit / output), started_at="2026-08-27T10:40:00Z",
                ended_at="2026-08-27T10:44:00Z"))
            result = self.finalize(audit)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            compliance = self.compliance(audit)
            self.assertFalse(compliance["compliance_pass"])
            self.assertFalse(compliance["all_five_auditors_ran"])
            self.assertIn(f"canonical auditor quorum differs: missing=[] extra=['{POST_FAN_OUT_ROLE}']",
                          compliance["violations"])
            # Keyed on role_class, never on the verifier_ name: nothing is listed as a verifier.
            self.assertEqual(list(compliance), COMPLIANCE_KEYS_263)
        with self.subTest("round 0, through the real reservation"):
            audit = make_audit(self.root / "round-0")
            info = record_verifier(audit, [POST_FAN_OUT_CLAIM], relabel=(POST_FAN_OUT_ROLE, "auditor"))
            self.assertEqual(info["role"], POST_FAN_OUT_ROLE)
            result = self.finalize(audit)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("FINALIZE FAILED — Phase-1 fan-out evidence", result.stdout + result.stderr)
            summary = subprocess.run(
                ["python3", str(SUMMARIZE), str(audit), "--check",
                 "--json", str(self.root / "masquerade.json"),
                 "--markdown", str(self.root / "masquerade.md")],
                text=True, capture_output=True, timeout=60, env=self.environment)
            self.assertNotEqual(summary.returncode, 0, summary.stdout + summary.stderr)
            self.assertIn("Phase-1 completed auditor invocations do not form the exact five-role "
                          "quorum", summary.stdout + summary.stderr)
            self.assertIn(f"extra_roles=['{info['role']}']", summary.stdout + summary.stderr)

    # -------------------------------------------------------------------------- the round gate
    def test_verifier_off_round_zero_is_refused_before_any_append(self) -> None:
        audit = make_audit(self.root)
        journal = audit / "run.events.jsonl"
        before = self.journal_state(audit)
        reservation = {
            "role": POST_FAN_OUT_ROLE, "role_class": "verifier", "round": 2,
            "attempt_kind": "role_inference", "provider": "anthropic",
            "requested_model": "sonnet-5", "reasoning_effort": "max",
            "input_artifact_sha256": "b" * 64, "prompt_sha256": "e" * 64,
        }

        def verifier_terminal(invocation_id: str, round_number: int) -> dict[str, Any]:
            return legacy_terminal_record(invocation_id, POST_FAN_OUT_ROLE, "verifier", round_number,
                                          output=None, output_sha256=None,
                                          started_at="2026-08-27T10:40:00Z",
                                          ended_at="2026-08-27T10:44:00Z", input_hash="b" * 64)

        for label, value in (("round 2", 2), ("round 1", 1), ("round null", None),
                             ("round True", True)):
            with self.subTest(label):
                with self.assertRaises(lifecycle_journal.JournalError) as caught:
                    record_invocation.reserve_invocation(audit, dict(reservation, round=value))
                self.assertNotIsInstance(caught.exception, lifecycle_journal.AuthorizationRequired)
                self.assertEqual(str(caught.exception),
                                 "a verifier invocation must be recorded at round 0")
        with self.subTest("synthesized terminal at round 2"):
            with self.assertRaises(lifecycle_journal.JournalError) as caught:
                lifecycle_journal.record_terminal(audit, verifier_terminal("verifier-synth-round-2", 2))
            self.assertEqual(str(caught.exception),
                             "a verifier invocation must be recorded at round 0")
        with self.subTest("synthesized terminal for an unreserved round-0 provider call"):
            # records-1: synthesizing a reservation/request/start for a verifier provider call
            # makes a lifecycle the runner never journaled.
            with self.assertRaises(lifecycle_journal.JournalError) as caught:
                lifecycle_journal.record_terminal(audit, verifier_terminal("verifier-synth-round-0", 0))
            self.assertEqual(str(caught.exception), lifecycle_journal.VERIFIER_UNRESERVED_ERROR)
            self.assertIn("scripts/run_verifier.py", str(caught.exception))
        self.assertEqual(self.journal_state(audit), before,
                         "a refused verifier reservation changed the journal or a projection")
        run = json.loads((audit / "run.json").read_text())
        self.assertNotEqual(run.get("status"), "paused")
        self.assertEqual([row["round"] for row in run["rounds"]], [1, 2])
        self.assertFalse(any(row.get("state") == "awaiting_authorization" for row in run["rounds"]))
        # A round-0 reservation is accepted, and its terminal record cannot move it to round 2.
        reserved = record_invocation.reserve_invocation(audit, dict(reservation, round=0))
        self.assertEqual(reserved["attempt_candidate"], 1)
        after_reserve = journal.read_bytes()
        with self.assertRaises(lifecycle_journal.JournalError) as caught:
            lifecycle_journal.record_terminal(
                audit, verifier_terminal(reserved["invocation_id"], 2), synthesize_lifecycle=False)
        self.assertEqual(str(caught.exception), "a verifier invocation must be recorded at round 0")
        self.assertEqual(journal.read_bytes(), after_reserve)
        # Negative control: the same reservation under any other role_class still meets the
        # critique-round authorization gate, which pauses the run. That is what the refusal
        # above keeps a misconfigured verifier from doing.
        with self.assertRaises(lifecycle_journal.AuthorizationRequired):
            record_invocation.reserve_invocation(audit, dict(reservation, role_class="other"))
        run = json.loads((audit / "run.json").read_text())
        self.assertEqual(run.get("status"), "paused")

    # --------------------------------------------------------------------- claim-hash binding
    def test_confirmation_scope_is_the_claim_text_the_verifier_confirmed(self) -> None:
        """finalize's scope rule, run on the ledger and registry on disk. (A registry holding an
        out-of-scope verifier confirmation cannot be carried through make_settled and the
        post-delta gates to finalize, so the rule is exercised through the function derive_run
        calls, on real records.)"""
        audit = make_audit(self.root)
        registry = rows_of(audit / "work/findings.jsonl")
        f1, f2 = registry
        confirmed = record_verifier(audit, [f1["claim"]], finding_ids=["F1"])
        policy = self.policy(audit)

        def check(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
            result = record_invocation.verifier_compliance(
                audit, rows_of(audit / "invocations.jsonl"), rows, policy)
            self.assertIsNotNone(result)
            return result

        positive = copy.deepcopy(f1)
        raised_after_fan_out(positive)
        positive["verifications"] = [verification_of(confirmed), positive["verifications"][1]]
        listing, violations = check([positive, f2])
        self.assertEqual(violations, [])
        self.assertEqual(listing[0]["cited_by"], ["F1"])
        self.assertEqual(listing[0]["claim_sha256s"], [verifier_contract.claim_sha256(f1["claim"])])

        other_row = copy.deepcopy(f2)
        raised_after_fan_out(other_row)
        other_row["verifications"] = [verification_of(confirmed)]
        cases = {
            "cited on a finding whose claim it did not confirm": (
                [positive, other_row], "finding F2 cites verifier call",
                "returned no verdict for this row's current claim text"),
            "claim edited after the verifier ran": (
                [dict(positive, claim=f1["claim"] + " (edited)"), f2],
                "finding F1 cites verifier call",
                "returned no verdict for this row's current claim text"),
            "row raised by a Phase-1 auditor": (
                [dict(positive, provenance=f1["provenance"]), f2], "finding F1 cites verifier call",
                "raised by a Phase-1 auditor"),
        }
        for label, (rows, prefix, reason) in cases.items():
            with self.subTest(label):
                _, violations = check(rows)
                self.assertTrue(any(item.startswith(prefix) and reason in item
                                    for item in violations), violations)
        # A citation that is not a confirmation is listed but is not a confirmation to judge.
        listing, violations = check([positive, dict(other_row, verifications=[
            verification_of(confirmed, "inconclusive")])])
        self.assertEqual(violations, [])
        self.assertEqual(listing[0]["cited_by"], ["F1", "F2"])
        # A cache replay of the call resolves to it. It is synthesized by record_terminal (no
        # provider call), which the unreserved-verifier refusal deliberately leaves open.
        replay = record_invocation.record_cache_reuse(audit, {
            **{key: value for key, value in rows_of(audit / "invocations.jsonl")[-1].items()
               if key not in {"journal_event"}},
            "invocation_id": "verifier-cache-replay", "source_invocation_id": confirmed["invocation_id"],
            "cache_usage": "read", "usage": usage(),
        })["record"]
        replayed = copy.deepcopy(positive)
        replayed["verifications"][0]["invocation_id"] = replay["invocation_id"]
        listing, violations = check([replayed, f2])
        self.assertEqual(violations, [])
        self.assertEqual([item["invocation_id"] for item in listing], [confirmed["invocation_id"]])
        self.assertEqual(listing[0]["cited_by"], ["F1"])
        # One verdict per claim text: a second call that REFUTED the same claim blocks it.
        refuting = record_verifier(audit, [f1["claim"], POST_FAN_OUT_CLAIM],
                                   verdicts=["REFUTED", "CONFIRMED"],
                                   started_at="2026-08-27T10:26:00Z",
                                   ended_at="2026-08-27T10:28:00Z")
        listing, violations = check([positive, f2])
        self.assertTrue(any("another verifier call returned REFUTED for the same claim text" in item
                            for item in violations), violations)
        self.assertEqual(len(listing), 2)
        cited_refutation = copy.deepcopy(positive)
        cited_refutation["verifications"][0] = verification_of(refuting)
        _, violations = check([cited_refutation, f2])
        self.assertTrue(any("the cited verifier call returned REFUTED for this claim" in item
                            for item in violations), violations)

    # ------------------------------------------------------------------- no-verifier invariance
    def test_audit_without_a_verifier_keeps_the_2_6_3_compliance_keys(self) -> None:
        audit = make_audit(self.root)
        compliance = self.compliance(audit)
        self.assertEqual(list(compliance), COMPLIANCE_KEYS_263)
        self.assertTrue(compliance["compliance_pass"], compliance["violations"])
        rows = rows_of(audit / "invocations.jsonl")
        self.assertIsNone(record_invocation.verifier_compliance(
            audit, rows, rows_of(audit / "work/findings.jsonl"), self.policy(audit)))
        # Negative control: the first verifier row, and only that, adds the two optional keys.
        record_verifier(audit, [POST_FAN_OUT_CLAIM])
        result = self.finalize(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(self.compliance(audit)) - set(COMPLIANCE_KEYS_263), VERIFIER_KEYS)

    def test_neudorfer_legacy_record_shape_still_finalizes_clean(self) -> None:
        audit = make_audit(self.root)
        output = audit / "work/coordinator/verifier_r2/output/verification_result.json"
        write(output, json.dumps({"verifications": [
            {"claim_id": f"C{index}", "verdict": "CONFIRMED", "evidence": "quoted text",
             "notes": None} for index in (1, 2, 3)]}) + "\n")
        hand_made = [
            ("operator-verifier-fixture", "operator_verifier", "other", 1),
            ("auditor-operator-verification-fixture", "auditor_operator_verification", "auditor", 2),
        ]
        for invocation_id, role, role_class, round_number in hand_made:
            lifecycle_journal.record_terminal(audit, legacy_terminal_record(
                invocation_id, role, role_class, round_number,
                output=output.relative_to(audit).as_posix(), output_sha256=digest(output),
                started_at="2026-08-27T10:31:00Z", ended_at="2026-08-27T10:33:00Z"))
        # Before the correction the relabelled row is the sixth auditor finalize refused.
        result = self.finalize(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        compliance = self.compliance(audit)
        self.assertFalse(compliance["compliance_pass"])
        self.assertIn("canonical auditor quorum differs: missing=[] "
                      "extra=['auditor_operator_verification']", compliance["violations"])
        for invocation_id, *_ in hand_made:
            record_invocation.correct_invocation(
                audit, invocation_id, {"status": "legacy_record"},
                reason="Out-of-protocol operator verification call (a role outside the Phase-1 "
                       "auditor quorum); superseded")
        result = self.finalize(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        compliance = self.compliance(audit)
        self.assertTrue(compliance["compliance_pass"], compliance["violations"])
        self.assertEqual(list(compliance), COMPLIANCE_KEYS_263)
        summary = subprocess.run(["python3", str(SUMMARIZE), str(audit), "--check",
                                  "--json", str(self.root / "neudorfer.json"),
                                  "--markdown", str(self.root / "neudorfer.md")],
                                 text=True, capture_output=True, timeout=60, env=self.environment)
        self.assertEqual(summary.returncode, 0, summary.stdout + summary.stderr)


class HandMadeRecordsAreRefused(_AuditCase):
    """records-1: a hand-made verifier record passed make_settled, finalize and publish, and the
    raw journal routes that made one were open."""

    def test_a_hand_made_record_is_not_the_runners_admitted_output(self) -> None:
        """Before re-admission this record finalized compliance_pass True, verifiers_ok True, with
        its claim listed as confirmed, and published (the review's forge.py, nocite variant)."""
        audit = make_audit(self.root)
        hand = verifier_fixtures.forge_hand_record(audit, POST_FAN_OUT_CLAIM,
                                                   started_at=T_VERIFIER[0], ended_at=T_VERIFIER[1])
        receipt = audit / verifier_contract.completion_receipt_path(hand["role"], hand["input_hash"])
        self.assertTrue(receipt.is_file(), "the hand record carries its own hand-written receipt")
        result = self.finalize(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        compliance = self.compliance(audit)
        self.assertFalse(compliance["compliance_pass"])
        self.assertFalse(compliance["verifiers_ok"])
        self.assertTrue(compliance["all_five_auditors_ran"])
        self.assertEqual([(item["invocation_id"], item["claim_sha256s"])
                          for item in compliance["verifier_calls"]], [(hand["invocation_id"], [])])
        self.assertTrue(any(item.startswith(f"verifier record {hand['invocation_id']}:")
                            and NOT_THE_RUNNERS in item for item in compliance["violations"]),
                        compliance["violations"])
        self.assertIn(NOT_THE_RUNNERS, result.stdout)
        publish = self.publish(audit)
        self.assertNotEqual(publish.returncode, 0, publish.stdout + publish.stderr)
        self.assertIn("not publication-compliant", publish.stdout + publish.stderr)
        self.assertFalse((self.root / "completed").exists() and any((self.root / "completed").rglob("run.json")),
                         "a refused publication left a published run behind")

    def test_a_hand_made_record_cited_on_a_row_never_promotes_it(self) -> None:
        """The review's forge.py cite variant: the hand record cited as F1's family-A confirmation.
        Before re-admission F1 settled on ['critic_stats', 'verifier_...'] and published."""
        forged: dict[str, Any] = {}

        def forge_and_cite(audit: Path, finding: dict[str, Any]) -> None:
            hand = verifier_fixtures.forge_hand_record(audit, finding["claim"],
                                                       started_at=T_VERIFIER[0], ended_at=T_VERIFIER[1])
            forged.update(hand)
            raised_after_fan_out(finding)
            critic = [v for v in finding["verifications"] if v["reviewer_id"] == "critic_stats"]
            finding["verifications"] = [{
                "reviewer_id": hand["role"], "invocation_id": hand["invocation_id"],
                "evidence_record": {"path": hand["result"], "sha256": hand["result_sha256"]},
                "model_family": "claude", "outcome": "confirmed"}, *critic]

        # make_settled either stops on the inadmissible record or rejects the citing row (and the
        # fixture's round-2 diff then misses F1); either way F1 is never promoted on it.
        with self.assertRaises(AssertionError) as caught:
            make_audit(self.root, before_registry=forge_and_cite)
        audit = self.root / "Discipline" / "Journal-Sanitized" / "Paper-Sanitized" / "audit"
        evidence = str(caught.exception)
        for name in ("SETTLED.json", "SETTLED.md"):
            if (audit / name).is_file():
                evidence += (audit / name).read_text(encoding="utf-8")
        self.assertIn(NOT_THE_RUNNERS, evidence)
        if (audit / "SETTLED.json").is_file():
            settled = json.loads((audit / "SETTLED.json").read_text())
            self.assertNotIn("F1", [fact["id"] for fact in settled.get("facts") or []])

    def test_raw_cli_verifier_lifecycle_is_refused_before_any_append(self) -> None:
        audit = make_audit(self.root)
        work = self.root / "raw"
        work.mkdir()

        def batch(name: str, events: list[dict[str, Any]]) -> Path:
            path = work / f"{name}.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            return path

        def payload(name: str, value: dict[str, Any]) -> Path:
            path = work / f"{name}.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            return path

        forge = forged_batch(POST_FAN_OUT_ROLE)
        cases: list[tuple[str, list[object]]] = [
            ("append-batch: the review's forge.py events",
             ["append-batch", audit, batch("forge", forge)]),
            ("append-batch: only payload.record names the verifier",
             ["append-batch", audit, batch("record-only", forged_batch(
                 POST_FAN_OUT_ROLE, invocation_id="hand-verifier-2", reservation_class="other"))]),
            ("append-batch: a failed round-3 verifier (the records-2 forge)",
             ["append-batch", audit, batch("round-3", forged_batch(
                 POST_FAN_OUT_ROLE, invocation_id="hand-verifier-3", round_number=3, status="failed"))]),
            ("append-event: an unreserved verifier terminal",
             ["append-event", audit, "invocation_terminal", payload("terminal", forge[3]["payload"])]),
            ("append-kv: a verifier reservation",
             ["append-kv", audit, "invocation_reserved", "invocation_id=hand-verifier-4",
              f"role={POST_FAN_OUT_ROLE}", "role_class=verifier", "round=0",
              "attempt_kind=role_inference", "provider=anthropic", "requested_model=sonnet-5",
              "reasoning_effort=max"]),
        ]
        for label, arguments in cases:
            with self.subTest(label):
                before = self.journal_state(audit)
                self.assert_refused_before_append(audit, self.python(LIFECYCLE, *arguments), before, label)
        with self.subTest("log_event.sh --batch with the forge.py events"):
            # log_event.sh (append-kv / append-batch underneath) admits no lifecycle type at all;
            # pinned so that widening its type list cannot reopen the route unnoticed.
            before = self.journal_state(audit)
            logged = subprocess.run(
                ["/bin/bash", str(LOG_EVENT), "--run", str(audit / "run.json"),
                 "--batch", str(batch("log-event", forge))],
                text=True, capture_output=True, timeout=120, env=self.environment)
            self.assertNotEqual(logged.returncode, 0, logged.stdout + logged.stderr)
            self.assertEqual(self.journal_state(audit), before)

        # Through an existing reservation: the runner's reservation (the Python lifecycle API) is
        # accepted; a raw CLI event for that invocation id is not.
        reserved = record_invocation.reserve_invocation(audit, {
            "role": POST_FAN_OUT_ROLE, "role_class": "verifier", "round": 0,
            "attempt_kind": "role_inference", "provider": "anthropic", "requested_model": "sonnet-5",
            "reasoning_effort": "max", "input_artifact_sha256": "d" * 64, "prompt_sha256": "e" * 64,
        })["invocation_id"]
        for label, arguments in (
                ("provider_request", ["append-kv", audit, "provider_request",
                                      f"invocation_id={reserved}", "status=accepted"]),
                ("inference_start", ["append-kv", audit, "inference_start",
                                     f"invocation_id={reserved}", "attempt=1"]),
                ("provider_response", ["append-kv", audit, "provider_response",
                                       f"invocation_id={reserved}", "artifact=work/x.json",
                                       f"sha256={'a' * 64}"])):
            with self.subTest(f"reserved verifier: {label}"):
                before = self.journal_state(audit)
                self.assert_refused_before_append(audit, self.python(LIFECYCLE, *arguments), before, label)

        # Through a projected row: a call recorded exactly as the runner records it.
        info = record_verifier(audit, [POST_FAN_OUT_CLAIM])
        for label, arguments in (
                ("invocation_correction", ["append-kv", audit, "invocation_correction",
                                           f"invocation_id={info['invocation_id']}",
                                           'set:={"status": "legacy_record"}', "reason=superseded"]),
                ("cache_reuse of the call under another role_class", [
                    "append-event", audit, "cache_reuse", payload("replay", {
                        "invocation_id": "raw-replay-1", "record": {
                            **legacy_terminal_record("raw-replay-1", POST_FAN_OUT_ROLE, "other", 0,
                                                     output=info["result"],
                                                     output_sha256=info["result_sha256"],
                                                     started_at=T_VERIFIER[1], ended_at=T_VERIFIER[1]),
                            "provider_call": False, "attempt_kind": "cache_replay", "attempt": None,
                            "source_invocation_id": info["invocation_id"]}})])):
            with self.subTest(f"recorded verifier: {label}"):
                before = self.journal_state(audit)
                self.assert_refused_before_append(audit, self.python(LIFECYCLE, *arguments), before, label)

        # Controls. (1) The documented correction, record_invocation.py --correct, still works on
        # a verifier call and is what the refusal names.
        correction = payload("correction", {"set": {"status": "legacy_record"},
                                            "reason": "superseded: re-run under a new claim text"})
        corrected = self.python(RECORD, "--correct", audit, info["invocation_id"], correction)
        self.assertEqual(corrected.returncode, 0, corrected.stdout + corrected.stderr)
        row = next(item for item in rows_of(audit / "invocations.jsonl")
                   if item["invocation_id"] == info["invocation_id"])
        self.assertEqual((row["status"], row["superseded_status"]), ("legacy_record", "completed"))
        # (2) Keyed on role_class only: the identical raw batch under role_class other (a
        # verifier_ name, but not a verifier) appends, as every CLI event did in 2.6.3.
        before_seq = len(rows_of(audit / "run.events.jsonl"))
        other_output = "work/coordinator/raw_other/result.json"
        write(audit / other_output, "{}\n")
        other = self.python(LIFECYCLE, "append-batch", audit, batch("other", forged_batch(
            POST_FAN_OUT_ROLE, "other", invocation_id="raw-other-1", output=other_output,
            output_sha256=digest(audit / other_output))))
        self.assertEqual(other.returncode, 0, other.stdout + other.stderr)
        self.assertEqual(len(rows_of(audit / "run.events.jsonl")), before_seq + 4)
        self.assertEqual(json.loads(other.stdout)["events"], 4)
        # (3) A non-lifecycle CLI event is untouched by the guard.
        noted = self.python(LIFECYCLE, "append-kv", audit, "note", "text=raw CLI still appends")
        self.assertEqual(noted.returncode, 0, noted.stdout + noted.stderr)

    def test_record_invocation_cli_refuses_a_verifier_lifecycle(self) -> None:
        """record_invocation.py's lifecycle flags are command lines too; --correct is not."""
        audit = make_audit(self.root)
        work = self.root / "cli"
        work.mkdir()
        reservation = {
            "role": POST_FAN_OUT_ROLE, "role_class": "verifier", "round": 0,
            "attempt_kind": "role_inference", "provider": "anthropic", "requested_model": "sonnet-5",
            "reasoning_effort": "max", "input_artifact_sha256": "d" * 64, "prompt_sha256": "e" * 64,
        }
        (work / "reservation.json").write_text(json.dumps(reservation), encoding="utf-8")
        before = self.journal_state(audit)
        self.assert_refused_before_append(
            audit, self.python(RECORD, "--reserve", audit, work / "reservation.json"), before, "--reserve")
        reserved = record_invocation.reserve_invocation(audit, reservation)["invocation_id"]
        for flag, extra in (("--provider-request", []), ("--inference-start", []),
                            ("--response-received", ["work/x.json", "a" * 64])):
            before = self.journal_state(audit)
            self.assert_refused_before_append(
                audit, self.python(RECORD, flag, audit, reserved, *extra), before, flag)
        record = legacy_terminal_record("cli-verifier-1", POST_FAN_OUT_ROLE, "verifier", 0,
                                        output=None, output_sha256=None, started_at=T_VERIFIER[0],
                                        ended_at=T_VERIFIER[1])
        (work / "record.json").write_text(json.dumps(record), encoding="utf-8")
        before = self.journal_state(audit)
        self.assert_refused_before_append(
            audit, self.python(RECORD, audit, work / "record.json"), before, "positional record")
        # Control: the same reservation under role_class other is accepted by --reserve.
        (work / "other.json").write_text(json.dumps(dict(reservation, role_class="other",
                                                         round=0)), encoding="utf-8")
        other = self.python(RECORD, "--reserve", audit, work / "other.json")
        self.assertEqual(other.returncode, 0, other.stdout + other.stderr)


# Runs one scripts/ command line in-process with the two nondeterministic journal inputs pinned
# (the event pid and the default event time), so two trees' outputs can be compared byte for byte.
PINNED_DRIVER = """
import importlib, os, sys
scripts, script = sys.argv[1], sys.argv[2]
sys.path.insert(0, scripts)
os.getpid = lambda: 4242
import lifecycle_journal
lifecycle_journal.utc_now = lambda: "2026-08-27T11:05:00Z"
module = importlib.import_module(script[:-3])
sys.argv = [os.path.join(scripts, script), *sys.argv[3:]]
raise SystemExit(module.main())
"""


class CliAppendInvariance(_AuditCase):
    """No-verifier invariance of the journal guard: every CLI append that names no verifier writes
    exactly the bytes 2.6.3 writes (journal, run.json, invocations.jsonl, metrics)."""

    @unittest.skipUnless(PRISTINE and (PRISTINE / "scripts/lifecycle_journal.py").is_file(),
                         "set I4R_V263_SKILL to a pristine 2.6.3 skill tree")
    def test_cli_appends_without_a_verifier_are_byte_identical_to_2_6_3(self) -> None:
        base = make_audit(self.root / "base")
        driver = self.root / "pinned_driver.py"
        driver.write_text(PINNED_DRIVER, encoding="utf-8")
        inputs = self.root / "inputs"
        inputs.mkdir()
        output = "work/coordinator/raw_other/result.json"
        write(base / output, "{}\n")
        (inputs / "note.json").write_text(json.dumps({"text": "an operator note"}), encoding="utf-8")
        (inputs / "batch.jsonl").write_text("".join(json.dumps(event) + "\n" for event in [
            *forged_batch(POST_FAN_OUT_ROLE, "other", invocation_id="raw-other-1", output=output,
                          output_sha256=digest(base / output)),
            {"type": "note", "payload": {"text": "after the batch"}},
        ]), encoding="utf-8")
        (inputs / "reservation.json").write_text(json.dumps({
            "invocation_id": "cli-other-1", "role": "operator_check", "role_class": "other", "round": 0,
            "attempt_kind": "role_inference", "provider": "anthropic", "requested_model": "sonnet-5",
            "reasoning_effort": "max", "input_artifact_sha256": "d" * 64, "prompt_sha256": "e" * 64,
        }), encoding="utf-8")
        (inputs / "correction.json").write_text(json.dumps({
            "set": {"status": "legacy_record"}, "reason": "superseded"}), encoding="utf-8")
        commands = [
            ("lifecycle_journal.py", ["append-kv", "{audit}", "note", "text=raw CLI note"]),
            ("lifecycle_journal.py", ["append-event", "{audit}", "note", str(inputs / "note.json")]),
            ("lifecycle_journal.py", ["append-batch", "{audit}", str(inputs / "batch.jsonl")]),
            ("lifecycle_journal.py", ["append-kv", "{audit}", "invocation_correction",
                                      "invocation_id=raw-other-1", 'set:={"output_sha256": null}',
                                      "reason=unbind the placeholder"]),
            ("record_invocation.py", ["--reserve", "{audit}", str(inputs / "reservation.json")]),
            ("record_invocation.py", ["--provider-request", "{audit}", "cli-other-1"]),
            ("record_invocation.py", ["--inference-start", "{audit}", "cli-other-1"]),
            ("record_invocation.py", ["--correct", "{audit}", "raw-other-1", str(inputs / "correction.json")]),
            ("lifecycle_journal.py", ["project", "{audit}"]),
        ]
        work = self.root / "work" / base.relative_to(self.root / "base")
        outputs = {}
        for label, skill in (("2.6.3", PRISTINE), ("2.6.4", SKILL)):
            if work.exists():
                subprocess.run(["chmod", "-R", "u+w", str(work)], check=False)
                shutil.rmtree(work)
            shutil.copytree(base, work, symlinks=True)
            log = []
            for script, arguments in commands:
                result = subprocess.run(
                    [sys.executable, str(driver), str(skill / "scripts"), script,
                     *(str(work) if value == "{audit}" else value for value in arguments)],
                    text=True, capture_output=True, timeout=120, env=self.environment)
                log.append((script, arguments[0], result.returncode, result.stdout, result.stderr))
                self.assertEqual(result.returncode, 0, f"{label} {script} {arguments}: {result.stderr}")
            outputs[label] = (log, {path.relative_to(work).as_posix(): path.read_bytes()
                                    for path in sorted(work.rglob("*")) if path.is_file()})
        self.assertEqual(outputs["2.6.4"][0], outputs["2.6.3"][0], "command outputs differ")
        self.assertEqual(sorted(outputs["2.6.4"][1]), sorted(outputs["2.6.3"][1]))
        for relative, data in outputs["2.6.3"][1].items():
            self.assertEqual(outputs["2.6.4"][1][relative], data, relative)
        events = [json.loads(line) for line in
                  outputs["2.6.4"][1]["run.events.jsonl"].decode().splitlines()]
        self.assertEqual({event["pid"] for event in events[-12:]}, {4242}, "the pin took effect")


class NonCompletedVerifierRows(_AuditCase):
    """records-2: verifier_compliance checked only completed provider calls, so a failed verifier
    row at round 3, with a malformed role or on a critic family, finalized verifiers_ok True and
    compliance_pass True, and published, although verifiers_ok promises that every verifier call
    meets the contract. Such rows can no longer be journaled through any API or command line, so
    they are built as they would sit in an older or tampered journal: through the raw Python
    append (lifecycle_journal.append_events), which is not a runner path."""

    def raw_failed_verifier(self, audit: Path, *, role: str = POST_FAN_OUT_ROLE, round_number: int = 0,
                            route: dict[str, Any] | None = None) -> None:
        # On the auditors' own route unless a test names another, so only the named defect differs.
        lifecycle_journal.append_events(audit, forged_batch(
            role, invocation_id="raw-failed-verifier", round_number=round_number, status="failed",
            route=route or claude_route(audit)))

    def finalized(self, audit: Path) -> dict[str, Any]:
        result = self.finalize(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.compliance(audit)

    def test_each_contract_violation_of_a_failed_verifier_row_fails_compliance(self) -> None:
        cases = {
            "round 3": ({"round_number": 3}, "has round 3, not 0"),
            "malformed role": ({"role": "verifier_hand"}, "role 'verifier_hand' is not verifier_<12 hex>"),
        }
        for label, (options, reason) in cases.items():
            with self.subTest(label):
                audit = make_audit(self.root / label.replace(" ", "-"))
                self.raw_failed_verifier(audit, **options)
                compliance = self.finalized(audit)
                self.assertFalse(compliance["compliance_pass"], compliance["violations"])
                self.assertFalse(compliance["verifiers_ok"])
                self.assertEqual(compliance["verifier_calls"], [], "a failed call is not listed")
                self.assertTrue(any(item.startswith("verifier call raw-failed-verifier (status 'failed'):")
                                    and reason in item for item in compliance["violations"]),
                                compliance["violations"])
                publish = self.publish(audit)
                self.assertNotEqual(publish.returncode, 0, publish.stdout + publish.stderr)

    def test_a_failed_verifier_row_on_a_critic_family_is_a_violation(self) -> None:
        """A row's family is derived from its observed model and provider, so a critic-family
        verifier row sits on the critic route, and finalize already refuses that ledger (the
        policy route check) before its compliance vector. verifier_compliance, which derive_run
        calls, must still name the row itself on the same on-disk state (records-2)."""
        audit = make_audit(self.root)
        self.raw_failed_verifier(audit, route=critic_family_route(audit))
        result = self.finalize(audit)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("recorded request violates pipeline policy", result.stdout + result.stderr)
        publish = self.publish(audit)
        self.assertNotEqual(publish.returncode, 0, publish.stdout + publish.stderr)
        listing, violations = record_invocation.verifier_compliance(
            audit, rows_of(audit / "invocations.jsonl"), rows_of(audit / "work/findings.jsonl"),
            self.policy(audit))
        self.assertEqual(listing, [])
        self.assertEqual(violations, [
            f"verifier call raw-failed-verifier (status 'failed'): verifier {POST_FAN_OUT_ROLE} "
            "family 'gpt' is a critic family ['gpt']"])

    def test_a_failed_call_made_as_the_runner_makes_it_does_not_fail_compliance(self) -> None:
        """Negative control: a failed round-0 verifier call on the auditor route, journaled
        through the runner's lifecycle API (a transient provider failure), is no violation."""
        audit = make_audit(self.root)
        route = claude_route(audit)
        invocation_id = record_invocation.reserve_invocation(audit, {
            "role": POST_FAN_OUT_ROLE, "role_class": "verifier", "round": 0,
            "attempt_kind": "role_inference", "provider": route["provider"],
            "requested_model": route["requested_model"], "reasoning_effort": route["reasoning_effort"],
            "input_artifact_sha256": "d" * 64, "prompt_sha256": "e" * 64,
        })["invocation_id"]
        record_invocation.mark_provider_request(audit, invocation_id, status="requested", at=T_VERIFIER[0])
        attempt = record_invocation.mark_inference_start(audit, invocation_id, at=T_VERIFIER[0])["attempt"]
        record = legacy_terminal_record(invocation_id, POST_FAN_OUT_ROLE, "verifier", 0, output=None,
                                        output_sha256=None, started_at=T_VERIFIER[0],
                                        ended_at=T_VERIFIER[1], route=route, status="failed",
                                        input_hash="d" * 64)
        record["attempt"] = attempt
        lifecycle_journal.record_terminal(audit, record, synthesize_lifecycle=False)
        compliance = self.finalized(audit)
        self.assertTrue(compliance["compliance_pass"], compliance["violations"])
        self.assertTrue(compliance["verifiers_ok"])
        self.assertEqual(compliance["verifier_calls"], [])


class HumanVerificationRollupTest(unittest.TestCase):
    """finalize's comment roll-up counts the post-fan-out verifier as a model, never a human, and
    recognises it by role_class only (records-3): a reviewer id is the verifier only when it is the
    role of a role_class verifier invocation in the run's ledger."""

    VERIFIER_ROW = {
        "invocation_id": "inv-verifier-rollup", "role": "verifier_0123456789ab",
        "role_class": "verifier", "round": 0, "attempt": None, "status": "completed",
        "provider": "anthropic", "provider_call": False, "cache_usage": "read", "session_id": None,
        "requested_model": "sonnet-5", "observed_model": None, "model_family": "claude",
        "model_source": "declared", "reasoning_effort": "max",
        "started_at": "2026-01-01T11:10:00Z", "ended_at": "2026-01-01T11:10:00Z",
        "elapsed_seconds": 0, "input_artifact_sha256": "a" * 64, "prompt_sha256": "b" * 64,
        "output_artifact": None, "output_sha256": None,
        "usage": {"obtainable": False, "input_tokens": None, "output_tokens": None,
                  "cached_input_tokens": None, "reasoning_tokens": None, "total_tokens": None,
                  "source": "unavailable"},
        "error": None, "error_class": None, "legacy_source": None,
    }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-264-rollup-")
        self.root = Path(self.temporary.name)
        self.environment = os.environ.copy()
        self.environment.update({"LC_ALL": "C", "I4R_ALLOW_SKILL_LOCAL_REGISTRY": "1"})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def finalized_comment(self, reviewer: str, *, verifier_row: bool = False,
                          skill: Path = SKILL, label: str = "") -> bytes:
        audit = self.root / f"{reviewer}-{int(verifier_row)}{label}"
        audit.mkdir()
        shutil.copyfile(SKILL / "schemas/EXAMPLE_run.json", audit / "run.json")
        comment = json.loads((SKILL / "schemas/EXAMPLE_comment.json").read_text())
        for finding in comment["findings"]:
            verification = finding.get("verification") or {}
            for item in verification.get("verifiers") or []:
                item["verifier"] = "critic_stats"
        comment["findings"][0].setdefault("verification", {})["verifiers"] = [
            {"verifier": reviewer, "at": None, "method": "rerun", "outcome": "reproduced",
             "notes": None}]
        write(audit / "comment.json", json.dumps(comment, indent=2) + "\n")
        if verifier_row:
            write(audit / "invocations.jsonl", json.dumps(dict(self.VERIFIER_ROW, role=reviewer)) + "\n")
        # The verification bundle finalize's own self-test builds for the shipped pair.
        report = [f"## {index}. {finding.get('claim') or 'Synthetic'} [claim {index}]\n\nText.\n"
                  for index, finding in enumerate(comment["findings"], 1)]
        write(audit / "report/REPORT_final.md", "\n".join(report))
        write(audit / "verification.Rmd", "---\ntitle: fixture\noutput: html_document\n---\n\nok\n")
        rows = "".join(f"C-{index},fixture,PASS\n" for index in range(1, len(comment["findings"]) + 1))
        write(audit / "verification_summary.fixture.csv", "claim,key,verdict\n" + rows)
        bin_dir = audit / ".verification-test-bin"
        write(bin_dir / "Rscript", "#!/bin/sh\ncp verification_summary.fixture.csv "
                                   "verification_summary.csv\nprintf '0\\n' > verification_status.txt\n"
                                   "exit 0\n")
        os.chmod(bin_dir / "Rscript", 0o755)
        environment = dict(self.environment, PATH=f"{bin_dir}:{self.environment.get('PATH', '')}")
        verification = subprocess.run(["/bin/bash", str(skill / "scripts/run_verification.sh"),
                                       str(audit)], text=True, capture_output=True, timeout=120,
                                      env=environment)
        self.assertEqual(verification.returncode, 0, verification.stdout + verification.stderr)
        (audit / "verification_summary.fixture.csv").unlink()
        shutil.rmtree(bin_dir)
        result = subprocess.run(["/bin/bash", str(skill / "scripts/finalize_run.sh"), str(audit),
                                 "--no-registry"],
                                text=True, capture_output=True, timeout=120, env=self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return (audit / "comment.json").read_bytes()

    def rollup(self, reviewer: str, **options: Any) -> dict[str, Any]:
        return json.loads(self.finalized_comment(reviewer, **options))["rollup"]

    def test_verifier_reviewer_is_not_counted_as_human_verification(self) -> None:
        self.assertEqual(self.rollup("verifier_0123456789ab", verifier_row=True)["n_human_verified"], 0)
        # Negative control: a person code on the same finding is counted.
        self.assertEqual(self.rollup("V9")["n_human_verified"], 1)

    def test_a_verifier_named_person_code_with_no_verifier_row_stays_human(self) -> None:
        """The name is never the key (records-3): 'verifier_1' is a person code, and even the exact
        verifier_<12 hex> shape is human when no role_class verifier call has that role."""
        for reviewer in ("verifier_1", "verifier_jane", "verifier_0123456789ab"):
            with self.subTest(reviewer):
                self.assertEqual(self.rollup(reviewer)["n_human_verified"], 1)

    @unittest.skipUnless(PRISTINE and (PRISTINE / "scripts/finalize_run.sh").is_file(),
                         "set I4R_V263_SKILL to a pristine 2.6.3 skill tree")
    def test_no_verifier_comment_bytes_equal_2_6_3(self) -> None:
        """With no verifier row the finalized comment.json is byte-identical to 2.6.3's for a
        'verifier_'-named person code (the rule-8 invariance records-3 broke)."""
        for reviewer in ("verifier_1", "verifier_0123456789ab", "V9"):
            with self.subTest(reviewer):
                ours = self.finalized_comment(reviewer, label="-264")
                theirs = self.finalized_comment(reviewer, skill=PRISTINE, label="-263")
                self.assertEqual(ours, theirs)


if __name__ == "__main__":
    unittest.main()
