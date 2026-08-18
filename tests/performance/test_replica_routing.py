from __future__ import annotations

from contextlib import contextmanager
from threading import Event

import pytest

from select_fuzz.config import COMPARISON_ROLES, NodeConfig, NodeRole
from select_fuzz.domain import (
    ColumnMeta,
    ErrorInfo,
    ExecutionStatus,
    NodeExecution,
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


def _nodes(base: int, host: str) -> tuple[NodeConfig, ...]:
    return tuple(
        NodeConfig(role=role, host=host, port=base + index)
        for index, role in enumerate(COMPARISON_ROLES)
    )


def test_performance_materialization_uses_same_two_endpoints_without_marker() -> None:
    nodes = _nodes(33061, "comparison.example")
    runner = _Runner()
    port = MySQLCpuMaterializationPort(
        nodes,
        _Factory(),
        runner,  # type: ignore[arg-type]
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

    evidence = ScaleMaterializer(port).rebuild_all("sf_performance_route_1", manifest)

    assert all(item.row_counts == {"cpu_data": 1} for item in evidence.values())
    assert {port for port, _sql in runner.calls} == {node.port for node in nodes}
    assert any(sql.startswith("SELECT COUNT") for _, sql in runner.calls)
    assert all("__select_fuzz_replication_marker" not in sql for _, sql in runner.calls)


def test_performance_setup_affected_row_mismatch_stops_before_evidence_reads() -> None:
    nodes = _nodes(33061, "comparison.example")
    runner = _Runner({NodeRole.CUSTOM_ON: 2})
    port = MySQLCpuMaterializationPort(
        nodes,
        _Factory(),
        runner,  # type: ignore[arg-type]
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

    with pytest.raises(MaterializationMismatch) as captured:
        ScaleMaterializer(port).rebuild_all("sf_performance_mismatch_1", manifest)

    assert captured.value.database == "sf_performance_mismatch_1"
    assert captured.value.sql == "INSERT INTO cpu_data VALUES (1)"
    assert set(captured.value.details["node_results"]) == {
        role.value for role in COMPARISON_ROLES
    }
    assert not any(sql.startswith("SELECT") for _, sql in runner.calls)


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
        _nodes(33061, "comparison.example"),
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


def test_performance_setup_requires_affected_rows() -> None:
    nodes = _nodes(33061, "comparison.example")
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
        nodes,
        _Factory(),
        _Runner({role: None for role in COMPARISON_ROLES}),  # type: ignore[arg-type]
        timeout_seconds=300,
        stop_event=Event(),
    )
    with pytest.raises(MaterializationExecutionFailure, match="MissingAffectedRows"):
        ScaleMaterializer(missing_rows).rebuild_all("sf_performance_missing_rows_1", manifest)

def test_performance_materialization_validates_topology_and_stop_state() -> None:
    nodes = _nodes(33061, "comparison.example")
    with pytest.raises(ValueError, match="two comparison"):
        MySQLCpuMaterializationPort(
            nodes[:1],
            _Factory(),
            _Runner(),  # type: ignore[arg-type]
            timeout_seconds=300,
            stop_event=Event(),
        )
    with pytest.raises(ValueError, match="two comparison"):
        MySQLCpuMaterializationPort(
            (
                NodeConfig(role=NodeRole.BASELINE, host="old.example", port=33060),
                *nodes,
            ),
            _Factory(),
            _Runner(),  # type: ignore[arg-type]
            timeout_seconds=300,
            stop_event=Event(),
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
