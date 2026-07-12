# MySQL Fuzzer Core and Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic Python core, coverage-driven MySQL schema/data/query generator, three-node correctness executor and oracle, durable artifacts, replay, doctor, and CLI vertical slice.

**Architecture:** Keep pure deterministic generation and comparison separate from MySQL adapters and filesystem effects. A correctness service coordinates one immutable case manifest across three fixed roles, publishes typed events, and writes append-only artifacts; performance and FastAPI consume the same domain, execution, service, and artifact ports.

**Tech Stack:** Python 3.11+, uv, Pydantic v2, PyYAML, Typer, mysql-connector-python, pytest, Hypothesis, ruff, mypy.

---

## Shared Interface Map

- `src/select_fuzz/config/models.py`: `AppConfig`, `NodeConfig`, `CorrectnessConfig`, `PerformanceConfig`.
- `src/select_fuzz/config/loader.py`: YAML, environment-secret, and CLI override loading.
- `src/select_fuzz/domain/models.py`: roles, metadata, outcomes, manifests, findings, requests, events.
- `src/select_fuzz/domain/values.py`: deterministic seed derivation, IDs, fingerprints.
- `src/select_fuzz/generation/`: feature catalog, coverage debt, schema, data, query AST, safety.
- `src/select_fuzz/execution/`: protocols, MySQL runner, watchdog, tri-node coordinator.
- `src/select_fuzz/oracle/`: canonical typed values, result/error/timeout comparison.
- `src/select_fuzz/artifacts/`: fsynced JSONL, atomic bundles, readers.
- `src/select_fuzz/correctness.py`: `CorrectnessRunService`.
- `src/select_fuzz/replay.py`: `ReplayService`.
- `src/select_fuzz/service.py`: shared `MODE_RUNNERS`, `EventSink`, `FindingReader`.
- `src/select_fuzz/cli.py`, `src/select_fuzz/__main__.py`: `select-fuzz` and module entry points.

Freeze these cross-plan method signatures:

```python
NodeQueryRunner.run(node, database, sql, *, timeout_s, row_limit, byte_limit,
                    barrier=None) -> NodeExecution
KillQueryWatchdog.arm(node, database, connection_id, timeout_s) -> KillHandle
CorrectnessRunService.run(request: RunRequest, stop_event: threading.Event) -> RunSummary
EventSink.publish(event: RunEvent) -> None
FindingReader.iter_findings(cursor=None) -> Iterator[Finding]
FindingReader.get(case_id: str) -> Finding
ReplayService.replay(case_id: str) -> ReplayResult
```

### Task 1: Bootstrap the Python package and red-green toolchain

**Files:**
- Create: `pyproject.toml`
- Create: `src/select_fuzz/__init__.py`
- Create: `src/select_fuzz/__main__.py`
- Create: `src/select_fuzz/cli.py`
- Create: `tests/test_package.py`
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write the failing package/CLI smoke test**

```python
from typer.testing import CliRunner

from select_fuzz import __version__
from select_fuzz.cli import app


def test_package_and_cli_are_importable() -> None:
    assert __version__ == "0.1.0"
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.stdout
    assert "doctor" in result.stdout
```

- [ ] **Step 2: Prove the test is red**

Run: `uv run pytest -q tests/test_package.py`

Expected: FAIL because the package and `pyproject.toml` do not exist.

- [ ] **Step 3: Add exact project metadata and minimal entry points**

```toml
[project]
name = "select-fuzz"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "mysql-connector-python>=9.0,<10",
  "pydantic>=2.8,<3",
  "PyYAML>=6.0,<7",
  "typer>=0.12,<1",
]

[project.scripts]
select-fuzz = "select_fuzz.cli:app"

[dependency-groups]
dev = [
  "hypothesis>=6.100",
  "mypy>=1.10",
  "pytest>=8.2",
  "pytest-cov>=5",
  "pytest-timeout>=2.3",
  "ruff>=0.5",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["mysql: real MySQL integration", "soak: long-running tests"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.mypy]
python_version = "3.11"
strict = true
packages = ["select_fuzz"]
```

```python
# src/select_fuzz/cli.py
import typer

app = typer.Typer(no_args_is_help=True)

for name in ("run", "serve", "doctor", "replay", "report", "cleanup"):
    app.command(name=name)(lambda: None)
```

- [ ] **Step 4: Lock and run the baseline gate**

Run: `uv lock && uv run ruff check . && uv run mypy src && uv run pytest -q`

Expected: lockfile is created and every command exits 0.

- [ ] **Step 5: Commit the bootstrap**

```bash
git add pyproject.toml uv.lock src/select_fuzz tests/test_package.py .github/workflows/ci.yml
git commit -m "chore: bootstrap select fuzz project"
```

### Task 2: Typed configuration and secret-safe loading

**Files:**
- Modify: `.gitignore`
- Create: `src/select_fuzz/config/__init__.py`
- Create: `src/select_fuzz/config/models.py`
- Create: `src/select_fuzz/config/loader.py`
- Create: `config/example.yaml`
- Create: `.env.example`
- Test: `tests/config/test_loader.py`

