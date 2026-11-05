#!/usr/bin/env python3
"""refreeze_pkg.py <audit-dir>

Gives pkg/ back the read-only mode intake set (SKILL.md: chmod -R a-w "$AUDIT_DIR/pkg") when the synced folder has
stripped it, and only when every file still matches package_manifest.json. The post-delta archive gate refuses a
writable deposit ("archive package directory is not read-only: pkg/."), which the audit cannot cause: Dropbox strips
the mode (10 of 11 audits on 2026-09-25, finalized ones included), while the bytes still hash to the manifest. Before
2.7.11 every finalize, stage and publication after that refused until someone ran chmod by hand, and stage_release.sh
named the wrong remedy (rebuild the packet and re-run the gates, which on a finalized audit journals a gate event
that stales its receipt).

Modes only: no byte of the deposit is read for anything but its hash, and none is written; nothing else in the audit
is written either (no journal event: on a finalized audit one would stale its receipt). The files are hashed against
package_manifest.json first; any difference (or a symlink, or anything that is not a file or a folder) leaves every
mode as it is and is named here (the archive gate then refuses the deposit as before). After the change the archive
gate's own check (run_post_delta_gates.verify_archive) runs, and if it refuses, every mode is put back.
stage_release.sh, finalize_run.sh and publish_completed.sh run this first (2.7.11).

Exit: 0 nothing to do or done, 1 left as it was (pkg/ differs, or a mode could not be changed), 2 usage.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import stat
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def file_sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def writable(package: pathlib.Path) -> list[pathlib.Path]:
    entries = [package] + sorted(package.rglob("*"))
    return [p for p in entries if not p.is_symlink() and (p.is_dir() or p.is_file())
            and stat.S_IMODE(p.stat().st_mode) & 0o222]


# Symlinks are not hashed here (the archive gate admits one that stays inside pkg/): say so, and the hand remedy.
SYMLINK = ", which refreeze_pkg does not handle"


def difference(audit: pathlib.Path) -> str | None:
    """The first way pkg/ differs from package_manifest.json, or None when every file matches it."""
    try:
        manifest = json.loads((audit / "package_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"package_manifest.json cannot be read ({exc})"
    rows = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(rows, list) or manifest.get("schema_version") != "2.0.0":
        return "package_manifest.json is not the v2 intake manifest"
    declared = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            return "package_manifest.json has a malformed row"
        declared[row["path"]] = row
    package = audit / "pkg"
    seen = set()
    for path in sorted(package.rglob("*")):
        relative = path.relative_to(package).as_posix()
        if path.is_symlink():
            return f"pkg/{relative} is a symlink{SYMLINK}"
        if path.is_dir():
            continue
        if not path.is_file():
            return f"pkg/{relative} is not a regular file"
        row = declared.get(relative)
        if row is None:
            return f"pkg/{relative} is not in package_manifest.json"
        try:
            matches = row.get("size") == path.stat().st_size and row.get("sha256") == file_sha256(path)
        except OSError as exc:
            return f"pkg/{relative} cannot be read ({exc.strerror or exc})"
        if not matches:
            return f"pkg/{relative} no longer matches package_manifest.json"
        seen.add(relative)
    missing = sorted(set(declared) - seen)
    if missing:
        return f"pkg/{missing[0]} is missing"
    return None


def refreeze(audit: pathlib.Path) -> int:
    package = audit / "pkg"
    if package.is_symlink() or not package.is_dir():
        return 0                                   # the archive gate refuses this itself
    entries = writable(package)
    if not entries:
        return 0
    differs = difference(audit)
    if differs:
        print(f"pkg/: {len(entries)} entries are writable, and {differs}: modes left as they are "
              + ("(if the deposit is unchanged, run chmod -R a-w pkg/ by hand)" if differs.endswith(SYMLINK)
                 else "(the archive gate refuses both)"))
        return 1
    try:
        import run_post_delta_gates   # the archive gate's own check, used read-only (loaded before any mode changes)
    except Exception as exc:   # noqa: BLE001
        print(f"pkg/: modes left as they are (the archive gate's check could not be loaded: {exc})")
        return 1
    before = {p: stat.S_IMODE(p.stat().st_mode) for p in entries}
    changed: list[pathlib.Path] = []
    try:
        for path in sorted(entries, key=lambda p: (p.is_dir(), -len(p.parts))):   # files, then folders deepest first
            path.chmod(before[path] & ~0o222)
            changed.append(path)
        detail = run_post_delta_gates.verify_archive(audit)
    except BaseException as exc:   # noqa: BLE001 -- whatever stops the check (an interrupt too), every mode goes back
        restored = 0
        for path in reversed(changed):
            try:
                path.chmod(before[path])
                restored += 1
            except OSError:
                pass
        if not isinstance(exc, Exception):
            raise
        print(f"pkg/: could not be made read-only again ({exc}); "
              + ("modes left as they were" if restored == len(changed) else
                 f"{len(changed) - restored} of {len(changed)} modes could not be put back"))
        return 1
    count = f"{len(changed)} {'entry' if len(changed) == 1 else 'entries'}"
    print(f"pkg/: {count} made read-only again (the synced folder strips intake's mode; "
          f"{detail}, all matching package_manifest.json; modes only)")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0].startswith("-"):
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        return 2
    audit = pathlib.Path(argv[0])
    if not audit.is_dir():
        print(f"refreeze_pkg: no such audit directory: {audit}", file=sys.stderr)
        return 2
    return refreeze(audit.resolve())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
