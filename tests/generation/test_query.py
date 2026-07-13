from __future__ import annotations

from pathlib import Path
import re

import pytest

from select_fuzz.generation.catalog import CapabilityStatus, FeatureCatalog, FeatureSpec
from select_fuzz.generation.coverage import CoverageLedger, CoverageScheduler
from select_fuzz.generation.query import (
    EvidenceGateError,
    QueryBatchPlanner,
    QueryBudget,
    QueryBudgetExceeded,
    QueryGenerator,
    QueryLane,
    QueryMix,
    SUPPORTED_VARIANT_IDS,
    TargetNotReachable,
)
from select_fuzz.generation.query_ast import ExpectedErrorKind
from select_fuzz.generation.query_safety import ReadOnlyValidator, UnsafeQuery
from select_fuzz.generation.schema import (
    ColumnDef,
    ForeignKeyDef,
    IndexDef,
    IndexExpression,
    IndexKind,
    IndexPart,
    PartitionDef,
    SchemaManifest,
    SchemaProfile,
    SortDirection,
    TableDef,
)


def _target(feature_id: str, *profiles: SchemaProfile, ready: bool = True) -> FeatureSpec:
    return FeatureSpec(
        feature_id=feature_id,
        family="query",
        min_version=(8, 0, 41),
        compatible_profiles=frozenset(profile.value for profile in profiles),
        ast_nodes=frozenset({"query_expression"}),
        guards=frozenset({"read_only_select"}),
        capability_status=CapabilityStatus.GENERATOR_SUPPORTED,
        evidence_lock_ready=ready,
        unverified_evidence_sources=frozenset() if ready else frozenset({"manual"}),
    )


def _primary() -> IndexDef:
    return IndexDef(
        "PRIMARY",
        (IndexPart(column_name="id"),),
        primary=True,
        unique=True,
    )


def _regular_manifest(*, unique: bool = True, tables: int = 2) -> SchemaManifest:
    built: list[TableDef] = []
    for ordinal in range(tables):
        indexes: tuple[IndexDef, ...]
        if unique:
            indexes = (
                _primary(),
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
            )
        else:
            indexes = ()
        built.append(
            TableDef(
                name=f"t{ordinal}",
                temporary=False,
                columns=(
                    ColumnDef("id", "BIGINT UNSIGNED", False),
                    ColumnDef(
                        "payload",
                        "VARCHAR(64)",
                        True,
                        "utf8mb4",
                        "utf8mb4_0900_ai_ci",
                    ),
                    ColumnDef("amount", "INT", True),
                    ColumnDef("tags", "JSON", True),
                    ColumnDef("created_at", "DATETIME(6)", True),
                ),
                indexes=indexes,
            )
        )
    return SchemaManifest(
        profile=SchemaProfile.REGULAR_INNODB,
        target_feature_id="select_query_specification",
        seed=7,
        tables=tuple(built),
    )


def _profile_manifest(profile: SchemaProfile) -> SchemaManifest:
    if profile is SchemaProfile.PARTITIONED_INNODB:
        table = TableDef(
            "t0",
            False,
            (
                ColumnDef("id", "BIGINT UNSIGNED", False),
                ColumnDef("payload", "VARCHAR(64)", True, "utf8mb4", "utf8mb4_0900_ai_ci"),
            ),
            (_primary(),),
            partition=PartitionDef("HASH", ("id",), 4),
        )
    elif profile is SchemaProfile.TEMPORARY_INNODB:
        table = TableDef(
            "t0",
            True,
            (
                ColumnDef("id", "BIGINT UNSIGNED", False),
                ColumnDef("payload", "VARCHAR(64)", True, "utf8mb4", "utf8mb4_0900_ai_ci"),
            ),
            (_primary(),),
        )
    elif profile is SchemaProfile.FULLTEXT_INNODB:
        table = TableDef(
            "t0",
            False,
            (
                ColumnDef("id", "BIGINT UNSIGNED", False),
                ColumnDef("body", "LONGTEXT", False, "utf8mb4", "utf8mb4_0900_ai_ci"),
            ),
            (
                _primary(),
                IndexDef("ft_body", (IndexPart(column_name="body"),), kind=IndexKind.FULLTEXT),
            ),
        )
    elif profile is SchemaProfile.SPATIAL_INNODB:
        table = TableDef(
            "t0",
            False,
            (
                ColumnDef("id", "BIGINT UNSIGNED", False),
                ColumnDef("location", "POINT", False, srid=4326),
            ),
            (
                _primary(),
                IndexDef(
                    "sx_location",
                    (IndexPart(column_name="location"),),
                    kind=IndexKind.SPATIAL,
                ),
            ),
        )
    elif profile is SchemaProfile.JSON_MULTIVALUE_INNODB:
        table = TableDef(
            "t0",
            False,
            (
                ColumnDef("id", "BIGINT UNSIGNED", False),
                ColumnDef("tags", "JSON", False),
            ),
            (
                _primary(),
                IndexDef(
                    "mx_tags",
                    (IndexPart(expression=IndexExpression.json_unsigned_array("tags")),),
                    kind=IndexKind.MULTIVALUE,
                ),
            ),
        )
    else:
        raise AssertionError(profile)
    return SchemaManifest(
        profile=profile,
        target_feature_id="scene_target",
        seed=8,
        tables=(table,),
        requires_same_session=profile is SchemaProfile.TEMPORARY_INNODB,
    )


