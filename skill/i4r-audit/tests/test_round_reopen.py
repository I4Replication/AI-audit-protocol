"""A live-grant round that already reached quorum can be re-dispatched into.

`reserve_invocation` refused any dispatch when the round state was not ready/running, and on
refusal appended `round_authorization_required` -- regressing a finished round to
awaiting_authorization and the run to paused. That made the driver's own printed remedies
("rerun this round to retry adjudication only", `I4R_FORCE_RERUN=1`) dispatch nothing on their
first application while still debiting the attempt budget. The grant is live in both cases, so
the plan a human (or the unattended default) authorized still covers the call: the round is
reopened in the same locked append and the round's clock keeps its first start.
"""
import json
import pathlib
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import lifecycle_journal  # noqa: E402
from tests.test_metadata_2_1_lifecycle import base_run  # noqa: E402

PLAN = {
    "purpose": "round-1 critique", "provider": "openai",
    "model_route": "gpt-5.6-terra/high", "data_classes": ["report", "evidence"],
    "policy_sha256": "d" * 64,
}


def critic(invocation_id):
    return {
        "invocation_id": invocation_id, "role": "critic_synth", "role_class": "adjudicator",
        "attempt_kind": "role_inference", "round": 1, "provider": "openai",
        "requested_model": "gpt-5.6-terra", "reasoning_effort": "max",
    }


class RoundReopen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-reopen-")
        self.root = pathlib.Path(self.tmp.name)
        lifecycle_journal.initialize(self.root, base_run())

    def tearDown(self):
        self.tmp.cleanup()

    def _authorized_running_round(self, expires="2099-01-01T00:00:00Z"):
        prepared = lifecycle_journal.prepare_round(self.root, 1, PLAN)
        authorization = {k: prepared["plan"][k] for k in
                         ("purpose", "round_plan_sha256", "provider", "model_route",
                          "data_classes", "policy_sha256")}
        authorization["expires_at"] = expires
        lifecycle_journal.authorize_round(self.root, 1, authorization)
        lifecycle_journal.append_events(self.root, [
            {"type": "round_start", "payload": {"round": 1}, "at": "2026-09-01T10:00:00Z"}])

    def _types(self):
        return [json.loads(l)["type"] for l in
                (self.root / "run.events.jsonl").read_text().splitlines() if l.strip()]

    def _round(self):
        run = json.loads((self.root / "run.json").read_text())
        return next(r for r in run["rounds"] if r["round"] == 1)

    def test_adjudicator_retry_after_quorum_complete_is_accepted(self):
        self._authorized_running_round()
        lifecycle_journal.append_events(self.root, [
            {"type": "round_quorum_complete", "payload": {"round": 1, "critics_run": ["critic_stats"]},
             "at": "2026-09-01T10:30:00Z"}])
        self.assertEqual("quorum_complete", self._round()["state"])

        lifecycle_journal.reserve_invocation(self.root, critic("inv-retry"))

        types = self._types()
        self.assertNotIn("round_authorization_required", types)
        self.assertEqual("round_start", types[-2])
        self.assertEqual("invocation_reserved", types[-1])
        row = self._round()
        self.assertEqual("running", row["state"])
        self.assertEqual("quorum_complete", row["reopened_from"])
        self.assertEqual("2026-09-01T10:00:00Z", row["started_at"], "reopen must keep the first start")

    def test_forced_rerun_after_integrated_is_accepted(self):
        self._authorized_running_round()
        lifecycle_journal.append_events(self.root, [
            {"type": "round_quorum_complete", "payload": {"round": 1, "critics_run": ["critic_stats"]}},
            {"type": "round_integrated", "payload": {"round": 1, "integrated_at": "2026-09-01T11:00:00Z"}}])
        lifecycle_journal.reserve_invocation(self.root, critic("inv-forced"))
        self.assertEqual("running", self._round()["state"])
        self.assertEqual("integrated", self._round()["reopened_from"])
        self.assertNotIn("round_authorization_required", self._types())

    def test_an_expired_grant_still_refuses(self):
        """Negative control: reopening rides on a LIVE grant, never on a stale one.

        authorize_round refuses a grant that is already expired, so the fixture grants two
        seconds and lets them pass.
        """
        import datetime as dt
        import time
        soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2)
                ).isoformat(timespec="seconds").replace("+00:00", "Z")
        self._authorized_running_round(expires=soon)
        time.sleep(2.5)
        lifecycle_journal.append_events(self.root, [
            {"type": "round_quorum_complete", "payload": {"round": 1, "critics_run": ["critic_stats"]}}])
        with self.assertRaises(lifecycle_journal.AuthorizationRequired):
            lifecycle_journal.reserve_invocation(self.root, critic("inv-stale"))
        self.assertEqual("awaiting_authorization", self._round()["state"])

    def test_an_unauthorized_round_still_refuses(self):
        """Negative control: the original rule is intact for a round that never had a grant."""
        lifecycle_journal.prepare_round(self.root, 1, PLAN)
        with self.assertRaises(lifecycle_journal.AuthorizationRequired):
            lifecycle_journal.reserve_invocation(self.root, critic("inv-none"))
        self.assertIn("round_authorization_required", self._types())


if __name__ == "__main__":
    unittest.main()
