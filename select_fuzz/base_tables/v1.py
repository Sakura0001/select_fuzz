"""冻结的 `v1` MySQL/PolarDB 核心及扩展基表生成器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from select_fuzz.metadata.models import BaseSqlFile

from .deterministic import derive_range, pick
from .loader import build_base_sql_bundle
from .models import BaseSqlBundle


TOP_PARTITION_VALUES = tuple(range(1, 9))
SUBPARTITION_VALUES = tuple(range(1, 9))
TARGET_TOTAL_INDEX_COUNT = 61
MIN_SEED_ROWS = 10
MAX_SEED_ROWS = 100
MIN_TABLE_COLUMNS = 200
MAX_TABLE_COLUMNS = 500
SEED_NUMBER_TABLE = "_select_fuzz_seed_numbers"
FIRST_PARTITION_START = 7
FIRST_PARTITION_COUNT = 8
SUBPARTITION_START = FIRST_PARTITION_START + FIRST_PARTITION_COUNT
PARTITION_TYPES = (
    "RANGE",
    "RANGE COLUMNS",
    "LIST",
    "LIST COLUMNS",
    "HASH",
    "LINEAR HASH",
    "KEY",
    "LINEAR KEY",
)
SUBPARTITION_TABLE_COUNT = len(PARTITION_TYPES) * len(PARTITION_TYPES)
TOTAL_TABLE_COUNT = SUBPARTITION_START + SUBPARTITION_TABLE_COUNT


@dataclass(frozen=True)
class TableColumnProfile:
    char_length: int
    varchar_length: int
    binary_length: int
    varbinary_length: int
    bit_length: int
    decimal_precision: int
    decimal_scale: int
    unsigned_decimal_precision: int
    unsigned_decimal_scale: int
    datetime_fsp: int
    timestamp_fsp: int
    time_fsp: int
    tinyint_unsigned: bool
    mediumint_unsigned: bool
    float_unsigned: bool
    double_unsigned: bool


@dataclass(frozen=True)
class ExtraColumnSpec:
    name: str
    sql_type: str
    value_expr: str
    indexed_prefix_length: int | None = None


_TABLE_COLUMN_PROFILE_VALUES = (
    (1, 255, 7, 155, 5, 24, 6, 20, 5, 6, 6, 2, False, False, False, True),
    (255, 1, 44, 146, 59, 16, 5, 23, 3, 5, 6, 5, False, True, False, True),
    (101, 188, 130, 50, 55, 22, 6, 17, 9, 5, 6, 6, True, True, False, False),
    (106, 63, 218, 16, 3, 18, 6, 25, 7, 2, 3, 4, False, True, False, False),
    (206, 45, 140, 129, 36, 20, 2, 21, 8, 4, 3, 1, False, True, False, False),
    (176, 120, 201, 2, 57, 27, 9, 16, 7, 3, 1, 3, False, True, True, False),
    (215, 182, 81, 51, 43, 8, 0, 13, 5, 5, 4, 0, False, False, False, True),
    (243, 121, 64, 28, 10, 28, 10, 15, 6, 6, 2, 0, False, True, False, False),
    (229, 168, 134, 27, 57, 19, 6, 12, 2, 1, 3, 3, True, True, True, True),
    (201, 39, 232, 90, 55, 11, 1, 16, 5, 2, 6, 4, True, False, False, False),
    (218, 146, 247, 163, 63, 17, 2, 24, 9, 6, 3, 5, True, True, False, True),
    (136, 82, 59, 252, 54, 19, 4, 13, 3, 4, 4, 5, False, False, False, True),
    (72, 76, 38, 199, 1, 22, 7, 11, 3, 1, 3, 0, True, False, True, True),
    (197, 238, 187, 186, 56, 24, 8, 18, 7, 4, 4, 1, False, False, False, False),
    (185, 226, 126, 131, 42, 20, 0, 19, 2, 1, 0, 3, False, True, False, True),
    (123, 47, 239, 32, 39, 27, 7, 15, 5, 1, 0, 6, False, True, True, True),
    (19, 149, 101, 23, 20, 21, 8, 11, 1, 4, 0, 2, False, False, False, False),
    (170, 225, 249, 247, 58, 24, 4, 19, 1, 4, 3, 3, True, False, False, False),
    (210, 26, 15, 91, 55, 20, 2, 20, 6, 1, 2, 4, False, True, True, True),
    (62, 95, 66, 127, 54, 26, 6, 23, 10, 2, 5, 1, False, False, True, False),
    (217, 97, 180, 64, 49, 15, 0, 24, 7, 2, 6, 1, True, True, False, False),
    (40, 102, 193, 182, 20, 18, 0, 22, 3, 0, 6, 3, True, False, False, True),
    (194, 88, 219, 8, 12, 20, 9, 20, 2, 6, 6, 5, False, True, False, True),
    (23, 242, 165, 228, 16, 22, 5, 15, 1, 1, 1, 3, False, True, True, True),
    (82, 144, 76, 146, 1, 19, 4, 13, 3, 0, 6, 0, True, True, False, False),
    (202, 137, 144, 138, 30, 12, 4, 14, 4, 2, 6, 1, False, False, False, True),
    (190, 244, 248, 3, 22, 20, 3, 22, 3, 1, 1, 1, False, False, True, False),
    (244, 195, 35, 132, 23, 12, 3, 22, 9, 6, 6, 6, False, False, False, False),
    (99, 64, 64, 170, 29, 19, 10, 16, 7, 1, 2, 2, False, True, True, False),
    (170, 251, 84, 70, 36, 18, 2, 21, 5, 3, 5, 5, False, True, True, False),
    (140, 207, 145, 121, 14, 27, 10, 26, 6, 2, 5, 5, True, True, False, False),
    (116, 5, 97, 244, 10, 14, 0, 18, 10, 1, 3, 2, True, True, True, True),
    (32, 80, 125, 108, 16, 25, 8, 16, 3, 2, 3, 1, False, True, True, True),
    (229, 168, 27, 248, 54, 17, 8, 13, 3, 5, 0, 6, False, False, True, False),
    (226, 204, 95, 237, 47, 25, 6, 30, 10, 3, 4, 1, True, False, False, True),
    (116, 125, 122, 143, 33, 9, 1, 20, 7, 6, 3, 1, False, True, False, True),
    (62, 65, 116, 119, 3, 21, 3, 8, 0, 3, 3, 3, False, True, True, False),
    (253, 44, 188, 135, 44, 14, 0, 18, 0, 5, 2, 6, True, True, True, True),
    (57, 182, 228, 5, 25, 24, 10, 23, 10, 2, 3, 0, True, True, True, False),
    (86, 140, 2, 28, 7, 16, 6, 14, 6, 4, 2, 3, False, False, False, True),
    (87, 123, 62, 80, 54, 21, 6, 25, 10, 1, 4, 3, False, True, False, False),
    (200, 183, 38, 210, 6, 15, 6, 14, 6, 5, 0, 6, False, False, False, False),
    (187, 180, 73, 140, 18, 22, 2, 24, 8, 1, 4, 6, False, False, False, False),
    (128, 158, 73, 159, 46, 10, 0, 19, 1, 1, 2, 5, True, False, True, True),
    (152, 210, 143, 154, 36, 22, 7, 16, 4, 1, 0, 6, False, True, False, False),
    (50, 47, 141, 204, 53, 16, 2, 17, 0, 4, 4, 0, True, True, True, False),
    (148, 225, 243, 220, 59, 29, 9, 20, 0, 6, 2, 0, False, False, True, False),
    (113, 252, 56, 95, 32, 27, 7, 15, 6, 1, 3, 1, False, True, True, True),
    (180, 255, 21, 158, 54, 14, 1, 26, 6, 4, 1, 5, True, False, False, True),
    (94, 4, 136, 86, 34, 22, 5, 24, 7, 5, 0, 2, True, True, False, True),
    (131, 47, 37, 93, 50, 16, 4, 18, 6, 0, 5, 3, True, False, True, True),
    (15, 176, 181, 44, 11, 19, 3, 30, 10, 2, 6, 6, False, False, False, True),
    (33, 107, 242, 70, 36, 13, 2, 11, 3, 2, 2, 5, True, True, False, True),
    (83, 173, 56, 219, 36, 14, 0, 22, 5, 3, 1, 6, True, False, False, True),
    (192, 216, 136, 248, 55, 24, 10, 21, 3, 2, 4, 5, True, False, True, True),
    (21, 99, 125, 158, 8, 24, 9, 24, 7, 2, 0, 4, False, False, False, False),
    (204, 90, 212, 208, 24, 28, 10, 18, 0, 5, 3, 6, False, False, False, True),
    (5, 7, 66, 38, 19, 27, 9, 17, 6, 4, 6, 2, False, True, False, False),
    (139, 215, 106, 8, 57, 16, 2, 12, 3, 4, 6, 5, True, True, False, False),
    (123, 183, 149, 31, 49, 19, 5, 17, 6, 4, 2, 2, True, True, True, True),
    (70, 34, 69, 49, 24, 18, 1, 22, 7, 3, 5, 0, False, False, False, False),
    (168, 216, 147, 126, 25, 18, 6, 27, 10, 1, 1, 3, False, False, True, True),
    (137, 215, 219, 156, 64, 18, 8, 17, 4, 0, 1, 0, True, True, False, False),
    (176, 130, 99, 226, 35, 13, 1, 19, 8, 4, 2, 6, False, True, False, True),
    (56, 77, 10, 141, 28, 18, 0, 12, 1, 0, 5, 4, False, True, False, False),
    (152, 183, 6, 69, 45, 21, 9, 15, 2, 6, 1, 5, False, False, True, True),
    (6, 13, 37, 132, 14, 21, 7, 13, 2, 2, 1, 0, False, True, True, False),
    (244, 75, 226, 220, 62, 16, 7, 13, 2, 5, 1, 4, False, True, False, False),
    (191, 15, 120, 180, 48, 22, 9, 19, 0, 6, 6, 0, True, True, False, True),
    (166, 186, 35, 78, 4, 13, 1, 24, 7, 6, 6, 4, True, False, True, False),
    (8, 50, 226, 64, 49, 17, 9, 25, 6, 4, 3, 2, False, True, False, False),
    (78, 158, 31, 189, 6, 9, 1, 19, 4, 6, 6, 4, True, True, False, True),
    (236, 69, 8, 206, 40, 19, 10, 24, 7, 0, 3, 5, True, False, True, True),
    (11, 12, 36, 147, 13, 12, 1, 10, 2, 1, 5, 0, True, True, False, True),
    (183, 28, 171, 74, 33, 18, 7, 11, 1, 4, 3, 6, True, False, False, True),
    (252, 250, 90, 221, 24, 29, 10, 20, 2, 4, 0, 6, True, True, True, True),
    (231, 193, 133, 205, 44, 29, 9, 15, 5, 6, 1, 2, False, True, False, False),
    (205, 19, 171, 99, 4, 14, 2, 19, 4, 5, 0, 4, False, True, True, False),
    (170, 240, 150, 97, 51, 21, 2, 17, 5, 5, 6, 3, True, False, False, False),
)
_TABLE_COLUMN_PROFILES = tuple(TableColumnProfile(*values) for values in _TABLE_COLUMN_PROFILE_VALUES)

_SEED_ROW_COUNTS = (
    44, 96, 78, 24, 23, 30, 68, 90, 18, 74, 78, 100, 77, 24, 46, 78,
    96, 83, 85, 66, 48, 56, 49, 44, 68, 86, 26, 34, 70, 60, 23, 87,
    31, 56, 27, 43, 19, 50, 84, 94, 14, 89, 28, 98, 55, 73, 10, 36,
    84, 81, 21, 43, 92, 55, 69, 27, 69, 42, 75, 68, 39, 82, 29, 55,
    63, 70, 97, 49, 87, 94, 99, 56, 67, 30, 36, 54, 60, 90, 37,
)


def table_column_profile(index: int) -> TableColumnProfile:
    try:
        return _TABLE_COLUMN_PROFILES[index]
    except IndexError as exc:
        raise ValueError(f"基表编号必须在 0 到 {TOTAL_TABLE_COUNT - 1} 之间") from exc


def table_column_count(index: int, *, seed: str = "0", expand_base_table_columns: bool = False) -> int:
    table_column_profile(index)
    if not expand_base_table_columns:
        return len(base_seed_columns())
    if index == 0:
        return MIN_TABLE_COLUMNS
    if index == 1:
        return MAX_TABLE_COLUMNS
    return derive_range(
        seed=seed,
        domain=f"table/{index}/column-count",
        minimum=MIN_TABLE_COLUMNS,
        maximum=MAX_TABLE_COLUMNS,
    )


_EXTRA_COLUMN_FAMILIES = (
    "integer",
    "decimal",
    "float",
    "datetime",
    "string",
    "binary",
    "enum_set",
    "bit",
    "json",
)
_INTEGER_TYPES = ("tinyint", "smallint", "mediumint", "int", "bigint")
_FLOAT_TYPES = ("float", "double")
_DATETIME_TYPES = ("date", "year", "datetime", "timestamp", "time")
_STRING_TYPES = ("char", "varchar", "tinytext", "text", "mediumtext", "longtext")
_BINARY_TYPES = ("binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob")


def _extra_domain(index: int, offset: int, purpose: str) -> str:
    return f"table/{index}/extra/{offset}/{purpose}"


def _integer_extra_column(index: int, offset: int, seed: str) -> tuple[str, str]:
    sql_type = pick(seed=seed, domain=_extra_domain(index, offset, "integer/type"), candidates=_INTEGER_TYPES)
    unsigned = derive_range(
        seed=seed,
        domain=_extra_domain(index, offset, "integer/unsigned"),
        minimum=0,
        maximum=1,
    ) == 1
    bias = derive_range(seed=seed, domain=_extra_domain(index, offset, "integer/bias"), minimum=0, maximum=997)
    if unsigned:
        return f"{sql_type} unsigned", f"MOD(`n` + {bias}, 200)"
    return sql_type, f"MOD(`n` + {bias}, 200) - 100"


def _decimal_extra_column(index: int, offset: int, seed: str) -> tuple[str, str]:
    scale = derive_range(seed=seed, domain=_extra_domain(index, offset, "decimal/scale"), minimum=0, maximum=8)
    precision = derive_range(
        seed=seed,
        domain=_extra_domain(index, offset, "decimal/precision"),
        minimum=max(scale + 1, 6),
        maximum=scale + 18,
    )
    bias = derive_range(seed=seed, domain=_extra_domain(index, offset, "decimal/bias"), minimum=0, maximum=9999)
    divisor = derive_range(seed=seed, domain=_extra_domain(index, offset, "decimal/divisor"), minimum=2, maximum=19)
    return f"decimal({precision},{scale})", f"ROUND((`n` + {bias}) / {divisor}, {scale})"


def _float_extra_column(index: int, offset: int, seed: str) -> tuple[str, str]:
    sql_type = pick(seed=seed, domain=_extra_domain(index, offset, "float/type"), candidates=_FLOAT_TYPES)
    unsigned = derive_range(
        seed=seed,
        domain=_extra_domain(index, offset, "float/unsigned"),
        minimum=0,
        maximum=1,
    ) == 1
    bias = derive_range(seed=seed, domain=_extra_domain(index, offset, "float/bias"), minimum=0, maximum=997)
    divisor = derive_range(seed=seed, domain=_extra_domain(index, offset, "float/divisor"), minimum=2, maximum=17)
    if unsigned:
        return f"{sql_type} unsigned", f"(`n` + {bias}) / {divisor}"
    return sql_type, f"((`n` + {bias}) / {divisor}) - 500"


def _datetime_extra_column(index: int, offset: int, seed: str) -> tuple[str, str]:
    sql_type = pick(seed=seed, domain=_extra_domain(index, offset, "datetime/type"), candidates=_DATETIME_TYPES)
    shift = derive_range(seed=seed, domain=_extra_domain(index, offset, "datetime/shift"), minimum=0, maximum=31535999)
    stride = derive_range(seed=seed, domain=_extra_domain(index, offset, "datetime/stride"), minimum=1, maximum=97)
    if sql_type == "date":
        return sql_type, f"DATE_ADD('2026-01-01', INTERVAL MOD(`n` * {stride} + {shift}, 365) DAY)"
    if sql_type == "year":
        return sql_type, f"2020 + MOD(`n` * {stride} + {shift}, 30)"
    fsp = derive_range(seed=seed, domain=_extra_domain(index, offset, "datetime/fsp"), minimum=0, maximum=6)
    rendered_type = f"{sql_type}({fsp})" if fsp else sql_type
    if sql_type == "time":
        return rendered_type, f"SEC_TO_TIME(MOD(`n` * {stride} + {shift}, 86400))"
    return rendered_type, f"TIMESTAMPADD(SECOND, `n` * {stride} + {shift}, '2026-01-01 10:11:12')"


def _string_extra_column(index: int, offset: int, seed: str) -> tuple[str, str, int | None]:
    sql_type = pick(seed=seed, domain=_extra_domain(index, offset, "string/type"), candidates=_STRING_TYPES)
    token = derive_range(seed=seed, domain=_extra_domain(index, offset, "string/token"), minimum=0, maximum=999999)
    if sql_type in ("char", "varchar"):
        length = derive_range(seed=seed, domain=_extra_domain(index, offset, "string/length"), minimum=1, maximum=16)
        rendered_type = f"{sql_type}({length}) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"
        return rendered_type, f"LEFT(CONCAT('s{token}_', LPAD(`n`, 4, '0')), {length})", length
    rendered_type = f"{sql_type} CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"
    return rendered_type, f"CONCAT('s{token}_', LPAD(`n`, 4, '0'))", None


def _binary_extra_column(index: int, offset: int, seed: str) -> tuple[str, str, int | None]:
    sql_type = pick(seed=seed, domain=_extra_domain(index, offset, "binary/type"), candidates=_BINARY_TYPES)
    token = derive_range(seed=seed, domain=_extra_domain(index, offset, "binary/token"), minimum=0, maximum=(1 << 32) - 1)
    expr = f"UNHEX(CONCAT(LPAD(HEX({token} + `n`), 8, '0'), REPEAT('00', 28)))"
    if sql_type in ("binary", "varbinary"):
        length = derive_range(seed=seed, domain=_extra_domain(index, offset, "binary/length"), minimum=1, maximum=32)
        return f"{sql_type}({length})", f"LEFT({expr}, {length})", length
    return sql_type, expr, None


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _enum_or_set_extra_column(index: int, offset: int, seed: str) -> tuple[str, str]:
    kind = pick(seed=seed, domain=_extra_domain(index, offset, "enum-set/type"), candidates=("enum", "set"))
    count = derive_range(seed=seed, domain=_extra_domain(index, offset, "enum-set/count"), minimum=2, maximum=5)
    token = derive_range(seed=seed, domain=_extra_domain(index, offset, "enum-set/token"), minimum=0, maximum=99)
    values = tuple(f"{kind[0]}{token}_{value}" for value in range(1, count + 1))
    sql_values = ",".join(_sql_string(value) for value in values)
    if kind == "enum":
        value_expr = f"ELT(MOD(`n` + {token}, {len(values)}) + 1, {sql_values})"
    else:
        value_expr = _sql_string(",".join(values[:2]))
    return f"{kind}({sql_values}) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin", value_expr


def _extra_column_spec(index: int, offset: int, seed: str) -> ExtraColumnSpec:
    family = pick(
        seed=seed,
        domain=_extra_domain(index, offset, "family"),
        candidates=_EXTRA_COLUMN_FAMILIES,
    )
    name = f"extra_t{index}_{offset:03d}"
    if family == "integer":
        sql_type, value_expr = _integer_extra_column(index, offset, seed)
        return ExtraColumnSpec(name, sql_type, value_expr)
    if family == "decimal":
        sql_type, value_expr = _decimal_extra_column(index, offset, seed)
        return ExtraColumnSpec(name, sql_type, value_expr)
    if family == "float":
        sql_type, value_expr = _float_extra_column(index, offset, seed)
        return ExtraColumnSpec(name, sql_type, value_expr)
    if family == "datetime":
        sql_type, value_expr = _datetime_extra_column(index, offset, seed)
        return ExtraColumnSpec(name, sql_type, value_expr)
    if family == "string":
        sql_type, value_expr, prefix_length = _string_extra_column(index, offset, seed)
        return ExtraColumnSpec(name, sql_type, value_expr, prefix_length)
    if family == "binary":
        sql_type, value_expr, prefix_length = _binary_extra_column(index, offset, seed)
        return ExtraColumnSpec(name, sql_type, value_expr, prefix_length)
    if family == "enum_set":
        sql_type, value_expr = _enum_or_set_extra_column(index, offset, seed)
        return ExtraColumnSpec(name, sql_type, value_expr)
    if family == "bit":
        length = derive_range(seed=seed, domain=_extra_domain(index, offset, "bit/length"), minimum=1, maximum=64)
        bit_value = derive_range(seed=seed, domain=_extra_domain(index, offset, "bit/value"), minimum=0, maximum=1)
        return ExtraColumnSpec(name, f"bit({length})", f"b'{bit_value}'")
    token = derive_range(seed=seed, domain=_extra_domain(index, offset, "json/token"), minimum=0, maximum=(1 << 32) - 1)
    return ExtraColumnSpec(name, "json", f"JSON_OBJECT('extra', '{token}', 'n', `n`)")


def extra_column_specs(index: int, *, seed: str) -> list[ExtraColumnSpec]:
    extra_count = table_column_count(index, seed=seed, expand_base_table_columns=True) - len(base_seed_columns())
    if extra_count < 0:
        raise ValueError(f"t{index} 目标列数小于核心列数")
    return [_extra_column_spec(index, offset, seed) for offset in range(extra_count)]


def table_kind(index: int) -> str:
    if index <= 1:
        return "normal"
    if index <= 6:
        return "temporary"
    if index < SUBPARTITION_START:
        return "partition"
    return "subpartition"


def range_partition_value(index: int, values: tuple[int, ...]) -> str:
    if index == len(values) - 1:
        return "MAXVALUE"
    return str(values[index] + 1)


def partition_expr(partition_type: str, column: str) -> str:
    return f"(`{column}`)"


def top_partition_definition(partition_type: str, index: int, subpartitions: list[str] | None = None) -> str:
    suffix = ""
    if partition_type.startswith("RANGE"):
        suffix = f" VALUES LESS THAN ({range_partition_value(index, TOP_PARTITION_VALUES)})"
    elif partition_type.startswith("LIST"):
        suffix = f" VALUES IN ({TOP_PARTITION_VALUES[index]})"
    if subpartitions is None:
        return f"  PARTITION p{index}{suffix}"
    inner = ",\n".join(f"    {line}" for line in subpartitions)
    return f"  PARTITION p{index}{suffix} (\n{inner}\n  )"


def subpartition_definition(partition_index: int, subpartition_index: int, subpartition_type: str) -> str:
    name = f"p{partition_index}sp{subpartition_index}"
    if subpartition_type.startswith("RANGE"):
        value = range_partition_value(subpartition_index, SUBPARTITION_VALUES)
        return f"SUBPARTITION {name} VALUES LESS THAN ({value})"
    if subpartition_type.startswith("LIST"):
        value = SUBPARTITION_VALUES[subpartition_index]
        return f"SUBPARTITION {name} VALUES IN ({value})"
    return f"SUBPARTITION {name}"


def partition_definitions(partition_type: str) -> str:
    parts = [top_partition_definition(partition_type, idx) for idx in range(len(TOP_PARTITION_VALUES))]
    return ",\n".join(parts)


def top_partition_clause(partition_type: str) -> str:
    expr = partition_expr(partition_type, "tenant_id")
    if partition_type in {"HASH", "LINEAR HASH", "KEY", "LINEAR KEY"}:
        return f"PARTITION BY {partition_type} {expr} PARTITIONS {len(TOP_PARTITION_VALUES)}"
    return f"""PARTITION BY {partition_type} {expr} (
{partition_definitions(partition_type)}
)"""


def subpartition_pair(index: int) -> tuple[str, str]:
    pair_index = index - SUBPARTITION_START
    outer = PARTITION_TYPES[pair_index // len(PARTITION_TYPES)]
    inner = PARTITION_TYPES[pair_index % len(PARTITION_TYPES)]
    return outer, inner


def composite_partition_definitions(outer_type: str, inner_type: str) -> str:
    parts = []
    for partition_index in range(len(TOP_PARTITION_VALUES)):
        subpartitions = None
        if inner_type.startswith(("RANGE", "LIST")):
            subpartitions = [
                subpartition_definition(partition_index, subpartition_index, inner_type)
                for subpartition_index in range(len(SUBPARTITION_VALUES))
            ]
        parts.append(top_partition_definition(outer_type, partition_index, subpartitions))
    return ",\n".join(parts)


def subpartition_clause(outer_type: str, inner_type: str) -> str:
    outer_expr = partition_expr(outer_type, "tenant_id")
    inner_expr = partition_expr(inner_type, "subpart_id")
    subpartition_count = (
        f" SUBPARTITIONS {len(SUBPARTITION_VALUES)}"
        if inner_type in {"HASH", "LINEAR HASH", "KEY", "LINEAR KEY"}
        else ""
    )
    return f"""PARTITION BY {outer_type} {outer_expr}
