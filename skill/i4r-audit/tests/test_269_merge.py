"""2.6.9: the merge of the QA release 2.6.8 with the four 2.6.8 fixes, pinned.

Each class pins one decision the merge had to make where the two releases met, so a later edit that
undoes it fails here rather than on a production audit:

* ``LiveProviderRefusalComesFirst`` -- 2.6.8 refuses a rerun while a killed driver's provider is still
  running; the fixes build, reuse and re-freeze the round snapshot. The refusal now runs before the
  snapshot step, so a snapshot that provider is still reading is never touched or handed the
  delete-and-rebuild remedy (behavioural, through the real driver and the fake Codex CLI).
* ``DriverOrder`` -- a worker whose driver was killed with -9 makes no further attempt: an orphaned
  critic stops before the pre-dispatch snapshot check and the attempt claim, before a focused
  repair's directory, contract and packet, and the targeted adjudicator stops the same way; a
  forced-rerun replay whose snapshot fails its integrity check stops with the snapshot remedy, not
  the paid ``I4R_FORCE_RERUN=1`` advice. Behavioural, through the real driver: a wrapper around the
  fake Codex CLI adds per-role transient and sleep knobs, the driver alone is killed, and the tests
  count the orphan's provider calls, budget claims, journal events and helper commands.
* ``DeferredDisclosureRows`` -- 2.6.8's deferred approvals at an unfinished stop against the
  report-not-block rule (``disclosure_policy.deferred_rows``; finalize is driven in
  ``tests/test_268_pii_report_not_block.py``).
* ``ReleaseIdentity`` -- VERSION 2.6.9 over 2.6.8's unchanged entry, the provenance file 2.6.8 stages,
  and integrate's decidable fields.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import check_deposit_values as cdv  # noqa: E402
import disclosure_policy  # noqa: E402
import test_268_snapshot_fix_a as fix_a  # noqa: E402  (its real-driver fixture; no tests are imported)

DRIVER_TEXT = (SCRIPTS / "run_critique.sh").read_text(encoding="utf-8")


class LiveProviderRefusalComesFirst(fix_a.CritiqueDriverCase):
    def test_a_live_provider_of_a_killed_run_is_refused_before_any_snapshot_is_built(self) -> None:
        # A provider left running by a driver killed with -9 (its command line names codex).
        provider = subprocess.Popen(["bash", "-c", "exec -a codex-provider-left-running sleep 120"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(provider.wait)
        self.addCleanup(provider.kill)
        active = self.round / "invocations/killed-run/active_providers"
        active.mkdir(parents=True)
        (active / "stats.pid").write_text(f"{provider.pid}\n")
        rc, log = self.driver("rerun")
        self.assertNotEqual(rc, 0, log)
        self.assertIn("still has a live provider process", log)
        self.assertNotIn("rm -rf", log, "no snapshot remedy for a snapshot a live provider may read")
        snapshots = self.round / "snapshots"
        self.assertFalse(snapshots.exists() and any(snapshots.iterdir()),
                         "the refusal comes before the round snapshot is built, reused or re-frozen")
        self.assertEqual(self.calls(), 0, "no provider call")

    def test_the_refusal_sits_between_the_exit_traps_and_the_snapshot_step(self) -> None:
        refusal = DRIVER_TEXT.index("still has a live provider process")
        self.assertLess(DRIVER_TEXT.index("\ntrap finish_run EXIT\n"), refusal,
                        "the refusal exits through finish_run, which restores the killed run's pointer")
        self.assertLess(refusal, DRIVER_TEXT.index('"$HELPER" snapshot --workspace'))
        # 2.6.8's own pin (tests/test_264_qa_fixes.py): before the probe.
        self.assertLess(refusal, DRIVER_TEXT.index('FINAL_DETAIL="snapshot ready; checking model probe"'))


# The wrapper Codex CLI DriverOrder runs the driver with (written per test; no provider is ever called).
WRAP_CODEX = r'''#!/bin/bash
# Test double around tests/fake_codex.sh (written by tests/test_269_merge.py, DriverOrder): per-role
# knobs that the fake's single FAKE_CODEX_SCENARIO cannot combine with a scenario.
#   WRAP_TRANSIENT_ROLE=<role>  that role's first call fails with a transient error (exit 75)
#   WRAP_SLEEP_ROLE=<role>      that role's first call sleeps WRAP_SLEEP_SECONDS (default 12) first
# Every call of a role, a failed one included, is counted in $FAKE_CODEX_STATE/wrap_calls_<role>.
if [ "${1:-}" = "--version" ] || [ "${1:-}" = "-V" ]; then exec bash "$WRAP_REAL_CODEX" "$@"; fi
prompt=$(mktemp "${TMPDIR:-/tmp}/wrap_codex_prompt.XXXXXX") || exit 2
cat > "$prompt"
if grep -q I4R_RUNTIME_PROBE "$prompt"; then role=model_probe
elif grep -q I4R_TARGETED_ADJUDICATION "$prompt"; then role=targeted_adjudicator
else role=$(sed -n 's/^runtime_role=\([a-z_]*\)$/\1/p' "$prompt" | head -1); fi
mkdir -p "$FAKE_CODEX_STATE"
count_file="$FAKE_CODEX_STATE/wrap_calls_$role"
count=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))
echo "$count" > "$count_file"
if [ "$role" = "${WRAP_TRANSIENT_ROLE:-}" ] && [ "$count" -eq 1 ]; then
  echo '{"type": "error", "message": "stream disconnected; try again"}'
  echo "stream disconnected; try again" >&2
  rm -f "$prompt"; exit 75
fi
if [ "$role" = "${WRAP_SLEEP_ROLE:-}" ] && [ "$count" -eq 1 ]; then sleep "${WRAP_SLEEP_SECONDS:-12}"; fi
bash "$WRAP_REAL_CODEX" "$@" < "$prompt"; rc=$?
rm -f "$prompt"; exit "$rc"
'''

# Imported by every Python process of a DriverOrder run (PYTHONPATH); inert outside the runtime helper.
DRIVER_ORDER_SITECUSTOMIZE = r'''
"""Test-only hooks for tests/test_269_merge.py (DriverOrder); they act only in the runtime helper.

