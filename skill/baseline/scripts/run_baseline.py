#!/usr/bin/env python3
"""run_baseline.py {preflight|probe|run|batch|status}: the single-agent, single-pass baseline.

  preflight            check the CLI, the work folder, the sandbox paths, every subset paper's inputs and the
                       readable disk for copies of the full pipeline's comments; no model call
  probe                one cheap call (budget USD 1) proving the sandbox lets the agent read its inputs and its own
                       saved tool results, run Python and R, and refuses a denied path to its file tools and shell,
                       and that the requests carry the full runs' output cap and cache lifetime
  render <audit-id|k>  render a run's comment again (after a renderer fix); no model call
  run <audit-id|k>     audit one subset paper (k = its 1-based position in SUBSET.json) in ONE Claude call
  batch                run every subset paper that has no run yet, one at a time
  status               one line per subset paper

Each run copies the exact Phase-1 snapshot the full pipeline's auditors saw (paper, deposit, deterministic indexes,
verified against its manifest) into a private work folder, gives one Claude call the task-definition files, and lets
it audit and write the whole comment in that one session: no critics, no coordinator, no revision round. The call is
launched and accounted for as an i4r-audit 2.7.7 Phase-1 auditor is, and confined more tightly (its file tools reach
only its own folders; its shell cannot read the tree, any cloud folder, the user's documents or the session data).
The comment, its findings record, its evidence and the run record land in baseline_comments/<journal>/<paper>/;
every run, finished or not, is also appended to baseline_comments/RUNLOG.jsonl.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import pathlib
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import baseline_common as bc  # noqa: E402
import sources  # noqa: E402
import validate_outputs  # noqa: E402

PROMPT = bc.SKILL / "prompts" / "baseline_prompt.md"
REFERENCE = bc.SKILL / "reference"
OUTPUT_FILES = ("front_matter.md", "REPORT_final.md", "captions.txt", "findings.json")
SYSTEMIC = {"authentication", "authorization", "quota", "model", "condition_mismatch"}   # the next paper would fail too
RETRYABLE = {"transient", "transport"}
RERUNNABLE = {"failed", "timeout", "interrupted", "abandoned"}
# The CLI's account limits ("You've hit your session limit", weekly, Sonnet, Opus, usage credit, monthly spend).
LIMIT = re.compile(r"you'?ve hit your|(?:session|usage|weekly|spend|sonnet|opus|fable|credit) limit", re.I)


class Stop(KeyboardInterrupt):
    """SIGTERM or SIGHUP: stop like Ctrl-C, so the paid call is killed and recorded and the lock is released."""


_STOPPING = False


def _on_signal(signum, _frame):
    global _STOPPING
    if _STOPPING:   # a second stop while the first is being recorded is ignored
        return
    _STOPPING = True
    raise Stop(f"signal {signum}")


class _Uninterruptible:
    """Recording a run (thawing, run.json, RUNLOG) must finish even if the operator presses Ctrl-C again."""

    SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def __enter__(self):
        self.saved = {s: signal.getsignal(s) for s in self.SIGNALS}
        for s in self.SIGNALS:
            signal.signal(s, signal.SIG_IGN)

    def __exit__(self, *exc):
        for s, handler in self.saved.items():
            signal.signal(s, handler)
        return False


# --- the subset and its folders -------------------------------------------------------------------------------------
def out_root(tree: pathlib.Path) -> pathlib.Path:
    return tree / bc.CONFIG["output_dir"]


def load_subset(tree: pathlib.Path) -> dict:
    path = out_root(tree) / "SUBSET.json"
    if not path.is_file():
        raise bc.BaselineError(f"no {path.relative_to(tree)}; run select_subset.py --write first")
    return json.loads(path.read_text())


def pick(subset: dict, key: str) -> dict:
    papers = subset["papers"]
    if key.isdigit() and 1 <= int(key) <= len(papers):
        return papers[int(key) - 1]
    for paper in papers:
        if paper["audit_id"] == key:
            return paper
    raise bc.BaselineError(f"{key} is neither an audit id in SUBSET.json nor a position 1..{len(papers)}")


def final_dir(tree: pathlib.Path, paper: dict) -> pathlib.Path:
    return out_root(tree) / paper["journal"] / bc.safe_name(paper["runs_folder"])


def run_record(folder: pathlib.Path) -> dict | None:
    try:
        return json.loads((folder / "run.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


def status_of(record: dict | None) -> str | None:
    return ((record or {}).get("outcome") or {}).get("status")


# --- the lock: one paid call at a time ------------------------------------------------------------------------------
def acquire_lock(tree: pathlib.Path) -> tuple[pathlib.Path, str]:
    lock = out_root(tree) / ".batch.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    mine = f"pid {os.getpid()} on {socket.gethostname()} since {bc.utc_now()} id {uuid.uuid4().hex[:8]}\n"
    for _ in range(3):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                holder = lock.read_text(errors="replace")
            except FileNotFoundError:
                continue
            match = re.match(r"pid (\d+) on (\S+)", holder)
            if not (match and match.group(2) == socket.gethostname() and not _alive(int(match.group(1)))):
                raise bc.BaselineError(f"another run holds {lock.name} ({holder.strip()}); paid runs go one at a "
                                       f"time. Stop it with kill -INT <pid>, or remove the file only if that "
                                       f"process is gone")
            aside = lock.with_name(f".batch.lock.stale-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}")
            try:
                os.rename(lock, aside)   # atomic: of two runners taking over, one moves it and the other gets ENOENT
            except FileNotFoundError:
                continue
            if aside.read_text(errors="replace") != holder:   # we moved a lock someone else had just taken: put it back
                try:
                    os.link(aside, lock)
                    aside.unlink()
                except FileExistsError:
                    pass
                raise bc.BaselineError(f"another run took {lock.name} at the same moment; try again")
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(mine)
        return lock, mine
    raise bc.BaselineError(f"could not take {lock}")


def release_lock(lock: pathlib.Path, mine: str) -> None:
    try:
        if lock.read_text(errors="replace") == mine:
            lock.unlink()
    except FileNotFoundError:
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


# --- confinement ----------------------------------------------------------------------------------------------------
def claude_tmp_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("I4R_CLAUDE_TMP_ROOT") or f"/private/tmp/claude-{os.getuid()}")


def protected_roots(tree: pathlib.Path) -> list[pathlib.Path]:
    """Where a published comment, an audit's own records or session material can be: none of it is readable to the
    call's shell (the file tools are allowlisted to the run's own folders anyway). Session folders are denied as they
    exist at launch: the call's own (its saved tool results, its temporary files) is created after launch."""
    home = pathlib.Path.home()
    cloud = home / "Library" / "CloudStorage"
    roots = [cloud if cloud.is_dir() and tree.is_relative_to(cloud) else tree]
    roots += [home / name for name in ("Downloads", "Desktop", "Documents", ".i4r_scratch", ".codex")]
    roots += [home / "Library" / name for name in ("Mail", "Messages", "Mobile Documents")]
    roots += [home / "Library" / "Application Support" / "Claude"]
    roots += [home / ".claude" / name for name in ("file-history", "debug", "todos", "plans", "sessions", "session-env",
                                                   "backups", "paste-cache", "tasks", "teams", "history.jsonl")]
    for parent in (home / ".claude" / "projects", claude_tmp_root()):
        if parent.is_dir():
            roots += sorted(p for p in parent.iterdir() if p.is_dir())
    roots += [pathlib.Path(p).expanduser() for p in bc.CONFIG.get("extra_deny_read", [])]
    return [r for r in roots if r.exists()]


