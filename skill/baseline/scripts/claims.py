#!/usr/bin/env python3
"""The baseline's claims script, built and checked the way the full pipeline builds and checks its own.

Every finding F<n> has one verification snippet in verification_snippets/, as the pipeline's projection writes them
(i4r-audit reference/verification_guide.md, section 5): C-<n>.R for a check that runs, C-<n>.judgement (first line the
assessment, the rest its basis) or C-<n>.unverifiable (first line the reason, the rest context) otherwise, plus an
optional _prelude.R of shared loaders and helpers. claims_map.tsv is derived from the snippet kinds exactly as
generate_finding_artifacts.render derives it (a code snippet maps to snippet:verification_snippets/C-<n>.R, anything
else to none), each finding is staged with the verification_script its comment_projection gives it, and
claims_script.R is built by the pipeline's own make_claims_script.sh (a copy beside this file; this public copy rewords one comment). The
pipeline releases only when every check passes, so the script is run here, standalone, from an empty working
directory with the deposit at I4R_PKG_DIR, and every FAIL is reported. What reaches a record is counts, claim ids and
key names: never a printed value or R's own output, which go to the agent's console only. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

SNIPPETS = "verification_snippets"
KINDS = {".R": "code", ".judgement": "judgement", ".unverifiable": "unverifiable"}
SUFFIX = {kind: suffix for suffix, kind in KINDS.items()}
SNIPPET_NAME = re.compile(r"C-([1-9][0-9]*)(\.R|\.judgement|\.unverifiable)|_prelude\.R")
FINDING_ID = re.compile(r"F([1-9][0-9]*)")
BUILDER = pathlib.Path(__file__).with_name("make_claims_script.sh")
BUILD_TIMEOUT = 60
RUN_TIMEOUT = 540   # under the agent's Bash tool maximum (600 s): the agent sees every result the runner sees
# run_snippets.sh literal_ids: comments dropped, strings kept (the ids ARE strings)
_STRIP = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|(#[^\n]*)')
_CALL = re.compile(r"\b(expect|check|judgement|unverifiable)\s*\(\s*(?:id\s*=\s*)?([\"'])(C-[^\"']*)\2")
# the harness prints "[%-6s %-34s] ...": a key may hold spaces, so it ends at the padding before "]"; a record is
# found anywhere in its line, since a cat() without a final newline just before it puts it mid-line
_OBSERVED = re.compile(r"\[(C-\d+)\s+(.+?)\s*\]\s+OBSERVED\s+.*\s(PASS|FAIL)\s*$")
_EXPECTED = re.compile(r"\[(C-\d+)\s+(.+?)\s*\]\s+EXPECTED(?:\s|$)")
_UNDECLARED = re.compile(r"\[(C-\d+)\s+(.+?)\s*\]\s+FAIL\s+undeclared\s*$")
_SUMMARY = re.compile(r"^(\d+) checks, (\d+) PASS, (\d+) FAIL\s*$")
# the runner (not the agent's call, which its own sandbox confines) runs agent-written R under this macOS profile
SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write* (subpath "{tmp}") (subpath "/dev"))
{deny_read}"""
R_ENV = ("PATH", "HOME", "LANG", "LANGUAGE", "R_LIBS", "R_LIBS_USER", "R_LIBS_SITE", "R_HOME")


def call_ids(text: str) -> list[tuple[str, str]]:
    """(function, claim id) for every expect/check/judgement/unverifiable call with a literal id."""
    code = _STRIP.sub(lambda m: "" if m.group(1) else m.group(0), text)
    return [(m.group(1), m.group(3)) for m in _CALL.finditer(code)]


