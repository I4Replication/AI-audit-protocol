---
name: i4r-audit
description: "Run a full Institute for Replication (I4R) reproducibility audit of a published paper and produce a publishable comment. Use when the user asks to audit, reproduce, replicate-check, or write an I4R comment on a paper with a replication package; when they point at a folder holding a paper PDF plus an OSF/figshare/GitHub archive; or when they mention reproducibility reports, replication packages, or checking whether a paper's numbers hold up. Fans out parallel auditors over reproduction, data integrity, text-vs-data consistency, code fidelity, and privacy, then hardens the draft through adversarial critique rounds driven by an external model. Do NOT use for peer review of unpublished manuscripts, for literature reviews, or when there is no replication package to check."
---

# I4R reproducibility audit

**Gate set G-2026-08-21** — the unified skill, merged from the JP and Bruno branches on the day's
team decisions. Comments cite this tag. One standing rule inherited from that meeting: **no material
this skill produces for replicators ever names the discipline** (not "sports", not "diet") — every
instruction, template, and page must be reusable verbatim for the next cohort.

Produces an I4R-format comment on a published paper from its replication package. Two phases:
**fan-out** (five parallel auditors) then **adversarial hardening** (independent critics, external
model, iterated to convergence).

The method's value is that it reports what it *checked and dismissed* as prominently as what it
found, and that it lets an adversary correct the auditors. In the calibration run it caught two
substantive errors in the paper — and four errors of the auditors' own, including one claim that had
to be withdrawn entirely.

## Autonomy

**Run the whole pipeline to completion without pausing for approval.** Fan-out, synthesis, every
critique round, and the deliverable. Do not stop to ask which finding to keep, whether to continue to
the next round, or whether a correction is worth making — verify it and decide. The only things that
warrant interrupting the user are a genuine ethical fork (responsible disclosure timing) or a missing
input you cannot obtain (author list, package URL). Everything else is the skill's job.

**The specific failure this is guarding against.** In calibration the pipeline stalled twice, and both
times it looked like diligence rather than stalling: the round finished, the corrections were applied,
a clear status report was written — and the turn ended. The user then had to say "run the next round."
A round is not a deliverable. **After integrating round N, launch round N+1 in the same turn.** Do not
end a turn on a round boundary; end it when the loop's own stopping rule fires (convergence, or the
round cap), or when a genuine blocker appears. Writing a good summary of where things stand is not a
substitute for continuing, and status reports between rounds are how an autonomous loop quietly
becomes a manual one.

A useful check before ending any turn mid-audit: *is there a command I could run right now without
asking?* If yes, run it.

## Before starting

**Step zero, before touching any paper: reserve it.** Six people audit this tree concurrently, and
the reservation file is the only thing standing between you and duplicating someone's day:

```bash
bash scripts/reserve_paper.sh claim "<discipline-root>/<Journal>/<Paper>"   # yours, or REFUSED
bash scripts/reserve_paper.sh list  "<discipline-root>"                    # who holds what; STALE flags
```

Set `I4R_OWNER=yourname@yourmachine` (or accept the `user@host` default). The tree syncs by Dropbox,
not a lock server, so the claim waits a grace window and backs off automatically if a competing
claim predates it (Dropbox surfaces the race as a "conflicted copy" — earliest `claimed_at` wins).
A claim older than 7 days with no completed report shows STALE in `list`; release it with
`release <paper-dir> --force` only after checking with the owner. `publish_completed.sh` releases
your reservation automatically when the finished audit is filed.

Confirm you have: the paper (PDF and extractable text), the complete replication package, and any
supplement. Extract the archive into `pkg/` and the paper into `paper/`. Then:

```bash
bash scripts/setup_workspace.sh <audit-dir> <paper.pdf> <package.zip>
```

`init_run.sh` records the machine's software state — OS, R and its notebook packages, Python and
its readers, pandoc, LibreOffice, the Codex CLI — into `run.json`'s `environment` field at init,
because versions reconstructed later describe a different machine (the Codex CLI alone changed
twice in one week of audits). If the capture warns, fix it before Phase 1; finalize treats a
missing environment as a defect in the record.


Read `reference/checklist.md` — the eleven-item audit checklist distilled from published I4R
comments — before briefing anyone.

## Run metadata — capture it live or lose it

