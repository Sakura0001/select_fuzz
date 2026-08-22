# Retain Crash SQL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore MySQL 8.0.22 `EXISTS (TABLE ...)` crash-query execution while guaranteeing that a one-sided lost connection remains an infrastructure retry and never becomes a correctness finding.

**Architecture:** Remove only the generator-side known-crash rejection; retain the read-only safety validator and all MySQL 8.0.22 grammar productions. Lock the existing execution boundary in place with round-engine characterization tests: any `INFRA_ERROR` is durably logged before oracle classification, retried after connection recovery, and excluded from `findings/`.

**Tech Stack:** Python 3.11, pytest, Pydantic domain models, MySQL Connector/Python, append-only fsynced JSONL artifacts.

---

## File map

- Modify `src/select_fuzz/generation/query_grammar.py`: remove the canonical-grammar fingerprint and `EXISTS (TABLE ...)` crash-shape rejection only.
- Modify `tests/generation/test_query_grammar.py`: convert the rejection regression into a generation-retention regression and restore unconditional fixed-seed generation loops.
- Modify `tests/service/test_round_engine.py`: characterize one-sided lost connection against both a successful peer and a normal database-error peer, including durable SQL/error logging and absence of findings.

### Task 1: Lock the lost-connection finding boundary

**Files:**
- Modify: `tests/service/test_round_engine.py`

- [ ] **Step 1: Add a one-sided lost-connection helper**

Add a helper beside `_infra_errors` so the test can identify the failing node precisely:

```python
def _lost_connection(role: NodeRole) -> NodeExecution:
    return NodeExecution.failure(
        role=role,
        status=ExecutionStatus.INFRA_ERROR,
        started_ns=10,
        ended_ns=20,
        connection_id=100 + list(COMPARISON_ROLES).index(role),
        error=ErrorInfo(2013, "HY000", "Lost connection to MySQL server during query"),
        connection_reusable=False,
        failure_evidence={
            "failure_stage": "execute",
            "exception": {"message": "socket reset by peer"},
        },
    )
```

- [ ] **Step 2: Add a parameterized round-engine characterization test**

Add this test beside the infrastructure retry tests:

```python
@pytest.mark.parametrize(
    "peer",
    [
        _success(NodeRole.CUSTOM_ON, ((1,), (2,))),
        NodeExecution.failure(
            role=NodeRole.CUSTOM_ON,
            status=ExecutionStatus.ERROR,
            started_ns=10,
            ended_ns=20,
            connection_id=101,
            error=ErrorInfo(1064, "42000", "syntax error"),
        ),
    ],
    ids=("peer_success", "peer_database_error"),
)
def test_one_sided_lost_connection_is_retried_without_finding(
    tmp_path: Path,
    peer: NodeExecution,
) -> None:
    query = _queries(1)[0]
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    coordinator = _RetryCoordinator(
        [(_lost_connection(NodeRole.CUSTOM_OFF), peer), _match()]
    )
    sink = _CollectSink()
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        coordinator,
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
        sleeper=lambda _: None,
    )

    summary = engine.run_round(
        _context(1), EventPublisher("run_engine_1", sink), Event()
    )

    assert summary.queries_completed == 1
    assert summary.findings == 0
    assert not tuple((tmp_path / "findings").glob("*/manifest.json"))
    assert coordinator.executed == [query.sql, query.sql]
    records = read_jsonl(tmp_path / "sql" / "worker-000.jsonl")
    assert records[0]["type"] == "query_attempt_started"
    assert records[0]["query_sql"] == query.sql
    failed = records[1]
    assert failed["verdict"] == "infrastructure_retry"
    assert failed["query_sql"] == query.sql
    assert failed["nodes"]["custom_off"]["status"] == "infra_error"
    assert failed["nodes"]["custom_off"]["error"]["errno"] == 2013
    assert failed["nodes"]["custom_off"]["failure_evidence"] == {
        "failure_stage": "execute",
        "exception": {"message": "socket reset by peer"},
    }
    pause = next(event for event in sink.events if event.kind == "infrastructure_pause")
    assert pause.payload["query_sql"] == query.sql
    round_sql = tmp_path / "rounds" / f"{materialized.database}.sql"
    assert query.sql in round_sql.read_text(encoding="utf-8")
```

- [ ] **Step 3: Run the characterization test**

Run:

```bash
uv run pytest -q tests/service/test_round_engine.py::test_one_sided_lost_connection_is_retried_without_finding
```

