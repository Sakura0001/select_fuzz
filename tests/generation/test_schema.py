from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from select_fuzz.generation.catalog import FeatureSpec
from select_fuzz.generation.schema import (
    BoundaryDeclarationId,
    ColumnDef,
    ForeignKeyDef,
    IndexDef,
    IndexExpression,
    IndexExpressionKind,
    IndexKind,
    IndexPart,
    PartitionDef,
    SchemaGenerator,
    SchemaLimits,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)
from select_fuzz.generation.schema_rules import SchemaRuleViolation, SchemaRules


ALL_PROFILES = tuple(SchemaProfile)


def _target(*profiles: SchemaProfile) -> FeatureSpec:
    return FeatureSpec(
        feature_id="schema_target",
        family="schema",
        min_version=(8, 0, 0),
        compatible_profiles=frozenset(profile.value for profile in profiles),
        ast_nodes=frozenset({"query_expression"}),
        guards=frozenset({"read_only_select"}),
    )


def _table(
    *,
    temporary: bool = False,
    columns: tuple[ColumnDef, ...] | None = None,
    indexes: tuple[IndexDef, ...] = (),
    partition: PartitionDef | None = None,
    foreign_keys: tuple[ForeignKeyDef, ...] = (),
) -> TableDef:
    return TableDef(
        name="t0",
        temporary=temporary,
        columns=columns
        or (
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("payload", "VARCHAR(255)", True, "utf8mb4", "utf8mb4_0900_ai_ci"),
        ),
        indexes=indexes,
        partition=partition,
        foreign_keys=foreign_keys,
    )


def _manifest(profile: SchemaProfile, table: TableDef, *, same_session: bool = False) -> SchemaManifest:
    return SchemaManifest(
        profile=profile,
        target_feature_id="schema_target",
        seed=1,
        tables=(table,),
        requires_same_session=same_session,
    )


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_each_profile_is_generated_with_its_required_shape(profile: SchemaProfile) -> None:
    manifest = SchemaGenerator().generate(_target(profile), seed=41, limits=SchemaLimits())

    assert manifest.profile is profile
    SchemaRules.mysql_8041().validate(manifest, limits=SchemaLimits())
    if profile is SchemaProfile.PARTITIONED_INNODB:
        assert any(table.partition is not None for table in manifest.tables)
    elif profile is SchemaProfile.TEMPORARY_INNODB:
        assert manifest.requires_same_session
        assert all(table.temporary for table in manifest.tables)
    elif profile is SchemaProfile.FOREIGN_KEY_GRAPH:
        assert len(manifest.tables) >= 2
        assert any(table.foreign_keys for table in manifest.tables)
    elif profile is SchemaProfile.FULLTEXT_INNODB:
        assert any(index.kind is IndexKind.FULLTEXT for table in manifest.tables for index in table.indexes)
    elif profile is SchemaProfile.SPATIAL_INNODB:
        assert any(index.kind is IndexKind.SPATIAL for table in manifest.tables for index in table.indexes)
    elif profile is SchemaProfile.JSON_MULTIVALUE_INNODB:
        assert any(index.kind is IndexKind.MULTIVALUE for table in manifest.tables for index in table.indexes)


def test_generation_is_byte_stable_and_uses_target_profile_intersection() -> None:
    limits = SchemaLimits(min_tables=3, max_tables=3, min_columns=5, max_columns=5)
    target = _target(SchemaProfile.REGULAR_INNODB, SchemaProfile.PARTITIONED_INNODB)

    first = SchemaGenerator().generate(target, seed=20260712, limits=limits)
    second = SchemaGenerator().generate(target, seed=20260712, limits=limits)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.render_setup_sql().encode() == second.render_setup_sql().encode()
    assert len(first.tables) == 3
    assert all(len(table.columns) == 5 for table in first.tables)
    assert first.profile.value in target.compatible_profiles


def test_default_limits_cover_one_to_eight_tables_and_two_to_sixteen_columns() -> None:
    limits = SchemaLimits()

    assert (limits.min_tables, limits.max_tables) == (1, 8)
    assert (limits.min_columns, limits.max_columns) == (2, 16)
    with pytest.raises(ValueError, match="min_tables"):
        SchemaLimits(min_tables=0)
    with pytest.raises(ValueError, match="max_columns"):
        SchemaLimits(min_columns=17, max_columns=16)
    with pytest.raises(ValueError, match="row_byte_budget"):
        SchemaLimits(row_byte_budget=65_536)
    with pytest.raises(ValueError, match="COMPRESSED"):
        SchemaLimits(row_format="COMPRESSED", page_size=32_768)
    assert SchemaLimits(max_indexes_per_table=65).max_indexes_per_table == 65
    with pytest.raises(ValueError, match="max_indexes_per_table"):
        SchemaLimits(max_indexes_per_table=66)


@pytest.mark.parametrize(
    "mysql_type",
    ("FOO", "VARCHAR(999999)", "DECIMAL(1,30)", "BIT(65)", "TIME(7)"),
)
def test_column_type_is_a_closed_mysql_8041_declaration(mysql_type: str) -> None:
    with pytest.raises(ValueError, match="mysql_type"):
        ColumnDef("c", mysql_type, True)


def test_column_rejects_incompatible_charset_collation_pair() -> None:
    with pytest.raises(ValueError, match="compatible pair"):
        ColumnDef("c", "VARCHAR(10)", True, "utf8mb4", "latin1_swedish_ci")


@pytest.mark.parametrize(
    ("profile", "features", "valid"),
    [
        (SchemaProfile.PARTITIONED_INNODB, {"partitioned", "descending"}, True),
        (SchemaProfile.PARTITIONED_INNODB, {"foreign_key"}, False),
        (SchemaProfile.PARTITIONED_INNODB, {"fulltext"}, False),
        (SchemaProfile.TEMPORARY_INNODB, {"partitioned"}, False),
        (SchemaProfile.FOREIGN_KEY_GRAPH, {"composite_fk"}, True),
        (SchemaProfile.JSON_MULTIVALUE_INNODB, {"unique_multivalue"}, True),
    ],
)
def test_profile_feature_compatibility_matrix(
    profile: SchemaProfile, features: set[str], valid: bool
) -> None:
    assert SchemaRules.mysql_8041().allows(profile, features) is valid


