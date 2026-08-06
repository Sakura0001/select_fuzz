from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from select_fuzz.generation.query import (
    GeneratedQuery,
    QueryGenerationContext,
    WeightedQueryGenerator,
)
from select_fuzz.generation.query.grammar import RandomGrammarQueryGenerator
from select_fuzz.generation.query.load_shaped import LoadShapedQueryGenerator
from select_fuzz.generation.query_grammar import (
    GrammarColumn,
    GrammarQueryConfig,
    GrammarQueryGenerator,
    GrammarSchema,
    GrammarTable,
)
from select_fuzz.modes.fuzz.query_pipeline import (
    InlineQueryPipeline,
    ProcessQueryPipeline,
    resolve_query_generator_processes,
)


def _schema() -> GrammarSchema:
    return GrammarSchema(
        (
            GrammarTable(
                "fuzz_t0",
                (
                    GrammarColumn("id", "BIGINT"),
                    GrammarColumn("tenant_id", "BIGINT"),
                    GrammarColumn("amount", "BIGINT"),
                    GrammarColumn("payload", "VARCHAR(32)"),
                ),
                (),
            ),
        )
    )


def _schema_with_table(table: str) -> GrammarSchema:
    base = _schema().tables[0]
    return GrammarSchema((GrammarTable(table, base.columns, base.indexes),))


class _RecordingGenerator:
    def __init__(self) -> None:
        self.seeds: list[int] = []

    def generate(
        self,
        context: QueryGenerationContext,
        *,
        seed: int,
    ) -> GeneratedQuery:
        self.seeds.append(seed)
        return GeneratedQuery(
            f"SELECT {seed} FROM `{context.database}`.`fuzz_t0`",
            seed,
            "recording",
        )


class _RecordingQueue:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def put(self, message: object) -> None:
        self.messages.append(message)


class _RollbackQueue(_RecordingQueue):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False
        self.joined = False

    def put_nowait(self, message: object) -> None:
        self.put(message)

    def close(self) -> None:
        self.closed = True

    def join_thread(self) -> None:
        self.joined = True


class _RollbackEvent:
    def __init__(self) -> None:
        self.is_set = False

    def set(self) -> None:
        self.is_set = True


class _RollbackProcess:
    def __init__(self, *, fail_start: bool) -> None:
        self._fail_start = fail_start
        self.started = False
        self.terminated = False
        self.joined = False
        self.exitcode: int | None = None
        self.name = "rollback-process"

    def start(self) -> None:
        if self._fail_start:
            raise RuntimeError("injected process start failure")
        self.started = True

    def is_alive(self) -> bool:
        return self.started and not self.terminated

    def terminate(self) -> None:
        self.terminated = True

    def join(self, timeout: float | None = None) -> None:
        del timeout
        self.joined = True


class _FailingSpawnContext:
    def __init__(self) -> None:
        self.queues: list[_RollbackQueue] = []
        self.processes: list[_RollbackProcess] = []

    def Event(self) -> _RollbackEvent:  # noqa: N802
        return _RollbackEvent()

    def Queue(self) -> _RollbackQueue:  # noqa: N802
        queue = _RollbackQueue()
        self.queues.append(queue)
        return queue

    def Process(self, **kwargs: object) -> _RollbackProcess:  # noqa: N802
        del kwargs
        process = _RollbackProcess(fail_start=len(self.processes) == 1)
        self.processes.append(process)
        return process


def _production_generator() -> WeightedQueryGenerator:
    return WeightedQueryGenerator(
        (
            (
                "grammar",
                RandomGrammarQueryGenerator(
                    GrammarQueryGenerator(
                        config=GrammarQueryConfig(max_tables_per_query_block=1)
                    )
                ),
                50,
            ),
            ("load_shaped", LoadShapedQueryGenerator(), 50),
        )
    )


def test_inline_pipeline_preserves_seed_and_query_identity() -> None:
    generator = _RecordingGenerator()
    pipeline = InlineQueryPipeline(generator)
    pipeline.start()
    pipeline.register_database(3, "sf_f_case", _schema())

    outcome = pipeline.submit(3, 7, 11, seed=991).result(Event())

    assert outcome.query == GeneratedQuery(
        "SELECT 991 FROM `sf_f_case`.`fuzz_t0`",
        991,
        "recording",
    )
    assert outcome.error_type is None
    assert generator.seeds == [991]
    assert outcome.compute_ns >= 0
    assert outcome.wait_ns >= 0
    pipeline.close()


def test_process_pipeline_matches_inline_production_sql_and_stops_children() -> None:
    seeds = (7, 11, 29, 41, 97, 101)
    expected_generator = _production_generator()
    expected = tuple(
        expected_generator.generate(
            QueryGenerationContext("sf_f_case", _schema()),
            seed=seed,
        )
        for seed in seeds
    )
    pipeline = ProcessQueryPipeline(
        process_count=1,
        max_tables_per_query_block=1,
        reader_keys=tuple((0, reader_id) for reader_id in range(len(seeds))),
        shutdown_timeout_seconds=2,
    )
    pipeline.start()
    pipeline.register_database(0, "sf_f_case", _schema())

    actual = tuple(
        pipeline.submit(0, reader_id, 0, seed=seed).result(Event()).query
        for reader_id, seed in enumerate(seeds)
    )

    assert actual == expected
    pipeline.close()
    assert pipeline.alive_processes == 0


