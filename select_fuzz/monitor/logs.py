from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


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
        return row


def append_jsonl(path: Path | str, row: Dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as file_obj:
        file_obj.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        file_obj.write("\n")


def read_jsonl(path: Path | str) -> List[Dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows
