from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from select_fuzz.api.app import create_app, create_default_app
from select_fuzz.api.schemas import TaskCreateRequest
from select_fuzz.api.service import BUILTIN_BASE_SQL_DIR, RuntimeService, TaskSnapshot
from select_fuzz.base_tables import build_base_sql_bundle
from select_fuzz.base_tables import registry as base_table_registry
from select_fuzz.config import TargetNodeConfig
from select_fuzz.metadata.models import BaseSqlFile
from select_fuzz.monitor.events import LostConnectionEvent
from select_fuzz.monitor.logs import SqlLogRecord, append_jsonl
from select_fuzz.monitor.store import MetricStore
from select_fuzz.runner.db import DatabaseClient, LostConnectionError
from select_fuzz.runner.task import FuzzTask, TaskStatus


class ApiFakeDatabase(DatabaseClient):
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.scalar_queries: list[str] = []
        self.connect_count = 0

    def connect(self) -> None:
        self.connect_count += 1

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
    base_dir = tmp_path / "api_base"
    base_dir.mkdir(exist_ok=True)
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: ApiFakeDatabase(),
        run_background=False,
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


def _task_payload(**overrides) -> dict:
    payload = {
        "node_name": "node-a",
        "host": "172.18.4.12",
        "port": 3306,
        "username": "fuzz",
        "password": "secret",
    }
    payload.update(overrides)
    return payload


def _small_bundle(*, expanded: bool = False, seed: str | None = None):
    return build_base_sql_bundle(
        (
            BaseSqlFile(
                path=Path("t0.sql"),
                sql="CREATE TABLE t0 (id BIGINT NOT NULL, PRIMARY KEY (id));\n",
            ),
        ),
        expand_base_table_columns=expanded,
        generator_version="v1" if expanded else None,
        seed=seed if expanded else None,
    )


class _FixedQueryGenerator:
    coverage_counts: dict[str, int] = {}
    recent_hits: list[str] = []
    last_sql_validity = "合法"
    last_risk_tags: list[str] = []
    last_expected_error = False

    def generate(self, *_args, **_kwargs) -> str:
        return "SELECT 1"


class _BlockingQueryDatabase(ApiFakeDatabase):
    def __init__(self, *, fail_when_closed: bool = True) -> None:
        super().__init__()
        self.fail_when_closed = fail_when_closed
        self.query_started = threading.Event()
        self.query_release = threading.Event()
        self.closed = threading.Event()

    def execute(self, sql: str) -> None:
        if sql.strip().upper().startswith("SELECT"):
            self.query_started.set()
            if not self.query_release.wait(timeout=3):
                raise RuntimeError("测试等待放行查询超时")
            if self.fail_when_closed and self.closed.is_set():
                raise LostConnectionError("Lost connection to MySQL server during query")
        super().execute(sql)

    def close(self) -> None:
        self.closed.set()


def _join_threads(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive(), f"后台线程未按期退出: {thread.name}"


def _start_creating_task(
    service: RuntimeService,
    **payload_overrides,
) -> tuple[threading.Thread, dict[str, object]]:
    result: dict[str, object] = {}

    def create() -> None:
        try:
            result["snapshot"] = service.create_task(
                TaskCreateRequest(**_task_payload(**payload_overrides))
            )
        except BaseException as exc:  # pragma: no cover - 仅用于把线程异常带回测试线程
            result["error"] = exc

    thread = threading.Thread(target=create)
    thread.start()
    return thread, result


def test_生产模式post不等待首次主库连接且停止会关闭迟到连接(tmp_path: Path) -> None:
    class BlockingInitialDatabase(ApiFakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.connect_started = threading.Event()
            self.allow_connect = threading.Event()
            self.closed = threading.Event()

        def connect(self) -> None:
            self.connect_count += 1
            self.connect_started.set()
            assert self.allow_connect.wait(timeout=3), "测试等待放行首次连接超时"

        def close(self) -> None:
            self.closed.set()

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    database = BlockingInitialDatabase()
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: database,
        run_background=True,
    )

    response = TestClient(create_app(service)).post("/api/tasks", json=_task_payload(thread_count=3))

    assert response.status_code == 200
    assert database.connect_started.wait(timeout=3)
    assert response.json()["status"] in {"新建", "连接实例"}
    task_id = response.json()["task_id"]
    assert service.get_task(task_id)["worker_total"] == 3
    stopped = service.stop_task(task_id)
    database.allow_connect.set()
    initializer = service._initialization_threads.get(task_id)
    if initializer is not None:
        initializer.join(timeout=3)

    assert stopped.status == "已停止"
    assert database.executed == []
    assert database.closed.is_set()
    assert task_id not in service._real_tasks


def test_initializer线程启动失败会收敛为失败快照且不残留登记(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: ApiFakeDatabase(),
        run_background=True,
    )
    original_start = threading.Thread.start

    def fail_initializer_start(thread: threading.Thread) -> None:
        if thread.name.endswith("-initializer"):
            raise RuntimeError("模拟 initializer 线程启动失败")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_initializer_start)

    snapshot = service.create_task(TaskCreateRequest(**_task_payload()))

    assert snapshot.status == "失败"
    assert snapshot.phase == "连接实例"
    assert "initializer 线程启动失败" in (snapshot.last_error or "")
    assert snapshot.task_id not in service._initialization_threads
    assert snapshot.task_id not in service._initialization_stop_events
    assert snapshot.task_id not in service._initialization_wake_events
    assert snapshot.task_id not in service._real_tasks


def test_初始化重试退避可被pause中断且resume立即继续(tmp_path: Path) -> None:
    class RetryConnectDatabase(ApiFakeDatabase):
        def connect(self) -> None:
            self.connect_count += 1
            raise LostConnectionError("connection refused")

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    retry_wait_started = threading.Event()
    paused_wait_started = threading.Event()
    waits: list[float | None] = []

    def controlled_wait(wake_event: threading.Event, delay: float | None) -> bool:
        waits.append(delay)
        if delay is None:
            paused_wait_started.set()
        else:
            retry_wait_started.set()
        result = wake_event.wait(timeout=3)
        wake_event.clear()
        return result

    database = RetryConnectDatabase()
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: database,
        run_background=True,
        initialization_wait=controlled_wait,
    )
    response = TestClient(create_app(service)).post("/api/tasks", json=_task_payload(thread_count=3))
    task_id = response.json()["task_id"]
    assert retry_wait_started.wait(timeout=3)
    assert service.get_task(task_id)["worker_total"] == 3

    paused = service.pause_task(task_id)

    assert paused.status == "已暂停"
    assert paused_wait_started.wait(timeout=3)
    connect_count_while_paused = database.connect_count
    resumed = service.resume_task(task_id)
    assert resumed.status in {"连接实例", "准备基表"}
    retry_wait_started.clear()
    assert retry_wait_started.wait(timeout=3)
    assert database.connect_count == connect_count_while_paused + 1
    assert [delay for delay in waits if delay is not None][:2] == [0.1, 0.2]

    service.stop_task(task_id)


def test_initializer进入下一轮前收到pause不会覆盖暂停或继续连接(tmp_path: Path) -> None:
    class RetryConnectDatabase(ApiFakeDatabase):
        def connect(self) -> None:
            self.connect_count += 1
            raise LostConnectionError("connection refused")

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    next_attempt_entered = threading.Event()
    allow_next_attempt = threading.Event()
    database = RetryConnectDatabase()
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: database,
        run_background=True,
        initialization_wait=lambda _event, _delay: False,
    )
    original_initialize = service._initialize_task_once
    attempts = 0

    def block_second_attempt(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            next_attempt_entered.set()
            assert allow_next_attempt.wait(timeout=3), "测试等待放行下一轮初始化超时"
        return original_initialize(**kwargs)

    service._initialize_task_once = block_second_attempt  # type: ignore[method-assign]
    snapshot = service.create_task(TaskCreateRequest(**_task_payload()))
    assert next_attempt_entered.wait(timeout=3)

    paused = service.pause_task(snapshot.task_id)
    connect_count_at_pause = database.connect_count
    allow_next_attempt.set()
    initializer = service._initialization_threads.get(snapshot.task_id)
    assert initializer is not None
    # initializer 应在暂停等待中保持存活，但不能再进入 connect。
    threading.Event().wait(0.05)

    current = service.get_task(snapshot.task_id)
    assert paused.status == "已暂停"
    assert current["status"] == "已暂停"
    assert database.connect_count == connect_count_at_pause
    service.stop_task(snapshot.task_id)


def test_initializer当轮db_factory暂态失败不会覆盖并发pause(tmp_path: Path) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    first_factory_entered = threading.Event()
    allow_first_failure = threading.Event()
    paused_wait_started = threading.Event()
    second_factory_entered = threading.Event()
    calls = 0

    def db_factory(_node):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_factory_entered.set()
            assert allow_first_failure.wait(timeout=3), "测试等待放行首次工厂失败超时"
            raise LostConnectionError("connection refused")
        second_factory_entered.set()
        return ApiFakeDatabase()

    def controlled_wait(wake_event: threading.Event, delay: float | None) -> bool:
        if delay is None:
            paused_wait_started.set()
        result = wake_event.wait(timeout=3)
        wake_event.clear()
        return result

    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=db_factory,
        run_background=True,
        initialization_wait=controlled_wait,
    )
    snapshot = service.create_task(TaskCreateRequest(**_task_payload()))
    assert first_factory_entered.wait(timeout=3)

    paused = service.pause_task(snapshot.task_id)
    allow_first_failure.set()
    assert paused_wait_started.wait(timeout=3)

    current = service.get_task(snapshot.task_id)
    assert paused.status == "已暂停"
    assert current["status"] == "已暂停"
    assert calls == 1

    service.resume_task(snapshot.task_id)
    assert second_factory_entered.wait(timeout=3)
    service.stop_task(snapshot.task_id)


