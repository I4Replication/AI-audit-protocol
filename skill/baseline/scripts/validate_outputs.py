#!/usr/bin/env python3
"""validate_outputs.py <output-dir> [--json FILE] [--pkg DIR]: check a baseline comment before it is accepted.

The agent runs this inside its own session (a copy sits in its packet under tools/) and fixes what it reports; the
runner runs it again afterwards and records the result. No model call, stdlib only.

Checks: the closed output set; findings.json against FINDINGS_FORMAT.md; the comment's structure (the rules
i4r-audit's check_consistency.sh section 3 applies: Abstract <= 250 words and prose only, no findings table before the
appendix, no Recommendations section, both protocol citations, no process narration); Appendix A agreeing with
findings.json; captions; front matter; and publication hygiene (no absolute path, no participant-code token, no
participant identifier beside a date or clock time, no e-mail address outside the byline). The hygiene patterns are
i4r-audit 2.7.7's (provider_safety.RAW_PARTICIPANT_CODE / ROW_PARTICIPANT_CODE, phase1_runtime.participant_date_linked).
Exit 0 when there is no error, 1 otherwise.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import pathlib
import re
import sys

REQUIRED = ("front_matter.md", "REPORT_final.md", "captions.txt", "findings.json")
IMPACT = ("none", "unverifiable", "value_wrong", "conclusion_narrowed", "conclusion_unsupported",
          "conclusion_contradicted", "artefact_of_construction")
REMEDY = ("one_line_code", "one_number", "reanalysis", "not_fixable", "disclose_in_paper")
CONFIDENCE = ("CONFIRMED", "LIKELY", "SPECULATIVE")
LOCUS = ("data", "cleaning", "analysis_code", "estimation", "inference", "sample", "reporting", "figure", "text",
         "environment", "other")
RUNS = ("yes", "no", "partial", "not_attempted")
REPRO = ("exact", "within_rounding", "qualitatively", "no", "not_attempted")
# i4r-audit check_release_privacy.TEXT_SUFFIXES (the pipeline's own list), plus SVG, logs and console output
TEXT_SUFFIXES = {".bib", ".cfg", ".css", ".csv", ".do", ".html", ".ini", ".ipynb", ".jl", ".js", ".json", ".jsonl",
                 ".m", ".md", ".py", ".qmd", ".r", ".rmd", ".sas", ".sh", ".sps", ".sql", ".tex", ".toml", ".ts",
                 ".tsv", ".txt", ".xml", ".yaml", ".yml", ".svg", ".log", ".out"}
DATA_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xls", ".sav", ".dta", ".rds", ".rdata", ".parquet", ".feather"}
BINARY_DATA = {".xlsx", ".xls", ".xlsm", ".sav", ".zsav", ".dta", ".rds", ".rdata", ".rda", ".parquet", ".feather",
               ".sqlite", ".db", ".mat", ".pkl", ".pickle", ".npy", ".npz", ".h5", ".hdf5"}
COMPRESSED = {".gz", ".zip", ".bz2", ".xz", ".7z", ".tar", ".tgz", ".zst"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}   # raster figures: no text to check
UNVERIFIABLE_REASONS = ("data_missing", "data_not_available", "code_missing", "code_not_functional",
                        "discrepancy_in_output", "software_not_available", "insufficient_compute", "missing_readme",
                        "insufficient_documentation")
CHECK_NATURE = ("deterministic", "deterministic_with_tolerance", "judgement")
BYLINE_EMAILS = {"juposada93@gmail.com", "abrodeur@uottawa.ca"}

# i4r-audit check_consistency.sh section 3c
BANNED = [r"earlier draft", r"earlier version of this comment", r"\bwe withdraw\b", r"\bround[ -][0-9]",
          r"adversarial (review|critic)", r"\bour critics\b", r"\ba critic\b", r"Changes from earlier revisions",
          r"\bRevision [0-9]\b", r"corrected (twice|in round)", r"intermediate draft", r"our (own )?initial reading",
          r"we initially", r"more embarrassingly", r"\bwe first (thought|reported)\b"]

ABSOLUTE_PATH = re.compile(r"(?:/" + "Users/" + r"|/home/|/private/(?:tmp|var)/|[A-Za-z]:[\\/]Users[\\/])")
EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
# i4r-audit provider_safety (2.7.7)
RAW_PARTICIPANT_CODE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,}(?:_[A-Z]{1,8})*_?P\d+(?![A-Za-z0-9])")
ROW_PARTICIPANT_CODE = re.compile(r"(?:^|[,\t|])\s*(P\d{1,4})\s*[,\t|]\s*[-+]?\d+(?:\.\d+)?", re.MULTILINE)
# i4r-audit phase1_runtime (2.7.7): participant_date_linked = the 2.6 pair AND the 2.7.7 pair
PARTICIPANT_ID = re.compile(r"(?i)(?<![A-Za-z0-9])(?:participant|subject|subj|pid|sid)[-_ ]?[A-Za-z0-9]{1,16}(?![A-Za-z0-9])")
DATE = re.compile(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b|\b\d{1,2}:\d{2}(?::\d{2})?\b")
_ID_SEP = "[-_  ]"
_ID_Q = "[*`\"'‘’“”]{0,2}"
_ID_CODE = r"#?(?:[A-Za-z]{1,8}[-_.])?(?=[A-Za-z0-9]{0,15}\d)[A-Za-z0-9]{1,16}(?![A-Za-z0-9])"
_ID_ITEM = rf"{_ID_Q}#?(?:[A-Za-z]{{1,4}}[-_.]?)?\d{{1,4}}[A-Za-z]?(?![A-Za-z0-9]){_ID_Q}"
_ID_LABEL = r"(?:ids?|identifiers?|nos?|nr|n°|nº|num|numbers?|codes?)\.?"
_NOT_A_PSEUDONYM = (r"(?:SDS?|SEM?|CVS?|CIS?|ICC|IQR|IDS?|AM|PM|RTS?|MD|N|M|F|OR|AND|THE|OF|TO|IN|RPE|HRV?|BMI|BP|MVC|"
                    r"EMG|EEG|ECG|MRI|CMJ|ACWR|RSI|SRPE|TL|VAS|PSQI|ESS|MEQ|POMS|TST|SOL|WASO)")
PARTICIPANT_LABELLED_ID = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:(?:participant|subject|subj|pid|sid)(?:"
    rf"\.?{_ID_SEP}?{_ID_Q}{_ID_CODE}"
    rf"|s?(?:{_ID_SEP}{{1,3}}(?:with|whose|of)(?:{_ID_SEP}{{1,3}}the)?(?!{_ID_SEP}{{1,3}}(?-i:no)(?![.A-Za-z0-9])))?"
    rf"{_ID_SEP}{{0,3}}{_ID_LABEL}(?:{_ID_SEP}{{1,3}}(?:no|number)\.?)?(?:{_ID_SEP}{{1,3}}(?:is|was|are|were))?"
    rf"[-_  :#=!<>\x22']{{0,6}}{_ID_Q}{_ID_CODE}"
    rf"|{_ID_SEP}(?-i:(?!{_NOT_A_PSEUDONYM}(?![A-Za-z0-9]))[A-Z]{{1,4}})(?![A-Za-z0-9])"
    rf"|s?{_ID_SEP}{{0,3}}:?{_ID_SEP}{{0,3}}{_ID_ITEM}(?:{_ID_SEP}{{0,3}}(?:,|and|&){_ID_SEP}{{0,3}}{_ID_ITEM})+)"
    rf"|(?-i:Participant|Subject|Subj\.?){_ID_SEP}(?-i:[MNF])(?![A-Za-z0-9])(?!{_ID_SEP}{{0,3}}[=<>(≈]))")
DATE_OR_CLOCK_TIME = re.compile(
    r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}(?!\d)"
    r"|\b(?:[01０１]?\d|[2２][0-4０-４]):[0-5０-５]\d(?::[0-5０-５]\d)?\b"
    r"|(?<=\d[Tt])(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?!\d)")


# i4r-audit run_coordinator (2.7.7): the comment writer's gates, applied by check_consistency.sh to the same comment
COORD_PARTICIPANT_CODE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,3}_?P\d+(?![A-Za-z0-9])|(?<![A-Za-z0-9])P\d{1,3}(?![A-Za-z0-9])")
COORD_DATE_OR_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}\.\d{1,2}\.\d{4}|\d{2}:\d{2}:\d{2}")
PLACEHOLDER = re.compile(r"(?im)^\s*(?:todo|tbd|placeholder|\[?insert\b[^\n]*|<[^>]*(?:insert|todo|tbd)[^>]*>)\s*$")
COMMENT_FILES = ("REPORT_final.md", "front_matter.md", "captions.txt", "findings.json")


def participant_date_linked(line: str) -> bool:
    return bool(PARTICIPANT_ID.search(line) and DATE.search(line)
                and PARTICIPANT_LABELLED_ID.search(line) and DATE_OR_CLOCK_TIME.search(line))


def deposit_headers(pkg: pathlib.Path | None) -> set[str]:
    """Column names in the deposit's delimited files: a header that is never a data value may be quoted."""
    names: set[str] = set()
    if not pkg or not pkg.is_dir():
        return names
    for path in pkg.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".csv", ".tsv", ".txt"} and path.stat().st_size < 50 << 20:
            try:
                with open(path, newline="", encoding="utf-8", errors="replace") as handle:
                    first = handle.readline()
                delim = "\t" if first.count("\t") > first.count(",") else ","
                names.update(cell.strip().strip('"') for cell in next(csv.reader([first], delimiter=delim)))
            except (OSError, StopIteration, csv.Error):
                continue
    return names


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def pdf_text(path: pathlib.Path) -> str | None:
    """The PDF's text (pdftotext), or None when it cannot be extracted: an unreadable PDF cannot be checked."""
    import shutil
    import subprocess
    conda = pathlib.Path.home() / "miniconda3" / "envs" / "i4rdoc" / "bin"
    tool = shutil.which("pdftotext") or shutil.which("pdftotext", path=str(conda))
    if not tool:
        return None
    try:
        done = subprocess.run([tool, "-layout", str(path), "-"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def read_text(path: pathlib.Path, rep: "Report", label: str) -> str:
    """The file's text; bytes that are not UTF-8 are an error (the pipeline refuses them) and read as replacements."""
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        rep.error(f"{label}: not UTF-8; write UTF-8 text")
        return data.decode("utf-8", errors="replace")


def hygiene(rep: Report, label: str, text: str, exempt: set[str], *, strict: bool) -> None:
    flag = rep.error if strict else rep.warn
    comment = pathlib.PurePath(label).name in COMMENT_FILES and "/" not in label
    if comment and PLACEHOLDER.search(text):
        flag(f"{label}: a line that is only TODO, TBD, PLACEHOLDER or an insert marker; finish it")
    units = text.splitlines()
    if comment and label == "findings.json":
        try:
            data = json.loads(text)
            units = [json.dumps(f, ensure_ascii=False) for f in (data.get("findings") or [])] + [
                json.dumps({k: v for k, v in data.items() if k != "findings"}, ensure_ascii=False)]
        except (ValueError, AttributeError):
            pass
    for number, unit in enumerate(units, 1):
        if comment and COORD_PARTICIPANT_CODE.search(unit) and COORD_DATE_OR_CLOCK.search(unit):
            where = f"finding {number}" if label == "findings.json" and len(units) > 1 else f"line {number}"
            flag(f"{label}: {where}: a participant code and a date or clock time together")
    for number, line in enumerate(text.splitlines(), 1):
        if ABSOLUTE_PATH.search(line):
            flag(f"{label}:{number}: an absolute path; refer to deposited files by their package-relative path and "
                 f"mask a home-directory prefix as <unix-user-dir>/... or <windows-user-dir>/...")
        for match in RAW_PARTICIPANT_CODE.finditer(line):
            if match.group(0) not in exempt:
                flag(f"{label}:{number}: a token shaped like a participant code; describe it in words")
                break
        if participant_date_linked(line):
            flag(f"{label}:{number}: a participant identifier beside a date or clock time; put them on separate lines")
        for match in EMAIL.finditer(line):
            address = match.group(0).lower()
            if address not in BYLINE_EMAILS and not address.split("@")[1].startswith("example."):
                flag(f"{label}:{number}: an e-mail address; name the identifier kind instead of a value")
                break
    for match in ROW_PARTICIPANT_CODE.finditer(text):
        if match.group(1) not in exempt:
            flag(f"{label}: a row opening with a participant code and a number; report counts, not rows")
            break


def section(text: str, heading_regex: str) -> str | None:
    match = re.search(heading_regex, text, re.M)
    if not match:
        return None
    rest = text[match.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def tables(text: str) -> list[list[str]]:
    """Markdown pipe tables: runs of lines starting with | that include a separator row."""
    found, block = [], []
    for line in text.splitlines() + [""]:
        if line.lstrip().startswith("|"):
            block.append(line)
            continue
        if block and any(re.match(r"^\s*\|?\s*:?-{3,}", row) for row in block):
            found.append(block)
        block = []
    return found


def cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def check_findings(rep: Report, data: object) -> list[dict]:
    if not isinstance(data, dict):
        rep.error("findings.json: not a JSON object")
        return []
    if data.get("schema_version") != "i4r-baseline-findings-1":
        rep.error("findings.json: schema_version must be 'i4r-baseline-findings-1'")
    pkg = data.get("package")
    if not isinstance(pkg, dict):
        rep.error("findings.json: package must be an object")
    else:
        for key in ("data_present", "code_present"):
            if not isinstance(pkg.get(key), bool):
                rep.error(f"findings.json: package.{key} must be true or false")
        if pkg.get("runs_as_shipped") not in RUNS:
            rep.error(f"findings.json: package.runs_as_shipped must be one of {RUNS}")
        if pkg.get("reproduces_headline") not in REPRO:
            rep.error(f"findings.json: package.reproduces_headline must be one of {REPRO}")
    findings = data.get("findings")
    if not isinstance(findings, list):
        rep.error("findings.json: findings must be a list")
        return []
    if not findings:
        rep.warn("findings.json: no findings")
    for k, f in enumerate(findings, 1):
        where = f"findings.json: findings[{k - 1}]"
        if not isinstance(f, dict):
            rep.error(f"{where} is not an object")
            continue
        if not isinstance(f.get("paper_only"), bool):
            rep.error(f"{where}: paper_only must be true or false")
        trio = ("unverifiable_reason", "blocked_tier", "unverifiable_basis")
        if f.get("impact") == "unverifiable":
            if f.get("unverifiable_reason") not in UNVERIFIABLE_REASONS:
                rep.error(f"{where}: an unverifiable finding needs unverifiable_reason, one of {UNVERIFIABLE_REASONS} "
                          f"(SCHEME.md Rules 2b and 2c: without a cause it is not a finding)")
            tier = f.get("blocked_tier")
            if not (isinstance(tier, int) and not isinstance(tier, bool) and 1 <= tier <= 5):
                rep.error(f"{where}: an unverifiable finding needs blocked_tier, the tier (1-5) of the check that could "
                          f"not be run")
            if not (isinstance(f.get("unverifiable_basis"), str) and f["unverifiable_basis"].strip()):
                rep.error(f"{where}: an unverifiable finding needs unverifiable_basis: what was sought, what was tried "
                          f"and why the check stays blocked")
        elif any(f.get(key) is not None for key in trio):
            rep.error(f"{where}: unverifiable_reason, blocked_tier and unverifiable_basis are only for an unverifiable "
                      f"finding; set them to null")
        if f.get("check_nature") is not None and f.get("check_nature") not in CHECK_NATURE:
            rep.error(f"{where}: check_nature must be one of {CHECK_NATURE} or null")
        if "remedy_verified" not in f or (f["remedy_verified"] is not None and not isinstance(f["remedy_verified"], bool)):
            rep.error(f"{where}: remedy_verified must be present: true, false or null")
        tol = (f.get("evidence") or {}).get("tolerance_note") if isinstance(f.get("evidence"), dict) else None
        if tol is not None and not isinstance(tol, str):
            rep.error(f"{where}: evidence.tolerance_note must be a string or null")
        if not f.get("benign_explanations_ruled_out"):
            rep.warn(f"{where}: no benign explanation recorded as ruled out")
        if f.get("id") != f"F{k}":
            rep.error(f"{where}: id must be F{k} (ids run F1..Fn in Appendix A order)")
        for key in ("claim", "section", "remedy_description"):
            if not isinstance(f.get(key), str) or not f.get(key).strip():
                rep.error(f"{where}: {key} must be a non-empty string")
        for key, allowed in (("impact", IMPACT), ("remedy_class", REMEDY), ("confidence", CONFIDENCE),
                             ("locus", LOCUS)):
            if f.get(key) not in allowed:
                rep.error(f"{where}: {key} must be one of {allowed}")
        for key in ("changes_conclusion", "disclosure_flag"):
            if not isinstance(f.get(key), bool):
                rep.error(f"{where}: {key} must be true or false")
        if f.get("changes_conclusion") is True and not (isinstance(f.get("changes_conclusion_which"), str)
                                                       and f["changes_conclusion_which"].strip()):
            rep.error(f"{where}: changes_conclusion is true, so changes_conclusion_which must name the conclusion")
        tier = f.get("verification_tier")
        if not (isinstance(tier, int) and not isinstance(tier, bool) and 1 <= tier <= 5):
            rep.error(f"{where}: verification_tier must be an integer 1-5")
        ev = f.get("evidence")
        if not isinstance(ev, dict):
            rep.error(f"{where}: evidence must be an object")
        else:
            for key in ("artifact", "operation", "observed"):
                if not isinstance(ev.get(key), str) or not ev.get(key).strip():
                    rep.error(f"{where}: evidence.{key} must be a non-empty string")
            if isinstance(ev.get("artifact"), str) and ev["artifact"].startswith("/"):
                rep.error(f"{where}: evidence.artifact must be a package- or paper-relative path")
        if not isinstance(f.get("benign_explanations_ruled_out", []), list):
            rep.error(f"{where}: benign_explanations_ruled_out must be a list")
    for key in ("tested_negatives", "passed_checks"):
        if not isinstance(data.get(key, []), list):
            rep.error(f"findings.json: {key} must be a list")
    return [f for f in findings if isinstance(f, dict)]


FINDING_ID = re.compile(r"(?<![\w./*-])\**F(\d{1,3})(?![\w(])(?!\.\d)")


def check_layout(rep: Report, body: str, findings: list[dict]) -> None:
    """The pipeline's layout (its comments, checked across all of them): no finding id in a heading or in the visible
    text, and exactly one invisible marker per finding. Code spans may name files, and F(1, 46) is a statistic."""
    ids = [str(f.get("id")) for f in findings if isinstance(f, dict)]
    visible = re.sub(r"`[^`\n]*`", "", re.sub(r"(?s)<!--.*?-->", "", body))
    for heading in re.findall(r"(?m)^#{2,4} (.+)$", visible):
        if FINDING_ID.search(heading):
            rep.error(f"REPORT_final.md: the heading '{heading[:80]}' carries a finding id; a heading states the finding")
    stray = sorted({f"F{m}" for m in FINDING_ID.findall(visible) if f"F{m}" in ids}, key=lambda s: int(s[1:]))
    if stray:
        rep.error(f"REPORT_final.md: finding ids in the visible text ({', '.join(stray)}); refer to a finding by its "
                  f"section, and keep its id in the <!-- finding:Fk --> marker only")
    marks = collections.Counter(re.findall(r"<!--\s*finding:(F\d+)\s*-->", body))
    missing = [i for i in ids if not marks[i]]
    if missing:
        rep.error(f"REPORT_final.md: no <!-- finding:Fk --> marker for {', '.join(missing)}")
    for mark, count in sorted(marks.items()):
        if mark not in ids:
            rep.error(f"REPORT_final.md: the marker <!-- finding:{mark} --> names no finding in findings.json")
        elif count > 1:
            rep.error(f"REPORT_final.md: {count} markers for {mark}; place exactly one, below its section heading")


def check_report(rep: Report, text: str, findings: list[dict]) -> None:
    headings = re.findall(r"^## (.+)$", text, re.M)
    if not headings or headings[0].strip() != "Abstract":
        rep.error("REPORT_final.md: the first level-2 heading must be exactly '## Abstract'")
    if len(text.strip()) < 1000:
        rep.error("REPORT_final.md: under 1,000 characters; a comment is a paper, not a skeleton")
    abstract = section(text, r"^## Abstract.*$")
    if abstract is not None:
        lines = abstract.split("\n")
        words = len(" ".join(l for l in lines if not l.lstrip().startswith("|")).split())
        if words > 250:
            rep.error(f"REPORT_final.md: the abstract is {words} words (limit 250)")
        if any(l.lstrip().startswith("|") for l in lines):
            rep.error("REPORT_final.md: the abstract carries a table; it must be prose only")
    if not re.search(r"^## 1\. ", text, re.M):
        rep.error("REPORT_final.md: no numbered '## 1. ...' section")
    if not re.search(r"(?im)^## \d+\.\s+Conclusion\s*$", text):
        rep.error("REPORT_final.md: no numbered '## N. Conclusion' section")
    for name in ("Data and Code Availability", "References"):
        if not re.search(rf"^## {name}\s*$", text, re.M):
            rep.error(f"REPORT_final.md: no '## {name}' section")
    if re.search(r"^## (?:\d+\. )?Recommendations\s*$", text, re.M):
        rep.error("REPORT_final.md: a Recommendations section; each finding carries its own Remedy line instead")
    for needle in ("AI-Games", "AI-vertical"):
        if needle not in text:
            rep.error(f"REPORT_final.md: the required protocol citation marker {needle} is missing "
                      f"(house_citations.md)")
    for number, line in enumerate(text.split("\n"), 1):
        for pat in BANNED:
            if re.search(pat, line, re.I):
                rep.error(f"REPORT_final.md:{number}: process narration ({pat}); the comment is about the paper")
                break
    appendix_at = re.search(r"(?im)^## Appendix A\.\s+Summary of findings\s*$", text)
    if not appendix_at:
        rep.error("REPORT_final.md: no '## Appendix A. Summary of findings' heading")
        return
    if re.search(r"^\|\s*F?\d+\s*\|", text[: appendix_at.start()], re.M):
        rep.error("REPORT_final.md: a numbered findings table appears before Appendix A")
    check_layout(rep, text[: appendix_at.start()], findings)
    rows = []
    for block in tables(text[appendix_at.start():]):
        for row in block:
            c = cells(row)
            if c and re.fullmatch(r"F?\d+", c[0]):
                rows.append(c)
    if len(rows) != len(findings):
        rep.error(f"REPORT_final.md: Appendix A has {len(rows)} numbered rows but findings.json has "
                  f"{len(findings)} findings")
    for k, (row, f) in enumerate(zip(rows, findings), 1):
        if row[0] != str(k):
            rep.error(f"Appendix A row {k}: the # cell is {row[0]!r}; number the rows 1..n in order, as plain numbers")
        status = next((x for x in row if x.upper() in CONFIDENCE), None)
        if status and status.upper() != f.get("confidence"):
            rep.error(f"Appendix A row {k}: status {status} but findings.json F{k} has {f.get('confidence')}")
        yes_no = next((x for x in row if x.lower() in ("yes", "no")), None)
        if yes_no is None:
            rep.error(f"Appendix A row {k}: no Yes/No cell for 'Changes a published conclusion?'")
        elif (yes_no.lower() == "yes") != bool(f.get("changes_conclusion")):
            rep.error(f"Appendix A row {k}: says {yes_no} but findings.json F{k} changes_conclusion is "
                      f"{f.get('changes_conclusion')}")
    words = len(text[: appendix_at.start()].split())
    if words > 6000:
        rep.warn(f"REPORT_final.md: about {words} words before the appendix; unlikely to fit ten pages")


def validate(out: pathlib.Path, pkg: pathlib.Path | None = None, exempt: set[str] | None = None) -> dict:
    rep = Report()
    if exempt is None:
        exempt = deposit_headers(pkg)
    for name in REQUIRED:
        if (out / name).is_symlink():
            rep.error(f"{name} is a symbolic link; write the file itself")
        elif not (out / name).is_file():
            rep.error(f"missing {name}")
    evidence = out / "evidence"
    ev_files = []
    if evidence.is_dir() and not evidence.is_symlink():
        for top, _dirs, files in os.walk(evidence, followlinks=False):
            for name in files:
                path = pathlib.Path(top) / name
                if path.is_symlink():
                    rep.error(f"{path.relative_to(out)}: a symbolic link; evidence holds files you wrote")
                else:
                    ev_files.append(path)
    if not ev_files:
        rep.error("evidence/ holds no file; every quantitative claim needs a script that reproduces it")
    extras = sorted(p.name for p in out.iterdir() if p.name not in (*REQUIRED, "evidence")
                    and p.name not in (".DS_Store",)) if out.is_dir() else []
    if extras:
        rep.warn(f"files outside the output contract will not be kept: {', '.join(extras)}")
    findings: list[dict] = []
    if (out / "findings.json").is_file():
        try:
            findings = check_findings(rep, json.loads(read_text(out / "findings.json", rep, "findings.json")))
        except json.JSONDecodeError as exc:
            rep.error(f"findings.json: not valid JSON ({exc})")
    if (out / "REPORT_final.md").is_file():
        text = read_text(out / "REPORT_final.md", rep, "REPORT_final.md")
        check_report(rep, text, findings)
        if (out / "captions.txt").is_file():
            caps = [l for l in read_text(out / "captions.txt", rep, "captions.txt").splitlines() if l.strip()]
            n_tables = len(tables(text))
            if len(caps) != n_tables:
                rep.error(f"captions.txt has {len(caps)} captions but REPORT_final.md has {n_tables} tables")
            if any(re.match(r"\s*Table\s+\d+", c, re.I) for c in caps):
                rep.error("captions.txt: write the caption text only, without a 'Table N.' prefix")
    if (out / "front_matter.md").is_file():
        fm = read_text(out / "front_matter.md", rep, "front_matter.md")
        if 'custom-style="Title"' not in fm or "A comment on" not in fm:
            rep.error("front_matter.md: needs the Title div with 'A comment on \"<exact paper title>\"'")
        for needle in ("Original study:", "Replication package:"):
            if needle not in fm:
                rep.error(f"front_matter.md: the '{needle}' line is required")
        last = [l for l in fm.splitlines() if l.strip()][-1:] or [""]
        if re.fullmatch(r"\s*[-=]+\s*", last[0]):
            rep.error("front_matter.md: must not end with a line of only - or =")
    for name in COMMENT_FILES:
        if (out / name).is_file() and not (out / name).is_symlink():
            hygiene(rep, name, (out / name).read_bytes().decode("utf-8", errors="replace"), exempt, strict=True)
    for path in ev_files:
        rel = path.relative_to(out)
        suffix = path.suffix.lower()
        size = path.stat().st_size
        if suffix in BINARY_DATA or suffix in COMPRESSED:
            rep.error(f"{rel}: binary or compressed data in evidence/; it cannot be checked for participant rows and "
                      f"stays out of the shared tree. Keep scripts and aggregate text results only")
        elif suffix in TEXT_SUFFIXES:
            if size > 20 << 20:
                rep.error(f"{rel}: text larger than 20 MB in evidence/; keep aggregate results only")
                continue
            text = read_text(path, rep, str(rel))
            hygiene(rep, str(rel), text, exempt, strict=True)
            if suffix in DATA_SUFFIXES and text.count("\n") > 200:
                rep.warn(f"{rel}: more than 200 lines of data; it stays out of the shared tree, since evidence holds "
                         f"aggregate results, never participant rows")
        elif suffix == ".pdf":
            text = pdf_text(path)
            if text is None:
                rep.error(f"{rel}: a PDF whose text cannot be extracted cannot be checked; it stays out of the shared tree")
            else:
                hygiene(rep, str(rel), text, exempt, strict=True)
        elif suffix not in IMAGE_SUFFIXES:
            rep.error(f"{rel}: a file type that cannot be checked ({suffix or 'no extension'}); it stays out of the "
                      f"shared tree")
    return {"ok": not rep.errors, "errors": rep.errors, "warnings": rep.warnings,
            "counts": {"findings": len(findings), "evidence_files": len(ev_files)}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", type=pathlib.Path)
    ap.add_argument("--json", type=pathlib.Path)
    ap.add_argument("--pkg", type=pathlib.Path, help="the deposit, for header exemptions")
    args = ap.parse_args(argv)
    pkg, exempt = args.pkg, None
    config = pathlib.Path(__file__).with_name("validate_config.json")
    if config.is_file():
        settings = json.loads(config.read_text())
        pkg = pkg or pathlib.Path(settings["pkg"])
        exempt = set(settings["exempt"]) if "exempt" in settings else None
    result = validate(args.out.resolve(), pkg, exempt)
    if args.json:
        args.json.write_text(json.dumps(result, indent=1) + "\n")
    for msg in result["errors"]:
        print("ERROR  " + msg)
    for msg in result["warnings"]:
        print("WARN   " + msg)
    print(f"{'OK' if result['ok'] else 'FAIL'}: {len(result['errors'])} error(s), {len(result['warnings'])} warning(s), "
          f"{result['counts']['findings']} finding(s)")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