- [ ] **Step 1: Write failing defaults, override, and redaction tests**

```python
def test_mode_defaults_and_secret_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv("SELECT_FUZZ_MYSQL_PASSWORD", "runtime-secret")
    config = load_config(write_config(tmp_path, workers=12), cli={"workers": 8})
    assert config.correctness.workers == 8
    assert config.correctness.queries_per_round == 1000
    assert config.performance.workers == 1
    assert config.performance.queries_per_round == 100
    assert "runtime-secret" not in config.model_dump_json()
    assert "runtime-secret" not in repr(config)


def test_performance_workers_must_equal_one():
    with pytest.raises(ValidationError):
        PerformanceConfig(workers=2)
```

- [ ] **Step 2: Prove red**

Run: `uv run pytest -q tests/config/test_loader.py`

Expected: import failure for `select_fuzz.config`.

- [ ] **Step 3: Implement fixed roles and approved defaults**

```python
class NodeRole(StrEnum):
    BASELINE = "baseline"
    CUSTOM_OFF = "custom_off"
    CUSTOM_ON = "custom_on"


class NodeConfig(BaseModel):
    role: NodeRole
    host: str
    port: int = Field(ge=1, le=65535)
    username_env: str = "SELECT_FUZZ_MYSQL_USER"
    password_env: str = "SELECT_FUZZ_MYSQL_PASSWORD"
    role_probe_sql: str | None = None
    role_probe_expected: str | None = None


class CorrectnessConfig(BaseModel):
    workers: int = Field(default=10, ge=1, le=64)
    queries_per_round: int = Field(default=1000, ge=1)
    timeout_seconds: float = 15.0
    row_limit: int = 10_000
    byte_limit: int = 32 * 1024 * 1024
    free_random_rate: float = 0.05
    negative_mutation_rate: float = 0.05


class PerformanceConfig(BaseModel):
    workers: Literal[1] = 1
    queries_per_round: int = 100
    initial_table_rows: int = 100_000
    max_table_rows: int = 50_000_000
    max_calibration_rounds: int = 8
    calibration_runs_per_reference: int = 3
    calibration_min_seconds: float = 5.0
    calibration_max_seconds: float = 30.0
    formal_timeout_seconds: float = 60.0
    regression_threshold: float = 0.20
    max_start_skew_ms: float = 100.0
```

Store only environment variable names in models. Resolve secret values immediately before connector creation and wrap them in a `SecretStr`; never attach them to manifests or events.

Add `.local/`, `config/local.yaml`, and `config/local-8041.yaml` to `.gitignore`. Commit only `config/example.yaml`; operators copy it to an ignored local file before supplying host addresses.

- [ ] **Step 4: Add config-difference warning and essential-permission policy tests**

Run: `uv run pytest -q tests/config`

Expected: CLI override wins, role/config differences produce warnings, essential capability failures are fatal, and no serialized object contains the test secret.

- [ ] **Step 5: Commit configuration**

```bash
git add .gitignore src/select_fuzz/config tests/config config/example.yaml .env.example
git commit -m "feat: add secret-safe three-node configuration"
```

### Task 3: Domain contracts, deterministic IDs, and seed tree

**Files:**
- Create: `src/select_fuzz/domain/__init__.py`
- Create: `src/select_fuzz/domain/models.py`
- Create: `src/select_fuzz/domain/values.py`
- Test: `tests/domain/test_values.py`
- Test: `tests/domain/test_models.py`

- [ ] **Step 1: Write failing deterministic seed and outcome tests**

```python
def test_seed_tree_is_deterministic_and_worker_safe():
    tree = SeedTree(root=42)
    assert tree.derive("worker", 3, "round", 7, "query", 9) == tree.derive(
        "worker", 3, "round", 7, "query", 9
    )
    assert tree.derive("worker", 3) != tree.derive("worker", 4)


def test_node_execution_records_typed_timing():
    result = NodeExecution.success(
        role=NodeRole.BASELINE, connection_id=12, started_ns=10, ended_ns=20,
        columns=(), rows=(), warnings=(),
    )
    assert result.elapsed_ns == 10
    assert result.status is ExecutionStatus.SUCCESS
```

- [ ] **Step 2: Prove red**

Run: `uv run pytest -q tests/domain`

Expected: domain imports fail.

- [ ] **Step 3: Implement immutable shared models**

```python
class ExecutionStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    INFRA_ERROR = "infra_error"


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    errno: int
    sqlstate: str
    message: str


@dataclass(frozen=True, slots=True)
class ColumnMeta:
    name: str
    type_code: int
    nullable: bool
    unsigned: bool
    binary: bool


@dataclass(frozen=True, slots=True)
class NodeExecution:
    role: NodeRole
    status: ExecutionStatus
    started_ns: int
    ended_ns: int
    connection_id: int | None
    columns: tuple[ColumnMeta, ...] = ()
    rows: tuple[tuple[object, ...], ...] = ()
    error: ErrorInfo | None = None
    warnings: tuple[str, ...] = ()
    watchdog_fired: bool = False
    performance_payload: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class RunRequest:
    run_id: str
    mode: Literal["correctness", "performance"]
    seed: int
    workers: int
    rounds: int | None
    queries_per_round: int
```

