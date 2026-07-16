from __future__ import annotations

from dataclasses import replace

import pytest

from select_fuzz.generation.catalog import CapabilityStatus, FeatureSpec
from select_fuzz.generation.query import (
    QueryBudget,
    QueryGenerator,
    QueryLane,
    TargetNotReachable,
)
from select_fuzz.generation.query_ast import (
    BinaryExpression,
    BinaryOperator,
    ColumnRef,
    IndexHint,
    IndexHintAction,
    IndexHintScope,
    JoinKind,
    JoinRelation,
    SqlType,
    TableRelation,
)
from select_fuzz.generation.query_render import render_relation
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


def _manifest() -> SchemaManifest:
    tables = tuple(
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
            ),
            indexes=(
                IndexDef(
                    "PRIMARY",
                    (IndexPart(column_name="id"),),
                    primary=True,
                    unique=True,
                ),
                IndexDef("ix_id", (IndexPart(column_name="id"),)),
            ),
        )
        for ordinal in range(3)
    )
    return SchemaManifest(
        profile=SchemaProfile.REGULAR_INNODB,
        target_feature_id="join_inner_cross_straight",
        seed=7,
        tables=tables,
    )


def _equality() -> BinaryExpression:
    return BinaryExpression(
        ColumnRef("t", "id", SqlType.NUMERIC),
        BinaryOperator.EQ,
        ColumnRef("u", "id", SqlType.NUMERIC),
        SqlType.BOOLEAN,
    )


def test_join_relation_closes_on_using_natural_and_comma_conditions() -> None:
    left = TableRelation("t0", "t")
    right = TableRelation("t1", "u")

    using = JoinRelation(left, right, JoinKind.INNER, using_columns=("id",))
    conditionless = JoinRelation(left, right, JoinKind.INNER)
    natural = JoinRelation(left, right, JoinKind.NATURAL_INNER)
    comma = JoinRelation(left, right, JoinKind.COMMA)

    assert render_relation(using).endswith("INNER JOIN `t1` AS `u` USING (`id`)")
    assert render_relation(conditionless).endswith("INNER JOIN `t1` AS `u`")
    assert "NATURAL INNER JOIN" in render_relation(natural)
    assert render_relation(comma) == "`t0` AS `t`, `t1` AS `u`"

    with pytest.raises(ValueError, match="cannot combine"):
        JoinRelation(
            left,
            right,
            JoinKind.INNER,
            _equality(),
            using_columns=("id",),
        )
    with pytest.raises(ValueError, match="USING"):
        JoinRelation(left, right, JoinKind.CROSS, using_columns=("id",))
    with pytest.raises(ValueError, match="unique"):
        JoinRelation(left, right, JoinKind.LEFT, using_columns=("id", "id"))


def test_index_hint_ast_is_a_typed_safe_closed_set() -> None:
    hint = IndexHint(
        IndexHintAction.FORCE,
        ("ix_id",),
        IndexHintScope.ORDER_BY,
    )
    relation = TableRelation("t0", "t", index_hints=(hint,))

    assert render_relation(relation) == ("`t0` AS `t` FORCE INDEX FOR ORDER BY (`ix_id`)")
    with pytest.raises(TypeError, match="action"):
        IndexHint("FORCE", ("ix_id",))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires at least one"):
        IndexHint(IndexHintAction.IGNORE, ())
    with pytest.raises(ValueError, match="duplicate"):
        TableRelation("t0", "t", index_hints=(hint, hint))


