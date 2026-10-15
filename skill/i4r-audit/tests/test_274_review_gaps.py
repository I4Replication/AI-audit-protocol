"""2.7.4: tests for the gaps the release's review found.

A mutation review planted single-point defects in the 2.7.4 implementation that the first test set let through; each
class below names the mutant(s) it kills. The second half covers three defects the end-to-end review found and 2.7.4
fixed before release: a later round left with no specialist (finalize requires one), a bookkeeping-only high-impact
finding raising an evidence gap every round, and the specialist being chosen from bookkeeping rows.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "tests"))
sys.path.insert(0, str(SKILL / "schemas" / "tools"))
import claim_change_rules as rules  # noqa: E402
import run_coordinator  # noqa: E402
import test_diff_packet as packet_tests  # noqa: E402
import test_270_policy_and_init as pinit  # noqa: E402


class RuleGaps(unittest.TestCase):
    """M21 (a rebind treated as substantive), M24 (an un-approval treated as bookkeeping)."""

    def test_a_rebound_confirmation_is_bookkeeping(self) -> None:
        import make_diff_packet
        before = copy.deepcopy(packet_tests.FIXTURE)
        after = copy.deepcopy(before)
        # the same critic confirms again in round 2: a new invocation and evidence record, nothing lost
        after["verifications"][1]["invocation_id"] = "inv-critic-stats-r2-1"
        after["verifications"][1]["evidence_record"] = {"path": "critique/round_2/critic_stats.json",
                                                        "sha256": "e" * 64}
        self.assertEqual(rules.substantive_paths(before, after, make_diff_packet.changed_paths), [])

    def test_withdrawing_an_approval_is_substantive(self) -> None:
        import make_diff_packet
        before = copy.deepcopy(packet_tests.FIXTURE)
        self.assertIs(before["coordinator_approved"], True)
        after = copy.deepcopy(before)
        after["coordinator_approved"] = False
        self.assertTrue(rules.substantive_paths(before, after, make_diff_packet.changed_paths))


class PacketSectionCoupling(packet_tests.DiffPacketTests):
    """M25: make_diff_packet must pass the changed report sections to the rule."""

    def test_a_bookkeeping_row_whose_section_text_changed_goes_to_the_specialists(self) -> None:
        (self.audit / "work/report_before.md").write_text("# Abstract\nStable.\n\n## 1. Findings\nBefore.\n")
        (self.audit / "report/REPORT_final.md").write_text("# Abstract\nStable.\n\n## 1. Findings\nAfter, reworded.\n")
        before_rows = [self.row("F1", "data"), self.row("F2", "statistics")]
        for row in before_rows:
            row["report_section"] = "Section 1.2" if row["id"] == "F1" else "Section 9"
        after_rows = copy.deepcopy(before_rows)
        after_rows[0] = self.row("F1", "data", settled=True)  # bookkeeping only
        after_rows[0]["report_section"] = "Section 1.2"
        after_rows[1] = self.row("F2", "statistics", settled=True)  # bookkeeping only, untouched section
        after_rows[1]["report_section"] = "Section 9"
        before = self.write_rows("work/findings.before.jsonl", before_rows)
        self.write_rows("work/findings.jsonl", after_rows)
        policy = json.loads((SKILL / "config/pipeline-policy-v2.json").read_text())
        envelope = {"snapshot_version": "1", "policy_sha256": __import__("record_invocation").policy_hash(policy),
                    "policy": policy}
        (self.audit / "pipeline-policy.snapshot.json").write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n")
        result = self.run_diff(before, "new.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        packet = json.loads((self.audit / "critique/new.json").read_text())
        self.assertIn("Section 1", packet["changed_sections"])
        self.assertEqual(packet["specialist_claim_ids"], ["F1"])
        self.assertEqual(packet["bookkeeping_only_claim_ids"], ["F2"])


class InitRefusesAnUnknownClaimRule(unittest.TestCase):
    """M20: init_run.sh refuses a leftover snapshot whose claim-change rule no packet builder admits."""

    def test_refused_untouched(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-prop-rule-") as name:
            policy = pinit.shipped_policy()
            policy["critique"]["later_rounds"]["routing_manifest"]["claim_change_rule"] = "substantive_fields_9"
            data = pinit.envelope_bytes(policy)
            audit = pinit.make_audit(pathlib.Path(name) / "odd" / "audit")
            (audit / pinit.SNAPSHOT).write_bytes(data)
            result = pinit.init(audit)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("freezes a claim-change rule this skill does not run", result.stderr)
            self.assertEqual((audit / pinit.SNAPSHOT).read_bytes(), data)
            self.assertFalse((audit / "run.json").exists())


class DriftJoinsTheNewModules(unittest.TestCase):
    """M40/M41: a module revert on disk fails the drift check (as test_272 does for phase1_timeouts.py)."""

    def check(self, relative: str, old: str, new: str, message: str) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-prop-drift-") as name:
            tree = pathlib.Path(name) / "i4r-audit"
            shutil.copytree(SKILL, tree, ignore=shutil.ignore_patterns("__pycache__"), symlinks=True)
            path = tree / relative
            text = path.read_text()
            self.assertEqual(text.count(old), 1)
            path.write_text(text.replace(old, new))
            result = subprocess.run([sys.executable, str(tree / "config/check_policy_drift.py")],
                                    text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(message, result.stdout)

    def test_critique_timeouts_dropping_2700(self) -> None:
        self.check("scripts/critique_timeouts.py",
                   "LIVE_CRITIQUE_TIMEOUTS: tuple[int, ...] = (LEGACY_CRITIQUE_TIMEOUT, SHIPPED_CRITIQUE_TIMEOUT)",
                   "LIVE_CRITIQUE_TIMEOUTS: tuple[int, ...] = (SHIPPED_CRITIQUE_TIMEOUT,)",
                   "LIVE_CRITIQUE_TIMEOUTS no longer holds the 2700 s ceiling")

    def test_claim_change_rules_dropping_the_legacy_rule(self) -> None:
        self.check("scripts/claim_change_rules.py",
                   "LIVE_RULES: tuple[str, ...] = (LEGACY_RULE, SPECIALIST_RULE)",
                   "LIVE_RULES: tuple[str, ...] = (SPECIALIST_RULE,)",
                   "LIVE_RULES no longer holds the legacy rule")


class OutputLimitAtTheDefaultCap(unittest.TestCase):
    """M07: the CLI's message at its own 64,000 default (a Sonnet 5 route, or no cap) is output_limit too."""

    def test_both_caps_classify(self) -> None:
        for cap in ("64000", "128000", "32000"):
            with self.subTest(cap=cap):
                message = (f"API Error: Claude's response exceeded the {cap} output token maximum. To configure "
                           "this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable.")
                self.assertEqual(run_coordinator.classify_error(message), "output_limit")


