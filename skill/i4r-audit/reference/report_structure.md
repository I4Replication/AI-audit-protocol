# I4R comment structure

Pipeline models and review rounds are governed by
[`config/pipeline-policy-v2.json`](../config/pipeline-policy-v2.json). This reference is authoritative
only for the public comment's structure and rendering invariants.

**A comment is a paper.** It reads like one: an abstract, numbered sections, a conclusion,
references. It does not read like a briefing note. There is no executive summary, and no summary
table on the opening page — a reader meets the argument first, in prose, and the machine-readable
inventory waits in an appendix.

**Hard cap: 10 rendered pages of BODY** — front matter through the last numbered section.
References and appendices start on their own page and do **not** count: a comment should never lose a
finding to make room for its own bibliography. `build_docx.sh` locates the first back-matter page,
counts everything before it, and exits non-zero above 10. The canonical limit comes only from the
hash-bound audit policy snapshot; `MAX_PAGES` is rejected. To inspect a deliberately longer draft,
use `--preview-max-pages N`: the result is written below `report/previews/`, marked non-release, and
cannot enter the document cache, staging tree, finalization receipt, or publication.

Draft toward **nine body pages before Round 2**, leaving one page for adversarial corrections. For
the 20-finding regression shape (audit A01), aim for 2,900--3,200 body words while preserving P1--P20,
their statuses, impact classes, evidence/remedy bindings, body treatments, and Appendix rows. Put
extended corroboration and benign-explanation detail in `LEDGER.md` or `ADJUDICATION.md`.

**The abstract must finish on page 1.** It is the first thing an editor reads and it belongs whole on
the opening page beside the citation block. `build_docx.sh` checks the rendered PDF for this and
fails if it spills, with its matcher self-tested on a string known to be on page 1.
The full evidentiary record — benign explanations ruled out, adjudication history, scripts — ships in
`report/LEDGER.md` and `report/ADJUDICATION.md` beside the package, never in the comment.

When the document outgrows the cap, **cut prose, not findings**: shrink a finding to two body
sentences and move its evidence detail to the ledger. If it still does not fit, merge adjacent
sections; never drop a numbered claim, and never move the appendix table into the body to save a
heading.

## Front matter

Goes in `report/front_matter.md`:

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
**Replication package:** <repository and URL, and what it contains>
:::
```

Use the `:::` divs exactly as above and **do not end `front_matter.md` with a `---` rule**. Pandoc
reads a trailing `---` as the start of a YAML block, swallows the body and fails with a parse error
pointing at the wrong file. Any trailing line of only `=` or `-` (even `==` or `-`) is refused too:
Pandoc reads it as a setext underline and turns the byline above it into a heading. **Keep the `**` bold markers inside the Title div**: `custom-style` only
takes effect on the `.docx` path, so without the inline bold the title renders as body text.

The **Original study** and **Replication package** lines are not optional. They are the first thing an
editor checks, and they are what makes the comment self-locating when it circulates on its own.

## Body

`report/REPORT_final.md`, using `## N. Title` headings that `build_docx.sh` renders as `Section N.`

| Section | Content | Budget |
|---|---|---|
| `## Abstract` | ≤250 words, counted. What reproduces, then the substantive errors, then whether any published conclusion changes. Prose only — **no tables** | ≤ ⅓ page |
| 1. Introduction | What the paper claims and how; what this comment audited; the method and its limits, including what was **not** verified and why; the protocol citation (house rule 2) | ≤ ¾ page |
| 2. What reproduces | First, because it is usually the dominant result | ≤ ¾ page |
| 3…k | One section per substantive finding, most consequential first. Heading states the finding, not the topic | ~4 pages total |
| k+1 | Minor issues — discrepancies with no numerical impact, mechanical reproducibility, reporting omissions, cosmetic items | ≤ ½ page |
| k+2 | Hypotheses tested and not confirmed — tests of the **paper**, never corrections of a draft | ≤ ½ page |
| k+3. Conclusion | What the audit establishes and what it does not. Two short paragraphs; no new findings | ≤ ¼ page |
| — | `## Data and Code Availability`, then `## References` — **new page, uncounted**. Both protocol citations from `house_citations.md` are mandatory and are checked | — |
| — | `## Appendix A. Summary of findings` — the numbered findings table, and a deposited-files table where the deposit is large enough to need one. **New page, uncounted** | — |

**There is no Recommendations section** (team decision 2026-08-22). Every finding already carries a
one-sentence **Remedy** in its own section, which is where a reader wants it — beside the evidence,
not collected into a list at the end that repeats all of them out of context.

`report/captions.txt`: one caption per line, in document order, one per markdown table. Write the
caption text only — `build_docx.sh` supplies the `Table N.` prefix, so a caption that begins
"Table 1." renders as "Table 1. Table 1. …".

## Appendix A — where the findings table lives

One row per numbered finding: **#** · **Finding** (one line, plain words) · **Status**
(CONFIRMED / LIKELY / SPECULATIVE) · **Changes a published conclusion?** (Yes / No) · **Section**.

Every included finding has exactly one row and one entry in the generated body cross-reference — `check_consistency.sh`
§4 blocks release when they disagree. The table is an index to the argument, not a substitute for it,
which is why it comes last: a reader who has finished the sections can use it to navigate, and a
replicator can key the claims script to it. **It must never appear in the body or on page 1.**

`scripts/generate_finding_artifacts.py --apply` renders the canonical block to
`report/APPENDIX_A.md` from `work/findings.jsonl`. Replace the Appendix A block in `REPORT_final.md`
with that generated text; do not maintain a second hand-written table.

