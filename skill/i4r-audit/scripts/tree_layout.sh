#!/bin/bash
# tree_layout.sh -- the ONE place that knows how the discipline tree is laid out (since 2.5.0).
#
#   <root>/data/<Journal>/<Paper>/         inputs: the article PDF and the replication package.
#                                          The skill only READS here; nothing is ever written.
#   <root>/runs/<Journal>/<Paper>/audit    the working audit directory (release staging sits beside it)
#   <root>/comments/<Journal>/<Paper>/     the published comment: PDF, plus the DOCX when bound
#   <root>/metadata/<Journal>/<Paper>/     the published record: run.json, comment.json, the
#                                          finalization receipt, both manifests, the claims script
#   <root>/registry/  <root>/reservations/  <root>/skill/  <root>/old_runs/
#
# The legacy layout (<root>/<Journal>/<Paper>/audit, published into <root>/Completed reports) is
# still recognised, so an audit started before 2.5.0 can be finished and published into the new
# folders by the same scripts. Every script that needs a tree path sources this file; none of them
# count `dirname`s on their own any more -- that rule lived in five places and drifted in each.
#
# Source it:  . "$SCRIPT_DIR/tree_layout.sh"      Call it:  bash tree_layout.sh <function> <path>

i4r_abs () {  # absolute path; the leaf need not exist yet
  # Logical (`pwd`), not physical: callers compare these against paths they were given, and on
  # macOS a physical form turns /var into /private/var. Every `-d` test works on either form.
  local p="$1" d
  d="$(cd "$(dirname "$p")" 2>/dev/null && pwd)" || { printf '%s' "$p"; return 0; }
  printf '%s/%s' "$d" "$(basename "$p")"
}

# --- from an AUDIT directory ---------------------------------------------------------------
i4r_layout_of_audit () {  # new | legacy
  local a; a="$(i4r_abs "$1")"
  if [ "$(basename "$(dirname "$(dirname "$(dirname "$a")")")")" = "runs" ]; then echo new; else echo legacy; fi
}
i4r_root_of_audit () {
  local a; a="$(i4r_abs "$1")"
  if [ "$(i4r_layout_of_audit "$a")" = new ]; then dirname "$(dirname "$(dirname "$(dirname "$a")")")"
  else dirname "$(dirname "$(dirname "$a")")"; fi
}
i4r_journal_of_audit () { basename "$(dirname "$(dirname "$(i4r_abs "$1")")")"; }
i4r_paper_name_of_audit () { basename "$(dirname "$(i4r_abs "$1")")"; }
i4r_paper_dir_of_audit () {  # the INPUT folder this audit reads: data/<J>/<P> when it exists
  local a root j p; a="$(i4r_abs "$1")"
  if [ "$(i4r_layout_of_audit "$a")" = new ]; then
    root="$(i4r_root_of_audit "$a")"; j="$(i4r_journal_of_audit "$a")"; p="$(i4r_paper_name_of_audit "$a")"
    if [ -d "$root/data/$j/$p" ]; then printf '%s' "$root/data/$j/$p"; return 0; fi
  fi
  dirname "$a"
}
i4r_audit_is_inside_data () {  # an audit dir placed under data/ would write into the inputs
  local a; a="$(i4r_abs "$1")"
  [ "$(basename "$(dirname "$(dirname "$(dirname "$a")")")")" = "data" ]
}

# --- from a PAPER (input) directory --------------------------------------------------------
i4r_layout_of_paper () {  # new | legacy
  local p; p="$(i4r_abs "$1")"
  if [ "$(basename "$(dirname "$(dirname "$p")")")" = "data" ]; then echo new; else echo legacy; fi
}
i4r_root_of_paper () {
  local p; p="$(i4r_abs "$1")"
  if [ "$(i4r_layout_of_paper "$p")" = new ]; then dirname "$(dirname "$(dirname "$p")")"
  else dirname "$(dirname "$p")"; fi
}
i4r_journal_of_paper () { basename "$(dirname "$(i4r_abs "$1")")"; }
i4r_audit_dir_for_paper () {  # where a NEW audit of this paper belongs
  local p root j n; p="$(i4r_abs "$1")"
  if [ "$(i4r_layout_of_paper "$p")" = new ]; then
    root="$(i4r_root_of_paper "$p")"; j="$(i4r_journal_of_paper "$p")"
    # the sandbox compiles the audit path as a glob, so the characters that disarm it are dropped
    # from the RUN folder name; the paper keeps its true name in data/ and in paper_source.json
    n="$(basename "$p" | tr -d '?*[]{}\\')"
    printf '%s/runs/%s/%s/audit' "$root" "$j" "$n"
  else
    printf '%s/audit' "$p"
  fi
}

