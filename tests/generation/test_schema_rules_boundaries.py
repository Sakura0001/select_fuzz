from __future__ import annotations

from collections.abc import Callable

import pytest

from select_fuzz.generation.schema import (
    ColumnDef,
    ForeignKeyDef,
    IndexDef,
    IndexExpression,
    IndexKind,
    IndexPart,
    PartitionDef,
    SchemaLimits,
    SchemaManifest,
    SchemaProfile,
    SortDirection,
    TableDef,
)
from select_fuzz.generation.schema_rules import (
    SchemaRuleViolation,
    SchemaRules,
    _foreign_key_columns_compatible,
)


ID = ColumnDef("id", "BIGINT", False)
TEXT = ColumnDef(
    "text_value", "VARCHAR(10)", True, "utf8mb4", "utf8mb4_0900_ai_ci"
)
PRIMARY = IndexDef(
    "PRIMARY", (IndexPart(column_name="id"),), unique=True, primary=True
)
RULES = SchemaRules.mysql_8041()


def table(
    *,
    name: str = "items",
    temporary: bool = False,
    columns: tuple[ColumnDef, ...] = (ID, TEXT),
    indexes: tuple[IndexDef, ...] = (PRIMARY,),
    partition: PartitionDef | None = None,
    foreign_keys: tuple[ForeignKeyDef, ...] = (),
    row_format: str = "DYNAMIC",
) -> TableDef:
    return TableDef(
        name,
        temporary,
        columns,
        indexes,
        partition,
        foreign_keys,
        row_format=row_format,
    )


def manifest(
    profile: SchemaProfile,
    *tables: TableDef,
    same_session: bool = False,
) -> SchemaManifest:
    return SchemaManifest(profile, "rules_boundary", 1, tables, same_session)


def assert_rule(rule_id: str, operation: Callable[[], object]) -> None:
    with pytest.raises(SchemaRuleViolation) as caught:
        operation()
    assert caught.value.rule_id == rule_id


def test_profile_rules_reject_every_incompatible_scene_boundary() -> None:
    persistent = table()
    temporary = table(temporary=True)
    partitioned = table(partition=PartitionDef("HASH", ("id",), 2))
    foreign_key = ForeignKeyDef("fk_parent", ("id",), "items", ("text_value",))
    with_fk = table(foreign_keys=(foreign_key,))
    fulltext = IndexDef("ft", (IndexPart(column_name="text_value"),), kind=IndexKind.FULLTEXT)
    special = table(indexes=(PRIMARY, fulltext))

    cases = [
        (
            "temporary_same_session",
            manifest(SchemaProfile.TEMPORARY_INNODB, temporary),
        ),
        (
            "temporary_tables_only",
            manifest(SchemaProfile.TEMPORARY_INNODB, persistent, same_session=True),
        ),
        (
            "temporary_no_compressed_row_format",
            manifest(
                SchemaProfile.TEMPORARY_INNODB,
                table(temporary=True, row_format="COMPRESSED"),
                same_session=True,
            ),
        ),
        (
            "temporary_no_partition",
            manifest(
                SchemaProfile.TEMPORARY_INNODB,
                table(temporary=True, partition=PartitionDef("HASH", ("id",), 2)),
                same_session=True,
            ),
        ),
        (
            "temporary_no_foreign_key",
            manifest(
                SchemaProfile.TEMPORARY_INNODB,
                table(temporary=True, foreign_keys=(foreign_key,)),
                same_session=True,
            ),
        ),
        (
            "temporary_no_special_index",
            manifest(
                SchemaProfile.TEMPORARY_INNODB,
                table(temporary=True, indexes=(PRIMARY, fulltext)),
                same_session=True,
            ),
        ),
        (
            "same_session_only_for_temporary",
            manifest(SchemaProfile.REGULAR_INNODB, persistent, same_session=True),
        ),
        (
            "persistent_tables_only",
            manifest(SchemaProfile.REGULAR_INNODB, temporary),
        ),
        (
            "partition_profile_required",
            manifest(SchemaProfile.PARTITIONED_INNODB, persistent),
        ),
        (
            "foreign_key_graph_required",
            manifest(SchemaProfile.FOREIGN_KEY_GRAPH, persistent),
        ),
        (
            "fulltext_profile_required",
            manifest(SchemaProfile.FULLTEXT_INNODB, persistent),
        ),
        (
            "spatial_profile_required",
            manifest(SchemaProfile.SPATIAL_INNODB, persistent),
        ),
        (
            "multivalue_profile_required",
            manifest(SchemaProfile.JSON_MULTIVALUE_INNODB, persistent),
        ),
        (
            "regular_profile_isolation",
            manifest(SchemaProfile.REGULAR_INNODB, partitioned),
        ),
        (
            "regular_profile_isolation",
            manifest(SchemaProfile.REGULAR_INNODB, with_fk),
        ),
        (
            "regular_profile_isolation",
            manifest(SchemaProfile.REGULAR_INNODB, special),
        ),
    ]
    for rule_id, candidate in cases:
        assert_rule(rule_id, lambda candidate=candidate: RULES._validate_profile(candidate))

    assert_rule(
        "special_profile_isolation",
        lambda: RULES._reject_unexpected(
            SchemaProfile.FULLTEXT_INNODB,
            True,
            False,
            {IndexKind.FULLTEXT},
            IndexKind.FULLTEXT,
        ),
    )


