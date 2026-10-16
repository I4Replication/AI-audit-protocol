#!/usr/bin/env python3
"""Renumber a finding registry to the published contiguous F1..FN scheme.

``generate_finding_artifacts.py`` requires every final id to match ``F<number>`` and requires the
INCLUDED rows to be contiguous F1..FN. ``build_draft_registry.py`` emits role-scoped ids instead
(``REP-f1_d100_n50_reproduce``, ``TXT-TVD-F2``, ``COD-FIND-FORMULA-01``) and the coordinator carries
those straight through, so every audit needs this conversion exactly once and no tool did it. Doing
it by hand means touching the report's markers, the registry ids, every ``ledger_disposition.target``
and every ``dependencies.finding_ids`` in step, and getting one wrong fails a gate several stages
later with a message that does not point back here.

Numbering follows the order the markers appear in the report, so Appendix A reads top to bottom.
MERGED and EXCLUDED rows are pushed past the last INCLUDED number, which is what the first
production audits did by hand.

    python3 scripts/renumber_findings.py "$AUDIT_DIR" --dry-run
    python3 scripts/renumber_findings.py "$AUDIT_DIR" --apply

``--apply`` also writes ``work/id_map.json`` and, with ``--also``, rewrites the same ids inside any
extra file (the round-N pre-integration snapshots a later diff packet is built from).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
from typing import Any
import sys

MARKER = re.compile(r"<!-- finding:(\S+) -->")
MERGED = re.compile(r"<!-- merged:([^\s>]+)->([^\s>]+) -->")
# One pass over every marker kind at once. A loop of str.replace, however it is ordered, can
# rewrite an id it has already written: mapping TXT-new->F2 while F2->F3 still pending turns the
# real F2 marker and the new one into the same number, silently. Length ordering does not help,
# because the hazard is a NEW id colliding with an OLD one, not a prefix.
ANY_MARKER = re.compile(
    r"<!-- (finding|tested-negative):(\S+) -->|<!-- merged:([^\s>]+)->([^\s>]+) -->")
FINAL_ID = re.compile(r"F[1-9][0-9]*$")


def load_rows(path: pathlib.Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            raise SystemExit(f"{path}:{number}: not valid JSON: {exc}")
    return rows


def build_map(report_text: str, rows: list[dict]) -> dict[str, str]:
    order = MARKER.findall(report_text)
    included = [r["id"] for r in rows if (r.get("ledger_disposition") or {}).get("status") == "INCLUDED"]
    missing = [i for i in included if i not in order]
    extra = [i for i in order if i not in included]
    if missing or extra:
        rejected_but_included = [r["id"] for r in rows if r["id"] in missing
                                 and str(r.get("status") or "").lower() == "rejected"]
        raise SystemExit(
            "report markers and INCLUDED rows do not correspond.\n"
            f"  INCLUDED rows with no marker: {missing}\n"
            f"  markers with no INCLUDED row: {extra}\n"
            + (f"  {rejected_but_included} are status rejected but still INCLUDED: a withdrawal also needs "
               "ledger_disposition EXCLUDED (or MERGED with a target) -- add it to that round's decision and re-apply "
               "integrate_round.py, then renumber.\n" if rejected_but_included else "")
            + "Fix the report or the registry first; renumbering cannot guess the intended order.")
    if len(set(order)) != len(order):
        dupes = sorted({i for i in order if order.count(i) > 1})
        raise SystemExit(f"a finding marker appears more than once in the report: {dupes}")

    mapping = {old: f"F{n}" for n, old in enumerate(order, 1)}
    rest = [r["id"] for r in rows if r["id"] not in mapping]
    for n, old in enumerate(rest, len(order) + 1):
        mapping[old] = f"F{n}"
    return mapping


def rewrite_text(text: str, mapping: dict[str, str]) -> str:
    def _sub(m: re.Match) -> str:
        if m.group(1):                       # finding: or tested-negative:
            return f"<!-- {m.group(1)}:{mapping.get(m.group(2), m.group(2))} -->"
        a, b = m.group(3), m.group(4)        # merged:A->B
        return f"<!-- merged:{mapping.get(a, a)}->{mapping.get(b, b)} -->"
    return ANY_MARKER.sub(_sub, text)


def rewrite_prose(text: str, mapping: dict[str, str]) -> str:
    """Rewrite bare finding ids inside free text, in ONE pass.

    ``ledger_disposition.reason`` is prose that names other findings ("merged into
    REP-f1_d100_n50_reproduce"), and leaving it alone published reasons pointing at ids that no
    longer exist anywhere. Substituting key by key would be worse than doing nothing: with
    F1->F2 and F2->F3 both pending, whichever runs first rewrites what the other has already
    written. One alternation, longest key first so a shorter id cannot claim a prefix of a
    longer one, and word boundaries so F1 does not match inside F12.
    """
    if not text or not mapping:
        return text
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(r"(?<![0-9A-Za-z_-])(" + "|".join(re.escape(k) for k in keys)
                         + r")(?![0-9A-Za-z_-])")
    return pattern.sub(lambda m: mapping[m.group(1)], text)


WRITTEN_PATH = "work/id_map_last_written.json"


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _written(root: pathlib.Path) -> dict:
    path = root / WRITTEN_PATH
    if not path.is_file():
        return {"map_sha256": None, "paths": [], "hashes": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):                      # the first format kept only content hashes
        return {"map_sha256": None, "paths": [], "hashes": value}
    return value


def _audit_relative(root: pathlib.Path, path: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _record_written(root: pathlib.Path, mapping: dict[str, str], written: list[tuple[pathlib.Path, str]],
                    *, reset: bool) -> None:
    """What the last renumber wrote: each path (resolved) and content hash, under that run's map. A swap
    map (F2<->F3) turns every new id into an old key, so re-applying it to a file it already rewrote
    puts the old ids back while looking like a normal rewrite; the late branch skips such a file by
    path even if it was edited since, and by content if it was copied elsewhere."""
    map_sha = _sha(json.dumps(mapping, sort_keys=True))
    old = _written(root)
    keep = not reset and old.get("map_sha256") == map_sha
    state = {"map_sha256": map_sha,
             # relative to the audit, so a copied or moved audit still recognises what this run wrote
             "paths": sorted(set(old["paths"] if keep else []) | {_audit_relative(root, p) for p, _ in written}),
             "hashes": sorted(set(old["hashes"] if keep else []) | {_sha(body) for _, body in written})}
    (root / WRITTEN_PATH).write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")


def rewrite_extra(path: pathlib.Path, mapping: dict[str, str]) -> str:
    """Rewrite an --also file. A report snapshot keeps its prose and rewrites only markers. Other text
    (the round's RESPONSE_notes.md, report/ADJUDICATION.md) rewrites ids where they are unambiguously
    finding references: the markers, the adjudication markers, `### ID` / `### A-ID` headings, whole
    id lists after "finding(s)", "duplicate of", "merged into" or "Sources:", and role-scoped draft
    ids anywhere (they cannot be mistaken for anything else). Other free prose is left alone:
    "Table F3" in a quoted critic result is the paper's appendix, not a finding. Every location is
    rewritten once, from the original text, so a swap map (F2<->F3) cannot be applied twice."""
    text = rewrite_text(path.read_text(encoding="utf-8"), mapping)
    if path.name.startswith("REPORT") or not mapping:
        return text
    tokens: list[str] = []

    def token(new_id: str) -> str:
        tokens.append(new_id)
        return f"\x00{len(tokens) - 1}\x00"

    def adjudication(m: re.Match) -> str:
        new = mapping.get(m.group(2))
        return m.group(0) if new is None else f"<!-- {m.group(1) or ''}adjudication:{token(new)}{m.group(3) or ''} -->"
    text = re.sub(r"<!-- (withdrawn-)?adjudication:(\S+?)(:round\d+)? -->", adjudication, text)
    keys = "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
    core = r"(?:" + keys + r")(?![0-9A-Za-z_-]|:\S)"
    one = r"(?<![0-9A-Za-z_-])" + core   # the first id of a list follows its anchor ("A-", a space)

    def ids_in(m: re.Match) -> str:
        return m.group(1) + re.sub(r"(?<![0-9A-Za-z_])" + core, lambda i: token(mapping[i.group(0)]), m.group(2))
    listed = r"(" + core + r"(?:\s*(?:,|;|&|\band\b)\s*" + one + r")*)"
    text = re.sub(r"(?m)(^###\s+(?:A-)?)" + listed, ids_in, text)
    text = re.sub(r"(?i)(\b(?:findings?|duplicate of|merged into|sources?:)\s*)" + listed, ids_in, text)
    # integrate_round's own lines: the conflicts list ("- critic_stats C1 on F3, F4: ...") and the
    # withdrawal reason, which copies ledger_disposition.reason -- rewritten everywhere, as it is in
    # the registry, so the log and the registry keep naming the same finding.
    text = re.sub(r"(?m)(^- \S+ \S* ?on )" + listed, ids_in, text)

    def reason(m: re.Match) -> str:
        return m.group(1) + re.sub(r"(?<![0-9A-Za-z_])" + core, lambda i: token(mapping[i.group(0)]), m.group(2))
    text = re.sub(r"(?im)^(.*?\bwithdrawn \([A-Za-z]+\): )(.*)$", reason, text)
    # The coordinator's own entries (adjudication_guide): "## A3 — F3" headings and "Sources:" lines,
    # which name registry rows anywhere on the line ("... draft registry row F3").
    # "## A3 — F3" only when the heading is nothing but the id list ("## A2 — F1 score misreported"
    # is a short-issue title); on Sources: lines, not ids that label the paper's own exhibits.
    text = re.sub(r"(?m)(^##\s+A\d+\s+[—–-]+\s+)" + listed + r"(?=\s*$)", ids_in, text)

    # a list/range continues only with items shaped like its first one: after "Table 2," an F3 is a finding
    labels = r"(?i:\b(?:tables?|tabs?\.?|fig(?:ure)?s?\.?|appendix(?:es)?|panels?|exhibits?))\s+"
    sep = r"\s*(?:,\s*(?:and|&)|,|and|&|-|–|—|through|to)\s*"   # Oxford-comma and "through" lists too
    exhibit = re.compile(labels + r"(?:(?P<p>[A-Za-z])\d+[A-Za-z]?(?:" + sep + r"(?P=p)\d+[A-Za-z]?)*" + sep + r")?$")

    def sources(m: re.Match) -> str:
        line = m.group(2)

        def one_id(i: re.Match) -> str:
            # An id that labels ("Table F3") or continues a same-prefix list/range of the paper's exhibits
            # ("Tables F2 and F3") stays; after "Table A3," or "Table 2," an F3 is a finding.
            found = exhibit.search(line[:i.start()])
            if found and (found.group("p") is None or found.group("p") == i.group(0)[:1]):
                return i.group(0)
            return token(mapping[i.group(0)])
        return m.group(1) + re.sub(r"(?<![0-9A-Za-z_])" + core, one_id, line)
    text = re.sub(r"(?im)^(\s*[-*]?\s*\**sources?\**:\**\s*)(.*)$", sources, text)
    drafts = [k for k in mapping if not FINAL_ID.fullmatch(k)]
    if drafts:
        draft = r"(?<![0-9A-Za-z_-])(?:" + "|".join(re.escape(k) for k in sorted(drafts, key=len, reverse=True)) \
            + r")(?![0-9A-Za-z_-]|:\S)"
        text = re.sub(draft, lambda m: token(mapping[m.group(0)]), text)
    return re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m.group(1))], text)


def bound_report_candidates(root: pathlib.Path, relative: str, reviewed: list[pathlib.Path]) -> list[tuple[str, Any]]:
    """Every copy of a before-report a frozen packet may have bound, as ("file", path) or ("text", str): the reviewed
    snapshot replayed through each renumber recorded since (exact history), or through the no-history map of an
    earlier version (legacy), and the post-delta gates' payload copies."""
    history_path = root / "work/id_map_history.jsonl"
    maps = [json.loads(line).get("map") or {} for line in
            (history_path.read_text(encoding="utf-8").splitlines() if history_path.is_file() else []) if line.strip()]
    legacy = []
    for name in ("work/id_map_last.json", "work/id_map.json"):
        try:
            value = json.loads((root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            legacy.append(value)
    out: list[tuple[str, Any]] = []
    for snapshot in reviewed:
        original = snapshot.read_text(encoding="utf-8")
        out.append(("text", original))
        # the snapshot is already in the ids of renumbers made before its round ran: replay every tail of the
        # history (maps[k:] for each k), not only the whole list
        for start in range(len(maps)):
            text = original
            for mapping in maps[start:]:
                text = rewrite_text(text, mapping)
                out.append(("text", text))
        for mapping in legacy:
            out.append(("text", rewrite_text(snapshot.read_text(encoding="utf-8"), mapping)))
    out.extend(("file", path) for path in sorted(root.glob(f"critique/round_*/post_delta/*/payload/{relative}")))
    return out


def rewrite_rows(rows: list[dict], mapping: dict[str, str]) -> list[dict]:
    for row in rows:
        row["id"] = mapping.get(row["id"], row["id"])
        disposition = row.get("ledger_disposition") or {}
        if disposition.get("target"):
            disposition["target"] = mapping.get(disposition["target"], disposition["target"])
        if isinstance(disposition.get("reason"), str):
            disposition["reason"] = rewrite_prose(disposition["reason"], mapping)
        deps = row.get("dependencies") or {}
        if deps.get("finding_ids"):
            deps["finding_ids"] = [mapping.get(x, x) for x in deps["finding_ids"]]
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audit_dir", type=pathlib.Path)
    ap.add_argument("--report", default="report/REPORT_final.md")
    ap.add_argument("--registry", default="work/findings.jsonl")
    ap.add_argument("--also", action="append", default=[],
                    help="extra file to rewrite with the same map (repeatable)")
    ap.add_argument("--late-also", action="store_true",
                    help="the registry is already renumbered: apply the LAST renumber's map to the --also "
                         "files that run left out (never implied; a withdrawal with nothing to renumber must "
                         "not re-apply an older map)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    root = args.audit_dir.resolve()
    report = root / args.report
    registry = root / args.registry
    for path in (report, registry):
        if not path.is_file():
            print(f"FATAL: not found: {path}", file=sys.stderr)
            return 2

    first_history = not (root / "work/id_map_history.jsonl").is_file()
    # an earlier renumber by a version without history moved an F<n> id (the pre-round-1 draft->F map, or an
    # identity map, is not one: critic verdicts were already in today's ids)
    import later_disputes
    had_last_map = later_disputes._legacy_renumbered(root)
    outside = []
    for extra in args.also:
        target = (root / extra).resolve()
        if re.fullmatch(r"findings\.before_round\d+_integration\.jsonl", target.name):
            print(f"FATAL: --also {extra} is integrate_round's pre-integration backup, which must keep the ids "
                  "its round was reviewed under (make_settled translates it through the renumber history)",
                  file=sys.stderr)
            return 2
        if target in (report.resolve(), registry.resolve()):
            print(f"FATAL: --also {extra} is the canonical report or registry, which the renumber itself "
                  "rewrites; list only snapshots, notes and logs", file=sys.stderr)
            return 2
        try:
            target.relative_to(root)
        except ValueError:
            outside.append(extra)
    if outside:
        # Checked before anything is written: a symlinked or ../ path used to fail after the report and
        # registry were rewritten, leaving the remaining --also files and the renumber record undone.
        print("FATAL: --also paths must resolve inside the audit directory (symlinks are followed): "
              + ", ".join(outside), file=sys.stderr)
        return 2
    # A before-file that an already-run round's frozen diff packet binds must keep its bytes: finalize
    # re-hashes it, and make_diff_packet translates its ids through work/id_map_history.jsonl instead.
    bound = {}
    for packet_path in root.glob("critique/round_*/snapshots/*/source/context/DIFF_MANIFEST.json"):
        try:
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key in ("before_report", "before_findings"):
            relative = (packet.get("input_paths") or {}).get(key)
            if isinstance(relative, str) and (root / relative).is_file():
                # by file identity: APFS is case-insensitive and resolve() keeps the typed case
                stat = (root / relative).stat()
                bound[(stat.st_dev, stat.st_ino)] = packet.get("round")
    def _identity(extra: str):
        path = root / extra
        return (path.stat().st_dev, path.stat().st_ino) if path.is_file() else None
    frozen = [f"{extra} (bound by round {bound[_identity(extra)]}'s snapshot)"
              for extra in args.also if _identity(extra) in bound]
    if frozen:
        print("FATAL: leave these out of --also -- a round that has been prepared or run froze a diff packet that "
              "binds their bytes, and make_diff_packet translates their ids when it reads them: " + ", ".join(frozen),
              file=sys.stderr)
        return 2

    text = report.read_text(encoding="utf-8")
    rows = load_rows(registry)
    # "F-shaped" is not "done": a registry can be F1,F2,F7 or carry duplicates, which is exactly
    # the state this tool exists to repair. Short-circuit only on a genuine contiguous F1..FN with
    # the INCLUDED rows first, and only when nothing extra was asked for.
    numbered = [r["id"] for r in rows if FINAL_ID.fullmatch(r["id"])]
    contiguous = sorted(int(i[1:]) for i in numbered) == list(range(1, len(rows) + 1))
    # ...and the ids must already be the ones build_map would assign: INCLUDED rows in report-marker
    # order, then the rest. An adjudication that turns F2 into MERGED leaves F1..F3 contiguous but
    # the INCLUDED rows F1,F3, which the projection refuses.
    already = len(numbered) == len(rows) and contiguous and all(
        old == new for old, new in build_map(text, rows).items())
    if already and not args.late_also:
        print(f"already renumbered: {len(rows)} contiguous F1..F{len(rows)} ids; nothing to do"
              + (" (the --also files were left alone; pass --late-also only to finish a renumber whose "
                 "--also files were forgotten)" if args.also else ""))
        return 0
    if args.late_also and not already:
        print("FATAL: --late-also is for a registry that is already renumbered; this one still needs a "
              "renumber, so run --apply (with --also) instead.", file=sys.stderr)
        return 1

    if not args.late_also:
        # Every round's response notes ship with the release and name findings by id; earlier renumbers moved
        # them to today's ids, so this map is right for all of them (a round-N withdrawal otherwise leaves the
        # earlier rounds' notes filing verdicts under ids that now name other findings).
        listed = {(root / e).resolve() for e in args.also}
        args.also = list(args.also) + [str(n.relative_to(root)) for n in sorted(root.glob("critique/round_*/RESPONSE_notes.md"))
                                        if n.resolve() not in listed]
    not_files = [extra for extra in args.also if (root / extra).exists() and not (root / extra).is_file()]
    if not_files:
        print(f"FATAL: --also takes files, not directories: {not_files}; nothing was written", file=sys.stderr)
        return 1
    id_map_path = root / "work/id_map.json"
    if already and args.late_also:
        # A plain --apply already ran; the report and registry carry final ids and build_map would
        # now produce the identity map. That used to be written over work/id_map.json and applied
        # to the extras as a no-op that printed "rewrote": the round-1 snapshots stayed on the old
        # ids while the tool reported success. Use the mapping the earlier run persisted.
        # The --also files still carry the ids from just before the LAST apply, so they take that
        # run's own map. The composed id_map.json mixes generations: after a second renumber an id
        # such as F3 is both an old key and a newer value, and applying it wrote wrong ids.
        last_map_path = root / "work/id_map_last.json"
        source = last_map_path if last_map_path.is_file() else id_map_path
        if not source.is_file():
            print("FATAL: the registry is already renumbered but work/id_map.json is missing, so the\n"
                  "       original ids cannot be recovered for the --also files. Restore the map or\n"
                  "       rewrite those files by hand.", file=sys.stderr)
            return 1
        mapping = json.loads(source.read_text(encoding="utf-8"))
        changed = {k: v for k, v in mapping.items() if k != v}
        print(f"registry already renumbered; applying the persisted map ({len(changed)} ids) to --also files only")
        if args.dry_run:
            return 0
        rewrote = 0
        state = _written(root)
        same_map = state.get("map_sha256") in (None, _sha(json.dumps(mapping, sort_keys=True)))
        done_paths = set(state["paths"]) if same_map else set()
        done_hashes = set(state["hashes"])
        new_bodies = []
        seen_paths: set[str] = set()
        unwritable = [extra for extra in args.also if (root / extra).is_file() and not os.access(root / extra, os.W_OK)]
        written_record = root / WRITTEN_PATH
        if written_record.exists() and not os.access(written_record, os.W_OK):
            unwritable.append(WRITTEN_PATH)
        if not os.access(root / "work", os.W_OK):
            unwritable.append("work/")
        if unwritable:
            print("FATAL: not writable, so nothing was rewritten: " + ", ".join(unwritable)
                  + ". Make them writable (chmod u+w <file>) and re-run the same command.", file=sys.stderr)
            return 1
        for extra in args.also:
            path = root / extra
            resolved = str(path.resolve())
            if resolved in seen_paths:
                print(f"  skipped (given twice): {extra}")
                continue
            seen_paths.add(resolved)
            if not path.is_file():
                print(f"  skipped (missing): {extra}")
                continue
            # recorded relative to the audit (older records: absolute)
            if resolved in done_paths or _audit_relative(root, path) in done_paths \
                    or _sha(path.read_text(encoding="utf-8")) in done_hashes:
                print(f"  skipped (already rewritten by the last renumber): {extra}")
                continue
            if path.suffix == ".jsonl":
                body = "\n".join(json.dumps(r, ensure_ascii=False)
                                 for r in rewrite_rows(load_rows(path), mapping)) + "\n"
            else:
                body = rewrite_extra(path, mapping)
            if body == path.read_text(encoding="utf-8"):
                print(f"  unchanged: {extra} (no old ids present)")
                continue
            path.write_text(body, encoding="utf-8")
            new_bodies.append((path, body))
            print(f"  rewrote {extra}")
            rewrote += 1
        if new_bodies:
            _record_written(root, mapping, new_bodies, reset=False)
        return 0

    mapping = build_map(text, rows)
    changed = {k: v for k, v in mapping.items() if k != v}
    print(f"{len(rows)} rows; {len(changed)} ids to change")
    for old, new in mapping.items():
        print(f"  {new:>5s} <- {old}")
    if args.dry_run:
        return 0

    # Render everything before writing anything. A failure between the two writes leaves the
    # report renumbered and the registry not, which no rerun can repair: the tool would then see
    # markers that match no row and refuse.
    new_report = rewrite_text(text, mapping)
    new_registry = "\n".join(
        json.dumps(r, ensure_ascii=False) for r in rewrite_rows(rows, mapping)) + "\n"
    extras: list[tuple[pathlib.Path, str]] = []
    for extra in dict.fromkeys(str((root / e).resolve()) for e in args.also):
        path = pathlib.Path(extra)
        if not path.is_file():
            print(f"  skipped (missing): {extra}")
            continue
        if path.suffix == ".jsonl":
            extras.append((path, "\n".join(
                json.dumps(r, ensure_ascii=False)
                for r in rewrite_rows(load_rows(path), mapping)) + "\n"))
        else:
            extras.append((path, rewrite_extra(path, mapping)))

    # all or nothing: an unwritable --also file (a step-1 copy taken from a read-only snapshot) used to fail after
    # the report, registry and id maps were already rewritten, leaving no history line and no re-integration guard
    work_files = [root / name for name in ("work/id_map.json", "work/id_map_last.json", WRITTEN_PATH,
                                           "work/renumber_state.json", "work/id_map_history.jsonl")]
    unwritable = [str(path) for path in [report, registry] + [path for path, _ in extras]
                  + [path for path in work_files if path.exists()] if not os.access(path, os.W_OK)]
    if not os.access(root / "work", os.W_OK):
        unwritable.append(str(root / "work"))
    if unwritable:
        print("FATAL: not writable, so nothing was renumbered: " + ", ".join(unwritable)
              + ". Make them writable (chmod u+w <file>) and re-run the same command.", file=sys.stderr)
        return 1
    report.write_text(new_report, encoding="utf-8")
    registry.write_text(new_registry, encoding="utf-8")
    if id_map_path.is_file():
        # Compose with an earlier map so a later --also run can still translate the oldest ids.
        earlier = json.loads(id_map_path.read_text(encoding="utf-8"))
        composed = {old_id: mapping.get(new_id, new_id) for old_id, new_id in earlier.items()}
        composed.update({k: v for k, v in mapping.items() if k not in composed})
        mapping_to_persist = composed
    else:
        mapping_to_persist = mapping
    id_map_path.write_text(json.dumps(mapping_to_persist, indent=1) + "\n", encoding="utf-8")
    (root / "work/id_map_last.json").write_text(json.dumps(mapping, indent=1) + "\n", encoding="utf-8")
    for path, body in extras:
        path.write_text(body, encoding="utf-8")
        print(f"  rewrote {path.relative_to(root)}")
    _record_written(root, mapping, extras, reset=True)
    # Which critique invocations had already run when the ids changed: integrate_round refuses to
    # (re)integrate those, since their verdicts and the decisions for them carry the old ids.
    ran = {}
    for status in sorted(root.glob("critique/round_*/ROUND_STATUS.json")):
        pointer = status.parent / "CURRENT_INVOCATION"
        if pointer.is_file():
            ran[status.parent.name] = pointer.read_text(encoding="utf-8").strip()
    (root / "work/renumber_state.json").write_text(
        json.dumps({"rounds_run_before_renumber": ran}, indent=1) + "\n", encoding="utf-8")
    # Every renumber, in order, with the rounds that had already run: make_settled translates a round's
    # snapshot ids to today's ids exactly, instead of guessing whether a moved text is a renumber.
    with (root / "work/id_map_history.jsonl").open("a", encoding="utf-8") as history:
        # round -> invocation: a map applies to a round's backup only if that backup was taken for the
        # same invocation (a round run again after the renumber was reviewed under the new ids)
        entry = {"map": mapping, "rounds_run_before_renumber": ran}
        if first_history and had_last_map:
            entry["legacy_prior"] = True     # an earlier renumber by a version that kept no history
        history.write(json.dumps(entry) + "\n")

    leftover = [m for m in MARKER.findall(report.read_text(encoding="utf-8"))
                if not FINAL_ID.fullmatch(m)]
    if leftover:
        print(f"FATAL: markers left unrenumbered: {leftover}", file=sys.stderr)
        return 1
    print(f"applied; map written to work/id_map.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
