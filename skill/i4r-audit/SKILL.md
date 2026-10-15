---
name: i4r-audit
description: "Run a full Institute for Replication (I4R) reproducibility audit of a published paper and its replication package, producing a publishable comment, verification notebook, claims handoff, and process record. Use when the user asks to audit, reproduce, or replication-check a paper with an available package. Do not use for peer review of unpublished manuscripts, literature reviews, or papers with no replication materials."
---

# I4R reproducibility audit

Run the complete audit: five independent area reviews, coordinator adjudication, external adversarial
review, a verified I4R comment, and the replicator handoff. The audit reports checks that passed and
hypotheses that were refuted as carefully as defects it confirms.

The machine-readable policy is [`config/pipeline-policy-v2.json`](config/pipeline-policy-v2.json).
It is authoritative for models, agent roles, round routing, retry limits, and stopping rules. Run
`python3 config/check_policy_drift.py` before a production audit; do not repair a disagreement by
editing the generated summary below.

<!-- PIPELINE-POLICY-V2:START -->
- Coordinator: `sonnet-5-5` (`claude-sonnet-5-5`), effort `xhigh`.
- Phase 1: 5 auditors, 5-up; all areas 1,2,3,4,5 use `sonnet-5-5` (`claude-sonnet-5-5`), effort `xhigh`.
- Post-fan-out verifier: `sonnet-5-5` (`claude-sonnet-5-5`), effort `xhigh`, auditor family; one blind call per batch of INCLUDED rows raised after fan-out, under the Phase-1 runtime contract; 0 calls by default.
- Critics: `gpt-6.1-sol`, effort `high`.
- Critique: target 2 rounds, hard cap 7; round 1 = stats, data, fairness, completeness, 4-up.
- Later rounds: completeness plus one best specialist by default (2 critics); add specialists only for conclusion-changing or disclosure-critical changes spanning multiple domains.
- Synthesis: deterministic merge; `gpt-6.1-sol`, effort `max`, adjudicates only policy-triggered conflicts or high-risk evidence gaps.
- Retry budget: 3 total attempts per role by default across parallel, serial, and resumed execution; the operator may raise the ceiling to 20 (`MAX_TRIES` for critics, `I4R_MAX_ATTEMPTS` for Phase 1 and the coordinator).
- Settled facts: generate after round 1 after 2 independent confirmations spanning 2 model families; inject from round 2.
- Stop values: `target_complete` after the required reviewed rounds; `converged` only after continuing; `cap_with_unresolved` at the audit's round cap; `aborted` only for a genuine blocker or failed run.
<!-- PIPELINE-POLICY-V2:END -->

The default audit has **five Claude subagents** and **six external Codex critic sessions**: 11
non-coordinator role invocations, 12 substantive sessions including the coordinator. A cold audit adds a Sonnet/xhigh
Phase-1 probe and a Sol/high critique probe for 14 invocations, seven on the critique path; a
warm probe makes it six. Three attempts per critic role bound the two-round path at 19. A policy-triggered adjudicator (zero by default) adds at
most three attempts per triggered round, bounding it at 22. Cross-domain
conclusion-changing or disclosure-critical corrections can add specialists after round 1; log each.

**Run every step to the final report without pausing.** Intake through publish runs end to end; no
step waits for authorization. Gates still fail closed and grants are still journaled.
Stop only for a gate failure or a real blocker. A folder name holding `?*[]{}\\` needs
`I4R_PAPER_DIR`; a killed Phase 1 needs `run_phase1.py --recover-stranded`; a batch needs
`status.py <tree-root> --stuck`. All three: INSTALL.md.

Record the exact model identifier each provider returns, not the requested alias. A role is not
independent because its display name differs: external critics must differ from the
coordinator/auditor family.

## Operating boundaries

- Complete the pipeline without pausing at phase or round boundaries. Pause only for a missing input
  or a gate that cannot be resolved safely.
- Deposit personal data never pauses, parks or skips an audit: report it as a value-free
  disclosure-flagged finding (identifier kind, file and column, rows affected, verified remedy;
  INSTALL.md), never an identifying value or a way to re-identify anyone.
