from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set

from select_fuzz.metadata.models import ColumnMetadata, ColumnTypeFamily, ForeignKeyMetadata, TableMetadata

from .operators import build_operator_registry


@dataclass(frozen=True)
class GenerationOptions:
    require_cte: bool = False
    require_join: bool = False
    require_vector: bool = False
    require_set_operation: bool = False
    require_subquery: bool = False
    require_window: bool = False
    require_locking: bool = False


@dataclass(frozen=True)
class TableRef:
    table: TableMetadata
    alias: str


@dataclass(frozen=True)
class Expr:
    sql: str
    family: ColumnTypeFamily


class SQLGenerator:
    def __init__(self, random_seed: int | None = None, max_sql_length: int = 8000) -> None:
        self.random = random.Random(random_seed)
        self.max_sql_length = max_sql_length
        self.registry = build_operator_registry()
        self.coverage_hits: Set[str] = set()
        self.coverage_counts: Dict[str, int] = {}
        self.recent_hits: List[str] = []
        self._attempt_hits: Set[str] = set()
        self._query_depth_limit = 3
        self._expr_depth_limit = 3
        self._risk_probability = 0.08

    def generate(self, tables: Sequence[TableMetadata], options: GenerationOptions | None = None) -> str:
        if not tables:
            raise ValueError("至少需要一张表元数据才能生成 SQL")
        options = options or GenerationOptions()
        table_list = list(tables)

        for _ in range(8):
            self._attempt_hits = set()
            self._query_depth_limit = self.random.randint(2, 7)
            self._expr_depth_limit = self.random.randint(2, 6)
            sql = self._generate_query_expression(table_list, options, self._query_depth_limit)
            if len(sql) <= self.max_sql_length:
                self._commit_hits()
                return sql

        self._attempt_hits = set()
        sql = self._fallback_query(table_list[0])
        self._commit_hits()
        return sql

    def _generate_query_expression(self, tables: List[TableMetadata], options: GenerationOptions, depth: int) -> str:
        use_set_operation = options.require_set_operation or (depth > 1 and self.random.random() < 0.18)
        projection_count = 3 if use_set_operation and options.require_window else 2 if use_set_operation else None
        body = self._select_block(tables, options, depth, projection_count=projection_count)
        if use_set_operation:
            op = self.random.choice(["UNION", "UNION ALL"])
            self._hit(op.split()[0])
            right = self._select_block(tables, GenerationOptions(), max(0, depth - 1), projection_count=projection_count, allow_locking=False)
            body = f"({body}) {op} ({right})"

        if options.require_cte or (depth > 1 and self.random.random() < 0.22):
            body = self._with_clause(tables, body, depth)

        return body

    def _select_block(
        self,
        tables: List[TableMetadata],
        options: GenerationOptions,
        depth: int,
        projection_count: Optional[int] = None,
        allow_locking: bool = True,
    ) -> str:
        refs, from_clause, join_count = self._from_clause(tables, options)
        group_enabled = self._should_group(options)
        window_enabled = options.require_window or self.random.random() < 0.18
        locking_enabled = allow_locking and (options.require_locking or self.random.random() < 0.08)
        if options.require_vector:
            group_enabled = False
        if projection_count is not None:
            group_enabled = False
            window_enabled = options.require_window
            locking_enabled = allow_locking and options.require_locking

        modifier = self._select_modifier(locking_enabled)
        select_items, group_columns, orderable_aliases = self._select_items(
            refs,
            depth,
            group_enabled,
            window_enabled,
            projection_count,
            options.require_vector,
        )
        where_clause = self._where_clause(refs, tables, depth, options.require_subquery)
        group_clause = self._group_clause(group_columns)
        having_clause = self._having_clause(group_enabled)
        window_clause = self._window_clause(refs, window_enabled, group_columns if group_enabled else None)
        order_clause = self._vector_order_clause(refs, options.require_vector, group_enabled) or self._order_clause(
            refs,
            orderable_aliases,
            modifier.strip() == "DISTINCT" or group_enabled,
        )
        limit_clause = self._limit_clause()
        locking_clause = self._locking_clause(refs, locking_enabled)

        self._hit("FROM")
        if join_count:
            self._hit("JOIN ... ON")
        return " ".join(
            part
            for part in [
                f"SELECT {modifier}{select_items}",
                from_clause,
                where_clause,
                group_clause,
                having_clause,
                window_clause,
                order_clause,
                limit_clause,
                locking_clause,
            ]
            if part
        )

    def _from_clause(self, tables: List[TableMetadata], options: GenerationOptions) -> tuple[List[TableRef], str, int]:
        table_pool = self._query_table_pool(tables)
        table_count = self.random.randint(1, min(len(table_pool), self.random.randint(1, 5)))
        if options.require_join and len(tables) > 1:
            table_count = max(2, table_count)
        chosen = self.random.sample(table_pool, min(table_count, len(table_pool)))
        if options.require_vector:
            vector_tables = [table for table in tables if self._first_column(table, ColumnTypeFamily.VECTOR)]
            if vector_tables and not self._first_column(chosen[0], ColumnTypeFamily.VECTOR):
                chosen[0] = self.random.choice(vector_tables)
        refs = [TableRef(table=chosen[0], alias="t0")]
        sql = f"FROM {_q(chosen[0].name)} AS t0"

        for index, table in enumerate(chosen[1:], start=1):
            ref = TableRef(table=table, alias=f"t{index}")
            join_sql = self._join_clause(refs, ref)
            refs.append(ref)
            sql = f"{sql} {join_sql}"
        return refs, sql, len(chosen) - 1

    def _join_clause(self, existing: List[TableRef], new_ref: TableRef) -> str:
        join_kind = self.random.choices(
            ["INNER JOIN", "LEFT JOIN", "RIGHT JOIN", "CROSS JOIN", "NATURAL JOIN", "STRAIGHT_JOIN", "JOIN ... USING", "JOIN ... ON"],
            weights=[22, 14, 10, 8, 2, 8, 3, 30],
            k=1,
        )
        join_kind = join_kind[0]
        if join_kind == "CROSS JOIN":
            self._hit("CROSS JOIN")
            return f"CROSS JOIN {_q(new_ref.table.name)} AS {new_ref.alias}"
        if join_kind == "NATURAL JOIN":
            self._hit("NATURAL JOIN")
            return f"NATURAL JOIN {_q(new_ref.table.name)} AS {new_ref.alias}"
        if join_kind == "STRAIGHT_JOIN":
            self._hit("STRAIGHT_JOIN")
            return f"STRAIGHT_JOIN {_q(new_ref.table.name)} AS {new_ref.alias} ON {self._join_condition(existing, new_ref)}"
        if join_kind == "JOIN ... USING":
            common = self._common_columns(existing[-1].table, new_ref.table)
            if common:
                self._hit("JOIN ... USING")
                columns = ", ".join(_q(name) for name in self.random.sample(common, min(len(common), self.random.randint(1, 3))))
                return f"JOIN {_q(new_ref.table.name)} AS {new_ref.alias} USING ({columns})"
        if join_kind in {"INNER JOIN", "LEFT JOIN", "RIGHT JOIN"}:
            self._hit(join_kind)
            return f"{join_kind} {_q(new_ref.table.name)} AS {new_ref.alias} ON {self._join_condition(existing, new_ref)}"
        self._hit("JOIN ... ON")
        return f"JOIN {_q(new_ref.table.name)} AS {new_ref.alias} ON {self._join_condition(existing, new_ref)}"

    def _join_condition(self, existing: List[TableRef], new_ref: TableRef) -> str:
        fk_condition = self._foreign_key_condition(existing, new_ref)
        if fk_condition:
            return fk_condition
        left = self.random.choice(existing)
        left_column = self._joinable_column(left.table)
        right_column = self._compatible_column(new_ref.table, left_column.type_family) or self._joinable_column(new_ref.table)
        return f"{left.alias}.{_q(left_column.name)} <=> {new_ref.alias}.{_q(right_column.name)}"

    def _foreign_key_condition(self, existing: List[TableRef], new_ref: TableRef) -> Optional[str]:
        for ref in existing:
            condition = self._fk_match(new_ref, ref, new_ref.table.foreign_keys)
            if condition:
                return condition
            condition = self._fk_match(ref, new_ref, ref.table.foreign_keys)
            if condition:
                return condition
        return None

    def _fk_match(self, child_ref: TableRef, parent_ref: TableRef, foreign_keys: List[ForeignKeyMetadata]) -> Optional[str]:
        for fk in foreign_keys:
            if fk.parent_table != parent_ref.table.name:
                continue
            pairs = zip(fk.child_columns, fk.parent_columns)
            parts = [f"{child_ref.alias}.{_q(child)} = {parent_ref.alias}.{_q(parent)}" for child, parent in pairs]
            if parts:
                return " AND ".join(parts)
        return None

    def _select_modifier(self, locking_enabled: bool) -> str:
        if locking_enabled:
            self._hit("SELECT ALL")
            return "ALL "
        modifier = self.random.choice(["", "ALL ", "DISTINCT "])
        if modifier == "ALL ":
            self._hit("SELECT ALL")
        elif modifier == "DISTINCT ":
            self._hit("SELECT DISTINCT")
        return modifier

    def _select_items(
        self,
        refs: List[TableRef],
        depth: int,
        group_enabled: bool,
        window_enabled: bool,
        projection_count: Optional[int],
        require_vector: bool,
    ) -> tuple[str, List[str], List[str]]:
        count = projection_count or self.random.randint(1, 6)
        items: List[str] = []
        group_columns: List[str] = []
        orderable_aliases: List[str] = []
        expression_count = max(1, count - 1) if window_enabled else count

        if group_enabled:
            group_columns = [self._column_expr(refs).sql for _ in range(self.random.randint(1, 2))]
            for index, column in enumerate(group_columns):
                items.append(f"{column} AS g{index}")
                orderable_aliases.append(f"g{index}")
            for index in range(max(1, count - len(items))):
                items.append(f"{self._aggregate_expr(refs)} AS a{index}")
                orderable_aliases.append(f"a{index}")
        else:
            for index in range(expression_count):
                expr = self._random_expr(refs, depth=self._expr_depth_limit).sql
                items.append(f"{expr} AS c{index}")
                orderable_aliases.append(f"c{index}")

        if window_enabled:
            self._hit("ROW_NUMBER")
            items.append("ROW_NUMBER() OVER w AS rn")
            orderable_aliases.append("rn")

        if require_vector:
            vector = self._vector_distance_expr(refs)
            if vector:
                items.append(f"{vector} AS vector_distance")
                orderable_aliases.append("vector_distance")
            vector_text = self._vector_to_string_expr(refs)
            if vector_text and projection_count is None:
                items.append(f"{vector_text} AS vector_text")
                orderable_aliases.append("vector_text")

        if projection_count is None and not group_enabled and self.random.random() < 0.08:
            ref = self.random.choice(refs)
            items.append(f"{ref.alias}.*")

        return ", ".join(items), group_columns, orderable_aliases

    def _where_clause(self, refs: List[TableRef], tables: List[TableMetadata], depth: int, require_subquery: bool) -> str:
        if not require_subquery and self.random.random() < 0.18:
            return ""
        predicate = self._predicate_expr(refs, tables, max(1, depth - 1), require_subquery)
        self._hit("WHERE")
        return f"WHERE {predicate}"

    def _group_clause(self, group_columns: List[str]) -> str:
        if not group_columns:
            return ""
        self._hit("GROUP BY")
        suffix = " WITH ROLLUP" if self.random.random() < 0.18 else ""
        return "GROUP BY " + ", ".join(group_columns) + suffix

    def _having_clause(self, group_enabled: bool) -> str:
        if not group_enabled or self.random.random() < 0.25:
            return ""
        self._hit("HAVING")
        return f"HAVING {self._aggregate_expr_from_name()} {self.random.choice(['>', '>=', '<=', '<>'])} {self.random_int(0, 20)}"

    def _window_clause(self, refs: List[TableRef], window_enabled: bool, group_columns: Optional[List[str]] = None) -> str:
        if not window_enabled:
            return ""
        if group_columns:
            partition = self.random.choice(group_columns)
            order = self.random.choice(group_columns)
        else:
            partition = self._column_expr(refs, preferred={ColumnTypeFamily.INTEGER, ColumnTypeFamily.STRING}).sql
            order = self._column_expr(refs, preferred={ColumnTypeFamily.INTEGER, ColumnTypeFamily.DATETIME}).sql
        self._hit("WINDOW")
        return f"WINDOW w AS (PARTITION BY {partition} ORDER BY {order})"

    def _order_clause(self, refs: List[TableRef], orderable_aliases: List[str], alias_only: bool = False) -> str:
        if self.random.random() < 0.25:
            return ""
        direction = self.random.choice(["ASC", "DESC"])
        self._hit(f"ORDER BY {direction}")
        if alias_only and orderable_aliases:
            expr = self.random.choice(orderable_aliases)
        else:
            expr = self._column_expr(refs, preferred={ColumnTypeFamily.INTEGER, ColumnTypeFamily.STRING, ColumnTypeFamily.DATETIME}).sql
        return f"ORDER BY {expr} {direction}"

    def _limit_clause(self) -> str:
        self._hit("LIMIT")
        row_count = self.random_int(1, 50)
        if self.random.random() < 0.35:
            return f"LIMIT {self.random_int(0, 5)}, {row_count}"
        if self.random.random() < 0.35:
            return f"LIMIT {row_count} OFFSET {self.random_int(0, 5)}"
        return f"LIMIT {row_count}"

    def _locking_clause(self, refs: List[TableRef], enabled: bool) -> str:
        if not enabled:
            return ""
        clause = self.random.choice(["FOR UPDATE", "FOR SHARE", "LOCK IN SHARE MODE"])
        self._hit(clause)
        if clause in {"FOR UPDATE", "FOR SHARE"} and self.random.random() < 0.35:
            clause += f" OF {self.random.choice(refs).alias}"
        if clause in {"FOR UPDATE", "FOR SHARE"} and self.random.random() < 0.35:
            option = self.random.choice(["NOWAIT", "SKIP LOCKED"])
            self._hit(option)
            clause += f" {option}"
        return clause

    def _with_clause(self, tables: List[TableMetadata], body: str, depth: int) -> str:
        if depth > 1 and self.random.random() < 0.35:
            self._hit("WITH RECURSIVE")
            return (
                "WITH RECURSIVE cte_num(n) AS "
                "(SELECT 1 UNION ALL SELECT n + 1 FROM cte_num WHERE n < 5) "
                f"{body}"
            )
        self._hit("WITH")
        cte_query = self._select_block(tables, GenerationOptions(), max(0, depth - 1), projection_count=2, allow_locking=False)
        return f"WITH cte_1 AS ({cte_query}) {body}"

    def _predicate_expr(self, refs: List[TableRef], tables: List[TableMetadata], depth: int, require_subquery: bool = False) -> str:
        if depth > 0 and (require_subquery or self.random.random() < 0.18):
            require_subquery = False
            kind = self.random.choice(["EXISTS", "IN", "SCALAR"])
            if kind == "EXISTS":
                self._hit("EXISTS")
                self._hit("EXISTS SUBQUERY")
                return f"EXISTS ({self._simple_subquery(tables)})"
            if kind == "IN":
                expr = self._column_expr(refs, preferred={ColumnTypeFamily.INTEGER}).sql
                self._hit("IN")
                self._hit("IN SUBQUERY")
                return f"{expr} IN ({self._single_column_subquery(tables)})"
            self._hit("SCALAR SUBQUERY")
            expr = self._numeric_expr(refs, depth - 1).sql
            return f"{expr} >= ({self._scalar_subquery(tables)})"

        if depth > 0 and self.random.random() < 0.35:
            left = self._predicate_expr(refs, tables, depth - 1)
            right = self._predicate_expr(refs, tables, depth - 1)
            op = self.random.choice(["AND", "OR", "XOR"])
            self._hit(op)
            return f"({left}) {op} ({right})"
        if depth > 0 and self.random.random() < 0.16:
            self._hit("NOT")
            return f"NOT ({self._predicate_expr(refs, tables, depth - 1)})"

        column = self._column_expr(refs)
        if column.family in {ColumnTypeFamily.INTEGER, ColumnTypeFamily.DECIMAL, ColumnTypeFamily.FLOAT, ColumnTypeFamily.BIT}:
            if self.random.random() < 0.18:
                self._hit("BETWEEN")
                return f"{column.sql} BETWEEN {self.random_int(-20, 20)} AND {self.random_int(21, 80)}"
            op = self.random.choice(["=", "<=>", "<>", "!=", ">", ">=", "<", "<="])
            self._hit(op)
            return f"{column.sql} {op} {self._numeric_expr(refs, max(0, depth - 1)).sql}"
        if column.family in {ColumnTypeFamily.STRING, ColumnTypeFamily.ENUM, ColumnTypeFamily.SET}:
            op = self.random.choice(["LIKE", "REGEXP", "=", "<>"])
            self._hit(op)
            rhs = self._string_literal()
            if op == "LIKE":
                rhs = "'%a%'"
            if op == "REGEXP":
                rhs = "'[a-z0-9_]+'"
            return f"{column.sql} {op} {rhs}"
        if column.family is ColumnTypeFamily.JSON:
            self._hit("JSON_EXTRACT")
            return f"JSON_EXTRACT({column.sql}, '$.k') IS NOT NULL"
        if column.family is ColumnTypeFamily.SPATIAL:
            self._hit("ST_ASTEXT")
            return f"ST_AsText({column.sql}) IS NOT NULL"
        self._hit("IS NULL")
        return f"{column.sql} IS NOT NULL"

    def _random_expr(self, refs: List[TableRef], depth: int) -> Expr:
        if self.random.random() < self._risk_probability:
            return self._risky_expr(refs, depth)
        families = [
            ColumnTypeFamily.INTEGER,
            ColumnTypeFamily.DECIMAL,
            ColumnTypeFamily.FLOAT,
            ColumnTypeFamily.STRING,
            ColumnTypeFamily.DATETIME,
            ColumnTypeFamily.JSON,
            ColumnTypeFamily.BINARY,
            ColumnTypeFamily.BIT,
        ]
        if self._has_family(refs, ColumnTypeFamily.SPATIAL):
            families.append(ColumnTypeFamily.SPATIAL)
        family = self.random.choice(families)
        if family in {ColumnTypeFamily.INTEGER, ColumnTypeFamily.DECIMAL, ColumnTypeFamily.FLOAT, ColumnTypeFamily.BIT}:
            return self._numeric_expr(refs, depth)
        if family is ColumnTypeFamily.STRING:
            return self._string_expr(refs, depth)
        if family is ColumnTypeFamily.DATETIME:
            return self._datetime_expr(refs, depth)
        if family is ColumnTypeFamily.JSON:
            return self._json_expr(refs)
        if family is ColumnTypeFamily.SPATIAL:
            return self._spatial_expr(refs)
        if family is ColumnTypeFamily.BINARY:
            return self._binary_expr(refs)
        return self._column_expr(refs)

    def _numeric_expr(self, refs: List[TableRef], depth: int) -> Expr:
        if depth <= 0 or self.random.random() < 0.45:
            return self._column_or_literal(refs, {ColumnTypeFamily.INTEGER, ColumnTypeFamily.DECIMAL, ColumnTypeFamily.FLOAT, ColumnTypeFamily.BIT})
        op = self.random.choice(["+", "-", "*", "/", "DIV", "MOD", "&", "|", "^", "<<", ">>"])
        self._hit(op)
        left = self._numeric_expr(refs, depth - 1).sql
        right = self._numeric_expr(refs, depth - 1).sql
        if op in {"/", "DIV", "MOD"}:
            self._hit("NULLIF")
            right = f"NULLIF({right}, 0)"
        if op in {"<<", ">>"}:
            right = str(self.random_int(0, 3))
        return Expr(f"({left} {op} {right})", ColumnTypeFamily.DECIMAL if op == "/" else ColumnTypeFamily.INTEGER)

    def _string_expr(self, refs: List[TableRef], depth: int) -> Expr:
        if depth <= 0 or self.random.random() < 0.42:
            return self._column_or_literal(refs, {ColumnTypeFamily.STRING, ColumnTypeFamily.ENUM, ColumnTypeFamily.SET})
        choice = self.random.choice(["CONCAT", "SUBSTRING", "LOWER", "CAST", "CASE WHEN", "IFNULL"])
        self._hit(choice)
        if choice == "CONCAT":
            return Expr(f"CONCAT({self._string_expr(refs, depth - 1).sql}, {self._string_literal()})", ColumnTypeFamily.STRING)
        if choice == "SUBSTRING":
            return Expr(f"SUBSTRING({self._string_expr(refs, depth - 1).sql}, 1, {self.random_int(1, 8)})", ColumnTypeFamily.STRING)
        if choice == "LOWER":
            return Expr(f"LOWER({self._string_expr(refs, depth - 1).sql})", ColumnTypeFamily.STRING)
        if choice == "IFNULL":
            return Expr(f"IFNULL({self._string_expr(refs, depth - 1).sql}, {self._string_literal()})", ColumnTypeFamily.STRING)
        if choice == "CASE WHEN":
            return Expr(f"CASE WHEN {self._numeric_expr(refs, 1).sql} > 0 THEN {self._string_literal()} ELSE {self._string_literal()} END", ColumnTypeFamily.STRING)
        self._hit("CAST")
        return Expr(f"CAST({self._numeric_expr(refs, depth - 1).sql} AS CHAR)", ColumnTypeFamily.STRING)

    def _datetime_expr(self, refs: List[TableRef], depth: int) -> Expr:
        column = self._column_or_literal(refs, {ColumnTypeFamily.DATETIME})
        if depth > 0 and self.random.random() < 0.45:
            self._hit("DATE_ADD")
            return Expr(f"DATE_ADD({column.sql}, INTERVAL {self.random_int(-10, 10)} DAY)", ColumnTypeFamily.DATETIME)
        if self.random.random() < 0.35:
            self._hit("YEAR")
            return Expr(f"YEAR({column.sql})", ColumnTypeFamily.INTEGER)
        return column

    def _json_expr(self, refs: List[TableRef]) -> Expr:
        column = self._column_or_literal(refs, {ColumnTypeFamily.JSON})
        choice = self.random.choice(["JSON_EXTRACT", "JSON_ARROW", "JSON_ARROW_UNQUOTE", "JSON_OBJECT"])
        self._hit(choice)
        if choice == "JSON_ARROW" and column.sql.startswith("t"):
            return Expr(f"{column.sql}->'$.k'", ColumnTypeFamily.JSON)
        if choice == "JSON_ARROW_UNQUOTE" and column.sql.startswith("t"):
            return Expr(f"{column.sql}->>'$.k'", ColumnTypeFamily.STRING)
        if choice == "JSON_OBJECT":
            return Expr(f"JSON_OBJECT('k', {self._string_literal()}, 'n', {self.random_int(0, 50)})", ColumnTypeFamily.JSON)
        return Expr(f"JSON_EXTRACT({column.sql}, '$.k')", ColumnTypeFamily.JSON)

    def _spatial_expr(self, refs: List[TableRef]) -> Expr:
        column = self._column_or_literal(refs, {ColumnTypeFamily.SPATIAL})
        choice = self.random.choice(["ST_ASTEXT", "ST_X", "ST_Y"])
        self._hit(choice)
        if choice == "ST_X":
            return Expr(f"ST_X({column.sql})", ColumnTypeFamily.FLOAT)
        if choice == "ST_Y":
            return Expr(f"ST_Y({column.sql})", ColumnTypeFamily.FLOAT)
        return Expr(f"ST_AsText({column.sql})", ColumnTypeFamily.STRING)

    def _binary_expr(self, refs: List[TableRef]) -> Expr:
        column = self._column_or_literal(refs, {ColumnTypeFamily.BINARY})
        choice = self.random.choice(["HEX", "LENGTH"])
        self._hit(choice)
        if choice == "LENGTH":
            return Expr(f"LENGTH({column.sql})", ColumnTypeFamily.INTEGER)
        return Expr(f"HEX({column.sql})", ColumnTypeFamily.STRING)

    def _vector_order_clause(self, refs: List[TableRef], require_vector: bool, group_enabled: bool) -> str:
        if group_enabled or (not require_vector and self.random.random() >= 0.08):
            return ""
        distance = self._vector_distance_expr(refs)
        if not distance:
            return ""
        self._hit("ORDER BY ASC")
        self._hit("LIMIT")
        return f"ORDER BY {distance} ASC"

    def _vector_distance_expr(self, refs: List[TableRef]) -> Optional[str]:
        candidates: List[tuple[TableRef, ColumnMetadata]] = []
        for ref in refs:
            for column in ref.table.columns.values():
                if column.type_family is ColumnTypeFamily.VECTOR:
                    candidates.append((ref, column))
        if not candidates:
            return None
        ref, column = self.random.choice(candidates)
        metric = self.random.choice(["COSINE", "EUCLIDEAN"])
        self._hit(f"VEC_DISTANCE_{metric}")
        self._hit("VEC_FROMTEXT")
        return f"VEC_DISTANCE_{metric}({ref.alias}.{_q(column.name)}, VEC_FROMTEXT('{self._vector_literal(column)}'))"

    def _vector_to_string_expr(self, refs: List[TableRef]) -> Optional[str]:
        candidates: List[tuple[TableRef, ColumnMetadata]] = []
        for ref in refs:
            for column in ref.table.columns.values():
                if column.type_family is ColumnTypeFamily.VECTOR:
                    candidates.append((ref, column))
        if not candidates:
            return None
        ref, column = self.random.choice(candidates)
        self._hit("VEC_TOTEXT")
        return f"VEC_TOTEXT({ref.alias}.{_q(column.name)})"

    def _risky_expr(self, refs: List[TableRef], depth: int) -> Expr:
        left = self._column_expr(refs)
        right = self._random_expr(refs, max(0, depth - 1))
        choice = self.random.choice(["CAST", "CONVERT", "+", "CASE WHEN"])
        self._hit(choice)
        if choice == "CAST":
            return Expr(f"CAST({left.sql} AS CHAR)", ColumnTypeFamily.STRING)
        if choice == "CONVERT":
            return Expr(f"CONVERT({left.sql}, CHAR)", ColumnTypeFamily.STRING)
        if choice == "CASE WHEN":
            return Expr(f"CASE WHEN {left.sql} IS NULL THEN {right.sql} ELSE {left.sql} END", left.family)
        return Expr(f"({left.sql} + {right.sql})", ColumnTypeFamily.DECIMAL)

    def _aggregate_expr(self, refs: List[TableRef]) -> str:
        column = self._column_expr(refs, preferred={ColumnTypeFamily.INTEGER, ColumnTypeFamily.DECIMAL, ColumnTypeFamily.FLOAT})
        function = self.random.choice(["COUNT", "SUM", "AVG", "MIN", "MAX", "GROUP_CONCAT"])
        self._hit(function)
        if function == "COUNT":
            return "COUNT(*)"
        if function == "GROUP_CONCAT":
            text = self._column_expr(refs, preferred={ColumnTypeFamily.STRING, ColumnTypeFamily.ENUM, ColumnTypeFamily.SET}).sql
            return f"GROUP_CONCAT({text})"
        return f"{function}({column.sql})"

    def _aggregate_expr_from_name(self) -> str:
        function = self.random.choice(["COUNT", "SUM", "AVG", "MIN", "MAX"])
        self._hit(function)
        return "COUNT(*)" if function == "COUNT" else f"{function}(1)"

    def _column_expr(self, refs: List[TableRef], preferred: Optional[Set[ColumnTypeFamily]] = None) -> Expr:
        candidates: List[tuple[TableRef, ColumnMetadata]] = []
        for ref in refs:
            for column in ref.table.columns.values():
                if column.type_family is ColumnTypeFamily.VECTOR and preferred is None:
                    continue
                if preferred is None or column.type_family in preferred:
                    candidates.append((ref, column))
        if not candidates:
            return self._column_expr(refs)
        ref, column = self.random.choice(candidates)
        return Expr(f"{ref.alias}.{_q(column.name)}", column.type_family)

    def _column_or_literal(self, refs: List[TableRef], families: Set[ColumnTypeFamily]) -> Expr:
        if self.random.random() < 0.7:
            candidates = [self._column_expr(refs, preferred=families)]
            if candidates[0].family in families:
                return candidates[0]
        family = self.random.choice(list(families))
        return self._literal_expr(family)

    def _literal_expr(self, family: ColumnTypeFamily) -> Expr:
        if family in {ColumnTypeFamily.INTEGER, ColumnTypeFamily.BIT}:
            return Expr(str(self.random_int(-100, 100)), ColumnTypeFamily.INTEGER)
        if family in {ColumnTypeFamily.DECIMAL, ColumnTypeFamily.FLOAT}:
            return Expr(f"{self.random.uniform(-100, 100):.3f}", family)
        if family in {ColumnTypeFamily.STRING, ColumnTypeFamily.ENUM, ColumnTypeFamily.SET}:
            return Expr(self._string_literal(), ColumnTypeFamily.STRING)
        if family is ColumnTypeFamily.DATETIME:
            return Expr(f"TIMESTAMP {self._datetime_literal()}", ColumnTypeFamily.DATETIME)
        if family is ColumnTypeFamily.JSON:
            self._hit("JSON_OBJECT")
            return Expr(f"JSON_OBJECT('k', {self._string_literal()}, 'n', {self.random_int(0, 99)})", ColumnTypeFamily.JSON)
        if family is ColumnTypeFamily.SPATIAL:
            return Expr(f"ST_GeomFromText('POINT({self.random_int(1, 80)} {self.random_int(1, 120)})', 4326)", ColumnTypeFamily.SPATIAL)
        if family is ColumnTypeFamily.BINARY:
            self._hit("UNHEX")
            return Expr(f"UNHEX('{self.random_int(0, 65535):04x}')", ColumnTypeFamily.BINARY)
        return Expr("NULL", ColumnTypeFamily.UNKNOWN)

    def _simple_subquery(self, tables: List[TableMetadata]) -> str:
        self._hit("SUBQUERY")
        table = self.random.choice(self._permanent_tables(tables))
        column = self._first_column(table, ColumnTypeFamily.INTEGER) or next(iter(table.columns.values()))
        return f"SELECT 1 FROM {_q(table.name)} AS sq WHERE sq.{_q(column.name)} IS NOT NULL LIMIT {self.random_int(1, 5)}"

    def _single_column_subquery(self, tables: List[TableMetadata]) -> str:
        self._hit("SUBQUERY")
        table = self.random.choice(self._permanent_tables(tables))
        column = self._first_column(table, ColumnTypeFamily.INTEGER) or next(iter(table.columns.values()))
        return f"SELECT sq.{_q(column.name)} FROM {_q(table.name)} AS sq"

    def _scalar_subquery(self, tables: List[TableMetadata]) -> str:
        self._hit("SUBQUERY")
        table = self.random.choice(self._permanent_tables(tables))
        column = self._first_column(table, ColumnTypeFamily.INTEGER) or next(iter(table.columns.values()))
        return f"SELECT COALESCE(MAX(sq.{_q(column.name)}), 0) FROM {_q(table.name)} AS sq"

    def _fallback_query(self, table: TableMetadata) -> str:
        self._hit("SELECT ALL")
        self._hit("FROM")
        return f"SELECT 1 FROM {_q(table.name)}"

    def _should_group(self, options: GenerationOptions) -> bool:
        if options.require_locking or options.require_window:
            return False
        return self.random.random() < 0.22

    def _common_columns(self, left: TableMetadata, right: TableMetadata) -> List[str]:
        return sorted(set(left.columns).intersection(right.columns))

    def _query_table_pool(self, tables: List[TableMetadata]) -> List[TableMetadata]:
        permanent = self._permanent_tables(tables)
        temporary = [table for table in tables if table.is_temporary]
        if not temporary or self.random.random() < 0.86:
            return permanent
        return permanent + [self.random.choice(temporary)]

    def _permanent_tables(self, tables: List[TableMetadata]) -> List[TableMetadata]:
        permanent = [table for table in tables if not table.is_temporary]
        return permanent or tables

    def _first_column(self, table: TableMetadata, family: ColumnTypeFamily) -> ColumnMetadata | None:
        for column in table.columns.values():
            if column.type_family is family:
                return column
        return None

    def _has_family(self, refs: List[TableRef], family: ColumnTypeFamily) -> bool:
        return any(column.type_family is family for ref in refs for column in ref.table.columns.values())

    def _compatible_column(self, table: TableMetadata, family: ColumnTypeFamily) -> ColumnMetadata | None:
        compatible = {
            ColumnTypeFamily.INTEGER: {ColumnTypeFamily.INTEGER, ColumnTypeFamily.BIT, ColumnTypeFamily.BOOLEAN},
            ColumnTypeFamily.DECIMAL: {ColumnTypeFamily.INTEGER, ColumnTypeFamily.DECIMAL, ColumnTypeFamily.FLOAT},
            ColumnTypeFamily.FLOAT: {ColumnTypeFamily.INTEGER, ColumnTypeFamily.DECIMAL, ColumnTypeFamily.FLOAT},
            ColumnTypeFamily.STRING: {ColumnTypeFamily.STRING, ColumnTypeFamily.ENUM, ColumnTypeFamily.SET},
        }.get(family, {family})
        for column in table.columns.values():
            if column.type_family in compatible:
                return column
        return None

    def _joinable_column(self, table: TableMetadata) -> ColumnMetadata:
        for family in [ColumnTypeFamily.INTEGER, ColumnTypeFamily.STRING, ColumnTypeFamily.DECIMAL, ColumnTypeFamily.FLOAT]:
            column = self._first_column(table, family)
            if column:
                return column
        return next(iter(table.columns.values()))

    def _vector_literal(self, column: ColumnMetadata) -> str:
        dimensions = column.vector_dimensions or 4
        values = [f"{self.random.random():.3f}" for _ in range(dimensions)]
        return "[" + ",".join(values) + "]"

    def _string_literal(self) -> str:
        value = "".join(self.random.choice("abcxyz0123456789_") for _ in range(self.random_int(1, 10)))
        return "'" + value.replace("'", "''") + "'"

    def _datetime_literal(self) -> str:
        month = self.random_int(1, 12)
        day = self.random_int(1, 28)
        hour = self.random_int(0, 23)
        minute = self.random_int(0, 59)
        second = self.random_int(0, 59)
        return f"'2026-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}'"

    def random_int(self, start: int, end: int) -> int:
        return self.random.randint(start, end)

    def _hit(self, name: str) -> None:
        if self.registry.has(name):
            self._attempt_hits.add(name)

    def _commit_hits(self) -> None:
        self.coverage_hits = set(self._attempt_hits)
        self.recent_hits = sorted(self._attempt_hits)
        for name in self._attempt_hits:
            self.coverage_counts[name] = self.coverage_counts.get(name, 0) + 1


def _q(identifier: str) -> str:
    return f"`{identifier.replace('`', '``')}`"
