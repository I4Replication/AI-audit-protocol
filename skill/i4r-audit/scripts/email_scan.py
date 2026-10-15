#!/usr/bin/env python3
"""Linear-time e-mail matching for the privacy scans (2.7.6).

The e-mail patterns of phase1_runtime.py and check_release_privacy.py start with an unbounded
``\\b[A-Z0-9._%+-]+@``. On a long run of those characters with word boundaries inside it and no '@' (the
percent-encoded data and dotted tokens of a self-contained knitted HTML report) every boundary is a start
position that consumes the run and backtracks, so one 289 KB file took 61 s and the Beyond Rubik audit
could never pass the 60 s Phase-1 preflight.

Every match of those patterns holds exactly one '@'. finditer() runs the unchanged pattern only in a
window around each '@' and yields the first match that covers it, so the matches are those of
``pattern.finditer(text)`` (for any address whose local part and domain are under REACH characters, i.e.
every real one) and the cost is linear in the text.
"""
from __future__ import annotations

import re
from typing import Iterator

REACH = 320


def finditer(pattern: re.Pattern[str], text: str, reach: int = REACH) -> Iterator[re.Match[str]]:
    resume = 0
    at = text.find("@")
    while at != -1:
        start, end = max(resume, at - reach), min(len(text), at + reach)
        for match in pattern.finditer(text, start, end):
            if match.start() <= at < match.end():
                yield match
                resume = match.end()
                break
        at = text.find("@", max(at + 1, resume))


def findall(pattern: re.Pattern[str], text: str) -> list[str]:
    return [match.group(0) for match in finditer(pattern, text)]
