"""Streaming execution that retains statistics but never result values."""

from __future__ import annotations

import math
from collections.abc import Callable
from threading import Event, Lock
import time
from typing import Any

from select_fuzz.config import NodeConfig
from select_fuzz.execution.protocols import ConnectionFactory, QuerySession
from select_fuzz.execution.timeout import KillQueryWatchdog
from select_fuzz.modes.fuzz.forensics import (
    capture_exception_evidence,
    watchdog_diagnostic_snapshot,
)
from select_fuzz.modes.fuzz.models import FuzzExecutionResult


_LOST_CONNECTION_ERRNOS = {2006, 2013, 2055}


def _error_identity(error: Exception) -> tuple[str, bool, int | None]:
    raw_errno = getattr(error, "errno", None)
    errno = raw_errno if isinstance(raw_errno, int) and not isinstance(raw_errno, bool) else None
    sqlstate = getattr(error, "sqlstate", None)
    identity = f"{type(error).__name__}"
    if isinstance(errno, int):
        identity += f":errno={errno}"
    if isinstance(sqlstate, str):
        identity += f":sqlstate={sqlstate}"
    return identity, errno in _LOST_CONNECTION_ERRNOS, errno


class StreamingQueryExecutor:
    def __init__(
        self,
        factory: ConnectionFactory,
        *,
        fetch_rows: int = 128,
        watchdog: KillQueryWatchdog | None = None,
    ) -> None:
        if fetch_rows <= 0:
            raise ValueError("fetch_rows must be positive")
        self._factory = factory
        self._fetch_rows = fetch_rows
        self._watchdog = watchdog or KillQueryWatchdog(factory)
        self._active: dict[object, Any] = {}
        self._manually_stopped: set[object] = set()
        self._active_lock = Lock()

    def execute(
        self,
        node: NodeConfig,
        database: str,
        sql: str,
        *,
        timeout_seconds: float,
    ) -> FuzzExecutionResult:
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        started = time.monotonic_ns()
        try:
            with self._factory.query_session(node, database) as session:
                return self.execute_session(
                    session,
                    sql,
                    node=node,
                    database=database,
                    started_ns=started,
                    timeout_seconds=float(timeout_seconds),
                )
        except Exception as error:
            identity, lost, errno = _error_identity(error)
            elapsed_ns = max(0, time.monotonic_ns() - started)
            evidence = capture_exception_evidence(error, "connection_open")
            evidence["connection_id"] = None
            evidence["watchdog"] = watchdog_diagnostic_snapshot(None)
            evidence["timings"] = {
                "execute_elapsed_ns": 0,
                "fetch_elapsed_ns": 0,
                "cursor_close_elapsed_ns": 0,
                "total_elapsed_ns": elapsed_ns,
            }
            return FuzzExecutionResult(
                False,
                0,
                elapsed_ns,
                error=identity,
                connection_lost=lost,
                failure_evidence=evidence,
                errno=errno,
            )

    def execute_session(
        self,
        session: QuerySession,
        sql: str,
        *,
        node: NodeConfig | None = None,
        database: str | None = None,
        started_ns: int | None = None,
        timeout_seconds: float | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> FuzzExecutionResult:
        started = time.monotonic_ns() if started_ns is None else started_ns
        deadline_ns = None
        if timeout_seconds is not None:
            if (
                not isinstance(timeout_seconds, (int, float))
                or isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0
            ):
                raise ValueError("timeout_seconds must be finite and positive")
            deadline_ns = started + int(float(timeout_seconds) * 1_000_000_000)
            if node is None or not database:
                raise ValueError(
                    "node and database are required when timeout_seconds is provided"
                )
        cursor = None
        rows_seen = 0
        affected_rows: int | None = None
        execute_elapsed_ns = 0
        fetch_elapsed_ns = 0
        success = False
        error_identity: str | None = None
        errno: int | None = None
        connection_lost = False
        client_deadline_exceeded = False
        statement_token: object | None = None
        statement_done: Event | None = None
        handle: Any | None = None
        connection_id: int | None = None
        failure_evidence: dict[str, object] | None = None
        cursor_close_evidence: dict[str, object] | None = None
        execute_started_ns: int | None = None
        fetch_started_ns: int | None = None
        cursor_close_elapsed_ns = 0
        failure_stage = "connection_id"
        try:
            if timeout_seconds is not None:
                assert node is not None
                assert database is not None
                statement_token = object()
                statement_done = Event()
                connection_id = session.connection_id()
                failure_stage = "watchdog_arm"
                handle = self._watchdog.arm(
                    node,
                    database,
                    connection_id,
                    float(timeout_seconds),
                    statement_token=statement_token,
                    fallback_abort=session.abort,
                    statement_done=statement_done,
                )
                self._register_active(handle, statement_token)
            if on_stage is not None:
                on_stage("executing")
            execute_started_ns = time.monotonic_ns()
            failure_stage = "execute"
            cursor = session.execute(sql)
            execute_elapsed_ns = max(0, time.monotonic_ns() - execute_started_ns)
            affected_rows = cursor.affected_rows
            if on_stage is not None:
                on_stage("fetching")
            fetch_started_ns = time.monotonic_ns()
            failure_stage = "fetch"
            while True:
                if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
                    raise TimeoutError("fuzz query exceeded client deadline")
                batch = cursor.fetchmany(self._fetch_rows)
                if not batch:
                    break
                rows_seen += len(batch)
            fetch_elapsed_ns = max(0, time.monotonic_ns() - fetch_started_ns)
            success = True
        except Exception as error:
            failed_at_ns = time.monotonic_ns()
            if failure_stage == "execute" and execute_started_ns is not None:
                execute_elapsed_ns = max(0, failed_at_ns - execute_started_ns)
            elif failure_stage == "fetch" and fetch_started_ns is not None:
                fetch_elapsed_ns = max(0, failed_at_ns - fetch_started_ns)
            error_identity, connection_lost, errno = _error_identity(error)
            client_deadline_exceeded = isinstance(error, TimeoutError)
            failure_evidence = capture_exception_evidence(error, failure_stage)
        finally:
            if statement_done is not None:
                statement_done.set()
            if handle is not None and statement_token is not None:
                try:
                    handle.cancel(statement_token=statement_token)
                except Exception as error:
                    if failure_evidence is None:
                        failure_evidence = capture_exception_evidence(
                            error,
                            "watchdog_cancel",
                        )
                        error_identity, connection_lost, errno = _error_identity(error)
                    else:
                        failure_evidence["watchdog_cancel_error"] = (
                            capture_exception_evidence(error, "watchdog_cancel")
                        )
                    success = False
                finally:
                    self._remove_active(statement_token)
            if cursor is not None:
                cursor_close_started_ns = time.monotonic_ns()
                try:
                    cursor.close()
                except Exception as error:
                    cursor_close_evidence = capture_exception_evidence(
                        error,
                        "cursor_close",
                    )
                    if failure_evidence is None:
                        failure_evidence = dict(cursor_close_evidence)
                        error_identity, connection_lost, errno = _error_identity(error)
                    success = False
                finally:
                    cursor_close_elapsed_ns = max(
                        0,
                        time.monotonic_ns() - cursor_close_started_ns,
                    )
        elapsed_ns = max(0, time.monotonic_ns() - started)
        timed_out = client_deadline_exceeded or bool(
            handle is not None and handle.timed_out
        )
        stopped = bool(
            statement_token is not None and self._consume_manual_stop(statement_token)
        )
        if timed_out and failure_evidence is None:
            failure_evidence = capture_exception_evidence(
                TimeoutError("watchdog deadline fired without connector exception"),
                "watchdog_timeout",
            )
        if failure_evidence is not None:
            failure_evidence["connection_id"] = connection_id
            failure_evidence["watchdog"] = watchdog_diagnostic_snapshot(handle)
            if cursor_close_evidence is not None:
                failure_evidence["cursor_close_error"] = cursor_close_evidence
            failure_evidence["timings"] = {
                "execute_elapsed_ns": execute_elapsed_ns,
                "fetch_elapsed_ns": fetch_elapsed_ns,
                "cursor_close_elapsed_ns": cursor_close_elapsed_ns,
                "total_elapsed_ns": elapsed_ns,
            }
        if timed_out:
            success = False
            error_identity = "query_timeout"
        elif stopped:
            success = False
            error_identity = "query_stopped"
        return FuzzExecutionResult(
            success,
            rows_seen,
            elapsed_ns,
            affected_rows=affected_rows,
            error=error_identity,
            connection_lost=connection_lost,
            execute_elapsed_ns=execute_elapsed_ns,
            fetch_elapsed_ns=fetch_elapsed_ns,
            timed_out=timed_out,
            stopped=stopped,
            failure_evidence=failure_evidence,
            errno=errno,
        )

    def _register_active(self, handle: Any, statement_token: object) -> None:
        with self._active_lock:
            self._active[statement_token] = handle

    def _register_active_for_test(self, handle: Any, statement_token: object) -> None:
        self._register_active(handle, statement_token)

    def _remove_active(self, statement_token: object) -> None:
        with self._active_lock:
            self._active.pop(statement_token, None)

    def _consume_manual_stop(self, statement_token: object) -> bool:
        with self._active_lock:
            if statement_token not in self._manually_stopped:
                return False
            self._manually_stopped.remove(statement_token)
            return True

    @property
    def active_queries(self) -> int:
        with self._active_lock:
            return len(self._active)

    def stop_active(self) -> None:
        with self._active_lock:
            active = tuple(self._active.items())
            self._manually_stopped.update(token for token, _handle in active)
        for token, handle in active:
            handle.trigger(statement_token=token)


__all__ = ["StreamingQueryExecutor"]
