# MySQL Fuzzer Performance Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single-worker performance mode that plan-seeds scalable workloads, calibrates `baseline` and `custom_off` with three runs each into 5–30 seconds, then measures all three nodes once behind a barrier and emits correctly classified diagnostics.

**Architecture:** Put performance-only models, TREE parsing, calibration, synchronized execution, verdicts, and serialization under `select_fuzz.performance`. Reuse the core config/domain/executor/watchdog/artifact layers and expose one thin adapter to the shared CLI/service/control plane; do not add a second supervisor or frontend.

**Tech Stack:** Python 3.11, dataclasses/protocols, pytest, `threading.Barrier`, `concurrent.futures`, `statistics.median`, MySQL 8.0.41 `EXPLAIN ANALYZE FORMAT=TREE`, Performance Schema, Typer/FastAPI integration tests.

---

## Boundaries and file map

Core prerequisites are `src/select_fuzz/config/{models.py,loader.py}`, `src/select_fuzz/domain/{models.py,values.py}`, `src/select_fuzz/execution/{protocols.py,mysql.py,timeout.py}`, and `src/select_fuzz/artifacts/{jsonl.py,bundle.py}`. In particular, reuse:

```python
NodeQueryRunner.run(node, database, sql, *, timeout_s, row_limit, byte_limit,
                    barrier: threading.Barrier | None = None) -> NodeExecution
KillQueryWatchdog.arm(node, database, connection_id, timeout_s) -> KillHandle
```

`NodeExecution` supplies role/status/start and end monotonic nanoseconds/connection ID/error. Extend it only with a performance payload in the core integration task; never duplicate connection setup, independent-control-connection `KILL QUERY`, JSONL fsync, bundle atomicity, or task supervision.

- `src/select_fuzz/performance/models.py`: scale knobs, policy, samples, measurements, verdicts.
- `src/select_fuzz/performance/tree.py`: TREE parsing, scientific numbers, root timing, shape boundaries.
- `src/select_fuzz/performance/materialization.py`: deterministic scale manifest and concurrent three-node rebuild/verification.
- `src/select_fuzz/performance/calibration.py`: plan-seeded scale progression and three-run medians.
- `src/select_fuzz/performance/diagnostics.py`: Performance Schema/session metrics.
- `src/select_fuzz/performance/execution.py`: three-way barrier, one-shot formal runs, timeout normalization.
- `src/select_fuzz/performance/oracle.py`: skew/timeout/two-reference verdict precedence.
- `src/select_fuzz/performance/artifacts.py`: compact pass record and full diagnostic bundle.
- `src/select_fuzz/performance/service.py`: sequential 100-query performance worker adapter.
- `tests/performance/`, `tests/integration/test_performance_mysql.py`, `tests/cli/test_performance_run.py`, `tests/control_plane/test_performance_run_contract.py`: tests only; no frontend files.

### Task 1: Define policy and every workload scale knob

**Files:**
- Create: `src/select_fuzz/performance/__init__.py`
- Create: `src/select_fuzz/performance/models.py`
- Create: `tests/performance/test_models.py`

- [ ] **Step 1: Write the failing defaults and invariant tests**

```python
def test_defaults_and_knobs():
    p, s = PerformancePolicy(), WorkloadScale()
    assert (p.worker_count, p.queries_per_round) == (1, 100)
    assert (p.calibration_runs, p.calibration_band_s) == (3, (5.0, 30.0))
    assert (p.max_calibration_rounds, p.timeout_s) == (8, 60.0)
    assert (p.threshold, p.max_skew_ms, p.cache_state) == (.20, 100.0, "unverified")
    assert vars(s) == {"table_rows": 100_000, "scan_rows": 100_000,
      "range_selectivity": .10, "join_build_rows": 25_000,
      "join_probe_rows": 100_000, "join_fanout": 4.0,
      "aggregate_input_rows": 100_000, "aggregate_groups": 1_000,
      "sort_rows": 100_000, "sort_key_bytes": 32,
      "window_partition_rows": 1_000, "window_frame_rows": 100}

def test_scale_caps_rows_and_validates_relations():
    assert WorkloadScale().scaled(1000, 50_000_000).table_rows == 50_000_000
    with pytest.raises(ValueError):
        WorkloadScale(window_partition_rows=10, window_frame_rows=11)
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/performance/test_models.py`

