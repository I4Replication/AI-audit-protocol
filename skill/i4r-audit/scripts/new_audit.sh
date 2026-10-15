#!/bin/bash
# new_audit.sh <data/<Journal>/<Paper>> [--paper <pdf>] [--package <zip|dir>] [--run-name <folder>]
#
# Start an audit of a paper that sits in the tree's data/ folder: derives the run directory
# (runs/<Journal>/<Paper>/audit), finds the article PDF and the replication package when the
# folder makes that unambiguous, and calls setup_workspace.sh with the paper folder recorded as
# I4R_PAPER_DIR. Prints the AUDIT_DIR export to use for every later step.
#
# Before 2.5.0 the operator typed the audit path by hand and the audit lived inside the paper
# folder; a mistyped path, or a title with a glob character in it, cost a run. Here the layout
# rule decides the path, and a title that ends in a question mark gets a run folder
# without the '?' while the paper keeps its true name in data/ and in paper_source.json.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd -P)"
. "$HERE/tree_layout.sh"
PAPER_DIR="${1:?usage: new_audit.sh <data/<Journal>/<Paper>> [--paper <pdf>] [--package <zip|dir>] [--run-name <folder>]}"; shift
PDF=""; PKG=""; RUN_NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --paper|--package|--run-name)
      { [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ]; } || { echo "FATAL: $1 requires a value" >&2; exit 2; } ;;
  esac
  case "$1" in
    --paper) PDF="${2:-}"; shift 2 ;;
    --package) PKG="${2:-}"; shift 2 ;;
    # the run folder under runs/<Journal>/ when the derived one belongs to another paper (a title differing only in
    # characters dropped from run folder names)
    --run-name) RUN_NAME="${2:-}"; shift 2 ;;
    *) echo "FATAL: unexpected argument: $1" >&2; exit 2 ;;
  esac
done
USER_PDF="$PDF"; USER_PKG="$PKG"   # as given, for a printed rerun command
[ -d "$PAPER_DIR" ] || { echo "FATAL: paper folder not found: $PAPER_DIR" >&2; exit 2; }
PAPER_DIR="$(cd "$PAPER_DIR" && pwd -P)"
# bash's pwd -P keeps the case typed; APFS ignores it. Use the names as stored (data/<Journal>/<Paper>), so the run
# folder, paper_source.json and the reservation all agree with the tree whatever case was typed.
PAPER_DIR="$(python3 -c 'import os, sys
parts = sys.argv[1].split("/")
for i in range(max(1, len(parts) - 3), len(parts)):
    parent = "/".join(parts[:i]) or "/"
    try:
        names = os.listdir(parent)
    except OSError:
        continue
    if parts[i] in names:
        continue
    here = os.path.join(parent, parts[i])
    for name in names:
        try:
            if os.path.samefile(os.path.join(parent, name), here):
                parts[i] = name
                break
        except OSError:
            pass
print("/".join(parts), end="")' "$PAPER_DIR")"
if [ "$(i4r_layout_of_paper "$PAPER_DIR")" != new ]; then
  echo "FATAL: $PAPER_DIR is not under <root>/data/<Journal>/. Move the paper's inputs there first;" >&2
  echo "       new_audit.sh only starts audits in the 2.5.0 layout (see INSTALL.md, 'Tree layout')." >&2
  exit 2
