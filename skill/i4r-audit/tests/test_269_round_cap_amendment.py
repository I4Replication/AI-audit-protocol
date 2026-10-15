"""2.6.9: the critique round cap raised from 5 to 7, and the journaled way an existing audit gets it.

The canonical cap is frozen into each audit's policy snapshot at init, so raising it helps new audits
only. These tests hold both sides of that line, through the real driver, tools, journal and finalize
code, with the fake Codex CLI and no provider call:

* ``DriverCap`` -- round 6 on an unamended audit initialised with the old cap of 5 is refused before
  anything is written; after ``amend_round_cap.py`` it runs to quorum; round 8 is refused on cap 7; a
  finalized audit runs no round until a journaled reopen.
* ``AmendTool`` -- every refusal of the tool writes nothing; the dry run writes nothing; the amendment
  archives the replaced snapshot read-only, rebinds the run and cannot be repeated.
* ``HandLoggedEvents`` -- ``policy_amendment``, ``audit_reopened`` and the fields they own cannot be
  logged by hand, through ``log_event.sh`` or the journal CLI.
* ``VerifierNegativeControls`` -- ``round_cap_amendment.verify`` (finalize and publication) refuses a
  hand-edited cap, a missing or altered archive, an amendment that changed more than the cap, a round
  prepared above a cap before its amendment, a plan bound to the wrong snapshot, a forged event, and a
  round run after a finalize with no reopen.
* ``ValidatorsAcceptTheLiveCaps`` -- ``validate_v2``, ``run.schema.json`` and the post-delta receipt
  schema admit 5 and 7 (and rounds 6-7), and nothing else.
* ``FinalizeAcceptsTheCapOnlyAtTheEffectiveCap`` -- finalize's own derivation code: cap_with_unresolved
  at round 7 of an amended audit passes, at 6 (or at 5 once amended) it is a violation.
* ``ReopenedPublishedAudit`` -- a published audit is reopened only after its filed set is moved and the
  paper re-claimed; re-finalized, it files a new registry version whose receipt supersedes the old one.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SKILL / "schemas" / "tools"))
import lifecycle_journal  # noqa: E402
import record_invocation  # noqa: E402
import round_cap_amendment as rca  # noqa: E402
import validate_json_schema  # noqa: E402
import validate_v2  # noqa: E402

DRIVER = SCRIPTS / "run_critique.sh"
FAKE_CODEX = TESTS / "fake_codex.sh"
INIT = SCRIPTS / "init_run.sh"
AMEND = SCRIPTS / "amend_round_cap.py"
REOPEN = SCRIPTS / "reopen_audit.py"
LOG_EVENT = SCRIPTS / "log_event.sh"
JOURNAL_CLI = SCRIPTS / "lifecycle_journal.py"
FINALIZE = SCRIPTS / "finalize_run.sh"
PUBLISH = SCRIPTS / "publish_completed.sh"
RESERVE = SCRIPTS / "reserve_paper.sh"
RECEIPT = SCRIPTS / "finalization_receipt.py"
POLICY = SKILL / "config" / "pipeline-policy-v2.json"
LEGACY_STOP = ("stop at round 5 and record every unresolved item and every section changed after its "
               "last adversarial review")


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def run(command: list[str], *, env: dict[str, str] | None = None, cwd: pathlib.Path | None = None,
        timeout: int = 300) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    return subprocess.run(command, text=True, capture_output=True, env=environment, cwd=cwd, timeout=timeout)


def legacy_policy() -> dict:
    """The shipped policy as an audit initialised on 2.6.8 or earlier froze it: cap 5, the old stop text,
    and no amendment contract."""
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["critique"]["round_cap"] = 5
    policy["critique"].pop("round_cap_amendment", None)
    policy["critique"]["stopping"]["cap_with_unresolved"] = LEGACY_STOP
    return policy


def write_snapshot(audit: pathlib.Path, policy: dict) -> None:
    (audit / "pipeline-policy.snapshot.json").write_bytes(rca.envelope_bytes(policy))


def write_audit(audit: pathlib.Path, *, cap: int, audit_id: str, protocol: str = "2.6.4") -> pathlib.Path:
    """A metadata-2.1 critique workspace (test_run_critique.sh's fixture), initialised with ``cap``."""
    for sub in ("report", "work", "pkg"):
        (audit / sub).mkdir(parents=True, exist_ok=True)
    (audit / "PAPER.txt").write_text("Example et al. (2026), deterministic runtime fixture.\n")
    (audit / "ENV.txt").write_text("python3 standard-library-only fixture\n")
    (audit / "SLOW.txt").write_text("Do not run any long job.\n")
    (audit / "WRAPUP.txt").write_text("Review the current changed claims and their dependencies.\n")
    report = audit / "report/REPORT_final.md"
    report.write_text("# Fixture report\n\nThe immutable audit draft contains one bounded claim.\n")
    row = {
        "schema_version": "2.0.0", "id": "F1", "claim": "The bounded fixture claim is correct.",
        "domain": "statistics", "source_location": {"path": "report/REPORT_final.md", "locator": "fixture claim"},
        "claim_ref": "fixture claim", "status": "draft",
        "ledger_disposition": {"status": "INCLUDED", "target": None, "reason": None},
        "coordinator_approved": False, "impact": "value_wrong", "impact_rank": 2,
        "verification_tier": 2, "paper_only": False, "confidence": "LIKELY",
        "evidence": {"source_hashes": [{"path": "report/REPORT_final.md", "sha256": sha(report)}],
                     "rerun_command": "sha256 report/REPORT_final.md", "expected": "bound fixture",
                     "observed": "bound fixture", "tolerance_basis": None},
        "unverifiable_basis": None, "verifications": [],
        "dependencies": {"environment_fingerprint": None, "finding_ids": [], "artifact_paths": []},
        "settled": None, "benign_explanations_ruled_out": [], "report_section": None,
        "verification_snippet": None, "disclosure_flag": False,
        "remedy": {"class": "one_number", "description": "Retain the bounded value.", "verified": None},
        "changes_conclusion": False, "changes_conclusion_which": None,
        "provenance": {"origin_role": "fixture", "independently_found_by": ["fixture"],
                       "agreed_after_seeing": [], "round_introduced": 0,
                       "source_records": ["report/REPORT_final.md"]},
    }
    (audit / "work/findings.jsonl").write_text(json.dumps(row, sort_keys=True) + "\n")
    if cap == 5:
        write_snapshot(audit, legacy_policy())
    result = run(["bash", str(INIT), str(audit), "--audit-id", audit_id, "--protocol-version", protocol,
                  "--gate-set-version", "test"])
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return audit


def events(audit: pathlib.Path) -> list[dict]:
    return lifecycle_journal.load_events(audit)


def plan(policy_sha: str) -> dict:
    return {"purpose": "I4R adversarial critique round", "provider": "openai", "model_route": "critics=x",
            "data_classes": ["report"], "policy_sha256": policy_sha}


def api_append(audit: pathlib.Path, *specs: dict) -> None:
    """Journal events through the library API, bypassing the tools and the CLI (what a hand edit or an
    older skill could do); the verifier must still see them."""
    lifecycle_journal.append_events(audit, list(specs))


class Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="i4r-269-cap-")).resolve()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for path in [self.work, *self.work.rglob("*")]:
            try:
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))
            except OSError:
                pass
        shutil.rmtree(self.work, ignore_errors=True)

    def legacy(self, name: str = "legacy", protocol: str = "2.6.4") -> pathlib.Path:
        return write_audit(self.work / name, cap=5, audit_id=f"{name}-cap5", protocol=protocol)

    def amended(self, name: str = "amended") -> pathlib.Path:
        audit = self.legacy(name)
        result = self.amend(audit, "--to", "7", "--reason", "fixture: continue past the old cap")
        self.assertEqual(result.returncode, 0, result.stderr)
        return audit

    def amend(self, audit: pathlib.Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return run([sys.executable, str(AMEND), str(audit), *arguments])

    def driver(self, audit: pathlib.Path, number: int, invocation: str, **env: str) -> tuple[int, str]:
        result = run(["bash", str(DRIVER), str(number)], env={
            "AUDIT_DIR": str(audit), "CODEX": str(FAKE_CODEX), "FAKE_CODEX_STATE": str(audit / "fake_state"),
            "FAKE_CODEX_SCENARIO": "success", "I4R_INVOCATION_ID": invocation, "RETRY_BACKOFF_SECONDS": "0",
            "PROBE_LOCK_WAIT_SECONDS": "3", **env}, timeout=600)
        return result.returncode, result.stdout + result.stderr

    @staticmethod
    def calls(audit: pathlib.Path) -> int:
        path = audit / "fake_state/calls_total"
        return int(path.read_text().split()[0]) if path.is_file() else 0

    @staticmethod
    def packet(audit: pathlib.Path, number: int) -> pathlib.Path:
        """A content-bound packet for round ``number``: F1's claim was reworded after the previous round."""
        row = json.loads((audit / "work/findings.jsonl").read_text())
        before = dict(row, claim="The bounded fixture claim had an older wording.")
        (audit / f"work/findings_before_round_{number}.jsonl").write_text(json.dumps(before, sort_keys=True) + "\n")
        (audit / f"work/report_before_round_{number}.md").write_bytes((audit / "report/REPORT_final.md").read_bytes())
        manifest = audit / f"critique/DIFF_MANIFEST_round_{number}.json"
        result = run([sys.executable, str(SCRIPTS / "make_diff_packet.py"), str(audit), "--round", str(number),
                      "--before-report", str(audit / f"work/report_before_round_{number}.md"),
                      "--after-report", str(audit / "report/REPORT_final.md"),
                      "--before-findings", str(audit / f"work/findings_before_round_{number}.jsonl"),
                      "--after-findings", str(audit / "work/findings.jsonl"), "--output", str(manifest)])
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)
        return manifest

    @staticmethod
    def state(audit: pathlib.Path) -> tuple[bytes, bytes, list[str]]:
        return ((audit / "run.events.jsonl").read_bytes(), (audit / "pipeline-policy.snapshot.json").read_bytes(),
                sorted(path.name for path in audit.glob("pipeline-policy.snapshot.superseded-*")))


