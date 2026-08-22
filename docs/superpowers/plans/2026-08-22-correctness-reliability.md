# Correctness Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make two-instance correctness runs reuse one accurately diagnosed session pair per round, eliminate infrastructure false findings, externalize large replay SQL, reject nondeterministic row limits, and exit promptly on fatal errors.

**Architecture:** Introduce shared bounded exception evidence and owned MySQL session leases, then let `PreparedRound` own a concurrently acquired pair for setup, EXPLAIN, SELECT, and mutation. Keep SQL value comparison strict while reporting connector-only metadata as advisories, and publish v2 finding bundles with streamed compressed SQL references.

**Tech Stack:** Python 3.11, mysql-connector-python, pytest, Pydantic configuration models, gzip/JSONL artifact storage, Docker MySQL 8.0.22.

---

### Task 1: Shared exception evidence and lossless internal errors

**Files:**
- Create: `src/select_fuzz/execution/evidence.py`
- Modify: `src/select_fuzz/modes/fuzz/forensics.py`
- Modify: `src/select_fuzz/execution/mysql.py`
- Modify: `src/select_fuzz/domain/models.py`
- Modify: `src/select_fuzz/artifacts/bundle.py`
- Test: `tests/execution/test_evidence.py`
- Test: `tests/execution/test_mysql_runner.py`
- Test: `tests/domain/test_models.py`

- [ ] **Step 1: Write failing evidence tests**

```python
def test_capture_exception_evidence_preserves_partial_connector_fields() -> None:
    error = _ConnectorError("raw connect failure", errno=2013, sqlstate=None, msg=None)
    evidence = capture_exception_evidence(error, "connect")
    assert evidence["stage"] == "connect"
    assert evidence["exception_type"] == "_ConnectorError"
    assert evidence["errno"] == 2013
    assert evidence["sqlstate"] is None
    assert "raw connect failure" in evidence["message"]

def test_internal_query_error_keeps_chinese_stage_and_original_text() -> None:
    result = NodeQueryRunner(_FailingFactory(ConnectionError("peer reset"))).run(
        CUSTOM_OFF, "sf_case", "SELECT 1", timeout_s=10, row_limit=10, byte_limit=1024
    )
    assert result.error is not None
    assert "查询会话建立失败" in result.error.message
    assert "peer reset" in result.error.message
    assert result.failure_evidence["stage"] == "query_session_connect"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/execution/test_evidence.py tests/execution/test_mysql_runner.py -q`

Expected: collection or assertion failures because shared evidence and `failure_evidence` do not exist.

- [ ] **Step 3: Implement bounded shared evidence**

```python
def capture_exception_evidence(error: BaseException, stage: str) -> dict[str, object]:
    return {
        "stage": stage,
        "exception_type": type(error).__name__,
        "message": _bounded_text(error),
        "repr": _bounded_repr(error),
        "errno": _optional_int(getattr(error, "errno", None)),
        "sqlstate": _optional_text(getattr(error, "sqlstate", None)),
        "connector_message": _optional_text(getattr(error, "msg", None)),
        "chain": _exception_chain(error),
        "traceback": _traceback_frames(error),
    }
```

Move the generic implementation from fuzz forensics into the shared module and re-export it from the old module. Add immutable `failure_evidence` to `NodeExecution`, serialize it in result artifacts, and render internal Chinese stage messages with the original exception text.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/execution/test_evidence.py tests/execution/test_mysql_runner.py tests/domain/test_models.py tests/modes/fuzz/test_forensics.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/select_fuzz/execution/evidence.py src/select_fuzz/modes/fuzz/forensics.py src/select_fuzz/execution/mysql.py src/select_fuzz/domain/models.py src/select_fuzz/artifacts/bundle.py tests/execution/test_evidence.py tests/execution/test_mysql_runner.py tests/domain/test_models.py
git commit -m "feat: preserve execution failure evidence"
```

### Task 2: Owned sessions, active registry, and concurrent pair acquisition

**Files:**
- Create: `src/select_fuzz/execution/sessions.py`
- Modify: `src/select_fuzz/execution/mysql.py`
- Modify: `src/select_fuzz/execution/protocols.py`
- Modify: `src/select_fuzz/execution/__init__.py`
- Test: `tests/execution/test_sessions.py`
- Test: `tests/execution/test_mysql_runner.py`

- [ ] **Step 1: Write failing session lifecycle tests**

```python
def test_pair_acquisition_attempts_both_roles_and_does_not_copy_failure() -> None:
    factory = _Factory({NodeRole.CUSTOM_OFF: ConnectionError("off down")})
    acquired = acquire_session_pair(NODES, "sf_case", factory)
    assert acquired.ready is False
    assert acquired.attempts[NodeRole.CUSTOM_OFF].evidence["message"] == "off down"
    assert acquired.attempts[NodeRole.CUSTOM_ON].opened is True
    assert acquired.attempts[NodeRole.CUSTOM_ON].evidence is None
    assert factory.sessions[NodeRole.CUSTOM_ON].closed is True