# --- the tree itself -----------------------------------------------------------------------
i4r_is_tree_root () {  # recognised by its markers, never by merely having subdirectories
  [ -d "$1/data" ] || [ -d "$1/reservations" ] || [ -d "$1/Completed reports" ]
}
i4r_published_comment_dir () { printf '%s/comments/%s/%s' "$1" "$2" "$3"; }   # root journal paper
i4r_published_metadata_dir () { printf '%s/metadata/%s/%s' "$1" "$2" "$3"; }
i4r_already_published () {  # root journal paper -> 0 when any publication of this paper exists
  [ -d "$(i4r_published_comment_dir "$1" "$2" "$3")" ] || [ -d "$(i4r_published_metadata_dir "$1" "$2" "$3")" ] \
    || [ -d "$1/Completed reports/$2/$3" ]
}
# 2.7.11: init_run.sh refuses a skill copy whose VERSION is not the tree's canonical one (check_skill_version_pin), but
# it runs after new_audit.sh and setup_workspace.sh have reserved the paper and copied the deposit, so a teammate still
# on the previous release left the paper looking taken. Intake makes the same comparison before anything is reserved
# or copied. Prints why and returns 1 when a new audit must not start; I4R_ALLOW_SKILL_VERSION_MISMATCH=1 makes it a
# warning, as in init_run.sh. A tree without a canonical VERSION (fixtures, tmp) is not checked.
i4r_intake_version_pin () {  # root skill-dir caller
  local c="${I4R_CANONICAL_SKILL_DIR:-$1/skill/i4r-audit-unified/i4r-audit}" canonical own
  [ -f "$c/VERSION" ] || return 0
  canonical="$(head -1 "$c/VERSION" | tr -d ' \t\r\n')"
  [ -n "$canonical" ] || return 0
  own=""; [ -f "$2/VERSION" ] && own="$(head -1 "$2/VERSION" | tr -d ' \t\r\n')"
  [ "$own" = "$canonical" ] && return 0
  if [ "${I4R_ALLOW_SKILL_VERSION_MISMATCH:-0}" = "1" ]; then
    echo "WARNING: running skill version '${own:-unversioned}' differs from the tree's canonical $canonical ($c); allowed by I4R_ALLOW_SKILL_VERSION_MISMATCH=1" >&2
    return 0
  fi
  echo "FATAL: $3: no audit started, nothing reserved or copied: this skill copy is version '${own:-unversioned}' ($2)" >&2
  echo "       but the discipline tree's canonical skill is $canonical ($c), which init_run.sh requires." >&2
  echo "       Reinstall the skill from the canonical copy (its ZIP sits beside it) and run again. To proceed anyway, set" >&2
  echo "       I4R_ALLOW_SKILL_VERSION_MISMATCH=1; the mismatch is then recorded in protocol_version." >&2
  return 1
}
# 2.7.11: intake extracts the article text (paper/paper.txt) with Poppler's pdftotext. Without it the text is simply
# missing, and Phase 1's fallback reads PAPER.txt, the operator's citation and summary, as the article.
i4r_intake_needs_pdftotext () {  # caller
  command -v pdftotext >/dev/null 2>&1 && return 0
  echo "FATAL: $1: no audit started, nothing reserved or copied: pdftotext (Poppler) is not on PATH, so the article" >&2
  echo "       text (paper/paper.txt) could not be extracted. Install Poppler (INSTALL.md, Dependencies: the i4rdoc" >&2
  echo "       conda environment has it) or put it on PATH, and run again." >&2
  return 1
}

# CLI form, for tests and for callers in other languages.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  fn="${1:?usage: tree_layout.sh <i4r_function> <path...>}"; shift
  case "$fn" in
    i4r_*) "$fn" "$@" ;;
    *) echo "unknown function: $fn" >&2; exit 2 ;;
  esac
fi
