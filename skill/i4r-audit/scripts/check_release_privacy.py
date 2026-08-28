#!/usr/bin/env python3
"""Fail-closed privacy inspection for files that can enter a release.

Text-like source files are scanned directly.  PDF text and metadata are
extracted with Poppler, and DOCX text/metadata are read from the OOXML
container.  A document that cannot be inspected is a release failure rather
than an implicit clean result.
"""
from __future__ import annotations

import argparse
import html
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.parse
import zipfile
from dataclasses import dataclass
from typing import Iterable


# Keep this list shared by staging and package construction.  Notebooks and
# statistical-language sources are text too; omitting them is a privacy bypass.
TEXT_SUFFIXES = {
    ".bib",
    ".cfg",
    ".css",
    ".csv",
    ".do",
    ".html",
    ".ini",
    ".ipynb",
    ".jl",
    ".js",
    ".json",
    ".jsonl",
    ".m",
    ".md",
    ".py",
    ".qmd",
    ".r",
    ".rmd",
    ".sas",
    ".sh",
    ".sps",
    ".sql",
    ".tex",
    ".toml",
    ".ts",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
DOCUMENT_SUFFIXES = {".pdf", ".docx"}

_UNIX_HOME = re.compile(
    r"/(?:" + "Us" + r"ers|home)/([A-Za-z0-9._-]+)(?=/|[\s'\"),:;]|$)"
)
_WINDOWS_HOME = re.compile(
    r"(?i)(?:[A-Z]:)?[\\/]+" + "Us" + r"ers[\\/]+([^\\/\s]+)(?=[\\/]|[\s'\"),:;]|$)"
)
_PLACEHOLDER = re.compile(
    r"^(?:example|placeholder|redacted|user|username|youruser|someone|account|"
    r"example-account|your-?name|\[.*\]|<.*>)$",
    re.I,
)

# The same identifier shapes used by the release gate are also applied to text
# extracted from PDF/DOCX, which otherwise bypasses the ordinary text walk.
_CODE = re.compile(
    r"(?:^|[^A-Za-z0-9])sub_[EN]_\d+"
    r"|(?:^|[^A-Za-z0-9])sub_\d+Placebo"
    r"|\bNoExperto\d+|\bExperto\d+"
    r"|\b(?:participant|subject)\s*#?\d{1,3}\b",
    re.I,
)
_STAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T?\d{0,2}:?\d{0,2}|\d{2}:\d{2}:\d{2}"
)
_OPAQUE = re.compile(
    r"\b(?:[A-Za-z]{1,4}_)?(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*[a-z])"
    r"(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{12,}\b"
)
_HEX = re.compile(r"\A[0-9a-fA-F]+\Z")

MAX_DOCX_MEMBERS = 5000
MAX_DOCX_UNCOMPRESSED = 50 * 1024 * 1024


@dataclass(frozen=True)
class Issue:
    category: str
    location: str
    detail: str


class InspectionError(ValueError):
    """Raised when an artifact cannot be fully inspected."""


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # Latin-1 is a one-byte mapping and therefore cannot silently discard a
        # path that was present in the input bytes.
        return data.decode("latin-1")


def _private_path_issues(text: str, label: str) -> list[Issue]:
    issues: list[Issue] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        seen_spans: set[tuple[int, int]] = set()
        for pattern in (_UNIX_HOME, _WINDOWS_HOME):
            for match in pattern.finditer(line):
                account = match.group(1)
                if not _PLACEHOLDER.fullmatch(account) and match.span() not in seen_spans:
                    seen_spans.add(match.span())
                    issues.append(
                        Issue(
                            "private_path",
                            f"{label}:{line_number}",
                            "absolute path names a real account",
                        )
                    )
    return issues


def _identifier_issues(text: str, label: str) -> list[Issue]:
    lines = text.splitlines()
    pair_lines: list[int] = []
    code_hits = 0
    for line_number, line in enumerate(lines, 1):
        if _CODE.search(line):
            code_hits += 1
            if _STAMP.search(line):
                pair_lines.append(line_number)
    nonempty = sum(bool(line.strip()) for line in lines)
    issues = [
        Issue("identifier_timestamp", f"{label}:{line}", "participant code is paired with a timestamp")
        for line in pair_lines
    ]
    if code_hits >= 8 and code_hits / max(nonempty, 1) >= 0.25:
        issues.append(
            Issue(
                "identifier_listing",
                label,
                f"participant-code listing ({code_hits} of {nonempty} nonempty lines)",
            )
        )
    opaque = {
        token
        for token in _OPAQUE.findall(text)
        if not _HEX.fullmatch(token.split("_")[-1])
    }
    if len(opaque) >= 20:
        issues.append(
            Issue(
                "opaque_identifier_listing",
                label,
                f"opaque per-row identifier listing ({len(opaque)} distinct identifiers)",
            )
        )
    return issues


def inspect_text(text: str, label: str, *, identifiers: bool = False) -> list[Issue]:
    normalized = urllib.parse.unquote(html.unescape(text))
    normalized = normalized.replace(r"\/", "/")
    normalized = re.sub(r"\\u002[fF]", "/", normalized)
    normalized = re.sub(r"\\u005[cC]", r"\\", normalized)
    issues = _private_path_issues(text, label)
    if normalized != text:
        issues.extend(_private_path_issues(normalized, label))
    if identifiers:
        issues.extend(_identifier_issues(text, label))
        if normalized != text:
            issues.extend(_identifier_issues(normalized, label))
    # Multiple normalizations can reveal the same source line.  Report it once
    # while preserving separate paths and separate privacy categories.
    return list(dict.fromkeys(issues))