def test_abort_all_aborts_only_currently_registered_sessions() -> None:
    registry = ActiveSessionRegistry()
    first = _Session()
    second = _Session()
    registry.register(first)
    registry.register(second)
    registry.unregister(first)
    registry.abort_all()
    assert first.aborted is False
    assert second.aborted is True
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/execution/test_sessions.py -q`

Expected: import failures for `ActiveSessionRegistry` and `acquire_session_pair`.

- [ ] **Step 3: Implement session leases and concurrent acquisition**

```python
@dataclass(slots=True)
class SessionLease:
    session: QuerySession
    connection_id: int
    timings_ns: Mapping[str, int]
    _close: Callable[[], None]
    _closed: bool = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._close()

def acquire_session_pair(nodes, database, factory) -> PairSessionAcquisition:
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sf-pair-connect") as pool:
        futures = {node.role: pool.submit(_open_one, factory, node, database) for node in nodes}
        attempts = {role: _resolve_attempt(future) for role, future in futures.items()}
    if not all(attempt.opened for attempt in attempts.values()):
        for attempt in attempts.values():
            if attempt.lease is not None:
                attempt.lease.close()
    return PairSessionAcquisition(attempts)
```

Move UTC session initialization into `open_query_session()`, wrap it for existing `query_session()`, and register every live lease in `ActiveSessionRegistry`.

- [ ] **Step 4: Run focused session tests and verify GREEN**

Run: `uv run pytest tests/execution/test_sessions.py tests/execution/test_mysql_runner.py tests/execution/test_timeout.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/select_fuzz/execution/sessions.py src/select_fuzz/execution/mysql.py src/select_fuzz/execution/protocols.py src/select_fuzz/execution/__init__.py tests/execution/test_sessions.py tests/execution/test_mysql_runner.py
git commit -m "feat: acquire owned mysql session pairs"
```

### Task 3: Persistent PreparedRound sessions, setup classification, and start gate

**Files:**
- Modify: `src/select_fuzz/execution/setup.py`
- Modify: `src/select_fuzz/execution/triad.py`
- Modify: `src/select_fuzz/execution/mysql.py`
- Modify: `src/select_fuzz/correctness.py`
- Test: `tests/execution/test_triad.py`
- Test: `tests/execution/test_mysql_runner.py`
- Test: `tests/service/test_round_engine.py`

- [ ] **Step 1: Write failing classification and reuse tests**

```python
def test_one_sided_setup_infrastructure_failure_is_retryable_pause() -> None:
    results = {
        NodeRole.CUSTOM_OFF: _setup_success(),
        NodeRole.CUSTOM_ON: _setup_infra(2013, "lost connection"),
    }
    assert _statement_verdict(results, compare_affected_rows=False) is LockstepSetupVerdict.INFRASTRUCTURE_PAUSE

def test_prepared_round_reuses_pair_for_setup_explain_and_queries() -> None:
    factory = _CountingFactory()
    prepared = coordinator.prepare(_Bundle(requires_same_session=False), database="sf_case")
    coordinator.explain_baseline(prepared, "SELECT 1", LIMITS)
    coordinator.execute(prepared, "SELECT 1", LIMITS)
    coordinator.execute(prepared, "SELECT 2", LIMITS)
    assert factory.open_count_by_role == {NodeRole.CUSTOM_OFF: 1, NodeRole.CUSTOM_ON: 1}

