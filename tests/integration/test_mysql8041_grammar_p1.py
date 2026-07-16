from __future__ import annotations

import os
import re
import time

import mysql.connector
import pytest

from select_fuzz.generation.data import DataScenario
from select_fuzz.generation.query_grammar import GrammarQueryGenerator, SelectGrammar
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexPart,
    PartitionDef,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)
from select_fuzz.generation.setup import SetupBundleBuilder


def _sockets() -> tuple[str, ...]:
    if os.environ.get("SELECT_FUZZ_MYSQL_SOCKET_INTEGRATION") != "1":
        pytest.skip("set SELECT_FUZZ_MYSQL_SOCKET_INTEGRATION=1 and socket list")
    sockets_value = os.environ.get("SELECT_FUZZ_MYSQL_SOCKETS")
    if sockets_value is None:
        pytest.skip("SELECT_FUZZ_MYSQL_SOCKETS is unset")
    sockets = tuple(item for item in sockets_value.split(",") if item)
    if len(sockets) != 3:
        pytest.skip("SELECT_FUZZ_MYSQL_SOCKETS must contain exactly three paths")
    return sockets


def _manifest() -> SchemaManifest:
    return SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "grammar_p1",
        8_041,
        tuple(
            TableDef(
                f"t{ordinal}",
                False,
                (
                    ColumnDef("id", "BIGINT", False),
                    ColumnDef("tenant_id", "BIGINT", True),
                    ColumnDef(
                        "txt",
                        "VARCHAR(64)",
                        True,
                        "utf8mb4",
                        "utf8mb4_0900_ai_ci",
                    ),
                    ColumnDef("created_at", "DATETIME(6)", True),
                ),
                (
                    IndexDef(
                        "PRIMARY",
                        (IndexPart(column_name="id"),),
                        unique=True,
                        primary=True,
                    ),
                    IndexDef("idx_tenant", (IndexPart(column_name="tenant_id"),)),
                ),
                PartitionDef("HASH", ("id",), 2),
            )
            for ordinal in range(2)
        ),
    )


def _generate(manifest: SchemaManifest, grammar_text: str, seed: int) -> str:
    return (
        GrammarQueryGenerator(SelectGrammar.from_text(grammar_text))
        .generate(
            manifest,
            seed=seed,
        )
        .sql
    )


