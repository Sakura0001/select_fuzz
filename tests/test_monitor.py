from datetime import datetime, timedelta, timezone
from pathlib import Path

from select_fuzz.monitor.events import (
    LostConnectionDeduplicator,
    LostConnectionEvent,
    is_lost_connection_error,
)
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
