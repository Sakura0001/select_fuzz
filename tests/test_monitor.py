import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from select_fuzz.monitor.events import (
    LostConnectionDeduplicator,
    LostConnectionEvent,
    is_lost_connection_error,
)
from select_fuzz.monitor import logs as monitor_logs
from select_fuzz.monitor.logs import SqlLogRecord, append_jsonl, read_jsonl
from select_fuzz.monitor.store import MetricStore


class InterfaceError(Exception):
    pass


def test_sql_日志按_jsonl_写入并保留中文状态(tmp_path: Path) -> None:
    path = tmp_path / "sql.jsonl"
    record = SqlLogRecord(
        timestamp=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
        task_id="task-1",
        node_name="node-a",
        status="成功",
        sql="SELECT 1",
    )

    append_jsonl(path, record.to_dict())

    rows = read_jsonl(path)
    assert rows[0]["status"] == "成功"
    assert rows[0]["sql"] == "SELECT 1"


def test_sql_日志可写入生成合法性和风险标签(tmp_path: Path) -> None:
    path = tmp_path / "sql.jsonl"
    record = SqlLogRecord(
        timestamp=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
        task_id="task-1",
        node_name="node-a",
        status="普通错误",
        sql="SELECT JSON_EXTRACT('{}')",
        error_message="参数数量错误",
        sql_validity="故意不合法",
        risk_tags=["invalid_function_arity"],
        expected_error=True,
    )

    append_jsonl(path, record.to_dict())

    rows = read_jsonl(path)
    assert rows[0]["sql_validity"] == "故意不合法"
    assert rows[0]["risk_tags"] == ["invalid_function_arity"]
    assert rows[0]["expected_error"] is True


def test_sql_日志始终包含基表模式版本和种子(tmp_path: Path) -> None:
    path = tmp_path / "sql.jsonl"
    record = SqlLogRecord(
        timestamp=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
        task_id="task-expanded",
        node_name="node-a",
        status="成功",
        sql="SELECT 1",
        expand_base_table_columns=True,
        base_table_seed="12345",
        base_table_generator_version="v1",
    )

    append_jsonl(path, record.to_dict())

    row = read_jsonl(path)[0]
    assert row["expand_base_table_columns"] is True
    assert row["base_table_seed"] == "12345"
    assert row["base_table_generator_version"] == "v1"
    assert "CREATE TABLE" not in row["sql"]


def test_sql_日志可携带_worker_路由和生成器信息(tmp_path: Path) -> None:
    path = tmp_path / "sql.jsonl"
    record = SqlLogRecord(
        timestamp=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
        task_id="task-role",
        node_name="node-a",
        status="成功",
        sql="UPDATE `t7` SET `c1` = 1 LIMIT 10",
        worker_type="dml",
        db_role="primary",
        target="10.0.0.10:3306",
        table_name="t7",
        operation="UPDATE",
        generator_seed="18446744073709551615",
        generator_version="v1",
    )

    append_jsonl(path, record.to_dict())

    row = read_jsonl(path)[0]
    assert row["worker_type"] == "dml"
    assert row["db_role"] == "primary"
    assert row["target"] == "10.0.0.10:3306"
    assert row["table_name"] == "t7"
    assert row["operation"] == "UPDATE"
    assert row["generator_seed"] == "18446744073709551615"
    assert row["generator_version"] == "v1"


