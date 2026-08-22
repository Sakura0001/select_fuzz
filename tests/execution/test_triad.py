from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Barrier, Lock
from time import monotonic, monotonic_ns
from typing import Any, Iterator

import pytest

from select_fuzz.config import COMPARISON_ROLES, NodeConfig, NodeRole
from select_fuzz.domain import ColumnMeta, ErrorInfo, ExecutionStatus, NodeExecution
from select_fuzz.execution.setup import (
    MySQLSetupRunner,
    SetupNodeResult,
    SetupStatementNodeResult,
)
from select_fuzz.execution.triad import (
    ComparisonCoordinator,
    DatabaseNameFactory,
    InfrastructureRetryPolicy,
    PrepareStatus,
    QueryLimits,
)


def test_comparison_coordinator_requires_exact_off_on_pair() -> None:
    pair = (
        NodeConfig(role=NodeRole.CUSTOM_OFF, host="off.example", port=3306),
        NodeConfig(role=NodeRole.CUSTOM_ON, host="on.example", port=3306),
    )

    factory = _Factory()
    coordinator = ComparisonCoordinator(
        pair,
        setup_runner=MySQLSetupRunner(factory),
        query_runner=_QueryRunner(),
        session_factory=factory,
    )

    prepared = coordinator.prepare(_Bundle(False), database="sf_comparison_pair_1")
    result = coordinator.execute(prepared, "SELECT 1", _limits())
    assert [item.role for item in result] == [NodeRole.CUSTOM_OFF, NodeRole.CUSTOM_ON]


class _DatabaseError(Exception):
    def __init__(self, errno: int, sqlstate: str, msg: str) -> None:
        self.errno = errno
        self.sqlstate = sqlstate
        self.msg = msg
        super().__init__(msg)


class _Cursor:
    columns: tuple[()] = ()

    def __init__(self, affected_rows: int | None = 0) -> None:
        self.affected_rows = affected_rows

    def fetchmany(self, size: int) -> tuple[()]:
        return ()

    def warnings(self) -> tuple[str, ...]:
        return ()

    def close(self) -> None:
        return None


class _Session:
    def __init__(self, factory: _Factory, role: NodeRole, connection_id: int) -> None:
        self.factory = factory
        self.role = role
        self._connection_id = connection_id
        self.alive = True
        self.closed = False
        self.saw_setup = False
        self.executed: list[str] = []

    def connection_id(self) -> int:
        # Real connectors may retain a cached connection ID after transport loss.
        return self._connection_id

    def is_alive(self) -> bool:
        return self.alive

    def execute(self, sql: str) -> _Cursor:
        if not self.alive:
            raise RuntimeError("connection lost")
        self.executed.append(sql)
        if sql.startswith("CREATE TABLE"):
            self.saw_setup = True
            if self.factory.setup_barrier is not None:
                self.factory.setup_barrier.wait(timeout=2)
            if self.role in self.factory.semantic_failures:
                raise self.factory.semantic_failures[self.role]
            if self.role in self.factory.infrastructure_failures:
                raise RuntimeError("transport unavailable")
        affected_rows = (
            self.factory.setup_affected_rows.get(self.role, 3) if sql.startswith("INSERT") else 0
        )
        return _Cursor(affected_rows)

    def disconnect(self) -> None:
        self.alive = False

    def abort(self) -> None:
        self.alive = False

    def close(self) -> None:
        self.closed = True
        self.alive = False


class _Factory:
    def __init__(self) -> None:
        self.next_connection_id = 100
        self.sessions: list[_Session] = []
        self.setup_barrier: Barrier | None = None
        self.semantic_failures: dict[NodeRole, _DatabaseError] = {}
        self.infrastructure_failures: set[NodeRole] = set()
        self.pin_failures: set[NodeRole] = set()
        self.setup_affected_rows: dict[NodeRole, int | None] = {}

    @contextmanager
    def query_session(self, node: NodeConfig, database: str) -> Iterator[_Session]:
        assert database == "information_schema"
        if node.role in self.pin_failures:
            raise RuntimeError("pin failed")
        self.next_connection_id += 1
        session = _Session(self, node.role, self.next_connection_id)
        self.sessions.append(session)
        try:
            yield session
        finally:
            session.close()

    @contextmanager
    def control_session(self, node: NodeConfig, database: str) -> Iterator[_Session]:
        with self.query_session(node, database) as session:
            yield session


