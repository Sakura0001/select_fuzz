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
    PAUSED = "已暂停"
    FAILED = "失败"
    STOPPED = "已停止"


@dataclass
class TaskWorker:
    worker_id: int
    db: DatabaseClient
    generator: SQLGenerator


@dataclass
class WorkerRuntimeState:
    worker_id: int
    state: str = "等待启动"
    last_heartbeat: Optional[datetime] = None
    current_sql: Optional[str] = None
    current_sql_started_at: Optional[datetime] = None
    last_error: Optional[str] = None
    sql_total: int = 0
    stalled_total: int = 0

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "state": self.state,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat is not None else None,
            "current_sql": self.current_sql,
            "current_sql_started_at": self.current_sql_started_at.isoformat()
            if self.current_sql_started_at is not None
            else None,
            "last_error": self.last_error,
            "sql_total": self.sql_total,
            "stalled_total": self.stalled_total,
        }


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
    phase: str = TaskStatus.NEW.value
    last_error: Optional[str] = None
    sql_total: int = 0
    failed_query_total: int = 0
    ordinary_error_total: int = 0
    lost_connection_total: int = 0
    tables: List[TableMetadata] = field(default_factory=list)
    _dedup: LostConnectionDeduplicator = field(init=False)
    _next_probe_at: Optional[datetime] = None
    _workers: List[TaskWorker] = field(default_factory=list, init=False)
    _worker_states: List[WorkerRuntimeState] = field(default_factory=list, init=False)
    _status_before_pause: TaskStatus = field(default=TaskStatus.NEW, init=False)
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
        self._worker_states = [WorkerRuntimeState(worker_id=0, last_heartbeat=self.clock())]

    def start(self) -> None:
        try:
            self._set_status(TaskStatus.CONNECTING)
            self._set_all_worker_states("连接实例")
            self.db.connect()
            self._set_status(TaskStatus.SEEDING)
            self._set_all_worker_states("准备基表")
            self._recreate_database()
            self.tables.clear()
            sql_files = load_base_sql_files(self.base_sql_dir)
            for sql_file in sql_files:
                try:
                    self.tables.append(parse_create_table(sql_file.sql))
                except ValueError:
                    continue
            if not self.tables:
                raise RuntimeError("至少需要一张可解析的基表才能启动任务")
            for sql_file in sql_files:
                self._execute_statements(sql_file, self.db)
            self._verify_seed_data(self.db)
            self._prepare_additional_workers(sql_files)
            self._set_status(TaskStatus.RUNNING)
            self._set_all_worker_states("空闲")
        except Exception as exc:
            self.fail(exc)
            raise

    def step(self, worker_id: int = 0) -> None:
        worker = self._worker(worker_id)
        if worker is None:
            return
        with self._lock:
            current_status = self.status
        if current_status is TaskStatus.RECOVERING:
            self._set_worker_state(worker_id, "恢复检测")
            if worker_id == 0:
                self.probe_recovery()
            return
        if current_status is TaskStatus.PAUSED:
            self._set_worker_state(worker_id, "已暂停")
            return
        if current_status is not TaskStatus.RUNNING:
            return

        self._set_worker_state(worker_id, "生成 SQL")
        try:
            sql = worker.generator.generate(
                self.tables,
                GenerationOptions(
                    require_join=len(self.tables) > 1,
                    require_vector=any(any(col.type_family.value == "向量" for col in table.columns.values()) for table in self.tables),
                ),
            )
        except Exception as exc:
            self.fail(exc, phase=TaskStatus.RUNNING.value)
            return
        self.record_worker_sql_start(worker_id, sql)
        try:
            worker.db.execute(sql)
        except Exception as exc:
            if isinstance(exc, LostConnectionError) or is_lost_connection_error(exc):
                self._finish_worker_sql(worker_id, "恢复检测", str(exc))
                self._handle_lost_connection(sql)
                return
            with self._lock:
                self.ordinary_error_total += 1
                self.failed_query_total += 1
                self._finish_worker_sql_locked(worker_id, "空闲", str(exc))
                self._write_sql_log("普通错误", sql)
                self._write_failed_sql(sql)
                self._write_metrics()
            return
        with self._lock:
            self.sql_total += 1
            self._finish_worker_sql_locked(worker_id, "空闲", None, increment_sql_total=True)
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
                    self._set_all_worker_states_locked("恢复检测")
                    self._write_metrics()
                return
            with self._lock:
                self._set_status_locked(TaskStatus.RUNNING)
                self._next_probe_at = None
                self._set_all_worker_states_locked("空闲")
                self._write_metrics()
            return

        with self._lock:
            self._next_probe_at = now + timedelta(seconds=self.recovery_probe_seconds)
            self._set_all_worker_states_locked("恢复检测")
            self._write_metrics()

    def stop(self) -> None:
        self._close_worker_connections()
        with self._lock:
            self._set_status_locked(TaskStatus.STOPPED)
            self._set_all_worker_states_locked("已停止")
            self._write_metrics()

    def pause(self) -> None:
        with self._lock:
            if self.status in {TaskStatus.STOPPED, TaskStatus.FAILED, TaskStatus.PAUSED}:
                return
            self._status_before_pause = self.status
            self._set_status_locked(TaskStatus.PAUSED)
            self._set_non_executing_worker_states_locked("已暂停")
            self._write_metrics()

    def resume(self) -> None:
        with self._lock:
            if self.status is not TaskStatus.PAUSED:
                return
            next_status = self._status_before_pause
            if next_status in {TaskStatus.NEW, TaskStatus.CONNECTING, TaskStatus.SEEDING, TaskStatus.PAUSED}:
                next_status = TaskStatus.RUNNING
            self._set_status_locked(next_status)
            self._set_non_executing_worker_states_locked("恢复检测" if next_status is TaskStatus.RECOVERING else "空闲")
            self._write_metrics()

    def fail(self, exc: Exception, phase: Optional[str] = None) -> None:
        self._mark_failed(exc, phase)
        self._close_worker_connections()

    @property
    def is_terminal(self) -> bool:
        with self._lock:
            return self.status in {TaskStatus.STOPPED, TaskStatus.FAILED}

    @property
    def worker_states(self) -> list[dict]:
        with self._lock:
            return [state.to_dict() for state in self._worker_states]

    def snapshot_counts(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "phase": self.phase,
                "last_error": self.last_error,
                "sql_total": self.sql_total,
                "success_query_total": self.sql_total,
                "failed_query_total": self.failed_query_total,
                "ordinary_error_total": self.ordinary_error_total,
                "lost_connection_total": self.lost_connection_total,
                "worker_states": [state.to_dict() for state in self._worker_states],
            }

    def record_worker_sql_start(self, worker_id: int, sql: str, started_at: Optional[datetime] = None) -> None:
        with self._lock:
            state = self._worker_state(worker_id)
            if state is None:
                return
            now = started_at or self.clock()
            state.state = "执行 SQL"
            state.current_sql = sql
            state.current_sql_started_at = now
            state.last_heartbeat = now
            state.last_error = None

    def interrupt_stalled_workers(self, timeout_seconds: int) -> list[int]:
        if timeout_seconds <= 0:
            return []
        now = self.clock()
        stalled_workers: list[TaskWorker] = []
        with self._lock:
            for worker, state in zip(self._workers, self._worker_states):
                if state.state != "执行 SQL" or state.current_sql_started_at is None:
                    continue
                if now - state.current_sql_started_at <= timedelta(seconds=timeout_seconds):
                    continue
                state.state = "疑似卡住"
                state.last_heartbeat = now
                state.last_error = f"worker {worker.worker_id} 执行 SQL 超过 {timeout_seconds} 秒，已关闭连接"
                state.stalled_total += 1
                stalled_workers.append(worker)
        for worker in stalled_workers:
            worker.db.close()
        if stalled_workers:
            with self._lock:
                self._write_metrics()
        return [worker.worker_id for worker in stalled_workers]

    def _handle_lost_connection(self, sql: str) -> None:
        with self._lock:
            now = self.clock()
            self.failed_query_total += 1
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
            self._set_status_locked(TaskStatus.RECOVERING)
            self._set_all_worker_states_locked("恢复检测")
            self._next_probe_at = now + timedelta(seconds=self.recovery_probe_seconds)
            self._write_metrics()

    def _set_status(self, status: TaskStatus) -> None:
        with self._lock:
            self._set_status_locked(status)
            self._write_metrics()

    def _set_status_locked(self, status: TaskStatus) -> None:
        self.status = status
        self.phase = status.value
        if status is not TaskStatus.FAILED:
            self.last_error = None

    def _mark_failed(self, exc: Exception, phase: Optional[str] = None) -> None:
        with self._lock:
            failed_phase = phase or self.phase or self.status.value
            self.status = TaskStatus.FAILED
            self.phase = failed_phase
            self.last_error = f"{failed_phase}失败: {exc}"
            self._set_all_worker_states_locked("失败", self.last_error)
            self._write_metrics()

    def _set_all_worker_states(self, state: str, error: Optional[str] = None) -> None:
        with self._lock:
            self._set_all_worker_states_locked(state, error)

    def _set_all_worker_states_locked(self, state: str, error: Optional[str] = None) -> None:
        now = self.clock()
        for worker_state in self._worker_states:
            worker_state.state = state
            worker_state.last_heartbeat = now
            worker_state.last_error = error
            if state != "执行 SQL":
                worker_state.current_sql = None
                worker_state.current_sql_started_at = None

    def _set_non_executing_worker_states_locked(self, state: str, error: Optional[str] = None) -> None:
        now = self.clock()
        for worker_state in self._worker_states:
            if worker_state.state == "执行 SQL":
                continue
            worker_state.state = state
            worker_state.last_heartbeat = now
            worker_state.last_error = error
            worker_state.current_sql = None
            worker_state.current_sql_started_at = None

    def _set_worker_state(self, worker_id: int, state: str, error: Optional[str] = None) -> None:
        with self._lock:
            worker_state = self._worker_state(worker_id)
            if worker_state is None:
                return
            worker_state.state = state
            worker_state.last_heartbeat = self.clock()
            worker_state.last_error = error
            if state != "执行 SQL":
                worker_state.current_sql = None
                worker_state.current_sql_started_at = None

    def _finish_worker_sql(
        self,
        worker_id: int,
        state: str,
        error: Optional[str],
        increment_sql_total: bool = False,
    ) -> None:
        with self._lock:
            self._finish_worker_sql_locked(worker_id, state, error, increment_sql_total)

    def _finish_worker_sql_locked(
        self,
        worker_id: int,
        state: str,
        error: Optional[str],
        increment_sql_total: bool = False,
    ) -> None:
        worker_state = self._worker_state(worker_id)
        if worker_state is None:
            return
        worker_state.state = "已暂停" if self.status is TaskStatus.PAUSED and state == "空闲" else state
        worker_state.last_heartbeat = self.clock()
        worker_state.current_sql = None
        worker_state.current_sql_started_at = None
        worker_state.last_error = error
        if increment_sql_total:
            worker_state.sql_total += 1

    def _worker_state(self, worker_id: int) -> WorkerRuntimeState | None:
        if worker_id < 0 or worker_id >= len(self._worker_states):
            return None
        return self._worker_states[worker_id]

    def _close_worker_connections(self) -> None:
        for worker in list(self._workers):
            worker.db.close()

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
    def success_query_total(self) -> int:
        return self.sql_total

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
            try:
                db.connect()
                self._prepare_worker_session(db, sql_files)
            except Exception:
                db.close()
                raise
            self._workers.append(TaskWorker(worker_id=worker_id, db=db, generator=self._new_generator(worker_id)))
            self._worker_states.append(WorkerRuntimeState(worker_id=worker_id, state="空闲", last_heartbeat=self.clock()))

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
