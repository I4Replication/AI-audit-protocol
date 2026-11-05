#!/usr/bin/env python3
"""Predict from the report alone a refusal by the document build's abstract-end locator (2.7.11).

usage: check_abstract_end.py <audit-dir> [report.md]      (default report/REPORT_final.md; writes nothing)

build_docx.sh's verifier (document_release.abstract_end_page) looks for the abstract's last 12 words, then its last
8, on exactly one page of the PDF, and refuses the build otherwise: "could not locate the abstract ending on exactly
one PDF page". It cannot be changed: every published comment's document manifest binds that file's hash. Between
2026-09-03 and 10-29 it refused builds of nine audits whose abstract did end on page 1, for two reasons:

- the abstract's closing words recur later in the comment (a body section, the Conclusion), so two pages match
  (matches=[1, 3], [1, 7], [1, 9], [1, 10]);
- a hyphenated compound among the last eight words breaks across a line in the PDF ("de-" / "identified"), which the
  locator reads as two words, so no page matches (matches=[]); emphasis inside a word (*p*-value) or a pandoc
  super/subscript (R^2^) differs between the Markdown and the PDF in the same way, on every build.

One of them was found at release and cost an extra critique round to review a reworded abstract. This check runs the
locator's own tokenizer (imported, never copied) on the Markdown, so it is cheap enough to run at every integration.
Exit 0: no known cause. Exit 1: the build will refuse (the closing words recur, or markup splits them). Exit 3: it
may refuse, depending on where the renderer breaks a line (a hyphenated compound among the last eight words).
Exit 2: the report cannot be read or has no abstract the locator can use.
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import document_release as dr  # noqa: E402  (the frozen locator: its words(), markdown_abstract(), contains_sequence())

SECTION = re.compile(r"(?ms)^##\s+(Abstract|Executive summary)\s*$\s*(.*?)(?=^##\s+|\Z)")   # markdown_abstract's


def locator_text(markdown: str) -> list[str]:
    """Words of Markdown as the locator cleans its abstract (links to their text, tags and `*_# removed)."""
    markdown = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", markdown)
    return dr.words(re.sub(r"<[^>]+>|[`*_#]", " ", markdown))


def rendered_text(markdown: str) -> list[str]:
    """Words as the PDF shows them: emphasis and super/subscript markers vanish without a space."""
    markdown = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", markdown)
    markdown = re.sub(r"<[^>]+>|[`#]", " ", markdown)
    # an underscore between two letters or digits is part of a name (pandoc's intraword rule), not emphasis
    markdown = re.sub(r"(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])", "", markdown)
    return dr.words(re.sub(r"\*+|\^|~", "", markdown))


def recurs(chunks: list[list[str]], needle: list[str]) -> bool:
    return any(dr.contains_sequence(chunk, needle) for chunk in chunks)


def check(report_text: str, others: list[str]) -> tuple[int, list[str]]:
    _, tail = dr.markdown_abstract(report_text)            # raises DocumentReleaseError like the build
    match = SECTION.search(report_text)
    # every other section on its own, as a page holds it: one pass over the whole text lets a bracket in one
    # section and a parenthesis in a later one read as a link and swallow the words between them. The front matter
    # before the first section heading (title, byline) shares page 1 with the abstract, so a repeat there cannot
    # make a second page match: it is left out.
    before = re.split(r"(?m)^(?=##\s)", report_text[:match.start()])
    chunks = [chunk for chunk in before if chunk.startswith("##")] + re.split(r"(?m)^(?=##\s)", report_text[match.end():])
    rest = [locator_text(chunk) for chunk in chunks + list(others)]
    problems: list[tuple[int, str]] = []
    if recurs(rest, tail):
        problems.append((1, f"the abstract's last {len(tail)} words recur later in the comment, so the locator finds "
                            "them on two pages: reword the abstract's closing words or the passage that repeats them"))
    elif len(tail) > 8 and recurs(rest, tail[-8:]):
        problems.append((3, "the abstract's last 8 words recur later in the comment: if its last 12 are not found on "
                            "page 1 the locator falls back to 8 and finds two pages; reword one of them"))
    if not dr.contains_sequence(rendered_text(match.group(2)), tail[-8:]):
        problems.append((1, "markup inside a word among the abstract's last eight (emphasis such as *p*-value, or a "
                            "^superscript^/~subscript~) renders as a different word in the PDF: move it earlier or "
                            "write it without the markup"))
    hyphenated = [word for word in tail[-8:] if "-" in word]
    if hyphenated:
        problems.append((3, "a hyphenated word among the abstract's last eight (" + ", ".join(hyphenated) + ") is "
                            "read as two words wherever the PDF breaks the line at its hyphen: move it earlier or "
                            "write it without the hyphen"))
    code = 1 if any(level == 1 for level, _ in problems) else (3 if problems else 0)
    return code, [text for _, text in problems]


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        return 2
    audit = pathlib.Path(argv[1])
    report = pathlib.Path(argv[2]) if len(argv) == 3 else audit / "report" / "REPORT_final.md"
    try:
        text = report.read_text(encoding="utf-8")
        # the table captions build_docx.sh places in the body (one per line)
        captions = audit / "report" / "captions.txt"
        others = [captions.read_text(encoding="utf-8")] if captions.is_file() else []
        code, problems = check(text, others)
    except (OSError, UnicodeDecodeError, dr.DocumentReleaseError) as exc:
        print(f"abstract end: cannot check {report.name}: {exc}", file=sys.stderr)
        return 2
    if not problems:
        print("abstract end: ok (the build's page-1 locator has no known reason to refuse)")
    for problem in problems:
        print(f"abstract end: {'WILL REFUSE' if code == 1 else 'MAY REFUSE'}: {problem}")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
