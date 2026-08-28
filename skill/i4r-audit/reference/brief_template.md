# Shared auditor brief

Fill this once, then specialize the assignment block for each of the five roles. Models and efforts
come from [`config/pipeline-policy-v2.json`](../config/pipeline-policy-v2.json); do not improvise a
substitution without recording it in `run.json`.

**Paper:** <verified citation>  
**DOI:** <doi>  
**Package:** <live repository URL>  
**Workspace:** <durable audit directory>

Layout: `pkg/` immutable deposit · `paper/` article and extracted text · `work/<role>/` private
scratch and evidence · `report/` reports and comment · `critique/` external review

## Assignment

**Role:** <canonical role name>  
**Area:** <1–5 and name>  
**Model:** <policy requested alias> · **Reasoning effort:** max  
**Checklist routing:** <primary item numbers from reference/checklist.md>  
**Private work directory:** `work/<role>/`  
**Report:** `report/<NN>_<area>.md`

## Evidence rules

- Verify against the raw deposited artefacts. Treat the package as read-only; copy anything that may
  write beside its inputs into your private work directory.
- Do not trust another role's cache, the shared brief, or a transcribed value. If the brief is wrong,
  report the correction explicitly.
- Derive article values from `paper/paper.txt` or the rendered PDF. Record source location and accept
  Unicode/format variants before believing an absence.
- Give every quantitative claim a rerunnable command or script. Self-test detectors on a known-present
  and known-absent case, and assert how many objects were actually inspected.
- Label each finding CONFIRMED / LIKELY / SPECULATIVE. State its impact on the paper's published
  conclusions; “none” is common and valid.
- List plausible benign explanations and how each was tested. Verify a proposed remedy as carefully as
  the defect.
- Report checks that passed and hypotheses that were refuted. A negative result is part of the audit.
- Never place a participant identifier beside a name, date, timestamp, or clock time. Describe counts
  and patterns without reproducing personal values.
- Credit specific things the paper or package does well.

## Report contract

Write the report early and update it as evidence develops. A skeleton preserves partial work but is
not completion. The finished report contains:

1. a short result summary;
2. corrections to this brief, including “none”;
3. checks performed and passed, with scope and controls;
4. one subsection per finding with status, evidence, benign explanations, conclusion impact, and
   remedy;
5. tested hypotheses not confirmed;
6. open questions for coordinator adjudication;
7. an inventory of rerunnable scripts and outputs in the private work directory.

Return only after the report is complete and evidence paths resolve. The coordinator will read the
full file, so do not rely on the return message to carry a finding.
