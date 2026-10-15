#!/bin/bash
# setup_workspace.sh <audit-dir> <paper.pdf> <package.zip|dir>
#
# Since 2.5.0 the tree is laid out as data/<Journal>/<Paper> (inputs) and runs/<Journal>/<Paper>/audit
# (the audit); scripts/new_audit.sh derives both from the paper folder and calls this script.
# scripts/tree_layout.sh is the one place that knows the layout; the legacy <Journal>/<Paper>/audit
# form is still recognised.
#
# I4R_PAPER_DIR overrides which paper directory this audit reserves. By default the paper dir is
# data/<Journal>/<Paper> for a run under runs/, else the audit dir's parent (legacy). That assumption is
# what makes a paper unauditable when its own title contains a glob metacharacter (see below):
# the audit dir inherits the bad character from the paper folder it sits in. Since the paper PDF
# and the package are COPIED into the audit dir, the audit dir does not have to live inside the
# paper folder at all -- point I4R_PAPER_DIR at the real paper folder and put the audit dir
# somewhere with a clean name. 14 of this tree's 171 papers end their title in a question mark.
set -euo pipefail
DIR="${1:?audit dir}"; PAPER="${2:?paper pdf}"; PKG="${3:?package zip or dir}"
STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)   # the intake start SKILL/INSTALL log as phase_start phase=intake at=...
[ -f "$PAPER" ] || { echo "FATAL: paper not found: $PAPER" >&2; exit 2; }
[ -d "$PKG" ] || [ -f "$PKG" ] || { echo "FATAL: package not found: $PKG" >&2; exit 2; }

# The Claude CLI compiles sandbox.filesystem entries as glob patterns, so an audit path holding
# any of ? * [ ] { } \ produces rules that match nothing: every sandboxed Bash read and write is
# denied while the preflight receipt and capability probe still report success. Refuse here, at
# intake, rather than at the first auditor -- one such run cost 5.4M tokens and produced nothing.
# Test the ABSOLUTE form. The forbidden characters almost always sit in a parent component (the
# paper folder's title), so `cd "<journal>/<paper?>" && setup_workspace.sh audit ...` -- the natural
# way to run it -- used to pass a guard that only looked at the string as typed.
# On a fresh audit the parent does not exist yet. A failed `cd` inside one command substitution was
# masked by the later `$(basename ...)`, leaving DIR_ABS as "/audit" and disarming this guard (and
# the reservation below) exactly when it mattered. Resolve the parent explicitly and test both the
# resolved and the typed form.
if PARENT_ABS="$(cd "$(dirname "$DIR")" 2>/dev/null && pwd -P)" && [ -n "$PARENT_ABS" ]; then
  DIR_ABS="$PARENT_ABS/$(basename "$DIR")"
