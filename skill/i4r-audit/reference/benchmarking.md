# Offline optimization benchmark

Use `scripts/benchmark_optimization.py` to compare completed baseline and optimized audit records.
It reads local measurements only and never invokes a model provider. The input is a JSON object with
`schema_version: "i4r-benchmark-input-1"` and a `records` array. Each record supplies:

- a stable `case_id`, `source` (`real` or `seeded`), and covered failure domains;
- ground-truth substantive, conclusion-changing, and disclosure-critical finding IDs;
- baseline and optimized token totals, wall-clock seconds, reported finding IDs, five telemetry
  coverage fractions, accepted stale/partial-output count, and failed settled-cache spot-check count.

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

The primary efficiency statistics are medians of paired per-case percentage reductions. Accuracy is
micro-aggregated over stable finding IDs, with optimized recall required to remain within two
percentage points of baseline, precision required not to decrease, and zero missed conclusion- or
disclosure-critical ground-truth IDs.