def check_work_root(tree: pathlib.Path, work: pathlib.Path) -> None:
    """The work folder holds a copy of the deposit and is the agent's only readable world: never inside a protected
    root (it would be unreadable, or the root readable), Claude's or Codex's own folders, or a cloud-synced folder."""
    home = pathlib.Path.home()
    static = [tree, home / "Library" / "CloudStorage", home / "Library" / "Mobile Documents", home / ".claude",
              home / ".codex", home / "Downloads", home / "Desktop", home / "Documents", home / ".i4r_scratch",
              claude_tmp_root()]
    for root in [*static, *protected_roots(tree)]:
        if work.is_relative_to(root):
            raise bc.BaselineError(f"the work folder {work} is inside {root}; set I4R_BASELINE_WORK to a local "
                                   f"folder outside the tree, cloud folders, Documents/Downloads/Desktop and "
                                   f"Claude's session folders")


def deny_read_paths(tree: pathlib.Path, run_root: pathlib.Path) -> list[pathlib.Path]:
    deny = protected_roots(tree)
    for root in deny:
        if run_root.is_relative_to(root):
            raise bc.BaselineError(f"the run folder {run_root} is inside the protected {root}")
    work = run_root.parent.parent
    deny += [other for other in sorted(work.glob("*/*")) if other.is_dir() and other != run_root]
    return deny


def contamination_scan(tree: pathlib.Path, papers: list[dict]) -> list[str]:
    return contamination(tree, papers)["hits"]


def contamination(tree: pathlib.Path, papers: list[dict]) -> dict:
    """Readable copies of the full pipeline's comment files for subset papers, outside the denied roots, and what the
    search covered. Everything the call's shell can read is searched, ~/Library and toolchain folders included."""
    t0 = time.monotonic()
    targets: dict[int, dict[str, str]] = {}
    for paper in papers:
        for folder in (tree / "comments" / paper["journal"] / paper["metadata_folder"],
                       tree / "metadata" / paper["journal"] / paper["metadata_folder"]):
            for path in folder.glob("*") if folder.is_dir() else []:
                if path.is_file() and path.suffix.lower() in {".pdf", ".docx", ".md", ".json", ".r"}:
                    targets.setdefault(path.stat().st_size, {})[bc.sha256_file(path)] = str(path.relative_to(tree))
    home = pathlib.Path.home()
    record = {"hits": [], "targets": sum(len(v) for v in targets.values()), "roots": [], "denied_roots_skipped": 0,
              "files_seen": 0, "seconds": 0.0}
    if not targets:
        return record
    skip = set(protected_roots(tree)) | {bc.work_root()}
    record["denied_roots_skipped"] = len(skip)
    if os.environ.get("I4R_SCAN_ROOTS"):   # tests point the scan at their own folders
        tops = [pathlib.Path(p) for p in os.environ["I4R_SCAN_ROOTS"].split(os.pathsep) if p]
    else:
        tops = [home, pathlib.Path("/Users/Shared"), pathlib.Path("/private/tmp")]
        if os.environ.get("TMPDIR"):
            tops.append(pathlib.Path(os.environ["TMPDIR"]))
    hits: list[str] = []
    for top_root in dict.fromkeys(p for p in tops if p.is_dir()):
        found, seen = _scan(top_root, targets, skip)
        hits += found
        record["files_seen"] += seen
        record["roots"].append(str(top_root).replace(str(home), "~"))
    record["hits"] = sorted(set(hits))
    record["seconds"] = round(time.monotonic() - t0, 1)
    return record


def _scan(start: pathlib.Path, targets: dict, skip: set) -> tuple[list[str], int]:
    hits, seen = [], 0
    for top, dirs, files in os.walk(start):
        top_p = pathlib.Path(top)
        dirs[:] = [d for d in dirs if top_p / d not in skip and not d.startswith(".git") and d != "node_modules"]
        seen += len(files)
        for name in files:
            path = top_p / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size in targets and not path.is_symlink():
                try:
                    digest = bc.sha256_file(path)
                except OSError:
                    continue
                if digest in targets[size]:
                    hits.append(f"{path} is a copy of {targets[size][digest]}")
    return hits, seen


# --- inputs ---------------------------------------------------------------------------------------------------------
def i4r_scripts(tree: pathlib.Path) -> pathlib.Path | None:
    for path in (tree / "skill" / "i4r-audit-unified" / "i4r-audit", pathlib.Path.home() / ".claude" / "skills" / "i4r-audit"):
        if (path / "scripts" / "provider_safety.py").is_file():
            return path
    return None


def _i4r_call(skill: pathlib.Path, module: str, expression: str, *args: str) -> tuple[object, str | None]:
    code = (f"import json,sys,pathlib; sys.path.insert(0, sys.argv[1]); "
            f"import {module}; print(json.dumps({expression}))")
    try:
        out = subprocess.run([sys.executable, "-B", "-c", code, str(skill / "scripts"), *args], capture_output=True,
                             text=True, timeout=600, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return None, "timed out"
    if out.returncode != 0:
        return None, out.stderr[-800:]
    return json.loads(out.stdout), None


def pipeline_helpers(tree: pathlib.Path, pkg: pathlib.Path) -> dict:
    """The pipeline's own deposit-header exemptions and environment fingerprint, computed on this host by the live
    i4r-audit code, each in its own subprocess so its modules never mix with the baseline's and one failure does not
    take the other with it. Without i4r-audit, or when it fails, nothing is exempt: never looser than the pipeline."""
    skill = i4r_scripts(tree)
    if not skill:
        return {"source": None, "exempt": [], "exempt_source": "none (i4r-audit not found)", "errors": {}}
    version = f"i4r-audit {(skill / 'VERSION').read_text().strip()}"
    exempt, err1 = _i4r_call(skill, "provider_safety",
                             "sorted(provider_safety.deposit_exemptions(pathlib.Path(sys.argv[2])))", str(pkg))
    fingerprint, err2 = _i4r_call(skill, "phase1_runtime", "phase1_runtime.environment_fingerprint()")
    return {"source": version, "exempt": exempt if exempt is not None else [],
            "exempt_source": version if exempt is not None else f"none ({version} failed)",
            "fingerprint": fingerprint, "errors": {k: v for k, v in (("exemptions", err1), ("fingerprint", err2)) if v}}


R_PACKAGES = ("haven", "foreign", "readxl", "psych", "lavaan", "lme4", "car", "emmeans", "metafor", "dplyr",
              "tidyr", "ggplot2", "afex", "effectsize", "MBESS", "mediation")
PY_PACKAGES = ("pandas", "numpy", "scipy", "statsmodels", "pyreadstat", "openpyxl", "matplotlib", "pingouin",
               "scikit-learn", "semopy")


def environment(requested: str) -> dict:
    """What the call could run, as i4r-audit's capture_environment.sh records it for the pipeline. Names only for
    the environment variables: never a value."""
    def version(argv: list[str]) -> str | None:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return None
        text = (done.stdout or done.stderr).strip().splitlines()
        return text[0] if text else None

    def captured(argv: list[str]) -> dict:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=180)
            return json.loads(done.stdout) if done.returncode == 0 else {}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return {}
    r_code = ("ip <- installed.packages()[, 'Version']; p <- c(" + ",".join(f"'{p}'" for p in R_PACKAGES) + "); "
              "v <- ifelse(p %in% names(ip), ip[p], NA); cat(paste0('{', paste0('\"', p, '\": ', "
              "ifelse(is.na(v), 'null', paste0('\"', v, '\"')), collapse=', '), '}'))")
    py_code = ("import json, importlib.metadata as m\nout = {}\nfor p in " + repr(PY_PACKAGES) + ":\n"
               "    try: out[p] = m.version(p)\n    except Exception: out[p] = None\nprint(json.dumps(out))")
    env = os.environ
    auth = ("bedrock" if env.get("CLAUDE_CODE_USE_BEDROCK") else "vertex" if env.get("CLAUDE_CODE_USE_VERTEX")
            else "api_key" if env.get("ANTHROPIC_API_KEY") else "auth_token" if env.get("ANTHROPIC_AUTH_TOKEN")
            else "oauth_token" if env.get("CLAUDE_CODE_OAUTH_TOKEN") else "cli_login")
    soffice = shutil.which("soffice") or ("/Applications/LibreOffice.app/Contents/MacOS/soffice"
                                          if pathlib.Path("/Applications/LibreOffice.app").exists() else None)
    return {
        "os": f"{platform.system()} {platform.release()} {platform.machine()}",
        "os_product": version(["sw_vers", "-productVersion"]) if platform.system() == "Darwin" else None,
        "runner_python": platform.python_version(),
        "python3": version(["python3", "--version"]), "R": version(["R", "--version"]),
        "quarto": version(["quarto", "--version"]), "pdftotext": version(["pdftotext", "-v"]),
        "pandoc": version(["pandoc", "--version"]), "libreoffice": version([soffice, "--version"]) if soffice else None,
        "r_packages": captured(["Rscript", "-e", r_code]) if shutil.which("Rscript") else {},
        "python_packages": captured(["python3", "-c", py_code]),
        "auth": auth, "base_url_set": bool(env.get("ANTHROPIC_BASE_URL")),
        "entrypoint": env.get("CLAUDE_CODE_ENTRYPOINT"),
        "spawn_env_names": sorted(k for k in bc.spawn_env(requested)
                                  if k.startswith(("CLAUDE", "ANTHROPIC_", "MAX_THINKING"))),
        "stripped_env_names": sorted(k for k in (*bc.SESSION_LINK_ENV, *bc.CACHE_OVERRIDE_ENV, *bc.REMAPPING_ENV)
                                     if k in env),
    }


