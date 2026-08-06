"""Concurrent per-database orchestration for the fuzz mode."""

from __future__ import annotations

from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
import random
from threading import Event, Lock, Thread
import time
from typing import Callable

from select_fuzz.artifacts import JsonlWriter
from select_fuzz.config import FuzzConfig, NodeConfig
from select_fuzz.domain import RunRequest, SeedTree
from select_fuzz.execution.protocols import ConnectionFactory, QuerySession, StopEventLike
from select_fuzz.execution.timeout import KillQueryWatchdog
from select_fuzz.generation.query import GeneratedQuery, WeightedQueryGenerator
from select_fuzz.generation.query_grammar import CandidateRejected
from select_fuzz.modes.fuzz.dml import FuzzDmlGenerator
from select_fuzz.modes.fuzz.execution import StreamingQueryExecutor
from select_fuzz.modes.fuzz.materialization import (
    FuzzDatabaseSchema,
    FuzzMaterializer,
    fuzz_database_name,
)
from select_fuzz.modes.fuzz.models import FuzzRowBudget
from select_fuzz.modes.fuzz.query_pipeline import (
    GenerationTicket,
    InlineQueryPipeline,
    QueryGenerationPipeline,
    QueryGenerationProcessDied,
    QueryGenerationStopped,
)
from select_fuzz.modes.fuzz.sql_log import FuzzSqlRecorder
from select_fuzz.modes.fuzz.telemetry import FuzzStageTelemetry
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


def _tag_worker_session(
    session: QuerySession,
    *,
    worker_kind: str,
    endpoint: str,
) -> None:
    tag = {
        ("writer", "primary"): "primary_writer",
        ("reader", "primary"): "primary_reader",
        ("reader", "replica"): "replica_reader",
    }.get((worker_kind, endpoint))
    if tag is None:
        raise ValueError("unsupported fuzz worker session tag")
    _execute_and_close(session, f"SET @select_fuzz_worker = '{tag}'")


@dataclass(frozen=True, slots=True)
class FuzzCounterSnapshot:
    databases_ready: int
    generations_ready: int
    reads: int
    writes: int
    errors: int
    reconnects: int


