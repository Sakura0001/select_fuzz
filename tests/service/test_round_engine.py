from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import cast

import pytest

import select_fuzz.correctness as correctness_module
from select_fuzz.artifacts import ArtifactReader, CaseBundleWriter, StoredFinding, read_jsonl
from select_fuzz.config import COMPARISON_ROLES, NodeRole
from select_fuzz.correctness import (
    CorrectnessQuery,
    CorrectnessRoundEngine,
    GeneratedRoundSource,
    RoundMaterialization,
)
from select_fuzz.domain import (
    ColumnMeta,
    ErrorInfo,
    ExecutionStatus,
    NodeExecution,
    RunRequest,
    SeedTree,
)
from select_fuzz.execution import (
    INTERNAL_RESULT_LIMIT_ERRNO,
    MutationBatchResult,
    MutationVerdict,
    PrepareStatus,
    QueryLimits,
    SetupNodeResult,
    ComparisonExecutionResult,
)
from select_fuzz.generation.mutation import (
    MutationBatch,
    MutationOperation,
    MutationStatement,
)
from select_fuzz.generation.data import DataScenario
from select_fuzz.generation.catalog import FeatureCatalog, FeatureSpec
from select_fuzz.generation.query_contract import ExpectedError, ExpectedErrorKind, QueryLane
from select_fuzz.generation.schema import (
    BoundaryDeclarationId,
    IndexKind,
    SchemaGenerator,
    SchemaLimits,
    SchemaManifest,
    SchemaProfile,
)
from select_fuzz.service import EventPublisher, RoundContext


class _Sink:
    def publish(self, event) -> None:  # type: ignore[no-untyped-def]
        return None


def _only_stored_finding(root: Path) -> StoredFinding:
    manifests = tuple((root / "findings").glob("*/manifest.json"))
    assert len(manifests) == 1
    return ArtifactReader(root).get_finding(manifests[0])


def _boundary_seed(limits: SchemaLimits, boundary_ordinal: int) -> int:
    declarations = SchemaGenerator.executable_boundary_declarations(limits)
    for seed in range(1, 1_000_000):
        tree = SeedTree(seed)
        if (
            tree.derive("data_scenario") % 6 == 1
            and tree.derive("typed_boundary") % len(declarations) == boundary_ordinal
        ):
            return seed
    raise AssertionError("unable to find deterministic boundary seed")


class _CollectSink:
    def __init__(self) -> None:
        self.events = []

    def publish(self, event) -> None:  # type: ignore[no-untyped-def]
        self.events.append(event)


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
            for role in COMPARISON_ROLES
        )

    def close(self) -> None:
        self.closed = True


def _success(role: NodeRole, rows: tuple[tuple[object, ...], ...]) -> NodeExecution:
    return NodeExecution.success(
        role=role,
        connection_id=100 + list(COMPARISON_ROLES).index(role),
        started_ns=10,
        ended_ns=20,
        columns=(ColumnMeta("id", 8, False, False, False),),
        rows=rows,
    )


def _success_with_flags(role: NodeRole, flags: int) -> NodeExecution:
    return NodeExecution.success(
        role=role,
        connection_id=100 + list(COMPARISON_ROLES).index(role),
        started_ns=10,
        ended_ns=20,
        columns=(
            ColumnMeta(
                "id",
                8,
                False,
                True,
                False,
                character_set_id=63,
                column_length=20,
                decimals=0,
                flags=flags,
            ),
        ),
        rows=((1,),),
    )


def _match() -> tuple[NodeExecution, ...]:
    return tuple(_success(role, ((1,), (2,))) for role in COMPARISON_ROLES)


def _mismatch() -> tuple[NodeExecution, ...]:
    values = list(_match())
    values[1] = _success(NodeRole.CUSTOM_ON, ((1,),))
    return tuple(values)


def _errors(errno: int, sqlstate: str, message: str) -> tuple[NodeExecution, ...]:
    return tuple(
        NodeExecution.failure(
            role=role,
            status=ExecutionStatus.ERROR,
            started_ns=10,
            ended_ns=20,
            connection_id=100 + list(COMPARISON_ROLES).index(role),
            error=ErrorInfo(errno, sqlstate, message),
        )
        for role in COMPARISON_ROLES
    )


def _errors_with_messages(
    errno: int, sqlstate: str, messages: tuple[str, ...]
) -> tuple[NodeExecution, ...]:
    return tuple(
        NodeExecution.failure(
            role=role,
            status=ExecutionStatus.ERROR,
            started_ns=10,
            ended_ns=20,
            connection_id=100 + list(COMPARISON_ROLES).index(role),
            error=ErrorInfo(errno, sqlstate, message),
        )
        for role, message in zip(COMPARISON_ROLES, messages, strict=False)
    )


def _infra_errors() -> tuple[NodeExecution, ...]:
    return tuple(
        NodeExecution.failure(
            role=role,
            status=ExecutionStatus.INFRA_ERROR,
            started_ns=10,
            ended_ns=20,
            connection_id=100 + list(COMPARISON_ROLES).index(role),
            error=ErrorInfo(2006, "HY000", "server has gone away"),
            connection_reusable=False,
        )
        for role in COMPARISON_ROLES
    )


