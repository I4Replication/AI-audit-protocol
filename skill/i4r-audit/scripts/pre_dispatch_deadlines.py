#!/usr/bin/env python3
"""The Phase-1 pre-dispatch deadline rule an audit's frozen snapshot may name (2.7.9).

The deadline bounds the provider-free preparation before any auditor or verifier call: hashing, copying and
freezing the deposit, the deterministic evidence scans and the sandbox preflight, under one SIGALRM. Until 2.7.9
it was ``phase_1.runtime.pre_dispatch_deadline_seconds`` (60 s) for every audit, whatever the size of its deposit.
Like the critique ceiling (``critique_timeouts.py``), it is a leaf that every audit initialised before 2.7.9 never
froze, ``phase_1.runtime.pre_dispatch_deadline_scaling``. So:

- a snapshot WITHOUT the leaf is a legacy audit: exactly ``pre_dispatch_deadline_seconds`` (60 s), the 2.7.8
  contract byte for byte;
- a snapshot WITH the leaf must carry a LIVE_PRE_DISPATCH_SCALINGS value. The deadline is then
  min(max_seconds, 60 + package bytes / package_bytes_per_second + package files / package_files_per_second),
  the bytes and files from the audit's own hash-bound ``package_manifest.json``, whose every size the preflight
  checks against the deposit inside the deadline.

``--pre-dispatch-deadline-seconds`` may still only tighten it.

Why these values (2026-10-26, the preflight receipts under runs/ and old_runs/; 2.7.8 read each large file about
seven times and copied it once):

- a teammate's WSL2 host, the tree on /mnt/c under the Windows Dropbox client: one deposit of a few large files took
  close to a minute, and so did another of a few hundred small ones, so both bytes (about 0.09 s/MB) and files (about
  0.2 s each) cost there; the smallest deposits took 6 to 19 s (CLI and tool version probes, paper text). A third,
  larger deposit paused just past 60 s and cannot pass 60 s on that host;
- macOS hosts did several hundred MB in a few seconds;
- 10,000,000 bytes and 5 files a second allow 0.1 s/MB and 0.2 s a file, at least what 2.7.8 cost on the WSL host,
  so the margin is about twice the cost now that 2.7.9 reads a large file four times instead of seven (that third
  deposit gets a deadline of a few minutes, where 2.7.9 should need about a minute); 600 s bounds a deposit of
  several gigabytes, or a filesystem that hangs.

Stdlib only, no imports: the drift check loads this file by path. The post-delta receipt's CODE_PATHS files must
never import it.
"""
from __future__ import annotations

from typing import Any, Mapping

LEAF = "pre_dispatch_deadline_scaling"
SHIPPED_PRE_DISPATCH_SCALING: dict[str, int] = {
    "package_bytes_per_second": 10_000_000,
    "package_files_per_second": 5,
    "max_seconds": 600,
}
# Every rule a frozen snapshot may carry, oldest first; SHIPPED_PRE_DISPATCH_SCALING is the newest. A snapshot
# without the leaf is the legacy fixed deadline and needs no entry here.
LIVE_PRE_DISPATCH_SCALINGS: tuple[dict[str, int], ...] = (SHIPPED_PRE_DISPATCH_SCALING,)
# The largest deadline any live rule gives.
MAXIMUM_PRE_DISPATCH_SECONDS = max(rule["max_seconds"] for rule in LIVE_PRE_DISPATCH_SCALINGS)


class PreDispatchScalingError(ValueError):
    pass


def live_scaling(value: Any) -> dict[str, int] | None:
    """The LIVE_PRE_DISPATCH_SCALINGS entry equal to a frozen ``value``, key for key, or None. A bool is never a
    number here (JSON true == 1 in Python)."""
    if not isinstance(value, Mapping) or any(isinstance(item, bool) for item in value.values()):
        return None
    return next((rule for rule in LIVE_PRE_DISPATCH_SCALINGS if dict(value) == rule), None)


def describe() -> str:
    return " or ".join(
        f"{rule['package_bytes_per_second']} bytes and {rule['package_files_per_second']} files a second, "
        f"at most {rule['max_seconds']} s" for rule in LIVE_PRE_DISPATCH_SCALINGS)


def frozen_scaling(runtime: Mapping[str, Any]) -> dict[str, int] | None:
    """The rule a ``phase_1.runtime`` section froze; None for a legacy section, which has no leaf."""
    if not isinstance(runtime, Mapping) or LEAF not in runtime:
        return None
    found = live_scaling(runtime[LEAF])
    if found is None:
        raise PreDispatchScalingError(
            f"its {LEAF} {runtime[LEAF]!r} is not a packaged pre-dispatch rule ({describe()})")
    return found


def deadline_seconds(
    scaling: Mapping[str, int] | None, base: int, package_bytes: int, package_files: int,
) -> float:
    """The pre-dispatch deadline for a deposit of ``package_bytes`` in ``package_files`` files: ``base`` itself
    for a legacy audit (``scaling`` None), else the scaled value, capped, to the millisecond.

    Never raises: it runs before the deadline is armed. An extent that alone reaches the cap (a corrupted
    manifest's 10**400 bytes, say, which a float division cannot take) gets the cap, as the formula would."""
    if scaling is None:
        return base
    ceiling = scaling["max_seconds"]
    package_bytes, package_files = max(0, package_bytes), max(0, package_files)
    if (package_bytes >= ceiling * scaling["package_bytes_per_second"]
            or package_files >= ceiling * scaling["package_files_per_second"]):
        return float(ceiling)
    scaled = (base + package_bytes / scaling["package_bytes_per_second"]
              + package_files / scaling["package_files_per_second"])
    return round(min(float(ceiling), scaled), 3)
