from __future__ import annotations

from contextlib import contextmanager
from threading import Event
import time

import pytest

from select_fuzz.config import NodeConfig, NodeRole
import select_fuzz.execution.timeout as timeout_module
from select_fuzz.execution.timeout import KillQueryWatchdog


class _ControlCursor:
    columns = ()

    def fetchmany(self, size: int) -> tuple[tuple[object, ...], ...]:
        return ()

    def warnings(self) -> tuple[str, ...]:
        return ()

    def close(self) -> None:
        return None


class _ControlSession:
    def __init__(self, killed: list[str], kill_seen: Event) -> None:
        self._killed = killed
        self._kill_seen = kill_seen

    def connection_id(self) -> int:
        return 9001

    def execute(self, sql: str) -> _ControlCursor:
        self._killed.append(sql)
        self._kill_seen.set()
        return _ControlCursor()

    def close(self) -> None:
        return None

    def abort(self) -> None:
        return None


class _ControlFactory:
    def __init__(self) -> None:
        self.killed: list[str] = []
        self.kill_seen = Event()
        self.control_databases: list[str] = []

    @contextmanager
    def control_session(self, node: NodeConfig, database: str):  # type: ignore[no-untyped-def]
        self.control_databases.append(database)
        yield _ControlSession(self.killed, self.kill_seen)


class _SlowControlSession(_ControlSession):
    def __init__(
        self,
        killed: list[str],
        kill_seen: Event,
        release_kill: Event,
    ) -> None:
        super().__init__(killed, kill_seen)
        self._release_kill = release_kill

    def execute(self, sql: str) -> _ControlCursor:
        self._killed.append(sql)
        self._kill_seen.set()
        assert self._release_kill.wait(2)
        return _ControlCursor()


class _SlowControlFactory(_ControlFactory):
    def __init__(self) -> None:
        super().__init__()
        self.release_kill = Event()

    @contextmanager
    def control_session(self, node: NodeConfig, database: str):  # type: ignore[no-untyped-def]
        self.control_databases.append(database)
        yield _SlowControlSession(self.killed, self.kill_seen, self.release_kill)


@pytest.fixture
def node() -> NodeConfig:
    return NodeConfig(role=NodeRole.BASELINE, host="127.0.0.1", port=33061)


def test_watchdog_uses_an_independent_control_session_and_integer_kill(
    node: NodeConfig,
) -> None:
    factory = _ControlFactory()
    watchdog = KillQueryWatchdog(factory)

    handle = watchdog.arm(
        node,
        "sf_case_1",
        connection_id=41,
        timeout_s=0.01,
        statement_token="old",
    )

    assert factory.kill_seen.wait(1)
    handle.cancel(statement_token="old")
    assert handle.timed_out is True
    assert handle.fired is True
    assert factory.killed == ["KILL QUERY 41"]
    assert factory.control_databases == ["sf_case_1"]
    assert handle.thread_alive is False


def test_watchdog_cancellation_joins_before_connection_can_be_reused(
    node: NodeConfig,
) -> None:
    factory = _ControlFactory()
    watchdog = KillQueryWatchdog(factory)

    for ordinal in range(50):
        token = f"statement-{ordinal}"
        handle = watchdog.arm(
            node,
            "sf_case_1",
            connection_id=41,
            timeout_s=0.05,
            statement_token=token,
        )
        handle.cancel(statement_token=token)
        assert handle.thread_alive is False

    time.sleep(0.07)
    assert factory.killed == []


