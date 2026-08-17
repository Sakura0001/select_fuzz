"""Bounded per-reader delay for consecutive MySQL compatibility errors."""

from __future__ import annotations

from dataclasses import dataclass
import math

from select_fuzz.modes.fuzz.models import FuzzExecutionResult


COMPATIBILITY_ERROR_ERRNOS = frozenset({1064, 1234, 1235, 1253})


@dataclass(slots=True)
class CompatibilityErrorBackoff:
    """Track one reader's compatibility-error streak without shared state."""

    initial_seconds: float
    maximum_seconds: float
    streak: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("initial_seconds", self.initial_seconds),
            ("maximum_seconds", self.maximum_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.initial_seconds > self.maximum_seconds:
            raise ValueError("initial_seconds must not exceed maximum_seconds")
        if isinstance(self.streak, bool) or not isinstance(self.streak, int) or self.streak < 0:
            raise ValueError("streak must be a nonnegative integer")

    def observe(self, result: FuzzExecutionResult) -> float:
        """Record a result and return the interruption-friendly delay to apply."""

        if (
            result.success
            or result.timed_out
            or result.connection_lost
            or result.errno not in COMPATIBILITY_ERROR_ERRNOS
        ):
            self.streak = 0
            return 0.0

        self.streak += 1
        if self.initial_seconds == 0 or self.maximum_seconds == 0:
            return 0.0

        exponent = self.streak - 1
        cap_exponent = max(
            0,
            math.ceil(
                math.log2(self.maximum_seconds) - math.log2(self.initial_seconds)
            ),
        )
        if exponent >= cap_exponent:
            return self.maximum_seconds
        return min(self.maximum_seconds, math.ldexp(self.initial_seconds, exponent))


__all__ = ["COMPATIBILITY_ERROR_ERRNOS", "CompatibilityErrorBackoff"]
