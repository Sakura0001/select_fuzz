from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional

from select_fuzz.config import TargetNodeConfig
from select_fuzz.metadata.base_sql import load_base_sql_files, split_sql_statements
from select_fuzz.metadata.ddl_parser import parse_create_table
from select_fuzz.metadata.models import BaseSqlFile, TableMetadata
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
        self._recreate_database()
        self.tables.clear()
        sql_files = load_base_sql_files(self.base_sql_dir)
        for sql_file in sql_files:
            try:
                self.tables.append(parse_create_table(sql_file.sql))
            except ValueError:
                continue
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
            try:
                self._rebuild_temporary_tables()
            except Exception:
                self._next_probe_at = now + timedelta(seconds=self.recovery_probe_seconds)
                self._write_metrics()
                return
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

    @property
    def coverage_counts(self) -> dict[str, int]:
        return dict(self._generator.coverage_counts)

    @property
    def recent_coverage_hits(self) -> list[str]:
        return list(self._generator.recent_hits)

    def _database_name(self) -> str:
        return self.node.database or "test"

    def _recreate_database(self) -> None:
        database = self._database_name()
        self.db.execute(f"DROP DATABASE IF EXISTS {_quote_identifier(database)}")
        self.db.execute(f"CREATE DATABASE {_quote_identifier(database)}")
        self.db.execute(f"USE {_quote_identifier(database)}")

    def _rebuild_temporary_tables(self) -> None:
        temporary_names = {table.name for table in self.tables if table.is_temporary}
        if not temporary_names:
            return
        self.db.execute(f"USE {_quote_identifier(self._database_name())}")
        sql_files = load_base_sql_files(self.base_sql_dir)
        for sql_file in sql_files:
            if self._is_temporary_table_file(sql_file):
                self._execute_statements(sql_file)
        self._execute_temporary_seed_statements(sql_files, temporary_names)

    def _execute_statements(self, sql_file: BaseSqlFile) -> None:
        for statement in split_sql_statements(sql_file.sql):
            self.db.execute(statement)

    def _is_temporary_table_file(self, sql_file: BaseSqlFile) -> bool:
        return bool(re.search(r"\bCREATE\s+TEMPORARY\s+TABLE\b", sql_file.sql, re.IGNORECASE))

    def _execute_temporary_seed_statements(self, sql_files: List[BaseSqlFile], temporary_names: set[str]) -> None:
        for sql_file in sql_files:
            if self._is_temporary_table_file(sql_file):
                continue
            for statement in split_sql_statements(sql_file.sql):
                target = self._insert_target_table(statement)
                if target in temporary_names:
                    self.db.execute(statement)

    def _insert_target_table(self, statement: str) -> Optional[str]:
        match = re.match(r"\s*INSERT\s+INTO\s+`?(?P<table>[\w$]+)`?", statement, re.IGNORECASE)
        return match.group("table") if match else None


def _quote_identifier(identifier: str) -> str:
    return f"`{identifier.replace('`', '``')}`"
