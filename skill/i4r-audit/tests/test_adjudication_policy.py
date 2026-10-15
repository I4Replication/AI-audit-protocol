#!/usr/bin/env python3
"""Focused policy tests for the deterministic targeted-adjudication trigger projector."""
from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import test_run_critique_runtime as runtime


def assessment(finding_id: str, verdict: str = "CONFIRMED") -> dict[str, object]:
    return {
        "item_id": f"item-{finding_id}",
        "finding_id": finding_id,
        "claim": f"Bounded claim {finding_id}",
        "verdict": verdict,
        "priority": "NON_BLOCKING",
        "confidence": "CONFIRMED",
        "evidence": {
            "artifact_refs": [{"path": f"source/evidence/work/{finding_id}.txt",
                               "sha256": "a" * 64}],
            "operation": "Compare the canonical claim with the frozen evidence artifact.",
            "result": f"The frozen evidence yields the bounded {finding_id} result.",
            "reproducible_script": None,
        },
        "requested_correction": (
            "Narrow the claim." if verdict in {"OVERSTATED", "REFUTED"} else None
        ),
    }


def critic(*items: dict[str, object], failed_spot_check: bool = False,
           new_issues: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "items": list(items),
        "new_issues": new_issues or [],
        "conflicts": [],
        "settled_spot_checks": ([{
            "fact_id": "F-settled", "status": "failed", "evidence": "hash changed",
        }] if failed_spot_check else []),
    }


class AdjudicationPolicyTests(unittest.TestCase):
    def kinds(self, rows, registry=None) -> list[str]:
        return [row["kind"] for row in runtime.adjudication_triggers(rows, registry or {})]

    def test_only_a_verdict_disagreement_triggers_an_ordinary_claim(self) -> None:
        rows = [
            ("stats", critic(assessment("F1", "OVERSTATED"))),
            ("data", critic(assessment("F1", "CONFIRMED"))),
        ]
        self.assertEqual(self.kinds(rows), ["verdict_conflict"])

    def test_nonconfirmed_blocking_and_new_items_do_not_trigger_by_themselves(self) -> None:
        ordinary = assessment("F1", "OVERSTATED")
        ordinary["priority"] = "BLOCKING"
        new_issue = assessment("NEW1")
        new_issue["finding_id"] = None
        rows = [("stats", critic(ordinary, new_issues=[new_issue]))]
        self.assertEqual(self.kinds(rows), [])

    def test_disputed_conclusion_changing_claim_is_targeted(self) -> None:
        registry = {"f1": {"id": "F1", "changes_conclusion": True, "impact_rank": 2}}
        rows = [("stats", critic(assessment("F1", "REFUTED")))]
        self.assertEqual(self.kinds(rows, registry), ["disputed_conclusion_changing_claim"])

    def test_high_impact_requires_two_independently_bound_critic_records(self) -> None:
        registry = {"f1": {"id": "F1", "changes_conclusion": False, "impact_rank": 3}}
        one = [("stats", critic(assessment("F1")))]
        two = one + [("data", critic(assessment("F1")))]
        self.assertEqual(self.kinds(one, registry), ["high_impact_evidence_gap"])
        self.assertEqual(self.kinds(two, registry), [])

    def test_a_merged_or_excluded_high_impact_row_raises_no_evidence_gap(self) -> None:
        """2.6.2: rows merged into another finding carry no evidence obligation of their own."""
        for status in ("MERGED", "EXCLUDED"):
            registry = {"f18": {"id": "F18", "changes_conclusion": False, "impact_rank": 3,
                                "ledger_disposition": {"status": status, "target": "F1", "reason": "x"}}}
            self.assertEqual(self.kinds([("stats", critic(assessment("F1")))], registry), [], status)
        included = {"f1": {"id": "F1", "changes_conclusion": False, "impact_rank": 3,
                           "ledger_disposition": {"status": "INCLUDED", "target": None, "reason": None}}}
        self.assertEqual(self.kinds([("stats", critic(assessment("F1")))], included), ["high_impact_evidence_gap"])

    def test_failed_settled_spot_check_is_explicit_cache_conflict(self) -> None:
        rows = [("fairness", critic(failed_spot_check=True))]
        self.assertEqual(self.kinds(rows), ["settled_fact_cache_conflict"])

    def test_claim_merge_is_deterministic_and_exposes_only_unresolved_rows(self) -> None:
        rows = [
            ("data", critic(assessment("F1", "CONFIRMED"))),
            ("stats", critic(assessment("F1", "OVERSTATED"))),
        ]
        triggers = runtime.adjudication_triggers(rows, {})
        triggers[0]["trigger_id"] = "T1"
        first = runtime.build_claim_merge(
            rows, {}, triggers, audit_id="audit-one", round_number=1,
            input_sha="a" * 64, report_sha="b" * 64,
        )
        second = runtime.build_claim_merge(
            list(reversed(rows)), {}, triggers, audit_id="audit-one", round_number=1,
            input_sha="a" * 64, report_sha="b" * 64,
        )
        self.assertEqual(first, second)
        claim = first["claims"][0]
        self.assertEqual([row["role"] for row in claim["role_results"]],
                         ["critic_stats", "critic_data"])
        self.assertTrue(claim["conflict"])
        self.assertFalse(claim["agreement"])
        self.assertEqual(claim["requested_corrections"], ["Narrow the claim."])
        unresolved = runtime.unresolved_rows_for_packet(triggers, first)
        self.assertEqual([row["row_id"] for row in unresolved], ["F1"])
        self.assertEqual(unresolved[0]["claim_merge"], claim)


if __name__ == "__main__":
    unittest.main()
