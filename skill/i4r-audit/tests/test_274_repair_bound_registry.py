"""2.7.4: a Phase-1 role completed by the focused output repair reaches the draft registry.

``run_phase1.try_focused_repair`` runs the repair in ``phase1/roles/<role>/<input_sha256>/repair_<uuid>/``,
beside the failed sample's ``attempt_NN_<uuid>/``, and the quorum binds the repair's invocation.
``build_draft_registry.py`` searched ``*/attempt_*/status.json`` only, so the first audit to complete a role
that way (20260930-audit-0ccfcb4e, auditor_code_methods) stopped before the coordinator with
"FATAL: no bound result.json".

Nothing here is written by hand: the real Phase-1 runner runs the fake CLI's ``repairable_output`` scenario
(the reproduction auditor's first sample fails only structurally and one focused repair completes it), and
the real builder, the coordinator's binding check and a second runner pass then run on that tree.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "schemas/tools"))
sys.path.insert(0, str(SKILL / "tests"))
import build_draft_registry  # noqa: E402
import record_invocation  # noqa: E402
import run_coordinator  # noqa: E402
import test_run_phase1  # noqa: E402  (the Phase-1 audit fixture and fake CLI)

ROLE = "auditor_reproduction"
ROLE_SHORT = build_draft_registry.ROLE_SHORT[ROLE]


def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


class RepairCompletedRole(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = test_run_phase1.Phase1RunnerTests(
            "test_normal_path_is_one_probe_plus_five_parallel_first_try_auditors")
        cls.fixture.setUp()
        # Resolved: the ledger validator resolves every output_artifact against the audit root, and
        # a macOS temporary directory sits behind the /var -> /private/var symlink.
        cls.root = cls.completed_audit("repairbound")

    @classmethod
    def state(cls, root: pathlib.Path) -> pathlib.Path:
        # One fake-CLI call counter per audit: the scenario breaks only a role's FIRST sample, so a
        # shared counter would give a second audit no repair at all.
        return cls.fixture.base / f"{root.name}-fake-state.jsonl"

    @classmethod
    def completed_audit(cls, name: str) -> pathlib.Path:
        root = cls.fixture.make_audit(name, metadata21=True).resolve()
        result = cls.fixture.run_runner("repairable_output", root=root, state=cls.state(root), target=ROLE)
        if result.returncode != 0:
            cls.fixture.tearDown()
            raise AssertionError(result.stderr + result.stdout)
        return root

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.tearDown()

    def copy(self, name: str) -> pathlib.Path:
        target = (self.fixture.base / name).resolve()
        shutil.copytree(self.root, target, symlinks=True)
        return target

    def binding(self, root: pathlib.Path) -> dict:
        quorum = read_json(root / "phase1/quorum.json")
        self.assertEqual(quorum["status"], "quorum_complete")
        return next(entry for entry in quorum["roles"] if entry["role"] == ROLE)

    def repair_dirs(self, root: pathlib.Path) -> list[pathlib.Path]:
        return sorted((root / "phase1/roles" / ROLE).glob("*/repair_*"))

    def build(self, root: pathlib.Path) -> tuple[list[dict], str]:
        self.assertEqual(build_draft_registry.main([str(root)]), 0)
        out = root / "work/registry_draft"
        rows = [json.loads(line) for line in (out / "draft_findings.jsonl").read_text().splitlines()]
        return rows, (out / "draft_findings.jsonl").read_text() + (out / "DRAFT_SUMMARY.md").read_text()

    def test_the_condition_is_on_disk(self) -> None:
        # Guards the fixture: without a repair-bound quorum entry every other test here is vacuous.
        entry = self.binding(self.root)
        [repair] = self.repair_dirs(self.root)
        status = read_json(repair / "status.json")
        self.assertEqual(status["invocation_id"], entry["invocation_id"])
        self.assertEqual(status["attempt_kind"], "output_repair")
        self.assertEqual(entry["source_invocation_id"],
                         read_json(repair / "input.json")["source_invocation_id"])
        [original] = sorted((self.root / "phase1/roles" / ROLE).glob("*/attempt_*"))
        self.assertEqual(read_json(original / "status.json")["state"], "failed")

    def test_the_draft_registry_is_built_from_the_repair(self) -> None:
        root = self.copy("build")
        entry = self.binding(root)
        [repair] = self.repair_dirs(root)
        rows, _ = self.build(root)
        mine = [row for row in rows if row["provenance"]["origin_role"] == ROLE]
        findings = read_json(repair / "output.partial/result.json")["scientific_fields"]["findings"]
        self.assertTrue(findings, "the repaired role must contribute findings for this test to bite")
        self.assertEqual(len(mine), len(findings))
        self.assertTrue(all(row["id"].startswith(ROLE_SHORT + "-") for row in mine))
        for row in mine:
            self.assertEqual(row["provenance"]["source_records"],
                             [f"phase1/quorum.json#{entry['invocation_id']}"])
        self.assertEqual({row["provenance"]["origin_role"] for row in rows},
                         set(build_draft_registry.ROLE_DOMAIN))

    def test_the_coordinator_binds_the_invocation_the_draft_cites(self) -> None:
        # run_coordinator reads the canonical ledger, not phase1/roles; it must name the same call.
        bindings, _ = run_coordinator.phase_one_bindings(
            self.root, run_coordinator.read_ledger(self.root),
            record_invocation.load_policy_snapshot(self.root))
        bound = next(item["invocation_id"] for item in bindings if item["role"] == ROLE)
        self.assertEqual(bound, self.binding(self.root)["invocation_id"])
        rows, _ = self.build(self.copy("coordinator"))
        cited = {row["provenance"]["source_records"][0] for row in rows
                 if row["provenance"]["origin_role"] == ROLE}
        self.assertEqual(cited, {f"phase1/quorum.json#{bound}"})

    def test_a_rerun_reuses_the_repaired_role_and_rebuilds_the_same_bytes(self) -> None:
        # In place, on an audit of its own: a moved audit re-runs its capability probe.
        root = self.completed_audit("rerun")
        [repair] = self.repair_dirs(root)
        first = self.binding(root)["invocation_id"]
        self.assertEqual(read_json(repair / "status.json")["invocation_id"], first)
        _, before = self.build(root)
        state = self.state(root)
        starts = len(self.fixture.starts(state))
        result = self.fixture.run_runner("repairable_output", root=root, state=state, target=ROLE)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(len(self.fixture.starts(state)), starts, "a completed audit must not dispatch again")
        entry = self.binding(root)
        self.assertTrue(entry["reused"])
        self.assertEqual(entry["invocation_id"], first)
        _, after = self.build(root)
        self.assertEqual(after, before)

    def test_a_result_changed_after_promotion_is_refused(self) -> None:
        root = self.copy("tampered")
        [repair] = self.repair_dirs(root)
        result = repair / "output.partial/result.json"
        result.chmod(0o644)
        result.write_text(result.read_text() + "\n", encoding="utf-8")  # still valid JSON
        with self.assertRaises(SystemExit) as caught:
            build_draft_registry.main([str(root), "--dry-run"])
        self.assertIn("does not hash to the result_sha256", str(caught.exception))

    def test_two_directories_claiming_the_invocation_are_refused(self) -> None:
        root = self.copy("ambiguous")
        [repair] = self.repair_dirs(root)
        shutil.copytree(repair, repair.with_name("repair_00000000-0000-0000-0000-000000000000"))
        with self.assertRaises(SystemExit) as caught:
            build_draft_registry.main([str(root), "--dry-run"])
        self.assertIn("2 attempt directories", str(caught.exception))

    def test_the_failed_sample_is_never_a_fallback(self) -> None:
        # Negative control: the failed attempt_* holds the same scientific fields (the repair
        # byte-freezes them), so only the invocation id can tell them apart. Without the repair's
        # own result the builder must refuse, not read the sample the quorum does not bind.
        root = self.copy("norepair")
        [repair] = self.repair_dirs(root)
        (repair / "output.partial/result.json").unlink()
        with self.assertRaises(SystemExit) as caught:
            build_draft_registry.main([str(root), "--dry-run"])
        self.assertIn("no bound result.json", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
