# Fuzz Small Data and Run Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support 20-row fuzz tables and print complete run/materialization/replica synchronization failure reasons.

**Architecture:** Keep configuration validation in `FuzzConfig`, enrich errors at the layer that owns the missing context, and let the CLI print the resulting exception string without a traceback. Materialization records replica probe context; the generation coordinator aggregates each database name, exception type, and exception message into both its exception and JSONL event.

**Tech Stack:** Python 3.11, Pydantic 2, Typer, pytest, Ruff, Mypy.

---

## File structure

- Modify `src/select_fuzz/config/models.py`: lower only the fuzz initial-row validation boundary.
- Modify `src/select_fuzz/cli.py`: include complete exception text in `run failed` output.
- Modify `src/select_fuzz/modes/fuzz/materialization.py`: explain replica marker timeout context.
- Modify `src/select_fuzz/modes/fuzz/service.py`: preserve per-database exception details.
- Modify `tests/config/test_loader.py`: cover the 20/19 row boundary.
- Modify `tests/cli/test_cli.py`: require raw exception text without a traceback.
- Create `tests/modes/fuzz/test_materialization.py`: cover both replica timeout outcomes.
- Modify `tests/modes/fuzz/test_service.py`: cover exception and JSONL aggregation.
- Modify `/private/tmp/select-fuzz-local-primary-replica-6db.yaml`: configure the approved local run; this file remains outside Git.

### Task 1: Accept 20-row fuzz tables

**Files:**
- Modify: `tests/config/test_loader.py`
- Modify: `src/select_fuzz/config/models.py:278`

- [ ] **Step 1: Write the failing boundary test**

```python
def test_fuzz_initial_rows_accepts_twenty_but_rejects_nineteen() -> None:
    assert FuzzConfig(
        initial_tables=1,
        initial_rows_per_table=20,
        max_rows_per_database=100,
    ).initial_rows_per_table == 20
    with pytest.raises(ValidationError):
        FuzzConfig(
            initial_tables=1,
            initial_rows_per_table=19,
            max_rows_per_database=100,
        )
```

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv/bin/pytest -q tests/config/test_loader.py::test_fuzz_initial_rows_accepts_twenty_but_rejects_nineteen`

Expected: FAIL because Pydantic currently requires at least 100 rows.

- [ ] **Step 3: Lower the validation boundary**

```python
initial_rows_per_table: int = Field(default=10_000, ge=20)
```

- [ ] **Step 4: Run configuration tests and verify GREEN**

Run: `.venv/bin/pytest -q tests/config/test_loader.py tests/modes/fuzz/test_models.py`

Expected: all tests pass.

- [ ] **Step 5: Commit the boundary change**

```bash
git add src/select_fuzz/config/models.py tests/config/test_loader.py
git commit -m "feat: allow 20-row fuzz tables"
```

### Task 2: Print complete run failure messages

**Files:**
- Modify: `tests/cli/test_cli.py:145-175`
- Modify: `src/select_fuzz/cli.py:180-184`

- [ ] **Step 1: Change the CLI test to require the original message**

```python
def test_run_cli_prints_runner_failure_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Existing FailingRunner raises RuntimeError("database setup failed in private test")
    assert result.exit_code == 1
    assert (
        "run failed: RuntimeError: database setup failed in private test"
        in result.output
    )
    assert "Traceback" not in result.output
```

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv/bin/pytest -q tests/cli/test_cli.py::test_run_cli_prints_runner_failure_without_traceback`

Expected: FAIL because the CLI prints only `RuntimeError`.

- [ ] **Step 3: Print exception type and string**

```python
except Exception as error:
    typer.echo(f"run failed: {type(error).__name__}: {error}", err=True)
    raise typer.Exit(code=1) from None
```

- [ ] **Step 4: Run CLI tests and verify GREEN**

Run: `.venv/bin/pytest -q tests/cli/test_cli.py`

Expected: all tests pass and no traceback is exposed.

- [ ] **Step 5: Commit the CLI behavior**

```bash
git add src/select_fuzz/cli.py tests/cli/test_cli.py
git commit -m "feat: print run failure details"
```

### Task 3: Explain replica synchronization timeouts

**Files:**
- Create: `tests/modes/fuzz/test_materialization.py`
- Modify: `src/select_fuzz/modes/fuzz/materialization.py:102-126`

- [ ] **Step 1: Add failing probe-error and marker-missing tests**

```python
def test_replica_timeout_reports_last_probe_exception() -> None:
    materializer = _materializer(
        _ProbeFactory(error=RuntimeError("replica route unavailable"))
    )
    with pytest.raises(
        TimeoutError,
        match=(
            r"replica synchronization timeout after 0.001 seconds; "
            r"database=sf_f_timeout; last probe error=RuntimeError: "
            r"replica route unavailable"
        ),
    ):
        materializer._wait_for_replica("sf_f_timeout")


def test_replica_timeout_reports_marker_not_visible() -> None:
    materializer = _materializer(_ProbeFactory(rows=()))
    with pytest.raises(
        TimeoutError,
        match=r"database=sf_f_timeout; replication marker not visible",
    ):
        materializer._wait_for_replica("sf_f_timeout")
```

