# Finding-Classification Scheme for Reproducibility Audits

**Status:** design specification, v1.0
**Date:** 2026-08-05

Three orthogonal fields classify every audit finding: **IMPACT** (what the audit found),
**VERIFICATION TIER** (what it costs to check), and **DISCLOSURE** (privacy, consent,
identifiability, licence). They are recorded separately and must never be collapsed into a
single score.

---

## Part 1 — The researched taxonomy

### 1.1 What the retraction literature actually does

Four things in the existing taxonomies drove this design.

**(a) The mature taxonomies separate the *defect* from its *cause* — and they do it on the same
object.** The Retraction Watch Database reason list is the most granular scheme in existence
(~111 codes, flat and alphabetical, not grouped). Read against the grain, it contains a repeated
four-step gradient applied independently to data, images, and results/conclusions:

| Object | Suspicion raised | Defect, no cause asserted | Defect + consequence | Cause asserted |
|---|---|---|---|---|
| Data | Concerns/Issues About Data | Error in Data | Unreliable Data | Falsification/Fabrication of Data; Manipulation of Data |
| Image | Concerns/Issues About Image | Error in Image | Unreliable Image | Falsification/Fabrication of Image; Manipulation of Images |
| Results | Concerns/Issues about Results and/or Conclusions | Error in Results and/or Conclusions | Unreliable Results and/or Conclusions | Falsification/Fabrication of Results; Manipulation of Results |

The same *observable* (a wrong number) sits in a different column depending on what is known
about *why*. That column is exactly what a reproducibility audit cannot fill in. This programme
therefore occupies the middle two columns only, and the scheme below is built so that occupying
them is structurally enforced rather than a matter of reviewer tact.

**(b) The honest-error / misconduct line is drawn at *intent*, and the literature says notices
get it wrong.** COPE states plainly that "Unreliable content or data may result from honest
error, naïve mistakes, or research misconduct" — i.e. unreliability is the finding; cause is a
separate determination. Fang, Steen & Casadevall (2012) then showed that determination is
routinely mis-made *in the notices themselves*: consulting Office of Research Integrity reports
and other secondary sources "resulted in the reclassification of 118 of 742 (15.9%) retractions
in an earlier study from error to fraud." Retraction Watch encodes the same phenomenon as a
first-class category, **Euphemisms for Misconduct**. Steen (2011) illustrates the opposite
failure: his "error" bucket absorbs plagiarism and ethics violations alongside genuine mistake,
which is why re-cutting the same data produced the opposite headline (73.5% "error" vs Fang's
21.3%). **Conclusion for this programme: any scheme whose top category implies intent will be
systematically miscoded, in both directions, by people who cannot observe intent.** Hence Rule 3.

**(c) The correction/retraction threshold is a *conclusion* threshold, not a magnitude
threshold.** This is consistent across every normative source:

- **ICMJE:** "Pervasive errors can result from a coding problem or a miscalculation and may
  result in extensive inaccuracies throughout an article. If such errors do not change the
  direction or implications of the results, interpretations, and conclusions of the article, a
  correction should be published." And: "Errors serious enough to invalidate a paper's results
  and conclusions may require retraction."
- **COPE:** retract on "clear evidence that the findings are unreliable"; retraction is *not*
  usually appropriate where "The main findings of the work are still reliable and correction
  could sufficiently address errors or concerns."
- **ACM** artifact badging, on tolerance: "differences in the results should not change the main
  claims made in the paper."
- **statcheck** (Nuijten et al. 2016) operationalises the same cut for a single statistic:
  *inconsistency* = reported value differs from recomputed; *gross inconsistency* = the
  difference "changes the statistical conclusion."

So the size of the error is nearly irrelevant; what matters is whether the claim survives. The
IMPACT field is therefore ordered by *what happens to the claim*, not by how wrong the number is.

**(d) Absence of evidence is explicitly not evidence of absence — and the standards say so.**
NISO CREC warns: "Both people and machines may interpret the absence of certain tags as meaning
that no such concerns are present, even though this is not necessarily true." COPE's route for
this state is the expression of concern: "If conclusive evidence about the reliability of a
publication cannot be obtained... an editor could consider publishing an expression of concern."
Grieneisen & Zhang found ~14% of notices state no reason at all. This is the direct warrant for
`unverifiable` being a distinct IMPACT value rather than a variety of `none`, and for Rule 2.

### 1.2 Severity ladders that already exist

The three-level correction-severity scheme recurs independently across fields, always cut at the
conclusion:

| Source | Levels | Cut point (verbatim where available) |
|---|---|---|
| Farrah & Rabb (2019), *JMLA* | Trivial / Minor / Major | Major = "impacts the analysis or interpretation of a primary outcome and, subsequently, the conclusions"; Minor = impacts an outcome "but does not impact the conclusions"; Trivial = does not relate to outcomes or conclusions |
| Zahn-Muñoz et al. (2026) | Trivial / Minor / Major | Same cut; observed 83.5% / 14.0% / 1.6% |
| Margalida & Colomer (2016), *PeerJ* | Mild / Moderate / Severe | 34.7% / 47.7% / 17.6% in Nature, Science, PNAS |
| Brodeur et al. (I4R) | Identical / Minor / Major differences | Minor = "do not lead to important changes in significance or magnitude"; Major = "lead to major differences in one or multiple claims" |
| AEA Data Editor | Full / Full w. minor issues / Partial / Not able | Partial = ">25%" of programs or numbers differ |
| cascad | RRR / RR / R / D / DD | Perfectly reproducible → Major issues |
| BITSS ACRE | L1–L10 | Keyed on reproducible-from-analytic-data vs from-raw-data |