class _Coordinator:
    def __init__(self, outcomes: dict[str, tuple[NodeExecution, ...]]) -> None:
        self.outcomes = outcomes
        self.prepared: _Prepared | None = None
        self.executed: list[str] = []

    def prepare_until_recovered(
        self,
        bundle: _Bundle,
        *,
        database: str,
        should_stop,
        retry=None,  # type: ignore[no-untyped-def]
    ) -> _Prepared:
        self.prepared = _Prepared(database, bundle)
        return self.prepared

    def execute(
        self, prepared: _Prepared, sql: str, limits: QueryLimits
    ) -> ComparisonExecutionResult:
        self.executed.append(sql)
        return ComparisonExecutionResult(prepared, self.outcomes[sql])  # type: ignore[arg-type]


class _RetryCoordinator(_Coordinator):
    def __init__(self, outcomes: list[tuple[NodeExecution, ...]]) -> None:
        super().__init__({})
        self.sequential_outcomes = list(outcomes)

    def execute(
        self, prepared: _Prepared, sql: str, limits: QueryLimits
    ) -> ComparisonExecutionResult:
        self.executed.append(sql)
        return ComparisonExecutionResult(
            prepared, self.sequential_outcomes.pop(0)
        )  # type: ignore[arg-type]


class _RaisingCoordinator(_Coordinator):
    def __init__(self) -> None:
        super().__init__({})

    def execute(
        self, prepared: _Prepared, sql: str, limits: QueryLimits
    ) -> ComparisonExecutionResult:
        self.executed.append(sql)
        raise RuntimeError("simulated executor failure")


class _StopAfterExecutionCoordinator(_Coordinator):
    def __init__(self, outcome: tuple[NodeExecution, ...], stop_event: Event) -> None:
        super().__init__({})
        self.outcome = outcome
        self.stop_event = stop_event

    def execute(
        self, prepared: _Prepared, sql: str, limits: QueryLimits
    ) -> ComparisonExecutionResult:
        self.executed.append(sql)
        self.stop_event.set()
        return ComparisonExecutionResult(prepared, self.outcome)  # type: ignore[arg-type]


class _Source:
    def __init__(self, materialized: RoundMaterialization) -> None:
        self.materialized = materialized

    def materialize(self, context: RoundContext) -> RoundMaterialization:
        return self.materialized


@dataclass(frozen=True)
class _ExplainResult:
    prepared: _Prepared
    execution: NodeExecution


class _DynamicSource(_Source):
    def __init__(
        self,
        materialized: RoundMaterialization,
        candidates: tuple[CorrectnessQuery, ...],
    ) -> None:
        super().__init__(materialized)
        self.candidates = candidates
        self.generated_ordinals: list[int] = []

    def generate_query(
        self,
        materialized: RoundMaterialization,
        context: RoundContext,
        candidate_ordinal: int,
    ) -> CorrectnessQuery:
        self.generated_ordinals.append(candidate_ordinal)
        return self.candidates[candidate_ordinal]


class _ExplainCoordinator(_Coordinator):
    def __init__(
        self,
        outcomes: dict[str, tuple[NodeExecution, ...]],
        explain_outcomes: dict[str, NodeExecution],
    ) -> None:
        super().__init__(outcomes)
        self.explain_outcomes = explain_outcomes
        self.explained: list[tuple[str, QueryLimits]] = []

    def explain_baseline(
        self,
        prepared: _Prepared,
        sql: str,
        limits: QueryLimits,
    ) -> _ExplainResult:
        self.explained.append((sql, limits))
        return _ExplainResult(prepared, self.explain_outcomes[sql])


class _Coverage:
    def __init__(self) -> None:
        self.hits: list[str] = []
        self.checkpoints = 0

    def record(self, feature_id: str, hits: int = 1) -> None:
        self.hits.extend([feature_id] * hits)

    def checkpoint(self) -> None:
        self.checkpoints += 1


class _MutationGenerator:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def generate(self, setup, *, seed: int, sequence: int) -> MutationBatch:  # type: ignore[no-untyped-def]
        self.calls.append((seed, sequence))
        return MutationBatch(
            seed,
            sequence,
            (
                MutationStatement(
                    MutationOperation.UPDATE,
                    "UPDATE `t0` SET `id` = `id` + 100 LIMIT 12",
                    12,
                ),
            ),
        )


class _MutationCoordinator:
    def __init__(self, verdict: MutationVerdict) -> None:
        self.verdict = verdict
        self.calls: list[tuple[str, MutationBatch]] = []

    def execute_batch(self, database: str, batch: MutationBatch) -> MutationBatchResult:
        self.calls.append((database, batch))
        final = {
            role: NodeExecution.success(
                role=role,
                connection_id=100 + list(COMPARISON_ROLES).index(role),
                started_ns=10,
                ended_ns=20,
                affected_rows=12,
            )
            for role in COMPARISON_ROLES
        }
        transaction_end = "COMMIT" if self.verdict is MutationVerdict.COMMITTED else "ROLLBACK"
        return MutationBatchResult(
            self.verdict,
            batch,
            ("START TRANSACTION", batch.statements[0].sql, transaction_end),
            (final, final, final),
            final,
            batch.statements[0].sql if self.verdict is MutationVerdict.MISMATCH else None,
        )


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


