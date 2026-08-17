from __future__ import annotations

import math

import pytest

from select_fuzz.modes.fuzz.compatibility_backoff import (
    COMPATIBILITY_ERROR_ERRNOS,
    CompatibilityErrorBackoff,
)
from select_fuzz.modes.fuzz.models import FuzzExecutionResult


def _result(
    *,
    success: bool = False,
    errno: int | None = None,
    error_stage: str | None = None,
    timed_out: bool = False,
    connection_lost: bool = False,
) -> FuzzExecutionResult:
    return FuzzExecutionResult(
        success=success,
        rows_seen=0,
        elapsed_ns=0,
        errno=errno,
        error_stage=error_stage,
        timed_out=timed_out,
        connection_lost=connection_lost,
    )


def test_compatibility_error_backoff_uses_bounded_exponential_delays() -> None:
    backoff = CompatibilityErrorBackoff(initial_seconds=0.01, maximum_seconds=0.25)

    delays = [
        backoff.observe(_result(errno=1064, error_stage="execute")) for _ in range(8)
    ]

    assert COMPATIBILITY_ERROR_ERRNOS == frozenset({1064, 1234, 1235, 1253})
    assert delays == [0.01, 0.02, 0.04, 0.08, 0.16, 0.25, 0.25, 0.25]
    assert backoff.streak == 8


def test_compatibility_error_backoff_resets_on_success_and_noncompatibility() -> None:
    backoff = CompatibilityErrorBackoff(initial_seconds=0.01, maximum_seconds=0.25)

    assert backoff.observe(_result(errno=1064, error_stage="fetch")) == 0.01
    assert backoff.observe(_result(success=True)) == 0.0
    assert backoff.streak == 0
    assert backoff.observe(_result(errno=1064, error_stage="execute")) == 0.01

    for errno in (1205, 1213, 1690, None):
        assert backoff.observe(_result(errno=errno, error_stage="execute")) == 0.0
        assert backoff.streak == 0


def test_compatibility_error_backoff_resets_on_timeout_or_connection_loss() -> None:
    backoff = CompatibilityErrorBackoff(initial_seconds=0.01, maximum_seconds=0.25)

    assert backoff.observe(_result(errno=1064, error_stage="execute")) == 0.01
    assert (
        backoff.observe(_result(errno=1064, error_stage="execute", timed_out=True))
        == 0.0
    )
    assert backoff.streak == 0
    assert backoff.observe(_result(errno=1064, error_stage="fetch")) == 0.01
    assert (
        backoff.observe(
            _result(errno=1064, error_stage="fetch", connection_lost=True)
        )
        == 0.0
    )
    assert backoff.streak == 0


def test_compatibility_error_backoff_caps_before_large_exponents() -> None:
    backoff = CompatibilityErrorBackoff(
        initial_seconds=0.01,
        maximum_seconds=0.25,
        streak=1_000_000,
    )

    assert backoff.observe(_result(errno=1064, error_stage="execute")) == 0.25
    assert backoff.streak == 1_000_001


def test_compatibility_error_backoff_zero_values_disable_wait_but_keep_streak() -> None:
    backoff = CompatibilityErrorBackoff(initial_seconds=0, maximum_seconds=0)

    assert backoff.observe(_result(errno=1064, error_stage="execute")) == 0.0
    assert backoff.streak == 1
    assert backoff.observe(_result(errno=1234, error_stage="fetch")) == 0.0
    assert backoff.streak == 2
    assert backoff.observe(_result(errno=1205, error_stage="execute")) == 0.0
    assert backoff.streak == 0


@pytest.mark.parametrize(
    ("initial_seconds", "maximum_seconds"),
    [
        (-0.01, 0.25),
        (0.01, -0.25),
        (0.5, 0.25),
        (math.inf, math.inf),
        (math.nan, 0.25),
        (True, 0.25),
        (0.01, False),
    ],
)
def test_compatibility_error_backoff_rejects_invalid_bounds(
    initial_seconds: float,
    maximum_seconds: float,
) -> None:
    with pytest.raises(ValueError):
        CompatibilityErrorBackoff(initial_seconds, maximum_seconds)


@pytest.mark.parametrize("error_stage", ["watchdog_cancel", "cursor_close", None])
def test_compatibility_error_backoff_does_not_delay_cleanup_errors(
    error_stage: str | None,
) -> None:
    backoff = CompatibilityErrorBackoff(initial_seconds=0.01, maximum_seconds=0.25)

    assert backoff.observe(_result(errno=1064, error_stage="execute")) == 0.01
    assert backoff.observe(_result(errno=1064, error_stage=error_stage)) == 0.0
    assert backoff.streak == 0
