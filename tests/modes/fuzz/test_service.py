from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event, Lock

import pytest

from select_fuzz.artifacts import JsonlWriter, read_jsonl
from select_fuzz.config import FuzzConfig, NodeConfig, NodeRole
from select_fuzz.domain import RunRequest
from select_fuzz.generation.query import GeneratedQuery, WeightedQueryGenerator
from select_fuzz.generation.query_grammar import (
    GrammarColumn,
    GrammarSchema,
    GrammarTable,
)
from select_fuzz.modes.fuzz.materialization import FuzzDatabaseSchema, fuzz_database_name
from select_fuzz.modes.fuzz.query_pipeline import GenerationOutcome, InlineQueryPipeline
from select_fuzz.modes.fuzz.service import (
    FuzzModeService,
    _fair_worker_thread_scheduling,
    _tag_worker_session,
)


def test_fair_worker_scheduling_is_safe_for_non_lifo_overlap(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    current = [0.005]
    monkeypatch.setattr(
        "select_fuzz.modes.fuzz.service.sys.getswitchinterval",
        lambda: current[0],
    )
    monkeypatch.setattr(
        "select_fuzz.modes.fuzz.service.sys.setswitchinterval",
        lambda value: current.__setitem__(0, value),
    )
    monkeypatch.setattr(
        "select_fuzz.modes.fuzz.service._fair_scheduling_users",
        0,
    )
    monkeypatch.setattr(
        "select_fuzz.modes.fuzz.service._fair_scheduling_baseline",
        None,
    )
    first = _fair_worker_thread_scheduling()
    second = _fair_worker_thread_scheduling()

    first.__enter__()
    second.__enter__()
    first.__exit__(None, None, None)
    assert current[0] == 0.001
    second.__exit__(None, None, None)

    assert current[0] == 0.005


def test_fair_worker_scheduling_restores_after_exception(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    current = [0.005]
    monkeypatch.setattr(
        "select_fuzz.modes.fuzz.service.sys.getswitchinterval",
        lambda: current[0],
    )
    monkeypatch.setattr(
        "select_fuzz.modes.fuzz.service.sys.setswitchinterval",
        lambda value: current.__setitem__(0, value),
    )
    monkeypatch.setattr(
        "select_fuzz.modes.fuzz.service._fair_scheduling_users",
        0,
    )
    monkeypatch.setattr(
        "select_fuzz.modes.fuzz.service._fair_scheduling_baseline",
        None,
    )

    with pytest.raises(RuntimeError, match="boom"):
        with _fair_worker_thread_scheduling():
            raise RuntimeError("boom")

    assert current[0] == 0.005


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


class _LifecycleMaterializer:
    def __init__(self, events: list[tuple[str, str]], lock: Lock) -> None:
        self._events = events
        self._lock = lock

    def materialize(self, database: str, *, seed: int) -> FuzzDatabaseSchema:
        del seed
        with self._lock:
            self._events.append(("build", database))
        return _schema(database)


class _LifecycleService(FuzzModeService):
    def __init__(
        self,
        *args,  # type: ignore[no-untyped-def]
        lifecycle: list[tuple[str, str]],
        lifecycle_lock: Lock,
        run_stop: Event,
        **kwargs,  # type: ignore[no-untyped-def]
    ) -> None:
        super().__init__(*args, **kwargs)
        self._lifecycle = lifecycle
        self._lifecycle_lock = lifecycle_lock
        self._run_stop = run_stop

    def _record_worker_lifecycle(self, phase: str, database: str) -> None:
        with self._lifecycle_lock:
            self._lifecycle.append((phase, database))

    def _reader_loop(self, request, schema, database_ordinal, worker_id, node, endpoint, stop_event):  # type: ignore[no-untyped-def]
        del request, database_ordinal, worker_id, node, endpoint
        self._record_worker_lifecycle("worker_start", schema.database)
        if "_g1_" in schema.database:
            self._run_stop.set()
        stop_event.wait()
        self._record_worker_lifecycle("worker_stop", schema.database)

    def _writer_loop(self, request, schema, row_budget, database_ordinal, worker_id, stop_event):  # type: ignore[no-untyped-def]
        del request, row_budget, database_ordinal, worker_id
        self._record_worker_lifecycle("worker_start", schema.database)
        if "_g1_" in schema.database:
            self._run_stop.set()
        stop_event.wait()
        self._record_worker_lifecycle("worker_stop", schema.database)


class _FailingMaterializer:
    def __init__(self, calls: list[str], lock: Lock) -> None:
        self._calls = calls
        self._lock = lock

    def materialize(self, database: str, *, seed: int) -> FuzzDatabaseSchema:
        del seed
        with self._lock:
            self._calls.append(database)
        raise RuntimeError("simulated kernel setup failure")


class _SecondGenerationFailingMaterializer(_LifecycleMaterializer):
    def materialize(self, database: str, *, seed: int) -> FuzzDatabaseSchema:
        schema = super().materialize(database, seed=seed)
        if "_g1_" in database:
            raise RuntimeError("simulated second-generation setup failure")
        return schema


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


class _RecordingSession:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, sql: str) -> _EmptyCursor:
        self.sql.append(sql)
        return _EmptyCursor()


def test_worker_sessions_receive_observable_route_tags() -> None:
    session = _RecordingSession()

    _tag_worker_session(session, worker_kind="writer", endpoint="primary")  # type: ignore[arg-type]
    _tag_worker_session(session, worker_kind="reader", endpoint="primary")  # type: ignore[arg-type]
    _tag_worker_session(session, worker_kind="reader", endpoint="replica")  # type: ignore[arg-type]

    assert session.sql == [
        "SET @select_fuzz_worker = 'primary_writer'",
        "SET @select_fuzz_worker = 'primary_reader'",
        "SET @select_fuzz_worker = 'replica_reader'",
    ]


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

    def connection_id(self) -> int:
        return 71

    def abort(self) -> None:
        return None

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


class _PrefetchTicket:
    def __init__(
        self,
        pipeline: _PrefetchPipeline,
        reader_id: int,
        operation: int,
        seed: int,
    ) -> None:
        self._pipeline = pipeline
        self._reader_id = reader_id
        self._operation = operation
        self._seed = seed

    def result(self, stop_event: Event) -> GenerationOutcome:
        assert not stop_event.is_set()
        self._pipeline.events.append(("result", self._reader_id, self._operation))
        return GenerationOutcome(
            GeneratedQuery("SELECT value FROM fuzz_t0", self._seed, "test"),
            None,
            11,
            13,
        )


class _PrefetchPipeline:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def start(self) -> None:
        self.events.append(("start",))

    def register_database(self, ordinal, database, schema):  # type: ignore[no-untyped-def]
        del database, schema
        self.events.append(("register", ordinal))

    def submit(
        self,
        database_ordinal: int,
        reader_id: int,
        operation: int,
        *,
        seed: int,
    ) -> _PrefetchTicket:
        self.events.append(("submit", database_ordinal, reader_id, operation))
        return _PrefetchTicket(self, reader_id, operation, seed)

    def close(self) -> None:
        self.events.append(("close",))


class _PrefetchSession:
    def __init__(self, pipeline: _PrefetchPipeline, stop_event: Event) -> None:
        self._pipeline = pipeline
        self._stop_event = stop_event

    def connection_id(self) -> int:
        return 91

    def execute(self, sql: str) -> _EmptyCursor:
        if sql.startswith("SELECT"):
            assert ("submit", 0, 0, 1) in self._pipeline.events
            self._pipeline.events.append(("execute", sql))
            self._stop_event.set()
        return _EmptyCursor()

    def abort(self) -> None:
        return None

    def close(self) -> None:
        return None


class _PrefetchFactory:
    def __init__(self, pipeline: _PrefetchPipeline, stop_event: Event) -> None:
        self._pipeline = pipeline
        self._stop_event = stop_event

    @contextmanager
    def query_session(self, node, database):  # type: ignore[no-untyped-def]
        del node, database
        yield _PrefetchSession(self._pipeline, self._stop_event)

    control_session = query_session


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
    pipeline = InlineQueryPipeline(_QueryGenerator())
    pipeline.start()
    pipeline.register_database(0, "sf_f_reader", _schema("sf_f_reader").grammar_schema)
    service._query_pipeline = pipeline  # type: ignore[attr-defined]

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


def test_reader_prefetches_two_queries_before_executing_current(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    stop_event = Event()
    pipeline = _PrefetchPipeline()
    factory = _PrefetchFactory(pipeline, stop_event)
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
        query_pipeline_factory=lambda: pipeline,
        materializer_factory=lambda: _Materializer([], Lock()),
    )
    pipeline.start()
    pipeline.register_database(0, "sf_f_reader", _schema("sf_f_reader").grammar_schema)
    service._query_pipeline = pipeline  # type: ignore[attr-defined]

    service._reader_loop(  # type: ignore[attr-defined]
        RunRequest("run-fuzz-prefetch", "fuzz", 1, 1, None, 1),
        _schema("sf_f_reader"),
        0,
        0,
        primary,
        "primary",
        stop_event,
    )

    first_submit = pipeline.events.index(("submit", 0, 0, 0))
    first_result = pipeline.events.index(("result", 0, 0))
    next_submit = pipeline.events.index(("submit", 0, 0, 1))
    second_next_submit = pipeline.events.index(("submit", 0, 0, 2))
    execute = pipeline.events.index(("execute", "SELECT value FROM fuzz_t0"))
    assert first_submit < next_submit < first_result < second_next_submit < execute


def test_schema_refresh_stops_old_workers_before_building_the_next_batch(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    run_stop = Event()
    lifecycle: list[tuple[str, str]] = []
    lifecycle_lock = Lock()
    config = FuzzConfig(
        databases=2,
        writer_threads_per_database=1,
        reader_threads_per_database=3,
        initial_tables=1,
        initial_rows_per_table=100,
        max_rows_per_database=1000,
        schema_refresh_interval_seconds=0.02,
    )
    service = _LifecycleService(
        config=config,
        primary=NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        replica=NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307),
        factory=_NoopFactory(),
        records=JsonlWriter(tmp_path / "events.jsonl"),
        query_generator=WeightedQueryGenerator((("test", _QueryGenerator(), 1),)),
        materializer_factory=lambda: _LifecycleMaterializer(
            lifecycle,
            lifecycle_lock,
        ),
        lifecycle=lifecycle,
        lifecycle_lock=lifecycle_lock,
        run_stop=run_stop,
    )

    summary = service.run(
        RunRequest("run-fuzz-refresh", "fuzz", 9, 1, None, 1),
        run_stop,
    )

    generation_zero = {
        database for phase, database in lifecycle if phase == "build" and "_g0_" in database
    }
    generation_one = {
        database for phase, database in lifecycle if phase == "build" and "_g1_" in database
    }
    assert len(generation_zero) == 2
    assert len(generation_one) == 2
    assert generation_zero.isdisjoint(generation_one)
    last_generation_zero_build = max(
        index
        for index, event in enumerate(lifecycle)
        if event[0] == "build" and event[1] in generation_zero
    )
    first_generation_zero_worker = min(
        index
        for index, event in enumerate(lifecycle)
        if event[0] == "worker_start" and event[1] in generation_zero
    )
    last_generation_zero_worker_stop = max(
        index
        for index, event in enumerate(lifecycle)
        if event[0] == "worker_stop" and event[1] in generation_zero
    )
    first_generation_one_build = min(
        index
        for index, event in enumerate(lifecycle)
        if event[0] == "build" and event[1] in generation_one
    )
    assert last_generation_zero_build < first_generation_zero_worker
    assert last_generation_zero_worker_stop < first_generation_one_build
    assert summary.rounds_completed == 4
    events = read_jsonl(tmp_path / "events.jsonl")
    assert [
        event["generation"]
        for event in events
        if event["type"] == "fuzz_generation_ready"
    ] == [0, 1]


def test_generation_build_waits_for_all_failures_and_never_starts_workers(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    worker_calls: list[tuple[str, int]] = []
    service = _RecordingService(
        config=FuzzConfig(
            databases=2,
            writer_threads_per_database=1,
            reader_threads_per_database=3,
            initial_tables=1,
            initial_rows_per_table=100,
            max_rows_per_database=1000,
        ),
        primary=NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        replica=NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307),
        factory=_NoopFactory(),
        records=JsonlWriter(tmp_path / "events.jsonl"),
        query_generator=WeightedQueryGenerator((("test", _QueryGenerator(), 1),)),
        materializer_factory=lambda: _FailingMaterializer(calls, Lock()),
        worker_calls=worker_calls,
    )

    with pytest.raises(RuntimeError) as captured:
        service.run(
            RunRequest("run-fuzz-build-failure", "fuzz", 3, 1, None, 1),
            Event(),
        )

    assert len(calls) == 2
    assert worker_calls == []
    message = str(captured.value)
    assert "database[0]=sf_f_" in message
    assert "database[1]=sf_f_" in message
    assert message.count("RuntimeError: simulated kernel setup failure") == 2
    events = read_jsonl(tmp_path / "events.jsonl")
    failure = next(
        event for event in events if event["type"] == "fuzz_generation_failed"
    )
    assert len(failure["failures"]) == 2
    assert {item["database"] for item in failure["failures"]} == set(calls)
    for item in failure["failures"]:
        assert item["error_type"] == "RuntimeError"
        assert item["error"] == "simulated kernel setup failure"


def test_failed_replacement_batch_does_not_fall_back_to_old_workers(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    run_stop = Event()
    lifecycle: list[tuple[str, str]] = []
    lifecycle_lock = Lock()
    service = _LifecycleService(
        config=FuzzConfig(
            databases=2,
            writer_threads_per_database=1,
            reader_threads_per_database=3,
            initial_tables=1,
            initial_rows_per_table=100,
            max_rows_per_database=1000,
            schema_refresh_interval_seconds=0.02,
        ),
        primary=NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        replica=NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307),
        factory=_NoopFactory(),
        records=JsonlWriter(tmp_path / "events.jsonl"),
        query_generator=WeightedQueryGenerator((("test", _QueryGenerator(), 1),)),
        materializer_factory=lambda: _SecondGenerationFailingMaterializer(
            lifecycle,
            lifecycle_lock,
        ),
        lifecycle=lifecycle,
        lifecycle_lock=lifecycle_lock,
        run_stop=run_stop,
    )

    with pytest.raises(RuntimeError, match="generation build failed"):
        service.run(
            RunRequest("run-fuzz-no-fallback", "fuzz", 5, 1, None, 1),
            run_stop,
        )

    first_generation_one_build = min(
        index
        for index, event in enumerate(lifecycle)
        if event[0] == "build" and "_g1_" in event[1]
    )
    last_generation_zero_stop = max(
        index
        for index, event in enumerate(lifecycle)
        if event[0] == "worker_stop" and "_g0_" in event[1]
    )
    assert last_generation_zero_stop < first_generation_one_build
    assert not any(
        phase == "worker_start" and "_g1_" in database
        for phase, database in lifecycle
    )
    events = read_jsonl(tmp_path / "events.jsonl")
    assert any(event["type"] == "fuzz_run_failed" for event in events)
    assert not any(event["type"] == "fuzz_run_finished" for event in events)


def test_fuzz_database_names_change_by_generation_without_reusing_old_names() -> None:
    first = fuzz_database_name("run-refresh", 0, generation=0)
    second = fuzz_database_name("run-refresh", 0, generation=1)

    assert first != second
    assert "_g0_0" in first
    assert "_g1_0" in second