The test helper supplies `replica_sync_timeout_seconds=0.001` and a no-op
sleeper. Its fake session returns either the configured rows or exception.

- [ ] **Step 2: Run the new tests and verify RED**

Run: `.venv/bin/pytest -q tests/modes/fuzz/test_materialization.py`

Expected: both tests fail because the current message omits duration, database,
and the raw probe message.

- [ ] **Step 3: Render complete timeout context**

```python
detail = "replication marker not visible"
if last_error is not None:
    detail = f"last probe error={type(last_error).__name__}: {last_error}"
raise TimeoutError(
    "replica synchronization timeout after "
    f"{self._replica_sync_timeout_seconds:g} seconds; "
    f"database={database}; {detail}"
)
```

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `.venv/bin/pytest -q tests/modes/fuzz/test_materialization.py`

Expected: both tests pass.

- [ ] **Step 5: Commit replica diagnostics**

```bash
git add src/select_fuzz/modes/fuzz/materialization.py tests/modes/fuzz/test_materialization.py
git commit -m "feat: explain replica synchronization timeouts"
```

### Task 4: Preserve per-database build errors

**Files:**
- Modify: `tests/modes/fuzz/test_service.py:500-555`
- Modify: `src/select_fuzz/modes/fuzz/service.py:110-123,455-497`

- [ ] **Step 1: Extend the failing service test**

```python
with pytest.raises(RuntimeError) as captured:
    service.run(
        RunRequest("run-fuzz-build-failure", "fuzz", 3, 1, None, 1),
        Event(),
    )

message = str(captured.value)
assert "database[0]=" in message
assert "database[1]=" in message
assert "RuntimeError: simulated kernel setup failure" in message

for item in failure["failures"]:
    assert item["database"].startswith("sf_f_")
    assert item["error_type"] == "RuntimeError"
    assert item["error"] == "simulated kernel setup failure"
```

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv/bin/pytest -q tests/modes/fuzz/test_service.py::test_generation_build_waits_for_all_failures_and_never_starts_workers`

Expected: FAIL because database names and exception strings are discarded.

- [ ] **Step 3: Store and render full failure tuples**

Use `(database_ordinal, database, error_type, error_message)` throughout
`_GenerationBuildError` and `_materialize_generation`:

```python
failures.append(
    (database_ordinal, database, type(error).__name__, str(error))
)
```

Render each entry as:

```python
f"database[{ordinal}]={database} {error_type}: {error_message}"
```

Write `database`, `error_type`, and `error` into every JSONL failure item.

- [ ] **Step 4: Run fuzz service tests and verify GREEN**

Run: `.venv/bin/pytest -q tests/modes/fuzz/test_service.py`

Expected: all tests pass.

- [ ] **Step 5: Commit build aggregation diagnostics**

```bash
git add src/select_fuzz/modes/fuzz/service.py tests/modes/fuzz/test_service.py
git commit -m "feat: preserve fuzz build failure reasons"
```

### Task 5: Configure and verify the local run

**Files:**
- Modify outside Git: `/private/tmp/select-fuzz-local-primary-replica-6db.yaml`

- [ ] **Step 1: Apply the approved small-data settings**

```yaml
initial_rows_per_table: 20
max_rows_per_database: 2000
batch_rows_min: 1
batch_rows_max: 5
delete_batch_rows_min: 1
delete_batch_rows_max: 2
```

- [ ] **Step 2: Run focused and static verification**

```bash
.venv/bin/pytest -q tests/config/test_loader.py tests/cli/test_cli.py tests/modes/fuzz/test_materialization.py tests/modes/fuzz/test_service.py
.venv/bin/ruff check src/select_fuzz/config/models.py src/select_fuzz/cli.py src/select_fuzz/modes/fuzz/materialization.py src/select_fuzz/modes/fuzz/service.py tests/config/test_loader.py tests/cli/test_cli.py tests/modes/fuzz/test_materialization.py tests/modes/fuzz/test_service.py
.venv/bin/mypy src tests/modes/fuzz/test_materialization.py
```

Expected: tests, Ruff, and Mypy pass.

- [ ] **Step 3: Run the full regression gate**

Run: `.venv/bin/pytest -q`

Expected: no failures.

- [ ] **Step 4: Validate the local configuration**

Run the existing local credential environment with:

```bash
.venv/bin/select-fuzz doctor --mode fuzz --config /private/tmp/select-fuzz-local-primary-replica-6db.yaml
```

Expected: `"can_start":true` and no fatal findings.

- [ ] **Step 5: Start and inspect the six-database run**

Run with a fresh numeric seed and artifact directory:

```bash
fuzz_diagnostic_seed=$(date +%s)
.venv/bin/select-fuzz run --mode fuzz \
  --config /private/tmp/select-fuzz-local-primary-replica-6db.yaml \
  --duration-seconds 600 --seed "$fuzz_diagnostic_seed" \
  --artifacts "/private/tmp/select-fuzz-local-6db-20rows-$fuzz_diagnostic_seed"
```

Expected: `fuzz_generation_ready` lists six new databases; primary has 24
writers and 24 readers, replica has 48 readers; stage snapshots do not show
sustained `waiting_for_generated_sql`.
