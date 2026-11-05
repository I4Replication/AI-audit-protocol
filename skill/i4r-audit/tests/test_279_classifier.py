"""2.7.9: a provider at capacity is throttling, and only the provider's own diagnostics are classified.

Audit A80's round-1 completeness critic worked for minutes, printing source/context/PIPELINE_POLICY.json
among other files, and Codex then reported "Selected model is at capacity. Please try a different model.".
failure.log is validation.txt, stderr and the first 200 event rows, so it carried the whole session transcript; the
classifier matched "authentication" in the printed policy's non_retryable_error_classes and booked a permanent
authentication failure. The role stopped on attempt 1 of 3 and the round failed its quorum. No rule matched the
capacity message itself either, so even a clean log was booked permanent. scripts/check_critic_access.py shares the
classifier, so a capacity reply there told the user to change accounts (exit 3).

The classifier (test_run_critique_runtime.provider_failure_classification) now drops every JSON event row other than
``error``, ``turn.failed`` and Codex's own warnings (``item.completed`` items of type ``error``, such as "Falling back
from WebSockets to HTTPS transport", the transport signal of the 2026-09-03 and 2026-10-13 outages). The provider's
last word (the last ``turn.failed`` message, else the last ``error`` row) is classified first and decides when it
names a cause other than a bare timeout; there "at capacity", "overloaded" and "server is busy" are throttling, so a
non-terminal "Reconnecting... (server overloaded)" row cannot outrank an unsupported-model or quota refusal.
Otherwise all the diagnostics are read with the rules of 2.7.8. classify-provider-failure also reads the diagnostic
rows of the events.jsonl beside the log, which failure.log cuts at 200 rows. Each test runs the real code:
the helper's classify-provider-failure command on a failure.log built as run_critique.sh builds it,
check_critic_access.py against tests/fake_codex.sh, and run_critique.sh itself. No provider is called.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
SKILL = TESTS.parent
SCRIPTS = SKILL / "scripts"
for _path in (str(SCRIPTS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import test_270_critic_route as t270  # noqa: E402  (fixtures only)
import test_275_critic_route_lock as t275  # noqa: E402  (fixtures only)

HELPER = SCRIPTS / "test_run_critique_runtime.py"
CAPACITY = "Selected model is at capacity. Please try a different model."
POLICY_COMMAND = "/bin/zsh -lc 'cat source/context/PIPELINE_POLICY.json'"


def row(event_type: str, **fields) -> dict:
    return {"type": event_type, **fields}


def command_rows(item_id: str, output: str, command: str = POLICY_COMMAND) -> list[dict]:
    """One command_execution item as codex exec --json writes it: started, then completed with its output."""
    item = {"id": item_id, "type": "command_execution", "command": command}
    return [row("item.started", item={**item, "aggregated_output": "", "exit_code": None, "status": "in_progress"}),
            row("item.completed", item={**item, "aggregated_output": output, "exit_code": 0, "status": "completed"})]


def capacity_rows() -> list[dict]:
    return [row("error", message=CAPACITY), row("turn.failed", error={"message": CAPACITY})]


def session(*outputs: str) -> list[dict]:
    """The shape of audit A80's failed session: a message, then commands and their output."""
    rows = [row("thread.started", thread_id="fake-thread"), row("turn.started"),
            row("item.completed", item={"id": "item_0", "type": "agent_message",
                                        "text": "I will read the frozen context first."})]
    for number, output in enumerate(outputs, start=1):
        rows += command_rows(f"item_{number}", output)
    return rows


# The two real outage shapes (one audit's, 2026-09-03; another audit's, 2026-10-13): five
# reconnects over WebSockets, Codex's warning that it falls back to HTTPS, five more reconnects, and the same error
# as the last word. Neither error names a cause; stderr is as recorded, less the MCP worker's tracing lines.
NOT_FOUND = ("unexpected status 404 Not Found: Unknown error, url: https://chatgpt.com/backend-api/codex/responses, "
             "cf-ray: 0000000000000000-XYZ")
OUTAGES = {
    "2026-09-03": (NOT_FOUND.replace("https://", "wss://"), NOT_FOUND,
                 "2026-09-03T00:00:00.000000Z ERROR codex_models_manager::manager: failed to refresh available models: "
                 "unexpected status 404 Not Found: Unknown error, url: https://chatgpt.com/backend-api/codex/models\n"
                 "2026-09-03T00:00:01.000000Z ERROR codex_api::endpoint::responses_websocket: failed to connect to "
                 "websocket: HTTP error: 404 Not Found, url: wss://chatgpt.com/backend-api/codex/responses\n"),
    "2026-10-13": ("workspace routing discovery failed", "workspace routing discovery failed",
                 "2026-10-13T00:00:00.000000Z ERROR codex_models_manager::manager: failed to refresh available models: "
                 "Connection failed: error sending request\n"),
}


