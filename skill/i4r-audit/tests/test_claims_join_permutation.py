"""The checklist gate demanded an ordering the claims-script builder is documented not to produce.

``make_claims_script.sh`` emits every claim it can execute, then an explicit no-code tail for claims
verified by inspection -- which is the shape SKILL.md requires ("Claims verified by inspection remain
in the explicit no-code tail"). ``generate_finding_artifacts.py``'s checklist nevertheless compared
the extracted claim numbers against ``list(range(1, N+1))`` with ``!=``, so any audit carrying a
single judgement or unverifiable claim failed the post-delta checklist gate with
"claims script does not join findings exactly".

It was latent until an audit produced a non-code claim. The dart-throwing audit has exactly one (a
raster schematic with no OCR layer, read by eye) and hit it at `[1..20, 22, 23, 24, 21]`. The first
production audit's own claims script is `[1,2,3,5,6,7,8,10,11,12,13,14,16,17,4,9,15]` and would
have failed the same gate had it run.

The property that actually matters is a bijection: every finding claimed exactly once, no claim
invented, none duplicated. Ordering is presentational. The check is now `sorted(...) == expected`.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))


def _load():
    spec = importlib.util.spec_from_file_location(
        "generate_finding_artifacts", SKILL / "scripts" / "generate_finding_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_finding_artifacts"] = module
    spec.loader.exec_module(module)
    return module


artifacts = _load()


class ClaimsJoinIsAPermutationCheck(unittest.TestCase):
    """Exercises the rule itself. These used to grep the source for the comparison operator,
    which passes just as well on a version whose branches have been swapped around it."""

    def accepts(self, observed: list[int], count: int) -> None:
        artifacts.check_claims_join(observed, list(range(1, count + 1)))

    def rejects(self, observed: list[int], count: int) -> str:
        with self.assertRaises(SystemExit) as caught:
            artifacts.check_claims_join(observed, list(range(1, count + 1)))
        return str(caught.exception)

    def test_strict_ascending_order_is_accepted(self) -> None:
        self.accepts([1, 2, 3, 4], 4)

    def test_the_documented_no_code_tail_is_accepted(self) -> None:
        # The first production audit's real ordering, and the dart-throwing audit's.
        self.accepts([1, 2, 3, 5, 6, 7, 8, 10, 11, 12, 13, 14, 16, 17, 4, 9, 15], 17)
        self.accepts([*range(1, 21), 22, 23, 24, 21], 24)

    def test_a_missing_claim_is_rejected_and_named(self) -> None:
        message = self.rejects([1, 2, 4], 4)
        self.assertIn("missing=[3]", message)

    def test_an_invented_claim_is_rejected_and_named(self) -> None:
        message = self.rejects([1, 2, 3, 9], 3)
        self.assertIn("unexpected=[9]", message)

    def test_a_duplicated_claim_is_rejected_and_named(self) -> None:
        # Negative control for the permutation loosening: reordering is fine, double-counting
        # a finding is not, and a set comparison would have let this through.
        message = self.rejects([1, 2, 2], 3)
        self.assertIn("duplicated=[2]", message)
        self.assertIn("missing=[3]", message)

    def test_an_empty_script_against_findings_is_rejected(self) -> None:
        self.assertIn("missing=[1, 2]", self.rejects([], 2))

    def test_claim_numbers_are_parsed_from_the_real_header_shape(self) -> None:
        script = (
            "# CLAIM 3 [reproduce] mean age\n"
            "expect(round(m, 1) == 24.6)\n"
            "# CLAIM 1 [reproduce] n\n"
            "# --- NO CODE: verified by inspection ---\n"
            "# CLAIM 2 [judgement] raster schematic read by eye\n"
        )
        self.assertEqual(artifacts.parse_claim_numbers(script), [3, 1, 2])
        self.accepts(artifacts.parse_claim_numbers(script), 3)


class TheNotebookCheckKeepsItsOrderingRequirement(unittest.TestCase):
    """The notebook generator emits chunks in strict order and has no documented tail, so its
    check is deliberately stricter. Changing it should be a decision, not a copied fix."""

    def test_the_notebook_comparison_is_still_ordered(self) -> None:
        source = (SKILL / "scripts/generate_finding_artifacts.py").read_text()
        self.assertIn("if notebook_numbers != expected_numbers:", source)


class TheBuilderStillEmitsTheDocumentedTail(unittest.TestCase):
    def test_make_claims_script_writes_a_no_code_tail(self) -> None:
        script = (SKILL / "scripts/make_claims_script.sh").read_text()
        self.assertIn("NO CODE", script)

    def test_skill_md_states_the_tail_is_required(self) -> None:
        self.assertIn("no-code tail", (SKILL / "SKILL.md").read_text())


if __name__ == "__main__":
    unittest.main()
