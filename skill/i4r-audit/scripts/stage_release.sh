#!/bin/bash
# stage_release.sh <audit-dir> <release-dir>
#
# Stages the audit bundle into a CLEAN directory and refuses to finish if anything
# excluded by the manifest survives. Staging into a fresh tree rather than deleting in
# place is deliberate: critics and auditors regenerate files, so an in-place clean is
# undone by the next run.
#
# Exclusions, and why each is here:
#   1. Verbatim copies of the authors' participant-level data. Seven such copies of five
#      source files existed under work/a4/. They are byte-for-byte duplicates of files
#      already public under CC0, so copying them disclosed nothing new — but shipping them
#      in our bundle is redundant, and the audit's own bright-line rule is that no artefact
#      we distribute pairs a participant identifier with a timestamp. Excluding them costs
#      nothing and removes the question.
#   2. Participant-keyed intermediate .mat files. 106 of 190 carry a participant code in the
#      filename plus an embedded creation timestamp. The timestamps are audit-run times, not
#      testing times, so the sensitivity is low — but the rule is a bright line, and these are
#      regenerable from the scripts we do ship.
#   3. Cached repository JSON, which puts result-file pseudonyms and upload timestamps on one line.
#   4. Raw agent transcripts and driver logs, which are process records, not audit evidence.
#
# What IS shipped: the comment, the ledger, the adjudication log, the five auditors' reports,
# the critique rounds, and every re-runnable script. A reader can regenerate every excluded
# artefact from the scripts plus the public deposit.
set -uo pipefail
A="${1:?usage: stage_release.sh <audit-dir> <release-dir>}"
R="${2:?usage: stage_release.sh <audit-dir> <release-dir>}"
[ -d "$A" ] || { echo "FATAL: $A not found"; exit 1; }
rm -rf "$R"; mkdir -p "$R"

echo "== checking the report to be staged is the current one =="
# A stale REPORT_final.md was staged once, silently, because this script copied whatever file had
# that name. Refuse unless it is byte-identical to the highest-numbered REPORT_v*.md.
LATEST=$(ls "$A"/report/REPORT_v*.md 2>/dev/null | sed 's/.*REPORT_v//; s/\.md//' | sort -n | tail -1)
if [ -n "$LATEST" ]; then
  V="$A/report/REPORT_v${LATEST}.md"
  if [ ! -f "$A/report/REPORT_final.md" ]; then
    echo "FATAL: report/REPORT_final.md missing. Copy REPORT_v${LATEST}.md to it first."; exit 1
  fi
  if ! cmp -s "$V" "$A/report/REPORT_final.md"; then
    echo "FATAL: report/REPORT_final.md and REPORT_v${LATEST}.md differ."
    echo "  If REPORT_final.md is the newer one -- the usual case, because a critique round edits"
    echo "  it in place -- cut a new snapshot and re-run. Do NOT copy the snapshot over the final:"
    echo "  that discards the round you just finished."
    echo "       cp report/REPORT_final.md report/REPORT_v$((LATEST+1)).md   then re-run."
    echo "  Only if REPORT_final.md is genuinely the stale one is the reverse copy right."; exit 1
  fi
  echo "  ok: REPORT_final.md matches REPORT_v${LATEST}.md"
fi

echo "== staging =="
# prose deliverables and the audit record
mkdir -p "$R/report"
for f in REPORT_final.md LEDGER.md ADJUDICATION.md retired_claims.txt front_matter.md captions.txt \
         01_reproduction.md 02_data_integrity.md 03_text_vs_data.md 04_code_methods.md 05_privacy_archive.md; do
  [ -f "$A/report/$f" ] && cp "$A/report/$f" "$R/report/"