def test_declaration_pool_contains_mysql_8041_legal_boundaries() -> None:
    declarations = set(SchemaGenerator.declaration_pool(SchemaLimits()))

    assert {
        "TINYINT",
        "TINYINT UNSIGNED",
        "BIGINT",
        "BIGINT UNSIGNED",
        "BIT(1)",
        "BIT(64)",
        "DECIMAL(1,0)",
        "DECIMAL(1,1)",
        "DECIMAL(30,30)",
        "DECIMAL(31,30)",
        "DECIMAL(65,0)",
        "DECIMAL(65,30)",
        "FLOAT",
        "FLOAT UNSIGNED",
        "DOUBLE",
        "DOUBLE UNSIGNED",
        "CHAR(0)",
        "CHAR(1)",
        "CHAR(255)",
        "VARCHAR(0)",
        "VARCHAR(1)",
        "VARCHAR(16383)",
        "BINARY(0)",
        "BINARY(1)",
        "BINARY(255)",
        "VARBINARY(0)",
        "VARBINARY(1)",
        "VARBINARY(65535)",
        "DATE",
        "TIME(0)",
        "TIME(6)",
        "DATETIME(0)",
        "DATETIME(6)",
        "TIMESTAMP(0)",
        "TIMESTAMP(6)",
        "YEAR",
        "TINYTEXT",
        "TEXT",
        "MEDIUMTEXT",
        "LONGTEXT",
        "TINYBLOB",
        "BLOB",
        "MEDIUMBLOB",
        "LONGBLOB",
        "JSON",
        "ENUM('a','z')",
        "SET('a','b','c')",
        "GEOMETRY",
        "POINT",
        "GEOMETRYCOLLECTION",
    } <= declarations


def test_non_special_boundaries_have_stable_machine_enumerable_ids() -> None:
    boundaries = SchemaGenerator.boundary_declarations(SchemaLimits())

    assert tuple(boundary.boundary_id for boundary in boundaries) == tuple(
        BoundaryDeclarationId
    )
    assert len({boundary.boundary_id for boundary in boundaries}) == len(boundaries)
    assert len({boundary.declaration for boundary in boundaries}) == len(boundaries)
    assert {
        boundary.declaration for boundary in boundaries
    } <= set(SchemaGenerator.declaration_pool(SchemaLimits()))
    assert all(
        boundary.declaration != "JSON"
        and not boundary.declaration.startswith(
            (
                "GEOMETRY",
                "POINT",
                "LINESTRING",
                "POLYGON",
                "MULTIPOINT",
                "MULTILINESTRING",
                "MULTIPOLYGON",
            )
        )
        for boundary in boundaries
    )
    for boundary in boundaries:
        assert ColumnDef("boundary", boundary.declaration, True).mysql_type == (
            boundary.declaration
        )


def test_decimal_boundary_ids_cover_precision_and_scale_edges() -> None:
    declarations_by_id = {
        boundary.boundary_id: boundary.declaration
        for boundary in SchemaGenerator.boundary_declarations(SchemaLimits())
    }

    assert {
        BoundaryDeclarationId.DECIMAL_P1_S0: "DECIMAL(1,0)",
        BoundaryDeclarationId.DECIMAL_P1_S1: "DECIMAL(1,1)",
        BoundaryDeclarationId.DECIMAL_P30_S30: "DECIMAL(30,30)",
        BoundaryDeclarationId.DECIMAL_P31_S30: "DECIMAL(31,30)",
        BoundaryDeclarationId.DECIMAL_P65_S0: "DECIMAL(65,0)",
        BoundaryDeclarationId.DECIMAL_P65_S30: "DECIMAL(65,30)",
    }.items() <= declarations_by_id.items()


def test_unsigned_float_boundaries_are_valid_but_tagged_deprecated() -> None:
    boundaries_by_id = {
        boundary.boundary_id: boundary
        for boundary in SchemaGenerator.boundary_declarations(SchemaLimits())
    }

    assert boundaries_by_id[BoundaryDeclarationId.FLOAT_UNSIGNED].declaration == (
        "FLOAT UNSIGNED"
    )
    assert boundaries_by_id[BoundaryDeclarationId.DOUBLE_UNSIGNED].declaration == (
        "DOUBLE UNSIGNED"
    )
    assert boundaries_by_id[BoundaryDeclarationId.FLOAT_UNSIGNED].tags == frozenset(
        {"deprecated"}
    )
    assert boundaries_by_id[BoundaryDeclarationId.DOUBLE_UNSIGNED].tags == frozenset(
        {"deprecated"}
    )


