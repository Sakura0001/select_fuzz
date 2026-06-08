from datetime import datetime, timedelta, timezone
from pathlib import Path

from select_fuzz.config import TargetNodeConfig
from select_fuzz.monitor.logs import read_jsonl
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
        self.scalar_queries: list[str] = []
        self.table_counts: dict[str, int] = {}
        self.fail_next_query = False
        self.fail_next_ordinary_error = False
        self.ping_results: list[bool] = []

    def connect(self) -> None:
        self.connected = True

    def execute(self, sql: str) -> None:
        normalized = sql.strip().upper()
        if self.fail_next_ordinary_error and (normalized.startswith("SELECT") or normalized.startswith("WITH") or normalized.startswith("(")):
            self.fail_next_ordinary_error = False
            raise RuntimeError("普通 SQL 执行失败")
        if self.fail_next_query and (normalized.startswith("SELECT") or normalized.startswith("WITH") or normalized.startswith("(")):
            self.fail_next_query = False
            raise LostConnectionError("Lost connection to MySQL server during query")
        self.executed.append(sql)

    def query_scalar(self, sql: str) -> int:
        self.scalar_queries.append(sql)
        for table_name, count in self.table_counts.items():
            if f"`{table_name}`" in sql:
                return count
        return 1

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


def test_任务启动后检查每张基表已插入数据(tmp_path: Path) -> None:
    db = FakeDatabase()
    task = FuzzTask(
        task_id="task-1",
        node=_node(),
        base_sql_dir=_base_dir_with_temporary_table(tmp_path),
        db=db,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )

    task.start()

    assert "SELECT COUNT(*) FROM `t0`" in db.scalar_queries
    assert "SELECT COUNT(*) FROM `t2`" in db.scalar_queries


def test_任务启动发现基表无数据会失败(tmp_path: Path) -> None:
    db = FakeDatabase()
    db.table_counts["t0"] = 0
    task = FuzzTask(
        task_id="task-1",
        node=_node(),
        base_sql_dir=_base_dir_with_temporary_table(tmp_path),
        db=db,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )

    try:
        task.start()
    except RuntimeError as exc:
        assert "基表初始化未插入数据" in str(exc)
    else:
        raise AssertionError("基表无数据时必须失败")

    assert task.status is TaskStatus.FAILED
    assert task.phase == "准备基表"
    assert task.last_error is not None
    assert "准备基表失败" in task.last_error


def test_任务启动没有可解析基表会失败(tmp_path: Path) -> None:
    directory = tmp_path / "empty_base"
    directory.mkdir()
    (directory / "001_seed.sql").write_text("INSERT INTO missing_table VALUES (1);", encoding="utf-8")
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

    try:
        task.start()
    except RuntimeError as exc:
        assert "至少需要一张可解析的基表" in str(exc)
    else:
        raise AssertionError("没有可解析基表时必须失败")

    assert task.status is TaskStatus.FAILED
    assert task.phase == "准备基表"


def test_step_遇到未预期异常会标记任务失败(tmp_path: Path) -> None:
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
    task.tables.clear()

    task.step()

    assert task.status is TaskStatus.FAILED
    assert task.phase == "执行 SQL"
    assert task.failed_query_total == 0
    assert task.last_error is not None
    assert "至少需要一张表元数据才能生成 SQL" in task.last_error


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
    assert task.success_query_total == 1
    assert task.failed_query_total == 0
    assert any(sql.startswith(("SELECT", "WITH", "(")) for sql in db.executed)
    assert "SELECT" in (tmp_path / "logs" / "2026-06-04" / "task-1.sql.jsonl").read_text(encoding="utf-8")


def test_普通执行失败会把原始_sql_写入失败目录(tmp_path: Path) -> None:
    db = FakeDatabase()
    db.fail_next_ordinary_error = True
    task = FuzzTask(
        task_id="task-1",
        node=_node(),
        base_sql_dir=_base_dir(tmp_path),
        db=db,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        failed_sql_dir=tmp_path / "failed_sql",
        clock=FakeClock(),
        random_seed=7,
    )

    task.start()
    task.step()

    assert task.success_query_total == 0
    assert task.failed_query_total == 1
    assert task.ordinary_error_total == 1
    log_rows = read_jsonl(tmp_path / "logs" / "2026-06-04" / "task-1.sql.jsonl")
    assert log_rows[0]["status"] == "普通错误"
    assert log_rows[0]["error_message"] == "普通 SQL 执行失败"
    failed_files = list((tmp_path / "failed_sql").glob("2026-06-04/*.sql"))
    assert len(failed_files) == 1
    content = failed_files[0].read_text(encoding="utf-8")
    assert content.startswith("SELECT")
    assert "\"status\"" not in content


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
    assert task.failed_query_total == 1
    task.probe_recovery()
    assert task.status is TaskStatus.RECOVERING
    clock.advance(60)
    task.probe_recovery()
    assert task.status is TaskStatus.RECOVERING
    clock.advance(60)
    task.probe_recovery()
    assert task.status is TaskStatus.RUNNING
    assert len([sql for sql in db.executed if sql.startswith("CREATE TABLE")]) == 2


