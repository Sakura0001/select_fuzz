"""Bounded deterministic reader-query generation outside the connection threads."""

from __future__ import annotations

from dataclasses import dataclass
import multiprocessing
import os
from queue import Empty
import sys
from threading import Lock
import time
from typing import Any, Protocol

from select_fuzz.config.models import MAX_FUZZ_READER_WORKERS
from select_fuzz.execution.protocols import StopEventLike
from select_fuzz.generation.query import (
    GeneratedQuery,
    QueryGenerationContext,
    QueryGenerator,
    WeightedQueryGenerator,
)
from select_fuzz.generation.query.grammar import RandomGrammarQueryGenerator
from select_fuzz.generation.query.load_shaped import LoadShapedQueryGenerator
from select_fuzz.generation.query_grammar import (
    GrammarQueryConfig,
    GrammarQueryGenerator,
    GrammarSchema,
)


_GENERATION_WORKER_SWITCH_INTERVAL_SECONDS = 0.001
READER_QUERY_PREFETCH_DEPTH = 3


def _configure_generation_worker_scheduling() -> None:
    current = sys.getswitchinterval()
    sys.setswitchinterval(
        min(current, _GENERATION_WORKER_SWITCH_INTERVAL_SECONDS)
    )


class QueryGenerationStopped(RuntimeError):
    """The run stopped before a generated query was consumed."""


class QueryGenerationProcessDied(RuntimeError):
    """A generator child exited without completing its outstanding work."""


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    query: GeneratedQuery | None
    error_type: str | None
    compute_ns: int
    wait_ns: int


class GenerationTicket(Protocol):
    def result(self, stop_event: StopEventLike) -> GenerationOutcome: ...


class QueryGenerationPipeline(Protocol):
    def start(self) -> None: ...

    def register_database(
        self,
        database_ordinal: int,
        database: str,
        schema: GrammarSchema,
    ) -> None: ...

    def replace_database(
        self,
        database_ordinal: int,
        database: str,
        schema: GrammarSchema,
    ) -> None: ...

    def submit(
        self,
        database_ordinal: int,
        reader_id: int,
        operation: int,
        *,
        seed: int,
    ) -> GenerationTicket: ...

    def cancel_reader(self, database_ordinal: int, reader_id: int) -> None: ...

    def close(self) -> None: ...


def resolve_query_generator_processes(
    configured: int,
    *,
    reader_workers: int,
    cpu_count: int | None = None,
) -> int:
    """Resolve 0=auto across readers while keeping process growth bounded."""

    available = max(1, cpu_count if cpu_count is not None else (os.cpu_count() or 1))
    if configured == 0:
        latency_processes = (reader_workers + 3) // 4
        requested = min(max(available, latency_processes), 32)
    else:
        requested = configured
    return max(1, min(requested, reader_workers))


class _InlineTicket:
    def __init__(
        self,
        outcome: GenerationOutcome,
        release: Any,
    ) -> None:
        self._outcome = outcome
        self._release = release
        self._lock = Lock()
        self._released = False

    def result(self, stop_event: StopEventLike) -> GenerationOutcome:
        wait_started_ns = time.monotonic_ns()
        try:
            if stop_event.is_set():
                raise QueryGenerationStopped("query generation stopped")
            return GenerationOutcome(
                self._outcome.query,
                self._outcome.error_type,
                self._outcome.compute_ns,
                max(0, time.monotonic_ns() - wait_started_ns),
            )
        finally:
            with self._lock:
                if not self._released:
                    self._released = True
                    self._release()


