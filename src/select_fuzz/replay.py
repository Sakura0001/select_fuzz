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
from select_fuzz.config import NodeRole
from select_fuzz.domain import ExecutionStatus, NodeExecution
from select_fuzz.execution.setup import validate_database_name
from select_fuzz.execution.triad import (
    InfrastructureRetryPolicy,
    PrepareStatus,
    QueryLimits,
    TriadCoordinator,
    TriadExecutionResult,
)
from select_fuzz.oracle import OracleVerdict, compare_three_nodes


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

    @classmethod
    def from_finding(cls, finding: StoredFinding) -> ReplayCase:
        replay = finding.replay_manifest
        setup_sql = replay.get("setup_sql")
        query_sql = replay.get("query_sql")
        query_limits = replay.get("query_limits")
        payload_sha256 = replay.get("payload_sha256")
        seeds = replay.get("seeds")
        databases = replay.get("databases")
        requires_same_session = replay.get("requires_same_session")
        mode = finding.manifest.get("mode")
        original_verdict = finding.manifest.get("original_verdict")
        if mode != "correctness":
            raise ArtifactValidationError("replay currently requires correctness mode")
        if (
            not isinstance(setup_sql, list)
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
            role.value for role in NodeRole
        }:
            raise ArtifactValidationError("replay databases are invalid")
        typed_databases: dict[NodeRole, str] = {}
        for role in NodeRole:
            database = databases[role.value]
            if not isinstance(database, str):
                raise ArtifactValidationError("replay database must be a string")
            typed_databases[role] = validate_database_name(database)
        if not isinstance(requires_same_session, bool):
            raise ArtifactValidationError("replay session scope is invalid")
        if original_verdict not in {verdict.value for verdict in OracleVerdict}:
            raise ArtifactValidationError("original oracle verdict is invalid")
        assert isinstance(mode, str) and isinstance(original_verdict, str)
        return cls(
            case_id=finding.case_id,
            mode=mode,
            setup_sql=tuple(setup_sql),
            query_sql=query_sql,
            query_limits=typed_limits,
            payload_sha256=payload_sha256,
            seeds=MappingProxyType(dict(seeds)),
            original_databases=MappingProxyType(typed_databases),
            original_verdict=original_verdict,
            requires_same_session=requires_same_session,
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
        """SetupBundleLike compatibility for the production triad adapter."""

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


class TriadReplayAdapter:
    """Bridge a stored ReplayCase to the production three-node coordinator."""

    def __init__(
        self,
        triad: TriadCoordinator,
        *,
        retry: InfrastructureRetryPolicy = InfrastructureRetryPolicy(max_attempts=3),
    ) -> None:
        self._triad = triad
        self._retry = retry

    def replay(
        self, replay_case: ReplayCase, new_database: str
    ) -> ReplayCoordinatorResult:
        prepared = self._triad.prepare_until_recovered(
            replay_case,
            database=new_database,
            retry=self._retry,
        )
        if prepared.status is not PrepareStatus.READY:
            prepared.close()
            raise ReplayPreparationError(prepared.status)
        batch: TriadExecutionResult | None = None
        try:
            batch = self._triad.execute(
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
            status = (
                ReplayStatus.INFRASTRUCTURE_ERROR
                if error.status is PrepareStatus.INFRASTRUCTURE_PAUSE
                else ReplayStatus.PREPARATION_FAILED
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
        oracle_result = compare_three_nodes(executions)
        status = (
            ReplayStatus.REPRODUCED
            if oracle_result.verdict.value == replay_case.original_verdict
            else ReplayStatus.NOT_REPRODUCED
        )
        return ReplayResult(
            case_id=replay_case.case_id,
            database=database,
            status=status,
            original_verdict=replay_case.original_verdict,
            replay_verdict=oracle_result.verdict,
            executions=executions,
        )


__all__ = [
    "ReplayCase",
    "ReplayCoordinator",
    "ReplayCoordinatorResult",
    "ReplayDatabaseNames",
    "ReplayResult",
    "ReplayService",
    "ReplayStatus",
    "TriadReplayAdapter",
]