class DriverRecordsTheReviewedReport(unittest.TestCase):
    """M33/M34: a round the real driver completes journals report_version_reviewed = the snapshot's report sha."""

    def test_round_1_records_the_report_it_reviewed(self) -> None:
        import test_269_round_cap_amendment as cap
        work = pathlib.Path(tempfile.mkdtemp(prefix="i4r-prop-rvr-")).resolve()
        try:
            audit = cap.write_audit(work / "new", cap=7, audit_id="new-rvr", protocol="2.6.9")
            report_sha = hashlib.sha256((audit / "report/REPORT_final.md").read_bytes()).hexdigest()
            helper = cap.Workspace("setUp")
            code, output = helper.driver(audit, 1, "rvr-1")
            self.assertEqual(code, 0, output[-3000:])
            integrated = [event for event in cap.events(audit) if event["type"] == "round_integrated"]
            self.assertEqual(len(integrated), 1)
            self.assertEqual(integrated[0]["payload"].get("report_version_reviewed"), report_sha)
            run = json.loads((audit / "run.json").read_text())
            self.assertEqual(run["rounds"][0].get("report_version_reviewed"), report_sha)
        finally:
            subprocess.run(["chmod", "-R", "u+w", str(work)], check=False)
            shutil.rmtree(work, ignore_errors=True)


class ReceiptBoundToAnotherInvocation(unittest.TestCase):
    """M31: a completion receipt naming another invocation is refused, not silently ignored."""

    def test_refused(self) -> None:
        import test_274_repair_bound_registry as repair
        import build_draft_registry
        repair.RepairCompletedRole.setUpClass()
        try:
            case = repair.RepairCompletedRole("test_the_condition_is_on_disk")
            root = case.copy("receipt")
            entry = case.binding(root)
            receipt = root / "phase1/completions" / repair.ROLE / f"{entry['input_sha256']}.json"
            self.assertTrue(receipt.is_file(), receipt)
            receipt.chmod(0o644)
            data = json.loads(receipt.read_text())
            data["invocation_id"] = "auditor-00000000-0000-0000-0000-000000000000"
            receipt.write_text(json.dumps(data))
            with self.assertRaises(SystemExit) as caught:
                build_draft_registry.main([str(root), "--dry-run"])
            self.assertIn("binds invocation", str(caught.exception))
        finally:
            repair.RepairCompletedRole.tearDownClass()


