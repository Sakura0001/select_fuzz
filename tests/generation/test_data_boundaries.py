from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from hashlib import sha256
import random

import pytest

from select_fuzz.generation.data import (
    DataBundle,
    DataGenerationError,
    DataGenerator,
    DistributionKind,
    DistributionPlan,
    GeometryValue,
    _base36,
    _combined_payload_digest,
    _first_size,
    _merge_unique_prefix,
    _payload_cell,
    _precision_scale,
    _render_sql_value,
    _unique_capacity,
)
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
    TableDef,
)


ID = ColumnDef("id", "BIGINT", False)
PRIMARY = IndexDef(
    "PRIMARY", (IndexPart(column_name="id"),), unique=True, primary=True
)


def table(
    name: str = "items",
    *,
    columns: tuple[ColumnDef, ...] = (ID,),
    indexes: tuple[IndexDef, ...] = (PRIMARY,),
    partition: PartitionDef | None = None,
    foreign_keys: tuple[ForeignKeyDef, ...] = (),
) -> TableDef:
    return TableDef(name, False, columns, indexes, partition, foreign_keys)


def schema(*tables: TableDef) -> SchemaManifest:
    return SchemaManifest(SchemaProfile.REGULAR_INNODB, "data_boundary", 1, tables)


def bundle_kwargs() -> dict[str, object]:
    payload = {"items": b""}
    return {
        "seed": 1,
        "schema_sha256": "schema",
        "payload": payload,
        "inserts_sql": (),
        "insert_sql_by_table": {"items": ()},
        "sha256_by_table": {"items": sha256(b"").hexdigest()},
        "rows_by_table": {"items": ((b"binary",),)},
        "distributions": {
            "items": (DistributionPlan("id", DistributionKind.UNIQUE),)
        },
        "table_order": ("items",),
        "payload_sha256": _combined_payload_digest(("items",), payload),
    }


