"""A critic that took a focused repair made the whole audit unfinalizable.

``run_critique.sh``'s focused-repair path re-dispatches a role with an augmented prompt and stamps
THAT prompt's hash into the critic result's ``input_hashes.prompt_sha256``. ``finalize_run.sh``
calls ``validate_round_projections.py``, which recomputed the hash of the round's ORIGINAL rendered
prompt and required exact equality across all five input hashes. So a role that took a repair
produced a result the finaliser rejected with "input-hash binding mismatch" -- after every round
had completed and every gate had passed.

Focused repair is a documented, journalled part of the contract: it is bounded to one attempt per
role, preserves every scientific field byte-for-byte, and writes its prompt to
``repair_prompt.txt`` inside the attempt directory. The binding is still real after this fix: the
recorded hash must match a prompt file that actually exists on disk for THAT role and THAT round,
and the other four hashes must match exactly. What is no longer required is that the prompt be the
un-augmented one.

Found on audit A37, where round 2's completeness critic was repaired
("repaired without scientific changes") and finalize then refused the run.
"""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))


def _load():
    spec = importlib.util.spec_from_file_location(
        "validate_round_projections", SKILL / "scripts" / "validate_round_projections.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_round_projections"] = module
    spec.loader.exec_module(module)
    return module


projections = _load()

BASE = {
    "snapshot_sha256": "a" * 64,
    "report_sha256": "b" * 64,
    "prompt_sha256": "c" * 64,
    "policy_sha256": "d" * 64,
    "evidence_manifest_sha256": "e" * 64,
}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ARepairedRoleCanStillFinalize(unittest.TestCase):
    """Exercises the binding rule against real files on disk.

    These used to slice validate_round_projections.py and assert that particular expressions
    appeared in the slice -- which passes unchanged if the condition around them is inverted.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="i4r-repair-binding-")
        self.round_dir = pathlib.Path(self._tmp.name).resolve() / "round_2"
        self.round_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_repair(self, role: str, text: str, *, round_dir: pathlib.Path | None = None,
                     invocation: str = "inv-1", attempt: str = "2") -> str:
        base = (round_dir or self.round_dir)
        path = base / "invocations" / invocation / "attempts" / role / attempt
        path.mkdir(parents=True, exist_ok=True)
        (path / "repair_prompt.txt").write_text(text, encoding="utf-8")
        return sha(text)

    def resolve(self, hashes: dict, role: str = "completeness") -> dict:
        return projections.admit_repair_prompt(hashes, dict(BASE), self.round_dir, role)

    def test_an_unrepaired_role_binds_exactly(self) -> None:
        self.assertEqual(self.resolve(dict(BASE)), BASE)

    def test_a_recorded_repair_prompt_is_admitted(self) -> None:
        digest = self.write_repair("completeness", "original prompt\n\nREPAIR: fix the schema\n")
        hashes = {**BASE, "prompt_sha256": digest}
        self.assertEqual(self.resolve(hashes), hashes)

    def test_a_prompt_hash_matching_no_file_is_still_a_mismatch(self) -> None:
        self.write_repair("completeness", "some other repair\n")
        hashes = {**BASE, "prompt_sha256": sha("a prompt nobody wrote")}
        self.assertNotEqual(self.resolve(hashes), hashes)

    def test_another_roles_repair_prompt_does_not_satisfy_the_binding(self) -> None:
        digest = self.write_repair("stats", "stats repair\n")
        hashes = {**BASE, "prompt_sha256": digest}
        self.assertNotEqual(self.resolve(hashes, role="completeness"), hashes)

    def test_another_rounds_repair_prompt_does_not_satisfy_the_binding(self) -> None:
        other = self.round_dir.parent / "round_3"
        other.mkdir()
        digest = self.write_repair("completeness", "round 3 repair\n", round_dir=other)
        hashes = {**BASE, "prompt_sha256": digest}
        self.assertNotEqual(self.resolve(hashes), hashes)

    def test_only_the_prompt_hash_can_be_relaxed(self) -> None:
        """Negative control: a repair prompt on disk must not excuse any other mismatch."""
        digest = self.write_repair("completeness", "a real repair\n")
        for key in ("snapshot_sha256", "report_sha256", "policy_sha256",
                    "evidence_manifest_sha256"):
            with self.subTest(key=key):
                hashes = {**BASE, "prompt_sha256": digest, key: "f" * 64}
                self.assertNotEqual(self.resolve(hashes), hashes)

    def test_a_result_with_no_prompt_hash_is_untouched(self) -> None:
        hashes = {k: v for k, v in BASE.items() if k != "prompt_sha256"}
        hashes["report_sha256"] = "f" * 64
        self.assertEqual(self.resolve(hashes), BASE)

    def test_the_callers_expected_hashes_are_not_mutated(self) -> None:
        digest = self.write_repair("completeness", "a real repair\n")
        expected = dict(BASE)
        projections.admit_repair_prompt(
            {**BASE, "prompt_sha256": digest}, expected, self.round_dir, "completeness")
        self.assertEqual(expected, BASE)


class TheRepairPathStillWritesThatFile(unittest.TestCase):
    def test_run_critique_writes_repair_prompt_txt(self) -> None:
        # The one thing that cannot be checked from this side: the driver has to produce the
        # file this binding looks for. tests/test_run_critique.sh exercises the repair path
        # end to end; this only pins the filename the two sides agree on.
        self.assertIn("repair_prompt.txt", (SKILL / "scripts/run_critique.sh").read_text())


if __name__ == "__main__":
    unittest.main()
