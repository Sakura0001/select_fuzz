from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Dict, List

from .events import LostConnectionEvent


_BUSY_TIMEOUT_MS = 5_000
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: Dict[str, threading.RLock] = {}
_EVENT_ROLE_COLUMNS = {
    "worker_type": "TEXT",
    "db_role": "TEXT",
    "table_name": "TEXT",
    "operation": "TEXT",
    "generator_seed": "TEXT",
    "generator_version": "TEXT",
}


def _lock_for_path(path: Path) -> threading.RLock:
    key = str(path.resolve(strict=False))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


class MetricStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = _lock_for_path(self.path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_MS / 1_000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        return conn

    def _init_schema(self) -> None:
        with self._write_lock:
            with closing(self._connect()) as conn, conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_metrics (
                      task_id TEXT PRIMARY KEY,
                      node_name TEXT NOT NULL,
                      status TEXT NOT NULL,
                      sql_total INTEGER NOT NULL,
                      lost_connection_total INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS lost_connection_events (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      timestamp TEXT NOT NULL,
                      task_id TEXT NOT NULL,
                      node_name TEXT NOT NULL,
                      jump_host TEXT,
                      target TEXT NOT NULL,
                      sql TEXT NOT NULL,
                      window_start TEXT NOT NULL,
                      worker_type TEXT,
                      db_role TEXT,
                      table_name TEXT,
                      operation TEXT,
                      generator_seed TEXT,
                      generator_version TEXT
                    )
                    """
                )
                self._migrate_lost_connection_event_columns(conn)

    @staticmethod
    def _migrate_lost_connection_event_columns(conn: sqlite3.Connection) -> None:
        existing_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(lost_connection_events)")
        }
        for column_name, column_type in _EVENT_ROLE_COLUMNS.items():
            if column_name not in existing_columns:
                conn.execute(
                    f"ALTER TABLE lost_connection_events ADD COLUMN {column_name} {column_type}"
                )

    def upsert_task_metric(
        self,
        task_id: str,
        node_name: str,
        status: str,
        sql_total: int,
        lost_connection_total: int,
    ) -> None:
        with self._write_lock:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO task_metrics(task_id, node_name, status, sql_total, lost_connection_total)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                      node_name = excluded.node_name,
                      status = excluded.status,
                      sql_total = excluded.sql_total,
                      lost_connection_total = excluded.lost_connection_total
                    """,
                    (task_id, node_name, status, sql_total, lost_connection_total),
                )

    def insert_lost_connection_event(self, event: LostConnectionEvent) -> None:
        with self._write_lock:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO lost_connection_events(
                      timestamp, task_id, node_name, jump_host, target, sql, window_start,
                      worker_type, db_role, table_name, operation, generator_seed, generator_version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.timestamp.isoformat(),
                        event.task_id,
                        event.node_name,
                        event.jump_host,
                        event.target,
                        event.sql,
                        event.window_start.isoformat(),
                        event.worker_type,
                        event.db_role,
                        event.table_name,
                        event.operation,
                        event.generator_seed,
                        event.generator_version,
                    ),
                )

    def summary(self) -> Dict[str, int]:
        with closing(self._connect()) as conn:
            task_count = conn.execute("SELECT COUNT(*) FROM task_metrics").fetchone()[0]
            lost_count = conn.execute("SELECT COUNT(*) FROM lost_connection_events").fetchone()[0]
        return {"任务数": task_count, "lost connection": lost_count}

    def list_lost_connection_events(self, task_id: str) -> List[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT timestamp, task_id, node_name, jump_host, target, sql, window_start,
                       worker_type, db_role, table_name, operation, generator_seed, generator_version
                FROM lost_connection_events
                WHERE task_id = ?
                ORDER BY timestamp DESC, id DESC
                """,
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]
