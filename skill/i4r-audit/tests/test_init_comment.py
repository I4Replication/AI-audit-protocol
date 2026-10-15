"""init_comment.py must emit a schema-valid comment.json skeleton and refuse bad facts."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import init_comment  # noqa: E402
import validate_json_schema  # noqa: E402

SCHEMA = SKILL / "schemas" / "comment.schema.json"


class InitCommentSkeleton(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        (self.root / "run.json").write_text(json.dumps({
            "audit_id": "20260901-audit-test0001", "paper_doi": "10.1000/example.2025.1",
            "paper_title": "An example", "protocol_version": "2.1.0",
        }))
        (self.root / "work").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_skeleton_with_no_facts_is_schema_valid(self) -> None:
        rc = init_comment.main([str(self.root)])
        self.assertEqual(rc, 0)
        record = json.loads((self.root / "comment.json").read_text())
        self.assertEqual(validate_json_schema.errors_for_value(record, SCHEMA), [])
        self.assertEqual(record["findings"], [])
        self.assertFalse(record["safe_to_publish"])
        self.assertEqual(record["bibliographic"]["doi"], "10.1000/example.2025.1")
        self.assertEqual(record["rollup"]["findings_total"], 0)
        self.assertEqual(record["author_response"]["status"], "not_notified")

    def test_facts_are_merged_and_validated(self) -> None:
        (self.root / "work/facts.json").write_text(json.dumps({
            "bibliographic": {"journal": "J", "year": 2025, "field": "f"},
            "design": {"type": "cross_sectional"},
            "sample": {"n": 131, "unit_of_observation": "participant", "human_subjects": True},
            "package": {"data_present": True, "code_present": True, "runs_as_shipped": "no"},
        }))
        rc = init_comment.main([str(self.root), "--facts", "work/facts.json"])
        self.assertEqual(rc, 0)
        record = json.loads((self.root / "comment.json").read_text())
        self.assertEqual(validate_json_schema.errors_for_value(record, SCHEMA), [])
        self.assertEqual(record["sample"]["n"], 131)
        self.assertEqual(record["package"]["runs_as_shipped"], "no")

    def test_unknown_fact_key_is_rejected(self) -> None:
        (self.root / "work/facts.json").write_text(json.dumps({"sample": {"n": 1, "typo_key": 2}}))
        with self.assertRaises(SystemExit):
            init_comment.main([str(self.root), "--facts", "work/facts.json"])
        self.assertFalse((self.root / "comment.json").exists())

    def test_existing_file_is_not_overwritten_without_force(self) -> None:
        (self.root / "comment.json").write_text("{}")
        self.assertEqual(init_comment.main([str(self.root)]), 2)
        self.assertEqual((self.root / "comment.json").read_text(), "{}")
        self.assertEqual(init_comment.main([str(self.root), "--force"]), 0)
        self.assertNotEqual((self.root / "comment.json").read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