class DriverCap(Workspace):
    def test_round_6_on_an_unamended_legacy_audit_is_refused_before_anything_is_written(self) -> None:
        audit = self.legacy()
        before = self.state(audit)
        rc, log = self.driver(audit, 6, "r6-refused")
        self.assertEqual(rc, 1, log)
        self.assertIn("round 6 exceeds this audit's round cap of 5", log)
        self.assertIn("scripts/amend_round_cap.py \"$AUDIT_DIR\" --to 7 --reason", log)
        self.assertFalse((audit / "critique").exists(), "refused before the round folder is created")
        self.assertEqual(self.state(audit), before, "nothing journaled, snapshot untouched")
        self.assertEqual(self.calls(audit), 0)
        # the journal refuses the same round under its lock; the audit's own last round is still allowed
        with self.assertRaises(lifecycle_journal.JournalError) as refused:
            lifecycle_journal.prepare_round(audit, 6, plan(sha(audit / "pipeline-policy.snapshot.json")))
        self.assertIn("round cap of 5", str(refused.exception))
        self.assertIsNone(rca.check_round(audit, 5))
        self.assertEqual(self.state(audit), before)

    def test_with_the_journaled_amendment_round_6_runs_and_round_8_is_still_refused(self) -> None:
        audit = self.amended()
        amended_snapshot = sha(audit / "pipeline-policy.snapshot.json")
        manifest = self.packet(audit, 6)
        rc, log = self.driver(audit, 6, "r6-amended", DIFF_MANIFEST=str(manifest))
        self.assertEqual(rc, 0, log)
        status = json.loads((audit / "critique/round_6/ROUND_STATUS.json").read_text())
        self.assertTrue(status["quorum_met"], status)
        self.assertEqual(status["outcome"], "complete")
        prepared = [e for e in events(audit) if e["type"] == "round_prepared"]
        self.assertEqual([e["payload"]["round"] for e in prepared], [6])
        self.assertEqual(prepared[-1]["payload"]["plan"]["policy_sha256"], amended_snapshot,
                         "round 6 binds the amended snapshot")
        frozen = next((audit / "critique/round_6/snapshots").glob("*/source/context/PIPELINE_POLICY.json"))
        self.assertEqual(json.loads(frozen.read_text())["policy"]["critique"]["round_cap"], 7)
        run_record = json.loads((audit / "run.json").read_text())
        self.assertEqual(run_record["round_cap"], 7)
        self.assertEqual(rca.verify(audit), ([], []))
        # the new cap is a cap
        rc, log = self.driver(audit, 8, "r8-refused")
        self.assertEqual(rc, 1, log)
        self.assertIn("round 8 exceeds this audit's round cap of 7", log)
        self.assertNotIn("amend_round_cap.py", log, "the skill ships no higher cap to amend to")
        self.assertFalse((audit / "critique/round_8").exists())

    def test_a_new_audit_gets_the_shipped_cap(self) -> None:
        audit = write_audit(self.work / "new", cap=7, audit_id="new-cap7", protocol="2.6.9")
        snapshot = json.loads((audit / "pipeline-policy.snapshot.json").read_text())["policy"]
        self.assertEqual(snapshot["critique"]["round_cap"], 7)
        self.assertEqual(json.loads((audit / "run.json").read_text())["round_cap"], 7)
        self.assertIsNone(rca.check_round(audit, 7))
        self.assertIn("round cap of 7", rca.check_round(audit, 8) or "")
        # init refuses the old cap for a new audit
        other = self.work / "new-five"
        (other / "pkg").mkdir(parents=True)
        result = run(["bash", str(INIT), str(other), "--audit-id", "x", "--round-cap", "5"])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse((other / "run.json").exists())

    def test_a_finalized_audit_runs_no_round_until_a_journaled_reopen(self) -> None:
        audit = write_audit(self.work / "closed", cap=7, audit_id="closed-1", protocol="2.6.9")
        api_append(audit, {"type": "finalize", "payload": {"status": "complete"}, "source": "finalize_run.sh"})
        before = self.state(audit)
        rc, log = self.driver(audit, 1, "closed-r1")
        self.assertEqual(rc, 1, log)
        self.assertIn("this audit was finalized", log)
        self.assertIn("scripts/reopen_audit.py", log)
        self.assertEqual(self.state(audit), before)
        self.assertEqual(self.calls(audit), 0)
        with self.assertRaises(lifecycle_journal.JournalError):
            lifecycle_journal.prepare_round(audit, 1, plan(sha(audit / "pipeline-policy.snapshot.json")))
        reopened = run([sys.executable, str(REOPEN), str(audit), "--reason", "fixture: one more review"])
        self.assertEqual(reopened.returncode, 0, reopened.stderr)
        record = json.loads((audit / "run.json").read_text())
        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["reopenings"][0]["reason"], "fixture: one more review")
        rc, log = self.driver(audit, 1, "reopened-r1")
        self.assertEqual(rc, 0, log)
        self.assertEqual(rca.verify(audit), ([], []))


