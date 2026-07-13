from __future__ import annotations

from collections.abc import Callable

import pytest

from select_fuzz.generation.schema import (
    ColumnDef,
    ForeignKeyDef,
    IndexDef,
    IndexExpression,
    IndexExpressionKind,
    IndexKind,
    IndexPart,
    PartitionDef,
    SchemaLimits,
    SchemaManifest,
    SchemaProfile,
    SortDirection,
    TableDef,
)


COLUMN = ColumnDef("id", "BIGINT", False)
PART = IndexPart(column_name="id")
PRIMARY = IndexDef("PRIMARY", (PART,), unique=True, primary=True)
TABLE = TableDef("items", False, (COLUMN,), (PRIMARY,))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_tables": 0},
        {"min_tables": True},
        {"min_tables": 2, "max_tables": 1},
        {"min_columns": 3, "max_columns": 2},
        {"max_columns": 1018},
        {"max_indexes_per_table": 66},
        {"index_byte_budget": 7},
        {"row_byte_budget": 65_536},
        {"index_byte_budget": 3073},
        {"page_size": 12345},
        {"row_format": "FIXED"},
        {"row_format": "COMPRESSED", "page_size": 32_768},
        {"max_varchar_characters": 65_536},
        {"max_varbinary_bytes": 65_536},
        {"max_partitions": 8193},
    ],
)
def test_schema_limits_reject_mysql_physical_limit_violations(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SchemaLimits(**kwargs)


@pytest.mark.parametrize(
    ("factory", "error"),
    [
        (lambda: ColumnDef("Bad", "INT", False), ValueError),
        (lambda: ColumnDef("value", "INT(11)", False), ValueError),
        (lambda: ColumnDef("value", "DECIMAL(2,3)", False), ValueError),
        (lambda: ColumnDef("value", "INT", 0), TypeError),
        (lambda: ColumnDef("value", "VARCHAR(10)", False, charset="utf8mb4"), ValueError),
        (
            lambda: ColumnDef(
                "value", "INT", False, charset="utf8mb4", collation="utf8mb4_bin"
            ),
            ValueError,
        ),
        (
            lambda: ColumnDef(
                "value",
                "VARCHAR(10)",
                False,
                charset="utf8mb4",
                collation="latin1_bin",
            ),
            ValueError,
        ),
        (lambda: ColumnDef("shape", "POINT", False, srid=True), ValueError),
        (lambda: ColumnDef("shape", "POINT", False, srid=2**32), ValueError),
        (lambda: ColumnDef("value", "INT", False, srid=0), ValueError),
        (
            lambda: IndexExpression(IndexExpressionKind.LOWER_CHAR, "value", 0),
            ValueError,
        ),
        (
            lambda: IndexExpression(IndexExpressionKind.JSON_UNSIGNED_ARRAY, "value", 1),
            ValueError,
        ),
        (lambda: IndexPart(), ValueError),
        (lambda: IndexPart(column_name="id", expression=IndexExpression.lower_char("id", 1)), ValueError),
        (lambda: IndexPart(expression="lower(id)"), TypeError),
        (lambda: IndexPart(column_name="id", prefix_length=0), ValueError),
        (
            lambda: IndexPart(
                expression=IndexExpression.lower_char("id", 1), prefix_length=1
            ),
            ValueError,
        ),
        (lambda: IndexDef("ix", (PART,), unique=1), TypeError),
        (lambda: IndexDef("ix", ()), ValueError),
        (lambda: IndexDef("ix", (PART,), unique=True, primary=True), ValueError),
        (
            lambda: IndexDef(
                "PRIMARY", (PART,), unique=True, primary=True, kind=IndexKind.FULLTEXT
            ),
            ValueError,
        ),
        (lambda: IndexDef("ix", (PART,), visible=1), TypeError),
        (
            lambda: IndexDef("PRIMARY", (PART,), unique=True, primary=True, visible=False),
            ValueError,
        ),
        (lambda: PartitionDef("LINEAR HASH", ("id",), 2), ValueError),
        (lambda: PartitionDef("HASH", (), 2), ValueError),
        (lambda: PartitionDef("HASH", ("id",), True), ValueError),
        (lambda: PartitionDef("HASH", ("id",), 0), ValueError),
        (lambda: ForeignKeyDef("fk", (), "parent", ()), ValueError),
        (lambda: ForeignKeyDef("fk", ("id",), "parent", ("id", "other")), ValueError),
        (
            lambda: ForeignKeyDef("fk", ("id",), "parent", ("id",), on_delete="DROP"),
            ValueError,
        ),
        (lambda: TableDef("items", 0, (COLUMN,), (PRIMARY,)), TypeError),
        (lambda: TableDef("items", False, (), ()), ValueError),
        (lambda: TableDef("items", False, (COLUMN, COLUMN), (PRIMARY,)), ValueError),
        (lambda: TableDef("items", False, (COLUMN,), (PRIMARY, PRIMARY)), ValueError),
        (
            lambda: TableDef(
                "items",
                False,
                (COLUMN,),
                (PRIMARY,),
                foreign_keys=(
                    ForeignKeyDef("fk", ("id",), "parent", ("id",)),
                    ForeignKeyDef("fk", ("id",), "parent", ("id",)),
                ),
            ),
            ValueError,
        ),
        (lambda: TableDef("items", False, (COLUMN,), (PRIMARY,), engine="MyISAM"), ValueError),
        (
            lambda: TableDef("items", False, (COLUMN,), (PRIMARY,), row_format="FIXED"),
            ValueError,
        ),
        (
            lambda: TableDef(
                "items",
                False,
                (COLUMN,),
                (PRIMARY,),
                default_charset="utf8mb4",
                default_collation="latin1_bin",
            ),
            ValueError,
        ),
        (
            lambda: SchemaManifest("regular_innodb", "feature", 1, (TABLE,)),
            TypeError,
        ),
        (
            lambda: SchemaManifest(SchemaProfile.REGULAR_INNODB, "feature", True, (TABLE,)),
            TypeError,
        ),
        (
            lambda: SchemaManifest(
                SchemaProfile.REGULAR_INNODB, "feature", 1, (TABLE,), requires_same_session=1
            ),
            TypeError,
        ),
        (
            lambda: SchemaManifest(SchemaProfile.REGULAR_INNODB, "feature", 1, ()),
            ValueError,
        ),
        (
            lambda: SchemaManifest(SchemaProfile.REGULAR_INNODB, "feature", 1, (TABLE, TABLE)),
            ValueError,
        ),
    ],
)
def test_schema_value_objects_reject_invalid_states(
    factory: Callable[[], object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        factory()


def test_schema_rendering_and_lookup_cover_all_partition_and_index_variants() -> None:
    text_column = ColumnDef(
        "value",
        "VARCHAR(10)",
        True,
        charset="utf8mb4",
        collation="utf8mb4_0900_ai_ci",
    )
    spatial_column = ColumnDef("shape", "POINT", False, srid=4326)
    assert "CHARACTER SET" in text_column.render()
    assert "SRID 4326" in spatial_column.render()
    assert "DESC" in IndexPart(column_name="value", prefix_length=3, direction=SortDirection.DESC).render()
    assert "CAST(LOWER" in IndexPart(expression=IndexExpression.lower_char("value", 4)).render()
    assert "JSON_EXTRACT" in IndexExpression.json_unsigned_array("value").render()

    assert PartitionDef("HASH", ("id",), 2).render().startswith("PARTITION BY HASH")
    assert PartitionDef("KEY", ("id",), 2).render().startswith("PARTITION BY KEY")
    assert "MAXVALUE" in PartitionDef("RANGE", ("id",), 2).render()
    assert "VALUES IN" in PartitionDef("LIST", ("id",), 2).render()
    assert PartitionDef("LIST COLUMNS", ("id",), 2).bucket_for_identity(3) == 1
    with pytest.raises(ValueError):
        PartitionDef("HASH", ("id",), 2).bucket_for_identity(1)
    with pytest.raises(TypeError):
        PartitionDef("LIST", ("id",), 2).bucket_for_identity(True)

    assert TABLE.column("id") == COLUMN
    with pytest.raises(KeyError):
        TABLE.column("missing")
