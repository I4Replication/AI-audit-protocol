#!/usr/bin/env python3
"""Deterministic Claude CLI double for run_coordinator.py tests.

2.7.0: the fake serves the model the route asks for. ``--model sonnet`` (a Sonnet 5 audit, every
audit initialised before 2.7.0) is served ``claude-sonnet-5`` plus the Haiku helper, exactly what
every earlier fixture recorded; ``--model claude-sonnet-5-5`` (a Sonnet 5.5 audit) is served
``claude-sonnet-5-5`` plus the same helper. ``two_sonnet`` serves two ids of the route's own model
(the legacy pair unchanged). tests/test_270_claude_routes.py's logging wrapper renames the served
ids per ``I4R_TEST_SERVED``, a map keyed on the legacy ids; while that map names a rename, this fake
serves the legacy ids on every route, so the wrapper alone decides what the call was served.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

# (primary, a second id of the same model for two_sonnet) served for each --model spawned.
SERVED_BY_MODEL = {
    "sonnet": ("claude-sonnet-5", "claude-sonnet-5-20260801"),
    "claude-sonnet-5-5": ("claude-sonnet-5-5", "claude-sonnet-5-5-20260801"),
}
LEGACY_MODEL = "sonnet"
HELPER = "claude-haiku-4-5"
RENAME_MAP_ENV = "I4R_TEST_SERVED"


def served_ids(argv: list[str]) -> tuple[str, str]:
    """What this call is served: the route's own ids, or the legacy ids under a rename map."""
    if json.loads(os.environ.get(RENAME_MAP_ENV) or "{}") or "--model" not in argv:
        return SERVED_BY_MODEL[LEGACY_MODEL]
    index = argv.index("--model") + 1
    requested = argv[index] if index < len(argv) else LEGACY_MODEL
    return SERVED_BY_MODEL.get(requested, SERVED_BY_MODEL[LEGACY_MODEL])


