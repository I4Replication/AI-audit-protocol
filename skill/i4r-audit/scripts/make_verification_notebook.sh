#!/bin/bash
# make_verification_notebook.sh <audit-dir> [-o out.Rmd] [-s snippets-dir] [-t title]
#
# Emits an R Markdown notebook that lets a human who did not run the audit re-check every
# numerical claim the comment makes, one section per claim.
#
# Two rules the notebook enforces mechanically, because vigilance did not:
#
#   1. THE EXPECTATION IS PRINTED BEFORE THE OBSERVATION EXISTS. Each section calls
#      expect(id, key = value, ...) first; that call prints the predicted number and returns.
#      Only then does the chunk compute anything, and check(id, key = observed) prints the
#      observation and the verdict. A verifier reading the knitted output can never mistake
#      "whatever the code printed" for "what the comment said would print". A key that is
#      declared and never checked is a FAIL, not a silence.
#
#   2. FAILURES ARE COLLECTED, NOT THROWN. One bad check does not abort the run: every
#      section executes, and the last chunk prints the whole table. The knit still ends in a
#      nonzero process exit when anything failed (see reference/verification_guide.md for the
#      one-line invocation that propagates it).
#
# The script writes the frame — header, setup chunk with the deposit loader, one section per
# claim with its provenance, and the summary chunk. It cannot write the check body for every
# claim. Where it has no snippet it emits a TODO stub carrying the claim text, the section
# number, the auditor and the adjudication entry, and every numeral found in the claim, so the
# coordinator finishes it rather than the claim being dropped. The stub count is reported on
# stderr and is the number to drive to zero.
#
# Snippets live in <snippets-dir> (default <audit-dir>/verification_snippets) and are keyed by
# claim id:
#     C-2.5.R              R code, inlined verbatim into the section's chunk
#     C-2.12.unverifiable  first line is the reason; claim cannot be checked from the deposit
#     C-7.8.judgement      first line is the judgement, remaining lines the basis for it
# Three states, not two: a claim resting on an assessment rather than an arithmetic result is
# marked JUDGEMENT and states what the judgement was, so a verifier can disagree on the merits.
set -uo pipefail

AUDIT=""; OUT=""; SNIPS=""; TITLE=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2;;
    -s) SNIPS="$2"; shift 2;;
    -t) TITLE="$2"; shift 2;;
    -*) echo "unknown option $1" >&2; exit 2;;
    *)  AUDIT="$1"; shift;;
  esac
done
[ -n "$AUDIT" ] || { echo "usage: make_verification_notebook.sh <audit-dir> [-o out.Rmd] [-s snippets-dir] [-t title]" >&2; exit 2; }
[ -d "$AUDIT" ] || { echo "no such audit directory: $AUDIT" >&2; exit 2; }
AUDIT="$(cd "$AUDIT" && pwd)"
OUT="${OUT:-$AUDIT/verification.Rmd}"
SNIPS="${SNIPS:-$AUDIT/verification_snippets}"

REPORT="$AUDIT/report/REPORT_final.md"
[ -f "$REPORT" ] || REPORT="$AUDIT/report/REPORT_v1.md"
[ -f "$REPORT" ] || { echo "no REPORT_final.md or REPORT_v1.md under $AUDIT/report" >&2; exit 2; }
ADJ="$AUDIT/report/ADJUDICATION.md"
[ -f "$ADJ" ] || { echo "WARN: no ADJUDICATION.md — provenance will be blank" >&2; ADJ=/dev/null; }
PKG="$AUDIT/pkg"
[ -d "$PKG" ] || { echo "no pkg/ under $AUDIT — the notebook has nothing to read" >&2; exit 2; }

python3 - "$REPORT" "$ADJ" "$PKG" "$SNIPS" "$OUT" "$TITLE" <<'PYGEN'
import sys, re, os, pathlib, html

report_p, adj_p, pkg_p, snips_p, out_p, title = sys.argv[1:7]
report = pathlib.Path(report_p).read_text()
adj    = pathlib.Path(adj_p).read_text() if os.path.exists(adj_p) else ""