Expected: `2 passed`. This is an existing safety boundary, so it should be green before production changes.

### Task 2: Restore known crash-query generation with TDD

**Files:**
- Modify: `tests/generation/test_query_grammar.py`
- Modify: `src/select_fuzz/generation/query_grammar.py`

- [ ] **Step 1: Replace the rejection expectation with a retained-candidate expectation**

Replace `test_mysql_8022_generator_rejects_known_crashing_exists_table_shape` with:

```python
def test_mysql_8022_generator_keeps_known_crashing_exists_table_shape() -> None:
    candidate = GrammarQueryGenerator().generate(_schema(), seed=1)

    assert "EXISTS (TABLE" in candidate.sql
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run pytest -q tests/generation/test_query_grammar.py::test_mysql_8022_generator_keeps_known_crashing_exists_table_shape
```

Expected: FAIL because `GrammarQueryGenerator.generate` raises `CandidateRejected` with `MySQL 8.0.22 known-crashing EXISTS (TABLE ...) shape`.

- [ ] **Step 3: Remove only the crash-shape rejection**

In `src/select_fuzz/generation/query_grammar.py`:

- remove `from functools import cache`;
- remove `_MYSQL_8022_CRASHING_EXISTS_TABLE`;
- remove `_canonical_mysql_8022_grammar_sha256`;
- remove `self._uses_canonical_mysql_8022_grammar`;
- remove the conditional `CandidateRejected` block after `candidate` construction.

The end of `generate` must remain:

```python
        candidate = CandidateQuery(sql, seed, self.grammar.sha256, tuple(context.trace))
        try:
            self.validator.validate_text(sql)
        except UnsafeQuery as error:
            raise CandidateRejected(
                "candidate failed the read-only safety gate",
                candidate=candidate,
            ) from error
        return candidate
```

- [ ] **Step 4: Restore unconditional generation in broad fixed-seed tests**

In `tests/generation/test_query_grammar.py`, replace both crash-specific `try/except CandidateRejected` loops added by commit `02dc0ea` with direct calls:

```python
candidate = generator.generate(schema, seed=seed)
```

and:

```python
candidate = generator.generate(_schema(), seed=seed)
```

Keep the custom-grammar `EXISTS (TABLE ...)` witness test because it independently verifies the normal rendering path.

- [ ] **Step 5: Run generator tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/generation/test_query_grammar.py
```

Expected: all tests pass and fixed seed `1` returns a candidate containing `EXISTS (TABLE`.

### Task 3: Verify behavior and repository health

**Files:**
- Verify: `src/select_fuzz/generation/query_grammar.py`
- Verify: `tests/generation/test_query_grammar.py`
- Verify: `tests/service/test_round_engine.py`

- [ ] **Step 1: Run the focused regression set**

Run:

```bash
uv run pytest -q \
  tests/generation/test_query_grammar.py \
  tests/service/test_round_engine.py::test_one_sided_lost_connection_is_retried_without_finding \
  tests/service/test_round_engine.py::test_round_engine_logs_every_infrastructure_retry_attempt
```

Expected: all selected tests pass; the parameterized one-sided test contributes two cases.

- [ ] **Step 2: Run all tests**

Run:

```bash
uv run pytest -q
```

Expected: exit code 0 with no failed tests.

- [ ] **Step 3: Run static checks**

Run:

```bash
uv run ruff check .
uv run mypy src
```

Expected: Ruff exits 0 and mypy reports success.

- [ ] **Step 4: Build release artifacts**

Run:

```bash
uv build
```

Expected: exit code 0 and both source and wheel distributions are created under `dist/`.

- [ ] **Step 5: Inspect the final diff and commit**

Run:

```bash
git diff --check
git status --short
git diff -- src/select_fuzz/generation/query_grammar.py tests/generation/test_query_grammar.py tests/service/test_round_engine.py
git add src/select_fuzz/generation/query_grammar.py tests/generation/test_query_grammar.py tests/service/test_round_engine.py docs/superpowers/plans/2026-08-23-retain-crash-sql.md
git commit -m "fix: retain crash queries without false findings"
```

Expected: clean diff check and a commit containing only the planned implementation, tests, and implementation plan.

- [ ] **Step 6: Push the active branch**

Run:

```bash
git push origin codex/two-instance-comparison
```

Expected: `origin/codex/two-instance-comparison` advances to the new implementation commit.
