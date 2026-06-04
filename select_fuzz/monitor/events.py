from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional


@dataclass(frozen=True)
class LostConnectionEvent:
    timestamp: datetime
    task_id: str
    node_name: str
    jump_host: Optional[str]
    target: str
    sql: str
    window_start: datetime

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "task_id": self.task_id,
            "node_name": self.node_name,
            "jump_host": self.jump_host,
            "target": self.target,
            "sql": self.sql,
            "window_start": self.window_start.isoformat(),
        }


class LostConnectionDeduplicator:
    def __init__(self, window: timedelta) -> None:
        self.window = window
        self._last_recorded: Dict[str, datetime] = {}

    def should_record(self, node_name: str, timestamp: datetime) -> bool:
        previous = self._last_recorded.get(node_name)
        if previous is not None and timestamp - previous < self.window:
            return False
        self._last_recorded[node_name] = timestamp
        return True


def is_lost_connection_error(error: BaseException) -> bool:
    message = str(error).lower()
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
