# findings.json

The machine-readable record of the comment, written beside `REPORT_final.md`. Its fields are the subset of the I4R
`comment.json` finding record that a single audit can fill, with the same names and the same allowed values, so the
two can be compared field by field. `tools/validate_outputs.py` checks it.

```json
{
  "schema_version": "i4r-baseline-findings-1",
  "package": {
    "data_present": true,
    "code_present": true,
    "runs_as_shipped": "yes | no | partial | not_attempted",
    "reproduces_headline": "exact | within_rounding | qualitatively | no | not_attempted"
  },
  "findings": [
    {
      "id": "F1",
      "claim": "One line in plain words: the Finding cell of Appendix A row 1.",
      "section": "Section 3",
      "confidence": "CONFIRMED | LIKELY | SPECULATIVE",
      "impact": "none | unverifiable | value_wrong | conclusion_narrowed | conclusion_unsupported | conclusion_contradicted | artefact_of_construction",
      "changes_conclusion": false,
      "changes_conclusion_which": null,
      "disclosure_flag": false,
      "remedy_class": "one_line_code | one_number | reanalysis | not_fixable | disclose_in_paper",
      "remedy_description": "One sentence: the verified remedy.",
      "remedy_verified": true,
      "verification_tier": 2,
      "paper_only": false,
      "check_nature": "deterministic | deterministic_with_tolerance | judgement",
      "unverifiable_reason": null,
      "blocked_tier": null,
      "unverifiable_basis": null,
      "locus": "data | cleaning | analysis_code | estimation | inference | sample | reporting | figure | text | environment | other",
      "evidence": {
        "artifact": "pkg/data/study1.csv",
        "operation": "What was computed, in one sentence.",
        "expected": "The value the article reports.",
        "observed": "The value the deposit yields.",
        "tolerance_note": "How a difference was judged against the printed precision, or null.",
        "script": "evidence/check_table2.py"
      },
      "benign_explanations_ruled_out": ["Rounding of the printed value", "A different exclusion rule"]
    }
  ],
  "tested_negatives": [
    {"hypothesis": "A test of the paper that did not confirm a problem.", "result": "What the test showed."}
  ],
  "passed_checks": ["A check that passed, in one line."]
}
```

## Fields

- **package.reproduces_headline**: did the headline published number come back out? `exact`, `within_rounding`,
  `qualitatively`, `no`, `not_attempted`. This is separate from `runs_as_shipped`: code can run cleanly and produce
  different numbers.
- **package.runs_as_shipped**: whether the deposited code runs as deposited: `yes`, `no`, `partial`, or
  `not_attempted` when there is no code or it was not run.
- **id**: `F1` to `Fn`, in Appendix A order. Appendix A row k is finding Fk. An id appears only here and in the
  finding's invisible marker in REPORT_final.md (`<!-- finding:Fk -->`), never in the visible text.
- **claim**: the Appendix A Finding cell, word for word.
- **section**: the body section that treats the finding, such as `Section 3` or `Section 7` for a minor issue.
- **confidence**: the Appendix A Status cell: CONFIRMED, LIKELY or SPECULATIVE.
- **impact**: the impact ladder in SCHEME.md (Field 1). `value_wrong` means a reported value is wrong but a reader
  who saw the corrected value would draw the same conclusion.
- **changes_conclusion**: does this change a conclusion the paper actually draws? Usually false, and saying so plainly
  is what makes the true ones credible. When true, `changes_conclusion_which` names that conclusion in one sentence,
  and the Appendix A row says Yes.
- **disclosure_flag**: true for a finding that the deposit holds personal data (SCHEME.md, Field 3).
- **remedy_class** and **remedy_description**: the kind of fix and the one-sentence Remedy from the body.
  **remedy_verified**: was the remedy checked to be possible and sufficient? A recommendation is a claim.
- **paper_only**: is the finding established from the published article alone, with no package, data, code or
  environment? Required, true or false.
- **check_nature**: whether the check was deterministic, deterministic within a tolerance, or a judgement (SCHEME.md
  Rule 4); null if not stated.
- **unverifiable_reason**, **blocked_tier**, **unverifiable_basis**: required when impact is `unverifiable`, null
  otherwise (SCHEME.md Rules 2b and 2c: an unverifiable claim without a cause is not a finding).
  `unverifiable_reason` is one of `data_missing`, `data_not_available`, `code_missing`, `code_not_functional`,
  `discrepancy_in_output`, `software_not_available`, `insufficient_compute`, `missing_readme`,
  `insufficient_documentation`. `blocked_tier` is the tier (1 to 5) of the check that could not be run, not the cost
  of finding that out. `unverifiable_basis` says what was sought, what was attempted and why the check stays blocked,
  without speculating about absent materials.
- **verification_tier**: SCHEME.md Field 2, the cost for a second person to verify the finding (1 to 5), not its
  severity.
- **evidence**: the artefact (a package- or paper-relative path), the operation, the expected and observed values,
  `tolerance_note` (how a difference was judged against the printed precision, a string or null), and the script
  under `evidence/` that reproduces it.
- **remedy_verified** must be present in every finding (true, false or null).
