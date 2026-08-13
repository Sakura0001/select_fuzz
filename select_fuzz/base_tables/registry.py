"""基表生成器版本注册、种子规范化和稳定序列化入口。"""

from __future__ import annotations

import re
import struct
from typing import Callable

from . import v1
from .models import BaseSqlBundle


CURRENT_BASE_TABLE_GENERATOR_VERSION = "v1"
MAX_BASE_TABLE_SEED = (1 << 64) - 1

_Generator = Callable[[str], BaseSqlBundle]


def _generate_v1(seed: str) -> BaseSqlBundle:
    return v1.generate_base_sql_bundle(seed, expand_base_table_columns=True)


_GENERATORS: dict[str, _Generator] = {"v1": _generate_v1}

_V1_TABLE_COUNT = 79
_V1_EXPECTED_FILE_NAMES = (
    *(f"t{index}.sql" for index in range(_V1_TABLE_COUNT)),
    "zz_seed_fk_data.sql",
)
_V1_EXPECTED_TABLE_NAMES = tuple(f"t{index}" for index in range(_V1_TABLE_COUNT))
_V1_CORE_COLUMN_NAMES = (
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
)
_V1_SEED_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+`(?P<table>t\d+)`\s*"
    r"\((?P<columns>.*?)\)\s*SELECT\s+"
    r"(?P<values>.*?)\s+FROM\s+`_select_fuzz_seed_numbers`",
    re.IGNORECASE | re.DOTALL,
)
_V1_QUOTED_IDENTIFIER_RE = re.compile(r"`(?P<name>[A-Za-z_][A-Za-z0-9_$]*)`")


def _v1_validation_error(detail: str) -> RuntimeError:
    return RuntimeError(f"v1 扩展基表包结构校验失败：{detail}")


def _split_v1_top_level_csv(text: str, *, location: str) -> tuple[str, ...]:
    """稳定拆分 v1 INSERT 列和值，不把函数或字符串内的逗号当作分隔符。"""

    parts: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    index = 0
    while index < len(text):
        character = text[index]
        if quote is not None:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                raise _v1_validation_error(f"{location} 括号不完整")
            depth -= 1
        elif character == "," and depth == 0:
            part = text[start:index].strip()
            if not part:
                raise _v1_validation_error(f"{location} 存在空项")
            parts.append(part)
            start = index + 1
        index += 1

    if quote is not None or depth != 0:
        raise _v1_validation_error(f"{location} 引号或括号不完整")
    final_part = text[start:].strip()
    if not final_part:
        raise _v1_validation_error(f"{location} 存在空项")
    parts.append(final_part)
    return tuple(parts)


def _parse_v1_insert_columns(text: str, *, table_name: str) -> tuple[str, ...]:
    columns = _split_v1_top_level_csv(text, location=f"{table_name} 种子 INSERT 列表")
    names = []
    for column in columns:
        match = _V1_QUOTED_IDENTIFIER_RE.fullmatch(column)
        if match is None:
            raise _v1_validation_error(f"{table_name} 种子 INSERT 列名格式无效：{column}")
        names.append(match.group("name"))
    return tuple(names)