class DocxHeadingCarriesTheBreak(unittest.TestCase):
    """M35/M36: the built DOCX's back-matter Heading1 carries pageBreakBefore and no marker survives."""

    def test_real_pandoc_build(self) -> None:
        import test_appendix_rendering as appendix
        if not (appendix.PANDOC and appendix.SOFFICE and appendix.POPPLER):
            self.skipTest("pandoc, LibreOffice and Poppler are required")
        with tempfile.TemporaryDirectory(prefix="i4r-prop-docx-") as name:
            audit = appendix.build_fixture(pathlib.Path(name), appendix.MARKDOWN_HEAVY[:3])
            result = subprocess.run(["bash", str(appendix.BUILDER), "--no-cache", str(audit), "comment"],
                                    text=True, capture_output=True, timeout=300,
                                    env=dict(os.environ, PANDOC=appendix.PANDOC))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with zipfile.ZipFile(audit / "report/comment.docx") as archive:
                document = archive.read("word/document.xml").decode("utf-8")
            self.assertNotIn("I4RPageBreakBeforeNext", document)
            self.assertNotIn('<w:br w:type="page"/>', document)
            paragraphs = re.findall(r"<w:p>(?:(?!</w:p>).)*?Data and Code Availability.*?</w:p>", document, re.S)
            heading = [p for p in paragraphs if 'w:val="Heading1"' in p]
            self.assertTrue(heading, "no Heading1 paragraph for Data and Code Availability")
            self.assertIn("<w:pageBreakBefore/>", heading[0])


