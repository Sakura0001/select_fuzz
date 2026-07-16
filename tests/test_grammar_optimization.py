from __future__ import annotations

from pathlib import Path

import pytest

from select_fuzz.grammar_optimization import (
    FailureOwner,
    GrammarOptimizationConfig,
    _bounded_failure,
    classify_mysql_failure,
)
from select_fuzz.config import NodeRole
from select_fuzz.domain import ErrorInfo, ExecutionStatus, NodeExecution


@pytest.mark.parametrize(
    ("errno", "message", "owner"),
    (
        (1064, "You have an error in your SQL syntax", FailureOwner.GRAMMAR),
        (1054, "Unknown column 'r9.x'", FailureOwner.GENERATOR),
        (
            1247,
            "Reference 'q1' not supported (forward reference in item list)",
            FailureOwner.GENERATOR,
        ),
        (1191, "Can't find FULLTEXT index matching the column list", FailureOwner.METADATA),
        (3146, "Invalid data type for JSON data", FailureOwner.RANDOM_DATA),
        (2013, "Lost connection to MySQL server", FailureOwner.INFRASTRUCTURE),
    ),
)
def test_failure_classifier_assigns_actionable_owner(
    errno: int,
    message: str,
    owner: FailureOwner,
) -> None:
    classified = classify_mysql_failure(
        phase="explain",
        errno=errno,
        sqlstate="HY000",
        message=message,
    )

    assert classified.owner is owner


def test_classifier_names_lateral_transitive_outer_reference() -> None:
    classified = classify_mysql_failure(
        phase="explain",
        errno=1247,
        sqlstate="42S22",
        message="Reference 'q1' not supported (forward reference in item list)",
    )

    assert classified.category == "lateral_transitive_outer_reference"


@pytest.mark.parametrize(
    ("status", "errno", "expected"),
    (
        (ExecutionStatus.ERROR, 65_001, "bounded_runner_limit"),
        (ExecutionStatus.TIMEOUT, 65_003, "query_timeout"),
        (ExecutionStatus.INFRA_ERROR, 65_002, "connection_or_server_crash"),
    ),
)
def test_bounded_failure_distinguishes_limits_timeouts_and_server_crashes(
    status: ExecutionStatus,
    errno: int,
    expected: str,
) -> None:
    execution = NodeExecution(
        NodeRole.BASELINE,
        status,
        1,
        2,
        None,
        error=ErrorInfo(errno, "HY000", "diagnostic failure"),
    )

    *_, classification = _bounded_failure(execution, phase="explain")

    assert classification.category == expected


def test_optimization_config_uses_requested_ten_second_timeout(tmp_path: Path) -> None:
    config = GrammarOptimizationConfig(
        socket=tmp_path / "mysql.sock",
        grammar_path=tmp_path / "grammar.yy",
        artifact_root=tmp_path / "artifacts",
        query_timeout_seconds=10,
    )

    assert config.query_timeout_seconds == 10
    assert config.compatible_type_percent == 80
