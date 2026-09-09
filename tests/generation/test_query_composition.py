from dataclasses import dataclass
import re
import sqlite3

from select_fuzz.generation.query import (
    GeneratedQuery,
    QueryGenerationContext,
    WeightedQueryGenerator,
)
from select_fuzz.generation.query.grammar import RandomGrammarQueryGenerator
from select_fuzz.generation.query.load_shaped import LoadShapedQueryGenerator
from select_fuzz.generation.query_grammar import (
    GrammarColumn,
    GrammarSchema,
    GrammarTable,
)
from select_fuzz.modes.fuzz import query_generation


_EXPECTED_FUZZ_EXCLUDED_FAMILIES = frozenset({"json", "fulltext", "spatial"})
# random.Random(0).randrange(100) == 49; grammar at the 50/50 boundary.
_GRAMMAR_SEED = 0
# random.Random(88).randrange(100) == 50; load-shaped at 50/50, grammar at 60/40.
_LOAD_BOUNDARY_SEED = 88


@dataclass
class _GrammarCandidate:
    sql: str


class _RecordingGrammarGenerator:
    def __init__(self) -> None:
        self.calls: list[tuple[object, int, frozenset[str]]] = []

    def generate(
        self,
        schema: object,
        *,
        seed: int,
        excluded_families: frozenset[str] = frozenset(),
    ) -> _GrammarCandidate:
        self.calls.append((schema, seed, excluded_families))
        return _GrammarCandidate("SELECT 1")


@dataclass
class _StubGenerator:
    name: str

    def generate(self, context: QueryGenerationContext, *, seed: int) -> GeneratedQuery:
        del context
        return GeneratedQuery(
            sql=f"SELECT '{self.name}'",
            seed=seed,
            generator=self.name,
            tags=frozenset({self.name}),
        )


def _load_shaped_schema() -> GrammarSchema:
    return GrammarSchema(
        (
            GrammarTable(
                "fuzz_t0",
                (
                    GrammarColumn("id", "BIGINT"),
                    GrammarColumn("tenant_id", "BIGINT"),
                    GrammarColumn("amount", "BIGINT"),
                    GrammarColumn("status", "INT"),
                    GrammarColumn("payload", "VARCHAR(32)"),
                ),
                (),
            ),
        )
    )


def test_weighted_query_generator_selects_per_call_deterministically() -> None:
    generator = WeightedQueryGenerator(
        (("grammar", _StubGenerator("grammar"), 50), ("load_shaped", _StubGenerator("load"), 50))
    )
    context = QueryGenerationContext(database="sf_f_case", schema=None)

    first = [generator.generate(context, seed=seed).generator for seed in range(200)]
    second = [generator.generate(context, seed=seed).generator for seed in range(200)]

    assert first == second
    assert 70 <= first.count("grammar") <= 130
    assert 70 <= first.count("load") <= 130


def test_random_grammar_generator_forwards_immutable_excluded_families() -> None:
    grammar = _RecordingGrammarGenerator()
    excluded_families = frozenset({"json", "fulltext", "spatial"})
    generator = RandomGrammarQueryGenerator(
        grammar, excluded_families=excluded_families  # type: ignore[arg-type]
    )
    schema = object()

    query = generator.generate(QueryGenerationContext("sf_f_case", schema), seed=41)

    assert grammar.calls[0][:2] == (schema, 41)
    assert grammar.calls[0][2] is excluded_families
    assert query == GeneratedQuery(
        "SELECT 1",
        41,
        "grammar",
        frozenset({"grammar_random"}),
    )


def test_fuzz_query_factory_excludes_unavailable_grammar_families(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    grammar = _RecordingGrammarGenerator()
    configured_max_tables: list[int] = []

    def build_grammar_generator(*, config: object) -> _RecordingGrammarGenerator:
        configured_max_tables.append(config.max_tables_per_query_block)  # type: ignore[attr-defined]
        return grammar

    monkeypatch.setattr(
        query_generation,
        "GrammarQueryGenerator",
        build_grammar_generator,
    )
    assert (
        query_generation.FUZZ_EXCLUDED_GRAMMAR_FAMILIES
        == _EXPECTED_FUZZ_EXCLUDED_FAMILIES
    )
    schema = _load_shaped_schema()
    context = QueryGenerationContext("sf_f_case", schema)
    generator = query_generation.build_fuzz_query_generator(3)

    query = generator.generate(context, seed=_GRAMMAR_SEED)
    load_query = generator.generate(context, seed=_LOAD_BOUNDARY_SEED)

    assert configured_max_tables == [3]
    assert grammar.calls == [
        (schema, _GRAMMAR_SEED, _EXPECTED_FUZZ_EXCLUDED_FAMILIES)
    ]
    assert query == GeneratedQuery(
        "SELECT 1",
        _GRAMMAR_SEED,
        "grammar",
        frozenset({"grammar_random"}),
    )
    assert load_query == LoadShapedQueryGenerator().generate(
        context,
        seed=_LOAD_BOUNDARY_SEED,
    )


def test_load_shaped_queries_use_pq_supported_expressions_and_subqueries() -> None:
    context = QueryGenerationContext("sf_f_case", _load_shaped_schema())
    generator = LoadShapedQueryGenerator()
    queries = tuple(generator.generate(context, seed=seed) for seed in range(100))
    forbidden = re.compile(
        r"\b(?:BIT_XOR|CRC32|OVER|WINDOW|HAVING|GROUP_CONCAT|SHA2)\b"
        r"|\bIN\s*\(\s*SELECT\b",
        re.IGNORECASE,
    )

    for query in queries:
        assert forbidden.search(query.sql) is None, query.sql
        assert query == generator.generate(context, seed=query.seed)
        assert "window" not in query.tags
    assert {"scan", "join", "aggregate", "group", "sort"} <= set().union(
        *(query.tags for query in queries)
    )


def test_load_shaped_join_fanout_stays_bounded_for_one_large_tenant() -> None:
    context = QueryGenerationContext("sf_f_case", _load_shaped_schema())
    generator = LoadShapedQueryGenerator()
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE fuzz_t0 (id INTEGER PRIMARY KEY, tenant_id INTEGER, "
            "amount INTEGER, status INTEGER, payload TEXT)"
        )
        connection.executemany(
            "INSERT INTO fuzz_t0 VALUES (?, 1, 1, 0, 'payload')",
            ((row_id,) for row_id in range(1, 501)),
        )
        joins = tuple(
            query
            for seed in range(100)
            if "join" in (query := generator.generate(context, seed=seed)).tags
        )
        assert joins
        for query in joins:
            row_count = connection.execute(query.sql).fetchone()[0]
            assert 0 < row_count <= 500 * 9, query.sql