def outage_rows(websocket_reason: str, reason: str, warning: bool = True) -> list[dict]:
    rows = [row("thread.started", thread_id="t"), row("turn.started")]
    rows += [row("error", message=f"Reconnecting... {n}/5 ({websocket_reason})") for n in range(2, 6)]
    if warning:
        warning = f"Falling back from WebSockets to HTTPS transport. {websocket_reason}"
        rows.append(row("item.completed", item={"id": "item_0", "type": "error", "message": warning}))
    rows += [row("error", message=f"Reconnecting... {n}/5 ({reason})") for n in range(1, 6)]
    return rows + [row("error", message=reason), row("turn.failed", error={"message": reason})]


OVERLOADED_RECONNECT = "Reconnecting... 1/5 (server overloaded)"
USAGE_LIMIT = "You've hit your usage limit. Upgrade to Pro or try again in 4 days."   # no rule names it
# The provider's last words from real failure logs and the access check, by the class each has always had.
REFUSALS = {
    "unsupported_model": "The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account.",
    "quota": "Your workspace is out of credits. Ask your workspace owner to refill in order to continue.",
    "authentication": "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header",
    "authorization": "unexpected status 403 Forbidden: organization must be verified",
    "deterministic_schema": json.dumps({"type": "error", "error": {
        "type": "invalid_request_error", "code": "invalid_json_schema",
        "message": "Invalid schema for response_format 'codex_output_schema'"}}),
}


def refusal_after_overloaded_reconnect(reason: str) -> list[dict]:
    return [row("thread.started", thread_id="t"), row("turn.started"), row("error", message=OVERLOADED_RECONNECT),
            row("error", message=reason), row("turn.failed", error={"message": reason})]