def test_dynamic_grammar_round_explains_first_and_counts_only_successful_pairs(
    tmp_path: Path,
) -> None:
    raw_candidates = _queries(5)
    candidates = (
        raw_candidates[0],
        raw_candidates[1],
        replace(
            raw_candidates[2],
            sql="SELECT /*+ NO_ICP(`r1`) */ 2 AS `id` ORDER BY 1",
        ),
        raw_candidates[3],
        raw_candidates[4],
    )
    materialized = RoundMaterialization(
        database="sf_c_20260713t120000_w0_r0_sgrammar_n123_q0",
        bundle=_Bundle(),
        queries=(),
        schema_seed=21,
        data_seed=22,
        schema=cast(SchemaManifest, object()),
        dynamic_queries=True,
    )
    explain_error = next(
        execution
        for execution in _errors(1064, "42000", "syntax error")
        if execution.role is NodeRole.CUSTOM_OFF
    )
    explain_success = _success(NodeRole.CUSTOM_OFF, ((1,),))
    coordinator = _ExplainCoordinator(
        {
            candidates[1].sql: _errors(1366, "HY000", "uniform runtime error"),
            candidates[3].sql: _match(),
            candidates[4].sql: _match(),
        },
        {
            candidates[0].sql: explain_error,
            candidates[1].sql: explain_success,
            candidates[2].sql: replace(
                explain_success,
                warnings=("Warning 1064 Optimizer hint syntax error",),
            ),
            candidates[3].sql: explain_success,
            candidates[4].sql: explain_success,
        },
    )
    source = _DynamicSource(materialized, candidates)
    engine = CorrectnessRoundEngine(
        source,
        coordinator,
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )
    sink = _CollectSink()

    summary = engine.run_round(_context(2), EventPublisher("run_engine_1", sink), Event())

    assert summary.queries_completed == 2
    assert summary.findings == 0
    assert summary.rejected == 3
    assert source.generated_ordinals == [0, 1, 2, 3, 4]
    assert coordinator.executed == [
        candidates[1].sql,
        candidates[3].sql,
        candidates[4].sql,
    ]
    assert [sql for sql, _limits in coordinator.explained] == [
        candidate.sql for candidate in candidates
    ]
    assert {limits.timeout_seconds for _, limits in coordinator.explained} == {10.0}
    round_payload = (tmp_path / "rounds" / f"{materialized.database}.sql").read_text(
        encoding="utf-8"
    )
    assert candidates[0].sql not in round_payload
    assert candidates[1].sql in round_payload
    assert candidates[2].sql not in round_payload
    assert candidates[3].sql in round_payload
    assert candidates[4].sql in round_payload
    rejected_event = next(event for event in sink.events if event.kind == "query_rejected")
    assert rejected_event.payload["query_sql"] == candidates[1].sql
    assert rejected_event.payload["observed_error_identities"] == (
        {"errno": 1366, "sqlstate": "HY000"},
    ) * 2
    records = read_jsonl(tmp_path / "sql" / "worker-000.jsonl")
    uniform_record = next(
        record
        for record in records
        if record.get("verdict") == "uniform_runtime_error_rejected"
    )
    assert uniform_record["query_sql"] == candidates[1].sql
    assert uniform_record["observed_error_identities"] == [
        {"errno": 1366, "sqlstate": "HY000"},
    ] * 2


def test_generated_round_source_defaults_to_grammar_only_generation(
    tmp_path: Path,
) -> None:
    source = GeneratedRoundSource(
        rows_per_table=3,
        schema_limits=SchemaLimits(
            min_tables=1,
            max_tables=2,
            min_columns=2,
            max_columns=4,
            max_indexes_per_table=2,
        ),
    )
    context = _context(2)

    materialized = source.materialize(context)
    first = source.generate_query(materialized, context, 0)
    second = source.generate_query(materialized, context, 1)

    assert materialized.dynamic_queries
    assert materialized.queries == ()
    assert materialized.schema is not None
    assert first.target_feature_id == "grammar_random"
    assert first.coverage_eligible
    assert any(tag.startswith("grammar:") for tag in first.coverage_tags)
    assert any(tag.startswith("grammar_alt:") for tag in first.coverage_tags)
    assert first.seed != second.seed
    assert first.sql
    assert second.sql
    with pytest.raises(TypeError, match="query_generator"):
        GeneratedRoundSource(query_generator=object())  # type: ignore[call-arg]