* I4R_TEST_HELPER_LOG: each helper command appends its start time, its parent pid and its name,
  so a test sees what an orphaned worker still ran after its driver was killed.
* I4R_TEST_DRIFT_ON_PREPARE_REPAIR=1: as prepare-repair starts, the round snapshot's `source`
  regains a write bit (0700, content untouched): the synced folder's mode drift in that window.
* I4R_TEST_PREPARE_FLAG and I4R_TEST_PREPARE_SLEEP: the first prepare-repair creates the flag file
  and then sleeps, so a test can kill the driver while prepare-repair itself is running.
* I4R_TEST_TAMPER_FLAG and I4R_TEST_TAMPER_SNAPSHOT: the first verify-complete of an output kept
  under result_cache/conflicts/ (a forced-rerun replay) appends a line to the round snapshot.
"""
import json, os, pathlib, sys, time


def _i4r_test_hooks():
    argv = list(sys.argv)
    if len(argv) < 2 or not argv[0].endswith("test_run_critique_runtime.py"):
        return
    command = argv[1]
    log = os.environ.get("I4R_TEST_HELPER_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": time.time(), "ppid": os.getppid(), "command": command}) + "\n")
    if command == "prepare-repair" and os.environ.get("I4R_TEST_DRIFT_ON_PREPARE_REPAIR") == "1":
        os.chmod(pathlib.Path(argv[argv.index("--snapshot") + 1]) / "source", 0o700)
    pause = os.environ.get("I4R_TEST_PREPARE_FLAG")
    if pause and command == "prepare-repair":
        try:
            os.close(os.open(pause, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            time.sleep(float(os.environ.get("I4R_TEST_PREPARE_SLEEP", "6")))
        except FileExistsError:
            pass
    flag = os.environ.get("I4R_TEST_TAMPER_FLAG")
    if flag and command == "verify-complete" and "--sidecar" in argv \
            and "/conflicts/" in argv[argv.index("--sidecar") + 1]:
        try:
            os.close(os.open(flag, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            return
        snapshot = pathlib.Path(os.environ["I4R_TEST_TAMPER_SNAPSHOT"])
        target = snapshot / "source/REPORT_TO_REVIEW.md"
        os.chmod(snapshot / "source", 0o755)
        os.chmod(target, 0o644)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("changed by a sync conflict\n")
        os.chmod(target, 0o444)
        os.chmod(snapshot / "source", 0o555)


_i4r_test_hooks()
'''


class DriverOrder(fix_a.CritiqueDriverCase):
    """The merge's runtime order, driven through the real driver with the fake Codex CLI.

    A worker whose driver was killed with -9 keeps running in the background; a rerun owns the
    round then. Each test kills the driver alone (never its workers), waits until every process of
    its group has exited, and counts what the orphan did after the kill: provider calls, attempts of
    the round's budget, journal events and runtime-helper commands."""

    # The retry backoff (and the repairable call's sleep) during which the driver is killed: long
    # enough to kill it inside that window on a loaded host; in_window() fails loudly otherwise.
    WINDOW_SECONDS = 12
    CRITICS = ("stats", "data", "completeness", "fairness")

    def setUp(self) -> None:
        super().setUp()
        site = self.work / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(DRIVER_ORDER_SITECUSTOMIZE)
        self.codex = self.work / "wrap_codex.sh"
        self.codex.write_text(WRAP_CODEX)
        self.codex.chmod(0o755)
        self.helper_log = self.work / "helper_commands.jsonl"
        self.site = site

    def env(self, invocation: str, scenario: str, **extra: str) -> dict[str, str]:
        values = dict(AUDIT_DIR=str(self.audit), CODEX=str(self.codex), WRAP_REAL_CODEX=str(fix_a.FAKE_CODEX),
                      FAKE_CODEX_STATE=str(self.state), FAKE_CODEX_SCENARIO=scenario,
                      I4R_INVOCATION_ID=invocation, RETRY_BACKOFF_SECONDS="0",
                      PROBE_LOCK_WAIT_SECONDS="3", PYTHONPATH=str(self.site),
                      I4R_TEST_HELPER_LOG=str(self.helper_log))
        values.update(extra)
        return fix_a.drift.env_without_refreeze(**values)

    def run_driver(self, invocation: str, scenario: str = "success", **extra: str) -> tuple[int, str]:
        log = self.work / f"{invocation}.log"
        with log.open("w") as handle:
            result = subprocess.run(["bash", str(fix_a.DRIVER), "1"], stdout=handle,
                                    stderr=subprocess.STDOUT, env=self.env(invocation, scenario, **extra),
                                    timeout=600, check=False)
        return result.returncode, log.read_text()

    def start_driver(self, invocation: str, scenario: str = "success",
                     **extra: str) -> tuple[subprocess.Popen, pathlib.Path]:
        log = self.work / f"{invocation}.log"
        handle = log.open("w")
        self.addCleanup(handle.close)
        process = subprocess.Popen(["bash", str(fix_a.DRIVER), "1"], stdout=handle,
                                   stderr=subprocess.STDOUT, env=self.env(invocation, scenario, **extra),
                                   start_new_session=True)
        self.addCleanup(self.stop_group, process)
        return process, log

    @staticmethod
    def stop_group(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait()

    def wait_until(self, predicate, what: str, log: pathlib.Path, timeout: float = 240) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}\n{log.read_text()}")
            time.sleep(0.1)

    def kill_driver(self, process: subprocess.Popen) -> tuple[float, int]:
        """kill -9 the driver alone and wait until every process of its group has exited.
        Returns the kill time and the journal's length at the kill."""
        journal_length = len(self.events())
        killed_at = time.time()
        process.send_signal(signal.SIGKILL)
        process.wait()
        deadline = time.monotonic() + 240
        while True:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() > deadline:
                self.fail("the orphaned workers never exited")
            time.sleep(0.2)
        return killed_at, journal_length

    def events(self) -> list[dict]:
        path = self.audit / "run.events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def events_since(self, length: int) -> list[tuple]:
        return [(event["type"], (event.get("payload") or {}).get("role"),
                 (event.get("payload") or {}).get("kind")) for event in self.events()[length:]]

    def reservations_since(self, length: int) -> list[tuple[str, str]]:
        """(role, attempt kind) of each call reserved in the journal since ``length``."""
        return [(event["payload"]["invocation_id"].split(":")[1], event["payload"]["attempt_kind"])
                for event in self.events()[length:] if event["type"] == "invocation_reserved"]

    def orphan_commands(self, killed_at: float, driver_pid: int) -> list[str]:
        """The helper commands started after the kill (the dead driver's own are excluded)."""
        if not self.helper_log.is_file():
            return []
        rows = [json.loads(line) for line in self.helper_log.read_text().splitlines() if line.strip()]
        return [row["command"] for row in rows if row["at"] >= killed_at and row["ppid"] != driver_pid]

    def wrap_calls(self, role: str) -> int:
        path = self.state / f"wrap_calls_{role}"
        return int(path.read_text().split()[0]) if path.is_file() else 0

    def budget(self, name: str) -> int:
        path = self.round / "attempt_budget" / f"{name}.count"
        return int(path.read_text().split()[0]) if path.is_file() else 0

    def in_window(self, opened: float) -> None:
        self.assertLess(time.monotonic(), opened + self.WINDOW_SECONDS - 2,
                        "the host was too slow to kill the driver inside the window; this is not an orphan defect")

    def backoff_started(self, role: str) -> bool:
        return any(event["type"] == "retry_backoff_start"
                   and (event.get("payload") or {}).get("role") == role for event in self.events())

    def test_an_orphaned_critic_worker_stops_before_the_pre_dispatch_check_and_the_claim(self) -> None:
        # critic_stats's first call fails transiently; the driver dies during the retry backoff.
        process, log = self.start_driver("killed", WRAP_TRANSIENT_ROLE="stats",
                                         RETRY_BACKOFF_SECONDS=str(self.WINDOW_SECONDS))
        results = self.round / "invocations/killed/results"
        self.wait_until(lambda: self.backoff_started("critic_stats"), "critic_stats's retry backoff", log)
        opened = time.monotonic()
        self.wait_until(lambda: all((results / f"{role}.complete.json").is_file()
                                    for role in self.CRITICS if role != "stats"), "the other critics", log)
        self.in_window(opened)
        killed_at, journal_length = self.kill_driver(process)
        text = log.read_text()
        self.assertIn(f"[stop ] critic_stats: the driver (pid {process.pid}) is gone; this orphaned worker "
                      "makes no further attempt", text)
        self.assertEqual(self.wrap_calls("stats"), 1, "no provider call after the kill")
        self.assertEqual(self.budget("stats"), 1, "no attempt of the round's budget claimed")
        self.assertEqual(self.orphan_commands(killed_at, process.pid), ["retry-backoff"],
                         "only the backoff's closing record: no pre-dispatch check, claim or reservation")
        self.assertEqual(self.events_since(journal_length), [("retry_backoff_end", "critic_stats", None)])
        # The rerun owns the round: attempt 2 of the budget, the other critics from the cache.
        rc, rerun = self.run_driver("rerun", WRAP_TRANSIENT_ROLE="stats")
        self.assertEqual(rc, 0, rerun)
        self.assertIn("[run  ] critic_stats attempt 2/3", rerun)
        for role in self.CRITICS[1:]:
            self.assertIn(f"[cache] critic_{role}\n", rerun)
        self.assertEqual((self.wrap_calls("stats"), self.budget("stats")), (2, 2))

    def test_an_orphaned_worker_prepares_no_focused_repair(self) -> None:
        # critic_data returns a repairable response; the driver dies during that call. The synced
        # folder re-opens the snapshot's `source` as soon as a prepare-repair starts.
        process, log = self.start_driver("killed", "repairable", WRAP_SLEEP_ROLE="data",
                                         WRAP_SLEEP_SECONDS=str(self.WINDOW_SECONDS),
                                         I4R_TEST_DRIFT_ON_PREPARE_REPAIR="1")
        results = self.round / "invocations/killed/results"
        self.wait_until(lambda: self.wrap_calls("data") == 1, "critic_data's call", log)
        opened = time.monotonic()
        self.wait_until(lambda: all((results / f"{role}.complete.json").is_file()
                                    for role in self.CRITICS if role != "data"), "the other critics", log)
        self.in_window(opened)
        killed_at, journal_length = self.kill_driver(process)
        text = log.read_text()
        self.assertIn(f"[stop ] critic_data: the driver (pid {process.pid}) is gone; this orphaned worker "
                      "makes no repair call", text)
        self.assertNotIn("[fix  ] critic_data", text)
        self.assertEqual(self.wrap_calls("data"), 1, "no repair call")
        self.assertEqual(self.budget("data"), 1, "no attempt claimed for the repair")
        self.assertEqual(sorted(path.name for path in (self.round / "attempt_budget").glob("data.*.repair.used")),
                         [], "the role's one focused repair is not consumed")
        attempts = self.round / "invocations/killed/attempts/data"
        self.assertEqual(sorted(path.name for path in attempts.iterdir()), ["1"],
                         "no repair directory, bound contract, repair packet or receipt")
        commands = self.orphan_commands(killed_at, process.pid)
        for command in ("bind-contract", "prepare-repair", "verify-snapshot"):
            self.assertNotIn(command, commands)
        for command in ("validate-critic", "telemetry"):
            self.assertIn(command, commands, "the paid response is still validated and recorded")
        # The paid attempt is recorded as what it was, and nothing else is journaled: no re-freeze
        # note (the drift planted at prepare-repair never came), no reservation.
        ledger = [json.loads(line) for line in (self.audit / "invocations.jsonl").read_text().splitlines()
                  if line.strip()]
        data = [row for row in ledger if row["role"] == "critic_data"]
        self.assertEqual([(row["status"], row["error_class"], row["attempt"]) for row in data],
                         [("failed", "schema", 1)])
        after = self.events()[journal_length:]
        self.assertEqual({(event.get("payload") or {}).get("invocation_id") for event in after},
                         {data[0]["invocation_id"]}, [event["type"] for event in after])
        self.assertEqual(after[-1]["type"], "invocation_terminal")
        self.assertNotIn("note", [event["type"] for event in after])

    def test_a_driver_killed_while_prepare_repair_runs_leaves_no_repair(self) -> None:
        # The driver dies after the early orphan check passed, while bind-contract/prepare-repair run: the
        # check after prepare-repair must stop the worker before its claim and its paid repair call.
        flag = self.work / "prepare_started"
        process, log = self.start_driver("killed-mid-prepare", "repairable",
                                         I4R_TEST_PREPARE_FLAG=str(flag), I4R_TEST_PREPARE_SLEEP="8")
        self.wait_until(flag.is_file, "prepare-repair to start", log)
        time.sleep(0.5)
        self.kill_driver(process)
        self.wait_until(lambda: "critic_data" in log.read_text() and (
            "makes no repair call" in log.read_text() or "[fix  ] critic_data" in log.read_text()),
            "the orphan's decision", log, timeout=60)
        text = log.read_text()
        self.assertIn(f"[stop ] critic_data: the driver (pid {process.pid}) is gone; this orphaned worker "
                      "makes no repair call", text)
        self.assertNotIn("[fix  ] critic_data", text)
        self.assertEqual(self.wrap_calls("data"), 1, "no repair call")
        self.assertEqual(self.budget("data"), 1, "no attempt claimed for the repair")
        self.assertEqual(sorted(path.name for path in (self.round / "attempt_budget").glob("data.*.repair.used")),
                         [], "the role's one focused repair is not consumed")

    def test_an_orphaned_targeted_adjudicator_makes_no_further_attempt(self) -> None:
        # The critics raise a trigger; the adjudicator's first call fails transiently and the driver
        # dies during its retry backoff.
        process, log = self.start_driver("killed", "adjudicate", WRAP_TRANSIENT_ROLE="targeted_adjudicator",
                                         RETRY_BACKOFF_SECONDS=str(self.WINDOW_SECONDS))
        self.wait_until(lambda: self.backoff_started("critic_synth"), "the adjudicator's retry backoff", log)
        self.in_window(time.monotonic())
        killed_at, journal_length = self.kill_driver(process)
        text = log.read_text()
        self.assertIn(f"[stop ] targeted_adjudicator: the driver (pid {process.pid}) is gone; this orphaned "
                      "worker makes no further attempt", text)
        self.assertEqual(self.wrap_calls("targeted_adjudicator"), 1, "no provider call after the kill")
        self.assertEqual(self.budget("targeted_adjudicator"), 1, "no attempt of the round's budget claimed")
        self.assertEqual(self.orphan_commands(killed_at, process.pid), ["retry-backoff"],
                         "only the backoff's closing record: no pre-dispatch check, claim or reservation")
        self.assertEqual(self.events_since(journal_length), [("retry_backoff_end", "critic_synth", None)])
        cache = self.round / "adjudication_cache"
        self.assertEqual(sorted(path.name for path in cache.rglob("*")) if cache.exists() else [], [],
                         "the orphan published no adjudication")
        # The rerun owns the round: attempt 2, its own output cached, no conflict with an orphan's.
        rc, rerun = self.run_driver("rerun", "adjudicate", WRAP_TRANSIENT_ROLE="targeted_adjudicator")
        self.assertEqual(rc, 0, rerun)
        self.assertIn("[ok   ] targeted_adjudicator attempt 2/3", rerun)
        self.assertEqual((self.wrap_calls("targeted_adjudicator"), self.budget("targeted_adjudicator")), (2, 2))
        self.assertFalse(list(cache.rglob("*.conflict")))
        self.assertEqual(self.status()["outcome"], "complete", self.status())

    def test_a_snapshot_fault_in_the_forced_rerun_replay_gets_the_snapshot_remedy(self) -> None:
        rc, text = self.run_driver("first", "nondeterministic")
        self.assertEqual(rc, 0, text)
        rc, text = self.run_driver("forced", "nondeterministic", I4R_FORCE_RERUN="1", MAX_TRIES="6")
        self.assertEqual(rc, 0, text)
        # Every forced output differed from the first cache and is kept under result_cache/<input>/conflicts/.
        self.assertEqual(sorted(path.name for path in (self.round / "result_cache").glob("*/conflicts/*")),
                         sorted(self.CRITICS))
        snapshot_rel = self.status()["snapshot"]
        calls, budgets = self.calls(), {role: self.budget(role) for role in self.CRITICS}
        journal_length = len(self.events())
        # A plain rerun replays the forced outputs; the round snapshot is changed as the first replay
        # is verified.
        flag = self.work / "tampered"
        rc, text = self.run_driver("replay-tampered", "nondeterministic", I4R_TEST_TAMPER_FLAG=str(flag),
                                   I4R_TEST_TAMPER_SNAPSHOT=str(self.audit / snapshot_rel))
        self.assertTrue(flag.is_file(), "the tamper hook fired")
        self.assertNotEqual(rc, 0, text)
        self.assertEqual(self.calls(), calls, "no provider call")
        self.assertEqual({role: self.budget(role) for role in self.CRITICS}, budgets, "no attempt claimed")
        self.assertEqual(self.reservations_since(journal_length), [("model_probe", "cache_replay")],
                         "no critic call reserved; the probe replays its cache")
        for role in self.CRITICS:
            self.assertIn(f"[stop ] critic_{role} (forced-rerun replay): snapshot integrity check failed "
                          "before dispatch; no provider call was made", text)
        self.assertIn("then rerun this same command without I4R_FORCE_RERUN: the forced run's output is "
                      "replayed", text)
        self.assertNotIn("I4R_FORCE_RERUN=1", text, "the paid advice is only for a replay that cannot be made")
        fix_a.run_printed_remedy(self, text, self.audit, snapshot_rel)
        rc, text = self.run_driver("replay-plain", "nondeterministic")
        self.assertEqual(rc, 0, text)
        for role in self.CRITICS:
            self.assertIn(f"[cache] critic_{role} (the forced rerun's output)", text)
        self.assertEqual(self.calls(), calls, "the forced outputs are replayed for free")
        self.assertEqual({role: self.budget(role) for role in self.CRITICS}, budgets)


