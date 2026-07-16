"""Explicit, prefix-locked cleanup for retained Select Fuzz databases."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import os
import re
from typing import Protocol

import mysql.connector

from select_fuzz.config import AppConfig, NodeConfig, NodeRole, resolve_credentials


_MANAGED_DATABASE = re.compile(
    r"^sf_[cp]_[0-9]{8}t[0-9]{6}_w[0-9]+_r[0-9]+_s[0-9a-f]{10}"
    r"(?:_n[0-9a-f]{8}_q[0-9a-f]+|[a-z0-9_]*_retry[1-9][0-9]*_[0-9a-f]{8})$"
)


class ManagedDatabaseError(ValueError):
    """A requested name cannot be proven to come from DatabaseNameFactory."""


class CursorLike(Protocol):
    def execute(self, sql: str) -> object: ...

    def close(self) -> object: ...


class ConnectionLike(Protocol):
    def cursor(self) -> CursorLike: ...

    def close(self) -> object: ...


@dataclass(frozen=True, slots=True)
class CleanupNodeResult:
    database: str
    role: NodeRole
    dropped: bool
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class CleanupReport:
    databases: tuple[str, ...]
    execute: bool
    nodes: tuple[CleanupNodeResult, ...]

    @property
    def success(self) -> bool:
        return all(item.error_type is None for item in self.nodes)


class CleanupService:
    def __init__(
        self,
        nodes: Sequence[NodeConfig],
        connect: Callable[[NodeConfig], ConnectionLike],
    ) -> None:
        if len(nodes) != len(NodeRole) or {node.role for node in nodes} != set(NodeRole):
            raise ValueError("cleanup requires one node for every fixed role")
        self._nodes = tuple(sorted(nodes, key=lambda node: node.role.value))
        self._connect = connect

    def run(self, databases: Sequence[str], *, execute: bool = False) -> CleanupReport:
        selected = tuple(dict.fromkeys(databases))
        if not selected:
            raise ManagedDatabaseError("at least one explicit managed database is required")
        for database in selected:
            if not isinstance(database, str) or _MANAGED_DATABASE.fullmatch(database) is None:
                raise ManagedDatabaseError("database is not a Select Fuzz managed ID")
        if not execute:
            return CleanupReport(
                selected,
                False,
                tuple(
                    CleanupNodeResult(database, node.role, False)
                    for database in selected
                    for node in self._nodes
                ),
            )

        results: list[CleanupNodeResult] = []
        for database in selected:
            for node in self._nodes:
                connection: ConnectionLike | None = None
                cursor: CursorLike | None = None
                try:
                    connection = self._connect(node)
                    cursor = connection.cursor()
                    cursor.execute(f"DROP DATABASE `{database}`")
                except Exception as error:
                    results.append(
                        CleanupNodeResult(
                            database,
                            node.role,
                            False,
                            type(error).__name__,
                        )
                    )
                else:
                    results.append(CleanupNodeResult(database, node.role, True))
                finally:
                    if cursor is not None:
                        try:
                            cursor.close()
                        except Exception:
                            pass
                    if connection is not None:
                        try:
                            connection.close()
                        except Exception:
                            pass
        return CleanupReport(selected, True, tuple(results))


def build_cleanup_service(
    config: AppConfig,
    *,
    environ: dict[str, str] | None = None,
    connect: Callable[..., ConnectionLike] = mysql.connector.connect,
) -> CleanupService:
    source = dict(os.environ) if environ is None else environ

    def connect_node(node: NodeConfig) -> ConnectionLike:
        credentials = resolve_credentials(node, source)
        return connect(
            host=node.host,
            port=node.port,
            user=credentials.username.get_secret_value(),
            password=credentials.password.get_secret_value(),
            autocommit=True,
            connection_timeout=5,
            read_timeout=5,
            write_timeout=5,
        )

    return CleanupService(config.primary_nodes, connect_node)


__all__ = [
    "CleanupNodeResult",
    "CleanupReport",
    "CleanupService",
    "ManagedDatabaseError",
    "build_cleanup_service",
]