def test_each_worker_triggers_one_transaction_after_ten_completed_queries(
    tmp_path: Path,
) -> None:
    queries = _queries(12)
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0",
        _Bundle(),
        queries,
        21,
        22,
    )
    generator = _MutationGenerator()
    mutation = _MutationCoordinator(MutationVerdict.CONSISTENT_ERROR_ROLLED_BACK)
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator({query.sql: _match() for query in queries}),
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
        mutation_generator=generator,  # type: ignore[arg-type]
        mutation_coordinator=mutation,
    )

    summary = engine.run_round(_context(12), EventPublisher("run_engine_1", _Sink()), Event())

    assert summary.queries_completed == 12
    assert summary.findings == 0
    assert len(generator.calls) == 1
    assert len(mutation.calls) == 1
    lines = (tmp_path / "rounds" / f"{materialized.database}.sql").read_text().splitlines()
    tenth = lines.index(queries[9].sql + ";")
    transaction = lines.index("START TRANSACTION;")
    eleventh = lines.index(queries[10].sql + ";")
    assert tenth < transaction < eleventh
    assert lines[transaction - 1] == ""
    assert lines[transaction + 3] == ""


def test_mutation_mismatch_stops_round_at_ten_queries_and_preserves_finding(
    tmp_path: Path,
) -> None:
    queries = _queries(12)
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0",
        _Bundle(),
        queries,
        21,
        22,
    )
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator({query.sql: _match() for query in queries}),
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
        mutation_generator=_MutationGenerator(),  # type: ignore[arg-type]
        mutation_coordinator=_MutationCoordinator(MutationVerdict.MISMATCH),
        replica_parameters_sha256="f" * 64,
    )

    summary = engine.run_round(_context(12), EventPublisher("run_engine_1", _Sink()), Event())

    assert summary.queries_completed == 10
    assert summary.findings == 1
    stored = _only_stored_finding(tmp_path)
    assert stored.manifest["original_verdict"] == MutationVerdict.MISMATCH.value
    assert stored.manifest["replica_parameters_sha256"] == "f" * 64
    assert stored.manifest["execution_sql"][-1] == "ROLLBACK"
    assert stored.manifest["first_difference"]["transaction_steps"]  # type: ignore[index]


def test_mutation_infrastructure_error_stops_round_without_false_finding(
    tmp_path: Path,
) -> None:
    queries = _queries(12)
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0",
        _Bundle(),
        queries,
        21,
        22,
    )
    sink = _CollectSink()
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator({query.sql: _match() for query in queries}),
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
        mutation_generator=_MutationGenerator(),  # type: ignore[arg-type]
        mutation_coordinator=_MutationCoordinator(MutationVerdict.INFRASTRUCTURE_ERROR),
    )

    summary = engine.run_round(
        _context(12), EventPublisher("run_engine_1", sink), Event()
    )

    assert summary.queries_completed == 10
    assert summary.findings == 0
    assert not tuple((tmp_path / "findings").glob("*/manifest.json"))
    assert any(event.kind == "mutation_infrastructure_error" for event in sink.events)


def test_round_engine_persists_finding_and_stops_current_database(
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
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    summary = engine.run_round(_context(3), EventPublisher("run_engine_1", _Sink()), Event())

    assert summary.queries_completed == 1
    assert summary.findings == 1
    assert coordinator.executed == [queries[0].sql]
    assert len(list((tmp_path / "findings").glob("*/manifest.json"))) == 1
    assert [event["type"] for event in ArtifactReader(tmp_path).events()].count("pass") == 0
    assert coverage.hits == []
    assert coverage.checkpoints == 1
    assert coordinator.prepared is not None and coordinator.prepared.closed is True
    query_records = read_jsonl(tmp_path / "sql" / "worker-000.jsonl")
    assert [record["type"] for record in query_records] == [
        "query_attempt_started",
        "query_attempt_finished",
    ]
    assert [
        record["verdict"] for record in query_records if record["type"] == "query_attempt_finished"
    ] == ["result_mismatch"]
    assert all(record["worker_id"] == 0 for record in query_records)
    assert all(record["query_sql"] in coordinator.executed for record in query_records)
    round_sql = tmp_path / "rounds" / f"{materialized.database}.sql"
    assert round_sql.exists()
    round_payload = round_sql.read_text(encoding="utf-8")
    assert f"CREATE DATABASE IF NOT EXISTS `{materialized.database}`;" in round_payload
    assert f"USE `{materialized.database}`;" in round_payload
    assert queries[0].sql in round_payload
    assert all(query.sql not in round_payload for query in queries[1:])
    finding_root = next(path for path in (tmp_path / "findings").iterdir() if path.is_dir())
    assert (finding_root / "case.sql").exists()
    assert (finding_root / "case.diff").exists()


def test_full_thread_sql_log_is_opt_in_append_only_and_sourceable(tmp_path: Path) -> None:
    queries = _queries(2)
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), queries, 1, 2
    )
    writer = CaseBundleWriter(
        tmp_path,
        full_thread_sql_log=True,
        query_attempt_json_log=False,
    )
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator({query.sql: _match() for query in queries}),
        writer,
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    engine.run_round(_context(2), EventPublisher("run_engine_1", _Sink()), Event())
    first = (tmp_path / "sql" / "worker-000.sql").read_text(encoding="utf-8")
    engine.run_round(_context(2), EventPublisher("run_engine_1", _Sink()), Event())
    second = (tmp_path / "sql" / "worker-000.sql").read_text(encoding="utf-8")

    assert len(second) == len(first) * 2
    assert second.startswith(first)
    assert second.count("CREATE TABLE `t0`") == 2
    assert second.count(queries[0].sql) == 2
    assert not (tmp_path / "sql" / "worker-000.jsonl").exists()


