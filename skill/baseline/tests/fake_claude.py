#!/usr/bin/env python3
"""A stand-in for the Claude CLI: never calls a model. FAKE_MODE picks the behaviour; FAKE_LOG records argv and stdin."""
import json
import os
import pathlib
import sys
import time

MODE = os.environ.get("FAKE_MODE", "good")
FIVE_MINUTE = MODE == "cache_5m"   # a good run whose cache was written for 5 minutes
if FIVE_MINUTE:
    MODE = "good"
if "--version" in sys.argv:
    print(os.environ.get("FAKE_VERSION", "2.1.293 (Claude Code)"))
    sys.exit(0)
prompt = sys.stdin.read()
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as handle:
        add = sys.argv[sys.argv.index("--add-dir") + 1:sys.argv.index("--add-dir") + 3] if "--add-dir" in sys.argv else []
        handle.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd(), "pid": os.getpid(),
                                 "env_cap": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
                                 "env_model": os.environ.get("ANTHROPIC_MODEL"),
                                 "env_sonnet": os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                                 "env_ttl": os.environ.get("CLAUDE_CODE_PROMPT_CACHE_TTL"),
                                 "env_linked": sorted(k for k in ("CLAUDE_CODE_MESSAGING_SOCKET",
                                                                  "CLAUDE_CODE_MESSAGING_TOKEN",
                                                                  "CLAUDE_CODE_USER_EMAIL",
                                                                  "FORCE_PROMPT_CACHING_5M") if k in os.environ),
                                 "packet_writable": [os.access(d, os.W_OK) for d in add]}) + "\n")
out = pathlib.Path.cwd()
if prompt.startswith("This is a sandbox check"):
    (out / "probe.json").write_text(json.dumps({"read_input": True, "read_saved_output": True, "r": "R version 4.5.2",
                                                "python": "Python 3.12",
                                                "denied_read_refused": MODE != "probe_denied_readable",
                                                "bash_denied_refused": MODE != "probe_bash_leak"}))
    bodies = os.environ.get("OTEL_LOG_RAW_API_BODIES", "")
    if bodies.startswith("file:") and MODE != "probe_no_bodies":
        cap = 64000 if MODE == "probe_no_cap" else int(os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "64000"))
        pathlib.Path(bodies[5:], "req-1.json").write_text(json.dumps(
            {"model": "claude-sonnet-5-5", "max_tokens": cap, "messages": [{"role": "user", "content": "x"}],
             "tools": [{"name": "Bash"}, {"name": "Read"}, {"type": "advisor_20260301", "name": "advisor"}]}))
        if MODE == "probe_mixed_cap":
            pathlib.Path(bodies[5:], "req-3.json").write_text(json.dumps({"model": "claude-sonnet-5-5", "max_tokens": 64000}))
        pathlib.Path(bodies[5:], "req-2.json").write_text(json.dumps(   # a helper call under its own cap
            {"model": "claude-haiku-5-5", "max_tokens": 32000, "messages": [{"role": "user", "content": "title"}]}))
    ttl = "5m" if MODE == "probe_5m_cache" else os.environ.get("CLAUDE_CODE_PROMPT_CACHE_TTL", "5m")
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "DONE", "num_turns": 7,
                      "usage": {"cache_creation": {"ephemeral_5m_input_tokens": 900 if ttl == "5m" else 0,
                                                   "ephemeral_1h_input_tokens": 900 if ttl == "1h" else 0}},
                      "total_cost_usd": 0.12, "modelUsage": {"claude-sonnet-5-5": {
                          "inputTokens": 1, "outputTokens": 1, "maxOutputTokens": 64000,
                          "contextWindow": 200000 if MODE == "probe_small_window" else 1000000}}}))
    sys.exit(0)