class DeferredDisclosureRows(unittest.TestCase):
    @staticmethod
    def row(fid: str, **fields) -> dict:
        base = {"id": fid, "disclosure_flag": True, "status": "confirmed", "coordinator_approved": False,
                "ledger_disposition": {"status": "INCLUDED", "target": None, "reason": None}}
        base.update(fields)
        return base

    def test_only_an_unfinished_stop_listing_a_reported_row_defers_it(self) -> None:
        rows = [(1, self.row("F1")), (2, self.row("F2", status="draft")),
                (3, self.row("F3", coordinator_approved=True)),
                (4, self.row("F4", ledger_disposition={"status": "EXCLUDED", "target": None,
                                                        "reason": "kept out of the comment"}))]
        listed = ["F1", "F2", "F3", "F4"]
        for stop in ("cap_with_unresolved", "aborted"):
            with self.subTest(stop=stop):
                self.assertEqual(disclosure_policy.deferred_rows(rows, stop, listed, True), ["F1", "F2"])
                problems, warnings = disclosure_policy.registry_review(rows, stop, listed, True)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("registry row F4", problems[0])
                self.assertEqual(sum("still awaiting approval" in w for w in warnings), 2, warnings)
        for stop in ("target_complete", "converged", None):
            with self.subTest(stop=stop):
                self.assertEqual(disclosure_policy.deferred_rows(rows, stop, listed, True), [])
                self.assertEqual(len(disclosure_policy.registry_review(rows, stop, listed, True)[0]), 3)
        self.assertEqual(disclosure_policy.deferred_rows(rows, "cap_with_unresolved", ["F2"], True), ["F2"])
        self.assertEqual(disclosure_policy.deferred_rows(rows, "cap_with_unresolved", listed, False), [])
        # The default arguments keep 2.6.8's rule exactly.
        self.assertEqual(len(disclosure_policy.registry_review(rows)[0]), 3)


