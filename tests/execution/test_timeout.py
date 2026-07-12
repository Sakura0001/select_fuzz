from __future__ import annotations

from contextlib import contextmanager
from threading import Event
import time

import pytest

from select_fuzz.config import NodeConfig, NodeRole
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