def test_基表包准备期间可暂停并恢复且不会误报实例不存在(tmp_path: Path) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: ApiFakeDatabase(),
        run_background=True,
    )
    prepare_entered = threading.Event()
    allow_prepare = threading.Event()
    initializer_started = threading.Event()
    original_prepare = service._prepare_base_sql_bundle
    original_start_initializer = service._start_initializer

    def blocked_prepare(**kwargs):
        prepare_entered.set()
        assert allow_prepare.wait(timeout=3), "测试等待放行基表包准备超时"
        return original_prepare(**kwargs)

    def record_initializer(**kwargs):
        initializer_started.set()
        return original_start_initializer(**kwargs)

    service._prepare_base_sql_bundle = blocked_prepare  # type: ignore[method-assign]
    service._start_initializer = record_initializer  # type: ignore[method-assign]
    created: list = []
    create_thread = threading.Thread(
        target=lambda: created.append(service.create_task(TaskCreateRequest(**_task_payload()))),
    )
    create_thread.start()
    assert prepare_entered.wait(timeout=3)
    task_id = service.list_tasks()[0]["task_id"]

    paused = service.pause_task(task_id)
    assert paused.status == "已暂停"
    resumed = service.resume_task(task_id)
    assert resumed.status == "连接实例"
    allow_prepare.set()
    create_thread.join(timeout=3)

    assert not create_thread.is_alive()
    assert initializer_started.wait(timeout=3)
    assert created and created[0].status != "失败"
    service.stop_task(task_id)


def test_生产模式确定性ddl错误仍在准备基表阶段失败(tmp_path: Path) -> None:
    class DeterministicDdlDatabase(ApiFakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.failed = threading.Event()

        def execute(self, sql: str) -> None:
            super().execute(sql)
            if sql.startswith("CREATE TABLE"):
                self.failed.set()
                raise RuntimeError(1064, "SQL syntax error")

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    database = DeterministicDdlDatabase()
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: database,
        run_background=True,
    )

    response = TestClient(create_app(service)).post("/api/tasks", json=_task_payload(thread_count=1))
    task_id = response.json()["task_id"]
    assert database.failed.wait(timeout=3)
    initializer = service._initialization_threads.get(task_id)
    if initializer is not None:
        initializer.join(timeout=3)

    snapshot = service.get_task(task_id)
    assert snapshot["status"] == "失败"
    assert snapshot["phase"] == "准备基表"
    assert "SQL syntax error" in snapshot["last_error"]


def test_生产模式跳板隧道真实暂态异常按退避重试(tmp_path: Path, monkeypatch) -> None:
    from sshtunnel import BaseSSHTunnelForwarderError

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    events: list[str] = []
    waits: list[float | None] = []
    attempts = 0
    initialized = threading.Event()

    class RetryTunnel:
        local_host = "127.0.0.1"
        local_port = 44001

        def __init__(self, jump_host, target_node) -> None:
            del jump_host, target_node

        def start(self) -> tuple[str, int]:
            nonlocal attempts
            attempts += 1
            events.append("start")
            if attempts == 1:
                raise BaseSSHTunnelForwarderError("Could not establish session to SSH gateway")
            return self.local_host, self.local_port

        def stop(self) -> None:
            events.append("stop")

    monkeypatch.setattr("select_fuzz.api.service.JumpTunnel", RetryTunnel)
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: ApiFakeDatabase(),
        run_background=True,
        initialization_wait=lambda _event, delay: waits.append(delay) or False,
    )
    service.add_jump_host(
        {"name": "jump-prod", "host": "10.2.0.8", "port": 22, "username": "ops"}
    )
    service._start_background_loop = lambda _task: initialized.set()  # type: ignore[method-assign]

    response = TestClient(create_app(service)).post(
        "/api/tasks",
        json=_task_payload(thread_count=1, jump_host="jump-prod"),
    )
    task_id = response.json()["task_id"]
    assert initialized.wait(timeout=3)

    assert waits == [0.1]
    assert events == ["start", "stop", "start"]
    assert service.get_task(task_id)["status"] == "执行 SQL"
    service.stop_task(task_id)
    assert events == ["start", "stop", "start", "stop"]


@pytest.mark.parametrize(
    ("case_name", "role_runtime", "expected_waits"),
    [
        ("legacy 成功", False, [0.37]),
        ("legacy 普通错误", False, [0.37]),
        ("role 成功", True, []),
    ],
)
def test_后台循环仅legacy零延迟step等待查询间隔(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_name: str,
    role_runtime: bool,
    expected_waits: list[float],
) -> None:
    del case_name

    class FakeStopEvent:
        def __init__(self) -> None:
            self.waits: list[float] = []
            self.stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def set(self) -> None:
            self.stopped = True

        def clear(self) -> None:
            return None

        def wait(self, seconds: float) -> bool:
            self.waits.append(seconds)
            return self.stopped

    class FakeThread:
        def __init__(self, *, target, args, name: str, daemon: bool) -> None:
            del daemon
            self.target = target
            self.args = args
            self.name = name
            self.started = False

        def start(self) -> None:
            self.started = True

        def run(self) -> None:
            self.target(*self.args)

        def is_alive(self) -> bool:
            return self.started

        def join(self, timeout: float | None = None) -> None:
            del timeout

    class FakeTask:
        task_id = "task-loop-wait"
        status = TaskStatus.RUNNING
        worker_ids = [0]

        def __init__(self) -> None:
            self._role_runtime = role_runtime
            self.terminal = False

        @property
        def is_terminal(self) -> bool:
            return self.terminal

        def interrupt_stalled_workers(self, _timeout: int) -> list[int]:
            return []

    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        run_background=False,
        query_interval_seconds=0.37,
    )
    task = FakeTask()
    service._tasks[task.task_id] = TaskSnapshot(
        task_id=task.task_id,
        node_name="node-a",
        target="127.0.0.1:3306",
    )
    stop_event = FakeStopEvent()
    monkeypatch.setattr("select_fuzz.api.service.threading.Event", lambda: stop_event)
    monkeypatch.setattr("select_fuzz.api.service.threading.Thread", FakeThread)

    def one_step(_task, _worker_id: int) -> float:
        task.terminal = True
        return 0

    service._run_task_step = one_step  # type: ignore[method-assign]
    service._start_background_loop(task)  # type: ignore[arg-type]
    worker_thread = service._background_worker_threads[task.task_id][0]
    worker_thread.run()

    assert stop_event.waits == expected_waits


