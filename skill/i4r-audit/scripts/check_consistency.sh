#!/bin/bash
# check_consistency.sh <report.md> [claim-file]
#
# Guards the two failure modes that recurred across both calibration audits:
#   1. a correction applied to the body but not the summary page (three occurrences);
#   2. a participant identifier republished next to a date or clock (two occurrences,
#      one of them in a release-bound draft).
# Run after EVERY round that edits the comment, before rebuilding the deliverable.
set -uo pipefail
R="${1:?usage: check_consistency.sh <report.md> [retired-claims.txt]}"
CLAIMS="${2:-}"
fail=0

echo "== 1. disclosure sweep =="
# Detector note, learned twice the hard way: do NOT write "code, then no digits, then date".
# Real rows put a timepoint or a measurement between them, so that pattern silently returns zero.
# Test co-occurrence on the same LINE, and self-test the pattern before trusting a clean result.
python3 - "$R" <<'PYSWEEP'
import sys, re, pathlib
# \b fails against an underscore: `_` is a word character, so \bMSTE_AC_P12\b never fires
# on the very identifier form this hunts. The shipped positive control used a form WITHOUT a
# leading underscore run, so it passed while the detector was blind — a positive control
# validated against the wrong form certifies a blind detector. Use explicit lookarounds.
CODE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,3}_?P\d+(?![A-Za-z0-9])|(?<![A-Za-z0-9])P\d{1,3}(?![A-Za-z0-9])")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}\.\d{1,2}\.\d{4}")
CLOCK = re.compile(r"\d{2}:\d{2}:\d{2}")
probe = "SA_P1,0,09/05/2022,118,74"
probe2 = "MSTE_AC_P12,2022-01-01"   # the underscore form that defeated the old \b anchors
if not (CODE.search(probe) and DATE.search(probe) and CODE.search(probe2) and DATE.search(probe2)):
    print("  FAIL: the disclosure detector does not fire on a known-present instance"); sys.exit(1)
bad = 0
for i, ln in enumerate(pathlib.Path(sys.argv[1]).read_text(errors="replace").split("\n"), 1):
    if CODE.search(ln) and DATE.search(ln):
        print(f"  FAIL line {i}: participant code and date on one line"); bad = 1
    if CODE.search(ln) and CLOCK.search(ln):
        print(f"  FAIL line {i}: participant code and clock on one line"); bad = 1
if not bad:
    print("  ok: no code/date or code/clock linkage (detector self-tested)")
sys.exit(bad)
PYSWEEP
[ $? -ne 0 ] && fail=1

echo "== 2. summary page vs body =="
python3 - "$R" "$CLAIMS" <<'PY'
import sys, pathlib, re
s = pathlib.Path(sys.argv[1]).read_text()
m0 = re.search(r"^## (?:Executive summary|Abstract)", s, re.M)
if not m0:
    print("  (no executive summary found)"); raise SystemExit(0)
a = m0.start(); b = re.search(r"^## 1\.", s[a:], re.M)
abst = s[a:a+b.start()] if b else s[a:a+6000]
body = s.replace(abst, "")
claims = sys.argv[2]
# Scan the WHOLE document, not only the abstract. Checking the abstract alone was a real bug:
# in the Granger-causality audit a claim the audit had explicitly retracted survived in a body
# section, this check passed clean, and two independent critics found it. A retired claim is
# wrong wherever it sits; the abstract is only where it is most visible.
if claims and pathlib.Path(claims).exists():
    bad = 0
    lines = s.split("\n")
    # Normalise before matching. Exact substring matching let a retired claim through once:
    # the file said "53 of the 71 player identifiers" and the draft said "53 of 71 player
    # identifiers" — the same withdrawn claim, one article apart. A withdrawn claim is withdrawn
    # in all its phrasings, so strip articles, punctuation and case before comparing.
    import re as _re
    def norm(x):
        x = x.lower()
        x = _re.sub(r"[^a-z0-9 ]+", " ", x)
        x = _re.sub(r"\b(the|a|an|of|its|their|this|that)\b", " ", x)
        return _re.sub(r"\s+", " ", x).strip()
    nlines = [norm(ln) for ln in lines]
    nabst = norm(abst)
    for line in pathlib.Path(claims).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        nl = norm(line)
        if not nl: continue
        for i, ln in enumerate(nlines, 1):
            if nl in ln:
                where = "SUMMARY PAGE" if nl in nabst else f"line {i}"
                print(f"  FAIL: retired claim still present ({where}) -> {line[:60]}")
                bad = 1
    if not bad: print("  ok: no retired claim survives anywhere in the document")
else:
    bad = 0
    print("  (no retired-claims file given; skipping targeted check)")
# numeric drift: any figure on the summary page absent from the body is suspicious
nums_a = set(re.findall(r"\b\d[\d,]*\.?\d*\b", abst))
nums_b = set(re.findall(r"\b\d[\d,]*\.?\d*\b", body))
orphan = sorted(n for n in nums_a - nums_b if len(n) > 2)
if orphan: print(f"  WARN: figures on the summary page not found in the body: {orphan}")
else: print("  ok: every multi-digit summary-page figure appears in the body")
raise SystemExit(bad)
PY
# Section 2 used to print FAIL and return 0 because this line was missing, so the
# retired-claims check was ADVISORY rather than blocking in every audit that used it.
# Same class as "&& echo OK swallows the failure". Check $? after every block.
[ $? -ne 0 ] && fail=1