def test_boundary_ids_cover_integer_bit_string_lob_temporal_and_enum_edges() -> None:
    declarations_by_id = {
        boundary.boundary_id: boundary.declaration
        for boundary in SchemaGenerator.boundary_declarations(SchemaLimits())
    }

    expected = {
        BoundaryDeclarationId.TINYINT_SIGNED: "TINYINT",
        BoundaryDeclarationId.TINYINT_UNSIGNED: "TINYINT UNSIGNED",
        BoundaryDeclarationId.SMALLINT_SIGNED: "SMALLINT",
        BoundaryDeclarationId.SMALLINT_UNSIGNED: "SMALLINT UNSIGNED",
        BoundaryDeclarationId.MEDIUMINT_SIGNED: "MEDIUMINT",
        BoundaryDeclarationId.MEDIUMINT_UNSIGNED: "MEDIUMINT UNSIGNED",
        BoundaryDeclarationId.INT_SIGNED: "INT",
        BoundaryDeclarationId.INT_UNSIGNED: "INT UNSIGNED",
        BoundaryDeclarationId.BIGINT_SIGNED: "BIGINT",
        BoundaryDeclarationId.BIGINT_UNSIGNED: "BIGINT UNSIGNED",
        BoundaryDeclarationId.BIT_LENGTH_1: "BIT(1)",
        BoundaryDeclarationId.BIT_LENGTH_64: "BIT(64)",
        BoundaryDeclarationId.CHAR_LENGTH_0: "CHAR(0)",
        BoundaryDeclarationId.CHAR_LENGTH_1: "CHAR(1)",
        BoundaryDeclarationId.CHAR_LENGTH_MAX: "CHAR(255)",
        BoundaryDeclarationId.VARCHAR_LENGTH_0: "VARCHAR(0)",
        BoundaryDeclarationId.VARCHAR_LENGTH_1: "VARCHAR(1)",
        BoundaryDeclarationId.VARCHAR_LENGTH_MAX: "VARCHAR(16383)",
        BoundaryDeclarationId.BINARY_LENGTH_0: "BINARY(0)",
        BoundaryDeclarationId.BINARY_LENGTH_1: "BINARY(1)",
        BoundaryDeclarationId.BINARY_LENGTH_MAX: "BINARY(255)",
        BoundaryDeclarationId.VARBINARY_LENGTH_0: "VARBINARY(0)",
        BoundaryDeclarationId.VARBINARY_LENGTH_1: "VARBINARY(1)",
        BoundaryDeclarationId.VARBINARY_LENGTH_MAX: "VARBINARY(65535)",
        BoundaryDeclarationId.DATE: "DATE",
        BoundaryDeclarationId.TIME_FSP_0: "TIME(0)",
        BoundaryDeclarationId.TIME_FSP_6: "TIME(6)",
        BoundaryDeclarationId.DATETIME_FSP_0: "DATETIME(0)",
        BoundaryDeclarationId.DATETIME_FSP_6: "DATETIME(6)",
        BoundaryDeclarationId.TIMESTAMP_FSP_0: "TIMESTAMP(0)",
        BoundaryDeclarationId.TIMESTAMP_FSP_6: "TIMESTAMP(6)",
        BoundaryDeclarationId.YEAR: "YEAR",
        BoundaryDeclarationId.TINYTEXT: "TINYTEXT",
        BoundaryDeclarationId.TEXT: "TEXT",
        BoundaryDeclarationId.MEDIUMTEXT: "MEDIUMTEXT",
        BoundaryDeclarationId.LONGTEXT: "LONGTEXT",
        BoundaryDeclarationId.TINYBLOB: "TINYBLOB",
        BoundaryDeclarationId.BLOB: "BLOB",
        BoundaryDeclarationId.MEDIUMBLOB: "MEDIUMBLOB",
        BoundaryDeclarationId.LONGBLOB: "LONGBLOB",
        BoundaryDeclarationId.ENUM: "ENUM('a','z')",
        BoundaryDeclarationId.SET: "SET('a','b','c')",
    }

    assert expected.items() <= declarations_by_id.items()


def test_contextual_max_boundary_ids_follow_schema_limits() -> None:
    boundaries_by_id = {
        boundary.boundary_id: boundary.declaration
        for boundary in SchemaGenerator.boundary_declarations(
            SchemaLimits(max_varchar_characters=128, max_varbinary_bytes=256)
        )
    }

    assert boundaries_by_id[BoundaryDeclarationId.VARCHAR_LENGTH_MAX] == "VARCHAR(128)"
    assert boundaries_by_id[BoundaryDeclarationId.VARBINARY_LENGTH_MAX] == (
        "VARBINARY(256)"
    )


def test_every_boundary_declaration_is_reachable_through_directed_lane() -> None:
    generator = SchemaGenerator()
    limits = SchemaLimits(
        min_tables=1,
        max_tables=1,
        min_columns=2,
        max_columns=3,
    )
    pool = generator.executable_boundary_pool(limits)
    target = _target(SchemaProfile.REGULAR_INNODB)
    reached: set[str] = set()

    for ordinal, declaration in enumerate(pool):
        manifest = generator.generate(
            target,
            seed=8,
            limits=limits,
            boundary_ordinal=ordinal,
        )
        reached.add(manifest.tables[0].column("boundary_col").mysql_type)
        SchemaRules.mysql_8041().validate(manifest, limits=limits)
        assert declaration.encode() in manifest.canonical_bytes()

    assert reached == set(pool)
    assert "VARBINARY(65535)" not in reached
    assert "VARCHAR(16383)" not in reached


def test_every_typed_boundary_is_reachable_without_json_or_spatial_types() -> None:
    generator = SchemaGenerator()
    limits = SchemaLimits(
        min_tables=1,
        max_tables=1,
        min_columns=4,
        max_columns=4,
    )
    target = _target(SchemaProfile.REGULAR_INNODB)
    boundaries = generator.executable_boundary_declarations(limits)
    reached: dict[BoundaryDeclarationId, str] = {}

    for boundary in boundaries:
        manifest = generator.generate(
            target,
            seed=8,
            limits=limits,
            typed_boundary_id=boundary.boundary_id,
        )
        declaration = manifest.tables[0].column("boundary_col").mysql_type
        reached[boundary.boundary_id] = declaration
        SchemaRules.mysql_8041().validate(manifest, limits=limits)
        assert len(manifest.tables[0].columns) == 4

    assert reached == {
        boundary.boundary_id: boundary.declaration for boundary in boundaries
    }
    assert all(
        declaration != "JSON"
        and not declaration.startswith(
            (
                "GEOMETRY",
                "POINT",
                "LINESTRING",
                "POLYGON",
                "MULTIPOINT",
                "MULTILINESTRING",
                "MULTIPOLYGON",
            )
        )
        for declaration in reached.values()
    )


def test_schema_generator_rejects_conflicting_boundary_lanes() -> None:
    with pytest.raises(ValueError, match="only one boundary lane"):
        SchemaGenerator().generate(
            _target(SchemaProfile.REGULAR_INNODB),
            seed=8,
            limits=SchemaLimits(min_columns=3, max_columns=3),
            boundary_ordinal=0,
            typed_boundary_id=BoundaryDeclarationId.TINYINT_SIGNED,
        )
    with pytest.raises(TypeError, match="typed_boundary_id"):
        SchemaGenerator().generate(
            _target(SchemaProfile.REGULAR_INNODB),
            seed=8,
            limits=SchemaLimits(min_columns=3, max_columns=3),
            typed_boundary_id="tinyint_signed",  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="boundary_id"):
        SchemaGenerator.typed_boundary_column(
            name="boundary_col",
            boundary_id="tinyint_signed",  # type: ignore[arg-type]
            limits=SchemaLimits(),
        )


