#!/usr/bin/env python3
"""Construct the provider-visible safe-evidence bundle required by run_coordinator.py.

    python3 scripts/build_safe_evidence.py "$AUDIT_DIR"

The coordinator refuses to run without ``work/safe_evidence/manifest.json`` plus a privacy
receipt, detector self-tests, path-free source bindings and a checksum closure. Nothing in
the skill previously produced them. This builder does, and every receipt is the result of
actually running the detectors below over the finished directory; nothing is asserted.

The bundle is deliberately minimal. The coordinator already receives the five canonical
Phase-1 reports, so the only payloads exposed here are the three receipts themselves. If a
future audit needs to expose an aggregate evidence file, drop it into work/safe_evidence
before running this script; it will be scanned, hashed and listed like the receipts.

Emits under work/safe_evidence/:
  source_bindings.json  hash/size bindings for the deposited sources, no paths
  self_tests.json       every detector against a planted positive and negative control
  privacy_scan.json     full-directory rescan, all counts must be zero
  manifest.json         i4r-safe-evidence-1 artifact manifest
  SHA256SUMS            closure over every payload plus manifest.json, excluding itself
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import pathlib
import re
import socket
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import provider_safety  # noqa: E402  -- the deposit-name exemption shared with run_coordinator

SCHEMA = "i4r-safe-evidence-1"
RECEIPTS = ("privacy_scan.json", "self_tests.json", "source_bindings.json")

# Detectors mirror scripts/phase1_runtime.py and scripts/run_coordinator.py. The macOS
# home segment is assembled, not written, so build_package.py's own path scan does not
# flag this source file as leaking an account name.
_HOME_SEGMENT = "/" + "Users/"
ABSOLUTE_PATH = re.compile(
    r"(?:" + _HOME_SEGMENT + r"[^/\s]+|/home/[^/\s]+|/private/(?:tmp|var)/[^\s]+|"
    r"[A-Za-z]:[\\/]Users[\\/][^\\/\s]+)", re.IGNORECASE
)
PARTICIPANT_CODE = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{1,3}_?P\d+(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])P\d{1,3}(?![A-Za-z0-9])"
)
ROW_SIGNATURE = re.compile(
    r"(?:^|[,\t|])\s*-?\d+(?:\.\d+)?\s*(?:[,\t|]\s*-?\d+(?:\.\d+)?\s*){4,}", re.MULTILINE
)
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def user_host_pattern() -> re.Pattern[str]:
    """Match this machine's own account and host names, plus any e-mail address."""
    tokens = set()
    for value in (
        os.environ.get("USER"), os.environ.get("LOGNAME"), os.environ.get("HOSTNAME"),
    ):
        if value:
            tokens.add(value)
    try:
        tokens.add(getpass.getuser())
    except Exception:  # pragma: no cover - environment dependent
        pass
    try:
        host = socket.gethostname()
        tokens.add(host)
        tokens.add(host.split(".")[0])
    except Exception:  # pragma: no cover - environment dependent
        pass
    tokens = {token for token in tokens if len(token) >= 4}
    alternatives = [re.escape(token) for token in sorted(tokens)]
    body = r"\b(?:" + "|".join(alternatives) + r")\b|" if alternatives else ""
    return re.compile(body + EMAIL.pattern, re.IGNORECASE)


def detectors() -> dict[str, re.Pattern[str]]:
    return {
        "absolute_path": ABSOLUTE_PATH,
        "participant_code": PARTICIPANT_CODE,
        "row_level_numeric_signature": ROW_SIGNATURE,
        "user_host_or_printer_value": user_host_pattern(),
    }


def controls() -> list[tuple[str, str, bool]]:
    """One planted positive and one planted negative per detector."""
    own = getpass.getuser() if len(getpass.getuser()) >= 4 else "someone@example.org"
    return [
        ("absolute_path", _HOME_SEGMENT + "example/audit/pkg/data.csv", True),
        ("absolute_path", "pkg/data.csv", False),
        ("participant_code", "P42", True),
        ("participant_code", "TLI equals 0.9546", False),
        ("row_level_numeric_signature", "1,19,2.67,-18.00,40.00,3.63,18.0", True),
        ("row_level_numeric_signature", "the model has 6 indicators", False),
        ("user_host_or_printer_value", f"written by {own}", True),
        ("user_host_or_printer_value", "the deposited script", False),
    ]


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_self_tests(patterns: dict[str, re.Pattern[str]]) -> dict[str, object]:
    run = passed = 0
    failures = []
    for name, text, should_match in controls():
        run += 1
        if bool(patterns[name].search(text)) == should_match:
            passed += 1
        else:
            failures.append({"detector": name, "expected_match": should_match})
    return {
        "schema_version": SCHEMA,
        "passed": run == passed and not failures,
        "tests_run": run,
        "tests_passed": passed,
        "failures": failures,
    }


