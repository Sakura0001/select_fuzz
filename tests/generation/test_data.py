from __future__ import annotations

from decimal import Decimal
import json
import os
import subprocess
import sys
from types import MappingProxyType

import pytest

from select_fuzz.config import NodeRole
from select_fuzz.generation.data import (
    DataGenerationError,
    DataGenerator,
    DistributionKind,
    GeometryValue,
)
from select_fuzz.generation.catalog import FeatureSpec
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
    SchemaGenerator,
    SchemaLimits,
    TableDef,
)
from select_fuzz.generation.setup import SetupBundleBuilder


def _primary() -> IndexDef:
    return IndexDef(
        "PRIMARY",
        (IndexPart(column_name="id"),),
        unique=True,
        primary=True,
    )


def _schema(
    *tables: TableDef,
    profile: SchemaProfile = SchemaProfile.REGULAR_INNODB,
    same_session: bool = False,
) -> SchemaManifest:
    return SchemaManifest(
        profile=profile,
        target_feature_id="data_target",
        seed=11,
        tables=tables,
        requires_same_session=same_session,
    )


def _regular_table(*columns: ColumnDef, name: str = "t0") -> TableDef:
    return TableDef(
        name=name,
        temporary=False,
        columns=(ColumnDef("id", "BIGINT UNSIGNED", False), *columns),
        indexes=(_primary(),),
    )


def test_one_immutable_data_bundle_is_reused_for_all_roles() -> None:
    schema = _schema(_regular_table(ColumnDef("payload", "VARCHAR(40)", True)))

    bundle = DataGenerator().generate(schema, seed=7, rows_per_table=100)

    assert bundle.for_role(NodeRole.BASELINE) is bundle.payload
    assert bundle.for_role(NodeRole.CUSTOM_OFF) is bundle.payload
    assert bundle.for_role(NodeRole.CUSTOM_ON) is bundle.payload
    assert isinstance(bundle.payload, MappingProxyType)
    with pytest.raises(TypeError):
        bundle.payload["t0"] = b"changed"  # type: ignore[index]


def test_generation_is_byte_stable_and_seed_sensitive() -> None:
    schema = _schema(
        _regular_table(
            ColumnDef("payload", "VARCHAR(40)", True, "utf8mb4", "utf8mb4_0900_ai_ci"),
            ColumnDef("amount", "DECIMAL(20,4)", False),
            ColumnDef("document", "JSON", True),
        )
    )
    generator = DataGenerator()

    first = generator.generate(schema, seed=20260713, rows_per_table=30)
    second = generator.generate(schema, seed=20260713, rows_per_table=30)
    different = generator.generate(schema, seed=20260714, rows_per_table=30)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.payload["t0"] == second.payload["t0"]
    assert first.inserts_sql == second.inserts_sql
    assert first.payload_sha256 == second.payload_sha256
    assert first.canonical_bytes() != different.canonical_bytes()