def test_prestart_failure_aborts_peer_without_waiting_for_query_timeout() -> None:
    started = time.monotonic()
    result = coordinator.execute(_prepared_with_identity_failure(), "SELECT 1", QueryLimits(10, 10, 1024))
    assert time.monotonic() - started < 1
    assert {execution.error.message for execution in result} >= {"对端执行准备失败"}
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/execution/test_triad.py tests/execution/test_mysql_runner.py tests/service/test_round_engine.py -q`

Expected: one-sided infra is mismatch, open counts exceed one pair, or barrier waits for timeout.

- [ ] **Step 3: Implement persistent pair and priority classification**

```python
def _statement_verdict(results, *, compare_affected_rows):
    ordered = tuple(results[role] for role in COMPARISON_ROLES)
    if any(result.status is ExecutionStatus.INFRA_ERROR for result in ordered):
        return LockstepSetupVerdict.INFRASTRUCTURE_PAUSE
    if all(result.status is ExecutionStatus.SUCCESS for result in ordered):
        if not compare_affected_rows:
            return LockstepSetupVerdict.READY
        return LockstepSetupVerdict.READY if len({r.affected_rows for r in ordered}) == 1 else LockstepSetupVerdict.MISMATCH
    if all(result.status is ExecutionStatus.ERROR for result in ordered):
        identities = {normalize_error(result.error) for result in ordered if result.error is not None}
        return LockstepSetupVerdict.REJECTED_GENERATION if len(identities) == 1 else LockstepSetupVerdict.MISMATCH
    return LockstepSetupVerdict.MISMATCH
```

Make `PreparedRound` own `PairSessionAcquisition` leases for every schema. Run setup with those sessions, remove per-query initialization, abort the shared start barrier on every pre-start failure, and encode peer-derived failure separately.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/execution/test_triad.py tests/execution/test_mysql_runner.py tests/integration/test_setup_mysql.py tests/service/test_round_engine.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/select_fuzz/execution/setup.py src/select_fuzz/execution/triad.py src/select_fuzz/execution/mysql.py src/select_fuzz/correctness.py tests/execution/test_triad.py tests/execution/test_mysql_runner.py tests/service/test_round_engine.py
git commit -m "fix: keep correctness session pairs for each round"
```

### Task 4: Mutation on the prepared pair with safe retry semantics

**Files:**
- Modify: `src/select_fuzz/execution/mutation.py`
- Modify: `src/select_fuzz/correctness.py`
- Modify: `src/select_fuzz/config/models.py`
- Modify: `src/select_fuzz/config/loader.py`
- Test: `tests/execution/test_mutation.py`
- Test: `tests/service/test_round_engine.py`
- Test: `tests/config/test_loader.py`

- [ ] **Step 1: Write failing mutation tests**

```python
def test_mutation_uses_caller_owned_pair_without_opening_connections() -> None:
    sessions = _pair_sessions()
    result = coordinator.execute_batch("sf_case", batch, sessions=sessions)
    assert result.verdict is MutationVerdict.COMMITTED
    assert factory.query_session_calls == 0

def test_precommit_infrastructure_failure_is_retryable_but_commit_failure_is_ambiguous() -> None:
    precommit = coordinator.execute_batch("sf_case", batch, sessions=_fails_on("UPDATE"))
    commit = coordinator.execute_batch("sf_case", batch, sessions=_fails_on("COMMIT"))
    assert precommit.retry_safety is MutationRetrySafety.SAFE_AFTER_RECONNECT
    assert commit.retry_safety is MutationRetrySafety.COMMIT_AMBIGUOUS
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/execution/test_mutation.py tests/service/test_round_engine.py -q`

Expected: API does not accept prepared sessions and has no retry-safety classification.

- [ ] **Step 3: Implement prepared-pair mutation and bounded retry**

```python
class MutationRetrySafety(StrEnum):
    NONE = "none"
    SAFE_AFTER_RECONNECT = "safe_after_reconnect"
    COMMIT_AMBIGUOUS = "commit_ambiguous"

def execute_batch(self, database, batch, *, sessions):
    return self._execute_batch(database, batch, sessions=sessions, on_statement=None)
```

