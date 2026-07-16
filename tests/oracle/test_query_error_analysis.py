from __future__ import annotations

import pytest

from select_fuzz.config import NodeRole
from select_fuzz.domain import ErrorInfo, ExecutionStatus, NodeExecution
from select_fuzz.execution import INTERNAL_RESULT_LIMIT_ERRNO
from select_fuzz.generation.query_ast import ExpectedError, ExpectedErrorKind
from select_fuzz.oracle.query_errors import (
    QueryErrorDisposition,
    analyze_query_errors,
)


def error_triplet(errno: int, sqlstate: str, message: str) -> tuple[NodeExecution, ...]:
    return tuple(
        NodeExecution.failure(
            role=role,
            status=ExecutionStatus.ERROR,
            started_ns=1,
            ended_ns=2,
            connection_id=100 + ordinal,
            error=ErrorInfo(errno, sqlstate, message),
        )
        for ordinal, role in enumerate(NodeRole)
    )


def success_triplet() -> tuple[NodeExecution, ...]:
    return tuple(
        NodeExecution.success(
            role=role,
            started_ns=1,
            ended_ns=2,
            connection_id=100 + ordinal,
            columns=(),
            rows=(),
        )
        for ordinal, role in enumerate(NodeRole)
    )


def test_exact_expected_negative_error_is_accepted() -> None:
    expected = ExpectedError(ExpectedErrorKind.UNKNOWN_COLUMN, 1054, "42S22")

    analysis = analyze_query_errors(
        expected,
        error_triplet(1054, "42S22", "Unknown column 't.missing'"),
    )

    assert analysis.disposition is QueryErrorDisposition.EXPECTED_ERROR
    assert analysis.coverage_eligible is False
    assert analysis.reason == "all nodes returned the exact expected error identity"


def test_same_error_from_valid_sql_is_a_generator_defect_not_a_pass() -> None:
    analysis = analyze_query_errors(
        None,
        error_triplet(1064, "42000", "You have an error in your SQL syntax"),
    )

    assert analysis.disposition is QueryErrorDisposition.UNEXPECTED_VALID_ERROR
    assert analysis.coverage_eligible is False
    assert analysis.observed_identities == ((1064, "42000"),) * 3


def test_negative_error_identity_mismatch_is_reported() -> None:
    expected = ExpectedError(ExpectedErrorKind.UNKNOWN_COLUMN, 1054, "42S22")

    analysis = analyze_query_errors(
        expected,
        error_triplet(1064, "42000", "syntax error"),
    )

    assert analysis.disposition is QueryErrorDisposition.EXPECTED_ERROR_MISMATCH
    assert analysis.coverage_eligible is False
    assert "expected errno=1054 sqlstate=42S22" in analysis.reason


def test_negative_query_that_succeeds_is_reported() -> None:
    expected = ExpectedError(ExpectedErrorKind.INVALID_FUNCTION_ARITY, 1582, "42000")

    analysis = analyze_query_errors(expected, success_triplet())

    assert analysis.disposition is QueryErrorDisposition.EXPECTED_ERROR_MISMATCH
    assert "expected an error" in analysis.reason


def test_success_without_an_expected_error_is_accepted_for_coverage() -> None:
    analysis = analyze_query_errors(None, success_triplet())

    assert analysis.disposition is QueryErrorDisposition.SUCCESS
    assert analysis.coverage_eligible is True


def test_internal_result_limit_is_a_resource_outcome_not_a_valid_query_error() -> None:
    analysis = analyze_query_errors(
        None,
        error_triplet(
            INTERNAL_RESULT_LIMIT_ERRNO,
            "HY000",
            "result row limit exceeded",
        ),
    )

    assert analysis.disposition is QueryErrorDisposition.RESOURCE_LIMIT
    assert analysis.coverage_eligible is False


def test_mixed_timeout_and_result_limits_are_one_resource_outcome() -> None:
    executions = list(
        error_triplet(
            INTERNAL_RESULT_LIMIT_ERRNO,
            "HY000",
            "result row limit exceeded",
        )
    )
    executions[0] = NodeExecution.failure(
        role=NodeRole.BASELINE,
        status=ExecutionStatus.TIMEOUT,
        started_ns=1,
        ended_ns=2,
        connection_id=100,
        error=ErrorInfo(3024, "HY000", "query timeout"),
        connection_reusable=False,
    )

    analysis = analyze_query_errors(None, executions)

    assert analysis.disposition is QueryErrorDisposition.RESOURCE_LIMIT
    assert analysis.coverage_eligible is False


def test_error_analysis_rejects_an_untyped_expected_error_contract() -> None:
    with pytest.raises(TypeError, match="ExpectedError"):
        analyze_query_errors("unknown_column", success_triplet())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("factory", "error"),
    [
        (lambda: ExpectedError("unknown_column", 1054, "42S22"), TypeError),
        (
            lambda: ExpectedError(ExpectedErrorKind.UNKNOWN_COLUMN, True, "42S22"),
            ValueError,
        ),
        (
            lambda: ExpectedError(ExpectedErrorKind.UNKNOWN_COLUMN, -1, "42S22"),
            ValueError,
        ),
        (
            lambda: ExpectedError(ExpectedErrorKind.UNKNOWN_COLUMN, 65_536, "42S22"),
            ValueError,
        ),
        (
            lambda: ExpectedError(ExpectedErrorKind.UNKNOWN_COLUMN, 1054, "bad"),
            ValueError,
        ),
        (
            lambda: ExpectedError(ExpectedErrorKind.UNKNOWN_COLUMN, 1054, "42s22"),
            ValueError,
        ),
    ],
)
def test_expected_error_contract_rejects_untyped_or_inexact_identities(
    factory, error: type[Exception]  # type: ignore[no-untyped-def]
) -> None:
    with pytest.raises(error):
        factory()
