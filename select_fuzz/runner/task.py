from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, List, Optional

from select_fuzz.base_tables import BaseSqlBundle, load_base_sql_bundle
from select_fuzz.config import TargetNodeConfig
from select_fuzz.metadata.base_sql import split_sql_statements
from select_fuzz.metadata.models import BaseSqlFile, TableMetadata
from select_fuzz.monitor.events import LostConnectionDeduplicator, LostConnectionEvent, is_lost_connection_error
from select_fuzz.monitor.logs import SqlLogRecord, append_jsonl, append_text_line
from select_fuzz.monitor.store import MetricStore
from select_fuzz.sqlgen.generator import GenerationOptions, SQLGenerator
from select_fuzz.sqlgen.dml import DMLOperation, eligible_v1_permanent_tables
from select_fuzz.sqlgen.registry import create_crud_generator, create_query_generator
from select_fuzz.sqlgen.seeds import (
    CURRENT_CRUD_GENERATOR_VERSION,
    CURRENT_QUERY_GENERATOR_VERSION,
    derive_worker_seed,
)

from .db import DatabaseClient, LostConnectionError


QUERY_MAX_EXECUTION_TIME_MS = 5000


def retry_backoff_seconds(attempt: int) -> float:
    """返回 0.1 秒起步、5 秒封顶且不会随无限重试溢出的退避时间。"""

    if type(attempt) is not int or attempt < 0:
        raise ValueError("重连尝试次数必须是非负整数")
    return min(0.1 * (1 << min(attempt, 6)), 5.0)


class _TaskInitializationStopped(Exception):
    """任务初始化期间收到终态请求；用于立即退出未完成的外部调用序列。"""


class _TaskInitializationPaused(Exception):
    """任务初始化期间收到暂停请求；当前幂等初始化轮次必须放弃。"""


class TaskStatus(str, Enum):
    NEW = "新建"
    CONNECTING = "连接实例"
    SEEDING = "准备基表"
    RUNNING = "执行 SQL"
    RECOVERING = "恢复检测"
    PAUSED = "已暂停"
    FAILED = "失败"
    STOPPED = "已停止"


class InitializationResult(str, Enum):
    SUCCESS = "success"
    RETRY = "retry"
    PAUSED = "paused"
    STOPPED = "stopped"


PASSIVE_CONNECTION_CLOSE_REASON = "worker 连接已被外部关闭或驱动标记不可用，准备重连"


@dataclass
class TaskWorker:
    worker_id: int
    db: DatabaseClient
    generator: Any
    worker_key: str = "query:0"
    worker_type: str = "query"
    db_role: str = "primary"
    target: str = ""
    table_name: Optional[str] = None
    estimated_rows: int = 0
    session_ready: bool = True
    pending_sql: Optional[str] = None
    pending_operation: Optional[str] = None
    has_connected: bool = False
    generator_lock: Any = field(default_factory=threading.RLock, repr=False)