def test_pause和resume会立即唤醒角色worker长退避且生命周期清理event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_event_class = threading.Event
    real_thread_class = threading.Thread

    class ControlledEvent:
        def __init__(self) -> None:
            self.inner = real_event_class()
            self.wait_started = real_event_class()
            self.waits: list[float | None] = []

        def is_set(self) -> bool:
            return self.inner.is_set()

        def set(self) -> None:
            self.inner.set()

        def clear(self) -> None:
            self.inner.clear()

        def wait(self, seconds: float | None = None) -> bool:
            self.waits.append(seconds)
            self.wait_started.set()
            return self.inner.wait(seconds)

    class FakeThread:
        def __init__(self, *, target, args, name: str, daemon: bool) -> None:
            del daemon
            self.target = target
            self.args = args
            self.name = name

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            del timeout

    task = FuzzTask(
        task_id="task-wake-retry",
        node=TargetNodeConfig(
            name="node-a",
            host="172.18.4.12",
            port=3306,
            username="fuzz",
            password="secret",
        ),
        base_sql_bundle=_small_bundle(),
        db=ApiFakeDatabase(),
        replica_db_factory=lambda: ApiFakeDatabase(),
        query_seed="107",
        metric_store=MetricStore(tmp_path / "task.db"),
        log_dir=tmp_path / "logs",
        clock=lambda: datetime.now(timezone.utc),
    )
    task.start()
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "service.db"),
        log_dir=tmp_path / "logs",
        run_background=False,
    )
    service._tasks[task.task_id] = TaskSnapshot(
        task_id=task.task_id,
        node_name="node-a",
        target="172.18.4.12:3306",
    )
    service._real_tasks[task.task_id] = task
    events = [ControlledEvent(), ControlledEvent()]
    event_iter = iter(events)
    monkeypatch.setattr(
        "select_fuzz.api.service.threading",
        SimpleNamespace(
            Event=lambda: next(event_iter),
            Thread=FakeThread,
            current_thread=threading.current_thread,
        ),
    )
    steps = 0

    def retry_then_stop(_task, _worker_id: int) -> float:
        nonlocal steps
        steps += 1
        if steps == 1:
            return 5.0
        task.stop()
        return 0

    service._run_task_step = retry_then_stop  # type: ignore[method-assign]
    service._start_background_loop(task)
    fake_worker = service._background_worker_threads[task.task_id][0]
    worker_thread = real_thread_class(target=lambda: fake_worker.target(*fake_worker.args))
    worker_thread.start()
    wake_event = service._background_wake_events[task.task_id][0]
    assert wake_event.wait_started.wait(timeout=3)

    service.pause_task(task.task_id)
    deadline = real_event_class()
    for _ in range(1000):
        if len(wake_event.waits) >= 2:
            break
        deadline.wait(0.001)
    assert len(wake_event.waits) >= 2
    assert steps == 1
    assert worker_thread.is_alive()

    service.resume_task(task.task_id)
    worker_thread.join(timeout=3)

    assert not worker_thread.is_alive()
    assert steps == 2
    service._sync_snapshot_from_task(task)
    service._background_worker_threads[task.task_id].clear()
    service._creating_task_ids.discard(task.task_id)
    service._try_finalize_terminal_task(task.task_id)
    assert task.task_id not in service._background_wake_events


def test_pause会广播唤醒全部角色worker且任一worker不能吞掉其他信号(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeThread:
        def __init__(self, *, target, args, name: str, daemon: bool) -> None:
            del daemon
            self.target = target
            self.args = args
            self.name = name

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            del timeout

    task = FuzzTask(
        task_id="task-broadcast-wake",
        node=TargetNodeConfig(
            name="node-a",
            host="172.18.4.12",
            port=3306,
            username="fuzz",
            password="secret",
        ),
        base_sql_bundle=_small_bundle(),
        db=ApiFakeDatabase(),
        replica_db_factory=lambda: ApiFakeDatabase(),
        thread_count=2,
        query_seed="112",
        metric_store=MetricStore(tmp_path / "task.db"),
        log_dir=tmp_path / "logs",
        clock=lambda: datetime.now(timezone.utc),
    )
    task.start()
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "service.db"),
        log_dir=tmp_path / "logs",
        run_background=False,
    )
    service._tasks[task.task_id] = TaskSnapshot(
        task_id=task.task_id,
        node_name="node-a",
        target="172.18.4.12:3306",
        status=TaskStatus.RUNNING.value,
    )
    service._real_tasks[task.task_id] = task
    monkeypatch.setattr("select_fuzz.api.service.threading.Thread", FakeThread)

    service._start_background_loop(task)
    wake_events = service._background_wake_events[task.task_id]
    assert set(wake_events) == {0, 1}
    assert len({id(event) for event in wake_events.values()}) == 2

    service.pause_task(task.task_id)

    assert all(event.is_set() for event in wake_events.values())
    wake_events[0].clear()
    assert wake_events[1].is_set()
    service.stop_task(task.task_id)


def test_创建任务默认关闭扩列并返回空复现参数(tmp_path: Path) -> None:
    response = _client(tmp_path).post("/api/tasks", json=_task_payload())

    assert response.status_code == 200
    assert response.json()["expand_base_table_columns"] is False
    assert response.json()["base_table_seed"] is None
    assert response.json()["base_table_generator_version"] is None


def test_任务请求默认关闭crud并使用16个查询worker且生成查询种子(tmp_path: Path) -> None:
    response = _client(tmp_path).post("/api/tasks", json=_task_payload())

    assert response.status_code == 200
    payload = response.json()
    assert payload["enable_crud"] is False
    assert payload["thread_count"] == 16
    assert payload["query_worker_total"] == 16
    assert payload["crud_worker_total"] == 0
    assert payload["worker_total"] == 16
    assert payload["query_seed"].isdigit()
    assert payload["query_generator_version"] == "v1"
    assert payload["crud_seed"] is None
    assert payload["crud_generator_version"] is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"query_generator_version": "v999"}, "未知查询生成器版本"),
        (
            {"enable_crud": True, "crud_generator_version": "v999"},
            "未知 CRUD 生成器版本",
        ),
    ),
)
def test_创建任务拒绝未知query与crud生成器版本(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    response = _client(tmp_path).post(
        "/api/tasks",
        json=_task_payload(**overrides),
    )

    assert response.status_code == 422
    assert message in response.text


def test_备库端口继承主端口且允许主备同地址并保留显式种子(tmp_path: Path) -> None:
    response = _client(tmp_path).post(
        "/api/tasks",
        json=_task_payload(
            replica_host="172.18.4.12",
            enable_crud=True,
            thread_count=1,
            query_seed="0",
            query_generator_version="v1",
            crud_seed=str(2**64 - 1),
            crud_generator_version="v1",
        ),
    )

    assert response.status_code == 422  # 自定义基表目录必须先于任何连接拒绝 CRUD
    assert "自定义基表目录不支持逐表 CRUD" in response.text


def test_只有备库端口没有备库地址返回422(tmp_path: Path) -> None:
    response = _client(tmp_path).post(
        "/api/tasks",
        json=_task_payload(replica_port=3307),
    )

    assert response.status_code == 422
    assert "replica_host" in response.text


@pytest.mark.parametrize("field", ["query_seed", "crud_seed"])
@pytest.mark.parametrize("value", [1, True, "01", "-1", str(2**64)])
def test_任务种子必须是规范uint64字符串(tmp_path: Path, field: str, value: object) -> None:
    response = _client(tmp_path).post(
        "/api/tasks",
        json=_task_payload(**{field: value}),
    )

    assert response.status_code == 422


def test_关闭crud时查询客户端路由到备库且主库只做初始化(tmp_path: Path) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    created: list[tuple[str, int, ApiFakeDatabase]] = []

    def factory(node):
        db = ApiFakeDatabase()
        created.append((node.host, node.port, db))
        return db

    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=factory,
        run_background=False,
    )
    response = TestClient(create_app(service)).post(
        "/api/tasks",
        json=_task_payload(replica_host="172.18.4.13", replica_port=3307, thread_count=2),
    )

    assert response.status_code == 200
    task = service._real_tasks[response.json()["task_id"]]
    assert [(host, port) for host, port, _db in created] == [
        ("172.18.4.12", 3306),
        ("172.18.4.13", 3307),
        ("172.18.4.13", 3307),
    ]
    task.step(0)
    query_prefixes = ("SELECT", "WITH", "(", "TABLE", "VALUES")
    assert any(sql.strip().upper().startswith(query_prefixes) for sql in created[1][2].executed)
    assert not any(sql.strip().upper().startswith(query_prefixes) for sql in created[0][2].executed)


def test_内置基表crud允许主备同地址并返回74加n汇总(tmp_path: Path) -> None:
    created: list[tuple[str, int, ApiFakeDatabase]] = []

    def factory(node):
        db = ApiFakeDatabase()
        created.append((node.host, node.port, db))
        return db

    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=BUILTIN_BASE_SQL_DIR,
        db_factory=factory,
        run_background=False,
    )
    response = TestClient(create_app(service)).post(
        "/api/tasks",
        json=_task_payload(
            replica_host="172.18.4.12",
            enable_crud=True,
            thread_count=1,
            query_seed="0",
            query_generator_version="v1",
            crud_seed=str(2**64 - 1),
            crud_generator_version="v1",
        ),
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "执行 SQL"
    assert payload["primary_target"] == "172.18.4.12:3306"
    assert payload["replica_target"] == "172.18.4.12:3306"
    assert payload["replica_host"] == "172.18.4.12"
    assert payload["replica_port"] == 3306
    assert payload["query_seed"] == "0"
    assert payload["crud_seed"] == str(2**64 - 1)
    assert payload["query_worker_total"] == 1
    assert payload["crud_worker_total"] == 74
    assert payload["worker_total"] == 75
    assert len(created) == 75
    assert len({id(row[2]) for row in created}) == 75


def test_未填写备节点时响应返回回退到主节点的replica_target(tmp_path: Path) -> None:
    response = _client(tmp_path).post("/api/tasks", json=_task_payload(thread_count=1))

    assert response.status_code == 200
    assert response.json()["replica_target"] == "172.18.4.12:3306"


def test_主备配置跳板机时分别建立并关闭两个隧道(tmp_path: Path, monkeypatch) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    events: list[tuple[str, str]] = []

    class FakeTunnel:
        local_host = "127.0.0.1"
        local_port = 44001

        def __init__(self, jump_host, target_node) -> None:
            del jump_host
            self.target_node = target_node

        def start(self):
            events.append(("start", self.target_node.host))
            return self.local_host, self.local_port

        def stop(self):
            events.append(("stop", self.target_node.host))

    monkeypatch.setattr("select_fuzz.api.service.JumpTunnel", FakeTunnel)
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: ApiFakeDatabase(),
        run_background=False,
    )
    service.add_jump_host(
        {"name": "jump-prod", "host": "10.2.0.8", "port": 22, "username": "ops"}
    )
    client = TestClient(create_app(service))
    response = client.post(
        "/api/tasks",
        json=_task_payload(
            jump_host="jump-prod",
            replica_host="172.18.4.13",
            replica_port=3307,
            thread_count=1,
        ),
    )
    client.post(f"/api/tasks/{response.json()['task_id']}/stop")

    assert events == [
        ("start", "172.18.4.12"),
        ("start", "172.18.4.13"),
        ("stop", "172.18.4.12"),
        ("stop", "172.18.4.13"),
    ]


