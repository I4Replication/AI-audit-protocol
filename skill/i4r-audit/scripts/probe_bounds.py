#!/usr/bin/env python3
"""The Phase-1 capability-probe token bounds an audit's frozen snapshot may name (2.7.1).

The bound (``phase_1.runtime.probe_max_normalized_tokens``) is part of the probe contract, and the
contract is hashed into the probe cache key and into every auditor's and verifier's input identity.
So, like its Claude route (``claude_routes.py``), an audit keeps the bound it froze for its whole
life, and the runtime admits exactly the values below, following the ``validate_v2.LIVE_ROUND_CAPS``
precedent:

- ``LEGACY_PROBE_BOUND`` (150,000 normalized tokens) is what every audit initialised before 2.7.1
  froze. Such an audit gets the 2.7.0 probe contract, probe key, portable probe key and
  ``PROBE_CONTRACT.json`` bytes, so nothing it cached or completed is re-keyed.
- ``SHIPPED_PROBE_BOUND`` (350,000) is what a new audit freezes from ``config/pipeline-policy-v2.json``;
  ``config/check_policy_drift.py`` pins the policy to it and joins its own literal to this module.

Why 350,000 (the invocation ledgers under runs/, as of 2026-09-30T09:02Z: 39 probe calls in 28 audits, cache
replays not counted; run_phase1.py's bound comment says the same):

- passed: 26, one per audit, 67,830 to 149,259 normalized tokens (median 122,621): 25 on Sonnet 5 (median
  122,675) and 1 on Sonnet 5.5 (93,846, an audit started on 2.7.0); the Sonnet 5.5 live canary used 124,283;
- over 150,000: 11, in 8 audits, at 160,004 to 254,191 (8 to 11 turns), 3 of which also failed the
  probe-contract check (2 more failures were "Not logged in", at 0 tokens);
- retries: all 8 audits passed on a re-run probe after 1 to 3 over-bound ones.

The programme lead chose 350,000: about 1.4 times the worst probe observed (254,191) and about 2.3 times the
worst passing one (149,259). None of the 11 would have failed on tokens, and it still bounds a probe that loops
on tool calls: the largest probes averaged about 23,000 normalized tokens a turn (254,191 in 11 turns), so one
that keeps calling tools passes 350,000 at roughly 13 to 15 turns, where a passing probe takes 5 to 8.
300,000 and 500,000 were considered and not shipped; no audit froze either, so neither is a live bound.

The runners never fall back to ``SHIPPED_PROBE_BOUND``: an audit's bound comes from its own snapshot
(``run_phase1.probe_bound``), and a missing or unknown one is refused before any provider call.

Stdlib only, no imports: the drift check loads this file by path. The post-delta receipt's
CODE_PATHS files must never import it.
"""
from __future__ import annotations

from typing import Any

LEGACY_PROBE_BOUND = 150_000
SHIPPED_PROBE_BOUND = 350_000
# Every bound a frozen snapshot may carry, oldest first; SHIPPED_PROBE_BOUND is the newest.
LIVE_PROBE_BOUNDS: tuple[int, ...] = (LEGACY_PROBE_BOUND, SHIPPED_PROBE_BOUND)


def live_probe_bound(value: Any) -> int | None:
    """The LIVE_PROBE_BOUNDS entry equal to a frozen ``value``, or None.

    Equality is 2.7.0's comparison (``run_phase1.load_policy`` compared the whole runtime dict with
    ``==``), so a value 2.7.0 admitted is admitted here and keyed with the packaged int. A bool is
    never a bound.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return next((bound for bound in LIVE_PROBE_BOUNDS if value == bound), None)


def describe() -> str:
    """The live bounds as the refusals print them: ``150000 or 350000``."""
    return " or ".join(str(bound) for bound in LIVE_PROBE_BOUNDS)
