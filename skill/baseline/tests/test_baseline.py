"""Baseline tests. A fake CLI (tests/fake_claude.py) stands in for Claude: no test makes a model call."""
from __future__ import annotations

import hashlib
import json
import os
import re
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
SKILL = TESTS.parent
SCRIPTS = SKILL / "scripts"
FAKE = TESTS / "fake_claude.py"
LIVE = SKILL.parent / "i4r-audit-unified" / "i4r-audit"
sys.path.insert(0, str(SCRIPTS))

import baseline_common as bc  # noqa: E402
import select_subset  # noqa: E402
import validate_outputs  # noqa: E402

A1, A2 = "20261001-audit-aaaa0001", "20261001-audit-aaaa0002"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_tree(root: pathlib.Path, audits=((A1, True),)) -> pathlib.Path:
    """A minimal I4R tree with published audits whose Phase-1 snapshots are real and hash-consistent."""
    tree = root / "tree"
    (tree / "data").mkdir(parents=True)
    for n, (audit_id, code) in enumerate(audits):
        journal, paper = "Test Journal", f"Paper {n}"
        audit = tree / "runs" / journal / paper / "audit"
        raw = {"paper/paper.pdf": b"%PDF-1.4 fake", "paper/paper.txt": b"The mean of x is 2.0.\n",
               "pkg/data.csv": b"x,y\n1,2\n3,4\n"}
        if code:
            raw["pkg/analysis.R"] = b"mean(read.csv('data.csv')$x)\n"
        evidence = {"pdf_text.txt": b"text", "file_inventory.json": b"{}", "privacy_scan.json": b"{}",
                    "environment_fingerprint.json": b'{"python_version": "3.9.6"}'}
        manifest = {"schema_version": "i4r-phase1-snapshot-1",
                    "source_files": [{"path": p, "sha256": sha(b), "bytes": len(b)} for p, b in raw.items()],
                    "common_evidence": {"artifacts": [{"path": p, "sha256": sha(b), "bytes": len(b)}
                                                      for p, b in evidence.items()]},
                    "context": []}
        snap_sha = sha(json.dumps(manifest, sort_keys=True).encode() + audit_id.encode())
        manifest["snapshot_sha256"] = snap_sha
        snap = audit / "phase1" / "snapshots" / snap_sha
        for p, b in raw.items():
            (snap / "raw" / p).parent.mkdir(parents=True, exist_ok=True)
            (snap / "raw" / p).write_bytes(b)
        (snap / "evidence").mkdir(parents=True)
        for p, b in evidence.items():
            (snap / "evidence" / p).write_bytes(b)
        (snap / "snapshot_manifest.json").write_text(json.dumps(manifest))
        (audit / "phase1" / "quorum.json").write_text(json.dumps({"source_snapshot_sha256": snap_sha}))
        run = {"audit_id": audit_id, "protocol_version": "2.7.4", "ended_at": "2026-10-02T00:00:00Z",
               "invocations": [
                   {"role": "auditor_reproduction", "status": "completed", "observed_model": "claude-sonnet-5-5",
                    "reasoning_effort": "xhigh", "model_family": "claude", "usage": {"total_tokens": 100},
                    "provider_cost": {"amount": 3.0}, "inference_start_status": "started"},
                   {"role": "coordinator", "status": "completed", "observed_model": "claude-sonnet-5-5",
                    "reasoning_effort": "xhigh", "model_family": "claude", "usage": {"total_tokens": 100},
                    "provider_cost": {"amount": 4.0}, "inference_start_status": "started"},
                   {"role": "model_probe", "status": "completed", "model_family": "gpt",
                    "inference_start_status": "not_started", "attempt_kind": "cache_replay"},
                   {"role": "critic_stats", "status": "completed", "requested_model": "gpt-6.1-sol",
                    "model_family": "gpt", "inference_start_status": "started"}],
               "rounds": [{}, {}], "durations": {"provider_active_interval_union_seconds": 7200}}
        (audit / "run.json").write_text(json.dumps(run))
        meta = tree / "metadata" / journal / paper
        meta.mkdir(parents=True)
        (meta / "run.json").write_text(json.dumps(run))
        (meta / "comment.json").write_text(json.dumps({
            "fixture": str(root), "package": {"code_present": code, "reproduces_headline": "exact"},
            "findings": [{"impact": "none", "changes_conclusion": False, "disclosure_flag": True}]}))
        comments = tree / "comments" / journal / paper
        comments.mkdir(parents=True)
        (comments / f"paper{n}_comment.pdf").write_bytes(f"%PDF published comment {n} {root}".encode())
    return tree


class Env(unittest.TestCase):
    audits = ((A1, True),)

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="baseline-test-")
        self.addCleanup(self._cleanup, tmp)
        self.root = pathlib.Path(tmp.name).resolve()
        self.tree = make_tree(self.root, self.audits)
        self.home = self.root / "home"
        (self.home / "Downloads").mkdir(parents=True)
        (self.home / ".claude" / "projects" / "-old-session").mkdir(parents=True)
        self.work = self.root / "work"
        self.log = self.root / "fake.log"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("I4R_", "FAKE_"))}
        (self.root / "tmp").mkdir()
        self.env.update(I4R_TREE=str(self.tree), I4R_BASELINE_WORK=str(self.work), FAKE_LOG=str(self.log),
                        TMPDIR=str(self.root / "tmp"), I4R_CLAUDE_TMP_ROOT=str(self.root / "claude-tmp"),
                        I4R_SCAN_ROOTS=str(self.home),
                        PYTHONDONTWRITEBYTECODE="1", ANTHROPIC_MODEL="claude-opus-9",
                        ANTHROPIC_DEFAULT_SONNET_MODEL="claude-sonnet-4", HOME=str(self.home))
        self.cli = str(FAKE)
        FAKE.chmod(0o755)

    @staticmethod
    def _cleanup(tmp) -> None:
        for path in pathlib.Path(tmp.name).rglob("*"):
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() else 0o644)
        tmp.cleanup()

    def script(self, name: str, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", str(SCRIPTS / name), *args], cwd=self.tree, text=True,
                              capture_output=True, env={**self.env, **env}, timeout=180)

    def subset(self) -> None:
        ids = self.root / "ids.txt"
        ids.write_text("\n".join(a for a, _ in self.audits) + "\n")
        result = self.script("select_subset.py", "--audit-ids", str(ids), "--write")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def dest(self, n: int = 0) -> pathlib.Path:
        return self.tree / "baseline_comments" / "Test Journal" / f"Paper {n}"

    def record(self, n: int = 0) -> dict:
        path = self.dest(n) / "run.json"
        return json.loads(path.read_text()) if path.is_file() else {}

    def run_paper(self, *extra: str, mode: str = "good", paper: str = "1", **env) -> tuple[subprocess.CompletedProcess, dict]:
        result = self.script("run_baseline.py", "run", paper, "--claude-bin", self.cli, "--no-render", *extra,
                             FAKE_MODE=mode, **env)
        return result, self.record(int(paper) - 1)

    def calls(self) -> list[dict]:
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def runlog(self) -> list[dict]:
        path = self.tree / "baseline_comments" / "RUNLOG.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