- Never contact authors or journals directly: the comment filed with I4R is the disclosure route.
- Work in the paper's run directory, `runs/<Journal>/<Paper>/audit`, never in `data/`, a session
  scratchpad or `/tmp`. `data/` holds inputs only and is never written.
- Treat `pkg/` as immutable. Copy code or notebooks into the role's `work/` directory before running
  anything that may write beside its inputs.
- Never reproduce a participant identifier beside a name, date, timestamp, or clock time in any audit
  artefact. Redact prose outputs; do not string-rewrite data used by computations.
- Do not attribute motive or intent. State what the paper, data, and code establish.
- Material intended for replicators must be discipline-agnostic.

## Read references only when their stage needs them

- Before briefing auditors, read [`reference/checklist.md`](reference/checklist.md) and use
  [`reference/brief_template.md`](reference/brief_template.md).
- Read [`reference/phase1_runtime.md`](reference/phase1_runtime.md) only when operating or diagnosing
  the packaged Phase-1 runner.
- Before merging reports or integrating critics, read
  [`reference/adjudication_guide.md`](reference/adjudication_guide.md).
- When classifying findings for `comment.json`, read
  [`reference/SCHEME.md`](reference/SCHEME.md). Keep impact, verification cost, and disclosure
  separate.
- Before drafting or rebuilding the comment, read
  [`reference/report_structure.md`](reference/report_structure.md) and
  [`reference/house_citations.md`](reference/house_citations.md), the single source of the two
  mandatory protocol citations and the published byline.
- Before implementing claim checks, read
  [`reference/verification_guide.md`](reference/verification_guide.md).
- Read [`reference/failure_history.md`](reference/failure_history.md) only when diagnosing a matching
  symptom or changing a gate.
- Read [`reference/benchmarking.md`](reference/benchmarking.md) only when benchmarking the
  workflow.

## 1. Intake and live run metadata

The tree is `data/<Journal>/<Paper>/` (article PDF and replication package, read-only),
`runs/<Journal>/<Paper>/audit` (this audit), `comments/` and `metadata/` (publications). Confirm the
article PDF, package, supplement or preregistration, citation, DOI, and repository URL, then start
the audit from the tree root, by the skill's path:

```bash
bash scripts/new_audit.sh "data/<Journal>/<Paper>"   # derives runs/<Journal>/<Paper>/audit, finds PDF + package
export AUDIT_DIR="<absolute path new_audit.sh prints>"   # '?*[]{}\' are dropped from it
bash scripts/init_run.sh "$AUDIT_DIR" --doi <doi> --paper-title <title> \
  --round-target 2 --round-cap 7 --auditor-model sonnet-5-5 \
  --auditor-family claude --auditor-provider anthropic
export RUN_JSON="$AUDIT_DIR/run.json"
chmod -R a-w "$AUDIT_DIR/pkg"
```

`new_audit.sh` refuses an ambiguous folder (two PDFs, two archives) and names the candidates; pass
`--paper` / `--package` then. It then checks that this host's Codex CLI can use the critic model
(`scripts/check_critic_access.py`, one minimal call) and starts nothing if not: report its reason to
the user and stop; changing the host's Codex account is the user's decision, and no audit can be moved
off the shipped critic model. It calls `setup_workspace.sh`, which reserves the paper and refuses a
competing claim. `init_run.sh` captures the package manifest and environment, and refuses a skill
copy whose `VERSION` differs from the tree's canonical one; resolve any warning before fan-out.

```bash
bash scripts/log_event.sh phase_start phase=intake at=<setup_workspace.sh start>   # all phases: INSTALL.md
bash scripts/log_event.sh model role=<role> role_class=<class> round=<n-or-0> \
  model_id=<served-id> model_family=<family> provider=<provider> source=observed
bash scripts/log_event.sh tokens role=<role> round=<n-or-0> total_tokens=<observed-total>
bash scripts/log_event.sh human_effort notes='<who did what, or: unattended run>'
```

Log phase boundaries, every role/model, observed tokens, substitutions, failures, brief
corrections, round results, gate exits, human effort and delivery as they occur. Use `null` or
`obtainable=false` for unavailable usage; never estimate it. Logged text reaches the coordinator and
run.json, so e-mail addresses, private paths and participant codes are refused; mask an earlier
event's text with `log_event.sh text_redaction seq=<n> field=<field> reason='<why>'`.