Pass `PreparedRound.sessions` from correctness. Retry ordinary-table batches at most `mutation_infrastructure_retry_attempts` only when the result is `SAFE_AFTER_RECONNECT`; never replay `COMMIT_AMBIGUOUS`; terminate temporary-table rounds on session loss. Infrastructure outcomes publish diagnostics and do not create correctness findings.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/execution/test_mutation.py tests/service/test_round_engine.py tests/config/test_loader.py tests/config/test_loader_boundaries.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/select_fuzz/execution/mutation.py src/select_fuzz/correctness.py src/select_fuzz/config/models.py src/select_fuzz/config/loader.py tests/execution/test_mutation.py tests/service/test_round_engine.py tests/config/test_loader.py
git commit -m "fix: retry mutations only before ambiguous commit"
```

### Task 5: Finding bundle v2 with streamed replay SQL

**Files:**
- Modify: `src/select_fuzz/artifacts/bundle.py`
- Modify: `src/select_fuzz/artifacts/reader.py`
- Modify: `src/select_fuzz/replay.py`
- Modify: `src/select_fuzz/correctness.py`
- Test: `tests/artifacts/test_bundle.py`
- Test: `tests/artifacts/test_bundle_boundaries.py`
- Test: `tests/integration/test_replay.py`
- Test: `tests/service/test_round_engine.py`

- [ ] **Step 1: Write failing v2 artifact tests**

```python
def test_large_setup_is_externalized_and_round_trips(tmp_path: Path) -> None:
    statement = "INSERT INTO `t` VALUES " + ",".join("(1)" for _ in range(17_000_000))
    finding = replace(_finding(), setup_sql=("CREATE TABLE `t` (`id` INT)", statement))
    published = CaseBundleWriter(tmp_path).write_finding(finding)
    manifest = json.loads((published / "manifest.json").read_text())
    assert manifest["schema_version"] == 2
    assert "setup_sql" not in manifest
    assert manifest["setup_sql_ref"]["statement_count"] == 2
    assert read_case_bundle(published).setup_sql == finding.setup_sql
    assert (published / "manifest.json").stat().st_size < 1_000_000

def test_v1_inline_setup_remains_readable(tmp_path: Path) -> None:
    path = _write_v1_fixture(tmp_path)
    assert read_case_bundle(path).setup_sql == ("CREATE TABLE `t` (`id` INT)",)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/artifacts/test_bundle.py tests/artifacts/test_bundle_boundaries.py tests/integration/test_replay.py -q`

Expected: schema remains v1 or large manifest raises the 64 MiB safety error.

- [ ] **Step 3: Implement streamed SQL references**

```python
def _write_sql_jsonl_gz(path: Path, statements: tuple[str, ...]) -> SqlPayloadRef:
    digest = sha256()
    uncompressed = 0
    with gzip.GzipFile(filename="", mode="wb", fileobj=path.open("xb"), mtime=0) as stream:
        for statement in statements:
            line = json.dumps(statement, ensure_ascii=False).encode("utf-8") + b"\n"
            digest.update(line)
            uncompressed += len(line)
            stream.write(line)
    return SqlPayloadRef(path.name, len(statements), uncompressed, path.stat().st_size, digest.hexdigest())
```

Write SQL payloads inside the existing temporary finding directory before creating the compact v2 manifest. Reader dispatches on `schema_version`; replay consumes the normalized reader model. Store only failing setup ordinal, digest, and bounded preview in `first_difference`.

- [ ] **Step 4: Run artifact and replay tests and verify GREEN**

Run: `uv run pytest tests/artifacts tests/integration/test_replay.py tests/service/test_round_engine.py -q`

Expected: all selected tests pass, including the >64 MiB case.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/select_fuzz/artifacts/bundle.py src/select_fuzz/artifacts/reader.py src/select_fuzz/replay.py src/select_fuzz/correctness.py tests/artifacts/test_bundle.py tests/artifacts/test_bundle_boundaries.py tests/integration/test_replay.py tests/service/test_round_engine.py
git commit -m "fix: externalize large finding replay sql"
```

### Task 6: Deterministic row-limit admission and metadata advisories

**Files:**
- Create: `src/select_fuzz/generation/query_determinism.py`
- Modify: `src/select_fuzz/correctness.py`
- Modify: `src/select_fuzz/generation/query_grammar.py`
- Modify: `src/select_fuzz/oracle/compare.py`
- Test: `tests/generation/test_query_determinism.py`
- Test: `tests/oracle/test_compare.py`
- Test: `tests/service/test_round_engine.py`

