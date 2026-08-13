from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class LostConnectionEvent:
    timestamp: datetime
    task_id: str
    node_name: str
    jump_host: Optional[str]
    target: str
    sql: str
    window_start: datetime
    worker_type: Optional[str] = None
    db_role: Optional[str] = None
    table_name: Optional[str] = None
    operation: Optional[str] = None
    generator_seed: Optional[str] = None
    generator_version: Optional[str] = None

    def to_dict(self) -> dict:
        row = {
            "timestamp": self.timestamp.isoformat(),
            "task_id": self.task_id,
            "node_name": self.node_name,
            "jump_host": self.jump_host,
            "target": self.target,
            "sql": self.sql,
            "window_start": self.window_start.isoformat(),
        }
        optional_fields = {
            "worker_type": self.worker_type,
            "db_role": self.db_role,
            "table_name": self.table_name,
            "operation": self.operation,
            "generator_seed": self.generator_seed,
            "generator_version": self.generator_version,
        }
        row.update({key: value for key, value in optional_fields.items() if value is not None})
        return row


class LostConnectionDeduplicator:
    def __init__(self, window: timedelta) -> None:
        self.window = window
        self._last_recorded: Dict[Tuple[str, Optional[str], Optional[str]], datetime] = {}
        self._lock = threading.Lock()

    def should_record(
        self,
        node_name: str,
        timestamp: datetime,
        db_role: Optional[str] = None,
        target: Optional[str] = None,
    ) -> bool:
        key = (node_name, db_role, target)
        with self._lock:
            previous = self._last_recorded.get(key)
            if previous is not None and timestamp - previous < self.window:
                return False
            self._last_recorded[key] = timestamp
            return True


def is_lost_connection_error(error: BaseException) -> bool:
    message = str(error).lower()
    error_name = error.__class__.__name__
    if error_name in {"InterfaceError", "OperationalError"} and getattr(error, "args", None) == (0, ""):
        return True
    signals = [
        "lost connection to mysql server",
        "mysql server has gone away",
        "server has gone away",
        "connection reset",
        "connection refused",
        "broken pipe",
        "socket closed",
        "eof",
    ]
    return isinstance(error, EOFError) or any(signal in message for signal in signals)
