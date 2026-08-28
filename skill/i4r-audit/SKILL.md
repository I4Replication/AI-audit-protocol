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
- Coordinator: `sonnet-5` (`sonnet`), effort `max`.
- Phase 1: 5 auditors, 5-up; all areas 1,2,3,4,5 use `sonnet-5` (`sonnet`), effort `max`.
- Critics: `gpt-5.6-terra`, effort `high`.
- Critique: target 2 rounds, hard cap 5; round 1 = stats, data, fairness, completeness, 4-up.
- Later rounds: completeness plus one best specialist by default (2 critics); add specialists only for conclusion-changing or disclosure-critical changes spanning multiple domains.
- Synthesis: deterministic merge; `gpt-5.6-terra`, effort `max`, adjudicates only policy-triggered conflicts or high-risk evidence gaps.
- Retry budget: at most 3 total attempts per role across parallel, serial, and resumed execution.
- Settled facts: generate after round 1 after 2 independent confirmations spanning 2 model families; inject from round 2.
- Stop values: `target_complete` after the required reviewed rounds; `converged` only after continuing; `cap_with_unresolved` at round 5; `aborted` only for a genuine blocker or failed run.
<!-- PIPELINE-POLICY-V2:END -->

The default audit therefore has **five true Claude subagents** and **six substantive external Codex
critic sessions**, or **11 non-coordinator role invocations**. The coordinator is one additional
model actor, for 12 substantive sessions. A cold audit adds one cached Terra/high capability probe: 13
total provider invocations, of which seven are on the critique path (four Round-1 critics, two
Round-2 critics, and the probe); a warm probe makes that path six. Three attempts per critic role
bound the ordinary two-round critique path at 19 provider invocations. Model probes are health
checks, not auditors. A policy-triggered adjudicator is zero by default; in one triggered round it
adds one first-try Terra call and at most three attempts, making that critique bound 22. Cross-domain conclusion-changing or disclosure-critical corrections can add
specialists after round 1, and every extra or substituted role must be logged.

Record the exact model identifier returned by each provider, not only the requested alias. A role is
not independent merely because it has a different display name: external critics must differ from
the coordinator/auditor model family.

## Operating boundaries

- Complete the pipeline without pausing at phase or round boundaries. Pause only for a missing input,
  a responsible-disclosure decision, or a gate that cannot be resolved safely.
- Never contact authors or journals directly. Route an immediate disclosure issue through I4R.
- Work in a durable audit directory beside the paper, never a session scratchpad or `/tmp`.
- Treat `pkg/` as immutable. Copy code or notebooks into the role's `work/` directory before running
  anything that may write beside its inputs.
- Never reproduce a participant identifier beside a name, date, timestamp, or clock time in any audit
  artefact. Redact prose outputs; do not string-rewrite data used by computations.
- Do not attribute motive or intent. State what the paper, data, and code establish.
- Material intended for replicators must be discipline-agnostic.

## Read references only when their stage needs them

- Before briefing auditors, read [`reference/checklist.md`](reference/checklist.md) and use
  [`reference/brief_template.md`](reference/brief_template.md).
- Before merging reports or integrating critics, read
  [`reference/adjudication_guide.md`](reference/adjudication_guide.md).
- When classifying findings for `comment.json`, read
  [`reference/SCHEME.md`](reference/SCHEME.md). Keep impact, verification cost, and disclosure
  separate.
- Before drafting or rebuilding the comment, read
  [`reference/report_structure.md`](reference/report_structure.md).
- Before implementing claim checks, read
  [`reference/verification_guide.md`](reference/verification_guide.md).
- Read [`reference/failure_history.md`](reference/failure_history.md) only when diagnosing a matching
  symptom or changing a gate. It is history, not an alternative policy.
- Read [`reference/benchmarking.md`](reference/benchmarking.md) only when collecting or comparing
  baseline and optimized workflow measurements.

## 1. Intake and live run metadata

Confirm the article PDF, complete package, supplement or preregistration when applicable, citation,
DOI, and repository URL. Then initialize a durable workspace:

```bash
export AUDIT_DIR="<durable-audit-dir>"
bash scripts/setup_workspace.sh "$AUDIT_DIR" <paper.pdf> <package.zip-or-dir>
bash scripts/init_run.sh "$AUDIT_DIR" --doi <doi> --paper-title <title> \
  --round-target 2 --round-cap 5 --auditor-model sonnet-5 \
  --auditor-family claude --auditor-provider anthropic
export RUN_JSON="$AUDIT_DIR/run.json"
chmod -R a-w "$AUDIT_DIR/pkg"
```

`setup_workspace.sh` automatically reserves the paper in the shared tree and refuses a competing
claim. `init_run.sh` captures the package manifest and environment. If environment capture or paper
text extraction warns, resolve it before fan-out.

