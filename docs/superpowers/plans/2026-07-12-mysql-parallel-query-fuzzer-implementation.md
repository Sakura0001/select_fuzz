# MySQL Parallel Query Fuzzer Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved Python/FastAPI/React MySQL 8.0.41 three-node correctness and performance fuzzing product, prove it locally, and complete the continuous 12-hour online query-shape discovery and generator-completion acceptance loop.

**Architecture:** A Python package owns deterministic generation, execution, oracle, calibration, artifacts, replay, and research tooling. FastAPI supervises isolated run subprocesses and serves a React SPA over REST plus resumable SSE; JSONL and per-case artifacts remain authoritative while a rebuildable index supports UI queries.

**Tech Stack:** Python 3.11+, pytest, Hypothesis, Pydantic, Typer, mysql-connector-python, FastAPI, SQLite, React 19, TypeScript, Vite, TanStack Query/Router, Vitest, Testing Library, Playwright, axe.

---

## Plan Set

Execute these detailed plans in dependency order:

1. `docs/superpowers/plans/2026-07-12-mysql-fuzzer-core-correctness.md`
2. `docs/superpowers/plans/2026-07-12-mysql-fuzzer-performance.md`
3. `docs/superpowers/plans/2026-07-12-mysql-fuzzer-control-plane-ui.md`
4. `docs/superpowers/plans/2026-07-12-mysql-fuzzer-validation-12h.md`

Use `docs/testing/mysql-parallel-query-fuzzer-test-plan.md` as the cross-plan release matrix. No subplan may weaken a user-approved decision in the design specification.

## Branch and Commit Policy

- Implementation branch: `codex/mysql-parallel-query-fuzzer`.
- Worktree: `.worktrees/mysql-parallel-query-fuzzer`.
- Write tests first for every behavior and bug fix.
- Stage only files from the active task.
- Commit each independently passing task.
- If a remote appears, push every commit immediately; while no remote exists, retain local commits and list them as pending push.
- Never persist the local MySQL password; use `SELECT_FUZZ_MYSQL_PASSWORD` only in process environment.

## Wave 1: Foundation and Correctness Vertical Slice

- [ ] Execute the core/correctness plan through project scaffolding, domain models, configuration, deterministic seed derivation, and coverage scheduling.
- [ ] Run `ruff check . && mypy src && pytest -q tests/unit tests/property` and record the exact counts.
- [ ] Continue the core/correctness plan through schema/data/query generation, read-only validation, three-node execution, oracle, artifacts, replay, and CLI.
- [ ] Run the same SQL against three logical roles on the discovered local MySQL 8.0.45 instance for the first real vertical slice.
- [ ] Prove controlled result/error/timeout injection is classified correctly.
- [ ] Commit the vertical-slice checkpoint and record the commit in `artifacts/build/checkpoints.jsonl` during execution; the runtime artifact remains ignored by Git.

