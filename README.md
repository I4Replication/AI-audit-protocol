# AI-audit-protocol

`i4r-audit` is a Claude Code skill. It runs a full Institute for Replication (I4R) reproducibility audit of a
**published** paper and its replication package, and writes the comment for publication.

This repository holds:
- the skill, `skill/i4r-audit` (version 2.7.7);
- a single-agent baseline used to evaluate it, `skill/baseline` (version 1.1.0).

## How it works

1. **Five auditors** (Claude Sonnet 5.5) check the paper and its package independently. Each covers one area:
   - reproduction;
   - data integrity;
   - text against data;
   - code and methods;
   - privacy and archiving.
2. **A coordinator** (Claude) settles their findings into a ledger and drafts the comment.
3. **Critics** (GPT-6.1 Sol, through the Codex CLI) review the draft in at least two rounds and at most seven.
   The rounds stop when no blocking issue is left.
4. **Release gates** check consistency, privacy and the 10-page limit before the comment is filed.

The human verifier receives three things:
- the comment;
- a numbered claims script that re-checks each finding;
- a verification notebook.

## Before you start

You need:

- **Claude Code** 2.1.285 or later (`npm install -g @anthropic-ai/claude-code`), on an account with Claude
  Sonnet 5.5.
- **The ChatGPT desktop app**, kept up to date. Its built-in Codex CLI (0.159.0 or later) runs the critics. Sign in
  to a workspace that offers GPT-6.1 Sol and has credits; without credits there is no critique.
- **Python 3** and **jq**.
- **R** 4.4 or later with `rmarkdown` and `knitr`. Add `haven` to read SPSS, Stata and SAS data.
- **pandoc** and **LibreOffice**.
- **Poppler** 21.06 or later.

pandoc and Poppler usually come from a conda environment. Create it once:

```bash
conda create -n i4rdoc -c conda-forge pandoc poppler
```

Then put its PATH at the top of every shell, before you start Claude. Otherwise the build fails hours into the run:

```bash
export PATH="$HOME/miniconda3/envs/i4rdoc/bin:$HOME/.local/bin:$PATH"
pdfinfo -v          # must print 21.06 or newer
```

## Install

```bash
mkdir -p ~/.claude/skills
cp -R skill/i4r-audit ~/.claude/skills/
chmod +x ~/.claude/skills/i4r-audit/scripts/*.sh
```

Restart Claude Code. To update later, move the old copy out of `~/.claude/skills` before copying in the new one.

To check the install and your access to the critics:

```bash
cd ~/.claude/skills/i4r-audit
python3 config/check_policy_drift.py      # prints POLICY DRIFT CHECK OK
python3 scripts/check_critic_access.py    # one small GPT call (a pass is cached 24 h); exit 0 means the critics can run
```

## Run an audit

Audits run in a shared project folder, called the *tree*. Put each paper's PDF and replication package in
`data/<Journal>/<Paper>/`.

1. Open a terminal at the root of the tree (the folder holding `data/` and `runs/`), not inside a paper folder.
2. Export the PATH shown above, start `claude`, and choose Sonnet 5.5 at `xhigh` effort with `/model`.
3. Ask for the audit with `/goal`, phrased as the result you want, for example:

   ```
   /goal Audit the paper in data/<Journal>/<Paper> end to end, starting with
   bash scripts/new_audit.sh "data/<Journal>/<Paper>". Done means the comment is filed to
   comments/<Journal>/<Paper>/ and its record to metadata/<Journal>/<Paper>/.
   ```

   With `/goal`, the target stays clear even after a long session compacts its history.

From there Claude runs every stage, from intake to publication. An audit takes several hours, sometimes most of
a day, most of them unattended. On a Mac, use `caffeinate -i` so the machine does not sleep.

To see where every audit stands, run these from the root of the tree:

```bash
python3 ~/.claude/skills/i4r-audit/scripts/status.py .            # a short status entry per audit
python3 ~/.claude/skills/i4r-audit/scripts/status.py . --stuck    # only audits that need attention
```

If a run is interrupted (by sleep, a reboot or a closed terminal), ask Claude to resume the audit.
[`INSTALL.md`](skill/i4r-audit/INSTALL.md) covers recovery in detail.

## What you get

| Where | What |
|---|---|
| `comments/<Journal>/<Paper>/` | The comment as a PDF, plus a DOCX when the build produces one. It reads as a paper: abstract, introduction, what reproduces, one section per substantive finding (What, Evidence, Impact, Remedy), minor issues, hypotheses tested and not confirmed, and a conclusion. References and Appendix A, a summary table of findings, follow on new pages. The body is at most 10 pages; references and appendices do not count |
| `metadata/<Journal>/<Paper>/` | `comment.json`: every finding, with its impact, remedy, confidence and disclosure flag. `run.json`: models, tokens, timings and Claude cost. The claims script for the human verifier. The build and release records |

Everything else stays in `runs/<Journal>/<Paper>/audit/`: the auditors' reports, the ledger, the critique
rounds and the verification notebook.

## Learn more

- [`skill/i4r-audit/SKILL.md`](skill/i4r-audit/SKILL.md): the full run book, stage by stage.
- [`skill/i4r-audit/INSTALL.md`](skill/i4r-audit/INSTALL.md): requirements, validation, troubleshooting and
  upgrade notes.
- [`GUIDE.md`](GUIDE.md): a detailed guide to the pipeline, its outputs, its safety gates and its code.
- [`CHEATSHEET.pdf`](CHEATSHEET.pdf): the operator's cheat sheet.

## The baseline

`skill/baseline` audits each of the same, already-audited papers in a single Claude call, with no critics and
no rounds. It measures what the full pipeline adds. It is for evaluation, not for new audits; see
[`skill/baseline/SKILL.md`](skill/baseline/SKILL.md).

## Status

**Private for now.** The files in this repository do not name the papers that were audited or their authors. Earlier commits in its history do, so rewrite the history (or start a new repository from the current files) before making it public.

## How to cite

> Brodeur, A., et al. (2026). AI-assisted teams outperform AI-led teams but not human-only teams in
> assessing research reproducibility in quantitative social science. *PNAS*, 123(22), e2524747123.
>
> Institute for Replication (2026). Reproducing with AI Across the Expertise Ladder. OSF:
> https://osf.io/dkfzt/