def test_initializer成功后并发暂停不会关闭活动任务的主备隧道(tmp_path: Path, monkeypatch) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    tunnels: list = []

    class FakeTunnel:
        local_host = "127.0.0.1"

        def __init__(self, jump_host, target_node) -> None:
            del jump_host
            self.target_node = target_node
            self.local_port = 44001 + len(tunnels)
            self.stop_count = 0
            tunnels.append(self)

        def start(self):
            return self.local_host, self.local_port

        def stop(self):
            self.stop_count += 1

    monkeypatch.setattr("select_fuzz.api.service.JumpTunnel", FakeTunnel)
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: ApiFakeDatabase(),
        run_background=True,
    )
    service.add_jump_host(
        {"name": "jump-prod", "host": "10.2.0.8", "port": 22, "username": "ops"}
    )
    background_entered = threading.Event()
    allow_background_return = threading.Event()

    def blocked_background(_task) -> None:
        background_entered.set()
        assert allow_background_return.wait(timeout=3), "测试等待放行后台登记超时"

    service._start_background_loop = blocked_background  # type: ignore[method-assign]
    snapshot = service.create_task(
        TaskCreateRequest(
            **_task_payload(
                jump_host="jump-prod",
                replica_host="172.18.4.13",
                replica_port=3307,
                thread_count=1,
            )
        )
    )
    assert background_entered.wait(timeout=3)
    assert len(tunnels) == 2

    paused = service.pause_task(snapshot.task_id)
    allow_background_return.set()
    initializer = service._initialization_threads.get(snapshot.task_id)
    if initializer is not None:
        initializer.join(timeout=3)

    assert paused.status == "已暂停"
    assert [tunnel.stop_count for tunnel in tunnels] == [0, 0]
    assert set(service._task_tunnels[snapshot.task_id]) == {"primary", "replica"}

    service.stop_task(snapshot.task_id)
    assert [tunnel.stop_count for tunnel in tunnels] == [1, 1]


@pytest.mark.parametrize("value", [0, 1, "true", "false"])
def test_扩列开关只接受_json_布尔值(tmp_path: Path, value: object) -> None:
    response = _client(tmp_path).post(
        "/api/tasks",
        json=_task_payload(expand_base_table_columns=value),
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "extra_fields",
    [
        {"base_table_seed": "1"},
        {"base_table_generator_version": "v1"},
        {"base_table_seed": "", "base_table_generator_version": ""},
    ],
)
def test_关闭扩列时拒绝非空复现参数但接受空字符串(tmp_path: Path, extra_fields: dict) -> None:
    response = _client(tmp_path).post("/api/tasks", json=_task_payload(**extra_fields))

    if all(value == "" for value in extra_fields.values()):
        assert response.status_code == 200
        assert response.json()["base_table_seed"] is None
        assert response.json()["base_table_generator_version"] is None
    else:
        assert response.status_code == 422
        assert "关闭扩展基表列" in response.text


@pytest.mark.parametrize(
    "seed",
    [1, True, "-1", "+1", " 1", "1 ", "01", "1.0", "١", str(2**64)],
)
def test_开启扩列时拒绝非规范或非字符串种子(tmp_path: Path, seed: object) -> None:
    response = _client(tmp_path).post(
        "/api/tasks",
        json=_task_payload(expand_base_table_columns=True, base_table_seed=seed),
    )

    assert response.status_code == 422
    assert "基表种子" in response.text


def test_开启扩列时拒绝未知生成器版本(tmp_path: Path) -> None:
    response = _client(tmp_path).post(
        "/api/tasks",
        json=_task_payload(
            expand_base_table_columns=True,
            base_table_seed="1",
            base_table_generator_version="v999",
        ),
    )

    assert response.status_code == 422
    assert "未知基表生成器版本" in response.text


@pytest.mark.parametrize("seed", ["0", str(2**64 - 1)])
def test_开启扩列时保留手动边界种子并补全版本(tmp_path: Path, seed: str) -> None:
    response = _client(tmp_path).post(
        "/api/tasks",
        json=_task_payload(expand_base_table_columns=True, base_table_seed=seed),
    )

    assert response.status_code == 200
    assert response.json()["expand_base_table_columns"] is True
    assert response.json()["base_table_seed"] == seed
    assert response.json()["base_table_generator_version"] == "v1"


def test_开启扩列留空种子时使用_uint64_随机种子(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("select_fuzz.api.service.secrets.randbits", lambda bits: 2**64 - 1 if bits == 64 else 0)

    response = _client(tmp_path).post(
        "/api/tasks",
        json=_task_payload(
            expand_base_table_columns=True,
            base_table_seed="",
            base_table_generator_version="",
        ),
    )

    assert response.status_code == 200
    assert response.json()["base_table_seed"] == str(2**64 - 1)
    assert response.json()["base_table_generator_version"] == "v1"


def test_自定义基表目录请求扩列在连接前返回失败快照(tmp_path: Path) -> None:
    base_dir = tmp_path / "custom_base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    database = ApiFakeDatabase()
    factory_calls = 0

    def factory(_node):
        nonlocal factory_calls
        factory_calls += 1
        return database

    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=factory,
        run_background=False,
    )

    response = TestClient(create_app(service)).post(
        "/api/tasks",
        json=_task_payload(expand_base_table_columns=True, base_table_seed="12345"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "失败"
    assert response.json()["phase"] == "准备基表"
    assert "自定义基表目录不支持扩展列" in response.json()["last_error"]
    assert response.json()["base_table_seed"] == "12345"
    assert response.json()["base_table_generator_version"] == "v1"
    assert factory_calls == 0
    assert database.connect_count == 0
    assert database.executed == []


def test_自定义目录即使声明内置标志也不能绕过扩列限制(tmp_path: Path, monkeypatch) -> None:
    base_dir = tmp_path / "custom_base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    factory_calls = 0

    def factory(_node):
        nonlocal factory_calls
        factory_calls += 1
        return ApiFakeDatabase()

    monkeypatch.setattr(
        "select_fuzz.api.service.generate_base_sql_bundle",
        lambda _version, seed: _small_bundle(expanded=True, seed=seed),
    )
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        use_builtin_base_tables=True,
        db_factory=factory,
        run_background=False,
    )

    response = TestClient(create_app(service)).post(
        "/api/tasks",
        json=_task_payload(expand_base_table_columns=True, base_table_seed="12345"),
    )

    assert response.json()["status"] == "失败"
    assert response.json()["phase"] == "准备基表"
    assert "自定义基表目录不支持扩展列" in response.json()["last_error"]
    assert factory_calls == 0


def test_真实内置目录无需布尔标志即可生成扩展包(tmp_path: Path, monkeypatch) -> None:
    builtin_dir = tmp_path / "project" / "sql_base_tables"
    builtin_dir.mkdir(parents=True)
    events: list[str] = []
    database = ApiFakeDatabase()
    monkeypatch.setattr("select_fuzz.api.service.BUILTIN_BASE_SQL_DIR", builtin_dir)

    def generate(_version: str, seed: str):
        events.append(f"generate:{seed}")
        return _small_bundle(expanded=True, seed=seed)

    monkeypatch.setattr("select_fuzz.api.service.generate_base_sql_bundle", generate)
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=builtin_dir,
        db_factory=lambda _node: database,
        run_background=False,
    )

    response = TestClient(create_app(service)).post(
        "/api/tasks",
        json=_task_payload(expand_base_table_columns=True, base_table_seed="77"),
    )

    assert response.json()["status"] == "执行 SQL"
    assert response.json()["base_table_seed"] == "77"
    assert events == ["generate:77"]

    task_id = response.json()["task_id"]
    service.stop_task(task_id)
    retained = service.get_task(task_id)
    assert retained["status"] == "已停止"
    assert retained["base_table_seed"] == "77"
    assert retained["base_table_generator_version"] == "v1"
    assert task_id not in service._real_tasks


