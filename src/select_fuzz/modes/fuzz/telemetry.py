"""Bounded in-memory stage and duration telemetry for fuzz workers."""

from __future__ import annotations

from collections.abc import Callable
from collections import Counter
from dataclasses import dataclass
from threading import Lock
import time


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

    def __init__(self, *, clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._clock_ns = clock_ns
        self._stages: dict[str, tuple[str, int]] = {}
        self._durations: dict[str, _Duration] = {}
        self._lock = Lock()

    def set_stage(self, worker: str, stage: str) -> None:
        if not worker or not stage:
            raise ValueError("worker and stage must be nonempty")
        with self._lock:
            current = self._stages.get(worker)
            if current is None or current[0] != stage:
                self._stages[worker] = (stage, self._clock_ns())

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
        now_ns = self._clock_ns()
        with self._lock:
            current_stages = dict(self._stages)
            durations = {
                name: value.snapshot()
                for name, value in sorted(self._durations.items())
            }
        stages = dict(
            sorted(Counter(stage for stage, _entered_ns in current_stages.values()).items())
        )
        workers_by_stage: dict[str, list[tuple[str, int]]] = {}
        worker_groups: dict[str, Counter[str]] = {}
        for worker, (stage, entered_ns) in current_stages.items():
            age_ns = max(0, now_ns - entered_ns)
            workers_by_stage.setdefault(stage, []).append((worker, age_ns))
            parts = worker.split(":", 2)
            group = parts[1].replace("-", "_") if len(parts) == 3 else "unknown"
            worker_groups.setdefault(group, Counter())[stage] += 1
        stage_details = {
            stage: {
                "count": len(workers),
                "max_age_ns": max((age_ns for _worker, age_ns in workers), default=0),
                "oldest_workers": tuple(
                    {"worker": worker, "age_ns": age_ns}
                    for worker, age_ns in sorted(
                        workers,
                        key=lambda item: (-item[1], item[0]),
                    )[:3]
                ),
            }
            for stage, workers in sorted(workers_by_stage.items())
        }
        grouped = {
            group: dict(sorted(counts.items()))
            for group, counts in sorted(worker_groups.items())
        }
        return {
            "stages": stages,
            "durations": durations,
            "stage_details": stage_details,
            "worker_groups": grouped,
        }


__all__ = ["FuzzStageTelemetry"]
