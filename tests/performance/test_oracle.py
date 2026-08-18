from __future__ import annotations

from select_fuzz.config import NodeRole
from select_fuzz.performance.models import (
    FormalRun,
    Measurement,
    Outcome,
    Verdict,
)
from select_fuzz.performance.oracle import assess


def _measurement(role: NodeRole, outcome: Outcome, seconds: float | None) -> Measurement:
    return Measurement(
        role=role,
        outcome=outcome,
        started_ns=0,
        ended_ns=0,
        connection_id=1,
        root_end_ms=None if seconds is None else seconds * 1000,
        tree=None,
        cache_state="unverified",
    )


def _run(
    custom_off: tuple[Outcome, float | None],
    custom_on: tuple[Outcome, float | None],
    *,
    skew: float = 0.0,
) -> FormalRun:
    return FormalRun(
        measurements={
            NodeRole.CUSTOM_OFF: _measurement(NodeRole.CUSTOM_OFF, *custom_off),
            NodeRole.CUSTOM_ON: _measurement(NodeRole.CUSTOM_ON, *custom_on),
        },
        start_skew_ms=skew,
    )


def test_exact_threshold_is_alert_against_custom_off_reference() -> None:
    result = assess(
        _run(
            (Outcome.COMPLETED, 20.0),
            (Outcome.COMPLETED, 24.0),
        ),
        threshold=0.20,
    )

    assert result.verdict is Verdict.PERF_ALERT
    assert result.reasons == ("VS_CUSTOM_OFF",)


def test_skew_over_threshold_suppresses_a_performance_alert() -> None:
    unreliable = assess(
        _run(
            (Outcome.COMPLETED, 10.0),
            (Outcome.COMPLETED, 20.0),
            skew=100.001,
        ),
        max_skew_ms=100.0,
    )
    exact = assess(
        _run(
            (Outcome.COMPLETED, 10.0),
            (Outcome.COMPLETED, 20.0),
            skew=100.0,
        ),
        max_skew_ms=100.0,
    )

    assert unreliable.verdict is Verdict.TIMING_UNRELIABLE
    assert exact.verdict is Verdict.PERF_ALERT


def test_all_timeouts_are_over_budget_and_infra_never_gets_performance_verdict() -> None:
    all_timeout = _run(
        (Outcome.TIMEOUT, None),
        (Outcome.TIMEOUT, None),
    )
    infra = _run(
        (Outcome.INFRA_ERROR, None),
        (Outcome.COMPLETED, 20.0),
    )

    assert assess(all_timeout).verdict is Verdict.OVER_BUDGET
    assert assess(infra).verdict is Verdict.INFRA_ERROR


def test_reference_timeout_is_drift_but_custom_on_timeout_is_alert() -> None:
    reference_timeout = _run(
        (Outcome.TIMEOUT, None),
        (Outcome.COMPLETED, 10.0),
    )
    on_timeout = _run(
        (Outcome.COMPLETED, 10.0),
        (Outcome.TIMEOUT, None),
    )

    assert assess(reference_timeout).verdict is Verdict.CALIBRATION_DRIFT
    assert assess(on_timeout).verdict is Verdict.PERF_ALERT
