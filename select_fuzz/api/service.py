from __future__ import annotations

import itertools
import secrets
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from select_fuzz.base_tables import (
    CURRENT_BASE_TABLE_GENERATOR_VERSION,
    BaseSqlBundle,
    generate_base_sql_bundle,
    generate_core_base_sql_bundle,
    load_base_sql_bundle,
)
from select_fuzz.config import JumpHostConfig, TargetNodeConfig
from select_fuzz.monitor.logs import read_jsonl
from select_fuzz.monitor.store import MetricStore
from select_fuzz.runner.db import DatabaseClient
from select_fuzz.runner.jump import JumpTunnel
from select_fuzz.runner.task import FuzzTask, TaskStatus
from select_fuzz.sqlgen.operators import build_operator_registry

from .schemas import TaskCreateRequest


@dataclass
class TaskSnapshot:
    task_id: str
    node_name: str
    target: str
    status: str
    phase: str = TaskStatus.NEW.value
    last_error: Optional[str] = None
    database: str = "test"
    jump_host: Optional[str] = None
    thread_count: int = 1
    sql_total: int = 0
    success_query_total: int = 0
    failed_query_total: int = 0
    ordinary_error_total: int = 0
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
    ) -> None:
        self.metric_store = metric_store
        self.log_dir = Path(log_dir)
        self.failed_sql_dir = Path(failed_sql_dir) if failed_sql_dir is not None else self.log_dir / "failed_sql"
        self.base_sql_dir = Path(base_sql_dir) if base_sql_dir is not None else None
        self.use_builtin_base_tables = use_builtin_base_tables
        self.db_factory = db_factory
        self.run_background = run_background
        self.query_interval_seconds = query_interval_seconds
        self.worker_stall_seconds = worker_stall_seconds
        self._tasks: Dict[str, TaskSnapshot] = {}
        self._real_tasks: Dict[str, FuzzTask] = {}
        self._task_tunnels: Dict[str, JumpTunnel] = {}
        self._background_stop_events: Dict[str, threading.Event] = {}
        self._background_worker_threads: Dict[str, Dict[int, threading.Thread]] = {}
        self._jump_hosts: List[dict] = []
        self._counter = itertools.count(1)

    def create_task(self, request: TaskCreateRequest) -> TaskSnapshot:
        expand_base_table_columns = request.expand_base_table_columns
        base_table_seed = request.base_table_seed
        base_table_generator_version = request.base_table_generator_version
        if expand_base_table_columns:
            base_table_generator_version = base_table_generator_version or CURRENT_BASE_TABLE_GENERATOR_VERSION
            base_table_seed = base_table_seed or str(secrets.randbits(64))

        task_id = f"task-{next(self._counter)}"
        snapshot = TaskSnapshot(
            task_id=task_id,
            node_name=request.node_name,
            target=f"{request.host}:{request.port}",
            status=TaskStatus.NEW.value,
            phase=TaskStatus.NEW.value,
            database=request.database or "test",
            jump_host=request.jump_host,
            thread_count=request.thread_count,
            expand_base_table_columns=expand_base_table_columns,
            base_table_seed=base_table_seed,
            base_table_generator_version=base_table_generator_version,
        )
        self._tasks[task_id] = snapshot
        runtime_enabled = self.db_factory is not None and (
            self.base_sql_dir is not None or self.use_builtin_base_tables
        )
        if runtime_enabled:
            try:
                base_sql_bundle = self._prepare_base_sql_bundle(
                    expand_base_table_columns=expand_base_table_columns,
                    generator_version=base_table_generator_version,
                    seed=base_table_seed,
                )
            except Exception as exc:
                self._mark_snapshot_failed(snapshot, exc, TaskStatus.SEEDING)
                return snapshot

            node = TargetNodeConfig(
                name=request.node_name,
                host=request.host,
                port=request.port,
                username=request.username,
                password=request.password,
                database=request.database or "test",
                jump_host=request.jump_host,
            )
            db_node = node
            try:
                tunnel = self._start_jump_tunnel(task_id, node)
            except Exception as exc:
                snapshot.status = TaskStatus.FAILED.value
                snapshot.phase = TaskStatus.CONNECTING.value
                snapshot.last_error = f"{TaskStatus.CONNECTING.value}失败: {exc}"
                self.metric_store.upsert_task_metric(task_id, request.node_name, snapshot.status, 0, 0)
                return snapshot
            if tunnel is not None:
                assert tunnel.local_port is not None
                db_node = replace(node, host=tunnel.local_host, port=tunnel.local_port)
            try:
                real_task = FuzzTask(
                    task_id=task_id,
                    node=node,
                    base_sql_dir=self.base_sql_dir,
                    base_sql_bundle=base_sql_bundle,
                    db=self.db_factory(db_node),
                    db_factory=lambda: self.db_factory(db_node),
                    metric_store=self.metric_store,
                    log_dir=self.log_dir,
                    failed_sql_dir=self.failed_sql_dir,
                    clock=lambda: datetime.now(timezone.utc),
                    thread_count=request.thread_count,
                    expand_base_table_columns=expand_base_table_columns,
                    base_table_seed=base_table_seed,
                    base_table_generator_version=base_table_generator_version,
                )
            except Exception as exc:
                self._stop_jump_tunnel(task_id)
                snapshot.status = TaskStatus.FAILED.value
                snapshot.phase = TaskStatus.CONNECTING.value
                snapshot.last_error = f"{TaskStatus.CONNECTING.value}失败: {exc}"
                self.metric_store.upsert_task_metric(task_id, request.node_name, snapshot.status, 0, 0)
                return snapshot
            self._real_tasks[task_id] = real_task
            try:
                real_task.start()
            except Exception:
                self._stop_jump_tunnel(task_id)
            self._sync_snapshot_from_task(real_task)
            if self.run_background and not real_task.is_terminal:
                self._start_background_loop(real_task)
        else:
            snapshot.status = TaskStatus.RUNNING.value
            snapshot.phase = TaskStatus.RUNNING.value
            self.metric_store.upsert_task_metric(task_id, request.node_name, snapshot.status, 0, 0)
        return snapshot

    def _prepare_base_sql_bundle(
        self,
        *,
        expand_base_table_columns: bool,
        generator_version: str | None,
        seed: str | None,
    ) -> BaseSqlBundle:
        if expand_base_table_columns:
            if not self.use_builtin_base_tables:
                raise RuntimeError("自定义基表目录不支持扩展列")
            if generator_version is None or seed is None:
                raise RuntimeError("扩展基表列缺少生成器版本或种子")
            return generate_base_sql_bundle(generator_version, seed)
        if self.use_builtin_base_tables:
            return generate_core_base_sql_bundle()
        if self.base_sql_dir is None:
            raise RuntimeError("未配置基表目录")
        return load_base_sql_bundle(self.base_sql_dir)

    def _mark_snapshot_failed(self, snapshot: TaskSnapshot, exc: Exception, phase: TaskStatus) -> None:
        snapshot.status = TaskStatus.FAILED.value
        snapshot.phase = phase.value
        snapshot.last_error = f"{phase.value}失败: {exc}"
        self.metric_store.upsert_task_metric(snapshot.task_id, snapshot.node_name, snapshot.status, 0, 0)

    def stop_task(self, task_id: str) -> TaskSnapshot:
        task = self._tasks[task_id]
        real_task = self._real_tasks.get(task_id)
        if real_task is not None:
            real_task.stop()
            self._sync_snapshot_from_task(real_task)
        else:
            task.status = TaskStatus.STOPPED.value
            task.phase = TaskStatus.STOPPED.value
        self.metric_store.upsert_task_metric(
            task.task_id,
            task.node_name,
            task.status,
            task.sql_total,
            task.lost_connection_total,
        )
        stop_event = self._background_stop_events.get(task_id)
        if stop_event is not None:
            stop_event.set()
        self._stop_jump_tunnel(task_id)
        return task

    def pause_task(self, task_id: str) -> TaskSnapshot:
        task = self._tasks[task_id]
        real_task = self._real_tasks.get(task_id)
        if real_task is not None:
            real_task.pause()
            self._sync_snapshot_from_task(real_task)
            return task
        task.status = TaskStatus.PAUSED.value
        task.phase = TaskStatus.PAUSED.value
        self.metric_store.upsert_task_metric(task.task_id, task.node_name, task.status, task.sql_total, task.lost_connection_total)
        return task

    def resume_task(self, task_id: str) -> TaskSnapshot:
        task = self._tasks[task_id]
        real_task = self._real_tasks.get(task_id)
        if real_task is not None:
            real_task.resume()
            self._sync_snapshot_from_task(real_task)
            return task
        task.status = TaskStatus.RUNNING.value
        task.phase = TaskStatus.RUNNING.value
        self.metric_store.upsert_task_metric(task.task_id, task.node_name, task.status, task.sql_total, task.lost_connection_total)
        return task

    def list_tasks(self) -> List[dict]:
        for task in self._real_tasks.values():
            self._sync_snapshot_from_task(task)
        return [task.to_dict() for task in self._tasks.values()]

    def get_task(self, task_id: str) -> dict:
        real_task = self._real_tasks.get(task_id)
        if real_task is not None:
            self._sync_snapshot_from_task(real_task)
        return self._tasks[task_id].to_dict()

    def metrics_summary(self) -> dict:
        summary = self.metric_store.summary()
        summary.setdefault("任务数", len(self._tasks))
        return summary

    def coverage(self) -> List[dict]:
        registry = build_operator_registry()
        hit_counts: Dict[str, int] = {}
        recent_hits = set()
        for task in self._real_tasks.values():
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
        self._background_stop_events[task.task_id] = stop_event
        self._background_worker_threads[task.task_id] = {}

        def run(worker_id: int) -> None:
            while not stop_event.is_set() and not task.is_terminal:
                if task.status is TaskStatus.PAUSED:
                    stop_event.wait(0.1)
                    self._sync_snapshot_from_task(task)
                    continue
                self._run_task_step(task, worker_id)
                stop_event.wait(self.query_interval_seconds)

        def watchdog() -> None:
            interval = min(max(self.worker_stall_seconds / 4, 1), 5)
            while not stop_event.is_set() and not task.is_terminal:
                interrupted = task.interrupt_stalled_workers(self.worker_stall_seconds)
                if interrupted:
                    self._sync_snapshot_from_task(task)
                stop_event.wait(interval)

        for worker_id in range(task.thread_count):
            thread = threading.Thread(target=run, args=(worker_id,), name=f"sql_fuzz-{task.task_id}-{worker_id}", daemon=True)
            self._background_worker_threads[task.task_id][worker_id] = thread
            thread.start()
        watcher = threading.Thread(target=watchdog, name=f"sql_fuzz-{task.task_id}-watchdog", daemon=True)
        watcher.start()

    def _run_task_step(self, task: FuzzTask, worker_id: int) -> None:
        try:
            task.step(worker_id)
        except Exception as exc:
            task.fail(exc, phase=task.phase or TaskStatus.RUNNING.value)
        self._sync_snapshot_from_task(task)
        if task.is_terminal:
            self._finalize_terminal_task(task.task_id)

    def _finalize_terminal_task(self, task_id: str) -> None:
        stop_event = self._background_stop_events.get(task_id)
        if stop_event is not None:
            stop_event.set()
        self._stop_jump_tunnel(task_id)

    def _sync_snapshot_from_task(self, task: FuzzTask) -> None:
        snapshot = self._tasks[task.task_id]
        state = task.snapshot_counts()
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

    def _start_jump_tunnel(self, task_id: str, node: TargetNodeConfig) -> JumpTunnel | None:
        if not node.jump_host:
            return None
        jump_host = self._find_jump_host(node.jump_host)
        tunnel = JumpTunnel(jump_host=jump_host, target_node=node)
        tunnel.start()
        self._task_tunnels[task_id] = tunnel
        return tunnel

    def _stop_jump_tunnel(self, task_id: str) -> None:
        tunnel = self._task_tunnels.pop(task_id, None)
        if tunnel is not None:
            tunnel.stop()

    def _find_jump_host(self, name: str) -> JumpHostConfig:
        for item in self._jump_hosts:
            if item.get("name") == name:
                return JumpHostConfig(**item)
        raise RuntimeError(f"跳板机配置不存在: {name}")