# ---------------------------------------------------------------- claim extraction
# A claim is a bolded numbered lead: **2.5 The analytic sample recovers...** or **A2. Recovered...**
# plus, from the abstract, each sentence carrying a numeral. Abstract sentences are claims in
# their own right: they are the version of the finding that most readers will ever see, and in
# calibration a correction reached the body and not the abstract three separate times.
def para_split(s):
    return re.split(r"\n\s*\n", s)

section_of = {}
cur_sec = "front matter"
claims = []

for line in report.split("\n"):
    m = re.match(r"^##\s+(\d+)\.\s+(.*)$", line)
    if m: cur_sec = f"Section {m.group(1)} — {m.group(2).strip()}"
    m = re.match(r"^##\s+(Appendix\s+A)\.?\s*(.*)$", line)
    if m: cur_sec = "Appendix A"
    m = re.match(r"^##\s+(Abstract|Executive summary)\s*$", line)
    if m: cur_sec = "Abstract"

# re-walk, tracking the section each paragraph belongs to
cur_sec = "front matter"
for para in para_split(report):
    first = para.strip().split("\n")[0]
    m = re.match(r"^##\s+(\d+)\.\s+(.*)$", first)
    if m: cur_sec = f"Section {m.group(1)}"
    elif re.match(r"^##\s+Appendix", first): cur_sec = "Appendix A"
    elif re.match(r"^##\s+(Abstract|Executive summary)", first): cur_sec = "Abstract"
    elif re.match(r"^##\s+", first): cur_sec = first.lstrip("# ").strip()
    flat = " ".join(para.split())
    m = re.match(r"\*\*([0-9]+\.[0-9]+|A[0-9]+)\.?\s+(.+?)\*\*(.*)$", flat)
    if m:
        cid = "C-" + m.group(1)
        claims.append(dict(id=cid, head=m.group(2).strip(), body=m.group(3).strip(),
                           sec=cur_sec, full=flat))
    # Merged page-1 format: findings are tagged inline as [claim N] inside a section body.
    for cm in re.finditer(r"\[claim (\d+)\]", flat):
        n = int(cm.group(1))
        if any(c["id"] == f"C-{n}" for c in claims): continue
        # A tag sits either INSIDE its sentence ("... 59 of 72 [claim 6] rows ...") or AFTER that
        # sentence's full stop ("... 59 of 72 rows. [claim 6]"). Both occur. Taking the text that
        # follows the tag unconditionally attaches every claim to the NEXT sentence, which is an
        # off-by-one that survives every gate: the notebook still has one section per tag, so the
        # count is right and the content is wrong. Decide by looking at what precedes the tag.
        # Sentence ends are terminators followed by whitespace or end-of-text. A bare `.` will
        # not do: "0.58" and "p = .0004" are the substance of these claims, and splitting on the
        # decimal point silently truncates the sentence to its own tail.
        ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", flat)]
        befo = [e for e in ends if e <= cm.start()]
        lo   = befo[-1] if befo else 0
        lead = flat[lo:cm.start()].strip()
        if lead:
            afte = [e for e in ends if e > cm.end()]
            sent = flat[lo:(afte[0] if afte else len(flat))]
        else:
            # nothing but whitespace between the previous full stop and the tag: the tag trails
            # its own sentence, so that sentence is the one before `lo`.
            sent = flat[(befo[-2] if len(befo) > 1 else 0):lo]
        sent = re.sub(r"\s*\[claim \d+\]\s*", " ", sent).strip()
        claims.append(dict(id=f"C-{n}", head=sent, body="", sec=cur_sec, full=sent))

# abstract sentences carrying a numeral
NUMWORD = r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty-four|thirty)\b"
am = re.search(r"##\s+(?:Abstract|Executive summary)\s*\n(.*?)(?=\n(?:##\s|\*\*Table))", report, re.S)
if am:
    abst = " ".join(am.group(1).split())
    n = 0
    for sent in re.split(r"(?<=[.!?])\s+", abst):
        if not sent.strip(): continue
        if re.search(r"\d", sent) or re.search(NUMWORD, sent, re.I):
            n += 1
            claims.append(dict(id=f"C-ABS.{n}", head=sent.strip(), body="",
                               sec="Abstract", full=sent.strip()))