def test_partition_and_foreign_key_profiles_reject_cross_scene_structures() -> None:
    foreign_key = ForeignKeyDef("fk_parent", ("id",), "items", ("text_value",))
    partitioned_with_fk = table(
        partition=PartitionDef("HASH", ("id",), 2),
        foreign_keys=(foreign_key,),
    )
    assert_rule(
        "partition_no_foreign_key",
        lambda: RULES._validate_profile(
            manifest(SchemaProfile.PARTITIONED_INNODB, partitioned_with_fk)
        ),
    )

    fulltext = IndexDef(
        "ft", (IndexPart(column_name="text_value"),), kind=IndexKind.FULLTEXT
    )
    assert_rule(
        "partition_no_special_index",
        lambda: RULES._validate_profile(
            manifest(
                SchemaProfile.PARTITIONED_INNODB,
                table(
                    indexes=(PRIMARY, fulltext),
                    partition=PartitionDef("HASH", ("id",), 2),
                ),
            )
        ),
    )

    assert_rule(
        "foreign_key_no_partition",
        lambda: RULES._validate_profile(
            manifest(
                SchemaProfile.FOREIGN_KEY_GRAPH,
                table(name="parent", partition=PartitionDef("HASH", ("id",), 2)),
                table(name="child", foreign_keys=(ForeignKeyDef("fk", ("id",), "parent", ("id",)),)),
            )
        ),
    )


def test_manifest_shape_rules_cover_counts_indexes_and_row_format() -> None:
    limits = SchemaLimits(min_tables=2, max_tables=2)
    assert_rule(
        "schema_table_count",
        lambda: RULES._validate_manifest_shape(
            manifest(SchemaProfile.REGULAR_INNODB, table()), limits
        ),
    )
    assert_rule(
        "table_column_count",
        lambda: RULES._validate_manifest_shape(
            manifest(SchemaProfile.REGULAR_INNODB, table(columns=(ID,))),
            SchemaLimits(min_columns=2),
        ),
    )
    assert_rule(
        "table_index_count",
        lambda: RULES._validate_manifest_shape(
            manifest(
                SchemaProfile.REGULAR_INNODB,
                table(indexes=(PRIMARY, IndexDef("ix", (IndexPart(column_name="id"),)))),
            ),
            SchemaLimits(max_indexes_per_table=1),
        ),
    )
    secondary = tuple(
        IndexDef(f"ix_{number}", (IndexPart(column_name="id"),)) for number in range(65)
    )
    assert_rule(
        "innodb_secondary_index_limit",
        lambda: RULES._validate_manifest_shape(
            manifest(SchemaProfile.REGULAR_INNODB, table(indexes=secondary)),
            SchemaLimits(max_indexes_per_table=65),
        ),
    )
    assert_rule(
        "table_row_format",
        lambda: RULES._validate_manifest_shape(
            manifest(
                SchemaProfile.REGULAR_INNODB,
                table(row_format="COMPACT"),
            ),
            SchemaLimits(),
        ),
    )


