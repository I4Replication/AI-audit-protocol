# Offline optimization benchmark

Use `scripts/benchmark_optimization.py` to compare completed baseline and optimized audit records.
It reads local measurements only and never invokes a model provider. The input is a JSON object with
`schema_version: "i4r-benchmark-input-1"` and a `records` array. Each record supplies:

- a stable `case_id`, `source` (`real` or `seeded`), `measurement_status` (`measured`,
  `projected`, or `unmeasured`), and covered failure domains;
- ground-truth substantive, conclusion-changing, and disclosure-critical finding IDs;
- baseline and optimized token totals, wall-clock seconds, reported finding IDs, five telemetry
  coverage fractions, accepted stale/partial-output count, and failed settled-cache spot-check count.

Each baseline and optimized run also carries `operational_metrics`: five Phase-1 turn counts, Claude
cache-read tokens, accounted tokens, failed billable tokens, and billable tokens attributed to local
preflight or schema admission. Unknown historical values remain `null`; they are never imputed.

Every run in a `measured` pair must carry a `measurement_manifest` reference containing the relative
path and SHA-256 of an `i4r-benchmark-measurement-manifest-1` file. That manifest binds the exact
measurement object, canonical `run.events.jsonl`, canonical `run_metrics.json`, journal sequence/hash
watermark, and at least one accepted output artifact. The harness verifies every file hash, the full
journal hash chain, and agreement between the journal and metrics watermark before including the case
in any efficiency or non-inferiority statistic. Missing, altered, stale, absolute, or escaping
provenance fails input validation; a JSON claim alone is not a measurement.

The harness retains at most eight real cases in input order. Add the bundled unmeasured failure cases
when fewer representative audits are available:

```bash
python3 scripts/benchmark_optimization.py measured-benchmark.json \
  --seeded-fixtures config/benchmark-seeded-fixtures.json \
  --output-json benchmark-result.json \
  --output-markdown benchmark-result.md \
  --require-complete --require-gates
```

Populate the seeded records' `baseline` and `optimized` measurements after running both workflows.
Null measurements stay visible as incomplete and make the overall result `indeterminate`; the harness
does not impute speed, token, recall, or precision results. `--require-complete` fails on any retained
unmeasured case. `--require-gates` fails unless all accuracy, safety, telemetry, token, and wall-clock
release gates pass.

The principal regression fixture (audit A01) can be checked without a provider call:

```bash
python3 scripts/benchmark_optimization.py tests/fixtures/regression-benchmark-replay.json \
  --output-json /tmp/regression-benchmark-result.json --require-gates
```

This command intentionally exits nonzero. It is a seeded/projected regression case: the historical
baseline is retained, but `optimized` is `null` until a completed optimized audit has canonical,
hash-bound artifacts. Its status is therefore `indeterminate`; it cannot be used to claim a measured
token or wall-clock reduction.

The primary efficiency statistics are medians of paired per-case percentage reductions. Accuracy is
micro-aggregated over stable finding IDs, with optimized recall required to remain within two
percentage points of baseline, precision required not to decrease, and zero missed conclusion- or
disclosure-critical ground-truth IDs.
The operational gates additionally require a median of at most 45 Phase-1 turns, at least 50%
fewer Claude cache-read tokens, failed billable tokens no greater than 10% of accounted tokens, and
zero billable tokens for provider-free preflight or rejected schema admission.
