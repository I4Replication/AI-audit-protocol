# Packaged Phase-1 runtime

`scripts/run_phase1.py` is the only supported launcher for the five Phase-1 auditors. Audit-local
launch/retry scripts are not part of the execution contract.

## Invocation

```bash
python3 scripts/run_phase1.py "$AUDIT_DIR"
```

The canonical limits are the audit's frozen timeout (5,400 seconds for an audit initialised on
2.7.2 or later, 2,700 seconds before; `scripts/phase1_timeouts.py`) and USD 15 per auditor call. A
call the timeout stops is terminalized by the runner as `timed_out`; it is a charged attempt when
the model wrote into its output workspace, and a request not started otherwise. One monotonic provider-free
pre-dispatch deadline covers source traversal, hashing and copying, paper extraction, deterministic
scans, immutable role-packet construction, and the exact sandbox/read/write/promotion preflight.
It is capped at 60 seconds; subprocesses and scans receive only its remaining time. The deadline
may be tightened for a test but never raised. A timeout pauses with zero auditor inference.
The provider timeout and budget options
are parsed for an explicit diagnostic, but a canonical run rejects any deviation from the exact
hash-bound policy values (including a stricter local override).
The runner reads the five auditor assignments from the audit's hash-bound policy snapshot and
rejects substitutions. They share one Claude route from `scripts/claude_routes.py`: Sonnet 5.5/xhigh,
spawned as `--model claude-sonnet-5-5 --effort xhigh`, for an audit initialised on 2.7.3 or later;
Sonnet 5.5/max for one initialised on 2.7.0 to 2.7.2; and Sonnet 5/max, spawned as `--model sonnet`,
for an earlier one. Each audit keeps its route, effort included, for its whole life.

## Ordered gates

1. Validate the policy, run identity, package-manifest checksum closure, raw source types, and source
   readability. Symlinks and special files are rejected.
2. Copy `paper/` and `pkg/` once into `phase1/snapshots/<sha256>/raw`, build the deterministic common
   evidence, bind all context hashes, and make the snapshot read-only.
3. Create one immutable packet per role. Every role retains read-only raw access, but receives only
   the deterministic evidence indexes routed to its assignment.
4. Run the provider-free smoke test. It checks the exact read/write/deny paths, deny-all network
   policy, explicit Bash patterns, source hashes, output filesystem, and atomic promotion path.
5. Reuse or run one synthetic capability probe. Its key binds the CLI version, requested model,
   effort, sandbox-policy hash, and probe-contract hash. The probe must exercise structured output
   and file write, expose exactly one primary of the route's model plus per-model usage, and use no more than
   the bound its snapshot froze (`phase_1.runtime.probe_max_normalized_tokens`, printed by `init_run.sh`):
   350,000 normalized tokens when init froze the shipped 2.7.1 policy, 150,000 for an audit initialised
   earlier (or one whose init reused a 2.7.0 leftover snapshot, which init warns about). An audit keeps
   its bound for life because it is part of the probe contract (`scripts/probe_bounds.py`). The bound
   covers the CLI's own system prompt and tool schemas, paid on every call in safe (non-bare) mode
   before the probe does any work. As of 2026-09-30T09:02Z the ledgers under `runs/` held 26 passing
   probes (67,830 to 149,259, median 122,621; 25 Sonnet 5, 1 Sonnet 5.5 at 93,846) and 11 that
   failed over 150,000 in 8 audits (160,004 to 254,191); each of those 8 audits passed on a re-run
   probe after 1 to 3 over-bound ones. The programme lead chose 350,000: about 1.4 times the worst
   (254,191) and about 2.3 times the worst passing one, and it still bounds a probe that loops (at
   roughly 13 to 15 turns, where a passing probe takes 5 to 8). The Sonnet 5.5 live canary used 124,283.