def test_round_engine_logs_every_infrastructure_retry_attempt(tmp_path: Path) -> None:
    query = _queries(1)[0]
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    coordinator = _RetryCoordinator([_infra_errors(), _match()])
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

    summary = engine.run_round(_context(1), EventPublisher("run_engine_1", _Sink()), Event())

    assert summary.queries_completed == 1
    assert coordinator.executed == [query.sql, query.sql]
    records = read_jsonl(tmp_path / "sql" / "worker-000.jsonl")
    assert [(record["type"], record.get("verdict")) for record in records] == [
        ("query_attempt_started", None),
        ("query_attempt_finished", "infrastructure_retry"),
        ("query_attempt_started", None),
        ("query_attempt_finished", "success"),
    ]
    assert [record["attempt_number"] for record in records] == [0, 0, 1, 1]
    assert records[1]["nodes"]["custom_off"]["error"] == {
        "errno": 2006,
        "message": "server has gone away",
        "sqlstate": "HY000",
    }


def test_stop_during_infrastructure_backoff_never_dispatches_an_extra_attempt(
    tmp_path: Path,
) -> None:
    query = _queries(1)[0]
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    stop_event = Event()
    coordinator = _RetryCoordinator([_infra_errors()])

    def stop_during_backoff(_: float) -> None:
        stop_event.set()

    engine = CorrectnessRoundEngine(
        _Source(materialized),
        coordinator,
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
        sleeper=stop_during_backoff,
    )

    summary = engine.run_round(
        _context(1),
        EventPublisher("run_engine_1", _Sink()),
        stop_event,
    )

    assert summary.queries_completed == 0
    assert coordinator.executed == [query.sql]
    records = read_jsonl(tmp_path / "sql" / "worker-000.jsonl")
    assert [(record["type"], record.get("verdict")) for record in records] == [
        ("query_attempt_started", None),
        ("query_attempt_finished", "infrastructure_retry"),
    ]


def test_infrastructure_result_with_concurrent_stop_is_logged_as_abort(
    tmp_path: Path,
) -> None:
    query = _queries(1)[0]
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    stop_event = Event()
    coordinator = _StopAfterExecutionCoordinator(_infra_errors(), stop_event)
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        coordinator,
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    summary = engine.run_round(
        _context(1),
        EventPublisher("run_engine_1", _Sink()),
        stop_event,
    )

    assert summary.queries_completed == 0
    assert coordinator.executed == [query.sql]
    records = read_jsonl(tmp_path / "sql" / "worker-000.jsonl")
    assert [(record["type"], record.get("verdict")) for record in records] == [
        ("query_attempt_started", None),
        ("query_attempt_finished", "infrastructure_abort"),
    ]


def test_classification_exception_is_logged_before_propagation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _queries(1)[0]
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator({query.sql: _match()}),
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    def fail_classification(_: object) -> object:
        raise RuntimeError("simulated classification failure")

    monkeypatch.setattr(
        "select_fuzz.correctness.compare_two_nodes",
        fail_classification,
    )

    with pytest.raises(RuntimeError, match="simulated classification failure"):
        engine.run_round(_context(1), EventPublisher("run_engine_1", _Sink()), Event())

    records = read_jsonl(tmp_path / "sql" / "worker-000.jsonl")
    assert [record["type"] for record in records] == [
        "query_attempt_started",
        "query_attempt_finished",
    ]
    assert records[-1]["verdict"] == "classification_exception"
    assert records[-1]["exception"] == {
        "message": "simulated classification failure",
        "type": "RuntimeError",
    }


def test_advisory_metadata_differences_remain_visible_in_worker_log(
    tmp_path: Path,
) -> None:
    query = _queries(1)[0]
    executions = (
        _success_with_flags(NodeRole.CUSTOM_OFF, 4129),
        _success_with_flags(NodeRole.CUSTOM_ON, 20515),
    )
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator({query.sql: executions}),
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    summary = engine.run_round(_context(1), EventPublisher("run_engine_1", _Sink()), Event())

    assert summary.findings == 0
    finished = read_jsonl(tmp_path / "sql" / "worker-000.jsonl")[-1]
    nodes = finished["nodes"]
    assert nodes["custom_off"]["column_metadata"][0]["flags"] == 4129
    assert nodes["custom_on"]["column_metadata"][0]["flags"] == 20515
    assert (
        nodes["custom_off"]["column_metadata_digest"]
        != nodes["custom_on"]["column_metadata_digest"]
    )


