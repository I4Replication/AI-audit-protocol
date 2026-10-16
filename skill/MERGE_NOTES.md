# i4r-audit unified — merge notes (G-2026-08-21)

Merged from branch A (one team member's skill folder, superset pipeline) and branch B (another
team member's skill folder, report format + run accounting), on the decisions of the two
2026-08-21 meetings. Both parents are untouched; audit this table against them.

| Piece | Won | Why |
|---|---|---|
| Report format | **B** | Page 1 = executive summary ≤200 words + findings table + files table; hard cap 10 rendered pages, measured on the built PDF. The findings table's row numbers ARE the claim numbers — one structure for editor triage, the claims script, and the landing-page verdict survey |
| `reference/report_structure.md`, `adjudication_guide.md`, `prompts/common.txt` | **B**, + A's claim-tags section | B's common.txt is a strict superset of A's; B's adjudication guide propagates corrections to page 1 *and both tables* |
| `build_docx.sh` | **B base + A graft** | B's pandoc-PATH resolution and measured page count (LibreOffice, honestly-labelled xelatex proxy) + A's `[claim N]` tag stripping with count |
| `check_consistency.sh` | **B base + A §1 + new §4** | B's summary-page gate and normalized retired-claims matching (catches rephrasings); A's §1 disclosure detector (B's still carried the underscore-blind `\b` — mechanism M2, disclosed live in the protocol paper); new §4 blocks when findings-table rows and body claim tags disagree (self-tested both ways) |
| `run_critique.sh` | **A base + B policy** | A's pre-flight model probe (a rejected model burned a full round, twice) + NEW out-of-credits probe (ate round 4 of audit A40); B's `EFFORT=max` and env-name fallbacks (`MODEL`/`EFFORT` accepted) |
| Round policy | **B** | Production default 2 rounds (round 2 reviews round 1's own corrections — 43%/28%/8% of final words were post-round-1 text in the three measured comments), raisable to the 5-cap for contested audits. Stopping at 2 is recorded as the production default, never as convergence |
| Models | **Team decision** | Coordinator Opus 5; sub-agents Sonnet 5 at effort max; critics gpt-5.6-terra at max |
| `run_meta.sh`, `verify_codex_model.sh` | **B, adopted whole** | Phase wall-clock + tokens = the protocol paper's Q17; served-model verification per session. Division of labor: `log_event.sh` remains the schema-validated audit record, `run_meta.sh` the cost meter |
| Registry pipeline (`schemas/`, `init_run.sh`, `log_event.sh`, `finalize_run.sh`) | **A, whole** | Machine-readable run.json + comment.json per audit — what makes the meta paper computable. No B equivalent |
| Verification notebook (`make_verification_notebook.sh`, guide, worked example) | **A, whole** | The R artifact replicators actually run; finalize gates on it |
| `make_claims_script.sh` | **A, rewritten language-aware** | Claims file takes the DEPOSIT's language (`.do`, `.sps`, …) with author code inline-executable; audit R checks embedded as marked run-in-R blocks when the file isn't R; **R fallback when no code ships** (decision 2026-08-21). Severity mapped mechanically from comment.json. 12-check selftest |
| `publish_completed.sh` | **New** | Files final PDF (hash-verified) + claims script — exactly two files — into `Completed reports/<Journal>/<Paper>/`. A stray file in the destination blocks the publish |
| `stage_release.sh` | **A** | Keeps the package-specific disclosure-gate hook branch B dropped |
| SKILL.md | **A base** | Superset failure-mode library (audit A40: a fabricated value, the false-clean notebook chunk, out-of-credits; audit A42) + Phase 3b + B's models/effort section + the new format rules + tag G-2026-08-21 + the discipline-agnostic rule |

## Known seams
- The page-1 format gate is validated on fixtures; no real comment uses the format yet. First real
  exercise is the next audit.
- `run_meta.sh` and `log_event.sh` overlap on intent; unify onto one event stream when someone has a
  quiet afternoon.
- Old comments (15 in the registry) predate the page-1 format and the claims-script handoff; they
  are NOT republished (team decision: no backfill).

## Format revision, 2026-08-22

Branch B's page-1 format (executive summary + two summary tables on the opening page) was exercised
for the first time on audit A43 and was rejected on delivery. It does not read like an
academic comment: a comment is a paper, and a reader should meet the argument in prose before an
inventory table. Reverted toward branch A's structure, keeping branch B's ten-page cap and its measured
page-count gate:

| Piece | Now |
|---|---|
| Opening | `## Abstract`, ≤250 words, prose only. No executive summary |
| §1 | Introduction (was: scope and method as a bare heading) |
| Findings table | **Appendix A**, last page. Blocked anywhere earlier by `check_consistency.sh` §3a-ii |
| Last numbered section | Conclusion |
| Front matter | Unchanged — the Original study and Replication package lines were the part that worked |
| Page cap | Unchanged at 10, measured on the built document |

`check_consistency.sh` §3a now gates the abstract (≤250 words, no table) and §4 reads findings-table
rows from the appendix rather than page 1. Both directions self-tested against planted violations.
Backups: `reference/report_structure.md.bak`, `scripts/check_consistency.sh.bak`, `SKILL.md.bak`.

## Addendum 2026-08-24 — team-run features

Added for the six-researcher concurrent run:
- **`reserve_paper.sh`** — advisory paper reservations in `<root>/reservations/`, Dropbox-race-aware
  (conflict copies detected, earliest `claimed_at` wins; 7-day STALE flags; 8-check selftest).
  `publish_completed.sh` releases the reservation when the audit is filed.
- **`capture_environment.sh`** + `run.json` `environment` field (schema extended) — OS, R + notebook
  packages, Python + readers, pandoc, LibreOffice, Codex CLI versions recorded at init; finalize
  warns when missing. Captured live, never reconstructed.
- **`CHEATSHEET.md`/`.pdf`** — team setup + run guide with verbatim captures and a timeline figure.

**Process note, on the record:** a 2026-08-24 Dropbox sync briefly overwrote the 2026-08-22 edits to
`SKILL.md` and `publish_completed.sh`. Both were recovered intact from this repository's history —
which is the argument for keeping the skill under version control rather than only in a synced
folder. The Dropbox copies now match the repo plus the additions above.
