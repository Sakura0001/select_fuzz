from __future__ import annotations

import itertools
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from select_fuzz.config import TargetNodeConfig
from select_fuzz.monitor.logs import read_jsonl
from select_fuzz.monitor.store import MetricStore
from select_fuzz.runner.db import DatabaseClient
from select_fuzz.runner.task import FuzzTask, TaskStatus
from select_fuzz.sqlgen.operators import build_operator_registry

from .schemas import TaskCreateRequest


@dataclass
class TaskSnapshot:
    task_id: str
    node_name: str
    target: str
    status: str
    database: str = "test"
    jump_host: Optional[str] = None
    sql_total: int = 0
    lost_connection_total: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class RuntimeService:
    def __init__(
        self,
        metric_store: MetricStore,
        log_dir: Path | str,
        base_sql_dir: Path | str | None = None,
        db_factory: Optional[Callable[[TargetNodeConfig], DatabaseClient]] = None,
        run_background: bool = True,
        query_interval_seconds: float = 0.05,
    ) -> None:
        self.metric_store = metric_store
        self.log_dir = Path(log_dir)
        self.base_sql_dir = Path(base_sql_dir) if base_sql_dir is not None else None
        self.db_factory = db_factory
        self.run_background = run_background
        self.query_interval_seconds = query_interval_seconds
        self._tasks: Dict[str, TaskSnapshot] = {}
        self._real_tasks: Dict[str, FuzzTask] = {}
        self._jump_hosts: List[dict] = []
        self._counter = itertools.count(1)

    def create_task(self, request: TaskCreateRequest) -> TaskSnapshot:
        task_id = f"task-{next(self._counter)}"
        snapshot = TaskSnapshot(
            task_id=task_id,
            node_name=request.node_name,
            target=f"{request.host}:{request.port}",
            status="执行 SQL",
            database=request.database or "test",
            jump_host=request.jump_host,
        )
        self._tasks[task_id] = snapshot
        if self.base_sql_dir is not None and self.db_factory is not None:
            node = TargetNodeConfig(
                name=request.node_name,
                host=request.host,
                port=request.port,
                username=request.username,
                password=request.password,
                database=request.database or "test",
                jump_host=request.jump_host,
            )
            real_task = FuzzTask(
                task_id=task_id,
                node=node,
                base_sql_dir=self.base_sql_dir,
                db=self.db_factory(node),
                metric_store=self.metric_store,
                log_dir=self.log_dir,
                clock=lambda: datetime.now(timezone.utc),
            )
            real_task.start()
            self._real_tasks[task_id] = real_task
            snapshot.status = real_task.status.value
            if self.run_background:
                self._start_background_loop(real_task)
        else:
            self.metric_store.upsert_task_metric(task_id, request.node_name, snapshot.status, 0, 0)
        return snapshot

    def stop_task(self, task_id: str) -> TaskSnapshot:
        task = self._tasks[task_id]
        real_task = self._real_tasks.get(task_id)
        if real_task is not None:
            real_task.stop()
        task.status = "已停止"
        self.metric_store.upsert_task_metric(
            task.task_id,
            task.node_name,
            task.status,
            task.sql_total,
            task.lost_connection_total,
        )
        return task

    def list_tasks(self) -> List[dict]:
        return [task.to_dict() for task in self._tasks.values()]

    def get_task(self, task_id: str) -> dict:
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
        self._jump_hosts.append(jump_host)

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
        def run() -> None:
            while task.status is not TaskStatus.STOPPED:
                task.step()
                snapshot = self._tasks[task.task_id]
                snapshot.status = task.status.value
                snapshot.sql_total = task.sql_total
                snapshot.lost_connection_total = task.lost_connection_total
                time.sleep(self.query_interval_seconds)

        thread = threading.Thread(target=run, name=f"sql_fuzz-{task.task_id}", daemon=True)
        thread.start()