def _queries(manifest: SchemaManifest) -> tuple[tuple[str, str], ...]:
    cases = [
        (
            "partition_index_hint",
            _generate(
                manifest,
                """
query:
    _scope_begin _prepare_relation SELECT _any_column AS _projection_alias FROM _emit_relation _scope_end
relation:
    _table_partition_index_hint
""",
                11,
            ),
        ),
        (
            "multi_group_by",
            _generate(
                manifest,
                """
query:
    _scope_begin _prepare_relation _prepare_group_columns SELECT _group_column AS _projection_alias , COUNT ( * ) _result_numeric AS _projection_alias FROM _emit_relation GROUP BY _group_columns _scope_end
relation:
    _table
""",
                13,
            ),
        ),
        (
            "multi_using",
            _generate(
                manifest,
                """
query:
    _scope_begin _prepare_relation SELECT _any_column AS _projection_alias FROM _emit_relation _scope_end
relation:
    _table JOIN _table USING ( _common_columns )
""",
                17,
            ),
        ),
        (
            "derived_set_expression",
            _generate(
                manifest,
                """
query:
    _scope_begin _prepare_relation SELECT _any_column AS _projection_alias FROM _emit_relation _scope_end
relation:
    _derived_query_expression_relation
derived_query_expression:
    _prepare_numeric_2_set_signature _set_select_operand UNION ALL _set_values_operand INTERSECT _set_scalar_operand _clear_set_signature
""",
                19,
            ),
        ),
        (
            "explicit_cte_set_expression",
            _generate(
                manifest,
                """
query:
    _prepare_query_expression_cte WITH _emit_cte_name _emit_cte_column_list AS ( _emit_cte_body ) _emit_cte_outer _clear_cte
derived_query_expression:
    _prepare_numeric_2_set_signature _set_select_operand UNION ALL _set_values_operand _clear_set_signature
cte_outer_select:
    _scope_begin_isolated _prepare_cte_relation SELECT _any_column AS _projection_alias FROM _emit_relation _scope_end
""",
                23,
            ),
        ),
        (
            "recursive_pair_cte",
            _generate(
                manifest,
                """
query:
    _prepare_recursive_pair_cte WITH RECURSIVE _emit_cte_name ( `n` , `total` ) AS ( SELECT 1 , 1 UNION ALL SELECT `n` + 1 , `total` + `n` FROM _emit_cte_name WHERE `n` < 5 ) SELECT `n` AS `q1` , `total` AS `q2` FROM _emit_cte_name ORDER BY `q1` _clear_cte
""",
                29,
            ),
        ),
        (
            "modifier_stack_and_hint",
            _generate(
                manifest,
                """
query:
    _scope_begin _prepare_base_relation SELECT _optimizer_hint DISTINCT HIGH_PRIORITY STRAIGHT_JOIN SQL_SMALL_RESULT SQL_BUFFER_RESULT SQL_NO_CACHE SQL_CALC_FOUND_ROWS _any_column AS _projection_alias FROM _emit_relation _scope_end
""",
                31,
            ),
        ),
        (
            "cast_convert_interval",
            _generate(
                manifest,
                """
query:
    _scope_begin SELECT CAST ( 1 AS FLOAT ) _result_numeric AS _projection_alias , CAST ( 1 AS DOUBLE ) _result_numeric AS _projection_alias , CAST ( 'Alpha beta' AS CHAR ( 64 ) CHARACTER SET utf8mb4 ) _result_text AS _projection_alias , CAST ( '12:34:56.123456' AS TIME ( 6 ) ) _result_temporal AS _projection_alias , CAST ( '2024-02-29 12:34:56.123456' AS DATETIME ( 6 ) ) _result_temporal AS _projection_alias , CAST ( 2024 AS YEAR ) _result_temporal AS _projection_alias , CONVERT ( 'Alpha beta' USING utf8mb4 ) _result_text AS _projection_alias , DATE_ADD ( CAST ( '2024-02-29' AS DATE ) , INTERVAL '1 02:03:04.000005' DAY_MICROSECOND ) _result_temporal AS _projection_alias , DATE_ADD ( CAST ( '2024-02-29' AS DATE ) , INTERVAL '1-2' YEAR_MONTH ) _result_temporal AS _projection_alias _scope_end
""",
                37,
            ),
        ),
        (
            "window_empty_multi_named_and_frames",
            _generate(
                manifest,
                """
query:
    _scope_begin _prepare_relation _scope_enable_named_window SELECT RANK ( ) OVER ( ) _result_numeric AS _projection_alias , LAG ( _window_value_column ) OVER _window_name2 _result_window_value AS _projection_alias , SUM ( _strict_numeric_column ) OVER ( ORDER BY _window_numeric_order RANGE BETWEEN 1 PRECEDING AND CURRENT ROW ) _result_numeric AS _projection_alias , COUNT ( * ) OVER ( ORDER BY _window_total_order ROWS BETWEEN 2 PRECEDING AND 1 PRECEDING ) _result_numeric AS _projection_alias FROM _emit_relation WINDOW _window_name AS ( PARTITION BY _window_partition_list ) , _window_name2 AS ( _window_name ORDER BY _window_total_order ) _scope_end
relation:
    _table
""",
                41,
            ),
        ),
        (
            "deterministic_aggregates",
            _generate(
                manifest,
                """
query:
    _scope_begin _prepare_relation SELECT _deterministic_group_concat AS _projection_alias , JSON_ARRAYAGG ( 1 ) _result_json AS _projection_alias , _json_object_aggregate AS _projection_alias FROM _emit_relation _scope_end
relation:
    _table
""",
                43,
            ),
        ),
    ]

    lateral_grammar = """
query:
    _scope_begin _prepare_relation SELECT _any_column AS _projection_alias FROM _emit_relation _scope_end
relation:
    _right_lateral_join_relation
    | _table
lateral_derived_select:
    _scope_begin _prepare_relation SELECT _any_column AS _projection_alias FROM _emit_relation _scope_end
predicate:
    _any_column IS NOT NULL
"""
    for seed in range(100):
        sql = _generate(manifest, lateral_grammar, 100 + seed)
        match = re.search(r"LATERAL \((.+)\) AS `r\d+` RIGHT", sql)
        if match is not None and "`r1`." in match.group(1):
            cases.append(("correlated_right_lateral", sql))
            break
    else:  # pragma: no cover - deterministic seed contract
        raise AssertionError("unable to render a correlated RIGHT LATERAL witness")

    operators = (
        "UNION",
        "UNION ALL",
        "UNION DISTINCT",
        "INTERSECT",
        "INTERSECT ALL",
        "EXCEPT",
        "EXCEPT ALL",
    )
    for left in operators:
        for right in operators:
            cases.append(
                (
                    f"set_pair:{left}>{right}",
                    _generate(
                        manifest,
                        f"""
query:
    _prepare_numeric_1_set_signature _set_select_operand {left} _set_values_operand {right} _set_scalar_operand _clear_set_signature
""",
                        47,
                    ),
                )
            )
    return tuple(cases)


