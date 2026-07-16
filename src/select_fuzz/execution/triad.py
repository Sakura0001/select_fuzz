"""Three-node setup and query coordination."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import math
from threading import Barrier, Lock
import time
from typing import Callable, Protocol, overload

from select_fuzz.config import MAX_STATEMENT_TIMEOUT_SECONDS, NodeConfig, NodeRole
from select_fuzz.domain import ErrorInfo, ExecutionStatus, NodeExecution
from select_fuzz.execution.protocols import BarrierLike, ConnectionFactory, QuerySession
from select_fuzz.execution.setup import (
    LockstepSetupResult,
    LockstepSetupVerdict,
    SetupBundleLike,
    SetupNodeResult,
    SetupStatementRecord,
    validate_database_name,
)
from select_fuzz.oracle.errors import normalize_error


class PrepareStatus(StrEnum):
    READY = "ready"
    SETUP_MISMATCH = "setup_mismatch"
    REJECTED_GENERATION = "rejected_generation"
    INFRASTRUCTURE_PAUSE = "infrastructure_pause"
    REPLICA_SYNC_TIMEOUT = "replica_sync_timeout"


@dataclass(frozen=True, slots=True)
class QueryLimits:
    timeout_seconds: float
    row_limit: int
    byte_limit: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > MAX_STATEMENT_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds must be finite, positive, and at most 300 seconds")
        for field_name in ("row_limit", "byte_limit"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class InfrastructureRetryPolicy:
    initial_delay_seconds: float = 0.25
    max_delay_seconds: float = 30.0
    multiplier: float = 2.0
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "initial_delay_seconds",
            "max_delay_seconds",
            "multiplier",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be a finite positive number")
        if self.initial_delay_seconds > self.max_delay_seconds:
            raise ValueError("initial_delay_seconds must not exceed max_delay_seconds")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least one")
        if self.max_attempts is not None and (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.max_attempts <= 0
        ):
            raise ValueError("max_attempts must be positive when supplied")


class DatabaseNameFactory:
    """Create safe retained-database names carrying the round reproduction identity."""

    def __init__(self, *, clock_ns: Callable[[], int] = time.time_ns) -> None:
        self._clock_ns = clock_ns
        self._lock = Lock()
        self._sequence = 0

    def new(
        self,
        *,
        mode: str,
        worker: int,
        round_number: int,
        seed: int,
    ) -> str:
        mode_code = {"correctness": "c", "performance": "p"}.get(mode)
        if mode_code is None:
            raise ValueError("mode must be correctness or performance")
        for field_name, value in (
            ("worker", worker),
            ("round_number", round_number),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} must be a nonnegative integer")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ValueError("seed must be an integer")
        now_ns = self._clock_ns()
        if not isinstance(now_ns, int) or isinstance(now_ns, bool) or now_ns < 0:
            raise ValueError("clock_ns must return a nonnegative integer")
        timestamp = datetime.fromtimestamp(now_ns / 1_000_000_000, tz=timezone.utc).strftime(
            "%Y%m%dt%H%M%S"
        )
        seed_id = sha256(str(seed).encode("ascii")).hexdigest()[:10]
        time_id = sha256(str(now_ns).encode("ascii")).hexdigest()[:8]
        with self._lock:
            sequence = self._sequence
            self._sequence += 1
        database = (
            f"sf_{mode_code}_{timestamp}_w{worker}_r{round_number}_"
            f"s{seed_id}_n{time_id}_q{sequence:x}"
        )
        return validate_database_name(database)


def _retry_database_name(database: str, generation: int) -> str:
    identity = sha256(database.encode("ascii")).hexdigest()[:8]
    suffix = f"_retry{generation}_{identity}"
    return validate_database_name(database[: 64 - len(suffix)] + suffix)


class SetupRunnerLike(Protocol):
    def apply(
        self,
        node: NodeConfig,
        database: str,
        bundle: SetupBundleLike,
        *,
        session: QuerySession | None = None,
    ) -> SetupNodeResult: ...


class QueryRunnerLike(Protocol):
    def run(
        self,
        node: NodeConfig,
        database: str,
        sql: str,
        *,
        timeout_s: float,
        row_limit: int,
        byte_limit: int,
        barrier: BarrierLike | None = None,
    ) -> NodeExecution: ...

    def run_session(
        self,
        session: QuerySession,
        node: NodeConfig,
        database: str,
        sql: str,
        *,
        timeout_s: float,
        row_limit: int,
        byte_limit: int,
        barrier: BarrierLike | None = None,
    ) -> NodeExecution: ...


class ReplicationWaiterLike(Protocol):
    def wait(self, database: str, sequence: int) -> object: ...


class PreparedRound:
    """One retained setup and, when required, its three pinned sessions."""

    def __init__(
        self,
        *,
        status: PrepareStatus,
        database: str,
        bundle: SetupBundleLike,
        nodes: tuple[SetupNodeResult, ...],
        generation: int,
        sessions: Mapping[NodeRole, QuerySession] | None = None,
        stack: ExitStack | None = None,
        replication_result: object | None = None,
        attempted_setup_sql: tuple[str, ...] = (),
        setup_statement_records: tuple[SetupStatementRecord, ...] = (),
        setup_failing_sql: str | None = None,
    ) -> None:
        self.status = status
        self.database = database
        self.bundle = bundle
        self.nodes = nodes
        self.generation = generation
        self.sessions = None if sessions is None else dict(sessions)
        self._stack = stack
        self.replication_result = replication_result
        self.attempted_setup_sql = attempted_setup_sql
        self.setup_statement_records = setup_statement_records
        self.setup_failing_sql = setup_failing_sql
        self._closed = False
        self._replacement: PreparedRound | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._stack is not None:
            self._stack.close()

    def __enter__(self) -> PreparedRound:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class BaselineExplainResult:
    """One unpersisted baseline EXPLAIN admission result."""

    prepared: PreparedRound
    execution: NodeExecution


@dataclass(frozen=True, slots=True)
class TriadExecutionResult(Sequence[NodeExecution]):
    prepared: PreparedRound
    executions: tuple[NodeExecution, ...]

    def __iter__(self) -> Iterator[NodeExecution]:
        return iter(self.executions)

    def __len__(self) -> int:
        return len(self.executions)

    @overload
    def __getitem__(self, index: int) -> NodeExecution: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[NodeExecution, ...]: ...

    def __getitem__(self, index: int | slice) -> NodeExecution | tuple[NodeExecution, ...]:
        return self.executions[index]


def _classify_setup(results: tuple[SetupNodeResult, ...]) -> PrepareStatus:
    if any(result.status is ExecutionStatus.INFRA_ERROR for result in results):
        return PrepareStatus.INFRASTRUCTURE_PAUSE
    if all(result.status is ExecutionStatus.SUCCESS for result in results):
        digests = {result.payload_sha256 for result in results}
        return PrepareStatus.READY if len(digests) == 1 else PrepareStatus.SETUP_MISMATCH
    if all(result.status is ExecutionStatus.ERROR for result in results):
        errors = [result.error for result in results]
        if all(error is not None for error in errors):
            normalized = {normalize_error(error) for error in errors if error is not None}
            if len(normalized) == 1:
                return PrepareStatus.REJECTED_GENERATION
    return PrepareStatus.SETUP_MISMATCH


def _query_infra_failure(node: NodeConfig, error: Exception) -> NodeExecution:
    now = time.monotonic_ns()
    return NodeExecution.failure(
        role=node.role,
        status=ExecutionStatus.INFRA_ERROR,
        started_ns=now,
        ended_ns=now,
        connection_id=None,
        error=ErrorInfo(65011, "HY000", f"triad query failed: {type(error).__name__}"),
        connection_reusable=False,
    )


def _setup_infra_failure(node: NodeConfig, error: Exception) -> SetupNodeResult:
    return SetupNodeResult(
        role=node.role,
        status=ExecutionStatus.INFRA_ERROR,
        error=ErrorInfo(65010, "HY000", f"triad setup failed: {type(error).__name__}"),
    )


class TriadCoordinator:
    """Coordinate one immutable case across all fixed node roles."""

    def __init__(
        self,
        nodes: Sequence[NodeConfig],
        *,
        setup_runner: SetupRunnerLike,
        query_runner: QueryRunnerLike,
        session_factory: ConnectionFactory,
        query_nodes: Sequence[NodeConfig] | None = None,
        replication_waiter: ReplicationWaiterLike | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        by_role = {node.role: node for node in nodes}
        if len(nodes) != 3 or len(by_role) != 3 or set(by_role) != set(NodeRole):
            raise ValueError("coordinator requires exactly one node for each fixed role")
        query_by_role = {
            node.role: node for node in (nodes if query_nodes is None else query_nodes)
        }
        if len(query_by_role) != 3 or set(query_by_role) != set(NodeRole):
            raise ValueError("query_nodes require exactly one node for each fixed role")
        self._nodes = tuple(by_role[role] for role in NodeRole)
        self._query_nodes = tuple(query_by_role[role] for role in NodeRole)
        self._setup_runner = setup_runner
        self._query_runner = query_runner
        self._session_factory = session_factory
        self._replication_waiter = replication_waiter
        self._sleeper = sleeper

    def prepare(
        self,
        bundle: SetupBundleLike,
        *,
        database: str,
        generation: int = 0,
    ) -> PreparedRound:
        database = validate_database_name(database)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ValueError("generation must be a nonnegative integer")
        stack = ExitStack()
        sessions: dict[NodeRole, QuerySession] | None = None
        lockstep_result: LockstepSetupResult | None = None
        try:
            if bundle.requires_same_session:
                sessions = {
                    node.role: stack.enter_context(
                        self._session_factory.query_session(node, "information_schema")
                    )
                    for node in self._nodes
                }

            lockstep_apply = getattr(self._setup_runner, "apply_lockstep", None)
            if callable(lockstep_apply):
                lockstep_result = lockstep_apply(
                    self._nodes,
                    database,
                    bundle,
                    sessions=sessions,
                )
                results = lockstep_result.nodes
                status = {
                    LockstepSetupVerdict.READY: PrepareStatus.READY,
                    LockstepSetupVerdict.MISMATCH: PrepareStatus.SETUP_MISMATCH,
                    LockstepSetupVerdict.REJECTED_GENERATION: PrepareStatus.REJECTED_GENERATION,
                    LockstepSetupVerdict.INFRASTRUCTURE_PAUSE: PrepareStatus.INFRASTRUCTURE_PAUSE,
                }[lockstep_result.verdict]
            else:

                def apply(node: NodeConfig) -> SetupNodeResult:
                    result = self._setup_runner.apply(
                        node,
                        database,
                        bundle,
                        session=None if sessions is None else sessions[node.role],
                    )
                    if result.role is not node.role:
                        return _setup_infra_failure(node, ValueError("setup role mismatch"))
                    return result

                with ThreadPoolExecutor(max_workers=3, thread_name_prefix="sf-setup") as pool:
                    futures = {node.role: pool.submit(apply, node) for node in self._nodes}
                    results = tuple(futures[role].result() for role in NodeRole)
                status = _classify_setup(results)
        except Exception as error:
            stack.close()
            results = tuple(_setup_infra_failure(node, error) for node in self._nodes)
            return PreparedRound(
                status=PrepareStatus.INFRASTRUCTURE_PAUSE,
                database=database,
                bundle=bundle,
                nodes=results,
                generation=generation,
                attempted_setup_sql=(
                    () if lockstep_result is None else lockstep_result.attempted_bundle_sql
                ),
                setup_statement_records=(
                    () if lockstep_result is None else lockstep_result.statement_records
                ),
                setup_failing_sql=(
                    None if lockstep_result is None else lockstep_result.failing_sql
                ),
            )
        if status is not PrepareStatus.READY:
            stack.close()
            return PreparedRound(
                status=status,
                database=database,
                bundle=bundle,
                nodes=results,
                generation=generation,
                attempted_setup_sql=(
                    () if lockstep_result is None else lockstep_result.attempted_bundle_sql
                ),
                setup_statement_records=(
                    () if lockstep_result is None else lockstep_result.statement_records
                ),
                setup_failing_sql=(
                    None if lockstep_result is None else lockstep_result.failing_sql
                ),
            )
        replication_result: object | None = None
        if self._replication_waiter is not None:
            required_sequence = getattr(bundle, "replication_sequence", 0)
            if (
                not isinstance(required_sequence, int)
                or isinstance(required_sequence, bool)
                or required_sequence < 0
            ):
                raise ValueError("bundle replication_sequence must be nonnegative")
            replication_result = self._replication_waiter.wait(database, required_sequence)
            if not bool(getattr(replication_result, "ready", False)):
                stack.close()
                return PreparedRound(
                    status=PrepareStatus.REPLICA_SYNC_TIMEOUT,
                    database=database,
                    bundle=bundle,
                    nodes=results,
                    generation=generation,
                    replication_result=replication_result,
                    attempted_setup_sql=(
                        bundle.statements
                        if lockstep_result is None
                        else lockstep_result.attempted_bundle_sql
                    ),
                    setup_statement_records=(
                        () if lockstep_result is None else lockstep_result.statement_records
                    ),
                )
        return PreparedRound(
            status=status,
            database=database,
            bundle=bundle,
            nodes=results,
            generation=generation,
            sessions=sessions,
            stack=stack if sessions is not None else None,
            replication_result=replication_result,
            attempted_setup_sql=(
                bundle.statements
                if lockstep_result is None
                else lockstep_result.attempted_bundle_sql
            ),
            setup_statement_records=(
                () if lockstep_result is None else lockstep_result.statement_records
            ),
        )

    def prepare_until_recovered(
        self,
        bundle: SetupBundleLike,
        *,
        database: str,
        retry: InfrastructureRetryPolicy = InfrastructureRetryPolicy(),
        should_stop: Callable[[], bool] = lambda: False,
    ) -> PreparedRound:
        """Retry only infrastructure pauses; semantic setup outcomes return directly."""

        delay = retry.initial_delay_seconds
        generation = 0
        while True:
            attempt_database = (
                database
                if generation == 0 or bundle.requires_same_session
                else _retry_database_name(database, generation)
            )
            prepared = self.prepare(
                bundle,
                database=attempt_database,
                generation=generation,
            )
            if prepared.status is not PrepareStatus.INFRASTRUCTURE_PAUSE:
                return prepared
            if should_stop():
                return prepared
            attempts = generation + 1
            if retry.max_attempts is not None and attempts >= retry.max_attempts:
                return prepared
            prepared.close()
            self._sleeper(delay)
            delay = min(retry.max_delay_seconds, delay * retry.multiplier)
            generation += 1

    def ensure_live(self, prepared: PreparedRound) -> PreparedRound:
        current = prepared
        while current._replacement is not None:
            current = current._replacement
        if current.status is not PrepareStatus.READY:
            return current
        if current.sessions is None:
            return current
        healthy = not current.closed
        if healthy:
            for session in current.sessions.values():
                try:
                    if not session.is_alive():
                        healthy = False
                        break
                except Exception:
                    healthy = False
                    break
        if healthy:
            return current
        current.close()
        rebuilt = self.prepare(
            current.bundle,
            database=current.database,
            generation=current.generation + 1,
        )
        current._replacement = rebuilt
        return rebuilt

    def explain_baseline(
        self,
        prepared: PreparedRound,
        sql: str,
        limits: QueryLimits,
    ) -> BaselineExplainResult:
        """Run a plain EXPLAIN on the baseline query node without a triad barrier."""

        current = self.ensure_live(prepared)
        if current.status is not PrepareStatus.READY:
            raise RuntimeError(f"round is not ready: {current.status.value}")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("sql must not be empty")
        statement = sql.strip()
        if statement.endswith(";"):
            statement = statement[:-1].rstrip()
        explain_sql = f"EXPLAIN {statement}"
        node = next(node for node in self._query_nodes if node.role is NodeRole.BASELINE)
        try:
            if current.sessions is None:
                execution = self._query_runner.run(
                    node,
                    current.database,
                    explain_sql,
                    timeout_s=limits.timeout_seconds,
                    row_limit=limits.row_limit,
                    byte_limit=limits.byte_limit,
                    barrier=None,
                )
            else:
                execution = self._query_runner.run_session(
                    current.sessions[node.role],
                    node,
                    current.database,
                    explain_sql,
                    timeout_s=limits.timeout_seconds,
                    row_limit=limits.row_limit,
                    byte_limit=limits.byte_limit,
                    barrier=None,
                )
            if execution.role is not NodeRole.BASELINE:
                raise ValueError("EXPLAIN role mismatch")
        except Exception as error:
            execution = _query_infra_failure(node, error)
        if current.sessions is not None and (
            execution.status is ExecutionStatus.INFRA_ERROR
            or not execution.connection_reusable
        ):
            current.close()
        return BaselineExplainResult(current, execution)

    def execute(
        self,
        prepared: PreparedRound,
        sql: str,
        limits: QueryLimits,
    ) -> TriadExecutionResult:
        current = self.ensure_live(prepared)
        if current.status is not PrepareStatus.READY:
            raise RuntimeError(f"round is not ready: {current.status.value}")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("sql must not be empty")
        barrier = Barrier(3)

        def run(node: NodeConfig) -> NodeExecution:
            try:
                if current.sessions is None:
                    result = self._query_runner.run(
                        node,
                        current.database,
                        sql,
                        timeout_s=limits.timeout_seconds,
                        row_limit=limits.row_limit,
                        byte_limit=limits.byte_limit,
                        barrier=barrier,
                    )
                else:
                    result = self._query_runner.run_session(
                        current.sessions[node.role],
                        node,
                        current.database,
                        sql,
                        timeout_s=limits.timeout_seconds,
                        row_limit=limits.row_limit,
                        byte_limit=limits.byte_limit,
                        barrier=barrier,
                    )
                if result.role is not node.role:
                    raise ValueError("query role mismatch")
                return result
            except Exception as error:
                barrier.abort()
                return _query_infra_failure(node, error)

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="sf-query") as pool:
            futures = {node.role: pool.submit(run, node) for node in self._query_nodes}
            executions = tuple(futures[role].result() for role in NodeRole)
        if current.sessions is not None and any(
            execution.status is ExecutionStatus.INFRA_ERROR or not execution.connection_reusable
            for execution in executions
        ):
            current.close()
        return TriadExecutionResult(current, executions)


__all__ = [
    "BaselineExplainResult",
    "DatabaseNameFactory",
    "InfrastructureRetryPolicy",
    "PrepareStatus",
    "PreparedRound",
    "QueryLimits",
    "TriadCoordinator",
    "TriadExecutionResult",
]