**What none of them have**, and what this scheme adds: a category for a result that is *entailed
by its own construction* (`artefact_of_construction`), and a category for *absent validation*
(`conclusion_unsupported`). Every ladder above presumes the analysis is a good-faith attempt
whose output can be compared to a recomputation. When a result is guaranteed by how the pipeline
was built — target leakage, a circular definition, a test set that overlaps training — there is
no recomputation that rescues it, and "major difference" understates it. Flagged as a genuine
extension beyond the surveyed sources, not something adopted from them.

### 1.3 Editorial vocabulary used in Part 2

Grounded in COPE (2019), ICMJE (2026), NISO RP-45-2024, and Crossref Crossmark's 12 update types
(`addendum`, `clarification`, `correction`, `corrigendum`, `erratum`, `expression_of_concern`,
`new_edition`, `new_version`, `partial_retraction`, `removal`, `retraction`, `withdrawal`).

- **Erratum** — journal/production-origin error. **Corrigendum** — author-origin error. Bentan et
  al. (2024) is the one study found that operationalises this split empirically (62.5% / 37.5%).
- **Expression of concern** — NISO: "A notice issued typically by an editor to draw attention to
  possible problems within a publication. It may but does not necessarily precede further
  editorial action."
- **Retraction** — NISO, quoting COPE: for content "so seriously flawed or erroneous... that
  their findings and conclusions cannot be relied upon."
- **Retraction and republication** — ICMJE's route for honest error causing a major change where
  "the underlying science appears valid."
- **Removal** — NISO: full text withdrawn, metadata preserved; reserved for court order, personal
  privacy, third-party legal rights, public health risk, or content "published without the
  required licenses having been obtained." *Removal is the only editorial action that a
  DISCLOSURE defect alone can trigger* — which is the structural reason Field 3 is orthogonal.
- **Partial retraction** — deprecated. COPE: "Partial retractions are not helpful." NISO records
  it as "deprecated by COPE and... no longer considered best practice." Not used by this scheme.

---

## Part 2 — The three fields

### FIELD 1 — `impact`

*What the audit found about the published claim.* Exactly one value per finding. Ordered; `rank`
is used by Rule 1.

| Value | Rank | Definition | Boundary test |
|---|---|---|---|
| `none` | 0 | The check was performed, completed, and disclosed no defect. | Requires that the check **was possible**. If it could not be completed, the value is `unverifiable`, never `none`. |
| `unverifiable` | 1 | The deposited package is insufficient to perform the check. An epistemic state, **not a clean bill of health**. | Was the obstacle the *package*, or the *claim*? Missing code → `unverifiable`. Code present and showing the claim has no supporting test → `conclusion_unsupported`. |
| `value_wrong` | 2 | A specific reported value is wrong; the direction and implications of the claim are unaffected. | Would a reader who saw the corrected value draw the same conclusion? If yes → here. (ICMJE's cut; statcheck's plain "inconsistency".) |
| `conclusion_narrowed` | 3 | The claim holds, but in a weaker form than stated — smaller effect, narrower population, fewer conditions, weaker modality. | Something survives. If nothing survives, go higher. |
| `conclusion_unsupported` | 4 | The evidence offered does not establish the claim. Typically the required validation is **absent**, not failed. | Absence of evidence. The claim may well be true; the paper does not show it. |
| `conclusion_contradicted` | 5 | The deposit's own contents contradict the published claim. | Evidence *against*. The refutation comes from the authors' own materials, not from an external standard. |
| `artefact_of_construction` | 6 | The result is guaranteed by how the analysis was built. Reanalysis within the existing design cannot rescue it. | Ask: is there **any** input dataset for which this pipeline would have produced a negative result? If no → here. This is what distinguishes it from `conclusion_contradicted`. |

**Optional facet — `locus`** (not part of the three fields; record where the defect lives, for
aggregate reporting). Modelled on the RW object split and the I4R coding-error taxonomy:
`data` · `cleaning` · `analysis_code` · `estimation` · `inference` · `sample` · `reporting` ·
`figure` · `text` · `environment` · `other`.

### FIELD 2 — `verification_tier`

*What it costs a human verifier to confirm or reject the finding.* Per the coordinator's
direction, tiers are defined by **effort and complexity**, not by specialist skill: all verifiers
are competent in R and statistics, so there is no expertise to route to. Tiers exist for
**scheduling and workload allocation**.

> **Naming note for the coordinator.** Because tiers now measure cost rather than gravity, the
> field is named `verification_tier`, not `severity_tier`. Gravity is read off `impact`. Keeping
> the old name would invite exactly the conflation the programme is trying to avoid. Flagged for
> a decision; the enumeration works under either name.

| Tier | What the verifier must do | Inputs needed | Nature of check | Expected effort |
|---|---|---|---|---|
| **T1** | Recompute one or two quantities by hand or in a few lines. | Printed paper only, or one deposited value. | Deterministic arithmetic. Holds or doesn't. | **< 30 min** |
| **T2** | Extract one target quantity from the deposit; run a single script or a bounded slice of the pipeline. | Deposit, partially. | Deterministic comparison to a printed value. | **0.5 – 2 h** |
| **T3** | Build the environment and reproduce the full pipeline, or a whole table/figure end to end. | Full deposit + working environment. | Mostly deterministic; requires a tolerance judgement on near-matches. | **4 h – 2 days** |
| **T4** | Reproduce, then re-run under a specified alternative specification and compare. | Full deposit + environment + a defensible alternative. | Judgement: is the alternative the plausible one? Contestable. | **2 – 5 days** |
| **T5** | Establish a structural property of the analysis — that the result is entailed by construction, or that a validation was never performed anywhere in the pipeline. May require a counterexample or simulation. | Full deposit + code architecture read. | Judgement-heavy. Argument, not comparison. | **1 – 2 weeks, may not converge** |

