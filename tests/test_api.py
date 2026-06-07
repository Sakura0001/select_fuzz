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
        self.scalar_queries: list[str] = []

    def connect(self) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def query_scalar(self, sql: str) -> int:
        self.scalar_queries.append(sql)
        return 1

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


class ApiFailingCreateDatabase(ApiFakeDatabase):
    def execute(self, sql: str) -> None:
        super().execute(sql)
        if sql.startswith("CREATE TABLE"):
            raise RuntimeError("模拟创建基表失败")


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
        "DROP DATABASE IF EXISTS `test`",
        "CREATE DATABASE `test`",
        "USE `test`",
        "CREATE TABLE base_api (id BIGINT NOT NULL, name VARCHAR(64), PRIMARY KEY (id))",
    ]
    assert fake_db.scalar_queries == ["SELECT COUNT(*) FROM `base_api`"]


def test_创建真实任务初始化失败会保留失败状态和原因(tmp_path: Path) -> None:
    base_dir = tmp_path / "sql_base_tables"
    base_dir.mkdir()
    (base_dir / "001_base.sql").write_text(
        "CREATE TABLE base_api (id BIGINT NOT NULL, PRIMARY KEY (id));",
        encoding="utf-8",
    )
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: ApiFailingCreateDatabase(),
        run_background=False,
    )
    client = TestClient(create_app(service))

    response = client.post(
        "/api/tasks",
        json={
            "node_name": "node-fail",
            "host": "172.18.4.12",
            "port": 3306,
            "username": "fuzz",
            "password": "secret",
        },
    )
    task_id = response.json()["task_id"]
    listed_task = client.get("/api/tasks").json()[0]

    assert response.status_code == 200
    assert response.json()["status"] == "失败"
    assert response.json()["phase"] == "准备基表"
    assert "准备基表失败" in response.json()["last_error"]
    assert listed_task["task_id"] == task_id
    assert listed_task["status"] == "失败"
    assert listed_task["last_error"] == response.json()["last_error"]


def test_任务支持暂停和恢复(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/api/tasks",
        json={
            "node_name": "node-a",
            "host": "172.18.4.12",
            "port": 3306,
            "username": "fuzz",
            "password": "secret",
        },
    )
    task_id = response.json()["task_id"]

    paused = client.post(f"/api/tasks/{task_id}/pause")
    loaded_paused = client.get(f"/api/tasks/{task_id}")
    resumed = client.post(f"/api/tasks/{task_id}/resume")

    assert paused.status_code == 200
    assert paused.json()["状态"] == "已暂停"
    assert loaded_paused.json()["status"] == "已暂停"
    assert resumed.json()["状态"] == "执行 SQL"


def test_跳板机启动后_db_factory_失败会关闭隧道并返回失败状态(tmp_path: Path, monkeypatch) -> None:
    base_dir = tmp_path / "sql_base_tables"
    base_dir.mkdir()
    (base_dir / "001_base.sql").write_text(
        "CREATE TABLE base_api (id BIGINT NOT NULL, PRIMARY KEY (id));",
        encoding="utf-8",
    )
    events: list[str] = []

    class FakeTunnel:
        local_host = "127.0.0.1"
        local_port = None

        def __init__(self, jump_host, target_node) -> None:
            self.jump_host = jump_host
            self.target_node = target_node

        def start(self) -> tuple[str, int]:
            events.append("start")
            self.local_port = 44001
            return self.local_host, self.local_port

        def stop(self) -> None:
            events.append("stop")

    def failing_factory(_node):
        raise RuntimeError("模拟 db_factory 失败")

    monkeypatch.setattr("select_fuzz.api.service.JumpTunnel", FakeTunnel)
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=failing_factory,
        run_background=False,
    )
    service.add_jump_host(
        {
            "name": "jump-prod",
            "host": "10.2.0.8",
            "port": 22,
            "username": "ops",
            "password": "ssh-secret",
        }
    )
    client = TestClient(create_app(service))

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

    assert response.status_code == 200
    assert response.json()["status"] == "失败"
    assert response.json()["phase"] == "连接实例"
    assert "模拟 db_factory 失败" in response.json()["last_error"]
    assert events == ["start", "stop"]


def test_后台_worker_未预期异常会同步失败快照(tmp_path: Path) -> None:
    base_dir = tmp_path / "sql_base_tables"
    base_dir.mkdir()
    (base_dir / "001_base.sql").write_text(
        "CREATE TABLE base_api (id BIGINT NOT NULL, PRIMARY KEY (id));",
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
    task.tables.clear()

    service._run_task_step(task, 0)
    loaded = client.get(f"/api/tasks/{task.task_id}").json()

    assert loaded["status"] == "失败"
    assert loaded["phase"] == "执行 SQL"
    assert "至少需要一张表元数据才能生成 SQL" in loaded["last_error"]


def test_默认_app_使用完整基表目录() -> None:
    app = create_default_app()
    service = app.state.runtime_service

    assert service.base_sql_dir == Path("sql_base_tables")


def test_创建真实任务支持自定义线程数并为每个_worker_准备临时表(tmp_path: Path) -> None:
    base_dir = tmp_path / "sql_base_tables"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text(
        "CREATE TABLE t0 (id BIGINT NOT NULL, PRIMARY KEY (id));",
        encoding="utf-8",
    )
    (base_dir / "t2.sql").write_text(
        "CREATE TEMPORARY TABLE `t2` (id BIGINT NOT NULL, PRIMARY KEY (id));",
        encoding="utf-8",
    )
    (base_dir / "zz_seed_fk_data.sql").write_text(
        "INSERT INTO `t0` (`id`) VALUES (1); INSERT INTO `t2` (`id`) VALUES (2);",
        encoding="utf-8",
    )
    dbs = [ApiFakeDatabase(), ApiFakeDatabase(), ApiFakeDatabase()]

    def factory(_node):
        return dbs.pop(0)

    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=factory,
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
            "thread_count": 3,
        },
    )

    assert response.status_code == 200
    assert response.json()["thread_count"] == 3
    created_dbs = [task_worker.db for task_worker in service._real_tasks[response.json()["task_id"]]._workers]
    assert len(created_dbs) == 3
    assert created_dbs[0].executed.count("CREATE TABLE t0 (id BIGINT NOT NULL, PRIMARY KEY (id))") == 1
    for db in created_dbs:
        assert any(sql.startswith("CREATE TEMPORARY TABLE `t2`") for sql in db.executed)


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
            "password": "ssh-secret",
        },
    )
    jump_hosts = client.get("/api/jump-hosts").json()

    assert response.status_code == 200
    saved = next(item for item in jump_hosts if item["name"] == "jump-dev")
    assert saved["password"] == "ssh-secret"
