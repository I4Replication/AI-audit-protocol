"""2.7.11: hand-copied coordinator bookkeeping no longer fails (or later stalls) a sound attempt.

Live evidence (2.7.9/2.7.10 audits):
  - A61: rows carried a Phase-1 report sha256 with one character wrong; the whole
    attempt was failed ("verification disagrees with canonical telemetry") after a full paid call.
  - A62: some rows also cited work/registry_draft/COORDINATOR_NOTES.md, which
    input_manifest.json lists (kind draft_registry) with its hash; failed after a full paid call,
    and the operator had to edit the notes to forbid the citation before attempt 2.
  - A62, A63: environment_fingerprint=null on every row; both operators back-filled it.
  - A64: dependencies.finding_ids named rows by prefix ("COD-Fnn" for "COD-Fnn-<slug>");
    generate_finding_artifacts.py refused the registry two steps later.

Every case runs the real run_coordinator.py with the fake CLI (FAKE_COORDINATOR_SLIPS /
FAKE_COORDINATOR_CITE in tests/fake_claude_coordinator.py). On 2.7.10 the first two fail the
attempt (exit 2, nothing promoted) and the last two promote rows that a later step refuses. The
controls (a wrong invocation, path or family; a row citing only prose; disagreeing draft
fingerprints; an ambiguous prefix; a citation 2.7.10 bound) behave as on 2.7.10. The last two
classes run the two coordinator recovery tools on attempts that needed normalization.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import unittest
from typing import Callable
from unittest import mock

TESTS = pathlib.Path(__file__).resolve().parent
SKILL = TESTS.parent
for _path in (str(SKILL / "scripts"), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import test_run_coordinator as trc  # noqa: E402  (fixtures only)
import test_recover_coordinator as trcov  # noqa: E402  (the quarantine fixture only)
import test_270_claude_routes as t270  # noqa: E402  (the misplaced-attempt fixture only)

SCRIPTS = SKILL / "scripts"
RECORD = "bookkeeping_normalization.json"
FINGERPRINT = "fingerprint_sha256=" + "c" * 64
GROUPING = "work/registry_draft/GROUPING.md"
REPRODUCTION = "report/01_reproduction.md"


def set_draft_fingerprint(draft: pathlib.Path, fingerprint: str = FINGERPRINT) -> None:
    """Production draft rows carry the audit's one Phase-1 fingerprint (build_draft_registry.py)."""
    rows = [json.loads(line) for line in draft.read_text().splitlines() if line.strip()]
    for row in rows:
        row["dependencies"]["environment_fingerprint"] = fingerprint
    draft.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def every_slip(root: pathlib.Path) -> dict:
    return {
        "verification": {"reviewer_id": "auditor_reproduction", "invocation_id": "inv-auditor_reproduction",
                         "path": REPRODUCTION, "sha256": trc.digest(root / REPRODUCTION)},
        "null_fingerprint": True,
        "abbreviated_dependency": "COD-F05",
    }


def note_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if "[note ] coordinator output:" in line]


