#!/usr/bin/env python3
"""离线生成 MySQL/PolarDB 基表 DDL 与种子数据。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from select_fuzz.base_tables import (  # noqa: E402
    CURRENT_BASE_TABLE_GENERATOR_VERSION,
    generate_base_sql_bundle,
    generate_core_base_sql_bundle,
    normalize_base_table_seed,
)
from select_fuzz.base_tables import v1  # noqa: E402
from select_fuzz.base_tables.models import BaseSqlBundle  # noqa: E402


OUT_DIR = ROOT / "sql_base_tables"
TOP_PARTITION_VALUES = v1.TOP_PARTITION_VALUES
SUBPARTITION_VALUES = v1.SUBPARTITION_VALUES
TARGET_TOTAL_INDEX_COUNT = v1.TARGET_TOTAL_INDEX_COUNT
MIN_SEED_ROWS = v1.MIN_SEED_ROWS
MAX_SEED_ROWS = v1.MAX_SEED_ROWS
MIN_TABLE_COLUMNS = v1.MIN_TABLE_COLUMNS
MAX_TABLE_COLUMNS = v1.MAX_TABLE_COLUMNS
SEED_NUMBER_TABLE = v1.SEED_NUMBER_TABLE
FIRST_PARTITION_START = v1.FIRST_PARTITION_START
FIRST_PARTITION_COUNT = v1.FIRST_PARTITION_COUNT
SUBPARTITION_START = v1.SUBPARTITION_START
PARTITION_TYPES = v1.PARTITION_TYPES
SUBPARTITION_TABLE_COUNT = v1.SUBPARTITION_TABLE_COUNT
TOTAL_TABLE_COUNT = v1.TOTAL_TABLE_COUNT
TableColumnProfile = v1.TableColumnProfile
ExtraColumnSpec = v1.ExtraColumnSpec


def table_column_profile(index: int) -> TableColumnProfile:
    return v1.table_column_profile(index)


def table_column_count(index: int, *, seed: str = "0", expand_columns: bool = False) -> int:
    return v1.table_column_count(
        index,
        seed=seed,
        expand_base_table_columns=expand_columns,
    )


def extra_column_specs(index: int, *, seed: str = "0") -> list[ExtraColumnSpec]:
    return v1.extra_column_specs(index, seed=normalize_base_table_seed(seed))


def create_table_sql(
    index: int,
    include_subpartition: bool = True,
    *,
    expand_columns: bool = False,
    seed: str = "0",
) -> str:
    normalized_seed = normalize_base_table_seed(seed) if expand_columns else "0"
    return v1.create_table_sql(
        index,
        include_subpartition=include_subpartition,
        seed=normalized_seed,
        expand_base_table_columns=expand_columns,
    )


def base_seed_columns() -> list[str]:
    return v1.base_seed_columns()


def seed_columns(index: int, *, expand_columns: bool = False, seed: str = "0") -> list[str]:
    normalized_seed = normalize_base_table_seed(seed) if expand_columns else "0"
    return v1.seed_columns(
        index,
        seed=normalized_seed,
        expand_base_table_columns=expand_columns,
    )


def seed_value_exprs(index: int, *, expand_columns: bool = False, seed: str = "0") -> list[str]:
    normalized_seed = normalize_base_table_seed(seed) if expand_columns else "0"
    return v1.seed_value_exprs(
        index,
        seed=normalized_seed,
        expand_base_table_columns=expand_columns,
    )


def seed_row_count(index: int) -> int:
    return v1.seed_row_count(index)


def seed_number_table_sql() -> str:
    return v1.seed_number_table_sql()


def insert_sql(index: int, *, expand_columns: bool = False, seed: str = "0") -> str:
    normalized_seed = normalize_base_table_seed(seed) if expand_columns else "0"
    return v1.insert_sql(
        index,
        seed=normalized_seed,
        expand_base_table_columns=expand_columns,
    )


def seed_sql(*, expand_columns: bool = False, seed: str = "0") -> str:
    normalized_seed = normalize_base_table_seed(seed) if expand_columns else "0"
    return v1.seed_sql(
        seed=normalized_seed,
        expand_base_table_columns=expand_columns,
    )


def execution_doc(
    include_subpartition: bool = True,
    *,
    expand_columns: bool = False,
    seed: str = "0",
) -> str:
    normalized_seed = normalize_base_table_seed(seed) if expand_columns else "0"
    return v1.execution_doc(
        include_subpartition=include_subpartition,
        expand_base_table_columns=expand_columns,
        seed=normalized_seed,
    )


def core_bundle() -> BaseSqlBundle:
    """返回默认核心包，供既有离线调用者和测试复用。"""

    return generate_core_base_sql_bundle()


def _managed_sql_names() -> set[str]:
    return {*(f"t{index}.sql" for index in range(TOTAL_TABLE_COUNT)), "zz_seed_fk_data.sql"}


def _assert_no_unknown_sql(output_dir: Path) -> None:
    unknown = sorted(path.name for path in output_dir.glob("*.sql") if path.name not in _managed_sql_names())
    if unknown:
        names = "、".join(unknown)
        raise ValueError(f"输出目录包含未知 SQL 文件，拒绝自动清理或覆盖：{names}")


def generate_files(
    output_dir: Path,
    include_subpartition: bool = True,
    *,
    expand_columns: bool = False,
    generator_version: str = CURRENT_BASE_TABLE_GENERATOR_VERSION,
    seed: str = "0",
) -> None:
    """离线写入生成结果；未知 SQL 文件不会被删除。"""

    if expand_columns:
        normalized_seed = normalize_base_table_seed(seed)
        if generator_version != CURRENT_BASE_TABLE_GENERATOR_VERSION:
            # 由注册表产生统一的中文未知版本错误。
            generate_base_sql_bundle(generator_version, normalized_seed)
        if include_subpartition:
            files = generate_base_sql_bundle(generator_version, normalized_seed).files
        else:
            files = v1.render_sql_files(
                normalized_seed,
                expand_base_table_columns=True,
                include_subpartition=False,
            )
    else:
        normalized_seed = "0"
        if include_subpartition:
            files = generate_core_base_sql_bundle().files
        else:
            files = v1.render_sql_files(
                normalized_seed,
                expand_base_table_columns=False,
                include_subpartition=False,
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    _assert_no_unknown_sql(output_dir)
    for sql_file in files:
        (output_dir / sql_file.path.name).write_bytes(sql_file.sql.encode("utf-8"))
    (output_dir / "执行顺序说明.md").write_bytes(
        execution_doc(
            include_subpartition=include_subpartition,
            expand_columns=expand_columns,
            seed=normalized_seed,
        ).encode("utf-8")
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 MySQL/PolarDB 基表 DDL 与种子数据")
    parser.add_argument("--output-dir", type=Path, default=OUT_DIR, help="输出目录，默认写入 sql_base_tables")
    parser.add_argument("--without-subpartition", action="store_true", help="将二级分区表降级为一级分区表；此兼容变体不属于标准 v1 金标")
    parser.add_argument("--expand-columns", action="store_true", help="显式生成每表 200 到 500 列的扩展模式")
    parser.add_argument("--generator-version", help="扩展模式生成器版本，例如 v1")
    parser.add_argument("--seed", help="扩展模式规范 uint64 十进制种子")
    args = parser.parse_args()
    if args.expand_columns:
        if args.generator_version is None or args.seed is None:
            parser.error("扩展列模式必须同时指定 --generator-version 和 --seed")
    elif args.generator_version is not None or args.seed is not None:
        parser.error("--generator-version 和 --seed 只能与 --expand-columns 一起使用")
    return args


def main() -> None:
    args = parse_args()
    generate_files(
        args.output_dir,
        include_subpartition=not args.without_subpartition,
        expand_columns=args.expand_columns,
        generator_version=args.generator_version or CURRENT_BASE_TABLE_GENERATOR_VERSION,
        seed=args.seed or "0",
    )


def __getattr__(name: str) -> object:
    """兼容重导出仍由冻结 `v1` 实现提供的旧 helper。"""

    try:
        return getattr(v1, name)
    except AttributeError as exc:
        raise AttributeError(f"模块 {__name__!r} 没有属性 {name!r}") from exc


if __name__ == "__main__":
    main()
