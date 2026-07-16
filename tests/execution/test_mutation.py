from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic_ns

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.domain import ErrorInfo, ExecutionStatus, NodeExecution
from select_fuzz.execution.mutation import MutationVerdict, TriadMutationCoordinator
from select_fuzz.execution.replication import (
    ReplicationObservation,
    ReplicationWaitResult,
    ReplicationWaitStatus,
)
from select_fuzz.execution.triad import QueryLimits
from select_fuzz.generation.mutation import (
    MutationBatch,
    MutationOperation,
    MutationStatement,
)


class _Session:
    def close(self) -> None:
        return None


class _Factory:
    @contextmanager
    def query_session(self, node, database):  # type: ignore[no-untyped-def]
        yield _Session()

    control_session = query_session


class _BrokenFactory(_Factory):
    @contextmanager
    def query_session(self, node, database):  # type: ignore[no-untyped-def]
        raise RuntimeError("connect failed")
        yield _Session()  # pragma: no cover


class _Runner:
    def __init__(self, *, affected_rows: int = 17) -> None:
        self.by_sql_role: dict[tuple[str, NodeRole], NodeExecution] = {}
        self.executed: list[str] = []
        self.affected_rows = affected_rows

    def run_session(self, session, node, database, sql, *, barrier, **kwargs):  # type: ignore[no-untyped-def]
        self.executed.append(sql)
        barrier.wait(timeout=1)
        override = self.by_sql_role.get((sql, node.role))
        if override is not None:
            return override
        now = monotonic_ns()
        affected = self.affected_rows if sql.startswith(("INSERT", "UPDATE", "DELETE")) else 0
        return NodeExecution.success(
            role=node.role,
            connection_id=100 + list(NodeRole).index(node.role),
            started_ns=now,
            ended_ns=now,
            affected_rows=affected,
        )

    def run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("owned-session path is not used")


@dataclass
class _Waiter:
    ready: bool = True
    calls: list[tuple[str, int]] | None = None

    def wait(self, database: str, sequence: int) -> ReplicationWaitResult:
        if self.calls is None:
            self.calls = []
        self.calls.append((database, sequence))
        observations = {
            role: ReplicationObservation(role, sequence if self.ready else sequence - 1)
            for role in NodeRole
        }
        return ReplicationWaitResult(
            ReplicationWaitStatus.READY if self.ready else ReplicationWaitStatus.TIMEOUT,
            sequence,
            observations,
        )


def _nodes() -> tuple[NodeConfig, ...]:
    return tuple(
        NodeConfig(role=role, host="primary.example", port=33061 + index)
        for index, role in enumerate(NodeRole)
    )


def _batch(sql: str = "UPDATE `t0` SET `id` = `id` + 1 LIMIT 17") -> MutationBatch:
    return MutationBatch(
        seed=41,
        sequence=1,
        statements=(MutationStatement(MutationOperation.UPDATE, sql, 17),),
    )


def _coordinator(runner: _Runner, waiter: _Waiter) -> TriadMutationCoordinator:
    return TriadMutationCoordinator(
        _nodes(),
        factory=_Factory(),
        runner=runner,
        replication_waiter=waiter,
        limits=QueryLimits(10, 1, 1024),
    )


def test_commits_identical_affected_rows_then_waits_for_replicas() -> None:
    runner = _Runner()
    waiter = _Waiter()

    result = _coordinator(runner, waiter).execute_batch("sf_mutation_1", _batch())

    assert result.verdict is MutationVerdict.COMMITTED
    assert result.executed_sql[0] == "START TRANSACTION"
    assert result.executed_sql[-1] == "COMMIT"
    assert waiter.calls == [("sf_mutation_1", 1)]


def test_logged_execution_publishes_each_statement_before_the_next_step() -> None:
    runner = _Runner()
    logged: list[str] = []

    result = _coordinator(runner, _Waiter()).execute_batch_logged(
        "sf_mutation_logged_1",
        _batch(),
        on_statement=logged.append,
    )

    assert result.verdict is MutationVerdict.COMMITTED
    assert logged == list(result.executed_sql)


