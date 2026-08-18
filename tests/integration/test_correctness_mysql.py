from __future__ import annotations

import os
from pathlib import Path
from threading import Event

import pytest

from select_fuzz.config import (
    COMPARISON_ROLES,
    AppConfig,
    CorrectnessConfig,
    NodeConfig,
)
from select_fuzz.correctness import build_correctness_runner
from select_fuzz.doctor import build_doctor
from select_fuzz.domain import RunRequest


def _release_config() -> AppConfig:
    if os.environ.get("SELECT_FUZZ_MYSQL_INTEGRATION") != "1":
        pytest.skip(
            "set SELECT_FUZZ_MYSQL_INTEGRATION=1 plus two MySQL 8.0.22 role "
            "endpoints and environment-only credentials"
        )
    if not os.environ.get("SELECT_FUZZ_MYSQL_USER") or os.environ.get(
        "SELECT_FUZZ_MYSQL_PASSWORD"
    ) is None:
        pytest.skip("environment-only MySQL credentials are not configured")
    nodes: list[NodeConfig] = []
    missing: list[str] = []
    for role in COMPARISON_ROLES:
        prefix = f"SELECT_FUZZ_{role.value.upper()}"
        host = os.environ.get(f"{prefix}_HOST")
        port = os.environ.get(f"{prefix}_PORT")
        if host is None or port is None:
            missing.extend((f"{prefix}_HOST", f"{prefix}_PORT"))
            continue
        nodes.append(NodeConfig(role=role, host=host, port=int(port)))
    if missing:
        pytest.skip("two-instance integration endpoints are unset: " + ", ".join(missing))
    return AppConfig(
        mode="correctness",
        nodes=tuple(nodes),
        correctness=CorrectnessConfig(
            workers=1,
            queries_per_round=1,
            min_rows_per_table=20,
            max_rows_per_table=20,
        ),
    )


@pytest.mark.mysql
@pytest.mark.timeout(180)
def test_one_generated_correctness_round_on_two_mysql_8_0_22_instances(
    tmp_path: Path,
) -> None:
    config = _release_config()
    report = build_doctor(config).run()
    assert report.can_start, report
    assert not any(issue.code == "mysql_version_mismatch" for issue in report.warnings)
    request = RunRequest(
        run_id="run_mysql_8022_pair_correctness",
        mode="correctness",
        seed=8022,
        workers=1,
        rounds=1,
        queries_per_round=1,
    )

    summary = build_correctness_runner(config, tmp_path).run(request, Event())

    assert summary.rounds_completed == 1
    assert summary.queries_completed == 1
    assert summary.findings == 0
    assert summary.rejected == 0
