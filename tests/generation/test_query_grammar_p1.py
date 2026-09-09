from __future__ import annotations

import re

import pytest

from select_fuzz.generation.query_grammar import (
    CandidateRejected,
    GrammarColumn,
    GrammarQueryGenerator,
    GrammarSchema,
    GrammarTable,
    SelectGrammar,
)
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexKind,
    IndexPart,
    PartitionDef,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)


def _rich_schema() -> GrammarSchema:
    columns = (
        GrammarColumn("id", "BIGINT"),
        GrammarColumn("tenant_id", "BIGINT"),
        GrammarColumn("txt", "VARCHAR(64)"),
        GrammarColumn("created_at", "DATETIME"),
    )
    return GrammarSchema(
        (
            GrammarTable("t0", columns, ("PRIMARY", "idx_tenant"), ("p0", "p1")),
            GrammarTable("t1", columns, ("PRIMARY", "idx_tenant"), ("p0", "p1")),
        )
    )


def test_manifest_metadata_keeps_only_usable_indexes_and_partition_names() -> None:
    table = TableDef(
        "t0",
        False,
        (
            ColumnDef("id", "BIGINT", False),
            ColumnDef(
                "txt",
                "VARCHAR(64)",
                True,
                "utf8mb4",
                "utf8mb4_0900_ai_ci",
            ),
        ),
        (
            IndexDef(
                "PRIMARY",
                (IndexPart(column_name="id"),),
                unique=True,
                primary=True,
            ),
            IndexDef("idx_visible", (IndexPart(column_name="id"),)),
            IndexDef("idx_hidden", (IndexPart(column_name="id"),), visible=False),
            IndexDef(
                "idx_fulltext",
                (IndexPart(column_name="txt"),),
                kind=IndexKind.FULLTEXT,
            ),
        ),
        PartitionDef("HASH", ("id",), 3),
    )
    manifest = SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "metadata",
        8041,
        (table,),
    )

    grammar_table = GrammarSchema.from_manifest(manifest).tables[0]

    assert grammar_table.indexes == ("PRIMARY", "idx_visible")
    assert grammar_table.partitions == ("p0", "p1", "p2")


def test_partition_and_index_hint_render_in_mysql_table_factor_order() -> None:
    grammar = SelectGrammar.from_text(
        """
query:
    _scope_begin _prepare_relation SELECT _any_column AS _projection_alias FROM _emit_relation _scope_end
relation:
    _table_partition_index_hint
"""
    )

    sql = GrammarQueryGenerator(grammar).generate(_rich_schema(), seed=17).sql

    assert re.search(
        r"FROM `t[01]` PARTITION \(`p[01]`(?:, `p[01]`)?\) AS `r1` "
        r"(?:USE|FORCE|IGNORE) INDEX(?: FOR (?:JOIN|ORDER BY|GROUP BY))? "
        r"\(`(?:PRIMARY|idx_tenant)`(?:, `(?:PRIMARY|idx_tenant)`)?\)",
        sql,
    )


def test_multi_column_group_by_and_using_bind_real_common_columns() -> None:
    grouped = SelectGrammar.from_text(
        """
query:
    _scope_begin _prepare_relation _prepare_group_columns SELECT _group_column AS _projection_alias , COUNT ( * ) _result_numeric AS _projection_alias FROM _emit_relation GROUP BY _group_columns _scope_end
relation:
    _table
"""
    )
    using = SelectGrammar.from_text(
        """
query:
    _scope_begin _prepare_relation SELECT _any_column AS _projection_alias FROM _emit_relation _scope_end
relation:
    _table JOIN _table USING ( _common_columns )
"""
    )

    grouped_sql = GrammarQueryGenerator(grouped).generate(_rich_schema(), seed=23).sql
    using_sql = GrammarQueryGenerator(using).generate(_rich_schema(), seed=29).sql

    assert re.search(r"GROUP BY `r1`\.`\w+`, `r1`\.`\w+`", grouped_sql)
    using_columns = re.search(r"USING \(([^)]+)\)", using_sql)
    assert using_columns is not None
    assert "," in using_columns.group(1)


def test_full_query_expression_derived_table_has_explicit_stable_columns() -> None:
    grammar = SelectGrammar.from_text(
        """
query:
    _scope_begin _prepare_relation SELECT _any_column AS _projection_alias FROM _emit_relation _scope_end
relation:
    _derived_query_expression_relation
derived_query_expression:
    _prepare_numeric_2_set_signature _set_select_operand UNION ALL _set_select_operand _clear_set_signature
"""
    )

    sql = GrammarQueryGenerator(grammar).generate(_rich_schema(), seed=31).sql

    assert "UNION ALL SELECT" in sql
    assert re.search(r"AS `r\d+` \(`d1`, `d2`\)", sql)
    assert re.search(r"`r\d+`\.`d[12]`", sql)


_SET_OPERATORS = (
    "UNION",
    "UNION ALL",
    "UNION DISTINCT",
    "INTERSECT",
    "INTERSECT ALL",
    "EXCEPT",
    "EXCEPT ALL",
)


@pytest.mark.parametrize("left", _SET_OPERATORS)
@pytest.mark.parametrize("right", _SET_OPERATORS)
def test_every_ordered_set_operator_pair_is_renderable(left: str, right: str) -> None:
    grammar = SelectGrammar.from_text(
        f"""
query:
    _prepare_numeric_1_set_signature _set_select_operand {left} _set_select_operand {right} _set_select_operand _clear_set_signature
"""
    )

    if any(operator.startswith(("INTERSECT", "EXCEPT")) for operator in (left, right)):
        with pytest.raises(CandidateRejected, match="PQ"):
            GrammarQueryGenerator(grammar).generate(_rich_schema(), seed=37)
        return
    sql = GrammarQueryGenerator(grammar).generate(_rich_schema(), seed=37).sql

    assert left in sql
    assert right in sql


def test_supported_cast_interval_and_aggregate_forms_are_renderable() -> None:
    grammar = SelectGrammar.from_text("""
query:
    _scope_begin _prepare_relation SELECT CAST ( '12:34:56.123456' AS TIME ( 6 ) ) _result_temporal AS _projection_alias , DATE_ADD ( MAX ( _strict_temporal_column ) , INTERVAL '1 02:03:04.000005' DAY_MICROSECOND ) _result_temporal AS _projection_alias , COUNT ( * ) _result_numeric AS _projection_alias FROM _emit_relation _scope_end
relation:
    _table
""")
    sql = GrammarQueryGenerator(grammar).generate(_rich_schema(), seed=41).sql
    assert "TIME(6)" in sql
    assert "DAY_MICROSECOND" in sql
    assert "COUNT(*)" in sql
    assert "MAX(" in sql
