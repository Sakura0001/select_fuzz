"""One-shot synchronized formal measurements using the shared query runner."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Protocol

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.domain import ExecutionStatus, NodeExecution
from select_fuzz.execution.protocols import BarrierLike
from select_fuzz.performance.diagnostics import DiagnosticsPort
from select_fuzz.performance.models import (
    FormalRun,
    FrozenCase,
    Measurement,
    Outcome,
    PerformancePolicy,
)
from select_fuzz.performance.tree import PlanParseError, ShapeBoundary, parse_tree


_TIMEOUT_ERRNOS = frozenset({3024})
_DISCONNECT_ERRNOS = frozenset({2006, 2013, 2055})


class QueryRunner(Protocol):
    def run(
        self,
        node: NodeConfig,
        database: str,
        sql: str,
        *,
        timeout_s: float,
        row_limit: int,
        byte_limit: int,
        barrier: BarrierLike | None = None,
    ) -> NodeExecution: ...


def classify_execution(execution: NodeExecution) -> Outcome:
    if execution.status is ExecutionStatus.SUCCESS:
        return Outcome.COMPLETED
    errno = None if execution.error is None else execution.error.errno
    if (
        execution.status is ExecutionStatus.TIMEOUT
        or errno in _TIMEOUT_ERRNOS
        or (errno == 1317 and execution.watchdog_fired)
    ):
        return Outcome.TIMEOUT
    if execution.status is ExecutionStatus.INFRA_ERROR or errno in _DISCONNECT_ERRNOS:
        return Outcome.INFRA_ERROR
    return Outcome.EXECUTION_ERROR


def _tree_payload(execution: NodeExecution) -> str | None:
    payload = execution.performance_payload
    if payload is not None:
        tree = payload.get("tree")
        if isinstance(tree, str):
            return tree
    if execution.rows and execution.rows[0] and isinstance(execution.rows[0][0], str):
        return execution.rows[0][0]
    return None


class FormalRunner:
    def __init__(
        self,
        nodes: Sequence[NodeConfig],
        core: QueryRunner,
        policy: PerformancePolicy,
        diagnostics: DiagnosticsPort | None = None,
    ) -> None:
        by_role = {node.role: node for node in nodes}
        if len(nodes) != 3 or set(by_role) != set(NodeRole):
            raise ValueError("formal runner requires one node for every fixed role")
        self._nodes = tuple(by_role[role] for role in NodeRole)
        self._core = core
        self._policy = policy
        self._diagnostics = diagnostics

    def run(self, frozen: FrozenCase) -> FormalRun:
        barrier = Barrier(3)
        explain_sql = f"EXPLAIN ANALYZE FORMAT=TREE {frozen.sql.rstrip().rstrip(';')}"

        def one(node: NodeConfig, start_barrier: BarrierLike | None) -> Measurement:
            diagnostic_before: object = None
            diagnostic_error: str | None = None
            if self._diagnostics is not None:
                try:
                    diagnostic_before = self._diagnostics.before(node, frozen.database)
                except Exception as error:
                    diagnostic_error = type(error).__name__
            try:
                raw = self._core.run(
                    node,
                    frozen.database,
                    explain_sql,
                    timeout_s=self._policy.formal_timeout_seconds,
                    row_limit=1,
                    byte_limit=32 * 1024 * 1024,
                    barrier=start_barrier,
                )
            except Exception as error:
                if start_barrier is not None:
                    barrier.abort()
                return Measurement(
                    role=node.role,
                    outcome=Outcome.INFRA_ERROR,
                    started_ns=0,
                    ended_ns=0,
                    connection_id=None,
                    root_end_ms=None,
                    tree=None,
                    cache_state=self._policy.cache_state,
                    error_type=type(error).__name__,
                )
            if raw.status is ExecutionStatus.INFRA_ERROR:
                # A lifecycle failure can return before NodeQueryRunner reaches the
                # shared barrier. Release peers immediately instead of waiting for
                # the full statement deadline.
                if start_barrier is not None:
                    barrier.abort()
            if raw.role is not node.role:
                if start_barrier is not None:
                    barrier.abort()
                return Measurement(
                    role=node.role,
                    outcome=Outcome.INFRA_ERROR,
                    started_ns=raw.started_ns,
                    ended_ns=raw.ended_ns,
                    connection_id=raw.connection_id,
                    root_end_ms=None,
                    tree=None,
                    cache_state=self._policy.cache_state,
                    error_type="RoleMismatch",
                )
            diagnostic_payload: dict[str, object] = {}
            if self._diagnostics is not None:
                try:
                    diagnostic_payload.update(
                        self._diagnostics.after(
                            node, frozen.database, raw.connection_id, diagnostic_before
                        )
                    )
                except Exception as error:
                    diagnostic_error = type(error).__name__
            if diagnostic_error is not None:
                diagnostic_payload["diagnostics_error"] = diagnostic_error
            return self.measure(raw, frozen, self._policy, diagnostic_payload=diagnostic_payload)

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="sf-perf-formal") as pool:
            futures = {node.role: pool.submit(one, node, barrier) for node in self._nodes}
            measurements = {role: futures[role].result() for role in futures}
        starts = [measurements[role].started_ns for role in NodeRole]
        return FormalRun(
            measurements=measurements,
            start_skew_ms=(max(starts) - min(starts)) / 1_000_000,
        )

    @staticmethod
    def measure(
        raw: NodeExecution,
        frozen: FrozenCase,
        policy: PerformancePolicy,
        *,
        diagnostic_payload: Mapping[str, object] | None = None,
    ) -> Measurement:
        outcome = classify_execution(raw)
        tree = _tree_payload(raw)
        root_end_ms: float | None = None
        error_type = raw.watchdog_error_type
        if error_type is None:
            error_type = {
                Outcome.TIMEOUT: "MySQLTimeout",
                Outcome.INFRA_ERROR: "MySQLInfrastructureError",
                Outcome.EXECUTION_ERROR: "MySQLError",
            }.get(outcome)
        if outcome is Outcome.COMPLETED:
            if tree is None:
                outcome = Outcome.PARSE_ERROR
                error_type = "MissingTreePayload"
            else:
                try:
                    plan = parse_tree(tree, completed=True)
                except PlanParseError:
                    outcome = Outcome.PARSE_ERROR
                    error_type = "PlanParseError"
                else:
                    boundary = frozen.boundary
                    if not isinstance(boundary, ShapeBoundary):
                        outcome = Outcome.SHAPE_MISMATCH
                        error_type = "InvalidShapeBoundary"
                    else:
                        try:
                            boundary.validate(plan, raw.role.value)
                        except PlanParseError:
                            outcome = Outcome.SHAPE_MISMATCH
                            error_type = "ShapeMismatch"
                        else:
                            root_end_ms = plan.root.end_ms
        payload = dict(raw.performance_payload or {})
        if diagnostic_payload:
            payload.update(diagnostic_payload)
        return Measurement(
            role=raw.role,
            outcome=outcome,
            started_ns=raw.started_ns,
            ended_ns=raw.ended_ns,
            connection_id=raw.connection_id,
            root_end_ms=root_end_ms,
            tree=tree,
            cache_state=policy.cache_state,
            wall_time_ms=raw.elapsed_ns / 1_000_000,
            metrics=payload,
            error_code=None if raw.error is None else raw.error.errno,
            watchdog_fired=raw.watchdog_fired,
            error_type=error_type,
        )


__all__ = ["FormalRunner", "QueryRunner", "classify_execution"]
