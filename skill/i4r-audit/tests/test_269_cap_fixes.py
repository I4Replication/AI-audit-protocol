"""2.6.9 review fixes to the round-cap amendment (lens 'cap', and the 'rt' finding on amending mid-round).

Every test drives the real tools, journal, driver and verifier on fixture audits, with the fake Codex CLI and
no provider call:

* ``RepinAndAmendment`` -- a re-pin after an amendment is refused (the amended snapshot may differ from the one it
  replaced only at the cap); a re-pin then an amendment, even in the same second, validates; two re-pins in one
  second archive under two names and never journal a binding to bytes that were not written.
* ``ProspectiveAmendment`` -- amend (and reopen), dry run and real run alike, verify the state they would leave
  BEFORE writing: a stray round event after a pre-2.6.9 close, a warning today, would become a permanent violation.
* ``ReclaimFromPaperSource`` -- the re-claim of a published paper is found through paper_source.json (the runs/
  folder drops the '?' that data/ keeps); a claim that cannot be confirmed is refused unless --unconfirmed-claim.
* ``ReopenKeepsTheSupersededClose`` -- the superseded finalization receipt is kept read-only and named, with its
  hash, in the reopen; the reopen records where the filed set was moved.
* ``JournaledCap`` -- check-round, prepare_round and the driver use the journaled cap (init, raised by each
  amendment) and refuse a snapshot whose cap differs, even re-hashed and rebound; the verifier refuses any round
  event above the cap in force when it was journaled.
* ``HandLoggedRounds`` -- log_event.sh and the journal CLI refuse a hand-logged round event above the cap or on a
  finalized audit.
* ``AmendMidRound`` -- amending while a round is prepared or started and unfinished is refused with the rerun cost
  (the completed reviews its relaunch would pay for again) unless --accept-rerun-cost.
* ``PublishedCapRunbook`` -- INSTALL.md's runbook blocks, executed verbatim on a fixture tree whose data/,
  comments/ and metadata/ folders keep a '?' that the runs/ folder dropped.
"""
from __future__ import annotations

import glob
import hashlib
import inspect
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "tests"))
sys.path.insert(0, str(SKILL))
import amend_round_cap  # noqa: E402
import lifecycle_journal  # noqa: E402
import record_invocation  # noqa: E402
import repin_critic_route  # noqa: E402
import round_cap_amendment as rca  # noqa: E402
from tests.test_269_round_cap_amendment import (  # noqa: E402
    AMEND, JOURNAL_CLI, LOG_EVENT, REOPEN, Workspace, api_append, events, plan, run, sha, write_audit,
    write_snapshot)

RESERVE = SKILL / "scripts" / "reserve_paper.sh"
INSTALL = SKILL / "INSTALL.md"
FINALIZE_EVENT = {"type": "finalize", "payload": {"status": "complete"}, "source": "finalize_run.sh"}
TREE_ENV = {"I4R_RESERVE_GRACE": "0", "LC_ALL": "C"}


def amend_api(audit: pathlib.Path, **kwargs):
    """amend_round_cap.amend with only the keywords this release knows (the regression is judged on behaviour)."""
    known = inspect.signature(amend_round_cap.amend).parameters
    return amend_round_cap.amend(audit, **{key: value for key, value in kwargs.items() if key in known})