**Effort is verifier-hours for confirming or rejecting the finding as stated.** It is not the time
the original auditor spent, and not the time to fix the paper.

**Tier is independent of impact.** A T1 check can yield `artefact_of_construction`; a T4 grind can
yield `none`. Do not let one field's value pull the other.

### FIELD 2b — `paper_only` (orthogonal flag)

**`paper_only: true`** — the finding is established from the published article alone. No
replication package, no data, no code, no environment.

Made a **flag rather than a tier** deliberately. Paper-only findings are the cheapest to verify
*and* frequently the gravest — an accuracy below the no-information rate is both a five-minute
check and fatal. Encoding "cheap" as a low tier is right; encoding "cheap" as "minor" would be
badly wrong, and a tier would smuggle in that second reading. The flag also captures the property
that actually matters strategically: **a paper-only finding cannot be disputed on grounds of how
the auditors handled the data**, because the auditors did not handle the data. There is nothing
to litigate but the paper's own arithmetic.

Canonical paper-only findings: a reported accuracy below the no-information rate implied by the
paper's own confusion matrix; a standard deviation arithmetically impossible for a bounded or
percentile-valued quantity; degrees of freedom exceeding the ceiling set by the reported N;
percentages that do not sum; an N that differs between two tables; a reported test statistic
inconsistent with its own p-value and df; a confidence interval not centred on its point estimate.

**Scheduling rule: `paper_only` findings are verified first, ahead of all others, regardless of
tier or impact.** They clear fastest and they are the least contestable.

Most `paper_only` findings are T1, but the flag and the tier are not redundant in either
direction: recomputing a whole meta-analysis from a printed forest plot is `paper_only` at T2–T3,
and checking a single deposited constant is T1 but not `paper_only`.

### FIELD 3 — `disclosure`

*Privacy, consent, identifiability, and licence defects in the deposited materials or the
article.* Orthogonal to both other fields — **may co-occur with any `impact` value, including
`none`**. A package can be perfectly reproducible and still expose participants.

Grounded in COPE's retraction criteria (material "without authorisation for use"; "Copyright has
been infringed or there is some other serious legal issue (eg, libel, privacy)"; "It reports
unethical research"), the corresponding RW codes (`Informed/Patient Consent – None/Withdrawn`,
`Lack of IRB/IACUC Approval and/or Compliance`, `Copyright Claims`, `Concerns/Issues about Human
Subject Welfare`, `Transfer of Copyright and/or Ownership`), and NISO's removal definition.

`disclosure.flags` — zero or more:

| Flag | Definition |
|---|---|
| `identifiability_direct` | Deposit contains direct identifiers: names, addresses, IDs, contact details, faces, unredacted free text. |
| `identifiability_indirect` | Re-identification feasible by combining quasi-identifiers, small cells, geography, or dates; includes unsuppressed low-count cells. |
| `consent_scope_exceeded` | Deposit or analysis goes beyond what the recorded consent covers, including open deposit where consent specified restricted access, or use after withdrawal. |
| `ethics_approval_absent` | No IRB/IACUC/REB record where the research type requires one, or approval does not cover the deposited use. |
| `licence_absent` | No licence stated for data or code, leaving reuse rights undefined. |
| `licence_incompatible` | Stated licence conflicts with the terms of an upstream source, a DUA, or the repository. |
| `third_party_rights` | Redistribution of data, instruments, or code the depositors do not have the right to redistribute. |
| `special_category_data` | Health, genetic, biometric, sexual-orientation, immigration, criminal-justice data, data on minors, or data on identifiable protected groups, exposed without adequate control. |

`disclosure.urgency` — exactly one:

| Value | Meaning | Handling |
|---|---|---|
| `none` | No disclosure defect found. | — |
| `routine` | Defect is formal; no live exposure. Typical of `licence_absent`. | Normal queue. |
| `prompt` | Real but not immediate exposure risk; re-identification requires effort or auxiliary data. | Expedite. |
| `immediate` | Identifiable individuals are exposed in a live public deposit. | **Report it in the comment** as a disclosure-flagged finding (identifier kind, file and column, rows or participants affected, verified remedy) and in `disclosure.statement`. Filing the comment with I4R is the escalation; drafting, critique and filing are never paused. Consistent with removal. Never publish an identifying value, or a procedure or worked example that demonstrates re-identification. |

---

## Part 3 — Tier and impact mapped to editorial actions

Both tables state what the finding is **consistent with** under published editorial standards.
**The programme does not recommend, request, or assert that any action be taken.** These columns
exist so a reader can locate a finding against the standards; they are descriptive, not
operative.

### 3.1 Primary: `impact` → editorial correspondence

Impact is what actually drives editorial correspondence, because every source cuts at the
conclusion.

