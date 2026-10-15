# i4r-audit: detailed guide

For a quick start, see the [README](README.md). This guide covers the protocol for AI-assisted
reproducibility auditing, `i4r-audit` (the Claude Code skill that implements it), and `baseline` (a
single-agent skill to compare it against) in detail.

`i4r-audit` runs a full Institute for Replication (I4R) reproducibility audit of a **published** paper and
its replication package. It produces a publishable comment, an R verification notebook, a claims script for
the human verifier, and a process record. It reports checks that passed and hypotheses it refuted as
carefully as the defects it confirms. It is not for peer review of unpublished manuscripts, for literature
reviews, or for papers with no replication materials.

Skill versions: **i4r-audit 2.7.7** and **baseline 1.1.0**.

<sub>Sources: `skill/i4r-audit/SKILL.md` frontmatter and lines 8-10; `skill/i4r-audit/VERSION`; `skill/baseline/VERSION`.</sub>

**Contents:** [1 Repository contents](#1-repository-contents) · [2 How an audit works](#2-how-an-audit-works) ·
[3 Requirements and installation](#3-requirements-and-installation) · [4 Running an audit](#4-running-an-audit) ·
[5 Outputs](#5-outputs) · [6 Findings scheme](#6-findings-scheme) ·
[7 Safety, privacy and release gates](#7-safety-privacy-and-release-gates) ·
[8 Code map, schemas and tests](#8-code-map-schemas-and-tests) · [9 The baseline skill](#9-the-baseline-skill) ·
[10 Version history](#10-version-history) · [11 Status and how to cite](#11-status-and-how-to-cite)

Where this README and the skill's own files disagree, the skill wins. `config/pipeline-policy-v2.json` is
authoritative for models, roles and stopping rules, and `INSTALL.md`, `config/` and the scripts win over the
cheat sheet (which itself says `INSTALL.md` wins).

## 1. Repository contents

| Path | What it is |
|---|---|
| `skill/i4r-audit/` | The audit skill. Five parallel auditors feed an adjudication with a dispositioned ledger. A second model family then runs adversarial critique rounds. The audit ends in a gated release with a verification notebook and a numbered claims script |
| `skill/baseline/` | The baseline. One Claude call per paper audits it and writes the whole comment, from the same inputs the pipeline's auditors received. It has no critics and no rounds ([§9](#9-the-baseline-skill)) |
| `CHEATSHEET.html`, `CHEATSHEET.pdf` | The operator's cheat sheet, byte-identical to the copies in `skill/i4r-audit/`. Its header still reads 2.6.2, but its install step and models are 2.7.x |
| `skill/MERGE_NOTES.md` | History only. The decision table from unifying two development branches (2026-08-21), with two addenda. Its model and round settings are out of date |

Inside the skill, [`SKILL.md`](skill/i4r-audit/SKILL.md) is the run book;
[`INSTALL.md`](skill/i4r-audit/INSTALL.md) covers installation, validation, dependencies, environment variables
and per-version upgrade notes; [`CHANGELOG.md`](skill/i4r-audit/CHANGELOG.md) covers 2.2.0 to 2.7.7.

<sub>Sources: current `README.md` Contents; `cmp` of both cheat-sheet copies; `CHEATSHEET.html` title block; `skill/MERGE_NOTES.md`.</sub>

## 2. How an audit works

**The design in one paragraph.** Five auditors, one question each, no view of each other; a coordinator who
settles disagreements by re-running the disputed computation, never by preferring an auditor; a ledger that
dispositions every finding as included, merged, or excluded with a reason; critique rounds whose declared
target is the audit rather than the paper; and release gates whose detectors must fire on a known positive
before any clean result is believed. A finished audit hands its verifier a claims script: every finding
numbered, severity-labelled, and paired with the code that checks it, in the deposited package's own language.

```text
data/<Journal>/<Paper>/     article PDF + replication package (inputs; never written)
   |
1  INTAKE       new_audit.sh -> check_critic_access.py -> setup_workspace.sh -> init_run.sh
   |            reservation, read-only pkg/ copy, run.json, frozen pipeline-policy.snapshot.json
2  PHASE 1      run_phase1.py: snapshot -> deterministic evidence -> smoke test -> capability probe
   |            -> five Sonnet 5.5 auditors in parallel, blind to each other -> admission -> quorum
3  COORDINATOR  build_draft_registry.py, build_safe_evidence.py -> run_coordinator.py
   |            ledger (work/findings.jsonl), ADJUDICATION.md, REPORT_v1.md; renumber to F1..FN
4  CRITIQUE     round 1: four GPT-6.1 Sol critics -> deterministic merge -> adjudicator on triggers only
   |            -> post-fan-out verifier when needed -> integrate_round.py -> settled facts
   |            round 2+: diff packet -> completeness + one routed specialist, after post-delta gates
   |            stop: target_complete | converged | cap_with_unresolved | aborted
5  RELEASE      build_docx.sh -> verification notebook -> claims script -> stage_release.sh
                -> finalize_run.sh (registry) -> publish_completed.sh -> comments/ and metadata/
```

The skill runs unattended from intake to publish. Gates still fail closed and round grants are still
journaled, and the run stops only for a gate failure or a real blocker. Since 2.3.0 the critique driver
authorizes its own round plan, as long as the plan is unmodified. `I4R_AUTO_AUTHORIZE=0` brings back the
attended pause.

### Roles and models

`config/pipeline-policy-v2.json` is authoritative for models, roles, round routing, retry limits and stopping
rules. These are the routes for an audit started on 2.7.7:

| Role | Default count | Model (spawned id) | Effort | Via |
|---|---|---|---|---|
| Coordinator | 1 | Sonnet 5.5 (`claude-sonnet-5-5`) | `xhigh` | Claude Code CLI |
| Phase-1 auditors | 5 | Sonnet 5.5 | `xhigh` | Claude Code CLI |
| Phase-1 capability probe | 1 on a cold start | Sonnet 5.5 | `xhigh` | Claude Code CLI |
| Post-fan-out verifier | 0 (only when needed) | Sonnet 5.5 | `xhigh` | Claude Code CLI |
| Critics `stats`, `data`, `fairness`, `completeness` | 4 in round 1, 2 in round 2 | GPT-6.1 Sol (`gpt-6.1-sol`) | `high` | Codex CLI |
| Critique model probe | 1 on a cold start | GPT-6.1 Sol | `high` | Codex CLI |
| Targeted adjudicator (`critic_synth`) | 0 (only on policy triggers) | GPT-6.1 Sol | `max` | Codex CLI |

- **Invocation counts.** A default audit makes 12 invocations, or 14 on a cold start. Three attempts per
  critic role bound the two-round critique path at 19 invocations. One triggered adjudicator round raises
  that bound to 22.
- **Independence.** Critics must come from a different model family than the coordinator. Each call records
  the model id the provider actually served, not the alias that was requested.
- **Frozen routes.** `init_run.sh` freezes models, efforts, round cap, probe bound and timeouts into the
  audit's `pipeline-policy.snapshot.json`, and they stay fixed for the audit's life. Older audits keep older
  routes: Sonnet 5 at `max` before 2.7.0, Sonnet 5.5 at `max` on 2.7.0-2.7.2, and GPT-5.6 Terra or GPT-6 Sol
  critics before 2.7.0. `CRITIC_MODEL`, `CRITIC_EFFORT` and `ADJUDICATOR_*` are not controls. A value that
  differs from the snapshot makes the run fail early.
- **Your session.** The interactive Claude Code session is not a policy role and is not route-checked. Pick
  Sonnet 5.5 with `/model`; that setting governs only the interactive coordinator session.

### Phase 1: five blind area reviews

| Area | Report | Checklist items |
|---|---|---|
| Reproduction | `report/01_reproduction.md` | 3, 8, 9, 10, 11 |
| Data integrity | `report/02_data_integrity.md` | 1, 2, 4, 6 |
| Text-vs-data consistency | `report/03_text_vs_data.md` | 3, 5, 9, 12 |
| Code and methods | `report/04_code_methods.md` | 4, 8, 10, 11 |
| Privacy and archive hygiene | `report/05_privacy_archive.md` | 7, 10, 12 |

The 12 items in `reference/checklist.md` are: (1) duplication and sample inflation, (2) impossible values,
(3) reproduction discrepancies, (4) scoring and coding errors, (5) statistical-reporting anomalies,
(6) fabrication signatures, (7) privacy and archive hygiene, (8) estimator specification and library defaults,
(9) figure fidelity, (10) mechanical reproducibility, (11) treatment fidelity, (12) reporting omissions. One
rule covers all of them: verify the remedy, not just the defect.

Each auditor sees only `paper/`, `pkg/`, its role packet and its own output folder. It verifies against the
raw deposit and writes a rerunnable script for every quantitative claim. Each finding gets a label
(CONFIRMED, LIKELY or SPECULATIVE), an impact (`none` is common and valid) and the benign explanations that
were tested.

A role's output is admitted only after a clean exit, exactly one observed primary model on the route,
complete usage, schema validation, exact source-hash joins and a closed evidence manifest. A sandbox
permission denial is recorded in the attempt's `sandbox_denials.json` and does not fail the role. The one
exception: after a schema-invalid output, a denial of a command the sandbox's own allowlist grants stops the
retries as an environment fault. Each call runs without network access or plugins, against a read-only
package, with a 90-minute (5,400 s) timeout and a USD 15 cap.

### Coordinator, ledger and adjudication

Every finding and tested negative from the five reports becomes one row in `work/findings.jsonl`. Each row is
marked `INCLUDED`, `MERGED` (with a target), or `EXCLUDED` (with a reason that would survive outside review).
Three kinds of decision need a second check: a claim that would change a conclusion, a disagreement between
coordinator and auditor, and a rejected criticism. For these the coordinator verifies by a second,
*structurally different* route, such as running the author's code against a direct calculation. A new random
seed does not count. The coordinator never decides by model prestige or by majority vote. Each decision
becomes an `A<n>` entry in `report/ADJUDICATION.md`, and withdrawn entries stay in the file.

### Rounds and convergence

- **Round 1.** The four critics review the same frozen draft in parallel. Each claim gets a schema-checked
  verdict (CONFIRMED, REFUTED, OVERSTATED or UNVERIFIABLE), marked BLOCKING or NON_BLOCKING. A deterministic
  merge follows. The Sol/`max` adjudicator is called only on a trigger: conflicting verdicts, a disputed
  conclusion-changing claim, a high-impact finding without two independently bound evidence records, or a
  failed settled-fact spot check.
- **Settled facts.** Promotion needs coordinator approval and two independent confirmations, one from each
  model family. The Claude confirmation is the Phase-1 auditor that found the issue; the GPT confirmation is a
  critic. Critics see the settled facts from round 2 on.
- **Post-fan-out verifier.** A finding first raised after Phase 1 can get its Claude-family confirmation only
  from `run_verifier.py`. This is one blind Sonnet call: it sees the claim but not the verdicts, the reasoning
  or the finding ids. The coordinator never reviews its own findings.
- **Round 2 and later.** A content-bound diff packet sets the scope. `completeness` always runs, plus by
  default the single best-matching specialist (`fairness`, `stats` or `data`). More specialists are added
  only when conclusion-changing or disclosure-critical changes span multiple domains. Four post-delta gates
  must pass first.
- **Target and cap.** The target is 2 rounds. Round 2 is mandatory because corrections made after round N
  have not been reviewed by round N. The hard cap for new audits is 7 rounds. Nothing loops rounds
  automatically.
- **Stopping values.**
  - `target_complete`: two rounds are done, no blocking item is unresolved, and every last-round OVERSTATED or
    REFUTED verdict is contested on record.
  - `converged`: the audit went past the target, and a round returned zero uncontested factual corrections.
  - `cap_with_unresolved`: the audit hit its cap; every open item is listed.
  - `aborted`: a genuine blocker.

  Never call the two-round finish "converged".

**What the human verifier receives.** The verifier did not run the audit and is not expected to trust it.
They get the comment, the R verification notebook (with its HTML, summary CSV and status file), the claims
script, and `report/LEDGER.md` and `report/ADJUDICATION.md` as the full evidentiary record. A replicator
verifies every claim by hand, and the coordinator reads every comment before submission. [§5](#5-outputs)
explains how to use these files.

<sub>Sources: `SKILL.md` lines 8-45, 149-453; `config/pipeline-policy-v2.json` `models`, `phase_1`, `critique`, `default_role_counts`; `reference/checklist.md` "Role routing"; `reference/brief_template.md`; `reference/phase1_runtime.md`; `scripts/run_phase1.py` (`execution_errors`, `allowlisted_denials`); `reference/adjudication_guide.md` "Round 1", "Settled facts after round 1", "Stopping"; `INSTALL.md` lines 7-17; `reference/report_structure.md` intro.</sub>

## 3. Requirements and installation

| Requirement | Version and account | Used for |
|---|---|---|
| Claude Code CLI | 2.1.285 or later. 2.1.281 does not recognise `claude-sonnet-5-5` and serves a 200,000-token window. The account needs Sonnet 5.5, through a subscription login or `ANTHROPIC_API_KEY` | Coordinator, auditors, probe, verifier |
| Codex CLI | 0.159.0 or later; it ships inside the ChatGPT desktop app. The account must offer `gpt-6.1-sol`. Some ChatGPT accounts are refused. An API-key login works if the key's organisation offers the model | Critics, critique probe, adjudicator |
| bash, Python 3, jq | `init_run.sh` refuses to start without `jq` or `python3`. `jsonschema` is needed only for the full test suite. `openpyxl` is optional; without it, workbooks are marked unscannable | Core workflow |
| Python readers | Whatever the deposit needs; commonly `pandas`, `scipy`, `statsmodels`, `openpyxl`, `pyreadstat` | Package inspection |
| R | 4.4 or later (cheat sheet), with `rmarkdown` and `knitr`. Add `haven`, `readxl`, `jsonlite` and `jmvReadWrite` as needed. Without R (`Rscript`), check 3c lists SPSS, Stata, SAS and R data files as not scanned; without `haven`, only the SPSS, Stata and SAS files | Verification notebook, check 3c |
| pandoc | Lookup order: `PANDOC`, then `PATH`, then the `i4rdoc` env, then `/opt/homebrew/bin` and `/usr/local/bin`. R must also find it on its own | DOCX build |
| LibreOffice (`soffice`) | Required; no substitute renderer is accepted | PDF rendering |
| Poppler (`pdfinfo`, `pdftotext`) | 21.06 or later. The privacy gate calls `pdfinfo -custom`, which older versions reject | Page counts, privacy gate |

- **No direct API calls.** No skill script calls a hosted API. The critics run on the signed-in Codex CLI and
  its credits. With no credits there is no critique, and the critique model probe catches that before any
  spending.
- **Platforms.** The docs are written for macOS first. `codex_cli.py` and `capture_environment.sh` also handle
  Linux and WSL. *(Inferred: there is no formal supported-OS statement.)*
- **Old Poppler.** If the system Poppler is too old, put a conda env first on `PATH` in every shell:

```bash
conda create -n i4rdoc -c conda-forge pandoc poppler
export PATH="$HOME/miniconda3/envs/i4rdoc/bin:$HOME/.local/bin:$PATH"
pdfinfo -v          # must print 21.06 or newer
```

Appendix A widths are sized for Liberation Serif at 12 pt on A4. A machine with a wider default font can fail
the `findings_table_integrity` build stage, so build on the canonical machine.

### Installing the skill

```bash
mkdir -p ~/.claude/skills            # create it first, or cp -R builds it from the skill's contents
cp -R skill/i4r-audit ~/.claude/skills/
chmod +x ~/.claude/skills/i4r-audit/scripts/*.sh
```

Restart Claude Code, then invoke `/i4r-audit` or ask for an I4R reproducibility audit.

- **Updating.** Move the old copy *out of* `~/.claude/skills` before copying in the new one; Claude Code could
  load a copy left beside it.
- **Teams.** Prefer a version-controlled source with per-user symlinks
  (`ln -s /path/to/versioned/i4r-audit ~/.claude/skills/i4r-audit`). Every Mac holds its own copy, so a new
  shared zip reaches nobody until they reinstall.
- **Version check.** `init_run.sh` refuses a skill copy whose `VERSION` differs from the tree's canonical copy
  at `<tree>/skill/i4r-audit-unified/i4r-audit/VERSION`. `I4R_CANONICAL_SKILL_DIR` changes that location.
  `I4R_ALLOW_SKILL_VERSION_MISMATCH=1` turns the refusal into a warning; do not use it to start new audits.

To install from the team's shared zip (cheat sheet, step 2):

```bash
mkdir -p ~/.claude/skills && cd "$(mktemp -d)"
unzip -q "<discipline-root>/skill/i4r-audit-unified/i4r-audit.zip"
[ ! -e ~/.claude/skills/i4r-audit ] || mv ~/.claude/skills/i4r-audit ~/i4r-audit-old-$(date +%Y%m%d%H%M)
cp -R i4r-audit ~/.claude/skills/ && cd ~/.claude/skills/i4r-audit && chmod +x scripts/*.sh
cat VERSION         # expect 2.7.7, then restart Claude Code
```

The zip is built with `python3 scripts/build_package.py --output ../i4r-audit.zip` and checked by running the
same command again with `--check`. The build is deterministic and privacy-scanned.

### Verifying the install

Run these from the installed skill folder before the first audit and after every policy edit. If anything
fails, do not start a production audit. A pass in another checkout does not validate the copy that Claude
loads.

```bash
cd ~/.claude/skills/i4r-audit
python3 config/check_policy_drift.py                 # prints POLICY DRIFT CHECK OK: ...
python3 -m unittest schemas.tests.test_v2_interfaces
python3 -m unittest discover -s tests -p 'test_*.py'
bash tests/test_run_critique.sh
python3 scripts/record_invocation.py --self-test
python3 scripts/test_document_build_cache.py
for s in init_run log_event finalize_run run_verification make_settled; do bash scripts/$s.sh --self-test; done
bash scripts/make_claims_script.sh --selftest        # --selftest, no hyphen
bash scripts/reserve_paper.sh --selftest             # --selftest, no hyphen
python3 -m unittest tests.test_post_delta_gates
```

The suite runs against fake providers, so it cannot catch credential, routing or sandbox problems. These live
checks can:

```bash
python3 scripts/codex_cli.py --explain        # which Codex CLI will be used; no provider call
python3 scripts/check_critic_access.py        # one minimal Sol call; a pass is cached 24 h in ~/.i4r/
claude -p --model claude-sonnet-5-5 --output-format json "Reply OK"   # paid; expect contextWindow 1000000
python3 scripts/run_phase1.py "$AUDIT_DIR" --probe-only               # after init: one short Sonnet call
```

`check_critic_access.py` exits 0 when usable, 3 when the account cannot use the model, 4 on a network,
rate-limit or timeout problem (retry), 5 when the CLI rejected the call (update it), and 2 when there is no
Codex CLI or the policy is unreadable. Moving the Codex CLI to a different account is the user's decision;
the operator agent reports the problem and stops.

### Configuration

`config/pipeline-policy-v2.json` (policy id `i4r-audit-pipeline-v2`) is the runtime policy. `init_run.sh`
freezes it into each audit, so a change reaches only new audits; a frozen snapshot is never edited. Change the
round target and cap in the policy and `config/check_policy_drift.py` together, never per run. Never fix a
disagreement by editing the generated `PIPELINE-POLICY-V2:START/END` block in `SKILL.md`. The drift check pins
the shipped values, joins them to the scripts that read them, and runs planted-drift negative controls.

<sub>Sources: `INSTALL.md` "Install", "Dependencies", "Live canary before the first audit", "Validate before the first audit and after every policy edit", "Upgrading in-flight audits to 2.7.0/2.7.2/2.7.5", "Appendix A rendering and document manifests"; `CHEATSHEET.html` steps 1-2; `scripts/check_critic_access.py`; `scripts/codex_cli.py`; `scripts/init_run.sh` lines 141-170; `scripts/check_deposit_values.py` (`R_HAVEN_KINDS`, `R_BASE_KINDS`, `read_with_r`); `scripts/build_docx.sh` lines 125-136; `config/check_policy_drift.py`.</sub>

## 4. Running an audit

### The shared tree

| Path | Contents |
|---|---|
| `data/<Journal>/<Paper>/` | Article PDF and replication package. Inputs only; never written |
| `runs/<Journal>/<Paper>/audit/` | The working audit. Release staging sits beside it, e.g. `../release` |
| `comments/<Journal>/<Paper>/` | Published comment PDF, plus the DOCX when the manifest binds one |
| `metadata/<Journal>/<Paper>/` | Published record: six files ([§5](#5-outputs)) |
| `registry/` | Programme registry: `index.jsonl` and one folder per audit |
| `reservations/` | Advisory claims, so a team can share one synced tree |
| `skill/` | Canonical skill copy, by default `skill/i4r-audit-unified/i4r-audit` |
| `old_runs/` | Everything before 2026-09-22, plus superseded publications |

`scripts/tree_layout.sh` is the one place that knows this layout.

- **No glob characters in audit paths.** An audit path must not contain `? * [ ] { } \`, because the Claude
  CLI reads sandbox paths as globs. `new_audit.sh` drops these characters from the `runs/` name.
- **Run in the durable tree only.** Never run an audit in a scratchpad or `/tmp`: usage is logged live and
  cannot be rebuilt later.
- **Sync clients.** A sync client can strip `pkg/`'s read-only mode. Restore it with
  `chmod -R a-w "$AUDIT_DIR/pkg"`.

### Starting an audit

1. Open a terminal at the **tree root**, never in a paper folder. Claude Code writes `.claude/` where it
   starts.
2. Export the `i4rdoc` `PATH`.
3. Start `claude` and pick Sonnet 5.5 at `xhigh` with `/model`.
4. State the goal as a deliverable. For example: audit `data/<Journal>/<Paper>` end to end, starting with
   `new_audit.sh`. Done means finalize filed to the tree registry, and publish filed the PDF and DOCX to
   `comments/` and six record files to `metadata/`.

The session then runs the stages below. `SKILL.md` names every script an ordinary audit calls.

**Intake.** Run from the tree root, calling scripts by the skill's path:

```bash
bash scripts/new_audit.sh "data/<Journal>/<Paper>"     # prints the AUDIT_DIR export and the init command
export AUDIT_DIR=...
bash scripts/init_run.sh "$AUDIT_DIR" --doi <doi> --paper-title <title> --round-target 2 --round-cap 7 \
  --auditor-model sonnet-5-5 --auditor-family claude --auditor-provider anthropic
export RUN_JSON="$AUDIT_DIR/run.json"
chmod -R a-w "$AUDIT_DIR/pkg"
```

`new_audit.sh` refuses, reserving and copying nothing, when the paper folder is ambiguous (pass `--paper` or
`--package`), the package is empty, an audit of the paper already exists (resume it, or move it under
`old_runs/`), or the host's Codex account cannot use `gpt-6.1-sol`. Before reserving the paper,
`setup_workspace.sh` also refuses corrupt archives and sync-conflicted copies.

**Phase 1, coordinator, renumbering:**

```bash
python3 scripts/run_phase1.py "$AUDIT_DIR"
python3 scripts/build_draft_registry.py "$AUDIT_DIR"
python3 scripts/build_safe_evidence.py "$AUDIT_DIR"
# write PAPER.txt (mandatory), ENV.txt, SLOW.txt and WRAPUP.txt at the audit root
python3 scripts/run_coordinator.py "$AUDIT_DIR" --safe-evidence-manifest "$AUDIT_DIR/work/safe_evidence/manifest.json"
python3 scripts/init_comment.py "$AUDIT_DIR" --facts work/comment_facts.json
python3 scripts/renumber_findings.py "$AUDIT_DIR" --apply --also report/ADJUDICATION.md   # --dry-run first
```

Renumber once, before round 1. Critic verdicts join by finding id, so renumbering after a round orphans that
round's verdicts.

**Critique.** Start round 1 with
`CRITICS="stats data fairness completeness" ADJUDICATE=auto bash scripts/run_critique.sh 1`. The coordinator
then integrates it in six steps, strictly in this order. `SKILL.md` §4 has the exact block.

1. Copy `REPORT_final.md` and `findings.jsonl` into `critique/round_1/` as the `*_BEFORE_INTEGRATION` files.
2. Run `integrate_round.py "$AUDIT_DIR" --round 1 plan`, then
   `apply --decisions "$AUDIT_DIR/work/round1_decisions.json"`. Apply your own decisions, never the plan file.
3. Edit the prose and `retired_claims.txt`. Then run `approve_settled.py` followed by
   `generate_finding_artifacts.py --apply`, and run that pair twice.
4. Run `check_consistency.sh`, then log its `gate` event and a `round_result` event.
5. Run `make_settled.sh`, log the `settled` event, then build the round-2 packet with `make_diff_packet.py`.
6. Update `WRAPUP.txt`. Prepare round 2 with
   `DIFF_MANIFEST=... I4R_AUTO_AUTHORIZE=0 bash scripts/run_critique.sh 2`, which creates the round folder and
   exits 3. Then run `run_post_delta_gates.py --round 2`, then
   `DIFF_MANIFEST=... ADJUDICATE=auto bash scripts/run_critique.sh 2`.

For findings raised after Phase 1, run `run_verifier.py "$AUDIT_DIR" --findings <ids>` after the round
reaches quorum and before integrating it. A final round that returns no factual correction and ends the audit
is not integrated. Its verdicts and gate event are still logged.

**Release:**

```bash
python3 scripts/generate_finding_artifacts.py "$AUDIT_DIR" --apply
bash scripts/build_docx.sh "$AUDIT_DIR" <output-name>      # needs report/front_matter.md, report/captions.txt
bash scripts/make_verification_notebook.sh "$AUDIT_DIR"
bash scripts/run_verification.sh "$AUDIT_DIR"
bash scripts/make_claims_script.sh "$AUDIT_DIR"
cp "$AUDIT_DIR/report/REPORT_final.md" "$AUDIT_DIR/report/REPORT_v<N+1>.md"
bash scripts/stage_release.sh "$AUDIT_DIR" <clean-release-dir>    # must not exist yet
bash scripts/log_event.sh delivery artifact_path=report/<output-name>.pdf recipient='I4R editorial queue'
bash scripts/finalize_run.sh "$AUDIT_DIR" --stopping-reason "$AUDIT_STOP_REASON" --registry "<tree-root>/registry"
python3 scripts/finalization_receipt.py check "$AUDIT_DIR"
bash scripts/publish_completed.sh "$AUDIT_DIR"
```

**The journal.** Log events as they happen with `bash scripts/log_event.sh <type> key=value ...`. That covers
phase boundaries, models, failures, round results, gate exits, human effort and delivery. Unavailable usage is
recorded as `null`, never estimated. Before finalize, the journal needs nine events (`phase_start` and
`phase_end` for intake, adjudication, critique and release, plus one `delivery` event). An audit that skips
them still files, but with `compliance_pass: false`. Also log a `human_effort` event. It is not required, but
with no minutes recorded finalize warns that the AI-assisted vs human-only comparison has no numerator for the
audit; `notes='unattended run'` makes that warning record the reason. Record the stopping evidence with a
`review_scope` event; `target_complete` and `converged` need its unresolved-item and unreviewed-section lists
to be empty.

### Monitoring

```bash
python3 scripts/status.py <tree-root>            # one line per audit; read-only, takes no lock
python3 scripts/status.py <tree-root> --stuck    # only audits needing attention; exits 1 if any
bash scripts/reserve_paper.sh list <tree-root>   # reservations; `reap` is a dry run unless --force
bash scripts/wait_for_round.sh "$AUDIT_DIR" N --pid $!   # waits on a background critique round
bash scripts/run_meta.sh report "$AUDIT_DIR"     # tokens, latency and cache totals
```

`wait_for_round.sh` checks the round's machine-readable end state; it never infers success from log text. In
driver output, `[wait ] ... retrying` is normal, `[fail ]` is a permanent failure, and `[stop ]` means no
provider call was made. Failure details are in
`critique/round_N/invocations/<id>/attempts/<role>/<attempt>/failure.log`.

### Resuming and recovering

- **A critique round.** Rerun the same `run_critique.sh N`. Completed roles replay from cache and attempt
  budgets persist. Add `MAX_TRIES=<n>` (at most 20) if a role has used its attempts. `I4R_FORCE_RERUN=1`
  ignores the cache, but only before integration.
- **A killed Phase 1** (sleep, reboot, Ctrl-C). Run
  `python3 scripts/run_phase1.py "$AUDIT_DIR" --recover-stranded`. A strand whose output workspace is empty
  is closed without charge. Since 2.7.2, one where the model had already written files is charged one attempt
  and recorded as started.
- **A Phase-1 role out of attempts.** When environment or harness problems spent the attempts, re-run with a
  higher ceiling, `I4R_MAX_ATTEMPTS=<n>` (at most 20); accepted roles are reused. The supported full reset is
  `init_run.sh --force`, passing the audit's `--audit-id`, `--doi` and `--paper-title` again. It archives the
  journal and `phase1/`, freezes the shipped policy again (so an older audit moves to the current routes) and
  re-runs all five roles.
- **The journal.** `python3 scripts/lifecycle_journal.py recover "$AUDIT_DIR"` checks the hash chain, rebuilds
  the projections and lists incomplete invocations. It makes no provider call.
- **Prevention.** Use `caffeinate -i` for overnight runs. Do not run the recovery scripts marked stale in
  [§8](#8-code-map-schemas-and-tests).

### Publishing and corrections

`publish_completed.sh` never overwrites a filed set, and it releases the paper's reservation.

To correct a published comment, move both published folders under
`old_runs/<Journal>/<Paper>.published-<UTC stamp>/`, re-claim the paper, run
`reopen_audit.py "$AUDIT_DIR" --reason "<why>"`, run the rounds you need, rebuild, restage, re-log delivery,
finalize with `I4R_REFINALIZE_PUBLISHED=1`, and publish. A re-file that changes only Appendix A needs no
reopen. Never edit the comment by hand: say what is wrong and let the session regenerate it.

### Key environment variables

| Variable | Effect |
|---|---|
| `I4R_AUTO_AUTHORIZE` | `1` (default) runs rounds unattended. `0` pauses: the driver exits 3, and you authorize with `lifecycle_journal.py authorize-round` |
| `MAX_TRIES` / `I4R_MAX_ATTEMPTS` | Attempt ceilings, 1-20, default 3. `MAX_TRIES` covers critique rounds; `I4R_MAX_ATTEMPTS` covers Phase 1, the coordinator, the verifier and recovery scripts |
| `I4R_PROVIDER_TIMEOUT_SECONDS` | On an audit started on 2.7.4 or later, can only lower the frozen 5,400 s critique ceiling |
| `CRITICS`, `PARALLEL`, `ADJUDICATE`, `DIFF_MANIFEST` | Critique driver inputs (see the `run_critique.sh` header) |
| `I4R_OWNER` | Reservation owner identity for `reserve_paper.sh` (default `<user>@<host>`; set it when one person claims from two machines). It is also recorded as the environment's operator, so use a plain name such as `<user>@<lab>` |
| `CODEX`, `I4R_CODEX_BIN` | Force a specific Codex binary |
| `REGISTRY_DIR`, `I4R_REGISTRY_DIR` | Leave unset. Finalize refuses a registry outside the tree unless `I4R_REGISTRY_OVERRIDE=1` |

The full list is under "Environment variables" in `INSTALL.md`.

### Time and cost

Measured on three audits (not targets; they drift): setup about 5 min, five auditors 7-35 min, registry and
adjudication 1.8-13 h, all critique rounds 0.5-4.0 h, build and file 20-40 min. A clean critique round takes
10-20 min. Most audits finished on 2.6.x converged in 3-5 rounds.

`run.json` records only the pipeline's own metered calls. It leaves out the orchestrating Claude Code
session, which on one measured run cost about ten times the recorded figure. Budget from the provider console.
The round-0 fan-out accounts for 66-81% of recorded tokens.

<sub>Sources: `INSTALL.md` "Tree layout (2.5.0)", "Audit directory naming", "Running a batch", "Upgrading in-flight audits to 2.7.7", "Upgrading in-flight audits to 2.7.2", "Upgrading in-flight audits to 2.7.0", "Validate before the first audit and after every policy edit" (attempt ceiling and `--force`), "When something dies mid-Phase-1", "Correcting a published comment", "What an audit actually costs", "Environment variables"; `SKILL.md` §§1-5; `scripts/new_audit.sh`; `scripts/setup_workspace.sh`; `scripts/run_post_delta_gates.py`; `scripts/run_phase1.py` (attempt-exhaustion message, `recover_stranded_attempts`); `scripts/finalize_run.sh` (human effort); `scripts/reserve_paper.sh`; `scripts/status.py`; `scripts/wait_for_round.sh`; `scripts/lifecycle_journal.py`; `CHEATSHEET.html` §§4, 6-10.</sub>

## 5. Outputs

### The published set

| Folder | Files |
|---|---|
| `comments/<Journal>/<Paper>/` | The final PDF (hash-checked against `report/`), plus the editable DOCX when the document manifest binds one |
| `metadata/<Journal>/<Paper>/` | `claims_script.<ext>`, `run.json`, `comment.json`, `finalization_receipt.json`, `document_build_manifest.json`, `release-manifest.json` |

That is eight files with the DOCX, seven without.

- **Canonical copies.** The registry holds the canonical copy of the `run.json` and `comment.json` pair.
  `metadata/` holds a second copy so you can browse by paper.
- **Both files are internal.** They default to `safe_to_publish=false` and carry a `publication_redaction`
  block. Read that block before releasing any extract outside I4R.
- **Everything else stays in the audit folder.** That includes the five area reports, the ledger, the
  adjudication log, the critique rounds, the `work/` evidence and the verification files. `INSTALL.md` lists
  them under "What each audit retains".

### The comment

| Part | Rules |
|---|---|
| Abstract | At most 250 words, prose only, must end on page 1 |
| 1. Introduction | States what was not verified; cites both protocol papers (`reference/house_citations.md` is the only source) |
| 2. What reproduces | |
| 3..k. One section per substantive finding | Most consequential first; What / Evidence / Impact / Remedy |
| Minor issues; Hypotheses tested and not confirmed; Conclusion | |
| *(new page)* Data and Code Availability, References | Not counted toward the page cap |
| *(new page)* Appendix A | Generated table: # · Finding · Status · Changes a published conclusion? · Section |

- **Length.** The body is capped at 10 rendered pages; draft toward 9 before round 2. The cap comes from the
  hash-bound policy snapshot, and `MAX_PAGES` is refused.
- **What it never contains.** No executive summary, no findings table in the body, no Recommendations section
  (each finding carries its own Remedy), and no narration of the audit process.
- **Build checks.** `build_docx.sh` fails closed on a body over 10 pages, an abstract that spills off page 1,
  a DOCX table cell that differs from its source, or a PDF table word broken across lines.
- **Previews.** `--preview-max-pages N` writes non-releasable previews to `report/previews/`.

### `comment.json`: what was found

Next to the bibliographic, design, sample, package, governance and disclosure fields, `comment.json` holds
`findings[]`, `rollup`, `timing` and `author_response`. Each finding carries `claim`, `impact`,
`verification_tier`, `paper_only`, `disclosure_flag`, `remedy_class`, `changes_conclusion` and `provenance`.
`provenance` separates roles that found the issue independently from roles that agreed after seeing it; only
the first kind counts as corroboration. `finalize_run.sh` derives the roll-up. `assertions_withheld` records
that intent and misconduct were not assessed.

### `run.json`, the journal and the invocation ledger: how it was produced

- **`run.json`** is process telemetry: protocol and gate-set versions, toolkit hashes, phases, model
  assignments, rounds, the stopping reason, the compliance vector, the package manifest at intake and close,
  the environment, and delivery. A value nobody observed is `null`, never an estimate.
- **`run.events.jsonl`** is the append-only, hash-chained journal and the single source of truth. `run.json`,
  `invocations.jsonl` and the metrics files are projections of it.
- **`invocations.jsonl`** holds one record per model call or attempt; retries are never aggregated. Each
  record has the requested and served model, effort, timestamps, input and output hashes, and a token
  breakdown (input, output, cached, cache-creation, reasoning, total) with a reason for every `null`. Tokens
  are recorded for **both** model families.
- **Cost** for Claude calls comes from the Claude CLI's `total_cost_usd` (source `cli_log`). For GPT calls it
  is **not** recorded, because the Codex CLI does not expose one, and the schema forbids back-computing it
  from a price list. The observed total is documented as "a floor, never the truth".

### Verification notebook

`make_verification_notebook.sh` writes `verification.Rmd`, one section per finding. A claim with no snippet
gets a `stop()` stub; drive the stubs to zero. The notebook is R whatever the deposit's language. It reads the
deposit through `read_deposit()` and checks MD5 sums on entry and exit; if it reports
`deposit unmodified: NO`, stop. Each section declares `expect()` before computing, then calls `check()`.

`run_verification.sh` renders `verification.html`, `verification_summary.csv` and `verification_status.txt`
(`0` is clean), and writes `verification_manifest.json`, which binds the result to the exact report, comment
and package. There is no waiver for unverified publication. Rerun after the last report edit, or staging
refuses the stale binding.

| Verdict | Meaning |
|---|---|
| PASS | Within tolerance. Read the tolerance and its note first |
| FAIL | Triage in order: (1) the tolerance note or a rounding convention; (2) a stochastic quantity that needs a band or seed; (3) the comment does not pin the configuration, so the claim is UNVERIFIABLE (a comment defect); (4) the check itself is wrong; (5) otherwise the comment is wrong, so report it to the coordinator |
| JUDGEMENT | The assessment is stated in full, so you can disagree |
| UNVERIFIABLE | Not in the package, underspecified by the comment, or deliberately not attempted (privacy claims use counts only) |

Never loosen a tolerance until it passes, and never delete a section. Schedule work by `verification_tier`, and
verify `paper_only` findings first.

### Claims script

`make_claims_script.sh` builds `claims_script.<ext>` from `comment.json`, one numbered claim per finding. The
authors' own code comes first, verbatim, in the deposit's language (`.do`, `.R` or `.sps`). Checks the audit
wrote stay in R, and a deposit with no code falls back to R. Claims with no code go to a
`CLAIMS WITH NO CODE` tail, each with a manual check, so nothing is dropped. Claim numbers match Appendix A and
the `[claim N]` tags and are never renumbered. `claims_map.tsv` records which code belongs to which claim.

| Severity label | Rule (mapped mechanically) |
|---|---|
| RETRACTION MATERIAL | `conclusion_contradicted` or `conclusion_unsupported` at CONFIRMED |
| CORRIGENDUM MATERIAL | Those two at LIKELY, or `conclusion_narrowed`, `value_wrong`, `artefact_of_construction` |
| COMMENT MATERIAL | Everything else, and anything SPECULATIVE |

The cheat sheet adds one rule. If a RETRACTION- or CORRIGENDUM-level claim cannot be verified after honest
study, return the paper and take another. Never skip it, and never let the AI decide.

### Ledger and adjudication record

`work/findings.jsonl` is the canonical findings registry. `generate_finding_artifacts.py --apply` writes every
row to `report/LEDGER.md`. Only final-status, coordinator-approved INCLUDED rows reach `report/APPENDIX_A.md`,
the findings in `comment.json`, `claims_map.tsv`, `work/verification_plan.json` and `verification_snippets/`.
Never edit these projections by hand. `--check` fails when they diverge from the registry.

`report/ADJUDICATION.md` logs each decision as an `A<n>` entry with these fields: Sources, Question, Checks,
Decision (ACCEPTED, CONTESTED, WITHDRAWN or UNRESOLVED), Evidence and Required propagation.

<sub>Sources: `scripts/publish_completed.sh` header; `INSTALL.md` "What each audit retains"; `reference/report_structure.md`; `reference/verification_guide.md` §§1-5; `scripts/make_claims_script.sh` header; `schemas/comment.schema.json`; `schemas/run.schema.json`; `schemas/v2/invocation-record.schema.json`; `scripts/run_coordinator.py` `result_provider_cost`; `scripts/test_run_critique_runtime.py` (provider cost); `CHEATSHEET.html` "The four iron rules".</sub>

## 6. Findings scheme

`reference/SCHEME.md` (v1.0) classifies every finding on three fields that must never be merged into one
score: impact, verification tier and disclosure. Remedy and confidence are recorded beside them.

| Field | Values | Notes |
|---|---|---|
| Impact: what happens to the claim, not how big the error is | `none` < `unverifiable` < `value_wrong` < `conclusion_narrowed` < `conclusion_unsupported` < `conclusion_contradicted` < `artefact_of_construction` | `unverifiable` is never reported as `none`; it names what was missing in AEA terms (`data_missing`, `code_not_functional`, ...). Floating-point, seed, platform or solver differences count as `none`, with their size recorded |
| Verification tier: verifier effort, not gravity | T1 < 30 min · T2 0.5-2 h · T3 4 h-2 days · T4 2-5 days · T5 1-2 weeks | Design-time estimates. The separate `paper_only` flag marks findings established from the article alone; verify these first |
| Disclosure | Flags such as `identifiability_direct`, `identifiability_indirect`, `consent_scope_exceeded`, `ethics_approval_absent`, `licence_absent`, `licence_incompatible`, `third_party_rights`, `special_category_data`. Urgency is `none`, `routine`, `prompt` or `immediate` | `immediate` means the finding is reported in the comment; it never pauses the audit. A disclosure defect never raises impact, and impact `none` never suppresses a flag |
| Remedy class | `one_line_code`, `one_number`, `reanalysis`, `not_fixable`, `disclose_in_paper` | Use `disclose_in_paper` when neither code nor data is wrong but the paper leaves something out |
| Confidence | CONFIRMED, LIKELY, SPECULATIVE | |

The programme never asserts misconduct or intent, and never recommends an editorial action. The
`consistent_with` mapping to editorial outcomes is descriptive only. At finalize, a scan for banned words such
as "fabricated" or "p-hacked" *warns* but does not block, because "manipulated" is ordinary experimental
vocabulary.

<sub>Sources: `reference/SCHEME.md` Parts 2-4 (Fields 1-3; Rules 2, 3, 5, 6); `schemas/comment.schema.json` `$defs.finding`; `scripts/finalize_run.sh` vocabulary scan.</sub>

## 7. Safety, privacy and release gates

**Operating boundaries.**

- Never contact authors or journals. The comment filed with I4R is the disclosure route, and I4R decides any
  notification.
- Work only in `runs/<Journal>/<Paper>/audit`. Never write to `data/`, and treat `pkg/` as immutable: copy
  code into a role's `work/` before running it.
- Never place a participant identifier beside a name, date, timestamp or clock time.
- Do not attribute motive or intent. Keep material for replicators discipline-agnostic.

**Personal data is reported, never withheld.** Personal data in a deposit never pauses, parks or skips an
audit. It becomes a numbered, disclosure-flagged finding with its own section and Appendix A row. The finding
states the identifier kind, the package-relative file and column, how many rows or participants are affected,
whether anonymisation was claimed, and a verified remedy. It never states an identifying value, a new linkage
or a way to re-identify anyone.

For audits started on 2.6.9 or later, a disclosure-flagged row can leave the comment only on a structured
basis: refuted, a duplicate, not a disclosure defect, or tested negative. `finalize_run.sh` also refuses
`governance.pii_present: true` unless the comment has a disclosure-flagged finding (or a flagged row is still
awaiting approval), non-empty `disclosure.flags` and a `disclosure.statement`. It also refuses non-empty flags
when no finding is flagged.

| Gate | When | Refuses |
|---|---|---|
| Intake (`new_audit.sh`, `setup_workspace.sh`) | Before reservation | Ambiguous or empty inputs, glob characters, an audit folder under `data/`, corrupt archives, sync-conflicted copies, a host that cannot reach the critic model |
| Version pin (`init_run.sh`) | At init | A skill copy whose `VERSION` differs from the canonical copy |
| Phase-1 admission (`run_phase1.py`) | Per auditor | Unclean exit, wrong served model, incomplete usage, schema failures, broken hash joins |
| Coordinator admission (`run_coordinator.py`) | Per call | Missing telemetry, invalid findings, private paths or participant codes in safe evidence |
| `check_consistency.sh` | After every round that edits the comment | A participant code beside a date or clock time; surviving retired claims; broken house rules (abstract, early tables, Recommendations, citations, process narration); claim tags that do not join Appendix A |
| Post-delta gates (`run_post_delta_gates.py`) | Before each round from 2 on | A failure in consistency, release-bound privacy, checklist/projection or intake archive. A receipt is written only if all four pass |
| Document build (`build_docx.sh`, `document_release.py`) | At build and at the release boundary | Page cap, abstract off page 1, table integrity, a manifest from an older skill |
| Verification binding (`run_verification.sh`) | Before staging and finalize | Missing notebook, TODO stubs, non-zero status, a stale binding |
| `stage_release.sh` ("RELEASE BLOCKED") | At staging | Verbatim author files; any data file; per-participant listings or code-timestamp pairs; opaque per-row identifiers; identifying values from the deposit, or any e-mail or IP address (check 3c); home paths or e-mails in text, PDF or DOCX. Check 3d masks paths and e-mails in records nobody can reword |
| `finalize_run.sh` | Once, after delivery | Invalid `run.json` or `comment.json`, id or DOI mismatch, stale projections or receipts, PII without a disclosure finding. Exits 0 ok, 1 invalid, 2 refused |
| `publish_completed.sh` | At filing | No receipt, a stale manifest, compliance failure, token coverage below 1, a privacy failure, an existing destination |

`check_release_privacy.py` fails closed: a document it cannot inspect fails the check. Post-delta receipts
bind its bytes, so editing it makes every receipt stale. Raw agent transcripts and driver logs are never
shipped.

<sub>Sources: `SKILL.md` "Operating boundaries"; `INSTALL.md` "Deposits containing personal data (2.6.9)", "Identifying values in the audit's own outputs"; `reference/report_structure.md` "Disclosure findings"; `scripts/disclosure_policy.py`; `scripts/check_consistency.sh`; `scripts/run_post_delta_gates.py`; `scripts/stage_release.sh` header; `scripts/check_release_privacy.py`; `scripts/finalize_run.sh` header and disclosure checks; `scripts/publish_completed.sh`; `scripts/document_release.py`.</sub>

## 8. Code map, schemas and tests

| Path in `skill/i4r-audit/` | Contents |
|---|---|
| `SKILL.md`, `INSTALL.md`, `CHANGELOG.md`, `VERSION` | Run book; install and operations; release history; version |
| `config/` | `pipeline-policy-v2.json` (runtime policy), `check_policy_drift.py` (drift guard), `benchmark-seeded-fixtures.json` (benchmark input, not audit configuration) |
| `prompts/` | Critic prompts that name no paper: `common.txt`, `contract.txt`, `role_{stats,data,fairness,completeness}.txt` |
| `reference/` | Each file is read only when its stage needs it. `checklist.md` and `brief_template.md`: briefing auditors. `phase1_runtime.md`: Phase 1. `adjudication_guide.md`: integrating critics. `SCHEME.md`: classifying. `report_structure.md` and `house_citations.md`: drafting. `verification_guide.md`: claim checks. `failure_history.md`: diagnosis. `benchmarking.md`: benchmarking |
| `schemas/`, `scripts/` (82 files: 81 scripts and a README), `tests/` | See below and `scripts/README.md` |

| Stage | Scripts |
|---|---|
| Intake and tree | `new_audit.sh`, `setup_workspace.sh`, `reserve_paper.sh`, `init_run.sh`, `capture_environment.sh`, `tree_layout.sh`, `check_critic_access.py`, `status.py` |
| Run record | `log_event.sh`, `lifecycle_journal.py`, `record_invocation.py`, `summarize_invocations.py`, `run_meta.sh`, `claude_model_usage.py`, `lock_wait.py` |
| Phase 1 | `run_phase1.py`, `phase1_runtime.py`, `provider_safety.py`, `email_scan.py`, `claude_routes.py`, `probe_bounds.py`, `phase1_timeouts.py` |
| Ledger and draft | `build_draft_registry.py`, `build_safe_evidence.py`, `run_coordinator.py`, `init_comment.py`, `generate_finding_artifacts.py`, `project_draft_claim_tags.py`, `check_consistency.sh`, `renumber_findings.py` |
| Critique rounds | `run_critique.sh`, `test_run_critique_runtime.py` (a required runtime helper, **not** a test), `wait_for_round.sh`, `integrate_round.py`, `approve_settled.py`, `make_settled.sh`, `make_diff_packet.py`, `verify_diff_packet.py`, `validate_round_projections.py`, `run_post_delta_gates.py`, `claim_change_rules.py`, `critique_timeouts.py`, `codex_cli.py`, `verify_codex_model.sh`, `later_disputes.py`, `repin_critic_route.py`, `amend_round_cap.py`, `reopen_audit.py`, `round_cap_amendment.py` |
| Post-fan-out verifier | `run_verifier.py`, `verifier_contract.py` |
| Deliverable and verification | `build_docx.sh`, `document_build_cache.py`, `document_release.py`, `make_verification_notebook.sh`, `run_verification.sh`, `run_snippets.sh`, `snippet_lint.R`, `make_claims_script.sh` |
| Release and filing | `stage_release.sh`, `check_release_privacy.py`, `check_deposit_values.py`, `release_record_masking.py`, `disclosure_policy.py`, `finalize_run.sh`, `finalization_receipt.py`, `publish_completed.sh` |
| Packaging and helpers | `build_package.py`, `benchmark_optimization.py`, `validate_json_schema.py` |

Recovery utilities sit outside the ordinary run:

- **Current:** `recover_coordinator.py`, `recover_misplaced_coordinator_outputs.py`, `migrate_metadata_2_1.py`.
- **Stale.** Do not run these until their pins are re-derived: `recover_critique_probe.py`,
  `recover_round1_projection.py`, `retry_completeness_once.py`, `retry_targeted_adjudication_once.py`,
  `repair_phase1_all_model_usage.py`, `migrate_phase1_provider_accounting.py`. Their failures look like a
  corrupt audit.
- **Retained history:** one one-off script from a single past audit, which is not a template.
  `scripts/README.md` names a second script that this repository leaves out; the released package keeps it.

`scripts/test_document_build_cache.py` is a test that lives in `scripts/` because `INSTALL.md` calls it by
that path. `scripts/README.md` does not yet list `claim_change_rules.py`, `critique_timeouts.py`,
`phase1_timeouts.py`, `provider_safety.py`, `renumber_findings.py`, `run_snippets.sh`, `snippet_lint.R` or
`status.py`.

**Schemas.**

| Schema (`schemas/`) | Validates |
|---|---|
| `comment.schema.json`, `run.schema.json` | `comment.json` (what was found), `run.json` (process telemetry) |
| `v2/finding-row.schema.json`, `v2/findings-registry.schema.json` | One `work/findings.jsonl` row; optional export envelope for the rows |
| `v2/invocation-record.schema.json`, `v2/lifecycle-event.schema.json` | One model-call record; one hash-chained journal event |
| `v2/critic-result.schema.json`, `v2/post-delta-gates.schema.json` | One critic's output; the four-gate receipt |
| `v3/document-build-manifest.schema.json` | The exact document build |

The folder also holds `EXAMPLE_comment.json` and `EXAMPLE_run.json`, which the finalize self-test uses. It
also holds `fixtures/`, `tests/test_v2_interfaces.py`, and `tools/` with `validate_v2.py` and two normalizers.
The normalizers never estimate missing telemetry.

**Tests.** The suite is standard-library `unittest` plus one bash harness; there is no Makefile, pytest or
tox configuration. Run the battery in [Verifying the install](#verifying-the-install) from the skill root,
because the suites use package-relative imports.

- **Size.** Counted from source, not from a run, so it drifts. `tests/` has 94 `test_*.py` modules with about
  1,440 test functions. `schemas/tests/test_v2_interfaces.py` has 37 more.
- **Run time.** The cheat sheet estimates about 5 min for the battery, plus about 12 min for
  `tests/test_run_critique.sh`, a deterministic end-to-end harness for the critique runtime.
- **No real model calls.** Fake CLIs stand in for the models: `tests/fake_claude_phase1.py`,
  `tests/fake_claude_coordinator.py` and `tests/fake_codex.sh`.
- **Layout.** There are per-release regression modules (`test_261` to `test_277`) beside per-subsystem modules.
  *(Grouping inferred from file names.)*
- **Skips.** Some tests skip without Rscript (with `haven` or `rmarkdown`), pandoc, LibreOffice, Poppler or
  `zipinfo`. Others skip unless an environment variable (`I4R_V263_SKILL`, or an `I4R_BASE_<version>`
  variable such as `I4R_BASE_270`) points to a pristine older skill tree. One class skips unless
  `I4R_STRAND_CORPUS` points to a discipline tree of real stranded attempts.

**Where the documents disagree.**

| Topic | Stale statement | What holds |
|---|---|---|
| Cheat-sheet version | Header says 2.6.2 | The install step shows 2.7.7; follow `INSTALL.md` and the policy |
| Critic timeout | Cheat sheet §9 says 45 min | New audits freeze `critique.provider_timeout_seconds=5400` (90 min) |
| `jsonschema` | `INSTALL.md` lists it as core | Needed only by the test suite; the skill ships its own validator |
| Adjudicator and verifier models | `reference/adjudication_guide.md` says Terra and Sonnet/max; the policy label `terra_targeted_on_policy_triggers` is legacy | The policy's `models.*`: `gpt-6.1-sol`/`max` and `sonnet-5-5`/`xhigh` |
| Findings table | `make_claims_script.sh` header mentions a "page-1 findings table" | Appendix A, never page 1 |
| Empty findings | `comment.schema.json` allows them | `publish_completed.sh` refuses a comment with no findings |
| Stranded-attempt recovery | `INSTALL.md` "When something dies mid-Phase-1" and `run_phase1.py --help` say `--recover-stranded` charges nothing | Since 2.7.2 a strand whose workspace holds files the model wrote is charged one attempt (`INSTALL.md` "Upgrading in-flight audits to 2.7.2") |
| Permission denials | `reference/phase1_runtime.md` "Role output contract" requires no permission denial | A denial is recorded in `sandbox_denials.json` and does not fail the role (`SKILL.md` §2; `scripts/run_phase1.py`) |

<sub>Sources: `scripts/README.md`; `SKILL.md` "Read references only when their stage needs them"; `schemas/` titles and `schemas/tools/*.py`; `INSTALL.md` "Validate before the first audit"; `CHEATSHEET.html` step 2, §1, §9; `tests/` listing; `config/pipeline-policy-v2.json`; `reference/adjudication_guide.md`; `scripts/make_claims_script.sh` lines 21-22; `tests/test_stranded_attempt_recovery.py` line 196; `INSTALL.md` "When something dies mid-Phase-1"; `reference/phase1_runtime.md` "Role output contract".</sub>

## 9. The baseline skill

`skill/baseline/` (version 1.1.0) runs the **single-agent, single-pass** condition. One Claude call per paper
audits the paper and its package and writes the whole comment, with no critics, no coordinator and no rounds.
It exists to be compared with the full pipeline on the same already-audited papers. Do not use it to audit a
new paper.

- **Held fixed against the pipeline.** The model (`claude-sonnet-5-5` at `xhigh`, a 1,000,000-token window,
  the 128,000-token output cap); the input (the published audit's frozen Phase-1 snapshot, copied byte for
  byte); the task definition (i4r-audit 2.7.7's checklist, brief, scheme, report structure and citations,
  verbatim and hash-recorded); the launch arguments and network denial; the comment layout and Appendix A
  generator; and the accounting.
- **What differs (recorded in every `run.json`).** One call does the work of five auditors, the coordinator
  and the rounds. Each call has a USD 30 budget and a 7,200 s timeout, with one retry only if no inference
  started. The sandbox is tighter. Before any paid call, a contamination scan refuses to start if it finds
  identical copies of the subset's published comments.

Install with `cp -R "<tree>/skill/baseline" ~/.claude/skills/baseline` (or a symlink), then restart Claude
Code. The scripts run from the tree root:

```bash
S=~/.claude/skills/baseline
python3 $S/scripts/select_subset.py --write        # save baseline_comments/SUBSET.json (omit --write to preview)
python3 $S/scripts/run_baseline.py preflight       # no model call
python3 $S/scripts/run_baseline.py probe           # one call (about USD 1) proving the sandbox works
python3 $S/scripts/run_baseline.py batch           # every paper with no run yet, one at a time (or: run 1)
python3 $S/scripts/run_baseline.py status
python3 $S/scripts/summarize_baseline.py --json baseline_comments/summary.json
```

- **Outputs.** They go to `baseline_comments/<journal>/<paper>/`. The baseline never edits `runs/`, `comments/`
  or `metadata/`. A rerun moves the earlier output to `.superseded/` and deletes nothing.
- **Tests.** Run `python3 -m unittest discover -s tests`. It uses a fake Claude CLI.
- **Finding i4r-audit.** The baseline looks for i4r-audit at `<tree>/skill/i4r-audit-unified/i4r-audit` or
  `~/.claude/skills/i4r-audit` (stated in `skill/baseline/SKILL.md`, Install). The two tests that compare
  against the live skill look only at `i4r-audit-unified/i4r-audit` beside the baseline's folder. In a plain
  checkout of this repository, where the skill sits at `skill/i4r-audit`, they skip even when i4r-audit is
  installed in `~/.claude/skills`.

See [`skill/baseline/SKILL.md`](skill/baseline/SKILL.md) for statuses, subset pools and the full command set.

<sub>Sources: `skill/baseline/SKILL.md` (purpose, "What it holds fixed", Install, Commands, Statuses, Tests); `skill/baseline/config/baseline.json`; `skill/baseline/tests/test_baseline.py` line 19 and the two `skipUnless` tests; `skill/baseline/scripts/run_baseline.py` line 287.</sub>

## 10. Version history

These are highlights only. [`skill/i4r-audit/CHANGELOG.md`](skill/i4r-audit/CHANGELOG.md) has the full record
from 2.2.0. 2.1.0 has no changelog entry; its row below comes from the commit that adopted it. All dates are
in 2026.

| Version (date) | Highlights |
|---|---|
| 2.1.0 (08-28) | Became the canonical skill. The critique driver was rewritten around immutable snapshots, cached probes and outputs, retry budgets that cannot reset, and schema quorum. Added `tests/`, `schemas/v2` and `config/` |
| 2.2.x (09-02 to 09-08) | Production audits begin. OAuth logins work (no `--bare`). `--probe-only` became the live canary. Sandbox paths are checked for glob characters. Every paid call leaves a ledger row |
| 2.3.0 (09-08) | Unattended runs became the default. Finalize resolves the tree's registry |
| 2.4.x (09-08 to 09-17) | Batch readiness: archive tests before reservation, refusal of conflicted copies, `--recover-stranded`, `status.py`, `renumber_findings.py`, `reserve_paper.sh reap`, an adversarial sweep of the skill itself, and a check that the running skill matches the canonical copy |
| 2.5.0 (09-22) | The `data/ runs/ comments/ metadata/ old_runs/` tree; `new_audit.sh` as one-command intake |
| 2.6.0-2.6.3 (09-22 to 09-23) | Critic route read from each audit's snapshot. A move to GPT-6 Sol was reverted to Terra because some accounts were refused it. Added a journaled re-pin |
| 2.6.4-2.6.7 (09-25 to 09-27) | Post-fan-out verifier. One participant-code rule. Fixed the sandbox "argument list too long" failure |
| 2.6.8-2.6.10 (09-27 to 09-29) | Deposit personal data is reported, never a reason to stop. Added stage check 3c. Round cap of 7 for new audits; an existing audit's cap changes only through a journaled amendment or reopen. Appendix A renders literally. Added `codex_cli.py` |
| 2.7.0-2.7.4 (09-29 to 09-30) | Sonnet 5.5 and GPT-6.1 Sol for new audits. Probe bound raised from 150,000 to 350,000; Phase-1 timeout raised from 2,700 to 5,400 s. Claude effort moved from `max` to `xhigh`, because `max` roughly doubled the cost per stage. 128,000-token output cap. The measured environment is kept in `run.json`. Existing audits keep what they froze |
| 2.7.5 (10-07) | Frozen critic route lock. `check_critic_access.py` runs before reservation |
| 2.7.6 (10-07) | Stage check 3d, the `text_redaction` event, and a linear-time e-mail scanner |
| 2.7.7 (10-09) | The validator accepts redacted events. The Phase-1 participant/date rule needs an identifier plus a date or clock time. A round-0 consistency check no longer creates a round row. The install command creates `~/.claude/skills` first |

`skill/MERGE_NOTES.md` predates most of this. Its Opus coordinator, Sonnet 5 auditors at `max`, Terra critics
and lower round cap have all been replaced.

<sub>Sources: `skill/i4r-audit/CHANGELOG.md` entries 2.2.0-2.7.7; commit `c649e88` message; `skill/MERGE_NOTES.md`.</sub>

## 11. Status and how to cite

### Status

**Private for now.** The files in this repository do not name the papers that were audited or their authors. Earlier commits in its history do, so rewrite the history (or start a new repository from the current files) before making it public.

### How to cite

Two papers to cite:

> Brodeur, A., et al. (2026). AI-assisted teams outperform AI-led teams but not human-only teams in
> assessing research reproducibility in quantitative social science. *PNAS*, 123(22), e2524747123.
>
> Institute for Replication (2026). Reproducing with AI Across the Expertise Ladder. OSF:
> https://osf.io/dkfzt/
