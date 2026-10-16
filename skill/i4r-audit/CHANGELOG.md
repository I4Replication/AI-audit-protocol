# Changelog

## 2.7.7 — 2026-10-09

Two defects that stopped three audits and would have stopped a fourth, and the install command. Nothing an audit
froze changes: no policy value, prompt, packet, receipt or snapshot byte, so every in-flight audit picks the fixes up
as it is.

- **`validate_v2.py run` accepts events masked by `text_redaction` (post-release fix, 2026-10-09).** The 2.7.6
  redaction masks a free-text field as `[redacted]` in the `run.json` projection, but the validator re-hashed that
  masked copy, so finalize refused every audit that used a redaction ("run.events[N].event_hash: does not hash the
  canonical event"; first seen on audit A01). An event that a later `text_redaction` names, and whose field
  reads `[redacted]`, is now checked by hash-chain continuity (the next event's `previous_hash`) instead of
  re-hashing; the append-only journal itself is unchanged and stays fully hash-verifiable.

- **Phase-1 admission no longer reads "participants ... 22:40" as a participant linked to a time.** The
  `participant_date` rule refused a role report line holding the word participant(s) or subject(s) followed by any
  word, together with any one- or two-digit number, a colon and two digits. The paper of audit A02 reports bedtimes (a mean clock
  time) and that of audit A03 names its conditions by ratios (such as 30:70 and 50:50). Audit A02 was parked
  before Phase 1 rather than spend attempts on it, and audit A03's code-and-methods role failed all
  six. The rule now needs an identifier and a date or a clock time. An identifier is what follows the label: a token
  holding a digit (`participant 12`, `subject07`, `Subj. 12`, `#12`, `sub-01`, also in backticks, quotes or bold), a
  label word and a code (`Participant ID 12`, `whose ID is 12`, `participant_id != 'S07'`), one to four capitals that
  are not a statistic (`Participant A`, `JMK`; not `SD`, `RPE`, `TST`), or a list of codes (`participants 12 and 14`,
  ``Subjects `S03` and `S07` ``). A lowercase word, a range or a count is not one (`participants were`, `participants
  18 to 30 years old`, `subjects 1-24`), nor is a capitalised word, so a named pseudonym (`Participant Anna`) is
  admitted. A time is a clock time (hour 0-24, minutes 00-59, also after an ISO date's `T`), so 30:70 and 50:50 are
  not times. The rule also requires the 2.7.6 pattern pair, so no line admitted before is refused now, and
  re-admitting an earlier verifier record is unaffected. Over the 537 Phase-1 reports on disk, 2.7.6 refused 28 lines
  in 9 reports and 2.7.7 refuses 1 (a participant code beside a date); all six refused reports of audit A03 pass. Three
  review rounds shaped the identifier forms, and every separator run is bounded, so a long line is checked in linear
  time. The privacy scan keeps the 2.7.6 patterns, because its counts are Phase-1 evidence. The comment's consistency
  gate looks for code-shaped identifiers and is unchanged.
- **A consistency check before round 1 no longer makes a round.** Operators logged a consistency check run before the
  first round as `round=0` (the `<n-or-0>` SKILL gives model and token events), and the projection turned every
  `check_consistency` gate into a round row, so `run.json` gained a round 0. Finalize (0c,
  `validate_round_projections.py`) requires rounds 1..N, and the append-only journal can never drop the row: audit
  A04, converged and staged, could not finalize, and audit A05 would have stopped
  there too. A round-0 gate now stays in `compliance.gates_run` and makes no row. `lifecycle_journal.py project`
  re-projects an existing audit's `run.json` from its unchanged journal.
- **Docs: the install command** (a docs revision of 2026-10-08 that was never shared, first shipped here). On a Mac
  without `~/.claude/skills`, `cp -R i4r-audit ~/.claude/skills/` creates that directory from the skill's contents,
  so Claude Code finds no skill. CHEATSHEET step 2 and INSTALL.md now create the directory first, move an existing
  copy out of it before an update, and say that every Mac holds its own copy. The CHEATSHEET's sample `VERSION`
  output read 2.6.2. The CHEATSHEET beside the shared zip (`skill/i4r-audit-unified/CHEATSHEET.html` and `.pdf`)
  dated from 2026-08-24 and is replaced by the skill's own.

## 2.7.6 — 2026-10-07

Six defects that left real audits unable to continue. Nothing an audit froze changes: no policy value, prompt, packet,
receipt or snapshot byte, so every in-flight audit picks the fixes up as it is.

- **Staging masks home paths and e-mail addresses in records nobody can reword (check 3d).** Check 4 refused any
  home path, and any address the byline and the article do not print, even inside a critic's output or a Phase-1
  evidence script, which are model output or hash-bound. "Reword it" had no remedy, so audits A06 and
  A07 could never stage. As check 3c does for deposit values, `stage_release.sh` now rewrites the STAGED copy
  of such a record (`check_deposit_values.record_file`): `<user-dir>` for the account segment, `[e-mail address]`
  for the address. Each file is listed in `RELEASE_PATH_NEUTRALISATION.txt`, with counts and never a value. The
  audit's files are untouched, and the comment's own outputs still refuse. The step is
  `scripts/release_record_masking.py`, which reads check 4's own patterns and address rule: every post-delta
  receipt binds `check_release_privacy.py`'s bytes, so that file is unchanged and no receipt goes stale.
- **Logged text can no longer block the coordinator or publication.** `run.json` is projected from the append-only
  journal; the coordinator scans all of it before any call, and publication scans it again. An e-mail address, a
  home path or a participant code typed into a note, failure detail, brief correction or delivery recipient
  therefore blocked the audit for good. The append commands now refuse such text, using the coordinator's own
  check and the release gate's address rule (example domains pass). A new `text_redaction` event
  (`seq`, `field`, `reason`) masks one free-text field of an earlier event as `[redacted]` in the projection, while
  the journal stays as written. The coordinator's refusal names it, and SKILL.md's delivery template is
  `recipient='I4R editorial queue'`.
- **A linear e-mail detector (`scripts/email_scan.py`).** The unbounded pattern backtracked quadratically on long
  runs of address characters: 7 s for 80k characters, 61 s for one 289 KB knitted HTML file. The Phase-1 privacy
  scan shares a 60 s pre-dispatch deadline, so audit A08 could never dispatch. The scan now runs the
  same pattern only in a window of 320 characters around each `@`, giving the same matches for any local part up to
  that length (RFC 5321 allows 64). Used by `phase1_runtime.privacy_scan`, check 3d and the journal's text check.
  `check_release_privacy.py` keeps its own scan (it is receipt-bound), and staging ships no deposit file.
- **Event types.** All three `lifecycle_journal.py append-*` commands refuse a type outside the lifecycle-event
  schema; they accepted any string, so a typo became permanent. `environment`, the pre-2.7.4 hand-logged type, is
  added to `run.schema.json`'s event types (the projection ignores it, and appending one is still refused). The
  audit A09, whose journal holds one, can therefore finalize.
- **`new_audit.sh` picks the right package.** Hidden folders (Claude Code writes `.claude/` into the folder it is
  started in) and `__MACOSX` are never the package. Package folders beside loose files are refused, with every
  `--package` choice named. An empty package is refused before the critic-access call. CHEATSHEET: start Claude Code
  at the tree root.
- **The coordinator's attempt budget counts every call that ran.** A call stopped at `--timeout-min`, or one that
  left no result envelope but wrote into its output workspace, was booked as never started and consumed no attempt,
  so reruns were unbounded. As in Phase 1 since 2.7.2, it is now charged. An envelope reporting a pre-inference
  rejection is still believed. `I4R_COORDINATOR_TRANSIENT_RETRIES` is capped at 20.

## 2.7.5 — 2026-10-07

Every audit started from now on runs its critique on GPT-6.1 Sol and its Claude roles on Sonnet 5.5, and nothing
moves it off them. The policy has frozen both for every new audit since 2.7.0, and every audit initialised since
then has Sonnet 5.5. But from 2026-09-30 to 2026-10-07 at least twelve audits frozen on Sol (thirteen snapshots
under `runs/`, one a failed copy; among them audits A10, A11, A12,
A13 and A14) ran their critique on `gpt-5.6-terra`. Each was re-pinned by
`repin_critic_route.py --critic-model gpt-5.6-terra`, several within seconds of init, because the host's Codex CLI
was signed in with a ChatGPT account that refuses Sol (HTTP 400 "The 'gpt-6.1-sol' model is not
supported when using Codex with a ChatGPT account"). INSTALL.md documented exactly that re-pin as the remedy.

- **The route is locked in the audit's own snapshot.** The policy gains `critique.critic_route_lock`
  (`{"model": "gpt-6.1-sol", "since": "2.7.5"}`), so every new audit freezes it, and every version of
  `repin_critic_route.py` keeps it (it deep-copies the policy). `run_critique.sh` reads it right after the route
  and refuses, before anything is built, journaled or paid for, any round whose critics, adjudicator or critique
  probe differ from it, naming `policy.critic_route_history` and the restore command. So a re-pin by an older copy
  of the tool (2.7.4 and earlier accepted any `--critic-model`) cannot move a new audit off Sol. The drift checker
  pins the leaf. And `record_invocation.validate_record` never validates a completed critic or adjudicator review
  on any other model, so an audit an older copy of the driver ran off the lock cannot be integrated, finalized
  (`summarize_invocations.py --check`) or published, whichever copy moved it. Audits initialised earlier froze no
  lock and keep their route.
- **`repin_critic_route.py` moves an audit only TO the shipped route.** `--critic-model` and `--adjudicator-model`
  are refused unless they name the shipped values, dry run included, and nothing is written; a frozen lock is
  honoured too. An audit pinned to an older route (`gpt-6-sol`, `gpt-5.6-terra`) with no completed review can
  still be moved up to Sol, and a request that changes nothing still answers "already pinned". A module-level
  `SHIPPED_ROUTE_ONLY` (default on) lets the tests of the re-pin mechanics use arbitrary targets.
- **`scripts/check_critic_access.py` (new) and `new_audit.sh`: the account is checked before an audit starts.**
  Whether a host can use Sol depends on the account, not on the login type (this release's host is a ChatGPT login
  that can; the programme's is one that cannot), so the only reliable test is a call. One minimal call in the
  critique driver's exact command shape (the policy's critic model and effort, the driver's flags, a one-field
  output schema, no audit content); `tests/test_275_critic_route_lock.py` builds the driver's argv from
  `test_run_critique_runtime.command_provider_exec` itself and requires the same shape. A pass is cached in
  `${I4R_STATE_DIR:-~/.i4r}/critic_access.json` for 24 hours, keyed on the CLI path and version, the scrubbed
  `codex login status` line (no key, no e-mail), a sha256 of `${CODEX_HOME:-~/.codex}/auth.json` (every login
  rewrites it; only the digest is stored, so two ChatGPT accounts, whose status lines are identical, are told
  apart) and the route; a refusal or failure is never reused as a pass, and a malformed stamp just means "call
  again". Exit 0 usable; 3 the account cannot use it (by the driver's own classifier, plus any other refusal the
  provider returns, such as an API key without access to the model or an unverified organisation; on a CLI older
  than 0.159.0 the message says to update the ChatGPT app first); 4 network, rate limit, server or timeout: run
  again; 5 the CLI rejected the call itself with no answer from the provider (its error line, not its usage line):
  update it; 2 no Codex CLI. `new_audit.sh` runs it
  after its own input checks (an ambiguous folder costs no call) and before it reserves or copies anything; on a
  refusal it prints the command to run again, and changing the account is left to the user. A host that cannot use
  Sol no longer pays for Phase 1 and then stops at the round-1 critique probe. One live call on this release's host
  (codex-cli 0.160.1, a ChatGPT login) passed.
- **`init_run.sh`** refuses a leftover snapshot (an init that did not finish) frozen on another critic route (one
  of the Terra-re-pinned audits' snapshots, say) or before the lock, since such an audit would not be locked and
  removing a leftover costs nothing, and prints the critic route it froze and whether it is locked.
- **Residual.** A teammate on a 2.7.4 copy that both re-pins and runs a round with its own driver still spends those
  calls on the other model (the audit then cannot be finalized), and a 2.7.4 init forced past the canonical-version
  gate with `I4R_ALLOW_SKILL_VERSION_MISMATCH=1` gives an audit with no lock. Run every script from the canonical
  copy (INSTALL.md, *Upgrading in-flight audits to 2.7.5*, Mixed teams).
- **Claude side.** New audits already freeze Sonnet 5.5 at xhigh (2.7.3), there is no per-role Claude re-pin,
  `init_run.sh --force` re-freezes the shipped policy, and `init_run.sh` refuses a skill copy whose `VERSION`
  differs from the tree's canonical one.
- **One-time cost.** `run_critique.sh` changed, which moves the adjudicator target-snapshot address (it binds the
  driver's bytes): a round relaunched after the upgrade that reaches adjudication makes one more adjudicator call.
  Nothing else about an in-flight audit changes.
- **SKILL.md word cap 4,500 -> 4,750** (`SKILL_WORD_CAP` in `config/check_policy_drift.py`, at the operators'
  request). The file sat at exactly 4,500 words, so the critic-access step had no room without deleting another
  rule. Detail still belongs in INSTALL.md.
- **Docs.** SKILL.md, INSTALL.md (*Upgrading in-flight audits to 2.7.5*, the 2.7.0 notes, the dependency table,
  the live canary, the direct `setup_workspace.sh` path, the re-pin section, `I4R_STATE_DIR`), CHEATSHEET.html
  and .pdf (regenerated with headless Chrome's `--print-to-pdf`) and scripts/README.md no longer offer a re-pin to
  another model; they give the account remedy and leave the account change to the user.
- **Tests.** `tests/test_275_critic_route_lock.py` (25 tests, no provider call: `tests/fake_codex.sh` answers the
  check, refuses a model named in `FAKE_UNSUPPORTED_MODEL` with the real account's words, and reads
  `FAKE_CODEX_LOGIN`, `FAKE_CODEX_VERSION` and `FAKE_ACCESS_SCENARIO`). `tests/test_run_critique.sh` step 11 now
  proves, end to end, that the Terra re-pin is refused, that the driver refuses a route an older tool moved off
  the lock with no provider call, that the restore command works, and that an older-route audit moved up to Sol
  finishes on it with its pre-re-pin probe still valid through the route history, while one whose reviews
  completed on the older route is not moved at all. test_275 also proves the ledger rule (with an unlocked negative
  control), the leftover refusal and two planted drifts of the lock. The 2.7.0 test that walked
  the Terra re-pin asserts the refusal; the re-pin mechanics tests switch `SHIPPED_ROUTE_ONLY` off explicitly;
  test_271's leftover fixtures carry the shipped lock;
  the older-policy reconstructions drop the new leaf; and every test that runs `new_audit.sh` answers the check
  with the fake and keeps its cache and `CODEX_HOME` in a scratch folder (three `test_264` intake tests had been
  skipping silently without it).

## 2.7.4 — 2026-09-30

Fixes for what audit A15, run on Sonnet 5.5 and GPT-6.1 Sol, ran into, and the environment record a teammate's session rebuilt. Identity-neutral, so for every audit: run.json keeps the measured environment; Sonnet 5.5 spawns get the CLI's full 128,000-token output cap and an overflow is retried once; Phase-1 shells keep their working directory, ending the E2BIG failures on Claude CLI 2.1.206; the draft registry finds a role the output repair completed; snippets can be run in the release notebook's environment; the driver records the report a round reviewed; and back-matter headings carry their own page break. Frozen at init, so for new audits only: a 5,400 s critique ceiling, and later-round specialists that re-review only substantive changes while completeness still reviews every changed finding. Every audit initialised earlier keeps its packets, prompts, receipts and ceilings. Two one-time costs: a relaunched round that reaches adjudication makes one more adjudicator call (the driver's bytes changed), and every document built on 2.7.3 or earlier must be rebuilt with `build_docx.sh` before finalize, staging or re-publication (the builder's bytes changed; see the page-break bullet).

- **A deposit file whose name reads as inline R code no longer breaks the notebook (fix of 2026-10-05, no
  version bump).** Audit A16's deposit held a file whose name opens with `r ` (called `r code notes.docx` below).
  - *The defect.* `make_verification_notebook.sh` wrote every deposit file name in a one-backtick code span in the
    notebook's deposit inventory, and knitr reads a span that opens with `r ` or `r#` as inline R code. So
    `run_snippets.sh` and `run_verification.sh` stopped at the inventory line, with "Failed to parse the inline R
    code" for this name and "object 'code.docx' not found" for `r code.docx`.
  - *Why a hand patch failed.* Escaping the generated `verification.Rmd` by hand cannot pass:
    `generate_finding_artifacts.py`'s verify (run by `stage_release.sh`) regenerates the notebook and requires byte
    identity.
  - *The fix.* Such a name is now padded in a double-backtick span (`` `` r code notes.docx `` ``), which renders the
    same text and is not inline code. Every other name keeps its one-backtick span, so every existing audit's
    notebook regenerates byte for byte (the 2.7.2 pin in `tests/test_274_run_snippets.py` still holds, and audit
    A17's notebook regenerates unchanged).
  - *The test.* `tests/test_274_inline_r_filename.py` checks the padded cell, the unchanged cells and a passing
    harness knit. On the previous generator its knit test fails with exit 2.
  - *Provenance.* This changes `scripts/make_verification_notebook.sh`, one of the files in `gate_files_digest`, so
    an audit initialised from now on stamps a different `gate_set_version` from the audits initialised on 2.7.4
    before it. No release gate compares the stamp.
- **Two driver and publish fixes from audit A18 (fix of 2026-10-05, no version bump).** The audit
  worked around both: a rerun of round 2, then a journaled reopen and a fifth round.
  - *A body page that opens with "appendix" is no longer read as back matter.*
    - The defect: `publish_completed.sh` recounts the publication's PDF with
      `document_release.recount_candidate()`. That treated any page whose first rendered line began with the word
      "appendix" or "references", or the words "data and code availability", as the start of the back matter.
    - What happened: body page 8 opened mid-sentence with "appendix output and pkg/data/sessions.xlsx". The recount put
      the boundary on page 8 against the manifest's page 10 and refused the publish ("candidate PDF
      body/back-matter boundary differs from the document manifest"). `build_docx.sh`, `stage_release.sh` and
      `finalize_run.sh` had all accepted the document, because they find the boundary from the report's own heading.
    - The fix: `pdf_line_starts_back_matter()` now requires the whole line to be the heading: "Data and Code
      Availability", "References", or "Appendix" with an optional label and title ("Appendix A. Summary of
      findings").
    - Checked on real documents: the refused PDF now recounts at 9 body pages, and all 50 published comments under
      `comments/` recount unchanged.
  - *Critique routes list their roles in canonical order.*
    - The defect: the route command kept a `CRITICS` override in the order it was written. The deterministic claim
      merge orders roles as `ROLES` does (stats, data, fairness, completeness), and the round projection compares
      the two lists.
    - What happened: round 2 run with `CRITICS="completeness fairness"` dispatched both critics, which completed,
      and then failed the projection with "claim merge identity or role quorum differs". The rerun in canonical
      order changed the round's input hash, so both critics ran again.
    - The fix: `command_route` in `scripts/test_run_critique_runtime.py` now writes the routed roles in `ROLES`
      order for every route.
  - *The tests.*
    - `tests/test_274_back_matter_heading_line.py` checks heading and body lines, and a stubbed recount of a page
      that opens with "appendix" mid-sentence. It also has a negative control: a real heading before the manifest's
      boundary still refuses.
    - `tests/test_274_critic_route_order.py` checks the routed order for several overrides and for round 1.
    - Both fail on the previous code.
  - *Provenance and one-time costs.*
    - Both files are in `gate_files_digest`, and neither is in `run_post_delta_gates.py`'s `CODE_PATHS`, so
      existing post-delta receipts stay valid.
    - Every document manifest binds `document_release.py`. So every document built before this fix must be rebuilt
      with `build_docx.sh` before verify, staging, finalize or re-publication (the `expected_code` refusal names
      the remedy).
    - A round relaunched after this fix that reaches adjudication makes one more adjudicator call, because the
      routing helper's bytes changed.
- **The environment journal: `run.json` keeps the software environment measured at init.**
  `capture_environment.sh` wrote `environment` straight into `run.json`, but on a metadata-2.1 audit `run.json`
  is a projection of `run.events.jsonl`, so the first append after init re-projected it without the block. On
  2026-09-30 a fresh 2.7.1 audit lost it at `log_event.sh phase_start phase=intake`; 2.7.2 and 2.7.3 lose it the
  same way. 31 of the 32 audits under `runs/` have none, and the one that has it was edited by a teammate. The
  capture now journals the environment, and the projection carries it. Every existing audit's journal projects and
  validates byte for byte as on 2.7.3 (its documents need the one rebuild described under the page-break bullet). A teammate's session built this change on 2.7.1 (labelled 2.7.2, then
  2.7.3); it is merged here three-way, file by file, and those builds are superseded. The merge adds one guard
  the original lacked: nothing the coordinator's provider-safety scan refuses is journaled (below).
- **One event, one writer.** `lifecycle_journal.record_environment` appends one
  `environment_captured` event, payload `{"environment": {...}}`, with source
  `capture_environment.sh`, and re-projects. `_apply_event` sets `run.environment` from it, so the
  latest capture is the record and an earlier one stays in the journal.
  - *Tool-only.* The capture is a live measurement, so the event joins `TOOL_ONLY_EVENTS`.
    `log_event.sh` (single and `--batch`) and the `lifecycle_journal.py append-*` commands refuse
    it, and they also refuse `environment`, the type three pre-2.7.4 journals hold from a 2.2.x copy.
    A hand-logged `metadata_correction` may not set `environment`.
  - *Validated where every writer passes.* `_append_locked` refuses an `environment_captured`
    payload that is not exactly `{"environment": {...}}`, has no zoned `captured_at`, or breaks
    `run.schema.json`'s own `environment` definition (read from the schema, not restated). Nothing
    reaches the append-only journal that finalize's schema check would then refuse.
  - *Refused where it would do harm.* `record_environment` refuses a workspace with no journal and a
    finalized audit that was not reopened: `finalization_receipt.py check` accepts only
    `POST_FINALIZATION_EVENTS` after the finalize it binds.
  - *Legacy.* A run.json that is not metadata 2.1 (no canonical journal) is still written in place,
    by the rule `log_event.sh` uses. A 2.1 run.json with no journal is refused (exit 1).
  - *Schemas.* `environment_captured` joins the event-type enums of `run.schema.json` and
    `schemas/v2/lifecycle-event.schema.json`. `environment` stays out of both, so the teammate's
    audit under `runs/` fails finalize's schema check on its seq-2 event as it did on 2.7.3.
- **The operator never blocks publication.** `run.json` is filed with every published comment, and
  both publication gates refuse any e-mail or IP address in it (`check_release_privacy.py`, and
  check 3c in `check_deposit_values.py`). The four owner identities in `reservations/`
  (`<user>@<short host>`) pass both. An `I4R_OWNER` that either gate's own pattern reads as an
  address is recorded as `<user>@<host>`, with a note, because the journal copy inside `run.json`
  could never be removed.
- **Nothing the coordinator would refuse is journaled.** The coordinator reads `run.json` as `context/run.json`
  and refuses the whole file when any string in it fails its provider-safety scan (a private path, or a
  participant-code-shaped token). Measured on a copy: a capture with the operator `lab@MBP16` (the account on a
  host named MBP16) made `run_coordinator.validate_provider_visible_string` refuse `run.json`, and a second
  capture under a safe `I4R_OWNER` did not help, because `run.json`'s events keep the first capture's payload.
  `capture_environment.sh` now runs that same function, with no deposit exemption, on the operator and on every
  tool reading: an `I4R_OWNER` it refuses gives way to `<user>@<host>`, an account it refuses leaves the operator
  null, and a tool reading it refuses is recorded as null, each with a note on stderr. The four owner identities
  in use pass it, so their captures are unchanged. Whether the operator should reach the provider at all is not
  decided here; on a new audit it does, inside `context/run.json`.
- **Check 3c treats the environment as provenance.** `environment` joins `RUN_JSON_PROVENANCE_KEYS`. Its strings
  are tool versions written by the skill, which nobody can reword, so they are not matched against deposit values.
  They are still read by the e-mail and IP detectors. The change can only drop a match, never add one. Ten legacy
  `run.json` files carry a block (one under `runs/`, nine under `old_runs/`; five of the nine reached `delivery`
  and one has a finalization receipt). Check 3c re-run on them can now pass where an environment string matched a
  deposit value, and nothing that passed fails.
- **finalize tells the truth.** The WARN "run.json carries no environment record — capture_environment.sh
  was never run" sat in the heredoc under `write_validator`, after the function's `return`. It was
  never printed. The new `environment_note` runs right after `run.json` is validated (step 3). It
  prints `ok: run.json carries the environment captured at <time>, journal seq <n>`, or a WARN
  naming the actual cause: no `environment_captured` event (the pre-2.7.4 loss), a hand-logged
  `environment` event the projection does not read, or a legacy `run.json` never captured. On a
  metadata-2.1 audit the journal decides, not `run.json`'s own key: a block written into `run.json`
  outside the journal (the teammate's audit has one) is named as dropped by the re-projection, and a
  `run.json` that differs from the latest capture is named too. The
  remedy is the capture on an open audit; on a finalized one the WARN says it is refused. It stays
  a warning, since those audits lost the record through no act of their operator. The WARN text is
  finalize output only and is bound into no artifact.
- **Tests.** `tests/test_274_environment_journal.py` has 19 tests, each running real code with a
  negative control, and the finalize self-test gains two cases: the e2e audit's run.json equals its
  seq-2 capture at finalize, and the note names five kinds of missing or unprojected record. The module covers: real
  `init_run.sh`, then `log_event.sh`, then `project`/`recover`, where a pre-2.7.4 direct write is
  still lost; the latest capture wins; the address-shaped owner, with `check_release_privacy`
  refusing it as given; an owner, an account and a tool reading the coordinator's scan refuses, with the
  coordinator refusing the owner as given; the finalized and reopened cases; the legacy and orphaned run.json; every
  hand path refused with the journal byte-identical; six malformed payloads through both API
  paths; the hand-logged legacy type not projected; and check 3c with the same deposit name refused
  in `notes` and not in `environment`. On the 2.7.3 tree the module fails 8 and errors 13 (15 of
  the 19 tests); the four that pass there are the direct-write negative control, the legacy in-place write, the
  projected-run schema check and the release-privacy pass. Each of fourteen single-point mutants of the fix
  fails at least one test.
- **Mixed teams.** An audit initialised on 2.7.4 and continued on 2.7.3 or earlier appends and projects without
  error, but its `run.json` loses `environment` until a 2.7.4 tool projects it again, and 2.7.3's finalize stops
  at schema validation (`/events/1/type: 'environment_captured' is not one of [...]`). The journal is
  append-only, so finalize such an audit on 2.7.4 or later (INSTALL.md, 2.7.4).
- **Unchanged.** The post-delta receipt's code files, `run_critique.sh`,
  `test_run_critique_runtime.py`, every prompt, the policy and its snapshot, and every hashed
  identity. `check_deposit_values.py` and both schema files are part of no audit's bound identity.
- **The draft registry finds a role the focused output repair completed.** `build_draft_registry.py` searched
  only `phase1/roles/<role>/<input>/attempt_*/status.json`, so a role whose sample failed only structurally and was
  completed by `run_phase1.try_focused_repair` (the quorum then binds the repair's invocation, under `repair_<uuid>/`)
  stopped the build with "FATAL: no bound result.json". The first audit to complete a role that way
  (A15, `auditor_code_methods`) hit it and was finished with a patched copy. `bound_result` now
  searches every attempt directory (`<input>/*/status.json`, as run_phase1's own recovery does), refuses when more
  than one directory carries the quorum's invocation, and, when Phase 1 recorded the promoted result's digest (the
  completion receipt's `result_sha256`, or a pre-receipt quorum entry's), refuses a `result.json` that no longer
  hashes to it. A failed sample is never a fallback. On every audit where 2.7.3 builds the registry, the output is
  byte-identical. Test: `tests/test_274_repair_bound_registry.py` (a Phase-1 tree with one role completed by repair;
  it fails on 2.7.3).
- **Sonnet 5.5 spawns get the CLI's full per-response output cap, and an overflow is retried once.** Claude CLI
  2.1.206 sends every request with `max_tokens` at its catalog default for the model (64,000; it maps
  claude-sonnet-5-5 to the claude-sonnet-5 row by prefix) unless `CLAUDE_CODE_MAX_OUTPUT_TOKENS` names another value,
  which it clamps to the row's upper limit, 128,000. A response that reaches the cap is resumed in context three
  times and then the call fails ("Claude's response exceeded the 64000 output token maximum"). The first Sonnet
  5.5/max coordinator call failed that way after 33 minutes and USD 7.11, and the runner classed it `permanent`, so
  nothing retried it. A live check on 2026-09-30 (CLI 2.1.206) confirmed that the CLI honours the variable (the
  request body carried `max_tokens` 128000 with it and 64000 without it) and that the API accepts 128000 for
  claude-sonnet-5-5. The envelope's `modelUsage.<id>.maxOutputTokens` reports the catalog default whatever the cap,
  so it is no evidence either way.
  - `claude_routes.output_cap(route)` is 128,000 for every live Sonnet 5.5 route (max and xhigh) and None otherwise;
    `spawn_env(env, *, route=None)` adds `CLAUDE_CODE_MAX_OUTPUT_TOKENS` for such a route, overriding an operator
    export. Every provider spawn passes its route: `run_coordinator.run_provider` and `run_phase1.run_process` (the
    probe, the auditors, a repair and the verifier). `claude --version` and Sonnet 5 routes keep the operator's
    environment as in 2.7.3.
  - A new failure class, `output_limit` (`run_coordinator.classify_error`, also used by Phase 1), is retryable once
    per coordinator invocation and once per Phase-1 role (`OUTPUT_LIMIT_RETRIES`), within the existing attempt
    budgets. The class joins the `error_class` enums of `run.schema.json`, `schemas/v2/invocation-record.schema.json`
    and `validate_v2`; no provider-facing schema changes.
  - Identity: the environment enters no packet, prompt, key, receipt or snapshot, so existing audits keep every hash;
    a Sonnet 5.5 audit initialised on 2.7.0 to 2.7.3 now also runs with the cap. Test:
    `tests/test_274_output_cap.py` (fake CLIs that record the cap and emit the CLI's overflow message).
- **Phase-1 shells keep their working directory (E2BIG).** On CLI 2.1.206 the macOS sandbox profile travels as one
  argv string on every Bash call, at 0.77 to 0.91 of ARG_MAX (1,048,576 bytes) for the audit roots under `runs/`
  (167 to 303 characters): for every denied path it writes an unlink and a create rule for the path and each of its
  ancestors, each with a 161-byte tag. When the CLI rebuilds that profile on a settings change while its tracked
  working directory is away from the start directory (after a `cd` into `raw/pkg`), it adds that directory's
  `.claude/`, `.mcp.json` and bare-repository deny paths for every level up to the `--add-dir` root, 34 deny paths
  become 52, the profile passes ARG_MAX, and every later command, even `pwd`, fails with E2BIG. Two of the five
  auditors of audit A15 lost their shell that way; one wrote its report from reading the code alone.
  `run_phase1.run_process` now spawns every Phase-1 call (the probe, the auditors, a repair and the verifier) with
  `CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1` (`PHASE1_SHELL_ENV`), so the CLI returns its tracked directory to the
  start directory after every call; `cd DIR && cmd` still works within its call. The coordinator (no Bash tool) is
  unchanged. No key, hash, prompt, settings file or receipt covers the environment, so this applies to every audit.
  The 2.6.6 verifier deny-list trim is not reused: its premise (no other writer during the call) does not hold during
  the Phase-1 fan-out, and most of the profile is the CLI's own paths under the deep working directory. Newer CLIs
  (2.1.270 and later, on teammates' machines) build much smaller profiles. Test: `tests/test_274_shell_cwd_pin.py`.
- **New audits freeze a 5,400 s critique ceiling.** Every Codex call the critique driver makes (the model probe,
  the critics, a focused repair and the targeted adjudicator) was bounded by `I4R_PROVIDER_TIMEOUT_SECONDS`, default
  2,700 s, a value set for GPT-5.6 Terra and recorded nowhere. GPT-6.1 Sol generates at about a third of Terra's
  speed (19 to 26 against about 51 output tokens per second on 2026-09-30); its longest completed call took 2,060 s,
  and one call hit 2,700 s. That call was a stalled stream, as both Terra timeouts on record were, and a retried
  stall has always succeeded, so a longer ceiling also means waiting longer on a stall. 5,400 s is 2.6 times Sol's
  longest completed call and equals Phase 1's: cutting a call that is still working costs more than waiting on one
  that has stalled, since a long call retried on the same input tends to be cut again.
  - `scripts/critique_timeouts.py` (new, stdlib only): `LEGACY_CRITIQUE_TIMEOUT` 2700, `SHIPPED_CRITIQUE_TIMEOUT`
    5400, `LIVE_CRITIQUE_TIMEOUTS` both. `frozen_ceiling(policy)` gives (2700, unfrozen) for a policy with no
    `critique.provider_timeout_seconds` (every audit initialised before 2.7.4) and refuses a leaf that is not live;
    `effective(policy, override)` honours the environment knob exactly as before for an unfrozen audit and lets it
    only lower a frozen ceiling.
  - `config/pipeline-policy-v2.json` sets `critique.provider_timeout_seconds` 5400. `run_critique.sh` resolves the
    ceiling from the audit's own snapshot before any round folder, journal event or call, refuses an override above a
    frozen ceiling with `fail_early`, and prints `[limit] provider call ceiling N s` for a frozen audit only, so an
    older audit's log is unchanged. `init_run.sh` prints `critique timeout: 5400 seconds per critic or adjudicator
    call`, refuses a leftover snapshot whose leaf no driver admits, and warns when a reused leftover frozen by 2.7.3 or
    earlier has neither critique leaf. `wait_for_round.sh` waits, by default, twice the audit's effective ceiling
    (the environment knob included): 90 minutes for an older audit, as before, and 180 for a new one or for an older
    one that exports 5400. A malformed knob (not a positive decimal integer, e.g. `05400`, which bash read as octal)
    is now refused before the round instead of misbehaving mid-round.
  - The drift check pins the leaf to a literal, joins it to the module (which must keep 2,700 s live), and gains a
    nineteenth planted control, the ceiling restored to 2700.
  - What existing audits keep: all 268 snapshots under `runs/` resolve to 2,700 s, and to 5,401 s with the knob at
    5401, as before; their snapshot bytes, round plans and critic caches are unchanged. The driver's own bytes change,
    which moves the adjudicator target-snapshot address that binds `sha256(run_critique.sh)`: a round relaunched
    after the upgrade that reaches adjudication makes one more adjudicator call, the cost every driver change has
    carried since 2.6.9. Test: `tests/test_274_critique_timeout.py`.
- **New audits: later-round specialists re-review only substantive changes; completeness still reviews every changed
  finding.** After an integration nearly every registry row differs from the round before, because binding a round's
  confirmations rewrites `verifications` and approval rewrites `settled`, so every later round sent every finding to
  every routed critic (audit A15's round 3 listed every finding although only two sections
  changed). Measured on rounds 3 and later of 27 audits (53 rounds), such bookkeeping-only rows were 82% of the ids;
  in round 2 the round-1 approvals (status, report section and snippet supplied) stay substantive, so round 2 is
  routed as before. They were also
  where 19 of the 36 non-CONFIRMED later-round verdicts came from, and 9 of those led to a rewording. So the
  programme lead chose to narrow the specialists only (2026-09-30).
  - `scripts/claim_change_rules.py` (new, stdlib only; no post-delta CODE_PATHS file imports it). The legacy rule is
    implied by an absent leaf, read only from the audit's own snapshot, never from the packaged policy. A substantive
    change is a finding added or removed, a registry difference other than bookkeeping, or a finding whose report
    section's text changed. Bookkeeping means `settled.*`, `coordinator_approved` turning true (a withdrawal is
    substantive), and confirmations bound or rebound with no confirmation lost and no new dispute; a status change
    and fields supplied at approval (report section, verification snippet) are substantive. Sections are compared at the top level on both sides, so
    the comparison errs toward a specialist look.
  - `config/pipeline-policy-v2.json` freezes `critique.later_rounds.routing_manifest.claim_change_rule`
    `substantive_specialists_1`.
  - `make_diff_packet.py`: under that rule the packet also carries `claim_change_rule`, `specialist_claim_ids` and
    `bookkeeping_only_claim_ids`, and chooses its specialist(s) (`role_scores`, `recommended_roles`,
    `critical_domains`) from the substantive rows, or from every changed row when there is none, so a bookkeeping
    majority in one domain does not send the substantive finding to another domain's specialist.
    `changed_claim_ids` and everything else stay exactly as the legacy rule computes them, so `verify_diff_packet.py`, `run_post_delta_gates.py`, finalize's review-scope union
    and the round-target rules are unchanged (no CODE_PATHS file was edited), and the packet keeps
    `packet_version` `i4r-diff-packet-2`.
  - The critique runtime's router gives completeness every changed finding and gives each specialist only the
    substantive ones. A specialist left with none is not dispatched, unless an explicit `CRITICS` override named it.
    When no specialist has any substantive change (only bookkeeping, or prose outside every finding's section), the
    round keeps the first routed specialist with the unnarrowed scope: finalize requires completeness plus one
    specialist in every round after round 1. The claim-assignment mode stays `domain_routed_delta`, which every
    consumer pins. A manifest with an unknown rule, or a specialist list outside the changed claims, is refused.
  - The targeted-adjudication triggers treat a bookkeeping-only finding as they treat an unchanged one: a
    high-impact finding that only completeness reviewed raises no `high_impact_evidence_gap` when every assessment
    confirms it (a dispute still does). Without this, such a finding would have ended every later round
    `deferred_evidence_gaps`.
  - The drift check pins the leaf to a literal, joins it to the module (which must keep the legacy rule live), and
    gains a twentieth planted control, the leaf removed. `init_run.sh` prints the rule and refuses a leftover
    snapshot holding an unknown one.
  - What existing audits keep: no leaf, so their packets are byte-identical. Audit A15's round-3 packet,
    rebuilt from its preserved before-files by 2.7.3 and by 2.7.4, equals the stored one byte for byte. The runtime
    routes their manifests as before. The runtime's bytes change, which moves the adjudicator target-snapshot address
    as the driver change above does. Test: `tests/test_274_claim_change_rule.py`.
- **Snippets can be run in the release notebook's own environment before release (`scripts/run_snippets.sh`).** The
  17 snippets of audit A15 passed in an operator harness that attached lavaan and psych, but five claims failed
  in `run_verification.sh` ("could not find function sem") because the audit's `_prelude.R` attached neither, which
  cost a release cycle. `run_snippets.sh <audit> [--decisions FILE] [--keep] [CLAIM ...]` runs the chosen snippets (or
  every projected one) through exactly the notebook's setup chunk, `_prelude.R`, `expect()`/`check()` ledger and
  per-claim `tryCatch` wrapper, in one knitr session, and binds nothing: no `verification.Rmd`, status, summary or
  manifest is written. It uses a new `--harness SPEC.json` mode of `make_verification_notebook.sh`, which is additive:
  the normal notebook's bytes are unchanged. `scripts/snippet_lint.R` adds one static warning class, printed by
  `make_verification_notebook.sh`: an unqualified call to a function an installed package documents, with nothing
  before it attaching that package. It stays quiet whenever it cannot be sure (non-standard evaluation, dynamic
  definitions). SKILL.md and INSTALL.md tell operators to run snippets with it while writing them. Test:
  `tests/test_274_run_snippets.py` (a prelude missing a library the snippet needs fails as the release would, and
  passes once the prelude attaches it).
- **Three smaller fixes.**
  - *Two integration times, explained.* `round_result` refuses an `integrated_at` other than the journaled one; its
    message now says that `run.json`'s value is the time `run_critique.sh` closed the round (journaled even for a
    final round that is never applied), while `integrate_round.py apply`'s own time is
    `critique/round_N/INTEGRATION_SUMMARY.json`, which finalize reads. No value moves.
  - *The driver records which report a round reviewed.* `run_critique.sh` passes the snapshot's report SHA-256 on its
    `round_integrated` event, so `run.json` `rounds[].report_version_reviewed` is filled without an operator argument
    (it stayed null before). The projection already copied the key; existing journals project byte for byte.
  - *No blank body page.* `build_docx.sh` started each back-matter section (Data and Code Availability, References,
    appendices) with a separate paragraph holding a page-break run. When the body exactly filled a page, that
    paragraph spilled onto the next page and left it blank, a counted, empty body page (the draft of audit A15 showed
    "10/10 body pages" with page 10 empty). The heading itself now carries `pageBreakBefore`: the converter input
    holds a marker paragraph that the post-pandoc step replaces, failing closed if a marker is not followed by a
    Heading1 paragraph. The counting rules, the manifest schema and the page cap are unchanged.

## 2.7.3 — 2026-09-30

New audits run every Claude role one effort degree lower: the coordinator, the five Phase-1 auditors, their
capability probe and the post-fan-out verifier are spawned with `--effort xhigh` instead of `--effort max` (the
Claude CLI's levels are low, medium, high, xhigh and max; CLI 2.1.206 accepts xhigh). The model and its spawn
(`--model claude-sonnet-5-5`) are unchanged, and so is the GPT side: the critics and the critique model probe stay
at high, the targeted adjudicator at max. The effort is part of the Claude route an audit froze, so, as 2.7.0 did
for the model, every audit that already exists keeps its route for its whole life: Sonnet 5.5 at max (initialised
on 2.7.0 to 2.7.2) or Sonnet 5 at max (earlier). Such an audit behaves byte for byte as on 2.7.2. Only a new
audit's snapshot freezes xhigh.

- **Why.** On the first Sonnet 5.5 audits (2026-09-30), Sonnet 5.5 at max wrote 2.3 to 2.9 times the output
  tokens of Sonnet 5 at max, about 72.5% of it thinking, so each Claude stage cost about twice as much at the
  same list price ($2/$10 per MTok for both). One coordinator response overflowed the Claude CLI's 64k-token
  per-response output cap. Sonnet 5.5's effort levels are recalibrated, so the level chosen for Sonnet 5 does not
  carry over; xhigh is the step below max.
- **One more live route (`scripts/claude_routes.py`).** `SONNET_5_5_XHIGH` is `sonnet-5-5`, spawned as
  `--model claude-sonnet-5-5`, at `xhigh`. `LIVE_ROUTES` is `(SONNET_5, SONNET_5_5, SONNET_5_5_XHIGH)`, oldest first,
  and `SHIPPED` is `SONNET_5_5_XHIGH`; `SONNET_5_5` (max) stays live for the audits that froze it. The served-model
  checks key on `requested_model` and are unchanged. `LEGACY_EFFORT` is `"max"` and `effort_of(packet)` returns a
  packet's model-block effort, or `"max"` for a packet with no model block (pre-2.7.0) or no effort in it.
- **The shipped policy.** `config/pipeline-policy-v2.json` sets `reasoning_effort` `xhigh` for
  `models.coordinator`, the five `models.phase_1_auditors`, `models.post_fan_out_verifier` and
  `phase_1.capability_probe`. Nothing else changes.
- **The prompts name the route's own effort.** "Use {model} at max effort." was a literal in three prompts:
  - the auditors' (`run_phase1.build_role_prompt`) and the verifier's (`run_verifier.build_verifier_prompt`) now
    render the packet's effort (`claude_routes.effort_of`). A new audit's packets say `xhigh`; every packet frozen
    before 2.7.3 says `max` or has no model block, so its prompt, prompt hash, role input hash and verifier packet
    hash are the 2.7.2 bytes, and recorded verifier calls re-admit (`readmit_record` rebuilds the prompt from the
    frozen packet; `pre_265_prompt` is unchanged);
  - the coordinator's (`run_coordinator.build_prompt`) takes an `effort` argument, defaulting to `"max"`.
    `run_coordinator.py` passes the snapshot coordinator route's effort, and `recover_coordinator.py` rebuilds a
    quarantined attempt's prompt with the record's effort, which it has already bound to the snapshot's route. A
    max audit's coordinator prompt and input hash are unchanged.
- **Every Claude consumer admits the new route, and only the live ones.** `run_phase1.load_policy` (auditors and
  probe), the coordinator's route check and `run_verifier.py` take their allow-set from `claude_routes.LIVE_ROUTES`,
  so a snapshot on Sonnet 5.5 at high, or Sonnet 5 at xhigh, is still refused before any provider call, with a
  message that lists the three live routes. The existing mixed-route rules are unchanged: the five auditors must
  share one route and the verifier must be on the auditors' route, so xhigh auditors beside a max verifier are
  refused. The probe cache key includes the effort, so a new audit's probe never replays a max probe.
  The ledger, receipt and finalize checks judge every row against the audit's own snapshot, as before.
- **`--recover-stranded`.** A strand whose reservation carries no `reasoning_effort` (none this runner writes)
  was recorded as `max`. It now takes the effort of the snapshot's live Claude route for that role (the probe's
  is the first auditor's) and falls back to `max` only when the snapshot cannot say.
- **`init_run.sh`.** It prints the effort the snapshot froze (`auditor model: sonnet-5-5 (claude / anthropic),
  effort xhigh`) and checks, after the freeze, that the snapshot's coordinator effort is the one it planned on.
  A leftover snapshot (no `run.json`, no journal) whose Claude routes are not the shipped ones is refused, with
  its `rm`, as since 2.7.0; that now covers 2.7.2's `record_invocation.py --init-policy` output (Sonnet 5.5 at
  max). The refusal says which kind it is: a live route "may have been frozen by an older skill"; any other "No
  runner dispatches that route (the live routes: ...)". An initialised audit whose `run.json` is gone is told that
  `--force` would also move its effort. `--help` prints the new header line. `--self-test` gains item 15: the
  printed effort, and a max and a high leftover each refused untouched.
- **The drift check.** `SHIPPED_CLAUDE_ROUTE` is Sonnet 5.5 / xhigh (the label every Claude pin prints), and
  `check_claude_routes` also requires `LIVE_ROUTES` to keep the Sonnet 5.5 / max route every 2.7.0 to 2.7.2 audit
  froze. Three new named negative controls, eighteen in all: the Claude effort reverted to max in all eight
  entries (rejected by the coordinator, auditor, verifier and probe pins), the verifier alone left at max
  (rejected by the verifier pin and the verifier-equals-auditors join), and the capability probe alone left at max
  (rejected by the probe pin). An allow-set pin would accept each, since max is still a live route. The generated
  SKILL.md block prints `xhigh`.
- **The finalize self-test** builds its coordinator and auditor rows at the efforts of the snapshot it froze (they
  were a literal `max`), so it holds for the shipped policy.
- **Existing audits are byte-identical to 2.7.2.** For a Sonnet 5.5/max or Sonnet 5/max snapshot: the auditor,
  coordinator and verifier prompts; the role packets and role input hashes; the probe key, the portable probe key
  and the probe prompt; the coordinator's input hash; the dispatched `--effort max`; the ledger rows; and init's
  refusals. 2.7.2 refuses a 2.7.3 audit's snapshot before any provider call (it is not a route 2.7.2 packages). A
  forced re-init (`init_run.sh --force`) is the only way to move an audit to xhigh; it re-freezes the whole shipped
  policy and re-runs Phase 1.
- **Docs.** SKILL.md (the generated block, the probe line, the route line); INSTALL.md, including a new
  *Upgrading in-flight audits to 2.7.3*; `reference/phase1_runtime.md`, which no code reads or hashes;
  `scripts/README.md`. The hashed reference files are unchanged.
- **Tests.** `tests/test_273_sonnet_effort.py` builds every condition on disk and runs the real code with the fake
  CLIs:
  - a new audit initialised by the real `init_run.sh` from the packaged policy freezes xhigh in all eight Claude
    entries and prints it;
  - `run_phase1.py` dispatches `--effort xhigh` for the probe and all five auditors, each auditor prompt says "at
    xhigh effort", and the rows and probe cache say `xhigh`; the coordinator and the verifier likewise;
  - Sonnet 5.5/max and Sonnet 5/max snapshots render the auditor, coordinator and verifier prompts, the role
    packets, role input hashes and probe cache keys byte-identical to the 2.7.2 tree, imported by path in a fresh
    interpreter (`I4R_BASE_272`, else `../base`; skipped without one);
  - a Sonnet 5.5/high snapshot is refused before any provider call by `run_phase1.py`, `run_coordinator.py` and
    `run_verifier.py`, each listing the live routes, and by `init_run.sh` as a leftover; so are mixed routes;
  - the recovery prefers the snapshot's effort; the drift check's literal, its join and the three controls, each
    rejected by the pins it names, and a revert on disk rejected.

  Existing tests that asserted what a new audit ships were updated: the allow-set, the shipped-route label in the
  drift pins and init's leftover message, and the drift control count. Fixtures that built a "Sonnet 5.5" audit
  from the shipped policy now carry the route's own effort in their rows (`route_policy` also sets the probe's
  effort; `test_264_verifier_confirmation.terminal_record` takes it from the route). Tests of legacy behaviour are
  unchanged.
- **Gaps a mutation review found, closed.** Planted single-point defects that the first test set let through are now
  each killed by a test that needs no older tree: an existing max audit's coordinator dispatched at the shipped effort
  (the coordinator test now asserts `--effort max` in its argv), its probe, auditors and verifier likewise
  (`MaxRoutePhase1Dispatch`), init's post-freeze effort check removed (`PostFreezeEffortCheck`: a 2.7.2 snapshot
  frozen by another process inside the freeze), the `--force` hint dropped (`ForceHintNamesTheEffort`), and
  `--recover-stranded` ignoring the snapshot's effort (through `recover_stranded_attempts` itself). A compatibility
  sweep over copies of all 35 audit directories under `runs/` found every existing audit byte-identical: 917 ledger
  rows, 228 role packets and their prompts, 46 coordinator prompts, 36 probe caches and 28 verifier re-admissions,
  with the harness itself shown sensitive by two planted mutants.
- **Test defects 2.7.2 shipped, fixed.** Four tests failed on 2.7.2 itself whenever the older trees were named
  (`I4R_BASE_2610`, `I4R_BASE_270`): the 2.6.10 and 2.7.0 policy reconstructions in `test_270_critic_route.py`,
  `test_270_legacy_audits.py` and `test_271_probe_bound.py` lacked 2.7.2's legacy 2,700 s timeout, and
  `test_sandbox_path_contract.py`'s glob-root test caught `Phase1Error` from a module copy another suite had swapped
  out of `sys.modules` (it now takes the class through `run_coordinator.phase1_runtime`).
- **Not changed, on purpose.** `reference/adjudication_guide.md` still calls the verifier "one Sonnet/max call": it is
  a hashed coordinator reference, so rewording it would change every new audit's coordinator input for a sentence the
  coordinator does not act on. The ledger validator does not route-check a capability-probe row's effort (as before;
  the runner records it from the route). `CHEATSHEET.html` now suggests `/model` Sonnet 5.5 at xhigh for the
  interactive session that drives the pipeline; that session is not a policy role. `CHEATSHEET.pdf` is regenerated
  from it separately.

## 2.7.2 — 2026-09-30

A Phase-1 call stopped by its timeout is now a terminalized, charged attempt instead of a strand, and new
audits' per-call Phase-1 timeout is 5,400 s (90 minutes), frozen per audit like the probe bound. Every audit
that already exists keeps 2,700 s and gets the 2.7.1 contract byte for byte.

- **The defect.** `run_process` caught `RunnerError` around `parse_result`, but `parse_result` raises
  `run_coordinator.CoordinatorError` (a `ValueError`) when stdout holds no result envelope, which is what a CLI
  killed by the timeout prints. The exception escaped after `claude_stdout.json` and `claude_stderr.txt` were
  written and before any `status.json` update or terminal row, so the role reported "Claude CLI did not emit a
  JSON result object" and the attempt stayed at `provider_requested`. The strand guard then refused the next run
  until `--recover-stranded` closed it, and that tool books a 2.1 strand as a request the provider never accepted
  (`attempt: null`, `inference_start_status: not_started`). `role_preinference_request_count` refuses a role
  after two such rows, whatever `I4R_MAX_ATTEMPTS` says. So two timeouts stopped a role for good, although both
  calls had been accepted and had run for the full 45 minutes. It is what stopped audit
  A19: four of five Sonnet 5.5 auditors, killed at 2,700 s on two dispatches while
  writing `REPORT.md` and `result.json`, were refused on the third. The verifier shares `run_process` and had
  the same defect.
- **Terminalized by the runner.** `run_process` now catches `CoordinatorError` too, so a timed-out call reaches
  the ordinary failure path: status `timed_out`, `error_class` `timeout`, one terminal row, no strand. A timeout
  is still not retried inside the run (`retryable` admits transient and transport only); re-running
  `run_phase1.py` spends the role's next attempt.
- **Acceptance proven by the workspace (`workspace_writes`).** The output workspace (`output.partial/`) is
  written only by the model's own tool calls, so a file that was not there at dispatch proves the provider
  accepted the request and ran inference. `run_process` records the workspace's files before the call; a call
  whose envelope proves nothing but whose workspace gained a file is booked as a started inference
  (`mark_inference_start`, one attempt charged, `provider_request_status` `accepted`). A request rejected before
  inference writes nothing and is still booked as not started, and still counts toward the two pre-inference
  requests.
- **`--recover-stranded` applies the same rule.** A 2.1 strand whose `output.partial/` holds files is charged one
  attempt and recorded as started, with an error text that says so; one with an empty workspace is closed
  uncharged, as before. If the journal has no attempt left to allocate, the strand is closed uncharged with a
  note rather than left unrecoverable. Rows an earlier recovery wrote are not rewritten, so an audit already
  refused by the old accounting stays refused: re-audit that paper from a fresh intake.
- **The timeout is a frozen per-audit value (`scripts/phase1_timeouts.py`, new).** `LEGACY_PHASE1_TIMEOUT` is
  2,700 and `SHIPPED_PHASE1_TIMEOUT` 5,400; `LIVE_PHASE1_TIMEOUTS` holds both, oldest first. It is stdlib only and
  imports nothing, like `probe_bounds.py`. The timeout enters no cache key, role input identity or receipt, but
  it is part of the exact `phase_1.runtime` contract and of the snapshot bytes `run.json` binds, so an audit keeps
  the value it froze. `run_phase1.phase1_timeout(policy)` returns it; `packaged_runtime(bound, timeout)` defaults
  the timeout to the legacy value, so `packaged_runtime(bound)` is the 2.7.1 dict. `load_policy` admits either
  live timeout and refuses any other before any provider call ("... its timeout_seconds 3600 is not a packaged
  Phase-1 timeout (2700 or 5400)"); a missing one gets the 2.7.0 refusal.
- **The runners use the audit's own timeout.** `--timeout-seconds` now defaults to the audit's value in
  `run_phase1.py` and `run_verifier.py`, an explicit value must equal it, and the refusal names it ("canonical
  Phase-1 runs require the policy-bound 5400-second/$15 limits"). `bounded_timeout` admits up to the largest live
  timeout. `DEFAULT_TIMEOUT_SECONDS` is kept, as the legacy value, for callers that import it.
- **Why 5,400.** The Sonnet 5 auditors the 2,700 s cap was set for finished in 8 to 34 minutes. On 2026-09-30 a
  teammate's Sonnet 5.5/max auditors finished in about 20 to 35 minutes; on audit A19 one finished in 41
  minutes (344,821 output tokens, USD 7.07) and four were still writing their final files at 45 minutes, twice.
  5,400 s is twice the old cap. The USD 15 per-call budget is unchanged; at about USD 0.17 a minute it binds near
  85 to 90 minutes, so it still bounds cost.
- **Policy, init and drift check.** `config/pipeline-policy-v2.json` sets `phase_1.runtime.timeout_seconds` and
  `phase_1.provider_timeout_seconds` to 5400. `init_run.sh` prints the frozen value (`phase-1 timeout: 5400
  seconds per auditor call`) and refuses a leftover snapshot whose timeout no runner admits. The drift check pins
  both leaves (the descriptive leaf is no longer free), joins its literal to the module, and gains a fifteenth
  planted control (the timeout reverted to 2700), rejected by each pin.
- **Existing audits are byte-identical to 2.7.1.** A 2,700 s snapshot is judged against exactly the 2.7.1 runtime
  dict, and nothing is re-keyed. 2.7.1 refuses a 2.7.2 audit's snapshot before any provider call.
- **Claude CLI (INSTALL.md).** CLI 2.1.281 does not recognise `claude-sonnet-5-5` and serves it with a 200,000-token
  context window (stderr `[claude-code:unrecognized_model]`); 2.1.285 reports 1,000,000 and no warning.
- **Tests.** `tests/test_272_phase1_timeout.py` (18): timeout accounting with a hanging fake CLI, both ways; the
  recovery rule, both ways; the frozen timeout on every live bound and route; the runners' and verifier's
  refusals; 2.7.1 against a 2.7.2 audit (with `I4R_BASE_271` or `../base271`); the drift pins and on-disk reverts.
  Each of the three fixes was reverted in turn and its tests failed. Existing tests updated for the fifteenth
  control, the verifier's default timeout and the reconstructed 2.7.0 policy.

## 2.7.1 — 2026-09-30

New audits' Phase-1 capability probe may use up to 350,000 normalized tokens, about 1.4 times the
largest probe the audit ledgers recorded (254,191); the old bound was 150,000. Every audit that
already exists keeps 150,000 for its whole life. The bound is part of the probe contract, and the
contract is hashed into the probe cache key, into the key that every auditor's input identity and
completion receipt is built on, and through the probe key into every verifier input. So 2.7.1 makes
the bound a frozen per-audit value, as 2.7.0 did for the Claude route: new audits freeze 350,000
from `config/pipeline-policy-v2.json`, and an existing audit's snapshot says 150,000, so it gets the
2.7.0 contract byte for byte. As of 2026-09-30T09:02Z every policy snapshot under `runs/` (246
files) froze 150,000 in both leaves: the 28 audit directories' own (one of them a teammate's Sonnet
5.5 audit started on 2.7.0 at 2026-09-30T08:12:26Z) and 218 copies (archived, in-run and release
copies, and an abandoned intake's). None of them is affected: nothing re-runs and nothing is re-keyed.

- **Measured.** The invocation ledgers under `runs/`, as of 2026-09-30T09:02Z, hold 39 capability
  probe calls in 28 audits (cache replays not counted).
  - *Passed:* 26, one per audit, at 67,830 to 149,259 normalized tokens (median 122,621; 5 to 8
    turns). 25 ran on Sonnet 5 (median 122,675) and 1 on Sonnet 5.5: the teammate's 2.7.0 audit, at
    93,846. The Sonnet 5.5 live canary, run outside `runs/`, used 124,283.
  - *Over the bound:* 11, in 8 audits, failed "capability probe exceeded 150000 normalized tokens",
    at 160,004 to 254,191 (median 178,045; 8 to 11 turns). 3 of them (166,079, 253,086 and 254,191)
    also failed "structured probe result does not match the exact probe contract". The other 2
    failures were "Not logged in", at 0 tokens. So 11 of the 37 probes that used tokens paused
    their audit.
  - *Retries:* all 8 audits passed on a re-run probe: 6 after one over-bound probe, 1 after two
    (252,757 and 178,045, then 122,675) and 1 after three (207,526, 166,079 and 180,859, then
    124,329), each within 15 minutes of its first. The passing retries used 119,498 to 132,694, and
    each over-bound probe cost $0.21 to $0.48.
  - *Why 350,000.* The programme lead chose it: about 1.4 times the worst probe observed (254,191)
    and about 2.3 times the worst passing one (149,259). Under it none of the 11 would have failed on
    tokens (the 3 contract failures still fail on the contract). It still bounds a probe that loops
    on tool calls: the largest probes averaged about 23,000 normalized tokens a turn (254,191 in 11
    turns), so one that keeps calling tools passes 350,000 at roughly 13 to 15 turns, where a passing
    probe takes 5 to 8. The 2.3.x code comment that calibrated 150,000 on 81,002 to 98,166-token
    probes was stale. 300,000 and 500,000 were considered and not shipped; no audit froze either,
    and the runtime refuses both like any other value.
- **One allow-set of bounds (`scripts/probe_bounds.py`, new).** It follows
  `claude_routes.LIVE_ROUTES` and `validate_v2.LIVE_ROUND_CAPS`. `LEGACY_PROBE_BOUND` is 150,000 and
  `SHIPPED_PROBE_BOUND` is 350,000; `LIVE_PROBE_BOUNDS` holds both, oldest first. It is stdlib only
  and imports nothing, so the drift check loads it by path to join its own literal to it, as it does
  `claude_routes.py`; loading `run_phase1.py` for that would run its imports. `run_phase1.py`
  re-exports the three names. The post-delta receipt's code files do not import it.
- **The runners take the bound from the snapshot, and only a live one.**
  - *`load_policy`.* It admits a snapshot whose `phase_1.runtime` is the packaged one with
    `probe_max_normalized_tokens` at either live bound; every other key is still compared exactly.
    `run_phase1.probe_bound(policy)` returns the audit's bound. A bound outside the set is refused
    before any provider call, by `run_phase1.py` (exit 1) and by `run_verifier.py` (refused, exit 5):
    "audit-scoped phase_1.runtime differs from the packaged execution contract: its
    probe_max_normalized_tokens 200000 is not a packaged probe bound (150000 or 350000)". 2.7.0
    refused such a snapshot with the first clause alone, which is still the whole message for a
    missing bound or any other difference.
  - *No default.* `probe_contract(bound)` replaces the `PROBE_CONTRACT` constant, and
    `PROBE_MAX_TOKENS` is gone; only two tests read them. `probe_key`, `portable_probe_key`,
    `validate_probe_cache` and `run_capability_probe` take `bound=` as a required keyword. A missing
    bound is a `TypeError` and one outside the set a `RunnerError`, never a silent re-key.
  - *Where it goes.* `run_phase1.py` passes the audit's bound to the probe (its contract, packet,
    nonce and input hash), to the portable key of the five auditors, to the cache check, to the
    token check and its message, and to the `--probe-only` summary. `run_verifier.py`'s plan carries
    it (`BatchPlan.probe_bound`, set by `plan_batch`) to the probe key it plans on and to its probe
    call. A plan built any other way has no bound and is refused.
- **Existing audits are byte-identical to 2.7.0.** For a 150,000 snapshot, on either Claude route,
  these are the 2.7.0 values:
  - the contract (keys, order and values), its hash and `PROBE_CONTRACT.json`;
  - the probe key, the portable key, the probe prompt and its input hash;
  - the pause message (`capability probe exceeded 150000 normalized tokens`) and the `--probe-only`
    summary.

  So cached probes and role completions replay, and verifier records re-admit. That includes a
  Sonnet 5.5 audit started on 2.7.0, which froze 150,000 too.
- **An existing audit whose probe exceeds 150,000 still pauses.** It stops at the probe, before any
  auditor call: exit 4, `pause_reason` `capability_probe_failed`, and "PAUSED: Phase-1 capability
  probe failed: Phase-1 capability probe failed: capability probe exceeded 150000 normalized tokens",
  as on 2.7.0. The verifier re-probes only when the Claude CLI version or the audit's path changed
  since Phase 1, and pauses the same way (exit 4). There are two remedies, and INSTALL.md gives
  both; the first comes first.
  - *Run the probe again.* Re-run `run_phase1.py` (or `run_verifier.py`). A failed probe is not
    cached, uses none of any role's attempts, and costs one probe call; the next run whose probe
    fits continues. It is the remedy the ledgers record: all 8 audits above recovered this way,
    after 1 to 3 over-bound probes. On a CLI whose own context has grown past the bound, every probe
    will exceed it, and such an audit can continue only from a machine whose probe fits.
  - *Re-initialise.* `bash scripts/init_run.sh "$AUDIT_DIR" --force --audit-id <its audit_id> --doi
    <doi> --paper-title <title>`, with the values from the audit's current `run.json` (leave out
    `--doi` when `paper_doi` is null). A forced re-init rebuilds `run.json` from its arguments, so
    without them the DOI and title are dropped and a new audit id is made. It re-freezes the whole
    shipped policy, not the bound alone: Sonnet 5.5, GPT-6.1 Sol critics, the shipped round cap and
    350,000. It archives the snapshot, the journal and `phase1/`, and re-runs all five auditors, so
    it is the recovery for an audit that has not yet paid for Phase 1.
- **`init_run.sh` prints the bound, and warns about a leftover's.** Init reuses a snapshot left
  without a `run.json` when its Claude routes are the shipped ones, so a leftover frozen by 2.7.0's
  `record_invocation.py --init-policy` gave a new audit (`protocol_version` 2.7.1) the 150,000 bound,
  and init said nothing about it. It now prints `probe bound: <n> normalized tokens`, read from the
  snapshot as frozen, as the round cap is. When that is not the shipped bound it adds a WARNING that
  names both and prints the full `--force` command above, filled in with the audit's id, DOI and
  title. It is only a warning: the audit is valid on its frozen bound. A leftover whose bound no
  runner admits (on the shipped routes, a candidate build's 300,000 or 500,000, say) is refused
  instead, with the leftover's `rm` remedy and nothing written: the re-check found it reused, with a
  warning that the audit keeps that bound for life, when Phase 1 and the verifier refuse such an
  audit before any call. Nothing else in init changes; `--help` prints the two new header lines. INSTALL.md's live canary and
  `reference/phase1_runtime.md` now say the bound is the one the snapshot froze: 350,000 when init
  froze the shipped policy.
- **The shipped policy and the drift check.**
  - *Policy.* `phase_1.runtime.probe_max_normalized_tokens` and
    `phase_1.capability_probe.maximum_normalized_tokens` are 350,000.
  - *Pins.* `config/check_policy_drift.py` pins both leaves to the literal `SHIPPED_PROBE_BOUND`,
    each with its own message. The capability-probe leaf was free until now (it left `FREE_LEAVES`),
    so it could disagree with the runtime and pass.
  - *The join.* `check_probe_bounds` joins the literal to `probe_bounds.py`: its
    `SHIPPED_PROBE_BOUND` must be the literal and the newest `LIVE_PROBE_BOUNDS` entry, and 150,000
    must still be there.
  - *The control.* A new named negative control, "Phase-1 probe bound restored to 150000", reverts
    both leaves and must be rejected by both pins. There are fourteen controls.
  - SKILL.md's generated block does not print the bound and is unchanged.
- **Tests.** `tests/test_271_probe_bound.py` (31 tests):
  - *Byte identity.* The legacy contract, keys and packet are pinned to 2.7.0's digests. Against
    the 2.7.0 tree (`I4R_BASE_270`, else `../base`, else skipped), whose `run_phase1` is imported by
    path in a fresh interpreter, they are equal across a grid of CLI versions, roots and both routes.
    The same legacy audit, probed at one path by either tree, gets identical keys, packet, prompt and
    input hashes, summary and pause line.
  - *The new contract.* The 350,000 contract differs in `maximum_normalized_tokens` alone. No
    function takes a default bound.
  - *Snapshots.* Each live bound is admitted on each route. Eleven other values (among them the
    never-shipped 300,000 and 500,000, and 349,999 and 350,001), a missing bound and another drifted
    runtime key are refused, and both runners refuse before any call.
  - *Probes.* The fake (new knob `FAKE_PHASE1_PROBE_TOTAL_TOKENS`) reports a 200,000-token probe. It
    pauses a legacy audit with the 2.7.0 line and passes a new one, whose full run then reuses the
    cached probe. So do the worst probe the ledgers recorded (254,191) and a 320,000-token one. A
    400,000-token probe pauses a new audit too, with "capability probe exceeded 350000 normalized
    tokens", and caches nothing. For a new audit, `nonce.txt` and the probe row's
    `input_artifact_sha256` are pinned to sha256 of its own (350,000) contract: the review's
    surviving mutants, which built them on the legacy contract (M4b, M5), are now caught. The bound
    is inclusive on both, and a cached probe is judged against the audit's bound.
  - *Across versions.* A legacy Phase 1 and verifier call made by 2.7.0 replay on 2.7.1 with no new
    call, and 2.7.0 replays what 2.7.1 recorded. The negative control: 2.7.0 refuses a new audit's
    snapshot before any call.
  - *The verifier.* A new audit's verifier plans, keys and replays the 350,000 probe, and its
    auditors' receipts are keyed on the 350,000 portable key.
  - *Remedies.* Three paused retries, then a fitting one, finish Phase 1 with every auditor on its
    first attempt; `--force` re-freezes 350,000.
  - *Init.* A new audit prints the shipped bound and no warning. A reused 150,000 leftover is
    printed and warned about, and the `--force` command in the warning, run exactly as printed,
    keeps the audit id, DOI and title and re-freezes 350,000; the negative control, a bare
    `--force`, drops the DOI and title. `init_run.sh --self-test` has the same case (14). A
    leftover at 300,000, 500,000 or 349,999 is refused with nothing written, and a 150,000 one is
    reused (control); with the refusal disabled, those three cases fail.
  - *Drift.* The drift pins, the join and the named control, on disk and in memory, and both leaves
    at each never-shipped value, 300,000 and 500,000.
  - *Mutants.* 42 planted mutants of the runtime, the verifier, the checker, the module and
    `init_run.sh` (each guard reverted, loosened or defaulted; the review's M1, M3, M4, M4b, M5, M6,
    M10 and M13; the never-shipped 500,000 kept live; the token check skipped on new audits), run
    against a copy of the tree: 40 were caught. The two survivors are equivalent: dropping
    `live_probe_bound`'s bool guard (`True` and `False` equal 1 and 0, never a live bound), and the
    cache check skipping its own bound validation (`probe_key` validates the bound first).
  - *Other tests.* Callers of `probe_key` pass the bound. The fixtures standing for a 2.6.10 audit
    (`test_270_legacy_audits.policy_on`, `test_270_critic_route.reconstructed_2610_policy`,
    `test_270_policy_and_init.sonnet_5_policy`) freeze 150,000, so they still equal the 2.6.10
    policy. `test_270_legacy_audits`' negative control, 2.6.10 refusing a Sonnet 5.5 audit, freezes
    that audit with 150,000, as 2.7.0 froze one; with 350,000 2.6.10 refused it at its runtime check
    before the route check the control names. The drift-control counts go from 13 to 14, and so does
    the OK line.
- **Not changed.** The prompts, the critique driver and its runtime helper, the post-delta receipt's
  files, the document builders and the hashed references are unchanged.
- **Upgrade cost.** An audit in flight pays nothing (INSTALL.md, *Upgrading in-flight audits to
  2.7.1*). A 2.7.1 audit cannot be continued on 2.7.0, which refuses its snapshot before any call.
  `gate_set_version` and `toolkit.scripts_sha256` differ for audits initialised on 2.7.1, as for
  every release.

## 2.7.0 — 2026-09-29

New audits run on Claude Sonnet 5.5 and GPT-6.1 Sol, and every audit that already exists keeps the
routes it froze for its whole life: Sonnet 5 and, for its critics, GPT-5.6 Terra (GPT-6 Sol for the
one audit started on 2.6.0). The coordinator, the five Phase-1 auditors, their capability probe and
the post-fan-out verifier take their route from the audit's own frozen `pipeline-policy.snapshot.json`,
never from `config/pipeline-policy-v2.json` after init, and so do the critics, the critique model
probe and the targeted adjudicator. That is unchanged: 2.7.0 changes the routes a new audit freezes,
and lets every Claude runner dispatch both Claude routes.
All 26 audits under `runs/` (and the abandoned intake) are frozen on `sonnet-5`; 25 of them (and the
intake) froze `gpt-5.6-terra` for the critics, and one, started on 2.6.0 and stopped at its critique
probe on 2026-09-23, froze `gpt-6-sol`. None of them is affected: nothing re-runs and nothing is
re-keyed. That one keeps dispatching `gpt-6-sol`; INSTALL.md gives its re-pin to Terra
(`--critic-model gpt-5.6-terra`, since the tool's default is now `gpt-6.1-sol`).

- **One allow-set of Claude routes (`scripts/claude_routes.py`, new).** It follows
  `validate_v2.LIVE_ROUND_CAPS`. `SONNET_5` is `sonnet-5`, spawned as `--model sonnet`, and
  `SONNET_5_5` is `sonnet-5-5`, spawned as `--model claude-sonnet-5-5`; both run at effort max on
  Anthropic. `LIVE_ROUTES` holds both and `SHIPPED` is Sonnet 5.5. Sonnet 5.5 is spawned by its full
  id because the alias does not reach it: on Claude CLI 2.1.206 `--model sonnet` serves
  `claude-sonnet-5`. A live call with `--model claude-sonnet-5-5` and the auditors' flags returned
  correct structured output, its usage keyed `claude-sonnet-5-5`, for $0.03. The post-delta receipt's
  code files do not import the module.
- **The shipped policy.** In `config/pipeline-policy-v2.json` the coordinator, the five auditors and
  the post-fan-out verifier are `sonnet-5-5` / `claude-sonnet-5-5` / max, and
  `phase_1.capability_probe.requested_model` is `sonnet-5-5`. The critic side moved too (below).
- **The runners take the route from the snapshot, and only a live one.** `run_phase1.py` (auditors
  and probe), `run_coordinator.py`, `run_verifier.py` and the coordinator recovery tools used to pin
  the snapshot route to a hard-coded Sonnet 5 dict. They now accept a route only when it is in
  `LIVE_ROUTES`, spawn its `spawn_alias`, and record its `requested_model`; the reservation, terminal
  and cache-replay rows `run_phase1.py` wrote carried a literal `sonnet-5`. Where a prompt names the
  model, a Sonnet 5.5 audit's prompt says "Claude Sonnet 5.5".
- **A served-model check for each route.** A Sonnet 5.5 call must report exactly one
  `claude-sonnet-5-5` primary (a date suffix or a context tag such as `[1m]` is allowed) and no other
  Sonnet id. Anything else is refused with the same class of error as before, naming Sonnet 5.5. A
  Sonnet 5 audit keeps the loose pre-2.7.0 check (`claude_routes.is_sonnet_5_legacy` is
  `run_coordinator.sonnet5`), which also matches `claude-sonnet-5-5`.
- **Sonnet 5 audits are byte-identical to 2.6.10.** They get the same spawn and the same prompt bytes
  for the Phase-1 auditors, the coordinator and the verifier; a packet with no model block renders
  as Sonnet 5. Record values, error messages, probe keys, packet hashes and receipts are also
  unchanged. The verifier prompt is rebuilt from the frozen packet at re-admission and its hash
  checked, so one changed byte would have broken every published verifier record. `pre_265_prompt`,
  the probe contract and its token bound (150,000 normalized tokens) are unchanged; moving it would
  re-key every audit in flight.
- **The spawn environment.** Every Claude CLI subprocess runs with the operator's environment minus
  `ANTHROPIC_MODEL` and `ANTHROPIC_DEFAULT_SONNET_MODEL` (`claude_routes.spawn_env`, `REMAPPING_ENV`).
  A Sonnet 5 audit spawns the alias `sonnet`. Without this, one exported variable could have moved it
  to another model while its rows still said `sonnet-5`, and the loose check would have accepted the
  result.
- **`init_run.sh` never leaves a snapshot behind.**
  - *The defect.* Init froze the snapshot before its model, provider, family and skill-version checks.
    A refused init therefore left a snapshot, and the next init in that directory reused it silently.
    On release day, a teammate still on 2.6.10 would be refused by the version pin, but only after
    freezing a Sonnet 5 snapshot into what the operator then started as a new audit, and the new
    runners accept that route.
  - *Refusals come first.* `policy_to_freeze` reads the policy init would freeze, and writes nothing.
  - *Cleanup.* When init stops before `run.json` binds the snapshot, it removes the snapshot, but only
    one this run created. `record_invocation.py --init-policy` now prints `policy snapshot created: ...`
    only when its O_EXCL create won, and `policy snapshot loaded: ...` otherwise (it used to print
    `policy snapshot: ...`; no caller parsed it). Init also never removes a snapshot whose bytes a
    `run.json` binds. The first cut treated "absent before the freeze" as "mine". So an init that
    loaded a snapshot a concurrent init had just created and bound removed it on the way out, and the
    new "not the policy init checked" refusal always did so.
  - *An initialised audit whose `run.json` is gone.* `run.json` is only a projection of
    `run.events.jsonl`, and `log_event.sh` sends an operator whose `run.json` is gone to init. Any of
    these makes an audit initialised, with or without `run.json`: a non-empty journal, Phase-1 work
    under `phase1/`, or a non-empty `invocations.jsonl`. Without `--force`, init refuses such an audit
    before the version pin and before writing anything.
    - With a journal, the refusal names the rebuild, `python3 scripts/record_invocation.py --recover
      <audit>`. With no journal, it says to restore `run.json` from a backup. Only when a snapshot is
      there does it call that file the policy the audit froze, to keep as it is; otherwise it says the
      snapshot is missing too.
    - Phase-1 work is anything in `phase1/` besides `run.lock` and `status.json`, or a `phase1` that is
      not a plain directory. `run_phase1.py` makes `phase1/` and those two files before it looks for the
      snapshot. So a `run_phase1.py` run before init, the `--probe-only` canary included, refuses with
      "run scripts/init_run.sh on the audit first" and leaves just them (or an empty `phase1/` when no
      Claude CLI is found). The second review found that the first fix refused such a workspace as an
      initialised audit, told the operator to keep a snapshot that did not exist and to restore
      `run.json` from a backup that could not exist, and let only `--force` past; 2.6.10 initialised it.
      It initialises again.
    - It says what `--force` would do, including the move from Sonnet 5 to Sonnet 5.5.
    - The first cut told an audit frozen on Sonnet 5 that its snapshot was a leftover and printed the
      `rm` that deletes the only copy. An audit on the shipped route got a fresh `run.json` written
      over it, and only then the journal refusal.
  - *Leftovers.* A snapshot with no `run.json` and none of those signs, whose Claude routes are not the
    shipped policy's, is refused and left untouched; the refusal names the file to remove. One that
    matches (for example `record_invocation.py --init-policy`, then `init_run.sh`) is reused as before.
  - *`--force`* still re-freezes from the shipped policy, so it now moves an older audit to Sonnet 5.5
    (INSTALL.md).
    - When a forced re-init stops before the new `run.json` is written, it moves the archived snapshot
      back and says so. The old `run.json` and journal are untouched until then, so the audit is left as
      it was. Before, it was left with no live snapshot and no message.
    - A forced re-init refuses when an archive of the same name (made in the same second) already
      exists, rather than overwrite it.
  - *One init at a time.* Init holds an exclusive fcntl lock on `<audit-dir>/.init.lock` from its
    `run.json` check through the journal's initialisation, and, when it stops early, until its cleanup
    has run. A second init of the directory prints `waiting: the init lock of this audit (...)` and then
    finds what the first left: a `run.json` to refuse, or nothing to clean up after.
    - *The defect.* The second review found a residue of the cleanup fix. A snapshot this init
      created, which a concurrent init had loaded as a matching leftover and not yet bound, was still
      removed; the other init was in its package scan, which takes minutes on a large deposit. The
      other init then bound a file that no longer existed and exited 0. After that the runners refused
      ("snapshot is missing") and init refused ("run.json already exists"), so only `--force` got out.
      The lock also closes an older race, present in 2.6.10: two inits could both pass the `run.json`
      check and write `run.json` over each other.
    - *The lock file.* It is removed on release, so a refused init leaves nothing behind. An init that
      waited on a file its holder then removed locks the file at the path instead. One left by a killed
      init is reused and removed. Init already needs fcntl locks in the audit directory (the journal's
      `.run.events.lock`, which every audit under `runs/` has), and the release staging copies named
      files only.
    - *Why a lock.* The alternative, re-checking the snapshot's hash after the `run.json` write, still
      leaves a window between another init's check and its `rm`, and does not stop the two `run.json`
      writes.
- **The printed init commands.** SKILL.md and `new_audit.sh` printed `--auditor-model sonnet-5`, and
  the `init_run.sh`, `log_event.sh` and `finalize_run.sh` self-tests passed it. Init refuses all of
  them once the policy ships Sonnet 5.5.
  - SKILL.md now says `sonnet-5-5`.
  - `new_audit.sh` prints the policy's target, cap, model and provider.
  - The self-tests read the model from the policy.
  - The finalize self-test builds its Claude rows from the audit's snapshot and serves `sonnet-5-5` as
    `claude-sonnet-5-5`.
- **The drift check.**
  - *Pins.* The Claude pins are the literal `SHIPPED_CLAUDE_ROUTE`, joined to `claude_routes.SHIPPED`.
    `SHIPPED` must be the newest `LIVE_ROUTES` entry, beside the Sonnet 5 route every older audit
    froze. The capability-probe leaf, free until now, is pinned to the auditors' route. The critic,
    adjudicator and critique-probe pins are the literal `SHIPPED_CRITIC_MODEL` (`gpt-6.1-sol`); the
    critique probe's route is pinned apart from its cache contract, so a revert is reported as one.
  - *Negative controls.* Four new ones revert the coordinator, the Phase-1 route (the auditors and the
    verifier), the capability-probe leaf, and the critic route to `gpt-5.6-terra`. An allow-set pin
    would accept each revert. There are thirteen controls in all, and each now names the pin
    message(s) that must reject it. A control used to pass on any error, so one pin could stand in for
    another: a Phase-1 revert that also reverted the probe leaf was rejected by the probe pin alone and
    would not have shown a loosened auditor or verifier pin. The Phase-1 control must now be rejected by
    the auditor model pin, the auditor alias pin and the verifier pin, and the probe leaf has its own.
  - *The generated SKILL.md block.* Its Phase-1 line was a literal and kept only areas spawned as
    `sonnet`, so a Sonnet 5.5 policy printed "all areas  use `sonnet-5`" and still passed. It now
    renders every area from the policy.
  - *Init commands in the docs.* A `--auditor-model` in SKILL.md, INSTALL.md or CHEATSHEET.html must
    name the shipped model. Each document is read as a reader copies it: HTML tags stripped (by name,
    so a Markdown placeholder such as `<audit-dir>` stays), entities such as CHEATSHEET's `&nbsp;`
    decoded, backslash-newline continuations joined, and a value may be quoted. A raw-text match missed
    each of those forms. The reference files are hashed into audit identities, so they get no model
    check and were not edited.
- **Docs.**
  - SKILL.md.
  - INSTALL.md, including a new *Upgrading in-flight audits to 2.7.0*.
  - CHEATSHEET.html: the interactive session's `/model` is now Sonnet 5.5; the ChatGPT-app row names
    GPT-6.1 Sol for the critics and adjudication and Codex CLI 0.159.0; two troubleshooting rows cover
    the Sol refusal, at the probe and, once the probe is cached, at a critic.
  - `reference/phase1_runtime.md`, which no code reads or hashes.
  - `scripts/README.md`.
- **Tests.** Five new modules, and every fixture that builds a snapshot from the shipped policy is
  now a Sonnet 5.5 and GPT-6.1 Sol audit; a fixture standing for a 2.6.10 audit is on Sonnet 5 and
  Terra. The fakes serve the model the route asks for: `--model sonnet` still
  serves exactly what it served on 2.6.10, `--model claude-sonnet-5-5` serves `claude-sonnet-5-5`.
  - `tests/test_270_claude_routes.py` (37 tests): the route module; the Phase-1, coordinator and
    verifier pins; the prompts pinned byte for byte to 2.6.10 for a Sonnet 5 packet and a packet with
    no model block; the loose and strict served checks, including a mixed Sonnet 5 + 5.5 call refused
    and a Haiku helper accepted; the records of a 5.5 audit; the recovery tools; a full 5.5 run; and
    the spawn environment. 21 planted runtime mutants were all caught.
    - The second review found two recovery paths no test ran. The misplaced-output tool's `main` now
      runs, `--dry-run`, on real defective attempts: `run_coordinator.py` on a metadata-2.1 audit, the
      fake CLI writing its outputs beside `output.partial`. A Sonnet 5.5 attempt and a Sonnet 5 one both
      pass every precondition and nothing moves. A `sonnet-5-5` row on a Sonnet 5 snapshot is refused
      by the ledger's own validation. That refusal comes before `failed_record`'s profile check, which
      is tested directly.
    - `recover_coordinator.py`'s served-model recheck runs on a quarantined attempt whose envelope,
      record and bundle are re-served. On Sonnet 5.5 it refuses `claude-sonnet-5-5` with
      `claude-sonnet-5` (observed 2), `claude-sonnet-5` alone (observed 0), and `claude-sonnet-5-5`
      with `claude-sonnet-4-5`, and it accepts a dated id. On Sonnet 5 it keeps 2.6.10's check: the
      Sonnet 5 + 5.5 pair is refused with 2.6.10's message, as 2.6.10 refused it, and
      `claude-sonnet-5-5` alone recovers.
    - Four more planted mutants were each caught: `main` dropping the route or reading `SHIPPED`, and
      the recheck made loose, or strict, on both routes.
  - `tests/test_270_legacy_audits.py` (11 tests): audits frozen from the 2.6.10 policy run the probe,
    five auditors, the verifier (re-admitted) and the coordinator on 2.7.0 with `--model sonnet` and
    `sonnet-5` records. Against the 2.6.10 tree (`I4R_BASE_2610`, else skipped), the snapshot and
    the three prompts are byte-identical, and each tree replays the other's Phase 1 and verifier with
    no new call. A Sonnet 5 audit served `claude-sonnet-5-5` is accepted as 2.6.10 accepted it; a
    Sonnet 5.5 audit served `claude-sonnet-5` is refused before anything is promoted. 12 planted
    mutants were all caught.
  - `tests/test_270_policy_and_init.py` (32 tests) covers:
    - the shipped policy's Claude roles against `claude_routes.SHIPPED`;
    - the drift check refusing a revert to Sonnet 5 on disk, and passing on the unmodified tree. Each of
      the two pins that join `claude_routes.SHIPPED` to the checker is isolated by its full message. A
      `LIVE_ROUTES` reordered newest first trips only the newest-entry pin. A Sonnet 5 `SHIPPED` made
      the newest entry, with the policy untouched, trips only the literal pin. Their messages share a
      prefix, which the old assertion matched, so either pin could stand in for the other;
    - a refused init, and one that fails after the freeze, leaving no snapshot, while a snapshot that
      was there before is kept;
    - the leftover-snapshot refusal, and the reuse of a matching leftover;
    - `--force` re-pinning to Sonnet 5.5, and a refused `--force` archiving nothing;
    - an initialised audit whose `run.json` is gone, refused with nothing written and no `rm`. The
      audits are real init output, journal included: Sonnet 5 ones come from a tree copy whose policy
      has the 2.6.10 routes, under a folder named like "Scandinavian Journal of Medicine & Science in
      Sports". The printed `--recover` command, run as printed, rebuilds a `run.json` bound to the
      untouched Sonnet 5 snapshot. Phase-1 work under `phase1/` (as a directory, a symlink, or files
      beside `run.lock` and `status.json`) or an invocation ledger alone is refused too. The refusal
      points to a backup and, with no snapshot there, does not call one the policy the audit froze.
      Negative controls: a fresh workspace still initialises, and so does one where the real
      `run_phase1.py` was tried first (with `--probe-only`, without it, or with no Claude CLI).
      `--force` still starts the audit over;
    - concurrent inits, run through a `python3` shim inside the freeze of the init under test: a
      snapshot another init created and bound, one another process created, and one this init
      created that another process then bound are all kept; and `--init-policy`'s created/loaded
      output. The other init starts in its own session, holding none of the init's descriptors, and
      since the lock it waits for the init under test. The shim can pause it in its package scan. So
      paused, the second review's race runs: it no longer binds a snapshot the first init removed. A
      second init waits and then refuses the first's `run.json`. A third, started while the second
      holds the lock the first released, waits too;
    - a `--force` that fails after the archive, at the gate digest or because `work/` cannot be
      created, putting the Sonnet 5 snapshot back byte for byte. On the shipped route it keeps the
      snapshot the old `run.json` binds. A `--force` refuses and changes nothing when an archive of
      the same second exists; decoys are planted for every second the init can run in;
    - the init commands SKILL.md and `new_audit.sh` print being accepted, and the 2.6.10 one refused;
    - the SKILL.md block regenerating byte for byte, a Sonnet 5 policy still rendering 2.6.10's
      Phase-1 line, and a split policy rendering both routes.

    Each figure below is the release's module run with another `init_run.sh` copied in.
    - Against 2.6.10's `init_run.sh`, 17 of the 32 tests fail (28 failing cases): every init guard
      test, the three serialisation tests, the same-second archive test and the SKILL.md command test.
      The matching-leftover test passes there. So does the `run_phase1.py`-first test, because 2.6.10
      initialised such a workspace; the refusal came with the first 2.7.0 cut. The concurrent-init test
      where another init created and bound the snapshot also passes, because 2.6.10 had no cleanup to
      misfire.
    - Against the first 2.7.0 cut (the `final_pre_sol` tree's `init_run.sh`), 13 tests fail (18
      cases).
    - Against the cut the second review read, 5 fail (11 cases): the `run_phase1.py`-first workspace
      (3 cases), a refusal naming a snapshot that is not there (5 cases), and the three serialisation
      tests.
    - Planted mutants were each caught. The first review's four: no O_EXCL guard, no `run.json` guard,
      no restore, the old `record_invocation.py`. The second review's nine:
      - any `phase1/` counted, or none;
      - the keep line printed with no snapshot;
      - no lock, no re-lock of a removed lock file, or the lock file kept;
      - no same-second archive guard;
      - each of the two checker pins on `SHIPPED` removed.
    - `init_run.sh --self-test` gains the no-snapshot and leftover checks too. It also gains:
      - the initialised-audit refusal with a working `--recover`;
      - a failed `--force` restoring the archived snapshot;
      - a `--force` refusing a same-second archive;
      - a workspace where `run_phase1.py` was tried first initialising, while Phase-1 work is refused;
      - no init leaving its lock file.
  - `record_invocation.py --self-test` builds its critic and targeted-adjudicator rows from the routes
    of the snapshot it freezes (they were literal `gpt-5.6-terra`), so it holds for whichever critic
    route ships. It passes on a copy whose critic, probe and adjudicator say `gpt-6.1-sol`, where the
    old self-test fails. It also checks that `--init-policy` reports created only for the call that
    created the snapshot.
  - `tests/test_270_critic_route.py` (11 tests) drives the real critique driver with the fake Codex CLI
    on an audit frozen from the shipped policy and one frozen from a copy of the 2.6.10 policy: every
    `-m` the fake received, every ledger row, the round's grant and both snapshot manifests name the
    audit's own route (`gpt-6.1-sol` or `gpt-5.6-terra`); the rows validate on that route and not on
    the other, and a `CRITIC_MODEL` or `ADJUDICATOR_MODEL` naming the other is refused before any
    call. An account that refuses `gpt-6.1-sol` stops at the probe with no critic call, and the
    documented re-pin finishes the round on Terra; an account without Terra does not stop a Sol audit.
    `finalize_run.sh --self-test` passes in a tree copy shipping Sol and in one shipping Terra, each
    refusing rows on the other model.
  - `tests/test_270_drift_controls.py` (9 tests): every pin a negative control names is loosened in
    turn on the real checker, and exactly the controls naming it fail, including a loosened auditor pin
    while the alias and verifier pins still fire; the critic-route pins refuse a revert on disk; each
    form of a printed `--auditor-model` (quoted, `&nbsp;`, `&#160;`, tags, continuations, encoded
    dashes) naming `sonnet-5` is refused on disk and passes naming the shipped model, while Markdown
    placeholders and prose are not read as a model.

    15 planted mutants of the checker, the policy, the driver's snapshot read, the finalize self-test,
    the re-pin tool and the fake's refusal were all caught by these two modules.
- **Measured: the live canary.** `run_phase1.py --probe-only` from the release tree, on a synthetic
  audit frozen on Sonnet 5.5 (CLI 2.1.206), completed in the real sandbox: served
  `claude-sonnet-5-5` alone, 124,283 normalized tokens (3,434 output) against the bound of 150,000.
  The 27 completed Sonnet 5 probes under `runs/` used 67,830 to 149,259 (median about 122,000), so
  Sonnet 5.5 sits at the Sonnet 5 median, and the bound is as tight for Sonnet 5 as for 5.5: one
  production probe came within 741 tokens of it. The note in 2.3.x that the bound was calibrated on
  66-98k probes predates that growth. A probe over the bound pauses its audit before any auditor
  call. `--model claude-sonnet-5-5` has not been tried on the teammates' CLIs (2.1.270, 2.1.281,
  2.1.283); each machine's first new audit runs the probe anyway.
- **New audits' critics move to GPT-6.1 Sol.** `config/pipeline-policy-v2.json` routes the four
  critics and the critique model probe (effort high) and the targeted adjudicator (effort max) to
  `gpt-6.1-sol`; the efforts are unchanged. As in 2.6.0 (to `gpt-6-sol`) and 2.6.1 (back to Terra),
  this is policy and fixtures only: `run_critique.sh` reads the critic and adjudicator route from each
  audit's snapshot, and its runtime helper tells the family by the `gpt-` prefix, so neither changed,
  and every existing audit finishes on the route it froze. `critique.adjudication.mode` keeps its
  identifier, `terra_targeted_on_policy_triggers`, as it did under Sol in 2.6.0.
  - *Measured.* On 2026-09-29 at 20:53Z the programme account, on Codex CLI 0.158.0-alpha.2.1, was
    refused with HTTP 400 "The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT
    account". On 2026-09-30 at about 00:31Z, CLI 0.159.0, the ChatGPT app's bundled CLI, listed
    `gpt-6.1-sol` ("GPT-6.1-Sol", efforts low to ultra), and the driver's exact command
    (`codex exec --skip-git-repo-check --ignore-user-config --color never --json --ephemeral -m
    gpt-6.1-sol -c model_reasoning_effort=<high|max> -s read-only -C <ws> --output-schema <schema>
    -o <response> -`) returned correct structured output at high and at max, about 29,000 input
    tokens each. As observed, a new audit therefore needs CLI 0.159.0 or newer (an updated ChatGPT
    app) and an account that offers the model.
  - *A machine that cannot use it* (an app not yet updated, or an account without Sol) stops at the
    critique model probe, the first call of `run_critique.sh 1`, with the provider's words and no
    critic call. The remedy is to update the ChatGPT app and re-run the round, or to re-pin the
    audit to Terra, the 2.6.1 precedent from when `gpt-6-sol` was refused on a teammate's account:
    `python3 scripts/repin_critic_route.py "$AUDIT_DIR" --critic-model gpt-5.6-terra --reason "..."`.
    Without `--critic-model` the tool re-pins to the shipped route, Sol (INSTALL.md).
  - *Only the first probe checks the account.* The passing probe is cached in the audit
    (`critique/.runtime/probes/`), keyed on the model, the effort, the Codex CLI's version string and
    the probe contract, not on the account (the driver is unchanged in 2.7.0). An account without Sol
    that runs a later round, or reruns one, on the same CLI version is therefore refused at each
    critic it dispatches (`[fail ] critic_<role> permanent failure on attempt N (unsupported_model)`,
    one attempt of that role each), not at the probe. Once any review has completed,
    `repin_critic_route.py` refuses as well ("Finish on the current route, or start a new audit"). On
    2.6.1 to 2.6.10 every account could use Terra, so this did not arise. A 2.7.0 audit is therefore
    continued, every round, from an account and a CLI that offer `gpt-6.1-sol`, and the re-pin applies
    only before any critic review has completed; INSTALL.md says so, and CHEATSHEET's troubleshooting
    has a row for each case.
  - *Fixtures.* The finalize self-test builds its probe and critic rows, and its round manifests, from
    the audit's snapshot, serving a dated id (`gpt-6.1-sol-2026-09-29`), and gains a negative control:
    the same rows on another model are refused by name. `tests/test_run_critique.sh` reads the expected
    model from the shipped policy and checks the `-m` each call received; its re-pin scenario now
    refuses the shipped model, checks that no critic is dispatched after the refused probe, and re-pins
    to Terra. The release, verifier-record and verifier-confirmation fixtures journal their critic rows
    on the audit's frozen route. Journal-only and migration fixtures, the schema example record, the
    stale recovery scripts (`retry_*_once.py`, `recover_critique_probe.py`) and
    `migrate_metadata_2_1.py` keep `gpt-5.6-terra`, a legitimate historical route.
- **Upgrade cost.** An audit in flight pays nothing (INSTALL.md, *Upgrading in-flight audits to
  2.7.0*). `gate_set_version` and `toolkit.scripts_sha256` differ for audits initialised on 2.7.0,
  as for every release.

## 2.6.10 — 2026-09-29

The skill now finds the Codex CLI instead of naming one path. The ChatGPT app update of 2026-09-28
moved its bundled CLI from `Contents/Resources/codex` to `Contents/Resources/codex-cli/bin/codex`
(codex-cli 0.158.0-alpha.2.1 on the programme Mac). `run_critique.sh`, `capture_environment.sh` and
the three critique recovery scripts had the old path as their default, so on an updated Mac every
critique round stopped before any provider call with "Codex executable is not executable", and
`run.json` recorded no `codex_cli` version, until the operator exported `CODEX` by hand. A Linux or
WSL host, where the CLI is a `codex` on `PATH`, had no working default at all.

- **`scripts/codex_cli.py` (new)** chooses the binary. An explicit `CODEX`, then `I4R_CODEX_BIN`, is
  used as given, even when it is older than another installed CLI or does not exist, so a typo is
  reported rather than silently replaced. Otherwise it runs `--version` on each candidate (a local
  call, not a provider call) and takes the newest that answers `codex-cli <version>`. The candidates
  are the app's bundled CLI in the new and old layouts, under `/Applications` and `~/Applications`,
  then every `codex` on `PATH`. Pre-releases rank below their release, one file reached twice (a
  symlink) is one candidate, and a tie goes to the bundled CLI, which the app keeps updated.
  `--explain` prints every candidate, its version and the choice.
- **One precedence everywhere.** `run_critique.sh`, `capture_environment.sh`,
  `recover_critique_probe.py`, `retry_completeness_once.py` and `retry_targeted_adjudication_once.py`
  all read `CODEX`, then `I4R_CODEX_BIN`, then the resolver, so the version `run.json` records is the
  CLI the rounds run. INSTALL.md still said `CODEX` and `I4R_CODEX_BIN` ignored each other, which
  stopped being true in an earlier release; it now documents the order. When nothing works the
  driver says so and lists where it looked: "no working Codex CLI found; searched: ...".
- **Tests.** `tests/test_codex_cli.py` covers version order, the moved bundle, the newest CLI
  winning from `PATH`, ties, broken candidates, symlinks, explicit overrides, and the real driver and
  `capture_environment.sh` with `CODEX` unset. Only the fake CLI is run. Run against 2.6.9, its three
  driver-level tests fail with the production message.
- **One-time cost.** A critique round relaunched after the upgrade that reaches targeted adjudication
  pays for one more adjudicator call, because the target snapshot's address binds `run_critique.sh`.
  Nothing else is invalidated (INSTALL.md, *Upgrading in-flight audits to 2.6.10*).

## 2.6.9 — 2026-09-28

2.6.9 merges two releases built in parallel on 2.6.7: the QA release 2.6.8 (gating, reservations,
intake and printed remedies; already live, and its entry below is unchanged), and four fixes
prompted by audits A20 and A21 and by a change in programme policy. A frozen
critique snapshot is now proven intact before every paid call, and harmless mode drift from the
synced folder is repaired rather than retried: one round had lost twelve paid calls to it. Appendix
A renders literally, in columns that keep words whole. A replication deposit that holds personal
data is no longer a reason to pause an audit: the comment reports the identifier by kind, file,
column and count, never by value, and a new release gate refuses a comment that reproduces a
deposit value. The critique round cap is 7 for new audits; an existing audit keeps the cap frozen
in its snapshot until `amend_round_cap.py` raises it, and a finalized audit runs no further round
until a journaled reopen. Of the files the post-delta receipt hashes, only its schema changed (it
admits rounds 6 and 7), so earlier rounds' receipts made under 2.6.8 stay valid and the latest
round's is re-gated once; the verifier's prompt is unchanged, so published verifier records still
re-admit.

The section after *Merged with the QA release 2.6.8* describes the round-cap change, built on the
merge. The four after it describe the fixes as they were built and reviewed on 2.6.7. Their counts,
sweeps ("on 2.6.7 ... fail") and one-time costs were measured against 2.6.7, before the merge; where
the merge changed a statement, the section says so. The costs of upgrading from the live 2.6.8 are
listed at the end of the next section and, by phase, in INSTALL.md, *Upgrading in-flight audits to
2.6.9*.

### Merged with the QA release 2.6.8

Most files merged without a conflict, and both sides' tests pass on the merge. What the merge had to
decide:

- **`integrate_round.py` is a union of both sides.** 2.6.8's refusal to apply or re-apply a round
  once a later round has run, its `apply(..., contested_path=)` signature, its re-apply from the
  pre-integration backup, its superseded (WITHDRAWN) `ADJUDICATION.md` blocks and its canonical
  `critique/round_N/CONTESTED.json` (keys `critic_<role>:<item_id>`; position and evidence both
  required, checked in `main()` before anything is written) all stay, and so does the disclosure
  rule. Three adjustments: 2.6.8's whitelist of decidable fields gains `disclosure_exclusion_basis`
  and `disclosure_flag_reason` (merged without a conflict, the whitelist refused every decision the
  disclosure rule allows as an unknown field); the disclosure rule judges the rows the apply starts
  from, which for a re-apply is the backup; and a flag cleared on a re-applied round is recorded in
  the round's live block, its earlier line relabelled WITHDRAWN with the rest of the superseded
  block. The fix's own malformed-contest check is dropped: 2.6.8's is stricter and runs first.
- **Stale document manifests.** The refusal keeps 2.6.8's wording ("Rebuild the document with
  build_docx.sh, then re-run finalize_run.sh before staging or publishing") and ends with the rebuild
  command, the bound PDF's name filled in, and the INSTALL.md section.
- **Deferred approvals of disclosure findings.** 2.6.8 lets an INCLUDED finding whose approval was
  deferred stay out of the numbered findings when the review stops unfinished, and it forbids
  changing the INCLUDED set after the last round. The disclosure rule refused every flagged row that
  is not final. Together they made an audit that reached its round cap with an unapproved disclosure
  row impossible to finalize, a block the report-not-block policy rules out. `finalize_run.sh` now
  accepts such a row with a warning (`disclosure_policy.deferred_rows`) at `cap_with_unresolved` or
  `aborted`, when the stop lists it as unresolved and the comment's `disclosure.flags` and
  `disclosure.statement` report the exposure. The row then also stands in for the flagged comment
  finding that `governance.pii_present` requires. At `target_complete` and `converged` it must still
  be approved first, and an EXCLUDED or MERGED row is judged exactly as before.
- **Driver order** (`run_critique.sh`), where 2.6.8's refusals meet the snapshot checks. The refusal
  while a killed driver's provider is still running now comes before the round snapshot is built,
  reused or re-frozen, so a snapshot that provider may still be reading is never handed the
  delete-and-rebuild remedy. An orphaned critic worker now stops before the pre-dispatch check and
  the attempt claim, and before a focused repair's, so it no longer re-freezes the snapshot, journals
  a note or spends an attempt of the round's budget. A forced-rerun replay whose snapshot fails its
  integrity check stops with the snapshot remedy instead of advising `I4R_FORCE_RERUN=1`, which would
  pay for every critic again when a plain rerun replays the forced output for free.
  - *Review fix (orphaned workers): the targeted adjudicator stops too.* Its retry loop lacked the
    critic workers' guard, a gap inherited from 2.6.8. When a driver was killed with -9 during the
    adjudicator's retry backoff, the adjudicator woke after the backoff, re-checked the target
    snapshot, claimed the round's next attempt and made a max-effort call for the dead invocation.
    That call ran alongside the rerun that owned the round. On a scratch copy with the fake Codex CLI,
    the orphan spent the round's last attempt. A nondeterministic adjudicator's differing second
    output would also have written `adjudication_cache/<sha>.conflict`, which refuses later plain
    reruns of the round. The guard is now the first statement of that loop as well.
  - *Review fix (orphaned workers): the focused repair's check comes first.* The check ran after
    `bind-contract` and `prepare-repair`. An orphan therefore still created the repair's attempt
    directory, contract, packet and receipt. `prepare-repair` re-validates the response against the
    snapshot, so it could re-freeze the snapshot and journal a `snapshot_mode_refrozen` note for the
    dead invocation. No attempt was spent. The check now runs before any of the repair exists, which
    is what the sentence above claims. The paid attempt is still recorded as what it was.
  - *Review fix: the driver-order tests are behavioural.* `DriverOrder` in `tests/test_269_merge.py`
    checked these decisions only by positions in the driver's source text. It now has four tests
    instead of two, and they run the real driver through a wrapper around the fake Codex CLI with
    per-role transient and sleep knobs. Three kill the driver alone with -9: during a critic's
    backoff, during a repairable critic's call, and during the adjudicator's backoff. The fourth
    changes the round snapshot as the first forced-rerun output is verified. The tests count provider
    calls, budget claims, journal events and runtime-helper commands. The adjudicator and repair tests
    fail on the merge as first built. Two mutants of the merge keep the old source strings but break
    the behaviour: a critic guard that no longer exits, and a replay check that swallows exit 5. The
    old tests pass on both mutants and the new tests fail on both.
- **Check 3c.** `critique/id_map_history.jsonl`, staged since 2.6.8, is a provenance file: it holds
  renumber maps and invocation ids, which nobody could reword. Its e-mail and IP detectors still read
  it. 2.6.8's check 4 refuses any e-mail that is neither the byline's nor printed in the article, so
  3c's exemption for an author's contact address found in the deposit code does not override it;
  both honour check 4's per-line `# i4r-privacy: not-an-email` marker in a code file (3c since the
  release review, below); check 3c remains the only gate for a quoted name, date of birth, IP
  address, postal address or phone number.
- **SKILL.md keeps its 4,500-word cap.** 2.6.8 left the file at 4,499 words and the report-not-block
  rule added 63, so the rule is now stated in 38 words with its detail in INSTALL.md, and the
  document-cache flags and the list of what an audit retains move there too: 4,470 words.
- **Tests.** The fixes' tests follow 2.6.8's stricter semantics: publication needs a tree or
  `I4R_PUBLISH_ANY_ROOT=1`; a contest needs a position and evidence; a re-applied round supersedes its
  block; apply notes the contests it writes; check 3c's staging test leaks a deposit full name, since
  2.6.8's post-delta privacy gate already refuses the e-mail it used to leak. The script index lists
  2.6.8's `lock_wait.py` and `later_disputes.py`. The snapshot mode-storm test is deterministic: a
  sitecustomize hook re-opens `source` after every freeze, where a busy-loop process could lose the
  race to a re-freeze on a loaded host. New `tests/test_269_merge.py` (8 tests) pins the merge's
  decisions, the live-provider order through the real driver; `tests/test_268_pii_report_not_block.py`
  gains the deferred-approval case through the real `finalize_run.sh`.
- **Version.** `VERSION` is 2.6.9, because audits initialised on the live 2.6.8 already record that
  label. `CHEATSHEET.pdf` is regenerated from the merged `CHEATSHEET.html` with headless Chrome.

**Upgrading from the live 2.6.8.** The one-time costs, by phase, are in INSTALL.md, *Upgrading
in-flight audits to 2.6.9*:

- **Receipts.** Of the files the post-delta receipt hashes, only
  `schemas/v2/post-delta-gates.schema.json` differs from 2.6.8's (the round-cap change widened it;
  the others were checked with `cmp`). Earlier rounds' receipts and integrated rounds validate
  unchanged. The latest round's receipt of an audit in flight is refused once as stale gate code:
  re-run that round's `run_post_delta_gates.py` (the refusal prints it; no provider call).
- **Round cap.** New audits get 7; an existing audit keeps its frozen 5 until
  `amend_round_cap.py` raises it, and a finalized audit needs `reopen_audit.py` (or
  `amend_round_cap.py --reopen`) before any new round. INSTALL.md, *Raising the round cap of an
  existing audit*.
- **One extra paid model probe per audit in flight**: the runtime helper's sha256 is part of the
  probe key.
- **One more adjudicator call** for each round relaunched after the upgrade that reaches targeted
  adjudication: the target snapshot's address binds the helper and `run_critique.sh` hashes.
- **Phase 1 re-dispatches if resumed.** The auditor prompt gained `AUDITOR_PII_REPORTING`, and the
  packet binds the changed `reference/checklist.md` and `reference/brief_template.md`. 2.6.8's
  portable-key and alias migration cannot reuse those receipts, because the prompt is part of the
  key. Finish Phase 1 on 2.6.8.
- **Coordinator and critique.** The coordinator snapshot binds changed references, so do not
  re-invoke a coordinator that completed under 2.6.8. `prompts/role_fairness.txt` changed, so finish
  a round dispatched on 2.6.8 there: relaunched on 2.6.9 it pays again for every critic.
- **Release.** Every `document_build_manifest.json` written by 2.6.8 or earlier is stale; rebuild
  with `build_docx.sh` before finalize. Staging and publication run check 3c.
- **Enforcement.** The disclosure checks are errors for a `protocol_version` of 2.6.9 or later
  (`disclosure_policy.ENFORCED_FROM`, moved from 2.6.8 by the release review, below). Audits
  initialised on the live 2.6.8, whose SKILL.md still told the operator to pause for a disclosure
  decision, get them as warnings.
- An audit still on 2.6.7 first takes 2.6.8's step (re-run the latest round's post-delta gates, see
  its entry); the fixes' 2.6.7-relative costs are in their sections below.

### The critique round cap is 7, and an existing audit gets it only by a journaled amendment

The programme raised the critique round cap from 5 to 7, for the papers that did not finish cleanly.
Two published audits (A22, A23) stopped `cap_with_unresolved` after round 5, each with one
section changed after its last review. Three more sat unfinalized with round 5 integrated, and one
after round 4. An assessment on
scratch copies of both published audits, with the fake Codex CLI and no provider call, found that
changing the policy alone would do nothing for them, and would do it quietly:

- The cap is frozen into each audit's `pipeline-policy.snapshot.json` at init, so a new canonical
  value reaches only audits initialised afterwards.
- The driver refused round 6 from a literal that read neither the policy nor the snapshot.
  `validate_v2.py`, `run.schema.json`, the drift check and `init_run.sh`'s default all pinned 5.
  `schemas/v2/post-delta-gates.schema.json` admitted receipts for rounds 2-5 only, so round 6 could
  never be gated.
- An existing audit had no honest way to raise its cap. A hand edit breaks the snapshot's hash
  binding. `init_run.sh --force` restarts the journal. A `metadata_correction` leaves the snapshot at 5.
- The journal was never sealed. On the published copies, round 6 was prepared, authorized, gated and
  run with no record that the audit had been reopened, while `run.json` still carried the old stop,
  its `ended_at` and `compliance_pass: true`.

What changed:

- **Policy.** `critique.round_cap` is 7. `critique.stopping.cap_with_unresolved` no longer names a
  number. A new pinned block, `critique.round_cap_amendment`, names the tool, the reopen tool, the
  verifier, the two events, raise-only, a maximum equal to the cap, and the earlier cap 5.
  `config/check_policy_drift.py` pins 2/7 and the block. It checks that `validate_v2.LIVE_ROUND_CAPS`,
  `run.schema.json`'s enums and the verifier accept exactly 5 and 7. It renders the SKILL.md summary
  as "at the audit's round cap", adds CHEATSHEET.html to the maintained documents, and bans the old
  wording ("hard cap 5", "cap 5", "at round 5", "round&nbsp;6", "five is the hard cap"), including
  across wrapped lines. Two new negative controls (cap back to 5, an unbounded amendment maximum) make
  nine.
- **One cap per audit, read from its snapshot.**
  - `run_critique.sh` refuses a round above the audit's cap before it creates the round folder, and
    names `amend_round_cap.py` when the skill ships a higher cap.
  - `lifecycle_journal.prepare_round` refuses the same round under the journal lock.
    `next_round_hint` at the cap says to finalize `cap_with_unresolved` or amend the cap.
  - `init_run.sh` takes its default from the snapshot and refuses 5 for a new audit.
  - `finalize_run.sh` compares against the run's own cap. The `or 5` fallback is gone.
  - `new_audit.sh` and SKILL.md print `--round-cap 7`.
- **A finalize closes the audit.** The driver and the journal refuse any new round, in any number, on
  an audit whose last finalize has no later `audit_reopened`. Re-logging a `round_result` or
  `round_integrated` before a re-finalize is not a new round and stays allowed.
  - `scripts/reopen_audit.py` journals the reopen for a correction inside the cap. It records the close
    (finalize seq and time, stop, rounds run, receipt hash and registry version, the filed
    publication) and the skill that reopened it.
  - It refuses a published audit whose filed set is still in place, or whose paper is not claimed
    again since the filing. It also refuses a live driver and an interrupted finalize.
  - The projection clears the stop, its detail, `ended_at`, the review-scope fields and the
    compliance verdict (the gate log is kept), so the next finalize must establish them again.
  - *Correcting a published comment* gains the step.
- **`scripts/amend_round_cap.py`.**
  - It raises the cap only, only to the cap this skill ships, and only with a reason. With
    `--reopen` it also reopens a finalized audit in the same journal lock.
  - It rewrites the snapshot: the cap, the old stop text replaced by the shipped one, and a
    `critique.round_cap_amendments` entry. It archives the replaced bytes read-only (0400) beside it,
    as `repin_critic_route.py` does. It journals `policy_amendment`, whose projection sets
    `run.round_cap`, the run's snapshot binding and `run.policy_amendments`.
  - It refuses: a run that is not live metadata 2.1, an invalid or unbound snapshot, a live driver, a
    round already prepared above the cap, and a finalized audit without `--reopen`. With `--reopen`
    it also refuses on the published-set and re-claim rules. `--dry-run` runs every check and writes
    nothing.
- **Hand-logging refused.** `log_event.sh` and the `lifecycle_journal.py append-*` commands refuse
  `policy_amendment` and `audit_reopened`. A hand-logged `metadata_correction` may not set
  `round_cap`, `round_target`, `policy_amendments`, `reopenings` or `toolkit`.
- **`scripts/round_cap_amendment.py`, the verifier.** finalize calls it (violations, and
  `compliance.policy_amendments_valid`, emitted only for an audit with an amendment or a reopening).
  `validate_round_projections.py` also calls it, so finalize step 0c and publication refuse an
  inconsistent chain. It checks:
  1. the snapshot history equals the journal's amendments, written by the tool;
  2. each archive hashes as recorded and differs from its successor only at the cap, its stop text
     and the history;
  3. each round's live plan binds the snapshot in force when it was prepared;
  4. no round above a cap was prepared before the amendment that raised it;
  5. no round ran after a finalize without a reopen (an error from protocol 2.6.9 or once the audit
     has an amendment or reopening; a warning for older records);
  6. the cap at init, raised by each amendment in turn, is the snapshot's and the run's.
- **Validators and schemas.**
  - `validate_v2.py` accepts a cap of 5 or 7, and requires `run.policy_amendments` to chain from a
    live cap to the run's cap, raising only. `cap_with_unresolved` still needs
    `rounds_run == round_cap`.
  - `run.schema.json` has enum [5, 7] and the new `policy_amendments`, `reopenings` and
    `compliance.policy_amendments_valid`. The event-type enums of both schemas gain the two events.
  - The post-delta receipt schema admits any round from 2.
- **Metrics, status, disclosure.**
  - `summarize_invocations.py` counts the time between a finalize and its reopen as paused, not
    idle.
  - `status.py` reads a reopen from the journal.
  - `disclosure_policy.enforced` also holds for an audit reopened by a skill at or above 2.6.8. A
    reopened audit is re-filed under the report-not-block rule: A22's empty `disclosure.flags` and
    `disclosure.statement` must be completed before it re-finalizes.
- **Docs.**
  - SKILL.md: 4,492 of 4,500 words.
  - INSTALL.md: the new section *Raising the round cap of an existing audit*, with the six checks,
    the tools, the runbook for an unfinalized audit, and the exact steps for A22 and A23. It
    names the round-6 critics the probe routed and covers `chmod -R a-w pkg` for A23, the
    notebook re-projection, the WRAPUP rewrite and the ordered-union `review_scope`.
  - Also updated: CHEATSHEET.html (and a regenerated `CHEATSHEET.pdf`, 7 pages),
    `reference/adjudication_guide.md`, `reference/failure_history.md` and `scripts/README.md`.

**Upgrade costs relative to the merge.**

- `schemas/v2/post-delta-gates.schema.json` is one of the files every post-delta receipt hashes. The
  latest round's receipt of an audit in flight is therefore stale once: a round gated but not yet
  run, or the last round before finalize. The driver and finalize print the
  `run_post_delta_gates.py` command, which makes no provider call. Earlier receipts keep their own
  gate code and replay as before.
- `validate_round_projections.py` is not a receipt-bound file, so its change stales nothing.
- `run_critique.sh` changed again. The target snapshot binds its hash, which is already a listed
  upgrade cost (one more adjudicator call for a relaunched round that reaches targeted adjudication).
  The probe key does not bind it.
- A finalized audit needs `reopen_audit.py` before any new round.

**Not done.** No real round was run on any real audit, and the live tree was not touched. Continuing
audits A22 and A23 (moving their filed sets, `chmod` on A23's `pkg/`, and rounds 6-7 at the
provider) is the owner's decision; INSTALL.md gives the steps.

**Tests.** New `tests/test_269_round_cap_amendment.py` (19 tests, the fake Codex CLI, no provider
call):

- through the real driver, round 6 on an unamended cap-5 audit is refused before anything is
  written; after the amendment it runs to quorum bound to the amended snapshot; round 8 is refused on
  cap 7; a finalized audit runs no round until `reopen_audit.py`;
- every refusal of the tool writes nothing; the dry run writes nothing; the archive is 0400 and
  byte-identical; the run is rebound; a second amendment is refused;
- the events and owned fields cannot be logged by hand (`log_event.sh`, `--batch` and all three
  journal CLI appends), while an ordinary correction still can;
- verifier negative controls: a hand-edited cap, a missing or altered archive, a snapshot changed
  beyond the cap, a dropped history entry, a stale round-6 plan, forged events, a round prepared above
  the cap before its amendment, and a round after a finalize (an error once amended, a warning on an
  older record, clean with a reopen);
- validate_v2, run.schema.json and the receipt schema accept 5 and 7 (and rounds 6-7) and nothing
  else;
- finalize's own derivation: `cap_with_unresolved` at round 7 of an amended audit passes; at 6, or
  at 5 once amended, it is a violation and validate_v2 refuses;
- a published audit (test_release_pipeline's fixture) reopens only after its set is moved and the
  paper re-claimed. It re-finalizes with `I4R_REFINALIZE_PUBLISHED=1` into a new registry version.
  The registry keeps both versions, the old receipt no longer verifies, and it publishes again.
  While reopened, the closed stretch counts as paused and `status.py` shows the reopen;
- a reopening by 2.6.8 or later makes the disclosure checks errors for an older audit.

Also: `schemas/tests/test_v2_interfaces.py` covers the cap-7 chain. The `init_run.sh` self-test
refuses 5 and defaults to the snapshot. The `finalize_run.sh` self-test refuses a round after a
finalize with no reopen at 0c, and re-finalizes a reopened audit clean. Two journal fixtures built on
the shipped snapshot now record cap 7.

#### Review fixes to the round-cap change

A review of the merged 2.6.9 on scratch copies of the live audits (fake Codex CLI, no provider call)
found five major and four minor defects in the tools above. Each was reproduced on a copy of the
merge, then fixed:

- **A re-pin and an amendment could wedge an audit.**
  - `repin_critic_route.py` knew nothing of amendments. A re-pin after an amendment rewrote the
    snapshot beyond the cap, and finalize and publication then refused the audit for good, while
    `check-round` kept passing. It now refuses an audit that has a `policy_amendment`: re-pin before
    amending.
  - A re-pin before an amendment wedged the audit too. The verifier's rule 3 took the first
    amendment's starting snapshot as the only earlier one, so the amendment failed its own check after
    writing. The verifier now follows the chain of snapshot bindings (init, each re-pin's rebinding,
    each amendment), and checks that each amendment starts from the snapshot bound when it was made.
  - Two rewrites in one second collided on one read-only archive name. A re-pin also journaled its
    rebinding before it wrote the snapshot, so the collision left a journal binding bytes that were
    never written. Both tools now share a collision-safe name (`...-2.json`). A re-pin archives and
    replaces the snapshot under the journal lock before it appends, and restores the old bytes if the
    append fails.
- **Amend and reopen check the state they would leave, before writing.** The dry run used to pass
  and the real run then write, fail its own check, and leave a permanent violation. Example: a stray
  round event after a pre-2.6.9 close is a warning until a reopen makes it an error.
  `round_cap_amendment.prospective()` now projects the tools' events onto the journal and verifies
  them with the new snapshot and its archive as an overlay. `amend_round_cap.py` and
  `reopen_audit.py` refuse before writing anything, in the dry run as in the real run.
- **The re-claim check reads `paper_source.json`.** The runs/ folder drops `?*[]{}\` from a paper's
  name, and data/ keeps it. For such a paper (A22, and 14 of the 171 papers in data/) the lookup
  found no paper folder, and the re-claim refusal became a printed note. The paper folder is now
  resolved from `paper_source.json` first, as `publish_completed.sh` and `status.py` already do. A
  claim that cannot be confirmed is refused unless `--unconfirmed-claim` is passed.
- **The A22/A23 runbook works as written.**
  - `P`, the runs/ folder name, is now used for `runs/` and `old_runs/` only. `PD`, read from
    `paper_source.json`, names `data/`, `comments/` and `metadata/`. With `P` there, every step-2
    command failed for A22.
  - Every script is called as `"$S/scripts/..."` with `S` the installed skill folder, since the
    tree root has no `scripts/`.
  - A22's `release.rejected-*` is moved with a command that works in bash and zsh.
- **One journaled cap.** `round_refusal` (`check-round`, which the driver calls, and `prepare_round`)
  read the cap from the snapshot file alone. A cap edited by hand and re-hashed, with `run.json`
  rebound, passed both. The round was then prepared and authorized and its model probe paid for
  before the runtime's binding check refused it, and round 6 stayed in the journal of a cap-5 audit
  for good.
  - The cap is now the journaled one: the init's cap, raised by each `policy_amendment`
    (`lifecycle_journal.journaled_round_cap`).
  - Before any round, the snapshot's envelope and `run.json`'s binding to it are proven, and a
    snapshot cap that differs from the journal's is refused, for every round number.
  - A new verifier rule 7 refuses any round event above the cap in force when it was journaled.
- **Minor.**
  - Hand-logged events of a round being run (`log_event.sh`, the `append-*` commands) now obey the
    same cap and finalized-audit refusal, judged in batch order. Re-logging `round_result` or
    `round_integrated` stays allowed.
  - The list of unfinalized audits is corrected. Audits A24, A09 and A25
    have round 5 integrated, and only A24 has its step-1 copies. Audit A04
    stopped after round 4 and needs no amendment yet. The runbook for them says how to make the
    missing step-1 copies (`make_diff_packet.py` prints the commands).
  - A reopen keeps the superseded `finalization_receipt.json` read-only as
    `finalization_receipt.superseded-<finalize seq>.json`, and names it and its hash in the event
    (`previous_receipt_archive`). It also records where the filed set was moved:
    `previous_publication.archived_to`, the newest `old_runs/<J>/<P>.published-*` whose filed
    receipt is this audit's, or `--archived-set`.
  - `amend_round_cap.py` refuses while a round is prepared or started and unfinished. The amendment
    rewrites the snapshot the round's own snapshot copies, so relaunching the round would pay again
    for every critic review and adjudication cached for it. The refusal prints that count, and
    `--accept-rerun-cost` overrides it. With nothing cached it only notes the round, and it runs after
    the prospective check, so an amendment that would fail its own round-cap check reports that instead.
  - A driver killed while a focused repair is being prepared: the orphaned worker is checked again after
    prepare-repair, so it claims no attempt and makes no paid repair call.
  - `disclosure_policy.critic_item_findings` translates no critic item on an audit renumbered without
    `work/id_map_history.jsonl`, whose items may name rows that have since moved: list such a deferred
    row by its finding id.
- **Tests.** New `tests/test_269_cap_fixes.py` (15 tests, fake Codex CLI, no provider call) drives the
  real tools, driver and journal. All 15 fail on the unfixed merge and pass here. One of them executes
  INSTALL.md's runbook blocks verbatim on a fixture tree whose paper name holds a `?`. One migration
  fixture in `tests/test_metadata_2_1_lifecycle.py` recorded cap 5 on the shipped cap-7 snapshot, and
  its `prepare_round` is now refused as a cap mismatch. It now records 7, as the two journal fixtures
  above do.
- **Upgrade cost: none.** None of the changed scripts is hashed by a post-delta receipt, and none is
  part of a probe key or a round snapshot. The copies of all 22 live journals are clean under the new
  verifier (rule 7 included), and each unfinalized one passes `check-round`.
- **Rehearsal.** The A22 and A23 runbooks were re-run end to end on scratch copies of their
  audits and of their `comments/`, `metadata/` and `data/` folders, up to but not including the first
  provider call (the second `run_critique.sh 6`).

### Frozen snapshots are proven intact before every paid call, and mode drift is repaired

On audit A20 (2026-09-27) the Dropbox File Provider reset a freshly frozen
critique snapshot's `source` directory to mode 0700 seconds after the freeze, during the model probe.
Every file stayed 0444 and every hash matched. The only immutability check,
`verify_read_only_inventory`, ran after each paid call and raised a plain error on the write bit. The
driver read that as a schema-invalid response: every critic was re-sampled against the same snapshot,
the focused repair re-ran the same check and fell through, and twelve paid calls were spent before the
round failed. The adjudicator's target snapshot hit the same race. An audit on 2026-09-24 had shown the
same drift on the same directory.

What changed:

- **Integrity and mode are two checks.** `test_run_critique_runtime.py` adds
  `SnapshotIntegrityError` (still a `ValueError`) for a symlink, an extra or missing file, a changed
  byte, a content-address mismatch, or a missing, unreadable or malformed `MANIFEST.json`, at the round
  and target reuse sites as well as after a call. `ensure_frozen` runs the integrity pass first and
  never touches a mode when it fails. Write bits alone are *mode drift*: with `I4R_SNAPSHOT_REFREEZE=1`
  the tree is re-frozen under an `flock` on a regular file (`critique/round_N/.snapshot_refreeze.lock`),
  re-checked, and the one process that ran `chmod` journals a single `note` (kind
  `snapshot_mode_refrozen`). The payload is audit-relative and depth-limited (`source`,
  `source/context`), with no file names and no absolute paths. The integrity pass runs again under
  the lock, right before the first `chmod`. A drift that comes back is re-frozen up to three times,
  with a short pause; one that still comes back is `SnapshotModeUnstableError`, a
  `SnapshotIntegrityError` whose message starts `snapshot_mode_unstable:` (below).
- **Strict by default.** Unset, `ensure_frozen` is today's check, with today's message, and it changes
  nothing on disk. The library, the pinned one-off recovery scripts and compatibility sweeps rely on
  that. Only `run_critique.sh` exports the variable.
- **Checked before dispatch, not only after.** A new helper command, `verify-snapshot --kind
  round|target`, rechecks the directory, the manifest identity, the content address and the modes. The
  driver runs it before the uncached model probe, before every critic attempt, before a focused repair
  and before every targeted-adjudicator attempt, and always ahead of `claim_attempt` and
  `lifecycle_before_provider`. A hard failure therefore costs no attempt, leaves no reservation or
  telemetry row, and makes no call. The role writes `failure.json` with `failure_class`
  `snapshot_integrity`, prints `[stop ] ... no provider call was made` with the remedy, and journals a
  `snapshot_integrity_refused` note.
- **A distinct exit code and error class.** The helper exits 5 on `SnapshotIntegrityError`, printing
  `runtime environment error:`. A post-call validator now proves the bound snapshot intact *before*
  judging the response, so a snapshot changed during a call surfaces as 5, never as a schema error.
  These are `validate-critic`, `prepare-repair`, `validate-repair`, `verify-complete`, `packet`,
  `validate-adjudication` and the projection. On 5 the driver records `failure_class` `permanent`,
  `outcome` `failed` and the new `error_class` `environment`, then stops the role. It makes no focused
  repair, no retry backoff and no permanent marker against the critic or the adjudicator, and it does
  not retire a valid result or adjudication cache to `.invalid`. `environment` is admitted additively by
  `schemas/tools/validate_v2.py`, `schemas/v2/invocation-record.schema.json` and
  `schemas/run.schema.json`. It is not in `config/pipeline-policy-v2.json`, because no
  provider-text classifier produces it.
- **Phase 1 and the verifier.** `phase1_runtime.verify_snapshot` gains `refreeze`, `on_refreeze`
  and `lenient_modes`. Content faults are `SnapshotIntegrityError` (a `Phase1Error`) and name
  manifest indexes, never deposit files. Before any `chmod`, in every mode, it also checks the
  snapshot's inventory: the regular files under `raw/` must be exactly the `source_files` paths (the
  provider sandbox's source), `evidence/` the artifacts plus `evidence_index.json`, `context/` the
  context rows, and the root only `snapshot_manifest.json` beside those three directories; a
  directory must lead to a listed file. A mismatch is an integrity fault that gives counts only, so a
  file that lands in a drifted directory (a sync "conflicted copy") is refused and every mode is left
  as found, never sealed by a re-freeze. Directory modes are judged only when re-freezing, so days-old
  snapshots stay admissible. The re-freeze uses `freeze_tree(snapshot, None)` and re-verifies.
  `run_phase1` checks the snapshot at the top of every role attempt and before a focused repair's
  attempt directory. Inside the SIGALRM pre-dispatch deadline it verifies content and inventory with
  `lenient_modes` (modes counted, nothing changed): SIGALRM can interrupt any syscall, and a
  re-freeze walk cut short would leave the snapshot half-frozen with no note. The walk runs once the
  deadline is disarmed and before any reservation (`refreeze_published_snapshot`, gated by an lstat
  walk), journaled by the process that ran it. A snapshot fault pauses Phase 1 through
  `phase1/status.json` (`snapshot_integrity_fault`, with the remedy), as `sandbox_environment_fault`
  does, and records the paid attempt with `error_class` `environment`; a mode that would not stay
  frozen pauses it as `snapshot_mode_unstable` instead. `run_verifier` works the same way and keeps
  `bind_inputs` journal-free and chmod-free. Under `--dry-run` it is read-only but lenient on modes,
  so the dry run predicts the real run: the content is verified strictly, the drift is reported as
  `would_refreeze: N` in the plan, and it exits 0, with no `chmod` and no journal event.
- **Coordinator recovery.** `recover_coordinator.validate_snapshot_tree` checks integrity first. A
  mode-only drift is re-frozen when `I4R_SNAPSHOT_REFREEZE=1` is set for that run; otherwise it is
  refused as before, and the message now names the remedy.
- **Root-cause hardening (a hypothesis).** Both observed drifts hit the `mkdtemp`-born (0700)
  `source` directory that is moved across parents. Those directories, and Phase 1's staging
  directory, are now created 0755 before the move. The freeze still sets the frozen mode, and no
  hash covers modes.

- **Messages name the snapshot.** Every critique integrity message names the audit-relative
  snapshot and its kind, for example `immutable file hash differs: source/REPORT_TO_REVIEW.md in round
  snapshot critique/round_1/snapshots/<sha>`, because a round and a target snapshot are both in play
  during targeted adjudication. The driver captures the helper's stderr at the round snapshot, the
  packet, the target snapshot and the projection, and names that directory in `FINAL_DETAIL`; before,
  a projection failure said only "the snapshot directory named in the driver log", and the log named
  none.
- **Corruption at reuse is corruption.** At round-snapshot reuse, a `MANIFEST.json` whose
  `input_sha256`, `snapshot_version` or `provider_schema_generation_version` differs from the
  directory's content address and format can only be damaged. It is now a `SnapshotIntegrityError`
  (exit 5), classified as the pre-dispatch check already classified it. A differing round, model,
  effort, roles, `audit_id`, `report_sha256` or `canonical_critic_schema_sha256` stays "snapshot was
  built for a different launch" (exit 1).
- **Modes that keep changing are not a content change.** With the content verified and the modes
  still changing after the bounded re-freezes, the helper exits 5 with `runtime environment error:
  snapshot_mode_unstable: ...`. The driver records the role's `failure.json` with `failure_class`
  `snapshot_mode_unstable` (not `snapshot_integrity`), still `error_class` `environment`, with no
  repair, retry or permanent marker. It prints `[stop ] ... snapshot modes keep changing (content
  verified)` before a dispatch, or `[fail ] ... snapshot modes kept changing after the provider call
  (content verified ...); response kept` after one. `FINAL_DETAIL` says the content verified and
  that the sync client must be stopped or the audit moved off the synced folder. It never says
  "changed during a provider call" and never tells the operator to delete the snapshot, which cannot
  help. The paid response stays under its attempt directory. `failure_class` in `failure.json` is
  free text that no schema validates, and the ledger's `error_class` is the already admitted
  `environment`, so no enum changes. A pre-dispatch refusal of this kind journals a
  `snapshot_mode_unstable` note instead of `snapshot_integrity_refused`.

Remedy for a hard stop: run the command the message prints, in the audit directory:
`chmod -R u+w <dir> && rm -rf <dir>`, where `<dir>` is the content-addressed directory it names
(`critique/round_N/snapshots/<sha>`, `critique/round_N/target_snapshots/<sha>` or
`phase1/snapshots/<sha>`). Then rerun. A frozen snapshot has 0555 directories and 0444 files, so a
plain `rm -rf` fails on every entry. The directory is rebuilt identically. Every printed remedy is
now that full command: the helper's `SNAPSHOT_DELETE_REMEDY`, the driver's `[stop ]` and `[fail ]`
lines and `FINAL_DETAIL`, and the Phase-1 preflight and pause remedies. A `snapshot_mode_unstable`
stop needs no deletion: stop the sync client, or move the audit off the synced folder, and rerun.

One-time costs for audits in flight at the upgrade:

- **One extra paid model probe per audit.** The critique probe cache key binds the runtime helper's
  sha256, and this release edits the helper.
- **One new target snapshot and adjudicator call per round.** The target snapshot's content address
  binds the helper and `run_critique.sh` hashes. A round relaunched after the upgrade that reaches
  targeted adjudication misses `adjudication_cache/<target_sha>` and pays for that call.
- **Nothing else from this change.** Round snapshots and critic result caches are unaffected: the
  round identity excludes code hashes. Phase-1 and verifier keys bind no runtime code. This
  release's other changes to prompts or references carry their own costs.
- The first relaunch on a snapshot that has already drifted re-freezes it and journals one note. The
  note adds one event to `run.events.jsonl` and `run.json` and changes no hash that anything binds.

Compatibility: with `I4R_SNAPSHOT_REFREEZE` unset, the strict check was run against all 118 critique
snapshots under `runs/` (71 round and 47 target snapshots, in 17 audits and 69 rounds), through both
`verify_read_only_inventory` and `ensure_frozen`. Every snapshot passed the integrity pass. 90
snapshots failed the strict mode check, exactly as they do under the 2.6.7 helper: their whole trees
carry write bits again, directories and files alike. The mode, mtime, size and inode of every entry
were identical before and after, as were each audit's `run.events.jsonl`, `run.json` and
`invocations.jsonl`, and no lock file was created. A relaunch of one of those rounds under 2.6.9
re-freezes it and journals one note; under 2.6.7 and 2.6.8 it was refused.

The sweep was repeated for the review fixes on 2026-09-28, over 133 critique snapshots (80 round
and 53 target, in 21 audits) and the 21 Phase-1 snapshots. Every critique snapshot passed the
integrity pass. 105 failed the strict mode check, the same count the 2.6.7 helper gives. All 21
Phase-1 snapshots passed the new inventory check and the content check. Strictly, 17 failed on a
writable `raw/` file and 4 passed, again the 2.6.7 counts. Under `lenient_modes` the 17 would
re-freeze 445 entries in all. The listing of every entry, the three journal files of each audit and
the absence of lock files were identical before and after. One first attempt was confounded by a
production round that started during the sweep, and it was repeated.

Tests: new `tests/test_268_snapshot_fix_a.py` (16 tests) pins each review defect through the real
runners and the fake CLIs. It covers:

- a planted file refused before any `chmod` in every mode and every area, by the library, the
  runner (a pause, then the printed remedy and a clean rerun) and the verifier;
- bounded re-freezes, then `SnapshotModeUnstableError`, in Phase 1 and the critique helper, with
  the helper's exit 5 and its `snapshot_mode_unstable:` token;
- two driver runs under a busy loop that keeps resetting `source`, one from the probe and one from
  a critic's call. Every snapshot failure is `snapshot_mode_unstable`, never "changed during a
  provider call", and the paid response is kept;
- a damaged `input_sha256`, which is corruption at relaunch too. The printed remedy is run
  verbatim, after checking that a plain `rm -rf` fails, and the round then completes on the cached
  probe;
- the helper's split between corruption and a different launch;
- a round snapshot changed during the adjudicator call, named by `FINAL_DETAIL`, removed by its
  printed remedy, and a round that then completes;
- a verifier `--dry-run` that predicts the real run;
- a `sitecustomize` probe proving that `run_phase1.py` and `run_verifier.py` never call
  `freeze_tree` on a published snapshot while `ITIMER_REAL` is armed.

Every one of them fails on the pre-review candidate. `tests/fake_codex.sh` gains
`FAKE_TAMPER_ROUND_FROM`, `FAKE_SNAPSHOT_MODE_STORM` and `FAKE_CORRUPT_MANIFEST`.
`tests/fake_claude_phase1.py` gains `snapshot_planted_file`. Case (b) of `tests/test_run_critique.sh`
now runs the remedy exactly as `ROUND_STATUS` prints it.

Also new: `tests/test_snapshot_mode_drift.py`. It runs one real round with the fake Codex CLI and
covers:

- the re-freeze journaled once, including four concurrent workers;
- the unchanged strict default;
- symlink, extra-file and byte-change faults leaving every mode untouched;
- `verify-snapshot` exits 0, 5 and 1 for round and target snapshots;
- `validate-critic` and `validate-adjudication` exit 5;
- the relaunch re-freeze;
- coordinator recovery;
- the verifier on a drifted Phase-1 snapshot, with and without `--dry-run` (the dry run now exits 0
  and reports `would_refreeze`).

In `tests/test_run_critique.sh`, `tamper_evidence` (now aimable at any role through
`FAKE_INCOMPLETE_ROLE`) costs one call instead of three re-samples, recorded as `environment`, with no
repair and nothing published. A new section drives the new `FAKE_SNAPSHOT_DRIFT=<role>` knob:

- audit A20's shape, a drift during the probe, now completes with every critic at attempt 1;
- a probe-time content change is refused before any critic claim, and the printed remedy then works;
- post-call drift and tamper for a critic;
- drift and tamper of the target snapshot for the adjudicator;
- a relaunch on a snapshot left drifted.

In `tests/test_run_phase1.py`, the mode-only post-call case becomes a content tamper, recorded as
`environment` with a pause. A drift case, and a pre-dispatch case that must never reach
`run_process`, are added. `tests/test_critique_contract_coverage.py` and
`tests/test_error_classification.py` learn the new exception, messages and class.

### Appendix A renders literally, in columns that keep its words whole

Every comment PDF and DOCX had two defects in the Appendix A findings table.

- **Words broke across lines.** The generator's dash counts gave the #, Status, Changes and
  Section columns too little width. LibreOffice therefore split "CONFIR MED", "SPECU LATIVE",
  "Sectio n", "publishe d" and two-digit finding numbers ("1" above "0"). Every published PDF whose
  table carries the generator's separator shows it: 15 to 57 broken fragments per audit across
  `runs/` and `old_runs/`, and 24 to 57 when the same sources are rebuilt with 2.6.7 today.
- **Claims were read as markup.** Claim text reached pandoc verbatim, with only `|` escaped. The
  formula in audit A20's fifth finding, `F*df1/(F*df1+df2)`, printed in italics with its asterisks gone.
  Run through pandoc as they stand, the real Appendix cells give Emph and Quoted inlines in 15 of
  the 17 audits under `runs/`, and Code, Emph, Math, RawInline (text that disappears) and Strong
  in `old_runs/`. A synthetic 2.6.7 build also prints `a<b and c>d` as "ad" and loses the
  backslashes of a Windows path. Smart quotes can also drop a space: a claim quoting a value with
  a trailing space printed the value without it.

The fix is at build time only. `generate_finding_artifacts.py` is post-delta gate code, and
changing it would stale every post-delta receipt and change `REPORT_final.md` for nearly every
audit. So it, the other gate scripts, the Markdown sources, the registry, `comment.json` and every
projection keep the bytes the release below gives them (2.6.7's when the fix was built; 2.6.8's,
which changed some of them, after the merge).

- `scripts/document_release.py` gains the findings-table helpers:
  - `FINDINGS_TABLE_HEADER`: exactly the generator's header.
  - A 206-character separator that gives relative widths 0.055/0.425/0.205/0.155/0.16, in
    half-percent units. Pandoc applies dash-count widths only when a table line is longer than
    `--columns`, and this line always is. On A4, the narrowest page, each narrow column holds its
    widest fixed text on one line: two digits, `SPECULATIVE`, `conclusion?` and `Section 10.2`.
  - `markdown_literal()`: backslash-escapes every pandoc-active character, quotes included.
  - `findings_table_cells()`: the exact inverse of the generator's `clean_cell()`.
  - `pandoc_findings_table()`: a no-op unless a block has the generator's header; it refuses a
    malformed row.
  - `findings_table_integrity()` and its `findings-table-integrity` CLI subcommand.
  - `words()` is unchanged. The integrity check has its own tokenizer, which splits at hyphens so a
    legitimate break after a hyphen passes.
- `scripts/build_docx.sh` rewrites each findings-table block in its converter input. Claim tags
  are stripped from the cells before escaping, so the output is what 2.6.7 would have produced from
  the same text. Caption placement and table count do not change. There are no new files, pandoc
  flags or Lua filters, and the reference document's contents do not change (its print is now
  timestamp-pinned, below).
- A new fail-closed build stage, `findings_table_integrity`, runs after the PDF exists and before
  the manifest is created, so it also covers cache-restored bytes, which real builds now produce
  (below). Each DOCX cell must equal the literal source text; the check normalizes smart quotes,
  dashes, ellipses and non-breaking spaces the same way on both sides and models the build's `§`
  substitution. Every table word must also be whole on the PDF pages from the Appendix A heading
  on.
  - The check counts words as a multiset. The header row repeats at the top of every page the table
    reaches, so its words ('Section', 'published') are required once per printed header; otherwise
    the repeats could stand in for one broken cell word.
  - A Finding word wider than the column itself, such as a long file name, has no break
    opportunity, so it is exempt from the PDF check (it is counted, and its DOCX text is still
    checked). Six of the 17 current audits have such words, and five of them would be refused
    without the exemption.
  - A report without a generated findings table passes before either file is read.
  - The stage is deliberately not a manifest validation. Fixtures that fabricate a DOCX over a real
    Appendix still pass, and later boundaries do not re-run it.
- Quotes are escaped as well, so they print straight in the table. The design had left them to
  smart typography, but that dropped the trailing space of a quoted value, and with quotes
  unescaped the stage refuses one current audit (A21). Dashes and ellipses stay typographic:
  `--`, `---` and `...` print as an en dash, an em dash and an ellipsis, as in body text, and the
  stage maps them back, so the table is literal for markup but not for those three.

Found by the release review and fixed before shipping:

- **Section cells wrapped.** The first widths (0.06/0.43/0.22/0.17/0.12) left the Section column
  45pt of text on Letter and 43pt on A4, so every `Section 10` printed as "Section" above "10"
  (on A4 even `Section 3`). The words stayed whole, so the stage passed. With the rebalanced
  widths above, all 2,976 `Section N`, `Section NN`, `Section N.N` and `Section N.NN` cells of the
  3,004 Appendix rows in the `REPORT_final.md` files under `runs/` and `old_runs/` fit on one line
  on A4 by the font metrics. The other 28 are legacy link-shaped cells in `old_runs/`, which wrap
  at their hyphens as before; a `Section NN.NN` cell, which no audit has, would also wrap.
- **Legitimate characters refused a build.**
  - A zero-width space or word joiner inside a word: pdftotext drops the character and prints
    "totalscore", while the tokenizer split the source into "total" and "score".
  - A soft hyphen: pandoc writes it as `<w:softHyphen/>`, which the DOCX reader skipped, so the
    DOCX check reported that "the converter read the cell as markdown".
  - A term in Chinese, Japanese or Korean: LibreOffice may break between any two ideographs, but
    the term was one token.

  `table_tokens()` now deletes format characters (Unicode category Cf) and makes each character
  of a script written without spaces a token of its own. `docx_cell_text()` reads
  `<w:softHyphen/>` back as U+00AD. A word holding a zero-width space or soft hyphen is accepted
  printed whole or broken at any of them, since LibreOffice breaks there (a soft hyphen then
  prints as "-"); a word with more than eight is not checked in the PDF. None of those 3,004 real
  Appendix rows holds a format character or a character of an unspaced script today. The refusal
  now also says that a claim character can be the cause, and that the claim must not be edited.
- **The stale-manifest refusal named no remedy.** It now ends "rebuild with
  scripts/build_docx.sh <audit-dir> <output-name> (the document builder changed; see INSTALL.md,
  'Appendix A rendering and document manifests')", with the bound PDF's name filled in.
- **The document cache never hit** (a defect in 2.6.8 and every earlier release). pandoc 3.10 stamps the current
  time into the zip entries of the reference document it prints, and the cache key binds those
  bytes, so every build had a new key, no build was restored, and each canonical build added one
  more archive to `.cache/document-builds`. `build_docx.sh` now prints the reference with
  `SOURCE_DATE_EPOCH=0`: the members are byte-identical, and two prints hash the same. The key's
  reference hash changes once, together with the builder hash that changes anyway.
- **Docs.** `reference/report_structure.md` and INSTALL.md no longer say the table prints claims
  exactly: `--`, `---` and `...` still print as dashes and an ellipsis. `CHEATSHEET.html` gains a
  row for the stale-manifest refusal, and `CHEATSHEET.pdf` is regenerated from it with headless
  Chrome's `--print-to-pdf`, the tool that made the 2.6.7 PDF. The PDF therefore carries this
  release's disclosure wording and snapshot row, and no longer the retracted disclosure sentence.

New `tests/test_appendix_rendering.py` (29 tests):

- Real pandoc JSON: the rewritten table has only Str and Space inlines, equal to the raw claims,
  and the exact column specs. The untransformed generator block, as a negative control, shows
  Emph, Math, Code, RawInline, Link, Superscript, Subscript, Cite and Quoted.
- The generator's rendered header matches `FINDINGS_TABLE_HEADER`. The cell splitter inverts
  `clean_cell()`, other tables pass through, and malformed rows are refused.
- The integrity stage refuses a DOCX that lost its asterisks, a PDF that splits CONFIRMED, a
  two-digit number or a short word, a broken cell word hidden among repeated page headers, and a
  DOCX with no findings table. It passes a hyphen break, a
  word wider than the column and smart typography. It models `§` and claim tags, and the CLI
  writes a diagnostic.
- Build wiring with fake converters: the converter input carries the new separator and escaped
  cells, other tables are untouched, `REPORT_final.md` keeps its bytes, and a table-less DOCX stops
  the build before a manifest exists.
- A real pandoc and LibreOffice build proves every word whole and every literal present, and
  proves that cache-restored bytes are checked too. That test now uses the real pandoc,
  unwrapped, and requires the second build to hit the cache with a single archive.
- The widths hold each narrow column's widest text on A4 by the font metrics, and a real build
  re-rendered on A4 prints every Section cell (`Section 10`, `Section 10.2`, `Section 4.10`), every
  `SPECULATIVE` and the header's `conclusion?` on one line, on Letter and on A4.
- Synthetic PDFs pass a zero-width space, word joiner and byte-order mark inside a word, and a word
  broken at its zero-width space or soft hyphen; a word broken elsewhere is still refused, with the
  new wording. A real build of padded rows, in which some words break at each opportunity and some
  terms break between ideographs, passes.
- A manifest bound to an older builder is refused with the rebuild command.
- On 2.6.7, 28 of the 29 tests fail. The pandoc negative control passes there by design. On the
  first candidate of this fix, before the review fixes, 11 fail.

Compatibility, checked on scratch copies:

- Each of the 17 audits under `runs/` and 17 buildable audits under `old_runs/` was rebuilt with
  `--no-cache`. All pass the new stage, every findings cell is plain Str/Space text, and no table
  word is broken.
- `body_pages`, `first_back_matter_page` and `abstract_ending_page` equal a 2.6.7 rebuild on the
  same machine for all 34. They also equal the published manifest for 32. The other two (A24,
  A23) were first built with pandoc 3.7.0.2 and differ in the same way under 2.6.7.
- Total pages grow by up to two. One audit whose older table had equal-width columns shrinks by
  two. Back matter is uncounted.
- The literal `F*df1/(F*df1+df2)` appears in the rebuilt PDF of audit A20. Run against the published
  files, the stage refuses the A20 DOCX (the formula row) and the A26 PDF (50 broken words).
- With the rebalanced widths, all 20 buildable audits under `runs/` and 16 under `old_runs/` were
  rebuilt again with `--no-cache` against the first widths. Every one passes the stage, and
  `body_pages`, `first_back_matter_page` and `abstract_ending_page` are unchanged for all 36. Two
  gain one back-matter page. For three audit copies (20 findings with three SPECULATIVE rows, 14
  with two, and 10 with sub-section cells) the text of every page before Appendix A is identical,
  and page images on Letter and A4 show every Section cell on one line.

One-time cost:

- Changing `build_docx.sh` and `document_release.py` changes the builder and verifier identities
  that every `document_build_manifest.json` binds. Every manifest from 2.6.8 or earlier is stale
  (2.6.8's own change to both scripts staled the 2.6.7 ones as well): finalize (4c),
  `stage_release.sh`, `publish_completed.sh` and the receipt check refuse it until the document is
  rebuilt.
- In-flight audits already rebuild after their last report edit, so for them the cost is one
  `build_docx.sh` run before finalize.
- Post-delta receipts, diff packets, settled approvals, verification manifests and verifier
  records are untouched.
- Re-filing a published comment with the corrected Appendix follows 2.6.8's procedure, INSTALL.md
  *Correcting a published comment* (published folders under `old_runs/`, claim the paper again,
  stage into a fresh directory, re-log delivery and release, finalize with
  `I4R_REFINALIZE_PUBLISHED=1`, publish). The report text does not change, so it needs no new
  critique round; its rebuild step is `build_docx.sh`.
- INSTALL.md gains one release-wide section, *Upgrading in-flight audits to 2.6.9*, with the
  one-time costs of the merged release by phase. It replaces the list that covered only the
  personal-data change.

Deferred to a release that changes gate code on purpose:

- The stale comment beside the generator's separator.
- Literal rendering of `LEDGER.md`, `SETTLED.md` and the verification notebook's claim headings.

### Deposit personal data is reported in the comment, never a reason to stop

New programme policy (2026-09-27): no audit is blocked, skipped, parked or paused because the
replication deposit contains personally identifying information. The paper is audited like any
other. The personal data is reported in the comment as a disclosure-flagged finding that gives the
kind of identifier, the file and column (or the directory and a file count when the file names are
themselves participant codes), the number of rows or participants affected, whether the paper claims
anonymisation, and a verified remedy. The finding never reproduces an identifying value and never
describes how anyone could be re-identified. The filed comment is the disclosure route: I4R decides
any notification, and the audit still never contacts authors or journals.

Apart from the coordinator's package-manifest scan (fixed below), no script stopped these audits;
the instructions did. SKILL.md told the operator to pause for a "responsible-disclosure decision".
SCHEME.md and the comment schema said an `immediate` defect "escalates to the coordinator before the
comment is drafted". The shipped example put the file and column "in the disclosure email, not in
this record", and it listed `pii_description` for removal as "a map to" the exposure. Agents
followed that wording and withheld the location, or stopped altogether. This release rewrites the
wording and makes the reporting half enforceable. No output-sanitizing gate is loosened: participant
codes, a code beside a date or clock time, listings, private paths and data files are refused
exactly as before, and stage check 3c (below) adds one. The rule protects a correct finding only: a
disclosure finding the deposit does not support is refuted and removed like any other claim, on a
stated basis.

- **Policy text.**
  - `SKILL.md` removes the disclosure pause and adds the report-not-block rule with its required
    elements (63 words net when built on 2.6.7; on the merge it is stated in 38 words, detail in
    INSTALL.md, and SKILL.md has 4,470 of its 4,500 words). `CHEATSHEET.html` says the same, and
    `CHEATSHEET.pdf` is regenerated from it.
  - `reference/SCHEME.md`:
    - `immediate` now means reported in the comment, never paused.
    - Rule 3's private channel is for suspicions about conduct only. A Field 3 defect is always
      reported in the comment.
    - Rule 5 and the worked example are reworded to match.
  - `schemas/comment.schema.json` changes descriptions only; the enums, `required` lists and
    structure are unchanged. `schemas/run.schema.json` changes only the `delivery.recipient` example.
  - `schemas/EXAMPLE_comment.json`:
    - The disclosure finding now names `data/wave1_sessions.csv`, column `session_date`, with 82 of 82
      rows and a package locus.
    - `pii_description` and `disclosure.statement` are value-free and no longer listed for removal.
      The auxiliary-information route to re-identification is gone from the statement.
    - Notification is left to I4R (`disclosure_notified: null`).
    - The code-shaped examples in `pii_detector_scope` are replaced with words.
- **Auditor, coordinator and critic instructions.**
  - The five Phase-1 auditors get one new paragraph (`run_phase1.AUDITOR_PII_REPORTING`) inside
    `build_role_prompt`. It asks them to report personal data as a finding with `disclosure_critical`
    true, with the required elements, counts and presence tests only. It also tells them to keep
    study dates and clock times off any line that mentions participants, because the Phase-1
    `participant_date` rule cannot be repaired.
  - The coordinator prompt says a personal-data finding the safe evidence supports stays INCLUDED
    (or MERGED into an included row) and is never EXCLUDED, deferred or withheld. One the evidence
    refutes is EXCLUDED as a tested negative whose reason opens "Refuted:" or "Tested negative:".
  - `prompts/role_fairness.txt` says a value-free description is the required form. For a disclosure
    finding the deposit supports, the critic marks BLOCKING only a reproduced value, a new linkage or
    a re-identification procedure, and never asks for the finding to be removed or deferred. A
    disclosure finding the deposit does not support is REFUTED like any other claim, and BLOCKING
    when materially wrong. The cap is scoped to disclosure findings, so the critic still blocks
    intent attribution or mis-calibrated impact.
  - The references say the same: checklist item 7 applies whether or not anonymisation was claimed,
    `brief_template.md` has a new bullet, `report_structure.md` has a new *Disclosure findings*
    paragraph, and `verification_guide.md` uses count and presence checks instead of UNVERIFIABLE.
  - `adjudication_guide.md` gets a new rule. A request to remove, defer, withhold or coarsen a
    correct, value-free disclosure finding is rejected: it goes in `critique/round_N/CONTESTED.json`
    under its `critic_<role>:<item_id>` key as `{"position", "evidence"}`, the position is written to the round's `RESPONSE_notes.md`, and
    `ADJUDICATION.md` marks the claim CONTESTED. Only a reproduced value, a new linkage or a
    re-identification procedure is corrected. A disclosure finding the deposit does not support is
    refuted like any other claim, and the guide's decisions example shows the structured basis it
    leaves on.
  - The post-fan-out verifier's prompt, `provider_safety.RULE_STATEMENT` and the rule set of
    `check_output_hygiene` are unchanged byte for byte, so published verifier records still
    re-admit.
- **Enforcement.** A new `scripts/disclosure_policy.py` holds the rule for both scripts that apply
  it: the structured basis, the version gate and the remedy text. Intent is never inferred from
  prose. A disclosure-flagged row leaves the comment only on a decision-level
  `disclosure_exclusion_basis`: `refuted`, `duplicate_of:<finding id>`, `not_a_disclosure_defect`
  or `tested_negative`. Anything else is refused. The withholding-language pattern survives only as
  a warning: it missed natural wording such as "Removed from the comment" or "Excluded: GDPR
  concerns", and it refused refuted rows described in privacy words.
  - `finalize_run.sh` refuses three things for an audit whose `run.json` `protocol_version` is 2.6.8
    or later (audits initialised on the live 2.6.8 included):
    - `governance.pii_present` true without a disclosure-flagged comment finding, a non-empty
      `disclosure.flags` and a non-empty `disclosure.statement`;
    - non-empty flags with no flagged finding;
    - a disclosure-flagged row in `work/findings.jsonl` that does not reach the comment. Every such
      row must be final (approved, confirmed, verified or accepted, and INCLUDED), MERGED into a
      final row (a chain is followed), or EXCLUDED with a reason that opens with an allowlisted
      label: "Refuted:", "Duplicate of <id>:" naming a final row, "Not a disclosure defect:" or
      "Tested negative:". A row left INCLUDED but rejected or unapproved used to drop out of the
      comment with no check at all. (After the merge, a row still awaiting approval at
      `cap_with_unresolved` or `aborted` only warns when it is listed as unresolved and reported in
      `disclosure.flags` and `disclosure.statement`; see *Merged with the QA release 2.6.8*.)

    For an older audit the same messages are warnings, so a published audit can still be
    re-finalized to re-file a corrected document. There is deliberately no reverse rule requiring
    flags on every flagged finding, because most published audits flag findings without
    comment-level flags. The `immediate` warning now asks the operator to confirm the finding is
    reported, instead of announcing a pause. New self-test cases cover both version paths: 4k for
    unreported personal data, and 4l for a flagged registry row that reaches no comment.
  - `integrate_round.py apply` refuses, before it writes anything, a decision on a
    disclosure-flagged row that:
    - EXCLUDEs it, clears its `disclosure_flag`, sets its status rejected or superseded, or
      withdraws its coordinator approval, without an allowlisted `disclosure_exclusion_basis`. Only
      a change of state counts: a draft kept unapproved while its claim is revised is still under
      review;
    - withdraws it on a basis but leaves it INCLUDED (it must end EXCLUDED, or MERGED into a final
      row);
    - MERGEs it into a row, or names a `duplicate_of` row, that does not end final: approved, final
      status and INCLUDED. A target that stayed INCLUDED but was rejected used to pass;
    - clears the flag without a `disclosure_flag_reason`.

    A row withdrawn on a basis gets a ledger reason opening with the basis label ("Refuted: ..."),
    which is how finalize reads the basis back, because the registry row cannot carry the decision
    key. A cleared flag's reason and basis are recorded under "Disclosure flags cleared" in
    `ADJUDICATION.md`. Each entry has its own marker and is recorded in the round's live block: a
    re-apply supersedes the block (2.6.8), this section included, so the live block records each
    cleared flag once. The section appears only when a flag is cleared. These are refusals only for
    an audit at 2.6.8 or later. An older audit gets the same messages as warnings and the decisions
    apply, so an audit in flight is not stopped by the upgrade. (A contest without a non-empty
    position and evidence is refused by 2.6.8's own check in `main()`.)
  - `config/check_policy_drift.py` fails if the old pause or withholding wording comes back in
    SKILL.md, INSTALL.md, the cheatsheet, the comment schema, the example, the references or the
    critic prompts.
- **Tests.**
  - New `tests/test_268_pii_report_not_block.py` (25 tests, 26 with the merge's deferred-approval
    case) runs the real `finalize_run.sh` on the
    shipped pair at 2.6.8 and at older versions, and `integrate_round.py apply` on the 2.6.4
    round-one fixture stamped 2.6.8 and legacy. It covers every withdrawal route without a basis,
    bases outside the allowlist, MERGE and duplicate targets that are not final, the label written
    and read back, a flag cleared on a re-applied round, a malformed contested entry, and the
    legacy warnings. It also runs the unchanged output gates on value-free descriptions and on the
    example's disclosure record, with a code-beside-date negative control. It checks the new
    coordinator references against the coordinator's own provider-safety scan and plants old
    wording for the drift check. Every one of the 25 fails on 2.6.7.
  - `tests/test_prompt_contract.py` pins the auditor paragraph (exactly once per role), the
    coordinator and fairness sentences with their refuted-finding scope, and the full verifier
    prompt's 2.6.7 digest.
  - `tests/test_264_verifier_runtime.py` now strips the new paragraph as well as `RULE_STATEMENT`
    before comparing with the 2.6.3 auditor input pin. A new negative control checks that stripping
    `RULE_STATEMENT` alone no longer matches.
  - The fixtures seeded from the example comment in `tests/test_release_pipeline.py` (also used by
    `tests/test_264_verifier_records.py`) and `tests/test_264_verifier_confirmation.py` no longer
    claim personal data they do not report.
- **Compatibility.** Measured on copies on 2026-09-28, with the fix on 2.6.7, before the merge. Four
  audits had by then been initialised on the 2.6.8 release: one complete, two in critique and one
  abandoned before critique.
  - finalize's derivation step was run on copies of the 170 recorded comment and run pairs under
    `runs/` and `old_runs/`, with their registries. On all 166 legacy pairs it gives the same exit
    code and the same errors as 2.6.7. Twenty-seven get new warnings (32 lines). Among them are
    four legacy audits with `pii_present` true and no flags or statement, and registries whose
    disclosure-flagged rows are not final. The completed 2.6.8 audit derives clean. The two 2.6.8
    audits still in critique now fail derivation until their disclosure findings are settled: one
    has flags but no flagged comment finding and one flagged draft row, and the other has
    `pii_present` true without a reported finding.
  - 68 integration-decision files are recorded, and 64 were replayed against their round's
    pre-integration registry (four name no round). Five, all from legacy audits, break the new
    rules: a 2.6.2 audit's round 2 clears seven flags with no reason, and four rows are withdrawn
    with no basis. All five would now integrate with warnings. The two decision files from 2.6.8
    audits break none. One of them keeps a flagged draft row unapproved while revising its claim,
    which is why only a change of state counts as a withdrawal. The pre-review candidate's claim
    that no file would be refused was wrong: it had replayed the post-integration registries, and
    it refused the 2.6.2 file outright.
  - The withholding pattern matches none of the 176 EXCLUDED reasons in the 41 canonical
    registries, 12 of them on disclosure-flagged rows.
  - Audits A21 and A20 re-finalize (rc 0, receipt valid) with this `finalize_run.sh`
    after the document rebuild that fix B requires. That was measured before 2.6.8's
    published-audit gate: re-filing now follows INSTALL.md, *Correcting a published comment*
    (`I4R_REFINALIZE_PUBLISHED=1`).
  - Round-projection and post-delta receipt validation give identical results on 2.6.7 and with the
    fix. After the merge, every file those receipts hash was byte-identical to 2.6.8's; the round-cap
    change then widened the receipt schema (see its section).

**One-time costs for audits in flight, upgrading straight from 2.6.7.** Upgrade between phases where
you can, and finish any critique round already dispatched on 2.6.7 before upgrading. Published audits
are unaffected. The release-wide list, by phase and relative to the live 2.6.8, is in INSTALL.md,
*Upgrading in-flight audits to 2.6.9*. This fix's costs are:

- **Phase 1.** The packet hash binds `reference/checklist.md`, `reference/brief_template.md` and the
  auditor prompt, and all three changed. A Phase 1 resumed after the upgrade re-dispatches, and pays
  for again, roles that completed under 2.6.7. Finish Phase 1 on the old tree, or recover a stranded
  attempt there, before upgrading.
- **Coordinator.** The coordinator snapshot binds the changed references, and `recover_coordinator.py`
  requires the current prompt. Do not re-invoke `run_coordinator.py` on an audit whose coordinator
  completed under 2.6.7: it refuses with "already completed for different immutable inputs". Recover
  a 2.6.7 coordinator attempt with the 2.6.7 tools.
- **Critique.** `prompts/role_fairness.txt` is part of the round's input identity. A round planned
  but not dispatched before the upgrade needs a new plan and authorization, and cached critic results
  are not reused. Rounds already integrated validate unchanged, because they bind the prompt text
  frozen in their own snapshot.
- **Provenance.** `gate_set_version` and `toolkit.scripts_sha256` differ for audits initialised on
  2.6.9, as for every release.
- **Audits already initialised at 2.6.8.** Audits initialised on the 2.6.8 release (four on
  2026-09-28) are enforced. A later round's decisions that withdraw a disclosure-flagged row need a
  `disclosure_exclusion_basis`. At finalize, every flagged row must be final, MERGED into a final
  row, or EXCLUDED with a labelled reason.

### Personal data in the deposit: one deposit-triggered block removed, one output gate added

From this release a deposit that holds personal data is audited and reported like any other. Two code
changes back that policy. The first removes the one script-level stop that an identifier in the
deposit could trigger. The second adds a gate that stops the audit's own outputs from quoting
identifying values. The new policy invites more privacy findings, and until now no gate would catch
one that quoted a value.

**The coordinator's copy of `package_manifest.json` is masked.** `package_manifest.json` is
provider-visible coordinator context. It was scanned with the participant-code rule, whose only
exemption is a column name of the deposit. So a deposit whose *file names* are shaped like
participant codes (one file per participant, named after the code) made `run_coordinator.py` refuse
before any provider call, and `recover_coordinator.py` refused the same bytes. No manifest under
`runs/` or `old_runs/` triggers this today, but it was a block triggered by the deposit itself.

- `run_coordinator.package_manifest_projection` gives the coordinator a masked projection. Each
  code-shaped path segment becomes a stable placeholder (`masked-file-001.csv`, `masked-dir-001`).
  Every entry keeps its size, mtime and sha256 and is marked `path_masked`. A
  `coordinator_masking` block summarises each holding directory as "directory, N files" and states
  that a placeholder is not a deposit path and cannot be cited. This matches
  `build_safe_evidence`'s `paths_emitted: false`.
- The snapshot binds the projection. The post-call live-input check recomputes the projection from
  the live file, and so do `recover_coordinator.py` and `recover_misplaced_coordinator_outputs.py`
  (new `recover_coordinator.check_live_package_manifest`, reused by the latter).
- Nothing else changes. A manifest that already passes the scan is passed through byte for byte, so
  every existing packet, input hash, idempotent re-run and recovery is unchanged.
  `provider_safety.RULE_STATEMENT`, the participant-code rule, `validate_output_privacy` and the
  verifier prompt are untouched. A code-shaped token anywhere else (a Phase-1 report, a manifest
  field that is not a path) still refuses. So do bytes that cannot be projected (not JSON, no
  `files` array), which are returned unchanged. `known_evidence` still binds only the real manifest
  paths, so a placeholder can never be cited as evidence.
- New `tests/test_coordinator_manifest_masking.py` (7 tests; all 7 fail on 2.6.7). A synthetic
  code-named deposit now reaches the fake provider and is promoted. A code token in a Phase-1 report
  is still refused, and the same deposit passes once the report is value-free. A normal deposit's
  manifest is byte-identical in the snapshot. A code token outside the paths still refuses.
  `recover_coordinator` recovers a masked attempt and refuses once the live manifest changes. The
  misplaced-output recovery compares the projection.

**New release gate: stage check 3c, `scripts/check_deposit_values.py`.** Checks 3, 3b and 4 of
`stage_release.sh` know participant codes, timestamps and private paths; since 2.6.8, check 4 also
refuses an e-mail that is neither the byline's nor printed in the article. A finding that quoted a
participant's name, date of birth, IP address, postal address or phone number passed every gate.
Verified on the release fixture: a deposit full name placed in a finding's `evidence.observed` is
projected verbatim into `comment.json`, and 2.6.8's gates stage it (2.6.7's staged a synthetic
e-mail the same way).

- *Collect.* The gate reads the frozen deposit: `pkg/`, else the newest Phase-1 snapshot's
  `raw/pkg`. Supported formats are CSV, TSV, table-shaped TXT, XLSX, JSON and JSON Lines, the same
  inside `.zip` and `.gz`, and, when `Rscript` has `haven`, SPSS, Stata, SAS and R files. For
  those, R sends each data frame's column names and labels, the gate classifies them with the same
  rules as every other reader, and R then sends only the selected columns, over a pipe. The gate
  collects the cells of columns whose header, or SPSS/Stata variable label, looks like a name,
  e-mail, date of birth, IP address (IPv4 or IPv6), postal address or phone number. The header
  vocabulary covers English, German, French, Spanish, Italian, Portuguese and Dutch (Name,
  Vorname, Nom, Prénom, Nombre, Apellido, Nome, Cognome, Sobrenome, Naam, Achternaam; E-Mail,
  courriel, adresse électronique, correo electrónico; DOB, Geburtsdatum, date de naissance, fecha
  de nacimiento, data di nascita, data de nascimento, geboortedatum; IPAddress, clientIP; Adresse,
  indirizzo; Telefon, teléfono, ...). A header in another language is not recognised. Headers are
  matched word by word, so `study_name`, `nom_etude` and `IPAQ_total` do not count. A bare name
  column beside a first-name column is read as the surname (Name + Vorname, Nom + Prénom), and
  beside a surname column as the given name (Nombre + Apellido).
- *In memory only.* Values are normalised: case- and Unicode-folded; dates in ISO form, read from
  numeric, month-name (the seven languages), `dd-Mon-yyyy` and ordinal forms; phone numbers as
  digits; IP addresses canonical. They are hashed with keyed BLAKE2b under a key drawn for the run.
  The key and the hashes never leave the process, nothing value-derived is written to disk, and
  every run rebuilds them. That takes 0.2 to 2.5 s on the published audits and 3.2 s on the
  largest queued deposit's tables. The `--json` summary holds counts, locations and value kinds
  only.
- *Scan.* The gate reads every staged file with a text suffix (the release privacy gate's list plus
  `.R`/`.Rmd`/`.htm`); JSON string by string, with escapes decoded, located by line; HTML without
  its markup, inline tags removed so that a word split by `<b>` is whole, and HTML attribute
  values (an alt text, an href); the visible text of the DOCX; and the text, Info fields and XMP
  text of the PDF. Hyphenated line ends and soft hyphens
  are joined. Bytes that are not UTF-8 are read as Windows-1252, in the deposit and in the release.
- *Refused.* A multi-word name or address from the deposit, matched in any case: a whole cell of
  two words or more, or one row's first and last name in either order, "Last, First" included. A
  date of birth in any common written form. A deposit e-mail, IP or phone value. Any other e-mail,
  IPv4 or IPv6 address. The report gives the location (file and line, PDF page and line, DOCX
  paragraph), the value class and the deposit column, never the value. Staging refuses with the
  remedy: reword the finding to describe the identifier by kind, file, column and count.
- *Warned, never refused.* A single word that matches a one-word value of a name or address column,
  such as a given name or a surname quoted alone. One-word names are too often also ordinary words,
  statistical eponyms or cited authors (Alexander, Welch, Holm, Pearson) for a refusal to be safe.
  The warning prints the location and class. A word beside initials ("J. Roe", "pearson(x") counts
  as one word.
- *Never matched against deposit values.* The skill's own generated and provenance files are never
  matched: `scripts/`, `SETTLED.*`, `ENV.txt`, the document, receipt, verification and release
  manifests, the policy snapshot, and invocation and event telemetry. Nor are the provenance and
  telemetry fields of `run.json` and `comment.json`; a journal note's text is still matched. Their
  text is written by the skill and its tools and cannot be reworded. The e-mail and IP detectors
  still read all of them.
- *Public text is exempt.* Anything that also occurs in the paper's text, in
  `reference/house_citations.md`, in the comment's own References section (a name in either order),
  or anywhere in `report/front_matter.md`. So is an author's contact address copied from the
  deposit's code, when its local part names a byline author.
- *Other false-positive rules.* Each rule is tested, and none applies to a value from the deposit's
  own e-mail or IP column:
  - Values shorter than five characters are not used, nor name values holding a digit or
    underscore. A one-word value must be capitalised in the release to warn, must not be on the
    built-in list of ordinary English words that are also names ("Young", "Grace", "Walker"), and
    must not be a word the paper writes in lower case.
  - A bare "name" column in a data dictionary or survey definition is dismissed and counted, as is
    any name column whose distinct values are mostly identifiers. The first applies when a sibling
    header says label, variable, showif, relevant or similar. An identifier column still has its
    multi-word values collected: variable names hold no space, so a mostly pseudonymised column
    keeps its leftover real full names under watch.
  - Dates: nothing inside a URL, URI or DOI (the XMP namespace `.../1999/02/22-rdf-syntax-ns#`),
    after a `/`, glued to an identifier (`FIND-02/03/05`), inside a longer number chain
    (`6/10/12/17`), before a unit (`10/12/14 km/h`), or dotted after a version cue
    (`Python 3.12.12`, `bash_version 3.2.57`, `v3.1.11`). PDF metadata is read without XMP schema
    URIs and without pdfinfo's structural fields.
  - E-mail: documentation domains, and code that only looks like an address (`X.T@X.values`,
    `x@W.to(device)`, an scp remote), by the rules the release privacy gate used before 2.6.8.
  - IPv4: the loopback, documentation, private, shared, link-local and multicast ranges, and
    version-shaped dotted quads, unless a network word precedes them. A dotted quad is
    version-shaped when every part after the first is at most 31 (LibreOffice 24.2.4.2, SPSS
    29.0.1.0), or when a version word or release suffix sits against it
    ("Linux 5.15.153.1-microsoft"). IPv6: anything outside global unicast (2000::/3), and the
    documentation, special-purpose and multicast ranges inside it.
- *Sources are named without values.* A deposit source is printed as its manifest path, its column
  and an ordinal (`sheet 2`, `member 5`, `object 3`), never a sheet, archive-member or R-object
  name. A path segment shaped like a participant code, an e-mail address, an IP address or a full
  date is printed as its manifest index instead (`manifest file #12`); a year alone is not masked.
- *Never a refusal for the deposit.* A file the gate cannot read is listed as "not scanned". That
  covers Parquet, `.xls`, a broken workbook, SPSS without R, and any reader error, recorded by
  exception class only. A file read only in part is listed as "read in part", with the cap it met.
  The caps are 1,000,000 rows, 200,000 distinct values per name column, 512 MB of delimited text,
  256 MB per workbook or zip member, and 300 R processes per run. A personal-data column that
  yields no usable value is listed too.
- *Exit status.* 0: clean, warnings allowed. 1: a value was found; reword. 2: nothing could be
  certified. Either a release document cannot be read, pdftotext or pdfinfo is missing, or the
  check stopped on an internal error, printed by exception class only. The remedy is to fix the
  document or the environment, not the text. `stage_release.sh` and `publish_completed.sh` branch
  on the status and print the matching remedy.
- *Self-test.* The gate runs 81 synthetic, out-of-range controls (invented names, 1901 and 1902
  birth dates, `.invalid` domains, the reserved 240.0.0.0/4 and returned 3ffe::/16 blocks).
  `stage_release.sh` runs them before the real check. Disabling any one of 58 detection,
  allowlist, reader, labelling and exit-status paths makes it fail. That was checked by source
  mutation on a copy.
- *Publication.* `publish_completed.sh` runs the self-test and the check again on the exact
  publication set, after the release privacy inspection and before anything moves. At that point
  `run.json` meets the gate for the first time, and `comment.json` may have been re-finalized.
- None of this is in a byte-identical or re-admission file. `check_release_privacy.py`,
  `check_consistency.sh`, `generate_finding_artifacts.py`, `run_post_delta_gates.py`,
  `check_output_hygiene` and `validate_output_privacy` are unchanged by it (2.6.8 changed some of
  them; the merge keeps 2.6.8's bytes), so this gate leaves every POST_DELTA receipt made under
  2.6.8, and every verifier re-admission, unaffected (the round-cap change's receipt-schema cost is
  in its own section).

**Hardened after review.** An adversarial review of the first draft of this gate found that it
would refuse every comment of a deposit with an ordinary name column. The house citation, the
`run.json` telemetry, statistical eponyms and the skill's own scripts all matched one-word names,
and nothing could reword them. The draft read XMP namespaces and interpreter versions as dates of
birth. It stored the hashing key beside the hashes in `work/`, printed sheet and archive-member
names, and sent a document that could not be read to "reword the text". It also missed
`dd-Mon-yyyy` dates, IPv6, camelCase IP columns read through R, Romance and Dutch headers, words
split by HTML inline tags or hyphenated line ends, and Latin-1 names deep in a file. The rules
above replace the draft's on each point. `tests/test_check_deposit_values.py` has one regression
test per review finding, and each fails on the draft.

**Checked against every published audit.** The gate was run on copies of the 15 audits under
`runs/*/*/` that have a staged `release/`, and on the 12 filed publication sets (`run.json`,
`comment.json`, claims script, receipt, document and release manifests, PDF, DOCX). All 27 pass,
with 0 hits and 0 warnings. One of those deposits has a genuine birth-date column (122 dates, read
through R), and no release reproduces one of its dates. With a synthetic table planted beside each
audit's own deposit:

- a roster of 400 common first-name and surname pairs, statistical eponyms included: 0 hits, with
  81 warnings in the releases and 10 in the publication sets;
- the tables of the largest queued deposit, a public name list of about 190,000 full names: 0 hits,
  with 537 and 51 warnings;
- the dates the review found inside fixed strings, and 300 adult and 300 youth random birth dates:
  0 hits;
- every day from 1940 to 2016 as a date of birth: 3 hits in 2 releases, all on dates the audit
  texts genuinely write, and none in a provenance file, PDF metadata or a version string;
- genuine leaks of 11 refusing classes planted into a copy of each of the 27 sets (a full name in
  lower case, a first and last name pair, "Last, First", a deposit e-mail in upper case, another
  e-mail, a reformatted birth date, a deposit IPv4 and an IPv6 in expanded form, a public IPv6 and
  IPv4, a phone number, and a name in `comment.json`'s evidence): all 27 refused, every class
  caught, and no value printed.

A first draft refused 14 of the 15 on build numbers in PDF metadata, `ENV.txt` and
`document_build_manifest.json`, on the SPSS build number quoted in two comments' findings, on a
Linux kernel version in `run.json`, on a private-range address in an auditor's detector self-test,
and on one author's contact address copied from the authors' code. The version, range and
contact-address rules above came out of that sweep. Another deposit's formr survey definition has a
`name` column that lists variables, and it is dismissed.

Tests: new `tests/test_check_deposit_values.py` (36 tests). It covers header classification in
seven languages and in-memory hashing with nothing written. It covers leaks of every refusing class
and one-word warnings. It covers value-free descriptions passing, the one-word rules, and
dictionary and identifier dismissal. It covers the allowlists (paper, house citations, reference
list, whole front matter, authors' contact) and the provenance exclusion. It covers the IPv4, IPv6
and date rules; unreadable files and reader crashes never refusing; truncation reports; containers,
JSON and XLSX; and labels that never carry a value. It covers PDF text, Info and XMP metadata and
DOCX, markup, hyphenation and encodings, a 20 MB table streamed, and the R reader with a camelCase
IP column and a numbered RData object. It covers exit 2 for an internal error, and the self-test
failing when any of 18 paths is disabled. Three integration tests drive the real
`stage_release.sh` and `publish_completed.sh` on the release fixture with a planted personal-data
deposit whose four full names collide with the fixture's own boilerplate. A value leaked through
`evidence.observed` (a deposit full name after the merge: 2.6.8's post-delta privacy gate refuses an
e-mail before staging) makes both refuse at 3c with the reword remedy. A value-free finding passes
both. A PDF only check 3c cannot read makes both refuse with the not-inspectable remedy. On 2.6.7
every test fails, because the module does not exist.

**One-time costs for in-flight audits.**

- *Coordinator.* An audit whose deposit has no code-shaped file name gets byte-identical packets:
  no cost. `reference/report_structure.md` gains one sentence pointing auditors and the coordinator
  at check 3c. That reference is part of the coordinator packet, so the release-wide cost is the one
  the policy-text change already brings: don't re-run or recover coordinator attempts from before
  the upgrade on the new tree.
- *Staging.* Check 3c reads the deposit on every staging and every publication, in seconds. It
  writes nothing: no store is kept, so there is nothing to build, migrate or delete. An audit
  already staged under 2.6.8 or earlier is not re-judged unless it is restaged or published. Publication now
  runs check 3c, so an in-flight audit whose outputs quote a deposit value, or carry an e-mail or
  a public IPv4 or IPv6 address outside the allowlists, must reword it and rebuild before it can be
  filed. One-word matches only warn.

### Release review of the merge: re-filing published audits, and check 3c on records

A review of the integrated 2.6.9 on scratch copies of the published audits, with guarded provider
CLIs and no provider call, found that a published audit could not always be re-filed and that check
3c could block an audit it was meant only to report on. Each fix below has a behavioural regression
test in the new `tests/test_269_release_fixes.py` (14 tests). Every test fails on the pre-review
tree, except two negative controls, and every one passes here.

- **A renumbered step-1 copy no longer blocks the last round's receipt** (`make_diff_packet.py`,
  major). Audit A20 (published on 2.6.7) had its round-4 step-1 registry renumbered by an old version
  that kept no history (F19 back to F4; `work/id_map.json` holds only the draft map, so
  `legacy_renumbered` is false). The copy therefore differed from integrate_round's backup. Round 5's
  frozen packet binds exactly its bytes, so the frozen-binding rule forbade any replacement. The
  refusal's restore copied the same bytes over themselves, and `run_post_delta_gates` then asked
  for a rebuild that the binding forbids. The round-5 receipt, stale since the receipt schema
  changed, could never be regenerated, so the audit could not be re-filed. When
  sha256(before-findings) equals the hash bound by the round's own frozen packet, those bytes are
  now authoritative: the packet is rebuilt from them with a note, including by the check-only
  rebuild `verify_diff_packet.py` runs. A copy that differs from the bound bytes is still refused,
  and its printed restore now always changes the file. The rebuilt round-5 packet is byte-identical
  to the published one. `make_diff_packet.py` is not among the files the receipt hashes.
- **Enforcement starts at 2.6.9** (`disclosure_policy.ENFORCED_FROM = (2, 6, 9)`, major). The live
  2.6.8 shipped without the report-not-block rule, and its prompts still said to pause. Audit
  A27, initialised and published on it (`pii_present` true, five flagged
  comment findings, empty `disclosure.flags` and `statement`, urgency `none`), failed re-finalize
  with a SCHEME ERROR. Every disclosure check in integrate and finalize (comment-level fields, flags
  without a finding, the registry review, the decision review) is now a warning for
  `protocol_version` 2.6.8 and earlier ("[warning only: this audit was initialised before skill
  2.6.9]"). A reopening by a 2.6.9 skill still enforces. This moves the version stated in the
  sections above and in the round-cap section ("a reopening by 2.6.8 or later") to 2.6.9.
  `finalize_run.sh --self-test` cases 4k and 4l now stamp 2.6.9 and 2.6.8.
- **Check 3c masks records nobody can reword** (`check_deposit_values.py --mask-records`,
  `stage_release.sh`, major). Phase-1 auditor reports are hash-bound, since editing one fails finalize
  on a stale SETTLED evidence record. Critic outputs are never edited, and work records and evidence
  scripts are bound into their evidence manifests. When one of them quoted a deposit value, 3c
  refused with "reword the finding", which no one can apply, and the audit could never stage. The
  STAGED copies of `report/0N_*.md`, `report/ADJUDICATION.md`, `report/retired_claims.txt`,
  `PAPER.txt`, `BRIEF.md` and everything under `critique/`, `work/`, `phase1/` and `coordinator/`
  (provenance files excepted) now have each matched span replaced with `[masked at release:
  <class>]`. Line numbers are kept, and a JSON string stays valid JSON. Each masking is listed in
  the release's `RELEASE_VALUE_REDACTION.txt` (file, line and class, never the value; a provenance
  file for 3c), and the release is scanned again. The audit's originals are never touched. The
  comment's own outputs (the report and its versions, `LEDGER.md`, the front matter and captions, the
  PDF and DOCX, `comment.json`, the verification notebook and summary, the claims script) still
  refuse. The refusal now prints the remedy for each kind of file it names. Without
  `--mask-records` (publication, a manual check) a record still refuses, and its remedy names the
  staging. The generation-time matcher the review also proposed (Phase-1 hygiene, critic validation)
  is not added: masking at release covers the outputs that already exist, and it needs no prompt
  or verifier change. The self-test gains ten controls (91).
- **3c honours the not-an-email marker** (minor). Check 4 and the post-delta privacy gate exempt a
  code-file line carrying `# i4r-privacy: not-an-email`, but 3c's generic e-mail detector refused it
  with the reword remedy, so such a release passed its round's gates and failed only at stage and
  publish. 3c now applies check 4's own rule (`check_release_privacy._code_file`, `_LINE_BREAK` and
  `_NOT_AN_EMAIL`, read and never changed, so no receipt goes stale) to the generic detector only.
  A deposit's own address is still refused on a marked line.
- **A deferred disclosure row may be listed by its critic item** (`disclosure_policy.deferred_rows`,
  minor). The adjudication guide says to copy the stop's unresolved ids from the round records, and
  those are critic item ids (`critic_stats:S3`), while the row was matched only by its finding id.
  `critic_item_findings` reads every round's critic results and the targeted adjudication, and
  translates each item's finding id through `work/id_map_history.jsonl`. It does not translate
  across a renumber that kept no history. A keyed or bare item id that maps to that row alone now
  lists it. The refusal, INSTALL.md and `reference/adjudication_guide.md` say that either id works.
- **Unapproving a flagged row that stays INCLUDED is "still under review"** (`decision_review`,
  minor). integrate's own remedy for an OVERSTATED verdict on unchanged text is "leave it
  unapproved until a later round confirms it", and the disclosure rule refused it as a withdrawal
  without a basis. Only EXCLUDEing a row, setting it rejected or superseded, or clearing its flag now
  needs a basis. That matches `deferred_rows` and finalize.
- **INSTALL.md.** *Appendix A rendering and document manifests* gives the full re-file chain for a
  published audit. The chain was proven end to end on scratch copies of audits A20 (2.6.7) and
  A27 (2.6.8): `chmod -R a-w pkg`, `build_docx.sh`, and for 2.6.7 or earlier the
  notebook, `--apply` and claims script. Then come the last round's packet and gates, staging into a
  fresh directory, delivery and `phase_end`, `I4R_REFINALIZE_PUBLISHED=1` finalize (compliance_pass
  True on both, receipt valid), and publish, which was not run. The section no longer says that
  every post-delta gate file keeps 2.6.8's bytes. *Upgrading* no longer says that published audits
  need nothing. It also moves the enforcement version, and 3c's section describes the masking and
  the marker.

## 2.6.8 — 2026-09-27

A QA loop run against 2.6.3 (rounds 37–58: three read-only testers per round, fake providers only,
every fix with a test that builds the condition on disk) found gaps in gating, reservations,
intake and the printed remedies. The loop stopped at the first round with no BLOCKER or MAJOR
finding; that round's minor findings are fixed here too. The fixes were then merged onto 2.6.4–2.6.7
(verifier, provider safety, sandbox trim, claim order), which are unchanged in behaviour.

**Upgrading an audit in flight.** `run_post_delta_gates.py` changed, so the receipt of the latest
round is stale gate code: re-run that round's gates (the refusal prints the command), then
re-finalize and stage again if you had already done so. Earlier rounds keep their receipts.

### Gating (a round must not run, or be released, ungated)
- `run_critique.sh` refuses a round 2+ with no `POST_DELTA_GATES.json` (once the audit has
  projections), with a failed-gate `POST_DELTA_GATES.partial.json`, or with a stale receipt, and
  prints the quoted gates command. Only an adjudicator-only retry on the same input is exempt;
  `I4R_FORCE_RERUN=1` is gated too.
- `validate_round_projections.py` (finalize, publish) refuses a last round that ran before any
  passing `gate=post_delta` event carrying its receipt's `log_path`; a receipt made afterwards does
  not count. A trailing round that was only prepared is ignored.
- The driver refuses to resume a started round with different critic roles (`I4R_SUPERSEDE_PLAN=1`
  re-routes on purpose), refuses while a killed driver's provider is still running, and an orphaned
  critic worker makes no further attempt once its driver is gone.

### Reservations (`reserve_paper.sh`, `status.py`)
- Names are computed the same way in every locale and Unicode form, from the folder's on-disk
  spelling; accented or colliding names get a hashed file, and legacy files are still found.
- Dropbox races: the earliest claim across the main file and every conflict copy wins, the winner's
  claim always ends on the main file, `RESERVED` is printed only if the main file holds this claim,
  and a loser deletes only its own claim. A forced release never deletes another paper's claim.
- `status.py` keys claims by journal and paper and labels rows from `paper_source.json`.

### Intake (`setup_workspace.sh`, `new_audit.sh`)
- A package that contains the audit, or is its own `pkg/`, is refused before anything is reserved or
  written; symlinks escaping the package are refused.
- `new_audit.sh` refuses a second audit of the same paper, names the other paper when two titles
  share a run folder, and has `--run-name` for that case.

### Printed commands and docs
- Every printed remedy with a path is shell-quoted (`$`, quotes, `!`, spaces, accents).
- The waiter's rerun hint carries the run's own settings; adjudicator exhaustion names `MAX_TRIES`.
- SKILL.md: the round integration sequence, round-5 stops (`converged` when nothing is unresolved),
  and a shorter verifier paragraph (details stay in INSTALL.md). INSTALL.md: the correction route
  rebuilds the release set and re-logs `round_result`; `phase_end release` follows the delivery.

### Merged with the verifier (2.6.4–2.6.7)
- `make_settled.sh`: an admitted verifier CONFIRMED on a claim's current text counts as a confirmation of a
  reworded claim's new wording, as it does for promotion.
- `integrate_round.py`: a re-apply of the same round starts from what its critics reviewed; it now refuses,
  instead of silently discarding them, when rows were added or claims edited by hand since that apply
  (e.g. a critic's new issue added for the verifier).
- `reference/adjudication_guide.md` and the validator's two-families hint send a row raised after fan-out
  to `run_verifier.py`, never to relabelling it as an auditor's.

Tests: `tests/test_264_qa_fixes.py` (about 250 tests); the release-pipeline, status, critique and
verifier fixtures now follow the stricter gates (contested or answered corrections, step-1 copies,
`expect()`/`check()` snippets, an existing registry, a tree or `I4R_PUBLISH_ANY_ROOT=1`).

## 2.6.7 — 2026-09-27

Audit A21 converged after four critique rounds, passed every release gate and staged a clean release,
and then `finalize_run.sh` refused it: "round 2 critic_completeness: actual claim assessments are not
assigned". Round 2's completeness critic had assessed exactly its eighteen assigned claims but
listed F39 before F3.

The critique runtime already treats a critic's coverage as a set. When it projects a round it
compares `covered_claim_ids` and the assessed `items` with the machine assignment casefolded, each id
once, in any order: no prompt states an ordering rule and the provider schema cannot express one.
`validate_round_projections.py`, which finalization and publication run, still compared the same
lists with plain `!=`. So a round the runtime had accepted was refused only at the end.

- `validate_round_projections.py` now applies the runtime's rule through
  `claims_match_assignment`: exactly the assigned ids, in any order or case. A missing, extra or
  repeated id is still refused.
- New `tests/test_round_projection_claim_order.py`: the observed reordering and a case difference
  pass, while missing, extra and duplicated ids fail.
- Compatibility: the validator gives the same result as 2.6.6 on every audit with critique rounds
  under `runs/` and `old_runs/`, except the one it was fixed for.

## 2.6.6 — 2026-09-26

The first post-fan-out verifier call of audit A21 ran its full 45 minutes without a working
shell. It checked what it could by reading files, left the two claims that need model refits
UNVERIFIABLE, and timed out before the CLI emitted its result. Nothing was admitted. Its report
named the cause: every Bash call failed with "argument list too long", whatever the command.

The CLI's macOS sandbox writes, for every denied path, an unlink and a create rule for the path and
each of its ancestor directories. Each rule carries a 160-byte message, and the whole profile goes
on the command line of every Bash call. The size therefore grows with the number of denials, the
depth and length of the audit path, and the working directory. This audit's paper folder names are
long (a long journal name and a long title), its working directory is 406 characters and 16
levels deep, and the verifier denied 47 paths. Measured with a shell wrapper and the real settings:
the auditors' 10 denials make a command line of about 911,000 bytes, each further denial adds about
8,400, and the exec limit is 1,048,576 including the environment. Earlier verifier runs on audits
with shorter names stayed under it.

`BatchPlan.deny_read` now denies only what can be read:

- a path that exists when the attempt's settings are written and is not an empty directory. No other
  writer runs during a verifier call;
- other verifier batches' packets. Auditor packets hold the same public brief, checklist and evidence
  indexes as the verifier's own packet;
- this audit's own records in its tree (`registry/<audit_id>`, and `<Journal>/<Paper>` under
  `comments/`, `metadata/`, `old_runs/`, `Completed reports/`), instead of those four trees whole;
- `RUN_METADATA.md` and `run_metrics.json` only once finalization has written its artefacts. Before
  then they hold only call and token telemetry.

For that audit the list falls from 47 denials to 21, a command line of 1,015,317 bytes. Its Bash calls
run, measured with a Haiku probe on a working directory of the same length and depth. Every
blindness property holds: the coordinator, critique, settled, comment and WRAPUP artefacts, the
auditors' retained reports, earlier samples of the same batch, and other batches' receipts and
packets stay denied whenever they exist.

Tests: `tests/test_264_verifier_runtime.py` has a new trim test, which fails on 2.6.5. The
blindness test now creates every withheld artefact before it runs and still requires each to be
denied. The tree test requires this audit's records and an earlier audit of the same paper to be
denied, and other papers' records not.

## 2.6.5 — 2026-09-26

Audit A20 stopped
after a paid Phase 1. All five auditors completed on their first attempt, and `run_coordinator.py`
then refused their reports before any provider call:
"Phase-1 report auditor_reproduction contains a concrete participant-code pattern and is not
provider-safe". The reports quoted SPSS column names of the deposit (rating variables), which match the
participant-code shape. The reports are hash-bound to their invocation records, and the runner
had no override, so the audit could not continue. Each change below has a test that builds the
condition on disk and fails on 2.6.4.

### One rule, enforced where it is paid for

Two stages disagreed about what a participant code is. The coordinator refused any
provider-visible text with a code-shaped token (`RAW_PARTICIPANT_CODE`, or a `P12` opening a data
row). Phase-1 output admission (`phase1_runtime.check_output_hygiene`) refused only an identifier
beside a date, and the auditor prompt said only that. So Phase 1 admitted, and paid for, reports the
coordinator was bound to refuse. The teammate audit A05 holds three
Phase-1 reports of that kind: they quote real participant values, and the coordinator will refuse
them on 2.6.4 and 2.6.5 alike.

`scripts/provider_safety.py` is now the single definition. `run_coordinator.py` (every
provider-visible input and the output check that pairs a code with a date), `recover_coordinator.py`,
`recover_misplaced_coordinator_outputs.py`, Phase-1 output admission, the post-fan-out verifier's
admission and its re-admission all call it. At Phase 1 a violation is `report_participant_code` or
`result_participant_code`: not focused-repairable (the repair contract freezes REPORT.md and
scientific_fields), so it spends a fresh sample instead of a coordinator run. The auditor and
verifier prompts state the rule, and `VERIFIER_RULES` registers both names.

### A deposit's own column names are not participant codes

A code-shaped token is admitted when it is demonstrably a variable name of the deposit. It must be
a column header or dictionary name in one of the deposit's data files, and it must never occur as
a data value anywhere in the deposit. A real code lives in an identifier column's cells, so it
stays refused even when a column shares its spelling. The dictionaries read are delimited tables
(`.csv`, `.tsv`, `.tab`, `.dat`, and a `.txt` that parses as a table), Excel workbooks, SPSS
system files (`.sav`, `.zsav`) and Stata 117-119 files.

The exemption can only fail closed:

- A header that looks like one column per participant is never exempt. A file or workbook sheet
  counts when at least three of its non-empty header cells are code-shaped, or begin with a
  code-shaped token plus a short suffix (`P01_SD`, `SUBJ_P01_L`), and those make up more than
  half of its non-empty header cells. Each sheet is judged on its own.
- Values are read from every file except code, syntax, documentation, media and analysis
  containers (`.jasp`, `.omv`, `.spv`). A parsed table has its body scanned; any other file has
  its raw bytes scanned after gzip, bzip2, xz, zip and UTF-16 decoding. Binary data sections are
  searched for each candidate name as a raw substring, because a fixed-width field can hold a
  code flush against the next field. If any such file cannot be scanned in full, no token is
  exempt.
- Code, syntax and documentation files contribute nothing in either direction, since a code can
  sit in an exclusion filter or a comment.

The coordinator reads the audit's `pkg/`; Phase 1 and the verifier read the snapshot's `raw/pkg`.
Internal symlinks are followed as Phase 1 follows them, so the two stages decide from the same
bytes. A deposit is computed once per process (five Phase-1 roles share one computation), and
the value backstop runs only when some name could be exempt. The code-plus-date check on
coordinator output (`validate_output_privacy`) keeps its 2.6.4 rule with no exemption, exactly as
`check_consistency.sh` applies it to the same report at every post-delta gate.

Every consumer applies the same rule:

- `build_safe_evidence.py` does not count a participant-code match inside an exempt deposit
  name, so its receipt agrees with the coordinator's scan of the same payload.
- `build_draft_registry.py` truncates evidence text at word boundaries. A cut inside `HR_P95th`
  could manufacture `HR_P9`.
- `run_verifier.py` refuses, before any provider call, a claim holding a code-shaped token that
  the verifier would have to repeat.

The auditor and verifier prompts now state the rule as the shape it is: file names,
syntax-created variables, labels and quotations included, with the deposit-header exception.

On the A20 deposit the eight code-shaped names are all SPSS dictionary names, and the
four refused reports and the draft registry now pass. An older audit's
questionnaire item names are admitted the same way. On every other audit under
`runs/` and `old_runs/` nothing is newly admitted, and the reports of audit A05 stay refused.

### Review

A three-lens adversarial review (privacy leak, robustness, contract consistency) had each claim
rerun by two independent skeptics. It confirmed 19 defects in the first cut, left 3 contested and
refuted 1. The first round of fixes addressed all 19:

- A negative SPSS record length looped forever.
- Three layouts escaped the participant-column test: a per-participant sheet next to a wider
  one, three or fewer participant columns, and blank or companion header cells.
- An unparsed data file loosened the rule.
- A code that filled a Stata field went undetected.
- The output check disagreed with `check_consistency.sh`, and the symlink handling differed
  between the stages.
- There was quadratic regex cost, 8x memory in SPSS decoding, and five simultaneous computations
  in Phase 1.
- The prompt stated a semantic rule for a shape check.
- The verifier sent claims it would then refuse.

A rerun of every reproduction against that round found 14 of the 19 fixed and five still partly
reproducing. They are fixed now:

- SPSS value labels were skipped. A numeric identifier whose value labels are the codes (1 is
  labelled "P1") kept the codes out of the values, so questionnaire items named `P1` to `P20`
  elsewhere in the deposit were exempt. The dictionary reader now parses record 3 and the record 4
  that must follow it, and the long-string labels in record 7 subtype 21. Every label text is a
  value, decoded like the file's other strings, and so is the labelled value of a string variable.
  A label record that is truncated, lacks its record 4, or names a variable outside the dictionary
  makes the file unscannable.
- Compressed and columnar files were invisible to the byte scan. Identifiers in a Parquet (snappy
  or zstd), Feather (lz4) or raw zlib file were not seen, and names they held were exempt. A file
  the scan cannot read as text is now unscannable: Parquet, Arrow and Feather, zstd, lz4, snappy,
  raw zlib, 7z, RAR, HDF5, MATLAB, NumPy, pickle, SQLite, OLE2 (`.xls`), SPSS portable, an R file
  in no known serialization, and any other binary with control bytes above 1 % of a 64 KiB window
  or mostly non-ASCII bytes that are not UTF-8. R serialization (`.rds`, `.RData`, bare or inside
  gzip, bzip2 or xz) is still read, because it stores each string whole, and each candidate name
  is also searched for in it as raw bytes. Folder metadata from macOS and Windows (`.DS_Store`,
  AppleDouble, `Thumbs.db`, recognised by content) is read as before; the A20 deposit's
  stimulus zips carry 25 such files. An SPSS, Stata or Excel file inside a zip is now parsed
  too, and its names still count as values.
- A structured read that failed fell back to the byte scan. A `.zsav` with a wrong trailer offset
  was never inflated, the scan of its compressed bytes found nothing, and the file did not count
  as unscannable. When the SPSS, Stata or Excel reader fails, or openpyxl is missing, the file is
  now unscannable. There is no byte fallback: an SPSS string can be split across bytecode command
  blocks.
- A directory that could not be listed was skipped without a word. The deposit walk now reports
  every listing, examination or read error, and the deposit is then unscannable. Internal symlinks
  are followed exactly as before. Phase 1's `source_files` refuses such a directory before any
  provider call, so its snapshot cannot silently drop files that the coordinator, reading the live
  `pkg/`, counts.
- The prompts and the validator disagreed about paths. The validator refuses a source join or
  evidence path naming a deposit file such as `pkg/EMG_P01.csv`, while the auditor and verifier
  prompts demand exact joins copied from the manifest and the rule statement only said to describe
  file names in words. The validator is unchanged. The rule statement now says the rule covers
  source joins and evidence paths: such a file is not cited, the evidence is bound to another
  manifest entry (for example the script that reads the file), and the file is described in words.
  `VERIFIER_RULES` registers the new phrases.

On every deposit under `runs/` and `old_runs/` the names, values and exemptions are the first
round's, and so is every sweep verdict. The value labels of 28 real SPSS files, and one long-string
label record, now parse without error.

Accepted residuals:

- The participant-column test is a heuristic. A wide table with fewer than three code-shaped
  headers, or where code-shaped headers are not a majority of its non-empty headers (six context
  columns plus `SUBJ_P01` to `SUBJ_P05`, say), has those headers treated as dictionary names.
  Tightening it would refuse common questionnaire-item layouts (`EXP1` to `EXP4`) again.
- `RAW_PARTICIPANT_CODE` is quadratic on pathological runs such as 60,000 characters of
  `AB_AB_...` (about 6 s). That is unchanged from the 2.6.4 coordinator scan, and it now also runs
  at Phase-1 admission.
- A `.zsav` is inflated in memory, at about 6.6x the inflated size at peak. `MAX_INFLATED_BYTES`
  bounds it; beyond the cap the file is unscannable.
- A finding whose only evidence is a deposit file whose name contains a code-shaped token cannot be
  evidence-bound. The rule fails closed.

### Audits started on 2.6.4

The verifier's frozen packet hash covers the SHA-256 of its prompt, and re-admission rebuilds the
prompt to check it. Every consumer re-admits verifier records: `integrate_round.py`,
`make_settled.sh`, finalize and publish. Adding the rule to the prompt therefore made every verifier
record made on 2.6.4 unadmissible. On a copy of audit A28, 2.6.4 re-admitted its
one verifier call and the first 2.6.5 candidate refused it: "the frozen packet and the runner's
prompt do not hash to the receipt's packet_sha256".

The rule now sits inline where 2.6.4 had a space, so removing that one sentence gives the 2.6.4
prompt byte for byte (`run_verifier.pre_265_prompt`, pinned against 2.6.4's own builder).
Re-admission accepts the current prompt or that one. A record frozen with the 2.6.4 prompt is
validated under the contract it ran with, so the participant-code rule, which its verifier was never
told, is not applied to its output (`check_output_hygiene(participant_code_rule=False)`). Every other
rule still is, and any other prompt text is still refused.

Phase-1 auditor inputs change too (their prompt states the rule), so a Phase 1 resumed on 2.6.5
re-runs any role that had not completed on 2.6.4. A completed Phase 1 is never re-read by prompt:
the coordinator and later stages read its reports and quorum. Do not re-run `run_phase1.py` on an
audit whose Phase 1 completed before the upgrade.

### Tests

`tests/test_provider_safety.py` (72 tests) builds CSV, semicolon-text, UTF-16, Excel, Stata 118,
gzip, zip, R serialization and SPSS deposits, with and without value labels, and a file of every
opaque kind above. Its SPSS fixtures read back correctly in R `haven`, including a
bytecode-compressed string that straddles a command block. Where R is installed, further fixtures
are written by `haven::write_sav` (numeric and long-string labels, bytecode, uncompressed and
zsav), `saveRDS` and `save`, and read back in R before they are scanned. The tests check both
stages' verdicts on the same bytes and cover each review defect, with negative controls.
`tests/test_run_coordinator.py` drives the real runner with a Phase-1 report that quotes deposit
column names; on 2.6.4 the positive case fails with the audit's own error.
`tests/test_prompt_contract.py` pins the prompt statement and the two registered rules.
`tests/test_264_verifier_runtime.py` re-admits a record frozen with the 2.6.4 prompt, refuses any
other prompt, and pins the 2.6.4 prompt digest.

## 2.6.4 — 2026-09-25

Audit A23 excluded three true article-text findings, F13–F15. The external critics had confirmed all
three in rounds 1–4, but the protocol had no admissible Claude-family confirmation for a finding
raised after Phase 1. Each change below has a test that builds the condition on disk. A review of
the release candidate found defects in the first cut of the runner and the gates; where a verifier
section below says "used to" or "once", it describes that candidate, never a shipped release.

### A finding raised after fan-out could never be promoted

A registry row is promoted only with confirmations from two model families. The Claude-family
(family-A) confirmation could come only from the Phase-1 auditor whose report raised the row
(`integrate_round.py` `build_plan`, and only in round 1). A finding first raised by the operator,
by the coordinator, or as a critic's new issue therefore had no admissible family-A reviewer. In
audit A23 the operator ran separate Sonnet checks of the three claims and built the ledger
rows by hand. Each gate refused them in turn:

- `make_settled.sh` aborted on the first (`role_class other`): "verification invocation is not a
  completed reviewer provider call".
- It then aborted on the second, relabelled `role_class auditor`, because the call ended after
  round-1 integration.
- The third, filed in round 2, was carried through rounds 2–4. Finalize then counted it as a sixth
  auditor: "canonical auditor quorum differs: missing=[] extra=['auditor_operator_verification']".

All three rows were corrected to `legacy_record`, and F13–F15 were excluded.

2.6.4 adds a packaged family-A role for exactly these rows, `scripts/run_verifier.py`, run as
`python3 scripts/run_verifier.py "$AUDIT_DIR" --findings <ids>` (one batch; `--dry-run` prints the
plan and writes nothing). It keeps the two-family rule intact:

- **Eligibility.** It runs only on `INCLUDED` rows with claim text that no Phase-1 role (the audit's
  own `phase_1.required_roles`) raised or independently found. Each row must carry a
  `source_location` and cite at least one `paper/` or `pkg/` file at the Phase-1 snapshot's hash;
  two rows in one batch may not share a claim text.
- **Identity.** Its calls are `role_class verifier`, round 0, role `verifier_<12 hex>` (one role, and
  so one three-start budget, per batch of hypotheses). A call is never an auditor and never counted
  in the quorum. It is routed to the auditors' Sonnet 5/max route and must be served on their
  family. The journal refuses a verifier reservation at any round other than 0 before appending.
- **Runtime.** It runs under the Phase-1 runtime contract through `run_phase1.run_role` and a new
  `RoleTargets` object: sandbox, no network, an immutable packet, the 2,700 s / USD 15 limits, the
  evidence manifest, source-hash joins, admission, retry classes, the strand guard, response
  recovery and one structural repair. It reuses the snapshot `phase1/quorum.json` binds and never
  rebuilds one. The auditors' targets (`auditor_targets()`) are the 2.6.3 behaviour, and their role
  input hash, probe key and sandbox settings are unchanged.
- **Blind framing.** It sees each claim as an opaque hypothesis with its source location and
  snapshot hashes. Critic verdicts, the coordinator's reasoning, status, provenance, the finding id
  and every other registry field are withheld. Beyond the paths Phase 1 already denies (`paper/`,
  `pkg/`, `report/`, `work/`, `critique/`, `run.json`, the journal, the ledger,
  `package_manifest.json` and the policy snapshot), the sandbox denies `coordinator/`, the settled
  cache, the comment projections, `claims_map.tsv`, `verification_snippets/`, `WRAPUP.txt`, earlier
  batches' receipts and status files (`phase1/completions/`, `phase1/verifier/`) and
  `phase1/transactions/`. Each attempt also denies, computed when its settings are written
  (`BatchPlan.deny_read`): every other attempt and role under `phase1/roles/` and
  `phase1/role_packets/` (earlier samples, rejected verdicts on the same claim, and the five
  auditors' retained reports), the finalization artefacts at the audit root (`claims_script.R`,
  `verification*`, `RUN_METADATA.md`, `run_metrics.json`, `finalization_receipt.json`,
  `RELEASE_MANIFEST.md`, `release-manifest.json`, `document_build_manifest.json`), the sibling
  `release/` directory, and, only when the tree root resolves as `tree_layout.sh` resolves it, the
  tree's `registry/`, `comments/`, `metadata/`, `old_runs/` and `Completed reports/`. A path that
  contains the audit is never denied. `phase1_runtime.sandbox_settings` accepts the per-attempt
  list as a callable, and the same callable reaches the preflight, every attempt and the focused
  repair.
- **Verdicts.** It returns CONFIRMED, REFUTED or UNVERIFIABLE per hypothesis, and they record as
  `confirmed`, `refuted` and `inconclusive`. There is no OVERSTATED, and UNVERIFIABLE never counts as
  a confirmation.
- **Binding.** A verdict binds to a row by the SHA-256 of the row's current claim text, not by
  finding id. Renumbering therefore cannot misbind it, and editing the claim voids it.
- **One verdict per claim text.** The runner never re-sends a claim that already has an admitted
  verdict. A REFUTED or UNVERIFIABLE verdict on a claim text blocks every verifier confirmation of
  that text, and it keeps blocking after its call is corrected to `legacy_record`, while its record
  still re-admits; only a CONFIRMED stops counting when its call is superseded.
- **Timing.** `integrate_round.py apply` binds only a call that ended no later than that
  integration's timestamp, in every round, and `make_settled.sh` requires the same of the latest
  integration. Round 0 keeps the call clear of the critique-round authorization gate.
- **Exit codes.** 0 completed, replayed or a dry-run plan; 1 a usage or unexpected error (a usage
  error used to exit 2, the code for a failed call); 2 the call failed; 3 a preflight or sandbox
  fault; 4 the capability probe failed; 5 refused, with no provider call and no journal event.

`INSTALL.md` (*Confirming findings raised after fan-out*) gives the commands, the blindness list,
the timing, the budget, the gates and recovery.

### Only what the runner produced counts: every consumer re-admits the record

Admission once meant four files that hash to what a receipt says. Anyone can write all four, and a
hand-made record with a nine-key receipt, a `{}` evidence manifest and a report reading "hand made"
passed `make_settled.sh`, finalize and publish. `verifier_contract.verify_record` now ends in
`run_verifier.readmit_record`, so the runner's refusal and replay, `integrate_round.py`,
`make_settled.sh`, `make_diff_packet.py` and finalize all re-admit a record exactly as the runner
admitted it: the runner's exact receipt (`RECEIPT_KEYS`) bound to the call, the snapshot
`phase1/quorum.json` binds, the frozen packet hashing to the receipt's `packet_sha256` with the
runner's prompt, the full output validation of the promoted tree with the receipt's hashes and
source joins, and the call's retained model-identity evidence. A hand-made record is refused
whatever its hashes, and so is every record if the runner cannot be imported. Operating-system
metadata files in a promoted tree (`.DS_Store`, `Thumbs.db`, `desktop.ini`, `Icon\r`, `._*`) are
ignored; any other extra file breaks the closed set. A Phase-1 re-run that binds a new snapshot
makes every earlier verifier record inadmissible.

The routes that wrote such a record are closed. `lifecycle_journal.py append-event`, `append-kv` and
`append-batch` (and so `log_event.sh`) refuse, under the journal lock and before appending, any
lifecycle event that names `role_class verifier` or an invocation already journaled as one. The
lifecycle flags of `record_invocation.py` (`--reserve`, `--provider-request`, `--inference-start`,
`--response-received`, `--cache-reuse`, the positional record) refuse the same, and `record_terminal`
refuses an unreserved verifier provider call. `record_invocation.py --correct <audit-dir>
<invocation-id> <correction.json>` still works. `INSTALL.md` now shows that real syntax, with
`{"set": {...}, "reason": "..."}`; it used to show `--correct <invocation-id> <field> <value>`.

### The runner's own admission holes

- Verdict evidence and evidence-manifest `source_bindings` must join the snapshot exactly, both
  path and hash strings. A file absent from the deposit with a null hash used to compare equal and
  was admitted as a CONFIRMED's only evidence.
- A malformed JSON shape (a list as a source path, a missing key) is an invalid sample, rule
  `result_json`, and is resampled. It used to escape as a permanent failure (exit 2, no resample).
- REPORT.md must have exactly one `## Hypotheses tested` section and no verdict block outside it; a
  second section could contradict the admitted verdict.
- A focused repair adopted after a kill is validated against the `SCIENTIFIC_PROJECTION.json` its
  source attempt froze (`RoleTargets.freeze_recovered_repairs`, verifier only). It used to be
  promoted with no freeze, so a repair could flip REFUTED to CONFIRMED with no new provider call.
  The auditors keep the 2.6.3 recovery path.

### A lost receipt is rebuilt by re-running the same command

The runner journals the completed terminal row and then writes the completion receipt. A kill
between the two left a completed row that no gate admitted: every batch was refused, and
`integrate_round.py` stopped. Re-running the same command now finds the batch's own completed row
(`BatchPlan.recover_receipt`), rebuilds the receipt from the retained response with no provider
call and no budget, re-admits it and records a `cache_replay`; `--dry-run` shows
`would_rebuild_receipt`. Correcting such a row to `legacy_record` still dead-ends the batch ("already
has a non-completed terminal record"), so every remedy text now says never to.

### An inadmissible record fails closed, but no longer costs the settled cache

The runner refuses every batch and `integrate_round.py` stops (`FATAL: verifier record is not
admissible`) while any completed verifier record in the ledger does not re-admit, because an unknown
record could hold a REFUTED for any claim. `integrate_round.py` now prints the remedy in order:
re-run that batch's own `--findings` command to rebuild a lost receipt; otherwise restore the
record's files to their recorded hashes; only for a genuinely tampered or foreign record, correct
it to `legacy_record` with the real `--correct` syntax, never for a lost receipt. `make_settled.sh`
used to abort the whole run, losing every auditor-raised fact over one unrelated record; it now
rejects only the rows that cite a verifier record as a confirmation, with the reason, and settles
the rest.

### A verifier record that does not count rejects its row

`make_settled.sh` dropped a confirmed-outcome verifier record that failed `confirmation_problem`
and still promoted the row when two other families confirmed it. The fact then omitted a record the
registry row carried, and `make_diff_packet.py` refused the whole `SETTLED.json`. A row is now
rejected, with a `verifier:` reason, whenever a confirming verifier record on it does not count.
`integrate_round.py apply` drops a verifier record already on a row, and gives the reason in
`RESPONSE_notes.md` and `ADJUDICATION.md`, in three cases: its call was superseded
(`legacy_record`), a Phase-1 auditor raised or independently found the row, or the claim was
edited. A superseded call is never bound, and a superseded REFUTED or UNVERIFIABLE that still blocks
the current text is listed. Two states still stop `make_settled.sh`, under the 2.6.3 rule for every
reviewer: a record citing a call that ended after the latest integration, and a record citing a call
corrected to `legacy_record` (the next `apply` drops it).

### Finalize checks every verifier row, and counts humans by role class

- Finalize lists each verifier call apart from the five auditors (`compliance.verifier_calls`,
  `verifiers_ok`, emitted only when a verifier row exists). It now also checks every verifier row
  that did not complete (failed, timed out, cancelled) for a `verifier_<12 hex>` role, round 0 and a
  non-critic family. It used to check only completed calls, although `verifiers_ok` promises every
  call meets the contract.
- The human-verification roll-up in `comment.json` recognises the verifier by `role_class`: a
  reviewer id is a model only when it is the role of a `role_class verifier` call in the run. A
  first cut excluded every `verifier_` name, which would have counted a person code such as
  `verifier_1` as a model and changed `comment.json` for an audit with no verifier row.

### The contract has one definition, and it is inert without a verifier

`scripts/verifier_contract.py` holds every verifier rule as pure, provider-free functions. The
rules are eligibility, the batch role name, the route (with its fallback for old snapshots), record
admission, claim-hash binding and the one-verdict rule. The runner, `integrate_round.py`,
`make_settled.sh`, `make_diff_packet.py`, `record_invocation.py` and `finalize_run.sh` all call the
same functions, so the producer and the gates cannot drift apart. Every new branch keys on
`role_class == "verifier"`, never on a role name: legacy rows named `operator_verifier` or
`auditor_operator_verification` keep their `other`/`auditor` class and `legacy_record` status. With
no verifier row, every output is byte-identical to 2.6.3 except in the two cases in the next
section: a Phase-1 start above three under a raised `I4R_MAX_ATTEMPTS`, and a critic-path record
that started in the same second as a critic re-pin. Three audits were running on these scripts when
2.6.4 shipped.

### Two fixes that reach audits with no verifier row

**A Phase-1 start above three wedged the audit.** `run_phase1.lifecycle_started` refused any attempt
above 3, although the journal allocates up to `I4R_MAX_ATTEMPTS` (at most 20). With the knob
raised, a legitimate fourth start was journaled and its response written, and then the runner raised
"lifecycle did not allocate a valid 1..3 inference attempt", leaving status.json at
`provider_requested`. The strand guard then refused every later run, and `--recover-stranded` could
not clear it. The check now uses `lifecycle_journal.max_attempts_per_role()`; legacy ledgers keep 3.
This reaches the auditors too, but only when `I4R_MAX_ATTEMPTS` is above 3.

**A record that started in the second of a critic re-pin was judged on the new route.** Found
while validating 2.6.4: the critique driver's re-pin scenario (`tests/test_run_critique.sh`, step
[11/11]) failed intermittently. `record_invocation.historical_route_for` compared whole-second
times with `<`, so a failed probe or unfinished attempt that started in the second the re-pin was
journaled was checked against the new route and rejected ("recorded request violates pipeline
policy"). It now uses `<=`. The rule only adds an allowed historical route, so it can turn a
refusal into a pass, never the reverse; the same comparison decides whether an audit re-pinned by
`repin_critic_route.py` validates at finalize.

### The policy names the verifier, and the drift check pins every new leaf

`config/pipeline-policy-v2.json` gains two things. The first is `models.post_fan_out_verifier`, the
auditors' route with `must_equal_auditor_family: true`. The second is a top-level
`post_fan_out_verification` section. That section holds the runner and contract module, role class,
role pattern, round 0, eligibility, the Phase-1 runtime, admission and repair contracts by
reference, the framing, packet fields, withheld inputs, verdicts, the `claim_sha256` binding,
one verdict per claim, the timing rule, and zero default sessions. Audits initialised before 2.6.4
keep their frozen snapshot, which has neither key. The verifier then falls back to the single route
their five auditors share, and refuses if there is none.

`config/check_policy_drift.py` pins all 24 new leaves. It also:

- cross-checks the verifier route against every auditor's, so moving the auditors without the
  verifier fails;
- joins the section to `verifier_contract.py`, covering role class, role pattern, round, verdicts,
  and both the declared and the fallback route;
- joins the new verifier verdict table in `reference/adjudication_guide.md` to
  `VERDICT_OUTCOME`;
- requires the runner and the contract module to ship, and requires SKILL.md and INSTALL.md to
  name `run_verifier.py`.

A seventh negative control moves the verifier to the critics' family. That is the tempting mistake:
it would let one family confirm a row twice. The 2.6.3 checker cannot tell that drift from the
correct policy; both produce the same 24 unpinned-leaf errors. The SKILL.md summary gains one
generated verifier line and a short paragraph on where a family-A confirmation comes from, the
command, when to run it, re-admission, the one-verdict rule and the lost-receipt rule. SKILL.md is
now 4,369 words of 4,500. `reference/adjudication_guide.md` adds the rule that a claim edit
after a REFUTED or UNVERIFIABLE verifier verdict must change substance and be made through the
round's decisions, so it shows in `RESPONSE_notes.md`: the claim-hash key cannot tell a real
correction from a cosmetic one.

### The package check failed on a stray `.Rhistory`

R writes `.Rhistory` and `.RData` into the directory it starts in. An empty `.Rhistory` appeared in
the skill root after the 2.6.3 ZIP was built, and `build_package.py --check` then failed on an
unchanged package with `missing=['i4r-audit/.Rhistory']`. `build_package.py` now ignores both files
by exact name. One set, `EXCLUDED_NAMES`, now serves the inclusion test and the symlink refusal,
which used to repeat the list, so the two cannot disagree. The stray file is left in place; it no
longer matters.

### Also shipped unchanged

`scripts/test_run_critique_runtime.py` carries an unused helper, `_scientific_value`, added on
2026-09-23 after the 2.6.3 ZIP was built. Nothing calls it. 2.6.4 does not touch the file, but it
differs from the copy in the 2.6.3 package.

### Tests

Counts are from running each module on its own. Two modules hold goldens that compare against the
2.6.3 scripts and run only when `I4R_V263_SKILL` names a 2.6.3 skill tree; otherwise they are
skipped.

- `tests/test_264_verifier_runtime.py` (33 tests) runs the real runner as a subprocess after a real
  Phase 1 with the fake Claude CLI:
  - `VerifierRunsUnderThePhase1Contract` (3): one admissible family-A call with Phase 1 left
    byte-identical, the blind packet and the sandbox denials, replay from the receipt with zero
    starts;
  - `VerifierRefusals` (3): every refusal with no call, no reservation and a named remedy, a
    preflight failure, a pre-metadata-2.1 audit;
  - `VerifierRetryAndRepair` (6): the focused repair bound to the batch input, a repair that changes
    a verdict, a missing hypothesis, budget exhaustion and the `I4R_MAX_ATTEMPTS` knob, transient
    rejections and the pre-inference circuit breaker, a REFUTED verdict;
  - `VerifierDryRunAndRecovery` (2): `--dry-run`, and a strand guarded then recovered without charge;
  - `Phase1CeilingKnobIsNotWedged` (1): a fourth auditor start under the knob;
  - `AuditorInputsArePinnedTo263` (3): the auditors' role input hash, probe key, sandbox and targets
    are the 2.6.3 values, and role identity keys on the packaged names only;
  - `VerifierReviewFindings264` (10): exact evidence and binding joins, an unhashable evidence path
    resampled, a self-contradicting report resampled, a lost receipt rebuilt by re-running the
    command (a kill between the terminal row and the receipt), a recovered repair re-proved against
    its frozen projection and one with no projection, the per-attempt denials, the tree records
    denied only inside a recognised tree, the tree root mirroring `tree_layout.sh`, and usage
    errors exiting 1;
  - `VerifierRecordsAreReadmitted` (5): a real runner record re-admitted and every forgery refused,
    OS metadata in a promoted tree ignored, and the fixture helper checked in a real Phase-1 audit,
    without one, and through finalize.
- `tests/test_264_verifier_confirmation.py` (18 tests) drives the shipped scripts end to end
  (`integrate_round.py` plan and apply, `approve_settled.py`, `make_settled.sh`,
  `make_diff_packet.py`, `renumber_findings.py`, `generate_finding_artifacts.py`):
  - `VerifierConfirmsARowRaisedAfterFanOut` (2), `RenumberingAfterBindingKeepsTheBinding` (1);
  - `VerifierNegativeControls` (12): a different claim; a claim edited after verification; a
    REFUTED and a second call on the same claim; a REFUTED corrected to `legacy_record` that still
    blocks; a bound record whose call is later superseded, dropped with its reason; the critic
    family; a call after the latest integration; an auditor-raised row (rejected, and the diff packet
    builds); the legacy `other` and `auditor` shapes; a hand-made record; tampered result bytes;
    one inadmissible record on another claim rejecting only the rows that cite a verifier;
  - `NoVerifierIsInert` (1);
  - `NoVerifierIsByteIdenticalTo263` (2), the golden: rounds 1 and 2 (literal rebinds and a claim
    edit), two `make_settled.sh` runs, the round-2 and round-3 diff packets and
    `generate_finding_artifacts.py --apply`, compared file by file with the 2.6.3 scripts. Its guard
    is now content-based: it runs when the named tree's `make_settled.sh`, `integrate_round.py` and
    `make_diff_packet.py` do not mention `verifier_contract`, so it runs against the live 2.6.3 tree,
    which ships an inert `verifier_contract.py` and made the old file-presence guard skip it.
- `tests/test_264_verifier_records.py` (20 tests) covers records, the journal and finalize through
  `make_audit`, `finalize_run.sh` and `publish_completed.sh`:
  - `VerifierRecordsTest` (9): the call listed apart from the five auditors, a confirmed verifier
    promoting a post-fan-out row, the critic family, a missing receipt, `role_class auditor` (still
    an extra auditor), a round other than 0 refused before any append, the claim scope, the 2.6.3
    compliance keys without a verifier, and the A23 legacy shape;
  - `HandMadeRecordsAreRefused` (4): a hand-made record at finalize and cited on a row, and the raw
    CLI and `record_invocation.py` lifecycle routes refused, with `--correct` still working;
  - `CliAppendInvariance` (1, golden): CLI appends without a verifier are byte-identical to 2.6.3;
  - `NonCompletedVerifierRows` (3): each contract violation of a failed verifier row, a failed row
    on a critic family, and a failed call made as the runner makes it (not a violation);
  - `HumanVerificationRollupTest` (3, one golden): the verifier is not a human, a `verifier_`-named
    person code with no verifier row stays human, and `comment.json` bytes equal 2.6.3's.
- `tests/test_264_policy_and_package.py` (8 tests; the whole module fails on 2.6.3):
  - `DriftCheckRejectsVerifierDrift` (5) plants 23 policy drifts, 6 documentation drifts, 3
    contract-module drifts and 2 missing files on disk in a copy of the skill tree. The real drift
    check must refuse each by name, and the unmodified copy passing is the negative control.
  - `PackageIgnoresStrayRFiles` (3) runs the real builder on a mini skill. `.Rhistory` written after
    the build must keep `--check` passing, while a real new file still fails it. Stray R files
    present at build time must stay out of the ZIP, while `.Rprofile` still ships. Symlinked R files
    must be skipped, while any other symlink is still refused.
- `tests/test_264_repin_same_second.py` (4 tests), `SameSecondRepin`: a record started in the
  re-pin second and one started before it get the old route; one started after it, and a completed
  review, do not.
- `VerifierPromptNamesValidatorRules` in `tests/test_prompt_contract.py` (6 tests): every rule in
  `run_verifier.VERIFIER_RULES` is stated in the prompt, every rejection names a registered and
  reachable rule, only structural bindings are repairable, an unregistered rule cannot be raised,
  the review's mutations are rejected on a registered stated rule, and an unexpected shape is an
  invalid sample, never a crash.
- `bash scripts/finalize_run.sh --self-test` gains four verifier controls: a run with no verifier row
  keeps the 2.6.3 compliance keys, a call recorded as the runner records it finalizes clean, the
  same call without its receipt is not admitted, and a hand-made record (the A23
  `make_record.py` shape) is refused as not the runner's admitted output. The self-test now imports
  `tests/verifier_fixtures.py`, so it needs `tests/` shipped with the skill, as the package does.
- Support: `tests/verifier_fixtures.py` (new) records a verifier call exactly as the runner does
  (real lifecycle API, frozen packet, validated tree, the runner's receipt) and makes the malformed
  variants and the hand-made shape; `tests/fake_claude_phase1.py` gains verifier scenarios; and
  `tests/test_release_pipeline.py` gains an opt-in `before_registry` hook on `make_audit`, which
  leaves the fixture byte-identical when unused.

## 2.6.3 — 2026-09-23

### A fifth author joins the published byline

`reference/house_citations.md` now lists a fifth author on every comment, with the
same affiliation as the rest of the team (Institute for Replication, Department of Economics,
University of Ottawa). Correspondence is unchanged. The byline has no other source in the skill, so
the coordinator prompt and `front_matter.md` pick it up from this file. The version bump makes
`init_run.sh` refuse stale installed copies, so every machine picks up the new byline.

## 2.6.2 — 2026-09-23

Six defects found by audit A28, which then
published on this code: converged after three rounds, `compliance_pass: true`, every verification
check passing, the PDF in `comments/`. Each fix has a test that builds the condition.

### `integrate_round.py` could not set a finding conclusion-changing

A row with `changes_conclusion: true` must carry `changes_conclusion_which`, and `apply` never copied
that field from the decisions file, so the documented path always failed validation. It is now
copied, and the up-front approval check names it when missing.

### A round result logged after integration moved the round's integration time

`round_result` sets `integrated_at` from its payload, or from its own event time when the payload has
none. On an audit whose journal already recorded `round_integrated`, logging a round result
afterwards (as the docs instruct) silently replaced that time; the settled cache binds it, and
`make_diff_packet` later failed with "Round-1 completion/integration/consistency gate is invalid",
naming neither cause. A NEW `round_result` now inherits the journaled time and refuses a different
one with the value to use; replaying existing journals projects exactly as before, so published
audits still verify.

### The coordinator wrote review status into the claim text

2.5.1 told the coordinator to record a candidate conclusion-changing finding "in ADJUDICATION.md and
in its claim text". The claim is projected verbatim into Appendix A and the claim cross-reference, so
after both model families confirmed the finding, Appendix A still called it "a candidate ... requiring
a second model family" -- a blocking round-2 item. The prompt and the draft hint now say
ADJUDICATION.md only.

### The release neutraliser missed an audit path split across string literals

An auditor's evidence script wrote the audit path as three adjacent literals, so the full audit root
never appeared and staging blocked on the operator's own account name. Staging now also rewrites the
discipline TREE root (recognised by `tree_layout.sh`, trailing separator required) to `<tree>`,
disclosed in the neutralisation note; a sibling such as `<tree>2/` and any other account path still
block.

### The evidence-gap trigger fired on merged findings

A high-impact row merged into another finding (or excluded as a tested negative) raised
`high_impact_evidence_gap` in a later round the critics were correctly not routed to. MERGED and
EXCLUDED rows are skipped; an INCLUDED row still triggers.

### Operator note: run each later round's post-delta gates before the next round starts

The round-N receipt binds the round-N packet, i.e. the state at round-N ENTRY. Audit A28 ran
them only at the end and had to produce the round-2 receipt on a byte-exact replica of the
round-2-entry state (preserved before-files, journal cut at the packet's position, settled cache
rebuilt to the recorded hash). Run `run_post_delta_gates.py --round N` right after
`make_diff_packet.py --round N`, and preserve each round's before-files before integrating it.

## 2.6.1 — 2026-09-23

Everything below was found by the first audits run after 2.6.0 -- a teammate's audit on 2.6.0 and
another on a stale 2.2.0 copy -- and each fix is exercised by a test that builds the condition.

### GPT-6 Sol is not available on every ChatGPT account: the default goes back to Terra

The first 2.6.0 audit failed at the critique probe with the provider's words "The gpt-6-sol model
is not supported when using Codex with a ChatGPT account"; on another account Sol is accepted. A
pinned route that some of the team cannot run is not a route, so `config/pipeline-policy-v2.json` is
back on `gpt-5.6-terra` (critics high, adjudicator max). The driver still reads the route from each
audit's own snapshot, so an audit keeps what it was initialised with.

### A journaled way to re-pin a live audit's critic model

That audit was then stuck: its snapshot pinned Sol and nothing could change it without hand-editing
hash-bound files. `scripts/repin_critic_route.py` does it safely: it refuses once any critic or
adjudicator review has completed or a round has reached quorum, refuses while a driver holds a round
lock, keeps the replaced route in `policy.critic_route_history`, archives the old snapshot read-only,
and journals a metadata correction (toolkit hash) plus a note with the reason. Both policy checks --
`record_invocation.validate_record` and finalize's compliance check, which now share one function --
accept a model probe or an incomplete attempt made before the re-pin on the route in force at the
time; a completed review never. `--dry-run` runs every check and writes nothing. Driver scenario 11
covers it end to end: unsupported model, provider message, re-pin, completed round on the new route,
telemetry check, and refusal of a second re-pin after reviews completed.

### The ledger recorded a CLI warning as the reason a call failed

codex-cli 0.156.1 prints "failed to load models cache: missing field ..." to stderr before the real
400, which arrives only as a JSON error event, and the telemetry recorded the warning. The error text
now prefers the provider's error event and drops that warning from the stderr fallback.

### The release gate blocked a clean release on `/Users/tester/`

Same class as `janedoe` in 2.4.9: the privacy auditor's own synthetic fixture path blocked a release.
`tester` joins the fictitious names; `testerman`/`atester` still block (negative control).

### `integrate_round.py apply` names the fields an approved row needs

An approved row must carry `report_section` and `verification_snippet`; the validator enforced it,
the docstring's example omitted both and SKILL.md never said so. The example now includes them, apply
refuses up front naming the missing field(s) with an example, and SKILL.md states it.

### Smaller

- `status.py` matches a run to its reservation through `paper_source.json`, so a run folder whose
  name lost a `?` no longer hides a finished audit's unreleased claim.
- `finalize_run.sh` and `build_docx.sh` sweep their own transaction directories older than an hour
  before starting; the synced folder had put removed ones back with their contents.

### Not fixable from the skill

A copy older than 2.4.11 cannot enforce the version pin on itself; one teammate's audits still run on
2.2.0 and need the current ZIP installed.

## 2.6.0 — 2026-09-22

### Critics and the targeted adjudicator move from GPT-5.6 Terra to GPT-6 Sol

`config/pipeline-policy-v2.json` now routes the four critics, the critique model probe (effort high)
and the targeted adjudicator (effort max) to `gpt-6-sol`; Sonnet 5 stays on the auditors and the
coordinator, so the two-family rule is unchanged. The identifier is the one the installed Codex CLI
lists; it could not be exercised live because the workspace was out of credits when the change was
made, so the first Sol round should be watched.

`run_critique.sh` no longer hard-codes the route. It reads the critic and adjudicator model and effort
from the audit's own `pipeline-policy.snapshot.json` (falling back to the shipped policy only when a
workspace has none), and the `CRITIC_MODEL`-style knobs are checked against that snapshot. An audit
initialised under Terra therefore finishes under Terra and validates; a new audit picks up Sol. The
drift check, the finalize and record self-tests, the driver tests and every fixture were moved to the
new identifier; the SKILL.md policy block was regenerated from the policy.

## 2.5.1 — 2026-09-22

Three defects surfaced by the 2.5.0 verification run (audit A28), which is
paused at critique round 1 pending Codex credits; its journal records the mid-run gate changes.

### A pre-Terra row can never be conclusion-changing, and the coordinator was told otherwise

The coordinator's first attempt on audit A28 ran 49 minutes and was rejected on one row:
"confirmations must span two known model families". Three Sonnet auditors had independently
confirmed that the paper's contrasts were coded differently from what the Methods
describe, the draft summary said "set changes_conclusion
only with two independent confirmations", and the coordinator, having three, set it. The validator's
actual rule needs two model FAMILIES, and every Phase-1 confirmation is Sonnet, so the flag is
structurally impossible before round 1 -- a rule enforced in one place and misstated in another.
`build_draft_registry.py`'s hint and `run_coordinator.py`'s prompt now state the real rule and where
the second family comes from; SKILL.md says the same in its pre-Terra sentence. The rejected row is a
test fixture: the validator refuses it as written and accepts it with the flag left false.

### The verification join was checked field by field and stated nowhere

Attempt 2 copied every invocation id and every report hash exactly from the Phase-1 quorum and was
rejected on the first row: `verifications[].model_family` read "Sonnet" (attempt 1 had written
"claude-sonnet-5") while the ledger's canonical string is "claude", and nothing the model reads
carries that string -- the quorum bindings it copies from have no family field. The prompt now
states the join field by field: reviewer role, invocation id and report path/hash copied verbatim
from the quorum, and `model_family` exactly `claude` for every Phase-1 auditor, never a model name.
A dry run of attempt 2's output with only that string corrected passes every remaining runner
check, so the two lost attempts (49 and 55 minutes) were both this class of defect.

### A failed provider call now says what the provider said

Round 1 stopped at the model probe with "model probe failed (permanent); no critic calls were
started". The classification (quota) was right and the provider's words -- "Your workspace is out
of credits. Ask your workspace owner to refill in order to continue." -- were on disk in the sealed
`failure.log`, but nothing printed them; the operator re-ran codex by hand to learn them. The probe
and critic failure lines now carry the error class, the provider's last error message and the path of
the log it came from. The driver test for permanent failures pins the message.

## 2.5.0 — 2026-09-22

### The tree has four folders a reader can find things in

Nobody could find a finished comment. Each lived eight files deep in `Completed reports/<Journal>/
<Paper>/` beside its metadata, the inputs sat in journal folders at the root beside gigabytes of
working audit trees, and the same folder name meant three different things. The tree is now:

```
data/<Journal>/<Paper>/        inputs (article PDF, package); read-only, never written by the skill
runs/<Journal>/<Paper>/audit   the working audit
comments/<Journal>/<Paper>/    the published comment (PDF, DOCX)
metadata/<Journal>/<Paper>/    the published record (six files)
old_runs/                      everything produced before this date, by its old path
```

`scripts/tree_layout.sh` is the one place that knows the layout; `setup_workspace.sh`,
`reserve_paper.sh`, `publish_completed.sh`, `finalize_run.sh`, `init_run.sh` and `status.py` all
source it instead of counting `dirname`s on their own (that rule lived in five places and had
drifted in each). The legacy form is still recognised everywhere, so the six audits teammates had in
flight on 2026-09-22 can be finished and published into the new folders by the same scripts.

- **`scripts/new_audit.sh "data/<Journal>/<Paper>"`** starts an audit: it derives
  `runs/<Journal>/<Paper>/audit`, picks the article PDF (never a comment or supplement) and the
  package when the folder makes that unambiguous, drops `?*[]{}\` from the run folder name while
  recording the paper's true name, and calls `setup_workspace.sh`. An ambiguous folder is refused
  with the candidates named.
- **`setup_workspace.sh` refuses an audit dir under `data/`** and derives the paper folder and the
  reservation root from the layout.
- **`publish_completed.sh` splits the eight files**: PDF and DOCX into `comments/`, the rest into
  `metadata/`, both renamed into place under one lock, both rolled back together on any failure
  after the renames. Its optional second argument is now the tree root. `reserve_paper.sh` treats a
  paper with a `comments/` or `metadata/` folder (or a legacy `Completed reports/` one) as published.
- **The registry restarted.** `registry/` holds only `_spec/`; the 29 audits filed before today are in
  `old_runs/registry/`. Reservations were reset except the six live claims.

The migration itself moved 358 input entries into `data/`, 60 result folders and files into
`old_runs/`, and left six live teammate audits in place with their inputs copied. Every operation is
listed in `old_runs/MIGRATION_MANIFEST.json`.

## 2.4.11 — 2026-09-17

Three audits were started after 2.4.10 shipped. One was published; its journal shows what the
skill still let happen, and two of the three ran from copies that were not 2.4.10 at all.

### The running skill must match the tree's canonical copy

The published audit A29 was run on another machine from a
**2.2.0** copy of the skill; a third audit ran on a Linux host from a copy with no VERSION file. Both
stamped themselves faithfully and then met defects fixed releases earlier: the `mkdir` publish lock
the synced folder resurrects, a filing into the skill-local registry, a compliance failure with no
named cause. The discipline tree syncs the canonical skill to every machine at
`<tree>/skill/i4r-audit-unified/i4r-audit`, so `init_run.sh` now compares its own `VERSION` with that
copy's when the audit sits at `<tree>/<journal>/<paper>/audit`, and refuses a mismatch or an
unversioned copy with both versions and the remedy in the message. `I4R_CANONICAL_SKILL_DIR` moves
the comparison target; `I4R_ALLOW_SKILL_VERSION_MISMATCH=1` downgrades the refusal to a warning and
the stale stamp is still recorded. Outside such a tree nothing is checked. A copy older than 2.4.11
cannot enforce this on itself, so the two stale copies still need reinstalling by hand.

### Filing into the skill-local registry is refused

`finalize_run.sh` announced the skill-local fallback and then took it. On audit A29 the row
landed in the skill checkout, the Completed-reports set was published against it, and both had to
be re-filed and moved aside by hand (`.superseded-skill-local-registry-filing-…`). When the
programme registry cannot be derived, finalize now exits 2 naming `--registry <tree>/registry` and
`--no-registry`; fixtures set `I4R_ALLOW_SKILL_LOCAL_REGISTRY=1` to keep the old behaviour.

### The open-phase violation names the command that closes it

The same audit's first finalize reported `compliance_pass: false` because the release phase was
still open. The violation read "phase 'release' did not run to completion" and stopped there. It now
says the journal holds no closed start/end pair and gives the `log_event.sh phase_end phase=release`
command; the finalize self-test drives a run with the release phase left open and checks for it.

### `RELEASE_REJECTED` names the step that refused

Two staging directories were left with a one-word marker before a third attempt passed. Each `== … ==`
banner in `stage_release.sh` now records the step it opens, and the marker names the step that was
open when staging stopped. The gate's own reason still goes to the terminal, not into the release
directory.

### Observed, not changed

Transaction directories (`.finalize-tx.*`, `.document-build-transactions/build.*`) were left behind
on the other machine with their full contents, although both scripts remove them on exit. The run on
this machine left none. They are inert: nothing reads them, staging copies named files only, and the
privacy scan inspects the release directory. They are Dropbox artefacts to delete by hand.

## 2.4.10 — 2026-09-13

The two items 2.4.9 recorded as observed and left open.

### The coordinator retries a dropped connection itself

On the run of audit A30 the coordinator's first attempt ran 39 minutes and died on `Connection closed
mid-response`. The runner classified it correctly (transport), recorded the failed attempt, and
exited 2 — and an unattended pipeline then sat on a stalled audit until someone re-ran the command.
`run_coordinator.py` now loops in the same invocation on a transport- or transient-class failure:
each attempt is still its own reservation and its own terminal row, the attempt budget
(`I4R_MAX_ATTEMPTS`) still bounds the total and `next_attempt()` refusing is still a permanent stop.
Only those two classes loop; authentication, quota, model, schema and provider-safety refusals stop
at once, as before. `I4R_COORDINATOR_TRANSIENT_RETRIES` caps the in-invocation retries (default 2)
and `RETRY_BACKOFF_SECONDS` (default 30) scales a linear backoff. The fake CLI gains a
`transport_then_success` scenario whose call counter lives in a state file, so the test proves the
runner retried rather than a second invocation succeeding; the negative control shows a provider
error still stops after one attempt.

### The publish lock is a file, not a directory

`publish_completed.sh` locked with `mkdir`. The EXIT trap removed the directory and the synced
folder the tree lives in resurrected it empty, so every published paper left a
`<paper>.publish.lock/` beside its report — three of them, removed by hand. The lock is now a file
created `O_EXCL` holding the owner's pid. The staleness rules are unchanged: a live owner blocks; a
dead owner, or no owner and a minute of age, is broken with a warning; a lock created seconds ago
with no pid is refused. A legacy directory lock is judged by the same rules and removed. Tests cover
a clean publish leaving nothing behind, a dead-owner lock in both forms, and a live-owner lock still
refusing.

## 2.4.9 — 2026-09-13

Both fixes come from audit A30, run in full on 2.4.8 (published,
`compliance_pass: true`, every verification check passing). The run also confirmed
in production that the 2.4.8 provenance stamp, the waiter's outcome vocabulary, the adjudicator's
progress lines, the context-file provider-safety scan and the post-publication receipt check all
behave as designed.

### A Phase-1 role lost two paid attempts to a word

`auditor_text_vs_data` was rejected twice with "role report findings omit evidence/benign
explanation/impact/remedy". Both reports carried *Benign explanations tested*, *Impact* and
*Remedy* labels and substantive, source-bound evidence — they never used the word "evidence" inside
the Findings section. The prompt listed four *elements*; the validator demanded four *tokens*; the
message named none. The prompt now spells the four labels and says the check is for those words;
the check is a named function (`findings_elements_missing`) whose message names the element that is
absent. Attempt 3 had passed only because the word happened to appear.

### The release gate blocked a clean release on `janedoe`

The privacy auditor's own detector script carries `path /Users/janedoe/data.csv` beside
`jane.doe@example.com` and a fake SSN as its known-present self-test string, and `stage_release.sh`
refused the whole release for that line. `_PLACEHOLDER` — the exemption for an account segment
that is nobody — now includes the standard fictitious names (`janedoe`, `johndoe`, `jdoe`, `test`,
`testuser`, `alice`, `bob`). A real-looking account still blocks; the negative control is in the
test. This change was made mid-run, after the audit's `gate_set_version` was stamped at init; the
staged release carries the gate that actually ran.

### Observed, fixed in 2.4.10

The coordinator's first attempt ran 39 minutes and died on `Connection closed mid-response`
(transport class). The runner recorded the failure and exited; each further attempt needed a manual
re-run. The in-invocation retry, and the `.publish.lock` directory the synced folder resurrected
after every publication, are both fixed in 2.4.10.

## 2.4.8 — 2026-09-12

An adversarial sweep of the skill itself — fourteen finders, one per subsystem, each required to
reproduce its claim by executing code, then two to three independent refuters per claim — judged
65 claims and killed 13. This release fixes the 52 that survived. The dominant class is the one
this changelog keeps recording: a rule enforced in one place and expressed in another, or nowhere.

### Blockers

- **One bad row wedged an audit permanently.** `_append_locked`'s pre-commit proof ran
  `project_run()` only; `project_all_locked` then ran the metrics projection outside any proof. A
  terminal row that the first accepted and `aggregate()` could not read was fsynced into the
  append-only journal, after which every write, projection, recovery and the finalize commit died
  on the same `KeyError`. The proof now runs every projection (`prove_projectable`), and the six
  invocation-lifecycle event types are no longer accepted by `log_event.sh`'s generic key=value
  path, which had no required keys for them. (The public record wrappers deliberately do NOT
  schema-validate: they synthesize lifecycle for partially-shaped rows, and the proof is the guard.)
- **The 2.4.6 sandbox fix was half the bug.** A correct denial of an allowlisted *verb* on a path
  nothing grants (`mkdir -p "$TMPDIR/x"`, `cat /Users/<author>/.Renviron`, `find / …`) still paused
  Phase 1 as a broken sandbox. `allowlisted_denials` now reads the attempt's `claude_settings.json`
  and attributes a denial to the sandbox only when every path operand lies inside a granted root.
- **Intake died silently on large deposits.** `find | head` under `set -euo pipefail` took SIGPIPE
  at ~650 top-level directories or ~330 code files: exit 141, no message, after the reservation
  and before the package hash. `-print -quit`, and a temp file for the listing.
- **The round waiter rejected two of the driver's five terminal outcomes** —
  `quorum_complete_adjudication_failed` (the state the driver made distinct on purpose) and
  `paused` — as a malformed status file, exit 4. It knows all five now and says what each means.
- **The four audit-context files bypassed the provider-safety scan.** `PAPER.txt`, which SKILL.md
  asks to carry the package location, was mounted into the snapshot unscanned while
  `recover_coordinator.py` refused the identical bytes. The runner scans them now.
- **Both printed remedies after an adjudication failure dispatched nothing.** `reserve_invocation`
  refused any dispatch into a round past `running` and, on refusal, appended
  `round_authorization_required` — regressing a finished round to `awaiting_authorization`, the run
  to `paused`, and keeping the attempt the driver had already debited. A round at
  `quorum_complete` or `integrated` with a live grant is now **reopened** in the same locked append
  (`round_start` with `reopened_from`, keeping its first `started_at`); the driver returns a debit
  the journal refused (`release_attempt`). This is also what made `I4R_FORCE_RERUN=1` a no-op on a
  metadata-2.1 round. The driver suite gained a metadata-2.1 scenario that runs the production
  sequence: `adjudicator_invalid`, then the printed rerun, then a forced rerun.

### Release gates

- Base64 is not a participant identifier: one rendered figure in a staged `.ipynb` was ~1170
  "distinct identifiers". Data URIs and long base64 runs are stripped before the identifier scan,
  in the module and in `stage_release.sh`'s mirrored detector.
- The 2.4.7 neutraliser read UTF-8 text and silently skipped what it could not decode, while the
  detector fell back to latin-1 and NFC-normalised — so a cp1252 script naming the audit root was
  skipped, then blocked. It now reads bytes with the detector's decoder, matches NFC and NFD
  spellings of the root, and writes bytes back, so CRLF survives.
- `inspect_docx` flattened visible parts with no separators, so a 25-row participant table became
  one line and one token and neither identifier rule could fire on a DOCX while the same table in
  Markdown was rejected. Rows are lines and cells are tabs now.

### Journal and log_event.sh

- `review_scope` lists use `_listify` like every other list, so `changed_claim_ids=F2,F7` is two
  ids, not five characters. `parse_kv` keeps `nan`/`inf` as strings and both writers pass
  `allow_nan=False`. Phase `notes` are projected; the legacy `<2 s → null` substitution that
  contradicted the schema is gone.
- The lock documentation, the `LOCK_TIMEOUT` knob and self-test #12 all described a directory
  lock the metadata-2.1 path never uses. `journal_lock` now polls `LOCK_NB` and refuses after
  `LOCK_TIMEOUT` with a message naming the live holder; the header describes the fcntl lock; the
  self-test holds the real lock; a refused write exits 2 on both paths.

### Phase 1, coordinator, critique

- Three `repairable=True` faults byte-froze exactly the bytes a repair would have to change
  (report/result leaks, unbound source hash) — a guaranteed-fail paid call. They are
  non-repairable and say why. A `./`-prefixed evidence path gets its real diagnosis and is
  repairable; the prompt warns about the spelling. The exhaustion message interpolates the live
  ceiling and names `I4R_MAX_ATTEMPTS` and `--recover-stranded`.
- Under 2.1 the coordinator's idempotent reuse was unreachable because `context/run.json` moves
  with every journal event. `completed_for_input` compares the packets file by file and reuses
  the completed call when only `run.json` differs; a changed report is still drift, and the refusal
  now names what changed. The strand guard names a remedy for both strand classes.
- `overall.blocking_count`/`publishable` derivation, the 20-character `overall.summary` floor
  and the set-semantics of every array are stated in `prompts/contract.txt`; `nonempty()` reports
  the bound it applied. The targeted adjudicator announces every attempt, retry and exit, and
  writes a failure record like the critics do. The three snapshot diagnostics say what actually
  happened (mismatched manifest fields, a malformed hash, or a genuinely missing manifest).

### Intake and provenance

- Both provenance stamps fed an unquoted `$(ls …)` through word-splitting, so every audit on a
  machine whose paths contain spaces recorded `gates-e3b0c442` — sha256 of nothing. One
  `gate_files_digest` covers every gate file for both stamps and refuses an empty stream.
- The glob-metacharacter guard tests the absolute audit dir; `I4R_SKIP_RESERVE=1` no longer
  discards `I4R_PAPER_DIR` or suppresses `paper_source.json`; `publish_completed.sh` releases the
  reservation under the true paper folder and reports a failed release; `reserve_paper.sh reap`
  holds (instead of crashing on) a claim whose audit has no journal yet.

### Tests, gate and docs

- `test_critique_contract_coverage.py` walks the validators' call graph instead of four
  hard-coded helper names; the 27 rejections that surfaced are classified. `check_policy_drift.py`
  mutates every policy leaf and requires a check to object unless the leaf is in an explicit
  `FREE_LEAVES` list (72 were silently free); its banned needles are word-bounded, so
  `--stopping-reason cap_with_unresolved` no longer trips `--stopping-reason cap`.
- `test_pipeline_ordering_remedies.py` builds its fixture instead of searching the tree for a
  finalized audit; `RealStrandRecoveryTest` is opt-in via `I4R_STRAND_CORPUS`. Both used to
  depend on data outside the artifact.
- `run_post_delta_gates.py` names the failing gate, its log, the partial receipt and the snapshot.
  `renumber_findings.py --also` after a plain `--apply` applies the persisted map instead of a
  silent identity. `build_safe_evidence.py` reads bytes. `project_draft_claim_tags.py` reports the
  real count. The stale-projection refusal blames `approve_settled.py`, which is the cause.
- INSTALL.md: `--recover-stranded` charges nothing; the SKILL.md cap is stated as the checker's
  constant; the env-var section lists `I4R_PROVIDER_TIMEOUT_SECONDS`, `BUILD_DOCX_CACHE_MODE`,
  `I4R_OWNER` and the `init_run.sh` defaults. SKILL.md's session arithmetic is fixed and
  `integrate_round.py` is named where integration is described. `scripts/README.md` moves the two
  Phase-1 accounting utilities to the stale list and stops calling the critique runtime helper a
  misplaced test. CHEATSHEET.html no longer claims the other docs are wrong.

Not fixed, by decision: the `status.py` intake-only marker claim was refuted on verification;
the coordinator has no `--recover-stranded` of its own yet (the guard now names the manual route).

## 2.4.7 — 2026-09-10

Three defects, all at the very end of the pipeline and none reachable from the audit itself. Two
were found by staging the release of a clean audit — they refuse a release that has nothing wrong
with it. The third was found by checking a finalization receipt *after* publication instead of
before, which is the only order in which it fails, and it had been failing on every published
audit.

### The privacy gate read Word's own stylesheet as a list of participants

`inspect_docx` concatenates the raw XML of every part in the archive. That is deliberate and it
must stay: a private path can hide in a relationship `Target`, which never appears as visible
text. But the whole blob was then handed to the identifier detector, so the detector ran over
markup.

On a clean 15-finding comment it reported:

    FAIL: 1 participant-identifier privacy issue(s):
      report/A31_comment.docx: opaque per-row identifier listing (22 distinct identifiers)

Not one of the 22 was an identifier. Eleven were pandoc bookmark anchors — `X` followed by hex,
which the detector's hex exemption misses because of the prefix. Nine were style names carried in
from the reference document (`Heading1Char` … `Heading9Char`). One was the institute's own name.
The threshold is 20, so any comment with enough sections crosses it on document machinery alone
and cannot be released.

The two scans are now separate: private paths are still hunted through every part including raw
markup, and identifiers are hunted only through the parts a reader sees — document, headers,
footers, footnotes, endnotes, comments — with styles, themes, settings and `.rels` excluded.

### A working script could not name the snapshot it had read

An auditor's reproduction script must point at the snapshot, and the snapshot lives under the
operator's home directory, so the script contains an absolute path. Phase 1 already refuses a
role whose `REPORT.md` or `result.json` leaks one; evidence artifacts are not scanned, and could
not usefully be — the script has nowhere else to point at run time, and the file is hash-bound
into the role's evidence manifest, so it cannot be edited afterwards without invalidating the
record.

`stage_release.sh` now rewrites **the audit's own root, and nothing else**, to `<audit>` in the
RELEASE copy of every text file under `work/`, and lists every substitution in
`RELEASE_PATH_NEUTRALISATION.txt`. The rest of the path survives — a reader still sees
`<audit>/phase1/snapshots/<sha>/raw/pkg` — and the audit-side original is untouched and still
matches its recorded sha256.

The first version of this fix rewrote any home prefix anywhere under `work/`, and
`test_statistical_source_private_path_blocks_staging` caught it: that would have silently
neutralised `%include '/home/<someone>/private/input'` in a shipped SAS script. A path that does
not start at the audit root is a different animal — the auditor should not have read from there,
and tidying it away hides that they did. Those still block, as does any private path outside
`work/`, which means a report, a manifest or a gate script leaked one.

The rewrite lives in `check_release_privacy.py` beside the pattern that rejects the path, so the
rule and its remedy cannot drift apart — which is the defect class this changelog keeps
recording.

### Publishing an audit invalidated its own finalization receipt

Found by checking the receipt after publication rather than before. `publish_completed.sh`
journals the filing itself — deliberately, and after the receipt is written and verified, so that
the `run.json` it files a moment earlier does not claim to contain its own filing record. Six of
the receipt's eighteen bound artifacts are projections of that journal, so the one appended row
changed all six:

    FATAL: finalization receipt is stale for invocations.watermark.json

Every published audit was in this state, including both that had been published before this was
noticed, and the message names an artifact that gives an operator no way to guess the cause is
publication. The receipt exists to be checked after the fact; it had never passed one.

The journal is append-only and hash-chained, so a later append does not alter what was finalized.
`check` now verifies those six by **prefix**: it recomputes the watermark file the receipt
recorded to locate the journal position that was bound, requires that position's chain hash still
to be present and intact, and requires every appended row to be one a finished audit may
legitimately gain (`delivery`, `note`, `records`, `package`, `human_effort`). Anything else — a
new round, a second finalize, a metadata correction — is still stale, and every artifact that is
not a journal projection is still compared byte for byte. When the journal has moved on, the
filed registry pair is still required to match its own copies and the live `comment.json`, which
is derived from the findings and not from the journal. The check now says what it tolerated:

    finalization receipt valid for <id> (journal extended after finalization by 1 row(s): delivery)

### Tests

`tests/test_release_privacy_scope.py` (7). Each builds the condition on disk and runs the shipped
code over it. The DOCX test was verified against the pre-fix module and reproduces the production
failure exactly (20 distinct identifiers from markup alone). Four are negative controls: an
identifier listing in visible text is still caught, a private path in a relationship attribute is
still caught, a private path outside the audit root is neither rewritten nor missed, and neither
`data/users/alice.csv` nor a URL is touched.

`tests/test_receipt_survives_publication.py` (4) builds a real audit, publishes it, and then
checks the receipt — the order nothing had exercised. Verified against the pre-fix module, where
two of the four fail. Three are negative controls: a `gate` row appended after filing still
fails, an edit to `report/REPORT_final.md` still fails and is still named, and a rewritten
journal row still fails.

## 2.4.6 — 2026-09-09

Found by running a **data-only** deposit — four SPSS `.sav` files, no code — which is the shape
two-thirds of the remaining papers have and which no completed audit had used. It halted Phase 1
on a false diagnosis.

### A newline made the sandbox guard blame the sandbox for its own correct denial
`allowlisted_denials` distinguishes a denial of a command the policy ALLOWS (the CLI is not
enforcing the rules it was handed — re-sampling cannot help, so stop the run) from a denial of one
the policy FORBIDS (the policy working). It decides on the command's first token, which is only
sound for a single simple command, so `_is_simple_command` rejects anything containing a shell
operator. The marker list held `&&`, `||`, `;`, `|`, `>`, `<`, backtick and `$(` — and not the
newline.

A role sent this as one Bash call:

    mkdir -p "$TMPDIR/audit_work"
    Rscript -e 'library(haven) ...'

Nothing grants `$TMPDIR`, so the sandbox denied it correctly. The guard saw no operator, judged the
call simple, matched `mkdir` against the allowlist, and raised `sandbox_environment_fault`. That
paused the entire Phase 1, refused to spend the role's remaining attempts, and reported the
filesystem sandbox as broken when it was working. The guard's own docstring warns about exactly
this failure — "judging it by its first token alone reads a correct denial as a broken sandbox" —
and the list it relies on omitted the most common separator there is.

Newline, carriage return and a bare `&` are compound markers now. With the fix the same audit
resumed and reached `quorum_complete`, the offending role passing on attempt 2.

`tests/test_sandbox_fault_attribution.py` covers both directions: the real multi-line denial is not
blamed on the sandbox, and a genuinely allowlisted denial is still reported. Verified by reverting
the marker list — three tests fail, and pass again when restored.

## 2.4.5 — 2026-09-09

A sweep of the operator-trap list that predates 2.4.x. Most of it turned out to be already fixed;
one item was not, and it was the worst kind — an unrecoverable stop on a path almost nobody reaches.

### A differing second adjudication no longer throws the round away
Two valid same-input critic reviews that differ have always been treated as ordinary sampling
variance: the first cached object stays canonical, the alternative is preserved under
`result_cache/conflicts/`, and the driver prints a `[note ]` and carries on. The targeted
adjudicator met the identical condition with `[fail ]` and `exit 1` — and nothing on disk could
clear it, because the cache read precedes the `I4R_FORCE_RERUN` branch and no code anywhere removes
the marker. A round that had already produced a valid adjudication was discarded over a tie-break
between two valid adjudications. Both paths now follow the same rule.

**This fix is not covered end to end, deliberately, and the reason is recorded at the site.**
Reaching the branch needs the same adjudication packet to trigger twice, but the trigger count is
computed from unresolved critic rows that the first round resolves — a rerun under
`I4R_FORCE_RERUN` re-dispatches the critics and yields `TRIGGER_COUNT=0`, so the adjudicator is
never called again. An end-to-end fixture was written, failed to drive the condition, and was
removed rather than left passing for the wrong reason. That near-unreachability is also why the
hard stop sat here undetected.

### Confirmed already fixed, not re-fixed
Checked against the trap list rather than assumed: the privacy gate no longer flags relative paths
(`data/users/alice.csv` and `https://example.org/users/profile` both pass, while `C:\Users\name`,
`/Users/name` and UNC `\\srv\Users\name` still fail); Phase 1 freezes before it promotes;
`publish_completed.sh`'s lock carries pid and staleness logic with an explicit Dropbox rationale;
the attempt-ceiling message distinguishes `MAX_TRIES` from `I4R_MAX_ATTEMPTS` by context; and the
focused-repair marker is scoped per input hash, so a role repaired in one round is repairable in
the next. `project_draft_claim_tags.py` and `run_meta.sh` are indexed in `scripts/README.md`.

## 2.4.4 — 2026-09-09

Carrying audit A32 to publication cost six cycles to ordering constraints in the
pre-finalize chain. None was a code defect and none is hard once you know it — but five of the six
announced themselves through a refusal that said only that something was stale, and two were
written down nowhere. That is the same defect this project keeps finding: a rule enforced in one
place and expressed in another, or nowhere.

### Five refusals now name the command that fixes them
- `document manifest <input> binding is stale` — the gate a report edit trips FIRST, before the
  verification or projection checks. It now names the whole rebuild tail
  (`generate_finding_artifacts --apply` → `build_docx.sh` → `make_verification_notebook.sh` →
  `run_verification.sh`) and says that a report edit invalidates all three in that order.
- `finding projection manifest does not match the canonical registry` — now names the usual cause,
  `make_settled.sh` writing settled fields into the rows after the projection ran, and the command
  to re-run.
- `projection manifest hash is stale: <file>` — now states the chain order the rest of the tail
  needs, since `approve_settled.py` binds the CURRENT report hash.
- `verification binding is stale for <file>` — now names both commands, and says that any report
  edit made to satisfy a critique round invalidates the run.
- `run changed_claim_ids is not the exact ordered union of generated later-round diffs` — now says
  where the expected list is read from (the round's snapshot-bound packet) and, crucially, that the
  round-N packet describes the round-**(N-1)** integration delta. Building it from round N's own
  before-files silently produces a different claim set and lands here.
- The `compliance_pass` violation for settled facts now names the event to log and explains that
  `generated_after_round` records the round the cache was FIRST generated after, not the round it
  was last re-bound in.

### Two orderings and two field meanings added to INSTALL.md
Re-run the projection **after** `make_settled.sh`, not before; re-execute the verification notebook
after the last report edit. Neither produces a distinctive error until staging, and neither was
documented. The two field meanings above are recorded beside them.

### The tests
`tests/test_pipeline_ordering_remedies.py` perturbs a copy of a real finalized audit and asserts
each refusal names its remedy, skipping when no finalized audit is available. The fixture rebuilds
its document against the code under test first, because the document manifest binds the gate code
and a stale fixture fails on code identity before any ordering check is reached. Verified by
stripping the remedies back out: two tests fail, and pass again when restored.

## 2.4.3 — 2026-09-09

Found by running a fresh paper end to end on 2.4.2 (paper A32). The
run cost two coordinator attempts, ~40 minutes each, to a defect no test could have caught, because
it only appears when a real model cites a real file.

### The coordinator was failed for citing the path it was given
The bound evidence set is keyed by AUDIT-relative path. The snapshot the model reads mounts those
same files elsewhere: `report/01_reproduction.md` is at `reports/01_reproduction.md`, and
`work/safe_evidence/privacy_scan.json` at `evidence/work/safe_evidence/privacy_scan.json`. The
runtime prompt itself says to *"read every file under ../source/reports"*. So the coordinator cited
what it had been handed and was rejected with `cites an unbound evidence hash` — twice, on two
different prefixes, on the same audit.

The dominant defect class again: a rule enforced in one coordinate system and expressed in another.

Three changes, because any one alone is wrong:
- **The prompt now states the rule.** Cite the `origin` value from `input_manifest.json`, never the
  `snapshot` path you read it from, with both examples spelled out. It was enforced and never said.
- **Citations are normalised on the way in.** `canonicalize_citations` rewrites a snapshot-spelled
  path to its audit-relative form, from the manifest's own `snapshot`→`origin` mapping, before
  anything validates or stores it.
- **The bound set stays strictly audit-relative.** The first attempt at this admitted the snapshot
  spelling instead, which let `reports/01_reproduction.md` into `work/findings.jsonl` — and every
  downstream tool resolves registry paths against the audit root, so the critique driver then died
  on `evidence path is missing or is not a regular file`. Fixing the rejection by loosening the
  validator just moved the failure later and further from its cause.

### The paper was carried to publication
Audit A32 is published: 2 rounds, `stopping_reason: target_complete`,
`compliance_pass: true`, no FAIL in the notebook, filed to the programme registry, reservation
released.

**Round 1 refuted two of our own findings and both refutations held.** The draft asserted a
non-integer value of a count variable and a fifth level "5" of a factor. The coordinator re-decoded
the deposit independently before acting: the count variable held only integers, and the
factor had four levels and no fifth. Both were withdrawn. Neither changed a published
conclusion, but publishing either would have put a false statement about the authors' file into the
record. Round 2 then caught that F3 still carried an inference limit round 1 had imposed on F5.
That is the adversarial design working, twice, on findings nothing else would have caught.

### Friction worth recording, none of it a code defect
Six things cost a cycle each and are all ordering, not bugs: the projection must run after
`make_settled` writes settled fields into the registry, or staging reports a stale projection
manifest; verification must be re-run after the last report edit; `REPORT_v2.md` must be cut before
staging; `approve_settled` binds to the report hash so it comes after the last report edit but
before `make_settled`; the `settled` event records the round the cache was FIRST generated after
(1), not the round it was last re-bound in; and the round-N diff packet describes the round-(N-1)
integration delta, so regenerating it from round N's own snapshots produces the wrong claim set and
fails the ordered-union check. INSTALL.md's binding-order paragraph covers the last four; the first
two are not written down anywhere and should be.

### The rest of the run
Phase 1: five auditors, all completed on attempt 1, no retries, no strands, `quorum_complete`.
Coordinator: 26 findings (16 INCLUDED / 6 MERGED / 4 EXCLUDED), none approved pre-Terra, as
required. Critique round 1: unattended authorization journaled its own plan and dispatched with no
pause; all four critics passed quorum on attempt 1; targeted adjudication completed; 54 items, one
new issue, **zero blocking**. The 2.3.0 unattended default, the 2.4.1 attempt accounting and the
2.4.2 strand-state work all behaved as documented on a live run.

## 2.4.2 — 2026-09-08

Regenerating the team cheat sheet for 2.4.1 meant re-deriving every fact in it from the tree
rather than from memory. Sixteen agents did that and a seventeenth asked what was missing; the
answer was a set of live defects nobody had a reason to look for, because each of them fails
silently. Everything below was found that way.

### Two of the three modern releases were filed into the wrong registry
Audit A33 recorded its registry root as `../../../../../../../.claude/skills/registry`
— a directory that **does not exist on this machine** — and audit A34 recorded
`../../../skill/i4r-audit-unified/registry`, the registry inside the skill checkout. Both receipts
said `filed: true`. Only the third was in the programme registry. Re-filed through
`finalization_receipt.py create --registry`, which supersedes rather than overwrites (both entries
now carry two versions), and the published copies of `finalization_receipt.json` in
`Completed reports/` were refreshed to match. Every receipt in the tree now resolves to
`<discipline-root>/registry`.

The mechanism that hides this: `finalize_run.sh` announces the registry it chose, but
`REGISTRY_DIR` / `I4R_REGISTRY_DIR` take priority over the derived path, and neither
`publish_completed.sh` nor `finalization_receipt.py` reads them at all. The label reads fine while
the path is wrong.

### `file_to_registry ()` was 79 lines of dead code — including the line the docs said to wait for
Defined in `finalize_run.sh`, called by nothing. Its success message,
`ok: filed into <registry> (index now holds N audits)`, was quoted in the cheat sheet and the FAQ
as *the* sign that an audit had filed. It could never print. Filing is done by
`finalization_receipt.py create --registry` under the `== 5. filing into the registry ==` banner.
Removed rather than repaired, so nobody turns it into a second writer of `registry/index.jsonl`.

### `status.py` could not match any audit to any reservation
`_reservations` keyed the index by `value["paper"]` — an absolute path on the **claiming** machine,
which this machine's audit directories can never equal — while `inspect()` looked up
`paper_dir.name`. Every row reported `reservation_age_days: null` while four reservations sat on
the board, so "finished but its paper reservation was never released" could not fire at all. Keyed
by basename now; a relocated workspace resolves through `paper_source.json` and reports unknown
without one, rather than inheriting its holding directory's name. Four of 21 rows now match.

Also: `status.py <tree> | head` died with a `BrokenPipeError` traceback and a non-zero exit that
looked like a real failure. SIGPIPE is restored to its default disposition.

### The package could not be built
`build_package.py --check` failed on eleven "private absolute paths in package inputs". Nine were
documentation examples *of the redaction logic itself* — the detector flagging the prose that
explains it — and are now written with the `<...>` placeholder its own allowlist accepts. Two were
real: two author home directories from an audited deposit, quoted verbatim in
a comment and a docstring, and are properly anonymised. Two test fixtures that must keep a real
path shape use the split-literal idiom the neighbouring lines already use. A stray `--help/`
directory in the skill root (an agent running `build_safe_evidence.py --help`) was removed.
`i4r-audit-2.4.1.zip` builds and checks clean at 150 files.

### Documentation that contradicted the code
- `INSTALL.md` still described the attended authorization pause as the default in two places, three
  releases after 2.3.0 made unattended the default. An operator following it would set
  `I4R_AUTO_AUTHORIZE=1` believing it changed something.
- `INSTALL.md`, `run_phase1.py --help` and `recover_stranded_attempts`' own docstring all still said
  a killed Phase 1 leaves the attempt at `inference_started`. Under metadata 2.1 it is
  `provider_requested`; 2.4.1 fixed the code and left three stale copies of the old claim in prose.
  The docstring also promised recovery "charges the attempt", which is true only of a legacy strand.
- `approve_settled.py` appeared nowhere in `SKILL.md`, and skipping it makes the settled cache
  promote **zero** facts silently. Added to the round-1 runbook, with the binding order that makes
  it work: last journal edit → `approve_settled.py` → `make_settled.sh` → the `settled` event →
  `make_diff_packet.py` → `run_post_delta_gates.py` → `finalize_run.sh`.

### A flaky test in the driver suite
`[10/10] terminating the driver reaps active providers` failed once in three runs, with all four
critics restarting at attempt 2 and then failing permanently. Not a regression — the critique path
was untouched — but a genuine race in the test. In the `slow` scenario only the stats role sleeps;
the other three run normally, and the test signalled as soon as stats reported started, catching
those three at an arbitrary point. It now waits until they have banked their results, so the reap
has exactly one live provider to kill and the resume sees the same state every time. Three
consecutive clean runs since.

### Checked and deliberately not changed
- **The seven stranded Phase-1 attempts.** Four are in a published, finalized audit: recovering
  them appends to its journal and stales the finalization receipt — verified on a copy, which
  failed with `finalization receipt is stale for RUN_METADATA.md`. Three are in an archived
  `audit.failed-*` sibling nobody will resume. Recovery exists to unblock an audit you intend to
  continue; neither case qualifies.
- **The four reservations.** All four belong to unfinished audits, so none is a leak, and three are
  other people's claims on other machines. `reap` is owner-scoped for exactly this reason.

### The onboarding document shipped in no release, and the SKILL.md cap was too tight
`EXCLUDED_SUFFIXES` in `build_package.py` excluded `.html`, and the only file in the tree that
matched was `CHEATSHEET.html` — so the team cheat sheet was in no package anyone ever installed,
and it was never scanned by the privacy gate either. `.html` is gone from the list; the rendered
`CHEATSHEET.pdf` stays excluded by name, as the build output it is. The package is 151 files.

The SKILL.md word cap is raised from 4000 to 4500. The cap does its job — it keeps SKILL.md a
procedure rather than a manual — but the file sat six words under it, and adding the one line that
stops `approve_settled.py` being skipped meant deleting the statement that there is no
`ALLOW_NO_VERIFICATION` waiver to pay for it. Both are present now, with 501 words of headroom.
Detail still belongs in INSTALL.md, which has no cap.

## 2.4.1 — 2026-09-08

An adversarial review of 2.4.0 found that six of its own fixes were broken, and that none of its
new tests would have caught it: every one of them grepped the source for a substring, so they pass
unchanged on code with the guard commented out. Three reviewers reported that independently, and
it is the most important item here. Fixed, and the tests replaced with ones that build the
condition on disk and run the real code.

### The wall-clock ceiling was worse than the wait it replaced
2.4.0 added a provider timeout. A killed call writes no classifiable text into its log, so the
text classifier called it **permanent**: the round ended on attempt 1 with the whole retry budget
unspent. It is now `transient` with `error_class=timeout`, branching on the flag rather than on
log text, in both the critic and adjudicator paths — the same reasoning already applied to a
cancellation. Provider-*reported* timeout wording stays permanent, deliberately.

Fixing that exposed a second, worse defect underneath. `budget_used` counted only reservations
that reached `inference_start`, so a call killed before the provider accepted it was worth zero,
and the retry loop re-dispatched the same attempt number **forever** — the round never returned at
all. `claim_attempt` writes its counter before every dispatch, so the counter is the only record
those calls happened; it is now read for every role, not just the model probe. `max()` can only
raise the count, so a resumed round still cannot re-spend a budget it has already used.

### `--recover-stranded` was a no-op on every real strand
Under metadata 2.1 the runner holds `status.json` at `provider_requested` for the whole call and
journals `inference_start` only once the stream proves acceptance. Both the guard and the recovery
matched only `inference_started`, a state 2.1 never writes. All **seven** strands in this tree are
`provider_requested`: the guard let a duplicate paid call through and the recovery cleared nothing.
Both now treat either state as a strand, and the guard distinguishes a genuinely live call by its
pid rather than calling it abandoned. Verified against copies of all seven.

Two honesty fixes fell out of it. A strand that never started inference records `attempt: null` —
the validator is right that a non-started inference cannot consume one — and `started_at` now comes
from the dispatch stamp in `status.json`, so a strand outstanding since 1 September no longer
records an `elapsed_seconds` of 0.001. A legacy pre-2.1 workspace is refused with the actual reason
(no journal, so the model and prompt hash a terminal record needs were never recorded and cannot be
reconstructed without inventing them) instead of "needs manual diagnosis".

### The corrupt-archive guard refused a good deposit
`unzip -t` inflates every member and exits 2 on the A35 deposit over one zero-length entry
written as `Defl:N` with zero compressed bytes. That archive extracts perfectly — the entry's true
content is nothing — so the guard would have stranded a real, auditable paper. The check is now
`zipinfo -t`, which is what "structurally broken" actually means: exit 3 on the truncated A36
deposit, exit 0 on the A35 one. Unreadable members are now a NOTE, never a refusal. `</dev/null`
throughout, because `unzip -t` on a password-protected archive blocks forever on a terminal.

### The archive gate contradicted the manifest builder
`verify_archive` still rejected *any* symlink while `phase1_runtime.source_files` — which builds
the manifest it checks against — applies the three-way rule. A deposit phase 1 had accepted failed
here instead, and this gate runs *after* the whole run's provider spend rather than at preflight.
Same rule on both sides now, with a test that asserts the two implementations agree.

### The workbook cap measured the wrong thing, and mislabelled files
The 20 MB cap sat *above* the suffix dispatch, so every oversize file of any kind was reported as
an "oversize workbook" — 34 in this tree (`.zip`, `.rds`, `.mov`, `.sav`) across 22 papers, each
inflating `workbook_count` and telling the role to "read the file directly" about a movie. The
suffix test now comes first.

The cap itself was also wrong. Measured here: a 20.3 MB / 387k-row xlsx costs 6.1 s and was being
dropped, while the two files that actually matter cost 21.1 s (334 MB) and 29.3 s (374 MB) out of a
60-second shared deadline. Size is a poor proxy for cost, so the byte cap moves to 256 MB — the
measured line — and the real bound is time: 8 s per file, 20 s for the whole scan, and exceeding
either degrades one file's evidence to `unavailable` instead of failing the preflight after the
paper is reserved and `pkg/` frozen. The shared deadline is re-checked so a genuine expiry still
fails closed.

### Smaller
- The conflicted-copy scan looked only at the audit directory, which at intake is normally empty
  or absent. It now scans the deposit being copied in, which is where they actually come from.
- `status.py` walked a fixed `<journal>/<paper>/audit` path and so could not see the three
  workspaces relocated with `I4R_PAPER_DIR` — idle five days, invisible. It now finds audits by
  marker, exits non-zero under `--json` as it already did in text mode, survives one unreadable
  audit instead of aborting the sweep, and lists archived siblings without flagging them.
- `renumber_findings.py` left `ledger_disposition.reason` naming ids that no longer exist. Rewritten
  in one pass, longest key first, so `{ALPHA: F1, F1: F2}` cannot cascade.

### The tests
`test_batch_scale_fixes.py`, `test_claims_join_permutation.py`, `test_repair_prompt_binding.py` and
`test_unattended_and_filing.py` were substring greps over source. They are now behavioural: real
zip fixtures reproducing the A35 and A36 defects, real symlinks, real strands, real
reservations, a real `init_run.sh` workspace. Two rules that were only reachable through a
several-hundred-line function are now named functions — `check_claims_join` and
`admit_repair_prompt` — so a test can exercise the rule instead of describing it. Each new module
was checked against the pre-fix code and fails there.

## 2.4.0 — 2026-09-08

Readiness for a 146-paper batch. A six-way read-only survey of the tree and the pipeline found
shapes the three completed audits never met; each item below stranded, bricked or silently
mis-filed a real audit, or would have on a paper already sitting in the tree. 335 tests (was 304).

### Three deposits could not be audited at all
- **A 1.19 GB CSV** took 178 s to summarise against a 60-second pre-dispatch deadline that
  `run_phase1.py` caps and refuses to raise, so the audit paused at preflight *after* the paper was
  reserved and `pkg/` frozen. `workbook_summaries` now caps at 20 MB, the same bound `privacy_scan`
  has always used; the file is still inventoried and hashed, and the role is told the summary was
  skipped rather than left to guess. Measured on the real file: 178 s → 0.00 s.
- **A symlink** made Phase 1 fatal for the whole deposit. Only an *escaping* link is a sandbox
  problem: an internal one is followed, a dangling one is skipped, an escaping one still raises.
  The deposit that triggered this ships `conf.yaml -> src/conf.yaml` with no `src/` in the archive.
- **A truncated archive** made `unzip` exit 50 mid-extract — and prompt for input on a terminal —
  after the reservation was claimed. Intake now runs `unzip -t` before claiming anything.

### Dropbox conflicted copies are refused at intake
Staging one release produced **80** `(… conflicted copy …)` files inside the audit's own tree.
Directory walks pick them up, inflating manifests and file counts. Intake refuses when any exist,
before the reservation. The guard is itself guarded: `set -euo pipefail` plus a `find` over an
audit directory that does not exist yet was killing intake on the happy path.

### A killed Phase 1 no longer bricks the audit
`guard_stranded_attempts` refuses to dispatch while an attempt sits at `inference_started` with no
terminal row — correct, since re-dispatching could duplicate a paid call, but nothing could ever
clear it. The window is the whole 45-minute call times five concurrent roles, so any sleep, OOM,
reboot or mistimed Ctrl-C left the directory permanently unrunnable, and the documented recovery
(`init_run.sh --force`) did not clear it.

`run_phase1.py --recover-stranded` terminalizes such an attempt as a transient failure, charging one
attempt against the role's budget exactly as a real failure would. It refuses outright when a
response *was* retained — that goes through the existing received-response path rather than
discarding real work — and reads the reservation from the journal, because `invocations.jsonl` is a
projection of terminal rows only and a strand has none. Nothing is invented for a call nobody
observed: model, cost, tokens and duration are null, not zero.

It also validates before appending. `record_terminal` appends and *then* projects, so an incomplete
record does not fail cleanly — it lands in the append-only journal and every later projection
crashes on it. That happened once during development and poisoned a fixture permanently.

### A hung critique call can no longer hold the round forever
`codex_exec` waited unbounded. Phase 1 has had a 45-minute per-attempt timeout since the beginning;
the critique path had none, so a hung provider call stalled the round indefinitely while holding the
round lock. The wait is now bounded (`I4R_PROVIDER_TIMEOUT_SECONDS`, default 2700), stops the
provider group on expiry and reports exit 124, which existing policy already treats as transient.

Two things the driver suite caught while landing it: liveness must poll the **child** pid, because
the helper is started with `&` from a shell without job control and never leads a process group, so
`kill -0 -- -PID` is false from the first tick and silently restored the unbounded wait; and the
poll must be fine-grained, because a 5-second tick adds its full period to *every* provider call
including the sub-second probe.

### Operating a batch
- `scripts/status.py <tree-root>` — the only thing in the skill that takes a tree root. Read-only,
  no locks, safe against a live batch. It immediately surfaced **seven** audits needing attention in
  this tree, including four that had sat idle between 5 and 12 days unnoticed.
- `scripts/renumber_findings.py` — every audit needs exactly one conversion from role-scoped ids to
  contiguous `F1..FN`, and no tool did it. Verified by reproducing, exactly, the 31-entry mapping
  audit A37 had been renumbered with by hand.
- `reserve_paper.sh reap` — `list` has always *flagged* stale claims and nothing ever released one.
  Reaping is owner-scoped and evidence-based: another machine's claim is reported and held, because
  `paper` is an absolute path on the claiming machine and "no audit directory" here means "not mine
  to judge". `--force` to act; `I4R_REAP_ANY_OWNER=1` only after asking.
- INSTALL.md gains a "Running a batch" section covering all of the above, and states plainly that
  `run.json` records only metered pipeline calls — not the orchestrator session, which on a measured
  run consumed roughly ten times the recorded figure. Budget from the provider console.

## 2.3.0 — 2026-09-08

**Unattended is now the default.** A run goes intake → publish without stopping for authorization.
Minor version bump because that changes behaviour for every existing caller. 304 tests (was 285).

### Critique rounds no longer pause for a human
`run_critique.sh` used to prepare the round plan, exit 3 and wait; `I4R_AUTO_AUTHORIZE=1` was the
opt-in. That is inverted: unattended is the default and `I4R_AUTO_AUTHORIZE=0` restores the pause.

Nothing about the *authorization* is weakened, only the pause. The grant is still journaled, still
bound to the driver's own unmodified template, and still written before any provider call — the
driver suite now asserts that ordering directly (`round_authorized` must precede the first
`provider_request`/`inference_start`), which is the property the pause actually protected. Every
model, effort and data class in the plan is policy-pinned, so an unattended grant cannot widen a
route a human would have seen. The three fixtures that exercise the attended pause now ask for it
with `I4R_AUTO_AUTHORIZE=0` instead of relying on the variable's absence.

SKILL.md states the end-to-end default up front, and both round-1 and later-round run books drop
the authorize-then-rerun two-step.

### finalize filed to the wrong registry by default
The default was `$HERE/../../registry` — the registry inside the skill checkout, not the programme
registry the discipline tree keeps at its root. An operator who omitted `--registry` got a green
"filed into the registry" and a row nobody would ever read. Audit A37 filed there
silently and had to be re-filed by hand.

`resolve_registry()` now derives `<tree>/registry` from the audit directory, the same way
`reserve_paper.sh` derives the reservations root, and falls back to the skill-local registry only
outside a discipline tree. `REGISTRY_DIR` / `I4R_REGISTRY_DIR` and `--registry` still win. The
choice is printed either way, because filing to the wrong registry looked exactly like filing to
the right one.

### An absent human-effort figure can now say why it is absent
`run.schema.json` is explicit that null means "nobody recorded it", which is not zero — but a run
had no way to say *which* kind of null it was, because `log_event.sh human_effort` required
`phase` and `person_minutes`, leaving the `notes` field the projection already supported
unreachable. Every unattended audit therefore produced the same finalize warning as one where
somebody simply forgot.

A notes-only `human_effort` event is now accepted; `phase` and `person_minutes` must still be given
together, and an empty event is still refused. Finalize distinguishes a documented absence from an
undocumented one, and still warns in both cases that the programme's numerator is missing —
documenting an absence explains it, it does not supply it.

While wiring this up: `lifecycle_journal.py` projects the same events by a second route and was not
guarded, so the first notes-only event wrote `person_minutes_by_phase[None]` and then crashed
`json.dumps(sort_keys=True)` on the mixed key types. Both projectors are now guarded and both record
`notes` and `people_involved`.

## 2.2.9 — 2026-09-08

Both defects here were found by carrying paper A37 all the way to publication — the first
audit driven end to end under this skill since 2.2.0. Each blocks a *finished* audit: every round
complete, every gate green, and then a refusal at the last step. 285 tests (was 272).

### A critic that took a focused repair made the audit unfinalizable
`run_critique.sh`'s focused-repair path re-dispatches a role with an augmented prompt and stamps
that prompt's hash into the result's `input_hashes.prompt_sha256`.
`validate_round_projections.py` — which `finalize_run.sh` runs before anything else — recomputed the
hash of the round's **original** rendered prompt and required exact equality. So a repaired role
produced a result the finaliser rejected as an "input-hash binding mismatch", after all four rounds
had completed and all four post-delta gates had passed.

Focused repair is a documented part of the contract: bounded to one attempt per role, every
scientific field byte-equivalent, its prompt written to `repair_prompt.txt` in the attempt
directory. The check now admits the recorded hash when it matches a `repair_prompt.txt` that exists
on disk **for that role and that round**; the other four input hashes still must match exactly, and
an unmatched prompt hash still fails. Round 2's completeness critic on audit A37 is
the case that surfaced it.

### The checklist gate demanded a claim order the builder is documented not to produce
`make_claims_script.sh` emits every executable claim in order and then an explicit no-code tail for
claims verified by inspection — the shape SKILL.md requires. `generate_finding_artifacts.py`'s
checklist compared the extracted claim numbers against `range(1, N+1)` with `!=`, so any audit
carrying one judgement or unverifiable claim failed the post-delta checklist gate.

Latent until an audit produced a non-code claim. This one has exactly one — a deposited raster
schematic with no OCR layer, read by eye — and hit it at `[1..20, 22, 23, 24, 21]`. Audit
A34's own claims script is `[1,2,3,5,6,7,8,10,11,12,13,14,16,17,4,9,15]` and would have
failed the same gate had it ever run. The join is now a permutation check — every finding claimed
exactly once, nothing invented, nothing duplicated — and the failure message names which of missing,
unexpected or duplicated it found instead of printing the raw list.

Both are the same defect class as the ten before them: a rule enforced in one place, and the thing
that satisfies it produced in another.

## 2.2.8 — 2026-09-07

Found by driving audit A37 end to
end. 272 tests (was 265).

### A deposited file that no auditor cited was uncitable by the coordinator
`package_manifest.json` is in `REQUIRED_AUDIT_CONTEXT`, so it is inside the coordinator's snapshot,
and the prompt tells the coordinator that snapshot is read-only and exhaustive. But
`known_evidence` assembled the admissible `(path, sha256)` set from three sources only: the five
role reports, the draft rows' own `source_hashes`, and the safe-evidence payloads. A file sitting
in the frozen deposit that no role happened to bind had no admissible pair, so citing it failed
the attempt.

That is backwards for precisely the rows the coordinator is required to produce. A tested negative
records that a file was examined and nothing was found in it — the file it names is by definition
one that no finding rests on, and therefore the one least likely to have been bound by a role.
Here it discarded a clean 25-minute coordinator session — four complete outputs, 49 rows, exit 0 —
because one tested-negative row cited `pkg/dataset/df_example.xlsx`. The hash the model gave
was that file's **true** hash, read out of the manifest it had been handed.

`known_evidence` now also admits the manifest's entries, `pkg/`-prefixed to match the audit-relative
convention every other binding uses. The rule this enforces — never invent an evidence hash —
is untouched: those digests are computed by `init_run.sh` from the read-only `pkg/` tree, which is
stronger provenance than a role's assertion, and a citation must still match one exactly. Negative
controls pin that a wrong digest for a real deposited path, and any path not in the deposit, are
both still rejected.

This is the same defect class as the nine before it: a rule enforced in one place, and the input
that satisfies it supplied from another.

### The audit-context files are now documented where they are first needed
`run_coordinator.py` hard-requires `PAPER.txt`, but SKILL.md introduced all four context files
(`PAPER.txt`, `ENV.txt`, `SLOW.txt`, `WRAPUP.txt`) under "Before round 1" -- one section *after* the
coordinator. Following the run book in order therefore failed the coordinator's preflight only after
Phase 1's five role invocations had been paid for. Section 3 now says to write them, and section 4
notes `PAPER.txt` is needed earlier. Same class again, in the documentation rather than the code.

## 2.2.7 — 2026-09-07

The last three items that had been carried as "needs a decision, not a patch". 265 tests (was 258).

### A paper-side remedy is no longer filed as a one-line code fix
`remedy_class` had four values, none of which fits "the analysis is right, the paper does not say
what was done". Those remedies were landing in `one_line_code` by default -- and
`count_by_remedy_class` is where the programme's headline claim comes from, so the bucket that
argues *most defects are trivial to fix* was being inflated by defects that are not code at all.
`disclose_in_paper` is now a fifth value, `heuristic_remedy_class` routes to it ahead of the
one-line branch, and `init_comment` derives its roll-up counters from a single `ROLLUP_REMEDY_KEYS`
tuple so the enum and the counters cannot drift apart. The sum check landed in 2.2.4 turns any
future drift into a loud finalize failure rather than a silently truncated distribution.

### The redaction baseline is documented without disabling two PII checks
`publication_redaction.fields_requiring_removal` was left empty because seeding the five pointers
the docs describe would have been actively harmful: `check_release_privacy` skips inspecting any
field a comment declares redactable, and two of those five --
`/governance/pii_description` and `/author_response/note` -- are on the release gate's
allowlist. Declaring them by default would have switched off two PII checks on **every** audit
ever run. The baseline now seeds only the three non-allowlisted pointers and names the other two in
its `note` as deliberately absent, with the reason. A negative-control test asserts the intersection
of the baseline and the gate's allowlist is empty, so this cannot be "helpfully" completed later.

### Warm retry, delivered without touching session persistence
Warm retry was parked because `--resume` needs `--no-session-persistence` dropped, which is pinned
at seven sites including `sandbox_policy_hash` -- invalidating every cached receipt. It is also
unnecessary: Phase 1 already has a warm path. `try_focused_repair` re-dispatches carrying the prior
attempt's diagnostics, preserves the scientific fields it produced, is bounded to one per role, and
records its own hashes. It was simply gated to two hash-mismatch cases.

Four faults in how a review is **written** now qualify: a private absolute path in `REPORT.md` or
in `result.json`, and an unbound source hash in either the report or the evidence manifest. In each
the science is already on disk and valid; only its written form is wrong, and throwing away a
completed five-role review to fix a path string is the expensive answer. The `result.json` message
now also tells the model the masking convention rather than only naming the fault. Faults that
impugn the *sample* -- no report, or a result that does not bind the immutable snapshot -- stay
non-repairable, pinned by a negative control.

Because the repair reuses the same prompt, `role_input_hash` is unchanged, so caching, recovery and
the content-addressed attempt ledger all behave exactly as before.

## 2.2.6 — 2026-09-04

The two remaining items that were internal design calls rather than policy. 258 tests (was 255).

### The schema no longer manufactures the completeness declaration
`status`, `completion_marker` and both `completeness_declaration` booleans were pinned as consts in
the provider schema, so the decoder was **compelled** to assert that every assigned check was
accounted for and every assessment evidenced -- whatever the model had actually done. The contract
meanwhile instructed it to return a truthful partial, which was unrepresentable. A reproducibility
tool recording a completeness claim its own schema forced is not acceptable.

`status` is now the open `partial | complete | failed` enum and all four declaration fields are
free. Nothing downstream loosens: `validate_v2` already accepted those statuses and ties
`completion_marker` to `status`, and `validate_critic_obj` still admits **only** `status=complete`,
so a truthful partial is retained as a failed attempt and re-sampled -- which is what the contract
now says happens.

The first attempt at this broke eight driver tests and was reverted in 2.2.4 as "needs a decision".
The decision turned out to be smaller than it looked: **`coverage.completed_checks` must stay
pinned**, because `_repair_candidate` reads it back out of the contract via
`_exact_array_from_contract` to derive the corrected administrative values. It is the focused-repair
path's source of truth, not a completeness assertion, and unpinning it silently disabled every
administrative repair. Pinning it while freeing the declaration gives both properties.

### A focused repair consumed by a harness defect no longer locks out the round
`<role>.repair.used` had no removal site anywhere in the tree, so a role whose one repair was spent
on a harness defect could never be repaired again that round -- even after the defect was fixed.
The marker is now scoped by the round's input hash: the one-repair-per-role rule holds exactly as
pinned within a snapshot, and a genuinely new snapshot, which is what a fixed harness produces,
starts clean.

## 2.2.5 — 2026-09-04

Pins the fixes that shipped without tests, and clears the operator traps left over from 2.2.4.
255 tests (was 246).

### Seven fixes were relying on a live run that will not be repeated
Two were proven only by one production round, five by nothing at all — the same shape as the
defects they fix. `tests/test_landed_fix_regressions.py` and a new critique-driver case now pin:
the attempt ceiling and its source; `superseded_status` and `calls_by_corrected_from_status`;
freeze-before-publish; the probe budget counter fallback; and the quorum-published-on-adjudication-
failure path. **Each was confirmed to fail with its fix reverted**, then the source restored
byte-identically.

### Writing that last test found two real bugs in the 2.2.4 fix
The live proof could not have caught either, because adjudication succeeded there:
- The exit handler collapsed every non-`complete`, non-`paused` outcome to `failed`, so the new
  `quorum_complete_adjudication_failed` state never survived to the status file.
- The `status` command's `--outcome` choices did not include it, so the status write **failed
  outright** — `FATAL: could not write atomic round status`. A failed adjudication would have left
  the round stale at `running` with a null exit code, indefinitely.

### The attempt ceiling now reaches the ledger
Journalling it in 2.2.2 was half the fix: it lived in `run.events.jsonl` alone, so a reader of
`invocations.jsonl` or `run.json` still could not tell budget exhaustion from completion. It is
carried through the reservation state into the projected row, and declared in both invocation
schemas.

### Token coverage no longer reads as full accounting
`coverage.tokens` counts an honestly-`unavailable` call as covered — deliberate, pinned, and
unchanged. But `1.0` alone reads as complete accounting. RUN_METADATA.md now states how many calls
report usage unavailable and that the totals are lower bounds, and the registry index rows carry
`token_totals_are_lower_bounds` and `invocations_with_unavailable_total` beside the fraction, so
audits compared side by side are comparable.

### Operator traps
- Both silent `exit 1` sites in the critique driver now say what happened and what to do. The
  adjudicator's budget exhaustion previously printed nothing at all.
- `run_critique.sh`'s usage header **stops advertising `CRITIC_MODEL`, `CRITIC_EFFORT`,
  `ADJUDICATOR_MODEL` and `ADJUDICATOR_EFFORT` as controls.** All four are policy-pinned: any value
  but the default is a hard failure, so they could only ever restate it. They are listed as read
  but not honoured, and `MAX_TRIES` is documented as the knob that actually applies.
- INSTALL.md gains a complete environment-variable reference — every variable the scripts read,
  led by the two attempt-ceiling knobs and which subsystem each governs.
- `CODEX` and `I4R_CODEX_BIN` now fall back to each other instead of each ignoring the other.
- `.runtime_lock.stale.*` directories are reaped after a day; nothing had ever removed them.
- The policy-snapshot warning in `validate_round_projections.py` fires once per run instead of once
  per critic per round.

## 2.2.4 — 2026-09-04

A systematic pass over the critique path after round 2 of audit A38 failed
twice. Three read-only sweeps found that nine defects shared one shape: **a rule enforced in one
place and expressed in another, or nowhere.** The fixes below close the instances; the last item
is what stops a tenth from shipping.

Verified end to end, not just by tests: the round that failed twice now completes
(`outcome: complete`, `adjudication: complete`, all four critics on their first attempt).

### The adjudicator was graded against a contract it was never given
- `prompts/common.txt` is rendered only into critic prompts, yet `command_validate_adjudication`
  runs the **full critic validator** against the targeted adjudicator. Every rule that prompt
  exists to state was enforced against a model shown none of it.
- The contract now lives in `prompts/contract.txt` and is rendered into **both**, with
  `{{INVENTORY}}` bound to each role's own snapshot layout — critics get
  `source/context/EVIDENCE_HASHES.json`, the adjudicator `source/EVIDENCE_HASHES.json`. Citing the
  path that genuinely existed in its own snapshot is what killed round 2.
- Target snapshot version `i4r-target-snapshot-3` → `-4`. `validate_round_projections.py` accepts
  both, so a round already run under `-3` still validates.

### Bindings are constrained instead of policed after the fact
- `artifact_binding.path`/`sha256` are now narrowed to the frozen inventory in both production
  paths (`constrain_evidence_bindings`). The canonical schema's traversal `pattern` is stripped
  for the provider, so any string was emittable and one bad reference out of ten killed an
  otherwise complete response. Both one-off recovery scripts had done this for a year; production
  never did. Had it, the round-2 failure was unreachable.
- The adjudicator's `new_issues` is capped at `maxItems: 0`, matching the coverage check that
  rejected any non-empty value whatever it contained.

### Failure handling that discarded valid work
- A schema-invalid adjudication was classified `permanent` (one attempt, no repair) while the
  identical condition is `transient` for critics. Round 2 died at attempt 1 of 3 with two-thirds
  of its budget unspent. Now transient, matching the critics and the stated principle.
- A same-input adjudication cache `.conflict` was an unrecoverable hard stop that no code
  anywhere removed and that `I4R_FORCE_RERUN` could not get past. It is now a `[note]`, as it has
  always been for critics.
- The adjudicator's `.permanent` marker was written but never cleared on success, unlike the five
  critic sites. Cleared now.
- **Quorum work is no longer discarded by a later failure.** A round that met quorum and then
  failed adjudication published nothing — four valid, paid-for reviews existed only inside the
  invocation directory. Critic results are now journaled and projected first; the round then exits
  non-zero without journaling `integrated`, so a rerun replays the cached critics and retries only
  the adjudicator.

### A deterministic wedge that had not fired yet
- `command_projection` compared `covered_claim_ids` and `items` to the routing assignment with
  order- and case-sensitive list `!=`, while every earlier gate is set-based and casefolded. No
  prompt states an ordering rule and the provider schema cannot express one. It runs *after* every
  critic is cached, so a rerun replays the identical output and dies at the same line — escapable
  only by deleting the cache by hand. Both comparisons are now set-based and casefolded. It had
  survived purely because round 1's critics happened to emit in assignment order.

### Correctness
- `count_by_remedy_class` counted only its four known keys while `findings_total` counted every
  finding, and nothing compared them: an unrecognised class was silently dropped from the
  published distribution. A sum check now fails finalize instead.
- The private-path detectors flagged any relative path containing a users- or home-named segment —
  a plausible deposit path and an ordinary URL both blocked a release. All three legitimate
  Windows forms (drive letter, UNC, bare separator) and the Unicode account class are preserved; an
  earlier proposed fix was rejected for silently dropping two of them.
- The model-probe budget guard was inert: the probe dispatches with `attempt_kind
  capability_probe`, which is deliberately outside `BUDGETED_ATTEMPT_KINDS`, so the journal path
  always returned 0 and the `.count` file was written and never read — one paid probe per
  invocation, unbounded. It now falls back to the counter for that role only.
- Snapshots are frozen **before** publication at all three promote-then-freeze sites. Freezing
  alone is not enough: renaming a directory across parents updates its `..` and needs write
  permission on it, so a fully frozen tree cannot leave a staging directory. Each site now stages
  inside the destination's own parent and renames within it, as the critique helper already did.

### Operator traps
- `publish_completed.sh`'s lock had no staleness logic at all, in a synced tree that resurrects
  empty directories. It now uses the pid/age/orphan handling `log_event.sh` has carried for the
  same reason.
- `I4R_ALLOW_EXISTING=1` advertised a recovery that could not run: the previous run left `pkg`
  read-only and nothing restored write before the re-copy, so it died on EACCES *after* claiming
  the reservation.
- The attempt-exhaustion message told the operator to raise `I4R_MAX_ATTEMPTS`, which
  `run_critique.sh` overwrites — correct for Phase 1, wrong inside a critique round, where the
  knob is `MAX_TRIES`.
- `VERSION` said 2.2.0 while the changelog was at 2.2.3, so every audit recorded a stamp three
  releases old. `gate_set_version` hashed only `scripts/*.sh`, ignoring ~35 Python gates and the
  provider schemas.
- Four recovery scripts are pinned to one earlier audit and no longer match the driver.
  They are now listed as stale rather than as working: one fails in a way that reads like a
  corrupt audit rather than a stale script.
- Remedies added to the messages that had none (`immutable path regained write permission`,
  `corrupt snapshot`, the coordinator's stranded-attempt guard).

### The guard that makes this class un-shippable
- `tests/test_critique_contract_coverage.py` extracts **every** rejection message reachable from
  `validate_critic_result` and `validate_critic_obj` — 88 of them — and requires each to be
  classified: STATED, with the mapped phrase asserted present in the rendered contract for **both**
  roles; SCHEMA_ENFORCED, because the provider schema makes it unemittable; or RUNTIME_INVARIANT,
  because it asserts something about the harness's own inputs that no model can influence. Adding
  a rejection without classifying it fails the suite, and a classification for a rule that no
  longer exists fails it too. Verified by injecting a plausible new rejection and watching it fail.
- The extractor deliberately follows the rejection *helpers* as well as the two entry points.
  Scanning only the entry points reached 62 messages and missed `verify_evidence_bindings` — the
  function whose message killed round 2 — along with the 18 `nonempty` and 13 `exact_keys` call
  sites. A guard that could not have caught the defect that motivated it is not a guard.
- The previous guard checked 3 of ~60 messages, against one prompt, and did not know the
  adjudicator prompt existed.

### Not fixed — deliberately
- **The completeness declaration is still compelled.** The provider schema pins `status`,
  `completion_marker` and both declaration booleans, so a truthful partial is unrepresentable even
  though `validate_v2` accepts `partial`/`failed` and the launcher would retain it as a failure.
  Dropping the constants reroutes several failure classes from focused-repair to re-sample and
  breaks tests that pin that behaviour deliberately, so it is a design decision, not a patch. The
  contract no longer *promises* a partial is possible — it now states the constraint plainly and
  says not to submit a declaration you cannot stand behind.
- `<role>.repair.used` is never cleared, so a role that burns its focused repair in a round cannot
  be repaired again that round. The zero-or-one-per-round budget is pinned by tests as intended
  behaviour; the cost only appears when the repair was consumed by a harness defect.
- `remedy_class` has no disclosure value; the `publication_redaction` baseline would loosen a
  fail-closed gate on every audit; warm retry needs `--no-session-persistence` dropped, which is
  pinned at seven sites including `sandbox_policy_hash`.

## 2.2.3 — 2026-09-03

Critique-side defects found by driving audit A38 through two full rounds.

### A critic could not raise a new issue at all
- The runtime narrows `$defs/assessment.finding_id` to an enum of the covered claim ids, so that
  `items` joins one-to-one to the assigned claims. `new_issues` referenced the *same* definition,
  so the structured-output decoder was forced to emit a real finding id there — `null` was not a
  permitted value — and `validate_v2` then rejected exactly what the schema had compelled
  ("new issues must use null finding_id"). Schema and validator contradicted each other, and no
  prompt wording could bridge them: a constrained decoder cannot emit a value the schema excludes.
- Measured across round 2's twelve critic attempts the correlation was perfect: **every attempt
  raising zero new issues passed, every attempt raising one or more failed.** Round 1 had survived
  only because all four critics happened to raise none. Round 2 lost quorum twice.
- `new_issues` now uses its own `$defs/new_issue`, identical to `assessment` but with
  `finding_id: {"type": "null"}`. Narrowing `assessment` no longer touches it.
  `tests/test_critic_new_issue_contract.py` pins the separation in both directions, including a
  negative control that the validator rule it routes around is still in force.

### Two semantic rules the critic prompt never stated
- `conflicts[].claim_ids` must be a subset of `covered_claim_ids` — enforced at
  `validate_v2.py:451`, stated nowhere. A critic cannot satisfy a rule it was never told.
- `new_issues[].finding_id` must be null — stated, but buried mid-sentence in a dense field list.
- Both are now their own bullets in the prompt's "contract details that reject an otherwise sound
  review" section, in the validator's own vocabulary. `tests/test_prompt_contract.py` gains
  `CriticPromptNamesValidatorRules`, so adding a semantic rejection to `validate_v2` without
  stating it in the prompt fails the suite. This is the same defect class
  `reference/failure_history.md` records for Phase 1, recurring on the critique side.

### Known traps, documented rather than changed
- `run_critique.sh:45` reads `MAX_TRIES` and line 60 *exports* it as `I4R_MAX_ATTEMPTS`, so
  setting `I4R_MAX_ATTEMPTS` externally has no effect on a critique round — it is silently
  overwritten. Phase 1's own exhaustion message tells the operator to "raise with
  I4R_MAX_ATTEMPTS", which is correct there and wrong here. Use `MAX_TRIES` for critique rounds.
- The attempt ceiling persists by round and role, so a role locked out by a *harness* defect stays
  locked out after the defect is fixed. That is the correct anti-thrash guard; `MAX_TRIES` is the
  intended operator remedy, and `max_attempts_per_role()` already documents the rationale.
- Editing a prompt changes the round input hash, so roles that already passed are re-dispatched
  and spend their remaining attempts on the new snapshot. A mid-round contract fix costs the whole
  round's budget, not just the failing role's.
- A run that aborts between building a round snapshot and freezing it leaves the snapshot root
  writable, and every later run then fails with "immutable path regained write permission" — a
  permanent wedge needing manual `chmod`. Seen once, on the snapshot built by a run that died at
  the budget check.

## 2.2.2 — 2026-09-03

Second batch from the same investigation: the defects the sandbox failure exposed but did not
cause, plus the audit-record correctness issues an independent evaluation had left open.

### A paid-for provider call always leaves a ledger row
- `run_role` caught only `OutputValidationError`, but `regular_file`/`strict_json` raise the
  parent `Phase1Error`. The two likeliest real failures — the model writes no `REPORT.md`, or a
  `result.json` that is not JSON — escaped every recorder. Measured: one provider start, **zero
  ledger rows**, attempt frozen at `response_received`, and `recover_received_role_attempt`
  re-adopting that dead attempt on every later run, re-raising the identical error forever.
  The spend was simply absent from run accounting.
- `validate_role_output` now converts a bare `Phase1Error` into `OutputValidationError`, so such
  a sample is recorded and re-sampled instead of vanishing.
- New `terminal_on_escape` guard wraps `run_role` and `try_focused_repair`: once the call is paid
  for, every exit path leaves exactly one terminal row. It also covers the post-call
  `verify_snapshot` and a promotion that dies before its journal event. The ledger — not a local
  flag — decides whether a row already exists, so nothing double-records.

### Session identity survives the failures that need it
- Every dispatch minted a `--session-id` uuid inline and discarded it; `run_process` persisted
  `provider_pid` but never the argv. The observed id is recorded only in the terminal record, so
  a provider rejection (which returns no envelope) left an attempt with no session identity at
  all — exactly the case you would want to trace.
- The requested id is now persisted in every attempt `status.json` and survives a crash through
  `recover_received_role_attempt`; `observed_or_requested_session` adopts the coordinator's
  existing observed-or-requested rule.

### The audit record stops drifting
- `finalize_run.sh` re-stamped `ended_at` to the current time on **every** finalize.
  `comment.timing.audit_date` derives from it and `days_elapsed` from that, so each re-finalize
  silently inflated the field the meta-paper plots against everything else. The shipped EXAMPLE
  pair proved it: recorded 308, re-derived 356. The close now comes from the last journal event,
  advancing when work advances and standing still when a correction merely re-derives.
- `invocation_correction` could overwrite a terminal status, erasing a failure from every count.
  Measured by re-projecting audit A34's journal: 13 rows were relabelled and
  **3 of them had terminated `failed`**, so 10 calls actually failed while `calls_by_status`
  reported 7. Projection now retains `superseded_status` and `summarize_invocations` reports
  `calls_by_corrected_from_status`, so a superseded failure stays countable. No history is
  rewritten and the counts are unchanged; the fact is simply no longer invisible. It
  back-populates on any existing audit via `recover`.
- The per-role attempt ceiling is journaled with the attempt it governs
  (`attempt_ceiling`, `attempt_ceiling_source`, `attempt_ceiling_applies`), so a reader can tell
  whether a role hit its budget or finished. `finalize_run.sh` and `validate_round_projections.py`
  no longer silently widen the contiguity check from 3 to 20 on a pre-2.2.0 policy snapshot; they
  warn instead.

### Privacy gate
- The account-name class was ASCII-only, so `/Users/<josé>/…`, `/Users/<张伟>/…` and every other
  non-ASCII home directory passed the release gate as though clean. The class is now Unicode-aware
  and identical to the old one on ASCII input; text is NFC-composed first, because macOS stores
  filenames decomposed and a combining mark is not a word character.
- `redact_private_paths` stopped at the first space, so `/Users/<Anna Smith>/…` redacted to
  `<private-path> Smith/…`. The surname survived into `run.json` and — with the `/Users/` prefix
  now gone — past the release gate too, so the leak was one-way and invisible to the check meant
  to catch it. A space-tolerant alternative fires only when a path separator follows, so ordinary
  prose is untouched.

### An audit no longer has to live inside its paper folder
- 14 of this tree's 171 papers end their title in a question mark, so 8% were unauditable after
  2.2.1's guard. The paper and package are *copied* into the audit dir, so it never needed to sit
  inside the paper folder — only the reservation and publication paths assumed it did.
  `I4R_PAPER_DIR` now decouples reservation identity from the audit dir's location, and intake
  records the true `<journal>/<paper>` in `paper_source.json`, which `publish_completed.sh` treats
  as authoritative. `setup_workspace.sh` refuses a sandbox-hostile audit dir at intake with that
  remedy.

### Capability probe, corrected
- The 2.2.1 probe asked for `find -fprint`. Two separate CLI gates reject that, neither of them
  the filesystem sandbox: `find` is confined to `--add-dir` directories, and `-fprint`/`-exec`
  "executes commands or modifies files — cannot be auto-allowed by a `Bash(find:*)` prefix rule".
  The read proof is now `cp <packet>/PROBE_CONTRACT.json packet_copy.json`, verified byte-for-byte,
  which exercises a sandboxed Bash read and write in one allowlisted command.

### Environment-fault escalation, narrowed after its first live run
- 2.2.1's guard classified a denial as an environment fault whenever the command's first token was
  on the Bash allowlist. Claude Code checks every sub-command of a compound command, so
  `cd X && export Y=1 && ...` is denied because `export` is not allowlisted — the policy working.
  Read by its first token that looked like a broken sandbox, and on the guard's first live run it
  paused a phase over a role whose real fault was a repairable evidence hash mismatch.
- A denial now implicates the sandbox only when the command is a single simple command: no
  `&& || ; | > <`, no backticks or `$(`, no leading `VAR=value`. The shapes that really were a
  dead sandbox (`find "<packet>" -maxdepth 3 -type f`, `mkdir -p "<output>/evidence"`) are simple
  commands and are still caught. Both directions are pinned by tests.

### Not fixed — these need a decision, not a patch
- `remedy_class` has no "disclose in the paper" option. Adding one changes the shape of the
  programme's public headline distribution ("most defects are one line of code or one number"),
  so it is an editorial call, not a schema chore.
- `comment.json publication_redaction: null`. The obvious fix seeds a baseline that would stop
  `check_release_privacy` inspecting `governance.pii_description` and `author_response.note` on
  **every** audit — a default-on loosening of a fail-closed gate. A corrected variant seeding only
  non-allowlisted pointers is specified and awaits sign-off.
- `_WINDOWS_HOME` flags relative paths like `data/users/alice.csv`. The proposed pattern silently
  stopped matching `\Users\name`, a real Windows home form; it needs re-deriving, and it is a
  loosening either way.
- True warm retry (`--resume`) still requires dropping `--no-session-persistence`, a deliberate
  isolation contract pinned by a test.

## 2.2.1 — 2026-09-03

Audit A38 stopped in Phase 1 with two dead roles and no reports. Root cause: the audit lived
under a directory named after the paper's title, which ends in `?`, and the Claude CLI
compiles `sandbox.filesystem` entries as glob patterns, so every rule containing the `?` matched
nothing. Cost of the undetected failure: 5,443,051 tokens and $6.43 for zero output.

### The sandbox rules are now checked for expressibility
- `phase1_runtime.assert_sandbox_expressible` refuses any path containing `? * [ ] { } \` before
  a provider call, naming the offending character and the remedy. Measured against the real CLI:
  those seven break; space, `:`, `~`, `(`, `)` and `!` are safe.
- Enforced at three points: `sandbox_settings` at emission, `validate_sandbox_contract` (which
  previously compared the emitted rules against themselves and so could not see a rule that is
  well-formed JSON but matches nothing), and `provider_free_preflight` before any other check.
- `run_coordinator.sandbox_settings` had the same raw-path emission and no contract validator at
  all; it now calls the same guard.
- On a broken root the failure is a preflight pause (rc 3) in under a second with zero provider
  calls, replacing a two-hour five-role fan-out that produced nothing.

### The capability probe now exercises sandboxed Bash
- The probe proved only that the Write tool worked — the one filesystem path that still succeeds
  when the rules are inert, because it takes a relative path under cwd and is not seatbelt-
  confined. It certified `Bash` and a `bash_allowlist` it never ran, and passed cleanly on a run
  where every sandboxed Bash command was denied.
- `PROBE_CONTRACT` gains `required_bash_write` (`evidence/bash.ok`, via `mkdir`+`cp`) and
  `required_bash_read` (`packet_listing.txt`, via `find` over the role packet). Both are verified
  on disk after the call. These are exactly the two operations denied in production.
- `probe_key` now binds the audit root, so a receipt earned under one root is not replayed under
  another through a shared `--probe-cache-dir`.
- `tests/fake_claude_phase1.py` models the real asymmetry: it always writes `probe.ok` but emits
  the Bash artefacts only when the emitted rules are expressible. The end-to-end suite is now a
  detector for this class rather than blind to it.

### Denials of allowlisted commands abort instead of re-sampling
- `allowlisted_denials` reads `sandbox_denials.json` — written since 2.2.0 and never read by
  anything — and reports denied commands the run's own `BASH_ALLOWLIST` grants.
- A validation failure with such denials raises `EnvironmentFaultError`, which is not re-sampled:
  the fault is local, and a fresh sample is denied identically. Two roles each spent all three
  attempts this way. Phase 1 now pauses with `pause_reason: sandbox_environment_fault` (rc 3).
- Role failure strings written into `phase1/status.json` are redacted through
  `redact_private_paths`. This path bypassed `make_terminal_record`, where redaction lived, and
  leaked the operator's absolute home path into a provider-visible release artefact.

### Known, not fixed
- A failed attempt still restarts cold; `output.partial` is preserved but not reused, and the
  recorded `session_id` is not used to resume. Resume requires dropping
  `--no-session-persistence`, which is a deliberate isolation contract and a separate decision.

## 2.2.0 — 2026-09-02

Audit A34 ran under the unified skill. Phase 1 reached `quorum_complete` after the fixes below; the full run and its cost
profile are recorded in that audit's `run.json` and `invocations.jsonl`.

### Authentication and dispatch
- Phase-1 runner no longer passes `--bare`, which refuses OAuth/keychain credentials and made a
  subscription-authenticated CLI unable to start any auditor. `--safe-mode` alone provides the
  customization isolation; `sandbox_policy_hash` now records `bare: False` truthfully.
- Probe token bound 25,000 → 150,000, recalibrated from measured safe-mode probes (66–98k).
- Transport failures (`Connection closed mid-response`, reset, timeout) classified `transport`
  and retried, instead of `permanent`.
- `run_phase1.py --probe-only`: a one-call live canary for credentials, routing and sandbox.

### Role output contract
- The role prompt now states every rule `phase1_runtime.py` validates: exact `scientific_fields`
  keys, finding field set and value types, evidence path base, closed output directory, no
  absolute paths, literal `report`/`evidence_manifest` path strings, write ordering, and that
  only `ROLE_PACKET.source_manifest` entries are citable sources.
- Sandbox permission denials are recorded to `sandbox_denials.json`, not treated as failures.
- Bash allowlist gains staging, text and cleanup utilities so compound commands built from
  allowed parts pass; the filesystem sandbox remains the boundary.
- Work-plan guidance targets one analysis script and well under 40 turns; turn count was the
  dominant Phase-1 cost (97–119 turns/role, 97% cache re-reads).
- `reference/brief_template.md` rewritten to match the runtime contract it is injected beside.

### Coordinator
- `read_ledger` byte-verifies only the latest completed row per artifact, so a role that
  completed twice no longer breaks the coordinator.
- Prompt names the literal headings the validator checks, including
  `## Appendix A. Summary of findings`, and lists the process-narration phrases
  `check_consistency.sh` rejects: attempt 2 of audit A34 paraphrased a drafting rule
  ("never corrections of an earlier draft") into the public report and tripped the gate.
- New `scripts/build_safe_evidence.py` and `scripts/build_draft_registry.py` produce the two
  coordinator inputs the skill previously required but never built.

### Unattended operation
- `I4R_AUTO_AUTHORIZE=1` makes `run_critique.sh` authorize its own unmodified generated round
  plan and continue in one invocation; the grant is journaled identically, only the pause goes.
  Covered by a new case in `tests/test_run_critique.sh`; the default still pauses.
- Coordinator prompt frames privacy and archive findings as data-stewardship observations. The
  first live coordinator call was refused by the provider's real-time cyber safeguard at turn 33
  ($7, 36 min, `error_class=permanent`); the audit's legitimate re-identification-risk finding is
  the likely trigger, and stewardship framing is the correct register for an I4R comment anyway.
- New `error_class` value `provider_safety_refusal` (schema, `validate_v2.ERROR_CLASSES`,
  `classify_error`): a provider content-safety refusal is named as such, never retried with
  identical content, and points the operator at the mitigation. The skill has no topic-based
  gates of its own and cannot disable the provider's. Pinned by `tests/test_error_classification.py`.

### Guardrails that discarded valid work
The operator's instruction after audit A34: no result is useless. Every gate below threw
away a complete, valid model output over a bookkeeping condition.
- Critique: a second valid review of identical input that differed from the first (ordinary
  sampling variance) was fatal to the round. Audit A34 lost a fully valid
  four-critic round this way, with `quorum_met: true` and the message `missing: unknown`. The
  first cached object is never overwritten, the newer output is preserved under
  `result_cache/conflicts/`, the invocation keeps the valid output it produced and bound in its
  own telemetry, the role counts as complete, and the quorum message names what actually
  happened.
- Critique: `.permanent` markers written for a role that later completed are now removed on
  success and on cache replay, so a plain rerun no longer skips a role that has a valid result.
- Critique: a `.permanent` marker left by an earlier invocation no longer stops the role without
  a provider call. A permanent classification describes one response (a rejected key, an
  exhausted quota, a bad configuration), not the role; those conditions change between
  invocations and the attempt budget already bounds spending. The role is retried within budget
  and the marker stays on disk as evidence until it completes. The same rule applies to the
  targeted adjudicator.
- Attempt ceilings (Phase 1 journal, coordinator) read `I4R_MAX_ATTEMPTS` (default 3, max 20), and
  the critique driver's `MAX_TRIES` accepts the same 1 to 20 range instead of refusing anything above
  the policy default of 3 (`pipeline-policy-v2.json` now records the operator ceiling). The critique
  cache loaders (`load_role_cache`, the targeted-adjudicator cache) still required a cached attempt
  count of 1..3 and would have refused to replay this audit's own attempt-4 reviews; they now accept
  the knob's maximum.
  The driver also exports `I4R_MAX_ATTEMPTS=$MAX_TRIES` for its helper calls: the runtime helper
  counts accepted inference starts per role under its own ceiling, and on audit A34 the driver
  allowed a fourth attempt that the helper then refused to record, discarding two complete reviews
  (data and completeness, round 1) after the provider had returned them. One knob now governs both.
  Every exhausted budget on audit A34 was consumed by harness defects, not by a role
  failing at its task; the operator decides the ceiling.
- Critique: the round's input identity (`runtime-contract-6`, snapshot `i4r-runtime-snapshot-5`)
  no longer includes the driver, helper and validator code hashes; they stay in `MANIFEST.json` as
  telemetry. Binding them meant every bug fix to the runtime orphaned an authorized round and its
  valid cached reviews, which is exactly how audit A34's complete four-critic round was
  lost twice over. The finalize gate, its self-test fixture and the release-pipeline test
  fixtures moved to `i4r-runtime-snapshot-5` with it. The snapshot-reuse check compares the identity-bearing
  manifest fields only; it had kept comparing the four code hashes, so the first same-input
  relaunch after a runtime fix read its own valid snapshot as corrupt.
- Critique: an unfinished round whose prepared plan no longer matches the current input can be
  re-prepared. `prepare_round(..., supersede=True)` journals a superseding plan (the earlier one
  stays in the chain), `authorize_round` binds to the latest plan, and the driver passes
  `--supersede-plan` under `I4R_AUTO_AUTHORIZE=1` so an unattended run continues instead of
  refusing with "prepared critique round plan differs from the current immutable input".
- Critique: a schema-invalid review is re-sampled within `MAX_TRIES` instead of marking the role
  permanent; all three critics that failed this way on audit A34 passed on a fresh attempt.
  The retry-backoff journal accepts the `schema` error class for this re-sample alongside the
  provider-retryable classes, so the telemetry still names the real reason for the retry.
- Critique prompt (`prompts/common.txt`) now states the two contract details that rejected three
  otherwise sound reviews: copy each 64-character hash verbatim from `EVIDENCE_HASHES.json`, and
  give a non-empty `requested_correction` on every REFUTED or OVERSTATED verdict.
- Settled facts: `scripts/approve_settled.py` writes the coordinator's promotion approval
  (`settled.approved` and `valid_for_report_hash` bound to the current `REPORT_final.md`) on every
  row that already meets the promotion rule, and clears it elsewhere. On audit A34 that object
  was never written, `make_settled.sh` promoted zero facts, and a critique round was dispatched with
  an empty cache before the omission was noticed. Run it after each integration, before
  `make_settled.sh`.
- Post-delta gates: documented that `DIFF_MANIFEST_round_N.json` must be regenerated against the
  integrated canonical files (after `make_settled.sh`) before `run_post_delta_gates.py --round N`;
  the verifier recomputes the packet from the live after-paths, so the packet a round was dispatched
  with fails the gate once integration has changed the report (audit A34, round 2).
- Phase 1: a schema-invalid role output no longer stops the role. It is a property of one
  sample, not of the role: on audit A34 every such failure passed on a fresh attempt.
  After the bounded focused repair, remaining budget is spent on a fresh sample before the role
  is declared failed.

### Document build
- `build_docx.sh` passed the LibreOffice profile as `file://<transaction dir>/lo-profile`. An audit
  directory named after a paper title routinely contains a colon and spaces, which are not URL-safe,
  and LibreOffice aborts (SIGABRT, exit 134) on that URL; audit A34's title contains a colon,
  so every canonical build failed while the scratch dry run passed. The throwaway profile now lives
  in a URL-safe private temp directory; document and output paths are passed as plain paths.

### Release privacy gate
- `check_release_privacy.py` reported two files of audit A34 as "absolute path(s) naming a real
  account": the auditor's own privacy-detector script and the safe-evidence builder, whose regex
  source spells the `/Users/` prefix with no account behind it. The Windows-path pattern's account
  segment accepted any non-slash character, so `|` and `[^` counted as account names. The segment is
  now restricted to account-name characters; real `/Users/<name>/`, `/home/<name>/` and
  `C:\Users\<name>\` paths are still caught, and a negative control covers detector source.

### Finalization gates
- `validate_round_projections.py` (finalize gate 0c) required `routing_version` `i4r-round-routing-1`
  while the critique runtime has written `i4r-round-routing-2` (claim-assignment routing) for every
  round; the finalize self-test and release-pipeline fixtures carried the stale string too, so the
  skew was invisible until the first end-to-end finalization. All three now name `-2`.
  The same gate also encoded the routing-1 semantics (round 1 unscoped, later rounds "exact
  changed claims" on every role) while routing-2 domain-routes claims to roles in every round;
  the gate now checks the routing-2 contract: well-formed per-role lists, round 1 covering the
  canonical registry, later rounds covering exactly the diff packet's changed claims across roles.
  Its per-role attempt check also hard-coded `1..3`; it now reads the policy's
  `max_attempts_per_role_operator_ceiling`, matching the driver's `MAX_TRIES` range.
  `finalize_run.sh`'s two invocation-ledger checks did the same and now read the same key.
  `record_invocation.py` (gate 0d) hard-coded the cap too and grouped capability-probe attempts
  round-wide, so four round-1 invocations that each legitimately re-probed (attempt 1 each, after
  identity and runtime changes) read as `[1, 1, 1, 1]`; probe sequences now group per invocation and
  the ceiling is the knob's maximum. `validate_v2.validate_run` carried the same rule and now matches. The
  compliance counter "one completed invocation per critic role per round" counted cache replays as
  completions; it now counts provider calls only. The same gate compared `run.changed_claim_ids` with a
  union built in critic-role iteration order; it now follows the generated packets' order (what a
  `review_scope` event records) and compares `rereviewed_claim_ids` by membership. Completed reviews of a superseded round identity
  (the identity-fix relaunch) are corrected to `legacy_record` like a reset Phase 1: retained as
  evidence, not canonical. Gate
  0c verified a later round's snapshot diff packet by recomputing it against the live report and
  registry, which integration has since changed (and the snapshot keeps no copy of the settled
  cache the packet referenced). The deterministic recomputation belongs to the post-delta gate, whose
  content-bound receipt is taken when the state is current; gate 0c now verifies the snapshot packet
  by its recorded bindings (version, round, SHA-256 of each bound input against the snapshot's
  own report and registry copies and the preserved pre-integration files).

### Lifecycle projection
- `lifecycle_journal.py` projected `round_quorum_complete` as state and timing only, so the
  `critics_run` the runtime declares there never reached `run.json` and finalize gate 0c compared an
  empty list with the immutable route. The quorum branch now projects `critics_run`, `critics_failed`,
  `parallelism` and `notes`; `_listify` accepts comma- or whitespace-separated role lists.
  The runtime journaled bare role names there while `run.json` and the immutable route use
  `critic_<role>`; the runtime now writes the prefixed names and the projection normalizes either
  form, so rounds journaled before the fix project correctly.
- Finalization also requires every phase (`intake`, `fan_out`, `adjudication`, `critique`, `release`)
  to carry `phase_start`/`phase_end` events and a `delivery` event dated after the last round's
  `integrated_at`. Only `fan_out` is journaled automatically; INSTALL.md now lists the events the
  coordinator logs (with `at=` set from the invocation records) before `finalize_run.sh`.

### Phase-1 reset history
- After `init_run.sh --force` re-ran Phase 1, the completed rows of the superseded first run stayed
  `completed` in the invocation ledger, so finalize gate 0d saw two canonical completions per auditor
  role and an output hash that no longer matched the promoted report. The truthful correction is an
  append-only `invocation_correction` setting those rows to `legacy_record` with the reset as the
  recorded reason (applied on audit A34 for twelve rows: seven Phase-1 rows of the reset run, two of them failed calls, and five critic rows of the superseded round identity); the status vocabulary has no
  `superseded` value and the reset itself should record this (see INSTALL.md).
  `summarize_invocations.py --check` (finalize gate 0d) now treats `legacy_record` rows as history:
  they are excluded from the Phase-1 quorum and five-way concurrency window, the initial fan-out is
  each role's earliest canonical call (attempt numbers continue across a reset), and
  `legacy_record` counts as a terminal disposition. Failed finalizations also left their
  `.finalize-tx.*` transaction directories behind (fifteen on audit A34).

### Post-delta gate journaling
- `run_post_delta_gates.py` wrote a passing receipt but never journaled the `gate=post_delta` event
  from which `post_delta_gates_passed` is projected, so `target_complete` finalization failed schema
  validation on audit A34 with a valid receipt on disk. The gate now journals its own passing
  result (stamped with the receipt's `completed_at`).

### Probe compliance scope
- The compliance rule "one successful capability probe per audit/model" counted every successful
  probe on a model route. The probe cache is keyed by the policy's `cache_key_fields` (CLI version,
  model, effort, schema-compiler identity, schema set, probe contract) and a key change makes a new
  probe mandatory; on audit A34 the Codex CLI updated between two invocations and the runtime
  helper (the schema compiler) changed twice, so four keys each held one legitimate success. The rule
  now counts successes per distinct cache key, using the `.runtime/probes/<key>.json` records.

### Declared publication redaction
- A coordinator attempt's error string journaled before the runner redacted local paths carried
  the account name into `run.json`'s event mirror, and publication's privacy inspection blocked the
  release. The journal is append-only and the mirror is hash-verified, so the record now supports
  the mechanism its schema describes: a journaled `publication_redaction` event appends JSON pointers
  to `publication_redaction.fields_requiring_removal` (`*` matches array indices), and
  `check_release_privacy.py` inspects `run.json`/`comment.json` with the declared fields masked while
  every undeclared field stays inspected verbatim. `run.schema.json` lists the new event type; `log_event.sh`
  requires its `fields_requiring_removal` payload. The set of pointers a declaration may cover is a
  closed list of internal-telemetry fields (paths, costs, provider names, error and stderr strings,
  the PII description, private correspondence); a pointer outside it is ignored and its field is
  inspected verbatim, so a record cannot silence the gate that checks it.

### Finalization record defects (found by the post-run evaluation)
- `records_retained` was derived relative to the finalization transaction directory (run.json's
  parent) once `audit_dir` stopped being stored, so a finalized run.json declared the final report,
  ledger, critique rounds and journal `never_created`. It now inspects the audit root, and because
  the field was only ever auto-filled when absent, a stale entry claiming an artifact was never
  created while that artifact is present on disk is now corrected (deliberate records about
  genuinely absent artifacts are still never overwritten).
- `comment.json.run_ref.run_json_sha256` was computed against the transaction copy of run.json
  before the metadata 2.1 journal commit re-projected the canonical file, so the filed comment bound
  a run.json hash that matched no file. Finalize now rebinds the hash to the committed run.json (and
  re-validates the comment) before committing the pair.
- The R claims script inlined audit-written snippets that call the verification notebook's
  `expect()`/`check()`/`read_deposit()` contract and the audit's `_prelude.R` helpers, none of
  which the file defined, so the published `claims_script.R` failed on its first call. When it
  inlines snippets, the R claims file now carries a standalone copy of that contract plus the
  prelude, and ends with a pass/fail summary.

### Post-run evaluation fixes (second pass)
- Appendix A now uses the `Status` header of `report_structure.md` and proportional pipe-table
  separators, so the Finding column no longer renders one inch wide over eight pages.
- Finalize re-closes `ended_at` on every successful finalization, derives `timing.audit_date` from
  it, and counts `n_human_verified` over human verifiers only (model reviewers are corroboration).
- `make_settled.sh` and `make_diff_packet.py` admit reviewer calls from every integrated round (not
  round 1 only), so literal round-2 confirmations of corrected claims replace the round-1
  OVERSTATED-derived records a settled fact would otherwise rest on. Both carried the same rule
  separately; they now derive the admissible rounds the same way.
- `scripts/integrate_round.py` replaces the ad-hoc integration code audit A34 improvised and
  lost twice. It plans and applies a round from the round's own outputs: every claim id that
  received a verdict (tested negatives included), the conflicts critics declared, coordinator
  approval read from the live registry, the verdict-to-outcome mapping of
  `reference/adjudication_guide.md`, and the response notes, adjudication block and integration
  summary all written from one plan so they cannot disagree.
- `publish_completed.sh` journals the actual filing as a `delivery` event.

### Coordinator output confinement
- The Claude CLI's built-in Write tool is not confined by the sandbox filesystem rules that
  govern shell commands; only cwd and prose steered where the coordinator wrote. The third live
  call ($7.18, 35 min) produced four files that pass every validator rule and the full
  consistency gate, but wrote them to the attempt directory, one level above `output.partial`,
  and was rejected. The runner now makes the attempt root read-only for the duration of the
  call, the prompt states the working directory explicitly, and a distinct error names the
  recovery when all four outputs land in the attempt root.
- New `scripts/recover_misplaced_coordinator_outputs.py`: fail-closed relocation of the
  byte-identical files, the standard `validate_outputs`, a journaled `invocation_correction`
  (identity and attempt accounting untouched), promotion through the shared transaction path,
  and a receipt. No provider call, no budget consumed. `promote_and_append` gained a pluggable
  `append_record` step so the transaction/rollback logic is shared, not copied.

### Journal hygiene
- Error strings are redacted (`run_coordinator.redact_private_paths`) before they are journaled
  by either runner. Attempt 3's error carried an absolute output path into `run.events.jsonl`
  and the `run.json` projection, which then failed the provider-safety check as a live input.

### Projection sequencing
- `generate_finding_artifacts.py --apply` rewrites `REPORT_final.md`'s Appendix A and claim tags
  from coordinator-approved rows only. Pre-Terra every row is unapproved, so the documented
  "after each schema-valid batch" call would have replaced the drafted 14-row Appendix A with an
  empty table. `--apply` now refuses with zero approved rows; SKILL.md says to project after
  Round 1 integration and to preview without `--apply` before that. The helper also surfaces its
  child scripts' output instead of a bare `CalledProcessError`.

### comment.json skeleton
- `generate_finding_artifacts.py --apply` requires an existing `comment.json` to inject findings
  into, but nothing created one; audit A34's round-1 chain stopped at its first step. New
  `scripts/init_comment.py` writes the schema-valid non-finding skeleton from `run.json` and a
  facts file, validating against `schemas/comment.schema.json` and rejecting unknown keys.

### House citations
- The validators demand the `AI-Games` and `AI-vertical` citation markers, but the unified skill
  dropped the reference text during the merge (the older SKILL.md and every published comment have
  it). Coordinator attempt 2 of audit A34 produced an otherwise fully valid bundle and failed
  only here, because a careful model will not invent a citation. New `reference/house_citations.md`
  is the single source; the coordinator prompt carries both entries verbatim with their anchors and
  the Introduction sentence, and the published four-author byline is recorded there too.

### Tests and docs
- `tests/test_prompt_contract.py` pins prompts to validator vocabulary; `tests/test_ledger_supersession.py`;
  Phase-1 tests for denial non-fatality and `--probe-only`.
- INSTALL.md: credential modes, Poppler ≥ 21.06, LibreOffice, the `i4rdoc` conda environment,
  the live canary, coordinator inputs, and `--force` recovery semantics.
- `scripts/README.md` classifies pipeline, recovery and retained one-off scripts.
- `reference/failure_history.md` records the prose-versus-validator failure class.