Expected: FAIL with `ModuleNotFoundError: select_fuzz.performance`.

- [ ] **Step 3: Implement the immutable values**

```python
@dataclass(frozen=True)
class PerformancePolicy:
    worker_count: int = 1; queries_per_round: int = 100
    calibration_runs: int = 3; calibration_band_s: tuple[float, float] = (5., 30.)
    max_calibration_rounds: int = 8; timeout_s: float = 60.
    threshold: float = .20; max_skew_ms: float = 100.
    scale_multiplier: float = 2.; cache_state: str = "unverified"
    def __post_init__(self):
        if self.worker_count != 1 or self.calibration_runs != 3:
            raise ValueError("performance requires one worker and three calibration runs")

@dataclass(frozen=True)
class WorkloadScale:
    table_rows: int = 100_000; scan_rows: int = 100_000
    range_selectivity: float = .10; join_build_rows: int = 25_000
    join_probe_rows: int = 100_000; join_fanout: float = 4.
    aggregate_input_rows: int = 100_000; aggregate_groups: int = 1_000
    sort_rows: int = 100_000; sort_key_bytes: int = 32
    window_partition_rows: int = 1_000; window_frame_rows: int = 100
    def __post_init__(self):
        if not 0 < self.range_selectivity <= 1 or self.aggregate_groups > self.aggregate_input_rows \
           or self.window_frame_rows > self.window_partition_rows:
            raise ValueError("invalid workload scale")
    def scaled(self, factor: float, cap: int):
        counts = ("table_rows", "scan_rows", "join_build_rows", "join_probe_rows",
                  "aggregate_input_rows", "aggregate_groups", "sort_rows",
                  "window_partition_rows", "window_frame_rows")
        values = {n: min(cap, max(1, ceil(getattr(self, n) * factor))) for n in counts}
        values["aggregate_groups"] = min(values["aggregate_groups"], values["aggregate_input_rows"])
        values["window_frame_rows"] = min(values["window_frame_rows"], values["window_partition_rows"])
        return replace(self, **values)
```

- [ ] **Step 4: Run GREEN**

Run: `uv run pytest -q tests/performance/test_models.py`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/performance tests/performance/test_models.py
git commit -m "feat(performance): define scale policy"
```

### Task 2: Parse TREE plans and gate intended shapes

**Files:**
- Create: `src/select_fuzz/performance/tree.py`
- Create: `tests/performance/test_tree.py`

- [ ] **Step 1: Write failing parser/boundary tests**

```python
TREE = """-> Sort: k (cost=1.2e+3 rows=2.5e+4) (actual time=1.25e-4..6.02e+3 rows=25000 loops=1)
    -> Table scan on t (cost=1 rows=25000) (actual time=5..7e+3 rows=25000 loops=1)"""
def test_scientific_notation_and_root_b_not_child_max():
    plan = parse_tree(TREE, completed=True)
    assert plan.root.start_ms == pytest.approx(.000125)
    assert plan.root.end_ms == pytest.approx(6020)

def test_boundary_is_per_role_not_exact_plan_equality():
    boundary = ShapeBoundary(required=frozenset({Family.SCAN}))
    boundary.validate(parse_tree("-> Table scan on a (cost=1 rows=10)"), "baseline")
    boundary.validate(parse_tree("-> Index range scan on b (cost=1 rows=12)"), "custom_off")

@pytest.mark.parametrize("tree", ["", "-> Scan (cost=1 rows=1)",
  "-> A (actual time=1..2 rows=1 loops=1)\n-> B (actual time=1..2 rows=1 loops=1)"])
def test_completed_tree_rejects_missing_or_ambiguous_root(tree):
    with pytest.raises(PlanParseError): parse_tree(tree, completed=True)
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/performance/test_tree.py`
Expected: FAIL because `performance.tree` does not exist.

- [ ] **Step 3: Implement numeric/root/operator parsing**

```python
NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
ACTUAL = re.compile(rf"actual time=(?P<a>{NUM})\.\.(?P<b>{NUM}) rows=(?P<r>{NUM}) loops=(?P<l>\d+)")
EST = re.compile(rf"cost=.*?rows=(?P<r>{NUM})")
class Family(StrEnum): SCAN="SCAN"; JOIN="JOIN"; AGGREGATE="AGGREGATE"; SORT="SORT"; WINDOW="WINDOW"

