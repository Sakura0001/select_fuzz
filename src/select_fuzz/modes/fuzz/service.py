"""Concurrent per-database orchestration for the fuzz mode."""

from __future__ import annotations

from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import random
import sys
from threading import Event, Lock, Thread
import time
from typing import Callable, Iterator, Mapping

from select_fuzz.artifacts import JsonlWriter
from select_fuzz.config import FuzzConfig, NodeConfig
from select_fuzz.domain import RunRequest, SeedTree
from select_fuzz.execution.protocols import ConnectionFactory, QuerySession, StopEventLike
from select_fuzz.execution.timeout import KillQueryWatchdog
from select_fuzz.generation.query import GeneratedQuery, WeightedQueryGenerator
from select_fuzz.generation.query_grammar import CandidateRejected
from select_fuzz.modes.fuzz.dml import FuzzDmlGenerator
from select_fuzz.modes.fuzz.diagnostics import (
    FuzzProcesslistCollector,
    FuzzProgressReporter,
    FuzzRuntimeDiagnostics,
)
from select_fuzz.modes.fuzz.execution import StreamingQueryExecutor
from select_fuzz.modes.fuzz.forensics import (
    FuzzErrorAggregator,
    render_traceback_text,
)
from select_fuzz.modes.fuzz.materialization import (
    _event_error_message,
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
    READER_QUERY_PREFETCH_DEPTH,
)
from select_fuzz.modes.fuzz.sql_log import FuzzSqlRecorder
from select_fuzz.modes.fuzz.telemetry import FuzzStageTelemetry
from select_fuzz.service import RunSummary


_FUZZ_THREAD_SWITCH_INTERVAL_SECONDS = 0.001
_fair_scheduling_lock = Lock()
_fair_scheduling_users = 0
_fair_scheduling_baseline: float | None = None


