"""The packaged 2.6.4 post-fan-out verifier, run on disk with the real runner.

The Neudorfer audit (20260923-audit-3309cfcf) excluded three true article-text findings, F13-F15,
because nothing could supply their family-A (Claude) confirmation: they were raised after Phase 1,
and the only packaged Claude confirmations were the five Phase-1 auditors'. Hand-made
verification records were refused by make_settled (role_class other, recorded after round-1
integration) and then by finalize (a record relabelled as an auditor counted as a sixth auditor).

scripts/run_verifier.py is the packaged role that closes that gap. Every test here builds a
metadata-2.1 audit, runs the real Phase-1 runner to quorum with the fake Claude CLI, writes a
schema-valid registry, and then runs the real verifier as a subprocess. Every positive check has
a negative control, and every refusal is shown to make zero provider calls and zero journal
events.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SKILL / "tests"))



def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

class _Lazy:
    """Resolve a scripts module at use, never at import.

    Several suites replace sys.modules entries (phase1_runtime, run_phase1) while the discoverer
    imports them. Importing run_coordinator or run_phase1 here, ahead of them alphabetically,
    would bind those modules to a phase1_runtime the later suites no longer share.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attribute: str):
        return getattr(importlib.import_module(self._name), attribute)


lifecycle_journal = _Lazy("lifecycle_journal")
phase1_runtime = _Lazy("phase1_runtime")
record_invocation = _Lazy("record_invocation")
run_coordinator = _Lazy("run_coordinator")
run_phase1 = _Lazy("run_phase1")
summarize_invocations = _Lazy("summarize_invocations")
verifier_contract = _Lazy("verifier_contract")

VERIFIER_EXTRA_DENIES = (
    "coordinator", "SETTLED.json", "SETTLED.md", "comment.json", "claims_map.tsv",
    "verification_snippets", "WRAPUP.txt",
)
RECEIPT_KEYS = {
    "schema_version", "status", "role", "input_sha256", "source_snapshot_sha256", "packet_sha256",
    "invocation_id", "attempt", "attempt_kind", "source_invocation_id", "result", "result_sha256",
    "report", "report_sha256", "evidence", "evidence_manifest_sha256", "hypotheses",
    "source_bindings",
}
# Strings that live only in fields the blind packet must withhold.
WITHHELD_SENTINELS = (
    "EXPECTED_SENTINEL", "OBSERVED_SENTINEL", "TOLERANCE_SENTINEL", "RERUN_SENTINEL",
    "BENIGN_SENTINEL", "REMEDY_SENTINEL", "critic-inv-SENTINEL", "COORDINATOR_NOTES",
    "critic_completeness", "coordinator_approved", "ledger_disposition", "verification_tier",
    "changes_conclusion", "provenance",
)
CLAIMS = {
    "F1": "Auditor-raised: the deposited script drops two rows before fitting.",
    "F2": "Excluded row: the abstract rounds the sample size.",
    "F13": "The article reports r = 0.91 for the chest strap in Table 2.",
    "F14": "The deposited data.csv holds three participants, contradicting the reported sample.",
    "F15": "The limitations paragraph omits the device firmware version.",
    "F16": "A claim whose only cited source is the generated report.",
    "F17": "A claim an auditor independently found as well.",
}

# 2.6.3 values, computed by the pristine 2.6.3 scripts on the fixed fixture in
# AuditorInputsArePinnedTo263 below. Any change to what feeds an auditor's role input hash or the
# probe key would re-dispatch all five auditors on every live audit.
PINNED_263 = {
    "probe_key": "c476bb26732c6e9a7bf5cdd7aeda08e68bfbb7e228fd2f5f85c0ae9eafc8dcfa",
    "sandbox_policy_sha256": "b16f9de61ac475a57a9c13664023c94e317f5e417a7946416681e65829238963",
    "sandbox_settings_sha256": "d8107c8c69a6148944fec3d7af7a5a3b12785eebd4f13259fbf772fd0d50b883",
    "roles": {
        "auditor_code_methods": "9ff43980e70747de2bca730c7b7e0bea055ee7e56185df07dae26e9c40f816f7",
        "auditor_data_integrity": "a5106252166f6a439ff2a25e5a6eac75bb1c24198b9589149a9fc74fc1b94c22",
        "auditor_privacy_archive": "ef0e60fcffeeaf88bea6245cafac21ecd5321c20ff918c63b3fddca4f4037ae6",
        "auditor_reproduction": "0da28d09acccdb8b4ea1cf63dd783f48daa914b67bb3d8e5274c0753b6e1da7c",
        "auditor_text_vs_data": "6be9fc82c9b4ca56e0b5c05520f49fdf8a4c5ced9069e5321b24d06c5413f689",
    },
}


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_route(root: pathlib.Path) -> dict:
    """The verifier route this audit's own snapshot froze (2.7.0: Sonnet 5.5 for an audit made from
    the shipped policy, Sonnet 5 for one initialised before) and the id the fake serves it."""
    route = verifier_contract.verifier_route(verifier_contract.load_snapshot_policy(root))
    served = importlib.import_module("fake_claude_phase1").SERVED_BY_MODEL[route["spawn_alias"]]
    return {**route, "served_model": served}