def _foreign_key_manifest() -> SchemaManifest:
    parent = TableDef(
        "t0",
        False,
        (
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("payload", "VARCHAR(64)", True, "utf8mb4", "utf8mb4_0900_ai_ci"),
        ),
        (_primary(),),
    )
    child = TableDef(
        "t1",
        False,
        (
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("parent_id", "BIGINT UNSIGNED", True),
        ),
        (_primary(), IndexDef("ix_parent", (IndexPart(column_name="parent_id"),))),
        foreign_keys=(ForeignKeyDef("fk_parent", ("parent_id",), "t0", ("id",)),),
    )
    return SchemaManifest(
        SchemaProfile.FOREIGN_KEY_GRAPH,
        "scene_foreign_key",
        9,
        (parent, child),
    )


def test_same_seed_is_byte_stable_and_query_ends_in_ordinal_ordering() -> None:
    generator = QueryGenerator()
    manifest = _regular_manifest()
    target = _target("select_query_specification", SchemaProfile.REGULAR_INNODB)

    first = generator.generate(manifest, target=target, seed=20260713, lane=QueryLane.VALID)
    second = generator.generate(manifest, target=target, seed=20260713, lane=QueryLane.VALID)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert re.search(r"ORDER BY 1(?:, 2)*\Z", first.sql)
    ReadOnlyValidator().validate_text(first.sql)


def test_recursive_cte_and_window_frame_have_exact_mysql_8041_snapshots() -> None:
    generator = QueryGenerator()
    manifest = _regular_manifest()
    recursive = generator.generate(
        manifest,
        target=_target("cte_recursive", SchemaProfile.REGULAR_INNODB),
        seed=17,
        lane=QueryLane.VALID,
    )
    framed = generator.generate(
        manifest,
        target=_target("window_frames", SchemaProfile.REGULAR_INNODB),
        seed=17,
        lane=QueryLane.VALID,
    )

    assert recursive.sql == (
        "WITH RECURSIVE `r` (`n`) AS (SELECT 1 AS `n` UNION ALL "
        "SELECT (`r`.`n` + 1) AS `n` FROM `r` AS `r` WHERE (`r`.`n` < 8)) "
        "SELECT `r0`.`n` AS `n` FROM `r` AS `r0` ORDER BY 1"
    )
    assert framed.sql == (
        "SELECT `t`.`id` AS `q1`, `t`.`payload` AS `q2`, "
        "SUM(`t`.`id`) OVER (ORDER BY `t`.`id` ROWS BETWEEN 1 PRECEDING AND "
        "1 FOLLOWING) AS `row_number` FROM `t0` AS `t` ORDER BY 1, 2, 3"
    )


def test_top_n_uses_a_proven_total_order_even_without_a_schema_unique_key() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(unique=False, tables=1),
        target=_target("select_query_specification", SchemaProfile.REGULAR_INNODB),
        seed=1,
        lane=QueryLane.VALID,
        require_top_n=True,
    )

    assert generated.ast.limit is not None
    assert generated.ast.order_by.proves_total_order(generated.ast.scope)
    assert generated.complexity.estimated_output_rows <= 1


def test_directed_scalar_literal_is_a_bounded_tableless_select() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(tables=1),
        target=_target("select_query_specification", SchemaProfile.REGULAR_INNODB),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant="scalar_literal",
    )

    assert generated.sql == "SELECT 1 AS `q1` ORDER BY 1"
    assert generated.complexity.tables == 0
    assert generated.complexity.estimated_output_rows == 1
    assert "scalar_literal" in generated.feature_tags


