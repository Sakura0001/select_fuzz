# PQ-only query generation implementation plan

**Goal:** Remove automatic query construction routes rejected by the desktop PQ specification; admit measured/tested PQ queries only with fresh plan evidence.

**Source:** `/Users/yuyu/Desktop/pq/粘贴的 markdown (1)。md(9)`, sections 2, 7, 9 and the appended limitations. Where the two support lists conflict, use their conservative intersection. SQL syntax alone cannot promise worker availability or bypass server hard gates.

**Architecture:** Tighten existing generators in place, preserving seeded schemas and all three main modes plus the independent PQ harness. Keep supported scans, numeric/date/control expressions, basic aggregates, bounded inner joins, derived aggregation and unions. Reject unsupported explicit/custom generation paths instead of silently executing serial SQL. Place runtime admission on the existing query sessions and retain exclusion evidence. Setup, metadata probes and the intentional serial/baseline comparison arms are not PQ workload queries.

**Tech stack:** Python 3.11, existing grammar expansion and typed schema models, mysql-connector-python, pytest.

The current checkout includes extensive untracked user work. Work here without git reset/clean, automatic commits or checkout changes. Pre-change files are copied to `artifacts/pq-only-20260908/baseline/` for review and rollback.

## Tasks

- [x] Grammar: first add regressions showing unsupported default routes; prune window/ROLLUP/recursive/lateral/constant-query roots, unsupported scalar/aggregate families, non-ordered LIMIT and unsuitable relations. Enforce PQ admissibility for explicit/custom grammars and prevent unsupported columns from being selected. Run affected generation tests and a bounded seed matrix.
- [x] Load-shaped queries and random schemas: reproduce BIT_XOR/CRC32/window paths and TEXT/BLOB columns; remove those alternatives and keep supported scans, aggregation and bounded joins. Run composition and schema tests.
- [x] Independent PQ generator: inspect every builder against the document; remove unsupported scalar-only construction routes, eliminate the non-PQ first-attempt branch, keep useful supported operator variation, and reject unsuitable schemas. Update shape contracts with regression tests before fixes. Preserve fresh plan and runtime fallback gates for conditional IN/LEFT JOIN support.
- [x] Main execution admission: reproduce execution of a SELECT whose PQ-side EXPLAIN has no marker. Gate on the same session before workload execution; preserve timeouts and cleanup, distinguish rejected plans from database bugs, and exclude non-PQ performance results. Add bounded rejection handling so an endpoint with no PQ cannot cause infinite generation. Replay now applies the same evidence contract.
- [x] Review/report: update user-facing scope and commands; run targeted tests followed by the offline suite, inspect the integrated diff and record remaining live-validation limits.

## Verification

```sh
.venv/bin/python -m pytest tests/pq -q
.venv/bin/python -m pytest tests/generation tests/modes/fuzz tests/execution tests/performance tests/service -q -m 'not mysql and not mysql_performance and not online and not soak'
.venv/bin/python -m pytest tests -q -m 'not mysql and not mysql_performance and not online and not soak'
```

New tests must fail for the expected missing contract before each implementation change. Existing tests asserting deliberately removed operators must be updated to assert rejection or the retained supported behavior. Do not weaken unrelated semantic, cardinality, determinism, result-comparison, or lifecycle tests.

Initial PQ-only result: 1957 passed, 17 failed, 15 deselected. The 17 PQ oracle failures were reproduced from the task baseline and are unchanged. Ruff and whitespace checks pass; the full strict mypy error set is identical to the 27 baseline errors. A 15,000-candidate static sweep reaches all 56 retained grammar productions. No database was contacted. See `docs/testing/pq-query-contract.md` and `artifacts/pq-only-20260908/` for evidence and rollback hashes.

## Rollback

Restore only changed pre-existing files from the task baseline, preserving later user edits. Remove only newly introduced task files if reverting. No existing database or artifacts are deleted by this change.

## Follow-up: use preconfigured parameters

The user clarified that every database parameter is already configured and must not be changed during tests. This supersedes the earlier automatic session setup: admission remains read-only; main session initialization, generated setup/replay prologues, server timeout overrides, PQ DOP/threshold/fallback setters and automatic DOP sweeps are removed. The independent PQ harness uses explicitly configured parallel and serial endpoints. Client deadlines, transaction control and diagnostic user-variable tags retain their execution roles. The checkpoint for this follow-up is `artifacts/pq-preconfigured-20260908/baseline/`.

Follow-up verification: 1994 passed, the same 17 oracle failures, 15 deselected; Ruff and whitespace checks passed. Strict mypy reports 23 existing errors and no new diagnostic. No database connection or parameter change was performed.
