"""Lockstep three-primary mutation transactions and replica catch-up gating."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from enum import StrEnum
from threading import Barrier
import time
from typing import Callable, Protocol

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.domain import ErrorInfo, ExecutionStatus, NodeExecution
from select_fuzz.execution.protocols import ConnectionFactory, QuerySession
from select_fuzz.execution.replication import ReplicationWaitResult, marker_upsert_sql
from select_fuzz.execution.setup import validate_database_name
from select_fuzz.execution.triad import QueryLimits, QueryRunnerLike
from select_fuzz.generation.mutation import MutationBatch
from select_fuzz.oracle.errors import normalize_error


class MutationVerdict(StrEnum):
    COMMITTED = "committed"
    CONSISTENT_ERROR_ROLLED_BACK = "consistent_error_rolled_back"
    ACTUAL_ROWS_OUT_OF_RANGE_ROLLED_BACK = "actual_rows_out_of_range_rolled_back"
    MISMATCH = "mutation_mismatch"
    INFRASTRUCTURE_ERROR = "mutation_infrastructure_error"
    REPLICA_SYNC_TIMEOUT = "replica_sync_timeout"


@dataclass(frozen=True, slots=True)
class MutationBatchResult:
    verdict: MutationVerdict
    batch: MutationBatch
    executed_sql: tuple[str, ...]
    statement_results: tuple[Mapping[NodeRole, NodeExecution], ...]
    final_results: Mapping[NodeRole, NodeExecution]
    failing_sql: str | None = None
    replication_result: ReplicationWaitResult | None = None
    actual_affected_rows: int | None = None

    @property
    def committed(self) -> bool:
        return self.verdict is MutationVerdict.COMMITTED

    @property
    def terminates_round(self) -> bool:
        return self.verdict not in {
            MutationVerdict.COMMITTED,
            MutationVerdict.CONSISTENT_ERROR_ROLLED_BACK,
            MutationVerdict.ACTUAL_ROWS_OUT_OF_RANGE_ROLLED_BACK,
        }


class ReplicationWaiterLike(Protocol):
    def wait(self, database: str, sequence: int) -> ReplicationWaitResult: ...


def _failure(node: NodeConfig, message: str) -> NodeExecution:
    now = time.monotonic_ns()
    return NodeExecution.failure(
        role=node.role,
        status=ExecutionStatus.INFRA_ERROR,
        started_ns=now,
        ended_ns=now,
        connection_id=None,
        error=ErrorInfo(65012, "HY000", message),
        connection_reusable=False,
    )


def _same(results: Mapping[NodeRole, NodeExecution], *, compare_affected_rows: bool) -> bool:
    identities: set[object] = set()
    for role in NodeRole:
        result = results[role]
        if result.status is ExecutionStatus.SUCCESS:
            identities.add(
                (
                    result.status,
                    result.affected_rows if compare_affected_rows else None,
                )
            )
        else:
            identities.add(
                (
                    result.status,
                    None if result.error is None else normalize_error(result.error),
                )
            )
    return len(identities) == 1


class TriadMutationCoordinator:
    """Execute one batch as one transaction on all primaries in statement lockstep."""

    def __init__(
        self,
        primaries: Sequence[NodeConfig],
        *,
        factory: ConnectionFactory,
        runner: QueryRunnerLike,
        replication_waiter: ReplicationWaiterLike,
        limits: QueryLimits,
    ) -> None:
        by_role = {node.role: node for node in primaries}
        if len(primaries) != 3 or set(by_role) != set(NodeRole):
            raise ValueError("mutation coordinator requires one primary for every role")
        self._primaries = tuple(by_role[role] for role in NodeRole)
        self._factory = factory
        self._runner = runner
        self._replication_waiter = replication_waiter
        self._limits = limits

    def execute_batch(self, database: str, batch: MutationBatch) -> MutationBatchResult:
        return self._execute_batch(database, batch, on_statement=None)

    def execute_batch_logged(
        self,
        database: str,
        batch: MutationBatch,
        *,
        on_statement: Callable[[str], None],
    ) -> MutationBatchResult:
        return self._execute_batch(database, batch, on_statement=on_statement)

    def _execute_batch(
        self,
        database: str,
        batch: MutationBatch,
        *,
        on_statement: Callable[[str], None] | None,
    ) -> MutationBatchResult:
        database = validate_database_name(database)
        executed_sql: list[str] = []
        statement_results: list[Mapping[NodeRole, NodeExecution]] = []
        sessions: dict[NodeRole, QuerySession] = {}
        actual_affected_rows = 0

        def record_statement(sql: str) -> None:
            if on_statement is not None:
                on_statement(sql)
            executed_sql.append(sql)

        try:
            with ExitStack() as stack:
                for node in self._primaries:
                    sessions[node.role] = stack.enter_context(
                        self._factory.query_session(node, database)
                    )

                def execute(sql: str) -> dict[NodeRole, NodeExecution]:
                    barrier = Barrier(3)

                    def one(node: NodeConfig) -> NodeExecution:
                        try:
                            result = self._runner.run_session(
                                sessions[node.role],
                                node,
                                database,
                                sql,
                                timeout_s=self._limits.timeout_seconds,
                                row_limit=self._limits.row_limit,
                                byte_limit=self._limits.byte_limit,
                                barrier=barrier,
                            )
                            if result.role is not node.role:
                                raise ValueError("mutation role mismatch")
                            return result
                        except Exception as error:
                            barrier.abort()
                            return _failure(
                                node, f"mutation execution failed: {type(error).__name__}"
                            )

                    with ThreadPoolExecutor(
                        max_workers=3, thread_name_prefix="sf-mutation"
                    ) as pool:
                        futures = {node.role: pool.submit(one, node) for node in self._primaries}
                        return {role: futures[role].result() for role in NodeRole}

                def rollback() -> Mapping[NodeRole, NodeExecution]:
                    record_statement("ROLLBACK")
                    rolled_back = execute("ROLLBACK")
                    statement_results.append(rolled_back)
                    return rolled_back

                record_statement("START TRANSACTION")
                started = execute("START TRANSACTION")
                statement_results.append(started)
                if not _same(started, compare_affected_rows=False) or any(
                    result.status is not ExecutionStatus.SUCCESS for result in started.values()
                ):
                    rolled_back = rollback()
                    return MutationBatchResult(
                        MutationVerdict.MISMATCH,
                        batch,
                        tuple(executed_sql),
                        tuple(statement_results),
                        started,
                        "START TRANSACTION",
                    )

                for statement in batch.statements:
                    record_statement(statement.sql)
                    results = execute(statement.sql)
                    statement_results.append(results)
                    if _same(results, compare_affected_rows=True):
                        statuses = {result.status for result in results.values()}
                        if statuses == {ExecutionStatus.SUCCESS}:
                            affected_rows = results[NodeRole.BASELINE].affected_rows
                            if affected_rows is None:
                                rollback()
                                return MutationBatchResult(
                                    MutationVerdict.MISMATCH,
                                    batch,
                                    tuple(executed_sql),
                                    tuple(statement_results),
                                    results,
                                    statement.sql,
                                    actual_affected_rows=actual_affected_rows,
                                )
                            actual_affected_rows += affected_rows
                            continue
                        if statuses == {ExecutionStatus.ERROR}:
                            rolled_back = rollback()
                            verdict = (
                                MutationVerdict.CONSISTENT_ERROR_ROLLED_BACK
                                if _same(rolled_back, compare_affected_rows=False)
                                and all(
                                    result.status is ExecutionStatus.SUCCESS
                                    for result in rolled_back.values()
                                )
                                else MutationVerdict.MISMATCH
                            )
                            return MutationBatchResult(
                                verdict,
                                batch,
                                tuple(executed_sql),
                                tuple(statement_results),
                                results,
                                statement.sql,
                                actual_affected_rows=actual_affected_rows,
                            )
                    rollback()
                    return MutationBatchResult(
                        (
                            MutationVerdict.INFRASTRUCTURE_ERROR
                            if any(
                                result.status is ExecutionStatus.INFRA_ERROR
                                for result in results.values()
                            )
                            else MutationVerdict.MISMATCH
                        ),
                        batch,
                        tuple(executed_sql),
                        tuple(statement_results),
                        results,
                        statement.sql,
                        actual_affected_rows=actual_affected_rows,
                    )

                if not 12 <= actual_affected_rows <= 50:
                    rolled_back = rollback()
                    verdict = (
                        MutationVerdict.ACTUAL_ROWS_OUT_OF_RANGE_ROLLED_BACK
                        if _same(rolled_back, compare_affected_rows=False)
                        and all(
                            result.status is ExecutionStatus.SUCCESS
                            for result in rolled_back.values()
                        )
                        else MutationVerdict.MISMATCH
                    )
                    return MutationBatchResult(
                        verdict,
                        batch,
                        tuple(executed_sql),
                        tuple(statement_results),
                        rolled_back,
                        batch.statements[-1].sql,
                        actual_affected_rows=actual_affected_rows,
                    )

                marker_sql = marker_upsert_sql(batch.sequence)
                record_statement(marker_sql)
                marker_results = execute(marker_sql)
                statement_results.append(marker_results)
                if not _same(marker_results, compare_affected_rows=True) or any(
                    result.status is not ExecutionStatus.SUCCESS
                    for result in marker_results.values()
                ):
                    rollback()
                    return MutationBatchResult(
                        MutationVerdict.MISMATCH,
                        batch,
                        tuple(executed_sql),
                        tuple(statement_results),
                        marker_results,
                        marker_sql,
                        actual_affected_rows=actual_affected_rows,
                    )

                record_statement("COMMIT")
                committed = execute("COMMIT")
                statement_results.append(committed)
                if not _same(committed, compare_affected_rows=False) or any(
                    result.status is not ExecutionStatus.SUCCESS for result in committed.values()
                ):
                    return MutationBatchResult(
                        MutationVerdict.MISMATCH,
                        batch,
                        tuple(executed_sql),
                        tuple(statement_results),
                        committed,
                        "COMMIT",
                        actual_affected_rows=actual_affected_rows,
                    )
        except Exception as error:
            failures = {
                node.role: _failure(node, f"mutation session failed: {type(error).__name__}")
                for node in self._primaries
            }
            return MutationBatchResult(
                MutationVerdict.INFRASTRUCTURE_ERROR,
                batch,
                tuple(executed_sql),
                tuple(statement_results),
                failures,
                executed_sql[-1] if executed_sql else "START TRANSACTION",
                actual_affected_rows=actual_affected_rows,
            )

        replication = self._replication_waiter.wait(database, batch.sequence)
        if not replication.ready:
            return MutationBatchResult(
                MutationVerdict.REPLICA_SYNC_TIMEOUT,
                batch,
                tuple(executed_sql),
                tuple(statement_results),
                committed,
                marker_upsert_sql(batch.sequence),
                replication,
                actual_affected_rows,
            )
        return MutationBatchResult(
            MutationVerdict.COMMITTED,
            batch,
            tuple(executed_sql),
            tuple(statement_results),
            committed,
            replication_result=replication,
            actual_affected_rows=actual_affected_rows,
        )


__all__ = [
    "MutationBatchResult",
    "MutationVerdict",
    "TriadMutationCoordinator",
]
