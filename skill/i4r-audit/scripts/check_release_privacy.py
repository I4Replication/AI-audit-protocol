#!/usr/bin/env python3
"""Fail-closed privacy inspection for files that can enter a release.

Text-like source files are scanned directly.  PDF text and metadata are
extracted with Poppler, and DOCX text/metadata are read from the OOXML
container.  A document that cannot be inspected is a release failure rather
than an implicit clean result.
"""
from __future__ import annotations

import argparse
import json
import html
import pathlib
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
import zipfile
from dataclasses import dataclass
from typing import Any, Iterable


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

_ACCOUNT = r"[\w.\-]+"
# A home path is only a home path when the separator that introduces it starts a path: at the
# beginning of the text, after whitespace or a quote/bracket, after a drive letter, or after a UNC
# server name. Without this, ANY relative path carrying a users- or home-named segment was reported
# as a real account -- a deposit path like data/<users-dir>/alice.csv, or a URL path, both of which
# used to block a release.
_PATH_START = r"(?<![A-Za-z0-9._~-])"
_UNIX_HOME = re.compile(
    _PATH_START + r"/(?:" + "Us" + r"ers|home)/(" + _ACCOUNT + r")(?=/|[\s'\"),:;]|$)"
)
# The account segment is restricted to account-name characters. The earlier `[^\\/\s]+` also
# matched regex metacharacters, so privacy-detector source that merely spells the `/Users/` prefix
# (`(/Users/|/home/|...)`, `/Users/[^/\s]+`) was reported as a real account and blocked a release.
# `\w` is Unicode-aware for str patterns, so _ACCOUNT is EXACTLY `[A-Za-z0-9._-]` on ASCII input
# -- no ASCII string changes verdict -- and additionally admits non-ASCII account names, which the
# ASCII class dropped entirely. Regex metacharacters stay out, so the note above still holds.
# Three ways a Windows home path legitimately begins: a drive letter, a UNC server name, or a bare
# separator that itself starts a path. The earlier `(?:[A-Z]:)?[\\/]+` made the drive optional with
# no boundary, so a single separator anywhere in a relative path matched. All three forms are
# preserved here -- a proposed earlier fix silently dropped the bare-separator and UNC forms, which
# is why it was rejected. The forms are spelled out as test fixtures in
# tests/test_stage_package_hardening.py rather than in this comment: this file is itself scanned,
# and writing a literal home path here blocks every release.
_WINDOWS_HOME = re.compile(
    r"(?i)(?:[A-Z]:[\\/]+|\\\\[^\\/\s]+[\\/]+|" + _PATH_START + r"[\\/]+)"
    + "Us" + r"ers[\\/]+(" + _ACCOUNT + r")(?=[\\/]|[\s'\"),:;]|$)"
)
# The account segment of a home path is exempt when it is a placeholder. The fictitious names
# a detector's self-test uses belong here too: an auditor's own privacy scanner carried
# `path /Users/janedoe/data.csv` beside `jane.doe@example.com` and a fake SSN as its known-present
# control string, and that synthetic line blocked an entire clean release. "janedoe" is nobody.
_PLACEHOLDER = re.compile(
    r"^(?:example|placeholder|redacted|user|username|youruser|someone|account|"
    r"example-account|your-?name|jane[._-]?doe|john[._-]?doe|j[._-]?doe|test(?:er|user)?|"
    r"alice|bob|\[.*\]|<.*>)$",
    re.I,
)

# The same identifier shapes used by the release gate are also applied to text
# extracted from PDF/DOCX, which otherwise bypasses the ordinary text walk.
_CODE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:MSTE_AC_)?sub_[EN]_\d+"
    r"|(?:^|[^A-Za-z0-9])(?:MSTE_AC_)?sub_\d+Placebo"
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
# A base64 payload is exactly the shape _OPAQUE hunts: runs of mixed-case alphanumerics with
# digits, split into ~16-character tokens by `+` and `/`. One rendered matplotlib figure left in a
# staged .ipynb produced ~1170 "distinct identifiers" and blocked a clean release -- the same
# defect the DOCX fix in 2.4.7 recorded for Word's stylesheet, in a different container. Strip
# data: URIs and long base64 runs before the identifier scan; the path scan still sees them.
_DATA_URI = re.compile(r"data:[A-Za-z0-9.+/-]+;base64,[A-Za-z0-9+/=\s]+")
_BASE64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])(?:[A-Za-z0-9+/]{4}){40,}={0,2}(?![A-Za-z0-9+/=])")


