"""Performance verdicts with explicit reliability precedence."""

from __future__ import annotations

from select_fuzz.config import NodeRole
from select_fuzz.performance.models import Assessment, FormalRun, Outcome, Verdict


REFERENCE_ROLES = (NodeRole.BASELINE, NodeRole.CUSTOM_OFF)


def assess(
    run: FormalRun,
    threshold: float = 0.20,
    max_skew_ms: float = 100.0,
) -> Assessment:
    if threshold < 0 or max_skew_ms < 0:
        raise ValueError("threshold and max skew must be nonnegative")
    measurements = run.measurements
    if any(item.outcome is Outcome.INFRA_ERROR for item in measurements.values()):
        return Assessment(Verdict.INFRA_ERROR)
    if all(item.outcome is Outcome.TIMEOUT for item in measurements.values()):
        return Assessment(Verdict.OVER_BUDGET)
    if any(measurements[role].outcome is Outcome.TIMEOUT for role in REFERENCE_ROLES):
        return Assessment(Verdict.CALIBRATION_DRIFT)
    if run.start_skew_ms > max_skew_ms:
        return Assessment(Verdict.TIMING_UNRELIABLE)
    custom_on = measurements[NodeRole.CUSTOM_ON]
    if custom_on.outcome is Outcome.TIMEOUT:
        return Assessment(Verdict.PERF_ALERT, ("CUSTOM_ON_TIMEOUT",))
    if any(item.outcome is not Outcome.COMPLETED for item in measurements.values()):
        return Assessment(Verdict.EXECUTION_ERROR)
    if custom_on.root_end_ms is None:  # protected by Measurement invariant
        return Assessment(Verdict.EXECUTION_ERROR)
    comparisons = (
        (NodeRole.CUSTOM_OFF, "VS_CUSTOM_OFF"),
        (NodeRole.BASELINE, "VS_BASELINE"),
    )
    reasons = tuple(
        label
        for role, label in comparisons
        if measurements[role].root_end_ms is not None
        and custom_on.root_end_ms
        >= measurements[role].root_end_ms * (1 + threshold)  # type: ignore[operator]
    )
    return Assessment(Verdict.PERF_ALERT if reasons else Verdict.PASS, reasons)


__all__ = ["REFERENCE_ROLES", "assess"]
