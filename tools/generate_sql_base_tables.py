#!/usr/bin/env python3
"""生成 MySQL/PolarDB 基表 DDL 与外键种子数据。"""

from __future__ import annotations

import argparse
import random
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "sql_base_tables"
TOP_PARTITION_VALUES = list(range(1, 9))
SUBPARTITION_VALUES = [1, 2]
TARGET_TOTAL_INDEX_COUNT = 61
MIN_SEED_ROWS = 1000
MAX_SEED_ROWS = 2000
SEED_ROW_RANDOM_SEED = 20260609
SEED_NUMBER_TABLE = "_select_fuzz_seed_numbers"


def table_kind(index: int) -> str:
    if index <= 1:
        return "normal"
    if index <= 6:
        return "temporary"
    if index <= 10:
        return "partition"
    return "subpartition"


def range_partitions() -> str:
    parts = [f"  PARTITION p{idx} VALUES LESS THAN ({value + 1})" for idx, value in enumerate(TOP_PARTITION_VALUES[:-1])]
    parts.append(f"  PARTITION p{len(TOP_PARTITION_VALUES) - 1} VALUES LESS THAN MAXVALUE")
    return ",\n".join(parts)


def list_partitions() -> str:
    return ",\n".join(
        f"  PARTITION p{idx} VALUES IN ({value})"
        for idx, value in enumerate(TOP_PARTITION_VALUES)
    )


def partition_clause(index: int, include_subpartition: bool = True) -> str:
    if index == 7:
        return f"""PARTITION BY RANGE (`tenant_id`) (
{range_partitions()}
)"""
    if index == 8:
        return f"""PARTITION BY LIST (`tenant_id`) (
{list_partitions()}
)"""
    if index == 9:
        return f"""PARTITION BY RANGE COLUMNS (`tenant_id`) (
{range_partitions()}
)"""
    if index == 10:
        return f"""PARTITION BY LIST COLUMNS (`tenant_id`) (
{list_partitions()}
)"""
    if not include_subpartition:
        return f"""PARTITION BY RANGE (`tenant_id`) (
{range_partitions()}
)"""

    subpartition_patterns = [
        ("RANGE", "HASH"),
        ("RANGE", "LINEAR HASH"),
        ("RANGE", "KEY"),
        ("RANGE", "LINEAR KEY"),
        ("RANGE COLUMNS", "HASH"),
        ("RANGE COLUMNS", "LINEAR HASH"),
        ("RANGE COLUMNS", "KEY"),
        ("RANGE COLUMNS", "LINEAR KEY"),
        ("LIST", "HASH"),
        ("LIST", "LINEAR HASH"),
        ("LIST", "KEY"),
        ("LIST", "LINEAR KEY"),
        ("LIST COLUMNS", "HASH"),
        ("LIST COLUMNS", "LINEAR HASH"),
        ("LIST COLUMNS", "KEY"),
        ("LIST COLUMNS", "LINEAR KEY"),
    ]
    outer, inner = subpartition_patterns[index - 11]
    if outer.startswith("RANGE"):
        outer_expr = "(`tenant_id`)" if "COLUMNS" in outer else "(`tenant_id`)"
        return f"""PARTITION BY {outer} {outer_expr}
SUBPARTITION BY {inner} (`subpart_id`) SUBPARTITIONS 2 (
{range_partitions()}
)"""
    outer_expr = "(`tenant_id`)" if "COLUMNS" in outer else "(`tenant_id`)"
    return f"""PARTITION BY {outer} {outer_expr}
SUBPARTITION BY {inner} (`subpart_id`) SUBPARTITIONS 2 (
{list_partitions()}
)"""


