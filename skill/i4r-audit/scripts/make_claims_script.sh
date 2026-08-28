#!/bin/bash
# make_claims_script.sh <audit-dir> [--selftest]
#
# Builds the REPLICATOR-FACING claims script from comment.json: one numbered, severity-labelled
# claim per finding, each carrying the code that checks it. Decided at the 2026-08-21 meetings:
#
#   1. AUTHOR CODE FIRST, IN THE PAPER'S OWN LANGUAGE. Where the deposit ships code, the claim
#      carries the AUTHORS' OWN code block verbatim, and the claims file itself takes the deposit's
#      language and extension (claims_script.do for Stata, .R for R, .sps for SPSS syntax), with
#      that language's comment markers. A replicator confirming AI-written R has confirmed our
#      code, not the paper's.
#   2. Audit-written checks stay R: inline and executable when the file is R; embedded as clearly
#      marked run-in-R blocks otherwise. When the deposit ships NO code at all, the whole file
#      falls back to R (team decision 2026-08-21).
#   3. Claims with no code at all go in a "CLAIMS WITH NO CODE" tail — one manual check each.
#      Nothing is dropped for lacking code.
#   4. Severity labels are mapped MECHANICALLY from comment.json — never re-judged here.
#      conclusion_contradicted|conclusion_unsupported + CONFIRMED -> retraction material;
#      those at LIKELY, or conclusion_narrowed|value_wrong|artefact_of_construction -> corrigendum;
#      everything else, and anything SPECULATIVE -> comment material.
#   5. Claim numbers are the join key to the [claim N] tags in the report, the page-1 findings
#      table, and the landing-page verdict survey. Never renumber.
#
# Claim->code attachment is judgment work; it lives in <audit-dir>/claims_map.tsv:
#   <finding-id>\t<source>   where source is
#     snippet:<path>                       an audit-written R check
#     author:<path>:<a>-<b>                lines a..b of a deposited code file, verbatim
#     none                                 goes to the no-code tail
# Findings absent from the map fall back to comment.json's verification_script, else the tail.
set -uo pipefail

selftest () {
  local T; T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
  # ---- case 1: R fallback (no author code anywhere) --------------------------------------
  mkdir -p "$T/r/verification_snippets"
  printf 'x <- 1 + 1\nstopifnot(x == 2)\n' > "$T/r/verification_snippets/C-1.1.R"
  cat > "$T/r/comment.json" <<'JSON'
{"findings":[
 {"id":"F1","claim":"A conclusion-level failure, confirmed.","impact":"conclusion_unsupported","confidence":"CONFIRMED","section":"Section 5"},
 {"id":"F2","claim":"There is no data for study four.","impact":"unverifiable","confidence":"CONFIRMED","section":"Section 7",
  "evidence":{"operation":"list the deposited files and observe none belongs to study 4"}},
 {"id":"F3","claim":"A speculative conclusion-level pattern.","impact":"conclusion_contradicted","confidence":"SPECULATIVE","section":"Section 6"}
]}
JSON
  printf 'F1\tsnippet:verification_snippets/C-1.1.R\nF2\tnone\nF3\tnone\n' > "$T/r/claims_map.tsv"
  "$0" "$T/r" >/dev/null || { echo "SELFTEST FAIL: R-case generator exited nonzero"; exit 1; }
  local bad=0 OUT="$T/r/claims_script.R"
  chk () { grep -qF "$2" "$3" || { echo "SELFTEST FAIL [$1]: $2"; bad=1; }; }
  [ -f "$OUT" ] || { echo "SELFTEST FAIL: no-code deposit did not fall back to claims_script.R"; exit 1; }
  chk r-retraction '## CLAIM 1 [RETRACTION MATERIAL]' "$OUT"
  chk r-speculative-demotes '## CLAIM 3 [COMMENT MATERIAL]' "$OUT"
  chk r-snippet-inline 'stopifnot(x == 2)' "$OUT"
  chk r-tail '## CLAIMS WITH NO CODE' "$OUT"
  chk r-manual 'list the deposited files' "$OUT"
  # ---- case 2: Stata-primary deposit ------------------------------------------------------
  mkdir -p "$T/d/verification_snippets" "$T/d/pkg"
  printf 'alpha <- 0.95\nstopifnot(alpha > 0.9)\n' > "$T/d/verification_snippets/C-2.1.R"
  printf 'junk line 1\nuse "data.dta", clear\nreg y x\njunk line 4\n' > "$T/d/pkg/analysis.do"
  cat > "$T/d/comment.json" <<'JSON'
{"findings":[
 {"id":"F1","claim":"The regression table reproduces.","impact":"value_wrong","confidence":"CONFIRMED","section":"Section 2"},
 {"id":"F2","claim":"Reliability is overstated.","impact":"value_wrong","confidence":"CONFIRMED","section":"Section 2"}
]}
JSON
  printf 'F1\tauthor:pkg/analysis.do:2-3\nF2\tsnippet:verification_snippets/C-2.1.R\n' > "$T/d/claims_map.tsv"
  "$0" "$T/d" >/dev/null || { echo "SELFTEST FAIL: Stata-case generator exited nonzero"; exit 1; }
  local DOUT="$T/d/claims_script.do"
  [ -f "$DOUT" ] || { echo "SELFTEST FAIL: Stata deposit did not name claims_script.do"; exit 1; }
  chk do-marker '* CLAIM 1 [CORRIGENDUM MATERIAL]' "$DOUT"
  chk do-author-inline 'reg y x' "$DOUT"
  grep -qE '^\* .*reg y x' "$DOUT" && { echo "SELFTEST FAIL: author Stata code was commented out in its own language"; bad=1; }
  chk do-r-flagged 'RUN THIS BLOCK IN R' "$DOUT"
  grep -qE '^\* R> stopifnot' "$DOUT" || { echo "SELFTEST FAIL: R check not comment-embedded in the .do file"; bad=1; }
  grep -qF 'junk line 1' "$DOUT" && { echo "SELFTEST FAIL: author extract carried lines outside the range"; bad=1; }
  [ "$bad" -eq 0 ] && echo "SELFTEST OK: 12 checks — R fallback, Stata-primary naming, author-inline, R-embedded, severity, tail" || exit 1
  exit 0
}
[ "${2:-}" = "--selftest" ] || [ "${1:-}" = "--selftest" ] && selftest

