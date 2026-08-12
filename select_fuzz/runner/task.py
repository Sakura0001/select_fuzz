from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional

from select_fuzz.base_tables import BaseSqlBundle, load_base_sql_bundle
from select_fuzz.config import TargetNodeConfig
from select_fuzz.metadata.base_sql import split_sql_statements
from select_fuzz.metadata.models import BaseSqlFile, TableMetadata
from select_fuzz.monitor.events import LostConnectionDeduplicator, LostConnectionEvent, is_lost_connection_error
from select_fuzz.monitor.logs import SqlLogRecord, append_jsonl
from select_fuzz.monitor.store import MetricStore
from select_fuzz.sqlgen.generator import GenerationOptions, SQLGenerator

from .db import DatabaseClient, LostConnectionError


QUERY_MAX_EXECUTION_TIME_MS = 5000


class TaskStatus(str, Enum):
    NEW = "新建"
    CONNECTING = "连接实例"
    SEEDING = "准备基表"
    RUNNING = "执行 SQL"
    RECOVERING = "恢复检测"
    PAUSED = "已暂停"
    FAILED = "失败"
    STOPPED = "已停止"


PASSIVE_CONNECTION_CLOSE_REASON = "worker 连接已被外部关闭或驱动标记不可用，准备重连"


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
    current_sql_metadata: Optional[dict] = None
    last_error: Optional[str] = None
    sql_total: int = 0
    stalled_total: int = 0
    needs_reconnect: bool = False
    last_connection_close_reason: Optional[str] = None

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
            "needs_reconnect": self.needs_reconnect,
            "last_connection_close_reason": self.last_connection_close_reason,
        }


