"""Regression pins for fixes that shipped without one.

Seven fixes landed across 2.2.2 and 2.2.4 with no test: two were proven only by a single live
production run, five by nothing at all. That is the same shape as the defects they fix -- a rule
enforced somewhere and pinned nowhere -- so each is pinned here, with the failure it prevents named
in the docstring rather than left to a changelog nobody reads at debug time.

Each test was confirmed to fail with its fix reverted.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "schemas/tools"))

import lifecycle_journal  # noqa: E402
import phase1_runtime  # noqa: E402
import summarize_invocations  # noqa: E402
from tests.test_metadata_2_1_lifecycle import base_run, reservation, terminal  # noqa: E402


class AttemptCeilingIsJournalled(unittest.TestCase):
    """A reader could not tell budget exhaustion from completion.

    The ceiling is read from the environment at call time, so without it in the journal there is
    no record of what budget an attempt actually ran under.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="i4r-ceiling-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        lifecycle_journal.initialize(self.root, base_run())

    def start(self, invocation_id: str) -> dict:
        lifecycle_journal.reserve_invocation(self.root, reservation(invocation_id))
        lifecycle_journal.mark_provider_request(self.root, invocation_id)
        lifecycle_journal.mark_inference_start(self.root, invocation_id)
        events = lifecycle_journal.load_events(self.root, allow_legacy_prefix=False)
        return next(
            event["payload"] for event in reversed(events)
            if event["type"] == "inference_start"
        )

    def test_the_ceiling_and_its_source_are_recorded_with_the_attempt(self) -> None:
        payload = self.start("inv-ceiling-default")
        self.assertEqual(payload["attempt_ceiling"], lifecycle_journal.max_attempts_per_role())
        self.assertEqual(payload["attempt_ceiling_source"], "protocol_default")
        self.assertIs(payload["attempt_ceiling_applies"], True)

    def test_the_operator_knob_is_recorded_as_the_source(self) -> None:
        previous = os.environ.get("I4R_MAX_ATTEMPTS")
        os.environ["I4R_MAX_ATTEMPTS"] = "7"
        self.addCleanup(
            lambda: os.environ.__setitem__("I4R_MAX_ATTEMPTS", previous)
            if previous is not None else os.environ.pop("I4R_MAX_ATTEMPTS", None)
        )
        payload = self.start("inv-ceiling-raised")
        self.assertEqual(payload["attempt_ceiling"], 7)
        self.assertEqual(payload["attempt_ceiling_source"], "I4R_MAX_ATTEMPTS")


