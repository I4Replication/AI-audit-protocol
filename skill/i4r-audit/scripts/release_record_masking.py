"""Stage check 3d (2.7.6): home paths and e-mail addresses in the STAGED copies of records nobody can reword.

Check 4 (check_release_privacy.py) refuses any home path whose account is not a placeholder and any e-mail address
the byline and the published article do not print. A critic quoting the authors' setwd() call, or a Phase-1 evidence
script naming a deposit path, is model output or hash-bound, so "reword it" had no remedy and the audit could never
stage. As check 3c does for deposit values, the staged copy of such a record (check_deposit_values.record_file) gets
<user-dir> for the account segment and [e-mail address] for the address, and every rewrite is listed in
RELEASE_PATH_NEUTRALISATION.txt with counts, never a value. The audit's files are never opened for writing, and the
comment's own outputs are not records, so a path or address there still refuses at check 4.

The detectors are check_release_privacy's own patterns and its own address rule, read and never changed here: every
post-delta receipt binds that file's bytes (run_post_delta_gates.CODE_PATHS).
"""
from __future__ import annotations

import pathlib
import sys
from typing import Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import check_release_privacy as crp  # noqa: E402
import email_scan  # noqa: E402

USER_DIR_PLACEHOLDER = "<user-dir>"
EMAIL_PLACEHOLDER = "[e-mail address]"


def _refused_email_spans(text: str, label: str) -> list[tuple[int, int]]:
    """The spans of the addresses check 4 refuses in `text`. check_release_privacy reports an address by line and
    by its first letter and domain, so every address on that line with the same first letter and domain is masked:
    an allowed address next to a refused one that shares both is masked too (over-masking a staged record copy is
    harmless; missing one is not)."""
    refused = set()
    for issue in crp._email_issues(text, label):
        line = int(issue.location.rsplit(":", 1)[1])
        shown = issue.detail.split("(", 1)[1].split(")", 1)[0]        # "<first letter>***@<domain>"
        refused.add((line, shown[:1], shown.split("@", 1)[1]))
    spans = []
    for match in email_scan.finditer(crp._EMAIL, text):
        local, domain = match.group(0).lower().rsplit("@", 1)
        if (text.count("\n", 0, match.start()) + 1, local[:1], domain) in refused:
            spans.append(match.span())
    return spans


def neutralise_records(release_root: pathlib.Path, is_record: Callable[[str], bool]) -> list[tuple[str, int, int]]:
    """Rewrite the staged copy of every record under `release_root`; return [(relpath, paths, addresses)]."""
    rewritten: list[tuple[str, int, int]] = []
    for path in sorted(release_root.rglob("*")):
        relative = path.relative_to(release_root).as_posix()
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in crp.TEXT_SUFFIXES \
                or not is_record(relative):
            continue
        data = path.read_bytes()
        try:
            text, encoding = data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            text, encoding = data.decode("latin-1"), "latin-1"
        paths = 0

        def account(match) -> str:
            nonlocal paths
            if crp._PLACEHOLDER.fullmatch(match.group(1)):
                return match.group(0)
            paths += 1
            whole, start, end = match.group(0), match.start(1) - match.start(0), match.end(1) - match.start(0)
            return whole[:start] + USER_DIR_PLACEHOLDER + whole[end:]
        for pattern in (crp._UNIX_HOME, crp._WINDOWS_HOME):
            text = pattern.sub(account, text)
        spans = _refused_email_spans(text, relative)
        for start, end in reversed(spans):
            text = text[:start] + EMAIL_PLACEHOLDER + text[end:]
        if paths or spans:
            path.write_bytes(text.encode(encoding))
            rewritten.append((relative, paths, len(spans)))
    return rewritten


def note(release_root: pathlib.Path, rewritten: list[tuple[str, int, int]]) -> None:
    """Append the rewrites to the release's neutralisation note: file and counts, never a value."""
    if not rewritten:
        return
    target = release_root / crp.NEUTRALISATION_NOTE
    lines = [target.read_text(encoding="utf-8").rstrip("\n"), ""] if target.is_file() else []
    lines += [
        "Records nobody can reword, masked at release staging (check 3d, 2.7.6)",
        "",
        "These staged files are copies of records that are hash-bound or model output (critique records,",
        "Phase-1 auditor reports, work records and evidence scripts). In each, the account segment of a home",
        f"path was replaced with {USER_DIR_PLACEHOLDER!r} and an e-mail address that is neither the byline's nor",
        f"printed in the published article with {EMAIL_PLACEHOLDER!r}. The audit-side originals are unmodified.",
        "",
    ]
    lines += [f"  {name}  ({paths} path(s), {emails} address(es))" for name, paths, emails in rewritten]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    """release_record_masking.py <release> <allowed-email file>...: the staging step (stage_release.sh check 3d)."""
    import check_deposit_values as cdv
    release = pathlib.Path(argv[0])
    crp.allow_emails_from(release / "report" / "front_matter.md")
    for path in argv[1:]:
        crp.allow_emails_from(pathlib.Path(path))
    rewritten = neutralise_records(release, cdv.record_file)
    note(release, rewritten)
    for name, paths, emails in rewritten:
        print(f"  masked in the staged record {name}: {paths} home path(s), {emails} e-mail address(es)")
    if not rewritten:
        print("  ok: no home path or non-public address in staged records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
