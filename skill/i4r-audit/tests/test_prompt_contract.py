"""The role prompt must name every rule the output validator enforces.

Nine separate Phase-1 failures on the first production audit came from a single defect
class: phase1_runtime validated a rule that the role prompt described only in prose or
not at all. Fake-runner tests cannot catch this because the fakes emit validator-perfect
output. This test pins the prompt to the validator's vocabulary instead.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import phase1_runtime  # noqa: E402
import run_coordinator  # noqa: E402
import run_phase1  # noqa: E402


def rendered_contract(inventory_path: str) -> str:
    """The shared contract exactly as a role receives it.

    The contract used to live in prompts/common.txt, which is rendered only into critic prompts.
    The targeted adjudicator is graded by the same validator and was shown none of it, which is
    how round 2 of the second production audit died. It now lives in prompts/contract.txt and is
    rendered into both, with {{INVENTORY}} bound to that role's snapshot layout.
    """
    text = (SKILL / "prompts/contract.txt").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.split("\n") if not line.startswith("#"))
    return " ".join(body.replace("{{INVENTORY}}", inventory_path).split())


ROLE_INVENTORIES = {
    "critic": "source/context/EVIDENCE_HASHES.json",
    "targeted_adjudicator": "source/EVIDENCE_HASHES.json",
}



class RolePromptNamesValidatorRules(unittest.TestCase):
    def prompt(self, role: str = "auditor_reproduction") -> str:
        # The prompt is hard-wrapped, so collapse whitespace before matching phrases.
        text = run_phase1.build_role_prompt(role, "audit-test", {"source_snapshot_sha256": "0" * 64})
        return " ".join(text.split())

    def test_scientific_fields_keys_are_spelled_out(self) -> None:
        text = self.prompt()
        for key in ("passed_check_ids", "open_question_ids", "findings"):
            self.assertIn(f"`{key}`", text, key)
        # The two natural-language misspellings four roles actually produced.
        self.assertIn("passed_checks", text)
        self.assertIn("open_questions", text)

    def test_finding_field_set_and_value_rules(self) -> None:
        text = self.prompt()
        for key in ("id", "status", "claim", "evidence", "impact", "remedy",
                    "conclusion_changing", "disclosure_critical"):
            self.assertIn(key, text)
        self.assertIn("CONFIRMED, LIKELY or SPECULATIVE", text)
        self.assertIn("JSON booleans", text)
        self.assertIn("source_path", text)
        self.assertIn("source_sha256", text)

    def test_source_manifest_versus_assigned_evidence(self) -> None:
        text = self.prompt()
        self.assertIn("ROLE_PACKET.source_manifest", text)
        self.assertIn("ROLE_PACKET.assigned_evidence", text)
        for index_file in ("pdf_text.txt", "file_inventory.json", "workbook_summaries.json"):
            self.assertIn(index_file, text)

    def test_evidence_manifest_path_base_is_stated(self) -> None:
        text = self.prompt()
        self.assertIn("must NOT begin with `evidence/`", text)
        self.assertIn("scripts/check.R", text)

    def test_closed_output_contract_is_stated(self) -> None:
        text = self.prompt()
        self.assertIn("CLOSED contract", text)
        for name in ("REPORT.md", "result.json", "evidence/manifest.json"):
            self.assertIn(name, text)
        self.assertIn("deleted before you finish", text)

    def test_report_rules_match_validator(self) -> None:
        text = self.prompt()
        for heading in ("Result summary", "Brief corrections", "Checks performed",
                        "Findings", "Tested hypotheses not confirmed", "Open questions",
                        "Evidence inventory"):
            self.assertIn(heading, text, heading)
        self.assertIn("i4r-role-complete:auditor_reproduction", text)
        self.assertIn("absolute filesystem path", text)
        # The home segment is assembled so build_package.py's path scan does not flag this file.
        for fragment in ("/" + "Users/", "/home/", "/private/tmp/", "/private/var/"):
            self.assertIn(fragment, text)
        self.assertIn("TODO", text)
        # 2.6.5: the coordinator's participant-code rule, stated where Phase 1 now enforces it.
        self.assertIn("Never write a token shaped like a participant code", text)
        self.assertIn("The rule is mechanical", text)
        self.assertIn("is not an identifier and may be quoted", text)
        # The validator refuses a source join or evidence path naming a code-named deposit file,
        # while this prompt demands exact joins; the statement says which rule wins and how.
        self.assertIn("must not be cited in a source join or an evidence path", " ".join(text.split()))
        self.assertIn("bind that evidence to another manifest entry", " ".join(text.split()))

    def test_result_path_strings_and_write_order(self) -> None:
        text = self.prompt()
        self.assertIn('"path": "REPORT.md"', text)
        self.assertIn('"path": "evidence/manifest.json"', text)
        self.assertIn("Write result.json last", text)

    def test_efficiency_guidance_present(self) -> None:
        text = self.prompt()
        self.assertIn("ONE analysis script", text)
        self.assertIn("40 turns", text)

    def test_bash_steer_reflects_non_fatal_denials_and_allowlist(self) -> None:
        self.assertIn("does not fail the role", run_phase1.BASH_STEER)
        self.assertNotIn("fails your entire role", run_phase1.BASH_STEER)
        for command in ("mkdir", "cp", "rm", "grep", "awk"):
            self.assertIn(f"Bash({command} *)", phase1_runtime.BASH_ALLOWLIST)

    def test_policy_hash_no_longer_claims_bare_mode(self) -> None:
        argv = run_phase1.claude_command(
            "claude", pathlib.Path("s.json"), pathlib.Path("src"), pathlib.Path("pkt"),
            "00000000-0000-0000-0000-000000000000", "sonnet", "max", "15",
        )
        self.assertNotIn("--bare", argv)
        self.assertIn("--safe-mode", argv)


class CoordinatorPromptStatesTheConclusionChangingRule(unittest.TestCase):
    """The seventh production audit lost a 49-minute coordinator call to this: three Sonnet auditors
    agreed a finding was conclusion-changing, the draft summary said "set it only with two
    independent confirmations", the coordinator set changes_conclusion=true, and the validator
    rejected the whole output because confirmations must span two MODEL FAMILIES -- which no
    pre-critique row can. The prompt and the summary now state the actual rule."""

    FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "coordinator_row_same_family_conclusion.json"

    def test_prompt_states_the_rule(self) -> None:
        text = run_coordinator.build_prompt("audit-test", "inv-test", "0" * 64)
        self.assertIn("changes_conclusion=false", text)
        self.assertIn("two model families", text)
        self.assertIn("round 1 supplies the second family", text)
        # 2.6.2: and the candidate status goes in ADJUDICATION.md, never in the projected claim text
        # (the seventh audit's F1 claim still read "a candidate ... requires a second model family"
        # in Appendix A after both families had confirmed it).
        self.assertIn("in ADJUDICATION.md only, never in the row's claim text", text)

    def test_validator_rejects_same_family_confirmations_and_accepts_the_row_left_false(self) -> None:
        import copy, json, sys
        sys.path.insert(0, str(SKILL / "schemas" / "tools"))
        import validate_v2
        row = json.loads(self.FIXTURE.read_text())
        self.assertTrue(row["changes_conclusion"])
        self.assertEqual(len({v["model_family"] for v in row["verifications"]}), 1)
        with self.assertRaises(Exception) as caught:
            validate_v2.validate_finding_row(copy.deepcopy(row), "findings[14]")
        self.assertIn("must span two known model families", str(caught.exception))
        row["changes_conclusion"] = False
        validate_v2.validate_finding_row(row, "findings[14]")   # the same row, as the prompt now asks for it

    def test_prompt_states_the_verification_join_and_the_exact_family_string(self) -> None:
        """Attempt 2 of the same audit copied every invocation id and report hash exactly and was
        rejected for writing model_family "Sonnet": the ledger says "claude" and nothing the model
        reads says so. The prompt now states the join field by field, with the literal."""
        text = run_coordinator.build_prompt("audit-test", "inv-test", "0" * 64)
        self.assertIn("`model_family` is exactly `claude`", text)
        self.assertIn("never a model name", text)
        self.assertIn("`evidence_record.sha256` are copied verbatim", text)

    def test_draft_summary_hint_no_longer_misstates_the_rule(self) -> None:
        import subprocess
        source = (SKILL / "scripts" / "build_draft_registry.py").read_text()
        self.assertNotIn("set it only with two independent confirmations", source)
        self.assertIn("two model families", source)


class CoordinatorPromptNamesValidatorHeadings(unittest.TestCase):
    def test_exact_headings_are_named(self) -> None:
        text = run_coordinator.build_prompt("audit-test", "inv-test", "0" * 64)
        for heading in ("## Abstract", "## 1. Introduction", "## Data and Code Availability",
                        "## References", "## Appendix A. Summary of findings"):
            self.assertIn(heading, text, heading)
        self.assertIn("Conclusion", text)
        self.assertIn("Recommendations", text)
        for citation in ("AI-Games", "AI-vertical"):
            self.assertIn(citation, text)
        # The validator demands these markers; the prompt must supply the reference text itself,
        # otherwise a careful model refuses to invent a citation (attempt 2 of the first audit).
        self.assertIn("10.1073/pnas.2524747123", text)
        self.assertIn("osf.io/dkfzt", text)
        self.assertIn("[]{#ref-AI-Games}", text)
        self.assertIn("[]{#ref-AI-vertical}", text)
        # check_consistency.sh's process-narration detector; attempt 2 of the first audit tripped
        # "earlier draft" by paraphrasing a drafting rule into the public report.
        for banned in ("earlier draft", "round 1", "our critics", "we initially"):
            self.assertIn(banned, text, banned)
        for label in ("Sources", "Question", "Checks", "Decision", "Evidence",
                      "Required propagation"):
            self.assertIn(label, text, label)


if __name__ == "__main__":
    unittest.main()


class CriticPromptNamesValidatorRules(unittest.TestCase):
    """Same defect class as the role-prompt tests above, on the critique side.

    Every rule below is enforced by validate_v2 / validate_critic_obj against BOTH critics and the
    targeted adjudicator, so each assertion runs against each role's rendered contract.
    """

    def test_new_issue_finding_id_rule_is_salient(self) -> None:
        for role, inventory in ROLE_INVENTORIES.items():
            with self.subTest(role=role):
                self.assertIn("Every `new_issues[].finding_id` is `null`",
                              rendered_contract(inventory))

    def test_conflicts_are_limited_to_covered_claims(self) -> None:
        for role, inventory in ROLE_INVENTORIES.items():
            with self.subTest(role=role):
                text = rendered_contract(inventory)
                self.assertIn("`conflicts[].claim_ids`", text)
                self.assertIn("`covered_claim_ids`", text)

    def test_every_semantic_rule_the_validator_rejects_on_is_named(self) -> None:
        # The validator's own failure strings, kept in step with the contract. Adding a semantic
        # rejection to validate_v2 without stating it here should fail this test.
        for role, inventory in ROLE_INVENTORIES.items():
            text = rendered_contract(inventory).lower()
            for phrase in ("new_issues[].finding_id", "conflicts[].claim_ids",
                           "requested_correction"):
                with self.subTest(role=role, rule=phrase):
                    self.assertIn(phrase.lower(), text)

    def test_each_role_is_told_its_own_inventory_path(self) -> None:
        # The exact bug: the contract named only the critic layout, and the adjudicator cited the
        # path real in its own snapshot and was killed for it.
        for role, inventory in ROLE_INVENTORIES.items():
            with self.subTest(role=role):
                text = rendered_contract(inventory)
                self.assertIn(inventory, text)
                other = next(v for k, v in ROLE_INVENTORIES.items() if k != role)
                if other != inventory:
                    self.assertNotIn(other, text.replace(inventory, ""))


class VerifierPromptNamesValidatorRules(unittest.TestCase):
    """The 2.6.4 post-fan-out verifier (scripts/run_verifier.py) is graded by its own admission
    validator, which reuses the generic Phase-1 checks and adds the verifier rules. The same defect
    class as above applies: a rule the validator enforces but the prompt does not state costs a
    paid attempt of a three-start budget. run_verifier.VERIFIER_RULES maps every rule the verifier
    validator can reject on to the prompt phrase(s) that state it; _reject() refuses an
    unregistered rule. These tests render the real prompt, drive the real validator into every
    rule on disk, and fail if a rejection is unregistered or unstated. (In the Neudorfer audit the
    hand-made verifier had no contract at all: CLAIMS.md asked for JSON on stdout and nothing
    checked it.)"""

    ROLE = "verifier_0123456789ab"
    SNAPSHOT = {
        "snapshot_sha256": "1" * 64,
        "source_files": [
            {"path": "paper/paper.txt", "sha256": "2" * 64, "bytes": 10},
            {"path": "pkg/data.csv", "sha256": "3" * 64, "bytes": 20},
        ],
    }

    @classmethod
    def setUpClass(cls) -> None:
        # Imported when the tests run, not when the module is discovered, and every class below is
        # taken from the runtime module run_verifier itself uses: other suites swap the
        # phase1_runtime entry in sys.modules while discovery imports them.
        sys.path.insert(0, str(SKILL / "tests"))
        import fake_claude_phase1
        import run_verifier
        cls.fake = fake_claude_phase1
        cls.rv = run_verifier

    def packet(self) -> dict:
        hypotheses = [
            {"hypothesis_id": "H1", "claim": "Claim one.", "claim_sha256": "a" * 64,
             "source_location": {"path": "paper/paper.txt", "locator": "p. 2"},
             "source_hashes": [{"path": "paper/paper.txt", "sha256": "2" * 64}]},
            {"hypothesis_id": "H2", "claim": "Claim two.", "claim_sha256": "b" * 64,
             "source_location": {"path": "pkg/data.csv", "locator": "column 2"},
             "source_hashes": [{"path": "pkg/data.csv", "sha256": "3" * 64}]},
        ]
        return {
            "schema_version": "i4r-verifier-packet-1", "role": self.ROLE,
            "source_snapshot_sha256": self.SNAPSHOT["snapshot_sha256"],
            "source_manifest": self.SNAPSHOT["source_files"], "hypotheses": hypotheses,
            "hypotheses_sha256": self.rv.runtime.sha256_bytes(self.rv.runtime.canonical_bytes(hypotheses)),
        }

    def prompt(self, packet: dict | None = None) -> str:
        return " ".join(self.rv.build_verifier_prompt(self.ROLE, "audit-test", packet or self.packet()).split())

    def missing_phrases(self, prompt: str) -> dict[str, list[str]]:
        missing = {}
        for rule, phrases in self.rv.VERIFIER_RULES.items():
            absent = [phrase for phrase in phrases if " ".join(phrase.split()) not in prompt]
            if absent:
                missing[rule] = absent
        return missing

    def test_every_registered_rule_is_stated_in_the_prompt(self) -> None:
        prompt = self.prompt()
        self.assertEqual(self.missing_phrases(prompt), {})
        # Negative control: the check notices a rule dropped from the prompt.
        self.assertIn("report_blocks", self.missing_phrases(prompt.replace("### H1: CONFIRMED", "")))

    def valid_output(self, base: pathlib.Path) -> tuple[pathlib.Path, dict]:
        packet = self.packet()
        packet_path = base / "ROLE_PACKET.json"
        packet_path.write_text(json.dumps(packet), encoding="utf-8")
        out = base / "out"
        out.mkdir()
        self.fake.verifier_output(out, self.ROLE, packet_path, "success", 1)
        return out, packet

    @staticmethod
    def rehash(out: pathlib.Path) -> None:
        result = json.loads((out / "result.json").read_text())
        result["report"]["sha256"] = hashlib.sha256((out / "REPORT.md").read_bytes()).hexdigest()
        result["evidence_manifest"]["sha256"] = hashlib.sha256(
            (out / "evidence/manifest.json").read_bytes()).hexdigest()
        (out / "result.json").write_text(json.dumps(result), encoding="utf-8")

    def cases(self):
        def result(fn):
            def apply(out):
                value = json.loads((out / "result.json").read_text())
                fn(value)
                (out / "result.json").write_text(json.dumps(value), encoding="utf-8")
            return apply

        def verdict(fn):
            return result(lambda value: fn(value["scientific_fields"]["verdicts"][0]))

        def manifest(fn):
            def apply(out):
                value = json.loads((out / "evidence/manifest.json").read_text())
                fn(value)
                (out / "evidence/manifest.json").write_text(json.dumps(value), encoding="utf-8")
                self.rehash(out)
            return apply

        def report(fn):
            def apply(out):
                (out / "REPORT.md").write_text(fn((out / "REPORT.md").read_text()), encoding="utf-8")
                self.rehash(out)
            return apply

        marker = self.rv.COMPLETION_MARKER
        return {
            "output_files": lambda out: (out / "REPORT.md").unlink(),
            "report_utf8": lambda out: (out / "REPORT.md").write_bytes(b"\xff\xfe broken"),
            "result_json": lambda out: (out / "result.json").write_text("{not json", encoding="utf-8"),
            "report_hash": result(lambda v: v["report"].update(sha256="0" * 64)),
            "manifest_hash": result(lambda v: v["evidence_manifest"].update(sha256="0" * 64)),
            "report_absolute_path": report(lambda t: t + "\nsee /home/someone/x\n"),
            "result_absolute_path": verdict(lambda v: v.update(basis="read at /private/tmp/x")),
            "participant_date": report(lambda t: t + "\nparticipant_A1 seen 2024-01-02\n"),
            "report_participant_code": report(lambda t: t + "\nThe row for SUBJ_P12 is repeated.\n"),
            "result_participant_code": verdict(lambda v: v.update(basis="SUBJ_P12 is repeated")),
            "report_join_missing": report(lambda t: "\n".join(
                line for line in t.splitlines() if "<!-- source:" not in line)),
            "report_join_unbound": report(lambda t: t + "\n<!-- source:pkg/data.csv@" + "7" * 64 + " -->\n"),
            "manifest_header": manifest(lambda m: m.update(role="verifier_ffffffffffff")),
            "manifest_bindings_missing": manifest(lambda m: m.update(source_bindings=[])),
            "manifest_binding_unbound": manifest(lambda m: m["source_bindings"][0].update(sha256="7" * 64)),
            "output_symlink": lambda out: (out / "evidence/link").symlink_to(out / "REPORT.md"),
            "artifact_row": manifest(lambda m: m["artifacts"].append(dict(m["artifacts"][0]))),
            "artifact_path": manifest(lambda m: m["artifacts"][0].update(path="./" + m["artifacts"][0]["path"])),
            "artifact_hash": manifest(lambda m: m["artifacts"][0].update(sha256="0" * 64)),
            "manifest_closure": lambda out: (out / "evidence/stray.json").write_text("{}", encoding="utf-8"),
            "closed_file_set": lambda out: (out / "scratch.txt").write_text("x", encoding="utf-8"),
            "result_keys": result(lambda v: v.update(notes="extra")),
            "result_header": result(lambda v: v.update(status="partial")),
            "result_snapshot": result(lambda v: v.update(source_snapshot_sha256="8" * 64)),
            "hypotheses_hash": result(lambda v: v.update(hypotheses_sha256="9" * 64)),
            "scientific_fields": result(lambda v: v["scientific_fields"].update(findings=[])),
            "verdict_keys": verdict(lambda v: v.pop("basis")),
            "verdict_set": result(lambda v: v["scientific_fields"]["verdicts"].append(
                dict(v["scientific_fields"]["verdicts"][0], hypothesis_id="H99"))),
            "verdict_claim_hash": verdict(lambda v: v.update(claim_sha256="c" * 64)),
            "verdict_value": verdict(lambda v: v.update(verdict="OVERSTATED")),
            "verdict_evidence": verdict(lambda v: v.update(evidence=[])),
            "verdict_rerun": verdict(lambda v: v["rerun"].update(artifact=None, command=None)),
            "verdict_basis": verdict(lambda v: v.update(basis=" ")),
            "report_placeholder": report(lambda t: t + "\nTODO\n"),
            "report_headings": report(lambda t: t.replace("## Evidence inventory", "## Evidence list")),
            "report_marker": report(lambda t: t + "\n" + marker + "\n"),
            "report_blocks": report(lambda t: t.replace("### H1: CONFIRMED", "### H1: REFUTED")),
        }

    def test_every_rule_the_validator_rejects_on_is_registered_and_reachable(self) -> None:
        triggered: dict[str, str] = {}
        for expected, mutate in self.cases().items():
            with self.subTest(rule=expected), tempfile.TemporaryDirectory() as directory:
                out, packet = self.valid_output(pathlib.Path(directory))
                mutate(out)
                with self.assertRaises(self.rv.runtime.OutputValidationError) as caught:
                    self.rv.validate_verifier_output(out, self.ROLE, self.SNAPSHOT, packet)
                self.assertIn(caught.exception.rule, self.rv.VERIFIER_RULES,
                              f"unregistered rejection: {caught.exception}")
                self.assertEqual(caught.exception.rule, expected, str(caught.exception))
                triggered[expected] = str(caught.exception)
        with tempfile.TemporaryDirectory() as directory:
            out, packet = self.valid_output(pathlib.Path(directory))
            # Positive control: the untouched output is admitted.
            self.rv.validate_verifier_output(out, self.ROLE, self.SNAPSHOT, packet)
            # A rerun artifact the manifest does not list is caught after the manifest is read.
            value = json.loads((out / "result.json").read_text())
            value["scientific_fields"]["verdicts"][0]["rerun"]["artifact"] = "evidence/unlisted.py"
            (out / "result.json").write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(self.rv.runtime.OutputValidationError) as caught:
                self.rv.validate_verifier_output(out, self.ROLE, self.SNAPSHOT, packet)
            self.assertEqual(caught.exception.rule, "verdict_rerun")
        with tempfile.TemporaryDirectory() as directory:
            out, packet = self.valid_output(pathlib.Path(directory))
            frozen = self.rv.verifier_projection(out, self.ROLE, self.SNAPSHOT, packet)
            (out / "REPORT.md").write_text(
                (out / "REPORT.md").read_text().replace("Every hypothesis", "Each hypothesis"),
                encoding="utf-8")
            self.rehash(out)
            with self.assertRaises(self.rv.runtime.OutputValidationError) as caught:
                self.rv.validate_verifier_output(out, self.ROLE, self.SNAPSHOT, packet,
                                                 expected_scientific_projection=frozen)
            self.assertEqual(caught.exception.rule, "frozen_projection")
            triggered["frozen_projection"] = str(caught.exception)
        # Every registered rule is reachable, so the registry cannot rot into dead phrases.
        self.assertEqual(set(triggered), set(self.rv.VERIFIER_RULES))

    def test_only_structural_bindings_are_repairable(self) -> None:
        repairable = set()
        for rule, mutate in self.cases().items():
            with tempfile.TemporaryDirectory() as directory:
                out, packet = self.valid_output(pathlib.Path(directory))
                mutate(out)
                try:
                    self.rv.validate_verifier_output(out, self.ROLE, self.SNAPSHOT, packet)
                except self.rv.runtime.OutputValidationError as exc:
                    if exc.repairable:
                        repairable.add(rule)
        self.assertEqual(repairable, {"report_hash", "manifest_hash", "hypotheses_hash",
                                      "manifest_binding_unbound", "artifact_path"})

    def test_an_unregistered_rule_cannot_be_raised(self) -> None:
        with self.assertRaises(AssertionError):
            self.rv._reject("a_rule_nobody_stated", "message")

    def review_mutations(self):
        """The 2.6.4 review's admission holes (runner-1, runner-5, runner-6), one on-disk mutation
        each, with the registered rule that must reject it and whether a focused repair may fix it.
        Before the fix the first, third and fifth were ADMITTED (None == None, and a single
        re.search over the report), and the second and fourth crashed with TypeError."""
        def result(fn):
            def apply(out):
                value = json.loads((out / "result.json").read_text())
                fn(value)
                (out / "result.json").write_text(json.dumps(value), encoding="utf-8")
            return apply

        def evidence(row):
            return result(lambda value: [v.update(evidence=[dict(row)])
                                         for v in value["scientific_fields"]["verdicts"]])

        def manifest(fn):
            def apply(out):
                value = json.loads((out / "evidence/manifest.json").read_text())
                fn(value)
                (out / "evidence/manifest.json").write_text(json.dumps(value), encoding="utf-8")
                self.rehash(out)
            return apply

        def report(fn):
            def apply(out):
                (out / "REPORT.md").write_text(fn((out / "REPORT.md").read_text()), encoding="utf-8")
                self.rehash(out)
            return apply

        missing = "pkg/NOT_IN_THE_DEPOSIT.csv"
        return [
            ("evidence names a file absent from the deposit with a null hash", "verdict_evidence", False,
             evidence({"source_path": missing, "source_sha256": None, "result": "fabricated"})),
            ("evidence source_path is a list", "verdict_evidence", False,
             evidence({"source_path": ["paper/paper.txt"], "source_sha256": "2" * 64, "result": "read"})),
            ("manifest binds a file absent from the deposit with a null hash", "manifest_binding_unbound", True,
             manifest(lambda m: m.update(source_bindings=[{"path": missing, "sha256": None}]))),
            ("manifest binding path is a list", "manifest_binding_unbound", True,
             manifest(lambda m: m.update(source_bindings=[{"path": ["pkg/data.csv"], "sha256": "3" * 64}]))),
            ("a second Hypotheses tested section contradicts the first", "report_blocks", False,
             report(lambda t: t + "\n## Hypotheses tested\n\n### H1: REFUTED\n\nOn reflection, no.\n")),
            ("a verdict block outside Hypotheses tested", "report_blocks", False,
             report(lambda t: t.replace("## Evidence inventory\n",
                                        "## Evidence inventory\n\n### H1: REFUTED\n\nContradiction.\n"))),
        ]

    def test_the_review_mutations_are_rejected_on_a_registered_stated_rule(self) -> None:
        for label, rule, repairable, mutate in self.review_mutations():
            with self.subTest(mutation=label), tempfile.TemporaryDirectory() as directory:
                out, packet = self.valid_output(pathlib.Path(directory))
                mutate(out)
                with self.assertRaises(self.rv.runtime.OutputValidationError) as caught:
                    self.rv.validate_verifier_output(out, self.ROLE, self.SNAPSHOT, packet)
                self.assertEqual(caught.exception.rule, rule, str(caught.exception))
                self.assertEqual(caught.exception.repairable, repairable, str(caught.exception))
                self.assertIn(rule, self.rv.VERIFIER_RULES)
        self.assertEqual(self.missing_phrases(self.prompt()), {})

    def test_an_unexpected_shape_is_an_invalid_sample_never_a_crash(self) -> None:
        """runner-5's backstop: a TypeError/KeyError/AttributeError inside admission is a sample
        fault (rule result_json), so run_role resamples instead of recording 'permanent'."""
        for error in (TypeError("unhashable type: 'list'"), KeyError("source_path"), AttributeError("get")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                out, packet = self.valid_output(pathlib.Path(directory))
                with mock.patch.object(self.rv.runtime, "check_report_joins", side_effect=error):
                    with self.assertRaises(self.rv.runtime.OutputValidationError) as caught:
                        self.rv.validate_verifier_output(out, self.ROLE, self.SNAPSHOT, packet)
                self.assertEqual(caught.exception.rule, "result_json")
                self.assertFalse(caught.exception.repairable)
        with tempfile.TemporaryDirectory() as directory:  # positive control
            out, packet = self.valid_output(pathlib.Path(directory))
            self.rv.validate_verifier_output(out, self.ROLE, self.SNAPSHOT, packet)


class DepositPersonalDataIsReportedNotWithheld(unittest.TestCase):
    """2.6.8: personal data in the deposit is reported in the comment, never a reason to stop. The
    auditors, the coordinator and the fairness critic are told so; the post-fan-out verifier's prompt
    is unchanged, because published verifier records re-admit only against the exact prompt bytes."""

    # sha256 of run_verifier.build_verifier_prompt on the synthetic packet below, measured on 2.6.7.
    VERIFIER_PROMPT_267 = "75e937e9460529a752217c7121f5c1257173aeb84d33141da66881953d2e610a"

    def test_every_auditor_is_told_to_report_personal_data_once(self) -> None:
        paragraph = run_phase1.AUDITOR_PII_REPORTING
        for role in phase1_runtime.ROLE_SPECS:
            with self.subTest(role=role):
                prompt = run_phase1.build_role_prompt(role, "audit-test", {"source_snapshot_sha256": "0" * 64})
                self.assertEqual(prompt.count(paragraph), 1)
        text = " ".join(paragraph.split())
        for phrase in ("never a reason to stop", "a finding with disclosure_critical true", "file and column",
                       "rows or participants affected", "whether anonymisation was claimed",
                       "verified remedy", "never an identifying value",
                       "never a procedure for identifying a person",
                       "different line from any sentence that mentions participants"):
            self.assertIn(phrase, text)

    def test_the_verifier_prompt_is_byte_identical_to_267(self) -> None:
        import run_verifier
        packet = {"source_snapshot_sha256": "a" * 64, "hypotheses_sha256": "b" * 64,
                  "hypotheses": [{"hypothesis_id": "H1", "claim_sha256": "c" * 64},
                                 {"hypothesis_id": "H2", "claim_sha256": "d" * 64}]}
        prompt = run_verifier.build_verifier_prompt("verifier_0123456789ab", "20260101-audit-00000000", packet)
        self.assertEqual(hashlib.sha256(prompt.encode("utf-8")).hexdigest(), self.VERIFIER_PROMPT_267)
        self.assertNotIn("Personal data in the deposit", prompt)

    def test_the_coordinator_never_excludes_or_defers_a_personal_data_finding(self) -> None:
        text = " ".join(run_coordinator.build_prompt("audit-test", "inv-test", "0" * 64).split())
        self.assertIn("Personal data in the deposit is reported, never withheld", text)
        self.assertIn("Never EXCLUDE, defer or withhold such a finding for privacy reasons", text)
        self.assertIn("never reproduce an identifying value", text)
        # Scoped to findings the evidence supports: a refuted one is a wrong claim, not a privacy matter.
        self.assertIn("identifying data, where the safe evidence supports it, stays INCLUDED", text)
        self.assertIn("A disclosure finding the safe evidence refutes is not a privacy matter but a wrong "
                      'claim: EXCLUDE it as a tested negative whose reason opens with "Refuted:" or '
                      '"Tested negative:"', text)
        # The stewardship sentence that precedes it is kept.
        self.assertIn("never describe, imply, or reason through a procedure for identifying a person", text)

    def test_the_fairness_critic_blocks_only_values_linkages_and_procedures(self) -> None:
        text = " ".join((SKILL / "prompts/role_fairness.txt").read_text(encoding="utf-8").split())
        self.assertIn("without any value is the required form, not a leak", text)
        # The BLOCKING cap is scoped to correct disclosure findings, never a global cap on the critic.
        self.assertIn("For such a finding that the deposit supports, mark BLOCKING only a reproduced "
                      "identifying value, a new linkage", text)
        self.assertNotIn("Mark BLOCKING only a reproduced identifying value", text)
        self.assertIn("Never request that a correct disclosure finding be removed, deferred", text)
        self.assertIn("A disclosure finding the deposit does not support is REFUTED like any other claim, "
                      "and BLOCKING when it is materially wrong.", text)
        self.assertIn("Do not reproduce sensitive values in your critique.", text)
