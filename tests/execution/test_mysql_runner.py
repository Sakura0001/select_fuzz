from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Barrier, Event, Thread
import time
from typing import Any

import pytest

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.domain import ColumnMeta, ExecutionStatus
from select_fuzz.execution.mysql import (
    INTERNAL_RESULT_LIMIT_ERRNO,
    MySQLConnectorFactory,
    NodeQueryRunner,
)
from select_fuzz.execution.timeout import KillQueryWatchdog


class _DatabaseError(Exception):
    def __init__(self, errno: int, sqlstate: str, msg: str) -> None:
        self.errno = errno
        self.sqlstate = sqlstate
        self.msg = msg
        super().__init__(msg)


class _Cursor:
    def __init__(
        self,
        rows: tuple[tuple[object, ...], ...] = (),
        *,
        columns: tuple[ColumnMeta, ...] = (),
        warnings: tuple[str, ...] = (),
        fetch_error: Exception | None = None,
        on_warnings: Any | None = None,
        close_error: Exception | None = None,
        warnings_error: Exception | None = None,
    ) -> None:
        self._rows = rows
        self.columns = columns
        self._warnings = warnings
        self._fetch_error = fetch_error
        self._on_warnings = on_warnings
        self._close_error = close_error
        self._warnings_error = warnings_error
        self.offset = 0
        self.closed = False
        self.fetch_sizes: list[int] = []

    def fetchmany(self, size: int) -> tuple[tuple[object, ...], ...]:
        self.fetch_sizes.append(size)
        if self._fetch_error is not None:
            raise self._fetch_error
        rows = self._rows[self.offset : self.offset + size]
        self.offset += len(rows)
        return rows

    def warnings(self) -> tuple[str, ...]:
        if self._on_warnings is not None:
            self._on_warnings()
        if self._warnings_error is not None:
            raise self._warnings_error
        return self._warnings

    def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class _Session:
    def __init__(
        self,
        result: _Cursor | Exception,
        *,
        connection_id: int = 41,
        killed: Event | None = None,
    ) -> None:
        self.result = result
        self._connection_id = connection_id
        self._killed = killed
        self.closed = False
        self.aborted = False
        self.executed: list[str] = []

    def connection_id(self) -> int:
        return self._connection_id

    def execute(self, sql: str) -> _Cursor:
        self.executed.append(sql)
        if self._killed is not None:
            assert self._killed.wait(1), "watchdog did not issue KILL QUERY"
            raise _DatabaseError(1317, "42000", "Query execution was interrupted")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.aborted = True
        if self._killed is not None:
            self._killed.set()


class _ControlCursor(_Cursor):
    pass


class _ControlSession:
    def __init__(self, factory: _Factory) -> None:
        self.factory = factory

    def connection_id(self) -> int:
        return 9001

    def execute(self, sql: str) -> _ControlCursor:
        self.factory.kills.append(sql)
        self.factory.killed.set()
        return _ControlCursor()

    def close(self) -> None:
        return None

    def abort(self) -> None:
        return None


class _Factory:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.kills: list[str] = []
        self.killed = Event()
        self.query_context_entries = 0
        self.control_context_entries = 0

    @contextmanager
    def query_session(self, node: NodeConfig, database: str):  # type: ignore[no-untyped-def]
        self.query_context_entries += 1
        try:
            yield self.session
        finally:
            self.session.close()

    @contextmanager
    def control_session(self, node: NodeConfig, database: str):  # type: ignore[no-untyped-def]
        self.control_context_entries += 1
        yield _ControlSession(self)


@pytest.fixture
def node() -> NodeConfig:
    return NodeConfig(role=NodeRole.BASELINE, host="127.0.0.1", port=33061)


def _runner(factory: _Factory) -> NodeQueryRunner:
    return NodeQueryRunner(factory, watchdog=KillQueryWatchdog(factory))


