from datetime import datetime, timedelta, timezone
from pathlib import Path

from select_fuzz.api.service import RuntimeService
from select_fuzz.config import TargetNodeConfig
from select_fuzz.monitor.store import MetricStore
from select_fuzz.runner.db import DatabaseClient, LostConnectionError
from select_fuzz.runner.task import FuzzTask, TaskStatus


class EndToEndClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class EndToEndDatabase(DatabaseClient):
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.query_attempts = 0
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def execute(self, sql: str) -> None:
        normalized = sql.strip().upper()
        if normalized.startswith("SELECT") or normalized.startswith("WITH") or normalized.startswith("("):
            self.query_attempts += 1
            if self.query_attempts in {2, 3}:
                raise LostConnectionError("Lost connection to MySQL server during query")
        self.executed.append(sql)

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        self.connected = False


def test_端到端任务执行日志去重和_api_查询(tmp_path: Path) -> None:
    base_dir = tmp_path / "sql_base_tables"
    base_dir.mkdir()
    (base_dir / "001_parent.sql").write_text(
        "CREATE TABLE parent_table (id BIGINT NOT NULL, name VARCHAR(64), embedding VECTOR(4), PRIMARY KEY (id));",
        encoding="utf-8",
    )
    (base_dir / "002_child.sql").write_text(
        "CREATE TABLE child_table (child_id BIGINT NOT NULL, parent_id BIGINT NOT NULL, amount DECIMAL(10,2), PRIMARY KEY (child_id));",
        encoding="utf-8",
    )
    store = MetricStore(tmp_path / "metrics.db")
    clock = EndToEndClock()
    task = FuzzTask(
        task_id="task-e2e",
        node=TargetNodeConfig(
            name="node-e2e",
            host="172.18.4.12",
            port=3306,
            username="fuzz",
            password="secret",
            database="select_fuzz",
            jump_host="jump-prod",
        ),
        base_sql_dir=base_dir,
        db=EndToEndDatabase(),
        metric_store=store,
        log_dir=tmp_path / "logs",
        clock=clock,
    )

    task.start()
    task.step()
    task.step()
    clock.advance(60)
    task.probe_recovery()
    task.step()

    service = RuntimeService(metric_store=store, log_dir=tmp_path / "logs")
    events = service.list_lost_connection_events("task-e2e")
    logs = service.list_sql_logs("task-e2e")

    assert task.status is TaskStatus.RECOVERING
    assert len(events) == 1
    assert len([row for row in logs if row["status"] == "lost connection"]) == 2
    assert store.summary()["lost connection"] == 1
    assert any(sql.startswith("CREATE TABLE parent_table") for sql in task.db.executed)
