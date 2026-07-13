"""Best-effort diagnostic helpers that never influence verdicts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from select_fuzz.config import NodeConfig
from select_fuzz.execution.protocols import ConnectionFactory, QuerySession


PFS_SQL = """SELECT TIMER_WAIT/1000000000, LOCK_TIME/1000000000,
 ROWS_EXAMINED, ROWS_SENT, CREATED_TMP_DISK_TABLES, NO_INDEX_USED
 FROM performance_schema.events_statements_history_long
 WHERE THREAD_ID=(SELECT THREAD_ID FROM performance_schema.threads
 WHERE PROCESSLIST_ID={connection_id})
 AND SQL_TEXT LIKE 'EXPLAIN ANALYZE%' ORDER BY EVENT_ID DESC LIMIT 1"""

STATUS_NAMES = (
    "Handler_read_rnd_next",
    "Created_tmp_tables",
    "Created_tmp_disk_tables",
    "Innodb_buffer_pool_reads",
    "Innodb_buffer_pool_read_ahead",
)


def metric_delta(
    before: Mapping[str, int], after: Mapping[str, int]
) -> dict[str, int]:
    return {name: after.get(name, 0) - before.get(name, 0) for name in STATUS_NAMES}


class DiagnosticsPort(Protocol):
    def before(self, node: NodeConfig, database: str) -> object: ...

    def after(
        self,
        node: NodeConfig,
        database: str,
        connection_id: int | None,
        before: object,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class DiagnosticBaseline:
    status: Mapping[str, int]
    errors: tuple[str, ...] = ()


def _fetch_all(session: QuerySession, sql: str) -> tuple[tuple[object, ...], ...]:
    cursor = session.execute(sql)
    rows: list[tuple[object, ...]] = []
    try:
        while True:
            batch = cursor.fetchmany(128)
            if not batch:
                break
            rows.extend(tuple(row) for row in batch)
    finally:
        cursor.close()
    return tuple(rows)


class MySQLDiagnosticsCollector:
    """Best-effort global deltas and per-statement PFS evidence."""

    def __init__(self, factory: ConnectionFactory) -> None:
        self._factory = factory

    @staticmethod
    def _status_sql() -> str:
        names = ",".join(f"'{name}'" for name in STATUS_NAMES)
        return f"SHOW GLOBAL STATUS WHERE Variable_name IN ({names})"

    def _status(self, node: NodeConfig, database: str) -> dict[str, int]:
        with self._factory.control_session(node, database) as session:
            rows = _fetch_all(session, self._status_sql())
        values: dict[str, int] = {}
        for row in rows:
            if len(row) == 2 and isinstance(row[0], str):
                raw_value = row[1]
                if not isinstance(raw_value, (str, int)) or isinstance(raw_value, bool):
                    continue
                try:
                    values[row[0]] = int(raw_value)
                except (TypeError, ValueError):
                    continue
        return values

    def before(self, node: NodeConfig, database: str) -> DiagnosticBaseline:
        try:
            return DiagnosticBaseline(self._status(node, database))
        except Exception as error:
            return DiagnosticBaseline({}, (type(error).__name__,))

    def after(
        self,
        node: NodeConfig,
        database: str,
        connection_id: int | None,
        before: object,
    ) -> Mapping[str, object]:
        baseline = before if isinstance(before, DiagnosticBaseline) else DiagnosticBaseline({})
        errors = list(baseline.errors)
        after_status: dict[str, int] = {}
        try:
            after_status = self._status(node, database)
        except Exception as error:
            errors.append(type(error).__name__)
        pfs: dict[str, object] = {}
        if connection_id is not None:
            try:
                with self._factory.control_session(node, database) as session:
                    rows = _fetch_all(
                        session, PFS_SQL.format(connection_id=int(connection_id))
                    )
                if rows:
                    row = rows[0]
                    names = (
                        "timer_wait_ms", "lock_time_ms", "rows_examined", "rows_sent",
                        "created_tmp_disk_tables", "no_index_used",
                    )
                    pfs = {name: row[index] for index, name in enumerate(names) if index < len(row)}
            except Exception as error:
                errors.append(type(error).__name__)
        result: dict[str, object] = {
            "status_delta": metric_delta(baseline.status, after_status),
            "pfs": pfs,
        }
        if errors:
            result["diagnostics_error"] = tuple(errors)
        return result


__all__ = [
    "DiagnosticBaseline",
    "DiagnosticsPort",
    "MySQLDiagnosticsCollector",
    "PFS_SQL",
    "STATUS_NAMES",
    "metric_delta",
]