def _without_base64(text: str) -> str:
    text = _DATA_URI.sub(" ", text)
    return _BASE64_RUN.sub(" ", text)

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


AUDIT_PLACEHOLDER = "<audit>"
TREE_PLACEHOLDER = "<tree>"


def neutralise_audit_paths(text: str, prefixes: Iterable[str],
                           tree_prefixes: Iterable[str] = ()) -> tuple[str, int]:
    """Replace occurrences of the audit's own absolute root with a placeholder.

    ONLY the audit root, and only in working source. An auditor's script must name the snapshot it
    actually read; the snapshot lives inside the audit directory, which lives under the operator's
    home, so the absolute path is unavoidable AT RUN TIME. It is also hash-bound into the role's
    evidence manifest, so the audit-side copy cannot be edited without invalidating the record.
    The release copy can be, and must be: only the account segment is private, and what remains
    (`<audit>/phase1/snapshots/<sha>/raw/pkg`) still tells the reader which snapshot was read.

    A path that does NOT start at the audit root is a different animal -- someone's private input
    directory, a Desktop, another project -- and stays untouched so the detector blocks the
    release. The auditor should not have read from there, and tidying it away would hide that.
    """
    count = 0
    # Longest first: an audit root and its resolved alias can share a prefix.
    for prefix in sorted({p for p in prefixes if p}, key=len, reverse=True):
        found = text.count(prefix)
        if found:
            text = text.replace(prefix, AUDIT_PLACEHOLDER)
            count += found
    # Then the discipline TREE root the audit sits in (2.6.2), always with its trailing separator so
    # a sibling such as `<tree>2/` is never touched. An auditor split the audit path across three
    # string literals ("<tree>/runs/" "<Journal>/<Paper>/" "audit/phase1/..."), so the full audit
    # root never appeared and the release blocked on our own account name. The tree root is ours,
    # exactly like the audit root; any other account path is still left for the detector to block.
    for prefix in sorted({p.rstrip("/") + "/" for p in tree_prefixes if p}, key=len, reverse=True):
        found = text.count(prefix)
        if found:
            text = text.replace(prefix, TREE_PLACEHOLDER + "/")
            count += found
    return text, count


def _identifier_issues(text: str, label: str) -> list[Issue]:
    text = _without_base64(text)
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


# An email address is a direct identifier. The byline's correspondence address and addresses the
# published article itself prints (author contact lines) are public and expected; anything else is
# reported. Reserved example domains are the fixtures detector self-tests use.
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_EXAMPLE_EMAIL_DOMAIN = re.compile(r"(?i)@(?:[a-z0-9-]+\.)*example\.(?:com|org|net)$")
ALLOWED_EMAILS: set[str] = set()
# Attribute names that follow `@` in matrix code (X.T@X.values, M@model.params). None of them is a
# delegated top-level domain (at, to, data and dot are, so they are NOT here: a method call such as
# x@W.to(device) is recognised by the "(" that follows instead). Anything else counts as an address.
_CODE_ATTRIBUTES = frozenset("""
t values params shape index columns loc iloc iat coef coefficients resid residuals fittedvalues
dtype size ndim flat real imag sum mean std var transpose inv pinv solve weight weights bias grad
mat matrix array cpu cuda numpy item tolist copy reshape flatten squeeze unsqueeze frame table
ident assay
""".split())
# A line carrying this marker is exempt: the author states the "address" is code (a matmul chain the
# heuristics below misread). Shipped text is reviewed, so the marker is itself visible in the release.
_NOT_AN_EMAIL = "i4r-privacy: not-an-email"
_LINE_BREAK = re.compile("\r\n|[\r\n\u2028\u2029\u0085]")
_CODE_SUFFIXES = (".py", ".r", ".jl", ".do", ".ado", ".m", ".sh", ".sas", ".qmd", ".rmd")


def _code_file(label: str) -> bool:
    return str(label).split(":", 1)[0].lower().endswith(_CODE_SUFFIXES)
_AMBIGUOUS_TLDS = frozenset({"at", "to", "data", "dot", "mt"})


def allow_emails_from(path: pathlib.Path) -> None:
    """Treat every address printed in `path` (the byline, the published article) as public."""
    if path.is_file():
        ALLOWED_EMAILS.update(m.lower() for m in _EMAIL.findall(_decode_text(path.read_bytes())))


