"""A record that started in the second a critic route was re-pinned still predates the re-pin (2.6.4).

Found while validating 2.6.4: the critique driver's re-pin scenario (tests/test_run_critique.sh,
[11/11]) failed intermittently with "existing invocation ledger line 1 is invalid: recorded request
violates pipeline policy: requested_model='gpt-5.6-terra', expected 'gpt-5.6-luna'". The failed
probe had started at 18:27:09Z and the re-pin was journaled at 18:27:10Z; both times are whole
seconds, and historical_route_for compared them with a strict "<", so whenever the probe and the
re-pin landed in the same second the probe was judged against the new route. The same comparison
decides whether a real audit re-pinned by scripts/repin_critic_route.py validates at finalize.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import record_invocation  # noqa: E402

OLD = {"requested_model": "gpt-5.6-terra", "provider": "openai", "reasoning_effort": "high"}
POLICY = {"critic_route_history": [{"superseded_at": "2026-09-25T18:27:10Z", "reason": "fixture",
                                    "critics": OLD, "targeted_adjudicator": OLD}]}


def probe(started: str, status: str = "failed") -> dict:
    return {"role": "model_probe", "role_class": "other", "status": status,
            "started_at": started, "ended_at": started}


class SameSecondRepin(unittest.TestCase):
    def test_probe_started_in_the_repin_second_is_judged_on_the_old_route(self) -> None:
        self.assertEqual(record_invocation.historical_route_for(probe("2026-09-25T18:27:10Z"), POLICY), [OLD])

    def test_probe_started_before_the_repin_is_judged_on_the_old_route(self) -> None:
        self.assertEqual(record_invocation.historical_route_for(probe("2026-09-25T18:27:09Z"), POLICY), [OLD])

    def test_negative_control_a_record_started_after_the_repin_gets_no_old_route(self) -> None:
        self.assertEqual(record_invocation.historical_route_for(probe("2026-09-25T18:27:11Z"), POLICY), [])

    def test_negative_control_a_completed_review_never_gets_an_old_route(self) -> None:
        review = {"role": "critic_stats", "role_class": "critic", "status": "completed",
                  "started_at": "2026-09-25T18:27:10Z"}
        self.assertEqual(record_invocation.historical_route_for(review, POLICY), [])


if __name__ == "__main__":
    unittest.main()