- [ ] **Step 1: Write failing determinism and advisory tests**

```python
@pytest.mark.parametrize("sql", [
    "SELECT `payload` FROM `t0` LIMIT 5",
    "TABLE `t0` ORDER BY 30 LIMIT 1, 5",
    "SELECT 1 UNION ALL SELECT `c7` FROM `t0` LIMIT 1, 100",
])
def test_unproved_nonzero_row_limit_is_rejected(sql: str) -> None:
    assert assess_query_determinism(sql, proof=None).admissible is False

def test_nullable_only_difference_is_match_with_metadata_advisory() -> None:
    result = compare_two_nodes((_execution(nullable=True), _execution(nullable=False)))
    assert result.verdict is OracleVerdict.MATCH
    assert result.advisories[0].category == "metadata"

def test_type_code_difference_remains_result_mismatch() -> None:
    result = compare_two_nodes((_execution(type_code=3), _execution(type_code=253)))
    assert result.verdict is OracleVerdict.RESULT_MISMATCH
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/generation/test_query_determinism.py tests/oracle/test_compare.py tests/service/test_round_engine.py -q`

Expected: no determinism assessor exists and nullable differences remain mismatch findings.

- [ ] **Step 3: Implement conservative admission and two-tier metadata**

```python
@dataclass(frozen=True, slots=True)
class QueryDeterminism:
    admissible: bool
    reason: str | None = None

def assess_query_determinism(sql: str, proof: DeterministicOrderProof | None) -> QueryDeterminism:
    limits = scan_row_limits(sql)
    if not limits or all(limit.count == 0 for limit in limits):
        return QueryDeterminism(True)
    if proof is not None and proof.covers_all_row_limits:
        return QueryDeterminism(True)
    return QueryDeterminism(False, "nondeterministic_row_limit")
```

Grammar-generated correctness queries carry an optional deterministic-order proof. Reject unproved nonzero row limits before EXPLAIN and remove their tags from coverage debt. Split oracle column keys into value semantics and advisory metadata; attach advisories to match results and publish bounded `metadata_advisory` events without findings.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/generation/test_query_determinism.py tests/generation/test_query_grammar.py tests/oracle/test_compare.py tests/service/test_round_engine.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 6**

```bash
git add src/select_fuzz/generation/query_determinism.py src/select_fuzz/generation/query_grammar.py src/select_fuzz/oracle/compare.py src/select_fuzz/correctness.py tests/generation/test_query_determinism.py tests/oracle/test_compare.py tests/service/test_round_engine.py
git commit -m "fix: exclude nondeterministic correctness results"
```

### Task 7: Default diagnostics and prompt fatal shutdown

**Files:**
- Create: `src/select_fuzz/correctness_diagnostics.py`
- Modify: `src/select_fuzz/correctness.py`
- Modify: `src/select_fuzz/service.py`
- Modify: `src/select_fuzz/config/models.py`
- Modify: `src/select_fuzz/config/loader.py`
- Modify: `config/intranet-correctness.example.yaml`
- Test: `tests/service/test_correctness.py`
- Test: `tests/service/test_round_engine.py`
- Test: `tests/config/test_loader.py`

- [ ] **Step 1: Write failing logging and shutdown tests**

```python
def test_fatal_worker_error_aborts_active_sessions_and_logs_original_message() -> None:
    rounds = _FailingRounds(ValueError("artifact payload exploded"))
    with pytest.raises(ValueError, match="artifact payload exploded"):
        CorrectnessRunService(rounds, sink).run(_request(), Event())
    assert rounds.abort_active_calls == 1
    failed = next(event for event in sink.items if event.kind == "run_failed")
    assert failed.payload["message"] == "artifact payload exploded"
    assert failed.payload["exception_type"] == "ValueError"

def test_correctness_production_enables_query_attempt_log_by_default(tmp_path: Path) -> None:
    runner = build_correctness_runner(_config(), tmp_path)
    runner.run(_one_query_request(), Event())
    assert next((tmp_path / "sql").glob("worker-*.jsonl")).stat().st_size > 0
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/service/test_correctness.py tests/service/test_round_engine.py tests/config/test_loader.py -q`

Expected: no `abort_active`, run_failed contains only error type, and production disables query-attempt JSON.