class VerifierFixture(unittest.TestCase):
    """An audit that has completed Phase 1 through the real runner, plus a registry."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-verifier-test-")
        self.base = pathlib.Path(self.temporary.name).resolve()
        self.fake = self.base / "fake-claude-phase1"
        shutil.copyfile(SKILL / "tests/fake_claude_phase1.py", self.fake)
        self.fake.chmod(0o755)
        self.state = self.base / "fake-state.jsonl"

    def tearDown(self) -> None:
        for path in self.base.rglob("*"):
            try:
                if not path.is_symlink():
                    path.chmod(0o700 if path.is_dir() else 0o600)
            except OSError:
                pass
        self.temporary.cleanup()

    # ---------------------------------------------------------------------------- building
    def make_audit(self, name: str = "audit", *, metadata21: bool = True,
                   audit_id: str | None = None) -> pathlib.Path:
        root = self.base / name
        for directory in ("paper", "pkg", "report", "work", "critique"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        (root / "paper/paper.txt").write_text(
            "Synthetic paper text for a bounded reproducibility fixture. " * 80 + "\n",
            encoding="utf-8",
        )
        (root / "pkg/data.csv").write_text(
            "participant,value,group\nparticipant_A1,1,A\nparticipant_A2,,B\n"
            "participant_A2,,B\ncontact@example.org,3,A\n",
            encoding="utf-8",
        )
        (root / "pkg/analysis.py").write_text(
            "if __name__ == '__main__':\n    print('aggregate analysis')\n", encoding="utf-8",
        )
        (root / "pkg/LICENSE").write_text("Synthetic permissive licence.\n", encoding="utf-8")
        (root / "pkg/binary-input.bin").write_bytes(b"x" * 120_000)
        rows = []
        for path in sorted((root / "pkg").rglob("*")):
            if path.is_file():
                rows.append({
                    "path": path.relative_to(root / "pkg").as_posix(), "size": path.stat().st_size,
                    "sha256": digest(path), "extension": path.suffix.lower().lstrip(".") or "<none>",
                })
        (root / "package_manifest.json").write_text(json.dumps({
            "schema_version": "2.0.0", "algorithm": "sha256",
            "package_sha256": hashlib.sha256("".join(
                f"{row['path']}\0{row['sha256']}\n" for row in rows).encode()).hexdigest(),
            "file_count": len(rows), "file_types": {}, "files": rows,
        }, indent=2) + "\n", encoding="utf-8")
        record_invocation.initialize_policy_snapshot(root)
        (root / "run.json").write_text(json.dumps({
            "schema_version": "2.0.0", "audit_id": audit_id or f"audit-{name}", "normalization": None,
            "toolkit": {"pipeline_policy_snapshot_sha256": digest(root / "pipeline-policy.snapshot.json")},
        }, indent=2) + "\n", encoding="utf-8")
        (root / "invocations.jsonl").write_text("", encoding="utf-8")
        if metadata21:
            lifecycle_journal.initialize(root)
        return root

    def env(self, scenario: str, target: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("I4R_MAX_ATTEMPTS", None)
        env.update({
            "FAKE_PHASE1_SCENARIO": scenario, "FAKE_PHASE1_TARGET": target,
            "FAKE_PHASE1_STATE": str(self.state), "PYTHONDONTWRITEBYTECODE": "1",
        })
        env.update(extra or {})
        return env

    def run_phase1(self, root: pathlib.Path, scenario: str = "success", *,
                   target: str = "auditor_reproduction", extra_env: dict[str, str] | None = None,
                   ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "run_phase1.py"), str(root), "--claude-bin", str(self.fake)],
            text=True, capture_output=True, env=self.env(scenario, target, extra_env), timeout=120,
            check=False,
        )

    def run_verifier(self, root: pathlib.Path, findings: list[str], scenario: str = "success", *,
                     target: str = "verifier", extra_env: dict[str, str] | None = None,
                     extra_args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "run_verifier.py"), str(root), "--findings", *findings,
             "--claude-bin", str(self.fake), *(extra_args or [])],
            text=True, capture_output=True, env=self.env(scenario, target, extra_env), timeout=120,
            check=False,
        )

    def audit_after_phase1(self, name: str = "audit", *, metadata21: bool = True,
                           audit_id: str | None = None) -> pathlib.Path:
        root = self.make_audit(name, metadata21=metadata21, audit_id=audit_id)
        # slow_parallel gives the five fake auditors the positive common overlap a real fan-out
        # has, so summarize_invocations' strict Phase-1 evidence check applies unchanged.
        result = self.run_phase1(root, "slow_parallel")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.write_registry(root)
        return root

    def snapshot_manifest(self, root: pathlib.Path) -> dict:
        quorum = json.loads((root / "phase1/quorum.json").read_text())
        return json.loads(
            (root / "phase1/snapshots" / quorum["source_snapshot_sha256"] / "snapshot_manifest.json").read_text()
        )

    def registry_row(self, fid: str, *, origin: str = "operator", found_by: tuple[str, ...] = ("operator",),
                     status: str = "INCLUDED", sources: list[dict] | None = None,
                     round_introduced: int | None = 0) -> dict:
        return {
            "schema_version": "2.0.0",
            "id": fid,
            "claim": CLAIMS[fid],
            "domain": "text",
            "source_location": {"path": "paper/paper.txt", "locator": "Table 2, second row"},
            "claim_ref": None,
            "status": "draft",
            "ledger_disposition": {
                "status": status, "target": None,
                "reason": None if status == "INCLUDED" else "Excluded for the fixture.",
            },
            "coordinator_approved": False,
            "impact": "value_wrong",
            "impact_rank": 2,
            "verification_tier": 1,
            "paper_only": True,
            "confidence": "LIKELY",
            "evidence": {
                "source_hashes": sources or [], "rerun_command": "python3 RERUN_SENTINEL.py",
                "expected": "EXPECTED_SENTINEL", "observed": "OBSERVED_SENTINEL",
                "tolerance_basis": "TOLERANCE_SENTINEL",
            },
            "unverifiable_basis": None,
            "verifications": [{
                "reviewer_id": "critic_completeness", "invocation_id": "critic-inv-SENTINEL",
                "evidence_record": {"path": "critique/round_1/critic_completeness.json", "sha256": "a" * 64},
                "model_family": "gpt", "outcome": "confirmed",
            }],
            "dependencies": {"environment_fingerprint": None, "finding_ids": [], "artifact_paths": []},
            "settled": None,
            "benign_explanations_ruled_out": ["BENIGN_SENTINEL"],
            "report_section": None,
            "verification_snippet": None,
            "disclosure_flag": False,
            "remedy": {"class": "one_number", "description": "REMEDY_SENTINEL", "verified": None},
            "changes_conclusion": False,
            "changes_conclusion_which": None,
            "provenance": {
                "origin_role": origin, "independently_found_by": list(found_by),
                "agreed_after_seeing": [], "round_introduced": round_introduced,
                "source_records": ["work/registry_draft/COORDINATOR_NOTES.md#N1"],
            },
        }

    def write_registry(self, root: pathlib.Path) -> None:
        known = {row["path"]: row["sha256"] for row in self.snapshot_manifest(root)["source_files"]}
        paper = {"path": "paper/paper.txt", "sha256": known["paper/paper.txt"]}
        data = {"path": "pkg/data.csv", "sha256": known["pkg/data.csv"]}
        stale = {"path": "pkg/data.csv", "sha256": "9" * 64}
        generated = {"path": "report/REPORT_final.md", "sha256": "8" * 64}
        rows = [
            self.registry_row("F1", origin="auditor_text_vs_data", found_by=("auditor_text_vs_data",),
                              sources=[paper]),
            self.registry_row("F2", status="EXCLUDED", sources=[paper]),
            self.registry_row("F13", sources=[paper]),
            self.registry_row("F14", origin="coordinator", found_by=(), sources=[stale, generated, data]),
            self.registry_row("F15", origin="critic_data", found_by=("critic_data",), sources=[paper],
                              round_introduced=1),
            self.registry_row("F16", sources=[generated]),
            self.registry_row("F17", found_by=("operator", "auditor_reproduction"), sources=[paper]),
        ]
        schema = SKILL / "schemas/v2/finding-row.schema.json"
        for row in rows:
            errors = record_invocation.validate_json_schema.errors_for_value(row, schema)
            self.assertEqual(errors, [], f"fixture row {row['id']} is not schema-valid: {errors}")
        (root / "work/findings.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8",
        )

    # ---------------------------------------------------------------------------- reading
    def starts(self) -> list[dict]:
        if not self.state.exists():
            return []
        return [row for row in (json.loads(line) for line in self.state.read_text().splitlines() if line.strip())
                if row["kind"] == "start"]

    def verifier_starts(self) -> list[dict]:
        return [row for row in self.starts() if row["role"].startswith("verifier_")]

    def ledger(self, root: pathlib.Path) -> list[dict]:
        return [json.loads(line) for line in (root / "invocations.jsonl").read_text().splitlines() if line.strip()]

    def verifier_rows(self, root: pathlib.Path) -> list[dict]:
        return [row for row in self.ledger(root) if row.get("role_class") == "verifier"]

    def claim_hash(self, fid: str) -> str:
        return verifier_contract.claim_sha256(CLAIMS[fid])

    def role_for(self, *fids: str) -> str:
        return verifier_contract.batch_role(self.claim_hash(fid) for fid in fids)

    def receipt(self, root: pathlib.Path, role: str) -> tuple[pathlib.Path, dict]:
        paths = sorted((root / "phase1/completions" / role).glob("*.json"))
        self.assertEqual(len(paths), 1, paths)
        return paths[0], json.loads(paths[0].read_text())

    def journal_bytes(self, root: pathlib.Path) -> tuple[bytes, bytes, bytes]:
        return ((root / "run.events.jsonl").read_bytes() if (root / "run.events.jsonl").exists() else b"",
                (root / "invocations.jsonl").read_bytes(), (root / "run.json").read_bytes())

    def event_types(self, root: pathlib.Path) -> list[str]:
        return [event["type"] for event in lifecycle_journal.load_events(root)]

    def phase1_listing(self, root: pathlib.Path) -> list[str]:
        return sorted(path.relative_to(root).as_posix() for path in (root / "phase1").rglob("*")
                      if "probe_cache" not in path.parts)

    def verdicts(self, root: pathlib.Path) -> tuple[dict, list]:
        return verifier_contract.collect_verdicts(root, self.ledger(root))

    def assert_refused(self, result: subprocess.CompletedProcess[str], fragment: str) -> dict:
        self.assertEqual(result.returncode, 5, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "refused")
        self.assertIn(fragment, payload["reason"])
        self.assertTrue(payload["remedy"].strip(), "every refusal names its remedy")
        self.assertIn("Remedy:", result.stderr)
        return payload


class VerifierRunsUnderThePhase1Contract(VerifierFixture):
    def test_one_admissible_family_a_call_and_phase1_left_byte_identical(self) -> None:
        root = self.audit_after_phase1()
        quorum = json.loads((root / "phase1/quorum.json").read_text())
        snapshot = quorum["source_snapshot_sha256"]
        protected = [root / "phase1/quorum.json", root / "phase1/status.json",
                     root / "phase1/preflight" / snapshot / "receipt.json",
                     *sorted((root / "report").glob("0*.md"))]
        self.assertEqual(len(protected), 8)
        before = {path: path.read_bytes() for path in protected}
        ledger_lines = (root / "invocations.jsonl").read_text().splitlines()
        auditor_lines = [line for line in ledger_lines
                         if json.loads(line).get("role") in phase1_runtime.ROLE_SPECS]
        self.assertEqual(len(auditor_lines), 5)
        run_before = json.loads((root / "run.json").read_text())
        phase_events_before = [t for t in self.event_types(root)
                               if t in {"phase_start", "phase_end", "pause_start", "pause_end"}]
        probe_starts = len([row for row in self.starts() if row["role"] == "phase1_capability_probe"])

        result = self.run_verifier(root, ["F13", "F14", "F15"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        summary = json.loads(result.stdout)
        role = self.role_for("F13", "F14", "F15")
        self.assertEqual(summary["role"], role)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual({row["verdict"] for row in summary["hypotheses"]}, {"CONFIRMED"})

        # One provider call for the batch; the probe is replayed from its cache, not re-run.
        self.assertEqual(len(self.verifier_starts()), 1)
        self.assertEqual(len([row for row in self.starts() if row["role"] == "phase1_capability_probe"]),
                         probe_starts)
        rows = self.verifier_rows(root)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        route = snapshot_route(root)
        receipt_path, receipt = self.receipt(root, role)
        input_hash = receipt_path.stem
        expected_dir = verifier_contract.output_dir(role, input_hash)
        self.assertEqual({key: row[key] for key in (
            "role", "role_class", "round", "attempt", "attempt_kind", "status", "provider_call",
            "identity_assurance", "model_family", "provider", "requested_model", "reasoning_effort",
            "input_artifact_sha256", "output_artifact",
        )}, {
            "role": role, "role_class": "verifier", "round": 0, "attempt": 1,
            "attempt_kind": "role_inference", "status": "completed", "provider_call": True,
            "identity_assurance": "observed", "model_family": "claude", "provider": "anthropic",
            "requested_model": route["requested_model"], "reasoning_effort": route["reasoning_effort"],
            "input_artifact_sha256": input_hash, "output_artifact": f"{expected_dir}/result.json",
        })
        self.assertEqual(row["served_model"], route["served_model"])
        argv = self.verifier_starts()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], route["spawn_alias"])
        self.assertEqual(row["output_sha256"], digest(root / row["output_artifact"]))
        # The receipt, exactly as the spec shows it.
        self.assertEqual(set(receipt), RECEIPT_KEYS)
        packet_dirs = list((root / "phase1/role_packets" / snapshot / role).iterdir())
        self.assertEqual(len(packet_dirs), 1)
        self.assertEqual(receipt["schema_version"], verifier_contract.COMPLETION_VERSION)
        self.assertEqual((receipt["status"], receipt["role"], receipt["input_sha256"]),
                         ("completed", role, input_hash))
        self.assertEqual(receipt["source_snapshot_sha256"], snapshot)
        self.assertEqual(receipt["packet_sha256"], packet_dirs[0].name)
        self.assertEqual((receipt["invocation_id"], receipt["attempt"], receipt["attempt_kind"],
                          receipt["source_invocation_id"]),
                         (row["invocation_id"], 1, "role_inference", None))
        self.assertEqual(receipt["result"], row["output_artifact"])
        self.assertEqual(receipt["result_sha256"], row["output_sha256"])
        self.assertEqual(receipt["report"], f"{expected_dir}/REPORT.md")
        self.assertEqual(receipt["report_sha256"], digest(root / receipt["report"]))
        self.assertEqual(receipt["evidence"], f"{expected_dir}/evidence/manifest.json")
        self.assertEqual(receipt["evidence_manifest_sha256"], digest(root / receipt["evidence"]))
        self.assertEqual({(h["finding_id_at_run"], h["claim_sha256"]) for h in receipt["hypotheses"]},
                         {(fid, self.claim_hash(fid)) for fid in ("F13", "F14", "F15")})
        self.assertEqual([h["hypothesis_id"] for h in receipt["hypotheses"]], ["H1", "H2", "H3"])
        self.assertTrue(receipt["source_bindings"])
        self.assertEqual(receipt_path.relative_to(root).as_posix(),
                         verifier_contract.completion_receipt_path(role, input_hash))

        # The shared contract admits the record, and binds by claim hash.
        ledger = self.ledger(root)
        by_id = {r["invocation_id"]: r for r in ledger}
        families = verifier_contract.auditor_families(ledger)
        self.assertEqual(families, {"claude"})
        admitted = verifier_contract.verify_record(root, row, by_id, families)
        self.assertEqual(admitted, {self.claim_hash(fid): "CONFIRMED" for fid in ("F13", "F14", "F15")})
        verdicts, errors = self.verdicts(root)
        self.assertEqual(errors, [])
        registry = {r["id"]: r for r in map(json.loads, (root / "work/findings.jsonl").read_text().splitlines())}
        roles = verifier_contract.required_roles(verifier_contract.load_snapshot_policy(root))
        self.assertIsNone(verifier_contract.confirmation_problem(
            registry["F13"], row["invocation_id"], roles, verdicts))
        # Negative controls: an auditor-raised row, a tampered result, a hand-made record.
        self.assertIn("Phase-1 auditor", verifier_contract.confirmation_problem(
            registry["F1"], row["invocation_id"], roles, verdicts))
        result_path = root / row["output_artifact"]
        original = result_path.read_bytes()
        result_path.write_bytes(original + b" ")
        with self.assertRaisesRegex(verifier_contract.VerifierContractError, "changed after it was recorded"):
            verifier_contract.verify_record(root, row, by_id, families)
        result_path.write_bytes(original)
        hand_made = dict(row, invocation_id="auditor-operator-verification-fd95c8f0",
                         input_artifact_sha256="2" * 64)
        with self.assertRaisesRegex(verifier_contract.VerifierContractError, "completion receipt"):
            verifier_contract.verify_record(root, hand_made, {**by_id, hand_made["invocation_id"]: hand_made},
                                            families)

        # Phase 1 is untouched: bytes, the five auditor rows, the fan-out phase, and its events.
        for path, value in before.items():
            self.assertEqual(path.read_bytes(), value, path)
        after_lines = (root / "invocations.jsonl").read_text().splitlines()
        self.assertEqual([line for line in after_lines
                          if json.loads(line).get("role") in phase1_runtime.ROLE_SPECS], auditor_lines)
        run_after = json.loads((root / "run.json").read_text())
        self.assertEqual(run_after.get("phases"), run_before.get("phases"))
        self.assertEqual((run_after.get("status"), run_after.get("pause_reason")),
                         (run_before.get("status"), run_before.get("pause_reason")))
        self.assertEqual([t for t in self.event_types(root)
                          if t in {"phase_start", "phase_end", "pause_start", "pause_end"}],
                         phase_events_before)
        metrics = summarize_invocations.aggregate(summarize_invocations.load(root), require_phase_one=True)
        self.assertEqual(metrics["errors"], [])
        self.assertTrue(metrics["phase_one"]["exact_quorum"])
        policy = record_invocation.load_policy_snapshot(root)
        bindings, _ = run_coordinator.phase_one_bindings(root, ledger, policy)
        self.assertEqual([b["role"] for b in bindings], list(phase1_runtime.ROLE_SPECS))
        status = json.loads((root / "phase1/verifier" / role / "status.json").read_text())
        self.assertEqual((status["status"], status["invocation_id"]), ("completed", row["invocation_id"]))

    def test_packet_is_blind_and_the_sandbox_denies_the_reasoning_artifacts(self) -> None:
        root = self.audit_after_phase1()
        # 2.6.6 denies only what exists, so every withheld artefact is created first.
        for name in VERIFIER_EXTRA_DENIES:
            target = root / name
            if "." in name:
                target.write_text("F13 REFUTED by the operator\n", encoding="utf-8")
            else:
                target.mkdir(parents=True, exist_ok=True)
                (target / "notes.md").write_text("F13 REFUTED by the operator\n", encoding="utf-8")
        result = self.run_verifier(root, ["F13", "F14", "F15"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        start = self.verifier_starts()[0]
        argv = start["argv"]
        source_dir, packet_dir = (pathlib.Path(p) for p in run_phase1_argv_add_dirs(argv))
        snapshot = json.loads((root / "phase1/quorum.json").read_text())["source_snapshot_sha256"]
        self.assertEqual(source_dir, root / "phase1/snapshots" / snapshot / "raw")
        files = sorted(p.relative_to(packet_dir).as_posix() for p in packet_dir.rglob("*") if p.is_file())
        self.assertEqual(files, sorted([
            "ROLE_PACKET.json", "packet_manifest.json", "evidence/pdf_text.txt",
            "evidence/file_inventory.json", "evidence/workbook_summaries.json",
            "evidence/code_entrypoints.json",
        ]))
        packet = json.loads((packet_dir / "ROLE_PACKET.json").read_text())
        self.assertEqual(set(packet), {
            "schema_version", "role", "source_snapshot_sha256", "source_manifest", "assigned_evidence",
            "raw_access", "model", "hypotheses", "hypotheses_sha256", "output_contract",
        })
        self.assertEqual(packet["schema_version"], verifier_contract.PACKET_VERSION)
        known = {row["path"]: row["sha256"] for row in self.snapshot_manifest(root)["source_files"]}
        by_claim = {}
        for index, hypothesis in enumerate(packet["hypotheses"], 1):
            self.assertEqual(set(hypothesis), {"hypothesis_id", "claim", "claim_sha256",
                                               "source_location", "source_hashes"})
            self.assertEqual(hypothesis["hypothesis_id"], f"H{index}")
            self.assertEqual(hypothesis["claim_sha256"], verifier_contract.claim_sha256(hypothesis["claim"]))
            for source in hypothesis["source_hashes"]:
                self.assertTrue(source["path"].startswith(("paper/", "pkg/")))
                self.assertEqual(known[source["path"]], source["sha256"])
            by_claim[hypothesis["claim"]] = hypothesis
        self.assertEqual(set(by_claim), {CLAIMS["F13"], CLAIMS["F14"], CLAIMS["F15"]})
        # F14 cited a stale pkg hash and a generated report; only the snapshot-bound file survives.
        self.assertEqual(by_claim[CLAIMS["F14"]]["source_hashes"],
                         [{"path": "pkg/data.csv", "sha256": known["pkg/data.csv"]}])
        self.assertEqual(packet["hypotheses_sha256"],
                         phase1_runtime.sha256_bytes(phase1_runtime.canonical_bytes(packet["hypotheses"])))
        # Nothing a reviewer or the coordinator concluded, and no finding id, reaches the model.
        attempt = pathlib.Path(start["cwd"]).parent
        visible = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                            for p in [*packet_dir.rglob("*"), attempt / "prompt.txt", attempt / "input.json"]
                            if p.is_file())
        for sentinel in (*WITHHELD_SENTINELS, "F13", "F14", "F15", "finding_id"):
            self.assertNotIn(sentinel, visible, sentinel)
        for name in ("CHECKLIST.md", "BRIEF_TEMPLATE.md"):
            self.assertFalse((packet_dir / name).exists())
        settings = json.loads(pathlib.Path(argv[argv.index("--settings") + 1]).read_text())
        filesystem = settings["sandbox"]["filesystem"]
        output = pathlib.Path(start["cwd"])
        self.assertEqual(filesystem["allowRead"], [str(source_dir), str(packet_dir), str(output)])
        self.assertEqual(filesystem["allowWrite"], [str(output)])
        for name in ("paper", "pkg", "report", "work", "critique", *VERIFIER_EXTRA_DENIES):
            self.assertIn(str(root / name), filesystem["denyRead"], name)
        self.assertEqual(settings["sandbox"]["network"], {"allowedDomains": [], "deniedDomains": ["*"]})
        # Same CLI flags as the auditors.
        auditor = next(row for row in self.starts() if row["role"] == "auditor_reproduction")["argv"]
        for flag in ("--safe-mode", "--no-session-persistence", "--strict-mcp-config", "--no-chrome",
                     "--disable-slash-commands"):
            self.assertIn(flag, argv)
        for option in ("--model", "--effort", "--max-budget-usd", "--permission-mode", "--tools"):
            self.assertEqual(argv[argv.index(option) + 1], auditor[auditor.index(option) + 1], option)
        self.assertNotIn("--bare", argv)
        # Negative control: the auditors' settings carry none of the verifier's extra denials,
        # and their packets keep the checklist the verifier withholds.
        auditor_settings = json.loads(pathlib.Path(auditor[auditor.index("--settings") + 1]).read_text())
        for name in VERIFIER_EXTRA_DENIES:
            self.assertNotIn(str(root / name), auditor_settings["sandbox"]["filesystem"]["denyRead"])
        self.assertTrue((pathlib.Path(run_phase1_argv_add_dirs(auditor)[1]) / "CHECKLIST.md").is_file())

    def test_rerun_replays_from_the_receipt_with_zero_starts(self) -> None:
        root = self.audit_after_phase1()
        first = self.run_verifier(root, ["F13", "F14"])
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        starts = len(self.starts())
        original = self.verifier_rows(root)
        self.assertEqual(len(original), 1)
        second = self.run_verifier(root, ["F14", "F13"])
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.assertEqual(json.loads(second.stdout)["status"], "replayed")
        self.assertEqual(len(self.starts()), starts, "a replay makes no provider call, not even a probe")
        rows = self.verifier_rows(root)
        self.assertEqual(len(rows), 2)
        replay = rows[1]
        self.assertEqual({key: replay[key] for key in (
            "role_class", "round", "attempt", "attempt_kind", "provider_call", "status",
            "source_invocation_id", "output_artifact", "output_sha256", "input_artifact_sha256",
        )}, {
            "role_class": "verifier", "round": 0, "attempt": None, "attempt_kind": "cache_replay",
            "provider_call": False, "status": "completed",
            "source_invocation_id": original[0]["invocation_id"],
            "output_artifact": original[0]["output_artifact"],
            "output_sha256": original[0]["output_sha256"],
            "input_artifact_sha256": original[0]["input_artifact_sha256"],
        })
        verdicts, errors = self.verdicts(root)
        self.assertEqual(errors, [])
        self.assertEqual(verdicts[self.claim_hash("F13")], [(original[0]["invocation_id"], "CONFIRMED")],
                         "the replay resolves to its source call and is counted once")


class VerifierRefusals(VerifierFixture):
    def assert_nothing_happened(self, root: pathlib.Path, before: tuple, starts: int,
                                listing: list[str]) -> None:
        self.assertEqual(self.journal_bytes(root), before, "a refusal appends no journal event")
        self.assertEqual(len(self.starts()), starts, "a refusal makes no provider call")
        self.assertEqual(self.phase1_listing(root), listing, "a refusal writes no packet or status")

    def test_each_refusal_makes_no_call_no_reservation_and_names_its_remedy(self) -> None:
        root = self.audit_after_phase1()
        before, starts, listing = self.journal_bytes(root), len(self.starts()), self.phase1_listing(root)
        reservations = self.event_types(root).count("invocation_reserved")
        cases = {
            ("F1",): "a Phase-1 auditor raised or independently found it",
            ("F17",): "a Phase-1 auditor raised or independently found it",
            ("F2",): "not INCLUDED",
            ("F13", "F16"): "F16 has no admissible paper/ or pkg/ source",
            ("F99",): "unknown finding id",
            ("F13", "F13"): "listed more than once",
        }
        for findings, fragment in cases.items():
            with self.subTest(findings=findings):
                self.assert_refused(self.run_verifier(root, list(findings)), fragment)
                self.assert_nothing_happened(root, before, starts, listing)
        self.assertEqual(self.event_types(root).count("invocation_reserved"), reservations)

        quorum = root / "phase1/quorum.json"
        quorum_bytes = quorum.read_bytes()
        value = json.loads(quorum_bytes)
        value["status"] = "failed"
        quorum.write_text(json.dumps(value), encoding="utf-8")
        self.assert_refused(self.run_verifier(root, ["F13"]), "not quorum_complete")
        quorum.write_bytes(quorum_bytes)
        quorum.unlink()
        self.assert_refused(self.run_verifier(root, ["F13"]), "quorum.json is missing")
        quorum.write_bytes(quorum_bytes)
        self.assert_nothing_happened(root, before, starts, listing)

        raw = root / "phase1/snapshots" / value["source_snapshot_sha256"] / "raw/paper/paper.txt"
        raw_bytes = raw.read_bytes()
        raw.chmod(0o644)
        raw.write_bytes(raw_bytes + b"tampered\n")
        raw.chmod(0o444)
        self.assert_refused(self.run_verifier(root, ["F13"]), "verified snapshot mismatch")
        self.assert_nothing_happened(root, before, starts, listing)
        raw.chmod(0o644)
        raw.write_bytes(raw_bytes)
        raw.chmod(0o444)

        # Positive control on the same audit, then the one-verdict-per-claim refusal.
        done = self.run_verifier(root, ["F13"])
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        before, starts, listing = self.journal_bytes(root), len(self.starts()), self.phase1_listing(root)
        again = self.assert_refused(self.run_verifier(root, ["F13", "F15"]),
                                    "already has an admitted verifier verdict")
        self.assertIn("one verdict per claim text", again["remedy"])
        self.assert_nothing_happened(root, before, starts, listing)

    def test_a_preflight_failure_dispatches_nothing_and_leaves_phase1_alone(self) -> None:
        root = self.audit_after_phase1()
        phase1_status = (root / "phase1/status.json").read_bytes()
        outside = self.base / "outside"
        outside.mkdir()
        (root / "work/verifier").symlink_to(outside, target_is_directory=True)
        before, starts = self.journal_bytes(root), len(self.starts())
        result = self.run_verifier(root, ["F13"])
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["status"], "preflight_failed")
        self.assertIn("verifier promotion target escapes", result.stdout)
        self.assertEqual((self.journal_bytes(root), len(self.starts())), (before, starts))
        self.assertEqual((root / "phase1/status.json").read_bytes(), phase1_status)
        self.assertNotIn("pause_start", self.event_types(root))
        status = json.loads((root / "phase1/verifier" / self.role_for("F13") / "status.json").read_text())
        self.assertEqual((status["status"], status["verifier_inferences_started"]), ("preflight_failed", 0))
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((root / "phase1/preflight").joinpath("verifier").exists())
        # Negative control: an unsafe status path is never written through, even to report.
        (root / "work/verifier").unlink()
        shutil.rmtree(root / "phase1/verifier")
        (root / "phase1/verifier").symlink_to(outside, target_is_directory=True)
        again = self.run_verifier(root, ["F13"])
        self.assertEqual(again.returncode, 3, again.stderr + again.stdout)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual((self.journal_bytes(root), len(self.starts())), (before, starts))

    def test_a_pre_metadata_21_audit_is_refused(self) -> None:
        root = self.audit_after_phase1("legacy", metadata21=False)
        before, starts = self.journal_bytes(root), len(self.starts())
        self.assert_refused(self.run_verifier(root, ["F13"]), "not a metadata-2.1 audit")
        self.assertEqual(self.journal_bytes(root), before)
        self.assertEqual(len(self.starts()), starts)
        self.assertFalse((root / "phase1/verifier").exists())


class VerifierRetryAndRepair(VerifierFixture):
    def test_a_structural_slip_gets_one_focused_repair_bound_to_the_batch_input(self) -> None:
        root = self.audit_after_phase1()
        result = self.run_verifier(root, ["F13", "F14"], "verifier_repairable")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(len(self.verifier_starts()), 2)
        rows = [row for row in self.verifier_rows(root) if row["provider_call"]]
        self.assertEqual([(r["status"], r["attempt_kind"], r["attempt"]) for r in rows],
                         [("failed", "role_inference", 1), ("completed", "output_repair", 2)])
        self.assertIn("report hash is invalid", rows[0]["error"])
        role = self.role_for("F13", "F14")
        receipt_path, receipt = self.receipt(root, role)
        self.assertEqual(rows[1]["source_invocation_id"], rows[0]["invocation_id"])
        self.assertEqual(rows[1]["input_artifact_sha256"], receipt_path.stem)
        self.assertEqual(rows[0]["input_artifact_sha256"], receipt_path.stem)
        self.assertEqual((receipt["attempt_kind"], receipt["attempt"], receipt["source_invocation_id"]),
                         ("output_repair", 2, rows[0]["invocation_id"]))
        ledger = self.ledger(root)
        verifier_contract.verify_record(root, rows[1], {r["invocation_id"]: r for r in ledger},
                                        verifier_contract.auditor_families(ledger))
        promoted = json.loads((root / receipt["result"]).read_text())
        self.assertEqual(promoted["report"]["sha256"], digest(root / receipt["report"]))

    def test_a_repair_that_changes_a_verdict_is_refused_and_a_fresh_sample_is_taken(self) -> None:
        root = self.audit_after_phase1()
        result = self.run_verifier(root, ["F13", "F14"], "verifier_repair_mutate_verdict")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("byte-frozen verifier projection", result.stderr)
        self.assertEqual(len(self.verifier_starts()), 3, "initial, refused repair, fresh sample")
        rows = [row for row in self.verifier_rows(root) if row["provider_call"]]
        self.assertEqual([(r["status"], r["attempt_kind"]) for r in rows],
                         [("failed", "role_inference"), ("failed", "output_repair"),
                          ("completed", "role_inference")])
        self.assertIn("byte-frozen", rows[1]["error"])
        _path, receipt = self.receipt(root, self.role_for("F13", "F14"))
        promoted = json.loads((root / receipt["result"]).read_text())
        self.assertEqual({v["verdict"] for v in promoted["scientific_fields"]["verdicts"]}, {"CONFIRMED"})
        self.assertEqual(receipt["attempt"], 3)

    def test_a_missing_hypothesis_is_resampled(self) -> None:
        root = self.audit_after_phase1()
        result = self.run_verifier(root, ["F13", "F14"], "verifier_missing_hypothesis")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = [row for row in self.verifier_rows(root) if row["provider_call"]]
        self.assertEqual([r["status"] for r in rows], ["failed", "completed"])
        self.assertIn("no verdict for H2", rows[0]["error"])
        self.assertEqual(rows[0]["attempt_kind"], "role_inference")
        self.assertEqual(rows[1]["attempt_kind"], "role_inference", "a missing verdict is not repairable")

    def test_budget_exhaustion_at_three_then_refusal_and_the_ceiling_knob_is_not_wedged(self) -> None:
        root = self.audit_after_phase1()
        result = self.run_verifier(root, ["F13", "F14"], "verifier_extra_hypothesis")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        rows = [row for row in self.verifier_rows(root) if row["provider_call"]]
        self.assertEqual([(r["status"], r["attempt"]) for r in rows],
                         [("failed", 1), ("failed", 2), ("failed", 3)])
        self.assertTrue(all("not a packet hypothesis" in r["error"] for r in rows))
        role = self.role_for("F13", "F14")
        self.assertFalse((root / "phase1/completions" / role).exists())
        self.assertFalse((root / "work/verifier").exists())
        status = json.loads((root / "phase1/verifier" / role / "status.json").read_text())
        self.assertEqual(status["status"], "failed")
        # A failed verifier batch is reported in its own status file only.
        self.assertEqual(json.loads((root / "phase1/status.json").read_text())["status"], "quorum_complete")
        self.assertEqual(json.loads((root / "phase1/quorum.json").read_text())["status"], "quorum_complete")
        starts, before = len(self.starts()), self.journal_bytes(root)
        self.assert_refused(self.run_verifier(root, ["F13", "F14"], "verifier_extra_hypothesis"),
                            "exhausted its 3 accepted inference starts")
        self.assertEqual((len(self.starts()), self.journal_bytes(root)), (starts, before))
        # The operator knob buys exactly one more start, and the fourth start is recorded and
        # terminal: before 2.6.4 lifecycle_started refused attempt 4 after journaling it, which
        # left status.json at provider_requested and wedged every later run.
        knob = {"I4R_MAX_ATTEMPTS": "4"}
        fourth = self.run_verifier(root, ["F13", "F14"], "verifier_extra_hypothesis", extra_env=knob)
        self.assertEqual(fourth.returncode, 2, fourth.stderr + fourth.stdout)
        self.assertEqual(len(self.starts()), starts + 1)
        rows = [row for row in self.verifier_rows(root) if row["provider_call"]]
        self.assertEqual([(r["status"], r["attempt"]) for r in rows][-1], ("failed", 4))
        states = [json.loads(p.read_text())["state"]
                  for p in sorted((root / "phase1/roles" / role).glob("*/attempt_*/status.json"))]
        self.assertEqual(states, ["failed"] * 4)
        self.assert_refused(self.run_verifier(root, ["F13", "F14"], extra_env=knob),
                            "exhausted its 4 accepted inference starts")

    def test_transient_rejections_charge_no_attempt_and_trip_the_circuit_breaker(self) -> None:
        root = self.audit_after_phase1()
        result = self.run_verifier(root, ["F13"], "transient_always")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        rows = [row for row in self.verifier_rows(root) if row["provider_call"]]
        self.assertEqual([(r["attempt"], r["inference_start_status"], r["provider_request_status"])
                          for r in rows], [(None, "not_started", "rejected")] * 2)
        self.assertEqual(len(self.verifier_starts()), 2)
        starts = len(self.starts())
        self.assert_refused(self.run_verifier(root, ["F13"], "transient_always"),
                            "two pre-inference provider-request retries")
        self.assertEqual(len(self.starts()), starts)

    def test_a_refuted_verdict_is_recorded_and_blocks_confirmation(self) -> None:
        root = self.audit_after_phase1()
        result = self.run_verifier(root, ["F13"], "verifier_refuted")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        row = next(r for r in self.verifier_rows(root) if r["provider_call"])
        verdicts, errors = self.verdicts(root)
        self.assertEqual(errors, [])
        self.assertEqual(verdicts[self.claim_hash("F13")], [(row["invocation_id"], "REFUTED")])
        self.assertEqual(verifier_contract.VERDICT_OUTCOME["REFUTED"], "refuted")
        registry = {r["id"]: r for r in map(json.loads, (root / "work/findings.jsonl").read_text().splitlines())}
        roles = verifier_contract.required_roles(verifier_contract.load_snapshot_policy(root))
        self.assertIn("returned REFUTED", verifier_contract.confirmation_problem(
            registry["F13"], row["invocation_id"], roles, verdicts))
        # One verdict per claim text: a REFUTED claim is not re-sent in the hope of a CONFIRMED.
        # The same batch replays its recorded REFUTED; any other batch carrying the claim is refused.
        starts = len(self.starts())
        replayed = self.run_verifier(root, ["F13"])
        self.assertEqual(replayed.returncode, 0, replayed.stderr + replayed.stdout)
        self.assertEqual(json.loads(replayed.stdout)["hypotheses"][0]["verdict"], "REFUTED")
        self.assert_refused(self.run_verifier(root, ["F13", "F15"]), "already has an admitted verifier verdict")
        self.assertEqual(len(self.starts()), starts)


class VerifierDryRunAndRecovery(VerifierFixture):
    def test_dry_run_prints_the_true_plan_and_changes_nothing(self) -> None:
        root = self.audit_after_phase1()
        before, starts, listing = self.journal_bytes(root), len(self.starts()), self.phase1_listing(root)
        work_before = sorted(p.as_posix() for p in (root / "work").rglob("*"))
        dry = self.run_verifier(root, ["F13", "F14"], extra_args=["--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr + dry.stdout)
        plan = json.loads(dry.stdout)
        self.assertEqual((plan["status"], plan["role"], plan["role_class"], plan["round"],
                          plan["provider_calls"], plan["would_replay"]),
                         ("dry_run", self.role_for("F13", "F14"), "verifier", 0, 0, False))
        self.assertEqual({(h["finding_id"], h["claim_sha256"]) for h in plan["hypotheses"]},
                         {("F13", self.claim_hash("F13")), ("F14", self.claim_hash("F14"))})
        self.assertEqual({h["claim"] for h in plan["packet"]["hypotheses"]}, {CLAIMS["F13"], CLAIMS["F14"]})
        dropped = next(h for h in plan["hypotheses"] if h["finding_id"] == "F14")["dropped_source_hashes"]
        self.assertEqual({d["path"] for d in dropped}, {"pkg/data.csv", "report/REPORT_final.md"})
        self.assertEqual(self.journal_bytes(root), before)
        self.assertEqual(len(self.starts()), starts)
        self.assertEqual(self.phase1_listing(root), listing)
        self.assertEqual(sorted(p.as_posix() for p in (root / "work").rglob("*")), work_before)
        # The plan is the truth: the real run uses exactly that packet, packet hash and input hash.
        real = self.run_verifier(root, ["F13", "F14"])
        self.assertEqual(real.returncode, 0, real.stderr + real.stdout)
        receipt_path, receipt = self.receipt(root, plan["role"])
        self.assertEqual(receipt_path.stem, plan["input_sha256"])
        self.assertEqual(receipt["packet_sha256"], plan["packet_sha256"])
        self.assertEqual(json.loads((root / plan["packet_dir"] / "ROLE_PACKET.json").read_text()),
                         plan["packet"])
        replay_plan = json.loads(self.run_verifier(root, ["F13", "F14"], extra_args=["--dry-run"]).stdout)
        self.assertTrue(replay_plan["would_replay"])

    def test_a_stranded_verifier_attempt_is_guarded_then_recovered_without_charge(self) -> None:
        root = self.audit_after_phase1()
        role = self.role_for("F13")
        route = snapshot_route(root)
        invocation = record_invocation.reserve_invocation(root, {
            "role": role, "role_class": "verifier", "round": 0, "attempt_kind": "role_inference",
            "provider": route["provider"], "requested_model": route["requested_model"],
            "reasoning_effort": route["reasoning_effort"],
            "input_artifact_sha256": "b" * 64, "prompt_sha256": "c" * 64, "source_invocation_id": None,
        })["invocation_id"]
        record_invocation.mark_provider_request(root, invocation, status="requested")
        attempt = root / "phase1/roles" / role / ("b" * 64) / "attempt_01_killed"
        attempt.mkdir(parents=True)
        (attempt / "status.json").write_text(json.dumps({
            "schema_version": "i4r-phase1-attempt-1", "state": "provider_requested",
            "invocation_id": invocation, "attempt_kind": "role_inference", "attempt": None,
            "provider_request_status": "requested", "inference_start_status": "not_started",
            "requested_session_id": "sess-killed", "input_artifact_sha256": "b" * 64,
            "started_at": "2026-09-25T10:00:00.000Z",
        }), encoding="utf-8")
        before, starts = self.journal_bytes(root), len(self.starts())
        self.assert_refused(self.run_verifier(root, ["F13"]), "unresolved Phase-1 invocation")
        self.assertEqual((self.journal_bytes(root), len(self.starts())), (before, starts))
        recovered = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_phase1.py"), str(root), "--recover-stranded"],
            text=True, capture_output=True, env=self.env("success", "verifier"), check=False,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr + recovered.stdout)
        row = next(r for r in self.ledger(root) if r["invocation_id"] == invocation)
        self.assertEqual((row["role_class"], row["round"], row["attempt"], row["status"]),
                         ("verifier", 0, None, "failed"))
        record_invocation.validate_record(row, root)
        result = self.run_verifier(root, ["F13"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        completed = next(r for r in self.verifier_rows(root) if r["status"] == "completed")
        self.assertEqual(completed["attempt"], 1, "the strand was never charged an attempt")


class Phase1CeilingKnobIsNotWedged(VerifierFixture):
    """lifecycle_started used to hard-code 1..3 while I4R_MAX_ATTEMPTS allows up to 20."""

    def test_a_fourth_auditor_start_under_the_knob_is_terminal_and_does_not_block_the_next_run(self) -> None:
        root = self.make_audit("knob")
        knob = {"I4R_MAX_ATTEMPTS": "4"}
        first = self.run_phase1(root, "invalid_evidence", extra_env=knob)
        self.assertEqual(first.returncode, 2, first.stderr + first.stdout)
        rows = [r for r in self.ledger(root) if r["role"] == "auditor_reproduction" and r["provider_call"]]
        self.assertEqual([(r["status"], r["attempt"]) for r in rows],
                         [("failed", 1), ("failed", 2), ("failed", 3), ("failed", 4)])
        states = [json.loads(p.read_text())["state"]
                  for p in sorted((root / "phase1/roles/auditor_reproduction").glob("*/*/status.json"))]
        self.assertEqual(states, ["failed"] * 4)
        second = self.run_phase1(root, "invalid_evidence", extra_env=knob)
        self.assertEqual(second.returncode, 2, second.stderr + second.stdout)
        self.assertNotIn("unresolved Phase-1 invocation", second.stderr + second.stdout)
        failed = json.loads((root / "phase1/status.json").read_text())["failed_roles"]
        self.assertIn("exhausted its 4 accepted inference starts", failed["auditor_reproduction"])
        # Negative control: without the knob the protocol default of three still applies.
        default = self.make_audit("default")
        third = self.run_phase1(default, "invalid_evidence")
        self.assertEqual(third.returncode, 2, third.stderr + third.stdout)
        self.assertEqual(len([r for r in self.ledger(default)
                              if r["role"] == "auditor_reproduction" and r["provider_call"]]), 3)


class AuditorInputsArePinnedTo263(unittest.TestCase):
    """The verifier generalised run_phase1/phase1_runtime only through defaulted keywords."""

    ROW = {"requested_model": "sonnet-5", "spawn_alias": "sonnet", "provider": "anthropic",
           "reasoning_effort": "max"}
    ROOT = pathlib.Path("/i4r/pin/audit")

    def manifest(self) -> dict:
        names = ["archive_licence_metadata.json", "code_entrypoints.json", "environment_fingerprint.json",
                 "file_inventory.json", "pdf_text.txt", "privacy_scan.json", "workbook_summaries.json"]
        return {
            "snapshot_sha256": "1" * 64,
            "source_files": [
                {"path": "paper/paper.txt", "sha256": "2" * 64, "bytes": 10},
                {"path": "pkg/data.csv", "sha256": "3" * 64, "bytes": 20},
            ],
            "common_evidence": {"artifacts": [
                {"path": name, "sha256": f"{index + 4:x}" * 64, "bytes": 100 + index}
                for index, name in enumerate(names)
            ]},
        }

    def test_role_input_hash_probe_key_and_sandbox_are_the_263_values(self) -> None:
        # A Sonnet 5 audit froze the legacy probe bound (2.7.1), under which the key is the 2.6.3 one.
        key, _components = run_phase1.probe_key("2.1.206 (Fake Claude Code)", self.ROW, self.ROOT,
                                                bound=run_phase1.LEGACY_PROBE_BOUND)
        self.assertEqual(key, PINNED_263["probe_key"])
        self.assertEqual(phase1_runtime.sandbox_policy_hash(), PINNED_263["sandbox_policy_sha256"])
        for role in phase1_runtime.ROLE_SPECS:
            packet = phase1_runtime.make_role_packet(role, self.manifest(), self.ROW)
            prompt = run_phase1.build_role_prompt(role, "audit-pin-fixture", packet)
            with self.subTest(role=role):
                # 2.6.5 changed the auditor prompt on purpose, and only by stating the coordinator's
                # participant-code rule. Without that one sentence the auditors' inputs are still
                # exactly the 2.6.3 ones, so nothing else in the auditor path moved.
                statement = phase1_runtime.provider_safety.RULE_STATEMENT
                self.assertEqual(prompt.count(statement), 1)
                # 2.6.8 added one more auditor-only paragraph on purpose: deposit personal data is
                # reported as a finding, never a reason to stop. Strip it as well, so the pin still
                # proves that nothing else in the auditor path moved since 2.6.3.
                paragraph = run_phase1.AUDITOR_PII_REPORTING + "\n\n"
                self.assertEqual(prompt.count(paragraph), 1)
                without_265 = prompt.replace(statement + " ", "", 1)
                without_268 = without_265.replace(paragraph, "", 1)
                self.assertNotEqual(phase1_runtime.role_input_hash(packet, prompt, key),
                                    PINNED_263["roles"][role])
                # Negative control: removing only the 2.6.5 statement no longer gives the 2.6.3
                # inputs, because the 2.6.8 paragraph is really in the prompt.
                self.assertNotEqual(phase1_runtime.role_input_hash(packet, without_265, key),
                                    PINNED_263["roles"][role])
                self.assertEqual(phase1_runtime.role_input_hash(packet, without_268, key),
                                 PINNED_263["roles"][role])
        paths = (self.ROOT, self.ROOT / "phase1", self.ROOT / "phase1/snapshots/x/raw",
                 self.ROOT / "phase1/role_packets/x/r/p", self.ROOT / "phase1/roles/r/i/a/output.partial")
        settings = phase1_runtime.sandbox_settings(*paths)
        self.assertEqual(phase1_runtime.sha256_bytes(phase1_runtime.canonical_bytes(settings)),
                         PINNED_263["sandbox_settings_sha256"])
        # Negative control: the verifier's extra denials do change the settings it runs under.
        verifier_settings = phase1_runtime.sandbox_settings(
            *paths, extra_deny_read=[self.ROOT / name for name in VERIFIER_EXTRA_DENIES])
        self.assertNotEqual(phase1_runtime.sha256_bytes(phase1_runtime.canonical_bytes(verifier_settings)),
                            PINNED_263["sandbox_settings_sha256"])

    def test_the_auditors_keep_their_263_targets_and_only_a_callable_sees_the_attempt(self) -> None:
        # runner-3 and runner-4 are scoped to the verifier: the auditors' targets carry no extra
        # denial and no recovered-repair freeze, so their settings hashes and recovery path are
        # the 2.6.3 ones (the pin above).
        targets = run_phase1.auditor_targets()
        self.assertEqual(targets.extra_deny_read, ())
        self.assertFalse(targets.freeze_recovered_repairs)
        self.assertFalse(targets.repair_records_original_input)
        paths = (self.ROOT, self.ROOT / "phase1", self.ROOT / "phase1/snapshots/x/raw",
                 self.ROOT / "phase1/role_packets/x/r/p", self.ROOT / "phase1/roles/r/i/a/output.partial")
        seen = []
        settings = phase1_runtime.sandbox_settings(
            *paths, extra_deny_read=lambda roots: seen.append(roots) or [self.ROOT / "phase1/roles/r/i/b"])
        self.assertEqual(seen, [paths[2:]], "a callable is asked with the attempt's allowRead roots")
        self.assertEqual(settings["sandbox"]["filesystem"]["denyRead"][-1], str(self.ROOT / "phase1/roles/r/i/b"))
        self.assertEqual(phase1_runtime.sandbox_settings(*paths, extra_deny_read=lambda roots: ()),
                         phase1_runtime.sandbox_settings(*paths))

    def test_role_identity_keys_on_the_packaged_names_only(self) -> None:
        for role in phase1_runtime.ROLE_SPECS:
            self.assertEqual(run_phase1.role_identity(role), ("auditor", 0))
        self.assertEqual(run_phase1.role_identity("verifier_0123456789ab"), ("verifier", 0))
        # The Neudorfer out-of-protocol names, the probe and near-misses are never a verifier.
        for role in ("operator_verifier", "auditor_operator_verification", "phase1_capability_probe",
                     "verifier", "verifier_0123456789AB", "verifier_0123456789abc"):
            self.assertEqual(run_phase1.role_identity(role), ("other", None), role)


# ------------------------------------------------------------------ the verified 2.6.4 review findings
KILL_AFTER_TERMINAL = """import os, sys
sys.path.insert(0, {scripts!r})
sys.argv[0] = {runner!r}
import run_verifier
def _die(*args, **kwargs):
    os._exit(137)  # SIGKILL-equivalent: the terminal row is journaled, the receipt is not
