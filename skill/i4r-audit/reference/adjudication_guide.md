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
| coordinator, at draft time | … | draft | INCLUDED §3; verifier before round 1 | `work/coordinator/check_table2.py` |
| operator note | … | draft | INCLUDED §6; verifier before round 1 | `work/operator/figure_pages.py` |
| critic new issue, round 1 | … | draft | INCLUDED §5; verifier before round 2 is integrated | `work/r1_new_issue/recount.py` |

`MERGED` requires its target and `EXCLUDED` requires a reason that would survive external review. “No
numerical impact” is not enough; most valid reproducibility findings do not change a conclusion.

The source decides where the row's Claude-family (family-A) confirmation comes from. A row that a
Phase-1 auditor raised or independently found (its role is in `provenance.origin_role` or
`provenance.independently_found_by`) is confirmed by that auditor's own record. A row raised after
fan-out — by the coordinator, the operator, or a critic's new issue — can be confirmed only by the
post-fan-out verifier (`scripts/run_verifier.py`; see *Family-A confirmation for findings raised
after fan-out* below). Never write that confirmation by hand or relabel another call as an auditor:
both are refused, as they were in audit A23, where three true findings (F13–F15) were
excluded after hand-made records failed one gate after another. Record the true origin in
`provenance`; `round_introduced` does not decide eligibility, because operator rows written at
coordinator time carry 0.

Preview with `python3 scripts/generate_finding_artifacts.py "$AUDIT_DIR"` after every schema-valid batch;
add `--apply` only from Round 1 integration on (before that it refuses).
It projects every registry row into `report/LEDGER.md`; only final-status, coordinator-approved
`INCLUDED` rows enter the public Appendix, comment findings, claims map, and verification plan. Edit
the registry, never the generated projections.

Mechanically merge items that agree. Adjudication effort is reserved for:

- disagreements between auditors or between a critic and the draft;
- a new material finding (once it is `INCLUDED`, it also needs the post-fan-out verifier before it
  can be promoted);
- a REFUTED, OVERSTATED, or materially UNVERIFIABLE claim;
- any BLOCKING item;
- any proposed change to a published conclusion.

## Disclosure findings are reported, not withheld

Personal data in the deposit is reported in the comment and never stops the audit. The rule protects
a value-free disclosure finding (identifier kind, file, column and counts) that is factually
correct. A critic item, BLOCKING or not, that asks for such a finding to be removed, deferred,
withheld, coarsened out of the comment or moved to a private channel is rejected: enter it in
`critique/round_N/CONTESTED.json` under its `critic_<role>:<item_id>` key as `{"position": <why it
is rejected>, "evidence": <what shows it>}` and pass that file to `integrate_round.py apply
--contested`. Integration writes the position and evidence to `critique/round_N/RESPONSE_notes.md`
and marks the claim `Decision: CONTESTED (accepted in part)` in `report/ADJUDICATION.md`; a
contested new issue appears in the response notes only, and an entry without a non-empty position
and evidence is refused before anything is written. Only a reproduced identifying value, a new linkage such as a
participant code beside a date, or a described re-identification procedure is corrected: remove
that value, linkage or procedure and keep the finding.

A disclosure finding the deposit does not support is REFUTED like any other claim, and a critic may
mark it BLOCKING. It leaves the comment on a structured basis, never a free-text one: the decision
carries `disclosure_exclusion_basis`, one of `refuted`, `duplicate_of:<finding id>`,
`not_a_disclosure_defect` or `tested_negative`. The same key is required whenever a decision
EXCLUDEs a disclosure-flagged row, clears its `disclosure_flag` (also give a
`disclosure_flag_reason`, which is recorded in `report/ADJUDICATION.md`), or sets its status
rejected or superseded or its `coordinator_approved` false:

```json
{"F9": {"status": "rejected", "coordinator_approved": false,
        "ledger_disposition": {"status": "EXCLUDED", "target": null,
                               "reason": "the column holds session dates, not birth dates"},
        "disclosure_exclusion_basis": "refuted"},
 "F4": {"disclosure_flag": false, "disclosure_exclusion_basis": "not_a_disclosure_defect",
        "disclosure_flag_reason": "a statistics finding the draft flagged by mistake"}}
```

A row withdrawn on a basis ends EXCLUDED, and integration writes its ledger reason opening with the
basis label ("Refuted: the column holds ..."), which is how finalization reads the basis back. A
MERGE, and a `duplicate_of` basis, must name a row that itself ends final: approved, status
confirmed, verified or accepted, and INCLUDED. To keep a disclosure row under review, leave status
and approval out of its decision; withdrawing the approval of a row that stays INCLUDED and flagged
also keeps it under review. For an audit initialised at 2.6.9 or later,
`scripts/integrate_round.py` refuses any other decision, and `finalize_run.sh` refuses a
disclosure-flagged row that is not final, MERGED into a final row, or EXCLUDED on an allowlisted
basis; an older audit (the live 2.6.8 included) gets the same messages as warnings. At
`cap_with_unresolved` or `aborted` a flagged row still awaiting approval is accepted when the stop
lists it as unresolved, by its finding id (`F7`) or by the id of a critic item that assessed it
(`critic_stats:S3`), and the comment's `disclosure.flags` and `disclosure.statement` report it.

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

The post-fan-out verifier is not that second route. It is an independent family-A reviewer that
tests the claim text blind; the coordinator's own obligation to recompute a disputed or
conclusion-changing decision by a structurally different route is unchanged.

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
5. Record every outcome in `critique/round_1/RESPONSE_notes.md` — for every claim id that received
   a verdict, tested negatives included, and for every conflict a critic declared. Use
   `scripts/integrate_round.py "$AUDIT_DIR" --round 1 plan`, then `apply --decisions "$AUDIT_DIR/work/round1_decisions.json"`
   (your decisions keyed by finding id, never the plan file):
   it derives the registry updates, the response notes and the `report/ADJUDICATION.md` round block
   from one `INTEGRATION_PLAN.json`, so the three records cannot disagree.

How a critic verdict becomes a registry verification outcome, so the trail reads the same way twice:

| Verdict | Recorded outcome | Why |
|---|---|---|
| CONFIRMED | `confirmed` | the claim as written was checked and held |
| OVERSTATED | `confirmed`, only when the claim text is reworded at this integration (an `applied_note` alone binds nothing; a later round's literal CONFIRMED can be rebound) | the substance held; the wording did not. Record `verdict_as_issued` in the response notes so the relabelling is visible |
| REFUTED | `refuted`, and the claim is corrected or retired | the claim as written did not hold |
| UNVERIFIABLE | `inconclusive`, except on a row whose own claim is that the figure cannot be checked from the deposit, where an independent failure to check it is `confirmed` | the row's claim and the verdict say the same thing |

A settled fact must rest on records whose verdicts confirm the claim text as published. Where a
later round issues a literal CONFIRMED verdict on a corrected claim, bind that record in place of
the earlier one (`"rebind_literal_confirmations": true` in that round's decision; ignored when the same
decision rewords the claim). A reviewer with no record yet is bound automatically, in every round, the
first time it confirms the claim (a CONFIRMED, or an OVERSTATED on a claim reworded at that integration).
`make_settled.sh` holds a claim reworded at an integration until a later round confirms the new
wording. The two-family rule still counts the records of the earlier wording, since only the critics'
family reviews after Phase 1; such a fact is marked `reworded_at_round` in `SETTLED.json`, and
`SETTLED.md` names the families that confirmed the published wording.

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
- reviewer id, unique invocation id, immutable reviewer-output path/SHA-256, canonical model family,
  honest observed/requested-only identity assurance, and outcome for each verification, plus an
  environment fingerprint and explicit dependent finding
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
Resolve intended promotions and rerun it; never hand-edit either generated output. For a row raised
after fan-out the family-A confirmation is the verifier's record, and `make_settled.sh` admits it
only as the next section describes.

## Family-A confirmation for findings raised after fan-out

Run the packaged verifier, `python3 scripts/run_verifier.py "$AUDIT_DIR" --findings <finding ids>`, on every
`INCLUDED` row that no Phase-1 auditor raised or independently found (the Phase-1 roles are the
audit's own `pipeline-policy.snapshot.json` `phase_1.required_roles`). It is one Sonnet/max call on
the auditors' family per batch, under the Phase-1 runtime contract, and it records its own
invocation rows (`role_class` `verifier`, round 0, role `verifier_<12 hex>`).

- **Blind.** Each hypothesis carries only an opaque id (H1…Hn), the claim verbatim, its SHA-256,
  the source location, and the `paper/` and `pkg/` source hashes that match the Phase-1 snapshot.
  Critic verdicts, the coordinator's reasoning, this log, settled facts, finding ids and every other
  registry field are withheld.
- **Bound to the claim text.** A verdict binds to a row by the SHA-256 of the row's current `claim`,
  not by finding id, so renumbering does not break it and editing the claim voids it.
- **One verdict per claim text.** The runner never re-sends a claim text that already has an
  admitted verdict, and a REFUTED or UNVERIFIABLE verdict on a claim text blocks every verifier
  confirmation of that same text. It keeps blocking after its call is corrected to `legacy_record`,
  as long as its record still re-admits: the correction tool cannot erase a non-confirming verdict.
  A corrected claim is a new text: it may be verified once, and the correction is routed to the
  next critique round like any other change.
- **A claim edit after a REFUTED or UNVERIFIABLE verdict must change substance.** The binding key
  is the exact claim bytes, so the tools cannot tell a real correction from a cosmetic one, and a
  new text gets a fresh verdict. After a non-confirming verifier verdict, edit the claim only to
  change what it asserts (the number, the scope, the comparison, the source it rests on), never
  only its wording or punctuation; if the finding does not survive, retire it instead. Make the
  edit through the round's `integrate_round.py apply` decisions (`"claim"`), never by hand in
  `work/findings.jsonl`, so that it is recorded in that round's `RESPONSE_notes.md` (the verifier
  record dropped because the claim text changed) and in `report/ADJUDICATION.md` (the revised
  text). Where a critic requested the correction, say in the decision's `applied_note` what
  substance changed.
- **Timing.** A verifier call binds only if it ended no later than the integration that binds it. Run it
  after a round reaches quorum and before integrating that round, or before round 1 for coordinator
  and operator rows written at draft time. `integrate_round.py apply` then records the verification
  (reviewer id = the verifier's role, its invocation id, the promoted `result.json` and hash, the
  auditor family, and the outcome below); `make_settled.sh` re-checks the call, its completion
  receipt, the claim hash and the timing.
- **When a record stops counting.** `apply` drops a verifier record already on a row, and gives the
  reason in `RESPONSE_notes.md` and `ADJUDICATION.md`, when its call was corrected to
  `legacy_record`, when a Phase-1 auditor raised or independently found the row, or when the claim
  was edited. `make_settled.sh` rejects a row, with a `verifier:` reason, whenever a confirming verifier
  record on it does not count, including when any verifier record in the ledger is inadmissible; the
  other rows still settle. Never hand-write or relabel a verifier record: every gate re-admits a record
  exactly as the runner admitted it, and the journal command lines refuse verifier events.

How a verifier verdict becomes a registry verification outcome. `scripts/verifier_contract.py`
`VERDICT_OUTCOME` holds the same table, and `config/check_policy_drift.py` fails if they differ:

<!-- VERIFIER-VERDICT-OUTCOME:START -->
| Verdict | Recorded outcome | Why |
|---|---|---|
| CONFIRMED | `confirmed` | the claim as written was checked against the deposit and held |
| REFUTED | `refuted` | the claim as written did not hold; recompute it, then correct or retire the claim |
| UNVERIFIABLE | `inconclusive` | the verifier could not check the claim from the deposit; it never counts as a confirmation, even on a row whose claim is that the figure cannot be checked |
<!-- VERIFIER-VERDICT-OUTCOME:END -->

There is no OVERSTATED verdict: the verifier tests the claim as written and proposes no wording.

### Findings the coordinator raises

The coordinator is not a reviewer: binding the coordinator invocation to its own finding is refused.
Critics are one model family, so a finding no Phase-1 auditor raised or independently found cannot
reach two families from critic verdicts alone, and `integrate_round.py apply` stops with
"confirmations must span two known model families". Its Sonnet-family confirmation comes only from the
post-fan-out verifier: `python3 scripts/run_verifier.py "$AUDIT_DIR" --findings <ids>`, run before the
integration that binds it (INSTALL.md, *Confirming findings raised after fan-out*). Never relabel the
row as an auditor's (adding an auditor to `provenance.independently_found_by` that did not find it), and
never write a verification record by hand: both are refused, and a row that an auditor genuinely found
is confirmed by that auditor, not the verifier. If the verifier returns REFUTED or UNVERIFIABLE, the
finding stays unapproved: keep it out of the numbered findings.

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

Each role receives three total attempts by default across its first launch, serial recovery, and resumed
execution; the operator may raise the ceiling to 20 with `MAX_TRIES` when attempts are being spent on
harness or environment problems. Unsupported models, exhausted credits, and dead credentials are deterministic failures;
fail fast rather than consuming the remaining budget. A partial round must say `M of N critics`, not
silently present itself as a full round.

The verifier has the same three-start budget, set with `I4R_MAX_ATTEMPTS` as for Phase 1. Its role
name is derived from the batch's claim hashes, so rerunning the same batch shares that one budget
rather than opening a new one. A rejected sample is not a verdict; recomposing a batch opens a new
budget for every claim in it, so put every eligible row in one batch. Two pre-inference requests
(provider rejections before inference or recovered strands) refuse a batch for good, whatever
`I4R_MAX_ATTEMPTS` says.

## Stopping

Two rounds are required. After every integrated round, classify accepted items as factual corrections,
unresolved blocking/conclusion-critical items, or wording only, and record the claim ids changed.

- `target_complete`: at least two rounds completed, every changed claim id received subsequent review,
  and no unresolved BLOCKING or conclusion-critical item remains. The report must be the one the last round
  reviewed apart from the generated Appendix A, claim tags and marker ids (finalize refuses other prose edits),
  and finalize refuses while the last round, integrated or not, has an uncontested OVERSTATED/REFUTED verdict or
  BLOCKING item (an `applied_note` cannot answer it, since the report may no longer change).
- Continue: any changed claim id still lacks subsequent review, or a BLOCKING/conclusion-critical item
  remains; route the next round from those changes.
- `converged`: after continuing beyond the two-round target, a completed round returns no factual
  correction and no unresolved BLOCKING or conclusion-critical item. A correction the coordinator rejects
  is contested in `critique/round_N/CONTESTED.json` (`{"critic_<role>:<item_id>": {"position", "evidence"}}`);
  finalize refuses `converged` while any OVERSTATED/REFUTED verdict, requested correction, BLOCKING item or
  BLOCKING new issue is uncontested. The targeted adjudication's own items are keyed `critic_synth:<item_id>`, and its
  CONFIRMED, NON_BLOCKING ruling on a finding (with no correction requested) resolves the critics' conflicting items there (integrate_round treats it
  the same way).
- `cap_with_unresolved`: the audit's round cap completed (`critique.round_cap` in its policy snapshot: 7
  from 2.6.9; an older audit keeps 5 until `amend_round_cap.py` raises it, INSTALL.md). List every
  unresolved item and every section changed after its last adversarial review.
- `aborted`: a genuine blocker or failed run. Reaching round 2 alone is not a reason to abort.

Use the `review_scope` event to record `changed_claim_ids`, `rereviewed_claim_ids`,
`unresolved_blocking_item_ids`, `unresolved_conclusion_critical_item_ids`, and `unreviewed_sections`
atomically before finalization. These arrays are evidence for the stopping value, not a retrospective
narrative; copy them from the round records and diff manifests. An item id may be written as the
critic item (`critic_stats:S3`) or as the finding it concerns (`F7`); a disclosure row whose approval
is deferred is matched either way.

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