def test_partition_rules_reject_invalid_columns_types_counts_and_unique_keys() -> None:
    candidates = [
        (
            "partition_count",
            table(partition=PartitionDef("HASH", ("id",), 3)),
            SchemaLimits(max_partitions=2),
        ),
        (
            "partition_column_exists",
            table(partition=PartitionDef("HASH", ("missing",), 2)),
            SchemaLimits(),
        ),
        (
            "hash_partition_integer",
            table(partition=PartitionDef("HASH", ("text_value",), 2)),
            SchemaLimits(),
        ),
        (
            "range_list_partition_integer",
            table(partition=PartitionDef("RANGE", ("text_value",), 2)),
            SchemaLimits(),
        ),
        (
            "key_partition_column_type",
            table(
                columns=(ID, ColumnDef("document", "JSON", True)),
                partition=PartitionDef("KEY", ("document",), 2),
            ),
            SchemaLimits(),
        ),
        (
            "columns_partition_column_type",
            table(
                columns=(ID, ColumnDef("amount", "DECIMAL(4,2)", False)),
                partition=PartitionDef("RANGE COLUMNS", ("amount",), 2),
            ),
            SchemaLimits(),
        ),
        (
            "partition_unique_key_contains_partition_key",
            table(
                partition=PartitionDef("HASH", ("id",), 2),
                indexes=(
                    IndexDef(
                        "uq_text", (IndexPart(column_name="text_value"),), unique=True
                    ),
                ),
            ),
            SchemaLimits(),
        ),
    ]
    for rule_id, candidate, limits in candidates:
        assert_rule(
            rule_id,
            lambda candidate=candidate, limits=limits: RULES._validate_table(
                candidate, SchemaProfile.PARTITIONED_INNODB, limits
            ),
        )


def test_index_rules_reject_invalid_structure_and_storage_semantics() -> None:
    nullable_id = ColumnDef("id", "BIGINT", True)
    shape = ColumnDef("shape", "POINT", False, srid=4326)
    json_column = ColumnDef("document", "JSON", True)
    blob = ColumnDef("payload", "BLOB", True)
    array_expression = IndexExpression.json_unsigned_array("document")
    cases: list[tuple[str, TableDef, IndexDef, SchemaProfile]] = [
        (
            "profile_index_compatibility",
            table(),
            IndexDef("ft", (IndexPart(column_name="text_value"),), kind=IndexKind.FULLTEXT),
            SchemaProfile.REGULAR_INNODB,
        ),
        (
            "index_column_exists",
            table(),
            IndexDef("ix", (IndexPart(column_name="missing"),)),
            SchemaProfile.REGULAR_INNODB,
        ),
        (
            "index_expression_columns_exist",
            table(),
            IndexDef(
                "ix",
                (IndexPart(expression=IndexExpression.lower_char("missing", 2)),),
                kind=IndexKind.FUNCTIONAL,
            ),
            SchemaProfile.REGULAR_INNODB,
        ),
        (
            "special_index_not_unique",
            table(),
            IndexDef(
                "ft",
                (IndexPart(column_name="text_value"),),
                unique=True,
                kind=IndexKind.FULLTEXT,
            ),
            SchemaProfile.FULLTEXT_INNODB,
        ),
        (
            "primary_whole_columns",
            table(),
            IndexDef(
                "PRIMARY",
                (IndexPart(column_name="text_value", prefix_length=2),),
                unique=True,
                primary=True,
            ),
            SchemaProfile.REGULAR_INNODB,
        ),
        (
            "primary_not_null",
            table(columns=(nullable_id, TEXT)),
            IndexDef(
                "PRIMARY",
                (IndexPart(column_name="id"),),
                unique=True,
                primary=True,
            ),
            SchemaProfile.REGULAR_INNODB,
        ),
        (
            "special_index_no_direction",
            table(),
            IndexDef(
                "ft",
                (IndexPart(column_name="text_value", direction=SortDirection.DESC),),
                kind=IndexKind.FULLTEXT,
            ),
            SchemaProfile.FULLTEXT_INNODB,
        ),
        (
            "fulltext_text_columns",
            table(),
            IndexDef("ft", (IndexPart(column_name="id"),), kind=IndexKind.FULLTEXT),
            SchemaProfile.FULLTEXT_INNODB,
        ),
        (
            "spatial_single_column",
            table(columns=(ID, shape)),
            IndexDef(
                "sp", (IndexPart(column_name="shape"), IndexPart(column_name="id")), kind=IndexKind.SPATIAL
            ),
            SchemaProfile.SPATIAL_INNODB,
        ),
        (
            "spatial_column_type",
            table(),
            IndexDef("sp", (IndexPart(column_name="id"),), kind=IndexKind.SPATIAL),
            SchemaProfile.SPATIAL_INNODB,
        ),
        (
            "spatial_not_null",
            table(columns=(ID, ColumnDef("shape", "POINT", True, srid=4326))),
            IndexDef("sp", (IndexPart(column_name="shape"),), kind=IndexKind.SPATIAL),
            SchemaProfile.SPATIAL_INNODB,
        ),
        (
            "spatial_srid_required",
            table(columns=(ID, ColumnDef("shape", "POINT", False))),
            IndexDef("sp", (IndexPart(column_name="shape"),), kind=IndexKind.SPATIAL),
            SchemaProfile.SPATIAL_INNODB,
        ),
        (
            "multivalue_single_array_part",
            table(columns=(ID, json_column)),
            IndexDef("mv", (IndexPart(column_name="id"),), kind=IndexKind.MULTIVALUE),
            SchemaProfile.JSON_MULTIVALUE_INNODB,
        ),
        (
            "multivalue_json_array",
            table(),
            IndexDef(
                "mv",
                (IndexPart(expression=IndexExpression.json_unsigned_array("text_value")),),
                kind=IndexKind.MULTIVALUE,
            ),
            SchemaProfile.JSON_MULTIVALUE_INNODB,
        ),
        (
            "multivalue_no_prefix",
            table(columns=(ID, json_column, TEXT)),
            IndexDef(
                "mv",
                (
                    IndexPart(expression=array_expression),
                    IndexPart(column_name="text_value", prefix_length=2),
                ),
                kind=IndexKind.MULTIVALUE,
            ),
            SchemaProfile.JSON_MULTIVALUE_INNODB,
        ),
        (
            "functional_typed_expression",
            table(),
            IndexDef("fn", (IndexPart(column_name="id"),), kind=IndexKind.FUNCTIONAL),
            SchemaProfile.REGULAR_INNODB,
        ),
        (
            "btree_column_parts",
            table(),
            IndexDef(
                "ix",
                (IndexPart(expression=IndexExpression.lower_char("text_value", 2)),),
            ),
            SchemaProfile.REGULAR_INNODB,
        ),
        (
            "index_prefix_string_type",
            table(),
            IndexDef("ix", (IndexPart(column_name="id", prefix_length=1),)),
            SchemaProfile.REGULAR_INNODB,
        ),
        (
            "lob_index_requires_prefix",
            table(columns=(ID, blob)),
            IndexDef("ix", (IndexPart(column_name="payload"),)),
            SchemaProfile.REGULAR_INNODB,
        ),
        (
            "index_prefix_within_column",
            table(),
            IndexDef("ix", (IndexPart(column_name="text_value", prefix_length=11),)),
            SchemaProfile.REGULAR_INNODB,
        ),
    ]
    for rule_id, candidate_table, candidate_index, profile in cases:
        columns = {column.name: column for column in candidate_table.columns}
        assert_rule(
            rule_id,
            lambda candidate_table=candidate_table,
            candidate_index=candidate_index,
            columns=columns,
            profile=profile: RULES._validate_index(
                candidate_table,
                candidate_index,
                columns,
                profile,
                SchemaLimits(),
            ),
        )