| `impact` | Consistent with | Standard grounding |
|---|---|---|
| `none` | No action. | — |
| `unverifiable` | An editorial data-availability query; where it cannot be resolved, an expression of concern. | COPE: inconclusive evidence → consider EoC. NISO: absent tags ≠ absent concerns. |
| `value_wrong` | Correction — erratum if production-origin, corrigendum if author-origin. | ICMJE: errors not changing "the direction or implications" → correction. |
| `conclusion_narrowed` | Correction with substantive amendment; where the change to the conclusion is major and the error honest, retraction with republication. | ICMJE retract-and-republish route. |
| `conclusion_unsupported` | Expression of concern; retraction where the missing support cannot be supplied. | COPE: EoC while reliability is unresolved. |
| `conclusion_contradicted` | Retraction; retraction with republication where a valid analysis survives in the same materials. | COPE: "clear evidence that the findings are unreliable." |
| `artefact_of_construction` | Retraction. **Retraction with republication is not applicable** — ICMJE's route presupposes "the underlying science appears valid," and here the design itself produces the result. | COPE unreliability + ICMJE's precondition failing. |

### 3.2 Secondary: `verification_tier` → typical co-occurring action

Weaker and purely observational. Tier measures cost; cost correlates loosely with gravity and
should never be read as implying it. **Where the two tables disagree, 3.1 governs.**

| Tier | Typically co-occurring `impact` | Consistent with (modal case) | Caveat |
|---|---|---|---|
| T1 | `value_wrong`, but also the sharpest `artefact_of_construction` findings | Correction — *or* retraction, when the arithmetic is fatal | Weakest correlation of any row. Do not infer gravity from T1. |
| T2 | `value_wrong`, `conclusion_narrowed` | Correction | — |
| T3 | `unverifiable`, `conclusion_narrowed`, `conclusion_contradicted` | Correction or expression of concern | Full-reproduction failures split widely by cause. |
| T4 | `conclusion_narrowed`, `conclusion_unsupported` | Expression of concern | Contestable by construction; the alternative specification is a judgement. |
| T5 | `conclusion_unsupported`, `artefact_of_construction` | Expression of concern or retraction | High cost reflects difficulty of *proof*, not certainty of the finding. |

---

## Part 4 — Classification rules

### Rule 1 — Multiple findings: the comment takes the maximum, and the maximum alone is not the record

**Yes, a comment takes the maximum `impact` across its findings** — by `rank` in the Field 1
table. Rationale: editorial correspondence is determined by the worst surviving defect, and every
source cuts on whether the conclusion stands. A comment containing one `value_wrong` and one
`conclusion_contradicted` is a `conclusion_contradicted` comment.

Constraints on the maximum:

1. **Findings are recorded individually and are never deleted by the roll-up.** The comment
   carries `impact_max` *and* the full `findings[]` array. Grieneisen & Zhang's warning applies:
   their justification counts "can not simply be added together" because notices carry multiple
   reasons. Preserve the multiplicity.
2. **The maximum does not inherit its tier.** `verification_tier` is per-finding. A comment
   reports the tier *of the finding that set the maximum*, plus the full distribution. A
   `conclusion_contradicted` established at T1 is not a two-week job because some T4 finding sits
   below it in the same comment.
3. **Findings on the same claim vs different claims.** If several findings bear on one claim, take
   the maximum. If they bear on *different* claims, record `claims_affected` as well — three
   separate `conclusion_narrowed` findings against three independent claims is materially
   different from one, and the maximum alone conceals that.
4. **No aggregation upward by accumulation.** Multiple `value_wrong` findings never sum to
   `conclusion_narrowed`. Escalation requires a finding that on its own reaches the higher rank.
   If the auditors judge that the errors *jointly* undermine the claim, that is a new,
   separately-stated finding, with its own evidence and its own tier.
5. **`paper_only` at comment level is `true` only if *every* finding is `paper_only`.** The
   scheduling advantage and the non-contestability both evaporate as soon as one finding requires
   the deposit. Per-finding flags remain individually readable.

### Rule 2 — `unverifiable` and its interaction with tier

**2a. `unverifiable` is never reported as `none`, and never silently absorbed.** This is the
single most important rule in the scheme. "We could not check" and "we checked and it was fine"
are opposite states. NISO CREC's warning is the standing hazard.

**2b. The tier of an `unverifiable` finding is the cost of establishing that it is unverifiable —
not the cost of the check that could not be run.** Discovering that the estimation script is
absent is usually T1–T2, even though running it would have been T3. Tiering it at T3 would clog
the schedule with work that cannot be done. The blocked check's would-be tier is recorded
separately as `blocked_tier` for planning, so the coordinator can see what capacity would be
released if the package were completed.

**2c. `unverifiable` findings must record what was missing**, using the AEA Data Editor failure
vocabulary: `data_missing` · `data_not_available` · `code_missing` · `code_not_functional` ·
`discrepancy_in_output` · `software_not_available` · `insufficient_compute` · `missing_readme` ·
`insufficient_documentation`. "Unverifiable" with no cause recorded is not a finding.

**2d. Residual carry.** Where a comment contains both `unverifiable` and higher-ranked substantive
findings, `impact_max` takes the substantive maximum, **and `residual_unverifiable` is set true**.
Without this, a comment reading `conclusion_contradicted` would falsely imply the rest of the
package was checked and sound. The flag says: this is the worst thing we found *and* there is
territory we could not enter.

**2e. `unverifiable` does not decay into `none` with time.** If a package is completed later and
the check then passes, that is a new finding at `none` with its own date. The original stands as
part of the record of what the deposit supported *as published*.

### Rule 3 — The programme never asserts misconduct or intent (hard rule)

**The programme reports only what the artifacts show. It makes no statement, and permits no
implication, about intent, motive, knowledge, competence, or good faith.**