def test_runner_streams_typed_rows_and_closes_owned_session(node: NodeConfig) -> None:
    columns = (ColumnMeta("x", 3, False, False, False),)
    cursor = _Cursor(((1,), (2,)), columns=columns, warnings=("note",))
    session = _Session(cursor)
    factory = _Factory(session)

    result = _runner(factory).run(
        node,
        "sf_case_1",
        "SELECT x FROM t ORDER BY 1",
        timeout_s=15,
        row_limit=10,
        byte_limit=1024,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert result.columns == columns
    assert result.rows == ((1,), (2,))
    assert result.warnings == ("note",)
    assert result.connection_id == 41
    assert session.closed is True
    assert cursor.closed is True
    assert factory.query_context_entries == 1
    assert max(cursor.fetch_sizes) == 1


def test_run_session_keeps_caller_owned_temporary_session_open(node: NodeConfig) -> None:
    cursor = _Cursor()
    session = _Session(cursor)
    factory = _Factory(session)

    result = _runner(factory).run_session(
        session,
        node,
        "sf_temp_1",
        "SELECT 1",
        timeout_s=15,
        row_limit=10,
        byte_limit=1024,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert session.closed is False
    assert cursor.closed is True
    assert factory.query_context_entries == 0


def test_statement_end_time_excludes_post_execution_warning_diagnostics(
    node: NodeConfig,
) -> None:
    now = [100]
    cursor = _Cursor(on_warnings=lambda: now.__setitem__(0, 10_000))
    factory = _Factory(_Session(cursor))
    runner = NodeQueryRunner(
        factory,
        watchdog=KillQueryWatchdog(factory),
        monotonic_ns=lambda: now[0],
    )

    result = runner.run_session(
        factory.session,
        node,
        "sf_case_1",
        "SELECT 1",
        timeout_s=15,
        row_limit=10,
        byte_limit=1024,
    )

    assert result.started_ns == 100
    assert result.ended_ns == 100


@pytest.mark.parametrize(
    ("rows", "row_limit", "byte_limit"),
    [
        (((1,), (2,), (3,)), 2, 1024),
        ((("12345",),), 10, 4),
    ],
)
def test_runner_aborts_before_retaining_results_past_hard_limits(
    node: NodeConfig,
    rows: tuple[tuple[object, ...], ...],
    row_limit: int,
    byte_limit: int,
) -> None:
    cursor = _Cursor(rows, columns=(ColumnMeta("x", 253, True, False, False),))
    factory = _Factory(_Session(cursor))

    result = _runner(factory).run(
        node,
        "sf_case_1",
        "SELECT x FROM t",
        timeout_s=15,
        row_limit=row_limit,
        byte_limit=byte_limit,
    )

    assert result.status is ExecutionStatus.ERROR
    assert result.error is not None
    assert result.error.errno == INTERNAL_RESULT_LIMIT_ERRNO
    assert result.rows == ()
    assert factory.kills == ["KILL QUERY 41"]
    assert cursor.closed is True
    assert result.connection_reusable is False
    assert max(cursor.fetch_sizes) == 1


def test_limit_cleanup_failure_marks_pinned_connection_unusable_immediately(
    node: NodeConfig,
) -> None:
    cursor = _Cursor(
        ((1,), (2,)),
        columns=(ColumnMeta("x", 3, False, False, False),),
        close_error=RuntimeError("Unread result found"),
    )
    factory = _Factory(_Session(cursor))

    result = _runner(factory).run_session(
        factory.session,
        node,
        "sf_temp_1",
        "SELECT x FROM temp_t",
        timeout_s=15,
        row_limit=1,
        byte_limit=1024,
    )

    assert result.status is ExecutionStatus.ERROR
    assert result.error is not None
    assert result.error.errno == INTERNAL_RESULT_LIMIT_ERRNO
    assert result.connection_reusable is False


class _AlreadyTimedOutHandle:
    timed_out = True
    kill_error_type = None

    def trigger(self, *, statement_token: object | None = None) -> None:
        return None

    def cancel(self, *, statement_token: object | None = None) -> None:
        return None


class _AlreadyTimedOutWatchdog:
    def arm(self, *args: object, **kwargs: object) -> _AlreadyTimedOutHandle:
        return _AlreadyTimedOutHandle()


def test_watchdog_timeout_takes_precedence_over_simultaneous_result_limit(
    node: NodeConfig,
) -> None:
    cursor = _Cursor(
        ((1,), (2,)),
        columns=(ColumnMeta("x", 3, False, False, False),),
    )
    factory = _Factory(_Session(cursor))
    runner = NodeQueryRunner(
        factory,
        watchdog=_AlreadyTimedOutWatchdog(),  # type: ignore[arg-type]
    )

    result = runner.run_session(
        factory.session,
        node,
        "sf_case_1",
        "SELECT x FROM t",
        timeout_s=15,
        row_limit=1,
        byte_limit=1024,
    )

    assert result.status is ExecutionStatus.TIMEOUT
    assert result.error is not None
    assert result.error.errno != INTERNAL_RESULT_LIMIT_ERRNO
    assert result.watchdog_fired is True
    assert result.connection_reusable is False


def test_watchdog_interruption_is_timeout(node: NodeConfig) -> None:
    session = _Session(_Cursor(), killed=Event())
    factory = _Factory(session)
    session._killed = factory.killed

    result = _runner(factory).run(
        node,
        "sf_case_1",
        "SELECT SLEEPY_CPU_WORK()",
        timeout_s=0.01,
        row_limit=10,
        byte_limit=1024,
    )

    assert result.status is ExecutionStatus.TIMEOUT
    assert result.error is not None
    assert result.error.errno == 1317
    assert result.watchdog_fired is True
    assert factory.kills == ["KILL QUERY 41"]
    assert result.connection_reusable is False


class _FailedKillControlSession(_ControlSession):
    def execute(self, sql: str) -> _ControlCursor:
        self.factory.kills.append(sql)
        raise _DatabaseError(2013, "HY000", "control connection lost")


class _FailedKillFactory(_Factory):
    @contextmanager
    def control_session(self, node: NodeConfig, database: str):  # type: ignore[no-untyped-def]
        self.control_context_entries += 1
        yield _FailedKillControlSession(self)


def test_failed_control_kill_falls_back_to_local_abort_and_returns_timeout(
    node: NodeConfig,
) -> None:
    wait_event = Event()
    session = _Session(_Cursor(), killed=wait_event)
    factory = _FailedKillFactory(session)

    result = _runner(factory).run_session(
        session,
        node,
        "sf_case_1",
        "SELECT CPU_HEAVY()",
        timeout_s=0.01,
        row_limit=10,
        byte_limit=1024,
    )

    assert result.status is ExecutionStatus.TIMEOUT
    assert session.aborted is True
    assert result.connection_reusable is False
    assert result.watchdog_error_type == "_DatabaseError"


def test_ordinary_interruption_without_watchdog_evidence_remains_error(
    node: NodeConfig,
) -> None:
    session = _Session(_DatabaseError(1317, "42000", "Query execution was interrupted"))
    factory = _Factory(session)

    result = _runner(factory).run(
        node,
        "sf_case_1",
        "SELECT x FROM t",
        timeout_s=15,
        row_limit=10,
        byte_limit=1024,
    )

    assert result.status is ExecutionStatus.ERROR
    assert result.watchdog_fired is False
    assert result.connection_reusable is True


def test_server_max_execution_time_error_is_timeout(node: NodeConfig) -> None:
    session = _Session(_DatabaseError(3024, "HY000", "maximum statement execution time exceeded"))
    factory = _Factory(session)

    result = _runner(factory).run(
        node,
        "sf_case_1",
        "SELECT x FROM t",
        timeout_s=15,
        row_limit=10,
        byte_limit=1024,
    )

    assert result.status is ExecutionStatus.TIMEOUT
    assert result.error is not None
    assert result.error.errno == 3024
    assert result.connection_reusable is True


@pytest.mark.parametrize("phase", ["execute", "fetch"])
@pytest.mark.parametrize("errno", [2013, 2014, 2020, 2027])
def test_connection_loss_is_infrastructure_and_invalidates_the_session(
    node: NodeConfig,
    phase: str,
    errno: int,
) -> None:
    lost = _DatabaseError(errno, "HY000", "client/protocol execution failure")
    result_source: _Cursor | Exception
    if phase == "execute":
        result_source = lost
    else:
        result_source = _Cursor(
            columns=(ColumnMeta("x", 3, False, False, False),),
            fetch_error=lost,
        )
    factory = _Factory(_Session(result_source))

    result = _runner(factory).run_session(
        factory.session,
        node,
        "sf_case_1",
        "SELECT x FROM t",
        timeout_s=15,
        row_limit=10,
        byte_limit=1024,
    )

    assert result.status is ExecutionStatus.INFRA_ERROR
    assert result.error is not None
    assert result.error.errno == errno
    assert result.connection_reusable is False


def test_warning_diagnostic_connection_loss_does_not_change_query_result_but_invalidates(
    node: NodeConfig,
) -> None:
    cursor = _Cursor(
        ((1,),),
        columns=(ColumnMeta("x", 3, False, False, False),),
        warnings_error=_DatabaseError(2013, "HY000", "lost during SHOW WARNINGS"),
    )
    factory = _Factory(_Session(cursor))

    result = _runner(factory).run_session(
        factory.session,
        node,
        "sf_case_1",
        "SELECT x FROM t",
        timeout_s=15,
        row_limit=10,
        byte_limit=1024,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert result.rows == ((1,),)
    assert result.warnings == ()
    assert result.connection_reusable is False


@dataclass
class _BrokenBarrier:
    message: str

    def wait(self, timeout: float | None = None) -> int:
        raise RuntimeError(self.message)


@dataclass
class _SlowBarrier:
    delay_s: float

    def wait(self, timeout: float | None = None) -> int:
        time.sleep(self.delay_s)
        return 0


def test_query_deadline_starts_after_the_synchronization_barrier(
    node: NodeConfig,
) -> None:
    factory = _Factory(_Session(_Cursor()))

    result = _runner(factory).run(
        node,
        "sf_case_1",
        "SELECT 1",
        timeout_s=0.01,
        row_limit=10,
        byte_limit=1024,
        barrier=_SlowBarrier(0.03),
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert factory.kills == []


def test_missing_barrier_participant_cannot_block_runners_forever(
    node: NodeConfig,
) -> None:
    barrier = Barrier(3)
    results = []

    def execute() -> None:
        factory = _Factory(_Session(_Cursor()))
        results.append(
            _runner(factory).run_session(
                factory.session,
                node,
                "sf_case_1",
                "SELECT 1",
                timeout_s=0.02,
                row_limit=10,
                byte_limit=1024,
                barrier=barrier,
            )
        )

    threads = [Thread(target=execute) for _ in range(2)]
    for thread in threads:
        thread.start()
    time.sleep(0.08)
    completed_before_external_abort = len(results)
    barrier.abort()
    for thread in threads:
        thread.join(1)

    assert completed_before_external_abort == 2
    assert all(result.status is ExecutionStatus.INFRA_ERROR for result in results)


def test_barrier_failure_is_sanitized_infrastructure_error(node: NodeConfig) -> None:
    factory = _Factory(_Session(_Cursor()))

    result = _runner(factory).run(
        node,
        "sf_case_1",
        "SELECT 1",
        timeout_s=15,
        row_limit=10,
        byte_limit=1024,
        barrier=_BrokenBarrier("secret should not be serialized"),
    )

    assert result.status is ExecutionStatus.INFRA_ERROR
    assert result.error is not None
    assert "secret" not in result.error.message


@pytest.mark.parametrize(
    ("timeout_s", "row_limit", "byte_limit"),
    [(0, 1, 1), (float("nan"), 1, 1), (1, 0, 1), (1, 1, 0), (True, 1, 1)],
)
def test_runner_rejects_invalid_resource_bounds_without_opening_a_connection(
    node: NodeConfig,
    timeout_s: Any,
    row_limit: int,
    byte_limit: int,
) -> None:
    factory = _Factory(_Session(_Cursor()))

    with pytest.raises((TypeError, ValueError)):
        _runner(factory).run(
            node,
            "sf_case_1",
            "SELECT 1",
            timeout_s=timeout_s,
            row_limit=row_limit,
            byte_limit=byte_limit,
        )

    assert factory.query_context_entries == 0


class _RawCursor:
    def __init__(
        self,
        *,
        rows: tuple[tuple[object, ...], ...] = (),
        description: tuple[tuple[object, ...], ...] = (),
        warning_count: int = 0,
    ) -> None:
        self._rows = list(rows)
        self.description = description
        self.warning_count = warning_count
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def fetchmany(self, size: int):  # type: ignore[no-untyped-def]
        rows = self._rows[:size]
        self._rows = self._rows[size:]
        return rows

    def fetchall(self):  # type: ignore[no-untyped-def]
        rows = tuple(self._rows)
        self._rows.clear()
        return rows

    def close(self) -> None:
        self.closed = True


class _RawConnection:
    connection_id = 41

    def __init__(self) -> None:
        self.query_cursor = _RawCursor(
            rows=((7,),),
            description=(("x", 3, None, None, None, None, 1, 32 | 2048, 63),),
            warning_count=1,
        )
        self.warning_cursor = _RawCursor(rows=(("Warning", 1265, "truncated"),))
        self.closed = False
        self.shutdown_called = False
        self.ping_calls: list[dict[str, object]] = []
        self.cursor_calls: list[dict[str, object]] = []

    def cursor(self, *, buffered: bool, raw: bool = False, **kwargs: object):  # type: ignore[no-untyped-def]
        self.cursor_calls.append({"buffered": buffered, "raw": raw, **kwargs})
        return self.warning_cursor if buffered else self.query_cursor

    def close(self) -> None:
        self.closed = True

    def shutdown(self) -> None:
        self.shutdown_called = True

    def ping(self, **kwargs: object) -> None:
        self.ping_calls.append(dict(kwargs))


def test_connector_adapter_preserves_flags_and_fetches_warnings_after_result(
    node: NodeConfig,
) -> None:
    connection = _RawConnection()
    connect_kwargs: dict[str, object] = {}

    def connect(**kwargs: object) -> _RawConnection:
        connect_kwargs.update(kwargs)
        return connection

    factory = MySQLConnectorFactory(
        environ={
            "SELECT_FUZZ_MYSQL_USER": "root",
            "SELECT_FUZZ_MYSQL_PASSWORD": "memory-only-secret",
        },
        connect=connect,
    )

    with factory.query_session(node, "sf_case_1") as session:
        cursor = session.execute("SELECT x FROM t")
        assert cursor.fetchmany(2) == ((7,),)
        assert cursor.fetchmany(2) == ()
        assert cursor.columns == (
            ColumnMeta(
                "x",
                3,
                True,
                True,
                True,
                character_set_id=63,
                flags=32 | 2048,
            ),
        )
        assert cursor.warnings() == ("Warning 1265: truncated",)
        cursor.close()

    assert connect_kwargs["get_warnings"] is False
    assert connect_kwargs["read_timeout"] == 310
    assert connection.warning_cursor.executed == ["SHOW WARNINGS"]
    assert connection.cursor_calls[-1]["read_timeout"] == 5
    assert connection.cursor_calls[-1]["write_timeout"] == 5
    assert connection.closed is True


def test_connector_read_timeout_must_leave_grace_beyond_product_deadline() -> None:
    with pytest.raises(ValueError, match="greater than 300"):
        MySQLConnectorFactory(read_timeout_s=300)


def test_connector_abort_shuts_down_socket_without_sending_quit(node: NodeConfig) -> None:
    connection = _RawConnection()
    factory = MySQLConnectorFactory(
        environ={
            "SELECT_FUZZ_MYSQL_USER": "root",
            "SELECT_FUZZ_MYSQL_PASSWORD": "memory-only-secret",
        },
        connect=lambda **kwargs: connection,
    )

    with factory.query_session(node, "sf_case_1") as session:
        session.abort()
        assert connection.shutdown_called is True
        assert connection.closed is False

    assert connection.closed is True


def test_connector_liveness_probe_never_reconnects_a_pinned_session(
    node: NodeConfig,
) -> None:
    connection = _RawConnection()
    factory = MySQLConnectorFactory(
        environ={
            "SELECT_FUZZ_MYSQL_USER": "root",
            "SELECT_FUZZ_MYSQL_PASSWORD": "memory-only-secret",
        },
        connect=lambda **kwargs: connection,
    )

    with factory.query_session(node, "sf_case_1") as session:
        assert session.is_alive() is True

    assert connection.ping_calls == [
        {"reconnect": False, "attempts": 1, "delay": 0}
    ]


def test_control_connections_use_a_short_independent_timeout(node: NodeConfig) -> None:
    connection = _RawConnection()
    connect_calls: list[dict[str, object]] = []

    def connect(**kwargs: object) -> _RawConnection:
        connect_calls.append(dict(kwargs))
        return connection

    factory = MySQLConnectorFactory(
        environ={
            "SELECT_FUZZ_MYSQL_USER": "root",
            "SELECT_FUZZ_MYSQL_PASSWORD": "memory-only-secret",
        },
        connect=connect,
    )

    with factory.control_session(node, "sf_case_1"):
        pass

    assert connect_calls[0]["connection_timeout"] == 5
    assert connect_calls[0]["read_timeout"] == 5
    assert connect_calls[0]["write_timeout"] == 5