class AttemptCeilingReachesTheLedger(unittest.TestCase):
    """Journalling the ceiling was only half the fix.

    It lived in run.events.jsonl alone, so a reader of invocations.jsonl or run.json still could
    not tell a role that exhausted its budget from one that finished -- which is the entire reason
    the ceiling is recorded.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="i4r-ceiling-row-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        lifecycle_journal.initialize(self.root, base_run())

    def test_the_projected_row_carries_the_ceiling(self) -> None:
        lifecycle_journal.reserve_invocation(self.root, reservation("inv-row"))
        lifecycle_journal.mark_provider_request(self.root, "inv-row")
        lifecycle_journal.mark_inference_start(self.root, "inv-row")
        lifecycle_journal.record_terminal(self.root, terminal("inv-row"))
        rows = [json.loads(line) for line
                in (self.root / "invocations.jsonl").read_text().splitlines() if line.strip()]
        row = next(row for row in rows if row["invocation_id"] == "inv-row")
        self.assertEqual(row["attempt_ceiling"], lifecycle_journal.max_attempts_per_role())
        self.assertEqual(row["attempt_ceiling_source"], "protocol_default")
        self.assertIs(row["attempt_ceiling_applies"], True)


class SupersededStatusSurvivesACorrection(unittest.TestCase):
    """A superseded failure stopped being countable as a failure.

    Re-projecting the first production audit showed 13 rows relabelled, 3 of them from `failed`:
    ten calls had failed while calls_by_status reported seven.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="i4r-superseded-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        lifecycle_journal.initialize(self.root, base_run())

    def corrected_row(self) -> dict:
        lifecycle_journal.reserve_invocation(self.root, reservation("inv-super"))
        lifecycle_journal.mark_provider_request(self.root, "inv-super")
        lifecycle_journal.mark_inference_start(self.root, "inv-super")
        failed = dict(terminal("inv-super"))
        failed.update(status="failed", error="planted", error_class="permanent",
                      output_artifact=None, output_sha256=None)
        lifecycle_journal.record_terminal(self.root, failed)
        lifecycle_journal.correct_invocation(
            self.root, "inv-super", {"status": "legacy_record"},
            reason="superseded by a Phase-1 reset")
        rows = [json.loads(line) for line
                in (self.root / "invocations.jsonl").read_text().splitlines() if line.strip()]
        return next(row for row in rows if row["invocation_id"] == "inv-super")

    def test_the_original_terminal_status_is_retained(self) -> None:
        row = self.corrected_row()
        self.assertEqual(row["status"], "legacy_record")
        self.assertEqual(row["superseded_status"], "failed")

    def test_the_summary_reports_what_the_status_no_longer_shows(self) -> None:
        self.corrected_row()
        # aggregate() directly: summarize_invocations.load() also demands an audit-scoped policy
        # snapshot, which this fixture has no reason to carry.
        rows = [json.loads(line) for line
                in (self.root / "invocations.jsonl").read_text().splitlines() if line.strip()]
        summary = summarize_invocations.aggregate(rows)
        self.assertEqual(summary["calls_by_corrected_from_status"].get("failed"), 1)
        # The headline count still shows the corrected status; that is why the companion exists.
        self.assertEqual(summary["calls_by_status"].get("failed"), None)


class TheCompletenessDeclarationIsNotCompelled(unittest.TestCase):
    """The schema used to manufacture the completeness claim it then recorded.

    status, completion_marker and both completeness_declaration booleans were pinned as consts, so
    the structured-output decoder could not emit anything but "I completed every assigned check and
    evidenced every assessment" -- whatever the model had actually done. The contract meanwhile
    told it to return a truthful partial, which was unrepresentable.

    Nothing downstream loosens: validate_v2 accepts partial|complete|failed and ties
    completion_marker to status, and validate_critic_obj still admits ONLY status=complete, so a
    truthful partial is retained as a failed attempt and re-sampled.
    """

    def schema(self) -> dict:
        import test_run_critique_runtime as runtime

        digests = {name: "b" * 64 for name in (
            "snapshot_sha256", "report_sha256", "prompt_sha256", "policy_sha256",
            "evidence_manifest_sha256")}
        return runtime.critic_schema(
            "stats", 1, "a" * 64, "audit", "inv", digests, ["F1"], ["C1"])

    def test_a_truthful_partial_is_representable(self) -> None:
        properties = self.schema()["properties"]
        self.assertEqual(
            set(properties["status"]["enum"]), {"partial", "complete", "failed"})
        for field in ("status", "completion_marker"):
            self.assertNotIn("const", properties[field], field)
        declaration = properties["completeness_declaration"]["properties"]
        for field in ("all_assigned_checks_accounted_for", "all_assessments_evidenced"):
            self.assertNotIn("const", declaration[field], field)
        self.assertNotIn("maxItems", declaration["unresolved_omissions"])

    def test_the_launcher_still_admits_only_a_complete_review(self) -> None:
        # Negative control. Dropping the consts must not let a partial be published: it is the
        # admission gate, not the schema, that decides what counts as a review.
        source = (SKILL / "scripts/test_run_critique_runtime.py").read_text()
        self.assertIn("v2 output status is not complete", source)

    def test_the_coverage_arrays_stay_pinned_for_the_repair_path(self) -> None:
        # completed_checks is read back out of the contract by _repair_candidate to derive the
        # corrected administrative values, so it is the focused-repair path's source of truth.
        # Unpinning it silently disabled every administrative repair.
        completed = self.schema()["properties"]["coverage"]["properties"]["completed_checks"]
        self.assertEqual(completed["minItems"], completed["maxItems"])


