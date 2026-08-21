# Adjudication and the critique loop

## Adjudicating auditors
Re-run the disputed computation. Never prefer an auditor. A tested negative beats an untested
assumption. Log decisions in `report/ADJUDICATION.md`.

**Preserve superseded decisions rather than deleting them.** Mark them WITHDRAWN with the reason and
add a corrections section. A log that shows a wrong decision and its correction is better evidence of
care than one that shows only right answers. The log ships, so keep it consistent with the comment.

## Running critique rounds
Critics run **sequentially** — concurrent sessions on one account get throttled and all die.
Each writes its file incrementally so a cutoff loses nothing. Three attempts each, resumable.

## Responding to criticism
1. **Verify before accepting.** Run the check yourself. In calibration a critic's claim that a parser
   dropped a participant was wrong — the row mapping was off by one — and accepting it would have put
   a false finding in the comment.
2. **Verify in the right language.** That error happened because the claim was about an R call and it
   was checked with pandas.
3. **Contest what is wrong, and show the reproduction.** A locale-dependent parsing failure survived
   three critics saying it did not occur, because the mechanism was identified and stated.
4. **Look for the stronger version.** When told a robustness claim was too narrow, the fix was to test
   more weightings — which made the conclusion stronger, not weaker.
5. **Accept corrections to your own errors loudly.** Put them in Appendix A.

## Convergence
Stop when a round returns only wording. Track the character of criticism, not just counts:
substantive errors → framing → prose precision. Report the table.

Calibration trajectory: R1 framing overreach + 1 factual error; R2 a conclusion reversed and a false
claim caught; R3 mostly precision + a self-inflicted privacy lapse; R4 two factual; R5 zero findings
changed.

## Priming the critics

The critique prompts are paper-agnostic and are filled from `PAPER.txt`, `ENV.txt` and `SLOW.txt` in
the audit directory. **Check the rendered prompt before trusting a round.** In calibration a critic
was primed with the previous audit's paper and returned a confident critique of findings that did not
exist in the report under review — including a verdict line reading "its two principal substantive
findings survive". Nothing in the output looked wrong.

A cheap gate, worth running every round:

```bash
grep -ci "<current paper keyword>" critique/round_N/prompt_stats.txt   # expect > 0
grep -ci "<previous paper keyword>" critique/round_N/prompt_stats.txt  # expect 0
grep -c '{{' critique/round_N/prompt_stats.txt                          # expect 0
```

## The findings ledger

Synthesis is the step where findings get lost. Both calibration audits lost one there, and in both
cases an adversarial critic restored it several rounds later — expensive, and only lucky.

Before drafting, list every finding from every auditor in `report/LEDGER.md`:

| Auditor | Finding | Disposition |
|---|---|---|
| 3 | second cluster feature ≠ Methods description | INCLUDED §5.4 |
| 1 | participant present in raw data, absent from all rosters | EXCLUDED — no numerical impact |

`EXCLUDED` needs a reason you would defend to a critic. "No numerical impact" is *not* sufficient on
its own: most findings in a good audit have no numerical impact, and reporting them is the point. A
defensible exclusion is closer to "superseded by §7, which states the same defect more generally".

Then, when the report is drafted, check every ledger row appears where it says it does.


## Applying corrections mid-round

The driver freezes `REPORT_TO_REVIEW.md` at the start of each round, so critics later in the same
round review the pre-correction text. Two workable policies:

- **Fix as you verify** (recommended). You get errors out of the document sooner, at the cost of
  later critics re-flagging things already fixed. Keep `critique/round_N/APPLIED.md` listing what
  changed after the snapshot, so a re-flag is dismissed in seconds rather than re-litigated.
- **Freeze for the round.** Cleaner bookkeeping, but a known-false claim sits in the draft through
  every remaining critic — in calibration that would have meant a contaminated file count surviving
  four more reviews.

Either way, never treat a re-flag of an already-fixed item as independent confirmation of the
problem, and never let it reopen a settled question.


## Propagate every correction to page 1

The executive summary and the two summary tables (findings, files) restate the findings in
compressed form, so a correction applied only to the body leaves a contradiction in the most-read
part of the document. This happened repeatedly in calibration — a scope narrowed in section 3 but
not up front, a claim conditionalised in section 9 but left unconditional in the summary, a figure
corrected in section 5 and still wrong on page 1. One of them was caught only by rendering the PDF
and reading page 1.

After every batch of corrections, grep the summary and both tables for the old wording — a
correction that changes a finding's status or impact changes its findings-table row too — and
re-read page 1 end to end before the next round.


## Detecting a finished critic

Critics write a skeleton early so a cut-off loses nothing, and some skeletons pre-fill placeholder
verdicts (`— UNVERIFIABLE`, "Audit in progress", "Pending"). A file that exists, is non-empty, and
even contains verdict markers may still be a stub. Gate on the *absence* of progress language:

```bash
done=$(grep -cE -- '— (CONFIRMED|REFUTED|OVERSTATED)' "$f")
stub=$(grep -ciE 'in progress|^pending|assessment pending' "$f")
[ "$done" -gt 0 ] && [ "$stub" -eq 0 ] && echo finished
```

Or simply trust the driver log's `[ok  ]` line, which is written only after the process exits.