def test_内置核心模式统一从配置目录加载(tmp_path: Path, monkeypatch) -> None:
    builtin_dir = tmp_path / "project" / "sql_base_tables"
    builtin_dir.mkdir(parents=True)
    (builtin_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    events: list[str] = []
    database = ApiFakeDatabase()
    original_connect = database.connect
    monkeypatch.setattr("select_fuzz.api.service.BUILTIN_BASE_SQL_DIR", builtin_dir)

    def load(base_dir: Path):
        events.append(f"load:{Path(base_dir).resolve()}")
        return _small_bundle()

    def factory(_node):
        events.append("factory")
        return database

    def connect() -> None:
        events.append("connect")
        original_connect()

    database.connect = connect
    monkeypatch.setattr("select_fuzz.api.service.load_base_sql_bundle", load)
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=builtin_dir,
        db_factory=factory,
        run_background=False,
    )

    response = TestClient(create_app(service)).post("/api/tasks", json=_task_payload())

    assert response.json()["status"] == "执行 SQL"
    assert events[:3] == [f"load:{builtin_dir.resolve()}", "factory", "connect"]


def test_缺少基表来源会在准备基表阶段失败且不创建数据库资源(tmp_path: Path) -> None:
    factory_calls = 0

    def factory(_node):
        nonlocal factory_calls
        factory_calls += 1
        return ApiFakeDatabase()

    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        db_factory=factory,
        run_background=False,
    )

    response = TestClient(create_app(service)).post("/api/tasks", json=_task_payload())

    assert response.json()["status"] == "失败"
    assert response.json()["phase"] == "准备基表"
    assert "未配置基表目录" in response.json()["last_error"]
    assert factory_calls == 0


def test_基表包准备成功但缺少数据库工厂会返回连接失败(tmp_path: Path) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        run_background=False,
    )

    response = TestClient(create_app(service)).post("/api/tasks", json=_task_payload())

    assert response.json()["status"] == "失败"
    assert response.json()["phase"] == "连接实例"
    assert "未配置数据库客户端工厂" in response.json()["last_error"]


