"""Deterministic, constraint-aware data for MySQL 8.0.41 schemas."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
import math
import random
import re
from types import MappingProxyType
from typing import TypeAlias

from select_fuzz.config import NodeRole
from select_fuzz.domain import SeedTree
from select_fuzz.generation.schema import (
    ColumnDef,
    ForeignKeyDef,
    IndexExpressionKind,
    IndexKind,
    SchemaManifest,
    TableDef,
)


class DataGenerationError(ValueError):
    """A schema/count combination has no valid deterministic data assignment."""


class DistributionKind(StrEnum):
    BOUNDARY = "boundary"
    UNIFORM = "uniform"
    ZIPF = "zipf"
    LOW_CARDINALITY = "low_cardinality"
    NULL_HEAVY = "null_heavy"
    UNIQUE = "unique"
    CORRELATED = "correlated"


@dataclass(frozen=True, slots=True)
class DistributionPlan:
    column_name: str
    kind: DistributionKind
    unique_prefix_length: int | None = None


@dataclass(frozen=True, slots=True)
class GeometryValue:
    """A geometry literal kept typed until SQL rendering."""

    kind: str
    wkt: str
    srid: int

    def __post_init__(self) -> None:
        if not self.kind or not self.wkt:
            raise ValueError("geometry kind and WKT must not be empty")
        if not isinstance(self.srid, int) or isinstance(self.srid, bool):
            raise TypeError("geometry SRID must be an integer")
        if not 0 <= self.srid <= 2**32 - 1:
            raise ValueError("geometry SRID must fit an unsigned 32-bit integer")


CellValue: TypeAlias = None | int | float | Decimal | str | bytes | GeometryValue
RowValue: TypeAlias = tuple[CellValue, ...]


@dataclass(frozen=True, slots=True)
class DataBundle:
    """One byte-identical payload shared by all three server roles.

    Each payload entry is canonical headerless ``LOAD DATA`` TSV: tab-delimited,
    backslash-escaped, ``\\N`` for SQL NULL, and one final newline per row.  Inline
    INSERT statements remain the executable, path-independent replay form.
    """

    seed: int
    schema_sha256: str
    payload: Mapping[str, bytes]
    inserts_sql: tuple[str, ...]
    insert_sql_by_table: Mapping[str, tuple[str, ...]]
    sha256_by_table: Mapping[str, str]
    rows_by_table: Mapping[str, tuple[RowValue, ...]]
    distributions: Mapping[str, tuple[DistributionPlan, ...]]
    table_order: tuple[str, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise TypeError("data seed must be an integer")
        object.__setattr__(self, "inserts_sql", tuple(self.inserts_sql))
        object.__setattr__(self, "table_order", tuple(self.table_order))
        object.__setattr__(
            self,
            "payload",
            MappingProxyType({name: bytes(value) for name, value in self.payload.items()}),
        )
        object.__setattr__(
            self,
            "insert_sql_by_table",
            MappingProxyType(
                {name: tuple(statements) for name, statements in self.insert_sql_by_table.items()}
            ),
        )
        object.__setattr__(
            self,
            "sha256_by_table",
            MappingProxyType(dict(self.sha256_by_table)),
        )
        object.__setattr__(
            self,
            "rows_by_table",
            MappingProxyType(
                {
                    name: tuple(tuple(cell for cell in row) for row in rows)
                    for name, rows in self.rows_by_table.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "distributions",
            MappingProxyType(
                {name: tuple(plans) for name, plans in self.distributions.items()}
            ),
        )
        expected = set(self.table_order)
        mappings = (
            self.payload,
            self.insert_sql_by_table,
            self.sha256_by_table,
            self.rows_by_table,
            self.distributions,
        )
        if any(set(mapping) != expected for mapping in mappings):
            raise ValueError("every data mapping must contain exactly the table order")
        if len(expected) != len(self.table_order):
            raise ValueError("table order must not contain duplicates")
        if any(sha256(self.payload[name]).hexdigest() != self.sha256_by_table[name] for name in expected):
            raise ValueError("table payload digest does not match payload bytes")
        if _combined_payload_digest(self.table_order, self.payload) != self.payload_sha256:
            raise ValueError("combined payload digest does not match payload bytes")

    def for_role(self, role: NodeRole) -> Mapping[str, bytes]:
        if not isinstance(role, NodeRole):
            raise TypeError("role must be a NodeRole")
        return self.payload

    def binary_values(self) -> Iterator[bytes]:
        for table_name in self.table_order:
            for row in self.rows_by_table[table_name]:
                for value in row:
                    if isinstance(value, bytes):
                        yield value

    def canonical_bytes(self) -> bytes:
        document = {
            "distributions": {
                name: [plan.kind.value for plan in self.distributions[name]]
                for name in self.table_order
            },
            "insert_sql": list(self.inserts_sql),
            "payload_hex": {name: self.payload[name].hex() for name in self.table_order},
            "payload_sha256": self.payload_sha256,
            "schema_sha256": self.schema_sha256,
            "seed": self.seed,
            "table_order": self.table_order,
        }
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _combined_payload_digest(
    table_order: Sequence[str], payload: Mapping[str, bytes]
) -> str:
    digest = sha256()
    for table_name in table_order:
        encoded_name = table_name.encode("ascii")
        table_payload = payload[table_name]
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(table_payload).to_bytes(8, "big"))
        digest.update(table_payload)
    return digest.hexdigest()


_INTEGER_BOUNDS: Mapping[str, tuple[int, int]] = {
    "TINYINT": (-(2**7), 2**7 - 1),
    "SMALLINT": (-(2**15), 2**15 - 1),
    "MEDIUMINT": (-(2**23), 2**23 - 1),
    "INT": (-(2**31), 2**31 - 1),
    "BIGINT": (-(2**63), 2**63 - 1),
}
_TEXT_TYPES = frozenset(
    {"CHAR", "VARCHAR", "TINYTEXT", "TEXT", "MEDIUMTEXT", "LONGTEXT", "ENUM", "SET"}
)
_BINARY_TYPES = frozenset(
    {"BINARY", "VARBINARY", "TINYBLOB", "BLOB", "MEDIUMBLOB", "LONGBLOB"}
)
_GEOMETRY_TYPES = frozenset(
    {
        "GEOMETRY",
        "POINT",
        "LINESTRING",
        "POLYGON",
        "MULTIPOINT",
        "MULTILINESTRING",
        "MULTIPOLYGON",
        "GEOMETRYCOLLECTION",
    }
)
_DISTRIBUTION_CYCLE = (
    DistributionKind.CORRELATED,
    DistributionKind.BOUNDARY,
    DistributionKind.UNIFORM,
    DistributionKind.ZIPF,
    DistributionKind.LOW_CARDINALITY,
    DistributionKind.NULL_HEAVY,
    DistributionKind.UNIQUE,
)


class DataGenerator:
    """Create deterministic valid rows without global RNG or wall-clock state."""

    def __init__(
        self,
        *,
        max_regular_lob_bytes: int = 64 * 1024,
        max_rows_per_table: int = 1_000_000,
        max_total_rows: int = 4_000_000,
        insert_batch_rows: int = 1_000,
        max_insert_statement_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        positive = {
            "max_regular_lob_bytes": max_regular_lob_bytes,
            "max_rows_per_table": max_rows_per_table,
            "max_total_rows": max_total_rows,
            "insert_batch_rows": insert_batch_rows,
            "max_insert_statement_bytes": max_insert_statement_bytes,
        }
        for name, value in positive.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if max_regular_lob_bytes > 64 * 1024:
            raise ValueError("max_regular_lob_bytes must not exceed the approved 64KiB cap")
        self.max_regular_lob_bytes = max_regular_lob_bytes
        self.max_rows_per_table = max_rows_per_table
        self.max_total_rows = max_total_rows
        self.insert_batch_rows = insert_batch_rows
        self.max_insert_statement_bytes = max_insert_statement_bytes

    def generate(
        self,
        schema: SchemaManifest,
        *,
        seed: int,
        rows_per_table: int | Mapping[str, int],
    ) -> DataBundle:
        if not isinstance(schema, SchemaManifest):
            raise TypeError("schema must be a SchemaManifest")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise TypeError("seed must be an integer")
        counts = self._resolve_counts(schema, rows_per_table)
        table_order = self._table_order(schema)
        tables = {table.name: table for table in schema.tables}
        value_length_limits = self._foreign_string_length_limits(schema)
        tree = SeedTree(seed)
        rows_by_table: dict[str, tuple[RowValue, ...]] = {}
        distributions: dict[str, tuple[DistributionPlan, ...]] = {}

        for table_name in table_order:
            table = tables[table_name]
            plans = self._distribution_plans(
                table,
                counts[table_name],
                value_length_limits.get(table_name, {}),
                tree,
            )
            distributions[table_name] = plans
            rows_by_table[table_name] = self._generate_table_rows(
                table,
                counts[table_name],
                tables,
                rows_by_table,
                plans,
                value_length_limits.get(table_name, {}),
                tree,
            )

        self._validate_constraints(schema, rows_by_table)
        payload: dict[str, bytes] = {}
        insert_sql_by_table: dict[str, tuple[str, ...]] = {}
        for table_name in table_order:
            table = tables[table_name]
            rows = rows_by_table[table_name]
            payload[table_name] = self._render_payload(rows)
            insert_sql_by_table[table_name] = self._render_inserts(table, rows)
        sha256_by_table = {
            table_name: sha256(payload[table_name]).hexdigest() for table_name in table_order
        }
        inserts_sql = tuple(
            statement
            for table_name in table_order
            for statement in insert_sql_by_table[table_name]
        )
        schema_sha256 = sha256(schema.canonical_bytes()).hexdigest()
        return DataBundle(
            seed=seed,
            schema_sha256=schema_sha256,
            payload=payload,
            inserts_sql=inserts_sql,
            insert_sql_by_table=insert_sql_by_table,
            sha256_by_table=sha256_by_table,
            rows_by_table=rows_by_table,
            distributions=distributions,
            table_order=table_order,
            payload_sha256=_combined_payload_digest(table_order, payload),
        )

    def _resolve_counts(
        self,
        schema: SchemaManifest,
        requested: int | Mapping[str, int],
    ) -> dict[str, int]:
        names = tuple(table.name for table in schema.tables)
        if isinstance(requested, bool):
            raise ValueError("rows_per_table must contain integer row counts")
        if isinstance(requested, int):
            counts = {name: requested for name in names}
        elif isinstance(requested, Mapping):
            if set(requested) != set(names):
                raise ValueError("rows_per_table mapping must contain exactly every schema table")
            counts = dict(requested)
        else:
            raise TypeError("rows_per_table must be an integer or table mapping")
        for name, count in counts.items():
            if not isinstance(count, int) or isinstance(count, bool):
                raise ValueError(f"rows for {name} must be an integer")
            if not 0 <= count <= self.max_rows_per_table:
                raise ValueError(
                    f"rows for {name} must be between 0 and {self.max_rows_per_table}"
                )
        if sum(counts.values()) > self.max_total_rows:
            raise ValueError("total rows exceed max_total_rows")
        return counts

    @staticmethod
    def _table_order(schema: SchemaManifest) -> tuple[str, ...]:
        tables = {table.name: table for table in schema.tables}
        remaining = list(tables)
        emitted: list[str] = []
        while remaining:
            ready = [
                name
                for name in remaining
                if all(
                    foreign_key.referenced_table == name
                    or foreign_key.referenced_table in emitted
                    for foreign_key in tables[name].foreign_keys
                )
            ]
            if not ready:
                raise DataGenerationError("foreign key graph contains an unsupported cycle")
            for name in ready:
                remaining.remove(name)
                emitted.append(name)
        return tuple(emitted)

    @staticmethod
    def _foreign_string_length_limits(
        schema: SchemaManifest,
    ) -> dict[str, dict[str, int]]:
        tables = {table.name: table for table in schema.tables}
        parents: dict[tuple[str, str], tuple[str, str]] = {}

        def find(node: tuple[str, str]) -> tuple[str, str]:
            parents.setdefault(node, node)
            while parents[node] != node:
                parents[node] = parents[parents[node]]
                node = parents[node]
            return node

        def union(left: tuple[str, str], right: tuple[str, str]) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        for table in schema.tables:
            for foreign_key in table.foreign_keys:
                referenced = tables[foreign_key.referenced_table]
                for local_name, referenced_name in zip(
                    foreign_key.columns, foreign_key.referenced_columns, strict=True
                ):
                    local = table.column(local_name)
                    if local.base_type in {"CHAR", "VARCHAR", "BINARY", "VARBINARY"}:
                        union(
                            (table.name, local_name),
                            (referenced.name, referenced_name),
                        )

        group_limits: dict[tuple[str, str], int] = {}
        for node in tuple(parents):
            table_name, column_name = node
            length = _first_size(tables[table_name].column(column_name).mysql_type)
            root = find(node)
            group_limits[root] = min(group_limits.get(root, length), length)
        result: dict[str, dict[str, int]] = {}
        for node in tuple(parents):
            table_name, column_name = node
            result.setdefault(table_name, {})[column_name] = group_limits[find(node)]
        return result

    def _distribution_plans(
        self,
        table: TableDef,
        row_count: int,
        value_length_limits: Mapping[str, int],
        tree: SeedTree,
    ) -> tuple[DistributionPlan, ...]:
        fk_columns = {
            column_name
            for foreign_key in table.foreign_keys
            for column_name in foreign_key.columns
        }
        partition_columns = set(table.partition.columns if table.partition is not None else ())
        multivalue_columns = {
            part.expression.column_name
            for index in table.indexes
            if index.kind is IndexKind.MULTIVALUE
            for part in index.parts
            if part.expression is not None
            and part.expression.kind is IndexExpressionKind.JSON_UNSIGNED_ARRAY
        }
        forced_unique = self._forced_unique_columns(table)
        offset = tree.derive("table", table.name, "distribution") % len(_DISTRIBUTION_CYCLE)
        plans: list[DistributionPlan] = []
        for position, column in enumerate(table.columns):
            if column.name == "id" or column.name in forced_unique or column.name in multivalue_columns:
                kind = DistributionKind.UNIQUE
            elif column.name in fk_columns or column.name in partition_columns:
                kind = DistributionKind.CORRELATED
            else:
                kind = _DISTRIBUTION_CYCLE[(position - 1 + offset) % len(_DISTRIBUTION_CYCLE)]
                if (
                    kind is DistributionKind.UNIQUE
                    and _unique_capacity(
                        column,
                        value_length_limit=value_length_limits.get(column.name),
                    )
                    < row_count
                ):
                    kind = DistributionKind.UNIFORM
            prefix_length = forced_unique.get(column.name)
            if (
                column.name in forced_unique
                and _unique_capacity(
                    column,
                    prefix_length=prefix_length,
                    value_length_limit=value_length_limits.get(column.name),
                )
                < row_count
            ):
                raise DataGenerationError(
                    f"unique index on {table.name}.{column.name} cannot hold {row_count} rows"
                )
            plans.append(DistributionPlan(column.name, kind, prefix_length))
        return tuple(plans)

    @staticmethod
    def _forced_unique_columns(table: TableDef) -> dict[str, int | None]:
        forced: dict[str, int | None] = {}
        for index in table.indexes:
            if not (index.primary or index.unique):
                continue
            ordinary = [part.column_name for part in index.parts if part.column_name is not None]
            if "id" in ordinary:
                continue
            for part in index.parts:
                if part.column_name is not None:
                    _merge_unique_prefix(forced, part.column_name, part.prefix_length)
                    break
                if part.expression is not None:
                    prefix = (
                        part.expression.cast_length
                        if part.expression.kind is IndexExpressionKind.LOWER_CHAR
                        else None
                    )
                    _merge_unique_prefix(forced, part.expression.column_name, prefix)
                    break
        return forced

    def _generate_table_rows(
        self,
        table: TableDef,
        count: int,
        tables: Mapping[str, TableDef],
        existing_rows: Mapping[str, tuple[RowValue, ...]],
        plans: tuple[DistributionPlan, ...],
        value_length_limits: Mapping[str, int],
        tree: SeedTree,
    ) -> tuple[RowValue, ...]:
        column_positions = {column.name: position for position, column in enumerate(table.columns)}
        foreign_assignments = self._foreign_assignments(
            table, count, tables, existing_rows, column_positions
        )
        multivalue_columns = {
            part.expression.column_name
            for index in table.indexes
            if index.kind is IndexKind.MULTIVALUE
            for part in index.parts
            if part.expression is not None
            and part.expression.kind is IndexExpressionKind.JSON_UNSIGNED_ARRAY
        }
        mvi_offset_limit = 2**64 - 2 * count
        mvi_offset = tree.derive("table", table.name, "mvi", "offset") % (
            mvi_offset_limit + 1
        )
        rows: list[RowValue] = []
        for row_index in range(count):
            row_values: list[CellValue] = []
            identity = row_index + 1
            for column, plan in zip(table.columns, plans, strict=True):
                assignment_key = (row_index, column.name)
                if assignment_key in foreign_assignments:
                    value: CellValue = foreign_assignments[assignment_key]
                elif column.name == "id":
                    value = identity
                elif (
                    table.partition is not None
                    and table.partition.method == "LIST COLUMNS"
                    and column.name in table.partition.columns
                ):
                    value = table.partition.bucket_for_identity(identity)
                elif column.name in multivalue_columns:
                    # Two disjoint unsigned keys per row make UNIQUE MVI valid without
                    # relying on collision probability.
                    base = mvi_offset + row_index * 2
                    value = json.dumps(
                        [base, base + 1], separators=(",", ":")
                    )
                else:
                    value = self._value_for(
                        column,
                        plan.kind,
                        row_index,
                        random.Random(
                            tree.derive(
                                "table",
                                table.name,
                                "column",
                                column.name,
                                "row",
                                row_index,
                            )
                        ),
                        row_count=count,
                        unique_prefix_length=plan.unique_prefix_length,
                        value_length_limit=value_length_limits.get(column.name),
                    )
                row_values.append(value)
            rows.append(tuple(row_values))
        return tuple(rows)

    def _foreign_assignments(
        self,
        table: TableDef,
        count: int,
        tables: Mapping[str, TableDef],
        existing_rows: Mapping[str, tuple[RowValue, ...]],
        local_positions: Mapping[str, int],
    ) -> dict[tuple[int, str], CellValue]:
        assignments: dict[tuple[int, str], CellValue] = {}
        for foreign_key in table.foreign_keys:
            referenced_table = tables.get(foreign_key.referenced_table)
            if referenced_table is None:
                raise DataGenerationError(
                    f"foreign key {foreign_key.name} references an unknown table"
                )
            if foreign_key.referenced_table == table.name:
                referenced_rows: tuple[RowValue, ...] = ()
            else:
                referenced_rows = existing_rows.get(foreign_key.referenced_table, ())
            referenced_positions = {
                column.name: position for position, column in enumerate(referenced_table.columns)
            }
            nullable_columns = {
                name for name in foreign_key.columns if table.column(name).nullable
            }
            can_be_null = bool(nullable_columns)
            is_unique = self._foreign_key_is_unique(table, foreign_key)
            if not referenced_rows and foreign_key.referenced_table != table.name:
                if count and not can_be_null:
                    raise DataGenerationError(
                        f"nonnullable foreign key {foreign_key.name} has no parent rows"
                    )
            if is_unique and count > len(referenced_rows) and not can_be_null:
                raise DataGenerationError(
                    f"unique foreign key {foreign_key.name} needs at least {count} parent rows"
                )
            for row_index in range(count):
                if foreign_key.referenced_table == table.name:
                    referenced_values: tuple[CellValue, ...] = tuple(
                        row_index + 1 if name == "id" else None
                        for name in foreign_key.referenced_columns
                    )
                    if any(value is None for value in referenced_values):
                        if can_be_null:
                            referenced_values = self._nullable_fk_fallback(
                                table,
                                foreign_key,
                                row_index,
                                count,
                                nullable_columns,
                            )
                        else:
                            raise DataGenerationError(
                                f"nonnullable self foreign key {foreign_key.name} is unsupported"
                            )
                elif not referenced_rows or (is_unique and row_index >= len(referenced_rows)):
                    referenced_values = self._nullable_fk_fallback(
                        table,
                        foreign_key,
                        row_index,
                        count,
                        nullable_columns,
                    )
                else:
                    parent_row = referenced_rows[
                        row_index if is_unique else row_index % len(referenced_rows)
                    ]
                    referenced_values = tuple(
                        parent_row[referenced_positions[name]]
                        for name in foreign_key.referenced_columns
                    )
                for local_name, value in zip(
                    foreign_key.columns, referenced_values, strict=True
                ):
                    if local_name not in local_positions:
                        raise DataGenerationError(
                            f"foreign key {foreign_key.name} references an unknown local column"
                        )
                    key = (row_index, local_name)
                    if key in assignments and assignments[key] != value:
                        raise DataGenerationError("overlapping foreign keys require conflicting values")
                    assignments[key] = value
        return assignments

    def _nullable_fk_fallback(
        self,
        table: TableDef,
        foreign_key: ForeignKeyDef,
        row_index: int,
        row_count: int,
        nullable_columns: set[str],
    ) -> tuple[CellValue, ...]:
        if not nullable_columns:
            raise DataGenerationError(
                f"nonnullable foreign key {foreign_key.name} has no referenced value"
            )
        values: list[CellValue] = []
        for local_name in foreign_key.columns:
            if local_name in nullable_columns:
                values.append(None)
                continue
            column = table.column(local_name)
            values.append(
                self._value_for(
                    column,
                    DistributionKind.CORRELATED,
                    row_index,
                    random.Random(row_index),
                    row_count=row_count,
                    unique_prefix_length=None,
                    value_length_limit=None,
                )
            )
        return tuple(values)

    @staticmethod
    def _foreign_key_is_unique(table: TableDef, foreign_key: ForeignKeyDef) -> bool:
        for index in table.indexes:
            if not (index.primary or index.unique):
                continue
            if any(part.expression is not None or part.prefix_length is not None for part in index.parts):
                continue
            names = tuple(part.column_name for part in index.parts)
            if names == foreign_key.columns:
                return True
        return False

    def _value_for(
        self,
        column: ColumnDef,
        distribution: DistributionKind,
        row_index: int,
        rng: random.Random,
        *,
        row_count: int,
        unique_prefix_length: int | None,
        value_length_limit: int | None,
    ) -> CellValue:
        if distribution is DistributionKind.NULL_HEAVY and column.nullable and rng.randrange(10) < 7:
            return None
        base = column.base_type
        if base in _INTEGER_BOUNDS:
            return self._integer_value(column, distribution, row_index, rng)
        if base == "BIT":
            width = _first_size(column.mysql_type)
            return self._bounded_integer(0, 2**width - 1, distribution, row_index, rng)
        if base == "DECIMAL":
            return self._decimal_value(column, distribution, row_index, rng)
        if base in {"FLOAT", "DOUBLE"}:
            return self._float_value(column, distribution, row_index, rng)
        if base in _TEXT_TYPES:
            return self._text_value(
                column,
                distribution,
                row_index,
                rng,
                row_count=row_count,
                unique_prefix_length=unique_prefix_length,
                value_length_limit=value_length_limit,
            )
        if base in _BINARY_TYPES:
            return self._binary_value(
                column,
                distribution,
                row_index,
                rng,
                row_count=row_count,
                unique_prefix_length=unique_prefix_length,
                value_length_limit=value_length_limit,
            )
        if base == "JSON":
            return self._json_value(distribution, row_index, rng)
        if base in _GEOMETRY_TYPES:
            return self._geometry_value(column, row_index, rng)
        if base == "DATE":
            return self._date_value(distribution, row_index, rng)
        if base == "YEAR":
            return self._year_value(distribution, row_index, rng)
        if base == "TIME":
            return self._time_value(column, distribution, row_index, rng)
        if base in {"DATETIME", "TIMESTAMP"}:
            return self._datetime_value(column, distribution, row_index, rng)
        raise DataGenerationError(f"no value generator for {column.mysql_type}")

    @staticmethod
    def _integer_value(
        column: ColumnDef,
        distribution: DistributionKind,
        row_index: int,
        rng: random.Random,
    ) -> int:
        minimum, maximum = _INTEGER_BOUNDS[column.base_type]
        if column.mysql_type.endswith(" UNSIGNED"):
            minimum = 0
            maximum = maximum * 2 + 1
        return DataGenerator._bounded_integer(
            minimum, maximum, distribution, row_index, rng
        )

    @staticmethod
    def _bounded_integer(
        minimum: int,
        maximum: int,
        distribution: DistributionKind,
        row_index: int,
        rng: random.Random,
    ) -> int:
        cardinality = maximum - minimum + 1
        if distribution is DistributionKind.UNIQUE:
            if row_index >= cardinality:
                raise DataGenerationError("unique distribution exceeds the column cardinality")
            return minimum + row_index
        if distribution is DistributionKind.BOUNDARY:
            candidates = tuple(
                value
                for value in (minimum, maximum, 0, 1, -1)
                if minimum <= value <= maximum
            )
            return candidates[row_index % len(candidates)]
        if distribution is DistributionKind.UNIFORM:
            return rng.randint(minimum, maximum)
        if distribution is DistributionKind.ZIPF:
            return minimum + min(cardinality - 1, int(cardinality * (rng.random() ** 6)))
        if distribution is DistributionKind.LOW_CARDINALITY:
            return minimum + row_index % min(4, cardinality)
        if distribution is DistributionKind.CORRELATED:
            return minimum + (row_index // 2) % cardinality
        return minimum + row_index % min(8, cardinality)

    @staticmethod
    def _decimal_value(
        column: ColumnDef,
        distribution: DistributionKind,
        row_index: int,
        rng: random.Random,
    ) -> Decimal:
        precision, scale = _precision_scale(column.mysql_type)
        maximum_scaled = 10**precision - 1
        signed_minimum = 0 if column.mysql_type.endswith(" UNSIGNED") else -maximum_scaled
        capacity = maximum_scaled - signed_minimum + 1
        if distribution is DistributionKind.UNIQUE and row_index >= capacity:
            raise DataGenerationError("unique decimal distribution exceeds precision")
        scaled = DataGenerator._bounded_integer(
            signed_minimum,
            maximum_scaled,
            distribution,
            row_index,
            rng,
        )
        magnitude = str(abs(scaled))
        digits = tuple(int(character) for character in magnitude)
        return Decimal((1 if scaled < 0 else 0, digits, -scale))

    @staticmethod
    def _float_value(
        column: ColumnDef,
        distribution: DistributionKind,
        row_index: int,
        rng: random.Random,
    ) -> float:
        base = column.base_type
        unsigned = column.mysql_type.endswith(" UNSIGNED")
        maximum = 3.4e38 if base == "FLOAT" else 1.7e308
        if distribution is DistributionKind.BOUNDARY:
            boundary_values = (0.0, 1.0, maximum) if unsigned else (
                0.0,
                -0.0,
                1.0,
                -1.0,
                maximum,
                -maximum,
            )
            return boundary_values[row_index % len(boundary_values)]
        if distribution is DistributionKind.UNIFORM:
            minimum = 0.0 if unsigned else -1_000_000.0
            return rng.uniform(minimum, 1_000_000.0)
        if distribution is DistributionKind.ZIPF:
            value = float((row_index % 17) - 8) / max(1, (row_index % 5) + 1)
            return abs(value) if unsigned else value
        if distribution is DistributionKind.LOW_CARDINALITY:
            low_cardinality_values = (
                (0.0, 1.5, 2.5, 3.5) if unsigned else (-1.5, 0.0, 1.5, 2.5)
            )
            return low_cardinality_values[row_index % 4]
        if distribution is DistributionKind.CORRELATED:
            return float(row_index // 2)
        return float(row_index) + (rng.randrange(1, 1000) / 1000.0)

    def _text_value(
        self,
        column: ColumnDef,
        distribution: DistributionKind,
        row_index: int,
        rng: random.Random,
        *,
        row_count: int,
        unique_prefix_length: int | None,
        value_length_limit: int | None,
    ) -> str:
        base = column.base_type
        if base == "ENUM":
            choices = _declared_members(column.mysql_type)
            return choices[row_index % len(choices)]
        if base == "SET":
            choices = _declared_members(column.mysql_type)
            mask = row_index % (2 ** len(choices))
            return ",".join(
                choice for index, choice in enumerate(choices) if mask & (1 << index)
            )
        capacity = self._text_capacity(column)
        if value_length_limit is not None:
            capacity = min(capacity, value_length_limit)
        if capacity == 0:
            if distribution is DistributionKind.UNIQUE and row_count > 1:
                raise DataGenerationError("zero-length text cannot satisfy uniqueness")
            return ""
        if column.name == "body":
            value = f"document {row_index:08d} alpha beta mysql parallel query"
        elif distribution is DistributionKind.UNIQUE:
            effective = min(capacity, unique_prefix_length or capacity)
            width = _base36_width(max(0, row_count - 1))
            if width > effective:
                raise DataGenerationError("text prefix is too short for unique values")
            value = _base36(row_index).rjust(width, "0")
        elif distribution is DistributionKind.BOUNDARY:
            value = ("", "a", "z" * min(capacity, 64))[row_index % 3]
        elif distribution is DistributionKind.LOW_CARDINALITY:
            value = ("alpha", "beta", "gamma", "delta")[row_index % 4]
        elif distribution is DistributionKind.ZIPF:
            value = "z" * (1 + int(31 * (rng.random() ** 6)))
        elif distribution is DistributionKind.CORRELATED:
            value = f"group_{row_index // 2:08d}"
        elif distribution is DistributionKind.UNIFORM:
            value = f"u_{rng.getrandbits(64):016x}_{row_index:08d}"
        else:
            value = f"unique_{row_index:020d}_{rng.getrandbits(32):08x}"
        return value[:capacity]

    def _text_capacity(self, column: ColumnDef) -> int:
        base = column.base_type
        if base in {"CHAR", "VARCHAR"}:
            return min(_first_size(column.mysql_type), self.max_regular_lob_bytes)
        byte_caps = {
            "TINYTEXT": 255,
            "TEXT": 65_535,
            "MEDIUMTEXT": 16_777_215,
            "LONGTEXT": 4_294_967_295,
        }
        return min(byte_caps[base], self.max_regular_lob_bytes)

    def _binary_value(
        self,
        column: ColumnDef,
        distribution: DistributionKind,
        row_index: int,
        rng: random.Random,
        *,
        row_count: int,
        unique_prefix_length: int | None,
        value_length_limit: int | None,
    ) -> bytes:
        base = column.base_type
        if base in {"BINARY", "VARBINARY"}:
            capacity = min(_first_size(column.mysql_type), self.max_regular_lob_bytes)
        else:
            byte_caps = {
                "TINYBLOB": 255,
                "BLOB": 65_535,
                "MEDIUMBLOB": 16_777_215,
                "LONGBLOB": 4_294_967_295,
            }
            capacity = min(byte_caps[base], self.max_regular_lob_bytes)
        if value_length_limit is not None:
            capacity = min(capacity, value_length_limit)
        if capacity == 0:
            if distribution is DistributionKind.UNIQUE and row_count > 1:
                raise DataGenerationError("zero-length binary cannot satisfy uniqueness")
            return b""
        if distribution is DistributionKind.UNIQUE:
            effective = min(capacity, unique_prefix_length or capacity)
            width = max(1, (max(0, row_count - 1).bit_length() + 7) // 8)
            if width > effective:
                raise DataGenerationError("binary prefix is too short for unique values")
            return row_index.to_bytes(width, "big")
        if distribution is DistributionKind.BOUNDARY:
            values = (b"", b"\x00", b"\xff" * min(capacity, 64))
            return values[row_index % len(values)]
        if distribution is DistributionKind.LOW_CARDINALITY:
            return (b"a", b"b", b"c", b"d")[row_index % 4][:capacity]
        if distribution is DistributionKind.CORRELATED:
            return (f"group_{row_index // 2:08d}".encode("ascii"))[:capacity]
        token = row_index.to_bytes(8, "big", signed=False) + rng.getrandbits(64).to_bytes(8, "big")
        return token[:capacity]

    def _json_value(
        self,
        distribution: DistributionKind,
        row_index: int,
        rng: random.Random,
    ) -> str:
        if distribution is DistributionKind.UNIQUE:
            bucket = row_index
        elif distribution is DistributionKind.CORRELATED:
            bucket = row_index // 2
        else:
            bucket = row_index % 4
        score = rng.randrange(10_000) if distribution is DistributionKind.UNIFORM else bucket
        document = {
            "bucket": bucket,
            "enabled": bool(row_index % 2),
            "score": score,
            "values": [bucket, bucket + 1],
        }
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self.max_regular_lob_bytes:
            raise DataGenerationError("generated JSON exceeds max_regular_lob_bytes")
        return encoded

    @staticmethod
    def _geometry_value(column: ColumnDef, row_index: int, rng: random.Random) -> GeometryValue:
        x = (row_index % 70) + rng.randrange(1000) / 10_000
        y = (row_index % 40) + rng.randrange(1000) / 10_000
        x2, y2 = x + 0.25, y + 0.25
        x3, y3 = x + 0.5, y
        kind = column.base_type
        wkts = {
            "GEOMETRY": f"POINT({x:.4f} {y:.4f})",
            "POINT": f"POINT({x:.4f} {y:.4f})",
            "LINESTRING": f"LINESTRING({x:.4f} {y:.4f},{x2:.4f} {y2:.4f})",
            "POLYGON": (
                f"POLYGON(({x:.4f} {y:.4f},{x2:.4f} {y:.4f},"
                f"{x2:.4f} {y2:.4f},{x:.4f} {y:.4f}))"
            ),
            "MULTIPOINT": f"MULTIPOINT(({x:.4f} {y:.4f}),({x2:.4f} {y2:.4f}))",
            "MULTILINESTRING": (
                f"MULTILINESTRING(({x:.4f} {y:.4f},{x2:.4f} {y2:.4f}))"
            ),
            "MULTIPOLYGON": (
                f"MULTIPOLYGON((({x:.4f} {y:.4f},{x2:.4f} {y:.4f},"
                f"{x2:.4f} {y2:.4f},{x:.4f} {y:.4f})))"
            ),
            "GEOMETRYCOLLECTION": (
                f"GEOMETRYCOLLECTION(POINT({x:.4f} {y:.4f}),"
                f"LINESTRING({x2:.4f} {y2:.4f},{x3:.4f} {y3:.4f}))"
            ),
        }
        return GeometryValue(kind, wkts[kind], column.srid or 0)

    @staticmethod
    def _year_value(
        distribution: DistributionKind,
        row_index: int,
        rng: random.Random,
    ) -> int:
        if distribution is DistributionKind.UNIQUE:
            if row_index >= 256:
                raise DataGenerationError("unique YEAR distribution exceeds cardinality")
            return 0 if row_index == 0 else 1900 + row_index
        if distribution is DistributionKind.BOUNDARY:
            return (0, 1901, 2155)[row_index % 3]
        if distribution is DistributionKind.UNIFORM:
            return 0 if rng.randrange(256) == 0 else rng.randint(1901, 2155)
        if distribution is DistributionKind.LOW_CARDINALITY:
            return (0, 1901, 2000, 2155)[row_index % 4]
        if distribution is DistributionKind.CORRELATED:
            return 1901 + (row_index // 2) % 255
        return 1901 + row_index % 8

    @staticmethod
    def _date_value(
        distribution: DistributionKind,
        row_index: int,
        rng: random.Random,
    ) -> str:
        minimum = date(1000, 1, 1)
        maximum = date(9999, 12, 31)
        days = (maximum - minimum).days
        offset = DataGenerator._bounded_integer(
            0, days, distribution, row_index, rng
        )
        return (minimum + timedelta(days=offset)).isoformat()

    @staticmethod
    def _time_value(
        column: ColumnDef,
        distribution: DistributionKind,
        row_index: int,
        rng: random.Random,
    ) -> str:
        fsp = _optional_size(column.mysql_type)
        maximum_seconds = 838 * 3600 + 59 * 60 + 59
        signed_seconds = DataGenerator._bounded_integer(
            -maximum_seconds,
            maximum_seconds,
            distribution,
            row_index,
            rng,
        )
        seconds = abs(signed_seconds)
        hours, remainder = divmod(seconds, 3600)
        minutes, second = divmod(remainder, 60)
        fraction_value = 0 if seconds == maximum_seconds else row_index % (10**fsp)
        fraction = "" if fsp == 0 else f".{fraction_value:0{fsp}d}"
        sign = "-" if signed_seconds < 0 else ""
        return f"{sign}{hours:02d}:{minutes:02d}:{second:02d}{fraction}"

    @staticmethod
    def _datetime_value(
        column: ColumnDef,
        distribution: DistributionKind,
        row_index: int,
        rng: random.Random,
    ) -> str:
        fsp = _optional_size(column.mysql_type)
        if column.base_type == "TIMESTAMP":
            minimum = datetime(1970, 1, 1, 0, 0, 1)
            maximum = datetime(2038, 1, 19, 3, 14, 6)
        else:
            minimum = datetime(1000, 1, 1)
            maximum = datetime(9999, 12, 31, 23, 59, 59)
        span_seconds = int((maximum - minimum).total_seconds())
        seconds = DataGenerator._bounded_integer(
            0, span_seconds, distribution, row_index, rng
        )
        value = minimum + timedelta(seconds=seconds)
        rendered = value.strftime("%Y-%m-%d %H:%M:%S")
        if fsp:
            rendered += f".{row_index % (10**fsp):0{fsp}d}"
        return rendered

    @staticmethod
    def _render_payload(rows: Sequence[RowValue]) -> bytes:
        payload = bytearray()
        for row in rows:
            payload.extend(b"\t".join(_payload_cell(value) for value in row))
            payload.extend(b"\n")
        return bytes(payload)

    def _render_inserts(self, table: TableDef, rows: Sequence[RowValue]) -> tuple[str, ...]:
        if not rows:
            return ()
        columns = ", ".join(f"`{column.name}`" for column in table.columns)
        prefix = f"INSERT INTO `{table.name}` ({columns}) VALUES "
        prefix_bytes = len(prefix.encode("utf-8"))
        statements: list[str] = []
        batch: list[str] = []
        batch_bytes = 0
        for row in rows:
            rendered = "(" + ", ".join(
                _render_sql_value(column, value)
                for column, value in zip(table.columns, row, strict=True)
            ) + ")"
            rendered_bytes = len(rendered.encode("utf-8"))
            separator_bytes = 2 * len(batch)
            candidate_bytes = prefix_bytes + batch_bytes + separator_bytes + rendered_bytes + 1
            if candidate_bytes > self.max_insert_statement_bytes:
                if not batch:
                    raise DataGenerationError(
                        f"one row for {table.name} exceeds max_insert_statement_bytes"
                    )
                statements.append(prefix + ", ".join(batch) + ";")
                batch = [rendered]
                batch_bytes = rendered_bytes
                if prefix_bytes + rendered_bytes + 1 > self.max_insert_statement_bytes:
                    raise DataGenerationError(
                        f"one row for {table.name} exceeds max_insert_statement_bytes"
                    )
            else:
                batch.append(rendered)
                batch_bytes += rendered_bytes
            if len(batch) == self.insert_batch_rows:
                statements.append(prefix + ", ".join(batch) + ";")
                batch = []
                batch_bytes = 0
        if batch:
            statements.append(prefix + ", ".join(batch) + ";")
        return tuple(statements)

    def _validate_constraints(
        self,
        schema: SchemaManifest,
        rows_by_table: Mapping[str, tuple[RowValue, ...]],
    ) -> None:
        tables = {table.name: table for table in schema.tables}
        for table in schema.tables:
            self._validate_not_null(table, rows_by_table[table.name])
            self._validate_unique_indexes(table, rows_by_table[table.name])
            self._validate_partitions(table, rows_by_table[table.name])
            self._validate_foreign_keys(table, tables, rows_by_table)

    @staticmethod
    def _validate_not_null(table: TableDef, rows: Sequence[RowValue]) -> None:
        for row in rows:
            for column, value in zip(table.columns, row, strict=True):
                if value is None and not column.nullable:
                    raise DataGenerationError(
                        f"nonnullable column {table.name}.{column.name} received NULL"
                    )

    @staticmethod
    def _validate_unique_indexes(table: TableDef, rows: Sequence[RowValue]) -> None:
        positions = {column.name: position for position, column in enumerate(table.columns)}
        for index in table.indexes:
            if not (index.primary or index.unique):
                continue
            seen: set[tuple[object, ...]] = set()
            for row in rows:
                key_parts: list[object] = []
                multivalue_members: tuple[object, ...] | None = None
                for part in index.parts:
                    if part.column_name is not None:
                        value = row[positions[part.column_name]]
                        if part.prefix_length is not None:
                            value = value[: part.prefix_length] if value is not None else None  # type: ignore[index]
                        key_parts.append(value)
                    else:
                        assert part.expression is not None
                        value = row[positions[part.expression.column_name]]
                        if part.expression.kind is IndexExpressionKind.JSON_UNSIGNED_ARRAY:
                            if not isinstance(value, str):
                                raise DataGenerationError("multivalue source must be JSON text")
                            loaded = json.loads(value)
                            if not isinstance(loaded, list):
                                raise DataGenerationError("multivalue source must be a JSON array")
                            multivalue_members = tuple(loaded)
                            key_parts.append(_MULTIVALUE_MARKER)
                        elif part.expression.kind is IndexExpressionKind.LOWER_CHAR:
                            key_parts.append(None if value is None else str(value).lower())
                expanded = (
                    (tuple(key_parts),)
                    if multivalue_members is None
                    else tuple(
                        tuple(member if item is _MULTIVALUE_MARKER else item for item in key_parts)
                        for member in multivalue_members
                    )
                )
                for key in expanded:
                    if any(item is None for item in key) and not index.primary:
                        continue
                    if key in seen:
                        raise DataGenerationError(
                            f"generated duplicate for unique index {table.name}.{index.name}"
                        )
                    seen.add(key)

    @staticmethod
    def _validate_partitions(table: TableDef, rows: Sequence[RowValue]) -> None:
        if table.partition is None or table.partition.method != "LIST COLUMNS":
            return
        positions = {column.name: position for position, column in enumerate(table.columns)}
        for row in rows:
            values = tuple(row[positions[name]] for name in table.partition.columns)
            if len(values) != 1 or not isinstance(values[0], int):
                raise DataGenerationError("LIST COLUMNS payload must match generated integer buckets")
            if not 0 <= values[0] < table.partition.partitions:
                raise DataGenerationError("LIST COLUMNS value has no target partition")

    @staticmethod
    def _validate_foreign_keys(
        table: TableDef,
        tables: Mapping[str, TableDef],
        rows_by_table: Mapping[str, tuple[RowValue, ...]],
    ) -> None:
        local_positions = {column.name: position for position, column in enumerate(table.columns)}
        for foreign_key in table.foreign_keys:
            referenced = tables[foreign_key.referenced_table]
            referenced_positions = {
                column.name: position for position, column in enumerate(referenced.columns)
            }
            valid = {
                tuple(row[referenced_positions[name]] for name in foreign_key.referenced_columns)
                for row in rows_by_table[referenced.name]
            }
            for row in rows_by_table[table.name]:
                key = tuple(row[local_positions[name]] for name in foreign_key.columns)
                if any(value is None for value in key):
                    continue
                if foreign_key.referenced_table == table.name and key == tuple(
                    row[local_positions.get(name, -1)]
                    if name in local_positions
                    else None
                    for name in foreign_key.referenced_columns
                ):
                    continue
                if key not in valid:
                    raise DataGenerationError(
                        f"generated orphan for foreign key {table.name}.{foreign_key.name}"
                    )


_MULTIVALUE_MARKER = object()


def _first_size(declaration: str) -> int:
    match = re.search(r"\(([0-9]+)", declaration)
    if match is None:
        raise DataGenerationError(f"type declaration has no size: {declaration}")
    return int(match.group(1))


def _optional_size(declaration: str) -> int:
    match = re.search(r"\(([0-9]+)\)", declaration)
    return int(match.group(1)) if match is not None else 0


def _precision_scale(declaration: str) -> tuple[int, int]:
    match = re.search(r"\(([0-9]+),([0-9]+)\)", declaration)
    if match is None:
        raise DataGenerationError(f"decimal declaration has no precision/scale: {declaration}")
    return (int(match.group(1)), int(match.group(2)))


def _declared_members(declaration: str) -> tuple[str, ...]:
    return tuple(re.findall(r"'([a-z0-9_]+)'", declaration))


def _merge_unique_prefix(
    forced: dict[str, int | None], column_name: str, prefix_length: int | None
) -> None:
    if column_name not in forced:
        forced[column_name] = prefix_length
        return
    current = forced[column_name]
    if current is None:
        forced[column_name] = prefix_length
    elif prefix_length is not None:
        forced[column_name] = min(current, prefix_length)


_BASE36_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(value: int) -> str:
    if value < 0:
        raise ValueError("base36 value must be nonnegative")
    if value == 0:
        return "0"
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, len(_BASE36_ALPHABET))
        digits.append(_BASE36_ALPHABET[remainder])
    return "".join(reversed(digits))


def _base36_width(value: int) -> int:
    return len(_base36(value))


def _unique_capacity(
    column: ColumnDef,
    *,
    prefix_length: int | None = None,
    value_length_limit: int | None = None,
) -> int:
    base = column.base_type
    if base in _INTEGER_BOUNDS:
        minimum, maximum = _INTEGER_BOUNDS[base]
        if column.mysql_type.endswith(" UNSIGNED"):
            minimum, maximum = 0, maximum * 2 + 1
        return maximum - minimum + 1
    if base == "BIT":
        return 1 << _first_size(column.mysql_type)
    if base == "DECIMAL":
        precision, _ = _precision_scale(column.mysql_type)
        magnitude = int(10**precision)
        return magnitude if column.mysql_type.endswith(" UNSIGNED") else 2 * magnitude - 1
    if base == "YEAR":
        return 256
    if base in {"CHAR", "VARCHAR"}:
        length = _first_size(column.mysql_type)
        if value_length_limit is not None:
            length = min(length, value_length_limit)
        effective = min(length, prefix_length or length)
        return 1 if effective == 0 else len(_BASE36_ALPHABET) ** effective
    if base in {"BINARY", "VARBINARY"}:
        length = _first_size(column.mysql_type)
        if value_length_limit is not None:
            length = min(length, value_length_limit)
        effective = min(length, prefix_length or length)
        return 1 if effective == 0 else 256**effective
    if base == "ENUM":
        return len(_declared_members(column.mysql_type))
    if base == "SET":
        return 1 << len(_declared_members(column.mysql_type))
    return 2**63


def _payload_cell(value: CellValue) -> bytes:
    if value is None:
        return b"\\N"
    if isinstance(value, GeometryValue):
        raw = value.wkt.encode("ascii")
    elif isinstance(value, bytes):
        raw = value
    elif isinstance(value, Decimal):
        raw = format(value, "f").encode("ascii")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise DataGenerationError("non-finite floats are forbidden")
        raw = repr(value).encode("ascii")
    else:
        raw = str(value).encode("utf-8")
    return (
        raw.replace(b"\\", b"\\\\")
        .replace(b"\x00", b"\\0")
        .replace(b"\t", b"\\t")
        .replace(b"\n", b"\\n")
        .replace(b"\r", b"\\r")
        .replace(b"\x1a", b"\\Z")
    )


def _render_sql_value(column: ColumnDef, value: CellValue) -> str:
    if value is None:
        return "NULL"
    base = column.base_type
    if isinstance(value, GeometryValue):
        wkt_hex = value.wkt.encode("ascii").hex().upper()
        return (
            f"ST_GeomFromText(CONVERT(X'{wkt_hex}' USING ascii), {value.srid})"
        )
    if base == "BIT":
        assert isinstance(value, int)
        return f"b'{value:0{_first_size(column.mysql_type)}b}'"
    if isinstance(value, bytes):
        return f"X'{value.hex().upper()}'"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DataGenerationError("non-finite floats are forbidden")
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if base in _TEXT_TYPES:
        charset = column.charset or "utf8mb4"
        return f"CONVERT(X'{value.encode('utf-8').hex().upper()}' USING {charset})"
    if base == "JSON":
        return f"CONVERT(X'{value.encode('utf-8').hex().upper()}' USING utf8mb4)"
    if base in {"DATE", "TIME", "DATETIME", "TIMESTAMP"}:
        return f"'{value}'"
    raise DataGenerationError(f"cannot render value for {column.mysql_type}")


__all__ = [
    "CellValue",
    "DataBundle",
    "DataGenerationError",
    "DataGenerator",
    "DistributionKind",
    "DistributionPlan",
    "GeometryValue",
    "RowValue",
]
