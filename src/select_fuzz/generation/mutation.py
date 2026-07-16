"""Deterministic periodic multi-row DML for correctness rounds."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import random
from typing import Protocol


class MutationOperation(StrEnum):
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class MutationStatement:
    operation: MutationOperation
    sql: str
    target_rows: int

    def __post_init__(self) -> None:
        if not self.sql.strip() or "\n" in self.sql or "\r" in self.sql:
            raise ValueError("mutation SQL must be one nonempty physical line")
        if self.target_rows <= 0:
            raise ValueError("mutation target_rows must be positive")


@dataclass(frozen=True, slots=True)
class MutationBatch:
    seed: int
    sequence: int
    statements: tuple[MutationStatement, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "statements", tuple(self.statements))
        if not 1 <= len(self.statements) <= 3:
            raise ValueError("mutation batches require one to three statements")
        if not 12 <= self.target_rows <= 50:
            raise ValueError("mutation batches must target 12 to 50 rows")
        if self.sequence <= 0:
            raise ValueError("mutation sequence must be positive")

    @property
    def target_rows(self) -> int:
        return sum(statement.target_rows for statement in self.statements)


class ColumnLike(Protocol):
    name: str


class TableLike(Protocol):
    name: str
    columns: tuple[ColumnLike, ...]


class SchemaLike(Protocol):
    tables: tuple[TableLike, ...]


class DataLike(Protocol):
    table_order: tuple[str, ...]


class SetupLike(Protocol):
    schema: SchemaLike
    data: DataLike


def _operation(rng: random.Random) -> MutationOperation:
    ticket = rng.random()
    if ticket < 0.5:
        return MutationOperation.INSERT
    if ticket < 0.75:
        return MutationOperation.UPDATE
    return MutationOperation.DELETE


def _partition_rows(rng: random.Random, total: int, parts: int) -> tuple[int, ...]:
    remaining = total
    values: list[int] = []
    for index in range(parts - 1):
        value = rng.randint(1, remaining - (parts - index - 1))
        values.append(value)
        remaining -= value
    values.append(remaining)
    rng.shuffle(values)
    return tuple(values)


class MutationBatchGenerator:
    """Generate syntactically valid DML against real generated tables/columns."""

    def generate(self, setup: SetupLike, *, seed: int, sequence: int) -> MutationBatch:
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise TypeError("mutation seed must be an integer")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
            raise ValueError("mutation sequence must be positive")
        tables = {table.name: table for table in setup.schema.tables}
        ordered = tuple(tables[name] for name in setup.data.table_order)
        if not ordered:
            raise ValueError("mutation setup requires at least one table")
        rng = random.Random(seed)
        count = rng.randint(1, 3)
        targets = _partition_rows(rng, rng.randint(12, 50), count)
        rows_by_table = getattr(setup.data, "rows_by_table", None)
        row_counts = (
            {name: len(rows_by_table[name]) for name in setup.data.table_order}
            if isinstance(rows_by_table, Mapping)
            and set(setup.data.table_order).issubset(rows_by_table)
            else None
        )
        if row_counts is not None and not any(row_counts.values()):
            raise ValueError("mutation setup requires at least one populated table")
        statements: list[MutationStatement] = []
        for index, target_rows in enumerate(targets):
            operation = _operation(rng)
            candidates = ordered
            if row_counts is not None:
                if operation is MutationOperation.INSERT:
                    candidates = tuple(table for table in ordered if row_counts[table.name] > 0)
                else:
                    candidates = tuple(
                        table for table in ordered if row_counts[table.name] >= target_rows
                    )
                if not candidates:
                    insert_candidates = tuple(
                        table for table in ordered if row_counts[table.name] > 0
                    )
                    if insert_candidates:
                        operation = MutationOperation.INSERT
                        candidates = insert_candidates
                    else:
                        candidates = ordered
            table = candidates[rng.randrange(len(candidates))]
            column_names = tuple(column.name for column in table.columns)
            if "id" not in column_names:
                raise ValueError("mutation tables require the generated id column")
            offset = 10_000_000_000 + sequence * 1_000_000 + index * 10_000
            if operation is MutationOperation.INSERT:
                columns = ", ".join(f"`{name}`" for name in column_names)
                selected = ", ".join(
                    (
                        f"`sf_source`.`id` + {offset} + `sf_seq`.`n`"
                        if name == "id"
                        else f"`sf_source`.`{name}`"
                    )
                    for name in column_names
                )
                if row_counts is None:
                    sql = (
                        f"INSERT INTO `{table.name}` ({columns}) SELECT {selected} "
                        f"FROM `{table.name}` AS `sf_source` "
                        f"CROSS JOIN (SELECT 0 AS `n`) AS `sf_seq` "
                        f"ORDER BY `sf_source`.`id` LIMIT {target_rows}"
                    )
                else:
                    sequence_rows = " UNION ALL ".join(
                        f"SELECT {value} AS `n`" if value == 0 else f"SELECT {value}"
                        for value in range(target_rows)
                    )
                    sql = (
                        f"INSERT INTO `{table.name}` ({columns}) SELECT {selected} FROM "
                        f"(SELECT * FROM `{table.name}` ORDER BY `id` LIMIT 1) "
                        f"AS `sf_source` CROSS JOIN ({sequence_rows}) AS `sf_seq` "
                        f"ORDER BY `sf_seq`.`n`"
                    )
                    row_counts[table.name] += target_rows
            elif operation is MutationOperation.UPDATE:
                sql = (
                    f"UPDATE `{table.name}` SET `id` = `id` + {offset} "
                    f"ORDER BY `id` DESC LIMIT {target_rows}"
                )
            else:
                sql = f"DELETE FROM `{table.name}` ORDER BY `id` DESC LIMIT {target_rows}"
                if row_counts is not None:
                    row_counts[table.name] = max(0, row_counts[table.name] - target_rows)
            statements.append(MutationStatement(operation, sql, target_rows))
        return MutationBatch(seed, sequence, tuple(statements))


__all__ = [
    "MutationBatch",
    "MutationBatchGenerator",
    "MutationOperation",
    "MutationStatement",
]
