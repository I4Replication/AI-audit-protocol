# Verification notebook — guide for the verifier

You are being asked to re-check an I4R comment's numbers against the deposited replication package.
You did not run the audit. You are not expected to trust it. The notebook exists so that you do not
have to.

You know R and you know statistics, so this guide says nothing about either. It covers how the
notebook is put together, what its four verdicts mean, and what to do when one of them is FAIL.

---

## 1. Run it

The notebook is self-contained apart from the deposit and a handful of CRAN packages, all named in
its header with an `install.packages` block you can run once.

```sh
bash scripts/run_verification.sh <audit-dir>
```

The wrapper renders the HTML, exits non-zero if anything failed, and writes a SHA-256 manifest that
binds the result to the exact notebook, report, `comment.json`, summary, and package manifest.
`finalize_run.sh` rejects a missing or stale binding. Every claim chunk catches runtime exceptions
and records them as FAIL, so an error before `expect()` cannot disappear from the summary.

Four audit records come out (the rendered HTML is a human-readable convenience):

| File | What it is |
|---|---|
| `verification.html` | the readable record: every claim, its expectation, its observation, its verdict |
| `verification_summary.csv` | the same table, machine-readable |
| `verification_status.txt` | `0` clean, `1` if any check failed |
| `verification_manifest.json` | hashes and claim IDs binding the executed checks to this audit |

Everything is read from `pkg/` and nothing is written into it. The setup chunk MD5s every file in the
deposit on entry and the summary chunk re-checks them; if the line `deposit unmodified: NO` ever
appears, stop and find out what wrote there before believing anything else in the run.

**Whatever the audited package was written in, the notebook is R.** Stata, SPSS, jamovi and Excel
deposits are all read from R through a single `read_deposit()` dispatcher in the setup chunk
(`haven::read_dta`, `haven::read_sav`, `jmvReadWrite::read_omv`, `readxl::read_excel`, and so on). A
jamovi `.omv` is a zip; if `jmvReadWrite` is not installed the loader falls back to unzipping it, so
a missing package is never a reason a claim goes unchecked. The per-claim chunks are the same shape
in every audit because the format-specific part is confined to that one function.

---

## 2. How to read a section

Every claim in the comment gets its own numbered section, matching the comment's own numbering, and
every section has the same five parts: the claim quoted, where it sits in the comment, which auditor
found it and which adjudication entry settled it, the expected value, and the code.

The ordering inside the code is the part that matters:

```
---- C-5 : expected -------------------------------------------------
[C-5      n_analytic            ] EXPECTED  677
[C-5      n_girls               ] EXPECTED  328
                        ... the computation runs here ...
[C-5      n_analytic            ] OBSERVED  677
[C-5      n_analytic            ] VERDICT   PASS   (|diff| 0, tol 0)
```

`expect()` runs and prints **before** the chunk computes anything. That is deliberate and it is the
whole design: you are comparing a prediction against a result, never reading back whatever the code
happened to produce. If you find a section where the expected value is derived from the observed one,
that is a defect in the notebook — report it.

A snippet declares and checks named values; every value needs a name, and `tol`/`note` are optional:

```r
expect("C-5", n_analytic = 677, n_girls = 328, tol = 0, note = "exact counts")
d <- read_deposit("analysis.csv")
check("C-5", n_analytic = nrow(d), n_girls = sum(d$girl == 1))
```

An expectation that is declared and then never checked is a **FAIL**, not a silence. So is an
observation reported under a key that was never declared. Neither can be produced by forgetting.

---

## 3. The four verdicts

**PASS** — reproduced within the stated tolerance. Tolerances are never bare: each carries a `note`
saying why it is what it is. A tolerance of zero means exact. A wide tolerance on a stochastic
quantity (a bootstrap share, a cross-validated error) will say so and will give the Monte-Carlo
reasoning. **Read the tolerance before you accept a PASS.** A generous tolerance on a claim the
comment states to four decimal places is a finding in itself.

**FAIL** — did not reproduce. See section 4.

**JUDGEMENT** — the claim rests on an assessment, not on an arithmetic result, and the assessment is
stated in full so you can disagree with it on the merits. Which deposited column *is* the variable a
paper names; whether twenty-nine is the right denominator for "analytic decisions"; whether a margin
counts as "barely outside" a permutation null. These are not dressed up as computations, and they
are not hidden either. If you would rule differently, say so — that is exactly the response the
marker is asking for. Where a judgement has a checkable substrate, that substrate is checked in the
same section and the judgement is confined to the part that genuinely is one.

**UNVERIFIABLE** — cannot be settled from the deposit, with the reason given. Three kinds recur, and
the reason string distinguishes them:

- *not in the package* — the claim is about the article's prose, the repository record, or an
  external reference table. Nothing in `pkg/` can speak to it; you check it by reading the paper or
  opening the repository.