Derive seeds by hashing length-prefixed labels and integers with BLAKE2b; do not use Python's process-randomized `hash()`.

- [ ] **Step 4: Run deterministic properties across process boundaries**

Run: `uv run pytest -q tests/domain --hypothesis-show-statistics`

Expected: subprocesses and worker order produce identical IDs/manifests with no collisions in the test corpus.

- [ ] **Step 5: Commit domain contracts**

```bash
git add src/select_fuzz/domain tests/domain
git commit -m "feat: define deterministic shared domain contracts"
```

### Task 4: Feature catalog and persistent coverage-debt scheduler

**Files:**
- Create: `src/select_fuzz/generation/__init__.py`
- Create: `src/select_fuzz/generation/catalog.py`
- Create: `src/select_fuzz/generation/coverage.py`
- Test: `tests/generation/test_coverage.py`

- [ ] **Step 1: Write failing debt and checkpoint tests**

```python
def test_scheduler_prefers_undercovered_features(tmp_path):
    ledger = CoverageLedger(tmp_path / "coverage.json")
    ledger.record("join.inner", hits=10)
    ledger.record("cte.recursive", hits=0)
    scheduler = CoverageScheduler(catalog=FeatureCatalog.default(), ledger=ledger, min_hits=10)
    assert scheduler.choose(FixedRandom(0)).feature_id == "cte.recursive"


def test_checkpoint_roundtrip_keeps_counts(tmp_path):
    ledger = CoverageLedger(tmp_path / "coverage.json")
    ledger.record("window.rows", hits=3)
    ledger.checkpoint()
    assert CoverageLedger.load(tmp_path / "coverage.json").hits("window.rows") == 3
```

- [ ] **Step 2: Prove red**

Run: `uv run pytest -q tests/generation/test_coverage.py`

Expected: generation coverage imports fail.

- [ ] **Step 3: Implement the versioned catalog and atomic ledger**

```python
@dataclass(frozen=True, slots=True)
class FeatureSpec:
    feature_id: str
    family: str
    min_version: tuple[int, int, int]
    compatible_profiles: frozenset[str]
    weight: float = 1.0


class CoverageScheduler:
    def choose(self, rng: random.Random) -> FeatureSpec:
        enabled = [item for item in self.catalog if self.version >= item.min_version]
        debt = [max(0, self.min_hits - self.ledger.hits(item.feature_id)) for item in enabled]
        weights = [(value + 1) * item.weight for value, item in zip(debt, enabled, strict=True)]
        return rng.choices(enabled, weights=weights, k=1)[0]
```

Expose `FeatureCatalog.signature_targets()` and directed target selection for the 12-hour capability auditor.

- [ ] **Step 4: Run unit/property tests and forced directed signatures**

Run: `uv run pytest -q tests/generation/test_coverage.py tests/property/test_coverage_scheduler.py`

Expected: debt decreases, cumulative counts never decrease, checkpoint recovery is exact, and every enabled feature is selectable.

- [ ] **Step 5: Commit coverage scheduling**

```bash
git add src/select_fuzz/generation tests/generation/test_coverage.py tests/property/test_coverage_scheduler.py
git commit -m "feat: add coverage-debt feature scheduling"
```

### Task 5: Schema profiles and MySQL compatibility rules

**Files:**
- Create: `src/select_fuzz/generation/schema.py`
- Create: `src/select_fuzz/generation/schema_rules.py`
- Create: `tests/fixtures/schema_rules.yaml`
- Test: `tests/generation/test_schema.py`
- Test: `tests/property/test_schema_rules.py`

- [ ] **Step 1: Write failing profile-rule examples**

```python
@pytest.mark.parametrize(
    ("profile", "features", "valid"),
    [
        ("partitioned", {"foreign_key"}, False),
        ("partitioned", {"fulltext"}, False),
        ("temporary", {"partitioned"}, False),
        ("foreign_key_graph", {"composite_fk"}, True),
        ("json_multivalue", {"unique_multivalue"}, False),
    ],
)
def test_profile_compatibility(profile, features, valid):
    assert SchemaRules.mysql_8041().allows(profile, features) is valid
```

- [ ] **Step 2: Prove red**

Run: `uv run pytest -q tests/generation/test_schema.py tests/property/test_schema_rules.py`

Expected: schema generator is missing.

- [ ] **Step 3: Implement typed schema IR and explicit compatibility predicates**

```python
@dataclass(frozen=True, slots=True)
class ColumnDef:
    name: str
    mysql_type: str
    nullable: bool
    charset: str | None = None
    collation: str | None = None


@dataclass(frozen=True, slots=True)
class TableDef:
    name: str
    temporary: bool
    columns: tuple[ColumnDef, ...]
    indexes: tuple[IndexDef, ...]
    partition: PartitionDef | None = None
    foreign_keys: tuple[ForeignKeyDef, ...] = ()


class SchemaGenerator:
    def generate(self, target: FeatureSpec, seed: int, limits: SchemaLimits) -> SchemaManifest:
        rng = random.Random(seed)
        profile = self.rules.choose_compatible_profile(target, rng)
        manifest = build_schema(profile, target, rng, limits)
        self.rules.validate(manifest)
        return manifest
```

