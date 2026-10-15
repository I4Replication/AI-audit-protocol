#!/bin/bash
# run_snippets.sh <audit-dir> [--decisions FILE] [--keep] [CLAIM ...]
#
# Runs verification snippets exactly as the release notebook will: the same generated setup chunk
# (deposit loaders, expect()/check() ledger, read-only guard) followed by verification_snippets/_prelude.R,
# each snippet inlined in the same tryCatch wrapper, in claim order, in one knitr session with the
# notebook's chunk options and working directory. Nothing is rendered to HTML and nothing is bound:
# no verification.Rmd, verification_status.txt, verification_summary.csv or manifest is written.
# Prints one verdict per claim and exits 0 only if no check failed.
#
#   CLAIM = C-<n>          the projected verification_snippets/C-<n>.R (.judgement, .unverifiable)
#         | C-<n>=<path>   a snippet source, e.g. work/coordinator/verification_R/f8_slug.R
#         | <path>         a source whose expect()/check() ids name exactly one claim
#   --decisions FILE       every approved decision's verification_snippet (F<n> runs as C-<n>);
#                          CLAIM arguments then narrow that set
#   no CLAIM, no --decisions: every projected snippet, i.e. the whole release notebook
#   --keep                 keep the harness notebook and its knit log (the path is printed)
#
# Exit: 0 all PASS/JUDGEMENT/UNVERIFIABLE, 1 a FAIL (or the deposit changed), 2 usage or setup.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AUDIT=""; DECISIONS=""; KEEP=0; CLAIMS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --decisions) DECISIONS="${2:?--decisions needs a file}"; shift 2;;
    --keep) KEEP=1; shift;;
    -h|--help) sed -n '2,21p' "$0"; exit 0;;
    -*) echo "unknown option $1" >&2; exit 2;;
    *) if [ -z "$AUDIT" ]; then AUDIT="$1"; else CLAIMS+=("$1"); fi; shift;;
  esac
done
[ -n "$AUDIT" ] && [ -d "$AUDIT" ] || { echo "usage: run_snippets.sh <audit-dir> [--decisions FILE] [--keep] [CLAIM ...]" >&2; exit 2; }
AUDIT="$(cd "$AUDIT" && pwd)"
command -v Rscript >/dev/null || { echo "Rscript is required" >&2; exit 2; }
T="$(mktemp -d "${TMPDIR:-/tmp}/i4r-snippets.XXXXXX")"
[ "$KEEP" = 1 ] || trap 'rm -rf "$T"' EXIT

python3 - "$AUDIT" "$T" "$DECISIONS" "${CLAIMS[@]+"${CLAIMS[@]}"}" <<'PY' || exit 2
import json, pathlib, re, sys
audit, tmp, decisions = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
args = sys.argv[4:]
SUFFIX = {"code": ".R", "judgement": ".judgement", "unverifiable": ".unverifiable"}
def die(msg): sys.exit(f"run_snippets: {msg}")
def resolve(path):
    p = pathlib.Path(path)
    p = (p if p.is_absolute() else audit / p).resolve()
    if not p.is_file(): die(f"no such snippet file: {path}")
    return p
def literal_ids(p):
    # the ids the snippet records under; comments dropped, strings kept (the ids ARE strings)
    text = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|(#[^\n]*)',
                  lambda m: "" if m.group(1) else m.group(0), p.read_text(errors="replace"))
    return sorted({m.group(3) for m in re.finditer(
        r"\b(expect|check|judgement|unverifiable)\s*\(\s*(?:id\s*=\s*)?([\"'])(C-[^\"']*)\2", text)})
chosen = {}
if decisions:
    rows = json.loads(resolve(decisions).read_text())
    for fid, row in rows.items():
        m = re.fullmatch(r"F([1-9][0-9]*)", fid)
        spec = (row or {}).get("verification_snippet")
        if not m or not isinstance(spec, dict): continue
        cid, kind = f"C-{m.group(1)}", spec.get("kind")
        if kind == "code":
            chosen[cid] = resolve(spec["path"])
        elif kind in SUFFIX and str(spec.get("basis") or "").strip():
            f = tmp / f"{cid}{SUFFIX[kind]}"; f.write_text(spec["basis"].strip() + "\n"); chosen[cid] = f
