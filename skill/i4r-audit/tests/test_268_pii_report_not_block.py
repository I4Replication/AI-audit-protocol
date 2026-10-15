"""Deposit personal data is reported in the comment, never a reason to stop or withhold (2.6.8).

Programme policy since 2026-09-27: a paper whose replication deposit holds personally identifying
information is audited normally, and the personal data is reported in the comment as a
disclosure-flagged finding -- identifier kind, file and column, rows or participants affected,
whether anonymisation was claimed, and a verified remedy -- without any identifying value and
without a re-identification procedure. The output-sanitizing gates are unchanged.

These tests drive the shipped scripts: ``finalize_run.sh`` on copies of the shipped example pair,
``integrate_round.py apply`` on the 2.6.4 round-one fixture, and the gate functions a value-free
description must pass. A disclosure-flagged row leaves the comment only on a structured basis
(``disclosure_exclusion_basis``: refuted, duplicate_of:<id>, not_a_disclosure_defect,
tested_negative), never on free text, and every row must end final, MERGED into a final row, or
EXCLUDED with the basis label. Every refusal has a positive control, and every rule has a legacy
negative control: an audit initialised before 2.6.9 (the live 2.6.8 included, whose prompts still
said to pause) gets warnings, never a refusal, so an audit in flight is not stopped and a published
audit can still be re-finalized to re-file a corrected document.

No test here writes a participant-code-shaped literal. The two negative controls that need one to
trip a detector assemble it at run time from harmless parts, as run_phase1.HOME_SEGMENT does for
a home path.
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SKILL / "config"))

# Import the MODULE, not its classes, so the loader does not collect its tests here again.
from tests import test_264_verifier_confirmation as confirm  # noqa: E402

ROW_FIXTURE = json.loads((SKILL / "schemas/fixtures/v2_finding_row.json").read_text(encoding="utf-8"))

WITHHOLDING_REASONS = (
    "Withheld from the comment for privacy reasons.",
    "Deferred to I4R's notification process.",
    "Excluded on privacy grounds: naming the file would point readers to the data.",
    "Too sensitive to publish.",
    "Kept out of the comment pending author notification.",
    "Handled in the disclosure email, not in the comment.",
    "Not reported in the comment to protect participant identities.",
    "Reported privately to I4R instead.",
    # The review's natural phrasings, which a basis alone would have let through silently.
    "Removed from the comment: naming the file could help re-identify participants.",
    "Excluded: GDPR concerns.",
)
NON_PRIVACY_REASONS = (
    "Refuted: the Name column holds team names, not personal data.",
    "Tested negative: no identifier column in any deposited file.",
    "Duplicate of the included row on the session file.",
    "Superseded wording retained as a tested-negative ledger row.",
    "Refuted: the deposit's current version was already redacted by the authors.",
    "Not reproducible: the privacy auditor's scan read a cached copy.",
    "Excluded from the report: refuted by the data critic.",
)


def policy():
    """Imported per test, so each test fails on its own on a tree without the module."""
    return importlib.import_module("disclosure_policy")


def write(path: pathlib.Path, content: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def stamp_protocol(audit: pathlib.Path, version: str | None) -> None:
    """Set (or, with None, remove) run.json protocol_version: the version the audit was initialised at."""
    path = audit / "run.json"
    run = json.loads(path.read_text(encoding="utf-8"))
    if version is None:
        run.pop("protocol_version", None)
    else:
        run["protocol_version"] = version
    path.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")


class DisclosurePolicyRules(unittest.TestCase):
    def test_withholding_language_is_recognised_and_privacy_words_alone_are_not(self) -> None:
        rules = policy()
        for reason in WITHHOLDING_REASONS:
            with self.subTest(reason=reason):
                self.assertIsNotNone(rules.withholding_reason(reason))
        # Negative controls: a refuted or duplicated disclosure row may say so in privacy words.
        for reason in NON_PRIVACY_REASONS:
            with self.subTest(reason=reason):
                self.assertIsNone(rules.withholding_reason(reason))
        self.assertIsNone(rules.withholding_reason(None))

    def test_enforcement_starts_at_268_and_only_on_a_parsable_version(self) -> None:
        # 2.6.9 (decided after review): enforcement starts at 2.6.9, not 2.6.8. The live 2.6.8 shipped
        # without the rule and told the operator to pause, and an audit published on it (pii_present
        # true, no comment-level flags or statement) must still re-finalize.
        rules = policy()
        self.assertEqual(rules.ENFORCED_FROM, (2, 6, 9))
        for version in ("2.6.9", "2.6.10", "2.7.0", "3.0.0", "v2.6.9"):
            with self.subTest(version=version):
                self.assertTrue(rules.enforced(version))
        # Published audits: initialised at 2.6.8 or earlier.
        for version in ("2.6.8", "v2.6.8", "2.6.7", "2.6.4", "2.0.0", "2026.05-r3", "unversioned-skill-abcdef12",
                        "unknown", "p", "", None):
            with self.subTest(version=version):
                self.assertFalse(rules.enforced(version))


    def test_only_an_allowlisted_basis_is_parsed_and_the_label_is_read_back(self) -> None:
        rules = policy()
        for value, parsed in (("refuted", ("refuted", None)), ("tested_negative", ("tested_negative", None)),
                              ("not_a_disclosure_defect", ("not_a_disclosure_defect", None)),
                              ("duplicate_of:F7", ("duplicate_of", "F7")),
                              ("duplicate_of:OPR-N1", ("duplicate_of", "OPR-N1"))):
            with self.subTest(value=value):
                self.assertEqual(rules.parse_basis(value), parsed)
                # The label integrate writes is what finalize reads back.
                labelled = rules.labelled_reason("the column holds session dates", parsed)
                self.assertEqual(rules.reason_basis(labelled), parsed)
                self.assertEqual(rules.labelled_reason(labelled, parsed), labelled)
        for value in ("privacy", "withheld", "Refuted", "refuted ", "duplicate_of:", "duplicate_of:F7 F8",
                      "sensitive", "", None, 1, ["refuted"]):
            with self.subTest(value=value):
                self.assertIsNone(rules.parse_basis(value))
        for reason in ("Withheld: refuted later.", "Removed from the comment.", "Excluded: GDPR concerns.",
                       "Refutedly private.", None, ""):
            with self.subTest(reason=reason):
                self.assertIsNone(rules.reason_basis(reason))
        self.assertEqual(rules.reason_basis("tested-negative; no column"), ("tested_negative", None))
        # An empty reason is left for the schema to refuse, never invented.
        self.assertEqual(rules.labelled_reason("", ("refuted", None)), "")


class FinalizeReportsDepositPersonalData(unittest.TestCase):
    """finalize_run.sh on copies of the shipped example pair, at 2.6.9 and before it."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-268-finalize.")
        self.root = pathlib.Path(self.temporary.name)
        self.environment = os.environ.copy()
        self.environment.update({"LC_ALL": "C"})
        self.count = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def finalize(self, *, protocol_version: str, mutate=None, registry: list[dict] | None = None,
                 mutate_run=None) -> tuple[int, str]:
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
        # The verification bundle finalize's own self-test builds for the shipped pair.
        report = [f"## {index}. {finding.get('claim') or 'Synthetic'} [claim {index}]\n\nText.\n"
                  for index, finding in enumerate(comment["findings"], 1)]
        write(audit / "report/REPORT_final.md", "\n".join(report) or "# Report with no findings\n")
        write(audit / "verification.Rmd", "---\ntitle: fixture\noutput: html_document\n---\n\nok\n")
        rows = "".join(f"C-{index},fixture,PASS\n" for index in range(1, len(comment["findings"]) + 1))
        write(audit / "verification_summary.fixture.csv", "claim,key,verdict\n" + rows)
        bin_dir = audit / ".verification-test-bin"
        write(bin_dir / "Rscript", "#!/bin/sh\ncp verification_summary.fixture.csv "
                                   "verification_summary.csv\nprintf '0\\n' > verification_status.txt\n"
                                   "exit 0\n")
        os.chmod(bin_dir / "Rscript", 0o755)
        environment = dict(self.environment, PATH=f"{bin_dir}:{self.environment.get('PATH', '')}")
        verification = subprocess.run(["/bin/bash", str(SCRIPTS / "run_verification.sh"), str(audit)],
                                      text=True, capture_output=True, timeout=120, env=environment)
        self.assertEqual(verification.returncode, 0, verification.stdout + verification.stderr)
        (audit / "verification_summary.fixture.csv").unlink()
        shutil.rmtree(bin_dir)
        result = subprocess.run(["/bin/bash", str(SCRIPTS / "finalize_run.sh"), str(audit), "--no-registry"],
                                text=True, capture_output=True, timeout=180, env=self.environment)
        return result.returncode, result.stdout + result.stderr

    @staticmethod
    def unflag(comment: dict) -> None:
        for finding in comment["findings"]:
            finding["disclosure_flag"] = False

    def assert_refused(self, rc: int, output: str, *fragments: str) -> None:
        self.assertNotEqual(rc, 0, output)
        self.assertIn("FINALIZE FAILED", output)
        for fragment in fragments:
            self.assertIn(fragment, output)
        self.assertNotIn("[warning only", output)

    def assert_warned(self, rc: int, output: str, *fragments: str) -> None:
        self.assertEqual(rc, 0, output)
        self.assertIn("[warning only: this audit was initialised before skill 2.6.9]", output)
        for fragment in fragments:
            self.assertIn(fragment, output)

    def test_the_shipped_example_names_file_and_column_and_finalizes_at_268(self) -> None:
        example = json.loads((SKILL / "schemas/EXAMPLE_comment.json").read_text(encoding="utf-8"))
        text = json.dumps(example).lower()
        self.assertNotIn("disclosure email", text)
        self.assertNotIn("/governance/pii_description", example["publication_redaction"]["fields_requiring_removal"])
        flagged = [finding for finding in example["findings"] if finding["disclosure_flag"]]
        self.assertTrue(flagged)
        for finding in flagged:
            self.assertTrue(finding["package_locus"], "a disclosure finding names its file and column")
            self.assertIn(finding["package_locus"][0].split(" ")[0], finding["claim"])
        self.assertIsNone(example["governance"]["disclosure_notified"])
        rc, output = self.finalize(protocol_version="2.6.9")
        self.assertEqual(rc, 0, output)
        self.assertNotIn("does not report it", output)
        self.assertNotIn("[warning only", output)

    def test_pii_present_without_a_reported_finding_is_refused_at_268_and_warned_before(self) -> None:
        rc, output = self.finalize(protocol_version="2.6.9", mutate=self.unflag)
        self.assert_refused(rc, output, "governance.pii_present is true but the comment does not report it",
                            "a comment finding with disclosure_flag true",
                            "is non-empty but no comment finding has disclosure_flag true")
        for version in ("2.6.8", "2.6.7"):
            rc, output = self.finalize(protocol_version=version, mutate=self.unflag)
            self.assert_warned(rc, output, "governance.pii_present is true but the comment does not report it")

    def test_flags_and_statement_are_required_beside_the_flagged_finding(self) -> None:
        def no_flags(comment: dict) -> None:
            comment["disclosure"].update({"flags": [], "urgency": "none", "statement": " "})

        rc, output = self.finalize(protocol_version="2.6.10", mutate=no_flags)
        self.assert_refused(rc, output, "non-empty disclosure.flags", "a non-empty disclosure.statement")
        self.assertNotIn("a comment finding with disclosure_flag true", output)
        rc, output = self.finalize(protocol_version="2.6.4", mutate=no_flags)
        self.assert_warned(rc, output, "non-empty disclosure.flags")
        # Audit A27's shape (initialised and published on the live 2.6.8):
        # pii_present true, flagged comment findings, but empty flags, urgency none and an empty
        # statement. It re-finalizes with warnings.
        rc, output = self.finalize(protocol_version="2.6.8", mutate=no_flags)
        self.assert_warned(rc, output, "non-empty disclosure.flags", "a non-empty disclosure.statement")

    def test_flags_need_a_flagged_finding_even_without_pii_present(self) -> None:
        def flags_only(comment: dict) -> None:
            self.unflag(comment)
            comment["governance"]["pii_present"] = False

        rc, output = self.finalize(protocol_version="2.6.9", mutate=flags_only)
        self.assert_refused(rc, output, "is non-empty but no comment finding has disclosure_flag true")
        self.assertNotIn("governance.pii_present is true", output)
        rc, output = self.finalize(protocol_version="2.6.6", mutate=flags_only)
        self.assert_warned(rc, output, "is non-empty but no comment finding has disclosure_flag true")
        # Positive control: no personal data, no flags, nothing flagged -- nothing to report.
        def clean(comment: dict) -> None:
            flags_only(comment)
            comment["disclosure"].update({"flags": [], "urgency": "none"})

        rc, output = self.finalize(protocol_version="2.6.9", mutate=clean)
        self.assertEqual(rc, 0, output)
        self.assertNotIn("does not report it", output)

    def registry_row(self, **disposition) -> dict:
        row = copy.deepcopy(ROW_FIXTURE)
        row.update({"disclosure_flag": True, "status": "rejected", "coordinator_approved": False,
                    "settled": None, "verifications": [],
                    "ledger_disposition": {"status": "EXCLUDED", "target": None, "reason": None}})
        row["ledger_disposition"].update(disposition)
        return row

    def test_a_disclosure_row_excluded_without_a_basis_is_refused_and_a_refuted_one_is_not(self) -> None:
        withheld = self.registry_row(reason=WITHHOLDING_REASONS[0])
        rc, output = self.finalize(protocol_version="2.6.9", registry=[withheld])
        self.assert_refused(rc, output, "PII findings are reported, not withheld", "'withheld'",
                            "EXCLUDED without an allowlisted basis")
        rc, output = self.finalize(protocol_version="2.6.7", registry=[withheld])
        self.assert_warned(rc, output, "PII findings are reported, not withheld")
        # Withholding wording the old phrase list missed is refused as well: no basis label.
        missed = self.registry_row(reason="Removed from the comment: naming the file could help re-identify participants.")
        rc, output = self.finalize(protocol_version="2.6.9", registry=[missed])
        self.assert_refused(rc, output, "EXCLUDED without an allowlisted basis")
        # Positive controls: a refuted disclosure row (even in privacy words, which only warn), an
        # unflagged exclusion, an included row.
        refuted = self.registry_row(reason=NON_PRIVACY_REASONS[0])
        screened = dict(self.registry_row(
            reason="Refuted: the file was screened for privacy issues and holds only aggregated data."), id="F2")
        unflagged = dict(self.registry_row(reason=WITHHOLDING_REASONS[0]), disclosure_flag=False, id="F3")
        included = dict(copy.deepcopy(ROW_FIXTURE), disclosure_flag=True, id="F4")
        rc, output = self.finalize(protocol_version="2.6.9", registry=[refuted, screened, unflagged, included])
        self.assertEqual(rc, 0, output)
        self.assertNotIn("not withheld", output)
        self.assertIn("registry row F2 (work/findings.jsonl line 2): the EXCLUDED reason reads like withholding "
                      "language ('for privacy'); accepted on its stated basis.", output)

    def test_every_flagged_registry_row_must_reach_the_comment(self) -> None:
        final = dict(copy.deepcopy(ROW_FIXTURE), id="F1")
        draft = dict(copy.deepcopy(ROW_FIXTURE), id="F2", disclosure_flag=True, status="draft",
                     coordinator_approved=False)
        unapproved = dict(copy.deepcopy(ROW_FIXTURE), id="F3", disclosure_flag=True, coordinator_approved=False)
        rejected = dict(copy.deepcopy(ROW_FIXTURE), id="F4", disclosure_flag=True, status="rejected",
                        coordinator_approved=False)
        merged_away = dict(self.registry_row(status="MERGED", target="F2"), id="F5")
        duplicate_away = dict(self.registry_row(reason="Duplicate of F4: the same file."), id="F6")
        for row in (draft, unapproved, rejected):
            with self.subTest(row=row["id"]):
                rc, output = self.finalize(protocol_version="2.6.9", registry=[final, row])
                self.assert_refused(rc, output, f"registry row {row['id']} (work/findings.jsonl line 2) is "
                                    "disclosure-flagged but not final")
        rc, output = self.finalize(protocol_version="2.6.9", registry=[final, draft, merged_away])
        self.assert_refused(rc, output, "registry row F5 (work/findings.jsonl line 3) is disclosure-flagged "
                            "and MERGED into 'F2', which is not a final row")
        rc, output = self.finalize(protocol_version="2.6.9", registry=[final, rejected, duplicate_away])
        self.assert_refused(rc, output, "is EXCLUDED as a duplicate of F4, which is not another final row")
        # Legacy: the same registry only warns, so a published audit still re-finalizes.
        rc, output = self.finalize(protocol_version="2.6.4", registry=[final, draft, merged_away, duplicate_away])
        self.assert_warned(rc, output, "disclosure-flagged but not final", "which is not a final row")
        # Positive controls: MERGED into a final row, a duplicate of a final row, a refuted row, a
        # tested negative, and one no longer a disclosure defect.
        rows = [dict(final, disclosure_flag=True),
                dict(self.registry_row(status="MERGED", target="F1"), id="F2"),
                dict(self.registry_row(reason="Duplicate of F1: the same session file."), id="F3"),
                dict(self.registry_row(reason="Tested negative: no identifier column in any file."), id="F4"),
                dict(self.registry_row(reason="Not a disclosure defect: the column holds team names."), id="F5")]
        rc, output = self.finalize(protocol_version="2.6.9", registry=rows)
        self.assertEqual(rc, 0, output)
        self.assertNotIn("registry row", output)

    def test_a_deferred_disclosure_row_at_an_unfinished_stop_is_reported_not_blocked(self) -> None:
        # 2.6.9 (merging 2.6.8's deferred approvals): a flagged row still INCLUDED and awaiting its
        # approval when the review stops unfinished can no longer be approved -- no round is left to
        # confirm it, and the INCLUDED set may not change after the last round. The shipped example
        # stops at cap_with_unresolved. Report, never block: listed as unresolved, with the exposure in
        # disclosure.flags and disclosure.statement, it only warns.
        self.assertEqual(json.loads((SKILL / "schemas/EXAMPLE_run.json").read_text())["stopping_reason"],
                         "cap_with_unresolved")
        final = dict(copy.deepcopy(ROW_FIXTURE), id="F1")
        pending = dict(copy.deepcopy(ROW_FIXTURE), id="F2", disclosure_flag=True, coordinator_approved=False)

        def listed(run: dict) -> None:
            run["unresolved_conclusion_critical_item_ids"] = ["F2"]

        rc, output = self.finalize(protocol_version="2.6.9", registry=[final, pending], mutate_run=listed)
        self.assertEqual(rc, 0, output)
        self.assertIn("registry row F2 (work/findings.jsonl line 2) is disclosure-flagged and still awaiting "
                      "approval at cap_with_unresolved; it is listed as unresolved", output)
        self.assertNotIn("is disclosure-flagged but not final", output)
        # The only disclosure finding pending: the registry row stands in for the comment finding.
        rc, output = self.finalize(protocol_version="2.6.9", registry=[final, pending], mutate_run=listed,
                                   mutate=self.unflag)
        self.assertEqual(rc, 0, output)
        self.assertIn("no comment finding is disclosure-flagged; the flagged registry row(s) ['F2']", output)
        self.assertNotIn("does not report it", output)
        # Not listed as unresolved: refused, and the refusal says what the stop needs.
        rc, output = self.finalize(protocol_version="2.6.9", registry=[final, pending])
        self.assert_refused(rc, output, "registry row F2 (work/findings.jsonl line 2) is disclosure-flagged but not final",
                            "accepted once the stop lists it as unresolved (review_scope",
                            "by its finding id (F2) or by the id of a critic item that assessed it")
        # Listed, but the comment does not report the exposure: refused.
        rc, output = self.finalize(protocol_version="2.6.9", registry=[final, pending], mutate_run=listed,
                                   mutate=lambda comment: comment["disclosure"].update(
                                       {"flags": [], "urgency": "none", "statement": " "}))
        self.assert_refused(rc, output, "is disclosure-flagged but not final")
        # Only an unfinished stop: at target_complete the row must be approved first (2.6.8's rule).
        def complete(run: dict) -> None:
            listed(run)
            run["stopping_reason"] = "target_complete"
            run.pop("stopping_reason_detail", None)

        rc, output = self.finalize(protocol_version="2.6.9", registry=[final, pending], mutate_run=complete)
        self.assert_refused(rc, output, "is disclosure-flagged but not final")
        self.assertNotIn("still awaiting approval", output)
        self.assertNotIn("accepted once the stop lists it", output)
        # A withdrawn or excluded row is never excused by the stop.
        excluded = self.registry_row(reason=WITHHOLDING_REASONS[0])
        excluded["id"] = "F2"
        rc, output = self.finalize(protocol_version="2.6.9", registry=[final, excluded], mutate_run=listed)
        self.assert_refused(rc, output, "EXCLUDED without an allowlisted basis")

    def test_immediate_urgency_is_reported_not_paused(self) -> None:
        rc, output = self.finalize(protocol_version="2.6.9",
                                   mutate=lambda comment: comment["disclosure"].update(urgency="immediate"))
        self.assertEqual(rc, 0, output)
        self.assertIn("disclosure.urgency is 'immediate'", output)
        self.assertIn("Confirm the comment reports it", output)
        self.assertIn("nothing is paused", output)
        self.assertNotIn("BEFORE the comment is drafted", output)