This is not politeness. It is a correctness constraint: intent is not observable from a
replication package, and the literature demonstrates that people who infer it from artifacts get
it wrong in both directions (Fang et al.'s 15.9% error→fraud reclassification; Steen's inflated
"error" bucket). A programme that cannot observe a variable must not code it.

COPE's own drafting standard for retraction notices is the operative model here: wording "should
be limited to proven facts," notices "should not engage in speculation (such as about motives or
elements that are unproven)," and any misconduct finding must be "properly attributed to the
finding body (eg, following an institutional or funder investigation)." The programme is not a
finding body.

Operationally:

- **No IMPACT value names a cause.** Compare the RW gradient in §1.1: this scheme lives in
  "Error in X" and "Unreliable X" and never enters "Falsification/Fabrication of X."
- **Prohibited vocabulary** in findings and comment text: *fabricated, falsified, manipulated,
  fraudulent, misconduct, deceptive, dishonest, deliberate, intentional, knowingly, concealed,
  p-hacked, HARKed, cherry-picked, misleading*. Also prohibited: *careless, sloppy, negligent* —
  these assert a state of mind about competence and are no more observable than intent.
- **Required construction:** state the observable, its source, and the discrepancy. "The
  deposited data yield X; the article reports Y." Not: "the authors changed Y to X."
- **The prohibition survives provocation.** It holds however strong the pattern looks, including
  where the artifacts would support an inference an ordinary reader would draw unaided. Auditors
  do not need to pretend the pattern is invisible — they state the observable precisely and stop.
  A precise description of what the deposit contains is the strongest thing the programme can
  say, and it is strong enough.
- **`artefact_of_construction` is a statement about the code, not the coder.** It says the
  pipeline entails its output. It does not say anyone arranged that, or knew.
- **Escalation, not adjudication.** Where auditors believe the artifacts warrant an integrity
  investigation, they say so to the coordinator through a private channel and that suspicion stays
  out of the comment. Bodies with investigative powers and a right of reply make those findings; the
  programme supplies observations to them and nothing more. The private channel carries suspicions
  about conduct only. A disclosure (Field 3) defect is an observation about the deposit, so it is
  always reported in the comment and never routed around it.
- **Field 3 states observables, never intent.** A disclosure finding names the kind of identifier,
  the file and column, and the counts, never a value; it does not say why the data were deposited
  that way. `ethics_approval_absent` records an absent record. It does not allege that anyone evaded
  review.

### Rule 4 — Evidence sufficiency

Every finding carries a reproduction path: the artifact inspected, the operation performed, the
expected value, the observed value. A finding no second verifier could re-derive from the comment
is not admissible at any tier. For T4–T5, where the check is a judgement, the alternative
specification or structural argument must be stated explicitly enough to be *disagreed with*.

### Rule 5 — Field independence

`impact`, `verification_tier`, and `disclosure` are assigned independently, and no value of one
constrains any value of another. Specifically: a `disclosure` defect never raises `impact` (a
consent problem is not evidence the result is wrong), and `impact: none` never suppresses a
disclosure flag. `disclosure.urgency: immediate` is flagged on its own in the comment and in
`comment.json`, independent of impact; it is never held back from the comment.

### Rule 6 — Numerical tolerance

Differences attributable to floating-point, RNG seeds, platform, or solver version are `none`,
recorded with the observed magnitude. The threshold for `value_wrong` is a difference not
explicable by the computational environment. Following ACM: "exact replication or reproduction of
results is not required, or even expected... differences in the results should not change the main
claims made in the paper." Where a tolerance judgement is contestable, state the observed
difference and the reasoning; do not round it away.

---

## Part 5 — Worked examples

Generic constructions. None refers to a real paper.

**`none`** — Article reports a regression coefficient of 0.42 (SE 0.11). The deposited data and
script rerun cleanly and return 0.42 (SE 0.11). Two figure panels differ in the third decimal,
consistent with a change in the solver's default convergence tolerance between library versions.
→ `impact: none`, T3, magnitude of the residual difference recorded.

**`unverifiable`** — Article's headline estimate comes from a matched sample. The deposit contains
the raw data and the analysis script, but the matching script that produces the analysis sample is
absent, and the analysis sample itself is not deposited. The headline number cannot be regenerated
by any route in the package. → `impact: unverifiable`, `reason: code_missing`, T2 (thirty minutes
to establish the gap), `blocked_tier: 3`. Explicitly **not** `none`: nothing here suggests the
estimate is wrong.

**`value_wrong`** — Article states a sample of 1,204 in the abstract and methods; every table
reports 1,240; the deposited dataset has 1,240 rows and the analyses use all of them. The abstract
figure is a transposition. No estimate, interval, or conclusion depends on it. → `impact:
value_wrong`, T1, `paper_only: true` (the internal inconsistency is visible in the article alone;
the deposit only confirms which figure is correct). Consistent with a corrigendum.

**`conclusion_narrowed`** — Article claims an intervention "improves outcomes across all three
sites." Reproduction confirms the pooled effect, but the deposited site-level results show the
effect is present at two sites and indistinguishable from zero at the third, with the pooled
estimate driven by the largest site. The claim holds for the pooled sample and for two sites, not
as stated. → `impact: conclusion_narrowed`, T3.

**`conclusion_unsupported`** — Article reports a predictive model and states it "generalises to
unseen patients," citing an accuracy figure. The deposited pipeline fits the model and computes
accuracy on the same rows used for fitting; there is no held-out split, no cross-validation, and
no external set anywhere in the code. The generalisation claim has no supporting computation. →
`impact: conclusion_unsupported`, T5 (establishing that *no* validation exists anywhere in the
pipeline requires reading it in full). The model may generalise; the paper does not show it.