class RunTests(Env):
    def test_a_good_run_is_one_confined_call_with_the_auditor_contract(self) -> None:
        self.subset()
        result, record = self.run_paper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(record["outcome"]["status"], "completed", record.get("validation"))
        t = record["totals"]
        self.assertEqual((t["model_calls"], t["findings"], t["cost_usd"], t["single_model"]), (1, 1, 12.5, True))
        self.assertEqual(record["attempts"][0]["usage"]["total_tokens"], 1000 + 50000 + 2000000 + 90000)
        self.assertEqual(record["attempts"][0]["usage"]["thinking_tokens"], 7000)
        self.assertEqual(record["attempts"][0]["context_window"], 1000000)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        call = calls[0]
        run_root = pathlib.Path(record["work_folder"])
        out = run_root / "out_1"
        expected = [
            "--print", "--output-format", "json", "--model", "claude-sonnet-5-5", "--effort", "xhigh", "--safe-mode",
            "--append-system-prompt", bc.BASH_STEER, "--no-chrome", "--disable-slash-commands", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--settings", str(run_root / "attempt_1" / "claude_settings.json"),
            "--add-dir", str(run_root / "input"), str(run_root / "packet"), "--no-session-persistence",
            "--session-id", record["attempts"][0]["session_id"], "--max-budget-usd", "30",
            "--permission-mode", "dontAsk", "--tools", "Read,Glob,Grep,Write,Edit,Bash", "--allowedTools",
            f"Read(/{run_root / 'input'}/**)", f"Read(/{run_root / 'packet'}/**)", f"Read(/{out}/**)",
            f"Edit(/{out}/**)", *bc.BASH_ALLOWLIST]
        self.assertEqual(call["argv"], expected)
        self.assertEqual(call["cwd"], str(out))
        self.assertEqual(call["env_cap"], "128000")
        self.assertIsNone(call["env_model"])
        self.assertIsNone(call["env_sonnet"])
        self.assertEqual(call["packet_writable"], [False, False], "input and packet are read-only during the call")
        self.assertIn("raw/pkg/", call["prompt"])
        self.assertNotIn("{INPUT}", call["prompt"])
        settings = json.loads((run_root / "attempt_1" / "claude_settings.json").read_text())
        sb = settings["sandbox"]
        self.assertEqual((sb["enabled"], sb["failIfUnavailable"], sb["autoAllowBashIfSandboxed"],
                          sb["allowUnsandboxedCommands"]), (True, True, False, False))
        self.assertEqual(sb["network"], {"allowedDomains": [], "deniedDomains": ["*"]})
        fs = sb["filesystem"]
        self.assertEqual(fs["allowWrite"], [str(out)])
        self.assertIn(str(self.tree), fs["denyRead"])
        self.assertIn(str(self.home / "Downloads"), fs["denyRead"])
        self.assertIn(str(self.home / ".claude" / "projects" / "-old-session"), fs["denyRead"])
        self.assertNotIn(str(self.home / ".claude" / "projects"), fs["denyRead"], "its own saved tool results stay readable")
        self.assertTrue({str(run_root / "input"), str(run_root / "packet")} <= set(fs["denyWrite"]))
        self.assertNotIn("Read", settings["permissions"]["allow"], "no bare file tool")
        self.assertIn(f"Read(/{self.tree}/**)", settings["permissions"]["deny"])
        self.assertIn(f"Read(/{run_root / 'input'}/**)", settings["permissions"]["allow"])
        dest = self.dest()
        for name in ("REPORT_final.md", "front_matter.md", "captions.txt", "findings.json", "evidence/check.py",
                     "records/prompt.txt", "records/claude_stdout.json", "records/validation.json"):
            self.assertTrue((dest / name).is_file(), name)
        self.assertTrue(os.access(run_root / "input" / "raw" / "pkg" / "data.csv", os.W_OK), "thawed after the call")
        self.assertEqual(record["environment"]["auth"], "cli_login")
        self.assertEqual([e["status"] for e in self.runlog()], ["completed"])

    def test_a_paper_with_a_run_is_skipped_and_force_keeps_the_old_output(self) -> None:
        self.subset()
        self.run_paper()
        again, _ = self.run_paper()
        self.assertIn("skip", again.stdout)
        self.assertEqual(len(self.calls()), 1)
        forced, record = self.run_paper("--force")
        self.assertEqual(record["outcome"]["status"], "completed", forced.stdout + forced.stderr)
        superseded = list((self.tree / "baseline_comments" / ".superseded").iterdir())
        self.assertEqual(len(superseded), 1)
        self.assertTrue((superseded[0] / "REPORT_final.md").is_file())
        self.assertEqual(record["previous_run"]["status"], "completed")

    def test_a_failed_paper_is_not_rerun_silently(self) -> None:
        self.subset()
        self.run_paper(mode="error")
        self.script("run_baseline.py", "batch", "--claude-bin", self.cli, "--no-render", FAKE_MODE="good")
        self.assertEqual(len(self.calls()), 1)
        self.script("run_baseline.py", "batch", "--claude-bin", self.cli, "--no-render", "--rerun-failed",
                    FAKE_MODE="good")
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.record()["outcome"]["status"], "completed")
        self.assertEqual([e["status"] for e in self.runlog()], ["failed", "completed"])

    def test_invalid_outputs_are_kept_and_never_repaired(self) -> None:
        self.subset()
        _, record = self.run_paper(mode="invalid")
        self.assertEqual(record["outcome"]["status"], "completed_invalid")
        self.assertEqual(record["totals"]["model_calls"], 1)

    def test_an_error_envelope_fails_without_a_retry(self) -> None:
        self.subset()
        result, record = self.run_paper(mode="error")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((record["outcome"]["status"], record["outcome"]["error_class"]), ("failed", "authentication"))
        self.assertEqual(record["totals"]["model_calls"], 1)

    def test_retries_happen_only_when_no_inference_started(self) -> None:
        self.subset()
        _, record = self.run_paper(mode="transient")
        self.assertEqual(record["totals"]["model_calls"], 2)
        _, record = self.run_paper("--force", mode="transient_after_spend")
        self.assertEqual(record["totals"]["model_calls"], 1, "money was spent, so no second call")
        self.assertEqual(record["totals"]["cost_usd"], 25.0)
        _, record = self.run_paper("--force", "--retries", "0", mode="transient")
        self.assertEqual(record["totals"]["model_calls"], 1)

    def test_no_retry_once_the_agent_has_written_a_file(self) -> None:
        self.subset()
        _, record = self.run_paper(mode="transient_wrote")
        self.assertEqual(record["totals"]["model_calls"], 1)
        self.assertTrue(record["attempts"][0]["wrote_files"])

    def test_any_other_served_model_is_a_condition_mismatch(self) -> None:
        self.subset()
        _, record = self.run_paper(mode="helper")
        self.assertEqual(record["outcome"]["status"], "failed")
        self.assertEqual(record["outcome"]["error_class"], "condition_mismatch")
        self.assertFalse(record["totals"]["single_model"])
        self.assertIn("claude-haiku-4-5", record["totals"]["observed_models"])
        self.assertTrue(any("also served" in p for p in record["outcome"]["problems"]))

    def test_a_timeout_is_recorded(self) -> None:
        self.subset()
        _, record = self.run_paper("--timeout-seconds", "2", mode="slow")
        self.assertEqual(record["outcome"]["status"], "timeout")

    def test_wrong_models_and_windows_fail_and_stop_a_batch(self) -> None:
        self.subset()
        for mode, klass in (("two_models", "model"), ("wrong_model", "model"), ("small_window", "condition_mismatch")):
            with self.subTest(mode=mode):
                _, record = self.run_paper("--force", mode=mode)
                self.assertEqual((record["outcome"]["status"], record["outcome"]["error_class"]), ("failed", klass))

    def test_evidence_and_comments_the_hygiene_check_refuses_stay_out_of_the_tree(self) -> None:
        self.subset()
        _, record = self.run_paper(mode="pii")
        self.assertFalse((self.dest() / "evidence" / "rows.csv").exists())
        self.assertIn("evidence/rows.csv", record["outputs"]["withheld_from_tree"])
        self.assertEqual(record["outcome"]["status"], "completed_invalid")
        _, record = self.run_paper("--force", mode="report_pii")
        self.assertFalse((self.dest() / "REPORT_final.md").exists())
        self.assertIn("REPORT_final.md", record["outputs"]["withheld_from_tree"])
        _, record = self.run_paper("--force", mode="binary_pii")
        for name in ("evidence/rows.csv.gz", "evidence/rows.rds"):
            self.assertIn(name, record["outputs"]["withheld_from_tree"])
            self.assertFalse((self.dest() / name).exists())
        self.assertEqual(record["outcome"]["status"], "completed_invalid")

    def test_a_symlink_is_never_followed_into_the_tree(self) -> None:
        self.subset()
        secret = self.root / "secret.txt"
        secret.write_text("SECRET")
        _, record = self.run_paper(mode="symlink", FAKE_LINK_TARGET=str(secret))
        self.assertIn("evidence/notes.txt", record["outputs"]["withheld_from_tree"])
        self.assertFalse((self.dest() / "evidence" / "notes.txt").exists())
        self.assertFalse(record["validation"]["ok"])

    def test_non_utf8_output_is_a_validation_error_not_a_crash(self) -> None:
        self.subset()
        result, record = self.run_paper(mode="non_utf8")
        self.assertEqual(record["outcome"]["status"], "completed_invalid", result.stdout + result.stderr)
        self.assertTrue(any("not UTF-8" in e for e in record["validation"]["errors"]))

    def test_a_cli_that_serves_a_small_window_is_refused_up_front_and_older_ones_run(self) -> None:
        self.subset()
        result = self.script("run_baseline.py", "run", "1", "--claude-bin", self.cli, FAKE_VERSION="2.1.281 (Claude Code)")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("200,000-token window", result.stderr)
        result, _ = self.run_paper(FAKE_VERSION="2.1.206 (Claude Code)")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_lock_refuses_a_live_holder_and_replaces_a_dead_one(self) -> None:
        self.subset()
        lock = self.tree / "baseline_comments" / ".batch.lock"
        import socket
        lock.write_text(f"pid {os.getpid()} on {socket.gethostname()} since now\n")
        result, _ = self.run_paper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("one at a time", result.stderr)
        lock.write_text(f"pid 999999 on {socket.gethostname()} since now\n")
        result, record = self.run_paper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(lock.exists(), "released")

    def test_sigterm_kills_the_call_records_it_and_releases_the_lock(self) -> None:
        self.subset()
        self.run_paper()
        proc = subprocess.Popen([sys.executable, "-B", str(SCRIPTS / "run_baseline.py"), "run", "1", "--force",
                                 "--claude-bin", self.cli, "--no-render"], cwd=self.tree, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**self.env, "FAKE_MODE": "slow"})
        for _ in range(100):
            if len(self.calls()) == 2:
                break
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=30)
        child = self.calls()[-1]["pid"]
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        self.assertFalse((self.tree / "baseline_comments" / ".batch.lock").exists())
        self.assertEqual(self.runlog()[-1]["status"], "interrupted")
        self.assertEqual(self.record()["outcome"]["status"], "completed", "the earlier run's record is untouched")
        self.assertTrue((self.dest() / "REPORT_final.md").is_file())

    def test_a_render_failure_never_loses_the_outcome(self) -> None:
        self.subset()
        code = (
            "import sys, types, argparse; sys.path.insert(0, sys.argv[1]); import run_baseline as rb, render_comment;"
            "render_comment.render = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom'));"
            "args = argparse.Namespace(claude_bin=sys.argv[2], model='sonnet-5-5', effort='xhigh', max_budget_usd='30',"
            " timeout_seconds=60, retries=1, force=False, rerun_failed=False, no_render=False, no_scan=False);"
            "import baseline_common as bc; tree = bc.find_tree(); r = rb.run_one(tree, rb.pick(rb.load_subset(tree), '1'), args);"
            "print(r['outcome']['status'], r['render'])")
        result = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPTS), self.cli], cwd=self.tree, text=True,
                                capture_output=True, env={**self.env, "FAKE_MODE": "good"}, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("completed", result.stdout)
        self.assertIn("boom", result.stdout)
        self.assertEqual(self.record()["outcome"]["status"], "completed")

    def test_preflight_checks_inputs_and_scans_for_copies_without_a_model_call(self) -> None:
        self.subset()
        result = self.script("run_baseline.py", "preflight", "--claude-bin", self.cli)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("preflight OK", result.stdout)
        self.assertEqual(self.calls(), [])
        stash = self.home / "stuff"
        stash.mkdir()
        (stash / "copy.pdf").write_bytes(f"%PDF published comment 0 {self.root}".encode())
        result = self.script("run_baseline.py", "preflight", "--claude-bin", self.cli)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("readable copy of a full-pipeline output", result.stdout)
        (stash / "copy.pdf").unlink()
        snap = next((self.tree / "runs").rglob("evidence/pdf_text.txt"))
        snap.write_bytes(b"tampered")
        result = self.script("run_baseline.py", "preflight", "--claude-bin", self.cli, "--no-scan")
        self.assertIn("does not match its manifest", result.stdout)
        result = self.script("run_baseline.py", "preflight", "--claude-bin", self.cli, "--no-scan",
                             FAKE_VERSION="2.1.281 (Claude Code)")
        self.assertIn("200,000-token window", result.stdout)

    def test_probe_checks_the_sandbox_and_the_cap_in_the_requests(self) -> None:
        result = self.script("run_baseline.py", "probe", "--claude-bin", self.cli)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for check in ("read_input", "read_saved_output", "r", "python", "denied_read_refused", "bash_denied_refused",
                      "context_window_1M", "request_max_tokens_128000", "cache_writes_1h"):
            self.assertIn(f"ok   {check}\n", result.stdout)
        self.assertNotIn("FAIL", result.stdout)
        call = self.calls()[-1]
        self.assertEqual(call["env_cap"], "128000")
        self.assertIn("for i in range(3000): print(f'row {i:05d} ' + 'y' * 20)", call["prompt"],
                      "many short lines, each under the Read tool's limit")
        self.assertIn("tools offered in the requests: Bash, Read, advisor", result.stdout)
        for mode, check in (("probe_no_cap", "request_max_tokens_128000"), ("probe_mixed_cap", "request_max_tokens_128000"),
                            ("probe_small_window", "context_window_1M"), ("probe_denied_readable", "denied_read_refused"),
                            ("probe_bash_leak", "bash_denied_refused"), ("probe_5m_cache", "cache_writes_1h")):
            result = self.script("run_baseline.py", "probe", "--claude-bin", self.cli, FAKE_MODE=mode)
            self.assertNotEqual(result.returncode, 0, mode)
            self.assertIn(f"FAIL {check}", result.stdout, mode + result.stderr)
            self.assertEqual(result.stdout.count("FAIL"), 1, mode + result.stdout)
        result = self.script("run_baseline.py", "probe", "--claude-bin", self.cli, FAKE_MODE="probe_no_bodies")
        self.assertEqual(result.returncode, 0, "no request log is unverified, not a failure: " + result.stdout + result.stderr)
        self.assertIn("n/a  request_max_tokens_128000", result.stdout)
        self.assertFalse(any((self.tree / "baseline_comments").glob(".batch.lock")), "the probe releases the lock")

    def test_the_call_gets_the_full_runs_cache_lifetime_and_none_of_the_session_links(self) -> None:
        self.subset()
        _, record = self.run_paper(CLAUDE_CODE_MESSAGING_SOCKET="/tmp/sock", CLAUDE_CODE_MESSAGING_TOKEN="tok",
                                   CLAUDE_CODE_USER_EMAIL="someone@example.org", FORCE_PROMPT_CACHING_5M="1",
                                   CLAUDE_CODE_ENTRYPOINT="claude-desktop")
        call = self.calls()[-1]
        self.assertEqual(call["env_ttl"], "1h")
        self.assertEqual(call["env_linked"], [], "session links and cache overrides never reach the call")
        self.assertEqual(record["condition"]["prompt_cache_ttl"], "1h")
        self.assertEqual(record["environment"]["entrypoint"], "claude-desktop")
        self.assertIn("CLAUDE_CODE_MESSAGING_TOKEN", record["environment"]["stripped_env_names"])
        self.assertEqual(record["attempts"][0]["cache_writes"], {"5m": 0, "1h": 90000})
        self.assertEqual((record["totals"]["cache_writes_5m"], record["totals"]["cache_writes_1h"]), (0, 90000))
        self.assertEqual(record["attempts"][0]["warnings"], [])
        self.assertEqual(self.runlog()[-1]["status"], "completed")

    def test_five_minute_cache_writes_are_flagged_not_failed(self) -> None:
        self.subset()
        _, record = self.run_paper(mode="cache_5m")
        self.assertEqual(record["outcome"]["status"], "completed")
        self.assertEqual(record["attempts"][0]["cache_writes"], {"5m": 90000, "1h": 0})
        self.assertTrue(any("5 minutes" in w for w in record["attempts"][0]["warnings"]))

    def test_the_record_ties_the_run_to_its_code_and_hides_the_home_folder(self) -> None:
        self.subset()
        (self.home / "notes").mkdir()
        (self.home / "notes" / "a.txt").write_text("readable, searched\n")
        (self.home / "Downloads" / "b.txt").write_text("denied, not searched\n")
        _, record = self.run_paper(I4R_BASELINE_WORK=str(self.home / "bw"))
        self.assertTrue(record["work_folder"].startswith("~/bw/"), record["work_folder"])
        self.assertNotIn(str(self.home), json.dumps(record))
        code = record["code_sha256"]
        for rel in ("scripts/run_baseline.py", "scripts/render_comment.py", "config/baseline.json",
                    "prompts/baseline_prompt.md", "VERSION"):
            self.assertEqual(code[rel], sha((SKILL / rel).read_bytes()), rel)
        scan = record["contamination_scan"]
        self.assertEqual(scan["hits"], 0)
        self.assertGreaterEqual(scan["targets"], 2)
        self.assertEqual(scan["files_seen"], 1, "the readable file is searched, the denied folder is not")
        self.assertEqual(scan["roots"], ["~"], "the home folder is masked")

    def test_render_redraws_a_finished_run_and_keeps_the_earlier_record(self) -> None:
        self.subset()
        self.run_paper()
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import run_baseline as rb, render_comment, argparse;"
            "render_comment.render = lambda folder, tree: ((folder / 'comment.pdf').write_bytes(b'%PDF new'),"
            " {'renderer': 'stub', 'ok': True, 'files': ['comment.pdf'], 'pages': {'body_pages': 9}})[1];"
            "tree = rb.bc.find_tree(); print(rb.rerender(tree, argparse.Namespace(paper='1')))")
        for _ in range(2):
            result = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPTS)], cwd=self.tree, text=True,
                                    capture_output=True, env=self.env, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
        record = self.record()
        self.assertEqual(record["render"]["renderer"], "stub")
        self.assertEqual(record["render"]["sha256"]["comment.pdf"], sha(b"%PDF new"))
        self.assertEqual(len(record["render_history"]), 1, "the second redraw keeps the first's record")
        self.assertEqual(record["outcome"]["status"], "completed")
        self.assertEqual(len(self.runlog()), 1, "a redraw is not a run")
        private = json.loads((pathlib.Path(record["work_folder"].replace("~", str(self.home), 1)) / "run.json").read_text())
        self.assertEqual(private["render"]["renderer"], "stub")

    def test_run_refuses_when_a_readable_copy_exists(self) -> None:
        self.subset()
        stash = self.home / "elsewhere"
        stash.mkdir()
        (stash / "copy.pdf").write_bytes(f"%PDF published comment 0 {self.root}".encode())
        result, _ = self.run_paper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("readable copy of a full-pipeline output", result.stderr)
        self.assertEqual(self.calls(), [], "refused before any paid call")

    def test_a_second_stop_during_recording_cannot_lose_the_record(self) -> None:
        self.subset()
        code = (
            "import sys, os, signal, argparse; sys.path.insert(0, sys.argv[1]); import run_baseline as rb;"
            "signal.signal(signal.SIGTERM, rb._on_signal);"
            "real = rb.bc.thaw_tree\n"
            "def thaw(p):\n    os.kill(os.getpid(), signal.SIGTERM); real(p)\n"
            "rb.bc.thaw_tree = thaw\n"
            "args = argparse.Namespace(claude_bin=sys.argv[2], model='sonnet-5-5', effort='xhigh', max_budget_usd='30',"
            " timeout_seconds=60, retries=1, force=False, rerun_failed=False, no_render=True, no_scan=True)\n"
            "tree = rb.bc.find_tree()\n"
            "r = rb.run_one(tree, rb.pick(rb.load_subset(tree), '1'), args); print('RESULT', r['outcome']['status'])")
        result = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPTS), self.cli], cwd=self.tree, text=True,
                                capture_output=True, env={**self.env, "FAKE_MODE": "good"}, timeout=120)
        self.assertIn("RESULT completed", result.stdout, result.stderr)
        self.assertEqual([e["status"] for e in self.runlog()], ["completed"])

    def test_a_work_folder_inside_a_protected_root_is_refused(self) -> None:
        self.subset()
        result, _ = self.run_paper(I4R_BASELINE_WORK=str(self.home / "Downloads" / "work"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is inside", result.stdout + result.stderr)

    def test_summary_counts_every_run_and_only_started_pipeline_calls(self) -> None:
        self.subset()
        self.run_paper("--timeout-seconds", "2", mode="slow")
        self.run_paper("--force", "--timeout-seconds", "2")
        self.run_paper("--force", mode="good")   # another condition (the default timeout): not counted with these
        result = self.script("summarize_baseline.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        s = json.loads(result.stdout)
        self.assertEqual(s["baseline_runs_total"], 1, "only the current run's condition is counted")
        self.run_paper("--force", "--timeout-seconds", "2")
        s = json.loads(self.script("summarize_baseline.py").stdout)
        self.assertEqual(s["baseline_runs_total"], 3)
        allp = s["all_papers_run"]
        self.assertEqual(allp["pipeline"]["model_calls_median"], 3, "the cache-replay row is not a call")
        self.assertEqual(allp["baseline"]["model_calls_median"], 3, "the timed-out run's call counts")
        self.assertEqual(allp["pipeline"]["claude_cost_usd_median"], 7.0)
        self.assertIsNone(allp["baseline"]["claude_cost_usd_median"], "the timed-out call's cost is unknown, not zero")
        self.assertEqual(allp["baseline"]["papers_with_unknown_cost"], 1)
        self.assertEqual(allp["pipeline"]["papers_with_unknown_other_model_cost"], 1, "the GPT critic's cost is unknown")
        self.assertEqual(allp["pipeline"]["claude_calls_median"], 2)
        self.assertIsNone(allp["baseline"]["claude_cost_usd_1h_cache_median"], "a timed-out run's cache writes are unknown")
        self.assertEqual(allp["pipeline"]["page_cap_unknown"], 1, "no build manifest: unknown, not 'within the cap'")
        log = self.tree / "baseline_comments" / "RUNLOG.jsonl"
        lines = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertTrue(all(l["condition"]["prompt_cache_ttl"] == "1h" and l["condition"]["prompt_template_sha256"]
                            for l in lines))
        lines[0]["condition"]["prompt_template_sha256"] = "an-earlier-prompt"
        log.write_text("".join(json.dumps(l) + "\n" for l in lines))
        s = json.loads(self.script("summarize_baseline.py").stdout)
        self.assertEqual(s["baseline_runs_total"], 2, "a run under another prompt is another condition")
        self.run_paper("--force", "--timeout-seconds", "4", mode="cache_5m")   # a condition of its own: one run
        self.script("summarize_baseline.py", "--json", str(self.root / "s.json"))
        row = json.loads((self.root / "s.json").read_text())["rows"][0]["baseline"]
        self.assertEqual(row["cache_writes"], {"5m": 90000, "1h": 0}, "the run log carries each run's cache writes")
        self.assertAlmostEqual(row["claude_cost_usd_1h_cache"], 12.5 + 90000 * (4.0 - 2.5) / 1e6, places=4)
        self.assertEqual((row["claude_calls"], row["other_model_calls"]), (1, 0))
        meta = self.tree / "metadata" / "Test Journal" / "Paper 0" / "run.json"
        run = json.loads(meta.read_text())
        run["audit_id"] = "20261020-audit-ffff9999"
        meta.write_text(json.dumps(run))
        s = json.loads(self.script("summarize_baseline.py").stdout)
        self.assertEqual(s["papers_run"], 0)
        self.assertEqual(len(s["skipped"]), 1)


class RoundTwoTests(Env):
    def test_nohup_keeps_a_batch_alive_through_a_terminal_hangup(self) -> None:
        self.subset()
        proc = subprocess.Popen([sys.executable, "-B", str(SCRIPTS / "run_baseline.py"), "run", "1",
                                 "--claude-bin", self.cli, "--no-render"], cwd=self.tree, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**self.env, "FAKE_MODE": "slow"},
                                preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN))
        for _ in range(100):
            if self.calls():
                break
            time.sleep(0.1)
        proc.send_signal(signal.SIGHUP)
        time.sleep(1.0)
        self.assertIsNone(proc.poll(), "a hangup under nohup must not stop the run")
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=30)
        self.assertEqual(self.record()["outcome"]["status"], "interrupted")

    def _inprocess(self, patch: str, *, force: bool = False, rerun: bool = False) -> subprocess.CompletedProcess:
        code = (
            "import sys, argparse; sys.path.insert(0, sys.argv[1]); import run_baseline as rb, render_comment, validate_outputs;"
            + patch +
            f"args = argparse.Namespace(claude_bin=sys.argv[2], model='sonnet-5-5', effort='xhigh', max_budget_usd='30',"
            f" timeout_seconds=60, retries=1, force={force}, rerun_failed={rerun}, no_render=False, no_scan=True);"
            "import baseline_common as bc; tree = bc.find_tree()\n"
            "try:\n    r = rb.run_one(tree, rb.pick(rb.load_subset(tree), '1'), args); print('RESULT', r['outcome']['status'])\n"
            "except KeyboardInterrupt:\n    print('STOPPED')")
        return subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPTS), self.cli], cwd=self.tree, text=True,
                              capture_output=True, env={**self.env, "FAKE_MODE": "good"}, timeout=120)

    def test_a_stop_after_the_call_keeps_its_outcome(self) -> None:
        self.subset()
        result = self._inprocess("render_comment.render = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt());")
        self.assertIn("STOPPED", result.stdout, result.stderr)
        record = self.record()
        self.assertEqual(record["outcome"]["status"], "completed")
        self.assertTrue(record["postprocess_interrupted"])

    def test_a_rerun_whose_post_processing_fails_still_owns_the_folder(self) -> None:
        self.subset()
        self.run_paper(mode="error")
        old = self.record()["run_id"]
        result = self._inprocess("validate_outputs.validate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x'));",
                                 rerun=True)
        self.assertIn("RESULT", result.stdout, result.stderr)
        record = self.record()
        self.assertNotEqual(record["run_id"], old)
        self.assertEqual(record["outcome"]["status"], "completed_invalid")
        self.assertIn("RuntimeError", record["postprocess_error"])

    def test_a_killed_run_is_booked_as_abandoned_and_not_rerun_silently(self) -> None:
        self.subset()
        stub = self.work / A1 / "20261009T000000Z-deadbeef"
        stub.mkdir(parents=True)
        (stub / "run.json").write_text(json.dumps({
            "paper": {"audit_id": A1}, "run_id": "20261009T000000Z-deadbeef", "started_at": "2026-10-09T00:00:00Z",
            "condition": {"requested_model": "sonnet-5-5", "reasoning_effort": "xhigh", "max_budget_usd": "30",
                          "timeout_seconds": 7200, "cli_version": "x"},
            "attempts": [{"number": 1, "status": "running", "started_at": "2026-10-09T00:00:00Z"}]}))
        skipped, _ = self.run_paper()
        self.assertIn("abandoned", skipped.stdout)
        self.assertEqual(self.calls(), [])
        self.run_paper("--rerun-failed")
        self.assertEqual([e["status"] for e in self.runlog()], ["abandoned", "completed"])

    def test_the_condition_is_checked_before_the_budget_and_the_providers_words_count(self) -> None:
        self.subset()
        cases = (("budget_wrong_model", "model", 1), ("login_empty", "authentication", 1), ("weekly_limit", "quota", 1),
                 ("overloaded_pre", "transient", 2))
        for mode, klass, calls in cases:
            with self.subTest(mode=mode):
                _, record = self.run_paper("--force", mode=mode)
                self.assertEqual(record["outcome"]["error_class"], klass)
                self.assertEqual(record["totals"]["model_calls"], calls)

    def test_records_never_carry_denied_tool_input(self) -> None:
        self.subset()
        _, record = self.run_paper(mode="denied_write")
        self.assertTrue(any(w.startswith("records/claude_stdout.json:permission_denials[].tool_input")
                            for w in record["outputs"]["withheld_from_tree"]), "the stripped input is listed")
        envelope = (self.dest() / "records" / "claude_stdout.json").read_text()
        self.assertNotIn("tool_input", envelope)
        self.assertNotIn("SUBJ_P12", envelope)
        self.assertIn('"tool_name": "Write"', envelope)
        self.assertNotIn(str(self.home), (self.dest() / "records" / "prompt.txt").read_text())

    def test_figures_scripts_and_long_tables_in_evidence(self) -> None:
        self.subset()
        _, record = self.run_paper(mode="svg_pii")
        self.assertIn("evidence/fig.svg", record["outputs"]["withheld_from_tree"])
        _, record = self.run_paper("--force", mode="sh_ok")
        self.assertEqual(record["outcome"]["status"], "completed")
        self.assertIn("evidence/run_all.sh", record["outputs"]["kept"])
        _, record = self.run_paper("--force", mode="long_csv")
        self.assertEqual(record["outcome"]["status"], "completed")
        self.assertIn("evidence/table.csv", record["outputs"]["withheld_from_tree"])

    def test_the_live_pipeline_helpers_are_used_and_each_failure_is_separate(self) -> None:
        skill = self.tree / "skill" / "i4r-audit-unified" / "i4r-audit"
        (skill / "scripts").mkdir(parents=True)
        (skill / "VERSION").write_text("9.9.9\n")
        (skill / "scripts" / "provider_safety.py").write_text("def deposit_exemptions(p):\n    return {'EMG_P01'}\n")
        (skill / "scripts" / "phase1_runtime.py").write_text(
            "def environment_fingerprint():\n    return {'python_version': 'fingerprint-from-this-host'}\n")
        self.subset()
        _, record = self.run_paper()
        inputs = record["inputs"]
        self.assertTrue(inputs["environment_fingerprint"]["regenerated_on_this_host"])
        self.assertEqual(inputs["deposit_exemptions"], {"count": 1, "source": "i4r-audit 9.9.9"})
        (skill / "scripts" / "phase1_runtime.py").write_text("raise RuntimeError('broken')\n")
        _, record = self.run_paper("--force")
        inputs = record["inputs"]
        self.assertFalse(inputs["environment_fingerprint"]["regenerated_on_this_host"])
        self.assertEqual(inputs["deposit_exemptions"], {"count": 1, "source": "i4r-audit 9.9.9"},
                         "a broken fingerprint helper does not take the exemptions with it")
        (skill / "scripts" / "provider_safety.py").write_text("raise RuntimeError('broken')\n")
        _, record = self.run_paper("--force")
        self.assertEqual(record["inputs"]["deposit_exemptions"]["source"], "none (i4r-audit 9.9.9 failed)")
        self.assertIn("describes the machine the original audit ran on", self.calls()[-1]["prompt"])

    def test_environment_values_never_reach_the_record(self) -> None:
        self.subset()
        _, record = self.run_paper(CLAUDE_TEST_SECRET="supersecretvalue-123")
        self.assertIn("CLAUDE_TEST_SECRET", record["environment"]["spawn_env_names"])
        for path in self.dest().rglob("*"):
            if path.is_file():
                self.assertNotIn("supersecretvalue-123", path.read_text(errors="replace"), path)

    def test_cloud_temp_and_session_folders_are_denied(self) -> None:
        cloud_tree = self.home / "Library" / "CloudStorage" / "Box" / "tree"
        cloud_tree.mkdir(parents=True)
        for rel in (".codex", ".i4r_scratch", "Library/Application Support/Claude", "Library/Mobile Documents",
                    ".claude/sessions"):
            (self.home / rel).mkdir(parents=True, exist_ok=True)
        (self.home / ".claude" / "history.jsonl").write_text("{}\n")
        (self.root / "claude-tmp" / "-some-session").mkdir(parents=True)
        code = ("import sys, json, pathlib; sys.path.insert(0, sys.argv[1]); import run_baseline as rb;"
                "print(json.dumps([str(p) for p in rb.deny_read_paths(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))]))")
        run_root = self.work / "a" / "r"
        run_root.mkdir(parents=True)
        out = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPTS), str(cloud_tree), str(run_root)],
                             capture_output=True, text=True, env=self.env, timeout=60)
        deny = json.loads(out.stdout)
        self.assertIn(str(self.home / "Library" / "CloudStorage"), deny)
        self.assertNotIn(str(cloud_tree), deny, "the whole cloud folder is denied, not just the tree")
        for rel in (".codex", ".i4r_scratch", "Library/Application Support/Claude", "Library/Mobile Documents",
                    ".claude/sessions", ".claude/history.jsonl"):
            self.assertIn(str(self.home / rel), deny, rel)
        self.assertIn(str(self.root / "claude-tmp" / "-some-session"), deny)
        self.subset()
        _, record = self.run_paper()
        settings = json.loads((pathlib.Path(record["work_folder"]) / "attempt_1" / "claude_settings.json").read_text())
        self.assertTrue({"WebFetch", "WebSearch"} <= set(settings["permissions"]["deny"]))
        result, _ = self.run_paper("--force", I4R_BASELINE_WORK=str(self.home / ".claude" / "newfolder"))
        self.assertIn("is inside", result.stdout + result.stderr)

    def test_the_lock_is_released_only_by_its_owner(self) -> None:
        code = ("import sys, pathlib; sys.path.insert(0, sys.argv[1]); import run_baseline as rb, baseline_common as bc;"
                "tree = bc.find_tree(); lock, mine = rb.acquire_lock(tree); lock.write_text('pid 1 on other since x\\n');"
                "rb.release_lock(lock, mine); print(lock.exists())")
        out = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPTS)], capture_output=True, text=True,
                             env=self.env, timeout=60)
        self.assertEqual(out.stdout.strip(), "True", out.stderr)


