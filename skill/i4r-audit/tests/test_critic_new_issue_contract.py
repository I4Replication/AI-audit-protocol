"""A critic must be able to raise a new issue.

`validate_v2` requires ``new_issues[].finding_id`` to be ``null`` (schemas/tools/validate_v2.py,
"new issues must use null finding_id"). The runtime narrows the SHARED ``$defs/assessment``
definition's ``finding_id`` to an enum of the covered claim ids, so that ``items`` must join
one-to-one to the assigned claims. ``new_issues`` referenced the same definition, so the
structured-output decoder was forced to emit a real finding id there -- ``null`` was not a
permitted value -- and the validator then rejected exactly what it had been forced to produce.

The two rules contradicted each other, and no prompt wording could bridge them: a constrained
decoder cannot emit a value the schema excludes.

Measured on audit A38's round 2, across all twelve critic attempts, the
correlation was perfect: every attempt that raised zero new issues passed, and every attempt that
raised one or more failed. Round 1 had survived only because all four critics happened to raise
none. The round lost quorum twice, and a prompt fix in between changed nothing.

These tests pin the separation, in both directions.
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "schemas/tools"))
import validate_v2  # noqa: E402

SCHEMA = json.loads((SKILL / "schemas/v2/critic-result.schema.json").read_text())


class NewIssuesDoNotShareTheNarrowedAssessment(unittest.TestCase):
    def test_new_issues_use_their_own_definition(self) -> None:
        self.assertEqual(SCHEMA["properties"]["new_issues"]["items"],
                         {"$ref": "#/$defs/new_issue"})
        self.assertEqual(SCHEMA["properties"]["items"]["items"],
                         {"$ref": "#/$defs/assessment"})

    def test_a_new_issue_finding_id_can_only_be_null(self) -> None:
        # Matches the validator exactly. If this ever widens, the decoder is free to emit an id
        # again and every new issue becomes unrepresentable.
        self.assertEqual(SCHEMA["$defs"]["new_issue"]["properties"]["finding_id"],
                         {"type": "null"})

    def test_narrowing_assessment_leaves_new_issues_alone(self) -> None:
        # This is the exact operation the runtime performs (test_run_critique_runtime.py, where
        # covered_claim_ids narrows the assessment definition before the call).
        schema = copy.deepcopy(SCHEMA)
        covered = ["REP-F1_undisclosed_N123_exclusion_table3", "DAT-DI-03"]
        schema["$defs"]["assessment"]["properties"]["finding_id"] = {
            "type": "string", "enum": covered}
        self.assertEqual(schema["$defs"]["new_issue"]["properties"]["finding_id"],
                         {"type": "null"})
        self.assertNotIn("enum", schema["$defs"]["new_issue"]["properties"]["finding_id"])

    def test_the_validator_still_rejects_a_non_null_new_issue_finding_id(self) -> None:
        # Negative control: the rule this fix routes around must remain in force. If the
        # validator stops rejecting, the schema separation is guarding nothing.
        source = (SKILL / "schemas/tools/validate_v2.py").read_text()
        self.assertIn("new issues must use null finding_id", source)


class CriticPromptStatesBothSemanticRules(unittest.TestCase):
    """The prose fix alone was not sufficient, but it is still necessary.

    Rendered per role: the contract moved out of common.txt so the targeted adjudicator, which is
    graded by the same validator, receives it too.
    """

    def prompt(self, inventory: str = "source/context/EVIDENCE_HASHES.json") -> str:
        text = (SKILL / "prompts/contract.txt").read_text(encoding="utf-8")
        body = "\n".join(line for line in text.split("\n") if not line.startswith("#"))
        return " ".join(body.replace("{{INVENTORY}}", inventory).split())

    def test_the_null_rule_is_stated(self) -> None:
        for inventory in ("source/context/EVIDENCE_HASHES.json", "source/EVIDENCE_HASHES.json"):
            with self.subTest(inventory=inventory):
                self.assertIn("Every `new_issues[].finding_id` is `null`", self.prompt(inventory))

    def test_the_covered_claim_rule_is_stated(self) -> None:
        text = self.prompt()
        self.assertIn("`conflicts[].claim_ids`", text)
        self.assertIn("`covered_claim_ids`", text)


if __name__ == "__main__":
    unittest.main()