6. Dispatch all five roles through one five-worker executor. Each call uses safe mode, an
   empty strict MCP configuration, no browser/plugins/network, no session persistence, a unique
   session ID, the explicit tool/Bash allowlist, and an environment without `ANTHROPIC_MODEL` or
   `ANTHROPIC_DEFAULT_SONNET_MODEL`, which could remap the alias `sonnet`. Bare mode is not used: it refuses OAuth and
   keychain credentials, so a subscription-authenticated CLI could not start an auditor.

A local preflight or probe failure writes `phase1/status.json`, projects a metadata-2.1 pause, and
starts zero auditor inferences. A successful retry projects `pause_end` before continuing.

## Deterministic evidence

The common evidence is computed once per content-addressed source snapshot:

- extracted PDF text;
- a file/hash/type inventory;
- CSV/TSV/XLSX sheet dimensions, cell types, missingness, and duplicate-row counts;
- code and candidate-entrypoint inventory;
- a host/user-free environment fingerprint;
- archive and licence/notice metadata; and
- a privacy scan that emits detector counts, never matches or participant rows.

The generated summaries contain no model conclusions. Workbook duplicate detection hashes rows
internally but emits only counts. Raw claim-critical verification remains available from the frozen
snapshot. Each immutable role packet contains only the deterministic indexes assigned to that role;
the provider-visible source root contains only frozen `paper/` and `pkg/` trees. The shared evidence
directory and other roles' packets are never mounted into an auditor session.

## Role output contract

An attempt writes incrementally under its private `output.partial` directory. Acceptance requires:

- clean provider exit, exactly one observed primary of the route's model (for Sonnet 5.5,
  `claude-sonnet-5-5` and no other Sonnet id; for Sonnet 5, the pre-2.7.0 check), complete normalized usage, and no
  permission denial;
- `REPORT.md` with all seven required sections, one role-completion marker, source-hash joins, and
  evidence/benign-explanation/impact/remedy treatment for every finding;
- `result.json` with the exact role/snapshot/report/evidence bindings and structured scientific
  fields. Every finding includes its full claim, source-bound evidence/result records, impact, and
  remedy; and
- `evidence/manifest.json` closing over every regular evidence file and binding every cited raw
  source hash.

Only a validated report and evidence directory can be promoted. Promotion is serialized and rolls
back before terminal journaling. Once the terminal event exists, canonical bytes stay at the
recorded hash; a missing completion receipt is rebuilt from the retained response instead of
rolling back or making another provider call.

## Retry and resume behavior

- Only accepted inference starts allocate the role attempt budget (three by default; `I4R_MAX_ATTEMPTS`
  raises the ceiling to at most 20).
- Transport, throttling, and 5xx-style transient failures are retried with backoff.
- Authentication, authorization, quota, model/capability, and other permanent provider failures
  stop the current invocation; a later invocation retries within the remaining budget, because
  those conditions describe one response and change between runs.
- A schema-invalid output is a property of one sample, not of the role: after at most one focused
  repair, the remaining budget is spent on fresh samples before the role is declared failed.
- A scientifically usable output with a repairable format/closure error may receive one focused
  `output_repair`. Its prior-response-plus-diagnostics packet must be at most 20 percent of the
  original packet. A canonical projection byte-freezes every finding claim, evidence/result,
  impact, remedy, all report prose, and every evidence artifact. Only structural JSON bindings may
  change.
- Successful role and probe receipts are content addressed. A rerun records cache reuse in metadata
  2.1 without allocating an attempt or provider process.
- A durable `response_received` receipt is recovered and validated before any new dispatch. A
  completed terminal record with a missing local receipt is reconstructed from its bound canonical
  output.

The complete quorum binding is `phase1/quorum.json`. The coordinator must consume only the five
canonical reports whose completed provider invocation hashes match that quorum.

## Post-fan-out verifier (2.6.4)