@dataclass(frozen=True, slots=True)
class _Bundle:
    requires_same_session: bool
    payload_sha256: str = "a" * 64
    statements: tuple[str, ...] = (
        "SET time_zone = '+00:00';",
        "CREATE TABLE `t0` (`id` BIGINT PRIMARY KEY);",
        "INSERT INTO `t0` VALUES (1),(2),(3);",
    )
    expected_rows: int = 3


class _QueryRunner:
    def __init__(self) -> None:
        self.used_sessions: dict[NodeRole, _Session] = {}
        self.not_reusable: set[NodeRole] = set()
        self.barriers: list[Any] = []

    def _result(self, node: NodeConfig, barrier: Any) -> NodeExecution:
        self.barriers.append(barrier)
        if barrier is not None:
            barrier.wait(timeout=2)
        now = monotonic_ns()
        return NodeExecution.success(
            role=node.role,
            connection_id=500 + list(COMPARISON_ROLES).index(node.role),
            started_ns=now,
            ended_ns=now,
            columns=(ColumnMeta("count", 8, False, False, False),),
            rows=((3,),),
            connection_reusable=node.role not in self.not_reusable,
        )

    def run(
        self,
        node: NodeConfig,
        database: str,
        sql: str,
        *,
        timeout_s: float,
        row_limit: int,
        byte_limit: int,
        barrier: Any,
    ) -> NodeExecution:
        return self._result(node, barrier)

    def run_session(
        self,
        session: _Session,
        node: NodeConfig,
        database: str,
        sql: str,
        *,
        timeout_s: float,
        row_limit: int,
        byte_limit: int,
        barrier: Any,
    ) -> NodeExecution:
        self.used_sessions[node.role] = session
        return self._result(node, barrier)


class _ExplodingQueryRunner(_QueryRunner):
    def _result(self, node: NodeConfig, barrier: Any) -> NodeExecution:
        if node.role is NodeRole.CUSTOM_OFF:
            raise RuntimeError("worker crashed before barrier")
        return super()._result(node, barrier)


class _DigestSetupRunner:
    def apply(
        self,
        node: NodeConfig,
        database: str,
        bundle: _Bundle,
        *,
        session: _Session | None = None,
    ) -> SetupNodeResult:
        digest = "b" * 64 if node.role is NodeRole.CUSTOM_ON else bundle.payload_sha256
        return SetupNodeResult(node.role, ExecutionStatus.SUCCESS, digest)


class _WrongRoleSetupRunner(_DigestSetupRunner):
    def apply(
        self,
        node: NodeConfig,
        database: str,
        bundle: _Bundle,
        *,
        session: _Session | None = None,
    ) -> SetupNodeResult:
        return SetupNodeResult(NodeRole.BASELINE, ExecutionStatus.SUCCESS, bundle.payload_sha256)


class _WrongRoleQueryRunner(_QueryRunner):
    def _result(self, node: NodeConfig, barrier: Any) -> NodeExecution:
        result = super()._result(node, barrier)
        if node.role is NodeRole.CUSTOM_OFF:
            return result
        return NodeExecution.success(
            role=NodeRole.BASELINE,
            connection_id=result.connection_id,
            started_ns=result.started_ns,
            ended_ns=result.ended_ns,
            columns=result.columns,
            rows=result.rows,
        )


class _FlakySetupRunner:
    def __init__(self) -> None:
        self._lock = Lock()
        self._calls = 0
        self.databases: list[str] = []

    def apply(
        self,
        node: NodeConfig,
        database: str,
        bundle: _Bundle,
        *,
        session: _Session | None = None,
    ) -> SetupNodeResult:
        with self._lock:
            self._calls += 1
            attempt = (self._calls - 1) // 2
            self.databases.append(database)
        if attempt == 0:
            return SetupNodeResult(
                node.role,
                ExecutionStatus.INFRA_ERROR,
                error=ErrorInfo(2003, "HY000", "Can't connect to MySQL server"),
            )
        return SetupNodeResult(node.role, ExecutionStatus.SUCCESS, bundle.payload_sha256)