Use the scripts at their actual paths under `scripts/`:

```bash
bash scripts/log_event.sh phase_start phase=fan_out
bash scripts/log_event.sh model role=<role> role_class=<class> round=<n-or-0> \
  model_id=<served-id> model_family=<family> provider=<provider> source=observed
bash scripts/log_event.sh tokens role=<role> round=<n-or-0> total_tokens=<observed-total>
```

Log phase boundaries, every role/model, observed token use, substitutions, failures, brief
corrections, round results, gate exits, human effort, and delivery when they occur. Use `null` or
`obtainable=false` for unavailable usage; never estimate it.

When several events are already available together, commit them with
`bash scripts/log_event.sh --batch <events.jsonl>`. Each nonblank line is
`{"type":"<event-type>","at":"<optional-UTC-time>","payload":{...}}`; the driver validates the
complete JSONL file, takes one lock, projects the events in line order, and rewrites `run.json` once.
An invalid or unprojectable line rejects the whole batch. Single-event calls remain valid.

For every coordinator and phase-1 model attempt, write one object conforming to
[`schemas/v2/invocation-record.schema.json`](schemas/v2/invocation-record.schema.json), then append it
through the validator:

```bash
python3 scripts/record_invocation.py "$AUDIT_DIR" "$AUDIT_DIR/work/<invocation-record>.json"
```

Use a unique invocation id and record requested and observed identity, session id when exposed,
timestamps, prompt/input/output hashes, actual token fields, provider-call status, and any error. A
completed attempt must name a workspace-relative output whose hash matches. The critique driver
records its own critic attempts in the same `invocations.jsonl`; do not append a second copy.

## 2. Phase 1 — five independent auditors

Create isolated role workspaces and fill `BRIEF.md` from the template:

```bash
mkdir -p "$AUDIT_DIR"/work/{a1_reproduction,a2_data_integrity,a3_text_vs_data,a4_code_methods,a5_privacy_archive}
```

Spawn all five auditors in one fan-out so they run concurrently. Give each the shared brief, its
checklist routing, its model/effort from policy, its private work directory, and its exact report
path.

| Area | Role | Model | Report |
|---|---|---|---|
| 1 | Reproduction | Sonnet 5 / max | `report/01_reproduction.md` |
| 2 | Data integrity | Sonnet 5 / max | `report/02_data_integrity.md` |
| 3 | Text-vs-data consistency | Sonnet 5 / max | `report/03_text_vs_data.md` |
| 4 | Code and methods | Sonnet 5 / max | `report/04_code_methods.md` |
| 5 | Privacy and archive hygiene | Sonnet 5 / max | `report/05_privacy_archive.md` |

Each auditor must write incrementally, verify against raw deposited artefacts, correct errors in the
brief, label findings CONFIRMED / LIKELY / SPECULATIVE, give the impact on published conclusions,
record benign explanations tested, report negative results, and leave rerunnable evidence in its own
work directory. A non-empty skeleton is not completion; the role must return cleanly and the report
must contain its checks, findings, open questions, and evidence inventory.

## 3. Ledger, adjudication, and first draft

Read all five reports end to end. Encode every finding and tested negative as one row in
`work/findings.jsonl`; set `ledger_disposition` to `INCLUDED`, `MERGED` with a target, or `EXCLUDED`
with a defensible reason. Never synthesize from subagent summary messages alone.

Use targeted adjudication:

1. Mechanically merge non-conflicting report items into the ledger.
2. Recompute disagreements, new material findings, blocking items, and refuted/overstated claims.
3. For a conclusion-changing claim, a coordinator–auditor disagreement, or a criticism you reject,
   verify by a second structurally different route.
4. Spot-check confirmed facts; do not rebuild them unless the current decision depends on them.
5. Preserve withdrawn decisions in `report/ADJUDICATION.md` and immediately propagate each accepted
   decision to the registry and draft.

Draft `report/REPORT_v1.md`, copy the working version to `report/REPORT_final.md`, and maintain
`report/retired_claims.txt`. Every report number must be independently derived before publication;
do not carry an auditor's count forward on confidence alone.

Maintain one canonical JSON object per candidate or tested negative in `work/findings.jsonl`, using
[`schemas/v2/finding-row.schema.json`](schemas/v2/finding-row.schema.json). This JSONL file is the
runtime source; `schemas/v2/findings-registry.schema.json` is only an optional export envelope. After
each schema-valid batch, project the repeated artefacts:

```bash
python3 scripts/generate_finding_artifacts.py "$AUDIT_DIR" --apply
```

The generator writes every row to `report/LEDGER.md`, but projects only final-status,
coordinator-approved `INCLUDED` rows into `report/APPENDIX_A.md`, `comment.json` findings,
`claims_map.tsv`, the verification plan, and verification snippets. Never hand-edit these projections.
Use the generated Appendix A block verbatim in `REPORT_final.md`; keep narrative finding sections
synchronized with the registry.