def load_rows(source: pathlib.Path) -> list[dict]:
    rows = []
    for path in sorted((source / "draft").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def marker(row: dict) -> str:
    disposition = row["ledger_disposition"]
    if disposition["status"] == "INCLUDED":
        return f"<!-- finding:{row['id']} -->"
    if disposition["status"] == "EXCLUDED":
        return f"<!-- tested-negative:{row['id']} -->"
    return f"<!-- merged:{row['id']}->{disposition['target']} -->"


def write_outputs(cwd: pathlib.Path, rows: list[dict], scenario: str) -> None:
    markers = "\n".join(marker(row) for row in rows)
    report = f"""## Abstract

The supplied audit evidence identifies one bounded discrepancy while retaining all tested negative
results. The first draft preserves uncertainty until independent external review can confirm or
refute each candidate. No conclusion is treated as settled at this stage.

## 1. Introduction

This comment audits the deposited results through the supplied, hash-bound evidence. The protocol
follows AI-Games and the I4R AI-vertical. It separates numerical discrepancies, evidence limits, and
the consequence for each published statement. The analysis is intentionally limited to the frozen
coordinator snapshot and does not infer author intent.

## 2. What reproduces

The five area reports document checks that passed as well as candidate discrepancies. Passed checks
remain in the registry so later reviewers can distinguish a negative test from an untested claim.
This treatment prevents repeated work while keeping the evidence trail visible.

## 3. The supplied value requires reconciliation

{markers}

**What** — The deposited evidence does not reproduce one printed value. The exact source locus and
the observed comparison remain in the structured registry. **Evidence** — The canonical Phase-1
report and privacy-reviewed summary provide hash-bound records. **Impact** — The candidate concerns
a value and remains unpromoted pending independent review. **Remedy** — Regenerate the value from an
authoritative analysis source and reconcile it with the deposit.

A benign rounding explanation does not account for the bounded comparison described in the source
record. The coordinator therefore preserves the candidate without claiming more than the supplied
evidence establishes. This paragraph is deliberately substantive rather than process narration.

## 4. Minor issues

No additional minor issue is promoted from the supplied evidence. Any later correction must enter
the registry with a stable identifier, a source hash, and a rerunnable evidentiary basis.

## 5. Hypotheses tested and not confirmed

The supplied negative checks remain documented. A clean sub-check does not refute a distinct
candidate whose mechanism, evidence, and remedy differ.

## 6. Conclusion

The frozen evidence supports a bounded draft finding, but it does not yet support promotion as a
settled fact. Independent external review remains necessary before publication.

The audit establishes what the supplied records show and leaves unsupported causal or intentional
explanations unstated. The structured registry is authoritative for status and evidence.

## Data and Code Availability

The replication deposit and the audit's hash-bound verification materials are described in the
supplied audit context. Only workspace-relative paths are used in this comment.

## References

Brodeur et al. AI-Games reproducibility protocol. Institute for Replication AI-vertical protocol.

## Appendix A. Summary of findings

The generated publication projection will replace this prose after external review. The initial
draft retains the complete structured inventory in findings.jsonl.
"""
    adjudications = []
    for index, row in enumerate(rows, 1):
        adjudications.append(f"""## A{index} — {row['id']}
<!-- adjudication:{row['id']} -->
Sources: canonical Phase-1 report and draft registry row {row['id']}
Question: Does the supplied evidence justify the candidate as currently stated?
Checks: Compared the hash-bound report, safe evidence, and structured source record.
Decision: ACCEPTED AS DRAFT
Evidence: The evidence paths and hashes are retained without inventing independent confirmation.
Required propagation: Report section, registry, ledger, metadata, and verification plan.
""")
    adjudication = "# Coordinator adjudication\n\n" + "\n".join(adjudications)
    cwd.joinpath("REPORT_v1.md").write_text(report, encoding="utf-8")
    cwd.joinpath("ADJUDICATION.md").write_text(adjudication, encoding="utf-8")
    if scenario == "bad_findings":
        cwd.joinpath("findings.jsonl").write_text("{}\n", encoding="utf-8")
    else:
        cwd.joinpath("findings.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
    if scenario != "missing_output":
        cwd.joinpath("retired_claims.txt").write_text("# No retired claims.\n", encoding="utf-8")


def main() -> int:
    scenario = os.environ.get("FAKE_COORDINATOR_SCENARIO", "success")
    rejection_messages = {
        "authentication_rejection": "authentication failed before inference",
        "quota_rejection": "quota exhausted before inference",
        "schema_rejection": "deterministic schema rejected before inference",
        "capability_rejection": "unsupported capability before inference",
    }
    if scenario == "transport_then_success":
        # First call: the provider drops the connection mid-response (the exact shape of the
        # 39-minute failure on the sixth production audit). Every later call: success. The
        # counter lives in FAKE_COORDINATOR_STATE so the runner's in-invocation retry is what
        # gets tested, not a lucky second invocation.
        state = pathlib.Path(os.environ["FAKE_COORDINATOR_STATE"])
        calls = int(state.read_text().strip() or 0) if state.exists() else 0
        state.write_text(str(calls + 1))
        scenario = "transport_error" if calls == 0 else "success"
    if scenario in ("output_cap_then_success", "output_cap_always"):
        # 2.7.4: the first live Sonnet 5.5 coordinator call (20260930-audit-0ccfcb4e, attempt 1): the CLI
        # cut one response at its 64,000-token cap four times and failed the call, having written nothing.
        state = pathlib.Path(os.environ["FAKE_COORDINATOR_STATE"])
        calls = int(state.read_text().strip() or 0) if state.exists() else 0
        state.write_text(str(calls + 1))
        scenario = "output_cap_error" if calls == 0 or scenario == "output_cap_always" else "success"
    env_log = os.environ.get("FAKE_COORDINATOR_ENV_LOG")
    if env_log:
        with pathlib.Path(env_log).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"CLAUDE_CODE_MAX_OUTPUT_TOKENS":
                                     os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
                                     "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR":
                                     os.environ.get("CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR")}) + "\n")
    primary, second = served_ids(sys.argv[1:])
    cwd = pathlib.Path.cwd()
    source = (cwd / "../source").resolve()
    prompt = sys.stdin.read()
    cwd.joinpath("fake_args.json").write_text(
        json.dumps({"argv": sys.argv[1:], "prompt": prompt}, indent=2), encoding="utf-8"
    )
    if scenario == "crash_after_write":
        # 2.7.6: the model wrote into its workspace (fake_args.json above), then the CLI died with no result envelope
        return 1
    rows = load_rows(source)
    if scenario == "premature_state":
        rows[0]["coordinator_approved"] = True
        rows[0]["settled"] = {"approved": False, "valid_for_report_hash": None}
    if scenario == "output_cap_error":
        pass
    elif scenario == "misplaced_outputs":
        # Model the third live coordinator call: correct files, wrong directory. The runner
        # makes the attempt root read-only during the call, so this write must fail here and
        # the runner must reject the attempt without any stray file in the attempt root.
        write_outputs(cwd.parent, rows, "success")
    else:
        write_outputs(cwd, rows, scenario)
    mutate = os.environ.get("FAKE_COORDINATOR_MUTATE")
    if mutate:
        with pathlib.Path(mutate).open("a", encoding="utf-8") as handle:
            handle.write("\n")
    if scenario == "output_cap_error":
        # The attempt-1 envelope's shape, byte for byte where the runner reads it: subtype "success"
        # with is_error true, terminal_reason api_error, the CLI's own text, full usage, exit 1.
        cap = os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") or "64000"
        print(json.dumps({
            "type": "result", "subtype": "success", "is_error": True, "api_error_status": None,
            "duration_ms": 1979947, "duration_api_ms": 1979075, "num_turns": 27,
            "result": (f"API Error: Claude's response exceeded the {cap} output token maximum. To configure "
                       "this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable."),
            "stop_reason": "stop_sequence", "session_id": "fake-coordinator-session",
            "total_cost_usd": 7.10574525,
            "usage": {"input_tokens": 745, "cache_creation_input_tokens": 757925,
                      "cache_read_input_tokens": 928855, "output_tokens": 265509},
            "modelUsage": {primary: {"inputTokens": 745, "outputTokens": 265509,
                                     "cacheReadInputTokens": 928855, "cacheCreationInputTokens": 757925,
                                     "webSearchRequests": 0, "costUSD": 7.10574525,
                                     "contextWindow": 1000000, "maxOutputTokens": 64000}},
            "permission_denials": [], "terminal_reason": "api_error",
        }, separators=(",", ":")))
        return 1
    if scenario in rejection_messages or scenario == "no_served_model":
        model_usage = {}
    elif scenario == "wrong_model":
        model_usage = {
            "claude-opus-5": {
                "inputTokens": 100, "outputTokens": 40,
                "cacheReadInputTokens": 10, "cacheCreationInputTokens": 5,
            }
        }
    elif scenario == "two_sonnet":
        model_usage = {
            primary: {
                "inputTokens": 80, "outputTokens": 30,
                "cacheReadInputTokens": 10, "cacheCreationInputTokens": 5,
            },
            second: {
                "inputTokens": 20, "outputTokens": 10,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
            },
        }
    else:
        model_usage = {
            primary: {
                "inputTokens": 100, "outputTokens": 40,
                "cacheReadInputTokens": 10, "cacheCreationInputTokens": 5,
            },
            HELPER: {
                "inputTokens": 5, "outputTokens": 2,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
            },
        }
    if scenario == "partial_usage":
        for usage in model_usage.values():
            usage.pop("cacheReadInputTokens", None)
            usage.pop("cacheCreationInputTokens", None)
    rejected = scenario in rejection_messages
    provider_failed = scenario in ("provider_error", "transport_error")
    failure_text = {
        "transport_error": "API Error: Connection closed mid-response. The response above may be incomplete.",
    }
    result = {
        "type": "result",
        "subtype": "error" if provider_failed or rejected else "success",
        "is_error": provider_failed or rejected,
        "session_id": "fake-coordinator-session",
        "result": rejection_messages.get(scenario) or failure_text.get(scenario, "complete"),
        "modelUsage": model_usage,
        # Claude's top-level usage covers the primary route only.  The production runner must
        # ignore this object when per-model usage is available, or it will omit auxiliary tokens.
        "usage": {} if rejected else {
            "input_tokens": 100, "output_tokens": 40,
            "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5,
            "total_tokens": 155,
        },
        "num_turns": 0 if rejected else 2,
        "duration_api_ms": 0 if rejected else 1250,
        "total_cost_usd": None if rejected else 0.0125,
        "permission_denials": [],
    }
    print(json.dumps(result, separators=(",", ":")))
    return 1 if provider_failed or rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