REPORT = """## Abstract

The deposited data reproduce the headline estimate exactly. One reported value in Table 2 does not match the deposit,
and no published conclusion changes.

## 1. Introduction

This comment was produced under the Institute for Replication's AI-assisted reproducibility protocol
([Brodeur et al., 2026](#ref-AI-Games); [Institute for Replication, 2026](#ref-AI-vertical)).

## 2. What reproduces

The main model reproduces at the precision printed.

## 3. Table 2 reports a mean that the deposit does not yield

<!-- finding:F1 -->

**What**: the mean in Table 2 differs from the deposit. **Evidence**: evidence/check.py. **Impact**: None.
**Remedy**: correct the value.

## 4. Minor issues

None beyond the above.

## 5. Hypotheses tested and not confirmed

Duplicate rows were tested and not found.

## 6. Conclusion

The paper's conclusions stand.

## Data and Code Availability

The scripts are in evidence/.

## References

[]{#ref-AI-Games}Brodeur, A., et al. (2026). AI-Games.

[]{#ref-AI-vertical}Institute for Replication (2026). AI-vertical.

## Appendix A. Summary of findings

| # | Finding | Status | Changes a published conclusion? | Section |
|---|---|---|---|---|
| 1 | Table 2 mean differs from the deposit | CONFIRMED | No | Section 3 |
"""
FRONT = """::: {custom-style="Title"}
**A comment on "A test paper"**
:::
::: {custom-style="Affil"}
**Original study:** Author, A. (2026). A test paper. DOI: 10.0/test
:::
::: {custom-style="Affil"}
**Replication package:** OSF, one CSV file
:::
"""
FINDINGS = {
    "schema_version": "i4r-baseline-findings-1",
    "package": {"data_present": True, "code_present": False, "runs_as_shipped": "not_attempted",
                "reproduces_headline": "exact"},
    "findings": [{
        "id": "F1", "claim": "Table 2 mean differs from the deposit", "section": "Section 3", "confidence": "CONFIRMED",
        "impact": "value_wrong", "changes_conclusion": False, "changes_conclusion_which": None, "disclosure_flag": False,
        "remedy_class": "one_number", "remedy_description": "Correct the value.", "verification_tier": 2,
        "remedy_verified": True, "paper_only": False, "check_nature": "deterministic", "unverifiable_reason": None,
        "blocked_tier": None, "unverifiable_basis": None, "locus": "reporting",
        "evidence": {"artifact": "pkg/data.csv", "operation": "mean of x", "expected": "2.0", "observed": "2.1",
                     "script": "evidence/check.py"},
        "benign_explanations_ruled_out": ["rounding"]}],
    "tested_negatives": [{"hypothesis": "duplicate rows", "result": "none"}],
    "passed_checks": ["headline estimate"],
}


def envelope(**extra):
    ttl = "5m" if FIVE_MINUTE else os.environ.get("CLAUDE_CODE_PROMPT_CACHE_TTL", "5m")
    base = {"type": "result", "subtype": "success", "is_error": False, "result": "Done.", "session_id": "s",
            "usage": {"cache_creation": {"ephemeral_5m_input_tokens": 90000 if ttl == "5m" else 0,
                                         "ephemeral_1h_input_tokens": 90000 if ttl == "1h" else 0}},
            "total_cost_usd": 12.5, "num_turns": 31, "duration_ms": 90000, "duration_api_ms": 80000,
            "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 1000, "outputTokens": 50000,
                                                 "cacheReadInputTokens": 2000000, "cacheCreationInputTokens": 90000,
                                                 "thinkingTokens": 7000, "costUSD": 12.5, "contextWindow": 1000000}}}
    base.update(extra)
    return base


if MODE == "transient":
    sys.stderr.write("API Error: 529 overloaded\n")
    sys.exit(1)
if MODE == "transient_wrote":
    (out / "notes.txt").write_text("started\n")
    sys.stderr.write("API Error: 529 overloaded\n")
    sys.exit(1)
if MODE == "helper":
    env = envelope()
    env["modelUsage"]["claude-haiku-4-5"] = {"inputTokens": 10, "outputTokens": 5, "cacheReadInputTokens": 0,
                                             "cacheCreationInputTokens": 0, "costUSD": 0.01}
    MODE = "good"
    HELPER_ENVELOPE = env
if MODE == "budget_wrong_model":
    env = envelope(is_error=True, subtype="error_max_budget_usd", terminal_reason="budget_exhausted", result=None,
                   total_cost_usd=30.02)
    env["modelUsage"] = {"claude-sonnet-5": env["modelUsage"]["claude-sonnet-5-5"]}
    print(json.dumps(env))
    sys.exit(1)
if MODE == "login_empty":
    print(json.dumps(envelope(is_error=True, subtype="success", result="Invalid API key \u00b7 Please run /login",
                              modelUsage={}, total_cost_usd=0, num_turns=1)))
    sys.exit(1)
