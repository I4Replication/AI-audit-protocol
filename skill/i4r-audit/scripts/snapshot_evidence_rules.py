#!/usr/bin/env python3
"""Which files a critic's round snapshot carries beyond the cited evidence (2.7.11).

Until 2.7.11 a round snapshot (test_run_critique_runtime.command_snapshot) carried only the files a registry row
binds: every ``evidence.source_hashes`` path and every existing ``dependencies.artifact_paths`` path. Two kinds of
file a critic is asked about were therefore invisible to it:

- a deposited file no row cited, although ``work/pkg_manifest.txt`` lists it and the completeness critic is told to
  map the package and look for untouched components. On one audit a finding about several deposited
  files bound only some of them, so every round's snapshot lacked one deposited file and the
  completeness critic marked its check of that finding UNVERIFIABLE (the uncited file was "absent from the accessible
  evidence set"); critics in four more audits reported deposit files missing the same way;
- the verification snippet a row names in ``verification_snippet.path`` (``work/snippets/F1.R``) and the snippets'
  shared helpers (``_prelude.R``): later-round critics check a row's observed values without the code that produced
  them (a later-round critic found the registry's snippets inaccessible).

The rule is frozen per audit, in the policy snapshot leaf ``critique.snapshot_evidence``:

- absent: ``LEGACY_RULE``, cited files only. Every audit initialised before 2.7.11 froze no leaf; its snapshots keep
  their bytes and content addresses, so an in-flight round reuses its snapshot, plan and cached critics. There is NO
  fallback to the packaged policy, and the value ``cited_only`` itself is never frozen.
- ``DEPOSIT_RULE``: the snapshot also carries, under ``source/evidence/`` at their audit-relative paths, first each
  row's verification snippet and the ``_prelude.R`` beside it (path order), then the deposit's uncited files as
  ``package_manifest.json`` lists them (smallest first, path as the tie-break), within the bounds below. Every
  candidate left out is listed in ``source/context/WITHHELD_FILES.json`` with its reason, so a critic can tell a
  withheld file from a missing one; that file exists only when something was withheld.

Provider safety (the files are sent to the critic provider, and their paths enter the inventory and the critic output
schema's evidence-path enum). The caller (command_snapshot) applies the coordinator's own screens before planning:

- a candidate whose path has a component shaped like a participant code (the rule run_coordinator's masked package
  manifest applies, 2.6.8, deposit exemptions included), or whose inventory path matches the coordinator's private-path
  pattern, is withheld as ``name_not_provider_safe`` and its record carries only sha256, size and the reason, never
  the name (so does an ``invalid_path`` record: a snippet path that is not a normalized audit-relative path);
- a snippet must pass the content screen the coordinator applies to its inputs
  (run_coordinator.validate_provider_visible_string: no private path, no participant code outside the deposit's own
  variable names), else it is withheld as ``content_not_provider_safe``. Deposit files are not content-screened: they
  are the deposit itself, which critics already receive whenever a row cites a file of it;
- every snapshot also copies ``work/pkg_manifest.txt`` (one ``<sha256>  <path>`` line per deposited file) into its
  context, and since 2.7.8 that copy named every deposit file. Under this rule the copy is masked
  (``masked_listing``), else a withheld name would sit one file away from its nameless record, which carries the
  same sha256. The cited-only rule keeps the copy byte for byte.

The bounds belong to the rule (new bounds are a new rule name). They keep the critic output schema, whose
artifact-binding path and sha256 enums ``constrain_evidence_bindings`` builds from the whole inventory, inside the
provider's structured-output limits with margin: at most 230 inventory rows (so neither enum reaches 250 values, and
230 distinct hashes are 14,720 characters) and at most 15,000 characters of inventory paths. Before 2.7.11 the
largest inventory under runs/ held 148 rows and 9,458 path characters. Added bytes are bounded per file and per
snapshot, which bounds what one round sends and stores.

Stdlib only, like claim_change_rules.py: the drift check loads it by path.
"""
from __future__ import annotations

import json
import pathlib
import posixpath
import re
from typing import Any, Callable, Iterable, Mapping

LEGACY_RULE = "cited_only"
DEPOSIT_RULE = "cited_plus_deposit_1"
LIVE_RULES: tuple[str, ...] = (LEGACY_RULE, DEPOSIT_RULE)
SHIPPED_RULE = DEPOSIT_RULE
LEAF = ("critique", "snapshot_evidence")
WITHHELD_NAME = "WITHHELD_FILES.json"
WITHHELD_VERSION = "i4r-snapshot-withheld-files-1"
EVIDENCE_PREFIX = "source/evidence/"
SNIPPET_PRELUDE = "_prelude.R"
DEFAULT_SNIPPET_DIR = "work/snippets"
PACKAGE_LISTING = "work/pkg_manifest.txt"   # intake's deposit listing, copied into every snapshot's context

