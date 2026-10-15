"""Exclusive fcntl locks that say so when they have to wait.

Several pipeline locks are legitimately held for a long time (a Phase-1 run holds its lock for the
whole fan-out), so they must keep waiting rather than time out like the journal lock. What they must
not do is wait silently: an operator whose command blocks with no output cannot tell a busy build
from a stuck one. This helper tries once without blocking and, if the lock is taken, prints one line
naming the lock before waiting as before. An fcntl lock is released when its holder process exits.
"""
from __future__ import annotations

import fcntl
import sys
from typing import IO, Any


def acquire_exclusive(handle: IO[Any], label: str) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    except BlockingIOError:
        pass
    name = getattr(handle, "name", "?")
    print(f"waiting: {label} ({name}) is held by another process; this command continues when that "
          "process finishes or exits (find it with: lsof " + str(name) + ")", file=sys.stderr, flush=True)
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