if MODE == "weekly_limit":
    print(json.dumps(envelope(is_error=True, subtype="success", api_error_status=429, total_cost_usd=4.0,
                              result="You've hit your weekly limit \u00b7 resets Mon 9am")))
    sys.exit(1)
if MODE == "overloaded_pre":
    print(json.dumps(envelope(is_error=True, subtype="success", api_error_status=529, modelUsage={}, total_cost_usd=0,
                              num_turns=0, result="API Error: 529 Overloaded")))
    sys.exit(1)
if MODE == "transient_after_spend":
    print(json.dumps(envelope(is_error=True, subtype="error_during_execution", result="API Error: 529 overloaded",
                              total_cost_usd=25.0, num_turns=40)))
    sys.exit(1)
if MODE == "session_limit":
    print(json.dumps(envelope(is_error=True, subtype="success", api_error_status=429,
                              result="You've hit your session limit \u00b7 resets 6:20pm", total_cost_usd=0, num_turns=0)))
    sys.exit(1)
if MODE == "wrong_model":
    env = envelope()
    env["modelUsage"] = {"claude-sonnet-5": env["modelUsage"]["claude-sonnet-5-5"]}
    print(json.dumps(env))
    sys.exit(0)
if MODE == "slow":
    time.sleep(30)
if MODE == "error":
    print(json.dumps(envelope(is_error=True, subtype="error_during_execution", result="authentication failed")))
    sys.exit(1)
if MODE == "small_window":
    env = envelope()
    env["modelUsage"]["claude-sonnet-5-5"]["contextWindow"] = 200000
    print(json.dumps(env))
    sys.exit(0)
if MODE == "two_models":
    print(json.dumps(envelope(modelUsage={"claude-sonnet-5-5": {"inputTokens": 1, "outputTokens": 1,
                                                                 "cacheReadInputTokens": 1, "cacheCreationInputTokens": 1},
                                           "claude-sonnet-5": {"inputTokens": 1, "outputTokens": 1,
                                                               "cacheReadInputTokens": 1, "cacheCreationInputTokens": 1}})))
    sys.exit(0)
(out / "evidence").mkdir(exist_ok=True)
(out / "evidence" / "check.py").write_text("print('mean')\n")
(out / "REPORT_final.md").write_text(REPORT)
(out / "front_matter.md").write_text(FRONT)
(out / "captions.txt").write_text("Summary of findings\n")
findings = dict(FINDINGS)
if MODE == "invalid":
    findings = {"schema_version": "wrong"}
(out / "findings.json").write_text(json.dumps(findings))
if MODE == "pii":
    (out / "evidence" / "rows.csv").write_text("id,score\nSUBJ_P12,3.4\n")
if MODE == "report_pii":
    (out / "REPORT_final.md").write_text(REPORT.replace("Duplicate rows were tested", "SUBJ_P12 rows were tested"))
if MODE == "binary_pii":
    (out / "evidence" / "rows.csv.gz").write_bytes(b"\x1f\x8b fake")
    (out / "evidence" / "rows.rds").write_bytes(b"X\n\x00 fake")
if MODE == "symlink":
    os.symlink(os.environ["FAKE_LINK_TARGET"], out / "evidence" / "notes.txt")
if MODE == "non_utf8":
    (out / "front_matter.md").write_bytes(FRONT.replace("A test paper", "Caf\u00e9").encode("cp1252"))
if MODE == "denied_write":
    HELPER_ENVELOPE = envelope(permission_denials=[{"tool_name": "Write", "tool_use_id": "t1",
                                                    "tool_input": {"file_path": "/x", "content": "SUBJ_P12,3.4\n"}}])
if MODE == "svg_pii":
    (out / "evidence" / "fig.svg").write_text("<svg><text>SUBJ_P12</text></svg>")
if MODE == "sh_ok":
    (out / "evidence" / "run_all.sh").write_text("python3 check.py\n")
if MODE == "long_csv":
    (out / "evidence" / "table.csv").write_text("x\n" + "".join(f"{i}\n" for i in range(300)))
if MODE == "budget":
    print(json.dumps(envelope(is_error=True, subtype="error_max_budget_usd", terminal_reason="budget_exhausted",
                              result=None, total_cost_usd=30.02)))
    sys.exit(1)
print(json.dumps(globals().get("HELPER_ENVELOPE") or envelope()))