Every audit writes two JSON records: `run.json` (how the audit ran) and `comment.json` (what it
found, one per audited paper, the meta-paper's unit of analysis). Schemas and scripts live in
`metadata/` — `schemas/run.schema.json`, `schemas/comment.schema.json`, and three scripts.

**None of this is reconstructable afterwards.** In the first cohort the auditor-phase cost data
never existed, token logs covered five of thirty critique rounds, and six of ten audits' working
records were gone before anyone asked for them. A value that was not observed is `null` — never an
estimate, because an estimate is indistinguishable from a measurement once it is written down.

| When | Command |
|---|---|
| Immediately after `setup_workspace.sh` | `bash metadata/scripts/init_run.sh <audit-dir> --doi <doi> --auditor-model <id> --auditor-family <fam> --auditor-provider <prov>` |
| Then, for the rest of the run | `export RUN_JSON=<audit-dir>/run.json` and call `log_event.sh` |
| At every phase boundary | `log_event.sh phase_start phase=fan_out` / `phase_end phase=fan_out` |
| Whenever a role runs | `log_event.sh model role=critic_stats role_class=critic round=3 model_id=<id> model_family=<fam> provider=<prov> source=observed` |
| After each round | `round_start` / `round_end` / `round_result round=3 blocking_items=… comment_defects=… paper_defects=… integrated_at=…` |
| Usage, if the provider reports it | `log_event.sh tokens role=… round=… total_tokens=…` / `cost role=… amount=…` — `obtainable=false` when it does not |
| Every failure and substitution | `log_event.sh failure kind=quota_exhausted role=… detail='…' substitution='…'` |
| Every auditor correction to your brief | `log_event.sh brief_correction raised_by=… description='…'` |
| After every gate | `log_event.sh gate gate=check_consistency round=3 exit_status=$?` — capture `$?` on the next line, never later |
| At delivery | `log_event.sh delivery artifact_path=… ` |
| Human time, per phase | `log_event.sh human_effort phase=critique person_minutes=95` — self-report, and the only numerator the AI-assisted vs human-only comparison has |
| When `SETTLED.md` is generated | `log_event.sh settled used=true generated_after_round=2` — otherwise the protocol's largest cost lever stays unmeasurable |
| At close | `bash metadata/scripts/finalize_run.sh <audit-dir> --stopping-reason converged\|cap\|aborted` |

`log_event.sh` is safe to call repeatedly and concurrently: it locks, rewrites through a temporary
and renames, so a parallel critic driver cannot corrupt the record. `finalize_run.sh` computes the
durations and the compliance vector, validates **both** files against their schemas, exits non-zero
if either fails, and only then files the pair into the internal registry with an index row.

Three things this exists to catch, each of which has already happened and been invisible: a model
silently swapped mid-run; "independent" critics colliding with the auditor family after a quota
failure; and a comment shipped before the critique that was supposed to harden it existed. All three
are booleans in `run.json`'s compliance vector, and all three are false-by-evidence, not by memory.

The registry is **internal**. Both files carry `safe_to_publish`, defaulting false, and a
`publication_redaction` block naming what must go before any extract is released.

Findings are classified against `metadata/SCHEME.md` (v1.0) on three **orthogonal** fields that must
never be collapsed into one score: `impact` (what was found about the claim, a seven-value ladder cut
at the conclusion), `verification_tier` (T1–T5, what it *costs* a verifier to check — a cost measure,
which is why it is not called severity), and `disclosure` (eight flags, four urgency levels, and the
only field whose defect alone can be consistent with removal). `paper_only` is a flag, not a tier,
and those findings are verified first. `unverifiable` is never recorded as `none`, and carries both
its own tier and the `blocked_tier` of the check it prevented.

**One thing this instrumentation reproduced while being written**, worth keeping because it is the
cheapest possible illustration of the rule: `log_event.sh` validated each rewrite with `jq empty`
before renaming it over `run.json`. `jq empty` exits **0 on an empty file**. Combined with a separate
bug that fed jq no input, the "integrity check" certified a zero-byte file and the rename destroyed
the record — while the test asserting "run.json still parses" reported ok. A detector that cannot
distinguish success from total failure is worse than none, because it launders the failure. The check
is now on shape and event count, and the same instinct applies to every gate here: assert what the
artefact *contains*, never merely that a tool exited 0 on it.

## Default models and reasoning effort (team decision 2026-08-21)

| Role | Model | Effort |
|---|---|---|
| Coordinator (this session: brief, adjudication, ledger, drafting, gates) | **Opus 5** | session default |
| Phase-1 auditors and any sub-agent (ledger extractors, verifiers) | **Sonnet 5** — `model: 'sonnet'` | **`effort: 'max'`** on every Agent call |
| External critics and synthesis (`run_critique.sh`) | **GPT 5.6 Terra** — `gpt-5.6-terra` | **`model_reasoning_effort=max`** |

This is an audit: the cost of a missed finding or a wrong correction is a published error, which
dwarfs the token cost of thinking harder. Overrides: pass `model:`/`effort:` when spawning, or
`CRITIC_MODEL=`/`CRITIC_EFFORT=` (also accepts `MODEL=`/`EFFORT=`) for the critics.

**Trust but verify the Codex side.** The CLI writes whatever effort string you pass into its session
rollout regardless of validity, so the rollout alone proves nothing; an invalid value is rejected by
the API with `invalid_request_error`, so output at all means the level was honoured (tested: `max`
and `xhigh` accepted, `banana` rejected). `scripts/verify_codex_model.sh` confirms the served MODEL
per session after each round — a plan that auto-downgrades on quota would otherwise attribute one
model's findings to another. Record wall-clock and tokens per phase with `scripts/run_meta.sh`
(start/end/tokens/report) — that is the protocol paper's Q17, and it cannot be reconstructed later.

## Phase 1 — five parallel auditors

Spawn all five in ONE message so they run concurrently. Each gets: the shared brief
(`reference/brief_template.md`, filled in), its area from `reference/checklist.md`, and an
instruction to write `report/<NN>_<area>.md` incrementally.

| # | Area | Core question |
|---|---|---|
| 1 | Reproduction | Do the published numbers come back out of the raw data? |
| 2 | Data integrity | Duplicates, impossible values, fabrication signatures |
| 3 | Text-vs-data consistency | GRIM/GRIMMER, table algebra, figure measurement |
| 4 | Code and methods | Does the code do what the Methods say? |
| 5 | Privacy and archive hygiene | PII, licence, usability, self-containment |

**Brief every auditor to report negative results.** A hypothesis tested and refuted is a finding.
Require each to label claims CONFIRMED / LIKELY / SPECULATIVE and to list benign explanations
considered and ruled out.

**Do not trust your own shared brief.** In calibration it stated the wrong file count and missed an
entire data folder; an auditor caught it. Tell them to correct you.


## Phase 2 — synthesis and adversarial rounds

1. Read all five reports. Adjudicate disagreements **by re-running the disputed computation**, not
   by preferring an auditor. Log every decision in `report/ADJUDICATION.md`, including ones you
   later withdraw — see `reference/adjudication_guide.md`.
2. Write `report/REPORT_v1.md` using `reference/report_structure.md`.

   **Before writing, build a findings ledger — from the full reports, not the summaries.** Each
   auditor returns a summary message *and* writes a long report. Build the ledger by reading
   `report/*.md` end to end. In calibration a real finding — a Discussion section contradicting the
   paper's own Results — appeared twice in an auditor's report, never in its summary, and was
   therefore missing from the ledger and the draft until an adversarial critic found it two rounds
   later. The ledger cannot catch what synthesis never saw.

   Enumerate every finding from all five auditor
   reports into `report/LEDGER.md`, one row each, and mark it `INCLUDED`, `MERGED into <section>`, or
   `EXCLUDED — <reason>`. Nothing may be dropped silently. In both calibration audits a real finding
   was lost at exactly this step and had to be restored by an adversarial critic later: an
   undocumented participant in the first, and a clustering feature that did not match its Methods
   description in the second. Synthesis compresses, and compression is where findings die.
3. Run critique rounds until convergence (**production default: 2 rounds**; raise toward the hard
   cap of 5 for a contested or high-stakes audit). Launch each round immediately after integrating
   the previous one — see **Autonomy**.

   **Round 2 exists to review round 1's own corrections.** Round 1 reliably catches the draft's
   overclaims; what it structurally cannot do is review the text written *in response* to it.
   Measured on three comments, that unreviewed surface was **43%, 28% and 8% of the final word
   count** — with one round, nearly half a comment can ship unexamined, and it is exactly where the
   worst errors have appeared. So round 2 must be scoped: generate `SETTLED.md` after round 1
   (`scripts/make_settled.sh`) and write `WRAPUP.txt` naming the sections that changed and stating
   that this is the final round. Stopping at 2 is recorded as the production default, not as
   convergence; a round that still returns factual corrections is a reason to run another.

Before the first round, write three context files into the audit directory — the prompts are
paper-agnostic and are filled from them:

| File | Contents | Missing? |
|---|---|---|
| `PAPER.txt` | citation, package location, what kind of analysis it is | **hard error** |
| `ENV.txt` | interpreters, installed packages, venv paths, version caveats | defaults |
| `SLOW.txt` | which jobs are expensive, so critics budget their time | defaults |

`PAPER.txt` is mandatory by design. In calibration the prompts still carried the *previous* paper's
citation, and a critic produced a fluent, confident critique of findings that were not in the report
— the failure surfaced as reassurance, not as an error.

```bash
bash scripts/run_critique.sh <round-number>     # needs AUDIT_DIR set
```

**Runtime, and how to keep it sane.** In calibration the critique loop took **11.7 hours across seven
rounds** — about 94% of total pipeline time. Phase 1's five auditors run in parallel and cost ~45
minutes; the serial critique loop is the whole problem. Four levers, in order of payoff:

| Lever | Mechanism | Saving |
|---|---|---|
| Stop re-deriving settled facts | `scripts/make_settled.sh` → `SETTLED.md`, injected into every prompt | ~40–50% of critic time |
| **Two critics from round 2** | automatic (was round 5, moved 2026-08-22); `CRITICS=` to override | **~40 min/round** |
| **Skip synthesis at ≤2 critics** | automatic; read the two reports directly. `SYNTH=1` forces it, `SYNTH=0` always skips | **~25 min/round** |
| **Round 1 runs 4-up** | automatic (was 2-up); `PARALLEL=2` if throttling returns | **~25 min** |
| Target the changed sections | `WRAPUP.txt` naming them | rounds so scoped ran 43–49 min against 80–109 |

**Why round 2 needs only two critics.** Measured across the Philyaw and Refalo audits: round 1
refuted a false claim and added a finding all five auditors had missed; round 2 produced **zero** new
findings about either paper and only corrections to the audit's own record. By round 2 the failure
mode has already flipped from "missed finding", which redundancy catches, to "wrong correction in our
own new text", which two critics catch as well as four.

**Why synthesis is skippable at two critics.** It is a serial pass that re-reads work already done,
and it cost 22–25 minutes per round. With four reports it earns that by deduplicating and
adjudicating between them. With two it does not — and in both audits the coordinator read the critic
reports directly while synthesis was still running, and got everything the consolidated file later
said.

Together these take roughly **90 minutes off a four-hour audit** with no reduction in adversarial
coverage.

`SETTLED.md` is the big one. Across seven rounds, four critics each rebuilt the analytic frame,
refit every model and re-ran the clustering — roughly 28 full reconstructions of artefacts that never
changed and never once disagreed. Generate it after round 2, once two reviewers have independently
confirmed the core counts, and hand-fill the counts table from their reports.

Sequential execution was itself an over-correction: four concurrent critics were throttled to death
in round 1, and the fix went straight to one. Two survives; `PARALLEL=1` remains available if
throttling returns.

**Consider fewer rounds.** Calibration rounds 6 and 7 found *zero* new defects in the paper — every
finding was an error in our own comment. That is worth one late round, not three. Four to five
targeted rounds gets substantially the same comment for a third of the time.

4. For each round: verify every criticism yourself before accepting it, apply what survives, and
   record accept/contest decisions in `critique/round_N/RESPONSE_notes.md`. Contest criticism that
   is wrong — but show the reproduction.

   **Then run the consistency check before rebuilding anything:**

   ```bash
   bash scripts/check_consistency.sh <audit-dir>/report/REPORT_final.md \
        <audit-dir>/report/retired_claims.txt
   ```

   It guards the two failures that recurred in both calibration audits and were never caught by
   vigilance alone. **Corrections reaching the body but not the abstract** happened three times — the
   third was ranked BLOCKING by an external critic, because the abstract is the public summary and it
   contradicted the qualification the body had just adopted. **Republishing a participant identifier
   beside a date or clock** happened twice, once in a release-bound draft, in a comment whose own §9
   asks the authors to remove exactly that linkage.

   Maintain `retired_claims.txt` as you go: every time you withdraw a claim, add its distinctive
   phrase. That file is what turns "remember to propagate" into something mechanical.
5. Stop when a round returns no factual corrections, only wording. Report the convergence table.

   **Announce the endgame.** Once you know how many rounds remain — because convergence is close, or
   because you are approaching the cap — say so **in the critics' prompts**, by writing
   `WRAPUP.txt` into the audit directory (see below). Critics that know a round is the last-but-one
   prioritise differently: they stop opening new lines of inquiry, close out what they raised earlier,
   and separate "must fix before release" from "would be nice". Without that signal the final round
   reads like any other and tends to open new threads that nobody will have time to resolve.

   **Expect the cap, not convergence, in a hard audit.** In calibration, round 5 produced *more*
   corrections than round 4. The reason is structural and worth planning for: **text written in
   response to round N has not been reviewed by round N.** Every round's fixes are new prose with no
   adversarial exposure. Two consequences:

   - The last round should be **targeted at the sections added or rewritten since the previous
     round**, not spread over the whole document. That is where the unreviewed surface is.
   - If you stop on the cap, say so plainly in the release notes and **list the sections that no
     critic has reviewed**. "Five rounds complete" reads as convergence to anyone who was not
     watching, and it is not the same claim.

## House rules for the comment itself

These are not stylistic preferences. A comment that breaks them gets sent back.

**1. A comment is a paper, and reads like one (team decision 2026-08-22).** Abstract, numbered
sections, conclusion, references — not a briefing note. There is **no executive summary** and **no
summary table in the body or on page 1**. The abstract is ≤250 words of prose, counted, stating what
reproduced, the substantive errors, and whether any published conclusion changes. §1 is an
Introduction; the last numbered section is a Conclusion. The **findings table lives in Appendix A**,
at the end, one numbered row per finding — it is an index to the argument, not a substitute for it,
and a reader must meet the findings as prose first. `check_consistency.sh` blocks a numbered findings
table anywhere before the appendix, and blocks a table inside the abstract.

The front matter carries **Original study** and **Replication package** lines. They are not optional:
they are the first thing an editor checks and what makes the comment self-locating on its own.

The abstract must **finish on page 1**, checked on the rendered PDF. There is **no Recommendations
section**: each finding carries its own one-line Remedy, beside its evidence.

The comment is capped at **10 rendered pages of body** — front matter through the last numbered
section. **References and appendices start on their own page and do not count**, so a comment never
loses a finding to make room for its bibliography. `build_docx.sh` finds the first back-matter page
and counts what precedes it.

Prose is house style: one idea per sentence, fact before qualification, active voice, numbers inside
the sentence, no intensifiers. Citations and URLs render **blue**, cross-references to tables,
figures and sections **red**; `build_docx.sh` colours them by anchor prefix and reports the counts. Cut prose, not findings: shrink a finding to two body
sentences and move the evidence detail to LEDGER.md/ADJUDICATION.md, which ship with the package; if
it still does not fit, merge adjacent sections. Never move the appendix table into the body to save a
heading, and never drop a numbered claim. Left unchecked, abstracts drift to 800–1100 words, because
every round adds a clause and none removes one; re-count after every round.

**2. Always cite both I4R AI-reproducibility papers**, in a `## References` section, and reference
them once in §1 as the protocol the comment was produced under:

> Brodeur, A., Valenta, D., Marcoci, A., Aparicio, J. P., Mikola, D., Barbarioli, B., Alexander, R.,
> Deer, L., Stafford, T., Vilhuber, L., Bensch, G., et al. (2026). AI-assisted teams outperform
> AI-led teams but not human-only teams in assessing research reproducibility in quantitative social
> science. *Proceedings of the National Academy of Sciences*, 123(22), e2524747123.
> https://doi.org/10.1073/pnas.2524747123 · https://github.com/I4Replication/AI-Games
>
> Institute for Replication (2026). Reproducing with AI Across the Expertise Ladder. Pre-analysis
> plan, OSF: https://osf.io/dkfzt/ · https://github.com/I4Replication/AI-vertical

**3. Write for a reader outside the paper's field.** The audience is an editor, a replicator, and
the authors — not a specialist in the paper's subdiscipline. Assume none of them shares your
statistical vocabulary.

- **Gloss inline, at first use, inside the sentence you were already writing.** Not in a glossary,
  not in a "note on terminology" box, not in a bulleted key. Papers do not have those, and adding one
  makes the comment read like a tutorial. Write "without `n_init`, the number of times the algorithm
  restarts from a fresh random guess before keeping its best answer" — the definition rides along and
  the sentence still does its job. A table header can carry a three-word gloss: "Inertia (lower =
  tighter groups)".