@pytest.mark.parametrize(
    ("feature_id", "variant", "needle", "tag"),
    [
        (
            "join_inner_cross_straight",
            "comma",
            "`t0` AS `t`, `t1` AS `u`",
            "join_comma",
        ),
        (
            "join_inner_cross_straight",
            "inner_using",
            "INNER JOIN `t1` AS `u` USING (`id`)",
            "join_inner_using",
        ),
        (
            "join_outer_natural",
            "left_using",
            "LEFT JOIN `t1` AS `u` USING (`id`)",
            "join_left_using",
        ),
        (
            "join_outer_natural",
            "right_using",
            "RIGHT JOIN `t1` AS `u` USING (`id`)",
            "join_right_using",
        ),
        (
            "join_inner_cross_straight",
            "natural_inner",
            "NATURAL INNER JOIN",
            "join_natural_inner",
        ),
        (
            "join_inner_cross_straight",
            "nested_three",
            ") LEFT JOIN `t2` AS `v` ON ",
            "join_nested_three",
        ),
    ],
)
def test_directed_join_leaf_is_typed_deterministic_and_read_only(
    feature_id: str,
    variant: str,
    needle: str,
    tag: str,
) -> None:
    generator = QueryGenerator()
    target = _target(feature_id)

    first = generator.generate(
        _manifest(),
        target=target,
        seed=41,
        lane=QueryLane.VALID,
        directed_variant=variant,
        estimated_rows_by_table={"t0": 8, "t1": 8, "t2": 8},
    )
    second = generator.generate(
        _manifest(),
        target=target,
        seed=41,
        lane=QueryLane.VALID,
        directed_variant=variant,
        estimated_rows_by_table={"t0": 8, "t1": 8, "t2": 8},
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    assert needle in first.sql
    assert tag in first.feature_tags
    assert first.complexity.within(QueryBudget())
    ReadOnlyValidator().validate_text(first.sql)


_INDEX_HINT_SCOPES = {
    "default": "",
    "join": " FOR JOIN",
    "order_by": " FOR ORDER BY",
    "group_by": " FOR GROUP BY",
}


@pytest.mark.parametrize("action", ["use", "force", "ignore"])
@pytest.mark.parametrize("scope", sorted(_INDEX_HINT_SCOPES))
def test_directed_table_index_hint_matrix_is_deterministic_and_read_only(
    action: str,
    scope: str,
) -> None:
    variant = f"index_hint_{action}_{scope}"
    generator = QueryGenerator()
    kwargs = {
        "target": _target("join_inner_cross_straight"),
        "seed": 73,
        "lane": QueryLane.VALID,
        "directed_variant": variant,
        "estimated_rows_by_table": {"t0": 8, "t1": 8, "t2": 8},
    }

    first = generator.generate(_manifest(), **kwargs)
    second = generator.generate(_manifest(), **kwargs)

    expected = f" {action.upper()} INDEX{_INDEX_HINT_SCOPES[scope]} (`ix_id`)"
    assert first.canonical_bytes() == second.canonical_bytes()
    assert expected in first.sql
    assert f"table_index_hint_{action}_{scope}" in first.feature_tags
    ReadOnlyValidator().validate_text(first.sql)


def test_undirected_join_never_selects_an_unreachable_index_hint() -> None:
    manifest = _manifest()
    manifest = replace(
        manifest,
        tables=tuple(replace(table, indexes=()) for table in manifest.tables),
    )
    generator = QueryGenerator()
    target = _target("join_inner_cross_straight")

    for seed in range(64):
        generated = generator.generate(
            manifest,
            target=target,
            seed=seed,
            lane=QueryLane.VALID,
            estimated_rows_by_table={"t0": 8, "t1": 8, "t2": 8},
        )
        assert not any(tag.startswith("table_index_hint_") for tag in generated.feature_tags)

    with pytest.raises(TargetNotReachable, match="BTREE"):
        generator.generate(
            manifest,
            target=target,
            seed=1,
            lane=QueryLane.VALID,
            directed_variant="index_hint_force_join",
            estimated_rows_by_table={"t0": 8, "t1": 8, "t2": 8},
        )


def test_index_hints_use_visible_primary_instead_of_an_invisible_secondary() -> None:
    manifest = _manifest()
    hidden = IndexDef(
        "ix_id_hidden",
        (IndexPart(column_name="id"),),
        visible=False,
    )
    left = replace(
        manifest.tables[0],
        indexes=(manifest.tables[0].indexes[0], hidden),
    )
    manifest = replace(manifest, tables=(left, *manifest.tables[1:]))
    generator = QueryGenerator()
    target = _target("join_inner_cross_straight")

    directed = generator.generate(
        manifest,
        target=target,
        seed=1,
        lane=QueryLane.VALID,
        directed_variant="index_hint_force_group_by",
        estimated_rows_by_table={"t0": 8, "t1": 8, "t2": 8},
    )

    assert "FORCE INDEX FOR GROUP BY (PRIMARY)" in directed.sql
    assert "ix_id_hidden" not in directed.sql

    saw_random_hint = False
    for seed in range(256):
        generated = generator.generate(
            manifest,
            target=target,
            seed=seed,
            lane=QueryLane.VALID,
            estimated_rows_by_table={"t0": 8, "t1": 8, "t2": 8},
        )
        assert "ix_id_hidden" not in generated.sql
        if any(tag.startswith("table_index_hint_") for tag in generated.feature_tags):
            saw_random_hint = True
            assert " INDEX" in generated.sql
            assert "(PRIMARY)" in generated.sql

    assert saw_random_hint


def test_directed_index_hint_rejects_a_table_with_only_invisible_btree_indexes() -> None:
    manifest = _manifest()
    hidden = IndexDef(
        "ix_id_hidden",
        (IndexPart(column_name="id"),),
        visible=False,
    )
    left = replace(manifest.tables[0], indexes=(hidden,))
    manifest = replace(manifest, tables=(left, *manifest.tables[1:]))

    with pytest.raises(TargetNotReachable, match="visible BTREE"):
        QueryGenerator().generate(
            manifest,
            target=_target("join_inner_cross_straight"),
            seed=1,
            lane=QueryLane.VALID,
            directed_variant="index_hint_ignore_group_by",
            estimated_rows_by_table={"t0": 8, "t1": 8, "t2": 8},
        )


def test_optimizer_index_hint_falls_back_from_hidden_secondary_to_primary() -> None:
    manifest = _manifest()
    hidden = IndexDef(
        "ix_id_desc",
        (IndexPart(column_name="id"),),
        visible=False,
    )
    left = replace(
        manifest.tables[0],
        indexes=(manifest.tables[0].indexes[0], hidden),
    )
    manifest = replace(manifest, tables=(left, *manifest.tables[1:]))

    generated = QueryGenerator().generate(
        manifest,
        target=_target("optimizer_hint_index_level"),
        seed=5,
        lane=QueryLane.VALID,
        estimated_rows_by_table={"t0": 8, "t1": 8, "t2": 8},
    )

    assert "/*+ INDEX(t PRIMARY) */" in generated.sql
    assert "ix_id_desc" not in generated.sql
    assert "optimizer_hint_index_primary" in generated.feature_tags


def test_optimizer_index_hint_rejects_only_invisible_btree_indexes() -> None:
    manifest = _manifest()
    hidden = IndexDef(
        "ix_id_desc",
        (IndexPart(column_name="id"),),
        visible=False,
    )
    left = replace(manifest.tables[0], indexes=(hidden,))
    manifest = replace(manifest, tables=(left, *manifest.tables[1:]))

    with pytest.raises(TargetNotReachable, match="visible BTREE"):
        QueryGenerator().generate(
            manifest,
            target=_target("optimizer_hint_index_level"),
            seed=5,
            lane=QueryLane.VALID,
            estimated_rows_by_table={"t0": 8, "t1": 8, "t2": 8},
        )
