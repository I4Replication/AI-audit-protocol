"""Render a baseline comment to DOCX and PDF the way the full pipeline renders its comments.

First choice: i4r-audit's build_docx.sh in preview mode (the pipeline's own converter, front matter divs, table and
link handling) on a staging folder, with the page cap lifted so a long single-pass comment still renders. If that
build refuses the document (for example an abstract that spills off page 1), a plain pandoc + LibreOffice build
follows, and the record says which renderer produced the files and the body pages the builder measured. As in the
pipeline, Appendix A and the findings cross-reference are generated from the findings record (findings.json), not
taken from the agent's hand-written table; the builder's exact input is kept as REPORT_rendered.md beside the agent's
REPORT_final.md. Rendering is presentation only: no model call, and a
failure never changes the run's outcome.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
from typing import Any


def _i4r_skill(tree: pathlib.Path) -> pathlib.Path | None:
    for path in (tree / "skill" / "i4r-audit-unified" / "i4r-audit", pathlib.Path.home() / ".claude" / "skills" / "i4r-audit"):
        if (path / "scripts" / "build_docx.sh").is_file():
            return path
    return None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    conda = pathlib.Path.home() / "miniconda3" / "envs" / "i4rdoc" / "bin"
    if conda.is_dir():
        env["PATH"] = f"{conda}:{env.get('PATH', '')}"
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def _tail(text: str, n: int = 1200) -> str:
    return text[-n:] if text else ""


BODY_PAGE_CAP = 10   # report_structure.md: the pipeline refuses a comment above it, or with the abstract off page 1
FINDINGS_HEADER = "| # | Finding | Status | Changes a published conclusion? | Section |"   # i4r document_release
APPENDIX = re.compile(r"(?m)^## Appendix A\b")


# --- i4r-audit 2.7.7 generate_finding_artifacts: the pipeline builds Appendix A and the findings cross-reference
# from its findings registry, never by hand. The two functions below are copied byte for byte (a test compares them
# with the live script); the table header and separator are those of its render().
APPENDIX_SEPARATOR = "|---:|" + "-" * 64 + "|" + "-" * 12 + "|:" + "-" * 10 + ":|" + "-" * 10 + "|"


def clean_cell(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "—")).replace("|", "\\|").strip()


def inject_report(report: str, appendix: str, claim_tags: str) -> str:
    # Structured tags have one owner. Remove an older generated block and legacy hand-keyed
    # tags before writing the canonical ordered block.
    report = re.sub(r"(?ms)^<!-- I4R-GENERATED-CLAIM-TAGS:START -->.*?"
                    r"^<!-- I4R-GENERATED-CLAIM-TAGS:END -->\s*", "", report)
    report = re.sub(r"\s*\[claim\s+[1-9][0-9]*\]", "", report, flags=re.I)
    start = re.search(r"(?m)^##\s+Appendix\s+A\b.*$", report, re.I)
    if start:
        following = re.search(r"(?m)^##\s+", report[start.end():])
        end = start.end() + following.start() if following else len(report)
        report = report[:start.start()].rstrip() + "\n\n" + appendix.rstrip() + "\n" + report[end:]
    else:
        report = report.rstrip() + "\n\n" + appendix.rstrip() + "\n"
    appendix_start = re.search(r"(?m)^##\s+Appendix\s+A\b", report, re.I)
    if not appendix_start:
        raise SystemExit("could not locate generated Appendix A in report projection")
    report = (report[:appendix_start.start()].rstrip() + "\n\n" + claim_tags.rstrip() +
              "\n\n" + report[appendix_start.start():].lstrip())
    return report.rstrip() + "\n"


def generated_blocks(findings: list) -> tuple[str, str] | None:
    """Appendix A and the findings cross-reference in render()'s layout, from findings.json (claim, confidence,
    changes_conclusion and section are the registry's claim, confidence, changes_conclusion and report_section).
    None when the record cannot be projected (ids not F1..Fn in order, a missing claim or status)."""
    appendix = ["## Appendix A. Summary of findings", "", FINDINGS_HEADER, APPENDIX_SEPARATOR]
    tags = ["<!-- I4R-GENERATED-CLAIM-TAGS:START -->", "### Findings cross-reference", ""]
    for n, row in enumerate(findings, 1):
        if not isinstance(row, dict) or row.get("id") != f"F{n}" or not row.get("claim") \
                or row.get("confidence") not in ("CONFIRMED", "LIKELY", "SPECULATIVE"):
            return None
        appendix.append("| " + " | ".join([str(n), clean_cell(row["claim"]), row["confidence"],
                                            "Yes" if row.get("changes_conclusion") else "No",
                                            clean_cell(row.get("section"))]) + " |")
        tags.append(f"- [claim {n}] {row.get('section')} — {row['claim']}")
    tags += ["", "<!-- I4R-GENERATED-CLAIM-TAGS:END -->"]
    return ("\n".join(appendix) + "\n", "\n".join(tags) + "\n") if findings else None


def split_appendix_a(text: str) -> tuple[str, str]:
    """The agent's Appendix A minus its findings table: the generator replaces the whole section, so anything else the
    agent put there (report_structure.md allows a deposited-files table) would be lost and the captions would no longer
    match the tables. Returns (text without that material, the material)."""
    start = re.search(r"(?m)^##\s+Appendix\s+A\b.*$", text, re.I)
    if not start:
        return text, ""
    following = re.search(r"(?m)^##\s+", text[start.end():])
    end = start.end() + following.start() if following else len(text)
    blocks = re.split(r"\n\s*\n", text[start.end():end].strip("\n"))
    findings_table = next((i for i, b in enumerate(blocks) if b.lstrip().startswith("| #")), None)
    rest = [b for i, b in enumerate(blocks) if i != findings_table and b.strip()]
    if findings_table is None or not rest:
        return text, ""
    kept = text[:start.end()] + "\n\n" + blocks[findings_table] + "\n" + (("\n" + text[end:]) if following else "")
    return kept, "\n\n".join(rest)


def rendered_report(folder: pathlib.Path) -> tuple[str, str]:
    """The text the builder receives: the agent's REPORT_final.md with Appendix A and the cross-reference generated
    from findings.json, as the pipeline's generator does. If findings.json cannot be projected, the agent's own table
    is kept with its ids written as plain numbers."""
    text = (folder / "REPORT_final.md").read_text(encoding="utf-8")
    try:
        findings = json.loads((folder / "findings.json").read_text(encoding="utf-8")).get("findings")
        blocks = generated_blocks(findings) if isinstance(findings, list) else None
    except (OSError, ValueError, AttributeError):
        blocks = None
    if blocks:
        text, extra = split_appendix_a(text)
        text = inject_report(text, *blocks)
        how = "generated from findings.json (i4r-audit generate_finding_artifacts layout)"
        if extra:   # as the one pipeline comment with a deposited-files table has it: its own Appendix B, after A
            files = re.search(r"(?mi)^\|\s*(File|Path|Deposited)", extra)
            title = "Deposited files" if files else "Supplementary material"
            text = text.rstrip() + f"\n\n## Appendix B. {title}\n\n" + extra.strip() + "\n"
            how += f"; the agent's other Appendix A material is printed as Appendix B. {title}"
        return text, how
    text, n = normalise_appendix_ids(text)
    return text, f"the agent's table ({n} ids written as plain numbers); findings.json could not be projected"


def normalise_appendix_ids(text: str) -> tuple[str, int]:
    """Appendix A's '#' cells as the pipeline's generator writes them (1, 2, ...). The builder sizes that column for
    plain numbers, so 'F10' and up would break across lines and the build would refuse. Presentation only: the
    comment in the tree keeps the agent's ids."""
    match = APPENDIX.search(text)
    if not match:
        return text, 0
    tail, n = re.subn(r"(?m)^\|\s*F(\d+)\s*\|", r"| \1 |", text[match.start():])
    return text[:match.start()] + tail, n


