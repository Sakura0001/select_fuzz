from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: Dict[str, threading.RLock] = {}


def _lock_for_path(path: Path) -> threading.RLock:
    """为同一日志路径复用进程内锁。

    使用解析后的绝对路径做键，避免相对路径与绝对路径为同一文件
    创建两把锁。
    """

    key = str(path.resolve(strict=False))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


@dataclass(frozen=True)
class SqlLogRecord:
    timestamp: datetime
    task_id: str
    node_name: str
    status: str
    sql: str
    error_message: Optional[str] = None
    sql_validity: Optional[str] = None
    risk_tags: Optional[List[str]] = None
    expected_error: Optional[bool] = None
    expand_base_table_columns: bool = False
    base_table_seed: Optional[str] = None
    base_table_generator_version: Optional[str] = None
    worker_type: Optional[str] = None
    db_role: Optional[str] = None
    target: Optional[str] = None
    table_name: Optional[str] = None
    operation: Optional[str] = None
    generator_seed: Optional[str] = None
    generator_version: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        row = {
            "timestamp": self.timestamp.isoformat(),
            "task_id": self.task_id,
            "node_name": self.node_name,
            "status": self.status,
            "sql": self.sql,
            "expand_base_table_columns": self.expand_base_table_columns,
            "base_table_seed": self.base_table_seed,
            "base_table_generator_version": self.base_table_generator_version,
        }
        if self.error_message is not None:
            row["error_message"] = self.error_message
        if self.sql_validity is not None:
            row["sql_validity"] = self.sql_validity
        if self.risk_tags is not None:
            row["risk_tags"] = self.risk_tags
        if self.expected_error is not None:
            row["expected_error"] = self.expected_error
        optional_fields = {
            "worker_type": self.worker_type,
            "db_role": self.db_role,
            "target": self.target,
            "table_name": self.table_name,
            "operation": self.operation,
            "generator_seed": self.generator_seed,
            "generator_version": self.generator_version,
        }
        row.update({key: value for key, value in optional_fields.items() if value is not None})
        return row


def append_jsonl(path: Path | str, row: Dict[str, Any]) -> None:
    target = Path(path)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    with _lock_for_path(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as file_obj:
            file_obj.write(line)


def append_text_line(path: Path | str, text: str) -> None:
    """将一个完整文本块以单次追加方式写入同一路径。"""

    target = Path(path)
    line = text + "\n"
    with _lock_for_path(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as file_obj:
            file_obj.write(line)


def read_jsonl(path: Path | str) -> List[Dict[str, Any]]:
    target = Path(path)
    with _lock_for_path(target):
        if not target.exists():
            return []
        rows: List[Dict[str, Any]] = []
        with target.open("r", encoding="utf-8") as file_obj:
            for line in file_obj:
                stripped = line.strip()
                if stripped:
                    rows.append(json.loads(stripped))
        return rows
