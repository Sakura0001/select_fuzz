"""PQ-safe schema materialization.

Builds deterministic InnoDB tables using only PQ-whitelisted types (int,
bigint, decimal, double, varchar, date, datetime) with single-column indexes,
and fills them with data deliberately shaped to stress PQ correctness: low- and
medium-cardinality key columns (for GROUP BY / joins), a NULL-prone optional
column, duplicate strings, negative and high-scale decimals, and skewed groups.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from collections.abc import Iterator

from select_fuzz.pq.config import PQConfig, validate_database
from select_fuzz.pq.connection import PQConnection


@dataclass(frozen=True, slots=True)
class PQColumn:
    name: str
    kind: str  # int | bigint | decimal | double | varchar | date | datetime
    nullable: bool
    indexed: bool = False


@dataclass(frozen=True, slots=True)
class PQTable:
    name: str
    columns: tuple[PQColumn, ...]

    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def indexed_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.indexed)


@dataclass(frozen=True, slots=True)
class PQSchema:
    database: str
    tables: tuple[PQTable, ...]

    def table(self, name: str) -> PQTable:
        for t in self.tables:
            if t.name == name:
                return t
        raise KeyError(name)


# A stable, generator-friendly column layout for every table. The generator
# can rely on these names existing and picks subsets as needed.
STANDARD_COLUMNS: tuple[PQColumn, ...] = (
    PQColumn("k1", "int", nullable=False, indexed=True),  # low cardinality (groups)
    PQColumn("k2", "int", nullable=False, indexed=True),  # medium cardinality
    PQColumn("val_dec", "decimal", nullable=False, indexed=False),
    PQColumn("val_dbl", "double", nullable=True, indexed=False),
    PQColumn("s1", "varchar", nullable=True, indexed=True),  # duplicate strings
    PQColumn("d1", "date", nullable=True, indexed=False),
    PQColumn("dt1", "datetime", nullable=False, indexed=False),
    PQColumn("opt", "int", nullable=True, indexed=True),  # NULL-prone
)


def _mysql_type(kind: str, nullable: bool = False) -> str:
    return {
        "int": "INT",
        "bigint": "BIGINT",
        "decimal": "DECIMAL(18,6)",
        "double": "DOUBLE",
        "varchar": "VARCHAR(64)",
        "date": "DATE",
        "datetime": "DATETIME",
    }[kind] + (" NULL DEFAULT NULL" if nullable else " NOT NULL")


def _table_ddl(name: str) -> str:
    cols = ["id BIGINT NOT NULL AUTO_INCREMENT"]
    cols.append("PRIMARY KEY (id)")
    for c in STANDARD_COLUMNS:
        cols.append(f"{c.name} {_mysql_type(c.kind, c.nullable)}")
    index_cols = [c.name for c in STANDARD_COLUMNS if c.indexed]
    indexes = [f"KEY idx_{name}_{c} ({c})" for c in index_cols]
    body = ",\n  ".join(cols + indexes)
    return (f"CREATE TABLE {name} (\n  {body}\n) ENGINE=InnoDB "
            "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin")


def _row_values(rng: random.Random, table_index: int) -> tuple[object, ...]:
    """Produce one data row (without the auto id)."""

    # Skewed groups: a few large groups + many tiny ones, deterministic per table.
    grp = rng.choices([1, 2, 3, 4, 5, 6, 7], weights=[40, 25, 15, 8, 5, 4, 3])[0]
    k2 = rng.randint(0, 200)
    # Decimals: mix of small/large, negative, high scale, near-integer.
    scale = rng.choice([2, 4, 6])
    val_dec = Decimal(f"{rng.randint(-99999, 999999)}.{rng.randint(0, 10**scale - 1):0{scale}d}")
    val_dbl = None if rng.random() < 0.12 else round(rng.uniform(-100000, 100000), 6)
    s1 = None if rng.random() < 0.08 else rng.choice(
        ["alpha", "beta", "gamma", "delta", "epsilon", "alpha", "beta"]
    )
    d1 = None if rng.random() < 0.10 else date(2018, 1, 1) + timedelta(days=rng.randint(0, 2000))
    base = datetime(2019, 1, 1) + timedelta(
        days=rng.randint(0, 1500), seconds=rng.randint(0, 86399)
    )
    dt1 = base
    opt = None if rng.random() < 0.35 else rng.randint(0, 3)
    return (grp, k2, val_dec, val_dbl, s1, d1, dt1, opt)


@dataclass(frozen=True, slots=True)
class PQSchemaSpec:
    schema: PQSchema
    create_statements: tuple[str, ...]
    row_counts: tuple[int, ...]


def build_schema_spec(database: str, *, table_count: int) -> PQSchemaSpec:
    tables = tuple(
        PQTable(name=f"t{i}", columns=STANDARD_COLUMNS) for i in range(table_count)
    )
    create = tuple(_table_ddl(t.name) for t in tables)
    return PQSchemaSpec(
        schema=PQSchema(database=database, tables=tables),
        create_statements=create,
        row_counts=(),
    )


def _format_cell(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, datetime):
        return f"'{value.isoformat(sep=' ')}'"
    if isinstance(value, date):
        return f"'{value.isoformat()}'"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def _insert_batches(table: str, rows: list[tuple[object, ...]], *, batch: int = 500) -> list[str]:
    col_names = ", ".join(c.name for c in STANDARD_COLUMNS)
    statements: list[str] = []
    for offset in range(0, len(rows), batch):
        chunk = rows[offset : offset + batch]
        values = ", ".join(
            "(" + ", ".join(_format_cell(v) for v in row) + ")" for row in chunk
        )
        statements.append(f"INSERT INTO {table} ({col_names}) VALUES {values}")
    return statements


def materialize(
    conn: PQConnection,
    *,
    database: str,
    table_count: int,
    rows_per_table: int,
    seed: int,
) -> PQSchema:
    """Create the database, tables, and deterministic data. Returns the schema."""

    validate_database(database)
    if table_count < 1 or rows_per_table < 1:
        raise ValueError("table_count and rows_per_table must be positive")
    spec = build_schema_spec(database, table_count=table_count)
    for statement in setup_statements(database, table_count=table_count,
                                      rows_per_table=rows_per_table, seed=seed):
        conn.run_script([statement])
    return spec.schema


def setup_statements(database: str, *, table_count: int, rows_per_table: int,
                     seed: int) -> Iterator[str]:
    """Deterministic full fixture, streamed in batches; fails on an existing DB."""
    validate_database(database)
    yield f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"
    yield f"USE `{database}`"
    spec = build_schema_spec(database, table_count=table_count)
    yield from spec.create_statements
    for idx, table in enumerate(spec.schema.tables):
        rng = random.Random((seed * 100003 + idx * 9176 + 7) & 0xFFFFFFFF)
        for offset in range(0, rows_per_table, 500):
            rows = [_row_values(rng, idx) for _ in range(min(500, rows_per_table - offset))]
            yield from _insert_batches(table.name, rows)
        yield f"ANALYZE TABLE {table.name}"


def write_setup(config: PQConfig, artifacts_dir: Path) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = artifacts_dir / "setup.sql"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(f"-- Deterministic fixture: seed={config.seed}. Run in a NEW test schema.\n")
        for stmt in setup_statements(config.database, table_count=config.materialize_tables,
                                     rows_per_table=config.materialize_rows, seed=config.seed):
            fh.write(stmt + ";\n")
    (artifacts_dir / "cleanup.sql").write_text(
        "-- Explicit cleanup: deletes ALL data in this run's dedicated test schema.\n"
        f"DROP DATABASE `{config.database}`;\n", encoding="utf-8",
    )
    return path


__all__ = [
    "PQColumn",
    "PQSchema",
    "PQSchemaSpec",
    "PQTable",
    "STANDARD_COLUMNS",
    "build_schema_spec",
    "materialize",
    "setup_statements",
    "write_setup",
]
