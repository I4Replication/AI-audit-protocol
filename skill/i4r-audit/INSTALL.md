# i4r-audit — install and validate

This Claude Code skill runs an I4R reproducibility audit from a published paper and replication
package. It produces the comment, verification notebook, claims handoff, and machine-readable run
record.

The canonical runtime policy is
[`config/pipeline-policy-v2.json`](config/pipeline-policy-v2.json). It specifies Sonnet 5/max for the
coordinator and all five phase-1 areas; GPT-5.6 Terra/high for ordinary external critics and the
cached model probe; and GPT-5.6 Terra/max for triggered targeted adjudication. The default audit uses
five Claude subagents, four round-1 critics, and two
dynamically routed round-2 critics. Add later-round specialists only when conclusion-changing or
disclosure-critical corrections span multiple domains.

## Install

Copy or symlink the complete `i4r-audit` directory into the Claude Code skills directory:

```bash
cp -R i4r-audit ~/.claude/skills/
chmod +x ~/.claude/skills/i4r-audit/scripts/*.sh
```

A team should prefer a version-controlled shared source with per-user symlinks so policy, scripts,
prompts, schemas, and references update together:

```bash
ln -s /path/to/versioned/i4r-audit ~/.claude/skills/i4r-audit
```

Restart Claude Code after installing. Invoke `/i4r-audit` or ask for an I4R reproducibility audit of
a paper and its package.

## Dependencies

| Needed for | Requirement |
|---|---|
| Core workflow | `bash`, Python 3 with `jsonschema`, `jq` |
| Claude coordination and auditors | Claude Code account with Sonnet 5 |
| External critics | Codex CLI; the bundled driver currently resolves it from the ChatGPT desktop app |
| Statistical/package inspection | Python readers required by the deposit; commonly pandas, scipy, statsmodels, openpyxl, pyreadstat |
| Verification notebook | R, `rmarkdown`, and format readers such as `haven`, `readxl`, `jsonlite`, or `jmvReadWrite` |
| DOCX/PDF build and visual gates | `pandoc`, LibreOffice, Poppler tools |

Nothing in the skill directly calls a hosted API. External critics use the authenticated Codex CLI
session and its available credits.

## Validate before the first audit and after every policy edit

From the installed skill directory:

```bash
python3 config/check_policy_drift.py
python3 -m unittest schemas.tests.test_v2_interfaces
python3 scripts/record_invocation.py --self-test
bash tests/test_run_critique.sh
bash scripts/init_run.sh --self-test
bash scripts/log_event.sh --self-test
bash scripts/finalize_run.sh --self-test
bash scripts/run_verification.sh --self-test
bash scripts/make_settled.sh --self-test
bash scripts/make_claims_script.sh --selftest
bash scripts/reserve_paper.sh --selftest
python3 -m unittest tests.test_post_delta_gates
```

The policy drift check verifies model routing, critic roles, target/cap, total retry budget, settled
timing and JSONL row contract, projection and release contracts, current report format, checklist
numbering, maintained-document links, and the 4,000-word limit on `SKILL.md`. It also rejects six
in-memory planted drifts. The other tests prove the metadata validator, claims handoff, and shared-tree
reservation controls can reject planted failures.

`work/findings.jsonl` is the single source for repeated finding artefacts. After a schema-valid update,
run `python3 scripts/generate_finding_artifacts.py "$AUDIT_DIR" --apply`; it regenerates the ledger,
Appendix A, comment finding projection, claims map, verification plan, and verification snippets.
After each delta round, `run_post_delta_gates.py` records a content-bound passing
`POST_DELTA_GATES.json` receipt for the
consistency, privacy, checklist, and archive gates; a missing, partial, failed, or stale receipt blocks
finalization.

If any test fails, do not start a production audit. Fix the installed copy and rerun the complete
set; a successful test in a different checkout does not validate the skill Claude will load.

## Default execution shape

| Stage | Model roles | Expected parallelism |
|---|---|---:|
| Coordinator | Sonnet 5 / max | 1 |
| Phase 1 | Five Sonnet 5/max auditors | 5 |
| Critique round 1 | Terra/high stats, data, fairness, completeness | 4 |
| Critique round 2 | Terra/high completeness plus routed specialist(s) | 2 by default |