## 4. Adversarial review

Before round 1, write:

| File | Required content |
|---|---|
| `PAPER.txt` | Correct citation, package location, and analysis type; mandatory |
| `ENV.txt` | Interpreters, packages, environment paths, and version caveats |
| `SLOW.txt` | Jobs critics should not rerun without a specific reason |

### Round 1

Run stats, data, fairness, and completeness together at Terra/high. The driver freezes the current
draft as `REPORT_TO_REVIEW.md`.

```bash
export AUDIT_DIR="<durable-audit-dir>"
CRITICS="stats data fairness completeness" ADJUDICATE=auto \
  bash scripts/run_critique.sh 1
```

The driver merges verdicts by claim and writes `adjudication_packet.json` with trigger ids, reasons,
and source roles. `ADJUDICATE=auto` calls Terra/max only for conflicting verdicts, disputed
conclusion-changing claims, high-impact findings lacking two independent evidence records, or a
failed settled-fact spot check. With no trigger there is no synthesis or adjudicator call. The Sonnet
coordinator integrates the targeted result. For each accepted, contested, or unresolved item, update
`critique/round_1/RESPONSE_notes.md`, the adjudication log, findings registry, retired claims, and report.

Before applying any round-1 response, preserve the exact pre-integration sources for delta routing:

```bash
cp "$AUDIT_DIR/report/REPORT_final.md" \
  "$AUDIT_DIR/critique/round_1/REPORT_BEFORE_INTEGRATION.md"
cp "$AUDIT_DIR/work/findings.jsonl" \
  "$AUDIT_DIR/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl"
```

Then run the consistency gate, record its immediate exit status, generate the settled cache
immediately after round 1 integration, and always generate the content-bound round-2 delta packet:

```bash
python3 scripts/generate_finding_artifacts.py "$AUDIT_DIR" --apply
bash scripts/check_consistency.sh "$AUDIT_DIR/report/REPORT_final.md" \
  "$AUDIT_DIR/report/retired_claims.txt"
bash scripts/make_settled.sh "$AUDIT_DIR" "$AUDIT_DIR/work/findings.jsonl"
python3 scripts/make_diff_packet.py "$AUDIT_DIR" --round 2 \
  --before-report "$AUDIT_DIR/critique/round_1/REPORT_BEFORE_INTEGRATION.md" \
  --after-report "$AUDIT_DIR/report/REPORT_final.md" \
  --before-findings "$AUDIT_DIR/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl" \
  --after-findings "$AUDIT_DIR/work/findings.jsonl" \
  --output "$AUDIT_DIR/critique/DIFF_MANIFEST_round_2.json"
```

If both sources are byte-identical, the helper emits a content-bound `no_changes` packet with empty
change arrays and the conservative fairness-plus-completeness route. Round 2 still consumes that
generated manifest; never replace an unchanged integration with an unbound hand-written fallback.

Every JSONL row follows the linked schema: stable identity, scientific domain, exact source locus,
ledger disposition, and policy classification; rerunnable evidence with relative source paths and
SHA-256 hashes; expected, observed, and tolerance; verification records that bind the reviewer,
unique invocation id, immutable output path/hash, observed model family, and outcome; environment fingerprint;
dependent finding ids and artefact paths; disclosure, remedy, conclusion-impact detail, and provenance. A
promotable row also records benign explanations ruled out, its `Section N[.N]`, and a code,
judgement, or unverifiable verification snippet. A row is promoted only when its status is confirmed,
verified, or accepted, the coordinator approves it, and at least two independent reviewers from two
model families confirm it. `make_settled.sh`
joins those confirmations to `invocations.jsonl`, verifies the reviewer outputs and source hashes,
requires a completed/integrated/passing Round 1, and generates both
`SETTLED.json` and `SETTLED.md`; never hand-edit them. Resolve any intended promotion listed as
rejected in `SETTLED.json`, rerun `make_settled.sh`, then log
`settled used=true generated_after_round=1`.

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

An explicitly named missing manifest is fatal. A `CRITICS` override may be used for recovery, but the
driver still retains completeness. The driver again emits `adjudication_packet.json`; the Sonnet
coordinator resolves only its triggers.

After accepting a batch, update `work/findings.jsonl`, rerun the projection helper, replace the
Appendix A block in the report, and then run consistency. This one-source update replaces separate
rewrites of the ledger, comment findings, claims map, and verification plan. If another round is
needed, preserve round N's pre-integration report and registry before editing, then run
`make_diff_packet.py --round N+1` against those files and the integrated outputs.

After every later-round integration, run the four whole-document gates against the same bound state:

```bash
python3 scripts/run_post_delta_gates.py "$AUDIT_DIR" --round 2 \
  --report "$AUDIT_DIR/report/REPORT_final.md"
```

The command runs consistency, release-bound privacy, canonical checklist/projection, and intake-
archive checks in parallel. It accepts only a passing content-bound
`critique/round_N/POST_DELTA_GATES.json`; failures are written separately as partial receipts.
Finalization replays every receipt and reruns all four gates against the latest report.

The retry limit is **three total attempts per role**, including initial parallel execution, serial
recovery, and later resume. A partial file does not reset the budget or prove completion. Fail fast
on an unsupported model, exhausted credits, or a dead network.

When a round is backgrounded, wait on the process and its machine-readable status, with a deadline:

```bash
DIFF_MANIFEST="$AUDIT_DIR/critique/DIFF_MANIFEST_round_2.json" ADJUDICATE=auto \
  bash scripts/run_critique.sh 2 > "$AUDIT_DIR/work/round2.log" 2>&1 &
bash scripts/wait_for_round.sh "$AUDIT_DIR" 2 --pid $! --timeout-min 90
```

Two rounds are required. After them, use `target_complete` only when every changed claim id received a
subsequent adversarial review and no unresolved BLOCKING or conclusion-critical item remains. Otherwise
integrate the result and run another routed round. Use `converged` only when a round after the target
returns no factual correction and no unresolved BLOCKING or conclusion-critical item. At round 5 use
`cap_with_unresolved`, listing every unresolved item and every section changed after its last review.
Use `aborted` only for a genuine blocker or failed run.

Every `round_result` event records `factual_corrections=<integer>`. A `converged` stop requires a
round beyond the target whose value is exactly zero; null or a qualitative `character` label is not
evidence of convergence.

Before finalization, record the actual stopping evidence atomically. For example, when only F2 changed
and was subsequently reviewed:

```bash
bash scripts/log_event.sh review_scope changed_claim_ids:='["F2"]' \
  rereviewed_claim_ids:='["F2"]' unresolved_blocking_item_ids:='[]' \
  unresolved_conclusion_critical_item_ids:='[]' unreviewed_sections:='[]'
```

Replace the example with the run's complete sets. `target_complete` requires every changed id in the
re-reviewed set and all three unresolved/unreviewed lists empty; the other stopping values retain
their real unresolved state.

## 5. Deliverable, verification, and filing

Follow `reference/report_structure.md`. The comment is a paper: an Abstract of at most 250 words,
numbered sections beginning with Introduction and ending with Conclusion, references, and Appendix A.
There is no executive summary, no findings table in the body, and no Recommendations section. The
body cap is 10 rendered pages; references and appendices begin on new pages and do not count.

Before release:

1. Rerun the finding projection helper. It replaces Appendix A and writes one ordered body
   cross-reference `[claim N]` entry per included finding, exactly matching `comment.json`; do not
   hand-key duplicate inline tags.
2. Run `check_consistency.sh` and resolve every failure.
3. Build the DOCX/PDF and inspect rendered pages, especially page 1 and tables.
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
8. Finalize the metadata pair, then publish five bound artefacts plus the release manifest.

```bash
python3 scripts/generate_finding_artifacts.py "$AUDIT_DIR" --apply
bash scripts/build_docx.sh "$AUDIT_DIR" <output-name> # validated content cache is on by default
bash scripts/make_verification_notebook.sh "$AUDIT_DIR"
bash scripts/run_verification.sh "$AUDIT_DIR"
bash scripts/make_claims_script.sh "$AUDIT_DIR"
bash scripts/stage_release.sh "$AUDIT_DIR" <clean-release-dir>
export AUDIT_STOP_REASON="target_complete"
bash scripts/finalize_run.sh "$AUDIT_DIR" --stopping-reason "$AUDIT_STOP_REASON"
bash scripts/publish_completed.sh "$AUDIT_DIR"
```

The document cache is bound to the report, front matter, captions, exact reference document, build
code, rendering-tool identities, output name, and page-limit options. Use `--refresh-cache` to force
a rebuild and replace the validated entry, or `--no-cache` to bypass both lookup and storage.

Set `AUDIT_STOP_REASON` from the four-value rule above; never label the required two-round finish as
convergence. `publish_completed.sh` files the PDF, claims script, metadata pair, and finalization
receipt plus `release-manifest.json` (six files total).

The finished audit retains the final Markdown, DOCX/PDF, five auditor reports, ledger, adjudication,
critique reports and responses, verification notebook and snippets, work scripts,
`work/findings.jsonl`, its generated projections, `SETTLED.json`, `SETTLED.md`,
`verification_manifest.json`, `run.json`, `comment.json`, and the claims script.
`publish_completed.sh` releases the reservation only after the validated bundle is filed.
