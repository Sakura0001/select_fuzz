from __future__ import annotations

from typing import Protocol

from select_fuzz.config import TargetNodeConfig
from select_fuzz.monitor.events import is_lost_connection_error


class LostConnectionError(RuntimeError):
    """数据库连接丢失。"""


class DatabaseClient(Protocol):
    def connect(self) -> None:
        ...

    def execute(self, sql: str) -> None:
        ...

    def query_scalar(self, sql: str) -> int:
        ...

    def ping(self) -> bool:
        ...

    def close(self) -> None:
        ...


class PyMySQLClient:
    def __init__(self, node: TargetNodeConfig, host: str | None = None, port: int | None = None) -> None:
        self.node = node
        self.host = host or node.host
        self.port = port or node.port
        self._connection = None
        self._connection_id = None
        self._connect_count = 0
        self._close_count = 0
        self._ping_reconnect_count = 0

    def connect(self) -> None:
        import pymysql

        self._connection = pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.node.username,
            password=self.node.password,
            autocommit=True,
            connect_timeout=10,
            read_timeout=60,
            write_timeout=60,
            charset="utf8mb4",
        )
        self._connect_count += 1
        self._refresh_connection_id()

    def execute(self, sql: str) -> None:
        if self._connection is None:
            self.connect()
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(sql)
        except Exception as exc:
            if is_lost_connection_error(exc):
                raise LostConnectionError(str(exc)) from exc
            raise

    def query_scalar(self, sql: str) -> int:
        if self._connection is None:
            self.connect()
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(sql)
                row = cursor.fetchone()
        except Exception as exc:
            if is_lost_connection_error(exc):
                raise LostConnectionError(str(exc)) from exc
            raise
        if row is None:
            return 0
        value = row[0]
        return int(value or 0)

    def ping(self) -> bool:
        try:
            if self._connection is None:
                self.connect()
                return True
            previous_id = self._connection_id
            self._connection.ping(reconnect=True)
            self._refresh_connection_id()
            if previous_id is not None and self._connection_id != previous_id:
                self._ping_reconnect_count += 1
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
            self._connection_id = None
            self._close_count += 1

    def connection_diagnostics(self) -> dict:
        connection_open = self._connection is not None and bool(getattr(self._connection, "open", True))
        return {
            "connection_open": connection_open,
            "connection_id": self._connection_id if connection_open else None,
            "connection_connect_count": self._connect_count,
            "connection_close_count": self._close_count,
            "connection_ping_reconnect_count": self._ping_reconnect_count,
        }

    def _refresh_connection_id(self) -> None:
        if self._connection is None:
            self._connection_id = None
            return
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT CONNECTION_ID()")
            row = cursor.fetchone()
        self._connection_id = int(row[0]) if row and row[0] is not None else None
