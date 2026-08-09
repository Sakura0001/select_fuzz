#!/usr/bin/env python3
"""校验生成的基表 DDL 是否覆盖当前要求。"""

from __future__ import annotations

import re
import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from select_fuzz.metadata.ddl_parser import parse_create_table

SQL_DIR = ROOT / "sql_base_tables"
EXECUTION_DOC = SQL_DIR / "执行顺序说明.md"
TOP_PARTITION_VALUES = list(range(1, 9))
SUBPARTITION_VALUES = list(range(1, 9))
TARGET_TOTAL_INDEX_COUNT = 61
MIN_SEED_ROWS = 10
MAX_SEED_ROWS = 100
MIN_TABLE_COLUMNS = 200
MAX_TABLE_COLUMNS = 500
SEED_NUMBER_TABLE = "_select_fuzz_seed_numbers"
FIRST_PARTITION_START = 7
FIRST_PARTITION_COUNT = 8
SUBPARTITION_START = FIRST_PARTITION_START + FIRST_PARTITION_COUNT
PARTITION_TYPES = [
    "RANGE",
    "RANGE COLUMNS",
    "LIST",
    "LIST COLUMNS",
    "HASH",
    "LINEAR HASH",
    "KEY",
    "LINEAR KEY",
]
SUBPARTITION_TABLE_COUNT = len(PARTITION_TYPES) * len(PARTITION_TYPES)
TOTAL_TABLE_COUNT = SUBPARTITION_START + SUBPARTITION_TABLE_COUNT
PARTITION_TABLES = set(range(FIRST_PARTITION_START, TOTAL_TABLE_COUNT))
TENANT_COVERAGE_EXPR = f"((`n` - 1) % {len(TOP_PARTITION_VALUES)}) + 1"
SUBPARTITION_COVERAGE_EXPR = f"((`n` - 1) % {len(SUBPARTITION_VALUES)}) + 1"
UNSUPPORTED_GEOMETRY_PATTERN = re.compile(
    r"\b(?:GEOMETRY|POINT|LINESTRING|POLYGON|MULTIPOINT|MULTILINESTRING|MULTIPOLYGON|GEOMETRYCOLLECTION)\b"
    r"|\bSPATIAL\s+KEY\b"
    r"|\bSRID\b"
    r"|\bST_GeomFromText\b",
    flags=re.I,
)


def fail(message: str, errors: list[str]) -> None:
    errors.append(message)


def read_table(sql_dir: Path, index: int, errors: list[str]) -> str:
    path = sql_dir / f"t{index}.sql"
    if not path.exists():
        fail(f"缺少 {path.name}", errors)
        return ""
    return path.read_text(encoding="utf-8")


def first_level_partition_count(sql: str) -> int:
    hash_or_key = re.search(r"^PARTITION BY (?:LINEAR\s+)?(?:HASH|KEY) \(`tenant_id`\) PARTITIONS (\d+)", sql, flags=re.I | re.M)
    if hash_or_key:
        return int(hash_or_key.group(1))
    return len(re.findall(r"^\s{2}PARTITION p\d+(?:\s|,|$)", sql, flags=re.I | re.M))


def partition_type(sql: str) -> str:
    match = re.search(
        r"^PARTITION BY (?P<type>(?:LINEAR\s+)?(?:RANGE|LIST|HASH|KEY)(?:\s+COLUMNS)?)\s+\(`tenant_id`\)",
        sql,
        flags=re.I | re.M,
    )
    return " ".join(match.group("type").upper().split()) if match else ""


def subpartition_type(sql: str) -> str:
    match = re.search(
        r"^SUBPARTITION BY (?P<type>(?:LINEAR\s+)?(?:RANGE|LIST|HASH|KEY)(?:\s+COLUMNS)?)\s+\(`subpart_id`\)",
        sql,
        flags=re.I | re.M,
    )
    return " ".join(match.group("type").upper().split()) if match else ""


