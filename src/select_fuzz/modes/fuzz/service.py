"""Concurrent per-database orchestration for the fuzz mode."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
import random
from threading import Event, Lock
from typing import Callable

from select_fuzz.artifacts import JsonlWriter
from select_fuzz.config import FuzzConfig, NodeConfig
from select_fuzz.domain import RunRequest, SeedTree
from select_fuzz.execution.protocols import ConnectionFactory, QuerySession
from select_fuzz.generation.query import QueryGenerationContext, WeightedQueryGenerator
from select_fuzz.generation.query_grammar import CandidateRejected
from select_fuzz.modes.fuzz.dml import FuzzDmlGenerator
from select_fuzz.modes.fuzz.execution import StreamingQueryExecutor
from select_fuzz.modes.fuzz.materialization import (
    FuzzDatabaseSchema,
    FuzzMaterializer,
    fuzz_database_name,
)
from select_fuzz.modes.fuzz.models import FuzzRowBudget
from select_fuzz.modes.fuzz.sql_log import FuzzSqlRecorder
from select_fuzz.service import RunSummary


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _execute_and_close(session: QuerySession, sql: str) -> None:
    cursor = session.execute(sql)
    try:
        while cursor.fetchmany(128):
            pass
    finally:
        cursor.close()


@dataclass(frozen=True, slots=True)
class FuzzCounterSnapshot:
    databases_ready: int
    reads: int
    writes: int
    errors: int
    reconnects: int


class FuzzCounters:
    def __init__(self) -> None:
        self._lock = Lock()
        self._values = {
            "databases_ready": 0,
            "reads": 0,
            "writes": 0,
            "errors": 0,
            "reconnects": 0,
        }

    def increment(self, name: str) -> None:
        with self._lock:
            self._values[name] += 1

    def snapshot(self) -> FuzzCounterSnapshot:
        with self._lock:
            return FuzzCounterSnapshot(**self._values)


class FuzzModeService:
    def __init__(
        self,
        *,
        config: FuzzConfig,
        primary: NodeConfig,
        replica: NodeConfig,
        factory: ConnectionFactory,
        records: JsonlWriter,
        query_generator: WeightedQueryGenerator,
        materializer_factory: Callable[[], FuzzMaterializer],
        sql_recorder: FuzzSqlRecorder | None = None,
    ) -> None:
        self._config = config
        self._primary = primary
        self._replica = replica
        self._factory = factory
        self._records = records
        self._queries = query_generator
        self._materializer_factory = materializer_factory
        self._sql_recorder = sql_recorder
        self._streaming = StreamingQueryExecutor(factory)
        self._counters = FuzzCounters()

    def run(self, request: RunRequest, stop_event: Event) -> RunSummary:
        if request.mode != "fuzz":
            raise ValueError("FuzzModeService requires fuzz mode")
        self._records.append(
            {
                "type": "fuzz_run_started",
                "run_id": request.run_id,
                "mode": request.mode,
                "occurred_at": _now(),
                "databases": self._config.databases,
                "writer_threads_per_database": (
                    self._config.writer_threads_per_database
                ),
                "reader_threads_per_database": (
                    self._config.reader_threads_per_database
                ),
                "seed": request.seed,
            }
        )
        with ThreadPoolExecutor(
            max_workers=self._config.databases,
            thread_name_prefix="sf-fuzz-database",
        ) as pool:
            futures = [
                pool.submit(
                    self._run_database,
                    request,
                    ordinal,
                    stop_event,
                )
                for ordinal in range(self._config.databases)
            ]
            for future in futures:
                try:
                    future.result()
                except Exception as error:
                    stop_event.set()
                    self._records.append(
                        {
                            "type": "fuzz_run_failed",
                            "run_id": request.run_id,
                            "mode": "fuzz",
                            "occurred_at": _now(),
                            "error_type": type(error).__name__,
                        }
                    )
                    raise
        counters = self._counters.snapshot()
        summary = RunSummary(
            run_id=request.run_id,
            rounds_completed=counters.databases_ready,
            queries_completed=counters.reads + counters.writes,
            findings=0,
            rejected=counters.errors,
            over_budget=0,
            stopped=stop_event.is_set(),
        )
        self._records.append(
            {
                "type": "fuzz_run_finished",
                "run_id": request.run_id,
                "mode": "fuzz",
                "occurred_at": _now(),
                "databases_ready": counters.databases_ready,
                "reads": counters.reads,
                "writes": counters.writes,
                "errors": counters.errors,
                "reconnects": counters.reconnects,
                "stopped": summary.stopped,
            }
        )
        return summary

    def _run_database(
        self,
        request: RunRequest,
        database_ordinal: int,
        stop_event: Event,
    ) -> None:
        database = fuzz_database_name(request.run_id, database_ordinal)
        seed = SeedTree(request.seed).derive("fuzz_database", database_ordinal)
        schema = self._materializer_factory().materialize(database, seed=seed)
        self._counters.increment("databases_ready")
        self._records.append(
            {
                "type": "fuzz_database_ready",
                "run_id": request.run_id,
                "mode": "fuzz",
                "occurred_at": _now(),
                "database": database,
                "database_ordinal": database_ordinal,
                "seed": seed,
            }
        )
        primary_readers = self._config.reader_threads_per_database // 3
        replica_readers = self._config.reader_threads_per_database - primary_readers
        row_budget = FuzzRowBudget(
            initial_rows=(
                self._config.initial_tables * self._config.initial_rows_per_table
            ),
            maximum_rows=self._config.max_rows_per_database,
        )
        workers = (
            self._config.writer_threads_per_database
            + primary_readers
            + replica_readers
        )
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"sf-fuzz-{database_ordinal}",
        ) as pool:
            futures = []
            for worker_id in range(self._config.writer_threads_per_database):
                futures.append(
                    pool.submit(
                        self._writer_loop,
                        request,
                        schema,
                        row_budget,
                        database_ordinal,
                        worker_id,
                        stop_event,
                    )
                )
            for reader_id in range(primary_readers):
                futures.append(
                    pool.submit(
                        self._reader_loop,
                        request,
                        schema,
                        database_ordinal,
                        reader_id,
                        self._primary,
                        "primary",
                        stop_event,
                    )
                )
            for reader_id in range(replica_readers):
                futures.append(
                    pool.submit(
                        self._reader_loop,
                        request,
                        schema,
                        database_ordinal,
                        primary_readers + reader_id,
                        self._replica,
                        "replica",
                        stop_event,
                    )
                )
            for future in futures:
                future.result()

    def _reader_loop(
        self,
        request: RunRequest,
        schema: FuzzDatabaseSchema,
        database_ordinal: int,
        worker_id: int,
        node: NodeConfig,
        endpoint: str,
        stop_event: Event,
    ) -> None:
        tree = SeedTree(request.seed)
        operation = 0
        delay = self._config.reconnect_initial_delay_seconds
        while not stop_event.is_set():
            try:
                with self._factory.query_session(node, schema.database) as session:
                    _execute_and_close(
                        session,
                        "SET SESSION max_execution_time = "
                        f"{int(self._config.query_timeout_seconds * 1000)}",
                    )
                    delay = self._config.reconnect_initial_delay_seconds
                    while not stop_event.is_set():
                        query_seed = tree.derive(
                            "fuzz_read",
                            database_ordinal,
                            worker_id,
                            operation,
                        )
                        operation += 1
                        try:
                            query = self._queries.generate(
                                QueryGenerationContext(
                                    schema.database,
                                    schema.grammar_schema,
                                ),
                                seed=query_seed,
                            )
                        except CandidateRejected as error:
                            self._record_error(
                                request,
                                schema.database,
                                "reader",
                                worker_id,
                                endpoint,
                                query_seed,
                                None,
                                type(error).__name__,
                            )
                            continue
                        self._record_query_sql(
                            schema.database,
                            f"reader-{endpoint}",
                            worker_id,
                            query.sql,
                        )
                        result = self._streaming.execute_session(session, query.sql)
                        if result.success:
                            self._counters.increment("reads")
                            continue
                        self._record_error(
                            request,
                            schema.database,
                            "reader",
                            worker_id,
                            endpoint,
                            query_seed,
                            query.sql,
                            result.error or "query_error",
                            connection_lost=result.connection_lost,
                        )
                        if result.connection_lost:
                            raise RuntimeError(result.error or "reader connection lost")
                        # A server-side SQL error (including a query timeout) does
                        # not by itself invalidate the long-lived read session.
                        # Keep it open so the next generated query reuses the same
                        # connection and continues to exercise the real workload.
                        continue
            except Exception as error:
                if stop_event.is_set():
                    break
                self._counters.increment("reconnects")
                self._record_reconnect(
                    request,
                    schema.database,
                    "reader",
                    worker_id,
                    endpoint,
                    error,
                    delay,
                )
                if stop_event.wait(delay):
                    break
                delay = min(self._config.reconnect_max_delay_seconds, delay * 2)

    def _writer_loop(
        self,
        request: RunRequest,
        schema: FuzzDatabaseSchema,
        row_budget: FuzzRowBudget,
        database_ordinal: int,
        worker_id: int,
        stop_event: Event,
    ) -> None:
        generator = FuzzDmlGenerator(
            schema.dml_tables,
            batch_rows_min=self._config.batch_rows_min,
            batch_rows_max=self._config.batch_rows_max,
            delete_batch_rows_min=self._config.delete_batch_rows_min,
            delete_batch_rows_max=self._config.delete_batch_rows_max,
        )
        tree = SeedTree(request.seed)
        transaction = 0
        delay = self._config.reconnect_initial_delay_seconds
        while not stop_event.is_set():
            try:
                with self._factory.query_session(
                    self._primary,
                    schema.database,
                ) as session:
                    _execute_and_close(session, "SET SESSION innodb_lock_wait_timeout = 10")
                    delay = self._config.reconnect_initial_delay_seconds
                    while not stop_event.is_set():
                        transaction_seed = tree.derive(
                            "fuzz_write",
                            database_ordinal,
                            worker_id,
                            transaction,
                        )
                        transaction += 1
                        rng = random.Random(transaction_seed)
                        _execute_and_close(session, "START TRANSACTION")
                        transaction_failed = False
                        transaction_connection_lost = False
                        try:
                            for statement_ordinal in range(rng.randint(1, 5)):
                                statement_seed = tree.derive(
                                    "fuzz_statement",
                                    transaction_seed,
                                    statement_ordinal,
                                )
                                statement = generator.generate(
                                    seed=statement_seed,
                                    known_high_watermark=(
                                        schema.initial_high_watermark
                                    ),
                                    insert_weight=self._config.insert_weight,
                                    update_weight=self._config.update_weight,
                                    delete_weight=self._config.delete_weight,
                                    upsert_weight=self._config.upsert_weight,
                                )
                                reserved_insert: int | None = None
                                if statement.operation == "insert":
                                    allowed = row_budget.reserve_insert(statement.target_rows)
                                    if allowed == 0:
                                        continue
                                    statement = statement.with_target_rows(allowed)
                                    reserved_insert = allowed
                                self._record_query_sql(
                                    schema.database,
                                    "writer",
                                    worker_id,
                                    statement.sql,
                                )
                                result = self._streaming.execute_session(
                                    session,
                                    statement.sql,
                                )
                                if reserved_insert is not None:
                                    row_budget.reconcile_insert(
                                        reserved_insert,
                                        result.affected_rows if result.success else 0,
                                    )
                                    reserved_insert = None
                                if not result.success:
                                    transaction_failed = True
                                    transaction_connection_lost = result.connection_lost
                                    self._record_error(
                                        request,
                                        schema.database,
                                        "writer",
                                        worker_id,
                                        "primary",
                                        statement_seed,
                                        statement.sql,
                                        result.error or "dml_error",
                                        connection_lost=result.connection_lost,
                                    )
                                    break
                                if result.success and statement.operation == "delete":
                                    row_budget.record_delete(result.affected_rows)
                            _execute_and_close(
                                session,
                                "ROLLBACK" if transaction_failed else "COMMIT",
                            )
                        except Exception:
                            try:
                                _execute_and_close(session, "ROLLBACK")
                            except Exception:
                                pass
                            raise
                        if transaction_failed:
                            if transaction_connection_lost:
                                raise RuntimeError(result.error or "writer connection lost")
                            # The failed statement was rolled back, but the
                            # connection remains reusable for the next transaction.
                            continue
                        self._counters.increment("writes")
            except Exception as error:
                if stop_event.is_set():
                    break
                self._counters.increment("reconnects")
                self._record_reconnect(
                    request,
                    schema.database,
                    "writer",
                    worker_id,
                    "primary",
                    error,
                    delay,
                )
                if stop_event.wait(delay):
                    break
                delay = min(self._config.reconnect_max_delay_seconds, delay * 2)

    def _record_query_sql(
        self,
        database: str,
        stream: str,
        worker_id: int,
        sql: str,
    ) -> None:
        if self._sql_recorder is not None:
            self._sql_recorder.record_query(database, stream, worker_id, sql)

    def _record_error(
        self,
        request: RunRequest,
        database: str,
        worker_kind: str,
        worker_id: int,
        endpoint: str,
        seed: int,
        sql: str | None,
        error: str,
        connection_lost: bool = False,
    ) -> None:
        self._counters.increment("errors")
        record: dict[str, object] = {
            "type": "fuzz_operation_error",
            "run_id": request.run_id,
            "mode": "fuzz",
            "occurred_at": _now(),
            "database": database,
            "worker_kind": worker_kind,
            "worker_id": worker_id,
            "endpoint": endpoint,
            "seed": seed,
            "error": error,
            "connection_lost": connection_lost,
        }
        if sql is not None:
            record["sql"] = sql
        self._records.append(record)

    def _record_reconnect(
        self,
        request: RunRequest,
        database: str,
        worker_kind: str,
        worker_id: int,
        endpoint: str,
        error: Exception,
        delay: float,
    ) -> None:
        self._records.append(
            {
                "type": "fuzz_worker_reconnect",
                "run_id": request.run_id,
                "mode": "fuzz",
                "occurred_at": _now(),
                "database": database,
                "worker_kind": worker_kind,
                "worker_id": worker_id,
                "endpoint": endpoint,
                "error_type": type(error).__name__,
                "error": str(error),
                "delay_seconds": delay,
            }
        )


__all__ = ["FuzzModeService"]