**`conclusion_contradicted`** — Article reports a statistically significant treatment effect
(p = 0.03). The deposited data and the deposited script, run as supplied, return p = 0.31 with an
effect of the opposite sign. No documented variation in the package reproduces the published
figure. The deposit contradicts the article. → `impact: conclusion_contradicted`, T3. Stated as
the discrepancy between two artifacts; no explanation of the discrepancy is offered, and none is
the programme's to offer.

**`artefact_of_construction`** — Article reports a classifier achieving AUC 0.94 for predicting an
outcome from baseline covariates. The deposited feature-construction script derives one predictor
from a variable recorded after the outcome, and that variable is a deterministic function of the
outcome. The model has the label among its inputs. No hyperparameter, resample, or alternative
estimator changes this: the pipeline returns a near-perfect score for any input data with the same
column structure. → `impact: artefact_of_construction`, T5. *Variant:* the same conclusion is
sometimes reachable at T1 and `paper_only: true` — where the article's own confusion matrix yields
an accuracy below the no-information rate of its class distribution, the reported performance is
incompatible with the reported data on the face of the paper.

**Disclosure co-occurring with `none`** — A package reproduces exactly (`impact: none`, T3). The
deposited analysis file also contains a free-text field with participant-supplied descriptions
including workplace and job title, alongside a five-digit postcode and date of birth, in a public
repository. → `disclosure.flags: [identifiability_direct, identifiability_indirect]`,
`urgency: immediate`. Reported in the comment as a disclosure-flagged finding naming the file and
its free-text, postcode and date-of-birth columns with row counts, without any value; filed with
I4R. Reproducibility and disclosure
are unrelated here, which is the point of Field 3.

---

## Part 6 — JSON

```json
{
  "comment_id": "RA-2026-0147",
  "target_doi": "10.0000/example.2024.001",
  "audit_date": "2026-08-05",

  "impact_max": "conclusion_contradicted",
  "impact_max_rank": 5,
  "impact_max_finding": "F3",
  "residual_unverifiable": true,
  "claims_affected": 2,
  "paper_only": false,
  "tier_of_max": 3,
  "tier_distribution": { "T1": 1, "T2": 1, "T3": 1 },
  "total_expected_verifier_hours": 13.75,

  "findings": [
    {
      "finding_id": "F1",
      "impact": "value_wrong",
      "impact_rank": 2,
      "locus": "reporting",
      "verification_tier": 1,
      "paper_only": true,
      "expected_verifier_hours": 0.25,
      "check_nature": "deterministic",
      "claim_ref": "abstract, sentence 3",
      "artifact": "published article, Table 1 vs abstract",
      "operation": "compare reported N across article sections",
      "expected": 1240,
      "observed": 1204,
      "statement": "The abstract reports N = 1,204. Tables 1-4 report N = 1,240. The deposited dataset contains 1,240 rows.",
      "consistent_with": "correction (corrigendum)"
    },
    {
      "finding_id": "F2",
      "impact": "unverifiable",
      "impact_rank": 1,
      "locus": "cleaning",
      "verification_tier": 2,
      "blocked_tier": 3,
      "unverifiable_reason": "code_missing",
      "paper_only": false,
      "expected_verifier_hours": 0.5,
      "check_nature": "deterministic",
      "claim_ref": "section 5.2, robustness table",
      "artifact": "deposit/code/",
      "statement": "The script constructing the matched sample used in Table 5 is not present in the deposit, and the matched sample is not deposited. Table 5 cannot be regenerated by any route in the package.",
      "note": "No inference is drawn about whether Table 5 is correct.",
      "consistent_with": "editorial data-availability query"
    },
    {
      "finding_id": "F3",
      "impact": "conclusion_contradicted",
      "impact_rank": 5,
      "locus": "estimation",
      "verification_tier": 3,
      "paper_only": false,
      "expected_verifier_hours": 13,
      "check_nature": "deterministic_with_tolerance",
      "claim_ref": "section 4.1, main result",
      "artifact": "deposit/code/main_analysis.R + deposit/data/analysis.csv",
      "operation": "execute as supplied, unmodified, R 4.4.1",
      "expected": { "estimate": 0.42, "p": 0.03 },
      "observed": { "estimate": -0.09, "p": 0.31 },
      "statement": "Running the deposited analysis script against the deposited data as supplied returns an estimate of -0.09 (p = 0.31). The article reports 0.42 (p = 0.03). No documented variation in the package reproduces the published values.",
      "tolerance_note": "Difference is not attributable to environment, seed, or solver version; sign differs.",
      "consistent_with": "retraction"
    }
  ],

  "disclosure": {
    "flags": ["licence_absent"],
    "urgency": "routine",
    "statement": "The deposit states no licence for either data or code. Reuse rights are undefined.",
    "consistent_with": "correction (deposit metadata)"
  },

  "assertions_withheld": {
    "intent": "not_assessed",
    "misconduct": "not_assessed",
    "note": "This programme reports only what the artifacts show. It makes no finding as to cause, intent, or good faith, and recommends no editorial action. 'consistent_with' fields locate findings against published editorial standards and are descriptive only."
  }
}
```

---

## Part 7 — Sources

Every source below was consulted for this document. Confidence is marked:
**[FULL]** full text read · **[PARTIAL]** partially retrieved · **[ABSTRACT]** abstract only ·
**[BLOCKED]** not retrieved.