echo "== 3. house rules =="
python3 - "$R" <<'PY2'
import sys, pathlib, re
s = pathlib.Path(sys.argv[1]).read_text(); bad = 0
# 3a. page 1: executive summary <=200 words (prose only), plus the two required tables
m0 = re.search(r"^## (?:Executive summary|Abstract)", s, re.M)
if not m0:
    print("  WARN: no executive summary found")
else:
    a = m0.start()
    m = re.search(r"^## (?:1\.|\d)", s[a+5:], re.M)
    seg = s[a:a+5+m.start()] if m else s[a:a+8000]
    if not m: print("  WARN: could not delimit the summary page (no '## 1.' heading after it)")
    lines = seg.split("\n")[1:]
    prose = [l for l in lines if not l.lstrip().startswith("|")]
    w = len(" ".join(prose).split())
    if w > 200: print(f"  FAIL: executive summary is {w} words (limit 200, tables excluded)"); bad = 1
    else: print(f"  ok: executive summary {w} words (tables excluded)")
    ntab = sum(1 for i, l in enumerate(lines)
               if l.lstrip().startswith("|") and (i == 0 or not lines[i-1].lstrip().startswith("|")))
    if ntab < 2:
        print(f"  FAIL: page 1 must carry two tables (findings summary, files summary) — found {ntab}"); bad = 1
    else:
        print(f"  ok: {ntab} summary tables on page 1")
# 3b. required citations
for needle, label in (("AI-Games", "Brodeur et al. / AI-Games"),
                      ("AI-vertical", "I4R / AI-vertical")):
    if needle not in s: print(f"  FAIL: missing required citation ({label})"); bad = 1
if "AI-Games" in s and "AI-vertical" in s: print("  ok: both required citations present")
# 3c. process narration
BANNED = [r"earlier draft", r"earlier version of this comment", r"\bwe withdraw\b",
          r"\bround[ -][0-9]", r"adversarial (review|critic)", r"\bour critics\b",
          r"\ba critic\b", r"Changes from earlier revisions", r"\bRevision [0-9]\b",
          r"corrected (twice|in round)", r"intermediate draft", r"our (own )?initial reading",
          r"we initially", r"more embarrassingly", r"\bwe first (thought|reported)\b"]
hits = [(i+1, l.strip()[:80]) for i, l in enumerate(s.split("\n"))
        for pat in BANNED if re.search(pat, l, re.I)]
if hits:
    print(f"  FAIL: {len(hits)} process-narration hit(s):"); bad = 1
    for ln, txt in hits[:8]: print(f"     line {ln}: {txt}")
else:
    print("  ok: no process narration")
# 3d. length early-warning — the hard page gate lives in build_docx.sh on the rendered PDF.
# Budget scales with the cap: ~600 words per rendered page at the reference.docx geometry
# (12pt, 10pt paragraph spacing), so a 10-page cap allows ~6000 words of prose plus tables.
import os
max_pages = int(os.environ.get("MAX_PAGES", "10"))
budget = 600 * max_pages
words = len(s.split())
if words > budget:
    print(f"  WARN: ~{words} words — unlikely to fit {max_pages} rendered pages (budget ~{budget}); cut before building")
else:
    print(f"  ok: ~{words} words, within the {max_pages}-page budget (~{budget})")
raise SystemExit(bad)
PY2
[ $? -ne 0 ] && fail=1


echo "== 4. findings table vs body vs claim tags =="
python3 - "$R" <<'PYX'
import sys, re, pathlib
s = pathlib.Path(sys.argv[1]).read_text()
bad = 0
# self-test the two extractors on planted forms before trusting either
assert re.search(r"^\|\s*(\d+)\s*\|", "| 3 | a finding | none |", re.M), "table-row extractor inert"
assert re.search(r"\[claim (\d+)\]", "text [claim 7] more"), "tag extractor inert"
# rows come from the PAGE-1 findings table only — a body table with a numeric first column
# (a results table) must not read as phantom findings
m0 = re.search(r"^## (?:Executive summary|Abstract)", s, re.M)
m1 = re.search(r"^## (?:1\.|\d)", s[m0.end():], re.M) if m0 else None
page1 = s[m0.start():m0.end()+m1.start()] if (m0 and m1) else (s[m0.start():m0.start()+8000] if m0 else "")
rows = set(int(m) for m in re.findall(r"^\|\s*(\d+)\s*\|", page1, re.M))
tags = set(int(m) for m in re.findall(r"\[claim (\d+)\]", s))
if not rows and not tags:
    print("  (no findings table and no claim tags — pre-handoff draft; nothing to cross-check)")
elif not rows:
    print(f"  FAIL: {len(tags)} claim tags but no numbered findings table on page 1"); bad = 1
else:
    # a body finding with no table row, or a row never tagged in the body, is a defect
    # (critics are told the same; this makes it blocking rather than advisory)
    missing_tags = sorted(rows - tags); orphan_tags = sorted(tags - rows)
    if missing_tags: print(f"  FAIL: findings-table rows never tagged in the body: {missing_tags}"); bad = 1
    if orphan_tags:  print(f"  FAIL: [claim N] tags with no findings-table row: {orphan_tags}"); bad = 1
    if not bad: print(f"  ok: {len(rows)} findings-table rows and body tags agree (extractors self-tested)")
sys.exit(bad)
PYX
[ $? -ne 0 ] && fail=1

exit $fail