class Fixture(unittest.TestCase):
    """A fixture audit whose run.json binds its policy snapshot, as classify-provider-failure requires."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-279-")
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()
        self.audit = t270.write_audit(self.root / "audit", t270.POLICY_PATH, "20261026-audit-c0ffee01")
        self.addCleanup(t270.thaw, self.root)
        self.policy = self.audit / "pipeline-policy.snapshot.json"
        self.printed_policy = self.policy.read_text(encoding="utf-8")
        self.assertIn('"authentication"', self.printed_policy, "the fixture must print the word that misled 2.7.8")
        self.logs = 0

    def failure_log(self, events: list[dict], stderr: str = "",
                    validation: str = "provider exit=1; response missing or empty\n",
                    ascii_rows: bool = True) -> pathlib.Path:
        """validation.txt, stderr.txt and events.jsonl concatenated by run_critique.sh's own commands."""
        self.logs += 1
        attempt = self.root / "attempts" / str(self.logs)
        attempt.mkdir(parents=True)
        (attempt / "validation.txt").write_text(validation, encoding="utf-8")
        (attempt / "stderr.txt").write_text(stderr, encoding="utf-8")
        (attempt / "events.jsonl").write_text(
            "".join(json.dumps(event, ensure_ascii=ascii_rows) + "\n" for event in events), encoding="utf-8")
        subprocess.run(["bash", "-c", "{ sed -n '1,200p' validation.txt 2>/dev/null; "
                                      "sed -n '1,200p' stderr.txt 2>/dev/null; "
                                      "sed -n '1,200p' events.jsonl 2>/dev/null; } > failure.log"],
                       cwd=attempt, check=True, env=t270.clean_env())
        return attempt / "failure.log"

    def classify(self, log: pathlib.Path) -> str:
        result = subprocess.run(
            [sys.executable, str(HELPER), "classify-provider-failure", "--workspace", str(self.audit),
             "--policy", str(self.policy), "--log", str(log)],
            text=True, capture_output=True, timeout=120, check=False, env=t270.clean_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()


class ClassifierReadsOnlyProviderDiagnostics(Fixture):
    def test_a_model_at_capacity_after_a_printed_policy_is_throttling(self) -> None:
        log = self.failure_log(session("{}", self.printed_policy, "{}") + capacity_rows())
        self.assertIn("authentication", log.read_text(encoding="utf-8"))
        self.assertEqual(self.classify(log), "transient throttling")

    def test_the_same_session_without_an_error_row_is_not_authentication(self) -> None:
        log = self.failure_log(session("{}", self.printed_policy, "{}"))
        self.assertIn("authentication", log.read_text(encoding="utf-8"))
        self.assertEqual(self.classify(log), "permanent permanent",
                         "an exit with no provider diagnosis stays permanent, but is not an account failure")

    def test_a_line_separator_in_command_output_does_not_split_the_row(self) -> None:
        # codex writes U+2028 unescaped (serde_json does), and str.splitlines() would split the row there.
        output = "Table 2 (extracted from the article PDF)\n" + self.printed_policy
        log = self.failure_log(session(output), ascii_rows=False)
        self.assertIn(" ", log.read_text(encoding="utf-8"))
        self.assertEqual(self.classify(log), "permanent permanent")

    def test_a_line_nested_too_deep_to_parse_is_kept_as_text_not_a_crash(self) -> None:
        log = self.failure_log(capacity_rows(), stderr='{"type": "item.completed", "item": ' * 50000 + "\n")
        self.assertEqual(self.classify(log), "transient throttling")

    def test_words_in_the_transcript_never_classify_the_failure(self) -> None:
        output = ("README: if the upload fails with HTTP 429 (rate limit) or 'stream disconnected', retry;\n"
                  "'out of credits' means the lab account ran dry; a 503 Service Unavailable is the server.\n")
        log = self.failure_log(session(output))
        self.assertEqual(self.classify(log), "permanent permanent")

    def test_every_capacity_wording_is_throttling_even_beside_a_permanent_word(self) -> None:
        stderr = "2026-10-18T21:50:01.000000Z ERROR codex_login: authentication token refresh deferred\n"
        for message in (CAPACITY, "The server is overloaded or not ready yet.",
                        json.dumps({"error": {"code": "server_is_overloaded", "message": "try again"}}),
                        "Server is busy, please try again later."):
            with self.subTest(message=message):
                log = self.failure_log([row("thread.started", thread_id="t"), row("error", message=message),
                                        row("turn.failed", error={"message": message})], stderr=stderr)
                self.assertEqual(self.classify(log), "transient throttling")

    def test_negative_control_provider_diagnostics_classify_as_before(self) -> None:
        # Error rows and stderr lines from real failure logs (numbers kept, no audit content).
        cases = (
            ([row("error", message="Your workspace is out of credits. Ask your workspace owner to refill in order "
                                   "to continue.")], "", "permanent quota"),
            ([row("error", message=json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "code": "invalid_json_schema",
                "message": "Invalid schema for response_format 'codex_output_schema'"}}, indent=2))],
             "", "permanent deterministic_schema"),
            ([row("error", message="Reconnecting... 2/5 (stream disconnected before completion: IO error: "
                                   "Connection reset by peer (os error 54))")], "", "transient transport"),
            ([row("error", message="exceeded retry limit, last status: 429 Too Many Requests")], "",
             "transient throttling"),
            ([row("error", message="The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT "
                                   "account.")], "", "permanent unsupported_model"),
            ([row("error", message="You've hit your usage limit.")], "", "permanent permanent"),
            ([], "stream disconnected before completion: error sending request\n", "transient transport"),
            # stderr with no final newline runs into the first event row; the joined line is kept as text.
            (session("{}"), "connection reset by peer", "transient transport"),
            # a JSON value that is not an event object is kept as text
            ([], "429\n", "transient throttling"),
        )
        for events, stderr, expected in cases:
            with self.subTest(expected=expected, stderr=stderr[:30]):
                self.assertEqual(self.classify(self.failure_log(events, stderr=stderr)), expected)


class CodexWarningsAreDiagnostics(Fixture):
    def test_the_websocket_fallback_warning_marks_an_outage_as_transport(self) -> None:
        for audit, (websocket_reason, reason, stderr) in OUTAGES.items():
            with self.subTest(outage=audit):
                log = self.failure_log(outage_rows(websocket_reason, reason), stderr=stderr)
                self.assertNotRegex(log.read_text(encoding="utf-8"), r"(?i)rmcp|channel closed")
                self.assertEqual(self.classify(log), "transient transport")

    def test_negative_control_without_the_warning_the_outage_names_no_cause(self) -> None:
        for audit, (websocket_reason, reason, stderr) in OUTAGES.items():
            with self.subTest(outage=audit):
                log = self.failure_log(outage_rows(websocket_reason, reason, warning=False), stderr=stderr)
                self.assertEqual(self.classify(log), "permanent permanent")