def test_process_results_use_one_direct_response_queue_per_reader() -> None:
    pipeline = ProcessQueryPipeline(
        process_count=4,
        max_tables_per_query_block=1,
        reader_keys=tuple((0, reader_id) for reader_id in range(16)),
        shutdown_timeout_seconds=2,
    )
    pipeline.start()
    pipeline.register_database(0, "sf_f_case", _schema())
    tickets = [
        pipeline.submit(0, reader_id, 0, seed=reader_id + 100)
        for reader_id in range(16)
    ]

    try:
        response_queues = pipeline._response_queues  # type: ignore[attr-defined]
        assert len(response_queues) == 16
        assert len({id(queue) for queue in response_queues.values()}) == 16
        with ThreadPoolExecutor(max_workers=len(tickets)) as pool:
            outcomes = tuple(
                pool.map(lambda ticket: ticket.result(Event()), tickets)
            )
        assert all(outcome.query is not None for outcome in outcomes)
    finally:
        pipeline.close()


def test_process_pipeline_rolls_back_partial_start_failure() -> None:
    context = _FailingSpawnContext()
    pipeline = ProcessQueryPipeline(
        process_count=2,
        max_tables_per_query_block=1,
        reader_keys=((0, 0), (0, 1)),
    )
    pipeline._context = context  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="injected process start failure"):
        pipeline.start()

    assert context.processes[0].terminated
    assert context.processes[0].joined
    assert all(queue.closed and queue.joined for queue in context.queues)
    assert pipeline.alive_processes == 0


def test_process_pipeline_broadcasts_schema_and_balances_one_database_readers() -> None:
    pipeline = ProcessQueryPipeline(
        process_count=4,
        max_tables_per_query_block=1,
        reader_keys=tuple((0, reader_id) for reader_id in range(4)),
    )
    queues = [_RecordingQueue() for _ in range(4)]
    pipeline._request_queues = queues  # type: ignore[attr-defined]
    pipeline._response_queues = {  # type: ignore[attr-defined]
        (0, reader_id): _RecordingQueue() for reader_id in range(4)
    }
    pipeline._started = True  # type: ignore[attr-defined]

    pipeline.register_database(0, "sf_f_case", _schema())

    assert [len(queue.messages) for queue in queues] == [1, 1, 1, 1]
    for queue in queues:
        queue.messages.clear()

    pipeline.replace_database(0, "sf_f_replaced", _schema())

    assert [len(queue.messages) for queue in queues] == [1, 1, 1, 1]
    for queue in queues:
        queue.messages.clear()

    tickets = [
        pipeline.submit(0, reader_id, 0, seed=reader_id)
        for reader_id in range(4)
    ]

    assert len(tickets) == 4
    assert [len(queue.messages) for queue in queues] == [1, 1, 1, 1]


def test_pipeline_rejects_more_than_one_outstanding_query_per_reader() -> None:
    pipeline = ProcessQueryPipeline(
        process_count=1,
        max_tables_per_query_block=1,
        reader_keys=((0, 2),),
        shutdown_timeout_seconds=2,
    )
    pipeline.start()
    pipeline.register_database(0, "sf_f_case", _schema())
    first = pipeline.submit(0, 2, 0, seed=1)

    try:
        pipeline.submit(0, 2, 1, seed=2)
    except RuntimeError as error:
        assert "outstanding" in str(error)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("second outstanding generation request was accepted")

    first.result(Event())
    pipeline.close()


def test_direct_reader_channel_discards_a_cancelled_job_response() -> None:
    pipeline = ProcessQueryPipeline(
        process_count=1,
        max_tables_per_query_block=1,
        reader_keys=((0, 0),),
        shutdown_timeout_seconds=2,
    )
    pipeline.start()
    pipeline.register_database(0, "sf_f_case", _schema())
    pipeline.submit(0, 0, 0, seed=101)
    pipeline.cancel_reader(0, 0)

    current = pipeline.submit(0, 0, 1, seed=202).result(Event()).query

    assert current is not None
    assert current.seed == 202
    pipeline.close()


def test_inline_pipeline_replaces_a_drained_database_context() -> None:
    generator = _RecordingGenerator()
    pipeline = InlineQueryPipeline(generator)
    pipeline.start()
    pipeline.register_database(0, "sf_f_g0", _schema())

    first = pipeline.submit(0, 1, 0, seed=1)
    try:
        pipeline.replace_database(0, "sf_f_g1", _schema())
    except RuntimeError as error:
        assert "outstanding" in str(error)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("active database context was replaced")

    first.result(Event())
    pipeline.replace_database(0, "sf_f_g1", _schema())
    outcome = pipeline.submit(0, 1, 1, seed=2).result(Event())

    assert outcome.query is not None
    assert "`sf_f_g1`.`fuzz_t0`" in outcome.query.sql
    pipeline.close()


def test_process_pipeline_replaces_schema_without_restarting_children() -> None:
    pipeline = ProcessQueryPipeline(
        process_count=1,
        max_tables_per_query_block=1,
        reader_keys=((0, 0),),
        shutdown_timeout_seconds=2,
    )
    pipeline.start()
    pipeline.register_database(0, "sf_f_g0", _schema_with_table("fuzz_old"))
    old_query = pipeline.submit(0, 0, 0, seed=22).result(Event()).query

    pipeline.replace_database(0, "sf_f_g1", _schema_with_table("fuzz_new"))
    new_query = pipeline.submit(0, 0, 0, seed=22).result(Event()).query

    assert old_query is not None and "`fuzz_old`" in old_query.sql
    assert new_query is not None and "`fuzz_new`" in new_query.sql
    assert pipeline.alive_processes == 1
    pipeline.close()


def test_auto_process_count_is_bounded_by_readers_cpu_and_config() -> None:
    assert resolve_query_generator_processes(0, reader_workers=144, cpu_count=64) == 32
    assert resolve_query_generator_processes(3, reader_workers=144, cpu_count=8) == 3
    assert resolve_query_generator_processes(20, reader_workers=12, cpu_count=8) == 12
