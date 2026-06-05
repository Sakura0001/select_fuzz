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
            self._connection.ping(reconnect=True)
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
