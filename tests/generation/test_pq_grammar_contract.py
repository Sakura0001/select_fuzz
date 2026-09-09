from __future__ import annotations

import re

import pytest

from select_fuzz.generation.function_registry import DETERMINISTIC_FUNCTION_SIGNATURES
from select_fuzz.generation.query_grammar import (
    CandidateRejected,
    GrammarColumn,
    GrammarQueryGenerator,
    GrammarSchema,
    GrammarTable,
    SelectGrammar,
    TypeFamily,
)


def _schema() -> GrammarSchema:
    return GrammarSchema((GrammarTable("t", (GrammarColumn("id", "INT"),)),))


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT 1",
        "VALUES ROW(1)",
        "TABLE t",
        "SELECT id FROM t LIMIT 1",
        "SELECT id FROM t ORDER BY id LIMIT 0",
        "SELECT ROW_NUMBER() OVER () FROM t",
        "SELECT id, COUNT(*) FROM t GROUP BY id WITH ROLLUP",
        "SELECT SUM(DISTINCT id) FROM t",
        "SELECT CONCAT(id, 'x') FROM t",
        "SELECT JSON_ARRAY(id) FROM t",
        "SELECT BIT_AND(id) FROM t",
        "SELECT CAST(id AS JSON) FROM t",
        "SELECT BINARY id FROM t",
        "SELECT BINARY(id) FROM t",
        "SELECT CAST(id AS BINARY) FROM t",
        "SELECT CAST(id AS BINARY(8)) FROM t",
        "SELECT id FROM t WHERE id IN (SELECT id FROM t)",
        "SELECT id FROM t WHERE id = ALL (SELECT id FROM t)",
        "SELECT id FROM t WHERE id IN ((SELECT id FROM t))",
        "SELECT ((SELECT id FROM t ORDER BY id LIMIT 1)) FROM t",
        "SELECT id FROM t WHERE EXISTS (SELECT id FROM t)",
        "SELECT id FROM t LEFT JOIN t AS other ON t.id = other.id",
        "SELECT id FROM t NATURAL JOIN t AS other",
        "SELECT id FROM t WHERE 1 = 0",
        "SELECT /*+ NO_PARALLEL */ id FROM t",
        "SELECT id FROM t PARTITION (p0, p1)",
    ),
)
def test_custom_grammar_cannot_bypass_pq_contract(sql: str) -> None:
    generator = GrammarQueryGenerator(SelectGrammar.from_text(f"query:\n    {sql}"))
    with pytest.raises(CandidateRejected, match="PQ"):
        generator.generate(_schema(), seed=1)


def test_unsupported_automatic_productions_and_hooks_are_removed() -> None:
    grammar = SelectGrammar.default()
    removed = {
        "fulltext_query",
        "recursive_cte_query",
        "lateral_derived_select",
        "boundary_query",
        "values_query_core",
        "table_query_core",
        "scalar_subquery",
        "membership_subquery",
        "anti_membership_predicate",
        "window_expression",
        "rollup_suffix",
        "json_scalar_function",
        "spatial_scalar_function",
        "bit_operator",
    }
    assert removed.isdisjoint(grammar.productions)
    terminals = {
        symbol.value
        for production in grammar.productions.values()
        for alternative in production.alternatives
        for symbol in alternative.symbols
    }
    assert terminals.isdisjoint(
        {
            "_set_scalar_operand",
            "_set_values_operand",
            "_set_table_operand",
            "_lateral_derived_relation",
            "_json_table_relation",
            "_bare_star",
            "_table_alias_star",
            "_deterministic_group_concat",
            "_json_object_aggregate",
        }
    )


def test_registry_contains_only_documented_pq_scalar_functions() -> None:
    allowed = set(
        "ABS ACOS ASIN ATAN CEIL CEILING COS COT DEGREES EXP FLOOR LN LOG LOG10 MOD PI RADIANS ROUND SIN SQRT TAN TRUNCATE STRCMP DATE DAY DAYNAME DAYOFYEAR HOUR MICROSECOND MINUTE MONTH MONTHNAME QUARTER SECOND TO_DAYS WEEK WEEKDAY YEAR COALESCE GREATEST IF ISNULL LEAST NULLIF ADDTIME".split()
    )
    names = {item.sql_name for item in DETERMINISTIC_FUNCTION_SIGNATURES}
    assert names <= allowed
    assert {"ABS", "ROUND", "YEAR", "COALESCE", "STRCMP"} <= names


def test_column_binding_omits_pq_unsupported_types_and_generated_columns() -> None:
    columns = (GrammarColumn("id", "INT"),) + tuple(
        GrammarColumn(f"bad_{index}", mysql_type)
        for index, mysql_type in enumerate(
            (
                "TEXT",
                "BLOB",
                "JSON",
                "GEOMETRY",
                "INT ZEROFILL",
                "INT GENERATED ALWAYS AS (1) STORED",
            )
        )
    )
    schema = GrammarSchema((GrammarTable("t", columns),))
    grammar = SelectGrammar.from_text("""
query:
    _scope_begin _prepare_base_relation SELECT _any_column AS _projection_alias FROM _emit_relation _scope_end
""")
    generator = GrammarQueryGenerator(grammar)
    for seed in range(100):
        sql = generator.generate(schema, seed=seed).sql
        assert "bad_" not in sql
        assert ".`id`" in sql