def _validate_v1_expanded_bundle(bundle: BaseSqlBundle, *, version: str, seed: str) -> None:
    """在版本注册边界独立校验标准 v1 扩展包的完整结构契约。"""

    if not isinstance(bundle, BaseSqlBundle):
        raise _v1_validation_error("生成器未返回 BaseSqlBundle")
    if bundle.expand_base_table_columns is not True:
        raise _v1_validation_error("扩展模式标记必须为 true")
    if bundle.generator_version != version:
        raise _v1_validation_error(
            f"生成器版本应为 {version}，实际为 {bundle.generator_version}"
        )
    if bundle.seed != seed:
        raise _v1_validation_error(f"基表种子应为 {seed}，实际为 {bundle.seed}")

    file_names = tuple(sql_file.path.name for sql_file in bundle.files)
    if file_names != _V1_EXPECTED_FILE_NAMES:
        raise _v1_validation_error(
            f"文件名顺序必须精确为 t0.sql 到 t78.sql 及 zz_seed_fk_data.sql，实际共 {len(file_names)} 个"
        )

    table_names = tuple(table.name for table in bundle.tables)
    if table_names != _V1_EXPECTED_TABLE_NAMES:
        raise _v1_validation_error(
            f"表名顺序必须精确为 t0 到 t78，实际为 {table_names}"
        )

    for table_index, table in enumerate(bundle.tables):
        table_name = f"t{table_index}"
        column_names = tuple(table.columns)
        column_count = len(column_names)
        if not v1.MIN_TABLE_COLUMNS <= column_count <= v1.MAX_TABLE_COLUMNS:
            raise _v1_validation_error(
                f"{table_name} 列数必须在 {v1.MIN_TABLE_COLUMNS} 到 {v1.MAX_TABLE_COLUMNS} 之间，实际为 {column_count}"
            )
        if table_index == 0 and column_count != v1.MIN_TABLE_COLUMNS:
            raise _v1_validation_error(f"t0 列数必须为 {v1.MIN_TABLE_COLUMNS}，实际为 {column_count}")
        if table_index == 1 and column_count != v1.MAX_TABLE_COLUMNS:
            raise _v1_validation_error(f"t1 列数必须为 {v1.MAX_TABLE_COLUMNS}，实际为 {column_count}")
        if column_names[: len(_V1_CORE_COLUMN_NAMES)] != _V1_CORE_COLUMN_NAMES:
            raise _v1_validation_error(f"{table_name} 核心列顺序不符合 v1 契约")
        expected_extra_names = tuple(
            f"extra_t{table_index}_{offset:03d}"
            for offset in range(column_count - len(_V1_CORE_COLUMN_NAMES))
        )
        if column_names[len(_V1_CORE_COLUMN_NAMES) :] != expected_extra_names:
            raise _v1_validation_error(
                f"{table_name} 扩展列顺序必须从 extra_t{table_index}_000 连续编号且数量与总列数吻合"
            )
        metadata_column_names = tuple(column.name for column in table.columns.values())
        if metadata_column_names != column_names:
            raise _v1_validation_error(f"{table_name} 列字典键与列元数据名称不一致")

    seed_sql = bundle.files[-1].sql
    insert_matches = tuple(_V1_SEED_INSERT_RE.finditer(seed_sql))
    insert_table_names = tuple(match.group("table") for match in insert_matches)
    if insert_table_names != _V1_EXPECTED_TABLE_NAMES:
        raise _v1_validation_error(
            f"种子 INSERT 表名顺序必须精确为 t0 到 t78，实际共 {len(insert_table_names)} 条"
        )
    for table_index, match in enumerate(insert_matches):
        table_name = f"t{table_index}"
        insert_columns = _parse_v1_insert_columns(
            match.group("columns"),
            table_name=table_name,
        )
        expected_columns = tuple(bundle.tables[table_index].columns)
        if insert_columns != expected_columns:
            raise _v1_validation_error(f"{table_name} 种子 INSERT 列顺序与表结构不一致")
        insert_values = _split_v1_top_level_csv(
            match.group("values"),
            location=f"{table_name} 种子 INSERT 值列表",
        )
        if len(insert_values) != len(insert_columns):
            raise _v1_validation_error(
                f"{table_name} 种子 INSERT 列和值数量不一致：{len(insert_columns)} 列、{len(insert_values)} 个值"
            )


def normalize_base_table_seed(seed: str) -> str:
    """只接受无符号 64 位整数的规范 ASCII 十进制字符串。"""

    valid = isinstance(seed, str) and (
        seed == "0"
        or (
            bool(seed)
            and seed[0] in "123456789"
            and all(character in "0123456789" for character in seed[1:])
        )
    )
    if not valid:
        raise ValueError("基表种子必须是规范的 ASCII 无符号十进制整数")
    value = int(seed)
    if value > MAX_BASE_TABLE_SEED:
        raise ValueError(f"基表种子不能大于 {MAX_BASE_TABLE_SEED}")
    return str(value)


def available_base_table_generator_versions() -> tuple[str, ...]:
    """按登记顺序返回所有仍受支持的生成器版本。"""

    return tuple(_GENERATORS)


def generate_base_sql_bundle(version: str, seed: str) -> BaseSqlBundle:
    """在内存中生成指定版本及种子的扩展基表包。"""

    try:
        generator = _GENERATORS[version]
    except KeyError as exc:
        raise ValueError(f"未知基表生成器版本：{version}") from exc
    normalized_seed = normalize_base_table_seed(seed)
    bundle = generator(normalized_seed)
    if version == "v1":
        _validate_v1_expanded_bundle(
            bundle,
            version=version,
            seed=normalized_seed,
        )
    return bundle


def generate_core_base_sql_bundle() -> BaseSqlBundle:
    """在内存中生成默认 42 核心列基表包。"""

    return v1.generate_base_sql_bundle("0", expand_base_table_columns=False)


def serialize_bundle(bundle: BaseSqlBundle) -> bytes:
    """以四字节大端长度前缀序列化文件名和 UTF-8 SQL。"""

    chunks: list[bytes] = []
    for sql_file in bundle.files:
        name = sql_file.path.name.encode("utf-8")
        sql = sql_file.sql.encode("utf-8")
        if len(name) > 0xFFFFFFFF or len(sql) > 0xFFFFFFFF:
            raise ValueError("基表 SQL 文件名或内容过长，无法使用 v1 序列化格式")
        chunks.extend((struct.pack(">I", len(name)), name, struct.pack(">I", len(sql)), sql))
    return b"".join(chunks)


__all__ = [
    "CURRENT_BASE_TABLE_GENERATOR_VERSION",
    "MAX_BASE_TABLE_SEED",
    "available_base_table_generator_versions",
    "generate_base_sql_bundle",
    "generate_core_base_sql_bundle",
    "normalize_base_table_seed",
    "serialize_bundle",
]
