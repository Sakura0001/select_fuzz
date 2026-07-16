from __future__ import annotations

import os
import time

import mysql.connector
import pytest

from select_fuzz.generation.catalog import CapabilityStatus, FeatureSpec
from select_fuzz.generation.data import DataScenario
from select_fuzz.generation.query import QueryGenerator, QueryLane
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexPart,
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


def _target(feature_id: str) -> FeatureSpec:
    return FeatureSpec(
        feature_id,
        "query",
        (8, 0, 41),
        frozenset({SchemaProfile.REGULAR_INNODB.value}),
        frozenset({"query_expression"}),
        frozenset({"read_only_select"}),
        capability_status=CapabilityStatus.GENERATOR_SUPPORTED,
        evidence_lock_ready=True,
    )


def _manifest() -> SchemaManifest:
    tables = tuple(
        TableDef(
            f"t{ordinal}",
            False,
            (
                ColumnDef("id", "BIGINT UNSIGNED", False),
                ColumnDef(
                    "payload",
                    "VARCHAR(64)",
                    True,
                    "utf8mb4",
                    "utf8mb4_0900_ai_ci",
                ),
                ColumnDef("amount", "INT", True),
                ColumnDef("created_at", "DATETIME(6)", True),
            ),
            (
                IndexDef(
                    "PRIMARY",
                    (IndexPart(column_name="id"),),
                    unique=True,
                    primary=True,
                ),
                IndexDef("ix_id", (IndexPart(column_name="id"),)),
            ),
        )
        for ordinal in range(3)
    )
    return SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "select_query_specification",
        8_041,
        tables,
    )


_INDEX_HINT_VARIANTS = tuple(
    f"index_hint_{action}_{scope}"
    for action in ("use", "force", "ignore")
    for scope in ("default", "join", "order_by", "group_by")
)

_DIRECTED_CASES = (
    *(
        ("select_query_specification", variant)
        for variant in (
            "modifier_high_priority",
            "modifier_sql_calc_found_rows",
            "modifier_sql_no_cache",
        )
    ),
    *(
        ("join_inner_cross_straight", variant)
        for variant in (
            "comma",
            "inner_conditionless",
            "inner_using",
            "natural_inner",
            "nested_three",
            *_INDEX_HINT_VARIANTS,
        )
    ),
    ("join_outer_natural", "left_using"),
    ("join_outer_natural", "right_using"),
    *(
        ("function_deterministic_scalar", variant)
        for variant in (
            "null_safe_eq",
            "divide",
            "integer_divide",
            "modulo",
            "bit_and",
            "bit_or",
            "bit_xor",
            "shift_left",
            "shift_right",
            "logical_xor",
            "unary_plus",
            "unary_minus",
            "between",
            "not_between",
            "in_list_null",
            "not_in_list_null",
            "like_escape",
            "not_like_escape",
            "regexp_like",
            "not_regexp_like",
            "is_true",
            "is_false",
            "is_unknown",
            "is_not_true",
            "is_not_false",
            "is_not_unknown",
        )
    ),
    *(
        ("subquery_result_kinds", variant)
        for variant in ("not_exists", "not_in", "not_in_null", "not_exists_empty")
    ),
    *(("cte_nonrecursive", variant) for variant in ("multiple", "dependency", "reuse")),
    ("set_intersect", "intersect_all"),
    ("set_except", "except_all"),
    ("grouping_with_rollup", "table_grouping_function"),
    *(
        ("function_aggregate", variant)
        for variant in (
            "sum",
            "avg",
            "min",
            "max",
            "count_distinct",
            "group_null_having",
            "bit_and",
            "bit_or",
            "bit_xor",
            "stddev_pop",
            "stddev_samp",
            "var_pop",
            "var_samp",
        )
    ),
    *(
        ("window_inline_named", variant)
        for variant in (
            "rank",
            "dense_rank",
            "lag",
            "lead",
            "cume_dist",
            "percent_rank",
            "ntile",
            "first_value",
            "last_value",
            "nth_value",
            "lag_offset",
            "lag_default",
            "lead_offset",
            "lead_default",
        )
    ),
    ("window_frames", "rows_frame"),
    ("window_frames", "range_frame"),
    ("window_frames", "rows_unbounded_current"),
    ("window_frames", "range_current_unbounded"),
    ("cte_recursive", "recursive_union_all"),
    ("cte_recursive", "recursive_union_distinct"),
)


@pytest.mark.mysql
@pytest.mark.timeout(600)
def test_every_new_semantic_variant_on_three_exact_8041_sockets() -> None:
    manifest = _manifest()
    setup = SetupBundleBuilder().build(
        manifest,
        seed=8_041,
        rows_per_table=8,
        scenario=DataScenario.MIXED_NULL,
    )
    generator = QueryGenerator()
    cases = (
        ("type_temporal_json_spatial", None),
        *_DIRECTED_CASES,
    )
    assert len(_DIRECTED_CASES) == 91
    database = f"sf_semantic_variants_{time.time_ns():x}"[-64:]
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

        for ordinal, (feature_id, variant) in enumerate(cases):
            generated = generator.generate(
                manifest,
                target=_target(feature_id),
                seed=8_041_000 + ordinal,
                case_ordinal=ordinal,
                lane=QueryLane.VALID,
                directed_variant=variant,
                estimated_rows_by_table={table.name: 8 for table in manifest.tables},
            )
            outcomes: list[tuple[tuple[object, ...], ...]] = []
            for connection in connections:
                cursor = connection.cursor()
                try:
                    cursor.execute(generated.sql)
                    outcomes.append(tuple(tuple(row) for row in cursor.fetchall()))
                except mysql.connector.Error as error:
                    pytest.fail(
                        f"{feature_id}:{variant} failed errno={error.errno} "
                        f"sqlstate={error.sqlstate}: {generated.sql}"
                    )
                finally:
                    cursor.close()
            assert outcomes[0] == outcomes[1] == outcomes[2], (feature_id, variant)
    finally:
        for connection in connections:
            cursor = connection.cursor()
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
            cursor.close()
            connection.close()