@pytest.fixture
def nodes() -> tuple[NodeConfig, ...]:
    return tuple(
        NodeConfig(role=role, host="127.0.0.1", port=33061 + index)
        for index, role in enumerate(COMPARISON_ROLES)
    )


def _coordinator(
    nodes: tuple[NodeConfig, ...], factory: _Factory, query_runner: _QueryRunner
) -> ComparisonCoordinator:
    return ComparisonCoordinator(
        nodes,
        setup_runner=MySQLSetupRunner(factory),
        query_runner=query_runner,
        session_factory=factory,
    )


def _limits() -> QueryLimits:
    return QueryLimits(timeout_seconds=15, row_limit=10_000, byte_limit=32 << 20)


def test_setup_and_queries_use_the_same_two_endpoints(
    nodes: tuple[NodeConfig, ...],
) -> None:
    setup_ports: list[int] = []
    query_ports: list[int] = []

    class Setup:
        def apply(self, node, database, bundle, *, session=None):  # type: ignore[no-untyped-def]
            setup_ports.append(node.port)
            return SetupNodeResult(node.role, ExecutionStatus.SUCCESS, bundle.payload_sha256)

    class Query(_QueryRunner):
        def run_session(
            self, session, node, database, sql, **kwargs
        ):  # type: ignore[no-untyped-def]
            query_ports.append(node.port)
            return super().run_session(session, node, database, sql, **kwargs)

    coordinator = ComparisonCoordinator(
        nodes,
        setup_runner=Setup(),
        query_runner=Query(),
        session_factory=_Factory(),
    )
    prepared = coordinator.prepare(_Bundle(False), database="sf_correctness_route_1")

    coordinator.execute(prepared, "SELECT 1", _limits())

    assert sorted(setup_ports) == sorted(node.port for node in nodes)
    assert sorted(query_ports) == sorted(node.port for node in nodes)


def test_baseline_explain_uses_one_query_node_without_a_barrier(
    nodes: tuple[NodeConfig, ...],
) -> None:
    calls: list[tuple[NodeConfig, str, float, Any]] = []

    class ExplainRunner(_QueryRunner):
        def run_session(
            self,
            session,
            node,
            database,
            sql,
            *,
            timeout_s,
            row_limit,
            byte_limit,
            barrier,
        ):  # type: ignore[no-untyped-def]
            calls.append((node, sql, timeout_s, barrier))
            now = monotonic_ns()
            return NodeExecution.success(
                role=node.role,
                connection_id=500,
                started_ns=now,
                ended_ns=now,
                columns=(ColumnMeta("table", 253, False, False, False),),
                rows=(("t0",),),
            )

    coordinator = _coordinator(nodes, _Factory(), ExplainRunner())
    prepared = coordinator.prepare(_Bundle(False), database="sf_correctness_explain_1")

    result = coordinator.explain_baseline(
        prepared,
        "SELECT 1;",
        QueryLimits(timeout_seconds=10, row_limit=10_000, byte_limit=32 << 20),
    )

    assert result.prepared is prepared
    assert result.execution.status is ExecutionStatus.SUCCESS
    assert result.execution.rows
    assert len(calls) == 1
    node, sql, timeout, barrier = calls[0]
    assert node.role is NodeRole.CUSTOM_OFF
    assert sql == "EXPLAIN SELECT 1"
    assert timeout == 10
    assert barrier is None