Commit several events at once with `bash scripts/log_event.sh --batch <events.jsonl>`. Each nonblank
line is `{"type":...,"at":...,"payload":{...}}`; the driver validates the file, takes one lock,
projects in order and rewrites `run.json` once. One invalid line rejects the batch.

The canonical append-only, hash-chained lifecycle journal is `run.events.jsonl`. `run.json`,
`invocations.jsonl`, and the metrics report are projections carrying the journal sequence/hash
watermark. Missing round authorization produces `awaiting_authorization` without reserving an
attempt or launching a provider. Never rewrite journal history to correct a projection.

For a coordinator attempt not managed by a packaged runner, write one object conforming to
[`schemas/v2/invocation-record.schema.json`](schemas/v2/invocation-record.schema.json), then append it
through the validator:

```bash
python3 scripts/record_invocation.py "$AUDIT_DIR" "$AUDIT_DIR/work/<invocation-record>.json"
```

Use a unique invocation id and record requested and observed identity, session id when exposed,
timestamps, prompt/input/output hashes, actual token fields, provider-call status, and any error. A
provider adapter that exposes no served deployment id must leave it null and use
`identity_assurance=requested_only`; never copy the requested alias into an observed field. A
completed attempt must name a workspace-relative output whose hash matches. The critique driver
records its own critic attempts in the same `invocations.jsonl`; do not append a second copy.

## 2. Phase 1 — five independent auditors

Run the packaged orchestrator; do not create audit-local launch or recovery scripts:

```bash
python3 scripts/run_phase1.py "$AUDIT_DIR"
```

It freezes the raw inputs and deterministic aggregate evidence under one monotonic 60-second
pre-dispatch deadline, runs a provider-free test of the exact sandbox/read/write/promotion paths,
then reuses or makes one bounded capability
probe before dispatching all five roles concurrently. A failed preflight stops before any auditor
inference. Attempts use no network, plugins, or cross-attempt state, have a 90-minute
timeout (45 in older audits), a $15 cap, immutable role packets and raw read-only access.
All five run the snapshot's Claude route: Sonnet 5.5/xhigh; older audits keep the route they froze.

| Area | Role | Report |
|---|---|---|
| 1 | Reproduction | `report/01_reproduction.md` |
| 2 | Data integrity | `report/02_data_integrity.md` |
| 3 | Text-vs-data consistency | `report/03_text_vs_data.md` |
| 4 | Code and methods | `report/04_code_methods.md` |
| 5 | Privacy and archive hygiene | `report/05_privacy_archive.md` |

The runner accepts a role only after clean exit, report/result schema validation, exact source-hash
joins, and evidence-manifest closure; partial output stays diagnostic. The attempt output directory
is a closed contract holding exactly `REPORT.md`, `result.json`, `evidence/manifest.json` and the
listed evidence files. A sandbox permission denial is recorded in the attempt's `sandbox_denials.json`, not
treated as a role failure. Each auditor must write incrementally, verify against raw deposited artefacts, correct errors in the
brief, label findings CONFIRMED / LIKELY / SPECULATIVE, give the impact on published conclusions,
record benign explanations tested, report negative results, and leave rerunnable evidence in its own
work directory. A non-empty skeleton is not completion; the role must return cleanly and the report
must contain its checks, findings, open questions, and evidence inventory.
Each structured finding also binds its complete claim, source-backed evidence/results, impact, and
remedy. A focused output repair may correct structural JSON bindings only: a canonical projection
byte-freezes those fields, all report prose, and every evidence artifact.

## 3. Ledger, adjudication, and first draft

Read all five reports end to end. Encode every finding and tested negative as one row in
`work/findings.jsonl`; set `ledger_disposition` to `INCLUDED`, `MERGED` with a target, or `EXCLUDED`
with a defensible reason. Never synthesize from subagent summary messages alone.

Use the maintained coordinator runner when Claude CLI execution is available. Build its inputs with
`python3 scripts/build_draft_registry.py "$AUDIT_DIR"` (one schema-valid draft row per role finding
in `work/registry_draft/`; set MERGED/EXCLUDED dispositions where roles overlap) and
`python3 scripts/build_safe_evidence.py "$AUDIT_DIR"` (privacy receipts, manifest and checksum
closure under `work/safe_evidence/`). Then run:

Write the four audit-context files now (contents in section 4): `run_coordinator.py` requires
`PAPER.txt`, so writing them here avoids failing an attempt Phase 1 has already paid for.

```bash
python3 scripts/run_coordinator.py "$AUDIT_DIR" \
  --safe-evidence-manifest "$AUDIT_DIR/work/safe_evidence/manifest.json"
```

The runner reads every `.jsonl` and `.md` in `work/registry_draft` by default; repeated
`--draft-registry <path>` arguments override that set. Every `.jsonl` row must be a
complete `finding-row.schema.json` object that also passes `validate_v2.validate_finding_row`;
prose belongs in the `.md` files. It requires the five
canonical reports to match completed Phase-1 invocation records, freezes all inputs into an immutable
attempt, and gives Sonnet read access only to that snapshot (network and Bash disabled). It accepts
a call only with exact model and token telemetry, valid v2 findings, evidence and invocation joins, and
complete `REPORT_v1.md`, `ADJUDICATION.md`, `findings.jsonl` and `retired_claims.txt`. Provider-visible
safe-evidence payloads are capped at 1 MiB and rejected on private host paths or participant codes.
Pre-critique rows stay unapproved, unsettled and `changes_conclusion=false` (two model families are
required to set it; every Phase-1 confirmation is Sonnet). A failed attempt leaves canonical files
unchanged and appends a failed invocation record.

Use targeted adjudication:

1. Mechanically merge non-conflicting report items into the ledger.
2. Recompute disagreements, new material findings, blocking items, and refuted/overstated claims.
3. For a conclusion-changing claim, a coordinator–auditor disagreement, or a criticism you reject,
   verify by a second structurally different route.
4. Spot-check confirmed facts; do not rebuild them unless the current decision depends on them.
5. Preserve withdrawn decisions in `report/ADJUDICATION.md` and immediately propagate each accepted
   decision to the registry and draft.

Draft `report/REPORT_v1.md`, copy the working version to `report/REPORT_final.md` (the maintained
runner performs this copy transactionally), and maintain `report/retired_claims.txt`. Every report
number must be independently derived before publication; do not carry an auditor's count forward on
confidence alone.

Maintain one canonical JSON object per candidate or tested negative in `work/findings.jsonl`, using
[`schemas/v2/finding-row.schema.json`](schemas/v2/finding-row.schema.json). This JSONL file is the
runtime source; `schemas/v2/findings-registry.schema.json` is only an optional export envelope.
Before the first projection create the `comment.json` skeleton it injects into:
`python3 scripts/init_comment.py "$AUDIT_DIR" --facts work/comment_facts.json` (facts shape: `--help`). The projection's `--apply` rewrites
`REPORT_final.md`'s Appendix A and claim tags from approved rows, so it refuses until Round 1
integration approves one. Preview each batch:

```bash
python3 scripts/generate_finding_artifacts.py "$AUDIT_DIR"   # --apply only from section 4 on
```