DIR="${1:?audit dir}"
[ -f "$DIR/comment.json" ] || { echo "FATAL: $DIR/comment.json not found — finalize the audit first"; exit 1; }

python3 - "$DIR" <<'PY'
import json, pathlib, sys
D = pathlib.Path(sys.argv[1])
c = json.loads((D/"comment.json").read_text())
finds = c.get("findings", [])
if not finds: print("FATAL: comment.json has no findings"); sys.exit(1)
ids = [str(f.get("id") or "") for f in finds]
expected = [f"F{i}" for i in range(1, len(finds)+1)]
if ids != expected:
    print(f"FATAL: comment findings must be unique and ordered {expected}; observed {ids}"); sys.exit(1)

def severity(f):  # mechanical; SKILL.md Phase 3b prose must agree — edit both or neither
    imp, conf = f.get("impact"), f.get("confidence")
    if conf == "SPECULATIVE": return "COMMENT MATERIAL"
    if imp in ("conclusion_contradicted", "conclusion_unsupported"):
        return "RETRACTION MATERIAL" if conf == "CONFIRMED" else "CORRIGENDUM MATERIAL"
    if imp in ("conclusion_narrowed", "value_wrong", "artefact_of_construction"):
        return "CORRIGENDUM MATERIAL"
    return "COMMENT MATERIAL"

cmap = {}
mp = D/"claims_map.tsv"
if mp.exists():
    for line_no, ln in enumerate(mp.read_text().splitlines(), 1):
        if ln.strip() and "\t" in ln:
            k, v = ln.split("\t", 1); k=k.strip()
            if k in cmap: print(f"FATAL: duplicate claims_map ID {k} on line {line_no}"); sys.exit(1)
            cmap[k] = v.strip()
else:
    print("FATAL: claims_map.tsv missing — generate it from work/findings.jsonl"); sys.exit(1)
if set(cmap) != set(ids):
    print("FATAL: claims_map IDs do not match comment findings exactly")
    print("  only map:", sorted(set(cmap)-set(ids))); print("  only comment:", sorted(set(ids)-set(cmap)))
    sys.exit(1)
report = D/"report/REPORT_final.md"
if report.is_file():
    import re, collections
    tags = re.findall(r"\[claim\s+(\d+)\]", report.read_text(), re.I)
    dup = [x for x,n in collections.Counter(tags).items() if n != 1]
    if dup or tags != [str(i) for i in range(1, len(finds)+1)]:
        print(f"FATAL: report claim tags must occur exactly once in order; observed {tags}"); sys.exit(1)

# ---- language: majority extension of author: entries; R fallback when none (2026-08-21) ----
LANG = {"r": ("R", "##"), "do": ("do", "*"), "ado": ("do", "*"), "sps": ("sps", "*"),
        "py": ("py", "#"), "jl": ("jl", "#"), "m": ("m", "%"), "sas": ("sas", "*")}
exts = []
for v in cmap.values():
    if v.startswith("author:"):
        try: exts.append(v[7:].rsplit(":", 1)[0].rsplit(".", 1)[1].lower())
        except IndexError: pass
prim = max(set(exts), key=exts.count) if exts else "r"
if prim not in LANG:
    print(f"note: no comment-marker rule for .{prim}; falling back to R conventions")
    prim = "r"
