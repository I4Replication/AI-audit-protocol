"""2.7.4: later-round specialists re-review only substantive changes; completeness reviews every changed finding.

After an integration nearly every registry row differs from the round before, because binding a round's confirmations
rewrites ``verifications`` and approval rewrites ``settled``, so every later round sent every finding to every routed
critic. Those re-reviews also caught real problems (19 of 36 later-round disputes on 27 audits), so the programme lead
chose to narrow the specialists only. A new audit freezes the rule; an older audit froze no leaf and keeps the legacy
packet byte for byte. These tests run make_diff_packet.py, verify_diff_packet.py and the critique runtime's router.
"""
from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "tests"))
import claim_change_rules as rules  # noqa: E402
import record_invocation  # noqa: E402
import test_diff_packet as packet_tests  # noqa: E402  (the diff-packet audit fixture)

RUNTIME = SKILL / "scripts/test_run_critique_runtime.py"
NEW_KEYS = ("claim_change_rule", "specialist_claim_ids", "bookkeeping_only_claim_ids")


def shipped_policy() -> dict:
    return json.loads((SKILL / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))


def freeze(audit: pathlib.Path, policy: dict) -> None:
    envelope = {"snapshot_version": "1", "policy_sha256": record_invocation.policy_hash(policy), "policy": policy}
    (audit / "pipeline-policy.snapshot.json").write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n")


def legacy_policy() -> dict:
    policy = shipped_policy()
    policy["critique"]["later_rounds"]["routing_manifest"].pop("claim_change_rule")
    return policy


class Rule(unittest.TestCase):
    def test_the_shipped_policy_freezes_the_specialist_rule_and_legacy_needs_no_leaf(self) -> None:
        self.assertEqual(rules.rule_of(shipped_policy()), rules.SPECIALIST_RULE)
        self.assertEqual(rules.rule_of(legacy_policy()), rules.LEGACY_RULE)
        for value in (rules.LEGACY_RULE, "substantive_fields_1", None, 1):
            with self.subTest(value=value):
                policy = shipped_policy()
                policy["critique"]["later_rounds"]["routing_manifest"]["claim_change_rule"] = value
                with self.assertRaises(rules.RuleError):
                    rules.rule_of(policy)

    def test_bookkeeping_and_substance(self) -> None:
        import make_diff_packet
        base = copy.deepcopy(packet_tests.FIXTURE)
        base["coordinator_approved"] = False
        cases = {
            "settled bound": (lambda row: row.update(settled={"approved": True}), False),
            "approval bound": (lambda row: row.update(coordinator_approved=True), False),
            "confirmation added": (lambda row: row["verifications"].append(
                {**row["verifications"][0], "reviewer_id": "critic_fairness", "outcome": "confirmed"}), False),
            "claim reworded": (lambda row: row.update(claim="a different claim"), True),
            "confirmation lost": (lambda row: row.update(verifications=[]), True),
            "dispute added": (lambda row: row["verifications"].append(
                {**row["verifications"][0], "reviewer_id": "critic_stats", "outcome": "refuted"}), True),
        }
        for label, (change, substantive) in cases.items():
            with self.subTest(case=label):
                after = copy.deepcopy(base)
                change(after)
                found = rules.substantive_paths(base, after, make_diff_packet.changed_paths)
                self.assertEqual(bool(found), substantive, found)

    def test_a_finding_whose_report_section_changed_stays_with_the_specialists(self) -> None:
        import make_diff_packet
        row = copy.deepcopy(packet_tests.FIXTURE)
        after = copy.deepcopy(row)
        after["settled"] = {"approved": True}
        ids = rules.specialist_claim_ids([row["id"]], {row["id"]: row}, {row["id"]: after},
                                         make_diff_packet.changed_paths, ["Section 3"])
        self.assertEqual(ids, [row["id"]])  # its report_section is Section 3.1
        self.assertEqual(rules.specialist_claim_ids([row["id"]], {row["id"]: row}, {row["id"]: after},
                                                    make_diff_packet.changed_paths, ["Section 4"]), [])


