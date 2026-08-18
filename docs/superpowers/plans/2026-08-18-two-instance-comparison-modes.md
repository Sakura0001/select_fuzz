# Two-Instance Comparison Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make correctness and performance run against exactly two independently writable MySQL endpoints, with `custom_off` as the only baseline and `custom_on` as the PQ-enabled candidate, while leaving fuzz topology and behavior unchanged.

**Architecture:** Introduce one shared ordered comparison-role contract and make configuration validation mode-aware. Convert the correctness and performance execution paths from fixed triads plus replica barriers to real two-party coordinators, barriers, oracles, and artifacts. Preserve historical three-role artifact reading for reports, but only replay newly written two-role findings.

**Tech Stack:** Python 3.11, Pydantic 2, mysql-connector-python, pytest, Ruff, mypy, Docker, MySQL 8.0.22, manylinux2014/CentOS 7 packaging.

---

## File Structure

- `src/select_fuzz/config/models.py`: shared comparison roles and mode-aware topology accessors.
- `src/select_fuzz/config/loader.py`: mode-aware YAML validation and Chinese configuration diagnostics.
- `src/select_fuzz/doctor.py`: two-endpoint comparison preflight and unchanged fuzz preflight.
- `src/select_fuzz/execution/setup.py`: two-role lockstep setup.
- `src/select_fuzz/execution/triad.py`: replace the production triad coordinator with a two-role comparison coordinator while retaining database/limit value objects.
- `src/select_fuzz/execution/mutation.py`: two-role transactional mutation coordinator without replication waits.
- `src/select_fuzz/oracle/compare.py`: exact pair result comparison.
- `src/select_fuzz/oracle/query_errors.py`: exact pair generator-error analysis.
- `src/select_fuzz/correctness.py`: two-endpoint production wiring, role iteration, artifacts, and canonical result selection.
- `src/select_fuzz/artifacts/bundle.py`: new two-role finding writer contract.
- `src/select_fuzz/artifacts/reader.py`: read both two-role and historical three-role findings.
- `src/select_fuzz/artifacts/report.py`: render either stored role shape.
- `src/select_fuzz/replay.py`: replay two-role findings and reject historical triads explicitly.
- `src/select_fuzz/performance/materialization.py`: two-role materialization and evidence comparison.
- `src/select_fuzz/performance/execution.py`: two-party formal execution barrier.
- `src/select_fuzz/performance/models.py`: two-measurement formal-run invariant.
- `src/select_fuzz/performance/oracle.py`: only `VS_CUSTOM_OFF` performance assessment.
- `src/select_fuzz/performance/shared_round.py`: attribute setup failures to the real off/on roles.
- `src/select_fuzz/performance/entrypoint.py`: direct two-endpoint materialization, evidence, execution, and fingerprints.
- `src/select_fuzz/performance/artifacts.py`: serialize exactly two new measurements and plan files.
- `config/example.yaml`: two-endpoint correctness/performance template.
- `packaging/centos7/Dockerfile`: include the comparison template in the offline archive.
- `README.md`, `python/README.md`: document topology, commands, and connection expectations.

### Task 1: Add the mode-aware two-endpoint configuration contract

**Files:**
- Modify: `src/select_fuzz/config/models.py`
- Modify: `src/select_fuzz/config/loader.py`
- Modify: `src/select_fuzz/config/__init__.py`
- Test: `tests/config/test_loader.py`
- Test: `tests/config/test_replica_topology.py`
- Test: `tests/config/test_loader_boundaries.py`

- [ ] **Step 1: Write failing comparison-topology tests**

Add tests equivalent to:

```python
def _comparison_nodes() -> list[dict[str, object]]:
    return [
        {"role": "custom_off", "host": "127.0.0.1", "port": 3307},
        {"role": "custom_on", "host": "127.0.0.1", "port": 3308},
    ]


@pytest.mark.parametrize("mode", ["correctness", "performance"])
def test_comparison_modes_require_exactly_two_flat_endpoints(
    tmp_path: Path, mode: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"mode": mode, "nodes": _comparison_nodes()}))

    config = load_config(path)

    assert [node.role for node in config.comparison_nodes] == [
        NodeRole.CUSTOM_OFF,
        NodeRole.CUSTOM_ON,
    ]
    assert [node.port for node in config.comparison_nodes] == [3307, 3308]


def test_comparison_mode_rejects_old_six_endpoint_topology(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"mode": "correctness", "nodes": _topology()}))

    with pytest.raises(ConfigLoadError, match="对比模式必须配置 custom_off 和 custom_on"):
        load_config(path)


def test_comparison_mode_rejects_replica_parameter_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "mode": "performance",
                "nodes": _comparison_nodes(),
                "replica_parameters_file": "replica-parameters.yaml",
            }
        )
    )

    with pytest.raises(ConfigLoadError, match="两实例对比模式不使用备库参数"):
        load_config(path)
```

Retain the existing fuzz tests that accept three role topologies, selected-role
primary/replica routing, and an allowed shared proxy.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest -q tests/config/test_loader.py tests/config/test_replica_topology.py tests/config/test_loader_boundaries.py
```

Expected: failures because `AppConfig` still requires all three roles and has no
`comparison_nodes` accessor.

- [ ] **Step 3: Implement the shared role contract and mode-aware validation**

Add and export:

```python
COMPARISON_ROLES: tuple[NodeRole, NodeRole] = (
    NodeRole.CUSTOM_OFF,
    NodeRole.CUSTOM_ON,
)
COMPARISON_ROLE_SET = frozenset(COMPARISON_ROLES)
```

Apply CLI overrides before resolving `replica_parameters_file`, then reject a
non-null reference immediately when the effective mode is correctness or
performance. This ensures `--mode` precedence and produces the intended error
even when the obsolete file path no longer exists.

Make `AppConfig.require_fixed_unique_topology()` branch on effective mode:

```python
if self.mode in {RunMode.CORRECTNESS, RunMode.PERFORMANCE}:
    roles = tuple(node.role for node in self.nodes)
    if len(self.nodes) != 2 or frozenset(roles) != COMPARISON_ROLE_SET:
        raise ValueError(
            "对比模式必须配置 custom_off 和 custom_on 两个单实例 endpoint"
        )
    if any(not node.legacy_single_endpoint for node in self.nodes):
        raise ValueError("两实例对比模式不接受 primary/replica 嵌套配置")
    endpoints = tuple(
        (node.primary.host.casefold(), node.primary.port) for node in self.nodes
    )
    if len(set(endpoints)) != 2:
        raise ValueError("custom_off 和 custom_on 必须使用不同的 host/port")
    if self.replica_parameters_file is not None:
        raise ValueError("两实例对比模式不使用备库参数文件")
    return self
```

Keep the existing three-role fuzz checks in the other branch. Add:

```python
@property
def comparison_nodes(self) -> tuple[NodeConfig, NodeConfig]:
    if self.mode not in {RunMode.CORRECTNESS, RunMode.PERFORMANCE}:
        raise ValueError("comparison_nodes 仅适用于 correctness/performance")
    return tuple(self.node_for(role) for role in COMPARISON_ROLES)  # type: ignore[return-value]
```

Remove the loader's blanket rejection of legacy single endpoints for every
non-fuzz mode; the model validator now owns the complete rule. Change
`_validation_summary()` to include the sanitized Pydantic `item["msg"]` after the
location and error type, so the concrete Chinese model reason reaches CLI
stderr without including the rejected input value.

- [ ] **Step 4: Run focused config tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/config tests/config
git commit -m "feat: accept two comparison endpoints"
```

### Task 2: Probe exactly two comparison endpoints in doctor

**Files:**
- Modify: `src/select_fuzz/doctor.py`
- Test: `tests/service/test_doctor.py`
- Test: `tests/service/test_doctor_boundaries.py`

- [ ] **Step 1: Write failing doctor tests**

```python
@pytest.mark.parametrize("mode", [RunMode.CORRECTNESS, RunMode.PERFORMANCE])
def test_doctor_probes_exactly_two_comparison_endpoints(mode: RunMode) -> None:
    config = AppConfig(mode=mode, nodes=_comparison_nodes())
    probe = RecordingProbe()

    report = DoctorService(config, probe).run()

    assert probe.roles == [NodeRole.CUSTOM_OFF, NodeRole.CUSTOM_ON]
    assert report.can_start
```