@pytest.mark.parametrize(
    ("factory", "error"),
    [
        (lambda: GeometryValue("", "POINT(0 0)", 0), ValueError),
        (lambda: GeometryValue("POINT", "", 0), ValueError),
        (lambda: GeometryValue("POINT", "POINT(0 0)", True), TypeError),
        (lambda: GeometryValue("POINT", "POINT(0 0)", -1), ValueError),
        (lambda: DataGenerator(max_regular_lob_bytes=0), ValueError),
        (lambda: DataGenerator(max_rows_per_table=True), ValueError),
        (lambda: DataGenerator(max_regular_lob_bytes=65_537), ValueError),
    ],
)
def test_data_value_objects_reject_invalid_boundaries(
    factory: Callable[[], object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        factory()


def test_data_bundle_validates_all_digests_keys_order_and_roles() -> None:
    kwargs = bundle_kwargs()
    bundle = DataBundle(**kwargs)
    assert list(bundle.binary_values()) == [b"binary"]
    assert bundle.canonical_bytes()
    with pytest.raises(TypeError, match="NodeRole"):
        bundle.for_role("baseline")

    invalid_seed = bundle_kwargs()
    invalid_seed["seed"] = True
    with pytest.raises(TypeError, match="seed"):
        DataBundle(**invalid_seed)

    missing_mapping = bundle_kwargs()
    missing_mapping["distributions"] = {}
    with pytest.raises(ValueError, match="exactly the table order"):
        DataBundle(**missing_mapping)

    duplicate_order = bundle_kwargs()
    duplicate_order["table_order"] = ("items", "items")
    with pytest.raises(ValueError, match="duplicates"):
        DataBundle(**duplicate_order)

    bad_table_digest = bundle_kwargs()
    bad_table_digest["sha256_by_table"] = {"items": "bad"}
    with pytest.raises(ValueError, match="table payload digest"):
        DataBundle(**bad_table_digest)

    bad_combined_digest = bundle_kwargs()
    bad_combined_digest["payload_sha256"] = "bad"
    with pytest.raises(ValueError, match="combined payload digest"):
        DataBundle(**bad_combined_digest)


def test_generator_rejects_invalid_schema_seed_and_row_count_contracts() -> None:
    manifest = schema(table())
    generator = DataGenerator(max_rows_per_table=2, max_total_rows=2)
    with pytest.raises(TypeError, match="SchemaManifest"):
        generator.generate("schema", seed=1, rows_per_table=1)
    with pytest.raises(TypeError, match="seed"):
        generator.generate(manifest, seed=True, rows_per_table=1)
    with pytest.raises(ValueError, match="integer row counts"):
        generator.generate(manifest, seed=1, rows_per_table=True)
    with pytest.raises(ValueError, match="exactly every schema table"):
        generator.generate(manifest, seed=1, rows_per_table={})
    with pytest.raises(TypeError, match="integer or table mapping"):
        generator.generate(manifest, seed=1, rows_per_table=1.5)
    with pytest.raises(ValueError, match="must be an integer"):
        generator.generate(manifest, seed=1, rows_per_table={"items": True})
    with pytest.raises(ValueError, match="between 0 and 2"):
        generator.generate(manifest, seed=1, rows_per_table=3)

    second = table("other")
    with pytest.raises(ValueError, match="total rows"):
        generator.generate(schema(table(), second), seed=1, rows_per_table=2)


def test_foreign_key_cycles_are_rejected_before_data_generation() -> None:
    fk_ab = ForeignKeyDef("fk_a_b", ("id",), "b", ("id",))
    fk_ba = ForeignKeyDef("fk_b_a", ("id",), "a", ("id",))
    manifest = schema(table("a", foreign_keys=(fk_ab,)), table("b", foreign_keys=(fk_ba,)))
    with pytest.raises(DataGenerationError, match="cycle"):
        DataGenerator().generate(manifest, seed=1, rows_per_table=1)


def test_constraint_validators_reject_null_duplicate_partition_and_foreign_key_rows() -> None:
    with pytest.raises(DataGenerationError, match="nonnullable"):
        DataGenerator._validate_not_null(table(), ((None,),))

    with pytest.raises(DataGenerationError, match="duplicate"):
        DataGenerator._validate_unique_indexes(table(), ((1,), (1,)))

    nullable = ColumnDef("value", "INT", True)
    unique = IndexDef("uq_value", (IndexPart(column_name="value"),), unique=True)
    DataGenerator._validate_unique_indexes(
        table(columns=(ID, nullable), indexes=(PRIMARY, unique)),
        ((1, None), (2, None)),
    )

    partitioned = table(partition=PartitionDef("LIST COLUMNS", ("id",), 2))
    with pytest.raises(DataGenerationError, match="integer buckets"):
        DataGenerator._validate_partitions(partitioned, (("bad",),))
    with pytest.raises(DataGenerationError, match="no target partition"):
        DataGenerator._validate_partitions(partitioned, ((2,),))

    parent = table("parent")
    child = table(
        "child",
        foreign_keys=(ForeignKeyDef("fk_child", ("id",), "parent", ("id",)),),
    )
    with pytest.raises(DataGenerationError, match="orphan"):
        DataGenerator._validate_foreign_keys(
            child,
            {"parent": parent, "child": child},
            {"parent": ((1,),), "child": ((2,),)},
        )
    DataGenerator._validate_foreign_keys(
        child,
        {"parent": parent, "child": child},
        {"parent": ((1,),), "child": ((None,),)},
    )


def test_multivalue_and_functional_unique_index_validation_paths() -> None:
    document = ColumnDef("document", "JSON", True)
    expression = IndexExpression.json_unsigned_array("document")
    unique = IndexDef(
        "uq_document",
        (IndexPart(expression=expression),),
        unique=True,
        kind=IndexKind.MULTIVALUE,
    )
    candidate = table(columns=(ID, document), indexes=(PRIMARY, unique))
    with pytest.raises(DataGenerationError, match="JSON text"):
        DataGenerator._validate_unique_indexes(candidate, ((1, 1),))
    with pytest.raises(DataGenerationError, match="JSON array"):
        DataGenerator._validate_unique_indexes(candidate, ((1, "{}"),))
    with pytest.raises(DataGenerationError, match="duplicate"):
        DataGenerator._validate_unique_indexes(candidate, ((1, "[1,1]"),))

    text = ColumnDef("text_value", "VARCHAR(10)", True)
    functional = IndexDef(
        "uq_lower",
        (IndexPart(expression=IndexExpression.lower_char("text_value", 10)),),
        unique=True,
        kind=IndexKind.FUNCTIONAL,
    )
    functional_table = table(columns=(ID, text), indexes=(PRIMARY, functional))
    with pytest.raises(DataGenerationError, match="duplicate"):
        DataGenerator._validate_unique_indexes(functional_table, ((1, "A"), (2, "a")))


def test_data_rendering_helpers_cover_escaping_types_and_errors() -> None:
    assert _payload_cell(None) == b"\\N"
    assert _payload_cell(Decimal("1.20")) == b"1.20"
    assert _payload_cell(GeometryValue("POINT", "POINT(0 0)", 0)) == b"POINT(0 0)"
    assert _payload_cell(b"\\\x00\t\n\r\x1a") == b"\\\\\\0\\t\\n\\r\\Z"
    with pytest.raises(DataGenerationError, match="non-finite"):
        _payload_cell(float("nan"))

    assert _render_sql_value(ID, None) == "NULL"
    assert _render_sql_value(ColumnDef("bits", "BIT(3)", False), 2) == "b'010'"
    assert _render_sql_value(ColumnDef("raw", "BINARY(2)", False), b"\x01") == "X'01'"
    assert _render_sql_value(ColumnDef("amount", "DECIMAL(3,1)", False), Decimal("1.2")) == "1.2"
    assert _render_sql_value(ColumnDef("value", "INT", False), 1) == "1"
    assert "CONVERT" in _render_sql_value(
        ColumnDef(
            "value",
            "VARCHAR(4)",
            False,
            "utf8mb4",
            "utf8mb4_0900_ai_ci",
        ),
        "x",
    )
    assert _render_sql_value(ColumnDef("day", "DATE", False), "2024-01-01") == "'2024-01-01'"
    assert "ST_GeomFromText" in _render_sql_value(
        ColumnDef("shape", "POINT", False, srid=0),
        GeometryValue("POINT", "POINT(0 0)", 0),
    )
    with pytest.raises(DataGenerationError, match="non-finite"):
        _render_sql_value(ColumnDef("value", "DOUBLE", False), float("inf"))
    with pytest.raises(DataGenerationError, match="cannot render"):
        _render_sql_value(ColumnDef("shape", "POINT", False, srid=0), "POINT(0 0)")


def test_capacity_and_parsing_helpers_cover_boundary_types() -> None:
    with pytest.raises(DataGenerationError, match="has no size"):
        _first_size("INT")
    with pytest.raises(DataGenerationError, match="precision/scale"):
        _precision_scale("DECIMAL")
    with pytest.raises(ValueError, match="nonnegative"):
        _base36(-1)
    assert _base36(0) == "0"
    assert _base36(36) == "10"

    forced: dict[str, int | None] = {}
    _merge_unique_prefix(forced, "value", None)
    _merge_unique_prefix(forced, "value", 5)
    _merge_unique_prefix(forced, "value", 3)
    assert forced == {"value": 3}

    assert _unique_capacity(ColumnDef("value", "TINYINT UNSIGNED", False)) == 256
    assert _unique_capacity(ColumnDef("value", "BIT(2)", False)) == 4
    assert _unique_capacity(ColumnDef("value", "DECIMAL(2,0)", False)) == 199
    assert _unique_capacity(ColumnDef("value", "YEAR", False)) == 256
    assert _unique_capacity(
        ColumnDef(
            "value", "VARCHAR(4)", False, "utf8mb4", "utf8mb4_0900_ai_ci"
        ),
        prefix_length=2,
        value_length_limit=3,
    ) == 36**2
    assert _unique_capacity(ColumnDef("value", "VARBINARY(2)", False)) == 256**2
    assert _unique_capacity(ColumnDef("value", "ENUM('a','b')", False)) == 2
    assert _unique_capacity(ColumnDef("value", "SET('a','b')", False)) == 4


def test_distribution_helpers_reject_impossible_unique_cardinalities() -> None:
    generator = DataGenerator(max_regular_lob_bytes=8)
    rng = random.Random(1)
    with pytest.raises(DataGenerationError, match="column cardinality"):
        generator._bounded_integer(0, 1, DistributionKind.UNIQUE, 2, rng)
    with pytest.raises(DataGenerationError, match="precision"):
        generator._decimal_value(
            ColumnDef("value", "DECIMAL(1,0) UNSIGNED", False),
            DistributionKind.UNIQUE,
            10,
            rng,
        )
    with pytest.raises(DataGenerationError, match="zero-length text"):
        generator._text_value(
            ColumnDef(
                "value", "VARCHAR(0)", False, "utf8mb4", "utf8mb4_0900_ai_ci"
            ),
            DistributionKind.UNIQUE,
            0,
            rng,
            row_count=2,
            unique_prefix_length=None,
            value_length_limit=None,
        )
    with pytest.raises(DataGenerationError, match="text prefix"):
        generator._text_value(
            ColumnDef(
                "value", "VARCHAR(8)", False, "utf8mb4", "utf8mb4_0900_ai_ci"
            ),
            DistributionKind.UNIQUE,
            0,
            rng,
            row_count=37,
            unique_prefix_length=1,
            value_length_limit=None,
        )
    with pytest.raises(DataGenerationError, match="zero-length binary"):
        generator._binary_value(
            ColumnDef("value", "VARBINARY(0)", False),
            DistributionKind.UNIQUE,
            0,
            rng,
            row_count=2,
            unique_prefix_length=None,
            value_length_limit=None,
        )
    with pytest.raises(DataGenerationError, match="binary prefix"):
        generator._binary_value(
            ColumnDef("value", "VARBINARY(8)", False),
            DistributionKind.UNIQUE,
            0,
            rng,
            row_count=257,
            unique_prefix_length=1,
            value_length_limit=2,
        )
    with pytest.raises(DataGenerationError, match="YEAR"):
        generator._year_value(DistributionKind.UNIQUE, 256, rng)


def test_foreign_assignment_rejects_missing_parents_and_unsupported_self_edges() -> None:
    generator = DataGenerator()
    child = table(
        "child",
        foreign_keys=(ForeignKeyDef("fk", ("id",), "missing", ("id",)),),
    )
    with pytest.raises(DataGenerationError, match="unknown table"):
        generator._foreign_assignments(
            child, 1, {"child": child}, {}, {"id": 0}
        )

    parent = table("parent")
    child = table(
        "child",
        foreign_keys=(ForeignKeyDef("fk", ("id",), "parent", ("id",)),),
    )
    with pytest.raises(DataGenerationError, match="no parent rows"):
        generator._foreign_assignments(
            child,
            1,
            {"child": child, "parent": parent},
            {"parent": ()},
            {"id": 0},
        )

    other = ColumnDef("other", "INT", False)
    self_table = table(
        "self_table",
        columns=(ID, other),
        foreign_keys=(
            ForeignKeyDef("fk_self", ("other",), "self_table", ("other",)),
        ),
    )
    with pytest.raises(DataGenerationError, match="self foreign key"):
        generator._foreign_assignments(
            self_table,
            1,
            {"self_table": self_table},
            {},
            {"id": 0, "other": 1},
        )