MAX_INVENTORY_ENTRIES = 230
MAX_INVENTORY_PATH_CHARS = 15_000
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_ADDED_BYTES = 100 * 1024 * 1024

# Why a candidate is not in the snapshot. A name_not_provider_safe or invalid_path record never carries the path.
NAME_NOT_PROVIDER_SAFE = "name_not_provider_safe"
INVALID_PATH = "invalid_path"
CONTENT_NOT_PROVIDER_SAFE = "content_not_provider_safe"
NOT_A_REGULAR_FILE = "not_a_regular_file"
FILE_OVER_CAP = "file_over_cap"
SNAPSHOT_OVER_CAP = "snapshot_over_cap"
INVENTORY_FULL = "inventory_full"
REASONS = (NAME_NOT_PROVIDER_SAFE, INVALID_PATH, CONTENT_NOT_PROVIDER_SAFE, NOT_A_REGULAR_FILE, FILE_OVER_CAP,
           SNAPSHOT_OVER_CAP, INVENTORY_FULL)
NAMELESS_REASONS = (NAME_NOT_PROVIDER_SAFE, INVALID_PATH)


class RuleError(ValueError):
    pass


def rule_of(policy: Mapping[str, Any] | None) -> str:
    """The rule a policy freezes: LEGACY_RULE when it has no leaf; refuse any value that is not a frozen live rule."""
    node: Any = policy
    for key in LEAF:
        if not isinstance(node, Mapping) or key not in node:
            return LEGACY_RULE
        node = node[key]
    if not isinstance(node, str) or node not in LIVE_RULES or node == LEGACY_RULE:
        raise RuleError(f"{'.'.join(LEAF)} is {node!r}, not a packaged snapshot-evidence rule "
                        f"({DEPOSIT_RULE!r}, or no leaf for the legacy rule)")
    return node


def frozen_rule(root: pathlib.Path) -> str:
    """The audit's rule, read ONLY from its own snapshot: LEGACY_RULE when there is none or it froze no leaf."""
    path = root / "pipeline-policy.snapshot.json"
    if not path.exists():
        return LEGACY_RULE
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuleError(f"cannot read the audit policy snapshot {path.name}: {exc}") from exc
    return rule_of(snapshot.get("policy") if isinstance(snapshot, Mapping) else None)


