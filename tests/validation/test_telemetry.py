from __future__ import annotations

import json
from pathlib import Path

from select_fuzz.validation.models import TelemetrySample
from select_fuzz.validation.telemetry import (
    FaultKind,
    ResourceTrendPolicy,
    TelemetryRecorder,
    ScheduledFaultController,
    build_fault_schedule,
)


def test_fault_schedule_is_seed_reproducible_and_bounded() -> None:
    first = build_fault_schedule(seed=7, duration_s=3600, events_per_hour=4)
    second = build_fault_schedule(seed=7, duration_s=3600, events_per_hour=4)

    assert first == second
    assert {event.kind for event in first} == set(FaultKind)
    assert [event.at_s for event in first] == sorted(event.at_s for event in first)
    assert all(0 < event.at_s < 3600 for event in first)


def test_linear_resource_growth_is_rejected() -> None:
    samples = tuple(
        TelemetrySample(
            "run",
            epoch=index,
            monotonic_s=index * 60,
            rss_bytes=100_000_000 + index * 10_000_000,
            threads=10 + index,
            open_fds=20 + index,
            mysql_connections=3,
        )
        for index in range(20)
    )

    verdict = ResourceTrendPolicy(max_growth_ratio=0.20).evaluate(samples)

    assert verdict.passed is False
    assert {"rss_bytes", "threads", "open_fds"} <= set(verdict.reasons)


def test_telemetry_recorder_appends_jsonl(tmp_path: Path) -> None:
    recorder = TelemetryRecorder(tmp_path / "soak.jsonl")
    sample = TelemetrySample("run", 1, 3.5, 100, 2, 4, 3)
    recorder.append(sample)
    recorder.append(sample)

    rows = [json.loads(line) for line in (tmp_path / "soak.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["rss_bytes"] == 100


def test_scheduled_faults_are_wired_once_when_elapsed_crosses_deadline() -> None:
    schedule = build_fault_schedule(seed=3, duration_s=100, events_per_hour=72)
    seen: list[FaultKind] = []
    controller = ScheduledFaultController(schedule, inject=lambda event: seen.append(event.kind))
    controller.tick(50)
    first_count = len(seen)
    controller.tick(50)
    controller.tick(100)
    assert first_count > 0
    assert len(seen) == len(schedule)


def test_resumed_fault_controller_does_not_replay_past_events() -> None:
    schedule = build_fault_schedule(seed=5, duration_s=100, events_per_hour=72)
    seen: list[FaultKind] = []
    controller = ScheduledFaultController(
        schedule, inject=lambda event: seen.append(event.kind), resume_elapsed_s=50
    )
    controller.tick(50)
    assert seen == []
    controller.tick(100)
    assert len(seen) == len([event for event in schedule if event.at_s > 50])
