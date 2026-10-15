---
name: baseline
description: Run the single-agent, single-pass I4R baseline on a fixed subset of already-audited papers, for comparison with the full multi-agent pipeline (i4r-audit). One Claude call per paper audits the paper and its replication package and writes the whole comment, with no critics and no revision rounds; outputs go to baseline_comments/. Use when the user asks to run, check or summarise the baseline. Do not use to audit a new paper (use i4r-audit).
---

# baseline: the single-agent, single-pass condition

The baseline audits a paper in **one Claude call**: one model (Sonnet 5.5 at xhigh by default), one session, no
external critics, no coordinator and no revision rounds. The call gets exactly what the full pipeline's Phase-1
auditors got for the same paper, plus the documents that define the task and the assessment standard, and writes the
complete comment. It exists to be compared with the full pipeline on the same papers, so the condition and every
resource used are recorded.

What is held fixed against the full pipeline:
- **Model**: `claude-sonnet-5-5` at xhigh with a 1,000,000-token context window, as every Claude call of the Sonnet
  5.5 full runs was served, and the 128,000 output cap they ran with (`CLAUDE_CODE_MAX_OUTPUT_TOKENS`). Each call's
  served model and window are checked; a different window, or any second served model, is a `condition_mismatch`
  (every Claude call of the 72 published full runs was served one model). The CLI's envelope does not show the output
  cap it used (2.1.206 reports 64,000 while requesting 128,000), so `probe` reads it from the requests.
- **Prompt cache**: written for 1 hour, as the full runs' calls were (`CLAUDE_CODE_PROMPT_CACHE_TTL=1h`; left to
  itself the CLI writes 5-minute cache on an API key, which is billed lower). Each call's cache writes by lifetime are
  recorded, and a 5-minute write is flagged.
- **Inputs**: the frozen Phase-1 snapshot of the published full audit (paper, deposit and deterministic indexes),
  copied byte for byte and checked against its manifest. The environment fingerprint index is regenerated on this
  host, as the auditors' described the host they ran on.
- **Task definition and standard**: i4r-audit 2.7.7's `checklist.md`, `brief_template.md`, `SCHEME.md`,
  `report_structure.md` and `house_citations.md`, verbatim (`reference/PROVENANCE.json` holds their hashes), plus the
  auditor and comment-writer rules on evidence, personal data and publication hygiene. The prompt tells the agent to
  ignore their pipeline-process instructions (rounds, critics, ledgers, claim tags).
- **Launch**: the Phase-1 auditor argv, Bash allowlist, appended steer and network denial (a test compares them with
  the live i4r-audit when it is beside this skill). The launching session's own links (its messaging socket and
  token, session ids, the user's e-mail) are removed from the call's environment.
- **Layout**: the pipeline's comment layout. The agent writes headings without finding ids, refers to findings by
  section, and marks each finding with one invisible `<!-- finding:Fk -->`, as the pipeline's coordinator does (the
  validator checks all three). As in the pipeline, the printed Appendix A and the findings cross-reference are
  generated from the findings record (`findings.json`) by i4r-audit's own generator functions, and the comment is
  built by i4r-audit's builder. Paper-level choices that the pipeline's own comments make differently from paper to
  paper (minor issues as run-in titles or numbered subsections, the wording of tested hypotheses) are left to the
  agent; matching one paper's published comment would mean showing it to the agent.
- **Accounting**: served model ids, tokens, cost, turns and time, read from the CLI's result envelope as the
  pipeline reads them.

What differs, and is recorded in every `run.json`: one call does the work of five auditors, the coordinator and the
critique rounds; its budget and timeout (config/baseline.json: USD 30, 7,200 s) are its own; it runs on this host's
toolchain (Python, R and their packages, recorded under `environment`), not the full run's; and it is confined more
tightly than the auditors, because the published comments now exist on disk. Its file tools reach only its own
input, packet and output folders (and its own saved tool output). Its shell cannot read:
- the tree, Dropbox and other CloudStorage folders, or iCloud Drive;
- Downloads, Desktop, Documents, Mail or Messages;
- Claude's app data, or Claude Code's history, sessions, transcripts, backups and temporary folders as they exist at
  launch;
- Codex's folder, or other baseline runs.

It can still read the rest of the disk, as the auditors could (system and toolchain files, other folders of the home
directory). That is why `preflight`, `run` and `batch` search that readable space for copies of the subset papers'
comments before any paid call.

## Operating rules

