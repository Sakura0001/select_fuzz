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
        feature_id=feature_id,
        family="query",
        min_version=(8, 0, 41),
        compatible_profiles=frozenset({SchemaProfile.REGULAR_INNODB.value}),
        ast_nodes=frozenset({"query_expression"}),
        guards=frozenset({"read_only_select"}),
        capability_status=CapabilityStatus.GENERATOR_SUPPORTED,
        evidence_lock_ready=True,
    )


def _primary() -> IndexDef:
    return IndexDef(
        "PRIMARY",
        (IndexPart(column_name="id"),),
        unique=True,
        primary=True,
    )


def _manifest() -> SchemaManifest:
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
            for ordinal in range(3)
        ),
    )


_SELECT_LEAVES = {
    "star": ("SELECT * FROM", "select_star"),
    "qualified_star": ("SELECT `t`.* FROM", "select_qualified_star"),
    "order_by_alias": ("ORDER BY `q1`", "select_order_by_alias"),
    "order_by_expression": (
        "ORDER BY `t`.`id`",
        "select_order_by_expression",
    ),
    "modifier_all": ("SELECT ALL ", "select_modifier_all"),
    "modifier_distinctrow": (
        "SELECT DISTINCTROW ",
        "select_modifier_distinctrow",
    ),
    "modifier_straight_join": (
        "SELECT STRAIGHT_JOIN ",
        "select_modifier_straight_join",
    ),
    "modifier_sql_small_result": (
        "SELECT SQL_SMALL_RESULT ",
        "select_modifier_sql_small_result",
    ),
    "modifier_sql_big_result": (
        "SELECT SQL_BIG_RESULT ",
        "select_modifier_sql_big_result",
    ),
    "modifier_sql_buffer_result": (
        "SELECT SQL_BUFFER_RESULT ",
        "select_modifier_sql_buffer_result",
    ),
}


@pytest.mark.parametrize(
    ("variant", "needle", "tag"),
    [(variant, *expectation) for variant, expectation in _SELECT_LEAVES.items()],
)
def test_select_leaf_is_directed_typed_deterministic_and_read_only(
    variant: str,
    needle: str,
    tag: str,
) -> None:
    generator = QueryGenerator()
    kwargs = {
        "target": _target("select_query_specification"),
        "seed": 71,
        "lane": QueryLane.VALID,
        "directed_variant": variant,
        "estimated_rows_by_table": {"t0": 8, "t1": 8, "t2": 8},
    }

    first = generator.generate(_manifest(), **kwargs)
    second = generator.generate(_manifest(), **kwargs)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert needle in first.sql
    assert tag in first.feature_tags
    assert first.ast.order_by.proves_total_order(first.ast.scope)
    assert all(marker not in first.sql.upper() for marker in ("JSON_", "ST_", "MATCH("))
    if variant in {"star", "qualified_star"}:
        assert first.ast.scope.projection_count == len(_manifest().tables[0].columns)
        assert first.sql.endswith("ORDER BY 1")
    if variant == "modifier_distinctrow":
        assert first.ast.scope.unique_projection_sets
    ReadOnlyValidator().validate_text(first.sql)


def test_select_leaf_tags_are_reachable_from_normal_seed_selection() -> None:
    expected = frozenset(tag for _, tag in _SELECT_LEAVES.values())
    seen: set[str] = set()
    generator = QueryGenerator()
    target = _target("select_query_specification")

    for seed in range(1024):
        generated = generator.generate(
            _manifest(),
            target=target,
            seed=seed,
            lane=QueryLane.VALID,
            estimated_rows_by_table={"t0": 8, "t1": 8, "t2": 8},
        )
        seen.update(generated.feature_tags & expected)
        if seen == expected:
            break

    assert seen == expected


_SET_PRECEDENCE_CASES = (
    (
        "set_union",
        "precedence_union_intersect",
        (" UNION SELECT ", " INTERSECT SELECT "),
        (),
        "set_precedence_union_intersect",
        2,
    ),
    (
        "set_union",
        "parenthesized_union_intersect",
        (" UNION SELECT ", ") INTERSECT SELECT "),
        (),
        "set_parenthesized_union_intersect",
        1,
    ),
    (
        "set_except",
        "precedence_except_intersect",
        (" EXCEPT SELECT ", " INTERSECT SELECT "),
        (),
        "set_precedence_except_intersect",
        1,
    ),
    (
        "set_except",
        "parenthesized_except_intersect",
        (" EXCEPT SELECT ", ") INTERSECT SELECT "),
        (),
        "set_parenthesized_except_intersect",
        1,
    ),
    (
        "set_union",
        "precedence_union_except",
        (" UNION SELECT ", " EXCEPT SELECT "),
        (" UNION (SELECT ",),
        "set_precedence_union_except",
        2,
    ),
    (
        "set_union",
        "parenthesized_union_except",
        (" UNION (SELECT ", " EXCEPT SELECT "),
        (),
        "set_parenthesized_union_except",
        2,
    ),
)


