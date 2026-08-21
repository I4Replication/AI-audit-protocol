# Critique prompts — how to keep these portable

These files are **paper-agnostic by construction**. Only three placeholders may carry
paper-specific content, and they are filled at run time from the audit directory:

| Placeholder | Source | Missing |
|---|---|---|
| `{{PAPER}}` | `$AUDIT_DIR/PAPER.txt` | hard error |
| `{{ENV}}`   | `$AUDIT_DIR/ENV.txt`   | generic default |
| `{{SLOW}}`  | `$AUDIT_DIR/SLOW.txt`  | generic default |

## The rule: no worked examples

Do not put a concrete finding, variable name, statistic, file name or count into a role prompt,
however illustrative. Describe the *kind* of check to perform, not an instance of it.

This is not fussiness. During calibration these prompts were carried over from a previous audit and
patched by find-and-replace three separate times. Each pass removed the obvious references and each
pass missed a site:

1. the paper citation in `common.txt`;
2. a cached-object path and a worked statistic in `common.txt` and `role_stats.txt`;
3. an entire instruction in `role_data.txt` — "verify the 57%HRmax claim (they say 488/488 rows are
   at 0.56)" and "check the paper really does print '0.37 FS units'" — none of which existed in the
   paper under review.

A grep can find a stale author name. It cannot find a stale *finding*, because there is no pattern
to match. The only sound fix was to rewrite each role prompt so it contains no instances at all.

Symptom to watch for: a critic returning `UNVERIFIABLE` on claims you do not recognise, or
confidently confirming findings that are not in your report. Both mean the prompt is stale.

## The fourth leak, and why grep kept failing

Four separate leakage sites were found across two audits, in four different rounds. The pattern of my
own attempts to fix them is the lesson:

1. Replaced the citation in `common.txt`. Grepped for the author name. Missed a cached-object path.
2. Rewrote `role_stats.txt`. Grepped for the statistic names. Missed an entire instruction in
   `role_data.txt` about a variable from the previous paper.
3. Rewrote `role_data.txt` and `role_fairness.txt`, then ran a "final sweep" against a keyword list —
   `CRAVE`, `SESOI`, `Youden`, `HRmax`, and so on — which returned zero for `role_completeness.txt`.
4. A critic then found that `role_completeness.txt` was still directing reviewers to
   instrument-native files, questionnaire column counts and measures from the previous study. My
   keyword list simply did not contain those words.

**The method that failed, four times, was grepping for terms I could think of.** The method that
worked was rewriting each file so it contains no instances at all — and I applied it to five of six
files, then trusted grep on the sixth precisely because grep said it was fine.

A fifth site turned up on the next paper, in a file I had already rewritten once:

5. `role_fairness.txt` still told the critic that "their own adjudication log says one auditor's
   source notes contain such pairings" — a **finding from a previous audit asserted as a fact about
   the current one** — and sent it looking for birth dates in a package that contains none. On the
   same pass, `common.txt` told every critic that "the supplement is paper/supplement.txt" for a paper
   that has no supplement.

Two lessons from the fifth. A prompt can carry a stale finding *without containing any of the
previous paper's nouns*, which is why keyword sweeps keep passing. And an instruction asserting that
a file or a fact **exists** is more dangerous than one merely aimed at the wrong topic, because the
critic will either hunt for it or quietly reason about what it must have contained. Both sites were
found by reading the files end to end, which remains the only method that has ever worked.

If you port these prompts, do not sanitise them. Rewrite them. Then read each rendered prompt once
before the first round, asking of every sentence: **does this assert that something exists?**

## Gate

`run_critique.sh` renders the prompt before each round and refuses to start if a keyword from
`PAPER.txt` is absent or a placeholder is unsubstituted. That catches a stale paper *name*; it will
not catch a stale finding. Read a rendered prompt yourself the first time you use these on a new
paper.
