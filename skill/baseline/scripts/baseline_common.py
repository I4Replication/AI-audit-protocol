"""Shared helpers for the single-agent baseline.

The baseline audits a paper in ONE Claude call: one model, one session, no critics and no revision rounds. Where it
launches, confines and accounts for that call it mirrors i4r-audit 2.7.7's Phase-1 auditor launch
(scripts/run_phase1.py claude_command, scripts/phase1_runtime.py sandbox_settings and BASH_ALLOWLIST,
scripts/run_coordinator.py parse_provider_result / extract_usage, scripts/claude_routes.py spawn_env), copied here
so that a later change to the live pipeline cannot move the baseline's conditions. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
from typing import Any, Iterable

SKILL = pathlib.Path(__file__).resolve().parent.parent
VERSION = (SKILL / "VERSION").read_text().strip()
CONFIG = json.loads((SKILL / "config" / "baseline.json").read_text())

# --- the model route: i4r-audit claude_routes.SONNET_5_5_XHIGH ------------------------------------------------------
ROUTES = {
    "sonnet-5-5": {"spawn_alias": "claude-sonnet-5-5", "display": "Claude Sonnet 5.5",
                   "served": re.compile(r"^claude-sonnet-5-5(?:-\d{8})?(?:\[[0-9a-z]+\])?$")},
}
ANY_SONNET = re.compile(r"(?:^|[-_.])sonnet(?:$|[-_.])")
REMAPPING_ENV = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL")
OUTPUT_CAP = {"sonnet-5-5": 128_000}
# Prompt-cache lifetime. The full runs' Claude calls wrote their cache for 1 hour (envelope usage.cache_creation:
# ephemeral_1h_input_tokens only, on the operators' Claude subscriptions). The CLI picks the lifetime by account unless
# CLAUDE_CODE_PROMPT_CACHE_TTL says otherwise (an API key gets 5 minutes, billed lower), so the baseline sets it, and
# drops the variables that would override it.
PROMPT_CACHE_TTL = "1h"
CACHE_OVERRIDE_ENV = ("FORCE_PROMPT_CACHING_5M", "DISABLE_PROMPT_CACHING", "DISABLE_PROMPT_CACHING_SONNET")
# A launching Claude session's own links (its messaging socket and token, its session ids, the user's e-mail) are no
# part of the condition and never reach the call.
SESSION_LINK_ENV = ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_HOST_SESSION_ID",
                    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_USER_EMAIL", "CLAUDE_PID")
# Every Claude call of all 50 published Sonnet 5.5 full runs was served claude-sonnet-5-5 with a 1,000,000-token context
# window, on CLI 2.1.206, 2.1.284, 2.1.285, 2.1.286, 2.1.289, 2.1.293 and 2.1.295 (their probe receipts and CLI
# envelopes). All ran with CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000, which 2.1.206 honours (request bodies, 2026-09-30)
# although its envelope reports the catalog default, 64,000; `probe` checks the cap in the requests themselves.
# i4r-audit INSTALL records that 2.1.281 does not recognise the id and serves a 200,000-token window. A baseline call
# served any other window ran a different condition, so the runner refuses it (and stops a batch).
KNOWN_BAD_CLI = {(2, 1, 281)}
EXPECTED_CONTEXT_WINDOW = {"sonnet-5-5": 1_000_000}

# --- tools and sandbox: i4r-audit phase1_runtime.BASH_ALLOWLIST / NON_BASH_TOOLS, byte for byte -------------------
BASH_ALLOWLIST: tuple[str, ...] = (
    "Bash(mkdir *)", "Bash(cp *)", "Bash(mv *)", "Bash(cd *)", "Bash(ls *)", "Bash(cat *)", "Bash(head *)",
    "Bash(tail *)", "Bash(python3 *)", "Bash(python *)", "Bash(Rscript *)", "Bash(R CMD *)", "Bash(jupyter *)",
    "Bash(quarto *)", "Bash(pdftotext *)", "Bash(pdfinfo *)", "Bash(file *)", "Bash(find *)", "Bash(rg *)",
    "Bash(wc *)", "Bash(sort *)", "Bash(uniq *)", "Bash(diff *)", "Bash(cmp *)", "Bash(shasum *)",
    "Bash(sha256sum *)", "Bash(unzip -l *)", "Bash(echo *)", "Bash(printf *)", "Bash(awk *)", "Bash(sed *)",
    "Bash(grep *)", "Bash(cut *)", "Bash(tr *)", "Bash(paste *)", "Bash(tee *)", "Bash(basename *)",
    "Bash(dirname *)", "Bash(pwd)", "Bash(test *)", "Bash(true)", "Bash(touch *)", "Bash(rm *)", "Bash(rmdir *)",
)
NON_BASH_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep", "Write", "Edit")
# i4r-audit run_phase1.BASH_STEER, byte for byte (the auditors' appended system prompt)
BASH_STEER = (
    "Work in as few tool calls as you can. Bash calls are checked against a fixed "
    "allowlist of common commands (python3, Rscript, mkdir, cp, mv, rm, ls, cat, head, "
    "tail, grep, awk, sed, find, shasum and similar) and every part of a compound command "
    "must be on it, so chaining allowed commands with && or | is fine. Redirection (>, >>, "
    "2>&1) and heredocs are never allowed: have your scripts write their own output files. "
    "A denied command is logged and costs you a turn; it does not fail the role."
)
GLOB_CHARS = set("?*[]{}\\")


class BaselineError(RuntimeError):
    """A condition the baseline refuses to run under."""


# --- small utilities -------------------------------------------------------------------------------------------------
def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def write_json(path: pathlib.Path, value: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def utc_now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def freeze_tree(root: pathlib.Path) -> None:
    """Read-only: files 0444, directories 0555 (the Write and Edit tools are not seatbelt-confined)."""
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def thaw_tree(root: pathlib.Path) -> None:
    if not root.exists():
        return
    root.chmod(0o755)
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)


def remove_tree(root: pathlib.Path) -> None:
    thaw_tree(root)
    shutil.rmtree(root)


# --- the project tree ------------------------------------------------------------------------------------------------
def find_tree(start: pathlib.Path | None = None) -> pathlib.Path:
    """The I4R discipline tree: the nearest ancestor holding data/, runs/ and metadata/."""
    env = os.environ.get("I4R_TREE")
    candidates = [pathlib.Path(env)] if env else []
    here = (start or pathlib.Path.cwd()).resolve()
    candidates += [here, *here.parents, SKILL.parent.parent]
    for path in candidates:
        if all((path / name).is_dir() for name in ("data", "runs", "metadata")):
            return path.resolve()
    raise BaselineError("no I4R tree found (a folder holding data/, runs/ and metadata/); run from the tree root "
                        "or set I4R_TREE")


def work_root() -> pathlib.Path:
    root = pathlib.Path(os.environ.get("I4R_BASELINE_WORK") or CONFIG["work_root"]).expanduser().resolve()
    assert_expressible([root])
    return root


def assert_expressible(paths: Iterable[pathlib.Path]) -> None:
    """The Claude CLI reads sandbox paths as globs, so a path with a glob character matches nothing and leaves the
    sandbox inert (i4r-audit phase1_runtime.assert_sandbox_expressible)."""
    for path in paths:
        bad = sorted(GLOB_CHARS & set(str(path)))
        if bad:
            raise BaselineError(f"path {path} holds {''.join(bad)!r}, which the CLI sandbox reads as a glob")


def safe_name(name: str) -> str:
    """A folder name without the characters the CLI sandbox reads as globs (i4r-audit tree_layout.sh)."""
    return "".join(ch for ch in name if ch not in GLOB_CHARS).strip() or "paper"


# --- the Claude CLI --------------------------------------------------------------------------------------------------
def resolve_cli(explicit: str | None = None) -> str:
    cli = explicit or os.environ.get("I4R_CLAUDE") or shutil.which("claude")
    if not cli:
        raise BaselineError("no Claude CLI found; install it or pass --claude-bin")
    if os.sep in cli:   # the call runs inside its output folder, so a relative path would not resolve there
        cli = str(pathlib.Path(cli).expanduser().resolve())
    elif shutil.which(cli):
        cli = shutil.which(cli)
    if not os.access(cli, os.X_OK):
        raise BaselineError(f"{cli} is not an executable Claude CLI")
    return cli


def cli_version(cli: str) -> str:
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BaselineError(f"cannot run {cli} --version: {exc}") from exc
    return (out.stdout or out.stderr).strip()


def version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in match.groups()) if match else None


def spawn_env(requested_model: str) -> dict[str, str]:
    drop = {*REMAPPING_ENV, *CACHE_OVERRIDE_ENV, *SESSION_LINK_ENV}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    if requested_model in OUTPUT_CAP:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(OUTPUT_CAP[requested_model])
    env["CLAUDE_CODE_PROMPT_CACHE_TTL"] = PROMPT_CACHE_TTL
    env["CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR"] = "1"
    return env


def cache_writes(result: dict[str, Any]) -> dict[str, int] | None:
    """Prompt-cache writes by lifetime (usage.cache_creation): a 1-hour write costs more than a 5-minute one."""
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    split = usage.get("cache_creation")
    if not isinstance(split, dict):
        return None
    return {"5m": _count(split.get("ephemeral_5m_input_tokens")) or 0,
            "1h": _count(split.get("ephemeral_1h_input_tokens")) or 0}


def file_tool_rules(read_roots: list[pathlib.Path], output: pathlib.Path) -> list[str]:
    """The file tools, scoped to the run's own folders. The Phase-1 auditors were granted bare Read/Glob/Grep/Write/Edit
    (every path); the baseline is not, because the published comments now exist on disk. A Read rule also covers
    Glob and Grep; an Edit rule covers Write. Under --permission-mode dontAsk every other path is refused, while the
    CLI still lets the agent read its own saved tool results."""
    return [*[f"Read(/{root}/**)" for root in [*read_roots, output]], f"Edit(/{output}/**)"]


def claude_command(cli: str, settings: pathlib.Path, read_dirs: list[pathlib.Path], session_id: str,
                   spawn_alias: str, effort: str, max_budget_usd: str, file_rules: list[str]) -> list[str]:
    """The Phase-1 auditor argv (i4r-audit run_phase1.claude_command), with the baseline's own read directories and
    path-scoped file-tool rules in place of the bare tool names."""
    return [
        cli, "--print", "--output-format", "json",
        "--model", spawn_alias, "--effort", effort,
        "--safe-mode",
        "--append-system-prompt", BASH_STEER,
        "--no-chrome", "--disable-slash-commands",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--settings", str(settings),
        "--add-dir", *[str(p) for p in read_dirs],
        "--no-session-persistence", "--session-id", session_id,
        "--max-budget-usd", max_budget_usd,
        "--permission-mode", "dontAsk",
        "--tools", ",".join([*NON_BASH_TOOLS, "Bash"]),
        "--allowedTools", *file_rules, *BASH_ALLOWLIST,
    ]


def sandbox_settings(read_roots: list[pathlib.Path], output: pathlib.Path, deny_read: list[pathlib.Path],
                     deny_write: list[pathlib.Path]) -> dict[str, Any]:
    """i4r-audit phase1_runtime.sandbox_settings, plus Read/Edit/Write permission denials on the same paths.

    The sandbox rules are a deny list (anything not named stays readable, so host R and Python libraries work).
    deny_read keeps the full pipeline's own records for every paper (runs/, comments/, metadata/, ...) and every
    other baseline run out of view; the permission rules repeat that for the file tools.
    """
    assert_expressible([*read_roots, output, *deny_read, *deny_write])
    tool_deny = ["WebFetch", "WebSearch"]
    for path in deny_read:
        tool_deny += [f"Read(/{path}/**)", f"Edit(/{path}/**)"]
    return {
        "permissions": {"allow": [*file_tool_rules(read_roots, output), *BASH_ALLOWLIST], "deny": tool_deny},
        "sandbox": {
            "enabled": True, "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": False, "allowUnsandboxedCommands": False,
            "network": {"allowedDomains": [], "deniedDomains": ["*"]},
            "filesystem": {
                "allowRead": [str(p) for p in [*read_roots, output]],
                "allowWrite": [str(output)],
                "denyRead": [str(p) for p in deny_read],
                "denyWrite": [str(p) for p in [*deny_read, *deny_write]],
            },
        },
        "i4rBashAllowlist": list(BASH_ALLOWLIST),
    }


# --- the result envelope: i4r-audit run_coordinator.parse_provider_result / extract_usage ---------------------------
def parse_result(stdout: str) -> dict[str, Any] | None:
    try:
        value = json.loads(stdout)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def usage_of(result: dict[str, Any]) -> dict[str, Any]:
    """Token counts summed over every served model (helpers included), the pipeline's derived total
    (input + cache read + cache creation + output), and thinking tokens, which the pipeline never captures."""
    camel = {"input_tokens": "inputTokens", "output_tokens": "outputTokens",
             "cached_input_tokens": "cacheReadInputTokens", "cache_creation_input_tokens": "cacheCreationInputTokens",
             "thinking_tokens": "thinkingTokens"}
    snake = {"input_tokens": "input_tokens", "output_tokens": "output_tokens",
             "cached_input_tokens": "cache_read_input_tokens",
             "cache_creation_input_tokens": "cache_creation_input_tokens", "thinking_tokens": "thinking_tokens"}
    per_model = result.get("modelUsage") if isinstance(result.get("modelUsage"), dict) else {}
    raw = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    counts: dict[str, int | None] = {}
    if per_model:
        for name, key in camel.items():
            values = [_count(item.get(key)) for item in per_model.values() if isinstance(item, dict)]
            counts[name] = sum(values) if values and all(v is not None for v in values) else None
    else:
        counts = {name: _count(raw.get(key)) for name, key in snake.items()}
    parts = ["input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens"]
    total = sum(counts[p] for p in parts) if all(counts[p] is not None for p in parts) else None
    return {**counts, "total_tokens": total, "total_tokens_kind": "derived" if total is not None else "unavailable",
            "total_tokens_components": parts if total is not None else []}


def models_of(result: dict[str, Any], requested_model: str) -> dict[str, Any]:
    per_model = result.get("modelUsage") if isinstance(result.get("modelUsage"), dict) else {}
    ids = [str(k) for k in per_model] or ([str(result["model"])] if result.get("model") else [])
    served = ROUTES[requested_model]["served"]
    primaries = [m for m in ids if served.match(m.lower())]
    conflicts = [m for m in ids if m not in primaries and ANY_SONNET.search(m.lower())]
    helpers = [m for m in ids if m not in primaries and m not in conflicts]
    windows = {m: per_model[m].get("contextWindow") for m in ids if isinstance(per_model.get(m), dict)}
    return {"observed_ids": ids, "primary": primaries[0] if len(primaries) == 1 else None,
            "primary_count": len(primaries) + len(conflicts) if primaries else 0, "conflicts": conflicts,
            "helpers": helpers, "single_model": len(ids) == 1 and len(primaries) == 1,
            "context_window": windows}


OUTPUT_LIMIT = re.compile(r"exceeded the \d+ output token maximum")


def classify_error(text: str, timed_out: bool) -> str:
    """i4r-audit 2.7.7 run_coordinator.classify_error, in its order, plus the CLI's unrecognised-model tag. The runner
    classifies the structured cases first (budget exhausted, session or usage limit, served model, context window)."""
    lower = text.lower()
    if timed_out:
        return "timeout"
    if any(t in lower for t in ("safety measures", "flagged this message", "cyber verification", "cyber safeguard")):
        return "provider_safety_refusal"
    if OUTPUT_LIMIT.search(lower):
        return "output_limit"
    rules = [
        ("authentication", ("authentication", "unauthorized", "api key", "login")),
        ("authorization", ("permission", "authorization", "forbidden")),
        ("quota", ("quota", "credit", "billing", "budget")),
        ("model", ("model not found", "unsupported model", "invalid model", "unrecognized_model")),
        ("capability", ("capability", "unsupported capability", "tool unsupported")),
        ("transient", ("rate limit", "429", "overloaded", "502", "503", "504", "5xx")),
    ]
    for name, needles in rules:
        if any(n in lower for n in needles):
            return name
    if "served sonnet" in lower or "sonnet 5 primary" in lower:
        return "model"
    if any(t in lower for t in ("network", "connection", "transport")):
        return "transport"
    return "permanent"