def assert_explicit_range_or_list_subpartitions(sql: str, index: int, errors: list[str]) -> None:
    sub_type = subpartition_type(sql)
    if not sub_type.startswith(("RANGE", "LIST")):
        return
    expected_fragments = (
        [f"SUBPARTITION p0sp{idx} VALUES LESS THAN ({value + 1})" for idx, value in enumerate(SUBPARTITION_VALUES[:-1])]
        + [f"SUBPARTITION p0sp{len(SUBPARTITION_VALUES) - 1} VALUES LESS THAN (MAXVALUE)"]
        if sub_type.startswith("RANGE")
        else [f"SUBPARTITION p0sp{idx} VALUES IN ({value})" for idx, value in enumerate(SUBPARTITION_VALUES)]
    )
    for fragment in expected_fragments:
        if fragment not in sql:
            fail(f"t{index}.sql 缺少显式子分区定义：{fragment}", errors)


def sql_secondary_index_count(sql: str) -> int:
    return len(re.findall(r"^\s+(?:UNIQUE\s+KEY|KEY|SPATIAL\s+KEY)\s+`", sql, flags=re.I | re.M))


def assert_no_unsupported_geometry(sql: str, label: str, errors: list[str]) -> None:
    match = UNSUPPORTED_GEOMETRY_PATTERN.search(sql)
    if match:
        fail(f"{label} 不应包含目标引擎不支持的 GEOMETRY/空间索引内容：{match.group(0)}", errors)


def unique_key_lines(sql: str) -> list[str]:
    return re.findall(r"^\s+UNIQUE\s+KEY\s+`[^`]+`.*$", sql, flags=re.I | re.M)


def assert_partition_unique_keys_include_columns(
    sql: str,
    index: int,
    required_columns: list[str],
    errors: list[str],
) -> None:
    for line in unique_key_lines(sql):
        for column in required_columns:
            if f"`{column}`" not in line:
                fail(f"t{index}.sql 分区唯一键缺少分区列 `{column}`：{line.strip()}", errors)


def seed_insert_block(seed_sql: str, index: int) -> str:
    match = re.search(
        rf"/\* t{index}:rows=\d+ \*/\s*INSERT INTO `t{index}` .*?(?=\n/\* t\d+:rows=\d+ \*/|\Z)",
        seed_sql,
        flags=re.I | re.S,
    )
    return match.group(0) if match else ""


def seed_select_prefix_matches(block: str, values: list[str]) -> bool:
    pattern = r"SELECT\s+" + r",\s*".join(re.escape(value) for value in values) + r"\s*,"
    return re.search(pattern, block, flags=re.I) is not None


def assert_seed_partition_coverage(seed_sql: str, errors: list[str]) -> None:
    for index in range(FIRST_PARTITION_START, TOTAL_TABLE_COUNT):
        block = seed_insert_block(seed_sql, index)
        if not block:
            continue
        if not seed_select_prefix_matches(block, ["`n`", TENANT_COVERAGE_EXPR]):
            fail(f"t{index} 种子数据 tenant_id 未覆盖 {TOP_PARTITION_VALUES}", errors)
    for index in range(SUBPARTITION_START, TOTAL_TABLE_COUNT):
        block = seed_insert_block(seed_sql, index)
        if not block:
            continue
        if not seed_select_prefix_matches(block, ["`n`", TENANT_COVERAGE_EXPR, SUBPARTITION_COVERAGE_EXPR]):
            fail(f"t{index} 种子数据 subpart_id 未覆盖 {SUBPARTITION_VALUES}", errors)


