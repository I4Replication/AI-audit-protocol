"""An unprojectable terminal row must be refused, never committed.

The pre-commit proof in `_append_locked` used to run only `project_run()`. `project_all_locked`
then ran the metrics projection outside any proof, so a terminal row that `project_run` accepted
but `summarize_invocations.aggregate()` could not read -- a bare
`log_event.sh invocation_terminal invocation_id=x`, or a runner omitting one telemetry key -- was
fsynced into the append-only journal. Every later append, projection, recovery and the finalize
commit then died on the same KeyError, and with the missing key immutable there was no remedy.

Each test builds the audit with the shipped initializer and drives the shipped code.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
import claude_routes  # noqa: E402
import lifecycle_journal  # noqa: E402


def _init(root: pathlib.Path) -> pathlib.Path:
    audit = root / "audit"
    (audit / "pkg").mkdir(parents=True)
    (audit / "pkg" / "f.csv").write_text("x\n")
    env = dict(os.environ, LC_ALL="C")
    subprocess.run(
        ["/bin/bash", str(SCRIPTS / "init_run.sh"), str(audit), "--audit-id", "t",
         "--doi", "10.1234/x", "--auditor-model", claude_routes.SHIPPED["requested_model"],
         "--auditor-family", "claude",
         "--auditor-provider", claude_routes.SHIPPED["provider"], "--protocol-version", "p1",
         "--gate-set-version", "g1"],
        check=True, text=True, capture_output=True, env=env, timeout=60)
    return audit


class JournalPoisonRefusal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-journal-poison-")
        self.audit = _init(pathlib.Path(self.tmp.name))
        self.journal = self.audit / "run.events.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_bare_terminal_row_is_refused_and_journal_untouched(self):
        before = self.journal.read_bytes()
        with self.assertRaises(lifecycle_journal.JournalError) as ctx:
            lifecycle_journal.append_events(self.audit, [
                {"type": "invocation_terminal", "payload": {"invocation_id": "inv-1"},
                 "at": "2026-09-10T16:00:00Z"},
            ])
        self.assertIn("not projectable", str(ctx.exception))
        self.assertEqual(before, self.journal.read_bytes(), "the poison row was committed")
        # And the audit is still alive: an ordinary event appends and projects.
        lifecycle_journal.append_events(self.audit, [
            {"type": "phase_start", "payload": {"phase": "critique"}, "at": "2026-09-10T16:00:01Z"},
        ])
        run = json.loads((self.audit / "run.json").read_text())
        self.assertIn("critique", json.dumps(run.get("phases") or run))

    def test_generic_cli_no_longer_admits_lifecycle_types(self):
        """The typed APIs validate whole records; the key=value CLI could not."""
        before = self.journal.read_bytes()
        for kind in ("invocation_terminal", "cache_reuse", "invocation_reserved",
                     "provider_request", "inference_start", "provider_response"):
            result = subprocess.run(
                ["/bin/bash", str(SCRIPTS / "log_event.sh"), "--run", str(self.audit / "run.json"),
                 kind, "invocation_id=inv-1"],
                text=True, capture_output=True, timeout=60)
            self.assertNotEqual(0, result.returncode, kind)
        self.assertEqual(before, self.journal.read_bytes())

    def test_an_unprojectable_record_through_the_wrapper_is_refused_by_the_journal(self):
        """The public wrapper synthesizes lifecycle for partially-shaped rows on purpose, so the
        guard is the journal's own pre-commit proof, not a schema check at the wrapper."""
        import record_invocation
        before = self.journal.read_bytes()
        with self.assertRaises(lifecycle_journal.JournalError):
            record_invocation.record_terminal(self.audit, {
                "invocation_id": "inv-2", "status": "completed", "provider_call": True,
                "attempt_kind": "role_inference",
            })
        self.assertEqual(before, self.journal.read_bytes())

    def test_a_valid_batch_still_appends(self):
        """Negative control: the proof must not refuse ordinary work."""
        lifecycle_journal.append_events(self.audit, [
            {"type": "phase_start", "payload": {"phase": "fan_out"}, "at": "2026-09-10T16:00:00Z"},
            {"type": "phase_end", "payload": {"phase": "fan_out"}, "at": "2026-09-10T16:10:00Z"},
        ])
        rows = [json.loads(l) for l in self.journal.read_text().splitlines() if l.strip()]
        self.assertEqual(["phase_start", "phase_end"], [r["type"] for r in rows[-2:]])
        self.assertTrue((self.audit / "run_metrics.json").is_file())


if __name__ == "__main__":
    unittest.main()
