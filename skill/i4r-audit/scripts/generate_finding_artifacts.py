#!/usr/bin/env python3
"""Generate repeated audit artifacts from the canonical findings JSONL registry."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any


FINAL_STATUSES = {"confirmed", "verified", "accepted"}


def strict_json_text(text: str, label: str) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in values:
            if key in out:
                raise ValueError(f"{label}: duplicate JSON key {key!r}")
            out[key] = value
        return out
    def constant(value: str) -> None:
        raise ValueError(f"{label}: non-finite JSON number {value!r}")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def atomic_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SystemExit(f"findings registry is missing: {path}")
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = strict_json_text(line, f"{path}:{line_no}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise SystemExit(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise SystemExit(f"{path}:{line_no}: each JSONL value must be an object")
        rows.append(row)
    return rows


def validate_rows(rows: list[dict[str, Any]], schema_path: pathlib.Path) -> None:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "schemas/tools"))
    import validate_json_schema
    import validate_v2
    errors: list[str] = []
    for line_no, row in enumerate(rows, 1):
        for error in validate_json_schema.errors_for_value(row, schema_path):
            errors.append(f"line {line_no}{error}")
        try:
            validate_v2.validate_finding_row(row, f"line {line_no}")
        except validate_v2.ValidationError as exc:
            errors.append(str(exc))
    ids = [str(row.get("id")) for row in rows]
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        errors.append("duplicate stable finding IDs: " + ", ".join(duplicates))
    known = set(ids)
    for line_no, row in enumerate(rows, 1):
        dependencies = set((row.get("dependencies") or {}).get("finding_ids") or [])
        missing = sorted(dependencies - known)
        if missing:
            errors.append(f"line {line_no}: missing dependency finding IDs: {', '.join(missing)}")
        disposition = row.get("ledger_disposition") or {}
        if disposition.get("status") == "MERGED" and disposition.get("target") not in known:
            errors.append(f"line {line_no}: merge target does not exist: {disposition.get('target')!r}")
        if (row.get("status") in FINAL_STATUSES and row.get("coordinator_approved") is True
                and disposition.get("status") != "INCLUDED"):
            errors.append(f"line {line_no}: approved final status must have INCLUDED disposition")
    if errors:
        raise SystemExit("findings registry is invalid:\n  " + "\n  ".join(errors[:40]))


def number(row: dict[str, Any]) -> int:
    match = re.fullmatch(r"F([1-9][0-9]*)", str(row.get("id") or ""))
    if not match:
        raise SystemExit(f"final finding ID must be F<number>: {row.get('id')!r}")
    return int(match.group(1))


def clean_cell(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "—")).replace("|", "\\|").strip()


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def evidence_projection(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row["evidence"]
    paths = [item["path"] for item in evidence["source_hashes"]]
    return {
        "artifact": " + ".join(paths),
        "operation": evidence["rerun_command"],
        "expected": evidence.get("expected"),
        "observed": evidence.get("observed"),
        "tolerance_note": evidence.get("tolerance_basis"),
    }


def comment_projection(row: dict[str, Any]) -> dict[str, Any]:
    remedy = row["remedy"]
    provenance = row["provenance"]
    snippet = row.get("verification_snippet") or {}
    suffix = {"code": ".R", "judgement": ".judgement",
              "unverifiable": ".unverifiable"}.get(snippet.get("kind"))
    verification_script = (f"verification_snippets/C-{number(row)}{suffix}"
                           if suffix is not None else None)
    verifiers = []
    outcome_map = {
        "confirmed": "reproduced", "pass": "reproduced", "verified": "reproduced",
        "refuted": "not_reproduced", "inconclusive": "inconclusive",
    }
    for verifier in row["verifications"]:
        verifiers.append({
            "verifier": verifier["reviewer_id"], "at": None,
            "method": row["evidence"]["rerun_command"],
            "outcome": outcome_map[verifier["outcome"]], "notes": None,
        })
    out: dict[str, Any] = {
        "id": row["id"], "claim": row["claim"],
        "section": row.get("report_section") or row.get("claim_ref"),
        "checklist_item": None, "impact": row["impact"], "impact_rank": row["impact_rank"],
        "locus": None, "verification_tier": row["verification_tier"],
        "expected_verifier_hours": None, "actual_verifier_hours": None,
        "paper_only": row["paper_only"],
        "unverifiable_reason": (row.get("unverifiable_basis") or {}).get("reason"),
        "blocked_tier": (row.get("unverifiable_basis") or {}).get("blocked_tier"),
        "unverifiable_basis": (row.get("unverifiable_basis") or {}).get("basis"),
        "check_nature": None, "claim_ref": row.get("claim_ref"),
        "evidence": evidence_projection(row), "consistent_with": None,
        "disclosure_flag": row["disclosure_flag"], "remedy_class": remedy["class"],
        "remedy_description": remedy.get("description"), "remedy_verified": remedy.get("verified"),
        "verification_script": verification_script,
        "changes_conclusion": row["changes_conclusion"],
        "changes_conclusion_which": row.get("changes_conclusion_which"),
        "confidence": row["confidence"],
        "benign_explanations_ruled_out": row.get("benign_explanations_ruled_out") or [],
        "affects": [],
        "package_locus": [x["path"] for x in row["evidence"]["source_hashes"]],
        "verification": {
            "verifier_count": len(verifiers), "verifiers": verifiers,
            "all_reproduced": bool(verifiers) and all(x["outcome"] == "reproduced" for x in verifiers),
        },
        "provenance": {
            "origin_role": provenance["origin_role"],
            "independently_found_by": provenance["independently_found_by"],
            "independent_corroboration_count": len(provenance["independently_found_by"]),
            "agreed_after_seeing": provenance["agreed_after_seeing"],
            "auditor_report": provenance["source_records"][0] if provenance["source_records"] else None,
            "adjudication_entry": None, "ledger_row": row["id"],
            "round_introduced": provenance["round_introduced"],
            "contested_by_critic": None, "survived_contest": None,
        },
    }
    return out


def ensure_final_invariants(rows: list[dict[str, Any]], root: pathlib.Path, quiet: bool = False) -> list[dict[str, Any]]:
    final = [
        r for r in rows
        if r.get("status") in FINAL_STATUSES
        and r.get("coordinator_approved") is True
        and (r.get("ledger_disposition") or {}).get("status") == "INCLUDED"
    ]
    final.sort(key=number)
    nums = [number(r) for r in final]
    # Every INCLUDED row, approved or not: a withdrawal gap below an unapproved finding otherwise surfaces only
    # rounds later, once that finding is approved. Draft ids (before the round-1 renumber) are exempt.
    # Once every INCLUDED id is final, contiguity is a property of the INCLUDED rows: an approval deferred to a
    # later round (integrate_round's remedy) leaves a hole among the APPROVED ones that is not a withdrawal gap,
    # and finalize refuses a stop while any INCLUDED finding is still unapproved.
    included = [r.get("id") for r in rows if (r.get("ledger_disposition") or {}).get("status") == "INCLUDED"]
    if included and all(re.fullmatch(r"F[1-9][0-9]*", str(i)) for i in included):
        nums = sorted(int(str(i)[1:]) for i in included)
    approved_nums = [number(r) for r in final]
    if not quiet and nums == list(range(1, len(nums) + 1)) and approved_nums != list(range(1, len(approved_nums) + 1)):
        pending = sorted(set(nums) - set(approved_nums))
        print(f"note: INCLUDED finding(s) {['F%d' % n for n in pending]} are not yet approved, so the projection "
              f"numbers the approved ones {['F%d' % n for n in approved_nums]} with a gap; approve or withdraw them "
              "before the stop (finalize refuses target_complete/converged, and a cap_with_unresolved or aborted "
              "release would ship the gap)", file=sys.stderr)
    if nums != list(range(1, len(nums) + 1)):
        raise SystemExit(
            f"final finding IDs must be contiguous F1..F{len(nums)}; observed {nums}. A finding withdrawn at "
            "integration leaves this gap: remove its report marker, then run renumber_findings.py --apply with "
            "--also for that round's REPORT_/FINDINGS_BEFORE_INTEGRATION snapshots and report/ADJUDICATION.md "
            "(every round's RESPONSE_notes.md is rewritten automatically; INSTALL.md, 'Renumbering').")
    seen: set[str] = set()
    for row in final:
        if row["id"] in seen:
            raise SystemExit(f"duplicate finding ID: {row['id']}")
        seen.add(row["id"])
        section = row.get("report_section") or row.get("claim_ref")
        if not isinstance(section, str) or not re.search(r"(?:Section|§)?\s*\d+", section, re.I):
            raise SystemExit(f"{row['id']}: report_section must identify a numbered section")
        if row["changes_conclusion"] and not row.get("changes_conclusion_which"):
            raise SystemExit(f"{row['id']}: conclusion-changing finding lacks changes_conclusion_which")
        if row["changes_conclusion"] and len(row["verifications"]) < 2:
            raise SystemExit(f"{row['id']}: conclusion-changing finding lacks two verifications")
        for source in row["evidence"]["source_hashes"]:
            path = (root / source["path"]).resolve()
            try:
                path.relative_to(root.resolve())
            except ValueError as exc:
                raise SystemExit(f"{row['id']}: evidence path escapes audit directory") from exc
            if not path.is_file():
                raise SystemExit(f"{row['id']}: missing evidence file {source['path']}")
            digest = file_sha256(path)
            if digest != source["sha256"]:
                raise SystemExit(f"{row['id']}: evidence hash changed for {source['path']}")
    return final


def disposition_text(row: dict[str, Any]) -> str:
    disposition = row["ledger_disposition"]
    status = disposition["status"]
    if status == "MERGED":
        return f"MERGED into {disposition['target']}"
    if status == "EXCLUDED":
        return f"EXCLUDED — {disposition['reason']}"
    return "INCLUDED"


def render(all_rows: list[dict[str, Any]], final_rows: list[dict[str, Any]]) -> dict[str, str]:
    ledger = ["# Findings ledger — generated from work/findings.jsonl", "",
              "Do not edit this projection; edit the canonical JSONL registry and regenerate.", "",
              "| ID | Disposition | Status | Finding | Impact | Evidence | Remedy | Verification |",
              "|---|---|---|---|---|---|---|---|"]
    # Pipe-table separator dash counts set the relative column widths when the table wraps
    # (pandoc); the Finding column carries the text, so it takes most of the width. The header
    # follows reference/report_structure.md (Status).
    appendix = ["## Appendix A. Summary of findings", "",
                "| # | Finding | Status | Changes a published conclusion? | Section |",
                "|---:|" + "-" * 64 + "|" + "-" * 12 + "|:" + "-" * 10 + ":|" + "-" * 10 + "|"]
    buf = io.StringIO(); writer = csv.writer(buf, delimiter="\t", lineterminator="\n")
    plan: list[dict[str, Any]] = []
    claim_tags = ["<!-- I4R-GENERATED-CLAIM-TAGS:START -->",
                  "### Findings cross-reference", ""]
    for row in all_rows:
        ev = row["evidence"]; remedy = row["remedy"]
        ledger.append("| " + " | ".join([
            row["id"], clean_cell(disposition_text(row)), row["status"],
            clean_cell(row["claim"]), row["impact"],
            clean_cell(ev["rerun_command"]), clean_cell(remedy.get("description")),
            f"{len(row['verifications'])} independent record(s)",
        ]) + " |")

    for row in final_rows:
        n = number(row); ev = row["evidence"]
        appendix.append("| " + " | ".join([
            str(n), clean_cell(row["claim"]), row["confidence"],
            "Yes" if row["changes_conclusion"] else "No",
            clean_cell(row.get("report_section") or row.get("claim_ref")),
        ]) + " |")
        snippet = row.get("verification_snippet") or {}
        source = f"snippet:verification_snippets/C-{n}.R" if snippet.get("kind") == "code" else "none"
        writer.writerow([row["id"], source])
        claim_tags.append(f"- [claim {n}] {row.get('report_section') or row.get('claim_ref')} — {row['claim']}")
        plan.append({"claim_id": f"C-{n}", "finding_id": row["id"],
                     "snippet": row.get("verification_snippet"),
                     "evidence": ev, "verifications": row["verifications"]})
    claim_tags += ["", "<!-- I4R-GENERATED-CLAIM-TAGS:END -->"]
    settled_candidates = {
        "schema_version": "2.0.0",
        "candidates": [{
            "finding_id": row["id"], "claim": row["claim"],
            "coordinator_approved": row["coordinator_approved"],
            "independent_confirmations": len({v["invocation_id"] for v in row["verifications"]
                                               if v["outcome"] in {"confirmed", "verified", "pass"}}),
            "confirmation_bindings": [v for v in row["verifications"]
                                      if v["outcome"] in {"confirmed", "verified", "pass"}],
            "rerun_command": row["evidence"]["rerun_command"],
            "source_hashes": row["evidence"]["source_hashes"],
            "environment_fingerprint": row["dependencies"]["environment_fingerprint"],
        } for row in final_rows if (row.get("settled") or {}).get("approved") is True],
    }
    return {"ledger": "\n".join(ledger) + "\n", "appendix": "\n".join(appendix) + "\n",
            "claims_map": buf.getvalue(), "verification_plan": json.dumps(plan, indent=2) + "\n",
            "comment_findings": json.dumps([comment_projection(r) for r in final_rows], indent=2) + "\n",
            "claim_tags": "\n".join(claim_tags) + "\n",
            "settled_candidates": json.dumps(settled_candidates, indent=2) + "\n"}


def _called_functions(source: pathlib.Path) -> set[str] | None:
    """Function names the R parser sees called in `source`, or None when R cannot answer.

    Asking R's own parser is the only exact way to tell a real call from one in a comment or a
    string (raw strings r"(...)" included); a regex over the text cannot. None (no Rscript, a
    timeout, or a parse error that the notebook render will report anyway) falls back to the
    text heuristic.
    """
    rscript = shutil.which("Rscript")
    if not rscript:
        return None
    code = ('p <- tryCatch(parse(file = commandArgs(TRUE)[1], keep.source = TRUE), error = function(e) NULL); '
            'if (is.null(p)) quit(status = 3); d <- utils::getParseData(p); '
            'cat(unique(d$text[d$token == "SYMBOL_FUNCTION_CALL"]), sep = "\\n")')
    try:
        # Own process group, so a timeout kills Rscript's children too, not just the launcher.
        proc = subprocess.Popen([rscript, "--vanilla", "-e", code, str(source)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
    except OSError:
        return None
    try:
        stdout, _ = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.communicate()
        return None
    if proc.returncode != 0:
        return None
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def prepare_snippets(rows: list[dict[str, Any]], root: pathlib.Path) -> dict[str, str]:
    prepared: dict[str, str] = {}
    for row in rows:
        spec = row.get("verification_snippet")
        if not isinstance(spec, dict):
            raise SystemExit(f"{row['id']}: verification_snippet is required for artifact generation")
        kind, path, basis = spec.get("kind"), spec.get("path"), spec.get("basis")
        suffix = {"code": ".R", "judgement": ".judgement", "unverifiable": ".unverifiable"}.get(kind)
        if suffix is None:
            raise SystemExit(f"{row['id']}: invalid verification snippet kind")
        name = f"C-{number(row)}{suffix}"
        if kind == "code":
            source = (root / str(path)).resolve()
            try: source.relative_to(root)
            except ValueError as exc:
                raise SystemExit(f"{row['id']}: snippet path escapes audit directory: {path}") from exc
            if not source.is_file(): raise SystemExit(f"{row['id']}: snippet file missing: {path}")
            # make_verification_notebook.sh inlines every executable snippet into an R chunk.
            # Silently relabelling Python/Stata/etc. as C-N.R produces a notebook that can only
            # fail at render time and makes the generated claims map misstate the language.
            # Fail at projection until the registry carries an explicit language and the
            # notebook has a corresponding execution contract.
            if source.suffix.lower() != ".r":
                raise SystemExit(
                    f"{row['id']}: code verification snippet must be R source because the "
                    f"generated notebook executes it in an R chunk: {path}"
                )
            content = source.read_text(encoding="utf-8")
            # The file is copied verbatim over verification_snippets/C-N.R on every --apply, and
            # the notebook records an outcome only through expect()/check(). A print-only script
            # otherwise surfaces three steps later as "claim chunk produced no outcome".
            calls = _called_functions(source)
            if calls is None:
                # No R parser available: drop comments and string contents as a best effort, so
                # neither a commented-out call nor a call named inside a string counts.
                live = re.sub(r'[rR](["\'])(-*)\(.*?\)\2\1'   # raw strings: the closing bracket
                              r'|[rR](["\'])(-*)\[.*?\]\4\3'   # must match the opening one:
                              r'|[rR](["\'])(-*)\{.*?\}\6\5'   # r"(...)", r"[...]", r"-{...}-"
                              r'|"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|(#[^\n]*)',
                              lambda m: "" if m.group(7) else '""', content, flags=re.S)
                calls = {name for name in ("expect", "check") if re.search(rf"\b{name}\s*\(", live)}
            if not {"expect", "check"} <= calls:
                raise SystemExit(
                    f"{row['id']}: code verification snippet {path} has no expect(...) and check(...) "
                    f"calls; the notebook would record no outcome. Write it in the notebook's "
                    f"expect()/check() form (see reference/verification_guide.md). Edit {path}, not "
                    f"verification_snippets/{name}, which is regenerated from it on every --apply."
                )
            own = f"C-{number(row)}"
            # Comments dropped, string literals kept (the ids ARE strings): a comment that mentions an
            # old id records nothing and must not be refused.
            uncommented = re.sub(r'[rR](["\'])(-*)[(\[{].*?[)\]}]\2\1|"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|(#[^\n]*)',
                                 lambda m: "" if m.group(3) else m.group(0), content, flags=re.S)
            stale = sorted({m.group(3) for m in re.finditer(
                r"\b(expect|check|judgement|unverifiable)\s*\(\s*(?:id\s*=\s*)?([\"'])(C-[^\"']*)\2", uncommented)
                if m.group(3) != own})
            if stale:
                # After a renumber the source still names the finding's old claim id; the notebook would
                # fail it later with no file named, and editing the generated copy only defers the refusal.
                raise SystemExit(
                    f"{row['id']}: code verification snippet {path} records outcomes under {', '.join(stale)}, "
                    f"but this finding is {own}. Change those expect()/check() ids to \"{own}\" in {path}, "
                    f"then re-run generate_finding_artifacts.py --apply.")
        else:
            if not isinstance(basis, str) or not basis.strip():
                raise SystemExit(f"{row['id']}: judgement/unverifiable snippet needs a basis")
            content = basis.strip() + "\n"
        prepared[name] = content
    return prepared


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


CLAIM_HEADER=re.compile(r"\bCLAIM\s+([1-9][0-9]*)\s+\[")


def parse_claim_numbers(text: str) -> list[int]:
    return [int(value) for value in CLAIM_HEADER.findall(text)]


def check_claims_join(claim_numbers: list[int], expected_numbers: list[int]) -> None:
    """Every finding claimed exactly once, no claim invented, none duplicated.

    A PERMUTATION check, not an ordering one. make_claims_script.sh emits every claim it can
    execute in order and then an explicit no-code tail for claims verified by inspection --
    which SKILL.md states as the required shape. Demanding strict ascending order contradicted
    that and failed any audit carrying a judgement or unverifiable claim; the first production
    audit's own claims script is [1,2,3,5,6,7,8,10,11,12,13,14,16,17,4,9,15]. Ordering here is
    presentational.
    """
    if sorted(claim_numbers) == expected_numbers:
        return
    missing=[n for n in expected_numbers if n not in claim_numbers]
    extra=[n for n in claim_numbers if n not in expected_numbers]
    duplicated=sorted({n for n in claim_numbers if claim_numbers.count(n) > 1})
    raise SystemExit(
        "claims script does not join findings exactly: "
        f"missing={missing} unexpected={extra} duplicated={duplicated} observed={claim_numbers}")


def safe_target(root: pathlib.Path, relative: str) -> pathlib.Path:
    rel = pathlib.PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError(f"unsafe generated target: {relative!r}")
    target = root.joinpath(*rel.parts)
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"generated target parent is a symlink: {current.relative_to(root)}")
    if target.is_symlink():
        raise ValueError(f"generated target is a symlink: {relative}")
    try: target.parent.resolve().relative_to(root.resolve())
    except ValueError as exc: raise ValueError(f"generated target escapes audit root: {relative}") from exc
    return target


def restore_generation(root: pathlib.Path, marker: pathlib.Path) -> None:
    if marker.is_symlink() or not marker.is_file():
        raise SystemExit("unsafe finding-generation recovery marker")
    state = strict_json_text(marker.read_text(), str(marker))
    tx_name = state.get("transaction_dir")
    if not isinstance(tx_name, str) or not re.fullmatch(r"\.finding-generation\.[A-Za-z0-9._-]+", tx_name):
        raise SystemExit("unsafe transaction directory in finding-generation marker")
    tx = root / tx_name
    try: tx.resolve().relative_to(root.resolve())
    except ValueError as exc: raise SystemExit("finding-generation transaction escapes audit root") from exc
    backup = tx / "backup"
    if not tx.is_dir() or not backup.is_dir():
        raise SystemExit("finding-generation recovery backup is missing; refusing destructive recovery")
    targets = state.get("targets")
    if not isinstance(targets, list) or any(not isinstance(item, str) for item in targets):
        raise SystemExit("invalid targets in finding-generation marker")
    for relative in targets:
        try: target = safe_target(root, relative)
        except ValueError as exc: raise SystemExit(str(exc)) from exc
        if target.is_file() or target.is_symlink(): target.unlink()
    if backup.is_dir():
        for source in sorted(backup.rglob("*")):
            if source.is_file():
                relative = source.relative_to(backup).as_posix()
                try: target = safe_target(root, relative)
                except ValueError as exc: raise SystemExit(str(exc)) from exc
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)
    shutil.rmtree(tx, ignore_errors=True)
    marker.unlink(missing_ok=True)


def apply_transaction(root: pathlib.Path, registry: pathlib.Path, rendered: dict[str, str],
                      final: list[dict[str, Any]], snippets: dict[str, str], here: pathlib.Path) -> None:
    marker = root / ".finding-generation-commit.json"
    if marker.exists():
        restore_generation(root, marker)
        print("recovered an interrupted finding-projection transaction", file=sys.stderr)
    report_path = root / "report/REPORT_final.md"
    comment_path = root / "comment.json"
    if not report_path.is_file() or not comment_path.is_file():
        raise SystemExit("--apply requires report/REPORT_final.md and comment.json")
    if not final:
        # Before critique every row is unapproved. Applying here would replace the drafted Appendix A in
        # REPORT_final.md with an empty table and strip every claim tag, while critics may be
        # reviewing that very file. Preview without --apply until at least one row is approved.
        raise SystemExit(
            "--apply refused: no coordinator-approved INCLUDED row is final, so the projection "
            "would rewrite REPORT_final.md with an empty Appendix A. Run without --apply to "
            "preview work/generated/, and apply after Round 1 integration approves rows."
        )
    try:
        comment = strict_json_text(comment_path.read_text(), str(comment_path))
    except (json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"comment.json is invalid: {exc}") from exc
    comment["findings"] = [comment_projection(row) for row in final]
    report_text = inject_report(report_path.read_text(), rendered["appendix"], rendered["claim_tags"])

    contents = {
        "report/LEDGER.md": rendered["ledger"],
        "report/APPENDIX_A.md": rendered["appendix"],
        "work/claim_tags.generated.md": rendered["claim_tags"],
        "claims_map.tsv": rendered["claims_map"],
        "work/verification_plan.json": rendered["verification_plan"],
        "work/comment_findings.generated.json": rendered["comment_findings"],
        "work/settled_candidates.json": rendered["settled_candidates"],
        "report/REPORT_final.md": report_text,
        "comment.json": json.dumps(comment, indent=2, ensure_ascii=False) + "\n",
    }
    claim_extensions = ("R", "do", "sps", "py", "jl", "m", "sas")
    targets = set(contents) | {"verification.Rmd", "work/finding_projections.manifest.json"}
    targets |= {f"claims_script.{extension}" for extension in claim_extensions}
    existing_snippets = {str(path.relative_to(root)) for path in (root / "verification_snippets").glob("C-*")
                         if path.is_file()}
    targets |= existing_snippets | {f"verification_snippets/{name}" for name in snippets}
    safe_targets = {relative: safe_target(root, relative) for relative in targets}

    tx = pathlib.Path(tempfile.mkdtemp(prefix=".finding-generation.", dir=root))
    backup = tx / "backup"
    for relative in sorted(targets):
        source = safe_targets[relative]
        if source.is_file():
            destination = backup / relative; destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
    atomic_text(marker, json.dumps({"transaction_dir": tx.name, "targets": sorted(targets)}, indent=2) + "\n")
    try:
        for relative in existing_snippets:
            (root / relative).unlink(missing_ok=True)
        for extension in claim_extensions:
            (root / f"claims_script.{extension}").unlink(missing_ok=True)
        for relative, content in contents.items(): atomic_text(root / relative, content)
        for name, content in snippets.items(): atomic_text(root / "verification_snippets" / name, content)

        for helper_script in ("make_verification_notebook.sh", "make_claims_script.sh"):
            child = subprocess.run(["bash", str(here / "scripts" / helper_script), str(root)],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if child.returncode:
                # Surface the child's own diagnostics; a bare CalledProcessError hid the cause of
                # the first production audit's projection rollback.
                tail = "\n".join((child.stdout or "").strip().splitlines()[-12:])
                raise RuntimeError(f"{helper_script} exited {child.returncode}:\n{tail}")
        claim_files = [path for path in root.glob("claims_script.*") if path.is_file()]
        if len(claim_files) != 1 or not (root / "verification.Rmd").is_file():
            raise RuntimeError("notebook/claims assembly did not produce its exact output quorum")
        artifact_paths = sorted((set(contents) - {"comment.json"}) |
                                {"verification.Rmd", str(claim_files[0].relative_to(root))} |
                                {f"verification_snippets/{name}" for name in snippets})
        projection = {
            "schema_version": "2.0.0",
            "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "registry": {"path": str(registry.resolve().relative_to(root)),
                         "sha256": file_sha256(registry)},
            "finding_ids": [row["id"] for row in final],
            "artifacts": [{"path": relative, "sha256": file_sha256(root / relative)}
                          for relative in artifact_paths],
        }
        atomic_text(root / "work/finding_projections.manifest.json",
                    json.dumps(projection, indent=2, ensure_ascii=False) + "\n")
    except Exception as exc:
        restore_generation(root, marker)
        raise SystemExit(f"finding projection transaction rolled back: {exc}") from exc
    marker.unlink(missing_ok=True); shutil.rmtree(tx, ignore_errors=True)


def check_projection(root: pathlib.Path, registry: pathlib.Path, rendered: dict[str, str],
                     final: list[dict[str, Any]], snippets: dict[str, str]) -> None:
    manifest_path = root / "work/finding_projections.manifest.json"
    if not manifest_path.is_file(): raise SystemExit("finding projection manifest is missing")
    manifest = strict_json_text(manifest_path.read_text(), str(manifest_path))
    expected_registry = str(registry.resolve().relative_to(root))
    if ((manifest.get("registry") or {}).get("path") != expected_registry or
            (manifest.get("registry") or {}).get("sha256") != file_sha256(registry)):
        raise SystemExit(
            "finding projection manifest does not match the canonical registry.\n"
            "  work/findings.jsonl has changed since the projection was written. The usual cause\n"
            "  is approve_settled.py, which binds the current report hash into each row's\n"
            "  `settled` object AFTER the projection ran (make_settled.sh only reads the registry).\n"
            "  Re-run the chain (the projection can change the report, which re-binds the settled cache):\n"
            "      approve_settled.py -> generate_finding_artifacts.py --apply -> approve_settled.py -> "
            "generate_finding_artifacts.py --apply -> make_settled.sh -> re-log settled -> "
            "make_diff_packet.py --round N -> run_post_delta_gates.py --round N (then stage, if a release is due)")
    finding_ids = [row["id"] for row in final]
    if manifest.get("finding_ids") != finding_ids:
        raise SystemExit("finding projection manifest has stale or reordered finding IDs")

    expected_text = {
        "report/LEDGER.md": rendered["ledger"], "report/APPENDIX_A.md": rendered["appendix"],
        "work/claim_tags.generated.md": rendered["claim_tags"], "claims_map.tsv": rendered["claims_map"],
        "work/verification_plan.json": rendered["verification_plan"],
        "work/comment_findings.generated.json": rendered["comment_findings"],
        "work/settled_candidates.json": rendered["settled_candidates"],
    }
    for relative, expected in expected_text.items():
        path=safe_target(root,relative)
        if not path.is_file() or path.read_text() != expected:
            raise SystemExit(f"finding projection is stale or divergent: {relative}")
    comment = strict_json_text((root/"comment.json").read_text(), str(root/"comment.json"))
    if comment.get("findings") != [comment_projection(row) for row in final]:
        raise SystemExit("comment.json findings diverge from the canonical registry")
    report_path=root/"report/REPORT_final.md"
    if not report_path.is_file() or report_path.read_text() != inject_report(
            report_path.read_text(), rendered["appendix"], rendered["claim_tags"]):
        raise SystemExit("report claim tags or Appendix A diverge from the canonical registry")
    snippet_dir=root/"verification_snippets"
    actual_snippets={path.name for path in snippet_dir.glob("C-*") if path.is_file()}
    if actual_snippets != set(snippets):
        raise SystemExit("verification snippet quorum diverges from the canonical registry")
    for name, expected in snippets.items():
        path=safe_target(root,f"verification_snippets/{name}")
        if path.read_text() != expected:
            raise SystemExit(
                f"verification snippet is stale: verification_snippets/{name} differs from what the projection "
                "writes. That file is regenerated: edit the source named by the row's verification_snippet.path, "
                "then run generate_finding_artifacts.py --apply (and rebuild the notebook).")

    claims=[path for path in root.glob("claims_script.*") if path.is_file()]
    if len(claims) != 1: raise SystemExit(f"expected exactly one generated claims script, found {len(claims)}")
    claim_numbers=parse_claim_numbers(claims[0].read_text(errors="replace"))
    expected_numbers=[number(r) for r in final]   # the approved findings' own numbers (a deferred approval leaves a hole)
    check_claims_join(claim_numbers, expected_numbers)
    notebook=root/"verification.Rmd"
    if not notebook.is_file(): raise SystemExit("generated verification.Rmd is missing")
    notebook_text=notebook.read_text()
    notebook_numbers=[int(value) for value in re.findall(r"(?m)^```\{r\s+C_([1-9][0-9]*)\b", notebook_text)]
    if notebook_numbers != expected_numbers:
        raise SystemExit(f"verification notebook chunks do not join findings exactly: {notebook_numbers}")
    headers=[int(value) for value in re.findall(r"(?m)^##\s+C-([1-9][0-9]*)\s+—", notebook_text)]
    if headers != expected_numbers:
        raise SystemExit(f"verification notebook sections do not exactly join findings: {headers}")
    blocks=re.split(r"(?m)(?=^##\s+C-[1-9][0-9]*\s+—)", notebook_text)
    claim_blocks={}
    for block in blocks:
        match=re.match(r"^##\s+C-([1-9][0-9]*)\s+—",block)
        if match: claim_blocks[int(match.group(1))]=block
    for name, expected in snippets.items():
        claim_number=int(re.match(r"C-([1-9][0-9]*)",name).group(1))
        path=safe_target(root,f"verification_snippets/{name}")
        marker=f"<!-- I4R-SNIPPET-SHA256: {file_sha256(path)} -->"
        block=claim_blocks.get(claim_number,"")
        if marker not in block:
            raise SystemExit(f"verification notebook does not bind canonical snippet: {name}")
        if name.endswith(".R") and expected.rstrip("\n") not in block:
            raise SystemExit(f"verification notebook inlines the wrong code snippet: {name}")

    # A hash manifest can be maliciously re-keyed after a notebook edit. Rebuild
    # the notebook from the current canonical report, plan, and snippets and
    # require byte-for-byte identity before trusting the manifest below.
    fd, regenerated_name=tempfile.mkstemp(prefix=".verification-regenerated.",suffix=".Rmd",dir=root/"work")
    os.close(fd); regenerated=pathlib.Path(regenerated_name)
    try:
        result=subprocess.run(["bash",str(pathlib.Path(__file__).resolve().parent/"make_verification_notebook.sh"),
                               str(root),"-o",str(regenerated)],text=True,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if result.returncode or regenerated.read_bytes() != notebook.read_bytes():
            raise SystemExit("verification notebook is not the exact deterministic projection of the canonical plan")
    finally:
        regenerated.unlink(missing_ok=True)

    artifact_rows=manifest.get("artifacts") or []
    if not isinstance(artifact_rows,list): raise SystemExit("projection manifest artifacts must be an array")
    artifact_map={}
    for item in artifact_rows:
        if not isinstance(item,dict) or not isinstance(item.get("path"),str) or item["path"] in artifact_map:
            raise SystemExit("projection manifest has invalid or duplicate artifact rows")
        artifact_map[item["path"]]=item.get("sha256")
    required=(set(expected_text) | {"report/REPORT_final.md","verification.Rmd",
                                    str(claims[0].relative_to(root))} |
              {f"verification_snippets/{name}" for name in snippets})
    if set(artifact_map) != required:
        raise SystemExit("projection manifest artifact quorum is not exact")
    for relative, expected_hash in artifact_map.items():
        path=safe_target(root,relative)
        if not path.is_file() or file_sha256(path) != expected_hash:
            raise SystemExit(
                f"projection manifest hash is stale: {relative}\n"
                "  This file changed after the projection was written. Re-run it, and note the\n"
                "  order the rest of the chain needs: approve_settled.py (binds the CURRENT report\n"
                "  hash) -> --apply -> approve_settled.py -> --apply (approve, project, approve, project)\n"
                "  -> make_settled.sh -> re-log the settled event -> stage.\n"
                "      python3 scripts/generate_finding_artifacts.py \"$AUDIT_DIR\" --apply")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_dir", type=pathlib.Path)
    parser.add_argument("--registry", type=pathlib.Path)
    parser.add_argument("--schema", type=pathlib.Path)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="install projections into production paths")
    mode.add_argument("--check", action="store_true", help="fail unless installed projections exactly match the registry")
    args = parser.parse_args()
    root = args.audit_dir.resolve()
    here = pathlib.Path(__file__).resolve().parent.parent
    registry = args.registry or root / "work/findings.jsonl"
    schema = args.schema or here / "schemas/v2/finding-row.schema.json"
    if args.apply and (root / ".finding-generation-commit.json").exists():
        restore_generation(root, root / ".finding-generation-commit.json")
        print("recovered an interrupted finding-projection transaction", file=sys.stderr)
    try:
        registry.resolve().relative_to(root)
    except ValueError as exc:
        raise SystemExit("findings registry must be inside the audit directory") from exc
    rows = load_rows(registry); validate_rows(rows, schema)
    final = ensure_final_invariants(rows, root); rendered = render(rows, final)
    # every INCLUDED finding keeps its marker in the report (the coordinator contract), and no other marker
    # appears; an ordinary edit that dropped one otherwise surfaced only at the next renumber
    report_path = root / "report/REPORT_final.md"
    included_ids = [r.get("id") for r in rows if (r.get("ledger_disposition") or {}).get("status") == "INCLUDED"]
    if (args.apply or args.check) and report_path.is_file() and included_ids \
            and all(re.fullmatch(r"F[1-9][0-9]*", str(i)) for i in included_ids):
        markers = re.findall(r"<!--\s*finding:(\S+?)\s*-->", report_path.read_text(encoding="utf-8"))
        # only a report that uses markers (older audits and some fixtures carry none)
        if markers and (sorted(set(markers)) != sorted(set(included_ids)) or len(markers) != len(set(markers))):
            missing = sorted(set(included_ids) - set(markers), key=lambda i: int(i[1:]))
            extra = sorted(set(markers) - set(included_ids))
            repeated = sorted({m for m in markers if markers.count(m) > 1})
            raise SystemExit(
                f"report/REPORT_final.md finding markers do not match the INCLUDED findings: missing {missing}, "
                f"not INCLUDED {extra}, repeated {repeated}. Restore each missing <!-- finding:ID --> marker at its "
                "claim (a withdrawn finding's marker is removed and the ids renumbered, INSTALL.md 'Renumbering')")
    snippets = prepare_snippets(final, root)
    destination = root if args.apply or args.check else root / "work/generated"
    paths = {
        "ledger": destination / ("report/LEDGER.md" if args.apply else "LEDGER.md"),
        "appendix": destination / ("report/APPENDIX_A.md" if args.apply else "APPENDIX_A.md"),
        "claims_map": destination / "claims_map.tsv",
        "verification_plan": destination / ("work/verification_plan.json" if args.apply else "verification_plan.json"),
        "comment_findings": destination / ("work/comment_findings.generated.json" if args.apply else "comment_findings.json"),
        "claim_tags": destination / ("work/claim_tags.generated.md" if args.apply else "claim_tags.md"),
        "settled_candidates": destination / ("work/settled_candidates.json" if args.apply else "settled_candidates.json"),
    }
    if args.check:
        check_projection(root, registry, rendered, final, snippets)
        print(f"finding projections are current and exactly joined ({len(final)} findings)")
        return 0
    if args.apply:
        apply_transaction(root, registry, rendered, final, snippets, here)
    else:
        for key, path in paths.items(): atomic_text(path, rendered[key])
    print(f"generated {len(final)} findings into {destination}")
    for path in paths.values(): print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