Implement row-size/index-byte calculations from configured page size, row format, and charset width. Generate 1–8 tables and 2–16 columns by default.

- [ ] **Step 4: Run 10,000 schema properties and exact SQL snapshots**

Run: `uv run pytest -q tests/generation/test_schema.py tests/property/test_schema_rules.py --hypothesis-show-statistics`

Expected: valid lane never emits a forbidden combination; negative fixtures fail with a stable rule ID.

- [ ] **Step 5: Commit schema generation**

```bash
git add src/select_fuzz/generation/schema.py src/select_fuzz/generation/schema_rules.py tests/generation tests/property/test_schema_rules.py tests/fixtures/schema_rules.yaml
git commit -m "feat: generate compatible MySQL schema profiles"
```

### Task 6: Deterministic data generation and setup bundles

**Files:**
- Create: `src/select_fuzz/generation/data.py`
- Create: `src/select_fuzz/generation/setup.py`
- Test: `tests/generation/test_data.py`
- Test: `tests/property/test_data_generation.py`

- [ ] **Step 1: Write failing distribution and bundle identity tests**

```python
def test_one_data_bundle_is_reused_for_all_roles(schema):
    bundle = DataGenerator().generate(schema, seed=7, rows_per_table=100)
    assert bundle.for_role(NodeRole.BASELINE) is bundle.payload
    assert bundle.for_role(NodeRole.CUSTOM_OFF) is bundle.payload
    assert bundle.for_role(NodeRole.CUSTOM_ON) is bundle.payload


def test_regular_lob_is_capped(schema_with_longblob):
    bundle = DataGenerator(max_regular_lob_bytes=64 * 1024).generate(
        schema_with_longblob, seed=8, rows_per_table=20
    )
    assert max(len(value) for value in bundle.binary_values()) <= 64 * 1024
```

- [ ] **Step 2: Prove red**

Run: `uv run pytest -q tests/generation/test_data.py`

Expected: data generator imports fail.

- [ ] **Step 3: Implement distribution plans and canonical local-infile payloads**

```python
class DistributionKind(StrEnum):
    BOUNDARY = "boundary"
    UNIFORM = "uniform"
    ZIPF = "zipf"
    LOW_CARDINALITY = "low_cardinality"
    NULL_HEAVY = "null_heavy"
    UNIQUE = "unique"
    CORRELATED = "correlated"


@dataclass(frozen=True, slots=True)
class DataBundle:
    payload: Mapping[str, bytes]
    inserts_sql: tuple[str, ...]
    sha256_by_table: Mapping[str, str]
```

Generate explicit values for integers, decimal, float/double, BIT, temporal, text/binary, ENUM/SET, JSON and geometry. Represent decimal as text, binary as bytes, and temporal values without host-time dependence. Create FK parents before children.

- [ ] **Step 4: Run property tests for reproducibility, uniqueness, FK validity, and safe payload sizes**

Run: `uv run pytest -q tests/generation/test_data.py tests/property/test_data_generation.py`

Expected: identical seed yields identical bytes; every declared uniqueness/FK constraint is satisfied in the valid lane.

- [ ] **Step 5: Commit data generation**

```bash
git add src/select_fuzz/generation/data.py src/select_fuzz/generation/setup.py tests/generation/test_data.py tests/property/test_data_generation.py
git commit -m "feat: generate deterministic mixed-distribution data"
```

### Task 7: Type-aware SELECT AST, rendering, and read-only validator

**Files:**
- Create: `src/select_fuzz/generation/query_ast.py`
- Create: `src/select_fuzz/generation/query.py`
- Create: `src/select_fuzz/generation/render.py`
- Create: `src/select_fuzz/generation/safety.py`
- Test: `tests/generation/test_query.py`
- Test: `tests/property/test_query_safety.py`

- [ ] **Step 1: Write failing deterministic/tie-breaker/safety tests**

```python
def test_limit_requires_proven_unique_tiebreaker(catalog):
    query = QueryGenerator(catalog).generate(target="limit", seed=1)
    assert query.limit is not None
    assert query.order_by.proves_total_order(query.scope)


@pytest.mark.parametrize("sql", [
    "SELECT RAND()", "SELECT NOW()", "SELECT LOAD_FILE('/tmp/x')",
    "SELECT 1 FOR UPDATE", "SELECT 1 INTO @x", "SELECT 1; DROP TABLE t",
])
def test_validator_rejects_unsafe_sql(sql):
    with pytest.raises(UnsafeQuery):
        ReadOnlyValidator().validate_text(sql)
```

- [ ] **Step 2: Prove red**

Run: `uv run pytest -q tests/generation/test_query.py tests/property/test_query_safety.py`

Expected: query AST modules are missing.

- [ ] **Step 3: Implement scoped typed nodes and complexity accounting**