def _email_issues(text: str, label: str) -> list[Issue]:
    issues = []
    for match in _EMAIL.finditer(text):
        if _code_file(label):
            # the marker exempts only its own line (any line-break convention), and only in source files:
            # a quoted marker inside a JSON record must not exempt the whole record
            breaks = [m.end() for m in _LINE_BREAK.finditer(text, 0, match.start())]
            line_start = breaks[-1] if breaks else 0
            following = _LINE_BREAK.search(text, match.end())
            if _NOT_AN_EMAIL in text[line_start:following.start() if following else len(text)]:
                continue
        address = match.group(0).lower()
        if address in ALLOWED_EMAILS or _EXAMPLE_EMAIL_DOMAIN.search(address):
            continue
        local, domain = address.rsplit("@", 1)
        after = text[match.end():match.end() + 2]
        # scp remote (git@github.com:org/pkg.git) or a URL's user part (ssh://git@..., https://oauth2@...)
        if (len(after) == 2 and after[0] == ":" and not after[1].isspace()) or \
                text[:match.start()].endswith("://"):
            continue
        # code: a method call or subscript right after the "domain", or a known attribute name as "TLD"
        # "[1]" or a Markdown "[^note]" after an address is a footnote, not a subscript
        subscript = after[:1] == "[" and not (after[1:2].isdigit() or after[1:2] == "^")
        # "data.T" or R's "data$x" continue a code chain; "$^{*}$" after an address is a LaTeX footnote
        attribute = (after[:1] == "." and after[1:2].isalnum()) or \
            (after[:1] == "$" and (after[1:2].isalpha() or after[1:2] in "._"))
        if after[:1] == "(" or subscript or attribute or domain.rsplit(".", 1)[-1] in _CODE_ATTRIBUTES:
            continue
        # at/to/data/dot are real TLDs, but "x@self.W.data" / "A@B.dot" are attribute chains: treat them
        # as code only when another label is `self` or a single letter, which no address domain here has
        # (univie.ac.at, wu.ac.at are still caught).
        labels = domain.split(".")
        # `self` and R's `@meta` slot are never address labels; a one-letter label is, in a university
        # subdomain (a dotted name at a one-letter university subdomain), so that branch also needs an undotted local part
        if labels[-1] in _AMBIGUOUS_TLDS and (
                (labels[-1] in ("data", "dot") and any(part in ("self", "meta", "weight") for part in labels[:-1]))
                # a one-letter label pairs with a code-short local part ("x@W.data", "X.T@W.data"),
                # never with a name at a one-letter university subdomain
                or (labels[-1] in ("data", "dot", "mt") and all(len(seg) <= 2 or seg == "self" for seg in local.split("."))
                    and any(len(part) == 1 for part in labels[:-1]))):
            continue
        line = text.count("\n", 0, match.start()) + 1
        masked = f"{local[:1]}***@{domain}"
        issues.append(Issue("email", f"{label}:{line}",
                            f"email address ({masked}) that is neither the byline's nor printed in the "
                            "published article"))
    return issues


def inspect_text(text: str, label: str, *, identifiers: bool = False) -> list[Issue]:
    normalized = urllib.parse.unquote(html.unescape(text))
    normalized = normalized.replace(r"\/", "/")
    normalized = re.sub(r"\\u002[fF]", "/", normalized)
    normalized = re.sub(r"\\u005[cC]", r"\\", normalized)
    # macOS stores filenames decomposed, so an accented account arrives as `e` + U+0301 and the
    # combining mark is not a `\w` character. Compose before the second scan.
    normalized = unicodedata.normalize("NFC", normalized)
    issues = _private_path_issues(text, label)
    if normalized != text:
        issues.extend(_private_path_issues(normalized, label))
    if identifiers:
        issues.extend(_identifier_issues(text, label))
        issues.extend(_email_issues(normalized, label))
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
    visible: list[str] = []
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
                    flattened = html.unescape(re.sub(r"<[^>]*>", "", decoded))
                    chunks.append(flattened)
                    if _is_visible_part(info.filename):
                        # The identifier rules are LINE-based (listing density) and TOKEN-based
                        # (distinct opaque tokens). The separator-less flatten above is right for
                        # the path scan -- Word splits a path across runs -- but it welds a 25-row
                        # participant table into one line and one token, so neither rule could
                        # fire on a DOCX while the same table in Markdown was rejected. Give the
                        # identifier scan the reader's structure: paragraph, row, cell and break
                        # boundaries become newlines before the remaining tags are removed.
                        visible.append(_visible_text(decoded))
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise InspectionError(f"cannot inspect DOCX {path}: {exc}") from exc
    # Private paths are hunted through EVERY part, raw markup included, because one can hide in a
    # relationship attribute. Participant identifiers are hunted only through what a reader can
    # SEE. Running the identifier scan over markup found 21 "opaque identifiers" in a clean
    # comment -- eleven pandoc bookmark anchors (`X` + hex, which the hex exemption misses because
    # of the prefix), nine Word style names from the reference document (`Heading1Char` ...), and
    # the institute's own name -- and blocked the release. Any comment with enough sections would
    # cross the threshold of 20 on document machinery alone.
    issues = inspect_text("\n".join(chunks), label, identifiers=False)
    issues.extend(inspect_text("\n".join(visible), label, identifiers=True))
    return list(dict.fromkeys(issues))


