"""Shared run lifecycle, event sequencing, and bounded worker scheduling."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import math
from threading import Event, Lock, Thread
import time
from typing import Protocol

from select_fuzz.domain import RunEvent, RunRequest, SeedTree
from select_fuzz.execution.evidence import capture_exception_evidence


class EventSink(Protocol):
    def publish(self, event: RunEvent) -> None: ...


class EventPublisher:
    """Assign one monotonic sequence across all worker threads."""

    def __init__(self, run_id: str, sink: EventSink) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must not be empty")
        self.run_id = run_id
        self._sink = sink
        self._sequence = 0
        self._lock = Lock()

    def publish(self, kind: str, payload: Mapping[str, object]) -> RunEvent:
        if not isinstance(kind, str) or not kind:
            raise ValueError("event kind must not be empty")
        with self._lock:
            event = RunEvent(
                run_id=self.run_id,
                sequence=self._sequence,
                kind=kind,
                payload=payload,
            )
            self._sink.publish(event)
            self._sequence += 1
            return event


@dataclass(frozen=True, slots=True)
class RoundContext:
    request: RunRequest
    worker_id: int
    round_number: int
    round_seed: int

    def __post_init__(self) -> None:
        for field_name in ("worker_id", "round_number", "round_seed"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class RoundSummary:
    round_number: int
    queries_completed: int
    findings: int
    rejected: int
    over_budget: int

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    rounds_completed: int
    queries_completed: int
    findings: int
    rejected: int
    over_budget: int
    stopped: bool

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id must not be empty")
        for field_name in (
            "rounds_completed",
            "queries_completed",
            "findings",
            "rejected",
            "over_budget",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} must be a nonnegative integer")
        if not isinstance(self.stopped, bool):
            raise TypeError("stopped must be a bool")


class RoundRunner(Protocol):
    def run_round(
        self,
        context: RoundContext,
        events: EventPublisher,
        stop_event: Event,
    ) -> RoundSummary: ...


class CorrectnessRunService:
    def __init__(
        self,
        rounds: RoundRunner,
        events: EventSink,
        *,
        diagnostics_interval_seconds: float = 5.0,
    ) -> None:
        if (
            not isinstance(diagnostics_interval_seconds, (int, float))
            or isinstance(diagnostics_interval_seconds, bool)
            or not math.isfinite(diagnostics_interval_seconds)
            or diagnostics_interval_seconds <= 0
        ):
            raise ValueError("diagnostics_interval_seconds must be finite and positive")
        self._rounds = rounds
        self._events = events
        self._diagnostics_interval_seconds = float(diagnostics_interval_seconds)

    def run(self, request: RunRequest, stop_event: Event) -> RunSummary:
        if request.mode != "correctness":
            raise ValueError("CorrectnessRunService requires correctness mode")
        publisher = EventPublisher(request.run_id, self._events)
        publisher.publish(
            "run_started",
            {
                "mode": request.mode,
                "queries_per_round": request.queries_per_round,
                "rounds": request.rounds,
                "seed": request.seed,
                "workers": request.workers,
            },
        )
        totals = {
            "rounds_completed": 0,
            "queries_completed": 0,
            "findings": 0,
            "rejected": 0,
            "over_budget": 0,
        }
        next_round = 0
        finite_rounds = request.rounds
        active_lock = Lock()
        active_rounds: dict[int, tuple[int, int]] = {}
        diagnostics_stop = Event()

        def publish_diagnostics() -> None:
            while not diagnostics_stop.wait(self._diagnostics_interval_seconds):
                now_ns = time.monotonic_ns()
                with active_lock:
                    workers = {
                        str(worker_id): {
                            "round_number": round_number,
                            "stage": "round_running",
                            "stage_age_seconds": max(
                                0.0,
                                (now_ns - started_ns) / 1_000_000_000,
                            ),
                        }
                        for worker_id, (round_number, started_ns) in active_rounds.items()
                    }
                runtime_snapshot: Mapping[str, object] = {}
                snapshot = getattr(self._rounds, "runtime_diagnostics", None)
                if callable(snapshot):
                    try:
                        raw_snapshot = snapshot()
                        if isinstance(raw_snapshot, Mapping):
                            runtime_snapshot = raw_snapshot
                    except Exception as error:
                        runtime_snapshot = {
                            "snapshot_failure": capture_exception_evidence(
                                error,
                                "correctness_runtime_diagnostics",
                            )
                        }
                publisher.publish(
                    "runtime_diagnostics",
                    {
                        "active_worker_count": len(workers),
                        "runtime": dict(runtime_snapshot),
                        "workers": workers,
                    },
                )

        diagnostics_thread = Thread(
            target=publish_diagnostics,
            name="sf-correctness-diagnostics",
            daemon=True,
        )
        diagnostics_thread.start()

        def can_submit() -> bool:
            return not stop_event.is_set() and (
                finite_rounds is None or next_round < finite_rounds
            )

        def execute(worker_id: int, round_number: int) -> RoundSummary:
            round_seed = SeedTree(request.seed).derive("round", round_number)
            with active_lock:
                active_rounds[worker_id] = (round_number, time.monotonic_ns())
            publisher.publish(
                "round_started",
                {
                    "round_number": round_number,
                    "round_seed": round_seed,
                    "worker_id": worker_id,
                },
            )
            try:
                result = self._rounds.run_round(
                    RoundContext(request, worker_id, round_number, round_seed),
                    publisher,
                    stop_event,
                )
            finally:
                with active_lock:
                    active_rounds.pop(worker_id, None)
            publisher.publish(
                "round_finished",
                {
                    "findings": result.findings,
                    "queries_completed": result.queries_completed,
                    "round_number": round_number,
                    "worker_id": worker_id,
                },
            )
            return result

        futures: dict[Future[RoundSummary], int] = {}
        with ThreadPoolExecutor(
            max_workers=request.workers, thread_name_prefix="sf-correctness"
        ) as pool:
            for worker_id in range(request.workers):
                if not can_submit():
                    break
                futures[pool.submit(execute, worker_id, next_round)] = worker_id
                next_round += 1
            while futures:
                completed, _pending = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    worker_id = futures.pop(future)
                    try:
                        result = future.result()
                    except Exception as error:
                        stop_event.set()
                        diagnostics_stop.set()
                        abort_active = getattr(self._rounds, "abort_active", None)
                        aborted_sessions = 0
                        abort_failure: Mapping[str, object] | None = None
                        if callable(abort_active):
                            try:
                                raw_aborted = abort_active()
                                if isinstance(raw_aborted, int) and not isinstance(
                                    raw_aborted, bool
                                ):
                                    aborted_sessions = max(0, raw_aborted)
                            except Exception as abort_error:
                                abort_failure = capture_exception_evidence(
                                    abort_error,
                                    "correctness_abort_active",
                                )
                        publisher.publish(
                            "run_failed",
                            {
                                "aborted_sessions": aborted_sessions,
                                "abort_failure": abort_failure,
                                "error_type": type(error).__name__,
                                "evidence": capture_exception_evidence(
                                    error,
                                    "correctness_worker",
                                ),
                                "exception_type": type(error).__name__,
                                "message": str(error),
                                "worker_id": worker_id,
                            },
                        )
                        for pending in futures:
                            pending.cancel()
                        raise
                    totals["rounds_completed"] += 1
                    totals["queries_completed"] += result.queries_completed
                    totals["findings"] += result.findings
                    totals["rejected"] += result.rejected
                    totals["over_budget"] += result.over_budget
                    if can_submit():
                        futures[pool.submit(execute, worker_id, next_round)] = worker_id
                        next_round += 1
        diagnostics_stop.set()
        diagnostics_thread.join(timeout=1.0)
        summary = RunSummary(
            run_id=request.run_id,
            rounds_completed=totals["rounds_completed"],
            queries_completed=totals["queries_completed"],
            findings=totals["findings"],
            rejected=totals["rejected"],
            over_budget=totals["over_budget"],
            stopped=stop_event.is_set(),
        )
        publisher.publish(
            "run_finished",
            {
                "findings": summary.findings,
                "queries_completed": summary.queries_completed,
                "rounds_completed": summary.rounds_completed,
                "stopped": summary.stopped,
            },
        )
        return summary


__all__ = [
    "CorrectnessRunService",
    "EventPublisher",
    "EventSink",
    "RoundContext",
    "RoundRunner",
    "RoundSummary",
    "RunSummary",
]
