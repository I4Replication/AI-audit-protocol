#!/usr/bin/env python3
"""os.replace, retried while a Windows program holds the file (2.7.9).

On WSL2 an audit tree under /mnt/<drive>/ sits on DrvFs, so every rename is a Windows rename. Windows refuses one
(STATUS_ACCESS_DENIED, seen here as EACCES) while another process holds a handle inside a directory being moved or
on a file being replaced without FILE_SHARE_DELETE, and the Windows Dropbox client or an antivirus scanner opens new
files seconds after they are written. Errno 13 paused 5 of one teammate's 9 WSL audits (one ten times,
another fifteen, mostly on a one-shot rename) and none of 90 macOS audits. The refusal clears once the other
process lets go, so a short bounded retry is all it takes; elsewhere a refused rename fails after the same delays.

Stdlib only: lifecycle_journal imports it on every journal write.
"""
from __future__ import annotations

import errno
import os
import pathlib
import platform
import re
import time
from typing import Any

# Seconds slept before each retry: eight tries in all, at most 15.75 s.
REPLACE_RETRY_DELAYS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 4.0, 4.0)
_TRANSIENT = frozenset({errno.EACCES, errno.EPERM})
_WINDOWS_DRIVE = re.compile(r"^/mnt/([A-Za-z])/")


def wsl_kernel() -> bool:
    """True on a WSL kernel ('microsoft' in its release, or in /proc/version)."""
    if "microsoft" in platform.release().lower():
        return True
    try:
        return "microsoft" in pathlib.Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def wsl_diagnosis(*paths: Any) -> str | None:
    """The one-line remedy for a rename refused under /mnt/<drive>/ on WSL, else None."""
    drive = next((match.group(1) for match in (_WINDOWS_DRIVE.match(os.fspath(path)) for path in paths
                                               if path is not None) if match), None)
    if drive is None or not wsl_kernel():
        return None
    return (f"on WSL the audit is on the Windows drive /mnt/{drive.lower()}, where a Windows program holding a file "
            "open (the Dropbox or OneDrive client, or an antivirus scan) refuses a rename: pause syncing, or "
            "exclude the folder from scanning, for the minute of the preflight, then re-run")


def replace_with_retry(source: Any, destination: Any, deadline: Any = None) -> None:
    """os.replace(source, destination), retrying a PermissionError (EACCES or EPERM) after each of
    REPLACE_RETRY_DELAYS. ``deadline`` (a phase1_runtime.MonotonicDeadline) is never slept past: the retry stops
    early instead. The last refusal is re-raised; on WSL under /mnt/<drive>/ its message carries the remedy."""
    for delay in (*REPLACE_RETRY_DELAYS, None):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if exc.errno not in _TRANSIENT or delay is None or (
                    deadline is not None and delay >= deadline.expires_at - time.monotonic()):
                diagnosis = wsl_diagnosis(source, destination)
                if diagnosis is None:
                    raise
                raise PermissionError(exc.errno, f"{exc.strerror} ({diagnosis})", exc.filename, None,
                                      exc.filename2) from exc
        time.sleep(delay)