def test_round_engine_logs_executor_exception_after_started_record(tmp_path: Path) -> None:
    query = _queries(1)[0]
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    coordinator = _RaisingCoordinator()
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        coordinator,
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    with pytest.raises(RuntimeError, match="simulated executor failure"):
        engine.run_round(_context(1), EventPublisher("run_engine_1", _Sink()), Event())

    records = read_jsonl(tmp_path / "sql" / "worker-000.jsonl")
    assert [record["type"] for record in records] == [
        "query_attempt_started",
        "query_attempt_finished",
    ]
    assert records[1]["verdict"] == "executor_exception"
    assert records[1]["exception"] == {
        "message": "simulated executor failure",
        "type": "RuntimeError",
    }


def test_stop_after_dispatch_still_logs_and_classifies_returned_result(
    tmp_path: Path,
) -> None:
    queries = _queries(2)
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), queries, 1, 2
    )
    stop_event = Event()
    coordinator = _StopAfterExecutionCoordinator(_match(), stop_event)
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        coordinator,
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    summary = engine.run_round(
        _context(2),
        EventPublisher("run_engine_1", _Sink()),
        stop_event,
    )

    assert summary.queries_completed == 1
    assert coordinator.executed == [queries[0].sql]
    records = read_jsonl(tmp_path / "sql" / "worker-000.jsonl")
    assert [(record["type"], record.get("verdict")) for record in records] == [
        ("query_attempt_started", None),
        ("query_attempt_finished", "success"),
    ]


def test_round_engine_classifies_all_timeout_as_over_budget(tmp_path: Path) -> None:
    query = _queries(1)[0]
    timeouts = tuple(
        NodeExecution.failure(
            role=role,
            status=ExecutionStatus.TIMEOUT,
            started_ns=1,
            ended_ns=2,
            connection_id=100 + list(COMPARISON_ROLES).index(role),
            error=ErrorInfo(3024, "HY000", "maximum statement execution time exceeded"),
        )
        for role in COMPARISON_ROLES
    )
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    coverage = _Coverage()
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator({query.sql: timeouts}),
        CaseBundleWriter(tmp_path),
        coverage,
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    summary = engine.run_round(_context(1), EventPublisher("run_engine_1", _Sink()), Event())

    assert summary.over_budget == 1
    assert summary.findings == 0
    assert coverage.hits == []
    records = read_jsonl(tmp_path / "sql" / "worker-000.jsonl")
    assert records[-1]["verdict"] == "resource_limit"
    assert all(node["status"] == "timeout" for node in records[-1]["nodes"].values())


def test_same_error_from_valid_sql_is_a_generator_finding_not_coverage(
    tmp_path: Path,
) -> None:
    query = _queries(1)[0]
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    coverage = _Coverage()
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator({query.sql: _errors(1064, "42000", "syntax error")}),
        CaseBundleWriter(tmp_path),
        coverage,
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    summary = engine.run_round(_context(1), EventPublisher("run_engine_1", _Sink()), Event())

    assert summary.findings == 1
    assert summary.rejected == 1
    assert coverage.hits == []
    stored = _only_stored_finding(tmp_path)
    assert stored.manifest["original_verdict"] == "unexpected_valid_error"


def test_exact_expected_negative_error_is_not_a_generic_pass_or_coverage(
    tmp_path: Path,
) -> None:
    query = replace(
        _queries(1)[0],
        lane=QueryLane.NEGATIVE,
        expected_error=ExpectedError(ExpectedErrorKind.UNKNOWN_COLUMN, 1054, "42S22"),
        coverage_eligible=False,
    )
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    coverage = _Coverage()
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator({query.sql: _errors(1054, "42S22", "unknown column")}),
        CaseBundleWriter(tmp_path),
        coverage,
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    sink = _CollectSink()
    summary = engine.run_round(_context(1), EventPublisher("run_engine_1", sink), Event())

    assert summary.findings == 0
    assert summary.rejected == 0
    assert coverage.hits == []
    assert list((tmp_path / "findings").glob("*/manifest.json")) == []
    assert list(ArtifactReader(tmp_path).events()) == []
    completed = next(event for event in sink.events if event.kind == "query_completed")
    assert completed.payload["verdict"] == "expected_error"
    assert completed.payload["expected_error"] == {
        "errno": 1054,
        "kind": "unknown_column",
        "sqlstate": "42S22",
    }
    assert (
        completed.payload["observed_error_identities"]
        == ({"errno": 1054, "sqlstate": "42S22"},) * 2
    )
    records = read_jsonl(tmp_path / "sql" / "worker-000.jsonl")
    assert records[-1]["verdict"] == "expected_error"
    assert records[-1]["query_sql"] == query.sql
    assert records[-1]["expected_error"] == {
        "errno": 1054,
        "kind": "unknown_column",
        "sqlstate": "42S22",
    }
    assert all(
        node["error"]
        == {
            "errno": 1054,
            "message": "unknown column",
            "sqlstate": "42S22",
        }
        for node in records[-1]["nodes"].values()
    )


