"""内置 v1 永久基表的有界随机 DML 生成器。"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from select_fuzz.base_tables import v1
from select_fuzz.metadata.models import ColumnMetadata, ColumnTypeFamily, TableMetadata

from .seeds import CURRENT_CRUD_GENERATOR_VERSION, normalize_uint64_seed


class DMLOperation(str, Enum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


@dataclass(frozen=True)
class DMLPlan:
    """一次 DML 尝试；跳过计划没有 SQL，但保留原操作和请求行数。"""

    operation: DMLOperation
    requested_rows: int
    sql: Optional[str]
    skip_reason: Optional[str] = None

    @property
    def skipped(self) -> bool:
        return self.sql is None


def _v1_table_index(table: TableMetadata) -> Optional[int]:
    match = re.fullmatch(r"t(\d+)", table.name)
    if match is None:
        return None
    index = int(match.group(1))
    if not 0 <= index < v1.TOTAL_TABLE_COUNT:
        return None
    return index


def eligible_v1_permanent_tables(tables: Sequence[TableMetadata]) -> tuple[TableMetadata, ...]:
    """保留 v1 的 74 张永久表，并按调用方原始顺序返回。"""

    eligible = []
    for table in tables:
        index = _v1_table_index(table)
        if index is None or table.is_temporary or v1.table_kind(index) == "temporary":
            continue
        eligible.append(table)
    return tuple(eligible)


class DMLGenerator:
    """使用独立随机序列为单个 v1 永久表生成有界 DML。"""

    def __init__(self, random_seed: int | None = None, *, base_table_seed: str = "0") -> None:
        self.random = random.Random(random_seed)
        self.base_table_seed = normalize_uint64_seed(base_table_seed)
        self.generator_version = CURRENT_CRUD_GENERATOR_VERSION
        self._insert_value_cache: dict[tuple[int, tuple[str, ...]], Optional[tuple[str, ...]]] = {}

    def generate(self, table: TableMetadata, estimated_rows: int) -> DMLPlan:
        table_index = _v1_table_index(table)
        if (
            table_index is None
            or table.is_temporary
            or v1.table_kind(table_index) == "temporary"
        ):
            raise ValueError("DML 生成器只支持内置 v1 永久表")
        if type(estimated_rows) is not int or estimated_rows < 0:
            raise ValueError("估算行数必须是非负整数")

        if estimated_rows <= 10:
            operation = DMLOperation.INSERT
        elif estimated_rows >= 200:
            operation = DMLOperation.DELETE
        else:
            operation = self.random.choice(tuple(DMLOperation))
        requested_rows = self.random.randint(1, 10)

        if operation is DMLOperation.INSERT:
            return self._generate_insert(table, table_index, requested_rows)
        if operation is DMLOperation.UPDATE:
            return self._generate_update(table, requested_rows)
        return self._generate_delete(table, requested_rows)

    def _generate_insert(self, table: TableMetadata, table_index: int, requested_rows: int) -> DMLPlan:
        actual_columns = tuple(table.columns)
        value_exprs = self._insert_value_exprs(table_index, actual_columns)
        if value_exprs is None:
            return DMLPlan(
                operation=DMLOperation.INSERT,
                requested_rows=requested_rows,
                sql=None,
                skip_reason="表结构与内置 v1 种子列不匹配",
            )

        # v1 的 smallint/date 等表达式按较小 n 设计；把批次限制在安全区间，
        # 同时避开初始化使用的 1～100，约束冲突仍按压力测试语义由 worker 统计。
        batch_offset = self.random.randint(1_000, 30_000 - requested_rows)
        columns_sql = ", ".join(_q(column) for column in actual_columns)
        values_sql = ",\n  ".join(value_exprs)
        sql = (
            f"INSERT INTO {_q(table.name)} ({columns_sql})\n"
            "SELECT\n"
            f"  {values_sql}\n"
            "FROM (\n"
            f"  SELECT `n` + {batch_offset} AS `n`\n"
            f"  FROM `{v1.SEED_NUMBER_TABLE}`\n"
            f"  WHERE `n` BETWEEN 1 AND {requested_rows}\n"
            ") AS `_select_fuzz_insert_batch`"
        )
        return DMLPlan(DMLOperation.INSERT, requested_rows, sql)

    def _insert_value_exprs(
        self,
        table_index: int,
        actual_columns: tuple[str, ...],
    ) -> Optional[tuple[str, ...]]:
        cache_key = (table_index, actual_columns)
        if cache_key in self._insert_value_cache:
            return self._insert_value_cache[cache_key]

        core_columns = tuple(v1.seed_columns(table_index))
        if actual_columns == core_columns:
            result: Optional[tuple[str, ...]] = tuple(v1.seed_value_exprs(table_index))
        else:
            expanded_columns = tuple(
                v1.seed_columns(
                    table_index,
                    seed=self.base_table_seed,
                    expand_base_table_columns=True,
                )
            )
            if actual_columns == expanded_columns:
                result = tuple(
                    v1.seed_value_exprs(
                        table_index,
                        seed=self.base_table_seed,
                        expand_base_table_columns=True,
                    )
                )
            else:
                result = None
        self._insert_value_cache[cache_key] = result
        return result

    def _generate_update(self, table: TableMetadata, requested_rows: int) -> DMLPlan:
        candidates = self._update_candidates(table)
        if not candidates:
            return DMLPlan(
                operation=DMLOperation.UPDATE,
                requested_rows=requested_rows,
                sql=None,
                skip_reason="没有可安全更新的列",
            )
        column = self.random.choice(candidates)
        seed = self.random.randint(0, (1 << 31) - 1)
        quoted_column = _q(column.name)
        expression = self._update_expression(column, quoted_column)
        sql = (
            f"UPDATE {_q(table.name)} SET {quoted_column} = {expression} "
            f"ORDER BY RAND({seed}) LIMIT {requested_rows}"
        )
        return DMLPlan(DMLOperation.UPDATE, requested_rows, sql)

    def _generate_delete(self, table: TableMetadata, requested_rows: int) -> DMLPlan:
        seed = self.random.randint(0, (1 << 31) - 1)
        sql = f"DELETE FROM {_q(table.name)} ORDER BY RAND({seed}) LIMIT {requested_rows}"
        return DMLPlan(DMLOperation.DELETE, requested_rows, sql)

    def _update_candidates(self, table: TableMetadata) -> list[ColumnMetadata]:
        protected: set[str] = set()
        for index in table.indexes.values():
            if not (index.primary or index.unique):
                continue
            for fragment in index.columns:
                if fragment in table.columns:
                    protected.add(fragment)
                protected.update(
                    token
                    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", fragment)
                    if token in table.columns
                )
        protected.update(
            column
            for foreign_key in table.foreign_keys
            for column in foreign_key.child_columns
        )
        if table.partition is not None:
            expressions = [table.partition.partition_expr]
            if table.partition.subpartition_expr is not None:
                expressions.append(table.partition.subpartition_expr)
            for expression in expressions:
                protected.update(
                    token
                    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", expression)
                    if token in table.columns
                )

        supported = {
            ColumnTypeFamily.INTEGER,
            ColumnTypeFamily.FLOAT,
            ColumnTypeFamily.DECIMAL,
            ColumnTypeFamily.BOOLEAN,
            ColumnTypeFamily.DATETIME,
            ColumnTypeFamily.STRING,
            ColumnTypeFamily.BINARY,
            ColumnTypeFamily.ENUM,
            ColumnTypeFamily.SET,
            ColumnTypeFamily.BIT,
            ColumnTypeFamily.JSON,
        }
        candidates = [
            column
            for column in table.columns.values()
            if column.name not in protected
            and not column.generated
            and column.nullable
            and column.type_family in supported
        ]
        table_index = _v1_table_index(table)
        if table_index in {0, 1} and set(v1.base_seed_columns()).issubset(table.columns):
            # t0/t1 的许多 UNIQUE 前缀/函数索引无法由当前简化元数据完整还原。
            # 仅保留已知不参与 UNIQUE 的核心列，以及没有建索引的扩展列。
            safe_core = {"bool_col", "binary_col", "varbinary_col", "enum_col"}
            extra_prefix = f"extra_t{table_index}_"
            candidates = [
                column
                for column in candidates
                if column.name in safe_core or column.name.startswith(extra_prefix)
            ]
        return candidates

    def _update_expression(self, column: ColumnMetadata, quoted_column: str) -> str:
        family = column.type_family
        lower_type = column.sql_type.lower()
        if family in {ColumnTypeFamily.INTEGER, ColumnTypeFamily.FLOAT, ColumnTypeFamily.DECIMAL, ColumnTypeFamily.BOOLEAN}:
            value = "0"
        elif family is ColumnTypeFamily.BIT:
            value = "b'0'"
        elif family is ColumnTypeFamily.DATETIME:
            if lower_type.startswith("date"):
                value = "DATE '2026-01-01'"
            elif lower_type.startswith("time"):
                value = "TIME '00:00:00'"
            elif lower_type.startswith("year"):
                value = "2026"
            else:
                value = "TIMESTAMP '2026-01-01 00:00:00'"
        elif family is ColumnTypeFamily.BINARY:
            value = "X''"
        elif family is ColumnTypeFamily.ENUM:
            value = "1"
        elif family is ColumnTypeFamily.SET:
            value = "0"
        elif family is ColumnTypeFamily.JSON:
            value = "JSON_OBJECT()"
        else:
            value = "''"
        return f"IF({quoted_column} IS NULL, {value}, NULL)"


def _q(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


__all__ = [
    "DMLGenerator",
    "DMLOperation",
    "DMLPlan",
    "eligible_v1_permanent_tables",
]
