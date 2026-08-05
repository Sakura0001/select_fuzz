from __future__ import annotations

from threading import Event

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


def test_pipeline_rejects_more_than_one_outstanding_query_per_reader() -> None:
    pipeline = ProcessQueryPipeline(
        process_count=1,
        max_tables_per_query_block=1,
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


def test_auto_process_count_is_bounded_by_databases_cpu_and_config() -> None:
    assert resolve_query_generator_processes(0, databases=12, cpu_count=8) == 8
    assert resolve_query_generator_processes(3, databases=12, cpu_count=8) == 3
    assert resolve_query_generator_processes(20, databases=4, cpu_count=8) == 4