### Normative standards

1. **[FULL]** COPE Council. *COPE Retraction Guidelines — English*, Version 2, November 2019.
   DOI [10.24318/cope.2019.1.4](https://doi.org/10.24318/cope.2019.1.4) →
   https://publicationethics.org/guidance/guideline/retraction-guidelines
   *Access note below.*
2. **[FULL]** ICMJE. *Recommendations for the Conduct, Reporting, Editing, and Publication of
   Scholarly Work in Medical Journals*, updated January 2026. §III.A Corrections and Version
   Control; §III.B Scientific Misconduct, Expressions of Concern, and Retraction.
   https://www.icmje.org/icmje-recommendations.pdf ·
   https://www.icmje.org/recommendations/browse/publishing-and-editorial-issues/corrections-and-version-control.html
3. **[FULL]** NISO. *RP-45-2024, Communication of Retractions, Removals, and Expressions of
   Concern (CREC)*. 27 June 2024. DOI 10.3789/niso-rp-45-2024.
   https://www.niso.org/publications/rp-45-2024-crec ·
   https://groups.niso.org/higherlogic/ws/public/download/30869/NISO_RP-45-2024_CREC.pdf
4. **[FULL]** NISO. *RP-31-2021, Reproducibility Badging and Definitions*. DOI
   10.3789/niso-rp-31-2021. https://www.niso.org/publications/rp-31-2021-badging
5. **[FULL]** ACM. *Artifact Review and Badging*, v1.1, 24 August 2020.
   https://www.acm.org/publications/policies/artifact-review-and-badging-current
6. **[FULL]** Crossref. *Crossmark participation* — the 12 defined update types.
   https://www.crossref.org/documentation/crossmark/participating-in-crossmark/
   *Crossref supplies the list without definitions; only the list is used here.*

### Retraction-reason taxonomies

7. **[FULL]** Retraction Watch Database, *User Guide Appendix B: Reasons*.
   https://retractionwatch.com/retraction-watch-database-user-guide/retraction-watch-database-user-guide-appendix-b-reasons/
   Also the parent guide: https://retractionwatch.com/retraction-watch-database-user-guide/
8. **[FULL]** Grieneisen ML, Zhang M (2012). A Comprehensive Survey of Retracted Articles from the
   Scholarly Literature. *PLoS ONE* 7(10):e44118. Table 2, 15 justification categories.
   https://doi.org/10.1371/journal.pone.0044118
9. **[FULL]** Fang FC, Steen RG, Casadevall A (2012). Misconduct accounts for the majority of
   retracted scientific publications. *PNAS* 109(42):17028–33.
   https://doi.org/10.1073/pnas.1212247109 · https://pmc.ncbi.nlm.nih.gov/articles/PMC3479492/
10. **[FULL]** Steen RG, Casadevall A, Fang FC (2013). Why Has the Number of Scientific
    Retractions Increased? *PLoS ONE* 8(7):e68397. https://doi.org/10.1371/journal.pone.0068397
11. **[ABSTRACT]** Steen RG (2011). Retractions in the scientific literature: is the incidence of
    research fraud increasing? *J Med Ethics* 37(4):249–53.
    https://doi.org/10.1136/jme.2010.040923
12. **[ABSTRACT]** Steen RG (2011). Retractions in the scientific literature: do authors
    deliberately commit research fraud? *J Med Ethics* 37(2):113–17.
    https://doi.org/10.1136/jme.2010.038125
13. **[FULL]** Teixeira da Silva JA (2022). Publishing formats for correcting the record.
    *J Gen Philos Sci* 53(4):583–99. https://pmc.ncbi.nlm.nih.gov/articles/PMC9159037/

### Correction classification and severity

14. **[FULL]** Farrah K, Rabb D (2019). Errata in systematic-review-relevant trials. *JMLA*
    107(2):187–93. https://doi.org/10.5195/jmla.2019.629 ·
    https://pmc.ncbi.nlm.nih.gov/articles/PMC6466495/ — *source of the Major/Minor/Trivial
    definitions quoted in §1.2.*
15. **[FULL]** Zahn-Muñoz et al. (2026). *Iberoamerican J. Science Measurement and Communication*
    6(1):1–13. DOI 10.47909/ijsmc.289.
    https://ijsmc.pro-metrics.org/index.php/i/article/download/289/190
16. **[FULL]** Margalida A, Colomer MÀ (2016). Improving the peer-review process. *PeerJ*.
    https://pmc.ncbi.nlm.nih.gov/articles/PMC4756748/
17. **[FULL]** Molckovsky A et al. (2011). *Curr Oncol*.
    https://pmc.ncbi.nlm.nih.gov/articles/PMC3031354/
18. **[FULL]** Bhatt et al. (2014). *J Community Hosp Intern Med Perspect*.
    https://pmc.ncbi.nlm.nih.gov/articles/PMC4246137/
19. **[ABSTRACT]** Bentan MA, Fenton E, Coelho DH (2024). *Ear Nose Throat J*.
    https://doi.org/10.1177/01455613241266467 — *erratum/corrigendum origin split.*
20. **[FULL]** Fanelli D (2013). Why Growing Retractions Are (Mostly) a Good Sign. *PLoS Med*
    10(12):e1001563. https://doi.org/10.1371/journal.pmed.1001563 — *Essay, not a research
    article.*
21. **[FULL]** Allison DB, Brown AW, George BJ, Kaiser KA (2016). Reproducibility: A tragedy of
    errors. *Nature* 530:27–29. https://doi.org/10.1038/530027a — *Comment; authors explicitly
    disclaim rate estimates.*

### Reproducibility auditing

22. **[FULL]** NASEM (2019). *Reproducibility and Replicability in Science*. DOI 10.17226/25303.
    Conclusion 3-1. https://nap.nationalacademies.org/read/25303/chapter/6
23. **[FULL]** Institute for Replication, *Template Reproduction/Replication Report*.
    https://osf.io/8dkxc/ · econ/poli-sci template: https://osf.io/download/a8q4m/
24. **[FULL]** Brodeur A, Mikola D, Cook N, Fiala L, et al. Working paper: I4R DP No. 107 / IZA DP
    No. 16912, April 2024. https://docs.iza.org/dp16912.pdf — *three-tier discrepancy severity and
    coding-error taxonomy.*
25. **[ABSTRACT]** Published version: Brodeur et al. (2026). Reproducibility and robustness of
    economics and political science research. *Nature* 652(8108):151–156.
    https://doi.org/10.1038/s41586-026-10251-x · Publisher Correction: *Nature* 653(8114):E7, DOI
    10.1038/s41586-026-10503-w
26. **[FULL]** BITSS. *ACRE — Assessment*, the L1–L10 scale.
    https://bitss.github.io/ACRE/assessment.html
27. **[FULL]** AEA Data Editor, *replication template*, "Classification" section and failure-reason
    vocabulary.
    https://raw.githubusercontent.com/AEADataEditor/replication-template/master/REPLICATION.md ·
    https://social-science-data-editors.github.io/
28. **[FULL]** cascad. https://www.cascad.tech/what · https://www.cascad.tech/how
29. **[FULL]** Nuijten MB et al. (2016). The prevalence of statistical reporting errors in
    psychology (1985–2013). *Behav Res Methods* 48(4):1205–26.
    https://doi.org/10.3758/s13428-015-0664-2 · https://pmc.ncbi.nlm.nih.gov/articles/PMC5101263/
30. **[ABSTRACT]** Fišar M et al. (2024). Reproducibility in Management Science. *Management
    Science* 70(3):1343–56. https://doi.org/10.1287/mnsc.2023.03556

---

## Part 8 — What could not be verified

Listed so nothing above is taken on more authority than it has.

1. **COPE's live version.** The full v2 (November 2019) text was read, but
   `publicationethics.org` returned HTTP 403 to every direct request; the text came from a
   third-party mirror of the official PDF. The DOI resolves correctly to the official page, so the
   citation is sound. **However, ICMJE's January 2026 text cites "2025 COPE guidelines" at that
   same URL.** A newer COPE version may exist that could not be retrieved. *Before publication,
   confirm the current COPE version and re-check the quoted criteria against it.*
2. **Retraction Watch reason count.** The Appendix B page rendered as 110 enumerated reasons in
   one retrieval and 111 table rows in another, both on 2026-08-05. The document says "~111." A
   figure of "137" circulating in secondary sources is **not** supported by either retrieval. The
   *structure* relied on in §1.1 — the four-step gradient across data, image, and
   results/conclusions — is verified directly and does not depend on the total.
3. **Fanelli, Ioannidis & Goodman (2018), "Improving the integrity of published science: An
   expanded taxonomy of retractions and corrections," *Eur J Clin Invest* 48(4):e12898, DOI
   10.1111/eci.12898 — [BLOCKED].** Paywalled at Wiley, and no abstract exists in PubMed, OpenAlex,
   or Semantic Scholar. Despite having the most directly relevant title of anything in the
   literature, **nothing from it informed this scheme.** This is the largest known gap. *Retrieve
   through the uOttawa library and reconcile Field 1 against it before the scheme is finalised.*
4. **Steen (2011a) eight-reason breakdown.** Only "scientific mistake" (31.5%) and "ambiguous"
   (18.1%) are in the abstract; the remaining six are in a paywalled table. Not cited.
5. **Dreber & Johannesson (2025)**, *Economic Inquiry* 63(2):338–56, DOI 10.1111/ecin.13244 —
   metadata only. The five-type reproducibility/replicability frame is quoted from the I4R
   template, which attributes it to this paper; the attribution was not verified at source.
6. **Terminological conflict, unresolved and load-bearing.** ACM's *Reproducibility* /
   *Replicability* are **inverted relative to NASEM's** for the artifact-reuse case; ACM footnotes
   that it swapped the terms after discussions with NISO. This scheme follows **NASEM** —
   reproducibility = same data and code, which is what this programme does. Any comment citing ACM
   badges must say which convention it uses.
7. **cascad rating glosses.** The current site gives only RRR / RR / R / D / DD with short labels.
   Older secondary sources gloss these as "Minor discrepancies / Potentially serious / Serious
   discrepancies"; the live site does not, and those glosses are not used here.
8. **Grieneisen & Zhang internal inconsistency.** Their Results state 601/4,232 (14%) of notices
   gave no reason; their Table 2 note 1.d says "~18%." §1.1 cites 14%, from the Results text. The
   discrepancy is in the source.
9. **Effort figures in Field 2 are estimates, not measurements.** The T1–T5 hour ranges are
   design-time judgements calibrated against the effort implied by the AEA, ACRE, and I4R
   workflows. No source publishes verifier-hours per finding type. *These should be replaced with
   observed medians once the programme has run enough comments to measure them* — recording
   `expected_verifier_hours` alongside actuals from the first cohort is the cheapest way to get
   there, and the JSON is structured to support it.