SUBPARTITION BY {inner_type} {inner_expr}{subpartition_count} (
{composite_partition_definitions(outer_type, inner_type)}
)"""


def partition_clause(index: int, include_subpartition: bool = True) -> str:
    if table_kind(index) == "partition":
        return top_partition_clause(PARTITION_TYPES[index - FIRST_PARTITION_START])
    outer, inner = subpartition_pair(index)
    if not include_subpartition:
        return top_partition_clause(outer)
    return subpartition_clause(outer, inner)


NORMAL_OR_TEMPORARY_UNIQUE_INDEXES = frozenset({
    "idx_int_col",
    "idx_bigint_desc",
    "idx_tiny_small_medium",
    "idx_decimal_float_double",
    "idx_date_time_mix",
    "idx_blob_prefix",
    "idx_text_prefix",
    "idx_unsigned_desc",
    "idx_arith_expr",
    "idx_json_expr",
    "idx_extra_tenant_int",
    "idx_extra_subpart_big",
    "idx_extra_tenant_year_date",
    "idx_extra_small_medium_desc",
    "idx_extra_decimal_desc",
    "idx_extra_float_double_desc",
    "idx_extra_datetime_date_desc",
    "idx_extra_time_timestamp",
    "idx_extra_tinyblob",
    "idx_extra_blob_tenant",
    "idx_extra_mediumblob",
    "idx_extra_longblob",
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
})

PARTITION_UNIQUE_INDEXES = frozenset({
    "idx_extra_tenant_int",
    "idx_extra_tenant_year_date",
    "idx_extra_blob_tenant",
})


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


_SUPPLEMENTAL_INDEX_PERMUTATIONS = (
    "22022421181d0f1b061e01080b160e0319151c0c070405231a0d1017142500122009131f0a11",
    "021e1521230e061c18081d051320000d0410240a19121a1114250b0701090c22161f0f17031b",
    "201d1c1e0f130d252422030e23181015040b0105021b0814121117060016091f210c1a070a19",
    "0d1e251203211c100f1418220b04170120020a0809231511161d24191f061a000e071b05130c",
    "1e050c06230402011103100f200d210824180a220b161907251f1d1c131b12170009150e141a",
    "19101d03231e2422060025040c020f1411151809121c1b210b13161708071f0a0d0120050e1a",
    "13140a191b1c08070023040b03220c160f171511210e06241d0518201a0d12010209251f1e10",
    "04120e1a14211b2519241f201c1607131d0f0b1e021523170011100c0a0608180503220d0109",
    "0b1f211b0d1613141a000415020f25121117080a1c1d1009060e200c07181924231e01050322",
    "071905030f082117060a1e021413011a16111c1218220c2310201d1b251f04150e0d0924000b",
    "1b1f0f140002161c050c1706150e01221019072023111e120304180821250b1d1a130a24090d",
    "14020024251d1f09211610111723180a011503200b1904120e050d0f1a2207131c1e081b0c06",
    "082015181b0f090d042213000702111c190a10160b06121417240c1d1a0e05231f031e012125",
    "1902000d051a0312171806161e11070b0c04130a0f201c1008251f1d1401230e242221151b09",
    "031f1d0522171910070a0c110616151a010b2021231209021e1c131425180e080424000d0f1b",
    "1e02120c031022041b23190e110f211d1609171c012018251305080d060b0a1f000715141a24",
    "1b00231c24181f09081216150d1025140e0c201a0f01021d0413170b1e07190a110622052103",
    "210517000a03101f1618070b201b1506082513041d0d0f191e0c24231209221c01111a02140e",
    "210f1a0c1714220a1d1918020723160e12080b0901041e240306201b1525100d1c110005131f",
    "0e210d061c19101f111a221423120a041e0215131d0c2408071b09250f2018171600010b0503",
    "052006241e2119111c0a13160008150c171d040d1001230712090f1a03142502181f0b1b220e",
    "0b14041d170c1b222415130318120a0d191101251006001c05081620022123091f0e1e071a0f",
    "02091d172325190b0d001214031807240e201508211a1f220f0401050c160a101c061e13111b",
    "1b19060124140f21111c1f131d12230809100a200d1a0e160400050b1e07152518031702220c",
    "0f0b1e130825211c1807100e090c1b0301000a1d20222314121f11040605020d19171a151624",
    "250f0e09121f11041b1805072114221e1913171a0a0316001d0b1c0820020d150c0624011023",
    "1225000d0a170c1e21141004011c1b151d240211132208180e19060b0f1f050716091a032023",
    "1f19030d120216100b070e141d061c0a252000222115131b17241a2308010911050c18040f1e",
    "0617220b16080c13241d1220181519100d1423050311010a02040f07001b251f091c0e1e211a",
    "0e1f241912211c1a05000d1e1023111716030c091806020b22041320011d1b140708250f150a",
    "0d0b200a10031a0f17011319111d14151f2122091e250804230e060c24051c12071b18001602",
    "0e0d1e15001a0f250118161c0c07241f1d192217061b03081012040b210213140920050a2311",
    "0a0501120625160d1115171b030219230c18040007131a1e20220b0e211f10081d0f09241c14",
    "000a02240513111a1d210812031f061b150d100e171e0b1c01181619092023252204070f0c14",
    "16171405010e021f1920061e1d1a241b131122040c1c150b25231009120018030a080d0f2107",
    "0506091b19101517110b240223210a1301121e14180c160e001c03080f1f040d1a2507201d22",
    "20231f210d142219060e0f081a16100b24001b051e0703131809041d251c11121502170a0c01",
    "230a05040312191d0f111c25151e021000170b0908201a0e220701060d141b1f16182113240c",
    "20240914210a12171f231625110e0406130b181b100815050002220c070d0119031e0f1a1d1c",
    "221d032317020f04241e0d1220061311100e00190807151c160b250c181b141f1a090a010521",
    "0f010907101f200d13191e120806111c02250c2218170e0b2316211a032414000a151b051d04",
    "191223170e02081a1311160b15002106200a01070f091b1c05250d1f14100422031e240c1d18",
    "0b1908091a12030c18240e150d1f06042005221d10161c0a131e010217211b0725110f002314",
    "101e191c25160e1b090701110a040306131a170b081d1802210c150020140523220f0d121f24",
    "19161822200b0308021c15140d23071b131e1109121f040e060a211d01240f17100025051a0c",
    "06100523222504011407130d240b1a090f1e1815021f20160c12210a170e1d081b191c001103",
    "051104102307131901151e17030c0800181c21060b24140a251b12090d1d1f200e220f02161a",
    "130a10140316021b11081e09180c0e1d05000f0723240617250b15190d1c1f2001121a210422",
    "1f200f1a1d2201240e0206112107181e2310150c030a000b1708141c041613091225190d051b",
    "1f171a21120c1923130024141b18251e09020b0811061d0d040e012016070a100322051c0f15",
    "020a1f20211e1810171b0c1125190b230f0e04001d0515221309010d031416071a2412061c08",
    "2402201b0607131e0a0e11030d1716040c15210119252300180f1f221c0b081a1d1205100914",
    "021a23170c011f07151200241d110f0a0e20031910082213161e250b14051c060904211b180d",
    "120d02251b11001e141c18231305080b0c01210407090f16190e240a1a03221006201d1f1715",
    "0f230e00071a031e1601121d0d08250910130c0a1b18170522020b241c14211f190411061520",
    "140f210218240a1f040b030d0e22230705121d17201615111b1925131a08011e101c000c0906",
    "031f0b0e12241600081a21251119231815051d1e07061c090117220c141b20020d100f040a13",
    "1e1d0b240e07121721131b051a160f040310080c0a2502150006111f191c2322140d01201809",
    "0f1318160119040822120609110a1d051e030e1a10140d021b0b1c2123001524250717201f0c",
    "0c091a0705001118211c23010d10251f0a1e0e1613040312241d172008060219141b0f150b22",
    "0c0723160d180e0b1a05031d0a22112401171c0419101f141e090f1b15060820211200022513",
    "211006190d240423201c08250c071b16051e18120e0015030a1d01141a0f221113091f17020b",
    "060f0e1a1505001c140d231308190c24251d220a1801040220211b12100b1e09160317111f07",
    "021d030a100805001813041c1e0d201a162101190c140623111f221b12090e0f0b0725172415",
    "0c0d07081a210924141e1d1b050b2522000a131811190f160e041c2302011703101f06151220",
    "18071900200211050c210124131b1008251c230a12221d150d031a1e0e06170f14161f09040b",
    "240f161b1c11041e10020922140a20080e121705070d1d180b0c03192315010006211f1a1325",
    "1b190412140b171f020f0d110625181c2023071310081d24091a16051522000c210e1e03010a",
    "000c22170d08071304101b03111624180e251e1c211505121d090b140a061a20021f2301190f",
    "251807001c0c130f0a24030e0b1a1b080915052201171404160d1206021f101d212011231e19",
    "0f2401221f1a1d191e0c0a1712080415091825201c1314110d0e0b1b03002306021610210507",
    "22111f0d0c1517251d0804100a03161c09012319180e0f05142106201b0007241a1e0213120b",
    "0002141a010c24160520130725211c1f0b081b0d2218191e1d10040e11030923120a060f1517",
    "23241d11140f17131a19020c0a05161f2022030d0e211b071e0415102500080b06011c181209",
    "22160e07191c11201a150f030d1221020518001e1f0b09241d1b1401082306101713250c0a04",
    "082002162206132324170b1805000c0415110d1f1a1b191c011e0e07031d09212512100f0a14",
    "2408001115221e092001210f0d0a181d0e1a0c230b142505121703161f0702041b10131c0619",
    "0611131a02170f1410151c0b090c1e0d042403121d0e211f0a051820250800221b1619072301",
    "1c12130e1514010d10091b201a0a21170723240b06080f18110c1e041d022200031f05251619",
)


def supplemental_index_lines(
    index: int,
    target_sql_index_count: int,
    include_subpartition: bool = True,
    profile: TableColumnProfile | None = None,
) -> list[str]:
    profile = profile or table_column_profile(index)
    extra_varchar_prefix_length = min(32, profile.varchar_length)
    extra_varbinary_prefix_length = min(32, profile.varbinary_length)
    index_lines = [
        key_line(index, f"idx_t{index}_extra_tenant_int", "(`tenant_id`,`int_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_subpart_big", "(`subpart_id`,`bigint_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_tenant_year_date", "(`tenant_id`,`year_col`,`date_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_char_varchar", f"(`char_col`,`varchar_col`({extra_varchar_prefix_length}))", include_subpartition),
        key_line(index, f"idx_t{index}_extra_tiny_bool", "(`tinyint_col`,`bool_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_small_medium_desc", "(`smallint_col` DESC,`mediumint_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_decimal_desc", "(`decimal_col` DESC)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_float_double_desc", "(`float_col`,`double_col` DESC)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_datetime_date_desc", "(`datetime_col`,`date_col` DESC)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_time_timestamp", "(`time_col`,`timestamp_col`)", include_subpartition),
        key_line(index, f"idx_t{index}_extra_varbinary_binary", f"(`varbinary_col`({extra_varbinary_prefix_length}),`binary_col`)", include_subpartition),
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
    permutation = bytes.fromhex(_SUPPLEMENTAL_INDEX_PERMUTATIONS[index])
    return [index_lines[position] for position in permutation[:missing_count]]


def extra_column_definition_line(spec: ExtraColumnSpec) -> str:
    lower_type = spec.sql_type.lower()
    if "text" in lower_type or "blob" in lower_type:
        return f"  `{spec.name}` {spec.sql_type},"
    return f"  `{spec.name}` {spec.sql_type} DEFAULT NULL,"


def create_table_sql(
    index: int,
    include_subpartition: bool = True,
    *,
    seed: str = "0",
    expand_base_table_columns: bool = False,
) -> str:
    kind = table_kind(index)
    profile = table_column_profile(index)
    extra_columns = extra_column_specs(index, seed=seed) if expand_base_table_columns else []
    create_keyword = "CREATE TEMPORARY TABLE" if kind == "temporary" else "CREATE TABLE"
    drop_keyword = "DROP TEMPORARY TABLE" if kind == "temporary" else "DROP TABLE"
    supplemental_indexes = supplemental_index_lines(index, TARGET_TOTAL_INDEX_COUNT, include_subpartition, profile)
    varchar_prefix_length = min(64, profile.varchar_length)
    tinyint_modifier = " unsigned" if profile.tinyint_unsigned else ""
    mediumint_modifier = " unsigned" if profile.mediumint_unsigned else ""
    float_modifier = " unsigned" if profile.float_unsigned else ""
    double_modifier = " unsigned" if profile.double_unsigned else ""
    datetime_type = f"datetime({profile.datetime_fsp})" if profile.datetime_fsp > 0 else "datetime"
    timestamp_type = f"timestamp({profile.timestamp_fsp})" if profile.timestamp_fsp > 0 else "timestamp"
    time_type = f"time({profile.time_fsp})" if profile.time_fsp > 0 else "time"
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
        f"  `char_col` char({profile.char_length}) DEFAULT NULL,",
        f"  `tinyint_col` tinyint{tinyint_modifier} DEFAULT NULL,",
        "  `bool_col` tinyint(1) DEFAULT NULL,",
        "  `smallint_col` smallint DEFAULT NULL,",
        f"  `mediumint_col` mediumint{mediumint_modifier} DEFAULT NULL,",
        f"  `decimal_col` decimal({profile.decimal_precision},{profile.decimal_scale}) unsigned DEFAULT NULL,",
        f"  `float_col` float{float_modifier} DEFAULT NULL,",
        f"  `double_col` double{double_modifier} DEFAULT NULL,",
        "  `date_col` date DEFAULT NULL,",
        f"  `datetime_col` {datetime_type} DEFAULT NULL,",
        f"  `timestamp_col` {timestamp_type} NULL DEFAULT NULL,",
        f"  `time_col` {time_type} DEFAULT NULL,",
        f"  `varchar_col` varchar({profile.varchar_length}) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL,",
        f"  `binary_col` binary({profile.binary_length}) DEFAULT NULL,",
        f"  `varbinary_col` varbinary({profile.varbinary_length}) DEFAULT NULL,",
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
        f"  `bit_col` bit({profile.bit_length}) DEFAULT b'1',",
        "  `unsigned_int_col` int unsigned DEFAULT NULL,",
        f"  `unsigned_decimal_col` decimal({profile.unsigned_decimal_precision},{profile.unsigned_decimal_scale}) unsigned DEFAULT NULL,",
        "  `json_col` json DEFAULT NULL,",
        *[extra_column_definition_line(spec) for spec in extra_columns],
        "  PRIMARY KEY (`id_col`,`tenant_id`,`subpart_id`),",
        f"  UNIQUE KEY `uk_t{index}_ref_id` (`tenant_id`,`subpart_id`,`id_col`),",
        f"  UNIQUE KEY `uk_t{index}_metric_ref` (`tenant_id`,`subpart_id`,`int_col`,`bigint_col`),",
        f"  KEY `idx_t{index}_char_scope` (`tenant_id`,`subpart_id`,`char_col`),",
        key_line(index, f"idx_t{index}_parent_id", "(`parent_tenant_id`,`parent_subpart_id`,`parent_id_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_parent_metric", "(`metric_parent_tenant_id`,`metric_parent_subpart_id`,`parent_int_col`,`parent_bigint_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_int_col", "(`int_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_bigint_desc", "(`bigint_col` DESC)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_year_char", "(`year_col`,`char_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_tiny_small_medium", "(`tinyint_col`,`smallint_col`,`mediumint_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_decimal_float_double", "(`decimal_col`,`float_col`,`double_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_date_time_mix", "(`date_col`,`datetime_col` DESC,`timestamp_col`,`time_col`)", include_subpartition) + ",",
        key_line(index, f"idx_t{index}_varchar_prefix", f"(`varchar_col`({varchar_prefix_length}))", include_subpartition) + ",",
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
    try:
        return _SEED_ROW_COUNTS[index]
    except IndexError as exc:
        raise ValueError(f"基表编号必须在 0 到 {TOTAL_TABLE_COUNT - 1} 之间") from exc


def max_seed_row_count() -> int:
    return max(seed_row_count(index) for index in range(TOTAL_TABLE_COUNT))


def tenant_expr(index: int) -> str:
    if index >= 7:
        return f"((`n` - 1) % {len(TOP_PARTITION_VALUES)}) + 1"
    return "1"


def subpart_expr(index: int) -> str:
    if index >= SUBPARTITION_START:
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


def base_seed_columns() -> list[str]:
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


def seed_columns(
    index: int,
    *,
    seed: str = "0",
    expand_base_table_columns: bool = False,
) -> list[str]:
    extra_columns = extra_column_specs(index, seed=seed) if expand_base_table_columns else []
    return [*base_seed_columns(), *[spec.name for spec in extra_columns]]


def unique_binary_expr(index: int, multiplier: int = 100000) -> str:
    return f"UNHEX(CONCAT(LPAD(HEX({index} * {multiplier} + `n`), 8, '0'), REPEAT('00', 28)))"


def unique_text_expr(index: int, label: str) -> str:
    return f"CONCAT('r', LPAD(`n`, 6, '0'), '_{label}_{index}')"


def seed_value_exprs(
    index: int,
    *,
    seed: str = "0",
    expand_base_table_columns: bool = False,
) -> list[str]:
    profile = table_column_profile(index)
    tinyint_modulus = 255 if profile.tinyint_unsigned else 127
    values = [
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
        f"LEFT(CONCAT('c', LPAD(CONV(`n`, 10, 36), 3, '0')), {profile.char_length})",
        f"MOD({index} + `n`, {tinyint_modulus})",
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
        f"LEFT(CONCAT('v', LPAD(`n`, 6, '0'), '_t{index}'), {profile.varchar_length})",
        "UNHEX(LPAD(HEX(MOD(`n`, 256)), 2, '0'))",
        f"LEFT({unique_binary_expr(index)}, {profile.varbinary_length})",
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
        "b'1'",
        f"1000000 + {index} * 100000 + `n`",
        f"2000000 + {index} * 100000 + `n`",
        f"JSON_OBJECT('k', CONCAT('json_{index}_', `n`), 'n', `n`)",
    ]
    if expand_base_table_columns:
        values.extend(spec.value_expr for spec in extra_column_specs(index, seed=seed))
    return values


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


def insert_sql(
    index: int,
    *,
    seed: str = "0",
    expand_base_table_columns: bool = False,
) -> str:
    col_sql = ",".join(
        f"`{column}`"
        for column in seed_columns(
            index,
            seed=seed,
            expand_base_table_columns=expand_base_table_columns,
        )
    )
    select_sql = ",\n  ".join(
        seed_value_exprs(
            index,
            seed=seed,
            expand_base_table_columns=expand_base_table_columns,
        )
    )
    row_count = seed_row_count(index)
    return f"""/* t{index}:rows={row_count} */
