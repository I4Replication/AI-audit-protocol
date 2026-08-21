# The I4R audit checklist

Distilled from published I4R comments (papers A40, A46, A45 and A47) and from the
audit A48 calibration run. Items 1–7 are what the published comments actually hunt for; 8–11
were added from calibration.

## 1. Duplication and sample inflation
Exact duplicate rows; near-duplicates; contiguous block duplication at every offset; repeat
submissions sharing timestamp/IP/contact; whole-sample duplication (a block of
rows copied to inflate n). **Quantify the coincidence** — how many response
patterns are possible? — and **name the mechanism**. Distinguish scope: "no near-duplicates" in
survey exports is not "no near-duplicates" in the response data.

## 2. Impossible and out-of-range values
Responses outside the scale; subscale scores above the arithmetic maximum; impossible demographics
(ages of 1, 2, 9; height 40 inches). Trace whether bad values entered published statistics.

## 3. Reproduction and source discrepancies
N in data vs N in paper; means reproduce but SDs do not; text vs figure vs data disagreement; whole
studies missing from an archive claiming completeness. **Reproduce at the precision printed, and
enumerate every value that does not.** One blanket "all quantities match" claim will be false.

## 4. Scoring and coding errors
Subscale formulas mixing in opposing-construct items; inconsistent composition across studies; wrong
multipliers; silent recoding; scale-offset bugs that manufacture a group difference.
**Before judging an intended key, verify the column-to-item mapping** — test it against the
covariance structure. Negative alpha means your mapping is wrong, not that the authors' key is.

## 5. Statistical-reporting anomalies (no data required)
p-value distributions; algebraically impossible statistics (CR < AVE violates Cauchy–Schwarz); sign
errors; missing decimals; mislabelled captions; wrong direction in an abstract; confidence levels
labelled inconsistently; **integer requirements rounded down** (a required n of 140.11 is 141).

## 6. Signatures of fabricated or random data
Absent latent structure (all correlations ~0 where theory demands more) — test with a permutation
test on the maximum eigenvalue; uniform distributions with sharp cut-offs where normal/skewed
expected; implausible values; impossibly narrow ranges; absurd effect sizes. **Benchmark against
published reference datasets** with goodness-of-fit tests.

## 7. Privacy and archive hygiene
PII in "anonymised" files; **but check whether an anonymisation claim was actually made** — the
usual contradiction may not exist. Compute uniqueness **per person, not per identifier value**, when
codes collide. Full DOB vs birth year is the difference between ~99% and ~5% singled out. Check
filenames and embedded document metadata, not just cells.

## 8. Estimator specification and library defaults (calibration addition)

**Both calibration papers failed here, in different libraries.** In one, `AROC.sp` silently used
`est.cdf.h="normal"` and a documented alternative reversed a ranking. In the other, scikit-learn
changed `KMeans(n_init=)` from 10 to 1 in v1.4 — and the single version the package pinned was the one
that broke its own published cluster table. Check the release notes of every pinned dependency for
default changes since the paper's own environment.
Which function is being called, and **which of its arguments are left at defaults**? Print the
returned object and check you are reading the element you think you are. Vary the defaults: if a
conclusion flips under a documented alternative, the correction is specification-dependent.

## 9. Figure fidelity
Pixel-measure plotted values against the text. In ggplot, `scale_*_continuous(limits=)` **removes**
observations; `coord_cartesian()` zooms. A clipped panel silently drops data from its own caption's N.

## 10. Mechanical reproducibility
Does the shipped code run as shipped? Undeclared dependencies; placeholder paths; runtime fetches by
hard-coded ID; locale-dependent parsing; absent environment capture; a path that silently loads the
wrong study's data.

## 11. Treatment fidelity, not just outcome provenance

When a model, sensor or piece of apparatus is cleared as "not the source of the dependent variable",
ask separately whether it is part of the **manipulation**. In calibration an ML component was
correctly shown not to generate either outcome — and was, on the same evidence, driving the
experimental feedback participants actually received, with a training set a third the stated size and
a window half the stated length. A refuted hypothesis can be correct and still leave the important
question unasked.

## 12. Reporting omissions
Computed but unreported analyses. Report them as omissions, not as errors, and **attribute no
motive**.

## Cross-cutting requirements
- Label every claim CONFIRMED / LIKELY / SPECULATIVE.
- List benign explanations considered **and how each was ruled out**.
- State impact on published conclusions for every finding. Most have none.
- Report refuted hypotheses as prominently as confirmed ones.
- Credit what the authors did well, specifically.


## Cross-cutting: verify the remedy, not just the defect

Every finding that ends "the authors should do X" asserts that X is possible and sufficient. In
calibration a comment told authors that fixing a live privacy exposure required a new archival
deposit, when the repository supported a direct modification request — a harder route prescribed for
no reason, in the one section where ease of remediation is the argument for acting.

Check the remedy against the platform's live documentation or metadata, exactly as you would check a
statistic against the data. Recommendations are claims.
