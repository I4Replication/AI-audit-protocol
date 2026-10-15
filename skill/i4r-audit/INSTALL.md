# i4r-audit — install and validate

This Claude Code skill runs an I4R reproducibility audit from a published paper and replication
package. It produces the comment, verification notebook, claims handoff, and machine-readable run
record.

The canonical runtime policy is
[`config/pipeline-policy-v2.json`](config/pipeline-policy-v2.json). It specifies Sonnet 5.5/xhigh, spawned
by its full id `claude-sonnet-5-5` with `--effort xhigh`, for the coordinator, all five phase-1 areas,
their capability probe and the post-fan-out verifier of a new audit (an audit initialised on 2.7.0 to
2.7.2 keeps Sonnet 5.5/max, and one initialised before 2.7.0 keeps Sonnet 5/max, each frozen in its own
snapshot); GPT-6.1 Sol/high for a new audit's ordinary external critics and the cached model probe,
and GPT-6.1 Sol/max for its triggered targeted adjudication (an audit initialised before 2.7.0 keeps
the critic route it froze the same way: GPT-5.6 Terra, or GPT-6 Sol for the one audit initialised
on 2.6.0). The default audit uses five Claude subagents, four
round-1 critics, two dynamically routed round-2 critics, and no verifier call. Add later-round specialists only when conclusion-changing or
disclosure-critical corrections span multiple domains.

## Install

Copy or symlink the complete `i4r-audit` directory into the Claude Code skills directory:

```bash
mkdir -p ~/.claude/skills
cp -R i4r-audit ~/.claude/skills/
chmod +x ~/.claude/skills/i4r-audit/scripts/*.sh
```

Create `~/.claude/skills` first: on a Mac without it, `cp -R` creates that directory from the skill's contents and
Claude Code finds no skill. To update, move the old copy out of `~/.claude/skills` (Claude Code could load a copy
left beside the new one), then copy again. Every Mac holds its own copy, so a new shared zip reaches nobody until
they reinstall; CHEATSHEET step 2 has the lines.

A team should prefer a version-controlled shared source with per-user symlinks so policy, scripts,
prompts, schemas, and references update together:

```bash
ln -s /path/to/versioned/i4r-audit ~/.claude/skills/i4r-audit
```

Restart Claude Code after installing. Invoke `/i4r-audit` or ask for an I4R reproducibility audit of
a paper and its package.

## Upgrading in-flight audits to 2.7.7

Nothing to do for an audit that is running. 2.7.7 changes no frozen value, prompt, packet or receipt (CHANGELOG,
2.7.7). After reinstalling, three kinds of stopped audit continue:

- **A Phase-1 role refused with "links a participant identifier to a date/time"** (rule `participant_date`). Claim the
  paper again if its reservation was released when it was parked (`bash scripts/reserve_paper.sh claim
  data/<Journal>/<Paper>`), then rerun `python3 scripts/run_phase1.py "$AUDIT_DIR"`. Accepted roles are reused; a role
  that spent its attempts needs a higher ceiling, e.g. `I4R_MAX_ATTEMPTS=7` for the tennis audit (e24acd26), whose
  code-and-methods role used six. The sleep audit (d1fa93d5) was parked before Phase 1 and starts it as usual.