def prepare(tree: pathlib.Path, paper: dict, run_root: pathlib.Path) -> dict:
    audit = sources.source_dir(tree, paper)
    snap, manifest = sources.verified_snapshot(audit, paper["snapshot_sha256"])
    input_dir, packet = run_root / "input", run_root / "packet"
    shutil.copytree(snap / "raw", input_dir / "raw")
    shutil.copytree(snap / "evidence", input_dir / "indexes")
    helpers = pipeline_helpers(tree, input_dir / "raw" / "pkg")
    (run_root / "fingerprint_note").write_text("" if helpers.get("fingerprint") else
                                               " The environment fingerprint index describes the machine the original "
                                               "audit ran on, not yours: check your own tools with Bash.")
    fingerprint = input_dir / "indexes" / "environment_fingerprint.json"
    original_fp = bc.sha256_file(fingerprint) if fingerprint.is_file() else None
    if helpers.get("fingerprint"):   # the auditors' fingerprint described the machine they ran on; so does this one
        fingerprint.write_text(json.dumps(helpers["fingerprint"], indent=2, sort_keys=True) + "\n")
    (packet / "reference").mkdir(parents=True)
    (packet / "tools").mkdir()
    for ref in sorted(REFERENCE.glob("*.md")):
        shutil.copy2(ref, packet / "reference" / ref.name)
    shutil.copy2(bc.SKILL / "scripts" / "validate_outputs.py", packet / "tools" / "validate_outputs.py")
    exempt = helpers["exempt"]
    bc.write_json(packet / "tools" / "validate_config.json", {"pkg": str(input_dir / "raw" / "pkg"), "exempt": exempt})
    bc.freeze_tree(input_dir)
    bc.freeze_tree(packet)
    pkg_rows = [r for r in manifest["source_files"] if r["path"].startswith("pkg/")]
    return {
        "source_audit_id": paper["audit_id"], "source_protocol_version": paper["protocol_version"],
        "snapshot_sha256": paper["snapshot_sha256"],
        "snapshot_files": len(manifest["source_files"]), "package_files": len(pkg_rows),
        "package_bytes": sum(r["bytes"] for r in pkg_rows),
        "indexes": sorted(r["path"] for r in manifest["common_evidence"]["artifacts"]),
        "environment_fingerprint": {"regenerated_on_this_host": bool(helpers.get("fingerprint")),
                                    "snapshot_sha256": original_fp,
                                    "sha256": bc.sha256_file(fingerprint) if fingerprint.is_file() else None},
        "deposit_exemptions": {"count": len(exempt), "source": helpers["exempt_source"]},
        "pipeline_helpers_errors": helpers["errors"],
        "reference_sha256": {p.name: bc.sha256_file(p) for p in sorted(REFERENCE.glob("*.md"))},
        "validator_sha256": bc.sha256_file(bc.SKILL / "scripts" / "validate_outputs.py"),
    }


# --- the call ---------------------------------------------------------------------------------------------------------
def render_prompt(route: dict, effort: str, run_root: pathlib.Path) -> str:
    text = PROMPT.read_text(encoding="utf-8")
    note_file = run_root / "fingerprint_note"
    note = note_file.read_text() if note_file.is_file() else ""
    return (text.replace("{MODEL_DISPLAY}", route["display"]).replace("{EFFORT}", effort)
            .replace("{INPUT}", str(run_root / "input")).replace("{PACKET}", str(run_root / "packet"))
            .replace("{FINGERPRINT_NOTE}", note))


def call(argv: list[str], prompt: str, cwd: pathlib.Path, env: dict, timeout: int) -> tuple[int, str, str, bool]:
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout)
        return process.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return 124, stdout or "", stderr or "", True
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise


def classify(result: dict, problems: list[str], stderr: str, timed_out: bool, models: dict,
             window_mismatch: bool) -> str | None:
    """Structured fields first, then the pipeline's text rules on the provider's own words (never on our labels)."""
    if not problems:
        return None
    if timed_out:
        return "timeout"
    if window_mismatch:   # a wrong condition stops the batch even when it also ran to the budget
        return "condition_mismatch"
    if result and models["observed_ids"] and models["primary_count"] != 1:
        return "model"
    if result.get("subtype") == "error_max_budget_usd" or result.get("terminal_reason") == "budget_exhausted":
        return "budget_exhausted"
    said = " ".join([str(result.get("result") or ""), " ".join(map(str, result.get("errors") or [])), stderr[-2000:]])
    if LIMIT.search(said):
        return "quota"
    if result.get("api_error_status") == 429:
        return "transient"
    return bc.classify_error(said, False)


