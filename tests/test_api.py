from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from select_fuzz.api.app import create_app, create_default_app
from select_fuzz.api.service import RuntimeService
from select_fuzz.monitor.events import LostConnectionEvent
from select_fuzz.monitor.logs import SqlLogRecord, append_jsonl
from select_fuzz.monitor.store import MetricStore
from select_fuzz.runner.db import DatabaseClient


class ApiFakeDatabase(DatabaseClient):
    def __init__(self) -> None:
        self.executed: list[str] = []

    def connect(self) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


def _client(tmp_path: Path) -> TestClient:
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
    )
    service.add_jump_host(
        {
            "name": "jump-prod",
            "host": "10.2.0.8",
            "port": 22,
            "username": "ops",
        }
    )
    return TestClient(create_app(service))


def test_health_返回中文状态(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["状态"] == "正常"


def test_创建停止任务并查询任务列表(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/tasks",
        json={
            "node_name": "node-a",
            "host": "172.18.4.12",
            "port": 3306,
            "username": "fuzz",
            "password": "secret",
            "jump_host": "jump-prod",
        },
    )
    task_id = response.json()["task_id"]
    stop_response = client.post(f"/api/tasks/{task_id}/stop")
    list_response = client.get("/api/tasks")

    assert response.status_code == 200
    assert stop_response.json()["状态"] == "已停止"
    assert list_response.json()[0]["node_name"] == "node-a"


def test_指标_覆盖矩阵_跳板机接口(tmp_path: Path) -> None:
    client = _client(tmp_path)

    metrics = client.get("/api/metrics/summary").json()
    coverage = client.get("/api/coverage").json()
    jump_hosts = client.get("/api/jump-hosts").json()

    assert "任务数" in metrics
    assert any(item["name"] == "WITH" for item in coverage)
    assert "hit_count" in coverage[0]
    assert jump_hosts[0]["name"] == "jump-prod"


def test_查询_lost_connection_事件和_sql_日志(tmp_path: Path) -> None:
    store = MetricStore(tmp_path / "metrics.db")
    event = LostConnectionEvent(
        timestamp=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
        task_id="task-1",
        node_name="node-a",
        jump_host="jump-prod",
        target="172.18.4.12:3306",
        sql="SELECT 1",
        window_start=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
    )
    store.insert_lost_connection_event(event)
    log_path = tmp_path / "logs" / "2026-06-04" / "task-1.sql.jsonl"
    append_jsonl(
        log_path,
        SqlLogRecord(
            timestamp=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
            task_id="task-1",
            node_name="node-a",
            status="成功",
            sql="SELECT 1",
        ).to_dict(),
    )
    client = TestClient(create_app(RuntimeService(metric_store=store, log_dir=tmp_path / "logs")))

    events = client.get("/api/tasks/task-1/lost-connections").json()
    logs = client.get("/api/tasks/task-1/sql-logs").json()

    assert events[0]["node_name"] == "node-a"
    assert logs[0]["sql"] == "SELECT 1"


def test_事件流端点返回_sse_格式(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/events/stream")

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert "data:" in response.text


def test_服务层创建真实任务时会执行基表_sql(tmp_path: Path) -> None:
    base_dir = tmp_path / "sql_base_tables"
    base_dir.mkdir()
    (base_dir / "001_base.sql").write_text(
        "CREATE TABLE base_api (id BIGINT NOT NULL, name VARCHAR(64), PRIMARY KEY (id));",
        encoding="utf-8",
    )
    fake_db = ApiFakeDatabase()
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: fake_db,
        run_background=False,
    )
    client = TestClient(create_app(service))

    response = client.post(
        "/api/tasks",
        json={
            "node_name": "node-real",
            "host": "172.18.4.12",
            "port": 3306,
            "username": "fuzz",
            "password": "secret",
        },
    )

    assert response.status_code == 200
    assert response.json()["database"] == "test"
    assert fake_db.executed == [
        "CREATE DATABASE IF NOT EXISTS `test`",
        "USE `test`",
        "SET FOREIGN_KEY_CHECKS=0",
        "DROP TABLE IF EXISTS `base_api`",
        "SET FOREIGN_KEY_CHECKS=1",
        "CREATE TABLE base_api (id BIGINT NOT NULL, name VARCHAR(64), PRIMARY KEY (id))",
    ]


def test_默认_app_使用_no_vector_基表目录() -> None:
    app = create_default_app()
    service = app.state.runtime_service

    assert service.base_sql_dir == Path("sql_base_tables_no_vector_subpartition")


def test_真实任务执行后_覆盖接口返回命中次数(tmp_path: Path) -> None:
    base_dir = tmp_path / "sql_base_tables"
    base_dir.mkdir()
    (base_dir / "001_parent.sql").write_text(
        "CREATE TABLE parent_table (id BIGINT NOT NULL, name VARCHAR(64), PRIMARY KEY (id));",
        encoding="utf-8",
    )
    (base_dir / "002_child.sql").write_text(
        "CREATE TABLE child_table (child_id BIGINT NOT NULL, parent_id BIGINT NOT NULL, PRIMARY KEY (child_id), "
        "CONSTRAINT fk_child_parent FOREIGN KEY (parent_id) REFERENCES parent_table(id));",
        encoding="utf-8",
    )
    fake_db = ApiFakeDatabase()
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: fake_db,
        run_background=False,
    )
    client = TestClient(create_app(service))

    response = client.post(
        "/api/tasks",
        json={
            "node_name": "node-real",
            "host": "172.18.4.12",
            "port": 3306,
            "username": "fuzz",
            "password": "secret",
        },
    )
    task = service._real_tasks[response.json()["task_id"]]
    task.step()

    coverage = client.get("/api/coverage").json()
    hit_rows = [item for item in coverage if item["hit_count"] > 0]
    assert hit_rows
    assert coverage[0]["recent"] in {True, False}


def test_跳板机_post_接口保存配置(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/jump-hosts",
        json={
            "name": "jump-dev",
            "host": "10.9.0.1",
            "port": 22,
            "username": "ops",
        },
    )
    jump_hosts = client.get("/api/jump-hosts").json()

    assert response.status_code == 200
    assert any(item["name"] == "jump-dev" for item in jump_hosts)