- **Expand an abbreviation the first time and then use it**, the ordinary convention: "a minimal
  perceived change (MPC) of 0.77 FS units". If the abstract already does this, the body needs nothing.
- **Lead every finding with the problem in plain words**, then give the technical detail. "The
  column cannot take any value but zero" before the prior-support argument; "the control condition
  does not do what the Methods describe" before the sampling analysis.
- Do not strip precision to achieve this. A comment still has to name `n_init`, R-hat and the
  Youden index, because vagueness is how a finding becomes unfalsifiable. Gloss them; don't avoid
  them.
- Section headings state the finding, not the topic. "The archived figure is label-reversed" beats
  "Figure issues"; "The cluster analysis does not reproduce as shipped" beats "Clustering".

The test: could a careful reader from a different field say what is wrong with the paper, and how
much it matters, after one pass? If the answer depends on them already knowing what a posterior is,
rewrite the sentence — do not add a glossary to compensate for it.

**4. Never narrate the audit process.** The comment is a finished document about the paper, not a
record of how it was made. It must contain **no** mention of drafts, revisions, rounds, critics,
adversarial review, models, or anything you got wrong along the way. Specifically banned:

- "an earlier draft of this comment…", "we withdraw…", "corrected in round 4"
- "a critic showed…", "our critics disagreed…", "following adversarial review…"
- sections titled "Changes from earlier revisions" or "Where our auditors disagreed"
- revision banners in the front matter