else
  case "$DIR" in /*) DIR_ABS="$DIR" ;; *) DIR_ABS="$(pwd -P)/$DIR" ;; esac
fi
case "$DIR_ABS|$DIR" in
  *[\?\*\[\]\{\}\\]*)
    echo "FATAL: audit dir contains a character that disarms the filesystem sandbox: $DIR_ABS" >&2
    echo "       Forbidden: ? * [ ] { } \\    Safe: spaces : ~ ( ) !" >&2
    echo "       The paper and package are copied into the audit dir, so it does NOT have to sit" >&2
    echo "       inside the paper folder. Put it somewhere clean and name the real paper folder:" >&2
    echo "         I4R_PAPER_DIR=\"<journal>/<paper with ?>\" \\" >&2
    echo "           bash scripts/setup_workspace.sh \"<clean-audit-dir>\" <paper.pdf> <package>" >&2
    echo "       The reservation is still written against I4R_PAPER_DIR, so the paper is claimed" >&2
    echo "       under its true name." >&2
    exit 2 ;;
esac
. "$(cd "$(dirname "$0")" && pwd -P)/tree_layout.sh"
if i4r_audit_is_inside_data "$DIR_ABS"; then
  echo "FATAL: the audit dir is under data/, which holds inputs only: $DIR_ABS" >&2
  echo "       Audits live at <root>/runs/<Journal>/<Paper>/audit. Run" >&2
  echo "         bash scripts/new_audit.sh \"data/<Journal>/<Paper>\"" >&2
  echo "       which derives that path, finds the PDF and the package, and calls this script." >&2
  exit 2
fi

# A populated intake directory can retain files that no longer exist in the source package.
# Failing closed is cheaper than auditing a hybrid of two deposits.
if [ "${I4R_ALLOW_EXISTING:-0}" != "1" ] && [ -d "$DIR/pkg" ] &&
   [ -n "$(find "$DIR/pkg" -mindepth 1 -print -quit 2>/dev/null)" ]; then
  echo "FATAL: $DIR/pkg is not empty; choose a fresh audit directory" >&2
  echo "       Set I4R_ALLOW_EXISTING=1 only for an explicitly documented recovery." >&2
  exit 2
fi

# ---- intake integrity, BEFORE the reservation is claimed ------------------------------------
# Both of these used to fail after reserve_paper.sh had already claimed the paper, leaving the
# operator a half-built audit dir plus a stale reservation to unwind by hand. Refuse at intake,
# the same way the sandbox-path check above does.

# A truncated archive makes `unzip` exit 50 mid-extract, and on a terminal it PROMPTS
# ("write error (disk full?). Continue? (y/n/^C)") instead of failing. One deposit in this tree
# is truncated 926 bytes short of its own central directory.
if [ -f "$PKG" ]; then
  case "$PKG" in
    *.zip|*.ZIP)
      # `zipinfo -t`, not `unzip -t`. What this guard is for is a *structurally* broken archive
      # -- the truncated A36 deposit, which both tools reject with 3. `unzip -t` additionally
      # inflates every member, and exits 2 on the A35 deposit over a single zero-length entry
      # written as Defl:N with zero compressed bytes. That archive extracts fine (the entry's
      # true content is nothing), so refusing it would strand a real, auditable paper on a
      # cosmetic defect. zipinfo also never prompts for a password, where `unzip -t` on an
      # encrypted archive blocks forever on a terminal; </dev/null makes that unconditional.
      # Exit 1 is "warning, some members skipped" and must not strand a paper either.
      if command -v zipinfo >/dev/null 2>&1; then
        zip_rc=0
        zipinfo -t "$PKG" </dev/null >/dev/null 2>&1 || zip_rc=$?
        if [ "$zip_rc" -gt 1 ]; then
          echo "FATAL: package archive is corrupt or truncated: $PKG" >&2
          echo "       zipinfo -t exits $zip_rc, so extraction would fail part-way and, on a" >&2
          echo "       terminal, would stop for input. Re-download the deposit first." >&2
          exit 2
        fi
        # Structurally sound but with unreadable members: worth saying, never worth refusing.
        if ! unzip -t -qq "$PKG" </dev/null >/dev/null 2>&1; then
          echo "NOTE: some members of $PKG do not inflate cleanly; they will extract empty or" >&2
          echo "      short. The archive itself is intact, so the audit proceeds." >&2
          BAD_MEMBERS=1
        fi
      fi ;;
  esac
fi

# Dropbox writes '<name> (Someone's conflicted copy YYYY-MM-DD).ext' beside a file it could not
# merge. Inside an audit those are silently picked up by directory walks, inflating manifests and
# file counts; one staging run here produced 80 of them. The audit tree must be clean going in.
CONFLICTS=""
# `set -euo pipefail` is on and the audit dir may not exist yet on a fresh intake, so each find
# is both guarded and allowed to fail without killing the script. Scanning only $DIR was close
# to useless: at intake the audit dir is normally empty or absent, and the copies that actually
# reach a manifest come in with the deposit. Scan the source being copied as well.
for SCAN_ROOT in "$DIR" "$PKG"; do
  [ -d "$SCAN_ROOT" ] || continue
  FOUND="$(find "$SCAN_ROOT" -name "*conflicted copy*" 2>/dev/null | head -5 || true)"
  [ -n "$FOUND" ] || continue
  CONFLICTS="${CONFLICTS:+$CONFLICTS
}$FOUND"
done
if [ -n "$CONFLICTS" ]; then
  echo "FATAL: the audit directory or the deposit holds Dropbox conflicted copies:" >&2
  echo "$CONFLICTS" | sed 's/^/         /' >&2
  echo "       Delete them (they are never the canonical file) and re-run. Letting them through" >&2
  echo "       puts a second copy of a frozen artifact inside manifests and release listings." >&2
  exit 2
fi

# is $1 the folder $2, or inside it? Compared by identity (APFS ignores case, so path strings are not enough)
path_within () {
  # both the path as written and its resolved form: an audit dir reached through a symlink lives where it points
  python3 -c 'import os, sys
top = sys.argv[2]
for path in (os.path.abspath(sys.argv[1]), os.path.realpath(sys.argv[1])):
    while True:
        try:
            if os.path.samefile(path, top):
                sys.exit(0)
        except OSError:
            pass
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
sys.exit(1)' "$1" "$2"
}
# a package folder that contains the audit (the paper folder data/<J>/<P> with the audit inside it) would copy
# the audit into its own pkg/; a re-intake's package must not be pkg/ itself or inside it (emptying pkg/ would
# delete it). Both refused here, before the claim, so nothing is reserved or created.
if [ -d "$PKG" ] && path_within "$DIR" "$PKG"; then
  echo "FATAL: the package folder $PKG contains the audit directory $DIR; pass the deposit folder itself (the package, not the paper folder that holds the audit)" >&2
  exit 2
fi
if [ -d "$DIR/pkg" ] && path_within "$PKG" "$DIR/pkg"; then
  echo "FATAL: the package $PKG is the audit's own pkg/ (or inside it); a re-intake copies from the original deposit" >&2
  exit 2
fi

# ---- automatic reservation (2026-08-24): the audit claims its own paper before touching it ----
# The paper dir is the audit dir's parent; the discipline tree is two levels up. If the audit is
# running OUTSIDE the shared tree (fixtures, tmp dirs, one-off jobs) there is no reservations
# root to write into, so the claim degrades to a warning. Inside the tree, a refusal ABORTS:
# someone else is already on this paper. I4R_SKIP_RESERVE=1 bypasses (say why in run notes).
# The paper dir is resolved UNCONDITIONALLY: paper_source.json below depends on it, and it used to
# be resolved only inside the reservation block, so I4R_SKIP_RESERVE=1 silently discarded an
# explicit I4R_PAPER_DIR, never wrote paper_source.json, and publication then filed the paper
# under the holding directory's name. The flag gates the claim, nothing else.
# I4R_PAPER_DIR decouples reservation identity from the audit dir's location, so an audit of a
# paper whose folder name is sandbox-hostile can run from a clean directory and still claim the
# right paper. Without it the reservation slug would be derived from the clean directory and two
# auditors could hold the same paper under two different names.
if [ -n "${I4R_PAPER_DIR:-}" ]; then
  PDIR="$(cd "$I4R_PAPER_DIR" 2>/dev/null && pwd || true)"
  [ -n "$PDIR" ] || { echo "FATAL: I4R_PAPER_DIR does not exist: $I4R_PAPER_DIR" >&2; exit 2; }
else
  # runs/<J>/<P>/audit reads data/<J>/<P> (2.5.0); a legacy audit reads its parent folder.
  # the paper dir may not exist yet on a fresh workspace; a failed cd must not kill the run
  PDIR="$(i4r_paper_dir_of_audit "$DIR_ABS")"
  [ -d "$PDIR" ] || PDIR="$(cd "$(dirname "$DIR")" 2>/dev/null && pwd || true)"
fi
if [ "${I4R_SKIP_RESERVE:-0}" != "1" ]; then
  RROOT="${PDIR:+$(i4r_root_of_paper "$PDIR")}"
  RESV="$(cd "$(dirname "$0")" && pwd)/reserve_paper.sh"
  # the discipline root is recognised by its markers (data/, reservations/, or the legacy
  # Completed reports/), not by merely having subdirectories — otherwise a tmp-dir fixture two
  # levels deep would scatter reservations into random parents
  if [ -x "$RESV" ] && [ -n "$RROOT" ] && i4r_is_tree_root "$RROOT"; then
    set +e; OUT="$("$RESV" claim "$PDIR" 2>&1)"; RC=$?; set -e
    # this run made the claim (not "already yours"): only then is releasing it part of undoing this intake
    if [ $RC -eq 0 ] && ! printf '%s' "$OUT" | grep -q "already yours"; then CLAIMED_NOW=1; fi
    echo "$OUT" | sed 's/^/  [reserve] /'
    if [ $RC -ne 0 ]; then
      echo "FATAL: could not reserve this paper — someone else holds it or a conflict is unresolved."
      echo "       Pick another paper, or resolve per the message above. (I4R_SKIP_RESERVE=1 overrides.)"
      exit 1
    fi
  else
    echo "  [reserve] outside the shared tree — no reservation written (fixtures/tmp are exempt)"
  fi
fi

# what this run creates, so a failed intake's undo advice never names anything that was already there (the audit
# dir can be an existing folder, even the paper folder itself in the legacy layout)
CREATED_BY_RUN=""
if [ ! -e "$DIR" ]; then CREATED_BY_RUN="."
else
  for entry in pkg paper work report critique paper_source.json; do
    [ -e "$DIR/$entry" ] || CREATED_BY_RUN="$CREATED_BY_RUN $entry"
  done
  # an existing pkg/ is empty (a non-empty one was refused above), so whatever it holds after a failure is this
  # run's partial copy
  [ -d "$DIR/pkg" ] && CREATED_BY_RUN="$CREATED_BY_RUN pkg"
fi
mkdir -p "$DIR"/{pkg,paper,work,report,critique}

# Publication derives <Journal>/<Paper> from the audit dir's parent, which is wrong whenever the
# audit dir was deliberately placed outside the paper folder (see I4R_PAPER_DIR above). Record
# the true names now, while they are known. Names only, never the absolute path: this file sits
# in the audit tree and an absolute path here would be a private-path leak into a release.
if [ -n "${PDIR:-}" ]; then
  python3 - "$DIR/paper_source.json" "$PDIR" <<'PYSRC'
import json, os, sys
dest, pdir = sys.argv[1], sys.argv[2].rstrip("/")
json.dump({
    "schema_version": "i4r-paper-source-1",
    "journal": os.path.basename(os.path.dirname(pdir)),
    "paper": os.path.basename(pdir),
}, open(dest, "w"), indent=2, sort_keys=True)
open(dest, "a").write("\n")
PYSRC
fi
cp "$PAPER" "$DIR/paper/paper.pdf"
# A previous run ends with `chmod -R a-w "$DIR/pkg"`, so re-copying into an existing intake fails
# with EACCES under `set -e` -- and it fails AFTER the paper has been reserved, leaving the
# operator to undo both by hand. I4R_ALLOW_EXISTING advertised a recovery that could not run.
if [ "${I4R_ALLOW_EXISTING:-0}" = "1" ] && [ -d "$DIR/pkg" ]; then
  # a re-intake replaces pkg/ (this script's own copy of the deposit): copying over the previous copy mixed the two
  # (a flattened wrapper came back beside its flattened files, renamed names beside their originals). The package
  # must not be pkg/ itself, or inside it: emptying pkg/ would delete it.
  chmod -R u+rwX "$DIR/pkg" 2>/dev/null || true
  if ! find "$DIR/pkg" -mindepth 1 -delete; then
    echo "FATAL: could not empty $DIR/pkg for the re-intake (see the error above); fix its permissions (chmod -R u+rwX) and re-run" >&2
    exit 2
  fi
fi
# a failed copy (an unreadable file in the deposit) left a half-built intake and an active reservation, and a
# retry then said "an audit already exists": say what to undo instead of dying on the raw cp error
copy_rc=0
if [ -d "$PKG" ]; then
  cp -R "$PKG"/. "$DIR/pkg/" || copy_rc=$?
  cause="usually an unreadable file; fix the file permissions in the deposit"
else
  unzip_log="$(mktemp "${TMPDIR:-/tmp}/i4r-unzip.XXXXXX")"
  unzip -q -o "$PKG" -d "$DIR/pkg" </dev/null 2>"$unzip_log" || copy_rc=$?
  cat "$unzip_log" >&2
  chmod -R u+rwX "$DIR/pkg" 2>/dev/null || true   # folders stored with odd modes must be readable for the checks below
  # 1 = warnings; 2 with members already reported as not inflating cleanly (the NOTE above) is that same case
  if [ "$copy_rc" -eq 1 ] || { [ "$copy_rc" -eq 2 ] && [ "${BAD_MEMBERS:-0}" = "1" ]; }; then copy_rc=0; fi
  cause="unzip exited $copy_rc; re-download or repair the deposit archive"
  # 50 with "Illegal byte sequence": member names macOS unzip cannot write (non-ASCII names in an archive made on
  # Windows/DOS). The archive itself is fine; unpack it with another tool and pass the folder as the package.
  if [ "$copy_rc" -eq 50 ]; then
    cause="unzip exited 50: a write error (disk full, or a member name too long for the filesystem; see unzip's message above)"
  fi
  # members that do not inflate cleanly extract empty or short, but every member must exist: another failure (a
  # file/directory name collision, a path unzip refused) is not excused by the CRC note
  if [ "$copy_rc" -eq 0 ]; then
    # (a function, not a `case` inside $(...): bash 3.2 cannot parse that)
    list_missing_members () {
      # python's zipfile, not zipinfo: zipinfo prints non-ASCII names as ?? under an empty/C locale (the agent's
      # shell), and a symlink member whose target is not shipped is present though [ -e ] says no
      python3 - "$PKG" "$DIR/pkg" <<'PYMEM' || true
import os, stat, sys, unicodedata, zipfile
root = os.fsencode(sys.argv[2])
try:
    infos = zipfile.ZipFile(sys.argv[1]).infolist()
except Exception:
    infos = []
seen = {}
for info in infos:
    # the bytes unzip wrote: UTF-8 when flagged, else the raw stored bytes (macOS zip and Finder store UTF-8
    # without the flag; zipfile's cp437 decoding round-trips back to them)
    raw = info.filename.encode("utf-8") if info.flag_bits & 0x800 else info.filename.encode("cp437")
    shown = raw.decode("utf-8", "replace")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in shown):
        shown = shown.encode("unicode_escape").decode("ascii")   # show control bytes (Icon\r) instead of hiding them
    # unzip's own normalisation: DOS-created or slash-free names use backslash separators; a leading / and
    # ../ parts are stripped (they land inside pkg/)
    if info.create_system == 0 and b"/" not in raw:   # macOS unzip converts only this case
        raw = raw.replace(b"\\", b"/")
    raw = bytes(b for b in raw if b >= 0x20 and b != 0x7f)   # unzip drops control bytes (Finder/Dropbox "Icon\r")
    parts = [part for part in raw.split(b"/") if part not in (b"", b".", b"..")]
    if raw.endswith(b"/") or not parts:
        continue
    relative = b"/".join(parts)
    # the audit filesystem (APFS) ignores case: a second member differing only by case overwrote the first
    # APFS ignores case and Unicode normalisation (NFC vs NFD) alike
    key = unicodedata.normalize("NFD", relative.decode("utf-8", "replace")).casefold()
    other = seen.setdefault(key, shown)
    if other != shown:
        print(f"{shown} (same name as {other} apart from case or Unicode normalisation)")
        continue
    path = os.path.join(root, relative)
    if not os.path.lexists(path):
        print(shown)
PYMEM
    }
    missing_members="$(list_missing_members || true)"   # no `| head`: under pipefail its SIGPIPE would end the script
    if [ -n "$missing_members" ]; then
      copy_rc=2
      cause="archive members were not extracted, e.g. $(printf '%s\n' "$missing_members" | sed -n '1,5p' | tr '\n' ' ')"
    fi
  fi
  # macOS unzip cannot write some non-ASCII names from Windows-made archives: it says "Illegal byte sequence" or, for a
  # top-level file, a misleading "write error (disk full?)", and those members end up missing. Decide from the names.
  if [ "$copy_rc" -ne 0 ] && ! grep -q "File name too long" "$unzip_log" \
      && { grep -q "Illegal byte sequence" "$unzip_log" || grep -q "write error" "$unzip_log"; }; then
    # the names macOS unzip cannot write: non-ASCII ones without the UTF-8 flag, or from a DOS-made archive
    nonascii_names="$(python3 -c 'import sys, zipfile
def utf8_bytes(i):   # unflagged names whose raw bytes are valid UTF-8 (what macOS zip and some Windows tools store)
    try:
        i.filename.encode("cp437").decode("utf-8")
        return True
    except UnicodeError:
        return False
def unwritable(i):
    if not any(ord(c) > 127 for c in i.filename):
        return False
    if i.create_system == 0:
        return True
    return not i.flag_bits & 0x800 and not utf8_bytes(i)
try:
    bad = [i for i in zipfile.ZipFile(sys.argv[1]).infolist() if unwritable(i)]
    # 1: genuine cp437 (or flagged) names -> python decodes them; 2: unflagged UTF-8 bytes -> ditto reads them right
    print(0 if not bad else 2 if all(not i.flag_bits & 0x800 and utf8_bytes(i) for i in bad) else 1)
except Exception:
    print(0)' "$PKG")"
    if [ "$nonascii_names" = "1" ]; then
      cause="macOS unzip could not write some member names (non-ASCII names, typically from a Windows-made archive); unpack it with python3 -m zipfile -e <zip> <folder> (it decodes those names correctly; ditto garbles them) and pass that folder as the package"
    elif [ "$nonascii_names" = "2" ]; then
      cause="macOS unzip could not write some member names (UTF-8 names in a Windows-made archive); unpack it with ditto -x -k <zip> <folder> (it reads those names correctly) and pass that folder as the package"
    fi
  fi
  rm -f "$unzip_log"
fi
# the copy may carry folders the owner cannot read or enter (a zip or deposit with odd modes): the manifest would skip
# them silently and the undo advice could not remove them. pkg/ is this run's copy and is made read-only at the end.
if [ "$copy_rc" -eq 0 ]; then chmod -R u+rwX "$DIR/pkg" 2>/dev/null || true; fi
# flatten a single wrapper directory BEFORE the checks below, so they judge the layout the audit will keep (a relative
# link moved up one level by the flatten points somewhere else afterwards).
# `-print -quit`, NOT `| head -1`. Under `set -euo pipefail`, find gets SIGPIPE the moment head
# closes its end -- which happens once find has emitted more than a pipe buffer (64 KB) of
# names, i.e. a deposit with a few hundred top-level directories -- and the script dies with
# exit 141 and no message, before the package is hashed or pkg/ made read-only, and AFTER the
# paper was reserved. The same construct at line 36 already used -print -quit.
if [ "$copy_rc" -eq 0 ]; then
  inner=$(find "$DIR/pkg" -mindepth 1 -maxdepth 1 -type d -print -quit)
  if [ "$(find "$DIR/pkg" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" = "1" ] && [ -n "$inner" ] && [ ! -L "$inner" ]; then
    # moved aside first: a child named like the wrapper (rep/rep/...) could not be moved onto it
    staged_wrapper="$DIR/pkg/.i4r-flatten.$$"
    # moving a directory to a new parent needs it writable (its ".." changes): read-only deposit folders stay so
    # until pkg/ is made read-only as a whole below
    if chmod u+w "$inner" 2>/dev/null && mv "$inner" "$staged_wrapper" \
        && find "$staged_wrapper" -mindepth 1 -maxdepth 1 -type d -exec chmod u+w {} \; \
        && find "$staged_wrapper" -mindepth 1 -maxdepth 1 -exec mv {} "$DIR/pkg/" \; \
        && rmdir "$staged_wrapper"; then
      :
    else
      copy_rc=5
      cause="could not flatten the package's single wrapper folder (see the error above)"
    fi
  fi
fi
# names with control bytes (Finder/Dropbox "Icon\r") cannot be cited in a report (the citation parser strips them)
# and differ from what unzip writes for the same package zipped: renamed the way unzip would name them. A clash is
# judged on the deposit itself (a re-intake finds the previous run's renamed copy, which is simply replaced).
if [ "$copy_rc" -eq 0 ] && [ -d "$PKG" ]; then
  rename_control_names () {   # a function: bash 3.2 misparses some heredocs inside $(...)
    python3 - "$DIR/pkg" "$PKG" <<'PYCTRL' || true
import os, stat, sys, unicodedata
root, source = os.fsencode(sys.argv[1]), os.fsencode(sys.argv[2])
def clean(name):
    return bytes(b for b in name if b >= 0x20 and b != 0x7f)
def show(path, top):
    return os.fsdecode(os.path.relpath(path, top)).encode("unicode_escape").decode("ascii")
clashes = []
def key(name):   # APFS ignores case and Unicode normalisation too
    return unicodedata.normalize("NFD", name.decode("utf-8", "replace")).casefold()
top = os.listdir(source)
flattened_away = top[0] if len(top) == 1 and os.path.isdir(os.path.join(source, top[0])) \
    and not os.path.islink(os.path.join(source, top[0])) else None   # the single wrapper the flatten removed
for base, dirs, files in os.walk(source):
    names = set(dirs + files)
    if base == source and flattened_away is not None:
        names.discard(flattened_away)
    # group every name by what it becomes (control bytes dropped, case and Unicode form folded): a group of two or
    # more that includes a renamed one would overwrite (x\r.R and x\x01.R both become x.R)
    groups = {}
    for name in names:
        groups.setdefault(key(clean(name)), []).append(name)
    for name in names:
        path = os.path.join(base, name)
        cleaned = clean(name)
        if cleaned != name and not cleaned:
            clashes.append(f"{show(path, source)} (a name made only of control characters)")
        elif cleaned != name and len(groups.get(key(cleaned), [])) > 1:
            other = next(o for o in groups[key(cleaned)] if o != name)
            clashes.append(f"{show(path, source)} (same name as {show(os.path.join(base, other), source)} apart from control characters, case or Unicode form)")
        if os.path.islink(path) and clean(os.readlink(path)) != os.readlink(path):
            # its target is renamed below: the link would dangle
            clashes.append(f"{show(path, source)} (a symlink to a name with control characters)")
for line in clashes:
    print(line)
if not clashes:
    for base, dirs, files in os.walk(root, topdown=False):
        for name in dirs + files:
            cleaned = clean(name)
            if cleaned == name:
                continue
            try:
                os.chmod(base, os.stat(base).st_mode | stat.S_IWUSR)   # a read-only deposit folder was copied as such
                os.replace(os.path.join(base, name), os.path.join(base, cleaned))
            except OSError as exc:
                print(f"{show(os.path.join(base, name), root)} (could not rename: {exc.strerror})")
PYCTRL
  }
  control_clash="$(rename_control_names)"
  if [ -n "$control_clash" ]; then
    copy_rc=4
    cause="names with control characters (like Finder's Icon<CR> file) that cannot be renamed safely: $(printf '%s\n' "$control_clash" | sed -n '1,3p' | tr '\n' ' ')- rename them in the deposit"
  fi
fi
# a symlink pointing outside pkg/ (absolute, or up out of it) would be followed by the manifest and later steps as if
# its target were package content: refused for folder and zip packages alike
if [ "$copy_rc" -eq 0 ]; then
  list_escaping_links () {   # a function: bash 3.2 misparses some heredocs inside $(...)
    python3 - "$DIR/pkg" <<'PYLINK' || true
import os, stat, sys
root = os.path.realpath(os.fsencode(sys.argv[1]))
for base, dirs, files in os.walk(root):
    for name in dirs + files:
        path = os.path.join(base, name)
        if os.path.islink(path):
            target = os.readlink(path)
            # realpath follows the whole chain (a "d -> ." link makes "d/../x" leave pkg/); a dangling link resolves
            # as far as it can, which is enough to see where it points
            resolved = os.path.realpath(path)
            if os.path.isabs(target) or not (resolved == root or resolved.startswith(root + b"/")):
                shown = f"{os.fsdecode(os.path.relpath(path, root))!r} -> {os.fsdecode(target)!r}"
                try:   # this run's copy: removed, so a re-run after fixing the deposit starts clean
                    os.chmod(base, os.stat(base).st_mode | stat.S_IWUSR)
                    os.unlink(path)
                except OSError as exc:
                    shown += f" (could not remove it from pkg/: {exc.strerror})"
                print(shown)
PYLINK
  }
  escaping_links="$(list_escaping_links)"
  if [ -n "$escaping_links" ]; then
    copy_rc=3
    cause="the package holds symlinks pointing outside it (removed from pkg/ unless noted), e.g. $(printf '%s\n' "$escaping_links" | sed -n '1,3p' | tr '\n' ' ')- in the deposit, replace them with the files they point to (if those belong to it) or remove them"
  fi
fi
if [ "$copy_rc" -ne 0 ]; then
  DIR_SHOWN="$(cd "$DIR" && pwd)"
  if [ "$copy_rc" -eq 3 ] || [ "$copy_rc" -eq 4 ]; then see=""; else see="see the error above; "; fi
  echo "FATAL: could not copy the package into $DIR_SHOWN/pkg (${see}$cause)." >&2
  if [ "${I4R_ALLOW_EXISTING:-0}" = "1" ]; then
    # a re-intake into an existing audit: nothing else of it is touched; fix the deposit and re-run
    echo "       Then re-run the same command (I4R_ALLOW_EXISTING=1)." >&2
  else
    echo "       Then undo this partial intake and start again (copied files may be read-only, hence the chmod):" >&2
    # printf %q: a title with $ or quotes must survive being pasted into a shell
    if [ "$CREATED_BY_RUN" = "." ]; then
      q="$(printf '%q' "$DIR_SHOWN")"
      echo "         chmod -R u+rwX $q && rm -rf $q" >&2
    else
      # the folder existed before: remove only the entries this run added
      for entry in $CREATED_BY_RUN; do
        q="$(printf '%q' "$DIR_SHOWN/$entry")"
        echo "         chmod -R u+rwX $q && rm -rf $q" >&2
      done
    fi
    [ "${CLAIMED_NOW:-0}" = "1" ] && echo "         bash $(printf '%q' "$(cd "$(dirname "$0")" && pwd)/reserve_paper.sh") release $(printf '%q' "$PDIR")" >&2
  fi
  exit 2
fi
command -v pdftotext >/dev/null && pdftotext -layout "$DIR/paper/paper.pdf" "$DIR/paper/paper.txt" 2>/dev/null || true

# One streaming inventory is reused by init, verification, staging and finalization.  The package
# becomes read-only only after hashing; audit-created work belongs under work/.
python3 - "$DIR/pkg" "$DIR/package_manifest.json" <<'PY'
import collections, hashlib, json, os, pathlib, sys, tempfile
root, out = pathlib.Path(sys.argv[1]).resolve(), pathlib.Path(sys.argv[2])
rows, types = [], collections.Counter()
for path in sorted(p for p in root.rglob("*") if p.is_file()):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    stat = path.stat(); rel = path.relative_to(root).as_posix()
    ext = path.suffix.lower().lstrip(".") or "<none>"; types[ext] += 1
    rows.append({"path": rel, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                 "sha256": h.hexdigest(), "extension": ext})
tree = hashlib.sha256("".join(f"{r['path']}\0{r['sha256']}\n" for r in rows).encode()).hexdigest()
payload = {"schema_version": "2.0.0", "algorithm": "sha256", "package_sha256": tree,
           "file_count": len(rows), "file_types": dict(sorted(types.items())), "files": rows}
fd, tmp = tempfile.mkstemp(prefix=out.name + ".", dir=out.parent)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2, ensure_ascii=False); fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
os.replace(tmp, out)
PY
chmod -R a-w "$DIR/pkg"
echo "=== workspace: $DIR ==="
echo "intake started_at: $STARTED_AT   (log it: log_event.sh phase_start phase=intake at=$STARTED_AT)"
echo "package entries: $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["file_count"])' "$DIR/package_manifest.json")"
echo "paper text:      $( [ -f "$DIR/paper/paper.txt" ] && wc -l < "$DIR/paper/paper.txt" || echo 'EXTRACTION FAILED — read the PDF directly')"
echo ""
echo "--- file types ---"
python3 - "$DIR/package_manifest.json" <<'PY'
import json, sys
d=json.load(open(sys.argv[1]))
for ext, n in sorted(d["file_types"].items(), key=lambda x: (-x[1], x[0]))[:12]:
    print(f"{n:6d} {ext}")
PY
echo ""
echo "--- candidate analysis code ---"
# Written to a file and then cut, for the same SIGPIPE reason as the wrapper-directory probe
# above: a deposit with a few hundred scripts killed intake here with exit 141.
__code_list=$(mktemp)
find "$DIR/pkg" -type f \( -iname '*.R' -o -iname '*.qmd' -o -iname '*.rmd' -o -iname '*.py' -o -iname '*.do' -o -iname '*.sps' -o -iname '*.ipynb' -o -iname '*.m' -o -iname '*.jl' -o -iname '*.sas' \) > "$__code_list"
sed -n '1,20p' "$__code_list"
__code_total=$(wc -l < "$__code_list" | tr -d ' ')
[ "$__code_total" -le 20 ] || echo "  ... and $((__code_total - 20)) more"
rm -f "$__code_list"
echo ""
echo "NOTE: verify these counts yourself before briefing auditors. A wrong brief propagates."
