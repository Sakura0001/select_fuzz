"""Immutable performance-mode contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
import math
from types import MappingProxyType

from select_fuzz.config import COMPARISON_ROLES, NodeRole, PerformanceConfig


def _finite_number(name: str, value: float, *, positive: bool = False) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or (positive and value <= 0)
    ):
        qualifier = "positive " if positive else ""
        raise ValueError(f"{name} must be a finite {qualifier}number")


@dataclass(frozen=True, slots=True)
class PerformancePolicy:
    worker_count: int = 1
    queries_per_round: int = 100
    calibration_runs_per_reference: int = 3
    calibration_band_seconds: tuple[float, float] = (5.0, 12.0)
    max_calibration_rounds: int = 8
    formal_timeout_seconds: float = 15.0
    regression_threshold: float = 0.20
    max_start_skew_ms: float = 100.0
    scale_multiplier: float = 2.0
    max_table_rows: int = 50_000_000
    workload_kind: str = "seeded_fuzz"
    cache_state: str = "unverified"

    @classmethod
    def from_config(cls, config: PerformanceConfig) -> PerformancePolicy:
        calibration_upper = min(config.calibration_max_seconds, config.formal_timeout_seconds)
        calibration_lower = min(config.calibration_min_seconds, calibration_upper)
        return cls(
            worker_count=config.workers,
            queries_per_round=config.queries_per_round,
            calibration_runs_per_reference=config.calibration_runs_per_reference,
            calibration_band_seconds=(
                calibration_lower,
                calibration_upper,
            ),
            max_calibration_rounds=config.max_calibration_rounds,
            formal_timeout_seconds=config.formal_timeout_seconds,
            regression_threshold=config.regression_threshold,
            max_start_skew_ms=config.max_start_skew_ms,
            max_table_rows=config.max_table_rows,
        )

    def __post_init__(self) -> None:
        if self.worker_count != 1:
            raise ValueError("performance mode requires exactly one worker")
        if self.calibration_runs_per_reference != 3:
            raise ValueError("performance calibration requires three runs per reference")
        for name in ("queries_per_round", "max_calibration_rounds", "max_table_rows"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if len(self.calibration_band_seconds) != 2:
            raise ValueError("calibration_band_seconds requires a lower and upper bound")
        lower, upper = self.calibration_band_seconds
        _finite_number("calibration lower bound", lower, positive=True)
        _finite_number("calibration upper bound", upper, positive=True)
        _finite_number("formal_timeout_seconds", self.formal_timeout_seconds, positive=True)
        _finite_number("regression_threshold", self.regression_threshold)
        _finite_number("max_start_skew_ms", self.max_start_skew_ms)
        _finite_number("scale_multiplier", self.scale_multiplier, positive=True)
        if lower > upper:
            raise ValueError("calibration lower bound must not exceed upper bound")
        if upper > self.formal_timeout_seconds:
            raise ValueError("calibration upper bound must not exceed formal timeout")
        if self.regression_threshold < 0 or self.max_start_skew_ms < 0:
            raise ValueError("threshold and skew limit must be nonnegative")
        if self.scale_multiplier <= 1:
            raise ValueError("scale_multiplier must be greater than one")
        if self.workload_kind != "seeded_fuzz":
            raise ValueError("performance mode is restricted to seeded_fuzz workloads")
        if self.cache_state != "unverified":
            raise ValueError("cache state must remain unverified")


@dataclass(frozen=True, slots=True)
class ScaleKnobs:
    table_rows: int = 100_000
    scan_rows: int = 100_000
    range_selectivity: float = 0.10
    join_build_rows: int = 25_000
    join_probe_rows: int = 100_000
    join_fanout: float = 4.0
    aggregate_input_rows: int = 100_000
    aggregate_groups: int = 1_000
    sort_rows: int = 100_000
    sort_key_bytes: int = 32
    window_partition_rows: int = 1_000
    window_frame_rows: int = 100

    @classmethod
    def from_config(cls, config: PerformanceConfig) -> ScaleKnobs:
        initial = cls()
        return initial.scaled(
            config.initial_table_rows / initial.table_rows,
            row_cap=config.max_table_rows,
        )

    def __post_init__(self) -> None:
        integer_names = (
            "table_rows",
            "scan_rows",
            "join_build_rows",
            "join_probe_rows",
            "aggregate_input_rows",
            "aggregate_groups",
            "sort_rows",
            "sort_key_bytes",
            "window_partition_rows",
            "window_frame_rows",
        )
        for name in integer_names:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        _finite_number("range_selectivity", self.range_selectivity, positive=True)
        _finite_number("join_fanout", self.join_fanout, positive=True)
        if self.range_selectivity > 1:
            raise ValueError("range_selectivity must not exceed one")
        if self.aggregate_groups > self.aggregate_input_rows:
            raise ValueError("aggregate_groups must not exceed aggregate_input_rows")
        if self.window_frame_rows > self.window_partition_rows:
            raise ValueError("window_frame_rows must not exceed window_partition_rows")

    def scaled(self, factor: float, *, row_cap: int) -> ScaleKnobs:
        _finite_number("scale factor", factor, positive=True)
        if not isinstance(row_cap, int) or isinstance(row_cap, bool) or row_cap <= 0:
            raise ValueError("row_cap must be a positive integer")
        count_names = (
            "table_rows",
            "scan_rows",
            "join_build_rows",
            "join_probe_rows",
            "aggregate_input_rows",
            "aggregate_groups",
            "sort_rows",
            "window_partition_rows",
            "window_frame_rows",
        )
        values = {
            name: min(row_cap, max(1, math.ceil(getattr(self, name) * factor)))
            for name in count_names
        }
        values["aggregate_groups"] = min(values["aggregate_groups"], values["aggregate_input_rows"])
        values["window_frame_rows"] = min(
            values["window_frame_rows"], values["window_partition_rows"]
        )
        return replace(self, **values)

    def as_dict(self) -> dict[str, int | float]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


WorkloadScale = ScaleKnobs


class Outcome(StrEnum):
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"
    INFRA_ERROR = "infra_error"
    PARSE_ERROR = "parse_error"
    SHAPE_MISMATCH = "shape_mismatch"


class Verdict(StrEnum):
    PASS = "pass"
    PERF_ALERT = "perf_alert"
    TIMING_UNRELIABLE = "timing_unreliable"
    OVER_BUDGET = "over_budget"
    CALIBRATION_DRIFT = "calibration_drift"
    INFRA_ERROR = "infra_error"
    EXECUTION_ERROR = "execution_error"


@dataclass(frozen=True, slots=True)
class CalibrationAttempt:
    number: int
    scale: ScaleKnobs
    sql: str
    samples_seconds: Mapping[NodeRole, tuple[float | None, ...]]
    medians_seconds: Mapping[NodeRole, float]
    failure_categories: Mapping[NodeRole, tuple[str | None, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "samples_seconds",
            MappingProxyType(
                {role: tuple(values) for role, values in self.samples_seconds.items()}
            ),
        )
        object.__setattr__(self, "medians_seconds", MappingProxyType(dict(self.medians_seconds)))
        object.__setattr__(
            self,
            "failure_categories",
            MappingProxyType(
                {role: tuple(values) for role, values in self.failure_categories.items()}
            ),
        )


@dataclass(frozen=True, slots=True)
class FrozenCase:
    case_id: str
    template_id: str
    seed: int
    database: str
    scale: ScaleKnobs
    data_manifest: object
    sql: str
    boundary: object
    medians_seconds: Mapping[NodeRole, float]
    attempts: tuple[CalibrationAttempt, ...]

    def __post_init__(self) -> None:
        if not self.case_id or not self.template_id or not self.database or not self.sql.strip():
            raise ValueError("frozen case identifiers and SQL must not be empty")
        object.__setattr__(self, "medians_seconds", MappingProxyType(dict(self.medians_seconds)))
        object.__setattr__(self, "attempts", tuple(self.attempts))


@dataclass(frozen=True, slots=True)
class Measurement:
    role: NodeRole
    outcome: Outcome
    started_ns: int
    ended_ns: int
    connection_id: int | None
    root_end_ms: float | None
    tree: str | None
    cache_state: str
    wall_time_ms: float | None = None
    metrics: Mapping[str, object] | None = None
    error_code: int | None = None
    watchdog_fired: bool = False
    error_type: str | None = None

    def __post_init__(self) -> None:
        if self.ended_ns < self.started_ns:
            raise ValueError("measurement end must not precede start")
        if self.outcome is Outcome.COMPLETED and self.root_end_ms is None:
            raise ValueError("completed measurement requires root iterator time")
        if self.outcome is not Outcome.COMPLETED and self.root_end_ms is not None:
            raise ValueError("non-completed measurement cannot carry root iterator time")
        if self.cache_state != "unverified":
            raise ValueError("performance cache state must remain unverified")
        if self.metrics is not None:
            object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        if self.error_type is not None and not self.error_type:
            raise ValueError("error_type must not be empty when supplied")


@dataclass(frozen=True, slots=True)
class FormalRun:
    measurements: Mapping[NodeRole, Measurement]
    start_skew_ms: float

    def __post_init__(self) -> None:
        if set(self.measurements) != set(COMPARISON_ROLES):
            raise ValueError("formal run requires exactly the two comparison roles")
        _finite_number("start_skew_ms", self.start_skew_ms)
        if self.start_skew_ms < 0:
            raise ValueError("start_skew_ms must be nonnegative")
        object.__setattr__(self, "measurements", MappingProxyType(dict(self.measurements)))


@dataclass(frozen=True, slots=True)
class Assessment:
    verdict: Verdict
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))


__all__ = [
    "Assessment",
    "CalibrationAttempt",
    "FormalRun",
    "FrozenCase",
    "Measurement",
    "Outcome",
    "PerformancePolicy",
    "ScaleKnobs",
    "Verdict",
    "WorkloadScale",
]