def test_watchdog_reuses_scheduler_instead_of_starting_a_thread_per_statement(
    node: NodeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _ControlFactory()
    watchdog = KillQueryWatchdog(factory)
    warmup = watchdog.arm(node, "sf_case_1", 41, 10)
    warmup.cancel()
    real_thread = timeout_module.Thread
    started: list[str | None] = []

    def recording_thread(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        started.append(kwargs.get("name") if isinstance(kwargs.get("name"), str) else None)
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(timeout_module, "Thread", recording_thread)

    for _ in range(20):
        handle = watchdog.arm(node, "sf_case_1", 41, 10)
        handle.cancel()

    assert started == []


def test_scheduler_survives_one_action_thread_start_failure(
    node: NodeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _ControlFactory()
    scheduler = timeout_module._DeadlineScheduler()
    first = timeout_module.KillHandle(
        factory,
        node,
        "sf_case_1",
        41,
        0.05,
        object(),
        None,
        None,
        0.01,
        scheduler,
    )
    second = timeout_module.KillHandle(
        factory,
        node,
        "sf_case_1",
        42,
        0.1,
        object(),
        None,
        None,
        0.01,
        scheduler,
    )
    real_thread = timeout_module.Thread

    class _FailStartThread:
        def start(self) -> None:
            raise RuntimeError("injected action thread start failure")

    def thread_factory(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if kwargs.get("name") == "select-fuzz-kill-baseline-41":
            return _FailStartThread()
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(timeout_module, "Thread", thread_factory)

    assert factory.kill_seen.wait(1)
    first.cancel()
    second.cancel()

    assert first.kill_error_type == "RuntimeError"
    assert second.timed_out is True
    assert factory.killed == ["KILL QUERY 42"]


def test_scheduler_compacts_cancelled_handles_behind_a_live_deadline(
    node: NodeConfig,
) -> None:
    factory = _ControlFactory()
    scheduler = timeout_module._DeadlineScheduler()
    blocker = timeout_module.KillHandle(
        factory,
        node,
        "sf_case_1",
        41,
        60,
        object(),
        None,
        None,
        0.01,
        scheduler,
    )
    cancelled = [
        timeout_module.KillHandle(
            factory,
            node,
            "sf_case_1",
            connection_id,
            120,
            object(),
            None,
            None,
            0.01,
            scheduler,
        )
        for connection_id in range(42, 554)
    ]

    for handle in cancelled:
        handle.cancel()

    with scheduler._condition:
        retained = len(scheduler._deadlines)
    blocker.cancel()

    assert retained <= 257


def test_fallback_abort_uses_absolute_grace_even_when_control_kill_blocks(
    node: NodeConfig,
) -> None:
    factory = _SlowControlFactory()
    abort_seen = Event()
    statement_done = Event()
    handle = KillQueryWatchdog(factory, kill_grace_s=0.02).arm(
        node,
        "sf_case_1",
        connection_id=41,
        timeout_s=0.01,
        fallback_abort=abort_seen.set,
        statement_done=statement_done,
    )

    assert factory.kill_seen.wait(1)
    assert abort_seen.wait(0.2)

    statement_done.set()
    factory.release_kill.set()
    handle.cancel()
    assert handle.timed_out is True
    assert handle.thread_alive is False


def test_failed_local_abort_falls_back_to_server_connection_kill(
    node: NodeConfig,
) -> None:
    factory = _ControlFactory()

    def unsupported_abort() -> None:
        raise RuntimeError("safe local abort is unavailable")

    handle = KillQueryWatchdog(factory, kill_grace_s=0.01).arm(
        node,
        "sf_case_1",
        connection_id=41,
        timeout_s=0.01,
        fallback_abort=unsupported_abort,
        statement_done=Event(),
    )

    assert factory.kill_seen.wait(1)
    handle.cancel()

    assert handle.timed_out is True
    assert factory.killed == ["KILL QUERY 41", "KILL CONNECTION 41"]
    assert handle.kill_error_type == "RuntimeError"


def test_watchdog_rejects_a_stale_statement_token(node: NodeConfig) -> None:
    factory = _ControlFactory()
    handle = KillQueryWatchdog(factory).arm(
        node,
        "sf_case_1",
        connection_id=41,
        timeout_s=10,
        statement_token="current",
    )

    with pytest.raises(ValueError, match="statement token"):
        handle.cancel(statement_token="old")

    assert handle.thread_alive is True
    handle.cancel(statement_token="current")
    assert handle.thread_alive is False


def test_statement_tokens_are_capabilities_not_value_equal_labels(
    node: NodeConfig,
) -> None:
    factory = _ControlFactory()
    token = ["current"]
    handle = KillQueryWatchdog(factory).arm(
        node,
        "sf_case_1",
        connection_id=41,
        timeout_s=10,
        statement_token=token,
    )

    with pytest.raises(ValueError, match="statement token"):
        handle.cancel(statement_token=["current"])

    handle.cancel(statement_token=token)


def test_default_handle_can_be_cancelled_without_an_external_token(
    node: NodeConfig,
) -> None:
    factory = _ControlFactory()
    handle = KillQueryWatchdog(factory).arm(
        node,
        "sf_case_1",
        connection_id=41,
        timeout_s=10,
    )

    handle.cancel()

    assert handle.thread_alive is False
    assert factory.killed == []


@pytest.mark.parametrize("connection_id", [0, -1, True, "41"])
def test_watchdog_rejects_unsafe_connection_ids(
    node: NodeConfig,
    connection_id: object,
) -> None:
    factory = _ControlFactory()

    with pytest.raises((TypeError, ValueError)):
        KillQueryWatchdog(factory).arm(
            node,
            "sf_case_1",
            connection_id=connection_id,  # type: ignore[arg-type]
            timeout_s=1,
        )

    assert factory.killed == []
