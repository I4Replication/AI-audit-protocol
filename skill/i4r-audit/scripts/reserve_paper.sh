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
GRACE="${I4R_RESERVE_GRACE:-15}"
STALE_DAYS="${I4R_RESERVE_STALE_DAYS:-7}"

slug_of () {  # <paper-dir> -> journal__paper, filesystem-safe
  local P J
  P="$(basename "$1")"; J="$(basename "$(dirname "$1")")"
  printf '%s__%s' "$J" "$P" | tr -cs 'A-Za-z0-9._-' '_' | cut -c1-180
}
root_of () { dirname "$(dirname "$1")"; }

now_iso () { date -u +%Y-%m-%dT%H:%M:%SZ; }

claim () {
  local DIR="$1"; [ -d "$DIR" ] || { echo "FATAL: no such paper dir: $DIR"; return 1; }
  local ROOT SLUG RES F
  ROOT="$(root_of "$DIR")"; SLUG="$(slug_of "$DIR")"
  RES="$ROOT/reservations"; mkdir -p "$RES"; F="$RES/$SLUG.json"
  # already completed?
  local DEST="$ROOT/Completed reports/$(basename "$(dirname "$DIR")")/$(basename "$DIR")"
  [ -d "$DEST" ] && { echo "REFUSED: this paper is already in Completed reports"; return 1; }
  # existing claim or conflict copies?
  local CONFLICTS
  CONFLICTS=$(find "$RES" -name "${SLUG}*conflicted copy*" 2>/dev/null)
  [ -n "$CONFLICTS" ] && { echo "REFUSED: unresolved conflict copies exist — earliest claimed_at wins, delete the loser:"; echo "$CONFLICTS" | sed 's/^/  /'; return 1; }
  if [ -f "$F" ]; then
    local WHO; WHO=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get('owner','?'),'since',d.get('claimed_at','?'))" "$F" 2>/dev/null || echo "unreadable claim")
    if python3 -c "import json,sys;d=json.load(open(sys.argv[1]));exit(0 if d.get('owner')=='$OWNER' else 1)" "$F" 2>/dev/null; then
      echo "ok: already yours ($WHO)"; return 0
    fi
    echo "REFUSED: reserved by $WHO"; return 1
  fi
  python3 - "$F" "$OWNER" "$DIR" <<'PY'
import json, sys, datetime
json.dump({"owner": sys.argv[2], "paper": sys.argv[3],
           "claimed_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "status": "active"}, open(sys.argv[1], "w"), indent=2)
PY
  # grace window: let Dropbox surface a same-window competing claim as a conflict copy
  [ "$GRACE" -gt 0 ] && { echo "claim written; waiting ${GRACE}s for sync conflicts ..."; sleep "$GRACE"; }
  CONFLICTS=$(find "$RES" -name "${SLUG}*conflicted copy*" 2>/dev/null)
  if [ -n "$CONFLICTS" ]; then
    # earliest claimed_at across all copies wins
    local MINE THEIRS
    MINE=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['claimed_at'])" "$F")
    THEIRS=$(cat $CONFLICTS | python3 -c "import json,sys;print(min(json.loads(l)['claimed_at'] for l in [sys.stdin.read()] for json_ in [0] ) )" 2>/dev/null || true)
    THEIRS=$(python3 - $CONFLICTS <<'PY'
import json, sys
print(min(json.load(open(f))["claimed_at"] for f in sys.argv[1:]))
PY
)
    if [ "$THEIRS" \< "$MINE" ]; then
      rm -f "$F"; echo "REFUSED: a competing claim ($THEIRS) predates yours ($MINE) — backing off. Pick another paper."; return 1
    fi
    echo "WARNING: competing later claim(s) exist as conflict copies; yours ($MINE) wins. Ask the other claimant to delete:"
    echo "$CONFLICTS" | sed 's/^/  /'
  fi
  echo "RESERVED: $(basename "$DIR")  ->  $F"
}