def snippet_paths(rows: Iterable[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """(named, preludes): each row's verification_snippet.path as written, and the _prelude.R candidates.

    The preludes are the one in the default snippet folder and the one beside each named snippet; a prelude is
    optional, so the caller adds one only when it exists. Both lists are sorted and free of duplicates."""
    named: set[str] = set()
    folders = {DEFAULT_SNIPPET_DIR}
    for row in rows:
        snippet = row.get("verification_snippet") if isinstance(row, Mapping) else None
        path = snippet.get("path") if isinstance(snippet, Mapping) else None
        if isinstance(path, str) and path:
            named.add(path)
            folder = posixpath.dirname(path.replace("\\", "/"))
            if folder:
                folders.add(folder)
    preludes = {posixpath.join(folder, SNIPPET_PRELUDE) for folder in folders} - named
    return sorted(named), sorted(preludes)


def deposit_candidates(manifest: Any, cited: set[str]) -> list[dict[str, Any]]:
    """The manifest's files no row cites, as audit-relative ``pkg/...`` rows with the manifest's size and sha256."""
    files = manifest.get("files") if isinstance(manifest, Mapping) else None
    if not isinstance(files, list):
        raise RuleError("package_manifest.json lacks its file list")
    rows = []
    for entry in files:
        path = entry.get("path") if isinstance(entry, Mapping) else None
        size = entry.get("size") if isinstance(entry, Mapping) else None
        digest = entry.get("sha256") if isinstance(entry, Mapping) else None
        if not isinstance(path, str) or not path or type(size) is not int or size < 0 \
                or not isinstance(digest, str) or len(digest) != 64:
            raise RuleError("package_manifest.json has a malformed file entry")
        relative = "pkg/" + path
        if relative not in cited:
            rows.append({"path": relative, "size": size, "sha256": digest})
    return rows


_LISTING_LINE = re.compile(r"([0-9a-f]{64})  (.*)")
_PLACEHOLDER_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,10}")


def masked_listing(text: str, path_safe: Callable[[str], bool], code_shaped: Callable[[str], bool]) -> str:
    """``work/pkg_manifest.txt`` as a snapshot under DEPOSIT_RULE carries it: ``text`` itself when every path passes.

    ``path_safe(path)`` is the rule's name screen for a deposit-relative path, ``code_shaped(segment)`` its
    participant-code test for one path component (both with the deposit's exemptions). A line whose path fails keeps
    its sha256, and each code-shaped component becomes a stable placeholder (``masked-dir-001``,
    ``masked-file-001.csv``) as run_coordinator.package_manifest_projection masks package_manifest.json; a path that
    still fails (the private-path pattern spans components) becomes one file placeholder. A line that is not
    ``<sha256>  <path>`` is screened whole, as a path."""
    placeholders: dict[tuple[Any, ...], str] = {}
    counters = {"dir": 0, "file": 0}

    def placeholder(key: tuple[Any, ...], kind: str, segment: str) -> str:
        if key not in placeholders:
            counters[kind] += 1
            suffix = posixpath.splitext(segment)[1] if kind == "file" else ""
            if not _PLACEHOLDER_SUFFIX.fullmatch(suffix) or code_shaped(suffix):
                suffix = ""
            placeholders[key] = f"masked-{kind}-{counters[kind]:03d}{suffix}"
        return placeholders[key]

    lines = text.split("\n")
    changed = False
    for index, line in enumerate(lines):
        match = _LISTING_LINE.fullmatch(line)
        digest, path = (match.group(1), match.group(2)) if match else (None, line)
        if path_safe(path):
            continue
        parts = path.split("/")
        shown = []
        for depth, segment in enumerate(parts):
            key = tuple(parts[:depth + 1])
            if key in placeholders or code_shaped(segment):
                segment = placeholder(key, "file" if depth == len(parts) - 1 else "dir", segment)
            shown.append(segment)
        masked = "/".join(shown)
        if not path_safe(masked):
            masked = placeholder((None,) + tuple(parts), "file", parts[-1])
        if not path_safe(masked):   # cannot happen with the shipped screens; never write what failed
            raise RuleError(f"{PACKAGE_LISTING} line {index + 1} cannot be masked")
        lines[index] = f"{digest}  {masked}" if digest else masked
        changed = True
    return "\n".join(lines) if changed else text


def plan_additions(snippets: Iterable[Mapping[str, Any]], deposit: Iterable[Mapping[str, Any]],
                   other_entries: int, other_chars: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split screened candidates (each with path, size, sha256) into (carried, withheld) under the rule's bounds.

    Snippets come first, in path order; then deposit files, smallest first with the path as the tie-break.
    ``other_entries`` and ``other_chars`` count every other inventory row and its path characters, the withheld
    list's own row included."""
    ordered = sorted((dict(row) for row in snippets), key=lambda row: row["path"]) + sorted(
        (dict(row) for row in deposit), key=lambda row: (row["size"], row["path"]))
    carried: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    total, entries, chars = 0, other_entries, other_chars
    for row in ordered:
        listed = len(EVIDENCE_PREFIX + row["path"])
        if row["size"] > MAX_FILE_BYTES:
            withheld.append({**row, "reason": FILE_OVER_CAP})
        elif total + row["size"] > MAX_ADDED_BYTES:
            withheld.append({**row, "reason": SNAPSHOT_OVER_CAP})
        elif entries + 1 > MAX_INVENTORY_ENTRIES or chars + listed > MAX_INVENTORY_PATH_CHARS:
            withheld.append({**row, "reason": INVENTORY_FULL})
        else:
            carried.append(row)
            total += row["size"]
            entries += 1
            chars += listed
    return carried, withheld


def withheld_document(rule: str, withheld: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The WITHHELD_FILES.json body: records sorted by path (nameless records last, by hash), fixed keys only."""
    rows = []
    for row in withheld:
        if row.get("reason") not in REASONS:
            raise RuleError(f"unknown withheld reason {row.get('reason')!r}")
        record = {"sha256": row.get("sha256"), "size": row.get("size"), "reason": row["reason"]}
        if row["reason"] not in NAMELESS_REASONS:
            record = {"path": row["path"], **record}
        rows.append(record)
    rows.sort(key=lambda record: ("path" not in record, record.get("path") or "", record.get("sha256") or ""))
    return {
        "withheld_version": WITHHELD_VERSION, "rule": rule,
        "bounds": {"max_inventory_entries": MAX_INVENTORY_ENTRIES,
                   "max_inventory_path_chars": MAX_INVENTORY_PATH_CHARS,
                   "max_file_bytes": MAX_FILE_BYTES, "max_added_bytes": MAX_ADDED_BYTES},
        "withheld": rows,
    }
