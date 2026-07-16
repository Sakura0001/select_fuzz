from __future__ import annotations

from dataclasses import dataclass

import pytest

from select_fuzz.generation.catalog import CapabilityStatus, FeatureSpec
from select_fuzz.generation.query import QueryGenerator, QueryLane
from select_fuzz.generation.query_ast import JoinRelation, SelectQuery, SqlType
from select_fuzz.generation.query_safety import ReadOnlyValidator
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexPart,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)


@dataclass(frozen=True, slots=True)
class _ScalarRecipe:
    variant: str
    snippets: tuple[str, ...]
    tags: frozenset[str]
    result_types: tuple[SqlType, ...]


_BOOLEAN = SqlType.BOOLEAN
_NUMERIC = SqlType.NUMERIC


def _comparison_recipe(position: str) -> _ScalarRecipe:
    operators = ("=", "<>", "<", "<=", ">", ">=", "<=>")
    if position == "left":
        snippets = tuple(f"(NULL {operator} 1)" for operator in operators)
    elif position == "right":
        snippets = tuple(f"(1 {operator} NULL)" for operator in operators)
    else:
        snippets = tuple(f"(NULL {operator} NULL)" for operator in operators)
    return _ScalarRecipe(
        f"comparison_null_{position}",
        snippets,
        frozenset(
            {
                f"predicate_comparison_null_{position}",
                f"predicate_null_safe_eq_{position}",
            }
        ),
        (_BOOLEAN,) * len(operators),
    )


def _numeric_binary_recipe(
    family: str,
    position: str,
    operators: tuple[str, ...],
    nonnull: int,
) -> _ScalarRecipe:
    if position == "left":
        snippets = tuple(f"(NULL {operator} {nonnull})" for operator in operators)
    elif position == "right":
        snippets = tuple(f"({nonnull} {operator} NULL)" for operator in operators)
    else:
        snippets = tuple(f"(NULL {operator} NULL)" for operator in operators)
    return _ScalarRecipe(
        f"{family}_null_{position}",
        snippets,
        frozenset({f"predicate_{family}_null_{position}"}),
        (_NUMERIC,) * len(operators),
    )