class IntegrateRefusesToWithholdDisclosureFindings(confirm.Fixture):
    """integrate_round.py apply on the 2.6.4 round-one fixture (thirteen draft rows), stamped as an
    audit initialised at 2.6.9 so that its refusals are errors."""

    protocol_version = "2.6.9"

    def setUp(self) -> None:
        super().setUp()
        self.stamp(self.protocol_version)

    def stamp(self, version: str | None) -> None:
        stamp_protocol(self.audit, version)

    def flag(self, index: int) -> str:
        path = self.audit / "work/findings.jsonl"
        rows = confirm.jsonl(path)
        rows[index]["disclosure_flag"] = True
        confirm.write_jsonl(path, rows)
        return rows[index]["id"]

    def row_id(self, index: int) -> str:
        return confirm.jsonl(self.audit / "work/findings.jsonl")[index]["id"]

    @staticmethod
    def withdrawn(decided: dict, *finding_ids: str) -> dict:
        # A row that does not end approved needs no report_section or verification_snippet.
        for finding_id in finding_ids:
            for key in ("report_section", "verification_snippet"):
                decided[finding_id].pop(key, None)
        return decided

    def assert_refused_before_writing(self, decided: dict, *fragments: str, contested: dict | None = None) -> None:
        registry = self.audit / "work/findings.jsonl"
        before = registry.read_bytes()
        adjudication = (self.audit / "report/ADJUDICATION.md").read_bytes()
        result = self.apply(decided, check=False, contested=contested)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for fragment in fragments:
            self.assertIn(fragment, result.stderr)
        self.assertNotIn("[warning only", result.stderr)
        self.assertEqual(registry.read_bytes(), before)
        self.assertEqual((self.audit / "report/ADJUDICATION.md").read_bytes(), adjudication)
        self.assertFalse((self.audit / "critique/round_1/INTEGRATION_PLAN.json").exists())
        self.assertFalse((self.audit / "work/findings.before_round1_integration.jsonl").exists())

    def test_excluding_a_disclosure_row_without_a_basis_is_refused(self) -> None:
        fid = self.flag(3)
        # Free text never decides: withholding wording the old phrase list missed is refused too.
        for reason in WITHHOLDING_REASONS[:3] + (
                "Removed from the comment: naming the file could help re-identify participants.",
                "Excluded: GDPR concerns.", "Refuted: the file was checked."):
            with self.subTest(reason=reason):
                decided = self.withdrawn(confirm.decisions(extra={fid: {
                    "status": "rejected", "coordinator_approved": False,
                    "ledger_disposition": {"status": "EXCLUDED", "target": None, "reason": reason}}}), fid)
                self.assert_refused_before_writing(decided, f"{fid} is disclosure-flagged",
                                                   "without a disclosure_exclusion_basis")
        # Positive control on the same fixture: the ordinary decisions, which keep the flagged row
        # INCLUDED, integrate, and an integration that clears no flag writes no new block.
        result = self.apply(confirm.decisions(approve_f13=False), check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # 2.6.8 QA: apply notes that it wrote the contests to critique/round_1/CONTESTED.json
        self.assertEqual([line for line in result.stderr.splitlines() if not line.startswith("note: ")], [])
        self.assertTrue(self.registry()[fid]["disclosure_flag"])
        self.assertEqual(self.registry()[fid]["ledger_disposition"]["status"], "INCLUDED")
        self.assertNotIn("Disclosure flags cleared", (self.audit / "report/ADJUDICATION.md").read_text())

    def test_rejecting_or_unapproving_an_included_disclosure_row_without_a_basis_is_refused(self) -> None:
        # The ledger stays INCLUDED, but the row is no longer final, so it silently left the comment.
        fid = self.flag(3)
        for change in ({"status": "rejected", "coordinator_approved": False,
                        "applied_note": "withheld from the comment for privacy reasons"},
                       {"status": "superseded"}):
            with self.subTest(change=change):
                decided = self.withdrawn(confirm.decisions(extra={fid: dict(change)}), fid)
                self.assert_refused_before_writing(decided, f"{fid} is disclosure-flagged",
                                                   "without a disclosure_exclusion_basis")
        # Withdrawing an approval the row already has, while it stays INCLUDED and flagged, is not a
        # withdrawal from the comment (2.6.9): tested in tests/test_269_release_fixes.py through apply.
        path = self.audit / "work/findings.jsonl"
        rows = confirm.jsonl(path)
        rows[3].update(confirm.decisions()[fid])
        confirm.write_jsonl(path, rows)
        # A basis does not make an INCLUDED row that leaves the comment acceptable: it must be EXCLUDED.
        decided = self.withdrawn(confirm.decisions(extra={fid: {
            "status": "rejected", "coordinator_approved": False, "disclosure_exclusion_basis": "refuted"}}), fid)
        self.assert_refused_before_writing(decided, "but leaves it INCLUDED", "set its ledger_disposition")
        # Positive control, the shape a real 2.6.8 round-1 decision had: a flagged draft row kept
        # unapproved while its claim is revised is still under review, not withdrawn.
        pending = self.flag(5)
        decided = self.withdrawn(confirm.decisions(approve_f13=False, extra={pending: {
            "coordinator_approved": False, "claim": f"{pending}: the revised claim, still under review.",
            "applied_note": "claim narrowed; approval deferred to the next round"}}), pending)
        decided[pending].pop("status")
        result = self.apply(decided, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # 2.6.8 QA: apply notes that it wrote the contests to critique/round_1/CONTESTED.json
        self.assertEqual([line for line in result.stderr.splitlines() if not line.startswith("note: ")], [])
        self.assertTrue(self.registry()[pending]["disclosure_flag"])
        self.assertEqual(self.registry()[pending]["status"], "draft")

    def test_a_basis_outside_the_allowlist_is_refused(self) -> None:
        fid = self.flag(3)
        for basis in ("privacy", "withheld", "Refuted", "duplicate_of:", "duplicate_of:F4 F5", ""):
            with self.subTest(basis=basis):
                decided = self.withdrawn(confirm.decisions(extra={fid: {
                    "status": "rejected", "coordinator_approved": False, "disclosure_exclusion_basis": basis,
                    "ledger_disposition": {"status": "EXCLUDED", "target": None, "reason": "Refuted: checked."}}}), fid)
                self.assert_refused_before_writing(decided, "is not an allowlisted basis")

    def test_clearing_the_flag_needs_a_basis_and_a_reason(self) -> None:
        fid = self.flag(3)
        self.assert_refused_before_writing(
            confirm.decisions(extra={fid: {"disclosure_flag": False}}),
            "clears its disclosure_flag without a disclosure_exclusion_basis",
            "clears disclosure_flag but states no disclosure_flag_reason")
        self.assert_refused_before_writing(
            confirm.decisions(extra={fid: {"disclosure_flag": False, "disclosure_flag_reason": NON_PRIVACY_REASONS[0]}}),
            "clears its disclosure_flag without a disclosure_exclusion_basis")
        self.assert_refused_before_writing(
            confirm.decisions(extra={fid: {"disclosure_flag": False, "disclosure_exclusion_basis": "refuted"}}),
            "clears disclosure_flag but states no disclosure_flag_reason")

    def test_merging_into_a_row_that_leaves_the_comment_is_refused(self) -> None:
        fid = self.flag(3)
        target = self.row_id(4)
        merge = {"status": "superseded", "coordinator_approved": False,
                 "ledger_disposition": {"status": "MERGED", "target": target, "reason": None}}
        excluded = self.withdrawn(confirm.decisions(extra={
            fid: dict(merge),
            target: {"status": "rejected", "coordinator_approved": False,
                     "ledger_disposition": {"status": "EXCLUDED", "target": None,
                                            "reason": NON_PRIVACY_REASONS[2]}}}), fid, target)
        self.assert_refused_before_writing(excluded, f"MERGEs it into {target!r}, which does not end final")
        # A target that stays INCLUDED but is rejected (or never approved) leaves the comment too.
        rejected = self.withdrawn(confirm.decisions(extra={
            fid: dict(merge), target: {"status": "rejected", "coordinator_approved": False}}), fid, target)
        self.assert_refused_before_writing(rejected, f"MERGEs it into {target!r}, which does not end final")
        unapproved = self.withdrawn(confirm.decisions(approve_f13=False, extra={fid: dict(merge)}), fid)
        unapproved[fid]["ledger_disposition"] = dict(merge["ledger_disposition"], target="F13")
        self.assert_refused_before_writing(unapproved, "MERGEs it into 'F13', which does not end final")

    def test_a_duplicate_basis_must_name_a_row_that_ends_final(self) -> None:
        fid = self.flag(3)
        target = self.row_id(4)
        exclude = {"status": "rejected", "coordinator_approved": False, "disclosure_exclusion_basis": f"duplicate_of:{target}",
                   "ledger_disposition": {"status": "EXCLUDED", "target": None, "reason": "the same session file"}}
        decided = self.withdrawn(confirm.decisions(extra={
            fid: dict(exclude), target: {"status": "rejected", "coordinator_approved": False}}), fid, target)
        self.assert_refused_before_writing(decided, f"duplicate_of:{target} names a row that does not end final")
        decided = self.withdrawn(confirm.decisions(extra={fid: dict(exclude, disclosure_exclusion_basis="duplicate_of:F99")}), fid)
        self.assert_refused_before_writing(decided, "duplicate_of:F99 names no other row")
        # Positive control: the duplicated row ends final, so the reason is written with its label.
        result = self.apply(self.withdrawn(confirm.decisions(approve_f13=False, extra={fid: dict(exclude)}), fid),
                            check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.registry()[fid]["ledger_disposition"]["reason"],
                         f"Duplicate of {target}: the same session file")

    def test_allowed_decisions_integrate_and_record_their_basis(self) -> None:
        fid = self.flag(3)
        merged = self.flag(5)
        cleared = self.flag(6)
        refuted = self.flag(8)
        target = self.row_id(7)
        reason = "Refuted on recount: the column holds team names, so this is not a disclosure defect."
        # The operator row (index 12) is left unapproved: it has no verifier record in this fixture.
        decided = self.withdrawn(confirm.decisions(approve_f13=False, extra={
            fid: {"status": "rejected", "coordinator_approved": False, "disclosure_exclusion_basis": "refuted",
                  "ledger_disposition": {"status": "EXCLUDED", "target": None, "reason": NON_PRIVACY_REASONS[0]}},
            merged: {"status": "superseded", "coordinator_approved": False,
                     "ledger_disposition": {"status": "MERGED", "target": target, "reason": None}},
            cleared: {"disclosure_flag": False, "disclosure_exclusion_basis": "not_a_disclosure_defect",
                      "disclosure_flag_reason": reason},
            # A refuted row may be described in privacy words: the basis decides, the words only warn.
            refuted: {"status": "rejected", "coordinator_approved": False, "disclosure_exclusion_basis": "refuted",
                      "ledger_disposition": {"status": "EXCLUDED", "target": None, "reason":
                                             "the file was screened for privacy issues and holds only aggregated data."}},
        }), fid, merged, refuted)
        result = self.apply(decided, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"WARNING: {refuted}: the EXCLUDED reason reads like withholding language", result.stderr)
        registry = self.registry()
        self.assertEqual(registry[fid]["ledger_disposition"],
                         {"status": "EXCLUDED", "target": None, "reason": NON_PRIVACY_REASONS[0]})
        self.assertEqual(registry[refuted]["ledger_disposition"]["reason"],
                         "Refuted: the file was screened for privacy issues and holds only aggregated data.")
        self.assertEqual(registry[merged]["ledger_disposition"]["target"], target)
        self.assertFalse(registry[cleared]["disclosure_flag"])
        log = (self.audit / "report/ADJUDICATION.md").read_text(encoding="utf-8")
        self.assertIn("### Disclosure flags cleared", log)
        self.assertIn(f"- {cleared}: {reason} (basis: not_a_disclosure_defect)", log)

    def test_a_flag_cleared_on_a_reapplied_round_is_recorded_once(self) -> None:
        fid = self.flag(6)
        first = self.apply(confirm.decisions(approve_f13=False), check=False)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        reason = "Refuted on recount: team names, not personal data."
        again = confirm.decisions(approve_f13=False, extra={fid: {
            "disclosure_flag": False, "disclosure_exclusion_basis": "not_a_disclosure_defect",
            "disclosure_flag_reason": reason}})
        for attempt in range(2):
            result = self.apply(again, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(self.registry()[fid]["disclosure_flag"])
            log = (self.audit / "report/ADJUDICATION.md").read_text(encoding="utf-8")
            # 2.6.8 QA: a re-apply supersedes the round's block (the old one is relabelled WITHDRAWN)
            # and appends a new one, so the live block records the cleared flag exactly once.
            self.assertEqual(log.count("<!-- adjudication:round-1 -->"), 1)
            self.assertEqual(log.count("<!-- withdrawn-adjudication:round-1 -->"), attempt + 1)
            live = log.split("<!-- adjudication:round-1 -->", 1)[1]
            self.assertEqual(live.count(f"- {fid}: {reason} (basis: not_a_disclosure_defect)"), 1, attempt)
            self.assertEqual(live.count("### Disclosure flags cleared"), 1, attempt)
            self.assertEqual(log.count(f"<!-- adjudication:round-1:disclosure-cleared:{fid} -->"), 1, attempt)

    def test_a_contested_entry_without_a_position_is_refused(self) -> None:
        # The guide's form, {"position": ..., "evidence": ...}, is recorded; any other shape used to
        # become an empty contested position without a word.
        plan = self.plan()
        self.assertEqual(plan.returncode, 0, plan.stderr)
        verdict = next(v for entry in self.plan_json()["findings"] for v in entry["verdicts"])
        key = f"{verdict['role']}:{verdict['item_id']}"
        (self.audit / "critique/round_1/INTEGRATION_PLAN.json").unlink()
        self.assert_refused_before_writing(confirm.decisions(approve_f13=False),
                                           "--contested entries must be {\"position\": \"...\", \"evidence\": \"...\"} with both non-empty",
                                           repr([key]),
                                           contested={key: {"reason": "The finding is value-free."}})
        position = "The finding is value-free and correct; removal is rejected."
        result = self.apply(confirm.decisions(approve_f13=False), check=False,
                            # 2.6.8 QA: the fixture's own contest of the data critic's REFUTED on F5 stays, or
                            # apply refuses that unanswered correction
                            contested={"critic_data:data-F5": {"position": "The deposit rounds to 5.",
                                                               "evidence": "work/check_primary.txt"},
                                       key: {"position": position, "evidence": "work/check_primary.txt"}})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        notes = (self.audit / "critique/round_1/RESPONSE_notes.md").read_text(encoding="utf-8")
        log = (self.audit / "report/ADJUDICATION.md").read_text(encoding="utf-8")
        self.assertIn(f"**contested**: {position}", notes)
        self.assertIn("Decision: CONTESTED (accepted in part).", log)


class IntegrateOnlyWarnsBefore268(confirm.Fixture):
    """The same decisions on an audit initialised before 2.6.9 (the live 2.6.8 included, or with no
    protocol_version) are applied with warnings: an in-flight audit is not refused mid-way by the upgrade."""

    def test_legacy_audits_get_warnings_and_the_decisions_apply(self) -> None:
        for number, version in enumerate(("2.6.8", "2.6.7", "2.6.2", None)):
            with self.subTest(version=version):
                self.audit = self.clone(pathlib.Path(self.temp.name) / f"legacy-{number}")
                stamp_protocol(self.audit, version)
                path = self.audit / "work/findings.jsonl"
                rows = confirm.jsonl(path)
                for index in (3, 6):
                    rows[index]["disclosure_flag"] = True
                confirm.write_jsonl(path, rows)
                rejected, cleared = rows[3]["id"], rows[6]["id"]
                decided = confirm.decisions(approve_f13=False, extra={
                    rejected: {"status": "rejected", "coordinator_approved": False},
                    # Flags cleared without any reason, as a 2.6.2 audit's round-2 decisions did.
                    cleared: {"disclosure_flag": False}})
                for key in ("report_section", "verification_snippet"):
                    decided[rejected].pop(key, None)
                result = self.apply(decided, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("[warning only: this audit was initialised before skill 2.6.9]", result.stderr)
                self.assertIn(f"WARNING: {rejected} is disclosure-flagged", result.stderr)
                self.assertIn(f"WARNING: {cleared}: the decision clears disclosure_flag but states no "
                              "disclosure_flag_reason", result.stderr)
                self.assertNotIn("FATAL", result.stderr)
                registry = self.registry()
                self.assertEqual(registry[rejected]["status"], "rejected")
                self.assertFalse(registry[cleared]["disclosure_flag"])


class ValueFreeDescriptionsPassTheOutputGates(unittest.TestCase):
    """What the new prompts and the shipped example ask for passes every unchanged output gate; a
    reproduced linkage still does not."""

    DESCRIPTIONS = (
        "The file data/intake.csv holds a Name column that identifies all 57 participants by name.",
        "The Birthdate column of data/intake.csv gives a full date of birth for 57 of 57 rows.",
        "The Email column of data/intake.csv is non-missing in 55 rows; no address is reproduced.",
        "The IPAddress and Latitude columns of data/survey_export.csv are present in all 212 rows.",
        "The directory data/raw/ holds 40 per-participant files whose names are participant codes.",
        "Remedy (verified): drop the four columns and replace the file through the repository.",
    )

    def setUp(self) -> None:
        import check_release_privacy
        import phase1_runtime
        import run_coordinator
        self.release = check_release_privacy
        self.phase1 = phase1_runtime
        self.coordinator = run_coordinator

    def assert_passes_every_gate(self, text: str) -> None:
        self.phase1.check_output_hygiene(text, {"findings": text.splitlines()})
        self.coordinator.validate_provider_visible_string(text, "fixture report")
        self.coordinator.validate_output_privacy(text, "fixture report")
        self.assertEqual(self.release.inspect_text(text, "fixture", identifiers=True), [])

    def test_value_free_descriptions_pass_and_a_reproduced_linkage_does_not(self) -> None:
        self.assert_passes_every_gate("\n".join(self.DESCRIPTIONS) + "\n")
        # The shipped example's disclosure record now names its file and column with counts; that
        # is the form every audit copies, so it must pass the same gates.
        example = json.loads((SKILL / "schemas/EXAMPLE_comment.json").read_text(encoding="utf-8"))
        flagged = [finding for finding in example["findings"] if finding["disclosure_flag"]]
        self.assertTrue(flagged)
        texts = [example["governance"]["pii_description"], example["disclosure"]["statement"]]
        for finding in flagged:
            self.assertTrue(finding["package_locus"], "a disclosure finding names its file and column")
            texts += [finding["claim"], finding["remedy_description"], *finding["package_locus"],
                      finding["evidence"]["artifact"], finding["evidence"]["observed"]]
        self.assert_passes_every_gate("\n".join(texts) + "\n")
        # Negative control: a participant code beside a date is still refused by the unchanged gates.
        code = "SUBJ" + "_P" + "999"          # assembled: no code-shaped literal in this file
        linkage = f"The row for {code} gives a session on 1900-01-01.\n"
        with self.assertRaises(self.coordinator.CoordinatorError):
            self.coordinator.validate_output_privacy(linkage, "fixture report")
        with self.assertRaises(self.phase1.OutputValidationError):
            self.phase1.check_output_hygiene(linkage, {})

    def test_a_date_beside_a_participant_identifier_is_refused_and_the_auditors_still_split_the_lines(self) -> None:
        # 2.6.8 refused this line too: the word "participants" counted as an identifier. Since 2.7.7 the rule needs a
        # real identifier, so a sentence about participants beside a date passes (test_277_unblocking).
        joined = "Sessions ran from 1900-01-01; the Name column identifies 57 participants.\n"
        self.phase1.check_output_hygiene(joined, {})
        linked = "Sessions ran from 1900-01-01; participant 57 missed one.\n"
        with self.assertRaises(self.phase1.OutputValidationError) as caught:
            self.phase1.check_output_hygiene(linked, {})
        self.assertEqual(caught.exception.rule, "participant_date")
        split = "Sessions ran from 1900-01-01.\nThe Name column identifies 57 participants.\n"
        self.phase1.check_output_hygiene(split, {})
        # participant_date is not repairable, so the auditors and their brief are told to split them (the frozen
        # prompt text is unchanged, and keeping dates off such lines stays the safe habit).
        import run_phase1
        for text in (run_phase1.AUDITOR_PII_REPORTING,
                     (SKILL / "reference/brief_template.md").read_text(encoding="utf-8")):
            self.assertIn("different line from any sentence that mentions participants", " ".join(text.split()))


class CoordinatorReferencesStateTheRule(unittest.TestCase):
    """The coordinator reads these references with no deposit exemption: the new text must state the
    report-not-withhold rule and still pass the coordinator's own provider-safety scan."""

    SECTIONS = {
        "reference/checklist.md": ("whether or not anonymisation was claimed", "never a value",
                                   "Personal data never stops, pauses or skips the audit"),
        "reference/report_structure.md": ("## Disclosure findings", "never withheld, deferred",
                                          "count or presence script that prints no value"),
        "reference/adjudication_guide.md": ("## Disclosure findings are reported, not withheld",
                                            "a value-free disclosure finding (identifier kind, file, column "
                                            "and counts) that is factually correct",
                                            "A disclosure finding the deposit does not support is REFUTED "
                                            "like any other claim",
                                            '"disclosure_exclusion_basis": "refuted"',
                                            "Only a reproduced identifying value, a new linkage"),
        "reference/SCHEME.md": ("Filing the comment with I4R is the escalation",
                                "always reported in the comment and never routed around it",
                                "never held back from the comment"),
    }
    OLD = ("before the comment is drafted", "Escalated to the coordinator before drafting",
           "routes on its own, outside the impact queue")

    def test_each_reference_states_the_rule_and_passes_the_coordinator_scan(self) -> None:
        import run_coordinator
        for relative, fragments in self.SECTIONS.items():
            with self.subTest(reference=relative):
                path = SKILL / relative
                text = " ".join(path.read_text(encoding="utf-8").split())
                for fragment in fragments:
                    self.assertIn(fragment, text)
                for fragment in self.OLD:
                    self.assertNotIn(fragment, text)
                run_coordinator.validate_provider_visible_text(path, f"coordinator reference {relative}")
        # Negative control: the scan does refuse a code-shaped token in a reference.
        with tempfile.TemporaryDirectory() as directory:
            planted = pathlib.Path(directory) / "planted.md"
            planted.write_text("A row for " + "SUBJ" + "_P" + "999" + " stays out.\n", encoding="utf-8")
            with self.assertRaises(run_coordinator.CoordinatorError):
                run_coordinator.validate_provider_visible_text(planted, "coordinator reference planted")


class CommentSchemaDescribesTheStatementValueFree(unittest.TestCase):
    def test_disclosure_statement_is_described_like_pii_description(self) -> None:
        schema = json.loads((SKILL / "schemas/comment.schema.json").read_text(encoding="utf-8"))

        def find(node, key):
            if isinstance(node, dict):
                if key in node and isinstance(node[key], dict) and "description" in node[key]:
                    yield node[key]["description"]
                for value in node.values():
                    yield from find(value, key)
            elif isinstance(node, list):
                for value in node:
                    yield from find(value, key)

        statement = " ".join(next(find(schema["properties"]["disclosure"], "statement")).split())
        pii = " ".join(next(find(schema, "pii_description")).split())
        self.assertNotIn("coarsest granularity", statement)
        for fragment in ("value-free", "the kind of identifier", "file and column", "file count",
                         "rows or participants affected"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, statement)
                self.assertIn(fragment, pii)
        self.assertIn("verified remedy", statement)
        self.assertIn("Never reproduce an identifier or any identifying value", statement)
        self.assertIn("re-identified", statement)


class PolicyDriftKeepsThePauseOut(unittest.TestCase):
    def test_a_reintroduced_pause_or_withholding_phrase_is_caught(self) -> None:
        import check_policy_drift
        errors: list[str] = []
        check_policy_drift.check_disclosure_language(errors)
        self.assertEqual(errors, [])
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for path in check_policy_drift.disclosure_language_files():
                relative = path.relative_to(SKILL)
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, root / relative)
            skill = root / "SKILL.md"
            skill.write_text(skill.read_text(encoding="utf-8").replace(
                "Pause only for a missing input\n  or a gate",
                "Pause only for a missing input,\n  a responsible-disclosure decision, or a gate"), encoding="utf-8")
            example = root / "schemas/EXAMPLE_comment.json"
            example.write_text(example.read_text(encoding="utf-8").replace(
                "pkg/data/wave1_sessions.csv", "pkg/data/ (named in the disclosure email)"), encoding="utf-8")
            errors = []
            check_policy_drift.check_disclosure_language(errors, root)
        self.assertEqual(len(errors), 2, errors)
        self.assertIn("SKILL.md reintroduces a disclosure pause", errors[0])
        self.assertIn("EXAMPLE_comment.json reintroduces withholding", errors[1])


if __name__ == "__main__":
    unittest.main()
