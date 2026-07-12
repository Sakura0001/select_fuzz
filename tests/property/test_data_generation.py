from __future__ import annotations

from hypothesis import given, settings, strategies as st

from select_fuzz.generation.data import DataGenerator
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexPart,
    PartitionDef,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)


def _property_schema(*, partitioned: bool = False) -> SchemaManifest:
    partition = PartitionDef("LIST COLUMNS", ("partition_bucket",), 5) if partitioned else None
    columns = [
        ColumnDef("id", "BIGINT UNSIGNED", False),
        ColumnDef("payload", "VARCHAR(80)", True, "utf8mb4", "utf8mb4_0900_ai_ci"),
        ColumnDef("amount", "DECIMAL(30,8)", False),
        ColumnDef("document", "JSON", True),
    ]
    primary_parts = [IndexPart(column_name="id")]
    if partitioned:
        columns.insert(1, ColumnDef("partition_bucket", "TINYINT UNSIGNED", False))
        primary_parts.append(IndexPart(column_name="partition_bucket"))
    table = TableDef(
        "t0",
        False,
        tuple(columns),
        (IndexDef("PRIMARY", tuple(primary_parts), unique=True, primary=True),),
        partition=partition,
    )
    return SchemaManifest(
        profile=(
            SchemaProfile.PARTITIONED_INNODB
            if partitioned
            else SchemaProfile.REGULAR_INNODB
        ),
        target_feature_id="property_data",
        seed=3,
        tables=(table,),
    )


@settings(max_examples=10_000, deadline=None)
@given(seed=st.integers(min_value=0, max_value=2**127 - 1))
def test_ten_thousand_seeds_are_reproducible_and_constraint_safe(seed: int) -> None:
    schema = _property_schema(partitioned=bool(seed & 1))
    generator = DataGenerator(max_rows_per_table=4, max_total_rows=4)

    first = generator.generate(schema, seed=seed, rows_per_table=4)
    second = generator.generate(schema, seed=seed, rows_per_table=4)

    assert first.canonical_bytes() == second.canonical_bytes()
    rows = first.rows_by_table["t0"]
    assert len({row[0] for row in rows}) == len(rows)
    if schema.tables[0].partition is not None:
        assert all(row[1] == row[0] % 5 for row in rows)
    assert all(len(value) <= 64 * 1024 for value in first.binary_values())


@settings(max_examples=300, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=2**64 - 1),
    rows=st.integers(min_value=0, max_value=40),
)
def test_payload_hashes_and_insert_batches_cover_exactly_the_configured_rows(
    seed: int, rows: int
) -> None:
    schema = _property_schema()
    bundle = DataGenerator(
        max_rows_per_table=40,
        max_total_rows=40,
        insert_batch_rows=7,
    ).generate(schema, seed=seed, rows_per_table=rows)

    assert len(bundle.rows_by_table["t0"]) == rows
    assert set(bundle.sha256_by_table) == {"t0"}
    assert len(bundle.sha256_by_table["t0"]) == 64
    assert len(bundle.inserts_sql) == (rows + 6) // 7