def test_same_setup_bundle_reaches_all_roles_concurrently(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    factory.setup_barrier = Barrier(2)
    coordinator = _coordinator(nodes, factory, _QueryRunner())
    bundle = _Bundle(requires_same_session=False)

    prepared = coordinator.prepare(bundle, database="sf_correctness_w0_r1_s7")

    assert prepared.status is PrepareStatus.READY
    assert {result.role for result in prepared.nodes} == set(COMPARISON_ROLES)
    assert {result.payload_sha256 for result in prepared.nodes} == {bundle.payload_sha256}
    assert all(session.saw_setup for session in factory.sessions)
    assert all(not session.closed for session in factory.sessions)
    prepared.close()
    assert all(session.closed for session in factory.sessions)


def test_initial_dml_affected_row_difference_is_setup_mismatch(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    factory.setup_affected_rows[NodeRole.CUSTOM_ON] = 2

    prepared = _coordinator(nodes, factory, _QueryRunner()).prepare(
        _Bundle(requires_same_session=False),
        database="sf_correctness_setup_rows_1",
    )

    assert prepared.status is PrepareStatus.SETUP_MISMATCH
    assert prepared.setup_failing_sql == "INSERT INTO `t0` VALUES (1),(2),(3);"
    failing_record = prepared.setup_statement_records[-1]
    assert failing_record.results[NodeRole.CUSTOM_OFF].affected_rows == 3
    assert failing_record.results[NodeRole.CUSTOM_ON].affected_rows == 2


def test_initial_dml_requires_connector_affected_rows(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    factory.setup_affected_rows = {role: None for role in COMPARISON_ROLES}

    prepared = _coordinator(nodes, factory, _QueryRunner()).prepare(
        _Bundle(requires_same_session=False),
        database="sf_correctness_setup_rows_2",
    )

    assert prepared.status is PrepareStatus.SETUP_MISMATCH
    assert prepared.setup_failing_sql.startswith("INSERT")


def test_different_semantic_setup_errors_are_a_mismatch(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    for ordinal, role in enumerate(COMPARISON_ROLES):
        factory.semantic_failures[role] = _DatabaseError(
            1005 + ordinal,
            "HY000",
            f"different setup error {ordinal}",
        )

    prepared = _coordinator(nodes, factory, _QueryRunner()).prepare(
        _Bundle(requires_same_session=False),
        database="sf_correctness_setup_errors_1",
    )

    assert prepared.status is PrepareStatus.SETUP_MISMATCH


def test_partial_setup_error_is_mismatch(nodes: tuple[NodeConfig, ...]) -> None:
    factory = _Factory()
    factory.semantic_failures[NodeRole.CUSTOM_ON] = _DatabaseError(
        1005, "HY000", "Cannot create table"
    )
    coordinator = _coordinator(nodes, factory, _QueryRunner())

    prepared = coordinator.prepare(
        _Bundle(requires_same_session=False), database="sf_correctness_w0_r2_s8"
    )

    assert prepared.status is PrepareStatus.SETUP_MISMATCH
    assert prepared.sessions is None


def test_all_same_setup_error_is_rejected_generation(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    for role in COMPARISON_ROLES:
        factory.semantic_failures[role] = _DatabaseError(
            1071, "42000", "Specified key was too long"
        )
    coordinator = _coordinator(nodes, factory, _QueryRunner())

    prepared = coordinator.prepare(
        _Bundle(requires_same_session=False), database="sf_correctness_w0_r3_s9"
    )

    assert prepared.status is PrepareStatus.REJECTED_GENERATION


def test_all_same_access_denied_is_infrastructure_pause_not_rejected_generation(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    for role in COMPARISON_ROLES:
        factory.semantic_failures[role] = _DatabaseError(
            1044, "42000", "Access denied for user to database"
        )
    coordinator = _coordinator(nodes, factory, _QueryRunner())

    prepared = coordinator.prepare(
        _Bundle(requires_same_session=False), database="sf_correctness_w0_r3_s10"
    )

    assert prepared.status is PrepareStatus.INFRASTRUCTURE_PAUSE


def test_partial_infrastructure_setup_failure_is_retryable_pause(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    factory.infrastructure_failures.add(NodeRole.CUSTOM_OFF)
    coordinator = _coordinator(nodes, factory, _QueryRunner())

    prepared = coordinator.prepare(
        _Bundle(requires_same_session=False), database="sf_correctness_w0_r4_s10"
    )

    assert prepared.status is PrepareStatus.INFRASTRUCTURE_PAUSE


def test_prepared_round_reuses_one_pair_for_setup_explain_and_queries(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    query_runner = _QueryRunner()
    coordinator = _coordinator(nodes, factory, query_runner)

    prepared = coordinator.prepare(
        _Bundle(requires_same_session=False), database="sf_correctness_reuse_1"
    )
    coordinator.explain_baseline(prepared, "SELECT 1", _limits())
    coordinator.execute(prepared, "SELECT 1", _limits())
    coordinator.execute(prepared, "SELECT 2", _limits())

    assert prepared.sessions is not None
    assert len(factory.sessions) == 2
    assert query_runner.used_sessions == dict(prepared.sessions)
    assert all(not session.closed for session in factory.sessions)
    prepared.close()
    assert all(session.closed for session in factory.sessions)


def test_temporary_setup_and_query_share_each_pinned_session(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    query_runner = _QueryRunner()
    coordinator = _coordinator(nodes, factory, query_runner)
    bundle = _Bundle(requires_same_session=True)

    prepared = coordinator.prepare(bundle, database="sf_correctness_w0_r5_s11")
    results = coordinator.execute(prepared, "SELECT COUNT(*) FROM `t0` ORDER BY 1", _limits())

    assert prepared.status is PrepareStatus.READY
    assert prepared.sessions is not None
    assert query_runner.used_sessions == dict(prepared.sessions)
    assert all(result.rows == ((bundle.expected_rows,),) for result in results)
    assert len({id(barrier) for barrier in query_runner.barriers}) == 1
    prepared.close()
    assert all(session.closed for session in factory.sessions)


def test_lost_temporary_session_rebuilds_the_whole_round(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    coordinator = _coordinator(nodes, factory, _QueryRunner())
    prepared = coordinator.prepare(
        _Bundle(requires_same_session=True), database="sf_correctness_w0_r6_s12"
    )
    assert prepared.sessions is not None
    old_sessions = dict(prepared.sessions)
    old_generation = prepared.generation
    old_sessions[NodeRole.CUSTOM_ON].disconnect()

    rebuilt = coordinator.ensure_live(prepared)

    assert rebuilt.generation == old_generation + 1
    assert rebuilt.sessions is not None
    assert all(session.closed for session in old_sessions.values())
    assert all(session.saw_setup for session in rebuilt.sessions.values())
    assert all(
        rebuilt.sessions[role] is not old_sessions[role] for role in COMPARISON_ROLES
    )
    rebuilt.close()


def test_lost_ordinary_session_reconnects_without_replaying_setup(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    coordinator = _coordinator(nodes, factory, _QueryRunner())
    prepared = coordinator.prepare(
        _Bundle(requires_same_session=False), database="sf_correctness_reconnect_1"
    )
    assert prepared.sessions is not None
    old_sessions = dict(prepared.sessions)
    old_sessions[NodeRole.CUSTOM_ON].disconnect()

    reconnected = coordinator.ensure_live(prepared)

    assert reconnected.status is PrepareStatus.READY
    assert reconnected.generation == prepared.generation + 1
    assert reconnected.sessions is not None
    assert all(session.closed for session in old_sessions.values())
    new_sessions = tuple(
        session for session in factory.sessions if session not in old_sessions.values()
    )
    assert len(new_sessions) == 2
    assert all(not session.saw_setup for session in new_sessions)
    assert all(
        session.executed == ["USE `sf_correctness_reconnect_1`"]
        for session in new_sessions
    )
    reconnected.close()


def test_ordinary_reconnect_pause_can_recover_without_replaying_setup(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    coordinator = _coordinator(nodes, factory, _QueryRunner())
    prepared = coordinator.prepare(
        _Bundle(requires_same_session=False), database="sf_correctness_reconnect_2"
    )
    assert prepared.sessions is not None
    prepared.sessions[NodeRole.CUSTOM_OFF].disconnect()
    factory.pin_failures.add(NodeRole.CUSTOM_OFF)

    paused = coordinator.ensure_live(prepared)

    assert paused.status is PrepareStatus.INFRASTRUCTURE_PAUSE
    factory.pin_failures.clear()
    recovered = coordinator.ensure_live(paused)

    assert recovered.status is PrepareStatus.READY
    assert recovered.generation == paused.generation + 1
    assert all(node.status is ExecutionStatus.SUCCESS for node in recovered.nodes)
    assert recovered.sessions is not None
    assert all(not session.saw_setup for session in recovered.sessions.values())
    assert all(
        session.executed == ["USE `sf_correctness_reconnect_2`"]
        for session in recovered.sessions.values()
    )
    recovered.close()


def test_reconnect_pause_returns_typed_infrastructure_results(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    query_runner = _QueryRunner()
    coordinator = _coordinator(nodes, factory, query_runner)
    prepared = coordinator.prepare(
        _Bundle(requires_same_session=False), database="sf_correctness_reconnect_3"
    )
    assert prepared.sessions is not None
    prepared.sessions[NodeRole.CUSTOM_ON].disconnect()
    factory.pin_failures.add(NodeRole.CUSTOM_ON)

    explain = coordinator.explain_baseline(prepared, "SELECT 1", _limits())
    comparison = coordinator.execute(explain.prepared, "SELECT 1", _limits())

    assert explain.prepared.status is PrepareStatus.INFRASTRUCTURE_PAUSE
    assert explain.execution.status is ExecutionStatus.INFRA_ERROR
    assert "查询连接恢复暂停" in explain.execution.error.message
    assert comparison.prepared.status is PrepareStatus.INFRASTRUCTURE_PAUSE
    assert [result.role for result in comparison] == list(COMPARISON_ROLES)
    assert all(result.status is ExecutionStatus.INFRA_ERROR for result in comparison)
    assert query_runner.used_sessions == {}


def test_unusable_temporary_query_result_invalidates_both_sessions(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    query_runner = _QueryRunner()
    query_runner.not_reusable.add(NodeRole.CUSTOM_ON)
    coordinator = _coordinator(nodes, factory, query_runner)
    prepared = coordinator.prepare(
        _Bundle(requires_same_session=True), database="sf_correctness_w0_r7_s13"
    )
    assert prepared.sessions is not None
    old_sessions = dict(prepared.sessions)

    first = coordinator.execute(prepared, "SELECT 1 ORDER BY 1", _limits())

    assert first.prepared is prepared
    assert all(session.closed for session in old_sessions.values())
    rebuilt = coordinator.ensure_live(prepared)
    assert rebuilt.generation == prepared.generation + 1
    rebuilt.close()


@pytest.mark.parametrize(
    "database",
    ("sf_ok`; DROP DATABASE mysql", "mysql", "information_schema"),
)
def test_setup_runner_rejects_unsafe_or_non_product_database_before_connecting(
    nodes: tuple[NodeConfig, ...],
    database: str,
) -> None:
    factory = _Factory()

    with pytest.raises(ValueError, match="database"):
        MySQLSetupRunner(factory).apply(nodes[0], database, _Bundle(False))

    assert factory.sessions == []


def test_setup_node_result_enforces_success_error_invariants() -> None:
    with pytest.raises(ValueError, match="payload"):
        SetupNodeResult(
            role=NodeRole.BASELINE,
            status=ExecutionStatus.SUCCESS,
            payload_sha256=None,
        )
    with pytest.raises(ValueError, match="error"):
        SetupNodeResult(
            role=NodeRole.BASELINE,
            status=ExecutionStatus.ERROR,
            error=None,
        )
    result = SetupNodeResult(
        role=NodeRole.BASELINE,
        status=ExecutionStatus.ERROR,
        error=ErrorInfo(1005, "HY000", "Cannot create table"),
    )
    assert result.payload_sha256 is None

    with pytest.raises(ValueError, match="successful setup cannot"):
        SetupNodeResult(
            role=NodeRole.BASELINE,
            status=ExecutionStatus.SUCCESS,
            payload_sha256="a" * 64,
            error=ErrorInfo(1005, "HY000", "unexpected"),
        )
    with pytest.raises(ValueError, match="status"):
        SetupNodeResult(
            role=NodeRole.BASELINE,
            status=ExecutionStatus.TIMEOUT,
            error=ErrorInfo(3024, "HY000", "timeout"),
        )
    with pytest.raises(ValueError, match="payload"):
        SetupNodeResult(
            role=NodeRole.BASELINE,
            status=ExecutionStatus.ERROR,
            payload_sha256="a" * 64,
            error=ErrorInfo(1005, "HY000", "failure"),
        )


def test_setup_statement_result_enforces_status_payload_invariants() -> None:
    error = ErrorInfo(1005, "HY000", "failure")
    with pytest.raises(ValueError, match="successful setup statement"):
        SetupStatementNodeResult(
            NodeRole.BASELINE,
            ExecutionStatus.SUCCESS,
            error=error,
        )
    with pytest.raises(ValueError, match="status"):
        SetupStatementNodeResult(
            NodeRole.BASELINE,
            ExecutionStatus.TIMEOUT,
            error=error,
        )
    with pytest.raises(ValueError, match="requires an error"):
        SetupStatementNodeResult(NodeRole.BASELINE, ExecutionStatus.ERROR)
    with pytest.raises(ValueError, match="affected rows"):
        SetupStatementNodeResult(
            NodeRole.BASELINE,
            ExecutionStatus.ERROR,
            affected_rows=1,
            error=error,
        )


def test_payload_checksum_divergence_is_setup_mismatch(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    coordinator = ComparisonCoordinator(
        nodes,
        setup_runner=_DigestSetupRunner(),
        query_runner=_QueryRunner(),
        session_factory=factory,
    )

    prepared = coordinator.prepare(_Bundle(False), database="sf_correctness_w0_r8_s14")

    assert prepared.status is PrepareStatus.SETUP_MISMATCH


def test_partial_pinned_session_acquisition_closes_earlier_leases(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    factory.pin_failures.add(NodeRole.CUSTOM_ON)
    coordinator = _coordinator(nodes, factory, _QueryRunner())

    prepared = coordinator.prepare(_Bundle(True), database="sf_correctness_w0_r9_s15")

    assert prepared.status is PrepareStatus.INFRASTRUCTURE_PAUSE
    assert prepared.sessions is None
    assert len(factory.sessions) == 1
    assert factory.sessions[0].closed


def test_failed_temporary_setup_closes_all_pinned_sessions(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    factory.semantic_failures[NodeRole.CUSTOM_ON] = _DatabaseError(
        1005, "HY000", "Cannot create table"
    )
    coordinator = _coordinator(nodes, factory, _QueryRunner())

    prepared = coordinator.prepare(_Bundle(True), database="sf_correctness_w0_r10_s16")

    assert prepared.status is PrepareStatus.SETUP_MISMATCH
    assert prepared.sessions is None
    assert len(factory.sessions) == 2
    assert all(session.closed for session in factory.sessions)


def test_query_worker_exception_aborts_shared_barrier_without_waiting_for_timeout(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    coordinator = _coordinator(nodes, factory, _ExplodingQueryRunner())
    prepared = coordinator.prepare(_Bundle(False), database="sf_correctness_w0_r11_s17")

    started = monotonic()
    result = coordinator.execute(prepared, "SELECT 1 ORDER BY 1", _limits())

    assert monotonic() - started < 1
    assert all(item.status is ExecutionStatus.INFRA_ERROR for item in result)
    by_role = {item.role: item for item in result}
    off_error = by_role[NodeRole.CUSTOM_OFF].error
    on_error = by_role[NodeRole.CUSTOM_ON].error
    assert off_error is not None and "worker crashed before barrier" in off_error.message
    assert on_error is not None and "BrokenBarrierError" in on_error.message
    assert all(
        item.failure_evidence is not None
        and item.failure_evidence["failure_stage"] == "comparison_query"
        for item in result
    )


def test_infrastructure_pause_retries_with_bounded_exponential_backoff(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    delays: list[float] = []
    setup_runner = _FlakySetupRunner()
    coordinator = ComparisonCoordinator(
        nodes,
        setup_runner=setup_runner,
        query_runner=_QueryRunner(),
        session_factory=factory,
        sleeper=delays.append,
    )

    prepared = coordinator.prepare_until_recovered(
        _Bundle(False),
        database="sf_correctness_w0_r12_s18",
        retry=InfrastructureRetryPolicy(
            initial_delay_seconds=0.125,
            max_delay_seconds=1,
            multiplier=2,
            max_attempts=3,
        ),
    )

    assert prepared.status is PrepareStatus.READY
    assert prepared.generation == 1
    assert delays == [0.125]
    first_attempt_databases = set(setup_runner.databases[:2])
    second_attempt_databases = set(setup_runner.databases[2:])
    assert first_attempt_databases == {"sf_correctness_w0_r12_s18"}
    assert len(second_attempt_databases) == 1
    assert second_attempt_databases != first_attempt_databases
    assert prepared.database in second_attempt_databases


@pytest.mark.parametrize(
    "limits",
    (
        {"timeout_seconds": float("nan"), "row_limit": 1, "byte_limit": 1},
        {"timeout_seconds": 301, "row_limit": 1, "byte_limit": 1},
        {"timeout_seconds": 1, "row_limit": 0, "byte_limit": 1},
    ),
)
def test_query_limits_reject_nonfinite_over_ceiling_or_nonpositive_values(
    limits: dict[str, float | int],
) -> None:
    with pytest.raises(ValueError):
        QueryLimits(**limits)  # type: ignore[arg-type]


def test_comparison_contracts_are_exported_from_execution_package() -> None:
    import select_fuzz.execution as execution

    assert execution.ComparisonCoordinator is ComparisonCoordinator
    assert execution.MySQLSetupRunner is MySQLSetupRunner
    assert execution.QueryLimits is QueryLimits


def test_database_names_include_mode_time_worker_round_seed_and_are_unique() -> None:
    names = DatabaseNameFactory(clock_ns=lambda: 1_784_134_800_123_456_789)

    first = names.new(
        mode="correctness", worker=7, round_number=42, seed=-9_223_372_036_854_775_808
    )
    second = names.new(
        mode="correctness", worker=7, round_number=42, seed=-9_223_372_036_854_775_808
    )

    assert first != second
    assert first.startswith("sf_c_202607")
    assert "_w7_r42_s" in first
    assert len(first) <= 64
    assert first.replace("_", "a").isalnum() and first == first.lower()


def test_database_names_do_not_collide_across_processes_in_the_same_second() -> None:
    first_process = DatabaseNameFactory(clock_ns=lambda: 1_784_134_800_000_000_001)
    second_process = DatabaseNameFactory(clock_ns=lambda: 1_784_134_800_000_000_002)

    first = first_process.new(mode="correctness", worker=0, round_number=1, seed=7)
    second = second_process.new(mode="correctness", worker=0, round_number=1, seed=7)

    assert first != second


def test_retry_names_preserve_identity_when_external_database_names_are_64_chars(
    nodes: tuple[NodeConfig, ...],
) -> None:
    bases = ("sf_" + "a" * 60 + "x", "sf_" + "a" * 60 + "y")
    recovered_names: list[str] = []
    for database in bases:
        setup_runner = _FlakySetupRunner()
        coordinator = ComparisonCoordinator(
            nodes,
            setup_runner=setup_runner,
            query_runner=_QueryRunner(),
            session_factory=_Factory(),
            sleeper=lambda delay: None,
        )
        recovered = coordinator.prepare_until_recovered(
            _Bundle(False),
            database=database,
            retry=InfrastructureRetryPolicy(max_attempts=2),
        )
        recovered_names.append(recovered.database)

    assert recovered_names[0] != recovered_names[1]
    assert all(len(name) <= 64 for name in recovered_names)


def test_setup_result_with_wrong_role_is_infrastructure_pause(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    coordinator = ComparisonCoordinator(
        nodes,
        setup_runner=_WrongRoleSetupRunner(),
        query_runner=_QueryRunner(),
        session_factory=factory,
    )

    prepared = coordinator.prepare(_Bundle(False), database="sf_correctness_w0_r13_s19")

    assert prepared.status is PrepareStatus.INFRASTRUCTURE_PAUSE
    assert {result.role for result in prepared.nodes} == set(COMPARISON_ROLES)


def test_query_result_with_wrong_role_is_sanitized_as_worker_infrastructure_error(
    nodes: tuple[NodeConfig, ...],
) -> None:
    factory = _Factory()
    coordinator = _coordinator(nodes, factory, _WrongRoleQueryRunner())
    prepared = coordinator.prepare(_Bundle(False), database="sf_correctness_w0_r14_s20")

    results = coordinator.execute(prepared, "SELECT 3 ORDER BY 1", _limits())

    assert {result.role for result in results} == set(COMPARISON_ROLES)
    assert results[1].status is ExecutionStatus.INFRA_ERROR
    assert all(
        result.status in {ExecutionStatus.SUCCESS, ExecutionStatus.INFRA_ERROR}
        for result in results
    )