@dataclass
class FuzzTask:
    task_id: str
    node: TargetNodeConfig
    db: DatabaseClient
    metric_store: MetricStore
    log_dir: Path
    clock: Callable[[], datetime]
    base_sql_dir: Path | None = None
    base_sql_bundle: BaseSqlBundle | None = None
    failed_sql_dir: Path | None = None
    db_factory: Optional[Callable[[], DatabaseClient]] = None
    thread_count: int = 1
    random_seed: Optional[int] = None
    recovery_probe_seconds: int = 60
    lost_connection_dedup_minutes: int = 10
    expand_base_table_columns: bool = False
    base_table_seed: Optional[str] = None
    base_table_generator_version: Optional[str] = None
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
    _base_sql_bundle_released: bool = field(default=False, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    def __post_init__(self) -> None:
        if self.thread_count < 1:
            raise ValueError("线程数必须大于等于 1")
        if self.thread_count > 1 and self.db_factory is None:
            raise ValueError("多线程任务必须提供 db_factory 以创建独立连接")
        if self.base_sql_dir is None and self.base_sql_bundle is None:
            raise ValueError("必须提供 base_sql_dir 或 base_sql_bundle")
        if self.base_sql_dir is not None:
            self.base_sql_dir = Path(self.base_sql_dir)
        if self.base_sql_bundle is not None:
            self.expand_base_table_columns = self.base_sql_bundle.expand_base_table_columns
            self.base_table_seed = self.base_sql_bundle.seed
            self.base_table_generator_version = self.base_sql_bundle.generator_version
            self.tables = list(self.base_sql_bundle.tables)
        self.log_dir = Path(self.log_dir)
        self.failed_sql_dir = Path(self.failed_sql_dir) if self.failed_sql_dir is not None else self.log_dir / "failed_sql"
        self._dedup = LostConnectionDeduplicator(timedelta(minutes=self.lost_connection_dedup_minutes))
        self._workers = [TaskWorker(worker_id=0, db=self.db, generator=self._new_generator(0))]
        self._worker_states = [WorkerRuntimeState(worker_id=0, last_heartbeat=self.clock())]

    def start(self) -> None:
        try:
            self._set_status(TaskStatus.SEEDING)
            self._set_all_worker_states("准备基表")
            base_sql_bundle = self._require_base_sql_bundle()
            self._set_status(TaskStatus.CONNECTING)
            self._set_all_worker_states("连接实例")
            self.db.connect()
            self._set_status(TaskStatus.SEEDING)
            self._set_all_worker_states("准备基表")
            self._recreate_database()
            for sql_file in base_sql_bundle.files:
                self._execute_statements(sql_file, self.db)
            self._verify_seed_data(self.db)
            self._prepare_additional_workers(base_sql_bundle)
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

        if not self._ensure_worker_session(worker_id, worker):
            return
        self._set_worker_state(worker_id, "生成 SQL")
        try:
            sql = worker.generator.generate(
                self.tables,
                GenerationOptions(
                    require_join=len(self.tables) > 1,
                ),
            )
            sql_metadata = self._generator_sql_metadata(worker.generator)
        except Exception as exc:
            self.fail(exc, phase=TaskStatus.RUNNING.value)
            return
        with self._lock:
            if self._is_terminal_locked():
                return
        self.record_worker_sql_start(worker_id, sql, sql_metadata=sql_metadata)
        with self._lock:
            if self.status is not TaskStatus.RUNNING or self._base_sql_bundle_released:
                return
        try:
            self._set_query_execution_timeout(worker.db)
            worker.db.execute(sql)
        except Exception as exc:
            with self._lock:
                if self._is_terminal_locked():
                    return
            if isinstance(exc, LostConnectionError) or is_lost_connection_error(exc):
                self._finish_worker_sql(worker_id, "恢复检测", str(exc))
                self._handle_lost_connection(sql, str(exc), sql_metadata)
                return
            with self._lock:
                if self._is_terminal_locked():
                    return
                self.ordinary_error_total += 1
                self.failed_query_total += 1
                self._finish_worker_sql_locked(worker_id, "空闲", str(exc))
                self._write_sql_log("普通错误", sql, str(exc), **sql_metadata)
                self._write_failed_sql(sql)
                self._write_metrics()
            return
        with self._lock:
            if self._is_terminal_locked():
                return
            self.sql_total += 1
            self._finish_worker_sql_locked(worker_id, "空闲", None, increment_sql_total=True)
            self._write_sql_log("成功", sql, **sql_metadata)
            self._write_metrics()

    def probe_recovery(self) -> None:
        with self._lock:
            if self.status is not TaskStatus.RECOVERING:
                return
            now = self.clock()
            if self._next_probe_at is not None and now < self._next_probe_at:
                return
            workers = list(self._workers)

        all_connections_alive = all(worker.db.ping() for worker in workers)
        with self._lock:
            if self.status is not TaskStatus.RECOVERING:
                should_close = True
            else:
                should_close = False
        if should_close:
            self._close_databases(workers)
            return

        with self._lock:
            if self.status is not TaskStatus.RECOVERING:
                return

        if all_connections_alive:
            try:
                base_sql_bundle = self._require_base_sql_bundle()
                for worker in workers:
                    self._prepare_worker_session(worker.db, base_sql_bundle)
                    with self._lock:
                        should_close = self.status is not TaskStatus.RECOVERING
                    if should_close:
                        self._close_databases(workers)
                        return
            except Exception:
                with self._lock:
                    if self.status is not TaskStatus.RECOVERING:
                        return
                    self._next_probe_at = now + timedelta(seconds=self.recovery_probe_seconds)
                    self._set_all_worker_states_locked("恢复检测")
                    self._write_metrics()
                return
            with self._lock:
                if self.status is not TaskStatus.RECOVERING:
                    return
                self._set_status_locked(TaskStatus.RUNNING)
                self._next_probe_at = None
                self._set_all_worker_states_locked("空闲")
                self._write_metrics()
            return

        with self._lock:
            if self.status is not TaskStatus.RECOVERING:
                return
            self._next_probe_at = now + timedelta(seconds=self.recovery_probe_seconds)
            self._set_all_worker_states_locked("恢复检测")
            self._write_metrics()

    def stop(self) -> None:
        with self._lock:
            if self._is_terminal_locked():
                return
            self._set_status_locked(TaskStatus.STOPPED)
            self._set_all_worker_states_locked("已停止")
            self._write_metrics()
        self._close_worker_connections("停止任务")
        self._release_base_sql_bundle()

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
        if not self._mark_failed(exc, phase):
            return
        self._close_worker_connections("任务失败")
        self._release_base_sql_bundle()

    @property
    def is_terminal(self) -> bool:
        with self._lock:
            return self.status in {TaskStatus.STOPPED, TaskStatus.FAILED}

    @property
    def worker_states(self) -> list[dict]:
        with self._lock:
            return self._worker_state_dicts_locked()

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
                "worker_states": self._worker_state_dicts_locked(),
                "expand_base_table_columns": self.expand_base_table_columns,
                "base_table_seed": self.base_table_seed,
                "base_table_generator_version": self.base_table_generator_version,
            }

    def record_worker_sql_start(
        self,
        worker_id: int,
        sql: str,
        started_at: Optional[datetime] = None,
        sql_metadata: Optional[dict] = None,
    ) -> None:
        with self._lock:
            if self._is_terminal_locked():
                return
            state = self._worker_state(worker_id)
            if state is None:
                return
            now = started_at or self.clock()
            state.state = "执行 SQL"
            state.current_sql = sql
            state.current_sql_started_at = now
            state.current_sql_metadata = dict(sql_metadata or {})
            state.last_heartbeat = now
            state.last_error = None

    def interrupt_stalled_workers(self, timeout_seconds: int) -> list[int]:
        if timeout_seconds <= 0:
            return []
        now = self.clock()
        stalled_workers: list[TaskWorker] = []
        stalled_records: list[tuple[str, str, dict]] = []
        with self._lock:
            if self._is_terminal_locked():
                return []
            for worker, state in zip(self._workers, self._worker_states):
                if state.state != "执行 SQL" or state.current_sql_started_at is None:
                    continue
                if now - state.current_sql_started_at <= timedelta(seconds=timeout_seconds):
                    continue
                message = f"worker {worker.worker_id} 执行 SQL 超过 {timeout_seconds} 秒，已中断并准备重连"
                state.state = "疑似卡住"
                state.last_heartbeat = now
                state.last_error = message
                state.stalled_total += 1
                state.needs_reconnect = True
                state.last_connection_close_reason = message
                self.failed_query_total += 1
                self.last_error = message
                if state.current_sql is not None:
                    stalled_records.append((state.current_sql, message, dict(state.current_sql_metadata or {})))
                stalled_workers.append(worker)
            for sql, message, sql_metadata in stalled_records:
                self._write_sql_log("疑似卡住", sql, message, **sql_metadata)
                self._write_failed_sql(sql)
        for worker in stalled_workers:
            worker.db.close()
        if stalled_workers:
            with self._lock:
                self._write_metrics()
        return [worker.worker_id for worker in stalled_workers]

    def _handle_lost_connection(self, sql: str, error_message: str, sql_metadata: Optional[dict] = None) -> None:
        with self._lock:
            if self._is_terminal_locked():
                return
            now = self.clock()
            self.failed_query_total += 1
            self._write_sql_log("lost connection", sql, error_message, **(sql_metadata or {}))
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

    def _set_status_locked(self, status: TaskStatus) -> bool:
        if self._is_terminal_locked():
            return self.status is status
        self.status = status
        self.phase = status.value
        if status is not TaskStatus.FAILED:
            self.last_error = None
        return True

    def _mark_failed(self, exc: Exception, phase: Optional[str] = None) -> bool:
        with self._lock:
            if self._is_terminal_locked():
                return False
            failed_phase = phase or self.phase or self.status.value
            self.status = TaskStatus.FAILED
            self.phase = failed_phase
            self.last_error = f"{failed_phase}失败: {exc}"
            self._set_all_worker_states_locked("失败", self.last_error)
            self._write_metrics()
            return True

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
                worker_state.current_sql_metadata = None

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
            worker_state.current_sql_metadata = None

    def _set_worker_state(self, worker_id: int, state: str, error: Optional[str] = None) -> None:
        with self._lock:
            if self._is_terminal_locked():
                return
            worker_state = self._worker_state(worker_id)
            if worker_state is None:
                return
            worker_state.state = state
            worker_state.last_heartbeat = self.clock()
            worker_state.last_error = error
            if state != "执行 SQL":
                worker_state.current_sql = None
                worker_state.current_sql_started_at = None
                worker_state.current_sql_metadata = None

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
        if self._is_terminal_locked():
            return
        worker_state = self._worker_state(worker_id)
        if worker_state is None:
            return
        worker_state.state = "已暂停" if self.status is TaskStatus.PAUSED and state == "空闲" else state
        worker_state.last_heartbeat = self.clock()
        worker_state.current_sql = None
        worker_state.current_sql_started_at = None
        worker_state.current_sql_metadata = None
        worker_state.last_error = error
        if increment_sql_total:
            worker_state.sql_total += 1

    def _worker_state(self, worker_id: int) -> WorkerRuntimeState | None:
        if worker_id < 0 or worker_id >= len(self._worker_states):
            return None
        return self._worker_states[worker_id]

    def _worker_state_dicts_locked(self) -> list[dict]:
        rows: list[dict] = []
        for worker, state in zip(self._workers, self._worker_states):
            diagnostics = self._connection_diagnostics(worker.db)
            self._mark_passive_connection_closed_locked(state, diagnostics)
            row = state.to_dict()
            row.update(diagnostics)
            rows.append(row)
        return rows

    def _connection_diagnostics(self, db: DatabaseClient) -> dict:
        diagnostics = getattr(db, "connection_diagnostics", None)
        if callable(diagnostics):
            return diagnostics()
        if hasattr(db, "connected"):
            return {"connection_open": bool(getattr(db, "connected"))}
        return {}

    def _mark_passive_connection_closed_locked(self, state: WorkerRuntimeState, diagnostics: dict) -> None:
        if self.status in {TaskStatus.NEW, TaskStatus.CONNECTING, TaskStatus.SEEDING, TaskStatus.FAILED, TaskStatus.STOPPED}:
            return
        if state.state == "执行 SQL" or state.needs_reconnect:
            return
        if diagnostics.get("connection_open") is not False:
            return
        connect_count = diagnostics.get("connection_connect_count")
        close_count = diagnostics.get("connection_close_count", 0)
        if connect_count is not None and int(connect_count or 0) <= int(close_count or 0):
            return
        state.needs_reconnect = True
        state.last_connection_close_reason = PASSIVE_CONNECTION_CLOSE_REASON
        state.last_heartbeat = self.clock()
        if state.state != "已暂停":
            state.state = "等待重连"

    def _close_worker_connections(self, reason: str = "关闭 worker 连接") -> None:
        with self._lock:
            for state in self._worker_states:
                state.last_connection_close_reason = reason
        for worker in list(self._workers):
            worker.db.close()

    def _write_sql_log(
        self,
        status: str,
        sql: str,
        error_message: Optional[str] = None,
        sql_validity: Optional[str] = None,
        risk_tags: Optional[list[str]] = None,
        expected_error: Optional[bool] = None,
    ) -> None:
        record = SqlLogRecord(
            timestamp=self.clock(),
            task_id=self.task_id,
            node_name=self.node.name,
            status=status,
            sql=sql,
            error_message=error_message,
            sql_validity=sql_validity,
            risk_tags=risk_tags,
            expected_error=expected_error,
            expand_base_table_columns=self.expand_base_table_columns,
            base_table_seed=self.base_table_seed,
            base_table_generator_version=self.base_table_generator_version,
        )
        append_jsonl(self._sql_log_path(), record.to_dict())

    def _ensure_worker_session(self, worker_id: int, worker: TaskWorker) -> bool:
        with self._lock:
            if self._is_terminal_locked() or self._base_sql_bundle_released:
                return False
            state = self._worker_state(worker_id)
            diagnostics = self._connection_diagnostics(worker.db)
            if state is not None:
                self._mark_passive_connection_closed_locked(state, diagnostics)
            needs_reconnect = bool(state and state.needs_reconnect)
        if not needs_reconnect:
            return True

        self._set_worker_state(worker_id, "恢复 worker 会话")
        try:
            worker.db.close()
            worker.db.connect()
            with self._lock:
                terminal_after_connect = self._is_terminal_locked() or self._base_sql_bundle_released
            if terminal_after_connect:
                worker.db.close()
                return False
            self._prepare_worker_session(worker.db, self._require_base_sql_bundle())
        except Exception as exc:
            with self._lock:
                terminal = self._is_terminal_locked() or self._base_sql_bundle_released
            if terminal:
                worker.db.close()
                return False
            self.fail(exc, phase="恢复 worker 会话")
            return False

        with self._lock:
            if self._is_terminal_locked() or self._base_sql_bundle_released:
                terminal = True
            else:
                terminal = False
            if not terminal:
                state = self._worker_state(worker_id)
                if state is not None:
                    state.needs_reconnect = False
                    state.state = "空闲"
                    state.current_sql = None
                    state.current_sql_started_at = None
                    state.current_sql_metadata = None
                    state.last_error = None
                    state.last_heartbeat = self.clock()
                self._write_metrics()
        if terminal:
            worker.db.close()
            return False
        return True

    @staticmethod
    def _close_databases(workers: list[TaskWorker]) -> None:
        for worker in workers:
            worker.db.close()

    def _is_terminal_locked(self) -> bool:
        return self.status in {TaskStatus.STOPPED, TaskStatus.FAILED}

    def _generator_sql_metadata(self, generator: SQLGenerator) -> dict:
        return {
            "sql_validity": getattr(generator, "last_sql_validity", "合法"),
            "risk_tags": list(getattr(generator, "last_risk_tags", [])),
            "expected_error": bool(getattr(generator, "last_expected_error", False)),
        }

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

    def _prepare_additional_workers(self, base_sql_bundle: BaseSqlBundle) -> None:
        for worker_id in range(1, self.thread_count):
            assert self.db_factory is not None
            db = self.db_factory()
            try:
                db.connect()
                self._prepare_worker_session(db, base_sql_bundle)
            except Exception:
                db.close()
                raise
            self._workers.append(TaskWorker(worker_id=worker_id, db=db, generator=self._new_generator(worker_id)))
            self._worker_states.append(WorkerRuntimeState(worker_id=worker_id, state="空闲", last_heartbeat=self.clock()))

    def _prepare_worker_session(self, db: DatabaseClient, base_sql_bundle: BaseSqlBundle) -> None:
        db.execute(f"USE {_quote_identifier(self._database_name())}")
        temporary_names = {table.name for table in self.tables if table.is_temporary}
        if not temporary_names:
            return
        for sql_file in base_sql_bundle.files:
            if self._is_temporary_table_file(sql_file):
                self._execute_statements(sql_file, db)
        self._execute_temporary_seed_statements(base_sql_bundle.files, temporary_names, db)
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

    def _set_query_execution_timeout(self, db: DatabaseClient) -> None:
        db.execute(f"SET SESSION max_execution_time = {QUERY_MAX_EXECUTION_TIME_MS}")

    def _is_temporary_table_file(self, sql_file: BaseSqlFile) -> bool:
        return bool(re.search(r"\bCREATE\s+TEMPORARY\s+TABLE\b", sql_file.sql, re.IGNORECASE))

    def _execute_temporary_seed_statements(
        self,
        sql_files: tuple[BaseSqlFile, ...],
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

    def _require_base_sql_bundle(self) -> BaseSqlBundle:
        with self._lock:
            if self.base_sql_bundle is not None:
                return self.base_sql_bundle
            if self._base_sql_bundle_released:
                raise RuntimeError("任务已结束，基表 SQL 内存包已释放")
            if self.base_sql_dir is None:
                raise RuntimeError("未提供可加载的基表目录")
            bundle = load_base_sql_bundle(self.base_sql_dir)
            self.base_sql_bundle = bundle
            self.tables = list(bundle.tables)
            self.expand_base_table_columns = bundle.expand_base_table_columns
            self.base_table_seed = bundle.seed
            self.base_table_generator_version = bundle.generator_version
            return bundle

    def _release_base_sql_bundle(self) -> None:
        with self._lock:
            self.base_sql_bundle = None
            self._base_sql_bundle_released = True

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