def snippets(out: pathlib.Path, findings: list[dict], errors: list[str]) -> dict[str, dict]:
    """One snippet per finding, of a known kind, recording only under its own claim id. Returns {finding id: {kind,
    file}} for the findings whose snippet is usable; every problem is appended to errors."""
    numbers = {}
    for f in findings:
        m = FINDING_ID.fullmatch(str(f.get("id") or "")) if isinstance(f, dict) else None
        if m:
            numbers[int(m.group(1))] = f["id"]
        else:
            fid = f.get("id") if isinstance(f, dict) else f
            errors.append(f"finding id {fid!r} is not F<n>; the claims script is built only when the ids are F1 to Fn")
    folder = out / SNIPPETS
    if folder.is_symlink() or not folder.is_dir():
        errors.append(f"{SNIPPETS}/ is missing: write one verification snippet per finding "
                      f"(C-<n>.R, C-<n>.judgement or C-<n>.unverifiable; reference/verification_guide.md)")
        return {}
    found: dict[int, list[pathlib.Path]] = {}
    for path in sorted(folder.iterdir()):
        rel = f"{SNIPPETS}/{path.name}"
        if path.name in (".DS_Store",):
            continue
        if path.is_symlink() or not path.is_file():
            errors.append(f"{rel}: not a regular file")
            continue
        m = SNIPPET_NAME.fullmatch(path.name)
        if not m:
            errors.append(f"{rel}: not a snippet name (C-<n>.R, C-<n>.judgement, C-<n>.unverifiable or _prelude.R); "
                          f"it will not be kept")
            continue
        if path.name == "_prelude.R":
            ids = call_ids(path.read_text(errors="replace"))
            if ids:
                errors.append(f"{rel}: records an outcome ({ids[0][0]}() under {ids[0][1]}); the prelude holds loaders "
                              f"and helpers only, and each snippet calls expect() and check() itself")
            continue
        n = int(m.group(1))
        if n not in numbers:
            errors.append(f"{rel}: there is no finding F{n}")
            continue
        found.setdefault(n, []).append(path)
    usable: dict[str, dict] = {}
    for n, fid in sorted(numbers.items()):
        files = found.get(n, [])
        if not files:
            errors.append(f"{fid} has no verification snippet ({SNIPPETS}/C-{n}.R, .judgement or .unverifiable)")
            continue
        if len(files) > 1:
            errors.append(f"{fid} has {len(files)} snippets ({', '.join(p.name for p in files)}); keep exactly one")
            continue
        path, kind = files[0], KINDS[files[0].suffix]
        text = path.read_text(errors="replace")
        cid = f"C-{n}"
        if kind == "code":
            ids = call_ids(text)
            others = sorted({i for _, i in ids if i != cid})
            if others:
                errors.append(f"{SNIPPETS}/{path.name}: records under {', '.join(others)}; a claim's chunk records only "
                              f"under its own id {cid}")
                continue
            calls = {fn for fn, i in ids if i == cid}
            if not {"expect", "check"} <= calls:
                errors.append(f"{SNIPPETS}/{path.name}: must call both expect(\"{cid}\", ...) and check(\"{cid}\", ...) "
                              f"itself, with the id written out (a helper that wraps them does not count)")
                continue
        else:
            first = text.strip().splitlines()[0].strip() if text.strip() else ""
            if not first:
                what = "the assessment" if kind == "judgement" else "the reason"
                errors.append(f"{SNIPPETS}/{path.name}: the first line must state {what}")
                continue
        usable[fid] = {"kind": kind, "file": f"{SNIPPETS}/{path.name}"}
    return usable


def claims_map(findings: list[dict], kinds: dict[str, dict]) -> str:
    """generate_finding_artifacts.render: a code snippet maps to its file, anything else to none."""
    rows = []
    for f in findings:
        n = FINDING_ID.fullmatch(f["id"]).group(1)
        code = (kinds.get(f["id"]) or {}).get("kind") == "code"
        rows.append(f"{f['id']}\t" + (f"snippet:{SNIPPETS}/C-{n}.R" if code else "none"))
    return "\n".join(rows) + "\n"


