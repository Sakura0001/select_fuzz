"""基表生成器版本注册、种子规范化和稳定序列化入口。"""

from __future__ import annotations

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
    return generator(normalized_seed)


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