_DOCX_ROW = re.compile(r"<w:tr\b.*?</w:tr>", re.I | re.S)
_DOCX_CELL_END = re.compile(r"</w:tc>", re.I)
_DOCX_PARA_END = re.compile(r"</w:p>", re.I)
_DOCX_BREAKS = re.compile(r"</w:p>|<w:br\b[^>]*/?>|<w:cr\b[^>]*/?>", re.I)
_DOCX_TAB = re.compile(r"<w:tab\b[^>]*/?>", re.I)


def _visible_text(xml: str) -> str:
    """Flatten a visible DOCX part the way a reader sees it: one line per paragraph, one line
    per table ROW with cells tab-separated -- so a participant code and its timestamp in
    adjacent cells land on one line, exactly as the same table does in Markdown."""
    def row(match: "re.Match[str]") -> str:
        cells = _DOCX_CELL_END.sub("\t", _DOCX_PARA_END.sub(" ", match.group(0)))
        return re.sub(r"<[^>]*>", "", cells).strip() + "\n"
    text = _DOCX_ROW.sub(row, xml)
    text = _DOCX_TAB.sub("\t", _DOCX_BREAKS.sub("\n", text))
    return html.unescape(re.sub(r"<[^>]*>", "", text))


# Parts a reader actually sees. Styles, themes, settings, fonts and relationship files carry
# generated names, not content.
_VISIBLE_DOCX_PARTS = re.compile(
    r"^word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$", re.I)


def _is_visible_part(name: str) -> bool:
    return bool(_VISIBLE_DOCX_PARTS.match(name))


REDACTION_PLACEHOLDER = "<removed-before-release>"

# A declaration can only stand in for removal of fields that are internal telemetry by design.
# Without this bound, any pointer could be declared and the gate would inspect nothing: the
# release privacy check would be silenced by the very file it is checking.
REDACTABLE_POINTER_PREFIXES = (
    "/audit_dir", "/toolkit/skill_path", "/costs", "/auditor_model/provider",
    "/model_assignments", "/failure_events", "/compliance/violations", "/delivery/recipient",
    "/delivery/release_dir", "/events/*/payload/release_dir",
    "/events/*/payload/record/error", "/invocations/*/error",
    "/events/*/payload/record/stderr", "/invocations/*/stderr",
    "/governance/pii_description", "/author_response/note",
)


def _mask_pointer(node: Any, tokens: list[str]) -> None:
    """Replace the value at a JSON pointer (``*`` matches every array index or object key)."""
    if not tokens:
        return
    head, rest = tokens[0], tokens[1:]
    if isinstance(node, list):
        indices = range(len(node)) if head == "*" else ([int(head)] if head.isdigit() and int(head) < len(node) else [])
        for index in indices:
            if rest:
                _mask_pointer(node[index], rest)
            else:
                node[index] = REDACTION_PLACEHOLDER
    elif isinstance(node, dict):
        keys = list(node.keys()) if head == "*" else ([head] if head in node else [])
        for key in keys:
            if rest:
                _mask_pointer(node[key], rest)
            else:
                node[key] = REDACTION_PLACEHOLDER


