# Critique prompt interface

Prompts are paper-agnostic and contain no worked scientific examples. Runtime substitution owns all
paper/run-specific text:

| Token | Source | If missing |
|---|---|---|
| `{{PAPER}}` | `PAPER.txt` | hard error |
| `{{ENV}}` | `ENV.txt` | explicit generic default |
| `{{SLOW}}` | `SLOW.txt` | explicit generic default |
| `{{SETTLED}}` | validated settled-facts projection | empty-set statement |
| `{{WRAPUP}}` | configured round target/cap guidance | hard error |
| `${ROUND}` | critique round number | hard error |

Critics run read-only and return one JSON object following `schemas/v2/critic-result.schema.json`.
The launcher validates it, renders Markdown deterministically, and atomically publishes the pair;
final Markdown ends with `<!-- I4R_CRITIC_COMPLETE -->`. The canonical findings runtime registry is JSONL with one
row conforming to `schemas/v2/finding-row.schema.json`; it retains every candidate and tested
negative with an INCLUDED/MERGED/EXCLUDED ledger disposition. Only coordinator-approved INCLUDED
rows project into the comment and Appendix A. `schemas/v2/findings-registry.schema.json` is only an
optional export envelope.

Routine synthesis has no prompt or model call. The runtime writes `CLAIM_MERGE.json`
deterministically from the validated critic records. When the policy trigger packet is nonempty, the
runtime generates a targeted adjudication prompt directly from only the unresolved matrix rows and
their immutable bindings; no dynamic critic-count token or full-critique synthesis interface remains.

When porting prompts, rewrite any paper-specific instruction rather than keyword-sanitizing it. Read
one rendered prompt end to end before the first round and reject any unresolved token or assertion
that a nonstandard artifact exists.
