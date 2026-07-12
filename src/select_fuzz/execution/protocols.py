"""Small protocols isolating the core from one concrete MySQL connector."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from select_fuzz.config import NodeConfig
from select_fuzz.domain import ColumnMeta


class CursorLike(Protocol):
    """An unbuffered result cursor with already-decoded typed values."""

    @property
    def columns(self) -> tuple[ColumnMeta, ...]: ...

    def fetchmany(self, size: int) -> tuple[tuple[object, ...], ...]: ...

    def warnings(self) -> tuple[str, ...]: ...

    def close(self) -> None: ...


class QuerySession(Protocol):
    """One MySQL connection; temporary tables live for this object's lifetime."""

    def connection_id(self) -> int: ...

    def execute(self, sql: str) -> CursorLike: ...

    def abort(self) -> None: ...

    def close(self) -> None: ...


class ConnectionFactory(Protocol):
    def query_session(
        self, node: NodeConfig, database: str
    ) -> AbstractContextManager[QuerySession]: ...

    def control_session(
        self, node: NodeConfig, database: str
    ) -> AbstractContextManager[QuerySession]: ...


class ControlConnectionFactory(Protocol):
    def control_session(
        self, node: NodeConfig, database: str
    ) -> AbstractContextManager[QuerySession]: ...


class BarrierLike(Protocol):
    def wait(self, timeout: float | None = None) -> object: ...


__all__ = [
    "BarrierLike",
    "ConnectionFactory",
    "ControlConnectionFactory",
    "CursorLike",
    "QuerySession",
]
