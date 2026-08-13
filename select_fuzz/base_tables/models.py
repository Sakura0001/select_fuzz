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

    def __post_init__(self) -> None:
        if self.expand_base_table_columns:
            if self.generator_version is None or self.seed is None:
                raise ValueError("扩展基表列时，生成器版本和种子不能为空")
        elif self.generator_version is not None or self.seed is not None:
            raise ValueError("未扩展基表列时，生成器版本和种子必须为空")