def render(folder: pathlib.Path, tree: pathlib.Path) -> dict:
    """Never raises: a failure is recorded and the run's outcome stands."""
    try:
        return _render(folder, tree)
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        return {"renderer": None, "ok": False, "files": [], "error": f"{type(exc).__name__}: {exc}"[:800]}


def _pages(manifest_path: pathlib.Path) -> dict:
    try:
        pages = json.loads(manifest_path.read_text()).get("pages") or {}
    except (OSError, ValueError):
        return {}
    keep = {k: pages.get(k) for k in ("body_pages", "back_matter_pages", "total_pages", "abstract_ending_page")}
    if isinstance(keep["body_pages"], int):
        keep["body_page_cap"] = BODY_PAGE_CAP
        keep["over_page_cap"] = keep["body_pages"] > BODY_PAGE_CAP or keep["abstract_ending_page"] != 1
    return keep


def _refused_pages(diagnostic: pathlib.Path, log: str) -> dict:
    """When the pipeline's builder refuses the document at a validation, its page measurements survive only as text
    in the validations' details (and the log): 'N body page(s), cap M' and 'abstract ends on page N'."""
    found: dict = {}
    try:
        details = " ".join(str(v.get("detail", "")) for v in json.loads(diagnostic.read_text()).get("validations") or []
                           if isinstance(v, dict))
    except (OSError, ValueError, AttributeError):
        details = ""
    for key, pattern in (("body_pages", r"(\d+) body page\(s\), cap \d+"), ("abstract_ending_page", r"abstract ends on page (\d+)")):
        match = re.search(pattern, details + "\n" + log)
        if match:
            found[key] = int(match.group(1))
    if not found:
        return {}
    found["body_page_cap"] = BODY_PAGE_CAP
    abstract, body = found.get("abstract_ending_page"), found.get("body_pages")
    found["over_page_cap"] = (abstract is not None and abstract != 1) or (body is not None and body > BODY_PAGE_CAP)
    found["source"] = "the pipeline builder's refusal"
    return found


