from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event, Thread

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.modes.fuzz.execution import StreamingQueryExecutor


class _Cursor:
    columns = ()
    affected_rows = None

    def __init__(self) -> None:
        self._batches = [((1,), (2,)), ((3,),), ()]

    def fetchmany(self, size: int):  # type: ignore[no-untyped-def]
        assert size == 128
        return self._batches.pop(0)

    def warnings(self):  # type: ignore[no-untyped-def]
        return ()

    def close(self) -> None:
        return None


class _Session:
    def connection_id(self) -> int:
        return 7

    def is_alive(self) -> bool:
        return True

    def execute(self, sql: str) -> _Cursor:
        assert sql == "SELECT value FROM t"
        return _Cursor()

    def abort(self) -> None:
        return None

    def close(self) -> None:
        return None


@dataclass
class _Factory:
    @contextmanager
    def query_session(self, node: NodeConfig, database: str):  # type: ignore[no-untyped-def]
        del node, database
        yield _Session()

    control_session = query_session


class _FailingOpenFactory(_Factory):
    @contextmanager
    def query_session(self, node: NodeConfig, database: str):  # type: ignore[no-untyped-def]
        del node, database
        raise _InternalConnectorError("cannot establish query connection")
        yield  # pragma: no cover


def test_streaming_executor_discards_values_and_counts_rows() -> None:
    node = NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1")
    result = StreamingQueryExecutor(_Factory()).execute(
        node,
        "sf_f_case",
        "SELECT value FROM t",
        timeout_seconds=5,
    )

    assert result.success
    assert result.rows_seen == 3
    assert result.error is None
    assert result.execute_elapsed_ns >= 0
    assert result.fetch_elapsed_ns >= 0


def test_streaming_executor_preserves_connection_open_failure_evidence() -> None:
    node = NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1")

    result = StreamingQueryExecutor(_FailingOpenFactory()).execute(
        node,
        "sf_f_case",
        "SELECT value FROM t",
        timeout_seconds=5,
    )

    assert result.success is False
    assert result.failure_evidence is not None
    assert result.failure_evidence["failure_stage"] == "connection_open"
    assert (
        result.failure_evidence["exception"]["message"]
        == "cannot establish query connection"
    )


class _Handle:
    def __init__(self) -> None:
        self.timed_out = False
        self.cancelled = 0
        self.triggered = 0

    def cancel(self, *, statement_token=None) -> None:  # type: ignore[no-untyped-def]
        del statement_token
        self.cancelled += 1

    def trigger(self, *, statement_token=None) -> None:  # type: ignore[no-untyped-def]
        del statement_token
        self.triggered += 1


class _Watchdog:
    def __init__(self) -> None:
        self.handle = _Handle()
        self.arm_calls: list[tuple[NodeConfig, str, int, float]] = []

    def arm(
        self,
        node: NodeConfig,
        database: str,
        connection_id: int,
        timeout_s: float,
        **kwargs: object,
    ) -> _Handle:
        assert kwargs["fallback_abort"] is not None
        assert isinstance(kwargs["statement_done"], Event)
        self.arm_calls.append((node, database, connection_id, timeout_s))
        return self.handle


class _TimeoutAfterCancelHandle(_Handle):
    def cancel(self, *, statement_token=None) -> None:  # type: ignore[no-untyped-def]
        super().cancel(statement_token=statement_token)
        self.timed_out = True


class _TimeoutAfterCancelWatchdog(_Watchdog):
    def __init__(self) -> None:
        super().__init__()
        self.handle = _TimeoutAfterCancelHandle()


def test_streaming_session_arms_deadline_and_stop_triggers_active_query() -> None:
    node = NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1")
    watchdog = _Watchdog()
    executor = StreamingQueryExecutor(_Factory(), watchdog=watchdog)  # type: ignore[arg-type]
    session = _Session()

    result = executor.execute_session(
        session,
        "SELECT value FROM t",
        node=node,
        database="sf_f_case",
        timeout_seconds=5,
    )

    assert result.success is True
    assert watchdog.arm_calls == [(node, "sf_f_case", 7, 5.0)]
    assert watchdog.handle.cancelled == 1
    assert executor.active_queries == 0

    blocking = _Handle()
    executor._register_active_for_test(blocking, object())  # type: ignore[attr-defined]
    executor.stop_active()
    assert blocking.triggered == 1


def test_streaming_session_records_watchdog_timeout_without_connector_exception() -> None:
    node = NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1")

    result = StreamingQueryExecutor(  # type: ignore[arg-type]
        _Factory(),
        watchdog=_TimeoutAfterCancelWatchdog(),
    ).execute_session(
        _Session(),
        "SELECT value FROM t",
        node=node,
        database="sf_f_case",
        timeout_seconds=5,
    )

    assert result.success is False
    assert result.timed_out is True
    assert result.error == "query_timeout"
    assert result.failure_evidence is not None
    assert result.failure_evidence["failure_stage"] == "watchdog_timeout"
    assert result.failure_evidence["watchdog"]["timed_out"] is True


class _Interrupted(Exception):
    errno = 1317
    sqlstate = "42000"


class _InternalConnectorError(RuntimeError):
    errno = -1
    sqlstate = "HY000"


class _FailingExecuteSession(_Session):
    def execute(self, sql: str) -> _Cursor:
        raise _InternalConnectorError("Unread result found")


class _FailingFetchCursor(_Cursor):
    def fetchmany(self, size: int):  # type: ignore[no-untyped-def]
        del size
        raise _InternalConnectorError("fetch protocol state is invalid")


class _FailingFetchSession(_Session):
    def execute(self, sql: str) -> _FailingFetchCursor:
        assert sql == "SELECT value FROM t"
        return _FailingFetchCursor()


