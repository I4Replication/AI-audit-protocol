#!/usr/bin/env python3
"""A cached reviewer report must still count as a confirmation -- in the SHIPPED code.

A `cache_replay` row is bookkeeping: it re-serves the bytes an earlier provider call produced and
records that call in `source_invocation_id`. Any resume replays the roles that already completed,
so a legitimately cached report carries a `cache-` invocation id in its verification. Both
`make_settled.sh` and `make_diff_packet.py` resolve that pointer, and both must agree, or a
settled cache passes one and fails the other.

The previous version of this module tested a copy of the walk written inside the test file and
grepped the two scripts for a substring: deleting the resolution from both shipped scripts left
the whole suite green. These tests drive the two scripts on the diff-packet fixture.
"""
from __future__ import annotations

import json
import subprocess
import unittest

# Import the MODULE, not the class: a TestCase class imported into this namespace is
# collected and run again by the loader as if it were defined here.
from tests import test_diff_packet

SETTLED = test_diff_packet.SETTLED


class ReplayConfirmationsFlowThroughBothScripts(test_diff_packet.DiffPacketTests):
    def _ledger(self) -> list[dict]:
        path = self.audit / "invocations.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    def _write_ledger(self, rows: list[dict]) -> None:
        (self.audit / "invocations.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))

    def _point_confirmation_at(self, invocation_id: str) -> None:
        for confirmation in self.confirmations:
            if confirmation["reviewer_id"] == "auditor_reproduction":
                confirmation["invocation_id"] = invocation_id

    def _replay_of(self, source_id: str, replay_id: str, *, source_pointer: str | None) -> dict:
        source = next(row for row in self._ledger() if row["invocation_id"] == source_id)
        replay = dict(source)
        # A row carrying attempt_kind is validated against the full 2.x contract, so it needs the
        # whole field set (the shape make_settled.sh's own self-test fixture uses).
        replay.update({
            "invocation_id": replay_id, "provider_call": False, "attempt_kind": "cache_replay",
            "cache_usage": "read", "source_invocation_id": source_pointer,
            "served_model": source.get("observed_model"), "identity_assurance": "observed",
            "model_identity_evidence": None, "provider_request_status": "not_requested",
            "inference_start_status": "not_started", "attempt": None, "turn_count": None,
            "provider_cost": None, "usage_by_model": [], "unattributed_usage": source["usage"],
            "provider_duration_seconds": None,
        })
        return replay

    def _settle_and_diff(self) -> tuple[subprocess.CompletedProcess, subprocess.CompletedProcess]:
        settled = self.row("F1", "statistics", settled=True)
        changed_before = self.row("F2", "data")
        changed_after = json.loads(json.dumps(changed_before))
        changed_after["claim"] = "Participant count corrected"
        changed_after["dependencies"]["finding_ids"] = ["F1"]
        before = self.write_rows("work/findings.before.jsonl", [settled, changed_before])
        self.write_rows("work/findings.jsonl", [settled, changed_after])
        made = subprocess.run(["bash", str(SETTLED), str(self.audit)], text=True, capture_output=True)
        diff = self.run_diff(before, "replay.json") if made.returncode == 0 else made
        return made, diff

    def test_a_replay_confirmation_settles_and_routes(self) -> None:
        ledger = self._ledger()
        ledger.append(self._replay_of("diff-inv-a", "cache-1", source_pointer="diff-inv-a"))
        self._write_ledger(ledger)
        self._point_confirmation_at("cache-1")
        made, diff = self._settle_and_diff()
        self.assertEqual(made.returncode, 0, made.stderr + made.stdout)
        self.assertEqual(diff.returncode, 0, diff.stderr)
        packet = json.loads((self.audit / "critique/replay.json").read_text())
        self.assertEqual([row["fact_id"] for row in packet["settled_fact_references"]], ["F1"])

    def test_a_replay_cycle_is_refused_not_spun_on(self) -> None:
        """Negative control: the walk terminates and names the fault."""
        ledger = self._ledger()
        ledger.append(self._replay_of("diff-inv-a", "cache-a", source_pointer="cache-b"))
        ledger.append(self._replay_of("diff-inv-a", "cache-b", source_pointer="cache-a"))
        self._write_ledger(ledger)
        self._point_confirmation_at("cache-a")
        made, diff = self._settle_and_diff()
        self.assertNotEqual(made.returncode, 0, "make_settled.sh accepted a replay cycle")
        text = made.stderr + made.stdout
        self.assertTrue("replay" in text.lower() or "source" in text.lower(), text[-800:])

    def test_a_replay_with_no_source_is_refused(self) -> None:
        ledger = self._ledger()
        ledger.append(self._replay_of("diff-inv-a", "cache-x", source_pointer=None))
        self._write_ledger(ledger)
        self._point_confirmation_at("cache-x")
        made, _ = self._settle_and_diff()
        self.assertNotEqual(made.returncode, 0)


# Reuse the parent's fixture, not its tests: those run in their own module.
for _name in [n for n in dir(test_diff_packet.DiffPacketTests) if n.startswith("test_")]:
    setattr(ReplayConfirmationsFlowThroughBothScripts, _name, None)
del _name


if __name__ == "__main__":
    unittest.main()
