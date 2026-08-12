"""基表 SQL 内存模型。"""

from __future__ import annotations

from dataclasses import dataclass

from select_fuzz.metadata.models import BaseSqlFile, TableMetadata


@dataclass(frozen=True)
class BaseSqlBundle:
    """保存一次加载并解析完成的有序基表 SQL。"""

    files: tuple[BaseSqlFile, ...]
    tables: tuple[TableMetadata, ...]
    expand_base_table_columns: bool = False
    generator_version: str | None = None
    seed: str | None = None