def parse_tree(text: str, completed=False) -> TreePlan:
    nodes = [parse_node(line.index("->"), line.split("->", 1)[1].strip())
             for line in text.splitlines() if "->" in line]
    if not nodes: raise PlanParseError("no iterator")
    roots = [n for n in nodes if n.indent == min(x.indent for x in nodes)]
    if len(roots) != 1: raise PlanParseError("ambiguous root")
    if completed and roots[0].end_ms is None: raise PlanParseError("incomplete root")
    return TreePlan(tuple(nodes), roots[0], text)

@dataclass(frozen=True)
class ShapeBoundary:
    required: frozenset[Family]
    def validate(self, plan, role):
        missing = self.required - {n.family for n in plan.nodes}
        if missing: raise PlanParseError(f"{role} misses {sorted(missing)}")
```

`parse_node` must classify scan/range as `SCAN`, nested-loop/hash joins as `JOIN`, and aggregate/sort/window separately; store estimated rows for plan-based scale seeding. Do not require identical plans across roles and do not gate `custom_on` against reference text.

- [ ] **Step 4: Run GREEN and commit**

Run: `uv run pytest -q tests/performance/test_tree.py`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/performance/tree.py tests/performance/test_tree.py
git commit -m "feat(performance): parse tree plans"
```

### Task 3: Plan-seed and calibrate references

**Files:**
- Create: `src/select_fuzz/performance/materialization.py`
- Create: `src/select_fuzz/performance/calibration.py`
- Create: `tests/performance/test_calibration.py`

- [ ] **Step 1: Write failing calibration tests**

```python
def test_three_runs_each_and_inclusive_band():
    port = FakePort(baseline=[4,5,6], custom_off=[29,30,31])
    materializer = FakeMaterializer()
    frozen = CalibrationEngine(port, materializer, PerformancePolicy()).calibrate(
        template, WorkloadScale(), database="perf_round_7")
    assert frozen.medians == {NodeRole.BASELINE: 5, NodeRole.CUSTOM_OFF: 30}
    assert port.analyze_roles == [NodeRole.BASELINE, NodeRole.CUSTOM_OFF] * 3
    assert NodeRole.CUSTOM_ON not in port.analyze_roles
    assert (frozen.sql, frozen.seed) == (template.render(frozen.scale), template.seed)
    assert frozen.database == "perf_round_7"
    assert frozen.data_manifest == template.data_manifest(frozen.scale)
    assert materializer.calls[-1] == ("perf_round_7", frozen.data_manifest, ALL_ROLES)

def test_eight_rounds_then_exhausted_with_diagnostics():
    with pytest.raises(CalibrationExhausted) as error:
        CalibrationEngine(FakePort(always_seconds=1), FakeMaterializer(), PerformancePolicy()).calibrate(
            template, WorkloadScale(), database="perf_round_8")
    assert len(error.value.attempts) == 8

def test_materialization_requires_same_digest_and_row_counts_on_all_roles():
    materializer = ScaleMaterializer(fake_setup_port(custom_on_rows=99))
    with pytest.raises(MaterializationMismatch):
        materializer.rebuild_all("perf_round_9", manifest(rows=100))
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/performance/test_calibration.py`
Expected: FAIL because `performance.calibration` does not exist.

- [ ] **Step 3: Implement plan seeding and median decisions**