class FuzzCounters:
    def __init__(self) -> None:
        self._lock = Lock()
        self._values = {
            "databases_ready": 0,
            "generations_ready": 0,
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


@dataclass(frozen=True, slots=True)
class _GenerationDatabase:
    ordinal: int
    database: str
    seed: int
    schema: FuzzDatabaseSchema


@dataclass(frozen=True, slots=True)
class _PreparedReaderQuery:
    query: GeneratedQuery
    seed: int
    next_operation: int


_GenerationFailure = tuple[int, str, str, str]


class _GenerationBuildError(RuntimeError):
    def __init__(self, failures: tuple[_GenerationFailure, ...]) -> None:
        self.failures = failures
        rendered = ", ".join(
            f"database[{ordinal}]={database} {error_type}: {error_message}"
            for ordinal, database, error_type, error_message in failures
        )
        super().__init__(f"fuzz generation build failed: {rendered}")


class _GenerationStop:
    """Event-compatible signal set by either the whole run or one refresh."""

    def __init__(self, run_stop: StopEventLike) -> None:
        self._run_stop = run_stop
        self._generation_stop = Event()

    def is_set(self) -> bool:
        return self._run_stop.is_set() or self._generation_stop.is_set()

    def set(self) -> None:
        self._generation_stop.set()

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            wait_seconds = 0.05
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self.is_set()
                wait_seconds = min(wait_seconds, remaining)
            self._generation_stop.wait(wait_seconds)
        return True


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
        query_pipeline_factory: Callable[[], QueryGenerationPipeline] | None = None,
        sql_recorder: FuzzSqlRecorder | None = None,
        connector_implementation: str = "python",
    ) -> None:
        self._config = config
        self._primary = primary
        self._replica = replica
        self._factory = factory
        self._records = records
        self._queries = query_generator
        self._query_pipeline_factory = query_pipeline_factory or (
            lambda: InlineQueryPipeline(self._queries)
        )
        self._query_pipeline: QueryGenerationPipeline | None = None
        self._materializer_factory = materializer_factory
        self._sql_recorder = sql_recorder
        self._streaming = StreamingQueryExecutor(
            factory,
            watchdog=KillQueryWatchdog(
                factory,
                kill_grace_s=config.query_kill_grace_seconds,
            ),
        )
        self._counters = FuzzCounters()
        self._connector_implementation = connector_implementation
        self._telemetry = FuzzStageTelemetry()
        self._prepared_reader_queries: dict[
            tuple[str, int], _PreparedReaderQuery
        ] = {}
        self._prepared_reader_queries_lock = Lock()

    def run(self, request: RunRequest, stop_event: Event) -> RunSummary:
        if request.mode != "fuzz":
            raise ValueError("FuzzModeService requires fuzz mode")
        pipeline = self._query_pipeline_factory()
        pipeline.start()
        self._query_pipeline = pipeline
        monitor_done = Event()
        stop_monitor = Thread(
            target=self._monitor_stop,
            args=(stop_event, monitor_done),
            name="sf-fuzz-stop-monitor",
            daemon=True,
        )
        stop_monitor.start()
        telemetry_done = Event()
        telemetry_monitor = Thread(
            target=self._monitor_telemetry,
            args=(request, telemetry_done),
            name="sf-fuzz-telemetry",
            daemon=True,
        )
        telemetry_monitor.start()
        try:
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
                    "schema_refresh_interval_seconds": (
                        self._config.schema_refresh_interval_seconds
                    ),
                    "connector_implementation": self._connector_implementation,
                    "seed": request.seed,
                }
            )
            generation = 0
            try:
                while not stop_event.is_set():
                    generation_started = time.monotonic()
                    self._records.append(
                        {
                            "type": "fuzz_generation_started",
                            "run_id": request.run_id,
                            "mode": "fuzz",
                            "occurred_at": _now(),
                            "generation": generation,
                            "databases": self._config.databases,
                        }
                    )
                    schemas = self._materialize_generation(
                        request,
                        generation,
                    )
                    if stop_event.is_set():
                        self._records.append(
                            {
                                "type": "fuzz_generation_stopped",
                                "run_id": request.run_id,
                                "mode": "fuzz",
                                "occurred_at": _now(),
                                "generation": generation,
                                "phase": "materialization",
                                "reason": "run_stop",
                            }
                        )
                        break
                    try:
                        self._register_generation(generation, schemas)
                        prewarmed = self._prewarm_generation(
                            request,
                            schemas,
                            stop_event,
                        )
                    except Exception as error:
                        self._records.append(
                            {
                                "type": "fuzz_generation_failed",
                                "run_id": request.run_id,
                                "mode": "fuzz",
                                "occurred_at": _now(),
                                "generation": generation,
                                "phase": "query_prewarm",
                                "error_type": type(error).__name__,
                            }
                        )
                        raise
                    if not prewarmed:
                        self._records.append(
                            {
                                "type": "fuzz_generation_stopped",
                                "run_id": request.run_id,
                                "mode": "fuzz",
                                "occurred_at": _now(),
                                "generation": generation,
                                "phase": "query_prewarm",
                                "reason": "run_stop",
                            }
                        )
                        break
                    for built in schemas:
                        self._counters.increment("databases_ready")
                        self._records.append(
                            {
                                "type": "fuzz_database_ready",
                                "run_id": request.run_id,
                                "mode": "fuzz",
                                "occurred_at": _now(),
                                "generation": generation,
                                "database": built.database,
                                "database_ordinal": built.ordinal,
                                "seed": built.seed,
                            }
                        )
                    self._counters.increment("generations_ready")
                    build_elapsed_seconds = max(
                        0.0,
                        time.monotonic() - generation_started,
                    )
                    self._records.append(
                        {
                            "type": "fuzz_generation_ready",
                            "run_id": request.run_id,
                            "mode": "fuzz",
                            "occurred_at": _now(),
                            "generation": generation,
                            "databases": [built.database for built in schemas],
                            "build_elapsed_seconds": build_elapsed_seconds,
                        }
                    )
                    deadline = None
                    if self._config.schema_refresh_interval_seconds > 0:
                        deadline = (
                            generation_started
                            + self._config.schema_refresh_interval_seconds
                        )
                        if time.monotonic() >= deadline:
                            self._records.append(
                                {
                                    "type": "fuzz_generation_refresh_overdue",
                                    "run_id": request.run_id,
                                    "mode": "fuzz",
                                    "occurred_at": _now(),
                                    "generation": generation,
                                    "build_elapsed_seconds": build_elapsed_seconds,
                                    "refresh_interval_seconds": (
                                        self._config.schema_refresh_interval_seconds
                                    ),
                                }
                            )
                    reason = self._run_generation_workers(
                        request,
                        generation,
                        schemas,
                        stop_event,
                        deadline=deadline,
                    )
                    if reason != "refresh":
                        break
                    generation += 1
            except Exception as error:
                self._records.append(
                    {
                        "type": "fuzz_run_failed",
                        "run_id": request.run_id,
                        "mode": "fuzz",
                        "occurred_at": _now(),
                        "error_type": type(error).__name__,
                    }
                )
                self._append_stage_snapshot(request, final=True)
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
            self._append_stage_snapshot(request, final=True)
            self._records.append(
                {
                    "type": "fuzz_run_finished",
                    "run_id": request.run_id,
                    "mode": "fuzz",
                    "occurred_at": _now(),
                    "databases_ready": counters.databases_ready,
                    "generations_ready": counters.generations_ready,
                    "reads": counters.reads,
                    "writes": counters.writes,
                    "errors": counters.errors,
                    "reconnects": counters.reconnects,
                    "stopped": summary.stopped,
                }
            )
            return summary
        finally:
            if stop_event.is_set():
                self._streaming.stop_active()
            monitor_done.set()
            stop_monitor.join()
            telemetry_done.set()
            telemetry_monitor.join()
            pipeline.close()
            self._query_pipeline = None

    def _monitor_stop(self, stop_event: Event, monitor_done: Event) -> None:
        while not monitor_done.wait(0.05):
            if stop_event.is_set():
                self._streaming.stop_active()
                return

    def _monitor_telemetry(self, request: RunRequest, done: Event) -> None:
        while not done.wait(5.0):
            self._append_stage_snapshot(request, final=False)

    def _append_stage_snapshot(self, request: RunRequest, *, final: bool) -> None:
        snapshot = self._telemetry.snapshot()
        self._records.append(
            {
                "type": "fuzz_stage_snapshot",
                "run_id": request.run_id,
                "mode": "fuzz",
                "occurred_at": _now(),
                "final": final,
                **snapshot,
            }
        )

    def _materialize_generation(
        self,
        request: RunRequest,
        generation: int,
    ) -> tuple[_GenerationDatabase, ...]:
        futures: list[tuple[int, str, int, Future[FuzzDatabaseSchema]]] = []
        with ThreadPoolExecutor(
            max_workers=self._config.databases,
            thread_name_prefix=f"sf-fuzz-build-g{generation}",
        ) as pool:
            for database_ordinal in range(self._config.databases):
                database = fuzz_database_name(
                    request.run_id,
                    database_ordinal,
                    generation=generation,
                )
                seed = SeedTree(request.seed).derive(
                    "fuzz_database",
                    generation,
                    database_ordinal,
                )
                futures.append(
                    (
                        database_ordinal,
                        database,
                        seed,
                        pool.submit(
                            self._materializer_factory().materialize,
                            database,
                            seed=seed,
                        ),
                    )
                )
        failures: list[_GenerationFailure] = []
        schemas: list[_GenerationDatabase] = []
        for database_ordinal, database, seed, future in futures:
            try:
                schema = future.result()
            except Exception as error:
                failures.append(
                    (
                        database_ordinal,
                        database,
                        type(error).__name__,
                        str(error),
                    )
                )
                continue
            schemas.append(
                _GenerationDatabase(database_ordinal, database, seed, schema)
            )
        if failures:
            self._records.append(
                {
                    "type": "fuzz_generation_failed",
                    "run_id": request.run_id,
                    "mode": "fuzz",
                    "occurred_at": _now(),
                    "generation": generation,
                    "phase": "materialization",
                    "failures": [
                        {
                            "database_ordinal": ordinal,
                            "database": database,
                            "error_type": error_type,
                            "error": error_message,
                        }
                        for ordinal, database, error_type, error_message in failures
                    ],
                }
            )
            raise _GenerationBuildError(tuple(failures))
        return tuple(sorted(schemas, key=lambda built: built.ordinal))

    def _register_generation(
        self,
        generation: int,
        schemas: tuple[_GenerationDatabase, ...],
    ) -> None:
        pipeline = self._query_pipeline
        if pipeline is None:
            raise RuntimeError("query generation pipeline is unavailable")
        for built in schemas:
            if generation == 0:
                pipeline.register_database(
                    built.ordinal,
                    built.schema.database,
                    built.schema.grammar_schema,
                )
            else:
                pipeline.replace_database(
                    built.ordinal,
                    built.schema.database,
                    built.schema.grammar_schema,
                )

    def _prewarm_generation(
        self,
        request: RunRequest,
        schemas: tuple[_GenerationDatabase, ...],
        stop_event: StopEventLike,
    ) -> bool:
        pipeline = self._query_pipeline
        if pipeline is None:
            raise RuntimeError("query generation pipeline is unavailable")
        tree = SeedTree(request.seed)
        prepared: dict[tuple[str, int], _PreparedReaderQuery] = {}
        pending = [
            (built, reader_id, 0)
            for built in schemas
            for reader_id in range(self._config.reader_threads_per_database)
        ]
        while pending:
            submitted: list[
                tuple[_GenerationDatabase, int, int, int, GenerationTicket]
            ] = []
            for built, reader_id, operation in pending:
                if stop_event.is_set():
                    return False
                seed = tree.derive(
                    "fuzz_read",
                    built.schema.database,
                    reader_id,
                    operation,
                )
                submitted.append(
                    (
                        built,
                        reader_id,
                        operation,
                        seed,
                        pipeline.submit(
                            built.ordinal,
                            reader_id,
                            operation,
                            seed=seed,
                        ),
                    )
                )
            pending = []
            for built, reader_id, operation, seed, ticket in submitted:
                try:
                    outcome = ticket.result(stop_event)
                except QueryGenerationStopped:
                    if stop_event.is_set():
                        return False
                    raise
                self._telemetry.observe("generation_compute_ns", outcome.compute_ns)
                self._telemetry.observe("generation_wait_ns", outcome.wait_ns)
                if outcome.query is None:
                    next_operation = operation + 1
                    if next_operation >= 100:
                        raise RuntimeError(
                            "failed to pre-generate a reader query after 100 attempts"
                        )
                    pending.append((built, reader_id, next_operation))
                    continue
                prepared[(built.schema.database, reader_id)] = _PreparedReaderQuery(
                    outcome.query,
                    seed,
                    operation + 1,
                )
        with self._prepared_reader_queries_lock:
            self._prepared_reader_queries.update(prepared)
        return True

    def _run_generation_workers(
        self,
        request: RunRequest,
        generation: int,
        schemas: tuple[_GenerationDatabase, ...],
        stop_event: StopEventLike,
        *,
        deadline: float | None,
    ) -> str:
        generation_stop = _GenerationStop(stop_event)
        primary_readers = self._config.reader_threads_per_database // 3
        replica_readers = self._config.reader_threads_per_database - primary_readers
        workers_per_database = (
            self._config.writer_threads_per_database
            + self._config.reader_threads_per_database
        )
        worker_futures: list[Future[None]] = []
        with ThreadPoolExecutor(
            max_workers=workers_per_database * len(schemas),
            thread_name_prefix=f"sf-fuzz-g{generation}",
        ) as pool:
            for built in schemas:
                row_budget = FuzzRowBudget(
                    initial_rows=(
                        self._config.initial_tables
                        * self._config.initial_rows_per_table
                    ),
                    maximum_rows=self._config.max_rows_per_database,
                )
                for worker_id in range(self._config.writer_threads_per_database):
                    worker_futures.append(
                        pool.submit(
                            self._tracked_writer_loop,
                            request,
                            built.schema,
                            row_budget,
                            built.ordinal,
                            worker_id,
                            generation_stop,
                        )
                    )
                for reader_id in range(primary_readers):
                    worker_futures.append(
                        pool.submit(
                            self._tracked_reader_loop,
                            request,
                            built.schema,
                            built.ordinal,
                            reader_id,
                            self._primary,
                            "primary",
                            generation_stop,
                        )
                    )
                for reader_id in range(replica_readers):
                    worker_futures.append(
                        pool.submit(
                            self._tracked_reader_loop,
                            request,
                            built.schema,
                            built.ordinal,
                            primary_readers + reader_id,
                            self._replica,
                            "replica",
                            generation_stop,
                        )
                    )
            timeout = None
            if deadline is not None:
                timeout = max(0.0, deadline - time.monotonic())
            done, pending = wait(
                worker_futures,
                timeout=timeout,
                return_when=FIRST_EXCEPTION,
            )
            failures = [future for future in done if future.exception() is not None]
            if failures:
                reason = "worker_error"
            elif stop_event.is_set():
                reason = "run_stop"
            elif pending:
                reason = "refresh"
            else:
                reason = "workers_completed"
            self._records.append(
                {
                    "type": "fuzz_generation_stopping",
                    "run_id": request.run_id,
                    "mode": "fuzz",
                    "occurred_at": _now(),
                    "generation": generation,
                    "reason": reason,
                }
            )
            generation_stop.set()
            if failures:
                self._streaming.stop_active()
            for future in worker_futures:
                try:
                    future.result()
                except Exception:
                    if future not in failures:
                        failures.append(future)
            if failures:
                first_error = failures[0].exception()
                assert first_error is not None
                raise first_error
        with self._prepared_reader_queries_lock:
            for built in schemas:
                for reader_id in range(self._config.reader_threads_per_database):
                    self._prepared_reader_queries.pop(
                        (built.schema.database, reader_id),
                        None,
                    )
        self._records.append(
            {
                "type": "fuzz_generation_stopped",
                "run_id": request.run_id,
                "mode": "fuzz",
                "occurred_at": _now(),
                "generation": generation,
                "phase": "workers",
                "reason": reason,
            }
        )
        return reason

    @staticmethod
    def _worker_key(
        database_ordinal: int,
        worker_kind: str,
        endpoint: str,
        worker_id: int,
    ) -> str:
        return f"db{database_ordinal}:{worker_kind}-{endpoint}:{worker_id}"

    def _tracked_reader_loop(
        self,
        request: RunRequest,
        schema: FuzzDatabaseSchema,
        database_ordinal: int,
        worker_id: int,
        node: NodeConfig,
        endpoint: str,
        stop_event: StopEventLike,
    ) -> None:
        key = self._worker_key(database_ordinal, "reader", endpoint, worker_id)
        self._telemetry.set_stage(key, "starting")
        try:
            self._reader_loop(
                request,
                schema,
                database_ordinal,
                worker_id,
                node,
                endpoint,
                stop_event,
            )
        finally:
            pipeline = self._query_pipeline
            if pipeline is not None:
                pipeline.cancel_reader(database_ordinal, worker_id)
            self._telemetry.remove_worker(key)

    def _tracked_writer_loop(
        self,
        request: RunRequest,
        schema: FuzzDatabaseSchema,
        row_budget: FuzzRowBudget,
        database_ordinal: int,
        worker_id: int,
        stop_event: StopEventLike,
    ) -> None:
        key = self._worker_key(database_ordinal, "writer", "primary", worker_id)
        self._telemetry.set_stage(key, "starting")
        try:
            self._writer_loop(
                request,
                schema,
                row_budget,
                database_ordinal,
                worker_id,
                stop_event,
            )
        finally:
            self._telemetry.remove_worker(key)

    def _reader_loop(
        self,
        request: RunRequest,
        schema: FuzzDatabaseSchema,
        database_ordinal: int,
        worker_id: int,
        node: NodeConfig,
        endpoint: str,
        stop_event: StopEventLike,
    ) -> None:
        tree = SeedTree(request.seed)
        with self._prepared_reader_queries_lock:
            prepared = self._prepared_reader_queries.pop(
                (schema.database, worker_id),
                None,
            )
        operation = 0 if prepared is None else prepared.next_operation
        delay = self._config.reconnect_initial_delay_seconds
        worker_key = self._worker_key(
            database_ordinal,
            "reader",
            endpoint,
            worker_id,
        )
        pipeline = self._query_pipeline
        if pipeline is None:
            raise RuntimeError("query generation pipeline is unavailable")

        def submit_next() -> tuple[GenerationTicket, int]:
            nonlocal operation
            query_operation = operation
            query_seed = tree.derive(
                "fuzz_read",
                schema.database,
                worker_id,
                query_operation,
            )
            operation += 1
            return (
                pipeline.submit(
                    database_ordinal,
                    worker_id,
                    query_operation,
                    seed=query_seed,
                ),
                query_seed,
            )

        current_ticket: GenerationTicket | None
        current_query: GeneratedQuery | None
        if prepared is None:
            current_ticket, current_seed = submit_next()
            current_query = None
        else:
            current_ticket = None
            current_seed = prepared.seed
            current_query = prepared.query
        while not stop_event.is_set():
            try:
                self._telemetry.set_stage(worker_key, "connecting")
                with self._factory.query_session(node, schema.database) as session:
                    _tag_worker_session(
                        session,
                        worker_kind="reader",
                        endpoint=endpoint,
                    )
                    _execute_and_close(
                        session,
                        "SET SESSION max_execution_time = "
                        f"{int(self._config.query_timeout_seconds * 1000)}",
                    )
                    delay = self._config.reconnect_initial_delay_seconds
                    while not stop_event.is_set():
                        self._telemetry.set_stage(
                            worker_key,
                            "waiting_for_generated_sql",
                        )
                        if current_query is None:
                            assert current_ticket is not None
                            outcome = current_ticket.result(stop_event)
                            current_ticket = None
                            self._telemetry.observe(
                                "generation_compute_ns",
                                outcome.compute_ns,
                            )
                            self._telemetry.observe(
                                "generation_wait_ns",
                                outcome.wait_ns,
                            )
                            if outcome.query is None:
                                self._record_error(
                                    request,
                                    schema.database,
                                    "reader",
                                    worker_id,
                                    endpoint,
                                    current_seed,
                                    None,
                                    outcome.error_type or CandidateRejected.__name__,
                                )
                                current_ticket, current_seed = submit_next()
                                continue
                            query = outcome.query
                        else:
                            query = current_query
                            current_query = None
                        next_ticket, next_seed = submit_next()
                        self._record_query_sql(
                            schema.database,
                            f"reader-{endpoint}",
                            worker_id,
                            query.sql,
                        )
                        result = self._streaming.execute_session(
                            session,
                            query.sql,
                            node=node,
                            database=schema.database,
                            timeout_seconds=self._config.query_timeout_seconds,
                            on_stage=lambda stage: self._telemetry.set_stage(
                                worker_key,
                                f"reader_{stage}",
                            ),
                        )
                        self._telemetry.observe(
                            "read_execute_ns",
                            result.execute_elapsed_ns,
                        )
                        self._telemetry.observe(
                            "read_fetch_ns",
                            result.fetch_elapsed_ns,
                        )
                        self._telemetry.observe("read_total_ns", result.elapsed_ns)
                        current_ticket, current_seed = next_ticket, next_seed
                        if result.stopped:
                            break
                        if result.success:
                            self._counters.increment("reads")
                            continue
                        self._record_error(
                            request,
                            schema.database,
                            "reader",
                            worker_id,
                            endpoint,
                            query.seed,
                            query.sql,
                            result.error or "query_error",
                            connection_lost=result.connection_lost,
                        )
                        if result.connection_lost:
                            if stop_event.is_set():
                                break
                            raise RuntimeError(result.error or "reader connection lost")
                        # A server-side SQL error (including a query timeout) does
                        # not by itself invalidate the long-lived read session.
                        # Keep it open so the next generated query reuses the same
                        # connection and continues to exercise the real workload.
                        continue
            except QueryGenerationStopped:
                break
            except QueryGenerationProcessDied:
                raise
            except Exception as error:
                if stop_event.is_set():
                    break
                self._telemetry.set_stage(worker_key, "reconnecting")
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
        stop_event: StopEventLike,
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
        worker_key = self._worker_key(
            database_ordinal,
            "writer",
            "primary",
            worker_id,
        )
        while not stop_event.is_set():
            try:
                self._telemetry.set_stage(worker_key, "connecting")
                with self._factory.query_session(
                    self._primary,
                    schema.database,
                ) as session:
                    _tag_worker_session(
                        session,
                        worker_kind="writer",
                        endpoint="primary",
                    )
                    _execute_and_close(session, "SET SESSION innodb_lock_wait_timeout = 10")
                    delay = self._config.reconnect_initial_delay_seconds
                    while not stop_event.is_set():
                        transaction_seed = tree.derive(
                            "fuzz_write",
                            schema.database,
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
                                if stop_event.is_set():
                                    transaction_failed = True
                                    break
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
                                    node=self._primary,
                                    database=schema.database,
                                    timeout_seconds=self._config.query_timeout_seconds,
                                    on_stage=lambda stage: self._telemetry.set_stage(
                                        worker_key,
                                        f"writer_{stage}",
                                    ),
                                )
                                self._telemetry.observe(
                                    "write_execute_ns",
                                    result.execute_elapsed_ns,
                                )
                                self._telemetry.observe(
                                    "write_fetch_ns",
                                    result.fetch_elapsed_ns,
                                )
                                self._telemetry.observe(
                                    "write_total_ns",
                                    result.elapsed_ns,
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
                                    if result.stopped:
                                        break
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
                                if stop_event.is_set():
                                    transaction_failed = True
                                    break
                                if result.success and statement.operation == "delete":
                                    row_budget.record_delete(result.affected_rows)
                            _execute_and_close(
                                session,
                                (
                                    "ROLLBACK"
                                    if transaction_failed or stop_event.is_set()
                                    else "COMMIT"
                                ),
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
                self._telemetry.set_stage(worker_key, "reconnecting")
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
