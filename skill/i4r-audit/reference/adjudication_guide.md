# Adjudication and adversarial review

Canonical models, roles, round routing, and stopping rules are in
[`config/pipeline-policy-v2.json`](../config/pipeline-policy-v2.json). This guide defines how the
coordinator turns independent reports into decisions without paying to recompute settled work.

## Merge before adjudicating

Read all five auditor reports, not their return messages. Put every finding and tested negative into
`work/findings.jsonl` with its source and one `ledger_disposition`:

| Source | Finding or tested negative | Status | Disposition | Evidence |
|---|---|---|---|---|
| auditor 3 | … | confirmed | INCLUDED §4 | `work/a3_text_vs_data/check.R` |
| auditor 1 | … | draft | MERGED into §5 | `work/a1_reproduction/reproduce.py` |
| auditor 4 | … | rejected | EXCLUDED — superseded by A7 | — |

`MERGED` requires its target and `EXCLUDED` requires a reason that would survive external review. “No
numerical impact” is not enough; most valid reproducibility findings do not change a conclusion.

Run `python3 scripts/generate_finding_artifacts.py "$AUDIT_DIR" --apply` after every schema-valid batch.
It projects every registry row into `report/LEDGER.md`; only final-status, coordinator-approved
`INCLUDED` rows enter the public Appendix, comment findings, claims map, and verification plan. Edit
the registry, never the generated projections.

Mechanically merge items that agree. Adjudication effort is reserved for:

- disagreements between auditors or between a critic and the draft;
- a new material finding;
- a REFUTED, OVERSTATED, or materially UNVERIFIABLE claim;
- any BLOCKING item;
- any proposed change to a published conclusion.

## Recompute the decision, not the whole audit

Re-run the disputed computation in the language and environment the claim is about. Never decide by
model prestige or majority vote. A tested negative beats an untested explanation.

Require a second structurally different route when:

- the conclusion of the paper would change;
- the coordinator disagrees with an auditor's computed result;
- the coordinator rejects a critic's factual correction.

Different seeds or a second transcription of the same formula are not structurally different. Useful
second routes include an independent implementation, an author-code execution versus a direct
calculation, a paper-only algebra check versus a data recomputation, or a complete enumeration versus
a detector.

For confirmed and settled facts, spot-check a hash, count, or known row. Rebuild from raw only when
the current criticism depends on the rebuild or the spot-check fails.

Log every decision in `report/ADJUDICATION.md`:

```markdown
## A<n> — <short issue>
Sources: <auditor/critic and claim ids>
Question: <exact disagreement>
Checks: <commands, inputs, and independent route when required>
Decision: ACCEPTED | CONTESTED | WITHDRAWN | UNRESOLVED
Evidence: <observations and tolerance>
Required propagation: <report sections, abstract, appendix row, ledger, metadata, verification>
```

Preserve superseded entries as `WITHDRAWN`; never rewrite the log to imply the coordinator was right
on the first attempt.

## Round 1

Run the four independent roles together: stats, data, fairness, and completeness. Each reviews the
same frozen draft. After they finish:

1. Parse verdicts by claim and concatenate issues they say were missed.
2. Deduplicate identical evidence mechanically.
3. Mark only policy-triggered conflicts and high-risk evidence gaps for targeted Terra adjudication.
4. Do not invoke a model to summarize reports when the trigger packet is empty.
5. Record every outcome in `critique/round_1/RESPONSE_notes.md`.

Run the maintained driver with `ADJUDICATE=auto`. It writes a deterministic
`adjudication_packet.json` containing trigger ids, reasons, and source roles. Terra/max runs only for
conflicting verdicts, disputed conclusion-changing claims, high-impact findings without two
independently bound evidence records, or failed settled-fact spot checks. The Sonnet coordinator then
integrates that targeted result. `ADJUDICATE=1` is forced diagnostic mode; `ADJUDICATE=0` is a
recovery mode that emits the packet without the required automatic adjudicator.

The coordinator may apply a verified correction before finishing all adjudication, but every critic
in the round still reviewed the frozen snapshot. Record changes in `APPLIED.md` so a repeated flag is
recognized as stale, not counted as independent confirmation.

After integration, update the registry, `REPORT_final.md`, adjudication log, and retired claims. Rerun
the projection helper, replace the report's Appendix A with the generated block, and run the
whole-document consistency gate before rebuilding the document.

## Settled facts after round 1

Immediately after round 1 is integrated, generate `SETTLED.json` and `SETTLED.md` from the canonical
runtime registry `work/findings.jsonl`:

```bash
bash scripts/make_settled.sh "$AUDIT_DIR" "$AUDIT_DIR/work/findings.jsonl"
```

Each line must be one object satisfying
[`schemas/v2/finding-row.schema.json`](../schemas/v2/finding-row.schema.json). The optional
`findings-registry.schema.json` envelope is an export form, not runtime input. A row includes:

- `id`, `claim`, scientific `domain`, exact workspace-relative source path and locator, status,
  ledger disposition, coordinator approval, policy classification, and confidence;
- rerunnable evidence with relative source path and SHA-256, command, expected/observed values, and
  tolerance basis; an explicit basis when the impact is `unverifiable`;
- reviewer id, unique invocation id, immutable reviewer-output path/SHA-256, observed model family,
  and outcome for each verification, plus an environment fingerprint and explicit dependent finding
  ids and unique workspace-relative, non-traversing artefact paths;