def sortkey(c):
    tail = c["id"][2:]
    # abstract claims run LAST: they are cross-references to body results, and the
    # cross-reference is only meaningful once the body check has produced an observation.
    if tail.startswith("ABS."): return (3, 0, int(tail.split(".")[1]))
    if tail.startswith("A"):    return (2, int(tail[1:]), 0)
    if "." not in tail:         return (1, int(tail), 0)
    a, b = tail.split(".")
    return (1, int(a), int(b))
claims.sort(key=sortkey)

# ---------------------------------------------------------------- provenance
# Adjudication entries look like:  ## A8. THE LEAD FINDING — ... — A4, CONFIRMED
# The trailing "A4" is the auditor; the leading "A8" is the adjudication entry id.
adj_entries = []
for block in re.split(r"\n(?=##\s)", adj):
    head = block.strip().split("\n")[0]
    m = re.match(r"^##\s+([A-Za-z0-9-]+)\.?\s*(.*)$", head)
    if not m: continue
    eid, etitle = m.group(1), m.group(2)
    # "— A5, CONFIRMED", "— A4 independently", "HANDED TO A1 AND A2", "-- A2"
    auditors = re.findall(r"[—–-]{1,2}\s*A([1-5])\b", block)
    auditors += re.findall(r"\bA([1-5])\b\s*(?:,\s*(?:CONFIRMED|NEGATIVE)|independently|handled|succeeded|is testing)", block)
    auditors += re.findall(r"HANDED TO ((?:A[1-5](?: AND )?)+)", block) and \
                re.findall(r"A([1-5])", " ".join(re.findall(r"HANDED TO ((?:A[1-5](?: AND )?)+)", block)))
    adj_entries.append(dict(id=eid, title=etitle, text=block,
                            auditors=sorted(set(auditors), key=int)))

STOP = set("""the a an of and or to in is are was were it its their this that with for on at by as
from be been has have had not no our we they he she which what when where than then so such but if
these those all any both each more most other some only own same too very can will just from""".split())
def toks(s):
    return set(w for w in re.findall(r"[a-z]{4,}", s.lower()) if w not in STOP)

for c in claims:
    ct = toks(c["full"])
    best, bs = None, 0.0
    for e in adj_entries:
        et = toks(e["title"] + " " + e["text"][:1200])
        if not et: continue
        s = len(ct & et) / (len(ct) ** 0.5 + 1e-9)
        if s > bs: bs, best = s, e
    if best is not None and bs >= 0.85:
        c["adj"] = best["id"]
        c["auditors"] = best["auditors"]
        c["adj_title"] = best["title"][:110]
    else:
        c["adj"] = None
        c["auditors"] = []
        c["adj_title"] = ""

