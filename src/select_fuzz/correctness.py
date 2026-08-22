"""Production correctness round generation, execution, oracle, and persistence."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
from threading import Event
import time
from typing import Any, Protocol, cast

from select_fuzz.artifacts import (
    CaseBundleWriter,
    FindingRecord,
    JsonlWriter,
    PassRecord,
    node_execution_to_artifact,
)
from select_fuzz.config import AppConfig, COMPARISON_ROLES, NodeRole
from select_fuzz.domain import (
    ExecutionStatus,
    NodeExecution,
    RunEvent,
    SeedTree,
    deterministic_id,
    stable_fingerprint,
)
from select_fuzz.execution import (
    BaselineExplainResult,
    ComparisonCoordinator,
    DatabaseNameFactory,
    MySQLConnectorFactory,
    MySQLSetupRunner,
    MutationBatchResult,
    MutationVerdict,
    NodeQueryRunner,
    PreparedRound,
    PrepareStatus,
    QuerySession,
    QueryLimits,
    PairMutationCoordinator,
    SetupNodeResult,
)
from select_fuzz.generation.catalog import FeatureCatalog, FeatureSpec
from select_fuzz.generation.catalog_schema import REVIEWED_VARIANT_IDS
from select_fuzz.generation.coverage import CoverageLedger
from select_fuzz.generation.mutation import MutationBatch, MutationBatchGenerator
from select_fuzz.generation.data import DataScenario
from select_fuzz.generation.query_contract import ExpectedError, QueryLane
from select_fuzz.generation.query_grammar import (
    CandidateRejected,
    GrammarQueryConfig,
    GrammarQueryGenerator,
    SelectGrammar,
)
from select_fuzz.generation.query_scope import DEFAULT_QUERY_SCOPE, QueryCoverageScope
from select_fuzz.generation.schema import (
    SchemaGenerator,
    SchemaLimits,
    SchemaManifest,
    SchemaProfile,
)
from select_fuzz.generation.setup import SetupBundleBuilder
from select_fuzz.oracle import (
    OracleVerdict,
    QueryErrorDisposition,
    analyze_query_errors,
    compare_two_nodes,
)
from select_fuzz.service import (
    CorrectnessRunService,
    EventPublisher,
    RoundContext,
    RoundSummary,
)


_PRODUCTION_SCHEMA_PROFILES = frozenset(
    {
        SchemaProfile.REGULAR_INNODB.value,
        SchemaProfile.PARTITIONED_INNODB.value,
        SchemaProfile.TEMPORARY_INNODB.value,
        SchemaProfile.FOREIGN_KEY_GRAPH.value,
        SchemaProfile.JSON_MULTIVALUE_INNODB.value,
    }
)


def _production_schema_targets(
    catalog: FeatureCatalog,
    *,
    replica_mode: bool,
) -> tuple[FeatureSpec, ...]:
    allowed_profiles = _PRODUCTION_SCHEMA_PROFILES
    if replica_mode:
        allowed_profiles -= {SchemaProfile.TEMPORARY_INNODB.value}
    targets: list[FeatureSpec] = []
    for target in catalog.signature_targets(version=(8, 0, 22), profiles=allowed_profiles):
        compatible_profiles = target.compatible_profiles & allowed_profiles
        if compatible_profiles:
            targets.append(replace(target, compatible_profiles=compatible_profiles))
    return tuple(targets)


@dataclass(frozen=True, slots=True)
class CorrectnessQuery:
    sql: str
    target_feature_id: str
    coverage_tags: frozenset[str]
    coverage_eligible: bool
    seed: int
    case_ordinal: int
    lane: QueryLane = QueryLane.VALID
    expected_error: ExpectedError | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.sql, str) or not self.sql.strip():
            raise ValueError("sql must not be empty")
        if not isinstance(self.target_feature_id, str) or not self.target_feature_id:
            raise ValueError("target_feature_id must not be empty")
        if not isinstance(self.coverage_eligible, bool):
            raise TypeError("coverage_eligible must be a bool")
        if not isinstance(self.lane, QueryLane):
            raise TypeError("lane must be a QueryLane")
        if self.expected_error is not None and not isinstance(self.expected_error, ExpectedError):
            raise TypeError("expected_error must be an ExpectedError or None")
        if (self.lane is QueryLane.NEGATIVE) != (self.expected_error is not None):
            raise ValueError("negative lane must have exactly one expected error contract")
        if self.lane is not QueryLane.VALID and self.coverage_eligible:
            raise ValueError("only valid-lane queries may be coverage eligible")


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
    schema: SchemaManifest | None = None
    dynamic_queries: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "queries", tuple(self.queries))
        if not self.queries and not self.dynamic_queries:
            raise ValueError("round materialization requires queries")
        if self.dynamic_queries and self.schema is None:
            raise ValueError("dynamic query rounds require an immutable schema snapshot")
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


class ExplainBatchLike(Protocol):
    @property
    def prepared(self) -> PreparedLike: ...

    @property
    def execution(self) -> NodeExecution: ...


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

    def explain_baseline(
        self, prepared: PreparedLike, sql: str, limits: QueryLimits
    ) -> ExplainBatchLike: ...


class MutationCoordinatorLike(Protocol):
    def execute_batch(
        self,
        database: str,
        batch: MutationBatch,
        *,
        sessions: Mapping[NodeRole, QuerySession] | None = None,
    ) -> MutationBatchResult: ...


class ProductionCoordinatorAdapter:
    """Widen the engine port while preserving the concrete pair type boundary."""

    def __init__(self, comparison: ComparisonCoordinator) -> None:
        self._comparison = comparison

    def prepare_until_recovered(
        self,
        bundle: SetupBundleLike,
        *,
        database: str,
        should_stop: Callable[[], bool],
    ) -> PreparedRound:
        return self._comparison.prepare_until_recovered(
            bundle,
            database=database,
            should_stop=should_stop,
        )

    def execute(self, prepared: PreparedLike, sql: str, limits: QueryLimits) -> ExecutionBatchLike:
        if not isinstance(prepared, PreparedRound):
            raise TypeError("production coordinator requires PreparedRound")
        return self._comparison.execute(prepared, sql, limits)

    def explain_baseline(
        self,
        prepared: PreparedLike,
        sql: str,
        limits: QueryLimits,
    ) -> BaselineExplainResult:
        if not isinstance(prepared, PreparedRound):
            raise TypeError("production coordinator requires PreparedRound")
        return self._comparison.explain_baseline(prepared, sql, limits)


class CoverageLike(Protocol):
    def record(self, feature_id: str, hits: int = 1) -> None: ...

    def checkpoint(self) -> None: ...


class ArtifactWriterLike(Protocol):
    def begin_round_sql(
        self,
        worker_id: int,
        *,
        database: str,
        setup_sql: tuple[str, ...],
        queries: tuple[str, ...],
        metadata: Mapping[str, object],
    ) -> Path: ...

    def append_round_sql(self, worker_id: int, database: str, sql: str) -> None: ...

    def append_round_dml_batch(
        self,
        worker_id: int,
        database: str,
        statements: tuple[str, ...],
    ) -> None: ...

    def begin_round_dml_batch(self, worker_id: int, database: str) -> None: ...

    def append_round_dml_sql(self, worker_id: int, database: str, sql: str) -> None: ...

    def end_round_dml_batch(self, worker_id: int, database: str) -> None: ...

    def append_thread_query_sql(
        self,
        worker_id: int,
        sql: str,
        *,
        metadata: Mapping[str, object],
    ) -> None: ...

    def write_query_record(
        self,
        worker_id: int,
        record: Mapping[str, object],
    ) -> None: ...

    def write_pass(self, record: PassRecord) -> None: ...

    def write_finding(self, record: FindingRecord) -> Path: ...


class JsonlEventSink:
    def __init__(
        self,
        writer: JsonlWriter,
        *,
        persist_query_events: bool = True,
    ) -> None:
        self._writer = writer
        self._persist_query_events = persist_query_events

    def publish(self, event: RunEvent) -> None:
        if event.kind == "query_completed" and not self._persist_query_events:
            return
        self._writer.append(
            {
                "payload": _json_event_value(event.payload),
                "run_id": event.run_id,
                "sequence": event.sequence,
                "type": event.kind,
            }
        )


def _json_event_value(value: object) -> object:
    """Thaw immutable RunEvent containers into strict JSON containers."""

    if isinstance(value, Mapping):
        return {key: _json_event_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_event_value(child) for child in value]
    if isinstance(value, (set, frozenset)):
        children = [_json_event_value(child) for child in value]
        return sorted(
            children,
            key=lambda child: json.dumps(
                child,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        )
    return value


_PRODUCTION_DATA_SCENARIOS = (
    DataScenario.SEEDED_RANDOM,
    DataScenario.BOUNDARY,
    DataScenario.ALL_NULL,
    DataScenario.MIXED_NULL,
    DataScenario.DUPLICATE,
    DataScenario.HOTSPOT,
)

_SCENARIO_WITNESS_MINIMUM = {
    DataScenario.SEEDED_RANDOM: 0,
    DataScenario.BOUNDARY: 8,
    DataScenario.ALL_NULL: 1,
    DataScenario.MIXED_NULL: 2,
    DataScenario.DUPLICATE: 2,
    DataScenario.HOTSPOT: 5,
    DataScenario.MIXED: 0,
}


class GeneratedRoundSource:
    """Build one immutable schema/data/query round from a deterministic context."""

    def __init__(
        self,
        *,
        rows_per_table: int | None = None,
        min_rows_per_table: int = 10,
        max_rows_per_table: int = 500,
        names: DatabaseNameFactory | None = None,
        schema_limits: SchemaLimits | None = None,
        grammar_query_generator: GrammarQueryGenerator | None = None,
        query_scope: QueryCoverageScope | None = None,
        replica_mode: bool = False,
    ) -> None:
        if rows_per_table is not None:
            if (
                not isinstance(rows_per_table, int)
                or isinstance(rows_per_table, bool)
                or rows_per_table < 0
            ):
                raise ValueError("rows_per_table must be nonnegative")
            min_rows_per_table = max_rows_per_table = rows_per_table
        for name, value in (
            ("min_rows_per_table", min_rows_per_table),
            ("max_rows_per_table", max_rows_per_table),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be nonnegative")
        if min_rows_per_table > max_rows_per_table:
            raise ValueError("min_rows_per_table must not exceed max_rows_per_table")
        self._fixed_rows_per_table = rows_per_table
        self._min_rows_per_table = min_rows_per_table
        self._max_rows_per_table = max_rows_per_table
        self._names = names or DatabaseNameFactory()
        self._schema_limits = schema_limits or SchemaLimits()
        self._grammar_queries = grammar_query_generator or GrammarQueryGenerator()
        self._query_scope = query_scope or DEFAULT_QUERY_SCOPE
        self._replica_mode = replica_mode
        self._catalog = self._query_scope.filter_catalog(
            FeatureCatalog.default(generator_supported_ids=REVIEWED_VARIANT_IDS)
        )
        self._schema = SchemaGenerator()
        self._typed_boundaries = (
            self._schema.executable_boundary_declarations(self._schema_limits)
            if self._schema_limits.max_columns >= 3
            else ()
        )
        self._setup = SetupBundleBuilder()

    def materialize(self, context: RoundContext) -> RoundMaterialization:
        tree = SeedTree(context.round_seed)
        enabled = _production_schema_targets(
            self._catalog,
            replica_mode=self._replica_mode,
        )
        if not enabled:
            raise RuntimeError("no evidence-verified query feature is enabled")
        data_seed = tree.derive("data")
        scenario = _PRODUCTION_DATA_SCENARIOS[
            tree.derive("data_scenario") % len(_PRODUCTION_DATA_SCENARIOS)
        ]
        boundary = None
        if scenario is DataScenario.BOUNDARY and self._typed_boundaries:
            boundary_index = tree.derive("typed_boundary") % len(self._typed_boundaries)
            boundary = self._typed_boundaries[boundary_index]
        schema_target = enabled[context.round_seed % len(enabled)]
        if boundary is not None:
            regular_targets = tuple(
                target
                for target in enabled
                if SchemaProfile.REGULAR_INNODB.value in target.compatible_profiles
            )
            if not regular_targets:
                raise RuntimeError("typed boundary coverage requires a regular target")
            schema_target = regular_targets[context.round_seed % len(regular_targets)]
        rows_per_table = self._rows_for_round(context.round_number, tree, scenario)
        for schema_attempt in range(32):
            schema_seed = (
                tree.derive("schema")
                if schema_attempt == 0
                else tree.derive("schema_retry", schema_attempt)
            )
            schema = self._schema.generate(
                schema_target,
                seed=schema_seed,
                limits=self._schema_limits,
                typed_boundary_id=(None if boundary is None else boundary.boundary_id),
            )
            if self._replica_mode and schema.requires_same_session:
                continue
            break
        else:
            raise RuntimeError("no replica-safe schema is reachable after 32 attempts")
        bundle = self._setup.build(
            schema,
            seed=data_seed,
            rows_per_table=rows_per_table,
            scenario=scenario,
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
            (),
            schema_seed,
            data_seed,
            rows_per_table,
            schema,
            True,
        )

    def generate_query(
        self,
        materialized: RoundMaterialization,
        context: RoundContext,
        candidate_ordinal: int,
    ) -> CorrectnessQuery:
        """Generate one stateless grammar candidate from the round schema snapshot."""

        if not materialized.dynamic_queries:
            raise RuntimeError("round source is not configured for dynamic grammar queries")
        if materialized.schema is None:  # pragma: no cover - dataclass invariant
            raise RuntimeError("dynamic round has no schema snapshot")
        if (
            not isinstance(candidate_ordinal, int)
            or isinstance(candidate_ordinal, bool)
            or candidate_ordinal < 0
        ):
            raise ValueError("candidate_ordinal must be a nonnegative integer")
        candidate_seed = SeedTree(context.round_seed).derive(
            "grammar_candidate",
            candidate_ordinal,
        )
        candidate = self._grammar_queries.generate(
            materialized.schema,
            seed=candidate_seed,
            excluded_families=self._query_scope.excluded_families,
        )
        grammar_production_tags = frozenset(
            f"grammar:{entry.partition('@')[0]}" for entry in candidate.production_trace
        )
        grammar_alternative_ids = tuple(
            self._grammar_queries.grammar.stable_alternative_id(entry)
            for entry in candidate.production_trace
        )
        grammar_alternative_tags = frozenset(
            f"grammar_alt:{identity}" for identity in grammar_alternative_ids
        )
        set_operator_ids = tuple(
            identity
            for identity in grammar_alternative_ids
            if identity.startswith("v1:set_operator:")
        )
        grammar_pair_tags = frozenset(
            f"grammar_pair:v1:set_operator:{left.rpartition(':')[2]}>{right.rpartition(':')[2]}"
            for left, right in zip(
                set_operator_ids,
                set_operator_ids[1:],
                strict=False,
            )
        )
        start_ordinal = context.round_number * context.request.queries_per_round
        return CorrectnessQuery(
            sql=candidate.sql,
            target_feature_id="grammar_random",
            coverage_tags=(
                grammar_production_tags
                | grammar_alternative_tags
                | grammar_pair_tags
                | {"grammar_random"}
            ),
            coverage_eligible=True,
            seed=candidate.seed,
            case_ordinal=start_ordinal + candidate_ordinal,
        )

    def _rows_for_round(
        self,
        round_number: int,
        tree: SeedTree,
        scenario: DataScenario,
    ) -> int:
        if self._fixed_rows_per_table is not None:
            return self._fixed_rows_per_table
        del round_number
        rows = self._min_rows_per_table + (
            tree.derive("rows_per_table")
            % (self._max_rows_per_table - self._min_rows_per_table + 1)
        )
        witness_minimum = _SCENARIO_WITNESS_MINIMUM[scenario]
        return max(rows, min(witness_minimum, self._max_rows_per_table))


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


def _expected_error_to_artifact(expected: ExpectedError | None) -> dict[str, object] | None:
    if expected is None:
        return None
    return {
        "errno": expected.expected_errno,
        "kind": expected.kind.value,
        "sqlstate": expected.expected_sqlstate,
    }


def _query_execution_to_log(execution: NodeExecution) -> dict[str, object]:
    columns = [
        {
            "binary": column.binary,
            "character_set_id": column.character_set_id,
            "column_length": column.column_length,
            "decimals": column.decimals,
            "flags": column.flags,
            "name": column.name,
            "nullable": column.nullable,
            "type_code": column.type_code,
            "unsigned": column.unsigned,
        }
        for column in execution.columns
    ]
    error = (
        None
        if execution.error is None
        else {
            "errno": execution.error.errno,
            "message": execution.error.message,
            "sqlstate": execution.error.sqlstate,
        }
    )
    return {
        "affected_rows": execution.affected_rows,
        "column_count": len(execution.columns),
        "column_metadata": columns,
        "column_metadata_digest": _json_digest(columns),
        "connection_id": execution.connection_id,
        "connection_reusable": execution.connection_reusable,
        "elapsed_ns": execution.elapsed_ns,
        "error": error,
        "row_count": len(execution.rows),
        "status": execution.status.value,
        "warnings": execution.warnings,
        "watchdog_error_type": execution.watchdog_error_type,
        "watchdog_fired": execution.watchdog_fired,
    }


def _execution_error_artifact(execution: NodeExecution) -> dict[str, object] | None:
    error = execution.error
    if error is None:
        return None
    return {
        "errno": error.errno,
        "message": error.message,
        "sqlstate": error.sqlstate,
    }


def _mutation_step_artifacts(
    result: MutationBatchResult,
) -> tuple[dict[str, object], ...]:
    steps: list[dict[str, object]] = []
    for sql, outcomes in zip(
        result.executed_sql,
        result.statement_results,
        strict=False,
    ):
        steps.append(
            {
                "sql": sql,
                "roles": {
                    role.value: {
                        "affected_rows": outcomes[role].affected_rows,
                        "error": _execution_error_artifact(outcomes[role]),
                        "status": outcomes[role].status.value,
                    }
                    for role in COMPARISON_ROLES
                },
            }
        )
    return tuple(steps)


def _has_uniform_runtime_error(executions: tuple[NodeExecution, ...]) -> bool:
    if len(executions) != len(COMPARISON_ROLES):
        return False
    if any(
        execution.status is not ExecutionStatus.ERROR or execution.error is None
        for execution in executions
    ):
        return False
    identities = {
        (execution.error.errno, execution.error.sqlstate)
        for execution in executions
        if execution.error is not None
    }
    return len(identities) == 1


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
        mutation_generator: MutationBatchGenerator | None = None,
        mutation_coordinator: MutationCoordinatorLike | None = None,
        replica_parameters_sha256: str | None = None,
        explain_timeout_seconds: float = 10.0,
    ) -> None:
        if set(configuration_fingerprints) != set(COMPARISON_ROLES):
            raise ValueError("configuration_fingerprints require custom_off and custom_on")
        self._source = source
        self._coordinator = coordinator
        self._artifacts = artifacts
        self._coverage = coverage
        self._limits = limits
        self._explain_limits = QueryLimits(
            explain_timeout_seconds,
            limits.row_limit,
            limits.byte_limit,
        )
        self._fingerprints = dict(configuration_fingerprints)
        self._sleeper = sleeper
        self._interruptible_backoff = sleeper is time.sleep
        if (mutation_generator is None) != (mutation_coordinator is None):
            raise ValueError("mutation generator and coordinator must be configured together")
        self._mutation_generator = mutation_generator
        self._mutation_coordinator = mutation_coordinator
        self._replica_parameters_sha256 = replica_parameters_sha256

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
        attempted_setup_sql = (
            tuple(getattr(prepared, "attempted_setup_sql"))
            if hasattr(prepared, "attempted_setup_sql")
            else materialized.bundle.statements
        )
        self._artifacts.begin_round_sql(
            context.worker_id,
            database=prepared.database,
            setup_sql=attempted_setup_sql,
            queries=(),
            metadata={
                "data_seed": materialized.data_seed,
                "round_number": context.round_number,
                "round_seed": context.round_seed,
                "run_id": context.request.run_id,
                "schema_seed": materialized.schema_seed,
                "worker_id": context.worker_id,
                "replica_parameters_sha256": self._replica_parameters_sha256,
            },
        )
        if prepared.status is not PrepareStatus.READY:
            kind = prepared.status.value
            events.publish(
                "setup_not_ready",
                {
                    "database": prepared.database,
                    "node_results": {
                        result.role.value: _setup_result_to_artifact(result)
                        for result in prepared.nodes
                    },
                    "status": kind,
                },
            )
            try:
                if prepared.status is PrepareStatus.SETUP_MISMATCH:
                    query = materialized.queries[0] if materialized.queries else None
                    case_id = deterministic_id(
                        "case",
                        context.request.run_id,
                        context.round_number,
                        "setup",
                    )
                    results = {
                        result.role: _setup_result_to_artifact(result) for result in prepared.nodes
                    }
                    failing_sql = getattr(prepared, "setup_failing_sql", None)
                    statement_records = tuple(
                        {
                            "sql": record.sql,
                            "roles": {
                                role.value: {
                                    "affected_rows": record.results[role].affected_rows,
                                    "error": (
                                        None
                                        if record.results[role].error is None
                                        else asdict(record.results[role].error)
                                    ),
                                    "status": record.results[role].status.value,
                                }
                                for role in COMPARISON_ROLES
                            },
                        }
                        for record in getattr(prepared, "setup_statement_records", ())
                    )
                    self._artifacts.write_finding(
                        FindingRecord(
                            case_id=case_id,
                            run_id=context.request.run_id,
                            mode="correctness",
                            databases={role: prepared.database for role in COMPARISON_ROLES},
                            seeds={
                                "data": materialized.data_seed,
                                "query": (context.round_seed if query is None else query.seed),
                                "round": context.round_seed,
                                "schema": materialized.schema_seed,
                            },
                            setup_sql=materialized.bundle.statements,
                            query_sql=(
                                failing_sql or (query.sql if query is not None else "SELECT 1")
                            ),
                            query_limits={
                                "byte_limit": self._limits.byte_limit,
                                "row_limit": self._limits.row_limit,
                                "timeout_seconds": self._limits.timeout_seconds,
                            },
                            payload_sha256=materialized.bundle.payload_sha256,
                            original_verdict=prepared.status.value,
                            first_difference={
                                "category": "setup",
                                "status_by_role": {
                                    role.value: results[role]["status"]
                                    for role in COMPARISON_ROLES
                                },
                                "failing_sql": failing_sql,
                                "statement_records": statement_records,
                            },
                            statistics={
                                role.value: {
                                    "status": results[role]["status"],
                                }
                                for role in COMPARISON_ROLES
                            },
                            configuration_fingerprints=self._fingerprints,
                            results=results,
                            requires_same_session=(materialized.bundle.requires_same_session),
                            replica_parameters_sha256=self._replica_parameters_sha256,
                            execution_sql=attempted_setup_sql,
                        )
                    )
            finally:
                prepared.close()
            return RoundSummary(
                context.round_number,
                0,
                (
                    1
                    if prepared.status is PrepareStatus.SETUP_MISMATCH
                    else 0
                ),
                1 if prepared.status is PrepareStatus.REJECTED_GENERATION else 0,
                0,
            )
        queries_completed = findings = rejected = over_budget = 0
        mutation_sequence = 0
        committed_mutation_sql: list[str] = []
        current_prepared = prepared
        dynamic_queries = materialized.dynamic_queries
        dynamic_generate: (
            Callable[[RoundMaterialization, RoundContext, int], CorrectnessQuery] | None
        ) = None
        if dynamic_queries:
            raw_generate = getattr(self._source, "generate_query", None)
            if not callable(raw_generate):
                raise RuntimeError("dynamic round source has no generate_query method")
            dynamic_generate = cast(
                Callable[[RoundMaterialization, RoundContext, int], CorrectnessQuery],
                raw_generate,
            )
        legacy_queries = iter(materialized.queries)
        candidate_ordinal = 0
        try:
            while True:
                if dynamic_queries:
                    if queries_completed >= context.request.queries_per_round:
                        break
                    if dynamic_generate is None:  # pragma: no cover - invariant above
                        raise RuntimeError("dynamic query generator is unavailable")
                    ordinal = candidate_ordinal
                    candidate_ordinal += 1
                    try:
                        query = dynamic_generate(materialized, context, ordinal)
                    except CandidateRejected:
                        rejected += 1
                        continue
                else:
                    try:
                        query = next(legacy_queries)
                    except StopIteration:
                        break
                if stop_event.is_set():
                    break
                if dynamic_queries:
                    explain_delay = 0.25
                    while True:
                        admission = self._coordinator.explain_baseline(
                            current_prepared,
                            query.sql,
                            self._explain_limits,
                        )
                        current_prepared = admission.prepared
                        explain_execution = admission.execution
                        if explain_execution.status is not ExecutionStatus.INFRA_ERROR:
                            break
                        events.publish(
                            "infrastructure_pause",
                            {
                                "database": current_prepared.database,
                                "stage": "baseline_explain",
                                "worker_id": context.worker_id,
                            },
                        )
                        if stop_event.is_set():
                            break
                        if self._interruptible_backoff:
                            if stop_event.wait(explain_delay):
                                break
                        else:
                            self._sleeper(explain_delay)
                            if stop_event.is_set():
                                break
                        explain_delay = min(30.0, explain_delay * 2)
                    if explain_execution.status is ExecutionStatus.INFRA_ERROR:
                        break
                    has_optimizer_hint_warning = "/*+" in query.sql and any(
                        "hint" in warning.casefold() for warning in explain_execution.warnings
                    )
                    if (
                        explain_execution.status is not ExecutionStatus.SUCCESS
                        or not explain_execution.rows
                        or has_optimizer_hint_warning
                    ):
                        rejected += 1
                        continue
                case_id = deterministic_id(
                    "case",
                    context.request.run_id,
                    context.round_number,
                    query.case_ordinal,
                )
                query_context: dict[str, object] = {
                    "case_id": case_id,
                    "case_ordinal": query.case_ordinal,
                    "coverage_eligible": query.coverage_eligible,
                    "coverage_tags": tuple(sorted(query.coverage_tags)),
                    "data_seed": materialized.data_seed,
                    "expected_error": _expected_error_to_artifact(query.expected_error),
                    "lane": query.lane.value,
                    "query_limits": {
                        "byte_limit": self._limits.byte_limit,
                        "row_limit": self._limits.row_limit,
                        "timeout_seconds": self._limits.timeout_seconds,
                    },
                    "query_seed": query.seed,
                    "query_sql": query.sql,
                    "requires_same_session": materialized.bundle.requires_same_session,
                    "round_number": context.round_number,
                    "round_seed": context.round_seed,
                    "run_id": context.request.run_id,
                    "schema_seed": materialized.schema_seed,
                    "schema_version": 1,
                    "setup_payload_sha256": materialized.bundle.payload_sha256,
                    "target_feature_id": query.target_feature_id,
                    "worker_id": context.worker_id,
                }
                delay = 0.25
                attempt_number = 0
                while True:
                    attempt_database = current_prepared.database
                    attempt_id = deterministic_id(
                        "attempt",
                        context.request.run_id,
                        context.round_number,
                        query.case_ordinal,
                        attempt_number,
                    )
                    attempt_context = {
                        **query_context,
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                        "database": attempt_database,
                    }
                    self._artifacts.write_query_record(
                        context.worker_id,
                        {
                            **attempt_context,
                            "type": "query_attempt_started",
                        },
                    )
                    self._artifacts.append_round_sql(
                        context.worker_id,
                        current_prepared.database,
                        query.sql,
                    )
                    self._artifacts.append_thread_query_sql(
                        context.worker_id,
                        query.sql,
                        metadata={
                            "attempt_id": attempt_id,
                            "attempt_number": attempt_number,
                            "case_ordinal": query.case_ordinal,
                            "database": attempt_database,
                            "query_seed": query.seed,
                            "round_number": context.round_number,
                            "run_id": context.request.run_id,
                            "worker_id": context.worker_id,
                        },
                    )
                    try:
                        batch = self._coordinator.execute(
                            current_prepared,
                            query.sql,
                            self._limits,
                        )
                    except Exception as error:
                        self._artifacts.write_query_record(
                            context.worker_id,
                            {
                                **attempt_context,
                                "exception": {
                                    "message": str(error),
                                    "type": type(error).__name__,
                                },
                                "type": "query_attempt_finished",
                                "verdict": "executor_exception",
                            },
                        )
                        raise
                    current_prepared = batch.prepared
                    executions = tuple(batch)
                    executions_by_role = {execution.role: execution for execution in executions}
                    nodes = {
                        role.value: _query_execution_to_log(executions_by_role[role])
                        for role in COMPARISON_ROLES
                    }
                    has_infra_error = any(
                        execution.status is ExecutionStatus.INFRA_ERROR for execution in executions
                    )
                    if not has_infra_error:
                        break
                    aborting = stop_event.is_set()
                    self._artifacts.write_query_record(
                        context.worker_id,
                        {
                            **attempt_context,
                            "nodes": nodes,
                            "result_database": current_prepared.database,
                            "type": "query_attempt_finished",
                            "verdict": (
                                "infrastructure_abort" if aborting else "infrastructure_retry"
                            ),
                        },
                    )
                    events.publish(
                        "infrastructure_pause",
                        {
                            "attempt_number": attempt_number,
                            "case_ordinal": query.case_ordinal,
                            "database": current_prepared.database,
                            "query_sql": query.sql,
                            "worker_id": context.worker_id,
                        },
                    )
                    if aborting:
                        break
                    if self._interruptible_backoff:
                        if stop_event.wait(delay):
                            break
                    else:
                        self._sleeper(delay)
                        if stop_event.is_set():
                            break
                    delay = min(30.0, delay * 2)
                    attempt_number += 1
                # A successful pair dispatch that already returned must still
                # be classified and receive its durable finished record.  The
                # stop flag prevents the next query at the top of the loop.
                if has_infra_error:
                    break
                if dynamic_queries and _has_uniform_runtime_error(executions):
                    rejected += 1
                    observed_error_identities = tuple(
                        {
                            "errno": execution.error.errno,
                            "sqlstate": execution.error.sqlstate,
                        }
                        for execution in executions
                        if execution.error is not None
                    )
                    self._artifacts.write_query_record(
                        context.worker_id,
                        {
                            **attempt_context,
                            "nodes": nodes,
                            "observed_error_identities": observed_error_identities,
                            "result_database": current_prepared.database,
                            "type": "query_attempt_finished",
                            "verdict": "uniform_runtime_error_rejected",
                        },
                    )
                    events.publish(
                        "query_rejected",
                        {
                            "case_id": case_id,
                            "case_ordinal": query.case_ordinal,
                            "database": current_prepared.database,
                            "observed_error_identities": observed_error_identities,
                            "query_seed": query.seed,
                            "query_sql": query.sql,
                            "reason": "uniform_runtime_error",
                            "round_number": context.round_number,
                            "worker_id": context.worker_id,
                        },
                    )
                    continue
                try:
                    oracle = compare_two_nodes(executions)
                    error_analysis = analyze_query_errors(query.expected_error, executions)
                    encoded = {
                        execution.role: node_execution_to_artifact(execution)
                        for execution in executions
                    }
                    finding_verdict: str | None = None
                    first_difference: Mapping[str, object] | None = None
                    pass_record: PassRecord | None = None
                    finding_record: FindingRecord | None = None
                    effective_verdict = oracle.verdict.value
                    if oracle.verdict is OracleVerdict.RESULT_MISMATCH:
                        first = next(pair for pair in oracle.pairwise if not pair.matched)
                        finding_verdict = oracle.verdict.value
                        first_difference = asdict(first)
                    elif oracle.verdict is OracleVerdict.OVER_BUDGET or (
                        error_analysis.disposition is QueryErrorDisposition.RESOURCE_LIMIT
                    ):
                        effective_verdict = QueryErrorDisposition.RESOURCE_LIMIT.value
                        over_budget += 1
                    elif error_analysis.disposition in {
                        QueryErrorDisposition.UNEXPECTED_VALID_ERROR,
                        QueryErrorDisposition.EXPECTED_ERROR_MISMATCH,
                        QueryErrorDisposition.DEFER_TO_ORACLE,
                    }:
                        finding_verdict = error_analysis.disposition.value
                        effective_verdict = error_analysis.disposition.value
                        first_difference = {
                            "category": "generator_contract",
                            "expected_error": _expected_error_to_artifact(query.expected_error),
                            "observed_identities": [
                                (
                                    None
                                    if identity is None
                                    else {
                                        "errno": identity[0],
                                        "sqlstate": identity[1],
                                    }
                                )
                                for identity in error_analysis.observed_identities
                            ],
                            "reason": error_analysis.reason,
                        }
                        rejected += 1
                    elif error_analysis.disposition is QueryErrorDisposition.SUCCESS:
                        baseline = executions_by_role[NodeRole.CUSTOM_OFF]
                        baseline_artifact = encoded[NodeRole.CUSTOM_OFF]
                        pass_record = PassRecord(
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
                        effective_verdict = QueryErrorDisposition.SUCCESS.value
                    elif error_analysis.disposition is QueryErrorDisposition.EXPECTED_ERROR:
                        effective_verdict = QueryErrorDisposition.EXPECTED_ERROR.value

                    if finding_verdict is not None:
                        if first_difference is None:  # pragma: no cover - local invariant
                            raise RuntimeError("finding requires first-difference details")
                        finding_record = FindingRecord(
                            case_id=case_id,
                            run_id=context.request.run_id,
                            mode="correctness",
                            databases={
                                role: current_prepared.database for role in COMPARISON_ROLES
                            },
                            seeds={
                                "data": materialized.data_seed,
                                "query": query.seed,
                                "round": context.round_seed,
                                "schema": materialized.schema_seed,
                            },
                            setup_sql=(
                                *materialized.bundle.statements,
                                *committed_mutation_sql,
                            ),
                            query_sql=query.sql,
                            query_limits={
                                "byte_limit": self._limits.byte_limit,
                                "row_limit": self._limits.row_limit,
                                "timeout_seconds": self._limits.timeout_seconds,
                            },
                            payload_sha256=materialized.bundle.payload_sha256,
                            original_verdict=finding_verdict,
                            first_difference=first_difference,
                            statistics={
                                role.value: {
                                    "elapsed_ns": next(
                                        e for e in executions if e.role is role
                                    ).elapsed_ns,
                                    "rows": len(next(e for e in executions if e.role is role).rows),
                                    "status": next(
                                        e for e in executions if e.role is role
                                    ).status.value,
                                }
                                for role in COMPARISON_ROLES
                            },
                            configuration_fingerprints=self._fingerprints,
                            results=encoded,
                            requires_same_session=materialized.bundle.requires_same_session,
                            replica_parameters_sha256=self._replica_parameters_sha256,
                        )
                except Exception as error:
                    self._artifacts.write_query_record(
                        context.worker_id,
                        {
                            **attempt_context,
                            "exception": {
                                "message": str(error),
                                "type": type(error).__name__,
                            },
                            "nodes": nodes,
                            "result_database": current_prepared.database,
                            "type": "query_attempt_finished",
                            "verdict": "classification_exception",
                        },
                    )
                    raise

                observed_identities = {
                    role.value: (
                        None
                        if identity is None
                        else {"errno": identity[0], "sqlstate": identity[1]}
                    )
                    for role, identity in zip(
                        COMPARISON_ROLES,
                        error_analysis.observed_identities,
                        strict=True,
                    )
                }
                self._artifacts.write_query_record(
                    context.worker_id,
                    {
                        **attempt_context,
                        "error_disposition": error_analysis.disposition.value,
                        "first_difference": first_difference,
                        "expected_finding_manifest": (
                            None if finding_record is None else f"findings/{case_id}/manifest.json"
                        ),
                        "nodes": nodes,
                        "observed_error_identities": observed_identities,
                        "oracle_verdict": oracle.verdict.value,
                        "reason": error_analysis.reason,
                        "result_database": current_prepared.database,
                        "type": "query_attempt_finished",
                        "verdict": effective_verdict,
                    },
                )

                if pass_record is not None:
                    self._artifacts.write_pass(pass_record)
                    if query.coverage_eligible and error_analysis.coverage_eligible:
                        self._coverage.record(query.target_feature_id)
                        for coverage_tag in sorted(query.coverage_tags - {query.target_feature_id}):
                            self._coverage.record(coverage_tag)
                if finding_record is not None:
                    self._artifacts.write_finding(finding_record)
                    findings += 1
                counted_this_query = not dynamic_queries or pass_record is not None
                if counted_this_query:
                    queries_completed += 1
                completion_payload: dict[str, object] = {
                    "case_id": case_id,
                    "case_ordinal": query.case_ordinal,
                    "database": current_prepared.database,
                    "lane": query.lane.value,
                    "query_seed": query.seed,
                    "query_sql": query.sql,
                    "round_number": context.round_number,
                    "target_feature_id": query.target_feature_id,
                    "verdict": effective_verdict,
                    "worker_id": context.worker_id,
                }
                if error_analysis.disposition is not QueryErrorDisposition.SUCCESS:
                    completion_payload.update(
                        {
                            "error_reason": error_analysis.reason,
                            "expected_error": _expected_error_to_artifact(query.expected_error),
                            "observed_error_identities": tuple(
                                (
                                    None
                                    if identity is None
                                    else {
                                        "errno": identity[0],
                                        "sqlstate": identity[1],
                                    }
                                )
                                for identity in error_analysis.observed_identities
                            ),
                        }
                    )
                events.publish(
                    (
                        "query_completed"
                        if counted_this_query or finding_record is not None
                        else "query_rejected"
                    ),
                    completion_payload,
                )
                if finding_record is not None:
                    break

                if (
                    self._mutation_generator is not None
                    and self._mutation_coordinator is not None
                    and counted_this_query
                    and queries_completed % 10 == 0
                ):
                    mutation_sequence += 1
                    mutation_seed = SeedTree(context.round_seed).derive(
                        "mutation", mutation_sequence
                    )
                    mutation_batch = self._mutation_generator.generate(
                        cast(Any, materialized.bundle),
                        seed=mutation_seed,
                        sequence=mutation_sequence,
                    )
                    logged_execute = getattr(
                        self._mutation_coordinator, "execute_batch_logged", None
                    )
                    prepared_sessions = getattr(current_prepared, "sessions", None)
                    session_arguments = (
                        {"sessions": prepared_sessions}
                        if isinstance(prepared_sessions, Mapping)
                        else {}
                    )
                    if callable(logged_execute):
                        self._artifacts.begin_round_dml_batch(
                            context.worker_id, current_prepared.database
                        )
                        try:
                            mutation_result = logged_execute(
                                current_prepared.database,
                                mutation_batch,
                                on_statement=lambda sql: self._artifacts.append_round_dml_sql(
                                    context.worker_id,
                                    current_prepared.database,
                                    sql,
                                ),
                                **session_arguments,
                            )
                        finally:
                            self._artifacts.end_round_dml_batch(
                                context.worker_id, current_prepared.database
                            )
                    else:
                        mutation_result = self._mutation_coordinator.execute_batch(
                            current_prepared.database,
                            mutation_batch,
                            **session_arguments,
                        )
                        self._artifacts.append_round_dml_batch(
                            context.worker_id,
                            current_prepared.database,
                            mutation_result.executed_sql,
                        )
                    events.publish(
                        "mutation_batch_completed",
                        {
                            "batch_seed": mutation_seed,
                            "database": current_prepared.database,
                            "sequence": mutation_sequence,
                            "statement_count": len(mutation_batch.statements),
                            "actual_affected_rows": mutation_result.actual_affected_rows,
                            "target_rows": mutation_batch.target_rows,
                            "verdict": mutation_result.verdict.value,
                            "retry_safety": mutation_result.retry_safety.value,
                            "worker_id": context.worker_id,
                        },
                    )
                    if mutation_result.verdict is MutationVerdict.COMMITTED:
                        committed_mutation_sql.extend(mutation_result.executed_sql)
                    if mutation_result.verdict is MutationVerdict.INFRASTRUCTURE_ERROR:
                        events.publish(
                            "mutation_infrastructure_error",
                            {
                                "database": current_prepared.database,
                                "failing_sql": mutation_result.failing_sql,
                                "retry_safety": mutation_result.retry_safety.value,
                                "sequence": mutation_sequence,
                                "worker_id": context.worker_id,
                            },
                        )
                        current_prepared.close()
                        break
                    if mutation_result.terminates_round:
                        mutation_case_id = deterministic_id(
                            "case",
                            context.request.run_id,
                            context.round_number,
                            "mutation",
                            mutation_sequence,
                        )
                        encoded_mutation = {
                            role: node_execution_to_artifact(mutation_result.final_results[role])
                            for role in COMPARISON_ROLES
                        }
                        mutation_difference: dict[str, object] = {
                            "category": "mutation",
                            "affected_rows_by_role": {
                                role.value: mutation_result.final_results[role].affected_rows
                                for role in COMPARISON_ROLES
                            },
                            "status_by_role": {
                                role.value: mutation_result.final_results[role].status.value
                                for role in COMPARISON_ROLES
                            },
                            "target_rows": mutation_batch.target_rows,
                            "transaction_steps": _mutation_step_artifacts(mutation_result),
                        }
                        self._artifacts.write_finding(
                            FindingRecord(
                                case_id=mutation_case_id,
                                run_id=context.request.run_id,
                                mode="correctness",
                                databases={
                                    role: current_prepared.database
                                    for role in COMPARISON_ROLES
                                },
                                seeds={
                                    "data": materialized.data_seed,
                                    "mutation": mutation_seed,
                                    "round": context.round_seed,
                                    "schema": materialized.schema_seed,
                                },
                                setup_sql=(
                                    *materialized.bundle.statements,
                                    *committed_mutation_sql,
                                ),
                                query_sql=(
                                    mutation_result.failing_sql or mutation_batch.statements[0].sql
                                ),
                                query_limits={
                                    "byte_limit": self._limits.byte_limit,
                                    "row_limit": self._limits.row_limit,
                                    "timeout_seconds": self._limits.timeout_seconds,
                                },
                                payload_sha256=materialized.bundle.payload_sha256,
                                original_verdict=mutation_result.verdict.value,
                                first_difference=mutation_difference,
                                statistics={
                                    role.value: {
                                        "affected_rows": mutation_result.final_results[
                                            role
                                        ].affected_rows,
                                        "status": mutation_result.final_results[role].status.value,
                                    }
                                    for role in COMPARISON_ROLES
                                },
                                configuration_fingerprints=self._fingerprints,
                                results=encoded_mutation,
                                requires_same_session=False,
                                replica_parameters_sha256=(self._replica_parameters_sha256),
                                execution_sql=mutation_result.executed_sql,
                            )
                        )
                        findings += 1
                        break
        finally:
            current_prepared.close()
            if current_prepared is not prepared:
                prepared.close()
            self._coverage.checkpoint()
        return RoundSummary(
            context.round_number,
            queries_completed,
            findings,
            rejected,
            over_budget,
        )


def _fingerprints(config: AppConfig) -> dict[NodeRole, str]:
    return {
        role: stable_fingerprint(
            {
                "endpoint": config.node_for(role).model_dump(mode="json"),
                "role": role.value,
            }
        )
        for role in COMPARISON_ROLES
    }


def build_correctness_runner(config: AppConfig, artifact_root: Path) -> CorrectnessRunService:
    if config.mode.value != "correctness":
        raise ValueError("correctness runner requires correctness config mode")
    event_writer = JsonlWriter(artifact_root / "events.jsonl")
    artifacts = CaseBundleWriter(
        artifact_root,
        events=event_writer,
        full_thread_sql_log=config.full_thread_sql_log,
        query_attempt_json_log=False,
        record_pass_events=False,
    )
    ledger = CoverageLedger(artifact_root / "coverage.json")
    grammar = (
        SelectGrammar.default()
        if config.correctness.query_grammar_path is None
        else SelectGrammar.from_path(config.correctness.query_grammar_path)
    )
    grammar_generator = GrammarQueryGenerator(
        grammar,
        config=GrammarQueryConfig(
            compatible_type_percent=(config.correctness.grammar_compatible_type_percent),
            max_tables_per_query_block=config.correctness.max_query_tables,
        ),
    )
    source = GeneratedRoundSource(
        min_rows_per_table=config.correctness.min_rows_per_table,
        max_rows_per_table=config.correctness.max_rows_per_table,
        schema_limits=SchemaLimits(
            min_tables=config.correctness.min_tables,
            max_tables=config.correctness.max_tables,
            min_columns=config.correctness.min_columns,
            max_columns=config.correctness.max_columns,
            max_indexes_per_table=config.correctness.max_indexes_per_table,
        ),
        grammar_query_generator=grammar_generator,
        replica_mode=False,
    )
    comparison_factory = MySQLConnectorFactory()
    coordinator = ComparisonCoordinator(
        config.comparison_nodes,
        setup_runner=MySQLSetupRunner(comparison_factory),
        query_runner=NodeQueryRunner(comparison_factory),
        session_factory=comparison_factory,
    )
    mutation_limits = QueryLimits(
        config.correctness.timeout_seconds,
        max(config.correctness.row_limit, 50),
        config.correctness.byte_limit,
    )
    mutation_coordinator = PairMutationCoordinator(
        config.comparison_nodes,
        factory=comparison_factory,
        runner=NodeQueryRunner(comparison_factory),
        limits=mutation_limits,
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
        mutation_generator=MutationBatchGenerator(),
        mutation_coordinator=mutation_coordinator,
        replica_parameters_sha256=None,
        explain_timeout_seconds=config.correctness.explain_timeout_seconds,
    )
    return CorrectnessRunService(
        engine,
        JsonlEventSink(event_writer, persist_query_events=False),
    )


__all__ = [
    "CorrectnessQuery",
    "CorrectnessRoundEngine",
    "GeneratedRoundSource",
    "JsonlEventSink",
    "ProductionCoordinatorAdapter",
    "RoundMaterialization",
    "build_correctness_runner",
]
