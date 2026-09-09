"""PQ workload admission must precede execution on the very same session."""

from contextlib import contextmanager

import pytest

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.domain import ColumnMeta, ExecutionStatus
from select_fuzz.execution.mysql import NodeQueryRunner
from select_fuzz.execution.pq_gate import PQAdmissionRejected, admit_pq
from select_fuzz.modes.fuzz.execution import StreamingQueryExecutor


SQL = "SELECT SUM(v / 3) FROM t"
PQ_TREE = "Gather: 4 workers, parallel scan on t"


class Cursor:
    affected_rows = None

    def __init__(self, rows=(), names=(), warnings=()):
        self.rows = list(rows)
        self.columns = tuple(ColumnMeta(n, 253, True, False, False) for n in names)
        self._warnings = warnings
        self.closed = False

    def fetchmany(self, size):
        batch, self.rows = self.rows[:size], self.rows[size:]
        return tuple(batch)

    def warnings(self):
        if isinstance(self._warnings, Exception):
            raise self._warnings
        return self._warnings

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *, plan=PQ_TREE, warnings=(), actual_tree=PQ_TREE):
        self.plan = plan
        self.result_warnings = warnings
        self.actual_tree = actual_tree
        self.sql = []
        self.cursors = []

    def connection_id(self):
        return 7

    def is_alive(self):
        return True

    def execute(self, sql):
        self.sql.append(sql)
        if sql.startswith("SET "):
            cursor = Cursor()
        elif sql.startswith("EXPLAIN ANALYZE"):
            cursor = Cursor(((self.actual_tree,),), ("EXPLAIN",), self.result_warnings)
        elif sql.startswith("EXPLAIN "):
            cursor = Cursor(((self.plan,),), ("EXPLAIN",))
        else:
            cursor = Cursor(((3,),), ("value",), self.result_warnings)
        self.cursors.append(cursor)
        return cursor

    def abort(self):
        pass

    def close(self):
        pass


class Factory:
    def __init__(self, session):
        self.session = session

    @contextmanager
    def query_session(self, node, database):
        yield self.session

    control_session = query_session


class Handle:
    timed_out = False
    kill_error_type = None

    def cancel(self, **kwargs):
        pass

    def trigger(self, **kwargs):
        pass


class Watchdog:
    def arm(self, *args, **kwargs):
        return Handle()


def run_node(session, *, role=NodeRole.CUSTOM_ON, sql=SQL):
    return NodeQueryRunner(Factory(session), watchdog=Watchdog(), require_pq=True).run(
        NodeConfig(role=role, host="127.0.0.1"),
        "pq_test",
        sql,
        timeout_s=2,
        row_limit=100,
        byte_limit=10000,
    )


def test_serial_plan_never_executes_pq_workload():
    session = Session(plan="Table scan on t")
    result = run_node(session)
    assert result.status is ExecutionStatus.ERROR
    assert result.error.errno == 65004
    assert SQL not in session.sql
    assert result.performance_payload["pq_rejection"] == "not_pq"
    assert all(c.closed for c in session.cursors)


@pytest.mark.parametrize("plan", [PQ_TREE, "Table scan on t"])
def test_pq_admission_uses_preconfigured_parameters_without_any_set(plan):
    session = Session(plan=plan)
    if plan == PQ_TREE:
        assert admit_pq(session, SQL)["pq_plan"]["triggered"]
    else:
        with pytest.raises(PQAdmissionRejected, match="not_pq"):
            admit_pq(session, SQL)
    assert session.sql == ["EXPLAIN " + SQL]


@pytest.mark.parametrize(
    "sql",
    [
        "/* replay case */ " + SQL,
        "-- replay case\n" + SQL,
        "(" + SQL + ")",
    ],
)
def test_comments_and_parentheses_cannot_bypass_workload_gate(sql):
    session = Session(plan="Table scan on t")
    result = run_node(session, sql=sql)
    assert result.error is not None and result.error.errno == 65004
    assert sql not in session.sql


