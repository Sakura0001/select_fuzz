from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from select_fuzz.monitor.store import MetricStore
from select_fuzz.runner.db import PyMySQLClient

from .schemas import JumpHostRequest, TaskCreateRequest
from .service import RuntimeService


def create_app(service: RuntimeService) -> FastAPI:
    app = FastAPI(title="sql_fuzz 运维控制台", version="0.1.0")

    @app.get("/api/health")
    def health() -> dict:
        return {"状态": "正常"}

    @app.get("/api/tasks")
    def list_tasks() -> list:
        return service.list_tasks()

    @app.post("/api/tasks")
    def create_task(request: TaskCreateRequest) -> dict:
        return service.create_task(request).to_dict()

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict:
        try:
            return service.get_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: str) -> dict:
        try:
            return {"状态": service.stop_task(task_id).status}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    @app.get("/api/tasks/{task_id}/lost-connections")
    def lost_connections(task_id: str) -> list:
        return service.list_lost_connection_events(task_id)

    @app.get("/api/tasks/{task_id}/sql-logs")
    def sql_logs(task_id: str) -> list:
        return service.list_sql_logs(task_id)

    @app.get("/api/metrics/summary")
    def metrics_summary() -> dict:
        return service.metrics_summary()

    @app.get("/api/coverage")
    def coverage() -> list:
        return service.coverage()

    @app.get("/api/jump-hosts")
    def jump_hosts() -> list:
        return service.list_jump_hosts()

    @app.post("/api/jump-hosts")
    def add_jump_host(request: JumpHostRequest) -> dict:
        row = request.model_dump()
        service.add_jump_host(row)
        return row

    @app.get("/api/events/stream")
    def event_stream() -> StreamingResponse:
        def stream() -> Iterator[str]:
            payload = {"类型": "心跳", "状态": "正常"}
            yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def create_default_app() -> FastAPI:
    log_dir = Path("logs")
    service = RuntimeService(
        metric_store=MetricStore(log_dir / "metrics.db"),
        log_dir=log_dir,
        base_sql_dir=Path("sql_base_tables"),
        db_factory=lambda node: PyMySQLClient(node),
    )
    return create_app(service)


app = create_default_app()
