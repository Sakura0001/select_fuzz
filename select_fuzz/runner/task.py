from __future__ import annotations

import re
import threading
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
class TaskWorker:
    worker_id: int
    db: DatabaseClient
    generator: SQLGenerator


@dataclass
class FuzzTask:
    task_id: str
    node: TargetNodeConfig
    base_sql_dir: Path
    db: DatabaseClient
    metric_store: MetricStore
    log_dir: Path
    clock: Callable[[], datetime]
    failed_sql_dir: Path | None = None
    db_factory: Optional[Callable[[], DatabaseClient]] = None
    thread_count: int = 1
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
    _workers: List[TaskWorker] = field(default_factory=list, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    def __post_init__(self) -> None:
        if self.thread_count < 1:
            raise ValueError("线程数必须大于等于 1")
        if self.thread_count > 1 and self.db_factory is None:
            raise ValueError("多线程任务必须提供 db_factory 以创建独立连接")
        self.base_sql_dir = Path(self.base_sql_dir)
        self.log_dir = Path(self.log_dir)
        self.failed_sql_dir = Path(self.failed_sql_dir) if self.failed_sql_dir is not None else self.log_dir / "failed_sql"
        self._dedup = LostConnectionDeduplicator(timedelta(minutes=self.lost_connection_dedup_minutes))
        self._workers = [TaskWorker(worker_id=0, db=self.db, generator=self._new_generator(0))]

    def start(self) -> None:
        with self._lock:
            self.status = TaskStatus.CONNECTING
        self.db.connect()
        with self._lock:
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
            self._execute_statements(sql_file, self.db)
        self._verify_seed_data(self.db)
        self._prepare_additional_workers(sql_files)
        with self._lock:
            self.status = TaskStatus.RUNNING
            self._write_metrics()

    def step(self, worker_id: int = 0) -> None:
        worker = self._worker(worker_id)
        if worker is None:
            return
        with self._lock:
            current_status = self.status
        if current_status is TaskStatus.RECOVERING:
            if worker_id == 0:
                self.probe_recovery()
            return
        if current_status is not TaskStatus.RUNNING:
            return

        sql = worker.generator.generate(
            self.tables,
            GenerationOptions(
                require_join=len(self.tables) > 1,
                require_vector=any(any(col.type_family.value == "向量" for col in table.columns.values()) for table in self.tables),
            ),
        )
        try:
            worker.db.execute(sql)
        except Exception as exc:
            if isinstance(exc, LostConnectionError) or is_lost_connection_error(exc):
                self._handle_lost_connection(sql)
                return
            with self._lock:
                self.ordinary_error_total += 1
                self._write_sql_log("普通错误", sql)
                self._write_failed_sql(sql)
                self._write_metrics()
            return
        with self._lock:
            self.sql_total += 1
            self._write_sql_log("成功", sql)
            self._write_metrics()

    def probe_recovery(self) -> None:
        with self._lock:
            if self.status is not TaskStatus.RECOVERING:
                return
            now = self.clock()
            if self._next_probe_at is not None and now < self._next_probe_at:
                return
            workers = list(self._workers)

        if all(worker.db.ping() for worker in workers):
            try:
                sql_files = load_base_sql_files(self.base_sql_dir)
                for worker in workers:
                    self._prepare_worker_session(worker.db, sql_files)
            except Exception:
                with self._lock:
                    self._next_probe_at = now + timedelta(seconds=self.recovery_probe_seconds)
                    self._write_metrics()
                return
            with self._lock:
                self.status = TaskStatus.RUNNING
                self._next_probe_at = None
                self._write_metrics()
            return

        with self._lock:
            self._next_probe_at = now + timedelta(seconds=self.recovery_probe_seconds)
            self._write_metrics()

    def stop(self) -> None:
        for worker in list(self._workers):
            worker.db.close()
        with self._lock:
            self.status = TaskStatus.STOPPED
            self._write_metrics()

    def _handle_lost_connection(self, sql: str) -> None:
        with self._lock:
            now = self.clock()
            self._write_sql_log("lost connection", sql)
            self._write_failed_sql(sql)
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

    def _write_failed_sql(self, sql: str) -> None:
        path = self._failed_sql_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file_obj:
            file_obj.write(sql.strip())
            file_obj.write("\n")

    def _sql_log_path(self) -> Path:
        date = self.clock().date().isoformat()
        return self.log_dir / date / f"{self.task_id}.sql.jsonl"

    def _failed_sql_path(self) -> Path:
        date = self.clock().date().isoformat()
        return Path(self.failed_sql_dir) / date / f"{self.task_id}.sql"

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
        counts: dict[str, int] = {}
        for worker in self._workers:
            for name, count in worker.generator.coverage_counts.items():
                counts[name] = counts.get(name, 0) + count
        return counts

    @property
    def recent_coverage_hits(self) -> list[str]:
        hits = set()
        for worker in self._workers:
            hits.update(worker.generator.recent_hits)
        return sorted(hits)

    def _database_name(self) -> str:
        return self.node.database or "test"

    def _recreate_database(self) -> None:
        database = self._database_name()
        self.db.execute(f"DROP DATABASE IF EXISTS {_quote_identifier(database)}")
        self.db.execute(f"CREATE DATABASE {_quote_identifier(database)}")
        self.db.execute(f"USE {_quote_identifier(database)}")

    def _prepare_additional_workers(self, sql_files: List[BaseSqlFile]) -> None:
        for worker_id in range(1, self.thread_count):
            assert self.db_factory is not None
            db = self.db_factory()
            db.connect()
            self._prepare_worker_session(db, sql_files)
            self._workers.append(TaskWorker(worker_id=worker_id, db=db, generator=self._new_generator(worker_id)))

    def _prepare_worker_session(self, db: DatabaseClient, sql_files: List[BaseSqlFile]) -> None:
        db.execute(f"USE {_quote_identifier(self._database_name())}")
        temporary_names = {table.name for table in self.tables if table.is_temporary}
        if not temporary_names:
            return
        for sql_file in sql_files:
            if self._is_temporary_table_file(sql_file):
                self._execute_statements(sql_file, db)
        self._execute_temporary_seed_statements(sql_files, temporary_names, db)
        self._verify_seed_data(db, temporary_names)

    def _verify_seed_data(self, db: DatabaseClient, table_names: set[str] | None = None) -> None:
        targets = [table for table in self.tables if table_names is None or table.name in table_names]
        for table in targets:
            count = db.query_scalar(f"SELECT COUNT(*) FROM {_quote_identifier(table.name)}")
            if count <= 0:
                raise RuntimeError(f"基表初始化未插入数据: {table.name}")

    def _execute_statements(self, sql_file: BaseSqlFile, db: DatabaseClient) -> None:
        for statement in split_sql_statements(sql_file.sql):
            db.execute(statement)

    def _is_temporary_table_file(self, sql_file: BaseSqlFile) -> bool:
        return bool(re.search(r"\bCREATE\s+TEMPORARY\s+TABLE\b", sql_file.sql, re.IGNORECASE))

    def _execute_temporary_seed_statements(
        self,
        sql_files: List[BaseSqlFile],
        temporary_names: set[str],
        db: DatabaseClient,
    ) -> None:
        for sql_file in sql_files:
            if self._is_temporary_table_file(sql_file):
                continue
            for statement in split_sql_statements(sql_file.sql):
                target = self._insert_target_table(statement)
                if target in temporary_names:
                    db.execute(statement)

    def _insert_target_table(self, statement: str) -> Optional[str]:
        match = re.match(r"\s*INSERT\s+INTO\s+`?(?P<table>[\w$]+)`?", statement, re.IGNORECASE)
        return match.group("table") if match else None

    def _worker(self, worker_id: int) -> TaskWorker | None:
        if worker_id < 0 or worker_id >= len(self._workers):
            return None
        return self._workers[worker_id]

    def _new_generator(self, worker_id: int) -> SQLGenerator:
        if self.random_seed is None:
            return SQLGenerator()
        return SQLGenerator(random_seed=self.random_seed + worker_id)


def _quote_identifier(identifier: str) -> str:
    return f"`{identifier.replace('`', '``')}`"