Also assert that a missing role probe yields two warnings at most, duplicate
endpoint configuration never reaches the probe, and the existing fuzz shared
proxy test is unchanged.

- [ ] **Step 2: Run tests and verify RED**

```bash
uv run pytest -q tests/service/test_doctor.py tests/service/test_doctor_boundaries.py
```

Expected: doctor still constructs jobs from all `NodeRole` values and/or six
primary/replica endpoints.

- [ ] **Step 3: Implement mode-aware doctor jobs**

Use one endpoint per active comparison role and retain fuzz's selected topology:

```python
if self._config.mode is RunMode.FUZZ:
    role = self._config.fuzz.target_role
    candidates = (
        (role, "primary", self._config.node_for(role)),
        (role, "replica", self._config.replica_for(role)),
    )
else:
    candidates = tuple(
        (role, "endpoint", self._config.node_for(role))
        for role in COMPARISON_ROLES
    )
```

Deduplicate only identical fuzz proxy endpoints. Do not deduplicate the two
comparison roles, since configuration already rejects duplicates.

- [ ] **Step 4: Run focused doctor tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/doctor.py tests/service/test_doctor.py tests/service/test_doctor_boundaries.py
git commit -m "feat: probe two comparison instances"
```

### Task 3: Convert setup, query coordination, and DML to a real pair

**Files:**
- Modify: `src/select_fuzz/execution/setup.py`
- Modify: `src/select_fuzz/execution/triad.py`
- Modify: `src/select_fuzz/execution/mutation.py`
- Modify: `src/select_fuzz/execution/__init__.py`
- Test: `tests/execution/test_triad.py`
- Test: `tests/execution/test_mutation.py`

- [ ] **Step 1: Write failing pair-execution tests**

Add tests that construct only off/on nodes and assert exact dispatch counts:

```python
def _pair() -> tuple[NodeConfig, NodeConfig]:
    return (
        NodeConfig(role=NodeRole.CUSTOM_OFF, host="off.example", port=3306),
        NodeConfig(role=NodeRole.CUSTOM_ON, host="on.example", port=3306),
    )


def test_lockstep_setup_executes_each_statement_on_the_pair() -> None:
    runner = MySQLSetupRunner(factory)
    result = runner.apply_lockstep(_pair(), DATABASE, bundle)
    assert result.verdict is LockstepSetupVerdict.READY
    assert {node.role for node in result.nodes} == set(COMPARISON_ROLES)


def test_comparison_coordinator_dispatches_query_to_both_nodes() -> None:
    coordinator = ComparisonCoordinator(
        _pair(), setup_runner=setup, query_runner=runner, session_factory=factory
    )
    prepared = coordinator.prepare(bundle, database=DATABASE)
    result = coordinator.execute(prepared, "SELECT 1", LIMITS)
    assert [item.role for item in result] == list(COMPARISON_ROLES)


def test_pair_mutation_has_two_party_barrier_and_no_replication_wait() -> None:
    coordinator = PairMutationCoordinator(
        _pair(), factory=factory, runner=runner, limits=LIMITS
    )
    result = coordinator.execute_batch(DATABASE, batch)
    assert set(result.final_results) == set(COMPARISON_ROLES)
    assert waiter.calls == []
```

Cover one-sided setup error, affected-row mismatch, rollback mismatch, connection
loss, timeout, barrier abort, and closure of both sessions.

- [ ] **Step 2: Run tests and verify RED**

```bash
uv run pytest -q tests/execution/test_triad.py tests/execution/test_mutation.py
```

Expected: constructors reject two roles and barriers still require three parties.

- [ ] **Step 3: Implement pair setup and coordinator behavior**

In each production coordinator, order nodes through `COMPARISON_ROLES`, use
`ThreadPoolExecutor(max_workers=2)`, and use `Barrier(2)`. Rename the production
classes while keeping unrelated value objects stable:

```python
class ComparisonCoordinator:
    def __init__(self, nodes: Sequence[NodeConfig], *, setup_runner: SetupRunnerLike,
                 query_runner: QueryRunnerLike, session_factory: ConnectionFactory) -> None:
        by_role = {node.role: node for node in nodes}
        if len(nodes) != 2 or set(by_role) != set(COMPARISON_ROLES):
            raise ValueError("comparison coordinator requires custom_off and custom_on")
        self._nodes = tuple(by_role[role] for role in COMPARISON_ROLES)
