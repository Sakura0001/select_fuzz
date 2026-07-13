from __future__ import annotations

import os
from pathlib import Path
from threading import Event

import pytest

from select_fuzz.artifacts import read_jsonl
from select_fuzz.config import AppConfig, NodeConfig, NodeRole, PerformanceConfig
from select_fuzz.doctor import build_doctor
from select_fuzz.domain import RunRequest
from select_fuzz.performance.entrypoint import build_performance_runner


def _release_config() -> AppConfig:
    if os.environ.get("SELECT_FUZZ_MYSQL_PERFORMANCE_INTEGRATION") != "1":
        pytest.skip(
            "set SELECT_FUZZ_MYSQL_PERFORMANCE_INTEGRATION=1 plus three isolated "
            "MySQL 8.0.41 endpoints and environment-only credentials"
        )
    if not os.environ.get("SELECT_FUZZ_MYSQL_USER") or os.environ.get(
        "SELECT_FUZZ_MYSQL_PASSWORD"
    ) is None:
        pytest.skip("environment-only MySQL credentials are not configured")
    nodes: list[NodeConfig] = []
    missing: list[str] = []
    for role in NodeRole:
        prefix = f"SELECT_FUZZ_{role.value.upper()}"
        host = os.environ.get(f"{prefix}_HOST")
        port = os.environ.get(f"{prefix}_PORT")
        if host is None or port is None:
            missing.extend((f"{prefix}_HOST", f"{prefix}_PORT"))
            continue
        nodes.append(NodeConfig(role=role, host=host, port=int(port)))
    if missing:
        pytest.skip("three-node integration endpoints are unset: " + ", ".join(missing))
    return AppConfig(
        mode="performance",
        nodes=tuple(nodes),
        performance=PerformanceConfig(
            workers=1,
            queries_per_round=1,
            initial_table_rows=100_000,
            max_table_rows=5_000_000,
            calibration_min_seconds=5,
            calibration_max_seconds=12,
            formal_timeout_seconds=15,
        ),
    )


@pytest.mark.mysql_performance
@pytest.mark.timeout(900)
def test_one_cpu_dense_case_on_exact_isolated_three_node_mysql_8_0_41(
    tmp_path: Path,
) -> None:
    config = _release_config()
    assert build_doctor(config).run().can_start
    request = RunRequest(
        run_id="run_mysql_8041_performance_gate",
        mode="performance",
        seed=8041,
        workers=1,
        rounds=1,
        queries_per_round=1,
    )

    summary = build_performance_runner(config, tmp_path).run(request, Event())

    assert summary.rounds_completed == 1
    assert summary.queries_completed == 1
    assert (tmp_path / "events.jsonl").exists()
    records = read_jsonl(tmp_path / "events.jsonl")
    result = next(
        item
        for item in records
        if item.get("type") in {"performance_result", "performance_alert"}
    )
    assert set(result["measurements"]) == {role.value for role in NodeRole}  # type: ignore[arg-type]
    assert all(
        len(attempt["samples_seconds"][role.value]) == 3  # type: ignore[index]
        for attempt in result["calibration"]  # type: ignore[union-attr]
        for role in (NodeRole.BASELINE, NodeRole.CUSTOM_OFF)
    )
