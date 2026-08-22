from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from select_fuzz.config import (
    AppConfig,
    CorrectnessConfig,
    NodeConfig,
    NodeRole,
    NodeTopologyConfig,
    RunMode,
)
from select_fuzz.correctness import build_correctness_runner


def test_correctness_io_timeout_is_longer_than_query_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class _Factory:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("select_fuzz.correctness.MySQLConnectorFactory", _Factory)
    config = AppConfig(
        mode=RunMode.CORRECTNESS,
        nodes=cast(
            tuple[NodeTopologyConfig, ...],
            (
                NodeConfig(role=NodeRole.CUSTOM_OFF, host="127.0.0.1"),
                NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.2"),
            ),
        ),
        correctness=CorrectnessConfig(timeout_seconds=10),
    )

    build_correctness_runner(config, tmp_path)

    assert captured["read_timeout_s"] == 310
    assert captured["statement_timeout_ceiling_s"] == 10
