#!/usr/bin/env python3
"""Build-time and release-boundary enforcement for I4R report documents.

The canonical interface is ``document_build_manifest.json`` schema 3.  It binds the
editable inputs, immutable policy snapshot, builder/verifier bytes, renderer identities,
exact DOCX/PDF bytes, page boundaries, and every deterministic validation result.

This module deliberately has no PDF parser fallback.  A release build is valid only when
``pdfinfo`` and ``pdftotext`` inspect the exact PDF that will be promoted or published.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import xml.etree.ElementTree as ElementTree
import zipfile
from collections import Counter
from typing import Any, Iterable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    import lock_wait  # noqa: E402
except ImportError:  # a standalone copy (e.g. a release's scripts/) without its sibling helper
    class lock_wait:  # noqa: N801
        @staticmethod
        def acquire_exclusive(handle, label):
            import fcntl as _fcntl
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)


MANIFEST_SCHEMA = "3.0.0"
DIAGNOSTIC_SCHEMA = "1.0.0"
CANONICAL_BODY_CAP = 10
HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_BUILDER = HERE / "build_docx.sh"
DEFAULT_CACHE_HELPER = HERE / "document_build_cache.py"


class DocumentReleaseError(ValueError):
    """A fail-closed document or release-contract violation."""


class ValidationFailure(DocumentReleaseError):
    def __init__(self, message: str, validations: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.validations = validations


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_bytes(path: pathlib.Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def strict_json(path: pathlib.Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise DocumentReleaseError(f"required regular file is missing or symlinked: {path}")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise DocumentReleaseError(f"duplicate JSON key {key!r} in {path}")
            value[key] = item
        return value

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DocumentReleaseError(f"invalid JSON in {path}: {exc}") from exc


def regular_file(path: pathlib.Path, label: str) -> pathlib.Path:
    if path.is_symlink() or not path.is_file():
        raise DocumentReleaseError(f"{label} must be a regular, non-symlink file: {path}")
    return path


def under_root(root: pathlib.Path, path: pathlib.Path, label: str) -> pathlib.Path:
    root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise DocumentReleaseError(f"{label} escapes the audit root: {path}") from exc
    return resolved


def safe_relative(root: pathlib.Path, relative: Any, label: str) -> pathlib.Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative or
            pathlib.PurePosixPath(relative).is_absolute() or ".." in pathlib.PurePosixPath(relative).parts):
        raise DocumentReleaseError(f"unsafe {label} path in document manifest: {relative!r}")
    path = (root / pathlib.PurePosixPath(relative)).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise DocumentReleaseError(f"{label} path escapes the audit root: {relative}") from exc
    return regular_file(path, label)


def artifact_record(root: pathlib.Path, source: pathlib.Path, relative: str | None = None) -> dict[str, Any]:
    source = regular_file(source, "artifact")
    if relative is None:
        source = under_root(root, source, "artifact")
        relative = source.relative_to(root.resolve()).as_posix()
    else:
        # Validate the declared destination even when source bytes are still private.
        candidate = (root.resolve() / pathlib.PurePosixPath(relative)).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise DocumentReleaseError(f"artifact destination escapes audit root: {relative}") from exc
    return {"path": relative, "sha256": sha256(source), "bytes": source.stat().st_size}


def verify_record(root: pathlib.Path, record: Any, label: str) -> pathlib.Path:
    if not isinstance(record, dict) or set(record) != {"path", "sha256", "bytes"}:
        raise DocumentReleaseError(f"document manifest {label} record is malformed")
    path = safe_relative(root, record.get("path"), label)
    if record.get("sha256") != sha256(path) or record.get("bytes") != path.stat().st_size:
        converged = False
        stop = None
        try:
            if label != "report":
                # only a REPORT edit is something the converged gate refuses; a stale PDF, DOCX, captions or
                # front matter is fixed by rebuilding the document
                raise ValueError("not the report")
            stop = json.loads((root / "run.json").read_text(encoding="utf-8")).get("stopping_reason")
            converged = stop == "converged"
        except (OSError, ValueError, AttributeError):
            pass
        if converged:
            # converged means the last round reviewed the report being released: rebuilding the tail
            # after an edit ends in finalize refusing converged anyway
            raise DocumentReleaseError(
                f"document manifest {label} binding is stale: the report changed after this run finalized as "
                "converged. Revert the edit (converged ships only the report the last round reviewed), or run "
                "another critique round on it; rebuilding the release tail alone cannot re-finalize as converged.")
        raise DocumentReleaseError(
            f"document manifest {label} binding is stale.\n"
            + ("  This run finalized as target_complete: after the last round only projection changes (Appendix A,\n"
               "  claim tags, marker ids) may reach the report. Revert a prose edit, or run another critique round\n"
               "  on it; for a projection change, continue below.\n" if stop == "target_complete" else "")
            +
            "  The built document no longer matches this input. A report edit -- including one\n"
            "  made to satisfy a critique round -- invalidates the build, the verification run and\n"
            "  the projection, in that order. Rebuild the tail before staging:\n"
            "      python3 scripts/generate_finding_artifacts.py \"$AUDIT_DIR\" --apply\n"
            "      bash scripts/build_docx.sh \"$AUDIT_DIR\" <output-name>\n"
            "      bash scripts/make_verification_notebook.sh \"$AUDIT_DIR\"\n"
            "      bash scripts/run_verification.sh \"$AUDIT_DIR\"\n"
            "  then (INSTALL.md order) approve/project again, make_settled.sh and re-log settled, rebuild the\n"
            "  last round's packet and post-delta gates, snapshot REPORT_v<N+1>.md, stage into a fresh\n"
            "  directory, re-log delivery (bash scripts/log_event.sh delivery artifact_path=report/<output-name>.pdf\n"
            "  recipient=...) and then phase_end phase=release, and re-run finalize_run.sh before publishing.")
    return path


def policy_snapshot(root: pathlib.Path) -> tuple[dict[str, Any], pathlib.Path, str]:
    path = root / "pipeline-policy.snapshot.json"
    envelope = strict_json(path)
    if not isinstance(envelope, dict):
        raise DocumentReleaseError("policy snapshot must be a JSON object")
    policy = envelope.get("policy")
    if not isinstance(policy, dict):
        raise DocumentReleaseError("policy snapshot lacks a policy object")
    policy_hash = sha256_bytes(json.dumps(policy, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False).encode("utf-8"))
    if envelope.get("snapshot_version") != "1" or envelope.get("policy_sha256") != policy_hash:
        raise DocumentReleaseError("audit-scoped policy snapshot hash is invalid")
    document = policy.get("document")
    cap = document.get("body_max_rendered_pages") if isinstance(document, dict) else None
    if not isinstance(cap, int) or isinstance(cap, bool) or cap != CANONICAL_BODY_CAP:
        raise DocumentReleaseError(
            f"canonical policy snapshot must set document.body_max_rendered_pages={CANONICAL_BODY_CAP}"
        )
    return policy, path, policy_hash


def canonical_cap(root: pathlib.Path) -> int:
    policy, _, _ = policy_snapshot(root)
    return int(policy["document"]["body_max_rendered_pages"])


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise DocumentReleaseError(f"required exact-PDF tool is unavailable: {name}")
    return path


def run_tool(command: list[str], label: str) -> str:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DocumentReleaseError(f"{label} failed: {exc}") from exc
    if result.returncode:
        message = (result.stderr or result.stdout).strip().replace("\n", " ")[:500]
        raise DocumentReleaseError(f"{label} failed with exit {result.returncode}: {message}")
    return result.stdout


def exact_page_count(pdf: pathlib.Path) -> int:
    regular_file(pdf, "PDF")
    output = run_tool([require_tool("pdfinfo"), str(pdf)], "pdfinfo page count")
    matches = re.findall(r"(?m)^Pages:\s*([0-9]+)\s*$", output)
    if len(matches) != 1:
        raise DocumentReleaseError("pdfinfo did not report exactly one Pages value")
    pages = int(matches[0])
    if pages < 1:
        raise DocumentReleaseError("the exact PDF has no pages")
    return pages


def page_texts(pdf: pathlib.Path, total_pages: int) -> list[str]:
    executable = require_tool("pdftotext")
    result: list[str] = []
    for page in range(1, total_pages + 1):
        output = run_tool(
            [executable, "-f", str(page), "-l", str(page), "-layout", str(pdf), "-"],
            f"pdftotext page {page}",
        )
        result.append(output.replace("\f", "\n"))
    return result


def words(text: str) -> list[str]:
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    return [value.lower() for value in re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?", text)]


def contains_sequence(haystack: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(haystack[index:index + width] == needle for index in range(len(haystack) - width + 1))


def markdown_abstract(report_text: str) -> tuple[str, list[str]]:
    match = re.search(
        r"(?ms)^##\s+(Abstract|Executive summary)\s*$\s*(.*?)(?=^##\s+|\Z)", report_text
    )
    if not match:
        raise DocumentReleaseError("REPORT_final.md lacks an Abstract or Executive summary section")
    body = match.group(2)
    body = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", body)
    body = re.sub(r"<[^>]+>|[`*_#]", " ", body)
    tokens = words(body)
    if len(tokens) < 6:
        raise DocumentReleaseError("the abstract is too short for an exact page-boundary check")
    return match.group(1), tokens[-min(12, len(tokens)):]


def abstract_end_page(report_text: str, texts: list[str]) -> int:
    _, tail = markdown_abstract(report_text)
    matches = [index for index, text in enumerate(texts, 1) if contains_sequence(words(text), tail)]
    if len(matches) != 1 and len(tail) > 8:
        shorter = tail[-8:]
        matches = [index for index, text in enumerate(texts, 1)
                   if contains_sequence(words(text), shorter)]
    if len(matches) != 1:
        raise DocumentReleaseError(
            f"could not locate the abstract ending on exactly one PDF page (matches={matches})"
        )
    return matches[0]


BACK_HEADING = re.compile(
    r"(?im)^##\s+(Data\s+and\s+Code\s+Availability|References|Appendix(?:\s+[^\n]+)?)\s*$"
)


def first_back_matter_heading(report_text: str) -> str | None:
    match = BACK_HEADING.search(report_text)
    return match.group(1).strip() if match else None


# A rendered back-matter heading is the whole line: "Data and Code Availability" or "References" alone, or "Appendix"
# with an optional label, alone or with the label's period or colon and a title ("Appendix A. Summary of findings").
BACK_MATTER_HEADING_LINE = re.compile(
    r"(?i:data and code availability|references)"
    r"|Appendix(?: [A-Z0-9]{1,3})?"
    r"|Appendix [A-Z0-9]{1,3}[.:] \S.*"
)


def pdf_line_starts_back_matter(line: str) -> bool:
    """Return whether the first rendered line is a canonical back-matter heading.

    The whole line must be the heading. Body text that only begins with one of its words after a page break is not
    one: on the MacIntyre 2026 audit, body page 8 opened mid-sentence with "appendix output and pkg/FS_during.xlsx",
    and publish_completed.sh's recount refused a release that build, staging and finalize had all accepted.
    """
    return BACK_MATTER_HEADING_LINE.fullmatch(" ".join(line.split())) is not None


def report_document_entries(root: pathlib.Path, suffix: str) -> list[pathlib.Path]:
    """List direct report documents case-insensitively, including unsafe entry types."""
    report = root / "report"
    if report.is_symlink() or not report.is_dir():
        raise DocumentReleaseError(f"report/ is missing, symlinked, or not a directory: {report}")
    return sorted(
        (path for path in report.iterdir() if path.suffix.lower() == suffix.lower()),
        key=lambda path: path.name,
    )


def verify_report_document_inventory(
    root: pathlib.Path, pdf: pathlib.Path, docx: pathlib.Path | None,
) -> None:
    """Require exactly the bound PDF and optional DOCX, regardless of suffix case."""
    pdf_entries = report_document_entries(root, ".pdf")
    if any(path.is_symlink() or not path.is_file() for path in pdf_entries):
        raise DocumentReleaseError("report/ contains a symlinked or non-regular PDF entry")
    publishable_pdfs = sorted(path.resolve() for path in pdf_entries)
    if publishable_pdfs != [pdf.resolve(strict=False)]:
        raise DocumentReleaseError(
            "report/ must contain exactly the single manifest-bound PDF; extra/proxy PDFs are forbidden"
        )

    docx_entries = report_document_entries(root, ".docx")
    if any(path.is_symlink() or not path.is_file() for path in docx_entries):
        raise DocumentReleaseError("report/ contains a symlinked or non-regular DOCX entry")
    publishable_docx = sorted(path.resolve() for path in docx_entries)
    expected_docx = [] if docx is None else [docx.resolve(strict=False)]
    if publishable_docx != expected_docx:
        raise DocumentReleaseError("report/ contains an extra or unbound DOCX")


def preflight_report_document_inventory(
    root: pathlib.Path, pdf_target: pathlib.Path, docx_target: pathlib.Path,
) -> None:
    """Reject extra documents before an expensive render while allowing absent targets."""
    root = root.resolve(strict=True)
    report = (root / "report").resolve(strict=True)
    targets = {pdf_target.resolve(strict=False), docx_target.resolve(strict=False)}
    if any(target.parent != report for target in targets):
        raise DocumentReleaseError("canonical PDF/DOCX targets must be direct children of report/")
    for suffix in (".pdf", ".docx"):
        for candidate in report_document_entries(root, suffix):
            if candidate.is_symlink() or not candidate.is_file():
                raise DocumentReleaseError(
                    f"report/ contains a symlinked or non-regular {suffix[1:].upper()} entry"
                )
            if candidate.resolve() not in targets:
                raise DocumentReleaseError(
                    f"extra unbound document in report/: {candidate.name}; "
                    "move diagnostics below report/previews"
                )


def page_lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def locate_back_matter(report_text: str, texts: list[str]) -> tuple[int | None, int, int]:
    heading = first_back_matter_heading(report_text)
    total = len(texts)
    if heading is None:
        return None, total, 0
    heading_words = words(heading)
    candidates: list[tuple[int, int]] = []
    for page_number, text in enumerate(texts, 1):
        lines = page_lines(text)
        for line_number, line in enumerate(lines):
            line_words = words(line)
            if line_words[:len(heading_words)] == heading_words:
                candidates.append((page_number, line_number))
                break
    if not candidates:
        raise DocumentReleaseError(f"back-matter heading {heading!r} is absent from the exact PDF")
    first_page = min(page for page, _ in candidates)
    first_offsets = [offset for page, offset in candidates if page == first_page]
    if min(first_offsets) != 0:
        raise DocumentReleaseError(
            f"ambiguous back-matter boundary: {heading!r} does not begin PDF page {first_page}"
        )
    if first_page <= 1:
        raise DocumentReleaseError("back matter begins before the report body has a page")
    return first_page, first_page - 1, total - first_page + 1


def markdown_table_count(report_text: str) -> int:
    lines = report_text.splitlines()
    count = 0
    index = 0
    separator = re.compile(r"^\s*\|?\s*:?-{3,}")
    while index < len(lines):
        if lines[index].lstrip().startswith("|"):
            block: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                block.append(lines[index])
                index += 1
            if len(block) >= 2 and separator.search(block[1]):
                count += 1
            continue
        index += 1
    return count


def docx_table_count(docx: pathlib.Path) -> int:
    regular_file(docx, "DOCX")
    try:
        with zipfile.ZipFile(docx) as archive:
            if archive.testzip() is not None:
                raise DocumentReleaseError("DOCX has a CRC failure")
            document = archive.read("word/document.xml")
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as exc:
        raise DocumentReleaseError(f"DOCX is not a readable OOXML package: {exc}") from exc
    return len(re.findall(br"<(?:w:)?tbl(?:\s|>)", document))


def captions(path: pathlib.Path) -> list[str]:
    return [line.strip() for line in regular_file(path, "captions file").read_text(
        encoding="utf-8").splitlines() if line.strip()]


def caption_is_present(number: int, caption: str, all_pdf_words: list[str]) -> bool:
    prefix = words(f"Table {number} {caption}")[:10]
    return contains_sequence(all_pdf_words, prefix)


# Appendix A findings table: literal rendering and a build-time integrity check.
#
# generate_finding_artifacts.render() writes this table. It is post-delta gate code and keeps its
# bytes, so no receipt or projection goes stale. Its cells are raw claim text with only '|'
# escaped, and its separator leaves the #, Status and Section columns too narrow. Pandoc therefore
# read claim text as markdown ('F*df1/(F*df1+df2)' became italics and lost its asterisks), and
# LibreOffice broke words across lines ('CONFIR MED', 'Sectio n', two-digit numbers). build_docx.sh
# now rewrites the table in its converter input only; REPORT_final.md is never changed.
FINDINGS_TABLE_HEADER = "| # | Finding | Status | Changes a published conclusion? | Section |"
FINDINGS_TABLE_COLUMNS = ("#", "Finding", "Status", "Changes a published conclusion?", "Section")
# Relative widths #/Finding/Status/Changes/Section = 0.055/0.425/0.205/0.155/0.16, written as
# dash-plus-colon counts that sum to 200 (half-percent units). Pandoc applies them only when some
# line of the table is longer than --columns (72 by default). This separator line is 206
# characters, so it always is. On the narrowest page the build meets (A4, 451pt of text, less
# 10.8pt of cell margins) each narrow column holds its widest fixed text on one line: '#' two
# digits (12pt in 14.0pt), Status 'SPECULATIVE' (80.7pt in 81.7pt), Changes the header word
# 'conclusion?' (57.3pt in 59.1pt) and Section 'Section 10.2' or 'Section 4.10' (60pt in 61.4pt).
# 2.6.8's first widths (0.06/0.43/0.22/0.17/0.12) printed every 'Section 10' as 'Section' above
# '10'. The widths are asserted against TIMES_WIDTHS by tests/test_appendix_rendering.py.
FINDINGS_TABLE_SEPARATOR = ("|" + "-" * 10 + ":|" + "-" * 85 + "|" + "-" * 41 + "|:" + "-" * 29
                            + ":|" + "-" * 32 + "|")
FINDINGS_TABLE_WIDTHS = (0.055, 0.425, 0.205, 0.155, 0.16)
PIPE_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}")
# The bottom line of the findings table's header row in the exact PDF's layout text; the row
# repeats at the top of every page the table reaches.
TABLE_HEADER_LINE = re.compile(r"(?m)^[ \t]*#[ \t]+Finding[ \t]+Status\b")
# Every character that pandoc's markdown reader can give a meaning inside a table cell. Under
# all_symbols_escapable a backslash before any of them renders it literally. Quotes are escaped
# too: smart quotes can move a space out of a quotation (a quoted value 'y ' rendered as 'y'),
# which changes the claim. '--', '---' and '...' stay typographic (they print as an en dash, an
# em dash and an ellipsis, as in body text); that only substitutes a glyph, and normalized_cell()
# maps it back.
MARKDOWN_ACTIVE = re.compile(r"([\\`*_\[\]<>$~^@&|'\"])")
# build_docx.sh strips legacy '[claim N]' tags from its whole converter input and then replaces
# '§N' with 'Section N'. An escaped tag would survive that strip, so findings cells lose their
# tags before they are escaped, and the rendered text is modelled in that order.
CLAIM_TAG = re.compile(r"\s?\[claim \d+\]")
SMART_TYPOGRAPHY = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "--", "—": "---", "…": "...", " ": " ",
}
SOFT_HYPHEN = "\u00ad"
# Format characters (Unicode category Cf: zero-width space, word joiner, zero-width joiners,
# direction marks, the soft hyphen) print nothing, and pdftotext drops them, so the PDF check
# deletes them on both sides. Two of them are also line-break opportunities: LibreOffice may break
# a word at a zero-width space, or at a soft hyphen, which then prints as '-'. A run holding them
# is accepted unbroken or broken at any of them; one with more than MAX_BREAK_OPPORTUNITIES is not
# checked in the PDF, as a word wider than the column is not.
BREAK_OPPORTUNITIES = frozenset({"\u200b", SOFT_HYPHEN})
MAX_BREAK_OPPORTUNITIES = 8
# Bidirectional controls (marks, embeddings, overrides, isolates) are Cf too, but pdftotext wraps
# a right-to-left run in them and writes no space after the run, so for tokenizing they are a word
# boundary, not nothing: deleting them would join a Hebrew or Arabic word to the next word.
BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
# Scripts written without spaces between words (Han, kana, bopomofo, Hangul, Thai, Lao, Myanmar,
# Khmer) may break between any two characters, so each such character is a token of its own.
UNSPACED_CHARACTER = re.compile(
    "[\u0e00-\u0eff\u1000-\u109f\u1100-\u11ff\u1780-\u17ff\u2e80-\u2fdf\u3040-\u30ff"
    "\u3100-\u312f\u3130-\u318f\u31a0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\ua960-\ua97f"
    "\uac00-\ud7ff\uf900-\ufaff\U00020000-\U0003134f]"
)
WORDML = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# A word of the Finding column that is wider than the column itself (a long file name, say) has no
# line-break opportunity, so LibreOffice must break it somewhere; that is not a defect of the
# widths. Such runs are measured with Liberation Serif's advance widths (1/1000 em; LibreOffice
# renders the reference document's text in Liberation Serif here, and it is metric-compatible with
# Times) at 12pt, against the Finding column on the narrowest page the build meets (A4 with 1in
# margins, 451pt of text) less pandoc's 108-twip cell margins on each side. Any other character
# counts as a full em, so an unknown glyph can only widen the exemption, never refuse a build.
TIMES_WIDTHS = dict(zip(
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~",
    (250, 333, 408, 500, 500, 833, 778, 333, 333, 333, 500, 564, 250, 333, 250, 278,
     500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 278, 278, 564, 564, 564, 444,
     921, 722, 667, 667, 722, 611, 556, 722, 722, 333, 389, 722, 611, 889, 722, 722,
     556, 722, 667, 556, 611, 722, 722, 944, 722, 722, 611, 333, 278, 333, 469, 500,
     333, 444, 500, 444, 500, 444, 333, 500, 500, 278, 278, 500, 278, 778, 500, 500,
     500, 500, 333, 389, 278, 500, 500, 722, 500, 500, 444, 480, 200, 480, 541),
))
TABLE_FONT_POINTS = 12.0
NARROWEST_TEXT_WIDTH_POINTS = 451.0
CELL_PADDING_POINTS = 10.8
# Runs wider than 90% of the column are exempt, so a width estimate a few percent low cannot
# refuse a word that LibreOffice was forced to break. The four narrow columns hold only fixed
# vocabulary that fits by design, and every word in them is always checked.
FINDING_RUN_LIMIT_POINTS = 0.9 * (FINDINGS_TABLE_WIDTHS[1] * NARROWEST_TEXT_WIDTH_POINTS
                                  - CELL_PADDING_POINTS)


def run_points(run: str) -> float:
    return sum(TIMES_WIDTHS.get(character, 1000) for character in run) * TABLE_FONT_POINTS / 1000


def markdown_literal(text: str) -> str:
    """Backslash-escape every pandoc-active character, so that ``text`` renders literally."""
    return MARKDOWN_ACTIVE.sub(r"\\\1", text)


def findings_table_cells(line: str) -> list[str]:
    """Split one generated pipe-table row into its raw cells.

    This is the exact inverse of the generator's clean_cell(), which escapes only '|' (as '\\|'):
    split on unescaped pipes, strip each cell, and turn '\\|' back into '|'.
    """
    row = line.strip()
    if len(row) < 2 or not row.startswith("|") or not re.search(r"(?<!\\)\|$", row):
        raise DocumentReleaseError(f"findings-table row is not a closed pipe-table row: {row[:80]!r}")
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", row[1:-1])]


def is_findings_table(block: list[str]) -> bool:
    """Whether a '|' block of the report is the generated findings table."""
    return (len(block) >= 2 and block[0].strip() == FINDINGS_TABLE_HEADER
            and bool(PIPE_TABLE_SEPARATOR.search(block[1])))


def findings_table_rows(block: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for number, line in enumerate(block[2:], 1):
        cells = findings_table_cells(line)
        if len(cells) != len(FINDINGS_TABLE_COLUMNS):
            raise DocumentReleaseError(
                f"findings-table row {number} has {len(cells)} cells, but the generated table has "
                f"{len(FINDINGS_TABLE_COLUMNS)}; regenerate Appendix A with "
                "generate_finding_artifacts.py --apply"
            )
        rows.append(cells)
    return rows


def pandoc_findings_table(block: list[str]) -> list[str]:
    """Return the converter-input form of one '|' block of the report.

    Any block other than the generated findings table (exact header, then a separator) comes
    back unchanged. The findings table gets the fixed-width separator and literal cells: legacy
    claim tags are removed first, then every pandoc-active character is escaped.
    """
    if not is_findings_table(block):
        return list(block)
    output = [FINDINGS_TABLE_HEADER, FINDINGS_TABLE_SEPARATOR]
    for cells in findings_table_rows(block):
        output.append("| " + " | ".join(
            markdown_literal(CLAIM_TAG.sub("", cell).strip()) for cell in cells
        ) + " |")
    return output


def normalized_cell(text: str) -> str:
    """Undo smart typography and collapse whitespace, the same way for source and DOCX text."""
    for typographic, plain in SMART_TYPOGRAPHY.items():
        text = text.replace(typographic, plain)
    return " ".join(text.split())


def rendered_cell_text(raw: str) -> str:
    """The normalized text the exact DOCX must show for one raw findings-table cell.

    This models build_docx.sh in its order: the claim tag is stripped before escaping, the escaped
    cell renders literally, and then the whole-text '§' substitution applies.
    """
    text = CLAIM_TAG.sub("", raw).strip()
    text = re.sub(r"§(\d+)\.(\d+)", r"Section \1.\2", text)
    text = re.sub(r"§(\d+)", r"Section \1", text)
    return normalized_cell(text)


def report_findings_tables(report_text: str) -> list[list[list[str]]]:
    """Expected rendered rows (header first) of every generated findings table in the report.

    Blocks are grouped exactly as build_docx.sh groups them: consecutive lines that start with '|'.
    """
    tables: list[list[list[str]]] = []
    lines = report_text.split("\n")
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            index += 1
            continue
        end = index
        while end < len(lines) and lines[end].lstrip().startswith("|"):
            end += 1
        block = lines[index:end]
        if is_findings_table(block):
            tables.append([list(FINDINGS_TABLE_COLUMNS)] + [
                [rendered_cell_text(cell) for cell in row] for row in findings_table_rows(block)
            ])
        index = end
    return tables


def docx_cell_text(cell: ElementTree.Element) -> str:
    paragraphs: list[str] = []
    for paragraph in cell.iter(WORDML + "p"):
        text: list[str] = []
        for run in paragraph.iter(WORDML + "r"):
            for node in run:
                if node.tag == WORDML + "t":
                    text.append(node.text or "")
                elif node.tag == WORDML + "tab":
                    text.append("\t")
                elif node.tag in (WORDML + "br", WORDML + "cr"):
                    text.append(" ")
                elif node.tag == WORDML + "noBreakHyphen":
                    text.append("-")
                elif node.tag == WORDML + "softHyphen":
                    # pandoc writes a claim's U+00AD as this element. Reading it back as the
                    # character keeps the soft hyphen on both sides, where the PDF check needs it
                    # as a place the word may break.
                    text.append(SOFT_HYPHEN)
        paragraphs.append("".join(text))
    return normalized_cell(" ".join(paragraphs))


def docx_tables(docx: pathlib.Path) -> list[list[list[str]]]:
    """Normalized cell text of every DOCX table, row by row."""
    regular_file(docx, "DOCX")
    try:
        with zipfile.ZipFile(docx) as archive:
            document = archive.read("word/document.xml")
        root = ElementTree.fromstring(document)
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise DocumentReleaseError(f"DOCX is not a readable OOXML package: {exc}") from exc
    return [[[docx_cell_text(cell) for cell in row.findall(WORDML + "tc")]
             for row in table.findall(WORDML + "tr")]
            for table in root.iter(WORDML + "tbl")]


def table_tokens(text: str) -> list[str]:
    """Lowercase alphanumeric runs. Hyphens, slashes and all other punctuation separate them.

    Unlike words(), which feeds the manifest validations and must not change, a hyphen never
    joins two tokens, so a word hyphenated in the source may break at its hyphen in the PDF.
    Format characters (category Cf) are deleted first, because they print nothing and pdftotext
    drops them, and each character of a script written without spaces is a token of its own,
    because a line may break between any two of them. Bidirectional controls are the exception:
    pdftotext wraps a right-to-left run in them and writes no space before the next word, so they
    mark a word boundary and become a space instead of joining two words into one token.
    """
    text = "".join(" " if character in BIDI_CONTROLS else character
                   for character in unicodedata.normalize("NFKC", text)
                   if character in BIDI_CONTROLS or unicodedata.category(character) != "Cf")
    return re.findall(r"[^\W_]+", UNSPACED_CHARACTER.sub(r" \g<0> ", text).lower())


def run_tokenizations(run: str) -> list[Counter[str]] | None:
    """The token multisets one whitespace-free run of a cell may print as, unbroken first.

    A run without a zero-width space or soft hyphen has exactly one. Each of them is a place
    where LibreOffice may break the run across lines, so every choice of breaks is listed. None
    means the run has more than MAX_BREAK_OPPORTUNITIES of them and is not checked in the PDF.
    """
    positions = [index for index, character in enumerate(run) if character in BREAK_OPPORTUNITIES]
    if len(positions) > MAX_BREAK_OPPORTUNITIES:
        return None
    options: list[Counter[str]] = []
    for mask in range(2 ** len(positions)):
        characters = list(run)
        for bit, position in enumerate(positions):
            if mask >> bit & 1:
                characters[position] = " "
        option = Counter(table_tokens("".join(characters)))
        if option not in options:
            options.append(option)
    return options


def appendix_a_page(texts: list[str]) -> int:
    for number, text in enumerate(texts, 1):
        lines = page_lines(text)
        if lines and table_tokens(lines[0])[:2] == ["appendix", "a"]:
            return number
    raise DocumentReleaseError("no page of the exact PDF begins with the Appendix A heading")


def first_difference(expected: str, actual: str) -> str:
    index = next((position for position, (left, right) in enumerate(zip(expected, actual))
                  if left != right), min(len(expected), len(actual)))
    start = max(0, index - 24)
    return (f"at character {index + 1}: expected {expected[start:index + 24]!r}, "
            f"found {actual[start:index + 24]!r}")


def findings_table_integrity(report: pathlib.Path, docx: pathlib.Path | None,
                             pdf: pathlib.Path) -> str:
    """Refuse a build whose Appendix A table did not render as its literal source text.

    DOCX: every cell of each generated findings table equals the text the build must render
    (font-independent and exact after the shared normalization). PDF: every table word and number
    is whole on the pages from the Appendix A heading to the end, so no column broke a word across
    lines. A report without a generated findings table passes before any file is read.
    """
    expected = report_findings_tables(
        regular_file(report, "report source").read_text(encoding="utf-8"))
    if not expected:
        return "findings table integrity: the report has no generated findings table; nothing to check"
    if docx is None:
        raise DocumentReleaseError("the findings table cannot be checked without the exact DOCX")
    actual = [table for table in docx_tables(docx)
              if table and table[0] == list(FINDINGS_TABLE_COLUMNS)]
    if len(actual) != len(expected):
        raise DocumentReleaseError(
            f"the exact DOCX has {len(actual)} findings table(s), but the report has {len(expected)}"
        )
    for table_number, (wanted, found) in enumerate(zip(expected, actual), 1):
        if len(found) != len(wanted):
            raise DocumentReleaseError(
                f"DOCX findings table {table_number} has {len(found) - 1} row(s), but the report "
                f"has {len(wanted) - 1}"
            )
        for row_number, (wanted_row, found_row) in enumerate(zip(wanted[1:], found[1:]), 1):
            if len(found_row) != len(wanted_row):
                raise DocumentReleaseError(
                    f"DOCX findings table {table_number} row {row_number} has {len(found_row)} "
                    f"cell(s), expected {len(wanted_row)}"
                )
            for column, wanted_cell, found_cell in zip(FINDINGS_TABLE_COLUMNS, wanted_row, found_row):
                if wanted_cell != found_cell:
                    raise DocumentReleaseError(
                        f"DOCX findings table {table_number} row {row_number} ({column}) is not the "
                        f"literal source text {first_difference(wanted_cell, found_cell)}; the "
                        "converter read the cell as markdown"
                    )
    required: Counter[str] = Counter()
    # Runs that may print unbroken or broken at a zero-width space or soft hyphen: any one of
    # their tokenizations must be whole in the PDF.
    alternatives: list[list[Counter[str]]] = []
    too_wide = 0
    unchecked = 0
    for table in expected:
        for row in table[1:]:
            for column_number, cell in enumerate(row):
                for run in cell.split():
                    if column_number == 1 and run_points(run) > FINDING_RUN_LIMIT_POINTS:
                        too_wide += 1
                        continue
                    options = run_tokenizations(run)
                    if options is None:
                        unchecked += 1
                    elif len(options) == 1:
                        required.update(options[0])
                    else:
                        alternatives.append(options)
    total = exact_page_count(pdf)
    texts = page_texts(pdf, total)
    first = appendix_a_page(texts)
    appendix = texts[first - 1:]
    # The header row repeats on every page the table reaches, and its words ('Section',
    # 'published') also occur in the cells. Require them once per printed header, so that the
    # repeats cannot stand in for a cell word broken across lines.
    headers = max(len(expected), sum(len(TABLE_HEADER_LINE.findall(text)) for text in appendix))
    for token, count in Counter(table_tokens(" ".join(FINDINGS_TABLE_COLUMNS))).items():
        required[token] += count * headers
    available = Counter(token for text in appendix for token in table_tokens(text))
    missing = required - available
    spare = available - required
    checked = sum(required.values())
    for options in alternatives:
        chosen = next((option for option in options if not option - spare), None)
        if chosen is None:
            missing.update(options[0])
        else:
            spare -= chosen
            checked += sum(chosen.values())
    if missing:
        sample = ", ".join(repr(token) for token in sorted(missing)[:8])
        raise DocumentReleaseError(
            f"{sum(missing.values())} word(s) of the Appendix A findings table are not whole on "
            f"PDF pages {first}-{total} ({sample}): a column too narrow for a word breaks it across "
            "lines, as in 'CONFIR'/'MED'. The build sets the column widths; a font wider than "
            "Liberation Serif/Times at 12pt (for example an installed Aptos) can still overflow "
            "them. If the listed words print whole, a character of the claim text that the PDF "
            "prints differently is the cause instead: report it as a build defect, and do not "
            "edit the claim, which the verifier's records bind"
        )
    rows = sum(len(table) - 1 for table in expected)
    wide = (f"; {too_wide} Finding word(s) wider than the column may break anywhere"
            if too_wide else "")
    if unchecked:
        wide += (f"; {unchecked} word(s) with more than {MAX_BREAK_OPPORTUNITIES} zero-width "
                 "spaces or soft hyphens are not checked")
    return (f"findings table integrity: {len(expected)} table(s), {rows} row(s); DOCX cells are the "
            f"literal source text; all {checked} table words are whole on PDF pages "
            f"{first}-{total}{wide}")


def code_identity(path: pathlib.Path, name: str) -> dict[str, Any]:
    regular_file(path, name)
    return {"name": name, "sha256": sha256(path), "bytes": path.stat().st_size}


def validation(name: str, passed: bool, detail: str, *, status: str | None = None) -> dict[str, Any]:
    return {"name": name, "status": status or ("passed" if passed else "failed"), "detail": detail}


def validate_exact_document(
    *, report: pathlib.Path, captions_path: pathlib.Path, docx: pathlib.Path | None,
    pdf: pathlib.Path, cap: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    validations: list[dict[str, Any]] = []
    report_text = regular_file(report, "report source").read_text(encoding="utf-8")
    try:
        total = exact_page_count(pdf)
        validations.append(validation("exact_pdf_page_count", True, f"pdfinfo counted {total} page(s)"))
        texts = page_texts(pdf, total)
        validations.append(validation("exact_pdf_text_extraction", True,
                                      f"pdftotext extracted all {total} page(s)"))
        first_back, body, back = locate_back_matter(report_text, texts)
        validations.append(validation(
            "back_matter_boundary", True,
            (f"first back-matter page is {first_back}" if first_back is not None
             else "the source has no back-matter section"),
        ))
        abstract_page = abstract_end_page(report_text, texts)
        abstract_ok = abstract_page == 1
        validations.append(validation("abstract_ends_on_page_one", abstract_ok,
                                      f"abstract ends on page {abstract_page}"))
        cap_ok = body <= cap
        validations.append(validation("body_page_cap", cap_ok,
                                      f"{body} body page(s), cap {cap}"))

        expected_tables = markdown_table_count(report_text)
        caption_rows = captions(captions_path)
        caption_count_ok = len(caption_rows) == expected_tables
        validations.append(validation(
            "table_caption_cardinality", caption_count_ok,
            f"{expected_tables} Markdown table(s), {len(caption_rows)} caption(s)",
        ))
        if docx is None:
            table_structure_ok = expected_tables == 0
            validations.append(validation(
                "docx_table_structure", table_structure_ok,
                ("DOCX omitted and the report has no tables" if table_structure_ok
                 else "DOCX omitted, so report tables cannot be structurally verified"),
            ))
        else:
            actual_tables = docx_table_count(docx)
            table_structure_ok = actual_tables == expected_tables
            validations.append(validation(
                "docx_table_structure", table_structure_ok,
                f"DOCX contains {actual_tables} table(s), expected {expected_tables}",
            ))
        all_pdf_words = words("\n".join(texts))
        missing_captions = [number for number, value in enumerate(caption_rows, 1)
                            if not caption_is_present(number, value, all_pdf_words)]
        captions_ok = not missing_captions
        validations.append(validation(
            "pdf_table_captions", captions_ok,
            ("all table captions are present in the exact PDF" if captions_ok
             else f"caption(s) missing from exact PDF: {missing_captions}"),
        ))
        metrics = {
            "total_pages": total,
            "body_pages": body,
            "back_matter_pages": back,
            "first_back_matter_page": first_back,
            "abstract_ending_page": abstract_page,
            "body_page_cap": cap,
        }
    except DocumentReleaseError as exc:
        validations.append(validation("document_validation", False, str(exc)))
        raise ValidationFailure(str(exc), validations) from exc

    failed = [row for row in validations if row["status"] == "failed"]
    if failed:
        raise ValidationFailure("; ".join(row["detail"] for row in failed), validations)
    return metrics, validations


def manifest_from_args(args: argparse.Namespace) -> dict[str, Any]:
    root = args.audit.resolve(strict=True)
    _, policy_path, policy_hash = policy_snapshot(root)
    cap = canonical_cap(root) if args.build_kind == "canonical" else args.cap
    if not isinstance(cap, int) or cap < 1:
        raise DocumentReleaseError("page cap must be a positive integer")
    if args.build_kind == "canonical" and cap != CANONICAL_BODY_CAP:
        raise DocumentReleaseError("canonical build cap may only come from the policy snapshot")
    if args.build_kind == "preview" and not str(args.pdf_relative).startswith("report/previews/"):
        raise DocumentReleaseError("preview PDF must be written below report/previews")

    docx = args.docx.resolve(strict=True) if args.docx else None
    metrics, validations = validate_exact_document(
        report=args.report.resolve(strict=True), captions_path=args.captions.resolve(strict=True),
        docx=docx, pdf=args.pdf.resolve(strict=True), cap=cap,
    )
    release_eligible = args.build_kind == "canonical"
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "created_at": utc_now(),
        "transaction_id": str(uuid.uuid4()),
        "build_kind": args.build_kind,
        "release_eligible": release_eligible,
        "inputs": {
            "report": artifact_record(root, args.report.resolve(strict=True)),
            "front_matter": artifact_record(root, args.front_matter.resolve(strict=True)),
            "captions": artifact_record(root, args.captions.resolve(strict=True)),
            "policy_snapshot": artifact_record(root, policy_path),
            "policy_sha256": policy_hash,
        },
        "code": {
            "builder": code_identity(args.builder.resolve(strict=True), "build_docx.sh"),
            "verifier": code_identity(pathlib.Path(__file__).resolve(), "document_release.py"),
            "cache_helper": code_identity(args.cache_helper.resolve(strict=True),
                                          "document_build_cache.py"),
        },
        "renderers": {
            "markdown_to_docx": {"name": "pandoc", "identity": args.pandoc_identity},
            "docx_to_pdf": {"name": "libreoffice", "identity": args.renderer_identity},
            "pdf_page_counter": {"name": "pdfinfo", "identity": args.pdfinfo_identity},
            "pdf_text_extractor": {"name": "pdftotext", "identity": args.pdftotext_identity},
        },
        "artifacts": {
            "pdf": artifact_record(root, args.pdf.resolve(strict=True), args.pdf_relative),
            "docx": (artifact_record(root, docx, args.docx_relative) if docx is not None else None),
        },
        "pages": metrics,
        "page_limit": {
            "value": cap,
            "source": ("hash_bound_policy_snapshot" if release_eligible else "preview_cli"),
            "release_enforced": release_eligible,
        },
        "validations": validations,
        "all_validations_passed": True,
    }
    return manifest


def write_diagnostic(path: pathlib.Path, *, build_kind: str, stage: str,
                     message: str, validations: list[dict[str, Any]] | None = None) -> None:
    payload = {
        "schema_version": DIAGNOSTIC_SCHEMA,
        "created_at": utc_now(),
        "build_kind": build_kind,
        "stage": stage,
        "status": "failed",
        "message": message[:1000],
        "validations": validations or [],
        "release_eligible": False,
    }
    atomic_bytes(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def validate_manifest_envelope(manifest: dict[str, Any]) -> None:
    """Validate the complete v3 envelope before trusting any nested record."""
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise DocumentReleaseError(
            f"document manifest must use schema {MANIFEST_SCHEMA}; v2 and unknown manifests are not releasable"
        )
    expected_top = {
        "schema_version", "created_at", "transaction_id", "build_kind", "release_eligible",
        "inputs", "code", "renderers", "artifacts", "pages", "page_limit", "validations",
        "all_validations_passed",
    }
    if set(manifest) != expected_top:
        raise DocumentReleaseError("document manifest v3 has missing or unexpected top-level fields")
    try:
        created = dt.datetime.fromisoformat(str(manifest.get("created_at", "")).replace("Z", "+00:00"))
        transaction = uuid.UUID(str(manifest.get("transaction_id", "")))
    except (TypeError, ValueError) as exc:
        raise DocumentReleaseError("document manifest timestamp or transaction UUID is malformed") from exc
    if created.tzinfo is None or not str(manifest.get("created_at", "")).endswith("Z"):
        raise DocumentReleaseError("document manifest created_at must be an absolute UTC timestamp")
    if str(transaction) != manifest.get("transaction_id"):
        raise DocumentReleaseError("document manifest transaction_id is not a canonical UUID")

    kind = manifest.get("build_kind")
    if kind not in {"canonical", "preview"}:
        raise DocumentReleaseError("document manifest build_kind is invalid")
    if manifest.get("release_eligible") is not (kind == "canonical"):
        raise DocumentReleaseError("document manifest release eligibility contradicts build_kind")
    if manifest.get("all_validations_passed") is not True:
        raise DocumentReleaseError("document manifest does not record a fully passing build")

    def record(value: Any, label: str, *, path: bool = True) -> None:
        keys = {"path", "sha256", "bytes"} if path else {"name", "sha256", "bytes"}
        locator = "path" if path else "name"
        if not isinstance(value, dict) or set(value) != keys:
            raise DocumentReleaseError(f"document manifest {label} record is malformed")
        if not isinstance(value.get(locator), str) or not value[locator]:
            raise DocumentReleaseError(f"document manifest {label} {locator} is empty")
        if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256", ""))):
            raise DocumentReleaseError(f"document manifest {label} SHA-256 is malformed")
        if (not isinstance(value.get("bytes"), int) or isinstance(value.get("bytes"), bool)
                or value["bytes"] < 0):
            raise DocumentReleaseError(f"document manifest {label} byte count is malformed")

    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {
        "report", "front_matter", "captions", "policy_snapshot", "policy_sha256",
    }:
        raise DocumentReleaseError("document manifest inputs are malformed")
    for label in ("report", "front_matter", "captions", "policy_snapshot"):
        record(inputs[label], label)
    if not re.fullmatch(r"[0-9a-f]{64}", str(inputs.get("policy_sha256", ""))):
        raise DocumentReleaseError("document manifest policy SHA-256 is malformed")

    code = manifest.get("code")
    if not isinstance(code, dict) or set(code) != {"builder", "verifier", "cache_helper"}:
        raise DocumentReleaseError("document manifest code identities are malformed")
    expected_names = {
        "builder": "build_docx.sh", "verifier": "document_release.py",
        "cache_helper": "document_build_cache.py",
    }
    for label, expected_name in expected_names.items():
        record(code[label], label, path=False)
        if code[label]["name"] != expected_name:
            raise DocumentReleaseError(f"document manifest {label} code name is invalid")

    renderers = manifest.get("renderers")
    expected_renderers = {
        "markdown_to_docx": "pandoc", "docx_to_pdf": "libreoffice",
        "pdf_page_counter": "pdfinfo", "pdf_text_extractor": "pdftotext",
    }
    if not isinstance(renderers, dict) or set(renderers) != set(expected_renderers):
        raise DocumentReleaseError("document manifest renderer identities are malformed")
    for label, expected_name in expected_renderers.items():
        value = renderers[label]
        if (not isinstance(value, dict) or set(value) != {"name", "identity"}
                or value.get("name") != expected_name
                or not isinstance(value.get("identity"), str) or not value["identity"].strip()):
            raise DocumentReleaseError(f"document manifest {label} renderer identity is malformed")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {"pdf", "docx"}:
        raise DocumentReleaseError("document manifest artifacts are malformed")
    record(artifacts.get("pdf"), "pdf")
    if artifacts.get("docx") is not None:
        record(artifacts["docx"], "docx")

    pages = manifest.get("pages")
    page_keys = {
        "total_pages", "body_pages", "back_matter_pages", "first_back_matter_page",
        "abstract_ending_page", "body_page_cap",
    }
    if not isinstance(pages, dict) or set(pages) != page_keys:
        raise DocumentReleaseError("document manifest page metrics are malformed")
    for label in page_keys - {"first_back_matter_page"}:
        value = pages.get(label)
        minimum = 0 if label == "back_matter_pages" else 1
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise DocumentReleaseError(f"document manifest page metric {label} is malformed")
    if pages["total_pages"] != pages["body_pages"] + pages["back_matter_pages"]:
        raise DocumentReleaseError("document manifest body/back-matter page counts do not sum")
    expected_first = (None if pages["back_matter_pages"] == 0 else pages["body_pages"] + 1)
    if pages["first_back_matter_page"] != expected_first:
        raise DocumentReleaseError("document manifest first back-matter page is inconsistent")
    if pages["abstract_ending_page"] != 1:
        raise DocumentReleaseError("document manifest abstract does not end on page one")
    if pages["body_pages"] > pages["body_page_cap"]:
        raise DocumentReleaseError("document manifest body pages exceed its recorded cap")

    limit = manifest.get("page_limit")
    expected_source = "hash_bound_policy_snapshot" if kind == "canonical" else "preview_cli"
    expected_limit = {
        "value": pages["body_page_cap"], "source": expected_source,
        "release_enforced": kind == "canonical",
    }
    if limit != expected_limit:
        raise DocumentReleaseError("document manifest page-limit provenance is inconsistent")
    if kind == "canonical" and pages["body_page_cap"] != CANONICAL_BODY_CAP:
        raise DocumentReleaseError("canonical document manifest does not carry the ten-page cap")

    validations = manifest.get("validations")
    if not isinstance(validations, list) or not validations:
        raise DocumentReleaseError("document manifest has no validation results")
    names: set[str] = set()
    for row in validations:
        if (not isinstance(row, dict) or set(row) != {"name", "status", "detail"}
                or not isinstance(row.get("name"), str) or not row["name"]
                or row.get("status") != "passed"
                or not isinstance(row.get("detail"), str) or not row["detail"]):
            raise DocumentReleaseError("document manifest contains a malformed validation result")
        if row["name"] in names:
            raise DocumentReleaseError(f"duplicate document validation result: {row['name']}")
        names.add(row["name"])


def load_manifest(path: pathlib.Path) -> dict[str, Any]:
    manifest = strict_json(path)
    if not isinstance(manifest, dict):
        raise DocumentReleaseError(
            f"document manifest must use schema {MANIFEST_SCHEMA}; v2 and unknown manifests are not releasable"
        )
    validate_manifest_envelope(manifest)
    return manifest


def expected_code(manifest: dict[str, Any], key: str, path: pathlib.Path) -> None:
    record = (manifest.get("code") or {}).get(key)
    if not isinstance(record, dict) or set(record) != {"name", "sha256", "bytes"}:
        raise DocumentReleaseError(f"document manifest has a malformed {key} code identity")
    regular_file(path, key)
    if record.get("sha256") != sha256(path) or record.get("bytes") != path.stat().st_size:
        # Every release that changes the document code makes older manifests stale (2.6.8 and
        # 2.6.9 changed build_docx.sh and document_release.py), so the refusal names its remedy.
        bound = ((manifest.get("artifacts") or {}).get("pdf") or {}).get("path")
        name = (pathlib.PurePosixPath(bound).stem
                if isinstance(bound, str) and re.fullmatch(r"report/[^/]+\.pdf", bound)
                else "<output-name>")
        raise DocumentReleaseError(
            f"document manifest {key} code identity differs from the release verifier (the skill's document "
            "scripts changed since this build). Rebuild the document with build_docx.sh, then re-run "
            f"finalize_run.sh before staging or publishing: rebuild with scripts/build_docx.sh <audit-dir> "
            f"{name} (see INSTALL.md, 'Appendix A rendering and document manifests')"
        )


def compare_pages(recorded: Any, actual: dict[str, Any]) -> None:
    if not isinstance(recorded, dict) or recorded != actual:
        raise DocumentReleaseError(
            f"document manifest page metrics are stale: recorded={recorded!r}, actual={actual!r}"
        )


def verify_release(root: pathlib.Path, *, manifest_path: pathlib.Path | None = None,
                   builder: pathlib.Path = DEFAULT_BUILDER, boundary: str = "release") -> dict[str, Any]:
    root = root.resolve(strict=True)
    manifest_path = manifest_path or (root / "document_build_manifest.json")
    manifest = load_manifest(manifest_path)
    if manifest.get("build_kind") != "canonical" or manifest.get("release_eligible") is not True:
        raise DocumentReleaseError("preview/non-release document manifests cannot cross a release boundary")
    _, policy_path, policy_hash = policy_snapshot(root)
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        raise DocumentReleaseError("document manifest lacks bound inputs")
    report = verify_record(root, inputs.get("report"), "report")
    front_matter = verify_record(root, inputs.get("front_matter"), "front_matter")
    captions_path = verify_record(root, inputs.get("captions"), "captions")
    bound_policy = verify_record(root, inputs.get("policy_snapshot"), "policy_snapshot")
    if bound_policy != policy_path.resolve() or inputs.get("policy_sha256") != policy_hash:
        raise DocumentReleaseError("document manifest policy binding is stale")
    if report.relative_to(root).as_posix() != "report/REPORT_final.md":
        raise DocumentReleaseError("release document must bind report/REPORT_final.md")
    if front_matter.relative_to(root).as_posix() != "report/front_matter.md":
        raise DocumentReleaseError("release document must bind report/front_matter.md")
    if captions_path.relative_to(root).as_posix() != "report/captions.txt":
        raise DocumentReleaseError("release document must bind report/captions.txt")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {"pdf", "docx"}:
        raise DocumentReleaseError("document manifest must bind exactly PDF and optional DOCX records")
    pdf = verify_record(root, artifacts.get("pdf"), "pdf")
    docx_record = artifacts.get("docx")
    docx = verify_record(root, docx_record, "docx") if docx_record is not None else None
    pdf_relative = pdf.relative_to(root).as_posix()
    if (not pdf_relative.startswith("report/") or pdf_relative.startswith("report/previews/") or
            "preview" in pdf.name.lower() or "_proxy" in pdf.name.lower()):
        raise DocumentReleaseError("release PDF is a preview, proxy, or outside report/")
    if docx is not None:
        docx_relative = docx.relative_to(root).as_posix()
        if (not docx_relative.startswith("report/") or docx_relative.startswith("report/previews/") or
                docx.stem != pdf.stem):
            raise DocumentReleaseError("bound DOCX must be the same report/<stem> as the exact PDF")
    verify_report_document_inventory(root, pdf, docx)

    expected_code(manifest, "builder", builder.resolve(strict=True))
    expected_code(manifest, "verifier", pathlib.Path(__file__).resolve())
    expected_code(manifest, "cache_helper", DEFAULT_CACHE_HELPER.resolve(strict=True))
    cap = canonical_cap(root)
    limit = manifest.get("page_limit")
    if limit != {"value": cap, "source": "hash_bound_policy_snapshot", "release_enforced": True}:
        raise DocumentReleaseError("document manifest page limit is not the canonical policy-bound cap")
    metrics, validations = validate_exact_document(
        report=report, captions_path=captions_path, docx=docx, pdf=pdf, cap=cap,
    )
    compare_pages(manifest.get("pages"), metrics)
    if manifest.get("validations") != validations or manifest.get("all_validations_passed") is not True:
        raise DocumentReleaseError("document manifest validation results are stale or incomplete")
    print(
        f"document boundary {boundary}: exact PDF {pdf_relative}, "
        f"{metrics['body_pages']}/{cap} body pages, {metrics['back_matter_pages']} back-matter page(s)"
    )
    return manifest


def recount_candidate(manifest_path: pathlib.Path, pdf: pathlib.Path) -> dict[str, int]:
    manifest = load_manifest(manifest_path)
    if manifest.get("build_kind") != "canonical" or manifest.get("release_eligible") is not True:
        raise DocumentReleaseError("candidate PDF is bound only by a preview/non-release manifest")
    record = ((manifest.get("artifacts") or {}).get("pdf"))
    if not isinstance(record, dict) or set(record) != {"path", "sha256", "bytes"}:
        raise DocumentReleaseError("manifest PDF record is malformed")
    regular_file(pdf, "candidate PDF")
    if record.get("sha256") != sha256(pdf) or record.get("bytes") != pdf.stat().st_size:
        raise DocumentReleaseError("candidate PDF bytes differ from the document manifest")
    total = exact_page_count(pdf)
    pages = manifest.get("pages") or {}
    if total != pages.get("total_pages"):
        raise DocumentReleaseError("candidate PDF page count differs from document manifest")
    texts = page_texts(pdf, total)
    boundary_candidates: list[int] = []
    for number, text in enumerate(texts, 1):
        lines = page_lines(text)
        if not lines:
            continue
        if pdf_line_starts_back_matter(lines[0]):
            boundary_candidates.append(number)
    actual_first_back = min(boundary_candidates) if boundary_candidates else None
    actual_body = total if actual_first_back is None else actual_first_back - 1
    actual_back = total - actual_body
    if (pages.get("first_back_matter_page") != actual_first_back
            or pages.get("body_pages") != actual_body
            or pages.get("back_matter_pages") != actual_back):
        raise DocumentReleaseError(
            "candidate PDF body/back-matter boundary differs from the document manifest"
        )
    body = actual_body
    cap = pages.get("body_page_cap")
    if not isinstance(body, int) or not isinstance(cap, int) or body > cap or cap != CANONICAL_BODY_CAP:
        raise DocumentReleaseError("candidate PDF does not carry a passing canonical body-page count")
    return {"total_pages": total, "body_pages": body, "body_page_cap": cap}


def safe_target(root: pathlib.Path, path: pathlib.Path, label: str) -> pathlib.Path:
    root = root.resolve(strict=True)
    parent = path.parent.resolve(strict=True)
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise DocumentReleaseError(f"{label} target escapes audit root: {path}") from exc
    target = parent / path.name
    if target.is_symlink() or target.is_dir():
        raise DocumentReleaseError(f"{label} target is a symlink or directory: {target}")
    return target


def stage_temp(target: pathlib.Path, data: bytes) -> pathlib.Path:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.",
                                                   suffix=".document-stage", dir=target.parent)
    temporary = pathlib.Path(temporary_name)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    return temporary


def promote(root: pathlib.Path, manifest_source: pathlib.Path, pdf_source: pathlib.Path,
            pdf_target: pathlib.Path, docx_source: pathlib.Path | None,
            docx_target: pathlib.Path | None, manifest_target: pathlib.Path) -> None:
    root = root.resolve(strict=True)
    manifest = load_manifest(manifest_source)
    artifacts = manifest.get("artifacts") or {}
    pdf_record = artifacts.get("pdf")
    docx_record = artifacts.get("docx")
    if not isinstance(pdf_record, dict) or pdf_record.get("sha256") != sha256(pdf_source):
        raise DocumentReleaseError("promoted PDF does not match the validated manifest")
    if pdf_record.get("path") != pdf_target.resolve(strict=False).relative_to(root).as_posix():
        raise DocumentReleaseError("PDF promotion target differs from the manifest path")
    if docx_record is None:
        if docx_source is not None or docx_target is not None:
            raise DocumentReleaseError("manifest omits DOCX but promotion supplied one")
    else:
        if docx_source is None or docx_target is None or docx_record.get("sha256") != sha256(docx_source):
            raise DocumentReleaseError("promoted DOCX does not match the validated manifest")
        if docx_record.get("path") != docx_target.resolve(strict=False).relative_to(root).as_posix():
            raise DocumentReleaseError("DOCX promotion target differs from the manifest path")

    targets: list[tuple[pathlib.Path, bytes]] = [(safe_target(root, pdf_target, "PDF"), pdf_source.read_bytes())]
    if docx_source is not None and docx_target is not None:
        targets.insert(0, (safe_target(root, docx_target, "DOCX"), docx_source.read_bytes()))
    targets.append((safe_target(root, manifest_target, "manifest"), manifest_source.read_bytes()))
    lock_path = root / ".document-build.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    staged: list[tuple[pathlib.Path, pathlib.Path]] = []
    backups: dict[pathlib.Path, bytes | None] = {}
    with lock_path.open("r+") as lock:
        lock_wait.acquire_exclusive(lock, "the document build lock")
        try:
            for target, data in targets:
                staged.append((stage_temp(target, data), target))
                backups[target] = target.read_bytes() if target.exists() else None
            replaced: list[pathlib.Path] = []
            try:
                # The binding manifest is always last.  Every file replacement is atomic; rollback
                # restores the old complete set if any later replacement fails.
                for temporary, target in staged:
                    os.replace(temporary, target)
                    replaced.append(target)
                for directory in {target.parent for target, _ in targets}:
                    fsync_directory(directory)
                # A canonical transaction is not committed merely because every rename
                # succeeded.  Recount and revalidate the now-visible exact bytes while the
                # journal-style document lock is still held; any failure restores the prior
                # complete set before another builder or release boundary can observe success.
                if manifest.get("build_kind") == "canonical":
                    verify_release(
                        root, manifest_path=manifest_target, builder=DEFAULT_BUILDER,
                        boundary="transaction-commit",
                    )
            except Exception:
                for target in reversed(replaced):
                    old = backups[target]
                    if old is None:
                        with contextlib.suppress(FileNotFoundError):
                            target.unlink()
                    else:
                        replacement = stage_temp(target, old)
                        os.replace(replacement, target)
                for directory in {target.parent for target, _ in targets}:
                    fsync_directory(directory)
                raise
        finally:
            for temporary, _ in staged:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def manifest_paths(manifest: dict[str, Any]) -> tuple[str, str | None]:
    artifacts = manifest.get("artifacts") or {}
    pdf = artifacts.get("pdf")
    docx = artifacts.get("docx")
    if not isinstance(pdf, dict) or not isinstance(pdf.get("path"), str):
        raise DocumentReleaseError("manifest lacks a PDF path")
    if docx is not None and (not isinstance(docx, dict) or not isinstance(docx.get("path"), str)):
        raise DocumentReleaseError("manifest DOCX path is malformed")
    return pdf["path"], (docx["path"] if docx is not None else None)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    cap = commands.add_parser("policy-cap")
    cap.add_argument("audit", type=pathlib.Path)

    create = commands.add_parser("create-manifest")
    create.add_argument("--audit", type=pathlib.Path, required=True)
    create.add_argument("--report", type=pathlib.Path, required=True)
    create.add_argument("--front-matter", type=pathlib.Path, required=True)
    create.add_argument("--captions", type=pathlib.Path, required=True)
    create.add_argument("--docx", type=pathlib.Path)
    create.add_argument("--pdf", type=pathlib.Path, required=True)
    create.add_argument("--docx-relative")
    create.add_argument("--pdf-relative", required=True)
    create.add_argument("--builder", type=pathlib.Path, default=DEFAULT_BUILDER)
    create.add_argument("--cache-helper", type=pathlib.Path, default=DEFAULT_CACHE_HELPER)
    create.add_argument("--pandoc-identity", required=True)
    create.add_argument("--renderer-identity", required=True)
    create.add_argument("--pdfinfo-identity", required=True)
    create.add_argument("--pdftotext-identity", required=True)
    create.add_argument("--build-kind", choices=("canonical", "preview"), required=True)
    create.add_argument("--cap", type=int)
    create.add_argument("--manifest-out", type=pathlib.Path, required=True)
    create.add_argument("--diagnostic-out", type=pathlib.Path)

    verify = commands.add_parser("verify")
    verify.add_argument("audit", type=pathlib.Path)
    verify.add_argument("--manifest", type=pathlib.Path)
    verify.add_argument("--builder", type=pathlib.Path, default=DEFAULT_BUILDER)
    verify.add_argument("--boundary", default="release")

    paths = commands.add_parser("paths")
    paths.add_argument("manifest", type=pathlib.Path)

    inventory = commands.add_parser("preflight-inventory")
    inventory.add_argument("audit", type=pathlib.Path)
    inventory.add_argument("--pdf-target", type=pathlib.Path, required=True)
    inventory.add_argument("--docx-target", type=pathlib.Path, required=True)

    recount = commands.add_parser("recount")
    recount.add_argument("manifest", type=pathlib.Path)
    recount.add_argument("pdf", type=pathlib.Path)

    promotion = commands.add_parser("promote")
    promotion.add_argument("--audit", type=pathlib.Path, required=True)
    promotion.add_argument("--manifest-source", type=pathlib.Path, required=True)
    promotion.add_argument("--manifest-target", type=pathlib.Path, required=True)
    promotion.add_argument("--pdf-source", type=pathlib.Path, required=True)
    promotion.add_argument("--pdf-target", type=pathlib.Path, required=True)
    promotion.add_argument("--docx-source", type=pathlib.Path)
    promotion.add_argument("--docx-target", type=pathlib.Path)

    diagnostic = commands.add_parser("diagnostic")
    diagnostic.add_argument("--out", type=pathlib.Path, required=True)
    diagnostic.add_argument("--build-kind", choices=("canonical", "preview"), required=True)
    diagnostic.add_argument("--stage", required=True)
    diagnostic.add_argument("--message", required=True)

    integrity = commands.add_parser("findings-table-integrity")
    integrity.add_argument("--report", type=pathlib.Path, required=True)
    integrity.add_argument("--docx", type=pathlib.Path)
    integrity.add_argument("--pdf", type=pathlib.Path, required=True)
    integrity.add_argument("--build-kind", choices=("canonical", "preview"), default="canonical")
    integrity.add_argument("--diagnostic-out", type=pathlib.Path)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "policy-cap":
            print(canonical_cap(args.audit.resolve(strict=True)))
        elif args.command == "create-manifest":
            if args.docx is not None and not args.docx_relative:
                raise DocumentReleaseError("--docx-relative is required with --docx")
            try:
                manifest = manifest_from_args(args)
            except ValidationFailure as exc:
                if args.diagnostic_out:
                    write_diagnostic(args.diagnostic_out, build_kind=args.build_kind,
                                     stage="validation", message=str(exc),
                                     validations=exc.validations)
                raise
            atomic_bytes(args.manifest_out, (json.dumps(manifest, indent=2,
                                                        sort_keys=True) + "\n").encode("utf-8"))
            print(f"document manifest v3: {args.manifest_out}")
        elif args.command == "verify":
            verify_release(args.audit, manifest_path=args.manifest,
                           builder=args.builder, boundary=args.boundary)
        elif args.command == "paths":
            manifest = load_manifest(args.manifest)
            pdf, docx = manifest_paths(manifest)
            print(pdf)
            print(docx or "")
        elif args.command == "preflight-inventory":
            preflight_report_document_inventory(
                args.audit, args.pdf_target, args.docx_target,
            )
            print("report document inventory preflight: ok")
        elif args.command == "recount":
            values = recount_candidate(args.manifest, args.pdf)
            print(json.dumps(values, sort_keys=True))
        elif args.command == "promote":
            if (args.docx_source is None) != (args.docx_target is None):
                raise DocumentReleaseError("DOCX source and target must be supplied together")
            promote(args.audit, args.manifest_source, args.pdf_source, args.pdf_target,
                    args.docx_source, args.docx_target, args.manifest_target)
            print(f"document transaction promoted: {args.manifest_target}")
        elif args.command == "diagnostic":
            write_diagnostic(args.out, build_kind=args.build_kind, stage=args.stage,
                             message=args.message)
        elif args.command == "findings-table-integrity":
            try:
                print(findings_table_integrity(args.report, args.docx, args.pdf))
            except DocumentReleaseError as exc:
                if args.diagnostic_out:
                    write_diagnostic(args.diagnostic_out, build_kind=args.build_kind,
                                     stage="findings_table_integrity", message=str(exc),
                                     validations=[validation("findings_table_integrity", False,
                                                             str(exc))])
                raise
    except (DocumentReleaseError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"FATAL: document release: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
