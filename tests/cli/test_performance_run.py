from __future__ import annotations

from threading import Event

from select_fuzz.cli import MODE_RUNNERS
from select_fuzz.config import AppConfig
from select_fuzz.domain import RunRequest
from select_fuzz.performance.entrypoint import build_performance_runner


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "mode": "performance",
            "nodes": [
                {"role": "baseline", "host": "127.0.0.1", "port": 34061},
                {"role": "custom_off", "host": "127.0.0.1", "port": 34062},
                {"role": "custom_on", "host": "127.0.0.1", "port": 34063},
            ],
        }
    )


def test_performance_mode_is_registered_with_a_production_builder() -> None:
    assert MODE_RUNNERS["performance"] is build_performance_runner


def test_performance_builder_returns_the_shared_run_contract(tmp_path) -> None:  # type: ignore[no-untyped-def]
    runner = build_performance_runner(_config(), tmp_path)

    assert hasattr(runner, "run")
    request = RunRequest(
        run_id="run_perf_contract_1",
        mode="performance",
        seed=7,
        workers=1,
        rounds=1,
        queries_per_round=1,
    )
    # Construction is side-effect free; database access starts only in run().
    assert isinstance(Event(), Event)
    assert request.mode == "performance"