```python
REFS = (NodeRole.BASELINE, NodeRole.CUSTOM_OFF)
def in_band(v, policy): return policy.calibration_band_s[0] <= v <= policy.calibration_band_s[1]

class CalibrationEngine:
    def calibrate(self, template, initial, database):
        self.materializer.rebuild_all(database, template.data_manifest(initial))
        base_sql = template.render(initial)
        plans = {r: parse_tree(self.port.explain_tree(r, base_sql)) for r in REFS}
        for r, p in plans.items(): template.boundary.validate(p, r.value)
        observed = max(p.estimated_work(template.driver_family) for p in plans.values())
        scale = initial.scaled(min(8, max(.25, template.target_rows(initial)/observed)), 50_000_000)
        attempts = []
        for number in range(1, self.policy.max_calibration_rounds + 1):
            data_manifest = template.data_manifest(scale)
            self.materializer.rebuild_all(database, data_manifest)
            sql, samples = template.render(scale), {r: [] for r in REFS}
            for _ in range(3):
                for role in REFS:
                    raw = self.port.analyze(role, sql, timeout_s=60)
                    if raw.completed:
                        samples[role].append(parse_tree(raw.tree, completed=True).root.end_ms / 1000)
            medians = {r: median(v) for r, v in samples.items() if len(v) == 3}
            attempts.append(CalibrationAttempt(number, scale, sql, samples, medians))
            if len(medians) == 2 and all(in_band(v, self.policy) for v in medians.values()):
                return FrozenCase(template.seed, database, scale, data_manifest, sql,
                                  medians, tuple(attempts))
            factor = .5 if len(medians) < 2 or max(medians.values()) > 30 else 2
            scale = scale.scaled(factor, 50_000_000)
        raise CalibrationExhausted(tuple(attempts))
```

`ScaleMaterializer.rebuild_all()` deterministically renders one data manifest, concurrently applies that exact manifest to all three roles, and verifies schema digest, row counts, and sampled content digest before any EXPLAIN/ANALYZE. A changed scale always triggers a three-node rebuild; materialization failure is setup/infrastructure classification, never a timing sample. Each candidate must pass both reference shape boundaries before ANALYZE. A timeout is a too-slow calibration sample and scales down; one reference below 5 while the other exceeds 30 terminates as reference-band divergence. Formal execution receives the accepted `FrozenCase` object unchanged, including database and data-manifest digest, so data, SQL, and seed are frozen.

- [ ] **Step 4: Run GREEN**

Run: `uv run pytest -q tests/performance/test_calibration.py`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/performance/materialization.py src/select_fuzz/performance/calibration.py tests/performance/test_calibration.py
git commit -m "feat(performance): calibrate reference nodes"
```

### Task 4: Run one synchronized formal measurement with diagnostics and KILL

**Files:**
- Create: `src/select_fuzz/performance/diagnostics.py`
- Create: `src/select_fuzz/performance/execution.py`
- Create: `tests/performance/test_execution.py`

- [ ] **Step 1: Write failing barrier, one-shot, skew, and partial-plan tests**

```python
def test_formal_run_executes_exactly_once_per_role_at_sixty_seconds():
    run = FormalRunner(fake_core, fake_metrics, PerformancePolicy()).run(frozen)
    assert fake_core.calls == [(r, frozen.sql, 60.0) for r in ALL_ROLES]
    assert len({call.barrier for call in fake_core.call_details}) == 1
    assert run.start_skew_ms == pytest.approx(80)
    assert all(m.cache_state == "unverified" for m in run.measurements.values())

def test_watchdog_kill_is_timeout_but_partial_tree_has_no_root_time():
    fake_core.result = killed(errno=1317, watchdog_fired=True, partial_tree="-> Scan ...")
    measurement = FormalRunner(fake_core, fake_metrics, PerformancePolicy()).run(frozen).custom_on
    assert measurement.outcome is Outcome.TIMEOUT
    assert measurement.root_end_ms is None

@pytest.mark.parametrize("errno,fired,outcome", [(3024,False,Outcome.TIMEOUT),
  (1317,True,Outcome.TIMEOUT), (1317,False,Outcome.EXECUTION_ERROR),
  (2013,False,Outcome.INFRA_ERROR)])
def test_timeout_and_disconnect_classification(errno, fired, outcome):
    assert classify_error(error(errno), watchdog_fired=fired) is outcome
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/performance/test_execution.py`
Expected: FAIL because `performance.execution` does not exist.

- [ ] **Step 3: Implement best-effort diagnostics**

```python
PFS_SQL = """SELECT TIMER_WAIT/1000000000, LOCK_TIME/1000000000,
 ROWS_EXAMINED, ROWS_SENT, CREATED_TMP_DISK_TABLES, NO_INDEX_USED
 FROM performance_schema.events_statements_history_long
 WHERE THREAD_ID=%s ORDER BY EVENT_ID DESC LIMIT 1"""