def test_random_manifest_lane_reaches_wide_contextual_string_lengths() -> None:
    limits = SchemaLimits(
        min_tables=1,
        max_tables=1,
        min_columns=3,
        max_columns=3,
    )
    declarations = {
        column.mysql_type
        for seed in range(600)
        for column in SchemaGenerator()
        .generate(
            _target(SchemaProfile.REGULAR_INNODB),
            seed=seed,
            limits=limits,
        )
        .tables[0]
        .columns
    }

    assert any(
        declaration.startswith("VARCHAR(")
        and int(declaration.removeprefix("VARCHAR(").removesuffix(")")) > 512
        for declaration in declarations
    )
    assert any(
        declaration.startswith("VARBINARY(")
        and int(declaration.removeprefix("VARBINARY(").removesuffix(")")) > 2048
        for declaration in declarations
    )


def test_limits_bound_large_declarations_and_index_budget() -> None:
    limits = SchemaLimits(max_varchar_characters=128, max_varbinary_bytes=256, index_byte_budget=100)
    declarations = set(SchemaGenerator.declaration_pool(limits))

    assert "VARCHAR(128)" in declarations and "VARCHAR(16383)" not in declarations
    assert "VARBINARY(256)" in declarations and "VARBINARY(65535)" not in declarations
    table = _table(
        indexes=(
            IndexDef("ix_payload", (IndexPart(column_name="payload", prefix_length=26),)),
        )
    )
    with pytest.raises(SchemaRuleViolation) as caught:
        SchemaRules.mysql_8041().validate(
            _manifest(SchemaProfile.REGULAR_INNODB, table), limits=limits
        )
    assert caught.value.rule_id == "index_key_byte_budget"


@pytest.mark.parametrize(
    "profile",
    (
        SchemaProfile.REGULAR_INNODB,
        SchemaProfile.FOREIGN_KEY_GRAPH,
        SchemaProfile.JSON_MULTIVALUE_INNODB,
    ),
)
def test_generator_filters_index_shapes_that_do_not_fit_tiny_budget(
    profile: SchemaProfile,
) -> None:
    limits = SchemaLimits(
        min_tables=2,
        max_tables=2,
        min_columns=3,
        max_columns=3,
        index_byte_budget=8,
    )

    for seed in range(100):
        manifest = SchemaGenerator().generate(_target(profile), seed=seed, limits=limits)
        SchemaRules.mysql_8041().validate(manifest, limits=limits)


def test_page_size_and_row_format_reduce_the_physical_index_budget() -> None:
    table = _table(
        indexes=(
            IndexDef("ix_payload", (IndexPart(column_name="payload", prefix_length=193),)),
        )
    )

    with pytest.raises(SchemaRuleViolation) as small_page:
        SchemaRules.mysql_8041().validate(
            _manifest(SchemaProfile.REGULAR_INNODB, table),
            limits=SchemaLimits(page_size=4096),
        )
    assert small_page.value.rule_id == "index_key_byte_budget"
    with pytest.raises(SchemaRuleViolation) as compact:
        SchemaRules.mysql_8041().validate(
            replace(
                _manifest(SchemaProfile.REGULAR_INNODB, table),
                tables=(replace(table, row_format="COMPACT"),),
            ),
            limits=SchemaLimits(row_format="COMPACT"),
        )
    assert compact.value.rule_id == "index_key_byte_budget"


def test_page_size_and_row_format_bound_minimum_local_row_size() -> None:
    wide_columns = (ColumnDef("id", "BIGINT UNSIGNED", False),) + tuple(
        ColumnDef(
            f"c{index}",
            "CHAR(255)",
            False,
            "latin1",
            "latin1_swedish_ci",
        )
        for index in range(1, 9)
    )
    table = _table(columns=wide_columns)

    with pytest.raises(SchemaRuleViolation) as caught:
        SchemaRules.mysql_8041().validate(
            _manifest(SchemaProfile.REGULAR_INNODB, table),
            limits=SchemaLimits(page_size=4096),
        )
    assert caught.value.rule_id == "innodb_local_row_size"


def test_profile_index_compatibility_matrix_is_explicit() -> None:
    rules = SchemaRules.mysql_8041()

    assert rules.allows_index(SchemaProfile.REGULAR_INNODB, IndexKind.FUNCTIONAL)
    assert rules.allows_index(SchemaProfile.PARTITIONED_INNODB, IndexKind.BTREE)
    assert rules.allows_index(SchemaProfile.TEMPORARY_INNODB, IndexKind.BTREE)
    assert rules.allows_index(SchemaProfile.FOREIGN_KEY_GRAPH, IndexKind.BTREE)
    assert rules.allows_index(SchemaProfile.FULLTEXT_INNODB, IndexKind.FULLTEXT)
    assert rules.allows_index(SchemaProfile.SPATIAL_INNODB, IndexKind.SPATIAL)
    assert rules.allows_index(SchemaProfile.JSON_MULTIVALUE_INNODB, IndexKind.MULTIVALUE)
    assert not rules.allows_index(SchemaProfile.TEMPORARY_INNODB, IndexKind.SPATIAL)
    assert not rules.allows_index(SchemaProfile.PARTITIONED_INNODB, IndexKind.FULLTEXT)
    assert not rules.allows_index(SchemaProfile.JSON_MULTIVALUE_INNODB, IndexKind.FULLTEXT)


def test_partition_unique_keys_always_include_partition_columns() -> None:
    manifest = SchemaGenerator().generate(
        _target(SchemaProfile.PARTITIONED_INNODB), seed=77, limits=SchemaLimits()
    )

    for table in manifest.tables:
        if table.partition is None:
            continue
        partition_columns = set(table.partition.columns)
        for index in table.indexes:
            if index.unique:
                assert partition_columns <= set(index.column_names)


def test_every_mysql_partition_family_is_reachable() -> None:
    methods = {
        table.partition.method
        for seed in range(100)
        for table in SchemaGenerator()
        .generate(
            _target(SchemaProfile.PARTITIONED_INNODB),
            seed=seed,
            limits=SchemaLimits(min_tables=1, max_tables=1),
        )
        .tables
        if table.partition is not None
    }

    assert methods == {"HASH", "KEY", "RANGE", "LIST", "RANGE COLUMNS", "LIST COLUMNS"}