@contextmanager
def _fair_worker_thread_scheduling() -> Iterator[None]:
    global _fair_scheduling_baseline, _fair_scheduling_users
    with _fair_scheduling_lock:
        if _fair_scheduling_users == 0:
            baseline = sys.getswitchinterval()
            sys.setswitchinterval(
                min(baseline, _FUZZ_THREAD_SWITCH_INTERVAL_SECONDS)
            )
            _fair_scheduling_baseline = baseline
        _fair_scheduling_users += 1
    try:
        yield
    finally:
        with _fair_scheduling_lock:
            _fair_scheduling_users -= 1
            if _fair_scheduling_users == 0:
                restore_interval = _fair_scheduling_baseline
                _fair_scheduling_baseline = None
                if restore_interval is not None:
                    sys.setswitchinterval(restore_interval)


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
    timeouts: int
    connection_losses: int
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
            "timeouts": 0,
            "connection_losses": 0,
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
        rendered = "；".join(
            f"数据库[{ordinal}]={database}，异常类型={error_type}，"
            f"原始错误={error_message}"
            for ordinal, database, error_type, error_message in failures
        )
        super().__init__(f"fuzz 批次创建失败：{rendered}")


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
        progress_sink: Callable[[str], None] | None = None,
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
        self._runtime = FuzzRuntimeDiagnostics()
        self._error_aggregator = FuzzErrorAggregator()
        self._last_error_summary_ns: int | None = None
        self._progress_sink = progress_sink
        expected_primary_readers = (
            config.databases * config.reader_threads_per_database // 3
        )
        expected_groups = {
            "primary_writer": config.databases * config.writer_threads_per_database,
            "primary_reader": expected_primary_readers,
            "replica_reader": (
                config.databases * config.reader_threads_per_database
                - expected_primary_readers
            ),
        }
        self._progress_reporter = (
            None
            if progress_sink is None
            else FuzzProgressReporter(
                diagnostics_interval_seconds=config.diagnostics_interval_seconds,
                expected_connection_groups=expected_groups,
                expected_databases=config.databases,
            )
        )
        self._processlist_collector = (
            None
            if progress_sink is None
            else FuzzProcesslistCollector(factory, primary, replica, self._runtime)
        )
        self._processlist_lock = Lock()
        self._latest_processlist: dict[str, object] = {
            "sampled_at_ns": time.monotonic_ns(),
            "endpoints": {
                "primary": FuzzProcesslistCollector._empty_endpoint(0),
                "replica": FuzzProcesslistCollector._empty_endpoint(0),
            },
        }
        self._prepared_reader_queries: dict[
            tuple[str, int], _PreparedReaderQuery
        ] = {}
        self._prepared_reader_queries_lock = Lock()

    @contextmanager
    def _track_worker_connection(
        self,
        session: QuerySession,
        *,
        worker: str,
        endpoint: str,
        worker_kind: str,
        database: str,
    ) -> Iterator[None]:
        connection_id = session.connection_id()
        self._runtime.register_connection(
            worker=worker,
            endpoint=endpoint,
            worker_kind=worker_kind,
            database=database,
            connection_id=connection_id,
        )
        try:
            yield
        finally:
            self._runtime.unregister_connection(worker, connection_id)

    def run(self, request: RunRequest, stop_event: Event) -> RunSummary:
        if request.mode != "fuzz":
            raise ValueError("FuzzModeService requires fuzz mode")
        pipeline = self._query_pipeline_factory()
        pipeline.start()
        self._query_pipeline = pipeline
        self._runtime.set_phase("starting")
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
        processlist_done = Event()
        processlist_monitor: Thread | None = None
        if self._processlist_collector is not None:
            processlist_monitor = Thread(
                target=self._monitor_processlist,
                args=(processlist_done,),
                name="sf-fuzz-processlist",
                daemon=True,
            )
            processlist_monitor.start()
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
                    "diagnostics_interval_seconds": (
                        self._config.diagnostics_interval_seconds
                    ),
                    "connector_implementation": self._connector_implementation,
                    "seed": request.seed,
                }
            )
            generation = 0
            try:
                while not stop_event.is_set():
                    generation_started = time.monotonic()
                    refresh_deadline_ns = None
                    if self._config.schema_refresh_interval_seconds > 0:
                        refresh_deadline_ns = time.monotonic_ns() + int(
                            self._config.schema_refresh_interval_seconds
                            * 1_000_000_000
                        )
                    self._runtime.set_phase(
                        "materializing",
                        generation=generation,
                        refresh_deadline_ns=refresh_deadline_ns,
                    )
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
                        self._runtime.set_phase("prewarming")
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
                    self._runtime.set_databases_ready(len(schemas))
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
                    self._runtime.set_phase("running")
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
                self._runtime.set_phase("failed")
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
            self._runtime.set_phase("finished")
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
                    "timeouts": counters.timeouts,
                    "connection_losses": counters.connection_losses,
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
            processlist_done.set()
            if processlist_monitor is not None:
                processlist_monitor.join(timeout=0.1)
            pipeline.close()
            self._query_pipeline = None

    def _monitor_stop(self, stop_event: Event, monitor_done: Event) -> None:
        while not monitor_done.wait(0.05):
            if stop_event.is_set():
                self._streaming.stop_active()
                return

    def _monitor_telemetry(self, request: RunRequest, done: Event) -> None:
        while not done.wait(self._config.diagnostics_interval_seconds):
            self._append_stage_snapshot(request, final=False)

    def _monitor_processlist(self, done: Event) -> None:
        collector = self._processlist_collector
        if collector is None:
            return
        while not done.is_set():
            try:
                snapshot = collector.collect()
            except Exception as error:  # pragma: no cover - defensive collector boundary
                snapshot = {
                    "sampled_at_ns": time.monotonic_ns(),
                    "collector_error_type": type(error).__name__,
                    "collector_error": str(error),
                    "endpoints": {
                        "primary": FuzzProcesslistCollector._empty_endpoint(0),
                        "replica": FuzzProcesslistCollector._empty_endpoint(0),
                    },
                }
            with self._processlist_lock:
                self._latest_processlist = snapshot
            if done.wait(self._config.diagnostics_interval_seconds):
                return

    def _append_stage_snapshot(self, request: RunRequest, *, final: bool) -> None:
        snapshot = self._telemetry.snapshot()
        counters = asdict(self._counters.snapshot())
        pipeline = self._query_pipeline
        pipeline_snapshot = (
            {
                "processes_total": 0,
                "processes_alive": 0,
                "registered_databases": 0,
                "pending_requests": 0,
                "pending_readers": 0,
                "oldest_pending_ns": 0,
                "max_pending_per_reader": 0,
            }
            if pipeline is None
            else asdict(pipeline.snapshot())
        )
        with self._processlist_lock:
            processlist = self._latest_processlist
        public_processlist = self._public_processlist_snapshot(processlist)
        now_ns = time.monotonic_ns()
        interval_seconds = self._config.diagnostics_interval_seconds
        if self._last_error_summary_ns is not None:
            interval_seconds = max(
                1e-9,
                (now_ns - self._last_error_summary_ns) / 1_000_000_000,
            )
        self._last_error_summary_ns = now_ns
        raw_error_summary = self._error_aggregator.snapshot(
            interval_seconds=interval_seconds
        )
        error_summary = self._bounded_error_summary(raw_error_summary)
        runtime_snapshot, runtime_connections = self._runtime.snapshot_with_connections()
        registered_connections = tuple(
            asdict(connection) for connection in runtime_connections
        )
        connection_groups = runtime_snapshot.get("connection_groups", {})
        document = {
            "type": "fuzz_stage_snapshot",
            "run_id": request.run_id,
            "mode": "fuzz",
            "occurred_at": _now(),
            "final": final,
            **snapshot,
            "counters": counters,
            "pipeline": pipeline_snapshot,
            "runtime": runtime_snapshot,
            "connections": {
                "total": len(registered_connections),
                "groups": connection_groups,
                "registered": registered_connections[:3],
                "truncated": max(0, len(registered_connections) - 3),
            },
            "processlist": public_processlist,
            "errors_summary": error_summary,
        }
        self._records.append(document)
        total_error_count = error_summary.get("total_count")
        if (
            isinstance(total_error_count, int)
            and not isinstance(total_error_count, bool)
            and total_error_count > 0
        ):
            self._records.append(
                {
                    "type": "fuzz_error_summary",
                    "run_id": request.run_id,
                    "mode": "fuzz",
                    "occurred_at": _now(),
                    "final": final,
                    "summary": error_summary,
                }
            )
        reporter = self._progress_reporter
        sink = self._progress_sink
        if reporter is None or sink is None:
            return
        try:
            for line in reporter.render(document):
                sink(line)
        except Exception:
            return

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
        event_failures: list[_GenerationFailure] = []
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
                event_failures.append(
                    (
                        database_ordinal,
                        database,
                        type(error).__name__,
                        _event_error_message(error),
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
                        for ordinal, database, error_type, error_message in event_failures
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
                        raise RuntimeError("尝试 100 次后仍无法为读线程预生成查询")
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
        with _fair_worker_thread_scheduling(), ThreadPoolExecutor(
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
            self._runtime.set_phase("stopping")
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

        pending_tickets: deque[tuple[GenerationTicket, int]] = deque()
        current_query: GeneratedQuery | None
        if prepared is None:
            current_query = None
        else:
            current_seed = prepared.seed
            current_query = prepared.query

        def refill_prefetch() -> None:
            while len(pending_tickets) < READER_QUERY_PREFETCH_DEPTH:
                pending_tickets.append(submit_next())

        refill_prefetch()
        while not stop_event.is_set():
            try:
                self._telemetry.set_stage(worker_key, "connecting")
                with (
                    self._factory.query_session(node, schema.database) as session,
                    self._track_worker_connection(
                        session,
                        worker=worker_key,
                        endpoint=endpoint,
                        worker_kind="reader",
                        database=schema.database,
                    ),
                ):
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
                            current_ticket, current_seed = pending_tickets.popleft()
                            outcome = current_ticket.result(stop_event)
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
                                refill_prefetch()
                                continue
                            query = outcome.query
                        else:
                            query = current_query
                            current_query = None
                        refill_prefetch()
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
                        if result.stopped:
                            break
                        if result.success:
                            self._counters.increment("reads")
                            continue
                        if result.timed_out:
                            self._counters.increment("timeouts")
                        if result.connection_lost:
                            self._counters.increment("connection_losses")
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
                            failure_evidence=result.failure_evidence,
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
                with (
                    self._factory.query_session(
                        self._primary,
                        schema.database,
                    ) as session,
                    self._track_worker_connection(
                        session,
                        worker=worker_key,
                        endpoint="primary",
                        worker_kind="writer",
                        database=schema.database,
                    ),
                ):
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
                                    if result.timed_out:
                                        self._counters.increment("timeouts")
                                    if result.connection_lost:
                                        self._counters.increment("connection_losses")
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
                                        failure_evidence=result.failure_evidence,
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
        failure_evidence: Mapping[str, object] | None = None,
    ) -> None:
        self._counters.increment("errors")
        evidence = (
            dict(failure_evidence)
            if failure_evidence is not None
            else self._identity_evidence(error)
        )
        connection_id = evidence.get("connection_id")
        mysql_visibility = self._connection_visibility(endpoint, connection_id)
        evidence["mysql_visibility"] = mysql_visibility
        worker = f"{database}:{worker_kind}-{endpoint}:{worker_id}"
        watchdog = evidence.get("watchdog")
        timed_out = bool(
            isinstance(watchdog, Mapping) and watchdog.get("timed_out") is True
        )
        raw_visible = mysql_visibility.get("visible")
        mysql_visible = raw_visible if isinstance(raw_visible, bool) else None
        decision = self._error_aggregator.record(
            evidence=evidence,
            worker=worker,
            database=database,
            endpoint=endpoint,
            sql=sql,
            timed_out=timed_out,
            connection_lost=connection_lost,
            mysql_visible=mysql_visible,
        )
        if decision.is_new:
            self._records.append(
                {
                    "type": "fuzz_error_sample",
                    "run_id": request.run_id,
                    "mode": "fuzz",
                    "occurred_at": _now(),
                    "generation": self._runtime.snapshot().get("generation"),
                    "database": database,
                    "worker_kind": worker_kind,
                    "worker_id": worker_id,
                    "endpoint": endpoint,
                    "seed": seed,
                    "sql": sql,
                    "fingerprint": decision.fingerprint,
                    "error": error,
                    "evidence": evidence,
                    "traceback": render_traceback_text(evidence),
                }
            )
        if not decision.write_operation_event:
            return
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
            "fingerprint": decision.fingerprint,
            "suppressed_repeats": decision.suppressed_repeats,
        }
        if sql is not None:
            record["sql"] = sql
        self._records.append(record)
        self._runtime.record_issue(
            worker=worker,
            endpoint=endpoint,
            error=error,
            sql=sql,
        )

    @staticmethod
    def _identity_evidence(error: str) -> dict[str, object]:
        exception_type = error.split(":", 1)[0] or "query_error"
        return {
            "failure_stage": "operation",
            "exception": {
                "module": "",
                "type": exception_type,
                "message": error,
                "repr": error,
                "args": (error,),
                "errno": None,
                "sqlstate": None,
                "relation": "root",
            },
            "exception_chain": (),
            "traceback_frames": (),
            "connection_id": None,
            "watchdog": {"timed_out": False, "fired": False, "completed": True},
        }

    def _connection_visibility(
        self,
        endpoint: str,
        connection_id: object,
    ) -> dict[str, object]:
        if not isinstance(connection_id, int) or isinstance(connection_id, bool):
            return {"visible": None, "reason": "connection_id_unavailable"}
        with self._processlist_lock:
            processlist = self._latest_processlist
        if processlist.get("collector_error_type") is not None:
            return {
                "visible": None,
                "reason": "processlist_collector_error",
                "collector_error_type": processlist.get("collector_error_type"),
            }
        sampled_at_ns = processlist.get("sampled_at_ns")
        if not isinstance(sampled_at_ns, int) or isinstance(sampled_at_ns, bool):
            return {"visible": None, "reason": "processlist_sample_unavailable"}
        age_seconds = max(0.0, (time.monotonic_ns() - sampled_at_ns) / 1_000_000_000)
        if age_seconds > max(15.0, self._config.diagnostics_interval_seconds * 2):
            return {
                "visible": None,
                "reason": "processlist_sample_stale",
                "sample_age_seconds": age_seconds,
            }
        endpoints = processlist.get("endpoints")
        endpoint_summary = (
            endpoints.get(endpoint)
            if isinstance(endpoints, Mapping)
            else None
        )
        if not isinstance(endpoint_summary, Mapping):
            return {"visible": None, "reason": "endpoint_sample_unavailable"}
        registered = endpoint_summary.get("registered")
        if (
            not isinstance(registered, int)
            or isinstance(registered, bool)
            or registered <= 0
        ):
            return {
                "visible": None,
                "reason": "processlist_sample_has_no_registered_connections",
            }
        visible_ids = endpoint_summary.get("_visible_connection_ids")
        if not isinstance(visible_ids, (tuple, list)):
            return {"visible": None, "reason": "connection_ids_unavailable"}
        return {
            "visible": connection_id in visible_ids,
            "reason": "periodic_processlist_sample",
            "sample_age_seconds": age_seconds,
        }

    @staticmethod
    def _public_processlist_snapshot(
        processlist: Mapping[str, object],
    ) -> dict[str, object]:
        public = dict(processlist)
        raw_endpoints = processlist.get("endpoints")
        if not isinstance(raw_endpoints, Mapping):
            return public
        public["endpoints"] = {
            str(endpoint): {
                str(key): value
                for key, value in summary.items()
                if not str(key).startswith("_")
            }
            if isinstance(summary, Mapping)
            else summary
            for endpoint, summary in raw_endpoints.items()
        }
        return public

    @staticmethod
    def _bounded_error_summary(summary: Mapping[str, object]) -> dict[str, object]:
        raw_top = summary.get("top")
        top: list[dict[str, object]] = []
        if isinstance(raw_top, (tuple, list)):
            for raw_item in raw_top[:8]:
                if not isinstance(raw_item, Mapping):
                    continue
                evidence = raw_item.get("first_evidence")
                evidence_map = evidence if isinstance(evidence, Mapping) else {}
                exception = evidence_map.get("exception")
                exception_map = exception if isinstance(exception, Mapping) else {}
                top.append(
                    {
                        key: raw_item.get(key)
                        for key in (
                            "fingerprint",
                            "total_count",
                            "interval_count",
                            "rate_per_second",
                            "worker_count",
                            "database_count",
                            "endpoints",
                            "timed_out_count",
                            "connection_lost_count",
                            "mysql_not_visible_count",
                        )
                    }
                    | {
                        "failure_stage": evidence_map.get("failure_stage"),
                        "error_type": exception_map.get("type"),
                        "message": str(exception_map.get("message", ""))[:4096],
                        "connection_id": evidence_map.get("connection_id"),
                        "mysql_visibility": evidence_map.get("mysql_visibility"),
                        "watchdog": evidence_map.get("watchdog"),
                        "sample_sql": str(raw_item.get("sample_sql") or "")[:300],
                    }
                )
        return {
            key: summary.get(key)
            for key in (
                "total_count",
                "interval_count",
                "rate_per_second",
                "fingerprint_count",
                "other_count",
                "other_interval_count",
            )
        } | {"top": tuple(top)}

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
        self._runtime.record_issue(
            worker=f"{database}:{worker_kind}-{endpoint}:{worker_id}",
            endpoint=endpoint,
            error=f"{type(error).__name__}: {error}",
        )
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