def test_same_semantic_error_rolls_back_without_waiting_or_finding() -> None:
    sql = "INSERT INTO `t0` VALUES (1)"
    runner = _Runner()
    now = monotonic_ns()
    for role in NodeRole:
        runner.by_sql_role[(sql, role)] = NodeExecution.failure(
            role=role,
            status=ExecutionStatus.ERROR,
            started_ns=now,
            ended_ns=now,
            connection_id=100 + list(NodeRole).index(role),
            error=ErrorInfo(1062, "23000", "Duplicate entry '1' for key 'PRIMARY'"),
        )
    waiter = _Waiter()

    result = _coordinator(runner, waiter).execute_batch("sf_mutation_2", _batch(sql))

    assert result.verdict is MutationVerdict.CONSISTENT_ERROR_ROLLED_BACK
    assert result.executed_sql[-1] == "ROLLBACK"
    assert waiter.calls is None


def test_affected_row_mismatch_rolls_back_and_terminates_round() -> None:
    batch = _batch()
    sql = batch.statements[0].sql
    runner = _Runner()
    now = monotonic_ns()
    runner.by_sql_role[(sql, NodeRole.CUSTOM_ON)] = NodeExecution.success(
        role=NodeRole.CUSTOM_ON,
        connection_id=103,
        started_ns=now,
        ended_ns=now,
        affected_rows=16,
    )

    result = _coordinator(runner, _Waiter()).execute_batch("sf_mutation_3", batch)

    assert result.verdict is MutationVerdict.MISMATCH
    assert result.terminates_round is True
    assert result.executed_sql[-1] == "ROLLBACK"


def test_successful_dml_with_actual_rows_outside_range_rolls_back_and_continues() -> None:
    waiter = _Waiter()

    result = _coordinator(_Runner(affected_rows=1), waiter).execute_batch(
        "sf_mutation_actual_rows_1", _batch()
    )

    assert result.verdict is MutationVerdict.ACTUAL_ROWS_OUT_OF_RANGE_ROLLED_BACK
    assert result.actual_affected_rows == 1
    assert result.terminates_round is False
    assert result.executed_sql[-1] == "ROLLBACK"
    assert waiter.calls is None


def test_committed_batch_reports_replica_sync_timeout_without_rollback() -> None:
    result = _coordinator(_Runner(), _Waiter(ready=False)).execute_batch("sf_mutation_4", _batch())

    assert result.verdict is MutationVerdict.REPLICA_SYNC_TIMEOUT
    assert result.executed_sql[-1] == "COMMIT"
    assert result.terminates_round is True


def test_start_transaction_error_and_session_failure_terminate_without_queries() -> None:
    runner = _Runner()
    now = monotonic_ns()
    runner.by_sql_role[("START TRANSACTION", NodeRole.CUSTOM_ON)] = NodeExecution.failure(
        role=NodeRole.CUSTOM_ON,
        status=ExecutionStatus.ERROR,
        started_ns=now,
        ended_ns=now,
        connection_id=103,
        error=ErrorInfo(1205, "HY000", "lock wait timeout"),
    )
    started = _coordinator(runner, _Waiter()).execute_batch("sf_mutation_5", _batch())
    assert started.verdict is MutationVerdict.MISMATCH
    assert started.failing_sql == "START TRANSACTION"

    broken = TriadMutationCoordinator(
        _nodes(),
        factory=_BrokenFactory(),
        runner=_Runner(),
        replication_waiter=_Waiter(),
        limits=QueryLimits(10, 1, 1024),
    ).execute_batch("sf_mutation_6", _batch())
    assert broken.verdict is MutationVerdict.INFRASTRUCTURE_ERROR
    assert set(broken.final_results) == set(NodeRole)