- **Finalize stopped at 0c with "run critique rounds are not unique and contiguous"** because a round-0 consistency
  check made a round 0 (Effort intensity, 82c35f72). Re-project, then finalize and publish as the stop note says:

  ```bash
  python3 ~/.claude/skills/i4r-audit/scripts/lifecycle_journal.py project "$AUDIT_DIR"
  ```

  The journal is not touched. That audit ran its critique on 2.6.4, so finalize then asks for what later releases
  added. On a scratch copy it passed after, in this order: a `review_scope` event carrying exactly the
  `changed_claim_ids` the 0c refusal lists (in its order, also as `rereviewed_claim_ids`); `chmod -R a-w
  "$AUDIT_DIR/pkg"`; steps 4 and 5 of *Re-filing a published comment with the corrected Appendix* (rebuild the
  verification notebook, then the last round's packet and post-delta receipt); and `build_docx.sh`. Finalize then
  completed and listed the journal events the audit never logged (phase pairs, the delivery), which are logged as it
  says before staging and finalizing again. None of these steps calls a provider.
- **Any other audit with a round-0 row** (Athlete burnout, a24422cf, paused in round 1): every logged event
  re-projects `run.json`, so the row goes with the next one; `project` as above removes it at once.

## Upgrading in-flight audits to 2.7.6

Nothing to do. 2.7.6 changes no frozen value, prompt, packet or receipt (CHANGELOG, 2.7.6). It unblocks audits that
could not continue:

- **Home paths and e-mail addresses in records nobody can reword.** Staging check 3d masks them in the STAGED copy of
  a critique record, Phase-1 report, work record or evidence script (`<user-dir>` for the account segment,
  `[e-mail address]` for an address the byline and the article do not print), lists each file in
  `RELEASE_PATH_NEUTRALISATION.txt`, and leaves the audit's files untouched. The comment's own outputs still refuse.
  Audits 70b8eac5 and 803f98f9: restage into a fresh directory.
- **Logged text.** `log_event.sh` and the `lifecycle_journal.py append-*` commands refuse text holding an e-mail
  address, a private absolute path or a participant-code token, because `run.json` is provider-visible and published.
  Text already journaled is masked in `run.json` by a later event, with the journal untouched:
  `bash scripts/log_event.sh text_redaction seq=<n> field=<text|notes|note|detail|description|recipient> reason='<why>'`
  (`seq` is the event's seq in `run.events.jsonl`). The coordinator's refusal now names this remedy.
- **Event types.** The append commands refuse a type outside the lifecycle-event schema. `environment`, the
  pre-2.7.4 hand-logged type, is now a `run.schema.json` event type the projection ignores, so the Social norms
  audit (`runs/`, 20260922-audit-89065d92) can finalize; a new `environment` event is still refused.
- **Phase-1 preflight on knitted HTML.** The e-mail detector is linear, so a deposit with large self-contained HTML
  no longer exceeds the 60 s pre-dispatch deadline (Beyond Rubik: resume Phase 1).
- **new_audit.sh** ignores hidden folders and `__MACOSX` when it picks the package, refuses a folder holding both
  sub-folders and loose files (it names the `--package` choices), and refuses an empty package. Start Claude Code
  at the tree root, never in a paper folder (CHEATSHEET).
- **Coordinator budget.** A coordinator call that timed out or wrote files counts as an attempt, so
  `I4R_MAX_ATTEMPTS` bounds it, and `I4R_COORDINATOR_TRANSIENT_RETRIES` is capped at 20.

## Upgrading in-flight audits to 2.7.5

2.7.5 makes sure every audit started from now on runs its critique on GPT-6.1 Sol and its Claude roles on Sonnet
5.5, as the policy has frozen since 2.7.0, and that nothing moves it off them. An audit in flight keeps its frozen
route, packets, prompts and receipts.

- **Why.** From 2026-09-30 to 2026-10-07 at least twelve audits frozen on `gpt-6.1-sol` (thirteen snapshots, one
  a failed copy) were re-pinned to `gpt-5.6-terra`, several within seconds of init, because their hosts' Codex CLIs
  were signed in with ChatGPT accounts that refuse Sol (HTTP 400 "The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account"), and
  this file documented that re-pin as the remedy. Whether a host can use Sol depends on the account, not on the
  login type: this release's host is a ChatGPT login that can, the programme's is one that cannot.
- **The route is locked in the audit.** A new audit's snapshot freezes `critique.critic_route_lock`
  (`gpt-6.1-sol`). `run_critique.sh` refuses, before anything is built or paid for, any round whose critics,
  adjudicator or critique probe differ from it: a re-pin by an older copy of `repin_critic_route.py` (2.7.4 and
  earlier accepted any `--critic-model`) cannot move it, and the refusal names the restore command. And a completed
  critic or adjudicator review on any other model never validates (`record_invocation.validate_record`), so an
  audit that an older copy of the driver ran off the lock cannot be integrated, finalized or published.
  `repin_critic_route.py` itself accepts `--critic-model` / `--adjudicator-model` only with the shipped values,
  moves an audit pinned to an older route (`gpt-6-sol`, `gpt-5.6-terra`) with no completed review up to Sol, and
  answers "already pinned" to a request that changes nothing. Audits already re-pinned to Terra keep it (one
  of them was still running on 2026-10-07).
- **`new_audit.sh` checks the account before it starts.** After validating its inputs and before it reserves the
  paper or copies anything, it runs `scripts/check_critic_access.py`: one minimal call in the critique driver's
  exact command shape (the policy's critic model and effort, a one-field output schema, no audit content). A pass
  is cached in `~/.i4r/critic_access.json` (`I4R_STATE_DIR` moves it) for 24 hours, keyed on the CLI path and
  version, the `codex login status` line, a digest of the CLI's login store (`${CODEX_HOME:-~/.codex}/auth.json`,
  which every login rewrites; only the digest is kept) and the route, so a new login, another account or another
  CLI is checked again; a refusal is never cached as a pass. Exit 3: the account cannot use it, including any refusal
  the provider returns for the account, key or organisation (on a CLI older than 0.159.0, update the ChatGPT app
  first); exit 4: network, rate limit, server or timeout, run it again; exit 5: the CLI rejected the call itself,
  with no answer from the provider, update it. On a refusal nothing is reserved, and `new_audit.sh` prints the command
  to run again. Signing the CLI in to another account (for example an OpenAI API key whose organisation offers the
  model: `printenv OPENAI_API_KEY | codex login --with-api-key`) is the user's decision, not the operator agent's.
  `setup_workspace.sh` called directly skips this check: run `python3 scripts/check_critic_access.py` first.
- **In-flight audits on Sol that have not run round 1:** run `python3 scripts/check_critic_access.py` on the host
  that will run the round, now. Since 2.7.5 an account without Sol cannot be worked around by a re-pin.
- **One-time cost.** `run_critique.sh` changed, which moves the adjudicator target-snapshot address (it binds the
  driver's bytes): a round relaunched after the upgrade that reaches adjudication makes one more adjudicator call.
- **Leftover snapshots.** `init_run.sh` refuses a leftover snapshot (an init that did not finish) frozen on another
  critic route or before the lock (removing it costs nothing), and prints the critic route it froze.
- **Audits initialised before 2.7.0** keep Sonnet 5 and their frozen critic model for life, by design.
- **Mixed teams.** Run every script from the canonical copy. A teammate on a 2.7.4 copy can still run
  `new_audit.sh` without the check and reserve and copy the paper; its `init_run.sh` is then refused by the
  canonical-`VERSION` gate, and the audit initialised from the canonical 2.7.5 copy freezes the lock but makes no
  account check at init: run `scripts/check_critic_access.py` before Phase 1. A 2.7.4 copy that re-pins a locked
  audit and runs the round with its own driver does spend calls on the other model, but those reviews never
  validate, so the audit cannot be finalized until the route is restored and the round re-run. Initialising with
  `I4R_ALLOW_SKILL_VERSION_MISMATCH=1` from a 2.7.4 copy gives an audit with no lock; do not.

## Upgrading in-flight audits to 2.7.4

Nothing to do. Every audit initialised before 2.7.4 keeps its frozen values and its packet, prompt and receipt bytes
(CHANGELOG, 2.7.4). What changes for it, and what only a new audit gets:

- **For every audit (identity-neutral environment and message changes).** Sonnet 5.5 spawns (an audit initialised on
  2.7.0 to 2.7.3 included) run with `CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000`, and an output-cap overflow is retried
  once. Every Phase-1 and verifier spawn runs with `CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1`, which ends the E2BIG
  shell failures on Claude CLI 2.1.206; updating the Claude CLI also helps, since newer CLIs build much smaller sandbox
  profiles. `build_draft_registry.py` finds a role the focused output repair completed. The driver records the
  reviewed report's SHA-256 on `round_integrated`. Back-matter headings carry their own page break.
  `scripts/run_snippets.sh` runs snippets in the release notebook's environment.
- **Only for a new audit (frozen at init).** The critique ceiling `critique.provider_timeout_seconds` 5400: an older
  audit keeps 2,700 s and `I4R_PROVIDER_TIMEOUT_SECONDS` exactly as before, so an in-flight Sol audit may export
  `I4R_PROVIDER_TIMEOUT_SECONDS=5400` for its rounds. And the claim-change rule
  `critique.later_rounds.routing_manifest.claim_change_rule` `substantive_specialists_1`: specialists re-review only
  substantive changes, completeness every changed finding; an older audit's packets keep their bytes. `init_run.sh`
  prints both.
- **Two one-time costs.** `run_critique.sh` and its runtime changed, which moves the adjudicator target-snapshot
  address (it binds the driver's bytes): a round relaunched after the upgrade that reaches adjudication makes one
  more adjudicator call. And `build_docx.sh` changed, so every `document_build_manifest.json` built on 2.7.3 or
  earlier binds a builder this release no longer has: finalize, `stage_release.sh` and `publish_completed.sh` refuse
  the document until you re-run `bash scripts/build_docx.sh "$AUDIT_DIR" <name>` (and the verification notebook if
  the report changed). Two in-flight audits on 2026-10-01 are affected (20260930-audit-70b8eac5 and
  20261001-audit-803f98f9); re-filing any published comment also needs the rebuild, which can move its page breaks.
- **The capture message** now reads `environment journaled (environment_captured, journal seq N): ...` instead of
  `environment recorded: ...`.

### The environment record

2.7.4 keeps the software environment `init_run.sh` measures.
`capture_environment.sh` now journals it as one `environment_captured` event, and `run.json`'s
`environment` is the projection of the latest one, so it survives every later append. Before 2.7.4
the capture wrote `run.json` directly and the first append after init re-projected `run.json`
without it: 31 of the 32 audits under `runs/` on 2026-09-30 have none.

- **Existing audits are unchanged.** No journal holds an `environment_captured` event, so each one
  projects and validates byte for byte as on 2.7.3. The lost records cannot be recovered
  from any audit file.
- **An audit that is still open** can record the machine as it is now: run
  `bash scripts/capture_environment.sh "$AUDIT_DIR"`. The record's `captured_at` says when it was
  taken, which is not the audit's start. A finalized audit refuses the capture until it is reopened,
  because an event after its finalize would stale the finalization receipt.
- **Only the capture writes it.** `log_event.sh` and the `lifecycle_journal.py append-*` commands
  refuse `environment_captured`, and the pre-2.7.4 hand-logged type `environment` too. A hand-logged
  `metadata_correction` may not set `environment`. Three journals hold a hand-logged `environment`
  event from a 2.2.x copy (one under `runs/`, "Social norms and emotion regulation among youth
  athletes", and two in `old_runs/`). The projection never read that type and still does not. On 2.7.4
  and 2.7.5 their finalize stopped at schema validation on it; 2.7.6 makes `environment` a `run.schema.json`
  event type, so it no longer does.
- **finalize says what it finds**, right after it validates `run.json`: `ok: run.json carries the
  environment captured at <time>, journal seq <n>`, or a WARN naming the true cause. On a
  metadata-2.1 audit it judges by the journal, so an `environment` block written into `run.json` by
  hand is reported as dropped by the re-projection. The WARN that 2.7.3 and earlier carried,
  "capture_environment.sh was never run", was never printed: it sat in dead code in `write_validator`,
  after the function's `return`.
- **The operator field.** It is `I4R_OWNER` when that is set, else `<user>@<host>`. `run.json` is
  filed with the comment, and both publication gates (`check_release_privacy.py` and check 3c)
  refuse any e-mail or IP address in it. So an `I4R_OWNER` either gate reads as one is recorded as
  `<user>@<host>` instead, with a note. An append-only journal could never shed it.
- **The coordinator sees the block.** On a new audit the coordinator reads `run.json` (as
  `context/run.json`), so the operator and the tool versions reach the provider, and it refuses the
  whole file if any string fails its provider-safety scan. A later capture cannot repair that, since
  `run.json`'s events keep every capture. So the capture applies the coordinator's own scan first: an
  `I4R_OWNER` it refuses gives way to `<user>@<host>`, an account it refuses (a host named `MBP16`
  makes one) leaves the operator null, and a tool reading it refuses is recorded as null, each with a
  note. If the operator note appears, set `I4R_OWNER` to a plain name such as `<user>@<lab>`.
- **Mixed teams.** A teammate on 2.7.3 or earlier can continue an audit initialised on 2.7.4: their
  appends and projections succeed, but their `run.json` drops `environment` until a 2.7.4 tool
  projects the journal again, and their finalize stops at schema validation (`/events/1/type:
  'environment_captured' is not one of [...]`). The journal is append-only, so finalize such an audit
  on 2.7.4 or later. An audit initialised on 2.7.3 or earlier continues on 2.7.4 unchanged.

## Upgrading in-flight audits to 2.7.3

2.7.3 runs a new audit's Claude roles one effort degree lower: the coordinator, the five Phase-1
auditors, their capability probe and the post-fan-out verifier are spawned with `--effort xhigh`
instead of `--effort max` (the Claude CLI's levels are low, medium, high, xhigh and max; CLI 2.1.206
accepts xhigh). The model and its spawn are unchanged (`--model claude-sonnet-5-5`), and so is the GPT
side: critics and the critique model probe at high, the targeted adjudicator at max (CHANGELOG, 2.7.3).

- **Why.** On the first Sonnet 5.5 audits, Sonnet 5.5 at max wrote 2.3 to 2.9 times the output tokens of
  Sonnet 5 at max, about 72.5% of them thinking, so each Claude stage cost about twice as much at the same
  list price. One coordinator response overflowed the Claude CLI's 64k-token per-response output cap.
  Sonnet 5.5's effort levels are recalibrated, so the level chosen for Sonnet 5 does not carry over.
- **Every existing audit keeps its effort for its whole life.** The effort is part of the Claude route
  an audit's `pipeline-policy.snapshot.json` froze, like the model: an audit initialised on 2.7.0 to
  2.7.2 stays on Sonnet 5.5 at max, and one initialised earlier on Sonnet 5 at max. For them nothing
  changes, byte for byte: the prompts (still "at max effort"), role packets, role input hashes, probe
  cache keys, the coordinator's input hash and prompt, the verifier prompt (so recorded verifier calls
  re-admit), ledger and receipt validation, and init's refusals. Nothing re-runs and nothing is re-keyed.
  `scripts/claude_routes.py` admits exactly Sonnet 5 at max, Sonnet 5.5 at max and Sonnet 5.5 at xhigh.
- **New audits: xhigh.** `init_run.sh` freezes it from the shipped policy for all eight Claude entries
  (`models.coordinator`, the five `models.phase_1_auditors`, `models.post_fan_out_verifier` and
  `phase_1.capability_probe`) and prints it (`auditor model: sonnet-5-5 (claude / anthropic), effort
  xhigh`). Each prompt names the effort it runs at ("Use Claude Sonnet 5.5 at xhigh effort."), the
  runners pass `--effort xhigh`, and the ledger rows say `xhigh`. The capability-probe cache key includes
  the effort, so a new audit makes its own probe.
- **Routes no runner dispatches are refused before any call.** `run_phase1.py`, `run_coordinator.py` and
  `run_verifier.py` refuse a snapshot whose Claude route is not one of the three (for example Sonnet 5.5
  at high, or Sonnet 5 at xhigh), listing the live routes, and the verifier still refuses a route other
  than the auditors' own, so auditors at xhigh beside a verifier at max are refused too. A snapshot left
  in an audit directory without a `run.json` (such as 2.7.2's `record_invocation.py --init-policy`
  output, at max) is not the shipped route, so `init_run.sh` refuses it with the `rm` that clears it;
  for a route no runner dispatches it says so.
- **Moving an audit to xhigh.** Only a forced re-init does it, before Phase 1 is paid for: `bash
  scripts/init_run.sh "$AUDIT_DIR" --force --audit-id <its audit_id> --doi <doi> --paper-title <title>`
  re-freezes the whole shipped policy and re-runs all five auditors. There is no per-role re-pin, and a
  frozen snapshot is never edited.
- **Mixed teams.** A teammate on 2.7.2 cannot start an audit once the canonical copy is 2.7.3, and 2.7.2
  refuses a 2.7.3 audit's snapshot ("... is not a packaged Claude route") before any provider call. An
  audit started on 2.7.2 continues on 2.7.3 at max.

## Upgrading in-flight audits to 2.7.2

2.7.2 fixes how Phase 1 books a call its timeout stops, and raises new audits' per-call Phase-1
timeout from 2,700 to 5,400 seconds (CHANGELOG, 2.7.2).

- **The defect.** Until 2.7.2 every Phase-1 or verifier call stopped by the timeout was left
  stranded at `provider_requested`: the killed CLI prints no result envelope, and the exception
  `parse_result` raised escaped `run_process` before any status or terminal row was written. Only
  `run_phase1.py --recover-stranded` could close it, as a request the provider never accepted, and
  two such closes refuse the role for good ("exhausted two pre-inference provider-request
  retries"), whatever `I4R_MAX_ATTEMPTS` says. Every timeout on 2.7.1 or earlier was booked this
  way, including calls that had run for the full 45 minutes.
- **Now.** The runner terminalizes a timed-out call itself (`status` `timed_out`, `error_class`
  `timeout`). If the model wrote any file into the attempt's output workspace, the call is booked as
  a started inference and charges one of the role's attempts; a timeout is not retried inside the
  same run, and re-running `run_phase1.py` spends the next attempt. A call that wrote nothing is
  still booked as not started. `--recover-stranded` applies the same rule to a strand left by a
  killed runner (or by a timeout on an older release): one whose output workspace holds files the
  model wrote is charged one attempt and recorded as started, so it no longer counts toward the two
  pre-inference requests; one with an empty workspace is closed uncharged, as before. The rows a
  2.7.1 recovery already wrote are not rewritten.
- **Why 5,400 s.** The Sonnet 5 auditors the 2,700 s cap was set for finished in 8 to 34 minutes.
  On 2026-09-30 a teammate's Sonnet 5.5/max auditors finished in about 20 to 35 minutes, but on the
  Goepp et al. audit one role took 41 minutes (344,821 output tokens, USD 7.07) and the other four
  were still writing `REPORT.md` and `result.json` when 2,700 s stopped them, twice. 5,400 s is
  twice the old cap; the USD 15 budget is unchanged and, at about USD 0.17 a minute, binds near the
  same point.
- **Every existing audit keeps 2,700 s for its whole life.** The timeout enters no cache key, input
  identity or receipt, but it is part of the exact `phase_1.runtime` contract and of the snapshot
  bytes `run.json` binds, so an audit keeps the value it froze (`scripts/phase1_timeouts.py`
  admits 2,700 and 5,400 only). Its snapshot says 2,700, so it gets the 2.7.1 contract byte for
  byte. An audit stopped by the old accounting (both pre-inference requests spent) cannot be
  continued; re-audit the paper from a fresh intake on 2.7.2.
- **New audits: 5,400 s.** `init_run.sh` freezes it from the shipped policy, in
  `phase_1.runtime.timeout_seconds` and `phase_1.provider_timeout_seconds`, and prints it
  (`phase-1 timeout: 5400 seconds per auditor call`). `run_phase1.py` and `run_verifier.py` use the
  audit's own value, and refuse a `--timeout-seconds` that differs from it.
- **Claude CLI.** CLI 2.1.281 does not recognise `claude-sonnet-5-5` (stderr
  `[claude-code:unrecognized_model]`) and serves it with a 200,000-token context window, which
  makes Sonnet 5.5 auditors compact repeatedly. Use 2.1.285 or later: `claude -p --model
  claude-sonnet-5-5 --output-format json "Reply OK"` should print no warning and report
  `contextWindow` 1000000.
- **Mixed teams.** A teammate on 2.7.1 cannot start an audit once the canonical copy is 2.7.2, and
  2.7.1 refuses a 2.7.2 audit's snapshot ("audit-scoped phase_1.runtime differs from the packaged
  execution contract") before any provider call. An audit started on 2.7.1 continues on 2.7.2.

## Upgrading in-flight audits to 2.7.1

2.7.1 raises the Phase-1 capability probe's token bound from 150,000 to 350,000 normalized tokens,
for new audits only (CHANGELOG, 2.7.1). The bound is part of the probe contract, which keys the
probe cache, every auditor's input identity and receipt, and the verifier's input. So, like its
Claude route, every audit keeps the bound its own `pipeline-policy.snapshot.json` froze. An audit in
any phase can take the upgrade, and a published audit needs nothing.

- **Why 350,000.** As of 2026-09-30T09:02Z the invocation ledgers under `runs/` held 39 capability
  probe calls in 28 audits (cache replays not counted):
  - *Passed:* 26, one per audit, at 67,830 to 149,259 normalized tokens (median 122,621; 5 to 8
    turns). 25 ran on Sonnet 5 (median 122,675) and 1 on Sonnet 5.5, a teammate's audit started on
    2.7.0, at 93,846. The Sonnet 5.5 live canary, run outside `runs/`, used 124,283.
  - *Over the bound:* 11, in 8 audits, failed "capability probe exceeded 150000 normalized tokens"
    at 160,004 to 254,191 (median 178,045; 8 to 11 turns). 3 of them (166,079, 253,086 and 254,191)
    also failed the probe-contract check. The other 2 failures were "Not logged in", at 0 tokens.
  - *Retries:* every one of those 8 audits passed on a re-run probe, 6 after one over-bound probe, 1
    after two and 1 after three, each within 15 minutes of its first; the passing probes used
    119,498 to 132,694.

  So 11 of the 37 probes that used tokens paused their audit. The programme lead chose 350,000: about
  1.4 times the worst probe observed (254,191) and about 2.3 times the worst passing one (149,259).
  Under it none of the 11 would have failed on tokens; the 3 contract failures would still fail. It
  still bounds a probe that loops on tool calls: the largest probes averaged about 23,000 normalized
  tokens a turn (254,191 in 11 turns), so one that keeps calling tools passes 350,000 at roughly 13
  to 15 turns, where a passing probe takes 5 to 8. 300,000 and 500,000 were considered and not
  shipped.
- **Every existing audit keeps 150,000 for its whole life.** That covers every audit initialised on
  2.7.0 or earlier, on Sonnet 5 or on Sonnet 5.5. Every policy snapshot under `runs/` froze 150,000
  in both leaves, as of the time above: all 246 snapshot files, the 28 audits' own and 218 copies
  (archived, in-run and release copies, and an abandoned intake's). Its snapshot says 150,000, so it
  gets the 2.7.0 probe contract, probe keys, `PROBE_CONTRACT.json`, prompt and messages byte for
  byte. Cached probes and role completions replay, and verifier records re-admit. Nothing is re-run
  and nothing is re-keyed.
- **New audits: 350,000.** `init_run.sh` freezes it from the shipped policy, in
  `phase_1.runtime.probe_max_normalized_tokens`, and prints it (`probe bound: 350000 normalized
  tokens`); `run_phase1.py --probe-only` prints it as `probe_max_normalized_tokens`. A new audit's
  probe above 350,000 pauses it the same way as below, with "capability probe exceeded 350000
  normalized tokens", and the same two remedies apply.
- **The runtime admits 150,000 and 350,000 only** (`scripts/probe_bounds.py`). A snapshot with any
  other bound is refused before any provider call, by `run_phase1.py` and by `run_verifier.py`
  ("... is not a packaged probe bound (150000 or 350000)"). A frozen snapshot is never edited.
- **An existing audit whose probe exceeds 150,000 still pauses.** It stops at the probe, before any
  auditor call: exit 4, with "PAUSED: Phase-1 capability probe failed: Phase-1 capability probe
  failed: capability probe exceeded 150000 normalized tokens", and `phase1/status.json` says
  `capability_probe_failed`. The verifier re-probes only when the Claude CLI version or the audit's
  path changed since Phase 1 (the probe key binds both), and it pauses the same way ("PAUSED:
  capability probe failed: ...", exit 4). There are two remedies; try the first one first.
  - *Run the probe again.* Re-run the same command, `python3 scripts/run_phase1.py "$AUDIT_DIR"` (or
    `run_verifier.py`). A failed probe is not cached and uses none of any role's attempts, so each
    re-run costs one more probe call (the 11 over-bound ones cost $0.21 to $0.48 each) and no
    auditor attempt. The first one that fits continues Phase 1 (`pause_end` is journaled). This is
    what the 8 audits above did. If every probe on one machine exceeds 150,000 (a CLI whose own
    context has grown), retrying will not help: continue the audit from a machine whose probe fits.
  - *Re-initialise with `init_run.sh --force`.* A forced re-init rebuilds `run.json` from its
    arguments, so pass the audit's own id, DOI and title again, read from its current `run.json`
    (`jq -r '.audit_id, .paper_doi, .paper_title' "$AUDIT_DIR/run.json"`; leave out `--doi` when
    `paper_doi` is null):

    ```bash
    bash scripts/init_run.sh "$AUDIT_DIR" --force --audit-id <its audit_id> --doi <doi> --paper-title <title>
    ```

    Without them the DOI and title are dropped (no event sets them later) and a new audit id is
    made. It re-freezes the whole shipped policy, not the bound alone: Sonnet 5.5, GPT-6.1 Sol
    critics, the shipped round cap and 350,000. It archives the snapshot, the journal and `phase1/`,
    and re-runs all five auditors, so it suits an audit that has not yet paid for Phase 1.
- **Mixed teams.** `init_run.sh` refuses a skill copy whose `VERSION` differs from the canonical
  one, so a teammate on 2.7.0 cannot start an audit once the canonical copy is 2.7.1. An audit
  started on 2.7.0 continues on 2.7.1 unchanged. A 2.7.1 audit cannot be continued on 2.7.0, which
  refuses its snapshot ("audit-scoped phase_1.runtime differs from the packaged execution contract")
  before any provider call.
- **A leftover snapshot keeps its bound, and init says so.** `init_run.sh` compares only the Claude
  routes of a snapshot left without a `run.json`, and takes the round cap from it. One frozen by
  2.7.0's `record_invocation.py --init-policy` on the shipped routes is therefore reused with
  150,000: init prints `probe bound: 150000 normalized tokens` and a WARNING that names the shipped
  350,000 and prints the `--force` command above, filled in for that audit. Remove the leftover
  before init, or run that command before Phase 1, if the new audit should get 350,000. A leftover
  whose bound is neither 150,000 nor 350,000 is refused, with the leftover's `rm` remedy, because no
  runner would ever start that audit.

`gate_set_version` and `toolkit.scripts_sha256` differ for audits initialised on 2.7.1, as for every
release.

## Upgrading in-flight audits to 2.7.0

2.7.0 moves new audits from Claude Sonnet 5 to Claude Sonnet 5.5 (CHANGELOG, 2.7.0): the
coordinator, the five Phase-1 auditors, their capability probe and the post-fan-out verifier. It also
moves new audits' four critics, their critique model probe and the targeted adjudicator from GPT-5.6
Terra to GPT-6.1 Sol. Every Claude role and every critic role takes its route from the audit's own
frozen `pipeline-policy.snapshot.json`, never from `config/pipeline-policy-v2.json` after init, so an
audit in any phase can take the upgrade. A published audit needs nothing.

- **New audits: Sonnet 5.5, spawned by its full id.** `init_run.sh` freezes `sonnet-5-5`, and every
  Claude call runs `claude --model claude-sonnet-5-5`, never an alias: on Claude CLI 2.1.206 the
  alias `sonnet` still serves `claude-sonnet-5`. A call is admitted only when its usage names exactly
  one `claude-sonnet-5-5` primary (a date suffix or a context tag such as `[1m]` is allowed) and no
  other Sonnet id; anything else is refused with today's served-model error, naming Sonnet 5.5. The
  account needs Sonnet 5.5. The full id was tried on CLI 2.1.206 only, so run the live canary
  (*Live canary before the first audit*) for each machine's first new audit.
- **Every existing audit keeps Sonnet 5 for its whole life.** Its snapshot says `sonnet-5`, so it
  keeps spawning `--model sonnet` with the same prompts, packets, probe keys, input hashes, receipts
  and loose served-model check as on 2.6.10. Nothing is re-run and nothing is re-keyed: a Phase 1,
  coordinator or verifier batch in flight continues on 2.7.0, cached probes and role completions
  are reused, and published verifier records re-admit. The critic prompts, the critique driver and
  its runtime helper, the post-delta receipt's files and the document builders did not change, so
  rounds, receipts and document manifests are untouched. Finishing such an audit still needs an
  account with Sonnet 5, and for its critics one whose Codex can still use the critic model it
  froze (below).
- **New audits' critics: GPT-6.1 Sol; every existing audit keeps the critic route it froze.**
  `init_run.sh` freezes `gpt-6.1-sol` for the critics and the critique model probe (effort high)
  and for the targeted adjudicator (effort max), and the critique driver dispatches
  `codex exec ... -m gpt-6.1-sol`. An audit initialised on 2.6.1 to 2.6.10, or before 2.6.0, froze
  `gpt-5.6-terra`; the one audit initialised on 2.6.0 froze `gpt-6-sol`. `run_critique.sh` reads the
  route from the audit's snapshot, so each finishes on its own route with the same snapshots, probe
  keys and receipts as on 2.6.10 (2.6.0 and 2.6.1 moved the route the same way); finishing it needs
  an account that can still use that model. A new audit needs the ChatGPT app's Codex CLI at 0.159.0
  or newer (update the ChatGPT app; `python3 scripts/codex_cli.py --explain` shows the version the
  skill picks) and an account that offers `gpt-6.1-sol`. On 2026-09-29 20:53Z CLI 0.158.0-alpha.2.1
  got HTTP 400 "The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account" on
  the programme account; on 2026-09-30 CLI 0.159.0 listed the model, and the driver's exact command
  (`codex exec --skip-git-repo-check --ignore-user-config --color never --json --ephemeral -m
  gpt-6.1-sol -c model_reasoning_effort=<high|max> -s read-only ...`) returned correct structured
  output at high and at max, about 29k input tokens each. That is the live canary; each machine's
  first new audit repeats it in the critique model probe, which `run_critique.sh 1` makes before any
  critic call, so a CLI or account without Sol stops there (`FATAL: model probe failed (permanent,
  unsupported_model): ...; no critic calls were started`) with the provider's words and nothing
  spent on critics. The remedy is to update the ChatGPT app, or sign the CLI in with an account that offers
  Sol, and re-run the round. (Until 2.7.5 the remedy could also re-pin the audit to Terra; 2.7.5 refuses
  that, *Upgrading in-flight audits to 2.7.5*, and `new_audit.sh` now checks the account before an audit
  starts.)
- **A 2.7.0 audit is continued, every round, from an account and a CLI that offer `gpt-6.1-sol`.**
  The passing critique probe is cached in the audit (`critique/.runtime/probes/`), keyed on the
  model, the effort, the Codex CLI's version string and the probe contract, not on the account, and
  round 2 and every rerun on that CLI version reuse it. So an account without Sol that picks up the
  audit after a probe passed on its CLI version is not stopped at the probe: each critic it
  dispatches is refused, `[fail ] critic_<role> permanent failure on attempt N (unsupported_model):
  The 'gpt-6.1-sol' model is not supported ...`, then `FATAL: exact role quorum failed`. Each
  refusal spends one of that role's attempts and nothing else. Re-run the same round from an
  account with Sol (add `MAX_TRIES=<n>` once a role's attempts are used up); completed roles replay
  from the cache. Since 2.7.5 no audit moves off the shipped critic route, so finishing on the current route,
  from an account with Sol, is the answer; a new audit would pay for Phase 1 again.
- **The 2.6.0 audit keeps dispatching `gpt-6-sol`.** *Athlete burnout and biomarkers* has been
  stopped at its round-1 critique probe since 2026-09-23, when its operator's account refused
  `gpt-6-sol`, and 2.7.0 re-runs that probe on `gpt-6-sol`. No review has completed, so if the
  account still cannot use it, its operator re-pins it to the shipped route,
  `python3 scripts/repin_critic_route.py "$AUDIT_DIR" --reason "<why>"`, which moves it to `gpt-6.1-sol`
  and needs an account and a CLI (0.159.0 or newer) that offer it. Since 2.7.5 no other target is accepted.
- **The Phase-1 probe's token bound: 150,000 for every audit initialised on 2.7.0 or earlier.** 2.7.0
  kept it at 150,000 normalized tokens for new audits too. It is part of the probe contract, which
  keys every auditor and verifier input. 2.7.1 raises it to 350,000 for new audits only; every
  existing audit keeps 150,000 for life (*Upgrading in-flight audits to 2.7.1*, which has the
  measurement: 11 of the 37 probes under `runs/` that used tokens went over 150,000, the worst at
  254,191). A probe above its audit's bound pauses the audit at the probe, before any auditor call,
  and `run_phase1.py --probe-only` shows it.
- **`init_run.sh --force` re-pins the model.** It archives the snapshot and freezes the shipped
  policy again, so forcing a re-init of an audit initialised before 2.7.0 (the recovery for a
  Phase-1 role that exhausted its attempts) restarts it on Sonnet 5.5 and GPT-6.1 Sol critics, with
  every other shipped value (since 2.7.1 the 350,000 probe bound too), and re-runs all five auditors.
  Pass its `--audit-id`, `--doi` and `--paper-title` again, since `run.json` is rebuilt from them (the
  full command is in *Upgrading in-flight audits to 2.7.1*). When environment or harness problems spent
  the attempts, raise `I4R_MAX_ATTEMPTS` instead; the audit then stays on Sonnet 5 and the critic
  route it froze. A forced re-init that fails before it writes the new `run.json` puts the archived
  snapshot back ("restored the archived one to ..."), so the audit stays on the routes it had.
- **Alias-remapping variables never reach a Claude call.** The runners start every Claude CLI
  subprocess without `ANTHROPIC_MODEL` and `ANTHROPIC_DEFAULT_SONNET_MODEL`
  (`scripts/claude_routes.py`, `REMAPPING_ENV`). A Sonnet 5 audit spawns the alias `sonnet`, so one
  exported variable would otherwise move it to another model while its rows still said `sonnet-5`.
  Your shell keeps them; the environment is part of no identity.
- **A snapshot left by an init that did not finish is refused, never reused.** Up to 2.6.10
  `init_run.sh` froze the snapshot before its model and skill-version checks, so a refused init left
  one behind and the next init in that directory reused it. 2.7.0 decides every refusal first. When
  init stops before `run.json` binds the snapshot, it removes the snapshot only if this init created
  it and no `run.json` binds it, so a snapshot another process made is left alone, and two inits of
  one audit directory run one at a time (below). A snapshot with no
  `run.json` and no journal beside it, whose Claude routes are not the shipped policy's, is refused
  with "freezes a Claude route this skill does not ship ... remove it (rm ...)": remove that file
  and re-run the command. One that matches the shipped policy (for example from
  `record_invocation.py --init-policy`) is reused as before.
- **An audit whose `run.json` is missing is not initialised again.** `run.json` is a projection of
  `run.events.jsonl`, and `log_event.sh` answers a missing `run.json` with "run init_run.sh first".
  For an audit with a journal, Phase-1 work under `phase1/` or an invocation ledger, `init_run.sh`
  (without `--force`) refuses with "is an initialised audit ... whose run.json is missing" and changes
  nothing. Rebuild `run.json` from the journal instead, with no provider call:
  `python3 scripts/record_invocation.py --recover "$AUDIT_DIR"`. The rebuilt `run.json` binds the
  audit's own snapshot, which stays on Sonnet 5 for an audit started before 2.7.0. Never remove the
  snapshot of such an audit. `--force` starts it over on Sonnet 5.5 and GPT-6.1 Sol. A `phase1/`
  that holds only `run.lock` and `status.json`, or nothing, is not Phase-1 work.
  `run_phase1.py` leaves exactly that when it is run before init, the `--probe-only` canary
  included, and refuses with "run scripts/init_run.sh on the audit first"; init then initialises
  the workspace as usual.
- **Two inits of one audit directory run one at a time.** `init_run.sh` holds an fcntl lock on
  `<audit-dir>/.init.lock` from its `run.json` check until the journal is initialised, or until its
  cleanup has run when it stops early. A second init prints "waiting: the init lock of this audit
  (...) is held by another process". Once the first is done, the second refuses with "run.json
  already exists", or initialises if the first stopped before binding a snapshot.
  `lsof <audit-dir>/.init.lock` names the holder. The file is removed when init ends. One left by a
  killed init is harmless: the next init takes it over and removes it.
- **Init commands.** The documented command passes `--auditor-model sonnet-5-5`, and `new_audit.sh`
  prints the model, provider and cap the policy ships; `init_run.sh` refuses `sonnet-5` for a new
  audit. Omitting the flag takes the policy's value.
- **Mixed teams.** `init_run.sh` refuses a skill copy whose `VERSION` differs from the tree's
  canonical one, so a teammate still on 2.6.10 cannot start an audit once the canonical copy is
  2.7.0. That refusal now comes before anything is written; a 2.6.10 init still freezes its snapshot
  first, and the 2.7.0 init then refuses that leftover as above. An audit started on 2.6.10 stays on
  Sonnet 5 whichever machine continues it. A 2.7.0 audit cannot be continued on 2.6.10, whose
  runners dispatch only Sonnet 5 and refuse its snapshot before any provider call. Hand a 2.7.0
  audit's critique rounds only to a teammate whose account and Codex CLI offer `gpt-6.1-sol`: the
  cached critique probe does not re-check the account (above).
- **The interactive session.** Pick Sonnet 5.5 with `/model` when starting a new audit (the
  CHEATSHEET). That session is not a policy role and is not route-checked; `log_event.sh model`
  records what it serves.

`gate_set_version` and `toolkit.scripts_sha256` differ for audits initialised on 2.7.0, as for every
release.

## Upgrading in-flight audits to 2.6.10

2.6.10 changes only how the Codex CLI is found (CHANGELOG, 2.6.10), so an audit in any phase can take
it. A published audit needs nothing. The one cost: a critique round relaunched after the upgrade
that reaches targeted adjudication pays for one more adjudicator call, because the target snapshot's
address binds `run_critique.sh`. Round snapshots, authorizations and cached critic results do not
bind the driver, and are reused. The model-probe cache is keyed on the CLI's version string, so the
first round an audit runs on a different CLI version pays for one probe; that is not new in 2.6.10,
and an app update that changes the bundled CLI costs it whichever skill version runs the round.

## Upgrading in-flight audits to 2.6.9

2.6.9 merges the QA release 2.6.8 (live since 2026-09-27) with four fixes built on 2.6.7 (CHANGELOG,
2.6.9), and raises the critique round cap from 5 to 7 for new audits. A published audit needs nothing
until it is re-filed; re-filing one (for example with the corrected Appendix) takes the chain in
*Appendix A rendering and document manifests*, not just a rebuild and staging.
Of the files the post-delta receipt hashes, only its schema changed (it now admits rounds 6 and 7),
and `validate_round_projections.py` changed only to call the round-cap check. Earlier rounds'
receipts and integrated rounds therefore validate unchanged, but the latest round's receipt of an
audit in flight is stale once (below). The verifier's prompt is unchanged, so verifier re-admission
stays valid. To re-file a published comment with the corrected Appendix, follow the chain in *Appendix A
rendering and document manifests*. An audit that is part-way through is cheapest to upgrade between phases, so keep
a copy of the 2.6.8 skill until each in-flight phase below has finished on it. What each phase costs
when the upgrade lands inside it:

- **Phase 1: finish it on the old tree.** The Phase-1 packet hash binds `reference/checklist.md`,
  `reference/brief_template.md` and the auditor prompt (which gained `AUDITOR_PII_REPORTING`), all
  of which changed. A Phase 1 resumed after the upgrade re-dispatches, and pays again for, every role
  that had completed under 2.6.8; 2.6.8's portable-key and alias migration cannot reuse those
  receipts, because the prompt is part of the key. Finish Phase 1, or run `run_phase1.py
  --recover-stranded`, with the 2.6.8 tools first.
- **Coordinator.** The coordinator snapshot binds the changed references, and
  `recover_coordinator.py` requires the current prompt. Do not re-invoke `run_coordinator.py` on an
  audit whose coordinator completed under 2.6.8 (it refuses: "already completed for different
  immutable inputs"), and finish or recover a 2.6.8 coordinator attempt with the 2.6.8 tools. A
  deposit without participant-code-shaped file names gets byte-identical coordinator packets from
  the manifest masking.
- **Critique: finish any round already dispatched on 2.6.8 before upgrading.**
  `prompts/role_fairness.txt` changed, and it is part of the input identity of every round that
  includes the fairness critic (round 1 always does). A relaunch of such a round on 2.6.9, the usual
  recovery for a round that failed quorum, journals a second `round_authorized`, freezes a new
  snapshot and pays again for every critic of the round, including those that had completed; they
  re-run at attempt 2, one retry down. Relaunch a failed round on 2.6.8 until it completes. A round
  planned but not dispatched before the upgrade needs a new plan and authorization (from round 2,
  fresh post-delta gates too), and its cached critic results are not reused. Rounds already
  integrated validate unchanged, because they bind the prompt text frozen in their own snapshot.
- **Critique, after the upgrade.** Expect one extra paid model probe per audit: the probe cache key
  binds the runtime helper, which changed. A round relaunched after the upgrade that reaches
  targeted adjudication pays for one more adjudicator call, because the target snapshot's address
  binds the helper and `run_critique.sh` and the cached adjudication is missed. A snapshot whose
  directory mode had already drifted is re-frozen on the first relaunch and journaled once as a
  `snapshot_mode_refrozen` note; it costs nothing.
- **Post-delta gates: re-run the latest round's once.** `schemas/v2/post-delta-gates.schema.json`
  admits rounds above 5 now, and every receipt binds that file's hash. A receipt made under 2.6.8 for
  a round gated but not yet run, or for the last round before finalize, is refused once with
  "post-delta receipt was produced by stale gate code (the skill was updated); regenerate it:
  python3 scripts/run_post_delta_gates.py ...". Run the printed command; it makes no provider call.
  Earlier rounds' receipts keep the gate code they were made with and are replayed as before.
- **Round cap.** A new audit gets the cap of 7. An audit initialised earlier keeps the cap of 5
  frozen in its snapshot. The driver now reads the cap there (it was a literal), and the journal,
  finalize and publication read it too. To continue such an audit past round 5, see *Raising the
  round cap of an existing audit*. `init_run.sh` takes its default cap from the snapshot.
- **A finalized audit is closed.** `run_critique.sh` and the journal refuse any new round on an
  audit whose last finalize has no later journaled reopen (`reopen_audit.py`, or
  `amend_round_cap.py --reopen`). *Correcting a published comment* gains that step. Re-logging a
  `round_result` or `round_integrated` before a re-finalize is not a new round and stays allowed.
- **Release: rebuild the documents.** Every `document_build_manifest.json` written by 2.6.8 or
  earlier is stale, because `build_docx.sh` and `document_release.py` changed again. Finalize (step
  4c), `stage_release.sh` and `publish_completed.sh` refuse it with "document manifest builder code
  identity differs from the release verifier (the skill's document scripts changed since this
  build). Rebuild the document with build_docx.sh, then re-run finalize_run.sh before staging or
  publishing: rebuild with scripts/build_docx.sh ...". Run `build_docx.sh` before finalize.
  `stage_release.sh` stages only into a fresh directory, so move an audit's existing `release/`
  aside before restaging it. Staging and publication now run check 3c (see *Identifying values in
  the audit's own outputs*). It writes nothing value-derived to disk. A comment output that quotes a
  deposit value (a full name, date of birth, IPv4 or IPv6 address, postal address or phone number),
  or carries a public IP address outside the allowlists, must be reworded before it can be filed; in
  the staged copy of a record nobody can reword (a Phase-1 auditor report, a critique record, a work
  record) the match is masked and listed in `RELEASE_VALUE_REDACTION.txt` instead. A one-word name
  match only warns.
- **Enforcement and provenance.** The finalize and integrate checks on disclosure findings (see
  *Deposits containing personal data*) are errors when `run.json` `protocol_version` is 2.6.9 or
  later (`disclosure_policy.ENFORCED_FROM`), or when a 2.6.9 skill reopened the audit. An audit
  initialised on the live 2.6.8, whose SKILL.md and prompts still told the operator to pause for a
  disclosure decision, gets them as warnings, like every older audit. So the food-insecurity SJMSS
  audit (published on 2.6.8 with `governance.pii_present` true, five flagged comment findings, and
  empty `disclosure.flags` and `disclosure.statement` with urgency `none`) re-finalizes with a
  warning, and the in-flight 2.6.8 audits are not refused at finalize for unsettled disclosure rows
  or comment-level fields. Settle them anyway: the warning names each one. `gate_set_version` and `toolkit.scripts_sha256` differ for audits initialised on 2.6.9,
  as for every release.
- **From 2.6.7 directly.** First take 2.6.8's step: `run_post_delta_gates.py` changed in 2.6.8, so
  re-run the latest round's post-delta gates (the refusal prints the command), then re-finalize and
  stage again if you had already done so. The costs above then apply with 2.6.7 in place of 2.6.8.

## Dependencies

| Needed for | Requirement |
|---|---|
| Core workflow | `bash`, Python 3 with `jsonschema`, `jq` |
| Claude coordination and auditors | Claude Code CLI signed in to an account with Sonnet 5.5, which new audits spawn by its full id (`--model claude-sonnet-5-5`); finishing an audit initialised before 2.7.0 also needs Sonnet 5, which it spawns as `--model sonnet`. A subscription (OAuth/keychain) login works; the runners use `--safe-mode`, not `--bare`, because bare mode refuses OAuth and keychain credentials. `ANTHROPIC_API_KEY` also works. |
| External critics | Codex CLI 0.159.0 or newer, signed in to an account that offers GPT-6.1 Sol (`gpt-6.1-sol`), which new audits' critics and adjudicator run; 0.158.0-alpha.2.1 was refused it. An audit initialised before 2.7.0 runs the critic model it froze: `gpt-5.6-terra`, or `gpt-6-sol` for the one audit initialised on 2.6.0. `scripts/codex_cli.py` picks the newest working copy among the ChatGPT desktop app's bundled CLI (`/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex` since the app update of 2026-09-28, earlier `Contents/Resources/codex`) and every `codex` on `PATH`; `python3 scripts/codex_cli.py --explain` shows the candidates and the choice. Set `CODEX` to choose one. Since 2.7.5 `new_audit.sh` checks the account before an audit starts (`scripts/check_critic_access.py`; *Upgrading in-flight audits to 2.7.5*) |
| Statistical/package inspection | Python readers required by the deposit; commonly pandas, scipy, statsmodels, openpyxl, pyreadstat |
| Verification notebook | R, `rmarkdown`, and format readers such as `haven`, `readxl`, `jsonlite`, or `jmvReadWrite` |
| DOCX/PDF build and visual gates | `pandoc`; LibreOffice (`soffice`, required, no substitute renderer is accepted); Poppler **21.06 or newer** — the release privacy gate calls `pdfinfo -custom`, which older Poppler rejects |

Nothing in the skill directly calls a hosted API. External critics use the authenticated Codex CLI
session and its available credits.

`build_docx.sh` also looks for `pandoc` in a conda environment named `i4rdoc`, so on a machine whose
system Poppler is too old, `conda create -n i4rdoc -c conda-forge pandoc poppler` and prepending
that environment's `bin` to `PATH` satisfies both requirements without touching the base environment.

## Audit directory naming

The audit path must not contain `?`, `*`, `[`, `]`, `{`, `}` or `\`. The Claude CLI reads
`sandbox.filesystem` entries as glob patterns, so a rule containing any of them matches nothing:
every sandboxed Bash read and write is denied, the `denyRead` protections on `paper/` and `pkg/`
go inert at the same moment, and the Write tool refuses such a path outright. Spaces, `:`, `~`,
`(`, `)` and `!` are safe, so ordinary article titles are fine — but a title ending in a question
mark is not (14 of this tree's 171 papers). `setup_workspace.sh` refuses such an audit dir at
intake and Phase 1 refuses it again in preflight, before any provider call.

You do not have to rename the paper folder. `new_audit.sh` drops those characters from the RUN
folder name (`runs/<journal>/Can automatic reactions mirror exercise dependence/audit`) and passes
the true paper folder as `I4R_PAPER_DIR`, so the reservation is claimed under the paper's true name
and intake records the true `<journal>/<paper>` in `paper_source.json`, which `publish_completed.sh`
treats as authoritative. Calling `setup_workspace.sh` directly works the same way, but skips
`new_audit.sh`'s critic-access check (2.7.5): run `python3 scripts/check_critic_access.py` first and stop if it
refuses:

```bash
I4R_PAPER_DIR="data/<journal>/Can automatic reactions mirror exercise dependence?" \
  bash scripts/setup_workspace.sh \
    "runs/<journal>/Can automatic reactions mirror exercise dependence/audit" \
    "<paper.pdf>" "<package.zip>"
```

## Tree layout (2.5.0)

```
<root>/
  data/<Journal>/<Paper>/        the article PDF and the replication package. Inputs only; the
                                 skill never writes here, and setup_workspace.sh refuses an audit
                                 dir placed under it
  runs/<Journal>/<Paper>/audit   the working audit (release staging goes beside it, e.g. ../release)
  comments/<Journal>/<Paper>/    the published comment: PDF, plus DOCX when the manifest binds one
  metadata/<Journal>/<Paper>/    the published record: run.json, comment.json, finalization
                                 receipt, document manifest, claims script, release-manifest.json
  registry/  reservations/  skill/   as before
  old_runs/                      everything produced before 2026-09-22, by its old path, and superseded
                                 publications (see 'Correcting a published comment')
```

`scripts/tree_layout.sh` is the one place that knows this. Every script that needs a tree path
sources it, and each accepts the legacy form too (`<root>/<Journal>/<Paper>/audit`, published into
`Completed reports/`), so an audit started before 2.5.0 can be finished and published into the new
folders. `bash scripts/tree_layout.sh i4r_root_of_audit <audit-dir>` answers "which tree is this
in" from the shell. Start a new audit with `bash scripts/new_audit.sh "data/<Journal>/<Paper>"`.
`scripts/` in the documented commands is the skill's own `scripts/` folder: run `new_audit.sh` from the tree
root by the skill's path, and use the absolute paths it prints for the next steps.

## Running a batch

Nothing in this skill takes a tree root except `status.py` and `reserve_paper.sh`. Across many
papers the failure signature is an audit directory that simply stops being written to, and nothing
else surfaces it.

```bash
python3 scripts/status.py <tree-root>            # every audit, one line each
python3 scripts/status.py <tree-root> --stuck    # only the ones needing attention; exit 1 if any
bash scripts/reserve_paper.sh reap <tree-root>   # stale claims, dry run; --force to release
```

`status.py` walks `runs/` (and the legacy journal folders), never `data/`, `metadata/` or
`old_runs/`. It is read-only, takes no lock and starts no provider call, so it is safe against a live
batch. It flags an unfinished audit idle past `--idle-days`, an audit holding a lock while idle
(the signature of a killed process), Dropbox conflicted copies inside the audit tree, and a
finished audit whose reservation was never released.

`reserve_paper.sh reap` releases only **your own** claims whose audit shows no journal activity
inside the stale window. Another machine's claim is reported and held, because `paper` in a
reservation is an absolute path on the claiming machine and this machine cannot see their audit
directory at all — "no audit directory" there means "not mine to judge". `I4R_REAP_ANY_OWNER=1`
overrides, and should only be set after actually asking them.

### When something dies mid-Phase-1

A killed process — sleep, OOM, reboot, a mistimed Ctrl-C — leaves the attempt in a non-terminal
state, and the guard that stops a duplicate paid call then refuses to dispatch anything for that
audit until it is cleared. Under metadata 2.1 that state is **`provider_requested`**: the runner
holds it there for the whole call and journals `inference_start` only once the provider stream
proves acceptance, so a 2.1 strand never reaches `inference_started` (legacy pre-2.1 workspaces
still do, and cannot be recovered — they carry no journal). If no response was retained:

```bash
python3 scripts/run_phase1.py "$AUDIT_DIR" --recover-stranded
```

It terminalizes the attempt as a failed row with `attempt: null` and charges **nothing** against the
role's budget: a metadata-2.1 strand sits at `provider_requested`, before any inference start was
accepted, so no attempt was ever allocated (the `--help` text and the journal say the same). It
still counts toward the limit of two pre-inference requests per role, which `I4R_MAX_ATTEMPTS`
does not raise. It refuses outright if a response *was* retained — that case goes through the
ordinary received-response path rather than discarding real work.

### Journal notes for the round sequence

`run_critique.sh` journals `round_integrated` itself when a round's critique completes; it marks the
critique projection, not the coordinator's integration, so a final clean round left unintegrated
(SKILL.md section 4) still carries it. In the integration sequence, every approved row needs
`report_section` and `verification_snippet` in its decision, and the second projection after the
second `approve_settled.py` must leave `REPORT_final.md` unchanged (the first can rewrite Appendix A
and the claim tags, which stales the first approval).

`human_effort` takes either `phase=<phase> person_minutes=<n>` (measured operator time) or
`notes='<text>'`; an unattended run logs `notes='unattended run'`. Finalize still warns (the comparison has no
numerator), but the warning then records the documented reason instead of reading as an oversight.

### Renumbering to the published scheme

`build_draft_registry.py` emits role-scoped finding ids and `generate_finding_artifacts.py`
requires contiguous `F1..FN`, so every audit needs one conversion. Doing it by hand means keeping
the report markers, the registry ids, every `ledger_disposition.target` and every
`dependencies.finding_ids` in step:

Run it once, right after `run_coordinator.py` and before round 1:

```bash
python3 scripts/renumber_findings.py "$AUDIT_DIR" --dry-run
python3 scripts/renumber_findings.py "$AUDIT_DIR" --apply --also report/ADJUDICATION.md
```

`--also report/ADJUDICATION.md` carries the coordinator's `<!-- adjudication:ID -->` decision
markers to the published ids; without it the log keeps the role-scoped draft ids.

Numbering follows marker order in the report; MERGED and EXCLUDED rows go past the last INCLUDED
number. Critic verdicts join the registry by id, so renumbering after a critique round orphans that
round's verdicts. If an audit was renumbered late anyway, integrate the round under its old ids
first, then renumber with `--also` for that round's own pre-integration snapshots
(`critique/round_N/REPORT_BEFORE_INTEGRATION.md`, `FINDINGS_BEFORE_INTEGRATION.jsonl`). Earlier rounds'
snapshots stay as they are: a later round's frozen snapshot binds them, and make_diff_packet translates
their ids through `work/id_map_history.jsonl`. A file left out of that run is finished
with `--apply --late-also --also <file>`, which applies the last renumber's own map and skips any
file that run already wrote; a plain `--apply` on an already-renumbered registry does nothing.

A finding withdrawn at integration (status `rejected` with ledger_disposition EXCLUDED or MERGED) that is not
the last number leaves
a gap the projection refuses. Remove its marker from the report in step 3, check with `--dry-run`
that ids change (a withdrawn last number leaves no gap; the dry-run may still reorder rows withdrawn
earlier, and either skipping or applying that renumber is safe), then renumber at once, before
approving:

```bash
python3 scripts/renumber_findings.py "$AUDIT_DIR" --apply \
  --also critique/round_N/REPORT_BEFORE_INTEGRATION.md --also critique/round_N/FINDINGS_BEFORE_INTEGRATION.jsonl \
  --also critique/round_N/RESPONSE_notes.md --also report/ADJUDICATION.md
# N >= 2: leave round N-1's before-files alone -- round N's frozen snapshot binds their hashes (finalize
# re-checks them), and make_diff_packet translates their ids through work/id_map_history.jsonl.
# Every round's RESPONSE_notes.md is rewritten automatically (earlier rounds' notes included).
```

The round's verdicts are already integrated, so nothing is orphaned; the notes and adjudication log
are rewritten to the published ids. The round's decisions file and critic outputs are not, so
`integrate_round.py` refuses to re-plan or re-apply that round afterwards.
Snippet sources are not rewritten: change each renumbered finding's `expect("C-<n>", ...)` and
`check("C-<n>", ...)` ids to its new number (the projection refuses a snippet whose literal ids are
not its row's own, and the notebook a chunk that records another claim's id).

### What an audit actually costs

`run.json` records only the metered provider calls the pipeline itself makes. It does **not**
record the orchestrator session that drives the pipeline, which on a measured run consumed roughly
ten times the recorded figure. Budget from the provider console, not from `run.json`, and on a
subscription expect plan limits rather than dollars to be the binding constraint. Extra critique
rounds are the largest controllable cost: the round target and cap are pinned in
`config/pipeline-policy-v2.json` and asserted in `config/check_policy_drift.py`, so change both
together, never per-run. Each audit freezes the cap in its snapshot at init (7 from 2.6.9); an older
audit keeps its frozen cap until `amend_round_cap.py` raises it with a journaled reason (*Raising
the round cap of an existing audit*). A later round usually routes completeness plus one specialist,
so rounds 6 and 7 cost about two to four critic calls each, plus the probe if its cache missed.

## Deposits containing personal data (2.6.9)

Programme policy since 2026-09-27: a paper is never blocked, skipped, parked or paused because its
replication deposit holds personally identifying information. It is audited normally, and the
personal data is reported in the comment as a disclosure-flagged finding with its own section and
Appendix A row. The finding names the identifier kind, the package-relative file and column (or the
directory and a file count when file names are themselves participant codes), the rows or
participants affected, whether the paper claims anonymisation, and a verified remedy (drop or
coarsen the column, then replace the file through the repository). It never reproduces an
identifying value and never describes how a person could be re-identified. The filed comment is the
disclosure route: I4R decides any notification, and the audit never contacts authors or journals.
No output-sanitizing gate is loosened: participant codes, a code beside a date or clock time,
listings, private paths and data files are refused exactly as before, and stage check 3c adds one
(*Identifying values in the audit's own outputs*, below). The rule protects a correct finding: a
disclosure finding the deposit does not support is refuted and removed like any other claim.

- **Who is told.** The five Phase-1 auditors (`run_phase1.AUDITOR_PII_REPORTING`), the coordinator
  (never EXCLUDE, defer or withhold a finding the evidence supports; one it refutes is a tested
  negative) and the fairness critic (a value-free description is the required form; for a supported
  finding, BLOCKING only for a reproduced value, a new linkage or a re-identification procedure; an
  unsupported one is REFUTED like any other claim). The references say the same:
  `reference/checklist.md` item 7, `reference/brief_template.md`, `reference/report_structure.md`
  (*Disclosure findings*), `reference/verification_guide.md` (count and presence checks),
  `reference/adjudication_guide.md` and `reference/SCHEME.md`, where `urgency: immediate` now
  means reported in the comment, never paused. The post-fan-out verifier's prompt is unchanged
  byte for byte.
- **Evidence for personal data held in code-named files.** A deposit file whose name is a
  participant code still may not be cited. Bind the finding's evidence to the script that reads
  those files, to the codebook or data dictionary, or to a sibling file with an ordinary name, and
  describe the files as a directory and a count ("the raw directory, 40 per-participant files").
- **What refuses.** A disclosure-flagged row leaves the comment only on a structured basis, never a
  free-text one. `scripts/integrate_round.py apply` refuses, before writing anything, a decision that
  EXCLUDEs such a row, clears its `disclosure_flag`, or sets its status rejected or superseded
  without a decision-level `disclosure_exclusion_basis` of `refuted`,
  `duplicate_of:<finding id>`, `not_a_disclosure_defect` or `tested_negative`. Only a change of
  state counts: a draft kept unapproved while its claim is revised is still under review, and so
  (2.6.9) is an approved row whose approval is withdrawn while it stays INCLUDED and flagged, which
  is integrate's own remedy for an OVERSTATED verdict on unchanged text. Clearing a
  flag also needs a `disclosure_flag_reason`, which is recorded in `ADJUDICATION.md`. A MERGE target
  and a `duplicate_of` row must themselves end final (approved, a final status, INCLUDED):

  ```json
  {"F9": {"status": "rejected", "coordinator_approved": false,
          "ledger_disposition": {"status": "EXCLUDED", "target": null,
                                 "reason": "the column holds session dates, not birth dates"},
          "disclosure_exclusion_basis": "refuted"}}
  ```

  The row's ledger reason is written opening with the basis label ("Refuted: the column holds
  ..."). `finalize_run.sh` reads that label back. It refuses a comment whose
  `governance.pii_present` is true without a disclosure-flagged finding, non-empty
  `disclosure.flags` and a `disclosure.statement`, and non-empty flags with no flagged finding. It
  also refuses any disclosure-flagged registry row that is not final, MERGED into a final row, or
  EXCLUDED with a reason opening "Refuted:", "Duplicate of <id>:", "Not a disclosure defect:" or
  "Tested negative:". Withholding language in a reason only warns (`scripts/disclosure_policy.py`).
  One exception keeps an unfinished audit fileable: at `cap_with_unresolved` or `aborted`, a flagged
  row still INCLUDED and awaiting its approval (a deferred approval that no remaining round can
  bind) only warns when the stop's `review_scope` lists it as unresolved (in
  `unresolved_blocking_item_ids` or `unresolved_conclusion_critical_item_ids`, by its finding id,
  `F7`, or by the id of a critic item that assessed it alone, `critic_stats:S3`; item ids are read
  from the round records and translated through `work/id_map_history.jsonl`) and the comment's
  `disclosure.flags` and `disclosure.statement` report the exposure; it then also stands in for the
  flagged comment finding. Its prose stays in the report, outside the numbered findings. At
  `target_complete` or `converged` it must be approved first, so verify and approve a disclosure
  finding before the last round where you can.
  Stage check 3c refuses a staged output that reproduces an identifying value from the deposit.
- **Only for new audits.** The integrate and finalize checks are errors only when `run.json`
  `protocol_version` is 2.6.9 or later (`disclosure_policy.ENFORCED_FROM`), or when a 2.6.9 skill
  reopened the audit. An audit initialised on the live 2.6.8 release, whose SKILL.md and
  coordinator prompts still said to pause for a disclosure decision, gets the same messages as
  warnings, like one initialised on an older skill. Its decisions still apply, and a published audit
  can still be re-finalized through *Correcting a published comment*.
  `config/check_policy_drift.py` fails if the old pause or withholding wording comes back.

**Upgrading an audit that is in flight:** the one-time costs of this and every other 2.6.9
change are listed by phase in *Upgrading in-flight audits to 2.6.9*. Published audits are
unaffected.

## Live canary before the first audit

The test suite uses fake providers and cannot see credential, model-routing or sandbox problems.
After `setup_workspace.sh` and `init_run.sh`, run the bounded capability probe on its own:

```bash
python3 scripts/run_phase1.py "$AUDIT_DIR" --probe-only
```

It costs one short Sonnet call, starts no auditor, and fails fast on a CLI that is not signed in,
a model the account cannot reach, or a probe that exceeds its token bound: the bound its snapshot
froze (`probe_max_normalized_tokens`, which `init_run.sh` printed as `probe bound`), 350,000 when
init froze the shipped 2.7.1 policy and 150,000 for an audit initialised earlier or on a reused 2.7.0
leftover snapshot (*Upgrading in-flight audits to 2.7.1*). The passing probe is cached and reused by
the full run.

Before any of that, `new_audit.sh` has already checked the critic route (2.7.5): `python3
scripts/check_critic_access.py` makes one minimal call on the shipped critic model and effort and refuses a
host whose Codex account cannot use it; `--force` repeats it after an account change.

The critic route has its own canary: `run_critique.sh 1` starts with the critique model probe, one
short call on the audit's critic model and effort, and stops the round there, before any critic call,
when the Codex CLI or the account cannot use that model (for a new audit `gpt-6.1-sol`, which needs
Codex CLI 0.159.0 or newer). The failure line carries the provider's words; see *Upgrading in-flight
audits to 2.7.0* for the remedy. A passing probe is cached in the audit per model, effort and CLI
version, not per account, so it checks the account only on the audit's first round (or a CLI
version it has not seen): a later round run from an account without the model is refused at a
critic instead, and is re-run from an account that has it.

## Validate before the first audit and after every policy edit

From the installed skill directory:

```bash
python3 config/check_policy_drift.py
python3 -m unittest schemas.tests.test_v2_interfaces
python3 -m unittest discover -s tests -p 'test_*.py'     # every Python module, none enumerated by hand
bash tests/test_run_critique.sh
python3 scripts/record_invocation.py --self-test
python3 scripts/test_document_build_cache.py
bash scripts/init_run.sh --self-test
bash scripts/log_event.sh --self-test
bash scripts/finalize_run.sh --self-test
bash scripts/run_verification.sh --self-test
bash scripts/make_settled.sh --self-test
bash scripts/make_claims_script.sh --selftest
bash scripts/reserve_paper.sh --selftest
python3 -m unittest tests.test_post_delta_gates
```

Before distributing a new installation, rebuild and verify the deterministic privacy-scanned ZIP:

```bash
python3 scripts/build_package.py --output ../i4r-audit.zip
python3 scripts/build_package.py --output ../i4r-audit.zip --check
```

The policy drift check verifies model routing, critic roles, target/cap, total retry budget, settled
timing and JSONL row contract, projection and release contracts, current report format, checklist
numbering, maintained-document links, and the word limit on `SKILL.md` (`SKILL_WORD_CAP` in the checker, 4,750 since 2.7.5). It
joins the post-fan-out verifier policy to `scripts/verifier_contract.py` (role class, role pattern,
round, verdicts, and the route an audit initialised before 2.6.4 falls back to) and the verifier
verdict table in `reference/adjudication_guide.md` to the contract's `VERDICT_OUTCOME`. It joins the
shipped Claude route to `scripts/claude_routes.py` (the route new audits freeze is its newest entry,
and the Sonnet 5 route older audits keep is still there), pins the critic route new audits freeze
(`gpt-6.1-sol`, effort high for the critics and the critique probe, max for the adjudicator), pins
the Phase-1 probe bound new audits freeze (350,000, in the runtime and in the capability-probe leaf)
and joins it to `scripts/probe_bounds.py` (it is the newest entry, and the 150,000 older audits keep
is still there), and
refuses a `--auditor-model` value in SKILL.md, INSTALL.md or CHEATSHEET.html that is not the shipped
model: each document is read with its HTML tags stripped, entities such as `&nbsp;` decoded and
backslash-newline continuations joined, and a quoted value counts. It also rejects fifteen
in-memory planted drifts (2.6.9 added the round cap and its amendment maximum; 2.7.0 reverts of the
coordinator, the Phase-1 route and its capability-probe leaf to Sonnet 5, and of the critic route to
`gpt-5.6-terra`; 2.7.1 the probe bound reverted to 150000; 2.7.2 the Phase-1 timeout reverted to
2700), and each must be rejected by the pin(s)
it names, not by any error. The other tests prove the metadata validator,
claims handoff, and shared-tree reservation controls can reject planted failures.

The coordinator needs two inputs the auditors do not produce. Build both with the shipped tools
after Phase 1 reaches `quorum_complete`:

```bash
python3 scripts/build_safe_evidence.py "$AUDIT_DIR"     # work/safe_evidence/ receipts and closure
python3 scripts/build_draft_registry.py "$AUDIT_DIR"    # work/registry_draft/ one schema-valid row per role finding
```

Before the first `generate_finding_artifacts.py --apply`, create the `comment.json` skeleton it
injects findings into: `python3 scripts/init_comment.py "$AUDIT_DIR" --facts work/comment_facts.json`.
It validates against `schemas/comment.schema.json`, rejects unknown fact keys, and refuses to
overwrite without `--force`.

The draft rows are mechanical and unmerged; edit dispositions or let the coordinator merge them.
Every `.jsonl` row under `work/registry_draft` must pass `schemas/v2/finding-row.schema.json` and
the semantic rules in `schemas/tools/validate_v2.py`; a free-form notes file belongs in `.md`.

Two operator commands appear in the record when a run needs them and are documented here so the
record stays readable: `python3 scripts/record_invocation.py --correct "$AUDIT_DIR" <invocation-id>
correction.json`, with `{"set": {"status": "legacy_record"}, "reason": "<why>"}` in the file, appends a
transparent `invocation_correction` (used to mark the rows of a reset or superseded run
`legacy_record`), and `bash scripts/log_event.sh publication_redaction
fields_requiring_removal:='["/events/*/payload/record/error"]'` declares internal-telemetry fields
that are removed before any extract is released. Both are append-only; neither rewrites history.
The correction file is one JSON object with a non-empty `set` and a non-empty `reason` (and an
optional `at`), for example `{"set": {"status": "legacy_record"}, "reason": "superseded by the
reset of 2026-09-24"}`. Only a terminal row can be corrected, and its identity, times and attempt
accounting (`IMMUTABLE_INVOCATION_FIELDS` in `lifecycle_journal.py`) cannot be changed. `--correct`
also works on a post-fan-out verifier call, although every other lifecycle flag of
`record_invocation.py` refuses one (see *Confirming findings raised after fan-out*).

After each critique-round integration, run `python3 scripts/approve_settled.py "$AUDIT_DIR"` before
`scripts/make_settled.sh`: it binds the settled-fact promotion approval to the current report hash on
every promotable row, which `make_settled.sh` requires and never infers.

After a later-round integration and before `scripts/run_post_delta_gates.py --round N`, regenerate
`critique/DIFF_MANIFEST_round_N.json` with `make_diff_packet.py --round N` against the round's
preserved pre-integration files and the integrated canonical report and registry, after
`make_settled.sh`: the gate recomputes the packet from the live after-paths and refuses one that
describes a superseded state.

Before `scripts/finalize_run.sh`, the journal must show every phase closed and the delivery made.
Phase 1 journals `fan_out` itself; log the others with timestamps taken from the invocation records
(`invocations.jsonl`, `run.json` round rows, the post-delta receipt, the staged release):

```bash
bash scripts/log_event.sh phase_start phase=intake       at=<setup_workspace.sh start>
bash scripts/log_event.sh phase_end   phase=intake       at=<fan_out started_at>
bash scripts/log_event.sh phase_start phase=adjudication at=<first coordinator started_at>
bash scripts/log_event.sh phase_end   phase=adjudication at=<coordinator completed ended_at>
bash scripts/log_event.sh phase_start phase=critique     at=<first critic started_at>
bash scripts/log_event.sh phase_end   phase=critique     at=<final-round end: the later of the last integrate_round apply (INTEGRATION_SUMMARY.json integrated_at) and the final round's last critic or targeted-adjudicator call>
bash scripts/log_event.sh phase_start phase=release      at=<first release action after the last round, e.g. build_docx.sh start>
bash scripts/log_event.sh delivery artifact_path=report/<comment>.pdf artifact_sha256=<sha> \
  release_dir=../release recipient='I4R editorial queue' at=<release staged>
bash scripts/log_event.sh phase_end   phase=release      at=<at or after the delivery; omit at= to use now>
```

Order of the pre-finalization artefacts matters because each binds the previous: `SETTLED.json`
binds the registry, the report and the invocation ledger's prefix at generation, so new invocations
and phase or delivery events appended afterwards leave it valid, but a registry or report edit, or an
`invocation_correction` (which rewrites an earlier ledger row), stales it; the diff packet binds
`SETTLED.json`; the post-delta receipt binds the packet and the gate code. After the last such edit
and the last gate-code edit, run in this order: approve -> project -> approve -> project (if the report or registry changed) ->
`make_settled.sh` -> the settled event -> `make_diff_packet.py --round N` ->
`run_post_delta_gates.py --round N` -> `finalize_run.sh`.
A `run_verifier.py` call journals its own invocation rows, so it never belongs inside this chain: run
it before the `integrate_round.py apply` that binds it (see *Confirming findings raised after
fan-out*).

Two more orderings that are easy to get wrong and cost a full cycle each when you do:

- **Re-run the projection after `approve_settled.py`, not before.** `approve_settled.py` binds the
  current report hash into each row's `settled` object in `work/findings.jsonl` (`make_settled.sh`
  only reads the registry), so a projection taken before it is stale the moment it runs, and
  `stage_release.sh` refuses with "canonical finding projections are missing or divergent".
  The projection can itself rewrite the report (Appendix A, claim tags), which stales the approval,
  so alternate once: `approve_settled.py` -> `generate_finding_artifacts.py --apply` ->
  `approve_settled.py` -> `generate_finding_artifacts.py --apply` (the second leaves the report
  unchanged) -> `make_settled.sh` -> `build_docx.sh` -> `stage_release.sh`. SKILL.md section 4 gives
  the same order with the commands.
- **Re-execute the verification notebook after the last report edit.** The notebook run is bound to
  the report it was executed against, so any edit made to satisfy a critique round invalidates it
  and staging refuses with "verification binding is stale". Re-run
  `make_verification_notebook.sh` and `run_verification.sh` before staging.

Two field meanings that are not obvious from their names:

- The `settled` event's `generated_after_round` records the round the cache was **first** generated
  after and injected from -- normally 1. Re-binding the same cache to an edited report in a later
  round does not change it, and setting it to the later round fails the compliance check. Re-log the
  event after **every** `make_settled.sh` run, with `generated_after_round=1` and the new `entries`
  count: finalize compares the last event's count with `SETTLED.json`, and a reworded claim held back
  in one round settles in a later one, so the count changes.
- A finding left unapproved to wait for a later confirmation must be approved before the stop: finalize
  refuses `target_complete`/`converged` while an INCLUDED finding is not final and approved. For
  `target_complete`, integrating the final round only to bind that approval is accepted -- then approve and
  project twice (the projection adds the finding to the report), run `make_settled.sh`, re-log settled and rebuild round N's packet and post-delta gates, all before
  staging; for `converged` it changes the report, so one more critique round follows.
- The final round may also be integrated only to record contests (`integrate_round.py ... --contested
  "$AUDIT_DIR/critique/round_N/CONTESTED.json"`, no rewording or applied_note for those verdicts); finalize then checks that
  `CONTESTED.json` is the set the integration applied. Integrating rewrites the registry (it binds first
  confirmations and unbinds a reviewer that disputes unchanged wording again), so the same follow-ups apply as
  above: approve and project twice, `make_settled.sh`, re-log settled, and rebuild round N's packet and
  post-delta gates before staging. Leaving the round unintegrated and writing `CONTESTED.json` directly avoids
  only approve/project: the disputed facts still need `make_settled.sh`, the settled re-log and the packet
  and gates rebuild (next bullet) before staging.
- After the final (unintegrated) round, run `make_settled.sh` and re-log the settled event again when that
  round has any OVERSTATED/REFUTED verdict: a fact whose confirming reviewer disputed it there is excluded,
  and finalize refuses a `SETTLED.json` that still lists one (then rebuild the latest packet and gates).
- The round-N diff packet describes the round-**(N-1)** integration delta: build it from round
  N-1's preserved `*_BEFORE_INTEGRATION` files against the current report and registry. Building it
  from round N's own before-files produces a different claim set and fails finalize's
  ordered-union check on `changed_claim_ids`.

When the audit tree lives in a synced folder (Dropbox, iCloud), a directory the scripts delete
during cleanup can reappear as an empty directory a moment later. `finalize_run.sh` and
`publish_completed.sh` then see a leftover `.finalize-tx.*` or `.<paper>.publish.*` directory;
remove the empty leftovers before re-running (they hold nothing). The publish lock itself is a
file since 2.4.10 (`<paper>.publish.lock`, holding the owner's pid), so it is not resurrected; a
leftover one from an earlier version, in either form, is broken automatically once its owner is
dead, and refused while the owner is alive.

Every machine that runs audits must run the same skill version. The tree syncs the canonical copy
to all of them; since 2.4.11 `init_run.sh` refuses to start an audit from a copy whose `VERSION`
differs from the canonical one (see `I4R_CANONICAL_SKILL_DIR` below). Reinstall from the ZIP beside
the canonical copy, or run the scripts from the canonical copy directly.

The per-role attempt ceiling defaults to 3 and is set with `I4R_MAX_ATTEMPTS=<n>` (1 to 20) for
Phase 1, the coordinator and the post-fan-out verifier; the critique driver reads `MAX_TRIES`. Raise it when attempts are
being consumed by environment or harness problems rather than by the role's own work.

If a Phase-1 role exhausts its attempts, `init_run.sh --force` is the supported recovery. It
archives the journal and `phase1/` (as `phase1.previous.<ts>`) and restores every budget, but it also clears the ledger rows that bind the
promoted reports, so all five roles re-run. Changing the role prompt has the same effect, because
reports are bound to the packet hash. `--force` also freezes the shipped policy again, so since 2.7.0
it moves an audit initialised earlier from Sonnet 5 to Sonnet 5.5 (see *Upgrading in-flight audits
to 2.7.0*), and since 2.7.1 to the 350,000 probe bound. Pass the audit's `--audit-id`, `--doi` and
`--paper-title` again: `run.json` is rebuilt from them (the full command is in *Upgrading in-flight
audits to 2.7.1*). A verifier batch that exhausts its attempts never needs it:
see *Confirming findings raised after fan-out*.

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
| Coordinator | Sonnet 5.5 / xhigh (Sonnet 5.5 / max for an audit initialised on 2.7.0 to 2.7.2, Sonnet 5 / max before 2.7.0) | 1 |
| Phase 1 | Five auditors on the coordinator's route | 5 |
| Post-fan-out verifier | The auditors' route and family | 0 by default; 1 call per batch of rows raised after fan-out |
| Critique round 1 | GPT-6.1 Sol/high stats, data, fairness, completeness (an audit initialised before 2.7.0 keeps its frozen route: GPT-5.6 Terra, or GPT-6 Sol on 2.6.0) | 4 |
| Critique round 2 | The same route: completeness plus routed specialist(s) | 2 by default |

Round 1 reports are merged deterministically. Maintained runs set `ADJUDICATE=auto`, so the adjudicator
(Sol/max; an older audit's frozen model at max) is called only for a conflicting verdict, a disputed conclusion-changing claim, a high-impact finding
lacking two independently bound evidence records, or a failed settled-fact spot check. With no
trigger there is no model synthesis/adjudication session. Later rounds route through a
`DIFF_MANIFEST` and always retain completeness. Build a changed round's manifest from explicit
pre/post report and registry files with `scripts/make_diff_packet.py`; its hashes bind the change
scope and its domain signals select one best specialist by default. It adds specialists only for
conclusion-changing or disclosure-critical changes spanning multiple domains.

Metadata 2.1 makes `run.events.jsonl` the append-only, hash-chained source of truth. Each fresh
critique round prepares its exact plan before any provider request. **Since 2.3.0 the driver
authorizes its own unmodified plan and continues — unattended is the default.** The grant is still
journaled and still bound to the exact plan; only the human pause is gone. Set
`I4R_AUTO_AUTHORIZE=0` to restore it, in which case the driver exits 3 and you review the
generated `critique/round_N/ROUND_AUTHORIZATION_TEMPLATE.json`, authorize it with
`python3 scripts/lifecycle_journal.py authorize-round <audit-dir> N <template>`, then rerun the
unchanged critique command. When authorizing itself the driver also passes `--supersede-plan`, so an unfinished round whose inputs
changed since it was prepared (a report edit, a prompt fix) gets a superseding plan journaled and
authorized instead of the refusal "prepared critique round plan differs from the current immutable
input". The round's input identity covers the frozen sources, round, model, effort, roles and the
critic output contract; runtime code hashes are recorded as telemetry but do not change it. `run.json`, `invocations.jsonl`, `run_metrics.json`, and
`RUN_METADATA.md` are deterministic projections carrying the journal watermark.

For example:

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
coordinator approval, two reviewers spanning two model families (the Claude-family one is the
Phase-1 auditor that raised the row or, for a row raised after fan-out, the post-fan-out verifier), distinct canonical invocation ids
and immutable reviewer-output path/hash records, rerunnable source hashes, a
dependency fingerprint, and exact canonical registry/current-report hashes. Two rounds are required;
target completion and later convergence have distinct meanings, and the audit's own round cap
(`critique.round_cap` in its snapshot: 7 from 2.6.9, 5 for an older audit until amended) is the hard
stop.

For the coordinator and each phase-1 attempt, construct one canonical
`schemas/v2/invocation-record.schema.json` object and append it with
`python3 scripts/record_invocation.py "$AUDIT_DIR" <record.json>`. The helper rejects policy/model
drift, duplicate invocation ids, malformed usage, and completed outputs whose hashes do not match.
The critique driver records critic attempts automatically, and `run_verifier.py` records the
verifier's, so do not duplicate those rows.

At the required two-round endpoint, use `target_complete` only when every changed claim id received
subsequent review and no unresolved blocking or conclusion-critical item remains; finalize refuses while
the last round, integrated or not, has an OVERSTATED/REFUTED verdict, a BLOCKING-priority item or a BLOCKING new
issue that no `CONTESTED.json` entry answers (same keys as for `converged` below; an optional suggestion on a
CONFIRMED item does not count for `target_complete`). An `applied_note` does not answer one: after the last round
only projection edits may reach the report, so contest it or run another round. A verdict on a finding already withdrawn (status `rejected`, or EXCLUDED/MERGED) does not count. A round counts as integrated when `integrate_round.py apply`
wrote its `critique/round_N/INTEGRATION_SUMMARY.json`. `target_complete` also needs `REPORT_final.md` to be the report the
last round reviewed, apart from the generated Appendix A, claim tags and finding markers (binding a deferred
approval changes only those), and the published registry fields of each INCLUDED finding (claim, confidence, section,
impact, evidence values, remedy, ...) to be the ones that round reviewed: after it, only binding a deferred
approval may change a finding (status, approval, bound verifications, and `report_section`,
`verification_snippet` where they were still empty at review); the set of INCLUDED findings must also be the one
that round reviewed: integrating even a wording-only fix after the last round means another round,
or `cap_with_unresolved` at the round cap. If review continues,
use `converged` only when a later round returns no factual corrections (finalize reads that round's
`critic_*.json`: an OVERSTATED/REFUTED verdict, a requested correction, a BLOCKING-priority item or a BLOCKING new issue refuses it
unless `critique/round_N/CONTESTED.json` records the coordinator's position on it, keyed by the critic
file's name and the item id, e.g. `{"critic_stats:stats-F1": {"position": "...", "evidence": "..."}}`,
both non-empty; the targeted adjudication's own items are keyed `critic_synth:<item_id>`, and its
CONFIRMED, NON_BLOCKING ruling on a finding (with no correction requested) resolves the critics' conflicting item there, also for integrate_round's
unacted-correction check; write CONTESTED.json before `stage_release.sh` so the release carries it --
finalize refuses when the last staged release lacks the same file, recorded by stage_release.sh in
work/RELEASE_STAGED.json); use `cap_with_unresolved` at
the audit's round cap, and `aborted` only for a genuine blocker or failed run. The `review_scope` log event records
changed, re-reviewed, unresolved, and unreviewed ids atomically before finalization.
Record `factual_corrections=0` on the final beyond-target `round_result` before using `converged`;
an omitted value cannot establish convergence.

Verification must run through `bash scripts/run_verification.sh "$AUDIT_DIR"`; it writes the
content-bound `verification_manifest.json` required by release staging. There is no
`ALLOW_NO_VERIFICATION` waiver. Build the final comment with
`bash scripts/build_docx.sh "$AUDIT_DIR"`. Canonical builds read the ten-body-page limit from the
hash-bound audit policy snapshot and reject `MAX_PAGES`; use `--preview-max-pages N` only for a
non-release preview under `report/previews/`. Publication contains the single
[`document-manifest-v3`](schemas/v3/document-build-manifest.schema.json)-bound PDF, claims script,
`run.json`, `comment.json`,
`finalization_receipt.json`, `document_build_manifest.json`, and `release-manifest.json`, plus the
bound DOCX when one was produced (seven files without DOCX, eight with it).

## Appendix A rendering and document manifests (2.6.9)

`build_docx.sh` rewrites the generated Appendix A findings table in its converter input only. It
sets fixed relative column widths (# 5.5%, Finding 42.5%, Status 20.5%, Changes 15.5%, Section
16%) and backslash-escapes every cell. Pandoc therefore prints every character it would read as
markup literally, and LibreOffice no longer breaks `CONFIRMED`, `SPECULATIVE`, two-digit numbers
or a Section cell such as `Section 10.2` across lines. Typography still applies as in body text:
`--`, `---` and `...` print as an en dash, an em dash and an ellipsis, quotes print straight, and
`§4` prints as `Section 4`. The helpers live in `scripts/document_release.py`. `REPORT_final.md`,
`APPENDIX_A.md`, the registry and `comment.json` keep their bytes, so projections and verifier
records stay valid. Of the files the post-delta receipt hashes, only
`schemas/v2/post-delta-gates.schema.json` changed (the round-cap change): earlier rounds' receipts
replay as before, but the latest round's receipt is stale once and is regenerated (no provider
call; the re-file chain below).

A fail-closed build stage, `findings_table_integrity`, checks the exact DOCX and PDF, rebuilt or
restored from the cache, before the manifest is written. Each DOCX cell of the table must equal its
literal source text, and every table word must be whole on the PDF pages from the Appendix A
heading on. A word wider than the Finding column itself, such as a long file name, has no break
opportunity; it is exempt from the PDF check and counted in the stage's output, and its DOCX text is
still checked. Invisible format characters in a claim (a zero-width space, a word joiner, a soft
hyphen) and scripts written without spaces (Chinese, Japanese, Korean, Thai) do not refuse a build:
the check deletes the former, treats each character of the latter as a word, and accepts a word
printed whole or broken at its zero-width space or soft hyphen. If the stage lists words that
print whole in the PDF, report it as a build defect; never edit the claim to get past it. The stage
is not a manifest validation, so later boundaries do not re-run it. To check a built pair by hand:

```bash
python3 scripts/document_release.py findings-table-integrity --report "$AUDIT_DIR/report/REPORT_final.md" \
  --docx "$AUDIT_DIR/report/<output-name>.docx" --pdf "$AUDIT_DIR/report/<output-name>.pdf"
```

The widths are sized for Liberation Serif (Times metrics) at 12pt so that each narrow column holds
its widest text on A4, the narrowest page the build meets: `SPECULATIVE` with 1pt to spare and
`Section 10.2` with 1.4pt. Letter gives the narrow columns 1 to 3.5pt more. `Section 10.12` and
wider still wrap onto two lines, which leaves every word whole. A machine whose default document
font is wider, for example one with Aptos installed, can fail the stage; build on the canonical
machine.

**Operator note.** Every `document_build_manifest.json` binds the hashes of `build_docx.sh` and
`document_release.py`, and 2.6.9 changes both again. A manifest written by 2.6.8 or earlier is
therefore stale: finalize (step 4c), `stage_release.sh`, `publish_completed.sh` and the
finalization-receipt check refuse it until the document is rebuilt, with "document manifest builder
code identity differs from the release verifier (the skill's document scripts changed since this
build). Rebuild the document with build_docx.sh, then re-run finalize_run.sh before staging or
publishing: rebuild with scripts/build_docx.sh <audit-dir> <output-name> (see INSTALL.md, 'Appendix A
rendering and document manifests')", the bound PDF's name filled in. Run `build_docx.sh` before
finalize, stage or publish; in-flight audits already rebuild after their last report edit (the other
one-time costs are in *Upgrading in-flight audits to 2.6.9*). Old document-cache entries are simply
missed, because the cache key binds both scripts.

The document cache now hits on an unchanged rebuild. pandoc stamps the current time into the
reference document the build prints, and the cache key binds those bytes, so before 2.6.9 every
canonical build had a new key and added one more archive to `.cache/document-builds`. The build
now prints the reference with `SOURCE_DATE_EPOCH=0`, whose members are the same bytes, so the key
is stable. This changes the key's reference hash once, together with the builder hash; the archives
written before it are never hit again and may be removed.

**Re-filing a published comment with the corrected Appendix.** The report text does not change, so
no critique round runs and no reopen is needed, but a rebuild and a restage alone are not enough.
The full chain, proven end to end on scratch copies of the Schütz audit (published on 2.6.7) and
the food-insecurity SJMSS audit (published on the live 2.6.8), with no provider call:

1. Move the filed set under `old_runs/` and claim the paper again (*Correcting a published
   comment*, steps 1 and 2).
2. `chmod -R a-w "$AUDIT_DIR/pkg"` when the synced folder stripped its read-only mode (7 of 10
   published audits swept): `validate_round_projections.py` refuses "archive package directory is
   not read-only: pkg/.".
3. `bash scripts/build_docx.sh "$AUDIT_DIR" <output-name>` (the stale-manifest refusal prints the
   name).
4. Only for an audit published on 2.6.7 or earlier. 2.6.8 changed the verification notebook's
   projection, so `generate_finding_artifacts.py --check` refuses "verification notebook is not the
   exact deterministic projection" until you run `bash scripts/make_verification_notebook.sh
   "$AUDIT_DIR" && bash scripts/run_verification.sh "$AUDIT_DIR"`, then `python3
   scripts/generate_finding_artifacts.py "$AUDIT_DIR" --apply` and `bash scripts/make_claims_script.sh
   "$AUDIT_DIR"`. An audit published on 2.6.8 passes `--check` as it is.
5. The last round's packet and receipt (the receipt schema changed, so its receipt is stale):
   `python3 scripts/make_diff_packet.py "$AUDIT_DIR" --round N --before-report
   critique/round_<N-1>/REPORT_BEFORE_INTEGRATION.md --after-report report/REPORT_final.md
   --before-findings critique/round_<N-1>/FINDINGS_BEFORE_INTEGRATION.jsonl --after-findings
   work/findings.jsonl --output critique/DIFF_MANIFEST_round_N.json` (paths under `"$AUDIT_DIR"`;
   `stage_release.sh` prints the exact command), then `python3 scripts/run_post_delta_gates.py
   "$AUDIT_DIR" --round N --report "$AUDIT_DIR/report/REPORT_final.md"`. The rebuilt packet was
   byte-identical to the published one on both audits. A step-1 registry copy that an old version
   renumbered without keeping a history (Schütz, round 4) no longer matches integrate_round's
   backup. From 2.6.9 it is accepted when it holds exactly the bytes the round's own frozen packet
   was built from: `make_diff_packet.py` prints a note, and never a restore that would copy the same
   bytes over themselves. `python3 scripts/validate_round_projections.py "$AUDIT_DIR"
   --require-post-delta-gates` then passes.
6. Move the audit's existing `../release` aside and stage into a fresh directory
   (`bash scripts/stage_release.sh "$AUDIT_DIR" <fresh dir>`).
7. Re-log the delivery (`bash scripts/log_event.sh delivery artifact_path=report/<output-name>.pdf
   recipient='<recipient>'`) and then `phase_end phase=release`.
8. `I4R_REFINALIZE_PUBLISHED=1 bash scripts/finalize_run.sh "$AUDIT_DIR" --stopping-reason <its
   stop>`. An audit initialised before 2.6.9 gets the disclosure checks as warnings (the
   food-insecurity audit warns that its comment-level `disclosure.flags` and `statement` are empty).
9. `bash scripts/publish_completed.sh "$AUDIT_DIR"`.

## Correcting a published comment

`publish_completed.sh` never overwrites a filed set, and `finalize_run.sh` refuses an audit whose journal
records publish's filing: a new finalize would file a registry version that no longer matches the published
`metadata/<Journal>/<Paper>/` record. Correcting a published comment is a coordinator decision:

1. Move both published folders under `old_runs/`, which `status.py` does not read (finalize with the
   override refuses while either is still in place):
   `A=old_runs/<Journal>/<Paper>.published-$(date -u +%Y%m%dT%H%M%SZ)`, `mkdir -p "$A"` (a new
   folder each time, so a second correction never lands inside the first), then
   `mv comments/<Journal>/<Paper> "$A/comments"` and `mv metadata/<Journal>/<Paper> "$A/metadata"`.
2. Claim the paper again (`bash scripts/reserve_paper.sh claim data/<Journal>/<Paper>`): publish released it,
   and a published paper cannot be claimed until its folders are moved.
3. Redo the work the correction needs. A report change means another critique round, so reopen the
   audit first (2.6.9: a finalized audit runs no further round until a journaled reopen):
   `python3 scripts/reopen_audit.py "$AUDIT_DIR" --reason "<why>"`. A round above the audit's cap needs
   `amend_round_cap.py --reopen` instead (*Raising the round cap of an existing audit*). If the last round
   was never integrated, first make its step-1 copies from the files that round reviewed: copy the
   snapshot its `CURRENT_INVOCATION` pins (`source/REPORT_TO_REVIEW.md` and `source/context/work/findings.jsonl`) to `critique/round_N/REPORT_BEFORE_INTEGRATION.md` and
   `critique/round_N/FINDINGS_BEFORE_INTEGRATION.jsonl` (`make_diff_packet.py` prints the exact commands, which keep the copies writable).
   If the reopened round has a `critique/round_N/CONTESTED.json`, pass it to `integrate_round.py
   --contested` for the contests you keep and remove the entries for verdicts you now act on.
   After re-integrating the reopened round (with or without contests), re-log its `round_result` with the counts
   its new `INTEGRATION_SUMMARY.json` records; then re-log
   `phase_end phase=critique at=<final-round end>` and `phase_start phase=release` (after it).
4. Rebuild the release set for the corrected report as in SKILL's release block (projections, DOCX/PDF, notebook,
   verification, claims script, a new `REPORT_v<N+1>.md` snapshot), then stage into a fresh directory, re-log the delivery (`bash scripts/log_event.sh delivery
   artifact_path=report/<output-name>.pdf recipient='<recipient>'`) and `phase_end phase=release`.
5. Run finalize with `I4R_REFINALIZE_PUBLISHED=1`, then publish again (publish releases the claim).

## Raising the round cap of an existing audit (2.6.9)

2.6.9 raises the critique round cap from 5 to 7 (`critique.round_cap` in
`config/pipeline-policy-v2.json`). Each audit freezes the cap in its `pipeline-policy.snapshot.json`
at `init_run.sh`, so only audits initialised on 2.6.9 or later get 7. An audit initialised earlier
keeps 5, and nothing re-caps it silently. The driver, the journal, finalize and publication all read
the audit's own cap. Raising it is an explicit operator act with a reason, journaled and checked at
every later release boundary.

**What enforces it.**

- `run_critique.sh N` refuses a round above the audit's cap before it creates the round folder. When
  the skill ships a higher cap, the message names the command below. `lifecycle_journal.py` refuses
  to prepare such a round under the journal lock, and at the cap its hint says to finalize
  `cap_with_unresolved` or amend the cap.
- The cap they enforce is the journaled one: the cap recorded at init, raised by each
  `policy_amendment`. Before any round is prepared or run, `round_cap_amendment.py check-round`
  (which the driver calls) and `prepare_round` prove the snapshot's envelope hash and `run.json`'s
  binding to it. They then refuse, for any round number, a snapshot whose cap differs from the
  journaled one. A snapshot cap edited by hand and re-hashed, even with `run.json` rebound, stops
  there: nothing is journaled and no model probe is paid for.
- A hand-logged round event (`round_prepared`, `round_start`, `round_end` and the other events of a
  round being run) obeys the same rule through `log_event.sh` and the `lifecycle_journal.py append-*`
  commands. A round above the cap, or any round on a finalized audit that was not reopened, is
  refused. Re-logging `round_result` or `round_integrated` stays allowed.
- A finalize closes an audit. The driver and the journal refuse any new round until an
  `audit_reopened` event follows the last finalize.
- `policy_amendment` and `audit_reopened` are written only by their tools. `log_event.sh` and the
  `lifecycle_journal.py append-*` commands refuse them. A hand-logged `metadata_correction` may not
  set `round_cap`, `round_target`, `policy_amendments`, `reopenings` or `toolkit`. (2.7.4 adds a
  third tool-only event, `environment_captured`, written by `capture_environment.sh`; see
  *Upgrading in-flight audits to 2.7.4*.)
- `scripts/round_cap_amendment.py` runs at finalize (its failures are compliance violations, and
  `compliance.policy_amendments_valid` is recorded) and in `validate_round_projections.py`, so
  finalize step 0c and `publish_completed.sh` refuse an inconsistent chain. It checks seven things:
  1. the snapshot's `critique.round_cap_amendments` equals the journal's `policy_amendment` events;
  2. each archived snapshot hashes as recorded and differs from its successor only at the cap, the
     cap's stop text and the history list;
  3. each round's plan binds the snapshot in force when it was prepared (rounds 1-5 the replaced
     snapshot, rounds 6-7 the amended one). The snapshot in force is the one bound at init, rebound by
     each journaled rewrite before the plan: a critic re-pin as well as an amendment. Each amendment
     must start from the snapshot bound when it was made;
  4. no round above a cap was prepared before the amendment that raised it;
  5. no round ran after a finalize without a reopen between them (an error from protocol 2.6.9, or
     once the audit has an amendment or a reopening; a warning on an older record);
  6. the cap recorded at init, raised by each amendment in turn, equals the snapshot's cap and
     `run.round_cap`;
  7. no round event names a round above the cap in force when it was journaled (an error on every
     record: no release ever allowed one).
- `validate_v2.py` and `run.schema.json` accept a cap of 5 or 7, and `run.policy_amendments` must
  chain from the old cap to the run's cap. finalize accepts `cap_with_unresolved` only when
  `rounds_run` equals the audit's effective cap: 5 for an unamended older audit, 7 for a new or an
  amended one (once amended, stopping after round 5 or 6 is a violation).

**The tools.** Run each with `--dry-run` first: every check runs and nothing is written.

```bash
# S is the installed skill folder (the tree root has no scripts/), e.g. S="$PWD/skill/i4r-audit-unified/i4r-audit"
# raise the cap; add --reopen when the audit is finalized (journaled first, in the same lock)
python3 "$S/scripts/amend_round_cap.py" "$AUDIT_DIR" --to 7 --reason "<why>" [--reopen] [--dry-run]
# reopen a finalized audit for a correction inside its cap (Correcting a published comment, step 3)
python3 "$S/scripts/reopen_audit.py" "$AUDIT_DIR" --reason "<why>" [--dry-run]
# the release-boundary check on its own
python3 "$S/scripts/round_cap_amendment.py" "$AUDIT_DIR"
```

`amend_round_cap.py` sets the snapshot's `critique.round_cap` to 7 and replaces the old stop text,
which named round 5, with the shipped one. It appends a history entry: from, to, time, reason, the
replaced snapshot's hashes and archive name, skill version, and shipped policy hash. The replaced bytes
are kept read-only as `pipeline-policy.snapshot.superseded-<time>.json`. The journal gets
`audit_reopened` (with `--reopen`) and then `policy_amendment`, and the projection sets `run.round_cap`
and the run's snapshot binding. It refuses:

- a run that is not live metadata 2.1 (a v1 record, such as the four in `old_runs/`, cannot be
  resumed; audit that paper afresh with `new_audit.sh`);
- an invalid snapshot, a run not bound to it, or a snapshot cap that differs from the journaled one;
- a `--to` that is not above the current cap or is above the shipped one;
- an empty reason;
- a live driver;
- a round already prepared above the old cap;
- a round prepared or started and not finished (no `round_end` or `round_integrated` after it). The
  amendment rewrites the policy snapshot, which the round's own snapshot copies. Relaunching the round
  would therefore supersede its plan and pay again for every critic review cached for it, and for its
  targeted adjudication. The refusal prints that count per round. Finish and integrate the round
  first, or pass `--accept-rerun-cost` to amend anyway. With nothing completed cached for the round
  (a round that failed before any review) nothing is paid again, so it only notes the round;
- a finalized audit without `--reopen`;
- a published audit whose filed set is still in place, or whose paper is not claimed again since the
  filing. The paper is found through the audit's `paper_source.json` (its true name), then through
  its `runs/` path. A re-claim that cannot be confirmed is refused: no `data/<Journal>/<Paper>`
  folder, or no readable reservation. Pass `--unconfirmed-claim` only for an audit outside a tree;
- an interrupted finalize;
- any amendment whose resulting state would fail `round_cap_amendment.py`. The tool projects its
  events onto the journal and checks them with the new snapshot and its archive before writing
  anything, in the dry run as in the real run. A stray round event after a pre-2.6.9 close is only a
  warning today. It becomes a permanent violation once the audit is reopened, so the tool refuses.
  `reopen_audit.py` runs the same check.

**Re-pin before amending.** `repin_critic_route.py` refuses an audit that has a `policy_amendment`: an
amended snapshot may differ from the one it replaced only at the cap. An audit on an older critic route that
is moved up to the shipped route (2.7.5: the only re-pin left) and also needs a higher cap is re-pinned first,
then amended. The verifier
follows that chain: rounds prepared before the re-pin bind the original snapshot, and the amendment
starts from the re-pinned one. Two rewrites in the same second archive under two names
(`...superseded-<time>-2.json`). A re-pin replaces the snapshot before it journals the rebinding, and
puts the old bytes back if the journal append fails.

A reopen records the close it reopens: finalize time, stopping reason, rounds run, receipt hash and
registry version, and publication. For a published audit the record adds `archived_to`, the
`old_runs/` folder the filed set was moved to. The tool finds it as the newest
`old_runs/<Journal>/<Paper>.published-*` whose `metadata/finalization_receipt.json` is this audit's
receipt, or takes it from `--archived-set <folder>`. A re-finalize rewrites `finalization_receipt.json`
in place, so the reopen first keeps the superseded receipt read-only (0400) as
`finalization_receipt.superseded-<finalize seq>.json`. It names that copy and its hash in the event
(`previous_receipt_archive`). It clears the stop, its evidence (the `review_scope` fields),
`ended_at` and the compliance verdict until the next finalize. The time between the previous finalize
and the reopen counts as paused in `run_metrics.json`, not idle. `status.py` shows the audit as reopened.
A reopened audit re-files under the rule in force when it was reopened: the disclosure checks
(*Deposits containing personal data*) are errors for it even if it was initialised before 2.6.9.

### An unfinalized audit that stopped after round 5

No reopen and no published set are involved. `S` is the installed skill folder, as in the next
section:

1. Integrate round 5 first: the amendment refuses a round prepared or started and not finished. Then
   `python3 "$S/scripts/amend_round_cap.py" "$AUDIT_DIR" --to 7 --reason "<why>"`, dry run first.
2. `python3 "$S/scripts/generate_finding_artifacts.py" "$AUDIT_DIR" --apply`. 2.6.8 changed the
   verification notebook's projection, and round 6's checklist gate refuses the old one until this
   is run.
3. Write `WRAPUP.txt` for round 6 of cap 7 (amended from 5): the changed sections, their dependent
   claim ids and the two rounds left. The round snapshot freezes this text into every critic prompt.
4. Build, prepare, gate and run round 6 exactly as round 2 in SKILL.md, with `--round 6` and round 5's
   step-1 copies (`critique/round_5/REPORT_BEFORE_INTEGRATION.md` and
   `FINDINGS_BEFORE_INTEGRATION.jsonl`). Where round 5 was integrated without them (IJSEP social
   norms and PSE intergroup criticism), make them first, as in *Correcting a published comment*,
   step 3. The first `make_diff_packet.py --round 6` stops and prints two `cp` commands. The report
   comes from the snapshot pinned by round 5's `CURRENT_INVOCATION`. The findings come from
   `integrate_round.py`'s pre-integration backup where one exists; neither audit named above has a
   round-5 backup, so for them they come from that snapshot's `context/work/findings.jsonl`, and the
   message's "never integrated" is inaccurate (the journal records round 5 as integrated). Run the
   commands and build the packet again.
   Integrate round 6 as usual. Run round 7 the same way if round 6 made a correction or left a
   BLOCKING or conclusion-critical item.
5. Log `review_scope` with the ordered union of every packet's `changed_claim_ids`
   (`validate_round_projections.py` names the list it expects), then release and finalize as usual.
   Use `converged` after a clean round, or `cap_with_unresolved` only after round 7.

### Ziv and Neudorfer: published audits that stopped at their cap

Both stopped `cap_with_unresolved` after five rounds, with one section changed after its last review.
Both are published. Every step below writes to the live tree and is the owner's decision. Rounds 6 and
7 are paid provider calls. Rehearse the steps on a copy of the audit first. Scratch copies of both
audits, driven with the fake Codex CLI, prepared, gated and completed round 6 this way.

| | Ziv | Neudorfer |
|---|---|---|
| audit id | `20260926-audit-d5de4cbe` | `20260923-audit-3309cfcf` |
| journal | Psychology of Sport and Exercise | Scandinavian Journal of Medicine & Science in Sports |
| initialised on | 2.6.4 | 2.6.3 (on a WSL machine) |
| unreviewed at the close | Section 2 | the Abstract |
| round-6 critics (probe) | stats, fairness, completeness | stats, data, fairness, completeness |

Two names are in play. `P` is the `runs/` folder name, used only for `runs/` and `old_runs/`.
`PD` is the paper's true name from the audit's `paper_source.json`, used for `data/`, `comments/`
and `metadata/`. `new_audit.sh` drops `?*[]{}\` from the `runs/` folder name, so for Ziv the two
differ: its `data/`, `comments/` and `metadata/` folders keep a `?` that its `runs/` folder does not.
Every script is called from the installed skill, `S`, because the tree root has no `scripts/`. From
the tree root (the folder holding `data/`, `runs/`, `comments/`, `metadata/`, `old_runs/` and
`skill/`):

```bash
cd "<tree root>"
export AUDIT_DIR="$PWD/runs/<Journal>/<runs folder>/audit"   # run_critique.sh reads it from the environment
S="$PWD/skill/i4r-audit-unified/i4r-audit"                # the installed 2.6.9 skill
J=$(basename "$(dirname "$(dirname "$AUDIT_DIR")")")       # the journal folder
P=$(basename "$(dirname "$AUDIT_DIR")")                    # runs/ and old_runs/ only
PD=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["paper"])' "$AUDIT_DIR/paper_source.json")
```

1. Install 2.6.9 and check it:

   ```bash
   python3 "$S/config/check_policy_drift.py"
   ```

2. Move the published set and the old release aside, then claim the paper again (*Correcting a
   published comment*, steps 1-2):

   ```bash
   A="old_runs/$J/$P.published-$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$A"
   mv "comments/$J/$PD" "$A/comments"; mv "metadata/$J/$PD" "$A/metadata"
   mv "runs/$J/$P/release" "$A/release"
   find "runs/$J/$P" -maxdepth 1 -name 'release.rejected-*' -exec mv {} "$A/" \;   # Ziv has one
   bash "$S/scripts/reserve_paper.sh" claim "data/$J/$PD"
   ```

3. Neudorfer only: `chmod -R a-w "$AUDIT_DIR/pkg"`. The synced folder gave its package back its write
   bits, and the archive-gate replay of the round 2-5 receipts refuses a writable `pkg/`.
4. Raise the cap and reopen. Dry run first:

   ```bash
   python3 "$S/scripts/amend_round_cap.py" "$AUDIT_DIR" --to 7 --reopen --dry-run \
     --reason "stopped cap_with_unresolved after round 5 with <Section 2 | the Abstract> unreviewed; 2.6.9 raises the cap to 7"
   ```

   It must end `and reopen the finalized audit; every check passed, nothing written`, with no note. It
   checks the state the amendment would leave, finds the paper's re-claim through `paper_source.json`,
   and finds the moved set under `old_runs/`. Then run the same command without `--dry-run`. The
   reopen keeps the superseded receipt read-only as `finalization_receipt.superseded-<seq>.json`, and
   records it and the `old_runs/` folder in the journal.
5. `python3 "$S/scripts/generate_finding_artifacts.py" "$AUDIT_DIR" --apply`. Only `verification.Rmd`
   and `work/finding_projections.manifest.json` change.
6. Rewrite `WRAPUP.txt`. Ziv's calls five rounds the hard cap, and Neudorfer's calls round 5 the
   last. Replace it with round 6 of cap 7 (amended from 5), the unreviewed section, its dependent
   claims, and the two rounds left.
7. Round 6, from round 5's step-1 copies (both audits have them):

   ```bash
   python3 "$S/scripts/make_diff_packet.py" "$AUDIT_DIR" --round 6 \
     --before-report "$AUDIT_DIR/critique/round_5/REPORT_BEFORE_INTEGRATION.md" \
     --after-report "$AUDIT_DIR/report/REPORT_final.md" \
     --before-findings "$AUDIT_DIR/critique/round_5/FINDINGS_BEFORE_INTEGRATION.jsonl" \
     --after-findings "$AUDIT_DIR/work/findings.jsonl" \
     --output "$AUDIT_DIR/critique/DIFF_MANIFEST_round_6.json"
   DIFF_MANIFEST="$AUDIT_DIR/critique/DIFF_MANIFEST_round_6.json" I4R_AUTO_AUTHORIZE=0 bash "$S/scripts/run_critique.sh" 6
   python3 "$S/scripts/run_post_delta_gates.py" "$AUDIT_DIR" --round 6 --report "$AUDIT_DIR/report/REPORT_final.md"
   DIFF_MANIFEST="$AUDIT_DIR/critique/DIFF_MANIFEST_round_6.json" ADJUDICATE=auto bash "$S/scripts/run_critique.sh" 6
   ```

   The first driver call prepares the round and exits 3. Integrate round 6 as in SKILL.md (step-1
   copies, plan, apply, prose, approve and project, consistency gate, `round_result`, settled cache).
   If round 6 made a correction, or left a BLOCKING or conclusion-critical item, run round 7 the same
   way with `--round 7` and round 6's step-1 copies.
8. Log `review_scope` with the ordered union of every packet's `changed_claim_ids`. Then log
   `phase_end phase=critique at=<last round's end>` and `phase_start phase=release`. For Neudorfer
   this also repairs its journal, which records the critique phase ending after release started.
9. Rebuild the release set as in SKILL.md's release block: `build_docx.sh` (both document manifests
   are stale), the notebook, `run_verification.sh` (Neudorfer: on this machine), the claims script,
   `report/REPORT_v<N+1>.md`, then `stage_release.sh` into a directory that does not exist yet. Log
   the delivery and `phase_end phase=release`.
10. Ziv's `comment.json` records `governance.pii_present: true` with empty `disclosure.flags` and
    `disclosure.statement`. Reopened under 2.6.9, that is an error, so report each exposure there by
    kind, file, column and count, never by value (*Deposits containing personal data*).
11. Finalize and publish:

    ```bash
    I4R_REFINALIZE_PUBLISHED=1 bash "$S/scripts/finalize_run.sh" "$AUDIT_DIR" --stopping-reason converged
    # only after round 7, with something still unresolved and listed in review_scope:
    #   --stopping-reason cap_with_unresolved --stopping-detail "<one line>"
    bash "$S/scripts/publish_completed.sh" "$AUDIT_DIR"
    ```

The re-filed `run.json` shows `round_cap` 7 and `policy_amendments` (5 to 7, with its time and
reason). `reopenings` records the previous close (`cap_with_unresolved`, 5 rounds, its registry
version, its release folder and the `old_runs/` folder the filed set was moved to). Rounds 1-7 are
listed, 6 and 7 bound to the amended snapshot, and `compliance.policy_amendments_valid` is true. The registry keeps both versions, and the old published
set stays under `old_runs/`.

The 2.6.9 assessment also found three unfinalized audits with round 5 integrated: Frontiers pelvic
kinematics (on 2.6.4), IJSEP social norms (2.2.0) and PSE intergroup criticism (2.6.3). They follow
*An unfinalized audit that stopped after round 5* above: no reopen and nothing to move aside. Only
Frontiers has round 5's step-1 copies, so the other two make them first (step 4). A fourth, PSE effort
intensity (2.6.3), stopped after round 4. It needs no amendment yet: round 5 is inside its cap of 5.
It is amended only if round 5 leaves something that needs rounds 6-7, and only once round 5 is
integrated. Check each audit with `status.py` first.

## Participant codes and deposit column names (2.6.5)

Provider-visible text may not contain a participant-code-shaped token: two or more capitals,
optional underscore-joined capital segments, then `P` and digits (`SUBJ_P12`), or a `P12` that
opens a data row. Phase-1 admission, the post-fan-out verifier, the coordinator, the coordinator
recovery tools and the safe-evidence receipt apply the same rule (`scripts/provider_safety.py`).
So a report that would stop the coordinator is re-sampled in Phase 1 instead.

A token with that shape is admitted only when it is a column header or dictionary name in one of
the deposit's data files and never a data value anywhere in the deposit. Examples are an SPSS
column `INT_P05`, or a CSV header `DUR_P15` whose identifier column holds other values. It is not
admitted from a file or sheet that looks like one column per participant. If any non-code,
non-document file cannot be scanned for values, nothing is admitted. That includes an SPSS, Stata
or Excel file its reader rejects, a file whose bytes hide their text (Parquet, Feather, zstd, raw
zlib, HDF5, pickle, SQLite, `.xls` or other binary), and a directory that cannot be listed; Phase 1
refuses such a directory before any provider call. To see what a deposit admits, and why, run:

```bash
python3 -c "import sys; sys.path.insert(0, 'scripts'); import provider_safety as p; v = p.deposit_vocabulary(sys.argv[1]); print('exempt', sorted(v.exempt)); print('participant columns', sorted(v.participant_columns)); print('unreadable', v.unreadable, 'unscannable', v.unscannable)" "$AUDIT_DIR/pkg"
```

A code-shaped variable that exists only in syntax (created by `COMPUTE`, say, and not stored in a
data file) is not admitted. Auditors are told to describe such things in words. File names count
too, in source joins and evidence paths: a deposit file named like `EMG_P01.csv` is not cited;
its evidence is bound to another manifest entry, such as the script that reads it. A finding whose
only evidence is such a file cannot be evidence-bound. The code-plus-date
check on coordinator output is unchanged: a code-shaped token beside a date or clock time is
refused whatever the deposit says, as `check_consistency.sh` refuses it.

## Confirming findings raised after fan-out (2.6.4)

A row is promoted only with confirmations from two model families. The Claude-family (family-A)
confirmation of a Phase-1 finding comes from the auditor whose report raised it. A finding first
raised later — by the operator, by the coordinator, or as a critic's new issue — has no such
auditor. Before 2.6.4 it could never be promoted. In the Neudorfer audit three true article-text
findings (F13–F15) were excluded for that reason, after hand-made verification records had been
refused by `make_settled.sh` and then by finalize. The packaged verifier closes that gap without
weakening the two-family rule. Its contract is `post_fan_out_verification` in
`config/pipeline-policy-v2.json`, implemented once in `scripts/verifier_contract.py`, which the
runner and every gate call.

```bash
python3 scripts/run_verifier.py "$AUDIT_DIR" --findings F13 F14 F15             # one batch: the eligible rows, by current id
python3 scripts/run_verifier.py "$AUDIT_DIR" --findings F13 F14 F15 --dry-run   # the plan only; writes nothing
python3 scripts/run_phase1.py "$AUDIT_DIR" --recover-stranded                   # only after a killed verifier attempt
```

Exit codes: 0 completed, replayed (including a rebuilt receipt) or a `--dry-run` plan; 1 a usage
error such as a missing `--findings`, a missing audit directory or Claude CLI, or an unexpected
error; 2 the call failed; 3 a local preflight or sandbox fault; 4 the capability probe failed; 5
refused. A refusal prints its reason and a remedy, makes no provider call and appends no journal
event.

**Eligibility.** Only `INCLUDED` rows with non-empty claim text that were raised after fan-out:
none of the audit's Phase-1 roles (`phase_1.required_roles` in its own
`pipeline-policy.snapshot.json`) appears in `provenance.origin_role` or
`provenance.independently_found_by`. `round_introduced` is not used, because operator rows written
at coordinator time carry 0. A row that an auditor raised or independently found is confirmed by
that auditor and is refused here. The row must also carry a `source_location` (path and locator)
and cite, in `evidence.source_hashes`, at least one `paper/` or `pkg/` file with the Phase-1
snapshot's hash; the runner refuses a row without one. Two rows in one batch with the same claim
text are refused as well: send one, and the verdict binds to every row with that exact text.

**Identity.** Each call is recorded with `role_class` `verifier`, round 0, and role
`verifier_<12 hex>`, derived from the batch's sorted claim hashes. It is never `auditor`, never a
Phase-1 role name, and never counted in the five-auditor quorum. Its model is the Claude route in
the audit snapshot's `models.post_fan_out_verifier` (Sonnet 5.5/xhigh for a new audit, Sonnet 5.5/max
for one initialised on 2.7.0 to 2.7.2, Sonnet 5/max for one initialised before 2.7.0), and its served
model family must be the Phase-1 auditors' family. It runs under the Phase-1 runtime contract: sandbox, no network, an immutable
packet, the audit's frozen timeout (5,400 s from 2.7.2, 2,700 s before) and $15 provider cap per
attempt, the evidence manifest, source-hash
joins, admission, and at most one structural repair. That repair byte-freezes the verdicts, the
report prose, and every evidence artefact, including a repair recovered after a kill.

**What the verifier sees (blind framing).** Each hypothesis carries only an opaque id (`H1`…`Hn`),
the claim verbatim, its `claim_sha256`, the `source_location` (path and locator), and the `paper/`
and `pkg/` source hashes that equal the Phase-1 snapshot's. The packet withholds the finding id,
status, confidence, impact, verification tier, verifications, provenance, ledger disposition,
approvals, expected/observed values, rerun command and tolerance, benign explanations, remedy,
`changes_conclusion`, the verification snippet, settled state, and dependencies. It also withholds
`CHECKLIST.md` and `BRIEF_TEMPLATE.md`.

The sandbox rules are a deny list: whatever they do not name is readable. Phase 1 already denies
the live `paper/` and `pkg/` (an attempt reads the frozen snapshot instead), `report/`, `work/`,
`critique/`, `run.json`, the journal `run.events.jsonl`, the ledger `invocations.jsonl`,
`package_manifest.json` and `pipeline-policy.snapshot.json`. Every verifier attempt also denies
`coordinator/`, `SETTLED.json`, `SETTLED.md`, `comment.json`, `claims_map.tsv`,
`verification_snippets/`, `WRAPUP.txt`, `phase1/completions/` and `phase1/verifier/` (earlier
batches' receipts and status files name finding ids) and `phase1/transactions/`. On top of that
fixed list, each attempt's denials are computed when its settings are written
(`BatchPlan.deny_read`):

- every entry under `phase1/roles/` that does not lead to the attempt's own output: the five
  auditors' retained reports, other batches, and every other attempt of its own batch, rejected
  samples included (a repair also keeps its source attempt, whose response it repairs);
- every other verifier batch's packet under `phase1/role_packets/` (it names that batch's claims).
  The auditors' packets hold only the public brief, checklist and evidence indexes and are not
  denied;
- the artefacts finalization writes at the audit root, which exist whenever a round is reopened
  after finalize: `claims_script.R`, every root entry whose name starts with `verification`,
  `finalization_receipt.json`, `RELEASE_MANIFEST.md`, `release-manifest.json`,
  `document_build_manifest.json`, and, once one of those exists, `RUN_METADATA.md` and
  `run_metrics.json` (before finalization those two hold only call and token telemetry);
- the release directory beside the audit (`../release`);
- when the audit sits in a tree that `tree_layout.sh` recognises (its root holds `data/`,
  `reservations/` or `Completed reports/`), this audit's own records there: `registry/<audit_id>`
  and `<Journal>/<Paper>` under `comments/`, `metadata/`, `old_runs/` and `Completed reports/`
  (an earlier audit of the same paper included). No path that contains the audit is ever denied.

Only a path that exists and is not an empty directory is denied (2.6.6). The CLI's macOS sandbox
writes, for every denied path, an unlink and a create rule for the path and each of its ancestor
directories into a profile passed on the command line of every Bash call. On the tenth production
audit, whose working directory is 406 characters and 16 levels deep, the 47 denials 2.6.5 wrote
overflowed the 1 MiB exec limit: every command failed with E2BIG, and the verifier worked 45
minutes without a shell before its timeout. The trimmed list for that audit is 21 denials and
about 1,015,000 bytes of command line; the auditors' 10 are about 911,000. A very long audit path
can still approach the limit; shorten the paper folder name if a verifier reports that its shell
is unavailable.

So the verifier never sees a critic verdict, the coordinator's reasoning, an earlier verdict on the
same claim or a published comment. A denied path the sandbox cannot express (an entry under
`phase1/roles/` or `phase1/role_packets/`, or a root entry beginning with `verification`, whose name
holds a glob metacharacter) stops the run at the preflight (exit 3) rather than being left
readable; rename or remove it. The auditors pass no extra denial, so their settings and every hash
of them are unchanged.

**What it returns.** It returns a closed output set: `REPORT.md`, `result.json` and
`evidence/manifest.json` with the evidence files it lists. For each hypothesis it gives one verdict,
CONFIRMED, REFUTED or UNVERIFIABLE, with evidence joined to the snapshot hashes. A CONFIRMED or
REFUTED verdict needs a rerunnable artefact. An UNVERIFIABLE verdict must say what the deposit
lacks. The runner validates the output, then promotes it to `work/verifier/<role>/<input_sha256>/`
and writes the completion receipt `phase1/completions/<role>/<input_sha256>.json`. The receipt binds
the invocation id, the snapshot, the frozen packet, the result, report and evidence-manifest
hashes, the claim hashes and the source joins. `reference/adjudication_guide.md` maps verdicts to
registry outcomes: CONFIRMED is `confirmed`, REFUTED is `refuted`, and UNVERIFIABLE is
`inconclusive`. There is no OVERSTATED, and UNVERIFIABLE never counts as a confirmation.

**Re-admission.** Every consumer re-admits a verifier record exactly as the runner admitted it:
the runner's own refusal and replay, `integrate_round.py`, `make_settled.sh`,
`make_diff_packet.py` and finalize all read verdicts through `verifier_contract.collect_verdicts`,
whose `verify_record` ends in `run_verifier.readmit_record`. That requires:

- the runner's exact receipt (`RECEIPT_KEYS`), bound field by field to the call;
- the snapshot that `phase1/quorum.json` binds, whose manifest must hash to itself;
- the frozen packet under `phase1/role_packets/<snapshot>/<role>/<packet_sha256>/`, which must hash
  to the receipt's `packet_sha256` with the runner's prompt and carry the receipt's hypotheses;
- the full output validation (`validate_verifier_output`) of the promoted tree, with the receipt's
  hashes and source joins;
- the call's retained model-identity evidence, still at its recorded hash.

Four files that hash to what a receipt says prove nothing, because anyone can write all four; so a
hand-made record is refused whatever its hashes, and every record is refused if the runner cannot
be imported. Operating-system metadata files in a promoted tree (`.DS_Store`, `Thumbs.db`,
`desktop.ini`, `Icon\r` and `._*` files, which Finder or a sync client may write) are ignored, but
any other extra file breaks the closed output set and makes the record inadmissible. A Phase-1
re-run that binds a new snapshot in `phase1/quorum.json` makes every earlier verifier record
inadmissible, because its receipt names the old snapshot (see *Recovery*).

**The journal.** A verifier call is journaled only by `run_verifier.py`, through the lifecycle
API. The command-line appends (`lifecycle_journal.py append-event`, `append-kv` and `append-batch`,
and so `log_event.sh`) refuse, before anything is appended, any lifecycle event (reservation,
provider request, inference start, response, terminal row, cache reuse or correction) that names
`role_class` `verifier` or an invocation already journaled as a verifier. `record_invocation.py`
refuses the same through `--reserve`, `--provider-request`, `--inference-start`,
`--response-received`, `--cache-reuse` and the positional record. `record_invocation.py --correct
<audit-dir> <invocation-id> <correction.json>` still works, and it is the only supported way to
supersede a verifier call; *Recovery* says when.

**Binding.** A verdict binds to a row by the SHA-256 of the row's current `claim`, never by finding
id. So `renumber_findings.py` after the verifier is harmless, and any edit to the claim text voids
the old verdict.

**Timing.** `integrate_round.py apply` binds a verifier confirmation only if the call ended no later
than that integration's timestamp, and `make_settled.sh` accepts it only if it ended no later than
the latest round integration (a record citing a later call stops `make_settled.sh`, as it does for
any reviewer). So:

- run the verifier on coordinator or operator rows written at draft time before round 1;
- run it on a row a round's critics raised as a new issue after that round is integrated, and
  before the next round is integrated (normally once the next round reaches quorum);
- a verifier call that ends after the last integration binds nothing. A new issue raised in the
  final planned round can be promoted only if a further routed round is integrated after the call.

Each call journals invocation rows, so it also stales any `SETTLED.json` built before it. Keep it
out of the pre-finalization chain above.

**No verdict shopping.** Each claim text gets exactly one verdict:

- the runner refuses to send a hypothesis whose claim hash already has an admitted verdict, from
  any earlier batch;
- a verifier confirmation counts only if the cited call returned CONFIRMED for the row's current
  claim hash and no other admitted verifier call returned anything else for that hash. A REFUTED or
  UNVERIFIABLE verdict therefore cannot be outvoted by running again;
- a REFUTED or UNVERIFIABLE verdict keeps blocking its claim text after its call is corrected to
  `legacy_record`, as long as its record still re-admits; the correction removes only a CONFIRMED
  (a superseded call confirms nothing). So the correction tool cannot erase a non-confirming
  verdict;
- the only way to a new verdict is a new claim text. That is a registry change like any other: make
  it through the round's `integrate_round.py apply` decisions, so the next diff packet lists the
  changed claim and the next critique round reviews it. After a REFUTED or UNVERIFIABLE verdict the
  edit must change the claim's substance, not only its wording or punctuation, and it must be
  visible in `RESPONSE_notes.md`; `reference/adjudication_guide.md` gives the rule.

**What each gate does with a verifier record.**

- `integrate_round.py` binds a verifier record, in every round, to each `INCLUDED` row raised after
  fan-out whose current claim text a completed call returned a verdict for, when the call ended no
  later than the integration. `apply` drops a verifier record already on a row, and gives the
  reason in `RESPONSE_notes.md` and `ADJUDICATION.md`, in three cases: its call was corrected to
  `legacy_record`; a Phase-1 auditor raised or independently found the row; or the claim was
  edited, so it is no longer the text the call verified. A superseded call whose REFUTED or
  UNVERIFIABLE still blocks the current text is listed there too.
- `make_settled.sh` rejects a row, with a `verifier:` reason, whenever a confirming verifier record
  on it does not count: the cited call returned no CONFIRMED for the current text, another call
  returned REFUTED or UNVERIFIABLE for it, a Phase-1 auditor raised the row, or any verifier record
  in the ledger is inadmissible. Every other row settles as usual. Two states still stop
  `make_settled.sh`, as for any reviewer: a record citing a call that ended after the latest
  integration, and a record citing a call corrected to `legacy_record` (the next `apply` drops it).
- `make_diff_packet.py` refuses a settled fact whose verifier confirmation is not admissible.
- Finalize lists each verifier call apart from the auditors (`compliance.verifier_calls` and
  `verifiers_ok`, emitted only when a verifier row exists), checks each completed call against the
  contract, reports every inadmissible record and every citation that does not count for the
  row's current claim text as a violation, and checks every verifier row that did not complete
  (failed, timed out, cancelled) for a `verifier_<12 hex>` role, round 0 and a non-critic family.
  In `comment.json` a reviewer id counts as a model rather than human verification only when it is
  the role of a `role_class` `verifier` call in the run, so a person code such as `verifier_1`
  stays human.

**Budget.** The default audit makes no verifier call. Each batch is one Sonnet call at the audit's frozen
effort (xhigh from 2.7.3, max before), normally
replaying the audit's cached Phase-1 capability probe rather than making a new one. Retries of the
same batch share one role and so one budget: 3 accepted inference starts per (role, round 0) by
default, the focused repair included, raised with `I4R_MAX_ATTEMPTS` (up to 20). A request the
provider rejects before inference charges no start but counts toward a separate limit of two
(see *Recovery*). Put every eligible row in one batch; a different set of hypotheses is a different
role with its own budget, and the ledger shows every batch you ran. A rejected sample is not a
verdict; recomposing a batch opens a new budget for every claim in it. An exhausted batch never
needs `init_run.sh --force`, which would re-run all five auditors (and, since 2.7.0, move an
older audit to Sonnet 5.5). If environment or harness
problems spent the attempts, raise `I4R_MAX_ATTEMPTS` and rerun the same command; that lifts the
three-start ceiling only, never the pre-inference limit.

**Recovery.**

- *A killed attempt* (sleep, OOM, Ctrl-C) is a strand under `phase1/roles/<role>/`, exactly like an
  auditor's, and the stranded-attempt guard then refuses any further Phase-1 or verifier dispatch
  for the audit. Clear it with `python3 scripts/run_phase1.py "$AUDIT_DIR" --recover-stranded`.
  That terminalizes the attempt as failed and charges nothing against the three-start budget (see
  *When something dies mid-Phase-1*), but it counts as a pre-inference request: after two recovered
  strands, or two provider rejections before inference, or one of each, the runner refuses that
  batch for good, whatever `I4R_MAX_ATTEMPTS` says. The refusal's remedy text mentions only
  credentials and quota. A strand whose response was retained goes through the received-response
  path instead.
- *A lost receipt.* If the runner dies after journaling the completed call and before writing its
  receipt, re-run the same command, with that batch's own `--findings`. The runner finds its own
  completed row, rebuilds the receipt from the retained response with no provider call and no
  budget, re-admits it in full and records a `cache_replay`; `--dry-run` shows
  `would_rebuild_receipt: true`. Never correct a lost-receipt row to `legacy_record`: the retained
  response can then never be adopted, and every re-run of that batch fails with "already has a
  non-completed terminal record".
- *An inadmissible record.* Any completed verifier record that does not re-admit makes the runner
  refuse every batch (exit 5) and stops `integrate_round.py` with `FATAL: verifier record is not
  admissible`, because an unknown record could hold a REFUTED for any claim. `make_settled.sh`
  rejects only the rows that cite a verifier record as a confirmation, and finalize reports a
  violation.
  `integrate_round.py` prints the remedy, in this order:
  1. if the runner was killed after journaling the call, so the receipt
     `phase1/completions/<role>/<input>.json` is missing, re-run that batch's own command,
     `python3 scripts/run_verifier.py "$AUDIT_DIR" --findings <the batch's rows, by their current
     ids>`; it rebuilds the lost receipt from the retained response with no provider call;
  2. otherwise restore every changed file of the record (`work/verifier/<role>/<input>/`, its
     receipt, its frozen packet under `phase1/role_packets/`) to its recorded hash;
  3. only for a genuinely tampered or foreign record, mark it superseded:
     `python3 scripts/record_invocation.py --correct "$AUDIT_DIR" <invocation-id> correction.json`,
     where `correction.json` is `{"set": {"status": "legacy_record"}, "reason": "..."}`. Never do
     this for a lost receipt, which leaves that batch unrecoverable.
- *A Phase-1 re-run.* If Phase 1 is re-run so that `phase1/quorum.json` binds a new snapshot, every
  earlier verifier record names the old snapshot and no longer re-admits, so the gates fail closed
  as above. Steps 1 and 2 cannot fix that: such a record is foreign to the current Phase 1, and
  step 3 is the only way forward. A superseded record that no longer re-admits blocks nothing, so
  its claims can then be verified again against the new snapshot; say why in the correction's
  `reason`.

**Audits initialised before 2.6.4.** Their frozen `pipeline-policy.snapshot.json` has no
`models.post_fan_out_verifier` and is never rewritten. For them the verifier uses the single route
that all five Phase-1 auditors in the snapshot share, which for an audit that old is Sonnet 5 / max
on Anthropic. If the snapshot's auditors do not share one route, there is no
defensible verifier route and the runner refuses. Nothing else changes for such an audit: with no
verifier row its outputs are byte-identical to 2.6.3, apart from the two general fixes listed in
the 2.6.4 CHANGELOG (a Phase-1 start above three under a raised `I4R_MAX_ATTEMPTS`, and a critic-path
record that started in the same second as a critic re-pin). Legacy hand-made verification rows are
unaffected: the Neudorfer `operator_verifier` and `auditor_operator_verification` rows, corrected
to `legacy_record`, have role class `other` or `auditor`, and every verifier check keys on
`role_class` `verifier`, never on a name.

## Identifying values in the audit's own outputs (2.6.9, stage check 3c)

A deposit that holds personal data is audited and reported like any other; nothing refuses an
audit because of what the deposit contains. What must never happen is the audit's own output
quoting an identifying value. Checks 3, 3b and 4 of `stage_release.sh` know participant codes,
timestamps and paths, and since 2.6.8 check 4 (`check_release_privacy.py`, also run by publication
and by the post-delta privacy gate) refuses any e-mail that is neither the byline's nor printed in
the article. A finding that quoted a participant's name, date of birth, IP address, postal address
or phone number passed all of them. Check 3c, `scripts/check_deposit_values.py`, closes that gap:

- It reads the frozen deposit (`pkg/`, else the newest Phase-1 snapshot copy): CSV, TSV and
  table-shaped TXT, XLSX, JSON, the same inside `.zip` and `.gz`, and, when `Rscript` has the
  `haven` package, SPSS, Stata, SAS and R files. It collects the values of every column whose
  header (or SPSS/Stata label) looks like a name, e-mail, date of birth, IP address (IPv4 or
  IPv6), postal address or phone number.
- **Language coverage.** Headers are recognised in English, German, French, Spanish, Italian,
  Portuguese and Dutch: Name, Vorname, Nachname, Nom, Prénom, Nombre, Apellido, Nome, Cognome,
  Sobrenome, Naam, Voornaam, Achternaam; E-Mail, courriel, adresse électronique, correo
  electrónico; DOB, Geburtsdatum, date de naissance, fecha de nacimiento, data di nascita, data de
  nascimento, geboortedatum; IPAddress; Adresse, indirizzo, endereço; Telefon, téléphone,
  teléfono, and so on. Written dates of birth are read in the same seven languages. **A header in
  any other language is not recognised**, so its column is not collected. When a deposit's
  personal-data headers are in another language, check the comment's disclosure finding by hand
  for quoted values.
- It keeps keyed hashes in memory only, under a key drawn for the run. Nothing value-derived is
  written to disk: no store, no cache. Every staging and publication reads the deposit again, in
  seconds. It prints locations and value kinds, never a value, and names a deposit source by its
  manifest path, column and an ordinal (`sheet 2`, `member 5`, `object 3`). It never prints a
  sheet, archive-member or R-object name.
- It **refuses** any staged text, PDF text or metadata, or DOCX location that reproduces a
  multi-word name or address from the deposit, in any case and either order. It also refuses a date
  of birth in any written form, a deposit e-mail, IP or phone value, and any other e-mail, IPv4 or
  IPv6 address. 3c's exemption below for an author's contact address copied from the deposit's
  code does not exempt it from check 4, which accepts only the byline's and the article's
  addresses. Both checks honour check 4's per-line escape, `# i4r-privacy: not-an-email`, on a
  code-file line whose "address" is code (a matrix product such as `q@keys.data`); in 3c it exempts
  the generic detector only, so a deposit's own address is refused on a marked line too (2.6.9).
- It **warns**, and never refuses, when a single word matches a one-word name or address value
  (a given name, a surname alone). Read each warning: it is usually an ordinary word, a
  statistical eponym or a cited author. Reword any warning that quotes a participant.
- Exempt everywhere: the paper's own text, `reference/house_citations.md`, the comment's own
  References section, the whole of `report/front_matter.md`, documentation domains and address
  ranges, private-network addresses, and version-shaped numbers such as `LibreOffice 24.2.4.2` or
  `Python 3.12.12`. The skill's generated and provenance files (`scripts/`, `SETTLED.*`,
  `ENV.txt`, the manifests and receipts, telemetry, `critique/id_map_history.jsonl`, and the
  provenance fields of `run.json` and `comment.json`) are never matched against deposit values. The e-mail and IP detectors still
  read them. The module docstring lists every rule, and its self-test runs first.
- A deposit file it cannot read is listed as "not scanned", a file it read only in part as "read
  in part", and a personal-data column with no usable value as such. None of these refuses
  anything.
- A value store (`work/deposit_value_hashes.json`) left by a pre-release build of this gate is reported
  on every run. It holds keyed hashes together with their key, so it counts as personal data:
  delete it. Nothing reads it.

**Records nobody can reword are masked in the staged copy (2.6.9).** A Phase-1 auditor report
(`report/0N_*.md`) is hash-bound: editing it makes finalize fail with "SETTLED.json facts[..]
reviewer evidence record is stale". A critic's output is model output that is never edited, and a
work record or evidence script under `work/` or `phase1/` is bound into its role's evidence
manifest. When one of them quotes a deposit value (the PII-bearing deposits the report-not-block
policy now audits make that likely), "reword the finding" has no remedy and the audit could never
stage. So `stage_release.sh` runs check 3c with `--mask-records`: each matched span in the STAGED
copy of such a record (`report/0N_*.md`, `report/ADJUDICATION.md`, `report/retired_claims.txt`,
`PAPER.txt`, `BRIEF.md`, and everything under `critique/`, `work/`, `phase1/` and `coordinator/`
except the provenance files) is replaced with `[masked at release: <class>]`, every masking is
listed in the release's `RELEASE_VALUE_REDACTION.txt` (file, line and class, never the value), and
the release is scanned again. The audit's own files are never touched, and nothing masked is ever
filed: `publish_completed.sh` files only the comment outputs and checks them again. A record the
masking cannot rewrite (HTML, UTF-16) still refuses; report it. Since 2.7.6 check 3d does the same
for home paths and e-mail addresses that check 4 refuses: in the staged copy of the same records the account
segment becomes `<user-dir>` and the address `[e-mail address]`, listed in `RELEASE_PATH_NEUTRALISATION.txt`.

**Remedy when it refuses (exit 1).** The refusal prints the remedy for each kind of file it names.
For the comment's own outputs (`report/REPORT_final.md` and `REPORT_vN.md`, `LEDGER.md`, the front
matter and captions, the comment PDF and DOCX, `comment.json`, the verification notebook and
summary, the claims script), which are never masked, reword the finding so that it describes the
identifier by kind, file, column and count ("data/survey.csv, column Name: 57 participant names"),
never its value. If the value came in through a finding row's `evidence.observed`, which is projected
verbatim into `comment.json`, record a count there instead. A rewording at release changes the
report and the registry, and every pre-finalization artefact binds the one before it, so rebuild in
the order *Validate before the first audit and after every policy edit* gives (approve, project,
approve, project, `make_settled.sh`, the settled event, then round N's packet and post-delta gates;
see also *Two more orderings*), then the release set as in step 4 of *Correcting a published
comment* (DOCX/PDF, notebook and verification, claims script, a new `REPORT_v<N+1>.md` snapshot),
staging into a fresh directory (move the refused one aside) and re-logging the delivery and
`phase_end phase=release`, then finalize. `stage_release.sh` also refuses a stale last-round
post-delta receipt (2.6.8). A reworded claim is a changed claim: `target_complete` and `converged`
require every changed claim to have had a later adversarial review, so an audit that stopped at
either may need one more routed round before it finalizes. `publish_completed.sh` runs the same
check again on the exact publication set (`run.json` and `comment.json` included) before anything is
filed. For an audit staged under 2.6.8 or earlier, see *Upgrading in-flight audits to 2.6.9*.
Any other staged file, such as an operator note (`WRAPUP.txt`, `SLOW.txt`), is edited in the audit
and restaged.

**When it cannot inspect (exit 2):** a staged PDF or DOCX could not be read, pdftotext or pdfinfo
is not on PATH, or the check stopped on an internal error, printed by its class only. Nothing was
certified, and no rewording helps. Rebuild the document, or put poppler on PATH (the `i4rdoc`
environment has it), then restage or re-publish. `stage_release.sh` and `publish_completed.sh`
print a distinct message for each case.

The coordinator's copy of `package_manifest.json` is masked in the same spirit. A deposit whose
file names are participant-code-shaped (one file per participant, named after the code) used to
make `run_coordinator.py` refuse before any call, and `recover_coordinator.py` refused the same.
The coordinator now sees each code-shaped path segment replaced by a stable placeholder
(`masked-file-001.csv`), with every entry's size and sha256 kept and the holding directories
summarised as "directory, N files". The participant-code rule itself is unchanged: a placeholder
is not a deposit path and cannot be cited. A manifest with no code-shaped name reaches the
coordinator byte for byte, as before.

## Environment variables

Every knob an operator can set. `config/check_policy_drift.py` checks that every `I4R_*` variable a
script reads appears in this file, so a new knob cannot ship undocumented.

**Attempt ceilings — two knobs, one per subsystem.** This split has cost real time, so read it once:

| Variable | Applies to | Notes |
|---|---|---|
| `MAX_TRIES` | a critique round | 1-20, default 3. `run_critique.sh` **exports it as `I4R_MAX_ATTEMPTS`**, so setting that variable from outside a critique round has no effect |
| `I4R_MAX_ATTEMPTS` | Phase 1, the coordinator, the post-fan-out verifier, and the standalone recovery scripts | 1-20, default 3. A recovery script run by hand reads this, *not* the `MAX_TRIES` the round used |

**Workflow controls**

| Variable | Effect |
|---|---|
| `I4R_AUTO_AUTHORIZE` | **default 1.** The critique driver journals a grant for its own unmodified round plan and continues. Set `0` to restore the attended pause (exit 3). The grant is journaled and bound to the exact plan either way |
| `I4R_SUPERSEDE_PLAN=1` | with `I4R_AUTO_AUTHORIZE=0`: re-prepare a round whose report, registry or packet changed after it was prepared (the driver otherwise refuses); the superseding plan needs its own `authorize-round` and, from round 2, fresh post-delta gates (the driver refuses a round-2+ dispatch whose gate receipt is stale) |
| `I4R_FORCE_RERUN=1` | ignore cached role results for the round. On a metadata-2.1 round that already reached quorum (or the driver's own `round_integrated` event), the journal reopens the round under its live grant and the roles re-dispatch. The inputs must be byte-identical, so this works only BEFORE `integrate_round.py apply` changes the registry; after the coordinator has integrated a round, review the change in round N+1 instead |
| `I4R_PROVIDER_TIMEOUT_SECONDS` | wall-clock ceiling per critique provider call. An audit initialised on 2.7.4 or later froze 5400 (`critique.provider_timeout_seconds`) and this may only lower it; an older audit runs at 2700 and this may raise or lower it. A positive decimal integer; a call that exceeds it is stopped and classified transient |
| `I4R_SNAPSHOT_REFREEZE` | **set to `1` by `run_critique.sh` itself; otherwise leave it unset.** With it, a frozen critique snapshot that got a write bit back while every byte still matches its manifest (a synced folder re-applying a mode; Dropbox did it on 2026-09-24 and 2026-09-27) is re-frozen in place and journaled once as a `snapshot_mode_refrozen` note instead of failing the paid response. The one hand-run exception: `recover_coordinator.py` refuses a coordinator attempt whose `source/` only drifted in mode and names this variable; set it for that one run to re-freeze after the same integrity checks. Unset, every snapshot check is strict and read-only, which the helper library, the one-off recovery scripts and compatibility sweeps over published audits rely on. A changed file, an extra or missing file, a symlink or a damaged `MANIFEST.json` is never repaired: the driver checks the snapshot before every paid dispatch and stops with `[stop ] ... no provider call was made` (after a call: `error_class` `environment`, not retried). Remedy: in the audit directory run the command it prints, `chmod -R u+w <dir> && rm -rf <dir>` for the content-addressed directory it names (`critique/round_N/snapshots/<sha>`, `critique/round_N/target_snapshots/<sha>` or `phase1/snapshots/<sha>`), then rerun; a plain `rm -rf` fails on a frozen tree, and the directory is rebuilt identically. A mode that keeps coming back after three re-freezes while the content verifies is `snapshot_mode_unstable` (`[stop ] ... snapshot modes keep changing (content verified)`), not a content change: stop the sync client or move the audit off the synced folder, then rerun; deleting does not help. Phase 1 and the verifier re-freeze their own snapshot without the variable, only after the pre-dispatch deadline and never under `--dry-run`, which reports `would_refreeze: N` and exits 0. They check the snapshot's inventory first, so a file that landed in a drifted directory is refused, not sealed |
| `I4R_COORDINATOR_TRANSIENT_RETRIES` | how many times `run_coordinator.py` retries a transport- or transient-class failure within one invocation, default 2. Each retry is a budgeted attempt under `I4R_MAX_ATTEMPTS`; other failure classes never retry. Backoff is `RETRY_BACKOFF_SECONDS` × retry number |
| `BUILD_DOCX_CACHE_MODE` | `use` (default), `bypass`, or `refresh` for the validated document cache, which is bound to the report, front matter, captions, reference document, build code, rendering-tool identities, output name and page-limit options; `--no-cache` (bypass both lookup and storage) / `--refresh-cache` (rebuild and replace the validated entry) set it |
| `I4R_OWNER` | reservation owner identity for `reserve_paper.sh`, default `<user>@<host>`; set it when the same person claims from two machines. `capture_environment.sh` records it as the environment's `operator`, unless a publication gate would read it as an e-mail or IP address or the coordinator's provider-safety scan refuses it: then `<user>@<host>`, or no operator if that is refused too (2.7.4) |
| `SKILL_DIR`, `PROTOCOL_VERSION`, `AUDITOR_MODEL`, `AUDITOR_FAMILY`, `AUDITOR_PROVIDER` | `init_run.sh` defaults for the corresponding flags. `AUDITOR_MODEL` must be the shipped policy's model (`sonnet-5-5` since 2.7.0); an old shell that still exports `sonnet-5` makes a new init refuse |
| `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL` | removed from every Claude CLI subprocess the runners start (2.7.0, `scripts/claude_routes.py` `REMAPPING_ENV`), so they cannot move an audit off the model its snapshot names. Your shell keeps them |
| `RUN_JSON` | the run to journal into, so `log_event.sh --run` can be omitted |
| `CRITICS`, `PARALLEL`, `ADJUDICATE`, `DIFF_MANIFEST` | see `scripts/run_critique.sh`'s usage header |
| `I4R_PAPER_DIR` | the paper directory this audit reserves, when the audit dir deliberately sits elsewhere (see *Audit directory naming*) |
| `I4R_SKIP_RESERVE=1` | skip the paper reservation. Say why in the run notes |
| `I4R_ALLOW_EXISTING=1` | re-use a populated intake directory: `pkg/` is emptied and copied again from the deposit (never pass the audit's own `pkg/` as the package) |
| `GATE_SET_VERSION` | override the derived gate-set stamp |
| `REGISTRY_DIR` / `I4R_REGISTRY_DIR` | registry root for `finalize_run.sh`. **Not** read by `finalization_receipt.py check` or `publish_completed.sh`, and neither takes `--registry`: they read the registry location from `finalization_receipt.json`, which `finalize_run.sh` writes. Only `finalize_run.sh --registry` (and `finalization_receipt.py create`, which it calls) choose it. When neither is set and the audit is not at `<tree>/runs/<journal>/<paper>/audit` (or the legacy `<tree>/<journal>/<paper>/audit`) under a tree holding `registry/`, finalize **refuses** rather than filing into the skill checkout; `I4R_ALLOW_SKILL_LOCAL_REGISTRY=1` restores the fallback for fixtures |
| `I4R_CREATE_REGISTRY=1` | let `finalize_run.sh --registry <dir>` (or `REGISTRY_DIR` / `I4R_REGISTRY_DIR`) create a registry folder that does not exist yet, only for an audit outside a tree that already has `registry/`; in a new tree (markers but no `registry/`) only `<tree>/registry` itself; otherwise a missing folder is refused as a likely typo |
| `I4R_PUBLISH_ANY_ROOT=1` | let `publish_completed.sh` publish an audit that is in no tree (no `data/`, `reservations/`, `Completed reports/` or `registry/` at its root), or into a second-argument root other than the audit's own tree root (test fixtures only) |
| `I4R_REGISTRY_OVERRIDE=1` | let `finalize_run.sh --registry <dir>` (or `REGISTRY_DIR` / `I4R_REGISTRY_DIR`) file into an existing registry other than the audit's own tree registry (in a new tree, `<tree>/registry`); it never creates a missing folder |
| `I4R_REFINALIZE_PUBLISHED=1` | let `finalize_run.sh` re-finalize an audit that was already published, after its published folders were moved aside (see 'Correcting a published comment') |
| `I4R_CANONICAL_SKILL_DIR` | where `init_run.sh` looks for the canonical skill's `VERSION`, default `<tree>/skill/i4r-audit-unified/i4r-audit`. A running copy whose `VERSION` differs, or has none, is refused at intake |
| `I4R_ALLOW_SKILL_VERSION_MISMATCH=1` | downgrade that refusal to a warning; the running copy's own version is still what gets stamped |

**Paths and binaries**

| Variable | Effect |
|---|---|
| `CODEX` | the Codex binary. `run_critique.sh`, `capture_environment.sh` and the recovery scripts read `CODEX`, then `I4R_CODEX_BIN`; an explicit value is used as given, even when it is older than another installed CLI or does not exist (the driver then refuses with the path). With neither set, `scripts/codex_cli.py` chooses the newest working CLI (2.6.10) |
| `I4R_CODEX_BIN` | the same, read after `CODEX` |
| `I4R_CODEX_KNOWN_PATHS` | colon-separated list that replaces the known install locations `codex_cli.py` searches before `PATH`. Tests and diagnosis only |
| `I4R_STATE_DIR` | where `check_critic_access.py` caches its 24-hour pass (`critic_access.json`); default `~/.i4r` (2.7.5) |
| `I4R_AUDIT_DIR`, `I4R_PKG_DIR`, `I4R_SNIPS_DIR`, `REPORT_PATH` | path overrides for the verification and snippet tooling |
| `I4R_FINDING_SCHEMA`, `I4R_INVOCATION_SCHEMA` | schema path overrides |
| `I4R_SKIP_FINDING_SCHEMA=1` | skip finding-row schema validation. Diagnostic only |
| `I4R_DEPOSIT_VALUES_NO_R=1` | stage check 3c reads no file through `Rscript`: SPSS, Stata, SAS and R files are listed as not scanned, as on a host without R. Tests and diagnosis only |

**Timing and locks**

`RETRY_BACKOFF_SECONDS`, `PROBE_LOCK_WAIT_SECONDS`, `LOCK_TIMEOUT`, `I4R_WAIT_POLL_SECONDS`,
`I4R_RESERVE_GRACE`, `I4R_RESERVE_STALE_DAYS`, `I4R_INVOCATION_ID` (pin an invocation id, used by
the test harness).

**Re-pinning a live audit's critic model.** To move an audit pinned to an older route (`gpt-6-sol`,
`gpt-5.6-terra`) with no completed review to the shipped route, run (an account that cannot use the
model an audit pins, which the probe reports with `unsupported_model` and the provider's own words, is
changed instead: `scripts/check_critic_access.py`)

```bash
python3 scripts/repin_critic_route.py "$AUDIT_DIR" --dry-run            # every check, nothing written
python3 scripts/repin_critic_route.py "$AUDIT_DIR" --reason "<why>"
```

It moves the audit to the shipped policy's route (GPT-6.1 Sol since 2.7.0) and, since 2.7.5, to nothing else:
`--critic-model` and `--adjudicator-model` other than the shipped values are refused. So it serves an audit pinned
to an older route; an account that cannot use Sol is changed instead (`scripts/check_critic_access.py`). It
refuses once any critic or adjudicator review has
completed or a round has reached quorum (one audit's reviews stay on one route) and while a driver
holds a round lock, keeps the replaced route in `policy.critic_route_history` so earlier probes and
failed attempts still validate, archives the old snapshot read-only, and journals the change. Then
re-run the round; the driver prepares a fresh plan. Once a review has completed, the tool refuses:
re-run the round from an account that offers the pinned model instead.

**Pinned by the audit's policy snapshot.** `CRITIC_MODEL`, `CRITIC_EFFORT`, `ADJUDICATOR_MODEL` and
`ADJUDICATOR_EFFORT` are read, but any value other than the route recorded in the audit's
`pipeline-policy.snapshot.json` (written at `init_run.sh` from `config/pipeline-policy-v2.json`, currently
GPT-6.1 Sol at high for critics and max for the adjudicator; GPT-5.6 Terra before 2.7.0, except GPT-6 Sol in 2.6.0) is a `fail_early`. An audit initialised under an
earlier policy keeps its own route for its whole life; only new audits pick up a policy change. The
post-fan-out verifier's route is read from the same snapshot: an audit initialised before 2.6.4 has
no `models.post_fan_out_verifier` in it and uses the single route its five Phase-1 auditors share
(see *Confirming findings raised after fan-out*).

## What each audit retains

| Artefact | Location |
|---|---|
| Comment source and built files | `report/REPORT_final.md` and `report/*.{docx,pdf}` |
| Five area reports | `report/01_reproduction.md` through `report/05_privacy_archive.md` |
| Ledger and adjudication | `report/LEDGER.md`, `report/ADJUDICATION.md` |
| Generated finding projections | `report/APPENDIX_A.md`, `claims_map.tsv`, `work/verification_plan.json` |
| Critic reports and responses | `critique/round_*/` |
| Work scripts, logs and evidence | `work/` |
| Verification | `verification.Rmd`, snippets, HTML, summary CSV, status file |
| Verification binding | `verification_manifest.json` |
| Claims handoff | `claims_script.<deposit-language-extension>` |
| Canonical finding rows and settled cache | `work/findings.jsonl`, `SETTLED.json`, `SETTLED.md` |
| Post-fan-out verifier outputs | `work/verifier/<role>/<input_sha256>/` (`REPORT.md`, `result.json`, `evidence/`) and the completion receipt `phase1/completions/<role>/<input_sha256>.json` |
| Process and finding metadata | `run.json`, `comment.json`, `run.events.jsonl` (the software environment: the `environment_captured` event, projected into `run.json` as `environment`) |

Run audits in a durable programme directory, never a temporary scratchpad. Model identity, token
usage, human effort, failures, and phase times must be logged live; they cannot be reconstructed
reliably after the session or its logs are deleted.

The metadata pair is internal and defaults to `safe_to_publish: false`. Read its
`publication_redaction` block before releasing any extract outside I4R.