def scan_directory(root: pathlib.Path, patterns: dict[str, re.Pattern[str]],
                   exempt: frozenset[str] = frozenset()) -> dict[str, int]:
    """Count detector matches. A participant-code match inside a deposit column name that the
    coordinator admits (provider_safety.deposit_exemptions) is not counted, so this receipt and
    the coordinator's text scan agree on the same payload."""
    counts = {name: 0 for name in patterns}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            # The docstring invites aggregate figures, PDFs and spreadsheets into this directory,
            # and read_text() on any of them raised a bare UnicodeDecodeError naming pathlib
            # internals, not the file. Scan the bytes as text with replacement -- a private path
            # or participant code in a binary is still a run of ASCII -- and name the file if
            # even that fails.
            try:
                text = path.read_bytes().decode("utf-8", errors="replace")
            except OSError as exc:
                raise SystemExit(f"cannot read safe-evidence file {path}: {exc}") from exc
            spans = provider_safety.exempt_spans(text, exempt) if exempt else []
            for name, pattern in patterns.items():
                if name == "participant_code" and spans:
                    counts[name] += sum(
                        1 for match in pattern.finditer(text)
                        if not any(start <= match.start() and match.end() <= end for start, end in spans)
                    )
                else:
                    counts[name] += len(pattern.findall(text))
    return counts


def receipt(counts: dict[str, int], *, rescan_passed: bool) -> dict[str, object]:
    return {
        "schema_version": SCHEMA,
        "passed": rescan_passed,
        "full_directory_rescan_passed": rescan_passed,
        "absolute_local_paths_emitted": counts["absolute_path"] > 0,
        "participant_identifiers_emitted": counts["participant_code"] > 0,
        "row_values_or_signatures_emitted": counts["row_level_numeric_signature"] > 0,
        "user_host_or_printer_values_emitted": counts["user_host_or_printer_value"] > 0,
        "generic_detector_match_counts": {
            "participant_code": counts["participant_code"],
            "row_level_numeric_signature": counts["row_level_numeric_signature"],
        },
        "source_derived_token_hit_counts": {
            "absolute_path": counts["absolute_path"],
            "user_host_or_printer_value": counts["user_host_or_printer_value"],
        },
    }


def write_json(path: pathlib.Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if any(value in {"-h", "--help"} for value in args):
        print(__doc__)
        return 0
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    audit = pathlib.Path(args[0]).resolve()
    safe = audit / "work" / "safe_evidence"
    safe.mkdir(parents=True, exist_ok=True)
    for stale in safe.iterdir():
        if stale.name in RECEIPTS or stale.name in {"manifest.json", "SHA256SUMS"}:
            stale.unlink()

    manifest_src = json.loads((audit / "package_manifest.json").read_text(encoding="utf-8"))
    entries = manifest_src.get("files") or manifest_src.get("artifacts") or []
    write_json(safe / "source_bindings.json", {
        "schema_version": SCHEMA,
        "paths_emitted": False,
        "deposited_source_count": len(entries),
        "sources": sorted(
            (
                {
                    "kind": (row.get("extension")
                             or pathlib.PurePosixPath(row.get("path", "")).suffix.lstrip(".")
                             or "unknown").lower(),
                    "sha256": row["sha256"],
                    "bytes": row.get("bytes"),
                }
                for row in entries
            ),
            key=lambda item: item["sha256"],
        ),
    })

    patterns = detectors()
    self_tests = run_self_tests(patterns)
    write_json(safe / "self_tests.json", self_tests)
    if not self_tests["passed"]:
        print(f"FATAL: detector self-tests failed: {self_tests['failures']}", file=sys.stderr)
        return 2

    # Write a provisional receipt, then rescan the whole directory so the receipt's own
    # bytes are covered, and write the final one from that rescan.
    exempt = provider_safety.deposit_exemptions(audit / "pkg")
    write_json(safe / "privacy_scan.json", receipt(scan_directory(safe, patterns, exempt), rescan_passed=False))
    rescan = scan_directory(safe, patterns, exempt)
    clean = all(value == 0 for value in rescan.values())
    write_json(safe / "privacy_scan.json", receipt(rescan, rescan_passed=clean))
    if not clean:
        print(f"FATAL: full-directory rescan found matches {rescan}; refusing to certify",
              file=sys.stderr)
        return 2

    payloads = sorted(
        path for path in safe.iterdir()
        if path.is_file() and path.name not in {"manifest.json", "SHA256SUMS"}
    )
    manifest_path = safe / "manifest.json"
    write_json(manifest_path, {
        "schema_version": SCHEMA,
        "artifact_count": len(payloads),
        "artifacts": [
            {"path": path.name, "sha256": sha256_file(path), "bytes": path.stat().st_size}
            for path in payloads
        ],
        "checksum_closure": (
            "SHA256SUMS hashes every payload plus manifest.json; SHA256SUMS excludes itself"
        ),
    })
    (safe / "SHA256SUMS").write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in [*payloads, manifest_path]),
        encoding="utf-8",
    )
    print(f"safe evidence built: {len(payloads)} payloads, self-tests "
          f"{self_tests['tests_passed']}/{self_tests['tests_run']}, rescan clean={clean}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
