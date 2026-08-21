# i4r-audit — install and share

A Claude Code skill that runs a full I4R reproducibility audit of a published paper and produces a
publishable comment. Five parallel auditors, then adversarial critique rounds driven by an external
model, then an I4R-format `.docx` plus an R notebook that lets a human re-check every number in it.

Built and hardened over thirteen audits in sport and exercise psychology (July–August 2026).

## Install

Drop the folder into the skills directory and restart Claude Code:

```bash
cp -R i4r-audit ~/.claude/skills/
chmod +x ~/.claude/skills/i4r-audit/scripts/*.sh
```

Then in Claude Code: `/i4r-audit`, or just describe the task — the skill's description triggers on
"audit this paper", "does this reproduce", "write an I4R comment".

For a whole team, commit the folder to a repo and have people symlink it, so a fix reaches everyone:

```bash
ln -s /path/to/repo/i4r-audit ~/.claude/skills/i4r-audit
```

## Dependencies

| Needed for | Requirement |
|---|---|
| Everything | `bash`, `python3`, `jq` |
| Adversarial critique rounds | the Codex CLI at `/Applications/ChatGPT.app/Contents/Resources/codex` — edit `CODEX=` at the top of `scripts/run_critique.sh` if yours is elsewhere |
| The `.docx` deliverable | `pandoc`, LibreOffice (`soffice`), `pdftoppm` |
| The verification notebook | R with `rmarkdown`, plus `haven` / `readxl` / `jsonlite` for whichever deposit formats you hit |
| Reading `.sav` / `.omv` deposits | `pyreadstat`, `jmvReadWrite` |

Nothing here calls a hosted API directly. The critics run through your own Codex CLI session.

## Before your first audit

Run the toolchain against its own shipped examples. It takes two seconds and it is the check that
would have caught the worst tooling failure in the programme's history — the skill shipped for weeks
with no `schemas/` directory, and nobody found out until the final step of an audit that had already
been delivered:

```bash
cd ~/.claude/skills/i4r-audit
bash scripts/finalize_run.sh --self-test
```

## What it produces per audit

| Artefact | Where |
|---|---|
| The comment | `report/I4R_comment_<FirstAuthor>_etal_<year>.{docx,pdf}` |
| The verification notebook | `verification.Rmd` + its knitted `.html` and `verification_summary.csv` |
| The audit record | `report/{LEDGER,ADJUDICATION,RELEASE_NOTES}.md` |
| Process telemetry | `run.json` |
| Coded findings | `comment.json` |

The last two validate against `schemas/` and are filed by `scripts/finalize_run.sh` into a registry
whose unit of analysis is one paper. `reference/SCHEME.md` is the classification scheme the findings
are coded against.

## Two rules that cost us the most to learn

**Run the audit somewhere durable.** Never a session scratchpad, never under `/tmp`. Put the audit
directory beside the paper. All thirteen of our working directories were deleted before anyone
retrieved the verification code, and it is not recoverable: the notebook generator's only input is
`report/REPORT_final.md`, and the per-critic token counts, model ids and reasoning-effort settings
live nowhere but the critic session logs. Six audits have no cost data at all because of this.

**Validate a detector before believing a zero.** Every clean scan in this skill's history that
turned out to be wrong was a detector that could not match what it was looking for — a `\b` word
boundary that never fires against an underscore, a date pattern that only knew `dd.mm.yyyy`. Fire it
on a known-present instance first, and build the positive control from split literals so it does not
trip the gate it is validating.

`SKILL.md`'s failure-modes table has the rest — every entry is something that actually happened.

## Licence and attribution

Internal Institute for Replication tooling, University of Ottawa. Ask before circulating outside
the group; the schemas and the classification scheme are unpublished.
