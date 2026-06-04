from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional

from select_fuzz.config import TargetNodeConfig
from select_fuzz.metadata.base_sql import load_base_sql_files, split_sql_statements
from select_fuzz.metadata.ddl_parser import parse_create_table
from select_fuzz.metadata.models import TableMetadata
from select_fuzz.monitor.events import LostConnectionDeduplicator, LostConnectionEvent, is_lost_connection_error
from select_fuzz.monitor.logs import SqlLogRecord, append_jsonl
from select_fuzz.monitor.store import MetricStore
from select_fuzz.sqlgen.generator import GenerationOptions, SQLGenerator

from .db import DatabaseClient, LostConnectionError


class TaskStatus(str, Enum):
    NEW = "新建"
    CONNECTING = "连接实例"
    SEEDING = "准备基表"
    RUNNING = "执行 SQL"
    RECOVERING = "恢复检测"
    STOPPED = "已停止"


@dataclass
class FuzzTask:
    task_id: str
    node: TargetNodeConfig
    base_sql_dir: Path
    db: DatabaseClient
    metric_store: MetricStore
    log_dir: Path
    clock: Callable[[], datetime]
    random_seed: Optional[int] = None
    recovery_probe_seconds: int = 60
    lost_connection_dedup_minutes: int = 10
    status: TaskStatus = TaskStatus.NEW
    sql_total: int = 0
    ordinary_error_total: int = 0
    lost_connection_total: int = 0
    tables: List[TableMetadata] = field(default_factory=list)
    _dedup: LostConnectionDeduplicator = field(init=False)
    _next_probe_at: Optional[datetime] = None
    _generator: SQLGenerator = field(init=False)

    def __post_init__(self) -> None:
        self.base_sql_dir = Path(self.base_sql_dir)
        self.log_dir = Path(self.log_dir)
        self._dedup = LostConnectionDeduplicator(timedelta(minutes=self.lost_connection_dedup_minutes))
        self._generator = SQLGenerator(random_seed=self.random_seed)

    def start(self) -> None:
        self.status = TaskStatus.CONNECTING
        self.db.connect()
        self.status = TaskStatus.SEEDING
        database = self.node.database or "test"
        self.db.execute(f"CREATE DATABASE IF NOT EXISTS {_quote_identifier(database)}")
        self.db.execute(f"USE {_quote_identifier(database)}")
        self.tables.clear()
        sql_files = load_base_sql_files(self.base_sql_dir)
        for sql_file in sql_files:
            try:
                self.tables.append(parse_create_table(sql_file.sql))
            except ValueError:
                continue
        self._reset_base_tables()
        for sql_file in sql_files:
            for statement in split_sql_statements(sql_file.sql):
                self.db.execute(statement)
        self.status = TaskStatus.RUNNING
        self._write_metrics()

    def step(self) -> None:
        if self.status is TaskStatus.RECOVERING:
            self.probe_recovery()
            return
        if self.status is not TaskStatus.RUNNING:
            return
        sql = self._generator.generate(
            self.tables,
            GenerationOptions(
                require_join=len(self.tables) > 1,
                require_vector=any(any(col.type_family.value == "向量" for col in table.columns.values()) for table in self.tables),
            ),
        )
        try:
            self.db.execute(sql)
        except Exception as exc:
            if isinstance(exc, LostConnectionError) or is_lost_connection_error(exc):
                self._handle_lost_connection(sql)
                return
            self.ordinary_error_total += 1
            self._write_sql_log("普通错误", sql)
            self._write_metrics()
            return
        self.sql_total += 1
        self._write_sql_log("成功", sql)
        self._write_metrics()

    def probe_recovery(self) -> None:
        if self.status is not TaskStatus.RECOVERING:
            return
        now = self.clock()
        if self._next_probe_at is not None and now < self._next_probe_at:
            return
        if self.db.ping():
            self.status = TaskStatus.RUNNING
            self._next_probe_at = None
        else:
            self._next_probe_at = now + timedelta(seconds=self.recovery_probe_seconds)
        self._write_metrics()

    def stop(self) -> None:
        self.db.close()
        self.status = TaskStatus.STOPPED
        self._write_metrics()

    def _handle_lost_connection(self, sql: str) -> None:
        now = self.clock()
        self._write_sql_log("lost connection", sql)
        if self._dedup.should_record(self.node.name, now):
            self.lost_connection_total += 1
            event = LostConnectionEvent(
                timestamp=now,
                task_id=self.task_id,
                node_name=self.node.name,
                jump_host=self.node.jump_host,
                target=self.node.address,
                sql=sql,
                window_start=now,
            )
            self.metric_store.insert_lost_connection_event(event)
            append_jsonl(self._event_log_path(), event.to_dict())
        self.status = TaskStatus.RECOVERING
        self._next_probe_at = now + timedelta(seconds=self.recovery_probe_seconds)
        self._write_metrics()

    def _write_sql_log(self, status: str, sql: str) -> None:
        record = SqlLogRecord(
            timestamp=self.clock(),
            task_id=self.task_id,
            node_name=self.node.name,
            status=status,
            sql=sql,
        )
        append_jsonl(self._sql_log_path(), record.to_dict())

    def _sql_log_path(self) -> Path:
        date = self.clock().date().isoformat()
        return self.log_dir / date / f"{self.task_id}.sql.jsonl"

    def _event_log_path(self) -> Path:
        date = self.clock().date().isoformat()
        return self.log_dir / date / f"{self.task_id}.lost_connection.jsonl"

    def _write_metrics(self) -> None:
        self.metric_store.upsert_task_metric(
            self.task_id,
            self.node.name,
            self.status.value,
            self.sql_total,
            self.lost_connection_total,
        )

    def _reset_base_tables(self) -> None:
        if not self.tables:
            return
        self.db.execute("SET FOREIGN_KEY_CHECKS=0")
        for table in reversed(self.tables):
            self.db.execute(f"DROP TABLE IF EXISTS {_quote_identifier(table.name)}")
        self.db.execute("SET FOREIGN_KEY_CHECKS=1")


def _quote_identifier(identifier: str) -> str:
    return f"`{identifier.replace('`', '``')}`"
