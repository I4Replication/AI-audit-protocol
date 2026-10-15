#!/usr/bin/env python3
"""Write the hash-bound Round-2 scope note for the active Fessi audit."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import tempfile


EXPECTED = {
    "report/REPORT_final.md": "6cb407be625be571bf8399f8dbdf862dee44624bf9687158303a86c8ac42c69b",
    "work/findings.jsonl": "77dfbe2175b92591efe094b37f25786cf9f06f553c18426ed3d919c36d46032e",
    "critique/DIFF_MANIFEST_round_2.json": "d9218e421f2bf425b91529355c1e9be4d8b6c5a474c06bf1d90f3fbc6ea755ac",
    "SETTLED.md": "696ce538fc21e8569306426019f02153d260a0c81f14175a8af9caacd87c8d57",
}

TEXT = """Round 2 of the required target of 2 critique rounds (hard cap 5).

This is a content-bound delta review. Read critique/DIFF_MANIFEST_round_2.json and inspect every changed claim, changed report section, listed dependency, and whole-document consistency/privacy surface. The settled-facts cache contains zero promoted facts; do not treat a cache hash as scientific confirmation.

Changed draft IDs, exact order: P1, P13, P16, P19, P2, P20, P5, P6.

Required specialist scope for this multi-domain change set:

- data: P1, P2, P19, and P20, including exact recomputation from the bound safe evidence;
- stats: P5, P6, and P16, including the one-/two-sided qualification, mixed-ANOVA claim, and power-input interpretation;
- fairness: P13 plus every neutral-provenance, privacy-remedy, attribution, and release-safety edit;
- completeness: every changed ID, Abstract/Conclusion/Appendix agreement, section order, evidence bindings, sanitized Phase-1 report quorum, and global joins.

Round-1 targeted adjudication for P1, P2, P20, P5, and P6 was inconclusive because its reduced packet contained zero bound claim-evidence records. That result was not a substantive refutation. Round 2 must give each of those five IDs an explicit claim-specific verdict using the newly bound evidence.

Changed report surfaces: Abstract; Sections 1, 4-7, 15, 17, 21-23; References; Findings cross-reference; Appendix A; removal of the standalone Data and Code Availability section.

Report SHA-256: 6cb407be625be571bf8399f8dbdf862dee44624bf9687158303a86c8ac42c69b
Findings-registry SHA-256: 77dfbe2175b92591efe094b37f25786cf9f06f553c18426ed3d919c36d46032e
Diff-manifest SHA-256: d9218e421f2bf425b91529355c1e9be4d8b6c5a474c06bf1d90f3fbc6ea755ac
Settled-facts SHA-256: 696ce538fc21e8569306426019f02153d260a0c81f14175a8af9caacd87c8d57

If every changed ID is re-reviewed and no blocking or conclusion-critical item remains, the required target is complete. A further round is required only if this review causes another substantive correction or leaves an unresolved critical item.
"""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.audit_dir.resolve()
    for relative, expected in EXPECTED.items():
        path = root / relative
        if not path.is_file() or digest(path) != expected:
            raise SystemExit(f"stale or missing Round-2 input: {relative}")
    before = digest(root / "WRAPUP.txt")
    after = hashlib.sha256(TEXT.encode()).hexdigest()
    if args.apply:
        atomic_write(root / "WRAPUP.txt", TEXT)
    print(f"WRAPUP before={before}; after={after}; applied={args.apply}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
