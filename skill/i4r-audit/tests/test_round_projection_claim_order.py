"""A later-round critic that listed its claims in a different order made the audit unfinalizable.

The critique runtime projects a round only after checking that each critic covered and assessed
exactly its machine assignment, compared casefolded as a set (``command_projection`` in
``test_run_critique_runtime.py``): no prompt states an ordering rule and the provider schema
cannot express one. ``validate_round_projections.py``, which ``finalize_run.sh`` runs, compared the
same lists with plain ``!=``. So a round the runtime had accepted was refused at finalization with
"actual claim assessments are not assigned", after every round and every release gate had passed.

Found on audit A21: round 2's completeness critic
assessed exactly its 18 assigned claims but listed F39 before F3.

The coverage rule itself still holds after this fix: a missing, extra or duplicated id is refused.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

import validate_round_projections as vrp  # noqa: E402

ASSIGNED = ["F1", "F10", "F11", "F2", "F28", "F3", "F39", "F4"]


class ClaimOrderTests(unittest.TestCase):
    def test_the_exact_assignment_matches(self) -> None:
        self.assertTrue(vrp.claims_match_assignment(list(ASSIGNED), ASSIGNED))

    def test_a_reordered_assessment_matches(self) -> None:
        # The observed case: F39 listed before F3.
        observed = ["F1", "F10", "F11", "F2", "F28", "F39", "F3", "F4"]
        self.assertNotEqual(observed, ASSIGNED)
        self.assertTrue(vrp.claims_match_assignment(observed, ASSIGNED))

    def test_a_case_difference_matches(self) -> None:
        self.assertTrue(vrp.claims_match_assignment([v.lower() for v in ASSIGNED], ASSIGNED))

    def test_a_missing_claim_is_refused(self) -> None:
        self.assertFalse(vrp.claims_match_assignment(ASSIGNED[:-1], ASSIGNED))

    def test_an_extra_claim_is_refused(self) -> None:
        self.assertFalse(vrp.claims_match_assignment(ASSIGNED + ["F99"], ASSIGNED))

    def test_a_duplicated_claim_is_refused(self) -> None:
        # The set is complete but one id is assessed twice (also when only its case differs).
        self.assertFalse(vrp.claims_match_assignment(ASSIGNED + ["F3"], ASSIGNED))
        self.assertFalse(vrp.claims_match_assignment(ASSIGNED + ["f3"], ASSIGNED))


if __name__ == "__main__":
    unittest.main()