def test_directed_scalar_aggregate_is_a_bounded_tableless_count() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(tables=1),
        target=_target("select_query_specification", SchemaProfile.REGULAR_INNODB),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant="scalar_aggregate",
    )

    assert generated.sql == "SELECT COUNT(*) AS `row_count` ORDER BY 1"
    assert generated.complexity.tables == 0
    assert generated.complexity.estimated_output_rows == 1
    assert generated.ast.order_by.proves_total_order(generated.ast.scope)


@pytest.mark.parametrize(
    ("variant", "needles"),
    [
        ("left", (" LEFT JOIN ",)),
        ("left_subquery", (" LEFT JOIN ", "EXISTS (SELECT")),
        ("inner_subquery", (" INNER JOIN ", "EXISTS (SELECT")),
    ],
)
def test_directed_left_join_variants_are_bounded_and_deterministic(
    variant: str, needles: tuple[str, ...]
) -> None:
    feature_id = (
        "join_inner_cross_straight" if variant == "inner_subquery" else "join_outer_natural"
    )
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target(feature_id, SchemaProfile.REGULAR_INNODB),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant=variant,
    )

    assert all(needle in generated.sql for needle in needles)
    assert generated.complexity.within(QueryBudget())
    assert generated.ast.order_by.proves_total_order(generated.ast.scope)
    ReadOnlyValidator().validate_text(generated.sql)


def test_set_table_values_is_a_real_tableless_values_query() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("set_table_values", SchemaProfile.REGULAR_INNODB),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant="values_only",
    )

    assert generated.sql == "VALUES ROW(0) ORDER BY 1"
    assert generated.complexity.tables == 0
    assert generated.complexity.estimated_output_rows == 1
    assert generated.ast.order_by.proves_total_order(generated.ast.scope)


def test_values_limit_is_bounded_by_a_proven_ordinal_order() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("set_table_values", SchemaProfile.REGULAR_INNODB),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant="values_limit",
    )

    assert generated.sql == "VALUES ROW(0) ORDER BY 1 LIMIT 1"
    assert generated.ast.limit == 1
    assert generated.ast.order_by.proves_total_order(generated.ast.scope)


@pytest.mark.parametrize(
    ("feature_id", "variant", "needles"),
    [
        ("table_explicit", "table_only", ("TABLE `t0` ORDER BY 1, 2, 3, 4, 5",)),
        (
            "table_values_union",
            "table_values_union",
            ("TABLE `t0` UNION ALL VALUES ROW(", "ORDER BY 1, 2, 3, 4, 5"),
        ),
        (
            "table_subquery_exists",
            "table_subquery",
            ("EXISTS (TABLE `t0`)", "ORDER BY 1"),
        ),
    ],
)
def test_explicit_table_directed_variants_are_closed_bounded_queries(
    feature_id: str, variant: str, needles: tuple[str, ...]
) -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target(feature_id, SchemaProfile.REGULAR_INNODB),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant=variant,
    )

    assert all(needle in generated.sql for needle in needles)
    assert generated.complexity.within(QueryBudget())
    assert generated.complexity.estimated_output_rows <= 201
    ReadOnlyValidator().validate_text(generated.sql)


def test_normal_set_table_values_schedule_stays_with_its_catalog_signature() -> None:
    target = _target("set_table_values", SchemaProfile.REGULAR_INNODB)
    generated_sql = {
        QueryGenerator()
        .generate(
            _regular_manifest(),
            target=target,
            seed=seed,
            lane=QueryLane.VALID,
        )
        .sql
        for seed in range(64)
    }

    assert generated_sql
    assert all(sql.startswith("SELECT") and " UNION ALL VALUES " in sql for sql in generated_sql)


@pytest.mark.parametrize(
    ("feature_id", "needle"),
    [
        ("table_explicit", "TABLE `t0`"),
        ("table_values_union", "TABLE `t0` UNION ALL VALUES ROW("),
        ("table_subquery_exists", "EXISTS (TABLE `t0`)"),
    ],
)
def test_normal_explicit_table_features_match_their_catalog_debt(
    feature_id: str, needle: str
) -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target(feature_id, SchemaProfile.REGULAR_INNODB),
        seed=17,
        lane=QueryLane.VALID,
    )

    assert needle in generated.sql
    assert generated.target_feature_id == feature_id
    assert feature_id in generated.feature_tags