def test_扩展基表生成失败发生在任何数据库资源之前(tmp_path: Path, monkeypatch) -> None:
    factory_calls = 0
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()

    def fail_generation(_version: str, _seed: str):
        raise RuntimeError("模拟扩展基表生成失败")

    def factory(_node):
        nonlocal factory_calls
        factory_calls += 1
        return ApiFakeDatabase()

    monkeypatch.setattr("select_fuzz.api.service.generate_base_sql_bundle", fail_generation)
    monkeypatch.setattr("select_fuzz.api.service.BUILTIN_BASE_SQL_DIR", builtin_dir)
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=builtin_dir,
        db_factory=factory,
        run_background=False,
    )

    response = TestClient(create_app(service)).post(
        "/api/tasks",
        json=_task_payload(expand_base_table_columns=True, base_table_seed="7"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "失败"
    assert response.json()["phase"] == "准备基表"
    assert "模拟扩展基表生成失败" in response.json()["last_error"]
    assert response.json()["base_table_seed"] == "7"
    assert factory_calls == 0


def test_扩展生成器返回非法结构时在准备基表阶段失败且数据库零调用(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    database = ApiFakeDatabase()
    factory_calls = 0

    def factory(_node):
        nonlocal factory_calls
        factory_calls += 1
        return database

    monkeypatch.setitem(
        base_table_registry._GENERATORS,
        "v1",
        lambda seed: _small_bundle(expanded=True, seed=seed),
    )
    monkeypatch.setattr("select_fuzz.api.service.BUILTIN_BASE_SQL_DIR", builtin_dir)
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=builtin_dir,
        db_factory=factory,
        run_background=False,
    )

    response = TestClient(create_app(service)).post(
        "/api/tasks",
        json=_task_payload(expand_base_table_columns=True, base_table_seed="7"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "失败"
    assert response.json()["phase"] == "准备基表"
    assert "文件名顺序" in response.json()["last_error"]
    assert factory_calls == 0
    assert database.connect_count == 0
    assert database.executed == []


def test_准备基表包期间停止会阻止继续创建任何运行资源(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    prepare_started = threading.Event()
    allow_prepare = threading.Event()
    database = ApiFakeDatabase()
    factory_calls = 0

    def factory(_node):
        nonlocal factory_calls
        factory_calls += 1
        return database

    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=factory,
        run_background=False,
    )
    original_prepare = service._prepare_base_sql_bundle

    def blocking_prepare(**kwargs):
        prepare_started.set()
        assert allow_prepare.wait(timeout=3), "测试等待放行基表包准备超时"
        return original_prepare(**kwargs)

    service._prepare_base_sql_bundle = blocking_prepare  # type: ignore[method-assign]
    create_thread, result = _start_creating_task(service)
    assert prepare_started.wait(timeout=3)

    terminal = service.stop_task("task-1")
    expected_status = "已停止"
    expected_phase = "已停止"
    assert terminal.status == expected_status
    assert terminal.phase == expected_phase
    allow_prepare.set()
    create_thread.join(timeout=3)

    assert not create_thread.is_alive()
    assert "error" not in result
    assert result["snapshot"] is terminal
    assert terminal.status == expected_status
    assert terminal.phase == expected_phase
    assert terminal.last_error is None
    assert factory_calls == 0
    assert database.connect_count == 0
    assert database.executed == []
    assert "task-1" not in service._real_tasks
    assert "task-1" not in service._background_stop_events
    assert "task-1" not in service._background_worker_threads
    assert "task-1" not in service._task_tunnels


def test_数据库工厂阻塞期间停止会关闭迟到客户端且不连接或执行_sql(tmp_path: Path) -> None:
    class UnconnectedDatabase(ApiFakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    factory_started = threading.Event()
    allow_factory = threading.Event()
    database = UnconnectedDatabase()

    def blocking_factory(_node):
        factory_started.set()
        assert allow_factory.wait(timeout=3), "测试等待放行数据库工厂超时"
        return database

    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=blocking_factory,
        run_background=False,
    )
    create_thread, result = _start_creating_task(service)
    assert factory_started.wait(timeout=3)

    stopped = service.stop_task("task-1")
    allow_factory.set()
    create_thread.join(timeout=3)

    assert not create_thread.is_alive()
    assert "error" not in result
    assert stopped.status == "已停止"
    assert stopped.phase == "已停止"
    assert stopped.last_error is None
    assert database.connect_count == 0
    assert database.executed == []
    assert database.close_count >= 1
    assert "task-1" not in service._real_tasks
    assert "task-1" not in service._background_stop_events
    assert "task-1" not in service._background_worker_threads


def test_跳板隧道启动期间停止会关闭迟到隧道且不创建数据库客户端(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingTunnel:
        local_host = "127.0.0.1"
        local_port: int | None = None

        def __init__(self, jump_host, target_node) -> None:
            del jump_host, target_node

        def start(self) -> tuple[str, int]:
            tunnel_started.set()
            assert allow_tunnel.wait(timeout=3), "测试等待放行跳板隧道超时"
            self.local_port = 44001
            return self.local_host, self.local_port

        def stop(self) -> None:
            tunnel_stopped.set()

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    tunnel_started = threading.Event()
    allow_tunnel = threading.Event()
    tunnel_stopped = threading.Event()
    factory_calls = 0

    def factory(_node):
        nonlocal factory_calls
        factory_calls += 1
        return ApiFakeDatabase()

    monkeypatch.setattr("select_fuzz.api.service.JumpTunnel", BlockingTunnel)
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=factory,
        run_background=False,
    )
    service.add_jump_host(
        {"name": "jump-prod", "host": "10.2.0.8", "port": 22, "username": "ops"}
    )
    create_thread, result = _start_creating_task(service, jump_host="jump-prod")
    assert tunnel_started.wait(timeout=3)

    stopped = service.stop_task("task-1")
    allow_tunnel.set()
    create_thread.join(timeout=3)

    assert not create_thread.is_alive()
    assert "error" not in result
    assert stopped.status == "已停止"
    assert stopped.phase == "已停止"
    assert stopped.last_error is None
    assert tunnel_stopped.is_set()
    assert factory_calls == 0
    assert "task-1" not in service._task_tunnels
    assert "task-1" not in service._real_tasks


def test_首次连接期间停止后迟到连接会关闭且不执行初始化_sql(tmp_path: Path) -> None:
    class BlockingConnectDatabase(ApiFakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.connect_started = threading.Event()
            self.allow_connect = threading.Event()
            self.connected = False
            self.close_count = 0

        def connect(self) -> None:
            self.connect_count += 1
            self.connect_started.set()
            if not self.allow_connect.wait(timeout=3):
                raise RuntimeError("测试等待放行首次连接超时")
            self.connected = True

        def close(self) -> None:
            self.close_count += 1
            self.connected = False

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    database = BlockingConnectDatabase()
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: database,
        run_background=False,
    )
    create_thread, result = _start_creating_task(service)
    assert database.connect_started.wait(timeout=3)

    stopped = service.stop_task("task-1")
    assert stopped.status == "已停止"
    database.allow_connect.set()
    create_thread.join(timeout=3)

    assert not create_thread.is_alive()
    assert "error" not in result
    assert stopped.status == "已停止"
    assert stopped.phase == "已停止"
    assert stopped.last_error is None
    assert database.connect_count == 1
    assert database.executed == []
    assert database.connected is False
    assert database.close_count >= 2
    assert "task-1" not in service._real_tasks
    assert "task-1" not in service._background_stop_events
    assert "task-1" not in service._background_worker_threads


@pytest.mark.parametrize(
    ("blocked_prefix", "expected_executed"),
    [
        ("DROP DATABASE", ["DROP DATABASE IF EXISTS `test`"]),
        (
            "CREATE DATABASE",
            ["DROP DATABASE IF EXISTS `test`", "CREATE DATABASE `test`"],
        ),
        (
            "USE",
            [
                "DROP DATABASE IF EXISTS `test`",
                "CREATE DATABASE `test`",
                "USE `test`",
            ],
        ),
    ],
)
def test_重建数据库每条语句返回后都会阻止停止任务继续初始化(
    tmp_path: Path,
    blocked_prefix: str,
    expected_executed: list[str],
) -> None:
    class BlockingInitDatabase(ApiFakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.statement_started = threading.Event()
            self.allow_statement = threading.Event()
            self.connected = False

        def connect(self) -> None:
            super().connect()
            self.connected = True

        def execute(self, sql: str) -> None:
            super().execute(sql)
            if sql.startswith(blocked_prefix):
                self.statement_started.set()
                if not self.allow_statement.wait(timeout=3):
                    raise RuntimeError("测试等待放行初始化语句超时")

        def close(self) -> None:
            self.connected = False

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    database = BlockingInitDatabase()
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: database,
        run_background=False,
    )
    create_thread, result = _start_creating_task(service)
    assert database.statement_started.wait(timeout=3)

    stopped = service.stop_task("task-1")
    database.allow_statement.set()
    create_thread.join(timeout=3)

    assert not create_thread.is_alive()
    assert "error" not in result
    assert stopped.status == "已停止"
    assert stopped.phase == "已停止"
    assert stopped.last_error is None
    assert database.executed == expected_executed
    assert database.scalar_queries == []
    assert database.connected is False
    assert "task-1" not in service._real_tasks


def test_基表文件语句执行期间停止后不会继续下一条语句或数据校验(tmp_path: Path) -> None:
    class BlockingBaseStatementDatabase(ApiFakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.statement_started = threading.Event()
            self.allow_statement = threading.Event()

        def execute(self, sql: str) -> None:
            super().execute(sql)
            if sql.startswith("CREATE TABLE t0"):
                self.statement_started.set()
                if not self.allow_statement.wait(timeout=3):
                    raise RuntimeError("测试等待放行基表语句超时")

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text(
        "CREATE TABLE t0 (id BIGINT); INSERT INTO t0 VALUES (1);",
        encoding="utf-8",
    )
    database = BlockingBaseStatementDatabase()
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: database,
        run_background=False,
    )
    create_thread, result = _start_creating_task(service)
    assert database.statement_started.wait(timeout=3)

    stopped = service.stop_task("task-1")
    database.allow_statement.set()
    create_thread.join(timeout=3)

    assert not create_thread.is_alive()
    assert "error" not in result
    assert stopped.status == "已停止"
    assert database.executed[-1] == "CREATE TABLE t0 (id BIGINT)"
    assert all(not sql.startswith("INSERT INTO t0") for sql in database.executed)
    assert database.scalar_queries == []
    assert "task-1" not in service._real_tasks


def test_基表数据校验期间停止后不会继续下一个_count查询(tmp_path: Path) -> None:
    class BlockingCountDatabase(ApiFakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.count_started = threading.Event()
            self.allow_count = threading.Event()

        def query_scalar(self, sql: str) -> int:
            result = super().query_scalar(sql)
            if len(self.scalar_queries) == 1:
                self.count_started.set()
                if not self.allow_count.wait(timeout=3):
                    raise RuntimeError("测试等待放行基表数据校验超时")
            return result

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    (base_dir / "t1.sql").write_text("CREATE TABLE t1 (id BIGINT);", encoding="utf-8")
    database = BlockingCountDatabase()
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: database,
        run_background=False,
    )
    create_thread, result = _start_creating_task(service)
    assert database.count_started.wait(timeout=3)

    stopped = service.stop_task("task-1")
    database.allow_count.set()
    create_thread.join(timeout=3)

    assert not create_thread.is_alive()
    assert "error" not in result
    assert stopped.status == "已停止"
    assert database.scalar_queries == ["SELECT COUNT(*) FROM `t0`"]
    assert "task-1" not in service._real_tasks


def test_附加_worker_连接期间停止会关闭尚未注册的迟到连接(tmp_path: Path) -> None:
    class BlockingAdditionalDatabase(ApiFakeDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.connect_started = threading.Event()
            self.allow_connect = threading.Event()
            self.connected = False
            self.close_count = 0

        def connect(self) -> None:
            self.connect_count += 1
            self.connect_started.set()
            if not self.allow_connect.wait(timeout=3):
                raise RuntimeError("测试等待放行附加 worker 连接超时")
            self.connected = True

        def close(self) -> None:
            self.close_count += 1
            self.connected = False

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    first_database = ApiFakeDatabase()
    additional_database = BlockingAdditionalDatabase()
    databases = iter((first_database, additional_database))
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: next(databases),
        run_background=False,
    )
    create_thread, result = _start_creating_task(service, thread_count=2)
    assert additional_database.connect_started.wait(timeout=3)

    stopped = service.stop_task("task-1")
    additional_database.allow_connect.set()
    create_thread.join(timeout=3)

    assert not create_thread.is_alive()
    assert "error" not in result
    assert stopped.status == "已停止"
    assert additional_database.connect_count == 1
    assert additional_database.executed == []
    assert additional_database.connected is False
    assert additional_database.close_count >= 1
    assert "task-1" not in service._real_tasks


def test_停止发生在后台循环注册前不会启动或遗留任何后台线程(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: ApiFakeDatabase(),
        run_background=True,
    )
    background_entry = threading.Event()
    allow_background_registration = threading.Event()
    started_sql_threads: list[threading.Thread] = []
    original_start_background_loop = service._start_background_loop
    original_thread_start = threading.Thread.start

    def blocking_start_background_loop(task) -> None:
        background_entry.set()
        assert allow_background_registration.wait(timeout=3), "测试等待放行后台循环注册超时"
        original_start_background_loop(task)

    def record_thread_start(thread: threading.Thread) -> None:
        if thread.name.startswith("sql_fuzz-") and not thread.name.endswith("-initializer"):
            started_sql_threads.append(thread)
        original_thread_start(thread)

    service._start_background_loop = blocking_start_background_loop  # type: ignore[method-assign]
    monkeypatch.setattr(threading.Thread, "start", record_thread_start)
    create_thread, result = _start_creating_task(service)
    assert background_entry.wait(timeout=3)

    stopped = service.stop_task("task-1")
    assert stopped.status == "已停止"
    assert stopped.phase == "已停止"
    allow_background_registration.set()
    create_thread.join(timeout=3)
    initializer = service._initialization_threads.get("task-1")
    if initializer is not None:
        initializer.join(timeout=3)
    for thread in started_sql_threads:
        thread.join(timeout=3)

    assert not create_thread.is_alive()
    assert "error" not in result
    assert result["snapshot"] is stopped
    assert stopped.status == "已停止"
    assert stopped.phase == "已停止"
    assert stopped.last_error is None
    assert started_sql_threads == []
    assert "task-1" not in service._real_tasks
    assert "task-1" not in service._background_stop_events
    assert "task-1" not in service._background_worker_threads
    assert "task-1" not in service._task_tunnels


def test_后台首个线程已启动时停止不会再启动其余线程或_join_未启动线程(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: ApiFakeDatabase(),
        run_background=True,
        query_interval_seconds=0,
    )
    first_sql_thread_started = threading.Event()
    allow_remaining_thread_starts = threading.Event()
    started_sql_thread_names: list[str] = []
    original_thread_start = threading.Thread.start
    original_registered_start = service._start_registered_background_thread

    def record_thread_start(thread: threading.Thread) -> None:
        if thread.name.startswith("sql_fuzz-") and not thread.name.endswith("-initializer"):
            started_sql_thread_names.append(thread.name)
        original_thread_start(thread)

    def block_after_first_registered_start(*args, **kwargs):
        result = original_registered_start(*args, **kwargs)
        thread_key = args[2]
        if thread_key == 0 and result[0]:
            first_sql_thread_started.set()
            assert allow_remaining_thread_starts.wait(timeout=3), "测试等待放行其余后台线程启动超时"
        return result

    monkeypatch.setattr(threading.Thread, "start", record_thread_start)
    service._start_registered_background_thread = block_after_first_registered_start  # type: ignore[method-assign]
    create_thread, result = _start_creating_task(service, thread_count=2)
    assert first_sql_thread_started.wait(timeout=3)

    stopped = service.stop_task("task-1")
    allow_remaining_thread_starts.set()
    create_thread.join(timeout=3)
    initializer = service._initialization_threads.get("task-1")
    if initializer is not None:
        initializer.join(timeout=3)

    assert not create_thread.is_alive()
    assert "error" not in result
    assert stopped.status == "已停止"
    assert started_sql_thread_names == ["sql_fuzz-task-1-0"]
    assert "task-1" not in service._real_tasks
    assert "task-1" not in service._background_stop_events
    assert "task-1" not in service._background_worker_threads
    assert "task-1" not in service._task_tunnels


def test_内置基表包在_db_factory_和连接前准备(tmp_path: Path, monkeypatch) -> None:
    events: list[str] = []
    database = ApiFakeDatabase()
    original_connect = database.connect
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()

    def generate(_version: str, seed: str):
        events.append(f"generate:{seed}")
        return _small_bundle(expanded=True, seed=seed)

    def connect() -> None:
        events.append("connect")
        original_connect()

    def factory(_node):
        events.append("factory")
        return database

    database.connect = connect
    monkeypatch.setattr("select_fuzz.api.service.generate_base_sql_bundle", generate)
    monkeypatch.setattr("select_fuzz.api.service.BUILTIN_BASE_SQL_DIR", builtin_dir)
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=builtin_dir,
        db_factory=factory,
        run_background=False,
    )

    response = TestClient(create_app(service)).post(
        "/api/tasks",
        json=_task_payload(expand_base_table_columns=True, base_table_seed="9"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "执行 SQL"
    assert events[:3] == ["generate:9", "factory", "connect"]
    task = service._real_tasks[response.json()["task_id"]]
    assert task.base_sql_bundle is not None
    assert task.base_sql_bundle.seed == "9"
    assert not list((tmp_path / "logs").rglob("*.sql"))


def test_后台任务进入终态会通知所有循环并关闭跳板机(tmp_path: Path) -> None:
    class RecordingTunnel:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: ApiFakeDatabase(),
        run_background=False,
    )
    response = TestClient(create_app(service)).post("/api/tasks", json=_task_payload())
    task_id = response.json()["task_id"]
    task = service._real_tasks[task_id]
    stop_event = threading.Event()
    tunnel = RecordingTunnel()
    service._background_stop_events[task_id] = stop_event
    service._background_worker_threads[task_id] = {0: threading.current_thread()}
    service._task_tunnels[task_id] = tunnel
    task.tables.clear()

    service._run_task_step(task, 0)

    assert task.is_terminal
    assert stop_event.is_set()
    assert tunnel.stopped is True
    assert task_id not in service._task_tunnels
    assert task_id in service._real_tasks
    assert task_id in service._background_stop_events
    assert task_id in service._background_worker_threads
    service._background_thread_exited(task_id, 0)
    assert task_id not in service._real_tasks
    assert task_id not in service._background_stop_events
    assert task_id not in service._background_worker_threads
    assert task.base_sql_bundle is None
    assert service.get_task(task_id)["status"] == "失败"


def test_停止阻塞查询时迟到_lost_connection_不能把快照改回恢复中(tmp_path: Path) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    database = _BlockingQueryDatabase()
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: database,
        run_background=False,
        query_interval_seconds=0,
    )
    response = TestClient(create_app(service)).post("/api/tasks", json=_task_payload())
    task_id = response.json()["task_id"]
    task = service._real_tasks[task_id]
    task._workers[0].generator = _FixedQueryGenerator()
    service._start_background_loop(task)
    threads = list(service._background_worker_threads[task_id].values())
    assert database.query_started.wait(timeout=3)

    stop_completed = threading.Event()

    def stop_task() -> None:
        service.stop_task(task_id)
        stop_completed.set()

    stop_thread = threading.Thread(target=stop_task)
    stop_thread.start()
    assert database.closed.wait(timeout=3)
    assert stop_completed.wait(timeout=3)
    assert task_id in service._real_tasks
    assert task_id in service._background_stop_events
    assert task_id in service._background_worker_threads
    database.query_release.set()
    stop_thread.join(timeout=3)
    assert not stop_thread.is_alive()
    _join_threads(threads)

    snapshot = service.get_task(task_id)
    assert snapshot["status"] == "已停止"
    assert snapshot["phase"] == "已停止"
    assert task.status is TaskStatus.STOPPED
    assert task_id not in service._real_tasks
    assert task_id not in service._background_stop_events
    assert task_id not in service._background_worker_threads


def test_一个_worker_失败后另一个迟到_lost_connection_不能覆盖失败终态(tmp_path: Path) -> None:
    class BlockingFailureGenerator:
        coverage_counts: dict[str, int] = {}
        recent_hits: list[str] = []

        def __init__(self, allow_failure: threading.Event) -> None:
            self.allow_failure = allow_failure

        def generate(self, *_args, **_kwargs) -> str:
            if not self.allow_failure.wait(timeout=3):
                raise RuntimeError("测试等待触发 worker 失败超时")
            raise RuntimeError("模拟 worker 生成失败")

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    first_database = ApiFakeDatabase()
    late_database = _BlockingQueryDatabase()
    databases = iter((first_database, late_database))
    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: next(databases),
        run_background=False,
        query_interval_seconds=0,
    )
    response = TestClient(create_app(service)).post(
        "/api/tasks",
        json=_task_payload(thread_count=2),
    )
    task_id = response.json()["task_id"]
    task = service._real_tasks[task_id]
    allow_failure = threading.Event()
    task._workers[0].generator = BlockingFailureGenerator(allow_failure)
    task._workers[1].generator = _FixedQueryGenerator()
    service._start_background_loop(task)
    threads = list(service._background_worker_threads[task_id].values())
    assert late_database.query_started.wait(timeout=3)

    allow_failure.set()
    assert late_database.closed.wait(timeout=3)
    assert task.status is TaskStatus.FAILED
    assert task_id in service._real_tasks
    assert task_id in service._background_stop_events
    assert task_id in service._background_worker_threads
    late_database.query_release.set()
    _join_threads(threads)

    snapshot = service.get_task(task_id)
    assert snapshot["status"] == "失败"
    assert snapshot["phase"] == "执行 SQL"
    assert "模拟 worker 生成失败" in snapshot["last_error"]
    assert task.status is TaskStatus.FAILED
    assert task_id not in service._real_tasks
    assert task_id not in service._background_stop_events
    assert task_id not in service._background_worker_threads


def test_终态任务重复停止暂停恢复都不改变原终态(tmp_path: Path) -> None:
    stopped_root = tmp_path / "stopped"
    stopped_root.mkdir()
    stopped_client = _client(stopped_root)
    stopped_response = stopped_client.post("/api/tasks", json=_task_payload())
    stopped_id = stopped_response.json()["task_id"]
    assert stopped_client.post(f"/api/tasks/{stopped_id}/stop").json()["状态"] == "已停止"
    assert stopped_client.post(f"/api/tasks/{stopped_id}/stop").json()["状态"] == "已停止"
    assert stopped_client.post(f"/api/tasks/{stopped_id}/pause").json()["状态"] == "已停止"
    assert stopped_client.post(f"/api/tasks/{stopped_id}/resume").json()["状态"] == "已停止"

    failed_root = tmp_path / "failed"
    failed_root.mkdir()
    base_dir = failed_root / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    failed_service = RuntimeService(
        metric_store=MetricStore(failed_root / "metrics.db"),
        log_dir=failed_root / "logs",
        base_sql_dir=base_dir,
        db_factory=lambda _node: ApiFailingCreateDatabase(),
        run_background=False,
    )
    failed_client = TestClient(create_app(failed_service))
    failed_response = failed_client.post("/api/tasks", json=_task_payload())
    failed_id = failed_response.json()["task_id"]
    assert failed_response.json()["status"] == "失败"
    assert failed_client.post(f"/api/tasks/{failed_id}/pause").json()["状态"] == "失败"
    assert failed_client.post(f"/api/tasks/{failed_id}/resume").json()["状态"] == "失败"
    assert failed_client.post(f"/api/tasks/{failed_id}/stop").json()["状态"] == "失败"


@pytest.mark.parametrize("action", ("pause", "resume"))
def test_暂停或恢复与停止并发时快照保持终态(tmp_path: Path, action: str) -> None:
    client = _client(tmp_path)
    service = client.app.state.runtime_service
    response = client.post("/api/tasks", json=_task_payload())
    task_id = response.json()["task_id"]
    task = service._real_tasks[task_id]
    if action == "resume":
        service.pause_task(task_id)

    state_captured = threading.Event()
    allow_stale_sync = threading.Event()
    original_snapshot_counts = task.snapshot_counts
    action_thread: threading.Thread | None = None

    def blocking_snapshot_counts():
        state = original_snapshot_counts()
        if threading.current_thread() is action_thread:
            state_captured.set()
            assert allow_stale_sync.wait(timeout=3), "测试等待放行迟到快照超时"
        return state

    task.snapshot_counts = blocking_snapshot_counts  # type: ignore[method-assign]
    action_method = service.pause_task if action == "pause" else service.resume_task
    action_thread = threading.Thread(target=lambda: action_method(task_id))
    action_thread.start()
    assert state_captured.wait(timeout=3)

    stopped = service.stop_task(task_id)
    allow_stale_sync.set()
    action_thread.join(timeout=3)

    assert not action_thread.is_alive()
    assert stopped.status == "已停止"
    assert service.get_task(task_id)["status"] == "已停止"


@pytest.mark.parametrize("action", ("pause", "resume"))
def test_暂停或恢复在终态检查后遇到停止清理不覆盖终态(
    tmp_path: Path,
    action: str,
) -> None:
    client = _client(tmp_path)
    service = client.app.state.runtime_service
    response = client.post("/api/tasks", json=_task_payload())
    task_id = response.json()["task_id"]
    action_entered = threading.Event()
    allow_action = threading.Event()
    action_thread: threading.Thread | None = None
    task = service._real_tasks[task_id]
    original_action = task.pause if action == "pause" else task.resume

    def blocking_action() -> None:
        action_entered.set()
        assert allow_action.wait(timeout=3), "测试等待放行暂停/恢复超时"
        original_action()

    if action == "pause":
        task.pause = blocking_action  # type: ignore[method-assign]
    else:
        task.resume = blocking_action  # type: ignore[method-assign]
    action_method = service.pause_task if action == "pause" else service.resume_task
    action_thread = threading.Thread(target=lambda: action_method(task_id))
    action_thread.start()
    assert action_entered.wait(timeout=3)

    service.stop_task(task_id)
    assert task_id not in service._real_tasks
    allow_action.set()
    action_thread.join(timeout=3)

    assert not action_thread.is_alive()
    snapshot = service.get_task(task_id)
    assert snapshot["status"] == "已停止"
    assert snapshot["phase"] == "已停止"
    assert snapshot["last_error"] is None


def test_后台线程部分启动失败返回失败快照并收敛资源(tmp_path: Path, monkeypatch) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "t0.sql").write_text("CREATE TABLE t0 (id BIGINT);", encoding="utf-8")
    databases: list[ApiFakeDatabase] = []
    tunnel_events: list[str] = []
    first_thread_exited = threading.Event()

    class FakeTunnel:
        local_host = "127.0.0.1"
        local_port = 44001

        def __init__(self, jump_host, target_node) -> None:
            del jump_host, target_node

        def start(self) -> tuple[str, int]:
            tunnel_events.append("start")
            return self.local_host, self.local_port

        def stop(self) -> None:
            tunnel_events.append("stop")

    def factory(_node):
        database = ApiFakeDatabase()
        database.closed = False

        def close() -> None:
            database.closed = True

        database.close = close
        databases.append(database)
        return database

    service = RuntimeService(
        metric_store=MetricStore(tmp_path / "metrics.db"),
        log_dir=tmp_path / "logs",
        base_sql_dir=base_dir,
        db_factory=factory,
        run_background=True,
        query_interval_seconds=0,
    )
    service.add_jump_host(
        {"name": "jump-prod", "host": "10.2.0.8", "port": 22, "username": "ops"}
    )
    original_exited = service._background_thread_exited

    def recording_exited(task_id: str, thread_key: int) -> None:
        original_exited(task_id, thread_key)
        if thread_key == 0:
            first_thread_exited.set()

    service._background_thread_exited = recording_exited  # type: ignore[method-assign]
    original_start = threading.Thread.start
    sql_fuzz_start_count = 0

    def fail_second_sql_fuzz_start(thread: threading.Thread) -> None:
        nonlocal sql_fuzz_start_count
        if thread.name.startswith("sql_fuzz-") and not thread.name.endswith("-initializer"):
            sql_fuzz_start_count += 1
            if sql_fuzz_start_count == 2:
                raise RuntimeError("模拟后台线程启动失败")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_second_sql_fuzz_start)
    monkeypatch.setattr("select_fuzz.api.service.JumpTunnel", FakeTunnel)

    response = TestClient(create_app(service)).post(
        "/api/tasks",
        json=_task_payload(thread_count=2, jump_host="jump-prod"),
    )
    task_id = response.json()["task_id"]

    assert response.status_code == 200
    assert first_thread_exited.wait(timeout=3)
    initializer = service._initialization_threads.get(task_id)
    if initializer is not None:
        initializer.join(timeout=3)
    snapshot = service.get_task(task_id)
    assert snapshot["status"] == "失败"
    assert "模拟后台线程启动失败" in snapshot["last_error"]
    assert tunnel_events == ["start", "stop"]
    assert all(database.closed for database in databases)
    assert task_id not in service._real_tasks
    assert task_id not in service._background_stop_events
    assert task_id not in service._background_worker_threads