STATUS_NAMES = ("Handler_read_rnd_next", "Created_tmp_tables", "Created_tmp_disk_tables",
                "Innodb_buffer_pool_reads", "Innodb_buffer_pool_read_ahead")

def metric_delta(before, after):
    return {name: after.get(name, 0) - before.get(name, 0) for name in STATUS_NAMES}
```

Use the query connection for session-status snapshots and a control connection for PFS lookup, keyed by Performance Schema thread ID. Missing consumer/table/privilege produces `metric_errors`; it never changes the verdict. Preserve complete TREE, monotonic wall time, PFS values, status deltas, connection ID, and watchdog outcome for diagnostics.

- [ ] **Step 4: Implement formal execution and error normalization**

```python
ALL_ROLES = (NodeRole.BASELINE, NodeRole.CUSTOM_OFF, NodeRole.CUSTOM_ON)
def classify_error(error, watchdog_fired):
    if error.errno == 3024 or (error.errno == 1317 and watchdog_fired): return Outcome.TIMEOUT
    if error.errno in {2006, 2013, 2055}: return Outcome.INFRA_ERROR
    return Outcome.EXECUTION_ERROR

class FormalRunner:
    def run(self, frozen):
        barrier = threading.Barrier(3)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {r: pool.submit(self._one, r, frozen, barrier) for r in ALL_ROLES}
        measurements = {r: f.result() for r, f in futures.items()}
        starts = [m.started_ns for m in measurements.values()]
        return FormalRun(measurements, (max(starts) - min(starts)) / 1_000_000)

    def _one(self, role, frozen, barrier):
        raw = self.core.run(role, frozen.database, frozen.sql, timeout_s=60,
                            row_limit=0, byte_limit=0, barrier=barrier)
        outcome = normalize(raw)
        plan = parse_tree(raw.tree, completed=True) if outcome is Outcome.COMPLETED else None
        return Measurement.from_raw(raw, outcome, plan, cache_state="unverified")
```

The core adapter sets session `MAX_EXECUTION_TIME=60000` and arms `KillQueryWatchdog` for 60 seconds before the barrier; the watchdog uses its independent control connection. `KillHandle.cancel()` runs after completion. Never parse a TREE from a killed/timed-out result and never issue a warm-up or cache-probe query.

- [ ] **Step 5: Run GREEN and commit**

Run: `uv run pytest -q tests/performance/test_execution.py`
Expected: all tests PASS.

```bash
git add src/select_fuzz/performance/{diagnostics.py,execution.py} tests/performance/test_execution.py
git commit -m "feat(performance): synchronize formal measurements"
```

### Task 5: Classify skew, timeouts, and both regression comparisons

**Files:**
- Create: `src/select_fuzz/performance/oracle.py`
- Create: `tests/performance/test_oracle.py`

- [ ] **Step 1: Write the failing verdict table**

```python
@pytest.mark.parametrize("case,verdict", [
  (times(10,10,12), Verdict.PERF_ALERT),
  (times(10,20,12), Verdict.PERF_ALERT),
  (all_timeout(), Verdict.OVER_BUDGET),
  (reference_timeout(), Verdict.CALIBRATION_DRIFT),
  (custom_on_timeout(), Verdict.PERF_ALERT),
  (infra_error(), Verdict.INFRA_ERROR),
])
def test_verdict_precedence(case, verdict): assert assess(case, threshold=.20).verdict is verdict

def test_exact_twenty_percent_is_alert_on_each_reference():
    result = assess(times(baseline=10, custom_off=20, custom_on=12), threshold=.20)
    assert result.reasons == ("VS_BASELINE",)