Wave 1 exit gate:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q tests/unit tests/property tests/integration -m "not soak"
uv run python -m select_fuzz doctor --mode correctness --config config/local.yaml
uv run python -m select_fuzz run --mode correctness --config config/local.yaml --rounds 1 --queries-per-round 100
```

Expected: static checks and tests exit 0; doctor reaches all three roles; the smoke run creates one retained database per role and a parseable report.

## Wave 2: Performance Pipeline

- [ ] Execute the performance plan after the shared executor, timeout controller, artifacts, and domain interfaces are stable.
- [ ] Verify EXPLAIN TREE fixtures from MySQL 8.0.41, including scientific notation and killed partial output.
- [ ] Run deterministic fake-clock/barrier tests for start skew and timeout classification.
- [ ] Run local MySQL scan, join, aggregation, filesort, and window calibration smoke workloads.
- [ ] Verify formal scoring executes each role once, records `cache_state_unverified`, and emits only `PERF_ALERT`.
- [ ] Commit the performance checkpoint.

Wave 2 exit gate:

```bash
uv run pytest -q tests/performance tests/integration/test_performance_mysql.py
uv run python -m select_fuzz run --mode performance --config config/local.yaml --rounds 1 --queries-per-round 3
```

Expected: calibration places baseline/off in the configured window or explicitly classifies the template as uncalibratable; no partial plan is scored.

## Wave 3: Control Plane and React SPA

- [ ] Execute the control-plane/UI plan through FastAPI run supervision, durable state, REST contract, Problem Details, SSE sequencing, and report/replay APIs.
- [ ] Execute the React tasks through overview, run creation/detail, findings, replay, and report pages.
- [ ] Run component tests for loading, empty, stale, data, and error states.
- [ ] Run mock-backend Playwright tests with SSE duplication, reordering, loss, reconnect, and expired cursors.
- [ ] Run real-backend Playwright smoke against the local MySQL-backed FastAPI service.
- [ ] Perform browser visual QA of every page at desktop and narrow viewport widths.
- [ ] Commit the control-plane/UI checkpoint.

Wave 3 exit gate:

```bash
uv run pytest -q tests/control_plane
npm --prefix frontend test -- --run --coverage
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend run e2e:mock
npm --prefix frontend run e2e:real
```

Expected: all commands exit 0, frontend branch coverage is at least 85%, and axe reports no serious/critical issue.

## Wave 4: Local Three-Instance Validation and Fault Injection

- [ ] Execute the local-cluster and soak tasks from the validation plan.
- [ ] Start three isolated 8.0.45 instances for fast local validation using distinct ports, sockets, datadirs, tmpdirs, pids, and logs.
- [ ] Acquire an exact MySQL 8.0.41 binary or image, verify its version, and repeat the cluster suite.
- [ ] Run every enabled schema profile and query family from the test matrix.
- [ ] Inject single-node data, error, timeout, connection, process, and report-storage faults.
- [ ] Run the ten-minute accelerated soak and verify recovery/resource gates.
- [ ] Run at least 50,000 correctness queries on three identical 8.0.41 nodes with zero confirmed false positives.
- [ ] Commit all validated fixtures, scripts, and regression seeds; do not commit generated databases or reports.

Wave 4 exit gate:

```bash
uv run pytest -q -m mysql tests/integration
uv run pytest -q -m soak tests/soak --soak-duration=600
uv run python -m select_fuzz run --mode correctness --config config/local-8041.yaml --rounds 50 --queries-per-round 1000
```

Expected: all injected differences are classified, all infrastructure faults recover, no target query remains active after cancellation grace, and the identical-node run has zero confirmed false positives.

## Wave 5: Independent Code Review and Fixes

- [ ] Invoke `superpowers:requesting-code-review` for the complete branch.
- [ ] Review correctness, race conditions, connection/KILL reuse, resource leaks, secret handling, SQL safety, report crash consistency, API idempotency, SSE recovery, accessibility, and missing tests.
- [ ] Invoke `superpowers:receiving-code-review` before applying feedback.
- [ ] Fix every Critical and Important finding with a failing regression test first.
- [ ] Re-run the full focused suite after each fix batch.
- [ ] Commit review fixes as narrowly scoped commits.

## Wave 6: Continuous 12-Hour Active Completion Loop

- [ ] Start from a clean committed epoch and record Git HEAD, generator version, source cursor, three-node versions/config fingerprints, and seed state.
- [ ] Execute `docs/testing/12h-sql-coverage-runbook.md` continuously for 12 hours.
- [ ] Search official/version-proven sources, isolate candidates, extract signatures, and audit static plus directed reachability.
- [ ] For every missing/unreachable valid 8.0.41 shape, create a dedicated worktree, prove a failing test, implement the minimum rule, run all gates, commit, and restart a new soak epoch.
- [ ] Checkpoint every 30 minutes and exclude interruptions from the 12-hour contiguous timer.
- [ ] Freeze code changes for the final 30 minutes while continuing discovery, regression, and report generation.
- [ ] Produce the source manifest, signature corpus, coverage matrix, gap ledger, saturation curve, commit mapping, soak report, and final HTML/JSONL reports.

## Wave 7: Final Verification and Delivery

- [ ] Invoke `superpowers:verification-before-completion`.
- [ ] Run every command below and retain its full output.

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q --cov=select_fuzz --cov-report=term-missing --cov-fail-under=90
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend test -- --run --coverage
npm --prefix frontend run build
npm --prefix frontend run e2e:mock
npm --prefix frontend run e2e:real
uv run pytest -q -m mysql tests/integration
uv run pytest -q -m soak tests/soak --soak-duration=600
git diff --check
git status --short
```

- [ ] Confirm no tracked file contains credentials, generated reports, local datadirs, or `.env`.
- [ ] Invoke `superpowers:finishing-a-development-branch` and integrate according to the approved local-commit workflow.
- [ ] Commit any final documentation-only verification records separately.
- [ ] Push when a remote is configured; otherwise report the exact local commit range pending push.