A finding first raised after fan-out (by the operator, the coordinator or a critic) has no Phase-1
auditor to supply its family-A confirmation. In audit A23 three true findings (F13-F15)
were excluded for that reason after hand-made verification records were refused by make_settled
and finalize. `scripts/run_verifier.py` is the packaged family-A role for such rows. It runs one
blind call per batch on the auditors' Claude route, under this same runtime contract, and
`scripts/verifier_contract.py` defines the record that the downstream gates admit.

```bash
python3 scripts/run_verifier.py "$AUDIT_DIR" --findings F13 F14 [--dry-run]
```

Run it after a round reaches quorum and before that round is integrated. For operator or
coordinator rows written at draft time, run it before round 1. `integrate_round.py apply` binds
only verifier calls that ended no later than its integration timestamp.

**Refusals.** Every refusal exits 5, prints the reason and a remedy, makes no provider call and
appends no journal event. The runner refuses:

- an audit with no `phase1/` directory, or a pre-metadata-2.1 audit;
- an audit whose policy snapshot cannot be loaded, a verifier route that is missing or is not the
  packaged Claude route (`scripts/claude_routes.py` `LIVE_ROUTES`), or a completed Phase-1 auditor family set other than exactly
  `claude`;
- `--timeout-seconds` or `--max-budget-usd` overrides of the policy-bound limits;
- an audit whose `phase1/quorum.json` is missing, not `quorum_complete`, bound to another audit, or
  does not hash to itself;
- a quorum snapshot that no longer verifies;
- a missing `work/findings.jsonl`, a registry line that is not one JSON object with an id, or an id
  held twice; no ids, an id listed twice, or an unknown id;
- a row that `verifier_contract.eligibility_problem` rejects (not INCLUDED, no claim text, or
  raised or independently found by a Phase-1 auditor);
- two rows in the batch with the same claim text;
- a row with no `source_location` {path, locator}, or with no admissible `paper/` or `pkg/`
  source, meaning no `evidence.source_hashes` entry carries the snapshot's hash;
- a stranded attempt;
- a ledger holding any completed verifier record that is not admissible (fail closed: an
  unreadable record could be a REFUTED verdict, and dropping it would let its claim be sent
  again). `integrate_round.py` stops on the same state. The one exception is the batch's own lost
  receipt, which is rebuilt instead (see below);
- a claim text that already has an admitted verdict (`verifier_contract.collect_verdicts`: one
  verdict per claim text), including a REFUTED or UNVERIFIABLE whose call was later corrected to
  `legacy_record`;
- an exhausted budget, or two pre-inference requests (provider rejections before inference or
  recovered strands, in any mix). `I4R_MAX_ATTEMPTS` raises only the first limit.

**Identity.** Each call records `role_class verifier` and `round 0`. Round 0 keeps the call clear
of the critique-round authorization gate. The role is `verifier_<12 hex>`, from
`verifier_contract.batch_role` over the batch's claim hashes, so the same hypotheses always share
one budget. The route is `verifier_contract.verifier_route`, which for a pre-2.6.4 snapshot falls
back to the auditors' shared route, and it must be a packaged Claude route.

How it reuses the Phase-1 contract:

- **Snapshot.** It reuses the snapshot that `quorum.json` names and never calls
  `materialize_snapshot`, which would produce a new hash if tool versions or `PAPER.txt` had
  changed. It verifies that snapshot before dispatch and again after the call.
- **Packet.** It builds `phase1/role_packets/<snapshot>/<role>/<packet_hash>/`, frozen and closed
  by `packet_manifest.json`. The packet holds `ROLE_PACKET.json` and four deterministic routing
  indexes. For each hypothesis `ROLE_PACKET.json` carries an opaque `H1..Hn`, the claim verbatim,
  its `claim_sha256`, its `source_location`, and the `paper/`/`pkg/` `source_hashes` that match
  the snapshot. It withholds status, confidence, impact, tier, verifications, provenance, ledger
  disposition, approvals, expected/observed/rerun/tolerance, benign explanations, remedy,
  conclusion flags, snippets, settled state, dependencies, finding ids, and CHECKLIST.md and
  BRIEF_TEMPLATE.md.
