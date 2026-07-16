from __future__ import annotations

import pytest

from select_fuzz.generation.catalog import CapabilityStatus, FeatureSpec
from select_fuzz.generation.query import QueryGenerator, QueryLane, TargetNotReachable
from select_fuzz.generation.query_safety import ReadOnlyValidator
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexPart,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)


def _target(feature_id: str, *profiles: SchemaProfile) -> FeatureSpec:
    return FeatureSpec(
        feature_id=feature_id,
        family="query",
        min_version=(8, 0, 41),
        compatible_profiles=frozenset(profile.value for profile in profiles),
        ast_nodes=frozenset({"query_expression"}),
        guards=frozenset({"read_only_select"}),
        capability_status=CapabilityStatus.GENERATOR_SUPPORTED,
        evidence_lock_ready=True,
    )


def _primary() -> IndexDef:
    return IndexDef(
        "PRIMARY",
        (IndexPart(column_name="id"),),
        primary=True,
        unique=True,
    )


def _regular_manifest() -> SchemaManifest:
    return SchemaManifest(
        profile=SchemaProfile.REGULAR_INNODB,
        target_feature_id="select_query_specification",
        seed=7,
        tables=tuple(
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
                    ColumnDef("created_at", "DATETIME(6)", True),
                ),
                indexes=(_primary(),),
            )
            for ordinal in range(2)
        ),
    )


def _profile_manifest(profile: SchemaProfile) -> SchemaManifest:
    assert profile is SchemaProfile.SPATIAL_INNODB
    return SchemaManifest(
        profile=profile,
        target_feature_id="type_temporal_json_spatial",
        seed=8,
        tables=(
            TableDef(
                "t0",
                False,
                (
                    ColumnDef("id", "BIGINT UNSIGNED", False),
                    ColumnDef("location", "POINT", False, srid=4326),
                ),
                (_primary(),),
            ),
        ),
    )


@pytest.mark.parametrize(
    "manifest",
    [
        _regular_manifest(),
        _profile_manifest(SchemaProfile.SPATIAL_INNODB),
    ],
)
def test_temporal_type_domain_is_pure_bounded_and_has_a_null_witness(manifest) -> None:
    generated = QueryGenerator().generate(
        manifest,
        target=_target(
            "type_temporal_json_spatial",
            SchemaProfile.REGULAR_INNODB,
            SchemaProfile.SPATIAL_INNODB,
        ),
        seed=19,
        lane=QueryLane.VALID,
    )

    assert "CAST('1000-01-01 00:00:00.000000' AS DATETIME(6))" in generated.sql
    assert "CAST('9999-12-31 23:59:59.999999' AS DATETIME(6))" in generated.sql
    assert "TIMESTAMP('1970-01-01 00:00:01.000000')" in generated.sql
    assert "TIMESTAMP('2038-01-19 03:14:07.499999')" in generated.sql
    assert "NULL AS `temporal_null`" in generated.sql
    assert not {"JSON_", "ST_", "MATCH("} & {
        token for token in ("JSON_", "ST_", "MATCH(") if token in generated.sql.upper()
    }
    semantic_tags = generated.feature_tags - {
        "type_temporal_json_spatial",
        "lane_valid",
    }
    assert semantic_tags >= {
        "type_temporal",
        "temporal_datetime_fsp6_bounds",
        "temporal_timestamp_fsp6_bounds",
        "temporal_null_witness",
    }
    assert all(
        marker not in " ".join(sorted(semantic_tags)).upper()
        for marker in ("JSON", "SPATIAL", "ST_", "MATCH")
    )
    ReadOnlyValidator().validate_text(generated.sql)


_PREDICATE_VARIANTS = {
    "null_safe_eq": (" <=> ", "predicate_null_safe_eq"),
    "divide": (" / ", "predicate_divide"),
    "integer_divide": (" DIV ", "predicate_integer_divide"),
    "modulo": (" % ", "predicate_modulo"),
    "bit_and": (" & ", "predicate_bit_and"),
    "bit_or": (" | ", "predicate_bit_or"),
    "bit_xor": (" ^ ", "predicate_bit_xor"),
    "shift_left": (" << ", "predicate_shift_left"),
    "shift_right": (" >> ", "predicate_shift_right"),
    "logical_xor": (" XOR ", "predicate_logical_xor"),
    "unary_plus": ("(+", "predicate_unary_plus"),
    "unary_minus": ("(-", "predicate_unary_minus"),
    "between": (" BETWEEN ", "predicate_between"),
    "not_between": (" NOT BETWEEN ", "predicate_not_between"),
    "in_list_null": (" IN (", "predicate_in_list_null"),
    "not_in_list_null": (" NOT IN (", "predicate_not_in_list_null"),
    "like_escape": (" ESCAPE ", "predicate_like_escape"),
    "regexp_like": ("REGEXP_LIKE(", "predicate_regexp_like"),
    "is_true": (" IS TRUE", "predicate_is_true"),
    "is_false": (" IS FALSE", "predicate_is_false"),
    "is_unknown": (" IS UNKNOWN", "predicate_is_unknown"),
}