State findings in the present tense as settled conclusions. If a claim did not survive, it is simply
absent — a reader does not need to know it was ever there. Where a *negative* result is worth
reporting ("we tested X and found nothing"), report it as a test of the **paper**, never as a
correction of a previous draft.

Rule 4 matters beyond tidiness. Process narration reads as hedging, invites the authors to litigate the
audit rather than the findings, and buries the reproducibility results the comment exists to deliver.

The place for all of it is `ADJUDICATION.md` and `LEDGER.md`, which ship with the replication package
as the audit record. Keep them scrupulously — just keep them out of the comment.

## Phase 3 — deliverable

```bash
bash scripts/build_docx.sh <audit-dir> <output-name>
```

Produces an I4R-format `.docx` (title block, `Section N.` headings, captioned tables above tables,
`Data and Code Availability`, `Appendix A`) plus a PDF preview. Verify by rendering pages to images
and reading them — table captions inserted by line number **will** land inside tables.

**Then ship the verification notebook alongside it. `finalize_run.sh` will not file the audit
without one** — step 4b fails, nothing reaches the registry, and `ALLOW_NO_VERIFICATION=1` is the
only bypass, which writes the waiver into `run.json` so the gap is on the record. This paragraph
used to be advice. Thirteen consecutive audits read it and shipped without a notebook, and by the
time anyone went looking the working directories had been deleted and the code behind every
published number was gone with them. Every numerical claim in the comment has to be independently
re-checkable by a human who did not run the audit — all of them, not only the conclusion-changing
ones.

```bash
bash scripts/make_verification_notebook.sh <audit-dir>     # emits verification.Rmd
Rscript -e 'rmarkdown::render("verification.Rmd"); \
           quit(status = as.integer(readLines("verification_status.txt")))'
```

It extracts one claim per bolded numbered lead plus one per abstract sentence carrying a numeral,
carries each claim's auditor and adjudication entry into its section, and emits a **TODO stub** for
any claim it cannot check — stubs are counted as failures, so an unfinished one cannot pass quietly.
Write the check bodies into `<audit-dir>/verification_snippets/<claim-id>.R`, with
`.judgement` and `.unverifiable` variants for claims that are an assessment or that the deposit
cannot settle. Two rules the notebook enforces mechanically: the expected value is printed **before**
the observation exists, and failures are collected and tabulated rather than thrown. Always R,
whatever the package was written in — the format dispatcher is in the setup chunk. See
`reference/verification_guide.md`, which ships with the notebook for the verifier.

## Where the audit lives

**Never run an audit in a session scratchpad or anywhere under `/tmp`.** Pass `setup_workspace.sh` a
directory beside the paper in the programme folder. This is not tidiness: the run schema was written
because the first ten audits' working records were destroyed before anyone read them, and it then
happened to all thirteen — the scratchpad was cleared and the verification code, the critic logs,
the auditor reports, the ledgers and the adjudication records went with it. The comments survive
because they were copied out; nothing else was.

Three specific things die with the working directory, and each is unrecoverable rather than merely
inconvenient:

| Artefact | What is lost | Can it be rebuilt? |
|---|---|---|
| `report/REPORT_final.md` | the generator's only input | No — `make_verification_notebook.sh` cannot run without it. A `pandoc -t markdown` of the delivered `.docx` recovers the section structure and the numerals, which is a reconstruction, not the original |
| `critique/round_*/log_*.txt` | per-critic token counts, the model id and the reasoning effort in each session header | No. Six audits have no token data at all for exactly this reason |
| `verification_snippets/*.R` | the code behind every number in the comment | No |

`stage_release.sh` copies the deliverable out. It does not copy the audit record. Do that too,
before the session ends — the notebook, the snippets, the ledger and the adjudication log belong
beside the comment.

## Phase 3b — replicator handoff (decided 2026-08-21)

When the comment will be verified by an external replicator rather than published directly, build
the handoff package. Four rules, fixed at the 2026-08-21 team meeting:

**1. Tag every finding in the report prose with its claim number** — `[claim 3]`, inline, matching
`comment.json` order. The tags are the join key between the report, the claims script, and the
landing-page verdict survey; never renumber one artifact without the others. `build_docx.sh` strips
the tags from the published deliverable automatically and reports how many it removed;
`REPORT_final.md` keeps them.

**2. Author code first, in the paper's own language.** Where the deposit ships code, the claims
script carries the AUTHORS' OWN code block, verbatim with file and line range — a replicator who
confirms AI-written R has confirmed our code, not the paper's — and the claims file itself takes
the deposit's language and extension (`claims_script.do` for a Stata deposit, `.R` for R, `.sps`
for SPSS syntax), with that language's comment markers. Audit-written checks stay R: inline and
executable when the file is R, embedded as clearly-marked run-in-R blocks otherwise. **When the
deposit ships no code at all, the file falls back to R** — what replicators can run for free and
what the verification checks already are. The attachment is judgment work: write it as
`claims_map.tsv` in the audit dir, one `<finding-id>\t<source>` line, where source is
`snippet:<path>`, `author:<path>:<a>-<b>`, or `none`.

**3. Map only executable checks.** A `judgement()` deferral handed to a replicator as "the code for
claim N" is a stub they will run and mistake for a check — the false-clean failure again, exported.
Before handoff, every RETRACTION- or CORRIGENDUM-level claim should have an executable snippet or an
author-code block; convert stubs first, and leave to the no-code tail only what is genuinely
verified by inspection.

**4. Nothing is dropped for lacking code.** Claims verified by arithmetic or inspection (impossible
p-values, missing datasets, missing variables) go in the script's `## CLAIMS WITH NO CODE` tail with
a one-line manual check each. These are most of the findings in the worst papers and they must get
verdicts like the rest.

