from __future__ import annotations

from dataclasses import dataclass
from threading import Barrier, Event, Lock

import pytest

from select_fuzz.domain import RunEvent, RunRequest
from select_fuzz.service import (
    CorrectnessRunService,
    EventPublisher,
    RoundContext,
    RoundSummary,
)


class _Events:
    def __init__(self) -> None:
        self.items: list[RunEvent] = []
        self.lock = Lock()

    def publish(self, event: RunEvent) -> None:
        with self.lock:
            self.items.append(event)


@dataclass
class _Rounds:
    barrier: Barrier | None = None

    def run_round(
        self,
        context: RoundContext,
        events: EventPublisher,
        stop_event: Event,
    ) -> RoundSummary:
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        findings = 1 if context.round_number == 0 else 0
        if findings:
            events.publish("finding", {"round_number": context.round_number})
        for query_index in range(context.request.queries_per_round):
            events.publish(
                "query_completed",
                {"round_number": context.round_number, "query_index": query_index},
            )
        return RoundSummary(
            round_number=context.round_number,
            queries_completed=context.request.queries_per_round,
            findings=findings,
            rejected=0,
            over_budget=0,
        )


def _request(*, rounds: int = 2, workers: int = 2) -> RunRequest:
    return RunRequest(
        run_id="run_service_1",
        mode="correctness",
        seed=7,
        workers=workers,
        rounds=rounds,
        queries_per_round=5,
    )


def test_correctness_service_publishes_and_continues_after_finding() -> None:
    sink = _Events()
    service = CorrectnessRunService(_Rounds(), sink)

    summary = service.run(_request(), Event())

    assert summary.rounds_completed == 2
    assert summary.queries_completed == 10
    assert summary.findings == 1
    assert [event.kind for event in sink.items][0] == "run_started"
    assert [event.kind for event in sink.items].count("round_started") == 2
    assert [event.kind for event in sink.items][-1] == "run_finished"
    assert [event.sequence for event in sink.items] == list(range(len(sink.items)))


def test_finite_rounds_run_concurrently_up_to_worker_limit() -> None:
    sink = _Events()
    service = CorrectnessRunService(_Rounds(barrier=Barrier(2)), sink)

    summary = service.run(_request(rounds=2, workers=2), Event())

    assert summary.rounds_completed == 2


def test_stop_event_prevents_new_rounds_without_marking_failure() -> None:
    sink = _Events()
    stop = Event()
    stop.set()

    summary = CorrectnessRunService(_Rounds(), sink).run(_request(), stop)

    assert summary.rounds_completed == 0
    assert summary.stopped is True
    assert [event.kind for event in sink.items] == ["run_started", "run_finished"]


def test_fatal_worker_error_aborts_sessions_and_logs_original_exception() -> None:
    class FailingRounds:
        def __init__(self) -> None:
            self.abort_active_calls = 0

        def run_round(self, context, events, stop_event):  # type: ignore[no-untyped-def]
            raise ValueError("artifact payload exploded")

        def abort_active(self) -> int:
            self.abort_active_calls += 1
            return 2

    sink = _Events()
    rounds = FailingRounds()

    with pytest.raises(ValueError, match="artifact payload exploded"):
        CorrectnessRunService(rounds, sink).run(_request(rounds=1, workers=1), Event())

    assert rounds.abort_active_calls == 1
    failed = next(event for event in sink.items if event.kind == "run_failed")
    assert failed.payload["exception_type"] == "ValueError"
    assert failed.payload["message"] == "artifact payload exploded"
    assert failed.payload["aborted_sessions"] == 2
    assert failed.payload["evidence"]["failure_stage"] == "correctness_worker"


def test_runtime_diagnostics_publish_active_workers_and_connections() -> None:
    class SlowRounds(_Rounds):
        def run_round(self, context, events, stop_event):  # type: ignore[no-untyped-def]
            Event().wait(0.06)
            return RoundSummary(context.round_number, 0, 0, 0, 0)

        def runtime_diagnostics(self):  # type: ignore[no-untyped-def]
            return {"active_session_count": 2, "connection_ids": (101, 102)}

    sink = _Events()

    CorrectnessRunService(
        SlowRounds(),
        sink,
        diagnostics_interval_seconds=0.01,
    ).run(_request(rounds=1, workers=1), Event())

    diagnostics = [event for event in sink.items if event.kind == "runtime_diagnostics"]
    assert diagnostics
    assert diagnostics[0].payload["active_worker_count"] == 1
    assert diagnostics[0].payload["runtime"] == {
        "active_session_count": 2,
        "connection_ids": (101, 102),
    }