INSERT INTO `t{index}` ({col_sql})
SELECT
  {select_sql}
FROM `{SEED_NUMBER_TABLE}`
WHERE `n` <= {row_count};"""


def seed_sql(*, seed: str = "0", expand_base_table_columns: bool = False) -> str:
    inserts = [
        insert_sql(index, seed=seed, expand_base_table_columns=expand_base_table_columns)
        for index in range(TOTAL_TABLE_COUNT)
    ]
    lines = [
        "SET transaction_isolation = 'READ-COMMITTED';",
        "SET FOREIGN_KEY_CHECKS=0;",
        seed_number_table_sql(),
        *[f"DELETE FROM `t{index}`;" for index in range(TOTAL_TABLE_COUNT - 1, -1, -1)],
        "SET FOREIGN_KEY_CHECKS=1;",
        *inserts,
    ]
    return "\n".join(lines) + "\n"


def execution_doc(
    include_subpartition: bool = True,
    *,
    expand_base_table_columns: bool = False,
    seed: str = "0",
) -> str:
    table_files = [f"t{index}.sql" for index in range(TOTAL_TABLE_COUNT)]
    compatibility_note = (
        "本目录下的 SQL 文件面向支持扩展二级分区能力的 MySQL 内核生成，不包含向量类型、向量索引列备注或向量种子表达式。"
        if include_subpartition
        else "本目录是关闭二级分区的离线兼容变体，不属于标准 `v1 + seed` 内存包或其金标身份。"
    )
    column_note = (
        f"- 当前是扩展列模式：使用冻结生成器 `v1` 和种子 `{seed}`，每张基表包含 {MIN_TABLE_COLUMNS} 到 {MAX_TABLE_COLUMNS} 列；`t0`、`t1` 分别覆盖 200、500 列边界。"
        if expand_base_table_columns
        else f"- 当前是默认核心模式：每张基表恰好包含 {len(base_seed_columns())} 个核心列，不生成 `extra_tN_NNN` 扩展列。"
    )
    column_coverage_note = (
        f"- 扩展列数量、类型参数和值表达式由冻结生成器 `v1` 与规范种子 `{seed}` 决定，列数范围为 {MIN_TABLE_COLUMNS} 到 {MAX_TABLE_COLUMNS}。"
        if expand_base_table_columns
        else f"- 每张表固定生成 {len(base_seed_columns())} 个核心列；核心列类型参数、索引和种子值均已冻结。"
    )
    subpartition_note = (
        f"4. 执行二级分区表：`t{SUBPARTITION_START}.sql` 到 `t{TOTAL_TABLE_COUNT - 1}.sql`。"
        if include_subpartition
        else f"4. 执行由二级分区降级为一级分区的兼容表：`t{SUBPARTITION_START}.sql` 到 `t{TOTAL_TABLE_COUNT - 1}.sql`。"
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
        "- 当前输出不包含向量列。",
        "- `t2.sql` 到 `t6.sql` 是临时表，必须和 `zz_seed_fk_data.sql` 在同一个 session 内执行。",
        "- `t2.sql` 到 `t6.sql` 是临时表，只保留父表引用列和种子数据关系，不声明 `FOREIGN KEY`，避免 InnoDB 在建临时表时报 1215。",
        (
            f"- `t{FIRST_PARTITION_START}.sql` 到 `t{TOTAL_TABLE_COUNT - 1}.sql` 是分区表，只保留父表引用列、关联索引和种子数据关系，不声明 `FOREIGN KEY`。"
            if include_subpartition
            else f"- `t{FIRST_PARTITION_START}.sql` 到 `t{TOTAL_TABLE_COUNT - 1}.sql` 是一级分区兼容表；`t{SUBPARTITION_START}.sql` 到 `t{TOTAL_TABLE_COUNT - 1}.sql` 不生成 `SUBPARTITION BY`，仍不声明 `FOREIGN KEY`。"
        ),
        "- 每个建表文件都会短暂执行 `SET FOREIGN_KEY_CHECKS=0;`，建表完成后恢复为 `SET FOREIGN_KEY_CHECKS=1;`。",
        f"- `zz_seed_fk_data.sql` 会创建 `{SEED_NUMBER_TABLE}` 辅助数字表，先按依赖反序清理数据，再恢复外键检查并为每张表插入 {MIN_SEED_ROWS} 到 {MAX_SEED_ROWS} 行可复现随机数据。",
        column_note,
        "- 当前基表不生成空间类型列、空间索引和空间构造函数，避免目标 InnoDB 内核报 1178。",
        "",
        "## 推荐执行顺序",
        "",
        "1. 执行普通父表：`t0.sql`、`t1.sql`。",
        "2. 在同一个 session 中执行临时表：`t2.sql`、`t3.sql`、`t4.sql`、`t5.sql`、`t6.sql`。",
        f"3. 执行一级分区表：`t{FIRST_PARTITION_START}.sql` 到 `t{SUBPARTITION_START - 1}.sql`。",
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
            column_coverage_note,
            f"- 每张表通过固定 seed 决定插入 {MIN_SEED_ROWS} 到 {MAX_SEED_ROWS} 行，生成结果可复现。",
            "- 可安全唯一化的普通索引会生成 `UNIQUE KEY`；二级分区表默认只保留已有唯一键，避免违反 MySQL 分区唯一键必须包含全部分区列的限制。",
            f"- `t{FIRST_PARTITION_START}.sql` 到 `t{SUBPARTITION_START - 1}.sql` 覆盖 8 种一级分区类型，种子数据使用 `tenant_id` 1 到 8，保证每个一级分区都有数据。",
            (
                f"- `t{SUBPARTITION_START}.sql` 到 `t{TOTAL_TABLE_COUNT - 1}.sql` 覆盖 8 x 8 共 64 种二级分区组合，每张表有 8 个一级分区；种子数据写入 `subpart_id` 1 到 8，用于覆盖一级分区和子分区路由。"
                if include_subpartition
                else f"- `t{SUBPARTITION_START}.sql` 到 `t{TOTAL_TABLE_COUNT - 1}.sql` 在兼容输出中降级为一级分区表，种子数据仍保留 `subpart_id` 1 到 8 取值。"
            ),
            "- 父表引用数据固定指向 `t0` 或 `t1`，避免永久表引用临时表造成生命周期不稳定。",
            "- `t0.sql` 和 `t1.sql` 是普通 InnoDB 表，不创建向量索引；其中 `t1.sql` 声明实际 `FOREIGN KEY` 约束。",
            "- `t2.sql` 到 `t6.sql` 是临时表，不包含向量列，也不声明实际外键。",
            f"- `t{FIRST_PARTITION_START}.sql` 到 `t{TOTAL_TABLE_COUNT - 1}.sql` 是分区表，不包含向量列，也不声明实际外键。",
            "- 按 InnoDB 每表最多 64 个二级索引计算，每张表补齐到索引上限数减 3，即 61 个索引；`PRIMARY KEY` 不计入该数量。",
            "- 输出不包含向量索引，每张表均为 61 个常规二级索引。",
        ]
    )
    return "\n".join(lines) + "\n"


def render_sql_files(
    seed: str,
    *,
    expand_base_table_columns: bool,
    include_subpartition: bool = True,
) -> tuple[BaseSqlFile, ...]:
    """纯内存渲染固定顺序的 80 个逻辑 SQL 文件。"""

    table_files = tuple(
        BaseSqlFile(
            path=Path(f"t{index}.sql"),
            sql=create_table_sql(
                index,
                include_subpartition=include_subpartition,
                seed=seed,
                expand_base_table_columns=expand_base_table_columns,
            ),
        )
        for index in range(TOTAL_TABLE_COUNT)
    )
    return (
        *table_files,
        BaseSqlFile(
            path=Path("zz_seed_fk_data.sql"),
            sql=seed_sql(seed=seed, expand_base_table_columns=expand_base_table_columns),
        ),
    )


def generate_base_sql_bundle(seed: str, *, expand_base_table_columns: bool) -> BaseSqlBundle:
    """生成标准 `v1` 内存包；标准身份始终包含二级分区。"""

    files = render_sql_files(
        seed,
        expand_base_table_columns=expand_base_table_columns,
        include_subpartition=True,
    )
    return build_base_sql_bundle(
        files,
        expand_base_table_columns=expand_base_table_columns,
        generator_version="v1" if expand_base_table_columns else None,
        seed=seed if expand_base_table_columns else None,
    )


__all__ = [
    "ExtraColumnSpec",
    "MAX_TABLE_COLUMNS",
    "MIN_TABLE_COLUMNS",
    "PARTITION_TYPES",
    "TableColumnProfile",
    "TOTAL_TABLE_COUNT",
    "base_seed_columns",
    "create_table_sql",
    "execution_doc",
    "extra_column_specs",
    "generate_base_sql_bundle",
    "insert_sql",
    "render_sql_files",
    "seed_columns",
    "seed_row_count",
    "seed_sql",
    "seed_value_exprs",
    "table_column_count",
    "table_column_profile",
]