class TheProviderLastWordDecides(Fixture):
    def test_a_refusal_after_an_overloaded_reconnect_keeps_its_class(self) -> None:
        for expected, reason in REFUSALS.items():
            rows = refusal_after_overloaded_reconnect(reason)
            for shape, events in (("error and turn.failed", rows), ("turn.failed only", rows[:-2] + rows[-1:]),
                                  ("error rows only", rows[:-1])):
                with self.subTest(expected=expected, shape=shape):
                    self.assertEqual(self.classify(self.failure_log(events)), f"permanent {expected}")

    def test_the_last_word_outranks_a_permanent_word_in_stderr(self) -> None:
        stderr = "2026-10-18T21:50:01.000000Z ERROR codex_login: authentication token refresh deferred\n"
        reason = "stream disconnected before completion: error sending request"
        log = self.failure_log([row("thread.started", thread_id="t"), row("error", message=reason),
                                row("turn.failed", error={"message": reason})], stderr=stderr)
        self.assertEqual(self.classify(log), "transient transport")

    def test_a_bare_timeout_as_the_last_word_does_not_outrank_a_named_fault(self) -> None:
        reconnect = "Reconnecting... 1/5 (stream disconnected before completion: error sending request)"
        timed_out = [row("error", message="request timed out"),
                     row("turn.failed", error={"message": "request timed out"})]
        self.assertEqual(self.classify(self.failure_log([row("error", message=reconnect), *timed_out])),
                         "transient transport")
        self.assertEqual(self.classify(self.failure_log(timed_out)), "permanent timeout",
                         "a bare timeout alone stays permanent")

    def test_a_capacity_word_counts_only_as_the_last_word(self) -> None:
        # A refusal no rule names leaves every diagnostic to the rules of 2.7.8, which have no capacity rule: the
        # overloaded reconnect before it does not make it retryable.
        self.assertEqual(self.classify(self.failure_log(refusal_after_overloaded_reconnect(USAGE_LIMIT))),
                         "permanent permanent")
        self.assertEqual(self.classify(self.failure_log(refusal_after_overloaded_reconnect(CAPACITY))),
                         "transient throttling")


class RowsPastTheFailureLogCap(Fixture):
    """failure.log keeps the first 200 rows of the events.jsonl the driver writes beside it."""

    def long_session(self) -> list[dict]:
        rows = session(self.printed_policy)
        for number in range(2, 102):
            rows += command_rows(f"item_{number}", "a.txt\n", command="/bin/zsh -lc 'ls'")
        self.assertGreater(len(rows), 200)
        return rows

    def test_a_refusal_past_row_200_is_read_from_events_jsonl(self) -> None:
        log = self.failure_log(self.long_session() + capacity_rows())
        self.assertNotIn(CAPACITY, log.read_text(encoding="utf-8"), "the refusal is past the log's 200 rows")
        self.assertEqual(self.classify(log), "transient throttling")

    def test_a_symlinked_events_file_is_not_read(self) -> None:
        log = self.failure_log(self.long_session())
        elsewhere = self.root / "elsewhere.jsonl"
        elsewhere.write_text("".join(json.dumps(event) + "\n" for event in capacity_rows()), encoding="utf-8")
        events = log.parent / "events.jsonl"
        events.unlink()
        events.symlink_to(elsewhere)
        self.assertEqual(self.classify(log), "permanent permanent")