@pytest.mark.parametrize(
    ("mysql_type", "family", "hook"),
    [
        ("BINARY(8)", TypeFamily.BINARY, "_any_column"),
        ("VARBINARY(32)", TypeFamily.BINARY, "_any_column"),
        ("BOOL", TypeFamily.NUMERIC, "_strict_numeric_column"),
        ("BOOLEAN", TypeFamily.NUMERIC, "_strict_numeric_column"),
        ("INTEGER", TypeFamily.NUMERIC, "_strict_numeric_column"),
        ("NUMERIC(10,2)", TypeFamily.NUMERIC, "_strict_numeric_column"),
        ("REAL", TypeFamily.NUMERIC, "_strict_numeric_column"),
        ("NCHAR(12)", TypeFamily.TEXT, "_strict_text_column"),
        ("NVARCHAR(24)", TypeFamily.TEXT, "_strict_text_column"),
    ],
)
@pytest.mark.parametrize("derived", [False, True])
def test_documented_physical_type_aliases_remain_reachable(
    mysql_type: str, family: TypeFamily, hook: str, derived: bool
) -> None:
    column = GrammarColumn("alias_col", mysql_type)
    schema = GrammarSchema((GrammarTable("t", (column,)),))
    prepare = "_prepare_relation" if derived else "_prepare_base_relation"
    outer_hook = "_any_column" if derived else hook
    grammar = SelectGrammar.from_text(f"""
query:
    _scope_begin {prepare} SELECT {outer_hook} AS _projection_alias FROM _emit_relation _scope_end
relation:
    _derived_relation
derived_select:
    _scope_begin_isolated _prepare_base_relation SELECT {hook} AS _projection_alias FROM _emit_relation _scope_end
""")
    candidate = GrammarQueryGenerator(grammar).generate(schema, seed=1)
    assert column.family is family
    assert ".`alias_col`" in candidate.sql
    if derived:
        assert ".`q1`" in candidate.sql


def test_every_partitioned_base_relation_scans_exactly_one_partition() -> None:
    schema = GrammarSchema(
        (GrammarTable("t", (GrammarColumn("id", "INT"),), partitions=("p0", "p1", "p2")),)
    )
    grammar = SelectGrammar.from_text("""
query:
    _scope_begin _prepare_base_relation SELECT _any_column AS _projection_alias FROM _emit_relation _scope_end
""")
    generator = GrammarQueryGenerator(grammar)
    for seed in range(50):
        sql = generator.generate(schema, seed=seed).sql
        assert re.search(r"FROM `t` PARTITION \(`p[012]`\) AS", sql)


def test_pq_generation_has_no_candidates_when_all_columns_are_unsupported() -> None:
    schema = GrammarSchema((GrammarTable("t", (GrammarColumn("bad", "BLOB"),)),))
    with pytest.raises(CandidateRejected, match="PQ"):
        GrammarQueryGenerator().generate(schema, seed=1)


def test_vector_tables_are_ineligible_even_when_projecting_an_integer() -> None:
    schema = GrammarSchema(
        (GrammarTable("t", (GrammarColumn("id", "INT"), GrammarColumn("v", "VECTOR"))),)
    )
    with pytest.raises(CandidateRejected, match="PQ"):
        GrammarQueryGenerator().generate(schema, seed=1)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM missing",
        "SELECT id FROM t, missing",
        "SELECT id FROM t",
        "SELECT id FROM (SELECT id FROM t) AS d",
    ],
)
def test_custom_grammar_requires_known_tables_and_explicit_single_partition(sql: str) -> None:
    schema = GrammarSchema(
        (GrammarTable("t", (GrammarColumn("id", "INT"),), partitions=("p0", "p1")),)
    )
    with pytest.raises(CandidateRejected, match="PQ"):
        GrammarQueryGenerator(SelectGrammar.from_text(f"query:\n    {sql}")).generate(
            schema, seed=1
        )


@pytest.mark.parametrize("sql", ["SELECT bad FROM t", "SELECT * FROM t", "SELECT t.* FROM t"])
def test_custom_grammar_cannot_reference_filtered_schema_columns(sql: str) -> None:
    schema = GrammarSchema(
        (
            GrammarTable(
                "t", (GrammarColumn("id", "INT"), GrammarColumn("bad", "INT", generated=True))
            ),
        )
    )
    with pytest.raises(CandidateRejected, match="PQ"):
        GrammarQueryGenerator(SelectGrammar.from_text(f"query:\n    {sql}")).generate(
            schema, seed=1
        )


def test_pq_query_gate_rejects_a_non_query_command_containing_select() -> None:
    from select_fuzz.generation.pq_eligibility import PqEligibilityValidator, PqIneligible

    with pytest.raises(PqIneligible):
        PqEligibilityValidator().validate_text("INSERT INTO result SELECT id FROM t")


def test_index_hints_require_fully_supported_minimal_table_metadata() -> None:
    schema = GrammarSchema(
        (
            GrammarTable(
                "t",
                (GrammarColumn("id", "INT"), GrammarColumn("bad", "TEXT")),
                indexes=("idx_unknown_key",),
            ),
        )
    )
    grammar = SelectGrammar.from_text("""
query:
    _scope_begin _prepare_base_relation SELECT _optimizer_hint_index_secondary _any_column AS _projection_alias FROM _emit_relation _scope_end
""")
    with pytest.raises(CandidateRejected):
        GrammarQueryGenerator(grammar).generate(schema, seed=1)


@pytest.mark.parametrize("scope", ["FOR JOIN", "FOR ORDER BY", "FOR GROUP BY", ""])
def test_index_hint_scope_does_not_start_a_join_relation(scope: str) -> None:
    schema = GrammarSchema(
        (GrammarTable("t", (GrammarColumn("id", "INT"),), indexes=("idx",), partitions=("p0",)),)
    )
    sql = f"SELECT id FROM t PARTITION (p0) AS r FORCE INDEX {scope} (idx)"
    candidate = GrammarQueryGenerator(SelectGrammar.from_text(f"query:\n    {sql}")).generate(
        schema, seed=1
    )
    assert "FORCE INDEX" in candidate.sql