class AmendTool(Workspace):
    def assert_refused(self, audit: pathlib.Path, needle: str, *arguments: str) -> None:
        before = self.state(audit)
        result = self.amend(audit, *arguments)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(needle, result.stderr)
        self.assertEqual(self.state(audit), before, f"a refusal wrote something: {arguments}")

    def test_every_refusal_writes_nothing(self) -> None:
        audit = self.legacy()
        reason = ("--reason", "fixture")
        self.assert_refused(audit, "--reason is required", "--to", "7")
        self.assert_refused(audit, "does not raise this audit's cap of 5", "--to", "5", *reason)
        self.assert_refused(audit, "above the cap this skill ships (7)", "--to", "9", *reason)
        self.assert_refused(audit, "must be one of the live caps", "--to", "6", *reason)
        self.assert_refused(audit, "nothing to reopen", "--to", "7", "--reopen", *reason)
        # a live critique driver
        lock = audit / "critique/round_1/.runtime_lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{os.getpid()}\n")
        self.assert_refused(audit, "a critique driver is running", "--to", "7", *reason)
        shutil.rmtree(audit / "critique")
        # a round prepared above the cap (hand-journaled: prepare_round itself refuses it)
        above = shutil.copytree(audit, self.work / "above")
        api_append(above, {"type": "round_prepared", "source": "fixture",
                           "payload": {"round": 6, "plan": plan(sha(above / "pipeline-policy.snapshot.json"))}})
        self.assert_refused(above, "already prepared above the cap of 5", "--to", "7", *reason)
        # a finalized audit without --reopen, and an interrupted finalize with it
        closed = shutil.copytree(audit, self.work / "closed")
        api_append(closed, {"type": "finalize", "payload": {"status": "complete"}, "source": "finalize_run.sh"})
        self.assert_refused(closed, "pass --reopen", "--to", "7", *reason)
        (closed / ".finalize-commit.json").write_text("{}\n")
        self.assert_refused(closed, "interrupted finalize", "--to", "7", "--reopen", *reason)
        # a run that is not live metadata 2.1
        old = shutil.copytree(audit, self.work / "v20")
        record = json.loads((old / "run.json").read_text())
        record["schema_version"] = "2.0.0"
        (old / "run.json").write_text(json.dumps(record))
        self.assert_refused(old, "only a live metadata-2.1 audit", "--to", "7", *reason)

    def test_the_dry_run_writes_nothing(self) -> None:
        audit = self.legacy()
        before = self.state(audit)
        result = self.amend(audit, "--to", "7", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would raise the round cap 5 -> 7", result.stdout)
        self.assertEqual(self.state(audit), before)

    def test_the_amendment_is_archived_bound_and_not_repeatable(self) -> None:
        audit = self.legacy()
        old_bytes = (audit / "pipeline-policy.snapshot.json").read_bytes()
        old_policy = json.loads(old_bytes)["policy"]
        result = self.amend(audit, "--to", "7", "--reason", "fixture: continue past the old cap")
        self.assertEqual(result.returncode, 0, result.stderr)
        archives = list(audit.glob("pipeline-policy.snapshot.superseded-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), old_bytes)
        self.assertEqual(stat.S_IMODE(archives[0].stat().st_mode), 0o400)
        new_policy = record_invocation.load_policy_snapshot(audit)
        self.assertEqual(rca.diff_paths(old_policy, new_policy), {
            "critique.round_cap", "critique.stopping.cap_with_unresolved", "critique.round_cap_amendments"})
        self.assertEqual(new_policy["critique"]["stopping"]["cap_with_unresolved"],
                         json.loads(POLICY.read_text())["critique"]["stopping"]["cap_with_unresolved"])
        entry = new_policy["critique"]["round_cap_amendments"][0]
        self.assertEqual((entry["from"], entry["to"], entry["reason"]), (5, 7, "fixture: continue past the old cap"))
        self.assertEqual(entry["previous_snapshot_sha256"], hashlib.sha256(old_bytes).hexdigest())
        self.assertEqual(entry["archived_snapshot"], archives[0].name)
        record_invocation.validate_policy_binding(audit)
        record = json.loads((audit / "run.json").read_text())
        self.assertEqual(record["round_cap"], 7)
        self.assertEqual(record["toolkit"]["pipeline_policy_snapshot_sha256"], sha(audit / "pipeline-policy.snapshot.json"))
        self.assertEqual([(a["from"], a["to"]) for a in record["policy_amendments"]], [(5, 7)])
        last = events(audit)[-1]
        self.assertEqual((last["type"], last["source"]), ("policy_amendment", "amend_round_cap.py"))
        self.assertEqual(rca.verify(audit), ([], []))
        validate_v2.validate_run(dict(record, stopping_reason=None))
        # idempotence: a second amendment has nothing to raise
        self.assert_refused(audit, "does not raise this audit's cap of 7", "--to", "7", "--reason", "again")


class HandLoggedEvents(Workspace):
    def test_the_tool_only_events_and_fields_cannot_be_logged_by_hand(self) -> None:
        audit = self.legacy()
        before = self.state(audit)
        env = {"RUN_JSON": str(audit / "run.json")}
        for arguments, needle in (
                (["policy_amendment", "leaf=critique.round_cap", "from:=5", "to:=7"], "amend_round_cap.py"),
                (["audit_reopened", "reason=hand"], "reopen_audit.py"),
                (["metadata_correction", 'set:={"round_cap": 7}'], "may not set round_cap"),
                (["metadata_correction", 'set:={"reopenings": []}'], "may not set reopenings"),
                (["metadata_correction", 'set:={"toolkit": {}}'], "may not set toolkit")):
            with self.subTest(arguments=arguments[0]):
                result = run(["bash", str(LOG_EVENT), *arguments], env=env)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(needle, result.stderr)
        batch = self.work / "batch.jsonl"
        batch.write_text(json.dumps({"type": "policy_amendment", "payload": {"leaf": "critique.round_cap"}}) + "\n")
        result = run(["bash", str(LOG_EVENT), "--batch", str(batch)], env=env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = self.work / "payload.json"
        payload.write_text(json.dumps({"reason": "hand"}))
        for command in (["append-kv", str(audit), "audit_reopened", "reason=hand"],
                        ["append-event", str(audit), "policy_amendment", str(payload)],
                        ["append-batch", str(audit), str(batch)]):
            with self.subTest(command=command[0]):
                result = run([sys.executable, str(JOURNAL_CLI), *command])
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("written only by their tool", result.stderr)
        self.assertEqual(self.state(audit), before)
        # negative control: the refusal is targeted, an ordinary correction still goes through
        result = run(["bash", str(LOG_EVENT), "metadata_correction", 'set:={"notes": "fixture note"}'], env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_journal_hint_at_the_cap_names_the_stop_or_the_amendment(self) -> None:
        audit = self.legacy()
        hint = lifecycle_journal.next_round_hint(events(audit), 5, audit)
        self.assertIn("round 5 is this audit's round cap (5)", hint)
        self.assertIn("amend_round_cap.py", hint)
        self.assertEqual(lifecycle_journal.next_round_hint(events(audit), 3, audit), "prepare round 4")


class VerifierNegativeControls(Workspace):
    def copy(self, source: pathlib.Path, name: str) -> pathlib.Path:
        return pathlib.Path(shutil.copytree(source, self.work / name))

    def resign(self, audit: pathlib.Path, policy: dict) -> None:
        """Rewrite the snapshot with a valid envelope and rebind the run to it through the library API."""
        path = audit / "pipeline-policy.snapshot.json"
        path.chmod(0o600)
        write_snapshot(audit, policy)
        toolkit = dict(json.loads((audit / "run.json").read_text())["toolkit"])
        toolkit["pipeline_policy_snapshot_sha256"] = sha(path)
        api_append(audit, {"type": "metadata_correction", "payload": {"set": {"toolkit": toolkit}},
                           "source": "fixture"})

    def violations(self, audit: pathlib.Path) -> str:
        found, _ = rca.verify(audit)
        self.assertTrue(found, "the tampered audit passed the amendment check")
        return " | ".join(found)

    def test_a_clean_amendment_passes_and_each_tamper_is_refused(self) -> None:
        clean = self.amended("clean")
        self.assertEqual(rca.verify(clean), ([], []))

        # a hand-edited cap: re-signed and rebound, but never journaled as an amendment
        hand = self.legacy("hand")
        policy = record_invocation.load_policy_snapshot(hand)
        policy["critique"]["round_cap"] = 7
        self.resign(hand, policy)
        found = self.violations(hand)
        self.assertIn("initialised with 5", found)
        self.assertIn("run.round_cap 5 differs", found)

        # the archived snapshot removed, or altered
        missing = self.copy(clean, "missing")
        for archive in missing.glob("pipeline-policy.snapshot.superseded-*.json"):
            archive.chmod(0o600)
            archive.unlink()
        self.assertIn("archived snapshot", self.violations(missing))
        altered = self.copy(clean, "altered")
        for archive in altered.glob("pipeline-policy.snapshot.superseded-*.json"):
            archive.chmod(0o600)
            envelope = json.loads(archive.read_text())
            envelope["policy"]["critique"]["max_attempts_per_role_total"] = 4
            archive.write_bytes(rca.envelope_bytes(envelope["policy"]))
        self.assertIn("is not the snapshot it replaced", self.violations(altered))

        # the amended snapshot changed beyond the cap afterwards, and a history entry dropped
        widened = self.copy(clean, "widened")
        policy = record_invocation.load_policy_snapshot(widened)
        policy["critique"]["max_attempts_per_role_total"] = 4
        self.resign(widened, policy)
        found = self.violations(widened)
        self.assertIn("changed the policy beyond the cap: critique.max_attempts_per_role_total", found)
        self.assertIn("not the snapshot the last policy_amendment wrote", found)
        dropped = self.copy(clean, "dropped")
        policy = record_invocation.load_policy_snapshot(dropped)
        policy["critique"].pop("round_cap_amendments")
        self.resign(dropped, policy)
        self.assertIn("records 0 round-cap amendment(s) but the journal has 1", self.violations(dropped))

        # a round-6 plan bound to the replaced snapshot after the amendment
        stale_plan = self.copy(clean, "stale-plan")
        archive = next(stale_plan.glob("pipeline-policy.snapshot.superseded-*.json"))
        api_append(stale_plan, {"type": "round_prepared", "source": "fixture",
                                "payload": {"round": 6, "plan": plan(sha(archive))}})
        self.assertIn("round 6's plan", self.violations(stale_plan))

        # a policy_amendment or audit_reopened not written by its tool
        forged = self.copy(clean, "forged")
        amendment = next(e for e in events(forged) if e["type"] == "policy_amendment")
        api_append(forged, {"type": "audit_reopened", "source": "log_event.sh",
                            "payload": {"reason": "hand", "purpose": "continue_critique"}},
                   {"type": "policy_amendment", "source": "log_event.sh", "payload": dict(amendment["payload"])})
        found = self.violations(forged)
        self.assertIn("was not written by scripts/amend_round_cap.py", found)
        self.assertIn("was not written by scripts/reopen_audit.py", found)

    def test_a_round_prepared_above_the_cap_before_the_amendment_is_refused(self) -> None:
        clean = self.amended("clean")
        amendment = next(e for e in events(clean) if e["type"] == "policy_amendment")
        early = self.legacy("early")   # byte-identical snapshot to the one clean replaced
        api_append(early, {"type": "round_prepared", "source": "fixture",
                           "payload": {"round": 6, "plan": plan(sha(early / "pipeline-policy.snapshot.json"))}})
        archive = next(clean.glob("pipeline-policy.snapshot.superseded-*.json"))
        shutil.copy2(archive, early / archive.name)
        (early / "pipeline-policy.snapshot.json").write_bytes((clean / "pipeline-policy.snapshot.json").read_bytes())
        api_append(early, {"type": "policy_amendment", "source": "amend_round_cap.py",
                           "payload": dict(amendment["payload"])})
        found = self.violations(early)
        self.assertIn("round(s) [6] were prepared above the cap of 5 before the amendment", found)

    def test_a_round_run_after_a_finalize_needs_a_reopen(self) -> None:
        finalize = {"type": "finalize", "payload": {"status": "complete"}, "source": "finalize_run.sh"}
        amended = self.amended("sealed")
        snapshot = sha(amended / "pipeline-policy.snapshot.json")
        api_append(amended, finalize, {"type": "round_prepared", "source": "fixture",
                                       "payload": {"round": 6, "plan": plan(snapshot)}})
        self.assertIn("round events after a finalize with no audit_reopened", self.violations(amended))
        # an audit initialised before 2.6.9 with no amendment or reopening: history, reported as a warning
        older = self.legacy("older")
        api_append(older, finalize, {"type": "round_prepared", "source": "fixture",
                                     "payload": {"round": 5, "plan": plan(sha(older / "pipeline-policy.snapshot.json"))}})
        found, warnings = rca.verify(older)
        self.assertEqual(found, [])
        self.assertTrue(any("after a finalize" in message for message in warnings), warnings)
        # with a reopen between them the same rounds are clean
        reopened = self.legacy("reopened", protocol="2.6.9")
        api_append(reopened, finalize)
        result = run([sys.executable, str(REOPEN), str(reopened), "--reason", "fixture"])
        self.assertEqual(result.returncode, 0, result.stderr)
        lifecycle_journal.prepare_round(reopened, 5, plan(sha(reopened / "pipeline-policy.snapshot.json")))
        self.assertEqual(rca.verify(reopened), ([], []))


class ValidatorsAcceptTheLiveCaps(Workspace):
    @staticmethod
    def minimal_run(cap: int, rounds_run: int, **fields) -> dict:
        record = {"schema_version": "2.0.0", "audit_id": "a", "round_target": 2, "round_cap": cap,
                  "rounds_run": rounds_run, "stopping_reason": None, "invocations": []}
        record.update(fields)
        return record

    def test_validate_v2_admits_5_and_7_only_and_the_amendment_chain(self) -> None:
        for cap in (5, 7):
            validate_v2.validate_run(self.minimal_run(cap, 2))
        for cap in (4, 6, 8):
            with self.subTest(cap=cap), self.assertRaises(validate_v2.ValidationError):
                validate_v2.validate_run(self.minimal_run(cap, 2))
        stop = {"stopping_reason": "cap_with_unresolved", "stopping_reason_detail": "one section left",
                "unreviewed_sections": ["Section 2"]}
        validate_v2.validate_run(self.minimal_run(7, 7, **stop))
        validate_v2.validate_run(self.minimal_run(5, 5, **stop))
        for cap, rounds in ((7, 6), (7, 5)):
            with self.subTest(cap=cap, rounds=rounds), self.assertRaises(validate_v2.ValidationError):
                validate_v2.validate_run(self.minimal_run(cap, rounds, **stop))
        step = {"seq": 9, "at": "2026-09-28T00:00:00Z", "leaf": "critique.round_cap", "from": 5, "to": 7,
                "reason": "continue", "snapshot_sha256": "a" * 64}
        validate_v2.validate_run(self.minimal_run(7, 7, policy_amendments=[step], **stop))
        for label, amendments, cap in (
                ("chain ends below the run's cap", [step], 5),
                ("a lowering", [dict(step, **{"from": 7, "to": 5})], 5),
                ("a cap that no audit carries", [dict(step, to=6)], 7),
                ("another leaf", [dict(step, leaf="critique.round_target")], 7)):
            with self.subTest(label), self.assertRaises(validate_v2.ValidationError):
                validate_v2.validate_run(self.minimal_run(cap, 2, policy_amendments=amendments))

    def test_run_schema_admits_the_live_caps_and_the_new_records(self) -> None:
        audit = self.amended()
        record = json.loads((audit / "run.json").read_text())
        schema = SKILL / "schemas" / "run.schema.json"

        def cap_errors(value: dict) -> list[str]:
            return [e for e in validate_json_schema.errors_for_value(value, schema)
                    if "round_cap" in e or "policy_amendments" in e or "/events/" in e]
        self.assertEqual(cap_errors(record), [])
        self.assertEqual(cap_errors(dict(record, round_cap=5, policy_amendments=[])), [])
        self.assertTrue(cap_errors(dict(record, round_cap=6)))
        widened = copy.deepcopy(record)
        widened["policy_amendments"][0]["note"] = "an unknown field"
        self.assertTrue(cap_errors(widened))

    def test_the_post_delta_receipt_schema_admits_rounds_6_and_7(self) -> None:
        import jsonschema
        schema = json.loads((SKILL / "schemas/v2/post-delta-gates.schema.json").read_text())
        round_schema = schema["properties"]["round"]
        path_schema = schema["properties"]["snapshot"]["properties"]["path"]
        for number in (2, 5, 6, 7, 12):
            jsonschema.validate(number, round_schema)
            jsonschema.validate(f"critique/round_{number}/post_delta/{'a' * 64}", path_schema)
        for bad in (1, 0):
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(bad, round_schema)
        for bad in ("critique/round_1/post_delta/", "critique/round_06/post_delta/", "critique/round_x/post_delta/"):
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(bad + "a" * 64, path_schema)


class FinalizeAcceptsTheCapOnlyAtTheEffectiveCap(Workspace):
    """finalize_run.sh's own derivation (its derive_run program) and validate_v2, on journal-projected runs."""

    @staticmethod
    def derive_program() -> str:
        source = FINALIZE.read_text(encoding="utf-8")
        start = source.index("<<'PYD'\n") + len("<<'PYD'\n")
        return source[start:source.index("\nPYD\n", start)]

    def rounds(self, audit: pathlib.Path, numbers: range) -> None:
        env = {"RUN_JSON": str(audit / "run.json")}
        for number in numbers:
            for arguments in (["round_start", f"round={number}", "critics_run=critic_stats,critic_completeness"],
                              ["round_end", f"round={number}"],
                              ["round_result", f"round={number}", "blocking_items=0", "factual_corrections=1"]):
                result = run(["bash", str(LOG_EVENT), *arguments], env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = run(["bash", str(LOG_EVENT), "review_scope", "changed_claim_ids:=[]", "rereviewed_claim_ids:=[]",
                      "unresolved_blocking_item_ids:=[]", "unresolved_conclusion_critical_item_ids:=[]",
                      'unreviewed_sections:=["Section 2"]'], env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def derive(self, audit: pathlib.Path) -> tuple[list[str], dict]:
        candidate = self.work / f"{audit.name}.run.json"
        shutil.copy2(audit / "run.json", candidate)
        # the program is read from stdin, exactly as finalize_run.sh feeds it to python3
        result = subprocess.run([sys.executable, "-", str(candidate), str(audit / "pkg"), "cap_with_unresolved",
                                 "Section 2 was changed after its last review", str(audit), str(POLICY)],
                                input=self.derive_program(), text=True, capture_output=True, timeout=120,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        derived = json.loads(pathlib.Path(str(candidate) + ".tmp").read_text())
        return json.loads(result.stdout)["violations"], derived

    def cap_violations(self, violations: list[str]) -> list[str]:
        return [v for v in violations if "round cap" in v or "round-cap" in v]

    def test_cap_with_unresolved_at_7_of_an_amended_audit_and_not_before(self) -> None:
        seven = self.amended("seven")
        self.rounds(seven, range(1, 8))
        violations, derived = self.derive(seven)
        self.assertEqual(self.cap_violations(violations), [], violations)
        self.assertIs(derived["compliance"]["policy_amendments_valid"], True)
        self.assertEqual((derived["rounds_run"], derived["round_cap"]), (7, 7))
        validate_v2.validate_run(derived)

        six = self.amended("six")
        self.rounds(six, range(1, 7))
        violations, derived = self.derive(six)
        self.assertIn("cap_with_unresolved recorded before this audit's round cap (6 < 7", " ".join(violations))
        with self.assertRaises(validate_v2.ValidationError):
            validate_v2.validate_run(derived)

        five = self.amended("five")
        self.rounds(five, range(1, 6))
        violations, derived = self.derive(five)
        self.assertIn("cap_with_unresolved recorded before this audit's round cap (5 < 7", " ".join(violations))

    def test_an_unamended_legacy_audit_still_stops_at_its_own_cap(self) -> None:
        legacy = self.legacy()
        self.rounds(legacy, range(1, 6))
        violations, derived = self.derive(legacy)
        self.assertEqual(self.cap_violations(violations), [], violations)
        self.assertNotIn("policy_amendments_valid", derived["compliance"], "no new key without an amendment")
        validate_v2.validate_run(derived)


class ReopenedPublishedAudit(Workspace):
    """A published audit re-finalizes, after a journaled reopen, into a new registry version."""

    def test_reopen_refinalize_and_republish(self) -> None:
        from tests.test_release_pipeline import make_audit
        # test_release_pipeline's fixture environment: its scratch registry sits outside the fixture tree
        env = {"I4R_PUBLISH_ANY_ROOT": "1", "I4R_CREATE_REGISTRY": "1", "I4R_REGISTRY_OVERRIDE": "1",
               "LC_ALL": "C", "I4R_RESERVE_GRACE": "0"}
        audit = make_audit(self.work, paper="Paper-Reopened", layout="new")
        tree = self.work / "Discipline"
        published = run(["bash", str(PUBLISH), str(audit)], env=env)
        self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
        old_receipt = (audit / "finalization_receipt.json").read_bytes()
        old_version = json.loads(old_receipt)["registry"]["version"]

        # closed: no new round; the reopen refuses while the filed set is in place
        self.assertIn("this audit was finalized", rca.check_round(audit, 3) or "")
        reopen = [sys.executable, str(REOPEN), str(audit), "--reason", "fixture: correct the published comment"]
        refused = run(reopen, env=env)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("its filed set is still in place", refused.stderr)
        aside = tree / "old_runs/Journal-Sanitized/Paper-Reopened.published-fixture"
        aside.mkdir(parents=True)
        shutil.move(str(tree / "comments/Journal-Sanitized/Paper-Reopened"), str(aside / "comments"))
        shutil.move(str(tree / "metadata/Journal-Sanitized/Paper-Reopened"), str(aside / "metadata"))
        refused = run(reopen, env=env)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("not claimed again since the filing", refused.stderr)
        claimed = run(["bash", str(RESERVE), "claim", str(tree / "data/Journal-Sanitized/Paper-Reopened")], env=env)
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        reopened = run(reopen, env=env)
        self.assertEqual(reopened.returncode, 0, reopened.stderr)

        record = json.loads((audit / "run.json").read_text())
        self.assertEqual((record["status"], record["stopping_reason"], record["ended_at"]), ("ready", None, None))
        entry = record["reopenings"][0]
        self.assertEqual(entry["previous_stopping_reason"], "target_complete")
        self.assertEqual(entry["previous_registry_version"], old_version)
        self.assertEqual(entry["previous_publication"]["release_dir"], "comments/Journal-Sanitized/Paper-Reopened")
        self.assertIsNone(rca.check_round(audit, 3))
        # the closed stretch is a pause, not idle time; status.py reads the reopen from the journal
        reopen_event = [e for e in events(audit) if e["type"] == "audit_reopened"][-1]
        dormant = (lifecycle_journal.parse_time(reopen_event["at"])
                   - lifecycle_journal.parse_time(reopen_event["payload"]["previous_finalized_at"])).total_seconds()
        paused = json.loads((audit / "run_metrics.json").read_text())["durations"]["paused_seconds"]
        self.assertGreaterEqual(paused, dormant)
        import status as status_module
        self.assertEqual(status_module.inspect(audit, {}).get("reopened"), "correct_published")
        stale = run([sys.executable, str(RECEIPT), "check", str(audit)], env=env)
        self.assertNotEqual(stale.returncode, 0, "the reopen extends the journal past the finalized state")

        # the correction route: re-log the stopping evidence, the delivery and the release phase, re-finalize
        scope = [e for e in events(audit) if e["type"] == "review_scope"][-1]["payload"]
        log_env = {**env, "RUN_JSON": str(audit / "run.json")}
        for arguments in (["review_scope", *[f"{key}:={json.dumps(value)}" for key, value in sorted(scope.items())]],
                          ["delivery", "artifact_path=report/comment.pdf"],
                          ["phase_end", "phase=release"]):
            result = run(["bash", str(LOG_EVENT), *arguments], env=log_env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        final = run(["bash", str(FINALIZE), str(audit), "--stopping-reason", "target_complete",
                     "--registry", str(self.work / "_registry")], env={**env, "I4R_REFINALIZE_PUBLISHED": "1"})
        self.assertEqual(final.returncode, 0, final.stdout[-3000:] + final.stderr[-3000:])
        record = json.loads((audit / "run.json").read_text())
        self.assertIs(record["compliance"]["compliance_pass"], True, record["compliance"]["violations"])
        self.assertIs(record["compliance"]["policy_amendments_valid"], True)
        self.assertEqual(len(record["reopenings"]), 1)
        new_receipt = (audit / "finalization_receipt.json").read_bytes()
        self.assertNotEqual(new_receipt, old_receipt)
        new_version = json.loads(new_receipt)["registry"]["version"]
        self.assertNotEqual(new_version, old_version)
        versions = sorted(p.name for p in (self.work / "_registry/audit-integration-001/versions").iterdir()
                          if not p.name.startswith("."))
        self.assertEqual(versions, sorted([old_version, new_version]), "the registry keeps both versions")
        current = json.loads((self.work / "_registry/audit-integration-001/current.json").read_text())
        self.assertEqual(current["version"], new_version)
        self.assertEqual(run([sys.executable, str(RECEIPT), "check", str(audit)], env=env).returncode, 0)
        # the old receipt no longer verifies against the re-finalized audit: it is superseded
        (audit / "finalization_receipt.json").write_bytes(old_receipt)
        self.assertNotEqual(run([sys.executable, str(RECEIPT), "check", str(audit)], env=env).returncode, 0)
        (audit / "finalization_receipt.json").write_bytes(new_receipt)
        self.assertIsNone(status_module.inspect(audit, {}).get("reopened"), "re-finalized: no longer reopened")
        republished = run(["bash", str(PUBLISH), str(audit)], env=env)
        self.assertEqual(republished.returncode, 0, republished.stdout + republished.stderr)


class ReopenedAuditsReportUnderTheCurrentRule(unittest.TestCase):
    def test_a_reopening_by_2_6_8_or_later_enforces_the_disclosure_rule(self) -> None:
        import disclosure_policy
        self.assertFalse(disclosure_policy.enforced("2.6.4"))
        self.assertFalse(disclosure_policy.enforced("2.6.4", {"reopenings": []}))
        self.assertFalse(disclosure_policy.enforced("2.6.4", {"reopenings": [{"skill_version": "2.6.7"}]}))
        self.assertTrue(disclosure_policy.enforced("2.6.4", {"reopenings": [{"skill_version": "2.6.9"}]}))
        # enforcement starts at 2.6.9 (the live 2.6.8's audits only warn), and a reopening by 2.6.8 does not enforce
        self.assertFalse(disclosure_policy.enforced("2.6.8"))
        self.assertFalse(disclosure_policy.enforced("2.6.4", {"reopenings": [{"skill_version": "2.6.8"}]}))
        self.assertTrue(disclosure_policy.enforced("2.6.9"))


if __name__ == "__main__":
    unittest.main()