def test_list_partition_routing_supports_more_rows_than_partition_count() -> None:
    regular_list = PartitionDef("LIST", ("id",), 4)

    assert "LIST (MOD(`id`, 4))" in regular_list.render()
    assert {regular_list.bucket_for_identity(identity) for identity in range(100)} == {
        0,
        1,
        2,
        3,
    }

    limits = SchemaLimits(min_tables=1, max_tables=1, min_columns=3, max_columns=3)
    list_columns_manifest = next(
        manifest
        for seed in range(100)
        if (
            manifest := SchemaGenerator().generate(
                _target(SchemaProfile.PARTITIONED_INNODB),
                seed=seed,
                limits=limits,
            )
        ).tables[0].partition is not None
        and manifest.tables[0].partition.method == "LIST COLUMNS"
    )
    table = list_columns_manifest.tables[0]
    partition = table.partition
    assert partition is not None
    assert partition.columns == ("partition_bucket",)
    assert table.column("partition_bucket").mysql_type == "TINYINT UNSIGNED"
    assert all(
        "partition_bucket" in index.column_names
        for index in table.indexes
        if index.unique
    )
    assert all(
        0 <= partition.bucket_for_identity(identity) < partition.partitions
        for identity in range(100)
    )


def test_regular_primary_key_and_secondary_index_matrix_is_reachable() -> None:
    tables = tuple(
        table
        for seed in range(400)
        for table in SchemaGenerator()
        .generate(
            _target(SchemaProfile.REGULAR_INNODB),
            seed=seed,
            limits=SchemaLimits(min_tables=1, max_tables=1),
        )
        .tables
    )
    index_names = {index.name for table in tables for index in table.indexes}

    assert any(not any(index.primary for index in table.indexes) for table in tables)
    assert any(
        any(index.primary and len(index.parts) == 2 for index in table.indexes)
        for table in tables
    )
    assert any(not table.indexes for table in tables)
    assert any(
        index.unique
        and not index.primary
        and any(
            part.column_name is not None and table.column(part.column_name).nullable
            for part in index.parts
        )
        for table in tables
        for index in table.indexes
    )
    assert any(not index.visible for table in tables for index in table.indexes)
    assert "ix_random_col" in index_names
    assert {
        "ix_payload",
        "ix_composite",
        "ix_id_desc",
        "uq_id_payload",
        "ix_payload_prefix",
        "ix_payload_lower",
    } <= index_names


def test_partition_rejects_spatial_columns_and_multi_column_hash_tuple() -> None:
    spatial_table = _table(
        columns=(
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("location", "POINT", False, srid=4326),
        ),
        indexes=(
            IndexDef("PRIMARY", (IndexPart(column_name="id"),), unique=True, primary=True),
        ),
        partition=PartitionDef("HASH", ("id",), 2),
    )
    with pytest.raises(SchemaRuleViolation) as spatial:
        SchemaRules.mysql_8041().validate(
            _manifest(SchemaProfile.PARTITIONED_INNODB, spatial_table),
            limits=SchemaLimits(),
        )
    assert spatial.value.rule_id == "partition_no_spatial_columns"

    hash_table = replace(
        _table(
            columns=(
                ColumnDef("id", "BIGINT UNSIGNED", False),
                ColumnDef("tenant", "BIGINT UNSIGNED", False),
            ),
            indexes=(
                IndexDef(
                    "PRIMARY",
                    (IndexPart(column_name="id"), IndexPart(column_name="tenant")),
                    unique=True,
                    primary=True,
                ),
            ),
        ),
        partition=PartitionDef("HASH", ("id", "tenant"), 2),
    )
    with pytest.raises(SchemaRuleViolation) as hash_tuple:
        SchemaRules.mysql_8041().validate(
            _manifest(SchemaProfile.PARTITIONED_INNODB, hash_table),
            limits=SchemaLimits(),
        )
    assert hash_tuple.value.rule_id == "hash_partition_single_column"


def test_foreign_key_metadata_and_referenced_unique_left_prefix_are_exact() -> None:
    manifest = SchemaGenerator().generate(
        _target(SchemaProfile.FOREIGN_KEY_GRAPH), seed=99, limits=SchemaLimits()
    )
    tables = {table.name: table for table in manifest.tables}

    for child in manifest.tables:
        for foreign_key in child.foreign_keys:
            parent = tables[foreign_key.referenced_table]
            child_columns = [child.column(name) for name in foreign_key.columns]
            parent_columns = [parent.column(name) for name in foreign_key.referenced_columns]
            assert [column.compatibility_key for column in child_columns] == [
                column.compatibility_key for column in parent_columns
            ]
            assert any(
                index.column_names[: len(foreign_key.referenced_columns)]
                == foreign_key.referenced_columns
                for index in parent.indexes
            )
            assert any(
                index.column_names[: len(foreign_key.columns)] == foreign_key.columns
                and all(part.prefix_length is None for part in index.parts[: len(foreign_key.columns)])
                for index in child.indexes
            )


def test_default_foreign_key_lane_reaches_composite_edges() -> None:
    manifest = SchemaGenerator().generate(
        _target(SchemaProfile.FOREIGN_KEY_GRAPH),
        seed=121,
        limits=SchemaLimits(min_tables=2, max_tables=2, min_columns=3, max_columns=3),
    )
    foreign_key = manifest.tables[1].foreign_keys[0]

    assert foreign_key.columns == ("parent_id", "parent_tenant_id")
    assert foreign_key.referenced_columns == ("id", "tenant_id")

    referenced_indexes = tuple(
        index
        for seed in range(20)
        for index in SchemaGenerator()
        .generate(
            _target(SchemaProfile.FOREIGN_KEY_GRAPH),
            seed=seed,
            limits=SchemaLimits(
                min_tables=2,
                max_tables=2,
                min_columns=3,
                max_columns=3,
            ),
        )
        .tables[0]
        .indexes
        if index.name == "ix_parent_ref_target"
    )
    assert {index.unique for index in referenced_indexes} == {False, True}


