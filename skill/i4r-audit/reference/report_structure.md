# I4R comment structure

**Hard cap: 10 rendered pages, everything included** — front matter, references, any appendix.
`build_docx.sh` exits non-zero above 10 (override with `MAX_PAGES=`), so verify page count after every
rebuild. Page 1 is the
summary page; the body gets the rest. The full evidentiary record — benign explanations ruled out,
adjudication history, scripts — ships in `report/LEDGER.md` and `report/ADJUDICATION.md` beside the
package, never in the comment. When the document outgrows the cap, cut prose, not findings: any
finding can shrink to its Table 1 row plus two body sentences.

Front matter goes in `report/front_matter.md`:

```
::: {custom-style="Title"}
**A comment on "<exact paper title>"**
:::
::: {custom-style="Authors"}
First Author^1^\*, Second^1^, Third^1^
:::
::: {custom-style="Affil"}
^1^ Institute for Replication, Department of Economics, University of Ottawa
:::
::: {custom-style="Affil"}
\* Correspondence: <name>, <email>
:::
::: {custom-style="Affil"}
**Original study:** <full citation>. DOI: <doi>
:::
::: {custom-style="Affil"}
**Replication package:** <repository and URL>
:::
```

Use the `:::` divs exactly as above and **do not end `front_matter.md` with a `---` rule**. Pandoc
reads a trailing `---` as the start of a YAML block, swallows the body and fails with a parse error
that points at the wrong file.

**Keep the `**` bold markers inside the Title div.** The `custom-style` attribute only takes effect on
the `.docx` path, where it maps to the reference document's Title paragraph style; every other output
ignores it, so without the inline bold the title renders as ordinary body text and the document opens
with no visible title at all. The bold markers apply in both paths.

Body in `report/REPORT_final.md`, using `## N. Title` headings that `build_docx.sh` converts to
`Section N.`.

## Page 1 — executive summary and summary tables

Three elements, in this order, all fitting the first rendered page:

| Element | Requirement |
|---|---|
| `## Executive summary` | ≤200 words, counted (tables excluded). What reproduces, the substantive errors, and whether any published conclusion changes. No method narrative, no caveat inventory. |
| Table 1 — Findings | One row per finding: # · Finding (one line, plain words) · Status (CONFIRMED / LIKELY / SPECULATIVE) · Changes a published conclusion? (Yes / No) · Section. Every body finding has exactly one row here; a finding absent from this table does not exist. |
| Table 2 — Files | One row per package file the comment cites: File · Role (data / code / output) · Issue ("—" if none). Aggregate uncited files as one closing row with a count ("remaining N files: not cited"). Never place a participant identifier and a date on one row. |

## Body — the remaining pages

| Section | Content | Budget |
|---|---|---|
| 1 | Scope and method, including what was **not** verified; the protocol citation (house rule 2) | ≤ half page |
| 2 | What reproduces (first — it is usually the dominant result) | ≤ half page |
| 3…k | One section per substantive finding, most consequential first | ~2.5 pages total |
| k+1 | Minor issues — one table collapsing text-vs-data discrepancies with no numerical impact, mechanical reproducibility, reporting omissions, and cosmetic items: Item · Location · Impact ("none" is the usual entry) | table only |
| k+2 | Hypotheses tested and refuted — a table: Hypothesis · Test · Result. Tests of the **paper**, never corrections of a draft | table + ≤3 lines |
| k+3 | Recommendations — numbered, one line each, remedy verified as possible | ≤ quarter page |
| Last | Data and Code Availability, then References | — |

Data protection: a substantive exposure is a §3…k finding; routine hygiene notes go in the k+1
table.

`report/captions.txt`: one caption per line, in document order, one per markdown table.

**There is no "where our auditors disagreed" section and no revision-history appendix.** Both were
in an earlier version of this file and both had to be deleted from finished comments twice, because
house rule 4 forbids narrating the audit. Internal disagreement and withdrawn claims live in
`report/ADJUDICATION.md` and `report/LEDGER.md`, which ship as the audit record — never in the
comment. A comment is a document about the paper, not about how it was written.

## Per-finding template (§3…k)

**What** — ≤2 sentences, plain words first, with file, sheet, cell or line references ·
**Evidence** — one line naming the script in `work/` that reproduces it ·
**Impact** — one sentence; write "None." when none ·
**Remedy** — one sentence.

Name a benign explanation only when a plausible one exists, in one sentence stating how it was
ruled out; the full list lives in the ledger.

## Objectivity

- Every finding is stated once in its body section and once in its Table 1 row — nowhere else.
- No intensifiers or build-up ("strikingly", "remarkably", "it is worth noting"). Lead with the
  fact; the numbers carry the weight.
- Every sentence carries a fact, a number, a file reference or a consequence. Anything else is a
  cut candidate.

## Claim tags (replicator handoff)

When the audit feeds an external replicator, every finding statement in the body carries its claim
number inline — `[claim 3]` — matching `comment.json` order, the page-1 findings-table row, and the
claims script. Write them into `REPORT_final.md`; `build_docx.sh` strips them from the published
deliverable (and prints the count) so the tags never reach the journal. One tag per finding at the
sentence that states it, not on every mention. `check_consistency.sh` §4 blocks release when table
rows and tags disagree.
