"""2.6.9 release review: the re-file path for published audits, and check 3c on records nobody can reword.

Each class is a behavioural regression test for one defect the release review of the integrated 2.6.9
found; each fails on the pre-review tree and passes here. No provider or model is called, and every
personal value is synthetic and out of range.

* ``FrozenBoundBeforeFindings`` (make_diff_packet.py). A round's step-1 registry copy that was renumbered
  by a version that kept no history (audit A20: F19 back to F4 after round 4, only the draft map in
  id_map.json) differs from integrate_round's backup, while the next round's frozen packet binds exactly
  its bytes. The packet could not be rebuilt -- so the last round's receipt could not be regenerated and
  the published audit could not be re-filed -- and the printed restore copied the same bytes over
  themselves. Those bytes are now authoritative; a copy that differs from them is still refused, with a
  restore that changes something.
* ``DisclosureEnforcementFrom269`` (disclosure_policy.ENFORCED_FROM). An audit initialised on the live
  2.6.8, whose prompts still said to pause for a disclosure decision, is re-finalized with warnings
  (audit A27: pii_present true, flagged findings, empty flags and statement).
* ``DeferredRowsByCriticItem`` (disclosure_policy.deferred_rows). The stop may list a deferred
  disclosure row by the critic item that assessed it (``critic_stats:S3``), as the adjudication guide
  says to copy the ids from the round records.
* ``UnapprovingAFlaggedRowKeepsItUnderReview`` (disclosure_policy.decision_review via
  integrate_round.py apply). integrate's own remedy for an OVERSTATED verdict on an approved row, leave
  it unapproved, is no longer refused for a disclosure-flagged row that stays INCLUDED.
* ``Check3cRecordsAndMarker`` and ``StagingMasksRecords`` (check_deposit_values.py, stage_release.sh).
  A value quoted by a Phase-1 report, a critic output or a work record is masked in the STAGED copy
  and listed in RELEASE_VALUE_REDACTION.txt, never refused with a remedy nobody can apply; the comment's
  own outputs still refuse. The generic e-mail detector honours check 4's per-line
  ``# i4r-privacy: not-an-email`` marker in code files.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

# Modules, not classes: the loader must not collect their tests here again.
from tests import test_264_verifier_confirmation as confirm  # noqa: E402
from tests import test_268_pii_report_not_block as pii  # noqa: E402
from tests import test_check_deposit_values as values_tests  # noqa: E402
from tests import test_diff_packet as packets  # noqa: E402

DIFF = SCRIPTS / "make_diff_packet.py"
VERIFY_DIFF = SCRIPTS / "verify_diff_packet.py"
CHECK = SCRIPTS / "check_deposit_values.py"
AT = "@"
NAME = values_tests.FULL_NAME          # synthetic, in the fixtures' deposits
MARKED_CODE = "z = q" + AT + "keys.data  # i4r-privacy: not-an-email\n"


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: pathlib.Path, content: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class FrozenBoundBeforeFindings(unittest.TestCase):
    def setUp(self) -> None:
        # The diff-packet test fixture, used by composition (its own tests are not collected here).
        self.fixture = packets.DiffPacketTests("test_byte_identical_inputs_emit_content_bound_no_change_packet")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.audit = audit = self.fixture.audit
        reviewed = [self.fixture.row("F1", "data"), self.fixture.row("F2", "statistics")]
        # integrate_round's write-once backup: the ids round 1's critics reviewed
        self.fixture.write_rows("work/findings.before_round1_integration.jsonl", reviewed)
        # A renumber by a version that kept no history moved the two ids in the step-1 copy (and in the
        # registry); only the draft->F map survives in work/id_map.json, so nothing can translate them.
        renumbered = [dict(copy.deepcopy(reviewed[1]), id="F1"), dict(copy.deepcopy(reviewed[0]), id="F2")]
        self.step1 = self.fixture.write_rows("critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl", renumbered)
        (audit / "work/id_map.json").write_text(json.dumps({"CM-F1": "F1", "ST-F2": "F2"}) + "\n")
        self.report = write(audit / "critique/round_1/REPORT_BEFORE_INTEGRATION.md",
                            (audit / "work/report_before.md").read_text())
        after = copy.deepcopy(renumbered)
        after[0]["claim"] = "F1 claim revised at round 1's integration"
        self.fixture.write_rows("work/findings.jsonl", after)
        # Round 2 was built, reviewed and gated from exactly these step-1 bytes: its frozen packet binds them,
        # and its post-delta payload kept a copy (as on the real audit).
        self.frozen_hash = sha(self.step1)
        write(audit / "critique/round_2/snapshots/s1/source/context/DIFF_MANIFEST.json", json.dumps({
            "input_paths": {"before_report": "critique/round_1/REPORT_BEFORE_INTEGRATION.md",
                            "before_findings": "critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl"},
            "inputs": {"before_report_sha256": sha(self.report), "before_findings_sha256": self.frozen_hash},
        }, indent=2) + "\n")
        self.payload = audit / ("critique/round_2/post_delta/" + "a" * 64
                                + "/payload/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl")
        self.payload.parent.mkdir(parents=True)
        shutil.copyfile(self.step1, self.payload)

    def build(self) -> subprocess.CompletedProcess[str]:
        audit = self.audit
        return subprocess.run(
            [sys.executable, str(DIFF), str(audit), "--round", "2",
             "--before-report", str(self.report), "--after-report", str(audit / "report/REPORT_final.md"),
             "--before-findings", str(self.step1), "--after-findings", str(audit / "work/findings.jsonl"),
             "--output", str(audit / "critique/DIFF_MANIFEST_round_2.json")],
            text=True, capture_output=True, timeout=120)

    def test_the_bytes_the_frozen_packet_binds_rebuild_the_packet(self) -> None:
        self.assertNotEqual(self.frozen_hash, sha(self.audit / "work/findings.before_round1_integration.jsonl"))
        built = self.build()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertIn("holds exactly the bytes round 2's frozen diff packet was built from", built.stderr)
        packet = json.loads((self.audit / "critique/DIFF_MANIFEST_round_2.json").read_text())
        self.assertEqual(packet["inputs"]["before_findings_sha256"], self.frozen_hash)
        self.assertEqual(packet["changed_claim_ids"], ["F1"])
        # the check-only rebuild the post-delta gates run (verify_diff_packet, --no-remedy-file) agrees
        verified = subprocess.run([sys.executable, str(VERIFY_DIFF), str(self.audit),
                                   str(self.audit / "critique/DIFF_MANIFEST_round_2.json"), "--round", "2"],
                                  text=True, capture_output=True, timeout=120)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertEqual(sha(self.step1), self.frozen_hash, "the step-1 copy is never rewritten")

    def test_a_copy_that_differs_from_the_bound_bytes_is_refused_with_a_real_restore(self) -> None:
        rows = [json.loads(line) for line in self.step1.read_text().splitlines()]
        rows[1]["claim"] = "edited after the round was built"
        self.step1.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
        refused = self.build()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("must not be replaced", refused.stderr)
        restore = re.search(r"&& cp (\S+) (\S+)", refused.stderr)
        self.assertIsNotNone(restore, refused.stderr)
        source = pathlib.Path(restore.group(1))
        self.assertEqual(sha(source), self.frozen_hash)
        self.assertNotEqual(sha(source), sha(self.step1), "the printed restore must change the file")
        shutil.copyfile(source, self.step1)
        rebuilt = self.build()
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stdout + rebuilt.stderr)


class FinalizeFixture(unittest.TestCase):
    """The shipped example pair through the real finalize_run.sh (as tests/test_268_pii_report_not_block.py),
    with a hook that may add audit files first."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-269-release.")
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.count = 0

    def finalize(self, *, protocol_version: str, mutate=None, registry=None, mutate_run=None,
                 extra=None) -> tuple[int, str]:
        self.count += 1
        audit = self.root / f"audit-{self.count}"
        audit.mkdir()
        run = json.loads((SKILL / "schemas/EXAMPLE_run.json").read_text(encoding="utf-8"))
        run["protocol_version"] = protocol_version
        if mutate_run:
            mutate_run(run)
        write(audit / "run.json", json.dumps(run, indent=2) + "\n")
        comment = json.loads((SKILL / "schemas/EXAMPLE_comment.json").read_text(encoding="utf-8"))
        if mutate:
            mutate(comment)
        write(audit / "comment.json", json.dumps(comment, indent=2) + "\n")
        if registry is not None:
            write(audit / "work/findings.jsonl", "".join(json.dumps(row) + "\n" for row in registry))
        if extra:
            extra(audit)
        report = [f"## {index}. {finding.get('claim') or 'Synthetic'} [claim {index}]\n\nText.\n"
                  for index, finding in enumerate(comment["findings"], 1)]
        write(audit / "report/REPORT_final.md", "\n".join(report) or "# Report with no findings\n")
        write(audit / "verification.Rmd", "---\ntitle: fixture\noutput: html_document\n---\n\nok\n")
        rows = "".join(f"C-{index},fixture,PASS\n" for index in range(1, len(comment["findings"]) + 1))
        write(audit / "verification_summary.fixture.csv", "claim,key,verdict\n" + rows)
        bin_dir = audit / ".verification-test-bin"
        write(bin_dir / "Rscript", "#!/bin/sh\ncp verification_summary.fixture.csv "
                                   "verification_summary.csv\nprintf '0\\n' > verification_status.txt\nexit 0\n")
        os.chmod(bin_dir / "Rscript", 0o755)
        environment = {**os.environ, "LC_ALL": "C", "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
        verification = subprocess.run(["/bin/bash", str(SCRIPTS / "run_verification.sh"), str(audit)],
                                      text=True, capture_output=True, timeout=120, env=environment)
        self.assertEqual(verification.returncode, 0, verification.stdout + verification.stderr)
        (audit / "verification_summary.fixture.csv").unlink()
        shutil.rmtree(bin_dir)
        result = subprocess.run(["/bin/bash", str(SCRIPTS / "finalize_run.sh"), str(audit), "--no-registry"],
                                text=True, capture_output=True, timeout=180,
                                env={**os.environ, "LC_ALL": "C"})
        return result.returncode, result.stdout + result.stderr


class DisclosureEnforcementFrom269(FinalizeFixture):
    @staticmethod
    def as_published_on_268(comment: dict) -> None:
        # pii_present true and flagged comment findings, but no comment-level flags, urgency or statement
        comment["disclosure"].update({"flags": [], "urgency": "none", "statement": ""})

    def test_an_audit_initialised_on_the_live_268_re_finalizes_with_warnings(self) -> None:
        comment = json.loads((SKILL / "schemas/EXAMPLE_comment.json").read_text(encoding="utf-8"))
        self.assertTrue(comment["governance"]["pii_present"])
        self.assertTrue(any(finding["disclosure_flag"] for finding in comment["findings"]))
        rc, output = self.finalize(protocol_version="2.6.8", mutate=self.as_published_on_268)
        self.assertEqual(rc, 0, output)
        self.assertNotIn("SCHEME ERROR", output)
        self.assertIn("governance.pii_present is true but the comment does not report it", output)
        self.assertIn("[warning only: this audit was initialised before skill 2.6.9]", output)

    def test_an_audit_initialised_at_269_is_still_refused(self) -> None:
        rc, output = self.finalize(protocol_version="2.6.9", mutate=self.as_published_on_268)
        self.assertNotEqual(rc, 0, output)
        self.assertIn("governance.pii_present is true but the comment does not report it", output)
        self.assertNotIn("[warning only", output)


class DeferredRowsByCriticItem(FinalizeFixture):
    def critic_record(self, audit: pathlib.Path, finding_id: str = "F2") -> None:
        write(audit / "critique/round_1/critic_stats.json", json.dumps({
            "role": "critic_stats", "round": 1,
            "items": [{"item_id": "S3", "finding_id": finding_id, "verdict": "OVERSTATED"},
                      {"item_id": "S4", "finding_id": "F1", "verdict": "CONFIRMED"}],
            "new_issues": []}, indent=2) + "\n")

    def rows(self) -> list[dict]:
        final = dict(copy.deepcopy(pii.ROW_FIXTURE), id="F1")
        pending = dict(copy.deepcopy(pii.ROW_FIXTURE), id="F2", disclosure_flag=True, coordinator_approved=False)
        return [final, pending]

    def test_a_deferred_row_listed_by_its_critic_item_is_accepted_at_the_cap(self) -> None:
        def listed(run: dict) -> None:
            run["unresolved_conclusion_critical_item_ids"] = ["critic_stats:S3"]

        rc, output = self.finalize(protocol_version="2.6.9", registry=self.rows(), mutate_run=listed,
                                   extra=self.critic_record)
        self.assertEqual(rc, 0, output)
        self.assertIn("registry row F2 (work/findings.jsonl line 2) is disclosure-flagged and still awaiting "
                      "approval at cap_with_unresolved; it is listed as unresolved", output)

    def test_an_item_that_assessed_another_finding_does_not_list_the_row(self) -> None:
        def listed(run: dict) -> None:
            run["unresolved_conclusion_critical_item_ids"] = ["critic_stats:S4"]

        rc, output = self.finalize(protocol_version="2.6.9", registry=self.rows(), mutate_run=listed,
                                   extra=self.critic_record)
        self.assertNotEqual(rc, 0, output)
        self.assertIn("is disclosure-flagged but not final", output)
        self.assertIn("by its finding id (F2) or by the id of a critic item that assessed it", output)

    def test_item_ids_are_translated_through_the_renumber_history(self) -> None:
        import disclosure_policy
        with tempfile.TemporaryDirectory() as scratch:
            audit = pathlib.Path(scratch)
            self.critic_record(audit, finding_id="F5")
            write(audit / "critique/round_1/CURRENT_INVOCATION", "inv-r1\n")
            write(audit / "work/id_map_history.jsonl", json.dumps(
                {"map": {"F5": "F2", "F1": "F1"}, "rounds_run_before_renumber": {"round_1": "inv-r1"}}) + "\n")
            found = disclosure_policy.critic_item_findings(audit)
            self.assertEqual(found["critic_stats:S3"], {"F2"})
            self.assertEqual(found["S3"], {"F2"})
            rows = [(1, dict(self.rows()[1]))]
            self.assertEqual(disclosure_policy.deferred_rows(rows, "cap_with_unresolved", ["critic_stats:S3"],
                                                             True, found), ["F2"])
            self.assertEqual(disclosure_policy.deferred_rows(rows, "cap_with_unresolved", ["critic_stats:S4"],
                                                             True, found), [])


    def test_an_audit_renumbered_without_history_translates_no_item(self) -> None:
        # The A20 shape: work/id_map.json maps original ids to today's F-ids (composed through renumbers after
        # critique) and no id_map_history.jsonl exists, so a round's item finding_id may name a row that has
        # since moved. No item is matched; the row must be listed by its finding id.
        import disclosure_policy
        with tempfile.TemporaryDirectory() as scratch:
            audit = pathlib.Path(scratch)
            self.critic_record(audit, finding_id="F19")
            write(audit / "critique/round_1/CURRENT_INVOCATION", "inv-r1\n")
            write(audit / "work/id_map.json", json.dumps({"REP-a": "F1", "STIM-b": "F4", "PRV-c": "F19"}) + "\n")
            self.assertEqual(disclosure_policy.critic_item_findings(audit), {})
            rows = [(1, dict(self.rows()[1]))]
            self.assertEqual(disclosure_policy.deferred_rows(rows, "cap_with_unresolved", ["critic_stats:S3"],
                                                             True, {}), [])

class UnapprovingAFlaggedRowKeepsItUnderReview(confirm.Fixture):
    def setUp(self) -> None:
        super().setUp()
        pii.stamp_protocol(self.audit, "2.6.9")

    def test_withdrawing_the_approval_of_an_included_flagged_row_is_not_a_withdrawal(self) -> None:
        path = self.audit / "work/findings.jsonl"
        rows = confirm.jsonl(path)
        rows[3]["disclosure_flag"] = True
        fid = rows[3]["id"]
        rows[3].update(confirm.decisions()[fid])   # already approved and confirmed
        confirm.write_jsonl(path, rows)
        decided = confirm.decisions(approve_f13=False, extra={fid: {"coordinator_approved": False}})
        for key in ("report_section", "verification_snippet"):
            decided[fid].pop(key, None)
        result = self.apply(decided, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("withdraws its coordinator approval", result.stderr)
        row = self.registry()[fid]
        self.assertIs(row["coordinator_approved"], False)
        self.assertIs(row["disclosure_flag"], True)
        self.assertEqual(row["ledger_disposition"]["status"], "INCLUDED")

    def test_leaving_the_comment_still_needs_a_basis(self) -> None:
        path = self.audit / "work/findings.jsonl"
        rows = confirm.jsonl(path)
        rows[3]["disclosure_flag"] = True
        fid = rows[3]["id"]
        confirm.write_jsonl(path, rows)
        decided = confirm.decisions(approve_f13=False, extra={fid: {
            "status": "rejected", "coordinator_approved": False,
            "ledger_disposition": {"status": "EXCLUDED", "target": None, "reason": "not needed"}}})
        for key in ("report_section", "verification_snippet"):
            decided[fid].pop(key, None)
        result = self.apply(decided, check=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("without a disclosure_exclusion_basis", result.stderr)


class Check3cRecordsAndMarker(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = values_tests.DepositFixture("run_check")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.audit, self.release = self.fixture.audit, self.fixture.release
        # The audit's own records (hash-bound, never edited) and their staged copies.
        self.records = {
            "report/02_data_integrity.md": f"# Data integrity\n\nThe roster row reads {NAME}.\n",
            "critique/round_1/critic_stats.md": f"Intro.\nItem S3: the row {NAME} is quoted.\nEnd.\n",
            "work/a2/check.ipynb": json.dumps({"cells": [{"source": [f"# {NAME}\n", "x = 1\n"]}]}, indent=1) + "\n",
        }
        for relative, text in self.records.items():
            write(self.audit / relative, text)
            write(self.release / relative, text)
        self.originals = {relative: sha(self.audit / relative) for relative in self.records}

    def run_check(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(CHECK), str(self.audit), str(self.release), *extra],
                              text=True, capture_output=True, timeout=300,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_staging_masks_the_staged_copies_of_records_and_lists_them(self) -> None:
        result = self.run_check("--mask-records")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(NAME.casefold(), (result.stdout + result.stderr).casefold())
        self.assertIn("masked: 3 location(s)", result.stdout)
        note = (self.release / "RELEASE_VALUE_REDACTION.txt").read_text(encoding="utf-8")
        for location in ("report/02_data_integrity.md line 3  (a name)",
                         "critique/round_1/critic_stats.md line 2  (a name)",
                         "work/a2/check.ipynb line 5  (a name)"):
            self.assertIn(location, note)
        self.assertNotIn(NAME.split()[0], note)
        for relative in self.records:
            staged = (self.release / relative).read_text(encoding="utf-8")
            self.assertNotIn(NAME.split()[0], staged)
            self.assertIn("[masked at release: a name]", staged)
            self.assertEqual(staged.count("\n"), self.records[relative].count("\n"))
            self.assertEqual(sha(self.audit / relative), self.originals[relative], "the audit's copy is untouched")
        self.assertEqual(json.loads((self.release / "work/a2/check.ipynb").read_text())["cells"][0]["source"][1],
                         "x = 1\n")
        # a second check of the masked release is clean without masking anything
        again = self.run_check()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

    def test_the_comment_outputs_still_refuse_with_the_reword_remedy(self) -> None:
        write(self.release / "report/REPORT_final.md", f"# Comment\n\nThe row reads {NAME}.\n")
        result = self.run_check("--mask-records")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("report/REPORT_final.md line 3", result.stdout)
        self.assertIn("Remedy: reword the finding", result.stdout)
        self.assertNotIn("report/02_data_integrity.md line", result.stdout.split("FAIL:", 1)[1])
        self.assertNotIn(NAME.split()[0], (self.release / "critique/round_1/critic_stats.md").read_text())

    def test_without_masking_a_record_names_the_remedy_that_applies_to_it(self) -> None:
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("stage_release.sh masks the staged copy of a record nobody can reword", result.stdout)
        self.assertNotIn("Remedy: reword", result.stdout)

    def test_the_not_an_email_marker_exempts_its_own_code_line(self) -> None:
        for relative in self.records:
            (self.release / relative).unlink()
        write(self.release / "work/extra/matmul.py", "import numpy\n" + MARKED_CODE)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # the marker exempts only a code file's own line, never another line or a prose file
        write(self.release / "work/extra/matmul.py", "# i4r-privacy: not-an-email\n" + MARKED_CODE.split("  #")[0] + "\n")
        self.assertEqual(self.run_check().returncode, 1)
        (self.release / "work/extra/matmul.py").unlink()
        write(self.release / "report/notes.md", MARKED_CODE)
        self.assertEqual(self.run_check().returncode, 1)


class StagingMasksRecords(unittest.TestCase):
    """The real stage_release.sh on the release fixture, with a personal-data deposit whose value a Phase-1
    report and an auditor work script quote (records nobody can reword)."""

    def test_a_record_quoting_a_deposit_value_stages_masked(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="i4r-269-stage-")
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        audit = values_tests.make_audit(root, before_registry=values_tests.plant_deposit(leak=False))
        phase1 = audit / "report/02_data_integrity.md"
        phase1.write_text(phase1.read_text(encoding="utf-8")
                          + f"\nThe roster row reads {values_tests.FIXTURE_NAME}.\n", encoding="utf-8")
        script = write(audit / "work/a2_data_integrity/check_roster.py",
                       f"# the row for {values_tests.FIXTURE_NAME}\n" + MARKED_CODE)
        before = {path: sha(path) for path in (phase1, script)}
        release = root / "stage"
        environment = {**os.environ, "LC_ALL": "C", "I4R_RESERVE_GRACE": "0", **values_tests.FIXTURE_ENV}
        staged = subprocess.run(["/bin/bash", str(values_tests.STAGE), str(audit), str(release)],
                                text=True, capture_output=True, timeout=600, env=environment)
        self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
        self.assertIn("release staged clean", staged.stdout)
        self.assertIn("masked: 2 location(s)", staged.stdout)
        self.assertNotIn(values_tests.FIXTURE_NAME, staged.stdout + staged.stderr)
        note = (release / "RELEASE_VALUE_REDACTION.txt").read_text(encoding="utf-8")
        self.assertIn("report/02_data_integrity.md line", note)
        self.assertIn("work/a2_data_integrity/check_roster.py line 1  (a name)", note)
        for relative in ("report/02_data_integrity.md", "work/a2_data_integrity/check_roster.py"):
            self.assertNotIn(values_tests.FIXTURE_NAME, (release / relative).read_text(encoding="utf-8"))
        self.assertIn("i4r-privacy: not-an-email", (release / "work/a2_data_integrity/check_roster.py").read_text())
        self.assertEqual({path: sha(path) for path in before}, before, "the audit's own files are never edited")


if __name__ == "__main__":
    unittest.main()
