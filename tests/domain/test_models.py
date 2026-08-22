from __future__ import annotations

from types import MappingProxyType

import pytest

from select_fuzz.config import NodeRole
from select_fuzz.domain.models import (
    ColumnMeta,
    ErrorInfo,
    ExecutionStatus,
    NodeExecution,
    RunEvent,
    RunRequest,
)


def test_node_execution_success_records_typed_timing_and_immutable_payload() -> None:
    mutable_payload = {"tree": {"operators": ["scan"]}}
    result = NodeExecution.success(
        role=NodeRole.BASELINE,
        connection_id=12,
        started_ns=10,
        ended_ns=20,
        columns=(ColumnMeta("answer", 3, False, False, False),),
        rows=((42,),),
        warnings=("warning text",),
        performance_payload=mutable_payload,
    )
    mutable_payload["tree"]["operators"].append("mutated")  # type: ignore[index,union-attr]

    assert result.elapsed_ns == 10
    assert result.status is ExecutionStatus.SUCCESS
    assert result.rows == ((42,),)
    assert isinstance(result.performance_payload, MappingProxyType)
    assert result.performance_payload["tree"] == {"operators": ("scan",)}
    with pytest.raises(TypeError):
        result.performance_payload["tree"] = "changed"  # type: ignore[index]


def test_node_execution_freezes_failure_evidence() -> None:
    evidence = {"failure_stage": "connect", "exception": {"message": "reset"}}

    result = NodeExecution.failure(
        role=NodeRole.CUSTOM_OFF,
        status=ExecutionStatus.INFRA_ERROR,
        started_ns=1,
        ended_ns=2,
        connection_id=None,
        error=ErrorInfo(65002, "HY000", "查询会话建立失败：ConnectionError: reset"),
        failure_evidence=evidence,
        connection_reusable=False,
    )
    evidence["exception"]["message"] = "mutated"  # type: ignore[index]

    assert isinstance(result.failure_evidence, MappingProxyType)
    assert result.failure_evidence["exception"] == {"message": "reset"}


def test_execution_normalizes_sequences_and_freezes_nested_row_values() -> None:
    json_value = {"items": [1, 2]}
    result = NodeExecution.success(
        role=NodeRole.BASELINE,
        connection_id=12,
        started_ns=10,
        ended_ns=20,
        columns=[ColumnMeta("doc", 245, True, False, False)],  # type: ignore[arg-type]
        rows=[[json_value]],  # type: ignore[arg-type]
        warnings=["warning"],  # type: ignore[arg-type]
    )
    json_value["items"].append(3)

    assert isinstance(result.columns, tuple)
    assert isinstance(result.rows, tuple)
    assert result.rows[0][0] == {"items": (1, 2)}
    assert result.warnings == ("warning",)


def test_execution_payload_status_invariants() -> None:
    error = ErrorInfo(errno=1064, sqlstate="42000", message="syntax error")

    with pytest.raises(ValueError, match="ended_ns"):
        NodeExecution.success(
            role=NodeRole.BASELINE,
            connection_id=1,
            started_ns=20,
            ended_ns=10,
        )
    with pytest.raises(ValueError, match="error"):
        NodeExecution(
            role=NodeRole.BASELINE,
            status=ExecutionStatus.SUCCESS,
            started_ns=1,
            ended_ns=2,
            connection_id=1,
            error=error,
        )
    with pytest.raises(ValueError, match="rows"):
        NodeExecution.failure(
            role=NodeRole.BASELINE,
            status=ExecutionStatus.ERROR,
            started_ns=1,
            ended_ns=2,
            connection_id=1,
            error=error,
            rows=((1,),),
        )

    with pytest.raises(ValueError, match="row width"):
        NodeExecution.success(
            role=NodeRole.BASELINE,
            connection_id=1,
            started_ns=1,
            ended_ns=2,
            columns=(ColumnMeta("a", 3, False, False, False),),
            rows=((1, 2),),
        )


def test_error_info_and_column_metadata_validate_wire_contract() -> None:
    with pytest.raises(ValueError, match="sqlstate"):
        ErrorInfo(errno=1, sqlstate="bad", message="x")
    with pytest.raises(ValueError, match="errno"):
        ErrorInfo(errno=-1, sqlstate="HY000", message="x")
    with pytest.raises(ValueError, match="name"):
        ColumnMeta("", 3, True, False, False)

    for kwargs in (
        {"errno": True, "sqlstate": "HY000", "message": "x"},
        {"errno": 65_536, "sqlstate": "HY000", "message": "x"},
        {"errno": 1, "sqlstate": 42_000, "message": "x"},
        {"errno": 1, "sqlstate": "HY000", "message": b"x"},
    ):
        with pytest.raises((TypeError, ValueError)):
            ErrorInfo(**kwargs)  # type: ignore[arg-type]

    malformed_metadata = (
        ("x", True, False, False, False),
        ("x", 256, False, False, False),
        ("x", 3, 0, False, False),
        ("x", 3, False, 1, False),
        ("x", 3, False, False, 0),
    )
    for values in malformed_metadata:
        with pytest.raises((TypeError, ValueError)):
            ColumnMeta(*values)  # type: ignore[arg-type]

    metadata = ColumnMeta(
        "bits",
        16,
        False,
        True,
        True,
        character_set_id=63,
        column_length=8,
        decimals=0,
        flags=0x20,
    )
    assert metadata.column_length == 8

    invalidated = NodeExecution.failure(
        role=NodeRole.BASELINE,
        status=ExecutionStatus.INFRA_ERROR,
        started_ns=1,
        ended_ns=2,
        connection_id=7,
        error=ErrorInfo(2013, "HY000", "lost connection"),
        connection_reusable=False,
        watchdog_error_type="ReadTimeoutError",
    )
    assert invalidated.connection_reusable is False
    assert invalidated.watchdog_error_type == "ReadTimeoutError"

    for field, value in (
        ("character_set_id", -1),
        ("column_length", -1),
        ("decimals", 256),
        ("flags", 65_536),
    ):
        with pytest.raises(ValueError, match=field):
            ColumnMeta("x", 3, True, False, False, **{field: value})


def test_run_request_validates_but_does_not_supply_mode_defaults() -> None:
    request = RunRequest(
        run_id="run_1",
        mode="correctness",
        seed=7,
        workers=10,
        rounds=None,
        queries_per_round=1000,
    )
    assert request.rounds is None

    with pytest.raises(ValueError, match="one worker"):
        RunRequest(
            run_id="run_2",
            mode="performance",
            seed=7,
            workers=2,
            rounds=1,
            queries_per_round=100,
        )
    with pytest.raises(ValueError, match="queries_per_round"):
        RunRequest(
            run_id="run_3",
            mode="correctness",
            seed=7,
            workers=1,
            rounds=1,
            queries_per_round=0,
        )


def test_run_event_copies_payload_and_sequence_is_nonnegative() -> None:
    source = {"state": "running", "details": {"roles": ["baseline"]}}
    event = RunEvent(run_id="run_1", sequence=0, kind="state", payload=source)
    source["state"] = "mutated"
    source["details"]["roles"].append("custom_on")  # type: ignore[index,union-attr]

    assert event.payload == {
        "state": "running",
        "details": {"roles": ("baseline",)},
    }
    with pytest.raises(ValueError, match="sequence"):
        RunEvent(run_id="run_1", sequence=-1, kind="state", payload={})