done
for f in "$A"/report/*.docx "$A"/report/*.pdf; do [ -f "$f" ] && cp "$f" "$R/report/"; done
for f in PAPER.txt ENV.txt SLOW.txt SETTLED.md WRAPUP.txt RELEASE_MANIFEST.md BRIEF.md; do
  [ -f "$A/$f" ] && cp "$A/$f" "$R/"
done
# The manifest asserts that specific gates passed. Ship the gates, or the assertion is uncheckable —
# a manifest can be accurate about what it ran and still be useless if what it names does not travel.
if [ -d "$A/scripts" ]; then mkdir -p "$R/scripts"; cp "$A"/scripts/*.sh "$R/scripts/" 2>/dev/null; fi
# critique rounds: the critiques themselves, not the transcripts
for d in "$A"/critique/round_*; do
  [ -d "$d" ] || continue
  n=$(basename "$d"); mkdir -p "$R/critique/$n"
  for f in "$d"/critic_*.md "$d"/CRITIQUE.md "$d"/RESPONSE_notes.md; do
    [ -f "$f" ] && cp "$f" "$R/critique/$n/"
  done
done
# Executable source only. NOT .txt/.md: a broad text glob shipped a superseded draft and three
# participant-keyed result listings past the gate below, because that gate only fired on a code and a
# timestamp on the SAME line and these files carry codes alone. Logs and listings are regenerable from
# the scripts; ship the code, not its output.
mkdir -p "$R/work"
( cd "$A/work" && find . -type f \
    \( -name '*.py' -o -name '*.R' -o -name '*.m' -o -name '*.sh' \) \
    ! -name '*superseded*' ! -name '*draft*' ! -name '*.bak' \
    -print0 ) | while IFS= read -r -d '' f; do
  mkdir -p "$R/work/$(dirname "$f")"; cp "$A/work/$f" "$R/work/$f"
done

echo "== pruning verbatim copies of the authors' files =="
# Auditors copy the authors' scripts into scratch directories to test running them as shipped,
# and copy the data alongside. Both are CC0 and already public, so the copies disclose nothing —
# but shipping duplicates of someone else's deposit inside ours is redundant, and for the data
# files it also breaches the audit's own no-identifier-with-timestamp rule. Prune by content
# hash rather than by path, so a renamed copy is caught too.
python3 - "$A" "$R" <<'PY'
import hashlib, pathlib, sys
A, R = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
def h(p): return hashlib.sha256(p.read_bytes()).hexdigest()
src = {h(p) for p in (A/"pkg").rglob("*") if p.is_file()}
dropped = []
for p in sorted(R.rglob("*")):
    if p.is_file() and h(p) in src:
        dropped.append(str(p.relative_to(R))); p.unlink()
print(f"  pruned {len(dropped)} verbatim copies of packaged files")
for d in dropped: print(f"    - {d}")
# leave no empty directories behind
for p in sorted(R.rglob("*"), key=lambda x: -len(x.parts)):
    if p.is_dir() and not any(p.iterdir()): p.rmdir()
PY

echo "== enforcing exclusions =="
viol=0

# 1. no verbatim copy of any file from the authors' package
python3 - "$A" "$R" <<'PY'
import hashlib, pathlib, sys
A, R = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
def h(p): return hashlib.sha256(p.read_bytes()).hexdigest()
src = {h(p) for p in (A/"pkg").rglob("*") if p.is_file()}
bad = [p.relative_to(R) for p in R.rglob("*") if p.is_file() and h(p) in src]
if bad:
    print(f"  FAIL: {len(bad)} verbatim copies of the authors' files survived pruning:")
    for b in bad[:10]: print(f"    {b}")
    sys.exit(1)
print("  ok: no verbatim copy of any packaged file (hash-compared against all of pkg/)")
PY
[ $? -ne 0 ] && viol=1

# 2. no data files at all in the release
#
# The extension list must cover SERIALIZED formats, not just the text and spreadsheet ones. In one
# audit a full verbatim copy of the participant-level data survived a scrub as work/02_d.rds
# precisely because every scanner in the pipeline was looking at text: a .rds is a gzipped R object,
# so it matches no grep, shows no identifier, and reads as a 40 KB binary blob. The same is true of
# .rdata/.rda, .omv (a zip whose strings.bin holds the identifier values), .dta, .parquet, .feather
# and .pkl. A gate that only knows .csv certifies these as clean.
DATA_EXT=( '*.mat' '*.csv' '*.xlsx' '*.xls' '*.sav' '*.rds' '*.RDS' '*.rdata' '*.RData' '*.rda' \
           '*.omv' '*.dta' '*.parquet' '*.feather' '*.pkl' '*.pickle' '*.h5' '*.hdf5' '*.jasp' )
# Built as a find argument vector, not mapfile: macOS ships bash 3.2, where mapfile does not exist
# and would leave the array empty — a detector reporting "0 data files" for the same reason it
# reports it on a clean release. That is the exact shape of failure this file warns about at the
# top, so the list is materialised through a plain find into a temp file instead.
find_args=(); for e in "${DATA_EXT[@]}"; do find_args+=( -o -name "$e" ); done
find_args=( "${find_args[@]:1}" )   # drop the leading -o

__hits_file=$(mktemp)
find "$R" -type f \( "${find_args[@]}" \) > "$__hits_file" 2>/dev/null
n=$(wc -l < "$__hits_file" | tr -d ' ')
if [ "$n" != "0" ]; then
  echo "  FAIL: $n data file(s) present in the release:"; sed -n '1,8p' "$__hits_file" | sed 's/^/    /'
  viol=1
else
  echo "  ok: no data file in any of ${#DATA_EXT[@]} formats (text, spreadsheet AND serialized)"
fi
rm -f "$__hits_file"

# 2b. Self-test the detector both ways, so it can never silently stop matching: a serialized
#     control must trip it, an ordinary .md must not. Controls live outside $R and are never staged.
__st=$(mktemp -d); printf 'x' > "$__st/control.rds"; printf 'x' > "$__st/innocent.md"
__pos=$(find "$__st" -type f \( "${find_args[@]}" \) 2>/dev/null | wc -l | tr -d ' ')
if [ "$__pos" != "1" ]; then
  echo "  FAIL: data-file detector self-test failed (matched $__pos of 1 serialized control)"; viol=1
else
  echo "  ok: data-file detector self-tested (.rds control caught, .md control ignored)"
fi
rm -rf "$__st"

# 3. Per-participant result listings and code/timestamp pairings. Both self-tested.
#
# Two levels, because the narrow one alone let three files through. A code beside a timestamp is the
# bright-line violation. But a file that is simply a per-participant listing keyed by collection code
# is also not something to distribute: the codes are already public in the deposit, yet a derived
# index-to-code mapping is information the deposit does not contain, and every such listing here is
# regenerable from the shipped scripts. Prose that mentions a code in passing is fine; a table of them
# is not, so the test is a per-file density threshold, not a single hit.
python3 - "$R" <<'PY'
import re, pathlib, sys
R = pathlib.Path(sys.argv[1])
# NO \b BEFORE `sub`. A prefixed deposit form such as ABC_XY_sub_E_<n>.mat puts `_` before it, a word
# character, so a leading \b never fires there — the exact bug this skill's own notes warn about,
# reintroduced here and caught by a critic. Match the prefixed form directly, and cover generic
# participant/subject labels too.
CODE  = re.compile(r"(?:^|[^A-Za-z0-9])sub_[EN]_\d+|(?:^|[^A-Za-z0-9])sub_\d+Placebo"
                   r"|\bNoExperto\d+|\bExperto\d+"
                   r"|\b(?:participant|subject|Participant|Subject)\s*#?\d{1,3}\b", re.I)
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T?\d{0,2}:?\d{0,2}|\d{2}:\d{2}:\d{2}")
# Positive controls are deliberately OUT OF RANGE (id 999, year 1900) so that a control can never be
# mistaken for real data, and can never itself trip the gate it is validating.
for probe in ("ABC_XY_sub_E_999.mat", "ABC_XY_sub_999Placebo.mat", "sub_N_999",
              "Experto999", "participant 999"):
    if not CODE.search(probe):
        print(f"  FAIL: the code detector does not fire on {probe!r}"); sys.exit(1)
if not STAMP.search("1900-01-01T00:00:00"):
    print("  FAIL: the timestamp detector does not fire on a known-present instance"); sys.exit(1)
if CODE.search("subtract the substring from subsequent submissions"):
    print("  FAIL: the code detector fires on ordinary prose"); sys.exit(1)
# density self-test: a synthetic listing must trip, a synthetic prose page must not
_listing = "\n".join(f"ABC_XY_sub_E_{900+i}.mat  0.01" for i in range(20))
_prose   = "\n".join(["ordinary sentence about the analysis."]*200 + ["we regenerated sub_N_999"])
def _dens(s):
    L=[x for x in s.split("\n")]; h=sum(1 for x in L if CODE.search(x))
    ne=sum(1 for x in L if x.strip()); return h>=8 and h/max(ne,1)>=0.25
if not _dens(_listing) or _dens(_prose):
    print("  FAIL: the listing-density rule does not separate a listing from prose"); sys.exit(1)
TEXT = {".md",".txt",".json",".py",".r",".m",".sh"}
pairs, listings = [], []
for p in sorted(R.rglob("*")):
    if not p.is_file() or p.suffix.lower() not in TEXT: continue
    lines = p.read_text(errors="replace").split("\n")
    hits = 0
    for i, ln in enumerate(lines, 1):
        if CODE.search(ln):
            hits += 1
            if STAMP.search(ln): pairs.append(f"{p.relative_to(R)}:{i}")
    nonempty = sum(1 for ln in lines if ln.strip())
    frac = hits / max(nonempty, 1)
    # A listing is code-dense: most of its lines are codes. Prose that discusses participants
    # names a few on a page. 25% with a floor of 8 separates the two cleanly here — the audit's
    # own reports run 1-4%, and a genuine per-participant table runs 80%+.
    if hits >= 8 and frac >= 0.25:
        listings.append(f"{p.relative_to(R)} ({hits} of {nonempty} lines, {frac:.0%} — looks like a listing)")
bad = False
if pairs:
    print(f"  FAIL: {len(pairs)} line(s) pair a participant code with a timestamp:")
    for b in pairs[:10]: print(f"    {b}")
    bad = True
if listings:
    print(f"  FAIL: {len(listings)} per-participant listing(s) staged; regenerate from the scripts instead:")
    for b in listings: print(f"    {b}")
    bad = True
if bad: sys.exit(1)
print("  ok: no code/timestamp pairing and no per-participant listing (both detectors self-tested)")
PY
[ $? -ne 0 ] && viol=1

# 3b. OPAQUE PER-ROW IDENTIFIERS, in any format. Check 3 above knows only the code shapes of the
# audits it was written for. On audit A12 a deposit keyed by Qualtrics response ids put
# 5,546 of them across nine staged files and check 3 passed, because none of them looks like
# anything it was taught. That is the scope failure this skill warns about, committed by the skill.
#
# So this check knows no formats. It looks for tokens with the SHAPE of an opaque row key -- long,
# mixed case, containing digits -- and flags a file holding many DISTINCT ones, which is what a
# per-participant listing is and what prose never is. Hex-only tokens are excluded so that hashes
# and commit ids do not trip it.
python3 - "$R" <<'IDPY'
import re, pathlib, sys
R = pathlib.Path(sys.argv[1])
TOKEN = re.compile(r"\b(?:[A-Za-z]{1,4}_)?(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*[a-z])"
                   r"(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{12,}\b")
HEXONLY = re.compile(r"\A[0-9a-fA-F]+\Z")
THRESHOLD = 20

def ids(text):
    return {t for t in TOKEN.findall(text) if not HEXONLY.match(t.split("_")[-1])}

# self-test both ways; controls are out of range so one can never be mistaken for real data
POS = "\n".join("R_%dZzAaBbCcDd%03d" % (i, i) for i in range(30))
if len(ids(POS)) < THRESHOLD:
    print("  FAIL: the opaque-identifier detector does not fire on a planted listing"); sys.exit(1)
NEG = "The response identifier R_9ZzAaBbCcDd999 appears once, in prose, which is fine."
if len(ids(NEG)) >= THRESHOLD:
    print("  FAIL: the opaque-identifier detector fires on prose"); sys.exit(1)
if ids("a" * 40) or ids("deadbeefcafe1234"):
    print("  FAIL: the opaque-identifier detector fires on a hash or a flat string"); sys.exit(1)

bad = []
for f in sorted(R.rglob("*")):
    if not f.is_file() or "pkg_manifest" in f.name: continue
    try: t = f.read_text(errors="ignore")
    except Exception: continue
    n = len(ids(t))
    if n >= THRESHOLD: bad.append((str(f.relative_to(R)), n))
if bad:
    print("  FAIL: %d file(s) hold a per-row identifier listing in an unrecognised format:" % len(bad))
    for b, n in bad[:10]: print("    %s  (%d distinct identifiers)" % (b, n))
    print("    Withhold the file and keep the script that regenerates it.")
    sys.exit(1)
print("  ok: no opaque per-row identifier listing (detector self-tested, threshold %d)" % THRESHOLD)
IDPY
[ $? -ne 0 ] && viol=1

# 4. no absolute home path naming a real account. Placeholders are fine and are what an auditor
# should write; a real username is a disclosure. Self-tested in both directions.
python3 - "$R" <<'PY'
import re, pathlib, sys
R = pathlib.Path(sys.argv[1])
PAT = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)/")
PLACEHOLDER = re.compile(r"^(example|placeholder|redacted|user|username|youruser|someone|account|"
                         r"\[.*\]|<.*>|example-account|your-?name)", re.I)
# The probe is assembled from parts so that no line of this shipped file itself contains the
# pattern — a positive control must not trip the gate it validates.
_P1, _P2, _P3 = "/Us" + "ers/", "realperson", "/Projects"
if not PAT.search(_P1 + _P2 + _P3) or PLACEHOLDER.match(_P2):
    print("  FAIL: the absolute-path detector does not fire on a known-present instance"); sys.exit(1)
if not PLACEHOLDER.match("example-account"):
    print("  FAIL: the placeholder allowance does not recognise an inert example"); sys.exit(1)
bad = []
for p in sorted(R.rglob("*")):
    if not p.is_file() or p.suffix.lower() not in {".md",".txt",".json",".py",".r",".m",".sh"}: continue
    for i, ln in enumerate(p.read_text(errors="replace").split("\n"), 1):
        for m in PAT.finditer(ln):
            if not PLACEHOLDER.match(m.group(1)):
                bad.append(f"{p.relative_to(R)}:{i}")
if bad:
    print(f"  FAIL: {len(bad)} absolute path(s) naming a real account:")
    for b in bad[:10]: print(f"    {b}")
    sys.exit(1)
print("  ok: no absolute path names a real account (detector self-tested both ways)")
PY
[ $? -ne 0 ] && viol=1

echo ""
if [ "$viol" -ne 0 ]; then echo "RELEASE BLOCKED — fix the failures above and re-run."; exit 1; fi
echo "release staged clean at $R"
echo "  files: $(find "$R" -type f | wc -l | tr -d ' ')"

# --- PKG_SENSITIVE_HOOK ---------------------------------------------------
# A generic gate cannot know what a given package treats as sensitive. If the
# audit ships scripts/gate_pkgspecific.py, run it against the STAGED tree and
# let it veto. Learned the hard way: the generic detector hunts P\d+ codes and
# returned a false clean for an archive whose identifiers were bare letters,
# while critic reports carried full-precision BMI that inverts to height/weight.
# This block runs AFTER the success banner, so setting a flag here is useless -- the exact
# "prints FAIL, returns 0" failure this toolkit has hit twice before. The hook must therefore
# veto by itself: tear the staged tree down so a rejected release cannot sit on disk looking
# clean, and exit non-zero directly.
if [ -f "$A/scripts/gate_pkgspecific.py" ]; then
  echo "== package-specific disclosure gate =="
  if ! python3 "$A/scripts/gate_pkgspecific.py" "$A" "$R"; then
    echo "  RELEASE VETOED by the package-specific gate; removing the staged tree"
    rm -rf "$R"
    exit 1
  fi
fi
exit ${fail:-0}
# --- end PKG_SENSITIVE_HOOK -----------------------------------------------

