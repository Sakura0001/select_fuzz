"""Classify generated-query outcomes independently from differential comparison."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from select_fuzz.config import NodeRole
from select_fuzz.domain import ExecutionStatus, NodeExecution
from select_fuzz.execution import INTERNAL_RESULT_LIMIT_ERRNO
from select_fuzz.generation.query_ast import ExpectedError
from select_fuzz.oracle.errors import OracleInputError


ErrorIdentity: TypeAlias = tuple[int, str]


class QueryErrorDisposition(StrEnum):
    """Generator-level interpretation of a three-node execution outcome."""

    SUCCESS = "success"
    EXPECTED_ERROR = "expected_error"
    UNEXPECTED_VALID_ERROR = "unexpected_valid_error"
    EXPECTED_ERROR_MISMATCH = "expected_error_mismatch"
    RESOURCE_LIMIT = "resource_limit"
    DEFER_TO_ORACLE = "defer_to_oracle"


@dataclass(frozen=True, slots=True)
class QueryErrorAnalysis:
    """Result of validating valid/negative lane error expectations."""

    disposition: QueryErrorDisposition
    reason: str
    observed_identities: tuple[ErrorIdentity | None, ...]
    coverage_eligible: bool


def _ordered_executions(executions: Iterable[NodeExecution]) -> tuple[NodeExecution, ...]:
    values = tuple(executions)
    by_role = {execution.role: execution for execution in values}
    if len(values) != 3 or len(by_role) != 3 or set(by_role) != set(NodeRole):
        raise OracleInputError("error analysis requires exactly one execution per three-node role")
    ordered = tuple(by_role[role] for role in NodeRole)
    if any(execution.status is ExecutionStatus.INFRA_ERROR for execution in ordered):
        raise OracleInputError("infra_error executions must not enter error analysis")
    return ordered


def _identity(execution: NodeExecution) -> ErrorIdentity | None:
    if execution.status is not ExecutionStatus.ERROR:
        return None
    if execution.error is None:  # pragma: no cover - NodeExecution invariant
        raise OracleInputError("error execution lacks ErrorInfo")
    return execution.error.errno, execution.error.sqlstate


def analyze_query_errors(
    expected: ExpectedError | None,
    executions: Iterable[NodeExecution],
) -> QueryErrorAnalysis:
    """Validate generator expectations without treating matching SQL errors as passes."""

    if expected is not None and not isinstance(expected, ExpectedError):
        raise TypeError("expected must be an ExpectedError or None")
    ordered = _ordered_executions(executions)
    identities = tuple(_identity(execution) for execution in ordered)
    statuses = tuple(execution.status for execution in ordered)

    if all(
        status is ExecutionStatus.TIMEOUT
        or (identity is not None and identity[0] == INTERNAL_RESULT_LIMIT_ERRNO)
        for status, identity in zip(statuses, identities, strict=True)
    ):
        return QueryErrorAnalysis(
            disposition=QueryErrorDisposition.RESOURCE_LIMIT,
            reason="all nodes reached the configured execution resource limit",
            observed_identities=identities,
            coverage_eligible=False,
        )

    if expected is None:
        if all(status is ExecutionStatus.SUCCESS for status in statuses):
            return QueryErrorAnalysis(
                disposition=QueryErrorDisposition.SUCCESS,
                reason="all nodes executed the valid query successfully",
                observed_identities=identities,
                coverage_eligible=True,
            )
        if all(status is ExecutionStatus.ERROR for status in statuses):
            return QueryErrorAnalysis(
                disposition=QueryErrorDisposition.UNEXPECTED_VALID_ERROR,
                reason="a valid-lane query returned an error on every node",
                observed_identities=identities,
                coverage_eligible=False,
            )
        return QueryErrorAnalysis(
            disposition=QueryErrorDisposition.DEFER_TO_ORACLE,
            reason="mixed or timeout outcomes require differential-oracle handling",
            observed_identities=identities,
            coverage_eligible=False,
        )

    expected_identity = (expected.expected_errno, expected.expected_sqlstate)
    if all(identity == expected_identity for identity in identities):
        return QueryErrorAnalysis(
            disposition=QueryErrorDisposition.EXPECTED_ERROR,
            reason="all nodes returned the exact expected error identity",
            observed_identities=identities,
            coverage_eligible=False,
        )

    identity_text = (
        f"expected errno={expected.expected_errno} "
        f"sqlstate={expected.expected_sqlstate}"
    )
    if all(status is ExecutionStatus.SUCCESS for status in statuses):
        reason = f"{identity_text}; expected an error but all nodes succeeded"
    else:
        reason = f"{identity_text}; observed identities did not match on every node"
    return QueryErrorAnalysis(
        disposition=QueryErrorDisposition.EXPECTED_ERROR_MISMATCH,
        reason=reason,
        observed_identities=identities,
        coverage_eligible=False,
    )


__all__ = [
    "ErrorIdentity",
    "QueryErrorAnalysis",
    "QueryErrorDisposition",
    "analyze_query_errors",
]