Then:

```bash
bash scripts/make_claims_script.sh <audit-dir>       # writes claims_script.<ext> in the deposit's language
bash scripts/make_claims_script.sh --selftest        # 12-check fixture; run after editing it
bash scripts/publish_completed.sh <audit-dir>        # files PDF + claims script into "Completed reports"
```

**5. Every finished audit is filed where the team can see it.** `publish_completed.sh` copies four
hash-verified files into `<discipline-root>/Completed reports/<Journal>/<Paper>/`, deriving journal
and paper from the audit's own path: the final PDF, the claims script, and **`run.json` and
`comment.json`** (added 2026-08-22). The folder holds exactly those four; anything else already
there blocks the publish. This is the team's one glance-able index of what is done.

The metadata pair lives here *as well as* in the registry because the registry is keyed by audit id
and is not browsable by paper: someone reading a finished comment wants the record of how it was
produced beside it, not in a directory named after a hash. The registry copy stays canonical, and
`publish_completed.sh` refuses a pair that `finalize_run.sh` has not accepted — an unvalidated
record filed next to a finished comment reads as authoritative and is not. Both files carry
`safe_to_publish: false`; "Completed reports" is internal, so this is not a release, but read the
`publication_redaction` block before forwarding the folder anywhere.

Severity labels are mapped MECHANICALLY from `comment.json` — never re-judged at handoff:
`conclusion_contradicted`/`conclusion_unsupported` at CONFIRMED → **retraction material**; the same
at LIKELY, or `conclusion_narrowed`/`value_wrong`/`artefact_of_construction` → **corrigendum
material**; everything else, and anything SPECULATIVE → **comment material**. The script and this
paragraph must agree; edit both or neither. The replicator-facing rule rides in the script header:
a retraction- or corrigendum-level claim you cannot verify means RETURN THE PAPER, and nobody
contacts authors or journals except I4R.


## Non-negotiables

- **Never publish an identifier paired with a date, timestamp or name.** If the audit reports
  quasi-identifiers, the audit's own files must not reproduce them — this has now happened in **both**
  calibration runs, from auditors pasting archive paths into inventory tables. Redact across every
  artefact, not just the deliverable, and rescan.
- **Validate your detector before trusting a zero — and validate its SCOPE, not just its pattern.**
  Six false cleans across three audits now. Two in calibration: a scan matching only `dd.mm.yyyy` that
  missed ISO dates, and a scan using `\b` anchors that returned zero for every format because `_` is a
  word character so the boundary never fires. Four more in one later audit, every one of them with a
  passing positive control:

  | Scan | Why it was clean and wrong |
  |---|---|
  | An auditor's privacy self-check | Covered only its own subdirectory |
  | The retired-claims check | Covered only the abstract, not the body |
  | A release gate | Required an identifier **and** a timestamp on the same line |
  | Its replacement | `\b` before `sub` cannot match the real form `MSTE_AC_sub_…` — the *same* bug as above, reintroduced while fixing a related one |
  | An auditor's path scan | The target file stores its strings in UTF-16BE, so no 8-bit pattern can match — pattern and file scope right, **encoding** scope wrong |

  **A positive control proves the pattern matches something. It says nothing about scope — which files,
  which lines, which forms.** Scope has at least five axes — which files, which sections, which lines, which regex forms, and which
  **text encodings**. State the scope out loud before you run it, test a known-present instance of every
  *form* you claim to cover, decode binary containers before scanning them, and test that ordinary prose does **not** fire. If a
  threshold is involved, make it relative: an absolute count of ten identifiers false-positived on the
  audit's own reports, which legitimately name participants on a few percent of a long document.
  Never write an exemption for a file that trips your own gate — restructure the file instead. In that
  audit the last offenders were positive controls inside two scanners, fixed by holding each control's
  halves on separate lines rather than whitelisting them.
- **Distinguish participant names from author names** before redacting: test each candidate token
  against the published article. A token that appears in the paper is an author or place, not a
  participant.
- **Redact prose deliverables, not machine-readable working data.** String-substituting a CSV or
  JSON that later computation reads will silently break joins and path lookups — in calibration it
  cost four attempts to verify a finding because condition labels inside a derived table had been
  rewritten. Exclude such files from the release or regenerate them clean.
- **Redact with word boundaries, then check for over-matching.** A bare lowercase `.replace()`
  turned "dropped" into "dr[name redacted]ed" and "opponent" into "o[name redacted]onen" in
  calibration. After redacting, grep for `[a-z]+\[name redacted\][a-z]+` and repair.
- **Treat `pkg/` as read-only.** In calibration a co-auditor's `__pycache__` grew the archive by 19
  files mid-audit and another auditor nearly filed a false finding against the authors. Never let
  scripts write inside the extracted package; if a file count moves, suspect yourself first.
- Critics regenerate deleted files, so stage the release into a clean directory and rescan at the
  end, not in place.
- **Responsible disclosure.** If the comment makes sensitive fields easier to locate, notify authors
  and journal before publication.
- **Impact discipline.** State for every finding whether it changes a published conclusion. Most will
  not. Say so plainly; it is what makes the ones that do credible.
- **No motive attribution.** Report what the code does, not why. "We make no claim about why this was
  not caught."

## A note on how these failures cluster

A striking share of the failure modes below are not mistakes in the audit method but **safeguards
interfering with one another**. Redaction protects participants and corrupts the derived tables the
analysis needs. Fixing errors as you verify them gets them out of the document sooner and causes
later critics in the same round to re-flag settled items. Telling critics to write output early
prevents data loss from cut-offs and produces files that look finished but are not.

Each safeguard is individually correct. Expect them to compose badly, and prefer completion and
integrity signals that a safeguard cannot forge — a process exit code over a file's contents, a
regenerated artefact over an edited one.

## Failure modes seen in calibration

