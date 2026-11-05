#!/usr/bin/env python3
"""Whether an audit records Claude's thinking tokens as an invocation's reasoning_tokens (2.7.11).

The Claude CLI's result envelope reports thinking per served model (``modelUsage.<id>.thinkingTokens``) and, for the
primary path, at the top level (``usage.output_tokens_details.thinking_tokens``). Until 2.7.11 every Claude usage parser
(run_coordinator.extract_usage, also used by Phase 1 and the post-fan-out verifier, and run_phase1.usage_by_model)
looked for ``reasoningTokens``, a key the CLI never writes, so ``reasoning_tokens`` stayed null on every Claude row:
960 of the 1,048 Claude envelopes under runs/ on 2026-11-01 expose thinking (about 98 million tokens), and no Claude
invocation row records any. Thinking is a subset of ``outputTokens`` (958 of 958 per-model entries), so it never
enters a derived total.

The rule is frozen per audit, in the policy snapshot leaf ``metadata.claude_reasoning_tokens``:

- absent: the legacy parse. Every audit initialised before 2.7.11 froze no leaf; its rows, its Phase-1 unattributed
  buckets and the usage comparisons of recover_coordinator.py and recover_misplaced_coordinator_outputs.py stay
  byte for byte what 2.7.10 recorded. There is NO fallback to the packaged policy, and no old record is rewritten.
- ``THINKING``: reasoning_tokens is the envelope's thinking count, summed over modelUsage like every other component.

Stdlib only and imports nothing beyond json/pathlib, like claim_change_rules.py: the drift check loads it by path. The
post-delta receipt's CODE_PATHS files never import it.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any, Mapping

LEAF = ("metadata", "claude_reasoning_tokens")
THINKING = "thinking_tokens"
LIVE_VALUES: tuple[str, ...] = (THINKING,)
SHIPPED = THINKING


class RuleError(ValueError):
    pass


def rule_of(policy: Mapping[str, Any] | None) -> bool:
    """True when a policy freezes THINKING, False when it has no leaf; any other value is refused."""
    node: Any = policy
    for key in LEAF:
        if not isinstance(node, Mapping) or key not in node:
            return False
        node = node[key]
    if node not in LIVE_VALUES:
        raise RuleError(f"{'.'.join(LEAF)} is {node!r}, not a packaged value ({THINKING!r}, or no leaf for the "
                        "legacy parse)")
    return True


def frozen(root: pathlib.Path) -> bool:
    """The audit's rule, read ONLY from its own snapshot: False when there is none or it froze no leaf."""
    path = pathlib.Path(root) / "pipeline-policy.snapshot.json"
    if not path.exists():
        return False
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuleError(f"cannot read the audit policy snapshot pipeline-policy.snapshot.json: {exc}") from exc
    return rule_of(snapshot.get("policy") if isinstance(snapshot, Mapping) else None)
