# scripts/

Everything here ships in the skill package. Only the first group is part of the documented
audit pipeline; the others are recovery utilities and retained one-off history. When in doubt,
follow `SKILL.md`, which names every script an ordinary audit calls.

## Pipeline (called by SKILL.md, in order of use)

| Script | Stage |
|---|---|
| `setup_workspace.sh`, `reserve_paper.sh`, `init_run.sh`, `capture_environment.sh` | intake |
| `log_event.sh`, `lifecycle_journal.py`, `record_invocation.py` | live run metadata (their command lines refuse any lifecycle event of a post-fan-out verifier call, which only `run_verifier.py` journals; `record_invocation.py --correct <audit-dir> <invocation-id> <correction.json>` still works on one) |
| `run_phase1.py`, `phase1_runtime.py`, `email_scan.py` (linear-time e-mail matching) | five Phase-1 auditors (`--probe-only` is the live canary; `--recover-stranded` also clears a killed verifier attempt) |
| `build_draft_registry.py`, `build_safe_evidence.py`, `run_coordinator.py`, `init_comment.py` | ledger, adjudication, first draft, comment.json skeleton |
| `generate_finding_artifacts.py`, `project_draft_claim_tags.py`, `check_consistency.sh` | registry projections |
| `run_critique.sh`, `test_run_critique_runtime.py` (its runtime helper -- not a test, despite the name; `run_critique.sh`, `finalize_run.sh --self-test` and `validate_round_projections.py` all import it), `wait_for_round.sh`, `integrate_round.py`, `approve_settled.py`, `make_settled.sh`, `make_diff_packet.py`, `verify_diff_packet.py`, `validate_round_projections.py`, `run_post_delta_gates.py` | adversarial review rounds |
| `snapshot_evidence_rules.py` | 2.7.11: which files a critic's round snapshot carries beyond the cited evidence, frozen per audit in `critique.snapshot_evidence` (no leaf, every audit initialised before 2.7.11: the cited files only; `cited_plus_deposit_1`: also each row's verification snippet, its `_prelude.R` and the deposit's uncited files, within bounds and after the coordinator's provider-safety screens, the rest listed in `source/context/WITHHELD_FILES.json`); `test_run_critique_runtime.py` applies it, `config/check_policy_drift.py` pins the shipped value and `init_run.sh` prints it (INSTALL.md, *Critic snapshots and the critic sandbox*) |
| `run_verifier.py`, `verifier_contract.py` (the shared contract every verifier consumer imports: eligibility, identity, claim-hash binding, record admission, which ends in `run_verifier.readmit_record`) | post-fan-out verifier, `python3 scripts/run_verifier.py "$AUDIT_DIR" --findings <ids>`: the family-A confirmation of an `INCLUDED` row the operator, the coordinator or a critic raised after fan-out; run after a round reaches quorum and before integrating it, or before round 1 (INSTALL.md) |
| `build_document.py` (2.7.11: runs `build_docx.sh` unchanged and keeps what a failed build printed in `document_build_diagnostics/last_failure_output.txt`), `build_docx.sh`, `document_build_cache.py`, `document_release.py`, `make_verification_notebook.sh`, `run_verification.sh`, `make_claims_script.sh`, `check_claims_script.sh` (2.7.10: runs the claims script on its own, as a replicator does; `stage_release.sh` requires it) | deliverable and verification |
| `check_abstract_end.py` | 2.7.11: predicts from `REPORT_final.md`, with the locator's own tokenizer, whether the document build's page-1 abstract locator (frozen in `document_release.py`) will (exit 1) or may (exit 3) refuse the build; run at each integration, while the Abstract can still change |
| `amend_claims_script.py` | 2.7.10: rebuild a published audit's claims script with the current builder and republish `comments/` and `metadata/` through `publish_completed.sh` (a dry run that rehearses it on a scratch copy by default; `--rebuild-documents` for a comment older document scripts built; `--rollback-pending` after a run killed before its note, `--apply` again after one killed later, even while it republished; INSTALL.md, *Amending a published claims script*) |
| `stage_release.sh`, `check_release_privacy.py`, `check_deposit_values.py` (stage check 3c, run again by `publish_completed.sh`: no deposit value in the audit's own outputs), `release_record_masking.py` (stage check 3d: home paths and addresses masked in staged records nobody can reword), `finalize_run.sh`, `finalization_receipt.py`, `publish_completed.sh` | release and filing |
| `repin_critic_route.py` | journaled move of a live audit with no completed review to the shipped critic route; since 2.7.5 no other target |
| `check_critic_access.py` | 2.7.5: one minimal call checking that this host's Codex account can use the shipped critic route; `new_audit.sh` runs it before it reserves a paper (a pass cached in `~/.i4r/critic_access.json` for 24 h, keyed on the CLI, the login and a digest of the login store) |
| `amend_round_cap.py`, `reopen_audit.py`, `round_cap_amendment.py` | 2.6.9: raise an existing audit's frozen round cap (5 to 7) and reopen a finalized audit, each a journaled operator act with a reason; `round_cap_amendment.py` is the shared rule set and the check that finalize, `validate_round_projections.py` and `run_critique.sh` call (INSTALL.md, *Raising the round cap of an existing audit*) |
| `refreeze_pkg.py` | 2.7.11: gives `pkg/` back intake's read-only mode when the synced folder stripped it and every file still hashes to `package_manifest.json`, then runs the archive gate's own check and puts every mode back if it refuses; modes only, never journals. `stage_release.sh`, `finalize_run.sh` (before step 0c) and `publish_completed.sh` run it first, and the archive gate keeps the last word |
| `disclosure_policy.py` | the 2.6.8 report-not-withhold rule for deposit personal data, shared by `integrate_round.py` and `finalize_run.sh` |
| `claims_harness.py` | 2.7.11: the claims-script harness an audit's frozen snapshot names (`publication.claims_script_harness`): without the leaf `make_claims_script.sh` builds the 2.7.10 script byte for byte; `portable_1` adds the UTF-8 switch, the R version check, the declared programs outside R (with the versions the audit recorded) and the not-published mark on manual checks; any other value is refused, by the builder and by `init_run.sh` |
| `tree_layout.sh`, `new_audit.sh` | the tree layout (data/ runs/ comments/ metadata/) and the one-command intake built on it (2.7.11: `--loose include\|exclude` for an archive with entries beside it; the skill-version pin and Poppler are checked before the claim; INSTALL.md, *What intake checks before it reserves*) |
| `claude_routes.py` | 2.7.0: the Claude routes an audit's frozen snapshot may name (Sonnet 5/max, spawned as `--model sonnet`, for audits initialised before 2.7.0; Sonnet 5.5, spawned as `--model claude-sonnet-5-5`, at max for audits initialised on 2.7.0 to 2.7.2 and, since 2.7.3, at xhigh for new ones), the served-model check for each, and the spawn environment without `REMAPPING_ENV`. `run_phase1.py`, `run_coordinator.py`, `run_verifier.py`, the recovery tools and `init_run.sh` (which refuses a leftover snapshot on another route) import it; `config/check_policy_drift.py` pins the shipped policy to its `SHIPPED`. The post-delta receipt's code files never import it |
| `probe_bounds.py` | 2.7.1: the Phase-1 capability-probe token bounds an audit's frozen snapshot may name (`phase_1.runtime.probe_max_normalized_tokens`: 150,000 for audits initialised before 2.7.1, 350,000 for new ones). The bound is part of the probe contract, so every audit keeps its own; `run_phase1.py` (and through it `run_verifier.py`) admits only these and never defaults to the shipped one; `config/check_policy_drift.py` pins the shipped policy to its `SHIPPED_PROBE_BOUND`; `init_run.sh` prints the bound a new audit's snapshot froze and warns when a reused leftover's is not the shipped one. Stdlib only; the post-delta receipt's code files never import it |
| `claude_thinking_tokens.py` | 2.7.11: whether an audit records the Claude CLI's thinking count (`modelUsage.<id>.thinkingTokens`) as an invocation's `reasoning_tokens`, frozen in the snapshot leaf `metadata.claude_reasoning_tokens`. An audit initialised before 2.7.11 froze no leaf and keeps the 2.7.10 parse (null) for its whole life; `run_coordinator.py`, `run_phase1.py` (and through it `run_verifier.py`) and the two coordinator recovery tools read it from the audit's own snapshot, never from `config/`. Stdlib only; the post-delta receipt's code files never import it |
| `codex_cli.py` | 2.6.10: finds the Codex CLI every critique script runs: `CODEX`, then `I4R_CODEX_BIN`, else the newest working CLI among the ChatGPT app's bundled copies and every `codex` on `PATH` (`--explain` lists them) |
| `summarize_invocations.py`, `claude_model_usage.py`, `benchmark_optimization.py`, `build_package.py`, `run_meta.sh`, `verify_codex_model.sh`, `validate_json_schema.py`, `lock_wait.py` (exclusive locks that say so when they wait; also copied into a staged release's `scripts/`), `later_disputes.py` (reviewers disputing a finding in an unintegrated round, shared by `make_settled.sh` and `finalize_run.sh`) | reporting, packaging, helpers |

## Recovery utilities (not part of an ordinary run)

Each is a narrow, receipt-producing repair for one documented failure mode. Read the module
docstring before using one; none of them makes a provider call unless it says so.

Current: `recover_coordinator.py` · `recover_misplaced_coordinator_outputs.py` ·
`migrate_metadata_2_1.py`

2.7.11: `run_coordinator.py` restores a bookkeeping field the model mistyped when its canonical value is
determined (a verification's evidence sha256, an unbound draft-registry prose citation, a null
`environment_fingerprint`, a dependency id given by its unique prefix) and lists every change in the attempt's
`bookkeeping_normalization.json`. `recover_misplaced_coordinator_outputs.py` does the same and names that record
in its receipt; `recover_coordinator.py` re-judges the bytes the runner stored and never rewrites them.

**Stale — do not run without re-deriving their pins first:** `recover_critique_probe.py`,
`recover_round1_projection.py`, `retry_completeness_once.py`,
`retry_targeted_adjudication_once.py`, `repair_phase1_all_model_usage.py`,
`migrate_phase1_provider_accounting.py`. Each pins a hash, a version string or a directory layout
the driver has since moved past — `retry_targeted_adjudication_once.py` still expects
`i4r-target-snapshot-2` against the current `-4`; `recover_critique_probe.py` expects a probe
prompt, response shape and layout the driver no longer uses; the two Phase-1 accounting utilities
look for `work/a*_*/invocations/attempt_*/invocation_record.json`, a layout the current driver
never writes (attempts live at `phase1/roles/<role>/<input_sha256>/attempt_NN_<uuid>/`, and a focused
output repair beside them at `repair_<uuid>/`), and
`migrate_phase1_provider_accounting.py` additionally cannot identify a primary served model on a
current ledger. They fail in ways that read like a corrupt audit rather than a stale script, which
is worse than not having them. Listing a stale script as working was itself the defect; re-derive
the pins, or delete the script, before relying on one. This repository omits
`migrate_phase1_provider_accounting.py`, which quotes one audit's report figures; the released package keeps it.

## Retained history (audit-specific, not reusable)

A round-1 integration script and `write_round2_wrapup_once.py` were written for one audit and are
kept as a record of that run. They are not templates for other papers. This repository omits
the round-1 integration script, which embeds that audit's draft comment text; the released package keeps it.

## Misplaced test

`test_document_build_cache.py` is a test module that lives here because INSTALL.md invokes it by
this path. It is not a pipeline step. (`test_run_critique_runtime.py` is NOT a test: it is the
critique driver's mandatory runtime helper, listed in the pipeline table above. Do not move it or
exclude it by a `test_*` rule -- the critique phase refuses to start without it.)