class SnapshotsArePublishedOnlyWhenFrozen(unittest.TestCase):
    """An abort between publish and freeze wedged every later run.

    freeze_tree walks bottom-up and checks the deadline per directory, so a routine deadline
    expiry mid-freeze was enough to publish a snapshot whose root was still writable. Every later
    run then died on "immutable path regained write permission" and needed a manual chmod.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="i4r-freeze-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)

    def staged_tree(self) -> pathlib.Path:
        staging = self.root / "staging" / "candidate"
        (staging / "nested").mkdir(parents=True)
        (staging / "nested" / "file.txt").write_text("payload\n", encoding="utf-8")
        return staging

    def test_a_deadline_expiring_mid_freeze_publishes_nothing(self) -> None:
        target = self.root / "snapshots" / "deadbeef"
        expired = phase1_runtime.MonotonicDeadline(expires_at=0.0)
        with self.assertRaises(phase1_runtime.Phase1Error):
            phase1_runtime.freeze_and_publish(self.staged_tree(), target, expired)
        self.assertFalse(target.exists(), "a half-frozen tree reached the published path")
        residue = [path.name for path in (self.root / "snapshots").iterdir()]
        self.assertEqual(residue, [], f"staging residue left behind: {residue}")

    def test_a_published_snapshot_is_read_only_including_its_root(self) -> None:
        target = self.root / "snapshots" / "cafebabe"
        phase1_runtime.freeze_and_publish(self.staged_tree(), target, None)
        self.assertTrue(target.is_dir())
        writable = [
            str(path) for path in [target, *target.rglob("*")]
            if path.stat().st_mode & 0o222
        ]
        self.assertEqual(writable, [], f"published tree is writable at: {writable}")


class ProbeBudgetIsActuallyCounted(unittest.TestCase):
    """The guard meant to bound paid probe calls was inert.

    The probe dispatches with attempt_kind "capability_probe", deliberately outside
    BUDGETED_ATTEMPT_KINDS, so the metadata-2.1 journal path always returned 0 and the counter
    file was written and never read: one paid probe per driver invocation, without limit.
    """

    def setUp(self) -> None:
        import test_run_critique_runtime

        self.runtime = test_run_critique_runtime
        self.temp = tempfile.TemporaryDirectory(prefix="i4r-probebudget-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        lifecycle_journal.initialize(self.root, base_run())
        self.budget = self.root / "attempt_budget"
        self.budget.mkdir()

    def used(self, role: str) -> int:
        counter = self.budget / f"{role}.count"
        import argparse

        args = argparse.Namespace(
            counter=str(counter), ledger=str(self.root / "telemetry.jsonl"),
            legacy_root=str(self.budget), budget_role=role, round=1,
        )
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.runtime.command_budget_used(args)
        return int(stream.getvalue().strip())

    def test_the_probe_counter_is_read_under_metadata_21(self) -> None:
        (self.budget / "model_probe.count").write_text("2\n", encoding="utf-8")
        self.assertEqual(self.used("model_probe"), 2)

    def test_a_critic_counter_bounds_calls_the_journal_cannot_see(self) -> None:
        """A dispatched call the provider never accepted still has to consume budget.

        The journal counts only reservations that reached inference_start, so a call killed by
        the wall-clock ceiling -- or a transport failure on connect -- was worth 0 to it. Those
        are classified transient, correctly, and the retry loop then re-dispatched the same
        attempt number forever: with the fake provider hanging on every call, a round never
        returned at all. claim_attempt writes the counter before every dispatch, so it is the
        only record that those calls happened.
        """
        (self.budget / "stats.count").write_text("2\n", encoding="utf-8")
        self.assertEqual(self.used("stats"), 2)

    def test_the_counter_can_only_ever_raise_the_count(self) -> None:
        """Which is what makes reading it safe: a stale counter can deny, never grant.

        Granting would be the dangerous direction -- it would let a resumed round re-spend a
        budget it had already used. max() cannot do that.
        """
        counter = self.budget / "stats.count"
        counter.write_text("0\n", encoding="utf-8")
        journalled_only = self.used("stats")
        counter.write_text("-5\n", encoding="utf-8")
        self.assertEqual(self.used("stats"), journalled_only)
        counter.write_text("not a number\n", encoding="utf-8")
        self.assertEqual(self.used("stats"), journalled_only)



class DisclosureRemediesAreNotFiledAsCodeFixes(unittest.TestCase):
    """A paper-side remedy was inflating the programme's headline bucket.

    "most defects are one line of code or one number" is the argument for auditing at all, and it
    is computed from count_by_remedy_class. A remedy that asks the PAPER to state something is
    neither, but with no value for it these were filed as one_line_code.
    """

    def setUp(self) -> None:
        import build_draft_registry

        self.registry = build_draft_registry

    def test_a_paper_side_remedy_routes_to_its_own_class(self) -> None:
        for remedy in ("Disclose the two-participant exclusion in Table 3's note.",
                       "State the deviation in the manuscript.",
                       "Acknowledge the limitation in the article."):
            with self.subTest(remedy=remedy):
                self.assertEqual(
                    self.registry.heuristic_remedy_class(remedy), "disclose_in_paper")

    def test_deposit_side_and_code_remedies_are_unchanged(self) -> None:
        # Negative control: a README or a setwd() fix is still a code/deposit change, not a
        # disclosure. If this drifts, the new bucket starts absorbing the old one.
        for remedy, expected in (
            ("Add a README to the deposit.", "one_line_code"),
            ("Change the setwd() line to a relative path.", "one_line_code"),
            ("Re-estimate the model without the outlier.", "reanalysis"),
            ("Correct the printed value in Table 2.", "one_number"),
        ):
            with self.subTest(remedy=remedy):
                self.assertEqual(self.registry.heuristic_remedy_class(remedy), expected)

    def test_every_enum_value_has_a_rollup_counter(self) -> None:
        # finalize_run.sh counts only known keys and compares the sum against findings_total, so a
        # value with no counter fails finalize rather than vanishing from the distribution.
        schema = json.loads((SKILL / "schemas/comment.schema.json").read_text())
        allowed = set(schema["$defs"]["finding"]["properties"]["remedy_class"]["enum"])
        import init_comment

        self.assertEqual(set(init_comment.ROLLUP_REMEDY_KEYS), allowed)


class TheRedactionBaselineDoesNotLoosenTheGate(unittest.TestCase):
    """Seeding the documented baseline would have disabled two PII checks on every audit.

    check_release_privacy masks a declared pointer only if it is allowlisted, and masked fields are
    not inspected. /governance/pii_description and /author_response/note are both allowlisted, so
    declaring them by default would stop the gate reading them for every audit ever run.
    """

    def test_no_seeded_pointer_is_release_gate_redactable(self) -> None:
        import check_release_privacy
        import init_comment

        declared = set(init_comment.COMMENT_REDACTION_BASELINE["fields_requiring_removal"])
        self.assertTrue(declared, "the baseline must not be empty")
        self.assertEqual(
            declared & set(check_release_privacy.REDACTABLE_POINTER_PREFIXES), set())

    def test_the_two_dangerous_pointers_are_named_as_deliberately_absent(self) -> None:
        import init_comment

        note = init_comment.COMMENT_REDACTION_BASELINE["note"]
        self.assertIn("pii_description", note)
        self.assertIn("author_response/note", note)


# WrittenFormFaultsAreWarmRepairable used to live here. It asserted, by grepping the source, that
# the report/result path leaks and the unbound-source-hash fault were marked repairable=True. That
# marking was wrong: the focused repair byte-freezes REPORT.md and scientific_fields, which is
# exactly where those faults sit, so every such repair was a guaranteed-fail paid call. They are
# non-repairable as of 2.4.8 and the behavioural coverage is
# tests/test_run_phase1.py::test_a_leak_is_resampled_not_sent_to_focused_repair, with the
# structural-fault positive control beside it.


if __name__ == "__main__":
    unittest.main()

