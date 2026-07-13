"""Deterministic soak faults and append-only resource telemetry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from collections.abc import Callable
from enum import StrEnum
import fcntl
import json
import os
from pathlib import Path
import random
from statistics import median

from select_fuzz.validation.models import TelemetrySample


class FaultKind(StrEnum):
    CONNECTION_RESET = "connection_reset"
    WORKER_TERMINATION = "worker_termination"
    REPORT_WRITE_FAILURE = "report_write_failure"
    QUERY_TIMEOUT = "query_timeout"


@dataclass(frozen=True, slots=True)
class FaultEvent:
    at_s: float
    kind: FaultKind
    recovery_deadline_s: float


def fault_event_id(event: FaultEvent, *, namespace: str = "") -> str:
    prefix = f"{namespace}:" if namespace else ""
    return f"{prefix}{event.kind.value}:{event.at_s:.6f}"


@dataclass(frozen=True, slots=True)
class TrendVerdict:
    passed: bool
    reasons: tuple[str, ...]
    growth_ratios: tuple[tuple[str, float], ...]


def build_fault_schedule(
    *, seed: int, duration_s: float, events_per_hour: int = 4, recovery_s: float = 30.0
) -> tuple[FaultEvent, ...]:
    if seed < 0 or duration_s <= 0 or events_per_hour <= 0 or recovery_s <= 0:
        raise ValueError("schedule inputs must be positive (seed may be zero)")
    count = max(1, round(duration_s / 3600 * events_per_hour))
    rng = random.Random(seed)
    kinds = tuple(FaultKind)
    schedule: list[FaultEvent] = []
    slot_width = duration_s / (count + 1)
    for index in range(count):
        center = slot_width * (index + 1)
        jitter = rng.uniform(-0.2, 0.2) * slot_width
        at_s = min(duration_s - 1e-6, max(1e-6, center + jitter))
        schedule.append(
            FaultEvent(
                at_s=round(at_s, 6),
                kind=kinds[index % len(kinds)],
                recovery_deadline_s=recovery_s,
            )
        )
    return tuple(sorted(schedule, key=lambda event: event.at_s))


class ResourceTrendPolicy:
    def __init__(self, *, max_growth_ratio: float = 0.20) -> None:
        if max_growth_ratio < 0:
            raise ValueError("max_growth_ratio must be nonnegative")
        self.max_growth_ratio = max_growth_ratio

    def evaluate(self, samples: tuple[TelemetrySample, ...]) -> TrendVerdict:
        if len(samples) < 2:
            return TrendVerdict(False, ("insufficient_samples",), ())
        window = max(1, len(samples) // 5)
        metrics = ("rss_bytes", "threads", "open_fds", "mysql_connections")
        growth: list[tuple[str, float]] = []
        failed: list[str] = []
        for metric in metrics:
            baseline = float(median(getattr(sample, metric) for sample in samples[:window]))
            final = float(median(getattr(sample, metric) for sample in samples[-window:]))
            ratio = 0.0 if baseline == 0 and final == 0 else (
                float("inf") if baseline == 0 else (final - baseline) / baseline
            )
            growth.append((metric, ratio))
            if ratio > self.max_growth_ratio:
                failed.append(metric)
        return TrendVerdict(not failed, tuple(failed), tuple(growth))


class TelemetryRecorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, sample: TelemetrySample) -> None:
        payload = json.dumps(asdict(sample), sort_keys=True, separators=(",", ":")).encode()
        with self.path.open("ab", buffering=0) as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.write(payload + b"\n")
                os.fsync(stream.fileno())
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def read(self) -> tuple[TelemetrySample, ...]:
        if not self.path.exists():
            return ()
        samples: list[TelemetrySample] = []
        for raw_line in self.path.read_text().splitlines():
            try:
                value = json.loads(raw_line)
                samples.append(TelemetrySample(**value))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return tuple(samples)


class ScheduledFaultController:
    def __init__(
        self,
        schedule: tuple[FaultEvent, ...],
        *,
        inject: Callable[[FaultEvent], None],
        resume_elapsed_s: float = 0.0,
        completed_event_ids: frozenset[str] = frozenset(),
        event_id_factory: Callable[[FaultEvent], str] = fault_event_id,
    ) -> None:
        self.schedule = schedule
        self.inject = inject
        self.completed_event_ids = completed_event_ids
        self.event_id_factory = event_id_factory
        self._next = next(
            (
                index
                for index, event in enumerate(schedule)
                if event.at_s > resume_elapsed_s
            ),
            len(schedule),
        )

    def tick(self, elapsed_s: float) -> None:
        while self._next < len(self.schedule) and self.schedule[self._next].at_s <= elapsed_s:
            event = self.schedule[self._next]
            if self.event_id_factory(event) not in self.completed_event_ids:
                self.inject(event)
            self._next += 1


__all__ = [
    "FaultEvent",
    "FaultKind",
    "fault_event_id",
    "ResourceTrendPolicy",
    "ScheduledFaultController",
    "TelemetryRecorder",
    "TrendVerdict",
    "build_fault_schedule",
]
