#!/bin/bash
# reserve_paper.sh claim|release|status|list|--selftest [paper-dir] [--force]
#
# Paper reservations for a team auditing one shared Dropbox tree concurrently. A reservation is a
# JSON file in <discipline-root>/reservations/, named for the journal and paper. The tree is synced
# by Dropbox, NOT a lock server, so this is advisory locking with the two failure modes that setup
# has, both handled:
#
#   RACE   two people claim within one sync window. Dropbox keeps one file and writes the other as
#          "<name> (conflicted copy ...)". After writing a claim we wait a grace period
#          (I4R_RESERVE_GRACE, default 15s) and re-scan: if a conflict copy for the same paper
#          exists, the EARLIER claimed_at wins and the later claimant backs off automatically.
#   STALE  someone claims and never finishes. `list` marks reservations older than
#          I4R_RESERVE_STALE_DAYS (default 7) with no completed report as STALE; release them with
#          `release <paper-dir> --force` after checking with the owner.
#
# publish_completed.sh releases the reservation automatically when the finished audit is filed.
set -uo pipefail
CMD="${1:?usage: reserve_paper.sh claim|release|status|list|--selftest [paper-dir]}"

OWNER="${I4R_OWNER:-$(id -un)@$(hostname -s)}"
. "$(cd "$(dirname "$0")" && pwd -P)/tree_layout.sh"
GRACE="${I4R_RESERVE_GRACE:-15}"
STALE_DAYS="${I4R_RESERVE_STALE_DAYS:-7}"

legacy_slug_of () {  # the name every version before 2.6.4 used (plain names still use it)
  local P J
  P="$(basename "$1")"; J="$(basename "$(dirname "$1")")"
  printf '%s__%s' "$J" "$P" | tr -cs 'A-Za-z0-9._-' '_' | cut -c1-180
}
slug_of () {  # <paper-dir> [hash] -> journal__paper, filesystem-safe ("hash": always add the name hash)
  local P J
  P="$(basename "$1")"; J="$(basename "$(dirname "$1")")"
  # Decided entirely in python so the name does not depend on the operator's locale (bash's [A-Za-z] ranges and
  # ${#var} follow the collation/charset, so a UTF-8 shell and a C shell named one paper differently).
  # One name per paper whatever the Unicode form typed (APFS treats NFC and NFD spellings as the same folder).
  # A name with characters the sanitising drops (accents, punctuation) or longer than the cut could collide with
  # another paper's ("Etude" and "Ütude" both became "tude"): those get a short hash of the real name. Plain ASCII
  # names keep exactly the legacy (pre-2.6.4) name.
  python3 -c '
import hashlib, re, sys, unicodedata
raw = unicodedata.normalize("NFC", sys.argv[1])
safe = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9._-]+", "_", raw))[:180]   # as tr -cs ... "_" squeezes
if sys.argv[2] == "hash" or not re.fullmatch(r"[A-Za-z0-9._-]*", raw) or len(raw.encode("utf-8")) > 180:
    safe = safe[:170] + "-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
