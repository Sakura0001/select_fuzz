from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Sequence, Set

from select_fuzz.metadata.models import ColumnMetadata, ColumnTypeFamily, TableMetadata

from .operators import build_operator_registry


@dataclass(frozen=True)
class GenerationOptions:
    require_cte: bool = False
    require_join: bool = False
    require_vector: bool = False
    require_set_operation: bool = False


class SQLGenerator:
    def __init__(self, random_seed: int | None = None, max_sql_length: int = 8000) -> None:
        self.random = random.Random(random_seed)
        self.max_sql_length = max_sql_length
        self.registry = build_operator_registry()
        self.coverage_hits: Set[str] = set()

    def generate(self, tables: Sequence[TableMetadata], options: GenerationOptions | None = None) -> str:
        if not tables:
            raise ValueError("至少需要一张表元数据才能生成 SQL")
        options = options or GenerationOptions()
        self.coverage_hits.clear()

        sql = self._generate_query(list(tables), options)
        if options.require_set_operation:
            op = self.random.choice(["UNION", "INTERSECT", "EXCEPT"])
            self._hit(op)
            sql = f"{sql} {op} {self._simple_query(tables[0])}"
        if options.require_cte:
            cte_table = tables[0]
            self._hit("WITH")
            sql = f"WITH cte_1 AS ({self._simple_query(cte_table)}) {sql}"

        if len(sql) > self.max_sql_length:
            return self._fallback_query(tables[0])
        return sql

    def _generate_query(self, tables: List[TableMetadata], options: GenerationOptions) -> str:
        base_table = self._choose_base_table(tables, options)
        select_items = self._select_items(base_table, options)
        from_clause = f"FROM {base_table.name} AS t0"
        join_clause = ""
        if options.require_join and len(tables) > 1:
            join_table = next(table for table in tables if table.name != base_table.name)
            join_clause = self._join_clause(base_table, join_table)
        where_clause = self._where_clause(base_table)
        order_clause = self._order_clause(base_table)
        self._hit("FROM")
        self._hit("WHERE")
        self._hit("LIMIT")
        return " ".join(
            part
            for part in [
                f"SELECT {select_items}",
                from_clause,
                join_clause,
                where_clause,
                order_clause,
                "LIMIT 50",
            ]
            if part
        )

    def _select_items(self, table: TableMetadata, options: GenerationOptions) -> str:
        columns = list(table.columns.values())
        items: List[str] = []
        for column in columns[:3]:
            items.append(f"t0.`{column.name}`")
        json_column = self._first_column(table, ColumnTypeFamily.JSON)
        if json_column:
            self._hit("JSON_ARROW_UNQUOTE")
            items.append(f"t0.`{json_column.name}`->>'$.a' AS json_value")
        if options.require_vector:
            vector_column = self._first_column(table, ColumnTypeFamily.VECTOR)
            if vector_column:
                self._hit("DISTANCE_COSINE")
                self._hit("STRING_TO_VECTOR")
                vector_literal = self._vector_literal(vector_column)
                items.append(
                    f"DISTANCE(t0.`{vector_column.name}`, STRING_TO_VECTOR('{vector_literal}'), 'COSINE') AS vector_distance"
                )
        if not items:
            items.append("1")
        return ", ".join(items)

    def _join_clause(self, base_table: TableMetadata, join_table: TableMetadata) -> str:
        self._hit("JOIN ... ON")
        left_column = self._joinable_column(base_table)
        right_column = self._joinable_column(join_table)
        return f"JOIN {join_table.name} AS t1 ON t0.`{left_column.name}` = t1.`{right_column.name}`"

    def _where_clause(self, table: TableMetadata) -> str:
        column = self._predicate_column(table)
        if column.type_family in {ColumnTypeFamily.INTEGER, ColumnTypeFamily.DECIMAL, ColumnTypeFamily.FLOAT}:
            self._hit(">=")
            return f"WHERE t0.`{column.name}` >= 0"
        if column.type_family is ColumnTypeFamily.STRING:
            self._hit("LIKE")
            return f"WHERE t0.`{column.name}` LIKE '%a%'"
        self._hit("IS NULL")
        return f"WHERE t0.`{column.name}` IS NOT NULL"

    def _order_clause(self, table: TableMetadata) -> str:
        column = self._predicate_column(table)
        self._hit("ORDER BY ASC")
        return f"ORDER BY t0.`{column.name}` ASC"

    def _simple_query(self, table: TableMetadata) -> str:
        column = next(iter(table.columns.values()), None)
        select_expr = f"`{column.name}`" if column else "1"
        return f"SELECT {select_expr} FROM {table.name} LIMIT 1"

    def _fallback_query(self, table: TableMetadata) -> str:
        return f"SELECT 1 FROM {table.name}"

    def _choose_base_table(self, tables: List[TableMetadata], options: GenerationOptions) -> TableMetadata:
        if options.require_vector:
            vector_tables = [
                table
                for table in tables
                if self._first_column(table, ColumnTypeFamily.VECTOR) is not None
            ]
            if vector_tables:
                return self.random.choice(vector_tables)
        return self.random.choice(tables)

    def _first_column(self, table: TableMetadata, family: ColumnTypeFamily) -> ColumnMetadata | None:
        for column in table.columns.values():
            if column.type_family is family:
                return column
        return None

    def _predicate_column(self, table: TableMetadata) -> ColumnMetadata:
        for family in [
            ColumnTypeFamily.INTEGER,
            ColumnTypeFamily.DECIMAL,
            ColumnTypeFamily.FLOAT,
            ColumnTypeFamily.STRING,
            ColumnTypeFamily.DATETIME,
        ]:
            column = self._first_column(table, family)
            if column:
                return column
        return next(iter(table.columns.values()))

    def _joinable_column(self, table: TableMetadata) -> ColumnMetadata:
        for column in table.columns.values():
            if column.type_family in {ColumnTypeFamily.INTEGER, ColumnTypeFamily.DECIMAL, ColumnTypeFamily.STRING}:
                return column
        return next(iter(table.columns.values()))

    def _vector_literal(self, column: ColumnMetadata) -> str:
        dimensions = column.vector_dimensions or 4
        values = [f"{self.random.random():.3f}" for _ in range(dimensions)]
        return "[" + ",".join(values) + "]"

    def _hit(self, name: str) -> None:
        if self.registry.has(name):
            self.coverage_hits.add(name)
