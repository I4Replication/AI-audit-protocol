"""2.7.11: a hand-logged event that would put into run.json a value finalize's schema check refuses is refused when it
is logged, and nothing is journaled.

Four audits in October 2026 journaled such a value through log_event.sh (failure kinds output_validation, environment
and intake_flatten_permission; model source=requested_only, several rows), and each one stopped at finalize step 3 until a
hand metadata_correction overrode it: the journal is append-only. Each test builds the audit with the shipped
initializer and drives the shipped log_event.sh, then judges run.json with finalize's own validator.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
import claude_routes  # noqa: E402
import lifecycle_journal  # noqa: E402
import validate_json_schema  # noqa: E402

RUN_SCHEMA = SKILL / "schemas" / "run.schema.json"


def _init(root: pathlib.Path) -> pathlib.Path:
    audit = root / "audit"
    (audit / "pkg").mkdir(parents=True)
    (audit / "pkg" / "f.csv").write_text("x\n")
    subprocess.run(
        ["/bin/bash", str(SCRIPTS / "init_run.sh"), str(audit), "--audit-id", "t-2711", "--doi", "10.1234/x",
         "--auditor-model", claude_routes.SHIPPED["requested_model"], "--auditor-family", "claude",
         "--auditor-provider", claude_routes.SHIPPED["provider"], "--protocol-version", "p1",
         "--gate-set-version", "g1"],
        check=True, text=True, capture_output=True, env=dict(os.environ, LC_ALL="C"), timeout=120)
    return audit


class HandEventSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-2711-hand-schema-")
        self.audit = _init(pathlib.Path(self.tmp.name))
        self.journal = self.audit / "run.events.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def log(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/bash", str(SCRIPTS / "log_event.sh"), "--run", str(self.audit / "run.json"),
                               *args], text=True, capture_output=True, timeout=120)

    def schema_errors(self) -> list[str]:
        return validate_json_schema.errors_for(self.audit / "run.json", RUN_SCHEMA)

    def assert_refused(self, args, fragment, ours=True):
        before = self.journal.read_bytes()
        result = self.log(*args)
        self.assertEqual(2, result.returncode, f"{args} was accepted: {result.stderr}")
        self.assertIn(fragment, result.stderr)
        if ours:
            self.assertIn("Nothing was journaled", result.stderr)
        self.assertEqual(before, self.journal.read_bytes(), f"{args} reached the append-only journal")

    def test_failure_kind_outside_the_schema_is_refused_and_other_is_accepted(self):
        # A61: the coordinator's own validator rejected attempt 1
        self.assert_refused(["failure", "kind=output_validation", "role=coordinator", "round=0", "retries=0",
                             "detail=coordinator attempt 1 rejected by its validator"], "'output_validation'")
        result = self.log("failure", "kind=other", "role=coordinator", "round=0", "retries=0",
                          "detail=coordinator attempt 1 rejected by its validator")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.schema_errors())

    def test_model_source_requested_only_is_refused_and_declared_is_accepted(self):
        # A67: critic rows, then a hand metadata_correction before finalize would pass
        args = ["model", "role=critic_stats", "role_class=critic", "round=1", "model_id=gpt-6.1-sol",
                "model_family=gpt", "provider=openai"]
        self.assert_refused(args + ["source=requested_only"], "identity_assurance")
        result = self.log(*args, "source=declared")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.schema_errors())

    def test_every_projection_finalize_validates_is_judged(self):
        cases = [
            (["model", "role=a", "role_class=phase1_auditor", "model_id=m", "provider=p"], "/model_assignments/"),
            (["tokens", "role=a", "total_tokens=10", "source=codex_json"], "/tokens/"),
            (["cost", "role=a", "amount=1", "billing_model=subscription_plan"], "/costs/"),
            (["records", "artifact=ledger", "present=true", "retention=kept"], "/records_retained/"),
            (["settled", "used=true", "sha256=abc"], "/settled_facts"),
            (["pause_start", "reason=coffee"], "/pause_reason"),
            (["human_effort", "phase=critique", "person_minutes=1.5"], "/human_effort/"),
            (["failure", "kind=other", "detail="], "/failure_events/"),
            (["note", "text=x", "at=2026-10-27T10:33Z"], "RFC 3339 time with seconds"),
            (["metadata_correction", 'set:={"total_cost": 1}', "reason=x"], "unexpected property 'total_cost'"),
        ]
        for args, fragment in cases:
            with self.subTest(args=args):
                self.assert_refused(args, fragment)
        # the journal's own parser (fromisoformat) accepts these on Python 3.11+, and 3.9's refuses them first
        for at in ("2026-10-27 10:33:05Z", "2026-10-27T10:33:05+0000", "20261027T103305Z"):
            with self.subTest(at=at):
                self.assert_refused(["note", "text=x", f"at={at}"], "RFC 3339", ours=False)
        self.assertEqual([], self.schema_errors())

    def test_a_batch_with_one_unfinalizable_line_journals_nothing(self):
        batch = pathlib.Path(self.tmp.name) / "events.jsonl"
        batch.write_text(
            '{"type":"note","payload":{"text":"would land"}}\n'
            '{"type":"model","payload":{"role":"critic_data","role_class":"critic","round":1,'
            '"model_id":"gpt-6.1-sol","provider":"openai","source":"requested_only"}}\n')
        before = self.journal.read_bytes()
        result = self.log("--batch", str(batch))
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertEqual(before, self.journal.read_bytes())

    def test_a_value_journaled_before_the_check_blocks_nothing_later(self):
        # A pre-2.7.11 journal: the same bad row, written where nothing judged it (the Python API is not the CLI).
        lifecycle_journal.append_events(self.audit, [{
            "type": "failure", "source": "log_event.sh",
            "payload": {"kind": "output_validation", "detail": "journaled on 2.7.10", "role": "coordinator"}}])
        self.assertTrue(any("/failure_events/0/kind" in error for error in self.schema_errors()))
        for args in (["note", "text=later work goes on"],
                     ["failure", "kind=timeout", "detail=a later, valid failure"]):
            result = self.log(*args)
            self.assertEqual(0, result.returncode, f"{args}: {result.stderr}")
        # the sanctioned remedy, one hand metadata_correction restating the row, still lands and clears finalize
        run = json.loads((self.audit / "run.json").read_text())
        rows = run["failure_events"]
        rows[0]["kind"] = "other"
        result = self.log("metadata_correction", "set:=" + json.dumps({"failure_events": rows}),
                          "reason=failure kind output_validation is not in the run schema enum")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.schema_errors())

    def test_the_gate_log_is_judged_but_the_compliance_finalize_derives_is_not(self):
        result = self.log("gate", "gate=check_consistency", "round=1", "exit_status=0")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assert_refused(["gate", "gate=check_consistency", "round=abc", "exit_status=0"], "/compliance/gates_run/")

    def test_an_old_error_moved_to_another_index_is_not_new(self):
        # rounds are kept sorted: a row created for round 1 after round 2's moves round 2's row from /rounds/0 to
        # /rounds/1, and the error it already had (journaled where nothing judged it) is not the new event's
        lifecycle_journal.append_events(self.audit, [{
            "type": "round_start", "source": "log_event.sh", "at": "2026-10-20T10:00:00Z",
            "payload": {"round": 2, "notes": 5}}])
        self.assertIn("/rounds/0/notes: expected type string|null, got integer", self.schema_errors())
        result = self.log("gate", "gate=check_consistency", "round=1", "exit_status=0")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("/rounds/1/notes: expected type string|null, got integer", self.schema_errors())
        # an error an old row already has is still new on a new row
        lifecycle_journal.append_events(self.audit, [{
            "type": "failure", "source": "log_event.sh", "payload": {"kind": "output_validation", "detail": "old"}}])
        self.assert_refused(["failure", "kind=output_validation", "detail=new"], "/failure_events/1/kind")

    def test_init_migration_anchor_and_finalize_are_refused_at_every_cli_but_not_for_their_tools(self):
        # 2.7.10 accepted `log_event.sh finalize status=complete`, and run.json then showed a closed audit
        batch = pathlib.Path(self.tmp.name) / "events.jsonl"
        payloads = {"init": {"initial_run": {}}, "migration_anchor": {"initial_run": {}},
                    "finalize": {"status": "complete"}}
        for kind, payload in payloads.items():
            with self.subTest(kind=kind):
                before = self.journal.read_bytes()
                single = self.log(kind, *[f"{key}:={json.dumps(value)}" for key, value in payload.items()])
                batch.write_text(json.dumps({"type": kind, "payload": payload}) + "\n")
                batched = self.log("--batch", str(batch))
                payload_file = pathlib.Path(self.tmp.name) / "payload.json"
                payload_file.write_text(json.dumps(payload))
                direct = [subprocess.run([sys.executable, str(SCRIPTS / "lifecycle_journal.py"), *args],
                                         text=True, capture_output=True, timeout=120) for args in (
                    ["append-kv", str(self.audit), kind] + [f"{k}:={json.dumps(v)}" for k, v in payload.items()],
                    ["append-event", str(self.audit), kind, str(payload_file)])]
                for result in (single, batched, *direct):
                    self.assertNotEqual(0, result.returncode, result.stdout)
                    self.assertIn("never logged by hand", result.stderr)
                self.assertEqual(before, self.journal.read_bytes())
        run = json.loads((self.audit / "run.json").read_text())
        self.assertNotEqual("complete", run.get("status"))
        # the tools write through the Python API, which is unchanged: finalize_run.sh's own close still lands
        lifecycle_journal.append_events(self.audit, [
            {"type": "finalize", "payload": {"status": "complete"}, "source": "finalize_run.sh"}])
        self.assertEqual("complete", json.loads((self.audit / "run.json").read_text())["status"])

    def test_the_documented_publication_redaction_declaration_is_journaled_again(self):
        # INSTALL.md's operator command; refused as an unknown type on every metadata-2.1 audit since 2.7.6
        pointer = "/events/*/payload/record/error"
        result = self.log("publication_redaction", f"fields_requiring_removal:={json.dumps([pointer])}",
                          "note=internal telemetry error strings are dropped before any extract")
        self.assertEqual(0, result.returncode, result.stderr)
        run = json.loads((self.audit / "run.json").read_text())
        self.assertIn(pointer, run["publication_redaction"]["fields_requiring_removal"])
        self.assertEqual([], self.schema_errors())


if __name__ == "__main__":
    unittest.main()