print(safe, end="")' "$(printf '%s__%s' "$J" "$P")" "${2:-}"
}
# does reservation file $1 record the paper $2? Journal and paper folder compared in NFC (the rest of the recorded
# path is the claiming machine's, and a relative/absolute spelling differs)
records_paper () {  # <file> <paper-dir>
  python3 -c '
import json, os, sys, unicodedata
try:
    recorded = str(json.load(open(sys.argv[1])).get("paper") or "")
except Exception:
    sys.exit(1)
def tail(path):
    parts = os.path.normpath(unicodedata.normalize("NFC", path)).split("/")[-2:]
    # a claim recorded as "." or "smith_2020" (typed relative by an earlier version), "../x" or with no path at all
    # does not name its journal: unknown, which counts as this paper (never a reason to refuse its own release)
    return None if len(parts) < 2 or any(part in ("", ".", "..") for part in parts) else parts
mine, theirs = tail(sys.argv[2]), tail(recorded)
# APFS ignores case: "data/aer/smith2020" typed by hand is the same folder as data/AER/Smith2020
mine, theirs = [None if t is None else [part.casefold() for part in t] for t in (mine, theirs)]
sys.exit(0 if mine is None or theirs is None or mine == theirs else 1)' "$1" "$2"
}
# the reservation file for a paper: its current name, or a claim an earlier version wrote under the legacy name
# for the same journal and paper (typed in either Unicode form), so an upgrade does not orphan active claims
reservation_file () {  # <root> <paper-dir>
  local current legacy form hashed
  current="$1/reservations/$(slug_of "$2").json"
  # a claim already filed under this paper's hashed name (taken while a colliding paper held the plain name) stays
  # this paper's claim after that other claim is released
  hashed="$1/reservations/$(slug_of "$2" hash).json"
  if [ "$hashed" != "$current" ] && [ -f "$hashed" ] && records_paper "$hashed" "$2"; then
    printf '%s' "$hashed"; return 0
  fi
  # two plain names can sanitise alike (Smith_2020 and Smith__2020): if the file records ANOTHER paper, this
  # paper's claim lives under its hashed name
  if [ -f "$current" ] && python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$current" 2>/dev/null \
      && ! records_paper "$current" "$2"; then
    printf '%s' "$1/reservations/$(slug_of "$2" hash).json"; return 0
  fi
  if [ ! -f "$current" ]; then
    for form in NFC NFD; do
      legacy="$1/reservations/$(legacy_slug_of "$(python3 -c 'import sys, unicodedata
print(unicodedata.normalize(sys.argv[1], sys.argv[2]), end="")' "$form" "$2")").json"
      if [ "$legacy" != "$current" ] && [ -f "$legacy" ] && records_paper "$legacy" "$2"; then
        printf '%s' "$legacy"; return 0
      fi
    done
  fi
  printf '%s' "$current"
}
# <existing dir> -> its absolute path with the journal and paper folder names as stored on disk (APFS ignores case
# and Unicode form, so "data/aer/étude" typed by hand is data/AER/Étude; the name and hash must come from the latter)
on_disk_dir () {
  python3 -c 'import os, sys
path = os.path.abspath(sys.argv[1])
parts = path.split("/")
for i in (len(parts) - 2, len(parts) - 1):
    if i < 1:
        continue
    parent = "/".join(parts[:i]) or "/"
    here = os.path.join(parent, parts[i])
    try:
        names = os.listdir(parent)
    except OSError:
        continue
    if parts[i] in names:
        continue
    for name in names:
        try:
            if os.path.samefile(os.path.join(parent, name), here):
                parts[i] = name
                break
        except OSError:
            pass
print("/".join(parts), end="")' "$1"
}
root_of () { i4r_root_of_paper "$1"; }   # data/<J>/<P> -> root (2.5.0); <J>/<P> -> root (legacy)

now_iso () { date -u +%Y-%m-%dT%H:%M:%SZ; }

claim () {
  local DIR="$1"; [ -d "$DIR" ] || { echo "FATAL: no such paper dir: $DIR"; return 1; }
  DIR="$(on_disk_dir "$(cd "$DIR" && pwd -P)")"   # the record holds an absolute path (INSTALL), whatever the caller typed
  local ROOT SLUG RES F
  ROOT="$(root_of "$DIR")"
  RES="$ROOT/reservations"; mkdir -p "$RES"; F="$(reservation_file "$ROOT" "$DIR")"; SLUG="$(basename "$F" .json)"
  # already completed?
  if i4r_already_published "$ROOT" "$(basename "$(dirname "$DIR")")" "$(basename "$DIR")"; then
    echo "REFUSED: this paper is already published (comments/ or metadata/, or the legacy Completed reports/)"
    echo "         To correct a published comment, first move both published folders aside (INSTALL.md,"
    echo "         'Correcting a published comment', step 1), then claim again."
    return 1
  fi
  # existing claim or conflict copies?
  local CONFLICTS
  CONFLICTS=$(find "$RES" -name "${SLUG} (*conflicted copy*" 2>/dev/null)
  if [ -n "$CONFLICTS" ]; then
    # sync may have left OUR winning claim only in a conflict copy (after our own grace window): the earliest claim
    # across the main file and the copies wins; if it is ours, put it back under the main name and carry on
    if python3 - "$RES" "$SLUG" "$F" "$OWNER" <<'PY'
import json, os, pathlib, sys
res, slug, main, owner = sys.argv[1:5]
files = ([pathlib.Path(main)] if os.path.exists(main) else []) + list(pathlib.Path(res).glob(slug + " (*conflicted copy*"))
claims = []
for f in files:
    try:
        d = json.load(open(f))
        claims.append((str(d["claimed_at"]), str(d.get("owner") or ""), f))
    except Exception:
        sys.exit(1)
if not claims:
    sys.exit(1)
winner = min(claims, key=lambda c: (c[0], c[1]))
if winner[1] != owner:
    sys.exit(1)
if winner[2] != pathlib.Path(main):
    displaced = [c for c in claims if c[2] == pathlib.Path(main) and c[1] != owner]
    os.replace(winner[2], main)
    for at, other, _ in displaced:
        print(f"WARNING: your earlier claim replaced {other}'s later claim (claimed {at}) on this paper; "
              f"tell {other} they do not hold it")
sys.exit(0)
PY
    then
      CONFLICTS=$(find "$RES" -name "${SLUG} (*conflicted copy*" 2>/dev/null)
      if [ -n "$CONFLICTS" ]; then
        echo "note: your earliest claim holds; later competing copies remain (ask their owners to delete):"
        echo "$CONFLICTS" | sed 's/^/  /'
      fi
    else
      python3 - "$RES" "$SLUG" "$F" <<'PY'
import json, os, pathlib, sys
res, slug, main = sys.argv[1:4]
files = ([pathlib.Path(main)] if os.path.exists(main) else []) + list(pathlib.Path(res).glob(slug + " (*conflicted copy*"))
claims = []
for f in files:
    try:
        d = json.load(open(f))
        claims.append((str(d["claimed_at"]), str(d.get("owner") or "?"), f))
    except Exception:
        claims.append(("~unreadable", "?", f))
winner = min(claims, key=lambda c: (c[0], c[1]))
print(f"REFUSED: this paper is held by {winner[1]} since {winner[0]} ({winner[2].name}); the earliest claimed_at wins.")
later = [c for c in claims if c is not winner]
if later:
    print("  Later competing claim file(s) -- their owners should delete them:")
    for at, o, f in later:
        print(f"    {f}  ({o}, {at})")
PY
      return 1
    fi
  fi
  if [ -f "$F" ]; then
    local WHO; WHO=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get('owner','?'),'since',d.get('claimed_at','?'))" "$F" 2>/dev/null || echo "unreadable claim")
    if python3 -c "import json,sys;d=json.load(open(sys.argv[1]));exit(0 if d.get('owner')==sys.argv[2] else 1)" "$F" "$OWNER" 2>/dev/null; then
      echo "ok: already yours ($WHO)"; return 0
    fi
    echo "REFUSED: reserved by $WHO"; return 1
  fi
  python3 - "$F" "$OWNER" "$DIR" <<'PY'
import datetime, json, os, sys
p=sys.argv[1]
try:
    fd=os.open(p, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o644)
except FileExistsError:
    print("REFUSED: another local process created the reservation first")
    raise SystemExit(3)
with os.fdopen(fd,"w") as fh:
    json.dump({"owner": sys.argv[2], "paper": sys.argv[3],
               "claimed_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "status": "active"}, fh, indent=2)
    fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
PY
  [ $? -eq 0 ] || return 1
  # our own claim, read back BEFORE the grace window: during it sync may keep the rival's file as $F and rename ours
  local MY_AT; MY_AT=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['claimed_at'])" "$F")
  # grace window: let Dropbox surface a same-window competing claim as a conflict copy
  [ "$GRACE" -gt 0 ] && { echo "claim written; waiting ${GRACE}s for sync conflicts ..."; sleep "$GRACE"; }
  CONFLICTS=$(find "$RES" -name "${SLUG} (*conflicted copy*" 2>/dev/null)
  if [ -n "$CONFLICTS" ]; then
    # the earliest claimed_at across $F and every copy wins (owner breaks a tie), whichever file sync kept as $F; a
    # loser removes only the file(s) holding its own claim
    local VERDICT
    VERDICT=$(python3 - "$RES" "$SLUG" "$F" "$OWNER" "$MY_AT" <<'PY'
import json, os, pathlib, sys
res, slug, main, owner, my_at = sys.argv[1:6]
files = [pathlib.Path(main)] + list(pathlib.Path(res).glob(slug + " (*conflicted copy*"))
claims = []
for f in files:
    try:
        d = json.load(open(f))
        claims.append((str(d["claimed_at"]), str(d.get("owner") or ""), f))
    except Exception:
        continue
if not any(o == owner and at == my_at for at, o, _ in claims):
    claims.append((my_at, owner, None))
winner = min(claims, key=lambda c: (c[0], c[1]))
if winner[1] == owner:
    # sync may have kept the rival's file as the main name and renamed ours: put our claim back under the main
    # name (atomically), so status/release/publish find it, and never leave the winner inside a "conflicted copy"
    ours = winner[2]
    if ours is not None and ours != pathlib.Path(main):
        os.replace(ours, main)
    print("WIN " + winner[0])
else:
    winner_in_copy = winner[2] is not None and winner[2] != pathlib.Path(main)
    for at, o, f in claims:
        # the winner's replace will overwrite the main name; deleting it here could race that replace over sync
        if winner_in_copy and f == pathlib.Path(main):
            continue
        if o == owner and f is not None:
            # re-read just before deleting: the winner may have moved its claim onto this name meanwhile
            try:
                d = json.load(open(f))
            except Exception:
                continue
            if str(d.get("owner") or "") == owner and str(d.get("claimed_at")) == my_at:
                os.unlink(f)
    print("LOSE " + winner[0] + " " + winner[1])
PY
)
    case "$VERDICT" in
      WIN*) others=$(find "$RES" -name "${SLUG} (*conflicted copy*" 2>/dev/null)
            if [ -n "$others" ]; then
              echo "WARNING: competing later claim(s) exist as conflict copies; yours ($MY_AT) wins. Ask the other claimant to delete:"
              echo "$others" | sed 's/^/  /'
            fi ;;
      *) echo "REFUSED: a competing claim (${VERDICT#LOSE }) predates yours ($MY_AT) — backing off. Pick another paper."; return 1 ;;
    esac
  fi
  # the main file must hold OUR claim: an earlier claimant's winning move can replace it with theirs during the window
  if ! python3 -c 'import json, sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if str(d.get("owner") or "") == sys.argv[2] and str(d.get("claimed_at")) == sys.argv[3] else 1)' \
      "$F" "$OWNER" "$MY_AT" 2>/dev/null; then
    WHO=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get('owner','?'),'since',d.get('claimed_at','?'))" "$F" 2>/dev/null || echo "an unreadable claim")
    echo "REFUSED: a competing claim won the sync race (now reserved by $WHO) — backing off. Pick another paper."
    return 1
  fi
  echo "RESERVED: $(basename "$DIR")  ->  $F"
}

