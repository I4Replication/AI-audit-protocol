#!/usr/bin/env python3
"""Project temporary claim joins from a draft Appendix A without promoting registry rows.

The canonical finding generator intentionally emits only approved final F-rows.  During an
adversarial round the registry still contains draft P-rows, but check_consistency requires the
working Appendix table and body claim joins to match.  This helper deterministically derives the
temporary join block from Appendix A; the canonical generator replaces it after promotion.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import tempfile


START = "<!-- I4R-GENERATED-CLAIM-TAGS:START -->"
END = "<!-- I4R-GENERATED-CLAIM-TAGS:END -->"


def atomic_write(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def project(text: str) -> str:
    if START in text or END in text:
        raise ValueError("report already contains a generated claim-tag block")
    appendix = re.search(r"(?m)^## Appendix A\. Summary of findings\s*$", text)
    if not appendix:
        raise ValueError("Appendix A heading is missing")
    rows = []
    for line in text[appendix.end():].splitlines():
        match = re.match(r"^\|\s*(\d+)\s*\|", line)
        if not match:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5:
            raise ValueError(f"Appendix row {match.group(1)} does not have five cells")
        rows.append((int(cells[0]), cells[1], cells[4]))
    numbers = [number for number, _, _ in rows]
    if numbers != list(range(1, len(rows) + 1)) or not rows:
        raise ValueError(f"Appendix numbers are not contiguous: {numbers}")
    body_sections = set(int(value) for value in re.findall(r"(?m)^##\s+(\d+)\.", text[:appendix.start()]))
    for number, _, section in rows:
        match = re.search(r"(?:Section|§)?\s*(\d+)", section, re.I)
        if not match or int(match.group(1)) not in body_sections:
            raise ValueError(f"Appendix row {number} points to a missing section: {section}")
    block = [START, "### Findings cross-reference", ""]
    block.extend(f"- [claim {number}] {section} — {finding}" for number, finding, section in rows)
    block.extend(["", END])
    return (
        text[:appendix.start()].rstrip()
        + "\n\n"
        + "\n".join(block)
        + "\n\n"
        + text[appendix.start():].lstrip()
    ).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    source = args.report.read_text()
    output = project(source)
    before = hashlib.sha256(source.encode()).hexdigest()
    after = hashlib.sha256(output.encode()).hexdigest()
    if args.apply:
        atomic_write(args.report, output)
    count = len(re.findall(r"^- \[claim \d+\]", output, re.M))
    print(f"draft claim tags: {count}; before={before}; after={after}; applied={args.apply}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