def attempt(cli: str, tree: pathlib.Path, run_root: pathlib.Path, number: int, requested: str, effort: str,
            budget: str, timeout: int, record: dict) -> dict:
    route = bc.ROUTES[requested]
    adir, out = run_root / f"attempt_{number}", run_root / f"out_{number}"
    adir.mkdir()
    out.mkdir()
    reads = [run_root / "input", run_root / "packet"]
    deny = deny_read_paths(tree, run_root)
    settings = bc.sandbox_settings(reads, out, deny, reads)
    bc.write_json(adir / "claude_settings.json", settings)
    prompt = render_prompt(route, effort, run_root)
    (adir / "prompt.txt").write_text(prompt, encoding="utf-8")
    session = str(uuid.uuid4())
    argv = bc.claude_command(cli, adir / "claude_settings.json", reads, session, route["spawn_alias"], effort, budget,
                             bc.file_tool_rules(reads, out))
    home = str(pathlib.Path.home())
    stub = {"number": number, "session_id": session, "started_at": bc.utc_now(), "status": "running",
            "deny_read": [str(p).replace(home, "~") for p in deny]}
    record.setdefault("attempts", []).append(stub)
    bc.write_json(run_root / "run.json", record)   # a hard kill still leaves a trace of the paid call
    t0 = time.monotonic()
    try:
        rc, stdout, stderr, timed_out = call(argv, prompt, out, bc.spawn_env(requested), timeout)
    except BaseException:
        stub.update(status="interrupted", ended_at=bc.utc_now(), elapsed_seconds=round(time.monotonic() - t0, 1),
                    inference_started=True)
        raise
    elapsed = time.monotonic() - t0
    (adir / "claude_stdout.json").write_text(stdout, encoding="utf-8")
    (adir / "claude_stderr.txt").write_text(stderr, encoding="utf-8")
    result = bc.parse_result(stdout) or {}
    models = bc.models_of(result, requested) if result else {"observed_ids": [], "primary": None, "primary_count": 0,
                                                              "conflicts": [], "helpers": [], "single_model": None,
                                                              "context_window": {}}
    problems = []
    if rc != 0:
        problems.append(f"exit status {rc}")
    if timed_out:
        problems.append(f"timed out after {timeout} s")
    if not result:
        problems.append("no JSON result envelope")
    elif result.get("is_error"):
        problems.append(f"error envelope: {str(result.get('result') or result.get('errors'))[:300]}")
    if result and models["primary_count"] != 1:
        problems.append(f"served models {models['observed_ids']}: not exactly one {route['spawn_alias']}")
    window = models["context_window"].get(models["primary"]) if models["primary"] else None
    expected = bc.EXPECTED_CONTEXT_WINDOW.get(requested)
    mismatch = bool(result and expected and window is not None and window != expected)
    if mismatch:
        problems.append(f"served a {window}-token context window; the full runs were served {expected}")
    if result and models["primary_count"] == 1 and models["helpers"]:
        # every Claude call of the 72 published full runs was served exactly one Claude model
        problems.append(f"also served {models['helpers']}; the full runs' calls were served {route['spawn_alias']} only")
        mismatch = True
    writes = bc.cache_writes(result) if result else None
    warnings = []
    if writes and writes["5m"] and bc.PROMPT_CACHE_TTL == "1h":
        warnings.append(f"{writes['5m']} prompt-cache tokens were written for 5 minutes, not the 1 hour asked for: "
                        f"its cost is not comparable with the full runs' as recorded")
    cost = result.get("total_cost_usd") if isinstance(result.get("total_cost_usd"), (int, float)) else None
    done = dict(stub)
    done.update({
        "status": "ended", "ended_at": bc.utc_now(), "elapsed_seconds": round(elapsed, 1), "exit_status": rc,
        "timed_out": timed_out, "ok": not problems, "problems": problems,
        "error_class": classify(result, problems, stderr, timed_out, models, mismatch),
        "wrote_files": any(p.is_file() for p in out.rglob("*")),
        "inference_started": bool(result and ((result.get("num_turns") or 0) > 0 or (cost or 0) > 0)) or timed_out,
        "prompt_sha256": bc.sha256_bytes(prompt.encode()),
        "settings_sha256": bc.sha256_bytes(bc.canonical(settings)),
        "argv": [a.replace(str(adir), "<attempt>").replace(home, "~") for a in argv[1:]],
        "model": models, "context_window": window,
        "usage": bc.usage_of(result) if result else None,
        "cost": {"amount": cost, "currency": "USD", "source": "cli_log"} if cost is not None else None,
        "num_turns": result.get("num_turns"),
        "duration_api_seconds": (result.get("duration_api_ms") or 0) / 1000 or None,
        "subtype": result.get("subtype"), "stop_reason": result.get("stop_reason"),
        "terminal_reason": result.get("terminal_reason"), "api_error_status": result.get("api_error_status"),
        "permission_denials": len(result.get("permission_denials") or []),
        "cache_writes": writes, "warnings": warnings,
    })
    record["attempts"][-1] = done
    return done


# --- after the call -------------------------------------------------------------------------------------------------
def _regular(path: pathlib.Path, root: pathlib.Path) -> bool:
    return path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve())


def _evidence_ok(path: pathlib.Path, rel: str, exempt: set[str]) -> bool:
    suffix, size = path.suffix.lower(), path.stat().st_size
    if size > 20 << 20:
        return False
    rep = validate_outputs.Report()
    if suffix in validate_outputs.TEXT_SUFFIXES:
        text = validate_outputs.read_text(path, rep, rel)
        if suffix in validate_outputs.DATA_SUFFIXES and text.count("\n") > 200:
            return False   # more rows than an aggregate result: possibly participant rows the patterns cannot see
    elif suffix == ".pdf":
        text = validate_outputs.pdf_text(path)
        if text is None:
            return False
    elif suffix in validate_outputs.IMAGE_SUFFIXES:
        return True
    else:
        return False
    validate_outputs.hygiene(rep, rel, text, exempt, strict=True)
    return not rep.errors


def _copy_records(adir: pathlib.Path, records: pathlib.Path, exempt: set[str]) -> list[str]:
    """The prompt, settings, CLI envelope and stderr, with the home folder masked. The envelope keeps the count and
    tool names of permission denials but never their tool input (a denied Write carries the whole file); a final
    message or stderr that fails the hygiene check is withheld."""
    home = str(pathlib.Path.home())
    withheld = []
    for name in ("prompt.txt", "claude_settings.json"):
        if (adir / name).is_file():
            (records / name).write_text((adir / name).read_text(errors="replace").replace(home, "~"))
    envelope = bc.parse_result((adir / "claude_stdout.json").read_text(errors="replace")) if (adir / "claude_stdout.json").is_file() else None
    if envelope is not None:
        denied = [d for d in envelope.get("permission_denials") or [] if isinstance(d, dict)]
        if any(d.get("tool_input") for d in denied):
            withheld.append(f"records/claude_stdout.json:permission_denials[].tool_input "
                            f"({sum(1 for d in denied if d.get('tool_input'))})")
        envelope["permission_denials"] = [{k: d.get(k) for k in ("tool_name", "tool_use_id")} for d in denied]
        rep = validate_outputs.Report()
        validate_outputs.hygiene(rep, "result", str(envelope.get("result") or ""), exempt, strict=True)
        if rep.errors:
            envelope["result"] = "[withheld: failed the hygiene check]"
            withheld.append("records/claude_stdout.json:result")
        (records / "claude_stdout.json").write_text(json.dumps(envelope, indent=1).replace(home, "~"))
    if (adir / "claude_stderr.txt").is_file():
        text = (adir / "claude_stderr.txt").read_text(errors="replace").replace(home, "~")
        rep = validate_outputs.Report()
        validate_outputs.hygiene(rep, "stderr", text, exempt, strict=True)
        if rep.errors:
            withheld.append("records/claude_stderr.txt")
        else:
            (records / "claude_stderr.txt").write_text(text)
    return withheld