NORMAL_OR_TEMPORARY_UNIQUE_INDEXES = {
    "idx_int_col",
    "idx_bigint_desc",
    "idx_year_char",
    "idx_tiny_small_medium",
    "idx_decimal_float_double",
    "idx_date_time_mix",
    "idx_varchar_prefix",
    "idx_binary_combo",
    "idx_blob_prefix",
    "idx_text_prefix",
    "idx_unsigned_desc",
    "idx_lower_varchar",
    "idx_arith_expr",
    "idx_json_expr",
    "idx_extra_tenant_int",
    "idx_extra_subpart_big",
    "idx_extra_tenant_year_date",
    "idx_extra_char_varchar",
    "idx_extra_small_medium_desc",
    "idx_extra_decimal_desc",
    "idx_extra_float_double_desc",
    "idx_extra_datetime_date_desc",
    "idx_extra_time_timestamp",
    "idx_extra_varbinary_binary",
    "idx_extra_tinyblob",
    "idx_extra_blob_tenant",
    "idx_extra_mediumblob",
    "idx_extra_longblob",
    "idx_extra_tinytext_enum",
    "idx_extra_text_set",
    "idx_extra_mediumtext_bit",
    "idx_extra_longtext_unsigned",
    "idx_extra_set_unsigned",
    "idx_extra_bit_decimal",
    "idx_extra_json_n",
    "idx_extra_json_k_lower",
    "idx_extra_time_to_sec",
    "idx_extra_abs_smallint",
    "idx_extra_unsigned_coalesce",
    "idx_extra_concat_code",
    "idx_extra_date_days",
    "idx_extra_decimal_round",
    "idx_extra_float_floor",
    "idx_extra_double_ceiling",
}

PARTITION_UNIQUE_INDEXES = {
    "idx_extra_tenant_int",
    "idx_extra_tenant_year_date",
    "idx_extra_blob_tenant",
}


def can_use_unique_index(index: int, index_name: str, include_subpartition: bool = True) -> bool:
    kind = table_kind(index)
    short_name = f"idx_{index_name.removeprefix(f'idx_t{index}_')}"
    if kind in {"normal", "temporary"}:
        return short_name in NORMAL_OR_TEMPORARY_UNIQUE_INDEXES
    if kind == "partition" or (kind == "subpartition" and not include_subpartition):
        return short_name in PARTITION_UNIQUE_INDEXES
    return False


def key_line(index: int, index_name: str, body: str, include_subpartition: bool = True, suffix: str = "") -> str:
    prefix = "UNIQUE KEY" if can_use_unique_index(index, index_name, include_subpartition) else "KEY"
    return f"  {prefix} `{index_name}` {body}{suffix}"


def supplemental_index_lines(index: int, target_sql_index_count: int, include_subpartition: bool = True) -> list[str]:
    index_lines = [
        key_line(index, f"idx_t{index}_extra_tenant_int", "(`tenant_id`,`int_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_subpart_big", "(`subpart_id`,`bigint_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_tenant_year_date", "(`tenant_id`,`year_col`,`date_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_char_varchar", "(`char_col`,`varchar_col`(32))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_tiny_bool", "(`tinyint_col`,`bool_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_small_medium_desc", "(`smallint_col` DESC,`mediumint_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_decimal_desc", "(`decimal_col` DESC)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_float_double_desc", "(`float_col`,`double_col` DESC)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_datetime_date_desc", "(`datetime_col`,`date_col` DESC)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_time_timestamp", "(`time_col`,`timestamp_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_varbinary_binary", "(`varbinary_col`(32),`binary_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_tinyblob", "(`tinyblob_col`(8))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_blob_tenant", "(`blob_col`(16),`tenant_id`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_mediumblob", "(`mediumblob_col`(16))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_longblob", "(`longblob_col`(16))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_tinytext_enum", "(`tinytext_col`(12),`enum_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_text_set", "(`text_col`(12),`set_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_mediumtext_bit", "(`mediumtext_col`(12),`bit_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_longtext_unsigned", "(`longtext_col`(12),`unsigned_int_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_enum_scope", "(`enum_col`,`tenant_id`,`subpart_id`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_set_unsigned", "(`set_col`,`unsigned_int_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_bit_decimal", "(`bit_col`,`unsigned_decimal_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_parent_chain", "(`parent_id_col`,`parent_int_col`,`parent_bigint_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_metric_parent_desc", "(`metric_parent_tenant_id`,`parent_bigint_col` DESC)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_json_n", "((cast(json_extract(`json_col`,_utf8mb4'$.n') as unsigned)))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_json_k_lower", "((lower(cast(json_unquote(json_extract(`json_col`,_utf8mb4'$.k')) as char(32)))))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_dayofweek", "((dayofweek(`date_col`)))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_month_datetime", "((month(`datetime_col`)))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_time_to_sec", "((time_to_sec(`time_col`)))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_abs_smallint", "((abs(`smallint_col`)))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_unsigned_coalesce", "((coalesce(`unsigned_int_col`,0)))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_concat_code", "((cast(left(concat(`char_col`,`varchar_col`),32) as char(32))))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_crc32_varchar", "((crc32(`varchar_col`)))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_date_days", "((to_days(`date_col`)))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_timestamp_seconds", "((timestampdiff(second,`datetime_col`,`timestamp_col`)))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_decimal_round", "((round(`decimal_col`,2)))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_float_floor", "((floor(`float_col`)))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_double_ceiling", "((ceiling(`double_col`)))", include_subpartition),
    ]
    existing_sql_index_count = 23
    missing_count = target_sql_index_count - existing_sql_index_count
    if missing_count < 0 or missing_count > len(index_lines):
        raise ValueError(f"无法补齐 t{index} 的索引数量：需要新增 {missing_count} 个")
    return index_lines[:missing_count]