class BatchTests(Env):
    audits = ((A1, True), (A2, False))

    def test_a_budget_hit_is_a_per_paper_outcome_and_the_batch_goes_on(self) -> None:
        self.subset()
        result = self.script("run_baseline.py", "batch", "--claude-bin", self.cli, "--no-render", FAKE_MODE="budget")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([self.record(n)["outcome"]["status"] for n in (0, 1)], ["budget_exhausted"] * 2)
        self.assertEqual(len(self.calls()), 2)
        again = self.script("run_baseline.py", "batch", "--claude-bin", self.cli, "--no-render", "--rerun-failed",
                            FAKE_MODE="good")
        self.assertEqual(len(self.calls()), 2, "a budget-exhausted paper is not rerun without --force", )
        self.assertEqual(again.returncode, 0)

    def test_systemic_errors_stop_the_batch(self) -> None:
        import shutil
        self.subset()
        for mode, klass in (("session_limit", "quota"), ("wrong_model", "model")):
            with self.subTest(mode=mode):
                for n in (0, 1):
                    if self.dest(n).exists():
                        shutil.rmtree(self.dest(n))
                self.log.unlink(missing_ok=True)
                result = self.script("run_baseline.py", "batch", "--claude-bin", self.cli, "--no-render", FAKE_MODE=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.record(0)["outcome"]["error_class"], klass)
                self.assertEqual(len(self.calls()), 1, "the second paper is not paid for")
                self.assertFalse((self.tree / "baseline_comments" / ".batch.lock").exists())


