"""2.7.6: six defects that left real audits unable to continue, each tested on disk with the real scripts.

1. Staging refused a home path or an e-mail address inside a record nobody can reword (a critic's output, a Phase-1
   evidence script), so two audits could never stage. Check 3d masks the STAGED copy and lists it.
2. Text an operator logged (an address, a home path, a participant code) blocked the coordinator and publication for
   good, because run.json is projected from an append-only journal. Such text is refused at append, and a
   text_redaction event masks text already journaled, in run.json only.
3. The e-mail detector was quadratic on long runs of address characters (knitted HTML), so one Phase-1 preflight
   never met its 60 s deadline.
4. A legacy or unknown event type made finalize impossible. Unknown types are refused at append; the legacy
   ``environment`` type is a run.schema.json event type.
5. new_audit.sh took a .claude/ folder (or an empty one) as the whole package.
7. A coordinator call that died without a result envelope consumed no attempt, so reruns were unbounded.

Each test has a negative control: what was accepted before is still accepted.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

TESTS = pathlib.Path(__file__).resolve().parent
SKILL = TESTS.parent
SCRIPTS = SKILL / "scripts"
for _path in (str(SCRIPTS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import check_deposit_values as cdv  # noqa: E402
import check_release_privacy as crp  # noqa: E402
import email_scan  # noqa: E402
import lifecycle_journal as lj  # noqa: E402
import phase1_runtime  # noqa: E402
import release_record_masking as rrm  # noqa: E402
import test_run_coordinator as trc  # noqa: E402  (fixtures only; its tests stay in its own module)
import test_tree_layout as layout  # noqa: E402

# Home paths are assembled, never spelled: this file is shipped and scanned like any other.
HOME = "/" + "Users/"
WIN_HOME = "C:/" + "Users/"


def clean_env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("I4R_", "RUN_JSON"))}
    env.update(PYTHONDONTWRITEBYTECODE="1", LC_ALL="C", CODEX="/nonexistent/codex", **extra)
    return env


def run(argv: list, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(item) for item in argv], text=True, capture_output=True, timeout=300,
                          env=clean_env(**env), check=False)


class EmailDetectorIsLinear(unittest.TestCase):
    def test_a_long_run_of_address_characters_is_scanned_in_well_under_a_second(self) -> None:
        for pattern in (phase1_runtime.EMAIL, crp._EMAIL):
            for size in (80_000, 400_000):
                text = "a+b-" * (size // 4)
                started = time.monotonic()
                self.assertEqual(email_scan.findall(pattern, text), [])
                self.assertLess(time.monotonic() - started, 1.0, f"{size} characters")

    def test_the_matches_are_the_unbounded_patterns_own(self) -> None:
        samples = [
            "write to jane.doe@uni.example.org or j@x.co.uk.", "a@b.cd@e.fg", "x" * 300 + "@host.org",
            "user+tag@sub.domain.museum, other@ip", "no address here", "@lead.org and trail@", "mail:a.b-c@d-e.fr;",
            "q" * 90 + "@" + "r" * 300 + ".com",
        ]
        for text in samples:
            for pattern in (phase1_runtime.EMAIL, crp._EMAIL):
                self.assertEqual(email_scan.findall(pattern, text), pattern.findall(text), text[:40])

    def test_the_one_difference_is_a_local_part_longer_than_the_reach(self) -> None:
        # RFC 5321 caps a local part at 64 characters; email_scan.REACH (320) is five times that
        text = "x" * (email_scan.REACH + 80) + "@host.org"
        self.assertEqual(email_scan.findall(crp._EMAIL, text), [])
        self.assertEqual(len(crp._EMAIL.findall(text)), 1)


class JournalAudit(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-276-journal-")
        self.addCleanup(directory.cleanup)
        self.audit = pathlib.Path(directory.name).resolve() / "audit"
        self.audit.mkdir()
        result = run(["/bin/bash", SCRIPTS / "init_run.sh", self.audit, "--audit-id", "t-276"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def log(self, *arguments: str) -> subprocess.CompletedProcess:
        return run(["/bin/bash", SCRIPTS / "log_event.sh", *arguments], RUN_JSON=str(self.audit / "run.json"))

    def journal(self) -> bytes:
        return (self.audit / lj.JOURNAL_NAME).read_bytes()

    def last_seq(self) -> int:
        return lj.load_events(self.audit)[-1]["seq"]


class OperatorTextIsCheckedAndMaskable(JournalAudit):
    def test_an_address_a_home_path_or_a_participant_code_is_refused_and_nothing_is_written(self) -> None:
        before = self.journal()
        for arguments, message in (
            (("note", "text=sent to jane.doe@uni.ca"), "contains an e-mail address"),
            (("delivery", "artifact_path=report/comment.pdf", "recipient=editor@journal.org"), "e-mail address"),
            (("failure", "kind=other", f"detail=read {HOME}kmensah/pkg/data.csv"), "private absolute path"),
            (("brief_correction", "raised_by=auditor_privacy_archive", "description=file EMG_P41.csv"),
             "participant-code"),
        ):
            with self.subTest(arguments=arguments):
                result = self.log(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.journal(), before)

    def test_negative_control_ordinary_text_and_example_addresses_are_journaled(self) -> None:
        for text in ("H1 and Study 2, Table S1", "fixture jane@example.com", "participant P041 timing"):
            with self.subTest(text=text):
                self.assertEqual(self.log("note", f"text={text}").returncode, 0)
        self.assertEqual(self.log("delivery", "artifact_path=report/comment.pdf",
                                  "recipient=I4R editorial queue").returncode, 0)

    def test_a_text_redaction_masks_run_json_and_leaves_the_journal_as_it_was(self) -> None:
        # a pre-2.7.6 journal: the note went in before the refusal existed
        lj.append_events(self.audit, [{"type": "note", "payload": {"text": "copy sent to jane.doe@uni.ca"},
                                       "source": "log_event.sh"}])
        target = self.last_seq()
        self.assertIn("jane.doe@uni.ca", (self.audit / "run.json").read_text(encoding="utf-8"))
        journal_before = self.journal()
        result = self.log("text_redaction", f"seq={target}", "field=text", "reason=an address in a note")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.journal().startswith(journal_before), "the journal is only appended to")
        self.assertIn(b"jane.doe@uni.ca", self.journal())
        projected = (self.audit / "run.json").read_text(encoding="utf-8")
        self.assertNotIn("jane.doe@uni.ca", projected)
        self.assertIn("[redacted]", projected)

    def test_a_redaction_must_name_an_existing_event_and_one_of_its_text_fields(self) -> None:
        self.assertEqual(self.log("note", "text=plain").returncode, 0)
        target = self.last_seq()
        before = self.journal()
        for arguments, message in (
            ((f"seq={target + 50}", "field=text", "reason=x"), "no journal event has seq"),
            ((f"seq={target}", "field=type", "reason=x"), "field must be one of"),
            ((f"seq={target}", "field=detail", "reason=x"), "has no text field"),
            ((f"seq={target}", "field=text"), "needs reason="),
        ):
            with self.subTest(arguments=arguments):
                result = self.log("text_redaction", *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.journal(), before)


class EventTypes(JournalAudit):
    def test_an_unknown_type_is_refused_by_every_append_command(self) -> None:
        before = self.journal()
        batch = self.audit.parent / "batch.jsonl"
        batch.write_text(json.dumps({"type": "phase_ended", "payload": {"phase": "intake"}}) + "\n")
        payload = self.audit.parent / "payload.json"
        payload.write_text("{}")
        for arguments in (("append-kv", self.audit, "phase_ended", "phase=intake"),
                          ("append-event", self.audit, "operator_note", payload),
                          ("append-batch", self.audit, batch)):
            with self.subTest(command=arguments[0]):
                result = run([sys.executable, SCRIPTS / "lifecycle_journal.py", *arguments])
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.journal(), before)

    def test_the_legacy_environment_type_validates_and_is_still_refused_at_append(self) -> None:
        schema = json.loads((SKILL / "schemas/run.schema.json").read_text(encoding="utf-8"))
        self.assertIn("environment", json.dumps(schema["$defs"]["event"]))
        enum = [value for value in re.findall(r'"([a-z_]+)"', json.dumps(schema["$defs"]["event"]["properties"]["type"]))]
        self.assertIn("environment", enum)
        self.assertIn("text_redaction", enum)
        result = run([sys.executable, SCRIPTS / "lifecycle_journal.py", "append-kv", self.audit, "environment",
                      "os=macOS"])
        self.assertNotEqual(result.returncode, 0)


class NewAuditPicksThePackage(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-276-intake-")
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()
        self.state = self.root.parent / (self.root.name + "-access")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, ignore_errors=True))

    def new_audit(self, paper: pathlib.Path, *arguments: str) -> subprocess.CompletedProcess:
        return layout.sh("/bin/bash", SCRIPTS / "new_audit.sh", paper, *arguments,
                         env={"FAKE_CODEX_STATE": str(self.state / "fake"), "I4R_STATE_DIR": str(self.state / "home")})

    def test_a_claude_folder_beside_the_loose_files_is_not_the_package(self) -> None:
        paper = layout.make_paper(self.root, "J", "P", zips=())
        (paper / ".claude").mkdir()
        (paper / ".claude" / "settings.local.json").write_text("{}")
        (paper / "analysis.R").write_text("x <- 1\n")
        result = self.new_audit(paper)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        pkg = self.root / "runs" / "J" / "P" / "audit" / "pkg"
        self.assertEqual(sorted(path.name for path in pkg.rglob("*") if path.is_file()), ["analysis.R"])

    def test_a_folder_beside_loose_files_is_refused_with_both_choices(self) -> None:
        paper = layout.make_paper(self.root, "J", "P", zips=())
        (paper / "figures").mkdir()
        (paper / "figures" / "f1.png").write_bytes(b"png")
        (paper / "analysis.R").write_text("x <- 1\n")
        result = self.new_audit(paper)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--package", result.stderr)
        self.assertFalse((self.root / "runs").exists())

    def test_an_empty_package_is_refused_before_anything_is_reserved(self) -> None:
        paper = layout.make_paper(self.root, "J", "P", zips=())
        (paper / "replication").mkdir()
        result = self.new_audit(paper)
        self.assertEqual(result.returncode, 2)
        self.assertIn("holds no files", result.stderr)
        self.assertFalse((self.root / "runs").exists())

    def test_negative_control_a_lone_package_folder_is_still_taken(self) -> None:
        paper = layout.make_paper(self.root, "J", "P", zips=())
        (paper / "replication").mkdir()
        (paper / "replication" / "analysis.R").write_text("x <- 1\n")
        result = self.new_audit(paper)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class StagedRecordsAreNeutralised(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-276-stage-")
        self.addCleanup(directory.cleanup)
        self.release = pathlib.Path(directory.name).resolve()
        self.addCleanup(crp.ALLOWED_EMAILS.clear)

    def write(self, relative: str, text: str) -> pathlib.Path:
        path = self.release / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_records_are_masked_and_listed_while_the_comment_still_refuses(self) -> None:
        crp.ALLOWED_EMAILS.add("corresponding@uni.edu")
        critic = self.write("critique/round_2/critic_stats.md",
                            f'The authors call setwd("{WIN_HOME}kwame.mensah/Desktop/study") and\n'
                            "write to k.mensah@uni.edu; corresponding@uni.edu is the byline's.\n")
        evidence = self.write("work/a5_privacy_archive/phase1_evidence/abc/scan.py",
                              json.dumps({"path": f"{HOME}kmensah/pkg/data.csv", "fixture": f"{HOME}janedoe/x"}) + "\n")
        report = self.write("REPORT_final.md", f"See {HOME}kmensah/pkg and k.mensah@uni.edu.\n")
        rewritten = rrm.neutralise_records(self.release, cdv.record_file)
        rrm.note(self.release, rewritten)
        self.assertEqual(sorted(rewritten), [("critique/round_2/critic_stats.md", 1, 1),
                                             ("work/a5_privacy_archive/phase1_evidence/abc/scan.py", 1, 0)])
        self.assertIn(f"{WIN_HOME}<user-dir>/Desktop/study", critic.read_text(encoding="utf-8"))
        self.assertIn("[e-mail address]", critic.read_text(encoding="utf-8"))
        self.assertIn("corresponding@uni.edu", critic.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(evidence.read_text(encoding="utf-8"))["fixture"], f"{HOME}janedoe/x")
        note = (self.release / crp.NEUTRALISATION_NOTE).read_text(encoding="utf-8")
        self.assertIn("critique/round_2/critic_stats.md  (1 path(s), 1 address(es))", note)
        self.assertNotIn("mensah", note)
        issues = crp.inspect_tree(self.release, identifiers=True)
        self.assertTrue(issues)
        self.assertEqual({issue.location.split(":")[0] for issue in issues}, {"REPORT_final.md"})
        self.assertIn("kmensah", report.read_text(encoding="utf-8"), "the comment's own output is never rewritten")


class CoordinatorChargesEnvelopeLessCalls(unittest.TestCase):
    setUp = trc.CoordinatorRunnerTests.setUp
    tearDown = trc.CoordinatorRunnerTests.tearDown
    run_runner = trc.CoordinatorRunnerTests.run_runner
    ledger = trc.CoordinatorRunnerTests.ledger
    upgrade_to_metadata_21 = trc.CoordinatorRunnerTests.upgrade_to_metadata_21

    def test_calls_that_wrote_files_and_died_stop_at_the_attempt_budget(self) -> None:
        self.upgrade_to_metadata_21()
        with mock.patch.dict(os.environ, {"I4R_MAX_ATTEMPTS": "2"}):
            for _ in range(2):
                result = self.run_runner("crash_after_write")
                self.assertNotEqual(result.returncode, 0)
            refused = self.run_runner("crash_after_write")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("I4R_MAX_ATTEMPTS", refused.stderr + refused.stdout)
        attempts = [row["attempt"] for row in self.ledger() if row["role"] == "coordinator"]
        self.assertEqual(attempts, [1, 2])

    def test_negative_control_a_rejection_envelope_still_costs_no_attempt(self) -> None:
        self.upgrade_to_metadata_21()
        result = self.run_runner("authentication_rejection")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIsNone(self.ledger()[-1]["attempt"])


if __name__ == "__main__":
    unittest.main()
