#!/usr/bin/env python3
"""The per-call critique provider ceilings an audit's snapshot may name (2.7.4).

``critique.provider_timeout_seconds`` bounds every Codex call run_critique.sh makes (the model probe, the
critics, a focused repair and the targeted adjudicator). Unlike the Phase-1 timeout, every audit initialised
before 2.7.4 froze NO such leaf, and run_critique.sh read the ceiling from I4R_PROVIDER_TIMEOUT_SECONDS
with a default of 2,700 s. So:

- a snapshot WITHOUT the leaf is a legacy audit: ceiling ``LEGACY_CRITIQUE_TIMEOUT`` (2,700), and the operator's
  I4R_PROVIDER_TIMEOUT_SECONDS is honoured exactly as before (it may raise or lower it);
- a snapshot WITH the leaf must carry a LIVE_CRITIQUE_TIMEOUTS value; I4R_PROVIDER_TIMEOUT_SECONDS may only lower
  it (tests, faster stall detection), never raise it.

Stdlib only, no imports beyond json/sys: the drift check loads this file by path.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Mapping

LEGACY_CRITIQUE_TIMEOUT = 2_700
SHIPPED_CRITIQUE_TIMEOUT = 5_400
LIVE_CRITIQUE_TIMEOUTS: tuple[int, ...] = (LEGACY_CRITIQUE_TIMEOUT, SHIPPED_CRITIQUE_TIMEOUT)
LEAF = "provider_timeout_seconds"


class CritiqueTimeoutError(ValueError):
    pass


def live_critique_timeout(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return next((timeout for timeout in LIVE_CRITIQUE_TIMEOUTS if value == timeout), None)


def describe() -> str:
    return " or ".join(str(timeout) for timeout in LIVE_CRITIQUE_TIMEOUTS)


def frozen_ceiling(policy: Mapping[str, Any]) -> tuple[int, bool]:
    """(ceiling, frozen). frozen is False for a legacy policy that has no leaf."""
    critique = policy.get("critique") if isinstance(policy, Mapping) else None
    if not isinstance(critique, Mapping) or LEAF not in critique:
        return LEGACY_CRITIQUE_TIMEOUT, False
    found = live_critique_timeout(critique[LEAF])
    if found is None:
        raise CritiqueTimeoutError(
            f"critique.{LEAF} {critique[LEAF]!r} is not a packaged critique timeout ({describe()})")
    return found, True


def effective(policy: Mapping[str, Any], override: str | None) -> int:
    ceiling, frozen = frozen_ceiling(policy)
    if override is None or override == "":
        return ceiling
    if not override.isdigit() or int(override) < 1:
        raise CritiqueTimeoutError(
            f"I4R_PROVIDER_TIMEOUT_SECONDS must be a positive integer number of seconds (got {override!r})")
    value = int(override)
    if frozen and value > ceiling:
        raise CritiqueTimeoutError(
            f"I4R_PROVIDER_TIMEOUT_SECONDS={value} exceeds this audit's frozen critique ceiling of {ceiling} s "
            f"(critique.{LEAF} in its pipeline-policy.snapshot.json); it may only lower it")
    return value


def main(argv: list[str]) -> int:
    # usage: critique_timeouts.py <snapshot-or-policy.json> [override]
    path = argv[1]
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2
    policy = data.get("policy", data) if isinstance(data, dict) and "snapshot_version" in data else data
    try:
        print(effective(policy, argv[2] if len(argv) > 2 else None))
    except CritiqueTimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