def test_nonunique_parent_index_and_different_string_lengths_are_valid_fk_extension() -> None:
    parent = TableDef(
        name="parent",
        temporary=False,
        columns=(
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("code", "VARCHAR(20)", False, "utf8mb4", "utf8mb4_0900_ai_ci"),
        ),
        indexes=(IndexDef("ix_code", (IndexPart(column_name="code"),)),),
    )
    child = TableDef(
        name="child",
        temporary=False,
        columns=(
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("code", "VARCHAR(40)", False, "utf8mb4", "utf8mb4_0900_ai_ci"),
        ),
        indexes=(IndexDef("ix_code", (IndexPart(column_name="code"),)),),
        foreign_keys=(ForeignKeyDef("fk_code", ("code",), "parent", ("code",)),),
    )
    manifest = SchemaManifest(
        profile=SchemaProfile.FOREIGN_KEY_GRAPH,
        target_feature_id="schema_target",
        seed=1,
        tables=(parent, child),
    )

    SchemaRules.mysql_8041().validate(manifest, limits=SchemaLimits())

    implicit_child_index = replace(child, indexes=())
    implicit_manifest = replace(manifest, tables=(parent, implicit_child_index))
    SchemaRules.mysql_8041().validate(implicit_manifest, limits=SchemaLimits())


def test_two_column_limit_uses_a_valid_single_column_foreign_key() -> None:
    limits = SchemaLimits(
        min_tables=2,
        max_tables=2,
        min_columns=2,
        max_columns=2,
    )
    manifest = SchemaGenerator().generate(
        _target(SchemaProfile.FOREIGN_KEY_GRAPH), seed=4, limits=limits
    )

    assert manifest.tables[1].column("parent_id").mysql_type == "BIGINT UNSIGNED"
    assert manifest.tables[1].foreign_keys[0].columns == ("parent_id",)
    SchemaRules.mysql_8041().validate(manifest, limits=limits)


def test_foreign_key_graph_reaches_one_to_one_one_to_many_nullable_and_junction() -> None:
    limits = SchemaLimits(
        min_tables=3,
        max_tables=3,
        min_columns=4,
        max_columns=4,
    )
    manifest = SchemaGenerator().generate(
        _target(SchemaProfile.FOREIGN_KEY_GRAPH), seed=14, limits=limits
    )
    one_to_one, junction = manifest.tables[1], manifest.tables[2]

    assert one_to_one.foreign_keys
    assert any(index.unique and index.name == "ix_parent_ref" for index in one_to_one.indexes)
    assert len(junction.foreign_keys) == 2
    assert junction.column("parent_id").nullable
    assert junction.column("other_parent_id").nullable
    assert {edge.referenced_table for edge in junction.foreign_keys} == {"t0", "t1"}
    SchemaRules.mysql_8041().validate(manifest, limits=limits)


def test_foreign_key_left_prefix_does_not_compress_away_expression_parts() -> None:
    identifier = ColumnDef("id", "BIGINT UNSIGNED", False)
    payload = ColumnDef(
        "payload", "VARCHAR(20)", False, "utf8mb4", "utf8mb4_0900_ai_ci"
    )
    misleading_unique = IndexDef(
        "uq_misleading",
        (
            IndexPart(expression=IndexExpression.lower_char("payload", 20)),
            IndexPart(column_name="id"),
        ),
        unique=True,
        kind=IndexKind.FUNCTIONAL,
    )
    parent = TableDef(
        name="parent",
        temporary=False,
        columns=(identifier, payload),
        indexes=(misleading_unique,),
    )
    child = TableDef(
        name="child",
        temporary=False,
        columns=(ColumnDef("id", "BIGINT UNSIGNED", False), ColumnDef("parent_id", "BIGINT UNSIGNED", False)),
        indexes=(IndexDef("ix_parent", (IndexPart(column_name="parent_id"),)),),
        foreign_keys=(ForeignKeyDef("fk_parent", ("parent_id",), "parent", ("id",)),),
    )
    manifest = SchemaManifest(
        profile=SchemaProfile.FOREIGN_KEY_GRAPH,
        target_feature_id="schema_target",
        seed=1,
        tables=(parent, child),
    )

    with pytest.raises(SchemaRuleViolation) as caught:
        SchemaRules.mysql_8041().validate(manifest, limits=SchemaLimits())
    assert caught.value.rule_id == "foreign_key_reference_index_left_prefix"


def test_foreign_key_constraint_names_are_global_and_parent_must_render_first() -> None:
    limits = SchemaLimits(min_tables=3, max_tables=3)
    manifest = SchemaGenerator().generate(
        _target(SchemaProfile.FOREIGN_KEY_GRAPH), seed=5, limits=limits
    )
    first_child, second_child = manifest.tables[1], manifest.tables[2]
    duplicate_name = replace(
        second_child.foreign_keys[0],
        name=first_child.foreign_keys[0].name,
    )
    duplicated = replace(
        manifest,
        tables=(
            manifest.tables[0],
            first_child,
            replace(second_child, foreign_keys=(duplicate_name,)),
        ),
    )

    with pytest.raises(SchemaRuleViolation) as duplicate:
        SchemaRules.mysql_8041().validate(duplicated, limits=limits)
    assert duplicate.value.rule_id == "foreign_key_name_global_unique"

    two_table_limits = replace(limits, min_tables=2, max_tables=2)
    ordered = SchemaGenerator().generate(
        _target(SchemaProfile.FOREIGN_KEY_GRAPH), seed=5, limits=two_table_limits
    )
    reversed_manifest = replace(ordered, tables=tuple(reversed(ordered.tables)))
    with pytest.raises(SchemaRuleViolation) as order:
        SchemaRules.mysql_8041().validate(reversed_manifest, limits=two_table_limits)
    assert order.value.rule_id == "foreign_key_parent_precedes_child"


def test_foreign_key_column_cannot_reference_itself() -> None:
    identifier = ColumnDef("id", "BIGINT UNSIGNED", False)
    primary = IndexDef(
        "PRIMARY", (IndexPart(column_name="id"),), unique=True, primary=True
    )
    self_referencing = TableDef(
        name="self_ref",
        temporary=False,
        columns=(identifier, ColumnDef("payload", "INT", True)),
        indexes=(primary,),
        foreign_keys=(ForeignKeyDef("fk_self", ("id",), "self_ref", ("id",)),),
    )
    dummy = TableDef(
        name="dummy",
        temporary=False,
        columns=(identifier, ColumnDef("payload", "INT", True)),
        indexes=(primary,),
    )
    manifest = SchemaManifest(
        profile=SchemaProfile.FOREIGN_KEY_GRAPH,
        target_feature_id="schema_target",
        seed=1,
        tables=(self_referencing, dummy),
    )

    with pytest.raises(SchemaRuleViolation) as caught:
        SchemaRules.mysql_8041().validate(manifest, limits=SchemaLimits())
    assert caught.value.rule_id == "foreign_key_column_not_self_reference"