@pytest.mark.parametrize(
    (
        "feature_id",
        "variant",
        "needles",
        "forbidden",
        "tag",
        "maximum",
    ),
    _SET_PRECEDENCE_CASES,
)
def test_mixed_set_precedence_and_parenthesized_reversal_leaves(
    feature_id: str,
    variant: str,
    needles: tuple[str, ...],
    forbidden: tuple[str, ...],
    tag: str,
    maximum: int,
) -> None:
    generated = QueryGenerator().generate(
        _manifest(),
        target=_target(feature_id),
        seed=79,
        lane=QueryLane.VALID,
        directed_variant=variant,
        estimated_rows_by_table={"t0": 8, "t1": 8, "t2": 8},
    )

    assert all(needle in generated.sql for needle in needles)
    assert all(needle not in generated.sql for needle in forbidden)
    assert tag in generated.feature_tags
    assert generated.ast.scope.max_rows == maximum
    assert generated.complexity.estimated_output_rows == maximum
    assert generated.ast.scope.unique_projection_sets == frozenset(
        {frozenset({1})}
    )
    assert all(marker not in generated.sql.upper() for marker in ("JSON_", "ST_", "MATCH("))
    ReadOnlyValidator().validate_text(generated.sql)


_SET_TYPE_CASES = tuple(
    (
        f"set_{operator}",
        f"{operator}_{domain}",
        f" {operator.upper()} ",
        token,
        f"set_type_{operator}_{domain}",
        2 if operator == "union" else 1,
    )
    for operator in ("union", "intersect", "except")
    for domain, token in (
        ("numeric", "CAST(1 AS SIGNED)"),
        ("text", "CAST('alpha' AS CHAR(64))"),
        ("binary", "X'01'"),
        ("temporal", "CAST('2024-01-01 00:00:00.000000' AS DATETIME(6))"),
    )
)


@pytest.mark.parametrize(
    ("feature_id", "variant", "operator", "type_token", "tag", "maximum"),
    _SET_TYPE_CASES,
)
def test_same_type_set_operation_matrix(
    feature_id: str,
    variant: str,
    operator: str,
    type_token: str,
    tag: str,
    maximum: int,
) -> None:
    generated = QueryGenerator().generate(
        _manifest(),
        target=_target(feature_id),
        seed=83,
        lane=QueryLane.VALID,
        directed_variant=variant,
        estimated_rows_by_table={"t0": 8, "t1": 8, "t2": 8},
    )

    assert operator in generated.sql
    assert type_token in generated.sql
    assert tag in generated.feature_tags
    assert generated.ast.scope.max_rows == maximum
    assert generated.complexity.estimated_output_rows == maximum
    assert generated.ast.scope.unique_projection_sets == frozenset(
        {frozenset({1})}
    )
    assert all(marker not in generated.sql.upper() for marker in ("JSON_", "ST_", "MATCH("))
    ReadOnlyValidator().validate_text(generated.sql)


@pytest.mark.parametrize(
    ("feature_id", "expected"),
    [
        (
            "set_union",
            frozenset(
                {
                    "set_precedence_union_intersect",
                    "set_parenthesized_union_intersect",
                    "set_precedence_union_except",
                    "set_parenthesized_union_except",
                    *(f"set_type_union_{domain}" for domain in ("numeric", "text", "binary", "temporal")),
                }
            ),
        ),
        (
            "set_intersect",
            frozenset(
                {f"set_type_intersect_{domain}" for domain in ("numeric", "text", "binary", "temporal")}
            ),
        ),
        (
            "set_except",
            frozenset(
                {
                    "set_precedence_except_intersect",
                    "set_parenthesized_except_intersect",
                    *(f"set_type_except_{domain}" for domain in ("numeric", "text", "binary", "temporal")),
                }
            ),
        ),
    ],
)
def test_set_matrix_leaf_tags_are_reachable_from_normal_seeds(
    feature_id: str,
    expected: frozenset[str],
) -> None:
    generator = QueryGenerator()
    target = _target(feature_id)
    seen: set[str] = set()

    for seed in range(2048):
        generated = generator.generate(
            _manifest(),
            target=target,
            seed=seed,
            lane=QueryLane.VALID,
            estimated_rows_by_table={"t0": 8, "t1": 8, "t2": 8},
        )
        seen.update(generated.feature_tags & expected)
        if seen == expected:
            break

    assert seen == expected
