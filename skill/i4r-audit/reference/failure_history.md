# Historical failure patterns

This is a diagnostic reference, not pipeline policy. Current operating defaults live in
[`config/pipeline-policy-v2.json`](../config/pipeline-policy-v2.json). Read the matching section when
a run shows the symptom or when changing a gate; do not load this history for an ordinary audit.

## Three CLI gates, not one (measured 2026-09-03)

A command can be refused by any of three independent mechanisms. Diagnosing a denial means
naming which one fired; the fix differs completely.

| Gate | What it refuses | Message |
|---|---|---|
| `permissions.allow` prefix rules | a command whose prefix is not listed, and separately any `find` flag that writes or executes (`-fprint`, `-exec`) regardless of the prefix rule | "cannot be auto-allowed by a `Bash(find:*)` prefix rule" |
| `--add-dir` working directories | `find`/search outside the directories passed to the CLI, even when the sandbox allows reading them | "may only search files in the allowed working directories" |
| `sandbox.filesystem` (seatbelt) | any read or write outside allowRead/allowWrite, including every path when a rule contains a glob metacharacter | `Operation not permitted` |

The Write tool is confined by none of them when given a relative path under the process cwd,
which is why it is useless as a probe of sandbox health. Shell redirection is not supported by
the permission matcher at all, so a check that needs Bash to produce a file must use a command
that writes directly (`cp`, `mkdir`) rather than `>`.

## A directory name disarmed the sandbox (Quossi audit, 2026-09)

The audit root ended in `dependence?`. The CLI reads `sandbox.filesystem` entries as glob
patterns, so every rule containing that `?` compiled to a pattern matching nothing.

| Symptom | Established cause | Required response |
|---|---|---|
| `mkdir -p` denied inside the attempt's own `output.partial`, `find` denied inside its own role packet, both while listed in `permissions.allow` | The Bash allowlist and the filesystem sandbox are separate gates; the allowlist admitted the command and the inert filesystem rule then denied the path | `assert_sandbox_expressible` refuses `? * [ ] { } \\` in any path entering a sandbox rule, before any provider call |
| Preflight receipt reported `sandbox_paths: true` on a completely inert sandbox | Every preflight write goes through Python, not through the sandbox, so it cannot distinguish a working sandbox from a dead one | Receipt adds `sandbox_rules_expressible`, the one sandbox property decidable without a provider call |
| Capability probe passed on the same inert sandbox | The probe only used the Write tool, which takes a relative path under cwd and is not seatbelt-confined, so it succeeds when every rule is inert | Probe must now produce `evidence/bash.ok` (`mkdir`+`cp`) and `packet_listing.txt` (`find`), verified on disk |
| Two roles each burned all three attempts on `API Error: Connection closed mid-response` | Symptom, not cause: the roles fought a dead environment for 24 and 46 turns until context reached 1.98M and 2.71M tokens and the connection dropped | Denials of allowlisted commands raise `EnvironmentFaultError` and pause the phase instead of re-sampling |
| Absolute `/Users/<account>/...` path in `phase1/status.json` | The role-failure path writes `str(exc)` directly, bypassing `make_terminal_record` where `redact_private_paths` is applied | Redact at the `failures[role]` assignment |

Measured character set: `?`, `*`, `[`, `]`, `{`, `}` and `\\` break the rules; space, `:`, `~`,
`(`, `)` and `!` are safe. Paper 1's root contained a colon and spaces and ran normally.

The general lesson is that a check which does not run through the mechanism it certifies will
report success on a broken mechanism. Both the preflight receipt and the probe asserted sandbox
health through the one code path that bypasses the sandbox.

## Contract stated in prose, enforced by a validator (Brimmell audit, 2026-08/09)

Nine of the first production audit's Phase-1 failures shared one cause: `phase1_runtime.py`
rejected a rule that the role prompt described loosely or not at all. The fake-runner tests could
not see it because the fakes emit validator-perfect output. Symptom, cause and response:

| Symptom | Established cause | Required response |
|---|---|---|
| `scientific_fields has an invalid exact field set` from 4 of 5 roles | Prompt said "passed-check IDs, open-question IDs"; validator required `passed_check_ids`, `open_question_ids` | Name every validated key literally; `tests/test_prompt_contract.py` pins the prompt to the validator's vocabulary |
| `role evidence must be a regular non-symlink file: .../evidence/evidence/x.R` | Manifest paths resolve under `evidence/`; prompt never said so, roles prefixed `evidence/` | Prompt states the path base with an example |
| `role output contains files outside the closed output contract` | Roles left `work/`, `scratch/`; prompt told them to copy code there and never said to clean up | Prompt states the closed contract and the cleanup step |
| `role evidence manifest contains an unbound source hash` | `ROLE_PACKET` carries both `source_manifest` and `assigned_evidence`; prompt said "copied from ROLE_PACKET" | Every citation names `source_manifest`; the index files are listed as never citable |
| `provider reported sandbox permission denials` rejecting five complete reports | Denial treated as an authorization failure; under `--bare` the docile model never tripped it | Denials are recorded to `sandbox_denials.json`, not fatal |
| `Not logged in · Please run /login` from every probe | `--bare` never reads OAuth or keychain credentials | Runners use `--safe-mode` only; `sandbox_policy_hash` records `bare: False` |
| `capability probe exceeded 25000 normalized tokens` | Bound calibrated for bare mode's stripped context; safe mode costs ~70-100k before any work | Bound is 150,000, measured |
| `capability probe exceeded 150000 normalized tokens` (2.7.0 and earlier audits) | A bound calibrated on 81-98k probes: by 2026-09-30T09:02Z, 11 of the 37 probes in `runs/` that used tokens went over it (160,004-254,191, in 8 audits; 3 also failed the contract check), against 26 passing at 67,830-149,259 | 2.7.1: new audits freeze 350,000 (about 1.4 times the worst); an existing audit keeps 150,000 (it keys its probe and receipts). Re-run the probe: all 8 audits passed on a retry after 1-3 over-bound probes. Or, before Phase 1 is paid for, re-initialise with the full `init_run.sh --force` command (INSTALL.md) |
| `completed invocation output hash mismatch` blocking the coordinator | Validator re-hashed every historical completed row; roles re-promote to fixed canonical paths | `read_ledger` byte-verifies only the latest completed row per artifact |
| Transport drop classified `permanent`, no retry | Error text `API Error: Connection closed mid-response` matched no transient pattern | Classifier treats connection closed/reset/timeout as transport |
| Coordinator succeeded (45 turns, 35 min) and reported four complete files, but `output.partial` was empty and the attempt failed `schema` | The model wrote to the attempt directory, one level up; the CLI's Write tool ignores the sandbox filesystem rules, so only cwd and prose steered it | Attempt root is read-only during the call; prompt names the working directory; `recover_misplaced_coordinator_outputs.py` relocates and revalidates without a provider call |
| `--apply requires report/REPORT_final.md and comment.json` on a fresh audit | Nothing in the skill created the comment.json skeleton the projection injects findings into | `scripts/init_comment.py` writes the schema-valid skeleton from run.json plus a facts file |
| Pre-Terra `--apply` would rewrite `REPORT_final.md` with an empty Appendix A and strip every claim tag | Projections come from coordinator-approved rows only, and no row is approved before Round 1 integration | `--apply` refuses with zero approved rows; preview without `--apply` until integration approves one |
| Coordinator refused at turn 33 after 36 min: `safety measures that flagged this message for a cybersecurity topic` | The provider's own real-time content-safety classifier, not a skill gate; the audit's finding that 38 of 131 rows have a unique demographic combination reads like re-identification analysis | Classified `provider_safety_refusal`, never retried with identical content. Write privacy findings as data-stewardship counts and banding remedies, never as a procedure for identifying a person; the coordinator prompt now says so. The skill cannot and must not disable the provider classifier |

Rule: when a validator gains a requirement, the prompt gains the same sentence, and a test asserts
the prompt contains it. A green fake-provider suite proves nothing about this class.

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

## Bookkeeping gates that discarded valid work (first production audit, 2026-09)

The Brimmell et al. (2025) audit lost no model output to a model failing at its task. It lost a
complete four-critic round, two complete fourth-attempt reviews, five Phase-1 reports and about $70
of provider spend to harness bookkeeping: a round identity that bound the driver's own hash, a
tie-break between two valid same-input reviews treated as fatal, permanent-failure markers that
stopped a role before any provider call, a schema-invalid sample treated as a property of the role,
two independent attempt ceilings (`MAX_TRIES` and `I4R_MAX_ATTEMPTS`) that disagreed, a snapshot
reuse check that compared code hashes, and an argparse `choices` list that rejected the retry class
the driver had just been taught to use. Each was removed or turned into a note (CHANGELOG 2.2.0,
"Guardrails that discarded valid work"). The rule that survives: a gate may refuse to *publish*
invalid work, but it must never *discard* a complete provider response over a condition the operator
can resolve; record the condition, keep the bytes, and let the next attempt or invocation continue.
Critic-side errors on that audit were real but recoverable by re-sampling: one critic cited the
evidence inventory file itself as an artifact, another set a `finding_id` on a new issue; both
passed on a fresh sample once the driver stopped discarding attempts.

## A synced folder re-applied a directory mode to a frozen snapshot (2026-09-24 and 2026-09-27)