`build_docx.sh` renders the generated table literally and sets its column widths; the Markdown
source keeps its bytes. In the converter input it escapes every character pandoc would read as
markup in the table's cells, so `*`, `_`, `$`, `<`, `~`, `^`, backticks, brackets and quotes print
as the registry holds them (quotes print straight). Typography still applies as in body text:
`--`, `---` and `...` print as an en dash, an em dash and an ellipsis, and `§4` prints as
`Section 4`. The column widths keep `CONFIRMED`, `SPECULATIVE`, two-digit numbers and a Section
cell such as `Section 10.2` on one line. The build refuses a document whose DOCX cell differs from
the claim text or whose PDF breaks a table word across lines. So write each claim as plain text,
and never edit a claim to change how it renders: the verifier's records are bound to the exact
claim text.

Where the deposit is large or its structure is itself a finding, a second appendix table lists one
row per deposited object: File · Role (data / code / output) · Issue ("—" if none). Aggregate uncited
files into one closing row with a count. Never place a participant identifier and a date on one row.

Release staging (check 3c, `scripts/check_deposit_values.py`) refuses any staged text that
reproduces a value from the deposit's name, e-mail, birth-date, IP, address or phone columns, or
any other e-mail, IPv4 or IPv6 address, so name an identifier by kind, file, column and count, never by
its value.

## Per-finding template (§3…k)

**What** — ≤2 sentences, plain words first, with file, sheet, cell or line references ·
**Evidence** — one line naming the script in `work/` that reproduces it ·
**Impact** — one sentence; write "None." when none · **Remedy** — one sentence.

Name a benign explanation only when a plausible one exists, in one sentence stating how it was ruled
out; the full list lives in the ledger.

## Disclosure findings

Personal data in the deposit is reported in the comment, never withheld, deferred or moved to a
private channel. Each disclosure-flagged finding gets a normal numbered section (a Minor-issues entry
when its urgency is `routine`) and an Appendix A row. **What** names the identifier kind, the
package-relative file and column, and the rows or participants affected, or the directory and a
file count when file names are themselves participant codes. **Evidence** names a count or presence
script that prints no value. **Impact** is usually "None." on the paper's conclusions, because
disclosure is orthogonal to impact. **Remedy** is the verified deposit change: drop or coarsen the
column, then replace the file through the repository. Never write an identifying value, a new
linkage such as a code beside a date, or a procedure for re-identifying anyone, and keep study dates
and clock times off any line that mentions participants.

## Objectivity

- Give every finding one detailed body treatment and one appendix row. The Abstract may summarize a
  substantive finding, but it must not introduce a new claim or contradict the body.
- No intensifiers or build-up ("strikingly", "remarkably", "it is worth noting"). Lead with the
  fact; the numbers carry the weight.
- Every sentence carries a fact, a number, a file reference or a consequence.
- Section headings state the finding: "The archived figure is label-reversed" beats "Figure issues".

**There is no "where our auditors disagreed" section and no revision-history appendix.** Both were in
an earlier version of this file and both had to be deleted from finished comments twice, because
house rule 4 forbids narrating the audit. Internal disagreement and withdrawn claims live in
`report/ADJUDICATION.md` and `report/LEDGER.md`, which ship as the audit record — never in the
comment. A comment is a document about the paper, not about how it was written.

## Claim tags (replicator handoff)

The projection helper writes one ordered body cross-reference entry — `[claim 3]` — matching
`comment.json`, the appendix row, and the claims script. `build_docx.sh` strips that generated block
from the published deliverable and prints the count, so the tags never reach the journal. Do not
scatter hand-keyed duplicates through the narrative.


## Prose

Write it to be read, in the group's house style. The reader is an editor or a replicator going
through a stack of these.

- **One idea per sentence.** Prefer a full stop to a semicolon and a semicolon to a comma splice.
  Long chains of subordinate clauses are the single biggest readability cost in an audit comment,
  because every finding is naturally conditional and the conditions accumulate.
- **Lead with the fact, then the qualification.** "Table 3 mixes two models" before "which matters
  only in Experiment 2".
- **Active voice, and name the actor** when there is one: "the syntax loads no data file", not "no
  data file is loaded".
- **Numbers in the sentence, not in a parenthesis at the end.** "The indirect effect is +0.783
  against a total effect of −2.470" beats "the effects differ in sign (ab = +0.783; c = −2.470)".
- **Gloss a term inline at first use**, inside the sentence you were already writing. No glossary.
- **No intensifiers, no build-up, no throat-clearing.** Cut "it is worth noting", "importantly",
  "strikingly", "we now turn to".

## Links and colour

Match the group's paper template: **citations and URLs in blue, cross-references to tables, figures
and sections in red.**

| Target | Markdown | Rendered |
|---|---|---|
| A reference | `[Montoya and Hayes (2017)](#ref-montoya2017)` | blue, links to the entry |
| The entry itself | `[]{#ref-montoya2017}Montoya, A. K., & Hayes, A. F. (2017). …` | anchor |
| A URL or DOI | `<https://doi.org/…>` or `[text](https://…)` | blue |
| A table, figure or section | `[Table 2](#appendix-a-summary-of-findings)`, `[Section 4](#4-table-3-mixes…)` | red |

The rule is mechanical, so it cannot drift: `build_docx.sh` colours by anchor prefix — `#ref-*` and
any external URL blue, every other internal anchor red — and reports the counts. Anchors for headings
are pandoc's own slugs (lower-cased, punctuation stripped, spaces to hyphens).
