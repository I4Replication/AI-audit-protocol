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

python3 - "$DIR" "$(cd "$(dirname "$0")" && pwd)" <<'PY'
import json, pathlib, sys
D = pathlib.Path(sys.argv[1])
sys.path.insert(0, sys.argv[2])
import claims_harness   # 2.7.11: which harness this audit froze (publication.claims_script_harness)
try:
    PORTABLE = claims_harness.frozen_harness(D) == claims_harness.SHIPPED_HARNESS
except claims_harness.HarnessError as exc:
    print(f"FATAL: {exc}"); sys.exit(1)
c = json.loads((D/"comment.json").read_text())
finds = c.get("findings", [])
if not finds: print("FATAL: comment.json has no findings"); sys.exit(1)
ids = [str(f.get("id") or "") for f in finds]
import re
nums = [int(i[1:]) if re.fullmatch(r"F[1-9][0-9]*", i) else None for i in ids]
# strictly increasing final ids: a finding whose approval is deferred to a later round leaves a hole here
# (finalize refuses target_complete/converged until it is approved or withdrawn)
if None in nums or nums != sorted(set(nums)):
    print(f"FATAL: comment findings must be unique F<n> ids in increasing order; observed {ids}"); sys.exit(1)

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
    if dup or tags != [str(n) for n in nums]:   # the comment's own ids, in order
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

# 2.7.11: a manual check is what a replicator does by hand, and a replicator has the deposit and the article, not the
# audit's working files. A finding's evidence.operation records how the AUDIT settled it and often names those files
# (phase1/quorum.json, report/03_text_vs_data.md, an auditor's evidence/ script): 24 of the first 144 published
# claims scripts did. The operation stays (it is the finding's provenance); the file names are marked as not
# published, and the build says which rows to reword. A path that exists in the deposit is the deposit's. Only the
# audit's own folder names and record names count, so the result depends on the operation and the frozen deposit alone.
INTERNAL_TOPS = ("work", "critique", "coordinator", "phase1", "verification_snippets", "report", "amendments",
                 "registry", "reservations", "evidence", "snippets")
INTERNAL_NAMES = re.compile(r"(?<![\w./-])(findings\.jsonl|claims_map\.tsv|quorum\.json|ADJUDICATION\.md|LEDGER\.md|"
                            r"REPORT_final\.md|REPORT_v[0-9]+\.md|PAPER\.txt|ENV\.txt|SETTLED\.(?:md|json)|"
                            r"run\.events\.jsonl|_prelude\.R|C-[0-9]+\.(?:R|judgement|unverifiable))(?![\w/-])")
def internal_refs(op):
    refs = []
    for m in re.finditer(r"(?<![\w/.~:-])((?:\.{1,2}/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.*<>{}-]+)+)", op or ""):
        p = m.group(1).rstrip(".,;:")
        rel = p[2:] if p.startswith("./") else p
        top = rel.split("/")[0]
        if top in ("pkg", "paper", "..") or (D / "pkg" / rel).exists():
            continue
        if top in INTERNAL_TOPS:
            refs.append(p)
    for m in INTERNAL_NAMES.finditer(op or ""):
        if not any(m.group(1) in r for r in refs):
            refs.append(m.group(1))
    return list(dict.fromkeys(refs))
internal_notes = []

