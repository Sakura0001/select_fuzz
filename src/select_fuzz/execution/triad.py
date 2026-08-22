"""Two-instance setup and query coordination."""

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
from typing import Callable, Protocol, cast, overload

from select_fuzz.config import (
    COMPARISON_ROLES,
    MAX_STATEMENT_TIMEOUT_SECONDS,
    NodeConfig,
    NodeRole,
)
from select_fuzz.domain import ErrorInfo, ExecutionStatus, NodeExecution
from select_fuzz.execution.evidence import capture_exception_evidence
from select_fuzz.execution.protocols import (
    BarrierLike,
    ConnectionFactory,
    OwnedConnectionFactory,
    QuerySession,
)
from select_fuzz.execution.sessions import acquire_session_pair
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


class PreparedRound:
    """One retained setup and, when required, its two pinned sessions."""

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
        setup_completed: bool = False,
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
        self.setup_completed = setup_completed
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
class ComparisonExecutionResult(Sequence[NodeExecution]):
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
        error=ErrorInfo(
            65011,
            "HY000",
            f"对比查询基础设施失败: {type(error).__name__}: {str(error)[:2048]}",
        ),
        connection_reusable=False,
        failure_evidence=capture_exception_evidence(error, "comparison_query"),
    )


def _paused_query_failure(prepared: PreparedRound, node: NodeConfig) -> NodeExecution:
    setup_result = next(
        (result for result in prepared.nodes if result.role is node.role),
        None,
    )
    detail = (
        setup_result.error.message
        if setup_result is not None and setup_result.error is not None
        else "查询连接尚未恢复"
    )
    now = time.monotonic_ns()
    return NodeExecution.failure(
        role=node.role,
        status=ExecutionStatus.INFRA_ERROR,
        started_ns=now,
        ended_ns=now,
        connection_id=None,
        error=ErrorInfo(65011, "HY000", f"查询连接恢复暂停: {detail}"),
        connection_reusable=False,
    )


def _setup_infra_failure(node: NodeConfig, error: Exception) -> SetupNodeResult:
    return SetupNodeResult(
        role=node.role,
        status=ExecutionStatus.INFRA_ERROR,
        error=ErrorInfo(
            65010,
            "HY000",
            f"对比建库失败: {type(error).__name__}: {str(error)[:2048]}",
        ),
    )


def _pair_open_failure_message(
    role: NodeRole,
    evidence: Mapping[str, object] | None,
) -> str:
    if evidence is None:
        return f"{role.value} 连接成功，但对端连接失败；本节点未开始建库"
    raw_exception = evidence.get("exception")
    if isinstance(raw_exception, Mapping):
        error_type = raw_exception.get("type", "Exception")
        message = raw_exception.get("message", "")
        return f"{role.value} 查询连接建立失败: {error_type}: {message}"
    return f"{role.value} 查询连接建立失败，异常证据不完整"


def _open_legacy_session_pair(
    nodes: Sequence[NodeConfig],
    factory: ConnectionFactory,
    stack: ExitStack,
) -> tuple[dict[NodeRole, QuerySession] | None, dict[NodeRole, Exception]]:
    """Compatibility path for test/third-party factories without owned leases."""

    managers = {
        node.role: factory.query_session(node, "information_schema") for node in nodes
    }

    def enter(role: NodeRole) -> QuerySession:
        return managers[role].__enter__()

    sessions: dict[NodeRole, QuerySession] = {}
    failures: dict[NodeRole, Exception] = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sf-pair-connect") as pool:
        futures = {role: pool.submit(enter, role) for role in COMPARISON_ROLES}
        for role in COMPARISON_ROLES:
            try:
                sessions[role] = futures[role].result()
            except Exception as error:
                failures[role] = error
    if failures:
        for role in sessions:
            managers[role].__exit__(None, None, None)
        return None, failures
    for role in COMPARISON_ROLES:
        manager = managers[role]
        stack.callback(manager.__exit__, None, None, None)
    return sessions, failures


