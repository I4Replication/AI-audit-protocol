"""Three projection defects in the metadata-2.1 journal, each reproduced through the CLI.

review_scope lists: `changed_claim_ids=F2,F7` -- the comma form log_event.sh's own examples use
for lists -- was projected with list(str) into five one-character ids, which the ordered-union
gate at finalize then rejected with no hint why.

Non-finite numbers: parse_kv's float() accepted nan/inf, json.dumps wrote a bare NaN into the
append-only journal, the strict reader then refused every later load, and jq read it as null.

Phase notes: the 2.1 projection dropped payload.notes on phase_start/phase_end; the legacy path
also substituted null for any phase under two seconds, contradicting run.schema.json.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
import lifecycle_journal  # noqa: E402
from tests.test_metadata_2_1_lifecycle import base_run  # noqa: E402


class JournalProjectionFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-projection-")
        self.root = pathlib.Path(self.tmp.name)
        lifecycle_journal.initialize(self.root, base_run())

    def tearDown(self):
        self.tmp.cleanup()

    def log(self, *args):
        return subprocess.run(
            ["/bin/bash", str(SCRIPTS / "log_event.sh"), "--run", str(self.root / "run.json"), *args],
            text=True, capture_output=True, timeout=60, env=dict(os.environ, LC_ALL="C"))

    def run_json(self):
        return json.loads((self.root / "run.json").read_text())

    def test_review_scope_comma_form_is_split_like_every_other_list(self):
        r = self.log("review_scope", "changed_claim_ids=F2,F7", "rereviewed_claim_ids=F2,F7",
                     "unresolved_blocking_item_ids=", "unresolved_conclusion_critical_item_ids=",
                     "unreviewed_sections=")
        self.assertEqual(0, r.returncode, r.stderr)
        run = self.run_json()
        self.assertEqual(["F2", "F7"], run["changed_claim_ids"])
        self.assertEqual([], run["unresolved_blocking_item_ids"])
        # and the JSON form still works
        r = self.log("review_scope", 'changed_claim_ids:=["F1","F10"]', 'rereviewed_claim_ids:=["F1","F10"]',
                     "unresolved_blocking_item_ids:=[]", "unresolved_conclusion_critical_item_ids:=[]",
                     "unreviewed_sections:=[]")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual(["F1", "F10"], self.run_json()["changed_claim_ids"])

    def test_non_finite_values_stay_strings_and_the_journal_stays_loadable(self):
        for raw in ("nan", "inf", "-inf", "Infinity"):
            r = self.log("note", f"text={raw}", f"value={raw}")
            self.assertEqual(0, r.returncode, r.stderr)
        journal = (self.root / "run.events.jsonl").read_text()
        self.assertNotIn("NaN", journal)
        self.assertNotIn("Infinity\n", journal.replace('"Infinity"', ""))
        # the strict reader the rest of the skill uses must accept every row
        sys.path.insert(0, str(SKILL / "schemas" / "tools"))
        import validate_json_schema
        for line in journal.splitlines():
            validate_json_schema.strict_loads(line, source="journal")
        # and a real number is still a number
        self.assertEqual(0, self.log("tokens", "role=critic_stats", "total_tokens=12").returncode)

    def test_phase_notes_are_projected_and_short_phases_record_zero(self):
        self.assertEqual(0, self.log("phase_start", "phase=intake", "at=2026-09-01T10:00:00Z",
                                     "notes=backfilled from setup_workspace timestamps").returncode)
        self.assertEqual(0, self.log("phase_end", "phase=intake", "at=2026-09-01T10:00:00Z").returncode)
        phase = self.run_json()["phases"]["intake"]
        self.assertEqual("backfilled from setup_workspace timestamps", phase["notes"])
        self.assertEqual(0, phase["wall_clock_seconds"], "derived whole seconds, minimum 0, per the schema")


if __name__ == "__main__":
    unittest.main()