Round 1 reports are merged deterministically. Maintained runs set `ADJUDICATE=auto`, so Terra/max is
called only for a conflicting verdict, a disputed conclusion-changing claim, a high-impact finding
lacking two independently bound evidence records, or a failed settled-fact spot check. With no
trigger there is no model synthesis/adjudication session. Later rounds route through a
`DIFF_MANIFEST` and always retain completeness. Build a changed round's manifest from explicit
pre/post report and registry files with `scripts/make_diff_packet.py`; its hashes bind the change
scope and its domain signals select one best specialist by default. It adds specialists only for
conclusion-changing or disclosure-critical changes spanning multiple domains. For example:

```bash
python3 scripts/make_diff_packet.py "$AUDIT_DIR" --round 2 \
  --before-report "$AUDIT_DIR/critique/round_1/REPORT_BEFORE_INTEGRATION.md" \
  --after-report "$AUDIT_DIR/report/REPORT_final.md" \
  --before-findings "$AUDIT_DIR/critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl" \
  --after-findings "$AUDIT_DIR/work/findings.jsonl"
```

The helper also emits a content-bound no-change packet for a byte-identical integration; it routes
the required fairness-plus-completeness fallback without fabricating changed claims. Settled facts are generated
immediately after round 1 and injected from round 2. Runtime reads one canonical
[`finding-row.schema.json`](schemas/v2/finding-row.schema.json) object per line from
`work/findings.jsonl`; `SETTLED.json` and `SETTLED.md` are generated outputs. Promotion requires
coordinator approval, two reviewers spanning two model families, distinct canonical invocation ids
and immutable reviewer-output path/hash records, rerunnable source hashes, a
dependency fingerprint, and exact canonical registry/current-report hashes. Two rounds are required;
target completion and later convergence have distinct meanings, and five is the hard cap.

For the coordinator and each phase-1 attempt, construct one canonical
`schemas/v2/invocation-record.schema.json` object and append it with
`python3 scripts/record_invocation.py "$AUDIT_DIR" <record.json>`. The helper rejects policy/model
drift, duplicate invocation ids, malformed usage, and completed outputs whose hashes do not match.
The critique driver records critic attempts automatically, so do not duplicate those rows.

At the required two-round endpoint, use `target_complete` only when every changed claim id received
subsequent review and no unresolved blocking or conclusion-critical item remains. If review continues,
use `converged` only when a later round returns no factual corrections; use `cap_with_unresolved` at
round 5, and `aborted` only for a genuine blocker or failed run. The `review_scope` log event records
changed, re-reviewed, unresolved, and unreviewed ids atomically before finalization.
Record `factual_corrections=0` on the final beyond-target `round_result` before using `converged`;
an omitted value cannot establish convergence.

Verification must run through `bash scripts/run_verification.sh "$AUDIT_DIR"`; it writes the
content-bound `verification_manifest.json` required by release staging. There is no
`ALLOW_NO_VERIFICATION` waiver. Publication contains the PDF, claims script, `run.json`,
`comment.json`, `finalization_receipt.json`, and `release-manifest.json`: five bound artefacts plus
the manifest, six files total.

## What each audit retains

| Artefact | Location |
|---|---|
| Comment source and built files | `report/REPORT_final.md` and `report/*.{docx,pdf}` |
| Five area reports | `report/01_reproduction.md` through `report/05_privacy_archive.md` |
| Ledger and adjudication | `report/LEDGER.md`, `report/ADJUDICATION.md` |
| Generated finding projections | `report/APPENDIX_A.md`, `claims_map.tsv`, `work/verification_plan.json` |
| Critic reports and responses | `critique/round_*/` |
| Verification | `verification.Rmd`, snippets, HTML, summary CSV, status file |
| Verification binding | `verification_manifest.json` |
| Claims handoff | `claims_script.<deposit-language-extension>` |
| Canonical finding rows and settled cache | `work/findings.jsonl`, `SETTLED.json`, `SETTLED.md` |
| Process and finding metadata | `run.json`, `comment.json`, `run.events.jsonl` |

Run audits in a durable programme directory, never a temporary scratchpad. Model identity, token
usage, human effort, failures, and phase times must be logged live; they cannot be reconstructed
reliably after the session or its logs are deleted.

The metadata pair is internal and defaults to `safe_to_publish: false`. Read its
`publication_redaction` block before releasing any extract outside I4R.
