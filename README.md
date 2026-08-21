# AI-audit-protocol

A protocol for AI-assisted reproducibility auditing, and the Claude Code skill that implements it.
Gate set **G-2026-08-21**.

## Contents

| Path | What |
|---|---|
| `paper/` | *A Protocol for AI-Assisted Reproducibility Auditing: Specification and Empirical Agenda* — markdown source and built PDF |
| `skill/i4r-audit/` | The skill: five parallel auditors → adjudication with a dispositioned ledger → adversarial critique rounds by an external model → gated release, a verification notebook, and a numbered claims script for the human verifier |
| `skill/MERGE_NOTES.md` | The decision table from unifying the two development branches (2026-08-21) |

## Install the skill

```bash
cp -R skill/i4r-audit ~/.claude/skills/
chmod +x ~/.claude/skills/i4r-audit/scripts/*.sh
```

Then in Claude Code: describe the task ("audit this paper", "write an I4R comment") or invoke
`/i4r-audit`. See `skill/i4r-audit/INSTALL.md` for external requirements (Codex CLI for the critic
rounds, R with haven for the verification notebook, pandoc + LibreOffice for the deliverable).

## The design in one paragraph

Five auditors, one question each, no view of each other; a coordinator who settles disagreements by
re-running the disputed computation, never by preferring an auditor; a ledger that dispositions
every finding as included, merged, or excluded with a reason; critique rounds whose declared target
is the audit rather than the paper; and release gates whose detectors must fire on a known positive
before any clean result is believed. A finished audit hands its verifier a claims script — every
finding numbered, severity-labelled, and paired with the code that checks it, in the deposited
package's own language.

## Status

**Private by design for now.** Do not make this repository public before the authors of the
audited papers have been notified (protocol §3, Disclosure).

Two papers to cite:

> Brodeur, A., et al. (2026). AI-assisted teams outperform AI-led teams but not human-only teams in
> assessing research reproducibility in quantitative social science. *PNAS*, 123(22), e2524747123.
>
> Institute for Replication (2026). Reproducing with AI Across the Expertise Ladder. OSF:
> https://osf.io/dkfzt/