def staged_findings(findings: list[dict], kinds: dict[str, dict]) -> list[dict]:
    """Each finding with the verification_script its pipeline comment_projection carries."""
    rows = []
    for f in findings:
        n = FINDING_ID.fullmatch(f["id"]).group(1)
        rows.append({**f, "verification_script": f"{SNIPPETS}/C-{n}{SUFFIX[kinds[f['id']]['kind']]}"})
    return rows


def deposit_digest(pkg: pathlib.Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in pkg.rglob("*") if p.is_file() and p.name != ".DS_Store"):
        h.update(str(path.relative_to(pkg)).encode() + b"\0")
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                h.update(block)
    return h.hexdigest()


def _sb_string(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sandbox(tmp: pathlib.Path, deny_read: list[str]) -> list[str] | None:
    """sandbox-exec argv for the runner's run of agent-written R, when this host has it and it works: no network,
    writes only in the scratch folder, and none of the paths the agent's own call could not read."""
    exe = shutil.which("sandbox-exec")
    if not exe or sys.platform != "darwin":
        return None
    profile = tmp / "claims.sb"
    denials = "".join(f"(deny file-read* (subpath {_sb_string(p)}))\n" for p in deny_read)
    profile.write_text(SANDBOX_PROFILE.format(tmp=str(tmp.resolve()), deny_read=denials))
    try:
        probe = subprocess.run([exe, "-f", str(profile), "/usr/bin/true"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return [exe, "-f", str(profile)] if probe.returncode == 0 else None


def build_and_run(out: pathlib.Path, findings: list[dict], kinds: dict[str, dict], pkg: pathlib.Path | None,
                  keep: pathlib.Path | None = None, *, confine: dict | None = None) -> dict:
    """Build claims_script.R with the pipeline's builder in a scratch folder and run it standalone against the
    deposit. With keep, the built claims_script.R and claims_map.tsv are copied there. With confine (the runner:
    {"deny_read": [paths], "tmp_base": folder}), the scratch folder is made in tmp_base, and the R run gets a minimal
    environment and, on macOS, a sandbox denying network, writes outside the scratch and reads of the denied paths."""
    result: dict = {"built": False, "ran": False, "builder_sha256": None, "checks": None, "pass": None, "fail": None,
                    "claims": {}, "error": None, "seconds": None, "deposit_unchanged": None, "runner_sandbox": None,
                    "fail_lines": [], "error_tail": None, "script_text": None}
    if not BUILDER.is_file():
        result["error"] = f"the claims builder is missing ({BUILDER.name})"
        return result
    result["builder_sha256"] = hashlib.sha256(BUILDER.read_bytes()).hexdigest()
    tmp_base = (confine or {}).get("tmp_base")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="baseline-claims-", dir=tmp_base)).resolve()
    try:
        return _build_and_run(out, findings, kinds, pkg, keep, confine, tmp, result)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _build_and_run(out: pathlib.Path, findings: list[dict], kinds: dict[str, dict], pkg: pathlib.Path | None,
                   keep: pathlib.Path | None, confine: dict | None, tmp: pathlib.Path, result: dict) -> dict:
    stage = tmp / "stage"
    (stage / SNIPPETS).mkdir(parents=True)
    (stage / "comment.json").write_text(json.dumps({"findings": staged_findings(findings, kinds)}, indent=1))
    (stage / "claims_map.tsv").write_text(claims_map(findings, kinds))
    for name in ["_prelude.R", *(pathlib.Path(k["file"]).name for k in kinds.values())]:
        if (out / SNIPPETS / name).is_file():
            shutil.copy2(out / SNIPPETS / name, stage / SNIPPETS / name)
    try:
        built = subprocess.run(["bash", str(BUILDER), str(stage)], capture_output=True, text=True,
                               timeout=BUILD_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["error"] = f"the claims builder did not finish ({type(exc).__name__})"
        return result
    script = stage / "claims_script.R"
    if built.returncode != 0 or not script.is_file():
        fatal = [l for l in (built.stdout + built.stderr).splitlines() if l.startswith("FATAL")]
        result["error"] = "the claims builder refused: " + (fatal[0][:200] if fatal else f"exit {built.returncode}")
        return result
    result["built"] = True
    result["script_text"] = script.read_text(errors="replace")
    if keep is not None:
        keep.mkdir(parents=True, exist_ok=True)
        shutil.copy2(script, keep / "claims_script.R")
        shutil.copy2(stage / "claims_map.tsv", keep / "claims_map.tsv")
    for fid, k in kinds.items():
        if k["kind"] != "code":
            result["claims"][fid] = {"kind": k["kind"], "status": k["kind"].upper()}
    if pkg is None or not pathlib.Path(pkg).is_dir():
        result["error"] = "no deposit to run the claims script against"
        return result
    if not shutil.which("Rscript"):
        result["error"] = "Rscript is not on PATH; the claims script cannot run"
        return result
    pkg = pathlib.Path(pkg).resolve()
    cwd, rtmp = tmp / "cwd", tmp / "rtmp"
    cwd.mkdir()
    rtmp.mkdir()
    if confine is not None:
        env = {k: v for k, v in os.environ.items() if k in R_ENV or k.startswith("LC_")}
    else:
        env = {k: v for k, v in os.environ.items() if k not in ("I4R_AUDIT_DIR", "I4R_SNIPS_DIR", "R_PROFILE_USER",
                                                               "R_ENVIRON_USER")}
    env.update(I4R_PKG_DIR=str(pkg), TMPDIR=str(rtmp))
    wrapper = _sandbox(tmp, [str(p) for p in confine.get("deny_read") or []]) if confine is not None else None
    result["runner_sandbox"] = ("sandbox-exec: network denied, writes only in the scratch folder, the call's denied "
                                "paths unreadable" if wrapper else "none" if confine is not None else None)
    before, t0 = deposit_digest(pkg), time.monotonic()
    try:
        done = subprocess.run([*(wrapper or []), "Rscript", "--vanilla", str(script)], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=RUN_TIMEOUT, errors="replace")
        log, code = done.stdout + "\n" + done.stderr, done.returncode
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or b""
        log, code = raw.decode(errors="replace") if isinstance(raw, bytes) else raw, None
    result["seconds"] = round(time.monotonic() - t0, 1)
    result["deposit_unchanged"] = deposit_digest(pkg) == before
    lines = log.splitlines()
    summary = [m for m in (_SUMMARY.match(l) for l in lines) if m]
    expected: dict[str, set] = {}
    observed: dict[str, dict] = {}
    last_id = None
    for line in lines:
        if m := _EXPECTED.search(line):
            expected.setdefault(m.group(1), set()).add(m.group(2))
            last_id = m.group(1)
        elif m := _OBSERVED.search(line):
            observed.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
            last_id = m.group(1)
            if m.group(3) == "FAIL":
                result["fail_lines"].append(line)
        elif m := _UNDECLARED.search(line):
            observed.setdefault(m.group(1), {})[m.group(2)] = "FAIL"
            result["fail_lines"].append(line)
    if code is None:
        result["error"] = (f"the claims script ran past {RUN_TIMEOUT} s and was stopped"
                           + (f" (the last claim it reached was {last_id})" if last_id else ""))
    elif code != 0 or not summary:
        result["error"] = (f"the claims script stopped with an error (Rscript exit {code})"
                           + (f"; the last claim it reached was {last_id}" if last_id else " before any claim"))
    if result["error"]:
        result["error_tail"] = "\n".join(l for l in lines if l.strip())[-1500:]   # the agent's console only
    if summary:
        result["ran"] = code == 0
        result["checks"], result["pass"], result["fail"] = (int(x) for x in summary[-1].groups())
    for fid, k in kinds.items():
        if k["kind"] != "code":
            continue
        cid = "C-" + FINDING_ID.fullmatch(fid).group(1)
        got, declared = observed.get(cid, {}), expected.get(cid, set())
        unchecked = sorted(declared - set(got))
        failing = sorted(key for key, v in got.items() if v != "PASS") + unchecked
        status = ("NO OUTPUT" if not got and not declared else "FAIL" if failing else "PASS")
        result["claims"][fid] = {"kind": "code", "status": status, "checks": len(set(got) | declared),
                                 "failing": failing, "unchecked": unchecked}
    return result


def errors_from(result: dict) -> list[str]:
    """The validation errors a claims-script result implies (claim ids and key names only: values stay out)."""
    errs = []
    if result.get("error"):
        errs.append(result["error"])
    if result.get("deposit_unchanged") is False:
        errs.append("the claims script changed the deposit; it must write only under tempdir()")
    claims = result.get("claims", {})
    any_failing = False
    parsed_fails = sum(len(c.get("failing") or []) for c in claims.values() if c.get("kind") == "code")
    misread = (result.get("fail") is not None and not result.get("error") and parsed_fails > result["fail"])
    for fid, c in sorted(claims.items(), key=lambda kv: int(kv[0][1:])):
        if c.get("kind") != "code":
            continue
        cid = "C-" + fid[1:]
        if c["status"] == "NO OUTPUT" and not result.get("error"):
            errs.append(f"{cid} ({fid}): the check recorded nothing")
        elif c["status"] == "FAIL" and result.get("error") and set(c["failing"]) <= set(c.get("unchecked") or []):
            any_failing = True
            errs.append(f"{cid} ({fid}): not reached before the claims script stopped "
                        f"({', '.join(c['failing'])} declared, never checked)")
        elif c["status"] == "FAIL" and misread and set(c["failing"]) <= set(c.get("unchecked") or []):
            any_failing = True
            errs.append(f"{cid} ({fid}): R reports no FAIL here, but the observed line of {', '.join(c['failing'])} "
                        f"could not be read; print each checked value on one line, end any cat() output with a "
                        f"newline, and do not capture check()'s output")
        elif c["status"] == "FAIL":
            any_failing = True
            parts = []
            if c.get("unchecked"):
                parts.append(f"declared but no observation was read: {', '.join(c['unchecked'])}")
            rest = [k for k in c["failing"] if k not in c.get("unchecked", [])]
            if rest:
                parts.append(f"did not reproduce: {', '.join(rest)}")
            errs.append(f"{cid} ({fid}) FAIL, {'; '.join(parts)}. Work through verification_guide.md section 4: if the "
                        f"check is wrong, correct it (a tolerance only for a stated reason, given in the note); if it is "
                        f"right, the comment's number is wrong: correct the comment, and the expected value with it")
    if result.get("checks") is not None and not result.get("error"):
        attributed = sum(c.get("checks", 0) for c in claims.values() if c.get("kind") == "code")
        if result["checks"] != attributed:
            errs.append(f"the claims script reports {result['checks']} checks but {attributed} could be attributed to "
                        f"a claim: give every value a plain name, print nothing over several lines in a check, and "
                        f"record each check under its own claim's literal id")
        if result.get("fail") and not any_failing:
            errs.append(f"the claims script reports {result['fail']} FAIL that no claim's line shows; run it and read "
                        f"its output")
    return errs


def summary(result: dict) -> dict:
    """What run.json keeps: counts and per-claim statuses; no output line, no printed value."""
    kinds = [c.get("kind") for c in result.get("claims", {}).values()]
    return {k: result.get(k) for k in ("built", "ran", "checks", "pass", "fail", "seconds", "deposit_unchanged",
                                        "builder_sha256", "runner_sandbox", "error")} | {
        "code_claims": kinds.count("code"), "judgement_claims": kinds.count("judgement"),
        "unverifiable_claims": kinds.count("unverifiable"),
        "claims": {fid: {k: v for k, v in c.items() if k in ("kind", "status", "checks", "failing")}
                   for fid, c in result.get("claims", {}).items()}}