def test_foreign_key_rules_reject_missing_incompatible_and_set_null_targets() -> None:
    parent = table(name="parent")
    cases = [
        (
            "foreign_key_parent_exists",
            table(
                name="child",
                foreign_keys=(ForeignKeyDef("fk", ("id",), "missing", ("id",)),),
            ),
            {"parent": parent},
        ),
        (
            "foreign_key_columns_exist",
            table(
                name="child",
                foreign_keys=(ForeignKeyDef("fk", ("missing",), "parent", ("id",)),),
            ),
            {"parent": parent},
        ),
        (
            "foreign_key_column_compatible",
            table(
                name="child",
                columns=(ColumnDef("id", "INT", False), TEXT),
                foreign_keys=(ForeignKeyDef("fk", ("id",), "parent", ("id",)),),
            ),
            {"parent": parent},
        ),
        (
            "foreign_key_set_null_nullable",
            table(
                name="child",
                foreign_keys=(
                    ForeignKeyDef(
                        "fk", ("id",), "parent", ("id",), on_delete="SET NULL"
                    ),
                ),
            ),
            {"parent": parent},
        ),
    ]
    for rule_id, child, tables in cases:
        assert_rule(
            rule_id,
            lambda child=child, tables=tables: RULES._validate_foreign_keys(
                child, {**tables, child.name: child}
            ),
        )


def test_rule_helpers_cover_unknown_profiles_binary_keys_and_fallback_compatibility() -> None:
    assert not RULES.allows("unknown_profile", {"partition"})
    assert not RULES.allows_index("unknown_profile", IndexKind.BTREE)

    binary = ColumnDef("value", "VARBINARY(8)", False)
    binary_table = table(columns=(ID, binary))
    assert RULES._index_part_bytes(
        IndexPart(column_name="value"), {"id": ID, "value": binary}, binary_table
    ) == 8
    assert _foreign_key_columns_compatible(binary, binary, binary_table, binary_table)

    left = ColumnDef("day", "DATE", False)
    right = ColumnDef("day", "DATE", False)
    date_table = table(columns=(ID, left))
    assert _foreign_key_columns_compatible(left, right, date_table, date_table)