- [ ] **Step 3: Implement diagnostics and shutdown hooks**

```python
except Exception as error:
    stop_event.set()
    publisher.publish("run_failed", {
        "error_type": type(error).__name__,
        "message": str(error),
        "evidence": capture_exception_evidence(error, "correctness_worker"),
        "worker_id": worker_id,
    })
    abort_active = getattr(self._rounds, "abort_active", None)
    if callable(abort_active):
        abort_active()
    for pending in futures:
        pending.cancel()
    raise
```

Enable query-attempt JSON from `CorrectnessConfig.query_attempt_json_log` defaulting true. Add a bounded diagnostics collector and a 5-second publisher for worker stage, stage age, connection IDs, retries, counts, and recent failures. Implement `CorrectnessRoundEngine.abort_active()` through the shared connector registry.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/service/test_correctness.py tests/service/test_round_engine.py tests/config/test_loader.py tests/api/test_events_and_index.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 7**

```bash
git add src/select_fuzz/correctness_diagnostics.py src/select_fuzz/correctness.py src/select_fuzz/service.py src/select_fuzz/config/models.py src/select_fuzz/config/loader.py config/intranet-correctness.example.yaml tests/service/test_correctness.py tests/service/test_round_engine.py tests/config/test_loader.py
git commit -m "feat: emit correctness runtime diagnostics"
```

### Task 8: Full regression, dual MySQL 8.0.22 fault injection, and build

**Files:**
- Modify: `docs/project-structure.md`
- Create: `docs/testing/correctness-reliability-validation.md`
- Test: existing complete suite and Docker integration tests

- [ ] **Step 1: Run all affected suites**

Run:

```bash
uv run pytest tests/execution tests/artifacts tests/oracle tests/generation/test_query_determinism.py tests/service/test_correctness.py tests/service/test_round_engine.py tests/integration/test_replay.py -q
```

Expected: zero failures.

- [ ] **Step 2: Run the complete project suite**

Run: `uv run pytest -q`

Expected: zero failures.

- [ ] **Step 3: Run static and packaging verification**

Run:

```bash
uv run ruff check src tests
uv run mypy src
uv build
git diff --check
```

Expected: every command exits zero and `dist/` contains wheel and source archive.

- [ ] **Step 4: Start two MySQL 8.0.22 containers**

Run:

```bash
docker run -d --name sf-mysql8022-off -e MYSQL_ROOT_PASSWORD=test_only_password -p 3307:3306 mysql:8.0.22 --max-connections=512
docker run -d --name sf-mysql8022-on -e MYSQL_ROOT_PASSWORD=test_only_password -p 3308:3306 mysql:8.0.22 --max-connections=512
```

Expected: both containers become healthy and `SELECT VERSION()` returns `8.0.22`.

- [ ] **Step 5: Run correctness smoke for at least five minutes**

Use a temporary two-node config with 8 workers, bounded rows/columns, `query_attempt_json_log: true`, and run:

```bash
SELECT_FUZZ_MYSQL_USER=root SELECT_FUZZ_MYSQL_PASSWORD=test_only_password \
uv run select-fuzz run --mode correctness --config /tmp/select-fuzz-correctness-reliability.yaml \
  --duration-seconds 300 --seed 20260822 --artifacts artifacts/correctness-reliability-smoke
```

Expected: no `run_failed`, no internal-error finding, stable per-round connection IDs, and bounded live connections.

- [ ] **Step 6: Run one-node stop/restart fault injection**

Start a second bounded correctness run, stop `sf-mysql8022-on`, wait for an infrastructure event, restart it, and let the run recover. Expected: custom_on carries the real connect error, custom_off is marked peer-not-ready without copied errno, no setup mismatch finding is created, and both sides have no lingering sessions after stop.

- [ ] **Step 7: Record validation evidence**

Write `docs/testing/correctness-reliability-validation.md` with exact commands, versions, run IDs, event counts, finding classifications, peak `Threads_connected`, maximum Sleep age, fault timestamps, recovery time, full test counts, and build artifact names.

- [ ] **Step 8: Commit validation and final code state**

```bash
git add docs/project-structure.md docs/testing/correctness-reliability-validation.md
git commit -m "test: validate correctness reliability on mysql 8.0.22"
```

Expected: `git status --short` is empty except ignored runtime artifacts.
