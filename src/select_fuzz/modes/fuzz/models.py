"""Immutable runtime models for concurrent read/write fuzzing."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from select_fuzz.config import FuzzConfig


@dataclass(frozen=True, slots=True)
class FuzzConnectionLayout:
    primary_writers: int
    primary_readers: int
    replica_readers: int
    total_connections: int

    @classmethod
    def from_config(cls, config: FuzzConfig) -> FuzzConnectionLayout:
        primary_writers = config.databases * config.writer_threads_per_database
        primary_readers = config.databases * (config.reader_threads_per_database // 3)
        replica_readers = config.databases * (
            config.reader_threads_per_database * 2 // 3
        )
        total = primary_writers + primary_readers + replica_readers
        if total > config.max_total_connections:
            raise ValueError(
                f"fuzz requires {total} connections, exceeding "
                f"max_total_connections={config.max_total_connections}"
            )
        return cls(primary_writers, primary_readers, replica_readers, total)


@dataclass(frozen=True, slots=True)
class FuzzExecutionResult:
    success: bool
    rows_seen: int
    elapsed_ns: int
    affected_rows: int | None = None
    error: str | None = None
    connection_lost: bool = False
    execute_elapsed_ns: int = 0
    fetch_elapsed_ns: int = 0
    timed_out: bool = False
    stopped: bool = False
    failure_evidence: dict[str, object] | None = None
    errno: int | None = None


class FuzzRowBudget:
    """Thread-safe approximate row cap shared by writers for one database."""

    def __init__(self, *, initial_rows: int, maximum_rows: int) -> None:
        if initial_rows < 0 or maximum_rows < initial_rows:
            raise ValueError("row budget bounds are invalid")
        self._current = initial_rows
        self._maximum = maximum_rows
        self._lock = Lock()

    def reserve_insert(self, requested: int) -> int:
        if requested < 0:
            raise ValueError("requested insert rows must be nonnegative")
        with self._lock:
            available = self._maximum - self._current
            reserved = min(requested, max(0, available))
            self._current += reserved
            return reserved

    def reconcile_insert(self, reserved: int, affected_rows: int | None) -> None:
        if reserved < 0:
            raise ValueError("reserved insert rows must be nonnegative")
        actual = reserved if affected_rows is None else max(0, min(reserved, affected_rows))
        with self._lock:
            self._current -= reserved - actual

    def record_delete(self, affected_rows: int | None) -> None:
        if affected_rows is None:
            return
        with self._lock:
            self._current = max(0, self._current - max(0, affected_rows))

    @property
    def current(self) -> int:
        with self._lock:
            return self._current


__all__ = ["FuzzConnectionLayout", "FuzzExecutionResult", "FuzzRowBudget"]