def create_table_sql(index: int, include_subpartition: bool = True) -> str:
    kind = table_kind(index)
    create_keyword = "CREATE TEMPORARY TABLE" if kind == "temporary" else "CREATE TABLE"
    drop_keyword = "DROP TEMPORARY TABLE" if kind == "temporary" else "DROP TABLE"
    supplemental_indexes = supplemental_index_lines(index, TARGET_TOTAL_INDEX_COUNT, include_subpartition)
    lines = [
        "SET transaction_isolation = 'READ-COMMITTED';",
        "SET FOREIGN_KEY_CHECKS=0;",
        f"{drop_keyword} IF EXISTS `t{index}`;",
        f"{create_keyword} `t{index}` (",
        "  `id_col` int NOT NULL AUTO_INCREMENT,",
        "  `tenant_id` int NOT NULL DEFAULT 1,",
        "  `subpart_id` int NOT NULL DEFAULT 1,",
        "  `parent_id_col` int DEFAULT NULL,",
        "  `parent_tenant_id` int DEFAULT NULL,",
        "  `parent_subpart_id` int DEFAULT NULL,",
        "  `metric_parent_tenant_id` int DEFAULT NULL,",
        "  `metric_parent_subpart_id` int DEFAULT NULL,",
        "  `parent_int_col` mediumint DEFAULT NULL,",
        "  `parent_bigint_col` bigint DEFAULT NULL,",
        "  `int_col` mediumint DEFAULT NULL,",
        "  `bigint_col` bigint DEFAULT NULL,",
        "  `year_col` year DEFAULT NULL,",
        "  `char_col` char(4) DEFAULT NULL,",
        "  `tinyint_col` tinyint unsigned DEFAULT NULL,",
        "  `bool_col` tinyint(1) DEFAULT NULL,",
        "  `smallint_col` smallint DEFAULT NULL,",
        "  `mediumint_col` mediumint unsigned DEFAULT NULL,",
        "  `decimal_col` decimal(25,5) unsigned DEFAULT NULL,",
        "  `float_col` float unsigned DEFAULT NULL,",
        "  `double_col` double unsigned DEFAULT NULL,",
        "  `date_col` date DEFAULT NULL,",
        "  `datetime_col` datetime(6) DEFAULT NULL,",
        "  `timestamp_col` timestamp NULL DEFAULT NULL,",
        "  `time_col` time DEFAULT NULL,",
        "  `varchar_col` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL,",
        "  `binary_col` binary(1) DEFAULT NULL,",
        "  `varbinary_col` varbinary(255) DEFAULT NULL,",
        "  `tinyblob_col` tinyblob,",
        "  `blob_col` blob,",
        "  `mediumblob_col` mediumblob,",
        "  `longblob_col` longblob,",
        "  `tinytext_col` tinytext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin,",
        "  `text_col` text CHARACTER SET gbk COLLATE gbk_chinese_ci,",
        "  `mediumtext_col` mediumtext CHARACTER SET utf8mb3 COLLATE utf8mb3_bin,",
        "  `longtext_col` longtext CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci,",
        "  `enum_col` enum('aaa','bbb','ccc') CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL,",
        "  `set_col` set('111','222','333') CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL,",
        "  `bit_col` bit(8) DEFAULT b'10011001',",
        "  `unsigned_int_col` int unsigned DEFAULT NULL,",
        "  `unsigned_decimal_col` decimal(10,0) unsigned DEFAULT NULL,",
        "  `json_col` json DEFAULT NULL,",
        "  PRIMARY KEY (`id_col`,`tenant_id`,`subpart_id`),",
        f"  UNIQUE KEY `uk_t{index}_ref_id` (`tenant_id`,`subpart_id`,`id_col`),",
        f"  UNIQUE KEY `uk_t{index}_metric_ref` (`tenant_id`,`subpart_id`,`int_col`,`bigint_col`),",
        f"  UNIQUE KEY `uk_t{index}_char_scope` (`tenant_id`,`subpart_id`,`char_col`),",
        key_line(index, f"idx_t{index}_parent_id", "(`parent_tenant_id`,`parent_subpart_id`,`parent_id_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_parent_metric", "(`metric_parent_tenant_id`,`metric_parent_subpart_id`,`parent_int_col`,`parent_bigint_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_int_col", "(`int_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_bigint_desc", "(`bigint_col` DESC)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_year_char", "(`year_col`,`char_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_tiny_small_medium", "(`tinyint_col`,`smallint_col`,`mediumint_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_decimal_float_double", "(`decimal_col`,`float_col`,`double_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_date_time_mix", "(`date_col`,`datetime_col` DESC,`timestamp_col`,`time_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_varchar_prefix", "(`varchar_col`(64))", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_binary_combo", "(`binary_col`,`varbinary_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_blob_prefix", "(`tinyblob_col`(16),`blob_col`(32),`mediumblob_col`(32),`longblob_col`(32))", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_text_prefix", "(`tinytext_col`(16),`text_col`(16),`mediumtext_col`(16),`longtext_col`(16))", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_enum_set_bit", "(`enum_col`,`set_col`,`bit_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_unsigned_desc", "(`unsigned_int_col` DESC,`unsigned_decimal_col`)", include_subpartition) + ",",
        f"  KEY `idx_t{index}_bool_invisible` (`bool_col`) INVISIBLE,",
        key_line(index, f"idx_t{index}_lower_varchar", "((lower(`varchar_col`)))", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_year_func", "((year(`datetime_col`)))", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_arith_expr", "(((`unsigned_int_col` + `smallint_col`)))", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_json_expr", "((cast(json_unquote(json_extract(`json_col`,_utf8mb4'$.k')) as char(32))))", include_subpartition) + ",",
        *[f"{index_line}," for index_line in supplemental_indexes],
        f"  KEY `idx_t{index}_extra_text_length` ((char_length(`text_col`)))",
    ]
    if index == 1:
        parent = 0
        lines[-1] += ","
        lines.extend(
            [
                f"  CONSTRAINT `fk_t{index}_parent_id` FOREIGN KEY (`parent_tenant_id`,`parent_subpart_id`,`parent_id_col`) REFERENCES `t{parent}` (`tenant_id`,`subpart_id`,`id_col`) ON DELETE CASCADE ON UPDATE CASCADE,",
                f"  CONSTRAINT `fk_t{index}_parent_metric` FOREIGN KEY (`metric_parent_tenant_id`,`metric_parent_subpart_id`,`parent_int_col`,`parent_bigint_col`) REFERENCES `t{parent}` (`tenant_id`,`subpart_id`,`int_col`,`bigint_col`) ON DELETE SET NULL ON UPDATE CASCADE",
            ]
        )
    table_options = ") ENGINE=InnoDB AUTO_INCREMENT=89671 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
    lines.append(table_options)
    if kind == "partition":
        lines[-1] += "\n" + partition_clause(index, include_subpartition)
    elif kind == "subpartition":
        lines[-1] += "\n" + partition_clause(index, include_subpartition)
    lines[-1] += ";"
    lines.append("SET FOREIGN_KEY_CHECKS=1;")
    return "\n".join(lines) + "\n"


def seed_row_count(index: int) -> int:
    return random.Random(SEED_ROW_RANDOM_SEED + index).randint(MIN_SEED_ROWS, MAX_SEED_ROWS)


def max_seed_row_count() -> int:
    return max(seed_row_count(index) for index in range(27))


def tenant_expr(index: int) -> str:
    if index >= 7:
        return f"((`n` - 1) % {len(TOP_PARTITION_VALUES)}) + 1"
    return "1"


def subpart_expr(index: int) -> str:
    if index >= 11:
        return f"((`n` - 1) % {len(SUBPARTITION_VALUES)}) + 1"
    return "1"


def parent_table_index(index: int) -> int:
    return 0 if index == 1 or index % 2 == 0 else 1


def parent_row_expr(index: int) -> str:
    parent = parent_table_index(index)
    return f"((`n` - 1) % {seed_row_count(parent)}) + 1"


def parent_value_expr(index: int, column: str) -> str:
    if index == 0:
        return "NULL"
    parent = 0 if index == 1 or index % 2 == 0 else 1
    row_expr = parent_row_expr(index)
    if column in {"parent_id_col"}:
        return row_expr
    if column in {"parent_tenant_id", "parent_subpart_id", "metric_parent_tenant_id", "metric_parent_subpart_id"}:
        return "1"
    if column == "parent_int_col":
        return f"100 + {parent} * 100000 + {row_expr}"
    if column == "parent_bigint_col":
        return f"100000000 + {parent} * 100000 + {row_expr}"
    raise ValueError(f"未知父列: {column}")


def seed_columns() -> list[str]:
    return [
        "id_col",
        "tenant_id",
        "subpart_id",
        "parent_id_col",
        "parent_tenant_id",
        "parent_subpart_id",
        "metric_parent_tenant_id",
        "metric_parent_subpart_id",
        "parent_int_col",
        "parent_bigint_col",
        "int_col",
        "bigint_col",
        "year_col",
        "char_col",
        "tinyint_col",
        "bool_col",
        "smallint_col",
        "mediumint_col",
        "decimal_col",
        "float_col",
        "double_col",
        "date_col",
        "datetime_col",
        "timestamp_col",
        "time_col",
        "varchar_col",
        "binary_col",
        "varbinary_col",
        "tinyblob_col",
        "blob_col",
        "mediumblob_col",
        "longblob_col",
        "tinytext_col",
        "text_col",
        "mediumtext_col",
        "longtext_col",
        "enum_col",
        "set_col",
        "bit_col",
        "unsigned_int_col",
        "unsigned_decimal_col",
        "json_col",
    ]


def unique_binary_expr(index: int, multiplier: int = 100000) -> str:
    return f"UNHEX(CONCAT(LPAD(HEX({index} * {multiplier} + `n`), 8, '0'), REPEAT('00', 28)))"


def unique_text_expr(index: int, label: str) -> str:
    return f"CONCAT('r', LPAD(`n`, 6, '0'), '_{label}_{index}')"


def seed_value_exprs(index: int) -> list[str]:
    return [
        "`n`",
        tenant_expr(index),
        subpart_expr(index),
        parent_value_expr(index, "parent_id_col"),
        parent_value_expr(index, "parent_tenant_id"),
        parent_value_expr(index, "parent_subpart_id"),
        parent_value_expr(index, "metric_parent_tenant_id"),
        parent_value_expr(index, "metric_parent_subpart_id"),
        parent_value_expr(index, "parent_int_col"),
        parent_value_expr(index, "parent_bigint_col"),
        f"100 + {index} * 100000 + `n`",
        f"100000000 + {index} * 100000 + `n`",
        "2020 + MOD(`n`, 30)",
        "CAST(CONCAT('c', LPAD(CONV(`n`, 10, 36), 3, '0')) AS CHAR(4))",
        f"MOD({index} + `n`, 255)",
        "MOD(`n`, 2)",
        f"{index} + `n`",
        f"100000 + {index} * 1000 + `n`",
        f"{index} * 100000 + `n` + 0.12345",
        f"{index} * 100000 + `n` + 0.5",
        f"{index} * 100000 + `n` + 0.75",
        "DATE_ADD('2026-01-01', INTERVAL `n` DAY)",
        "TIMESTAMPADD(SECOND, `n`, '2026-01-01 10:11:12.123456')",
        "TIMESTAMPADD(SECOND, `n`, '2026-01-01 10:11:12')",
        "SEC_TO_TIME(MOD(`n`, 86400))",
        f"CONCAT('v', LPAD(`n`, 6, '0'), '_t{index}')",
        "UNHEX(LPAD(HEX(MOD(`n`, 256)), 2, '0'))",
        unique_binary_expr(index),
        unique_binary_expr(index, 1000000),
        unique_binary_expr(index, 1000001),
        unique_binary_expr(index, 1000002),
        unique_binary_expr(index, 1000003),
        unique_text_expr(index, "tinytext"),
        unique_text_expr(index, "text"),
        unique_text_expr(index, "mediumtext"),
        unique_text_expr(index, "longtext"),
        "ELT(MOD(`n`, 3) + 1, 'aaa', 'bbb', 'ccc')",
        "IF(MOD(`n`, 3) = 0, '111', IF(MOD(`n`, 3) = 1, '111,222', '222,333'))",
        "b'10011001'",
        f"1000000 + {index} * 100000 + `n`",
        f"2000000 + {index} * 100000 + `n`",
        f"JSON_OBJECT('k', CONCAT('json_{index}_', `n`), 'n', `n`)",
    ]


def seed_number_table_sql() -> str:
    digit_select = " UNION ALL ".join(f"SELECT {value} AS n" if value == 0 else f"SELECT {value}" for value in range(10))
    return f"""DROP TABLE IF EXISTS `{SEED_NUMBER_TABLE}`;
CREATE TABLE `{SEED_NUMBER_TABLE}` (`n` int NOT NULL PRIMARY KEY) ENGINE=InnoDB;
INSERT INTO `{SEED_NUMBER_TABLE}` (`n`)
SELECT ones.n + tens.n * 10 + hundreds.n * 100 + thousands.n * 1000 + 1 AS `n`
FROM ({digit_select}) AS ones
CROSS JOIN ({digit_select}) AS tens
CROSS JOIN ({digit_select}) AS hundreds
CROSS JOIN ({digit_select}) AS thousands
WHERE ones.n + tens.n * 10 + hundreds.n * 100 + thousands.n * 1000 + 1 BETWEEN 1 AND {max_seed_row_count()}
ORDER BY `n`;"""


def insert_sql(index: int) -> str:
    col_sql = ",".join(f"`{column}`" for column in seed_columns())
    select_sql = ",\n  ".join(seed_value_exprs(index))
    row_count = seed_row_count(index)
    return f"""/* t{index}:rows={row_count} */
INSERT INTO `t{index}` ({col_sql})
SELECT
  {select_sql}
FROM `{SEED_NUMBER_TABLE}`
WHERE `n` <= {row_count};"""


def seed_sql() -> str:
    inserts = [insert_sql(index) for index in range(27)]
    lines = [
        "SET transaction_isolation = 'READ-COMMITTED';",
        "SET FOREIGN_KEY_CHECKS=0;",
        seed_number_table_sql(),
        *[f"DELETE FROM `t{index}`;" for index in range(26, -1, -1)],
        "SET FOREIGN_KEY_CHECKS=1;",
        *inserts,
    ]
    return "\n".join(lines) + "\n"


def execution_doc(include_subpartition: bool = True) -> str:
    table_files = [f"t{index}.sql" for index in range(27)]
    compatibility_note = "本目录下的 SQL 文件面向 MySQL 8.0.22 兼容能力生成，不包含向量类型、向量索引列备注或向量种子表达式。"
    subpartition_note = (
        "4. 执行二级分区表：`t11.sql` 到 `t26.sql`。"
        if include_subpartition
        else "4. 执行由二级分区降级为一级分区的兼容表：`t11.sql` 到 `t26.sql`。"
    )
    lines = [
        "# SQL 基表执行顺序说明",
        "",
        compatibility_note,
        "",
        "## 执行前提",
        "",
        "- 所有文件需要在同一个数据库中执行，不在文件内创建或切换数据库。",
        "- 所有建表文件和种子数据文件都会设置 `SET transaction_isolation = 'READ-COMMITTED';`。",
        "- 当前输出不包含向量列，可用于普通 MySQL 8.0 本地建表和插入验证。",
        "- `t2.sql` 到 `t6.sql` 是临时表，必须和 `zz_seed_fk_data.sql` 在同一个 session 内执行。",
        "- `t2.sql` 到 `t6.sql` 是临时表，只保留父表引用列和种子数据关系，不声明 `FOREIGN KEY`，避免 InnoDB 在建临时表时报 1215。",
        (
            "- `t7.sql` 到 `t26.sql` 是分区表，只保留父表引用列、关联索引和种子数据关系，不声明 `FOREIGN KEY`，避免 InnoDB 在建分区表时报 1506。"
            if include_subpartition
            else "- `t7.sql` 到 `t26.sql` 是一级分区兼容表；`t11.sql` 到 `t26.sql` 不生成 `SUBPARTITION BY`，仍不声明 `FOREIGN KEY`，避免 InnoDB 在建分区表时报 1506。"
        ),
        "- 每个建表文件都会短暂执行 `SET FOREIGN_KEY_CHECKS=0;`，建表完成后恢复为 `SET FOREIGN_KEY_CHECKS=1;`。",
        f"- `zz_seed_fk_data.sql` 会创建 `{SEED_NUMBER_TABLE}` 辅助数字表，先按依赖反序清理数据，再恢复外键检查并为每张表插入 {MIN_SEED_ROWS} 到 {MAX_SEED_ROWS} 行可复现随机数据。",
        "- 当前基表不生成空间类型列、空间索引和空间构造函数，避免目标 InnoDB 内核报 1178。",
        "",
        "## 推荐执行顺序",
        "",
        "1. 执行普通父表：`t0.sql`、`t1.sql`。",
        "2. 在同一个 session 中执行临时表：`t2.sql`、`t3.sql`、`t4.sql`、`t5.sql`、`t6.sql`。",
        "3. 执行一级分区表：`t7.sql`、`t8.sql`、`t9.sql`、`t10.sql`。",
        subpartition_note,
        "5. 执行种子数据脚本：`zz_seed_fk_data.sql`。",
        "",
        "## 完整文件顺序",
        "",
    ]
    lines.extend(f"{idx + 1}. `{name}`" for idx, name in enumerate(table_files))
    lines.append(f"{len(table_files) + 1}. `zz_seed_fk_data.sql`")
    lines.extend(
        [
            "",
            "## 分区数据覆盖",
            "",
            f"- 每张表通过固定 seed 决定插入 {MIN_SEED_ROWS} 到 {MAX_SEED_ROWS} 行，生成结果可复现。",
            "- 可安全唯一化的普通索引会生成 `UNIQUE KEY`；二级分区表默认只保留已有唯一键，避免违反 MySQL 分区唯一键必须包含全部分区列的限制。",
            "- `t7.sql` 到 `t10.sql` 每张一级分区表有 8 个一级分区，种子数据使用 `tenant_id` 1 到 8，保证每个一级分区都有数据。",
            (
                "- `t11.sql` 到 `t26.sql` 每张二级分区表有 8 个一级分区，每个一级分区下种子数据写入 `subpart_id` 1 和 2，用于覆盖一级分区和子分区路由。"
                if include_subpartition
                else "- `t11.sql` 到 `t26.sql` 在兼容输出中降级为一级分区表，种子数据仍保留 `subpart_id` 1 和 2 取值。"
            ),
            "- 父表引用数据固定指向 `t0` 或 `t1`，避免永久表引用临时表造成生命周期不稳定。",
            "- `t0.sql` 和 `t1.sql` 是普通 InnoDB 表，不创建向量索引；其中 `t1.sql` 声明实际 `FOREIGN KEY` 约束。",
            "- `t2.sql` 到 `t6.sql` 是临时表，不包含向量列，也不声明实际外键。",
            "- `t7.sql` 到 `t26.sql` 是分区表，不包含向量列，也不声明实际外键。",
            "- 按 InnoDB 每表最多 64 个二级索引计算，每张表补齐到索引上限数减 3，即 61 个索引；`PRIMARY KEY` 不计入该数量。",
            "- 输出不包含向量索引，每张表均为 61 个常规二级索引。",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 MySQL/PolarDB 基表 DDL 与种子数据")
    parser.add_argument("--output-dir", type=Path, default=OUT_DIR, help="输出目录，默认写入 sql_base_tables")
    parser.add_argument("--without-subpartition", action="store_true", help="将二级分区表降级为一级分区表")
    return parser.parse_args()


def generate_files(output_dir: Path, include_subpartition: bool = True) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for path in output_dir.glob("*.sql"):
        path.unlink()
    for index in range(27):
        (output_dir / f"t{index}.sql").write_text(
            create_table_sql(index, include_subpartition=include_subpartition),
            encoding="utf-8",
        )
    (output_dir / "zz_seed_fk_data.sql").write_text(seed_sql(), encoding="utf-8")
    (output_dir / "执行顺序说明.md").write_text(
        execution_doc(include_subpartition=include_subpartition),
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    generate_files(
        args.output_dir,
        include_subpartition=not args.without_subpartition,
    )


if __name__ == "__main__":
    main()
