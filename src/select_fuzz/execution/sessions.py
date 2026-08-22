"""Owned query sessions and independent two-node acquisition."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock
from time import perf_counter_ns
from types import MappingProxyType

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.execution.evidence import capture_exception_evidence
from select_fuzz.execution.protocols import OwnedConnectionFactory, QuerySession


class ActiveSessionRegistry:
    """Track live sessions so fatal shutdown can actively unblock their I/O."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._sessions: dict[int, QuerySession] = {}

    def register(self, session: QuerySession) -> None:
        with self._lock:
            self._sessions[id(session)] = session

    def unregister(self, session: QuerySession) -> None:
        with self._lock:
            self._sessions.pop(id(session), None)

    def abort_all(self) -> int:
        """Abort a stable snapshot and return how many sessions were targeted."""

        with self._lock:
            sessions = tuple(self._sessions.values())
        for session in sessions:
            try:
                session.abort()
            except Exception:
                # Shutdown is best effort; one broken connector must not prevent
                # other blocked sessions from being interrupted.
                continue
        return len(sessions)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)


@dataclass(slots=True)
class SessionLease:
    """An explicitly owned session with idempotent release semantics."""

    role: NodeRole
    session: QuerySession
    connection_id: int
    timings_ns: Mapping[str, int]
    close_callback: Callable[[], None]
    _closed: bool = field(default=False, init=False, repr=False)
    _close_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.timings_ns = MappingProxyType(dict(self.timings_ns))

    @property
    def closed(self) -> bool:
        with self._close_lock:
            return self._closed

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self.close_callback()


@dataclass(frozen=True, slots=True)
class SessionOpenAttempt:
    role: NodeRole
    lease: SessionLease | None
    failure_evidence: Mapping[str, object] | None
    elapsed_ns: int

    @property
    def opened(self) -> bool:
        return self.lease is not None


@dataclass(slots=True)
class PairSessionAcquisition:
    attempts: Mapping[NodeRole, SessionOpenAttempt]

    def __post_init__(self) -> None:
        self.attempts = MappingProxyType(dict(self.attempts))

    @property
    def ready(self) -> bool:
        return len(self.attempts) == 2 and all(
            attempt.lease is not None for attempt in self.attempts.values()
        )

    @property
    def leases(self) -> Mapping[NodeRole, SessionLease]:
        if not self.ready:
            raise RuntimeError("成对查询连接未全部就绪")
        return MappingProxyType(
            {
                role: attempt.lease
                for role, attempt in self.attempts.items()
                if attempt.lease is not None
            }
        )

    def close(self) -> None:
        for attempt in self.attempts.values():
            if attempt.lease is not None:
                attempt.lease.close()


def _open_one(
    factory: OwnedConnectionFactory,
    node: NodeConfig,
    database: str,
) -> SessionOpenAttempt:
    started_ns = perf_counter_ns()
    try:
        lease = factory.open_query_session(node, database)
    except Exception as exc:
        return SessionOpenAttempt(
            role=node.role,
            lease=None,
            failure_evidence=capture_exception_evidence(exc, "query_session_open"),
            elapsed_ns=perf_counter_ns() - started_ns,
        )
    return SessionOpenAttempt(
        role=node.role,
        lease=lease,
        failure_evidence=None,
        elapsed_ns=perf_counter_ns() - started_ns,
    )


def acquire_session_pair(
    nodes: Sequence[NodeConfig],
    database: str,
    factory: OwnedConnectionFactory,
) -> PairSessionAcquisition:
    """Attempt both nodes concurrently and preserve each node's own outcome."""

    if len(nodes) != 2:
        raise ValueError("成对建连必须且只能提供两个节点")
    if nodes[0].role == nodes[1].role:
        raise ValueError("成对建连的两个节点角色必须不同")

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sf-pair-connect") as pool:
        futures = {
            node.role: pool.submit(_open_one, factory, node, database) for node in nodes
        }
        attempts = {role: future.result() for role, future in futures.items()}

    acquisition = PairSessionAcquisition(attempts=attempts)
    if not acquisition.ready:
        acquisition.close()
    return acquisition


__all__ = [
    "ActiveSessionRegistry",
    "PairSessionAcquisition",
    "SessionLease",
    "SessionOpenAttempt",
    "acquire_session_pair",
]
