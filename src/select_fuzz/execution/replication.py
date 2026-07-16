"""Replication-marker setup and bounded primary-to-replica catch-up waits."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import time
import re
from typing import Protocol

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.execution.protocols import ConnectionFactory, QuerySession
from select_fuzz.execution.setup import validate_database_name


MARKER_TABLE = "__select_fuzz_replication_marker"
MARKER_DDL_SQL = (
    f"CREATE TABLE IF NOT EXISTS `{MARKER_TABLE}` ("
    "`marker_id` TINYINT NOT NULL PRIMARY KEY, "
    "`batch_sequence` BIGINT NOT NULL) ENGINE=InnoDB"
)


def marker_upsert_sql(sequence: int) -> str:
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ValueError("replication sequence must be a nonnegative integer")
    return (
        f"INSERT INTO `{MARKER_TABLE}` (`marker_id`, `batch_sequence`) VALUES (1, {sequence}) "
        "ON DUPLICATE KEY UPDATE `batch_sequence` = VALUES(`batch_sequence`)"
    )


_MARKER_SEQUENCE = re.compile(
    rf"(?is)INSERT\s+INTO\s+`?{MARKER_TABLE}`?.*?VALUES\s*\(\s*1\s*,\s*([0-9]+)\s*\)"
)


def replication_sequence_from_sql(statements: Sequence[str]) -> int:
    sequence = 0
    for statement in statements:
        match = _MARKER_SEQUENCE.search(statement)
        if match is not None:
            sequence = max(sequence, int(match.group(1)))
    return sequence


class SetupBundleLike(Protocol):
    @property
    def statements(self) -> tuple[str, ...]: ...

    @property
    def payload_sha256(self) -> str: ...

    @property
    def requires_same_session(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReplicationSetupBundle:
    """Append the initial marker to an otherwise immutable generated setup."""

    base: SetupBundleLike

    @property
    def statements(self) -> tuple[str, ...]:
        return (*self.base.statements, MARKER_DDL_SQL, marker_upsert_sql(0))

    @property
    def payload_sha256(self) -> str:
        return self.base.payload_sha256

    @property
    def requires_same_session(self) -> bool:
        return self.base.requires_same_session

    @property
    def schema(self) -> object:
        return getattr(self.base, "schema")

    @property
    def data(self) -> object:
        return getattr(self.base, "data")

    @property
    def replication_sequence(self) -> int:
        return 0


def with_replication_marker(bundle: SetupBundleLike) -> ReplicationSetupBundle:
    if isinstance(bundle, ReplicationSetupBundle):
        return bundle
    return ReplicationSetupBundle(bundle)


class ReplicationWaitStatus(StrEnum):
    READY = "ready"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class ReplicationObservation:
    role: NodeRole
    observed_sequence: int | None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class ReplicationWaitResult:
    status: ReplicationWaitStatus
    required_sequence: int
    observations: Mapping[NodeRole, ReplicationObservation]

    @property
    def ready(self) -> bool:
        return self.status is ReplicationWaitStatus.READY


def _fetch_sequence(session: QuerySession) -> int | None:
    cursor = session.execute(f"SELECT `batch_sequence` FROM `{MARKER_TABLE}` WHERE `marker_id` = 1")
    try:
        rows = cursor.fetchmany(2)
    finally:
        cursor.close()
    if len(rows) != 1 or len(rows[0]) != 1:
        return None
    value = rows[0][0]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


class ReplicationBarrier:
    """Poll all corresponding replicas until a committed marker becomes visible."""

    def __init__(
        self,
        replicas: Sequence[NodeConfig],
        factory: ConnectionFactory,
        *,
        timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.1,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        by_role = {node.role: node for node in replicas}
        if len(replicas) != 3 or set(by_role) != set(NodeRole):
            raise ValueError("replication barrier requires one replica for every role")
        if timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("replication wait intervals must be positive")
        self._replicas = tuple(by_role[role] for role in NodeRole)
        self._factory = factory
        self._timeout_seconds = float(timeout_seconds)
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._monotonic = monotonic
        self._sleeper = sleeper

    def wait(self, database: str, sequence: int) -> ReplicationWaitResult:
        database = validate_database_name(database)
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValueError("replication sequence must be a nonnegative integer")
        deadline = self._monotonic() + self._timeout_seconds
        observations: dict[NodeRole, ReplicationObservation] = {
            role: ReplicationObservation(role, None) for role in NodeRole
        }

        def poll(node: NodeConfig, node_deadline: float) -> ReplicationObservation:
            try:
                deadline_session = getattr(self._factory, "control_session_until", None)
                bounded_session = getattr(self._factory, "control_session_with_timeout", None)
                manager = (
                    deadline_session(node, database, node_deadline)
                    if callable(deadline_session)
                    else (
                        bounded_session(
                            node,
                            database,
                            max(1.0, node_deadline - self._monotonic()),
                        )
                        if callable(bounded_session)
                        else self._factory.control_session(node, database)
                    )
                )
                with manager as session:
                    observed = _fetch_sequence(session)
                return ReplicationObservation(node.role, observed)
            except Exception as error:
                return ReplicationObservation(node.role, None, type(error).__name__)

        while True:
            pending = tuple(
                node
                for node in self._replicas
                if (observations[node.role].observed_sequence or -1) < sequence
            )
            if pending:
                for index, node in enumerate(pending):
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        return ReplicationWaitResult(
                            ReplicationWaitStatus.TIMEOUT,
                            sequence,
                            dict(observations),
                        )
                    nodes_left = len(pending) - index
                    probe_budget = remaining / nodes_left
                    deadline_session = getattr(self._factory, "control_session_until", None)
                    if callable(deadline_session) and probe_budget < 1:
                        self._sleeper(remaining)
                        return ReplicationWaitResult(
                            ReplicationWaitStatus.TIMEOUT,
                            sequence,
                            dict(observations),
                        )
                    observation = poll(
                        node,
                        min(deadline, self._monotonic() + probe_budget),
                    )
                    observations[observation.role] = observation
            if all(
                observation.observed_sequence is not None
                and observation.observed_sequence >= sequence
                for observation in observations.values()
            ):
                return ReplicationWaitResult(
                    ReplicationWaitStatus.READY,
                    sequence,
                    dict(observations),
                )
            now = self._monotonic()
            if now >= deadline:
                return ReplicationWaitResult(
                    ReplicationWaitStatus.TIMEOUT,
                    sequence,
                    dict(observations),
                )
            self._sleeper(min(self._poll_interval_seconds, deadline - now))


__all__ = [
    "MARKER_DDL_SQL",
    "MARKER_TABLE",
    "ReplicationBarrier",
    "ReplicationObservation",
    "ReplicationSetupBundle",
    "ReplicationWaitResult",
    "ReplicationWaitStatus",
    "marker_upsert_sql",
    "replication_sequence_from_sql",
    "with_replication_marker",
]