```

`explain_baseline()` must select `NodeRole.CUSTOM_OFF`. Remove the
`replication_waiter` argument and every marker/replication branch from comparison
prepare and mutation. In mutation accounting use:

```python
affected_rows = results[NodeRole.CUSTOM_OFF].affected_rows
```

Return mappings and tuples in `COMPARISON_ROLES` order. Preserve cleanup through
`ExitStack` for every success and failure path.

- [ ] **Step 4: Run focused execution tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/execution tests/execution
git commit -m "refactor: execute comparisons as a pair"
```

### Task 4: Convert the correctness oracle and runner to two roles

**Files:**
- Modify: `src/select_fuzz/oracle/compare.py`
- Modify: `src/select_fuzz/oracle/query_errors.py`
- Modify: `src/select_fuzz/oracle/__init__.py`
- Modify: `src/select_fuzz/correctness.py`
- Test: `tests/oracle/test_compare.py`
- Test: `tests/oracle/test_query_error_analysis.py`
- Test: `tests/property/test_float_multiset.py`
- Test: `tests/service/test_round_engine.py`
- Test: `tests/service/test_correctness.py`

- [ ] **Step 1: Write failing pair-oracle and production-wiring tests**

```python
def test_compare_two_nodes_matches_equal_results() -> None:
    result = compare_two_nodes(
        (
            _success(NodeRole.CUSTOM_OFF, (INT,), ((1,),)),
            _success(NodeRole.CUSTOM_ON, (INT,), ((1,),)),
        )
    )
    assert result.verdict is OracleVerdict.MATCH
    assert len(result.pairwise) == 1


def test_compare_two_nodes_rejects_baseline_or_missing_role() -> None:
    with pytest.raises(OracleInputError, match="custom_off and custom_on"):
        compare_two_nodes((_success(NodeRole.BASELINE, (INT,), ((1,),)),))


def test_production_correctness_uses_same_two_endpoints_for_setup_and_query() -> None:
    runner = build_correctness_runner(_comparison_config(), tmp_path)
    runner.run(request, Event())
    assert connector.roles == [NodeRole.CUSTOM_OFF, NodeRole.CUSTOM_ON]
    assert replication_waiter.calls == []
```

Add pair versions of expected-error, resource-limit, metadata mismatch, fuzzy
floating-point multiset, uniform runtime error, DML finding, and canonical pass
digest tests.

- [ ] **Step 2: Run tests and verify RED**

```bash
uv run pytest -q tests/oracle tests/property/test_float_multiset.py tests/service/test_round_engine.py tests/service/test_correctness.py
```

Expected: the oracle/error analyzer require three roles and correctness builds a
primary/replica barrier.

- [ ] **Step 3: Implement pair oracle and correctness wiring**

Implement:

```python
def compare_two_nodes(executions: Iterable[NodeExecution]) -> OracleResult:
    values = tuple(executions)
    by_role = {item.role: item for item in values}
    if len(values) != 2 or set(by_role) != set(COMPARISON_ROLES):
        raise OracleInputError("oracle requires custom_off and custom_on executions")
    ordered = tuple(by_role[role] for role in COMPARISON_ROLES)
    if any(item.status is ExecutionStatus.INFRA_ERROR for item in ordered):
        raise OracleInputError("infra_error executions must not enter oracle")
    pair = _compare_pair(ordered[0], ordered[1], _FuzzyComparisonBudget())
    if all(_is_resource_limited(item) for item in ordered):
        verdict = OracleVerdict.OVER_BUDGET
    else:
        verdict = OracleVerdict.MATCH if pair.matched else OracleVerdict.RESULT_MISMATCH
    return OracleResult(verdict=verdict, pairwise=(pair,))
```

Make query-error ordering use the same exact pair. In correctness replace every
production `for role in NodeRole` with `for role in COMPARISON_ROLES`, select
`CUSTOM_OFF` for canonical pass results, and build one connector factory over
`config.comparison_nodes`. Wire `ComparisonCoordinator` and
`PairMutationCoordinator` without `ReplicationBarrier`, replica factories,
replica session variables, or marker SQL. Build fingerprints from each flat
endpoint only.