```python
@dataclass(frozen=True, slots=True)
class QueryBudget:
    max_tables: int = 4
    max_depth: int = 3
    max_ctes: int = 2
    max_set_branches: int = 3
    max_projection: int = 12
    max_predicates: int = 12


@dataclass(frozen=True, slots=True)
class SelectQuery:
    ctes: tuple[Cte, ...]
    projection: tuple[Expression, ...]
    source: Relation
    predicate: Expression | None
    grouping: Grouping | None
    windows: tuple[WindowSpec, ...]
    order_by: OrderBy
    limit: int | None
    feature_tags: frozenset[str]
```

Render identifiers/literals from typed nodes only. The free-random and negative lanes may relax grammar compatibility, but they must pass the same single-statement/read-only denylist before execution. Add directed generation by `FeatureSignature` for the research auditor.

- [ ] **Step 4: Execute snapshots and 10,000 read-only properties**

Run: `uv run pytest -q tests/generation/test_query.py tests/property/test_query_safety.py --hypothesis-show-statistics`

Expected: every valid query parses on MySQL fixtures, every limit/window ordering is deterministic, and no generated SQL contains a forbidden construct.

- [ ] **Step 5: Commit query generation**

```bash
git add src/select_fuzz/generation/query_ast.py src/select_fuzz/generation/query.py src/select_fuzz/generation/render.py src/select_fuzz/generation/safety.py tests/generation/test_query.py tests/property/test_query_safety.py
git commit -m "feat: generate safe type-aware MySQL select queries"
```

### Task 8: MySQL execution protocol and independent KILL watchdog

**Files:**
- Create: `src/select_fuzz/execution/__init__.py`
- Create: `src/select_fuzz/execution/protocols.py`
- Create: `src/select_fuzz/execution/mysql.py`
- Create: `src/select_fuzz/execution/timeout.py`
- Test: `tests/execution/test_mysql_runner.py`
- Test: `tests/execution/test_timeout.py`

- [ ] **Step 1: Write failing streaming-limit and race tests**

```python
def test_runner_stops_before_result_limits(fake_factory, node):
    runner = NodeQueryRunner(fake_factory)
    result = runner.run(node, "db", "SELECT x FROM t", timeout_s=15, row_limit=2, byte_limit=1024)
    assert result.status is ExecutionStatus.ERROR
    assert result.error.errno == INTERNAL_RESULT_LIMIT_ERRNO


def test_watchdog_cannot_kill_reused_connection(fake_factory, node):
    watchdog = KillQueryWatchdog(fake_factory)
    handle = watchdog.arm(node, "db", connection_id=41, timeout_s=0.01)
    handle.cancel(statement_token="old")
    fake_factory.mark_statement(node, 41, token="new")
    fake_factory.clock.advance(1)
    assert fake_factory.killed == []
```

- [ ] **Step 2: Prove red**

Run: `uv run pytest -q tests/execution`

Expected: execution modules are missing.

- [ ] **Step 3: Implement protocols and the frozen signatures**

```python
class QuerySession(Protocol):
    def connection_id(self) -> int: ...
    def execute(self, sql: str) -> CursorLike: ...
    def close(self) -> None: ...


class ConnectionFactory(Protocol):
    def query_session(self, node: NodeConfig, database: str) -> ContextManager[QuerySession]: ...
    def control_session(self, node: NodeConfig, database: str) -> ContextManager[QuerySession]: ...


class NodeQueryRunner:
    def run(self, node, database, sql, *, timeout_s, row_limit, byte_limit, barrier=None) -> NodeExecution:
        ...

    def run_session(self, session, node, database, sql, *, timeout_s, row_limit,
                    byte_limit, barrier=None) -> NodeExecution:
        """Execute on a caller-owned pinned session without closing or pooling it."""
        ...


class KillQueryWatchdog:
    def arm(self, node, database, connection_id, timeout_s) -> KillHandle:
        ...
```

Use a dedicated control connection and `KILL QUERY <id>`. Normalize errno 3024/1317 as timeout only when timeout state/watchdog evidence agrees; ordinary interruption remains an error. Wait for statement exit before connection reuse.
`run()` owns a short-lived/pooled query session; `run_session()` shares the same bounded-fetch and watchdog implementation but never closes or returns the caller-owned session. This second path is required for temporary tables, whose definition and data are connection-scoped.

- [ ] **Step 4: Run execution race tests repeatedly**

Run: `uv run pytest -q tests/execution --count=50`

Expected: no test kills a new statement, leaks a watchdog thread, or returns an untyped exception.

- [ ] **Step 5: Commit execution adapters**

```bash
git add src/select_fuzz/execution tests/execution
git commit -m "feat: execute bounded MySQL queries with safe kill watchdog"
```

### Task 9: Three-node setup and query coordinator

**Files:**
- Create: `src/select_fuzz/execution/triad.py`
- Create: `src/select_fuzz/execution/setup.py`
- Test: `tests/execution/test_triad.py`
- Test: `tests/integration/test_setup_mysql.py`

- [ ] **Step 1: Write failing setup/error/recovery tests**