- **Preflight.** It runs `provider_free_preflight` with `destinations=` (its own promotion
  target, receipt and status file), `workspace=phase1/verifier/<role>/preflight/<snapshot>/` and
  `extra_deny_read=`. The Phase-1 receipt is never overwritten.
- **Probe.** It calls `run_capability_probe` with the auditors' key, which normally hits the
  cache.
- **Dispatch.** It uses `run_phase1.run_role` with a `RoleTargets` object, so the CLI flags,
  sandbox, the audit's timeout / USD 15 limits, retry classes, budget (3 accepted starts per `(role, 0)`,
  `I4R_MAX_ATTEMPTS` up to 20), strand guard, response recovery and one focused repair are the
  auditors'. Beyond the protected list every attempt already carries (`paper/`, `pkg/`,
  `report/`, `work/`, `critique/`, `run.json`, `run.events.jsonl`, `invocations.jsonl`,
  `package_manifest.json`, `pipeline-policy.snapshot.json`), `sandbox_settings(extra_deny_read=...)`
  also denies reading `coordinator/`, `SETTLED.json`, `SETTLED.md`, `comment.json`,
  `claims_map.tsv`, `verification_snippets/`, `WRAPUP.txt`, `phase1/completions/`,
  `phase1/verifier/` and `phase1/transactions/`, plus the per-attempt list below.
- **Admission.** It calls the generic Phase-1 checks the auditors use (`read_role_output`,
  `check_result_bindings`, `check_output_hygiene`, `check_report_joins`,
  `check_evidence_manifest`, `check_output_tree`), then adds the verifier rules:
  - exact `result.json` keys, schema `i4r-verifier-result-1`, and the packet's
    `hypotheses_sha256`;
  - exactly one verdict per hypothesis, carrying the packet's `claim_sha256`;
  - a verdict of CONFIRMED, REFUTED or UNVERIFIABLE, with snapshot-bound evidence and a
    non-empty basis;
  - a `rerun.artifact` listed in the manifest for CONFIRMED and REFUTED;
  - the three REPORT.md headings, one `### H1: VERDICT` block per hypothesis that matches
    result.json, and exactly one `<!-- i4r-role-complete:verifier -->`.

  `run_verifier.VERIFIER_RULES` names every rule, and the prompt states each one. This is
  enforced by `VerifierPromptNamesValidatorRules`.
- **Repair.** A repair freezes the verdicts, the report prose and every evidence artifact. Only
  these errors are repairable: the report hash, the evidence-manifest hash, `hypotheses_sha256`,
  an unbound manifest binding, and a non-normalised artifact path.
- **Promotion.** The whole validated tree goes to `work/verifier/<role>/<input_hash>/`, a
  content-addressed path that is never overwritten. The terminal row (`output_artifact` = that
  `result.json`, `input_artifact_sha256` = the batch input hash, including for a repair) is then
  written through the lifecycle journal, followed by the `i4r-verifier-completion-1` receipt at
  `phase1/completions/<role>/<input_hash>.json`. That receipt is what `verify_record` requires.
- **Re-runs.** A completed batch replays from its receipt as a `cache_replay` row with
  `role_class verifier`. It makes no provider call and no probe replay.

The runner writes its own status to `phase1/verifier/<role>/status.json`. Finding ids appear in
that file and in the receipt only after the call. It never writes `phase1/status.json`,
`quorum.json` or anything under `report/`. It never emits `phase_start`, `phase_end`,
`pause_start` or `pause_end`, and it leaves the five auditor rows untouched.