- settled/report-hash scope, disclosure flag, remedy, conclusion impact (and which conclusion when
  changed), and provenance;
- for a promotable row, benign explanations ruled out, the exact `Section N[.N]`, and a verification
  snippet classified as code, judgement, or unverifiable.

Promotion requires a confirmed/verified/accepted status, coordinator approval, and two independent
confirmations spanning two model families, two invocation ids, and two evidence records.
`make_settled.sh` joins them to completed canonical invocation rows, re-hashes every source and
reviewer output, checks the completed/integrated/passing Round-1 gate, and records
rejected rows in `SETTLED.json`.
Resolve intended promotions and rerun it; never hand-edit either generated output.

Critics in round 2 may take these as established but must spot-check anything their own criticism
depends on. A failed spot-check reopens the fact and is itself BLOCKING.

## Dynamic routing for round 2 and later

Every later round includes completeness plus specialists selected from changes since the previous
snapshot:

| Changed content | Add role |
|---|---|
| Disclosure, privacy, licence, remedy, conclusion wording, impact, attribution, benign explanation, proportionality | fairness |
| Statistic, numerical result, estimator, model, power, interval, methods, inference | stats |
| Sample or participant count, exclusion, missingness, duplicate, raw record, derived outcome, file manifest, repository metadata | data |

Use fairness when only general prose changed. Ordinarily choose the single best-matching specialist.
Add implicated specialists only when conclusion-changing or disclosure-critical changes span
multiple domains. Name changed sections and dependent claim ids in `WRAPUP.txt`, and tell reviewers
how many rounds remain.

Before integrating a completed round, preserve its report and registry. After integration, run
`scripts/make_diff_packet.py` with those explicit pre-integration files, the current report and
registry, and `--round <next-round>`. Pass its output with `DIFF_MANIFEST=<path>`. The helper validates
both JSONL registries, hashes all four inputs, and derives changed ids/sections, dependencies, settled
references, risk signals, recommended roles, and scores. Do not hand-write a competing scope packet.
The driver adds the packet's selected specialist(s), always retains completeness, and copies the full
packet into the immutable critic snapshot. A missing explicitly named manifest is fatal. Parallelism
defaults to the routed role count; set `PARALLEL` only to throttle it. If both report and registry are
unchanged, the helper emits a content-bound `no_changes` packet with empty change arrays and the
conservative fairness-plus-completeness route. Round 2 still requires that generated packet; never
replace it with an unbound hand-written fallback or omit the manifest.

Scope later critics to:

- changed sections and the claims they depend on;
- whether corrections propagated to the abstract, appendix, metadata, and verification;
- global consistency, privacy, checklist, and archive gates;
- unresolved items from earlier rounds.

After integrating each later round, execute
`python3 scripts/run_post_delta_gates.py "$AUDIT_DIR" --round <N>`. The accepted
`critique/round_N/POST_DELTA_GATES.json` binds all four gates, their logs and code, the canonical
report/findings, diff inputs, policy, and intake manifest. A partial, failed, missing, or stale receipt
does not count; finalization replays the latest bound state.

Do not reopen a settled line of inquiry merely because time remains.

## Completion and retry contract

A critic report is complete only when the critic process exits cleanly and its role status is
`complete`. A non-empty file, verdict heading, or early skeleton is insufficient. Quarantine partial
output and record the role as failed until a clean attempt completes.

Each role receives at most three total attempts across its first launch, serial recovery, and resumed
execution. Unsupported models, exhausted credits, and dead credentials are deterministic failures;
fail fast rather than consuming the remaining budget. A partial round must say `M of N critics`, not
silently present itself as a full round.

## Stopping

Two rounds are required. After every integrated round, classify accepted items as factual corrections,
unresolved blocking/conclusion-critical items, or wording only, and record the claim ids changed.

- `target_complete`: at least two rounds completed, every changed claim id received subsequent review,
  and no unresolved BLOCKING or conclusion-critical item remains.
- Continue: any changed claim id still lacks subsequent review, or a BLOCKING/conclusion-critical item
  remains; route the next round from those changes.
- `converged`: after continuing beyond the two-round target, a completed round returns no factual
  correction and no unresolved BLOCKING or conclusion-critical item.
- `cap_with_unresolved`: round 5 completed. List every unresolved item and every section changed after
  its last adversarial review.
- `aborted`: a genuine blocker or failed run. Reaching round 2 alone is not a reason to abort.

Use the `review_scope` event to record `changed_claim_ids`, `rereviewed_claim_ids`,
`unresolved_blocking_item_ids`, `unresolved_conclusion_critical_item_ids`, and `unreviewed_sections`
atomically before finalization. These arrays are evidence for the stopping value, not a retrospective
narrative; copy them from the round records and diff manifests.

Corrections written in response to round N have not been reviewed by round N. That is why round 2 is
mandatory and why a correction in round 2 can require a targeted round 3.

## Propagation check

After each accepted batch, confirm:

- the body states the adjudicated fact and no retired wording survives;
- the Abstract carries the same status and impact;
- exactly one Appendix A row and one `[claim N]` tag represent the finding;
- generated `LEDGER.md`, `comment.json`, and the verification entry agree with `findings.jsonl`;
- no process narration entered the public comment;
- the consistency and disclosure gates pass on the complete draft.