class SubsetTests(Env):
    def test_allocation_is_proportional_or_balanced_and_capped(self) -> None:
        strata = {True: list(range(3)), False: list(range(30))}
        self.assertEqual(select_subset.allocate(strata, 25), {True: 2, False: 23})
        self.assertEqual(select_subset.allocate(strata, 25, balanced=True), {True: 3, False: 22})
        strata = {True: list(range(11)), False: list(range(32))}
        self.assertEqual(select_subset.allocate(strata, 25, balanced=True), {True: 11, False: 14})

    def test_the_draw_is_reproducible(self) -> None:
        papers = [{"audit_id": f"a{i:02d}", "code_present": i % 4 == 0} for i in range(40)]
        self.assertEqual(select_subset.draw(papers, 10, 7), select_subset.draw(papers, 10, 7))
        self.assertNotEqual(select_subset.draw(papers, 10, 7), select_subset.draw(papers, 10, 8))

    def test_a_seeded_draw_records_the_pool(self) -> None:
        make_tree(self.root / "two", ((A1, True), (A2, False)))
        env = {**self.env, "I4R_TREE": str(self.root / "two" / "tree")}
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "select_subset.py"), "--n", "1", "--seed", "5",
                                 "--write"], cwd=self.root / "two" / "tree", text=True, capture_output=True, env=env,
                                timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        subset = json.loads((self.root / "two" / "tree" / "baseline_comments" / "SUBSET.json").read_text())
        self.assertEqual((subset["pool_name"], subset["pool_size"], subset["pool_audit_ids"]), ("shipped", 2, [A1, A2]))
        self.assertEqual(subset["n"], 1)

    def test_the_subset_records_its_pool_and_is_protected_once_used(self) -> None:
        self.subset()
        subset = json.loads((self.tree / "baseline_comments" / "SUBSET.json").read_text())
        self.assertEqual(subset["pool_audit_ids"], [A1])
        self.assertIn("pool_sha256", subset)
        self.run_paper()
        ids = self.root / "ids.txt"
        refused = self.script("select_subset.py", "--audit-ids", str(ids), "--write")
        self.assertNotEqual(refused.returncode, 0)
        replaced = self.script("select_subset.py", "--audit-ids", str(ids), "--write", "--replace")
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertEqual(len(list((self.tree / "baseline_comments").glob("SUBSET.*.json"))), 1)