EXT, MK = LANG[prim]

def wrap(t, w=96):
    out, line = [], MK + "  "
    for word in str(t).split():
        if len(line) + len(word) + 1 > w: out.append(line); line = MK + "   " + word
        else: line = (line + " " + word) if line.strip() != MK else (MK + "   " + word)
    if line.strip() != MK: out.append(line)
    return out

body, tail, n_ret, n_cor = [], [], 0, 0
for i, f in enumerate(finds, 1):
    sev = severity(f)
    if sev.startswith("RETRACTION"): n_ret += 1
    if sev.startswith("CORRIGENDUM"): n_cor += 1
    src = cmap.get(f.get("id") or f"F{i}", "")
    if not src and f.get("verification_script"): src = "snippet:" + f["verification_script"]
    head = [MK + " " + "="*92, f"{MK} CLAIM {i} [{sev}]  (finding {f.get('id', f'F{i}')}, {f.get('section','')})"]
    head += wrap(f.get("claim", ""))
    if src.startswith("snippet:"):
        p = D/src[8:]
        if not p.exists(): print(f"FATAL: claim {i} maps to missing snippet {p}"); sys.exit(1)
        code = p.read_text().rstrip("\n").split("\n")
        if EXT == "R":
            body += head + [f"{MK} Check written by the audit (no author code covers this claim):", ""] + code + [""]
        else:
            body += head + [f"{MK} Audit-written check — RUN THIS BLOCK IN R (this file's language is .{EXT}):", ""]
            body += [f"{MK} R> " + l for l in code] + [""]
    elif src.startswith("author:"):
        ref = src[7:]; p, rng = ref.rsplit(":", 1); a, b = (int(x) for x in rng.split("-"))
        fp = D/p
        if not fp.exists(): print(f"FATAL: claim {i} maps to missing author file {fp}"); sys.exit(1)
        lines = fp.read_text(errors="replace").split("\n")[a-1:b]
        ext = p.rsplit(".", 1)[-1].lower()
        native = LANG.get(ext, ("?", "#"))[0] == EXT
        if native:  # the deposit's own language: ship it executable
            body += head + [f"{MK} AUTHORS' OWN CODE, verbatim: {p} lines {a}-{b}", ""] + lines + [""]
        else:       # author code in a second language: embed commented, run in its own tool
            body += head + [f"{MK} AUTHORS' OWN CODE, verbatim: {p} lines {a}-{b} — run in its own tool (.{ext}):", ""]
            body += [f"{MK} | " + l for l in lines] + [""]
    else:
        op = (f.get("evidence") or {}).get("operation") if isinstance(f.get("evidence"), dict) else None
        tail += [f"{MK} CLAIM {i} [{sev}]  (finding {f.get('id', f'F{i}')})"]
        tail += wrap(f.get("claim", ""))
        tail += wrap("MANUAL CHECK: " + (op or "see the report — this claim is verified by inspection, not by code"))
        tail += [MK]

hdr_lines = [
  MK + " " + "="*92,
  f"{MK} CLAIMS SCRIPT — verify every claim in the AI report, one numbered block at a time.",
  f"{MK} Claim numbers match the [claim N] tags in the report, the Appendix-A findings table, and the",
  f"{MK} rows of the verdict survey.",
  MK,
  f"{MK} THE RULE: if you cannot verify a claim marked RETRACTION or CORRIGENDUM MATERIAL after",
  f"{MK} honest study, RETURN THE PAPER and take another. Do not skip it. Do not let AI decide.",
  MK,
  f"{MK} NEVER contact the paper's authors or the journal. I4R handles all correspondence.",
  MK,
  f"{MK} {len(finds)} claims: {n_ret} retraction material, {n_cor} corrigendum material, "
  f"{len(finds)-n_ret-n_cor} comment material. File language: .{EXT} (the deposit's own).",
  MK + " " + "="*92, ""]
out = "\n".join(hdr_lines) + "\n" + "\n".join(body)
if tail:
    out += "\n".join([MK + " " + "="*92,
        f"{MK} CLAIMS WITH NO CODE — verified by inspection or arithmetic, not by running anything.",
        f"{MK} These count exactly like the others: record a verdict for each in the survey.",
        MK + " " + "="*92, ""]) + "\n" + "\n".join(tail) + "\n"
dest = D/f"claims_script.{EXT}"
dest.write_text(out)
mapped = sum(1 for f in finds if (cmap.get(f.get('id','')) or f.get('verification_script') or '').strip() not in ('','none'))
print(f"{dest.name} written: {len(finds)} claims ({n_ret} retraction, {n_cor} corrigendum), "
      f"{mapped} with code, {len(finds)-mapped} in the no-code tail")
PY