def test_derived_explicit_columns_is_an_independent_nonrandom_catalog_target() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("derived_explicit_columns", SchemaProfile.REGULAR_INNODB),
        seed=17,
        lane=QueryLane.VALID,
    )

    assert "derived_explicit_columns" in generated.feature_tags
    assert " AS `d` (`dq1`" in generated.sql


def test_set_branch_local_top_n_is_parenthesized_and_globally_ordered() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("set_branch_local_top_n", SchemaProfile.REGULAR_INNODB),
        seed=17,
        lane=QueryLane.VALID,
    )

    assert generated.sql.count("ORDER BY 1 LIMIT 2)") == 2
    assert ") UNION (" in generated.sql
    assert generated.sql.endswith("ORDER BY 1")
    assert "set_branch_local_top_n" in generated.feature_tags
    assert generated.complexity.estimated_output_rows <= 4
    ReadOnlyValidator().validate_text(generated.sql)


def test_nested_parenthesized_top_n_has_three_bounded_ordering_layers() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("select_nested_parenthesized_top_n", SchemaProfile.REGULAR_INNODB),
        seed=17,
        lane=QueryLane.VALID,
    )

    assert generated.sql.startswith("((SELECT")
    assert generated.sql.count("ORDER BY 1") == 3
    assert "LIMIT 5) ORDER BY 1 LIMIT 3) ORDER BY 1 LIMIT 2" in generated.sql
    assert "select_nested_parenthesized_top_n" in generated.feature_tags
    assert generated.complexity.depth == 3
    assert generated.complexity.estimated_output_rows <= 2
    ReadOnlyValidator().validate_text(generated.sql)


def test_explicit_table_projection_over_budget_is_unreachable_before_render() -> None:
    columns = (ColumnDef("id", "BIGINT UNSIGNED", False),) + tuple(
        ColumnDef(f"c{ordinal}", "INT", True) for ordinal in range(1, 13)
    )
    manifest = SchemaManifest(
        profile=SchemaProfile.REGULAR_INNODB,
        target_feature_id="table_explicit",
        seed=7,
        tables=(TableDef("t0", False, columns, (_primary(),)),),
    )

    with pytest.raises(TargetNotReachable, match="projection budget"):
        QueryGenerator().generate(
            manifest,
            target=_target("table_explicit", SchemaProfile.REGULAR_INNODB),
            seed=0,
            lane=QueryLane.VALID,
        )


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("limit_zero", "SELECT 1 AS `q1` ORDER BY 1 LIMIT 0"),
        ("offset_limit", "LIMIT 1 OFFSET 1"),
        ("limit_zero_offset", "SELECT 1 AS `q1` ORDER BY 1 LIMIT 0 OFFSET 1"),
        (
            "table_limit_zero_offset",
            "COUNT(*) AS `row_count` FROM `t0` AS `t` ORDER BY 1 LIMIT 0 OFFSET 1",
        ),
    ],
)
def test_limit_boundary_directed_variants_are_bounded_and_deterministic(
    variant: str, expected: str
) -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("select_query_specification", SchemaProfile.REGULAR_INNODB),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant=variant,
        estimated_rows_by_table={"t0": 8, "t1": 8},
    )

    assert expected in generated.sql
    assert generated.complexity.estimated_output_rows <= 1
    assert generated.ast.order_by.proves_total_order(generated.ast.scope)
    ReadOnlyValidator().validate_text(generated.sql)


def test_offset_directed_variant_keeps_table_domain_without_a_unique_key() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(unique=False),
        target=_target("select_query_specification", SchemaProfile.REGULAR_INNODB),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant="offset_limit",
    )

    assert generated.sql == (
        "SELECT COUNT(*) AS `row_count` FROM `t0` AS `t` ORDER BY 1 LIMIT 1 OFFSET 1"
    )
    assert generated.complexity.estimated_output_rows == 0
    assert generated.ast.order_by.proves_total_order(generated.ast.scope)


def test_derived_explicit_columns_directed_variant_renames_the_outer_scope() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("derived_regular", SchemaProfile.REGULAR_INNODB),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant="explicit_columns",
    )

    assert "AS `d` (`dq1`, `dq2`)" in generated.sql
    assert generated.sql.startswith("SELECT `d`.`dq1` AS `q1`, `d`.`dq2` AS `q2`")
    assert "derived_explicit_columns" in generated.feature_tags
    assert generated.ast.order_by.proves_total_order(generated.ast.scope)
    assert generated.complexity.within(QueryBudget())
    ReadOnlyValidator().validate_text(generated.sql)


