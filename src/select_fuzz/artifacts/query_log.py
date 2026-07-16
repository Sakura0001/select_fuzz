"""Durable, append-only SQL audit logs partitioned by fuzz worker."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path
from threading import Lock

from select_fuzz.artifacts.jsonl import JsonlWriter


class WorkerQueryLogWriter:
    """Route query-attempt records to one fsynced JSONL file per worker."""

    def __init__(
        self,
        directory: str | Path,
        *,
        fsync: Callable[[int], None] = os.fsync,
    ) -> None:
        self.directory = Path(directory)
        self._fsync = fsync
        self._writers: dict[int, JsonlWriter] = {}
        self._writers_lock = Lock()

    def path_for(self, worker_id: int) -> Path:
        self._validate_worker_id(worker_id)
        return self.directory / f"worker-{worker_id:03d}.jsonl"

    def append(self, worker_id: int, record: Mapping[str, object]) -> None:
        self._validate_worker_id(worker_id)
        if not isinstance(record, Mapping):
            raise TypeError("query log record must be a mapping")
        with self._writers_lock:
            writer = self._writers.get(worker_id)
            if writer is None:
                writer = JsonlWriter(self.path_for(worker_id), fsync=self._fsync)
                self._writers[worker_id] = writer
        writer.append(record)

    @staticmethod
    def _validate_worker_id(worker_id: int) -> None:
        if (
            not isinstance(worker_id, int)
            or isinstance(worker_id, bool)
            or worker_id < 0
        ):
            raise ValueError("worker_id must be a nonnegative integer")


__all__ = ["WorkerQueryLogWriter"]