def test_tree_predicate_string_is_not_a_parallel_operator():
    session = Session(plan="-> Filter: (t.s1 = 'Gather: 4 workers')\n    -> Table scan on t")
    result = run_node(session)
    assert result.error is not None and result.error.errno == 65004
    assert SQL not in session.sql


@pytest.mark.parametrize("streaming", [False, True])
def test_admission_timeout_never_dispatches_the_workload(streaming):
    handle = Handle()

    class TimedPlan(Session):
        def execute(self, sql):
            cursor = super().execute(sql)
            if sql.startswith("EXPLAIN "):
                handle.timed_out = True
            return cursor

    class DeadlineWatchdog:
        def arm(self, *args, **kwargs):
            return handle

    session = TimedPlan()
    node = NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1")
    if streaming:
        result = StreamingQueryExecutor(
            Factory(session), watchdog=DeadlineWatchdog()
        ).execute_session(
            session,
            SQL,
            node=node,
            database="pq_test",
            timeout_seconds=2,
            require_pq=True,
        )
        assert result.timed_out
        assert result.failure_evidence["pq_rejection"] == "admission_timeout"
    else:
        result = NodeQueryRunner(
            Factory(session), watchdog=DeadlineWatchdog(), require_pq=True
        ).run(
            node,
            "pq_test",
            SQL,
            timeout_s=2,
            row_limit=100,
            byte_limit=10000,
        )
        assert result.status is ExecutionStatus.TIMEOUT
        assert result.performance_payload["pq_rejection"] == "admission_timeout"
    assert SQL not in session.sql


@pytest.mark.parametrize("streaming", [False, True])
def test_plan_cleanup_failure_preserves_budget_rejection(streaming):
    class CleanupError(RuntimeError):
        errno = 2013
        sqlstate = "HY000"
        msg = "lost connection during cleanup"

    class UnreadPlan(Cursor):
        def close(self):
            raise CleanupError()

    class OversizedPlan(Session):
        def execute(self, sql):
            if sql.startswith("EXPLAIN "):
                self.sql.append(sql)
                return UnreadPlan((("x" * (2 * 1024 * 1024 + 1),),), ("EXPLAIN",))
            return super().execute(sql)

        def abort(self):
            raise CleanupError()

    session = OversizedPlan()
    if streaming:
        result = StreamingQueryExecutor(Factory(session)).execute_session(
            session,
            SQL,
            require_pq=True,
        )
        assert result.errno == 65004 and result.connection_lost
        evidence = result.failure_evidence
    else:
        result = run_node(session)
        assert result.error.errno == 65004 and not result.connection_reusable
        evidence = result.performance_payload
    assert evidence["pq_rejection"] == "plan_budget_exceeded"
    assert "pq_abort_error" in evidence and "pq_cleanup_error" in evidence
    assert SQL not in session.sql


def test_traditional_table_alias_is_not_a_parallel_operator():
    from select_fuzz.pq.detector import parse_explain

    info = parse_explain((("<gather1>", "ALL", "Using where"),), ("table", "type", "Extra"))
    assert not info.triggered


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("stage", ["EXPLAIN ", "SELECT "])
def test_server_deadline_preserves_admission_or_workload_stage(streaming, stage):
    class ServerTimeout(RuntimeError):
        errno = 3024
        sqlstate = "HY000"
        msg = "query execution was interrupted, maximum statement execution time exceeded"

    class TimedSession(Session):
        def execute(self, sql):
            if sql.startswith(stage):
                self.sql.append(sql)
                raise ServerTimeout()
            return super().execute(sql)

    session = TimedSession()
    if streaming:
        result = StreamingQueryExecutor(Factory(session)).execute_session(
            session,
            SQL,
            require_pq=True,
        )
        assert not result.success and result.errno == 3024
        evidence = result.failure_evidence or {}
    else:
        result = run_node(session)
        assert result.status is ExecutionStatus.TIMEOUT and result.error.errno == 3024
        evidence = result.performance_payload or {}
    if stage == "SELECT ":
        assert "pq_rejection" not in evidence
    else:
        assert evidence["pq_rejection"] == "admission_timeout"
        assert SQL not in session.sql