| Symptom | Cause | Fix |
|---|---|---|
| All critics die at once | Concurrent sessions on one account get throttled | Run critics sequentially |
| `bash -n` "passes" silently | `&& echo OK` swallows the failure | Check `$?` explicitly |
| Prompt text breaks the script | Apostrophes in shell strings | Keep prompts in files, pipe via stdin |
| A finding evaporates on recheck | Verified a claim in the wrong language | Run the call the claim is *about* |
| A clean scan that is wrong | Detector cannot match what it seeks | Test it against a known-present instance |
| Archive file count changes mid-audit | Your own scripts wrote into `pkg/` | Treat the package as read-only |
| A published result will not reproduce | A library default changed between versions | Pin and vary the default; check release notes |
| Tables truncated in the docx | Caption inserted mid-table | Detect table blocks, insert before block |
| A running round dies with a syntax error on a valid line | The script was edited while executing; bash reads it incrementally | Never edit `run_critique.sh` mid-round. Re-invoke with the same round number to resume — completed critics are skipped |
| Critic confidently reviews the wrong paper | Prompts carry a hard-coded citation from a previous audit | `PAPER.txt` is required; a missing one is a hard error, never a default |
| Critic marks unfamiliar claims UNVERIFIABLE | A stale *finding* survives in a role prompt; grep cannot catch these | Rewrite every role prompt from scratch. Sanitising by keyword failed four times — see `prompts/README.md` |
| A monitor fires but the file is empty of content | Critics write a skeleton early so cutoffs lose nothing | Gate on verdict markers, not on file existence |
| A finding vanishes between auditor report and draft | Synthesis compresses | Build `report/LEDGER.md` first; three findings were lost this way |
| A gate prints FAIL and the run continues | A section's exit status was never checked, so the gate was advisory not blocking. In one audit the retired-claims check printed two FAILs and returned 0 — for four audits it had never actually blocked anything | Check `$?` after **every** block, and test the gate by planting a violation and asserting a non-zero exit |
| A retired claim survives a retired-claims check | The matcher compares exact substrings, so "53 of the 71" in the claims file misses "53 of 71" in the draft | Normalise both sides — case, punctuation, articles, whitespace — before comparing |
| A claim the audit **retracted** is still in the draft | The retraction was logged in `ADJUDICATION.md` and never applied to the body | `check_consistency.sh` now scans the whole document for `retired_claims.txt`, not just the abstract — it used to check the abstract only, so a retracted result sitting in a body section passed clean and two critics found it |
| An adjudicated decision never reaches the comment | Writing "Instruction to the drafter" in the log feels like having acted on it | After each adjudication entry, apply it to the draft in the same turn. Grep the draft for the superseded wording before moving on |
| Redaction mangles ordinary words | Bare lowercase `.replace()` on short name tokens | Word boundaries, then grep for `[a-z]+\[name redacted\][a-z]+` |
| A join silently returns nothing | Redaction rewrote machine-readable working data | Redact prose only; regenerate derived tables clean |
| `random_state` is set but results still vary | The seed permutes an already-nondeterministic ordering | Sort inputs before any seeded operation |
| Thirteen audits ship with no verification notebook | Phase 3 described the step in prose and no gate enforced it, so every audit reported 'complete' without one and nothing flagged it | `finalize_run.sh` step 4b refuses to file without `verification.Rmd`. A documented step that nothing checks is a step that does not happen |
| A notebook exists and verifies nothing | It is all TODO stubs, which read as completed checks in the knitted output | The gate counts stubs and fails on any. A stub is a FAIL, not a silence |
| The code behind every published number is gone | The audit ran in a session scratchpad under `/tmp`, which was cleared between turns | Run audits in the programme folder. See **Where the audit lives** |
| A gate I wrote to catch a missing artefact died on its own arithmetic | `stubs=$(grep -c … \|\| echo 0)` — `grep -c` prints `0` AND exits 1, so the fallback appended a second line and `[ "0\n0" -gt 0 ]` errored, printing a shell diagnostic while the run continued | Never `\|\| echo 0` after `grep -c`. And test every new gate by planting a violation and asserting a non-zero exit — this one was caught that way, on its first run |
| Every `grep` against a shell script returns nothing, including for strings you can see in it | ONE NUL byte inside a jq string literal (`"\0unknown"`) made `file` classify `log_event.sh` as binary, and GNU/BSD grep silently suppresses output for binary files. Not a clean result — a suppressed one, and it hid the very lock code being searched for | `grep -a` when a script might be binary, and `file` it first. The NUL was also a live bug: the jq default compared against `"\0unknown"` rather than `"unknown"`. Same false-clean family as the encoding failure above — the detector was fine, its *input assumption* was not |
| `log_event.sh` deadlocks forever and the run loses its metadata | An ORPHANED lock: `mkdir` succeeded and the owner died before writing `pid` and `ts`. The stale-lock breaker required BOTH files to decide, so it fell through every iteration and the lock could never be broken — every later writer blocked until `LOCK_TIMEOUT`, permanently. Triggered on the first audit run out of a Dropbox directory | Fall back on the lock DIRECTORY's mtime, which exists whether or not the owner wrote anything. Tested against five planted states: orphaned+aged breaks, orphaned+fresh waits, live pid waits, dead pid breaks, no lock writes |
| A test of a new gate reports pass/fail and tested nothing | The harness used `timeout`, which does not exist on macOS, so every case exited 127 (`command not found`) and the script under test never ran. Two cases read as PASS | Assert on the artefact, not the exit code alone — count the events the call was supposed to append. Where the tool under test has its own timeout knob (`LOCK_TIMEOUT`), drive that instead of wrapping it |
| Every critic in a round dies instantly and the log says only "no output file" | The Codex CLI updated and its configured default model became one the account cannot use, so every attempt returned HTTP 400 `model is not supported`. `run_critique.sh` inherited `~/.codex/config.toml` and passed no `-m`, so a config change outside the audit silently broke the round — and the previous audit had recommended pinning and not done it | PIN the model and effort in `run_critique.sh` (`CRITIC_MODEL`, `CRITIC_EFFORT`) rather than inheriting config, and probe the model ONCE before the round so a rejection fails fast with the provider's own message. An unpinned critic model also makes `model_assignments` a fiction: the audit cannot claim which model reviewed it |
| The stub gate counts 0 stubs in a notebook that is 21 stubs and nothing else | It grepped for `TODO: write the check`, a string `make_verification_notebook.sh` never emits. The gate would have passed exactly the artefact it exists to reject, and the self-test agreed because its fixture planted the same invented marker | Anchor a gate on the string the PRODUCER writes, verified by grepping a real generated artefact — never on a plausible-looking marker. The replacement was then one wildcard wide (`TODO . STUB`) and matched the generator's em dash but not a fixture's two ASCII hyphens; `TODO.*STUB` is dash-agnostic, and the `stop()` line is a second independent anchor |
| `finalize_run.sh` aborts with 'no schemas' at the very end of an audit | The skill shipped `SKILL.md`, `prompts/`, `reference/` and `scripts/` but no `schemas/`, so the pair could never be validated. The gap only surfaces at finalisation, after the comment has already shipped | `schemas/` is now part of the skill. Run `finalize_run.sh --no-registry` on the shipped examples at the START of an audit, not the end: it is a two-second check that the toolchain can validate anything at all |
| A hand-completed `run.json`/`comment.json` pair is 'internally consistent' and still fails validation nineteen times | Writing the pair by hand produces plausible free text in enum slots — `locus: 'privacy'`, `character: 'claims_beyond_evidence'`, a sentence in `interval_type`, a boolean in a string field. Internal consistency is not conformance | Validate incrementally while filling the record, never once at the end. Where the true value has no enum slot, map to the nearest allowed code and preserve the original wording in a free-text field on the same object — do not drop it, and do not widen the enum |
| `unclassified_defects` goes negative | `comment_defects` and `paper_defects` partition the **blocking** items only; counting accepted non-blocking corrections into `comment_defects` overflows the total | Record non-blocking acceptances in `non_blocking_items`. The negative residual is the schema catching a miscount, not an off-by-one in the derivation |
| An audit files under an id like `20260807-fanagg_audit-…` | `init_run.sh` was given the working-directory nickname instead of the first author's surname, and nothing downstream checks it | The convention is `<yyyymmdd>-<first-author-surname>-<hash>`. Confirm the FIRST author from the article before init; a comment named for the second author also breaks the deliverable naming series |

## Failure modes added from the CRAVE audit (Stults-Kolehmainen et al., 2021)

