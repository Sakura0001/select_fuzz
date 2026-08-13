from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from select_fuzz.base_tables import build_base_sql_bundle
from select_fuzz.config import TargetNodeConfig
from select_fuzz.metadata.models import BaseSqlFile
from select_fuzz.monitor.logs import read_jsonl
from select_fuzz.monitor.store import MetricStore
from select_fuzz.runner import task as task_module
from select_fuzz.runner.db import DatabaseClient, LostConnectionError, PyMySQLClient
from select_fuzz.runner.task import FuzzTask, InitializationResult, TaskStatus
from select_fuzz.sqlgen.seeds import derive_worker_seed
from select_fuzz.api.service import BUILTIN_BASE_SQL_DIR
from select_fuzz.base_tables import load_base_sql_bundle


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _is_query_expression(sql: str) -> bool:
    normalized = sql.strip().upper()
    return normalized.startswith(("SELECT", "WITH", "(", "TABLE", "VALUES"))


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
        if self.fail_next_ordinary_error and _is_query_expression(sql):
            self.fail_next_ordinary_error = False
            raise RuntimeError("普通 SQL 执行失败")
        if self.fail_next_query and _is_query_expression(sql):
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


class RoleDatabase(FakeDatabase):
    def __init__(self, role: str, *, failures: list[Exception] | None = None) -> None:
        super().__init__()
        self.role = role
        self.failures = list(failures or [])
        self.close_count = 0

    def execute(self, sql: str) -> int:
        if self.failures and sql.strip().upper().startswith(("SELECT", "INSERT", "UPDATE", "DELETE")):
            raise self.failures.pop(0)
        self.executed.append(sql)
        if sql.strip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            return 3
        return 0

    def query_scalar(self, sql: str) -> int:
        self.scalar_queries.append(sql)
        return 50

    def close(self) -> None:
        self.close_count += 1
        super().close()


def test_无限重连退避在极大尝试次数仍稳定封顶五秒() -> None:
    assert [task_module.retry_backoff_seconds(attempt) for attempt in range(7)] == [
        0.1,
        0.2,
        0.4,
        0.8,
        1.6,
        3.2,
        5.0,
    ]
    assert task_module.retry_backoff_seconds(10**9) == 5.0


def test_初始化遇到1146返回可重试并从重建数据库边界完整重做(tmp_path: Path) -> None:
    class RetrySeedDatabase(FakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.close_count = 0
            self.count_attempt = 0

        def query_scalar(self, sql: str) -> int:
            self.scalar_queries.append(sql)
            self.count_attempt += 1
            if self.count_attempt == 1:
                raise RuntimeError(1146, "Table doesn't exist")
            return 1

        def close(self) -> None:
            self.close_count += 1
            super().close()

    database = RetrySeedDatabase()
    task = FuzzTask(
        task_id="task-init-retry",
        node=_node(),
        base_sql_bundle=build_base_sql_bundle(
            (
                BaseSqlFile(
                    path=Path("t0.sql"),
                    sql="CREATE TABLE t0 (id BIGINT); INSERT INTO t0 VALUES (1);",
                ),
            )
        ),
        db=database,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )

    first = task.start(retry_transient=True)

    assert first is InitializationResult.RETRY
    assert task.status is TaskStatus.SEEDING
    assert database.connected is False
    assert database.executed.count("DROP DATABASE IF EXISTS `select_fuzz`") == 1
    assert database.executed.count("CREATE TABLE t0 (id BIGINT)") == 1

    second = task.start(retry_transient=True)

    assert second is InitializationResult.SUCCESS
    assert task.status is TaskStatus.RUNNING
    assert database.executed.count("DROP DATABASE IF EXISTS `select_fuzz`") == 2
    assert database.executed.count("CREATE TABLE t0 (id BIGINT)") == 2
    assert database.executed.count("INSERT INTO t0 VALUES (1)") == 2
    assert database.close_count >= 1


def test_execute_返回游标受影响行数(monkeypatch) -> None:
    class Cursor:
        rowcount = 7

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _sql: str) -> None:
            return None

    class Connection:
        open = True

        def cursor(self):
            return Cursor()

    client = PyMySQLClient(_node())
    client._connection = Connection()

    assert client.execute("UPDATE `t0` SET `id` = `id`") == 7


def test_crud_注册74主写worker和独占的备读worker并按角色路由(tmp_path: Path) -> None:
    bundle = load_base_sql_bundle(BUILTIN_BASE_SQL_DIR)
    primary = RoleDatabase("primary")
    primary_created: list[RoleDatabase] = []
    replicas: list[RoleDatabase] = []

    def primary_factory() -> RoleDatabase:
        db = RoleDatabase("primary")
        primary_created.append(db)
        return db

    def replica_factory() -> RoleDatabase:
        db = RoleDatabase("replica")
        replicas.append(db)
        return db

    task = FuzzTask(
        task_id="task-crud-routing",
        node=_node(),
        primary_target="172.18.4.12:3306",
        replica_target="172.18.4.13:3307",
        base_sql_bundle=bundle,
        db=primary,
        primary_db_factory=primary_factory,
        replica_db_factory=replica_factory,
        thread_count=2,
        enable_crud=True,
        query_seed="11",
        crud_seed="22",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )

    task.start()

    states = task.worker_states
    assert task.worker_ids == list(range(76))
    assert [row["worker_key"] for row in states[:2]] == ["query:0", "query:1"]
    assert {row["table_name"] for row in states[2:]} == {
        f"t{index}" for index in range(79) if index not in {2, 3, 4, 5, 6}
    }
    assert all(row["db_role"] == "replica" for row in states[:2])
    assert all(row["db_role"] == "primary" for row in states[2:])
    assert [row["generator_seed"] for row in states[:2]] == [
        str(derive_worker_seed("11", "query", "query:0")),
        str(derive_worker_seed("11", "query", "query:1")),
    ]
    assert states[2]["generator_seed"] == str(
        derive_worker_seed("22", "dml", states[2]["worker_key"])
    )
    assert len({id(worker.db) for worker in task._workers}) == 76
    assert primary_created and len(primary_created) == 73
    assert len(replicas) == 2

    task.step(0)
    task.step(2)

    assert any(_is_query_expression(sql) for sql in replicas[0].executed)
    assert not any(sql.strip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in replicas[0].executed)
    assert any(sql.strip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in primary.executed)


def test_worker_断连保留原sql并独立指数退避且不改变全局状态(tmp_path: Path) -> None:
    clock = FakeClock()
    bundle = _in_memory_bundle()
    primary = RoleDatabase("primary")
    broken_replica = RoleDatabase(
        "replica",
        failures=[LostConnectionError("断连一次")],
    )
    healthy_replica = RoleDatabase("replica")
    replicas = iter([broken_replica, healthy_replica])
    task = FuzzTask(
        task_id="task-worker-retry",
        node=_node(),
        base_sql_bundle=bundle,
        db=primary,
        replica_db_factory=lambda: next(replicas),
        thread_count=2,
        query_seed="33",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=clock,
    )
    task.start()
    task._workers[0].pending_sql = "SELECT 1"
    task._workers[0].pending_operation = "SELECT"

    assert task.step(0) == 0.1
    pending_sql = task._workers[0].pending_sql
    assert pending_sql is not None
    assert task.status is TaskStatus.RUNNING
    task.step(1)
    assert task.success_query_total == 1

    clock.advance(1)
    assert task.step(0) == 0
    assert task._workers[0].pending_sql is None
    assert broken_replica.executed.count(pending_sql) == 1
    assert task.worker_states[0]["reconnect_total"] == 1