def test_measurement_barrier_is_after_pq_admission():
    session = Session()

    class Barrier:
        def wait(self, timeout=None):
            assert "EXPLAIN " + SQL in session.sql, (
                "PQ preparation must finish before synchronization"
            )

    result = NodeQueryRunner(Factory(session), watchdog=Watchdog(), require_pq=True).run(
        NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1"),
        "pq_test",
        SQL,
        timeout_s=2,
        row_limit=100,
        byte_limit=10000,
        barrier=Barrier(),
    )
    assert result.status is ExecutionStatus.SUCCESS


def test_rejected_pq_plan_aborts_waiting_reference_arms():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    sessions = {role: Session(plan="Table scan on t") for role in NodeRole}
    barrier = Barrier(3)

    def run(role):
        return NodeQueryRunner(Factory(sessions[role]), watchdog=Watchdog(), require_pq=True).run(
            NodeConfig(role=role, host="127.0.0.1"),
            "pq_test",
            SQL,
            timeout_s=2,
            row_limit=100,
            byte_limit=10000,
            barrier=barrier,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, NodeRole))
    assert results[-1].error.errno == 65004
    assert all(SQL not in session.sql for session in sessions.values())


def test_confirmed_plan_executes_and_retains_plan_evidence():
    session = Session()
    result = run_node(session)
    assert result.status is ExecutionStatus.SUCCESS
    assert session.sql.index("EXPLAIN " + SQL) < session.sql.index(SQL)
    assert result.performance_payload["pq_plan"]["triggered"] is True
    assert result.performance_payload["pq_plan"]["dop"] == 4
    assert all(c.closed for c in session.cursors)


@pytest.mark.parametrize("role", [NodeRole.BASELINE, NodeRole.CUSTOM_OFF])
def test_intentional_reference_arms_remain_serial(role):
    session = Session(plan="Table scan on t")
    result = run_node(session, role=role)
    assert result.status is ExecutionStatus.SUCCESS
    assert not any(s.startswith("EXPLAIN ") for s in session.sql)


@pytest.mark.parametrize(
    "warnings, reason",
    [
        (("Warning 999: fallback to non-PQ serial execution",), "runtime_fallback"),
        (RuntimeError("warnings unavailable"), "warnings_unavailable"),
    ],
)
def test_fallback_or_missing_warnings_cannot_become_success(warnings, reason):
    result = run_node(Session(warnings=warnings))
    assert result.status is ExecutionStatus.ERROR
    assert result.error.errno == 65004
    assert result.performance_payload["pq_rejection"] == reason
    assert not result.rows


def test_analyze_rechecks_actual_tree_after_qualifying_plan():
    session = Session(actual_tree="Table scan on t (actual time=1..2 rows=1 loops=1)")
    result = run_node(session, sql="EXPLAIN ANALYZE FORMAT=TREE " + SQL)
    assert result.error.errno == 65004
    assert result.performance_payload["pq_rejection"] == "runtime_plan_not_pq"
    assert "EXPLAIN FORMAT=TREE " + SQL in session.sql


@pytest.mark.parametrize("plan, admitted", [(PQ_TREE, True), ("Table scan on t", False)])
def test_streaming_reader_uses_same_session_plan_gate(plan, admitted):
    session = Session(plan=plan)
    result = StreamingQueryExecutor(Factory(session)).execute_session(
        session,
        SQL,
        require_pq=True,
    )
    assert result.success is admitted
    assert (SQL in session.sql) is admitted
    if not admitted:
        assert result.errno == 65004
        assert result.error_stage == "pq_admission"
    assert all(c.closed for c in session.cursors)


def test_streaming_fallback_is_excluded_after_full_fetch():
    session = Session(warnings=("Warning 999: retry without parallel query",))
    result = StreamingQueryExecutor(Factory(session)).execute_session(
        session,
        SQL,
        require_pq=True,
    )
    assert not result.success and result.errno == 65004
    assert result.error_stage == "pq_evidence"