def test_normal_derived_schedule_reaches_implicit_and_explicit_column_lists() -> None:
    target = _target("derived_regular", SchemaProfile.REGULAR_INNODB)
    generated_sql = {
        QueryGenerator()
        .generate(
            _regular_manifest(),
            target=target,
            seed=seed,
            lane=QueryLane.VALID,
        )
        .sql
        for seed in range(32)
    }

    assert any("AS `d` (`dq1`, `dq2`)" in sql for sql in generated_sql)
    assert any("AS `d` (`dq1`, `dq2`)" not in sql for sql in generated_sql)


def test_join_cast_variant_uses_a_real_typed_projection() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("join_inner_cross_straight", SchemaProfile.REGULAR_INNODB),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant="inner_cast",
    )

    assert " INNER JOIN " in generated.sql
    assert "CAST(" in generated.sql
    assert generated.complexity.within(QueryBudget())
    ReadOnlyValidator().validate_text(generated.sql)


def test_scalar_intersect_except_is_a_real_nested_set_query() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("set_intersect", SchemaProfile.REGULAR_INNODB),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant="scalar_intersect_except",
    )

    assert " INTERSECT " in generated.sql
    assert " EXCEPT " in generated.sql
    assert generated.complexity.tables == 0
    assert generated.complexity.within(QueryBudget())


@pytest.mark.parametrize("variant", ["table_limit", "scalar_limit"])
def test_subquery_limit_variants_have_a_proven_total_order(variant: str) -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("subquery_result_kinds", SchemaProfile.REGULAR_INNODB),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant=variant,
    )

    assert "(SELECT" in generated.sql
    assert generated.ast.limit == 1
    assert generated.ast.order_by.proves_total_order(generated.ast.scope)
    assert generated.complexity.within(QueryBudget())


def test_scalar_rollup_is_a_real_tableless_grouping_query() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("grouping_with_rollup", SchemaProfile.REGULAR_INNODB),
        seed=2,
        lane=QueryLane.VALID,
        directed_variant="scalar_rollup",
    )

    assert " GROUP BY 1 WITH ROLLUP" in generated.sql
    assert generated.complexity.tables == 0
    assert generated.complexity.within(QueryBudget())


@pytest.mark.parametrize(
    ("feature_id", "needle"),
    [
        ("select_parenthesized", "(SELECT"),
        ("join_inner_cross_straight", "JOIN"),
        ("join_outer_natural", "JOIN"),
        ("subquery_result_kinds", " (SELECT"),
        ("subquery_quantified", " (SELECT"),
        ("derived_regular", "FROM (SELECT"),
        ("lateral_correlated", "LATERAL"),
        ("cte_nonrecursive", "WITH `c0`"),
        ("cte_recursive", "WITH RECURSIVE"),
        ("set_union", " UNION "),
        ("set_intersect", " INTERSECT "),
        ("set_except", " EXCEPT "),
        ("set_table_values", "VALUES ROW("),
        ("grouping_aggregate_having", " HAVING "),
        ("grouping_with_rollup", "WITH ROLLUP"),
        ("window_inline_named", "WINDOW `w0` AS"),
        ("window_frames", "ROWS BETWEEN"),
        ("json_table_columns", "JSON_TABLE("),
        ("json_create_extract", "JSON_EXTRACT("),
        ("json_value_scalar", "JSON_VALUE("),
        ("json_schema_validation", "JSON_SCHEMA_VALID("),
        ("case_simple", "CASE "),
        ("case_searched", "CASE WHEN"),
        ("optimizer_hint_join_order", "/*+ JOIN_ORDER("),
        ("optimizer_hint_index_level", "/*+ INDEX("),
        ("optimizer_hint_derived_pushdown", "DERIVED_CONDITION_PUSHDOWN"),
        ("function_deterministic_scalar", "COALESCE("),
        ("function_aggregate", "COUNT("),
        ("regression_8041_union_view_charset", "UNION ALL"),
        ("regression_8041_desc_pk_index_merge", " OR "),
        ("regression_8041_subquery_materialization", " IN (SELECT DISTINCT"),
        ("regression_8041_union_chain_flatten", "UNION ALL"),
        ("regression_8041_rollup_row_comparator", "WITH ROLLUP"),
        ("regression_8041_antijoin_spill_null_key", "NOT EXISTS"),
        ("regression_8041_distinct_not_in", "SELECT DISTINCT"),
        ("regression_8041_hint_lexer", "/*+"),
        ("scene_regular", "SELECT"),
        ("index_prefix", "LIKE"),
        ("index_descending", "DESC"),
        ("index_functional", "LOWER("),
        ("type_numeric_boundaries", "CAST("),
        ("type_string_lob_boundaries", "OCTET_LENGTH("),
        ("type_temporal_json_spatial", "JSON_TYPE("),
        ("function_version_import", "COALESCE("),
    ],
)
def test_regular_profile_directed_renderers_are_reachable(feature_id: str, needle: str) -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target(feature_id, SchemaProfile.REGULAR_INNODB),
        seed=17,
        lane=QueryLane.VALID,
    )

    assert needle in generated.sql
    assert generated.feature_tags >= {feature_id, "lane_valid"}
    ReadOnlyValidator().validate_text(generated.sql)