class WaiterDefaultsToTwiceTheCeiling(unittest.TestCase):
    """M18: wait_for_round.sh with no --timeout-min waits twice this audit's critique ceiling.

    A PATH shim for `date +%s` jumps the clock 91 minutes after the waiter computes its deadline: a fixed
    90-minute default times out, the 2.7.4 default (180 minutes for a frozen 5400 s audit) keeps waiting."""

    def test_a_new_audit_is_not_cut_at_90_minutes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-prop-wait-") as name:
            root = pathlib.Path(name)
            audit = root / "audit"
            (audit / "critique/round_1").mkdir(parents=True)
            (audit / "pipeline-policy.snapshot.json").write_bytes(pinit.envelope_bytes(pinit.shipped_policy()))
            shim = root / "bin"
            shim.mkdir()
            real_date = shutil.which("date")
            state = root / "date_calls"
            (shim / "date").write_text(
                "#!/bin/bash\n"
                f'if [ "$1" = "+%s" ]; then n=$(cat "{state}" 2>/dev/null || echo 0); echo $((n+1)) > "{state}";\n'
                f'  now=$({real_date} +%s); if [ "$n" -ge 1 ]; then echo $((now + 91*60)); else echo $now; fi;\n'
                f'else exec {real_date} "$@"; fi\n')
            (shim / "date").chmod(0o755)
            sleeper = subprocess.Popen(["sleep", "30"])
            try:
                env = dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}", I4R_WAIT_POLL_SECONDS="1")
                waiter = subprocess.Popen(["bash", str(SKILL / "scripts/wait_for_round.sh"), str(audit), "1",
                                           "--pid", str(sleeper.pid)], text=True, env=env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                time.sleep(6)
                still_waiting = waiter.poll() is None
                waiter.kill()
                out, err = waiter.communicate()
                self.assertTrue(still_waiting, f"the waiter gave up before 180 minutes: {out}{err}")
            finally:
                sleeper.kill()


class DriverDeadlineIsTheFrozenCeiling(unittest.TestCase):
    """M16: every Codex call of a new audit is bounded by the frozen 5,400 s, not the raw knob's 2,700 default.

    A `date +%s` shim jumps the clock 3,000 s once the first provider call is in flight (the fake Codex touches a
    marker one second after it starts, then finishes two seconds later): a 2,700 s deadline stops that call, the
    frozen 5,400 s one does not."""

    def test_a_new_audit_call_is_not_cut_at_2700_s(self) -> None:
        import test_269_round_cap_amendment as cap
        work = pathlib.Path(tempfile.mkdtemp(prefix="i4r-prop-deadline-")).resolve()
        try:
            audit = cap.write_audit(work / "new", cap=7, audit_id="new-deadline", protocol="2.6.9")
            shim = work / "bin"
            shim.mkdir()
            marker = work / "clock_jumped"
            real_date = shutil.which("date")
            (shim / "date").write_text(
                "#!/bin/bash\n"
                f'if [ "$1" = "+%s" ] && [ -e "{marker}" ]; then echo $(( $({real_date} +%s) + 3000 )); '
                f'else exec {real_date} "$@"; fi\n')
            (shim / "date").chmod(0o755)
            codex = work / "codex"
            codex.write_text(
                "#!/bin/bash\n"
                f'if [ "$1" = exec ] && [ ! -e "{marker}" ]; then sleep 1; touch "{marker}"; sleep 2; fi\n'
                f'exec "{cap.FAKE_CODEX}" "$@"\n')
            codex.chmod(0o755)
            helper = cap.Workspace("setUp")
            code, output = helper.driver(audit, 1, "deadline-1", CODEX=str(codex),
                                         PATH=f"{shim}:{os.environ['PATH']}")
            self.assertTrue(marker.exists(), "the clock never jumped: the test did not bite")
            # Only the deadline is asserted: the jumped shell clock disagrees with the Python helpers' clock, so the
            # round itself then stops on "timestamps and elapsed_seconds are inconsistent" either way.
            self.assertNotIn("[tmout] provider call exceeded", output, output[-3000:])
        finally:
            subprocess.run(["chmod", "-R", "u+w", str(work)], check=False)
            shutil.rmtree(work, ignore_errors=True)


class LegacyWaiterHonoursTheKnob(unittest.TestCase):
    """An audit initialised before 2.7.4 that exports I4R_PROVIDER_TIMEOUT_SECONDS=5400 (INSTALL.md, Upgrading in-flight
    audits to 2.7.4) gets a 180-minute default wait, as run_critique.sh gets a 5,400 s ceiling."""

    def test_the_waiter_uses_the_effective_ceiling(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-274-wait-legacy-") as name:
            root = pathlib.Path(name)
            audit = root / "audit"
            (audit / "critique/round_1").mkdir(parents=True)
            policy = pinit.shipped_policy()
            policy["critique"].pop("provider_timeout_seconds")
            (audit / "pipeline-policy.snapshot.json").write_bytes(pinit.envelope_bytes(policy))
            shim = root / "bin"
            shim.mkdir()
            real_date = shutil.which("date")
            state = root / "date_calls"
            (shim / "date").write_text(
                "#!/bin/bash\n"
                f'if [ "$1" = "+%s" ]; then n=$(cat "{state}" 2>/dev/null || echo 0); echo $((n+1)) > "{state}";\n'
                f'  now=$({real_date} +%s); if [ "$n" -ge 1 ]; then echo $((now + 91*60)); else echo $now; fi;\n'
                f'else exec {real_date} "$@"; fi\n')
            (shim / "date").chmod(0o755)
            for knob, survives in (("5400", True), ("", False)):
                with self.subTest(knob=knob):
                    state.unlink(missing_ok=True)
                    sleeper = subprocess.Popen(["sleep", "30"])
                    try:
                        env = dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}", I4R_WAIT_POLL_SECONDS="1",
                                   I4R_PROVIDER_TIMEOUT_SECONDS=knob)
                        waiter = subprocess.Popen(["bash", str(SKILL / "scripts/wait_for_round.sh"), str(audit), "1",
                                                   "--pid", str(sleeper.pid)], text=True, env=env,
                                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        time.sleep(6)
                        still_waiting = waiter.poll() is None
                        waiter.kill()
                        waiter.communicate()
                        self.assertEqual(still_waiting, survives)
                    finally:
                        sleeper.kill()


class RouterKeepsOneSpecialist(unittest.TestCase):
    """When no specialist has a substantive change, the round keeps the first routed specialist with the unnarrowed
    scope: finalize requires completeness plus one specialist in every round after round 1."""

    def test_an_all_bookkeeping_round_keeps_a_specialist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-274-route-keep-") as name:
            ws = pathlib.Path(name)
            (ws / "work").mkdir()
            rows = []
            for finding_id, domain in (("F1", "data"), ("F2", "statistics")):
                row = copy.deepcopy(packet_tests.FIXTURE)
                row.update(id=finding_id, domain=domain, disclosure_flag=False)
                rows.append(row)
            (ws / "work/findings.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
            manifest = {"round": 2, "changed_sections": [], "changed_claim_ids": ["F1", "F2"],
                        "recommended_roles": ["stats"], "routing_mode": "single_best_specialist",
                        "critical_domains": [], "claim_change_rule": rules.SPECIALIST_RULE,
                        "specialist_claim_ids": [], "bookkeeping_only_claim_ids": ["F1", "F2"]}
            (ws / "manifest.json").write_text(json.dumps(manifest))
            out = ws / "routing.json"
            result = subprocess.run([sys.executable, str(SKILL / "scripts/test_run_critique_runtime.py"), "route",
                                     "--workspace", str(ws), "--round", "2", "--manifest", str(ws / "manifest.json"),
                                     "--output", str(out)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            routing = json.loads(out.read_text())
            self.assertEqual(routing["roles"], ["stats", "completeness"])
            self.assertEqual(routing["claim_assignments"], {"stats": ["F1", "F2"], "completeness": ["F1", "F2"]})
            self.assertIn("keeps the unnarrowed scope", routing["reason"])


class BookkeepingFindingsRaiseNoEvidenceGap(unittest.TestCase):
    """A high-impact bookkeeping-only finding that completeness alone confirms raises no high_impact_evidence_gap;
    the same finding without the bookkeeping exemption, or disputed, still does."""

    def test_the_exemption(self) -> None:
        import test_run_critique_runtime as runtime
        canonical = copy.deepcopy(packet_tests.FIXTURE)
        canonical.update(id="F1", impact_rank=4, changes_conclusion=False)
        registry = {"f1": canonical}
        confirmed = {"finding_id": "F1", "item_id": "c-F1", "verdict": "CONFIRMED",
                     "evidence": {"artifact_refs": [{"path": "source/REPORT_TO_REVIEW.md", "sha256": "a" * 64}]}}
        rows = [("completeness", {"settled_spot_checks": [], "items": [confirmed]})]
        kinds = lambda found: [trigger["kind"] for trigger in found]  # noqa: E731
        self.assertIn("high_impact_evidence_gap", kinds(runtime.adjudication_triggers(rows, registry, {"F1"})))
        self.assertNotIn("high_impact_evidence_gap",
                         kinds(runtime.adjudication_triggers(rows, registry, {"F1"}, {"F1"})))
        disputed = [("completeness", {"settled_spot_checks": [], "items": [dict(confirmed, verdict="OVERSTATED")]})]
        self.assertIn("high_impact_evidence_gap",
                      kinds(runtime.adjudication_triggers(disputed, registry, {"F1"}, {"F1"})))


class SpecialistChosenFromSubstantiveRows(packet_tests.DiffPacketTests):
    """Under the specialist rule the packet chooses its specialist from the substantive rows; legacy from all rows."""

    def test_the_bookkeeping_majority_does_not_pick_the_specialist(self) -> None:
        before_rows = [self.row("F1", "data"), self.row("F2", "data"), self.row("F3", "statistics")]
        after_rows = [self.row("F1", "data", settled=True), self.row("F2", "data", settled=True),
                      copy.deepcopy(before_rows[2])]
        after_rows[2]["claim"] = "Statistical estimate changed"
        before = self.write_rows("work/findings.before.jsonl", before_rows)
        self.write_rows("work/findings.jsonl", after_rows)
        legacy_policy = pinit.shipped_policy()
        legacy_policy["critique"]["later_rounds"]["routing_manifest"].pop("claim_change_rule")
        packets = {}
        for label, policy in (("legacy", legacy_policy), ("new", pinit.shipped_policy())):
            (self.audit / "pipeline-policy.snapshot.json").write_bytes(pinit.envelope_bytes(policy))
            result = self.run_diff(before, f"{label}.json")
            self.assertEqual(result.returncode, 0, result.stderr)
            packets[label] = json.loads((self.audit / f"critique/{label}.json").read_text())
        self.assertEqual(packets["new"]["specialist_claim_ids"], ["F3"])
        self.assertEqual(packets["new"]["recommended_roles"], ["stats"])
        self.assertEqual(packets["legacy"]["recommended_roles"], ["data"])


def load_tests(loader, tests, pattern):
    """Only the tests defined here; the borrowed fixtures' own tests run in their own files."""
    suite = unittest.TestSuite()
    for cls in (RuleGaps, PacketSectionCoupling, InitRefusesAnUnknownClaimRule, DriftJoinsTheNewModules,
                OutputLimitAtTheDefaultCap, DriverRecordsTheReviewedReport, ReceiptBoundToAnotherInvocation,
                DocxHeadingCarriesTheBreak, WaiterDefaultsToTwiceTheCeiling, DriverDeadlineIsTheFrozenCeiling,
                LegacyWaiterHonoursTheKnob, RouterKeepsOneSpecialist, BookkeepingFindingsRaiseNoEvidenceGap,
                SpecialistChosenFromSubstantiveRows):
        for name in sorted(vars(cls)):
            if name.startswith("test"):
                suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
