from __future__ import annotations

import random

import pytest

from select_fuzz.generation.query import QueryGenerator, TargetNotReachable
from select_fuzz.generation.query_ast import SqlType
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexExpression,
    IndexKind,
    IndexPart,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)


def table(name: str, *, unique: bool = True) -> TableDef:
    primary = (
        IndexDef(
            "PRIMARY",
            (IndexPart(column_name="id"),),
            unique=True,
            primary=True,
        ),
    )
    return TableDef(
        name,
        False,
        (
            ColumnDef("id", "BIGINT", False),
            ColumnDef(
                "payload",
                "VARCHAR(10)",
                True,
                "utf8mb4",
                "utf8mb4_0900_ai_ci",
            ),
        ),
        primary if unique else (),
    )


def manifest(*, unique: bool = True) -> SchemaManifest:
    return SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "query_internal",
        1,
        (table("left_table", unique=unique), table("right_table", unique=unique)),
    )


def test_join_builder_covers_every_directed_kind_unique_and_nonunique_estimate() -> None:
    generator = QueryGenerator()
    unique_manifest = manifest(unique=True)
    rows = {"left_table": 2, "right_table": 3}
    for outer, variants in (
        (False, ("inner", "cross", "straight")),
        (True, ("left", "right", "natural_left", "natural_right", "left_subquery")),
    ):
        for variant in variants:
            built = generator._join(
                unique_manifest,
                rows,
                random.Random(1),
                outer=outer,
                directed=variant,
            )
            assert f"join_{variant}" in built.extra_tags

    nonunique = manifest(unique=False)
    for outer, variant in ((False, "inner"), (True, "left"), (True, "right")):
        assert generator._join(
            nonunique,
            rows,
            random.Random(1),
            outer=outer,
            directed=variant,
        ).complexity.estimated_output_rows > 0


def test_type_mapping_and_unique_key_analysis_cover_all_storage_categories() -> None:
    assert QueryGenerator._type(ColumnDef("value", "INT", False)) is SqlType.NUMERIC
    assert QueryGenerator._type(
        ColumnDef(
            "value", "VARCHAR(2)", False, "utf8mb4", "utf8mb4_0900_ai_ci"
        )
    ) is SqlType.TEXT
    assert QueryGenerator._type(ColumnDef("value", "DATE", False)) is SqlType.TEMPORAL
    assert QueryGenerator._type(ColumnDef("value", "JSON", False)) is SqlType.JSON
    assert QueryGenerator._type(ColumnDef("value", "POINT", False, srid=0)) is SqlType.SPATIAL
    assert QueryGenerator._type(ColumnDef("value", "BINARY(2)", False)) is SqlType.BINARY

    candidate = table("candidate")
    assert QueryGenerator._unique_key(candidate) == ("id",)
    assert QueryGenerator._unique_key(table("candidate", unique=False)) is None

    nullable = ColumnDef("id", "BIGINT", True)
    nullable_index = IndexDef(
        "uq", (IndexPart(column_name="id"),), unique=True
    )
    assert QueryGenerator._unique_key(
        TableDef("nullable", False, (nullable,), (nullable_index,))
    ) is None

    prefix = IndexDef(
        "uq", (IndexPart(column_name="payload", prefix_length=1),), unique=True
    )
    assert QueryGenerator._unique_key(
        TableDef("prefixed", False, candidate.columns, (prefix,))
    ) is None

    functional = IndexDef(
        "uq",
        (IndexPart(expression=IndexExpression.lower_char("payload", 2)),),
        unique=True,
        kind=IndexKind.FUNCTIONAL,
    )
    assert QueryGenerator._unique_key(
        TableDef("functional", False, candidate.columns, (functional,))
    ) is None


def test_simple_and_negative_builders_cover_singleton_free_random_and_all_mutations() -> None:
    generator = QueryGenerator()
    unique_manifest = manifest(unique=True)
    nonunique_manifest = manifest(unique=False)
    rows = {"left_table": 2, "right_table": 3}
    assert "top_n" in generator._simple(
        nonunique_manifest, rows, top_n=True, free_random=False
    ).extra_tags
    assert generator._simple(
        unique_manifest, rows, top_n=True, free_random=True
    ).complexity.predicates == 2

    class FixedRandom:
        def __init__(self, value: int) -> None:
            self.value = value

        def randrange(self, stop: int) -> int:
            assert stop == 3
            return self.value

    tags = {
        next(iter(generator._build_negative(unique_manifest, rng=FixedRandom(value), rows=rows).extra_tags))
        for value in range(3)
    }
    assert tags == {
        "negative_unknown_column",
        "negative_set_arity",
        "negative_function_arity",
    }


def test_query_shape_builders_cover_directed_subquery_group_window_json_case_and_type_paths() -> None:
    generator = QueryGenerator()
    unique_manifest = manifest(unique=True)
    nonunique_manifest = manifest(unique=False)
    rows = {"left_table": 2, "right_table": 3}

    for variant in ("scalar", "row", "exists"):
        assert generator._subquery(
            unique_manifest,
            rows,
            materialized=False,
            rng=random.Random(1),
            directed=variant,
        ).extra_tags == {f"{variant}_subquery"}
    assert "materialized_subquery" in generator._subquery(
        unique_manifest,
        rows,
        materialized=True,
        rng=random.Random(1),
        directed=None,
    ).extra_tags

    for rollup, having in ((False, False), (True, False), (False, True)):
        assert generator._grouping(
            unique_manifest, rows, rollup=rollup, having=having
        ).complexity.projection == 2
    for candidate in (unique_manifest, nonunique_manifest):
        for frame in (False, True):
            assert generator._window(candidate, rows, frame=frame).ast.has_window

    assert "json_table" in generator._json_table(
        unique_manifest, rows, implicit=False, max_elements_per_row=4
    ).extra_tags
    with pytest.raises(TargetNotReachable, match="JSON column"):
        generator._json_table(
            unique_manifest, rows, implicit=True, max_elements_per_row=4
        )
    for feature_id in (
        "json_create_extract",
        "json_member_overlap",
        "json_value_scalar",
        "json_schema_validation",
    ):
        assert generator._json_function(
            unique_manifest, rows, feature_id
        ).complexity.projection == 2
    for searched in (False, True):
        assert generator._case(
            unique_manifest, rows, searched=searched
        ).complexity.projection == 2
    for feature_id in (
        "type_numeric_boundaries",
        "type_string_lob_boundaries",
        "type_temporal_json_spatial",
    ):
        assert generator._type_domain(
            unique_manifest, rows, feature_id
        ).complexity.projection >= 2


def test_shape_precondition_errors_cover_missing_physical_schema_features() -> None:
    generator = QueryGenerator()
    candidate = manifest(unique=True)
    rows = {"left_table": 2, "right_table": 3}
    operations = [
        lambda: generator._partition(candidate, rows),
        lambda: generator._profile_function(candidate, rows),
        lambda: generator._fulltext(candidate, rows),
        lambda: generator._spatial(candidate, rows),
        lambda: generator._multivalue(candidate, rows),
        lambda: generator._index_shape(candidate, rows, "index_prefix"),
        lambda: generator._index_shape(candidate, rows, "index_descending"),
        lambda: generator._index_shape(candidate, rows, "index_functional"),
        lambda: generator._index_merge(candidate, rows),
    ]
    for operation in operations:
        with pytest.raises(TargetNotReachable):
            operation()