On the ninth production audit the Dropbox File Provider reset a freshly frozen critique snapshot's
`source` directory to mode 0700 seconds after the freeze, during the model probe. Every file stayed
0444 and every hash matched. The only immutability check ran after each paid call, raised a plain
error on the write bit, and the driver read that as a schema-invalid response: every critic was
re-sampled against the same snapshot, the focused repair re-ran the same check and silently fell
through, and twelve paid calls were spent before the round failed. The adjudicator's target snapshot
hit the same race. An audit three days earlier had shown the same drift, on the same directory.

| Symptom | Established cause | Required response |
|---|---|---|
| `immutable path regained write permission` on `.../snapshots/<sha>/source` or `target_snapshots/<sha>/source`, the mode 0700, every file 0444 and every hash unchanged | The synced folder re-applied the mode it recorded when `mkdtemp` created the directory (0700), after the tree was frozen and moved across parents | Mode-only drift is re-frozen in place under a lock and journaled once (`snapshot_mode_refrozen`); the paid response is kept. `mkdtemp`-born source directories are given an ordinary mode before the move, a hypothesis that does not replace the check |
| A snapshot fault classified as `schema`, re-sampled to the budget | The helper exited 1 for every failure, so the driver could not tell a changed snapshot from a bad response | `SnapshotIntegrityError`, helper exit 5, `error_class` `environment`: one call, no repair, no retry, no permanent marker against the role |
| Nothing checked the snapshot between the freeze and the provider call | The check lived only after each call | `verify-snapshot` runs before every claim and reservation (probe, each critic attempt, the focused repair, each adjudicator attempt); a content fault stops the role with no attempt, reservation, row or call |
| A strict check used by one-off tools and compatibility sweeps must not write | Re-freezing is a side effect | Opt-in only (`I4R_SNAPSHOT_REFREEZE=1`, exported by the driver); unset, every check is strict and read-only |

A content, inventory or manifest change is never repaired: in the audit directory run the command
the refusal prints, `chmod -R u+w <dir> && rm -rf <dir>` (a plain `rm -rf` cannot remove a frozen
tree), and rerun; it is rebuilt identically. A mode that keeps coming back after bounded re-freezes
while the content verifies is `snapshot_mode_unstable`: stop the sync client or move the audit off
the synced folder instead. Phase 1 and the post-fan-out verifier
apply the same rule to their own snapshot (directory modes are judged only when re-freezing, so
old snapshots stay admissible), and `recover_coordinator.py` does so when the variable is set.

## A literal round cap, and a finalized audit that could keep running (2.6.9)

Two audits (Ziv, Neudorfer) stopped `cap_with_unresolved` after round 5 with one changed section never
re-reviewed, and the programme decided to allow seven rounds. Raising `critique.round_cap` in the
policy changed nothing for them. Each audit's snapshot froze 5 at init. The driver refused round 6
from a literal that never read the snapshot, and the post-delta receipt schema admitted rounds 2-5
only. A hand edit would have broken the snapshot's hash binding, and `init_run.sh --force` restarts
the journal. Scratch copies also showed the opposite gap: the journal was never sealed, so a
finalized, published audit could prepare, authorize and run round 6 with no record that it had been
reopened, while its `run.json` still showed the old stop. Now every cap site reads the audit's own
snapshot. A raise is `amend_round_cap.py` (the snapshot history, a read-only archive of the replaced
bytes, a `policy_amendment` event). A new round on a finalized audit needs a journaled
`audit_reopened`. `round_cap_amendment.py` proves the chain at finalize and publication. Tests:
`tests/test_269_round_cap_amendment.py`.

## Release and metadata failures

### Fessi regression incident (frozen 2026-08)

The principal optimization fixture recorded a lower bound of 118,249,574 tokens. Failed attempts
accounted for 47,973,272; five rejected Phase-1 sessions alone accounted for 45,853,590. Claude cache
reads accounted for 109,041,155 tokens over 953 turns. The run paused before any Round-2 inference,
but its projection claimed Round 2 had started, and an environment override allowed a 14-body-page
PDF to replace the intended ten-page release.

Preserve that history rather than rewriting it. Migration appends corrective lifecycle events,
projects Round 2 as `awaiting_authorization`, includes all 38 known invocation records, keeps Terra's
served identity unverified where the adapter exposed only the request, and marks max-14 documents
diagnostic-only. The regression fixture exists to prove that preflight prevents rejected fan-outs,
unknown failed-call usage remains unknown, round state starts only at provider dispatch, and an
over-cap build cannot stage or replace a passing document. Do not rerun its Round 1 to make the
history look cleaner.

Late validation has previously exposed missing schemas, invalid enum values, stale report snapshots,
privacy controls embedded in the files being scanned, and metadata that was never captured during the
run. Run toolchain controls before the audit, validate metadata incrementally, and log events when they
happen. Never reconstruct token usage, model identity, environment, or human effort from memory.
