from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event, Lock
import time

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
from select_fuzz.modes.fuzz.materialization import (
    FuzzDatabaseSchema,
    FuzzMaterializer,
    fuzz_database_name,
)
from select_fuzz.modes.fuzz.diagnostics import FuzzProcesslistCollector
from select_fuzz.modes.fuzz.forensics import (
    FuzzErrorAggregator,
    capture_exception_evidence,
)
from select_fuzz.modes.fuzz.query_pipeline import GenerationOutcome, InlineQueryPipeline
from select_fuzz.modes.fuzz.service import (
    FuzzModeService,
    _GenerationDatabase,
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


class _ConnectorInternalError(RuntimeError):
    errno = -1
    sqlstate = "HY000"


def _diagnostic_service(tmp_path, *, progress_sink=None):  # type: ignore[no-untyped-def]
    return FuzzModeService(
        config=FuzzConfig(
            databases=1,
            writer_threads_per_database=1,
            reader_threads_per_database=3,
            initial_tables=1,
            initial_rows_per_table=20,
            max_rows_per_database=100,
        ),
        primary=NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        replica=NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307),
        factory=_NoopFactory(),
        records=JsonlWriter(tmp_path / "events.jsonl"),
        query_generator=WeightedQueryGenerator((("test", _QueryGenerator(), 1),)),
        materializer_factory=lambda: _Materializer([], Lock()),
        progress_sink=progress_sink,
    )


def _internal_error_evidence() -> dict[str, object]:
    try:
        raise _ConnectorInternalError("Unread result found")
    except _ConnectorInternalError as error:
        evidence = capture_exception_evidence(error, "execute")
    evidence["connection_id"] = 71
    evidence["watchdog"] = {
        "timed_out": True,
        "kill_query_succeeded": True,
        "abort_attempted": True,
        "abort_succeeded": True,
        "completed": True,
    }
    return evidence


