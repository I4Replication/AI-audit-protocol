#!/usr/bin/env python3
"""Check that this host's Codex CLI can use the critic route a new audit freezes, before the audit starts.

    python3 scripts/check_critic_access.py [--force]

Every critique round of an audit initialised on 2.7.0 or later runs on the shipped critic route
(config/pipeline-policy-v2.json, models.critics: gpt-6.1-sol), and since 2.7.5 repin_critic_route.py no
longer moves an audit to another model. Whether a host can use that model depends on the account its Codex
CLI is signed in with (and on a CLI new enough: 0.159.0): some ChatGPT accounts get HTTP 400 "The
'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account", others do not, and an
API-key login can if the key's organisation offers the model. From 2026-09-30 to 2026-10-07 at least twelve
audits frozen on Sol found that out at the round-1 critique probe, after Phase 1 had been paid for, and were
re-pinned to gpt-5.6-terra. new_audit.sh runs this check before it reserves a paper.

What it does: one minimal call, the driver's exact `codex exec` command shape (same flags, the policy's
critic model and effort, a one-field output schema, no audit content), and nothing else. A passing result
is cached in ${I4R_STATE_DIR:-~/.i4r}/critic_access.json for 24 hours, keyed on the CLI path and version,
the `codex login status` line, a digest of the CLI's login store (${CODEX_HOME:-~/.codex}/auth.json, which
every login rewrites; only the digest is kept) and the route. So a host pays for at most one tiny call a
day, and a new login, another account or another CLI is checked again. A refusal is never cached as a
pass. --force always calls.

Exit 0: the route is usable. Exit 3: the account cannot use it (unsupported model, authentication,
authorization, quota, or any other refusal the provider returns, such as an API key without access to the model
or an unverified organisation; on a CLI older than 0.159.0, update the ChatGPT app first). Exit 4: a network,
rate-limit, server or timeout failure: run again. Exit 5: the Codex CLI rejected the call itself, with no
answer from the provider (an unknown option, an output-schema error): update the ChatGPT app or the CLI. Exit 2: no Codex CLI, or an
unreadable policy.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import codex_cli  # noqa: E402

POLICY = HERE.parent / "config" / "pipeline-policy-v2.json"
TOKEN = "I4R_CRITIC_ACCESS_OK"
PROMPT = (
    "I4R_CRITIC_ACCESS_CHECK\n"
    "This is an access check before an audit starts, not an audit. Return the JSON object "
    f'{{"status": "{TOKEN}"}} and nothing else.\n'
)
SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": [TOKEN]}},
    "required": ["status"],
    "additionalProperties": False,
}
MAX_AGE = dt.timedelta(hours=24)
CALL_TIMEOUT_SECONDS = 300
ACCOUNT_CLASSES = {"unsupported_model", "authentication", "authorization", "quota", "account_refusal"}
RETRY_CLASSES = {"transport", "throttling", "provider_5xx", "timeout"}
# Provider refusals the driver's classifier leaves "permanent" but that are about the account or key, not the CLI:
# an API key without the model, an unverified organisation, a plan's usage limit.
ACCOUNT_WORDS = re.compile(r"does not exist or you do not have access|do not have access to (?:the )?model|"
                           r"must be verified to use|usage limit|not available (?:on|for|with) (?:your|this) "
                           r"(?:plan|account|organi[sz]ation)", re.I)
MIN_CLI_FOR_SOL = "0.159.0"   # 0.158.0-alpha.2.1 was refused gpt-6.1-sol on 2026-09-29 (INSTALL.md, 2.7.0)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def critic_route(policy_path: pathlib.Path = POLICY) -> tuple[str, str]:
    critics = json.loads(policy_path.read_text(encoding="utf-8"))["models"]["critics"]
    return critics["requested_model"], critics["reasoning_effort"]


def scrub(text: str) -> str:
    """The login line without secrets: no key material, no e-mail address."""
    text = re.sub(r"\b(sk-[A-Za-z0-9]{0,4})[A-Za-z0-9_*.\-]+", r"\1...", text)
    text = re.sub(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", "<e-mail>", text)
    return " ".join(text.split())[:120]


def login_line(codex: str) -> str:
    try:
        result = subprocess.run([codex, "login", "status"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    lines = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    return scrub(lines[0]) if lines else "unknown"


def account_fingerprint() -> tuple[str, str]:
    """(login store directory, sha256 of its auth.json or "none"). Only the digest is kept: no token is stored."""
    home = pathlib.Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
    try:
        return str(home), hashlib.sha256((home / "auth.json").read_bytes()).hexdigest()
    except OSError:
        return str(home), "none"


def cli_outdated(version: str) -> bool:
    key = codex_cli.version_key(version)
    return key != (-1,) and key < codex_cli.version_key(MIN_CLI_FOR_SOL)


def state_file() -> pathlib.Path:
    return pathlib.Path(os.environ.get("I4R_STATE_DIR") or "~/.i4r").expanduser() / "critic_access.json"


def fresh_pass(stamp: dict, key: dict, now: dt.datetime) -> bool:
    if stamp.get("result") != "ok" or any(stamp.get(name) != value for name, value in key.items()):
        return False
    try:
        checked = dt.datetime.fromisoformat(str(stamp["checked_at"]).replace("Z", "+00:00"))
        if checked.tzinfo is None:
            return False
        return dt.timedelta(0) <= now - checked <= MAX_AGE
    except (KeyError, ValueError, TypeError):
        return False


def write_stamp(path: pathlib.Path, record: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError as exc:  # the cache is a convenience; the check itself stands
        print(f"note: could not record the result in {path}: {exc}", file=sys.stderr)
        try:
            temporary.unlink()
        except OSError:
            pass


def call(codex: str, model: str, effort: str) -> tuple[bool, str]:
    """(passed, provider diagnostics) for one call in the driver's command shape."""
    with tempfile.TemporaryDirectory(prefix="i4r-critic-access-") as directory:
        work = pathlib.Path(directory)
        (work / "workspace").mkdir()
        schema, response = work / "schema.json", work / "response.json"
        schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
        argv = [codex, "exec", "--skip-git-repo-check", "--ignore-user-config", "--color", "never", "--json",
                "--ephemeral", "-m", model, "-c", f"model_reasoning_effort={effort}", "-s", "read-only",
                "-C", str(work / "workspace"), "--output-schema", str(schema), "-o", str(response), "-"]
        try:
            result = subprocess.run(argv, input=PROMPT, capture_output=True, text=True,
                                    timeout=CALL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return False, f"the call did not finish within {CALL_TIMEOUT_SECONDS} s (timed out)"
        except OSError as exc:
            return False, f"could not run {codex}: {exc}"
        diagnostics = result.stdout + "\n" + result.stderr
        try:
            answer = json.loads(response.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            answer = None
        if result.returncode == 0 and isinstance(answer, dict) and answer.get("status") == TOKEN:
            return True, ""
        return False, diagnostics if diagnostics.strip() else f"exit {result.returncode} with no output"


def provider_words(diagnostics: str) -> str:
    """The provider's own reason, if one is in the JSON event stream, else the last non-empty line."""
    reasons = []
    for line in diagnostics.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") in {"error", "turn.failed"}:
            message = (event.get("error") or {}).get("message") if event.get("type") == "turn.failed" \
                else event.get("message")
            if isinstance(message, str):
                try:
                    inner = json.loads(message)
                    message = ((inner.get("error") or {}).get("message")) or message
                except (ValueError, AttributeError):
                    pass
                reasons.append(message)
    if reasons:
        return reasons[-1]
    lines = [line.strip() for line in diagnostics.splitlines() if line.strip()]
    errors = [line for line in lines if re.search(r"error|unexpected|invalid|not supported", line, re.I)]
    return (errors[0] if errors else lines[-1] if lines else "no diagnostics")[:400]


def provider_refused(diagnostics: str) -> bool:
    """Whether the provider answered with an error event (the CLI ran; the account, key or model was refused)."""
    for line in diagnostics.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") in {"error", "turn.failed"}:
            return True
    return False


def classify(diagnostics: str) -> str:
    import test_run_critique_runtime as runtime  # the driver's own classifier, so both agree

    return runtime.provider_failure_classification(diagnostics)[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--force", action="store_true", help="call even if a pass from the last 24 h is cached")
    args = parser.parse_args(argv)
    try:
        model, effort = critic_route()
    except (OSError, ValueError, KeyError) as exc:
        print(f"FATAL: check_critic_access.py: cannot read the shipped critic route from {POLICY}: {exc}",
              file=sys.stderr)
        return 2
    codex, how = codex_cli.resolve()
    version = codex_cli.cli_version(codex) if codex else None
    if not codex or version is None:
        print(f"FATAL: check_critic_access.py: no working Codex CLI ({how if not codex else codex}); every critique "
              f"round needs one. Install or update the ChatGPT app (it bundles the CLI), or set CODEX=/path/to/codex.",
              file=sys.stderr)
        return 2
    codex_home, account = account_fingerprint()
    key = {"codex": codex, "cli_version": version, "login": login_line(codex), "codex_home": codex_home,
           "account_sha256": account, "model": model, "effort": effort}
    path, now = state_file(), _now()
    if not args.force:
        try:
            stamp = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stamp = {}
        if isinstance(stamp, dict) and fresh_pass(stamp, key, now):
            print(f"critic access OK: {model} at {effort} via codex-cli {version} ({key['login']}); "
                  f"checked {stamp['checked_at']}")
            return 0
    passed, diagnostics = call(codex, model, effort)
    if passed:
        write_stamp(path, {**key, "result": "ok", "checked_at": _iso(now)})
        print(f"critic access OK: {model} at {effort} via codex-cli {version} ({key['login']})")
        return 0
    error_class = classify(diagnostics)
    if error_class not in ACCOUNT_CLASSES | RETRY_CLASSES and (ACCOUNT_WORDS.search(diagnostics)
                                                               or provider_refused(diagnostics)):
        error_class = "account_refusal"   # the provider refused this account, key or organisation, not the CLI
    words = provider_words(diagnostics)
    write_stamp(path, {**key, "result": "refused" if error_class in ACCOUNT_CLASSES else "failed",
                       "error_class": error_class, "checked_at": _iso(now)})
    if error_class == "unsupported_model" and cli_outdated(version):
        print(f"FATAL: this host cannot use the critic model {model}: {words} ({error_class}).\n"
              f"       Its Codex CLI is codex-cli {version}, older than {MIN_CLI_FOR_SOL}, the first that offers {model}\n"
              f"       (0.158.0-alpha.2.1 got exactly this refusal). Update the ChatGPT app, which bundles the CLI\n"
              f"       (python3 {HERE / 'codex_cli.py'} --explain shows which CLI is used), then check again:\n"
              f"         python3 {HERE / 'check_critic_access.py'} --force", file=sys.stderr)
        return 3
    if error_class in ACCOUNT_CLASSES:
        print(f"FATAL: this host cannot use the critic model {model}: {words} ({error_class}).\n"
              f"       Every critique round of a new audit runs on {model}, and since 2.7.5 an audit cannot be\n"
              f"       re-pinned off it. This Codex CLI ({codex}, codex-cli {version},\n"
              f"       {key['login']}) must be signed in to an account that offers {model}, which is the user's\n"
              f"       decision: for example an OpenAI API key whose organisation offers the model\n"
              f"         printenv OPENAI_API_KEY | \"{codex}\" login --with-api-key\n"
              f"       or start the audit from a host whose account does. Then check again:\n"
              f"         python3 {HERE / 'check_critic_access.py'} --force", file=sys.stderr)
        return 3
    if error_class in RETRY_CLASSES:
        print(f"FATAL: could not confirm that this host can use {model}: {words} ({error_class}). Nothing was "
              f"started; run the check again (python3 {HERE / 'check_critic_access.py'} --force).", file=sys.stderr)
        return 4
    print(f"FATAL: the Codex CLI rejected the access check itself: {words} ({error_class}). Nothing was started.\n"
          f"       Update the ChatGPT app, which bundles the CLI, or the CLI you set in CODEX (python3\n"
          f"       {HERE / 'codex_cli.py'} --explain shows which is used), then run python3\n"
          f"       {HERE / 'check_critic_access.py'} --force.", file=sys.stderr)
    return 5


if __name__ == "__main__":
    sys.exit(main())
