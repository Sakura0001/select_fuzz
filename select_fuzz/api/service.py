from __future__ import annotations

import itertools
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from select_fuzz.base_tables import (
    CURRENT_BASE_TABLE_GENERATOR_VERSION,
    BaseSqlBundle,
    generate_base_sql_bundle,
    load_base_sql_bundle,
)
from select_fuzz.config import JumpHostConfig, TargetNodeConfig
from select_fuzz.monitor.logs import read_jsonl
from select_fuzz.monitor.store import MetricStore
from select_fuzz.runner.db import DatabaseClient
from select_fuzz.runner.jump import JumpTunnel
from select_fuzz.runner.task import (
    FuzzTask,
    InitializationResult,
    TaskStatus,
    is_retryable_initialization_error,
    retry_backoff_seconds,
)
from select_fuzz.sqlgen.operators import build_operator_registry
from select_fuzz.sqlgen.seeds import CURRENT_CRUD_GENERATOR_VERSION, CURRENT_QUERY_GENERATOR_VERSION

from .schemas import TaskCreateRequest


BUILTIN_BASE_SQL_DIR = Path(__file__).resolve().parents[2] / "sql_base_tables"
BACKGROUND_JOIN_TIMEOUT_SECONDS = 0.2


@dataclass
class TaskSnapshot:
    task_id: str
    node_name: str
    target: str
    primary_target: str = ""
    replica_target: str = ""
    replica_host: Optional[str] = None
    replica_port: Optional[int] = None
    status: str = TaskStatus.NEW.value
    phase: str = TaskStatus.NEW.value
    last_error: Optional[str] = None
    database: str = "test"
    jump_host: Optional[str] = None
    thread_count: int = 16
    enable_crud: bool = False
    query_seed: Optional[str] = None
    query_generator_version: Optional[str] = None
    crud_seed: Optional[str] = None
    crud_generator_version: Optional[str] = None
    query_worker_total: int = 0
    crud_worker_total: int = 0
    worker_total: int = 0
    sql_total: int = 0
    success_query_total: int = 0
    failed_query_total: int = 0
    ordinary_error_total: int = 0
    insert_success_total: int = 0
    insert_failed_total: int = 0
    update_success_total: int = 0
    update_failed_total: int = 0
    delete_success_total: int = 0
    delete_failed_total: int = 0
    crud_success_total: int = 0
    crud_failed_total: int = 0
    primary_reconnect_total: int = 0
    replica_reconnect_total: int = 0
    primary_reconnecting: int = 0
    replica_reconnecting: int = 0
    lost_connection_total: int = 0
    sql_rate: float = 0
    worker_states: list[dict] = field(default_factory=list)
    expand_base_table_columns: bool = False
    base_table_seed: Optional[str] = None
    base_table_generator_version: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class RuntimeService:
    def __init__(
        self,
        metric_store: MetricStore,
        log_dir: Path | str,
        failed_sql_dir: Path | str | None = None,
        base_sql_dir: Path | str | None = None,
        use_builtin_base_tables: bool = False,
        db_factory: Optional[Callable[[TargetNodeConfig], DatabaseClient]] = None,
        run_background: bool = True,
        query_interval_seconds: float = 0.05,
        worker_stall_seconds: int = 120,
        initialization_wait: Optional[Callable[[threading.Event, float | None], bool]] = None,
    ) -> None:
        self.metric_store = metric_store
        self.log_dir = Path(log_dir)
        self.failed_sql_dir = Path(failed_sql_dir) if failed_sql_dir is not None else self.log_dir / "failed_sql"
        self.base_sql_dir = Path(base_sql_dir) if base_sql_dir is not None else None
        # 仅保留旧构造调用兼容；内置来源必须由规范化后的实际目录身份确认。
        self.use_builtin_base_tables = use_builtin_base_tables
        self._uses_builtin_base_tables = (
            self.base_sql_dir is not None
            and self.base_sql_dir.resolve() == BUILTIN_BASE_SQL_DIR.resolve()
        )
        self.db_factory = db_factory
        self.run_background = run_background
        self.query_interval_seconds = query_interval_seconds
        self.worker_stall_seconds = worker_stall_seconds
        self._initialization_wait = initialization_wait or self._wait_for_initialization
        self._tasks: Dict[str, TaskSnapshot] = {}
        self._real_tasks: Dict[str, FuzzTask] = {}
        self._task_tunnels: Dict[str, Dict[str, JumpTunnel]] = {}
        self._background_stop_events: Dict[str, threading.Event] = {}
        self._background_wake_events: Dict[str, Dict[int, threading.Event]] = {}
        self._background_worker_threads: Dict[str, Dict[int, threading.Thread]] = {}
        self._initialization_threads: Dict[str, threading.Thread] = {}
        self._initialization_stop_events: Dict[str, threading.Event] = {}
        self._initialization_wake_events: Dict[str, threading.Event] = {}
        self._creating_task_ids: set[str] = set()
        self._jump_hosts: List[dict] = []
        self._counter = itertools.count(1)
        self._lifecycle_lock = threading.RLock()

    def create_task(self, request: TaskCreateRequest) -> TaskSnapshot:
        if request.enable_crud and not self._uses_builtin_base_tables:
            raise ValueError("自定义基表目录不支持逐表 CRUD")
        expand_base_table_columns = request.expand_base_table_columns
        base_table_seed = request.base_table_seed
        base_table_generator_version = request.base_table_generator_version
        if expand_base_table_columns:
            base_table_generator_version = base_table_generator_version or CURRENT_BASE_TABLE_GENERATOR_VERSION
            base_table_seed = base_table_seed or str(secrets.randbits(64))
        query_seed = request.query_seed or str(secrets.randbits(64))
        query_generator_version = request.query_generator_version or CURRENT_QUERY_GENERATOR_VERSION
        crud_seed = request.crud_seed or (str(secrets.randbits(64)) if request.enable_crud else None)
        crud_generator_version = (
            request.crud_generator_version or CURRENT_CRUD_GENERATOR_VERSION
            if request.enable_crud
            else None
        )
        replica_host = request.replica_host
        replica_port = request.replica_port if request.replica_host is not None else None
        effective_replica_host = replica_host or request.host
        effective_replica_port = replica_port or request.port

        task_id = f"task-{next(self._counter)}"
        snapshot = TaskSnapshot(
            task_id=task_id,
            node_name=request.node_name,
            target=f"{request.host}:{request.port}",
            primary_target=f"{request.host}:{request.port}",
            replica_target=f"{effective_replica_host}:{effective_replica_port}",
            replica_host=replica_host,
            replica_port=effective_replica_port if replica_host is not None else None,
            status=TaskStatus.NEW.value,
            phase=TaskStatus.NEW.value,
            database=request.database or "test",
            jump_host=request.jump_host,
            thread_count=request.thread_count,
            enable_crud=request.enable_crud,
            query_seed=query_seed,
            query_generator_version=query_generator_version,
            crud_seed=crud_seed,
            crud_generator_version=crud_generator_version,
            query_worker_total=request.thread_count,
            crud_worker_total=74 if request.enable_crud else 0,
            worker_total=request.thread_count + (74 if request.enable_crud else 0),
            expand_base_table_columns=expand_base_table_columns,
            base_table_seed=base_table_seed,
            base_table_generator_version=base_table_generator_version,
        )
        with self._lifecycle_lock:
            self._tasks[task_id] = snapshot
            self._creating_task_ids.add(task_id)
        try:
            try:
                base_sql_bundle = self._prepare_base_sql_bundle(
                    expand_base_table_columns=expand_base_table_columns,
                    generator_version=base_table_generator_version,
                    seed=base_table_seed,
                )
            except Exception as exc:
                self._mark_snapshot_failed(snapshot, exc, TaskStatus.SEEDING)
                return snapshot

            if self._snapshot_is_terminal_for_task(task_id):
                return snapshot

            if self.db_factory is None:
                self._mark_snapshot_failed(
                    snapshot,
                    RuntimeError("未配置数据库客户端工厂"),
                    TaskStatus.CONNECTING,
                )
                return snapshot

            primary_node = TargetNodeConfig(
                name=request.node_name,
                host=request.host,
                port=request.port,
                username=request.username,
                password=request.password,
                database=request.database or "test",
                jump_host=request.jump_host,
            )
            replica_node = replace(
                primary_node,
                name=f"{request.node_name}-replica",
                host=effective_replica_host,
                port=effective_replica_port,
            )
            if request.jump_host is not None:
                try:
                    self._find_jump_host(request.jump_host)
                except Exception as exc:
                    self._mark_snapshot_failed(snapshot, exc, TaskStatus.CONNECTING)
                    return snapshot
            if self.run_background:
                self._start_initializer(
                    task_id=task_id,
                    snapshot=snapshot,
                    request=request,
                    primary_node=primary_node,
                    replica_node=replica_node,
                    base_sql_bundle=base_sql_bundle,
                    query_seed=query_seed,
                    query_generator_version=query_generator_version,
                    crud_seed=crud_seed,
                    crud_generator_version=crud_generator_version,
                )
                return snapshot
            primary_db_node = primary_node
            replica_db_node = replica_node
            try:
                primary_tunnel = self._start_jump_tunnel(task_id, "primary", primary_node)
                if self._snapshot_is_terminal_for_task(task_id):
                    self._stop_jump_tunnel(task_id)
                    return snapshot
                replica_tunnel = None
                if request.replica_host is not None:
                    replica_tunnel = self._start_jump_tunnel(task_id, "replica", replica_node)
            except Exception as exc:
                self._stop_jump_tunnel(task_id)
                self._mark_snapshot_failed(snapshot, exc, TaskStatus.CONNECTING)
                return snapshot
            if self._snapshot_is_terminal_for_task(task_id):
                self._stop_jump_tunnel(task_id)
                return snapshot
            if primary_tunnel is not None:
                assert primary_tunnel.local_port is not None
                primary_db_node = replace(
                    primary_node,
                    host=primary_tunnel.local_host,
                    port=primary_tunnel.local_port,
                )
            if replica_tunnel is not None:
                assert replica_tunnel.local_port is not None
                replica_db_node = replace(
                    replica_node,
                    host=replica_tunnel.local_host,
                    port=replica_tunnel.local_port,
                )
            elif request.replica_host is None:
                replica_db_node = primary_db_node
            try:
                primary_db_factory = lambda: self.db_factory(primary_db_node)
                replica_db_factory = lambda: self.db_factory(replica_db_node)
                use_role_runtime = request.enable_crud or request.replica_host is not None
                real_task = FuzzTask(
                    task_id=task_id,
                    node=primary_node,
                    base_sql_dir=self.base_sql_dir,
                    base_sql_bundle=base_sql_bundle,
                    db=primary_db_factory(),
                    db_factory=replica_db_factory if use_role_runtime else primary_db_factory,
                    primary_db_factory=primary_db_factory if request.enable_crud else None,
                    replica_db_factory=replica_db_factory if use_role_runtime else None,
                    metric_store=self.metric_store,
                    log_dir=self.log_dir,
                    failed_sql_dir=self.failed_sql_dir,
                    clock=lambda: datetime.now(timezone.utc),
                    thread_count=request.thread_count,
                    expand_base_table_columns=expand_base_table_columns,
                    base_table_seed=base_table_seed,
                    base_table_generator_version=base_table_generator_version,
                    enable_crud=request.enable_crud,
                    primary_target=primary_node.address,
                    replica_target=replica_node.address,
                    query_seed=query_seed,
                    query_generator_version=query_generator_version,
                    crud_seed=crud_seed,
                    crud_generator_version=crud_generator_version,
                )
            except Exception as exc:
                self._stop_jump_tunnel(task_id)
                self._mark_snapshot_failed(snapshot, exc, TaskStatus.CONNECTING)
                return snapshot
            with self._lifecycle_lock:
                if self._snapshot_is_terminal(snapshot):
                    should_start = False
                else:
                    self._real_tasks[task_id] = real_task
                    should_start = True
            if not should_start:
                real_task.stop()
                self._stop_jump_tunnel(task_id)
                return snapshot
            try:
                real_task.start()
            except Exception:
                pass
            self._sync_snapshot_from_task(real_task)
            if real_task.is_terminal:
                self._finalize_terminal_task(task_id)
            elif self.run_background:
                self._start_background_loop(real_task)
            return snapshot
        finally:
            self._finish_task_creation(task_id)

    def _snapshot_is_terminal_for_task(self, task_id: str) -> bool:
        with self._lifecycle_lock:
            return self._snapshot_is_terminal(self._tasks[task_id])

    def _finish_task_creation(self, task_id: str) -> None:
        with self._lifecycle_lock:
            self._creating_task_ids.discard(task_id)
        self._try_finalize_terminal_task(task_id)

    @staticmethod
    def _wait_for_initialization(wake_event: threading.Event, delay: float | None) -> bool:
        interrupted = wake_event.wait(delay)
        wake_event.clear()
        return interrupted

    def _start_initializer(
        self,
        *,
        task_id: str,
        snapshot: TaskSnapshot,
        request: TaskCreateRequest,
        primary_node: TargetNodeConfig,
        replica_node: TargetNodeConfig,
        base_sql_bundle: BaseSqlBundle,
        query_seed: str,
        query_generator_version: str,
        crud_seed: str | None,
        crud_generator_version: str | None,
    ) -> None:
        stop_event = threading.Event()
        wake_event = threading.Event()

        def run() -> None:
            retry_attempt = 0
            try:
                while not stop_event.is_set():
                    with self._lifecycle_lock:
                        if self._snapshot_is_terminal(snapshot):
                            return
                        paused = snapshot.status == TaskStatus.PAUSED.value
                    if paused:
                        self._initialization_wait(wake_event, None)
                        continue
                    outcome = self._initialize_task_once(
                        task_id=task_id,
                        snapshot=snapshot,
                        request=request,
                        primary_node=primary_node,
                        replica_node=replica_node,
                        base_sql_bundle=base_sql_bundle,
                        query_seed=query_seed,
                        query_generator_version=query_generator_version,
                        crud_seed=crud_seed,
                        crud_generator_version=crud_generator_version,
                    )
                    if outcome is InitializationResult.SUCCESS:
                        return
                    if outcome is InitializationResult.STOPPED:
                        return
                    if outcome is InitializationResult.PAUSED:
                        continue
                    delay = retry_backoff_seconds(retry_attempt)
                    retry_attempt += 1
                    self._initialization_wait(wake_event, delay)
            finally:
                with self._lifecycle_lock:
                    self._initialization_threads.pop(task_id, None)
                    self._initialization_stop_events.pop(task_id, None)
                    self._initialization_wake_events.pop(task_id, None)
                self._try_finalize_terminal_task(task_id)

        thread = threading.Thread(
            target=run,
            name=f"sql_fuzz-{task_id}-initializer",
            daemon=True,
        )
        start_error: Exception | None = None
        with self._lifecycle_lock:
            if self._snapshot_is_terminal(snapshot):
                return
            self._initialization_stop_events[task_id] = stop_event
            self._initialization_wake_events[task_id] = wake_event
            self._initialization_threads[task_id] = thread
            try:
                thread.start()
            except Exception as exc:
                stop_event.set()
                wake_event.set()
                if not thread.is_alive():
                    self._initialization_threads.pop(task_id, None)
                    self._initialization_stop_events.pop(task_id, None)
                    self._initialization_wake_events.pop(task_id, None)
                start_error = exc
        if start_error is not None:
            self._mark_snapshot_failed(snapshot, start_error, TaskStatus.CONNECTING)
            self._try_finalize_terminal_task(task_id)

    def _initialize_task_once(
        self,
        *,
        task_id: str,
        snapshot: TaskSnapshot,
        request: TaskCreateRequest,
        primary_node: TargetNodeConfig,
        replica_node: TargetNodeConfig,
        base_sql_bundle: BaseSqlBundle,
        query_seed: str,
        query_generator_version: str,
        crud_seed: str | None,
        crud_generator_version: str | None,
    ) -> InitializationResult:
        with self._lifecycle_lock:
            if self._snapshot_is_terminal(snapshot):
                return InitializationResult.STOPPED
            if snapshot.status == TaskStatus.PAUSED.value:
                return InitializationResult.PAUSED
            snapshot.status = TaskStatus.CONNECTING.value
            snapshot.phase = TaskStatus.CONNECTING.value
            snapshot.last_error = None
        self._stop_jump_tunnel(task_id)
        real_task: FuzzTask | None = None
        try:
            primary_tunnel = self._start_jump_tunnel(task_id, "primary", primary_node)
            if self._snapshot_is_terminal_for_task(task_id):
                return InitializationResult.STOPPED
            replica_tunnel = None
            if request.replica_host is not None:
                replica_tunnel = self._start_jump_tunnel(task_id, "replica", replica_node)
            if self._snapshot_is_terminal_for_task(task_id):
                return InitializationResult.STOPPED
            primary_db_node = primary_node
            replica_db_node = replica_node
            if primary_tunnel is not None:
                assert primary_tunnel.local_port is not None
                primary_db_node = replace(
                    primary_node,
                    host=primary_tunnel.local_host,
                    port=primary_tunnel.local_port,
                )
            if replica_tunnel is not None:
                assert replica_tunnel.local_port is not None
                replica_db_node = replace(
                    replica_node,
                    host=replica_tunnel.local_host,
                    port=replica_tunnel.local_port,
                )
            elif request.replica_host is None:
                replica_db_node = primary_db_node
            assert self.db_factory is not None
            primary_db_factory = lambda: self.db_factory(primary_db_node)
            replica_db_factory = lambda: self.db_factory(replica_db_node)
            use_role_runtime = request.enable_crud or request.replica_host is not None
            real_task = FuzzTask(
                task_id=task_id,
                node=primary_node,
                base_sql_dir=self.base_sql_dir,
                base_sql_bundle=base_sql_bundle,
                db=primary_db_factory(),
                db_factory=replica_db_factory if use_role_runtime else primary_db_factory,
                primary_db_factory=primary_db_factory if request.enable_crud else None,
                replica_db_factory=replica_db_factory if use_role_runtime else None,
                metric_store=self.metric_store,
                log_dir=self.log_dir,
                failed_sql_dir=self.failed_sql_dir,
                clock=lambda: datetime.now(timezone.utc),
                thread_count=request.thread_count,
                expand_base_table_columns=request.expand_base_table_columns,
                base_table_seed=snapshot.base_table_seed,
                base_table_generator_version=snapshot.base_table_generator_version,
                enable_crud=request.enable_crud,
                primary_target=primary_node.address,
                replica_target=replica_node.address,
                query_seed=query_seed,
                query_generator_version=query_generator_version,
                crud_seed=crud_seed,
                crud_generator_version=crud_generator_version,
            )
            with self._lifecycle_lock:
                if self._snapshot_is_terminal(snapshot):
                    should_start = False
                elif snapshot.status == TaskStatus.PAUSED.value:
                    should_start = False
                    paused = True
                else:
                    self._real_tasks[task_id] = real_task
                    should_start = True
                    paused = False
            if not should_start:
                real_task.stop()
                return InitializationResult.PAUSED if paused else InitializationResult.STOPPED
            result = real_task.start(retry_transient=True)
            if result is InitializationResult.SUCCESS:
                self._sync_snapshot_from_task(real_task)
                self._start_background_loop(real_task)
                if real_task.is_terminal:
                    self._finalize_terminal_task(task_id)
                return result
            self._sync_initialization_state_from_task(real_task)
            with self._lifecycle_lock:
                if self._real_tasks.get(task_id) is real_task:
                    self._real_tasks.pop(task_id, None)
            return result
        except Exception as exc:
            if real_task is not None and real_task.is_terminal:
                self._sync_snapshot_from_task(real_task)
                self._finalize_terminal_task(task_id)
                return InitializationResult.STOPPED
            if real_task is not None:
                real_task.stop()
            if is_retryable_initialization_error(exc):
                with self._lifecycle_lock:
                    if self._snapshot_is_terminal(snapshot):
                        outcome = InitializationResult.STOPPED
                    elif snapshot.status == TaskStatus.PAUSED.value:
                        outcome = InitializationResult.PAUSED
                    else:
                        snapshot.status = TaskStatus.CONNECTING.value
                        snapshot.phase = TaskStatus.CONNECTING.value
                        snapshot.last_error = f"连接实例暂时不可用: {exc}"
                        outcome = InitializationResult.RETRY
                return outcome
            self._mark_snapshot_failed(snapshot, exc, TaskStatus.CONNECTING)
            return InitializationResult.STOPPED
        finally:
            with self._lifecycle_lock:
                task_owns_tunnels = (
                    real_task is not None
                    and self._real_tasks.get(task_id) is real_task
                    and not self._snapshot_is_terminal(snapshot)
                )
            if not task_owns_tunnels:
                self._stop_jump_tunnel(task_id)

    def _sync_initialization_state_from_task(self, task: FuzzTask) -> None:
        state = task.snapshot_counts()
        with self._lifecycle_lock:
            snapshot = self._tasks[task.task_id]
            incoming_terminal = state["status"] in {TaskStatus.STOPPED, TaskStatus.FAILED}
            if self._snapshot_is_terminal(snapshot) and not incoming_terminal:
                return
            if snapshot.status == TaskStatus.PAUSED.value and not incoming_terminal:
                return
            snapshot.status = state["status"].value
            snapshot.phase = state["phase"]
            snapshot.last_error = state["last_error"]

    def _prepare_base_sql_bundle(
        self,
        *,
        expand_base_table_columns: bool,
        generator_version: str | None,
        seed: str | None,
    ) -> BaseSqlBundle:
        if expand_base_table_columns:
            if not self._uses_builtin_base_tables:
                raise RuntimeError("自定义基表目录不支持扩展列")
            if generator_version is None or seed is None:
                raise RuntimeError("扩展基表列缺少生成器版本或种子")
            return generate_base_sql_bundle(generator_version, seed)
        if self.base_sql_dir is None:
            raise RuntimeError("未配置基表目录")
        return load_base_sql_bundle(self.base_sql_dir)

    def _mark_snapshot_failed(self, snapshot: TaskSnapshot, exc: Exception, phase: TaskStatus) -> None:
        with self._lifecycle_lock:
            if self._snapshot_is_terminal(snapshot):
                return
            snapshot.status = TaskStatus.FAILED.value
            snapshot.phase = phase.value
            snapshot.last_error = f"{phase.value}失败: {exc}"
            metric_values = (snapshot.task_id, snapshot.node_name, snapshot.status)
        self.metric_store.upsert_task_metric(*metric_values, 0, 0)

    def stop_task(self, task_id: str) -> TaskSnapshot:
        task = self._tasks[task_id]
        with self._lifecycle_lock:
            real_task = self._real_tasks.get(task_id)
            is_creating = task_id in self._creating_task_ids
            initializer_stop_event = self._initialization_stop_events.get(task_id)
            initializer_wake_event = self._initialization_wake_events.get(task_id)
            if initializer_stop_event is not None:
                initializer_stop_event.set()
            if initializer_wake_event is not None:
                initializer_wake_event.set()
            stop_event = self._background_stop_events.get(task_id)
            wake_events = list(self._background_wake_events.get(task_id, {}).values())
            if stop_event is not None:
                stop_event.set()
            for wake_event in wake_events:
                wake_event.set()
            if real_task is None and (is_creating or initializer_stop_event is not None) and not self._snapshot_is_terminal(task):
                task.status = TaskStatus.STOPPED.value
                task.phase = TaskStatus.STOPPED.value
                task.last_error = None
        if real_task is not None:
            real_task.stop()
            self._sync_snapshot_from_task(real_task)
        elif not is_creating and not self._snapshot_is_terminal(task):
            self._mark_snapshot_failed(task, RuntimeError("任务运行实例不存在"), TaskStatus.RUNNING)
        self.metric_store.upsert_task_metric(
            task.task_id,
            task.node_name,
            task.status,
            task.sql_total,
            task.lost_connection_total,
        )
        self._finalize_terminal_task(task_id)
        self._join_background_threads(task_id)
        self._try_finalize_terminal_task(task_id)
        return task

    def pause_task(self, task_id: str) -> TaskSnapshot:
        with self._lifecycle_lock:
            task = self._tasks[task_id]
            if self._snapshot_is_terminal(task):
                return task
            real_task = self._real_tasks.get(task_id)
            if real_task is None:
                wake_event = self._initialization_wake_events.get(task_id)
                if wake_event is not None or task_id in self._creating_task_ids:
                    task.status = TaskStatus.PAUSED.value
                    task.phase = TaskStatus.PAUSED.value
                    task.last_error = None
                    if wake_event is not None:
                        wake_event.set()
                    return task
        if real_task is not None:
            real_task.pause()
            self._wake_background_workers(task_id)
            self._sync_snapshot_from_task(real_task)
            return task
        self._mark_snapshot_failed(task, RuntimeError("任务运行实例不存在"), TaskStatus.RUNNING)
        self._finalize_terminal_task(task_id)
        return task

    def resume_task(self, task_id: str) -> TaskSnapshot:
        with self._lifecycle_lock:
            task = self._tasks[task_id]
            if self._snapshot_is_terminal(task):
                return task
            real_task = self._real_tasks.get(task_id)
            if real_task is None:
                wake_event = self._initialization_wake_events.get(task_id)
                if (
                    (wake_event is not None or task_id in self._creating_task_ids)
                    and task.status == TaskStatus.PAUSED.value
                ):
                    task.status = TaskStatus.CONNECTING.value
                    task.phase = TaskStatus.CONNECTING.value
                    task.last_error = None
                    if wake_event is not None:
                        wake_event.set()
                    return task
        if real_task is not None:
            real_task.resume()
            self._wake_background_workers(task_id)
            self._sync_snapshot_from_task(real_task, allow_paused_transition=True)
            return task
        self._mark_snapshot_failed(task, RuntimeError("任务运行实例不存在"), TaskStatus.RUNNING)
        self._finalize_terminal_task(task_id)
        return task

    def list_tasks(self) -> List[dict]:
        with self._lifecycle_lock:
            real_tasks = list(self._real_tasks.values())
        for task in real_tasks:
            self._sync_task_for_read(task)
        return [task.to_dict() for task in self._tasks.values()]

    def get_task(self, task_id: str) -> dict:
        with self._lifecycle_lock:
            real_task = self._real_tasks.get(task_id)
        if real_task is not None:
            self._sync_task_for_read(real_task)
        return self._tasks[task_id].to_dict()

    def _sync_task_for_read(self, task: FuzzTask) -> None:
        with self._lifecycle_lock:
            initializing = task.task_id in self._initialization_threads
        if initializing and task.status in {
            TaskStatus.NEW,
            TaskStatus.CONNECTING,
            TaskStatus.SEEDING,
            TaskStatus.PAUSED,
        }:
            self._sync_initialization_state_from_task(task)
            return
        self._sync_snapshot_from_task(task)

    def metrics_summary(self) -> dict:
        summary = self.metric_store.summary()
        summary.setdefault("任务数", len(self._tasks))
        return summary

    def coverage(self) -> List[dict]:
        registry = build_operator_registry()
        hit_counts: Dict[str, int] = {}
        recent_hits = set()
        with self._lifecycle_lock:
            real_tasks = list(self._real_tasks.values())
        for task in real_tasks:
            for name, count in task.coverage_counts.items():
                hit_counts[name] = hit_counts.get(name, 0) + count
            recent_hits.update(task.recent_coverage_hits)
        return [
            {
                "name": operator.name,
                "category": operator.category,
                "implemented": operator.implemented,
                "hit_count": hit_counts.get(operator.name, 0),
                "recent": operator.name in recent_hits,
            }
            for operator in registry.operators.values()
        ]

    def add_jump_host(self, jump_host: dict) -> None:
        others = [item for item in self._jump_hosts if item.get("name") != jump_host.get("name")]
        self._jump_hosts = [*others, jump_host]

    def list_jump_hosts(self) -> List[dict]:
        return list(self._jump_hosts)

    def list_lost_connection_events(self, task_id: str) -> List[dict]:
        return self.metric_store.list_lost_connection_events(task_id)

    def list_sql_logs(self, task_id: str) -> List[dict]:
        rows: List[dict] = []
        for path in sorted(self.log_dir.glob(f"*/{task_id}.sql.jsonl")):
            rows.extend(read_jsonl(path))
        return rows

    def _start_background_loop(self, task: FuzzTask) -> None:
        stop_event = threading.Event()
        worker_ids = task.worker_ids
        wake_events = {worker_id: threading.Event() for worker_id in worker_ids}

        def wait_for_worker(worker_id: int, delay: float | None = None) -> None:
            wake_event = wake_events[worker_id]
            wake_event.wait(delay)
            wake_event.clear()

        def run(worker_id: int) -> None:
            while not stop_event.is_set() and not task.is_terminal:
                if task.status is TaskStatus.PAUSED:
                    wait_for_worker(worker_id)
                    self._sync_snapshot_from_task(task)
                    continue
                retry_delay = self._run_task_step(task, worker_id)
                if retry_delay > 0:
                    wait_for_worker(worker_id, retry_delay)
                elif not task._role_runtime:
                    wait_for_worker(worker_id, self.query_interval_seconds)

        def watchdog() -> None:
            interval = min(max(self.worker_stall_seconds / 4, 1), 5)
            while not stop_event.is_set() and not task.is_terminal:
                interrupted = task.interrupt_stalled_workers(self.worker_stall_seconds)
                if interrupted:
                    self._sync_snapshot_from_task(task)
                stop_event.wait(interval)

        def run_registered(thread_key: int, target: Callable[[], None]) -> None:
            try:
                target()
            finally:
                self._background_thread_exited(task.task_id, thread_key)

        candidate_threads: Dict[int, threading.Thread] = {}
        for worker_id in worker_ids:
            thread = threading.Thread(
                target=run_registered,
                args=(worker_id, lambda worker_id=worker_id: run(worker_id)),
                name=f"sql_fuzz-{task.task_id}-{worker_id}",
                daemon=True,
            )
            candidate_threads[worker_id] = thread
        candidate_threads[-1] = threading.Thread(
            target=run_registered,
            args=(-1, watchdog),
            name=f"sql_fuzz-{task.task_id}-watchdog",
            daemon=True,
        )
        with self._lifecycle_lock:
            snapshot = self._tasks.get(task.task_id)
            should_start = snapshot is not None and not self._snapshot_is_terminal(snapshot)
            if should_start:
                self._background_stop_events[task.task_id] = stop_event
                self._background_wake_events[task.task_id] = wake_events
                self._background_worker_threads[task.task_id] = {}
        if not should_start:
            self._try_finalize_terminal_task(task.task_id)
            return
        start_error: Exception | None = None
        for thread_key, thread in candidate_threads.items():
            started, start_error = self._start_registered_background_thread(
                task.task_id,
                stop_event,
                thread_key,
                thread,
            )
            if not started:
                break
        if start_error is not None:
            task.fail(start_error, phase=TaskStatus.RUNNING.value)
            self._sync_snapshot_from_task(task)
            self._finalize_terminal_task(task.task_id)
            self._join_background_threads(task.task_id)
        self._try_finalize_terminal_task(task.task_id)

    def _start_registered_background_thread(
        self,
        task_id: str,
        stop_event: threading.Event,
        thread_key: int,
        thread: threading.Thread,
    ) -> tuple[bool, Exception | None]:
        """原子登记并启动一个后台线程，确保 stop 只会看见已经启动的线程。"""

        with self._lifecycle_lock:
            snapshot = self._tasks.get(task_id)
            tracked_threads = self._background_worker_threads.get(task_id)
            may_start = (
                snapshot is not None
                and not self._snapshot_is_terminal(snapshot)
                and not stop_event.is_set()
                and tracked_threads is not None
            )
            if not may_start:
                return False, None
            tracked_threads[thread_key] = thread
            try:
                thread.start()
            except Exception as exc:
                tracked_threads.pop(thread_key, None)
                stop_event.set()
                return False, exc
            return True, None

    def _run_task_step(self, task: FuzzTask, worker_id: int) -> float:
        try:
            retry_delay = task.step(worker_id)
        except Exception as exc:
            task.fail(exc, phase=task.phase or TaskStatus.RUNNING.value)
            retry_delay = 0
        self._sync_snapshot_from_task(task)
        if task.is_terminal:
            self._finalize_terminal_task(task.task_id)
        return float(retry_delay or 0)

    def _finalize_terminal_task(self, task_id: str) -> None:
        with self._lifecycle_lock:
            stop_event = self._background_stop_events.get(task_id)
            wake_events = list(self._background_wake_events.get(task_id, {}).values())
        if stop_event is not None:
            stop_event.set()
        for wake_event in wake_events:
            wake_event.set()
        self._stop_jump_tunnel(task_id)
        self._try_finalize_terminal_task(task_id)

    def _background_thread_exited(self, task_id: str, thread_key: int) -> None:
        with self._lifecycle_lock:
            threads = self._background_worker_threads.get(task_id)
            if threads is not None:
                threads.pop(thread_key, None)
        self._try_finalize_terminal_task(task_id)

    def _join_background_threads(self, task_id: str) -> None:
        with self._lifecycle_lock:
            threads = list(self._background_worker_threads.get(task_id, {}).values())
            initializer = self._initialization_threads.get(task_id)
            if initializer is not None:
                threads.append(initializer)
        current_thread = threading.current_thread()
        deadline = time.monotonic() + BACKGROUND_JOIN_TIMEOUT_SECONDS
        for thread in threads:
            if thread is current_thread:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            thread.join(timeout=remaining)

    def _try_finalize_terminal_task(self, task_id: str) -> bool:
        with self._lifecycle_lock:
            snapshot = self._tasks.get(task_id)
            if snapshot is None or not self._snapshot_is_terminal(snapshot):
                return False
            if task_id in self._creating_task_ids:
                return False
            if task_id in self._initialization_threads:
                return False
            if self._background_worker_threads.get(task_id):
                return False
            self._background_worker_threads.pop(task_id, None)
            self._background_stop_events.pop(task_id, None)
            self._background_wake_events.pop(task_id, None)
            self._real_tasks.pop(task_id, None)
            return True

    def _wake_background_workers(self, task_id: str) -> None:
        with self._lifecycle_lock:
            wake_events = list(self._background_wake_events.get(task_id, {}).values())
            initialization_wake_event = self._initialization_wake_events.get(task_id)
        for wake_event in wake_events:
            wake_event.set()
        if initialization_wake_event is not None:
            initialization_wake_event.set()

    @staticmethod
    def _snapshot_is_terminal(snapshot: TaskSnapshot) -> bool:
        return snapshot.status in {TaskStatus.STOPPED.value, TaskStatus.FAILED.value}

    def _sync_snapshot_from_task(
        self,
        task: FuzzTask,
        *,
        allow_paused_transition: bool = False,
    ) -> None:
        state = task.snapshot_counts()
        with self._lifecycle_lock:
            snapshot = self._tasks[task.task_id]
            incoming_terminal = state["status"] in {TaskStatus.STOPPED, TaskStatus.FAILED}
            if self._snapshot_is_terminal(snapshot) and not incoming_terminal:
                return
            if (
                snapshot.status == TaskStatus.PAUSED.value
                and state["status"] is not TaskStatus.PAUSED
                and not incoming_terminal
                and not allow_paused_transition
            ):
                return
            snapshot.status = state["status"].value
            snapshot.phase = state["phase"]
            snapshot.last_error = state["last_error"]
            snapshot.sql_total = state["sql_total"]
            snapshot.success_query_total = state["success_query_total"]
            snapshot.failed_query_total = state["failed_query_total"]
            snapshot.ordinary_error_total = state["ordinary_error_total"]
            snapshot.lost_connection_total = state["lost_connection_total"]
            snapshot.worker_states = self._worker_states_with_thread_diagnostics(task.task_id, state["worker_states"])
            snapshot.expand_base_table_columns = state["expand_base_table_columns"]
            snapshot.base_table_seed = state["base_table_seed"]
            snapshot.base_table_generator_version = state["base_table_generator_version"]
            for field_name in (
                "enable_crud",
                "query_seed",
                "query_generator_version",
                "crud_seed",
                "crud_generator_version",
                "query_worker_total",
                "crud_worker_total",
                "worker_total",
                "insert_success_total",
                "insert_failed_total",
                "update_success_total",
                "update_failed_total",
                "delete_success_total",
                "delete_failed_total",
                "crud_success_total",
                "crud_failed_total",
                "primary_reconnect_total",
                "replica_reconnect_total",
                "primary_reconnecting",
                "replica_reconnecting",
            ):
                setattr(snapshot, field_name, state[field_name])

    def _worker_states_with_thread_diagnostics(self, task_id: str, worker_states: list[dict]) -> list[dict]:
        threads = self._background_worker_threads.get(task_id, {})
        rows: list[dict] = []
        for state in worker_states:
            row = dict(state)
            thread = threads.get(row.get("worker_id"))
            row["thread_alive"] = thread.is_alive() if thread is not None else None
            row["thread_name"] = getattr(thread, "name", None) if thread is not None else None
            rows.append(row)
        return rows

    def _start_jump_tunnel(self, task_id: str, role: str, node: TargetNodeConfig) -> JumpTunnel | None:
        if not node.jump_host:
            return None
        jump_host = self._find_jump_host(node.jump_host)
        tunnel = JumpTunnel(jump_host=jump_host, target_node=node)
        try:
            tunnel.start()
        except Exception:
            try:
                tunnel.stop()
            except Exception:
                pass
            raise
        with self._lifecycle_lock:
            self._task_tunnels.setdefault(task_id, {})[role] = tunnel
        return tunnel

    def _stop_jump_tunnel(self, task_id: str) -> None:
        with self._lifecycle_lock:
            tunnels = self._task_tunnels.pop(task_id, {})
        if isinstance(tunnels, JumpTunnel) or not hasattr(tunnels, "values"):
            tunnels = {"primary": tunnels}
        for tunnel in tunnels.values():
            try:
                tunnel.stop()
            except Exception:
                continue

    def _find_jump_host(self, name: str) -> JumpHostConfig:
        for item in self._jump_hosts:
            if item.get("name") == name:
                return JumpHostConfig(**item)
        raise RuntimeError(f"跳板机配置不存在: {name}")