def copy_outputs(out: pathlib.Path, adir: pathlib.Path, dest: pathlib.Path, exempt: set[str]) -> dict:
    """The comment, its record and its evidence go to the shared tree only if they pass the pipeline's hygiene
    patterns (participant codes, codes or identifiers beside dates and clock times, absolute paths, e-mail
    addresses). Anything that fails, cannot be checked (binary or compressed data, oversized text, links) or holds
    more rows than an aggregate result stays in the private work folder and is listed as withheld."""
    kept, withheld = [], []
    for name in OUTPUT_FILES:
        path = out / name
        if not path.exists() and not path.is_symlink():
            continue
        rep = validate_outputs.Report()
        if _regular(path, out):
            validate_outputs.hygiene(rep, name, validate_outputs.read_text(path, rep, name), exempt, strict=True)
        if not _regular(path, out) or rep.errors:
            withheld.append(name)
            continue
        shutil.copy2(path, dest / name)
        kept.append(name)
    evidence = out / "evidence"
    walk = os.walk(evidence, followlinks=False) if evidence.is_dir() and not evidence.is_symlink() else []
    for top, _dirs, files in walk:
        for name in sorted(files):
            path = pathlib.Path(top) / name
            rel = str(path.relative_to(out))
            if not _regular(path, out) or not _evidence_ok(path, rel, exempt):
                withheld.append(rel)
                continue
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest / rel)
            kept.append(rel)
    records = dest / "records"
    records.mkdir(exist_ok=True)
    withheld += _copy_records(adir, records, exempt)
    return {"kept": kept, "withheld_from_tree": withheld, "sha256": {n: bc.sha256_file(dest / n) for n in kept}}


def supersede(dest: pathlib.Path, tree: pathlib.Path) -> str | None:
    """A rerun never deletes an earlier baseline output: it moves to baseline_comments/.superseded/."""
    if dest.exists() and any(dest.iterdir()):
        target = out_root(tree) / ".superseded" / f"{dest.parent.name}__{dest.name}__{time.strftime('%Y%m%dT%H%M%S')}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dest), str(target))
        return str(target.relative_to(tree))
    return None


def totals(attempts: list[dict]) -> dict:
    ended = [a for a in attempts if a.get("status") == "ended"]

    def total(key, sub=None, rows=None):
        vals = [(a.get(key) or {}).get(sub) if sub else a.get(key) for a in (ended if rows is None else rows)]
        vals = [v for v in vals if isinstance(v, (int, float))]
        return sum(vals) if vals else None
    spent = [a for a in attempts if a.get("inference_started")]
    cost_unknown = sum(1 for a in spent if not isinstance((a.get("cost") or {}).get("amount"), (int, float)))
    observed = [a for a in ended if a["model"]["observed_ids"]]
    return {
        "model_calls": len(attempts), "calls_with_inference": sum(1 for a in ended if a.get("inference_started")),
        "elapsed_seconds": total("elapsed_seconds", rows=attempts), "duration_api_seconds": total("duration_api_seconds"),
        "num_turns": total("num_turns"), "cost_usd": total("cost", "amount"), "calls_with_unknown_cost": cost_unknown,
        "total_tokens": total("usage", "total_tokens"), "output_tokens": total("usage", "output_tokens"),
        "thinking_tokens": total("usage", "thinking_tokens"),
        "single_model": all(a["model"]["single_model"] for a in observed) if observed else None,
        "observed_models": sorted({m for a in ended for m in a["model"]["observed_ids"]}),
        "permission_denials": total("permission_denials"),
        "cache_writes_5m": total("cache_writes", "5m"), "cache_writes_1h": total("cache_writes", "1h"),
        "final_attempt": attempts[-1]["number"] if attempts else None,
    }


def code_hashes() -> dict[str, str]:
    """What ran: every script, the config, the prompt template and VERSION, by hash."""
    files = [*sorted((bc.SKILL / "scripts").glob("*.py")), bc.SKILL / "config" / "baseline.json", PROMPT,
             bc.SKILL / "VERSION"]
    return {str(p.relative_to(bc.SKILL)): bc.sha256_file(p) for p in files if p.is_file()}


def render_into(dest: pathlib.Path, tree: pathlib.Path) -> dict:
    """Rendering is presentation only: a failure is recorded and never changes the run's outcome."""
    import render_comment
    try:
        rec = render_comment.render(dest, tree)
    except Exception as exc:
        rec = {"ok": False, "files": [], "error": f"{type(exc).__name__}: {exc}"[:800]}
    rec["rendered_at"] = bc.utc_now()
    rec["sha256"] = {n: bc.sha256_file(dest / n) for n in rec.get("files") or [] if (dest / n).is_file()}
    return rec


def rerender(tree: pathlib.Path, args) -> int:
    """Render a finished run's comment again, keeping the earlier render record in render_history."""
    paper = pick(load_subset(tree), args.paper)
    dest = final_dir(tree, paper)
    record = run_record(dest)
    kept = ((record or {}).get("outputs") or {}).get("kept") or []
    if not ("REPORT_final.md" in kept and "front_matter.md" in kept):
        raise bc.BaselineError(f"{paper['audit_id']}: no comment in the tree to render")
    if record.get("render"):
        record.setdefault("render_history", []).append(record["render"])
    record["render"] = render_into(dest, tree)
    bc.write_json(dest / "run.json", record)
    private = pathlib.Path(str(record.get("work_folder") or "")).expanduser() / "run.json"
    if record.get("work_folder") and private.is_file():
        bc.write_json(private, record)
    pages = record["render"].get("pages") or {}
    print(f"render {paper['audit_id']}: {record['render'].get('renderer')}; body pages {pages.get('body_pages')}, "
          f"over the cap: {pages.get('over_page_cap')}")
    return 0 if record["render"].get("ok") else 1