class DenyListTests(Env):
    def test_no_protected_root_may_contain_the_run_and_siblings_are_denied(self) -> None:
        code = ("import sys, json, pathlib; sys.path.insert(0, sys.argv[1]); import run_baseline as rb;"
                "tree, run = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]);"
                "print(json.dumps([str(p) for p in rb.deny_read_paths(tree, run)]))")
        run_root = self.work / "a1" / "r1"
        run_root.mkdir(parents=True)
        (self.work / "a2" / "r9").mkdir(parents=True)
        out = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPTS), str(self.tree), str(run_root)],
                             capture_output=True, text=True, env=self.env, timeout=60)
        deny = json.loads(out.stdout)
        self.assertIn(str(self.tree), deny)
        self.assertIn(str(self.work / "a2" / "r9"), deny)
        self.assertIn(str(self.home / "Downloads"), deny)
        bad_run = self.home / "Downloads" / "w" / "a" / "r"
        bad_run.mkdir(parents=True)
        out = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPTS), str(self.tree), str(bad_run)],
                             capture_output=True, text=True, env=self.env, timeout=60)
        self.assertIn("inside the protected", out.stderr)


class ValidatorTests(unittest.TestCase):
    def outputs(self, **edits) -> pathlib.Path:
        tmp = tempfile.TemporaryDirectory(prefix="baseline-validate-")
        self.addCleanup(tmp.cleanup)
        out = pathlib.Path(tmp.name)
        env = {**os.environ, "FAKE_MODE": "good"}
        env.pop("FAKE_LOG", None)
        subprocess.run([sys.executable, str(FAKE)], cwd=out, input="", text=True, capture_output=True, env=env, check=True)
        for name, (old, new) in edits.items():
            path = out / name
            text = path.read_text()
            self.assertIn(old, text)
            path.write_text(text.replace(old, new))
        return out

    def test_the_fake_good_output_passes(self) -> None:
        self.assertTrue(validate_outputs.validate(self.outputs())["ok"])

    def test_structure_agreement_and_hygiene_errors(self) -> None:
        cases = {
            "abstract": {"REPORT_final.md": ("The deposited data", "word " * 260 + "The deposited data")},
            "citation": {"REPORT_final.md": ("AI-vertical", "AI-other")},
            "appendix": {"REPORT_final.md": ("| CONFIRMED | No |", "| CONFIRMED | Yes |")},
            "status": {"REPORT_final.md": ("| CONFIRMED | No |", "| LIKELY | No |")},
            "narration": {"REPORT_final.md": ("None beyond the above.", "An earlier draft said otherwise.")},
            "recommendations": {"REPORT_final.md": ("## 4. Minor issues", "## 4. Recommendations")},
            "conclusion": {"REPORT_final.md": ("## 6. Conclusion", "## 6. Closing")},
            "appendix_heading": {"REPORT_final.md": ("## Appendix A. Summary of findings", "## Appendix A")},
            "placeholder": {"REPORT_final.md": ("None beyond the above.", "TBD")},
            "abs_path": {"REPORT_final.md": ("The scripts are in evidence/.", "See /" + "Users/someone/x.R.")},
            "pid_date": {"REPORT_final.md": ("Duplicate rows were tested", "participant 12 was tested on 2019-03-02; rows were tested")},
            "pcode_clock": {"REPORT_final.md": ("Duplicate rows were tested", "P7 stopped at 10:42:05; rows were tested")},
            "code_token": {"REPORT_final.md": ("Duplicate rows", "SUBJ_P12 rows")},
            "email": {"REPORT_final.md": ("Duplicate rows", "jane.doe@uni.edu rows")},
            "captions": {"captions.txt": ("Summary of findings", "Table 1. Summary of findings")},
            "front": {"front_matter.md": ("**Replication package:**", "**Package:**")},
            "front_pii": {"front_matter.md": ("DOI: 10.0/test", "DOI: 10.0/test SUBJ_P12")},
            "ids": {"findings.json": ('"F1"', '"F2"')},
            "id_heading": {"REPORT_final.md": ("does not yield\n", "does not yield (F1)\n")},
            "id_text": {"REPORT_final.md": ("None beyond the above.", "None beyond F1.")},
            "id_label": {"REPORT_final.md": ("None beyond the above.", "**F1. A label.** None.")},
            "marker_missing": {"REPORT_final.md": ("<!-- finding:F1 -->", "")},
            "marker_unknown": {"REPORT_final.md": ("<!-- finding:F1 -->", "<!-- finding:F1 -->\n<!-- finding:F7 -->")},
            "marker_twice": {"REPORT_final.md": ("None beyond the above.", "None. <!-- finding:F1 -->")},
            "f_rows": {"REPORT_final.md": ("| 1 | Table 2 mean", "| F1 | Table 2 mean")},
            "enum": {"findings.json": ('"value_wrong"', '"bad_value"')},
            "paper_only": {"findings.json": ('"paper_only": false', '"paper_only": "no"')},
            "unverifiable_no_cause": {"findings.json": ('"value_wrong"', '"unverifiable"')},
            "trio_on_non_unverifiable": {"findings.json": ('"blocked_tier": null', '"blocked_tier": 2')},
            "remedy_verified_missing": {"findings.json": ('"remedy_verified": true, ', '')},
            "abstract_heading": {"REPORT_final.md": ("## Abstract", "## Abstract and summary")},
        }
        for name, edits in cases.items():
            with self.subTest(case=name):
                self.assertFalse(validate_outputs.validate(self.outputs(**edits))["ok"], name)

    def test_compact_findings_json_is_judged_finding_by_finding(self) -> None:
        out = self.outputs()
        data = json.loads((out / "findings.json").read_text())
        second = dict(json.loads(json.dumps(data["findings"][0])), id="F2", claim="Questionnaire item P3 is reverse-scored")
        data["findings"][0]["evidence"]["artifact"] = "pkg/export_2022-03-14.csv"
        data["findings"].append(second)
        (out / "findings.json").write_text(json.dumps(data))
        report = (out / "REPORT_final.md").read_text().replace(
            "| 1 | Table 2 mean differs from the deposit | CONFIRMED | No | Section 3 |",
            "| 1 | Table 2 mean differs from the deposit | CONFIRMED | No | Section 3 |\n| 2 | Item | CONFIRMED | No | Section 3 |")
        report = report.replace("None beyond the above.", "**An item is reverse-scored.** <!-- finding:F2 --> Noted.")
        (out / "REPORT_final.md").write_text(report)
        result = validate_outputs.validate(out)
        self.assertTrue(result["ok"], result["errors"])

    def test_a_heading_with_a_finding_id_is_named(self) -> None:
        result = validate_outputs.validate(self.outputs(**{"REPORT_final.md": ("does not yield\n", "does not yield (F1)\n")}))
        self.assertTrue(any("carries a finding id" in e for e in result["errors"]), result["errors"])

    def test_ids_in_code_spans_and_f_statistics_are_not_finding_ids(self) -> None:
        out = self.outputs(**{"REPORT_final.md": ("None beyond the above.",
                                                  "The file `F1.csv` gives F(1, 46) = 3.2 and F1.5 is a label.")})
        result = validate_outputs.validate(out)
        self.assertTrue(result["ok"], result["errors"])

    def test_the_printed_appendix_is_generated_from_findings_json(self) -> None:
        import render_comment
        out = self.outputs()
        text, how = render_comment.rendered_report(out)
        self.assertTrue(how.startswith("generated from findings.json"), how)
        self.assertIn(render_comment.FINDINGS_HEADER + "\n" + render_comment.APPENDIX_SEPARATOR + "\n"
                      "| 1 | Table 2 mean differs from the deposit | CONFIRMED | No | Section 3 |", text)
        self.assertIn("<!-- I4R-GENERATED-CLAIM-TAGS:START -->\n### Findings cross-reference\n\n"
                      "- [claim 1] Section 3 — Table 2 mean differs from the deposit", text)
        self.assertLess(text.index("### Findings cross-reference"), text.index("## Appendix A"))
        self.assertEqual(text.count("## Appendix A"), 1)
        self.assertEqual(render_comment.APPENDIX_SEPARATOR, "|---:|" + "-" * 64 + "|" + "-" * 12 + "|:" + "-" * 10
                         + ":|" + "-" * 10 + "|", "i4r-audit render(): ids right-aligned, the conclusion column centred")
        data = json.loads((out / "findings.json").read_text())
        data["findings"][0]["id"] = "F2"
        self.assertIsNone(render_comment.generated_blocks(data["findings"]), "ids out of order are not projected")
        (out / "findings.json").write_text("{broken")
        report = (out / "REPORT_final.md").read_text().replace("| 1 | Table 2 mean", "| F1 | Table 2 mean")
        (out / "REPORT_final.md").write_text(report)
        text, how = render_comment.rendered_report(out)
        self.assertIn("could not be projected", how)
        self.assertIn("| 1 | Table 2 mean", text)
        self.assertNotIn("Findings cross-reference", text)

    def test_other_appendix_a_material_moves_to_appendix_b(self) -> None:
        import render_comment
        out = self.outputs()
        report = (out / "REPORT_final.md").read_text().rstrip() + (
            "\n\n| File | Role | Issue |\n|---|---|---|\n| pkg/data.csv | data | None |\n")
        (out / "REPORT_final.md").write_text(report)
        text, how = render_comment.rendered_report(out)
        self.assertIn("Appendix B. Deposited files", how)
        a, b = text.index("## Appendix A"), text.index("## Appendix B. Deposited files")
        self.assertLess(a, b)
        self.assertIn("| pkg/data.csv | data | None |", text[b:])
        self.assertNotIn("pkg/data.csv", text[:b], "the files table is not left in Appendix A")
        self.assertEqual(len(re.findall(r"(?m)^\|[-:| ]+\|$", text)), 2, "both tables are printed")

    def test_a_short_report_is_an_error(self) -> None:
        out = self.outputs()
        text = (out / "REPORT_final.md").read_text()
        self.assertGreater(len(text), 1000)
        self.assertTrue(validate_outputs.validate(out)["ok"])

    def test_a_complete_unverifiable_finding_passes(self) -> None:
        out = self.outputs(**{"findings.json": ('"impact": "value_wrong"', '"impact": "unverifiable"')})
        data = json.loads((out / "findings.json").read_text())
        data["findings"][0].update(unverifiable_reason="code_missing", blocked_tier=3,
                                   unverifiable_basis="The model script is not in the deposit.")
        (out / "findings.json").write_text(json.dumps(data))
        self.assertTrue(validate_outputs.validate(out)["ok"])

    def test_binary_and_compressed_evidence_are_errors(self) -> None:
        for name in ("rows.rds", "rows.csv.gz", "blob.bin"):
            with self.subTest(file=name):
                out = self.outputs()
                (out / "evidence" / name).write_bytes(b"\x00\x01")
                self.assertFalse(validate_outputs.validate(out)["ok"])

    def test_a_deposit_header_may_be_quoted(self) -> None:
        out = self.outputs(**{"REPORT_final.md": ("Duplicate rows", "The column EMG_P01 rows")})
        self.assertTrue(validate_outputs.validate(out, None, {"EMG_P01"})["ok"])
        self.assertFalse(validate_outputs.validate(out, None, set())["ok"])