_SCALAR_RECIPES = (
    *(_comparison_recipe(position) for position in ("left", "right", "both")),
    *(
        _numeric_binary_recipe(
            "arithmetic",
            position,
            ("+", "-", "*", "/", "DIV", "%"),
            2,
        )
        for position in ("left", "right", "both")
    ),
    *(
        _numeric_binary_recipe(
            "bitwise",
            position,
            ("&", "|", "^", "<<", ">>"),
            1,
        )
        for position in ("left", "right", "both")
    ),
    _ScalarRecipe(
        "logical_null_left",
        (
            "(NULL AND 0)",
            "(NULL AND 1)",
            "(NULL OR 0)",
            "(NULL OR 1)",
            "(NULL XOR 1)",
        ),
        frozenset({"predicate_logical_null_left"}),
        (_BOOLEAN,) * 5,
    ),
    _ScalarRecipe(
        "logical_null_right",
        (
            "(0 AND NULL)",
            "(1 AND NULL)",
            "(0 OR NULL)",
            "(1 OR NULL)",
            "(1 XOR NULL)",
        ),
        frozenset({"predicate_logical_null_right"}),
        (_BOOLEAN,) * 5,
    ),
    _ScalarRecipe(
        "logical_null_both",
        ("(NULL AND NULL)", "(NULL OR NULL)", "(NULL XOR NULL)"),
        frozenset({"predicate_logical_null_both"}),
        (_BOOLEAN,) * 3,
    ),
    _ScalarRecipe(
        "like_regexp_null_left",
        (
            "(NULL LIKE 'a%' ESCAPE '\\\\')",
            "REGEXP_LIKE(NULL, '^a')",
        ),
        frozenset({"predicate_like_null_left", "predicate_regexp_null_left"}),
        (_BOOLEAN, _BOOLEAN),
    ),
    _ScalarRecipe(
        "like_regexp_null_right",
        (
            "('abc' LIKE NULL ESCAPE '\\\\')",
            "REGEXP_LIKE('abc', NULL)",
        ),
        frozenset({"predicate_like_null_right", "predicate_regexp_null_right"}),
        (_BOOLEAN, _BOOLEAN),
    ),
    _ScalarRecipe(
        "like_regexp_null_both",
        (
            "(NULL LIKE NULL ESCAPE '\\\\')",
            "REGEXP_LIKE(NULL, NULL)",
        ),
        frozenset({"predicate_like_null_both", "predicate_regexp_null_both"}),
        (_BOOLEAN, _BOOLEAN),
    ),
    _ScalarRecipe(
        "between_null_value",
        ("(NULL BETWEEN 1 AND 10)", "(NULL NOT BETWEEN 1 AND 10)"),
        frozenset(
            {"predicate_between_null_value", "predicate_not_between_null_value"}
        ),
        (_BOOLEAN, _BOOLEAN),
    ),
    _ScalarRecipe(
        "between_null_lower",
        ("(5 BETWEEN NULL AND 10)", "(5 NOT BETWEEN NULL AND 10)"),
        frozenset(
            {"predicate_between_null_lower", "predicate_not_between_null_lower"}
        ),
        (_BOOLEAN, _BOOLEAN),
    ),
    _ScalarRecipe(
        "between_null_upper",
        ("(5 BETWEEN 1 AND NULL)", "(5 NOT BETWEEN 1 AND NULL)"),
        frozenset(
            {"predicate_between_null_upper", "predicate_not_between_null_upper"}
        ),
        (_BOOLEAN, _BOOLEAN),
    ),
    _ScalarRecipe(
        "between_null_bounds",
        ("(5 BETWEEN NULL AND NULL)", "(5 NOT BETWEEN NULL AND NULL)"),
        frozenset(
            {"predicate_between_null_bounds", "predicate_not_between_null_bounds"}
        ),
        (_BOOLEAN, _BOOLEAN),
    ),
    _ScalarRecipe(
        "between_null_all",
        ("(NULL BETWEEN NULL AND NULL)", "(NULL NOT BETWEEN NULL AND NULL)"),
        frozenset({"predicate_between_null_all", "predicate_not_between_null_all"}),
        (_BOOLEAN, _BOOLEAN),
    ),
    _ScalarRecipe(
        "in_null_left",
        ("(NULL IN (1, 2))", "(NULL NOT IN (1, 2))"),
        frozenset({"predicate_in_null_left", "predicate_not_in_null_left"}),
        (_BOOLEAN, _BOOLEAN),
    ),
    _ScalarRecipe(
        "in_null_right",
        (
            "(1 IN (1, NULL))",
            "(3 IN (1, NULL))",
            "(1 NOT IN (1, NULL))",
            "(3 NOT IN (1, NULL))",
        ),
        frozenset(
            {
                "predicate_in_null_right_match",
                "predicate_in_null_right_no_match",
                "predicate_not_in_null_right_match",
                "predicate_not_in_null_right_no_match",
            }
        ),
        (_BOOLEAN,) * 4,
    ),
    _ScalarRecipe(
        "in_null_both",
        ("(NULL IN (1, NULL))", "(NULL NOT IN (1, NULL))"),
        frozenset({"predicate_in_null_both", "predicate_not_in_null_both"}),
        (_BOOLEAN, _BOOLEAN),
    ),
)


_JOIN_RECIPES = {
    "nullable_key_left": (
        "INNER JOIN `t1` AS `u` ON (`t`.`amount` = `u`.`id`)",
        "join_nullable_key_left",
    ),
    "nullable_key_right": (
        "INNER JOIN `t1` AS `u` ON (`t`.`id` = `u`.`amount`)",
        "join_nullable_key_right",
    ),
    "nullable_key_both": (
        "INNER JOIN `t1` AS `u` ON (`t`.`amount` = `u`.`amount`)",
        "join_nullable_key_both",
    ),
}


_AGGREGATE_SNIPPETS = (
    "COUNT(*)",
    "SUM(`t`.`amount`)",
    "AVG(`t`.`amount`)",
    "MIN(`t`.`amount`)",
    "MAX(`t`.`amount`)",
    "COUNT(`t`.`amount`)",
    "COUNT(DISTINCT `t`.`amount`)",
    "WHERE (`t`.`amount` IS NULL)",
)
_AGGREGATE_TAGS = frozenset(
    {
        "aggregate_all_null",
        "aggregate_sum_all_null",
        "aggregate_avg_all_null",
        "aggregate_min_all_null",
        "aggregate_max_all_null",
        "aggregate_count_all_null",
        "aggregate_count_distinct_all_null",
    }
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
                    ColumnDef("id", "BIGINT", False),
                    ColumnDef("amount", "INT", True),
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
            for ordinal in range(2)
        ),
    )


