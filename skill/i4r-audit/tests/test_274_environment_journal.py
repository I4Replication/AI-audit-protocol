"""2.7.4: the software environment is journaled, so run.json keeps it.

capture_environment.sh used to write ``environment`` straight into run.json at init. On a metadata-2.1 audit run.json
is a projection of run.events.jsonl, so the first later append (``log_event.sh phase_start phase=intake``)
re-projected run.json without it: 31 of the 32 audits under runs/ on 2026-09-30 had none. The capture now journals
one ``environment_captured`` event through ``lifecycle_journal.record_environment`` and the projection carries the
latest one into run.json. Every test builds the condition on disk and runs the real scripts. Each has a negative
control: a pre-2.7.4 direct write is still lost, a refused event leaves the journal byte-identical, and the gates
still refuse what they refused.
"""
from __future__ import annotations

import csv
import io
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_deposit_values as cdv  # noqa: E402
import check_release_privacy as crp  # noqa: E402
import lifecycle_journal as lj  # noqa: E402

# The shapes of the reservation owners in use on 2026-09-30 (user@short-host, synthetic names here), which neither
# gate reads as an address.
ENVIRONMENT = {
    "captured_at": "2026-09-30T12:00:00Z", "operator": "opa@1234-ABCDEFGHIJ-PF",
    "os": "macOS 27.0.1 26A434", "r": 'R version 4.5.2 (2025-10-31) -- "[Not] Part in a Rumble"',
    "r_packages": {"haven": "2.5.5", "lavaan": "0.6.20"}, "python": "Python 3.12.12",
    "python_packages": {"pandas": "2.3.3", "jsonschema": "absent"}, "pandoc": "pandoc 3.10.2",
    "libreoffice": "LibreOffice 26.2.5.2 cd7284b4cbbfeb507e630c1aac019f4157393acb", "codex_cli": "codex-cli 0.159.0",
}


def clean_env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("I4R_", "RUN_JSON"))}
    # CODEX pinned to nothing: the capture must not run the operator's real Codex CLI in a test
    env.update(PYTHONDONTWRITEBYTECODE="1", LC_ALL="C", CODEX="/nonexistent/codex", **extra)
    return env


