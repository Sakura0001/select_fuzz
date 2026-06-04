#!/usr/bin/env python3
"""生成 MySQL/PolarDB 基表 DDL 与外键种子数据。"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "sql_base_tables"
NO_VECTOR_OUT_DIR = ROOT / "sql_base_tables_no_vector_subpartition"
TOP_PARTITION_VALUES = list(range(1, 9))
SUBPARTITION_VALUES = [1, 2]
TARGET_TOTAL_INDEX_COUNT = 61


def table_kind(index: int) -> str:
    if index <= 1:
        return "normal"
    if index <= 6:
        return "temporary"
    if index <= 10:
        return "partition"
    return "subpartition"


def vector_index_comment(index: int) -> str:
    return [
        "imci_vector_index=HNSW(metric=COSINE,max_degree=16,ef_construction=300)",
        "imci_vector_index=FAISS_HNSW_FLAT(metric=COSINE,max_degree=32,ef_construction=300)",
    ][index % 2]


def range_partitions() -> str:
    parts = [f"  PARTITION p{idx} VALUES LESS THAN ({value + 1})" for idx, value in enumerate(TOP_PARTITION_VALUES[:-1])]
    parts.append(f"  PARTITION p{len(TOP_PARTITION_VALUES) - 1} VALUES LESS THAN MAXVALUE")
    return ",\n".join(parts)


def list_partitions() -> str:
    return ",\n".join(
        f"  PARTITION p{idx} VALUES IN ({value})"
        for idx, value in enumerate(TOP_PARTITION_VALUES)
    )


def partition_clause(index: int) -> str:
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


def supplemental_index_lines(index: int, target_sql_index_count: int) -> list[str]:
    index_lines = [
        f"  KEY `idx_t{index}_extra_tenant_int` (`tenant_id`,`int_col`)",
        f"  KEY `idx_t{index}_extra_subpart_big` (`subpart_id`,`bigint_col`)",
        f"  KEY `idx_t{index}_extra_tenant_year_date` (`tenant_id`,`year_col`,`date_col`)",
        f"  KEY `idx_t{index}_extra_char_varchar` (`char_col`,`varchar_col`(32))",
        f"  KEY `idx_t{index}_extra_tiny_bool` (`tinyint_col`,`bool_col`)",
        f"  KEY `idx_t{index}_extra_small_medium_desc` (`smallint_col` DESC,`mediumint_col`)",
        f"  KEY `idx_t{index}_extra_decimal_desc` (`decimal_col` DESC)",
        f"  KEY `idx_t{index}_extra_float_double_desc` (`float_col`,`double_col` DESC)",
        f"  KEY `idx_t{index}_extra_datetime_date_desc` (`datetime_col`,`date_col` DESC)",
        f"  KEY `idx_t{index}_extra_time_timestamp` (`time_col`,`timestamp_col`)",
        f"  KEY `idx_t{index}_extra_varbinary_binary` (`varbinary_col`(32),`binary_col`)",
        f"  KEY `idx_t{index}_extra_tinyblob` (`tinyblob_col`(8))",
        f"  KEY `idx_t{index}_extra_blob_tenant` (`blob_col`(16),`tenant_id`)",
        f"  KEY `idx_t{index}_extra_mediumblob` (`mediumblob_col`(16))",
        f"  KEY `idx_t{index}_extra_longblob` (`longblob_col`(16))",
        f"  KEY `idx_t{index}_extra_tinytext_enum` (`tinytext_col`(12),`enum_col`)",
        f"  KEY `idx_t{index}_extra_text_set` (`text_col`(12),`set_col`)",
        f"  KEY `idx_t{index}_extra_mediumtext_bit` (`mediumtext_col`(12),`bit_col`)",
        f"  KEY `idx_t{index}_extra_longtext_unsigned` (`longtext_col`(12),`unsigned_int_col`)",
        f"  KEY `idx_t{index}_extra_enum_scope` (`enum_col`,`tenant_id`,`subpart_id`)",
        f"  KEY `idx_t{index}_extra_set_unsigned` (`set_col`,`unsigned_int_col`)",
        f"  KEY `idx_t{index}_extra_bit_decimal` (`bit_col`,`unsigned_decimal_col`)",
        f"  KEY `idx_t{index}_extra_parent_chain` (`parent_id_col`,`parent_int_col`,`parent_bigint_col`)",
        f"  KEY `idx_t{index}_extra_metric_parent_desc` (`metric_parent_tenant_id`,`parent_bigint_col` DESC)",
        f"  KEY `idx_t{index}_extra_json_n` ((cast(json_extract(`json_col`,_utf8mb4'$.n') as unsigned)))",
        f"  KEY `idx_t{index}_extra_json_k_lower` ((lower(cast(json_unquote(json_extract(`json_col`,_utf8mb4'$.k')) as char(32)))))",
        f"  KEY `idx_t{index}_extra_dayofweek` ((dayofweek(`date_col`)))",
        f"  KEY `idx_t{index}_extra_month_datetime` ((month(`datetime_col`)))",
        f"  KEY `idx_t{index}_extra_time_to_sec` ((time_to_sec(`time_col`)))",
        f"  KEY `idx_t{index}_extra_abs_smallint` ((abs(`smallint_col`)))",
        f"  KEY `idx_t{index}_extra_unsigned_coalesce` ((coalesce(`unsigned_int_col`,0)))",
        f"  KEY `idx_t{index}_extra_concat_code` ((cast(left(concat(`char_col`,`varchar_col`),32) as char(32))))",
        f"  KEY `idx_t{index}_extra_crc32_varchar` ((crc32(`varchar_col`)))",
        f"  KEY `idx_t{index}_extra_date_days` ((to_days(`date_col`)))",
        f"  KEY `idx_t{index}_extra_timestamp_seconds` ((timestampdiff(second,`datetime_col`,`timestamp_col`)))",
        f"  KEY `idx_t{index}_extra_decimal_round` ((round(`decimal_col`,2)))",
        f"  KEY `idx_t{index}_extra_float_floor` ((floor(`float_col`)))",
        f"  KEY `idx_t{index}_extra_double_ceiling` ((ceiling(`double_col`)))",
    ]
    existing_sql_index_count = 23
    missing_count = target_sql_index_count - existing_sql_index_count
    if missing_count < 0 or missing_count > len(index_lines):
        raise ValueError(f"无法补齐 t{index} 的索引数量：需要新增 {missing_count} 个")
    return index_lines[:missing_count]


def create_table_sql(index: int) -> str:
    kind = table_kind(index)
    create_keyword = "CREATE TEMPORARY TABLE" if kind == "temporary" else "CREATE TABLE"
    drop_keyword = "DROP TEMPORARY TABLE" if kind == "temporary" else "DROP TABLE"
    vector_col_suffix = f" COMMENT '{vector_index_comment(index)}'" if index <= 1 else ""
    target_sql_index_count = TARGET_TOTAL_INDEX_COUNT - (1 if index <= 1 else 0)
    supplemental_indexes = supplemental_index_lines(index, target_sql_index_count)
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
        "  `point_col` point NOT NULL SRID 4326,",
        f"  `vector_col` vector(4){vector_col_suffix},",
        "  `vector_aux_col` vector(8),",
        "  PRIMARY KEY (`id_col`,`tenant_id`,`subpart_id`),",
        f"  UNIQUE KEY `uk_t{index}_ref_id` (`tenant_id`,`subpart_id`,`id_col`),",
        f"  UNIQUE KEY `uk_t{index}_metric_ref` (`tenant_id`,`subpart_id`,`int_col`,`bigint_col`),",
        f"  UNIQUE KEY `uk_t{index}_char_scope` (`tenant_id`,`subpart_id`,`char_col`),",
        f"  KEY `idx_t{index}_parent_id` (`parent_tenant_id`,`parent_subpart_id`,`parent_id_col`),",
        f"  KEY `idx_t{index}_parent_metric` (`metric_parent_tenant_id`,`metric_parent_subpart_id`,`parent_int_col`,`parent_bigint_col`),",
        f"  KEY `idx_t{index}_int_col` (`int_col`),",
        f"  KEY `idx_t{index}_bigint_desc` (`bigint_col` DESC),",
        f"  KEY `idx_t{index}_year_char` (`year_col`,`char_col`),",
        f"  KEY `idx_t{index}_tiny_small_medium` (`tinyint_col`,`smallint_col`,`mediumint_col`),",
        f"  KEY `idx_t{index}_decimal_float_double` (`decimal_col`,`float_col`,`double_col`),",
        f"  KEY `idx_t{index}_date_time_mix` (`date_col`,`datetime_col` DESC,`timestamp_col`,`time_col`),",
        f"  KEY `idx_t{index}_varchar_prefix` (`varchar_col`(64)),",
        f"  KEY `idx_t{index}_binary_combo` (`binary_col`,`varbinary_col`),",
        f"  KEY `idx_t{index}_blob_prefix` (`tinyblob_col`(16),`blob_col`(32),`mediumblob_col`(32),`longblob_col`(32)),",
        f"  KEY `idx_t{index}_text_prefix` (`tinytext_col`(16),`text_col`(16),`mediumtext_col`(16),`longtext_col`(16)),",
        f"  KEY `idx_t{index}_enum_set_bit` (`enum_col`,`set_col`,`bit_col`),",
        f"  KEY `idx_t{index}_unsigned_desc` (`unsigned_int_col` DESC,`unsigned_decimal_col`),",
        f"  KEY `idx_t{index}_bool_invisible` (`bool_col`) INVISIBLE,",
        f"  KEY `idx_t{index}_lower_varchar` ((lower(`varchar_col`))),",
        f"  KEY `idx_t{index}_year_func` ((year(`datetime_col`))),",
        f"  KEY `idx_t{index}_arith_expr` (((`unsigned_int_col` + `smallint_col`))),",
        f"  KEY `idx_t{index}_json_expr` ((cast(json_unquote(json_extract(`json_col`,_utf8mb4'$.k')) as char(32)))),",
        *[f"{index_line}," for index_line in supplemental_indexes],
        f"  SPATIAL KEY `sp_t{index}_point_col` (`point_col`)",
    ]
    if index > 0 and kind != "temporary":
        parent = 0 if index == 1 or index % 2 == 0 else 1
        lines[-1] += ","
        lines.extend(
            [
                f"  CONSTRAINT `fk_t{index}_parent_id` FOREIGN KEY (`parent_tenant_id`,`parent_subpart_id`,`parent_id_col`) REFERENCES `t{parent}` (`tenant_id`,`subpart_id`,`id_col`) ON DELETE CASCADE ON UPDATE CASCADE,",
                f"  CONSTRAINT `fk_t{index}_parent_metric` FOREIGN KEY (`metric_parent_tenant_id`,`metric_parent_subpart_id`,`parent_int_col`,`parent_bigint_col`) REFERENCES `t{parent}` (`tenant_id`,`subpart_id`,`int_col`,`bigint_col`) ON DELETE SET NULL ON UPDATE CASCADE",
            ]
        )
    table_options = ") ENGINE=InnoDB AUTO_INCREMENT=89671 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
    if index <= 1:
        table_options += " COMMENT='COLUMNAR=1'"
    lines.append(table_options)
    if kind == "partition":
        lines[-1] += "\n" + partition_clause(index)
    elif kind == "subpartition":
        lines[-1] += "\n" + partition_clause(index)
    lines[-1] += ";"
    lines.append("SET FOREIGN_KEY_CHECKS=1;")
    return "\n".join(lines) + "\n"


def vector_literal(index: int, row_id: int, dims: int) -> str:
    values = [f"{(index + 1) * 0.1 + row_id * 0.001 + offset * 0.01:.3f}" for offset in range(dims)]
    return "[" + ",".join(values) + "]"


def seed_rows_for_table(index: int) -> list[tuple[int, int]]:
    if index <= 6:
        return [(1, 1)]
    if index <= 10:
        return [(tenant_id, 1) for tenant_id in TOP_PARTITION_VALUES]
    return [
        (tenant_id, subpart_id)
        for tenant_id in TOP_PARTITION_VALUES
        for subpart_id in SUBPARTITION_VALUES
    ]


def insert_sql(index: int, tenant_id: int, subpart_id: int, row_id: int) -> str:
    parent = 0 if index == 1 or index % 2 == 0 else 1
    parent_columns = {
        "parent_id_col": "NULL" if index == 0 else "1",
        "parent_tenant_id": "NULL" if index == 0 else "1",
        "parent_subpart_id": "NULL" if index == 0 else "1",
        "metric_parent_tenant_id": "NULL" if index == 0 else "1",
        "metric_parent_subpart_id": "NULL" if index == 0 else "1",
        "parent_int_col": "NULL" if index == 0 else str(100 + parent * 1000 + 1),
        "parent_bigint_col": "NULL" if index == 0 else str(100000 + parent * 1000 + 1),
    }
    columns = [
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
        "point_col",
        "vector_col",
        "vector_aux_col",
    ]
    values = [
        str(row_id),
        str(tenant_id),
        str(subpart_id),
        parent_columns["parent_id_col"],
        parent_columns["parent_tenant_id"],
        parent_columns["parent_subpart_id"],
        parent_columns["metric_parent_tenant_id"],
        parent_columns["metric_parent_subpart_id"],
        parent_columns["parent_int_col"],
        parent_columns["parent_bigint_col"],
        str(100 + index * 1000 + row_id),
        str(100000 + index * 1000 + row_id),
        str(2020 + (index % 10)),
        f"'c{row_id % 100:02d}'",
        str((index + row_id) % 255),
        "1" if row_id % 2 else "0",
        str(index + row_id),
        str(1000 + index + row_id),
        f"{index + row_id}.12345",
        f"{index + row_id}.5",
        f"{index + row_id}.75",
        f"'2026-01-{(row_id % 27) + 1:02d}'",
        f"'2026-01-{(row_id % 27) + 1:02d} 10:11:12.123456'",
        f"'2026-01-{(row_id % 27) + 1:02d} 10:11:12'",
        f"'0{row_id % 10}:01:02'",
        f"'varchar_{index}_{row_id}'",
        "0x41",
        f"UNHEX('{index * 1000 + row_id:064x}')",
        f"UNHEX('{(index + row_id) % 256:02x}')",
        f"UNHEX('{index * 1000 + row_id:04x}')",
        f"UNHEX('{index * 1000 + row_id:08x}')",
        f"UNHEX('{index * 1000 + row_id:016x}')",
        f"'tinytext_{index}_{row_id}'",
        f"'text_{index}_{row_id}'",
        f"'mediumtext_{index}_{row_id}'",
        f"'longtext_{index}_{row_id}'",
        "'aaa'",
        "'111,222'",
        "b'10011001'",
        str(10000 + index * 1000 + row_id),
        str(20000 + index * 1000 + row_id),
        f"JSON_OBJECT('k','json_{index}_{row_id}','n',{row_id})",
        f"ST_GeomFromText('POINT({100 + index + row_id * 0.001:.3f} {30 + index + row_id * 0.001:.3f})', 4326)",
        f"STRING_TO_VECTOR('{vector_literal(index, row_id, 4)}')",
        f"STRING_TO_VECTOR('{vector_literal(index, row_id, 8)}')",
    ]
    col_sql = ",".join(f"`{column}`" for column in columns)
    val_sql = ", ".join(values)
    return f"/* t{index}:tenant={tenant_id},subpart={subpart_id} */\nINSERT INTO `t{index}` ({col_sql}) VALUES ({val_sql});"


def seed_sql() -> str:
    inserts: list[str] = []
    for index in range(27):
        for row_id, (tenant_id, subpart_id) in enumerate(seed_rows_for_table(index), start=1):
            inserts.append(insert_sql(index, tenant_id, subpart_id, row_id))
    lines = [
        "SET transaction_isolation = 'READ-COMMITTED';",
        "SET FOREIGN_KEY_CHECKS=0;",
        *[f"DELETE FROM `t{index}`;" for index in range(26, -1, -1)],
        "SET FOREIGN_KEY_CHECKS=1;",
        *inserts,
    ]
    return "\n".join(lines) + "\n"


def execution_doc() -> str:
    table_files = [f"t{index}.sql" for index in range(27)]
    lines = [
        "# SQL 基表执行顺序说明",
        "",
        "本目录下的 SQL 文件面向支持 PolarDB MySQL 向量类型和内部二级分区表能力的 InnoDB 内核。标准 MySQL 客户端或普通 MySQL 8.0 内核不一定支持 `VECTOR(N)`、向量索引列备注和内部二级分区语法。",
        "",
        "## 执行前提",
        "",
        "- 所有文件需要在同一个数据库中执行，不在文件内创建或切换数据库。",
        "- 所有建表文件和种子数据文件都会设置 `SET transaction_isolation = 'READ-COMMITTED';`。",
        "- 向量索引功能默认禁用时，需要具备 SUPER 权限后按环境要求开启，例如 `SET GLOBAL vidx_disabled=OFF;`。",
        "- 单个向量索引缓存默认按 `vidx_hnsw_cache_size` 控制；缓存超限后的清理和重载行为由内核实现负责。",
        "- `t2.sql` 到 `t6.sql` 是临时表，必须和 `zz_seed_fk_data.sql` 在同一个 session 内执行。",
        "- `t2.sql` 到 `t6.sql` 是临时表，只保留父表引用列和种子数据关系，不声明 `FOREIGN KEY`，避免 InnoDB 在建临时表时报 1215。",
        "- 每个建表文件都会短暂执行 `SET FOREIGN_KEY_CHECKS=0;`，建表完成后恢复为 `SET FOREIGN_KEY_CHECKS=1;`。",
        "- `zz_seed_fk_data.sql` 会先按依赖反序清理数据，再恢复外键检查并插入种子数据。",
        "- 向量索引查询需要使用 `VEC_DISTANCE`，参数形式为向量列加常量向量，排序方向使用 ASC，并带 LIMIT。",
        "- 查询距离函数需要和向量索引 DISTANCE 设置一致；DESC 排序不触发向量索引。",
        "- 向量索引 ADD、DROP、RENAME 不应和其他 DDL 组合在同一条 `ALTER TABLE` 中。",
        "- 向量索引不支持 `PACK_KEYS` 等紧凑索引存储选项，`ALTER INDEX ... VISIBLE/INVISIBLE` 对向量索引无效。",
        "",
        "## 推荐执行顺序",
        "",
        "1. 执行普通父表：`t0.sql`、`t1.sql`。",
        "2. 在同一个 session 中执行临时表：`t2.sql`、`t3.sql`、`t4.sql`、`t5.sql`、`t6.sql`。",
        "3. 执行一级分区表：`t7.sql`、`t8.sql`、`t9.sql`、`t10.sql`。",
        "4. 执行二级分区表：`t11.sql` 到 `t26.sql`。",
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
            "- `t7.sql` 到 `t10.sql` 每张一级分区表有 8 个一级分区，种子数据使用 `tenant_id` 1 到 8，保证每个一级分区至少一行。",
            "- `t11.sql` 到 `t26.sql` 每张二级分区表有 8 个一级分区，每个一级分区下种子数据写入 `subpart_id` 1 和 2 两行，用于覆盖一级分区和子分区路由。",
            "- 外键父表固定为 `t0` 或 `t1`，避免永久表引用临时表造成生命周期不稳定。",
        "- `t0.sql` 和 `t1.sql` 是普通 InnoDB 表，每张表有且仅有一个向量索引。",
            "- `t2.sql` 到 `t6.sql` 是临时表，保留向量列但不创建向量索引，也不声明实际外键。",
            "- `t7.sql` 到 `t26.sql` 是分区表，保留向量列但不创建向量索引。",
            "- 按 InnoDB 每表最多 64 个二级索引计算，每张表补齐到索引上限数减 3，即 61 个索引；`PRIMARY KEY` 不计入该数量。",
            "- `t0.sql` 和 `t1.sql` 的 61 个索引包含 1 个向量索引和 60 个常规二级索引，其余表为 61 个常规二级索引。",
        ]
    )
    return "\n".join(lines) + "\n"


def no_vector_table_sql(index: int) -> str:
    sql = create_table_sql(index)
    lines: list[str] = []
    for line in sql.splitlines():
        lowered = line.lower()
        if "`vector_col`" in lowered or "`vector_aux_col`" in lowered:
            continue
        line = line.replace(" COMMENT='COLUMNAR=1'", "")
        if index <= 1 and line.strip().startswith("SPATIAL KEY"):
            lines.append(f"  KEY `idx_t{index}_extra_no_vector_fill` ((length(`varchar_col`))),")
        lines.append(line)
    return "\n".join(lines) + "\n"


def no_vector_seed_sql() -> str:
    seed_lines: list[str] = []
    for line in seed_sql().splitlines():
        if re.match(r"DELETE FROM `t(?:1[1-9]|2[0-6])`;", line):
            continue
        if re.match(r"/\* t(?:1[1-9]|2[0-6]):", line):
            continue
        if re.match(r"INSERT INTO `t(?:1[1-9]|2[0-6])`", line):
            continue
        if line.startswith("INSERT INTO `t"):
            line = line.replace(",`vector_col`,`vector_aux_col`", "")
            line = re.sub(r", STRING_TO_VECTOR\('[^']+'\), STRING_TO_VECTOR\('[^']+'\)\);$", ");", line)
        seed_lines.append(line)
    return "\n".join(seed_lines) + "\n"


def no_vector_execution_doc() -> str:
    table_files = [f"t{index}.sql" for index in range(11)]
    lines = [
        "# SQL 基表执行顺序说明",
        "",
        "本目录下的 SQL 文件需要在同一个数据库中执行，不在文件内创建或切换数据库。",
        "",
        "## 执行前提",
        "",
        "- `t2.sql` 到 `t6.sql` 是临时表，必须和 `zz_seed_fk_data.sql` 在同一个 session 内执行。",
        "- `t2.sql` 到 `t6.sql` 是临时表，只保留父表引用列和种子数据关系，不声明 `FOREIGN KEY`，避免 InnoDB 在建临时表时报 1215。",
        "- 每个建表文件都会短暂执行 `SET FOREIGN_KEY_CHECKS=0;`，建表完成后恢复为 `SET FOREIGN_KEY_CHECKS=1;`。",
        "- `zz_seed_fk_data.sql` 会先按依赖反序清理数据，再恢复外键检查并插入种子数据。",
        "- 按 InnoDB 每表最多 64 个二级索引计算，每张表补齐到索引上限数减 3，即 61 个索引；`PRIMARY KEY` 不计入该数量。",
        "",
        "## 推荐执行顺序",
        "",
        "1. 执行普通父表：`t0.sql`、`t1.sql`。",
        "2. 在同一个 session 中执行临时表：`t2.sql`、`t3.sql`、`t4.sql`、`t5.sql`、`t6.sql`。",
        "3. 执行一级分区表：`t7.sql`、`t8.sql`、`t9.sql`、`t10.sql`。",
        "4. 执行种子数据脚本：`zz_seed_fk_data.sql`。",
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
            "- `t7.sql` 到 `t10.sql` 每张一级分区表有 8 个一级分区。",
            "- 种子数据使用 `tenant_id` 1 到 8，保证每个一级分区至少一行。",
            "- 外键父表固定为 `t0` 或 `t1`，避免永久表引用临时表造成生命周期不稳定。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in OUT_DIR.glob("*.sql"):
        path.unlink()
    for index in range(27):
        (OUT_DIR / f"t{index}.sql").write_text(create_table_sql(index), encoding="utf-8")
    (OUT_DIR / "zz_seed_fk_data.sql").write_text(seed_sql(), encoding="utf-8")
    (OUT_DIR / "执行顺序说明.md").write_text(execution_doc(), encoding="utf-8")
    NO_VECTOR_OUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in NO_VECTOR_OUT_DIR.glob("*.sql"):
        path.unlink()
    for index in range(11):
        (NO_VECTOR_OUT_DIR / f"t{index}.sql").write_text(no_vector_table_sql(index), encoding="utf-8")
    (NO_VECTOR_OUT_DIR / "zz_seed_fk_data.sql").write_text(no_vector_seed_sql(), encoding="utf-8")
    (NO_VECTOR_OUT_DIR / "执行顺序说明.md").write_text(no_vector_execution_doc(), encoding="utf-8")


if __name__ == "__main__":
    main()