fi
ROOT="$(i4r_root_of_paper "$PAPER_DIR")"
AUDIT_DIR="$(i4r_audit_dir_for_paper "$PAPER_DIR")"
DERIVED_AUDIT_DIR="$AUDIT_DIR"
if [ -n "$RUN_NAME" ]; then
  case "$RUN_NAME" in */*|.|..|*[\?\*\[\]\{\}\\]*)
    echo "FATAL: --run-name must be a single folder name without / or ?*[]{}\\: $RUN_NAME" >&2; exit 2 ;;
  esac
  AUDIT_DIR="$(dirname "$(dirname "$AUDIT_DIR")")/$RUN_NAME/audit"
fi

pick_one () {  # label, then candidates (one per line) -> the single candidate, or a refusal
  local label="$1" list="$2" n flag
  n=$(printf '%s\n' "$list" | sed '/^$/d' | wc -l | tr -d ' ')
  if [ "$n" -eq 1 ]; then printf '%s\n' "$list" | sed '/^$/d'; return 0; fi
  case "$label" in *PDF*) flag="--paper <pdf>" ;; *) flag="--package <zip|dir>" ;; esac
  if [ "$n" -eq 0 ]; then echo "FATAL: no $label found in $PAPER_DIR; pass it explicitly with $flag" >&2
  else echo "FATAL: $n candidate ${label}s in $PAPER_DIR; pass the right one explicitly with $flag:" >&2
       printf '%s\n' "$list" | sed '/^$/d;s/^/         /' >&2; fi
  return 1
}
if [ -z "$PDF" ]; then
  # the article, never a comment on it and never a supplement
  PDF="$(pick_one "article PDF" "$(find "$PAPER_DIR" -maxdepth 1 -type f \( -iname '*.pdf' \) \
          ! -iname 'I4R_comment_*' ! -iname '*supplement*' ! -iname '*appendix*' | sort)")" || exit 2
fi
if [ -z "$PKG" ]; then
  archives="$(find "$PAPER_DIR" -maxdepth 1 -type f \( -iname '*.zip' \) | sort)"
  # 2.7.6: never a hidden directory (Claude Code writes .claude/ into the folder it is started in, and new_audit.sh
  # took it as the whole package) nor macOS resource forks
  dirs="$(find "$PAPER_DIR" -mindepth 1 -maxdepth 1 -type d ! -name '.*' ! -name '__MACOSX' | sort)"
  loose="$(find "$PAPER_DIR" -mindepth 1 -maxdepth 1 -type f ! -name '.*' ! -iname 'I4R_comment_*' ! -iname '*.pdf' | sort)"
  if [ -n "$archives" ]; then PKG="$(pick_one "package archive" "$archives")" || exit 2
  elif [ -n "$dirs" ] && [ -n "$loose" ]; then
    # 2.7.6: a folder beside loose files used to win silently, the loose files ignored; say which is meant
    echo "FATAL: $PAPER_DIR holds both package folder(s) and loose files; pass the package explicitly:" >&2
    printf '%s\n' "$dirs" | sed 's/^/         --package /' >&2
    printf '         --package %s   (the loose files and folders together)\n' "$PAPER_DIR" >&2
    exit 2
  elif [ -n "$dirs" ]; then PKG="$(pick_one "package directory" "$dirs")" || exit 2
  else
    # loose files beside the PDF ARE the package: everything except the article and comments
    PKG="$PAPER_DIR"
    echo "note: no archive or package directory; the loose files beside the PDF are the package"
  fi
fi
[ -f "$PDF" ] || { echo "FATAL: article PDF not found: $PDF" >&2; exit 2; }
PDF="$(cd "$(dirname "$PDF")" && pwd -P)/$(basename "$PDF")"   # physical, so the loose-package copy can exclude it
[ -e "$PKG" ] || { echo "FATAL: package not found: $PKG" >&2; exit 2; }
# 2.7.6: a package with no file in it (an empty folder) used to pass intake and fail late in Phase 1 (checked before
# the critic-access call: an input error costs no call)
if [ -d "$PKG" ] && [ -z "$(find "$PKG" -mindepth 1 \( -name '.*' -prune \) -o -type f -print 2>/dev/null | head -1)" ]; then
  echo "FATAL: the package $PKG holds no files; pass the replication package with --package <zip|dir>" >&2
  exit 2
fi
# one audit per paper: the derived run folder and its "<run> (n)" siblings may already hold this paper's audit (started
# earlier with --run-name, or after the colliding paper's folder was moved away)
existing="$(python3 -c 'import json, os, sys, unicodedata
f = lambda v: unicodedata.normalize("NFC", str(v or "")).casefold()
base, journal, paper, target = sys.argv[1:5]
parent, name = os.path.dirname(base), os.path.basename(base)
# every run folder of this journal (an earlier --run-name can be anything), the derived one first
try:
    siblings = sorted(os.path.join(parent, entry) for entry in os.listdir(parent))
except OSError:
    siblings = []
for cand in [base] + [path for path in siblings if path != base]:
    audit = os.path.join(cand, "audit")
    try:
        src = json.load(open(os.path.join(audit, "paper_source.json")))
    except Exception:
        continue
    if f(src.get("paper")) == f(paper) and f(src.get("journal") or journal) == f(journal) \
            and os.path.realpath(audit) != os.path.realpath(target):
        print(audit, end="")
        break' "$(dirname "$DERIVED_AUDIT_DIR")" "$(basename "$(dirname "$PAPER_DIR")")" "$(basename "$PAPER_DIR")" "$AUDIT_DIR")"
if [ -n "$existing" ]; then
  echo "FATAL: an audit of this paper already exists: $existing" >&2
  echo "       Resume it, or move it under old_runs/ before starting again." >&2
  exit 2
fi
if [ -e "$AUDIT_DIR" ] && [ -n "$(find "$AUDIT_DIR" -mindepth 1 -print -quit 2>/dev/null)" ]; then
  # titles differing only in the characters dropped from run folder names (?*[]{}\) share one run folder: that audit
  # may be of ANOTHER paper, which must never be "resumed" for this one
  other="$(python3 -c 'import json, sys, unicodedata
try:
    named = json.load(open(sys.argv[1])).get("paper") or ""
except Exception:
    sys.exit(0)
nfc = lambda value: unicodedata.normalize("NFC", value).casefold()
print(named if named and nfc(named) != nfc(sys.argv[2]) else "", end="")' "$AUDIT_DIR/paper_source.json" "$(basename "$PAPER_DIR")")"
  if [ -n "$other" ]; then
    # this paper may already have its own "<run> (n)" folder from an earlier collision: that is the audit to resume
    run_base="$(dirname "$(dirname "$AUDIT_DIR")")/$(basename "$(dirname "$AUDIT_DIR")")"
    n=2; free=""
    while [ "$n" -le 20 ]; do
      cand="$run_base ($n)/audit"
      if [ -d "$cand" ]; then
        if python3 -c 'import json, sys, unicodedata
f = lambda v: unicodedata.normalize("NFC", v).casefold()
try:
    named = json.load(open(sys.argv[1])).get("paper") or ""
except Exception:
    sys.exit(1)
sys.exit(0 if f(named) == f(sys.argv[2]) else 1)' "$cand/paper_source.json" "$(basename "$PAPER_DIR")"; then
          echo "FATAL: an audit of this paper already exists: $cand" >&2
          echo "       ($AUDIT_DIR is another paper's, $other). Resume it, or move it under old_runs/ before starting again." >&2
          exit 2
        fi
      elif [ -z "$free" ]; then
        free="$cand"
      fi
      n=$((n + 1))
    done
    echo "FATAL: $AUDIT_DIR is the audit of a different paper ($other): the two titles differ only in characters" >&2
    echo "       dropped from run folder names. Start this paper in its own run folder:" >&2
    free="${free:-$run_base (new)/audit}"
    printf '         bash %q %q --run-name %q' "$HERE/new_audit.sh" "$PAPER_DIR" "$(basename "$(dirname "$free")")" >&2
    [ -n "$USER_PDF" ] && printf ' --paper %q' "$USER_PDF" >&2
    [ -n "$USER_PKG" ] && printf ' --package %q' "$USER_PKG" >&2
    printf '\n' >&2
    exit 2
  fi
  echo "FATAL: an audit of this paper already exists: $AUDIT_DIR" >&2
  echo "       Resume it, or move it under old_runs/ before starting again." >&2
  exit 2
fi
# 2.7.5: every critique round runs on the shipped critic route (gpt-6.1-sol), and an audit can no longer be re-pinned
# to another model. Whether a host can use it depends on its Codex account, so a host that cannot would pay for
# Phase 1 and then stop at the round-1 probe. Check after the inputs are validated and before anything is reserved or
# copied (one minimal call, a pass cached per host for 24 h; scripts/check_critic_access.py).
python3 "$HERE/check_critic_access.py"; access_rc=$?
if [ "$access_rc" -ne 0 ]; then
  echo "FATAL: new_audit.sh: no audit started, nothing reserved or copied: this host's Codex CLI must be able to use" >&2
  echo "       the critic route before an audit starts (check_critic_access.py exit $access_rc, reason above). Changing" >&2
  echo "       the host's Codex account is the user's decision; once it is resolved, run again:" >&2
  printf '         bash %q %q' "$HERE/new_audit.sh" "$PAPER_DIR" >&2
  [ -n "$USER_PDF" ] && printf ' --paper %q' "$USER_PDF" >&2
  [ -n "$USER_PKG" ] && printf ' --package %q' "$USER_PKG" >&2
  [ -n "$RUN_NAME" ] && printf ' --run-name %q' "$RUN_NAME" >&2
  printf '\n' >&2
  exit 2
fi
echo "paper:    $PAPER_DIR"
echo "article:  $PDF"
echo "package:  $PKG"
echo "audit:    $AUDIT_DIR"
if [ -d "$PKG" ] && [ "$(cd "$PKG" && pwd -P)" = "$(cd "$PAPER_DIR" && pwd -P)" ]; then
  # copy the loose package files into a staging dir so the article itself is not part of pkg/
  STAGE="$(mktemp -d "${TMPDIR:-/tmp}/i4r-pkg.XXXXXX")" || exit 2
  trap 'chmod -R u+w "$STAGE" 2>/dev/null; rm -rf "$STAGE"' EXIT   # setup_workspace.sh copies it into pkg/; a second copy of the data must not linger
  # one cp per entry, stopping at the first failure: an unreadable file must not leave a silently incomplete pkg/
  find "$(cd "$PAPER_DIR" && pwd -P)" -mindepth 1 -maxdepth 1 ! -samefile "$PDF" ! -iname 'I4R_comment_*' ! -name '.*' ! -name '__MACOSX' \
    -print0 | while IFS= read -r -d '' entry; do cp -R "$entry" "$STAGE"/ || exit 1; done \
    || { echo "FATAL: could not copy the loose package files into staging (see the cp error above); fix their permissions" >&2; exit 2; }
  PKG="$STAGE"
fi
I4R_PAPER_DIR="$PAPER_DIR" bash "$HERE/setup_workspace.sh" "$AUDIT_DIR" "$PDF" "$PKG" || exit $?
echo ""
echo "next:"
case "$AUDIT_DIR" in   # a name may hold $, backticks, quotes or ! (zsh history), which double quotes would expand
  *[\$\`\"\\!]*) printf '  export AUDIT_DIR=%q\n' "$AUDIT_DIR" ;;
  *) echo "  export AUDIT_DIR=\"$AUDIT_DIR\"" ;;
esac
# The target, cap, model and provider printed are the shipped policy's, the values a new audit freezes, so
# the command is one init_run.sh accepts. Until 2.7.0 it printed a literal --auditor-model sonnet-5, which
# init refuses once the policy ships Sonnet 5.5.
INIT_VALUES="$(python3 -c 'import json, sys
policy = json.load(open(sys.argv[1], encoding="utf-8"))
coordinator, critique = policy["models"]["coordinator"], policy["critique"]
family = {"anthropic": "claude", "openai": "gpt"}.get(coordinator["provider"], coordinator["provider"])
print(critique["round_target"], critique["round_cap"], coordinator["requested_model"], family, coordinator["provider"])' \
  "$HERE/../config/pipeline-policy-v2.json" 2>/dev/null)"
read -r I_TARGET I_CAP I_MODEL I_FAMILY I_PROVIDER <<EOF
$INIT_VALUES
EOF
if [ -n "${I_PROVIDER:-}" ]; then
  echo "  bash \"$HERE/init_run.sh\" \"\$AUDIT_DIR\" --doi <doi> --paper-title <title> --round-target $I_TARGET --round-cap $I_CAP \\"
  echo "    --auditor-model $I_MODEL --auditor-family $I_FAMILY --auditor-provider $I_PROVIDER"
else   # an unreadable policy: init_run.sh defaults every one of them from the snapshot it freezes
  echo "  bash \"$HERE/init_run.sh\" \"\$AUDIT_DIR\" --doi <doi> --paper-title <title>"
fi