| Symptom | Cause | Fix |
|---|---|---|
| A comment reports a number the article never printed | Every check tested whether p followed from r and n. **A fabricated input passes an arithmetic check perfectly.** Nothing tested existence | Derive check inputs **from the source text**, not from a typed list. A parser cannot cite a value the paper does not contain |
| A search of the paper returns nothing for a claim that is there | The article prints a Unicode minus (U+2212); the search used an ASCII hyphen | Accept both forms, and assert the pattern fires on a known-present instance before trusting a zero — the same rule already applied to privacy scans |
| A notebook check reports a clean result having done no work | A helper was defined **lower in the notebook than the chunk using it**; `error = TRUE` kept the run alive; the comparison loop never ran and its counters stayed at zero | Any check whose pass state is *the absence of incremented counters* must separately assert it ran (`stopifnot(cells == N)`), and load its own data with `stopifnot` on the match |
| A check passes unconditionally | The "published" values were transcribed from **our own computed output** instead of the article | Transcribe from the paper with a file-and-line comment saying so. If a check cannot fail, it is not a check |
| Our own script runs here and dies on a reviewer's machine | `lavaan` derives `maxcpu` from `parallel::detectCores()`, which returns `NA` in containers and restricted CPU sets | Guard it. This is the same non-portability the comment reports in other people's deposits |
| Accepting a criticism because its specific catch was valid | Two critics correctly spotted a missing case and both proposed the wrong total | Verify the catch **and** the proposed fix. Settle a disputed count by enumerating the whole set, not by adjudicating the one disputed cell |
| A blanket impact claim that overstates against the authors | "These claims are not supported" was applied to eleven cases, five of which were real associations printed at too strict a threshold | Split findings by what the author must actually do. Five thresholds to relax is a different ask from six claims to withdraw |
| Critics fail with "no output file" and three retries burn | The provider was **out of credits**. The round-1 probe catches a rejected model but not an exhausted balance | Extend the probe to grep for `out of credits` and fail fast with the provider's own message |
| A critic's report looks finished but has no findings | Critics write a skeleton early so a cut-off loses nothing; the skeleton then reads as a verdict of "nothing wrong" | Gate on the process exit code, never on the file existing. Quarantine skeletons rather than integrating them, and record the round as N of M critics |
| Registry validation rejects sensible extra fields | `run.schema.json` and `comment.schema.json` are closed, and `critics_run`/`critics_failed` are **arrays of role names**, not counts | Read `$defs` before writing the record. Put narrative in `notes` (round) or `evidence.tolerance_note` (finding) |

## Failure modes added from the Philyaw audit (Philyaw, Covey & Smith, 2025)

| Symptom | Cause | Fix |
|---|---|---|
| The verification notebook extracts ZERO claims and the stub gate passes it | `make_verification_notebook.sh` matched only the pre-merge report format — bolded numbered leads and `## Abstract`. Under the merged format (`## N.` sections, `## Executive summary`) it matched nothing, and a notebook with no claims has no stubs, so the gate that exists to catch an empty notebook waved it through | The extractor now also reads `[claim N]` tags and accepts either opening heading. More generally: a gate that counts violations passes vacuously on an empty artefact. Gate on the count of things that SHOULD be there, not only on the count of things that should not |
| A whole body section is deleted and every gate still passes | `check_consistency.sh` §4 compares findings-table row numbers against `[claim N]` tags. Both survived in a files table, so the numbers agreed — while the section those rows cited no longer existed. A compression step had silently removed it | Cross-check that each cited section EXISTS, not merely that the numbers match. Still open; worth adding |
| A snippet cannot reference a sibling file | The notebook generator emitted `PKG_DIR` but no `AUDIT_DIR` or `SNIPS_DIR`, and snippets are inlined verbatim into the notebook | All three are emitted now |
| A tolerance is set wide enough to hide the defect it measures | A 0.06 cell tolerance passed a cell the comment reports as wrong by 0.055, and the "25 of 27 reproduce" count came out 26 | Choose the tolerance so it SEPARATES: it must admit every genuine match and neither reported error. Record the reasoning in the snippet |
| Two detectors disagree on a count and both look right | The adjudication tested whether a token appears in the article as a **substring**; the notebook tested whether it appears as a **word**. The substring test is more permissive and silently excluded a token occurring inside a longer word, giving 4 where the word test gave 5 | Report both counts with their rules when the difference is a genuine ambiguity, rather than picking one. Same family as the encoding and scope failures above: the pattern was fine, the *absence test* was not |
| A page-cap fix nibbles for an hour and never lands | Each small prose cut was reabsorbed because the last body page was already full; the cap only moved when a whole structural element moved | When over the cap, cut structure (merge sections, move a table to the ledger), not sentences |
| The staging gate tells you to overwrite the current draft with a stale one | `stage_release.sh` refuses unless `REPORT_final.md` is byte-identical to the highest-numbered `REPORT_v*.md`, and its remedy line says to copy the snapshot over the final — which would discard every round since that snapshot | Cut a NEW `REPORT_v<n>.md` from the current final instead. The gate is right that they differ; its suggested direction is backwards |

## Failure modes — Gomez audit (2026-08-22), first controlled model A/B

This run put five Sonnet 5 and five Opus 5 auditors on one paper over byte-identical prompts. The
comparison is at `report/COMPARISON.md` in that audit; what follows is what the run taught the
skill.

| What happened | Why it matters | What to do |
|---|---|---|
| A diagnostic I called "constant-free" was not. The ratio of antegrade to retrograde shear looked like it cancelled every unknown, and did not: retrograde shear rate falls below the viscosity cap on many rows, so viscosity differs between channels. | It reached the right answer for a reason that does not hold, and it deviated by up to 47 cm/s on rows where the answer was independently known. | Every diagnostic gets a self-test on rows whose answer is known by another route, **before** any zero from it is believed. The self-test is what caught this; without it the finding would have shipped on bad reasoning. |
| Three auditor headline counts were wrong as stated while their findings were right: "11 of 12" abstract figures (really 15 of 16), "70 of 72" retrograde Reynolds (really 61 of 72), "23 of 24 exactly" (really 18 exactly, 23 within the printed rounding). | A comment that carries an unverified count is wrong in public even when its finding is sound. | Re-derive every number that enters the comment. Do not carry an auditor's count across, however confident the report sounds. |
| `make_verification_notebook.sh` anchored each `[claim N]` to the sentence **after** the tag. Every section had the wrong claim text. | It passes every gate — one section per tag, so the count is right and the content is wrong. | Fixed: the extractor now decides by what precedes the tag, and sentence boundaries no longer split on decimal points. Self-tested both placements. |
| The generator baked `STUB_CLAIMS` at generation time, so a notebook with a real check for all 37 claims still reported all 37 as unwritten stubs. | The stub gate is the notebook's whole point; a stub list that never clears makes it uninformative rather than strict. | Fixed: the emitted list is now filtered at runtime against what `expect()`/`judgement()`/`unverifiable()` actually recorded. Self-tested in both directions. |
| `check_consistency.sh` blocks any numbered table in the body, which also catches a legitimate results table whose first column happens to be numbered. | Correct behaviour, surprising in the moment. | Give body tables a non-numeric first column. Reserve numbering for Appendix A. |
| `stage_release.sh` blocked on `/Users/jsmith/data` inside the PII detector's own positive-control fixture. | The fixture is not a leak, but weakening the gate for it would be worse than the false positive. | Assemble such literals at runtime so the detector still sees the string and the released file does not contain it. Two real leaks in auditor scripts were found in the same sweep, which is why the gate stays strict. |
| I invented a first author in `PAPER.txt`. It propagated into the audit id and all ten auditor prompts. | The Opus privacy auditor caught it; the Sonnet one wrote "Corrections to the brief: None" while naming the correct author elsewhere in the same report. | Area 5 stays on the stronger model. Its real job is catching the coordinator's errors, not enumerating files. |
| My pre-registered discriminator did not discriminate. Both arms gave the correct three-part answer on the paired/unpaired question, and both located the `paired = F` calls more precisely than my ground truth did. | A ground truth written before the run is only useful if a null result is reported as readily as a hit. | Record it. `ADJUDICATION.md` A6 and A11 log both places my ground truth was wrong and the arms were right. |

