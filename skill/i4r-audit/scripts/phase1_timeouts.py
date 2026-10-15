#!/usr/bin/env python3
"""The Phase-1 per-call provider timeouts an audit's frozen snapshot may name (2.7.2).

The timeout (``phase_1.runtime.timeout_seconds``) bounds every auditor and post-fan-out verifier call.
Unlike the probe bound (``probe_bounds.py``) it enters no cache key, role input identity or completion
receipt, but it is part of the exact ``phase_1.runtime`` contract that ``run_phase1.load_policy`` and
``run_verifier`` compare, and of the snapshot bytes ``run.json`` binds. So, like the probe bound, an
audit keeps the timeout it froze for its whole life, and the runtime admits exactly the values below:

- ``LEGACY_PHASE1_TIMEOUT`` (2,700 s, 45 minutes) is what every audit initialised before 2.7.2 froze.
  Such an audit gets the 2.7.1 contract byte for byte.
- ``SHIPPED_PHASE1_TIMEOUT`` (5,400 s, 90 minutes) is what a new audit freezes from
  ``config/pipeline-policy-v2.json``; ``config/check_policy_drift.py`` pins the policy to it and joins its
  own literal to this module.

Why 5,400 (2026-09-30, the first Sonnet 5.5/max audits):

- the Sonnet 5 auditors this cap was set for finished in 8 to 34 minutes;
- Sonnet 5.5/max auditors on a teammate's audit finished in about 20 to 35 minutes, but on
  audit A19 one role finished in 41 minutes (344,821 output tokens, USD 7.07) and the
  other four were still writing REPORT.md and result.json when 2,700 s stopped them, on two dispatches;
- 5,400 s is twice the old cap. The USD 15 per-call budget is unchanged and still bounds cost: at the observed
  rate of about USD 0.17 a minute it is reached at roughly 85 to 90 minutes, so the two limits bind at about
  the same point.

The runners never fall back to ``SHIPPED_PHASE1_TIMEOUT``: an audit's timeout comes from its own snapshot
(``run_phase1.phase1_timeout``), and a missing or unknown one is refused before any provider call.

Stdlib only, no imports: the drift check loads this file by path. The post-delta receipt's CODE_PATHS files
must never import it.
"""
from __future__ import annotations

from typing import Any

LEGACY_PHASE1_TIMEOUT = 2_700
SHIPPED_PHASE1_TIMEOUT = 5_400
# Every timeout a frozen snapshot may carry, oldest first; SHIPPED_PHASE1_TIMEOUT is the newest.
LIVE_PHASE1_TIMEOUTS: tuple[int, ...] = (LEGACY_PHASE1_TIMEOUT, SHIPPED_PHASE1_TIMEOUT)


def live_phase1_timeout(value: Any) -> int | None:
    """The LIVE_PHASE1_TIMEOUTS entry equal to a frozen ``value``, or None.

    Equality is 2.7.1's comparison (``run_phase1.load_policy`` compared the whole runtime dict with ``==``),
    so a value 2.7.1 admitted is admitted here and used as the packaged int. A bool is never a timeout.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return next((timeout for timeout in LIVE_PHASE1_TIMEOUTS if value == timeout), None)


def describe() -> str:
    """The live timeouts as the refusals print them: ``2700 or 5400``."""
    return " or ".join(str(timeout) for timeout in LIVE_PHASE1_TIMEOUTS)
