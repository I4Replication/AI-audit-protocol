"""The Findings-section element check names what is missing, and the prompt spells the labels.

On audit A30 `auditor_text_vs_data` lost two paid attempts to
"role report findings omit evidence/benign explanation/impact/remedy": both reports carried
*Benign explanations tested*, *Impact* and *Remedy* labels and substantive evidence, but never the
word "evidence" inside the Findings section. The rule was expressed as four ELEMENTS in the prompt
and enforced as four TOKENS in the validator.
"""
import pathlib
import sys
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import phase1_runtime  # noqa: E402

ATTEMPT_ONE_SHAPE = """
- **F1 — A table value differs from the value recomputed from the deposit (CONFIRMED).**
  Recomputing the cell from the deposited file gives 0.42 where the table prints 0.24, a likely
  transposition. *Benign explanations tested*: a careful analyst may have
  merged two runs. *Impact*: none. *Remedy*: correct the printed value.
"""
FOUR_LABELS = ATTEMPT_ONE_SHAPE.replace("Recomputing the cell", "*Evidence*: recomputing the cell")


class FindingsElementCheck(unittest.TestCase):
    def test_the_rejected_production_shape_is_told_exactly_what_is_missing(self):
        self.assertEqual(["evidence"], phase1_runtime.findings_elements_missing(ATTEMPT_ONE_SHAPE))

    def test_all_four_labels_pass(self):
        self.assertEqual([], phase1_runtime.findings_elements_missing(FOUR_LABELS))

    def test_the_prompt_spells_every_label_the_validator_checks(self):
        prompt_source = (SKILL / "scripts" / "run_phase1.py").read_text(encoding="utf-8")
        for term in phase1_runtime.FINDING_ELEMENTS:
            self.assertIn(f'"{term}"', prompt_source,
                          f"the prompt no longer tells the role the validator looks for {term!r}")
        self.assertIn("*Benign explanations tested*", prompt_source)


if __name__ == "__main__":
    unittest.main()