class CoordinatorBookkeeping(unittest.TestCase):
    setUp_base = trc.CoordinatorRunnerTests.setUp
    tearDown = trc.CoordinatorRunnerTests.tearDown
    run_runner = trc.CoordinatorRunnerTests.run_runner
    attempt = trc.CoordinatorRunnerTests.attempt

    def setUp(self) -> None:
        self.setUp_base()
        set_draft_fingerprint(self.draft)

    def run_with(self, *, slips: dict | None = None, cite: list | None = None):
        env = {"FAKE_COORDINATOR_SLIPS": json.dumps(slips or {}),
               "FAKE_COORDINATOR_CITE": json.dumps(cite or [])}
        with mock.patch.dict(os.environ, env):
            return self.run_runner()

    def promoted(self) -> list[dict]:
        return [json.loads(line) for line in (self.root / "work/findings.jsonl").read_text().splitlines()]

    def assert_promoted(self, result) -> list[dict]:
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return self.promoted()

    def record(self) -> dict:
        path = self.attempt() / RECORD
        self.assertTrue(path.is_file(), "every restored or dropped field is recorded beside the attempt")
        return json.loads(path.read_text())

    def assert_noted(self, result, expected: list[tuple[str, str, str]]) -> dict:
        """The record lists exactly ``expected`` (finding id, field, action), and stderr carries one note naming
        the record by its audit-relative path."""
        record = self.record()
        self.assertEqual(sorted((e["finding_id"], e["field"], e["action"]) for e in record["entries"]),
                         sorted(expected))
        self.assertEqual(record["fields"], len(expected))
        for entry in record["entries"]:
            self.assertTrue(entry["source"], entry)
        lines = note_lines(result.stderr)
        self.assertEqual(1, len(lines), result.stderr)
        self.assertIn(self.attempt().relative_to(self.root).as_posix() + "/" + RECORD, lines[0])
        for private in (str(self.root), str(self.root.resolve()), "/Users/", "/private/", "/tmp/"):
            self.assertNotIn(private, lines[0])
        return record

    def assert_failed_unpromoted(self, result, message: str) -> None:
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIn(message, result.stdout)
        self.assertFalse((self.root / "work/findings.jsonl").exists())

    def test_mistyped_verification_hash_is_restored_from_telemetry(self) -> None:
        canonical = trc.digest(self.root / REPRODUCTION)
        result = self.run_with(slips={"verification": {
            "reviewer_id": "auditor_reproduction", "invocation_id": "inv-auditor_reproduction",
            "path": REPRODUCTION, "sha256": canonical}})
        rows = self.assert_promoted(result)
        self.assertEqual(rows[0]["verifications"][0]["evidence_record"]["sha256"], canonical)
        record = self.assert_noted(result, [("F1", "/verifications/0/evidence_record/sha256", "restored")])
        entry = record["entries"][0]
        self.assertEqual(entry["value"], canonical)
        self.assertEqual(1, sum(a != b for a, b in zip(entry["previous"], canonical)), "one character was mistyped")
        # The bundle the ledger binds hashes the stored (restored) bytes.
        bundle = json.loads((self.attempt() / "coordinator_bundle.json").read_text())
        self.assertEqual(bundle["outputs"]["findings.jsonl"]["sha256"], trc.digest(self.root / "work/findings.jsonl"))

    def test_a_wrong_invocation_path_or_family_still_fails(self) -> None:
        for change in ({"invocation_id": "inv-auditor_data_integrity"},
                       {"path": "report/02_data_integrity.md", "sha256_of": "report/02_data_integrity.md"},
                       {"model_family": "gpt"}):
            with self.subTest(change=sorted(change)):
                spec = {"reviewer_id": "auditor_reproduction", "invocation_id": "inv-auditor_reproduction",
                        "path": REPRODUCTION, **change}
                spec["sha256"] = trc.digest(self.root / spec.pop("sha256_of", REPRODUCTION))
                result = self.run_with(slips={"verification": spec})
                self.assert_failed_unpromoted(result, "verification disagrees with canonical telemetry")
                self.assertFalse((self.attempt() / RECORD).exists())
                self.assertEqual([], note_lines(result.stderr))
                self.tearDown()
                self.setUp()

    def test_draft_prose_citation_is_dropped_when_other_evidence_remains(self) -> None:
        for spelling in (GROUPING, "draft/" + GROUPING):   # the origin, and the snapshot path the model read
            with self.subTest(spelling=spelling):
                result = self.run_with(cite=[{"path": spelling, "origin": GROUPING}])
                rows = self.assert_promoted(result)
                self.assertEqual([item["path"] for item in rows[0]["evidence"]["source_hashes"]], [REPRODUCTION])
                record = self.assert_noted(result, [("F1", "/evidence/source_hashes/1", "dropped")])
                self.assertEqual(record["entries"][0]["previous"], spelling)
                self.tearDown()
                self.setUp()

    def test_draft_prose_dependency_is_dropped(self) -> None:
        result = self.run_with(cite=[{"dependency": GROUPING}])
        rows = self.assert_promoted(result)
        self.assertNotIn(GROUPING, rows[0]["dependencies"]["artifact_paths"])
        record = self.record()
        self.assertEqual([(e["field"].rsplit("/", 1)[0], e["action"], e["previous"]) for e in record["entries"]],
                         [("/dependencies/artifact_paths", "dropped", GROUPING)])

    def test_a_row_citing_only_draft_prose_still_fails_and_says_why(self) -> None:
        result = self.run_with(slips={"prose_only": {"path": GROUPING, "origin": GROUPING}})
        self.assert_failed_unpromoted(
            result, f"F1 cites an unbound evidence hash: {GROUPING} (draft-registry prose "
                    "(work/registry_draft/*.md) is context handed to the coordinator, not evidence; cite the paper/")
        self.assertFalse((self.attempt() / RECORD).exists())

    def test_a_prose_citation_the_draft_bound_is_kept(self) -> None:
        # 2.7.10 accepted it (the draft row binds the hash), so it is never dropped.
        row = json.loads(self.draft.read_text())
        row["evidence"]["source_hashes"].append({"path": GROUPING, "sha256": trc.digest(self.root / GROUPING)})
        self.draft.write_text(json.dumps(row, sort_keys=True) + "\n")
        rows = self.assert_promoted(self.run_with())
        self.assertIn(GROUPING, [item["path"] for item in rows[0]["evidence"]["source_hashes"]])
        self.assertFalse((self.attempt() / RECORD).exists())

    def test_null_fingerprint_is_restored_from_the_draft(self) -> None:
        result = self.run_with(slips={"null_fingerprint": True})
        rows = self.assert_promoted(result)
        self.assertTrue(all(row["dependencies"]["environment_fingerprint"] == FINGERPRINT for row in rows))
        self.assert_noted(result, [("F1", "/dependencies/environment_fingerprint", "restored")])

    def test_disagreeing_draft_fingerprints_restore_nothing(self) -> None:
        first = json.loads(self.draft.read_text())
        second = json.loads(json.dumps(first))
        second.update(id="F2", claim="A second bounded comparison in the supplied report holds.")
        second["dependencies"]["environment_fingerprint"] = "fingerprint_sha256=" + "d" * 64
        self.draft.write_text(json.dumps(first, sort_keys=True) + "\n" + json.dumps(second, sort_keys=True) + "\n")
        result = self.run_with(slips={"null_fingerprint": True})
        rows = self.assert_promoted(result)
        self.assertEqual([None, None], [row["dependencies"]["environment_fingerprint"] for row in rows])
        self.assertFalse((self.attempt() / RECORD).exists())

    def test_abbreviated_dependency_is_resolved_to_its_unique_row(self) -> None:
        result = self.run_with(slips={"abbreviated_dependency": "COD-F05"})
        rows = self.assert_promoted(result)
        self.assertEqual(rows[0]["dependencies"]["finding_ids"], ["COD-F05-coordinator-merge-note"])
        record = self.assert_noted(result, [("F1", "/dependencies/finding_ids/0", "resolved")])
        self.assertEqual((record["entries"][0]["previous"], record["entries"][0]["value"]),
                         ("COD-F05", "COD-F05-coordinator-merge-note"))

    def test_an_ambiguous_prefix_is_left_for_the_later_refusal(self) -> None:
        rows = self.assert_promoted(self.run_with(slips={"ambiguous_dependency": "COD-F05"}))
        self.assertEqual(rows[0]["dependencies"]["finding_ids"], ["COD-F05"])
        self.assertFalse((self.attempt() / RECORD).exists())

    def test_a_refused_attempt_still_records_what_it_rewrote(self) -> None:
        # The hash is restored and findings.jsonl rewritten; then the prose-only row refuses the response.
        canonical = trc.digest(self.root / REPRODUCTION)
        result = self.run_with(slips={
            "verification": {"reviewer_id": "auditor_reproduction", "invocation_id": "inv-auditor_reproduction",
                             "path": REPRODUCTION, "sha256": canonical},
            "prose_only": {"path": GROUPING, "origin": GROUPING}})
        self.assert_failed_unpromoted(result, f"F1 cites an unbound evidence hash: {GROUPING}")
        self.assert_noted(result, [("F1", "/verifications/0/evidence_record/sha256", "restored")])
        stored = json.loads((self.attempt() / "output.partial/findings.jsonl").read_text().splitlines()[0])
        self.assertEqual(stored["verifications"][0]["evidence_record"]["sha256"], canonical)

    def test_a_clean_response_writes_no_normalization_record(self) -> None:
        result = self.run_with()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse((self.attempt() / RECORD).exists())
        self.assertEqual([], note_lines(result.stderr))


