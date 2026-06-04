#!/usr/bin/env python3
"""校验生成的基表 DDL 是否覆盖当前要求。"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "sql_base_tables"
NO_VECTOR_SQL_DIR = ROOT / "sql_base_tables_no_vector_subpartition"
EXECUTION_DOC = SQL_DIR / "执行顺序说明.md"
PARTITION_TABLES = set(range(7, 27))
TOP_PARTITION_VALUES = list(range(1, 9))
SUBPARTITION_VALUES = [1, 2]
TARGET_TOTAL_INDEX_COUNT = 61


def fail(message: str, errors: list[str]) -> None:
    errors.append(message)


def read_table(index: int, errors: list[str]) -> str:
    path = SQL_DIR / f"t{index}.sql"
    if not path.exists():
        fail(f"缺少 {path.name}", errors)
        return ""
    return path.read_text(encoding="utf-8")


def expected_seed_rows(index: int) -> set[tuple[int, int]]:
    if index <= 6:
        return {(1, 1)}
    if 7 <= index <= 10:
        return {(tenant_id, 1) for tenant_id in TOP_PARTITION_VALUES}
    return {
        (tenant_id, subpart_id)
        for tenant_id in TOP_PARTITION_VALUES
        for subpart_id in SUBPARTITION_VALUES
    }


def first_level_partition_count(sql: str) -> int:
    hash_or_key = re.search(r"^PARTITION BY (?:HASH|KEY) \(`tenant_id`\) PARTITIONS (\d+)", sql, flags=re.I | re.M)
    if hash_or_key:
        return int(hash_or_key.group(1))
    return len(re.findall(r"^\s{2}PARTITION p\d+ ", sql, flags=re.I | re.M))


def vector_dimensions(sql: str) -> list[int]:
    return [int(value) for value in re.findall(r"\bvector\((\d+)\)", sql, flags=re.I)]


def sql_secondary_index_count(sql: str) -> int:
    return len(re.findall(r"^\s+(?:UNIQUE\s+KEY|KEY|SPATIAL\s+KEY)\s+`", sql, flags=re.I | re.M))


def main() -> int:
    errors: list[str] = []
    files = sorted(path.name for path in SQL_DIR.glob("t*.sql"))
    if len(files) != 27:
        fail(f"期望 27 个 tN.sql 文件，实际 {len(files)} 个", errors)

    type_counts = {"normal": 0, "temporary": 0, "partition": 0, "subpartition": 0}
    all_sql_parts: list[str] = []
    for index in range(27):
        sql = read_table(index, errors)
        if not sql:
            continue
        all_sql_parts.append(sql)
        create_pattern = rf"^CREATE\s+(?:TEMPORARY\s+)?TABLE\s+`t{index}`"
        if len(re.findall(create_pattern, sql, flags=re.I | re.M)) != 1:
            fail(f"t{index}.sql 没有且仅有一个匹配表名的 CREATE TABLE", errors)
        if sql.upper().count("VECTOR(") < 2:
            fail(f"t{index}.sql 向量列少于 2 个", errors)
        for dimension in vector_dimensions(sql):
            if dimension > 16383:
                fail(f"t{index}.sql 向量维度超过 16383：{dimension}", errors)
        vector_index_count = sql.count("imci_vector_index=")
        effective_index_count = sql_secondary_index_count(sql) + vector_index_count
        if effective_index_count != TARGET_TOTAL_INDEX_COUNT:
            fail(f"t{index}.sql 索引数量应为 {TARGET_TOTAL_INDEX_COUNT}，实际 {effective_index_count}", errors)
        if index <= 1:
            if vector_index_count != 1:
                fail(f"t{index}.sql 普通表应有且仅有 1 个向量索引，实际 {vector_index_count} 个", errors)
            if "COMMENT='COLUMNAR=1'" not in sql:
                fail(f"t{index}.sql 普通表向量索引需要表级 COLUMNAR=1", errors)
        else:
            if vector_index_count != 0:
                fail(f"t{index}.sql 临时表或分区表不应创建向量索引，实际 {vector_index_count} 个", errors)
            if "COMMENT='COLUMNAR=1'" in sql:
                fail(f"t{index}.sql 临时表或分区表不应带表级 COLUMNAR=1", errors)
        if "SET FOREIGN_KEY_CHECKS=0;" not in sql or "SET FOREIGN_KEY_CHECKS=1;" not in sql:
            fail(f"t{index}.sql 缺少外键检查开关", errors)
        if "SET transaction_isolation = 'READ-COMMITTED';" not in sql:
            fail(f"t{index}.sql 缺少 READ COMMITTED 隔离级别设置", errors)
        if re.search(r"vector_[^\n]*invisible", sql, re.I):
            fail(f"t{index}.sql 向量列不应设置为 INVISIBLE", errors)
        if re.search(r"(PRIMARY KEY|UNIQUE KEY|FOREIGN KEY|PARTITION BY|SUBPARTITION BY)[^\n]*vector_", sql, re.I):
            fail(f"t{index}.sql 向量列不应进入主键、唯一键、外键或分区键", errors)
        if index == 0:
            if "CONSTRAINT `fk_t0_" in sql:
                fail("t0.sql 不应包含父表外键", errors)
        elif 2 <= index <= 6:
            if "FOREIGN KEY" in sql.upper():
                fail(f"t{index}.sql 临时表不应声明 FOREIGN KEY，避免 InnoDB 1215", errors)
        else:
            if sql.count("FOREIGN KEY") < 2:
                fail(f"t{index}.sql 外键数量少于 2 个", errors)
            references = set(re.findall(r"REFERENCES\s+`t(\d+)`", sql, flags=re.I))
            if any(int(ref) >= 2 for ref in references):
                fail(f"t{index}.sql 不应引用临时表或分区表作为外键父表：{sorted(references)}", errors)

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
        elif index <= 10:
            type_counts["partition"] += 1
            if "PARTITION BY" not in upper_sql or "SUBPARTITION BY" in upper_sql:
                fail(f"t{index}.sql 应为一级分区表", errors)
            if first_level_partition_count(sql) != 8:
                fail(f"t{index}.sql 一级分区数量应为 8 个", errors)
        else:
            type_counts["subpartition"] += 1
            if "SUBPARTITION BY" not in upper_sql:
                fail(f"t{index}.sql 应为二级分区表", errors)
            if first_level_partition_count(sql) != 8:
                fail(f"t{index}.sql 一级分区数量应为 8 个", errors)

    expected_counts = {"normal": 2, "temporary": 5, "partition": 4, "subpartition": 16}
    if type_counts != expected_counts:
        fail(f"表类型分布不匹配：{type_counts}", errors)

    all_sql = "\n".join(all_sql_parts)
    if re.search(r"\bFULLTEXT\b", all_sql, re.I):
        fail("带向量基表目录不应包含 FULLTEXT 索引", errors)
    if NO_VECTOR_SQL_DIR.exists():
        no_vector_sql = "\n".join(
            path.read_text(encoding="utf-8")
            for path in NO_VECTOR_SQL_DIR.glob("*")
            if path.is_file()
        )
        if re.search(r"\bFULLTEXT\b", no_vector_sql, re.I):
            fail("无向量副本目录不应包含 FULLTEXT 索引", errors)
        if re.search(r"\bVECTOR\s*\(|STRING_TO_VECTOR|imci_vector_index", no_vector_sql, re.I):
            fail("无向量副本目录不应包含向量列、向量值或向量索引", errors)
        if re.search(r"\bSUBPARTITION\b", no_vector_sql, re.I):
            fail("无向量副本目录不应包含二级分区语法", errors)
        expected_no_vector_files = {f"t{index}.sql" for index in range(11)} | {"zz_seed_fk_data.sql"}
        actual_no_vector_files = {path.name for path in NO_VECTOR_SQL_DIR.glob("*.sql")}
        if actual_no_vector_files != expected_no_vector_files:
            fail(f"无向量副本 SQL 文件集合不匹配：{sorted(actual_no_vector_files)}", errors)
        for index in range(11):
            path = NO_VECTOR_SQL_DIR / f"t{index}.sql"
            if not path.exists():
                fail(f"无向量副本目录缺少 {path.name}", errors)
                continue
            no_vector_table_sql = path.read_text(encoding="utf-8")
            no_vector_index_count = sql_secondary_index_count(no_vector_table_sql)
            if no_vector_index_count != TARGET_TOTAL_INDEX_COUNT:
                fail(f"无向量副本 {path.name} 索引数量应为 {TARGET_TOTAL_INDEX_COUNT}，实际 {no_vector_index_count}", errors)
            if 2 <= index <= 6 and re.search(r"\bFOREIGN\s+KEY\b", no_vector_table_sql, re.I):
                fail(f"无向量副本 {path.name} 临时表不应声明 FOREIGN KEY，避免 InnoDB 1215", errors)
        no_vector_doc_path = NO_VECTOR_SQL_DIR / "执行顺序说明.md"
        if not no_vector_doc_path.exists():
            fail("无向量副本目录缺少执行顺序说明.md", errors)
        else:
            no_vector_doc = no_vector_doc_path.read_text(encoding="utf-8")
            for fragment in ["临时表", "不声明 `FOREIGN KEY`", "1215"]:
                if fragment not in no_vector_doc:
                    fail(f"无向量副本执行顺序说明缺少临时表外键限制说明：{fragment}", errors)
    required_fragments = [
        "SPATIAL KEY",
        "INVISIBLE",
        " DESC",
        "lower(`varchar_col`)",
        "year(`datetime_col`)",
        "`unsigned_int_col` + `smallint_col`",
        "json_extract(`json_col`",
        "`varchar_col`(64)",
        "`blob_col`(32)",
        "FAISS_HNSW_FLAT",
        "HNSW",
        "ON DELETE CASCADE",
        "ON DELETE SET NULL",
    ]
    for fragment in required_fragments:
        if fragment not in all_sql:
            fail(f"缺少覆盖项：{fragment}", errors)

    seed_path = SQL_DIR / "zz_seed_fk_data.sql"
    if not seed_path.exists():
        fail("缺少 zz_seed_fk_data.sql", errors)
    else:
        seed_sql = seed_path.read_text(encoding="utf-8")
        expected_insert_count = sum(len(expected_seed_rows(index)) for index in range(27))
        if "SET transaction_isolation = 'READ-COMMITTED';" not in seed_sql:
            fail("种子数据脚本缺少 READ COMMITTED 隔离级别设置", errors)
        if seed_sql.upper().count("INSERT INTO") != expected_insert_count:
            fail(f"种子数据脚本应包含 {expected_insert_count} 条 INSERT", errors)
        if seed_sql.count("STRING_TO_VECTOR(") != expected_insert_count * 2:
            fail("种子数据脚本应为每条数据插入 2 个向量值", errors)
        if re.search(r"\b(?:nan|inf)\b", seed_sql, re.I):
            fail("种子数据不应包含 NaN 或 Inf", errors)
        for index in range(27):
            if f"INSERT INTO `t{index}`" not in seed_sql:
                fail(f"种子数据缺少 t{index}", errors)
            for tenant_id, subpart_id in expected_seed_rows(index):
                marker = f"/* t{index}:tenant={tenant_id},subpart={subpart_id} */"
                if marker not in seed_sql:
                    fail(f"种子数据缺少 {marker}", errors)

    if not EXECUTION_DOC.exists():
        fail("缺少执行顺序说明.md", errors)
    else:
        doc = EXECUTION_DOC.read_text(encoding="utf-8")
        for name in ["t0.sql", "t1.sql", *[f"t{index}.sql" for index in range(2, 27)], "zz_seed_fk_data.sql"]:
            if name not in doc:
                fail(f"执行顺序说明缺少 {name}", errors)
        for fragment in ["READ-COMMITTED", "vidx_disabled", "SUPER", "vidx_hnsw_cache_size", "LIMIT", "ASC", "VEC_DISTANCE"]:
            if fragment not in doc:
                fail(f"执行顺序说明缺少约束说明：{fragment}", errors)
        for fragment in ["64 个二级索引", "61 个索引", "PRIMARY KEY"]:
            if fragment not in doc:
                fail(f"执行顺序说明缺少索引上限说明：{fragment}", errors)
        for fragment in ["临时表", "不声明 `FOREIGN KEY`", "1215"]:
            if fragment not in doc:
                fail(f"执行顺序说明缺少临时表外键限制说明：{fragment}", errors)

    if errors:
        print("\n".join(errors))
        return 1
    print("结构验证通过：27 张基表、8 个一级分区、分区种子数据、向量约束、外键、索引类型和执行顺序文档均满足要求。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