- *underspecified by the comment* — the number is reproducible in principle but the comment does not
  state the configuration that produced it (a seed, a sample definition, a control parameter). These
  are the ones to take seriously: they are a defect in the comment, and the notebook prints what the
  documented pipeline does produce so you can see the size of the gap.
- *deliberately not attempted* — value-level confirmation only. Privacy claims are still verified:
  their checks count rows or non-missing cells, or test that a column is present, and print only
  the counts and PASS or FAIL, never a value. What is not attempted is confirming the values
  themselves, which would mean republishing the identifiers the comment asks the authors to remove.

---

## 4. A check failed. Now what

A FAIL means the number in the comment did not come back out of the deposit under the procedure the
notebook documents. It does not by itself say who is wrong. Work through it in this order.

**1. Read the tolerance and the note.** A FAIL by 0.006 on a figure the comment prints to two
decimals is a rounding convention, not a defect. Fix the tolerance, in the notebook, with a note.

**2. Ask whether the quantity is stochastic.** Bootstraps, cross-validated errors, permutation nulls
and anything downstream of an unseeded fold assignment will not reproduce to the digit, ever. If the
section did not already band it, that is the bug: the claim needs a Monte-Carlo tolerance or the
comment needs to publish its seed.

**3. Ask whether the comment pins the configuration.** Re-read the claim. If it says "refitting under
the settings the Methods state" and there are three settings consistent with that text giving three
answers, the comment is underspecified and the honest resolution is an UNVERIFIABLE with the reason,
not a widened tolerance. This is the most common cause of a genuine FAIL and it is a defect in the
comment, not in the deposit.

**4. Only then suspect the check itself.** Positional column indices, NA conventions (do two absent
values "agree"?), which stratum a mean is taken over, whether a ratio is high-to-low or
high-to-total. Most first-draft FAILs are here. Fix the check, and add the convention to the note so
the next reader does not have to rediscover it.

**5. If none of those explain it, the comment is wrong.** Say so, loudly, to the coordinator, with
the section number, the expected value, the observed value and the code that produced it. A number
in a published comment that does not come out of the deposit is exactly what this artefact exists to
catch, and catching one late is much better than not catching it.

Do not "fix" a FAIL by loosening the tolerance until it passes, and do not delete a section. Both
are undetectable in the summary table and both defeat the point.

---

## 5. Regenerating the notebook

```sh
bash scripts/make_verification_notebook.sh <audit-dir> \
     [-o out.Rmd] [-s snippets-dir] [-t "title"]
```

The generator reads `report/REPORT_final.md` and `report/ADJUDICATION.md`, extracts one claim per
generated `[claim N]` tag (a v2 audit keeps exactly the finding ids in `work/verification_plan.json`),
matches each to the
adjudication entry that settled it and to the auditor who found it, and writes the frame.

It cannot write the check body for every claim. Where it has no snippet it emits a **TODO stub** that
carries the claim text, the section, the auditor, the adjudication entry and every numeral found in
the claim, and that calls `stop()` — so an unfinished claim fails the run rather than passing
silently. The stub count is printed on stderr and is the number to drive to zero.

An empty report or a report from which no claims can be extracted is a generator failure, not a
zero-claim success. After replacing every stub, use `run_verification.sh`; do not create a status
file by hand.

Snippets live in `<snippets-dir>` (default `<audit-dir>/verification_snippets`), keyed by claim id:

Claim ids are flat, `C-<n>`, matching the comment's `[claim n]` tags and finding `F<n>`;
`run_verification.sh` binds only that form, so a dotted id such as `C-2.5` is refused.

| File | Effect |
|---|---|
| `_prelude.R` | appended to the setup chunk — shared frame construction, helpers, transcribed figures |
| `C-5.R` | inlined into that claim's chunk |
| `C-8.judgement` | first line the assessment, the rest its basis |
| `C-12.unverifiable` | first line the reason, the rest context |

For numbered findings, `generate_finding_artifacts.py --apply` writes `C-<n>.R` (or `.judgement` /
`.unverifiable`) from each approved row's `verification_snippet`, overwriting the file on every run.
Edit the file the row's `verification_snippet.path` points to, never `verification_snippets/C-<n>.R`.
That source file must itself call both `expect()` and `check()`, each with the claim's own id: the
notebook records an outcome only through them, the projection refuses a code snippet missing either
call, and a helper in `_prelude.R` that wraps them does not count (put the calls in the snippet and
let the helper compute). A chunk that records an outcome under another claim's id fails.

The notebook needs `work/verification_plan.json` (written by the projection) and binds exactly one
section per finding; the older abstract cross-check sections (`C-ABS.<n>`) are no longer generated,
so keep the abstract consistent with the body through `check_consistency.sh`.