def main(sql_dir: Path = SQL_DIR, include_subpartition: bool = True) -> int:
    errors: list[str] = []
    files = sorted(path.name for path in sql_dir.glob("t*.sql"))
    if len(files) != TOTAL_TABLE_COUNT:
        fail(f"期望 {TOTAL_TABLE_COUNT} 个 tN.sql 文件，实际 {len(files)} 个", errors)

    type_counts = {"normal": 0, "temporary": 0, "partition": 0, "subpartition": 0}
    all_sql_parts: list[str] = []
    column_counts: list[int] = []
    first_partition_types: set[str] = set()
    subpartition_pairs: set[tuple[str, str]] = set()
    for index in range(TOTAL_TABLE_COUNT):
        sql = read_table(sql_dir, index, errors)
        if not sql:
            continue
        all_sql_parts.append(sql)
        table_metadata = parse_create_table(sql)
        column_count = len(table_metadata.columns)
        column_counts.append(column_count)
        if not MIN_TABLE_COLUMNS <= column_count <= MAX_TABLE_COLUMNS:
            fail(f"t{index}.sql 列数应在 {MIN_TABLE_COLUMNS} 到 {MAX_TABLE_COLUMNS} 之间，实际 {column_count}", errors)
        if f"`extra_t{index}_000`" not in sql:
            fail(f"t{index}.sql 缺少随机扩展列", errors)
        create_pattern = rf"^CREATE\s+(?:TEMPORARY\s+)?TABLE\s+`t{index}`"
        if len(re.findall(create_pattern, sql, flags=re.I | re.M)) != 1:
            fail(f"t{index}.sql 没有且仅有一个匹配表名的 CREATE TABLE", errors)
        if re.search(r"\bVECTOR\s*\(|imci_vector_index=|\bVEC_", sql, flags=re.I):
            fail(f"t{index}.sql 不应包含向量列、向量索引备注或向量函数", errors)
        effective_index_count = sql_secondary_index_count(sql)
        if effective_index_count != TARGET_TOTAL_INDEX_COUNT:
            fail(f"t{index}.sql 索引数量应为 {TARGET_TOTAL_INDEX_COUNT}，实际 {effective_index_count}", errors)
        if "COMMENT='COLUMNAR=1'" in sql:
            fail(f"t{index}.sql 不应带向量索引所需的表级 COLUMNAR=1", errors)
        if "SET FOREIGN_KEY_CHECKS=0;" not in sql or "SET FOREIGN_KEY_CHECKS=1;" not in sql:
            fail(f"t{index}.sql 缺少外键检查开关", errors)
        if "SET transaction_isolation = 'READ-COMMITTED';" not in sql:
            fail(f"t{index}.sql 缺少 READ COMMITTED 隔离级别设置", errors)
        if index in {0, 2} and "UNIQUE KEY `idx_t" not in sql:
            fail(f"t{index}.sql 普通或临时表应尽量将安全索引转换为 UNIQUE KEY", errors)
        if index == 0:
            if "CONSTRAINT `fk_t0_" in sql:
                fail("t0.sql 不应包含父表外键", errors)
        elif index == 1:
            if sql.count("FOREIGN KEY") < 2:
                fail("t1.sql 外键数量少于 2 个", errors)
            references = set(re.findall(r"REFERENCES\s+`t(\d+)`", sql, flags=re.I))
            if any(int(ref) >= 2 for ref in references):
                fail(f"t1.sql 不应引用临时表或分区表作为外键父表：{sorted(references)}", errors)
        elif 2 <= index <= 6:
            if "FOREIGN KEY" in sql.upper():
                fail(f"t{index}.sql 临时表不应声明 FOREIGN KEY，避免 InnoDB 1215", errors)
        else:
            if "FOREIGN KEY" in sql.upper():
                fail(f"t{index}.sql 分区表不应声明 FOREIGN KEY，避免 InnoDB 1506", errors)

        upper_sql = sql.upper()
        if index <= 1:
            type_counts["normal"] += 1
            if "CREATE TEMPORARY TABLE" in upper_sql or "PARTITION BY" in upper_sql:
                fail(f"t{index}.sql 应为普通表", errors)
        elif index <= 6:
            type_counts["temporary"] += 1
            if "CREATE TEMPORARY TABLE" not in upper_sql:
                fail(f"t{index}.sql 应为临时表", errors)
            if "FOREIGN KEY" in upper_sql:
                fail(f"t{index}.sql 临时表不应声明 FOREIGN KEY，避免 InnoDB 1215", errors)
        elif index < SUBPARTITION_START:
            type_counts["partition"] += 1
            if "PARTITION BY" not in upper_sql or "SUBPARTITION BY" in upper_sql:
                fail(f"t{index}.sql 应为一级分区表", errors)
            first_partition_types.add(partition_type(sql))
            if first_level_partition_count(sql) != 8:
                fail(f"t{index}.sql 一级分区数量应为 8 个", errors)
            if "FOREIGN KEY" in upper_sql:
                fail(f"t{index}.sql 一级分区表不应声明 FOREIGN KEY，避免 InnoDB 1506", errors)
            assert_partition_unique_keys_include_columns(sql, index, ["tenant_id"], errors)
        else:
            if include_subpartition:
                type_counts["subpartition"] += 1
                if "SUBPARTITION BY" not in upper_sql:
                    fail(f"t{index}.sql 应为二级分区表", errors)
                subpartition_pairs.add((partition_type(sql), subpartition_type(sql)))
                assert_explicit_range_or_list_subpartitions(sql, index, errors)
                assert_partition_unique_keys_include_columns(sql, index, ["tenant_id", "subpart_id"], errors)
            else:
                type_counts["partition"] += 1
                if "PARTITION BY" not in upper_sql or "SUBPARTITION BY" in upper_sql:
                    fail(f"t{index}.sql 兼容输出应将二级分区降级为一级分区表", errors)
                assert_partition_unique_keys_include_columns(sql, index, ["tenant_id"], errors)
            if first_level_partition_count(sql) != 8:
                fail(f"t{index}.sql 一级分区数量应为 8 个", errors)
            if "FOREIGN KEY" in upper_sql:
                fail(f"t{index}.sql 二级分区表不应声明 FOREIGN KEY，避免 InnoDB 1506", errors)

    expected_counts = (
        {"normal": 2, "temporary": 5, "partition": FIRST_PARTITION_COUNT, "subpartition": SUBPARTITION_TABLE_COUNT}
        if include_subpartition
        else {"normal": 2, "temporary": 5, "partition": FIRST_PARTITION_COUNT + SUBPARTITION_TABLE_COUNT, "subpartition": 0}
    )
    if type_counts != expected_counts:
        fail(f"表类型分布不匹配：{type_counts}", errors)
    if first_partition_types != set(PARTITION_TYPES):
        fail(f"一级分区类型覆盖不完整：{sorted(first_partition_types)}", errors)
    if include_subpartition and subpartition_pairs != {(outer, inner) for outer in PARTITION_TYPES for inner in PARTITION_TYPES}:
        fail(f"二级分区组合覆盖不完整：{sorted(subpartition_pairs)}", errors)

    all_sql = "\n".join(all_sql_parts)
    assert_no_unsupported_geometry(all_sql, "基表目录", errors)
    if re.search(r"\bFULLTEXT\b", all_sql, re.I):
        fail("基表目录不应包含 FULLTEXT 索引", errors)
    if column_counts:
        if min(column_counts) != MIN_TABLE_COLUMNS or max(column_counts) != MAX_TABLE_COLUMNS:
            fail(f"基表列数应覆盖 {MIN_TABLE_COLUMNS} 到 {MAX_TABLE_COLUMNS}，实际 {min(column_counts)} 到 {max(column_counts)}", errors)
        if len(set(column_counts)) < 20:
            fail("基表列数随机性不足，不同列数少于 20 种", errors)
    char_lengths = [int(value) for value in re.findall(r"`char_col`\s+char\((\d+)\)", all_sql, flags=re.I)]
    varchar_lengths = [int(value) for value in re.findall(r"`varchar_col`\s+varchar\((\d+)\)", all_sql, flags=re.I)]
    if len(char_lengths) != TOTAL_TABLE_COUNT or min(char_lengths) != 1 or max(char_lengths) != 255:
        fail("char_col 应按表随机覆盖 char(1) 到 char(255)", errors)
    if len(varchar_lengths) != TOTAL_TABLE_COUNT or min(varchar_lengths) != 1 or max(varchar_lengths) != 255:
        fail("varchar_col 应按表随机覆盖 varchar(1) 到 varchar(255)", errors)
    for index in range(TOTAL_TABLE_COUNT):
        sql = read_table(sql_dir, index, errors)
        varchar_match = re.search(r"`varchar_col`\s+varchar\((\d+)\)", sql, flags=re.I)
        prefix_match = re.search(r"`idx_t\d+_varchar_prefix`\s+\(`varchar_col`\((\d+)\)\)", sql, flags=re.I)
        if varchar_match and prefix_match and int(prefix_match.group(1)) > int(varchar_match.group(1)):
            fail(f"t{index}.sql varchar 前缀索引长度超过列长度", errors)
    required_fragments = [
        "INVISIBLE",
        " DESC",
        "lower(`varchar_col`)",
        "year(`datetime_col`)",
        "`unsigned_int_col` + `smallint_col`",
        "json_extract(`json_col`",
        "`blob_col`(32)",
        "ON DELETE CASCADE",
        "ON DELETE SET NULL",
    ]
    for fragment in required_fragments:
        if fragment not in all_sql:
            fail(f"缺少覆盖项：{fragment}", errors)

    seed_path = sql_dir / "zz_seed_fk_data.sql"
    if not seed_path.exists():
        fail("缺少 zz_seed_fk_data.sql", errors)
    else:
        seed_sql = seed_path.read_text(encoding="utf-8")
        assert_no_unsupported_geometry(seed_sql, "基表种子数据", errors)
        if "SET transaction_isolation = 'READ-COMMITTED';" not in seed_sql:
            fail("种子数据脚本缺少 READ COMMITTED 隔离级别设置", errors)
        if f"CREATE TABLE `{SEED_NUMBER_TABLE}`" not in seed_sql:
            fail(f"种子数据脚本缺少 {SEED_NUMBER_TABLE} 辅助数字表", errors)
        if seed_sql.upper().count("INSERT INTO") != TOTAL_TABLE_COUNT + 1:
            fail(f"种子数据脚本应包含 1 条数字表 INSERT 和 {TOTAL_TABLE_COUNT} 条基表 INSERT SELECT", errors)
        seed_markers = {int(index): int(count) for index, count in re.findall(r"/\* t(\d+):rows=(\d+) \*/", seed_sql)}
        if set(seed_markers) != set(range(TOTAL_TABLE_COUNT)):
            fail("种子数据脚本应为每张表写入 rows 标记", errors)
        for index, count in seed_markers.items():
            if not MIN_SEED_ROWS <= count <= MAX_SEED_ROWS:
                fail(f"t{index} 种子数据行数应在 {MIN_SEED_ROWS} 到 {MAX_SEED_ROWS} 之间，实际 {count}", errors)
            if f"WHERE `n` <= {count}" not in seed_sql:
                fail(f"t{index} 种子数据缺少按行数过滤的 INSERT SELECT", errors)
        assert_seed_partition_coverage(seed_sql, errors)
        if re.search(r"\b(?:VECTOR|VEC_FROMTEXT|STRING_TO_VECTOR|VECTOR_TO_STRING|DISTANCE)\s*\(|\bVEC_DISTANCE_\w+\b|'DOT'", seed_sql, re.I):
            fail("种子数据脚本不应包含向量类型、向量函数、DISTANCE 第三参数语法或 DOT 距离", errors)
        if re.search(r"\b(?:nan|inf)\b", seed_sql, re.I):
            fail("种子数据不应包含 NaN 或 Inf", errors)
        for index in range(TOTAL_TABLE_COUNT):
            if f"INSERT INTO `t{index}`" not in seed_sql:
                fail(f"种子数据缺少 t{index}", errors)

    execution_doc = sql_dir / "执行顺序说明.md"
    if not execution_doc.exists():
        fail("缺少执行顺序说明.md", errors)
    else:
        doc = execution_doc.read_text(encoding="utf-8")
        for name in ["t0.sql", "t1.sql", *[f"t{index}.sql" for index in range(2, TOTAL_TABLE_COUNT)], "zz_seed_fk_data.sql"]:
            if name not in doc:
                fail(f"执行顺序说明缺少 {name}", errors)
        doc_fragments = ["READ-COMMITTED", "10", "100", "200", "500", "UNIQUE KEY"]
        for fragment in doc_fragments:
            if fragment not in doc:
                fail(f"执行顺序说明缺少约束说明：{fragment}", errors)
        for fragment in ["64 个二级索引", "61 个索引", "PRIMARY KEY"]:
            if fragment not in doc:
                fail(f"执行顺序说明缺少索引上限说明：{fragment}", errors)
        for fragment in ["临时表", "分区表", "不声明 `FOREIGN KEY`", "1215"]:
            if fragment not in doc:
                fail(f"执行顺序说明缺少表外键限制说明：{fragment}", errors)

    if errors:
        print("\n".join(errors))
        return 1
    print(f"结构验证通过：{TOTAL_TABLE_COUNT} 张基表、每表 200 到 500 列、8 种一级分区、64 种二级分区组合、随机列类型、分区种子数据、无向量约束、外键、索引类型和执行顺序文档均满足要求。")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="校验生成的基表 DDL 是否覆盖当前要求")
    parser.add_argument("--sql-dir", type=Path, default=SQL_DIR, help="待校验的 SQL 目录")
    parser.add_argument("--without-subpartition", action="store_true", help="按不含二级分区的兼容输出校验")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(
        main(
            sql_dir=args.sql_dir,
            include_subpartition=not args.without_subpartition,
        )
    )
