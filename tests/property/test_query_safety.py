from __future__ import annotations

from hypothesis import given, settings, strategies as st

from select_fuzz.generation.catalog import FeatureSpec
from select_fuzz.generation.query import QueryBudget, QueryGenerator, QueryLane
from select_fuzz.generation.query_safety import ReadOnlyValidator
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexExpression,
    IndexKind,
    IndexPart,
    SchemaManifest,
    SchemaProfile,
    SortDirection,
    TableDef,
)


PROPERTY_FEATURES = (
    "select_query_specification",
    "select_parenthesized",
    "join_inner_cross_straight",
    "join_outer_natural",
    "subquery_result_kinds",
    "subquery_quantified",
    "derived_regular",
    "lateral_correlated",
    "cte_nonrecursive",
    "cte_recursive",
    "set_union",
    "set_intersect",
    "set_except",
    "set_table_values",
    "table_explicit",
    "table_values_union",
    "table_subquery_exists",
    "grouping_aggregate_having",
    "grouping_with_rollup",
    "window_inline_named",
    "window_frames",
    "json_table_columns",
    "json_create_extract",
    "json_value_scalar",
    "json_schema_validation",
    "case_simple",
    "case_searched",
    "optimizer_hint_join_order",
    "optimizer_hint_index_level",
    "optimizer_hint_derived_pushdown",
    "function_deterministic_scalar",
    "function_aggregate",
    "regression_8041_union_view_charset",
    "regression_8041_desc_pk_index_merge",
    "regression_8041_subquery_materialization",
    "regression_8041_union_chain_flatten",
    "regression_8041_rollup_row_comparator",
    "regression_8041_antijoin_spill_null_key",
    "regression_8041_distinct_not_in",
    "regression_8041_hint_lexer",
    "scene_regular",
    "index_prefix",
    "index_descending",
    "index_functional",
    "type_numeric_boundaries",
    "type_string_lob_boundaries",
    "type_temporal_json_spatial",
    "function_version_import",
)


def _manifest() -> SchemaManifest:
    primary = IndexDef(
        "PRIMARY",
        (IndexPart(column_name="id"),),
        unique=True,
        primary=True,
    )
    tables = tuple(
        TableDef(
            f"t{ordinal}",
            False,
            (
                ColumnDef("id", "BIGINT UNSIGNED", False),
                ColumnDef("payload", "VARCHAR(64)", True, "utf8mb4", "utf8mb4_0900_ai_ci"),
                ColumnDef("amount", "INT", True),
                ColumnDef("tags", "JSON", True),
                ColumnDef("created_at", "DATETIME(6)", True),
            ),
            (
                primary,
                IndexDef(
                    "ix_id_desc",
                    (IndexPart(column_name="id", direction=SortDirection.DESC),),
                ),
                IndexDef(
                    "ix_payload_prefix",
                    (IndexPart(column_name="payload", prefix_length=8),),
                ),
                IndexDef(
                    "ix_payload_lower",
                    (IndexPart(expression=IndexExpression.lower_char("payload", 32)),),
                    kind=IndexKind.FUNCTIONAL,
                ),
            ),
        )
        for ordinal in range(2)
    )
    return SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "property_query",
        1,
        tables,
    )


def _target(feature_id: str) -> FeatureSpec:
    return FeatureSpec(
        feature_id=feature_id,
        family="property",
        min_version=(8, 0, 41),
        compatible_profiles=frozenset({SchemaProfile.REGULAR_INNODB.value}),
        ast_nodes=frozenset({"query_expression"}),
        guards=frozenset({"read_only_select"}),
    )


@settings(max_examples=10_000, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=2**63 - 1),
    case_ordinal=st.integers(min_value=0, max_value=10**9),
    feature_id=st.sampled_from(PROPERTY_FEATURES),
)
def test_ten_thousand_generated_queries_are_safe_bounded_and_byte_stable(
    seed: int, case_ordinal: int, feature_id: str
) -> None:
    generator = QueryGenerator()
    manifest = _manifest()
    target = _target(feature_id)
    budget = QueryBudget(
        max_tables=4,
        max_depth=3,
        max_ctes=2,
        max_set_branches=3,
        max_projection=12,
        max_predicates=12,
        max_intermediate_rows=20_000,
        max_output_rows=10_000,
    )

    first = generator.generate(
        manifest,
        target=target,
        seed=seed,
        case_ordinal=case_ordinal,
        budget=budget,
        estimated_rows_by_table={"t0": 50, "t1": 50},
    )
    second = generator.generate(
        manifest,
        target=target,
        seed=seed,
        case_ordinal=case_ordinal,
        budget=budget,
        estimated_rows_by_table={"t0": 50, "t1": 50},
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.complexity.within(budget)
    order_suffix = first.sql.rsplit(" ORDER BY ", maxsplit=1)[1]
    if " LIMIT " in order_suffix:
        order_suffix = order_suffix.split(" LIMIT ", maxsplit=1)[0]
    assert [item.removesuffix(" DESC") for item in order_suffix.split(", ")] == [
        str(i) for i in range(1, first.ast.scope.projection_count + 1)
    ]
    if first.ast.limit is not None or first.ast.has_window:
        assert first.ast.order_by.proves_total_order(first.ast.scope)
        assert first.ast.window_orders_are_total()
    ReadOnlyValidator().validate_text(first.sql)


@settings(max_examples=1_000, deadline=None)
@given(seed=st.integers(min_value=0, max_value=2**63 - 1))
def test_explicit_negative_lane_never_injects_a_second_statement(seed: int) -> None:
    generated = QueryGenerator().generate(
        _manifest(),
        target=_target("select_query_specification"),
        seed=seed,
        lane=QueryLane.NEGATIVE,
    )

    assert generated.expected_error is not None
    assert ";" not in generated.sql
    ReadOnlyValidator().validate_text(generated.sql)
