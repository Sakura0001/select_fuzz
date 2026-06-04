from datetime import datetime, timedelta, timezone
from pathlib import Path

from select_fuzz.config import TargetNodeConfig
from select_fuzz.monitor.store import MetricStore
from select_fuzz.runner.db import DatabaseClient, LostConnectionError
from select_fuzz.runner.task import FuzzTask, TaskStatus


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class FakeDatabase(DatabaseClient):
    def __init__(self) -> None:
        self.connected = False
        self.executed: list[str] = []
        self.fail_next_query = False
        self.ping_results: list[bool] = []

    def connect(self) -> None:
        self.connected = True

    def execute(self, sql: str) -> None:
        normalized = sql.strip().upper()
        if self.fail_next_query and (normalized.startswith("SELECT") or normalized.startswith("WITH") or normalized.startswith("(")):
            self.fail_next_query = False
            raise LostConnectionError("Lost connection to MySQL server during query")
        self.executed.append(sql)

    def ping(self) -> bool:
        if self.ping_results:
            return self.ping_results.pop(0)
        return True

    def close(self) -> None:
        self.connected = False


def _base_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "base"
    directory.mkdir()
    (directory / "002_child.sql").write_text(
        "CREATE TABLE child_table (child_id BIGINT NOT NULL, parent_id BIGINT NOT NULL, PRIMARY KEY (child_id));",
        encoding="utf-8",
    )
    (directory / "001_parent.sql").write_text(
        "CREATE TABLE parent_table (id BIGINT NOT NULL, name VARCHAR(64), embedding VECTOR(4), PRIMARY KEY (id));",
        encoding="utf-8",
    )
    return directory


def _base_dir_with_temporary_table(tmp_path: Path) -> Path:
    directory = tmp_path / "base_with_temp"
    directory.mkdir()
    (directory / "t0.sql").write_text(
        "CREATE TABLE t0 (id BIGINT NOT NULL, PRIMARY KEY (id));",
        encoding="utf-8",
    )
    (directory / "t2.sql").write_text(
        """
        DROP TEMPORARY TABLE IF EXISTS `t2`;
        CREATE TEMPORARY TABLE `t2` (
          `id` BIGINT NOT NULL,
          PRIMARY KEY (`id`)
        );
        """,
        encoding="utf-8",
    )
    (directory / "zz_seed_fk_data.sql").write_text(
        """
        INSERT INTO `t0` (`id`) VALUES (1);
        INSERT INTO `t2` (`id`) VALUES (2);
        """,
        encoding="utf-8",
    )
    return directory


def _node() -> TargetNodeConfig:
    return TargetNodeConfig(
        name="node-a",
        host="172.18.4.12",
        port=3306,
        username="fuzz",
        password="secret",
        database="select_fuzz",
        jump_host="jump-prod",
    )


def test_任务启动时按顺序执行基表目录全部_sql(tmp_path: Path) -> None:
    db = FakeDatabase()
    task = FuzzTask(
        task_id="task-1",
        node=_node(),
        base_sql_dir=_base_dir(tmp_path),
        db=db,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )

    task.start()

    assert task.status is TaskStatus.RUNNING
    assert db.executed[0] == "DROP DATABASE IF EXISTS `select_fuzz`"
    assert db.executed[1] == "CREATE DATABASE `select_fuzz`"
    assert db.executed[2] == "USE `select_fuzz`"
    assert db.executed[3].startswith("CREATE TABLE parent_table")
    assert db.executed[4].startswith("CREATE TABLE child_table")


def test_任务启动默认创建并使用_test_库(tmp_path: Path) -> None:
    db = FakeDatabase()
    node = TargetNodeConfig(
        name="node-a",
        host="127.0.0.1",
        port=3306,
        username="root",
        password="Taurus_123",
    )
    task = FuzzTask(
        task_id="task-1",
        node=node,
        base_sql_dir=_base_dir(tmp_path),
        db=db,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )

    task.start()

    assert db.executed[0] == "DROP DATABASE IF EXISTS `test`"
    assert db.executed[1] == "CREATE DATABASE `test`"
    assert db.executed[2] == "USE `test`"