```python
def test_same_setup_bundle_reaches_all_roles(coordinator, case_bundle):
    result = coordinator.prepare(case_bundle)
    assert result.status == "ready"
    assert {item.payload_sha256 for item in result.nodes} == {case_bundle.payload_sha256}


def test_partial_setup_error_is_finding(coordinator, case_bundle):
    coordinator.fake(NodeRole.CUSTOM_ON).fail_ddl(errno=1005)
    assert coordinator.prepare(case_bundle).status == "setup_mismatch"


def test_all_same_setup_error_is_rejected_sample(coordinator, case_bundle):
    coordinator.fail_all(errno=1071)
    assert coordinator.prepare(case_bundle).status == "rejected_generation"


def test_temporary_table_setup_and_query_share_each_pinned_session(coordinator, temporary_bundle):
    prepared = coordinator.prepare(temporary_bundle)
    results = coordinator.execute(prepared, "SELECT COUNT(*) FROM tmp_t", limits())
    assert all(result.scalar == temporary_bundle.expected_rows for result in results)
    assert coordinator.fake_sessions.used_for_setup == coordinator.fake_sessions.used_for_query


def test_lost_temporary_session_rebuilds_the_whole_round(coordinator, temporary_bundle):
    prepared = coordinator.prepare(temporary_bundle)
    prepared.sessions[NodeRole.CUSTOM_ON].disconnect()
    rebuilt = coordinator.ensure_live(prepared)
    assert rebuilt.generation == prepared.generation + 1
    assert all(session.saw_setup for session in rebuilt.sessions.values())
```

- [ ] **Step 2: Prove red**

Run: `uv run pytest -q tests/execution/test_triad.py`

Expected: triad coordinator is missing.

- [ ] **Step 3: Implement concurrent prepare, pinned temporary sessions, checksums, and infrastructure pause**

```python
class TriadCoordinator:
    def prepare(self, bundle: CaseManifest) -> PreparedRound:
        sessions = self.sessions.pin_all(self.nodes, bundle.database) if bundle.has_temporary_tables else None
        outcomes = run_concurrently(
            self.nodes,
            lambda node: self.setup_runner.apply(node, bundle, session=sessions and sessions[node.role]),
        )
        return PreparedRound.from_setup(bundle, sessions, outcomes)

    def execute(self, prepared: PreparedRound, sql: str, limits: QueryLimits) -> tuple[NodeExecution, ...]:
        prepared = self.ensure_live(prepared)
        def one(node):
            common = dict(timeout_s=limits.timeout_seconds, row_limit=limits.row_limit,
                          byte_limit=limits.byte_limit)
            if prepared.sessions is not None:
                return self.query_runner.run_session(
                    prepared.sessions[node.role], node, prepared.database, sql, **common)
            return self.query_runner.run(node, prepared.database, sql, **common)
        return run_concurrently(
            self.nodes,
            one,
        )
```

`PreparedRound` owns the optional three-session lease and is closed only when the round ends. Setup classification still returns `ready`, `setup_mismatch`, or `rejected_generation`; failed preparation closes all opened leases. Database names include mode/time/worker/round/seed and remain retained. A lost temporary-table session invalidates all three leases and rebuilds the entire round from the retained manifest before any query resumes. Any infrastructure error pauses related workers with exponential backoff until all three roles recover.

- [ ] **Step 4: Run fake-node tests and opt-in real MySQL setup smoke**

Run: `uv run pytest -q tests/execution/test_triad.py && uv run pytest -q -m mysql tests/integration/test_setup_mysql.py`

Expected: setup checksums match, partial errors produce findings, and retained databases exist after the test.

- [ ] **Step 5: Commit tri-node coordination**

```bash
git add src/select_fuzz/execution/triad.py src/select_fuzz/execution/setup.py tests/execution/test_triad.py tests/integration/test_setup_mysql.py
git commit -m "feat: coordinate identical three-node test cases"
```

### Task 10: Typed multiset, floating, error, and timeout oracle

**Files:**
- Create: `src/select_fuzz/oracle/__init__.py`
- Create: `src/select_fuzz/oracle/canonical.py`
- Create: `src/select_fuzz/oracle/errors.py`
- Create: `src/select_fuzz/oracle/compare.py`
- Test: `tests/oracle/test_compare.py`
- Test: `tests/property/test_float_multiset.py`

- [ ] **Step 1: Write failing duplicate, float, JSON, and error tests**

```python
def test_unordered_duplicates_are_preserved():
    assert compare_rows([(1,), (1,), (2,)], [(2,), (1,), (1,)]).equal
    assert not compare_rows([(1,), (1,), (2,)], [(2,), (1,)]).equal


def test_float_matching_is_one_to_one_not_rounding_hash():
    policy = FloatPolicy(abs_tol=0.1, rel_tol=0.0)
    assert compare_rows([(0.0,), (0.15,)], [(0.08,), (0.16,)], float_policy=policy).equal


def test_error_normalization_keeps_raw_message():
    result = compare_errors(
        ErrorInfo(1054, "42S22", "Unknown column x on host-a connection 12"),
        ErrorInfo(1054, "42S22", "Unknown column x on host-b connection 99"),
    )
    assert result.equal
    assert "host-a" in result.left_raw
```

- [ ] **Step 2: Prove red**

Run: `uv run pytest -q tests/oracle tests/property/test_float_multiset.py`

Expected: oracle modules are missing.