def test_同一路径并发追加_jsonl_不会拼接或丢行(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "concurrent.jsonl"
    real_open = Path.open
    worker_total = 16
    barrier = threading.Barrier(worker_total)

    class SlowAppendFile:
        def __init__(self, file_obj) -> None:
            self._file_obj = file_obj

        def __enter__(self):
            self._file_obj.__enter__()
            return self

        def __exit__(self, *args):
            return self._file_obj.__exit__(*args)

        def write(self, text: str):
            result = self._file_obj.write(text)
            if not text.endswith("\n"):
                self._file_obj.flush()
                time.sleep(0.01)
            return result

        def __getattr__(self, name: str):
            return getattr(self._file_obj, name)

    def slow_open(target: Path, mode: str = "r", *args, **kwargs):
        file_obj = real_open(target, mode, *args, **kwargs)
        if mode == "a":
            return SlowAppendFile(file_obj)
        return file_obj

    monkeypatch.setattr(Path, "open", slow_open)

    def write_row(worker_id: int) -> None:
        barrier.wait()
        append_jsonl(path, {"worker_id": worker_id, "payload": "并发写入"})

    with ThreadPoolExecutor(max_workers=worker_total) as executor:
        list(executor.map(write_row, range(worker_total)))

    rows = read_jsonl(path)
    assert len(rows) == worker_total
    assert {row["worker_id"] for row in rows} == set(range(worker_total))


def test_同一路径并发追加多行文本时每个文本块保持完整(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "failed.sql"
    real_open = Path.open
    worker_total = 16
    barrier = threading.Barrier(worker_total)

    class SlowAppendFile:
        def __init__(self, file_obj) -> None:
            self._file_obj = file_obj

        def __enter__(self):
            self._file_obj.__enter__()
            return self

        def __exit__(self, *args):
            return self._file_obj.__exit__(*args)

        def write(self, text: str):
            midpoint = max(1, len(text) // 2)
            self._file_obj.write(text[:midpoint])
            self._file_obj.flush()
            time.sleep(0.005)
            return self._file_obj.write(text[midpoint:])

        def __getattr__(self, name: str):
            return getattr(self._file_obj, name)

    def slow_open(target: Path, mode: str = "r", *args, **kwargs):
        file_obj = real_open(target, mode, *args, **kwargs)
        if mode == "a":
            return SlowAppendFile(file_obj)
        return file_obj

    monkeypatch.setattr(Path, "open", slow_open)
    sql_blocks = [
        f"-- worker-{worker_id} begin\nSELECT {worker_id};\n-- worker-{worker_id} end"
        for worker_id in range(worker_total)
    ]

    def write_sql(sql: str) -> None:
        barrier.wait()
        monitor_logs.append_text_line(path, sql)

    with ThreadPoolExecutor(max_workers=worker_total) as executor:
        list(executor.map(write_sql, sql_blocks))

    content = path.read_text(encoding="utf-8")
    assert len(content) == sum(len(sql) + 1 for sql in sql_blocks)
    assert all(content.count(sql + "\n") == 1 for sql in sql_blocks)


def test_lost_connection_错误识别() -> None:
    assert is_lost_connection_error(Exception("Lost connection to MySQL server during query"))
    assert is_lost_connection_error(Exception("MySQL server has gone away"))
    assert is_lost_connection_error(EOFError("socket closed"))
    assert is_lost_connection_error(InterfaceError(0, ""))
    assert not is_lost_connection_error(Exception("Duplicate entry"))


def test_同一节点十分钟内只记录第一次_lost_connection() -> None:
    dedup = LostConnectionDeduplicator(window=timedelta(minutes=10))
    first = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)
    second = first + timedelta(minutes=5)
    third = first + timedelta(minutes=11)

    assert dedup.should_record("node-a", first) is True
    assert dedup.should_record("node-a", second) is False
    assert dedup.should_record("node-a", third) is True
    assert dedup.should_record("node-b", second) is True


def test_lost_connection_按节点角色和目标分别去重() -> None:
    dedup = LostConnectionDeduplicator(window=timedelta(minutes=10))
    first = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)
    second = first + timedelta(minutes=1)

    assert dedup.should_record(
        "node-a", first, db_role="primary", target="10.0.0.10:3306"
    ) is True
    assert dedup.should_record(
        "node-a", second, db_role="primary", target="10.0.0.10:3306"
    ) is False
    assert dedup.should_record(
        "node-a", second, db_role="replica", target="10.0.0.10:3306"
    ) is True
    assert dedup.should_record(
        "node-a", second, db_role="primary", target="10.0.0.11:3306"
    ) is True


def test_sqlite_指标存储保存任务和事件(tmp_path: Path) -> None:
    store = MetricStore(tmp_path / "metrics.db")
    event = LostConnectionEvent(
        timestamp=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
        task_id="task-1",
        node_name="node-a",
        jump_host="jump-prod",
        target="172.18.4.12:3306",
        sql="SELECT 1",
        window_start=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
    )

    store.upsert_task_metric("task-1", "node-a", "运行中", 12, 1)
    store.insert_lost_connection_event(event)

    assert store.summary()["任务数"] == 1
    assert store.summary()["lost connection"] == 1
    assert store.list_lost_connection_events("task-1")[0]["node_name"] == "node-a"


def test_lost_connection_事件可携带_worker_路由和生成器信息(tmp_path: Path) -> None:
    store = MetricStore(tmp_path / "metrics.db")
    event = LostConnectionEvent(
        timestamp=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
        task_id="task-role",
        node_name="node-a",
        jump_host=None,
        target="10.0.0.20:3306",
        sql="SELECT 1",
        window_start=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
        worker_type="query",
        db_role="replica",
        table_name="t8",
        operation="SELECT",
        generator_seed="12345",
        generator_version="v1",
    )

    store.insert_lost_connection_event(event)

    row = store.list_lost_connection_events("task-role")[0]
    assert event.to_dict()["worker_type"] == "query"
    assert row["worker_type"] == "query"
    assert row["db_role"] == "replica"
    assert row["target"] == "10.0.0.20:3306"
    assert row["table_name"] == "t8"
    assert row["operation"] == "SELECT"
    assert row["generator_seed"] == "12345"
    assert row["generator_version"] == "v1"


def test_sqlite_旧_schema_自动迁移角色字段并保留旧数据(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE task_metrics (
              task_id TEXT PRIMARY KEY,
              node_name TEXT NOT NULL,
              status TEXT NOT NULL,
              sql_total INTEGER NOT NULL,
              lost_connection_total INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE lost_connection_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              timestamp TEXT NOT NULL,
              task_id TEXT NOT NULL,
              node_name TEXT NOT NULL,
              jump_host TEXT,
              target TEXT NOT NULL,
              sql TEXT NOT NULL,
              window_start TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO lost_connection_events(
              timestamp, task_id, node_name, jump_host, target, sql, window_start
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-04T09:00:00+00:00",
                "legacy-task",
                "legacy-node",
                None,
                "127.0.0.1:3306",
                "SELECT 1",
                "2026-06-04T09:00:00+00:00",
            ),
        )

    store = MetricStore(path)

    with sqlite3.connect(path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(lost_connection_events)")
        }
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert {
        "worker_type",
        "db_role",
        "table_name",
        "operation",
        "generator_seed",
        "generator_version",
    }.issubset(columns)
    legacy_row = store.list_lost_connection_events("legacy-task")[0]
    assert legacy_row["node_name"] == "legacy-node"
    assert legacy_row["db_role"] is None


def test_sqlite_wal_busy_timeout_与多实例并发写入稳定(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    stores = [MetricStore(path) for _ in range(8)]
    event_total = 160

    with stores[0]._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5_000

    def persist(index: int) -> None:
        store = stores[index % len(stores)]
        task_id = f"task-{index}"
        store.upsert_task_metric(task_id, "node-a", "运行中", index, index % 3)
        store.insert_lost_connection_event(
            LostConnectionEvent(
                timestamp=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)
                + timedelta(microseconds=index),
                task_id=task_id,
                node_name="node-a",
                jump_host=None,
                target="10.0.0.20:3306",
                sql="SELECT 1",
                window_start=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
                worker_type="query",
                db_role="replica",
            )
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(persist, range(event_total)))

    assert stores[0].summary() == {"任务数": event_total, "lost connection": event_total}