class RecoverCoordinatorAcceptsANormalizedPromotion(unittest.TestCase):
    """recover_coordinator.py re-validates the bytes a quarantined promotion stored, without the bookkeeping
    context, and requires the bundle, ledger record and transaction to agree with them. A promotion whose
    findings.jsonl the runner normalized (every slip at once) is recovered like any other."""

    _thaw_attempt = trcov.CoordinatorRecoveryTests._thaw_attempt
    _write_bundle_and_rebind_record = trcov.CoordinatorRecoveryTests._write_bundle_and_rebind_record
    _quarantine_success = trcov.CoordinatorRecoveryTests._quarantine_success
    run_recovery = trcov.CoordinatorRecoveryTests.run_recovery
    coordinator_rows = trcov.CoordinatorRecoveryTests.coordinator_rows

    def setUp(self) -> None:
        self.fixture = trc.CoordinatorRunnerTests(
            methodName="test_success_is_content_bound_promotes_and_records_all_models_and_usage")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        set_draft_fingerprint(self.fixture.draft)
        env = {"FAKE_COORDINATOR_SLIPS": json.dumps(every_slip(self.root)),
               "FAKE_COORDINATOR_CITE": json.dumps([{"path": GROUPING, "origin": GROUPING}])}
        with mock.patch.dict(os.environ, env):
            result = self.fixture.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.attempt = self.fixture.attempt()
        record = json.loads((self.attempt / RECORD).read_text())
        self.assertEqual({entry["field"].split("/")[1] for entry in record["entries"]},
                         {"verifications", "evidence", "dependencies"})
        self.normalized = (self.attempt / "output.partial/findings.jsonl").read_bytes()
        self.invocation_id = self.attempt.name.split("_", 3)[-1]
        self.attempt_relative = self.attempt.relative_to(self.root).as_posix()
        self._quarantine_success()

    def test_the_normalized_promotion_is_recovered_byte_for_byte(self) -> None:
        result = self.run_recovery()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["corrected_fields"], ["elapsed_seconds"])
        self.assertEqual(1, len(self.coordinator_rows()))
        self.assertEqual((self.root / "work/findings.jsonl").read_bytes(), self.normalized)
        self.assertEqual((self.attempt / "output.partial/findings.jsonl").read_bytes(), self.normalized)
        self.assertEqual([], note_lines(result.stderr), "recovery re-judges stored bytes; it normalizes nothing")