class CriticAccessAtCapacity(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-279-access-")
        self.addCleanup(directory.cleanup)
        self.state = pathlib.Path(directory.name)

    def test_a_model_at_capacity_asks_for_a_rerun_not_another_account(self) -> None:
        result = t275.run_check(self.state, "--force", FAKE_ACCESS_SCENARIO="capacity", FAKE_CODEX_VERSION="0.160.1")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn(CAPACITY, result.stderr)
        self.assertIn("(throttling)", result.stderr)
        self.assertIn("run the check again", result.stderr)
        self.assertNotIn("cannot use the critic model", result.stderr)
        self.assertEqual(t275.stamp(self.state)["result"], "failed")
        self.assertEqual(t275.stamp(self.state)["error_class"], "throttling")
        self.assertEqual(t275.access_calls(self.state), 1)

    def replay(self, name: str, events: list[dict], stderr: str = "") -> subprocess.CompletedProcess[str]:
        """check_critic_access.py against tests/fake_codex.sh printing ``events`` and ``stderr``, then exit 1."""
        state = self.state / name
        rows, errors = state / "events.jsonl", state / "stderr.txt"
        state.mkdir()
        rows.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        errors.write_text(stderr, encoding="utf-8")
        return t275.run_check(state, "--force", FAKE_ACCESS_SCENARIO="replay", FAKE_ACCESS_EVENTS=str(rows),
                              FAKE_ACCESS_STDERR=str(errors), FAKE_CODEX_VERSION="0.160.1")

    def test_an_outage_with_the_websocket_fallback_warning_asks_for_a_rerun(self) -> None:
        for audit, (websocket_reason, reason, stderr) in OUTAGES.items():
            with self.subTest(outage=audit):
                result = self.replay(audit, outage_rows(websocket_reason, reason), stderr)
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertIn("(transport)", result.stderr)
                self.assertIn("run the check again", result.stderr)
                self.assertNotIn("cannot use the critic model", result.stderr)

    def test_a_refusal_after_an_overloaded_reconnect_names_the_account(self) -> None:
        for expected, reason in (("unsupported_model", REFUSALS["unsupported_model"]), ("quota", REFUSALS["quota"]),
                                 ("account_refusal", USAGE_LIMIT)):
            with self.subTest(expected=expected):
                result = self.replay(expected, refusal_after_overloaded_reconnect(reason))
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn(f"cannot use the critic model {t270.SOL}", result.stderr)
                self.assertIn(f"({expected})", result.stderr)
                self.assertEqual(t275.stamp(self.state / expected)["result"], "refused")


class CritiqueRoundAtCapacity(unittest.TestCase):
    """The real run_critique.sh: the completeness critic prints the policy, then the provider is at capacity."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-279-round-")
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()
        self.addCleanup(t270.thaw, self.root)
        self.audit = t270.write_audit(self.root / "audit", t270.POLICY_PATH, "20261026-audit-c0ffee02")
        self.state = self.root / "state"

    def drive(self, scenario: str) -> subprocess.CompletedProcess[str]:
        return t270.run_driver(self.audit, self.state, "20261026T120000Z.r1.1.1", scenario=scenario,
                               FAKE_TRANSIENT_ROLE="completeness")

    def completeness_rows(self) -> list[dict]:
        return [entry for entry in t270.ledger(self.audit) if entry["role"] == "critic_completeness"]

    def first_failure_log(self) -> str:
        logs = sorted(self.audit.glob("critique/round_1/invocations/*/attempts/completeness/1/failure.log"))
        self.assertEqual(len(logs), 1)
        return logs[0].read_text(encoding="utf-8")

    def test_a_critic_at_capacity_is_retried_and_the_round_completes(self) -> None:
        result = self.drive("capacity_once")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("critic_completeness throttling failure; retrying", result.stdout)
        self.assertIn("authentication", self.first_failure_log(), "the printed policy is in the log")
        self.assertEqual(t270.calls(self.state, "completeness"), 2)
        first, second = self.completeness_rows()
        self.assertEqual((first["attempt"], first["status"], first["error_class"]), (1, "failed", "throttling"))
        self.assertEqual(first["error"], f"transient: {CAPACITY}")
        self.assertEqual((second["attempt"], second["status"]), (2, "completed"))
        status = json.loads((self.audit / "critique/round_1/ROUND_STATUS.json").read_text(encoding="utf-8"))
        self.assertEqual(status["outcome"], "complete")
        t270.validate_ledger(self, self.audit)

    def test_a_refusal_past_row_200_of_the_session_is_retried(self) -> None:
        result = self.drive("capacity_late_once")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("critic_completeness throttling failure; retrying", result.stdout)
        log = self.first_failure_log()
        self.assertNotIn(CAPACITY, log, "failure.log stops at row 200 of the session")
        (events,) = self.audit.glob("critique/round_1/invocations/*/attempts/completeness/1/events.jsonl")
        self.assertIn(CAPACITY, events.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(t270.calls(self.state, "completeness"), 2)
        first, second = self.completeness_rows()
        self.assertEqual((first["attempt"], first["status"], first["error_class"]), (1, "failed", "throttling"))
        self.assertEqual((second["attempt"], second["status"]), (2, "completed"))
        status = json.loads((self.audit / "critique/round_1/ROUND_STATUS.json").read_text(encoding="utf-8"))
        self.assertEqual(status["outcome"], "complete")
        t270.validate_ledger(self, self.audit)

    def test_negative_control_an_unexplained_exit_stays_permanent_but_is_not_authentication(self) -> None:
        result = self.drive("transcript_once")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("authentication", self.first_failure_log())
        self.assertEqual(t270.calls(self.state, "completeness"), 1, "no retry: vague failures stay permanent")
        (first,) = self.completeness_rows()
        self.assertEqual((first["status"], first["error_class"]), ("failed", "permanent"))
        t270.validate_ledger(self, self.audit)


if __name__ == "__main__":
    unittest.main()