class InlineQueryPipeline:
    """Compatibility pipeline used by tests and an explicit zero-process fallback."""

    def __init__(self, generator: QueryGenerator) -> None:
        self._generator = generator
        self._schemas: dict[int, QueryGenerationContext] = {}
        self._pending: dict[tuple[int, int], int] = {}
        self._lock = Lock()
        self._closed = False

    def start(self) -> None:
        return None

    def register_database(
        self,
        database_ordinal: int,
        database: str,
        schema: GrammarSchema,
    ) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("query generation pipeline is closed")
            if database_ordinal in self._schemas:
                raise ValueError("database ordinal is already registered")
            self._schemas[database_ordinal] = QueryGenerationContext(database, schema)

    def replace_database(
        self,
        database_ordinal: int,
        database: str,
        schema: GrammarSchema,
    ) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("query generation pipeline is closed")
            if database_ordinal not in self._schemas:
                raise KeyError(f"database ordinal is not registered: {database_ordinal}")
            if any(key[0] == database_ordinal for key in self._pending):
                raise RuntimeError("database still has outstanding generation requests")
            self._schemas[database_ordinal] = QueryGenerationContext(database, schema)

    def submit(
        self,
        database_ordinal: int,
        reader_id: int,
        operation: int,
        *,
        seed: int,
    ) -> GenerationTicket:
        del operation
        key = (database_ordinal, reader_id)
        with self._lock:
            if self._closed:
                raise RuntimeError("query generation pipeline is closed")
            context = self._schemas.get(database_ordinal)
            if context is None:
                raise KeyError(f"database ordinal is not registered: {database_ordinal}")
            pending = self._pending.get(key, 0)
            if pending >= READER_QUERY_PREFETCH_DEPTH:
                raise RuntimeError("reader has too many outstanding generation requests")
            self._pending[key] = pending + 1
        started_ns = time.monotonic_ns()
        try:
            query = self._generator.generate(context, seed=seed)
            outcome = GenerationOutcome(
                query,
                None,
                max(0, time.monotonic_ns() - started_ns),
                0,
            )
        except Exception as error:
            outcome = GenerationOutcome(
                None,
                type(error).__name__,
                max(0, time.monotonic_ns() - started_ns),
                0,
            )
        return _InlineTicket(
            outcome,
            lambda: self._release(key),
        )

    def _release(self, key: tuple[int, int]) -> None:
        with self._lock:
            pending = self._pending.get(key, 0)
            if pending <= 1:
                self._pending.pop(key, None)
            else:
                self._pending[key] = pending - 1

    def cancel_reader(self, database_ordinal: int, reader_id: int) -> None:
        with self._lock:
            self._pending.pop((database_ordinal, reader_id), None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending.clear()


@dataclass(frozen=True, slots=True)
class _RegisterDatabase:
    database_ordinal: int
    database: str
    schema: GrammarSchema


@dataclass(frozen=True, slots=True)
class _Generate:
    job_id: int
    database_ordinal: int
    reader_id: int
    seed: int


@dataclass(frozen=True, slots=True)
class _StopWorker:
    pass


@dataclass(frozen=True, slots=True)
class _GenerationResponse:
    job_id: int
    query: GeneratedQuery | None
    error_type: str | None
    compute_ns: int


def _production_query_generator(max_tables_per_query_block: int) -> WeightedQueryGenerator:
    grammar = RandomGrammarQueryGenerator(
        GrammarQueryGenerator(
            config=GrammarQueryConfig(
                max_tables_per_query_block=max_tables_per_query_block
            )
        )
    )
    return WeightedQueryGenerator(
        (
            ("grammar", grammar, 50),
            ("load_shaped", LoadShapedQueryGenerator(), 50),
        )
    )


def _generation_worker(
    request_queue: Any,
    response_queues: dict[tuple[int, int], Any],
    stop_event: Any,
    max_tables_per_query_block: int,
) -> None:
    _configure_generation_worker_scheduling()
    generator = _production_query_generator(max_tables_per_query_block)
    contexts: dict[int, QueryGenerationContext] = {}
    while not stop_event.is_set():
        try:
            message = request_queue.get(timeout=0.1)
        except Empty:
            continue
        if isinstance(message, _StopWorker):
            return
        if isinstance(message, _RegisterDatabase):
            contexts[message.database_ordinal] = QueryGenerationContext(
                message.database,
                message.schema,
            )
            continue
        if not isinstance(message, _Generate):
            continue
        started_ns = time.monotonic_ns()
        try:
            context = contexts[message.database_ordinal]
            query = generator.generate(context, seed=message.seed)
            response = _GenerationResponse(
                message.job_id,
                query,
                None,
                max(0, time.monotonic_ns() - started_ns),
            )
        except Exception as error:
            response = _GenerationResponse(
                message.job_id,
                None,
                type(error).__name__,
                max(0, time.monotonic_ns() - started_ns),
            )
        response_queues[(message.database_ordinal, message.reader_id)].put(response)


class _ProcessTicket:
    def __init__(
        self,
        broker: ProcessQueryPipeline,
        response_queue: Any,
        job_id: int,
        key: tuple[int, int],
    ) -> None:
        self._broker = broker
        self._response_queue = response_queue
        self._job_id = job_id
        self._key = key
        self._released = False
        self._lock = Lock()

    def result(self, stop_event: StopEventLike) -> GenerationOutcome:
        wait_started_ns = time.monotonic_ns()
        try:
            while True:
                if stop_event.is_set():
                    raise QueryGenerationStopped("query generation stopped")
                try:
                    response = self._response_queue.get(timeout=0.1)
                except Empty:
                    self._broker.assert_healthy()
                    continue
                if not isinstance(response, _GenerationResponse):
                    continue
                if response.job_id != self._job_id:
                    continue
                break
            return GenerationOutcome(
                response.query,
                response.error_type,
                response.compute_ns,
                max(0, time.monotonic_ns() - wait_started_ns),
            )
        finally:
            with self._lock:
                if not self._released:
                    self._released = True
                    self._broker.release_reader(self._key)


class ProcessQueryPipeline:
    """Sharded generators with one direct result channel per reader."""

    def __init__(
        self,
        *,
        process_count: int,
        max_tables_per_query_block: int,
        reader_keys: tuple[tuple[int, int], ...],
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        if process_count <= 0:
            raise ValueError("process_count must be positive")
        if max_tables_per_query_block <= 0:
            raise ValueError("max_tables_per_query_block must be positive")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        if not reader_keys:
            raise ValueError("reader_keys must not be empty")
        if len(reader_keys) != len(set(reader_keys)):
            raise ValueError("reader_keys must be unique")
        if len(reader_keys) > MAX_FUZZ_READER_WORKERS:
            raise ValueError(
                "direct query generation supports at most "
                f"{MAX_FUZZ_READER_WORKERS} readers"
            )
        if any(
            not isinstance(database_ordinal, int)
            or isinstance(database_ordinal, bool)
            or database_ordinal < 0
            or not isinstance(reader_id, int)
            or isinstance(reader_id, bool)
            or reader_id < 0
            for database_ordinal, reader_id in reader_keys
        ):
            raise ValueError("reader_keys must contain nonnegative integer pairs")
        if process_count > len(reader_keys):
            raise ValueError("process_count must not exceed configured readers")
        self._process_count = process_count
        self._max_tables_per_query_block = max_tables_per_query_block
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._reader_keys = reader_keys
        self._reader_process_indices = {
            key: ordinal % process_count for ordinal, key in enumerate(reader_keys)
        }
        self._context = multiprocessing.get_context("spawn")
        self._request_queues: list[Any] = []
        self._response_queues: dict[tuple[int, int], Any] = {}
        self._processes: list[Any] = []
        self._stop_event: Any | None = None
        self._pending_readers: dict[tuple[int, int], int] = {}
        self._registered: set[int] = set()
        self._next_job_id = 1
        self._lock = Lock()
        self._lifecycle_lock = Lock()
        self._started = False
        self._closed = False

    def start(self) -> None:
        with self._lifecycle_lock:
            self._start()

    def _start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("query generation pipeline is closed")
            if self._started:
                return
        try:
            self._stop_event = self._context.Event()
            for key in self._reader_keys:
                self._response_queues[key] = self._context.Queue()
            for ordinal in range(self._process_count):
                request_queue = self._context.Queue()
                self._request_queues.append(request_queue)
                worker_response_queues = {
                    key: self._response_queues[key]
                    for key, process_index in self._reader_process_indices.items()
                    if process_index == ordinal
                }
                process = self._context.Process(
                    target=_generation_worker,
                    args=(
                        request_queue,
                        worker_response_queues,
                        self._stop_event,
                        self._max_tables_per_query_block,
                    ),
                    name=f"sf-query-generator-{ordinal}",
                    daemon=True,
                )
                process.start()
                self._processes.append(process)
        except BaseException:
            self._rollback_start()
            with self._lock:
                self._closed = True
            raise
        with self._lock:
            self._started = True

    def _rollback_start(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        for request_queue in self._request_queues:
            try:
                request_queue.put_nowait(_StopWorker())
            except Exception:
                pass
        for process in self._processes:
            if process.is_alive():
                process.terminate()
            process.join()
        for queue in (*self._request_queues, *self._response_queues.values()):
            try:
                queue.close()
                queue.join_thread()
            except Exception:
                pass
        self._request_queues.clear()
        self._response_queues.clear()
        self._processes.clear()
        self._stop_event = None

    def register_database(
        self,
        database_ordinal: int,
        database: str,
        schema: GrammarSchema,
    ) -> None:
        self._require_started()
        with self._lock:
            if database_ordinal in self._registered:
                raise ValueError("database ordinal is already registered")
            self._registered.add(database_ordinal)
        message = _RegisterDatabase(database_ordinal, database, schema)
        for request_queue in self._request_queues:
            request_queue.put(message)

    def replace_database(
        self,
        database_ordinal: int,
        database: str,
        schema: GrammarSchema,
    ) -> None:
        self._require_started()
        with self._lock:
            if database_ordinal not in self._registered:
                raise KeyError(f"database ordinal is not registered: {database_ordinal}")
            if any(key[0] == database_ordinal for key in self._pending_readers):
                raise RuntimeError("database still has outstanding generation requests")
        message = _RegisterDatabase(database_ordinal, database, schema)
        for request_queue in self._request_queues:
            request_queue.put(message)

    def submit(
        self,
        database_ordinal: int,
        reader_id: int,
        operation: int,
        *,
        seed: int,
    ) -> GenerationTicket:
        del operation
        self._require_started()
        key = (database_ordinal, reader_id)
        with self._lock:
            if database_ordinal not in self._registered:
                raise KeyError(f"database ordinal is not registered: {database_ordinal}")
            if key not in self._reader_process_indices:
                raise KeyError(f"reader is not configured: {key}")
            pending = self._pending_readers.get(key, 0)
            if pending >= READER_QUERY_PREFETCH_DEPTH:
                raise RuntimeError("reader has too many outstanding generation requests")
            job_id = self._next_job_id
            self._next_job_id += 1
            self._pending_readers[key] = pending + 1
            queue_index = self._reader_process_indices[key]
            response_queue = self._response_queues[key]
        self._request_queues[queue_index].put(
            _Generate(job_id, database_ordinal, reader_id, seed)
        )
        return _ProcessTicket(self, response_queue, job_id, key)

    def _require_started(self) -> None:
        with self._lock:
            if not self._started:
                raise RuntimeError("query generation pipeline is not started")
            if self._closed:
                raise RuntimeError("query generation pipeline is closed")

    def assert_healthy(self) -> None:
        with self._lock:
            if self._closed:
                raise QueryGenerationStopped("query generation pipeline is closed")
            failed = [
                process.name
                for process in self._processes
                if process.exitcode is not None and process.exitcode != 0
            ]
        if failed:
            raise QueryGenerationProcessDied(
                "查询生成进程异常退出：" + "，".join(failed)
            )

    def release_reader(self, key: tuple[int, int]) -> None:
        with self._lock:
            pending = self._pending_readers.get(key, 0)
            if pending <= 1:
                self._pending_readers.pop(key, None)
            else:
                self._pending_readers[key] = pending - 1

    def cancel_reader(self, database_ordinal: int, reader_id: int) -> None:
        with self._lock:
            self._pending_readers.pop((database_ordinal, reader_id), None)

    @property
    def alive_processes(self) -> int:
        return sum(process.is_alive() for process in self._processes)

    def close(self) -> None:
        with self._lifecycle_lock:
            self._close()

    def _close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stop_event = self._stop_event
            queues = tuple(self._request_queues)
            response_queues = tuple(self._response_queues.values())
            processes = tuple(self._processes)
        if stop_event is not None:
            stop_event.set()
        for request_queue in queues:
            try:
                request_queue.put_nowait(_StopWorker())
            except Exception:
                pass
        deadline = time.monotonic() + self._shutdown_timeout_seconds
        for process in processes:
            process.join(max(0.0, deadline - time.monotonic()))
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
        with self._lock:
            self._pending_readers.clear()
        for request_queue in queues:
            try:
                request_queue.close()
                request_queue.join_thread()
            except Exception:
                pass
        for response_queue in response_queues:
            try:
                response_queue.close()
                response_queue.join_thread()
            except Exception:
                pass
        with self._lock:
            self._request_queues.clear()
            self._response_queues.clear()
            self._processes.clear()
            self._registered.clear()
            self._stop_event = None
            self._started = False


__all__ = [
    "GenerationOutcome",
    "GenerationTicket",
    "InlineQueryPipeline",
    "ProcessQueryPipeline",
    "QueryGenerationPipeline",
    "QueryGenerationProcessDied",
    "QueryGenerationStopped",
    "READER_QUERY_PREFETCH_DEPTH",
    "resolve_query_generator_processes",
]