def apply_declared_redactions(data: bytes) -> bytes | None:
    """For an internal metadata record (run.json, comment.json) that declares
    ``publication_redaction.fields_requiring_removal``, return the record with those fields masked.
    Those fields are removed before any extract is released, so they are inspected as removed; every
    undeclared field is still inspected verbatim. Returns None when the file declares nothing."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    block = obj.get("publication_redaction")
    pointers = (block or {}).get("fields_requiring_removal") if isinstance(block, dict) else None
    if not isinstance(pointers, list) or not pointers:
        return None
    masked = False
    for pointer in pointers:
        if not isinstance(pointer, str) or not pointer.startswith("/"):
            continue
        if pointer not in REDACTABLE_POINTER_PREFIXES:
            # Declaring an undeclarable field is not a way to pass the gate; the field is
            # inspected verbatim and the release fails on it if it carries private data.
            continue
        tokens = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
        _mask_pointer(obj, tokens)
        masked = True
    if not masked:
        return None
    return json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")


def inspect_file(path: pathlib.Path, label: str | None = None, *, identifiers: bool = False) -> list[Issue]:
    label = label or path.as_posix()
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        data = path.read_bytes()
        if suffix == ".json" and path.name in {"run.json", "comment.json"}:
            masked = apply_declared_redactions(data)
            if masked is not None:
                data = masked
        return inspect_text_bytes(data, label, identifiers=identifiers)
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


NEUTRALISATION_NOTE = "RELEASE_PATH_NEUTRALISATION.txt"


def neutralise_tree(root: pathlib.Path, prefixes: Iterable[str],
                    tree_prefixes: Iterable[str] = ()) -> list[tuple[str, int]]:
    """Rewrite audit-root prefixes in every text file under `root`; return [(relpath, count)].

    Callers scope this to the working-source subtree. Outside it, a private path means a report,
    a manifest or a gate script leaked one, and that must fail the release rather than be tidied.
    """
    # The neutraliser must read what the detector reads, or a file it skips is exactly the file
    # the detector then rejects. The first version read UTF-8 text and silently `continue`d on a
    # decode error, while `_decode_text` falls back to latin-1 -- so a cp1252 script from a
    # non-English lab that named the audit's own root was not rewritten and then blocked the
    # release, with "absolute path(s) naming a real account" and no hint why. It also wrote back
    # in text mode, so a CRLF script came out LF. Bytes in, the module's own decoder, the
    # detector's NFC normalisation for the match, bytes out in the same encoding.
    def _variants(values):
        found: set[str] = set()
        for prefix in values:
            if prefix:
                found.add(prefix)
                found.add(unicodedata.normalize("NFC", prefix))
                found.add(unicodedata.normalize("NFD", prefix))
        return found
    variants = _variants(list(prefixes))
    tree_variants = _variants(list(tree_prefixes))
    rewritten: list[tuple[str, int]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = data.decode("latin-1")
            encoding = "latin-1"
        out, count = neutralise_audit_paths(text, variants, tree_variants)
        if count:
            path.write_bytes(out.encode(encoding))
            rewritten.append((path.relative_to(root).as_posix(), count))
    return rewritten


def write_neutralisation_note(release_root: pathlib.Path,
                              rewritten: list[tuple[str, int]]) -> pathlib.Path | None:
    """Record the substitutions in the release. A rewrite nobody can see is a silent edit."""
    if not rewritten:
        return None
    lines = [
        "Audit paths neutralised at release staging",
        "",
        "These working scripts named the audit snapshot by its absolute path on the machine that",
        f"ran the audit. Each audit-root prefix below was replaced with {AUDIT_PLACEHOLDER!r};",
        "nothing else in the files was changed. Absolute paths pointing anywhere OTHER than the",
        "audit's own directory are never rewritten -- they block the release instead. The",
        "audit-side originals are unmodified and still match the sha256 recorded in their role",
        "evidence manifest.",
        "",
    ]
    for name, count in rewritten:
        lines.append(f"  {name}  ({count} path{'s' if count != 1 else ''})")
    note = release_root / NEUTRALISATION_NOTE
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return note


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
    parser.add_argument("--allow-emails-from", action="append", default=[], type=pathlib.Path,
                        help="a file whose printed email addresses are public (e.g. the article text); repeatable")
    args = parser.parse_args()
    allow_emails_from(args.root / "report" / "front_matter.md")
    for path in args.allow_emails_from:
        allow_emails_from(path)
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