class ReleaseIdentity(unittest.TestCase):
    def test_version_2_6_9_sits_above_the_unchanged_2_6_8_entry(self) -> None:
        # 2.6.10 (the Codex CLI resolver), 2.7.0 (Sonnet 5.5 for new audits), 2.7.1 (the probe bound
        # of new audits), 2.7.2 (Phase-1 timeout accounting and the timeout of new audits), 2.7.3 (Claude
        # effort xhigh for new audits), 2.7.4 (among others, the journaled environment) 2.7.5 (the critic route
        # lock), 2.7.6 and 2.7.7 (unblocking fixes) sit above 2.6.9 and change none of this.
        self.assertEqual((SKILL / "VERSION").read_text().strip(), "2.7.7")
        changelog = (SKILL / "CHANGELOG.md").read_text(encoding="utf-8")
        headings = re.findall(r"^## (\S+) — ", changelog, flags=re.MULTILINE)
        self.assertEqual(headings[:12], ["2.7.7", "2.7.6", "2.7.5", "2.7.4", "2.7.3", "2.7.2", "2.7.1", "2.7.0",
                                         "2.6.10", "2.6.9", "2.6.8", "2.6.7"])
        self.assertNotRegex(changelog, r"(?m)^(<<<<<<<|=======|>>>>>>>)( |$)")
        top = changelog.split("\n## 2.6.9 — ", 1)[1].split("\n## 2.6.8 — ", 1)[0]
        self.assertIn("merges", top.split("\n### ", 1)[0])
        self.assertIn("Upgrading in-flight audits to 2.6.9", top)

    def test_the_staged_id_map_history_is_provenance(self) -> None:
        self.assertTrue(cdv.provenance_file("critique/id_map_history.jsonl"))
        self.assertIn('copy_input "$A/work/id_map_history.jsonl" "$R/critique/id_map_history.jsonl"',
                      (SCRIPTS / "stage_release.sh").read_text(encoding="utf-8"))
        self.assertFalse(cdv.provenance_file("critique/round_1/CONTESTED.json"),
                         "the coordinator writes the contests: they stay matched against deposit values")

    def test_integrate_accepts_the_disclosure_decision_keys(self) -> None:
        source = (SCRIPTS / "integrate_round.py").read_text(encoding="utf-8")
        decidable = source[source.index("    decidable = {"):]
        decidable = decidable[:decidable.index("}") + 1]
        self.assertIn("disclosure_policy.BASIS_KEY", decidable)
        self.assertIn('"disclosure_flag_reason"', decidable)
        self.assertEqual(disclosure_policy.BASIS_KEY, "disclosure_exclusion_basis")


if __name__ == "__main__":
    unittest.main()