- [ ] **Step 4: Run focused correctness tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/oracle src/select_fuzz/correctness.py tests/oracle tests/property/test_float_multiset.py tests/service
git commit -m "feat: compare correctness across two instances"
```

### Task 5: Write two-role findings and replay only the new shape

**Files:**
- Modify: `src/select_fuzz/artifacts/bundle.py`
- Modify: `src/select_fuzz/artifacts/reader.py`
- Modify: `src/select_fuzz/artifacts/report.py`
- Modify: `src/select_fuzz/replay.py`
- Test: `tests/artifacts/test_bundle.py`
- Test: `tests/artifacts/test_bundle_boundaries.py`
- Test: `tests/integration/test_replay.py`

- [ ] **Step 1: Write failing artifact compatibility tests**

```python
def test_new_finding_writes_exactly_two_role_results(tmp_path: Path) -> None:
    published = CaseBundleWriter(tmp_path).write_finding(_pair_finding())
    manifest = json.loads((published / "manifest.json").read_text())
    assert set(manifest["result_files"]) == {"custom_off", "custom_on"}


def test_reader_accepts_historical_three_role_finding(tmp_path: Path) -> None:
    _write_historical_three_role_bundle(tmp_path)
    finding = ArtifactReader(tmp_path).get_finding("legacy_case")
    assert set(finding.results) == set(NodeRole)


def test_replay_rejects_historical_topology_in_chinese(tmp_path: Path) -> None:
    _write_historical_three_role_bundle(tmp_path)
    with pytest.raises(ArtifactValidationError, match="历史三节点产物不能使用两实例配置回放"):
        build_replay_service(_comparison_config(), tmp_path).replay("legacy_case")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
uv run pytest -q tests/artifacts tests/integration/test_replay.py
```

Expected: writer validators demand all three roles and replay builds a triad.

- [ ] **Step 3: Implement new-write and legacy-read role policies**

Use two separate validators instead of one ambiguous role helper:

```python
def _new_comparison_role_mapping(value: Mapping[NodeRole, Any], label: str) -> Mapping[NodeRole, Any]:
    if set(value) != set(COMPARISON_ROLES):
        raise ValueError(f"{label} must contain custom_off and custom_on")
    return MappingProxyType({role: value[role] for role in COMPARISON_ROLES})


def _stored_role_values(raw: Mapping[str, object]) -> tuple[NodeRole, ...]:
    keys = frozenset(raw)
    if keys == frozenset(role.value for role in COMPARISON_ROLES):
        return COMPARISON_ROLES
    if keys == frozenset(role.value for role in NodeRole):
        return tuple(NodeRole)
    raise ArtifactValidationError("artifact roles are invalid")
```

New manifests use `CUSTOM_OFF` as the canonical result and write only two gzip
result files. Report rendering iterates the stored roles rather than global
`NodeRole`. Replay parses only exact pair databases/results, wires the comparison
coordinator directly to the two configured endpoints, and raises the documented
Chinese error before connecting when it sees the historical role set.

- [ ] **Step 4: Run focused artifact/replay tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/artifacts src/select_fuzz/replay.py tests/artifacts tests/integration/test_replay.py
git commit -m "feat: store and replay two-role findings"
```

### Task 6: Convert performance materialization, formal execution, and oracle

**Files:**
- Modify: `src/select_fuzz/performance/materialization.py`
- Modify: `src/select_fuzz/performance/execution.py`
- Modify: `src/select_fuzz/performance/models.py`
- Modify: `src/select_fuzz/performance/oracle.py`
- Modify: `src/select_fuzz/performance/shared_round.py`
- Test: `tests/performance/test_materialization.py`
- Test: `tests/performance/test_execution.py`
- Test: `tests/performance/test_performance_models.py`
- Test: `tests/performance/test_oracle.py`
- Test: `tests/performance/test_shared_round.py`

- [ ] **Step 1: Write failing two-party performance tests**

