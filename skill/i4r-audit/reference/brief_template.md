# Shared auditor brief

This template is injected into every Phase-1 role packet as `BRIEF_TEMPLATE.md`. It states the
evidence standard; the machine contract for what a role must write is in the role prompt and is
enforced by `scripts/phase1_runtime.py`. Where the two seem to differ, the role prompt governs.
Models and efforts come from [`config/pipeline-policy-v2.json`](../config/pipeline-policy-v2.json).

## Where things are

The role sees an immutable source snapshot and its own attempt output directory, nothing else.

- `paper/` — the article PDF and its extracted text, read-only
- `pkg/` — the deposited package, read-only
- `ROLE_PACKET.json` — the role's assignment, `source_manifest` (the deposited files and their
  hashes), and `assigned_evidence` (deterministic indexes that route attention; never conclusions,
  never citable as sources)
- the current directory — the only writable location; it must end holding exactly `REPORT.md`,
  `result.json`, `evidence/manifest.json` and the evidence files that manifest lists

## Evidence rules

- Verify against the raw deposited artefacts. Copy anything that may write beside its inputs into
  the current directory before running it, and delete or promote the copy before you return.
- Do not trust another role's cache, this brief, or a transcribed value. If the brief or the packet
  is wrong, say so in the report's Brief corrections section.
- Derive article values from `paper/paper.txt` or the rendered PDF. Record the source location and
  accept Unicode and formatting variants before believing an absence.
- Give every quantitative claim a rerunnable command or script that lives under `evidence/`.
  Self-test any detector on a known-present and known-absent case and state how many objects it
  actually inspected.
- Label each finding CONFIRMED, LIKELY or SPECULATIVE. State its impact on the paper's published
  conclusions; "none" is common and valid, and every reproduction discrepancy should be placed
  relative to the paper's own stated thresholds.
- List plausible benign explanations and how each was tested. Verify a proposed remedy as carefully
  as the defect, and keep it proportionate.
- Report checks that passed and hypotheses that were refuted. A negative result is part of the audit.
- Cite only deposited sources (`ROLE_PACKET.source_manifest`) as evidence bindings, by their
  deposit-relative path. Never write an absolute filesystem path anywhere.
- Never place a participant identifier beside a name, date, timestamp or clock time. Describe counts
  and patterns without reproducing personal values.
- A deposit holding personal data is a disclosure-critical finding, never a reason to stop. Name the
  identifier kind, file, column and count, and a verified remedy; never quote a value. Write study
  dates and clock times on a different line from any sentence that mentions participants.
- Credit specific things the paper or package does well.

## Working efficiently

Cost scales with the number of tool calls, not the depth of analysis. Read the packet and sources
first, write one analysis script that performs all planned checks and emits JSON results, run it
once, then write the report from those results. Compound shell commands built from allowed
utilities are fine; redirection is not, so scripts write their own output files.

## Report contract

`REPORT.md` has exactly these level-2 sections, in this order: Result summary; Brief corrections;
Checks performed and passed; Findings; Tested hypotheses not confirmed; Open questions; Evidence
inventory. Write it early and update it as evidence develops; a skeleton preserves partial work but
is not completion. Finish with the role-completion marker the prompt specifies. The coordinator
reads the whole file, so do not rely on the return message to carry a finding.
