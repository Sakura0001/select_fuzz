from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event

from select_fuzz.artifacts import ArtifactReader, CaseBundleWriter
from select_fuzz.config import NodeRole
from select_fuzz.correctness import (
    CorrectnessQuery,
    CorrectnessRoundEngine,
    GeneratedRoundSource,
    RoundMaterialization,
    query_mix_from_rates,
)
from select_fuzz.domain import ColumnMeta, ErrorInfo, ExecutionStatus, NodeExecution, RunRequest
from select_fuzz.execution import (
    PrepareStatus,
    QueryLimits,
    SetupNodeResult,
    TriadExecutionResult,
)
from select_fuzz.generation.coverage import CoverageLedger
from select_fuzz.service import EventPublisher, RoundContext


class _Sink:
    def publish(self, event) -> None:  # type: ignore[no-untyped-def]
        return None


@dataclass(frozen=True)
class _Bundle:
    statements: tuple[str, ...] = (
        "CREATE TABLE `t0` (`id` BIGINT PRIMARY KEY);",
        "INSERT INTO `t0` VALUES (1),(2);",
    )
    payload_sha256: str = "a" * 64
    requires_same_session: bool = False


class _Prepared:
    status = PrepareStatus.READY
    generation = 0

    def __init__(self, database: str, bundle: _Bundle) -> None:
        self.database = database
        self.bundle = bundle
        self.closed = False
        self.nodes = tuple(
            SetupNodeResult(
                role=role,
                status=ExecutionStatus.SUCCESS,
                payload_sha256=bundle.payload_sha256,
            )
            for role in NodeRole
        )

    def close(self) -> None:
        self.closed = True


def _success(role: NodeRole, rows: tuple[tuple[object, ...], ...]) -> NodeExecution:
    return NodeExecution.success(
        role=role,
        connection_id=100 + list(NodeRole).index(role),
        started_ns=10,
        ended_ns=20,
        columns=(ColumnMeta("id", 8, False, False, False),),
        rows=rows,
    )


def _match() -> tuple[NodeExecution, ...]:
    return tuple(_success(role, ((1,), (2,))) for role in NodeRole)


def _mismatch() -> tuple[NodeExecution, ...]:
    values = list(_match())
    values[2] = _success(NodeRole.CUSTOM_ON, ((1,),))
    return tuple(values)


class _Coordinator:
    def __init__(self, outcomes: dict[str, tuple[NodeExecution, ...]]) -> None:
        self.outcomes = outcomes
        self.prepared: _Prepared | None = None
        self.executed: list[str] = []

    def prepare_until_recovered(
        self, bundle: _Bundle, *, database: str, should_stop, retry=None  # type: ignore[no-untyped-def]
    ) -> _Prepared:
        self.prepared = _Prepared(database, bundle)
        return self.prepared

    def execute(
        self, prepared: _Prepared, sql: str, limits: QueryLimits
    ) -> TriadExecutionResult:
        self.executed.append(sql)
        return TriadExecutionResult(prepared, self.outcomes[sql])  # type: ignore[arg-type]


class _Source:
    def __init__(self, materialized: RoundMaterialization) -> None:
        self.materialized = materialized

    def materialize(self, context: RoundContext) -> RoundMaterialization:
        return self.materialized


class _Coverage:
    def __init__(self) -> None:
        self.hits: list[str] = []
        self.checkpoints = 0

    def record(self, feature_id: str, hits: int = 1) -> None:
        self.hits.extend([feature_id] * hits)

    def checkpoint(self) -> None:
        self.checkpoints += 1


def _context(queries: int) -> RoundContext:
    request = RunRequest(
        run_id="run_engine_1",
        mode="correctness",
        seed=7,
        workers=1,
        rounds=1,
        queries_per_round=queries,
    )
    return RoundContext(request, worker_id=0, round_number=0, round_seed=11)


def _queries(count: int) -> tuple[CorrectnessQuery, ...]:
    return tuple(
        CorrectnessQuery(
            sql=f"SELECT {index} AS `id` ORDER BY 1",
            target_feature_id=f"feature_{index}",
            coverage_tags=frozenset({f"tag_{index}"}),
            coverage_eligible=True,
            seed=100 + index,
            case_ordinal=index,
        )
        for index in range(count)
    )