class ComparisonCoordinator:
    """Coordinate one immutable case across custom_off and custom_on."""

    def __init__(
        self,
        nodes: Sequence[NodeConfig],
        *,
        setup_runner: SetupRunnerLike,
        query_runner: QueryRunnerLike,
        session_factory: ConnectionFactory,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        by_role = {node.role: node for node in nodes}
        if len(nodes) != 2 or len(by_role) != 2 or set(by_role) != set(COMPARISON_ROLES):
            raise ValueError("comparison coordinator requires custom_off and custom_on")
        self._nodes = tuple(by_role[role] for role in COMPARISON_ROLES)
        self._query_nodes = self._nodes
        self._setup_runner = setup_runner
        self._query_runner = query_runner
        self._session_factory = session_factory
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
            open_owned = getattr(self._session_factory, "open_query_session", None)
            if callable(open_owned):
                acquisition = acquire_session_pair(
                    self._nodes,
                    "information_schema",
                    cast(OwnedConnectionFactory, self._session_factory),
                )
                if not acquisition.ready:
                    results = tuple(
                        _setup_infra_failure(
                            node,
                            RuntimeError(
                                _pair_open_failure_message(
                                    node.role,
                                    acquisition.attempts[node.role].failure_evidence,
                                )
                            ),
                        )
                        for node in self._nodes
                    )
                    return PreparedRound(
                        status=PrepareStatus.INFRASTRUCTURE_PAUSE,
                        database=database,
                        bundle=bundle,
                        nodes=results,
                        generation=generation,
                    )
                stack.callback(acquisition.close)
                sessions = {
                    role: lease.session for role, lease in acquisition.leases.items()
                }
            else:
                sessions, failures = _open_legacy_session_pair(
                    self._nodes,
                    self._session_factory,
                    stack,
                )
                if sessions is None:
                    results = tuple(
                        _setup_infra_failure(
                            node,
                            failures.get(
                                node.role,
                                RuntimeError("对端查询连接建立失败，本节点未开始建库"),
                            ),
                        )
                        for node in self._nodes
                    )
                    return PreparedRound(
                        status=PrepareStatus.INFRASTRUCTURE_PAUSE,
                        database=database,
                        bundle=bundle,
                        nodes=results,
                        generation=generation,
                    )

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

                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sf-setup") as pool:
                    futures = {node.role: pool.submit(apply, node) for node in self._nodes}
                    results = tuple(futures[role].result() for role in COMPARISON_ROLES)
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
        return PreparedRound(
            status=status,
            database=database,
            bundle=bundle,
            nodes=results,
            generation=generation,
            sessions=sessions,
            stack=stack,
            attempted_setup_sql=(
                bundle.statements
                if lockstep_result is None
                else lockstep_result.attempted_bundle_sql
            ),
            setup_statement_records=(
                () if lockstep_result is None else lockstep_result.statement_records
            ),
            setup_completed=True,
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
        if current.status is PrepareStatus.INFRASTRUCTURE_PAUSE:
            current.close()
            rebuilt = (
                self._reconnect_existing_round(current)
                if current.setup_completed and not current.bundle.requires_same_session
                else self.prepare(
                    current.bundle,
                    database=current.database,
                    generation=current.generation + 1,
                )
            )
            current._replacement = rebuilt
            return rebuilt
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
        rebuilt = (
            self.prepare(
                current.bundle,
                database=current.database,
                generation=current.generation + 1,
            )
            if current.bundle.requires_same_session
            else self._reconnect_existing_round(current)
        )
        current._replacement = rebuilt
        return rebuilt

    def _reconnect_existing_round(self, current: PreparedRound) -> PreparedRound:
        """Reconnect an ordinary-table round without replaying non-idempotent setup."""

        stack = ExitStack()
        sessions: dict[NodeRole, QuerySession] | None = None
        failures: dict[NodeRole, Exception] = {}
        open_owned = getattr(self._session_factory, "open_query_session", None)
        if callable(open_owned):
            acquisition = acquire_session_pair(
                self._nodes,
                "information_schema",
                cast(OwnedConnectionFactory, self._session_factory),
            )
            if acquisition.ready:
                stack.callback(acquisition.close)
                sessions = {
                    role: lease.session for role, lease in acquisition.leases.items()
                }
            else:
                failures = {
                    node.role: RuntimeError(
                        _pair_open_failure_message(
                            node.role,
                            acquisition.attempts[node.role].failure_evidence,
                        )
                    )
                    for node in self._nodes
                }
        else:
            sessions, failures = _open_legacy_session_pair(
                self._nodes,
                self._session_factory,
                stack,
            )

        if sessions is None:
            stack.close()
            results = tuple(
                _setup_infra_failure(
                    node,
                    failures.get(
                        node.role,
                        RuntimeError("对端查询连接建立失败，本节点未开始恢复"),
                    ),
                )
                for node in self._nodes
            )
            return PreparedRound(
                status=PrepareStatus.INFRASTRUCTURE_PAUSE,
                database=current.database,
                bundle=current.bundle,
                nodes=results,
                generation=current.generation + 1,
                attempted_setup_sql=current.attempted_setup_sql,
                setup_statement_records=current.setup_statement_records,
                setup_failing_sql=current.setup_failing_sql,
                setup_completed=current.setup_completed,
            )

        def select_database(node: NodeConfig) -> Exception | None:
            try:
                cursor = sessions[node.role].execute(f"USE `{current.database}`")
                cursor.close()
            except Exception as error:
                return error
            return None

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sf-pair-reconnect") as pool:
            futures = {
                node.role: pool.submit(select_database, node) for node in self._nodes
            }
            use_failures = {
                role: error
                for role, future in futures.items()
                if (error := future.result()) is not None
            }
        if use_failures:
            stack.close()
            results = tuple(
                _setup_infra_failure(
                    node,
                    use_failures.get(
                        node.role,
                        RuntimeError("对端选择已有数据库失败，本节点连接已释放"),
                    ),
                )
                for node in self._nodes
            )
            return PreparedRound(
                status=PrepareStatus.INFRASTRUCTURE_PAUSE,
                database=current.database,
                bundle=current.bundle,
                nodes=results,
                generation=current.generation + 1,
                attempted_setup_sql=current.attempted_setup_sql,
                setup_statement_records=current.setup_statement_records,
                setup_failing_sql=current.setup_failing_sql,
                setup_completed=current.setup_completed,
            )

        return PreparedRound(
            status=PrepareStatus.READY,
            database=current.database,
            bundle=current.bundle,
            nodes=tuple(
                SetupNodeResult(
                    role=node.role,
                    status=ExecutionStatus.SUCCESS,
                    payload_sha256=current.bundle.payload_sha256,
                )
                for node in self._nodes
            ),
            generation=current.generation + 1,
            sessions=sessions,
            stack=stack,
            attempted_setup_sql=current.attempted_setup_sql,
            setup_statement_records=current.setup_statement_records,
            setup_failing_sql=current.setup_failing_sql,
            setup_completed=True,
        )

    def explain_baseline(
        self,
        prepared: PreparedRound,
        sql: str,
        limits: QueryLimits,
    ) -> BaselineExplainResult:
        """Run a plain EXPLAIN on custom_off without a comparison barrier."""

        current = self.ensure_live(prepared)
        if current.status is PrepareStatus.INFRASTRUCTURE_PAUSE:
            node = next(
                node for node in self._query_nodes if node.role is NodeRole.CUSTOM_OFF
            )
            return BaselineExplainResult(current, _paused_query_failure(current, node))
        if current.status is not PrepareStatus.READY:
            raise RuntimeError(f"round is not ready: {current.status.value}")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("sql must not be empty")
        statement = sql.strip()
        if statement.endswith(";"):
            statement = statement[:-1].rstrip()
        explain_sql = f"EXPLAIN {statement}"
        node = next(node for node in self._query_nodes if node.role is NodeRole.CUSTOM_OFF)
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
            if execution.role is not NodeRole.CUSTOM_OFF:
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
    ) -> ComparisonExecutionResult:
        current = self.ensure_live(prepared)
        if current.status is PrepareStatus.INFRASTRUCTURE_PAUSE:
            return ComparisonExecutionResult(
                current,
                tuple(
                    _paused_query_failure(current, node)
                    for node in self._query_nodes
                ),
            )
        if current.status is not PrepareStatus.READY:
            raise RuntimeError(f"round is not ready: {current.status.value}")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("sql must not be empty")
        barrier = Barrier(2)

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

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sf-query") as pool:
            futures = {node.role: pool.submit(run, node) for node in self._query_nodes}
            executions = tuple(futures[role].result() for role in COMPARISON_ROLES)
        if current.sessions is not None and any(
            execution.status is ExecutionStatus.INFRA_ERROR or not execution.connection_reusable
            for execution in executions
        ):
            current.close()
        return ComparisonExecutionResult(current, executions)


# Import compatibility for internal callers while they migrate to pair terminology.
TriadCoordinator = ComparisonCoordinator
TriadExecutionResult = ComparisonExecutionResult


__all__ = [
    "BaselineExplainResult",
    "ComparisonCoordinator",
    "ComparisonExecutionResult",
    "DatabaseNameFactory",
    "InfrastructureRetryPolicy",
    "PrepareStatus",
    "PreparedRound",
    "QueryLimits",
    "TriadCoordinator",
    "TriadExecutionResult",
]
