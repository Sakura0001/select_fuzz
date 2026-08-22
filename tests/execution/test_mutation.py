from __future__ import annotations

from contextlib import contextmanager
from time import monotonic_ns

from select_fuzz.config import COMPARISON_ROLES, NodeConfig, NodeRole
from select_fuzz.domain import ErrorInfo, ExecutionStatus, NodeExecution
from select_fuzz.execution.mutation import (
    MutationRetrySafety,
    MutationVerdict,
    PairMutationCoordinator,
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
    def __init__(self) -> None:
        self.query_session_calls = 0

    @contextmanager
    def query_session(self, node, database):  # type: ignore[no-untyped-def]
        self.query_session_calls += 1
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
            connection_id=100 + list(COMPARISON_ROLES).index(node.role),
            started_ns=now,
            ended_ns=now,
            affected_rows=affected,
        )

    def run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("owned-session path is not used")


def _nodes() -> tuple[NodeConfig, ...]:
    return tuple(
        NodeConfig(role=role, host="primary.example", port=33061 + index)
        for index, role in enumerate(COMPARISON_ROLES)
    )


def _batch(sql: str = "UPDATE `t0` SET `id` = `id` + 1 LIMIT 17") -> MutationBatch:
    return MutationBatch(
        seed=41,
        sequence=1,
        statements=(MutationStatement(MutationOperation.UPDATE, sql, 17),),
    )


def _coordinator(
    runner: _Runner, factory: _Factory | None = None
) -> PairMutationCoordinator:
    return PairMutationCoordinator(
        _nodes(),
        factory=factory or _Factory(),
        runner=runner,
        limits=QueryLimits(10, 1, 1024),
    )


def test_mutation_uses_caller_owned_pair_without_opening_connections() -> None:
    runner = _Runner()
    factory = _Factory()
    sessions = {role: _Session() for role in COMPARISON_ROLES}

    result = _coordinator(runner, factory).execute_batch(
        "sf_mutation_owned_1", _batch(), sessions=sessions
    )

    assert result.verdict is MutationVerdict.COMMITTED
    assert factory.query_session_calls == 0


def test_mutation_infrastructure_retry_safety_distinguishes_commit_ambiguity() -> None:
    now = monotonic_ns()

    def infrastructure(role: NodeRole) -> NodeExecution:
        return NodeExecution.failure(
            role=role,
            status=ExecutionStatus.INFRA_ERROR,
            started_ns=now,
            ended_ns=now,
            connection_id=100,
            error=ErrorInfo(2013, "HY000", "lost connection"),
            connection_reusable=False,
        )

    update_runner = _Runner()
    update_runner.by_sql_role[(_batch().statements[0].sql, NodeRole.CUSTOM_ON)] = (
        infrastructure(NodeRole.CUSTOM_ON)
    )
    precommit = _coordinator(update_runner).execute_batch("sf_mutation_retry_1", _batch())

    commit_runner = _Runner()
    commit_runner.by_sql_role[("COMMIT", NodeRole.CUSTOM_ON)] = infrastructure(
        NodeRole.CUSTOM_ON
    )
    commit = _coordinator(commit_runner).execute_batch("sf_mutation_retry_2", _batch())

    assert precommit.verdict is MutationVerdict.INFRASTRUCTURE_ERROR
    assert precommit.retry_safety is MutationRetrySafety.SAFE_AFTER_RECONNECT
    assert commit.verdict is MutationVerdict.INFRASTRUCTURE_ERROR
    assert commit.retry_safety is MutationRetrySafety.COMMIT_AMBIGUOUS


def test_commits_identical_affected_rows_without_replication_marker_or_wait() -> None:
    runner = _Runner()

    result = _coordinator(runner).execute_batch("sf_mutation_1", _batch())

    assert result.verdict is MutationVerdict.COMMITTED
    assert result.executed_sql[0] == "START TRANSACTION"
    assert result.executed_sql[-1] == "COMMIT"
    assert all("__sf_replication_marker" not in sql for sql in result.executed_sql)


def test_logged_execution_publishes_each_statement_before_the_next_step() -> None:
    runner = _Runner()
    logged: list[str] = []

    result = _coordinator(runner).execute_batch_logged(
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
    for role in COMPARISON_ROLES:
        runner.by_sql_role[(sql, role)] = NodeExecution.failure(
            role=role,
            status=ExecutionStatus.ERROR,
            started_ns=now,
            ended_ns=now,
            connection_id=100 + list(COMPARISON_ROLES).index(role),
            error=ErrorInfo(1062, "23000", "Duplicate entry '1' for key 'PRIMARY'"),
        )
    result = _coordinator(runner).execute_batch("sf_mutation_2", _batch(sql))

    assert result.verdict is MutationVerdict.CONSISTENT_ERROR_ROLLED_BACK
    assert result.executed_sql[-1] == "ROLLBACK"


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

    result = _coordinator(runner).execute_batch("sf_mutation_3", batch)

    assert result.verdict is MutationVerdict.MISMATCH
    assert result.terminates_round is True
    assert result.executed_sql[-1] == "ROLLBACK"


def test_successful_dml_with_actual_rows_outside_range_rolls_back_and_continues() -> None:
    result = _coordinator(_Runner(affected_rows=1)).execute_batch(
        "sf_mutation_actual_rows_1", _batch()
    )

    assert result.verdict is MutationVerdict.ACTUAL_ROWS_OUT_OF_RANGE_ROLLED_BACK
    assert result.actual_affected_rows == 1
    assert result.terminates_round is False
    assert result.executed_sql[-1] == "ROLLBACK"


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
    started = _coordinator(runner).execute_batch("sf_mutation_5", _batch())
    assert started.verdict is MutationVerdict.MISMATCH
    assert started.failing_sql == "START TRANSACTION"

    broken = PairMutationCoordinator(
        _nodes(),
        factory=_BrokenFactory(),
        runner=_Runner(),
        limits=QueryLimits(10, 1, 1024),
    ).execute_batch("sf_mutation_6", _batch())
    assert broken.verdict is MutationVerdict.INFRASTRUCTURE_ERROR
    assert set(broken.final_results) == set(COMPARISON_ROLES)