def test_round_engine_persists_finding_and_continues_remaining_queries(
    tmp_path: Path,
) -> None:
    queries = _queries(3)
    materialized = RoundMaterialization(
        database="sf_c_20260713t120000_w0_r0_sabc_n123_q0",
        bundle=_Bundle(),
        queries=queries,
        schema_seed=21,
        data_seed=22,
    )
    coordinator = _Coordinator(
        {queries[0].sql: _mismatch(), queries[1].sql: _match(), queries[2].sql: _match()}
    )
    coverage = _Coverage()
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        coordinator,
        CaseBundleWriter(tmp_path),
        coverage,
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={role: f"fp-{role.value}" for role in NodeRole},
    )

    summary = engine.run_round(
        _context(3), EventPublisher("run_engine_1", _Sink()), Event()
    )

    assert summary.queries_completed == 3
    assert summary.findings == 1
    assert coordinator.executed == [query.sql for query in queries]
    assert len(list((tmp_path / "findings").glob("*/manifest.json"))) == 1
    assert [event["type"] for event in ArtifactReader(tmp_path).events()].count("pass") == 2
    assert coverage.hits == [query.target_feature_id for query in queries]
    assert coverage.checkpoints == 1
    assert coordinator.prepared is not None and coordinator.prepared.closed is True


def test_round_engine_classifies_all_timeout_as_over_budget(tmp_path: Path) -> None:
    query = _queries(1)[0]
    timeouts = tuple(
        NodeExecution.failure(
            role=role,
            status=ExecutionStatus.TIMEOUT,
            started_ns=1,
            ended_ns=2,
            connection_id=100 + list(NodeRole).index(role),
            error=ErrorInfo(3024, "HY000", "maximum statement execution time exceeded"),
        )
        for role in NodeRole
    )
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator({query.sql: timeouts}),
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={role: f"fp-{role.value}" for role in NodeRole},
    )

    summary = engine.run_round(
        _context(1), EventPublisher("run_engine_1", _Sink()), Event()
    )

    assert summary.over_budget == 1
    assert summary.findings == 0


def test_generated_round_source_builds_real_schema_data_and_requested_query_count(
    tmp_path: Path,
) -> None:
    source = GeneratedRoundSource(
        CoverageLedger(tmp_path / "coverage.json"),
        rows_per_table=10,
    )

    materialized = source.materialize(_context(5))

    assert len(materialized.queries) == 5
    assert materialized.database.startswith("sf_c_")
    assert materialized.bundle.statements
    assert all(query.sql for query in materialized.queries)


def test_query_mix_rounding_always_totals_one_hundred() -> None:
    mix = query_mix_from_rates(0.333, 0.333)

    assert (
        mix.valid_percent + mix.free_random_percent + mix.negative_percent
    ) == 100
    assert mix.identity() == "34:33:33"


def test_generated_round_source_chooses_deterministic_rows_inside_range(
    tmp_path: Path,
) -> None:
    source = GeneratedRoundSource(
        CoverageLedger(tmp_path / "coverage.json"),
        min_rows_per_table=37,
        max_rows_per_table=419,
    )

    first = source.materialize(_context(2))
    second = source.materialize(_context(2))

    assert 37 <= first.rows_per_table <= 419
    assert first.rows_per_table == second.rows_per_table


def test_setup_mismatch_persists_complete_finding_bundle(tmp_path: Path) -> None:
    query = _queries(1)[0]
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    coordinator = _Coordinator({query.sql: _match()})
    prepared = _Prepared(materialized.database, materialized.bundle)
    prepared.status = PrepareStatus.SETUP_MISMATCH
    prepared.nodes = (
        SetupNodeResult(
            NodeRole.BASELINE,
            ExecutionStatus.SUCCESS,
            payload_sha256="a" * 64,
        ),
        SetupNodeResult(
            NodeRole.CUSTOM_OFF,
            ExecutionStatus.ERROR,
            error=ErrorInfo(1064, "42000", "syntax error"),
        ),
        SetupNodeResult(
            NodeRole.CUSTOM_ON,
            ExecutionStatus.ERROR,
            error=ErrorInfo(1054, "42S22", "unknown column"),
        ),
    )
    coordinator.prepared = prepared
    coordinator.prepare_until_recovered = (  # type: ignore[method-assign]
        lambda bundle, *, database, should_stop: prepared
    )
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        coordinator,
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={role: f"fp-{role.value}" for role in NodeRole},
    )

    summary = engine.run_round(
        _context(1), EventPublisher("run_engine_1", _Sink()), Event()
    )

    assert summary.findings == 1
    stored = ArtifactReader(tmp_path).get_finding(
        next((tmp_path / "findings").iterdir()).name
    )
    assert stored.manifest["original_verdict"] == "setup_mismatch"
    assert stored.manifest["setup_sql"] == list(materialized.bundle.statements)
    assert set(stored.results) == set(NodeRole)