def test_health_返回中文状态(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["状态"] == "正常"


def test_openapi_任务响应声明基表复现字段(tmp_path: Path) -> None:
    schema = _client(tmp_path).get("/openapi.json").json()
    properties = schema["components"]["schemas"]["TaskResponse"]["properties"]

    assert properties["expand_base_table_columns"]["type"] == "boolean"
    assert "base_table_seed" in properties
    assert "base_table_generator_version" in properties


def test_创建停止任务并查询任务列表(tmp_path: Path) -> None:
    client = _client(tmp_path)
    service = client.app.state.runtime_service

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
    stop_event = threading.Event()
    service._background_stop_events[task_id] = stop_event
    service._background_worker_threads[task_id] = {0: threading.current_thread()}
    stop_response = client.post(f"/api/tasks/{task_id}/stop")
    list_response = client.get("/api/tasks")

    assert response.status_code == 200
    assert stop_response.json()["状态"] == "已停止"
    assert list_response.json()[0]["node_name"] == "node-a"
    assert stop_event.is_set()
    assert task_id in service._real_tasks
    service._background_thread_exited(task_id, 0)
    assert task_id not in service._real_tasks
    assert task_id not in service._background_stop_events
    assert task_id not in service._background_worker_threads


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
    assert response.json()["success_query_total"] == 0
    assert response.json()["failed_query_total"] == 0
    assert response.json()["ordinary_error_total"] == 0
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
    assert task_id not in service._real_tasks
    assert task_id not in service._background_stop_events
    assert task_id not in service._background_worker_threads


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

    assert service.base_sql_dir == BUILTIN_BASE_SQL_DIR
    assert service.base_sql_dir.is_absolute()


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
    assert len(response.json()["worker_states"]) == 3
    assert response.json()["success_query_total"] == 0
    assert response.json()["failed_query_total"] == 0
    created_dbs = [task_worker.db for task_worker in service._real_tasks[response.json()["task_id"]]._workers]
    assert len(created_dbs) == 3
    assert created_dbs[0].executed.count("CREATE TABLE t0 (id BIGINT NOT NULL, PRIMARY KEY (id))") == 1
    for db in created_dbs:
        assert len([sql for sql in db.executed if sql.startswith("CREATE TEMPORARY TABLE `t2`")]) == 1


def test_任务快照展示后台_worker_线程存活状态(tmp_path: Path) -> None:
    class FakeThread:
        name = "sql_fuzz-task-1-0"

        def __init__(self, alive: bool) -> None:
            self.alive = alive

        def is_alive(self) -> bool:
            return self.alive

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
    task_id = response.json()["task_id"]
    service._background_worker_threads[task_id] = {0: FakeThread(alive=False)}

    loaded = client.get(f"/api/tasks/{task_id}").json()

    assert loaded["worker_states"][0]["thread_alive"] is False
    assert loaded["worker_states"][0]["thread_name"] == "sql_fuzz-task-1-0"


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

    task_payload = client.get(f"/api/tasks/{response.json()['task_id']}").json()
    coverage = client.get("/api/coverage").json()
    hit_rows = [item for item in coverage if item["hit_count"] > 0]
    assert task_payload["success_query_total"] == 1
    assert task_payload["failed_query_total"] == 0
    assert task_payload["ordinary_error_total"] == 0
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
