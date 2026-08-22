from __future__ import annotations

from threading import Lock
import time

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.execution.sessions import (
    ActiveSessionRegistry,
    SessionLease,
    acquire_session_pair,
)


class _Session:
    def __init__(self, connection_id: int) -> None:
        self._connection_id = connection_id
        self.aborted = False
        self.closed = False

    def connection_id(self) -> int:
        return self._connection_id

    def is_alive(self) -> bool:
        return not self.closed

    def execute(self, sql: str) -> object:
        raise AssertionError(f"unexpected SQL: {sql}")

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.closed = True


class _PairFactory:
    def __init__(self, failures: dict[NodeRole, Exception] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[NodeRole] = []
        self.sessions: dict[NodeRole, _Session] = {}
        self._lock = Lock()

    def open_query_session(self, node: NodeConfig, database: str) -> SessionLease:
        assert database == "sf_case_1"
        with self._lock:
            self.calls.append(node.role)
        # 让两个建连任务真实重叠，防止测试只覆盖串行实现。
        time.sleep(0.01)
        failure = self.failures.get(node.role)
        if failure is not None:
            raise failure
        session = _Session(100 + len(self.sessions))
        self.sessions[node.role] = session
        return SessionLease(
            role=node.role,
            session=session,
            connection_id=session.connection_id(),
            timings_ns={"total": 1},
            close_callback=session.close,
        )


def _nodes() -> tuple[NodeConfig, NodeConfig]:
    return (
        NodeConfig(role=NodeRole.CUSTOM_OFF, host="127.0.0.1", port=3307),
        NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1", port=3308),
    )


def test_pair_acquisition_attempts_both_roles_and_preserves_independent_failure() -> None:
    factory = _PairFactory(
        {NodeRole.CUSTOM_OFF: ConnectionError("custom_off 握手失败")}
    )

    acquisition = acquire_session_pair(_nodes(), "sf_case_1", factory)

    assert acquisition.ready is False
    assert set(factory.calls) == {NodeRole.CUSTOM_OFF, NodeRole.CUSTOM_ON}
    off = acquisition.attempts[NodeRole.CUSTOM_OFF]
    on = acquisition.attempts[NodeRole.CUSTOM_ON]
    assert off.opened is False
    assert on.opened is True
    assert off.failure_evidence is not None
    assert off.failure_evidence["exception"]["message"] == "custom_off 握手失败"
    assert on.failure_evidence is None
    # 成对建连不完整时，已成功的一侧必须立即释放，不能留下 Sleep 连接。
    assert factory.sessions[NodeRole.CUSTOM_ON].closed is True


def test_pair_acquisition_returns_owned_leases_when_both_sides_succeed() -> None:
    factory = _PairFactory()

    acquisition = acquire_session_pair(_nodes(), "sf_case_1", factory)

    assert acquisition.ready is True
    assert acquisition.leases[NodeRole.CUSTOM_OFF].closed is False
    assert acquisition.leases[NodeRole.CUSTOM_ON].closed is False
    acquisition.close()
    assert all(session.closed for session in factory.sessions.values())


def test_active_session_registry_aborts_only_current_sessions() -> None:
    registry = ActiveSessionRegistry()
    released = _Session(1)
    active = _Session(2)
    registry.register(released)
    registry.register(active)
    registry.unregister(released)

    aborted = registry.abort_all()

    assert aborted == 1
    assert released.aborted is False
    assert active.aborted is True


def test_session_lease_close_is_idempotent() -> None:
    session = _Session(1)
    lease = SessionLease(
        role=NodeRole.CUSTOM_OFF,
        session=session,
        connection_id=1,
        timings_ns={},
        close_callback=session.close,
    )

    lease.close()
    lease.close()

    assert lease.closed is True
    assert session.closed is True
