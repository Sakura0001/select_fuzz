from __future__ import annotations

import pytest

from select_fuzz.config import PerformanceConfig
from select_fuzz.performance.models import PerformancePolicy, ScaleKnobs


def test_policy_defaults_are_the_seeded_fuzz_contract() -> None:
    policy = PerformancePolicy()

    assert policy.worker_count == 1
    assert policy.calibration_runs_per_reference == 3
    assert policy.calibration_band_seconds == (5.0, 12.0)
    assert policy.formal_timeout_seconds == 15.0
    assert policy.regression_threshold == 0.20
    assert policy.max_start_skew_ms == 100.0
    assert policy.workload_kind == "seeded_fuzz"
    assert policy.cache_state == "unverified"


def test_scale_knobs_grow_with_a_hard_row_cap_and_preserve_relations() -> None:
    scale = ScaleKnobs(window_partition_rows=10, window_frame_rows=10)

    grown = scale.scaled(1_000, row_cap=50_000_000)

    assert grown.table_rows == 50_000_000
    assert grown.window_frame_rows <= grown.window_partition_rows
    assert grown.aggregate_groups <= grown.aggregate_input_rows


@pytest.mark.parametrize(
    "kwargs",
    [
        {"worker_count": 2},
        {"calibration_runs_per_reference": 2},
        {"calibration_band_seconds": (30.0, 5.0)},
        {"formal_timeout_seconds": 0.0},
        {"formal_timeout_seconds": 10.0, "calibration_band_seconds": (5.0, 12.0)},
        {"regression_threshold": -0.1},
    ],
)
def test_policy_rejects_invalid_timing_contracts(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        PerformancePolicy(**kwargs)  # type: ignore[arg-type]


def test_policy_adapts_the_shared_config_without_cli_specific_logic() -> None:
    config = PerformanceConfig(
        queries_per_round=17,
        max_table_rows=900_000,
        max_calibration_rounds=4,
        calibration_min_seconds=6.0,
        calibration_max_seconds=20.0,
        formal_timeout_seconds=40.0,
        regression_threshold=0.25,
        max_start_skew_ms=75.0,
    )

    policy = PerformancePolicy.from_config(config)

    assert policy.queries_per_round == 17
    assert policy.max_table_rows == 900_000
    assert policy.max_calibration_rounds == 4
    assert policy.calibration_band_seconds == (6.0, 20.0)
    assert policy.formal_timeout_seconds == 40.0
    assert policy.regression_threshold == 0.25
    assert policy.max_start_skew_ms == 75.0
    assert ScaleKnobs.from_config(config).table_rows == config.initial_table_rows