def run(argv: list[str], **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(argv, text=True, capture_output=True, timeout=300, env=clean_env(**env), check=False)


def journal_bytes(audit: pathlib.Path) -> bytes:
    return (audit / lj.JOURNAL_NAME).read_bytes()


def run_json(audit: pathlib.Path) -> dict:
    return json.loads((audit / "run.json").read_text(encoding="utf-8"))


def events_of(audit: pathlib.Path, kind: str) -> list[dict]:
    return [event for event in lj.load_events(audit) if event["type"] == kind]


class Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="i4r-274-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)

    def init_audit(self, name: str = "audit") -> pathlib.Path:
        """A real metadata-2.1 audit: init_run.sh, which runs capture_environment.sh after it."""
        audit = self.work / name
        (audit / "pkg").mkdir(parents=True)
        (audit / "pkg" / "f.csv").write_text("x\n1\n", encoding="utf-8")
        result = run(["/bin/bash", str(SCRIPTS / "init_run.sh"), str(audit), "--audit-id", "t-274"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return audit

    def api_audit(self, name: str = "api") -> pathlib.Path:
        """A bare journal (no capture): the API fixtures below add exactly the events a case needs."""
        audit = self.work / name
        audit.mkdir()
        lj.initialize(audit, {"audit_id": "t-274-api", "started_at": "2026-09-30T11:00:00Z", "status": "ready"})
        return audit


class InitJournalsTheEnvironment(Workspace):
    def test_the_capture_survives_every_later_append_and_rebuild(self) -> None:
        audit = self.init_audit()
        captured = events_of(audit, lj.ENVIRONMENT_EVENT)
        self.assertEqual([(event["seq"], event["source"]) for event in captured], [(2, "capture_environment.sh")])
        environment = captured[0]["payload"]["environment"]
        self.assertEqual(run_json(audit)["environment"], environment)
        self.assertIsNone(lj.environment_payload_problem({"environment": environment}))
        # the observed failure: the first append after init
        result = run(["bash", str(SCRIPTS / "log_event.sh"), "--run", str(audit / "run.json"),
                      "phase_start", "phase=intake"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(run_json(audit)["environment"], environment)
        for command in ("project", "recover"):
            result = run([sys.executable, str(SCRIPTS / "lifecycle_journal.py"), command, str(audit)])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(run_json(audit)["environment"], environment, command)

    def test_negative_control_a_direct_run_json_write_is_still_lost(self) -> None:
        """What capture_environment.sh did before 2.7.4: the projection, not the append, keeps the record."""
        audit = self.api_audit()
        record = run_json(audit)
        record["environment"] = dict(ENVIRONMENT)
        (audit / "run.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        result = run(["bash", str(SCRIPTS / "log_event.sh"), "--run", str(audit / "run.json"),
                      "phase_start", "phase=intake"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("environment", run_json(audit))


class CaptureScript(Workspace):
    def capture(self, audit: pathlib.Path, **env: str) -> subprocess.CompletedProcess:
        return run(["bash", str(SCRIPTS / "capture_environment.sh"), str(audit)], **env)

    def test_the_latest_capture_is_the_record_and_both_stay_journaled(self) -> None:
        audit = self.api_audit()
        later = {**ENVIRONMENT, "captured_at": "2026-09-30T12:30:00Z", "python": "Python 3.13.1"}
        lj.record_environment(audit, ENVIRONMENT)
        lj.record_environment(audit, later)
        self.assertEqual([event["payload"]["environment"] for event in events_of(audit, lj.ENVIRONMENT_EVENT)],
                         [ENVIRONMENT, later])
        self.assertEqual(run_json(audit)["environment"], later)

    def test_an_address_shaped_owner_is_replaced_by_the_account(self) -> None:
        audit = self.api_audit()
        owner = "first.last" + "@" + "uni-nowhere.edu"
        result = self.capture(audit, I4R_OWNER=owner)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("I4R_OWNER is shaped like an e-mail or IP address", result.stderr)
        operator = run_json(audit)["environment"]["operator"]
        self.assertNotEqual(operator, owner)
        self.assertTrue(operator.startswith(os.environ.get("USER", "?") + "@"), operator)
        self.assertEqual(crp.inspect_file(audit / "run.json", "run.json", identifiers=True), [])
        # negative control: the owner as given is exactly what the release privacy gate refuses in a run.json
        forced = self.work / "forced" / "run.json"
        forced.parent.mkdir()
        forced.write_text((audit / "run.json").read_text(encoding="utf-8").replace(operator, owner), encoding="utf-8")
        self.assertTrue([issue for issue in crp.inspect_file(forced, "run.json", identifiers=True)
                         if "email" in issue.category], "the gate must refuse the owner as given")

    def test_an_owner_the_coordinator_would_refuse_is_replaced_by_the_account(self) -> None:
        """run.json is the coordinator's context/run.json, and its events keep every capture's payload: an operator
        the coordinator's provider-safety scan refuses would stop every coordinator run of the audit for good."""
        import run_coordinator
        audit = self.api_audit()
        owner = "lab@MBP16"   # a host named like this is participant-code-shaped
        result = self.capture(audit, I4R_OWNER=owner)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("I4R_OWNER holds a private path or a participant-code-shaped token", result.stderr)
        record = run_json(audit)
        operator = record["environment"]["operator"]
        self.assertIn(operator, (os.environ.get("USER", "?") + "@" + socket.gethostname().split(".")[0], None))
        self.assertNotIn(owner, (audit / "run.json").read_text(encoding="utf-8"))
        self.assertNotIn(owner.encode(), journal_bytes(audit))
        run_coordinator.validate_provider_visible_string((audit / "run.json").read_text(encoding="utf-8"), "run.json")
        # negative control: the owner as given is exactly what the coordinator refuses in its context/run.json
        record["environment"]["operator"] = owner
        with self.assertRaises(run_coordinator.CoordinatorError):
            run_coordinator.validate_provider_visible_string(json.dumps(record, indent=2), "run.json")
        # an account the scan refuses, with no I4R_OWNER, leaves the operator unrecorded
        audit = self.api_audit("account")
        result = self.capture(audit, USER="SUBJ_P12")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the environment records no operator", result.stderr)
        self.assertIsNone(run_json(audit)["environment"]["operator"])
        self.assertNotIn(b"SUBJ_P12", journal_bytes(audit))

    def test_a_tool_reading_the_coordinator_would_refuse_is_recorded_as_null(self) -> None:
        import run_coordinator
        audit = self.api_audit()
        tools = self.work / "bin"
        tools.mkdir()
        pandoc = tools / "pandoc"
        pandoc.write_text('#!/bin/bash\necho "pandoc 3.10.2 SUBJ_P12"\n', encoding="utf-8")
        pandoc.chmod(0o755)
        path = f"{tools}{os.pathsep}{os.environ.get('PATH', '')}"
        result = self.capture(audit, PATH=path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the pandoc reading holds a private path or a participant-code-shaped token", result.stderr)
        self.assertIsNone(run_json(audit)["environment"]["pandoc"])
        self.assertNotIn(b"SUBJ_P12", journal_bytes(audit))
        run_coordinator.validate_provider_visible_string((audit / "run.json").read_text(encoding="utf-8"), "run.json")
        # negative control: the same tool with a clean banner is recorded as it prints it
        pandoc.write_text('#!/bin/bash\necho "pandoc 3.10.2-fake"\n', encoding="utf-8")
        result = self.capture(audit, PATH=path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("pandoc reading", result.stderr)
        self.assertEqual(run_json(audit)["environment"]["pandoc"], "pandoc 3.10.2-fake")

    def test_an_owner_the_gates_accept_is_kept(self) -> None:
        audit = self.api_audit()
        result = self.capture(audit, I4R_OWNER="opd@XYPC")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("I4R_OWNER", result.stderr)
        self.assertEqual(run_json(audit)["environment"]["operator"], "opd@XYPC")
        self.assertIn(f"journal seq {len(lj.load_events(audit))}", result.stdout)

    def test_a_finalized_audit_refuses_until_it_is_reopened(self) -> None:
        audit = self.api_audit()
        lj.append_event(audit, "finalize", {"status": "complete"}, source="finalize_run.sh")
        before = journal_bytes(audit)
        result = self.capture(audit)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("finalized", result.stderr)
        self.assertEqual(journal_bytes(audit), before)
        self.assertNotIn("environment", run_json(audit))
        # negative control: the same audit, reopened by its tool's event
        lj.append_event(audit, lj.REOPEN_EVENT, {"reason": "test"}, source="reopen_audit.py")
        result = self.capture(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsInstance(run_json(audit)["environment"], dict)

    def test_a_legacy_run_json_is_still_written_in_place(self) -> None:
        audit = self.work / "legacy"
        audit.mkdir()
        (audit / "run.json").write_text('{"schema_version": "1.0.0", "events": []}\n', encoding="utf-8")
        result = self.capture(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("environment recorded:", result.stdout)
        self.assertFalse((audit / lj.JOURNAL_NAME).exists())
        self.assertIsInstance(run_json(audit)["environment"], dict)

    def test_a_metadata_21_run_json_without_its_journal_is_refused(self) -> None:
        audit = self.work / "orphan"
        audit.mkdir()
        (audit / "run.json").write_text('{"schema_version": "2.1.0", "events": []}\n', encoding="utf-8")
        result = self.capture(audit)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("run init_run.sh first", result.stderr)
        self.assertNotIn("environment", run_json(audit))


class NothingElseWritesIt(Workspace):
    def assert_refused(self, audit: pathlib.Path, argv: list[str], expected: str, **env: str) -> None:
        before = journal_bytes(audit)
        result = run(argv, **env)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected, result.stderr)
        self.assertEqual(journal_bytes(audit), before)

    def test_log_event_refuses_both_types_single_and_batch(self) -> None:
        audit = self.api_audit()
        log = ["bash", str(SCRIPTS / "log_event.sh"), "--run", str(audit / "run.json")]
        for kind in (lj.ENVIRONMENT_EVENT, lj.LEGACY_ENVIRONMENT_EVENT):
            with self.subTest(kind=kind):
                self.assert_refused(audit, log + [kind, f"environment:={json.dumps(ENVIRONMENT)}"],
                                    "journaled only by scripts/capture_environment.sh")
                batch = self.work / f"{kind}.jsonl"
                batch.write_text(json.dumps({"type": kind, "payload": {"environment": ENVIRONMENT}}) + "\n")
                self.assert_refused(audit, log + ["--batch", str(batch)],
                                    "journaled only by scripts/capture_environment.sh")

    def test_the_journal_command_line_refuses_both_types(self) -> None:
        audit = self.api_audit()
        cli = [sys.executable, str(SCRIPTS / "lifecycle_journal.py")]
        payload = self.work / "payload.json"
        payload.write_text(json.dumps({"environment": ENVIRONMENT}))
        self.assert_refused(audit, cli + ["append-event", str(audit), lj.ENVIRONMENT_EVENT, str(payload)],
                            "written only by their tool, never logged by hand: bash scripts/capture_environment.sh")
        self.assert_refused(audit, cli + ["append-kv", str(audit), lj.ENVIRONMENT_EVENT,
                                          f"environment:={json.dumps(ENVIRONMENT)}"], "never logged by hand")
        self.assert_refused(audit, cli + ["append-kv", str(audit), lj.LEGACY_ENVIRONMENT_EVENT, "os=x"],
                            f"journaled as {lj.ENVIRONMENT_EVENT}, only by bash scripts/capture_environment.sh")

    def test_a_hand_metadata_correction_may_not_set_it(self) -> None:
        audit = self.api_audit()
        log = ["bash", str(SCRIPTS / "log_event.sh"), "--run", str(audit / "run.json"), "metadata_correction"]
        self.assert_refused(audit, log + [f"set:={json.dumps({'environment': ENVIRONMENT})}"],
                            "metadata_correction may not set environment by hand")
        # negative control: the same event on an unowned field is written
        result = run(log + ['set:={"notes": "a correction"}'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(run_json(audit)["notes"], "a correction")

    def test_a_malformed_capture_is_refused_by_every_writer(self) -> None:
        audit = self.api_audit()
        cases = {
            "not an object": None,
            "an unknown field": {**ENVIRONMENT, "kernel": "x"},
            "a non-string version": {**ENVIRONMENT, "os": 27},
            "a non-string package version": {**ENVIRONMENT, "r_packages": {"lavaan": 0.6}},
            "no captured_at": {key: value for key, value in ENVIRONMENT.items() if key != "captured_at"},
            "a zone-less captured_at": {**ENVIRONMENT, "captured_at": "2026-09-30T12:00:00"},
        }
        before = journal_bytes(audit)
        for label, environment in cases.items():
            with self.subTest(label):
                with self.assertRaises(lj.JournalError):
                    lj.record_environment(audit, environment)
                # the lower-level append the tools share refuses it too, whoever calls it
                with self.assertRaises(lj.JournalError):
                    lj.append_event(audit, lj.ENVIRONMENT_EVENT, {"environment": environment})
                self.assertEqual(journal_bytes(audit), before)
        with self.assertRaises(lj.JournalError):
            lj.append_event(audit, lj.ENVIRONMENT_EVENT, {"environment": ENVIRONMENT, "extra": 1})
        self.assertEqual(journal_bytes(audit), before)
        # negative control: the well-formed capture is written
        lj.record_environment(audit, ENVIRONMENT)
        self.assertEqual(run_json(audit)["environment"], ENVIRONMENT)


class ExistingJournalsProjectAsBefore(Workspace):
    def test_a_hand_logged_environment_event_is_still_not_projected(self) -> None:
        """Three journals (one under runs/) hold a pre-2.7.4 hand-logged `environment` event. They keep the
        run.json their journal gives today: without an environment."""
        audit = self.api_audit()
        with lj.journal_lock(audit):
            events = lj._append_locked(audit, [{"type": lj.LEGACY_ENVIRONMENT_EVENT, "source": "log_event.sh",
                                                "payload": {"environment": ENVIRONMENT}}])
            lj.project_all_locked(audit, events)
        self.assertNotIn("environment", run_json(audit))
        # negative control: the 2.7.4 event on the same journal is projected
        lj.record_environment(audit, ENVIRONMENT)
        self.assertEqual(run_json(audit)["environment"], ENVIRONMENT)

    def test_both_event_schemas_admit_the_type(self) -> None:
        run_schema = json.loads((SKILL / "schemas/run.schema.json").read_text(encoding="utf-8"))
        lifecycle = json.loads((SKILL / "schemas/v2/lifecycle-event.schema.json").read_text(encoding="utf-8"))
        for enum in (run_schema["$defs"]["event"]["properties"]["type"]["enum"],
                     lifecycle["properties"]["type"]["enum"]):
            self.assertIn(lj.ENVIRONMENT_EVENT, enum)
        # 2.7.6: the legacy type validates in run.json (the three audits holding one can finalize), and the journal's
        # own schema still lacks it, so no append command accepts a new one
        self.assertIn(lj.LEGACY_ENVIRONMENT_EVENT, run_schema["$defs"]["event"]["properties"]["type"]["enum"])
        self.assertNotIn(lj.LEGACY_ENVIRONMENT_EVENT, lifecycle["properties"]["type"]["enum"])

    def test_a_projected_run_with_the_capture_validates_as_finalize_validates_it(self) -> None:
        import validate_json_schema
        audit = self.init_audit()
        errors = validate_json_schema.errors_for(audit / "run.json", SKILL / "schemas/run.schema.json")
        self.assertFalse([error for error in errors if "environment" in error or "/events/1" in error], errors)


class PublicationGates(Workspace):
    """run.json is filed with the comment and meets both publication gates there (publish_completed.sh)."""

    def test_the_environment_is_provenance_for_check_3c_and_notes_are_not(self) -> None:
        name = "Quennell Ardmore"   # synthetic; a deposit name that also appears in both fields below
        audit, release = self.work / "audit", self.work / "release"
        for directory in ("pkg/data", "paper", "report", "work"):
            (audit / directory).mkdir(parents=True)
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="\n").writerows([["Name", "score"], [name, "3"], ["Other Person", "4"]])
        (audit / "pkg/data/survey.csv").write_text(buffer.getvalue(), encoding="utf-8")
        (audit / "paper/paper.txt").write_text("A synthetic article\nSynthetic Author\n", encoding="utf-8")
        (audit / "report/front_matter.md").write_text("Synthetic Author\n", encoding="utf-8")
        release.mkdir()
        text = json.dumps({"environment": {**ENVIRONMENT, "os": f"{name} build"}, "notes": name}, indent=2) + "\n"
        (release / "run.json").write_text(text, encoding="utf-8")
        lines = {key: number for number, line in enumerate(text.splitlines(), 1)
                 for key in ("os", "notes") if f'"{key}"' in line}
        refused = {hit.location for hit in cdv.scan_release(audit, release, cdv.build_store(audit)).hits}
        # negative control: the same value in a field a person types is refused
        self.assertIn(f"run.json line {lines['notes']}", refused)
        self.assertNotIn(f"run.json line {lines['os']}", refused)

    def test_a_real_capture_passes_the_release_privacy_gate(self) -> None:
        import run_coordinator
        audit = self.init_audit()
        self.assertEqual(crp.inspect_file(audit / "run.json", "run.json", identifiers=True), [])
        for operator in ("opb@Operators-MacBook-Pro", "opa@1234-ABCDEFGHIJ-PF",
                         "opc@Hosts-MacBook-Pro", "opd@XYPC"):
            block = json.dumps({"environment": {**ENVIRONMENT, "operator": operator}}, indent=2)
            with self.subTest(operator=operator):
                self.assertEqual(crp.inspect_text(block, "run.json", identifiers=True), [])
                # and the coordinator's provider-safety scan, so the capture keeps these operators as given
                run_coordinator.validate_provider_visible_string(block, "run.json")


if __name__ == "__main__":
    unittest.main()
