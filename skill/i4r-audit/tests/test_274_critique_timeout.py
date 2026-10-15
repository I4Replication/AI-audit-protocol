"""2.7.4: the per-call critique ceiling is frozen per audit (scripts/critique_timeouts.py).

run_critique.sh read the ceiling from I4R_PROVIDER_TIMEOUT_SECONDS with a default of 2,700 s, set for GPT-5.6 Terra.
GPT-6.1 Sol generates at about a third of Terra's speed; its longest completed call on 2026-09-30 took 2,060 s and one
call hit 2,700 s. A new audit freezes critique.provider_timeout_seconds = 5400; an audit initialised before 2.7.4 has
no leaf and keeps 2,700 s and the environment knob exactly as before. These tests run the resolver, the real driver
up to its resolution step, and the real init_run.sh.
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
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "tests"))
import critique_timeouts  # noqa: E402
import test_270_policy_and_init as pinit  # noqa: E402  (shipped_policy, envelope_bytes, make_audit, init)

LEAF = "provider_timeout_seconds"


def legacy_policy() -> dict:
    """The shipped policy as every audit before 2.7.4 froze it: no critique ceiling leaf."""
    policy = pinit.shipped_policy()
    policy["critique"].pop(LEAF)
    return policy


class Resolver(unittest.TestCase):
    def test_the_shipped_policy_freezes_5400_and_the_module_keeps_2700_live(self) -> None:
        self.assertEqual(pinit.shipped_policy()["critique"][LEAF], 5400)
        self.assertEqual(critique_timeouts.LIVE_CRITIQUE_TIMEOUTS, (2700, 5400))
        self.assertEqual(critique_timeouts.frozen_ceiling(pinit.shipped_policy()), (5400, True))

    def test_a_legacy_audit_keeps_2700_and_the_knob_as_before(self) -> None:
        policy = legacy_policy()
        self.assertEqual(critique_timeouts.frozen_ceiling(policy), (2700, False))
        for override, expected in ((None, 2700), ("", 2700), ("600", 600), ("5401", 5401), ("9000", 9000)):
            with self.subTest(override=override):
                self.assertEqual(critique_timeouts.effective(policy, override), expected)

    def test_a_frozen_ceiling_may_only_be_lowered(self) -> None:
        policy = pinit.shipped_policy()
        self.assertEqual(critique_timeouts.effective(policy, None), 5400)
        self.assertEqual(critique_timeouts.effective(policy, "6"), 6)
        with self.assertRaisesRegex(critique_timeouts.CritiqueTimeoutError, "may only lower it"):
            critique_timeouts.effective(policy, "5401")

    def test_bad_values_are_refused(self) -> None:
        for value in (3600, 2700.5, True, "5400", None):
            with self.subTest(value=value):
                policy = pinit.shipped_policy()
                policy["critique"][LEAF] = value
                with self.assertRaisesRegex(critique_timeouts.CritiqueTimeoutError, "not a packaged critique timeout"):
                    critique_timeouts.frozen_ceiling(policy)
        for override in ("0", "-5", "abc", "1.5"):
            with self.subTest(override=override):
                with self.assertRaisesRegex(critique_timeouts.CritiqueTimeoutError, "positive integer"):
                    critique_timeouts.effective(legacy_policy(), override)


class DriverResolution(unittest.TestCase):
    """The real run_critique.sh, stopped right after it resolves the ceiling (the audit has no run.json)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-274-ctimeout-")
        self.base = pathlib.Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def drive(self, policy: dict, override: str | None) -> subprocess.CompletedProcess[str]:
        audit = self.base / ("frozen" if LEAF in policy["critique"] else "legacy") / str(override)
        audit.mkdir(parents=True)
        (audit / "pipeline-policy.snapshot.json").write_bytes(pinit.envelope_bytes(policy))
        env = {key: value for key, value in os.environ.items() if key != "I4R_PROVIDER_TIMEOUT_SECONDS"}
        env.update(AUDIT_DIR=str(audit), CODEX="/nonexistent/codex", I4R_CODEX_KNOWN_PATHS="/nonexistent/codex")
        if override is not None:
            env["I4R_PROVIDER_TIMEOUT_SECONDS"] = override
        result = subprocess.run(["bash", str(SKILL / "scripts/run_critique.sh"), "1"], text=True, capture_output=True,
                                env=env, timeout=120, check=False)
        self.assertFalse((audit / "critique" / "round_1").exists(), "nothing past the preflight is written")
        return result

    def test_a_frozen_audit_announces_its_ceiling_and_refuses_a_higher_knob(self) -> None:
        result = self.drive(pinit.shipped_policy(), None)
        self.assertIn("[limit] provider call ceiling 5400 s", result.stderr)
        lowered = self.drive(pinit.shipped_policy(), "60")
        self.assertIn("[limit] provider call ceiling 60 s", lowered.stderr)
        raised = self.drive(pinit.shipped_policy(), "6000")
        self.assertEqual(raised.returncode, 1, raised.stderr)
        self.assertIn("FATAL: I4R_PROVIDER_TIMEOUT_SECONDS=6000 exceeds this audit's frozen critique ceiling of 5400 s",
                      raised.stderr)

    def test_a_legacy_audit_is_silent_and_takes_any_knob_as_before(self) -> None:
        for override in (None, "6000"):
            with self.subTest(override=override):
                result = self.drive(legacy_policy(), override)
                self.assertNotIn("[limit]", result.stderr)
                self.assertNotIn("critique ceiling", result.stderr)


class InitPrintsAndRefuses(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-274-ctimeout-init-")
        self.root = pathlib.Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_new_audit_prints_5400(self) -> None:
        audit = pinit.make_audit(self.root / "new" / "audit")
        result = pinit.init(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("\n  critique timeout:  5400 seconds per critic or adjudicator call\n", result.stdout)

    def test_a_leftover_with_a_ceiling_no_driver_admits_is_refused_untouched(self) -> None:
        policy = pinit.shipped_policy()
        policy["critique"][LEAF] = 3600
        data = pinit.envelope_bytes(policy)
        audit = pinit.make_audit(self.root / "odd" / "audit")
        (audit / pinit.SNAPSHOT).write_bytes(data)
        result = pinit.init(audit)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("freezes a critique ceiling this skill does not run", result.stderr)
        self.assertIn("remove it (rm ", result.stderr)
        self.assertEqual((audit / pinit.SNAPSHOT).read_bytes(), data)
        self.assertFalse((audit / "run.json").exists())


if __name__ == "__main__":
    unittest.main()
