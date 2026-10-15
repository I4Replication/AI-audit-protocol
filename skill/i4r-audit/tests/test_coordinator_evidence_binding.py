"""A deposited file the five roles never cited was uncitable by the coordinator.

The coordinator's snapshot includes ``package_manifest.json`` -- it is in
``REQUIRED_AUDIT_CONTEXT`` -- and its prompt says the snapshot is read-only and exhaustive. But
``known_evidence`` assembled the bound set from the role reports, the draft rows' own
``source_hashes`` and the safe-evidence payloads only. A deposited file that no role happened to
bind therefore had no admissible (path, sha256) pair, and citing it failed the attempt.

That is backwards for exactly the rows the coordinator is required to write. A tested negative
says "I looked at this file and found nothing there"; the file it looked at is by definition one
no finding rests on. On the dart-throwing audit this discarded a clean 25-minute coordinator
session -- four complete outputs, 49 rows -- over one such citation in one tested-negative row,
where the hash the model supplied was the file's true hash, read from the manifest it was given.

The rule being enforced is "never invent an evidence hash", and it survives intact: the manifest's
digests are computed by ``init_run.sh`` from the frozen read-only ``pkg/`` tree, so they are
stronger provenance than a role's assertion, and a citation must still match one exactly.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import run_coordinator  # noqa: E402


class DepositedFilesAreCitable(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        body = b"subject,score\n1,10\n"
        self.digest = hashlib.sha256(body).hexdigest()
        deposited = self.root / "pkg/dataset"
        deposited.mkdir(parents=True)
        (deposited / "df_exploratory.xlsx").write_bytes(body)
        (self.root / "package_manifest.json").write_text(json.dumps({
            "schema_version": "2.0.0", "algorithm": "sha256", "file_count": 1,
            "files": [{"path": "dataset/df_exploratory.xlsx", "size": len(body),
                       "sha256": self.digest, "extension": "xlsx"}],
        }))

    def bound(self, draft_rows=()):
        # safe_paths[0] is the manifest itself and is skipped by known_evidence.
        pairs, paths, _canonical = run_coordinator.known_evidence(
            list(draft_rows), {"report/01_reproduction.md": "a" * 64}, self.root,
            [self.root / "work/safe_evidence/manifest.json"])
        return pairs, paths

    def test_a_deposited_file_no_role_cited_is_bound(self) -> None:
        pairs, paths = self.bound()
        self.assertIn(("pkg/dataset/df_exploratory.xlsx", self.digest), pairs)
        self.assertIn("pkg/dataset/df_exploratory.xlsx", paths)

    def test_the_wrong_hash_for_that_file_is_still_rejected(self) -> None:
        # Negative control. Admitting the manifest must not admit any hash for the path -- the
        # point of the rule is that the digest has to be the real one.
        pairs, _ = self.bound()
        self.assertNotIn(("pkg/dataset/df_exploratory.xlsx", "b" * 64), pairs)

    def test_an_undeposited_path_is_still_rejected(self) -> None:
        pairs, paths = self.bound()
        self.assertNotIn("pkg/dataset/invented.xlsx", paths)
        self.assertFalse([p for p in pairs if "invented" in p[0]])

    def test_the_manifest_path_convention_matches_the_draft_rows(self) -> None:
        # Manifest paths are deposit-relative, every other binding is audit-relative. Getting this
        # wrong would bind "dataset/df_exploratory.xlsx", which no citation would ever match, and
        # the test above would still pass on the pairs set alone.
        _, paths = self.bound()
        self.assertNotIn("dataset/df_exploratory.xlsx", paths)

    def test_role_bindings_and_report_hashes_survive(self) -> None:
        row = {"evidence": {"source_hashes": [{"path": "pkg/R codes/x.R", "sha256": "c" * 64}]},
               "dependencies": {"artifact_paths": ["work/a1/evidence/results.json"]}}
        pairs, paths = self.bound([row])
        self.assertIn(("pkg/R codes/x.R", "c" * 64), pairs)
        self.assertIn("work/a1/evidence/results.json", paths)
        self.assertIn(("report/01_reproduction.md", "a" * 64), pairs)

    def test_a_missing_manifest_is_not_fatal(self) -> None:
        (self.root / "package_manifest.json").unlink()
        pairs, _ = self.bound()
        self.assertIn(("report/01_reproduction.md", "a" * 64), pairs)

    def test_a_corrupt_manifest_says_so(self) -> None:
        (self.root / "package_manifest.json").write_text("{not json")
        with self.assertRaises(run_coordinator.CoordinatorError) as caught:
            self.bound()
        self.assertIn("package_manifest.json", str(caught.exception))



class TheSnapshotPathTheModelIsGivenIsCitable(unittest.TestCase):
    """The coordinator must accept the report path its own sandbox presents.

    The snapshot mounts the five Phase-1 reports at `reports/` (plural) and the runtime prompt
    says to "read every file under ../source/reports". `report_hashes` is keyed audit-relative,
    `report/...` (singular). A model that cited what it could see was failed with "cites an
    unbound evidence hash: reports/01_reproduction.md" -- a correct citation rejected for using
    the location it was handed. This happened on a real audit.
    """

    MANIFEST = [
        {"kind": "phase1_report", "origin": "report/01_reproduction.md",
         "snapshot": "reports/01_reproduction.md", "sha256": "a" * 64},
        {"kind": "phase1_report", "origin": "report/02_data_integrity.md",
         "snapshot": "reports/02_data_integrity.md", "sha256": "b" * 64},
        {"kind": "safe_evidence", "origin": "work/safe_evidence/privacy_scan.json",
         "snapshot": "evidence/work/safe_evidence/privacy_scan.json", "sha256": "e" * 64},
        {"kind": "audit_context", "origin": "run.json",
         "snapshot": "context/run.json", "sha256": "f" * 64},
    ]

    def known(self, report_hashes, manifest=None):
        with tempfile.TemporaryDirectory() as tmp:
            return run_coordinator.known_evidence(
                [], report_hashes, pathlib.Path(tmp), [],
                self.MANIFEST if manifest is None else manifest)

    def row(self, path):
        return {"evidence": {"source_hashes": [{"path": path, "sha256": "a" * 64}]},
                "dependencies": {"artifact_paths": [path]}}

    def test_the_bound_set_stays_strictly_audit_relative(self) -> None:
        """A snapshot path must NOT become citable: findings.jsonl is read against the audit root."""
        _, paths, _ = self.known({"report/01_reproduction.md": "a" * 64})
        self.assertIn("report/01_reproduction.md", paths)
        self.assertNotIn("reports/01_reproduction.md", paths)

    def test_the_snapshot_spelling_maps_back_to_the_audit_path(self) -> None:
        _, _, canonical = self.known({"report/01_reproduction.md": "a" * 64,
                                      "work/safe_evidence/privacy_scan.json": "e" * 64})
        self.assertEqual(canonical["reports/01_reproduction.md"], "report/01_reproduction.md")
        self.assertEqual(canonical["evidence/work/safe_evidence/privacy_scan.json"],
                         "work/safe_evidence/privacy_scan.json")

    def test_nothing_unbound_gets_a_mapping(self) -> None:
        """context/run.json is in the snapshot but is not evidence; it must not become citable."""
        _, _, canonical = self.known({"report/01_reproduction.md": "a" * 64})
        self.assertNotIn("context/run.json", canonical)
        self.assertNotIn("reports/02_data_integrity.md", canonical)

    def test_a_citation_is_rewritten_to_the_audit_path(self) -> None:
        rows = [self.row("reports/01_reproduction.md")]
        changed = run_coordinator.canonicalize_citations(
            rows, {"reports/01_reproduction.md": "report/01_reproduction.md"})
        self.assertTrue(changed)
        self.assertEqual(rows[0]["evidence"]["source_hashes"][0]["path"],
                         "report/01_reproduction.md")
        self.assertEqual(rows[0]["dependencies"]["artifact_paths"], ["report/01_reproduction.md"])

    def test_an_already_canonical_citation_is_left_alone(self) -> None:
        rows = [self.row("report/01_reproduction.md")]
        self.assertFalse(run_coordinator.canonicalize_citations(rows, {"reports/x": "report/x"}))
        self.assertEqual(rows[0]["evidence"]["source_hashes"][0]["path"],
                         "report/01_reproduction.md")

    def test_the_prompt_states_the_spelling_rule(self) -> None:
        """It was enforced and never expressed; that is what cost two coordinator attempts."""
        prompt = run_coordinator.build_prompt("audit-x", "inv-x", "0" * 64)
        self.assertIn("`origin`", prompt)
        self.assertIn("not `reports/01_reproduction.md`", prompt)


if __name__ == "__main__":
    unittest.main()