_AGGREGATE_VARIANTS_FOR_TEST = {
    "sum",
    "avg",
    "min",
    "max",
    "count_distinct",
    "group_null_having",
}


@pytest.mark.parametrize(
    ("variant", "expected", "tag"),
    [(variant, *expectation) for variant, expectation in _PREDICATE_VARIANTS.items()],
)
def test_operator_predicate_directed_variants_are_deterministic_and_read_only(
    variant: str,
    expected: str,
    tag: str,
) -> None:
    generator = QueryGenerator()
    kwargs = {
        "target": _target("function_deterministic_scalar", SchemaProfile.REGULAR_INNODB),
        "seed": 37,
        "lane": QueryLane.VALID,
        "directed_variant": variant,
    }

    first = generator.generate(_regular_manifest(), **kwargs)
    second = generator.generate(_regular_manifest(), **kwargs)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert expected in first.sql
    assert tag in first.feature_tags
    if "list_null" in variant:
        assert "NULL" in first.sql
    ReadOnlyValidator().validate_text(first.sql)


@pytest.mark.parametrize(
    ("variant", "expected", "tag"),
    [
        ("not_exists", "NOT EXISTS (SELECT", "subquery_not_exists"),
        ("not_in", " NOT IN (SELECT", "subquery_not_in"),
        ("not_in_null", " NOT IN (SELECT", "subquery_not_in_null"),
        (
            "not_exists_empty",
            "NOT EXISTS (SELECT",
            "subquery_not_exists_empty",
        ),
    ],
)
def test_subquery_null_and_empty_set_semantics_are_directed_and_read_only(
    variant: str,
    expected: str,
    tag: str,
) -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("subquery_result_kinds", SchemaProfile.REGULAR_INNODB),
        seed=43,
        lane=QueryLane.VALID,
        directed_variant=variant,
    )

    assert expected in generated.sql
    assert tag in generated.feature_tags
    if variant == "not_in_null":
        assert "UNION ALL VALUES ROW(NULL)" in generated.sql
    if variant == "not_exists_empty":
        assert "WHERE (1 = 0)" in generated.sql
    ReadOnlyValidator().validate_text(generated.sql)


@pytest.mark.parametrize(
    ("variant", "needles", "tag"),
    [
        ("multiple", ("WITH `c0`", ", `c1`"), "cte_multiple"),
        ("dependency", (", `c1`", "FROM `c0` AS"), "cte_dependency"),
        ("reuse", ("FROM `c0` AS `a`", "JOIN `c0` AS `b`"), "cte_reuse"),
    ],
)
def test_nonrecursive_cte_composition_variants(
    variant: str,
    needles: tuple[str, ...],
    tag: str,
) -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("cte_nonrecursive", SchemaProfile.REGULAR_INNODB),
        seed=47,
        lane=QueryLane.VALID,
        directed_variant=variant,
    )

    assert all(needle in generated.sql for needle in needles)
    assert tag in generated.feature_tags
    ReadOnlyValidator().validate_text(generated.sql)


def test_cte_reuse_rejects_a_temporary_base_table_and_random_mode_avoids_it() -> None:
    regular = _regular_manifest()
    table = regular.tables[0]
    manifest = SchemaManifest(
        profile=SchemaProfile.TEMPORARY_INNODB,
        target_feature_id="cte_nonrecursive",
        seed=regular.seed,
        tables=(
            TableDef(
                table.name,
                True,
                table.columns,
                table.indexes,
            ),
        ),
        requires_same_session=True,
    )
    target = _target("cte_nonrecursive", SchemaProfile.TEMPORARY_INNODB)
    generator = QueryGenerator()

    with pytest.raises(TargetNotReachable, match="temporary base table"):
        generator.generate(
            manifest,
            target=target,
            seed=0,
            lane=QueryLane.VALID,
            directed_variant="reuse",
        )

    generated = tuple(
        generator.generate(
            manifest,
            target=target,
            seed=seed,
            lane=QueryLane.VALID,
        )
        for seed in range(32)
    )
    assert all("cte_reuse" not in query.feature_tags for query in generated)