**Model assignment, from the measured result.** Run Sonnet for area 3 (text-vs-data is enumeration
against a fixed corpus; Sonnet's report was ~90% of Opus's at a fraction of the cost). Run Opus for
areas 1, 2, 4 and 5 — every conclusion-changing finding in this audit came from those areas, and
area 5 carries the brief-correction duty. The pattern across all ten reports: **Sonnet localises,
Opus explains**, and causes are where conclusion-changing findings live.

### What round-1 critique caught on the same audit

The A/B above was run with full coordinator verification of every conclusion-changing claim before
the critics saw anything. The round still refuted three claims and narrowed twelve. That is the
argument for the stage, so the specifics are worth keeping.

| What the critique caught | Why coordinator verification missed it |
|---|---|
| **The coordinator "corrected" an auditor and was wrong.** Opus-2 reported 70 of 72 retrograde Reynolds numbers reproducing. I rebuilt the viscosity with its shear-rate term hardcoded at the Methods' cap of 100 s⁻¹, got 61, and logged the auditor as overstating. The cap binds on **all 72 antegrade rows and only 62 retrograde ones**, so the shortcut reproduced antegrade *exactly* and corrupted retrograde. | The shortcut was validated on the channel where it happens to be exact. Detector scope has five axes and this was the regime axis: same formula, same file, different operating range. A simplification that reproduces one channel to 0.01% feels verified. |
| Three claims stated as established fact were conditional: which column is authoritative in the frozen-velocity finding, whether the duplicated pairs are the same people, and whether the Methods' wording requires model-based contrasts. | Verifying that a computation is right is not the same as verifying that the sentence built on it is warranted. The arithmetic was correct in all three cases; the epistemic status was not. |
| "The paper silently uses a second turbulence rule" — it does not; it defines a pulsatile critical Reynolds number and reports against it. | I checked the Methods' fixed thresholds against the Results' claim and stopped there, without reading the two paragraphs that introduce the criterion actually used. |
| "No power column exists" — the column is named `workload`. | The detector's positive control fired, so the zero looked validated. The control proves the search *runs*; it does not prove the search *terms* cover the target. Add a term-coverage check, not just a control. |

**Rules this adds.**

1. When you re-derive an auditor's number and disagree, the disagreement is a finding about one of
   you and you do not yet know which. Re-derive by a second, structurally different route before
   recording the auditor as wrong. Both of my coordinator errors on this audit were corrections of
   a correct auditor.
2. A simplification of a published formula must be validated in **every regime the data occupy**,
   not just the one you tested. Check whether a cap, floor or branch changes state across the rows.
3. Separate "the arithmetic is right" from "the sentence is warranted". State the conditional when
   the deposit cannot choose between two readings, and give the consequence of the reading you can
   compute rather than presenting it as the correction.
4. Run the critique stage even when you believe the comment is verified. On this audit the
   coordinator had personally re-derived every conclusion-changing claim, and the round still
   found four things wrong in about forty minutes of critic time.

**Two script defects fixed while filing this audit.** `stage_release.sh` told the operator to copy
the old snapshot over `REPORT_final.md` when the two differ, which discards the round just
finished; it now names the correct direction and says when the reverse is right. And a
`log_event.sh gate` event recorded only into `compliance.gates_run`, while `finalize_run.sh` checks
the round record — so a compliant audit could not satisfy the consistency-gate check with one call.
The gate event now projects onto both.

## Failure modes — Kritzler audit (2026-08-23), second model A/B

Second controlled A/B, this time on a **clean** paper: a preregistered survey with a
reproducible-manuscript deposit. Where the first A/B measured which model finds more on a broken
paper, this one measured which invents less on a sound one. Comparison at `report/COMPARISON.md`.

| What happened | Why it matters | What to do |
|---|---|---|
| **The critique stage found the biggest thing, and ten auditors did not.** Both headline results depend on pooling across sports; the classification is a cluster-level predictor, and under three standard sport-aware specifications both effects fall above the paper's own α. A fan-out of ten across two model tiers missed it. | The fan-out asks "is this number right?". This paper's problem was "is this the right model?", and enumeration does not reach it. | **Do not trade critique rounds for auditor breadth.** On a paper whose arithmetic is clean, the marginal return on the adversarial stage exceeds the marginal return on a second five auditors. |
| **Opus's errors concentrated in its refutations, not its findings.** Four Opus claims failed verification against none of Sonnet's, and two were the same item: one auditor reported hard-coded country figures as reproducing, another *refuted* the discrepancy by inventing a double-rounding mechanism for a passage with no rounding code. | A refutation is the claim a coordinator is least likely to re-check, because it says there is nothing to look at. A confident wrong mechanism closes the question; an honest miss leaves it open. | **Re-derive an Opus report's refuted hypotheses, not just its findings.** That one rule would have caught three of the four errors here. |
| A detector was scoped to the code shapes of previous audits, so `stage_release.sh` passed a staging area holding 5,546 Qualtrics identifiers and a converted copy of the raw participant data. | Self-tested successfully against its own controls and reported clean on a directory full of the thing it exists to catch. The same failure the audit was reporting about the paper. | Fixed: new check 3b knows no formats, and blocks any file with twenty or more distinct opaque row keys by shape. Self-tested both ways and against hashes. |
| Both arms shared one `work/` directory. One Opus auditor confirmed its file **was** overwritten by the other arm and renamed its artefacts. | Same-name collisions make a cited evidence file possibly the other arm's, and blindness is weaker than claimed. Contamination pushes arms toward agreement, so measured gaps are a lower bound. | Give each arm `work/<arm>/`. One line in setup. |
| Knitting inside `pkg/` made papaja regenerate a deposited file. Two auditors in two arms noticed independently; one disclosed a lapse it had committed itself. | Discipline caught what permissions should have prevented. | `chmod -R a-w` the deposit after the manifest; tell auditors to copy the Rmd into `work/` before knitting. |
| My predicted discriminator failed for the second time running. | Two A/Bs, two wrong predictions, both times the arms outperformed my ground truth. | Keep writing the ground truth first and keep reporting the nulls. A discriminator that fails is a result about the coordinator, not a wasted step. |

**On a clean paper the arms converge.** All ten auditors reproduced the sample; none reported a
false discrepancy on a correct paper. The area split from the first A/B stands, but it matters less
here than the critique budget does.