def test_skew_over_100ms_suppresses_alert_but_exactly_100_does_not():
    assert assess(times(10,10,20,skew=100.001)).verdict is Verdict.TIMING_UNRELIABLE
    assert assess(times(10,10,20,skew=100)).verdict is Verdict.PERF_ALERT
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/performance/test_oracle.py`
Expected: FAIL because `performance.oracle` does not exist.

- [ ] **Step 3: Implement explicit precedence and two alert reasons**

```python
def assess(run, threshold=.20, max_skew_ms=100):
    m = run.measurements
    if any(x.outcome is Outcome.INFRA_ERROR for x in m.values()): return Assessment(Verdict.INFRA_ERROR)
    if all(x.outcome is Outcome.TIMEOUT for x in m.values()): return Assessment(Verdict.OVER_BUDGET)
    if any(m[r].outcome is Outcome.TIMEOUT for r in REFERENCE_ROLES):
        return Assessment(Verdict.CALIBRATION_DRIFT)
    if run.start_skew_ms > max_skew_ms: return Assessment(Verdict.TIMING_UNRELIABLE)
    if m[NodeRole.CUSTOM_ON].outcome is Outcome.TIMEOUT:
        return Assessment(Verdict.PERF_ALERT, ("CUSTOM_ON_TIMEOUT",))
    if any(x.outcome is not Outcome.COMPLETED for x in m.values()):
        return Assessment(Verdict.EXECUTION_ERROR)
    on = m[NodeRole.CUSTOM_ON].root_end_ms
    reasons = tuple(label for role, label in ((NodeRole.CUSTOM_OFF,"VS_CUSTOM_OFF"),
      (NodeRole.BASELINE,"VS_BASELINE")) if on >= m[role].root_end_ms * (1 + threshold))
    return Assessment(Verdict.PERF_ALERT if reasons else Verdict.PASS, reasons)
```

- [ ] **Step 4: Run GREEN**

Run: `uv run pytest -q tests/performance/test_oracle.py`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/performance/oracle.py tests/performance/test_oracle.py
git commit -m "feat(performance): classify performance alerts"
```

### Task 6: Persist diagnostics and run the sequential performance service

**Files:**
- Create: `src/select_fuzz/performance/artifacts.py`
- Create: `src/select_fuzz/performance/service.py`
- Create: `tests/performance/test_service.py`

- [ ] **Step 1: Write failing service/artifact tests**

```python
def test_formal_query_is_once_per_node_without_warmup():
    service.run(templates=[template], rounds=1)
    assert formal.calls == [frozen]
    assert recorder.records[0]["cache_state"] == "unverified"

def test_non_pass_gets_plans_metrics_and_calibration_history():
    record = recorder.record(frozen, run, Assessment(Verdict.PERF_ALERT,("VS_BASELINE",)))
    assert set(record["measurements"]) == {"baseline","custom_off","custom_on"}
    assert bundle.files >= {"plans/baseline.tree","plans/custom_off.tree","plans/custom_on.tree",
                            "diagnostics/metrics.json","calibration.json"}
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/performance/test_service.py`
Expected: FAIL because performance artifacts/service modules do not exist.

- [ ] **Step 3: Implement recording and orchestration**

```python
class PerformanceService:
    def run(self, templates, *, rounds=None, queries_per_round=None, stop_event=None):
        templates = tuple(templates)
        if not templates:
            raise ValueError("performance template catalog is empty")
        per_round = queries_per_round or self.policy.queries_per_round
        round_numbers = count(1) if rounds is None else range(1, rounds + 1)
        for round_number in round_numbers:
            database = self.database_names.new("performance", round_number)
            for query_number, template in enumerate(islice(cycle(templates), per_round), 1):
                if stop_event is not None and stop_event.is_set(): return
                try:
                    frozen = self.calibration.calibrate(
                        template.for_case(round_number, query_number),
                        template.initial_scale,
                        database=database,
                    )
                except CalibrationExhausted as error:
                    self.recorder.record_calibration_failure(template, error.attempts); continue
                formal = self.runner.run(frozen)
                self.recorder.record(frozen, formal, assess(formal, self.policy.threshold,
                                                            self.policy.max_skew_ms))

class PerformanceRecorder:
    def record(self, frozen, run, assessment):
        record = compact_record(frozen, run, assessment, cache_state="unverified")
        self.jsonl.append(record)
        if assessment.verdict is not Verdict.PASS:
            self.bundles.write(frozen.case_id, full_manifest(record), diagnostic_files(frozen, run))
        return record
```

All findings include three configuration fingerprints. Passing records keep timings/metric summary/coverage tags; `PERF_ALERT`, `TIMING_UNRELIABLE`, `OVER_BUDGET`, `CALIBRATION_DRIFT`, parse/error, and infrastructure results keep full TREE plans, PFS/session metrics, wall times, calibration trials, SQL, seed, database, and manifest.

