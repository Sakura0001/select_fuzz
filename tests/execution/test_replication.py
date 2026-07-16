from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.execution.replication import (
    MARKER_DDL_SQL,
    ReplicationBarrier,
    ReplicationWaitStatus,
    marker_upsert_sql,
    replication_sequence_from_sql,
    with_replication_marker,
)


class _Cursor:
    columns: tuple[()] = ()

    def __init__(self, sequence: int | None) -> None:
        self._rows = () if sequence is None else ((sequence,),)

    def fetchmany(self, size: int):  # type: ignore[no-untyped-def]
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows

    def close(self) -> None:
        return None


class _Session:
    def __init__(self, sequence: int | None) -> None:
        self.sequence = sequence

    def execute(self, sql: str) -> _Cursor:
        return _Cursor(self.sequence)

    def close(self) -> None:
        return None

    def abort(self) -> None:
        return None

    def connection_id(self) -> int:
        return 1

    def is_alive(self) -> bool:
        return True


class _Factory:
    def __init__(self, sequences: dict[NodeRole, list[int | None]]) -> None:
        self.sequences = sequences

    @contextmanager
    def control_session(self, node: NodeConfig, database: str):  # type: ignore[no-untyped-def]
        values = self.sequences[node.role]
        yield _Session(values.pop(0) if len(values) > 1 else values[0])

    query_session = control_session


class _BoundedFactory(_Factory):
    def __init__(self, sequences: dict[NodeRole, list[int | None]]) -> None:
        super().__init__(sequences)
        self.timeouts: list[float] = []
        self.deadlines: list[float] = []

    def control_session_with_timeout(self, node: NodeConfig, database: str, timeout_seconds: float):  # type: ignore[no-untyped-def]
        self.timeouts.append(timeout_seconds)
        return self.control_session(node, database)

    def control_session_until(self, node: NodeConfig, database: str, deadline_monotonic: float):  # type: ignore[no-untyped-def]
        self.deadlines.append(deadline_monotonic)
        return self.control_session(node, database)


@dataclass(frozen=True)
class _Bundle:
    statements: tuple[str, ...] = ("CREATE TABLE t (id INT)",)
    payload_sha256: str = "a" * 64
    requires_same_session: bool = False


def _nodes() -> tuple[NodeConfig, ...]:
    return tuple(
        NodeConfig(role=role, host="replica.example", port=33061 + index)
        for index, role in enumerate(NodeRole)
    )


def test_setup_wrapper_appends_initial_marker_without_changing_payload_identity() -> None:
    wrapped = with_replication_marker(_Bundle())

    assert wrapped.statements[-2:] == (MARKER_DDL_SQL, marker_upsert_sql(0))
    assert wrapped.payload_sha256 == "a" * 64
    assert with_replication_marker(wrapped) is wrapped
    assert wrapped.replication_sequence == 0


def test_marker_helpers_validate_and_find_highest_sequence() -> None:
    assert (
        replication_sequence_from_sql((marker_upsert_sql(2), "SELECT 1", marker_upsert_sql(7))) == 7
    )
    with pytest.raises(ValueError, match="nonnegative"):
        marker_upsert_sql(-1)


def test_barrier_waits_until_every_replica_observes_required_sequence() -> None:
    clock = [0.0]
    sequences = {
        NodeRole.BASELINE: [1, 2],
        NodeRole.CUSTOM_OFF: [2],
        NodeRole.CUSTOM_ON: [None, 2],
    }
    barrier = ReplicationBarrier(
        _nodes(),
        _Factory(sequences),
        timeout_seconds=10,
        poll_interval_seconds=0.1,
        monotonic=lambda: clock[0],
        sleeper=lambda delay: clock.__setitem__(0, clock[0] + delay),
    )

    result = barrier.wait("sf_replication_1", 2)

    assert result.status is ReplicationWaitStatus.READY
    assert {item.observed_sequence for item in result.observations.values()} == {2}


def test_barrier_times_out_and_retains_each_replica_observation() -> None:
    clock = [0.0]
    sequences = {role: [3 if role is NodeRole.BASELINE else 2] for role in NodeRole}
    barrier = ReplicationBarrier(
        _nodes(),
        _Factory(sequences),
        timeout_seconds=0.2,
        poll_interval_seconds=0.1,
        monotonic=lambda: clock[0],
        sleeper=lambda delay: clock.__setitem__(0, clock[0] + delay),
    )

    result = barrier.wait("sf_replication_2", 3)

    assert result.status is ReplicationWaitStatus.TIMEOUT
    assert result.observations[NodeRole.BASELINE].observed_sequence == 3
    assert result.observations[NodeRole.CUSTOM_ON].observed_sequence == 2


def test_barrier_passes_remaining_deadline_to_bounded_control_sessions() -> None:
    factory = _BoundedFactory({role: [4] for role in NodeRole})
    barrier = ReplicationBarrier(
        _nodes(),
        factory,
        timeout_seconds=10,
        poll_interval_seconds=0.1,
    )

    result = barrier.wait("sf_replication_bounded_1", 4)

    assert result.status is ReplicationWaitStatus.READY
    assert len(factory.deadlines) == 3
    assert factory.deadlines == sorted(factory.deadlines)


def test_bounded_barrier_waits_out_subsecond_deadline_without_starting_session() -> None:
    clock = [0.0]
    factory = _BoundedFactory({role: [0] for role in NodeRole})
    barrier = ReplicationBarrier(
        _nodes(),
        factory,
        timeout_seconds=0.2,
        poll_interval_seconds=0.1,
        monotonic=lambda: clock[0],
        sleeper=lambda delay: clock.__setitem__(0, clock[0] + delay),
    )

    result = barrier.wait("sf_replication_bounded_timeout_1", 1)

    assert result.status is ReplicationWaitStatus.TIMEOUT
    assert clock[0] == pytest.approx(0.2)
    assert factory.deadlines == []


def test_barrier_rejects_invalid_topology_intervals_and_sequence() -> None:
    with pytest.raises(ValueError, match="one replica"):
        ReplicationBarrier(_nodes()[:2], _Factory({}))
    with pytest.raises(ValueError, match="positive"):
        ReplicationBarrier(_nodes(), _Factory({}), timeout_seconds=0)
    barrier = ReplicationBarrier(
        _nodes(),
        _Factory({role: [0] for role in NodeRole}),
        timeout_seconds=0.1,
        monotonic=lambda: 1.0,
        sleeper=lambda delay: None,
    )
    with pytest.raises(ValueError, match="nonnegative"):
        barrier.wait("sf_replication_invalid_1", -1)