@pytest.mark.parametrize(
    ("feature_id", "profile", "needle"),
    [
        ("partition_explicit_selection", SchemaProfile.PARTITIONED_INNODB, "PARTITION (`p0`)"),
        ("scene_partitioned", SchemaProfile.PARTITIONED_INNODB, "PARTITION (`p0`)"),
        ("scene_temporary", SchemaProfile.TEMPORARY_INNODB, "FROM `t0`"),
        ("scene_fulltext", SchemaProfile.FULLTEXT_INNODB, "MATCH("),
        ("index_fulltext", SchemaProfile.FULLTEXT_INNODB, "MATCH("),
        ("function_fulltext_spatial", SchemaProfile.FULLTEXT_INNODB, "MATCH("),
        ("scene_spatial", SchemaProfile.SPATIAL_INNODB, "ST_"),
        ("index_spatial", SchemaProfile.SPATIAL_INNODB, "ST_"),
        ("function_fulltext_spatial", SchemaProfile.SPATIAL_INNODB, "ST_"),
        ("type_temporal_json_spatial", SchemaProfile.SPATIAL_INNODB, "ST_"),
        ("scene_json_multivalue", SchemaProfile.JSON_MULTIVALUE_INNODB, "MEMBER OF"),
        ("index_multivalue", SchemaProfile.JSON_MULTIVALUE_INNODB, "MEMBER OF"),
        ("json_member_overlap", SchemaProfile.JSON_MULTIVALUE_INNODB, "JSON_OVERLAPS("),
        ("json_table_implicit_lateral", SchemaProfile.JSON_MULTIVALUE_INNODB, "JSON_TABLE("),
    ],
)
def test_profile_specific_directed_renderers_are_reachable(
    feature_id: str, profile: SchemaProfile, needle: str
) -> None:
    generated = QueryGenerator().generate(
        _profile_manifest(profile),
        target=_target(feature_id, profile),
        seed=31,
        lane=QueryLane.VALID,
    )
    assert needle in generated.sql
    ReadOnlyValidator().validate_text(generated.sql)


def test_foreign_key_scene_uses_declared_edge() -> None:
    generated = QueryGenerator().generate(
        _foreign_key_manifest(),
        target=_target("scene_foreign_key", SchemaProfile.FOREIGN_KEY_GRAPH),
        seed=2,
        lane=QueryLane.VALID,
    )

    assert "`c`.`parent_id` = `p`.`id`" in generated.sql
    ReadOnlyValidator().validate_text(generated.sql)


def test_rollup_regression_contains_a_real_row_comparator() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target(
            "regression_8041_rollup_row_comparator",
            SchemaProfile.REGULAR_INNODB,
        ),
        seed=1,
        lane=QueryLane.VALID,
    )

    assert "HAVING (ROW(" in generated.sql
    assert ") >= ROW(" in generated.sql


def test_grouping_prefers_a_non_identity_column_to_create_real_groups() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("grouping_aggregate_having", SchemaProfile.REGULAR_INNODB),
        seed=1,
        lane=QueryLane.VALID,
    )

    assert "GROUP BY `t`.`payload`" in generated.sql


@pytest.mark.parametrize(
    ("variant", "needle"),
    [("scalar", " = (SELECT"), ("row", "ROW("), ("exists", "EXISTS (SELECT")],
)
def test_subquery_result_kinds_have_directed_scalar_row_and_exists_lanes(
    variant: str, needle: str
) -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("subquery_result_kinds", SchemaProfile.REGULAR_INNODB),
        seed=4,
        lane=QueryLane.VALID,
        directed_variant=variant,
    )

    assert needle in generated.sql
    assert f"{variant}_subquery" in generated.feature_tags