Exit codes are 0 (completed, replayed, a rebuilt receipt, or a dry-run plan), 1 (a usage error
such as a missing `--findings`, a missing audit directory or Claude CLI, a probe key that changed
after planning, or an unexpected error), 2 (the call failed, or a replay or receipt rebuild
failed), 3 (local preflight or sandbox fault), 4 (probe) and 5 (refused).

`--dry-run` prints the batch role, the hypothesis-to-finding map, the packet and its hash, and the
input hash. It writes nothing, makes no provider call and appends no journal event.

A killed verifier attempt blocks later Phase-1 and verifier runs, like any other attempt. Clear it
with `python3 scripts/run_phase1.py "$AUDIT_DIR" --recover-stranded`, which keeps the
reservation's `role_class`.

**Recovery, refusal and re-admission (2.6.4 review fixes).** These hold on their own, whatever
the installation notes say:

- *Lost receipt.* If the runner dies between journaling a completed call and writing its receipt
  (a kill, an OOM, a failed write), re-run the same command, with that batch's own `--findings`.
  The runner sees its own completed row for the batch's role and input hash, rebuilds the receipt
  from the retained response (the attempt left at `response_received`), checks it against the
  promoted tree, and records a `cache_replay`. It makes no provider call and charges no budget.
  `--dry-run` reports `would_rebuild_receipt: true`. Do not correct such a row to
  `legacy_record`: that strands the retained response for good.
- *Fail-closed refusal.* Any other completed verifier record that is not admissible (a promoted
  file or receipt no longer at its recorded hash, or a record that fails re-admission) makes the
  runner refuse every batch with exit 5, and `integrate_round.py` stops with `FATAL: verifier
  record is not admissible` and prints `INADMISSIBLE_REMEDY`, in this order: (1) if the receipt is
  missing because the runner was killed after journaling the call, re-run that batch's own
  `run_verifier.py ... --findings` command, which rebuilds it with no provider call; (2) otherwise
  restore the record's files (`work/verifier/<role>/<input>/`, its receipt, its frozen packet) to
  their recorded hashes; (3) only for a genuinely tampered or foreign record, run
  `python3 scripts/record_invocation.py --correct <audit-dir> <invocation-id> <correction.json>`
  with `{"set": {"status": "legacy_record"}, "reason": "..."}`, never for a lost receipt.
  `make_settled.sh` does not stop: it rejects only the rows that cite a verifier record as a
  confirmation, each with the reason, and settles the rest. Finalize reports the record as a
  violation.
- *Strands and the pre-inference limit.* `--recover-stranded` writes the strand's terminal row
  with `inference_start_status: not_started`, so `role_preinference_request_count` counts it. Two
  such rows (recovered strands or provider rejections before inference, in any mix) refuse that
  batch for good; `I4R_MAX_ATTEMPTS` raises only the accepted-start ceiling.
- *Re-admission.* `verifier_contract.verify_record`, which every gate uses through
  `collect_verdicts` (the runner's refusal, `integrate_round.py`, `make_settled.sh`,
  `make_diff_packet.py` and finalize), ends by calling `run_verifier.readmit_record`. That call
  re-admits the promoted call exactly as the runner admitted it. It requires the exact
  `RECEIPT_KEYS` receipt bound to the call, and the snapshot that `phase1/quorum.json` binds (the
  manifest must hash to itself; the raw deposit is not re-hashed). It requires the frozen packet
  under `phase1/role_packets/<snapshot>/<role>/<packet_sha256>/`, closed by its manifest and
  hashing to `packet_sha256` with the runner's prompt, and the receipt's hypotheses must be the
  packet's. The promoted `work/verifier/<role>/<input>/` must pass `validate_verifier_output`
  with the receipt's hashes and source joins, and the call's retained model-identity file, when it
  names one, must match its recorded hash. Records written by hand fail, whatever their hashes:
  the receipt, the report, the result and the evidence manifest can all be written by anyone. If the runner cannot
  be imported, the record is refused. Operating-system metadata files in the promoted tree
  (`OS_METADATA_NAMES`: `.DS_Store`, `Thumbs.db`, `desktop.ini`, `Icon\r`, and `._*` files) are
  ignored; any other extra file breaks the closed output set. Because the receipt names its
  snapshot, a Phase-1 re-run that binds a new snapshot in `quorum.json` makes every earlier
  verifier record inadmissible. Tests build valid records only through
  `tests/verifier_fixtures.py`, which uses the runner's own functions.
