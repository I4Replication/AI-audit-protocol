"""A mechanical draft row may not assert that a finding changes the paper's conclusion.

``validate_v2`` requires two independent confirmations for ``changes_conclusion: true`` at every
stage, before promotion as well as after -- see
``test_conclusion_change_requires_independence_even_before_promotion``. That is deliberate: it is
the strongest claim the registry can carry, and one model's say-so is not evidence for it.

``build_draft_registry`` nevertheless copied a single auditor's ``conclusion_changing`` flag
straight into the field, with ``verifications: []`` by construction. The resulting row could never
validate, and because the builder fails closed on the first bad row, one such finding aborted the
entire 27-row registry -- discarding every other finding in the audit along with it. The claim is
not dropped: it is surfaced to the coordinator in ``DRAFT_SUMMARY.md``, which is part of the
coordinator's snapshot, and the coordinator sets the field once confirmations exist.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "schemas/tools"))
import build_draft_registry  # noqa: E402
import run_coordinator  # noqa: E402
import validate_v2  # noqa: E402


def finding(**overrides):
    value = {
        "id": "F2_table3_table4_coding_footnote_mismatch",
        "claim": "Table 3 and Table 4 footnotes disagree with the coding used in the script.",
        "status": "CONFIRMED",
        "impact": "none",
        "evidence": [],
        "remedy": "Correct the footnote to match the analysis code.",
    }
    value.update(overrides)
    return value


class DraftRowsAssertNothing(unittest.TestCase):
    def build(self, **overrides):
        return build_draft_registry.build_row(
            "auditor_text_vs_data", "inv-0123456789abcdef", finding(**overrides), "os=darwin",
        )

    def test_a_conclusion_changing_flag_does_not_become_an_assertion(self) -> None:
        row = self.build(conclusion_changing=True)
        self.assertFalse(row["changes_conclusion"])
        self.assertIsNone(row["changes_conclusion_which"])
        self.assertFalse(row["coordinator_approved"])

    def test_such_a_row_validates_instead_of_aborting_the_registry(self) -> None:
        # Before the fix this raised, and the builder's fail-closed loop then discarded every
        # other finding in the audit.
        validate_v2.validate_finding_row(self.build(conclusion_changing=True), "draft[1]")

    def test_an_unflagged_finding_is_unchanged(self) -> None:
        row = self.build()
        self.assertFalse(row["changes_conclusion"])
        validate_v2.validate_finding_row(row, "draft[1]")

    def test_the_integrity_rule_itself_is_still_enforced(self) -> None:
        # Negative control: the bar this fix routes around must still be in force. If this ever
        # stops raising, the fix has become a hole rather than a re-routing.
        row = self.build()
        row["changes_conclusion"] = True
        row["changes_conclusion_which"] = "Primary inference"
        with self.assertRaises(validate_v2.ValidationError):
            validate_v2.validate_finding_row(row, "draft[1]")



class DepositPathsAreMaskedNotDropped(unittest.TestCase):
    """A finding about a hardcoded path necessarily quotes one.

    The second production audit's deposit opened with ``setwd("C:/Users/<user>/...")`` and
    ``setwd("/Users/<user>/...")``. The code-and-methods role correctly reported that as a
    portability defect, and its REPORT.md was clean -- the auditor described the paths without
    quoting them, as the role contract requires. ``result.json`` is not scanned for absolute
    paths, so the literal path reached the draft registry, and the coordinator's provider-safety
    gate then refused the entire 27-row input. The finding must survive; only the authors'
    account name needs to go.
    """

    def mask(self, text: str) -> str:
        return build_draft_registry.mask_home_accounts(text)

    def test_the_gate_accepts_what_the_masker_produces(self) -> None:
        # This is the property that actually matters: masked text must pass the same gate that
        # rejected the unmasked text. Masking only the account segment did NOT satisfy it,
        # because LOCAL_PATH matches /Users/<anything-not-a-slash>.
        for raw in (
            'setwd("C:/' + 'Users/4821/switchdrive/SHARED_FOLDERS")',
            'setwd("/' + 'Users/authorx/switchdrive/Name/Folder")',
            "/" + "home/anna/analysis/x.R",
        ):
            with self.subTest(raw=raw):
                self.assertTrue(run_coordinator.LOCAL_PATH.search(raw), "fixture is not a leak")
                masked = self.mask(raw)
                self.assertIsNone(run_coordinator.LOCAL_PATH.search(masked), masked)

    def test_the_finding_survives_the_masking(self) -> None:
        masked = self.mask('scripts open with setwd("C:/' + 'Users/4821/switchdrive/X")')
        self.assertIn("setwd(", masked)
        self.assertIn("switchdrive/X", masked)
        self.assertIn("<windows-user-dir>", masked)
        self.assertNotIn("4821", masked)

    def test_text_without_a_home_path_is_untouched(self) -> None:
        for text in ("no path here", "~/switchdrive/Name/Folder", "pkg/analysis.R"):
            with self.subTest(text=text):
                self.assertEqual(self.mask(text), text)

    def test_masking_reaches_nested_finding_structures(self) -> None:
        finding = {"claim": "x", "evidence": [{"result": "at C:/" + "Users/4821/y"}]}
        masked = build_draft_registry.mask_home_accounts(finding)
        self.assertNotIn("4821", str(masked))
        self.assertIn("<windows-user-dir>", masked["evidence"][0]["result"])


if __name__ == "__main__":
    unittest.main()
