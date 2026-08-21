# Shared auditor brief (fill in, then give to all five)

**Paper:** <citation> · **Package:** <url> · **Workspace:** <audit-dir>

Layout: `pkg/` archive · `paper/` paper.pdf + paper.txt · `work/` your scripts · `report/` your output

## Your job
Audit area <N>: <area>. Write `report/<NN>_<area>.md` **incrementally** — write the file early and
append as you go, so a cutoff does not lose your work.

## Rules
- Verify from the **raw files**. Do not trust another auditor's cache or this brief. **This brief may
  contain errors — if you find one, say so.**
- Label every claim **CONFIRMED / LIKELY / SPECULATIVE**.
- For each finding: what, evidence (a re-runnable script), benign explanations considered and how you
  ruled them out, and **impact on the paper's published conclusions** — "none" is a valid and common
  answer.
- **Report what you checked and did NOT find.** A refuted hypothesis is a finding.
- Redact personal values. Never put a participant code and a birth date on the same line.
- Note what the authors did **well**, specifically.

## Output
Summary (bulleted) · Checks performed and passed · Findings (one subsection each) · Open questions
for the lead auditor · Scripts left in `work/`
