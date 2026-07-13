"""Production correctness round generation, execution, oracle, and persistence."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from threading import Event
import time
from typing import Protocol

from select_fuzz.artifacts import (
    CaseBundleWriter,
    FindingRecord,
    JsonlWriter,
    PassRecord,
    node_execution_to_artifact,
)
from select_fuzz.config import AppConfig, NodeRole
from select_fuzz.domain import (
    ExecutionStatus,
    NodeExecution,
    RunEvent,
    SeedTree,
    deterministic_id,
    stable_fingerprint,
)
from select_fuzz.execution import (
    DatabaseNameFactory,
    MySQLConnectorFactory,
    MySQLSetupRunner,
    NodeQueryRunner,
    PreparedRound,
    PrepareStatus,
    QueryLimits,
    SetupNodeResult,
    TriadCoordinator,
)
from select_fuzz.generation.coverage import CoverageLedger, CoverageScheduler
from select_fuzz.generation.query import QueryBatchPlanner, QueryGenerator, QueryMix
from select_fuzz.generation.schema import SchemaGenerator, SchemaLimits
from select_fuzz.generation.setup import SetupBundleBuilder
from select_fuzz.oracle import OracleVerdict, compare_three_nodes
from select_fuzz.service import (
    CorrectnessRunService,
    EventPublisher,
    RoundContext,
    RoundSummary,
)


@dataclass(frozen=True, slots=True)
class CorrectnessQuery:
    sql: str
    target_feature_id: str
    coverage_tags: frozenset[str]
    coverage_eligible: bool
    seed: int
    case_ordinal: int

    def __post_init__(self) -> None:
        if not isinstance(self.sql, str) or not self.sql.strip():
            raise ValueError("sql must not be empty")
        if not isinstance(self.target_feature_id, str) or not self.target_feature_id:
            raise ValueError("target_feature_id must not be empty")
        if not isinstance(self.coverage_eligible, bool):
            raise TypeError("coverage_eligible must be a bool")


class SetupBundleLike(Protocol):
    @property
    def statements(self) -> tuple[str, ...]: ...

    @property
    def payload_sha256(self) -> str: ...

    @property
    def requires_same_session(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class RoundMaterialization:
    database: str
    bundle: SetupBundleLike
    queries: tuple[CorrectnessQuery, ...]
    schema_seed: int
    data_seed: int
    rows_per_table: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "queries", tuple(self.queries))
        if not self.queries:
            raise ValueError("round materialization requires queries")
        if (
            not isinstance(self.rows_per_table, int)
            or isinstance(self.rows_per_table, bool)
            or self.rows_per_table < 0
        ):
            raise ValueError("rows_per_table must be a nonnegative integer")


class RoundSource(Protocol):
    def materialize(self, context: RoundContext) -> RoundMaterialization: ...


class PreparedLike(Protocol):
    @property
    def status(self) -> PrepareStatus: ...

    @property
    def database(self) -> str: ...

    @property
    def nodes(self) -> tuple[SetupNodeResult, ...]: ...

    def close(self) -> None: ...


class ExecutionBatchLike(Protocol):
    @property
    def prepared(self) -> PreparedLike: ...

    def __iter__(self) -> Iterator[NodeExecution]: ...


class CoordinatorLike(Protocol):
    def prepare_until_recovered(
        self,
        bundle: SetupBundleLike,
        *,
        database: str,
        should_stop: Callable[[], bool],
    ) -> PreparedLike: ...

    def execute(
        self, prepared: PreparedLike, sql: str, limits: QueryLimits
    ) -> ExecutionBatchLike: ...


class ProductionCoordinatorAdapter:
    """Widen the engine port while preserving the concrete triad type boundary."""

    def __init__(self, triad: TriadCoordinator) -> None:
        self._triad = triad

    def prepare_until_recovered(
        self,
        bundle: SetupBundleLike,
        *,
        database: str,
        should_stop: Callable[[], bool],
    ) -> PreparedRound:
        return self._triad.prepare_until_recovered(
            bundle,
            database=database,
            should_stop=should_stop,
        )

    def execute(
        self, prepared: PreparedLike, sql: str, limits: QueryLimits
    ) -> ExecutionBatchLike:
        if not isinstance(prepared, PreparedRound):
            raise TypeError("production coordinator requires PreparedRound")
        return self._triad.execute(prepared, sql, limits)


class CoverageLike(Protocol):
    def record(self, feature_id: str, hits: int = 1) -> None: ...

    def checkpoint(self) -> None: ...


class ArtifactWriterLike(Protocol):
    def write_pass(self, record: PassRecord) -> None: ...

    def write_finding(self, record: FindingRecord) -> Path: ...


class JsonlEventSink:
    def __init__(self, writer: JsonlWriter) -> None:
        self._writer = writer

    def publish(self, event: RunEvent) -> None:
        self._writer.append(
            {
                "payload": dict(event.payload),
                "run_id": event.run_id,
                "sequence": event.sequence,
                "type": event.kind,
            }
        )


class GeneratedRoundSource:
    """Build one immutable schema/data/query round from a deterministic context."""

    def __init__(
        self,
        ledger: CoverageLedger,
        *,
        rows_per_table: int | None = None,
        min_rows_per_table: int = 10,
        max_rows_per_table: int = 500,
        names: DatabaseNameFactory | None = None,
        schema_limits: SchemaLimits | None = None,
        query_generator: QueryGenerator | None = None,
    ) -> None:
        if rows_per_table is not None:
            if (
                not isinstance(rows_per_table, int)
                or isinstance(rows_per_table, bool)
                or rows_per_table <= 0
            ):
                raise ValueError("rows_per_table must be positive")
            min_rows_per_table = max_rows_per_table = rows_per_table
        for name, value in (
            ("min_rows_per_table", min_rows_per_table),
            ("max_rows_per_table", max_rows_per_table),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if min_rows_per_table > max_rows_per_table:
            raise ValueError(
                "min_rows_per_table must not exceed max_rows_per_table"
            )
        self._ledger = ledger
        self._min_rows_per_table = min_rows_per_table
        self._max_rows_per_table = max_rows_per_table
        self._names = names or DatabaseNameFactory()
        self._schema_limits = schema_limits or SchemaLimits()
        self._queries = query_generator or QueryGenerator()
        self._catalog = self._queries.feature_catalog()
        self._schema = SchemaGenerator()
        self._setup = SetupBundleBuilder()

    def materialize(self, context: RoundContext) -> RoundMaterialization:
        tree = SeedTree(context.round_seed)
        enabled = self._catalog.signature_targets(version=(8, 0, 41))
        if not enabled:
            raise RuntimeError("no evidence-verified query feature is enabled")
        schema_target = enabled[context.round_seed % len(enabled)]
        schema_seed = tree.derive("schema")
        data_seed = tree.derive("data")
        rows_per_table = self._min_rows_per_table + (
            tree.derive("rows_per_table")
            % (self._max_rows_per_table - self._min_rows_per_table + 1)
        )
        schema = self._schema.generate(
            schema_target,
            seed=schema_seed,
            limits=self._schema_limits,
        )
        bundle = self._setup.build(
            schema,
            seed=data_seed,
            rows_per_table=rows_per_table,
        )
        start_ordinal = context.round_number * context.request.queries_per_round
        scheduler = CoverageScheduler(
            catalog=self._catalog,
            ledger=self._ledger,
            min_hits=10,
            version=(8, 0, 41),
            profiles=frozenset({schema.profile.value}),
            schedule_seed=context.request.seed,
            plan_start_ordinal=start_ordinal,
        )
        generated = QueryBatchPlanner(self._queries).plan(
            schema,
            scheduler=scheduler,
            run_seed=context.request.seed,
            start_case_ordinal=start_ordinal,
            queries_per_round=context.request.queries_per_round,
            estimated_rows_by_table={
                table.name: rows_per_table for table in schema.tables
            },
        )
        queries = tuple(
            CorrectnessQuery(
                sql=query.sql,
                target_feature_id=query.target_feature_id,
                coverage_tags=query.feature_tags,
                coverage_eligible=query.coverage_eligible,
                seed=query.seed,
                case_ordinal=query.case_ordinal,
            )
            for query in generated
        )
        database = self._names.new(
            mode="correctness",
            worker=context.worker_id,
            round_number=context.round_number,
            seed=context.round_seed,
        )
        return RoundMaterialization(
            database,
            bundle,
            queries,
            schema_seed,
            data_seed,
            rows_per_table,
        )


def _json_digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _setup_result_to_artifact(result: SetupNodeResult) -> dict[str, object]:
    artifact: dict[str, object] = {
        "payload_sha256": result.payload_sha256,
        "role": result.role.value,
        "status": result.status.value,
    }
    if result.error is not None:
        artifact["error"] = asdict(result.error)
    return artifact


class CorrectnessRoundEngine:
    def __init__(
        self,
        source: RoundSource,
        coordinator: CoordinatorLike,
        artifacts: ArtifactWriterLike,
        coverage: CoverageLike,
        limits: QueryLimits,
        *,
        configuration_fingerprints: Mapping[NodeRole, str],
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if set(configuration_fingerprints) != set(NodeRole):
            raise ValueError("configuration_fingerprints require all three roles")
        self._source = source
        self._coordinator = coordinator
        self._artifacts = artifacts
        self._coverage = coverage
        self._limits = limits
        self._fingerprints = dict(configuration_fingerprints)
        self._sleeper = sleeper

    def run_round(
        self,
        context: RoundContext,
        events: EventPublisher,
        stop_event: Event,
    ) -> RoundSummary:
        materialized = self._source.materialize(context)
        prepared = self._coordinator.prepare_until_recovered(
            materialized.bundle,
            database=materialized.database,
            should_stop=stop_event.is_set,
        )
        if prepared.status is not PrepareStatus.READY:
            kind = prepared.status.value
            events.publish("setup_not_ready", {"database": prepared.database, "status": kind})
            try:
                if prepared.status is PrepareStatus.SETUP_MISMATCH:
                    query = materialized.queries[0]
                    case_id = deterministic_id(
                        "case",
                        context.request.run_id,
                        context.round_number,
                        "setup",
                    )
                    results = {
                        result.role: _setup_result_to_artifact(result)
                        for result in prepared.nodes
                    }
                    self._artifacts.write_finding(
                        FindingRecord(
                            case_id=case_id,
                            run_id=context.request.run_id,
                            mode="correctness",
                            databases={role: prepared.database for role in NodeRole},
                            seeds={
                                "data": materialized.data_seed,
                                "query": query.seed,
                                "round": context.round_seed,
                                "schema": materialized.schema_seed,
                            },
                            setup_sql=materialized.bundle.statements,
                            query_sql=query.sql,
                            query_limits={
                                "byte_limit": self._limits.byte_limit,
                                "row_limit": self._limits.row_limit,
                                "timeout_seconds": self._limits.timeout_seconds,
                            },
                            payload_sha256=materialized.bundle.payload_sha256,
                            original_verdict=PrepareStatus.SETUP_MISMATCH.value,
                            first_difference={
                                "category": "setup",
                                "status_by_role": {
                                    role.value: results[role]["status"]
                                    for role in NodeRole
                                },
                            },
                            statistics={
                                role.value: {
                                    "status": results[role]["status"],
                                }
                                for role in NodeRole
                            },
                            configuration_fingerprints=self._fingerprints,
                            results=results,
                            requires_same_session=(
                                materialized.bundle.requires_same_session
                            ),
                        )
                    )
            finally:
                prepared.close()
            return RoundSummary(
                context.round_number,
                0,
                1 if prepared.status is PrepareStatus.SETUP_MISMATCH else 0,
                1 if prepared.status is PrepareStatus.REJECTED_GENERATION else 0,
                0,
            )
        queries_completed = findings = over_budget = 0
        current_prepared = prepared
        try:
            for query in materialized.queries:
                if stop_event.is_set():
                    break
                delay = 0.25
                while True:
                    batch = self._coordinator.execute(current_prepared, query.sql, self._limits)
                    current_prepared = batch.prepared
                    executions = tuple(batch)
                    if not any(
                        execution.status is ExecutionStatus.INFRA_ERROR
                        for execution in executions
                    ):
                        break
                    events.publish(
                        "infrastructure_pause",
                        {"case_ordinal": query.case_ordinal, "database": current_prepared.database},
                    )
                    if stop_event.is_set():
                        break
                    self._sleeper(delay)
                    delay = min(30.0, delay * 2)
                if stop_event.is_set() or any(
                    execution.status is ExecutionStatus.INFRA_ERROR
                    for execution in executions
                ):
                    break
                oracle = compare_three_nodes(executions)
                case_id = deterministic_id(
                    "case", context.request.run_id, context.round_number, query.case_ordinal
                )
                encoded = {
                    execution.role: node_execution_to_artifact(execution)
                    for execution in executions
                }
                if oracle.verdict is OracleVerdict.RESULT_MISMATCH:
                    first = next(pair for pair in oracle.pairwise if not pair.matched)
                    self._artifacts.write_finding(
                        FindingRecord(
                            case_id=case_id,
                            run_id=context.request.run_id,
                            mode="correctness",
                            databases={role: current_prepared.database for role in NodeRole},
                            seeds={
                                "data": materialized.data_seed,
                                "query": query.seed,
                                "round": context.round_seed,
                                "schema": materialized.schema_seed,
                            },
                            setup_sql=materialized.bundle.statements,
                            query_sql=query.sql,
                            query_limits={
                                "byte_limit": self._limits.byte_limit,
                                "row_limit": self._limits.row_limit,
                                "timeout_seconds": self._limits.timeout_seconds,
                            },
                            payload_sha256=materialized.bundle.payload_sha256,
                            original_verdict=oracle.verdict.value,
                            first_difference=asdict(first),
                            statistics={
                                role.value: {
                                    "elapsed_ns": next(e for e in executions if e.role is role).elapsed_ns,
                                    "rows": len(next(e for e in executions if e.role is role).rows),
                                    "status": next(e for e in executions if e.role is role).status.value,
                                }
                                for role in NodeRole
                            },
                            configuration_fingerprints=self._fingerprints,
                            results=encoded,
                            requires_same_session=materialized.bundle.requires_same_session,
                        )
                    )
                    findings += 1
                elif oracle.verdict is OracleVerdict.OVER_BUDGET:
                    over_budget += 1
                else:
                    baseline = next(e for e in executions if e.role is NodeRole.BASELINE)
                    baseline_artifact = encoded[NodeRole.BASELINE]
                    self._artifacts.write_pass(
                        PassRecord(
                            case_id=case_id,
                            run_id=context.request.run_id,
                            database=current_prepared.database,
                            seed=query.seed,
                            query_sql=query.sql,
                            row_count=len(baseline.rows),
                            result_digest=_json_digest(baseline_artifact["rows"]),
                            column_metadata_digest=_json_digest(baseline_artifact["columns"]),
                            elapsed_ns_by_role={
                                execution.role: execution.elapsed_ns for execution in executions
                            },
                            coverage_tags=tuple(sorted(query.coverage_tags)),
                        )
                    )
                if query.coverage_eligible:
                    self._coverage.record(query.target_feature_id)
                queries_completed += 1
                events.publish(
                    "query_completed",
                    {
                        "case_id": case_id,
                        "case_ordinal": query.case_ordinal,
                        "verdict": oracle.verdict.value,
                    },
                )
        finally:
            current_prepared.close()
            if current_prepared is not prepared:
                prepared.close()
            self._coverage.checkpoint()
        return RoundSummary(
            context.round_number,
            queries_completed,
            findings,
            0,
            over_budget,
        )


def _fingerprints(config: AppConfig) -> dict[NodeRole, str]:
    return {
        node.role: stable_fingerprint(
            {
                "host": node.host,
                "port": node.port,
                "role": node.role.value,
                "role_probe_expected": node.role_probe_expected,
                "role_probe_sql": node.role_probe_sql,
            }
        )
        for node in config.nodes
    }


def query_mix_from_rates(
    free_random_rate: float,
    negative_mutation_rate: float,
) -> QueryMix:
    """Convert configured rates to an exact 100-ticket deterministic mix."""

    for name, value in (
        ("free_random_rate", free_random_rate),
        ("negative_mutation_rate", negative_mutation_rate),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError(f"{name} must be a number")
        if not 0 <= value <= 1:
            raise ValueError(f"{name} must be between zero and one")
    if free_random_rate + negative_mutation_rate > 1:
        raise ValueError("query lane rates must sum to at most one")
    free_percent = round(free_random_rate * 100)
    negative_percent = round(negative_mutation_rate * 100)
    valid_percent = 100 - free_percent - negative_percent
    if valid_percent < 0:
        negative_percent += valid_percent
        valid_percent = 0
    return QueryMix(valid_percent, free_percent, negative_percent)


def build_correctness_runner(config: AppConfig, artifact_root: Path) -> CorrectnessRunService:
    if config.mode.value != "correctness":
        raise ValueError("correctness runner requires correctness config mode")
    event_writer = JsonlWriter(artifact_root / "events.jsonl")
    artifacts = CaseBundleWriter(artifact_root, events=event_writer)
    ledger = CoverageLedger(artifact_root / "coverage.json")
    mix = query_mix_from_rates(
        config.correctness.free_random_rate,
        config.correctness.negative_mutation_rate,
    )
    source = GeneratedRoundSource(
        ledger,
        min_rows_per_table=config.correctness.min_rows_per_table,
        max_rows_per_table=config.correctness.max_rows_per_table,
        query_generator=QueryGenerator(mix=mix),
    )
    factory = MySQLConnectorFactory()
    coordinator = TriadCoordinator(
        config.nodes,
        setup_runner=MySQLSetupRunner(factory),
        query_runner=NodeQueryRunner(factory),
        session_factory=factory,
    )
    engine = CorrectnessRoundEngine(
        source,
        ProductionCoordinatorAdapter(coordinator),
        artifacts,
        ledger,
        QueryLimits(
            config.correctness.timeout_seconds,
            config.correctness.row_limit,
            config.correctness.byte_limit,
        ),
        configuration_fingerprints=_fingerprints(config),
    )
    return CorrectnessRunService(engine, JsonlEventSink(event_writer))


__all__ = [
    "CorrectnessQuery",
    "CorrectnessRoundEngine",
    "GeneratedRoundSource",
    "JsonlEventSink",
    "ProductionCoordinatorAdapter",
    "RoundMaterialization",
    "build_correctness_runner",
    "query_mix_from_rates",
]