```python
def test_materializer_prepares_and_verifies_only_off_and_on() -> None:
    result = ScaleMaterializer(port).rebuild_all("perf_pair", manifest)
    assert set(result) == set(COMPARISON_ROLES)
    assert port.roles == list(COMPARISON_ROLES) * 2


def test_formal_runner_uses_two_party_barrier() -> None:
    run = FormalRunner(_pair(), core, policy).run(frozen)
    assert set(run.measurements) == set(COMPARISON_ROLES)
    assert run.start_skew_ms <= policy.max_start_skew_ms


def test_performance_oracle_has_only_custom_off_reference() -> None:
    run = _pair_run(off_ms=100, on_ms=121)
    assessment = assess(run, threshold=0.20)
    assert assessment.verdict is Verdict.PERF_ALERT
    assert assessment.reasons == ("VS_CUSTOM_OFF",)
```

Cover custom_off timeout as unreliable reference, custom_on timeout as alert,
both over budget, one-sided execution/infra failure, start skew, evidence
mismatch, and a faster custom_on pass.

- [ ] **Step 2: Run tests and verify RED**

```bash
uv run pytest -q tests/performance/test_materialization.py tests/performance/test_execution.py tests/performance/test_performance_models.py tests/performance/test_oracle.py tests/performance/test_shared_round.py
```

Expected: role invariants and barriers still require all three roles and oracle
emits `VS_BASELINE`.

- [ ] **Step 3: Implement the two-role performance core**

Iterate `COMPARISON_ROLES`, set executor workers and barriers to two, and validate
`FormalRun.measurements` against the comparison role set. Assessment becomes:

```python
REFERENCE_ROLE = NodeRole.CUSTOM_OFF

def assess(run: FormalRun, threshold: float = 0.20, max_skew_ms: float = 100.0) -> Assessment:
    measurements = run.measurements
    if any(item.outcome is Outcome.INFRA_ERROR for item in measurements.values()):
        return Assessment(Verdict.INFRA_ERROR)
    if all(item.outcome is Outcome.TIMEOUT for item in measurements.values()):
        return Assessment(Verdict.OVER_BUDGET)
    if measurements[REFERENCE_ROLE].outcome is Outcome.TIMEOUT:
        return Assessment(Verdict.CALIBRATION_DRIFT)
    if run.start_skew_ms > max_skew_ms:
        return Assessment(Verdict.TIMING_UNRELIABLE)
    candidate = measurements[NodeRole.CUSTOM_ON]
    if candidate.outcome is Outcome.TIMEOUT:
        return Assessment(Verdict.PERF_ALERT, ("CUSTOM_ON_TIMEOUT",))
    if any(item.outcome is not Outcome.COMPLETED for item in measurements.values()):
        return Assessment(Verdict.EXECUTION_ERROR)
    reference_ms = measurements[REFERENCE_ROLE].root_end_ms
    candidate_ms = candidate.root_end_ms
    assert reference_ms is not None and candidate_ms is not None
    regressed = candidate_ms >= reference_ms * (1 + threshold)
    return Assessment(Verdict.PERF_ALERT if regressed else Verdict.PASS,
                      ("VS_CUSTOM_OFF",) if regressed else ())
```

Materialization mismatch evidence contains the two real roles. Shared-round
fallback attribution uses `CUSTOM_OFF`, never `BASELINE`.

- [ ] **Step 4: Run focused performance tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/performance tests/performance
git commit -m "feat: assess performance across two instances"
```

### Task 7: Wire performance directly to the two endpoints and update records

**Files:**
- Modify: `src/select_fuzz/performance/entrypoint.py`
- Modify: `src/select_fuzz/performance/artifacts.py`
- Modify: `src/select_fuzz/performance/calibration.py`
- Test: `tests/performance/test_replica_routing.py`
- Test: `tests/performance/test_performance_artifacts.py`
- Test: `tests/performance/test_service.py`
- Test: `tests/cli/test_performance_run.py`

- [ ] **Step 1: Write failing production-wiring and record tests**

```python
def test_performance_entrypoint_uses_two_endpoints_without_replication() -> None:
    runner = build_performance_runner(_comparison_config(), tmp_path)
    summary = runner.run(request, Event())
    assert summary.queries_completed == 1
    assert setup.roles == list(COMPARISON_ROLES)
    assert replication_waiter.calls == []