- Paid calls run **one at a time**: `run`, `batch` and `probe` take `baseline_comments/.batch.lock` atomically.
- Never contact authors or journals. Personal data in a deposit is reported as a finding, never a reason to stop.
- Never edit the full pipeline's records (`runs/`, `comments/`, `metadata/`) or another baseline run's output. A
  rerun moves the earlier output to `baseline_comments/.superseded/`; nothing is deleted.
- Do not change the condition between papers. A different model or effort is a different condition: run it into its
  own output folder, or record why.
- Choose the subset before the first paid run. `select_subset.py --write` refuses to replace a SUBSET.json that runs
  already used, unless you pass `--replace`, which keeps the old file aside.

## Install

```bash
cp -R "<tree>/skill/baseline" ~/.claude/skills/baseline      # or: ln -s "<tree>/skill/baseline" ~/.claude/skills/baseline
```

Restart Claude Code. The scripts run from the tree root, the folder holding `data/`, `runs/` and `metadata/`. They
use i4r-audit, from `<tree>/skill/i4r-audit-unified/i4r-audit` or `~/.claude/skills/i4r-audit`, for the deposit's
column-name exemptions, the environment fingerprint and rendering. Without it the baseline still runs and says so.

**Claude CLI.** Any version the full runs used works (2.1.206 to 2.1.295; most ran on 2.1.285 or 2.1.286). To match
a paper's full run exactly, use the version it ran on. 2.1.281 is refused up front, since it serves a 200,000-token
window. To use another binary, pass `--claude-bin` or set `I4R_CLAUDE`. The Claude desktop app
bundles one at `~/Library/Application Support/Claude/claude-code/<version>/<hash>/claude.app/Contents/MacOS/claude`.

## Commands

`S=~/.claude/skills/baseline`:

```bash
python3 $S/scripts/select_subset.py                 # preview the default draw (no file written)
python3 $S/scripts/select_subset.py --write         # save baseline_comments/SUBSET.json
python3 $S/scripts/run_baseline.py preflight        # CLI, work folder, input hashes, contamination scan; no model call
python3 $S/scripts/run_baseline.py probe            # one cheap call (about USD 1): proves the sandbox works
python3 $S/scripts/run_baseline.py run 1            # one paper (position in SUBSET.json, or its audit id)
python3 $S/scripts/run_baseline.py batch            # every paper with no run yet, one at a time
python3 $S/scripts/run_baseline.py status
python3 $S/scripts/run_baseline.py render 1         # build a run's comment again (after a renderer fix); no model call
python3 $S/scripts/summarize_baseline.py --json baseline_comments/summary.json
```

Run `probe` once before the first batch, and again after a CLI update. It checks that the agent can read its inputs
and its own saved tool output, run Python and R, and is refused a denied file by its file tools and by its shell. It
also checks that the window is 1,000,000 tokens, that the cache was written for 1 hour, and, from the CLI's raw
request log (kept for the probe only, in `_probe/<time>/api_bodies`), that the requests ask for the 128,000 output
cap; it lists the tools the requests offer. A CLI that writes no request log leaves the cap unverified, and the probe
says so. Launch a batch detached (`nohup
python3 $S/scripts/run_baseline.py batch > baseline_comments/batch.log 2>&1 &`) and follow it with `status`. To stop it,
run `kill -INT <pid>` (the pid is in `.batch.lock`): the running call is killed and recorded, and the lock is released.

**Subset.** `select_subset.py` draws from published audits whose Phase-1 snapshot is on disk:
- **Pool.** `--pool shipped` (the default) takes audits whose Claude roles ran Sonnet 5.5 at xhigh with GPT-6.1 Sol
  critics, the shipped condition. `--pool sonnet55-xhigh` adds the Terra-critic audits, and `--pool sonnet55` adds the
  max-effort ones, which ran at an effort the baseline does not.
- **Stratification.** `--stratify proportional|balanced` sets the code/no-code mix.
- **Draw.** `--seed` fixes the draw, and `--audit-ids FILE` takes an explicit list instead.

SUBSET.json records the pool, its member ids and their hash.

**Contamination.** `preflight`, `run` and `batch` hash every comment and metadata file of the subset papers. They then
search the home folder, /Users/Shared, /private/tmp and $TMPDIR outside the denied folders for identical copies, and
refuse to start on a hit. Move the copy into a denied folder, or list its folder under `extra_deny_read` in
config/baseline.json. Everything the shell can read is searched, ~/Library and toolchain folders included. Each
`run.json` records how many files were sought and seen, the roots and the time. The search matches identical files
only; a renamed copy with other bytes, or the paper's title in another document, is not detected.

## Statuses and what to do