class Packets(packet_tests.DiffPacketTests):
    def rows(self) -> tuple[pathlib.Path, list[dict]]:
        before_rows = [self.row("F1", "data"), self.row("F2", "statistics")]
        after_rows = copy.deepcopy(before_rows)
        after_rows[0] = self.row("F1", "data", settled=True)  # bookkeeping only: the approval is settled
        after_rows[1]["claim"] = "Statistical estimate changed"  # substantive
        before = self.write_rows("work/findings.before.jsonl", before_rows)
        self.write_rows("work/findings.jsonl", after_rows)
        return before, after_rows

    def build(self, name: str, policy: dict | None) -> dict:
        snapshot = self.audit / "pipeline-policy.snapshot.json"
        if policy is None:
            snapshot.unlink(missing_ok=True)
        else:
            freeze(self.audit, policy)
        result = self.run_diff(self.before, name)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.audit / "critique" / name).read_text())

    def test_a_legacy_audit_keeps_its_packet_bytes(self) -> None:
        self.before, _ = self.rows()
        no_snapshot = self.build("none.json", None)
        legacy = self.build("legacy.json", legacy_policy())
        self.assertEqual((self.audit / "critique/none.json").read_bytes(),
                         (self.audit / "critique/legacy.json").read_bytes())
        self.assertFalse(set(NEW_KEYS) & set(legacy))
        self.assertEqual(no_snapshot["changed_claim_ids"], ["F1", "F2"])

    def test_a_new_audit_lists_the_substantive_changes_for_the_specialists(self) -> None:
        self.before, _ = self.rows()
        legacy = self.build("legacy.json", legacy_policy())
        new = self.build("new.json", shipped_policy())
        self.assertEqual(new["claim_change_rule"], rules.SPECIALIST_RULE)
        self.assertEqual(new["changed_claim_ids"], ["F1", "F2"])  # completeness still sees both
        self.assertEqual(new["specialist_claim_ids"], ["F2"])
        self.assertEqual(new["bookkeeping_only_claim_ids"], ["F1"])
        # the specialist is chosen from the substantive rows (F2, statistics) under the rule; all else as legacy
        routing_keys = ("role_scores", "recommended_roles", "critical_domains", "routing_mode")
        self.assertEqual({key: value for key, value in new.items() if key not in NEW_KEYS + routing_keys},
                         {key: value for key, value in legacy.items() if key not in routing_keys})
        self.assertEqual(new["recommended_roles"], ["stats"])
        verified = subprocess.run(["python3", str(packet_tests.VERIFY_DIFF), str(self.audit),
                                   str(self.audit / "critique/new.json"), "--round", "2"], text=True, capture_output=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)


class Routing(unittest.TestCase):
    """The runtime's router, on a registry and a hand-shaped manifest."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-274-route-")
        self.ws = pathlib.Path(self.temporary.name)
        (self.ws / "work").mkdir()
        rows = []
        for finding_id, domain in (("F1", "data"), ("F2", "statistics"), ("F3", "statistics")):
            row = copy.deepcopy(packet_tests.FIXTURE)
            row.update(id=finding_id, domain=domain, disclosure_flag=False)
            rows.append(row)
        (self.ws / "work/findings.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def route(self, manifest: dict, override: str = "") -> dict:
        path = self.ws / "manifest.json"
        path.write_text(json.dumps({"round": 2, "changed_sections": [], **manifest}))
        out = self.ws / "routing.json"
        result = subprocess.run(["python3", str(RUNTIME), "route", "--workspace", str(self.ws), "--round", "2",
                                 "--manifest", str(path), "--output", str(out), "--override", override],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(out.read_text())

    def test_a_legacy_manifest_routes_as_before(self) -> None:
        routing = self.route({"changed_claim_ids": ["F1", "F2", "F3"], "recommended_roles": ["stats", "data"],
                              "routing_mode": "multi_domain_critical", "critical_domains": ["data", "statistics"]})
        self.assertEqual(routing["claim_assignment_mode"], "domain_routed_delta")
        self.assertEqual(routing["claim_assignments"],
                         {"stats": ["F2", "F3"], "data": ["F1"], "completeness": ["F1", "F2", "F3"]})

    def test_specialists_get_only_the_substantive_changes_and_an_empty_one_is_not_dispatched(self) -> None:
        manifest = {"changed_claim_ids": ["F1", "F2", "F3"], "recommended_roles": ["stats", "data"],
                    "routing_mode": "multi_domain_critical", "critical_domains": ["data", "statistics"],
                    "claim_change_rule": rules.SPECIALIST_RULE, "specialist_claim_ids": ["F2"]}
        routing = self.route(manifest)
        self.assertEqual(routing["claim_assignment_mode"], "domain_routed_delta")  # every consumer pins the mode
        self.assertEqual(routing["roles"], ["stats", "completeness"])
        self.assertEqual(routing["claim_assignments"], {"stats": ["F2"], "completeness": ["F1", "F2", "F3"]})
        self.assertIn("data not dispatched", routing["reason"])
        # an explicit CRITICS override is never second-guessed
        overridden = self.route(manifest, override="stats data completeness")
        self.assertEqual(overridden["roles"], ["stats", "data", "completeness"])
        self.assertEqual(overridden["claim_assignments"]["data"], [])

    def test_a_scope_outside_the_changed_claims_or_an_unknown_rule_is_refused(self) -> None:
        for manifest, fragment in (
            ({"changed_claim_ids": ["F1"], "claim_change_rule": rules.SPECIALIST_RULE,
              "specialist_claim_ids": ["F2"]}, "specialist_claim_ids are not changed claims"),
            ({"changed_claim_ids": ["F1"], "claim_change_rule": "other", "specialist_claim_ids": []},
             "unknown claim_change_rule"),
        ):
            with self.subTest(fragment=fragment):
                path = self.ws / "manifest.json"
                path.write_text(json.dumps({"round": 2, "changed_sections": [], **manifest}))
                result = subprocess.run(["python3", str(RUNTIME), "route", "--workspace", str(self.ws), "--round", "2",
                                         "--manifest", str(path), "--output", str(self.ws / "r.json")],
                                        text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(fragment, result.stderr)


def load_tests(loader, tests, pattern):
    """Only the tests defined here; the borrowed diff-packet fixture's own tests run in their own file."""
    suite = unittest.TestSuite()
    for cls in (Rule, Packets, Routing):
        for name in sorted(vars(cls)):
            if name.startswith("test"):
                suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