def test_special_index_contracts_are_rendered_for_mysql_8041() -> None:
    generator = SchemaGenerator()
    spatial = generator.generate(
        _target(SchemaProfile.SPATIAL_INNODB), seed=8, limits=SchemaLimits()
    )
    multivalue_manifests = tuple(
        generator.generate(
            _target(SchemaProfile.JSON_MULTIVALUE_INNODB), seed=seed, limits=SchemaLimits()
        )
        for seed in range(12)
    )
    fulltext = generator.generate(
        _target(SchemaProfile.FULLTEXT_INNODB), seed=10, limits=SchemaLimits()
    )

    spatial_column = next(
        table.column(index.parts[0].column_name or "")
        for table in spatial.tables
        for index in table.indexes
        if index.kind is IndexKind.SPATIAL
    )
    assert not spatial_column.nullable and spatial_column.srid is not None
    multivalue_indexes = tuple(
        index
        for manifest in multivalue_manifests
        for table in manifest.tables
        for index in table.indexes
        if index.kind is IndexKind.MULTIVALUE
    )
    assert any(index.unique for index in multivalue_indexes)
    assert all(
        len(index.parts) == 2
        and index.parts[0].column_name == "id"
        and index.parts[1].expression is not None
        and index.parts[1].expression.kind is IndexExpressionKind.JSON_UNSIGNED_ARRAY
        for index in multivalue_indexes
    )
    assert "UNIQUE KEY" in next(
        manifest.render_setup_sql()
        for manifest in multivalue_manifests
        if any(
            index.unique
            for table in manifest.tables
            for index in table.indexes
            if index.kind is IndexKind.MULTIVALUE
        )
    )
    assert "FULLTEXT KEY" in fulltext.render_setup_sql()


def test_functional_expression_fixes_result_charset_and_collation() -> None:
    expression = IndexExpression.lower_char("payload", 20).render()

    assert "CHAR(20) CHARACTER SET utf8mb4" in expression
    assert expression.endswith("COLLATE utf8mb4_0900_ai_ci")

    table = _table(
        columns=(
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("payload", "VARCHAR(1000)", False, "latin1", "latin1_swedish_ci"),
        ),
        indexes=(
            IndexDef(
                "ix_lower",
                (IndexPart(expression=IndexExpression.lower_char("payload", 1000)),),
                kind=IndexKind.FUNCTIONAL,
            ),
        ),
    )
    with pytest.raises(SchemaRuleViolation) as caught:
        SchemaRules.mysql_8041().validate(
            _manifest(SchemaProfile.REGULAR_INNODB, table), limits=SchemaLimits()
        )
    assert caught.value.rule_id == "index_key_byte_budget"


def test_invisible_secondary_index_is_reachable_but_primary_cannot_be_invisible() -> None:
    manifests = (
        SchemaGenerator().generate(
            _target(SchemaProfile.REGULAR_INNODB), seed=seed, limits=SchemaLimits()
        )
        for seed in range(30)
    )
    sql = "\n".join(manifest.render_setup_sql() for manifest in manifests)

    assert "INVISIBLE" in sql
    with pytest.raises(ValueError, match="primary index cannot be invisible"):
        IndexDef(
            "PRIMARY",
            (IndexPart(column_name="id"),),
            unique=True,
            primary=True,
            visible=False,
        )


def test_fulltext_columns_require_one_charset_and_collation() -> None:
    table = _table(
        columns=(
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("body", "TEXT", False, "utf8mb4", "utf8mb4_0900_ai_ci"),
            ColumnDef("title", "VARCHAR(20)", False, "latin1", "latin1_swedish_ci"),
        ),
        indexes=(
            IndexDef(
                "ft_text",
                (IndexPart(column_name="body"), IndexPart(column_name="title")),
                kind=IndexKind.FULLTEXT,
            ),
        ),
    )

    with pytest.raises(SchemaRuleViolation) as caught:
        SchemaRules.mysql_8041().validate(
            _manifest(SchemaProfile.FULLTEXT_INNODB, table), limits=SchemaLimits()
        )
    assert caught.value.rule_id == "fulltext_column_collation"


def test_inherited_utf8mb4_charset_is_used_for_index_byte_budget() -> None:
    table = _table(
        columns=(
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("payload", "VARCHAR(1000)", False),
        ),
        indexes=(IndexDef("ix_payload", (IndexPart(column_name="payload"),)),),
    )

    with pytest.raises(SchemaRuleViolation) as caught:
        SchemaRules.mysql_8041().validate(
            _manifest(SchemaProfile.REGULAR_INNODB, table), limits=SchemaLimits()
        )
    assert caught.value.rule_id == "index_key_byte_budget"


def test_json_column_requires_typed_expression_index() -> None:
    table = _table(
        columns=(
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("payload", "JSON", False),
        ),
        indexes=(IndexDef("ix_payload", (IndexPart(column_name="payload"),)),),
    )

    with pytest.raises(SchemaRuleViolation) as caught:
        SchemaRules.mysql_8041().validate(
            _manifest(SchemaProfile.REGULAR_INNODB, table), limits=SchemaLimits()
        )
    assert caught.value.rule_id == "json_requires_expression_index"


@pytest.mark.parametrize(
    "payload",
    ["1); DROP TABLE t0; --", "LOWER(`payload`)); SELECT SLEEP(1)", "x /* injected */"],
)
def test_index_expression_rejects_raw_or_injected_sql(payload: str) -> None:
    with pytest.raises(TypeError, match="IndexExpression"):
        IndexPart(expression=payload)  # type: ignore[arg-type]


def test_temporary_manifest_is_same_session_and_has_no_forbidden_features() -> None:
    manifest = SchemaGenerator().generate(
        _target(SchemaProfile.TEMPORARY_INNODB), seed=15, limits=SchemaLimits()
    )

    assert manifest.requires_same_session
    assert "CREATE TEMPORARY TABLE" in manifest.render_setup_sql()
    for table in manifest.tables:
        assert table.temporary
        assert table.partition is None and not table.foreign_keys
        assert all(
            index.kind not in {IndexKind.FULLTEXT, IndexKind.SPATIAL, IndexKind.MULTIVALUE}
            for index in table.indexes
        )


