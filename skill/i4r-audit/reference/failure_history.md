# Historical failure patterns

This is a diagnostic reference, not pipeline policy. Current operating defaults live in
[`config/pipeline-policy-v2.json`](../config/pipeline-policy-v2.json). Read the matching section when
a run shows the symptom or when changing a gate; do not load this history for an ordinary audit.

## Completion, retries, and long jobs

| Symptom | Established cause | Required response |
|---|---|---|
| A round appears to run for hours after reports exist | A waiter polled human log wording rather than the process and machine-readable status | Wait on the PID, impose a deadline, then validate `ROUND_STATUS` |
| A report exists but contains no completed assessment | Critics write a skeleton early and a cutoff leaves it behind | Completion requires clean process exit and explicit role-complete status; quarantine partial output |
| Every critic fails with no output | Rejected model, exhausted credits, or network failure was retried as if transient | Probe once, preserve the provider error, and fail fast on deterministic failures |
| A retry storm exceeds the configured budget | Parallel and serial passes reset independent counters | Count attempts per role across all execution modes and resumes |
| A live shell reports syntax failure on a valid line | The running shell script was edited while Bash was reading it | Never edit a driver during its run; resume the same round after the process exits |

One audit spent 22 hours waiting for a case-sensitive log phrase after the round had finished in 17
minutes. The general rule is more important than the incident: silence must never be ambiguous
between “still running,” “failed,” and “complete.”

## False-clean detectors

A detector that only counts violations passes vacuously when it inspected nothing. Every absence
test needs both a positive control and a work-performed assertion.

Known variants:

- a privacy regex accepted one date form but not ISO dates;
- a word boundary could not match identifiers joined by underscores;
- a byte-oriented scan could not see UTF-16 text;
- a notebook comparison loop ran zero iterations and therefore reported zero mismatches;
- a claim extractor found zero claims, leaving zero stubs for its stub gate to reject;
- a check transcribed its “published” expectation from its own computed output and could not fail;
- a body section disappeared while claim tags and appendix rows still agreed with each other.

Before trusting a zero, state the intended scope across files, sections, lines, forms, encodings, and
data regimes. Assert the expected number of objects inspected. Test at least one known-present form
and one known-absent control. For a formula with a cap, floor, branch, or regime switch, self-test the
simplification in every regime occupied by the data.

## Package and workspace contamination

Auditors have accidentally changed deposits by knitting notebooks in `pkg/`, creating caches, and
sharing output names. One controlled model comparison had one arm overwrite the other arm's evidence.

- Make the package read-only after hashing it.
- Copy executable notebooks and code into `work/<role>/`.
- Give every role a private directory and unique output paths.
- If a manifest changes, investigate the audit before alleging a package defect.
- Stage releases into a clean directory; do not clean a live workspace in place.

## Synthesis and propagation loss

Real findings have repeatedly appeared in full auditor reports but not their summary messages, then
disappeared during drafting. Other corrections reached a body section but not the abstract or
appendix. Prevent both classes mechanically:

- put every full-report item in the canonical findings registry before drafting, give every exclusion
  a defensible reason, and generate the ledger from it;
- map each body claim to one appendix row, one `comment.json` finding, and one verification entry;
- preserve withdrawn decisions and scan the whole draft for retired wording;
- after an accepted correction, update the registry and draft in the same turn, regenerate the ledger,
  appendix, metadata, claims map, and verification plan, then propagate the wording to the abstract;
- verify that a cited section exists, not merely that its claim number appears somewhere.

## Verification-notebook failures

The notebook is a prediction-versus-observation artefact. Expectations must be declared before the
computation, not read back from it. A missing check is a failure, not silence.

Historical defects included stale stub counters, claim text anchored to the wrong sentence, helpers
defined after the chunks using them, and tolerances wide enough to hide a reported discrepancy. A
tolerance must separate genuine matches from the errors the comment reports. A stochastic check needs
a documented Monte Carlo tolerance or seed. A judgement or unverifiable marker must carry its basis.

## Model and prompt attribution

Prompts once carried a citation, file path, and worked finding from the previous paper. Keyword sweeps
did not catch generic stale assertions. Read the first rendered prompt end to end and ask of every
sentence whether it asserts that a paper-specific file or fact exists.

Requested model names are not proof of the served model. Record provider-returned identity per role
and round. A fallback onto the coordinator family invalidates the independence claim even if the
output itself looks sound.

## Accuracy lessons from controlled comparisons

Two legacy model A/B audits informed a superseded routing policy; they are historical observations,
not the current baseline in `pipeline-policy-v2.json`:

- Sonnet was efficient and strong at area 3's bounded enumeration.
- Opus found the conclusion-changing causal/mechanistic issues in areas 1, 2, 4, and 5.
- Opus also produced confident but wrong refutations, so negative results from a stronger model still
  require re-derivation.
- Ten auditors missed a model-specification defect later found by adversarial review. Do not exchange
  critique coverage for a second broad auditor fan-out.
- Round 1 can review the existing draft but not prose written in response to its own corrections;
  retain round 2.

These observations justify retaining the verification safeguards, not overriding the current routing.
Re-derive every number that enters the comment,
verify the remedy as a claim, and use a second structurally different route when the coordinator and
an auditor disagree.

## Release and metadata failures

Late validation has previously exposed missing schemas, invalid enum values, stale report snapshots,
privacy controls embedded in the files being scanned, and metadata that was never captured during the
run. Run toolchain controls before the audit, validate metadata incrementally, and log events when they
happen. Never reconstruct token usage, model identity, environment, or human effort from memory.