def test_lost_connection_去重窗口内失败查询数仍逐次累计(tmp_path: Path) -> None:
    class TwoLostConnectionDatabase(FakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.remaining_lost_queries = 2

        def execute(self, sql: str) -> None:
            normalized = sql.strip().upper()
            if self.remaining_lost_queries > 0 and (
                normalized.startswith("SELECT") or normalized.startswith("WITH") or normalized.startswith("(")
            ):
                self.remaining_lost_queries -= 1
                raise LostConnectionError("Lost connection to MySQL server during query")
            super().execute(sql)

    clock = FakeClock()
    db = TwoLostConnectionDatabase()
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
    task.step()
    clock.advance(60)
    task.probe_recovery()
    task.step()

    assert task.lost_connection_total == 1
    assert task.failed_query_total == 2


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


def test_多线程任务为每个_worker_准备独立临时表会话(tmp_path: Path) -> None:
    class SessionTemporaryDatabase(FakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.temporary_t2_created = False
            self.temporary_t2_count_checks = 0
            self.temporary_t2_execute_checks = 0

        def execute(self, sql: str) -> None:
            normalized = sql.strip().upper()
            if normalized.startswith("CREATE TEMPORARY TABLE `T2`"):
                self.temporary_t2_created = True
            if (normalized.startswith("SELECT") or normalized.startswith("WITH") or normalized.startswith("(")) and "`T2`" in normalized:
                if not self.temporary_t2_created:
                    raise RuntimeError("当前 worker 会话未创建临时表 t2")
                self.temporary_t2_execute_checks += 1
            super().execute(sql)

        def query_scalar(self, sql: str) -> int:
            if "`t2`" in sql:
                if not self.temporary_t2_created:
                    raise RuntimeError("当前 worker 会话未创建临时表 t2")
                self.temporary_t2_count_checks += 1
            return super().query_scalar(sql)

    class FixedTemporaryTableGenerator:
        coverage_counts: dict[str, int] = {}
        recent_hits: list[str] = []

        def generate(self, *_args, **_kwargs) -> str:
            return "SELECT COUNT(*) FROM `t2`"

    dbs = [SessionTemporaryDatabase(), SessionTemporaryDatabase(), SessionTemporaryDatabase()]
    task = FuzzTask(
        task_id="task-1",
        node=_node(),
        base_sql_dir=_base_dir_with_temporary_table(tmp_path),
        db=dbs[0],
        db_factory=lambda: dbs[len([db for db in dbs if db.connected])],
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
        thread_count=3,
    )

    task.start()
    for worker in task._workers:
        worker.generator = FixedTemporaryTableGenerator()

    assert task.thread_count == 3
    assert dbs[0].executed.count("CREATE TABLE t0 (id BIGINT NOT NULL, PRIMARY KEY (id))") == 1
    for db in dbs:
        assert db.connected is True
        assert "USE `select_fuzz`" in db.executed
        assert any(sql.startswith("CREATE TEMPORARY TABLE `t2`") for sql in db.executed)
        assert db.executed.count("INSERT INTO `t2` (`id`) VALUES (2)") == 1
        assert db.temporary_t2_count_checks == 1
    for worker_id in range(3):
        task.step(worker_id)
    for db in dbs:
        assert db.temporary_t2_execute_checks == 1
    assert task.success_query_total == 3
    assert task.failed_query_total == 0
    assert [state["sql_total"] for state in task.worker_states] == [1, 1, 1]


def test_多线程新增_worker_准备失败会关闭该连接(tmp_path: Path) -> None:
    class FailingPrepareDatabase(FakeDatabase):
        def execute(self, sql: str) -> None:
            super().execute(sql)
            if sql.startswith("CREATE TEMPORARY TABLE"):
                raise RuntimeError("模拟临时表创建失败")

    dbs = [FakeDatabase(), FailingPrepareDatabase()]
    task = FuzzTask(
        task_id="task-1",
        node=_node(),
        base_sql_dir=_base_dir_with_temporary_table(tmp_path),
        db=dbs[0],
        db_factory=lambda: dbs[1],
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
        thread_count=2,
    )

    try:
        task.start()
    except RuntimeError as exc:
        assert "模拟临时表创建失败" in str(exc)
    else:
        raise AssertionError("新增 worker 准备失败时必须失败")

    assert task.status is TaskStatus.FAILED
    assert dbs[1].connected is False


def test_任务暂停后_step_不会继续发送_sql(tmp_path: Path) -> None:
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
    before = len(db.executed)

    task.pause()
    task.step()
    task.resume()
    task.step()

    assert task.status is TaskStatus.RUNNING
    assert len(db.executed) == before + 1


def test_看门狗会关闭长时间执行_sql_的_worker_连接(tmp_path: Path) -> None:
    clock = FakeClock()
    db = FakeDatabase()
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
    task.record_worker_sql_start(0, "SELECT SLEEP(999)", clock())
    clock.advance(31)

    interrupted = task.interrupt_stalled_workers(timeout_seconds=30)
    worker_state = task.worker_states[0]

    assert interrupted == [0]
    assert db.connected is False
    assert worker_state["state"] == "疑似卡住"
    assert worker_state["current_sql"] == "SELECT SLEEP(999)"
    assert worker_state["stalled_total"] == 1


def test_暂停不会清空正在执行_sql_的_worker_状态(tmp_path: Path) -> None:
    clock = FakeClock()
    db = FakeDatabase()
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
    task.record_worker_sql_start(0, "SELECT SLEEP(999)", clock())

    task.pause()
    clock.advance(31)
    interrupted = task.interrupt_stalled_workers(timeout_seconds=30)

    assert task.status is TaskStatus.PAUSED
    assert interrupted == [0]
    assert task.worker_states[0]["current_sql"] == "SELECT SLEEP(999)"