def test_尚未建立会话的角色worker不会被误判为被动断连(tmp_path: Path) -> None:
    replica = RoleDatabase("replica")
    task = FuzzTask(
        task_id="task-role-before-first-connect",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=RoleDatabase("primary"),
        replica_db_factory=lambda: replica,
        thread_count=1,
        query_seed="34",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()

    state_before_connect = task.worker_states[0]
    assert state_before_connect["needs_reconnect"] is False
    assert state_before_connect["reconnect_total"] == 0

    task.step(0)

    state_after_connect = task.worker_states[0]
    assert state_after_connect["needs_reconnect"] is False
    assert state_after_connect["reconnect_total"] == 0


def test_复用初始化主连接的首个dml_worker第一次重连会计入主节点汇总(tmp_path: Path) -> None:
    clock = FakeClock()
    primary = RoleDatabase("primary")
    task = FuzzTask(
        task_id="task-first-dml-reconnect",
        node=_node(),
        base_sql_bundle=load_base_sql_bundle(BUILTIN_BASE_SQL_DIR),
        db=primary,
        primary_db_factory=lambda: RoleDatabase("primary"),
        replica_db_factory=lambda: RoleDatabase("replica"),
        thread_count=1,
        enable_crud=True,
        query_seed="35",
        crud_seed="36",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=clock,
    )
    task.start()
    first_dml = task._workers[1]
    assert first_dml.worker_key == "dml:t0"
    assert first_dml.session_ready is True
    assert first_dml.has_connected is True
    primary.failures.append(LostConnectionError("主节点断连一次"))

    assert task.step(first_dml.worker_id) == 0.1
    clock.advance(1)
    assert task.step(first_dml.worker_id) == 0

    state = task.worker_states[first_dml.worker_id]
    assert state["reconnect_total"] == 1
    assert task.snapshot_counts()["primary_reconnect_total"] == 1


def test_角色worker工厂部分失败会关闭已创建连接(tmp_path: Path) -> None:
    bundle = load_base_sql_bundle(BUILTIN_BASE_SQL_DIR)
    primary = RoleDatabase("primary")
    created = [RoleDatabase("replica"), RoleDatabase("replica")]
    calls = 0

    def replica_factory() -> RoleDatabase:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("模拟备库工厂失败")
        return created[calls - 1]

    task = FuzzTask(
        task_id="task-partial-factory",
        node=_node(),
        base_sql_bundle=bundle,
        db=primary,
        replica_db_factory=replica_factory,
        thread_count=3,
        query_seed="44",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )

    with pytest.raises(RuntimeError, match="模拟备库工厂失败"):
        task.start()

    assert task.status is TaskStatus.FAILED
    assert [db.close_count for db in created] == [1, 1]


def test_角色worker工厂返回前停止不会继续创建剩余客户端(tmp_path: Path) -> None:
    first_factory_entered = threading.Event()
    allow_first_factory = threading.Event()
    created: list[RoleDatabase] = []

    def replica_factory() -> RoleDatabase:
        database = RoleDatabase("replica")
        created.append(database)
        if len(created) == 1:
            first_factory_entered.set()
            assert allow_first_factory.wait(timeout=3), "测试等待放行首个备库工厂超时"
        return database

    task = FuzzTask(
        task_id="task-stop-during-role-factory",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=RoleDatabase("primary"),
        replica_db_factory=replica_factory,
        thread_count=8,
        query_seed="45",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    result: list[InitializationResult] = []
    start_thread = threading.Thread(target=lambda: result.append(task.start()))
    start_thread.start()
    assert first_factory_entered.wait(timeout=3)

    task.stop()
    allow_first_factory.set()
    start_thread.join(timeout=3)

    assert not start_thread.is_alive()
    assert result == [InitializationResult.STOPPED]
    assert len(created) == 1
    assert created[0].close_count >= 1
    assert task.status is TaskStatus.STOPPED


def test_覆盖率快照与查询生成并发时不会迭代变更中的字典(tmp_path: Path) -> None:
    task = FuzzTask(
        task_id="task-coverage-snapshot",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=RoleDatabase("primary"),
        replica_db_factory=lambda: RoleDatabase("replica"),
        thread_count=1,
        query_seed="46",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()
    generator = task._workers[0].generator

    class MutatingCoverage(dict):
        def items(self):
            iterator = iter(super().items())
            first = next(iterator)
            self["late-key"] = 1
            yield first
            yield from iterator

    generator.coverage_counts = MutatingCoverage({"first-key": 1})

    assert task.coverage_counts == {"first-key": 1}


def test_dml_已知冲突只计失败且不写sql日志或失败文件(tmp_path: Path) -> None:
    bundle = load_base_sql_bundle(BUILTIN_BASE_SQL_DIR)
    primary = RoleDatabase("primary")
    task = FuzzTask(
        task_id="task-silent-conflict",
        node=_node(),
        base_sql_bundle=bundle,
        db=primary,
        primary_db_factory=lambda: RoleDatabase("primary"),
        replica_db_factory=lambda: RoleDatabase("replica"),
        thread_count=1,
        enable_crud=True,
        query_seed="55",
        crud_seed="66",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()
    dml_worker = task._workers[1]
    conflict = RuntimeError(1062, "Duplicate entry")
    original_execute = dml_worker.db.execute

    def fail_dml(sql: str):
        if sql.strip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            raise conflict
        return original_execute(sql)

    dml_worker.db.execute = fail_dml  # type: ignore[method-assign]

    assert task.step(dml_worker.worker_id) == 0

    assert task.crud_failed_total == 1
    assert not list((tmp_path / "logs").rglob("*.jsonl"))
    assert not list((tmp_path / "logs" / "failed_sql").rglob("*.sql"))


@pytest.mark.parametrize(
    ("table_not_ready_code", "table_not_ready_message"),
    [(1049, "Unknown database"), (1146, "Table doesn't exist")],
)
def test_同一pending_sql连续断连和表未就绪按指数退避(
    tmp_path: Path,
    table_not_ready_code: int,
    table_not_ready_message: str,
) -> None:
    clock = FakeClock()
    replica = RoleDatabase(
        "replica",
        failures=[
            LostConnectionError("断连一"),
            LostConnectionError("断连二"),
            RuntimeError(table_not_ready_code, table_not_ready_message),
        ],
    )
    task = FuzzTask(
        task_id="task-backoff",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=RoleDatabase("primary"),
        replica_db_factory=lambda: replica,
        thread_count=1,
        query_seed="77",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=clock,
    )
    task.start()
    task._workers[0].pending_sql = "SELECT 1"
    task._workers[0].pending_operation = "SELECT"

    delays = []
    pending_sql = None
    for seconds in (0, 1, 1):
        clock.advance(seconds)
        delays.append(task.step(0))
        pending_sql = pending_sql or task._workers[0].pending_sql

    assert delays == [0.1, 0.2, 0.4]
    assert task._workers[0].pending_sql == pending_sql
    assert task.status is TaskStatus.RUNNING
    assert task.lost_connection_total == 2

    clock.advance(1)
    assert task.step(0) == 0
    assert task._workers[0].pending_sql is None


def test_兼容主库查询路径消费query_seed并公开稳定worker身份(tmp_path: Path) -> None:
    def build_task(task_id: str) -> FuzzTask:
        databases = [FakeDatabase(), FakeDatabase()]
        iterator = iter(databases[1:])
        task = FuzzTask(
            task_id=task_id,
            node=_node(),
            base_sql_bundle=_in_memory_bundle(),
            db=databases[0],
            db_factory=lambda: next(iterator),
            thread_count=2,
            query_seed="88",
            query_generator_version="v1",
            metric_store=MetricStore(tmp_path / f"{task_id}.db"),
            log_dir=tmp_path / "logs",
            clock=FakeClock(),
        )
        task.start()
        return task

    first = build_task("task-query-seed-a")
    second = build_task("task-query-seed-b")

    assert [row["worker_key"] for row in first.worker_states] == ["query:0", "query:1"]
    assert all(row["target"] == _node().address for row in first.worker_states)
    assert all(row["db_role"] == "replica" for row in first.worker_states)
    assert all(row["generator_version"] == "v1" for row in first.worker_states)
    assert [row["generator_seed"] for row in first.worker_states] == [
        str(derive_worker_seed("88", "query", row["worker_key"]))
        for row in first.worker_states
    ]
    for first_worker, second_worker in zip(first._workers, second._workers):
        first_sql = first_worker.generator.generate(first.tables)
        second_sql = second_worker.generator.generate(second.tables)
        assert first_sql == second_sql


def test_runtime_严格按query与crud请求版本调用注册派发(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import select_fuzz.runner.task as task_module

    bundle = load_base_sql_bundle(BUILTIN_BASE_SQL_DIR)
    query_versions: list[tuple[str, int | None]] = []
    crud_versions: list[tuple[str, int | None, str]] = []
    original_query_factory = task_module.create_query_generator
    original_crud_factory = task_module.create_crud_generator

    def create_query(version: str, seed: int | None, **kwargs):
        query_versions.append((version, seed))
        return original_query_factory("v1", seed, **kwargs)

    def create_crud(version: str, seed: int | None, **kwargs):
        crud_versions.append((version, seed, kwargs["base_table_seed"]))
        return original_crud_factory("v1", seed, **kwargs)

    monkeypatch.setattr(task_module, "create_query_generator", create_query)
    monkeypatch.setattr(task_module, "create_crud_generator", create_crud)
    task = FuzzTask(
        task_id="task-version-dispatch",
        node=_node(),
        base_sql_bundle=bundle,
        db=RoleDatabase("primary"),
        primary_db_factory=lambda: RoleDatabase("primary"),
        replica_db_factory=lambda: RoleDatabase("replica"),
        thread_count=2,
        enable_crud=True,
        query_seed="88",
        query_generator_version="v-query-requested",
        crud_seed="99",
        crud_generator_version="v-crud-requested",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )

    task.start()

    assert [version for version, _seed in query_versions] == [
        "v-query-requested",
        "v-query-requested",
    ]
    assert len(crud_versions) == 74
    assert {version for version, _seed, _base_seed in crud_versions} == {"v-crud-requested"}


def test_角色worker被看门狗关闭后保留pending并进入独立重连(tmp_path: Path) -> None:
    clock = FakeClock()
    replica = RoleDatabase("replica")
    task = FuzzTask(
        task_id="task-role-stalled",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=RoleDatabase("primary"),
        replica_db_factory=lambda: replica,
        thread_count=1,
        query_seed="99",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=clock,
    )
    task.start()
    task.step(0)
    worker = task._workers[0]
    worker.pending_sql = "SELECT 42"
    worker.pending_operation = "SELECT"
    task.record_worker_sql_start(0, worker.pending_sql)
    clock.advance(10)

    assert task.interrupt_stalled_workers(5) == [0]

    assert worker.session_ready is False
    assert worker.pending_sql == "SELECT 42"
    assert task.status is TaskStatus.RUNNING


def test_角色worker登记sql后停止不会继续发送数据库请求(tmp_path: Path) -> None:
    replica = RoleDatabase("replica")
    task = FuzzTask(
        task_id="task-stop-before-execute",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=RoleDatabase("primary"),
        replica_db_factory=lambda: replica,
        thread_count=1,
        query_seed="100",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()
    registered = threading.Event()
    allow_execute = threading.Event()
    original_record = task.record_worker_sql_start

    def blocking_record(*args, **kwargs) -> None:
        original_record(*args, **kwargs)
        registered.set()
        assert allow_execute.wait(timeout=3), "测试等待放行 SQL 执行超时"

    task.record_worker_sql_start = blocking_record  # type: ignore[method-assign]
    thread = threading.Thread(target=lambda: task.step(0))
    thread.start()
    assert registered.wait(timeout=3)

    task.stop()
    executed_before_release = list(replica.executed)
    allow_execute.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert replica.executed == executed_before_release
    assert task.status is TaskStatus.STOPPED


def test_角色worker迟到完成use时终态会再次关闭且不复活会话(tmp_path: Path) -> None:
    class BlockingUseDatabase(RoleDatabase):
        def __init__(self) -> None:
            super().__init__("replica")
            self.use_started = threading.Event()
            self.allow_use = threading.Event()

        def execute(self, sql: str) -> int:
            if sql.startswith("USE "):
                self.use_started.set()
                assert self.allow_use.wait(timeout=3), "测试等待放行 USE 超时"
            return super().execute(sql)

    replica = BlockingUseDatabase()
    task = FuzzTask(
        task_id="task-late-use",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=RoleDatabase("primary"),
        replica_db_factory=lambda: replica,
        thread_count=1,
        query_seed="101",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()
    thread = threading.Thread(target=lambda: task.step(0))
    thread.start()
    assert replica.use_started.wait(timeout=3)

    task.stop()
    replica.allow_use.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert task.status is TaskStatus.STOPPED
    assert task._workers[0].session_ready is False
    assert replica.connected is False
    assert replica.close_count >= 2
    assert not any(_is_query_expression(sql) for sql in replica.executed)


def test_角色worker被动断开后下一轮按独立连接重连(tmp_path: Path) -> None:
    class PassiveRoleDatabase(RoleDatabase):
        def __init__(self) -> None:
            super().__init__("replica")
            self.connect_count = 0
            self.connected = False

        def connect(self) -> None:
            self.connect_count += 1
            self.connected = True

        def execute(self, sql: str) -> int:
            if not self.connected:
                raise RuntimeError("数据库连接尚未显式建立")
            return super().execute(sql)

        def connection_diagnostics(self) -> dict:
            return {
                "connection_open": self.connected,
                "connection_connect_count": self.connect_count,
                # 模拟驱动/服务端被动关闭：本地没有调用 close()。
                "connection_close_count": max(0, self.connect_count - 1),
            }

    replica = PassiveRoleDatabase()
    task = FuzzTask(
        task_id="task-passive-role",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=RoleDatabase("primary"),
        replica_db_factory=lambda: replica,
        thread_count=1,
        query_seed="110",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()
    task.step(0)
    assert replica.connect_count == 1
    replica.connected = False
    task.worker_states

    task.step(0)

    state = task.worker_states[0]
    assert replica.connect_count == 2
    assert task.success_query_total == 2
    assert state["needs_reconnect"] is False
    assert state["reconnect_total"] == 1
    assert task.snapshot_counts()["replica_reconnect_total"] == 1


def test_角色worker看门狗中断后的成功重连会计入主备汇总(tmp_path: Path) -> None:
    clock = FakeClock()
    replica = RoleDatabase("replica")
    task = FuzzTask(
        task_id="task-watchdog-role-reconnect",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=RoleDatabase("primary"),
        replica_db_factory=lambda: replica,
        thread_count=1,
        query_seed="111",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=clock,
    )
    task.start()
    task.step(0)
    reconnects_before_watchdog = task.worker_states[0]["reconnect_total"]
    task.record_worker_sql_start(0, "SELECT SLEEP(999)", clock())
    clock.advance(31)
    assert task.interrupt_stalled_workers(30) == [0]
    clock.advance(1)

    task.step(0)

    assert task.worker_states[0]["reconnect_total"] == reconnects_before_watchdog + 1
    assert task.snapshot_counts()["replica_reconnect_total"] == reconnects_before_watchdog + 1


def test_角色worker在use完成后登记会话前停止也不会复活连接(tmp_path: Path) -> None:
    replica = RoleDatabase("replica")
    task = FuzzTask(
        task_id="task-stop-after-use",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=RoleDatabase("primary"),
        replica_db_factory=lambda: replica,
        thread_count=1,
        query_seed="109",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()
    use_finished = threading.Event()
    allow_registration = threading.Event()
    original_execute_initialization = task._execute_initialization_statement

    def block_after_use(db: DatabaseClient, statement: str) -> None:
        original_execute_initialization(db, statement)
        if statement.startswith("USE "):
            use_finished.set()
            assert allow_registration.wait(timeout=3), "测试等待放行会话登记超时"

    task._execute_initialization_statement = block_after_use  # type: ignore[method-assign]
    thread = threading.Thread(target=lambda: task.step(0))
    thread.start()
    assert use_finished.wait(timeout=3)

    task.stop()
    allow_registration.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert task.status is TaskStatus.STOPPED
    assert task._workers[0].session_ready is False
    assert replica.connected is False
    assert not any(_is_query_expression(sql) for sql in replica.executed)


def test_只有角色查询禁用锁定读和临时表而legacy保留默认选项(tmp_path: Path) -> None:
    class CapturingGenerator:
        coverage_counts: dict[str, int] = {}
        recent_hits: list[str] = []
        last_sql_validity = "合法"
        last_risk_tags: list[str] = []
        last_expected_error = False

        def __init__(self) -> None:
            self.options = []

        def generate(self, _tables, options) -> str:
            self.options.append(options)
            return "SELECT 1"

    legacy = FuzzTask(
        task_id="task-options-legacy",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=FakeDatabase(),
        query_seed="102",
        metric_store=MetricStore(tmp_path / "legacy.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    legacy.start()
    legacy_generator = CapturingGenerator()
    legacy._workers[0].generator = legacy_generator
    legacy.step(0)

    role = FuzzTask(
        task_id="task-options-role",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=FakeDatabase(),
        replica_db_factory=lambda: FakeDatabase(),
        query_seed="103",
        metric_store=MetricStore(tmp_path / "role.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    role.start()
    role_generator = CapturingGenerator()
    role._workers[0].generator = role_generator
    role.step(0)

    assert legacy_generator.options[0].allow_locking is True
    assert legacy_generator.options[0].allow_temporary_tables is True
    assert role_generator.options[0].allow_locking is False
    assert role_generator.options[0].allow_temporary_tables is False


def test_主备worker断连分别记录角色事件且不写逐条sql失败日志(tmp_path: Path) -> None:
    store = MetricStore(tmp_path / "metrics.db")
    task = FuzzTask(
        task_id="task-role-events",
        node=_node(),
        primary_target="172.18.4.12:3306",
        replica_target="172.18.4.13:3307",
        base_sql_bundle=load_base_sql_bundle(BUILTIN_BASE_SQL_DIR),
        db=RoleDatabase("primary"),
        primary_db_factory=lambda: RoleDatabase("primary"),
        replica_db_factory=lambda: RoleDatabase("replica"),
        thread_count=1,
        enable_crud=True,
        query_seed="104",
        crud_seed="105",
        metric_store=store,
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()
    query_worker = task._workers[0]
    dml_worker = task._workers[1]
    query_worker.db.failures = [LostConnectionError("备库断连")]
    dml_worker.db.failures = [LostConnectionError("主库断连")]

    task.step(query_worker.worker_id)
    task.step(dml_worker.worker_id)

    events = store.list_lost_connection_events(task.task_id)
    by_role = {event["db_role"]: event for event in events}
    assert set(by_role) == {"primary", "replica"}
    assert by_role["replica"]["target"] == "172.18.4.13:3307"
    assert by_role["replica"]["worker_type"] == "query"
    assert by_role["replica"]["operation"] == "SELECT"
    assert by_role["replica"]["generator_seed"] == task.worker_states[0]["generator_seed"]
    assert by_role["primary"]["target"] == "172.18.4.12:3306"
    assert by_role["primary"]["worker_type"] == "dml"
    assert by_role["primary"]["table_name"] == dml_worker.table_name
    assert by_role["primary"]["operation"] in {"INSERT", "UPDATE", "DELETE"}
    assert not list((tmp_path / "logs").rglob("*.sql.jsonl"))
    assert not list((tmp_path / "logs" / "failed_sql").rglob("*.sql"))


def test_角色worker成功和普通错误只更新内存不逐条写sqlite指标(tmp_path: Path) -> None:
    replica = RoleDatabase("replica")
    task = FuzzTask(
        task_id="task-role-memory-metrics",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=RoleDatabase("primary"),
        replica_db_factory=lambda: replica,
        query_seed="106",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()
    metric_writes = 0
    original_write_metrics = task._write_metrics

    def count_metrics() -> None:
        nonlocal metric_writes
        metric_writes += 1
        original_write_metrics()

    task._write_metrics = count_metrics  # type: ignore[method-assign]
    class FixedGenerator:
        coverage_counts: dict[str, int] = {}
        recent_hits: list[str] = []
        last_sql_validity = "合法"
        last_risk_tags: list[str] = []
        last_expected_error = False

        def generate(self, *_args, **_kwargs) -> str:
            return "SELECT 1"

    task._workers[0].generator = FixedGenerator()
    task.step(0)
    replica.failures = [RuntimeError("普通 SQL 错误")]
    task.step(0)

    assert task.success_query_total == 1
    assert task.failed_query_total == 1
    assert metric_writes == 0


def test_一个角色worker日志io阻塞不阻止另一个worker完成(tmp_path: Path) -> None:
    replicas = [RoleDatabase("replica"), RoleDatabase("replica")]
    iterator = iter(replicas)
    task = FuzzTask(
        task_id="task-role-log-lock",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=RoleDatabase("primary"),
        replica_db_factory=lambda: next(iterator),
        thread_count=2,
        query_seed="108",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()

    class FixedGenerator:
        coverage_counts: dict[str, int] = {}
        recent_hits: list[str] = []
        last_sql_validity = "合法"
        last_risk_tags: list[str] = []
        last_expected_error = False

        def generate(self, *_args, **_kwargs) -> str:
            return "SELECT 1"

    for worker in task._workers:
        worker.generator = FixedGenerator()
    log_started = threading.Event()
    allow_log = threading.Event()
    second_done = threading.Event()
    original_write_log = task._write_sql_log

    def blocking_write_log(status: str, sql: str, *args, **kwargs) -> None:
        if kwargs.get("worker_key") == "query:0":
            log_started.set()
            assert allow_log.wait(timeout=3), "测试等待放行日志写入超时"
        original_write_log(status, sql, *args, **kwargs)

    task._write_sql_log = blocking_write_log  # type: ignore[method-assign]
    first_thread = threading.Thread(target=lambda: task.step(0))
    second_thread = threading.Thread(
        target=lambda: (task.step(1), second_done.set())
    )
    first_thread.start()
    assert log_started.wait(timeout=3)
    second_thread.start()

    assert second_done.wait(timeout=0.5), "另一个 worker 被日志 I/O 持有的 task 锁阻塞"
    allow_log.set()
    first_thread.join(timeout=3)
    second_thread.join(timeout=3)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert task.success_query_total == 2


def test_一个角色worker追加失败sql时不占用任务全局锁(tmp_path: Path) -> None:
    replicas = [RoleDatabase("replica"), RoleDatabase("replica")]
    iterator = iter(replicas)
    task = FuzzTask(
        task_id="task-role-failed-sql-lock",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=RoleDatabase("primary"),
        replica_db_factory=lambda: next(iterator),
        thread_count=2,
        query_seed="110",
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()

    class FixedGenerator:
        coverage_counts: dict[str, int] = {}
        recent_hits: list[str] = []
        last_sql_validity = "合法"
        last_risk_tags: list[str] = []
        last_expected_error = False

        def generate(self, *_args, **_kwargs) -> str:
            return "SELECT 1"

    for worker in task._workers:
        worker.generator = FixedGenerator()
    replicas[0].failures = [RuntimeError("普通 SQL 错误")]
    failed_sql_started = threading.Event()
    allow_failed_sql = threading.Event()
    second_done = threading.Event()
    original_write_failed_sql = task._write_failed_sql

    def blocking_write_failed_sql(sql: str) -> None:
        failed_sql_started.set()
        assert allow_failed_sql.wait(timeout=3), "测试等待放行失败 SQL 写入超时"
        original_write_failed_sql(sql)

    task._write_failed_sql = blocking_write_failed_sql  # type: ignore[method-assign]
    first_thread = threading.Thread(target=lambda: task.step(0))
    second_thread = threading.Thread(target=lambda: (task.step(1), second_done.set()))
    first_thread.start()
    assert failed_sql_started.wait(timeout=3)
    second_thread.start()

    try:
        assert second_done.wait(timeout=0.5), "失败 SQL I/O 占用了任务全局锁"
    finally:
        allow_failed_sql.set()
        first_thread.join(timeout=3)
        second_thread.join(timeout=3)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert task.failed_query_total == 1
    assert task.success_query_total == 1


def _base_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "base"
    directory.mkdir()
    (directory / "002_child.sql").write_text(
        "CREATE TABLE child_table (child_id BIGINT NOT NULL, parent_id BIGINT NOT NULL, PRIMARY KEY (child_id));",
        encoding="utf-8",
    )
    (directory / "001_parent.sql").write_text(
        "CREATE TABLE parent_table (id BIGINT NOT NULL, name VARCHAR(64), PRIMARY KEY (id));",
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


def _in_memory_bundle(*, expanded: bool = False, seed: str | None = None):
    return build_base_sql_bundle(
        (
            BaseSqlFile(
                path=Path("t0.sql"),
                sql="CREATE TABLE t0 (id BIGINT NOT NULL, PRIMARY KEY (id));\n",
            ),
            BaseSqlFile(
                path=Path("t2.sql"),
                sql="CREATE TEMPORARY TABLE t2 (id BIGINT NOT NULL, PRIMARY KEY (id));\n",
            ),
            BaseSqlFile(
                path=Path("zz_seed_fk_data.sql"),
                sql="INSERT INTO t0 (id) VALUES (1);\nINSERT INTO t2 (id) VALUES (2);\n",
            ),
        ),
        expand_base_table_columns=expanded,
        generator_version="v1" if expanded else None,
        seed=seed if expanded else None,
    )


def test_无效基表目录在数据库连接前失败(tmp_path: Path) -> None:
    directory = tmp_path / "invalid_base"
    directory.mkdir()
    (directory / "seed.sql").write_text("INSERT INTO missing VALUES (1);", encoding="utf-8")

    class CountingDatabase(FakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.connect_count = 0

        def connect(self) -> None:
            self.connect_count += 1
            super().connect()

    db = CountingDatabase()
    task = FuzzTask(
        task_id="task-invalid",
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
        raise AssertionError("无效基表必须在连接前失败")

    assert db.connect_count == 0
    assert db.executed == []
    assert task.phase == "准备基表"


def test_注入内存包时无需基表目录且所有_worker_使用同一对象(tmp_path: Path) -> None:
    bundle = _in_memory_bundle(expanded=True, seed="12345")
    databases = [FakeDatabase(), FakeDatabase(), FakeDatabase()]
    database_iter = iter(databases[1:])
    task = FuzzTask(
        task_id="task-bundle",
        node=_node(),
        base_sql_bundle=bundle,
        db=databases[0],
        db_factory=lambda: next(database_iter),
        thread_count=3,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    consumed_bundles = []
    original_prepare = task._prepare_worker_session

    def record_prepare(db: DatabaseClient, prepared_bundle) -> None:
        consumed_bundles.append(prepared_bundle)
        original_prepare(db, prepared_bundle)

    task._prepare_worker_session = record_prepare

    task.start()

    assert task.base_sql_bundle is bundle
    assert consumed_bundles == [bundle, bundle]
    assert task.expand_base_table_columns is True
    assert task.base_table_seed == "12345"
    assert task.base_table_generator_version == "v1"


def test_目录删除后恢复检测和_worker_重连仍复用启动时内存包(tmp_path: Path) -> None:
    clock = FakeClock()
    directory = _base_dir_with_temporary_table(tmp_path)
    db = FakeDatabase()
    task = FuzzTask(
        task_id="task-reuse",
        node=_node(),
        base_sql_dir=directory,
        db=db,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=clock,
    )
    task.start()
    original_bundle = task.base_sql_bundle
    assert original_bundle is not None
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()
    consumed_bundles = []
    original_prepare = task._prepare_worker_session

    def record_prepare(db_arg: DatabaseClient, prepared_bundle) -> None:
        consumed_bundles.append(prepared_bundle)
        original_prepare(db_arg, prepared_bundle)

    task._prepare_worker_session = record_prepare

    task._handle_lost_connection("SELECT 1", "Lost connection")
    clock.advance(60)
    task.probe_recovery()
    task.record_worker_sql_start(0, "SELECT SLEEP(999)", clock())
    clock.advance(31)
    task.interrupt_stalled_workers(30)
    assert task._ensure_worker_session(0, task._workers[0]) is True

    assert task.status is TaskStatus.RUNNING
    assert task.base_sql_bundle is original_bundle
    assert consumed_bundles == [original_bundle, original_bundle]


def test_目录删除后被动断连也使用原内存包重建临时会话(tmp_path: Path) -> None:
    class PassiveDiagnosticDatabase(FakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.connect_count = 0
            self.close_count = 0

        def connect(self) -> None:
            self.connect_count += 1
            super().connect()

        def close(self) -> None:
            self.close_count += 1
            super().close()

        def connection_diagnostics(self) -> dict:
            return {
                "connection_open": self.connected,
                "connection_connect_count": self.connect_count,
                "connection_close_count": self.close_count,
            }

    directory = _base_dir_with_temporary_table(tmp_path)
    db = PassiveDiagnosticDatabase()
    task = FuzzTask(
        task_id="task-passive-reuse",
        node=_node(),
        base_sql_dir=directory,
        db=db,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()
    original_bundle = task.base_sql_bundle
    assert original_bundle is not None
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()
    prepared_bundles = []
    original_prepare = task._prepare_worker_session

    def record_prepare(db_arg: DatabaseClient, prepared_bundle) -> None:
        prepared_bundles.append(prepared_bundle)
        original_prepare(db_arg, prepared_bundle)

    task._prepare_worker_session = record_prepare
    db.connected = False
    task.worker_states

    assert task._ensure_worker_session(0, task._workers[0]) is True
    assert db.connected is True
    assert prepared_bundles == [original_bundle]
    assert task.base_sql_bundle is original_bundle


def test_暂停保留内存包而停止后释放_sql_引用(tmp_path: Path) -> None:
    bundle = _in_memory_bundle()
    task = FuzzTask(
        task_id="task-release",
        node=_node(),
        base_sql_bundle=bundle,
        db=FakeDatabase(),
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()

    task.pause()
    assert task.base_sql_bundle is bundle

    task.stop()
    assert task.base_sql_bundle is None
    assert task.snapshot_counts()["base_table_seed"] is None


def test_任务停止或失败后迟到恢复入口不会改写终态(tmp_path: Path) -> None:
    stopped = FuzzTask(
        task_id="task-stopped-terminal",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=FakeDatabase(),
        metric_store=MetricStore(tmp_path / "stopped.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    stopped.start()
    stopped.stop()
    stopped._handle_lost_connection("SELECT 1", "Lost connection")
    stopped.probe_recovery()
    assert stopped._ensure_worker_session(0, stopped._workers[0]) is False
    stopped.fail(RuntimeError("迟到失败"))
    assert stopped.status is TaskStatus.STOPPED
    assert stopped.phase == "已停止"

    failed = FuzzTask(
        task_id="task-failed-terminal",
        node=_node(),
        base_sql_bundle=_in_memory_bundle(),
        db=FakeDatabase(),
        metric_store=MetricStore(tmp_path / "failed.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    failed.start()
    failed.fail(RuntimeError("先失败"), phase="执行 SQL")
    failed.stop()
    failed.pause()
    failed.resume()
    failed._handle_lost_connection("SELECT 1", "Lost connection")
    assert failed.status is TaskStatus.FAILED
    assert failed.phase == "执行 SQL"
    assert failed.last_error == "执行 SQL失败: 先失败"


def test_扩展任务_sql_jsonl_只记录复现元数据而不记录初始化_sql(tmp_path: Path) -> None:
    bundle = _in_memory_bundle(expanded=True, seed="18446744073709551615")
    task = FuzzTask(
        task_id="task-expanded-log",
        node=_node(),
        base_sql_bundle=bundle,
        db=FakeDatabase(),
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
        random_seed=7,
    )

    task.start()
    task.step()

    log_path = tmp_path / "logs" / "2026-06-04" / "task-expanded-log.sql.jsonl"
    rows = read_jsonl(log_path)
    assert rows[0]["expand_base_table_columns"] is True
    assert rows[0]["base_table_seed"] == "18446744073709551615"
    assert rows[0]["base_table_generator_version"] == "v1"
    assert "CREATE TABLE t0" not in log_path.read_text(encoding="utf-8")


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
    assert any(_is_query_expression(sql) for sql in db.executed)
    assert "SELECT" in (tmp_path / "logs" / "2026-06-04" / "task-1.sql.jsonl").read_text(encoding="utf-8")


def test_每次执行查询前设置_session_最大执行时间为_5_秒(tmp_path: Path) -> None:
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
    startup_sql_count = len(db.executed)

    task.step()

    assert db.executed[startup_sql_count] == "SET SESSION max_execution_time = 5000"
    assert _is_query_expression(db.executed[startup_sql_count + 1])


@pytest.mark.parametrize("method_name", ("execute", "query_scalar"))
def test_pymysql_未显式连接时不会由_sql_操作隐式重连(method_name: str) -> None:
    client = PyMySQLClient(_node())
    connect_calls = 0

    def unexpected_connect() -> None:
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("SQL 操作不应隐式调用 connect")

    client.connect = unexpected_connect  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="数据库连接尚未显式建立"):
        getattr(client, method_name)("SELECT 1")

    assert connect_calls == 0


def test_step_在记录_sql_后被停止不再发送_set_或查询(tmp_path: Path) -> None:
    class FixedGenerator:
        coverage_counts: dict[str, int] = {}
        recent_hits: list[str] = []

        def generate(self, *_args, **_kwargs) -> str:
            return "SELECT 1"

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
    task._workers[0].generator = FixedGenerator()
    startup_sql_count = len(db.executed)
    sql_recorded = threading.Event()
    allow_dispatch = threading.Event()
    original_record = task.record_worker_sql_start

    def blocking_record(*args, **kwargs) -> None:
        original_record(*args, **kwargs)
        sql_recorded.set()
        assert allow_dispatch.wait(timeout=3), "测试等待放行 SQL 派发超时"

    task.record_worker_sql_start = blocking_record  # type: ignore[method-assign]
    worker = threading.Thread(target=task.step)
    worker.start()
    assert sql_recorded.wait(timeout=3)

    task.stop()
    allow_dispatch.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert task.status is TaskStatus.STOPPED
    assert db.executed[startup_sql_count:] == []


def test_worker_迟到重连在任务停止后会关闭新连接(tmp_path: Path) -> None:
    class DelayedReconnectDatabase(FakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.delay_next_connect = False
            self.reconnect_started = threading.Event()
            self.allow_reconnect = threading.Event()
            self.close_count = 0

        def connect(self) -> None:
            if self.delay_next_connect:
                self.reconnect_started.set()
                assert self.allow_reconnect.wait(timeout=3), "测试等待放行重连超时"
            self.connected = True

        def close(self) -> None:
            self.close_count += 1
            self.connected = False

        def connection_diagnostics(self) -> dict:
            return {"connection_open": self.connected}

    db = DelayedReconnectDatabase()
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
    db.delay_next_connect = True
    with task._lock:
        task._worker_states[0].needs_reconnect = True
    worker = threading.Thread(target=task.step)
    worker.start()
    assert db.reconnect_started.wait(timeout=3)

    task.stop()
    db.allow_reconnect.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert task.status is TaskStatus.STOPPED
    assert db.connected is False
    assert db.close_count >= 3


def test_worker_重连返回时已停止不再准备会话_sql(tmp_path: Path) -> None:
    class StopWhileReconnectDatabase(FakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.delay_next_connect = False
            self.reconnect_started = threading.Event()
            self.allow_reconnect = threading.Event()
            self.stop_close_started = threading.Event()
            self.allow_stop_close = threading.Event()
            self.sql_after_stop = threading.Event()
            self.stop_thread: threading.Thread | None = None
            self.task: FuzzTask | None = None

        def connect(self) -> None:
            if self.delay_next_connect:
                self.reconnect_started.set()
                assert self.allow_reconnect.wait(timeout=3), "测试等待放行重连超时"
            self.connected = True

        def close(self) -> None:
            if threading.current_thread() is self.stop_thread:
                self.stop_close_started.set()
                assert self.allow_stop_close.wait(timeout=3), "测试等待放行停止关闭超时"
            self.connected = False

        def execute(self, sql: str) -> None:
            if self.task is not None and self.task.status is TaskStatus.STOPPED:
                self.sql_after_stop.set()
            super().execute(sql)

        def connection_diagnostics(self) -> dict:
            return {"connection_open": self.connected}

    db = StopWhileReconnectDatabase()
    task = FuzzTask(
        task_id="task-1",
        node=_node(),
        base_sql_dir=_base_dir(tmp_path),
        db=db,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    db.task = task
    task.start()
    db.delay_next_connect = True
    with task._lock:
        task._worker_states[0].needs_reconnect = True
    worker = threading.Thread(target=task.step)
    worker.start()
    assert db.reconnect_started.wait(timeout=3)

    stop_thread = threading.Thread(target=task.stop)
    db.stop_thread = stop_thread
    stop_thread.start()
    assert db.stop_close_started.wait(timeout=3)

    db.allow_reconnect.set()
    worker.join(timeout=3)
    db.allow_stop_close.set()
    stop_thread.join(timeout=3)

    assert not worker.is_alive()
    assert not stop_thread.is_alive()
    assert db.sql_after_stop.is_set() is False
    assert db.connected is False


def test_恢复探测迟到_ping_在任务停止后会关闭新连接(tmp_path: Path) -> None:
    class DelayedPingDatabase(FakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.ping_started = threading.Event()
            self.allow_ping = threading.Event()
            self.close_count = 0

        def ping(self) -> bool:
            self.ping_started.set()
            assert self.allow_ping.wait(timeout=3), "测试等待放行 ping 超时"
            self.connected = True
            return True

        def close(self) -> None:
            self.close_count += 1
            self.connected = False

    db = DelayedPingDatabase()
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
    with task._lock:
        task._set_status_locked(TaskStatus.RECOVERING)
        task._next_probe_at = None
    probe = threading.Thread(target=task.probe_recovery)
    probe.start()
    assert db.ping_started.wait(timeout=3)

    task.stop()
    db.allow_ping.set()
    probe.join(timeout=3)

    assert not probe.is_alive()
    assert task.status is TaskStatus.STOPPED
    assert db.connected is False
    assert db.close_count >= 2


def test_运行时生成选项不包含向量强制开关(tmp_path: Path) -> None:
    class RecordingGenerator:
        coverage_counts: dict[str, int] = {}
        recent_hits: list[str] = []

        def __init__(self, sql: str) -> None:
            self.sql = sql
            self.has_require_vector: bool | None = None

        def generate(self, _tables, options) -> str:
            self.has_require_vector = hasattr(options, "require_vector")
            return self.sql

    databases = [FakeDatabase(), FakeDatabase(), FakeDatabase()]
    database_iter = iter(databases[1:])
    task = FuzzTask(
        task_id="task-1",
        node=_node(),
        base_sql_dir=_base_dir(tmp_path),
        db=databases[0],
        db_factory=lambda: next(database_iter),
        thread_count=3,
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        clock=FakeClock(),
    )
    task.start()
    generators = [RecordingGenerator(f"SELECT {index}") for index in range(3)]
    for worker, generator in zip(task._workers, generators):
        worker.generator = generator

    for worker_id in range(3):
        task.step(worker_id)

    assert [generator.has_require_vector for generator in generators] == [False, False, False]
    assert [database.executed[-1] for database in databases] == ["SELECT 0", "SELECT 1", "SELECT 2"]


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
    assert _is_query_expression(content)
    assert "\"status\"" not in content


def test_sql_日志记录生成风险分类和预期错误(tmp_path: Path) -> None:
    class IntentionallyInvalidGenerator:
        coverage_counts: dict[str, int] = {}
        recent_hits: list[str] = []
        last_sql_validity = "故意不合法"
        last_risk_tags = ["invalid_function_arity"]
        last_expected_error = True

        def generate(self, *_args, **_kwargs) -> str:
            return "SELECT JSON_EXTRACT('{}')"

    db = FakeDatabase()
    db.fail_next_ordinary_error = True
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
    task._workers[0].generator = IntentionallyInvalidGenerator()

    task.step()

    log_rows = read_jsonl(tmp_path / "logs" / "2026-06-04" / "task-1.sql.jsonl")
    assert log_rows[0]["status"] == "普通错误"
    assert log_rows[0]["sql_validity"] == "故意不合法"
    assert log_rows[0]["risk_tags"] == ["invalid_function_arity"]
    assert log_rows[0]["expected_error"] is True


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
            if self.remaining_lost_queries > 0 and _is_query_expression(sql):
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
            if _is_query_expression(sql) and "`T2`" in normalized:
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
    assert db.executed[before] == "SET SESSION max_execution_time = 5000"
    assert _is_query_expression(db.executed[before + 1])
    assert len(db.executed) == before + 2


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


def test_看门狗中断长_sql_会记录失败日志和任务告警(tmp_path: Path) -> None:
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

    log_rows = read_jsonl(tmp_path / "logs" / "2026-06-04" / "task-1.sql.jsonl")
    failed_sql = (tmp_path / "logs" / "failed_sql" / "2026-06-04" / "task-1.sql").read_text(encoding="utf-8")
    assert interrupted == [0]
    assert task.failed_query_total == 1
    assert task.status is TaskStatus.RUNNING
    assert task.last_error is not None
    assert "worker 0 执行 SQL 超过 30 秒" in task.last_error
    assert log_rows[0]["status"] == "疑似卡住"
    assert log_rows[0]["sql"] == "SELECT SLEEP(999)"
    assert "已中断并准备重连" in log_rows[0]["error_message"]
    assert "SELECT SLEEP(999)" in failed_sql


def test_worker_状态包含连接诊断和关闭原因(tmp_path: Path) -> None:
    class DiagnosticDatabase(FakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.connect_count = 0
            self.close_count = 0
            self.connection_id = 4101

        def connect(self) -> None:
            super().connect()
            self.connect_count += 1

        def close(self) -> None:
            super().close()
            self.close_count += 1

        def connection_diagnostics(self) -> dict:
            return {
                "connection_open": self.connected,
                "connection_id": self.connection_id if self.connected else None,
                "connection_connect_count": self.connect_count,
                "connection_close_count": self.close_count,
            }

    clock = FakeClock()
    db = DiagnosticDatabase()
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

    running_state = task.worker_states[0]
    assert running_state["connection_open"] is True
    assert running_state["connection_id"] == 4101
    assert running_state["connection_connect_count"] == 1
    assert running_state["connection_close_count"] == 0
    assert running_state["last_connection_close_reason"] is None

    task.record_worker_sql_start(0, "SELECT SLEEP(999)", clock())
    clock.advance(31)
    task.interrupt_stalled_workers(timeout_seconds=30)
    stalled_state = task.worker_states[0]

    assert stalled_state["connection_open"] is False
    assert stalled_state["connection_close_count"] == 1
    assert "已中断并准备重连" in stalled_state["last_connection_close_reason"]


def test_看门狗中断后下一轮会重连并重建临时表(tmp_path: Path) -> None:
    class ReconnectTemporaryDatabase(FakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.connect_count = 0
            self.temporary_t2_created = False
            self.temporary_t2_execute_checks = 0

        def connect(self) -> None:
            super().connect()
            self.connect_count += 1
            self.temporary_t2_created = False

        def close(self) -> None:
            super().close()
            self.temporary_t2_created = False

        def execute(self, sql: str) -> None:
            normalized = sql.strip().upper()
            if not self.connected:
                raise RuntimeError("worker 连接未恢复")
            if normalized.startswith("CREATE TEMPORARY TABLE `T2`"):
                self.temporary_t2_created = True
            if _is_query_expression(sql) and "`T2`" in normalized:
                if not self.temporary_t2_created:
                    raise RuntimeError("worker 临时表会话未恢复")
                self.temporary_t2_execute_checks += 1
            super().execute(sql)

        def query_scalar(self, sql: str) -> int:
            if "`t2`" in sql and not self.temporary_t2_created:
                raise RuntimeError("worker 临时表会话未恢复")
            return super().query_scalar(sql)

    class FixedTemporaryTableGenerator:
        coverage_counts: dict[str, int] = {}
        recent_hits: list[str] = []

        def generate(self, *_args, **_kwargs) -> str:
            return "SELECT COUNT(*) FROM `t2`"

    clock = FakeClock()
    db = ReconnectTemporaryDatabase()
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
    task._workers[0].generator = FixedTemporaryTableGenerator()
    task.record_worker_sql_start(0, "SELECT SLEEP(999)", clock())
    clock.advance(31)
    task.interrupt_stalled_workers(timeout_seconds=30)

    task.step()

    assert db.connected is True
    assert db.connect_count == 2
    assert db.executed.count("USE `select_fuzz`") == 2
    assert len([sql for sql in db.executed if sql.startswith("CREATE TEMPORARY TABLE `t2`")]) == 2
    assert db.executed.count("INSERT INTO `t2` (`id`) VALUES (2)") == 2
    assert db.temporary_t2_execute_checks == 1
    assert task.success_query_total == 1
    assert task.failed_query_total == 1


def test_worker_被动断开后下一轮会重连并重建临时表(tmp_path: Path) -> None:
    class PassiveClosedTemporaryDatabase(FakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.connect_count = 0
            self.close_count = 0
            self.connection_id = 8100
            self.temporary_t2_created = False
            self.temporary_t2_execute_checks = 0

        def connect(self) -> None:
            super().connect()
            self.connect_count += 1
            self.connection_id += 1
            self.temporary_t2_created = False

        def close(self) -> None:
            super().close()
            self.close_count += 1
            self.temporary_t2_created = False

        def execute(self, sql: str) -> None:
            normalized = sql.strip().upper()
            if not self.connected:
                raise RuntimeError("worker 连接未恢复")
            if normalized.startswith("CREATE TEMPORARY TABLE `T2`"):
                self.temporary_t2_created = True
            if _is_query_expression(sql) and "`T2`" in normalized:
                if not self.temporary_t2_created:
                    raise RuntimeError("worker 临时表会话未恢复")
                self.temporary_t2_execute_checks += 1
            super().execute(sql)

        def query_scalar(self, sql: str) -> int:
            if "`t2`" in sql and not self.temporary_t2_created:
                raise RuntimeError("worker 临时表会话未恢复")
            return super().query_scalar(sql)

        def connection_diagnostics(self) -> dict:
            return {
                "connection_open": self.connected,
                "connection_id": self.connection_id if self.connected else None,
                "connection_connect_count": self.connect_count,
                "connection_close_count": self.close_count,
            }

    class FixedTemporaryTableGenerator:
        coverage_counts: dict[str, int] = {}
        recent_hits: list[str] = []

        def generate(self, *_args, **_kwargs) -> str:
            return "SELECT COUNT(*) FROM `t2`"

    clock = FakeClock()
    db = PassiveClosedTemporaryDatabase()
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
    task._workers[0].generator = FixedTemporaryTableGenerator()
    db.connected = False
    db.temporary_t2_created = False

    disconnected_state = task.worker_states[0]
    task.step()

    assert disconnected_state["needs_reconnect"] is True
    assert disconnected_state["connection_open"] is False
    assert "外部关闭" in disconnected_state["last_connection_close_reason"]
    assert db.connected is True
    assert db.connect_count == 2
    assert db.close_count == 1
    assert db.executed.count("USE `select_fuzz`") == 2
    assert len([sql for sql in db.executed if sql.startswith("CREATE TEMPORARY TABLE `t2`")]) == 2
    assert db.executed.count("INSERT INTO `t2` (`id`) VALUES (2)") == 2
    assert db.temporary_t2_execute_checks == 1
    assert task.success_query_total == 1
    assert task.failed_query_total == 0


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
