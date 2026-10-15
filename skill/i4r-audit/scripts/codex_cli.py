#!/usr/bin/env python3
"""Find the Codex CLI the skill runs (2.6.10).

    python3 scripts/codex_cli.py            # prints the chosen binary, exit 0; exit 1 if none works
    python3 scripts/codex_cli.py --explain  # every candidate, its version, and which one was chosen

An explicit choice always wins: ``CODEX``, then ``I4R_CODEX_BIN`` (run_critique.sh and
capture_environment.sh read them in that order). Otherwise the skill uses the NEWEST working CLI
among the known install locations and every ``codex`` on ``PATH``. A candidate is working when it is
an executable file whose ``--version`` prints ``codex-cli <version>``. Ties go to the earlier
candidate, so the ChatGPT app's bundled CLI (which the app keeps up to date) is preferred over
another copy of the same version.

Why this exists: the ChatGPT app update of 2026-09-28 moved its bundled CLI from
``Contents/Resources/codex`` to ``Contents/Resources/codex-cli/bin/codex``. Every script had the old
path as a literal default, so every critique round on an updated Mac stopped with "Codex executable
is not executable" until the operator set ``CODEX`` by hand. A Linux or WSL host, where the CLI is a
``codex`` on ``PATH``, had no working default at all.

``I4R_CODEX_KNOWN_PATHS`` (colon-separated) replaces the list of known install locations; tests and
diagnosis only. Nothing here makes a provider call: ``--version`` is local.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys
from typing import Iterable, Sequence

# The ChatGPT app's bundled CLI, newest layout first; then the pre-2026-09-28 layout.
KNOWN_PATHS: tuple[str, ...] = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    "~/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    "/Applications/ChatGPT.app/Contents/Resources/codex",
    "~/Applications/ChatGPT.app/Contents/Resources/codex",
)
VERSION_LINE = re.compile(r"\bcodex-cli\s+(\S+)")
NUMERIC = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")
VERSION_TIMEOUT_SECONDS = 15


def version_key(version: str) -> tuple:
    """Order versions: 0.158.0 > 0.158.0-alpha.2.1 > 0.157.9; an unparseable one sorts lowest."""
    match = NUMERIC.match(version)
    if not match:
        return (-1,)
    major, minor, patch, pre = match.groups()
    if pre is None:
        return (int(major), int(minor), int(patch), 1)
    parts = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split("."))
    return (int(major), int(minor), int(patch), 0, parts)


def cli_version(path: str) -> str | None:
    """The version a candidate reports, or None when it is not a working Codex CLI."""
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True,
                                timeout=VERSION_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = VERSION_LINE.search(result.stdout or "")
    return match.group(1) if match else None


def known_paths(env: dict[str, str]) -> list[str]:
    raw = env.get("I4R_CODEX_KNOWN_PATHS")
    entries: Iterable[str] = raw.split(":") if raw is not None else KNOWN_PATHS
    return [os.path.expanduser(entry) for entry in entries if entry]


def path_candidates(env: dict[str, str]) -> list[str]:
    found = []
    for directory in (env.get("PATH") or "").split(os.pathsep):
        if directory:
            found.append(os.path.join(directory, "codex"))
    return found


def candidates(env: dict[str, str]) -> list[str]:
    """Known locations, then every codex on PATH; each real file once, in that order."""
    ordered, seen = [], set()
    for path in [*known_paths(env), *path_candidates(env)]:
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        ordered.append(path)
    return ordered


def survey(env: dict[str, str] | None = None) -> list[tuple[str, str | None]]:
    env = dict(os.environ if env is None else env)
    return [(path, cli_version(path)) for path in candidates(env)]


def resolve(env: dict[str, str] | None = None) -> tuple[str | None, str]:
    """(binary, how it was chosen). An explicit CODEX or I4R_CODEX_BIN is returned as given, even when
    it does not exist, so the caller's own check reports the operator's typo rather than silently
    running another CLI."""
    env = dict(os.environ if env is None else env)
    for name in ("CODEX", "I4R_CODEX_BIN"):
        if env.get(name):
            return env[name], f"set by {name}"
    working = [(path, version) for path, version in survey(env) if version is not None]
    if not working:
        return None, "no working Codex CLI found"
    best = max(range(len(working)), key=lambda index: (version_key(working[index][1]), -index))
    path, version = working[best]
    return path, f"newest working CLI (codex-cli {version})"


def searched(env: dict[str, str] | None = None) -> list[str]:
    env = dict(os.environ if env is None else env)
    return [*known_paths(env), "every codex on PATH"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--explain", action="store_true", help="list every candidate and the choice")
    args = parser.parse_args(argv)
    path, how = resolve()
    if args.explain:
        for candidate, version in survey():
            print(f"  {'*' if candidate == path else ' '} {candidate}: "
                  f"{'codex-cli ' + version if version else 'not a working Codex CLI'}")
        print(f"chosen: {path or '(none)'} -- {how}")
        return 0 if path else 1
    if not path:
        print("no working Codex CLI found; searched: " + "; ".join(searched()) +
              ". Install or update the ChatGPT app (it bundles the CLI), or set CODEX=/path/to/codex.",
              file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