class _FailingCloseCursor(_Cursor):
    def fetchmany(self, size: int):  # type: ignore[no-untyped-def]
        del size
        return ()

    def close(self) -> None:
        raise _InternalConnectorError("cursor close found unread result")


class _FailingCloseSession(_Session):
    def execute(self, sql: str) -> _FailingCloseCursor:
        assert sql == "SELECT value FROM t"
        return _FailingCloseCursor()


def test_streaming_session_preserves_execute_exception_evidence() -> None:
    node = NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1")
    watchdog = _Watchdog()

    result = StreamingQueryExecutor(  # type: ignore[arg-type]
        _Factory(),
        watchdog=watchdog,
    ).execute_session(
        _FailingExecuteSession(),
        "SELECT value FROM t",
        node=node,
        database="sf_f_case",
        timeout_seconds=5,
    )

    assert result.success is False
    assert result.error == "_InternalConnectorError:errno=-1:sqlstate=HY000"
    assert result.failure_evidence is not None
    assert result.failure_evidence["failure_stage"] == "execute"
    assert result.failure_evidence["connection_id"] == 7
    assert result.failure_evidence["exception"]["message"] == "Unread result found"
    assert result.failure_evidence["watchdog"]["timed_out"] is False


def test_streaming_session_preserves_fetch_exception_evidence() -> None:
    node = NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1")

    result = StreamingQueryExecutor(  # type: ignore[arg-type]
        _Factory(),
        watchdog=_Watchdog(),
    ).execute_session(
        _FailingFetchSession(),
        "SELECT value FROM t",
        node=node,
        database="sf_f_case",
        timeout_seconds=5,
    )

    assert result.success is False
    assert result.failure_evidence is not None
    assert result.failure_evidence["failure_stage"] == "fetch"
    assert (
        result.failure_evidence["exception"]["message"]
        == "fetch protocol state is invalid"
    )
    assert result.failure_evidence["timings"]["fetch_elapsed_ns"] >= 0
    assert result.failure_evidence["timings"]["total_elapsed_ns"] >= 0


def test_streaming_session_turns_cursor_close_failure_into_evidence() -> None:
    node = NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1")

    result = StreamingQueryExecutor(  # type: ignore[arg-type]
        _Factory(),
        watchdog=_Watchdog(),
    ).execute_session(
        _FailingCloseSession(),
        "SELECT value FROM t",
        node=node,
        database="sf_f_case",
        timeout_seconds=5,
    )

    assert result.success is False
    assert result.error == "_InternalConnectorError:errno=-1:sqlstate=HY000"
    assert result.failure_evidence is not None
    assert result.failure_evidence["failure_stage"] == "cursor_close"
    assert (
        result.failure_evidence["exception"]["message"]
        == "cursor close found unread result"
    )
    assert result.failure_evidence["timings"]["cursor_close_elapsed_ns"] >= 0


class _BlockingSession:
    def __init__(self, query_started: Event, killed: Event) -> None:
        self.query_started = query_started
        self.killed = killed
        self.aborted = False

    def connection_id(self) -> int:
        return 88

    def execute(self, sql: str) -> _Cursor:
        assert sql == "SELECT value FROM t"
        self.query_started.set()
        assert self.killed.wait(2)
        raise _Interrupted("query interrupted")

    def abort(self) -> None:
        self.aborted = True
        self.killed.set()

    def close(self) -> None:
        return None


class _KillSession:
    def __init__(self, killed: Event) -> None:
        self.killed = killed

    def execute(self, sql: str) -> "_KillCursor":
        assert sql == "KILL QUERY 88"
        self.killed.set()
        return _KillCursor()

    def connection_id(self) -> int:
        return 999

    def abort(self) -> None:
        return None

    def close(self) -> None:
        return None


class _KillCursor:
    columns = ()
    affected_rows = 0

    def fetchmany(self, size: int):  # type: ignore[no-untyped-def]
        del size
        return ()

    def close(self) -> None:
        return None


class _BlockingFactory:
    def __init__(self) -> None:
        self.query_started = Event()
        self.killed = Event()
        self.session = _BlockingSession(self.query_started, self.killed)

    @contextmanager
    def query_session(self, node, database):  # type: ignore[no-untyped-def]
        del node, database
        yield self.session

    @contextmanager
    def control_session(self, node, database):  # type: ignore[no-untyped-def]
        del node, database
        yield _KillSession(self.killed)


def test_wall_clock_watchdog_kills_blocking_statement_and_reuses_connection() -> None:
    node = NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1")
    factory = _BlockingFactory()
    executor = StreamingQueryExecutor(factory)

    result = executor.execute_session(
        factory.session,
        "SELECT value FROM t",
        node=node,
        database="sf_f_case",
        timeout_seconds=0.01,
    )

    assert result.success is False
    assert result.timed_out is True
    assert result.stopped is False
    assert result.error == "query_timeout"
    assert result.connection_lost is False
    assert executor.active_queries == 0


def test_stop_active_interrupts_blocking_statement_without_waiting_for_deadline() -> None:
    node = NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1")
    factory = _BlockingFactory()
    executor = StreamingQueryExecutor(factory)
    results = []

    thread = Thread(
        target=lambda: results.append(
            executor.execute_session(
                factory.session,
                "SELECT value FROM t",
                node=node,
                database="sf_f_case",
                timeout_seconds=10,
            )
        )
    )
    thread.start()
    assert factory.query_started.wait(1)

    executor.stop_active()
    thread.join(1)

    assert thread.is_alive() is False
    assert len(results) == 1
    assert results[0].stopped is True
    assert results[0].timed_out is False
    assert results[0].error == "query_stopped"