- [ ] **Step 3: Implement typed canonicalization and deterministic perfect matching**

```python
def canonical_exact(value: object) -> tuple[str, object]:
    if value is None:
        return ("null", None)
    if isinstance(value, bytes):
        return ("bytes", value)
    if isinstance(value, Decimal):
        return ("decimal", value.as_tuple())
    if isinstance(value, dict):
        return ("json_object", tuple(sorted((k, canonical_exact(v)) for k, v in value.items())))
    if isinstance(value, list):
        return ("json_array", tuple(canonical_exact(v) for v in value))
    return (type(value).__name__, value)


def float_multiset_equal(left: Sequence[tuple[float, ...]], right: Sequence[tuple[float, ...]], policy: FloatPolicy) -> bool:
    graph = [[vectors_close(a, b, policy) for b in right] for a in left]
    return has_deterministic_perfect_matching(graph)
```

Group rows by column metadata and all non-float fields, then perform stable one-to-one matching inside each group. Compare all three pairings; do not use majority voting. Classify all timeout as `OVER_BUDGET`, partial timeout/success-error mix/value/metadata difference as `RESULT_MISMATCH`, and infrastructure errors outside the oracle. Ignore warnings for verdicts.

- [ ] **Step 4: Run oracle unit/property tests with adversarial non-transitive tolerance examples**

Run: `uv run pytest -q tests/oracle tests/property/test_float_multiset.py --hypothesis-show-statistics`

Expected: result comparison is permutation invariant, multiplicity preserving, and deterministic.

- [ ] **Step 5: Commit the oracle**

```bash
git add src/select_fuzz/oracle tests/oracle tests/property/test_float_multiset.py
git commit -m "feat: compare typed three-node query outcomes"
```

### Task 11: Fsynced artifacts, readers, HTML report, and replay

**Files:**
- Create: `src/select_fuzz/artifacts/__init__.py`
- Create: `src/select_fuzz/artifacts/jsonl.py`
- Create: `src/select_fuzz/artifacts/bundle.py`
- Create: `src/select_fuzz/artifacts/reader.py`
- Create: `src/select_fuzz/artifacts/report.py`
- Create: `src/select_fuzz/replay.py`
- Test: `tests/artifacts/test_jsonl.py`
- Test: `tests/artifacts/test_bundle.py`
- Test: `tests/integration/test_replay.py`

- [ ] **Step 1: Write failing durability and pass/finding retention tests**

```python
def test_jsonl_fsyncs_before_publish(tmp_path, recording_fsync):
    writer = JsonlWriter(tmp_path / "events.jsonl", fsync=recording_fsync)
    writer.append({"type": "finding", "case_id": "c1"})
    assert recording_fsync.calls == 1
    assert read_jsonl(tmp_path / "events.jsonl") == [{"type": "finding", "case_id": "c1"}]


def test_pass_is_compact_and_finding_is_complete(tmp_path):
    writer = CaseBundleWriter(tmp_path)
    writer.write_pass(pass_record(rows=10, digest="a" * 64))
    writer.write_finding(finding_with_three_results())
    assert not list((tmp_path / "passes").rglob("*.result.gz"))
    assert len(list((tmp_path / "findings").rglob("*.result.gz"))) == 3
```

- [ ] **Step 2: Prove red**

Run: `uv run pytest -q tests/artifacts`

Expected: artifact modules are missing.

- [ ] **Step 3: Implement append/fsync and atomic directory publication**

```python
class JsonlWriter:
    def append(self, record: Mapping[str, object]) -> None:
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        with self.path.open("ab", buffering=0) as stream:
            stream.write(payload)
            stream.flush()
            self._fsync(stream.fileno())


class CaseBundleWriter:
    def write(self, case_id: str, manifest: CaseManifest, files: Mapping[str, bytes]) -> Path:
        temp = self.root / f".{case_id}.tmp"
        final = self.root / case_id
        write_bundle_files(temp, manifest, files)
        fsync_tree(temp)
        os.replace(temp, final)
        return final
```

Daily JSONL is authoritative. The finding bundle includes three full compressed results/errors/plans, first difference/statistics, fingerprints, database names, seeds, setup/query SQL, checksums and replay manifest. The reader ignores only a torn final JSONL line. HTML is generated from readers and can be rebuilt.

- [ ] **Step 4: Implement replay by case ID and manifest-path compatibility**

```python
class ReplayService:
    def replay(self, case_id: str) -> ReplayResult:
        finding = self.reader.get(case_id)
        new_database = self.names.replay_database(finding)
        executions = self.coordinator.replay(finding.manifest, new_database)
        return ReplayResult.from_comparison(finding, executions)
```

Run: `uv run pytest -q tests/artifacts tests/integration/test_replay.py`

Expected: torn-tail recovery works, no secret appears, and deterministic injected findings replay as `reproduced`.

- [ ] **Step 5: Commit artifacts and replay**

```bash
git add src/select_fuzz/artifacts src/select_fuzz/replay.py tests/artifacts tests/integration/test_replay.py
git commit -m "feat: persist and replay complete correctness findings"
```

### Task 12: Correctness service, mode registry, doctor, and CLI