run_verifier._receipt = _die
raise SystemExit(run_verifier.main())
"""
KILL_AFTER_REPAIR_RESPONSE = """import os, sys
sys.path.insert(0, {scripts!r})
sys.argv[0] = {runner!r}
import run_verifier, run_phase1
original = run_phase1.execution_errors
def execution_errors(execution, **kwargs):
    if execution.attempt_dir.name.startswith("repair_"):
        os._exit(137)  # killed after the repair response was retained, before its validation
    return original(execution, **kwargs)
run_phase1.execution_errors = execution_errors
raise SystemExit(run_verifier.main())
"""


def covered_by(path: pathlib.Path, deny: list[str]) -> list[str]:
    return [entry for entry in deny if path == pathlib.Path(entry) or pathlib.Path(entry) in path.parents]


class VerifierReviewFindings264(VerifierFixture):
    """The verified review findings on the 2.6.4 runner, each rebuilt on disk and run for real.

    runner-1 evidence/bindings joined by None == None, runner-2 a receipt lost after the terminal
    row, runner-3 a recovered repair promoted without its freeze, runner-4 a deny list that left
    every other sample readable, runner-5 a malformed sample recorded as permanent, runner-6 a
    report contradicting itself, docs-5 usage errors exiting 2.
    """

    def provider_rows(self, root: pathlib.Path, role: str) -> list[dict]:
        return [row for row in self.verifier_rows(root) if row["provider_call"] and row["role"] == role]

    def admitted(self, root: pathlib.Path, role: str) -> tuple[dict, dict, dict]:
        _path, receipt = self.receipt(root, role)
        result = json.loads((root / receipt["result"]).read_text())
        manifest = json.loads((root / receipt["evidence"]).read_text())
        return receipt, result, manifest

    def settings_of(self, start: dict) -> dict:
        argv = start["argv"]
        return json.loads(pathlib.Path(argv[argv.index("--settings") + 1]).read_text())

    def wrapper(self, template: str, name: str) -> pathlib.Path:
        path = self.base / name
        path.write_text(template.format(scripts=str(SCRIPTS), runner=str(SCRIPTS / "run_verifier.py")),
                        encoding="utf-8")
        return path

    def run_wrapped(self, wrapper: pathlib.Path, root: pathlib.Path, findings: list[str],
                    scenario: str = "success") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(wrapper), str(root), "--findings", *findings, "--claude-bin", str(self.fake)],
            text=True, capture_output=True, env=self.env(scenario, "verifier"), timeout=120, check=False,
        )

    def attempt_states(self, root: pathlib.Path, role: str) -> dict[str, str]:
        return {path.parent.name.split("_")[0]: json.loads(path.read_text())["state"]
                for path in sorted((root / "phase1/roles" / role).glob("*/*/status.json"))}

    # runner-1 ----------------------------------------------------------------------------------
    def test_verdict_evidence_and_manifest_bindings_must_join_the_snapshot_exactly(self) -> None:
        root = self.audit_after_phase1()
        known = {row["path"]: row["sha256"] for row in self.snapshot_manifest(root)["source_files"]}
        result = self.run_verifier(root, ["F13"], "verifier_evidence_unbound")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = self.provider_rows(root, self.role_for("F13"))
        # Before the fix the first sample, citing a file absent from the deposit with a null hash,
        # was admitted as a CONFIRMED verdict: one row, completed.
        self.assertEqual([(r["status"], r["attempt_kind"]) for r in rows],
                         [("failed", "role_inference"), ("completed", "role_inference")])
        self.assertIn("evidence[0] is not an exact", rows[0]["error"])
        _receipt, admitted, manifest = self.admitted(root, self.role_for("F13"))
        for verdict in admitted["scientific_fields"]["verdicts"]:
            for row in verdict["evidence"]:
                self.assertEqual(known.get(row["source_path"]), row["source_sha256"])
                self.assertIsInstance(row["source_sha256"], str)
        # The same None == None hole in the evidence manifest's source_bindings (a structural
        # binding, so it goes to the focused repair first; the fake repair keeps the bad manifest).
        result = self.run_verifier(root, ["F14"], "verifier_binding_unbound")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = self.provider_rows(root, self.role_for("F14"))
        self.assertEqual([(r["status"], r["attempt_kind"]) for r in rows],
                         [("failed", "role_inference"), ("failed", "output_repair"),
                          ("completed", "role_inference")])
        self.assertIn("unbound source hash", rows[0]["error"])
        _receipt, _admitted, manifest = self.admitted(root, self.role_for("F14"))
        for binding in manifest["source_bindings"]:
            self.assertEqual(known.get(binding["path"]), binding["sha256"])
            self.assertIsInstance(binding["sha256"], str)
        self.assertEqual(self.verdicts(root)[1], [])

    # runner-5 ----------------------------------------------------------------------------------
    def test_an_unhashable_evidence_path_is_an_invalid_sample_and_is_resampled(self) -> None:
        root = self.audit_after_phase1()
        result = self.run_verifier(root, ["F13"], "verifier_evidence_unhashable")
        # Before the fix: rc 2, one row failed "permanent" with "TypeError: unhashable type".
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = self.provider_rows(root, self.role_for("F13"))
        self.assertEqual([(r["status"], r["error_class"]) for r in rows], [("failed", "schema"), ("completed", None)])
        self.assertIn("evidence[0] is not an exact", rows[0]["error"])
        self.assertEqual(len(self.verifier_starts()), 2)

    # runner-6 ----------------------------------------------------------------------------------
    def test_a_report_that_contradicts_itself_is_resampled(self) -> None:
        root = self.audit_after_phase1()
        result = self.run_verifier(root, ["F13"], "verifier_contradicting_section")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = self.provider_rows(root, self.role_for("F13"))
        # Before the fix the report carrying "### H1: CONFIRMED" and a second section with
        # "### H1: REFUTED" was admitted on the first sample.
        self.assertEqual([r["status"] for r in rows], ["failed", "completed"])
        self.assertIn("exactly one ## Hypotheses tested section", rows[0]["error"])
        _path, receipt = self.receipt(root, self.role_for("F13"))
        report = (root / receipt["report"]).read_text()
        self.assertEqual(report.count("## Hypotheses tested"), 1)
        self.assertNotIn("REFUTED", report)

    # runner-2 ----------------------------------------------------------------------------------
    def test_a_receipt_lost_after_the_terminal_row_is_rebuilt_by_rerunning_the_command(self) -> None:
        root = self.audit_after_phase1()
        role = self.role_for("F13")
        killed = self.run_wrapped(self.wrapper(KILL_AFTER_TERMINAL, "kill_after_terminal.py"), root, ["F13"])
        self.assertEqual(killed.returncode, 137, killed.stderr + killed.stdout)
        self.assertEqual([(r["status"], r["attempt_kind"]) for r in self.provider_rows(root, role)],
                         [("completed", "role_inference")])
        self.assertFalse((root / "phase1/completions" / role).exists())
        self.assertEqual(self.attempt_states(root, role), {"attempt": "response_received"})
        # Every other batch still fails closed on the inadmissible record, and the remedy now
        # names the working recovery instead of the legacy_record dead end.
        refused = self.assert_refused(self.run_verifier(root, ["F15"]), "not admissible")
        self.assertIn("re-run the same run_verifier.py command", refused["remedy"])
        self.assertIn("rebuild the lost receipt with no provider call", refused["remedy"])
        plan = json.loads(self.run_verifier(root, ["F13"], extra_args=["--dry-run"]).stdout)
        self.assertEqual((plan["would_rebuild_receipt"], plan["would_replay"], plan["provider_calls"]),
                         (True, False, 0))
        starts, inferences = len(self.starts()), self.event_types(root).count("inference_start")
        # Before the fix this was refused (rc 5) forever: bind_inputs judged the ledger first.
        again = self.run_verifier(root, ["F13"])
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertEqual(json.loads(again.stdout)["status"], "replayed")
        self.assertEqual(len(self.starts()), starts, "the rebuild makes no provider call and no probe")
        self.assertEqual(self.event_types(root).count("inference_start"), inferences)
        self.assertEqual([(r["status"], r["attempt_kind"]) for r in self.provider_rows(root, role)],
                         [("completed", "role_inference")], "no second provider row")
        receipt_path, receipt = self.receipt(root, role)
        self.assertEqual(set(receipt), RECEIPT_KEYS)
        self.assertEqual(self.attempt_states(root, role), {"attempt": "completed"})
        verdicts, errors = self.verdicts(root)
        self.assertEqual(errors, [])
        self.assertEqual([v for _inv, v in verdicts[self.claim_hash("F13")]], ["CONFIRMED"])
        # The rebuilt record is the runner's: it is re-admitted in full.
        row = self.provider_rows(root, role)[0]
        importlib.import_module("run_verifier").readmit_record(root, row, receipt)
        other = self.run_verifier(root, ["F15"])
        self.assertEqual(other.returncode, 0, other.stderr + other.stdout)

    # runner-3 ----------------------------------------------------------------------------------
    def test_a_repair_recovered_after_a_kill_is_reproved_against_its_frozen_projection(self) -> None:
        root = self.audit_after_phase1()
        # Control: live, the repair that flips an honest REFUTED to CONFIRMED is refused.
        control = self.run_verifier(root, ["F14"], "verifier_repair_flip")
        self.assertEqual(control.returncode, 0, control.stderr + control.stdout)
        self.assertEqual([(r["status"], r["attempt_kind"]) for r in self.provider_rows(root, self.role_for("F14"))],
                         [("failed", "role_inference"), ("failed", "output_repair"), ("completed", "role_inference")])
        # Attack: the runner dies after the repair response is retained, before its validation.
        role = self.role_for("F13")
        killed = self.run_wrapped(self.wrapper(KILL_AFTER_REPAIR_RESPONSE, "kill_after_repair.py"), root,
                                  ["F13"], "verifier_repair_flip")
        self.assertEqual(killed.returncode, 137, killed.stderr + killed.stdout)
        self.assertEqual(self.attempt_states(root, role), {"attempt": "failed", "repair": "response_received"})
        starts = len(self.verifier_starts())
        # A plain re-run adopts the retained repair. Before the fix it was validated with no frozen
        # projection and promoted as output_repair, turning the REFUTED into CONFIRMED with no new
        # provider call. Now the freeze is re-proved, the repair fails, and a fresh sample decides.
        again = self.run_verifier(root, ["F13"], "verifier_refuted")
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        rows = self.provider_rows(root, role)
        self.assertEqual([(r["status"], r["attempt_kind"]) for r in rows],
                         [("failed", "role_inference"), ("failed", "output_repair"), ("completed", "role_inference")])
        self.assertIn("byte-frozen verifier projection", rows[1]["error"])
        self.assertEqual(len(self.verifier_starts()), starts + 1)
        verdicts, errors = self.verdicts(root)
        self.assertEqual(errors, [])
        self.assertEqual(verdicts[self.claim_hash("F13")], [(rows[2]["invocation_id"], "REFUTED")])

    def test_a_recovered_repair_with_no_frozen_projection_cannot_prove_its_freeze(self) -> None:
        root = self.audit_after_phase1()
        role = self.role_for("F13")
        killed = self.run_wrapped(self.wrapper(KILL_AFTER_REPAIR_RESPONSE, "kill_after_repair.py"), root,
                                  ["F13"], "verifier_repairable")
        self.assertEqual(killed.returncode, 137, killed.stderr + killed.stdout)
        frozen = next((root / "phase1/roles" / role).glob("*/attempt_*/repair_packet/SCIENTIFIC_PROJECTION.json"))
        frozen.unlink()
        again = self.run_verifier(root, ["F13"])
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        rows = self.provider_rows(root, role)
        self.assertEqual([(r["status"], r["attempt_kind"]) for r in rows],
                         [("failed", "role_inference"), ("failed", "output_repair"), ("completed", "role_inference")])
        self.assertIn("cannot prove its byte-freeze", rows[1]["error"])

    # runner-4 ----------------------------------------------------------------------------------
    def test_each_attempt_denies_every_other_sample_and_the_late_artifacts(self) -> None:
        root = self.audit_after_phase1()
        # Three rejected samples of F13, each saying REFUTED, stay on disk under phase1/roles.
        first = self.run_verifier(root, ["F13"], "verifier_refuted_invalid")
        self.assertEqual(first.returncode, 2, first.stderr + first.stdout)
        prior = sorted((root / "phase1/roles" / self.role_for("F13")).rglob("output.partial/result.json"))
        self.assertEqual(len(prior), 3)
        auditor_reports = sorted((root / "phase1/roles").glob("auditor_*/*/*/output.partial/REPORT.md"))
        self.assertEqual(len(auditor_reports), 5)
        # The artefacts finalization writes at the audit root (a round reopened after finalize).
        late = [root / name for name in (
            "claims_script.R", "verification.Rmd", "verification.html", "verification_summary.csv",
            "verification_manifest.json", "RUN_METADATA.md", "run_metrics.json",
            "finalization_receipt.json", "RELEASE_MANIFEST.md", "release-manifest.json",
            "document_build_manifest.json")]
        for path in [*late, root / "verification_extra_notes.txt"]:
            path.write_text("F13 REFUTED by the operator\n", encoding="utf-8")
        orphan = root / "phase1/transactions/orphaned-0000"
        orphan.mkdir(parents=True)
        release = root.parent / "release"
        release.mkdir()
        (release / "SETTLED.json").write_text("{}\n", encoding="utf-8")
        role = self.role_for("F13", "F15")
        result = self.run_verifier(root, ["F13", "F15"], "verifier_repairable")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        starts = [s for s in self.verifier_starts() if s["role"] == role]
        self.assertEqual(len(starts), 2, "one sample and its focused repair")
        own_dirs = []
        for start in starts:
            filesystem = self.settings_of(start)["sandbox"]["filesystem"]
            deny, allow = filesystem["denyRead"], filesystem["allowRead"]
            with self.subTest(attempt=pathlib.Path(start["cwd"]).parent.name):
                # Before the fix: prior REFUTED output denied by [], auditor copies all readable.
                for path in [*prior, *auditor_reports, *late, root / "verification_extra_notes.txt",
                             orphan, release / "SETTLED.json"]:
                    self.assertTrue(covered_by(path, deny), path)
                for path in allow:  # its own source, packet and output stay readable
                    self.assertEqual(covered_by(pathlib.Path(path), deny), [], path)
                # No tree around this audit, so no tree-level denial is invented.
                self.assertFalse({pathlib.Path(d).name for d in deny} & {"registry", "comments", "old_runs"})
            own_dirs.append(pathlib.Path(start["cwd"]).parent)
        sample, repair = own_dirs
        self.assertTrue(repair.name.startswith("repair_"))
        # The sample's settings deny nothing of its own; the repair reads its source attempt's
        # frozen packet, and the source attempt is the one sibling it does not deny.
        repair_deny = self.settings_of(starts[1])["sandbox"]["filesystem"]["denyRead"]
        self.assertEqual(covered_by(sample / "repair_packet", repair_deny), [])
        self.assertTrue(covered_by(prior[0].parent, repair_deny))
        # Negative control: the auditors' settings are exactly the 2.6.3 protected list.
        auditor = next(s for s in self.starts() if s["role"] == "auditor_reproduction")
        self.assertEqual(self.settings_of(auditor)["sandbox"]["filesystem"]["denyRead"], [
            str(root / name) for name in (
                "paper", "pkg", "report", "work", "critique", "run.json", "run.events.jsonl",
                "invocations.jsonl", "package_manifest.json", "pipeline-policy.snapshot.json")])

    def test_the_deny_list_holds_only_what_can_be_read(self) -> None:
        # 2.6.6: every denial adds sandbox profile to every Bash call (unlink and create rules for
        # the path and each ancestor); 47 denials under a long, deep audit path overflowed ARG_MAX
        # (E2BIG) on the tenth production audit and left the verifier without a shell.
        run_verifier = importlib.import_module("run_verifier")
        root = self.audit_after_phase1()
        (root / "coordinator").mkdir()
        (root / "coordinator/notes.md").write_text("x\n", encoding="utf-8")
        (root / "phase1/transactions").mkdir(parents=True, exist_ok=True)   # empty
        (root / "RUN_METADATA.md").write_text("telemetry\n", encoding="utf-8")
        plan = object.__new__(run_verifier.BatchPlan)
        plan.root, plan.phase_root, plan.audit_id = root, root / "phase1", "audit-pin"
        snapshot = json.loads((root / "phase1/quorum.json").read_text())["source_snapshot_sha256"]
        packets = root / "phase1/role_packets" / snapshot
        own_packet = packets / "verifier_0123456789ab/p1"
        own_packet.mkdir(parents=True)
        (packets / "verifier_ba9876543210/p2").mkdir(parents=True)
        (packets / "verifier_ba9876543210/p2/ROLE_PACKET.json").write_text("{}\n", encoding="utf-8")
        output = root / "phase1/roles/verifier_0123456789ab/i/attempt_01/output.partial"
        deny = [str(p) for p in plan.deny_read((root / "phase1/snapshots" / snapshot / "raw", own_packet, output))]
        self.assertIn(str(root / "coordinator"), deny)
        for absent in ("SETTLED.json", "SETTLED.md", "claims_map.tsv", "verification_snippets"):
            self.assertNotIn(str(root / absent), deny, absent)
        self.assertNotIn(str(root / "phase1/transactions"), deny, "an empty directory holds nothing")
        self.assertNotIn(str(root / "RUN_METADATA.md"), deny, "telemetry before finalization")
        self.assertIn(str(packets / "verifier_ba9876543210"), deny, "another batch's claims")
        for role in phase1_runtime.ROLE_SPECS:
            if (packets / role).exists():
                self.assertNotIn(str(packets / role), deny, role)
        for role in phase1_runtime.ROLE_SPECS:
            if (root / "phase1/roles" / role).exists():
                self.assertIn(str(root / "phase1/roles" / role), deny, role)
        # After finalization the telemetry files carry results and are denied again.
        (root / "finalization_receipt.json").write_text("{}\n", encoding="utf-8")
        deny = [str(p) for p in plan.deny_read((root / "phase1/snapshots" / snapshot / "raw", own_packet, output))]
        self.assertIn(str(root / "RUN_METADATA.md"), deny)
        self.assertIn(str(root / "finalization_receipt.json"), deny)

    def test_tree_records_are_denied_only_inside_a_recognised_tree(self) -> None:
        tree = self.base / "tree"
        for name in ("data", "registry/audit-tree-1", "comments", "metadata", "old_runs"):
            (tree / name).mkdir(parents=True)
        (tree / "registry/audit-tree-1/comment.json").write_text("{}\n", encoding="utf-8")
        own = [tree / "comments/Journal/Paper/comment.pdf", tree / "metadata/Journal/Paper/run.json",
               tree / "old_runs/Journal/Paper/audit/report/REPORT_final.md"]
        other = [tree / "comments/Journal/Other paper/comment.pdf", tree / "metadata/Other/Paper/run.json"]
        for path in own + other:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("F13 REFUTED by the operator\n", encoding="utf-8")
        root = self.audit_after_phase1("tree/runs/Journal/Paper/audit", audit_id="audit-tree-1")
        (root.parent / "release").mkdir(parents=True)
        (root.parent / "release/SETTLED.json").write_text("{}\n", encoding="utf-8")
        result = self.run_verifier(root, ["F13"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        deny = self.settings_of(self.verifier_starts()[0])["sandbox"]["filesystem"]["denyRead"]
        # This audit's own published records (and an earlier audit of the same paper) are denied.
        for path in (tree / "registry/audit-tree-1/comment.json", *own, root.parent / "release/SETTLED.json"):
            self.assertTrue(covered_by(path, deny), path)
        # 2.6.6: other papers' records are not; whole trees cost sandbox profile on every call.
        for path in other:
            self.assertEqual(covered_by(path, deny), [], path)
        self.assertEqual(covered_by(root, deny), [], "nothing that holds the audit is denied")
        self.assertEqual(covered_by(tree / "data", deny), [])

    def test_the_tree_root_mirrors_tree_layout_sh(self) -> None:
        run_verifier = importlib.import_module("run_verifier")
        new = self.base / "new"
        legacy = self.base / "legacy"
        (new / "data").mkdir(parents=True)
        (legacy / "reservations").mkdir(parents=True)
        cases = {
            new / "runs/J/P/audit": new,
            legacy / "J/P/audit": legacy,
            self.base / "scratch/J/P/audit": None,       # no marker: a scratch parent is no tree
            self.base / "audit": None,
            pathlib.Path("/audit"): None,                # too shallow: never an IndexError
            pathlib.Path("/a/audit"): None,
        }
        for audit, expected in cases.items():
            with self.subTest(audit=str(audit)):
                self.assertEqual(run_verifier.tree_root_of_audit(audit), expected)
                if expected is not None:
                    shell = subprocess.run(
                        ["bash", str(SCRIPTS / "tree_layout.sh"), "i4r_root_of_audit", str(audit)],
                        text=True, capture_output=True, check=True).stdout.strip()
                    self.assertEqual(pathlib.Path(shell).resolve(), expected.resolve())

    # docs-5 ------------------------------------------------------------------------------------
    def test_usage_errors_exit_1_not_the_failed_call_code(self) -> None:
        root = self.make_audit()
        positional = subprocess.run([sys.executable, str(SCRIPTS / "run_verifier.py"), str(root), "F13", "F14"],
                                    text=True, capture_output=True, timeout=60, check=False)
        self.assertEqual(positional.returncode, 1, positional.stderr)
        self.assertIn("the following arguments are required: --findings", positional.stderr)
        self.assertIn("usage:", positional.stderr)
        unknown = subprocess.run([sys.executable, str(SCRIPTS / "run_verifier.py"), str(root), "--findings",
                                  "F13", "--no-such-flag"], text=True, capture_output=True, timeout=60, check=False)
        self.assertEqual(unknown.returncode, 1, unknown.stderr)
        # Negative control: --help is not an error.
        shown = subprocess.run([sys.executable, str(SCRIPTS / "run_verifier.py"), "--help"],
                               text=True, capture_output=True, timeout=60, check=False)
        self.assertEqual(shown.returncode, 0, shown.stderr)


class VerifierRecordsAreReadmitted(VerifierFixture):
    """records-1: verify_record re-admits every verifier call through run_verifier.readmit_record.

    Before, a verifier call was admitted when four files hashed to what a receipt said; the
    reviewers' forge.py wrote all four by hand and the record passed make_settled, finalize and
    publish. Now only what the runner produces is re-admitted -- and tests/verifier_fixtures.py,
    which builds records with the runner's own functions, is proved to be exactly that.
    """

    def ledger_view(self, root: pathlib.Path) -> tuple[list[dict], dict, set]:
        ledger = self.ledger(root)
        return ledger, {r["invocation_id"]: r for r in ledger}, verifier_contract.auditor_families(ledger)

    def test_a_real_runner_record_is_readmitted_and_every_forgery_is_refused(self) -> None:
        from tests import verifier_fixtures
        run_verifier = importlib.import_module("run_verifier")
        root = self.audit_after_phase1()
        result = self.run_verifier(root, ["F13", "F14"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        role = self.role_for("F13", "F14")
        receipt_path, receipt = self.receipt(root, role)
        ledger, by_id, families = self.ledger_view(root)
        row = next(r for r in ledger if r.get("role") == role and r["provider_call"])
        # The reviewers' forge.py record, journaled through the real lifecycle API: a receipt
        # without packet, snapshot or bindings, a '{}' manifest and a 'hand made' report. Before
        # the fix verify_record returned {F15's claim hash: CONFIRMED} for it.
        hand = verifier_fixtures.forge_hand_record(root, CLAIMS["F15"])
        ledger, by_id, families = self.ledger_view(root)
        with self.assertRaisesRegex(verifier_contract.VerifierContractError, "not the runner's admitted output"):
            verifier_contract.verify_record(root, by_id[hand["invocation_id"]], by_id, families)
        verdicts, errors = self.verdicts(root)
        self.assertNotIn(self.claim_hash("F15"), verdicts, "a forged CONFIRMED is never a verdict")
        self.assertEqual(len(errors), 1)
        self.assertIn("hand-verifier-1", errors[0])
        self.assertIn("not the runner's", errors[0])
        # Positive control: the runner's own record is re-admitted.
        self.assertEqual(verdicts[self.claim_hash("F13")], [(row["invocation_id"], "CONFIRMED")])
        verifier_contract.verify_record(root, row, by_id, families)
        run_verifier.readmit_record(root, row, receipt)

        # Each forgery of the real record, built on disk and undone.
        packet_file = root / "phase1/role_packets" / receipt["source_snapshot_sha256"] / role / \
            receipt["packet_sha256"] / "ROLE_PACKET.json"
        report_path = root / receipt["report"]
        evidence_path = pathlib.Path(root / row["model_identity_evidence"]["artifact"])

        def receipt_with(**changes):
            return {**receipt, **changes}

        def refused(fragment: str, value: dict | None = None) -> None:
            with self.assertRaisesRegex(verifier_contract.VerifierContractError, fragment):
                run_verifier.readmit_record(root, row, value if value is not None else receipt)

        refused("not the runner's", {**receipt, "notes": "hand"})
        refused("not the runner's", {k: v for k, v in receipt.items() if k != "source_bindings"})
        refused("another source snapshot", receipt_with(source_snapshot_sha256="0" * 64))
        refused("no frozen packet", receipt_with(packet_sha256="0" * 64))
        refused("hypotheses are not the frozen packet", receipt_with(hypotheses=list(reversed(receipt["hypotheses"]))))
        refused("hypotheses are not the frozen packet", receipt_with(hypotheses=[
            {**h, "finding_id_at_run": None} for h in receipt["hypotheses"]]))
        refused("source_bindings", receipt_with(source_bindings=[]))
        for path, fragment in ((packet_file, "immutable verifier packet changed"),
                               (evidence_path, "model identity evidence changed")):
            original, mode = path.read_bytes(), path.stat().st_mode
            path.chmod(mode | 0o200)
            path.write_bytes(original + b" ")
            try:
                refused(fragment)
            finally:
                path.write_bytes(original)
                path.chmod(mode)
        # A hand-edited promoted report, with the receipt rehashed to match: the runner's own
        # admission refuses the tree (verify_record's four-hash check alone would pass it).
        original = report_path.read_bytes()
        report_path.write_text("hand made\n", encoding="utf-8")
        forged = receipt_with(report_sha256=digest(report_path))
        refused("report", forged)
        report_path.write_bytes(original)
        run_verifier.readmit_record(root, row, receipt)
        # Fail closed when the runner cannot be loaded at all.
        with mock.patch.dict(sys.modules, {"run_verifier": None}):
            with self.assertRaisesRegex(verifier_contract.VerifierContractError, "does not load"):
                verifier_contract.verify_record(root, row, by_id, families)

    def test_the_pre_265_prompt_is_byte_identical_to_the_264_prompt(self) -> None:
        # Pinned from 2.6.4's own build_verifier_prompt on this synthetic packet. The first cut of
        # 2.6.5 put the rule on a new line; removing it then left a line break where 2.6.4 had a
        # space, so no 2.6.4 record re-admitted (seen on the seventh production audit's copy).
        run_verifier = importlib.import_module("run_verifier")
        packet = {"source_snapshot_sha256": "a" * 64, "hypotheses_sha256": "b" * 64,
                  "hypotheses": [{"hypothesis_id": "H1", "claim_sha256": "c" * 64},
                                 {"hypothesis_id": "H2", "claim_sha256": "d" * 64}]}
        prompt = run_verifier.build_verifier_prompt("verifier_0123456789ab", "20260101-audit-00000000", packet)
        legacy = run_verifier.pre_265_prompt(prompt)
        self.assertIsNotNone(legacy)
        self.assertEqual(digest_text(legacy),
                         "f6591dfa53f0f370e0bdfcb59a1bbed1f14f117397db59f5788457f819318479")
        self.assertIsNone(run_verifier.pre_265_prompt(legacy), "the statement occurs exactly once")

    def test_a_record_frozen_with_the_pre_265_prompt_is_readmitted(self) -> None:
        # 2.6.5 inserted the participant-code rule into the verifier prompt, and the frozen packet
        # hash covers the prompt's SHA-256. A record made on 2.6.4 must still re-admit, so that
        # make_settled, integrate_round and finalize keep working on audits started before the
        # upgrade; a prompt that is neither the current nor the pre-2.6.5 one is still refused.
        from tests import verifier_fixtures
        run_verifier = importlib.import_module("run_verifier")
        current = run_verifier.build_verifier_prompt
        root = self.audit_after_phase1()

        def pre_265(role, audit_id, packet):
            legacy = run_verifier.pre_265_prompt(current(role, audit_id, packet))
            self.assertIsNotNone(legacy)
            return legacy

        with mock.patch.object(run_verifier, "build_verifier_prompt", pre_265):
            info = verifier_fixtures.record_verifier(root, [CLAIMS["F13"]], ["CONFIRMED"], finding_ids=["F13"])
        ledger, by_id, families = self.ledger_view(root)
        row = by_id[info["invocation_id"]]
        _path, receipt = self.receipt(root, info["role"])
        run_verifier.readmit_record(root, row, receipt)
        verifier_contract.verify_record(root, row, by_id, families)
        verdicts, errors = self.verdicts(root)
        self.assertEqual(errors, [])
        self.assertEqual(verdicts[self.claim_hash("F13")], [(info["invocation_id"], "CONFIRMED")])
        # Negative control: another prompt text does not hash to the frozen packet.
        with mock.patch.object(run_verifier, "build_verifier_prompt",
                               lambda role, audit_id, packet: current(role, audit_id, packet) + " x"):
            with self.assertRaisesRegex(verifier_contract.VerifierContractError, "do not hash"):
                run_verifier.readmit_record(root, row, receipt)
        # The current prompt differs from the pre-2.6.5 one by exactly the rule statement.
        packet = json.loads((root / "phase1/role_packets" / receipt["source_snapshot_sha256"] / info["batch_role"]
                             / receipt["packet_sha256"] / "ROLE_PACKET.json").read_text(encoding="utf-8"))
        prompt = current(info["batch_role"], json.loads((root / "run.json").read_text())["audit_id"], packet)
        self.assertEqual(prompt.count(phase1_runtime.provider_safety.RULE_STATEMENT), 1)
        self.assertNotEqual(run_verifier.packet_contract_hash(packet, prompt), receipt["packet_sha256"])

    def test_the_fixture_helper_makes_the_runners_record_in_a_real_phase1_audit(self) -> None:
        from tests import verifier_fixtures
        root = self.audit_after_phase1()
        info = verifier_fixtures.record_verifier(
            root, [CLAIMS["F13"]], ["REFUTED"], finding_ids=["F13"], locator="Table 2, second row")
        ledger, by_id, families = self.ledger_view(root)
        _path, receipt = self.receipt(root, info["role"])
        importlib.import_module("run_verifier").readmit_record(root, by_id[info["invocation_id"]], receipt)
        verdicts, errors = self.verdicts(root)
        self.assertEqual(errors, [])
        self.assertEqual(verdicts[self.claim_hash("F13")], [(info["invocation_id"], "REFUTED")])
        # The runner itself takes the helper's record for its own: the same batch replays it with
        # zero provider calls, and any other batch carrying the claim is refused.
        starts = len(self.starts())
        replay = self.run_verifier(root, ["F13"])
        self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)
        self.assertEqual(json.loads(replay.stdout)["status"], "replayed")
        self.assertEqual(json.loads(replay.stdout)["hypotheses"][0]["verdict"], "REFUTED")
        self.assertEqual(len(self.starts()), starts)
        self.assert_refused(self.run_verifier(root, ["F13", "F15"]), "already has an admitted verifier verdict")

    def test_os_metadata_in_a_promoted_tree_does_not_make_a_sound_record_inadmissible(self) -> None:
        """A person browsing work/verifier/ in Finder leaves a .DS_Store behind; that is not a
        forged file. Any other extra file still breaks the closed set (negative control)."""
        from tests import test_264_verifier_confirmation as confirmation
        from tests import verifier_fixtures
        results = {}
        for name, extra in (("finder", ".DS_Store"), ("appledouble", "evidence/._manifest.json"),
                            ("stray", "notes.txt")):
            root = self.base / f"osmeta-{name}"
            confirmation.build_template(root)
            info = verifier_fixtures.record_verifier(
                root, [confirmation.F13_CLAIM], ["CONFIRMED"], finding_ids=["F13"],
                started_at=confirmation.T_VERIFIER[0], ended_at=confirmation.T_VERIFIER[1])
            target = root / info["output_dir"] / extra
            target.write_bytes(b"\x00\x05\x16\x07 metadata")
            results[name] = self.verdicts(root)
        f13 = verifier_contract.claim_sha256(confirmation.F13_CLAIM)
        for name in ("finder", "appledouble"):
            verdicts, errors = results[name]
            self.assertEqual(errors, [], name)
            self.assertEqual([v for _, v in verdicts[f13]], ["CONFIRMED"], name)
        verdicts, errors = results["stray"]
        self.assertEqual(verdicts, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("not the runner's admitted output", errors[0])

    def test_the_fixture_helper_works_without_a_real_phase1_and_makes_each_malformed_variant(self) -> None:
        from tests import test_264_verifier_confirmation as confirmation
        from tests import verifier_fixtures
        template = self.base / "template"
        confirmation.build_template(template)       # the test_diff_packet fixture: no phase1/
        self.assertFalse((template / "phase1").exists())

        def fresh(name: str) -> pathlib.Path:
            target = self.base / name
            shutil.copytree(template, target, symlinks=True)
            return target

        def record(root: pathlib.Path, claims: list[str], verdicts: list[str], **options) -> dict:
            return verifier_fixtures.record_verifier(
                root, claims, verdicts, finding_ids=[f"F{13 + i}" for i in range(len(claims))],
                started_at=confirmation.T_VERIFIER[0], ended_at=confirmation.T_VERIFIER[1], **options)

        f13 = verifier_contract.claim_sha256(confirmation.F13_CLAIM)
        root = fresh("positive")
        info = record(root, [confirmation.F13_CLAIM], ["CONFIRMED"])
        self.assertTrue((root / "phase1/quorum.json").is_file())
        verdicts, errors = self.verdicts(root)
        self.assertEqual((verdicts, errors), ({f13: [(info["invocation_id"], "CONFIRMED")]}, []))
        row = next(r for r in self.ledger(root) if r["invocation_id"] == info["invocation_id"])
        self.assertEqual((row["role_class"], row["round"], row["ended_at"]),
                         ("verifier", 0, confirmation.T_VERIFIER[1]))
        self.assertEqual(info["result_sha256"], digest(root / info["result"]))
        # A second batch on another claim in the same audit reuses the quorum it wrote.
        second = record(root, [confirmation.OTHER_CLAIM], ["UNVERIFIABLE"])
        self.assertEqual(second["snapshot_sha256"], info["snapshot_sha256"])
        self.assertEqual(self.verdicts(root)[1], [])
        variants = {
            "no-receipt": ({"receipt": False}, "completion receipt is not a regular file"),
            "tampered-result": ({"tamper": "result"}, "changed after it was recorded"),
            "tampered-report": ({"tamper": "report"}, "changed after promotion"),
            "tampered-packet": ({"tamper": "packet"}, "immutable verifier packet changed"),
            "tampered-artifact": ({"tamper": "evidence_artifact"}, "hash/size mismatch"),
            "hand-receipt": ({"tamper": "receipt"}, "not the runner's"),
            "wrong-family": ({"family": "gpt"}, "is not the Phase-1 auditor family"),
        }
        for name, (options, fragment) in variants.items():
            with self.subTest(variant=name):
                root = fresh(name)
                info = record(root, [confirmation.F13_CLAIM], ["CONFIRMED"], **options)
                verdicts, errors = self.verdicts(root)
                self.assertNotIn(f13, verdicts)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(fragment, errors[0])
        # A legacy_record correction keeps a REFUTED blocking and never lets a CONFIRMED count.
        root = fresh("legacy")
        refuted = record(root, [confirmation.F13_CLAIM], ["REFUTED"], legacy_correction="superseded")
        confirmed = record(root, [confirmation.OTHER_CLAIM], ["CONFIRMED"], legacy_correction="superseded")
        self.assertEqual({r["invocation_id"]: r["status"] for r in self.ledger(root)
                          if r.get("role_class") == "verifier"},
                         {refuted["invocation_id"]: "legacy_record", confirmed["invocation_id"]: "legacy_record"})
        verdicts, errors = self.verdicts(root)
        self.assertEqual((verdicts, errors), ({f13: [(refuted["invocation_id"], "REFUTED")]}, []))
        # The Neudorfer relabelled shape is never a verifier.
        root = fresh("relabel")
        record(root, [confirmation.F13_CLAIM], ["CONFIRMED"], relabel=("operator_verifier", "other"))
        self.assertEqual(self.verdicts(root), ({}, []))

    def test_the_fixture_helper_record_finalizes_clean_in_the_release_pipeline_audit(self) -> None:
        from tests import test_release_pipeline, verifier_fixtures
        audit = test_release_pipeline.make_audit(self.base / "release-fixture")
        self.assertFalse((audit / "phase1").exists())
        info = verifier_fixtures.record_verifier(
            audit, ["The abstract reports 24 participants; the participant table lists 22."],
            finding_ids=["F3"], started_at="2026-08-27T10:21:00Z", ended_at="2026-08-27T10:25:00Z")
        self.assertEqual(self.verdicts(audit)[1], [])
        env = os.environ.copy()
        env.update({"LC_ALL": "C", "I4R_RESERVE_GRACE": "0"})
        env.pop("I4R_MAX_ATTEMPTS", None)
        (self.base / "_registry").mkdir(parents=True, exist_ok=True)   # finalize writes only into an existing registry
        final = subprocess.run(["/bin/bash", str(test_release_pipeline.FINALIZE), str(audit),
                                "--stopping-reason", "target_complete", "--registry", str(self.base / "_registry")],
                               text=True, capture_output=True, env=env, timeout=300, check=False)
        self.assertEqual(final.returncode, 0, final.stdout[-3000:] + final.stderr[-3000:])
        compliance = json.loads((audit / "run.json").read_text())["compliance"]
        self.assertEqual((compliance["compliance_pass"], compliance["verifiers_ok"], compliance["violations"]),
                         (True, True, []))
        self.assertEqual([call["invocation_id"] for call in compliance["verifier_calls"]], [info["invocation_id"]])


def run_phase1_argv_add_dirs(argv: list[str]) -> list[str]:
    index = argv.index("--add-dir")
    return [argv[index + 1], argv[index + 2]]


if __name__ == "__main__":
    unittest.main()