The generator writes every row to `report/LEDGER.md`, but projects only final-status,
coordinator-approved `INCLUDED` rows into `report/APPENDIX_A.md`, `comment.json` findings,
`claims_map.tsv`, the verification plan, and verification snippets. Never hand-edit these projections.
Test snippets with `scripts/run_snippets.sh` (the notebook's setup).
Use the generated Appendix A block verbatim in `REPORT_final.md`; keep narrative finding sections
synchronized with the registry.

## 4. Adversarial review

Write these before round 1 if section 3 has not:

| File | Required content |
|---|---|
| `PAPER.txt` | Correct citation, package location, and analysis type; mandatory |
| `ENV.txt` | Interpreters, packages, environment paths, and version caveats |
| `SLOW.txt` | Jobs critics should not rerun without a specific reason |
| `WRAPUP.txt` | Round 1: full-report four-role scope, target 2 and the audit's cap, and remaining budget; later rounds: changed sections, dependent claim ids, and remaining budget |

### Round 1

Run stats, data, fairness, and completeness together at Sol/high (older audits keep theirs). The
driver freezes the current draft as `REPORT_TO_REVIEW.md` and gives each critic a compact immutable
role packet: statistics gets quantitative/method claims, data gets provenance/count/exclusion/duplicate
claims, fairness gets privacy/disclosure/archive/attribution/framing claims, and completeness gets
every claim and the whole-report join checks.

Give the findings their final `F<n>` ids first. Critic verdicts join the registry by id, so a
renumbering after a round orphans that round's verdicts for good:

```bash
export AUDIT_DIR="<durable-audit-dir>"
python3 scripts/renumber_findings.py "$AUDIT_DIR" --apply --also report/ADJUDICATION.md   # once, before round 1
CRITICS="stats data fairness completeness" ADJUDICATE=auto \
  bash scripts/run_critique.sh 1
# phase_end intake, the adjudication pair, then (INSTALL.md):
bash scripts/log_event.sh phase_start phase=critique at=<first critic started_at>
```

The command runs unattended: it prepares the immutable plan, journals a hash-bound grant for its
unmodified `ROUND_AUTHORIZATION_TEMPLATE.json` before any provider call, and dispatches; every model,
effort and data class is policy-pinned. Set `I4R_AUTO_AUTHORIZE=0` for
an attended round: the driver then exits 3 with no provider call, and you authorize the exact
template with `lifecycle_journal.py authorize-round` before rerunning the unchanged command.

Never authorize a modified template. If the account cannot use the pinned critic model, tell the user: the Codex
CLI must be signed in to one that can, and since 2.7.5 no audit moves off the shipped critic route. For an
audit on the shipped route, `scripts/check_critic_access.py --force` checks the account; otherwise the
round's own model probe is the check. Never edit the snapshot. If the report, policy, routing, provider, model, effort or data
classes change, prepare and review a new round plan.

The driver merges verdicts by claim and writes `adjudication_packet.json` with trigger ids, reasons,
and source roles. `ADJUDICATE=auto` calls Sol/max only when a trigger has a canonical finding,
evidence-complete critic records, bound source evidence, and one decision-complete question.
Conflicting verdicts, disputed conclusion-changing claims, and failed settled-fact checks can qualify;
an evidence gap is deferred into the next routed critique round rather than sent to adjudication.
With no ready trigger there is no synthesis or adjudicator call. The Sonnet coordinator integrates
the round with this sequence, in this order (each step binds the output of the one before; a step run
out of order is not undone by rerunning it later):

```bash
# 1. Preserve the pre-integration sources.
cp "$AUDIT_DIR/report/REPORT_final.md" "$AUDIT_DIR/critique/round_1/REPORT_BEFORE_INTEGRATION.md"
cp "$AUDIT_DIR/work/findings.jsonl" "$AUDIT_DIR/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl"
# 2. Plan, then apply YOUR decisions (finding id -> fields; --help), never the plan file.
python3 scripts/integrate_round.py "$AUDIT_DIR" --round 1 plan
python3 scripts/integrate_round.py "$AUDIT_DIR" --round 1 apply --decisions "$AUDIT_DIR/work/round1_decisions.json"
# 3. Edit the prose and retired_claims.txt, then approve, project, approve, project. Withdrew a finding? INSTALL.md, Renumbering.
python3 scripts/approve_settled.py "$AUDIT_DIR"; python3 scripts/generate_finding_artifacts.py "$AUDIT_DIR" --apply
python3 scripts/approve_settled.py "$AUDIT_DIR"; python3 scripts/generate_finding_artifacts.py "$AUDIT_DIR" --apply
# 4. Consistency; capture its exit with $?, never PIPESTATUS.
bash scripts/check_consistency.sh "$AUDIT_DIR/report/REPORT_final.md" "$AUDIT_DIR/report/retired_claims.txt" \
  > "$AUDIT_DIR/work/consistency_round1.log" 2>&1; RC=$?
bash scripts/log_event.sh gate gate=check_consistency round=1 exit_status=$RC
bash scripts/log_event.sh round_result round=1 blocking_items=<n> factual_corrections=<n> character=<substantive|framing|precision|wording_only|none>
# 5. Settled cache plus its settled event, every round (generated_after_round stays 1), then the packet.
bash scripts/make_settled.sh "$AUDIT_DIR" "$AUDIT_DIR/work/findings.jsonl"
bash scripts/log_event.sh settled used=true generated_after_round=1 path=SETTLED.md entries=<n> spot_check_failures=0
python3 scripts/make_diff_packet.py "$AUDIT_DIR" --round 2 \
  --before-report "$AUDIT_DIR/critique/round_1/REPORT_BEFORE_INTEGRATION.md" \
  --after-report "$AUDIT_DIR/report/REPORT_final.md" \
  --before-findings "$AUDIT_DIR/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl" \
  --after-findings "$AUDIT_DIR/work/findings.jsonl" \
  --output "$AUDIT_DIR/critique/DIFF_MANIFEST_round_2.json"
# 6. WRAPUP.txt, prepare round 2 (creates its folder, exits 3), gates, then run it.
DIFF_MANIFEST="$AUDIT_DIR/critique/DIFF_MANIFEST_round_2.json" I4R_AUTO_AUTHORIZE=0 bash scripts/run_critique.sh 2
python3 scripts/run_post_delta_gates.py "$AUDIT_DIR" --round 2 --report "$AUDIT_DIR/report/REPORT_final.md"
DIFF_MANIFEST="$AUDIT_DIR/critique/DIFF_MANIFEST_round_2.json" ADJUDICATE=auto bash scripts/run_critique.sh 2
```

If both sources are byte-identical the helper emits a content-bound `no_changes` packet with the
conservative fairness-plus-completeness route. Round 2 still consumes that generated manifest;
never replace it with an unbound hand-written fallback.

Every JSONL row follows `schemas/v2/finding-row.schema.json`; `reference/adjudication_guide.md`
lists its fields, including what a promotable row must also record. A row is promoted only when its status is confirmed,
verified, or accepted, the coordinator approves it, and two independent reviewers from two
model families confirm it. `make_settled.sh`
joins those confirmations to `invocations.jsonl`, verifies reviewer outputs and source hashes,
requires a completed/integrated/passing Round 1, and generates
`SETTLED.json`/`SETTLED.md`; never hand-edit them. Resolve any intended promotion listed as
rejected, rerun `make_settled.sh`, then log
`settled used=true generated_after_round=1`. `approve_settled.py` must run first or the cache
promotes zero facts silently; INSTALL.md gives the order.

The Sonnet-family confirmation comes from the Phase-1 auditor that raised or found the row. A row
first raised after fan-out (operator, coordinator, a critic's new issue) gets one only from
`python3 scripts/run_verifier.py "$AUDIT_DIR" --findings <ids>`, run after a round reaches quorum and
before integrating it (or before round 1): one blind verdict per claim text, bound to its hash;
REFUTED or UNVERIFIABLE keeps blocking it. Never hand-make or relabel verifier records; if a run dies
before its receipt, re-run the same command, never correct that row to `legacy_record`. Details: INSTALL.md.

### Round 2 and later

Generate `WRAPUP.txt` naming changed sections, their dependent claim ids, and the remaining round
budget. Use `make_diff_packet.py` as above to derive the JSON `DIFF_MANIFEST` from explicit before and
after reports and schema-valid registries. It content-binds all four inputs and emits
`changed_claim_ids`, `changed_sections`, dependencies, settled references, risk signals,
`recommended_roles`, and `role_scores`; do not replace it with a hand-written scope summary. The
driver preserves the full packet in the critic snapshot.

The driver always adds completeness. Route specialists from the changes:

- fairness for disclosure, licence/remedy, impact, attribution, benign-explanation, or
  proportionality changes;
- stats for quantitative, estimator, model, power, interval, methods, or inference changes;
- data for sample, exclusion, missingness, duplicate, raw-record, outcome, manifest, or repository
  changes;
- fairness as fallback when only general prose changed.

The default is completeness plus one best-matching specialist. Add implicated specialists only when
multiple domains contain conclusion-changing or disclosure-critical changes. Inject `SETTLED.md`;
scope work to changed sections, their dependencies, and global consistency/privacy checks.
Parallelism defaults to the actual routed role count, so set `PARALLEL` only to throttle it.

```bash
DIFF_MANIFEST="$AUDIT_DIR/critique/DIFF_MANIFEST_round_2.json" ADJUDICATE=auto \
  bash scripts/run_critique.sh 2
```

As in round 1, this runs to completion unattended; `I4R_AUTO_AUTHORIZE=0` restores the pause.

An explicitly named missing manifest is fatal. A `CRITICS` override may be used for recovery, but the
driver still retains completeness. The driver again emits `adjudication_packet.json`; the Sonnet
coordinator resolves only its triggers.

Integrate every later round with the same six steps (round N's preserved sources, packet N+1,
prepare N+1, gates N+1), re-logging the settled event with the new `entries` each time. A round that returns
no factual correction and ends the audit is NOT integrated: record its verdicts in
`critique/round_N/RESPONSE_notes.md`, `round_result` and `review_scope` events, and leave the
report and registry untouched (target_complete may integrate it only to bind a deferred approval or record
contests, then rebuild: INSTALL.md); otherwise its post-delta receipt and packet go stale.
Still run step 4's consistency check and log its round-N `gate` event; finalize requires one per round.

Run round N's four whole-document gates after its packet and prepare call, before it runs: the receipt binds the round-N-entry state.

```bash
python3 scripts/run_post_delta_gates.py "$AUDIT_DIR" --round 2 \
  --report "$AUDIT_DIR/report/REPORT_final.md"
```

The command runs consistency, release-bound privacy, canonical checklist/projection, and intake-
archive checks in parallel. It accepts only a passing content-bound
`critique/round_N/POST_DELTA_GATES.json`; failures are written separately as partial receipts.
Finalization replays every receipt and reruns all four gates against the latest report.

The retry limit is **three inference starts per role**, including initial execution, focused repair,
transport retry, and later resume. Preflight, cache replay, and rejected schema admission do not
consume it. Retry only transport, throttling, and provider 5xx errors; authentication,
authorization, quota, model, capability, and deterministic schema failures stop immediately. A
compiled schema digest may receive at most two rejected admission requests for one role and round;
the journal blocks a third request before provider dispatch, including after resume. A
scientifically valid response that fails only administration may receive one repair no larger than
20% of its original packet; every scientific field must remain byte-equivalent. A partial file does
not reset the budget or prove completion.

When a round is backgrounded, wait on the process and its machine-readable status, with a deadline:

```bash
DIFF_MANIFEST="$AUDIT_DIR/critique/DIFF_MANIFEST_round_2.json" ADJUDICATE=auto \
  bash scripts/run_critique.sh 2 > "$AUDIT_DIR/work/round2.log" 2>&1 &
bash scripts/wait_for_round.sh "$AUDIT_DIR" 2 --pid $!
```

After the two required rounds, use `target_complete` only when every changed claim id received a
subsequent adversarial review, no unresolved BLOCKING or conclusion-critical item remains, and every
last-round OVERSTATED/REFUTED verdict is contested in `CONTESTED.json`. Otherwise
integrate the result and run another routed round. Use `converged` only when a round after the target
returns no uncontested correction or unresolved BLOCKING or conclusion-critical item (contests: `CONTESTED.json`, INSTALL.md). At the
audit's cap (`critique.round_cap` in its snapshot: 7; older audits keep 5 until `amend_round_cap.py`
raises it, INSTALL.md) use `cap_with_unresolved`, listing every unresolved item and changed section (none unresolved: `converged`).
Use `aborted` only for a genuine blocker or failed run. Both need
`finalize_run.sh --stopping-detail "<one-line reason>"`; finalize refuses them without it.

Every `round_result` event records `factual_corrections=<integer>`. A `converged` stop requires a
round beyond the target whose value is exactly zero (not null, not a `character` label).

Before finalization, record the actual stopping evidence atomically. For example, when only F2 changed
and was subsequently reviewed:

```bash
bash scripts/log_event.sh review_scope changed_claim_ids:='["F2"]' \
  rereviewed_claim_ids:='["F2"]' unresolved_blocking_item_ids:='[]' \
  unresolved_conclusion_critical_item_ids:='[]' unreviewed_sections:='[]'
```

`changed_claim_ids` is the ordered union of every later round's packet ids (withdrawn ones included),
`rereviewed_claim_ids` the same set; `target_complete` and `converged` need the three other lists
empty; both ship the reviewed report (target_complete: projection edits aside).

## 5. Deliverable, verification, and filing

Follow `reference/report_structure.md`. The comment is a paper: an Abstract of at most 250 words,
numbered sections beginning with Introduction and ending with Conclusion, references, and Appendix A.
There is no executive summary, no findings table in the body, and no Recommendations section. The
body cap is 10 rendered pages; references and appendices begin on new pages and do not count. Draft
toward nine body pages before Round 2 so one page remains for adversarial corrections.

Before release:

1. If the report or registry changed after the last `approve_settled.py`, run approve, project,
   approve, project, then `make_settled.sh` (INSTALL.md, "Two more orderings"). The projection
   replaces Appendix A and writes one ordered body
   cross-reference `[claim N]` entry per included finding, exactly matching `comment.json`; do not
   hand-key duplicate inline tags.
2. Run `check_consistency.sh` and resolve every failure.
3. Build the DOCX/PDF (it needs `report/front_matter.md` and `report/captions.txt`; see
   `reference/report_structure.md`) and inspect rendered pages, especially page 1 and tables.
4. Complete the non-finding fields of `comment.json` against `reference/SCHEME.md`; finding rows come
   from the projection helper. Validate incrementally.
5. Generate the R verification notebook, replace every stub, and run the required wrapper. It renders
   the notebook, requires its status to pass, and writes a content-bound `verification_manifest.json`.
   Judgement and unverifiable claims must say why; they are not silent omissions. There is no
   `ALLOW_NO_VERIFICATION` waiver.
6. Build the claims script from executable snippets or verbatim author-code ranges. Claims verified
   by inspection remain in the explicit no-code tail.
7. Stage into a clean release directory and run its disclosure/exclusion gates. Staging requires the
   verification notebook, status, summary, and manifest.
8. Finalize the metadata pair, then publish the bound artefact set plus the release manifest.

```bash
# registry/appendix changed since the last packet, or final verdicts disputed a settled fact? approve+project
# twice, make_settled.sh, re-log settled, rebuild the last packet and gates (INSTALL.md order)
python3 scripts/generate_finding_artifacts.py "$AUDIT_DIR" --apply
bash scripts/log_event.sh phase_end phase=critique at=<final-round end>; bash scripts/log_event.sh phase_start phase=release
bash scripts/build_docx.sh "$AUDIT_DIR" <output-name>
bash scripts/make_verification_notebook.sh "$AUDIT_DIR"
bash scripts/run_verification.sh "$AUDIT_DIR"
bash scripts/make_claims_script.sh "$AUDIT_DIR"
cp "$AUDIT_DIR/report/REPORT_final.md" "$AUDIT_DIR/report/REPORT_v<N+1>.md"  # staging needs the snapshot
bash scripts/stage_release.sh "$AUDIT_DIR" <clean-release-dir>   # a directory that does not exist yet
bash scripts/log_event.sh delivery artifact_path=report/<output-name>.pdf recipient='I4R editorial queue'
bash scripts/log_event.sh phase_end phase=release
export AUDIT_STOP_REASON="target_complete"
bash scripts/finalize_run.sh "$AUDIT_DIR" --stopping-reason "$AUDIT_STOP_REASON" \
  --registry "<tree-root>/registry"   # new tree: I4R_CREATE_REGISTRY=1
bash scripts/publish_completed.sh "$AUDIT_DIR"
```

`build_docx.sh --refresh-cache` rebuilds and replaces the validated cache entry; `--no-cache`
bypasses the cache (INSTALL.md).

Set `AUDIT_STOP_REASON` from the four-value rule above; never call the required two-round finish
convergence. `publish_completed.sh` files the PDF and the manifest-bound DOCX into
`comments/<Journal>/<Paper>/`, and the claims script, the metadata pair, the finalization receipt,
the document manifest and `release-manifest.json` into `metadata/<Journal>/<Paper>/` (eight files).

The finished audit keeps what INSTALL.md lists under *What each audit retains*. The reservation
is released once the bundle is filed.