class Fixtures(Workspace):
    def published_tree(self, *, paper: str = "Is it fixed? a fixture paper", journal: str = "Journal-T",
                       move: bool = True) -> tuple[pathlib.Path, pathlib.Path, str, str]:
        """A tree with one published, finalized cap-5 audit whose paper name holds a '?': data/, comments/ and
        metadata/ keep it, the runs/ folder drops it (new_audit.sh). Returns (tree, audit, runs name, true name)."""
        tree = self.work / "tree"
        runs_name = paper.translate(str.maketrans("", "", "?*[]{}\\"))
        audit = write_audit(tree / "runs" / journal / runs_name / "audit", cap=5, audit_id="tree-cap5")
        (audit / "paper_source.json").write_text(json.dumps(
            {"schema_version": "1", "journal": journal, "paper": paper}, sort_keys=True) + "\n")
        for folder in ("data", "comments", "metadata"):
            (tree / folder / journal / paper).mkdir(parents=True, exist_ok=True)
        (tree / "reservations").mkdir(exist_ok=True)
        (tree / "old_runs").mkdir(exist_ok=True)
        (tree / "runs" / journal / runs_name / "release").mkdir()
        (tree / "runs" / journal / runs_name / "release" / "comment.pdf").write_text("fixture release\n")
        receipt = {"schema_version": "1", "registry": {"version": "v-fixture-1"}, "fixture": True}
        (audit / "finalization_receipt.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
        shutil.copy2(audit / "finalization_receipt.json", tree / "metadata" / journal / paper / "finalization_receipt.json")
        (tree / "comments" / journal / paper / "comment.pdf").write_text("fixture comment\n")
        api_append(audit, FINALIZE_EVENT, {"type": "delivery", "source": "publish_completed.sh", "payload": {
            "release_dir": f"comments/{journal}/{paper}", "recipient": rca.FILING_RECIPIENT}})
        if move:
            aside = tree / "old_runs" / journal / f"{runs_name}.published-20260928T000000Z"
            aside.mkdir(parents=True)
            shutil.move(str(tree / "comments" / journal / paper), str(aside / "comments"))
            shutil.move(str(tree / "metadata" / journal / paper), str(aside / "metadata"))
        return tree, audit, runs_name, paper

    def claim(self, tree: pathlib.Path, journal: str, paper: str) -> None:
        result = run(["bash", str(RESERVE), "claim", str(tree / "data" / journal / paper)], env=TREE_ENV)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def reopen(self, audit: pathlib.Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return run([sys.executable, str(REOPEN), str(audit), *arguments], env=TREE_ENV)


class RepinAndAmendment(Fixtures):
    def setUp(self) -> None:
        super().setUp()
        # 2.7.5: re-pin mechanics with arbitrary targets; the shipped-route-only rule is tested in
        # tests/test_275_critic_route_lock.py
        repin_critic_route.SHIPPED_ROUTE_ONLY = False
        self.addCleanup(setattr, repin_critic_route, "SHIPPED_ROUTE_ONLY", True)

    def test_a_repin_after_an_amendment_is_refused_and_writes_nothing(self) -> None:
        audit = self.amended()
        before = self.state(audit)
        with self.assertRaises(repin_critic_route.RepinError) as refused:
            repin_critic_route.repin(audit, critic_model="gpt-5.6-sol", adjudicator_model=None,
                                     reason="fixture: account cannot use the pinned model")
        self.assertIn("Re-pin BEFORE amending", str(refused.exception))
        self.assertEqual(self.state(audit), before)
        self.assertEqual(rca.verify(audit), ([], []))
        self.assertIsNone(rca.check_round(audit, 6))

    def test_a_repin_then_an_amendment_in_the_same_second_validates(self) -> None:
        audit = self.legacy()
        # round 1 was prepared on the original route, then the account could not use it (critic_model_unavailable)
        lifecycle_journal.prepare_round(audit, 1, plan(sha(audit / "pipeline-policy.snapshot.json")))
        at = "2026-09-28T10:00:00Z"
        repin_critic_route.repin(audit, critic_model="gpt-5.6-sol", adjudicator_model=None,
                                 reason="fixture: account cannot use the pinned model", at=at)
        amend_api(audit, to=7, reason="fixture: continue past the old cap", at=at, accept_rerun_cost=True)
        archives = sorted(audit.glob("pipeline-policy.snapshot.superseded-*.json"))
        self.assertEqual(len(archives), 2, "two rewrites in one second keep two archives")
        for archive in archives:
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o400)
        record_invocation.validate_policy_binding(audit)
        self.assertEqual(rca.verify(audit), ([], []))
        self.assertIsNone(rca.check_round(audit, 6))

    def test_two_repins_in_one_second_never_journal_unwritten_bytes(self) -> None:
        audit = self.legacy()
        at = "2026-09-28T11:00:00Z"
        repin_critic_route.repin(audit, critic_model="gpt-5.6-sol", adjudicator_model=None, reason="first", at=at)
        repin_critic_route.repin(audit, critic_model="gpt-5.6-luna", adjudicator_model=None, reason="second", at=at)
        self.assertEqual(len(list(audit.glob("pipeline-policy.snapshot.superseded-*.json"))), 2)
        record_invocation.validate_policy_binding(audit)
        bound = json.loads((audit / "run.json").read_text())["toolkit"]["pipeline_policy_snapshot_sha256"]
        self.assertEqual(bound, sha(audit / "pipeline-policy.snapshot.json"))
        self.assertEqual(record_invocation.load_policy_snapshot(audit)["models"]["critics"]["requested_model"],
                         "gpt-5.6-luna")


class ProspectiveAmendment(Fixtures):
    def stray(self, name: str) -> pathlib.Path:
        """A pre-2.6.9 record closed by a finalize, then a round event with no reopen: a warning today."""
        audit = self.legacy(name)
        api_append(audit, FINALIZE_EVENT, {"type": "round_end", "source": "log_event.sh", "payload": {"round": 5}})
        violations, warnings = rca.verify(audit)
        self.assertEqual(violations, [])
        self.assertTrue(any("after a finalize" in message for message in warnings), warnings)
        return audit

    def test_amend_refuses_before_writing_a_state_its_own_check_would_fail(self) -> None:
        audit = self.stray("stray-amend")
        before = self.state(audit)
        reason = ("--reason", "fixture: continue past the old cap")
        for arguments in (("--dry-run",), ()):
            with self.subTest(arguments=arguments):
                result = self.amend(audit, "--to", "7", "--reopen", *reason, *arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("would fail its own round-cap check", result.stderr)
                self.assertIn("round events after a finalize", result.stderr)
                self.assertEqual(self.state(audit), before, "nothing may be written")
        self.assertFalse(list(audit.glob("finalization_receipt.superseded-*")))

    def test_reopen_refuses_the_same_state_before_writing(self) -> None:
        audit = self.stray("stray-reopen")
        before = self.state(audit)
        for arguments in (("--dry-run",), ()):
            with self.subTest(arguments=arguments):
                result = self.reopen(audit, "--reason", "fixture: correct it", *arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("would fail its round-cap check", result.stderr)
                self.assertEqual(self.state(audit), before)


class ReclaimFromPaperSource(Fixtures):
    def test_the_reclaim_is_found_through_paper_source_json(self) -> None:
        tree, audit, _, paper = self.published_tree()
        refused = self.reopen(audit, "--reason", "fixture: correct it", "--dry-run")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("not claimed again since the filing", refused.stderr)
        refused = self.amend(audit, "--to", "7", "--reopen", "--dry-run", "--reason", "fixture")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("not claimed again since the filing", refused.stderr)
        self.claim(tree, "Journal-T", paper)
        allowed = self.reopen(audit, "--reason", "fixture: correct it", "--dry-run")
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
        self.assertNotIn("could not confirm", allowed.stderr)

    def test_a_claim_that_cannot_be_confirmed_is_refused_unless_overridden(self) -> None:
        tree, audit, _, paper = self.published_tree()
        shutil.rmtree(tree / "data")   # no data/<J>/<P> under any spelling: the claim cannot be told
        before = self.state(audit)
        refused = self.reopen(audit, "--reason", "fixture: correct it")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("re-claim cannot be confirmed", refused.stderr)
        self.assertEqual(self.state(audit), before)
        allowed = self.reopen(audit, "--reason", "fixture: correct it", "--unconfirmed-claim", "--dry-run")
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
        self.assertIn("--unconfirmed-claim", allowed.stderr)


class ReopenKeepsTheSupersededClose(Fixtures):
    def test_the_receipt_is_kept_and_the_moved_set_is_recorded(self) -> None:
        tree, audit, runs_name, paper = self.published_tree()
        self.claim(tree, "Journal-T", paper)
        receipt = (audit / "finalization_receipt.json").read_bytes()
        result = self.reopen(audit, "--reason", "fixture: correct the published comment")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        close = [e for e in events(audit) if e["type"] == "finalize"][-1]
        kept = audit / f"finalization_receipt.superseded-{close['seq']}.json"
        self.assertTrue(kept.is_file(), "the superseded receipt is kept beside the audit")
        self.assertEqual(kept.read_bytes(), receipt)
        self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o400)
        payload = [e for e in events(audit) if e["type"] == "audit_reopened"][-1]["payload"]
        self.assertEqual(payload["previous_receipt_archive"],
                         {"name": kept.name, "sha256": hashlib.sha256(receipt).hexdigest()})
        moved = f"old_runs/Journal-T/{runs_name}.published-20260928T000000Z"
        self.assertEqual(payload["previous_publication"]["archived_to"], moved)
        self.assertEqual(payload["previous_publication"]["release_dir"], f"comments/Journal-T/{paper}")
        entry = json.loads((audit / "run.json").read_text())["reopenings"][0]
        self.assertEqual(entry["previous_publication"]["archived_to"], moved)
        self.assertEqual(rca.verify(audit), ([], []))


class JournaledCap(Fixtures):
    def recapped(self, name: str, *, rebind: bool) -> pathlib.Path:
        """A cap-5 audit whose snapshot says 7 with a valid envelope (the operator re-hashed it)."""
        audit = self.legacy(name)
        policy = record_invocation.load_policy_snapshot(audit)
        policy["critique"]["round_cap"] = 7
        path = audit / "pipeline-policy.snapshot.json"
        path.chmod(0o600)
        write_snapshot(audit, policy)
        if rebind:
            toolkit = dict(json.loads((audit / "run.json").read_text())["toolkit"])
            toolkit["pipeline_policy_snapshot_sha256"] = sha(path)
            api_append(audit, {"type": "metadata_correction", "payload": {"set": {"toolkit": toolkit}},
                               "source": "fixture"})
        return audit

    def test_a_rehashed_cap_is_refused_by_check_round_and_prepare_round(self) -> None:
        unbound = self.recapped("unbound", rebind=False)
        self.assertIn("cannot be trusted", rca.check_round(unbound, 6) or "")
        rebound = self.recapped("rebound", rebind=True)
        refusal = rca.check_round(rebound, 6) or ""
        self.assertIn("the journal records 5", refusal)
        self.assertIn("the journal records 5", rca.check_round(rebound, 2) or "", "any round, not only above 5")
        before = self.state(rebound)
        with self.assertRaises(lifecycle_journal.JournalError):
            lifecycle_journal.prepare_round(rebound, 6, plan(sha(rebound / "pipeline-policy.snapshot.json")))
        self.assertEqual(self.state(rebound), before)

    def test_the_driver_refuses_a_rehashed_cap_before_any_journal_write_or_call(self) -> None:
        audit = self.recapped("driver", rebind=True)
        manifest = self.packet(audit, 6)
        before = self.state(audit)
        rc, log = self.driver(audit, 6, "r6-rehashed", DIFF_MANIFEST=str(manifest))
        self.assertEqual(rc, 1, log)
        self.assertIn("the journal records 5", log)
        self.assertFalse((audit / "critique/round_6").exists())
        self.assertEqual(self.state(audit), before)
        self.assertEqual(self.calls(audit), 0)

    def test_the_verifier_refuses_a_round_above_the_cap_in_force(self) -> None:
        audit = self.legacy("above")
        api_append(audit, {"type": "round_prepared", "source": "fixture",
                           "payload": {"round": 6, "plan": plan(sha(audit / "pipeline-policy.snapshot.json"))}})
        violations, _ = rca.verify(audit)
        self.assertTrue(any("above the round cap in force" in v and "round 6" in v for v in violations), violations)
        # negative control: after the amendment, round 6 is inside the cap
        clean = self.amended("inside")
        lifecycle_journal.prepare_round(clean, 6, plan(sha(clean / "pipeline-policy.snapshot.json")))
        self.assertEqual(rca.verify(clean), ([], []))


class HandLoggedRounds(Fixtures):
    def test_a_hand_logged_round_above_the_cap_or_after_a_finalize_is_refused(self) -> None:
        audit = self.legacy()
        env = {"RUN_JSON": str(audit / "run.json")}
        before = self.state(audit)
        result = run(["bash", str(LOG_EVENT), "round_start", "round=6"], env=env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("round cap of 5", result.stderr)
        batch = self.work / "batch.jsonl"
        batch.write_text(json.dumps({"type": "round_end", "payload": {"round": 6}}) + "\n")
        result = run([sys.executable, str(JOURNAL_CLI), "append-batch", str(audit), str(batch)])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.state(audit), before)
        # negative control: a round inside the cap on an open audit is still logged by hand
        result = run(["bash", str(LOG_EVENT), "round_start", "round=2"], env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        api_append(audit, FINALIZE_EVENT)
        closed = self.state(audit)
        result = run(["bash", str(LOG_EVENT), "round_start", "round=3"], env=env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("finalized", result.stderr)
        self.assertEqual(self.state(audit), closed)
        # a re-logged result before a re-finalize is not a new round
        result = run(["bash", str(LOG_EVENT), "round_result", "round=2", "blocking_items=0"], env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class AmendMidRound(Fixtures):
    def test_an_unfinished_round_with_nothing_cached_is_amended_with_a_note(self) -> None:
        # Nothing completed is cached for the prepared round, so a relaunch pays for nothing again: no refusal,
        # no --accept-rerun-cost, only a note naming the round.
        audit = self.legacy()
        lifecycle_journal.prepare_round(audit, 1, plan(sha(audit / "pipeline-policy.snapshot.json")))
        accepted = self.amend(audit, "--to", "7", "--reason", "fixture")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn("round 1 (prepared", accepted.stdout + accepted.stderr)
        self.assertIn("nothing is paid again", accepted.stdout + accepted.stderr)
        self.assertNotIn("pass --accept-rerun-cost", accepted.stdout + accepted.stderr)

    def test_a_stray_round_after_a_finalize_reports_the_accurate_refusal(self) -> None:
        # A hand-logged round_start after a pre-2.6.9 finalize: the amendment would fail its own round-cap check.
        # That is the accurate reason, and it must be what the operator sees, not a rerun cost.
        audit = self.legacy("stray-start")
        api_append(audit, FINALIZE_EVENT, {"type": "round_start", "source": "log_event.sh", "payload": {"round": 3}})
        before = self.state(audit)
        refused = self.amend(audit, "--to", "7", "--reopen", "--reason", "fixture: continue past the old cap")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("would fail its own round-cap check", refused.stderr)
        self.assertNotIn("--accept-rerun-cost", refused.stderr)
        self.assertEqual(self.state(audit), before)

    def test_the_refusal_counts_the_reviews_a_relaunch_would_pay_for_again(self) -> None:
        audit = self.legacy()
        manifest = self.packet(audit, 5)
        rc, log = self.driver(audit, 5, "r5-partial", DIFF_MANIFEST=str(manifest), FAKE_CODEX_SCENARIO="permanent",
                              FAKE_PERMANENT_ROLE="completeness", MAX_TRIES="1")
        self.assertNotEqual(rc, 0, log)
        completed = [row for row in json.loads((audit / "run.json").read_text())["invocations"]
                     if row.get("round") == 5 and row.get("status") == "completed" and row.get("role") != "model_probe"
                     and row.get("role_class") in {"critic", "synthesizer", "adjudicator"}]
        self.assertTrue(completed, "some critics completed before the failure")
        before = self.state(audit)
        refused = self.amend(audit, "--to", "7", "--reason", "fixture: raise mid-round")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(f"{len(completed)} completed critic/adjudicator review(s) would be dispatched again",
                      refused.stderr)
        self.assertEqual(self.state(audit), before)


class PublishedCapRunbook(Fixtures):
    """INSTALL.md's published-audit runbook, its blocks executed verbatim (only the tree root and the audit folder,
    the two placeholders, are substituted)."""

    @staticmethod
    def blocks() -> list[str]:
        text = INSTALL.read_text(encoding="utf-8")
        start = text.index("### Audits A22 and A23: published audits that stopped at their cap")
        end = text.index("\n## ", start)
        return re.findall(r"```bash\n(.*?)```", text[start:end], flags=re.S)

    def test_steps_1_to_4_run_on_a_tree_whose_paper_name_holds_a_question_mark(self) -> None:
        tree, audit, runs_name, paper = self.published_tree(move=False)
        skill = tree / "skill" / "i4r-audit-unified" / "i4r-audit"
        skill.parent.mkdir(parents=True)
        skill.symlink_to(SKILL, target_is_directory=True)
        blocks = self.blocks()
        setup = next(block for block in blocks if "paper_source.json" in block and "AUDIT_DIR=" in block)
        setup = re.sub(r'(?m)^cd "<[^>]*>".*$', 'cd "$FIXTURE_TREE"', setup)
        setup = re.sub(r'(?m)^(export )?AUDIT_DIR=.*$', r'\1AUDIT_DIR="$PWD/runs/$FIXTURE_J/$FIXTURE_P/audit"', setup)
        move = next(block for block in blocks if "reserve_paper.sh" in block and "old_runs/" in block)
        dry_run = next(block for block in blocks if "amend_round_cap.py" in block and "--dry-run" in block)
        drift = next(block for block in blocks if "check_policy_drift.py" in block)
        # the driver reads AUDIT_DIR from the environment: the setup must export it
        script = "set -e\n" + "\n".join((setup, 'printenv AUDIT_DIR > /dev/null', drift, move, dry_run))
        result = run(["bash", "-c", script], env={**TREE_ENV, "FIXTURE_TREE": str(tree), "FIXTURE_J": "Journal-T",
                                                  "FIXTURE_P": runs_name}, timeout=600)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
        self.assertIn("would raise the round cap 5 -> 7 and reopen the finalized audit", result.stdout)
        self.assertFalse((tree / "comments/Journal-T" / paper).exists())
        self.assertFalse((tree / "metadata/Journal-T" / paper).exists())
        aside = list((tree / "old_runs/Journal-T").glob(glob.escape(runs_name) + ".published-*"))
        self.assertEqual(len(aside), 1, aside)
        for part in ("comments", "metadata", "release"):
            self.assertTrue((aside[0] / part).is_dir(), part)
        self.assertFalse((audit.parent / "release").exists())


if __name__ == "__main__":
    unittest.main()