class ProvenanceTests(unittest.TestCase):
    @unittest.skipUnless((LIVE / "scripts" / "generate_finding_artifacts.py").is_file(), "i4r-audit not beside the baseline")
    def test_the_appendix_generator_is_the_live_one(self) -> None:
        import importlib.util
        import inspect
        import render_comment
        spec = importlib.util.spec_from_file_location("gfa", LIVE / "scripts" / "generate_finding_artifacts.py")
        live = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(live)
        for name in ("inject_report", "clean_cell"):
            self.assertEqual(inspect.getsource(getattr(render_comment, name)), inspect.getsource(getattr(live, name)), name)
        source = inspect.getsource(live.render)
        self.assertIn(render_comment.FINDINGS_HEADER, source)
        self.assertIn('"|---:|" + "-" * 64 + "|" + "-" * 12 + "|:" + "-" * 10 + ":|" + "-" * 10 + "|"', source)
        self.assertEqual(render_comment.APPENDIX_SEPARATOR,
                         "|---:|" + "-" * 64 + "|" + "-" * 12 + "|:" + "-" * 10 + ":|" + "-" * 10 + "|")

    def test_reference_files_are_the_recorded_bytes(self) -> None:
        prov = json.loads((SKILL / "reference" / "PROVENANCE.json").read_text())
        for name, row in prov["files"].items():
            self.assertEqual(sha((SKILL / "reference" / name).read_bytes()), row["sha256"], name)

    @unittest.skipUnless((LIVE / "scripts" / "run_phase1.py").is_file(), "i4r-audit not beside the baseline")
    def test_the_argv_and_sandbox_match_the_live_auditor_launch(self) -> None:
        code = ("import sys, json, pathlib; sys.path.insert(0, sys.argv[1]); import run_phase1, phase1_runtime as rt;"
                "print(json.dumps({'argv': run_phase1.claude_command('CLI', pathlib.Path('S'), pathlib.Path('A'),"
                " pathlib.Path('B'), 'SID', 'claude-sonnet-5-5', 'xhigh', '30'), 'allow': list(rt.BASH_ALLOWLIST),"
                " 'steer': run_phase1.BASH_STEER, 'tools': list(rt.NON_BASH_TOOLS)}))")
        live = json.loads(subprocess.run([sys.executable, "-B", "-c", code, str(LIVE / "scripts")], capture_output=True,
                                         text=True, timeout=120, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}).stdout)
        self.assertEqual(live["allow"], list(bc.BASH_ALLOWLIST))
        self.assertEqual(live["steer"], bc.BASH_STEER)
        self.assertEqual(live["tools"], list(bc.NON_BASH_TOOLS))
        ours = bc.claude_command("CLI", pathlib.Path("S"), [pathlib.Path("A"), pathlib.Path("B")], "SID",
                                 "claude-sonnet-5-5", "xhigh", "30", ["Read(//A/**)"])
        cut = live["argv"].index("--allowedTools")
        self.assertEqual(ours[:cut], live["argv"][:cut], "every flag before the file-tool grants is the auditor's")
        self.assertEqual(ours[cut + 2:], live["argv"][cut + 1 + len(live["tools"]):])


if __name__ == "__main__":
    unittest.main()