# ---------------------------------------------------------------- numerals in the claim
def numerals(s):
    out = []
    for m in re.finditer(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(percent|%|children|participants|of\s+[\d,]+)?", s):
        v = m.group(1)
        if len(v.replace(",", "")) == 1 and not m.group(2): continue
        out.append(m.group(0).strip())
    seen, uniq = set(), []
    for v in out:
        if v not in seen: seen.add(v); uniq.append(v)
    return uniq[:24]

# ---------------------------------------------------------------- deposit inventory
LOADERS = {".xlsx": "readxl", ".xls": "readxl", ".dta": "haven", ".sav": "haven",
           ".por": "haven", ".omv": "jamovi", ".csv": "csv", ".tsv": "csv", ".txt": "csv",
           ".rds": "rds", ".rdata": "rdata", ".rda": "rdata", ".sas7bdat": "haven",
           ".json": "json", ".parquet": "arrow"}
deposit = []
for root, _, files in os.walk(pkg_p):
    for f in sorted(files):
        p = os.path.join(root, f)
        ext = os.path.splitext(f)[1].lower()
        deposit.append((os.path.relpath(p, pkg_p), ext, LOADERS.get(ext, "unknown"),
                        os.path.getsize(p)))
deposit.sort()
needed = sorted(set(l for _, _, l, _ in deposit if l in ("readxl", "haven", "jamovi", "arrow")))
pkgmap = {"readxl": "readxl", "haven": "haven", "jamovi": "jmvReadWrite", "arrow": "arrow"}
req = ["knitr", "rmarkdown"] + [pkgmap[n] for n in needed]

# ---------------------------------------------------------------- snippets
def snippet(cid):
    base = os.path.join(snips_p, cid)
    for ext, kind in ((".R", "code"), (".r", "code"), (".unverifiable", "unverifiable"),
                      (".judgement", "judgement")):
        if os.path.exists(base + ext):
            return kind, pathlib.Path(base + ext).read_text().rstrip("\n")
    return None, None

# ---------------------------------------------------------------- emit
title = title or "Verification notebook"
L = []
w = L.append

w("---")
w(f'title: "{title}"')
w('output:')
w('  html_document:')
w('    toc: true')
w('    toc_depth: 2')
w('    df_print: kable')
w('---')
w("")
w("```{r meta, include=FALSE}")
w('knitr::opts_chunk$set(echo = TRUE, error = TRUE, comment = "", message = FALSE, warning = FALSE)')
w("```")
w("")
w("## What you are being asked to do")
w("")
w("This notebook re-checks, from the deposited replication package alone, every numerical claim")
w("made by the comment named below. You did not run the audit and have no reason to trust it.")
w("Each section states a claim, where it sits in the comment, who produced it, and **the number")
w("the comment predicts**, and only then computes anything. Run the whole notebook and read the")
w("summary table at the end. Every claim ends in one of four states: **PASS** (reproduced within")
w("the stated tolerance), **FAIL** (did not), **JUDGEMENT** (an assessment, not an arithmetic")
w("result — the judgement is stated so you can disagree with it), or **UNVERIFIABLE** (cannot be")
w("checked from the deposit, with the reason given). A claim with no section is a bug in the")
w("generator, not an absence of a claim; the summary counts unfinished stubs separately.")
w("")
w(f"- Comment verified: `{os.path.basename(report_p)}`")
w(f"- Adjudication log: `{os.path.basename(adj_p)}`")
w(f"- Deposited package: `<audit-dir>/pkg` (override with $I4R_PKG_DIR) — **read-only**. The notebook reads it and never writes into it;")
w("  the setup chunk fingerprints every file and the summary chunk re-checks the fingerprints.")
w("- R version used to build this notebook: 4.5.2. Any 4.x should do.")
w("")
w("### Deposit inventory")
w("")
w("| File | Loader |")
w("|---|---|")
for rel, ext, loader, size in deposit:
    w(f"| `{rel}` | `{loader}` ({size:,} bytes) |")
w("")
w("### Required packages")
w("")
w("```{r install, eval=FALSE}")
w("# run once if anything below is missing")
w("need <- c(" + ", ".join(f'"{p}"' for p in dict.fromkeys(req)) + ")")
w('install.packages(setdiff(need, rownames(installed.packages())))')
w("```")
w("")
w("## Setup")
w("")
w("```{r setup}")

setup = r'''
suppressPackageStartupMessages({
  library(knitr)
__LIBS__
})

# Resolve the deposit WITHOUT hardcoding the auditor's machine. A baked absolute path does two bad
# things: it leaks the auditor's account name into a released artefact, and it makes the notebook
# unrunnable for the verifier it exists to serve. Order: env var, then the audit dir's own pkg/,
# then the recorded path as a last-resort fallback.
PKG_DIR <- local({
  e <- Sys.getenv("I4R_PKG_DIR", "")
  if (nzchar(e) && dir.exists(e)) return(e)
  for (cand in c("pkg", file.path("..", "pkg"), file.path("..", "..", "pkg")))
    if (dir.exists(cand)) return(normalizePath(cand))
  stop("cannot locate the deposit. Set I4R_PKG_DIR to the extracted package directory, ",
       "or run this notebook from the audit directory that contains pkg/.")
})
stopifnot(dir.exists(PKG_DIR))

## The audit directory and the snippet directory, derived the same portable way as PKG_DIR.
## Snippets are inlined verbatim into this notebook, so a snippet that needs a sibling file
## (a shared loader, the paper text) has no other way to name one.
AUDIT_DIR <- local({
  e <- Sys.getenv("I4R_AUDIT_DIR", "")
  if (nzchar(e) && dir.exists(e)) return(e)
  normalizePath(dirname(PKG_DIR))
})
SNIPS_DIR <- local({
  e <- Sys.getenv("I4R_SNIPS_DIR", "")
  if (nzchar(e) && dir.exists(e)) return(e)
  cand <- file.path(AUDIT_DIR, "verification_snippets")
  if (dir.exists(cand)) normalizePath(cand) else cand
})

## ---- read-only guard -------------------------------------------------------
## pkg/ is the authors' deposit. In calibration an auditor's own scripts grew the archive by
## nineteen files and a co-auditor nearly filed a finding against the authors for it. Fingerprint
## on entry, re-check on exit; scratch goes to tempdir(), never beside the deposit.
.pkg_manifest <- function() {
  fs <- list.files(PKG_DIR, recursive = TRUE, all.files = TRUE, no.. = TRUE, full.names = TRUE)
  data.frame(file = fs, size = file.size(fs),
             md5 = unname(tools::md5sum(fs)), stringsAsFactors = FALSE)
}
.PKG_ON_ENTRY <- .pkg_manifest()
SCRATCH <- tempfile("verif_"); dir.create(SCRATCH)

## ---- deposit loader --------------------------------------------------------
## Always R, whatever the package was written in. Format dispatch lives here so that the
## per-claim chunks below are identical in shape across audits.
read_deposit <- function(file, ...) {
  path <- if (file.exists(file)) file else file.path(PKG_DIR, file)
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
    stop("no loader for .", ext, " — add one to read_deposit()")
  )
}

## A jamovi .omv is a zip: metadata in meta/, data in a binary or a CSV-ish part. Prefer the
## package; fall back to the zip so a missing jmvReadWrite does not block verification.
read_omv_deposit <- function(path, ...) {
  if (requireNamespace("jmvReadWrite", quietly = TRUE))
    return(jmvReadWrite::read_omv(path, ...))
  message("jmvReadWrite unavailable; reading the .omv as a zip archive")
  ex <- file.path(SCRATCH, paste0("omv_", basename(path)))
  dir.create(ex, showWarnings = FALSE); utils::unzip(path, exdir = ex)
  list(parts = list.files(ex, recursive = TRUE, full.names = TRUE))
}

## ---- expectation / check machinery ----------------------------------------
.V <- new.env(parent = emptyenv())
.V$exp   <- list()   # id|key -> expected, tol, claim
.V$obs   <- list()   # id|key -> observed, verdict
.V$state <- list()   # id     -> JUDGEMENT / UNVERIFIABLE marker
.V$order <- character(0)

.fmt <- function(x) {
  if (is.numeric(x)) {
    ifelse(is.na(x), "NA",
      ifelse(x == round(x) & abs(x) < 1e15, format(x, big.mark = ",", scientific = FALSE),
             format(round(x, 6), nsmall = 0, scientific = FALSE)))
  } else as.character(x)
}
.line <- function(id, key, label, value) {
  cat(sprintf("[%-10s %-22s] %-9s %s\n", id, key, label, .fmt(value)))
}

## expect() runs BEFORE anything is computed and prints the prediction. That ordering is the
## point of the notebook: a verifier compares a prediction against a result, never reads back
## whatever the code happened to produce.
expect <- function(id, ..., tol = 0, note = NULL) {
  vals <- list(...)
  if (is.null(names(vals)) || any(names(vals) == "")) stop("expect(): every value needs a name")
  cat(sprintf("---- %s : expected -------------------------------------------------\n", id))
  if (!is.null(note)) cat("     note: ", note, "\n", sep = "")
  for (k in names(vals)) {
    key <- paste0(id, "|", k)
    .V$exp[[key]] <- list(id = id, key = k, expected = vals[[k]], tol = tol, note = note)
    if (!key %in% .V$order) .V$order <- c(.V$order, key)
    .line(id, k, "EXPECTED", vals[[k]])
  }
  invisible(TRUE)
}

## check() prints the observation and the verdict. It never stops the run: a failure that
## aborts the notebook hides every claim after it, which is the opposite of what is wanted.
check <- function(id, ...) {
  vals <- list(...)
  if (is.null(names(vals)) || any(names(vals) == "")) stop("check(): every value needs a name")
  for (k in names(vals)) {
    key <- paste0(id, "|", k)
    e <- .V$exp[[key]]
    if (is.null(e)) {
      .V$obs[[key]] <- list(id = id, key = k, observed = vals[[k]], verdict = "FAIL",
                            detail = "observed value was never declared with expect()")
      if (!key %in% .V$order) .V$order <- c(.V$order, key)
      .line(id, k, "OBSERVED", vals[[k]])
      cat(sprintf("[%-10s %-22s] %-9s %s\n", id, k, "VERDICT", "*** FAIL *** undeclared expectation"))
      next
    }
    ex <- e$expected; ob <- vals[[k]]; tol <- e$tol
    if (is.numeric(ex) && is.numeric(ob) && length(ex) == length(ob)) {
      d <- max(abs(ex - ob)); ok <- isTRUE(all(!is.na(d)) && d <= tol + 1e-12)
      detail <- sprintf("|diff| %s, tol %s", .fmt(d), .fmt(tol))
    } else {
      ok <- identical(as.character(ex), as.character(ob))
      detail <- if (ok) "exact match" else "string mismatch"
    }
    .V$obs[[key]] <- list(id = id, key = k, observed = ob,
                          verdict = if (ok) "PASS" else "FAIL", detail = detail)
    .line(id, k, "OBSERVED", ob)
    if (ok) {
      cat(sprintf("[%-10s %-22s] %-9s PASS   (%s)\n", id, k, "VERDICT", detail))
    } else {
      cat(sprintf("[%-10s %-22s] %-9s *** FAIL ***   (%s)\n", id, k, "VERDICT", detail))
      cat(strrep("!", 78), "\n")
      cat("!!  CLAIM ", id, " KEY ", k, " DID NOT REPRODUCE\n", sep = "")
      cat("!!  expected ", .fmt(ex), "   observed ", .fmt(ob), "   ", detail, "\n", sep = "")
      cat(strrep("!", 78), "\n")
    }
  }
  invisible(TRUE)
}

## Reach back into an earlier section's observation. Abstract claims are cross-references:
## the question is whether the public summary carries the number the body actually produced.
observed_of <- function(claim, key) {
  o <- .V$obs[[paste0(claim, "|", key)]]
  if (is.null(o)) NA else o$observed
}

## Third state, distinct from both a check and a silence: the claim rests on an assessment.
## State the assessment so the verifier can reject it on the merits.
judgement <- function(id, statement, basis = "") {
  .V$state[[id]] <- list(id = id, verdict = "JUDGEMENT", detail = statement, basis = basis)
  cat(sprintf("---- %s : JUDGEMENT ------------------------------------------------\n", id))
  cat("     assessment: ", statement, "\n", sep = "")
  if (nzchar(basis)) cat("     basis:      ", basis, "\n", sep = "")
  cat("     This is not an arithmetic result. Disagree with it on the merits if you do.\n")
  invisible(TRUE)
}

## Fourth outcome, and never a silence: the deposit cannot settle it.
unverifiable <- function(id, reason, observed = NULL) {
  .V$state[[id]] <- list(id = id, verdict = "UNVERIFIABLE", detail = reason, basis = "")
  cat(sprintf("---- %s : UNVERIFIABLE ---------------------------------------------\n", id))
  cat("     reason: ", reason, "\n", sep = "")
  if (!is.null(observed))
    cat("     for information, this pipeline produces: ", .fmt(observed), "\n", sep = "")
  invisible(TRUE)
}
'''
libs = "\n".join(f"  library({p})" for p in dict.fromkeys(pkgmap[n] for n in needed)) or "  invisible(NULL)"
setup = setup.replace("__LIBS__", libs).replace("__PKG__", pkg_p)
w(setup.strip())
# audit-specific shared helpers (analytic-frame construction, imputers, figure transcriptions)
prelude = os.path.join(snips_p, "_prelude.R")
if os.path.exists(prelude):
    w("")
    w("## ---- audit-specific helpers (from _prelude.R) ------------------------------")
    w(pathlib.Path(prelude).read_text().rstrip("\n"))
w("```")
w("")

stub_ids = []
for c in claims:
    kind, body = snippet(c["id"])
    prov_auditor = ", ".join("A" + a for a in c["auditors"]) or "not recorded in the adjudication log"
    prov_adj = (f"`{c['adj']}` — {c['adj_title']}" if c["adj"] else
                "no adjudication entry matched this claim")
    short = c["head"] if len(c["head"]) <= 92 else c["head"][:89].rstrip() + "..."
    w(f"## {c['id']} — {short}")
    w("")
    w(f"> {c['head']}" + (f" {c['body']}" if c["body"] else ""))
    w("")
    w(f"- **Where in the comment:** {c['sec']}")
    w(f"- **Found by:** {prov_auditor}")
    w(f"- **Settled in adjudication:** {prov_adj}")
    nums = numerals(c["full"])
    if nums:
        w("- **Figures asserted:** " + ", ".join(f"`{n}`" for n in nums))
    w("")
    w(f"```{{r {c['id'].replace('.', '_').replace('-', '_')}}}")
    if kind == "code":
        w(body)
    elif kind == "unverifiable":
        lines = body.split("\n")
        reason = lines[0].strip().replace('"', "'")
        w(f'unverifiable("{c["id"]}",')
        w(f'  "{reason}")')
        for extra in lines[1:]:
            if extra.strip(): w("# " + extra)
    elif kind == "judgement":
        lines = [l for l in body.split("\n") if l.strip()]
        stmt = lines[0].strip().replace('"', "'")
        basis = " ".join(l.strip() for l in lines[1:]).replace('"', "'")
        w(f'judgement("{c["id"]}",')
        w(f'  "{stmt}",')
        w(f'  "{basis}")')
    else:
        stub_ids.append(c["id"])
        w("## ------------------------------------------------------------------------")
        w("## TODO — STUB. This claim has no check yet. It is NOT verified.")
        w("## Replace this block with expect(...) / check(...), or with judgement(...) or")
        w("## unverifiable(...) if that is the honest state. Do not delete the section.")
        w("##")
        w(f"## claim   : {c['head'][:150]}")
        w(f"## section : {c['sec']}")
        w(f"## auditor : {prov_auditor}")
        w(f"## adjud.  : {c['adj'] or '(unmatched)'}")
        if nums:
            w("## figures : " + ", ".join(nums))
        w("## ------------------------------------------------------------------------")
        w(f'stop("TODO: no check written for {c["id"]} — see the stub comment above")')
    w("```")
    w("")

w("## Summary")
w("")
w("```{r summary, error=TRUE}")
# Unfinished stubs are counted as failures by name. A stub calls stop(), but chunks run with
# error=TRUE so that one bad section cannot hide the rest -- which means a stub would otherwise
# leave no trace in the summary at all. Naming them here closes that hole.
w("## Claims the generator left as TODO stubs. This is a STARTING POINT, not a standing verdict:")
w("## the next line removes any claim that has since acquired a real expect()/judgement()/")
w("## unverifiable() record, so writing a check clears the stub automatically. Without this the")
w("## summary reports a fully written notebook as entirely unverified.")
w("STUB_CLAIMS <- c(" + ", ".join(f'"{i}"' for i in stub_ids) + ")")
w("STUB_CLAIMS <- setdiff(STUB_CLAIMS,")
w("  unique(c(vapply(.V$exp, function(e) e$id, character(1)), names(.V$state))))")

summary_r = r'''
declared <- .V$order
rows <- do.call(rbind, lapply(declared, function(key) {
  e <- .V$exp[[key]]; o <- .V$obs[[key]]
  if (is.null(e)) e <- list(id = strsplit(key, "\\|")[[1]][1],
                            key = strsplit(key, "\\|")[[1]][2], expected = NA, tol = NA)
  data.frame(claim = e$id, key = e$key,
             expected = .fmt(e$expected),
             observed = if (is.null(o)) "-- never computed --" else .fmt(o$observed),
             tol      = .fmt(e$tol),
             verdict  = if (is.null(o)) "FAIL" else o$verdict,
             detail   = if (is.null(o)) "expectation declared but no check() ran" else o$detail,
             stringsAsFactors = FALSE)
}))
states <- do.call(rbind, lapply(.V$state, function(s)
  data.frame(claim = s$id, key = "-", expected = "-", observed = "-", tol = "-",
             verdict = s$verdict, detail = s$detail, stringsAsFactors = FALSE)))
stubs <- if (length(STUB_CLAIMS))
  data.frame(claim = STUB_CLAIMS, key = "-", expected = "-", observed = "-", tol = "-",
             verdict = "FAIL", detail = "TODO stub: no check has been written for this claim",
             stringsAsFactors = FALSE) else NULL
all_rows <- rbind(rows, states, stubs)
all_rows <- all_rows[order(all_rows$claim, all_rows$key), ]

cat("\n"); cat(strrep("=", 100), "\n")
cat("VERIFICATION SUMMARY\n")
cat(strrep("=", 100), "\n")
print(knitr::kable(all_rows, row.names = FALSE))

n_pass <- sum(all_rows$verdict == "PASS")
n_fail <- sum(all_rows$verdict == "FAIL")
n_jud  <- sum(all_rows$verdict == "JUDGEMENT")
n_unv  <- sum(all_rows$verdict == "UNVERIFIABLE")
claims_covered <- length(unique(all_rows$claim))

cat("\n")
cat(sprintf("claims with at least one outcome : %d\n", claims_covered))
cat(sprintf("checks PASS                      : %d\n", n_pass))
cat(sprintf("checks FAIL                      : %d\n", n_fail))
cat(sprintf("claims marked JUDGEMENT          : %d\n", n_jud))
cat(sprintf("claims marked UNVERIFIABLE       : %d\n", n_unv))
cat(sprintf("claims still a TODO stub         : %d%s\n", length(STUB_CLAIMS),
            if (length(STUB_CLAIMS)) paste0("  <-- ", paste(STUB_CLAIMS, collapse = ", ")) else ""))

## the deposit must be untouched
after <- .pkg_manifest()
same <- identical(.PKG_ON_ENTRY[order(.PKG_ON_ENTRY$file), c("file", "size", "md5")],
                  after[order(after$file), c("file", "size", "md5")])
cat(sprintf("deposit unmodified               : %s\n", if (same) "yes" else "NO -- INVESTIGATE"))
if (!same) n_fail <- n_fail + 1

if (n_fail > 0) {
  cat("\n"); cat(strrep("!", 100), "\n")
  cat(sprintf("!!  VERIFICATION FAILED: %d check(s) did not reproduce\n", n_fail))
  bad <- all_rows[all_rows$verdict == "FAIL", ]
  for (i in seq_len(nrow(bad)))
    cat(sprintf("!!    %-12s %-22s expected %-18s observed %-18s %s\n",
                bad$claim[i], bad$key[i], bad$expected[i], bad$observed[i], bad$detail[i]))
  cat(strrep("!", 100), "\n")
} else {
  cat("\nAll declared checks reproduced.\n")
}

utils::write.csv(all_rows, "verification_summary.csv", row.names = FALSE)
writeLines(as.character(as.integer(n_fail > 0)), "verification_status.txt")
unlink(SCRATCH, recursive = TRUE)

if (n_fail > 0)
  stop(sprintf("verification failed: %d check(s) did not reproduce (see the table above)", n_fail))
'''
w(summary_r.strip())
w("```")
w("")
w("The chunk above writes `verification_summary.csv` and `verification_status.txt` (`0` clean,")
w("`1` if anything failed). To get a nonzero **process** exit as well:")
w("")
w("```sh")
w("Rscript -e 'rmarkdown::render(\"verification.Rmd\"); \\")
w("           quit(status = as.integer(readLines(\"verification_status.txt\")))'")
w("```")

pathlib.Path(out_p).write_text("\n".join(x for x in L if x is not None) + "\n")

sys.stderr.write(f"claims extracted : {len(claims)}\n")
sys.stderr.write(f"with a check     : {len(claims) - len(stub_ids)}\n")
sys.stderr.write(f"TODO stubs       : {len(stub_ids)}\n")
if stub_ids:
    sys.stderr.write("stub ids         : " + ", ".join(stub_ids) + "\n")
sys.stderr.write(f"wrote            : {out_p}\n")
PYGEN
rc=$?
[ $rc -ne 0 ] && { echo "generation failed" >&2; exit $rc; }
exit 0
