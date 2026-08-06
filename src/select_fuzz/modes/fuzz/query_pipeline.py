"""Bounded deterministic reader-query generation outside the connection threads."""

from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import multiprocessing
import os
from queue import Empty
from threading import Lock
import time
from typing import Any, Protocol

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
    requested = min(available, 32) if configured == 0 else configured
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
        self._pending: set[tuple[int, int]] = set()
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
            if key in self._pending:
                raise RuntimeError("reader already has an outstanding generation request")
            self._pending.add(key)
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
            self._pending.discard(key)

    def cancel_reader(self, database_ordinal: int, reader_id: int) -> None:
        self._release((database_ordinal, reader_id))

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
    response_queue: Any,
    stop_event: Any,
    max_tables_per_query_block: int,
) -> None:
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
        response_queue.put(response)


class _ProcessTicket:
    def __init__(
        self,
        broker: ProcessQueryPipeline,
        future: Future[_GenerationResponse],
        key: tuple[int, int],
        queue_index: int,
    ) -> None:
        self._broker = broker
        self._future = future
        self._key = key
        self._queue_index = queue_index
        self._released = False
        self._lock = Lock()

    def result(self, stop_event: StopEventLike) -> GenerationOutcome:
        wait_started_ns = time.monotonic_ns()
        try:
            response = self._broker._wait_for_response(
                self._future,
                stop_event,
                queue_index=self._queue_index,
            )
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
    """Sharded generators whose waiting readers dispatch their process results."""

    def __init__(
        self,
        *,
        process_count: int,
        max_tables_per_query_block: int,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        if process_count <= 0:
            raise ValueError("process_count must be positive")
        if max_tables_per_query_block <= 0:
            raise ValueError("max_tables_per_query_block must be positive")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._process_count = process_count
        self._max_tables_per_query_block = max_tables_per_query_block
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._context = multiprocessing.get_context("spawn")
        self._request_queues: list[Any] = []
        self._response_queues: list[Any] = []
        self._processes: list[Any] = []
        self._stop_event: Any | None = None
        self._futures: dict[int, Future[_GenerationResponse]] = {}
        self._pending_readers: set[tuple[int, int]] = set()
        self._registered: set[int] = set()
        self._next_job_id = 1
        self._lock = Lock()
        self._started = False
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("query generation pipeline is closed")
            if self._started:
                return
            self._stop_event = self._context.Event()
            for ordinal in range(self._process_count):
                request_queue = self._context.Queue()
                response_queue = self._context.Queue()
                process = self._context.Process(
                    target=_generation_worker,
                    args=(
                        request_queue,
                        response_queue,
                        self._stop_event,
                        self._max_tables_per_query_block,
                    ),
                    name=f"sf-query-generator-{ordinal}",
                    daemon=True,
                )
                process.start()
                self._request_queues.append(request_queue)
                self._response_queues.append(response_queue)
                self._processes.append(process)
            self._started = True

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
            if key in self._pending_readers:
                raise RuntimeError("reader already has an outstanding generation request")
            job_id = self._next_job_id
            self._next_job_id += 1
            future: Future[_GenerationResponse] = Future()
            self._futures[job_id] = future
            self._pending_readers.add(key)
            queue_index = (job_id - 1) % self._process_count
        self._request_queues[queue_index].put(
            _Generate(job_id, database_ordinal, seed)
        )
        return _ProcessTicket(self, future, key, queue_index)

    def _require_started(self) -> None:
        with self._lock:
            if not self._started:
                raise RuntimeError("query generation pipeline is not started")
            if self._closed:
                raise RuntimeError("query generation pipeline is closed")

    def _wait_for_response(
        self,
        future: Future[_GenerationResponse],
        stop_event: StopEventLike,
        *,
        queue_index: int,
    ) -> _GenerationResponse:
        try:
            response_queue = self._response_queues[queue_index]
        except IndexError as error:  # pragma: no cover - start invariant
            raise RuntimeError("query generation response queue is unavailable") from error
        while True:
            if stop_event.is_set():
                raise QueryGenerationStopped("query generation stopped")
            if future.done():
                return future.result()
            try:
                # multiprocessing.Queue serializes consumers behind one read lock.
                # One response queue per generator keeps that lock sharded, while
                # nonblocking reads avoid holding it while a queue is temporarily
                # empty. A dispatched result wakes its owning reader immediately.
                response = response_queue.get_nowait()
            except Empty:
                try:
                    return future.result(timeout=0.005)
                except FutureTimeoutError:
                    pass
                self.assert_healthy()
                continue
            if not isinstance(response, _GenerationResponse):
                continue
            with self._lock:
                response_future = self._futures.pop(response.job_id, None)
            if response_future is not None and not response_future.done():
                response_future.set_result(response)

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
                "query generator process exited: " + ", ".join(failed)
            )

    def release_reader(self, key: tuple[int, int]) -> None:
        with self._lock:
            self._pending_readers.discard(key)

    def cancel_reader(self, database_ordinal: int, reader_id: int) -> None:
        self.release_reader((database_ordinal, reader_id))

    @property
    def alive_processes(self) -> int:
        return sum(process.is_alive() for process in self._processes)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stop_event = self._stop_event
            queues = tuple(self._request_queues)
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
            futures = tuple(self._futures.values())
            self._futures.clear()
            self._pending_readers.clear()
        for future in futures:
            if not future.done():
                future.set_exception(QueryGenerationStopped("query generation stopped"))
        for request_queue in queues:
            try:
                request_queue.close()
                request_queue.join_thread()
            except Exception:
                pass
        for response_queue in self._response_queues:
            try:
                response_queue.close()
                response_queue.join_thread()
            except Exception:
                pass


__all__ = [
    "GenerationOutcome",
    "GenerationTicket",
    "InlineQueryPipeline",
    "ProcessQueryPipeline",
    "QueryGenerationPipeline",
    "QueryGenerationProcessDied",
    "QueryGenerationStopped",
    "resolve_query_generator_processes",
]
