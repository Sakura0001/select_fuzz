"""Single-worker sequential performance orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from threading import Event
from typing import Protocol, Self

from select_fuzz.performance.calibration import (
    CalibrationExhausted,
    CalibrationInfrastructurePause,
    CalibrationTerminated,
    PerformanceTemplate,
)
from select_fuzz.performance.models import (
    Assessment,
    FormalRun,
    FrozenCase,
    PerformancePolicy,
    ScaleKnobs,
    Verdict,
)
from select_fuzz.performance.oracle import assess


class ServiceTemplate(PerformanceTemplate, Protocol):
    @property
    def initial_scale(self) -> ScaleKnobs: ...

    def for_case(self, round_number: int, query_number: int) -> Self: ...


class CasePreparationPort(Protocol):
    def prepare(
        self, template: PerformanceTemplate, initial: ScaleKnobs, *, database: str
    ) -> FrozenCase: ...


class FormalServicePort(Protocol):
    def run(self, frozen: FrozenCase) -> FormalRun: ...


class PerformanceRecordPort(Protocol):
    def record(self, frozen: FrozenCase, run: FormalRun, assessment: Assessment) -> object: ...

    def record_calibration_failure(
        self,
        template: object,
        attempts: object,
        failure: CalibrationTerminated | None = None,
        *,
        attempt_number: int = 1,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PerformanceServiceResult:
    assessments: tuple[Assessment, ...]
    rounds_started: int
    rounds_completed: int
    rejected: int
    setup_failures: int

    @property
    def queries_completed(self) -> int:
        return len(self.assessments)

    @property
    def calibration_failures(self) -> int:
        """Compatibility alias for pre-shared-round callers and event readers."""

        return self.setup_failures


class PerformanceService:
    def __init__(
        self,
        preparation: CasePreparationPort,
        formal: FormalServicePort,
        recorder: PerformanceRecordPort,
        *,
        database_name: Callable[[int], str],
        policy: PerformancePolicy,
        retry_waiter: Callable[[Event, float], bool] | None = None,
    ) -> None:
        self._preparation = preparation
        self._formal = formal
        self._recorder = recorder
        self._database_name = database_name
        self._policy = policy
        self._retry_waiter = retry_waiter or (lambda stop, seconds: stop.wait(seconds))

    def run(
        self,
        templates: Iterable[ServiceTemplate],
        *,
        rounds: int | None = None,
        queries_per_round: int | None = None,
        stop_event: Event | None = None,
    ) -> PerformanceServiceResult:
        catalog = tuple(templates)
        if not catalog:
            raise ValueError("performance template catalog is empty")
        if rounds is not None and (
            not isinstance(rounds, int) or isinstance(rounds, bool) or rounds <= 0
        ):
            raise ValueError("rounds must be a positive integer when supplied")
        per_round = (
            self._policy.queries_per_round if queries_per_round is None else queries_per_round
        )
        if not isinstance(per_round, int) or isinstance(per_round, bool) or per_round <= 0:
            raise ValueError("queries_per_round must be a positive integer")
        results: list[Assessment] = []
        rounds_started = 0
        rounds_completed = 0
        rejected = 0
        setup_failures = 0
        active_stop = stop_event or Event()
        round_number = 1
        template_cursor = 0

        def result() -> PerformanceServiceResult:
            return PerformanceServiceResult(
                tuple(results),
                rounds_started,
                rounds_completed,
                rejected,
                setup_failures,
            )

        while rounds is None or round_number <= rounds:
            if active_stop.is_set():
                return result()
            database = self._database_name(round_number)
            rounds_started += 1
            terminate_current_round = False
            for query_number in range(1, per_round + 1):
                template = catalog[template_cursor % len(catalog)]
                template_cursor += 1
                if active_stop.is_set():
                    return result()
                case_template = template.for_case(round_number, query_number)
                retry_delay = 0.25
                frozen: FrozenCase | None = None
                infrastructure_attempt = 0
                while frozen is None:
                    try:
                        frozen = self._preparation.prepare(
                            case_template,
                            case_template.initial_scale,
                            database=database,
                        )
                    except CalibrationInfrastructurePause as error:
                        infrastructure_attempt += 1
                        self._recorder.record_calibration_failure(
                            case_template,
                            error.attempts,
                            error,
                            attempt_number=infrastructure_attempt,
                        )
                        if self._retry_waiter(active_stop, retry_delay):
                            return result()
                        retry_delay = min(30.0, retry_delay * 2)
                    except CalibrationTerminated as error:
                        self._recorder.record_calibration_failure(
                            case_template, error.attempts, error, attempt_number=1
                        )
                        rejected += 1
                        setup_failures += 1
                        terminate_current_round = True
                        break
                    except CalibrationExhausted as error:
                        self._recorder.record_calibration_failure(
                            case_template, error.attempts, attempt_number=1
                        )
                        rejected += 1
                        setup_failures += 1
                        terminate_current_round = True
                        break
                if frozen is None:
                    if terminate_current_round:
                        break
                    continue
                formal = self._formal.run(frozen)
                assessment = assess(
                    formal,
                    threshold=self._policy.regression_threshold,
                    max_skew_ms=self._policy.max_start_skew_ms,
                )
                self._recorder.record(frozen, formal, assessment)
                results.append(assessment)
                if assessment.verdict is not Verdict.PASS:
                    terminate_current_round = True
                    break
            rounds_completed += 1
            round_number += 1
        return result()


__all__ = [
    "CasePreparationPort",
    "FormalServicePort",
    "PerformanceRecordPort",
    "PerformanceService",
    "PerformanceServiceResult",
    "ServiceTemplate",
]