class RecoverMisplacedNormalizesAsTheRunner(unittest.TestCase):
    """recover_misplaced_coordinator_outputs.py judges a response the runner never saw (it was written to the
    attempt root). It passes the runner's bookkeeping context, so a slip the runner would have restored is
    restored there too, recorded beside the attempt and named in the receipt; a response with no slip keeps the
    receipt's keys. Each case is a real misplaced attempt (test_270's fixture) and the tool's real run."""

    def misplaced_attempt(self, slips_for: Callable[[pathlib.Path], dict], cite: list):
        fixture = t270.coordinator_on_route(self, t270.SONNET_5_5, metadata21=True)
        set_draft_fingerprint(fixture.draft)
        fake = pathlib.Path(fixture.temporary.name) / "claude-lifting-the-attempt-root"
        fake.write_text(t270.LIFTING_FAKE.replace("@PYTHON_REPR@", repr(sys.executable))
                        .replace("@PYTHON@", sys.executable).replace("@FAKE@", repr(str(fixture.fake))),
                        encoding="utf-8")
        fake.chmod(0o755)
        fixture.fake = fake
        with mock.patch.dict(os.environ, {"FAKE_COORDINATOR_SLIPS": json.dumps(slips_for(fixture.root)),
                                          "FAKE_COORDINATOR_CITE": json.dumps(cite)}):
            result = fixture.run_runner("misplaced_outputs")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        attempt = fixture.attempt()
        self.assertFalse((attempt / RECORD).exists(), "the runner found nothing to validate")
        stdout, denials = attempt / "claude_stdout.json", attempt / "permission_denials.json"
        with t270.writable(attempt, attempt / "output.partial", stdout, denials):
            (attempt / "output.partial" / "fake_args.json").unlink()
            envelope = json.loads(stdout.read_text())
            envelope.update(terminal_reason="completed", stop_reason="end_turn")
            stdout.write_text(json.dumps(envelope, separators=(",", ":")) + "\n")
            denials.write_text(json.dumps({"count": 0, "tool_names": [], "raw_stdout_sha256": t270.digest(stdout)}))
        recovered = subprocess.run(
            [sys.executable, str(SCRIPTS / "recover_misplaced_coordinator_outputs.py"), str(fixture.root),
             "--attempt", attempt.relative_to(fixture.root).as_posix()],
            text=True, capture_output=True, timeout=120, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return fixture, attempt, recovered

    def test_a_misplaced_response_with_slips_is_recovered_as_the_runner_would_store_it(self) -> None:
        fixture, attempt, result = self.misplaced_attempt(every_slip, [{"path": GROUPING, "origin": GROUPING}])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["status"], "completed")
        rows = [json.loads(line) for line in (fixture.root / "work/findings.jsonl").read_text().splitlines()]
        self.assertEqual(rows[0]["verifications"][0]["evidence_record"]["sha256"],
                         t270.digest(fixture.root / REPRODUCTION))
        self.assertEqual([item["path"] for item in rows[0]["evidence"]["source_hashes"]], [REPRODUCTION])
        self.assertTrue(all(row["dependencies"]["environment_fingerprint"] == FINGERPRINT for row in rows))
        self.assertEqual(rows[0]["dependencies"]["finding_ids"], ["COD-F05-coordinator-merge-note"])
        record = json.loads((attempt / RECORD).read_text())
        receipt = json.loads(next((fixture.root / "coordinator/recoveries").glob("*/receipt.json")).read_text())
        self.assertEqual(receipt["bookkeeping_normalization"],
                         {"record": attempt.relative_to(fixture.root).as_posix() + "/" + RECORD,
                          "fields": record["fields"]})
        self.assertEqual(receipt["outputs"]["findings.jsonl"]["sha256"],
                         t270.digest(fixture.root / "work/findings.jsonl"))
        self.assertEqual(1, len(note_lines(result.stderr)), result.stderr)
        self.assertEqual((attempt / RECORD).stat().st_mode & 0o222, 0, "the attempt is frozen again")

    def test_a_misplaced_response_without_slips_keeps_the_receipt_keys(self) -> None:
        fixture, attempt, result = self.misplaced_attempt(lambda root: {}, [])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        receipt = json.loads(next((fixture.root / "coordinator/recoveries").glob("*/receipt.json")).read_text())
        self.assertNotIn("bookkeeping_normalization", receipt)
        self.assertFalse((attempt / RECORD).exists())
        self.assertEqual([], note_lines(result.stderr))


if __name__ == "__main__":
    unittest.main()