@pytest.mark.parametrize("recipe", _SCALAR_RECIPES, ids=lambda recipe: recipe.variant)
def test_scalar_null_matrix_is_typed_deterministic_and_read_only(
    recipe: _ScalarRecipe,
) -> None:
    generator = QueryGenerator()
    kwargs = {
        "target": _target("function_deterministic_scalar"),
        "seed": 80_410,
        "lane": QueryLane.VALID,
        "directed_variant": recipe.variant,
        "estimated_rows_by_table": {"t0": 3, "t1": 3},
    }

    first = generator.generate(_manifest(), **kwargs)
    second = generator.generate(_manifest(), **kwargs)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert all(snippet in first.sql for snippet in recipe.snippets)
    assert first.feature_tags >= recipe.tags
    assert isinstance(first.ast.body, SelectQuery)
    assert tuple(
        projection.expression.sql_type for projection in first.ast.body.projection
    ) == recipe.result_types
    assert first.ast.body.source is None
    assert first.ast.scope.max_rows == 1
    ReadOnlyValidator().validate_text(first.sql)


@pytest.mark.parametrize(
    ("variant", "snippet", "tag"),
    [
        (variant, snippet, tag)
        for variant, (snippet, tag) in _JOIN_RECIPES.items()
    ],
)
def test_nullable_key_join_matrix_is_deterministic_and_read_only(
    variant: str,
    snippet: str,
    tag: str,
) -> None:
    generator = QueryGenerator()
    kwargs = {
        "target": _target("join_inner_cross_straight"),
        "seed": 80_411,
        "lane": QueryLane.VALID,
        "directed_variant": variant,
        "estimated_rows_by_table": {"t0": 3, "t1": 3},
    }

    first = generator.generate(_manifest(), **kwargs)
    second = generator.generate(_manifest(), **kwargs)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert snippet in first.sql
    assert tag in first.feature_tags
    assert isinstance(first.ast.body, SelectQuery)
    assert isinstance(first.ast.body.source, JoinRelation)
    assert first.ast.body.source.predicate is not None
    assert first.ast.body.source.predicate.sql_type is SqlType.BOOLEAN
    assert tuple(
        projection.expression.sql_type for projection in first.ast.body.projection
    ) == (SqlType.NUMERIC, SqlType.NUMERIC)
    assert not first.ast.scope.unique_projection_sets
    assert first.complexity.estimated_output_rows == 9
    ReadOnlyValidator().validate_text(first.sql)


def test_all_null_aggregate_is_typed_deterministic_and_read_only() -> None:
    generator = QueryGenerator()
    kwargs = {
        "target": _target("function_aggregate"),
        "seed": 80_412,
        "lane": QueryLane.VALID,
        "directed_variant": "aggregate_all_null",
        "estimated_rows_by_table": {"t0": 3, "t1": 3},
    }

    first = generator.generate(_manifest(), **kwargs)
    second = generator.generate(_manifest(), **kwargs)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert all(snippet in first.sql for snippet in _AGGREGATE_SNIPPETS)
    assert first.feature_tags >= _AGGREGATE_TAGS
    assert isinstance(first.ast.body, SelectQuery)
    assert tuple(
        projection.expression.sql_type for projection in first.ast.body.projection
    ) == (SqlType.NUMERIC,) * 7
    assert first.ast.body.predicate is not None
    assert first.ast.body.predicate.sql_type is SqlType.BOOLEAN
    assert first.ast.scope.max_rows == 1
    ReadOnlyValidator().validate_text(first.sql)


@pytest.mark.parametrize(
    ("feature_id", "expected_tags", "seed_count"),
    [
        (
            "function_deterministic_scalar",
            frozenset(tag for recipe in _SCALAR_RECIPES for tag in recipe.tags),
            4_096,
        ),
        (
            "join_inner_cross_straight",
            frozenset(tag for _, tag in _JOIN_RECIPES.values()),
            2_048,
        ),
        ("function_aggregate", _AGGREGATE_TAGS, 512),
    ],
)
def test_null_matrix_tags_are_reachable_from_normal_seed_selection(
    feature_id: str,
    expected_tags: frozenset[str],
    seed_count: int,
) -> None:
    generator = QueryGenerator()
    manifest = _manifest()
    target = _target(feature_id)
    seen: set[str] = set()

    for seed in range(seed_count):
        generated = generator.generate(
            manifest,
            target=target,
            seed=seed,
            lane=QueryLane.VALID,
            estimated_rows_by_table={"t0": 3, "t1": 3},
        )
        seen.update(generated.feature_tags & expected_tags)
        if seen == expected_tags:
            break

    assert seen == expected_tags