def test_wrong_error_for_negative_sql_is_a_generator_finding(tmp_path: Path) -> None:
    query = replace(
        _queries(1)[0],
        lane=QueryLane.NEGATIVE,
        expected_error=ExpectedError(ExpectedErrorKind.UNKNOWN_COLUMN, 1054, "42S22"),
        coverage_eligible=False,
    )
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator({query.sql: _errors(1064, "42000", "syntax error")}),
        CaseBundleWriter(tmp_path),
        _Coverage(),
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    summary = engine.run_round(_context(1), EventPublisher("run_engine_1", _Sink()), Event())

    assert summary.findings == 1
    assert summary.rejected == 1
    stored = _only_stored_finding(tmp_path)
    assert stored.manifest["original_verdict"] == "expected_error_mismatch"


def test_negative_query_that_succeeds_is_a_finding_not_a_pass(tmp_path: Path) -> None:
    query = replace(
        _queries(1)[0],
        lane=QueryLane.NEGATIVE,
        expected_error=ExpectedError(ExpectedErrorKind.UNKNOWN_COLUMN, 1054, "42S22"),
        coverage_eligible=False,
    )
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    coverage = _Coverage()
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator({query.sql: _match()}),
        CaseBundleWriter(tmp_path),
        coverage,
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    summary = engine.run_round(_context(1), EventPublisher("run_engine_1", _Sink()), Event())

    assert summary.findings == 1
    assert summary.rejected == 1
    assert coverage.hits == []
    assert [event["type"] for event in ArtifactReader(tmp_path).events()] == ["finding"]
    stored = _only_stored_finding(tmp_path)
    assert stored.manifest["original_verdict"] == "expected_error_mismatch"


def test_differential_error_mismatch_wins_over_expected_negative_identity(
    tmp_path: Path,
) -> None:
    query = replace(
        _queries(1)[0],
        lane=QueryLane.NEGATIVE,
        expected_error=ExpectedError(ExpectedErrorKind.UNKNOWN_COLUMN, 1054, "42S22"),
        coverage_eligible=False,
    )
    executions = _errors_with_messages(
        1054,
        "42S22",
        ("unknown column a", "unknown column b"),
    )
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    coverage = _Coverage()
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator({query.sql: executions}),
        CaseBundleWriter(tmp_path),
        coverage,
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    summary = engine.run_round(_context(1), EventPublisher("run_engine_1", _Sink()), Event())

    assert summary.findings == 1
    assert summary.rejected == 0
    assert coverage.hits == []
    stored = _only_stored_finding(tmp_path)
    assert stored.manifest["original_verdict"] == "result_mismatch"
    assert stored.manifest["first_difference"]["category"] == "error"