def test_bundle_bytes_are_stable_across_process_hash_seeds() -> None:
    source = """
from select_fuzz.generation.data import DataGenerator
from select_fuzz.generation.schema import ColumnDef, IndexDef, IndexPart, SchemaManifest, SchemaProfile, TableDef
table = TableDef(
    't0', False,
    (
        ColumnDef('id', 'BIGINT UNSIGNED', False),
        ColumnDef('payload', 'VARCHAR(40)', True, 'utf8mb4', 'utf8mb4_0900_ai_ci'),
        ColumnDef('document', 'JSON', True),
    ),
    (IndexDef('PRIMARY', (IndexPart(column_name='id'),), unique=True, primary=True),),
)
schema = SchemaManifest(SchemaProfile.REGULAR_INNODB, 'process_data', 1, (table,))
print(DataGenerator().generate(schema, seed=77, rows_per_table=25).canonical_bytes().hex())
"""
    outputs = []
    for hash_seed in ("1", "987654"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = hash_seed
        outputs.append(
            subprocess.run(
                [sys.executable, "-c", source],
                check=True,
                capture_output=True,
                env=environment,
            ).stdout
        )

    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("profile", tuple(SchemaProfile))
def test_generated_schema_profiles_accept_deterministic_data(
    profile: SchemaProfile,
) -> None:
    target = FeatureSpec(
        feature_id="data_profile",
        family="data",
        min_version=(8, 0, 0),
        compatible_profiles=frozenset({profile.value}),
        ast_nodes=frozenset({"query_expression"}),
        guards=frozenset({"read_only_select"}),
    )
    generator = SchemaGenerator()
    data_generator = DataGenerator(max_rows_per_table=20, max_total_rows=160)

    for seed in range(25):
        schema = generator.generate(target, seed=seed, limits=SchemaLimits())
        bundle = data_generator.generate(schema, seed=seed, rows_per_table=20)
        assert all(len(bundle.rows_by_table[name]) == 20 for name in bundle.table_order)


@pytest.mark.parametrize(
    "declaration",
    (
        "TINYINT",
        "TINYINT UNSIGNED",
        "SMALLINT",
        "SMALLINT UNSIGNED",
        "MEDIUMINT",
        "MEDIUMINT UNSIGNED",
        "INT",
        "INT UNSIGNED",
        "BIGINT",
        "BIGINT UNSIGNED",
        "BIT(1)",
        "BIT(64)",
        "DECIMAL(1,0)",
        "DECIMAL(65,30)",
        "DECIMAL(10,2) UNSIGNED",
        "FLOAT",
        "FLOAT UNSIGNED",
        "DOUBLE",
        "DOUBLE UNSIGNED",
        "CHAR(0)",
        "CHAR(255)",
        "VARCHAR(0)",
        "VARCHAR(16383)",
        "BINARY(0)",
        "BINARY(255)",
        "VARBINARY(0)",
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
        "LINESTRING",
        "POLYGON",
        "MULTIPOINT",
        "MULTILINESTRING",
        "MULTIPOLYGON",
        "GEOMETRYCOLLECTION",
    ),
)
def test_every_schema_type_has_a_deterministic_executable_value(declaration: str) -> None:
    base = declaration.split("(", 1)[0].split(" ", 1)[0]
    text_types = {
        "CHAR",
        "VARCHAR",
        "TINYTEXT",
        "TEXT",
        "MEDIUMTEXT",
        "LONGTEXT",
        "ENUM",
        "SET",
    }
    geometry_types = {
        "GEOMETRY",
        "POINT",
        "LINESTRING",
        "POLYGON",
        "MULTIPOINT",
        "MULTILINESTRING",
        "MULTIPOLYGON",
        "GEOMETRYCOLLECTION",
    }
    column = ColumnDef(
        "candidate",
        declaration,
        False,
        "utf8mb4" if base in text_types else None,
        "utf8mb4_0900_ai_ci" if base in text_types else None,
        4326 if base in geometry_types else None,
    )
    schema = _schema(_regular_table(column))

    bundle = DataGenerator().generate(schema, seed=19, rows_per_table=5)
    values = [row[1] for row in bundle.rows_by_table["t0"]]

    assert len(values) == 5
    assert all(value is not None for value in values)
    assert bundle.inserts_sql
    assert all("RAND(" not in sql and "NOW(" not in sql for sql in bundle.inserts_sql)
    if base == "DECIMAL":
        assert all(isinstance(value, Decimal) for value in values)
    if declaration.endswith(" UNSIGNED"):
        assert all(value >= 0 for value in values)  # type: ignore[operator]
    if base in {"BINARY", "VARBINARY", "TINYBLOB", "BLOB", "MEDIUMBLOB", "LONGBLOB"}:
        assert all(isinstance(value, bytes) for value in values)
    if base in geometry_types:
        assert all(isinstance(value, GeometryValue) for value in values)


def test_regular_lob_and_json_actual_values_are_capped_at_64_kib() -> None:
    schema = _schema(
        _regular_table(
            ColumnDef("long_text", "LONGTEXT", False, "utf8mb4", "utf8mb4_0900_ai_ci"),
            ColumnDef("long_blob", "LONGBLOB", False),
            ColumnDef("document", "JSON", False),
        )
    )

    bundle = DataGenerator(max_regular_lob_bytes=64 * 1024).generate(
        schema, seed=8, rows_per_table=20
    )

    assert max(len(value) for value in bundle.binary_values()) <= 64 * 1024
    for row in bundle.rows_by_table["t0"]:
        assert len(row[1].encode("utf-8")) <= 64 * 1024  # type: ignore[union-attr]
        assert len(row[3].encode("utf-8")) <= 64 * 1024  # type: ignore[union-attr]
        json.loads(row[3])


def test_decimal_65_30_preserves_all_digits_without_context_rounding() -> None:
    table = TableDef(
        "t0",
        False,
        (
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("amount", "DECIMAL(65,30)", False),
        ),
        (
            _primary(),
            IndexDef("uq_amount", (IndexPart(column_name="amount"),), unique=True),
        ),
    )

    bundle = DataGenerator().generate(_schema(table), seed=1, rows_per_table=1)
    value = bundle.rows_by_table["t0"][0][1]

    assert isinstance(value, Decimal)
    assert format(value, "f") == "-" + "9" * 35 + "." + "9" * 30
    assert "-" + "9" * 35 + "." + "9" * 30 in bundle.inserts_sql[0]


def test_distribution_plan_is_mixed_and_explicit() -> None:
    schema = _schema(
        _regular_table(
            *(ColumnDef(f"c{i}", "INT", True) for i in range(9))
        )
    )

    bundle = DataGenerator().generate(schema, seed=31, rows_per_table=50)
    kinds = {plan.kind for plan in bundle.distributions["t0"]}

    assert DistributionKind.UNIQUE in kinds
    assert DistributionKind.CORRELATED in kinds
    assert len(kinds) >= 5
    table = schema.tables[0]
    positions = {column.name: position for position, column in enumerate(table.columns)}
    for plan in bundle.distributions["t0"]:
        values = [row[positions[plan.column_name]] for row in bundle.rows_by_table["t0"]]
        if plan.kind is DistributionKind.LOW_CARDINALITY:
            assert len(set(values)) <= 4
        elif plan.kind is DistributionKind.BOUNDARY:
            assert -(2**31) in values and 2**31 - 1 in values
        elif plan.kind is DistributionKind.NULL_HEAVY:
            assert sum(value is None for value in values) >= 20
        elif plan.kind is DistributionKind.CORRELATED:
            assert values[0] == values[1]
        elif plan.kind is DistributionKind.UNIQUE:
            assert len(values) == len(set(values))


def test_partition_bucket_values_follow_list_columns_routing() -> None:
    table = TableDef(
        name="t0",
        temporary=False,
        columns=(
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("partition_bucket", "TINYINT UNSIGNED", False),
            ColumnDef("payload", "VARCHAR(20)", True, "utf8mb4", "utf8mb4_0900_ai_ci"),
        ),
        indexes=(
            IndexDef(
                "PRIMARY",
                (IndexPart(column_name="id"), IndexPart(column_name="partition_bucket")),
                unique=True,
                primary=True,
            ),
        ),
        partition=PartitionDef("LIST COLUMNS", ("partition_bucket",), 7),
    )
    schema = _schema(table, profile=SchemaProfile.PARTITIONED_INNODB)

    bundle = DataGenerator().generate(schema, seed=17, rows_per_table=50)

    assert all(row[1] == row[0] % 7 for row in bundle.rows_by_table["t0"])


def test_fk_parents_precede_children_and_every_edge_is_valid() -> None:
    parent = TableDef(
        name="t0",
        temporary=False,
        columns=(
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("tenant_id", "BIGINT UNSIGNED", False),
        ),
        indexes=(
            _primary(),
            IndexDef(
                "ix_parent_ref_target",
                (IndexPart(column_name="id"), IndexPart(column_name="tenant_id")),
                unique=True,
            ),
        ),
    )
    child = TableDef(
        name="t1",
        temporary=False,
        columns=(
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("parent_id", "BIGINT UNSIGNED", False),
            ColumnDef("parent_tenant_id", "BIGINT UNSIGNED", False),
        ),
        indexes=(
            _primary(),
            IndexDef(
                "ix_parent_ref",
                (
                    IndexPart(column_name="parent_id"),
                    IndexPart(column_name="parent_tenant_id"),
                ),
                unique=True,
            ),
        ),
        foreign_keys=(
            ForeignKeyDef(
                "fk_t1_parent",
                ("parent_id", "parent_tenant_id"),
                "t0",
                ("id", "tenant_id"),
            ),
        ),
    )
    schema = _schema(parent, child, profile=SchemaProfile.FOREIGN_KEY_GRAPH)

    bundle = DataGenerator().generate(schema, seed=44, rows_per_table=40)

    assert bundle.table_order == ("t0", "t1")
    parent_keys = {(row[0], row[1]) for row in bundle.rows_by_table["t0"]}
    child_keys = [(row[1], row[2]) for row in bundle.rows_by_table["t1"]]
    assert all(key in parent_keys for key in child_keys)
    assert len(child_keys) == len(set(child_keys))


def test_impossible_unique_nonnullable_fk_cardinality_is_rejected() -> None:
    parent = TableDef(
        "t0",
        False,
        (ColumnDef("id", "BIGINT UNSIGNED", False), ColumnDef("v", "INT", False)),
        (_primary(),),
    )
    child = TableDef(
        "t1",
        False,
        (ColumnDef("id", "BIGINT UNSIGNED", False), ColumnDef("parent_id", "BIGINT UNSIGNED", False)),
        (
            _primary(),
            IndexDef("uq_parent", (IndexPart(column_name="parent_id"),), unique=True),
        ),
        foreign_keys=(ForeignKeyDef("fk_parent", ("parent_id",), "t0", ("id",)),),
    )

    with pytest.raises(DataGenerationError, match="unique foreign key"):
        DataGenerator().generate(
            _schema(parent, child, profile=SchemaProfile.FOREIGN_KEY_GRAPH),
            seed=1,
            rows_per_table={"t0": 2, "t1": 3},
        )


def test_composite_fk_with_one_nullable_component_can_exist_without_parent_rows() -> None:
    parent = TableDef(
        "t0",
        False,
        (
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("tenant_id", "BIGINT UNSIGNED", False),
        ),
        (
            _primary(),
            IndexDef(
                "ix_ref",
                (IndexPart(column_name="id"), IndexPart(column_name="tenant_id")),
                unique=True,
            ),
        ),
    )
    child = TableDef(
        "t1",
        False,
        (
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("parent_id", "BIGINT UNSIGNED", False),
            ColumnDef("parent_tenant_id", "BIGINT UNSIGNED", True),
        ),
        (
            _primary(),
            IndexDef(
                "ix_parent",
                (
                    IndexPart(column_name="parent_id"),
                    IndexPart(column_name="parent_tenant_id"),
                ),
            ),
        ),
        foreign_keys=(
            ForeignKeyDef(
                "fk_parent",
                ("parent_id", "parent_tenant_id"),
                "t0",
                ("id", "tenant_id"),
            ),
        ),
    )

    bundle = DataGenerator().generate(
        _schema(parent, child, profile=SchemaProfile.FOREIGN_KEY_GRAPH),
        seed=3,
        rows_per_table={"t0": 0, "t1": 2},
    )

    assert all(row[1] is not None and row[2] is None for row in bundle.rows_by_table["t1"])


def test_string_fk_values_fit_the_shortest_compatible_declaration() -> None:
    parent = TableDef(
        "t0",
        False,
        (
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("code", "VARCHAR(10)", False, "ascii", "ascii_general_ci"),
        ),
        (
            _primary(),
            IndexDef("ix_code", (IndexPart(column_name="code"),)),
        ),
    )
    child = TableDef(
        "t1",
        False,
        (
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("parent_code", "VARCHAR(2)", False, "ascii", "ascii_general_ci"),
        ),
        (
            _primary(),
            IndexDef("ix_parent_code", (IndexPart(column_name="parent_code"),)),
        ),
        foreign_keys=(
            ForeignKeyDef("fk_code", ("parent_code",), "t0", ("code",)),
        ),
    )

    bundle = DataGenerator().generate(
        _schema(parent, child, profile=SchemaProfile.FOREIGN_KEY_GRAPH),
        seed=13,
        rows_per_table=20,
    )
    parent_values = {row[1] for row in bundle.rows_by_table["t0"]}
    child_values = [row[1] for row in bundle.rows_by_table["t1"]]

    assert all(isinstance(value, str) and len(value) <= 2 for value in parent_values)
    assert all(value in parent_values for value in child_values)


def test_multivalue_arrays_are_unsigned_and_duplicate_free() -> None:
    table = TableDef(
        "t0",
        False,
        (ColumnDef("id", "BIGINT UNSIGNED", False), ColumnDef("tags", "JSON", False)),
        (
            _primary(),
            IndexDef(
                "mx_tags",
                (IndexPart(expression=IndexExpression.json_unsigned_array("tags")),),
                unique=True,
                kind=IndexKind.MULTIVALUE,
            ),
        ),
    )

    bundle = DataGenerator().generate(
        _schema(table, profile=SchemaProfile.JSON_MULTIVALUE_INNODB),
        seed=71,
        rows_per_table=50,
    )
    arrays = [json.loads(row[1]) for row in bundle.rows_by_table["t0"]]
    flattened = [item for values in arrays for item in values]

    assert all(isinstance(item, int) and 0 <= item <= 2**64 - 1 for item in flattened)
    assert len(flattened) == len(set(flattened))


def test_unique_prefix_column_gets_collision_free_leading_tokens() -> None:
    table = TableDef(
        "t0",
        False,
        (
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("short_code", "VARCHAR(2)", False, "ascii", "ascii_general_ci"),
        ),
        (
            _primary(),
            IndexDef(
                "uq_short_prefix",
                (IndexPart(column_name="short_code", prefix_length=2),),
                unique=True,
            ),
        ),
    )

    bundle = DataGenerator(max_rows_per_table=100, max_total_rows=100).generate(
        _schema(table), seed=5, rows_per_table=100
    )
    values = [row[1] for row in bundle.rows_by_table["t0"]]

    assert len(values) == len(set(values))
    assert all(isinstance(value, str) and len(value) <= 2 for value in values)


def test_spatial_and_fulltext_profiles_generate_constraint_consistent_values() -> None:
    spatial = TableDef(
        "t0",
        False,
        (ColumnDef("id", "BIGINT UNSIGNED", False), ColumnDef("location", "POINT", False, srid=4326)),
        (_primary(), IndexDef("sx", (IndexPart(column_name="location"),), kind=IndexKind.SPATIAL)),
    )
    fulltext = TableDef(
        "t0",
        False,
        (
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("body", "LONGTEXT", False, "utf8mb4", "utf8mb4_0900_ai_ci"),
        ),
        (_primary(), IndexDef("ft", (IndexPart(column_name="body"),), kind=IndexKind.FULLTEXT)),
    )

    spatial_bundle = DataGenerator().generate(
        _schema(spatial, profile=SchemaProfile.SPATIAL_INNODB), seed=1, rows_per_table=10
    )
    fulltext_bundle = DataGenerator().generate(
        _schema(fulltext, profile=SchemaProfile.FULLTEXT_INNODB), seed=1, rows_per_table=10
    )

    assert all(row[1].srid == 4326 for row in spatial_bundle.rows_by_table["t0"])  # type: ignore[union-attr]
    assert all("document" in row[1] for row in fulltext_bundle.rows_by_table["t0"])  # type: ignore[operator]


@pytest.mark.parametrize("rows", (-1, 101))
def test_row_count_boundaries_are_enforced(rows: int) -> None:
    schema = _schema(_regular_table(ColumnDef("payload", "INT", False)))
    generator = DataGenerator(max_rows_per_table=100, max_total_rows=100)

    with pytest.raises(ValueError, match="rows"):
        generator.generate(schema, seed=1, rows_per_table=rows)


def test_zero_and_maximum_configured_row_boundaries_are_supported() -> None:
    schema = _schema(_regular_table(ColumnDef("payload", "INT", False)))
    generator = DataGenerator(max_rows_per_table=20, max_total_rows=20)

    empty = generator.generate(schema, seed=1, rows_per_table=0)
    full = generator.generate(schema, seed=1, rows_per_table=20)

    assert empty.rows_by_table["t0"] == ()
    assert empty.payload["t0"] == b""
    assert empty.inserts_sql == ()
    assert len(full.rows_by_table["t0"]) == 20


def test_insert_batches_respect_both_row_and_byte_ceilings() -> None:
    schema = _schema(
        _regular_table(
            ColumnDef("payload", "VARCHAR(40)", False, "utf8mb4", "utf8mb4_0900_ai_ci")
        )
    )
    generator = DataGenerator(
        insert_batch_rows=100,
        max_insert_statement_bytes=220,
        max_rows_per_table=20,
        max_total_rows=20,
    )

    bundle = generator.generate(schema, seed=4, rows_per_table=20)

    assert len(bundle.inserts_sql) > 1
    assert all(len(statement.encode("utf-8")) <= 220 for statement in bundle.inserts_sql)


def test_setup_bundle_orders_session_preamble_ddl_and_data() -> None:
    table = TableDef(
        "t0",
        True,
        (ColumnDef("id", "BIGINT UNSIGNED", False), ColumnDef("payload", "INT", False)),
        (_primary(),),
    )
    schema = _schema(
        table,
        profile=SchemaProfile.TEMPORARY_INNODB,
        same_session=True,
    )

    setup = SetupBundleBuilder(DataGenerator()).build(
        schema, seed=99, rows_per_table=3
    )

    assert setup.requires_same_session
    assert setup.statements[0] == "SET time_zone = '+00:00';"
    assert setup.statements[1].startswith("CREATE TEMPORARY TABLE")
    assert setup.statements[2].startswith("INSERT INTO `t0`")
    assert setup.payload_sha256 == setup.data.payload_sha256
    assert setup.canonical_bytes() == SetupBundleBuilder(DataGenerator()).build(
        schema, seed=99, rows_per_table=3
    ).canonical_bytes()