def log_run(tree: pathlib.Path, record: dict) -> None:
    """Every run, finished or not, one line: what the comparison counts as resources spent."""
    t = record.get("totals") or totals(record.get("attempts") or [])
    line = {"audit_id": record["paper"]["audit_id"], "run_id": record["run_id"], "started_at": record["started_at"],
            "ended_at": record.get("ended_at"), "status": status_of(record),
            "error_class": (record.get("outcome") or {}).get("error_class"), "model_calls": t.get("model_calls"),
            "calls_with_inference": t.get("calls_with_inference"), "cost_usd": t.get("cost_usd"),
            "calls_with_unknown_cost": t.get("calls_with_unknown_cost"),
            "elapsed_seconds": t.get("elapsed_seconds"), "total_tokens": t.get("total_tokens"),
            "cache_writes_5m": t.get("cache_writes_5m"), "cache_writes_1h": t.get("cache_writes_1h"),
            "condition": {k: record["condition"].get(k) for k in ("requested_model", "reasoning_effort", "max_budget_usd",
                                                                  "timeout_seconds", "cli_version", "prompt_template_sha256",
                                                                  "prompt_cache_ttl")}}
    with open(out_root(tree) / "RUNLOG.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def orphans(paper: dict) -> list[pathlib.Path]:
    """Work-folder records of runs that never finished: the runner was killed (kill -9, a crash, a reboot)."""
    found = []
    for path in sorted((bc.work_root() / paper["audit_id"]).glob("*/run.json")):
        try:
            if "ended_at" not in json.loads(path.read_text()):
                found.append(path)
        except (OSError, json.JSONDecodeError):
            continue
    return found


def close_orphans(tree: pathlib.Path, paths: list[pathlib.Path]) -> None:
    """Book a killed run once, as abandoned, so the comparison counts its call."""
    for path in paths:
        record = json.loads(path.read_text())
        record["ended_at"] = bc.utc_now()
        record["outcome"] = {"status": "abandoned", "error_class": None,
                             "problems": ["the runner was killed before it could record this run"]}
        for att in record.get("attempts") or []:
            if att.get("status") == "running":
                att.update(status="abandoned", inference_started=True)
        record["totals"] = totals(record.get("attempts") or [])
        bc.write_json(path, record)
        log_run(tree, record)


def run_one(tree: pathlib.Path, paper: dict, args, scan: dict | None = None) -> dict:
    dest = final_dir(tree, paper)
    previous = run_record(dest)
    prev_status = status_of(previous)
    killed = orphans(paper)
    if killed and not previous:
        prev_status = "abandoned"
    if (previous or killed) and not args.force and not (args.rerun_failed and prev_status in RERUNNABLE):
        hint = " (--rerun-failed reruns it)" if prev_status in RERUNNABLE else ""
        print(f"skip {paper['audit_id']}: already {prev_status}{hint}; --force reruns it")
        return previous or {"outcome": {"status": prev_status}}
    if scan is None and not args.no_scan:
        scan = contamination(tree, [paper])
    if scan and scan["hits"]:
        raise bc.BaselineError(f"{paper['audit_id']}: a readable copy of a full-pipeline output: {scan['hits'][0]}")
    close_orphans(tree, killed)
    cli = bc.resolve_cli(args.claude_bin)
    requested, effort = args.model, args.effort
    budget, timeout = str(args.max_budget_usd), int(args.timeout_seconds)
    check_work_root(tree, bc.work_root())
    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    run_root = bc.work_root() / paper["audit_id"] / run_id
    bc.assert_expressible([run_root])
    run_root.mkdir(parents=True)
    record = {
        "schema_version": "i4r-baseline-run-1", "baseline_version": bc.VERSION, "run_id": run_id,
        "host": socket.gethostname(), "operator": os.environ.get("USER"),
        "paper": {k: paper[k] for k in ("audit_id", "journal", "runs_folder", "metadata_folder", "protocol_version",
                                        "code_present")},
        "condition": {
            "name": bc.CONFIG["condition"], "single_agent": True, "revision_rounds": 0, "critics": None,
            "requested_model": requested, "spawn_alias": bc.ROUTES[requested]["spawn_alias"],
            "reasoning_effort": effort, "timeout_seconds": timeout, "max_budget_usd": budget,
            "retry_rule": f"up to {max(0, args.retries)} more call(s), only after a transient or transport error "
                          f"with no inference started and no file written",
            "tools": [*bc.NON_BASH_TOOLS, "Bash"], "file_tools": "scoped to the run's input, packet and output",
            "bash_allowlist_sha256": bc.sha256_bytes(bc.canonical(list(bc.BASH_ALLOWLIST))), "network": "denied",
            "cli_path": cli.replace(str(pathlib.Path.home()), "~"), "cli_version": bc.cli_version(cli),
            "prompt_template_sha256": bc.sha256_file(PROMPT),
            "output_cap": bc.OUTPUT_CAP.get(requested), "prompt_cache_ttl": bc.PROMPT_CACHE_TTL,
            "served_models": f"{bc.ROUTES[requested]['spawn_alias']} only; any other served model is a "
                             f"condition_mismatch",
        },
        "code_sha256": code_hashes(),
        "previous_run": {"run_id": (previous or {}).get("run_id"), "status": prev_status} if previous or killed else None,
        "contamination_scan": {**{k: v for k, v in scan.items() if k != "hits"}, "hits": len(scan["hits"])}
                              if scan else "skipped",
        "work_folder": str(run_root).replace(str(pathlib.Path.home()), "~"), "started_at": bc.utc_now(),
    }
    print(f"run {paper['audit_id']} ({paper['journal']}): {requested} at {effort}, budget ${budget}, "
          f"timeout {timeout} s, work folder {run_root}")
    owns_dest = not dest.exists()
    try:
        record["environment"] = environment(requested)
        record["inputs"] = prepare(tree, paper, run_root)
        exempt = set(json.loads((run_root / "packet" / "tools" / "validate_config.json").read_text())["exempt"])
        for number in range(1, 2 + max(0, args.retries)):
            att = attempt(cli, tree, run_root, number, requested, effort, budget, timeout, record)
            if att["ok"] or att["error_class"] not in RETRYABLE or att["wrote_files"] or att["inference_started"]:
                break
            print(f"  attempt {number}: {att['error_class']} before any inference; one more call")
        final = record["attempts"][-1]
        status = ("timeout" if final["timed_out"] else "budget_exhausted" if final["error_class"] == "budget_exhausted"
                  else "failed" if not final["ok"] else "pending_validation")
        record["outcome"] = {"status": status, "error_class": final["error_class"], "problems": final["problems"]}
        record["totals"] = totals(record["attempts"])
        record["superseded_to"] = supersede(dest, tree)   # a paid call was made: from here on dest is this run's
        dest.mkdir(parents=True, exist_ok=True)
        owns_dest = True
        try:
            out, adir = run_root / f"out_{final['number']}", run_root / f"attempt_{final['number']}"
            validation = validate_outputs.validate(out, run_root / "input" / "raw" / "pkg", exempt)
            bc.write_json(run_root / "validation.json", validation)
            record["validation"] = validation
            if status == "pending_validation":
                record["outcome"]["status"] = "completed" if validation["ok"] else "completed_invalid"
            record["outputs"] = copy_outputs(out, adir, dest, exempt)
            bc.write_json(dest / "records" / "validation.json", validation)
            try:
                record["totals"]["findings"] = len(json.loads((dest / "findings.json").read_text())["findings"])
            except (OSError, ValueError, KeyError, TypeError):
                record["totals"]["findings"] = None
            kept = record["outputs"]["kept"]
            if not args.no_render and "REPORT_final.md" in kept and "front_matter.md" in kept:
                record["render"] = render_into(dest, tree)
        except Exception:
            record["postprocess_error"] = traceback.format_exc()[-2000:]
            if record["outcome"]["status"] == "pending_validation":
                record["outcome"]["status"] = "completed_invalid"
    except KeyboardInterrupt:
        outcome = record.get("outcome")
        if outcome is None:   # stopped during the call: nothing but the call itself is known
            record["outcome"] = {"status": "interrupted", "error_class": None, "problems": ["stopped by the operator"]}
        else:                 # stopped after the call: its outcome stands
            record["postprocess_interrupted"] = True
            if outcome["status"] == "pending_validation":
                outcome["status"] = "completed_invalid"
                outcome["problems"] = [*outcome["problems"], "stopped before validation finished"]
        raise
    except bc.BaselineError as exc:
        record["outcome"] = {"status": "failed", "error_class": "setup", "problems": [str(exc)]}
    except Exception:
        record["outcome"] = {"status": "failed", "error_class": "runner", "problems": [traceback.format_exc()[-2000:]]}
    finally:
        with _Uninterruptible():
            for sub in ("input", "packet"):
                bc.thaw_tree(run_root / sub)
            record["ended_at"] = bc.utc_now()
            record["totals"] = {**totals(record.get("attempts") or []),
                                **{k: v for k, v in (record.get("totals") or {}).items() if k == "findings"}}
            bc.write_json(run_root / "run.json", record)
            if owns_dest:   # never pair this run's record with another run's comment
                dest.mkdir(parents=True, exist_ok=True)
                bc.write_json(dest / "run.json", record)
            log_run(tree, record)
    o, t = record["outcome"], record.get("totals") or {}
    cost = t.get("cost_usd")
    mins = f"{t['elapsed_seconds'] / 60:.0f} min" if t.get("elapsed_seconds") else "0 min"
    extra = ""
    if record.get("validation") and not record["validation"]["ok"]:
        extra += f"; {len(record['validation']['errors'])} validation error(s)"
    if (record.get("outputs") or {}).get("withheld_from_tree"):
        extra += f"; withheld from the tree: {', '.join(record['outputs']['withheld_from_tree'])}"
    klass = f" ({o['error_class']})" if o.get("error_class") else ""
    print(f"  {o['status']}{klass}: {t.get('findings', '?')} findings, ${cost or 0:.2f}, {mins}{extra}")
    return record


# --- commands -------------------------------------------------------------------------------------------------------
def preflight(tree: pathlib.Path, args) -> int:
    problems, notes = [], []
    try:
        cli = bc.resolve_cli(args.claude_bin)
        version = bc.cli_version(cli)
        vt = bc.version_tuple(version)
        notes.append(f"Claude CLI: {cli} ({version}); the full runs used 2.1.206 to 2.1.295, mostly 2.1.285 and "
                     f"2.1.286. Each call's served model and context window are checked against theirs")
        if vt is None or vt in bc.KNOWN_BAD_CLI:
            problems.append(f"Claude CLI {version} serves claude-sonnet-5-5 a 200,000-token window (i4r-audit "
                            f"INSTALL); update it (claude update) or pass --claude-bin")
    except bc.BaselineError as exc:
        problems.append(str(exc))
    try:
        work = bc.work_root()
        notes.append(f"work folder: {work}")
        bc.assert_expressible([work])
        check_work_root(tree, work)
    except bc.BaselineError as exc:
        problems.append(str(exc))
    docs = str(pathlib.Path.home() / "miniconda3" / "envs" / "i4rdoc" / "bin")
    for tool in ("python3", "Rscript", "pdftotext", "pandoc", "soffice"):
        if not (shutil.which(tool) or shutil.which(tool, path=docs)
                or (tool == "soffice" and pathlib.Path("/Applications/LibreOffice.app").exists())):
            notes.append(f"warning: {tool} not found" + (" (comments will not be rendered)" if tool == "pandoc" else ""))
    if not i4r_scripts(tree):
        notes.append("warning: i4r-audit not found; deposit exemptions fall back to CSV/TSV headers and the "
                     "environment fingerprint stays the original host's")
    try:
        subset = load_subset(tree)
    except bc.BaselineError as exc:
        problems.append(str(exc))
        subset = {"papers": []}
    pending = 0
    for k, paper in enumerate(subset["papers"], 1):
        state = status_of(run_record(final_dir(tree, paper))) or "ready"
        try:
            audit = sources.source_dir(tree, paper)
            if not args.quick:
                sources.verified_snapshot(audit, paper["snapshot_sha256"])
        except bc.BaselineError as exc:
            problems.append(f"paper {k} ({paper['audit_id']}): {exc}")
            state = "BLOCKED"
        pending += state == "ready"
        print(f"  {k:2d} {paper['audit_id']}  {state:17s} code={'yes' if paper['code_present'] else 'no '}  "
              f"{paper['journal'][:40]}")
    if subset["papers"] and not args.no_scan:
        scan = contamination(tree, subset["papers"])
        hits = scan["hits"]
        for hit in hits:
            problems.append(f"a readable copy of a full-pipeline output: {hit}; move it into a denied folder or add "
                            f"its folder to extra_deny_read in config/baseline.json")
        notes.append(f"contamination scan: {len(hits)} readable copy(ies) of subset papers' comments outside the "
                     f"denied folders ({scan['targets']} files sought among {scan['files_seen']} in "
                     f"{scan['seconds']} s)")
    budget = float(args.max_budget_usd)
    notes.append(f"{pending} paper(s) never run; each run spends at most about ${budget:.0f} (the CLI stops a call at "
                 f"its budget after the turn in progress, and a retry happens only when no inference started): about "
                 f"${pending * budget:.0f} for them, one at a time. A rerun (--rerun-failed, --force) is a new run with "
                 f"its own budget")
    for n in notes:
        print(n)
    for p in problems:
        print("PROBLEM: " + p)
    print("preflight " + ("OK" if not problems else f"FAILED ({len(problems)} problem(s))"))
    return 0 if not problems else 1


PROBE_PROMPT = """This is a sandbox check, not an audit. Do exactly these steps with as few tool calls as possible, then
write probe.json in the current directory.
1. Read {INPUT}/indexes/file_inventory.json with the Read tool. Record "read_input": true if it worked.
2. Run: python3 -c "for i in range(3000): print(f'row {i:05d} ' + 'y' * 20)"  The output is long, so the tool saves
   it to a file. Read the first 3 lines of that saved file with the Read tool (limit 3). Record "read_saved_output":
   true if its first line starts with "row 00000".
3. Run: Rscript -e "cat(R.version.string)"  and  python3 --version. Record "r" and "python" with what they printed
   (null if a command failed).
4. Try to Read {DENIED} with the Read tool. Record "denied_read_refused": true if it was refused.
5. With the Bash tool run  ls ~/Library/CloudStorage  and then  head -c 80 {DENIED}  as two separate commands.
   Record "bash_denied_refused": true only if neither printed a file name or any file content.
Write probe.json with exactly those six keys, then reply DONE."""


def request_tools(bodies: pathlib.Path) -> list[str]:
    """The tools the CLI offered the model in its requests (names, or the type of a server tool)."""
    names: set[str] = set()
    for f in sorted(p for p in bodies.rglob("*.request.json") if p.is_file()) or sorted(bodies.rglob("*.json")):
        try:
            body = json.loads(f.read_text(errors="replace"))
        except json.JSONDecodeError:
            continue
        for tool in (body.get("tools") if isinstance(body, dict) else None) or []:
            if isinstance(tool, dict):
                names.add(str(tool.get("name") or tool.get("type")))
    return sorted(names)


def request_max_tokens(bodies: pathlib.Path, requested: str) -> list[int]:
    """max_tokens of every request the CLI sent for the requested model, from its raw request-body log. The envelope's
    modelUsage.maxOutputTokens is the CLI's catalog default (2.1.206 reports 64,000 while sending 128,000), so only
    the request itself shows the cap the call ran under."""
    served, found = bc.ROUTES[requested]["served"], []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("max_tokens"), int) and served.match(str(node.get("model", "")).lower()):
                found.append(node["max_tokens"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    for f in sorted(p for p in bodies.rglob("*") if p.is_file()):
        text = f.read_text(errors="replace")
        try:
            walk(json.loads(text))
        except json.JSONDecodeError:
            for line in text.splitlines():
                try:
                    walk(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return found


def probe(tree: pathlib.Path, args) -> int:
    cli = bc.resolve_cli(args.claude_bin)
    check_work_root(tree, bc.work_root())
    run_root = bc.work_root() / "_probe" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"
    run_root.mkdir(parents=True)
    (run_root / "input" / "indexes").mkdir(parents=True)
    (run_root / "input" / "indexes" / "file_inventory.json").write_text('{"probe": true}\n')
    (run_root / "packet").mkdir()
    out = run_root / "out_1"
    out.mkdir()
    reads = [run_root / "input", run_root / "packet"]
    deny = deny_read_paths(tree, run_root)
    denied = tree / "README.md" if (tree / "README.md").is_file() else out_root(tree) / "README.md"
    settings = bc.sandbox_settings(reads, out, deny, reads)
    bc.write_json(run_root / "claude_settings.json", settings)
    prompt = PROBE_PROMPT.replace("{INPUT}", str(run_root / "input")).replace("{DENIED}", str(denied))
    argv = bc.claude_command(cli, run_root / "claude_settings.json", reads, str(uuid.uuid4()), "claude-sonnet-5-5",
                             "low", "1", bc.file_tool_rules(reads, out))
    bodies = run_root / "api_bodies"   # the probe's own requests only: a paper run never logs bodies (they hold the deposit)
    bodies.mkdir()
    env = {**bc.spawn_env("sonnet-5-5"), "OTEL_LOG_RAW_API_BODIES": f"file:{bodies}"}
    rc, stdout, stderr, timed_out = call(argv, prompt, out, env, 600)
    (run_root / "claude_stdout.json").write_text(stdout)
    (run_root / "claude_stderr.txt").write_text(stderr)
    try:
        result = json.loads((out / "probe.json").read_text())
    except (OSError, json.JSONDecodeError):
        result = {}
    envelope = bc.parse_result(stdout) or {}
    window = bc.models_of(envelope, "sonnet-5-5")["context_window"] if envelope else {}
    checks = {"read_input": result.get("read_input") is True, "read_saved_output": result.get("read_saved_output") is True,
              "r": bool(result.get("r")), "python": bool(result.get("python")),
              "denied_read_refused": result.get("denied_read_refused") is True,
              "bash_denied_refused": result.get("bash_denied_refused") is True,
              "context_window_1M": 1_000_000 in window.values()}
    caps = request_max_tokens(bodies, "sonnet-5-5")
    cap = bc.OUTPUT_CAP["sonnet-5-5"]
    if caps:
        checks[f"request_max_tokens_{cap}"] = set(caps) == {cap}
    writes = bc.cache_writes(envelope) if envelope else None
    if writes and (writes["5m"] or writes["1h"]):
        checks[f"cache_writes_{bc.PROMPT_CACHE_TTL}"] = writes[bc.PROMPT_CACHE_TTL] > 0 and not any(
            v for k, v in writes.items() if k != bc.PROMPT_CACHE_TTL)
    for k, v in checks.items():
        print(f"  {'ok  ' if v else 'FAIL'} {k}")
    if caps and set(caps) != {cap}:
        print(f"  the CLI sent max_tokens {sorted(set(caps))}; the full runs ran with CLAUDE_CODE_MAX_OUTPUT_TOKENS={cap}")
    if not caps:
        print(f"  n/a  request_max_tokens_{cap}: this CLI wrote no request bodies, so the output cap is unverified")
    if not (writes and (writes["5m"] or writes["1h"])):
        print(f"  n/a  cache_writes_{bc.PROMPT_CACHE_TTL}: the envelope reports no cache writes by lifetime")
    offered = request_tools(bodies)
    if offered:
        print(f"  tools offered in the requests: {', '.join(offered)}")
    bc.write_json(run_root / "probe_result.json", {"checks": checks, "request_max_tokens": sorted(set(caps)),
                                                   "cache_writes": writes, "offered_tools": offered})
    print(f"probe cost ${envelope.get('total_cost_usd') or 0:.2f}; records in {run_root}")
    return 0 if all(checks.values()) and rc == 0 and not timed_out else 1


def batch(tree: pathlib.Path, args) -> int:
    subset = load_subset(tree)
    scan = None
    if not args.no_scan:
        scan = contamination(tree, subset["papers"])
        if scan["hits"]:
            raise bc.BaselineError("readable copies of full-pipeline outputs: " + "; ".join(scan["hits"][:5])
                                   + ". Move them into a denied folder or list their folders under extra_deny_read")
    for paper in subset["papers"]:
        record = run_one(tree, paper, args, scan)
        if (record.get("outcome") or {}).get("error_class") in SYSTEMIC:
            print(f"stopping the batch: {record['outcome']['error_class']} would stop the next paper too")
            return 1
    return 0


def status(tree: pathlib.Path, _args) -> int:
    subset = load_subset(tree)
    print(subset["method"])
    for k, paper in enumerate(subset["papers"], 1):
        rec = run_record(final_dir(tree, paper)) or {}
        t = rec.get("totals") or {}
        n = t.get("findings")
        cost = t.get("cost_usd")
        mins = f"{t['elapsed_seconds'] / 60:4.0f} min" if t.get("elapsed_seconds") else ""
        line = f"  {k:2d} {paper['audit_id']}  {status_of(rec) or 'not run':17s} "
        line += "" if n is None else f"{n:2d} findings  "
        line += "" if cost is None else f"${cost:6.2f}  "
        print(line + mins)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("preflight", "probe", "run", "batch", "status", "render"):
        p = sub.add_parser(name)
        if name in ("run", "render"):
            p.add_argument("paper", help="an audit id from SUBSET.json, or its 1-based position")
        if name in ("preflight", "probe", "run", "batch"):
            p.add_argument("--claude-bin")
            p.add_argument("--allow-old-cli", action="store_true")
        if name == "preflight":
            p.add_argument("--quick", action="store_true", help="skip re-hashing every snapshot")
            p.add_argument("--max-budget-usd", default=bc.CONFIG["limits"]["max_budget_usd"])
        if name in ("preflight", "run", "batch"):
            p.add_argument("--no-scan", action="store_true", help="skip the contamination scan (not for paid runs)")
        if name in ("run", "batch"):
            p.add_argument("--model", default=bc.CONFIG["model"]["requested_model"], choices=sorted(bc.ROUTES))
            p.add_argument("--effort", default=bc.CONFIG["model"]["reasoning_effort"],
                           choices=["low", "medium", "high", "xhigh", "max"])
            p.add_argument("--max-budget-usd", default=bc.CONFIG["limits"]["max_budget_usd"])
            p.add_argument("--timeout-seconds", type=int, default=bc.CONFIG["limits"]["timeout_seconds"])
            p.add_argument("--retries", type=int, default=bc.CONFIG["retries_on_transient"],
                           help="extra calls after a transient or transport error with no inference started")
            p.add_argument("--force", action="store_true", help="rerun a paper that already has a run (the old output "
                                                                 "moves to baseline_comments/.superseded/)")
            p.add_argument("--rerun-failed", action="store_true", help="rerun papers whose run failed, timed out or "
                                                                        "was interrupted")
            p.add_argument("--no-render", action="store_true")
    args = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _on_signal)
    if signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN:   # under nohup a terminal hangup must not stop a batch
        signal.signal(signal.SIGHUP, _on_signal)
    tree = bc.find_tree()
    if args.command in ("probe", "run", "batch") and not args.allow_old_cli:
        vt = bc.version_tuple(bc.cli_version(bc.resolve_cli(args.claude_bin)))
        if vt is None or vt in bc.KNOWN_BAD_CLI:
            raise bc.BaselineError("this Claude CLI version serves claude-sonnet-5-5 a 200,000-token window; run preflight")
    if args.command == "preflight":
        return preflight(tree, args)
    if args.command == "status":
        return status(tree, args)
    lock, mine = acquire_lock(tree)
    try:
        if args.command == "probe":
            return probe(tree, args)
        if args.command == "render":
            return rerender(tree, args)
        if args.command == "run":
            record = run_one(tree, pick(load_subset(tree), args.paper), args)
            return 0 if status_of(record) in ("completed", "completed_invalid", "budget_exhausted") else 1
        return batch(tree, args)
    finally:
        release_lock(lock, mine)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except bc.BaselineError as exc:
        print(f"run_baseline: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt as exc:
        print(f"run_baseline: stopped ({exc or 'interrupt'})", file=sys.stderr)
        sys.exit(130)
