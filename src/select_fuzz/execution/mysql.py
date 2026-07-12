"""Bounded MySQL query execution and the production connector adapter."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence, Set
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import math
from threading import Event
import time as time_module
from typing import Any

import mysql.connector
from mysql.connector.constants import FieldFlag

from select_fuzz.config import (
    MAX_STATEMENT_TIMEOUT_SECONDS,
    NodeConfig,
    resolve_credentials,
)
from select_fuzz.domain import ColumnMeta, ErrorInfo, ExecutionStatus, NodeExecution
from select_fuzz.execution.protocols import (
    BarrierLike,
    ConnectionFactory,
    CursorLike,
    QuerySession,
)
from select_fuzz.execution.timeout import KillQueryWatchdog


INTERNAL_RESULT_LIMIT_ERRNO = 65001
INTERNAL_RUNNER_ERRNO = 65002
INTERNAL_WATCHDOG_TIMEOUT_ERRNO = 65003
_INTERNAL_SQLSTATE = "HY000"
_TIMEOUT_SQLSTATE = "HYT00"
_MYSQL_CLIENT_ERROR_RANGE = range(2000, 3000)


class _ResultLimitExceeded(RuntimeError):
    pass


def _positive_bound(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _validate_execution_inputs(
    database: str,
    sql: str,
    timeout_s: object,
    row_limit: object,
    byte_limit: object,
) -> None:
    if not isinstance(database, str) or not database:
        raise ValueError("database must not be empty")
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("sql must not be empty")
    if (
        not isinstance(timeout_s, (int, float))
        or isinstance(timeout_s, bool)
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
    ):
        raise ValueError("timeout_s must be a finite positive number")
    _positive_bound(row_limit, "row_limit")
    _positive_bound(byte_limit, "byte_limit")


def _cell_wire_size(value: object) -> int:
    if value is None:
        return 1
    if isinstance(value, bool):
        return 1
    if isinstance(value, bytes):
        return len(value) + 1
    if isinstance(value, (bytearray, memoryview)):
        return len(value) + 1
    if isinstance(value, str):
        return len(value.encode("utf-8")) + 1
    if isinstance(value, (int, float, Decimal)):
        return len(str(value).encode("ascii")) + 1
    if isinstance(value, (date, datetime, time, timedelta)):
        return len(str(value).encode("ascii")) + 1
    if isinstance(value, Mapping):
        return 2 + sum(
            _cell_wire_size(key) + _cell_wire_size(child)
            for key, child in value.items()
        )
    if isinstance(value, (tuple, list, Set)):
        return 2 + sum(_cell_wire_size(child) for child in value)
    raise TypeError(f"unsupported MySQL wire value type: {type(value).__qualname__}")


def _row_wire_size(row: Sequence[object]) -> int:
    return 1 + sum(_cell_wire_size(value) for value in row)


def _internal_error(message: str, *, errno: int = INTERNAL_RUNNER_ERRNO) -> ErrorInfo:
    return ErrorInfo(errno, _INTERNAL_SQLSTATE, message)


def _database_error(error: Exception) -> ErrorInfo | None:
    errno = getattr(error, "errno", None)
    sqlstate = getattr(error, "sqlstate", None)
    message = getattr(error, "msg", None)
    if (
        isinstance(errno, int)
        and not isinstance(errno, bool)
        and 0 <= errno <= 0xFFFF
        and isinstance(sqlstate, str)
        and isinstance(message, str)
    ):
        try:
            return ErrorInfo(errno, sqlstate, message)
        except (TypeError, ValueError):
            return None
    return None


class NodeQueryRunner:
    """Execute one statement, stream bounded rows, and return only typed outcomes."""

    def __init__(
        self,
        factory: ConnectionFactory,
        *,
        watchdog: KillQueryWatchdog | None = None,
        monotonic_ns: Callable[[], int] = time_module.monotonic_ns,
    ) -> None:
        self._factory = factory
        self._watchdog = watchdog or KillQueryWatchdog(factory)
        self._monotonic_ns = monotonic_ns

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
    ) -> NodeExecution:
        _validate_execution_inputs(database, sql, timeout_s, row_limit, byte_limit)
        started_ns = self._monotonic_ns()
        try:
            with self._factory.query_session(node, database) as session:
                return self.run_session(
                    session,
                    node,
                    database,
                    sql,
                    timeout_s=timeout_s,
                    row_limit=row_limit,
                    byte_limit=byte_limit,
                    barrier=barrier,
                )
        except Exception as error:
            ended_ns = self._monotonic_ns()
            return NodeExecution.failure(
                role=node.role,
                status=ExecutionStatus.INFRA_ERROR,
                started_ns=started_ns,
                ended_ns=max(started_ns, ended_ns),
                connection_id=None,
                error=_internal_error(
                    f"query session lifecycle failed: {type(error).__name__}"
                ),
                connection_reusable=False,
            )

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
    ) -> NodeExecution:
        """Use a pinned caller-owned session without closing or returning it."""

        _validate_execution_inputs(database, sql, timeout_s, row_limit, byte_limit)
        initial_ns = self._monotonic_ns()
        try:
            connection_id = session.connection_id()
            if (
                not isinstance(connection_id, int)
                or isinstance(connection_id, bool)
                or connection_id <= 0
            ):
                raise ValueError("connector returned an invalid connection ID")
        except Exception as error:
            ended_ns = self._monotonic_ns()
            return NodeExecution.failure(
                role=node.role,
                status=ExecutionStatus.INFRA_ERROR,
                started_ns=initial_ns,
                ended_ns=max(initial_ns, ended_ns),
                connection_id=None,
                error=_internal_error(
                    f"connection identity lookup failed: {type(error).__name__}"
                ),
                connection_reusable=False,
            )

        if barrier is not None:
            try:
                barrier.wait(timeout=float(timeout_s))
            except Exception as error:
                ended_ns = self._monotonic_ns()
                return NodeExecution.failure(
                    role=node.role,
                    status=ExecutionStatus.INFRA_ERROR,
                    started_ns=initial_ns,
                    ended_ns=max(initial_ns, ended_ns),
                    connection_id=connection_id,
                    error=_internal_error(f"start barrier failed: {type(error).__name__}"),
                )

        statement_token = object()
        statement_done = Event()
        try:
            handle = self._watchdog.arm(
                node,
                database,
                connection_id,
                timeout_s,
                statement_token=statement_token,
                fallback_abort=session.abort,
                statement_done=statement_done,
            )
        except Exception as error:
            ended_ns = self._monotonic_ns()
            return NodeExecution.failure(
                role=node.role,
                status=ExecutionStatus.INFRA_ERROR,
                started_ns=initial_ns,
                ended_ns=max(initial_ns, ended_ns),
                connection_id=connection_id,
                error=_internal_error(f"watchdog arm failed: {type(error).__name__}"),
            )

        started_ns = self._monotonic_ns()
        cursor: CursorLike | None = None
        rows: list[tuple[object, ...]] = []
        columns: tuple[ColumnMeta, ...] = ()
        warnings: tuple[str, ...] = ()
        status = ExecutionStatus.SUCCESS
        error_info: ErrorInfo | None = None
        cleanup_error: Exception | None = None
        statement_ended_ns: int | None = None
        connection_reusable = True
        try:
            cursor = session.execute(sql)
            columns = cursor.columns
            retained_bytes = 0
            while True:
                batch = cursor.fetchmany(1)
                if not batch:
                    break
                for raw_row in batch:
                    row = tuple(raw_row)
                    if len(row) != len(columns):
                        raise TypeError("cursor row width differs from column metadata")
                    if len(rows) >= row_limit:
                        raise _ResultLimitExceeded("result row limit exceeded")
                    row_bytes = _row_wire_size(row)
                    if retained_bytes + row_bytes > byte_limit:
                        raise _ResultLimitExceeded("result byte limit exceeded")
                    rows.append(row)
                    retained_bytes += row_bytes
            statement_ended_ns = self._monotonic_ns()
            statement_done.set()
            handle.cancel(statement_token=statement_token)
            try:
                warnings = cursor.warnings()
            except Exception:
                warnings = ()
                connection_reusable = False
        except _ResultLimitExceeded as limit_error:
            handle.trigger(statement_token=statement_token)
            statement_done.set()
            statement_ended_ns = self._monotonic_ns()
            connection_reusable = False
            if handle.timed_out:
                status = ExecutionStatus.TIMEOUT
                error_info = ErrorInfo(
                    INTERNAL_WATCHDOG_TIMEOUT_ERRNO,
                    _TIMEOUT_SQLSTATE,
                    "query exceeded watchdog deadline",
                )
            else:
                status = ExecutionStatus.ERROR
                error_info = _internal_error(
                    str(limit_error), errno=INTERNAL_RESULT_LIMIT_ERRNO
                )
            rows.clear()
            columns = ()
        except Exception as execution_error:
            statement_done.set()
            statement_ended_ns = self._monotonic_ns()
            error_info = _database_error(execution_error)
            if error_info is None:
                status = (
                    ExecutionStatus.TIMEOUT
                    if handle.timed_out
                    else ExecutionStatus.INFRA_ERROR
                )
                connection_reusable = False
                error_info = (
                    ErrorInfo(
                        INTERNAL_WATCHDOG_TIMEOUT_ERRNO,
                        _TIMEOUT_SQLSTATE,
                        "query exceeded watchdog deadline",
                    )
                    if handle.timed_out
                    else _internal_error(
                        f"query execution failed: {type(execution_error).__name__}"
                    )
                )
            elif error_info.errno == 3024 or (
                handle.timed_out
                and (
                    error_info.errno == 1317
                    or error_info.errno in _MYSQL_CLIENT_ERROR_RANGE
                )
            ):
                status = ExecutionStatus.TIMEOUT
                connection_reusable = error_info.errno == 3024
            elif error_info.errno in _MYSQL_CLIENT_ERROR_RANGE:
                status = ExecutionStatus.INFRA_ERROR
                connection_reusable = False
            else:
                status = ExecutionStatus.ERROR
            rows.clear()
            columns = ()
        finally:
            statement_done.set()
            handle.cancel(statement_token=statement_token)
            if cursor is not None:
                try:
                    cursor.close()
                except Exception as close_error:
                    cleanup_error = close_error

        ended_ns = (
            self._monotonic_ns()
            if statement_ended_ns is None
            else statement_ended_ns
        )
        if cleanup_error is not None and status is ExecutionStatus.SUCCESS:
            status = ExecutionStatus.INFRA_ERROR
            error_info = _internal_error(
                f"cursor cleanup failed: {type(cleanup_error).__name__}"
            )
            rows.clear()
            columns = ()
            connection_reusable = False
        elif cleanup_error is not None:
            connection_reusable = False
        elif handle.timed_out and status is ExecutionStatus.SUCCESS:
            status = ExecutionStatus.TIMEOUT
            error_info = ErrorInfo(
                INTERNAL_WATCHDOG_TIMEOUT_ERRNO,
                _TIMEOUT_SQLSTATE,
                "query exceeded watchdog deadline",
            )
            rows.clear()
            columns = ()
            connection_reusable = False

        if status is ExecutionStatus.SUCCESS:
            return NodeExecution.success(
                role=node.role,
                connection_id=connection_id,
                started_ns=started_ns,
                ended_ns=max(started_ns, ended_ns),
                columns=columns,
                rows=tuple(rows),
                warnings=warnings,
                connection_reusable=connection_reusable,
            )
        assert error_info is not None
        return NodeExecution.failure(
            role=node.role,
            status=status,
            started_ns=started_ns,
            ended_ns=max(started_ns, ended_ns),
            connection_id=connection_id,
            error=error_info,
            warnings=warnings,
            watchdog_fired=handle.timed_out,
            watchdog_error_type=handle.kill_error_type,
            connection_reusable=connection_reusable,
        )


class _ConnectorCursor:
    def __init__(self, cursor: Any, connection: Any, diagnostic_timeout_s: int) -> None:
        self._cursor = cursor
        self._connection = connection
        self._diagnostic_timeout_s = diagnostic_timeout_s
        description = cursor.description or ()
        self._columns = tuple(self._column_meta(item) for item in description)

    @staticmethod
    def _column_meta(item: Sequence[object]) -> ColumnMeta:
        if len(item) < 7:
            raise ValueError("connector returned incomplete column metadata")
        name = item[0]
        type_code = item[1]
        nullable = item[6]
        flags = item[7] if len(item) > 7 else 0
        character_set_id = item[8] if len(item) > 8 else None
        if not isinstance(name, str) or not isinstance(type_code, int):
            raise TypeError("connector returned invalid column metadata")
        flags_value = flags if isinstance(flags, int) and not isinstance(flags, bool) else 0
        charset_value = (
            character_set_id
            if isinstance(character_set_id, int)
            and not isinstance(character_set_id, bool)
            else None
        )
        return ColumnMeta(
            name=name,
            type_code=type_code,
            nullable=bool(nullable),
            unsigned=bool(flags_value & FieldFlag.UNSIGNED),
            binary=bool(flags_value & FieldFlag.BINARY) or charset_value == 63,
            character_set_id=charset_value,
            flags=flags_value,
        )

    @property
    def columns(self) -> tuple[ColumnMeta, ...]:
        return self._columns

    def fetchmany(self, size: int) -> tuple[tuple[object, ...], ...]:
        rows = self._cursor.fetchmany(size)
        return tuple(tuple(row) for row in rows)

    def warnings(self) -> tuple[str, ...]:
        warning_count = getattr(self._cursor, "warning_count", 0)
        if not isinstance(warning_count, int) or warning_count <= 0:
            return ()
        warning_cursor = self._connection.cursor(
            buffered=True,
            raw=False,
            read_timeout=self._diagnostic_timeout_s,
            write_timeout=self._diagnostic_timeout_s,
        )
        try:
            warning_cursor.execute("SHOW WARNINGS")
            warning_rows = warning_cursor.fetchall()
            return tuple(
                f"{level} {code}: {message}" for level, code, message in warning_rows
            )
        finally:
            warning_cursor.close()

    def close(self) -> None:
        self._cursor.close()


class _ConnectorSession:
    def __init__(self, connection: Any, diagnostic_timeout_s: int) -> None:
        self._connection = connection
        self._diagnostic_timeout_s = diagnostic_timeout_s

    def connection_id(self) -> int:
        value = self._connection.connection_id
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError("connector returned an invalid connection ID")
        return value

    def is_alive(self) -> bool:
        self._connection.ping(reconnect=False, attempts=1, delay=0)
        return True

    def execute(self, sql: str) -> _ConnectorCursor:
        cursor = self._connection.cursor(buffered=False, raw=False)
        try:
            cursor.execute(sql)
        except Exception:
            cursor.close()
            raise
        return _ConnectorCursor(
            cursor,
            self._connection,
            self._diagnostic_timeout_s,
        )

    def abort(self) -> None:
        self._connection.shutdown()

    def close(self) -> None:
        self._connection.close()


class MySQLConnectorFactory:
    """Late-resolving secret-safe mysql-connector-python connection factory."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        connect: Callable[..., Any] = mysql.connector.connect,
        connection_timeout_s: int = 10,
        read_timeout_s: int = 310,
        control_timeout_s: int = 5,
        diagnostic_timeout_s: int = 5,
    ) -> None:
        _positive_bound(connection_timeout_s, "connection_timeout_s")
        _positive_bound(read_timeout_s, "read_timeout_s")
        _positive_bound(control_timeout_s, "control_timeout_s")
        _positive_bound(diagnostic_timeout_s, "diagnostic_timeout_s")
        if read_timeout_s <= MAX_STATEMENT_TIMEOUT_SECONDS:
            raise ValueError(
                "read_timeout_s must be greater than 300 seconds to leave watchdog grace"
            )
        self._environ = environ
        self._connect = connect
        self._connection_timeout_s = connection_timeout_s
        self._read_timeout_s = read_timeout_s
        self._control_timeout_s = control_timeout_s
        self._diagnostic_timeout_s = diagnostic_timeout_s

    @contextmanager
    def _session(
        self,
        node: NodeConfig,
        database: str,
        *,
        connection_timeout_s: int,
        read_timeout_s: int,
    ) -> Iterator[QuerySession]:
        credentials = resolve_credentials(node, self._environ)
        connection = self._connect(
            host=node.host,
            port=node.port,
            user=credentials.username.get_secret_value(),
            password=credentials.password.get_secret_value(),
            database=database,
            autocommit=True,
            buffered=False,
            get_warnings=False,
            raise_on_warnings=False,
            connection_timeout=connection_timeout_s,
            read_timeout=read_timeout_s,
            write_timeout=read_timeout_s,
            use_pure=True,
        )
        session = _ConnectorSession(connection, self._diagnostic_timeout_s)
        try:
            yield session
        finally:
            session.close()

    def query_session(self, node: NodeConfig, database: str):  # type: ignore[no-untyped-def]
        return self._session(
            node,
            database,
            connection_timeout_s=self._connection_timeout_s,
            read_timeout_s=self._read_timeout_s,
        )

    def control_session(self, node: NodeConfig, database: str):  # type: ignore[no-untyped-def]
        return self._session(
            node,
            database,
            connection_timeout_s=self._control_timeout_s,
            read_timeout_s=self._control_timeout_s,
        )


__all__ = [
    "INTERNAL_RESULT_LIMIT_ERRNO",
    "INTERNAL_RUNNER_ERRNO",
    "INTERNAL_WATCHDOG_TIMEOUT_ERRNO",
    "MySQLConnectorFactory",
    "NodeQueryRunner",
]