release () {
  local DIR="$1" FORCE="${2:-}"
  DIR="$(cd "$DIR" 2>/dev/null && on_disk_dir "$(pwd -P)" || printf '%s' "$DIR")"   # as claim records it ("." or a relative path)
  local ROOT SLUG F; ROOT="$(root_of "$DIR")"; F="$(reservation_file "$ROOT" "$DIR")"; SLUG="$(basename "$F" .json)"
  [ -f "$F" ] || { echo "note: no reservation to release"; return 0; }
  # a file of this name recording ANOTHER paper (two names sanitising alike) is not this paper's claim: --force must
  # not delete it (publish_completed releases with --force)
  if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$F" 2>/dev/null && ! records_paper "$F" "$DIR"; then
    echo "REFUSED: $F records a different paper ($(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("paper"))' "$F")); not releasing it"; return 1
  fi
  if [ "$FORCE" != "--force" ] && ! python3 -c "import json,sys;exit(0 if json.load(open(sys.argv[1])).get('owner')==sys.argv[2] else 1)" "$F" "$OWNER" 2>/dev/null; then
    echo "REFUSED: not your reservation (use --force after checking with the owner)"; return 1
  fi
  rm -f "$F"; echo "released: $SLUG"
}

status_one () {
  local DIR="$1"; DIR="$(cd "$DIR" 2>/dev/null && on_disk_dir "$(pwd -P)" || printf '%s' "$DIR")"
  local ROOT SLUG F; ROOT="$(root_of "$DIR")"; F="$(reservation_file "$ROOT" "$DIR")"; SLUG="$(basename "$F" .json)"
  if [ -f "$F" ]; then python3 -c "
import json,sys; d=json.load(open(sys.argv[1]))
print(f\"RESERVED by {d['owner']} since {d['claimed_at']}\")" "$F"
  else echo "free"; fi
}

list_all () {
  local ROOT="${1:?list needs the discipline root}"
  local RES="$ROOT/reservations"
  [ -d "$RES" ] || { echo "(no reservations directory yet)"; return 0; }
  python3 - "$RES" "$STALE_DAYS" <<'PY'
import json, pathlib, sys, datetime
RES, days = pathlib.Path(sys.argv[1]), int(sys.argv[2])
now = datetime.datetime.now(datetime.timezone.utc)
rows = sorted(RES.glob("*.json"))
if not rows: print("(no active reservations)")
for f in rows:
    try: d = json.load(open(f))
    except Exception: print(f"  UNREADABLE  {f.name}"); continue
    t = datetime.datetime.strptime(d["claimed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    age = (now - t).days
    flag = "STALE" if age >= days else "     "
    print(f"  {flag}  {d['owner']:<28} {age:>2}d  {f.stem[:70]}")
conf = list(RES.glob("*conflicted copy*"))
for c in conf: print(f"  CONFLICT COPY (resolve: earliest claimed_at wins): {c.name}")
PY
}

reap () {
  # `list` has always FLAGGED stale claims and nothing has ever released one. On the success path
  # publish_completed.sh releases the reservation; on every other path -- an abandoned run, a
  # killed process, a paper skipped after intake -- the claim is held forever and the paper cannot
  # be re-audited without someone deleting a file by hand. At 146 papers that accumulates silently.
  #
  # Deliberately conservative: a claim is reapable only when it is older than the stale window AND
  # its audit directory shows no journal activity inside that window. A live-but-slow audit keeps
  # its claim. Prints what it would do unless --force is given.
  local ROOT="${1:?reap needs the discipline root}"
  local FORCE="${2:-}"
  local RES="$ROOT/reservations"
  [ -d "$RES" ] || { echo "(no reservations directory yet)"; return 0; }
  python3 - "$RES" "$ROOT" "$STALE_DAYS" "$FORCE" "$OWNER" "${I4R_REAP_ANY_OWNER:-0}" <<'PY'
import datetime, glob, json, pathlib, sys
RES, ROOT, days, force = (pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]),
                          int(sys.argv[3]), sys.argv[4] == "--force")
me, any_owner = sys.argv[5], sys.argv[6] == "1"
now = datetime.datetime.now(datetime.timezone.utc)
reaped = held = 0
for f in sorted(RES.glob("*.json")):
    try:
        d = json.loads(f.read_text())
        claimed = datetime.datetime.strptime(d["claimed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        age = (now - claimed).days
    except Exception as exc:
        # One malformed reservation must not abort the sweep for the other 145 papers.
        print(f"  UNREADABLE {f.name}: {exc}")
        held += 1
        continue
    if "conflicted copy" in f.name:
        # claim() arbitrates a race by earliest claimed_at across conflicted copies; deleting one
        # destroys that evidence. Report and hold.
        print(f"  CONFLICT COPY (held, resolve by hand): {f.name}")
        held += 1
        continue
    if age < days:
        held += 1; continue
    # Never reap somebody else's claim by default. `paper` is an absolute path on the CLAIMING
    # machine, so this machine cannot see their audit directory at all and "no audit directory"
    # here means "not mine to judge", not "abandoned". I4R_REAP_ANY_OWNER=1 overrides, and should
    # only be set after actually asking them.
    if d.get("owner") != me and not any_owner:
        print(f"  OTHER    {age:>3}d claim by {d.get('owner')}: {pathlib.Path(str(d.get('paper') or '')).name[:48]}")
        held += 1; continue
    # Proof of life. Getting this wrong RELEASES A LIVE AUDIT, so look in every place one can be.
    #  1. the claim's own absolute path, when it resolves on this machine;
    #  2. anywhere under the tree, matching the basename -- glob.escape'd, because paper titles
    #     legitimately contain [ ] ? * and an unescaped basename silently matches nothing;
    #  3. the whole tree, for an audit dir moved off the paper folder. That is not exotic: it is
    #     exactly what the documented I4R_PAPER_DIR remedy for sandbox-hostile titles produces,
    #     and a basename glob can never find it.
    # Unknown means HELD, never reaped: absence of evidence here is not evidence of abandonment.
    seen_ages = []
    def _seen(path):
        try:
            seen_ages.append((now.timestamp() - path.stat().st_mtime) / 86400.0)
        except OSError:
            pass
    if not d.get("paper") or not str(d["paper"]).startswith("/"):
        # recorded without an absolute path (an earlier version typed "." or a relative one): which audit it guards is
        # unknowable here, and resolving it against reap's own working directory would judge the wrong folder -- hold it
        print(f"  UNKNOWN  {age:>3}d claim with no absolute paper path: {f.name} (held)")
        held += 1
        continue
    literal = pathlib.Path(str(d["paper"]))
    if literal.is_dir():
        for journal in literal.glob("audit*/run.events.jsonl"):
            _seen(journal)
    base = glob.escape(literal.name)
    for journal in ROOT.glob(f"runs/*/{base}/audit*/run.events.jsonl"):   # 2.5.0 layout
        _seen(journal)
    for journal in ROOT.glob(f"*/{base}/audit*/run.events.jsonl"):        # legacy layout
        _seen(journal)
    idle = min(seen_ages) if seen_ages else None
    if idle is None:
        # An audit relocated off its paper folder records the true paper in paper_source.json.
        for source in ROOT.rglob("paper_source.json"):
            try:
                claimed = json.loads(source.read_text())
            except Exception:
                continue
            named = claimed.get("paper") or claimed.get("paper_dir") or ""
            if named and pathlib.Path(str(named)).name == literal.name:
                _seen(source.parent / "run.events.jsonl")
                unknown_location = False
                break
        else:
            unknown_location = True
        idle = min(seen_ages) if seen_ages else None
        if idle is None and unknown_location:
            print(f"  UNKNOWN  {age:>3}d claim, no audit directory found anywhere: "
                  f"{literal.name[:44]} (held; reap cannot prove it is abandoned)")
            held += 1
            continue
    if idle is not None and idle < days:
        print(f"  ACTIVE   {age:>3}d claim, journal touched {idle:.1f}d ago: "
          f"{pathlib.Path(str(d.get('paper') or '')).name[:48]}")
        held += 1; continue
    if idle is None:
        # paper_source.json named this paper but its audit has no journal yet (intake done,
        # init_run.sh never run). That used to fall through to the f-string below with idle=None
        # -- a TypeError that killed the whole sweep and released nothing.
        print(f"  UNKNOWN  {age:>3}d claim, an audit names this paper but has no journal yet: "
              f"{literal.name[:44]} (held; reap cannot prove it is abandoned)")
        held += 1
        continue
    where = f"journal idle {idle:.0f}d"
    if force:
        f.unlink()
        print(f"  REAPED   {age:>3}d claim ({where}): {pathlib.Path(str(d.get('paper') or '')).name[:48]}")
    else:
        print(f"  WOULD REAP {age:>3}d claim ({where}): {pathlib.Path(str(d.get('paper') or '')).name[:48]}")
    reaped += 1
print(f"\n{reaped} reapable, {held} held" + ("" if force else "  (re-run with --force to release)"))
PY
}

selftest () {
  local SELF; SELF="$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")"   # a bare relative $0 is looked up in PATH
  local T; T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
  # a REFUSED claim exits 1 by design; with pipefail on, `cmd | grep -q` would report the pipeline
  # failed even when grep matched — the selftest would fail on its own correct refusals
  set +o pipefail
  export I4R_RESERVE_GRACE=0
  mkdir -p "$T/root/Journal A/Paper One" "$T/root/Journal A/Paper Two"
  local bad=0
  I4R_OWNER=alice "$SELF" claim "$T/root/Journal A/Paper One" | grep -q RESERVED || { echo "FAIL: first claim refused"; bad=1; }
  I4R_OWNER=bob   "$SELF" claim "$T/root/Journal A/Paper One" | grep -q REFUSED  || { echo "FAIL: second owner not refused"; bad=1; }
  I4R_OWNER=alice "$SELF" claim "$T/root/Journal A/Paper One" | grep -q "already yours" || { echo "FAIL: re-claim by owner not idempotent"; bad=1; }
  I4R_OWNER=bob   "$SELF" release "$T/root/Journal A/Paper One" | grep -q REFUSED || { echo "FAIL: foreign release not refused"; bad=1; }
  I4R_OWNER=alice "$SELF" release "$T/root/Journal A/Paper One" | grep -q released || { echo "FAIL: owner release failed"; bad=1; }
  # conflict-copy handling: bob's earlier claim survives as a conflict copy; alice must back off
  local SLUG; SLUG="$(slug_of "$T/root/Journal A/Paper Two")"
  mkdir -p "$T/root/reservations"
  printf '{"owner":"bob","paper":"x","claimed_at":"2020-01-01T00:00:00Z","status":"active"}' \
    > "$T/root/reservations/$SLUG (conflicted copy 2020-01-01).json"
  I4R_OWNER=alice "$SELF" claim "$T/root/Journal A/Paper Two" | grep -q REFUSED || { echo "FAIL: conflict copy not blocking"; bad=1; }
  rm "$T/root/reservations/$SLUG (conflicted copy 2020-01-01).json"
  # completed papers are not claimable
  mkdir -p "$T/root/Completed reports/Journal A/Paper Two"
  I4R_OWNER=alice "$SELF" claim "$T/root/Journal A/Paper Two" | grep -q "already published" || { echo "FAIL: completed paper claimable"; bad=1; }
  # stale flag
  mkdir -p "$T/root/Journal A/Paper Three"  # exists so slug parity holds
  printf '{"owner":"carol","paper":"y","claimed_at":"2020-01-01T00:00:00Z","status":"active"}' \
    > "$T/root/reservations/old_claim.json"
  "$SELF" list "$T/root" | grep -q "STALE.*carol" || { echo "FAIL: stale claim not flagged"; bad=1; }
  [ "$bad" -eq 0 ] && echo "SELFTEST OK: 8 checks — claim, contention, idempotence, foreign release, conflict copy, completed guard, stale flag" || exit 1
  exit 0
}

case "$CMD" in
  --selftest) selftest ;;
  claim)   claim "${2:?paper dir}" ;;
  release) release "${2:?paper dir}" "${3:-}" ;;
  status)  status_one "${2:?paper dir}" ;;
  list)    list_all "${2:-}" ;;
  reap)    reap "${2:?reap needs the discipline root}" "${3:-}" ;;
  *) echo "usage: reserve_paper.sh claim|release|status|list|reap|--selftest [paper-dir|root]"
     echo "       reap <root> [--force]   release stale claims whose audit shows no activity"
     exit 1 ;;
esac