release () {
  local DIR="$1" FORCE="${2:-}"
  local ROOT SLUG F; ROOT="$(root_of "$DIR")"; SLUG="$(slug_of "$DIR")"; F="$ROOT/reservations/$SLUG.json"
  [ -f "$F" ] || { echo "note: no reservation to release"; return 0; }
  if [ "$FORCE" != "--force" ] && ! python3 -c "import json,sys;exit(0 if json.load(open(sys.argv[1])).get('owner')=='$OWNER' else 1)" "$F" 2>/dev/null; then
    echo "REFUSED: not your reservation (use --force after checking with the owner)"; return 1
  fi
  rm -f "$F"; echo "released: $SLUG"
}

status_one () {
  local DIR="$1"; local ROOT SLUG F; ROOT="$(root_of "$DIR")"; SLUG="$(slug_of "$DIR")"; F="$ROOT/reservations/$SLUG.json"
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

selftest () {
  local T; T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
  # a REFUSED claim exits 1 by design; with pipefail on, `cmd | grep -q` would report the pipeline
  # failed even when grep matched — the selftest would fail on its own correct refusals
  set +o pipefail
  export I4R_RESERVE_GRACE=0
  mkdir -p "$T/root/Journal A/Paper One" "$T/root/Journal A/Paper Two"
  local bad=0
  I4R_OWNER=alice "$0" claim "$T/root/Journal A/Paper One" | grep -q RESERVED || { echo "FAIL: first claim refused"; bad=1; }
  I4R_OWNER=bob   "$0" claim "$T/root/Journal A/Paper One" | grep -q REFUSED  || { echo "FAIL: second owner not refused"; bad=1; }
  I4R_OWNER=alice "$0" claim "$T/root/Journal A/Paper One" | grep -q "already yours" || { echo "FAIL: re-claim by owner not idempotent"; bad=1; }
  I4R_OWNER=bob   "$0" release "$T/root/Journal A/Paper One" | grep -q REFUSED || { echo "FAIL: foreign release not refused"; bad=1; }
  I4R_OWNER=alice "$0" release "$T/root/Journal A/Paper One" | grep -q released || { echo "FAIL: owner release failed"; bad=1; }
  # conflict-copy handling: bob's earlier claim survives as a conflict copy; alice must back off
  local SLUG; SLUG="$(slug_of "$T/root/Journal A/Paper Two")"
  mkdir -p "$T/root/reservations"
  printf '{"owner":"bob","paper":"x","claimed_at":"2020-01-01T00:00:00Z","status":"active"}' \
    > "$T/root/reservations/$SLUG (conflicted copy 2020-01-01).json"
  I4R_OWNER=alice "$0" claim "$T/root/Journal A/Paper Two" | grep -q REFUSED || { echo "FAIL: conflict copy not blocking"; bad=1; }
  rm "$T/root/reservations/$SLUG (conflicted copy 2020-01-01).json"
  # completed papers are not claimable
  mkdir -p "$T/root/Completed reports/Journal A/Paper Two"
  I4R_OWNER=alice "$0" claim "$T/root/Journal A/Paper Two" | grep -q "already in Completed" || { echo "FAIL: completed paper claimable"; bad=1; }
  # stale flag
  mkdir -p "$T/root/Journal A/Paper Three"  # exists so slug parity holds
  printf '{"owner":"carol","paper":"y","claimed_at":"2020-01-01T00:00:00Z","status":"active"}' \
    > "$T/root/reservations/old_claim.json"
  "$0" list "$T/root" | grep -q "STALE.*carol" || { echo "FAIL: stale claim not flagged"; bad=1; }
  [ "$bad" -eq 0 ] && echo "SELFTEST OK: 8 checks — claim, contention, idempotence, foreign release, conflict copy, completed guard, stale flag" || exit 1
  exit 0
}

case "$CMD" in
  --selftest) selftest ;;
  claim)   claim "${2:?paper dir}" ;;
  release) release "${2:?paper dir}" "${3:-}" ;;
  status)  status_one "${2:?paper dir}" ;;
  list)    list_all "${2:-}" ;;
  *) echo "usage: reserve_paper.sh claim|release|status|list|--selftest [paper-dir]"; exit 1 ;;
esac