def test_error_forensics_writes_one_full_sample_for_new_fingerprint(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service = _diagnostic_service(tmp_path)
    request = RunRequest("run-error-sample", "fuzz", 1, 1, None, 7)
    evidence = _internal_error_evidence()

    for seed in (101, 102):
        service._record_error(  # type: ignore[attr-defined]
            request,
            "sf_f_case",
            "reader",
            0,
            "replica",
            seed,
            "SELECT 1",
            "_ConnectorInternalError:errno=-1:sqlstate=HY000",
            failure_evidence=evidence,
        )

    events = read_jsonl(tmp_path / "events.jsonl")
    samples = [event for event in events if event["type"] == "fuzz_error_sample"]
    operations = [event for event in events if event["type"] == "fuzz_operation_error"]
    assert len(samples) == 1
    assert len(operations) == 1
    assert samples[0]["fingerprint"] == operations[0]["fingerprint"]
    assert samples[0]["sql"] == "SELECT 1"
    assert samples[0]["evidence"]["failure_stage"] == "execute"
    assert samples[0]["evidence"]["exception"]["message"] == "Unread result found"
    assert samples[0]["evidence"]["connection_id"] == 71
    assert samples[0]["evidence"]["watchdog"]["abort_succeeded"] is True
    assert "_internal_error_evidence" in samples[0]["traceback"]
    assert service._counters.snapshot().errors == 2  # type: ignore[attr-defined]


def test_error_forensics_emits_representative_with_suppressed_count(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service = _diagnostic_service(tmp_path)
    current = [0]
    service._error_aggregator = FuzzErrorAggregator(  # type: ignore[attr-defined]
        clock_ns=lambda: current[0]
    )
    request = RunRequest("run-error-suppression", "fuzz", 1, 1, None, 7)
    evidence = _internal_error_evidence()

    for ordinal, now_ns in enumerate((0, 1_000_000_000, 2_000_000_000)):
        current[0] = now_ns
        service._record_error(  # type: ignore[attr-defined]
            request,
            "sf_f_case",
            "reader",
            0,
            "replica",
            ordinal,
            "SELECT 1",
            "_ConnectorInternalError:errno=-1:sqlstate=HY000",
            failure_evidence=evidence,
        )
    current[0] = 31_000_000_000
    service._record_error(  # type: ignore[attr-defined]
        request,
        "sf_f_case",
        "reader",
        0,
        "replica",
        4,
        "SELECT 2",
        "_ConnectorInternalError:errno=-1:sqlstate=HY000",
        failure_evidence=evidence,
    )

    operations = [
        event
        for event in read_jsonl(tmp_path / "events.jsonl")
        if event["type"] == "fuzz_operation_error"
    ]
    assert len(operations) == 2
    assert operations[0]["error"] == "_ConnectorInternalError:errno=-1:sqlstate=HY000"
    assert operations[0]["sql"] == "SELECT 1"
    assert operations[1]["suppressed_repeats"] == 2
    assert operations[1]["sql"] == "SELECT 2"


def test_stage_snapshot_writes_bounded_error_summary(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service = _diagnostic_service(tmp_path)
    request = RunRequest("run-error-summary", "fuzz", 1, 1, None, 7)
    service._record_error(  # type: ignore[attr-defined]
        request,
        "sf_f_case",
        "reader",
        0,
        "replica",
        101,
        "SELECT 1",
        "_ConnectorInternalError:errno=-1:sqlstate=HY000",
        failure_evidence=_internal_error_evidence(),
    )

    service._append_stage_snapshot(request, final=False)  # type: ignore[attr-defined]

    events = read_jsonl(tmp_path / "events.jsonl")
    snapshot = next(event for event in events if event["type"] == "fuzz_stage_snapshot")
    summary = next(event for event in events if event["type"] == "fuzz_error_summary")
    assert snapshot["errors_summary"] == summary["summary"]
    assert summary["summary"]["total_count"] == 1
    assert summary["summary"]["interval_count"] == 1
    assert len(summary["summary"]["top"]) == 1
    assert summary["summary"]["top"][0]["error_type"] == "_ConnectorInternalError"
    assert summary["summary"]["top"][0]["message"] == "Unread result found"
    assert "traceback_frames" not in summary["summary"]["top"][0]


def test_error_sample_distinguishes_uncollected_from_invisible_connection(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    service = _diagnostic_service(tmp_path)
    request = RunRequest("run-visibility-uncollected", "fuzz", 1, 1, None, 7)

    service._record_error(  # type: ignore[attr-defined]
        request,
        "sf_f_case",
        "reader",
        0,
        "replica",
        101,
        "SELECT 1",
        "_ConnectorInternalError:errno=-1:sqlstate=HY000",
        failure_evidence=_internal_error_evidence(),
    )

    sample = read_jsonl(tmp_path / "events.jsonl")[0]
    assert sample["evidence"]["mysql_visibility"] == {
        "visible": None,
        "reason": "processlist_sample_has_no_registered_connections",
    }


def test_error_sample_uses_fresh_processlist_connection_ids_without_persisting_them(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    service = _diagnostic_service(tmp_path)
    request = RunRequest("run-visibility-sampled", "fuzz", 1, 1, None, 7)
    replica = FuzzProcesslistCollector._empty_endpoint(1)
    replica["visible"] = 1
    replica["sleep"] = 1
    replica["_visible_connection_ids"] = (71,)
    service._latest_processlist = {  # type: ignore[attr-defined]
        "sampled_at_ns": time.monotonic_ns(),
        "endpoints": {
            "primary": FuzzProcesslistCollector._empty_endpoint(0),
            "replica": replica,
        },
    }

    service._record_error(  # type: ignore[attr-defined]
        request,
        "sf_f_case",
        "reader",
        0,
        "replica",
        101,
        "SELECT 1",
        "_ConnectorInternalError:errno=-1:sqlstate=HY000",
        failure_evidence=_internal_error_evidence(),
    )
    service._append_stage_snapshot(request, final=False)  # type: ignore[attr-defined]

    events = read_jsonl(tmp_path / "events.jsonl")
    sample = next(event for event in events if event["type"] == "fuzz_error_sample")
    snapshot = next(event for event in events if event["type"] == "fuzz_stage_snapshot")
    visibility = sample["evidence"]["mysql_visibility"]
    assert visibility["visible"] is True
    assert visibility["reason"] == "periodic_processlist_sample"
    assert "_visible_connection_ids" not in snapshot["processlist"]["endpoints"]["replica"]


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


class _ReplicaProbeFactory:
    def query_session(self, node, database):  # type: ignore[no-untyped-def]
        del node, database
        raise AssertionError("query session is not used by replica probes")

    @contextmanager
    def control_session(self, node, database):  # type: ignore[no-untyped-def]
        del node, database
        yield _RecordingSession()


class _ReplicaTimeoutMaterializer:
    def __init__(self) -> None:
        node = NodeConfig(role=NodeRole.CUSTOM_ON, host="replica")
        self._delegate = FuzzMaterializer(
            _ReplicaProbeFactory(),  # type: ignore[arg-type]
            node,
            node,
            FuzzConfig(
                initial_tables=1,
                initial_rows_per_table=20,
                max_rows_per_database=100,
            ),
            replica_sync_timeout_seconds=0.001,
            sleeper=lambda seconds: None,
        )

    def materialize(self, database: str, *, seed: int) -> FuzzDatabaseSchema:
        del seed
        self._delegate._wait_for_replica(database)
        raise AssertionError("replica timeout did not fire")


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


def test_stage_snapshot_adds_live_diagnostics_and_emits_chinese_status(tmp_path) -> None:  # type: ignore[no-untyped-def]
    emitted: list[str] = []
    records = JsonlWriter(tmp_path / "events.jsonl")
    service = FuzzModeService(
        config=FuzzConfig(
            databases=1,
            writer_threads_per_database=1,
            reader_threads_per_database=3,
            initial_tables=1,
            initial_rows_per_table=20,
            max_rows_per_database=100,
        ),
        primary=NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        replica=NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307),
        factory=_NoopFactory(),
        records=records,
        query_generator=WeightedQueryGenerator((("test", _QueryGenerator(), 1),)),
        materializer_factory=lambda: _Materializer([], Lock()),
        progress_sink=emitted.append,
    )
    pipeline = InlineQueryPipeline(_QueryGenerator())
    pipeline.start()
    pipeline.register_database(0, "sf_f_case", _schema("sf_f_case").grammar_schema)
    service._query_pipeline = pipeline  # type: ignore[attr-defined]
    service._runtime.set_phase("running", generation=0)  # type: ignore[attr-defined]
    service._telemetry.set_stage(  # type: ignore[attr-defined]
        "db0:reader-primary:0",
        "waiting_for_generated_sql",
    )

    service._append_stage_snapshot(  # type: ignore[attr-defined]
        RunRequest("run-fuzz-diagnostics", "fuzz", 1, 1, None, 1),
        final=False,
    )

    event = read_jsonl(tmp_path / "events.jsonl")[0]
    assert event["type"] == "fuzz_stage_snapshot"
    assert event["stages"] == {"waiting_for_generated_sql": 1}
    assert "durations" in event
    assert "stage_details" in event
    assert "worker_groups" in event
    assert event["counters"]["timeouts"] == 0
    assert event["pipeline"]["registered_databases"] == 1
    assert event["runtime"]["phase"] == "running"
    assert event["connections"] == {
        "total": 0,
        "groups": {},
        "registered": [],
        "truncated": 0,
    }
    assert event["processlist"]["endpoints"]["primary"]["visible"] == 0
    assert emitted and emitted[0].startswith("[fuzz状态]")
    assert "判断=" in emitted[0]
    pipeline.close()


def test_stage_snapshot_bounds_persisted_connection_details(tmp_path) -> None:  # type: ignore[no-untyped-def]
    records = JsonlWriter(tmp_path / "events.jsonl")
    service = FuzzModeService(
        config=FuzzConfig(
            databases=1,
            writer_threads_per_database=1,
            reader_threads_per_database=3,
            initial_tables=1,
            initial_rows_per_table=20,
            max_rows_per_database=100,
        ),
        primary=NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        replica=NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307),
        factory=_NoopFactory(),
        records=records,
        query_generator=WeightedQueryGenerator((("test", _QueryGenerator(), 1),)),
        materializer_factory=lambda: _Materializer([], Lock()),
    )
    for ordinal in range(5):
        service._runtime.register_connection(  # type: ignore[attr-defined]
            worker=f"db0:reader-replica:{ordinal}",
            endpoint="replica",
            worker_kind="reader",
            database="sf_f_case",
            connection_id=100 + ordinal,
        )

    service._append_stage_snapshot(  # type: ignore[attr-defined]
        RunRequest("run-fuzz-bounded-connections", "fuzz", 1, 1, None, 1),
        final=False,
    )

    event = read_jsonl(tmp_path / "events.jsonl")[-1]
    assert event["connections"]["total"] == 5
    assert event["connections"]["groups"] == {"replica_reader": 5}
    assert event["runtime"]["connections"] == 5
    assert event["runtime"]["connection_groups"] == event["connections"]["groups"]
    assert len(event["connections"]["registered"]) == 3
    assert event["connections"]["truncated"] == 2


def test_worker_connection_tracking_uses_exact_connection_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service = FuzzModeService(
        config=FuzzConfig(
            databases=1,
            writer_threads_per_database=1,
            reader_threads_per_database=3,
            initial_tables=1,
            initial_rows_per_table=20,
            max_rows_per_database=100,
        ),
        primary=NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        replica=NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307),
        factory=_NoopFactory(),
        records=JsonlWriter(tmp_path / "events.jsonl"),
        query_generator=WeightedQueryGenerator((("test", _QueryGenerator(), 1),)),
        materializer_factory=lambda: _Materializer([], Lock()),
    )
    session = _ErrorSession(Event())

    with service._track_worker_connection(  # type: ignore[attr-defined]
        session,
        worker="db0:reader-replica:0",
        endpoint="replica",
        worker_kind="reader",
        database="sf_f_case",
    ):
        connections = service._runtime.connections()  # type: ignore[attr-defined]
        assert len(connections) == 1
        assert connections[0].connection_id == 71
        service._append_stage_snapshot(  # type: ignore[attr-defined]
            RunRequest("run-fuzz-connection-snapshot", "fuzz", 1, 1, None, 1),
            final=False,
        )
        event = read_jsonl(tmp_path / "events.jsonl")[-1]
        registered = event["connections"]["registered"]
        assert registered[0]["worker"] == "db0:reader-replica:0"
        assert registered[0]["endpoint"] == "replica"
        assert registered[0]["connection_id"] == 71

    assert service._runtime.connections() == ()  # type: ignore[attr-defined]


class _BlockingProcesslistCollector:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()

    def collect(self) -> dict[str, object]:
        self.entered.set()
        self.release.wait(2)
        return {
            "sampled_at_ns": time.monotonic_ns(),
            "endpoints": {
                "primary": {},
                "replica": {},
            },
        }


def test_blocked_processlist_diagnostics_do_not_delay_run_shutdown(tmp_path) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    worker_calls: list[tuple[str, int]] = []
    collector = _BlockingProcesslistCollector()
    service = _RecordingService(
        config=FuzzConfig(
            databases=1,
            writer_threads_per_database=1,
            reader_threads_per_database=3,
            initial_tables=1,
            initial_rows_per_table=20,
            max_rows_per_database=100,
        ),
        primary=NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        replica=NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307),
        factory=_NoopFactory(),
        records=JsonlWriter(tmp_path / "events.jsonl"),
        query_generator=WeightedQueryGenerator((("test", _QueryGenerator(), 1),)),
        materializer_factory=lambda: _Materializer(calls, Lock()),
        progress_sink=lambda message: None,
        worker_calls=worker_calls,
    )
    service._processlist_collector = collector  # type: ignore[attr-defined]
    started = time.monotonic()
    try:
        service.run(
            RunRequest("run-fuzz-blocked-diagnostics", "fuzz", 1, 1, None, 1),
            Event(),
        )
        elapsed = time.monotonic() - started
        assert collector.entered.is_set()
        assert elapsed < 0.8
    finally:
        collector.release.set()


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


class _ScriptedStopEvent:
    """Stop without sleeping, while retaining the requested wait durations."""

    def __init__(self, *, stop_after_waits: int | None = None) -> None:
        self._stopped = False
        self._stop_after_waits = stop_after_waits
        self.waits: list[float | None] = []

    def is_set(self) -> bool:
        return self._stopped

    def set(self) -> None:
        self._stopped = True

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        if (
            self._stop_after_waits is not None
            and len(self.waits) >= self._stop_after_waits
        ):
            self._stopped = True
        return self._stopped


class _ScriptedSqlError(Exception):
    def __init__(self, errno: int) -> None:
        super().__init__(f"scripted errno={errno}")
        self.errno = errno
        self.sqlstate = "42000"


@dataclass(frozen=True)
class _ScriptedFetchFailure:
    error: Exception


class _FailingFetchCursor(_EmptyCursor):
    def __init__(self, error: Exception) -> None:
        self._error = error

    def fetchmany(self, size: int):  # type: ignore[no-untyped-def]
        del size
        raise self._error


class _ScriptedReaderSession:
    def __init__(
        self,
        actions: list[Exception | _ScriptedFetchFailure | None],
        stop_event: _ScriptedStopEvent,
    ) -> None:
        self._actions = actions
        self._stop_event = stop_event

    def execute(self, sql: str) -> _EmptyCursor:
        if not sql.startswith("SELECT"):
            return _EmptyCursor()
        if not self._actions:
            self._stop_event.set()
            return _EmptyCursor()
        error = self._actions.pop(0)
        if error is not None:
            if isinstance(error, _ScriptedFetchFailure):
                return _FailingFetchCursor(error.error)
            raise error
        return _EmptyCursor()

    def connection_id(self) -> int:
        return 72

    def abort(self) -> None:
        return None

    def close(self) -> None:
        return None


class _ScriptedReaderFactory:
    def __init__(
        self,
        actions: list[Exception | _ScriptedFetchFailure | None],
        stop_event: _ScriptedStopEvent,
    ) -> None:
        self._actions = actions
        self._stop_event = stop_event
        self.opens = 0

    @contextmanager
    def query_session(self, node, database):  # type: ignore[no-untyped-def]
        del node, database
        self.opens += 1
        yield _ScriptedReaderSession(self._actions, self._stop_event)


class _ElapsedStopEvent(_ScriptedStopEvent):
    def __init__(self, clock_ns: list[int], *, elapsed_ns: int) -> None:
        super().__init__(stop_after_waits=1)
        self._clock_ns = clock_ns
        self._elapsed_ns = elapsed_ns

    def wait(self, timeout: float | None = None) -> bool:
        interrupted = super().wait(timeout)
        self._clock_ns[0] += self._elapsed_ns
        return interrupted


def _reader_backoff_service(
    tmp_path,
    factory: _ScriptedReaderFactory,
) -> tuple[FuzzModeService, NodeConfig, InlineQueryPipeline]:  # type: ignore[no-untyped-def]
    primary = NodeConfig(role=NodeRole.CUSTOM_ON, host="primary")
    service = FuzzModeService(
        config=FuzzConfig(
            databases=1,
            writer_threads_per_database=1,
            reader_threads_per_database=3,
            initial_tables=1,
            initial_rows_per_table=100,
            max_rows_per_database=1000,
            compatibility_error_backoff_initial_seconds=0.01,
            compatibility_error_backoff_max_seconds=0.25,
        ),
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
    return service, primary, pipeline


def test_reader_compatibility_errors_back_off_on_one_long_lived_connection(tmp_path) -> None:  # type: ignore[no-untyped-def]
    stop_event = _ScriptedStopEvent(stop_after_waits=3)
    factory = _ScriptedReaderFactory(
        [_ScriptedSqlError(1064), _ScriptedSqlError(1064), _ScriptedSqlError(1064)],
        stop_event,
    )
    service, primary, pipeline = _reader_backoff_service(tmp_path, factory)

    try:
        service._reader_loop(  # type: ignore[attr-defined]
            RunRequest("run-fuzz-reader-backoff", "fuzz", 1, 1, None, 1),
            _schema("sf_f_reader"),
            0,
            0,
            primary,
            "primary",
            stop_event,
        )
    finally:
        pipeline.close()

    assert stop_event.waits == [0.01, 0.02, 0.04]
    assert factory.opens == 1
    counters = service._counters.snapshot()  # type: ignore[attr-defined]
    assert counters.errors == 3
    assert counters.reconnects == 0
    telemetry = service._telemetry.snapshot()  # type: ignore[attr-defined]
    assert telemetry["stages"] == {"compatibility_error_backoff": 1}
    assert telemetry["durations"]["compatibility_error_backoff_ns"]["count"] == 3
    assert telemetry["durations"]["compatibility_error_backoff_ns"]["max_ns"] >= 0


def test_reader_success_resets_compatibility_error_backoff(tmp_path) -> None:  # type: ignore[no-untyped-def]
    stop_event = _ScriptedStopEvent(stop_after_waits=2)
    factory = _ScriptedReaderFactory(
        [_ScriptedSqlError(1064), None, _ScriptedSqlError(1064)],
        stop_event,
    )
    service, primary, pipeline = _reader_backoff_service(tmp_path, factory)

    try:
        service._reader_loop(  # type: ignore[attr-defined]
            RunRequest("run-fuzz-reader-backoff-reset", "fuzz", 1, 1, None, 1),
            _schema("sf_f_reader"),
            0,
            0,
            primary,
            "primary",
            stop_event,
        )
    finally:
        pipeline.close()

    assert stop_event.waits == [0.01, 0.01]


def test_reader_fetch_compatibility_error_uses_the_same_backoff(tmp_path) -> None:  # type: ignore[no-untyped-def]
    stop_event = _ScriptedStopEvent(stop_after_waits=1)
    factory = _ScriptedReaderFactory(
        [_ScriptedFetchFailure(_ScriptedSqlError(1235))],
        stop_event,
    )
    service, primary, pipeline = _reader_backoff_service(tmp_path, factory)

    try:
        service._reader_loop(  # type: ignore[attr-defined]
            RunRequest("run-fuzz-reader-fetch-backoff", "fuzz", 1, 1, None, 1),
            _schema("sf_f_reader"),
            0,
            0,
            primary,
            "primary",
            stop_event,
        )
    finally:
        pipeline.close()

    assert stop_event.waits == [0.01]
    assert factory.opens == 1
    assert service._counters.snapshot().errors == 1  # type: ignore[attr-defined]


def test_reader_compatibility_backoff_records_interrupted_actual_wait_time(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    clock_ns = [0]
    monkeypatch.setattr(
        "select_fuzz.modes.fuzz.service.time.monotonic_ns",
        lambda: clock_ns[0],
    )
    stop_event = _ElapsedStopEvent(clock_ns, elapsed_ns=7_000_000)
    factory = _ScriptedReaderFactory([_ScriptedSqlError(1064)], stop_event)
    service, primary, pipeline = _reader_backoff_service(tmp_path, factory)

    try:
        service._reader_loop(  # type: ignore[attr-defined]
            RunRequest("run-fuzz-reader-backoff-duration", "fuzz", 1, 1, None, 1),
            _schema("sf_f_reader"),
            0,
            0,
            primary,
            "primary",
            stop_event,
        )
    finally:
        pipeline.close()

    duration = service._telemetry.snapshot()["durations"][  # type: ignore[attr-defined]
        "compatibility_error_backoff_ns"
    ]
    assert stop_event.waits == [0.01]
    assert duration == {"count": 1, "total_ns": 7_000_000, "max_ns": 7_000_000}


@pytest.mark.parametrize("errno", (1213, 1205, 1690))
def test_reader_noncompatibility_errors_do_not_use_compatibility_backoff(
    tmp_path,
    errno: int,
) -> None:  # type: ignore[no-untyped-def]
    stop_event = _ScriptedStopEvent()
    factory = _ScriptedReaderFactory([_ScriptedSqlError(errno)], stop_event)
    service, primary, pipeline = _reader_backoff_service(tmp_path, factory)

    try:
        service._reader_loop(  # type: ignore[attr-defined]
            RunRequest("run-fuzz-reader-noncompat", "fuzz", 1, 1, None, 1),
            _schema("sf_f_reader"),
            0,
            0,
            primary,
            "primary",
            stop_event,
        )
    finally:
        pipeline.close()

    assert stop_event.waits == []


def test_reader_timeout_does_not_use_compatibility_backoff(tmp_path) -> None:  # type: ignore[no-untyped-def]
    stop_event = _ScriptedStopEvent()
    factory = _ScriptedReaderFactory([TimeoutError("scripted timeout")], stop_event)
    service, primary, pipeline = _reader_backoff_service(tmp_path, factory)

    try:
        service._reader_loop(  # type: ignore[attr-defined]
            RunRequest("run-fuzz-reader-timeout", "fuzz", 1, 1, None, 1),
            _schema("sf_f_reader"),
            0,
            0,
            primary,
            "primary",
            stop_event,
        )
    finally:
        pipeline.close()

    assert stop_event.waits == []


def test_reader_connection_loss_reconnects_without_compatibility_backoff(tmp_path) -> None:  # type: ignore[no-untyped-def]
    stop_event = _ScriptedStopEvent(stop_after_waits=1)
    factory = _ScriptedReaderFactory([_ScriptedSqlError(2013)], stop_event)
    service, primary, pipeline = _reader_backoff_service(tmp_path, factory)

    try:
        service._reader_loop(  # type: ignore[attr-defined]
            RunRequest("run-fuzz-reader-lost", "fuzz", 1, 1, None, 1),
            _schema("sf_f_reader"),
            0,
            0,
            primary,
            "primary",
            stop_event,
        )
    finally:
        pipeline.close()

    assert stop_event.waits == [0.25]
    counters = service._counters.snapshot()  # type: ignore[attr-defined]
    assert counters.connection_losses == 1
    assert counters.reconnects == 1
    assert "compatibility_error_backoff_ns" not in service._telemetry.snapshot()["durations"]  # type: ignore[attr-defined]


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


class _RejectingTicket:
    def result(self, stop_event: Event) -> GenerationOutcome:
        del stop_event
        return GenerationOutcome(None, "CandidateRejected", 1, 1)


class _RejectingPipeline(_PrefetchPipeline):
    def submit(
        self,
        database_ordinal: int,
        reader_id: int,
        operation: int,
        *,
        seed: int,
    ) -> _RejectingTicket:
        del database_ordinal, reader_id, operation, seed
        return _RejectingTicket()


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


def test_prewarm_rejection_limit_is_reported_in_chinese(tmp_path) -> None:  # type: ignore[no-untyped-def]
    schema = _schema("sf_f_rejected")
    service = FuzzModeService(
        config=FuzzConfig(
            databases=1,
            writer_threads_per_database=1,
            reader_threads_per_database=3,
            initial_tables=1,
            initial_rows_per_table=100,
            max_rows_per_database=1000,
        ),
        primary=NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        replica=NodeConfig(
            role=NodeRole.CUSTOM_ON,
            host="replica",
            port=3307,
        ),
        factory=_NoopFactory(),
        records=JsonlWriter(tmp_path / "events.jsonl"),
        query_generator=WeightedQueryGenerator((("test", _QueryGenerator(), 1),)),
        materializer_factory=lambda: _Materializer([], Lock()),
    )
    service._query_pipeline = _RejectingPipeline()
    request = RunRequest("run-fuzz-prewarm-rejected", "fuzz", 7, 1, None, 1)

    with pytest.raises(
        RuntimeError,
        match="尝试 100 次后仍无法为读线程预生成查询",
    ):
        service._prewarm_generation(
            request,
            (_GenerationDatabase(0, schema.database, 7, schema),),
            Event(),
        )


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


def test_reader_prefetches_three_queries_before_executing_current(
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
    third_next_submit = pipeline.events.index(("submit", 0, 0, 3))
    execute = pipeline.events.index(("execute", "SELECT value FROM fuzz_t0"))
    assert (
        first_submit
        < next_submit
        < second_next_submit
        < first_result
        < third_next_submit
        < execute
    )


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
    assert "fuzz 批次创建失败：" in message
    assert "数据库[0]=sf_f_" in message
    assert "数据库[1]=sf_f_" in message
    assert (
        message.count(
            "异常类型=RuntimeError，原始错误=simulated kernel setup failure"
        )
        == 2
    )
    events = read_jsonl(tmp_path / "events.jsonl")
    failure = next(
        event for event in events if event["type"] == "fuzz_generation_failed"
    )
    assert len(failure["failures"]) == 2
    assert {item["database"] for item in failure["failures"]} == set(calls)
    for item in failure["failures"]:
        assert item["error_type"] == "RuntimeError"
        assert item["error"] == "simulated kernel setup failure"


def test_replica_timeout_keeps_legacy_machine_event_text(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service = FuzzModeService(
        config=FuzzConfig(
            databases=1,
            writer_threads_per_database=1,
            reader_threads_per_database=3,
            initial_tables=1,
            initial_rows_per_table=20,
            max_rows_per_database=100,
        ),
        primary=NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        replica=NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307),
        factory=_NoopFactory(),
        records=JsonlWriter(tmp_path / "events.jsonl"),
        query_generator=WeightedQueryGenerator((("test", _QueryGenerator(), 1),)),
        materializer_factory=_ReplicaTimeoutMaterializer,
    )

    with pytest.raises(RuntimeError) as captured:
        service.run(
            RunRequest("run-fuzz-replica-timeout", "fuzz", 3, 1, None, 1),
            Event(),
        )

    assert "等待备节点同步超时" in str(captured.value)
    failure_details = captured.value.failures[0]  # type: ignore[attr-defined]
    assert len(failure_details) == 4
    _ordinal, display_database, error_type, display_error = failure_details
    assert error_type == "TimeoutError"
    assert "等待备节点同步超时" in display_error
    events = read_jsonl(tmp_path / "events.jsonl")
    event = next(
        item for item in events if item["type"] == "fuzz_generation_failed"
    )
    failure = event["failures"][0]
    assert failure["error_type"] == "TimeoutError"
    assert failure["database"] == display_database
    assert failure["error"] == (
        "replica synchronization timeout after 0.001 seconds; "
        f"database={failure['database']}; replication marker not visible"
    )


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

    with pytest.raises(RuntimeError, match="fuzz 批次创建失败"):
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
