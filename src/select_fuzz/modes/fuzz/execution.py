"""Streaming execution that retains statistics but never result values."""

from __future__ import annotations

import time
import math

from select_fuzz.config import NodeConfig
from select_fuzz.execution.protocols import ConnectionFactory, QuerySession
from select_fuzz.modes.fuzz.models import FuzzExecutionResult


_LOST_CONNECTION_ERRNOS = {2006, 2013, 2055}


def _error_identity(error: Exception) -> tuple[str, bool]:
    errno = getattr(error, "errno", None)
    sqlstate = getattr(error, "sqlstate", None)
    identity = f"{type(error).__name__}"
    if isinstance(errno, int):
        identity += f":errno={errno}"
    if isinstance(sqlstate, str):
        identity += f":sqlstate={sqlstate}"
    return identity, errno in _LOST_CONNECTION_ERRNOS


class StreamingQueryExecutor:
    def __init__(self, factory: ConnectionFactory, *, fetch_rows: int = 128) -> None:
        if fetch_rows <= 0:
            raise ValueError("fetch_rows must be positive")
        self._factory = factory
        self._fetch_rows = fetch_rows

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
                    started_ns=started,
                    timeout_seconds=float(timeout_seconds),
                )
        except Exception as error:
            identity, lost = _error_identity(error)
            return FuzzExecutionResult(
                False,
                0,
                time.monotonic_ns() - started,
                error=identity,
                connection_lost=lost,
            )

    def execute_session(
        self,
        session: QuerySession,
        sql: str,
        *,
        started_ns: int | None = None,
        timeout_seconds: float | None = None,
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
        cursor = None
        rows_seen = 0
        try:
            cursor = session.execute(sql)
            affected_rows = cursor.affected_rows
            while True:
                if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
                    session.abort()
                    raise TimeoutError("fuzz query exceeded client deadline")
                batch = cursor.fetchmany(self._fetch_rows)
                if not batch:
                    break
                rows_seen += len(batch)
            return FuzzExecutionResult(
                True,
                rows_seen,
                time.monotonic_ns() - started,
                affected_rows=affected_rows,
            )
        except Exception as error:
            identity, lost = _error_identity(error)
            return FuzzExecutionResult(
                False,
                rows_seen,
                time.monotonic_ns() - started,
                error=identity,
                connection_lost=lost,
            )
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass


__all__ = ["StreamingQueryExecutor"]
