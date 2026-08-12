"""加载并预校验基表 SQL 内存包。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from select_fuzz.metadata.base_sql import is_base_table_definition, load_base_sql_files
from select_fuzz.metadata.ddl_parser import parse_create_table
from select_fuzz.metadata.models import BaseSqlFile

from .models import BaseSqlBundle


def build_base_sql_bundle(
    files: Iterable[BaseSqlFile],
    *,
    expand_base_table_columns: bool = False,
    generator_version: str | None = None,
    seed: str | None = None,
) -> BaseSqlBundle:
    """按输入顺序构建内存包，并预先解析其中可用的基表。"""

    ordered_files = tuple(files)
    tables = []
    for sql_file in ordered_files:
        if not is_base_table_definition(sql_file):
            continue
        try:
            tables.append(parse_create_table(sql_file.sql))
        except ValueError:
            continue

    if not tables:
        raise RuntimeError("至少需要一张可解析的基表")

    return BaseSqlBundle(
        files=ordered_files,
        tables=tuple(tables),
        expand_base_table_columns=expand_base_table_columns,
        generator_version=generator_version,
        seed=seed,
    )


def load_base_sql_bundle(base_dir: Path | str) -> BaseSqlBundle:
    """从目录加载基表 SQL，并构建已经预校验的内存包。"""

    return build_base_sql_bundle(load_base_sql_files(base_dir))