def test_performance_record_contains_only_two_measurements_and_plans(tmp_path: Path) -> None:
    record = recorder.record(frozen, _pair_run(100, 121), assessment)
    assert set(record["measurements"]) == {"custom_off", "custom_on"}
    assert set(record["node_config_fingerprints"]) == {"custom_off", "custom_on"}
    assert "plans/baseline.tree" not in diagnostic_files
```

Also assert preflight fingerprints contain two roles, setup errors preserve both
node results, and replica marker SQL is absent from audit SQL.

- [ ] **Step 2: Run tests and verify RED**

```bash
uv run pytest -q tests/performance/test_replica_routing.py tests/performance/test_performance_artifacts.py tests/performance/test_service.py tests/cli/test_performance_run.py
```

Expected: entrypoint constructs primary/replica factories and artifact validators
demand three fingerprints.

- [ ] **Step 3: Implement direct pair wiring and records**

Use one connector and the same pair for setup, evidence, diagnostics, and formal
queries:

```python
nodes = self._config.comparison_nodes
connector = MySQLConnectorFactory()
query_runner = _SqlLoggingQueryRunner(NodeQueryRunner(connector), thread_sql_log)
materializer = ScaleMaterializer(
    MySQLCpuMaterializationPort(
        nodes,
        connector,
        query_runner,
        timeout_seconds=self._config.performance.materialization_timeout_seconds,
        stop_event=stop_event,
        sql_log=thread_sql_log,
    )
)
formal = FormalRunner(
    nodes,
    query_runner,
    policy,
    diagnostics=MySQLDiagnosticsCollector(connector),
)
```

Remove `ReplicationBarrier`, replica session variables, read-node indirection,
marker synchronization, and `replica_parameters_sha256` from new performance
events. Iterate `COMPARISON_ROLES` for fingerprints, measurements, diagnostics,
plans, and failure records. Keep stable top-level event and summary fields.

- [ ] **Step 4: Run focused production performance tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/performance tests/performance tests/cli/test_performance_run.py
git commit -m "refactor: route performance to two endpoints"
```

### Task 8: Update templates, offline packaging, documentation, and real integration

**Files:**
- Modify: `config/example.yaml`
- Modify: `packaging/centos7/Dockerfile`
- Modify: `README.md`
- Modify: `python/README.md`
- Modify: `tests/config/test_loader.py`
- Modify: `tests/packaging/test_distribution.py`
- Modify: `tests/integration/test_correctness_mysql.py`
- Modify: `tests/integration/test_performance_mysql.py`

- [ ] **Step 1: Write failing template and packaging tests**

```python
def test_example_is_a_two_endpoint_comparison_config() -> None:
    config = load_config(PROJECT_ROOT / "config/example.yaml")
    assert config.mode is RunMode.CORRECTNESS
    assert [node.role for node in config.comparison_nodes] == list(COMPARISON_ROLES)


def test_centos_bundle_dockerfile_copies_both_mode_templates() -> None:
    dockerfile = (PROJECT_ROOT / "packaging/centos7/Dockerfile").read_text()
    assert "cp /src/config/example.yaml" in dockerfile
    assert "cp /src/config/intranet-fuzz.example.yaml" in dockerfile
```

Change integration fixtures to require only `SELECT_FUZZ_CUSTOM_OFF_*` and
`SELECT_FUZZ_CUSTOM_ON_*` endpoint variables, then assert doctor and one bounded
round for each mode.

- [ ] **Step 2: Run tests and verify RED**

```bash
uv run pytest -q tests/config/test_loader.py tests/packaging/test_distribution.py tests/integration/test_correctness_mysql.py tests/integration/test_performance_mysql.py
```

Expected: the current example has six endpoints, the Dockerfile omits it, and
integration fixtures still construct the old role set.

- [ ] **Step 3: Update templates, package recipe, and usage docs**

Replace `config/example.yaml` topology with two flat endpoints and retain all
correctness/performance tuning comments. Explicitly state that off/on instances
must be independently writable and preconfigured. Keep fuzz instructions pointed
to `config/intranet-fuzz.example.yaml`.

