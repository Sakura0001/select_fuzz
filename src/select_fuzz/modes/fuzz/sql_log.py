"""Append-only SQL files for inspectable fuzz setup and worker traffic."""

from __future__ import annotations

from pathlib import Path
import re
from threading import Lock


_SAFE_COMPONENT = re.compile(r"^[a-zA-Z0-9_-]+$")


def _safe_component(value: str) -> str:
    if not value or _SAFE_COMPONENT.fullmatch(value) is None:
        raise ValueError(f"unsafe SQL artifact path component: {value!r}")
    return value


class FuzzSqlRecorder:
    """Write attempted fuzz SQL into sourceable, per-database files."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def _append(self, path: Path, sql: str) -> None:
        statement = sql.strip()
        if not statement:
            return
        if not statement.endswith(";"):
            statement += ";"
        with self._lock:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(statement)
                stream.write("\n")
                stream.flush()

    def record_schema(self, database: str, sql: str) -> None:
        database = _safe_component(database)
        self._append(self.root / f"fuzz_schema_{database}.sql", sql)

    def record_query(
        self,
        database: str,
        stream: str,
        worker_id: int,
        sql: str,
    ) -> None:
        if worker_id < 0:
            raise ValueError("worker_id must be nonnegative")
        database = _safe_component(database)
        stream = _safe_component(stream)
        self._append(
            self.root / f"fuzz_{database}_{stream}_{worker_id:03d}.sql",
            sql,
        )


__all__ = ["FuzzSqlRecorder"]