def test_任务启动会先清理已存在基表再重建(tmp_path: Path) -> None:
    db = FakeDatabase()
    task = FuzzTask(
        task_id="task-1",
        node=_node(),
        base_sql_dir=_base_dir(tmp_path),
        db=db,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )

    task.start()

    drop_database_index = db.executed.index("DROP DATABASE IF EXISTS `select_fuzz`")
    create_child_index = next(index for index, sql in enumerate(db.executed) if sql.startswith("CREATE TABLE child_table"))
    assert drop_database_index < create_child_index


def test_任务启动会逐条执行基表文件内的多条_sql(tmp_path: Path) -> None:
    directory = tmp_path / "base"
    directory.mkdir()
    (directory / "t2.sql").write_text(
        """
        SET FOREIGN_KEY_CHECKS=0;
        DROP TEMPORARY TABLE IF EXISTS `temp_table`;
        CREATE TEMPORARY TABLE `temp_table` (
          `id` BIGINT NOT NULL,
          PRIMARY KEY (`id`)
        );
        SET FOREIGN_KEY_CHECKS=1;
        """,
        encoding="utf-8",
    )
    db = FakeDatabase()
    task = FuzzTask(
        task_id="task-1",
        node=_node(),
        base_sql_dir=directory,
        db=db,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )

    task.start()

    assert "DROP TEMPORARY TABLE IF EXISTS `temp_table`" in db.executed
    assert any(sql.startswith("CREATE TEMPORARY TABLE `temp_table`") for sql in db.executed)
    assert not any(";\n" in sql for sql in db.executed)


def test_执行查询会写入_sql_日志(tmp_path: Path) -> None:
    db = FakeDatabase()
    task = FuzzTask(
        task_id="task-1",
        node=_node(),
        base_sql_dir=_base_dir(tmp_path),
        db=db,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
        random_seed=7,
    )

    task.start()
    task.step()

    assert task.sql_total == 1
    assert any(sql.startswith(("SELECT", "WITH", "(")) for sql in db.executed)
    assert "SELECT" in (tmp_path / "logs" / "2026-06-04" / "task-1.sql.jsonl").read_text(encoding="utf-8")


def test_lost_connection_后每分钟检测恢复且不重建永久表(tmp_path: Path) -> None:
    clock = FakeClock()
    db = FakeDatabase()
    db.fail_next_query = True
    db.ping_results = [False, True]
    task = FuzzTask(
        task_id="task-1",
        node=_node(),
        base_sql_dir=_base_dir(tmp_path),
        db=db,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=clock,
    )

    task.start()
    ddl_count = len(db.executed)
    task.step()

    assert task.status is TaskStatus.RECOVERING
    assert task.lost_connection_total == 1
    task.probe_recovery()
    assert task.status is TaskStatus.RECOVERING
    clock.advance(60)
    task.probe_recovery()
    assert task.status is TaskStatus.RECOVERING
    clock.advance(60)
    task.probe_recovery()
    assert task.status is TaskStatus.RUNNING
    assert len([sql for sql in db.executed if sql.startswith("CREATE TABLE")]) == 2


def test_lost_connection_恢复后重建临时表并只插入临时表数据(tmp_path: Path) -> None:
    clock = FakeClock()
    db = FakeDatabase()
    db.fail_next_query = True
    db.ping_results = [True]
    task = FuzzTask(
        task_id="task-1",
        node=_node(),
        base_sql_dir=_base_dir_with_temporary_table(tmp_path),
        db=db,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=clock,
    )

    task.start()
    task.step()
    clock.advance(60)
    task.probe_recovery()

    assert task.status is TaskStatus.RUNNING
    assert db.executed.count("USE `select_fuzz`") == 2
    assert len([sql for sql in db.executed if sql.startswith("CREATE TABLE t0")]) == 1
    assert len([sql for sql in db.executed if sql.startswith("CREATE TEMPORARY TABLE `t2`")]) == 2
    assert db.executed.count("INSERT INTO `t0` (`id`) VALUES (1)") == 1
    assert db.executed.count("INSERT INTO `t2` (`id`) VALUES (2)") == 2