In the Dockerfile add:

```dockerfile
cp /src/config/example.yaml "$bundle/config/"; \
cp /src/config/intranet-fuzz.example.yaml "$bundle/config/"; \
cp /src/config/replica-parameters.example.yaml "$bundle/config/"; \
```

Document exact smoke commands using `./select-fuzz`, the two-endpoint role
meaning, lack of replication waits in comparison modes, and the expectation of
no residual application connections.

- [ ] **Step 4: Run focused template/packaging tests and verify GREEN**

Run the Step 2 command. Expected: non-opt-in tests pass; real-MySQL integration
tests skip until their environment variables are enabled.

- [ ] **Step 5: Run static checks and the complete automated suite**

```bash
uv run ruff check src tests
uv run mypy src/select_fuzz
uv run pytest -q
git diff --check
```

Expected: zero Ruff/mypy/diff errors and all tests pass apart from documented
opt-in skips.

- [ ] **Step 6: Run two-instance MySQL 8.0.22 integration and Sleep sampling**

Start two bounded local MySQL 8.0.22 containers on distinct ports. Configure one
as `custom_off` and one as `custom_on`, create a temporary two-endpoint YAML, and
run:

```bash
uv run select-fuzz doctor --mode correctness --config "$PAIR_CONFIG"
uv run select-fuzz run --mode correctness --config "$PAIR_CONFIG" \
  --rounds 1 --workers 2 --queries-per-round 30 --seed 20260818 \
  --artifacts /tmp/sf-pair-correctness
uv run select-fuzz doctor --mode performance --config "$PAIR_CONFIG"
uv run select-fuzz run --mode performance --config "$PAIR_CONFIG" \
  --rounds 1 --queries-per-round 20 --seed 20260818 \
  --artifacts /tmp/sf-pair-performance
```

During bounded sustained runs, sample both endpoints once per second:

```sql
SELECT COMMAND, COUNT(*), MAX(TIME)
FROM information_schema.PROCESSLIST
WHERE USER = 'root' AND ID <> CONNECTION_ID()
GROUP BY COMMAND;
SHOW GLOBAL STATUS LIKE 'Questions';
```

Expected: Questions advances while running, no application Sleep time grows with
stalled progress, and no Select Fuzz application connections remain after exit.

- [ ] **Step 7: Rebuild and smoke-test the offline bundle**

```bash
./python/build-centos7-bundle.sh
tar -tzf python/output/select-fuzz-centos7-x86_64.tar.gz | \
  rg 'config/(example|intranet-fuzz\.example)\.yaml'
docker run --rm --platform linux/amd64 \
  -v "$PWD/python/output/select-fuzz-centos7-x86_64:/bundle:ro" \
  quay.io/pypa/manylinux2014_x86_64:latest \
  /bundle/select-fuzz --help
```

Expected: both templates are present and the packaged launcher exits zero in the
CentOS 7-compatible x86_64 container.

- [ ] **Step 8: Commit**

```bash
git add config/example.yaml packaging/centos7/Dockerfile README.md python/README.md tests/config/test_loader.py tests/packaging/test_distribution.py tests/integration
git commit -m "docs: ship two-instance comparison configuration"
```

### Task 9: Final audit, push, and handoff

**Files:**
- Review: all files changed since `015bd0b`

- [ ] **Step 1: Verify repository and commit scope**

```bash
git status --short
git log --oneline 015bd0b..HEAD
git diff --stat 015bd0b..HEAD
git diff --check 015bd0b..HEAD
```

Expected: only planned files are changed and the worktree is clean.

- [ ] **Step 2: Re-run the release gate immediately before push**

```bash
uv run ruff check src tests
uv run mypy src/select_fuzz
uv run pytest -q
```

Expected: all required checks pass with only documented opt-in skips.

- [ ] **Step 3: Push the current branch**

```bash
git push origin agent/publish-fuzz-mode
```

Expected: the remote branch advances to the final local commit.

- [ ] **Step 4: Report exact evidence**

Provide the final commit range, automated test counts, real two-instance run
summaries, PROCESSLIST/Questions observations, archive path, size, SHA256, and the
correct `./select-fuzz` commands for correctness, performance, and unchanged fuzz.
