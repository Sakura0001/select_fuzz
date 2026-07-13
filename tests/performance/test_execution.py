from __future__ import annotations

from threading import BrokenBarrierError, Lock
import time

import pytest

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.domain import ErrorInfo, ExecutionStatus, NodeExecution
from select_fuzz.performance.execution import FormalRunner, classify_execution
from select_fuzz.performance.models import FrozenCase, Outcome, PerformancePolicy, ScaleKnobs
from select_fuzz.performance.tree import Family, ShapeBoundary


def _nodes() -> tuple[NodeConfig, ...]:
    return tuple(
        NodeConfig(role=role, host=f"{role.value}.example") for role in NodeRole
    )


def _tree(seconds: float) -> str:
    return (
        "-> Table scan on t (cost=1 rows=100) "
        f"(actual time=0..{seconds * 1000} rows=100 loops=1)"
    )


def _frozen() -> FrozenCase:
    return FrozenCase(
        case_id="case_1",
        template_id="test_scan_v1",
        seed=1,
        database="perf_1",
        scale=ScaleKnobs(),
        data_manifest={"rows": 100},
        sql="SELECT SUM(v) FROM t",
        boundary=ShapeBoundary(frozenset({Family.SCAN})),
        medians_seconds={NodeRole.BASELINE: 10.0, NodeRole.CUSTOM_OFF: 10.0},
        attempts=(),
    )


class _Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[NodeRole, str, str, float, object]] = []
        self._lock = Lock()

    def run(
        self,
        node: NodeConfig,
        database: str,
        sql: str,
        *,
        timeout_s: float,
        row_limit: int,
        byte_limit: int,
        barrier: object | None,
    ) -> NodeExecution:
        del row_limit, byte_limit
        assert barrier is not None
        barrier.wait(timeout=2)  # type: ignore[attr-defined]
        started = {
            NodeRole.BASELINE: 1_000_000_000,
            NodeRole.CUSTOM_OFF: 1_050_000_000,
            NodeRole.CUSTOM_ON: 1_080_000_000,
        }[node.role]
        with self._lock:
            self.calls.append((node.role, database, sql, timeout_s, barrier))
        return NodeExecution.success(
            role=node.role,
            connection_id=1,
            started_ns=started,
            ended_ns=started + 10_000_000_000,
            performance_payload={"tree": _tree(10.0)},
        )


def test_all_three_nodes_start_once_behind_one_barrier_and_all_count_for_skew() -> None:
    core = _Runner()

    run = FormalRunner(_nodes(), core, PerformancePolicy()).run(_frozen())

    assert len(core.calls) == 3
    assert {call[0] for call in core.calls} == set(NodeRole)
    assert all(call[2].startswith("EXPLAIN ANALYZE FORMAT=TREE ") for call in core.calls)
    assert all(call[3] == 15.0 for call in core.calls)
    assert len({id(call[4]) for call in core.calls}) == 1
    assert all(call[4] is not None for call in core.calls)
    assert run.start_skew_ms == pytest.approx(80.0)
    assert all(item.cache_state == "unverified" for item in run.measurements.values())


@pytest.mark.parametrize(
    "status,errno,watchdog,outcome",
    [
        (ExecutionStatus.TIMEOUT, 3024, False, Outcome.TIMEOUT),
        (ExecutionStatus.ERROR, 3024, False, Outcome.TIMEOUT),
        (ExecutionStatus.ERROR, 1317, True, Outcome.TIMEOUT),
        (ExecutionStatus.ERROR, 1317, False, Outcome.EXECUTION_ERROR),
        (ExecutionStatus.INFRA_ERROR, 2013, False, Outcome.INFRA_ERROR),
    ],
)
def test_timeout_disconnect_and_execution_error_classification(
    status: ExecutionStatus, errno: int, watchdog: bool, outcome: Outcome
) -> None:
    raw = NodeExecution.failure(
        role=NodeRole.CUSTOM_ON,
        status=status,
        started_ns=0,
        ended_ns=1,
        connection_id=1,
        error=ErrorInfo(errno, "HY000", "safe"),
        watchdog_fired=watchdog,
    )

    assert classify_execution(raw) is outcome


def test_completed_execution_with_partial_tree_is_parse_error() -> None:
    raw = NodeExecution.success(
        role=NodeRole.BASELINE,
        connection_id=1,
        started_ns=0,
        ended_ns=1,
        performance_payload={"tree": "-> Table scan on t (cost=1 rows=1)"},
    )

    run = FormalRunner.measure(raw, _frozen(), PerformancePolicy())

    assert run.outcome is Outcome.PARSE_ERROR
    assert run.root_end_ms is None


def test_execution_error_keeps_only_a_safe_error_type() -> None:
    raw = NodeExecution.failure(
        role=NodeRole.BASELINE,
        status=ExecutionStatus.ERROR,
        started_ns=0,
        ended_ns=1,
        connection_id=1,
        error=ErrorInfo(1064, "42000", "contains private SQL text"),
    )

    measurement = FormalRunner.measure(raw, _frozen(), PerformancePolicy())

    assert measurement.error_type == "MySQLError"
    assert "private" not in measurement.error_type


class _EarlyInfraRunner:
    def run(
        self,
        node: NodeConfig,
        database: str,
        sql: str,
        *,
        timeout_s: float,
        row_limit: int,
        byte_limit: int,
        barrier: object,
    ) -> NodeExecution:
        del database, sql, timeout_s, row_limit, byte_limit
        if node.role is NodeRole.BASELINE:
            return NodeExecution.failure(
                role=node.role,
                status=ExecutionStatus.INFRA_ERROR,
                started_ns=0,
                ended_ns=0,
                connection_id=None,
                error=ErrorInfo(2013, "HY000", "safe"),
                connection_reusable=False,
            )
        try:
            barrier.wait(timeout=0.25)  # type: ignore[attr-defined]
        except BrokenBarrierError:
            pass
        return NodeExecution.failure(
            role=node.role,
            status=ExecutionStatus.INFRA_ERROR,
            started_ns=0,
            ended_ns=0,
            connection_id=None,
            error=ErrorInfo(2013, "HY000", "safe"),
            connection_reusable=False,
        )


def test_pre_barrier_infrastructure_result_aborts_other_workers_immediately() -> None:
    started = time.monotonic()

    FormalRunner(_nodes(), _EarlyInfraRunner(), PerformancePolicy()).run(_frozen())

    assert time.monotonic() - started < 0.10


class _Diagnostics:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.before_roles: list[NodeRole] = []
        self.after_roles: list[NodeRole] = []

    def before(self, node: NodeConfig, database: str) -> object:
        del database
        self.before_roles.append(node.role)
        if self.fail:
            raise RuntimeError("diagnostics unavailable")
        return {"Handler_read_rnd_next": 1}

    def after(
        self, node: NodeConfig, database: str, connection_id: int | None, before: object
    ) -> dict[str, object]:
        del database, connection_id, before
        self.after_roles.append(node.role)
        if self.fail:
            raise RuntimeError("diagnostics unavailable")
        return {"status_delta": {"Handler_read_rnd_next": 9}, "pfs": {"rows_examined": 100}}


def test_diagnostics_are_collected_for_each_node_without_changing_query_elapsed() -> None:
    diagnostics = _Diagnostics()
    run = FormalRunner(
        _nodes(), _Runner(), PerformancePolicy(), diagnostics=diagnostics
    ).run(_frozen())

    assert set(diagnostics.before_roles) == set(NodeRole)
    assert set(diagnostics.after_roles) == set(NodeRole)
    assert all(item.wall_time_ms == 10_000 for item in run.measurements.values())
    assert all("status_delta" in (item.metrics or {}) for item in run.measurements.values())


def test_diagnostics_failure_is_degraded_without_changing_verdict_input() -> None:
    run = FormalRunner(
        _nodes(), _Runner(), PerformancePolicy(), diagnostics=_Diagnostics(fail=True)
    ).run(_frozen())

    assert all(item.outcome is Outcome.COMPLETED for item in run.measurements.values())
    assert all((item.metrics or {}).get("diagnostics_error") for item in run.measurements.values())
