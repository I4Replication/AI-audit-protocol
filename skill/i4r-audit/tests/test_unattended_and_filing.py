"""Three operator traps that each let a correct audit end in the wrong place, or not end at all.

All three surfaced on the third production audit, the first driven end to end under this skill.
None of them is a scientific defect; each wastes an operator's run or files it where nobody reads.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
FINALIZE = (SKILL / "scripts/finalize_run.sh").read_text()
CRITIQUE = (SKILL / "scripts/run_critique.sh").read_text()
LOG_EVENT = (SKILL / "scripts/log_event.sh").read_text()


class UnattendedIsTheDefault(unittest.TestCase):
    """A round used to stop and wait for a human unless I4R_AUTO_AUTHORIZE=1 was set.

    The pause exists so somebody reviews the model route and data classes before paid dispatch.
    But the route is policy-pinned -- any value but the default is a hard fail_early -- so the
    pause could not actually widen or narrow anything; it only stopped the run. Unattended is now
    the default and I4R_AUTO_AUTHORIZE=0 restores the pause.

    The behaviour itself is exercised end to end in tests/test_run_critique.sh, which runs the
    real driver against the fake provider and asserts that the default dispatches while
    I4R_AUTO_AUTHORIZE=0 exits 3 before any provider request. What is checked here is only what
    that suite cannot see about itself: that its coverage of BOTH modes still exists.
    """

    def test_the_opt_out_is_documented_where_an_operator_will_look(self) -> None:
        header = CRITIQUE[:CRITIQUE.index("set -")]
        self.assertIn("I4R_AUTO_AUTHORIZE", header)
        self.assertIn("Set 0", header)

    def test_the_driver_suite_exercises_both_modes(self) -> None:
        suite = (SKILL / "tests/test_run_critique.sh").read_text()
        self.assertEqual(len(re.findall(r"run_driver [^\n]*I4R_AUTO_AUTHORIZE=0", suite)), 3)
        self.assertIn("paused exit 3", suite)
        # And the unattended default is asserted to journal its grant BEFORE it dispatches.
        self.assertIn("round_authorized", suite)


class FinalizeFilesToTheProgrammeRegistry(unittest.TestCase):
    """finalize defaulted to the registry inside the skill checkout, not the programme's.

    An operator who omitted --registry got a green "filed into the registry" and a row in
    skill/i4r-audit-unified/registry that nobody would ever read. The third production audit filed
    there and had to be re-filed by hand.
    """

    def test_the_default_is_derived_from_the_audit_directory(self) -> None:
        self.assertIn("resolve_registry ()", FINALIZE)
        block = FINALIZE[FINALIZE.index("resolve_registry ()"):][:900]
        # <tree>/runs/<journal>/<paper>/audit (2.5.0) or <tree>/<journal>/<paper>/audit -> <tree>/registry;
        # the depth is tree_layout.sh's business, not this function's
        self.assertIn("i4r_root_of_audit", block)
        self.assertIn('-d "$tree/registry"', block)

    def test_the_skill_local_registry_is_only_a_fallback(self) -> None:
        block = FINALIZE[FINALIZE.index("resolve_registry ()"):][:900]
        self.assertIn("SKILL_LOCAL_REGISTRY", block)
        self.assertLess(block.index('"$tree/registry"'), block.index("$SKILL_LOCAL_REGISTRY"))

    def test_an_explicit_registry_still_wins(self) -> None:
        self.assertIn("REGISTRY_EXPLICIT=1", FINALIZE)
        self.assertIn('if [ "$REGISTRY_EXPLICIT" -eq 0 ]; then', FINALIZE)

    def test_the_environment_variables_still_win_over_derivation(self) -> None:
        self.assertIn('DEFAULT_REGISTRY="${REGISTRY_DIR:-${I4R_REGISTRY_DIR:-}}"', FINALIZE)
        block = FINALIZE[FINALIZE.index("resolve_registry ()"):][:900]
        self.assertIn('if [ -n "$DEFAULT_REGISTRY" ]; then', block)

    def test_the_choice_is_announced(self) -> None:
        # The whole failure mode was that filing to the wrong registry looked identical to filing
        # to the right one.
        self.assertIn("registry: $REGISTRY (programme registry", FINALIZE)
        self.assertIn("skill-local", FINALIZE)

    def test_resolution_picks_the_tree_registry_for_a_real_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            audit = root / "tree/Journal Name/Paper Title/audit"
            audit.mkdir(parents=True)
            (root / "tree/registry").mkdir()
            out = subprocess.run(
                ["bash", "-c",
                 f'HERE={SKILL}/scripts; . "$HERE/tree_layout.sh"; SKILL_LOCAL_REGISTRY="$HERE/../../registry"; '
                 f'DEFAULT_REGISTRY=""; '
                 + FINALIZE[FINALIZE.index("resolve_registry ()"):FINALIZE.index("die () {")]
                 + f'\nresolve_registry "{audit}"'],
                capture_output=True, text=True)
            self.assertEqual(out.stdout, str(root / "tree/registry"), out.stderr)

    def test_resolution_picks_the_tree_registry_for_the_2_5_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            audit = root / "tree/runs/Journal Name/Paper Title/audit"
            audit.mkdir(parents=True)
            (root / "tree/registry").mkdir()
            out = subprocess.run(
                ["bash", "-c",
                 f'HERE={SKILL}/scripts; . "$HERE/tree_layout.sh"; SKILL_LOCAL_REGISTRY="$HERE/../../registry"; '
                 f'DEFAULT_REGISTRY=""; '
                 + FINALIZE[FINALIZE.index("resolve_registry ()"):FINALIZE.index("die () {")]
                 + f'\nresolve_registry "{audit}"'],
                capture_output=True, text=True)
            self.assertEqual(out.stdout, str(root / "tree/registry"), out.stderr)

    def test_resolution_falls_back_outside_a_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "a/b/audit"
            audit.mkdir(parents=True)
            out = subprocess.run(
                ["bash", "-c",
                 f'HERE={SKILL}/scripts; . "$HERE/tree_layout.sh"; SKILL_LOCAL_REGISTRY="/sentinel/registry"; '
                 f'DEFAULT_REGISTRY=""; '
                 + FINALIZE[FINALIZE.index("resolve_registry ()"):FINALIZE.index("die () {")]
                 + f'\nresolve_registry "{audit}"'],
                capture_output=True, text=True)
            self.assertEqual(out.stdout, "/sentinel/registry", out.stderr)


class AnAbsenceOfHumanEffortCanBeExplained(unittest.TestCase):
    """run.schema.json says null means "nobody recorded it", which is not zero -- but a run had no
    way to say WHICH kind of null it was, so an unattended audit produced the same finalize warning
    as one where somebody simply forgot. person_minutes could not be omitted, so the note field
    that already existed in the projection was unreachable.

    These run the real log_event.sh against a real run.json. The previous version asserted that
    particular shell fragments appeared in the script, which says nothing about what it accepts.
    """

    LOG_EVENT_SH = SKILL / "scripts" / "log_event.sh"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="i4r-human-effort-")
        base = pathlib.Path(self._tmp.name).resolve()
        self.audit = base / "a"
        (self.audit / "pkg").mkdir(parents=True)
        (self.audit / "pkg" / "f.csv").write_text("x\n", encoding="utf-8")
        # A real initialised run: the jq projection reads typed fields that only init_run.sh
        # creates, so a hand-built skeleton tests the wrong thing (or nothing).
        created = subprocess.run(
            ["bash", str(SKILL / "scripts" / "init_run.sh"), str(self.audit),
             "--audit-id", "t-human-effort", "--protocol-version", "p1",
             "--gate-set-version", "g1"],
            capture_output=True, text=True)
        if created.returncode != 0:
            self.skipTest(f"init_run.sh unavailable: {created.stderr[-400:]}")
        self.run_json = self.audit / "run.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def log(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.LOG_EVENT_SH), "--run", str(self.run_json), *arguments],
            capture_output=True, text=True)

    def run_value(self) -> dict:
        return json.loads(self.run_json.read_text(encoding="utf-8"))

    def test_a_notes_only_event_is_accepted_and_recorded(self) -> None:
        result = self.log("human_effort", "notes=unattended; no human performed any phase")
        self.assertEqual(result.returncode, 0, result.stderr)
        effort = self.run_value()["human_effort"]
        self.assertIn("unattended", effort["notes"])

    def test_a_notes_only_event_does_not_invent_a_phase_total(self) -> None:
        """The bug this caused: person_minutes_by_phase[None], which then crashed the projector."""
        self.log("human_effort", "notes=unattended")
        by_phase = self.run_value()["human_effort"]["person_minutes_by_phase"]
        # No None key -- that is what crashed json.dumps(sort_keys=True) -- and every real phase
        # still null, because "nobody recorded it" is not zero.
        self.assertNotIn(None, by_phase)
        self.assertEqual(set(by_phase.values()), {None})

    def test_a_phase_and_minutes_pair_still_accumulates(self) -> None:
        self.assertEqual(self.log("human_effort", "phase=critique",
                                  "person_minutes=95").returncode, 0)
        self.assertEqual(self.log("human_effort", "phase=critique",
                                  "person_minutes=15").returncode, 0)
        effort = self.run_value()["human_effort"]
        self.assertEqual(effort["person_minutes_by_phase"]["critique"], 110)

    def test_a_phase_without_minutes_is_refused(self) -> None:
        result = self.log("human_effort", "phase=critique")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be given together", result.stdout + result.stderr)

    def test_minutes_without_a_phase_are_refused(self) -> None:
        result = self.log("human_effort", "person_minutes=95")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be given together", result.stdout + result.stderr)

    def test_an_empty_human_effort_event_is_refused(self) -> None:
        result = self.log("human_effort")
        self.assertNotEqual(result.returncode, 0)

    def test_the_canonical_projector_agrees_with_the_jq_one(self) -> None:
        """log_event.sh and lifecycle_journal.py project the SAME events by two routes.

        Guarding only the jq one left the Python one writing person_minutes_by_phase[None],
        which crashed json.dumps(sort_keys=True) on mixed key types -- so the first notes-only
        event ever logged took the canonical projector down.
        """
        import lifecycle_journal

        root = self.audit.parent / "canonical"
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "f.csv").write_text("x\n", encoding="utf-8")
        subprocess.run(
            ["bash", str(SKILL / "scripts" / "init_run.sh"), str(root),
             "--audit-id", "t-human-effort-2", "--protocol-version", "p1",
             "--gate-set-version", "g1"], capture_output=True, text=True, check=True)
        lifecycle_journal.append_event(root, "human_effort", {"notes": "unattended"})
        lifecycle_journal.append_event(
            root, "human_effort", {"phase": "critique", "person_minutes": 95})
        run = json.loads((root / "run.json").read_text(encoding="utf-8"))
        effort = run["human_effort"]
        self.assertEqual(effort["notes"], "unattended")
        self.assertNotIn(None, effort["person_minutes_by_phase"])
        self.assertEqual(effort["person_minutes_by_phase"]["critique"], 95)
        json.dumps(run, sort_keys=True)  # the crash this guards against

    def test_finalize_distinguishes_a_documented_absence(self) -> None:
        self.assertIn("the run documents why", FINALIZE)
        self.assertIn("and no note explaining the", FINALIZE)

    def test_finalize_still_warns_either_way(self) -> None:
        # The programme's numerator is still missing. Documenting the absence explains it; it does
        # not supply it, and the warning must keep saying so.
        start = FINALIZE.index("if not vals:")
        block = FINALIZE[start:FINALIZE.index("# ---- the settled-facts lever", start)]
        self.assertEqual(block.count("numerator"), 2, block)


if __name__ == "__main__":
    unittest.main()
