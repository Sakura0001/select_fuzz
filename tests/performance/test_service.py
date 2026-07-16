from __future__ import annotations

from dataclasses import dataclass
from threading import Event

from select_fuzz.config import NodeRole
from select_fuzz.performance.calibration import (
    CalibrationFailureKind,
    CalibrationInfrastructurePause,
    CalibrationTerminated,
)
from select_fuzz.performance.models import (
    Assessment,
    FormalRun,
    FrozenCase,
    Measurement,
    Outcome,
    PerformancePolicy,
    ScaleKnobs,
    Verdict,
)
from select_fuzz.performance.service import PerformanceService
from select_fuzz.performance.tree import Family, ShapeBoundary


@dataclass(frozen=True)
class _Template:
    case_id: str
    seed: int
    initial_scale: ScaleKnobs = ScaleKnobs()

    @property
    def template_id(self) -> str:
        return "service_fake"

    def for_case(self, round_number: int, query_number: int) -> _Template:
        return _Template(f"{self.case_id}_{round_number}_{query_number}", self.seed)


class _Preparation:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace

    def prepare(self, template: _Template, initial: ScaleKnobs, *, database: str) -> FrozenCase:
        self.trace.append(f"prepare:{template.case_id}")
        return FrozenCase(
            case_id=template.case_id,
            template_id=template.template_id,
            seed=template.seed,
            database=database,
            scale=initial,
            data_manifest={},
            sql="SELECT 1",
            boundary=ShapeBoundary(frozenset({Family.SCAN})),
            medians_seconds={NodeRole.BASELINE: 5.0, NodeRole.CUSTOM_OFF: 5.0},
            attempts=(),
        )


class _Formal:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace

    def run(self, frozen: FrozenCase) -> FormalRun:
        self.trace.append(f"formal:{frozen.case_id}")
        return FormalRun(
            measurements={
                role: Measurement(
                    role,
                    Outcome.COMPLETED,
                    0,
                    1,
                    1,
                    5_000.0,
                    None,
                    "unverified",
                )
                for role in NodeRole
            },
            start_skew_ms=0.0,
        )


class _Recorder:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.records: list[tuple[FrozenCase, FormalRun, Assessment]] = []

    def record(self, frozen: FrozenCase, run: FormalRun, assessment: Assessment) -> None:
        self.trace.append(f"record:{frozen.case_id}")
        self.records.append((frozen, run, assessment))

    def record_calibration_failure(
        self,
        template: object,
        attempts: object,
        failure: object = None,
        *,
        attempt_number: int = 1,
    ) -> None:
        raise AssertionError((template, attempts, failure, attempt_number))


def test_service_runs_cases_strictly_sequentially_and_persists_each_result() -> None:
    trace: list[str] = []
    recorder = _Recorder(trace)
    service = PerformanceService(
        _Preparation(trace),
        _Formal(trace),
        recorder,
        database_name=lambda round_number: f"perf_{round_number}",
        policy=PerformancePolicy(queries_per_round=2),
    )

    result = service.run([_Template("case", 1)], rounds=2)

    assert trace == [
        "prepare:case_1_1",
        "formal:case_1_1",
        "record:case_1_1",
        "prepare:case_1_2",
        "formal:case_1_2",
        "record:case_1_2",
        "prepare:case_2_1",
        "formal:case_2_1",
        "record:case_2_1",
        "prepare:case_2_2",
        "formal:case_2_2",
        "record:case_2_2",
    ]
    assert all(item[2].verdict is Verdict.PASS for item in recorder.records)
    assert (result.rounds_started, result.rounds_completed) == (2, 2)
    assert (result.queries_completed, result.rejected, result.calibration_failures) == (4, 0, 0)


def test_service_keeps_a_fair_template_cursor_across_round_boundaries() -> None:
    trace: list[str] = []
    service = PerformanceService(
        _Preparation(trace),
        _Formal(trace),
        _Recorder(trace),
        database_name=lambda round_number: f"perf_{round_number}",
        policy=PerformancePolicy(queries_per_round=1),
    )

    service.run([_Template("alpha", 1), _Template("beta", 2)], rounds=2)

    assert trace[0] == "prepare:alpha_1_1"
    assert trace[3] == "prepare:beta_2_1"


