from __future__ import annotations

from contextlib import contextmanager
from threading import Event

import pytest

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.domain import (
    ColumnMeta,
    ErrorInfo,
    ExecutionStatus,
    NodeExecution,
)
from select_fuzz.execution.replication import (
    ReplicationObservation,
    ReplicationWaitResult,
    ReplicationWaitStatus,
)
from select_fuzz.performance.entrypoint import MySQLCpuMaterializationPort
from select_fuzz.performance.materialization import (
    MaterializationExecutionFailure,
    MaterializationInfrastructureFailure,
    MaterializationMismatch,
    MaterializationTimeout,
    ScaleMaterializer,
)
from select_fuzz.performance.templates import CpuDenseSetupManifest


class _Cursor:
    columns: tuple[()] = ()

    def fetchmany(self, size: int):  # type: ignore[no-untyped-def]
        return ()

    def close(self) -> None:
        return None


class _Session:
    def execute(self, sql: str) -> _Cursor:
        return _Cursor()


class _Factory:
    @contextmanager
    def control_session(self, node, database):  # type: ignore[no-untyped-def]
        yield _Session()

    query_session = control_session


class _Runner:
    def __init__(self, affected_by_role: dict[NodeRole, int | None] | None = None) -> None:
        self.calls: list[tuple[int, str]] = []
        self.affected_by_role = affected_by_role or {}

    def run(self, node, database, sql, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((node.port, sql))
        if sql.startswith("SELECT COUNT"):
            columns = (ColumnMeta("count", 8, False, False, False),)
            rows = ((1,),)
        elif sql.startswith("SELECT *"):
            columns = (ColumnMeta("id", 8, False, False, False),)
            rows = ((1,),)
        elif sql.startswith("SHOW CREATE"):
            columns = (ColumnMeta("ddl", 253, False, False, False),)
            rows = (("CREATE TABLE cpu_data",),)
        else:
            columns = ()
            rows = ()
        return NodeExecution.success(
            role=node.role,
            connection_id=100,
            started_ns=1,
            ended_ns=2,
            columns=columns,
            rows=rows,
            affected_rows=self.affected_by_role.get(node.role, 1),
        )


class _Waiter:
    def __init__(self, *, ready: bool = True) -> None:
        self.calls: list[tuple[str, int]] = []
        self.ready = ready

    def wait(self, database: str, sequence: int) -> ReplicationWaitResult:
        self.calls.append((database, sequence))
        return ReplicationWaitResult(
            ReplicationWaitStatus.READY if self.ready else ReplicationWaitStatus.TIMEOUT,
            sequence,
            {
                role: ReplicationObservation(role, sequence if self.ready else sequence - 1)
                for role in NodeRole
            },
        )


def _nodes(base: int, host: str) -> tuple[NodeConfig, ...]:
    return tuple(
        NodeConfig(role=role, host=host, port=base + index) for index, role in enumerate(NodeRole)
    )


def test_performance_materialization_writes_primary_then_reads_replica_after_barrier() -> None:
    primaries = _nodes(33061, "primary.example")
    replicas = _nodes(33161, "replica.example")
    writes = _Runner()
    reads = _Runner()
    waiter = _Waiter()
    port = MySQLCpuMaterializationPort(
        primaries,
        _Factory(),
        writes,  # type: ignore[arg-type]
        timeout_seconds=300,
        stop_event=Event(),
        read_nodes=replicas,
        read_query_runner=reads,  # type: ignore[arg-type]
        replication_waiter=waiter,  # type: ignore[arg-type]
    )
    manifest = CpuDenseSetupManifest(
        "test",
        1,
        1,
        (
            "CREATE TABLE cpu_data (id BIGINT PRIMARY KEY)",
            "INSERT INTO cpu_data VALUES (1)",
        ),
    )

    evidence = ScaleMaterializer(port).rebuild_all("sf_performance_route_1", manifest)

    assert all(item.row_counts == {"cpu_data": 1} for item in evidence.values())
    assert {port for port, _sql in writes.calls} == {node.port for node in primaries}
    assert {port for port, _sql in reads.calls} == {node.port for node in replicas}
    assert any(sql.startswith("INSERT INTO `__select_fuzz") for _, sql in writes.calls)
    assert waiter.calls == [("sf_performance_route_1", 1)]


def test_performance_primary_setup_affected_row_mismatch_stops_before_replica_reads() -> None:
    primaries = _nodes(33061, "primary.example")
    replicas = _nodes(33161, "replica.example")
    writes = _Runner({NodeRole.CUSTOM_ON: 2})
    reads = _Runner()
    waiter = _Waiter()
    port = MySQLCpuMaterializationPort(
        primaries,
        _Factory(),
        writes,  # type: ignore[arg-type]
        timeout_seconds=300,
        stop_event=Event(),
        read_nodes=replicas,
        read_query_runner=reads,  # type: ignore[arg-type]
        replication_waiter=waiter,  # type: ignore[arg-type]
    )
    manifest = CpuDenseSetupManifest(
        "test",
        1,
        1,
        (
            "CREATE TABLE cpu_data (id BIGINT PRIMARY KEY)",
            "INSERT INTO cpu_data VALUES (1)",
        ),
    )

    with pytest.raises(MaterializationMismatch) as captured:
        ScaleMaterializer(port).rebuild_all("sf_performance_mismatch_1", manifest)

    assert captured.value.database == "sf_performance_mismatch_1"
    assert captured.value.sql == "INSERT INTO cpu_data VALUES (1)"
    assert set(captured.value.details["node_results"]) == {role.value for role in NodeRole}
    assert reads.calls == []
    assert waiter.calls == []


@pytest.mark.parametrize(
    ("status", "expected_exception"),
    (
        (ExecutionStatus.TIMEOUT, MaterializationTimeout),
        (ExecutionStatus.INFRA_ERROR, MaterializationInfrastructureFailure),
        (ExecutionStatus.ERROR, MaterializationExecutionFailure),
    ),
)
def test_performance_primary_setup_classifies_consistent_failures(
    status: ExecutionStatus,
    expected_exception: type[Exception],
) -> None:
    class FailingRunner(_Runner):
        def run(self, node, database, sql, **kwargs):  # type: ignore[no-untyped-def]
            if sql.startswith("INSERT INTO cpu_data"):
                return NodeExecution.failure(
                    role=node.role,
                    status=status,
                    connection_id=100,
                    started_ns=1,
                    ended_ns=2,
                    error=ErrorInfo(3024, "HY000", "same failure"),
                    watchdog_error_type=(
                        "ReplicaUnavailable" if status is ExecutionStatus.INFRA_ERROR else None
                    ),
                )
            return super().run(node, database, sql, **kwargs)

    port = MySQLCpuMaterializationPort(
        _nodes(33061, "primary.example"),
        _Factory(),
        FailingRunner(),  # type: ignore[arg-type]
        timeout_seconds=300,
        stop_event=Event(),
    )
    manifest = CpuDenseSetupManifest(
        "test",
        1,
        1,
        (
            "CREATE TABLE cpu_data (id BIGINT PRIMARY KEY)",
            "INSERT INTO cpu_data VALUES (1)",
        ),
    )

    with pytest.raises(expected_exception):
        ScaleMaterializer(port).rebuild_all("sf_performance_failure_1", manifest)


def test_performance_setup_requires_affected_rows_and_replica_timeout_is_terminal() -> None:
    primaries = _nodes(33061, "primary.example")
    manifest = CpuDenseSetupManifest(
        "test",
        1,
        1,
        (
            "CREATE TABLE cpu_data (id BIGINT PRIMARY KEY)",
            "INSERT INTO cpu_data VALUES (1)",
        ),
    )
    missing_rows = MySQLCpuMaterializationPort(
        primaries,
        _Factory(),
        _Runner({role: None for role in NodeRole}),  # type: ignore[arg-type]
        timeout_seconds=300,
        stop_event=Event(),
    )
    with pytest.raises(MaterializationExecutionFailure, match="MissingAffectedRows"):
        ScaleMaterializer(missing_rows).rebuild_all("sf_performance_missing_rows_1", manifest)

    timeout = MySQLCpuMaterializationPort(
        primaries,
        _Factory(),
        _Runner(),  # type: ignore[arg-type]
        timeout_seconds=300,
        stop_event=Event(),
        replication_waiter=_Waiter(ready=False),  # type: ignore[arg-type]
    )
    with pytest.raises(MaterializationExecutionFailure, match="ReplicaSyncTimeout") as captured:
        ScaleMaterializer(timeout).rebuild_all("sf_performance_sync_timeout_1", manifest)
    assert captured.value.database == "sf_performance_sync_timeout_1"
    assert captured.value.details["replication"]["required_sequence"] == 1  # type: ignore[index]


def test_performance_materialization_validates_topology_and_stop_state() -> None:
    nodes = _nodes(33061, "primary.example")
    with pytest.raises(ValueError, match="all three"):
        MySQLCpuMaterializationPort(
            nodes[:2],
            _Factory(),
            _Runner(),  # type: ignore[arg-type]
            timeout_seconds=300,
            stop_event=Event(),
        )
    with pytest.raises(ValueError, match="reads"):
        MySQLCpuMaterializationPort(
            nodes,
            _Factory(),
            _Runner(),  # type: ignore[arg-type]
            timeout_seconds=300,
            stop_event=Event(),
            read_nodes=nodes[:2],
        )
    stopped = Event()
    stopped.set()
    port = MySQLCpuMaterializationPort(
        nodes,
        _Factory(),
        _Runner(),  # type: ignore[arg-type]
        timeout_seconds=300,
        stop_event=stopped,
    )
    with pytest.raises(MaterializationInfrastructureFailure, match="RunStopped"):
        port.prepare_all(
            "sf_performance_stopped_1",
            CpuDenseSetupManifest("test", 1, 1, ("CREATE TABLE cpu_data (id INT)",)),
        )
