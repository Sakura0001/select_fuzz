from __future__ import annotations

import pytest

from select_fuzz.generation.catalog import CapabilityStatus, FeatureSpec
from select_fuzz.generation.query import QueryGenerator, QueryLane
from select_fuzz.generation.query_safety import ReadOnlyValidator
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexPart,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)


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
    return SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "select_query_specification",
        8_041,
        tuple(
            TableDef(
                f"t{ordinal}",
                False,
                (
                    ColumnDef("id", "BIGINT UNSIGNED", False),
                    ColumnDef("amount", "INT", True),
                    ColumnDef(
                        "payload",
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
                ),
            )
            for ordinal in range(3)
        ),
    )


_CASES = (
    (
        "select_query_specification",
        "modifier_high_priority",
        "SELECT HIGH_PRIORITY ",
        "select_modifier_high_priority",
    ),
    (
        "select_query_specification",
        "modifier_sql_calc_found_rows",
        "SELECT SQL_CALC_FOUND_ROWS ",
        "select_modifier_sql_calc_found_rows",
    ),
    (
        "select_query_specification",
        "modifier_sql_no_cache",
        "SELECT SQL_NO_CACHE ",
        "select_modifier_sql_no_cache",
    ),
    (
        "function_deterministic_scalar",
        "not_like_escape",
        " NOT LIKE ",
        "predicate_not_like_escape",
    ),
    (
        "function_deterministic_scalar",
        "not_regexp_like",
        "(NOT REGEXP_LIKE(",
        "predicate_not_regexp_like",
    ),
    (
        "function_deterministic_scalar",
        "is_not_true",
        " IS NOT TRUE",
        "predicate_is_not_true",
    ),
    (
        "function_deterministic_scalar",
        "is_not_false",
        " IS NOT FALSE",
        "predicate_is_not_false",
    ),
    (
        "function_deterministic_scalar",
        "is_not_unknown",
        " IS NOT UNKNOWN",
        "predicate_is_not_unknown",
    ),
    (
        "join_inner_cross_straight",
        "inner_conditionless",
        "INNER JOIN `t1` AS `u`",
        "join_inner_conditionless",
    ),
    ("function_aggregate", "bit_and", "BIT_AND(", "aggregate_bit_and"),
    ("function_aggregate", "bit_or", "BIT_OR(", "aggregate_bit_or"),
    ("function_aggregate", "bit_xor", "BIT_XOR(", "aggregate_bit_xor"),
    ("function_aggregate", "stddev_pop", "STDDEV_POP(", "aggregate_stddev_pop"),
    ("function_aggregate", "stddev_samp", "STDDEV_SAMP(", "aggregate_stddev_samp"),
    ("function_aggregate", "var_pop", "VAR_POP(", "aggregate_var_pop"),
    ("function_aggregate", "var_samp", "VAR_SAMP(", "aggregate_var_samp"),
    (
        "grouping_with_rollup",
        "table_grouping_function",
        "GROUPING(`t`.`id`)",
        "grouping_function",
    ),
    ("window_inline_named", "cume_dist", "CUME_DIST() OVER", "window_cume_dist"),
    (
        "window_inline_named",
        "percent_rank",
        "PERCENT_RANK() OVER",
        "window_percent_rank",
    ),
    ("window_inline_named", "ntile", "NTILE(2) OVER", "window_ntile"),
    (
        "window_inline_named",
        "first_value",
        "FIRST_VALUE(`t`.`id`) OVER",
        "window_first_value",
    ),
    (
        "window_inline_named",
        "last_value",
        "LAST_VALUE(`t`.`id`) OVER",
        "window_last_value",
    ),
    (
        "window_inline_named",
        "nth_value",
        "NTH_VALUE(`t`.`id`, 2) OVER",
        "window_nth_value",
    ),
    (
        "window_inline_named",
        "lag_offset",
        "LAG(`t`.`id`, 2) OVER",
        "window_lag_offset",
    ),
    (
        "window_inline_named",
        "lag_default",
        "LAG(`t`.`id`, 2, 0) OVER",
        "window_lag_default",
    ),
    (
        "window_inline_named",
        "lead_offset",
        "LEAD(`t`.`id`, 2) OVER",
        "window_lead_offset",
    ),
    (
        "window_inline_named",
        "lead_default",
        "LEAD(`t`.`id`, 2, 0) OVER",
        "window_lead_default",
    ),
    (
        "window_frames",
        "rows_unbounded_current",
        "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
        "window_rows_unbounded_current",
    ),
    (
        "window_frames",
        "range_current_unbounded",
        "RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING",
        "window_range_current_unbounded",
    ),
    (
        "cte_recursive",
        "recursive_union_all",
        " UNION ALL ",
        "cte_recursive_union_all",
    ),
    (
        "cte_recursive",
        "recursive_union_distinct",
        " UNION SELECT ",
        "cte_recursive_union_distinct",
    ),
)


@pytest.mark.parametrize(
    ("feature_id", "variant", "snippet", "tag"),
    _CASES,
)
def test_verified_8041_grammar_expansion_is_directed_and_read_only(
    feature_id: str,
    variant: str,
    snippet: str,
    tag: str,
) -> None:
    generator = QueryGenerator()
    kwargs = {
        "target": _target(feature_id),
        "seed": 8_041,
        "lane": QueryLane.VALID,
        "directed_variant": variant,
        "estimated_rows_by_table": {"t0": 3, "t1": 3, "t2": 3},
    }

    first = generator.generate(_manifest(), **kwargs)
    second = generator.generate(_manifest(), **kwargs)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert snippet in first.sql
    assert tag in first.feature_tags
    assert f"query_leaf:{feature_id}:{variant}" in first.feature_tags
    if variant == "inner_conditionless":
        assert " ON " not in first.sql
        assert first.complexity.estimated_intermediate_rows == 9
    ReadOnlyValidator().validate_text(first.sql)


@pytest.mark.parametrize(
    "variant",
    ("stddev_pop", "stddev_samp", "var_pop", "var_samp"),
)
def test_statistical_aggregates_avoid_extreme_nullable_numeric_columns(
    variant: str,
) -> None:
    manifest = _manifest()
    table = manifest.tables[0]
    manifest = SchemaManifest(
        manifest.profile,
        manifest.target_feature_id,
        manifest.seed,
        (
            TableDef(
                table.name,
                table.temporary,
                (
                    table.columns[0],
                    ColumnDef("extreme_double", "DOUBLE", True),
                    *table.columns[1:],
                ),
                table.indexes,
            ),
            *manifest.tables[1:],
        ),
    )

    generated = QueryGenerator().generate(
        manifest,
        target=_target("function_aggregate"),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant=variant,
    )

    assert f"{variant.upper()}(`t`.`id`)" in generated.sql
    assert "extreme_double" not in generated.sql