def _normalized_rows(rows: list[tuple[object, ...]]) -> tuple[str, ...]:
    return tuple(sorted(repr(tuple(row)) for row in rows))


@pytest.mark.mysql
@pytest.mark.timeout(600)
def test_grammar_p1_witnesses_on_three_exact_8041_sockets() -> None:
    manifest = _manifest()
    setup = SetupBundleBuilder().build(
        manifest,
        seed=8_041,
        rows_per_table=8,
        scenario=DataScenario.MIXED_NULL,
    )
    cases = _queries(manifest)
    assert len(cases) == 60
    database = f"sf_grammar_p1_{time.time_ns():x}"[-64:]
    connections = [
        mysql.connector.connect(unix_socket=socket, user="root", autocommit=True)
        for socket in _sockets()
    ]
    try:
        for connection in connections:
            cursor = connection.cursor()
            cursor.execute("SELECT VERSION()")
            assert cursor.fetchone()[0].startswith("8.0.41")
            cursor.execute(f"CREATE DATABASE `{database}`")
            cursor.execute(f"USE `{database}`")
            for statement in setup.statements:
                cursor.execute(statement)
            cursor.close()

        for case_name, sql in cases:
            outcomes: list[tuple[str, ...]] = []
            warning_codes: list[tuple[int, ...]] = []
            for connection in connections:
                cursor = connection.cursor()
                try:
                    cursor.execute(f"EXPLAIN {sql}")
                    assert cursor.fetchall(), case_name
                    cursor.execute("SHOW WARNINGS")
                    explain_warnings = tuple(cursor.fetchall())
                    assert not any(
                        "hint" in str(warning[2]).casefold() for warning in explain_warnings
                    ), (case_name, explain_warnings, sql)
                    cursor.execute(sql)
                    outcomes.append(_normalized_rows(cursor.fetchall()))
                    cursor.execute("SHOW WARNINGS")
                    warnings = tuple(cursor.fetchall())
                    warning_codes.append(tuple(int(warning[1]) for warning in warnings))
                except mysql.connector.Error as error:
                    pytest.fail(
                        f"{case_name} failed errno={error.errno} sqlstate={error.sqlstate}: {sql}"
                    )
                finally:
                    cursor.close()
            assert outcomes[0] == outcomes[1] == outcomes[2], case_name
            assert warning_codes[0] == warning_codes[1] == warning_codes[2], case_name
    finally:
        for connection in connections:
            cursor = connection.cursor()
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
            cursor.close()
            connection.close()