def _measure_without_table_check(stage: pathlib.Path, skill: pathlib.Path) -> dict:
    """After the builder's Appendix A word check refused, rebuild once with the table header escaped (an ordinary table
    to the builder) only to measure the pages: the body is the same, so its pages are the refused build's."""
    report = stage / "report" / "REPORT_final.md"
    try:
        refused_at = json.loads((stage / "document_build_diagnostics" / "last_failure.json").read_text()).get("stage")
        text = report.read_text(encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        return {}
    if refused_at != "findings_table_integrity" or FINDINGS_HEADER not in text:
        return {}
    report.write_text(text.replace(FINDINGS_HEADER, "| \\#" + FINDINGS_HEADER[3:], 1), encoding="utf-8")
    try:
        again = subprocess.run(["bash", str(skill / "scripts" / "build_docx.sh"), "--no-cache", "--preview-max-pages",
                                "99", str(stage), "comment"], capture_output=True, text=True, env=_env(), timeout=900)
    finally:
        report.write_text(text, encoding="utf-8")
    pages = _pages(stage / "report" / "previews" / "comment.manifest.json") if again.returncode == 0 else {}
    pages = {k: v for k, v in pages.items() if k in ("body_pages", "abstract_ending_page", "body_page_cap", "over_page_cap")}
    if pages:
        pages["source"] = "i4r rebuild with Appendix A as an ordinary table, after the builder's word check refused it"
    return pages


def _render(folder: pathlib.Path, tree: pathlib.Path) -> dict:
    record: dict = {"renderer": None, "ok": False, "files": []}
    skill = _i4r_skill(tree)
    with tempfile.TemporaryDirectory(prefix="baseline-render-") as tmp:
        stage = pathlib.Path(tmp) / "doc"
        (stage / "report").mkdir(parents=True)
        for name in ("front_matter.md", "REPORT_final.md", "captions.txt"):
            if (folder / name).is_file():
                shutil.copy2(folder / name, stage / "report" / name)
        (stage / "report" / "captions.txt").touch()
        staged = stage / "report" / "REPORT_final.md"
        if staged.is_file():
            text, record["appendix"] = rendered_report(folder)
            staged.write_text(text, encoding="utf-8")
            (folder / "REPORT_rendered.md").write_text(text, encoding="utf-8")   # the builder's exact input
        if skill:
            version = (skill / "VERSION").read_text().strip()
            freeze = subprocess.run(
                ["python3", "-B", "-c", "import sys,pathlib; sys.path.insert(0, sys.argv[1]); import record_invocation; "
                 "record_invocation.freeze_policy_snapshot(pathlib.Path(sys.argv[2]))", str(skill / "scripts"), str(stage)],
                capture_output=True, text=True, env=_env(), timeout=120)
            build = subprocess.run(["bash", str(skill / "scripts" / "build_docx.sh"), "--no-cache",
                                    "--preview-max-pages", "99", str(stage), "comment"],
                                   capture_output=True, text=True, env=_env(), timeout=900) if freeze.returncode == 0 else freeze
            made = {p.suffix: p for p in (stage / "report" / "previews").glob("comment.*")} if (stage / "report" / "previews").is_dir() else {}
            if build.returncode == 0 and ".pdf" in made and ".docx" in made:
                for suffix in (".pdf", ".docx"):
                    shutil.copy2(made[suffix], folder / f"comment{suffix}")
                return {**record, "renderer": f"i4r-audit {version} build_docx.sh (preview, page cap lifted)",
                        "ok": True, "files": ["comment.pdf", "comment.docx"],
                        "pages": _pages(stage / "report" / "previews" / "comment.manifest.json")}
            log = build.stdout + build.stderr
            record["i4r_build_refused"] = _tail(log)
            record["pages"] = _refused_pages(stage / "document_build_diagnostics" / "last_failure.json", log)
            if freeze.returncode == 0 and not record["pages"].get("body_pages"):
                record["pages"] = _measure_without_table_check(stage, skill) or record["pages"]
        pandoc = shutil.which("pandoc", path=_env()["PATH"])
        soffice = shutil.which("soffice", path=_env()["PATH"]) or (
            "/Applications/LibreOffice.app/Contents/MacOS/soffice"
            if pathlib.Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").exists() else None)
        if not pandoc:
            record["error"] = "pandoc not found"
            return record
        full = stage / "full.md"
        full.write_text((folder / "front_matter.md").read_text(encoding="utf-8") + "\n\n"
                        + (staged.read_text(encoding="utf-8") if staged.is_file()
                           else (folder / "REPORT_final.md").read_text(encoding="utf-8")), encoding="utf-8")
        docx = stage / "comment.docx"
        conv = subprocess.run([pandoc, str(full), "-f", "markdown+pipe_tables", "-t", "docx", "-o", str(docx)],
                              capture_output=True, text=True, env=_env(), timeout=300)
        if conv.returncode != 0:
            record["error"] = _tail(conv.stderr)
            return record
        shutil.copy2(docx, folder / "comment.docx")
        record.update(renderer="pandoc + LibreOffice (fallback)", files=["comment.docx"])
        if soffice:
            profile = pathlib.Path(tmp) / "lo"
            pdf = subprocess.run([soffice, "--headless", f"-env:UserInstallation=file://{profile}", "--convert-to",
                                  "pdf", "--outdir", str(stage), str(docx)],
                                 capture_output=True, text=True, env=_env(), timeout=300)
            if pdf.returncode == 0 and (stage / "comment.pdf").is_file():
                shutil.copy2(stage / "comment.pdf", folder / "comment.pdf")
                record["files"].append("comment.pdf")
        record["ok"] = "comment.pdf" in record["files"]
        if record["ok"] and not record.get("pages"):
            pdfinfo = shutil.which("pdfinfo", path=_env()["PATH"])
            total = None
            if pdfinfo:
                info = subprocess.run([pdfinfo, str(folder / "comment.pdf")], capture_output=True, text=True, timeout=60)
                total = next((int(l.split()[-1]) for l in info.stdout.splitlines() if l.startswith("Pages:")), None)
            record["pages"] = {"total_pages": total, "body_pages": None, "over_page_cap": None,
                               "note": "fallback build: body pages and the abstract's page are not measured"}
    return record