body, tail, n_ret, n_cor = [], [], 0, 0
code_ids = []   # the claims whose audit-written check is inlined below, in order
for f, i in zip(finds, nums):   # i is the finding's own number, as the report's [claim n] tags use
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
            cid = f"C-{i}"; code_ids.append(cid)
            body += head + [f"{MK} Check written by the audit (no author code covers this claim):", "",
                            f'.V$current <- "{cid}"', "tryCatch({"] + code + \
                    [f'}}, error = function(e) runtime_failure("{cid}", e))', ".V$current <- NULL", ""]
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
        refs = internal_refs(op) if PORTABLE else []
        if refs:
            internal_notes.append((i, f.get("id", f"F{i}"), refs))
            tail += wrap("NOT PUBLISHED: " + ", ".join(refs) + (" is one of" if len(refs) == 1 else " are") +
                         " the audit's own working records, which you do not have. Check this claim against the "
                         "deposit and the article, as the report states it.")
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
# Audit-written R checks call the verification notebook's contract (expect() before check(),
# read_deposit(), the audit's _prelude.R helpers). A replicator runs this file on its own, so an
# R claims file that inlines any snippet carries a standalone copy of that contract. (Audit
# A34 shipped a claims_script.R whose every helper was undefined.)
uses_snippet = any(v.startswith("snippet:") for v in cmap.values()) or any(f.get("verification_script") for f in finds)
harness = []
if EXT == "R" and uses_snippet:
    # 2.7.10: the harness IS the verification notebook's setup chunk (make_verification_notebook.sh), so a check
    # that passed in the notebook runs the same way here. 2.7.9 and earlier carried a cut-down copy (csv/tsv/txt/rds
    # loaders only; no SCRATCH, SNIPS_DIR, judgement(), unverifiable() or per-claim error capture), and about half of
    # the published claims scripts stopped with an error when a replicator ran them.
    import re as _re
    LOADER_PKG = {".xlsx": "readxl", ".xls": "readxl", ".dta": "haven", ".sav": "haven", ".por": "haven",
                  ".sas7bdat": "haven", ".parquet": "arrow"}
    loaders = []
    pkg_dir = D / "pkg"
    if pkg_dir.is_dir():
        for path in sorted(pkg_dir.rglob("*")):
            name = LOADER_PKG.get(path.suffix.lower())
            if name and path.is_file() and name not in loaders:
                loaders.append(name)
    BASE_R = {"base", "stats", "utils", "tools", "methods", "graphics", "grDevices", "datasets", "grid",
              "parallel", "splines", "stats4", "tcltk", "compiler"}
    sources = []
    prelude = D/"verification_snippets/_prelude.R"
    if prelude.is_file():
        sources.append(prelude.read_text(errors="replace"))
    for v in cmap.values():
        if v.startswith("snippet:") and (D/v[8:]).is_file():
            sources.append((D/v[8:]).read_text(errors="replace"))
    used = []
    # comments go (a '#' inside a string stays); for pkg:: the strings go too, so prose cannot name a package
    STRING_OR_COMMENT = _re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|(#[^\n]*)')
    for text in sources:
        code = STRING_OR_COMMENT.sub(lambda m: "" if m.group(1) else m.group(0), text)
        bare = STRING_OR_COMMENT.sub(lambda m: "" if m.group(1) else '""', text)
        names = [m.group(1) for m in _re.finditer(r"\b(?:library|require)\(\s*[\"']?([A-Za-z][A-Za-z0-9.]*)", code)]
        names += [m.group(1) for m in _re.finditer(r"\b([A-Za-z][A-Za-z0-9.]*):::?[A-Za-z.]", bare)]
        for name in names:
            if name not in BASE_R and name not in used and name not in ("character", "TRUE", "FALSE"):
                used.append(name)
    need = list(dict.fromkeys(loaders + used))
    reads_paper = any(_re.search(r"paper\.(?:txt|pdf)|[\"']paper[\"']", s) for s in sources)
    r_vec = lambda xs: "c(" + ", ".join(f'"{x}"' for x in xs) + ")"
    # Helper files the checks read through SNIPS_DIR (a Python core, a transcribed table) live beside the snippets in
    # the audit and are never published: carry them in the script and write them to its scratch folder, so it runs on
    # its own. A binary or oversized helper cannot be carried; the checks that need it FAIL as before.
    helpers, snips = [], D / "verification_snippets"
    if snips.is_dir():
        for path in sorted(snips.rglob("*")):
            rel = path.relative_to(snips).as_posix()
            if (not path.is_file() or path.is_symlink() or any(part.startswith(".") for part in rel.split("/"))
                    or rel == "_prelude.R" or _re.fullmatch(r"C-[0-9]+\.(R|r|judgement|unverifiable)", rel)
                    or '"' in rel or "\\" in rel):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if len(text) <= 1_000_000:
                helpers.append((rel, text))

    if PORTABLE:   # only an audit that froze portable_1 runs this; a 2.7.10 build computes none of it
        # ---- 2.7.11: the outside programs the checks run (not R packages), declared and checked before any claim ----
        # Published scripts call pdftotext/pdfinfo/pdfimages, python3 with numpy and scipy, and macOS's textutil at run
        # time, and named none of them: with Poppler 21.03 instead of the audit's 26.x, six checks of one script FAIL
        # (pdftotext -raw spaces "p = .NN" where 26.x gives "p=.NN"); without scipy, a script FAILs twenty checks with
        # "subscript out of bounds". Each program is named with the claims that run it, directly or through the shared
        # helpers, and with the version the audit ran where the audit recorded one; the script checks each one up front
        # and prints one line for a missing, older or wrong-system one. The lines never change a verdict: they say why a
        # FAIL may be the program's. The versions come from the audit's own records, never from this machine: the build
        # must stay a function of the audit's files (generate_finding_artifacts.py --check binds the script's bytes, and
        # a rebuild in a shell with another PATH would otherwise change them).
        def version_of(text):
            m = _re.search(r"[0-9]+(?:\.[0-9]+)+", str(text or ""))
            return m.group(0) if m else ""
        try:   # Phase 1's environment fingerprint, in the snapshot its quorum names: Poppler and Python
            quorum = json.loads((D/"phase1/quorum.json").read_text(encoding="utf-8"))
            snap = str(quorum.get("source_snapshot_sha256") or "")
            fingerprint = (json.loads((D/"phase1/snapshots"/snap/"evidence/environment_fingerprint.json").read_text(encoding="utf-8"))
                           if _re.fullmatch(r"[0-9a-f]{64}", snap) else {})
            fp_tools = fingerprint.get("tools") or {}
        except (OSError, ValueError, AttributeError):
            fp_tools = {}
        try:   # run.json's environment (capture_environment.sh at init): Python and its common packages
            environment = json.loads((D/"run.json").read_text(encoding="utf-8")).get("environment") or {}
            py_packages = {k: version_of(v) for k, v in (environment.get("python_packages") or {}).items()}
        except (OSError, ValueError, AttributeError):
            environment, py_packages = {}, {}
        fp_tools = fp_tools if isinstance(fp_tools, dict) else {}   # a malformed record names no version
        environment = environment if isinstance(environment, dict) else {}
        POPPLER = {"pdftotext", "pdfinfo", "pdfimages", "pdftoppm", "pdftocairo", "pdffonts"}
        recorded = {**{name: version_of(fp_tools.get("pdftotext")) for name in POPPLER},
                    "python3": version_of(fp_tools.get("python")) or version_of(environment.get("python")),
                    "python": version_of(fp_tools.get("python")) or version_of(environment.get("python"))}
        TOOL_INFO = {   # name: (what to install, version flag or "")
            "pdftotext": ("Poppler (pdftotext)", "-v"), "pdfinfo": ("Poppler (pdfinfo)", "-v"),
            "pdfimages": ("Poppler (pdfimages)", "-v"), "pdftoppm": ("Poppler (pdftoppm)", "-v"),
            "pdftocairo": ("Poppler (pdftocairo)", "-v"), "pdffonts": ("Poppler (pdffonts)", "-v"),
            "python3": ("Python 3", "--version"), "python": ("Python", "--version"),
            "textutil": ("textutil (part of macOS)", ""), "tesseract": ("Tesseract OCR", "--version"), "qpdf": ("qpdf", "--version"),
            "mutool": ("MuPDF (mutool)", "-v"), "gs": ("Ghostscript", "--version"), "pandoc": ("pandoc", "--version"),
            "soffice": ("LibreOffice", "--version"), "libreoffice": ("LibreOffice", "--version"),
            "java": ("Java", "-version"), "magick": ("ImageMagick", "-version"),
            "julia": ("Julia", "--version"), "octave": ("GNU Octave", "--version"),
        }
        MACOS_ONLY = {"textutil", "sips", "mdls", "osascript", "pbpaste", "pbcopy", "ditto", "plutil", "sw_vers"}
        UNAMBIGUOUS = {"pdftotext", "pdfinfo", "pdfimages", "pdftoppm", "pdftocairo", "pdffonts", "python3", "textutil",
                       "tesseract", "qpdf", "mutool", "soffice", "libreoffice", "pandoc"}
        NOT_OUTSIDE = {"R", "Rscript", "Rcmd"}   # R itself: a check runs it from R.home("bin"), and R is there
        RAW_STRING = _re.compile(r"""(?<![\w.])[rR](["'])(-*)([(\[{])(.*?)[)\]}]\2\1""", _re.S)
        CALL = _re.compile(r"""\b(?:base::)?(?:system2|system|pipe|Sys\.which|processx::run|sys::exec_wait|"""
                           r"""sys::exec_internal)\s*\(\s*(?:command\s*=\s*)?(["'])([^"'\n]+)\1""")
        def command_name(text):
            for token in text.strip().split():
                if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):   # skip VAR=value prefixes
                    return token.rsplit("/", 1)[-1]
            return ""
        def tools_in(text):
            code = RAW_STRING.sub('""', text)
            code = STRING_OR_COMMENT.sub(lambda m: "" if m.group(1) else m.group(0), code)
            found = [command_name(m.group(2)) for m in CALL.finditer(code)]
            for m in _re.finditer(r"""(["'])([^"'\n]{1,300})\1""", code):   # "python3" in Sys.getenv(..., "python3"), a path
                value = m.group(2).strip(); base = value.rsplit("/", 1)[-1]
                if base in UNAMBIGUOUS and (value == base or value.startswith("/")):
                    found.append(base)
            return [t for t in dict.fromkeys(found)
                    if _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", t) and t not in NOT_OUTSIDE]
        def uses_name(text, name):
            code = STRING_OR_COMMENT.sub(lambda m: "" if m.group(1) else m.group(0), RAW_STRING.sub('""', text))
            return _re.search(r"(?<![\w.])" + _re.escape(name) + r"(?![\w.])", code) is not None
        claim_src = {}
        for f, i in zip(finds, nums):
            src = cmap.get(f.get("id") or f"F{i}", "")
            if not src and f.get("verification_script"): src = "snippet:" + f["verification_script"]
            if src.startswith("snippet:") and (D/src[8:]).is_file():
                claim_src[f"C-{i}"] = (D/src[8:]).read_text(encoding="utf-8", errors="replace")
        prelude_text = prelude.read_text(encoding="utf-8", errors="replace") if prelude.is_file() else ""
        py_helpers = [rel for rel, _ in helpers if rel.endswith(".py")]
        # the prelude's top-level definitions, so a claim that calls PAPER_TXT or paper_has() runs what they run
        defs, current = {}, None
        for line in RAW_STRING.sub('""', prelude_text).split("\n"):   # Python inside an R raw string defines nothing
            m = _re.match(r"^([A-Za-z.][A-Za-z0-9._]*)\s*(?:<<?-|=(?!=))", line)
            if m: current = m.group(1)
            if current: defs[current] = defs.get(current, "") + line + "\n"
        tools = list(dict.fromkeys([t for text in [prelude_text] + list(claim_src.values()) for t in tools_in(text)]))
        if py_helpers and not ({"python3", "python"} & set(tools)):
            tools.append("python3")
        tool_rows = []
        for tool in tools:
            hits = {name for name, block in defs.items() if tool in tools_in(block)}
            if tool in ("python3", "python"):
                hits |= {name for name, block in defs.items() if any(rel.rsplit("/", 1)[-1] in block for rel in py_helpers)}
            grown = True
            while grown:
                more = {name for name, block in defs.items() if name not in hits and any(uses_name(block, h) for h in hits)}
                hits |= more; grown = bool(more)
            claims_using = [cid for cid, text in claim_src.items()
                            if tool in tools_in(text) or any(uses_name(text, h) for h in hits)
                            or (tool in ("python3", "python") and any(rel.rsplit("/", 1)[-1] in text for rel in py_helpers))]
            what, flag = TOOL_INFO.get(tool, (tool, ""))
            env = "I4R_PYTHON" if tool in ("python3", "python") and any("I4R_PYTHON" in s for s in sources) else ""
            version = recorded.get(tool, "")
            modules, module_versions, isolated = [], [], False
            if tool in ("python3", "python"):
                texts = [prelude_text] + list(claim_src.values()) + [text for rel, text in helpers if rel.endswith(".py")]
                local_names = {pathlib.PurePosixPath(rel).stem for rel in py_helpers}
                stdlib = claims_harness.STDLIB_MODULES   # fixed, never this machine's Python
                for text in texts:
                    for m in _re.finditer(r"(?m)^[ \t]*(?:import[ \t]+([\w., \t]+)|from[ \t]+([\w.]+)[ \t]+import\b)", text):
                        for item in (m.group(1) or m.group(2) or "").split(","):
                            top = item.strip().split(" ")[0].split(".")[0]
                            if top and top not in stdlib and top not in local_names and top not in modules \
                                    and _re.fullmatch(r"[A-Za-z_]\w*", top):
                                modules.append(top)
                isolated = any(_re.search(r"""["']-I["']""", text) for text in [prelude_text] + list(claim_src.values()))
                module_versions = [py_packages.get(m, "") for m in modules]
            tool_rows.append(dict(name=tool, what=what, flag=flag, audit=_re.sub(r"[^0-9.]", "", version),
                                  os="Darwin" if tool in MACOS_ONLY else "", env=env, modules=modules,
                                  module_versions=module_versions, isolated=isolated, claims=claims_using))
        if tool_rows:
            rows = []
            for t in tool_rows:
                rows.append('  list(name = "{name}", what = "{what}", flag = "{flag}", audit = "{audit}", os = "{os}", '
                            'env = "{env}", modules = {mods}, module_versions = {mvs}, isolated = {iso}, claims = {claims})'
                            .format(mods=r_vec(t["modules"]), mvs=r_vec(t["module_versions"]),
                                    iso="TRUE" if t["isolated"] else "FALSE", claims=r_vec(t["claims"]),
                                    **{k: t[k] for k in ("name", "what", "flag", "audit", "os", "env")}))
            tools_r = "\n".join([
                "",
                "## The programs outside R these checks run, with the versions the audit ran them with. Each is checked here,",
                "## before any claim: a missing one, an older version than the audit's, a missing Python module or the wrong",
                "## system is named on one line with the claims that run it, which may then FAIL because of the program and",
                "## not because of the claim. Install what is named, or run on the system named, and run this file again.",
                ".I4R_TOOLS <- list(", ",\n".join(rows), ")",
                ".I4R_TOOL_ISSUES <- character(0)",
                "for (.t in .I4R_TOOLS) local({",
                "  t <- .t; cmd <- if (nzchar(t$env)) Sys.getenv(t$env, t$name) else t$name",
                '  who <- if (length(t$claims)) paste0(" (claims ", paste(t$claims, collapse = ", "), ")") else ""',
                "  say <- function(text) {",
                "    .I4R_TOOL_ISSUES <<- c(.I4R_TOOL_ISSUES, t$name)",
                '    message("Outside program ", t$name, ": ", text, who)',
                "  }",
                '  if (nzchar(t$os) && Sys.info()[["sysname"]] != t$os)',
                '    return(say(paste0("runs on macOS only and this system is ", Sys.info()[["sysname"]],',
                '                      "; those claims FAIL here: run them on macOS")))',
                "  path <- Sys.which(cmd)",
                '  if (!nzchar(path)) return(say(paste0("MISSING: install ", t$what,',
                '    if (nzchar(t$audit)) paste0(" (the audit ran ", t$audit, ")") else "", "; until then those claims FAIL")))',
                '  here <- ""',
                "  if (nzchar(t$flag)) {",
                "    out <- tryCatch(suppressWarnings(system2(path, t$flag, stdout = TRUE, stderr = TRUE)),",
                "                    error = function(e) character(0))   # system2() quotes the command itself",
                '    m <- regmatches(out, regexpr("[0-9]+(\\\\.[0-9]+)+", out)); here <- if (length(m)) m[1] else ""',
                "  }",
                "  major <- function(v) as.integer(strsplit(v, \".\", fixed = TRUE)[[1]][1])",
                "  if (nzchar(here) && nzchar(t$audit) &&   # an older MAJOR version (Poppler's is the year: 21 before 26)",
                "      isTRUE(tryCatch(major(here) < major(t$audit), error = function(e) FALSE, warning = function(w) FALSE)))",
                '    say(paste0(here, " is OLDER than the audit\'s ", t$audit, ": those claims may FAIL because of it; install ",',
                '               t$what, " ", t$audit, " or later"))',
                "  if (length(t$modules)) {",
                "    probe <- \"import importlib.util, sys; print(' '.join(m for m in sys.argv[1:] if importlib.util.find_spec(m) is None))\"",
                '    out <- tryCatch(suppressWarnings(system2(path, c(if (t$isolated) "-I", "-c", shQuote(probe), t$modules),',
                '                                             stdout = TRUE, stderr = TRUE)), error = function(e) "?")',
                '    gone <- intersect(strsplit(paste(out, collapse = " "), "[[:space:]]+")[[1]], t$modules)',
                '    if (length(gone)) say(paste0("MISSING Python module", if (length(gone) > 1) "s", " ", paste(gone, collapse = ", "),',
                '      " (the audit ran ", paste(trimws(paste(t$modules, t$module_versions)), collapse = ", "), "); install ",',
                '      if (length(gone) > 1) "them" else "it", " for ", path, if (t$isolated) " itself (the checks run it with -I, so",',
                '      if (t$isolated) " a module in your user site-packages is not seen)", "; until then those claims FAIL"))',
                "  }",
                "})",
                "rm(.t)",
                ""])
        else:
            tools_r = ""
        # the R the checks need: the native pipe |> and \(x) need R 4.1; the carried helpers' raw strings need R 4.0
        stripped = "\n".join(STRING_OR_COMMENT.sub(lambda m: "" if m.group(1) else '""', RAW_STRING.sub('""', s))
                             for s in sources)
        need_r = "4.1.0" if _re.search(r"\|>|(?<![\w.])\\\(", stripped) else ("4.0.0" if helpers or any(
            RAW_STRING.search(s) for s in sources) else "")
        rversion_r = (f'if (getRversion() < "{need_r}") stop("these checks need R {need_r} or later; this is R ", '
                      f'getRversion(), call. = FALSE)\n' if need_r else "")
    else:
        tools_r = rversion_r = ""
    PORTABLE_SETUP = [
        (r"""## How to run it: Rscript claims_script.R from any folder, with the deposit's folder in I4R_PKG_DIR (or a pkg/
## folder beside the working directory). Checks that read the article find it in paper/ beside pkg/ (or in
## I4R_AUDIT_DIR/paper/): the PDF as paper/paper.pdf and its text as paper/paper.txt, made with
##     pdftotext -layout paper/paper.pdf paper/paper.txt
## Run it in a UTF-8 locale. Every check passed, with these helpers, in the audit's verification notebook.
if (!isTRUE(l10n_info()[["UTF-8"]]))
  message("This R session is not in a UTF-8 locale; checks on text with non-ASCII characters may FAIL. ",
          "Run it with LANG=en_US.UTF-8 (or C.UTF-8).")
""",
         r"""## How to run it: put the deposit in a folder named pkg/ and the article in a folder named paper/, side by side
## (the PDF as paper/paper.pdf, its text as paper/paper.txt, made with
##     pdftotext -layout paper/paper.pdf paper/paper.txt
## ), and run Rscript claims_script.R from the folder that holds them both. From any other folder, set I4R_PKG_DIR
## to the deposit's folder: paper/ is then looked for beside that folder (or set I4R_AUDIT_DIR to the folder that
## holds paper/).
## Run it in a UTF-8 locale. Every check passed, with these helpers, in the audit's verification notebook.
## In any other locale (LC_ALL=C, say) R reads the non-ASCII text in this file and in the deposit as bytes it cannot
## match, and whole groups of checks FAIL, so this switches the R process to UTF-8 first. It must stay the file's
## first expression: R reads each later expression in the locale it sets.
if (!isTRUE(l10n_info()[["UTF-8"]])) local({
  for (loc in c("en_US.UTF-8", "C.UTF-8", "en_US.utf8", "C.utf8"))
    if (nzchar(suppressWarnings(Sys.setlocale("LC_CTYPE", loc)))) {
      suppressWarnings(Sys.setlocale("LC_COLLATE", loc))
      message("This R session was not in a UTF-8 locale; it now runs in ", loc, ", as the checks did.")
      break
    }
  if (!isTRUE(l10n_info()[["UTF-8"]]))
    message("This R session is not in a UTF-8 locale and none could be set; checks on text with non-ASCII ",
            "characters may FAIL. Run it with LANG=en_US.UTF-8 (or C.UTF-8).")
})
__RVERSION__"""),
        (r"""

## Resolve the deposit WITHOUT hardcoding the auditor's machine: I4R_PKG_DIR, else a pkg/ folder beside the
## working directory (or the audit layout's), else stop.
""",
         r"""
__TOOLS__
## Resolve the deposit WITHOUT hardcoding the auditor's machine: I4R_PKG_DIR, else pkg/ in the working directory
## (or ../audit/pkg, the audit's own layout), else stop.
"""),
        (r"""  stop("cannot locate the deposit: set I4R_PKG_DIR to the extracted package directory (the pkg/ folder)")
""",
         r"""  stop("cannot locate the deposit: run this from the folder that holds pkg/, or set I4R_PKG_DIR to the ",
       "extracted package directory")
"""),
        (r"""  if (!prelude_ok) cat("the shared helpers (_prelude.R) stopped with an error; claims that use them FAIL\n")
""",
         r"""  tool_issues <- get0(".I4R_TOOL_ISSUES", ifnotfound = character(0))
  if (length(tool_issues) && n_fail > 0) cat("outside programs missing or older than the audit's (named at the top):",
                               paste(unique(tool_issues), collapse = ", "),
                               "-- a FAIL in a claim that runs one may be the program's, not the claim's\n")
  if (!prelude_ok) cat("the shared helpers (_prelude.R) stopped with an error; claims that use them FAIL\n")
"""),
    ]
    setup = r"""
## How to run it: Rscript claims_script.R from any folder, with the deposit's folder in I4R_PKG_DIR (or a pkg/
## folder beside the working directory). Checks that read the article find it in paper/ beside pkg/ (or in
## I4R_AUDIT_DIR/paper/): the PDF as paper/paper.pdf and its text as paper/paper.txt, made with
##     pdftotext -layout paper/paper.pdf paper/paper.txt
## Run it in a UTF-8 locale. Every check passed, with these helpers, in the audit's verification notebook.
if (!isTRUE(l10n_info()[["UTF-8"]]))
  message("This R session is not in a UTF-8 locale; checks on text with non-ASCII characters may FAIL. ",
          "Run it with LANG=en_US.UTF-8 (or C.UTF-8).")

## The packages these checks use. A missing one is named here; the claims that need it record a FAIL.
.I4R_NEED <- __NEED__
.I4R_MISSING <- .I4R_NEED[!vapply(.I4R_NEED, requireNamespace, logical(1), quietly = TRUE)]
if (length(.I4R_MISSING)) message(
  "These R packages are used below but not installed: ", paste(.I4R_MISSING, collapse = ", "),
  "\nInstall them, then run this file again: install.packages(", deparse(.I4R_MISSING), ")",
  "\n(a Bioconductor package installs with BiocManager::install()). Until then the claims that need them FAIL.")
suppressPackageStartupMessages(for (.p in c("knitr", __LIBS__)) if (requireNamespace(.p, quietly = TRUE)) library(.p, character.only = TRUE))

## Resolve the deposit WITHOUT hardcoding the auditor's machine: I4R_PKG_DIR, else a pkg/ folder beside the
## working directory (or the audit layout's), else stop.
PKG_DIR <- local({
  e <- Sys.getenv("I4R_PKG_DIR", "")
  if (nzchar(e) && dir.exists(e)) return(normalizePath(e))
  for (cand in c(file.path(getwd(), "pkg"), file.path(dirname(getwd()), "audit", "pkg"), "pkg"))
    if (dir.exists(cand)) return(normalizePath(cand))
  stop("cannot locate the deposit: set I4R_PKG_DIR to the extracted package directory (the pkg/ folder)")
})
AUDIT_DIR <- local({
  e <- Sys.getenv("I4R_AUDIT_DIR", "")
  if (nzchar(e) && dir.exists(e)) return(normalizePath(e))
  normalizePath(dirname(PKG_DIR))
})
SNIPS_DIR <- local({
  e <- Sys.getenv("I4R_SNIPS_DIR", "")
  if (nzchar(e) && dir.exists(e)) return(e)
  cand <- file.path(AUDIT_DIR, "verification_snippets")
  if (dir.exists(cand)) normalizePath(cand) else cand
})
## The notebook's chunks run from the audit folder; so do these checks.
setwd(AUDIT_DIR)
__PAPER_NOTE__

## ---- read-only guard: pkg/ is the authors' deposit. Fingerprint on entry, re-check in the summary; scratch goes
## to tempdir(), never beside the deposit.
.pkg_manifest <- function() {
  fs <- list.files(PKG_DIR, recursive = TRUE, all.files = TRUE, no.. = TRUE, full.names = TRUE)
  data.frame(file = fs, size = file.size(fs), md5 = unname(tools::md5sum(fs)), stringsAsFactors = FALSE)
}
.PKG_ON_ENTRY <- .pkg_manifest()
SCRATCH <- tempfile("verif_"); dir.create(SCRATCH)

## ---- deposit loader: always R, whatever the package was written in.
read_deposit <- function(file, ...) {
  path <- if (file.exists(file.path(PKG_DIR, file))) file.path(PKG_DIR, file) else file
  if (!file.exists(path)) stop("not in the deposit: ", file)
  ext <- tolower(tools::file_ext(path))
  switch(ext,
    xlsx = , xls  = readxl::read_excel(path, ...),
    dta          = haven::read_dta(path, ...),
    sav = , por  = haven::read_sav(path, ...),
    sas7bdat     = haven::read_sas(path, ...),
    omv          = read_omv_deposit(path, ...),
    csv          = utils::read.csv(path, ...),
    tsv = , txt  = utils::read.delim(path, ...),
    rds          = readRDS(path),
    rdata = , rda = { e <- new.env(); load(path, envir = e); as.list(e) },
    parquet      = arrow::read_parquet(path, ...),
    stop("no loader for .", ext, " -- add one to read_deposit()")
  )
}
## A jamovi .omv is a zip: prefer the package; fall back to the zip so a missing jmvReadWrite does not block a check.
read_omv_deposit <- function(path, ...) {
  if (requireNamespace("jmvReadWrite", quietly = TRUE))
    return(jmvReadWrite::read_omv(path, ...))
  message("jmvReadWrite unavailable; reading the .omv as a zip archive")
  ex <- file.path(SCRATCH, paste0("omv_", basename(path)))
  dir.create(ex, showWarnings = FALSE); utils::unzip(path, exdir = ex)
  list(parts = list.files(ex, recursive = TRUE, full.names = TRUE))
}

## ---- expectation / check machinery: the notebook's verdict rules, one printed line per value.
.V <- new.env(parent = emptyenv())
.V$exp <- list(); .V$obs <- list(); .V$state <- list(); .V$order <- character(0); .V$current <- NULL
## A claim's block may record outcomes only under its own claim id.
.bind_claim <- function(id) {
  if (!is.null(.V$current) && !identical(id, .V$current))
    stop(sprintf("the %s block recorded an outcome under %s", .V$current, id))
}
.fmt <- function(x) {
  v <- if (is.numeric(x)) {
    ifelse(is.na(x), "NA",
      ifelse(x == round(x) & abs(x) < 1e15, format(x, big.mark = ",", scientific = FALSE),
             format(round(x, 6), nsmall = 0, scientific = FALSE)))
  } else as.character(x)
  paste(v, collapse = ", ")
}
.show <- function(x) if (is.null(x)) "NULL" else paste(format(x), collapse = ", ")
## expect() runs BEFORE anything is computed and prints the prediction.
expect <- function(id, ..., tol = 0, note = NULL) {
  .bind_claim(id)
  vals <- list(...); if (is.null(names(vals)) || any(names(vals) == "")) stop("expect(): every value needs a name")
  for (k in names(vals)) {
    key <- paste(id, k, sep = "|")
    .V$exp[[key]] <- list(id = id, key = k, expected = vals[[k]], tol = tol, note = note)
    if (!key %in% .V$order) .V$order <- c(.V$order, key)
    cat(sprintf("[%-6s %-34s] EXPECTED  %s\n", id, k, .show(vals[[k]])))
  }
  invisible(TRUE)
}
## check() prints the observation and the verdict, and never stops the run.
check <- function(id, ...) {
  .bind_claim(id)
  vals <- list(...); if (is.null(names(vals)) || any(names(vals) == "")) stop("check(): every value needs a name")
  for (k in names(vals)) {
    key <- paste(id, k, sep = "|"); e <- .V$exp[[key]]; ob <- vals[[k]]
    if (is.null(e)) {
      .V$obs[[key]] <- list(id = id, key = k, observed = vals[[k]], verdict = "FAIL", detail = "never declared with expect()")
      if (!key %in% .V$order) .V$order <- c(.V$order, key)
      cat(sprintf("[%-6s %-34s] FAIL  undeclared\n", id, k)); next
    }
    ex <- e$expected; tol <- e$tol
    if (is.numeric(ex) && is.numeric(ob) && length(ex) == length(ob)) {
      d <- max(abs(ex - ob)); ok <- isTRUE(all(!is.na(d)) && d <= tol + 1e-12)
    } else ok <- identical(as.character(ex), as.character(ob))
    .V$obs[[key]] <- list(id = id, key = k, observed = ob, verdict = if (ok) "PASS" else "FAIL")
    cat(sprintf("[%-6s %-34s] OBSERVED  %s  %s\n", id, k, .show(ob), if (ok) "PASS" else "FAIL"))
  }
  invisible(TRUE)
}
## Reach back into an earlier claim's observation.
observed_of <- function(claim, key) {
  o <- .V$obs[[paste(claim, key, sep = "|")]]
  if (is.null(o)) NA else o$observed
}
## The claim rests on an assessment, stated so a replicator can reject it on the merits.
judgement <- function(id, statement, basis = "") {
  .bind_claim(id)
  .V$state[[id]] <- list(id = id, verdict = "JUDGEMENT")
  cat(sprintf("[%-6s %-34s] JUDGEMENT  %s\n", id, "-", statement))
  if (nzchar(basis)) cat(sprintf("[%-6s %-34s] basis      %s\n", id, "-", basis))
  invisible(TRUE)
}
## The deposit cannot settle it; the reason is given.
unverifiable <- function(id, reason, observed = NULL) {
  .bind_claim(id)
  .V$state[[id]] <- list(id = id, verdict = "UNVERIFIABLE")
  cat(sprintf("[%-6s %-34s] UNVERIFIABLE  %s\n", id, "-", reason))
  invisible(TRUE)
}
## A runtime error inside a claim's block is that claim's FAIL, never the end of the run.
runtime_failure <- function(id, error) {
  .V$state[[id]] <- list(id = id, verdict = "FAIL")
  cat(sprintf("[%-6s %-34s] FAIL  runtime error: %s\n", id, "-", conditionMessage(error)))
  invisible(FALSE)
}
claims_summary <- function(claims = character(0)) {
  for (key in .V$order) if (is.null(.V$obs[[key]]))
    .V$obs[[key]] <- list(id = .V$exp[[key]]$id, key = .V$exp[[key]]$key, verdict = "FAIL")
  verdicts <- vapply(.V$obs, function(o) o$verdict, character(1))
  states <- vapply(.V$state, function(s) s$verdict, character(1))
  recorded <- unique(c(vapply(.V$obs, function(o) o$id, character(1)), names(.V$state)))
  silent <- setdiff(claims, recorded)
  foreign <- if (length(claims)) setdiff(recorded, claims) else character(0)
  after <- .pkg_manifest()
  sorted <- function(m) { m <- m[order(enc2utf8(m$file), method = "radix"), c("file", "size", "md5")]; rownames(m) <- NULL; m }
  same <- identical(sorted(.PKG_ON_ENTRY), sorted(after))
  prelude_ok <- isTRUE(get0(".I4R_PRELUDE_OK", ifnotfound = TRUE))
  n_pass <- sum(verdicts == "PASS")
  n_fail <- sum(verdicts != "PASS") + sum(states == "FAIL") + length(silent) + length(foreign) + (!same) + (!prelude_ok)
  cat(sprintf("\n%d checks, %d PASS, %d FAIL\n", n_pass + n_fail, n_pass, n_fail))
  cat(sprintf("claims marked JUDGEMENT: %d; UNVERIFIABLE: %d\n", sum(states == "JUDGEMENT"), sum(states == "UNVERIFIABLE")))
  if (!prelude_ok) cat("the shared helpers (_prelude.R) stopped with an error; claims that use them FAIL\n")
  if (any(states == "FAIL")) cat("claims that stopped with an error:", paste(names(states)[states == "FAIL"], collapse = ", "), "\n")
  if (length(silent)) cat("claims that recorded no outcome:", paste(silent, collapse = ", "), "\n")
  if (length(foreign)) cat("outcomes recorded under ids that are not claims here:", paste(foreign, collapse = ", "), "\n")
  cat(sprintf("deposit unmodified: %s\n", if (same) "yes" else "NO -- INVESTIGATE"))
  invisible(n_fail)
}
""".replace("__NEED__", r_vec(need)).replace("__LIBS__", ", ".join(f'"{x}"' for x in loaders) or "NULL").replace(
        "__PAPER_NOTE__", ('if (!file.exists(file.path(AUDIT_DIR, "paper", "paper.txt"))) message(\n'
                           '  "Some checks read the article at ", file.path(AUDIT_DIR, "paper", "paper.txt"), ": put the PDF at ",\n'
                           '  "paper/paper.pdf beside the deposit folder and run pdftotext -layout paper/paper.pdf paper/paper.txt ",\n'
                           '  "(or set I4R_AUDIT_DIR to the folder holding paper/). Until then those claims FAIL.")')
        if reads_paper else "")
    if PORTABLE:   # 2.7.11 (publication.claims_script_harness): each 2.7.10 passage below is replaced exactly once
        for old, new in PORTABLE_SETUP:
            if setup.count(old) != 1:
                print("FATAL: the 2.7.11 claims-script harness no longer matches its 2.7.10 base; fix PORTABLE_SETUP")
                sys.exit(1)
            setup = setup.replace(old, new.replace("__RVERSION__", rversion_r).replace("__TOOLS__", tools_r))
    harness = [MK + " ---- standalone check harness: the verification notebook's setup chunk -------------------------"]
    harness += setup.strip("\n").split("\n")
    if helpers:
        harness += [MK + " ---- helper files the checks read from the snippets folder, carried so the script runs on its own",
                    'SNIPS_DIR <- file.path(SCRATCH, "verification_snippets")',
                    '.i4r_helper <- function(rel, text) { f <- file.path(SNIPS_DIR, rel)',
                    '  dir.create(dirname(f), recursive = TRUE, showWarnings = FALSE); cat(text, file = f, sep = "") }']
        for rel, text in helpers:
            n = 1
            while f']{"-" * n}"' in text:
                n += 1
            harness += [f'.i4r_helper("{rel}", r"{"-" * n}[' + text.split("\n")[0]] + text.split("\n")[1:-1] + \
                       [text.split("\n")[-1] + f']{"-" * n}")'] if "\n" in text else \
                       [f'.i4r_helper("{rel}", r"{"-" * n}[{text}]{"-" * n}")']
    if prelude.is_file():
        harness += [MK + " ---- audit-specific helpers (verification_snippets/_prelude.R) ----"]
        harness += [".I4R_PRELUDE_OK <- tryCatch({"] + prelude.read_text().rstrip("\n").split("\n") + \
                   ['TRUE }, error = function(e) { cat(sprintf("[%-6s %-34s] FAIL  runtime error: %s\\n", "prelude", "-", '
                    'conditionMessage(e))); FALSE })']
    harness += [MK + " " + "="*92, ""]
out = "\n".join(hdr_lines) + "\n" + "\n".join(harness) + "\n".join(body)
if EXT == "R" and uses_snippet:
    out += "\n" + MK + " ---- summary of every audit-written check above ----\nclaims_summary(" + \
           ("c(" + ", ".join(f'"{c}"' for c in code_ids) + ")" if code_ids else "") + ")\n"
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
for i, fid, refs in internal_notes:
    print(f"NOTE: claim {i}'s manual check names the audit's working files ({', '.join(refs)}), which a replicator does "
          f"not have; the script marks them as not published. Better: reword finding {fid}'s evidence.operation as a "
          f"check on the deposit and the article.")
PY