def test_internal_result_limit_is_resource_not_finding_pass_or_coverage(
    tmp_path: Path,
) -> None:
    query = _queries(1)[0]
    materialized = RoundMaterialization(
        "sf_c_20260713t120000_w0_r0_sabc_n123_q0", _Bundle(), (query,), 1, 2
    )
    coverage = _Coverage()
    engine = CorrectnessRoundEngine(
        _Source(materialized),
        _Coordinator(
            {
                query.sql: _errors(
                    INTERNAL_RESULT_LIMIT_ERRNO,
                    "HY000",
                    "result row limit exceeded",
                )
            }
        ),
        CaseBundleWriter(tmp_path),
        coverage,
        QueryLimits(15, 10_000, 32 << 20),
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    summary = engine.run_round(_context(1), EventPublisher("run_engine_1", _Sink()), Event())

    assert summary.over_budget == 1
    assert summary.findings == 0
    assert summary.rejected == 0
    assert coverage.hits == []
    assert ArtifactReader(tmp_path).events() == []


def test_correctness_query_rejects_an_untyped_expected_error_contract() -> None:
    with pytest.raises(TypeError, match="expected_error"):
        replace(
            _queries(1)[0],
            lane=QueryLane.NEGATIVE,
            expected_error=object(),  # type: ignore[arg-type]
            coverage_eligible=False,
        )


def test_generated_round_source_chooses_deterministic_rows_inside_range(
    tmp_path: Path,
) -> None:
    source = GeneratedRoundSource(
        min_rows_per_table=37,
        max_rows_per_table=419,
    )

    context = replace(_context(2), round_number=2, round_seed=13)
    first = source.materialize(context)
    second = source.materialize(context)

    assert 37 <= first.rows_per_table <= 419
    assert first.rows_per_table == second.rows_per_table


@pytest.mark.parametrize("rows_per_table", (0, 1, 10))
def test_generated_round_source_preserves_explicit_fixed_row_count(
    tmp_path: Path,
    rows_per_table: int,
) -> None:
    source = GeneratedRoundSource(
        rows_per_table=rows_per_table,
    )

    materialized = source.materialize(replace(_context(1), round_number=5, round_seed=105))

    assert materialized.rows_per_table == rows_per_table


def test_generated_round_source_seed_randomizes_data_scenarios_and_row_counts(
    tmp_path: Path,
) -> None:
    source = GeneratedRoundSource(
        min_rows_per_table=2,
        max_rows_per_table=5,
    )
    witness_minimum = {
        DataScenario.SEEDED_RANDOM: 0,
        DataScenario.BOUNDARY: 5,
        DataScenario.ALL_NULL: 1,
        DataScenario.MIXED_NULL: 2,
        DataScenario.DUPLICATE: 2,
        DataScenario.HOTSPOT: 5,
    }
    observed_scenarios: set[DataScenario] = set()
    observed_rows: set[int] = set()

    for round_number in range(10):
        materialized = source.materialize(
            replace(
                _context(1),
                round_number=round_number,
                round_seed=101 + round_number,
            )
        )
        scenario = materialized.bundle.data.scenario  # type: ignore[attr-defined]
        observed_scenarios.add(scenario)
        observed_rows.add(materialized.rows_per_table)
        assert 2 <= materialized.rows_per_table <= 5
        assert materialized.rows_per_table >= witness_minimum[scenario]

    assert observed_scenarios == {
        DataScenario.SEEDED_RANDOM,
        DataScenario.BOUNDARY,
        DataScenario.ALL_NULL,
        DataScenario.MIXED_NULL,
        DataScenario.DUPLICATE,
        DataScenario.HOTSPOT,
    }
    assert len(observed_rows) > 1


def test_production_schema_targets_remove_unsupported_engine_profiles() -> None:
    catalog = FeatureCatalog(
        (
            FeatureSpec(
                feature_id="profile_filter_target",
                family="schema",
                min_version=(8, 0, 0),
                compatible_profiles=frozenset(
                    {
                        SchemaProfile.REGULAR_INNODB.value,
                        SchemaProfile.TEMPORARY_INNODB.value,
                        SchemaProfile.FULLTEXT_INNODB.value,
                        SchemaProfile.SPATIAL_INNODB.value,
                    }
                ),
                ast_nodes=frozenset({"query_expression"}),
                guards=frozenset({"read_only_select"}),
            ),
        )
    )

    primary_targets = correctness_module._production_schema_targets(  # type: ignore[attr-defined]
        catalog,
        replica_mode=False,
    )
    replica_targets = correctness_module._production_schema_targets(  # type: ignore[attr-defined]
        catalog,
        replica_mode=True,
    )

    assert primary_targets[0].compatible_profiles == frozenset(
        {
            SchemaProfile.REGULAR_INNODB.value,
            SchemaProfile.TEMPORARY_INNODB.value,
        }
    )
    assert replica_targets[0].compatible_profiles == frozenset(
        {SchemaProfile.REGULAR_INNODB.value}
    )


def test_generated_round_source_never_emits_fulltext_or_spatial_indexes() -> None:
    source = GeneratedRoundSource(rows_per_table=0)

    for seed in range(1, 80):
        materialized = source.materialize(
            replace(_context(1), round_number=seed, round_seed=seed)
        )
        assert materialized.schema.profile not in {
            SchemaProfile.FULLTEXT_INNODB,
            SchemaProfile.SPATIAL_INNODB,
        }
        assert all(
            index.kind not in {IndexKind.FULLTEXT, IndexKind.SPATIAL}
            for table in materialized.schema.tables
            for index in table.indexes
        )


def test_production_boundary_rounds_reach_every_typed_declaration(
    tmp_path: Path,
) -> None:
    limits = SchemaLimits(
        min_tables=1,
        max_tables=1,
        min_columns=3,
        max_columns=3,
    )
    source = GeneratedRoundSource(
        rows_per_table=8,
        schema_limits=limits,
    )
    expected = SchemaGenerator.executable_boundary_declarations(limits)
    reached: dict[BoundaryDeclarationId, str] = {}

    for ordinal, boundary in enumerate(expected):
        round_number = ordinal
        materialized = source.materialize(
            replace(
                _context(1),
                round_number=round_number,
                round_seed=_boundary_seed(limits, ordinal),
            )
        )
        bundle = materialized.bundle
        declaration = bundle.schema.tables[0].column("boundary_col").mysql_type  # type: ignore[attr-defined]
        reached[boundary.boundary_id] = declaration

    assert reached == {boundary.boundary_id: boundary.declaration for boundary in expected}


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
            NodeRole.CUSTOM_OFF,
            ExecutionStatus.SUCCESS,
            payload_sha256="a" * 64,
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
        configuration_fingerprints={
            role: f"fp-{role.value}" for role in COMPARISON_ROLES
        },
    )

    sink = _CollectSink()
    summary = engine.run_round(_context(1), EventPublisher("run_engine_1", sink), Event())

    assert summary.findings == 1
    setup_event = next(event for event in sink.events if event.kind == "setup_not_ready")
    assert setup_event.payload["node_results"]["custom_on"]["error"] == {  # type: ignore[index]
        "errno": 1054,
        "message": "unknown column",
        "sqlstate": "42S22",
    }
    stored = _only_stored_finding(tmp_path)
    assert stored.manifest["original_verdict"] == "setup_mismatch"
    assert stored.manifest["setup_sql"] == list(materialized.bundle.statements)
    assert set(stored.results) == set(COMPARISON_ROLES)