**Files:**
- Create: `src/select_fuzz/service.py`
- Create: `src/select_fuzz/correctness.py`
- Create: `src/select_fuzz/doctor.py`
- Modify: `src/select_fuzz/cli.py`
- Modify: `src/select_fuzz/__main__.py`
- Test: `tests/service/test_correctness.py`
- Test: `tests/cli/test_cli.py`
- Test: `tests/integration/test_correctness_mysql.py`

- [ ] **Step 1: Write failing run lifecycle and CLI tests**

```python
def test_correctness_service_publishes_and_continues_after_finding(service, request):
    service.coordinator.inject_mismatch(query_index=0)
    summary = service.run(request, threading.Event())
    assert summary.findings == 1
    assert summary.queries_completed == request.queries_per_round
    assert service.events.types()[:2] == ["run_started", "round_started"]


def test_cli_dispatches_correctness_defaults(monkeypatch):
    called = capture_mode_runner(monkeypatch, "correctness")
    result = CliRunner().invoke(app, ["run", "--mode", "correctness", "--config", "config/example.yaml", "--rounds", "1"])
    assert result.exit_code == 0
    assert called.request.workers == 10
    assert called.request.queries_per_round == 1000
```

- [ ] **Step 2: Prove red**

Run: `uv run pytest -q tests/service/test_correctness.py tests/cli/test_cli.py`

Expected: service/doctor behavior is missing.

- [ ] **Step 3: Implement the shared ports, service loop, and mode registry**

```python
class EventSink(Protocol):
    def publish(self, event: RunEvent) -> None: ...


class FindingReader(Protocol):
    def iter_findings(self, cursor: str | None = None) -> Iterator[Finding]: ...
    def get(self, case_id: str) -> Finding: ...


class CorrectnessRunService:
    def run(self, request: RunRequest, stop_event: threading.Event) -> RunSummary:
        self.events.publish(RunEvent.started(request.run_id))
        return self.scheduler.run_workers(request, stop_event, self._run_round)


MODE_RUNNERS: dict[str, Callable[[RunRequest, threading.Event], RunSummary]] = {
    "correctness": build_correctness_runner,
}
```

`doctor` checks connectivity, `VERSION()`, role probe, configuration fingerprint and required capability canaries. Configuration differences become warning events; missing essential capability returns nonzero. `run` defaults to infinite and supports rounds, duration, seed, workers and queries-per-round. SIGINT/SIGTERM stops new work, waits the configured grace, then cancels active statements. `cleanup` acts only on explicit managed database IDs and never runs automatically.

- [ ] **Step 4: Run unit and local MySQL vertical slice**

Run:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q tests/unit tests/config tests/domain tests/generation tests/execution tests/oracle tests/artifacts tests/service tests/cli
uv run pytest -q -m mysql tests/integration/test_correctness_mysql.py
uv run python -m select_fuzz doctor --mode correctness --config config/local.yaml
uv run python -m select_fuzz run --mode correctness --config config/local.yaml --rounds 1 --queries-per-round 100
```

Expected: all checks pass; the local run retains one database per role, executes 100 queries, emits a parseable daily JSONL file, and contains no credential value.

- [ ] **Step 5: Commit the correctness vertical slice**

```bash
git add src/select_fuzz/service.py src/select_fuzz/correctness.py src/select_fuzz/doctor.py src/select_fuzz/cli.py src/select_fuzz/__main__.py tests/service tests/cli tests/integration/test_correctness_mysql.py
git commit -m "feat: deliver three-node correctness mode"
```

### Task 13: Core release gate and regression corpus

**Files:**
- Create: `tests/regression/seeds.json`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`

- [ ] **Step 1: Freeze controlled regression seeds for every schema/query/error family**

Run: `uv run python -m select_fuzz report --write-regression-seeds tests/regression/seeds.json --seed 20260712`

Expected: the file contains versioned seeds and expected feature tags, not SQL copied from the web and not credentials.

- [ ] **Step 2: Run the complete core gate**

Run:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q --cov=select_fuzz --cov-report=term-missing --cov-fail-under=90
git diff --check
```

Expected: all commands exit 0, at least 10,000 property examples are reported, and coverage is at least 90%.

- [ ] **Step 3: Document exact local usage without a password literal**

```markdown
export SELECT_FUZZ_MYSQL_USER='<local user>'
export SELECT_FUZZ_MYSQL_PASSWORD='<set in shell only>'
uv run select-fuzz doctor --mode correctness --config config/local.yaml
uv run select-fuzz run --mode correctness --config config/local.yaml --rounds 1
```

- [ ] **Step 4: Verify tracked files contain no local secret or runtime output**

Run: `git ls-files '.env*' 'artifacts/**' 'reports/**'`

Expected: no output.

Run: `git grep -n -E 'password[[:space:]]*[:=][[:space:]]*[^<]'`

Expected: exit 1 with no output; environment variable names and synthetic test canaries remain allowed.

- [ ] **Step 5: Commit the core release gate**

```bash
git add tests/regression/seeds.json .github/workflows/ci.yml README.md
git commit -m "test: gate correctness core with regression seeds"
```