- [ ] **Step 4: Run GREEN and commit**

Run: `uv run pytest -q tests/performance/test_service.py`
Expected: all tests PASS.

```bash
git add src/select_fuzz/performance/{artifacts.py,service.py} tests/performance/test_service.py
git commit -m "feat(performance): record diagnostic runs"
```

### Task 7: Wire CLI/API contracts and execute release gates

**Files:**
- Create: `src/select_fuzz/performance/entrypoint.py`
- Modify: `src/select_fuzz/service.py`
- Modify: `src/select_fuzz/cli.py`
- Create: `tests/cli/test_performance_run.py`
- Create: `tests/control_plane/test_performance_run_contract.py`
- Create: `tests/integration/test_performance_mysql.py`

- [ ] **Step 1: Write failing CLI and cross-plan API contract tests**

```python
def test_cli_dispatches_performance_options(runner, cli):
    result = cli.invoke(["run","--mode","performance","--rounds","2",
                         "--queries-per-round","17","--seed","7"])
    assert result.exit_code == 0
    runner.assert_called_once_with(mode="performance", rounds=2, queries_per_round=17, seed=7)

def test_api_accepts_single_worker_and_preserves_options(client, launcher):
    response = client.post("/api/v1/runs", json={"mode":"performance","workers":1,
                           "seed":7,"rounds":2,"queries_per_round":17})
    assert response.status_code == 202
    assert launcher.last.options == {"mode":"performance","workers":1,"seed":7,
                                     "rounds":2,"queries_per_round":17}
```

The control-plane plan owns the separate `workers != 1 -> 422` test and generic launcher; keep this file as an integration gate rather than reimplementing its route.

- [ ] **Step 2: Run interface tests and verify RED**

Run: `uv run pytest -q tests/cli/test_performance_run.py tests/control_plane/test_performance_run_contract.py`
Expected: FAIL because the performance mode adapter is not registered.

- [ ] **Step 3: Add the thin mode adapter and registration**

```python
# src/select_fuzz/performance/entrypoint.py
def run_performance(app_config, run_options, dependencies):
    policy = PerformancePolicy.from_config(app_config.performance)
    return dependencies.performance_service(policy).run(
        templates=dependencies.performance_templates(seed=run_options.seed),
        rounds=run_options.rounds,
        queries_per_round=run_options.queries_per_round,
        stop_event=run_options.stop_event,
    )

# shared registry in src/select_fuzz/service.py
MODE_RUNNERS["performance"] = run_performance
```

Keep `src/select_fuzz/cli.py` as a thin consumer of `MODE_RUNNERS`; do not add performance-specific supervision or API routes.

- [ ] **Step 4: Run interface tests and verify GREEN**

Run: `uv run pytest -q tests/cli/test_performance_run.py tests/control_plane/test_performance_run_contract.py`
Expected: all tests PASS.

- [ ] **Step 5: Add and run opt-in MySQL integration tests**

Test one TREE with scientific notation, exactly three formal executions, `MAX_EXECUTION_TIME`, watchdog cleanup (`PROCESSLIST` shows no surviving connection query), diagnostics fallback when PFS is unavailable, and the `cache_state=unverified` marker.

Run: `SELECT_FUZZ_CONFIG=tests/fixtures/mysql-8.0.41-three-node.yaml uv run pytest -q -m mysql_performance tests/integration/test_performance_mysql.py`
Expected: PASS on the three-node 8.0.41 release matrix; local 8.0.45 is smoke-only.

- [ ] **Step 6: Run the complete performance suite**

Run: `uv run pytest -q tests/performance tests/cli/test_performance_run.py tests/control_plane/test_performance_run_contract.py`
Expected: all tests PASS with no warm-cache verification query and no frontend change.

- [ ] **Step 7: Commit the integration**

```bash
git add src/select_fuzz/performance/entrypoint.py src/select_fuzz/service.py src/select_fuzz/cli.py tests/cli/test_performance_run.py tests/control_plane/test_performance_run_contract.py tests/integration/test_performance_mysql.py
git commit -m "feat(performance): expose performance run mode"
```
