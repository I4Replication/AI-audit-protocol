"""2.7.5: every critique round of a new audit runs on gpt-6.1-sol, and nothing moves an audit off it.

From 2026-09-30 to 2026-10-07 at least twelve audits initialised on 2.7.0-2.7.4 were re-pinned to gpt-5.6-terra,
several within seconds of init, because their hosts' Codex accounts refused gpt-6.1-sol ("not supported when using Codex with a
ChatGPT account"). 2.7.5 closes both ends:

* scripts/check_critic_access.py makes one minimal call in the driver's exact command shape and passes only
  when the policy's critic route answers; new_audit.sh runs it before it reserves a paper or copies anything;
* scripts/repin_critic_route.py moves an audit only TO the shipped route.

No provider is called: tests/fake_codex.sh answers the access check (FAKE_UNSUPPORTED_MODEL refuses a model the
way the real account did).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS = pathlib.Path(__file__).resolve().parent
SKILL = TESTS.parent
SCRIPTS = SKILL / "scripts"
for _path in (str(SCRIPTS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import test_270_critic_route as t270  # noqa: E402  (fixtures only; its tests stay in its own module)
import test_tree_layout as layout  # noqa: E402

FAKE = TESTS / "fake_codex.sh"
CHECK = SCRIPTS / "check_critic_access.py"
SOL, TERRA = t270.SOL, t270.TERRA
CRITIC_EFFORT = t270.shipped_policy()["models"]["critics"]["reasoning_effort"]
REFUSAL = "since 2.7.5 a re-pin can only move an audit to the shipped critic route"


def access_env(state: pathlib.Path, **extra: str) -> dict[str, str]:
    env = t270.clean_env(CODEX=str(FAKE), FAKE_CODEX_STATE=str(state / "fake"), I4R_STATE_DIR=str(state / "home"),
                         CODEX_HOME=str(state / "codex_home"))
    for name in ("FAKE_ACCESS_SCENARIO", "FAKE_CODEX_LOGIN", "FAKE_CODEX_VERSION"):
        env.pop(name, None)
    env.update(extra)
    return env


def run_check(state: pathlib.Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CHECK), *args], text=True, capture_output=True, timeout=120,
                          check=False, env=access_env(state, **extra))


def access_calls(state: pathlib.Path) -> int:
    path = state / "fake" / "access_calls"
    return int(path.read_text()) if path.is_file() else 0


def stamp(state: pathlib.Path) -> dict:
    return json.loads((state / "home" / "critic_access.json").read_text(encoding="utf-8"))


class CriticAccessCheck(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-275-access-")
        self.addCleanup(directory.cleanup)
        self.state = pathlib.Path(directory.name)

    def test_a_pass_is_cached_for_a_day_and_checked_again_after(self) -> None:
        first = run_check(self.state)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn(f"critic access OK: {SOL} at {CRITIC_EFFORT}", first.stdout)
        again = run_check(self.state)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(access_calls(self.state), 1, "a pass from the last 24 h is reused")
        record = stamp(self.state)
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
        record["checked_at"] = old.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        (self.state / "home" / "critic_access.json").write_text(json.dumps(record), encoding="utf-8")
        stale = run_check(self.state)
        self.assertEqual(stale.returncode, 0, stale.stdout + stale.stderr)
        self.assertEqual(access_calls(self.state), 2, "a pass older than 24 h is checked again")
        forced = run_check(self.state, "--force")
        self.assertEqual(forced.returncode, 0)
        self.assertEqual(access_calls(self.state), 3, "--force always calls")

    def test_the_call_asks_for_the_policy_critic_route(self) -> None:
        self.assertEqual(run_check(self.state).returncode, 0)
        requests = [json.loads(line) for line in (self.state / "fake/access_requests.jsonl").read_text().splitlines()]
        self.assertEqual(requests, [{"model": SOL, "effort": CRITIC_EFFORT}])

    def test_a_changed_login_or_cli_is_checked_again(self) -> None:
        self.assertEqual(run_check(self.state).returncode, 0)
        self.assertEqual(run_check(self.state, FAKE_CODEX_LOGIN="Logged in using ChatGPT").returncode, 0)
        self.assertEqual(access_calls(self.state), 2, "another login is another account")
        record = stamp(self.state)
        record["cli_version"] = "0.159.0"
        (self.state / "home" / "critic_access.json").write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(run_check(self.state, FAKE_CODEX_LOGIN="Logged in using ChatGPT").returncode, 0)
        self.assertEqual(access_calls(self.state), 3, "another CLI version is checked again")

    def test_an_account_without_sol_is_refused_with_the_remedy_and_never_cached_as_a_pass(self) -> None:
        refused = run_check(self.state, FAKE_UNSUPPORTED_MODEL=SOL)
        self.assertEqual(refused.returncode, 3, refused.stdout + refused.stderr)
        self.assertIn(f"this host cannot use the critic model {SOL}", refused.stderr)
        self.assertIn("not supported when using Codex with a ChatGPT account", refused.stderr)
        self.assertIn("(unsupported_model)", refused.stderr)
        self.assertIn("login --with-api-key", refused.stderr)
        self.assertEqual(stamp(self.state)["result"], "refused")
        again = run_check(self.state, FAKE_UNSUPPORTED_MODEL=SOL)
        self.assertEqual(again.returncode, 3)
        self.assertEqual(access_calls(self.state), 2, "a refusal is checked again every time, never reused")
        fixed = run_check(self.state)
        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        self.assertEqual(stamp(self.state)["result"], "ok")

    def test_negative_control_an_account_without_terra_passes(self) -> None:
        result = run_check(self.state, FAKE_UNSUPPORTED_MODEL=TERRA)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_network_failure_asks_for_a_rerun_and_is_not_a_pass(self) -> None:
        failed = run_check(self.state, FAKE_ACCESS_SCENARIO="transport")
        self.assertEqual(failed.returncode, 4, failed.stdout + failed.stderr)
        self.assertIn("(transport)", failed.stderr)
        self.assertIn("run the check again", failed.stderr)
        self.assertEqual(stamp(self.state)["result"], "failed")

    def test_no_codex_cli_is_refused(self) -> None:
        result = subprocess.run([sys.executable, str(CHECK)], text=True, capture_output=True, timeout=60,
                                env=access_env(self.state, CODEX="/nonexistent/codex"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no working Codex CLI", result.stderr)

    def test_the_cached_login_line_holds_no_key_and_no_address(self) -> None:
        for login in ("Logged in using an API key - sk-proj-abcdefghijklmnop", "Logged in using ChatGPT (tester@example.org)"):
            result = run_check(self.state, "--force", FAKE_CODEX_LOGIN=login)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for text in (result.stdout, json.dumps(stamp(self.state))):
                self.assertNotIn("abcdefghijklmnop", text)
                self.assertNotIn("tester@example.org", text)

    def test_another_account_is_checked_again(self) -> None:
        """Every ChatGPT login prints the same status line; the login store's digest tells two accounts apart."""
        home = self.state / "codex_home"
        home.mkdir()
        (home / "auth.json").write_text('{"tokens": "account A"}')
        self.assertEqual(run_check(self.state, FAKE_CODEX_LOGIN="Logged in using ChatGPT").returncode, 0)
        (home / "auth.json").write_text('{"tokens": "account B"}')
        refused = run_check(self.state, FAKE_CODEX_LOGIN="Logged in using ChatGPT", FAKE_UNSUPPORTED_MODEL=SOL)
        self.assertEqual(refused.returncode, 3, refused.stdout + refused.stderr)
        self.assertEqual(access_calls(self.state), 2, "a pass for account A is not reused for account B")
        self.assertNotIn("account B", json.dumps(stamp(self.state)), "only a digest of the login store is kept")

    def test_a_stamp_without_a_time_zone_is_checked_again_not_a_crash(self) -> None:
        self.assertEqual(run_check(self.state).returncode, 0)
        record = stamp(self.state)
        record["checked_at"] = record["checked_at"].rstrip("Z")
        (self.state / "home" / "critic_access.json").write_text(json.dumps(record), encoding="utf-8")
        again = run_check(self.state)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertNotIn("Traceback", again.stderr)
        self.assertEqual(access_calls(self.state), 2)

    def test_a_provider_refusal_of_the_account_or_key_is_an_account_refusal(self) -> None:
        for scenario, words in (("key_without_model", "does not exist or you do not have access"),
                                ("provider_other", "The request was rejected")):
            with self.subTest(scenario=scenario):
                result = run_check(self.state, "--force", FAKE_ACCESS_SCENARIO=scenario, FAKE_CODEX_VERSION="0.160.1")
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn(words, result.stderr)
                self.assertNotIn("rejected the access check itself", result.stderr, "not a CLI rejection")

    def test_a_cli_that_rejects_the_call_is_told_to_update_not_to_retry(self) -> None:
        result = run_check(self.state, FAKE_ACCESS_SCENARIO="cli_rejects")
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertIn("unexpected argument", result.stderr, "the CLI's own error, not its usage line")
        self.assertIn("Update the ChatGPT app", result.stderr)

    def test_an_old_cli_is_told_to_update_before_changing_accounts(self) -> None:
        old = run_check(self.state, FAKE_UNSUPPORTED_MODEL=SOL, FAKE_CODEX_VERSION="0.158.0-alpha.2.1")
        self.assertEqual(old.returncode, 3, old.stdout + old.stderr)
        self.assertIn("older than 0.159.0", old.stderr)
        self.assertIn("Update the ChatGPT app", old.stderr)
        self.assertNotIn("login --with-api-key", old.stderr)
        new = run_check(self.state, FAKE_UNSUPPORTED_MODEL=SOL, FAKE_CODEX_VERSION="0.160.1")
        self.assertEqual(new.returncode, 3, new.stdout + new.stderr)
        self.assertIn("login --with-api-key", new.stderr)
        self.assertIn("the user's", new.stderr, "changing the account is the user's decision")

    def test_the_call_has_the_driver_command_shape(self) -> None:
        """The check and the critique driver must hit the same command shape, or a pass would prove nothing."""
        log = self.state / "argv.jsonl"
        wrapper = self.state / "codex"
        wrapper.write_text("#!/bin/bash\n"
                           f"python3 -c 'import json,sys; open(sys.argv[1],\"a\").write(json.dumps(sys.argv[2:])+\"\\n\")' "
                           f"\"{log}\" \"$@\"\n"
                           f"exec bash \"{FAKE}\" \"$@\"\n", encoding="utf-8")
        wrapper.chmod(0o755)
        result = subprocess.run([sys.executable, str(CHECK)], text=True, capture_output=True, timeout=60,
                                env=access_env(self.state, CODEX=str(wrapper)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        check_argv = next(json.loads(line) for line in log.read_text().splitlines() if json.loads(line)[:1] == ["exec"])

        runtime = t270._module("test_run_critique_runtime")
        captured: list[list[str]] = []
        args = argparse.Namespace(pid_file=str(self.state / "pid"), codex="codex", model=SOL, effort=CRITIC_EFFORT,
                                  workspace="WS", schema="SCHEMA", response="RESPONSE")
        with mock.patch.object(runtime.os, "setsid"), \
                mock.patch.object(runtime.os, "execv", side_effect=lambda path, argv: captured.append(argv)):
            runtime.command_provider_exec(args)
        driver_argv = captured[0][1:]

        def shape(argv: list[str]) -> list[str]:
            out, values = [], {"-C", "--output-schema", "-o"}
            skip = False
            for item in argv:
                if skip:
                    out.append("<path>"); skip = False; continue
                out.append(item)
                skip = item in values
            return out

        self.assertEqual(shape(check_argv), shape(driver_argv))


class NewAuditRefusesAHostWithoutSol(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-275-intake-")
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()
        self.state = self.root.parent / (self.root.name + "-access")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, ignore_errors=True))

    def inventory(self) -> set[str]:
        return {str(path.relative_to(self.root)) for path in self.root.rglob("*")}

    def test_nothing_is_reserved_or_copied_and_the_reason_is_shown(self) -> None:
        paper = layout.make_paper(self.root, "J", "P")
        before = self.inventory()
        result = layout.sh("/bin/bash", SCRIPTS / "new_audit.sh", paper,
                           env={"FAKE_CODEX_STATE": str(self.state / "fake"), "I4R_STATE_DIR": str(self.state / "home"),
                                "CODEX_HOME": str(self.state / "codex_home"),
                                "FAKE_UNSUPPORTED_MODEL": SOL})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"this host cannot use the critic model {SOL}", result.stderr)
        self.assertIn("no audit started, nothing reserved", result.stderr)
        self.assertEqual(self.inventory(), before, "the tree is exactly as it was")

    def test_positive_control_a_host_with_sol_starts_the_audit(self) -> None:
        paper = layout.make_paper(self.root, "J", "P")
        result = layout.sh("/bin/bash", SCRIPTS / "new_audit.sh", paper,
                           env={"FAKE_CODEX_STATE": str(self.state / "fake"), "I4R_STATE_DIR": str(self.state / "home")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("critic access OK", result.stdout)
        self.assertTrue((self.root / "runs" / "J" / "P" / "audit").is_dir())

    def test_input_errors_come_before_the_call(self) -> None:
        paper = layout.make_paper(self.root, "J", "P", pdfs=("a.pdf", "b.pdf"))
        result = layout.sh("/bin/bash", SCRIPTS / "new_audit.sh", paper,
                           env={"FAKE_CODEX_STATE": str(self.state / "fake"), "I4R_STATE_DIR": str(self.state / "home")})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--paper <pdf>", result.stderr)
        self.assertEqual(access_calls(self.state), 0, "an input error costs no call")


class InitFreezesTheLockedRoute(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-275-init-")
        self.root = pathlib.Path(directory.name).resolve()
        self.state = self.root.parent / (self.root.name + "-access")
        self.addCleanup(lambda: (t270.thaw(self.root), directory.cleanup(),
                                 __import__("shutil").rmtree(self.state, ignore_errors=True)))
        paper = layout.make_paper(self.root, "J", "P")
        started = layout.sh("/bin/bash", SCRIPTS / "new_audit.sh", paper,
                            env={"FAKE_CODEX_STATE": str(self.state / "fake"), "I4R_STATE_DIR": str(self.state / "home"),
                                 "CODEX_HOME": str(self.state / "codex_home")})
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.audit = self.root / "runs" / "J" / "P" / "audit"

    def init(self) -> subprocess.CompletedProcess[str]:
        return layout.sh("/bin/bash", SCRIPTS / "init_run.sh", self.audit, "--doi", "10.1234/abc.275",
                         "--paper-title", "P")

    def test_a_new_audit_freezes_and_prints_the_locked_sol_route(self) -> None:
        result = self.init()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"critics {SOL} at {CRITIC_EFFORT}", result.stdout)
        self.assertIn("locked (2.7.5)", result.stdout)
        policy = t270._module("record_invocation").load_policy_snapshot(self.audit)
        self.assertEqual(policy["critique"]["critic_route_lock"]["model"], SOL)
        self.assertEqual(t270.frozen_critic_route(self.audit), (SOL, SOL, SOL))

    def test_a_leftover_on_another_critic_route_is_refused_and_left_as_it_was(self) -> None:
        policy = self.root / "terra-policy.json"
        policy.write_text(json.dumps(t270.on_critic_route(t270.shipped_policy(), TERRA), indent=2) + "\n",
                          encoding="utf-8")
        record_invocation = t270._module("record_invocation")
        with mock.patch.object(record_invocation, "POLICY", policy):
            record_invocation.initialize_policy_snapshot(self.audit)
        before = (self.audit / "pipeline-policy.snapshot.json").read_bytes()
        result = self.init()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("freezes a critic route this skill does not ship", result.stderr)
        self.assertEqual((self.audit / "pipeline-policy.snapshot.json").read_bytes(), before)
        self.assertFalse((self.audit / "run.json").exists())


class LeftoverWithoutTheLockIsRefused(unittest.TestCase):
    def test_a_leftover_frozen_before_the_lock_is_refused_and_left_as_it_was(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-275-unlocked-") as directory:
            root = pathlib.Path(directory).resolve()
            state = root / ".access"
            self.addCleanup(t270.thaw, root)
            paper = layout.make_paper(root, "J", "P")
            env = {"FAKE_CODEX_STATE": str(state / "fake"), "I4R_STATE_DIR": str(state / "home"),
                   "CODEX_HOME": str(state / "codex_home")}
            self.assertEqual(layout.sh("/bin/bash", SCRIPTS / "new_audit.sh", paper, env=env).returncode, 0)
            audit = root / "runs" / "J" / "P" / "audit"
            policy = t270.shipped_policy()
            del policy["critique"]["critic_route_lock"]   # what 2.7.4's init froze
            source = root / "unlocked-policy.json"
            source.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
            record_invocation = t270._module("record_invocation")
            with mock.patch.object(record_invocation, "POLICY", source):
                record_invocation.initialize_policy_snapshot(audit)
            before = (audit / "pipeline-policy.snapshot.json").read_bytes()
            result = layout.sh("/bin/bash", SCRIPTS / "init_run.sh", audit, "--doi", "10.1234/abc.275")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("predates the critic-route lock", result.stderr)
            self.assertEqual((audit / "pipeline-policy.snapshot.json").read_bytes(), before)
            self.assertFalse((audit / "run.json").exists())


class LedgerRefusesReviewsOffTheLock(unittest.TestCase):
    """An older copy of the driver can run a round after an older copy of the re-pin tool moved the route; the
    2.7.5 ledger rule then keeps that audit from being integrated, finalized or published."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-275-ledger-")
        self.base = pathlib.Path(directory.name)
        self.addCleanup(lambda: (t270.thaw(self.base), directory.cleanup()))
        self.repin = t270._module("repin_critic_route")
        self.addCleanup(setattr, self.repin, "SHIPPED_ROUTE_ONLY", True)

    def moved_to_terra(self, name: str, policy: dict) -> pathlib.Path:
        source = self.base / f"{name}.json"
        source.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        audit = t270.write_audit(self.base / name / "audit", source, f"audit-275-{name}")
        self.repin.SHIPPED_ROUTE_ONLY = False   # what repin_critic_route.py 2.7.4 and earlier did
        self.repin.repin(audit, critic_model=TERRA, adjudicator_model=None, reason="fixture: an older copy")
        self.repin.SHIPPED_ROUTE_ONLY = True
        self.assertEqual(t270.frozen_critic_route(audit), (TERRA, TERRA, TERRA))
        return audit

    def test_a_completed_review_off_the_lock_never_validates(self) -> None:
        source = self.base / "shipped.json"
        source.write_text(json.dumps(t270.shipped_policy(), indent=2) + "\n", encoding="utf-8")
        done = t270.write_audit(self.base / "done" / "audit", source, "audit-275-done")
        finished = t270.run_driver(done, self.base / "state", "ledger-1", "success")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        review = next(row for row in t270.ledger(done) if row["role"].startswith("critic_") and row["status"] == "completed")
        record_invocation = t270._module("record_invocation")
        locked = self.moved_to_terra("locked", t270.shipped_policy())
        with self.assertRaisesRegex(ValueError, f"critique.critic_route_lock='{SOL}'"):
            record_invocation.validate_record(dict(review, requested_model=TERRA), locked, verify_output_bytes=False)
        # negative control: on an audit frozen before the lock, the lock rule is not what judges the review (the
        # borrowed record may still fail on its artifact path, which belongs to the other fixture audit)
        unlocked_policy = t270.shipped_policy()
        del unlocked_policy["critique"]["critic_route_lock"]
        unlocked = self.moved_to_terra("unlocked", unlocked_policy)
        try:
            record_invocation.validate_record(dict(review, requested_model=TERRA), unlocked, verify_output_bytes=False)
        except ValueError as exc:
            self.assertNotIn("critic_route_lock", str(exc))


class DriftCheckPinsTheLock(unittest.TestCase):
    def test_a_removed_or_changed_lock_is_a_drift(self) -> None:
        for name, edit in (("removed", lambda c: c.pop("critic_route_lock")),
                           ("changed", lambda c: c["critic_route_lock"].update(model=TERRA))):
            with self.subTest(edit=name), tempfile.TemporaryDirectory(prefix="i4r-275-drift-") as directory:
                tree = pathlib.Path(directory) / "i4r-audit"
                shutil.copytree(SKILL, tree, ignore=t270.IGNORED, symlinks=True)
                path = tree / "config/pipeline-policy-v2.json"
                policy = json.loads(path.read_text(encoding="utf-8"))
                edit(policy["critique"])
                path.write_text(json.dumps(policy, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                result = subprocess.run([sys.executable, str(tree / "config/check_policy_drift.py")], text=True,
                                        capture_output=True, timeout=300, env=t270.clean_env())
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(f"critique.critic_route_lock is not {SOL}", result.stdout + result.stderr)


class RepinOnlyToTheShippedRoute(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-275-repin-")
        self.base = pathlib.Path(directory.name)
        self.addCleanup(lambda: (t270.thaw(self.base), directory.cleanup()))

    def audit(self, model: str, name: str) -> pathlib.Path:
        frozen = t270.on_critic_route(t270.shipped_policy(), model)
        if model != SOL:   # an audit frozen on an older route was initialised before 2.7.5: no lock
            frozen["critique"].pop("critic_route_lock", None)
        policy = self.base / f"{name}-policy.json"
        policy.write_text(json.dumps(frozen, indent=2) + "\n", encoding="utf-8")
        return t270.write_audit(self.base / name / "audit", policy, f"audit-275-{name}")

    def test_a_sol_audit_cannot_be_moved_to_terra_and_nothing_is_written(self) -> None:
        audit = self.audit(SOL, "sol")
        snapshot = (audit / "pipeline-policy.snapshot.json").read_bytes()
        for flags in (("--critic-model", TERRA), ("--adjudicator-model", TERRA), ("--critic-model", "gpt-6-sol")):
            result = t270.run_python(str(SCRIPTS / "repin_critic_route.py"), str(audit), *flags, "--reason", "x")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(REFUSAL, result.stderr)
        self.assertEqual((audit / "pipeline-policy.snapshot.json").read_bytes(), snapshot)
        self.assertEqual(list(audit.glob("pipeline-policy.snapshot.superseded-*.json")), [])

    def test_the_library_refuses_too(self) -> None:
        audit = self.audit(SOL, "lib")
        repin = t270._module("repin_critic_route")
        self.assertTrue(repin.SHIPPED_ROUTE_ONLY)
        with self.assertRaisesRegex(repin.RepinError, REFUSAL):
            repin.repin(audit, critic_model=TERRA, adjudicator_model=None, reason="x")

    def test_an_older_route_can_still_be_moved_up_to_sol(self) -> None:
        audit = self.audit(TERRA, "terra")
        refused = t270.run_python(str(SCRIPTS / "repin_critic_route.py"), str(audit), "--critic-model", "gpt-6-sol",
                                  "--reason", "x")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        moved = t270.run_python(str(SCRIPTS / "repin_critic_route.py"), str(audit), "--reason",
                                "fixture: the account can now use the shipped route")
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        self.assertIn(f"re-pinned: critics {TERRA} -> {SOL}, adjudicator {TERRA} -> {SOL}", moved.stdout)
        self.assertEqual(t270.frozen_critic_route(audit), (SOL, SOL, SOL))
        explicit = self.audit(TERRA, "terra-explicit")
        moved = t270.run_python(str(SCRIPTS / "repin_critic_route.py"), str(explicit), "--critic-model", SOL,
                                "--reason", "fixture")
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        self.assertEqual(t270.frozen_critic_route(explicit), (SOL, SOL, SOL))


if __name__ == "__main__":
    unittest.main()