| Status | Meaning | Rerun? |
|---|---|---|
| `completed` | the call succeeded and the outputs pass `validate_outputs.py` | no |
| `completed_invalid` | the call succeeded but the outputs fail validation; they are kept, the errors listed, nothing repaired (a repair would be a second round) | no: it is a baseline result |
| `budget_exhausted` | the call hit its USD budget; whatever it wrote is validated and kept | no: it is a baseline result (`--force` to rerun) |
| `timeout` | the call ran past its timeout and was killed | `batch --rerun-failed` |
| `failed` | the call or the setup failed (the error class says why) | `batch --rerun-failed`, after fixing the cause |
| `interrupted` | the operator stopped it during the call (a stop after the call keeps the call's outcome) | `batch --rerun-failed` |
| `abandoned` | the runner was killed (kill -9, a crash, a reboot) before it could record the run; found and booked the next time the paper comes up | `batch --rerun-failed` |

A batch stops by itself on an authentication, quota (any of the CLI's session, weekly, Sonnet or spend limits), model
or condition-mismatch error (a wrong window, or a second served model such as the CLI's Opus advisor tool), since the
next paper would fail too. A wrong model or context window is caught even when
the call also ran to its budget. A batch never reruns a paper that already has a run unless you pass `--rerun-failed`
(for failed, timeout, interrupted and abandoned runs) or `--force`. One extra call is made only after a transient or
transport error in which no inference started (`--retries 0` turns that off), so a single run spends at most its
budget plus the turn in progress; each rerun is a new run with its own budget. Every run, finished or not, is a line in
`baseline_comments/RUNLOG.jsonl`. `summarize_baseline.py` counts every run made under the current condition in the
resources, counts only calls that started inference (as the pipeline does), and reports a cost as unknown, not zero,
when a call's cost was not recorded. Calls and hours cover every model on the pipeline side (Claude roles and GPT
critics); cost and tokens are Claude's only, since the critics' cost is not recorded. Cost is also given as if every
cache write had the full runs' 1-hour lifetime (config: `cache_write_usd_per_mtok`), and the pipeline's page counts
come from its build manifest.

## Outputs

`baseline_comments/<journal>/<paper>/`:
- **The comment.** `front_matter.md`, `REPORT_final.md` and `captions.txt` as the agent wrote them, plus
  `comment.pdf` and `comment.docx`. These are built by i4r-audit's builder with its page cap lifted, from
  `REPORT_rendered.md`: the agent's report with Appendix A and the findings cross-reference generated from
  `findings.json`, as the pipeline generates them (the builder leaves the cross-reference out of the PDF, as it does
  for the pipeline's comments). `run.json` records the body pages and whether the comment would have passed the
  10-page cap (`render.pages.over_page_cap`); `render` builds it again and keeps the earlier record in
  `render_history`.
- **`findings.json`.** The comment.json finding subset (`reference/FINDINGS_FORMAT.md`).
- **`evidence/`.** The agent's scripts, aggregate text results and figures.
- **`run.json`.** The condition, the environment, the source audit and snapshot, the hashes of the code that ran,
  and every call (argv, deny list, served models, context window, tokens, cache writes by lifetime, cost, turns,
  time, errors). It also holds the scan, validation and render records.
- **`records/`.** The prompt, the sandbox settings, the CLI envelope and stderr, and `validation.json`.

**Withheld files.** A comment or evidence file that fails the pipeline's hygiene patterns stays in the private work
folder. Those patterns cover participant codes, a code or identifier beside a date or clock time, absolute paths and
e-mail addresses. Three other kinds of file stay there too:
- anything that cannot be checked (binary or compressed data, unreadable PDFs, text over 20 MB, links);
- any data file of more than 200 lines, which is more than an aggregate result;
- the parts of the CLI record that could carry file contents (a denied tool's input, a final message or stderr that
  fails the check).

All are listed under `outputs.withheld_from_tree`. The patterns cannot see every identifier (a numeric id in a short
table, a name), so read the evidence before sharing the folder outside the team.

**Work folder.** The private work folder (`~/.i4r_baseline_work/<audit-id>/<run-id>/`, or `I4R_BASELINE_WORK`, never
inside the tree, a cloud folder or Documents) keeps the input copy, the packet and every call's raw output. It holds a
copy of the deposit: remove it with `rm -rf` when the analysis no longer needs it.

## Tests

From the skill folder: `python3 -m unittest discover -s tests`. A fake CLI stands in for Claude, so no test makes a
model call. `I4R_SCAN_ROOTS` and `I4R_CLAUDE_TMP_ROOT` exist so tests can point the contamination scan and the
temporary-folder denial at their own folders; do not set them for real runs.