- *Journal routes.* The command-line appends of `lifecycle_journal.py` (`append-event`,
  `append-kv`, `append-batch`, and so `log_event.sh`) and the lifecycle flags of
  `record_invocation.py` (`--reserve`, `--provider-request`, `--inference-start`,
  `--response-received`, `--cache-reuse`, the positional record) refuse any lifecycle event that
  names `role_class verifier` or a call already journaled as one. `record_terminal` refuses an
  unreserved verifier provider call. `record_invocation.py --correct` still works.
- *Superseded calls.* A call corrected to `legacy_record` is never bound and confirms nothing, but
  its REFUTED or UNVERIFIABLE keeps blocking its claim text while its record still re-admits
  (`collect_verdicts`); a superseded record that no longer re-admits blocks nothing and is not an
  admission error.
- *Per-attempt read denials.* The sandbox rules are a deny list, so anything they do not name is
  readable. Each verifier attempt therefore also denies, computed when its settings are written:
  every entry under `phase1/roles/` and `phase1/role_packets/` that does not lead to one of its
  own read roots (other roles, the other input hashes of its own role, and every other attempt;
  a repair keeps only its source attempt, whose frozen packet it reads). It also denies the
  finalization artefacts at the audit root (`claims_script.R`, `verification*`,
  `RUN_METADATA.md`, `run_metrics.json`, `finalization_receipt.json`, `RELEASE_MANIFEST.md`,
  `release-manifest.json`, `document_build_manifest.json`), the release directory beside the
  audit, and, only when `tree_layout.sh` would recognise the tree root (`data/`,
  `reservations/` or `Completed reports/`), the tree's `registry/`, `comments/`, `metadata/`,
  `old_runs/` and `Completed reports/`. No path that contains the audit is ever denied. The
  auditors pass no extra denial, so their settings and every hash of them are unchanged.
- *Recovered repairs.* A focused repair adopted after a kill is validated against the
  `SCIENTIFIC_PROJECTION.json` its source attempt froze, exactly as a live repair is. If that file
  is missing or altered, the repair is rejected as an invalid sample and a fresh sample runs.
  This applies to verifiers only (`RoleTargets.freeze_recovered_repairs`); the auditors keep the
  2.6.3 path.
- *Admission holes closed.* Verdict evidence and manifest `source_bindings` must be an exact join
  of two strings to the snapshot (a file absent from the deposit with a null hash no longer passes).
  REPORT.md must have exactly one `## Hypotheses tested` section, with no verdict block outside
  it. A malformed JSON shape is an invalid sample (rule `result_json`) and is resampled, not a
  permanent failure. A command-line usage error exits 1.

Regression coverage lives in `tests/test_264_verifier_runtime.py`, which runs the real runner on
disk (the review findings in `VerifierReviewFindings264`, re-admission and the fixture helper in
`VerifierRecordsAreReadmitted`), and in the `VerifierPromptNamesValidatorRules` class of
`tests/test_prompt_contract.py`.

## Focused regression suite

```bash
pytest -q tests/test_run_phase1.py
```

The fake runner covers the global deadline with a hanging PDF extractor, zero-call source/path
preflight failure, probe failure/bounds, first-try
parallel fan-out, aggregate-evidence privacy, transient retry, authentication stop, focused repair, failed-promotion
preservation, cache replay, response/terminal crash recovery, metadata-2.1 pause/lifecycle events,
partitioned/unpartitioned/residual token attribution, every frozen repair surface, and two
concurrent audits.