def test_all_catalog_variants_have_a_registered_renderer() -> None:
    catalog_ids = {spec.feature_id for spec in FeatureCatalog.default()}
    assert SUPPORTED_VARIANT_IDS == catalog_ids

    registered = QueryGenerator.feature_catalog()
    assert {spec.feature_id for spec in registered} == catalog_ids
    assert {spec.feature_id for spec in registered.signature_targets(version=(8, 0, 41))} == {
        spec.feature_id for spec in registered if spec.evidence_lock_ready
    }


def test_unverified_official_evidence_is_not_schedulable_or_generatable(tmp_path: Path) -> None:
    blocked = _target("cte_recursive", SchemaProfile.REGULAR_INNODB, ready=False)
    catalog = FeatureCatalog((blocked,))

    assert catalog.signature_targets(version=(8, 0, 41)) == ()
    with pytest.raises(EvidenceGateError, match="evidence lock"):
        QueryGenerator().generate(
            _regular_manifest(),
            target=blocked,
            seed=1,
            lane=QueryLane.VALID,
        )


def test_default_lane_mix_is_exact_over_each_hundred_case_ordinals() -> None:
    generator = QueryGenerator()
    target = _target("select_query_specification", SchemaProfile.REGULAR_INNODB)
    counts = {lane: 0 for lane in QueryLane}

    for ordinal in range(100):
        query = generator.generate(
            _regular_manifest(),
            target=target,
            seed=44,
            case_ordinal=ordinal,
        )
        counts[query.lane] += 1

    assert counts == {
        QueryLane.VALID: 90,
        QueryLane.FREE_RANDOM: 5,
        QueryLane.NEGATIVE: 5,
    }
    assert QueryMix().identity() == "90:5:5"


def test_negative_lane_is_typed_expected_error_not_statement_injection() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("select_query_specification", SchemaProfile.REGULAR_INNODB),
        seed=6,
        lane=QueryLane.NEGATIVE,
    )

    assert generated.expected_error is not None
    assert generated.expected_error.kind in set(ExpectedErrorKind)
    assert ";" not in generated.sql
    assert "lane_negative" in generated.feature_tags
    ReadOnlyValidator().validate_text(generated.sql)


def test_adjustable_queries_per_round_follow_coverage_debt(tmp_path: Path) -> None:
    specs = (
        _target("select_query_specification", SchemaProfile.REGULAR_INNODB),
        _target("case_searched", SchemaProfile.REGULAR_INNODB),
    )
    ledger = CoverageLedger(tmp_path / "coverage.json")
    ledger.record("select_query_specification", hits=2)
    scheduler = CoverageScheduler(
        catalog=FeatureCatalog(specs),
        ledger=ledger,
        min_hits=2,
        version=(8, 0, 41),
        profiles=frozenset({SchemaProfile.REGULAR_INNODB.value}),
        schedule_seed=71,
    )

    batch = QueryBatchPlanner(QueryGenerator()).plan(
        _regular_manifest(),
        scheduler=scheduler,
        run_seed=100,
        start_case_ordinal=0,
        queries_per_round=5,
        lane=QueryLane.VALID,
    )

    assert len(batch) == 5
    assert {query.target_feature_id for query in batch} == {"case_searched"}
    assert [query.case_ordinal for query in batch] == [0, 1, 2, 3, 4]


def test_batch_planner_skips_a_schema_incompatible_debt_target() -> None:
    unreachable = _target(
        "regression_8041_desc_pk_index_merge",
        SchemaProfile.REGULAR_INNODB,
    )
    reachable = _target(
        "select_query_specification",
        SchemaProfile.REGULAR_INNODB,
    )

    class _SequenceScheduler:
        plan_start_ordinal = 0
        planned_case_count = 2

        def choose(self, *, case_ordinal: int) -> FeatureSpec:
            return (unreachable, reachable)[case_ordinal]

    original = _regular_manifest(tables=1).tables[0]
    manifest = SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "case_simple",
        1,
        (
            TableDef(
                original.name,
                original.temporary,
                original.columns,
                (_primary(),),
            ),
        ),
    )

    batch = QueryBatchPlanner(QueryGenerator()).plan(
        manifest,
        scheduler=_SequenceScheduler(),  # type: ignore[arg-type]
        run_seed=100,
        start_case_ordinal=0,
        queries_per_round=1,
        lane=QueryLane.VALID,
    )

    assert len(batch) == 1
    assert batch[0].target_feature_id == "select_query_specification"
    assert batch[0].case_ordinal == 0