def inspect_text_bytes(data: bytes, label: str, *, identifiers: bool = False) -> list[Issue]:
    """Inspect the exact byte snapshot that a caller intends to distribute."""
    return inspect_text(_decode_text(data), label, identifiers=identifiers)


def _run_extractor(command: list[str], path: pathlib.Path, kind: str) -> str:
    executable = shutil.which(command[0])
    if executable is None:
        raise InspectionError(f"cannot inspect {kind} {path}: required tool {command[0]!r} is unavailable")
    command[0] = executable
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InspectionError(f"cannot inspect {kind} {path}: {exc}") from exc
    if result.returncode != 0:
        reason = _decode_text(result.stderr).strip().replace("\n", " ")[:300]
        raise InspectionError(
            f"cannot inspect {kind} {path}: extractor exited {result.returncode}"
            + (f" ({reason})" if reason else "")
        )
    return _decode_text(result.stdout)


def inspect_pdf(path: pathlib.Path, label: str) -> list[Issue]:
    metadata = _run_extractor(["pdfinfo", "-custom", str(path)], path, "PDF metadata")
    xmp_metadata = _run_extractor(["pdfinfo", "-meta", str(path)], path, "PDF XMP metadata")
    text = _run_extractor(["pdftotext", "-enc", "UTF-8", "-nopgbrk", str(path), "-"], path, "PDF text")
    return inspect_text(metadata + "\n" + xmp_metadata + "\n" + text, label, identifiers=True)


def inspect_docx(path: pathlib.Path, label: str) -> list[Issue]:
    if not zipfile.is_zipfile(path):
        raise InspectionError(f"cannot inspect DOCX {path}: not a valid OOXML ZIP container")
    chunks: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_DOCX_MEMBERS:
                raise InspectionError(
                    f"cannot inspect DOCX {path}: {len(infos)} members exceeds {MAX_DOCX_MEMBERS}"
                )
            if sum(info.file_size for info in infos) > MAX_DOCX_UNCOMPRESSED:
                raise InspectionError(
                    f"cannot inspect DOCX {path}: uncompressed content exceeds {MAX_DOCX_UNCOMPRESSED} bytes"
                )
            names = {info.filename for info in infos}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise InspectionError(f"cannot inspect DOCX {path}: required OOXML parts are missing")
            for info in infos:
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise InspectionError(f"cannot inspect DOCX {path}: symlink member {info.filename!r}")
                lower = info.filename.lower()
                if lower.endswith((".xml", ".rels", ".txt")):
                    chunks.append(info.filename)
                    decoded = _decode_text(archive.read(info))
                    chunks.append(decoded)
                    # Word may split a visible path across adjacent text runs
                    # or encode separators as XML entities.  Scan a flattened,
                    # entity-decoded representation as well as the raw XML so
                    # relationship attributes remain visible.
                    chunks.append(html.unescape(re.sub(r"<[^>]*>", "", decoded)))
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise InspectionError(f"cannot inspect DOCX {path}: {exc}") from exc
    return inspect_text("\n".join(chunks), label, identifiers=True)


def inspect_file(path: pathlib.Path, label: str | None = None, *, identifiers: bool = False) -> list[Issue]:
    label = label or path.as_posix()
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return inspect_text_bytes(path.read_bytes(), label, identifiers=identifiers)
    if suffix == ".pdf":
        return inspect_pdf(path, label)
    if suffix == ".docx":
        return inspect_docx(path, label)
    return []


def inspect_tree(root: pathlib.Path, *, identifiers: bool = False) -> list[Issue]:
    root = root.resolve(strict=True)
    issues: list[Issue] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise InspectionError(f"cannot inspect release tree: symlink is present: {path.relative_to(root)}")
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES | DOCUMENT_SUFFIXES:
            continue
        issues.extend(inspect_file(path, path.relative_to(root).as_posix(), identifiers=identifiers))
    return issues


def format_issues(issues: Iterable[Issue]) -> str:
    rows = list(issues)
    private = [issue for issue in rows if issue.category == "private_path"]
    other = [issue for issue in rows if issue.category != "private_path"]
    parts: list[str] = []
    if private:
        parts.append(f"FAIL: {len(private)} absolute path(s) naming a real account:")
        parts.extend(f"  {issue.location}" for issue in private[:30])
    if other:
        parts.append(f"FAIL: {len(other)} participant-identifier privacy issue(s):")
        parts.extend(f"  {issue.location}: {issue.detail}" for issue in other[:30])
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=pathlib.Path)
    parser.add_argument("--identifiers", action="store_true", help="also scan extracted documents for identifier listings")
    args = parser.parse_args()
    try:
        issues = inspect_tree(args.root, identifiers=args.identifiers)
    except (InspectionError, OSError) as exc:
        print(f"FAIL: privacy inspection was incomplete: {exc}", file=sys.stderr)
        return 1
    if issues:
        print(format_issues(issues))
        return 1
    print("  ok: text, PDF text/metadata, and DOCX text/metadata pass privacy inspection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
