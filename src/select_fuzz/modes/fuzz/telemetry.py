"""Bounded in-memory stage and duration telemetry for fuzz workers."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from threading import Lock


@dataclass(slots=True)
class _Duration:
    count: int = 0
    total_ns: int = 0
    max_ns: int = 0

    def observe(self, elapsed_ns: int) -> None:
        self.count += 1
        self.total_ns += elapsed_ns
        self.max_ns = max(self.max_ns, elapsed_ns)

    def snapshot(self) -> dict[str, int]:
        return {
            "count": self.count,
            "total_ns": self.total_ns,
            "max_ns": self.max_ns,
        }


class FuzzStageTelemetry:
    """Thread-safe fixed-size aggregates; no per-operation values are retained."""

    def __init__(self) -> None:
        self._stages: dict[str, str] = {}
        self._durations: dict[str, _Duration] = {}
        self._lock = Lock()

    def set_stage(self, worker: str, stage: str) -> None:
        if not worker or not stage:
            raise ValueError("worker and stage must be nonempty")
        with self._lock:
            self._stages[worker] = stage

    def remove_worker(self, worker: str) -> None:
        with self._lock:
            self._stages.pop(worker, None)

    def observe(self, metric: str, elapsed_ns: int) -> None:
        if not metric:
            raise ValueError("metric must be nonempty")
        if (
            not isinstance(elapsed_ns, int)
            or isinstance(elapsed_ns, bool)
            or elapsed_ns < 0
        ):
            raise ValueError("elapsed_ns must be a nonnegative integer")
        with self._lock:
            self._durations.setdefault(metric, _Duration()).observe(elapsed_ns)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            stages = dict(sorted(Counter(self._stages.values()).items()))
            durations = {
                name: value.snapshot()
                for name, value in sorted(self._durations.items())
            }
        return {"stages": stages, "durations": durations}


__all__ = ["FuzzStageTelemetry"]