def test_hard_intermediate_row_budget_rejects_cross_product() -> None:
    with pytest.raises(QueryBudgetExceeded, match="intermediate"):
        QueryGenerator().generate(
            _regular_manifest(),
            target=_target("join_inner_cross_straight", SchemaProfile.REGULAR_INNODB),
            seed=11,
            lane=QueryLane.VALID,
            budget=QueryBudget(max_intermediate_rows=100),
            estimated_rows_by_table={"t0": 100, "t1": 100},
            directed_variant="cross",
        )


def test_correlated_exists_uses_outer_times_inner_worst_case_budget() -> None:
    with pytest.raises(QueryBudgetExceeded, match="intermediate"):
        QueryGenerator().generate(
            _regular_manifest(),
            target=_target("subquery_result_kinds", SchemaProfile.REGULAR_INNODB),
            seed=11,
            lane=QueryLane.VALID,
            budget=QueryBudget(max_intermediate_rows=100_000),
            estimated_rows_by_table={"t0": 1_000, "t1": 1_000},
            directed_variant="exists",
        )


def test_desc_index_merge_regression_requires_a_real_descending_index() -> None:
    original = _regular_manifest(tables=1).tables[0]
    primary_only = TableDef(
        original.name,
        original.temporary,
        original.columns,
        (_primary(),),
    )
    manifest = SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "regression_8041_desc_pk_index_merge",
        1,
        (primary_only,),
    )

    with pytest.raises(TargetNotReachable, match="DESC"):
        QueryGenerator().generate(
            manifest,
            target=_target(
                "regression_8041_desc_pk_index_merge",
                SchemaProfile.REGULAR_INNODB,
            ),
            seed=1,
            lane=QueryLane.VALID,
        )


def test_free_random_lane_is_not_counted_as_a_directed_coverage_hit() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("cte_recursive", SchemaProfile.REGULAR_INNODB),
        seed=91,
        lane=QueryLane.FREE_RANDOM,
    )

    assert generated.coverage_eligible is False
    assert any(tag.startswith("free_shape_") for tag in generated.feature_tags)


def test_outer_join_estimate_and_unique_side_follow_the_preserved_relation() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("join_outer_natural", SchemaProfile.REGULAR_INNODB),
        seed=11,
        lane=QueryLane.VALID,
        estimated_rows_by_table={"t0": 10, "t1": 20},
        directed_variant="right",
    )

    assert generated.complexity.estimated_output_rows == 20
    assert generated.ast.scope.unique_projection_sets == frozenset({frozenset({2})})


def test_functional_index_predicate_matches_the_declared_key_expression() -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("index_functional", SchemaProfile.REGULAR_INNODB),
        seed=3,
        lane=QueryLane.VALID,
    )

    assert (
        "CAST(LOWER(`t`.`payload`) AS CHAR(32) CHARACTER SET utf8mb4) "
        "COLLATE utf8mb4_0900_ai_ci = 'alpha'"
    ) in generated.sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT RAND()",
        "SELECT NOW()",
        "SELECT UUID()",
        "SELECT CURDATE()",
        "SELECT CURRENT_USER",
        "SELECT LOAD_FILE('/tmp/x')",
        "SELECT SLEEP(1)",
        "SELECT 1 FOR UPDATE",
        "SELECT 1 FOR SHARE",
        "SELECT 1 INTO @x",
        "SELECT 1; DROP TABLE t",
        "WITH c AS (DELETE FROM t RETURNING id) SELECT * FROM c",
        "/*!50000 SELECT 1 */",
        "SELECT @@hostname",
        "SELECT app.side_effect()",
        "SELECT side_effect()",
        "SELECT `side_effect`()",
    ],
)
def test_validator_rejects_unsafe_or_nondeterministic_sql(sql: str) -> None:
    with pytest.raises(UnsafeQuery):
        ReadOnlyValidator().validate_text(sql)


def test_validator_is_quote_aware_and_allows_only_a_trailing_semicolon() -> None:
    validator = ReadOnlyValidator()
    validator.validate_text("SELECT 'DROP TABLE; RAND()' AS `safe` ORDER BY 1;")
    with pytest.raises(UnsafeQuery, match="multiple"):
        validator.validate_text("SELECT 1; SELECT 2")
