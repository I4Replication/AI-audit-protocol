"""2.7.4 (fix of 2026-10-05): every critique route lists its roles in the canonical order.

The deterministic claim merge (build_claim_merge in scripts/test_run_critique_runtime.py) orders its roles as ROLES
does: stats, data, fairness, completeness. The round projection then compares that list with the routed roles, as a
list. The route command kept a CRITICS override in the order it was written. On audit
A18, round 2 run with CRITICS="completeness fairness" dispatched both critics, which completed,
and then failed the projection with "claim merge identity or role quorum differs". The rerun in canonical order changed
the round's input hash, so both critics ran again. The route now writes canonical order whatever order the override
gives.

The fixture is a claim-free workspace: an empty work/findings.jsonl and no diff manifest, which the route command
tolerates. No provider or model is called.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
RUNTIME = SKILL / "scripts" / "test_run_critique_runtime.py"
CANONICAL = ["stats", "data", "fairness", "completeness"]


def route(round_number: int, override: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = pathlib.Path(tmp) / "audit"
        (workspace / "work").mkdir(parents=True)
        (workspace / "work" / "findings.jsonl").write_text("", encoding="utf-8")
        output = pathlib.Path(tmp) / "ROUTING.json"
        subprocess.run([sys.executable, str(RUNTIME), "route", "--workspace", str(workspace), "--round", str(round_number),
                        "--override", override, "--output", str(output)],
                       check=True, capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        return json.loads(output.read_text(encoding="utf-8"))["roles"]


class CanonicalRouteOrder(unittest.TestCase):
    def test_later_round_override_is_written_in_canonical_order(self) -> None:
        for override, expected in (("completeness fairness", ["fairness", "completeness"]),
                                   ("completeness stats", ["stats", "completeness"]),
                                   ("critic_completeness, critic_data", ["data", "completeness"]),
                                   ("fairness", ["fairness", "completeness"])):
            with self.subTest(override=override):
                self.assertEqual(route(2, override), expected)

    def test_round_one_and_the_default_route_are_canonical(self) -> None:
        self.assertEqual(route(1, "completeness fairness data stats"), CANONICAL)
        self.assertEqual(route(2, ""), ["fairness", "completeness"])

    def test_routed_order_is_the_claim_merge_order(self) -> None:
        # The projection compares the merge's roles with the routed roles as lists.
        roles = route(2, "completeness data")
        self.assertEqual(roles, sorted(roles, key=CANONICAL.index))


if __name__ == "__main__":
    unittest.main()
