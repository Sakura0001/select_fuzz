"""Replay stored correctness findings by case ID or manifest path."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from select_fuzz.artifacts.reader import (
    ArtifactReader,
    ArtifactValidationError,
    StoredFinding,
)
from select_fuzz.config import AppConfig, COMPARISON_ROLES, NodeRole
from select_fuzz.domain import ExecutionStatus, NodeExecution
from select_fuzz.execution.setup import validate_database_name
from select_fuzz.execution.triad import (
    DatabaseNameFactory,
    ComparisonCoordinator,
    ComparisonExecutionResult,
    InfrastructureRetryPolicy,
    PrepareStatus,
    QueryLimits,
)
from select_fuzz.execution import (
    MySQLConnectorFactory,
    MySQLSetupRunner,
    NodeQueryRunner,
)
from select_fuzz.generation.query_contract import ExpectedError, ExpectedErrorKind
from select_fuzz.oracle import (
    OracleVerdict,
    QueryErrorDisposition,
    analyze_query_errors,
    compare_two_nodes,
)


class ReplayStatus(StrEnum):
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    PREPARATION_FAILED = "preparation_failed"


@dataclass(frozen=True, slots=True)
class ReplayCase:
    case_id: str
    mode: str
    setup_sql: tuple[str, ...]
    query_sql: str
    query_limits: QueryLimits
    payload_sha256: str
    seeds: Mapping[str, int]
    original_databases: Mapping[NodeRole, str]
    original_verdict: str
    requires_same_session: bool
    expected_error: ExpectedError | None

    @classmethod
    def from_finding(cls, finding: StoredFinding) -> ReplayCase:
        if set(finding.results) == set(NodeRole):
            raise ArtifactValidationError("历史三节点产物不能使用两实例配置回放")
        if set(finding.results) != set(COMPARISON_ROLES):
            raise ArtifactValidationError("产物角色集合无效")
        replay = finding.replay_manifest
        setup_sql = finding.setup_sql
        query_sql = finding.query_sql
        query_limits = replay.get("query_limits")
        payload_sha256 = replay.get("payload_sha256")
        seeds = replay.get("seeds")
        databases = replay.get("databases")
        requires_same_session = replay.get("requires_same_session")
        mode = finding.manifest.get("mode")
        original_verdict = finding.manifest.get("original_verdict")
        first_difference = finding.manifest.get("first_difference")
        if mode != "correctness":
            raise ArtifactValidationError("replay currently requires correctness mode")
        if (
            not isinstance(setup_sql, tuple)
            or not setup_sql
            or any(not isinstance(sql, str) or not sql.strip() for sql in setup_sql)
        ):
            raise ArtifactValidationError("replay setup_sql is invalid")
        if not isinstance(query_sql, str) or not query_sql.strip():
            raise ArtifactValidationError("replay query_sql is invalid")
        if not isinstance(query_limits, dict):
            raise ArtifactValidationError("replay query_limits are invalid")
        try:
            typed_limits = QueryLimits(
                timeout_seconds=query_limits["timeout_seconds"],
                row_limit=query_limits["row_limit"],
                byte_limit=query_limits["byte_limit"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ArtifactValidationError("replay query_limits are invalid") from error
        if set(query_limits) != {"timeout_seconds", "row_limit", "byte_limit"}:
            raise ArtifactValidationError("replay query_limits contain unsupported keys")
        if (
            not isinstance(payload_sha256, str)
            or len(payload_sha256) != 64
            or any(character not in "0123456789abcdef" for character in payload_sha256)
        ):
            raise ArtifactValidationError("replay payload_sha256 is invalid")
        if not isinstance(seeds, dict) or not seeds or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, int)
            or isinstance(value, bool)
            for key, value in seeds.items()
        ):
            raise ArtifactValidationError("replay seeds are invalid")
        if not isinstance(databases, dict) or set(databases) != {
            role.value for role in COMPARISON_ROLES
        }:
            raise ArtifactValidationError("replay databases are invalid")
        typed_databases: dict[NodeRole, str] = {}
        for role in COMPARISON_ROLES:
            database = databases[role.value]
            if not isinstance(database, str):
                raise ArtifactValidationError("replay database must be a string")
            typed_databases[role] = validate_database_name(database)
        if not isinstance(requires_same_session, bool):
            raise ArtifactValidationError("replay session scope is invalid")
        accepted_verdicts = {
            *(verdict.value for verdict in OracleVerdict),
            PrepareStatus.SETUP_MISMATCH.value,
            QueryErrorDisposition.UNEXPECTED_VALID_ERROR.value,
            QueryErrorDisposition.EXPECTED_ERROR_MISMATCH.value,
        }
        if original_verdict not in accepted_verdicts:
            raise ArtifactValidationError("original oracle verdict is invalid")
        expected_error: ExpectedError | None = None
        if original_verdict in {
            QueryErrorDisposition.UNEXPECTED_VALID_ERROR.value,
            QueryErrorDisposition.EXPECTED_ERROR_MISMATCH.value,
        }:
            if not isinstance(first_difference, Mapping):
                raise ArtifactValidationError("generator finding details are invalid")
            if first_difference.get("category") != "generator_contract":
                raise ArtifactValidationError(
                    "generator finding category must be generator_contract"
                )
            expected_payload = first_difference.get("expected_error")
            if original_verdict == QueryErrorDisposition.EXPECTED_ERROR_MISMATCH.value:
                if not isinstance(expected_payload, Mapping):
                    raise ArtifactValidationError("expected error contract is missing")
                try:
                    expected_error = ExpectedError(
                        ExpectedErrorKind(expected_payload["kind"]),
                        expected_payload["errno"],
                        expected_payload["sqlstate"],
                    )
                except (KeyError, TypeError, ValueError) as error:
                    raise ArtifactValidationError(
                        "expected error contract is invalid"
                    ) from error
            elif expected_payload is not None:
                raise ArtifactValidationError(
                    "valid-query generator finding cannot expect an error"
                )
        assert isinstance(mode, str) and isinstance(original_verdict, str)
        return cls(
            case_id=finding.case_id,
            mode=mode,
            setup_sql=setup_sql,
            query_sql=query_sql,
            query_limits=typed_limits,
            payload_sha256=payload_sha256,
            seeds=MappingProxyType(dict(seeds)),
            original_databases=MappingProxyType(typed_databases),
            original_verdict=original_verdict,
            requires_same_session=requires_same_session,
            expected_error=expected_error,
        )

    @property
    def seed(self) -> int:
        for name in ("round", "query", "schema"):
            value = self.seeds.get(name)
            if value is not None:
                return value
        return next(iter(self.seeds.values()))

    @property
    def statements(self) -> tuple[str, ...]:
        """SetupBundleLike compatibility for the production pair adapter."""

        return self.setup_sql

@dataclass(frozen=True, slots=True)
class ReplayCoordinatorResult:
    executions: tuple[NodeExecution, ...]
    database: str


class ReplayPreparationError(RuntimeError):
    def __init__(self, status: PrepareStatus) -> None:
        self.status = status
        super().__init__(f"replay setup did not become ready: {status.value}")


class ReplayCoordinator(Protocol):
    def replay(
        self, replay_case: ReplayCase, new_database: str
    ) -> Sequence[NodeExecution] | ReplayCoordinatorResult: ...


class ComparisonReplayAdapter:
    """Bridge a stored ReplayCase to the production two-instance coordinator."""

    def __init__(
        self,
        comparison: ComparisonCoordinator,
        *,
        retry: InfrastructureRetryPolicy = InfrastructureRetryPolicy(max_attempts=3),
    ) -> None:
        self._comparison = comparison
        self._retry = retry

    def replay(
        self, replay_case: ReplayCase, new_database: str
    ) -> ReplayCoordinatorResult:
        prepared = self._comparison.prepare_until_recovered(
            replay_case,
            database=new_database,
            retry=self._retry,
        )
        if prepared.status is not PrepareStatus.READY:
            prepared.close()
            raise ReplayPreparationError(prepared.status)
        batch: ComparisonExecutionResult | None = None
        try:
            batch = self._comparison.execute(
                prepared,
                replay_case.query_sql,
                replay_case.query_limits,
            )
            return ReplayCoordinatorResult(
                executions=batch.executions,
                database=batch.prepared.database,
            )
        finally:
            if batch is not None:
                batch.prepared.close()
            prepared.close()


class ReplayDatabaseNames(Protocol):
    def new(
        self,
        *,
        mode: str,
        worker: int,
        round_number: int,
        seed: int,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class ReplayResult:
    case_id: str
    database: str
    status: ReplayStatus
    original_verdict: str
    replay_verdict: OracleVerdict | None
    executions: tuple[NodeExecution, ...]
    replay_classification: str | None = None


class ReplayService:
    def __init__(
        self,
        reader: ArtifactReader,
        coordinator: ReplayCoordinator,
        names: ReplayDatabaseNames,
    ) -> None:
        self._reader = reader
        self._coordinator = coordinator
        self._names = names

    def replay(self, reference: str | Path) -> ReplayResult:
        finding = self._reader.get_finding(reference)
        replay_case = ReplayCase.from_finding(finding)
        database = self._names.new(
            mode=replay_case.mode,
            worker=0,
            round_number=0,
            seed=replay_case.seed,
        )
        try:
            coordinator_result = self._coordinator.replay(replay_case, database)
        except ReplayPreparationError as error:
            reproduced_setup_mismatch = (
                replay_case.original_verdict
                == PrepareStatus.SETUP_MISMATCH.value
                and error.status is PrepareStatus.SETUP_MISMATCH
            )
            status = (
                ReplayStatus.REPRODUCED
                if reproduced_setup_mismatch
                else (
                    ReplayStatus.INFRASTRUCTURE_ERROR
                    if error.status is PrepareStatus.INFRASTRUCTURE_PAUSE
                    else ReplayStatus.PREPARATION_FAILED
                )
            )
            return ReplayResult(
                case_id=replay_case.case_id,
                database=database,
                status=status,
                original_verdict=replay_case.original_verdict,
                replay_verdict=None,
                executions=(),
            )
        if isinstance(coordinator_result, ReplayCoordinatorResult):
            database = coordinator_result.database
            executions = coordinator_result.executions
        else:
            executions = tuple(coordinator_result)
        if any(
            execution.status is ExecutionStatus.INFRA_ERROR
            for execution in executions
        ):
            return ReplayResult(
                case_id=replay_case.case_id,
                database=database,
                status=ReplayStatus.INFRASTRUCTURE_ERROR,
                original_verdict=replay_case.original_verdict,
                replay_verdict=None,
                executions=executions,
            )
        oracle_result = compare_two_nodes(executions)
        replay_verdict = oracle_result.verdict.value
        if oracle_result.verdict is OracleVerdict.MATCH:
            replay_verdict = analyze_query_errors(
                replay_case.expected_error,
                executions,
            ).disposition.value
        status = (
            ReplayStatus.REPRODUCED
            if replay_verdict == replay_case.original_verdict
            else ReplayStatus.NOT_REPRODUCED
        )
        return ReplayResult(
            case_id=replay_case.case_id,
            database=database,
            status=status,
            original_verdict=replay_case.original_verdict,
            replay_verdict=oracle_result.verdict,
            executions=executions,
            replay_classification=replay_verdict,
        )


def build_replay_service(config: AppConfig, artifact_root: Path) -> ReplayService:
    """Build the production two-instance replay path from a correctness config."""

    if config.mode.value != "correctness":
        raise ValueError("replay requires correctness config mode")
    comparison_factory = MySQLConnectorFactory()
    comparison = ComparisonCoordinator(
        config.comparison_nodes,
        setup_runner=MySQLSetupRunner(comparison_factory),
        query_runner=NodeQueryRunner(comparison_factory),
        session_factory=comparison_factory,
    )
    return ReplayService(
        ArtifactReader(artifact_root),
        ComparisonReplayAdapter(comparison),
        DatabaseNameFactory(),
    )


__all__ = [
    "ReplayCase",
    "ReplayCoordinator",
    "ReplayCoordinatorResult",
    "ReplayDatabaseNames",
    "ReplayResult",
    "ReplayService",
    "ReplayStatus",
    "ComparisonReplayAdapter",
    "build_replay_service",
]
