"""2.7.9: the coordinator refused citations of the audit context its prompt tells it to cite.

The runtime prompt says "Cite every evidence path by its `origin` value in input_manifest.json", and
input_manifest.json lists PAPER.txt, package_manifest.json, the safe-evidence manifest, run.json and
the policy snapshot, each with a sha256. ``known_evidence`` bound none of them, so four coordinator
attempts (audits A86, A88, A87, A89) were failed after long runs for citing
package_manifest.json or work/safe_evidence/manifest.json with the very hash the manifest gave.

PAPER.txt, the safe-evidence manifest and package_manifest.json are now bound at their live hash,
the package manifest only when its snapshot copy is its own bytes (no name masked). run.json and
pipeline-policy.snapshot.json stay refused, now by name: their bytes change later (every journal
event, amend_round_cap.py, repin_critic_route.py), and every later reader re-hashes what a finding
cites. Everything runs through the real runner with the fake CLI (FAKE_COORDINATOR_CITE in
tests/fake_claude_coordinator.py), and an admitted citation is then put through the real round-1
critique snapshot and the finalize-time re-hash. recover_misplaced_coordinator_outputs.py judges a
response with the runner's snapshot-to-origin map too (run for real, not --dry-run), and INSTALL's
remedies (a raised I4R_MAX_ATTEMPTS, restoring a cited file from the snapshot) are run as written.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import unittest
from unittest import mock

TESTS = pathlib.Path(__file__).resolve().parent
SKILL = TESTS.parent
SCRIPTS = SKILL / "scripts"
for _path in (str(SCRIPTS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import generate_finding_artifacts  # noqa: E402
import record_invocation  # noqa: E402
import run_coordinator  # noqa: E402
import test_270_claude_routes as t270  # noqa: E402  (the misplaced-attempt fixture only)
import test_coordinator_manifest_masking as masking  # noqa: E402  (deposit fixtures only)
import test_run_coordinator as trc  # noqa: E402  (fixtures only; its tests stay in its own module)

SAFE_MANIFEST = "work/safe_evidence/manifest.json"
STABLE_CONTEXT = ("PAPER.txt", SAFE_MANIFEST, "package_manifest.json")


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plain_deposit() -> dict[str, str]:
    return {"data/scores.csv": "condition,score\nA,1.5\nB,2.5\n", "analysis.R": "read.csv('data/scores.csv')\n"}


class CoordinatorFixture(unittest.TestCase):
    setUp = trc.CoordinatorRunnerTests.setUp
    tearDown = trc.CoordinatorRunnerTests.tearDown
    run_runner = trc.CoordinatorRunnerTests.run_runner
    attempt = trc.CoordinatorRunnerTests.attempt

    def cite(self, citations: list[dict]) -> subprocess.CompletedProcess:
        with mock.patch.dict(os.environ, {"FAKE_COORDINATOR_CITE": json.dumps(citations)}):
            return self.run_runner()

    def promoted(self) -> dict:
        return json.loads((self.root / "work/findings.jsonl").read_text().splitlines()[0])

    def refusal(self, result: subprocess.CompletedProcess) -> str:
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertFalse((self.root / "work/findings.jsonl").exists(), "nothing is promoted")
        return json.loads(result.stdout.strip().splitlines()[-1])["error"]

    def listed_sha(self, origin: str) -> str:
        """The sha256 input_manifest.json gave the model for ``origin``."""
        manifest = json.loads((self.attempt() / "source/input_manifest.json").read_text())
        return next(entry["sha256"] for entry in manifest["files"] if entry["origin"] == origin)


class StableContextIsCitable(CoordinatorFixture):
    def test_each_stable_context_origin_is_admitted_at_its_live_hash(self) -> None:
        masking.write_deposit(self.root, plain_deposit())
        result = self.cite([{"path": origin, "origin": origin} for origin in STABLE_CONTEXT]
                           + [{"dependency": SAFE_MANIFEST}, {"dependency": "package_manifest.json"}])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        row = self.promoted()
        for origin in STABLE_CONTEXT:
            self.assertIn({"path": origin, "sha256": sha(self.root / origin)}, row["evidence"]["source_hashes"])
            self.assertEqual(self.listed_sha(origin), sha(self.root / origin), "the hash the model was given")
        self.assertIn(SAFE_MANIFEST, row["dependencies"]["artifact_paths"])
        self.assertIn("package_manifest.json", row["dependencies"]["artifact_paths"])

    def test_the_snapshot_spelling_is_stored_as_the_origin(self) -> None:
        result = self.cite([
            {"path": "context/PAPER.txt", "origin": "PAPER.txt"},
            {"path": "evidence/SAFE_EVIDENCE.json", "origin": SAFE_MANIFEST},
            {"path": "context/package_manifest.json", "origin": "package_manifest.json"},
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        cited = {item["path"] for item in self.promoted()["evidence"]["source_hashes"]}
        self.assertTrue(set(STABLE_CONTEXT) <= cited, cited)
        self.assertFalse(cited & {"context/PAPER.txt", "evidence/SAFE_EVIDENCE.json",
                                  "context/package_manifest.json"})

    def test_the_context_is_bound_without_the_input_manifest(self) -> None:
        # recover_coordinator.py calls known_evidence without the input manifest (the runner already
        # rewrote its stored outputs), so the binding may not depend on it. The misplaced-output
        # recovery is run for real in MisplacedRecoveryJudgesLikeTheRunner.
        safe = run_coordinator.load_safe_evidence(self.root.resolve(), self.safe_manifest)
        pairs, paths, _canonical = run_coordinator.known_evidence([], {}, self.root.resolve(), safe)
        for origin in STABLE_CONTEXT:
            self.assertIn((origin, sha(self.root / origin)), pairs)
            self.assertIn(origin, paths)
        self.assertNotIn(("PAPER.txt", "0" * 64), pairs, "the binding is to the exact live hash")
        self.assertFalse({"run.json", "pipeline-policy.snapshot.json"} & paths)


class UncitableContextIsRefusedByName(CoordinatorFixture):
    def test_run_json_is_refused_as_audit_metadata(self) -> None:
        error = self.refusal(self.cite([{"path": "run.json", "origin": "run.json"}]))
        self.assertIn("cites an unbound evidence hash: run.json (run.json is audit metadata, not evidence", error)
        self.assertIn("cite the paper/, pkg/, report/ or work/ file the finding rests on", error)

    def test_run_json_as_a_dependency_or_in_its_snapshot_spelling_is_refused_too(self) -> None:
        error = self.refusal(self.cite([{"dependency": "context/run.json"}]))
        self.assertIn("cites an unbound dependency: context/run.json (run.json is audit metadata", error)

    def test_the_policy_snapshot_is_refused_as_audit_policy(self) -> None:
        origin = "pipeline-policy.snapshot.json"
        error = self.refusal(self.cite([{"path": origin, "origin": origin}]))
        self.assertIn(f"cites an unbound evidence hash: {origin} ({origin} is audit policy, not evidence: "
                      "amend_round_cap.py and repin_critic_route.py rewrite it", error)

    def test_a_masked_package_manifest_stays_refused(self) -> None:
        live = masking.write_deposit(self.root, masking.code_named_deposit())
        error = self.refusal(self.cite([{"path": "package_manifest.json", "origin": "package_manifest.json"}]))
        self.assertNotEqual(self.listed_sha("package_manifest.json"), sha(self.root / "package_manifest.json"),
                            "the model was shown the masked projection")
        self.assertIn("cites an unbound evidence hash: package_manifest.json (the package_manifest.json this "
                      "coordinator was shown masks participant-code-shaped names", error)
        # Nor is the file's own hash admitted: the model was given the projection's hash and never
        # saw the file's.
        error = self.refusal(self.cite([{"path": "package_manifest.json",
                                         "sha256": hashlib.sha256(live).hexdigest()}]))
        self.assertIn("cites an unbound evidence hash: package_manifest.json (the package_manifest.json", error)

    def test_an_unbound_path_is_still_refused_as_before(self) -> None:
        # PAPER.txt is admitted, so the refusal names the invented path, in the 2.7.8 words exactly.
        error = self.refusal(self.cite([{"path": "PAPER.txt", "origin": "PAPER.txt"},
                                        {"path": "work/invented.json", "sha256": "a" * 64}]))
        self.assertTrue(error.endswith("cites an unbound evidence hash: work/invented.json"), error)


class AdmittedContextSurvivesDownstream(CoordinatorFixture):
    """Every later reader re-hashes a cited path against the live audit; an admitted citation must pass."""

    def round_one_snapshot(self) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
        critics = record_invocation.load_policy_snapshot(self.root)["models"]["critics"]
        routing = self.root / "routing.json"
        routing.write_text(json.dumps({"claim_assignment_mode": "domain_routed_round_1",
                                       "claim_assignments": {"stats": ["F1"]}, "reason": "test"}))
        (self.root / "WRAPUP.txt").write_text("No wrap-up yet.\n")
        out = self.root / "critique/round_1"
        out.mkdir(parents=True)
        result = subprocess.run([
            sys.executable, str(SCRIPTS / "test_run_critique_runtime.py"), "snapshot",
            "--workspace", str(self.root), "--report", str(self.root / "report/REPORT_final.md"),
            "--prompts", str(SKILL / "prompts"), "--out", str(out),
            "--model", critics["requested_model"], "--effort", critics["reasoning_effort"],
            "--roles", "stats", "--routing", str(routing),
            "--runtime-script", str(SCRIPTS / "run_critique.sh"), "--round", "1",
        ], capture_output=True, text=True, check=False)
        return result, out

    def test_the_round_one_snapshot_and_finalize_re_hash_accept_the_citation(self) -> None:
        masking.write_deposit(self.root, plain_deposit())
        admitted = self.cite([{"path": origin, "origin": origin} for origin in STABLE_CONTEXT])
        self.assertEqual(admitted.returncode, 0, admitted.stderr + admitted.stdout)
        result, out = self.round_one_snapshot()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        source = pathlib.Path(json.loads(result.stdout)["snapshot"]) / "source"
        for origin in STABLE_CONTEXT:
            self.assertEqual(sha(source / "evidence" / origin), sha(self.root / origin))
        approved = copy.deepcopy(self.promoted())
        approved.update(status="confirmed", coordinator_approved=True, report_section="Section 3")
        final = generate_finding_artifacts.ensure_final_invariants([approved], self.root, quiet=True)
        self.assertEqual([row["id"] for row in final], ["F1"])

    def test_a_changed_cited_file_is_restored_from_the_coordinator_snapshot(self) -> None:
        # INSTALL's remedy: the completed attempt's source/ holds the bytes the citation was bound
        # to, at the snapshot path its input_manifest.json lists for the origin.
        admitted = self.cite([{"path": "PAPER.txt", "origin": "PAPER.txt"}])
        self.assertEqual(admitted.returncode, 0, admitted.stderr + admitted.stdout)
        paper = self.root / "PAPER.txt"
        paper.write_text(paper.read_text() + "An operator's later edit.\n")
        stopped, _out = self.round_one_snapshot()
        self.assertNotEqual(stopped.returncode, 0)
        self.assertIn("referenced evidence hash changed: PAPER.txt", stopped.stderr + stopped.stdout)
        manifest = json.loads((self.attempt() / "source/input_manifest.json").read_text())
        snapshot = next(entry["snapshot"] for entry in manifest["files"] if entry["origin"] == "PAPER.txt")
        self.assertEqual(snapshot, "context/PAPER.txt")
        paper.write_bytes((self.attempt() / "source" / snapshot).read_bytes())
        shutil.rmtree(self.root / "critique/round_1")
        result, _out = self.round_one_snapshot()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


class ExhaustedBudgetAfterARefusedCitation(unittest.TestCase):
    """INSTALL's remedy for a coordinator whose three attempts were refused for a context citation:
    the plain rerun is refused, neither recovery tool applies, and a raised I4R_MAX_ATTEMPTS buys
    one more (paid) call, which a 2.7.9 citation then passes."""

    def test_a_fourth_attempt_needs_a_raised_ceiling_and_no_recovery_tool_applies(self) -> None:
        fixture = t270.coordinator_on_route(self, t270.SONNET_5_5_XHIGH, metadata21=True)
        environment = {key: value for key, value in os.environ.items() if key != "I4R_MAX_ATTEMPTS"}
        refused = json.dumps([{"path": "run.json", "origin": "run.json"}])
        with mock.patch.dict(os.environ, {**environment, "FAKE_COORDINATOR_CITE": refused}, clear=True):
            for _ in range(3):
                self.assertEqual(fixture.run_runner().returncode, 2)
            exhausted = fixture.run_runner()
        self.assertEqual(exhausted.returncode, 1, exhausted.stderr + exhausted.stdout)
        self.assertIn("3-attempt coordinator provider budget is exhausted (raise with I4R_MAX_ATTEMPTS",
                      exhausted.stderr + exhausted.stdout)
        attempts = sorted((fixture.root / "coordinator/invocations").glob("*/attempt_*"))
        self.assertEqual(len(attempts), 3)
        for tool in ("recover_coordinator.py", "recover_misplaced_coordinator_outputs.py"):
            recovery = subprocess.run(
                [sys.executable, str(SCRIPTS / tool), str(fixture.root),
                 "--attempt", attempts[-1].relative_to(fixture.root).as_posix()],
                text=True, capture_output=True, timeout=120, env={**environment, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(recovery.returncode, 1, tool + recovery.stderr + recovery.stdout)
        cited = json.dumps([{"path": "PAPER.txt", "origin": "PAPER.txt"}])
        with mock.patch.dict(os.environ, {**environment, "FAKE_COORDINATOR_CITE": cited,
                                          "I4R_MAX_ATTEMPTS": "4"}, clear=True):
            fourth = fixture.run_runner()
        self.assertEqual(fourth.returncode, 0, fourth.stderr + fourth.stdout)
        self.assertEqual([(row["attempt"], row["status"]) for row in fixture.ledger()
                          if row.get("role") == "coordinator"],
                         [(1, "failed"), (2, "failed"), (3, "failed"), (4, "completed")])


class MisplacedRecoveryJudgesLikeTheRunner(unittest.TestCase):
    """recover_misplaced_coordinator_outputs.py re-runs validate_outputs on a response the runner
    never got to see. It passed no snapshot-to-origin map, so a citation the runner rewrites and
    admits (context/PAPER.txt, and since 2.4.3 reports/01_reproduction.md) was refused there, after
    --dry-run had said preconditions_ok. Each case is a real misplaced attempt (test_270's fixture:
    run_coordinator.py, the fake CLI writing into the attempt root, the attempt frozen read-only)
    and the tool's real run, not --dry-run."""

    misplaced_attempt = t270.MisplacedOutputRecoveryReadsTheSnapshotRoute.misplaced_attempt

    def recover(self, citations: list[dict]):
        with mock.patch.dict(os.environ, {"FAKE_COORDINATOR_CITE": json.dumps(citations)}):
            fixture, attempt = self.misplaced_attempt(t270.SONNET_5_5)
        moved = {name: (attempt / name).read_bytes() for name in run_coordinator.OUTPUT_TARGETS}
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "recover_misplaced_coordinator_outputs.py"), str(fixture.root),
             "--attempt", attempt.relative_to(fixture.root).as_posix()],
            text=True, capture_output=True, timeout=120, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return fixture, attempt, moved, result

    def test_a_snapshot_spelled_citation_is_recovered_as_the_runner_stores_it(self) -> None:
        fixture, attempt, moved, result = self.recover([
            {"path": "context/PAPER.txt", "origin": "PAPER.txt"},
            {"path": "evidence/SAFE_EVIDENCE.json", "origin": SAFE_MANIFEST},
            {"path": "context/package_manifest.json", "origin": "package_manifest.json"},
            {"path": "reports/01_reproduction.md", "origin": "report/01_reproduction.md"},
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["status"], "completed")
        root = fixture.root
        stored = root / "work/findings.jsonl"
        cited = {item["path"]: item["sha256"]
                 for item in json.loads(stored.read_text().splitlines()[0])["evidence"]["source_hashes"]}
        for origin in (*STABLE_CONTEXT, "report/01_reproduction.md"):
            self.assertEqual(cited.get(origin), sha(root / origin), origin)
        self.assertFalse({"context/PAPER.txt", "evidence/SAFE_EVIDENCE.json", "context/package_manifest.json",
                          "reports/01_reproduction.md"} & set(cited),
                         "the registry holds the origin spelling, as the runner stores it")
        row = next(r for r in fixture.ledger() if r["invocation_id"] == attempt.name.split("_", 2)[2])
        self.assertEqual(row["status"], "completed")
        # The receipt: relocated_outputs hashes the bytes the model wrote and the tool moved;
        # outputs (and the bundle the ledger binds) hash the rewritten bytes that were stored.
        receipt = json.loads(next((root / "coordinator/recoveries").glob("*/receipt.json")).read_text())
        relocated = {entry["name"]: entry["sha256"] for entry in receipt["relocated_outputs"]}
        self.assertEqual(relocated, {name: hashlib.sha256(data).hexdigest() for name, data in moved.items()})
        self.assertEqual(receipt["outputs"]["findings.jsonl"]["sha256"], sha(stored))
        self.assertNotEqual(relocated["findings.jsonl"], sha(stored), "the citations were rewritten")
        bundle = json.loads((attempt / "coordinator_bundle.json").read_text())
        self.assertEqual(bundle["outputs"]["findings.jsonl"]["sha256"], sha(stored))
        self.assertEqual((attempt / "output.partial/findings.jsonl").stat().st_mode & 0o777, 0o444,
                         "the attempt is frozen again")

    def test_a_refusal_after_the_rewrite_puts_back_the_bytes_the_model_wrote(self) -> None:
        # The snapshot spelling is rewritten first, then the invented hash refuses the response.
        fixture, attempt, moved, result = self.recover([
            {"path": "context/PAPER.txt", "origin": "PAPER.txt"},
            {"path": "work/invented.json", "sha256": "a" * 64},
        ])
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertTrue(result.stderr.strip().endswith("cites an unbound evidence hash: work/invented.json"),
                        result.stderr)
        for name, data in moved.items():
            self.assertEqual((attempt / name).read_bytes(), data, name)
            self.assertEqual((attempt / name).stat().st_mode & 0o777, 0o444, name)
            self.assertFalse((attempt / "output.partial" / name).exists(), name)
        self.assertFalse((fixture.root / "work/findings.jsonl").exists())
        self.assertEqual(fixture.ledger()[-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