def test_service_persists_infrastructure_pause_then_retries_same_case() -> None:
    trace: list[str] = []

    class InfraThenSuccess(_Preparation):
        calls = 0

        def prepare(self, template: _Template, initial: ScaleKnobs, *, database: str) -> FrozenCase:
            self.calls += 1
            if self.calls == 1:
                raise CalibrationInfrastructurePause(
                    CalibrationFailureKind.INFRA,
                    NodeRole.BASELINE,
                    error_type="ConnectionUnavailable",
                )
            return super().prepare(template, initial, database=database)

    class RetryRecorder(_Recorder):
        def record_calibration_failure(
            self,
            template: object,
            attempts: object,
            failure: object = None,
            *,
            attempt_number: int = 1,
        ) -> None:
            del template, attempts, failure
            self.trace.append(f"pause:{attempt_number}")

    recorder = RetryRecorder(trace)
    service = PerformanceService(
        InfraThenSuccess(trace),
        _Formal(trace),
        recorder,
        database_name=lambda round_number: f"perf_{round_number}",
        policy=PerformancePolicy(queries_per_round=1),
        retry_waiter=lambda event, delay: event.is_set(),
    )

    results = service.run([_Template("case", 1)], rounds=1, stop_event=Event())

    assert len(results.assessments) == 1
    assert trace[:2] == ["pause:1", "prepare:case_1_1"]


def test_service_counts_terminal_calibration_failure_as_rejected() -> None:
    trace: list[str] = []

    class SetupMismatch(_Preparation):
        def prepare(self, template: _Template, initial: ScaleKnobs, *, database: str) -> FrozenCase:
            del initial, database
            raise CalibrationTerminated(
                CalibrationFailureKind.SETUP_MISMATCH,
                NodeRole.BASELINE,
                error_type="MaterializationMismatch",
            )

    class FailureRecorder(_Recorder):
        def record_calibration_failure(
            self,
            template: object,
            attempts: object,
            failure: object = None,
            *,
            attempt_number: int = 1,
        ) -> None:
            del template, attempts, failure, attempt_number
            self.trace.append("rejected")

    result = PerformanceService(
        SetupMismatch(trace),
        _Formal(trace),
        FailureRecorder(trace),
        database_name=lambda round_number: f"perf_{round_number}",
        policy=PerformancePolicy(queries_per_round=1),
    ).run([_Template("case", 1)], rounds=1)

    assert result.queries_completed == 0
    assert result.rejected == result.calibration_failures == 1
    assert (result.rounds_started, result.rounds_completed) == (1, 1)


def test_terminal_calibration_failure_preserves_round_database_and_starts_new_one() -> None:
    trace: list[str] = []

    class FailFirstRound(_Preparation):
        calls = 0

        def prepare(self, template: _Template, initial: ScaleKnobs, *, database: str) -> FrozenCase:
            self.calls += 1
            trace.append(f"database:{database}")
            if self.calls == 1:
                raise CalibrationTerminated(
                    CalibrationFailureKind.SETUP_MISMATCH,
                    NodeRole.BASELINE,
                    error_type="MaterializationMismatch",
                )
            return super().prepare(template, initial, database=database)

    class FailureRecorder(_Recorder):
        def record_calibration_failure(
            self,
            template: object,
            attempts: object,
            failure: object = None,
            *,
            attempt_number: int = 1,
        ) -> None:
            del template, attempts, failure, attempt_number
            self.trace.append("rejected")

    result = PerformanceService(
        FailFirstRound(trace),
        _Formal(trace),
        FailureRecorder(trace),
        database_name=lambda round_number: f"perf_{round_number}",
        policy=PerformancePolicy(queries_per_round=2),
    ).run([_Template("case", 1)], rounds=2)

    assert "prepare:case_1_2" not in trace
    assert trace.count("database:perf_1") == 1
    assert trace.count("database:perf_2") == 2
    assert result.rejected == result.calibration_failures == 1
    assert result.queries_completed == 2


def test_performance_finding_stops_current_database_and_starts_next_round() -> None:
    trace: list[str] = []

    class AlertFormal(_Formal):
        def run(self, frozen: FrozenCase) -> FormalRun:
            self.trace.append(f"formal:{frozen.case_id}")
            return FormalRun(
                measurements={
                    role: Measurement(
                        role,
                        Outcome.COMPLETED,
                        0,
                        1,
                        1,
                        10_000.0 if role is NodeRole.CUSTOM_ON else 5_000.0,
                        None,
                        "unverified",
                    )
                    for role in NodeRole
                },
                start_skew_ms=0.0,
            )

    result = PerformanceService(
        _Preparation(trace),
        AlertFormal(trace),
        _Recorder(trace),
        database_name=lambda round_number: f"perf_{round_number}",
        policy=PerformancePolicy(queries_per_round=2),
    ).run([_Template("case", 1)], rounds=2)

    assert "prepare:case_1_2" not in trace
    assert "prepare:case_2_2" not in trace
    assert result.queries_completed == 2
    assert all(item.verdict is Verdict.PERF_ALERT for item in result.assessments)


def test_stop_before_round_does_not_count_started_or_completed_round() -> None:
    stop = Event()
    stop.set()
    trace: list[str] = []
    result = PerformanceService(
        _Preparation(trace),
        _Formal(trace),
        _Recorder(trace),
        database_name=lambda round_number: f"perf_{round_number}",
        policy=PerformancePolicy(queries_per_round=1),
    ).run([_Template("case", 1)], rounds=1, stop_event=stop)

    assert (result.rounds_started, result.rounds_completed) == (0, 0)