def test_generator_rejects_compressed_temporary_configuration_before_building() -> None:
    with pytest.raises(ValueError, match="temporary_innodb does not support COMPRESSED"):
        SchemaGenerator().generate(
            _target(SchemaProfile.TEMPORARY_INNODB),
            seed=1,
            limits=SchemaLimits(row_format="COMPRESSED"),
        )


def test_fixed_seed_sql_snapshot_is_exact() -> None:
    manifest = SchemaGenerator().generate(
        _target(SchemaProfile.SPATIAL_INNODB),
        seed=3,
        limits=SchemaLimits(
            min_tables=1,
            max_tables=1,
            min_columns=2,
            max_columns=2,
            max_indexes_per_table=2,
        ),
    )

    assert manifest.render_setup_sql() == (
        "CREATE TABLE `t0` (\n"
        "  `id` BIGINT UNSIGNED NOT NULL,\n"
        "  `location` POINT SRID 4326 NOT NULL,\n"
        "  PRIMARY KEY (`id`),\n"
        "  SPATIAL KEY `sx_location` (`location`)\n"
        ") ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 "
        "COLLATE=utf8mb4_0900_ai_ci;\n"
    )


def test_invalid_yaml_fixtures_report_stable_rule_ids() -> None:
    fixture_path = Path(__file__).parents[1] / "fixtures" / "schema_rules.yaml"
    cases = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))["invalid_cases"]
    rules = SchemaRules.mysql_8041()

    for case in cases:
        manifest, limits = _invalid_fixture(case["mutation"])
        with pytest.raises(SchemaRuleViolation) as caught:
            rules.validate(manifest, limits=limits)
        assert caught.value.rule_id == case["rule_id"], case["id"]


def _invalid_fixture(mutation: str) -> tuple[SchemaManifest, SchemaLimits]:
    id_column = ColumnDef("id", "BIGINT UNSIGNED", False)
    payload = ColumnDef("payload", "VARCHAR(255)", True, "utf8mb4", "utf8mb4_0900_ai_ci")
    primary = IndexDef("PRIMARY", (IndexPart(column_name="id"),), unique=True, primary=True)
    base = _table(columns=(id_column, payload), indexes=(primary,))
    limits = SchemaLimits()
    if mutation == "temporary_partition":
        table = replace(
            base,
            temporary=True,
            partition=PartitionDef("HASH", ("id",), 4),
        )
        return _manifest(SchemaProfile.TEMPORARY_INNODB, table, same_session=True), limits
    if mutation == "partition_unique_missing_key":
        table = replace(
            base,
            partition=PartitionDef("KEY", ("payload",), 4),
        )
        return _manifest(SchemaProfile.PARTITIONED_INNODB, table), limits
    if mutation == "lob_index_without_prefix":
        table = _table(
            columns=(id_column, ColumnDef("payload", "TEXT", True, "utf8mb4", "utf8mb4_0900_ai_ci")),
            indexes=(IndexDef("ix_payload", (IndexPart(column_name="payload"),)),),
        )
        return _manifest(SchemaProfile.REGULAR_INNODB, table), limits
    if mutation == "index_budget":
        table = replace(
            base,
            indexes=(IndexDef("ix_payload", (IndexPart(column_name="payload", prefix_length=26),)),),
        )
        return _manifest(SchemaProfile.REGULAR_INNODB, table), replace(limits, index_byte_budget=100)
    if mutation in {"spatial_nullable", "spatial_missing_srid"}:
        column = ColumnDef(
            "location",
            "POINT",
            mutation == "spatial_nullable",
            srid=None if mutation == "spatial_missing_srid" else 4326,
        )
        table = _table(
            columns=(id_column, column),
            indexes=(IndexDef("sx_location", (IndexPart(column_name="location"),), kind=IndexKind.SPATIAL),),
        )
        return _manifest(SchemaProfile.SPATIAL_INNODB, table), limits
    if mutation == "unique_multivalue":
        table = _table(
            columns=(id_column, ColumnDef("tags", "JSON", False)),
            indexes=(
                IndexDef(
                    "mx_tags",
                    (
                        IndexPart(expression=IndexExpression.json_unsigned_array("tags")),
                        IndexPart(expression=IndexExpression.json_unsigned_array("tags")),
                    ),
                    kind=IndexKind.MULTIVALUE,
                ),
            ),
        )
        return _manifest(SchemaProfile.JSON_MULTIVALUE_INNODB, table), limits
    if mutation in {"fk_type_mismatch", "fk_collation_mismatch", "fk_reference_missing_index"}:
        string_fk = mutation == "fk_collation_mismatch"
        parent_key = (
            ColumnDef("code", "VARCHAR(20)", False, "utf8mb4", "utf8mb4_0900_ai_ci")
            if string_fk
            else id_column
        )
        parent = TableDef(
            name="parent",
            temporary=False,
            columns=(parent_key, payload),
            indexes=()
            if mutation == "fk_reference_missing_index"
            else (IndexDef("ix_parent", (IndexPart(column_name=parent_key.name),)),),
        )
        child_fk_column = (
            ColumnDef("parent_code", "VARCHAR(40)", False, "utf8mb4", "utf8mb4_bin")
            if string_fk
            else ColumnDef(
                "parent_id",
                "BIGINT" if mutation == "fk_type_mismatch" else "BIGINT UNSIGNED",
                False,
            )
        )
        child = TableDef(
            name="child",
            temporary=False,
            columns=(ColumnDef("id", "BIGINT UNSIGNED", False), child_fk_column),
            indexes=(IndexDef("ix_parent", (IndexPart(column_name=child_fk_column.name),)),),
            foreign_keys=(
                ForeignKeyDef(
                    "fk_parent",
                    (child_fk_column.name,),
                    "parent",
                    (parent_key.name,),
                ),
            ),
        )
        return SchemaManifest(
            profile=SchemaProfile.FOREIGN_KEY_GRAPH,
            target_feature_id="schema_target",
            seed=1,
            tables=(parent, child),
        ), limits
    raise AssertionError(f"unknown mutation: {mutation}")
