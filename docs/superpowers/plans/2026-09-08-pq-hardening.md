# PQ hardening implementation plan

**Goal:** Execute PQ-qualified SQL in Fast, differential, and performance modes; preserve evidence and distinguish engine defects from unsupported plans, fallback, incomplete results, and documented precision/order differences.

**Architecture:** Keep `select_fuzz.pq` and its three public mode entry points. Share plan validation and execution through `DifferentialRunner`; separate SQL generation, comparison, connection/resource handling, and reports. Work in the current checkout because the existing PQ module is untracked user work. A full pre-change copy is saved at `artifacts/pq-hardening-20260908/baseline/pq`.

**Tech stack:** Python 3.11, mysql-connector-python, pytest, Typer.

## Evidence and decisions

- Read `/Users/yuyu/Desktop/PQ/粘贴的 markdown (1)。md(9)` in full. Its whitelist and older support list conflict on DISTINCT aggregates and some joins/functions; use their conservative intersection for generated SQL, leaving conditional support to curated probes and EXPLAIN.
- `force_parallel_execute=ON` cannot override hard gates. Do not change global server parameters. Record actual session variables and unsupported optional switches.
- EXPLAIN is planning evidence, not a guarantee that workers executed. Check both PQ and serial plans at execution; suppress fail-retry/graceful fallback per session where supported; capture fallback warnings immediately after execution. Exclude fallback and unknown/incomplete evidence from comparisons and performance records.
- The source permits small precision changes in function intermediate storage but specifies no numerical epsilon. Keep integer, NULL, raw Decimal and string comparison exact. Permit configurable Decimal expression tolerance only for generator-declared expression columns; record tolerated precision differences separately. FLOAT/DOUBLE use explicit configurable absolute/relative tolerances with multiset matching, never rounded string keys.
- Generated ORDER BY/LIMIT must have a total order. Unordered SQL uses full multisets with duplicate counts. Ambiguous curated LIMIT is inconclusive unless supplied an explicit comparison contract.
- Never drop an existing database on setup. CREATE a dedicated new schema, emit deterministic complete setup SQL and explicit cleanup SQL. Timeout, fetch budget, query count, retry count, DOP and resource cleanup are bounded.

## Implementation tasks

- [ ] Connection/config/detector/runner: add validation and safe defaults, structured plan evidence, correct marker matching, serial gate, immediate warning/fallback capture, complete-result accounting, reference connections without PQ variables, timeouts, and cleanup on partial initialization. Regression tests: plan loss, serial plan PQ, false marker positives, truncation, fetch errors, warning ordering.
- [ ] Generator: ONLY_FULL_GROUP_BY-safe selection, valid numeric domains, deterministic GROUP/UNION/LIMIT ordering, bounded joins, non-overlapping regeneration seeds, conservative whitelist and a PQ-friendly same-shape retry. Tests on fixed seeds and large seed ranges.
- [ ] Oracle: exact typed comparison; multiplicity-preserving tolerance-aware multiset matching with a bounded work budget; precision variance/inconclusive verdicts; order/category diagnostics. Tests include boundary tolerance, Decimal context loss, NULL/string/numeric differences and duplicate matching.
- [ ] Modes: shared retry accounting; log all attempts and skip causes; actual two/three-way comparisons with identical deterministic fixtures; balanced performance sampling with re-gating each sample, full result checks, requested/planned DOP and scan estimates; exclude invalid samples. Save CSV/JSONL summaries and complete reproducibility bundles.
- [ ] CLI/docs: local default, environment-only example credentials, configurable budgets/tolerance/reference/DOP sweep; explain reported metrics and limitations. Record verified findings with evidence and rollback commands.
- [ ] Verification: observe regression tests fail before fixes; run focused tests and existing offline suite; review integrated code. If the user identifies an authorized live endpoint, run bounded Fast/Compare/Performance campaigns, repeat candidates, reduce SQL/data while retaining PQ evidence, and report stable results without asserting unverified engine internals.

## Shared contracts

- `QueryResult` retains existing constructor fields and adds `complete=True`, `total_rows=None`, `warnings=()`, `fallback=False`, `warnings_complete=True`.
- `ExplainInfo` retains `text`, `triggered`, adding `error=''`, `dop=0`, `scan_rows=None` (estimate), `rows=()`, `columns=()`.
- `DifferentialResult` retains existing fields and adds `seq_explain_text`, `skip_reason`, `seed`, and precision metadata. `is_mismatch` means a substantive mismatch only; inconclusive and precision observations are separate.
- `runner.run_differential(sql, compare_mode=..., decimal_columns=(), reverse=False)` checks fresh PQ/serial plans before execution. It returns a result with `outcome.verdict='INCONCLUSIVE'` and `skip_reason` without SQL execution if either plan gate fails. `reverse` alternates execution order. `runner.explain(sql, serial=False)` and `runner.set_dop(dop)` support performance sweeps.
- `compare_result_sets(..., decimal_columns=())` uses `Tolerance.decimal_absolute` / `decimal_relative` only in these result-column positions.
- `PQGeneratedQuery` gains `decimal_columns=()`; `PQGenerator.generate(seed=..., shape=None, pq_friendly=False)` preserves public compatibility while permitting same-shape retries.
- `PQConfig` gains `perf_dops=()`, `perf_warmups=1`, `max_duration_seconds=600.0`; existing fields remain. Materialization and mode reports use a deterministic setup writer owned by the connection/config integration task.

## Verification commands

```sh
.venv/bin/python -m pytest tests/pq -q
.venv/bin/python -m pytest tests -m 'not mysql and not mysql_performance and not online and not soak' -q
.venv/bin/python -m select_fuzz.pq.cli --help
```

Live commands and reproduction ratios will be saved with the final report after the target is established. Live tests use a newly named schema for each campaign; existing user schemas and artifacts are preserved.
