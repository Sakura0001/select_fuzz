from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event, Lock

from select_fuzz.artifacts import JsonlWriter
from select_fuzz.config import FuzzConfig, NodeConfig, NodeRole
from select_fuzz.domain import RunRequest
from select_fuzz.generation.query import GeneratedQuery, WeightedQueryGenerator
from select_fuzz.generation.query_grammar import (
    GrammarColumn,
    GrammarSchema,
    GrammarTable,
)
from select_fuzz.modes.fuzz.materialization import FuzzDatabaseSchema
from select_fuzz.modes.fuzz.service import FuzzModeService


def _schema(database: str) -> FuzzDatabaseSchema:
    table = GrammarTable(
        "fuzz_t0",
        (GrammarColumn("id", "BIGINT"), GrammarColumn("amount", "BIGINT")),
        (),
    )
    from select_fuzz.modes.fuzz.dml import FuzzTable

    return FuzzDatabaseSchema(
        database,
        GrammarSchema((table,)),
        (FuzzTable("fuzz_t0", "id", ("amount",)),),
        100,
    )


class _Materializer:
    def __init__(self, calls: list[str], lock: Lock) -> None:
        self._calls = calls
        self._lock = lock

    def materialize(self, database: str, *, seed: int) -> FuzzDatabaseSchema:
        del seed
        with self._lock:
            self._calls.append(database)
        return _schema(database)


class _RecordingService(FuzzModeService):
    def __init__(self, *args, worker_calls: list[tuple[str, int]], **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._worker_calls = worker_calls
        self._worker_lock = Lock()

    def _reader_loop(self, request, schema, database_ordinal, worker_id, node, endpoint, stop_event):  # type: ignore[no-untyped-def]
        del request, schema, database_ordinal, node, stop_event
        with self._worker_lock:
            self._worker_calls.append((endpoint, worker_id))

    def _writer_loop(self, request, schema, row_budget, database_ordinal, worker_id, stop_event):  # type: ignore[no-untyped-def]
        del request, schema, row_budget, database_ordinal, stop_event
        with self._worker_lock:
            self._worker_calls.append(("primary-write", worker_id))


@dataclass
class _NoopFactory:
    def query_session(self, node, database):  # type: ignore[no-untyped-def]
        raise AssertionError("worker loops are replaced in this test")

    def control_session(self, node, database):  # type: ignore[no-untyped-def]
        raise AssertionError("worker loops are replaced in this test")


class _QueryGenerator:
    def generate(self, context, *, seed):  # type: ignore[no-untyped-def]
        return GeneratedQuery("SELECT 1", seed, "test")


class _EmptyCursor:
    affected_rows = 0

    def fetchmany(self, size: int):  # type: ignore[no-untyped-def]
        del size
        return ()

    def close(self) -> None:
        return None


class _SqlError(Exception):
    errno = 1064
    sqlstate = "42000"


class _ErrorSession:
    def __init__(self, stop_event: Event) -> None:
        self._stop_event = stop_event

    def execute(self, sql: str) -> _EmptyCursor:
        if sql.startswith("SELECT"):
            self._stop_event.set()
            raise _SqlError("ordinary generated SQL error")
        return _EmptyCursor()

    def close(self) -> None:
        return None


class _ErrorFactory:
    def __init__(self, stop_event: Event) -> None:
        self._stop_event = stop_event
        self.opens = 0

    @contextmanager
    def query_session(self, node, database):  # type: ignore[no-untyped-def]
        del node, database
        self.opens += 1
        yield _ErrorSession(self._stop_event)


def test_ordinary_reader_sql_error_keeps_the_long_lived_session(tmp_path) -> None:  # type: ignore[no-untyped-def]
    stop_event = Event()
    factory = _ErrorFactory(stop_event)
    config = FuzzConfig(
        databases=1,
        writer_threads_per_database=1,
        reader_threads_per_database=3,
        initial_tables=1,
        initial_rows_per_table=100,
        max_rows_per_database=1000,
    )
    primary = NodeConfig(role=NodeRole.CUSTOM_ON, host="primary")
    service = FuzzModeService(
        config=config,
        primary=primary,
        replica=NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307),
        factory=factory,
        records=JsonlWriter(tmp_path / "events.jsonl"),
        query_generator=WeightedQueryGenerator((("test", _QueryGenerator(), 1),)),
        materializer_factory=lambda: _Materializer([], Lock()),
    )

    service._reader_loop(  # type: ignore[attr-defined]
        RunRequest("run-fuzz-reader", "fuzz", 1, 1, None, 1),
        _schema("sf_f_reader"),
        0,
        0,
        primary,
        "primary",
        stop_event,
    )

    assert factory.opens == 1
    counters = service._counters.snapshot()  # type: ignore[attr-defined]
    assert counters.errors == 1
    assert counters.reconnects == 0


def test_each_database_starts_its_own_1_to_2_reader_pool(tmp_path) -> None:  # type: ignore[no-untyped-def]
    config = FuzzConfig(
        databases=2,
        writer_threads_per_database=2,
        reader_threads_per_database=6,
        initial_tables=1,
        initial_rows_per_table=100,
        max_rows_per_database=1000,
    )
    primary = NodeConfig(role=NodeRole.CUSTOM_ON, host="primary")
    replica = NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307)
    materialized: list[str] = []
    worker_calls: list[tuple[str, int]] = []
    service = _RecordingService(
        config=config,
        primary=primary,
        replica=replica,
        factory=_NoopFactory(),
        records=JsonlWriter(tmp_path / "events.jsonl"),
        query_generator=WeightedQueryGenerator((("test", _QueryGenerator(), 1),)),
        materializer_factory=lambda: _Materializer(materialized, Lock()),
        worker_calls=worker_calls,
    )

    summary = service.run(
        RunRequest(
            run_id="run-fuzz-service",
            mode="fuzz",
            seed=7,
            workers=1,
            rounds=None,
            queries_per_round=1,
        ),
        Event(),
    )

    assert len(materialized) == 2
    assert summary.rounds_completed == 2
    assert sum(endpoint == "primary" for endpoint, _ in worker_calls) == 4
    assert sum(endpoint == "replica" for endpoint, _ in worker_calls) == 8
    assert sum(endpoint == "primary-write" for endpoint, _ in worker_calls) == 4