def projected(cid):
    for ext in (".R", ".r", ".unverifiable", ".judgement"):
        f = audit / "verification_snippets" / f"{cid}{ext}"
        if f.is_file(): return f
    die(f"{cid} has no snippet in verification_snippets/ (before the projection, name its source: {cid}=<path>)")
selected = {}
for a in args:
    if re.fullmatch(r"C-[1-9][0-9]*", a):
        selected[a] = chosen[a] if a in chosen else projected(a)
    elif re.match(r"C-[1-9][0-9]*=", a):
        cid, path = a.split("=", 1); selected[cid] = resolve(path)
    else:
        p = resolve(a); ids = literal_ids(p)
        if len(ids) != 1: die(f"{a} records under {ids or 'no literal id'}; name its claim as C-<n>={a}")
        selected[ids[0]] = p
if not args:
    selected = chosen or {f.stem: f for f in sorted((audit / "verification_snippets").glob("C-*"))
                          if f.is_file() and re.fullmatch(r"C-[1-9][0-9]*", f.stem)}
if not selected: die("no snippets to run")
for cid, p in selected.items():
    if p.suffix.lower() not in (".r", ".judgement", ".unverifiable"):
        die(f"{cid}: {p.name} is not R source; the notebook executes snippets in an R chunk")
spec = {"claims": [{"id": c, "path": str(p)} for c, p in
                   sorted(selected.items(), key=lambda kv: int(kv[0][2:]))],
        "summary_csv": str(tmp / "summary.csv"), "status_txt": str(tmp / "status.txt")}
(tmp / "spec.json").write_text(json.dumps(spec, indent=2) + "\n")
PY

bash "$HERE/make_verification_notebook.sh" "$AUDIT" --harness "$T/spec.json" -o "$T/run_snippets.Rmd" \
  2> >(grep -v -e '^claims extracted' -e '^with a check' -e '^TODO stubs' -e '^wrote ' >&2) || {
  echo "run_snippets: the harness notebook could not be generated" >&2; exit 2; }
# The release render runs in the notebook's own directory (run_verification.sh); so does this knit.
(cd "$AUDIT" && Rscript --no-init-file -e 'a <- commandArgs(TRUE); invisible(rmarkdown::render(a[1], run_pandoc = FALSE, intermediates_dir = a[2], output_dir = a[2], knit_root_dir = a[3], quiet = TRUE))' \
  "$T/run_snippets.Rmd" "$T" "$AUDIT") > "$T/knit.log" 2>&1
RC=$?
[ "$KEEP" = 1 ] && echo "run_snippets: harness kept in $T (run_snippets.Rmd, run_snippets.knit.md, knit.log)"
if [ ! -s "$T/summary.csv" ] || [ ! -s "$T/status.txt" ]; then
  echo "run_snippets: the knit ended (exit $RC) before the ledger was written; last lines of the log:" >&2
  tail -n 15 "$T/knit.log" >&2
  exit 2
fi
python3 - "$T/summary.csv" "$T/status.txt" "$T/run_snippets.knit.md" <<'PY'
import csv, sys, collections
rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8")))
status = open(sys.argv[2]).read().strip()
by = collections.OrderedDict()
for r in rows: by.setdefault(r["claim"], []).append(r)
for claim, rs in by.items():
    verdicts = collections.Counter(r["verdict"] for r in rs)
    state = "FAIL" if verdicts["FAIL"] else ("PASS" if verdicts["PASS"] else "/".join(verdicts))
    print(f"{claim:<8} {state:<12} " + ", ".join(f"{n} {v}" for v, n in verdicts.items()))
    for r in rs:
        if r["verdict"] == "FAIL": print(f"           {r['key']}: {r['detail']}")
try: modified = "deposit unmodified               : NO" in open(sys.argv[3], errors="replace").read()
except OSError: modified = False
if modified: print("DEPOSIT MODIFIED: a snippet wrote into pkg/ -- investigate before trusting anything")
fails = sum(r["verdict"] == "FAIL" for r in rows)
print(f"run_snippets: {len(by)} claim(s), {fails} failing check(s)")
sys.exit(1 if (fails or status != "0") else 0)
PY
