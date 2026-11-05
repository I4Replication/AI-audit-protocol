"""2.7.11: a review_scope id list typed as JSON text is refused instead of mis-projected.

Audit A63 logged review_scope with `key='[...]'` (`=` where `:=` was meant).
log_event.sh accepted it and the projection split the text on commas: unresolved_blocking_item_ids
'[]' became ["[]"] -- one phantom unresolved blocking item on the audit -- and the changed ids
became '["F1"', '"F2"', ... The operator noticed and re-logged with typed arrays; the
journal is append-only, so the bad event stays. The same slip is in an archived 2.4.11 audit.

Runs the real init_run.sh and log_event.sh on a scratch audit. On 2.7.10 the first test fails: the
event is journaled (exit 0) and run.json carries ["[]"].
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
LIST_KEYS = ("changed_claim_ids", "rereviewed_claim_ids", "unresolved_blocking_item_ids",
             "unresolved_conclusion_critical_item_ids", "unreviewed_sections")


class ReviewScopeLists(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-2711-scope-")
        self.audit = pathlib.Path(self.tmp.name) / "a"
        (self.audit / "pkg").mkdir(parents=True)
        (self.audit / "pkg/f.csv").write_text("x\n")
        model = json.loads((SKILL / "config/pipeline-policy-v2.json").read_text())["models"]["coordinator"][
            "requested_model"]
        subprocess.run(["bash", str(SCRIPTS / "init_run.sh"), str(self.audit), "--audit-id", "t-2711",
                        "--doi", "10.1234/abc", "--auditor-model", model, "--auditor-family", "claude",
                        "--auditor-provider", "anthropic", "--protocol-version", "p1", "--gate-set-version", "g1"],
                       check=True, capture_output=True, text=True)
        self.env = {**os.environ, "RUN_JSON": str(self.audit / "run.json")}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def log(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPTS / "log_event.sh"), "review_scope", *args],
                              env=self.env, capture_output=True, text=True)

    def journal(self) -> bytes:
        return (self.audit / "run.events.jsonl").read_bytes()

    def test_json_text_is_refused_and_nothing_is_journaled(self) -> None:
        before = self.journal()
        result = self.log('changed_claim_ids=["F1", "F2"]', 'rereviewed_claim_ids=["F1", "F2"]',
                          "unresolved_blocking_item_ids=[]", "unresolved_conclusion_critical_item_ids=[]",
                          "unreviewed_sections=[]")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(":=", result.stderr)
        self.assertEqual(self.journal(), before)
        run = json.loads((self.audit / "run.json").read_text())
        self.assertNotIn("[]", run.get("unresolved_blocking_item_ids") or [])

    def test_each_list_key_alone_is_refused_as_json_text(self) -> None:
        for key in LIST_KEYS:
            for text in ("[]", '["F1"]', '{"a": 1}', '"F1"', "  [ ]"):
                with self.subTest(key=key, text=text):
                    before = self.journal()
                    result = self.log(*[f"{other}:=[]" for other in LIST_KEYS if other != key], f"{key}={text}")
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(f"{key}:='[...]'", result.stderr)
                    self.assertIn(f"{key}=F1,F2", result.stderr)
                    self.assertEqual(self.journal(), before)

    def test_a_batch_line_and_publication_redaction_are_judged_too(self) -> None:
        batch = pathlib.Path(self.tmp.name) / "events.jsonl"
        batch.write_text('{"type":"note","payload":{"text":"would land"}}\n'
                         + json.dumps({"type": "review_scope", "payload": {
                             **{key: [] for key in LIST_KEYS}, "unresolved_blocking_item_ids": "[]"}}) + "\n")
        before = self.journal()
        result = subprocess.run(["bash", str(SCRIPTS / "log_event.sh"), "--batch", str(batch)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.journal(), before)
        result = subprocess.run(["bash", str(SCRIPTS / "log_event.sh"), "publication_redaction",
                                 'fields_requiring_removal=["/notes"]', "note=x"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("fields_requiring_removal:='[...]'", result.stderr)
        self.assertEqual(self.journal(), before)

    def test_typed_arrays_and_comma_lists_still_project(self) -> None:
        result = self.log("changed_claim_ids:=[\"F1\",\"F2\"]", "rereviewed_claim_ids=F1,F2",
                          "unresolved_blocking_item_ids:=[]", "unresolved_conclusion_critical_item_ids:=[]",
                          "unreviewed_sections:=[]")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run = json.loads((self.audit / "run.json").read_text())
        self.assertEqual(run["changed_claim_ids"], ["F1", "F2"])
        self.assertEqual(run["rereviewed_claim_ids"], ["F1", "F2"])
        self.assertEqual(run["unresolved_blocking_item_ids"], [])


if __name__ == "__main__":
    unittest.main()