@dataclass
class WorkerRuntimeState:
    worker_id: int
    worker_key: str = "query:0"
    worker_type: str = "query"
    db_role: str = "primary"
    target: str = ""
    table_name: Optional[str] = None
    generator_seed: Optional[str] = None
    generator_version: Optional[str] = None
    operation: Optional[str] = None
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
    reconnecting: bool = False
    reconnect_total: int = 0
    reconnect_attempt: int = 0
    next_retry_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "worker_key": self.worker_key,
            "worker_type": self.worker_type,
            "db_role": self.db_role,
            "target": self.target,
            "table_name": self.table_name,
            "operation": self.operation,
            "generator_seed": self.generator_seed,
            "generator_version": self.generator_version,
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
            "reconnecting": self.reconnecting,
            "reconnect_total": self.reconnect_total,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at is not None else None,
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
    primary_db_factory: Optional[Callable[[], DatabaseClient]] = None
    replica_db_factory: Optional[Callable[[], DatabaseClient]] = None
    thread_count: int = 1
    random_seed: Optional[int] = None
    recovery_probe_seconds: int = 60
    lost_connection_dedup_minutes: int = 10
    expand_base_table_columns: bool = False
    base_table_seed: Optional[str] = None
    base_table_generator_version: Optional[str] = None
    enable_crud: bool = False
    primary_target: Optional[str] = None
    replica_target: Optional[str] = None
    query_seed: Optional[str] = None
    query_generator_version: str = CURRENT_QUERY_GENERATOR_VERSION
    crud_seed: Optional[str] = None
    crud_generator_version: Optional[str] = None
    status: TaskStatus = TaskStatus.NEW
    phase: str = TaskStatus.NEW.value
    last_error: Optional[str] = None
    sql_total: int = 0
    failed_query_total: int = 0
    ordinary_error_total: int = 0
    lost_connection_total: int = 0
    insert_success_total: int = 0
    insert_failed_total: int = 0
    update_success_total: int = 0
    update_failed_total: int = 0
    delete_success_total: int = 0
    delete_failed_total: int = 0
    tables: List[TableMetadata] = field(default_factory=list)
    _dedup: LostConnectionDeduplicator = field(init=False)
    _next_probe_at: Optional[datetime] = None
    _workers: List[TaskWorker] = field(default_factory=list, init=False)
    _worker_states: List[WorkerRuntimeState] = field(default_factory=list, init=False)
    _status_before_pause: TaskStatus = field(default=TaskStatus.NEW, init=False)
    _base_sql_bundle_released: bool = field(default=False, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _initial_table_counts: dict[str, int] = field(default_factory=dict, init=False)
    _role_runtime: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.thread_count < 1:
            raise ValueError("线程数必须大于等于 1")
        self._role_runtime = self.replica_db_factory is not None or self.enable_crud
        if self.thread_count > 1 and self.db_factory is None and self.replica_db_factory is None:
            raise ValueError("多线程任务必须提供 db_factory 以创建独立连接")
        if self.enable_crud and self.primary_db_factory is None:
            raise ValueError("开启 CRUD 时必须提供主库连接工厂")
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
        self.primary_target = self.primary_target or self.node.address
        self.replica_target = self.replica_target or self.primary_target
        if self._role_runtime:
            self._workers = []
            self._worker_states = []
        else:
            worker, state = self._new_legacy_query_worker(0, self.db)
            self._workers = [worker]
            self._worker_states = [state]

    def start(self, *, retry_transient: bool = False) -> InitializationResult:
        try:
            self._set_initialization_state(TaskStatus.SEEDING, "准备基表")
            base_sql_bundle = self._require_base_sql_bundle()
            self._raise_if_initialization_stopped()
            self._set_initialization_state(TaskStatus.CONNECTING, "连接实例")
            self.db.connect()
            self._raise_if_initialization_stopped()
            self._set_initialization_state(TaskStatus.SEEDING, "准备基表")
            self._recreate_database()
            for sql_file in base_sql_bundle.files:
                self._execute_statements(sql_file, self.db)
            self._verify_seed_data(self.db)
            if self._role_runtime:
                self._prepare_role_workers()
            else:
                self._prepare_additional_workers(base_sql_bundle)
            self._set_initialization_state(TaskStatus.RUNNING, "空闲")
            return InitializationResult.SUCCESS
        except _TaskInitializationPaused:
            self._reset_initialization_attempt("任务初始化已暂停")
            return InitializationResult.PAUSED
        except _TaskInitializationStopped:
            self._close_worker_connections("任务初始化已停止")
            return InitializationResult.STOPPED
        except Exception as exc:
            if retry_transient and is_retryable_initialization_error(exc):
                self._reset_initialization_attempt("初始化暂时不可用，准备重试")
                with self._lock:
                    if self.status is TaskStatus.PAUSED:
                        return InitializationResult.PAUSED
                    if self._is_terminal_locked() or self._base_sql_bundle_released:
                        return InitializationResult.STOPPED
                    self.last_error = f"{self.phase}暂时不可用: {exc}"
                    self._write_metrics()
                return InitializationResult.RETRY
            self.fail(exc)
            raise

    def step(self, worker_id: int = 0) -> float:
        worker = self._worker(worker_id)
        if worker is None:
            return 0
        if self._role_runtime:
            return self._role_step(worker)
        with self._lock:
            current_status = self.status
        if current_status is TaskStatus.RECOVERING:
            self._set_worker_state(worker_id, "恢复检测")
            if worker_id == 0:
                self.probe_recovery()
            return 0
        if current_status is TaskStatus.PAUSED:
            self._set_worker_state(worker_id, "已暂停")
            return 0
        if current_status is not TaskStatus.RUNNING:
            return 0

        if not self._ensure_worker_session(worker_id, worker):
            return 0
        self._set_worker_state(worker_id, "生成 SQL")
        try:
            with worker.generator_lock:
                sql = worker.generator.generate(
                    self.tables,
                    GenerationOptions(
                        require_join=len(self.tables) > 1,
                    ),
                )
                sql_metadata = self._generator_sql_metadata(worker.generator)
        except Exception as exc:
            self.fail(exc, phase=TaskStatus.RUNNING.value)
            return 0
        with self._lock:
            if self._is_terminal_locked():
                return 0
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
            return 0
        with self._lock:
            if self._is_terminal_locked():
                return
            self.sql_total += 1
            self._finish_worker_sql_locked(worker_id, "空闲", None, increment_sql_total=True)
            self._write_sql_log("成功", sql, **sql_metadata)
            self._write_metrics()
        return 0

    @property
    def worker_ids(self) -> list[int]:
        with self._lock:
            return [worker.worker_id for worker in self._workers]

    def _prepare_role_workers(self) -> None:
        query_seed = self.query_seed or "0"
        workers: list[TaskWorker] = []
        states: list[WorkerRuntimeState] = []
        replica_factory = self.replica_db_factory or self.db_factory
        if replica_factory is None:
            raise RuntimeError("未配置查询连接工厂")
        try:
            for index in range(self.thread_count):
                self._raise_if_initialization_stopped()
                worker_id = len(workers)
                worker_key = f"query:{index}"
                seed = derive_worker_seed(query_seed, "query", worker_key)
                db = replica_factory()
                try:
                    self._raise_if_initialization_stopped()
                except Exception:
                    try:
                        db.close()
                    except Exception:
                        pass
                    raise
                worker = TaskWorker(
                    worker_id=worker_id,
                    db=db,
                    generator=create_query_generator(
                        self.query_generator_version,
                        seed,
                    ),
                    worker_key=worker_key,
                    worker_type="query",
                    db_role="replica",
                    target=str(self.replica_target),
                    session_ready=False,
                )
                workers.append(worker)
                states.append(
                    WorkerRuntimeState(
                        worker_id=worker_id,
                        worker_key=worker_key,
                        worker_type="query",
                        db_role="replica",
                        target=str(self.replica_target),
                        generator_seed=str(seed),
                        generator_version=self.query_generator_version,
                        last_heartbeat=self.clock(),
                    )
                )
            if self.enable_crud:
                crud_seed = self.crud_seed or "0"
                tables = eligible_v1_permanent_tables(self.tables)
                if len(tables) != 74:
                    raise RuntimeError(f"逐表 CRUD 要求 74 张内置永久表，实际为 {len(tables)} 张")
                assert self.primary_db_factory is not None
                for table_index, table in enumerate(tables):
                    self._raise_if_initialization_stopped()
                    worker_id = len(workers)
                    worker_key = f"dml:{table.name}"
                    seed = derive_worker_seed(crud_seed, "dml", worker_key)
                    db = self.db if table_index == 0 else self.primary_db_factory()
                    try:
                        self._raise_if_initialization_stopped()
                    except Exception:
                        if table_index != 0:
                            try:
                                db.close()
                            except Exception:
                                pass
                        raise
                    worker = TaskWorker(
                        worker_id=worker_id,
                        db=db,
                        generator=create_crud_generator(
                            self.crud_generator_version or CURRENT_CRUD_GENERATOR_VERSION,
                            seed,
                            base_table_seed=self.base_table_seed or "0",
                        ),
                        worker_key=worker_key,
                        worker_type="dml",
                        db_role="primary",
                        target=str(self.primary_target),
                        table_name=table.name,
                        estimated_rows=self._initial_table_counts.get(table.name, 0),
                        session_ready=table_index == 0,
                        has_connected=table_index == 0,
                    )
                    workers.append(worker)
                    states.append(
                        WorkerRuntimeState(
                            worker_id=worker_id,
                            worker_key=worker_key,
                            worker_type="dml",
                            db_role="primary",
                            target=str(self.primary_target),
                            table_name=table.name,
                            generator_seed=str(seed),
                            generator_version=self.crud_generator_version or CURRENT_CRUD_GENERATOR_VERSION,
                            last_heartbeat=self.clock(),
                        )
                    )
        except Exception:
            self._close_databases(workers)
            raise
        with self._lock:
            if self._is_terminal_locked():
                should_close = True
            else:
                self._workers = workers
                self._worker_states = states
                should_close = False
        if should_close:
            self._close_databases(workers)
            raise _TaskInitializationStopped
        if not self.enable_crud:
            try:
                self.db.close()
            except Exception:
                pass

    def _role_step(self, worker: TaskWorker) -> float:
        with self._lock:
            if self.status is TaskStatus.PAUSED:
                self._set_worker_state(worker.worker_id, "已暂停")
                return 0.1
            if self.status is not TaskStatus.RUNNING or self._base_sql_bundle_released:
                return 0
            state = self._worker_state(worker.worker_id)
            if state is None:
                return 0
            if worker.session_ready:
                diagnostics = self._connection_diagnostics(worker.db)
                self._mark_passive_connection_closed_locked(state, diagnostics)
            if state.needs_reconnect and worker.session_ready:
                worker.session_ready = False
                state.reconnecting = True
            if state.next_retry_at is not None and self.clock() < state.next_retry_at:
                return max(0.0, (state.next_retry_at - self.clock()).total_seconds())

        if worker.pending_sql is None:
            if worker.worker_type == "query":
                try:
                    with worker.generator_lock:
                        worker.pending_sql = worker.generator.generate(
                            self.tables,
                            GenerationOptions(
                                require_join=len(self.tables) > 1,
                                allow_locking=False,
                                allow_temporary_tables=False,
                            ),
                        )
                    worker.pending_operation = "SELECT"
                except Exception as exc:
                    self.fail(exc, phase=TaskStatus.RUNNING.value)
                    return 0
            else:
                table = next(table for table in self.tables if table.name == worker.table_name)
                try:
                    with worker.generator_lock:
                        plan = worker.generator.generate(table, worker.estimated_rows)
                except Exception as exc:
                    self.fail(exc, phase=TaskStatus.RUNNING.value)
                    return 0
                if plan.skipped:
                    return 0
                worker.pending_sql = plan.sql
                worker.pending_operation = plan.operation.value
            with self._lock:
                state.operation = worker.pending_operation

        if not worker.session_ready:
            retry = self._connect_role_worker(worker)
            if retry > 0:
                return retry
            with self._lock:
                if self.status is not TaskStatus.RUNNING:
                    return 0

        assert worker.pending_sql is not None
        sql = worker.pending_sql
        self.record_worker_sql_start(
            worker.worker_id,
            sql,
            sql_metadata=self._role_sql_metadata(worker),
        )
        with self._lock:
            if self.status is not TaskStatus.RUNNING or self._base_sql_bundle_released:
                return 0
        try:
            if worker.worker_type == "query":
                self._set_query_execution_timeout(worker.db)
            affected = worker.db.execute(sql)
            affected_rows = int(affected or 0)
        except Exception as exc:
            if (
                isinstance(exc, LostConnectionError)
                or is_lost_connection_error(exc)
                or _is_table_not_ready_error(exc)
            ):
                return self._schedule_worker_reconnect(worker, exc)
            self._record_role_sql_error(worker, sql, exc)
            worker.pending_sql = None
            worker.pending_operation = None
            return 0

        with self._lock:
            if self._is_terminal_locked():
                return 0
            if worker.worker_type == "query":
                self.sql_total += 1
            else:
                self._record_dml_success_locked(worker, affected_rows)
            self._finish_worker_sql_locked(worker.worker_id, "空闲", None, increment_sql_total=True)
            sql_log_metadata = self._role_sql_metadata(worker)
        self._write_sql_log("成功", sql, **sql_log_metadata)
        worker.pending_sql = None
        worker.pending_operation = None
        with self._lock:
            state.reconnect_attempt = 0
            state.next_retry_at = None
        return 0

    def _connect_role_worker(self, worker: TaskWorker) -> float:
        state = self._worker_state(worker.worker_id)
        assert state is not None
        try:
            try:
                worker.db.close()
            except Exception:
                pass
            worker.db.connect()
            with self._lock:
                terminal = self._is_terminal_locked() or self._base_sql_bundle_released
            if terminal:
                try:
                    worker.db.close()
                except Exception:
                    pass
                return 0
            self._execute_initialization_statement(
                worker.db,
                f"USE {_quote_identifier(self._database_name())}",
            )
        except Exception as exc:
            with self._lock:
                terminal = self._is_terminal_locked() or self._base_sql_bundle_released
            if terminal:
                try:
                    worker.db.close()
                except Exception:
                    pass
                return 0
            return self._schedule_worker_reconnect(worker, exc)
        with self._lock:
            terminal = self._is_terminal_locked() or self._base_sql_bundle_released
            if not terminal:
                completed_reconnect = worker.has_connected and bool(
                    state.reconnect_attempt or state.needs_reconnect
                )
                worker.session_ready = True
                worker.has_connected = True
                state.needs_reconnect = False
                state.reconnecting = False
                state.next_retry_at = None
                if completed_reconnect:
                    state.reconnect_total += 1
                state.state = "空闲"
                state.last_error = None
                state.last_heartbeat = self.clock()
        if terminal:
            try:
                worker.db.close()
            except Exception:
                pass
            return 0
        return 0

    def _schedule_worker_reconnect(self, worker: TaskWorker, exc: Exception) -> float:
        try:
            worker.db.close()
        except Exception:
            pass
        with self._lock:
            if self._is_terminal_locked():
                return 0
            worker.session_ready = False
            state = self._worker_state(worker.worker_id)
            assert state is not None
            delay = retry_backoff_seconds(state.reconnect_attempt)
            state.reconnect_attempt += 1
            state.reconnecting = True
            state.needs_reconnect = True
            state.next_retry_at = self.clock() + timedelta(seconds=delay)
            state.state = "已暂停" if self.status is TaskStatus.PAUSED else "等待重连"
            state.last_error = str(exc)
            state.last_connection_close_reason = str(exc)
            state.last_heartbeat = self.clock()
            if not _is_table_not_ready_error(exc):
                self.lost_connection_total += 1
                if isinstance(exc, LostConnectionError) or is_lost_connection_error(exc):
                    self._record_role_lost_connection_locked(worker)
            self._write_metrics()
            return delay

    def _record_role_lost_connection_locked(self, worker: TaskWorker) -> None:
        now = self.clock()
        if not self._dedup.should_record(
            self.node.name,
            now,
            db_role=worker.db_role,
            target=worker.target,
        ):
            return
        state = self._worker_state(worker.worker_id)
        event = LostConnectionEvent(
            timestamp=now,
            task_id=self.task_id,
            node_name=self.node.name,
            jump_host=self.node.jump_host,
            target=worker.target,
            sql=worker.pending_sql or "",
            window_start=now,
            worker_type=worker.worker_type,
            db_role=worker.db_role,
            table_name=worker.table_name,
            operation=worker.pending_operation,
            generator_seed=state.generator_seed if state is not None else None,
            generator_version=state.generator_version if state is not None else None,
        )
        self.metric_store.insert_lost_connection_event(event)
        append_jsonl(self._event_log_path(), event.to_dict())

    def _role_sql_metadata(self, worker: TaskWorker) -> dict:
        metadata = self._generator_sql_metadata(worker.generator) if worker.worker_type == "query" else {}
        metadata.update(
            {
                "worker_key": worker.worker_key,
                "worker_type": worker.worker_type,
                "db_role": worker.db_role,
                "target": worker.target,
                "table_name": worker.table_name,
                "operation": worker.pending_operation,
                "generator_seed": self._worker_state(worker.worker_id).generator_seed,
                "generator_version": self._worker_state(worker.worker_id).generator_version,
            }
        )
        return metadata

    def _record_role_sql_error(self, worker: TaskWorker, sql: str, exc: Exception) -> None:
        sql_log_metadata: dict | None = None
        should_write_failed_sql = False
        with self._lock:
            if self._is_terminal_locked():
                return
            if worker.worker_type == "query":
                self.failed_query_total += 1
                self.ordinary_error_total += 1
            else:
                self._increment_dml_counter_locked(worker.pending_operation, success=False)
            self._finish_worker_sql_locked(worker.worker_id, "空闲", str(exc))
            state = self._worker_state(worker.worker_id)
            if state is not None:
                state.reconnect_attempt = 0
                state.next_retry_at = None
            if not (worker.worker_type == "dml" and _is_silent_dml_conflict(exc)):
                sql_log_metadata = self._role_sql_metadata(worker)
                should_write_failed_sql = True
        if sql_log_metadata is not None:
            if should_write_failed_sql:
                self._write_failed_sql(sql)
            self._write_sql_log("普通错误", sql, str(exc), **sql_log_metadata)

    def _record_dml_success_locked(self, worker: TaskWorker, affected_rows: int) -> None:
        operation = worker.pending_operation
        self._increment_dml_counter_locked(operation, success=True)
        if operation == DMLOperation.INSERT.value:
            worker.estimated_rows += affected_rows
        elif operation == DMLOperation.DELETE.value:
            worker.estimated_rows = max(0, worker.estimated_rows - affected_rows)

    def _increment_dml_counter_locked(self, operation: Optional[str], *, success: bool) -> None:
        if operation not in {item.value for item in DMLOperation}:
            return
        name = operation.lower()
        attribute = f"{name}_{'success' if success else 'failed'}_total"
        setattr(self, attribute, getattr(self, attribute) + 1)

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
            if next_status is TaskStatus.PAUSED:
                next_status = TaskStatus.NEW
            self._set_status_locked(next_status)
            if next_status is TaskStatus.RECOVERING:
                worker_state = "恢复检测"
            elif next_status is TaskStatus.RUNNING:
                worker_state = "空闲"
            else:
                worker_state = next_status.value
            self._set_non_executing_worker_states_locked(worker_state)
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
            primary_reconnect_total = sum(
                state.reconnect_total for state in self._worker_states if state.db_role == "primary"
            )
            replica_reconnect_total = sum(
                state.reconnect_total for state in self._worker_states if state.db_role == "replica"
            )
            primary_reconnecting = sum(
                int(state.reconnecting) for state in self._worker_states if state.db_role == "primary"
            )
            replica_reconnecting = sum(
                int(state.reconnecting) for state in self._worker_states if state.db_role == "replica"
            )
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
                "enable_crud": self.enable_crud,
                "query_seed": self.query_seed,
                "query_generator_version": self.query_generator_version,
                "crud_seed": self.crud_seed,
                "crud_generator_version": self.crud_generator_version,
                "query_worker_total": sum(worker.worker_type == "query" for worker in self._workers),
                "crud_worker_total": sum(worker.worker_type == "dml" for worker in self._workers),
                "worker_total": len(self._workers),
                "insert_success_total": self.insert_success_total,
                "insert_failed_total": self.insert_failed_total,
                "update_success_total": self.update_success_total,
                "update_failed_total": self.update_failed_total,
                "delete_success_total": self.delete_success_total,
                "delete_failed_total": self.delete_failed_total,
                "crud_success_total": self.crud_success_total,
                "crud_failed_total": self.crud_failed_total,
                "primary_reconnect_total": primary_reconnect_total,
                "replica_reconnect_total": replica_reconnect_total,
                "primary_reconnecting": primary_reconnecting,
                "replica_reconnecting": replica_reconnecting,
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
                if self._role_runtime:
                    worker.session_ready = False
                    state.reconnecting = True
                    state.next_retry_at = now
                else:
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
            if worker.session_ready:
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
            try:
                worker.db.close()
            except Exception:
                continue
        if self._role_runtime and all(worker.db is not self.db for worker in self._workers):
            try:
                self.db.close()
            except Exception:
                pass

    def _reset_initialization_attempt(self, reason: str) -> None:
        """关闭本轮所有连接，并恢复到可从 DROP DATABASE 重做的内存边界。"""

        self._close_worker_connections(reason)
        with self._lock:
            self._initial_table_counts.clear()
            if self._role_runtime:
                self._workers = []
                self._worker_states = []
            else:
                worker, state = self._new_legacy_query_worker(0, self.db)
                self._workers = [worker]
                self._worker_states = [state]

    def _write_sql_log(
        self,
        status: str,
        sql: str,
        error_message: Optional[str] = None,
        sql_validity: Optional[str] = None,
        risk_tags: Optional[list[str]] = None,
        expected_error: Optional[bool] = None,
        **extra_fields: object,
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
        payload = record.to_dict()
        payload.update(extra_fields)
        append_jsonl(self._sql_log_path(), payload)

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
            try:
                worker.db.close()
            except Exception:
                continue

    def _is_terminal_locked(self) -> bool:
        return self.status in {TaskStatus.STOPPED, TaskStatus.FAILED}

    def _generator_sql_metadata(self, generator: SQLGenerator) -> dict:
        return {
            "sql_validity": getattr(generator, "last_sql_validity", "合法"),
            "risk_tags": list(getattr(generator, "last_risk_tags", [])),
            "expected_error": bool(getattr(generator, "last_expected_error", False)),
        }

    def _write_failed_sql(self, sql: str) -> None:
        append_text_line(self._failed_sql_path(), sql.strip())

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
    def crud_success_total(self) -> int:
        return self.insert_success_total + self.update_success_total + self.delete_success_total

    @property
    def crud_failed_total(self) -> int:
        return self.insert_failed_total + self.update_failed_total + self.delete_failed_total

    @property
    def coverage_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self._lock:
            workers = list(self._workers)
        for worker in workers:
            with worker.generator_lock:
                worker_counts = dict(getattr(worker.generator, "coverage_counts", {}))
            for name, count in worker_counts.items():
                counts[name] = counts.get(name, 0) + count
        return counts

    @property
    def recent_coverage_hits(self) -> list[str]:
        hits = set()
        with self._lock:
            workers = list(self._workers)
        for worker in workers:
            with worker.generator_lock:
                recent_hits = list(getattr(worker.generator, "recent_hits", []))
            hits.update(recent_hits)
        return sorted(hits)

    def _database_name(self) -> str:
        return self.node.database or "test"

    def _recreate_database(self) -> None:
        database = self._database_name()
        for statement in (
            f"DROP DATABASE IF EXISTS {_quote_identifier(database)}",
            f"CREATE DATABASE {_quote_identifier(database)}",
            f"USE {_quote_identifier(database)}",
        ):
            self._execute_initialization_statement(self.db, statement)

    def _prepare_additional_workers(self, base_sql_bundle: BaseSqlBundle) -> None:
        for worker_id in range(1, self.thread_count):
            assert self.db_factory is not None
            db = self.db_factory()
            try:
                duplicate_client = any(existing.db is db for existing in self._workers)
                if not duplicate_client:
                    self._raise_if_initialization_stopped()
                    db.connect()
                    self._raise_if_initialization_stopped()
                    self._prepare_worker_session(db, base_sql_bundle)
                    self._raise_if_initialization_stopped()
            except Exception:
                db.close()
                raise
            with self._lock:
                if self._is_terminal_locked() or self._base_sql_bundle_released:
                    should_append = False
                else:
                    should_append = True
                    worker, state = self._new_legacy_query_worker(worker_id, db)
                    state.state = "空闲"
                    self._workers.append(worker)
                    self._worker_states.append(state)
            if not should_append:
                db.close()
                raise _TaskInitializationStopped

    def _prepare_worker_session(self, db: DatabaseClient, base_sql_bundle: BaseSqlBundle) -> None:
        self._execute_initialization_statement(db, f"USE {_quote_identifier(self._database_name())}")
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
            self._raise_if_initialization_stopped()
            count = db.query_scalar(f"SELECT COUNT(*) FROM {_quote_identifier(table.name)}")
            self._raise_if_initialization_stopped()
            if count <= 0:
                raise RuntimeError(f"基表初始化未插入数据: {table.name}")
            if db is self.db:
                self._initial_table_counts[table.name] = count

    def _execute_statements(self, sql_file: BaseSqlFile, db: DatabaseClient) -> None:
        for statement in split_sql_statements(sql_file.sql):
            self._execute_initialization_statement(db, statement)

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
                    self._execute_initialization_statement(db, statement)

    def _execute_initialization_statement(self, db: DatabaseClient, statement: str) -> None:
        self._raise_if_initialization_stopped()
        db.execute(statement)
        self._raise_if_initialization_stopped()

    def _raise_if_initialization_stopped(self) -> None:
        with self._lock:
            stopped = self._is_terminal_locked() or self._base_sql_bundle_released
            paused = self.status is TaskStatus.PAUSED
        if stopped:
            raise _TaskInitializationStopped
        if paused:
            raise _TaskInitializationPaused

    def _set_initialization_state(self, status: TaskStatus, worker_state: str) -> None:
        with self._lock:
            if self._is_terminal_locked() or self._base_sql_bundle_released:
                raise _TaskInitializationStopped
            if self.status is TaskStatus.PAUSED:
                raise _TaskInitializationPaused
            self._set_status_locked(status)
            self._set_all_worker_states_locked(worker_state)
            self._write_metrics()

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
        if self.query_seed is not None:
            return create_query_generator(
                self.query_generator_version,
                derive_worker_seed(
                    self.query_seed,
                    "query",
                    f"query:{worker_id}",
                ),
            )
        if self.random_seed is None:
            return create_query_generator(self.query_generator_version, None)
        return create_query_generator(
            self.query_generator_version,
            self.random_seed + worker_id,
        )

    def _new_legacy_query_worker(
        self,
        worker_id: int,
        db: DatabaseClient,
    ) -> tuple[TaskWorker, WorkerRuntimeState]:
        worker_key = f"query:{worker_id}"
        generator_seed: Optional[str]
        if self.query_seed is not None:
            generator_seed = str(derive_worker_seed(self.query_seed, "query", worker_key))
        elif self.random_seed is not None:
            generator_seed = str(self.random_seed + worker_id)
        else:
            generator_seed = None
        worker = TaskWorker(
            worker_id=worker_id,
            db=db,
            generator=self._new_generator(worker_id),
            worker_key=worker_key,
            worker_type="query",
            db_role="replica",
            target=str(self.primary_target),
        )
        state = WorkerRuntimeState(
            worker_id=worker_id,
            worker_key=worker_key,
            worker_type="query",
            db_role="replica",
            target=str(self.primary_target),
            generator_seed=generator_seed,
            generator_version=self.query_generator_version,
            last_heartbeat=self.clock(),
        )
        return worker, state


def _quote_identifier(identifier: str) -> str:
    return f"`{identifier.replace('`', '``')}`"


_SILENT_DML_CONFLICT_CODES = {1062, 1213, 1451, 1452, 3819}
_TABLE_NOT_READY_CODES = {1049, 1146}
_TRANSIENT_CONNECTION_CODES = {2002, 2003, 2005, 2006, 2013, 2055}


def _is_silent_dml_conflict(exc: Exception) -> bool:
    code = getattr(exc, "errno", None)
    if code is None and getattr(exc, "args", None):
        code = exc.args[0]
    try:
        return int(code) in _SILENT_DML_CONFLICT_CODES
    except (TypeError, ValueError):
        return False


def _is_table_not_ready_error(exc: Exception) -> bool:
    code = getattr(exc, "errno", None)
    if code is None and getattr(exc, "args", None):
        code = exc.args[0]
    try:
        return int(code) in _TABLE_NOT_READY_CODES
    except (TypeError, ValueError):
        return False


def is_retryable_initialization_error(exc: Exception) -> bool:
    if _is_table_not_ready_error(exc):
        return True
    if isinstance(exc, (LostConnectionError, ConnectionError, TimeoutError, EOFError)):
        return True
    # sshtunnel 0.4.x 会把 SSH gateway 不可达、会话建立失败和
    # forwarder 启动失败统一包装为该异常，不会保留 socket 异常类型。
    # 对任务生命周期而言它属于连接路径暂时不可用，应按同一退避继续重试。
    try:
        from sshtunnel import BaseSSHTunnelForwarderError
    except ImportError:
        pass
    else:
        if isinstance(exc, BaseSSHTunnelForwarderError):
            return True
    if is_lost_connection_error(exc):
        return True
    code = getattr(exc, "errno", None)
    if code is None and getattr(exc, "args", None):
        code = exc.args[0]
    try:
        return int(code) in _TRANSIENT_CONNECTION_CODES
    except (TypeError, ValueError):
        return False
