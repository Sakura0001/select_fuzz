from __future__ import annotations

from contextlib import contextmanager

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.performance.diagnostics import MySQLDiagnosticsCollector


class _Cursor:
    columns = ()

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        del size
        rows, self.rows = self.rows, []
        return rows

    def warnings(self) -> tuple[str, ...]:
        return ()

    def close(self) -> None:
        return None


class _Session:
    def __init__(self, factory: _Factory) -> None:
        self.factory = factory

    def execute(self, sql: str) -> _Cursor:
        self.factory.sql.append(sql)
        if sql.startswith("SHOW GLOBAL STATUS"):
            self.factory.status_calls += 1
            value = 1 if self.factory.status_calls == 1 else 9
            return _Cursor([("Handler_read_rnd_next", str(value))])
        return _Cursor([(1000, 2, 30, 1, 0, 0)])

    def connection_id(self) -> int:
        return 1

    def abort(self) -> None:
        return None


class _Factory:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.status_calls = 0

    @contextmanager
    def control_session(self, node: NodeConfig, database: str):  # type: ignore[no-untyped-def]
        del node, database
        yield _Session(self)


def test_mysql_diagnostics_uses_processlist_mapping_and_status_delta() -> None:
    factory = _Factory()
    collector = MySQLDiagnosticsCollector(factory)  # type: ignore[arg-type]
    node = NodeConfig(role=NodeRole.BASELINE, host="127.0.0.1")

    before = collector.before(node, "perf_db")
    result = collector.after(node, "perf_db", 42, before)

    assert result["status_delta"] == {  # type: ignore[index]
        "Handler_read_rnd_next": 8,
        "Created_tmp_tables": 0,
        "Created_tmp_disk_tables": 0,
        "Innodb_buffer_pool_reads": 0,
        "Innodb_buffer_pool_read_ahead": 0,
    }
    assert result["pfs"]["rows_examined"] == 30  # type: ignore[index]
    assert any("PROCESSLIST_ID=42" in sql for sql in factory.sql)
    assert any("SQL_TEXT LIKE 'EXPLAIN ANALYZE%'" in sql for sql in factory.sql)
