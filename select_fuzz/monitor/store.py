from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List

from .events import LostConnectionEvent


class MetricStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
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
                  window_start TEXT NOT NULL
                )
                """
            )

    def upsert_task_metric(
        self,
        task_id: str,
        node_name: str,
        status: str,
        sql_total: int,
        lost_connection_total: int,
    ) -> None:
        with self._connect() as conn:
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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO lost_connection_events(timestamp, task_id, node_name, jump_host, target, sql, window_start)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.timestamp.isoformat(),
                    event.task_id,
                    event.node_name,
                    event.jump_host,
                    event.target,
                    event.sql,
                    event.window_start.isoformat(),
                ),
            )

    def summary(self) -> Dict[str, int]:
        with self._connect() as conn:
            task_count = conn.execute("SELECT COUNT(*) FROM task_metrics").fetchone()[0]
            lost_count = conn.execute("SELECT COUNT(*) FROM lost_connection_events").fetchone()[0]
        return {"任务数": task_count, "lost connection": lost_count}

    def list_lost_connection_events(self, task_id: str) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, task_id, node_name, jump_host, target, sql, window_start
                FROM lost_connection_events
                WHERE task_id = ?
                ORDER BY timestamp DESC
                """,
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]