@pytest.mark.parametrize(
    ("feature_id", "variant", "operator", "tag"),
    [
        ("set_intersect", "intersect_all", " INTERSECT ALL ", "set_intersect_all"),
        ("set_except", "except_all", " EXCEPT ALL ", "set_except_all"),
    ],
)
def test_intersect_and_except_all_are_typed_bag_operations(
    feature_id: str,
    variant: str,
    operator: str,
    tag: str,
) -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target(feature_id, SchemaProfile.REGULAR_INNODB),
        seed=49,
        lane=QueryLane.VALID,
        directed_variant=variant,
        estimated_rows_by_table={"t0": 7, "t1": 11},
    )

    assert operator in generated.sql
    assert tag in generated.feature_tags
    assert not generated.ast.scope.unique_projection_sets
    assert generated.complexity.estimated_output_rows == 7
    ReadOnlyValidator().validate_text(generated.sql)


@pytest.mark.parametrize(
    ("variant", "expected", "tag"),
    [
        ("sum", "SUM(", "aggregate_sum"),
        ("avg", "AVG(", "aggregate_avg"),
        ("min", "MIN(", "aggregate_min"),
        ("max", "MAX(", "aggregate_max"),
        ("count_distinct", "COUNT(DISTINCT ", "aggregate_count_distinct"),
        (
            "group_null_having",
            " HAVING (",
            "aggregate_group_null_having",
        ),
    ],
)
def test_aggregate_semantic_variants(
    variant: str,
    expected: str,
    tag: str,
) -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target("function_aggregate", SchemaProfile.REGULAR_INNODB),
        seed=53,
        lane=QueryLane.VALID,
        directed_variant=variant,
    )

    assert expected in generated.sql
    assert tag in generated.feature_tags
    if variant == "group_null_having":
        assert " IS NULL" in generated.sql
    ReadOnlyValidator().validate_text(generated.sql)


@pytest.mark.parametrize(
    ("feature_id", "variant", "expected", "tag"),
    [
        ("window_inline_named", "rank", "RANK() OVER", "window_rank"),
        (
            "window_inline_named",
            "dense_rank",
            "DENSE_RANK() OVER",
            "window_dense_rank",
        ),
        ("window_inline_named", "lag", "LAG(", "window_lag"),
        ("window_inline_named", "lead", "LEAD(", "window_lead"),
        ("window_frames", "rows_frame", "ROWS BETWEEN", "window_rows_frame"),
        ("window_frames", "range_frame", "RANGE BETWEEN", "window_range_frame"),
    ],
)
def test_window_function_and_frame_variants(
    feature_id: str,
    variant: str,
    expected: str,
    tag: str,
) -> None:
    generated = QueryGenerator().generate(
        _regular_manifest(),
        target=_target(feature_id, SchemaProfile.REGULAR_INNODB),
        seed=59,
        lane=QueryLane.VALID,
        directed_variant=variant,
    )

    assert expected in generated.sql
    assert tag in generated.feature_tags
    ReadOnlyValidator().validate_text(generated.sql)


@pytest.mark.parametrize(
    ("feature_id", "expected_tags", "seed_count"),
    [
        (
            "function_deterministic_scalar",
            frozenset(tag for _, tag in _PREDICATE_VARIANTS.values()),
            2048,
        ),
        (
            "subquery_result_kinds",
            frozenset(
                {
                    "subquery_not_exists",
                    "subquery_not_in",
                    "subquery_not_in_null",
                    "subquery_not_exists_empty",
                }
            ),
            256,
        ),
        (
            "cte_nonrecursive",
            frozenset({"cte_multiple", "cte_dependency", "cte_reuse"}),
            256,
        ),
        (
            "function_aggregate",
            frozenset(f"aggregate_{variant}" for variant in _AGGREGATE_VARIANTS_FOR_TEST),
            512,
        ),
        (
            "window_inline_named",
            frozenset({"window_rank", "window_dense_rank", "window_lag", "window_lead"}),
            256,
        ),
        (
            "window_frames",
            frozenset({"window_rows_frame", "window_range_frame"}),
            64,
        ),
        ("set_intersect", frozenset({"set_intersect_all"}), 64),
        ("set_except", frozenset({"set_except_all"}), 64),
    ],
)
def test_semantic_leaf_tags_are_reachable_from_normal_seed_selection(
    feature_id: str,
    expected_tags: frozenset[str],
    seed_count: int,
) -> None:
    generator = QueryGenerator()
    target = _target(feature_id, SchemaProfile.REGULAR_INNODB)
    seen: set[str] = set()

    for seed in range(seed_count):
        generated = generator.generate(
            _regular_manifest(),
            target=target,
            seed=seed,
            lane=QueryLane.VALID,
            estimated_rows_by_table={"t0": 3, "t1": 4},
        )
        seen.update(generated.feature_tags & expected_tags)
        if seen == expected_tags:
            break

    assert seen == expected_tags
